"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""
import argparse
import concurrent.futures
import copy
import datetime
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import derive
import graphstore
import manifest as manifest_mod

SCHEMA_VERSION = 1
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


def build_bundle(meta, commits, prs, issues):
    """Assemble the on-disk bundle skeleton.

    Phase 1 fills meta/commits/prs/issues; every other top-level field is
    reserved with an empty value so the schema is stable across phases.
    """
    meta = dict(meta)
    meta.setdefault("schema_version", SCHEMA_VERSION)
    return {
        "meta": meta,
        "commits": commits,
        "prs": prs,
        "issues": issues,
        # --- reserved for later phases (empty, schema-stable) ---
        "timeline": [],
        "artifacts": {},
        "feature_deltas": [],
        "code_events": [],
        "symbol_events": [],
        "symbol_moves": {"links": [], "by_confidence": {"high": 0, "medium": 0}},
        "trains": [],
        "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        "people": {},
        "halls": {},
        "flow": [],
        "blockers": [],
        "code_owners": {},
        "code_graph": {},
        "label_taxonomy": {},
        "modules": {},
        "workflow_stats": {},
        "workflows": [],
        "releases": [],
        "milestones": [],
        "docsRefs": [],
        "release_train": {},
        "sprints": {},
        "project": {},
        "diagrams": {},
    }


def parse_git_log(raw):
    """Parse `git log` output formatted with RECORD_SEP/FIELD_SEP separators.

    Each record: <sha>\x1f<parents>\x1f<author>\x1f<date>\x1f<subject> followed by
    newline-separated file paths. Returns a list of commit dicts. When the record
    carries a sixth field (the author email, `%ae`, COMMIT_WALK_ARGS) it is kept as
    a TRANSIENT `author_email` key: `resolve_commit_authors` reads it to find the
    author's GitHub login and removes it, so no email reaches the bundle (#78).
    """
    commits = []
    for chunk in raw.split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, parents, author, date, subject = (f.strip() for f in fields[:5])
        # `--name-only` paths: C-quoted ones (quote/backslash/control chars, or
        # non-ASCII without core.quotepath=false) are unquoted.
        files = [unquote_git_path(ln) for ln in lines[1:] if ln.strip()]
        commit = {
            "sha": sha,
            "parents": parents.split() if parents.strip() else [],
            "author": author,
            "date": date,
            "message": subject,
            "files": files,
            "pr": None,  # resolved in link.py
        }
        if len(fields) > 5:
            commit["author_email"] = fields[5].strip()
        commits.append(commit)
    return commits


# git log format for the full-window code-event walk (Phase 3a). Same RECORD_SEP/
# FIELD_SEP header as parse_git_log, but the BODY is `--name-status` lines so each
# changed path carries its change type (and rename/copy detection via -M -C gives
# `R###`/`C###` with old+new paths).
CODE_LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%cd%x1f%s"

# `git log` arguments (after `git -C <clone> log`) for acquire()'s three walks,
# shared with the real-repo tests so they exercise the exact commands. Paths in
# their output are made comparable by run_git (core.quotepath=false) plus
# unquote_git_path in each parser.
# The commit walk alone also asks for the author email (`%ae`, appended LAST so the
# shared five-field header parses unchanged); it is only used to resolve the
# author's GitHub login and is never stored (#78).
COMMIT_LOG_FORMAT = CODE_LOG_FORMAT + "%x1f%ae"
COMMIT_WALK_ARGS = (f"--pretty=format:{COMMIT_LOG_FORMAT}", "--date=short",
                    "--name-only")
CODE_WALK_ARGS = ("--reverse", f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short",
                  "--name-status", "-M", "-C")
PATCH_WALK_ARGS = ("--reverse", f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short",
                   "-p", "--unified=3", "-M", "-C")

_STATUS_TO_CHANGE = {"A": "add", "M": "modify", "D": "delete",
                     "R": "rename", "C": "copy", "T": "modify"}


def parse_code_events(raw):
    """Parse `git log --name-status -M -C` output into raw code-events.

    Each event: {commit, author, date, change, path[, old_path]} where change is
    add|modify|delete|rename|copy. Rename/copy lines (`R###`/`C###`) carry the old
    path in `old_path` and the new path in `path`. Pure; permissive on junk lines.
    """
    events = []
    for chunk in raw.split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, _parents, author, date, _subject = (f.strip() for f in fields[:5])
        for ln in lines[1:]:
            if not ln.strip():
                continue
            cols = ln.split("\t")
            status = cols[0].strip()
            change = _STATUS_TO_CHANGE.get(status[:1])
            if change is None or len(cols) < 2:
                continue
            ev = {"commit": sha, "author": author, "date": date, "change": change}
            # Path columns are TAB-separated; spaces are literal and a path with a
            # TAB/newline/quote (or non-ASCII, without core.quotepath=false) is
            # C-quoted, so unquote each column. Columns are not whitespace-stripped:
            # a leading/trailing space is part of the name (as in the patch walk).
            if change in ("rename", "copy"):
                if len(cols) < 3:
                    continue  # malformed R/C line (real `-M -C` always has old+new)
                ev["old_path"] = unquote_git_path(cols[1])
                ev["path"] = unquote_git_path(cols[2])
            else:
                ev["path"] = unquote_git_path(cols[1])
            events.append(ev)
    return events


# Fetch a margin of history BEFORE the window so the shallow-clone boundary commit
# predates it. The boundary commit's parent is grafted away, so `git log -p` /
# `--name-status` diff it against the EMPTY tree — a whole-tree phantom that would
# otherwise flood code_events/symbol_events with thousands of spurious "add"s. With
# the margin the first in-window commit keeps its real parent; when the margin holds
# no earlier commit, deepen_window_boundary deepens past it, and shallow_boundary_shas
# is the fallback that drops any boundary commit that still lands in-window.
CLONE_MARGIN_DAYS = 14


def _clone_margin_days():
    """Effective clone margin in days. `ACTIVITY_CLONE_MARGIN_DAYS` overrides the
    CLONE_MARGIN_DAYS default at call time (a non-int/empty value falls back to the
    default) — widen it to pull enough pre-window history that an in-window commit's
    parent is in the clone, so it is no longer a grafted boundary commit
    (recovering meta.boundary_dropped_commits). Clamped to >= 0: a negative override
    would shift `--shallow-since` AFTER `from_date`, inverting the margin guarantee
    and silently reintroducing boundary-commit gaps."""
    try:
        return max(0, int(os.environ.get("ACTIVITY_CLONE_MARGIN_DAYS", CLONE_MARGIN_DAYS)))
    except (ValueError, TypeError):
        return CLONE_MARGIN_DAYS


def _shift_date(date_str, days):
    """YYYY-MM-DD shifted by `days` (negative = earlier); input returned on parse error."""
    try:
        d = datetime.date.fromisoformat(date_str[:10])
        return (d + datetime.timedelta(days=days)).isoformat()
    except (ValueError, TypeError):
        return date_str


def _shallow_selector(from_date, depth1=False):
    """The history bound shared by clone and fetch: `--shallow-since=<from - margin>`,
    or `--depth=1` for the quiet-repo fallback."""
    if depth1:
        return "--depth=1"
    return f"--shallow-since={_shift_date(from_date, -_clone_margin_days())}"


def build_clone_cmd(repo_url, from_date, clone_dir, depth1=False):
    """Construct the bounded, partial clone command (network-free to build).

    `--shallow-since` reaches CLONE_MARGIN_DAYS before `from_date` (override with
    ACTIVITY_CLONE_MARGIN_DAYS) so the shallow boundary commit (whole-tree phantom
    diff) sits OUTSIDE the report window. `depth1=True` is the quiet-repo fallback
    (see prepare_clone): just the tip, so HEAD exists for the IaC/CODEOWNERS steps."""
    return [
        "git", "clone",
        "--filter=blob:none",
        _shallow_selector(from_date, depth1),
        "--no-single-branch",
        repo_url, clone_dir,
    ]


def build_fetch_cmd(clone_dir, from_date, depth1=False):
    """Refresh an existing clone to the same history bound a fresh clone would get.
    `--shallow-since` on fetch deepens OR shortens the shallow boundary, so a reused
    clone ends up with the same boundary as a fresh one for this window."""
    return ["git", "-C", clone_dir, "fetch", "--prune",
            _shallow_selector(from_date, depth1), "origin"]


# git's message when `--shallow-since` matches no commit on any fetched ref (the
# repo has been quiet since before `from - margin`). Only THIS failure falls back
# to --depth=1; any other clone/fetch error (network, auth, missing repo) raises.
_NO_SHALLOW_COMMITS = "no commits selected for shallow requests"


def _normalize_remote(url):
    """Comparable form of a remote URL: GitHub ssh forms mapped to https, case
    folded, trailing `/` and `.git` dropped."""
    u = (url or "").strip()
    for pre in ("git@github.com:", "ssh://git@github.com/"):
        if u.lower().startswith(pre):
            u = "https://github.com/" + u[len(pre):]
    u = u.rstrip("/")
    if u.lower().endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


def _shallow_or_depth1(run, build):
    """Run `build(depth1=False)`; on the quiet-repo failure retry `build(depth1=True)`.
    Returns True when the depth-1 fallback was used."""
    try:
        run(build(False))
        return False
    except RuntimeError as exc:
        if _NO_SHALLOW_COMMITS not in str(exc):
            raise
    run(build(True))
    return True


def _default_branch(clone_dir, run):
    """The remote's default branch name for an existing clone. Refreshes
    `origin/HEAD` first (best effort), falls back to the checked-out branch."""
    try:
        run(["git", "-C", clone_dir, "remote", "set-head", "origin", "--auto"])
    except RuntimeError:
        pass
    try:
        ref = run(["git", "-C", clone_dir, "symbolic-ref", "--short",
                   "refs/remotes/origin/HEAD"]).strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]
    except RuntimeError:
        pass
    return run(["git", "-C", clone_dir, "symbolic-ref", "--short", "HEAD"]).strip()


def prepare_clone(repo_url, from_date, clone_dir, no_clone=False, run=None):
    """Make `clone_dir` a bounded clone of `repo_url` for a window starting
    `from_date`, and return how (recorded as `meta.clone_mode`):

    - "fresh": no (or an empty) dir -> `git clone --shallow-since=<from - margin>`.
    - "fetched": the dir is already a clone of the SAME remote -> `git fetch
      --shallow-since=...` (deepens or shortens to the window's bound) and check
      out the remote default branch head. So a second monthly run into the same
      `--clone-dir` works, and `--no-clone` no longer walks stale history.
    - "depth1-fallback": `--shallow-since` selected no commits (a quiet repo) ->
      retried with `--depth=1`, so HEAD exists for IaC edges / CODEOWNERS / the
      structural scan; the caller treats the commit walks as empty.
    - "none": `no_clone` and no clone on disk -> nothing created; walks degrade
      to empty.

    An existing non-empty dir that is not a git clone, or is a clone of a
    different remote, raises RuntimeError and is left untouched (never deleted).
    """
    run = run or run_git
    exists = os.path.exists(clone_dir)
    if exists and os.path.isdir(clone_dir) and not os.listdir(clone_dir):
        exists = False      # an empty dir is as good as none: git clone accepts it
    if exists:
        if not os.path.exists(os.path.join(clone_dir, ".git")):
            raise RuntimeError(
                f"--clone-dir {clone_dir} exists but is not a git clone; "
                f"point --clone-dir elsewhere or remove it")
        try:
            have = run(["git", "-C", clone_dir, "config", "--get",
                        "remote.origin.url"]).strip()
        except RuntimeError:
            have = ""
        if _normalize_remote(have) != _normalize_remote(repo_url):
            raise RuntimeError(
                f"--clone-dir {clone_dir} is a clone of a different remote "
                f"({have or 'no origin'}), not {repo_url}; point --clone-dir elsewhere")
        depth1 = _shallow_or_depth1(
            run, lambda d1: build_fetch_cmd(clone_dir, from_date, d1))
        branch = _default_branch(clone_dir, run)
        run(["git", "-C", clone_dir, "checkout", "-q", "-B", branch,
             f"refs/remotes/origin/{branch}"])
        return "depth1-fallback" if depth1 else "fetched"
    if no_clone:
        return "none"
    # git clone needs the parent dir to exist for the default workspace/ path.
    os.makedirs(os.path.dirname(clone_dir) or ".", exist_ok=True)
    depth1 = _shallow_or_depth1(
        run, lambda d1: build_clone_cmd(repo_url, from_date, clone_dir, d1))
    return "depth1-fallback" if depth1 else "fresh"


def shallow_boundary_shas(clone_dir):
    """SHAs at the shallow-clone boundary (from `.git/shallow`). Their parents are
    grafted away, so a diff against them is a whole-tree phantom — exclude them from
    the code/symbol walks. Empty for a full clone or when the file is absent."""
    try:
        with open(os.path.join(clone_dir, ".git", "shallow")) as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except OSError:
        return set()


def drop_boundary_events(events, boundary):
    """Drop events whose commit is a shallow boundary SHA (phantom whole-tree diffs)."""
    if not boundary:
        return events
    return [e for e in events if e.get("commit") not in boundary]


def in_window_boundary_commits(boundary, commits):
    """Boundary SHAs that are ALSO in-window commits. Their diffs were dropped as
    whole-tree phantoms (parent grafted away), so their real changes are a gap — the
    margin had no earlier commit to anchor them. Surfaced in meta so it's never silent;
    widening CLONE_MARGIN_DAYS recovers them. Sorted; empty in the common case."""
    shas = {c.get("sha") for c in commits}
    return sorted(s for s in boundary if s in shas)


# `git fetch --deepen=<n>` steps tried, in order, while an in-window commit of the
# walked branch still sits on the shallow boundary (#68). One commit past the
# boundary is normally enough (the parent predates `from - margin`); the larger
# steps cover non-monotonic committer dates. Bounded: at most 1+2+4+8 extra commits
# per boundary, four fetches.
BOUNDARY_DEEPEN_STEPS = (1, 2, 4, 8)


def _in_window_boundary_on_head(clone_dir, from_date, to_date, run):
    """Shallow-boundary SHAs that are in-window commits of the checked-out branch
    (the history the three walks read). Sorted. A root commit is never listed:
    git records only grafted commits in `.git/shallow`."""
    boundary = shallow_boundary_shas(clone_dir)
    if not boundary:
        return []
    raw = run(["git", "-C", clone_dir, "log", f"--pretty=format:{CODE_LOG_FORMAT}",
               "--date=short"])
    head = window_records(parse_git_log(raw), from_date, to_date)
    return in_window_boundary_commits(boundary, head)


def deepen_window_boundary(clone_dir, from_date, to_date, run=None,
                           steps=BOUNDARY_DEEPEN_STEPS):
    """Make in-window shallow-boundary commits real (#68).

    A boundary commit's parent is grafted away, so git diffs it against the empty
    tree (a whole-tree phantom). When the clone margin held no earlier commit, the
    first in-window commit is such a commit. Deepen the clone (`git fetch
    --deepen=<n>`, n from `steps`) until no in-window commit of the walked branch
    is on the boundary. Stops at the first fetch error (not retried) or when the
    steps run out. Returns the in-window boundary SHAs still left (sorted; empty
    when resolved) — the caller falls back to dropping their diffs and files."""
    run = run or run_git
    left = _in_window_boundary_on_head(clone_dir, from_date, to_date, run)
    for n in steps:
        if not left:
            break
        try:
            run(["git", "-C", clone_dir, "fetch", f"--deepen={n}", "origin"])
        except RuntimeError as exc:
            sys.stderr.write(
                f"warning: could not deepen {clone_dir} past the shallow boundary: "
                f"{str(exc).strip().splitlines()[-1] if str(exc).strip() else exc}\n")
            break
        left = _in_window_boundary_on_head(clone_dir, from_date, to_date, run)
    return left


def in_window(ts, from_date, to_date):
    """True if ISO date/datetime string `ts` falls within [from_date, to_date]
    inclusive. None/empty is never in window. Bounds may be date-only or ISO;
    the granularity-safe rule is `graphstore.ts_in_window` (a date-only value
    covers its whole day, so an ISO `from` no longer drops the first day)."""
    return graphstore.ts_in_window(ts, from_date, to_date)


def window_records(records, from_date, to_date):
    """Keep only commits/events whose `date` (committer/landing date) falls in
    [from,to] inclusive. Pure. Used instead of git's `--since/--until`, which
    silently prunes valid in-window commits when commit dates are non-monotonic
    across committer timezones (and excludes the upper boundary day)."""
    return [r for r in records if in_window(r.get("date"), from_date, to_date)]


# GitHub closing-keyword grammar (#62). A keyword (close/closes/closed,
# fix/fixes/fixed, resolve/resolves/resolved; any case), an optional `:`, then
# ONE reference: `#N`, `owner/repo#N`, or a full
# `https://github.com/owner/repo/(issues|pull)/N` URL. Each closed issue needs its
# own keyword: in "Fixes #1, #2" / "Fixes #1 and #2" only #1 closes; #2 is a
# *listed* ref (GitHub links it as a mention, never closes it). Closing keywords
# link issues only, so a keyword before a `/pull/` URL is a cross-ref, not a
# close. No Markdown parsing: a keyword inside a code block / code span matches
# like anywhere else. Owners/repos: GitHub names — alnum start, then word/.-.
_CLOSING_KW = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_REF = (
    r"(?:https?://github\.com/(?P<uowner>[A-Za-z0-9][\w.-]*)/"
    r"(?P<urepo>[A-Za-z0-9][\w.-]*)/(?P<ukind>issues|pull)/(?P<unum>\d+)"
    r"|(?:(?P<qowner>[A-Za-z0-9][\w.-]*)/(?P<qrepo>[A-Za-z0-9][\w.-]*))?"
    r"#(?P<num>\d+))"
)
_CLOSING_RE = re.compile(_CLOSING_KW + r"(?:\s*:\s*|\s+)" + _REF, re.IGNORECASE)
# A further ref continuing a closing keyword's list WITHOUT its own keyword:
# ", #2", ", and #2", " and #2", " & #2". Applied (anchored) right after a match.
_LIST_TAIL_RE = re.compile(r"(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)" + _REF,
                           re.IGNORECASE)

# Cross-repo references (Phase 9). Any github.com issue/PR URL that is not a
# keyword-closed issue -> a `cross_ref` mention (issues -> issue node, pull -> pr
# node). Bare `#N` stays same-repo (parse_closing_refs).
_QUALIFIED_URL_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9][\w.-]*)/(?P<repo>[A-Za-z0-9][\w.-]*)/"
    r"(?P<kind>issues|pull)/(?P<num>\d+)",
)


def _ref_from_match(m):
    """(owner, repo, number, is_pr) for a `_REF` match; owner/repo None for a
    bare `#N`. is_pr is True only for a `/pull/` URL (a bare/qualified `#N`
    cannot be told apart from an issue without the API)."""
    if m.group("unum") is not None:
        return (m.group("uowner"), m.group("urepo"), int(m.group("unum")),
                m.group("ukind").lower() == "pull")
    return m.group("qowner"), m.group("qrepo"), int(m.group("num")), False


def _scan_closing(text):
    """Every closing-keyword reference in `text`, in order, as
    (owner, repo, number, is_pr, closes): the ref right after a keyword has
    closes=True unless it is a `/pull/` URL; each list-tail ref after it
    (", #2", " and #2") has closes=False. Pure."""
    text = text or ""
    out = []
    for m in _CLOSING_RE.finditer(text):
        owner, repo, num, is_pr = _ref_from_match(m)
        out.append((owner, repo, num, is_pr, not is_pr))
        pos = m.end()
        while True:
            t = _LIST_TAIL_RE.match(text, pos)
            if not t:
                break
            out.append(_ref_from_match(t) + (False,))
            pos = t.end()
    return out


def _same_repo(owner, repo, slug):
    """True if a ref is in the current repo: a bare `#N` (owner None) always is;
    a qualified ref/URL only when `slug` ('owner/repo') matches,
    case-insensitively (GitHub names are). Without a slug only bare refs are."""
    if owner is None:
        return True
    return bool(slug) and "{}/{}".format(owner, repo).lower() == slug.lower()


def parse_qualified_refs(text):
    """Cross-repo refs in PR/issue text, ordered + deduped. Returns a list of
    {owner, repo, number, kind, is_pr}: a keyword-closed qualified issue ref
    (`Closes owner/repo#N`, `Fixes: https://github.com/o/r/issues/N`) -> kind
    'closes' (is_pr False); every other github.com URL, a keyword before a
    `/pull/` URL, and an unkeyworded list-tail qualified ref
    (`Fixes a/b#1, a/b#2` -> #2) -> kind 'cross_ref' (is_pr True for /pull/).
    Bare `#N` is left to parse_closing_refs (same-repo). Same-repo qualified refs
    are returned too; callers skip them by slug. Dedupe is case-insensitive on
    owner/repo (GitHub names are, issue #70); the first spelling seen is kept.
    Pure."""
    out, seen = [], set()

    def add(owner, repo, num, kind, is_pr):
        key = (owner.lower(), repo.lower(), num)
        if key not in seen:
            seen.add(key)
            out.append({"owner": owner, "repo": repo, "number": num,
                        "kind": kind, "is_pr": is_pr})

    scanned = [r for r in _scan_closing(text) if r[0] is not None]
    for owner, repo, num, is_pr, closes in scanned:
        if closes:
            add(owner, repo, num, "closes", False)
    for owner, repo, num, is_pr, closes in scanned:
        if not closes:
            add(owner, repo, num, "cross_ref", is_pr)
    for m in _QUALIFIED_URL_RE.finditer(text or ""):
        add(m.group("owner"), m.group("repo"), int(m.group("num")), "cross_ref",
            m.group("kind") == "pull")
    return out


def parse_closing_refs(text, repo=None):
    """Same-repo issue numbers GitHub would CLOSE from `text`, de-duplicated,
    order-preserving (see `_CLOSING_RE`). `repo` ('owner/repo', optional) lets a
    same-repo qualified ref or full issue URL count; without it only bare `#N`
    closes (backwards compatible). Cross-repo closes are parse_qualified_refs'."""
    out = []
    for owner, rname, n, _is_pr, closes in _scan_closing(text):
        if closes and _same_repo(owner, rname, repo) and n not in out:
            out.append(n)
    return out


def parse_listed_refs(text, repo=None):
    """Same-repo numbers that continue a closing keyword's list without their
    own keyword ("Fixes #1, #2" -> [2]): GitHub links these as mentions but does
    not close them, so they are cross-references. Numbers closed elsewhere in
    `text` are excluded. Ordered, deduped. `repo` as in parse_closing_refs. Pure."""
    closed = set(parse_closing_refs(text, repo))
    out = []
    for owner, rname, n, is_pr, closes in _scan_closing(text):
        if (not closes and not is_pr and _same_repo(owner, rname, repo)
                and n not in closed and n not in out):
            out.append(n)
    return out


# Issue dependency phrasing -> a directed `blocks` relation (issue->issue). "this
# blocks #N" / "blocking #N" is an OUTbound block (this -> N); "blocked by #N" /
# "depends on #N" is INbound (N -> this). Conservative keyword set so prose like
# "this unblocks the path" never trips it. Same-repo bare `#N` only.
_BLOCKS_RE = re.compile(
    r"\b(?P<kw>blocked\s+by|depends\s+on|blocking|blocks?)\s+#(?P<num>\d+)",
    re.IGNORECASE,
)
_BLOCKS_INBOUND = ("blocked by", "depends on")


def parse_blocks_refs(text):
    """Directed `blocks` refs in issue text, ordered + deduped. Returns
    [{number, direction}] where direction is 'out' (this issue blocks #N) or 'in'
    (this issue is blocked by / depends on #N). Pure."""
    out, seen = [], set()
    for m in _BLOCKS_RE.finditer(text or ""):
        kw = " ".join(m.group("kw").lower().split())
        num = int(m.group("num"))
        direction = "in" if kw in _BLOCKS_INBOUND else "out"
        key = (num, direction)
        if key not in seen:
            seen.add(key)
            out.append({"number": num, "direction": direction})
    return out


def normalize_pr(raw):
    """Map a GitHub REST PR object to the bundle's PR shape."""
    milestone = raw.get("milestone")
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "milestone": milestone.get("title") if milestone else None,
        "merged": bool(raw.get("merged_at")),
        "merged_by": (raw.get("merged_by") or {}).get("login")
        if raw.get("merged_by") else None,
        "merged_at": raw.get("merged_at"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "state": raw.get("state"),
        # base/head branch refs: a PR whose base isn't the analyzed branch is a
        # stacked/fork PR (it merged into another branch, not main) — kept as
        # context but not counted as shipped-to-main (see link.build_trains).
        "base": (raw.get("base") or {}).get("ref"),
        "head": (raw.get("head") or {}).get("ref"),
        "comments": raw.get("comments", 0) or 0,
        "review_comments_count": raw.get("review_comments", 0) or 0,
        "reviewers": [],
        "review_decision": "none",
        # seeded with listed-but-not-closed refs ("Fixes #1, #2" -> [2]);
        # acquire() currently REPLACES this with the timeline cross-refs.
        "crossref_issues": parse_listed_refs(_pr_text(raw), _pr_repo_slug(raw)),
        "closes": parse_closing_refs(_pr_text(raw), _pr_repo_slug(raw)),
        "url": raw.get("html_url"),
    }


def _pr_text(raw):
    """The text GitHub scans for closing keywords: title + body."""
    return (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")


def _pr_repo_slug(raw):
    """'owner/repo' of a raw REST PR (base repo, else parsed from html_url), or
    None. Lets parse_closing_refs recognise a same-repo issue URL. Pure."""
    full = (((raw.get("base") or {}).get("repo")) or {}).get("full_name")
    if full:
        return full
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/\d+",
                 raw.get("html_url") or "")
    return m.group(1) if m else None


def select_merged_prs(prs, from_date, to_date):
    """Return normalized PRs merged within [from_date, to_date]."""
    return [p for p in prs if p["merged"] and in_window(p["merged_at"], from_date, to_date)]


def normalize_issue(raw):
    """Map a GitHub REST issue object to the bundle's issue shape."""
    milestone = raw.get("milestone")
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "kind": "other",  # refined in later phases (issue types/labels/template)
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "assignees": [a["login"] for a in raw.get("assignees", [])],
        "milestone": milestone.get("title") if milestone else None,
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "comments": raw.get("comments", 0) or 0,
        # directed issue->issue dependency refs parsed from title+body (same-repo
        # bare #N); fold emits `blocks` edges between gathered issues.
        "blocks": parse_blocks_refs(
            (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")
        ),
        "url": raw.get("html_url"),
    }


# Submitted review states (GitHub: APPROVED, CHANGES_REQUESTED, COMMENTED,
# DISMISSED; PENDING is an unsubmitted draft and is never counted). Only the
# "decisive" ones set a reviewer's effective state: GitHub records every
# review-thread reply as a COMMENTED review and keeps the earlier approval /
# change request in force, and a dismissed review (whose state GitHub rewrites
# to DISMISSED) no longer gates the merge.
_DECISIVE_REVIEW_STATES = ("approved", "changes_requested", "dismissed")
_SUBMITTED_REVIEW_STATES = _DECISIVE_REVIEW_STATES + ("commented",)


def _review_order_key(index, r):
    """Chronological sort key for a raw review: (submitted_at, id, list index).
    ISO-8601 UTC `Z` timestamps sort correctly as plain strings; GitHub review
    ids grow monotonically, so they break same-second ties; the input position
    is the final, deterministic fallback."""
    rid = r.get("id")
    return (r.get("submitted_at") or "",
            rid if isinstance(rid, int) and not isinstance(rid, bool) else -1,
            index)


def summarize_reviews(raw_reviews):
    """Reduce raw PR reviews to {reviewers, decision}. Pure.

    `reviewers`: sorted logins with at least one SUBMITTED review (PENDING
    drafts and unknown states are ignored).

    `decision` (issue #63): each reviewer's effective state is their latest
    APPROVED / CHANGES_REQUESTED / DISMISSED review — a later COMMENTED review
    never overrides it; a reviewer with none of those is `commented`; an
    effective DISMISSED contributes nothing. Overall: `changes_requested` if any
    reviewer's effective state is that, else `approved` if any, else
    `commented` if any, else `none`."""
    indexed = sorted(enumerate(raw_reviews or []),
                     key=lambda p: _review_order_key(p[0], p[1]))
    effective = {}  # login -> effective state
    for _, r in indexed:
        login = (r.get("user") or {}).get("login")
        if not login:
            continue
        state = (r.get("state") or "").lower()
        if state not in _SUBMITTED_REVIEW_STATES:
            continue
        if state in _DECISIVE_REVIEW_STATES:
            effective[login] = state          # later decisive review wins
        else:
            effective.setdefault(login, "commented")
    states = set(effective.values())
    for decision in ("changes_requested", "approved", "commented"):
        if decision in states:
            break
    else:
        decision = "none"
    return {"reviewers": sorted(effective), "decision": decision}


def normalize_review(raw):
    """Map a raw PR review submission to the bundle's review shape. Pure.

    Phase 10 slice 1: `summarize_reviews` reduces reviews to {reviewers,
    decision} and DISCARDS the individual submissions; this KEEPS each one so
    the fold can persist it as a `review` social node (the review-rounds
    texture). A review's `html_url` is surfaced as `url` for provenance; when
    absent, the fold synthesizes a stable `<pr url>#pullrequestreview-<id>`
    ref (so check_provenance never fails)."""
    return {
        "id": raw.get("id"),
        "author": (raw.get("user") or {}).get("login"),
        "state": (raw.get("state") or "").lower() or None,
        "submitted_at": raw.get("submitted_at"),
        "body": raw.get("body"),
        "url": raw.get("html_url"),
    }


# Lifecycle events we persist as first-class `event` social nodes. Conservative
# allowlist (the spec's decision): `cross-referenced` stays on its existing
# crossref/xref path and is NOT duplicated here.
_LIFECYCLE_EVENTS = ("reopened", "closed", "ready_for_review")


def parse_timeline_lifecycle(raw_timeline):
    """Normalize the allowlisted timeline lifecycle events. Pure.

    Keeps only `reopened`/`closed`/`ready_for_review` (order-preserving), each as
    {id, actor, event, created_at, label, url}. `url` is None when the raw event
    carries none — the fold synthesizes a stable `<parent html_url>#event-<id>`
    provenance ref in that case (so check_provenance passes)."""
    out = []
    for ev in raw_timeline or []:
        event = ev.get("event")
        if event not in _LIFECYCLE_EVENTS:
            continue
        out.append({
            "id": ev.get("id"),
            "actor": (ev.get("actor") or {}).get("login"),
            "event": event,
            "created_at": ev.get("created_at"),
            "label": (ev.get("label") or {}).get("name"),
            "url": ev.get("url"),
        })
    return out


def parse_timeline_merged_by(raw_timeline):
    """Login of the actor of the (last) `merged` timeline event, or None. Pure.

    The list-pulls endpoint omits `merged_by` (only GET /pulls/{n} carries it);
    acquire already fetches each kept PR's issue timeline, whose `merged` event
    actor is the merger, so this recovers it with no extra API call. A deleted
    (ghost) actor yields None."""
    login = None
    for ev in raw_timeline or []:
        if ev.get("event") == "merged":
            login = (ev.get("actor") or {}).get("login")
    return login


_TRAILING_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_PR_RE = re.compile(r"Merge pull request #(\d+)")
_PAREN_PR_RE = re.compile(r"\(#(\d+)\)")


def resolve_commit_pr(message):
    """Best-effort PR number from a commit message (#62). In order:
      1. a trailing `(#N)` on the SUBJECT line (squash merges append the PR
         number last, so `Revert "x (#4012)" (#4020)` -> 4020, backports too);
      2. `Merge pull request #N` (merge-commit style);
      3. the LAST `(#N)` on the subject line, then the last in the message."""
    message = message or ""
    subject = message.split("\n", 1)[0]
    m = _TRAILING_PR_RE.search(subject)
    if m:
        return int(m.group(1))
    m = _MERGE_PR_RE.search(message)
    if m:
        return int(m.group(1))
    for scope in (subject, message):
        hits = _PAREN_PR_RE.findall(scope)
        if hits:
            return int(hits[-1])
    return None


def attach_commit_prs(commits):
    """Set each commit's `pr` from its message in place."""
    for c in commits:
        c["pr"] = resolve_commit_pr(c.get("message", ""))
    return commits


# --- People identity (#78): commit author -> GitHub login --------------------
#
# PR/issue/review participants are GitHub LOGINS; git gives a commit author only
# as a free-text NAME (`%an`) and an email (`%ae`). Each commit's author is
# resolved to a login from real data, in this order:
#   1. the commits-list API's `author.login` for that SHA (GitHub's own link of the
#      commit email to an account) — `login_source: "commits_api"`;
#   2. a `users.noreply.github.com` author email, which embeds the login
#      (`<id>+<login>@…`, or the older `<login>@…`) — `login_source: "noreply_email"`;
#   3. otherwise the git name, flagged `login_resolved: false` and keyed by
#      `derive.unresolved_person_key` so it can never equal (merge with) a login.
# The PR author is NOT assumed to be the commit author: no guessing.

_NOREPLY_EMAIL_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?)"
    r"@users\.noreply\.github\.com$", re.IGNORECASE)


def login_from_noreply_email(email):
    """The GitHub login embedded in a `users.noreply.github.com` email, or None.
    Pure."""
    m = _NOREPLY_EMAIL_RE.match((email or "").strip())
    return m.group(1) if m else None


def commit_logins_from_api(items):
    """{sha: login} from commits-list API items (`author.login`; `author` is null
    when GitHub links the commit email to no account — those are skipped). Pure."""
    out = {}
    for it in items or []:
        login = ((it or {}).get("author") or {}).get("login")
        if it.get("sha") and login:
            out[it["sha"]] = login
    return out


def commits_api_url(api, head_sha, frm, to):
    """The commits-list listing for the walked history: from the clone's HEAD,
    bounded to the window widened by one day each side (git dates are in the
    committer's own timezone; the SHA join discards anything extra). Pure."""
    query = urllib.parse.urlencode([
        ("sha", head_sha),
        ("since", _shift_date(frm, -1)[:10] + "T00:00:00Z"),
        ("until", _shift_date(to, 1)[:10] + "T23:59:59Z"),
        ("per_page", "100"),
    ], safe=":")
    return f"{api}/commits?{query}"


def resolve_commit_authors(commits, api_logins, known_logins=()):
    """Resolve each commit's `author` to a GitHub login, in place (#78).

    Sets `author` (the login, or the unresolved person key), `author_name` (the
    git `%an`), `login_resolved` and, when resolved, `login_source`; removes the
    transient `author_email`. A noreply-email login takes the spelling of a
    `known_logins` entry that differs only in case (GitHub logins are
    case-insensitive). Returns {sha: author} so the code/symbol events of the
    same commits can be re-keyed (`remap_event_authors`). Deterministic."""
    by_lower = {}
    for login in sorted(k for k in known_logins if k):
        by_lower.setdefault(login.lower(), login)
    out = {}
    for c in commits:
        name = c.get("author_name", c.get("author")) or ""
        email = c.pop("author_email", None)
        login, source = api_logins.get(c.get("sha")), "commits_api"
        if not login:
            login, source = login_from_noreply_email(email), "noreply_email"
            if login:
                login = by_lower.get(login.lower(), login)
        c["author_name"] = name
        if login:
            c["author"] = login
            c["login_resolved"] = True
            c["login_source"] = source
        else:
            c["author"] = derive.unresolved_person_key(name)
            c["login_resolved"] = False
            c.pop("login_source", None)
        if c.get("sha"):
            out[c["sha"]] = c["author"]
    return out


def remap_event_authors(events, by_sha):
    """Re-key code/symbol events' `author` to their commit's resolved author, in
    place, so artifact lifecycle credit lands on the same person as the commit.
    Events of a commit not in `by_sha` keep their git name. Pure otherwise."""
    for ev in events:
        if ev.get("commit") in by_sha:
            ev["author"] = by_sha[ev["commit"]]
    return events


def parse_timeline_crossrefs(raw_timeline):
    """Issue numbers cross-referenced/connected to a PR via timeline events.

    De-duplicated, order-preserving. Skips cross-refs whose source is itself a
    pull request (we only want issue links). Pure."""
    out = []
    for ev in raw_timeline or []:
        kind = ev.get("event")
        num = None
        if kind == "cross-referenced":
            issue = (ev.get("source") or {}).get("issue") or {}
            if issue.get("pull_request") is None:
                num = issue.get("number")
        elif kind == "connected":
            num = (ev.get("subject") or {}).get("number")
        if num is not None and num not in out:
            out.append(num)
    return out


def parse_timeline_xrefs(raw_timeline, current_slug):
    """Cross-REPO timeline cross-references (Phase 9): cross-referenced events
    whose source issue/PR lives in a DIFFERENT repo than `current_slug`
    ('owner/repo'). Same-repo refs are left to parse_timeline_crossrefs. Repo
    comparison and dedupe are case-insensitive (the API's canonical `full_name`
    may differ in case from the manifest / --owner spelling, issue #70). Returns
    ordered, deduped [{owner, repo, number, kind='cross_ref', is_pr}]. Pure."""
    out, seen = [], set()
    current = (current_slug or "").lower()
    for ev in raw_timeline or []:
        if ev.get("event") != "cross-referenced":
            continue
        src = (ev.get("source") or {}).get("issue") or {}
        full = ((src.get("repository") or {}).get("full_name")) or ""
        num = src.get("number")
        if not full or full.lower() == current or num is None:
            continue
        key = (full.lower(), num)
        if key in seen:
            continue
        seen.add(key)
        owner, _, repo = full.partition("/")
        out.append({"owner": owner, "repo": repo, "number": num,
                    "kind": "cross_ref", "is_pr": src.get("pull_request") is not None})
    return out


def normalize_workflow(raw):
    """Map a GitHub Actions run object to the bundle's workflow shape."""
    return {
        "name": raw.get("name"),
        "conclusion": raw.get("conclusion"),
        "status": raw.get("status"),
        "event": raw.get("event"),
        "head_branch": raw.get("head_branch"),
        "created_at": raw.get("created_at"),
        "url": raw.get("html_url"),
    }


def aggregate_workflow_stats(workflows):
    """Count runs per workflow name by conclusion. Pure.

    Buckets conclusions into success/failure/cancelled/other so the report can
    show a CI health line per workflow."""
    stats = {}
    for wf in workflows or []:
        name = wf.get("name") or "(unnamed)"
        s = stats.setdefault(
            name, {"total": 0, "success": 0, "failure": 0, "cancelled": 0, "other": 0})
        s["total"] += 1
        conclusion = wf.get("conclusion")
        if conclusion in ("success", "failure", "cancelled"):
            s[conclusion] += 1
        else:
            s["other"] += 1
    return stats


def normalize_release(raw):
    """Map a GitHub release object to the bundle's release shape."""
    return {
        "tag_name": raw.get("tag_name"),
        "name": raw.get("name"),
        "published_at": raw.get("published_at"),
        "prerelease": bool(raw.get("prerelease")),
        "url": raw.get("html_url"),
    }


def normalize_milestone(raw):
    """Map a GitHub milestone object to the bundle's milestone shape."""
    return {
        "title": raw.get("title"),
        "number": raw.get("number"),
        "state": raw.get("state"),
        "due_on": raw.get("due_on"),
        "open_issues": raw.get("open_issues", 0) or 0,
        "closed_issues": raw.get("closed_issues", 0) or 0,
        "url": raw.get("html_url"),
    }


def _normalize_comment_obj(raw):
    """Shared mapping for conversation + review comments: the bundle's comment
    shape {id, author, author_association, body, url, created_at}. Pure, permissive."""
    return {
        "id": raw.get("id"),
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "body": raw.get("body") or "",
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
    }


def normalize_comment(raw):
    """Map a GitHub issue/PR conversation comment to the bundle's comment shape."""
    return _normalize_comment_obj(raw)


def normalize_review_comment(raw):
    """Map a GitHub PR review comment (inline diff comment) to the same shape."""
    return _normalize_comment_obj(raw)


# The four reaction kinds the bundle tracks (the upvote/downvote/affinity signal
# the flow analysis keys on), plus a derived total.
_TRACKED_REACTIONS = ("+1", "-1", "heart", "hooray")

# Thresholds for the Phase-3a `open_high_activity` signal: an OPEN issue with
# meaningful discussion or upvotes. Deliberately permissive — it is a hint for
# the "open risks" report section, not a hard classification.
_HIGH_ACTIVITY_COMMENTS = 5
_HIGH_ACTIVITY_UPVOTES = 5


def summarize_reactions(raw):
    """Reduce a GitHub reactions object to {'+1','-1','heart','hooray','total'}.
    Pure, permissive: missing keys count as 0; total prefers `total_count`, else
    sums the tracked keys."""
    raw = raw or {}
    out = {k: int(raw.get(k) or 0) for k in _TRACKED_REACTIONS}
    if raw.get("total_count") is not None:
        out["total"] = int(raw["total_count"])
    else:
        out["total"] = sum(out[k] for k in _TRACKED_REACTIONS)
    return out


def derive_open_high_activity(issue):
    """True for an OPEN issue with notable engagement (many comments or upvotes).
    A cheap surface for the report's open-risks section. Pure."""
    if issue.get("state") != "open":
        return False
    comments = issue.get("comments", 0) or 0
    upvotes = (issue.get("reactions") or {}).get("+1", 0) or 0
    return comments >= _HIGH_ACTIVITY_COMMENTS or upvotes >= _HIGH_ACTIVITY_UPVOTES


# Re-export derive's canonical path classifier so `gather.classify_artifact_path`
# (used by build_artifacts callers and test_gather) and `derive.classify_artifact_path`
# are ONE function. gather→derive is acyclic (derive is a stdlib-only leaf).
classify_artifact_path = derive.classify_artifact_path


# ---- Phase 3d: symbol-granular change attribution (diff-local, build-free) ----
# A single `git log -p` walk yields per-(commit,path) hunks; we attribute changes to
# symbol DECLARATIONS recognised within each hunk's lines (no per-commit `git show`,
# no build). Heuristic but pure and offline-testable. Identity across renames is 3e.

_BICEP_DECL_RE = re.compile(r"^\s*(param|var|output|resource|module)\s+([A-Za-z_]\w*)")
_BICEP_COMMENT_RE = re.compile(r"^\s*(//|/\*|\*/|\*|@sys\.description|@description)")
_TF_RES_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"')
_TF_BLOCK_RE = re.compile(r'^\s*(variable|output|module)\s+"([^"]+)"')
_TF_COMMENT_RE = re.compile(r"^\s*(#|//)")
# Hunk header `@@ -a[,b] +c[,d] @@`: groups (old_start, old_count, new_start,
# new_count); an omitted count means 1. The counts delimit the hunk exactly.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Actionable/decision markers — surfaced as subkind `todo` so the report can focus on
# them (these are where ideas get broken down into follow-on issues/PRs).
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG)\b", re.IGNORECASE)


def _comment_decl(text):
    """Classify a matched comment line -> (kind, subkind, name), or None. The comment
    TEXT is the identity (capped) so a comment REPLACED as a decision evolves is tracked
    as the old text dropped + the new text added, not collapsed away. `todo` flags
    markers. Decorative separators/banners (no alphanumeric content) are not tracked."""
    body = text.strip()
    if not re.search(r"[A-Za-z0-9]", body):
        return None   # e.g. `// ======`, `// ------` — no decision content
    subkind = "todo" if _TODO_RE.search(body) else "comment"
    return ("comment", subkind, body[:140])


def symbol_lang(path):
    """Source language for symbol attribution, or None (file-level only)."""
    if (path or "").endswith(".bicep"):
        return "bicep"
    if (path or "").endswith(".tf"):
        return "terraform"
    return None


def detect_symbol_decl(lang, text):
    """One source line (diff sign already stripped) -> (kind, subkind, name) or None.

    kind is `symbol` (param/var/output/resource/module) or `comment`. For comments the
    `name` is the comment TEXT (capped) and subkind is `todo` for decision markers else
    `comment`; decorative banners return None. Pure."""
    if lang == "bicep":
        m = _BICEP_DECL_RE.match(text)
        if m:
            return ("symbol", m.group(1), m.group(2))
        if _BICEP_COMMENT_RE.match(text):
            return _comment_decl(text)
        return None
    if lang == "terraform":
        m = _TF_RES_RE.match(text)
        if m:
            return ("symbol", "resource", f"{m.group(1)}.{m.group(2)}")
        m = _TF_BLOCK_RE.match(text)
        if m:
            return ("symbol", m.group(1), m.group(2))
        if _TF_COMMENT_RE.match(text):
            return _comment_decl(text)
        return None
    return None


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
              '"': 34, "\\": 92}


def unquote_git_path(path):
    """Undo git's C-style path quoting. Pure.

    git prints a path wrapped in double quotes, with `\\"`, `\\\\`, `\\t`, `\\n`
    (etc.) escapes and `\\ooo` octal bytes, when it contains a quote, backslash
    or control character — and, unless `core.quotepath=false`, any byte >= 0x80
    (`"docs/caf\\303\\251.md"`). The octal bytes are reassembled and decoded as
    UTF-8 (`errors="replace"`, like run_git). Anything not wrapped in quotes is
    returned unchanged."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        else:
            out += bytes([_C_ESCAPES[nxt]]) if nxt in _C_ESCAPES else nxt.encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def _diff_header_path(rest):
    """Path from a `--- `/`+++ ` header remainder: `/dev/null` -> None, C-quoting
    undone, the `a/`/`b/` prefix dropped. git appends one TAB when the path
    contains a space (`--- a/with space.md<TAB>`); only that TAB is removed, so
    other whitespace in a name survives."""
    if rest.endswith("\t"):
        rest = rest[:-1]
    if rest == "/dev/null":
        return None
    p = unquote_git_path(rest)
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_unified_diff(patch):
    """Parse a (multi-file) unified diff into per-file hunks. Pure.

    -> [{"path","old_path","hunks":[{"new_start":int,"lines":[(sign,text), ...]}]}]
    sign in {' ','+','-'}; `/dev/null` paths normalise to None (pure add/delete).

    Hunk boundaries come from the `@@ -a,b +c,d @@` counts (omitted count = 1):
    exactly b old-side (` `/`-`) and d new-side (` `/`+`) lines are consumed, so
    a removed line `-- x` or an added line `++ y` (rendered `--- x` / `+++ y`) is
    hunk content, never a file header. `--- `/`+++ ` and `rename|copy from|to`
    are read only in a file's header block (after `diff --git`, before its first
    `@@`). `\\ No newline at end of file` is skipped and not counted. Once a
    hunk's counts are exhausted, lines other than a new `@@` or `diff --git` are
    ignored. C-quoted paths are unquoted (unquote_git_path)."""
    files, cur, hunk = [], None, None
    old_left = new_left = 0
    for line in (patch or "").split("\n"):
        if line.startswith("diff --git "):
            # Never hunk content: every content line carries a sign prefix.
            cur = {"path": None, "old_path": None, "hunks": []}
            files.append(cur)
            hunk, old_left, new_left = None, 0, 0
            continue
        if cur is None:
            continue
        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith("\\"):
                continue                       # "\ No newline at end of file"
            if line == "":
                line = " "                     # blank context line, prefix stripped
            sign = line[0]
            if sign == " " and old_left > 0 and new_left > 0:
                old_left, new_left = old_left - 1, new_left - 1
            elif sign == "-" and old_left > 0:
                old_left -= 1
            elif sign == "+" and new_left > 0:
                new_left -= 1
            else:
                # Inconsistent with the declared counts (malformed/truncated input):
                # close the hunk and let the line be checked for a new `@@` below.
                old_left = new_left = 0
                sign = None
            if sign is not None:
                hunk["lines"].append((sign, line[1:]))
                continue
        m = _HUNK_RE.match(line)
        if m:
            old_left = 1 if m.group(2) is None else int(m.group(2))
            new_left = 1 if m.group(4) is None else int(m.group(4))
            hunk = {"new_start": int(m.group(3)), "lines": []}
            cur["hunks"].append(hunk)
        elif cur["hunks"]:
            continue                           # past the header block
        elif line.startswith("--- "):
            cur["old_path"] = _diff_header_path(line[4:])
        elif line.startswith("+++ "):
            cur["path"] = _diff_header_path(line[4:])
        elif line.startswith(("rename from ", "copy from ")):
            cur["old_path"] = unquote_git_path(line.split(" ", 2)[2])
        elif line.startswith(("rename to ", "copy to ")):
            cur["path"] = unquote_git_path(line.split(" ", 2)[2])
    return files


def _cap_snippet(text, limit=200):
    return (text or "").strip()[:limit] or None


def build_symbol_deltas(path, hunks):
    """Attribute a file's diff hunks to symbol-level changes (diff-local). Pure.

    -> [{"path","lang","subkind","name","change":add|drop|change,"before","after"}]
    A declaration on a `+` line ⇒ add; on a `-` line ⇒ drop; a same-key decl on both,
    or a body edit inside a symbol whose declaration is in context ⇒ change. `before`/
    `after` are bounded snippets (first removed/added line of the symbol's region)."""
    lang = symbol_lang(path)
    if lang is None:
        return []
    added, removed, touched = {}, {}, set()
    befores, afters = {}, {}

    def note(key, sign, text):
        if sign == "+":
            afters.setdefault(key, _cap_snippet(text))
        elif sign == "-":
            befores.setdefault(key, _cap_snippet(text))

    for hunk in hunks:
        current = None
        for sign, text in hunk["lines"]:
            decl = detect_symbol_decl(lang, text)
            if decl is None:
                if sign in ("+", "-") and current is not None:
                    touched.add(current)
                    note(current, sign, text)
                continue
            kind, subkind, name = decl
            key = (subkind, name)
            if sign == "+":
                added[key] = key
                note(key, "+", text)
            elif sign == "-":
                removed[key] = key
                note(key, "-", text)
            # Symbol declarations become the enclosing `current` for body edits;
            # comments are identified by text and never own a following body edit.
            if kind == "symbol":
                current = key

    deltas = []
    for key in dict.fromkeys(list(added) + list(removed) + list(touched)):
        subkind, name = key
        in_a, in_r = key in added, key in removed
        change = "change" if (in_a and in_r) or (not in_a and not in_r) else \
                 ("add" if in_a else "drop")
        deltas.append({"path": path, "lang": lang, "subkind": subkind, "name": name,
                       "change": change, "before": befores.get(key),
                       "after": afters.get(key)})
    return sorted(deltas, key=lambda d: (str(d["subkind"]), str(d["name"]), d["change"]))


# Per-file diff cap (Phase 10 slice-diffs). A bounded unified-diff snippet rides the
# stored ledger so the slice is self-contained for EVERY language (not just graphify
# ones) — deliberately small (the slice is a bounded context unit, not a patch
# archive). Whichever of chars/lines is hit first truncates; mirrors _cap_snippet's
# "keep it small" style.
FILE_DIFF_CAP = 800
FILE_DIFF_LINE_CAP = 30


def bounded_file_diff(hunks, cap=FILE_DIFF_CAP, line_cap=FILE_DIFF_LINE_CAP):
    """Render a file's parsed `hunks` as a bounded unified-diff snippet. Pure.

    Emits an `@@ +<new_start> @@` marker per hunk followed by its sign-prefixed
    lines (`+`/`-`/` `). The BODY is held to roughly `cap` chars OR `line_cap` lines
    (whichever is hit first), appending a `…[+N lines]` marker for the dropped diff
    lines (hunk markers are not counted toward `line_cap`). The result is
    *approximately* bounded, not strict: it may exceed `cap` by a small margin — it
    always keeps at least the first body line (truncated to `cap` + `…` if huge) plus
    its `@@` marker, and may add the overflow marker. Returns None when there are no
    hunks (or no lines) so the field is omit-when-empty for byte-stability."""
    if not hunks:
        return None
    # Flatten into the ordered output lines: a marker per hunk, then its body lines.
    out, total_body = [], 0
    for hunk in hunks:
        out.append(("@@", "@@ +{} @@".format(hunk.get("new_start", 0))))
        for sign, text in hunk.get("lines", []):
            total_body += 1
            out.append((sign, sign + text))
    if total_body == 0:
        return None
    rendered, used, kept_body = [], 0, 0
    for kind, line in out:
        is_body = kind != "@@"
        # Always keep the FIRST body line (truncated to `cap`) so a genuinely-changed
        # file never renders to None just because its first line is huge.
        first_body = is_body and kept_body == 0
        if first_body and len(line) > cap:
            line = line[:cap] + "…"
        projected = used + len(line) + (1 if rendered else 0)
        # Stop before exceeding either cap — checked for markers too, so many small
        # hunks can't overshoot `cap` on marker bytes alone.
        if not first_body and ((is_body and kept_body >= line_cap)
                               or (rendered and projected > cap)):
            break
        rendered.append(line)
        used = projected
        if is_body:
            kept_body += 1
    # Drop a trailing hunk marker with no body lines under it (cap hit right after it).
    while rendered and rendered[-1].startswith("@@"):
        rendered.pop()
    if not rendered:
        return None
    dropped = total_body - kept_body
    if dropped > 0:
        rendered.append("…[+{} lines]".format(dropped))
    return "\n".join(rendered)


def _iter_patch_files(raw):
    """Yield (sha, author, date, file) for every changed file in each commit chunk
    of a `git log -p` walk — a SINGLE parse of the patch text. `file` is a
    parse_unified_diff entry ({path, old_path, hunks}). Merge commits (no patch)
    yield nothing. Pure; the shared walk behind the parsers below + parse_patch_events
    (so acquire can extract both symbol events and file diffs in one pass)."""
    for chunk in (raw or "").split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, _parents, author, date, _subject = (f.strip() for f in fields[:5])
        body = "\n".join(lines[1:])
        for f in parse_unified_diff(body):
            yield sha, author, date, f


def _symbol_events_for(sha, author, date, f):
    """Symbol-level change events for one patched file. Pure."""
    path = f["path"] or f["old_path"]
    return [{
        "commit": sha, "author": author, "date": date, "path": d["path"],
        "lang": d["lang"], "subkind": d["subkind"], "name": d["name"],
        "change": d["change"], "before": d["before"], "after": d["after"],
    } for d in build_symbol_deltas(path, f["hunks"])]


def _file_diff_for(sha, f):
    """The bounded {commit, path, hunk} record for one patched file, or None. Pure."""
    path = f["path"] or f["old_path"]
    diff = bounded_file_diff(f["hunks"])
    return {"commit": sha, "path": path, "hunk": diff} if path and diff else None


def parse_file_diffs(raw):
    """Parse `git log -p` output into per-(commit, path) bounded file diffs. Pure.

    Emits ONE bounded unified-diff snippet per changed file (every language, not just
    symbol_lang ones): `[{commit, path, hunk}]`, keyed by the NEW path (rename target).
    Files whose hunks render empty are omitted; merge commits yield nothing."""
    out = []
    for sha, _author, _date, f in _iter_patch_files(raw):
        rec = _file_diff_for(sha, f)
        if rec:
            out.append(rec)
    return out


def parse_symbol_events(raw):
    """Parse `git log -p` output into symbol-level change events. Pure.

    Each event: {commit, author, date, path, lang, subkind, name, change, before, after}.
    Only tracked source files (symbol_lang) contribute; file-level lifecycle still comes
    from parse_code_events. Merge commits show no patch and yield nothing."""
    events = []
    for sha, author, date, f in _iter_patch_files(raw):
        events.extend(_symbol_events_for(sha, author, date, f))
    return events


def parse_patch_events(raw):
    """Single pass over a `git log -p` walk → (symbol_events, file_diffs). Pure.

    Same outputs as `parse_symbol_events(raw)` and `parse_file_diffs(raw)` but parses
    the (potentially large) patch text ONCE — acquire uses this so the walk isn't
    re-parsed per consumer."""
    sym, diffs = [], []
    for sha, author, date, f in _iter_patch_files(raw):
        sym.extend(_symbol_events_for(sha, author, date, f))
        rec = _file_diff_for(sha, f)
        if rec:
            diffs.append(rec)
    return sym, diffs


def attach_file_diffs(code_events, file_diffs):
    """Attach each bounded file diff to its file-level code_event (in place).

    Joins on (commit, path): the patch walk keys by the NEW path (rename target);
    falls back to `old_path` for the rare case the diff names only the source.
    Both walks yield unquoted paths, so non-ASCII/space/quoted names join.
    Omit-when-empty: events without a diff get no `hunk` key."""
    diff_by_key = {(d["commit"], d["path"]): d["hunk"] for d in file_diffs}
    if not diff_by_key:
        return code_events
    for ev in code_events:
        hunk = diff_by_key.get((ev.get("commit"), ev.get("path")))
        if hunk is None and ev.get("old_path"):
            hunk = diff_by_key.get((ev.get("commit"), ev.get("old_path")))
        if hunk:
            ev["hunk"] = hunk
    return code_events


# Ordered code-area patterns for the directory provider (primary, zero-dep).
# Each entry is (name, predicate(parts) -> area_id_or_None) tried in order; the
# first match wins. `parts` is path.split("/"). Patterns are directory-first and
# match how IaC repos define a module. The generic fallback is last.
_AVM_KINDS = ("res", "ptn", "utl")


def _avm_area(parts):
    # avm/{res,ptn,utl}/<group>/<module>/...  -> the 4-segment module subtree. The
    # module segment must be a DIRECTORY (a file under it follows, >= 5 parts): a
    # file directly under the group (avm/res/network/README.md) is not a module and
    # belongs to the group-level area avm/res/<group>.
    if len(parts) >= 4 and parts[0] == "avm" and parts[1] in _AVM_KINDS:
        return "/".join(parts[:4] if len(parts) >= 5 else parts[:3])
    return None


def _is_avm_group_area(area_id):
    """True for an AVM group-level area id `avm/{res,ptn,utl}/<group>` (the home of
    files directly under a group, e.g. its README) — never a module. Pure."""
    parts = (area_id or "").split("/")
    return len(parts) == 3 and parts[0] == "avm" and parts[1] in _AVM_KINDS


def _main_bicep_dir(parts):
    # any directory containing a main.bicep -> that directory.
    if parts and parts[-1] == "main.bicep":
        return "/".join(parts[:-1]) or parts[0]
    return None


def _terraform_modules_dir(parts):
    # modules/<name>/... -> modules/<name>. <name> must be a directory: a file
    # directly under modules/ (modules/README.md) is not a module and falls through.
    if len(parts) >= 3 and parts[0] == "modules":
        return "/".join(parts[:2])
    return None


def _tf_dir(parts):
    # Any directory containing a *.tf file -> that directory. Repo-root .tf files
    # (no directory component) collapse to a SINGLE "main.tf" root-module area, so a
    # Terraform root module is one area (like a Bicep main.bicep dir) instead of
    # fragmenting into one area per root file (main.tf/variables.tf/outputs.tf/...).
    if parts and parts[-1].endswith(".tf"):
        return "/".join(parts[:-1]) or "main.tf"
    return None


def _topn_dir(parts, n=2):
    # generic fallback: the first N segments of the file's DIRECTORY, so a shallow
    # file (docs/README.md) lands in its directory (docs), never an area named after
    # the file. A repo-root file has no directory to name and keeps its own name.
    if not parts:
        return None
    return "/".join(parts[:-1][:n]) or parts[-1]


DEFAULT_AREA_PATTERNS = [
    ("avm", _avm_area),
    ("main_bicep", _main_bicep_dir),
    ("terraform_modules", _terraform_modules_dir),
    ("tf_dir", _tf_dir),
    ("topn", _topn_dir),
]


def classify_code_area(path, patterns):
    """Map a tracked file path to a single area id (a directory path), or None.

    Tries the ordered `patterns` (AVM module subtree, any main.bicep dir, Terraform
    modules/<name>, any *.tf dir, else the top 2 segments of the file's directory;
    a repo-root file keeps its own name). Pure."""
    if not path:
        return None
    parts = path.split("/")
    for _name, fn in patterns:
        area = fn(parts)
        if area:
            return area
    return None


def _area_label(area_id):
    """A short, human tail for an area id (the last path segment)."""
    return (area_id or "").rstrip("/").split("/")[-1] or area_id


def _is_scaffold_area(area_id):
    """True for a Terraform `examples/` or `tests/` subtree. These are the module's
    unit-test / documentation scaffolding: their `module` blocks reference the
    module-under-test and test fixtures (often via CI-manipulated relative paths, or
    other registry modules used only to stand up a test), NOT the published module's
    own dependencies. Such areas are skipped at edge extraction and their depends_on
    edges are excluded at fold, so they are never built or misattributed as the
    module's real (incl. cross-repo) dependencies. Pure."""
    return bool({"examples", "tests"} & set((area_id or "").split("/")))


def _is_scaffold_path(path):
    """True for a FILE inside an `examples/` or `tests/` subtree at any depth.

    A nested scaffold file (e.g. `modules/sub/examples/x/main.tf`) classifies to its
    module's area (`modules/sub`), so the area-level `_is_scaffold_area` check does
    not catch it; entrypoint selection and the structural scan use this so such a
    file is never read or built as the module itself. Pure."""
    return _is_scaffold_area(os.path.dirname(path or ""))


def build_directory_areas(paths, patterns):
    """Fold a list of tracked file paths into the `code_graph` directory provider.

    Shape: {"provider":"directory","areas":[{"id","label","paths":[...],"edges":[]}]}.
    Area id is the directory path; label is its tail; edges are deferred (empty).
    Deterministic (areas sorted by id, paths sorted). Pure."""
    grouped = {}
    for p in paths:
        area = classify_code_area(p, patterns)
        if area is None:
            continue
        grouped.setdefault(area, set()).add(p)
    areas = [
        {"id": area, "label": _area_label(area),
         "paths": sorted(grouped[area]), "edges": []}
        for area in sorted(grouped)
    ]
    return {"provider": "directory", "areas": areas}


def parse_graphify_graph(graph_json):
    """Group graphify nodes by their integer `community` into the code_graph shape.

    Reads graphify's REAL output: top keys `nodes`/`links` (edges live under
    `links`, NOT `edges`); each node carries `community` (int) + `source_file`;
    there is NO top-level `communities` list. Produces
    {"provider":"graphify","areas":[{"id":"community:<n>","label",
    "paths":[distinct source_files],"edges":[]}]}. Area edges are deferred (empty);
    `links` are graphify's symbol-level edges, not resolved area edges. Pure."""
    by_comm = {}
    for node in (graph_json or {}).get("nodes", []):
        comm = node.get("community")
        if comm is None:
            continue
        src = node.get("source_file")
        if src:
            by_comm.setdefault(int(comm), set()).add(src)
    areas = []
    for comm in sorted(by_comm):
        paths = sorted(by_comm[comm])
        # representative label: the shortest common-ish dir — use the first path's
        # directory (deterministic given sorted paths), or the path itself.
        head = paths[0]
        label = head.rsplit("/", 1)[0] if "/" in head else head
        areas.append({"id": f"community:{comm}", "label": label,
                      "paths": paths, "edges": []})
    return {"provider": "graphify", "areas": areas}


def _read_json_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def select_code_area_provider(paths, clone_dir, which=shutil.which,
                              run=None, read_json=_read_json_file,
                              patterns=None):
    """Pick the code-area provider, directory-first.

    graphify is OPTIONAL: prefer it only if it is on PATH AND `graphify update
    <clone>` yields a `graphify-out/graph.json` with nodes; any absence/failure/
    nodeless-graph falls back to the directory provider (never fails fast). The
    `which`/`run`/`read_json` seams make this offline-testable without graphify.
    Returns the `code_graph` provider dict."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    if run is None:
        run = run_git
    directory = build_directory_areas(paths, patterns)
    if not which("graphify"):
        return directory
    try:
        run(["graphify", "update", clone_dir])
        graph = read_json(os.path.join(clone_dir, "graphify-out", "graph.json"))
    except Exception:
        return directory
    if not (graph or {}).get("nodes"):
        return directory
    graphified = parse_graphify_graph(graph)
    return graphified if graphified["areas"] else directory


# Phase 3c: IaC dependency-edge extraction (build-only). The edge object is
# {to, kind, ref, version, transitive, provider, resolved}; see BUNDLE.md.

_BICEP_MODULE_RE = re.compile(
    r"module\s+\w+\s+'(?P<ref>[^']+)'(?:\s*=\s*(?P<open>[\[{]))?")


# A non-local Bicep reference scheme: `br:` / `ts:` or an alias `br/<a>:` / `ts/<a>:`.
_BICEP_SCHEME_RE = re.compile(r"^(?P<scheme>br|ts)(?:/(?P<alias>[^:/]+))?:(?P<body>.*)$")
# The public Bicep registry (what the built-in `br/public` alias points at).
_BICEP_PUBLIC_REGISTRY = "mcr.microsoft.com/bicep/"


def _split_ref_tag(body):
    """`<path>:<tag>` / `<path>@<digest>` -> (path, tag-or-None)."""
    if "@" in body:
        return tuple(body.split("@", 1))
    if ":" in body:
        return tuple(body.rsplit(":", 1))
    return body, None


def parse_bicep_module_refs(source_text):
    """Extract `module <sym> '<ref>'` references from a .bicep source.

    Each result is {ref, registry_path, version, local_path, instances}. Only a
    PUBLIC Bicep registry ref (`br/public:<path>:<ver>`, or the same registry named
    explicitly, `br:mcr.microsoft.com/bicep/<path>:<ver>`) sets `registry_path` (the
    module path in the public registry). A private registry (`br:<other-host>/...`),
    a template spec (`ts:<sub>/<rg>/<name>:<ver>`, `ts/<alias>:<name>:<ver>`) or a
    user alias (`br/<alias>:...`) sets neither `registry_path` nor `local_path`:
    its path says nothing about this repo, so only the raw `ref` (+ `version`) is
    kept. A ref with no scheme is a `local_path`. `instances` is `"many"` when the
    module is instantiated as an array (`= [for ...]`), `"one"` for a single
    `= {`, else None. Pure."""
    refs = []
    for m in _BICEP_MODULE_RE.finditer(source_text or ""):
        ref = m.group("ref")
        open_ch = m.group("open")
        instances = "many" if open_ch == "[" else "one" if open_ch == "{" else None
        registry_path = version = local_path = None
        sm = _BICEP_SCHEME_RE.match(ref)
        if sm:
            path_part, version = _split_ref_tag(sm.group("body"))
            alias = sm.group("alias")
            if sm.group("scheme") == "br" and alias == "public":
                registry_path = path_part
            elif (sm.group("scheme") == "br" and alias is None
                  and path_part.lower().startswith(_BICEP_PUBLIC_REGISTRY)):
                registry_path = path_part[len(_BICEP_PUBLIC_REGISTRY):]
            # else: private registry / template spec / user alias -> raw ref only
        else:
            local_path = ref
        refs.append({"ref": ref, "registry_path": registry_path,
                     "version": version, "local_path": local_path,
                     "instances": instances})
    return refs


def resolve_module_ref(ref_info, base_path, patterns=None, area_ids=None,
                       module_exists=None):
    """Map a parsed bicep/tf ref to a canonical area-id (reusing classify_code_area).

    A public-registry ref (`registry_path`) classifies its `<path>/main.bicep` and
    maps to that area ONLY when it is a module (not an AVM group-level area) of
    this repo: in `area_ids` (the areas changed in the window) or, for an untouched
    module, `module_exists(area)` says its entrypoint is in the clone; otherwise None — never a
    phantom id for a module that is not in this repo (`area_ids` None => no set
    => None). Local refs are normalised relative to `base_path`'s directory then
    classified (the raw dir when classification declines). None when nothing
    resolvable is set (private registry / template spec / alias refs). Pure."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    rp = ref_info.get("registry_path")
    if rp:
        area = classify_code_area(rp.strip("/") + "/main.bicep", patterns)
        if not area or _is_avm_group_area(area):
            return None
        if area in (area_ids or ()):
            return area
        # `area_ids` is built from the paths CHANGED in the window, so a module of
        # this repo that nobody touched is not in it. It is still this repo's
        # module when its entrypoint is in the clone (`module_exists`).
        if module_exists is not None and module_exists(area):
            return area
        return None
    lp = ref_info.get("local_path")
    if lp:
        base_dir = os.path.dirname(base_path or "")
        joined = os.path.normpath(os.path.join(base_dir, lp))
        return classify_code_area(joined, patterns) or os.path.dirname(joined) or joined
    return None


def _arm_resources(template):
    """ARM `resources` may be a list or (symbolic-name) dict; normalise to a list."""
    res = (template or {}).get("resources", [])
    return list(res.values()) if isinstance(res, dict) else (res or [])


def walk_arm_deployments(arm_json):
    """Yield every Microsoft.Resources/deployments node in the full transitive tree.

    Each node is {name, depth, metadata_name, depends_on}. `depth` is 1 for a
    top-level module instantiation and increments per nesting level. `metadata_name`
    is the inner template's `metadata.name` (a short module name, best-effort
    identity hint). Pure."""
    nodes = []

    def visit(template, depth):
        for res in _arm_resources(template):
            if not isinstance(res, dict):
                continue
            if res.get("type") == "Microsoft.Resources/deployments":
                inner = (res.get("properties") or {}).get("template") or {}
                nodes.append({
                    "name": res.get("name"),
                    "depth": depth + 1,
                    "metadata_name": (inner.get("metadata") or {}).get("name"),
                    "depends_on": res.get("dependsOn", []),
                })
                visit(inner, depth + 1)

    visit(arm_json or {}, 0)
    return nodes


def _normalize_id(text):
    """Lowercase + strip separators, for lenient identity matching."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _match_repo_area(name, area_ids):
    """Find the area-id in `area_ids` whose tail confidently matches `name`.

    Lenient normalised comparison (the ARM `metadata.name` is a short module name
    like `vault`; the area tail is `vault`/`key-vault`). DETERMINISTIC: an exact
    normalised tail match always wins; otherwise the most specific (longest tail)
    substring match wins, ties broken by area-id — so the result never depends on
    `set` iteration order. Returns the area-id or None."""
    target = _normalize_id(name)
    if not target:
        return None
    exact, partial = [], []
    for aid in area_ids:
        tail = aid.rstrip("/").split("/")[-1]
        nt = _normalize_id(tail)
        if not nt:
            continue
        if nt == target:
            exact.append(aid)
        elif nt in target or target in nt:
            partial.append((nt, aid))
    if exact:
        return min(exact)                                   # exact wins; stable
    if partial:
        return min(partial, key=lambda p: (-len(p[0]), p[1]))[1]  # longest tail, stable
    return None


def build_bicep_edges(source_text, arm_json, base_path, area_ids, patterns=None,
                      module_exists=None):
    """Build inter-area dependency edges for one Bicep area.

    Immediate edges (transitive=False) come from the SOURCE refs — fully identified
    (area-id + version), build-validated by the caller. They correspond to the
    DEPTH-1 module instantiations in the ARM tree. Transitive edges (transitive=True)
    come from deployments nested DEEPER (depth >= 2), and only when the nested
    deployment's `metadata.name` confidently matches ANOTHER area in this repo (no
    fabricated external ids) that is not already a direct dependency. Pure;
    deterministic (deduped, then sorted)."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    edges = []
    seen = set()
    self_id = classify_code_area(base_path, patterns)

    for ri in parse_bicep_module_refs(source_text):
        to = resolve_module_ref(ri, base_path, patterns, area_ids, module_exists)
        local = False
        # A local relative ref that resolves back to THIS area is a private child
        # submodule (e.g. `nat-rule/main.bicep`), not a self-dependency. Name the
        # child node (`<area>/<child>`) so the graph shows which private submodule
        # is used; a ref to the area's own main.bicep is genuine recursion and is
        # left as a self edge.
        if ri.get("local_path") and to is not None and to == self_id:
            joined = os.path.normpath(
                os.path.join(os.path.dirname(base_path or ""), ri["local_path"]))
            child_dir = os.path.dirname(joined)
            if child_dir != self_id and child_dir.startswith(str(self_id) + "/"):
                to = child_dir
                local = True
        key = (to, ri["ref"], False)
        if key in seen:
            continue
        seen.add(key)
        edge = {"to": to, "kind": "module", "ref": ri["ref"],
                "version": ri["version"], "transitive": False,
                "provider": "bicep", "resolved": to is not None}
        if local:
            edge["local"] = True
            if ri.get("instances"):
                edge["instances"] = ri["instances"]
        edges.append(edge)

    immediate_tos = {e["to"] for e in edges}  # direct deps, already captured above
    # Only module areas are transitive targets: an AVM group-level area (a group's
    # README home) would otherwise substring-match nested names like `network`.
    module_area_ids = [a for a in (area_ids or ()) if not _is_avm_group_area(a)]
    for node in walk_arm_deployments(arm_json):
        # depth 1 == the direct module instantiations (already emitted as immediate
        # source refs); only genuinely-nested deployments are transitive.
        if node["depth"] < 2:
            continue
        to = _match_repo_area(node.get("metadata_name"), module_area_ids)
        if to is None or to == self_id or to in immediate_tos:
            continue
        key = (to, node.get("name"), True)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"to": to, "kind": "module", "ref": node.get("name"),
                      "version": None, "transitive": True,
                      "provider": "bicep", "resolved": True})

    return sorted(edges, key=lambda e: (e["transitive"], str(e["to"]), str(e["ref"])))


# Module-block parsing runs on an HCL "mask" of the text (see _hcl_mask): comments,
# string contents and heredocs are blanked to spaces at the SAME offsets, so these
# patterns only ever see real HCL structure and values are sliced from the
# original text. A module head must start its line (after optional whitespace);
# its label may be quoted or a bare identifier.
_TF_MODULE_HEAD_RE = re.compile(
    r'(?m)^[ \t]*module[ \t]+(?:"(?P<name>[^"\n]+)"|(?P<ident>[A-Za-z_][\w-]*))[ \t]*\{')
# A top-level `source`/`version` ARGUMENT: anchored at line start (so
# `kubernetes_version`/`custom_source` never match), `=` but not `==`, and a
# plain quoted string value. Depth-0 (not inside a nested block/map/list) is
# checked separately by _tf_top_level_attr.
_TF_ATTR_RE = {
    key: re.compile(r'(?m)^[ \t]*%s[ \t]*=(?!=)[ \t]*"(?P<val>[^"\n]*)"' % key)
    for key in ("source", "version")}
_TF_HEREDOC_RE = re.compile(r'<<-?(?P<id>[A-Za-z_][\w-]*)[ \t]*\r?\n')
_HCL_OPEN, _HCL_CLOSE = "{[(", "}])"


def _hcl_mask(text):
    """Return `text` with HCL comments, string-literal contents and heredoc
    bodies replaced by spaces (newlines kept), so offsets are unchanged.

    Handles `#` / `//` line comments, `/* */` block comments, double-quoted
    strings with `\\` escapes, `${...}` / `%{...}` template interpolation (with
    nested braces and nested strings) and `$${` / `%%{` literal escapes, and
    `<<ID` / `<<-ID` heredocs closed by a line holding only `ID`. The quote
    characters of a top-level string are kept so a value's span can be located.
    An unterminated quoted string ends at the newline (HCL forbids newlines in
    quoted strings outside interpolation); an unterminated block comment or
    heredoc runs to the end. Internal helper. Pure."""
    out = list(text)
    n = len(text)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    stack = []          # "str" | [interp_brace_depth]; empty = code context
    i = 0
    while i < n:
        c = text[i]
        if stack and stack[-1] == "str":
            if c == "\\":
                blank(i, min(i + 2, n))
                i += 2
            elif c in "$%" and text.startswith(c + c + "{", i):   # literal $${
                blank(i, i + 3)
                i += 3
            elif c in "$%" and text.startswith(c + "{", i):
                blank(i, i + 2)
                stack.append([0])
                i += 2
            elif c == '"' or (c == "\n" and len(stack) == 1):
                stack.pop()
                if stack and c == '"':
                    blank(i, i + 1)                     # nested string's quote
                i += 1
            else:
                blank(i, i + 1)
                i += 1
            continue
        if stack:                                       # inside an interpolation
            top = stack[-1]
            # Comments are legal inside an interpolation expression; a quote or
            # brace inside one is not syntax.
            if c == "#" or text.startswith("//", i):
                j = text.find("\n", i)
                j = n if j < 0 else j
                blank(i, j)
                i = j
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j)
                i = j
                continue
            if c == '"':
                stack.append("str")
            elif c == "{":
                top[0] += 1
            elif c == "}":
                if top[0]:
                    top[0] -= 1
                else:
                    stack.pop()
            blank(i, i + 1)
            i += 1
            continue
        # code context
        if c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == '"':
            stack.append("str")
            i += 1
        elif c == "<" and text.startswith("<<", i):
            m = _TF_HEREDOC_RE.match(text, i)
            if not m:
                i += 1
                continue
            end = re.compile(r'(?m)^[ \t]*%s[ \t]*\r?$' % re.escape(m.group("id")))
            em = end.search(text, m.end())
            j = n if em is None else em.end()
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def _hcl_depths(masked):
    """Bracket depth ({, [, ( combined) BEFORE each offset of a masked text, as a
    list of len(masked)+1. Internal helper. Pure."""
    depths, d = [0], 0
    for ch in masked:
        if ch in _HCL_OPEN:
            d += 1
        elif ch in _HCL_CLOSE:
            d = max(d - 1, 0)
        depths.append(d)
    return depths


def _tf_top_level_attr(body, key):
    """Value of the module body's own top-level `key = "..."` argument, or None.

    Ignores the key inside comments, strings, heredocs and nested blocks/maps/
    lists (e.g. `providers = {...}`, an object input with a `version` field) and
    longer names ending in the key (`kubernetes_version`). First match wins
    (HCL rejects a duplicate argument). Internal helper. Pure."""
    body = body or ""
    masked = _hcl_mask(body)
    depths = None
    for m in _TF_ATTR_RE[key].finditer(masked):
        if depths is None:
            depths = _hcl_depths(masked)
        if depths[m.start()] == 0:
            return body[m.start("val"):m.end("val")]
    return None
# Terraform graph DOT: every quoted string is a candidate node address. The
# `[root] ` node prefix is OPTIONAL: older `terraform graph` emitted
# `"[root] module.x..."`, modern terraform (>= 1.7) emits unprefixed `"module.x..."`.
# A node's module CALL chain is its leading `module.<name>` segments (an instance
# key `[0]` / `["k"]` may follow each name): `module.a.module.b.res.x` -> (a, b).
_TF_DOT_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_TF_MODULE_STEP_RE = re.compile(r'module\.([A-Za-z0-9_-]+)(?:\[[^\]]*\])?(?:\.|(?=\s)|$)')


def _terraform_module_bodies(tf_text):
    """Map each module name to its raw block body via brace-balancing.

    Non-greedy regexes break on nested blocks (`tags { ... }`, `dynamic`), so the
    body is scanned from the opening `{` to its matching close, counting depth.
    Scanning runs on the HCL mask (_hcl_mask), so a `module "x" {` inside a
    comment, string or heredoc is not a block and braces in those never count;
    only modules at file top level (depth 0) are returned. The body is the
    ORIGINAL text. An unclosed block runs to end of text. Internal helper. Pure."""
    text = tf_text or ""
    masked = _hcl_mask(text)
    depths = _hcl_depths(masked)
    out = {}
    for m in _TF_MODULE_HEAD_RE.finditer(masked):
        brace = m.end() - 1                         # offset of the opening `{`
        if depths[brace] != 0:
            continue
        g = "name" if m.group("name") is not None else "ident"
        name = text[m.start(g):m.end(g)]
        i = m.end()
        while i < len(masked) and depths[i + 1] > 0:
            i += 1
        out[name] = text[m.end():i]
    return out


def parse_terraform_module_blocks(tf_text):
    """Map each `module "<name>" { source = "..." }` to its source string. Pure.

    Brace-balanced so a `source` after a nested block is still found; only the
    block's own top-level `source` argument counts (_tf_top_level_attr)."""
    out = {}
    for name, body in _terraform_module_bodies(tf_text).items():
        src = _tf_top_level_attr(body, "source")
        if src:
            out[name] = src
    return out


def _tf_module_chain(node):
    """The module call chain a DOT node address lives in: `module.a.module.b.res`
    -> ("a", "b"); a root-module node -> (). Instance keys are dropped. Pure."""
    node = node.replace('\\"', '"')
    if node.startswith("[root]"):
        node = node[len("[root]"):].lstrip()
    chain, pos = [], 0
    while True:
        m = _TF_MODULE_STEP_RE.match(node, pos)
        if not m:
            return tuple(chain)
        chain.append(m.group(1))
        pos = m.end()


def parse_terraform_module_chains(dot_text):
    """Every module call chain the terraform graph DOT shows, with all its
    prefixes: a node in `module.a.module.b` yields ("a",) and ("a", "b"). Read
    from every quoted node address (edges and node declarations), so it works on
    the legacy `[root] `-prefixed and the modern resource-only layout alike. The
    DOT says WHICH module calls exist below the root, not who depends on whom.
    Deterministic (sorted, deduped). Pure."""
    chains = set()
    for m in _TF_DOT_STRING_RE.finditer(dot_text or ""):
        chain = _tf_module_chain(m.group(1))
        for i in range(1, len(chain) + 1):
            chains.add(chain[:i])
    return sorted(chains)


def parse_terraform_modules_manifest(text):
    """Parse terraform's own record of the installed module tree,
    `.terraform/modules/modules.json` (written by `terraform init`), into
    {call chain: {"source", "version", "dir"}}; the root entry (Key "") is
    dropped. Malformed input or entries are ignored ({} / skipped). Pure."""
    try:
        doc = json.loads(text or "")
    except ValueError:
        return {}
    mods = doc.get("Modules") if isinstance(doc, dict) else None
    out = {}
    for e in mods if isinstance(mods, list) else ():
        if not isinstance(e, dict) or not isinstance(e.get("Key"), str) or not e["Key"]:
            continue
        out[tuple(e["Key"].split("."))] = {
            "source": e.get("Source") if isinstance(e.get("Source"), str) else None,
            "version": e.get("Version") if isinstance(e.get("Version"), str) else None,
            "dir": e.get("Dir") if isinstance(e.get("Dir"), str) else None}
    return out


def _resolve_tf_module_source(name, sources, bodies, base_dir, patterns):
    """Resolve one terraform `module "<name>"` block to (to_area|None, ref|None,
    version). A LOCAL source (`./` or `/`) maps to a repo area id (via
    classify_code_area, relative to `base_dir`) -> `to`=area; a REGISTRY/external
    source can't map to a repo area so `to`=None with the source kept in `ref`
    (+ pinned `version`); `ref`=None means a module block with no source. Pure.
    Shared by the DOT-driven build_terraform_edges and the static whole-tree scan."""
    src = sources.get(name)
    if not src:
        return None, None, None
    if src.startswith(".") or src.startswith("/"):
        return _tf_local_area(base_dir, src, patterns), src, None
    version = _tf_top_level_attr(bodies.get(name, ""), "version") or None
    return None, src, version                          # external: to=None, ref=src


def _tf_local_area(base_dir, path, patterns):
    """The repo area id of a local module dir `path` (relative to `base_dir`), or
    None when it escapes the repo. Pure."""
    joined = os.path.normpath(os.path.join(base_dir, path))
    if joined == os.pardir or joined.startswith(os.pardir + os.sep):
        # escapes the repo (e.g. ../../.. above root): unresolvable to a
        # repo area -> external (to=None), never a phantom dangling area.
        return None
    # normpath('.') is the repo root; classify it the same way root files
    # are (-> "main.tf"), so a local source pointing at the root module
    # resolves to the SAME area id as the root module's own files.
    rel = "" if joined == os.curdir else joined
    return classify_code_area((rel + "/main.tf").lstrip("/"), patterns) \
        or rel or "main.tf"


def _tf_direct_edges(tf_text, base_dir, patterns):
    """The DIRECT edges of one Terraform module: one per top-level `module {}`
    block with a `source` in `tf_text` (the module dir's whole *.tf set), consumed
    or not, all `transitive: false`; deduped on (to, ref), sorted. Shared by the
    build path (build_terraform_edges) and the static whole-tree scan, so both
    report the same direct set. Pure."""
    bodies = _terraform_module_bodies(tf_text)
    sources = parse_terraform_module_blocks(tf_text)
    edges, seen = [], set()
    for name in sources:                           # only blocks WITH a source
        to, ref, version = _resolve_tf_module_source(name, sources, bodies,
                                                      base_dir, patterns)
        if ref is None or (to, ref) in seen:
            continue
        seen.add((to, ref))
        edges.append({"to": to, "kind": "module", "ref": ref, "version": version,
                      "transitive": False, "provider": "terraform",
                      "resolved": to is not None})
    return sorted(edges, key=lambda e: (str(e["to"]), str(e["ref"])))


def _resolve_tf_nested(chain, manifest, base_dir, patterns):
    """Resolve a NESTED module call chain via the init module manifest
    (parse_terraform_modules_manifest) to (to|None, ref|None, version).

    A module installed in the working tree (its `Dir` outside `.terraform/`) is a
    repo dir -> its area, with the recorded `Source` as `ref`. One installed under
    `.terraform/modules/` belongs to a downloaded package: it is attributed to the
    nearest call in its chain (itself first) with a remote source, e.g. a
    registry module's internal `./modules/x` is that registry module. Unknown
    (no manifest entry, no remote ancestor) -> (None, None, None). Pure."""
    entry = manifest.get(chain)
    d = os.path.normpath(entry["dir"]) if entry and entry["dir"] else None
    if d is None:
        return None, None, None
    if not (os.path.isabs(d) or d == ".terraform"
            or d.startswith(".terraform" + os.sep)):
        return _tf_local_area(base_dir, d, patterns), entry["source"], None
    for i in range(len(chain), 0, -1):
        anc = manifest.get(chain[:i]) or {}
        src = anc.get("source")
        if src and not src.startswith((".", "/")):
            return None, src, anc.get("version")
    return None, None, None


def _tf_target_identity(to, ref):
    """What a module edge points at, for 'direct wins' dedup: a repo area, else the
    registry module (manifest.registry_identity: host-defaulted, case-folded,
    `//submodule` dropped — exactly what a multi-repo fold resolves to one member),
    else the raw ref. None when unknown. Pure."""
    if to is not None:
        return ("area", to)
    if ref is None:
        return None
    reg = manifest_mod.registry_identity(ref)
    return ("registry",) + reg if reg else ("ref", ref)


def scan_structural_terraform_areas(clone_dir, patterns=None, list_files=None,
                                    read_text=None):
    """Statically discover EVERY terraform module area in the clone tree and its
    `module {source=}` dependency edges, WITHOUT terraform init (source-parse only).

    The directory provider only sees in-window-CHANGED paths, so the DOT-driven
    extract_iac_edges resolves edges only for module areas touched in the window —
    a blast-radius graph that shrinks/grows with churn. This scan reads the whole
    tracked tree so the module-dependency graph reflects the repo's actual module
    structure regardless of in-window activity. Every statically-parsed block is a
    DIRECT edge (no DOT transitivity), shaped exactly like build_terraform_edges'
    output. Scaffold areas (examples/tests) are skipped. Returns
    {area_id: {"paths": [...], "edges": [...]}}. `list_files`/`read_text` are
    injectable for offline tests; defaults list tracked *.tf via git + read disk."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    if list_files is None:
        def list_files():
            return git_ls_files(clone_dir)
    if read_text is None:
        def read_text(rel):
            return _read_text_file(os.path.join(clone_dir, rel))

    by_area = {}
    for rel in list_files():
        if not rel.endswith(".tf"):                # only terraform sources
            continue
        aid = classify_code_area(rel, patterns)
        # skip scaffold areas AND scaffold files nested inside a module area
        # (modules/<x>/examples/...): they are tests of the module, not the module.
        if aid is None or _is_scaffold_area(aid) or _is_scaffold_path(rel):
            continue
        by_area.setdefault(aid, []).append(rel)

    out = {}
    for aid, paths in by_area.items():
        paths = sorted(set(paths))
        # The module is the area dir's own *.tf set (terraform reads one dir as one
        # module); a nested dir is only used when the area dir itself has no *.tf.
        mod_dir = _tf_module_dir(aid, paths)
        own = [p for p in paths if os.path.dirname(p) == mod_dir]
        text = "\n".join(read_text(p) for p in own)
        out[aid] = {"paths": paths, "edges": _tf_direct_edges(text, mod_dir, patterns)}
    return out


def merge_structural_areas(code_graph, structural):
    """Union the static structural terraform areas/edges into `code_graph`
    (multi-repo blast-radius augmentation). An in-window area keeps its DOT-derived
    edges and gains any structural edge not already present (dedup by (to, ref));
    module areas the in-window walk never saw are appended (deterministic by id)
    with their .tf paths + edges. Mutates and returns code_graph."""
    areas = code_graph.setdefault("areas", [])
    by_id = {a["id"]: a for a in areas}
    for aid, info in sorted(structural.items()):
        if aid in by_id:
            a = by_id[aid]
            edges = a.setdefault("edges", [])
            have = {(e.get("to"), e.get("ref")) for e in edges}
            for e in info["edges"]:
                if (e.get("to"), e.get("ref")) not in have:
                    edges.append(e)
                    have.add((e.get("to"), e.get("ref")))
        else:
            areas.append({"id": aid, "label": _area_label(aid),
                          "paths": info["paths"], "edges": info["edges"]})
    return code_graph


def build_terraform_edges(tf_text, dot_text, base_path, area_ids, patterns=None,
                          modules_json=None):
    """Build inter-area dependency edges for one Terraform area (#80).

    DIRECT edges are the area's own `module {}` blocks (`tf_text` = the module
    dir's whole *.tf set): every block with a `source` is `transitive`=False,
    whether or not anything consumes it (_tf_direct_edges, the same rule as the
    static whole-tree scan). A LOCAL source resolves to a repo area-id (via
    classify_code_area, relative to base_path) → `to`=area, `resolved`=True. A
    REGISTRY/external source cannot map to a repo area, so — like the Bicep path
    and the schema (`to`: area-id|null) — `to`=None, `resolved`=False, with the
    source kept in `ref` (+ pinned `version`).

    The terraform graph DOT is used ONLY for NESTED module calls
    (`module.a.module.b`, parse_terraform_module_chains): each is a
    `transitive`=True edge carrying its `address` and the parent call it is
    reached `via`. Its target comes from `modules_json` (the
    `.terraform/modules/modules.json` the same `terraform init` wrote;
    _resolve_tf_nested); without it the target is unknown (`to`/`ref` None) but
    the edge is still recorded. Direct wins: a nested call whose target is
    already a direct edge (or an earlier nested one) is not repeated
    (_tf_target_identity). Pure; deterministic."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    base_dir = os.path.dirname(base_path or "")
    edges = _tf_direct_edges(tf_text, base_dir, patterns)
    seen = {_tf_target_identity(e["to"], e["ref"]) for e in edges}
    manifest = parse_terraform_modules_manifest(modules_json) if modules_json else {}
    nested = []
    for chain in parse_terraform_module_chains(dot_text):
        if len(chain) < 2:                         # a root call: direct, from source
            continue
        to, ref, version = _resolve_tf_nested(chain, manifest, base_dir, patterns)
        address = ".".join("module." + n for n in chain)
        key = _tf_target_identity(to, ref) or ("address", address)
        if key in seen:
            continue
        seen.add(key)
        nested.append({"to": to, "kind": "module", "ref": ref, "version": version,
                       "transitive": True, "provider": "terraform",
                       "resolved": to is not None, "address": address,
                       "via": address.rsplit(".module.", 1)[0]})
    return sorted(edges + nested,
                  key=lambda e: (str(e["to"]), str(e["ref"]), e.get("address", "")))


def _read_text_file(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# The area ids the directory provider gives to REPO-ROOT IaC files: `_main_bicep_dir`
# -> "main.bicep", `_tf_dir` -> "main.tf". Each names the root module, whose
# directory is the repo root (""). Each kind only honours its own sentinel.
_BICEP_ROOT_ID = "main.bicep"
_TF_ROOT_ID = "main.tf"


def _area_rel_dir(area_id, root_id):
    """The clone-relative directory an area id names ("" for the repo root)."""
    return "" if area_id == root_id else (area_id or "")


def _rel_join(rel_dir, name):
    return f"{rel_dir}/{name}" if rel_dir else name


def _shallowest(paths):
    """Deterministic shallowest path (fewest segments, then lexical)."""
    return min(paths, key=lambda p: (p.count("/"), p), default=None)


def _pick_main_tf(files):
    """`main.tf` from one directory's *.tf list when present, else the first."""
    files = sorted(files)
    return next((f for f in files if os.path.basename(f) == "main.tf"),
                files[0] if files else None)


def _tf_module_dir(area_id, tf_paths):
    """The directory whose *.tf set is this area's module, from a known file list:
    the area dir itself when it holds any of `tf_paths`, else the shallowest dir
    among them (an area id that is not a module dir, e.g. a graphify community)."""
    own = _area_rel_dir(area_id, _TF_ROOT_ID)
    if any(os.path.dirname(p) == own for p in tf_paths):
        return own
    shallow = _shallowest(tf_paths)
    return os.path.dirname(shallow) if shallow is not None else None


def _area_dir_on_disk(area_id, clone_dir, root_id):
    """The area's directory when it exists in the clone (root sentinel -> ""), else
    None (the area id is not a directory: deleted, or a non-path id)."""
    if area_id == root_id:
        return ""
    rel = _area_rel_dir(area_id, root_id)
    if rel and os.path.isdir(os.path.join(clone_dir, rel)):
        return rel
    return None


def _has_clone(clone_dir):
    return bool(clone_dir) and os.path.isdir(clone_dir)


def _clone_module_exists(clone_dir):
    """Predicate `area_id -> bool`: the clone holds `<area_id>/main.bicep`. Lets a
    public-registry ref resolve to a module of this repo that was not changed in
    the window. An id that escapes the clone (absolute, or `..`) is never a module.
    None when there is no clone."""
    if not clone_dir or not os.path.isdir(clone_dir):
        return None

    def exists(area_id):
        rel = os.path.normpath(area_id or "")
        if not area_id or os.path.isabs(rel) or rel.split(os.sep)[0] == "..":
            return False
        return os.path.isfile(os.path.join(clone_dir, rel, "main.bicep"))
    return exists


def _bicep_entrypoint(area, clone_dir=None):
    """The `main.bicep` that IS this area's module, or None.

    With a clone on disk: `<area id>/main.bicep` when the area id is a directory in
    the checkout — whichever files changed in the window, so a child-only change
    still builds the parent, and never a child module (`<area>/<child>/main.bicep`)
    that happens to sort first. When the area dir exists but has no `main.bicep`
    (deleted) nothing is built. Only when the area id is NOT a directory (deleted
    dir, graphify `community:<n>`) does it fall back to the shallowest of the area's
    paths that exists on disk. Never a path missing from the checkout, never one
    under a nested `examples/`/`tests/` subtree. Without a clone (offline callers)
    the same rule runs over `paths` alone."""
    aid = area.get("id") or ""
    cands = [p for p in area.get("paths", [])
             if (p == "main.bicep" or p.endswith("/main.bicep"))
             and not _is_scaffold_path(p)]
    if _has_clone(clone_dir):
        rel = _area_dir_on_disk(aid, clone_dir, _BICEP_ROOT_ID)
        if rel is not None:
            entry = _rel_join(rel, "main.bicep")
            return entry if os.path.isfile(os.path.join(clone_dir, entry)) else None
        cands = [p for p in cands if os.path.isfile(os.path.join(clone_dir, p))]
    else:
        own = _rel_join(_area_rel_dir(aid, _BICEP_ROOT_ID), "main.bicep")
        if own in cands:
            return own
    return _shallowest(cands)


def _tf_entrypoint(area, clone_dir=None):
    """A `.tf` file in the directory that IS this area's module, or None (`main.tf`
    when that dir has one). Callers use its directory: terraform runs there and
    every `*.tf` in it is read.

    With a clone on disk: the area dir's own `*.tf` set when the area id is a
    directory in the checkout (regardless of which files changed in the window);
    otherwise the dir of the shallowest of the area's `.tf` paths that exists on
    disk. Never a path missing from the checkout, never one under a nested
    `examples/`/`tests/` subtree (`modules/sub/examples/x/main.tf` is not
    `modules/sub`). Without a clone the same rule runs over `paths` alone."""
    aid = area.get("id") or ""
    tfs = [p for p in area.get("paths", [])
           if p.endswith(".tf") and not _is_scaffold_path(p)]
    if _has_clone(clone_dir):
        rel = _area_dir_on_disk(aid, clone_dir, _TF_ROOT_ID)
        if rel is not None:
            return _pick_main_tf(_list_tf_files(clone_dir, rel))
        tfs = [p for p in tfs if os.path.isfile(os.path.join(clone_dir, p))]
        shallow = _shallowest(tfs)
        if shallow is None:
            return None
        return _pick_main_tf(_list_tf_files(clone_dir, os.path.dirname(shallow))
                             or [shallow])
    mod_dir = _tf_module_dir(aid, tfs)
    return _pick_main_tf([p for p in tfs if os.path.dirname(p) == mod_dir])


def _bicep_build_arm(run, clone_dir, rel_path, timeout=None):
    """Run bicep restore + build; return the compiled ARM JSON text. Not unit-tested.
    `timeout` (seconds) bounds each subprocess so a hung build can't stall the run."""
    full = os.path.join(clone_dir, rel_path)
    run(["bicep", "restore", full], timeout=timeout)   # pull registry modules
    return run(["bicep", "build", full, "--stdout"], timeout=timeout)


def _terraform_graph_dot(run, clone_dir, rel_dir, timeout=None):
    """Run terraform init (no backend) + graph; return DOT text. Not unit-tested.
    `timeout` (seconds) bounds each subprocess so a hung command can't stall the run."""
    full = os.path.join(clone_dir, rel_dir or ".")
    run(["terraform", f"-chdir={full}", "init", "-backend=false", "-input=false"],
        timeout=timeout)
    return run(["terraform", f"-chdir={full}", "graph"], timeout=timeout)


_CODEOWNERS_TOKEN = re.compile(r"(?:\\.|\S)+")


def parse_codeowners(text):
    """Parse a CODEOWNERS file into an ORDERED rule list
    `[{"pattern": "<pattern>", "owners": ["login", ...]}, ...]`, in file order.

    Each rule line is `<pattern> <owner...> [# comment]`. Blank lines and lines
    starting with `#` are skipped; an inline `# comment` (the first token starting
    with `#` after the pattern) and everything after it is dropped; GitLab-style
    section headers (`[Section]`, `^[Section]`, `[Section][2] @owner`) are not
    GitHub syntax and are skipped. `@user` / `@org/team` are stripped of the
    leading `@`; emails are kept as-is. A pattern with NO owners is KEPT (with
    `owners: []`): in CODEOWNERS it un-assigns ownership for matching paths.
    Duplicate patterns are kept in place — order matters because the last
    matching rule wins (derive.codeowners_owners). A list, not a dict, so the
    rule order survives the store (node data is JSON-encoded with sorted keys).
    Pure."""
    rules = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") or line.startswith("^["):
            continue
        tokens = _CODEOWNERS_TOKEN.findall(line)
        pattern, handles = tokens[0], []
        for tok in tokens[1:]:
            if tok.startswith("#"):
                break
            handles.append(tok)
        logins = [h[1:] if h.startswith("@") else h for h in handles]
        rules.append({"pattern": pattern, "owners": logins})
    return rules


# Conventional label namespace -> facet (auto-detect). AVM uses `Class:`/`Type:`/
# `Needs:`; most repos use `area`/`priority`/`status`/`lifecycle` with `:` or `/`.
# AVM's `Class:` (Resource/Pattern/Utility Module) says what *scope* an issue is
# about, not what kind of issue it is — nearly every AVM issue carries one — so
# it is its own `class` facet (recorded in the taxonomy, never a kind source, and
# not stamped by `apply_facets`). Only `type`/`kind` namespaces feed the kind facet.
_AUTO_FACET_NAMESPACES = {
    "area": "area", "component": "area",
    "priority": "priority", "p": "priority",
    "status": "status", "needs": "status",
    "lifecycle": "lifecycle",
    "type": "kind", "kind": "kind",
    "class": "class",
}


def _namespace_of(label):
    """Return the lowercase namespace token of a structured label, or None.
    A structured label looks like `<ns>: value` or `<ns>/value`."""
    for sep in (":", "/"):
        if sep in label:
            ns = label.split(sep, 1)[0].strip().lower()
            if ns:
                return ns, label.split(sep, 1)[0].strip() + sep
    return None


def detect_label_taxonomy(labels, config=None):
    """Auto-detect structured label namespaces and map them to facets.

    Returns {"<facet>": {"<namespace>": [label, ...]}, "source": "auto|config|merged"}.
    `config` (a {facet: [namespace-prefix, ...]} block) overrides/extends the
    auto-map. Degrades to {"source": "auto"} (no facets) rather than guessing on
    unprefixed labels. Pure."""
    auto = {}
    config_facets = {}

    # Build the config namespace -> facet lookup (prefixes may end with ':' or '/').
    cfg_lookup = {}
    for facet, prefixes in (config or {}).items():
        for pre in prefixes:
            cfg_lookup[pre.rstrip(":/").lower()] = (facet, pre)

    for label in labels or []:
        parsed = _namespace_of(label)
        if not parsed:
            continue
        ns_token, ns_display = parsed
        # config wins over auto for the same namespace token.
        if ns_token in cfg_lookup:
            facet, pre = cfg_lookup[ns_token]
            config_facets.setdefault(facet, {}).setdefault(pre, []).append(label)
        elif ns_token in _AUTO_FACET_NAMESPACES:
            facet = _AUTO_FACET_NAMESPACES[ns_token]
            auto.setdefault(facet, {}).setdefault(ns_display, []).append(label)

    # Merge config over auto.
    merged = {f: dict(ns) for f, ns in auto.items()}
    for facet, ns_map in config_facets.items():
        merged.setdefault(facet, {})
        for pre, labs in ns_map.items():
            merged[facet][pre] = labs

    if config_facets and auto:
        source = "merged"
    elif config_facets:
        source = "config"
    else:
        source = "auto"
    out = {f: ns for f, ns in merged.items()}
    out["source"] = source
    return out


_FACET_KEYS = ("area", "priority", "status", "lifecycle")

# Native issue-type / label-value / template / title word sequences -> canonical
# kind, in match priority order (the specific multi-word module phrases first).
# Matched on whole words only: a bare "module" (e.g. AVM's "[AVM Module Issue]"
# bug titles, `avm_module_issue.yml`) is NOT a module request, "debugger" is not
# "bug" and "supported" is not "support".
_KIND_PHRASES = (
    (("module", "request"), "module-request"),
    (("module", "proposal"), "module-request"),
    (("feature", "request"), "feature"),
    (("feature",), "feature"), (("enhancement",), "feature"),
    (("bug",), "bug"), (("defect",), "bug"),
    (("idea",), "idea"), (("proposal",), "idea"),
    (("question",), "question"), (("support",), "question"),
    (("doc",), "docs"), (("docs",), "docs"), (("documentation",), "docs"),
)
_VALID_KINDS = {"feature", "module-request", "bug", "idea", "question", "docs", "other"}
_KIND_WORD_RE = re.compile(r"[a-z0-9]+")


def _kind_words(text):
    """Lowercase ASCII-alphanumeric words of `text` (punctuation, `_`, `-`, `/`
    and emoji are separators)."""
    return tuple(_KIND_WORD_RE.findall((text or "").lower()))


def _kind_from_token(text):
    """Map free text (issue-type name, label value, template stem, title) to a
    kind: the first `_KIND_PHRASES` entry occurring as a whole-word sequence."""
    words = _kind_words(text)
    for phrase, kind in _KIND_PHRASES:
        n = len(phrase)
        if any(words[i:i + n] == phrase for i in range(len(words) - n + 1)):
            return kind
    return None


def _kind_from_bare_label(label):
    """Kind of an un-namespaced label that is *exactly* a kind phrase (`bug`,
    `enhancement`, `documentation`, `Bug 🐛`), else None. A label that merely
    contains a kind word (`good first issue`, `bug-bash`) is not a kind label."""
    if _namespace_of(label):
        return None
    words = _kind_words(label)
    for phrase, kind in _KIND_PHRASES:
        if words == phrase:
            return kind
    return None


def _labels_in_taxonomy(item, taxonomy, facet):
    """Labels on `item` that belong to `facet` per the taxonomy, order-preserving."""
    facet_labels = set()
    for labs in (taxonomy.get(facet) or {}).values():
        facet_labels.update(labs)
    return [lbl for lbl in (item.get("labels") or []) if lbl in facet_labels]


def apply_facets(item, taxonomy):
    """Derive {area, priority, status, lifecycle} for an item from its labels.

    Each facet takes the first matching label (or None). Pure; never raises on
    an empty taxonomy (every facet is then None)."""
    out = {}
    for facet in _FACET_KEYS:
        matches = _labels_in_taxonomy(item, taxonomy, facet)
        out[facet] = matches[0] if matches else None
    return out


def classify_issue_kind(issue, taxonomy, types_present):
    """Classify an issue into one of feature/module-request/bug/idea/question/docs/other.

    Priority: native GitHub issue type (when present) -> label kind (the
    `type:`/`kind:` facet labels, then a bare label that is exactly a kind word
    such as `bug`/`enhancement`) -> issue-template filename -> title heuristic
    -> other. `Class:` labels never decide kind, and the free-text heuristic
    reads the title only (whole words), never the body. Pure."""
    # 1. native issue type
    if types_present:
        kind = _kind_from_token(issue.get("issue_type"))
        if kind:
            return kind
    # 2a. label kind facet (the values carried under the taxonomy's `kind` facet)
    for lbl in _labels_in_taxonomy(issue, taxonomy, "kind"):
        value = lbl.split(":", 1)[-1] if ":" in lbl else lbl.split("/", 1)[-1]
        kind = _kind_from_token(value)
        if kind:
            return kind
    # 2b. bare kind labels (GitHub defaults: bug, enhancement, documentation, ...)
    for lbl in issue.get("labels") or []:
        kind = _kind_from_bare_label(lbl)
        if kind:
            return kind
    # 3. issue-template filename (e.g. module_request.md, bug_report.yml)
    kind = _kind_from_token(issue.get("template"))
    if kind:
        return kind
    # 4. title heuristic (title only: bodies mention modules, bugs, "support" ...)
    title = issue.get("title") or ""
    if "?" in title:
        return "question"
    kind = _kind_from_token(title)
    if kind:
        return kind
    return "other"


# Conventional-commit type prefix -> canonical kind (only the prefixes that map
# cleanly onto _VALID_KINDS; everything else falls through to None/"other").
_CC_TYPE_TO_KIND = {"feat": "feature", "fix": "bug", "docs": "docs"}
_CC_PREFIX_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:")


def classify_pr_kind(pr):
    """Kind from a PR's conventional-commit title prefix: feat->feature, fix->bug,
    docs->docs. Returns None when the title has no recognized prefix, so callers
    can fall through to 'other'. Pure.

    Used as a fallback for trains with no typed root issue: repos like AVM title
    PRs conventionally (`feat:`/`fix:`) but rarely close a typed issue, so without
    this every train's kind would be 'other' and significance could not weight
    feature vs fix."""
    title = (pr.get("title") or "").strip().lower()
    m = _CC_PREFIX_RE.match(title)
    if not m:
        return None
    return _CC_TYPE_TO_KIND.get(m.group(1))


def fetch_all(get_page, first_url):
    """Walk a paginated endpoint. `get_page(url)` returns (items, next_url|None).
    Network/parse details live in the caller's closure, so this is testable with
    a fake."""
    items = []
    url = first_url
    while url:
        page_items, url = get_page(url)
        items.extend(page_items)
    return items


def fetch_until(get_page, first_url, more):
    """Like `fetch_all`, but stop paging once `more(page_items)` returns False.
    The page that triggered the stop is still included. Lets a caller bound a
    sorted endpoint — e.g. stop once results fall before the window — instead of
    walking the entire history of a large repo."""
    items = []
    url = first_url
    while url:
        page_items, url = get_page(url)
        items.extend(page_items)
        if not more(page_items):
            break
    return items


def parse_args(argv):
    p = argparse.ArgumentParser(description="Acquire an activity-overview store.")
    p.add_argument("--owner")
    p.add_argument("--repo")
    p.add_argument("--from", dest="from")
    p.add_argument("--to")
    p.add_argument("--branches", default="main")
    p.add_argument("--clone-dir", default=None)
    p.add_argument("--no-clone", action="store_true")
    p.add_argument("--ref-date", default=None,
                   help="Reference date for milestone/sprint/forecast/flow "
                        "framing (YYYY-MM-DD or a full ISO timestamp; default: "
                        "--to). Stored with the window; view.py/digest.py "
                        "read it back.")
    p.add_argument("--include-workflows", dest="include_workflows",
                   action="store_true", default=True)
    p.add_argument("--no-workflows", dest="include_workflows", action="store_false")
    p.add_argument("--include-releases", dest="include_releases",
                   action="store_true", default=True)
    p.add_argument("--no-releases", dest="include_releases", action="store_false")
    # Phase 12: auto-discover + ingest the repo's Projects v2 board (its single-
    # select Status, plus sprint/iteration nodes when the board defines them). On
    # by default; degrades to empty on any error (no board / missing read:project
    # scope). --no-project-board skips the GraphQL query entirely.
    p.add_argument("--project-board", dest="project_board",
                   action="store_true", default=True)
    p.add_argument("--no-project-board", dest="project_board",
                   action="store_false")
    # Store-only (Phase 7): the SQLite journey-graph store is THE deliverable.
    # gather folds the assembled bundle into it and writes no bundle file. The
    # bundle is a transient view extract materializes from the store (Phase 8
    # reader stage). Roll-up = a wider range_query; resume = re-fold against the
    # pinned clone_sha — both store-native, so the flat-bundle flags are gone.
    p.add_argument("--store", required=True,
                   help="path to the SQLite journey-graph store to fold into "
                        "(the sole gather deliverable)")
    p.add_argument("--manifest",
                   help="path to a multi-repo project manifest (JSON). Mutually "
                        "exclusive with --owner/--repo; folds every member repo "
                        "into one store under the manifest's logical project name.")
    args = p.parse_args(argv)
    # --manifest carries its own member list + window, so the single-repo flags
    # are meaningless alongside it. Enforce the documented exclusivity rather than
    # silently letting the manifest branch win (ambiguous, misleading help text).
    if args.manifest and (args.owner or args.repo
                          or getattr(args, "from") or args.to):
        p.error("--manifest is mutually exclusive with --owner/--repo/--from/--to")
    # Single-repo flags get the same name/date rules as a manifest member
    # (shared validators in manifest.py): owner/repo go unescaped into API and
    # clone URLs, and the window must be real YYYY-MM-DD dates with from <= to.
    # Presence is still checked by acquire(); here only given values are checked.
    frm = getattr(args, "from")
    try:
        if args.owner is not None:
            manifest_mod.check_owner(args.owner, "--owner")
        if args.repo is not None:
            manifest_mod.check_repo(args.repo, "--repo")
        if frm is not None:
            manifest_mod.check_date(frm, "--from")
        if args.to is not None:
            manifest_mod.check_date(args.to, "--to")
        if frm is not None and args.to is not None:
            manifest_mod.check_window(frm, args.to, "--from", "--to")
        # Stored with the window at fold and read back by every view (#75).
        if args.ref_date is not None:
            manifest_mod.check_ref_date(args.ref_date, "--ref-date")
    except ValueError as exc:
        p.error(str(exc))
    return args


def resolve_token(env):
    """Return a GitHub token from env, preferring GITHUB_TOKEN. Exit if absent."""
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        sys.stderr.write(
            "error: set GITHUB_TOKEN (or GH_TOKEN) with `repo` scope (read access)\n"
        )
        raise SystemExit(2)
    return token


def run_git(args, cwd=None, timeout=None):
    """Thin wrapper around git. Its live git interaction is integration-only,
    but the non-UTF-8 decode path below is covered by TestRunGitDecoding.
    Surfaces git's own stderr on failure so errors like "not a git repository"
    reach the user. An optional
    `timeout` (seconds) bounds the call — used by the IaC edge build so a hung
    `bicep`/`terraform` cannot stall the run (TimeoutExpired propagates).

    Decodes git's output as UTF-8 with `errors="replace"`: real-world patches
    can carry non-UTF-8 bytes (latin-1 source, binary hunks), and a strict
    decode would hard-crash the whole run on a single bad byte. Replacing keeps
    the diff/log walks robust on large repos (e.g. Azure/bicep).

    When the command is `git`, `-c core.quotepath=false` is inserted right after
    it so non-ASCII paths print verbatim instead of as C-quoted octal escapes
    (`"docs/caf\\303\\251.md"`). Paths with a quote, backslash, TAB or newline are
    still C-quoted by git; the path parsers undo that via unquote_git_path.
    Non-git commands routed through here (bicep, terraform, graphify) are passed
    through untouched."""
    args = list(args)
    if args and os.path.basename(args[0]) == "git":
        args = [args[0], "-c", "core.quotepath=false"] + args[1:]
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def git_ls_files(clone_dir, run=None):
    """Tracked paths of the clone (`git ls-files`), C-quoting undone."""
    run = run or run_git
    return [unquote_git_path(ln)
            for ln in run(["git", "-C", clone_dir, "ls-files"]).splitlines() if ln]


def clone_head_sha(clone_dir, run=run_git):
    """Resolve the clone's HEAD commit SHA — stamped into `meta.clone_sha` as
    structural provenance.

    Pins the exact tree the code_graph/edges were built against. The store-native
    resume (re-fold against the pinned SHA) keys off it so a refresh rebuilds from
    the identical source (no mixed-SHA graph). Returns the 40-char SHA, or None
    when there is no clone (e.g. --no-clone with nothing on disk)."""
    try:
        return run(["git", "-C", clone_dir, "rev-parse", "HEAD"]).strip() or None
    except Exception:
        return None


# IaC edge-extraction tuning. The timeout is GENEROUS — a healthy cold bicep/
# terraform build finishes well under it, so the bound only trips a genuinely hung
# process (never a slow-but-progressing one). Speed comes from bounded concurrency,
# not a tight timeout. A timed-out/failed build is retried once, then recorded as a
# VISIBLE gap (edges_status) rather than a silent empty.
#
# Env-overridable for heavy live runs: a real AVM pattern module's `terraform init`
# pulls dozens of registry modules (raise the timeout). A shared TF_PLUGIN_CACHE_DIR
# is not safe under concurrent writes, but `_terraform_prewarm` warms every area's
# providers serially first so the parallel inits are cache HITS (no race) — keeping
# parallel extraction safe without lowering the workers. The defaults are unchanged
# for the offline test/CI path.
#
# These are the DEFAULTS. The env overrides are read at CALL time (_iac_settings)
# through `_env_int`, never at import: a non-integer is a clear error naming the
# variable, and an out-of-range value (e.g. ACTIVITY_IAC_MAX_WORKERS=0, which
# ThreadPoolExecutor rejects) is clamped to its minimum with a warning.
IAC_BUILD_TIMEOUT = 300  # seconds per subprocess   (ACTIVITY_IAC_BUILD_TIMEOUT, min 1)
IAC_MAX_WORKERS = 8      # parallel per-module builds (ACTIVITY_IAC_MAX_WORKERS, min 1)
IAC_RETRIES = 1          # extra attempts on timeout/failure (ACTIVITY_IAC_RETRIES, min 0)


def _env_int(name, default, minimum, env=None):
    """Read an integer env knob. Unset/blank -> `default`. A non-integer raises
    SystemExit with a message naming the variable and the bad value. A value
    below `minimum` is clamped to `minimum` with a stderr warning."""
    env = os.environ if env is None else env
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise SystemExit(
            "error: {} must be an integer (got {!r})".format(name, raw)) from None
    if value < minimum:
        sys.stderr.write("warning: {}={} is below the minimum {}; using {}\n".format(
            name, value, minimum, minimum))
        return minimum
    return value


def _iac_settings(env=None):
    """(timeout, max_workers, retries) for the IaC edge build, from env at call time."""
    return (_env_int("ACTIVITY_IAC_BUILD_TIMEOUT", IAC_BUILD_TIMEOUT, 1, env),
            _env_int("ACTIVITY_IAC_MAX_WORKERS", IAC_MAX_WORKERS, 1, env),
            _env_int("ACTIVITY_IAC_RETRIES", IAC_RETRIES, 0, env))


def _list_tf_files(clone_dir, rel_dir):
    """Every *.tf file (clone-relative path) in a module directory, sorted.

    Terraform treats ALL *.tf in a directory as ONE module, and real modules spread
    their `module` blocks across files (an AVM consumer puts them in
    main.networking.tf / main.monitoring.tf / ..., not main.tf). Edge extraction
    must read the whole directory so `build_terraform_edges` sees every module
    source — the terraform-graph DOT already spans the whole dir. Returns [] when
    the dir cannot be listed (the caller falls back to the entrypoint)."""
    full = os.path.join(clone_dir, rel_dir) if rel_dir else clone_dir
    try:
        names = sorted(f for f in os.listdir(full) if f.endswith(".tf"))
    except OSError:
        return []
    return [os.path.join(rel_dir, f) if rel_dir else f for f in names]


def _read_tf_modules_json(read_text, clone_dir, rel_dir):
    """The `.terraform/modules/modules.json` that `terraform init` just wrote in the
    module dir (its record of the installed module tree, used to name NESTED
    module calls), or None when it is absent or unreadable — the nested edges then
    keep an unknown target; the build itself still counts as resolved."""
    try:
        return read_text(os.path.join(clone_dir, rel_dir, ".terraform", "modules",
                                      "modules.json"))
    except Exception:
        return None


def _extract_area_edges(area, clone_dir, area_ids, patterns, have_bicep, have_tf,
                        run, read_text, timeout, retries):
    """Resolve ONE area's edges. Returns (edges_or_None, status). Never raises.

    status: `resolved` (build succeeded — edges may legitimately be empty),
    `timeout` (build exceeded the bound after retries), `failed` (build errored
    after retries), `skipped` (no entrypoint or its CLI absent). A timeout/failure
    leaves edges untouched but is reported via the status so the gap is visible."""
    # Entrypoints are resolved on disk in the clone (the area's own module file),
    # not from the in-window changed paths — see _bicep_entrypoint/_tf_entrypoint.
    bp = _bicep_entrypoint(area, clone_dir)
    tp = _tf_entrypoint(area, clone_dir)
    if bp and have_bicep:
        kind = "bicep"
    elif tp and have_tf:
        kind = "terraform"
    else:
        return None, "skipped"

    status = "failed"
    for _attempt in range(retries + 1):
        try:
            if kind == "bicep":
                arm = json.loads(_bicep_build_arm(run, clone_dir, bp, timeout))
                src = read_text(os.path.join(clone_dir, bp))
                return build_bicep_edges(
                    src, arm, bp, area_ids, patterns,
                    module_exists=_clone_module_exists(clone_dir)), "resolved"
            rel_dir = os.path.dirname(tp)
            dot = _terraform_graph_dot(run, clone_dir, rel_dir, timeout)
            # Read EVERY *.tf in the module dir (not just the entrypoint): a module's
            # `module` blocks span all its files, and terraform graph already spans
            # the whole dir. Fall back to the entrypoint if the dir can't be listed.
            tf_files = _list_tf_files(clone_dir, rel_dir) or [tp]
            src = "\n".join(read_text(os.path.join(clone_dir, f)) for f in tf_files)
            mods = _read_tf_modules_json(read_text, clone_dir, rel_dir)
            return build_terraform_edges(src, dot, tp, area_ids, patterns,
                                         modules_json=mods), "resolved"
        except subprocess.TimeoutExpired:
            status = "timeout"   # hung build: bounded, retried, then recorded
        except Exception:
            status = "failed"
    return None, status


def _terraform_prewarm(build_areas, clone_dir, run, timeout):
    """Populate a SHARED TF_PLUGIN_CACHE_DIR before the parallel builds. terraform's
    plugin cache is not safe under concurrent writes, so a cold cache + N parallel
    `terraform init` would race on provider downloads. Warm EVERY build area's
    providers serially up front; the subsequent parallel inits are then pure cache
    HITS (read-only) — race-free for ANY provider mix, not just members that happen
    to share the same providers. Best-effort: a single warm-up failing is ignored
    (that area's parallel build still runs and reports its own status)."""
    for area in build_areas:
        tp = _tf_entrypoint(area, clone_dir)
        if not tp:
            continue
        full = os.path.join(clone_dir, os.path.dirname(tp) or ".")
        try:
            run(["terraform", f"-chdir={full}", "init", "-backend=false",
                 "-input=false"], timeout=timeout)
        except Exception:
            pass


def extract_iac_edges(code_graph, clone_dir, which=shutil.which, run=run_git,
                      read_text=_read_text_file, patterns=None,
                      max_workers=None, timeout=None, retries=None):
    """Enrich each area's `edges` with inter-area dependency edges (BUILD-ONLY).

    Each area with a main.bicep (and `bicep` on PATH) or a *.tf (and `terraform` on
    PATH) is built — in PARALLEL across `max_workers`, each subprocess bounded by
    `timeout` and retried `retries` times — to resolve its edges. ANY missing CLI,
    restore failure, build error, or timeout leaves `edges` as-is ([]) but stamps
    `area["edges_status"]` so the gap is VISIBLE, never a silent empty. An aggregate
    `code_graph["edge_extraction"]` counts resolved/timeout/failed/skipped. Injectable
    `which`/`run`/`read_text` keep it offline-testable. Mutates and returns code_graph."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    # None -> the env-overridable defaults (read now, not at import). An explicit
    # max_workers is still clamped so ThreadPoolExecutor never sees a value < 1.
    env_timeout, env_workers, env_retries = _iac_settings()
    timeout = env_timeout if timeout is None else timeout
    max_workers = env_workers if max_workers is None else max(1, max_workers)
    retries = env_retries if retries is None else retries
    areas = code_graph.get("areas", [])
    area_ids = {a["id"] for a in areas}
    have_bicep = bool(which("bicep"))
    have_tf = bool(which("terraform"))
    summary = {"resolved": 0, "timeout": 0, "failed": 0, "skipped": 0}

    # No toolchain: build-only ⇒ everything skipped.
    if not (have_bicep or have_tf):
        for area in areas:
            area["edges_status"] = "skipped"
        summary["skipped"] = len(areas)
        code_graph["edge_extraction"] = summary
        return code_graph

    def work(area):
        return area, _extract_area_edges(area, clone_dir, area_ids, patterns,
                                         have_bicep, have_tf, run, read_text,
                                         timeout, retries)

    # examples/ and tests/ are module scaffolding whose edges are excluded from
    # depends_on at fold time; don't spend a terraform/bicep build on them (the AVM
    # pattern examples instantiate the whole stack) — mark skipped, build the rest.
    build = []
    for area in areas:
        if _is_scaffold_area(area.get("id")):
            area["edges_status"] = "skipped"
            summary["skipped"] += 1
        else:
            build.append(area)

    # Parallel terraform graph generation, mirroring the parallel bicep build path
    # (both run through one ThreadPoolExecutor). With a SHARED TF_PLUGIN_CACHE_DIR
    # the cold-cache parallel inits would race, so warm it once serially first.
    if have_tf and max_workers > 1 and os.environ.get("TF_PLUGIN_CACHE_DIR"):
        _terraform_prewarm(build, clone_dir, run, timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for area, (edges, status) in pool.map(work, build):
            if edges is not None:
                area["edges"] = edges
            area["edges_status"] = status
            summary[status] += 1

    code_graph["edge_extraction"] = summary
    return code_graph


# --- GitHub HTTP layer (#56) ---------------------------------------------------
#
# Every REST and GraphQL call goes through `_github_open`, which adds:
#   * a timeout on every request (ACTIVITY_HTTP_TIMEOUT, default 60 s), so one
#     stalled connection cannot hang the run;
#   * bounded retries with exponential back-off on timeouts, connection errors
#     and 5xx (ACTIVITY_HTTP_RETRIES, default 3 extra attempts);
#   * rate-limit back-off per GitHub's documented rules: a 403/429 with
#     `x-ratelimit-remaining: 0` (primary) waits until `x-ratelimit-reset`; one
#     with `Retry-After` waits that long; a secondary-limit message with neither
#     header waits 60 s, doubling on repeats. Every wait is announced on stderr
#     (how long, until when), is capped by ACTIVITY_RATE_LIMIT_MAX_WAIT (default
#     3600 s, 0 = never wait) and at most RATE_LIMIT_MAX_WAITS waits are made per
#     request. Any other 403 (SAML SSO, token scope) fails fast with the existing
#     diagnostic;
#   * a redirect handler that drops `Authorization` when a 3xx leaves
#     https://api.github.com (urllib's default handler would re-send it), and the
#     token is only ever attached to https://api.github.com URLs in the first place.
# `opener`, `sleep` and `clock` are injectable so the suite never sleeps or
# touches the network. The token never appears in a log line or error message:
# only URLs (which carry no credentials) and GitHub's response headers are shown.

GITHUB_API_HOST = "api.github.com"
HTTP_TIMEOUT = 60                  # seconds per request   (ACTIVITY_HTTP_TIMEOUT, min 1)
HTTP_RETRIES = 3                   # extra attempts on timeout/5xx (ACTIVITY_HTTP_RETRIES, min 0)
RATE_LIMIT_MAX_WAIT = 3600         # longest single wait, s (ACTIVITY_RATE_LIMIT_MAX_WAIT, min 0)
RATE_LIMIT_MAX_WAITS = 5           # rate-limit waits per request before giving up
SECONDARY_RATE_LIMIT_WAIT = 60     # GitHub: "wait at least one minute" with no header
_RETRYABLE_STATUS = (500, 502, 503, 504)
_BACKOFF_CAP = 60


class GitHubAPIError(Exception):
    """A GitHub API failure (HTTP error, exhausted retries, GraphQL errors with no
    data). A normal Exception, so best-effort layers (the Projects v2 board) can
    catch it and degrade. REST `http_get_json` re-raises it as SystemExit to keep
    its fail-fast contract."""


def _http_settings(env=None):
    """(timeout, retries, max_wait) from env at call time (validated)."""
    return (_env_int("ACTIVITY_HTTP_TIMEOUT", HTTP_TIMEOUT, 1, env),
            _env_int("ACTIVITY_HTTP_RETRIES", HTTP_RETRIES, 0, env),
            _env_int("ACTIVITY_RATE_LIMIT_MAX_WAIT", RATE_LIMIT_MAX_WAIT, 0, env))


def validate_env_knobs(env=None):
    """Parse every integer env knob once, up front, so a bad value fails before
    any clone or API call (SystemExit naming the variable)."""
    _http_settings(env)
    _iac_settings(env)


def _auth_allowed(url):
    """The token is only ever sent to https://api.github.com."""
    parts = urllib.parse.urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "").lower() == GITHUB_API_HOST


class _GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but strip `Authorization` when the target is not
    https://api.github.com (e.g. a signed blob-storage URL for logs/artifacts)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _auth_allowed(newurl):
            for store in (new.headers, new.unredirected_hdrs):
                for key in [k for k in store if k.lower() == "authorization"]:
                    del store[key]
        return new


def _default_opener():
    return urllib.request.build_opener(_GitHubRedirectHandler)


def _github_headers(url, token, extra=None):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "activity-overview"}
    if token and _auth_allowed(url):
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra or {})
    return headers


def _header_int(headers, name):
    value = headers.get(name) if headers is not None else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _error_message(body):
    try:
        return json.loads((body or b"").decode()).get("message", "") or ""
    except Exception:
        return ""


def _rate_limit_wait(code, headers, message, clock, secondary_seen=0):
    """Classify a refusal. Returns (kind, seconds) with kind 'primary' or
    'secondary', or (None, 0) for a response that is NOT a rate limit (a genuine
    403 for SSO/scope must fail fast). Follows GitHub's documented rules: honour
    Retry-After; with x-ratelimit-remaining 0 wait until x-ratelimit-reset; a
    secondary limit with neither header waits a minute, growing on repeats."""
    if code not in (403, 429):
        return None, 0
    remaining = str(headers.get("x-ratelimit-remaining") or "").strip()
    retry_after = _header_int(headers, "Retry-After")
    text = (message or "").lower()
    secondary_msg = "secondary rate limit" in text or "abuse" in text
    if retry_after is not None:
        return ("primary" if remaining == "0" and not secondary_msg
                else "secondary"), max(1, retry_after)
    if remaining == "0":
        reset = _header_int(headers, "x-ratelimit-reset")
        wait = SECONDARY_RATE_LIMIT_WAIT if reset is None else int(reset - clock()) + 1
        return ("secondary" if secondary_msg else "primary"), max(1, wait)
    if secondary_msg or code == 429:
        return "secondary", SECONDARY_RATE_LIMIT_WAIT * (2 ** secondary_seen)
    return None, 0


def _utc(ts):
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_for_rate_limit(kind, wait, url, waits, max_wait, sleep, clock, diagnostic):
    """Sleep out one rate-limit window (bounded + announced on stderr) or raise."""
    if waits > RATE_LIMIT_MAX_WAITS:
        raise GitHubAPIError(
            f"{diagnostic}\n  rate limit: still limited after "
            f"{RATE_LIMIT_MAX_WAITS} waits; giving up.")
    if wait > max_wait:
        raise GitHubAPIError(
            f"{diagnostic}\n  rate limit: {kind} limit resets in {wait}s (at "
            f"{_utc(clock() + wait)}), longer than ACTIVITY_RATE_LIMIT_MAX_WAIT="
            f"{max_wait}s; not waiting. Re-run later or raise the cap.")
    sys.stderr.write(
        f"warning: GitHub {kind} rate limit hit on {url}; waiting {wait}s "
        f"(until {_utc(clock() + wait)}) [wait {waits}/{RATE_LIMIT_MAX_WAITS}]\n")
    sleep(wait)


def _github_open(req, url, allow_404=False, opener=None, sleep=None, clock=None,
                 rate_limited=None):
    """Send `req` with timeout, retries and rate-limit back-off. Returns
    (headers, body_bytes), or (None, 404) for a 404 when allow_404. Raises
    GitHubAPIError on any other failure.

    `rate_limited(headers, body)` optionally reports a rate limit signalled in a
    200 body (GraphQL `RATE_LIMITED`); it returns True to trigger a wait."""
    opener = opener or _default_opener()
    sleep = sleep or time.sleep
    clock = clock or time.time
    timeout, retries, max_wait = _http_settings()
    attempt, waits, secondary = 0, 0, 0

    def backoff(reason):
        nonlocal attempt
        attempt += 1
        delay = min(_BACKOFF_CAP, 2 ** (attempt - 1))
        sys.stderr.write(
            f"warning: GitHub API {reason} for {url}; retrying in {delay}s "
            f"(attempt {attempt + 1}/{retries + 1})\n")
        sleep(delay)

    while True:
        try:
            with opener.open(req, timeout=timeout) as resp:
                headers, body = resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            if allow_404 and err.code == 404:
                return None, 404
            try:
                body = err.read()
            except Exception:
                body = b""
            diagnostic = _format_http_error(url, err, body)
            kind, wait = _rate_limit_wait(err.code, err.headers,
                                          _error_message(body), clock, secondary)
            if kind:
                waits += 1
                secondary += kind == "secondary"
                _wait_for_rate_limit(kind, wait, url, waits, max_wait, sleep,
                                     clock, diagnostic)
                continue
            if err.code in _RETRYABLE_STATUS and attempt < retries:
                backoff(f"{err.code}")
                continue
            if err.code in _RETRYABLE_STATUS:
                diagnostic += f"\n  gave up after {attempt + 1} attempt(s)."
            raise GitHubAPIError(diagnostic) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            # URLError (DNS, refused, wrapped timeout), TimeoutError/socket.timeout
            # and ConnectionError (resets) are OSError subclasses; HTTPException
            # covers RemoteDisconnected / IncompleteRead mid-body.
            reason = getattr(err, "reason", None) or err
            if attempt < retries:
                backoff(f"request failed ({type(err).__name__}: {reason})")
                continue
            raise GitHubAPIError(
                f"error: GitHub API request to {url} failed after {attempt + 1} "
                f"attempt(s): {type(err).__name__}: {reason} "
                f"(timeout {timeout}s; ACTIVITY_HTTP_TIMEOUT / "
                f"ACTIVITY_HTTP_RETRIES tune this)") from None
        if rate_limited is not None and rate_limited(headers, body):
            waits += 1
            reset = _header_int(headers, "x-ratelimit-reset")
            wait = (SECONDARY_RATE_LIMIT_WAIT if reset is None
                    else max(1, int(reset - clock()) + 1))
            _wait_for_rate_limit("primary", wait, url, waits, max_wait, sleep,
                                 clock, f"error: GitHub API rate limited for {url}")
            continue
        return headers, body


def http_get_json(url, token, allow_404=False, opener=None, sleep=None, clock=None):
    """GET a GitHub API URL → (parsed_json, next_url).

    Timeouts, retries, rate-limit back-off and redirect auth-stripping come from
    `_github_open` (see the section comment above). On an HTTP error, GitHub
    explains the cause in the response body and a few headers (rate limit vs.
    SAML SSO vs. token scope); `_format_http_error` surfaces both.

    With allow_404=True a 404 returns (None, 404) instead of raising, so the
    backfill seam can map a definitively-absent ref to ABSENT. All other
    failures raise SystemExit with the diagnostic (fail fast).
    """
    req = urllib.request.Request(url, headers=_github_headers(url, token))
    try:
        headers, body = _github_open(req, url, allow_404=allow_404, opener=opener,
                                     sleep=sleep, clock=clock)
    except GitHubAPIError as err:
        raise SystemExit(str(err)) from None
    if headers is None and body == 404:
        return None, 404
    return json.loads(body.decode()), _next_link(headers.get("Link", ""))


# Headers GitHub uses to explain a refusal; surfaced verbatim on error so the
# distinction (rate limit / SAML SSO / missing scope) is visible without guessing.
_DIAGNOSTIC_HEADERS = (
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "retry-after",
    "x-github-sso",
    "x-accepted-oauth-scopes",
    "x-oauth-scopes",
)


def _format_http_error(url, err, body=None):
    """Build a one-screen diagnostic from an HTTPError: status, GitHub's message,
    and the headers that disambiguate a 403. `body` is the already-read response
    body (an HTTPError body can only be read once); None reads it here."""
    if body is None:
        try:
            body = err.read()
        except Exception:
            body = b""
    detail = _error_message(body)
    lines = [f"error: GitHub API {err.code} for {url}"]
    if detail:
        lines.append(f"  message: {detail}")
    for name in _DIAGNOSTIC_HEADERS:
        value = err.headers.get(name) if err.headers is not None else None
        if value:
            lines.append(f"  {name}: {value}")
    if err.code == 403:
        lines.append(
            "  hint: 403 with rate-limit headers = rate limited; with x-github-sso "
            "= authorize the PAT for the org's SAML SSO; otherwise check token scope."
        )
    return "\n".join(lines)


# --- Projects v2 (Phase 12): GraphQL board ingest ----------------------------
#
# gather is REST-only everywhere else; this is its single GraphQL call. The
# helper is kept THIN (like http_get_json); it is tested through an injected
# opener (test_github_api.py), and the normalization (parse_project_board) and
# the fetch driver (fetch_project_board) against crafted fixtures.

GRAPHQL_URL = "https://api.github.com/graphql"

# Auto-discover the repo's board(s) and pull each board's first page of items.
# `closed`/`updatedAt` drive the maintenance filter (skip deprecated boards). The
# shared `$cursor` is only used for this discovery page; every kept board's
# remaining items are paginated by NODE ID afterward (PROJECT_BOARD_ITEMS_QUERY),
# so a per-board cursor is never confused with another board's. Confirmed live.
PROJECT_BOARD_QUERY = """
query($owner:String!, $repo:String!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    projectsV2(first:100) {
      nodes {
        id
        number
        title
        closed
        updatedAt
        fields(first:50) {
          nodes {
            __typename
            ... on ProjectV2IterationField {
              name
              configuration {
                iterations { id title startDate duration }
                completedIterations { id title startDate duration }
              }
            }
          }
        }
        items(first:100, after:$cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            content {
              __typename
              ... on Issue { number repository { nameWithOwner } }
              ... on PullRequest { number repository { nameWithOwner } }
            }
            fieldValues(first:30) {
              nodes {
                __typename
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2FieldCommon { name } }
                }
                ... on ProjectV2ItemFieldIterationValue { title iterationId }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


# Board-scoped item pagination: paginate ONE board's items by its node id (so a
# cursor always belongs to that board's connection — unlike the discovery query,
# whose shared $cursor can't correctly paginate multiple boards at once).
PROJECT_BOARD_ITEMS_QUERY = """
query($id:ID!, $cursor:String) {
  node(id:$id) {
    ... on ProjectV2 {
      items(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            __typename
            ... on Issue { number repository { nameWithOwner } }
            ... on PullRequest { number repository { nameWithOwner } }
          }
          fieldValues(first:30) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldIterationValue { title iterationId }
            }
          }
        }
      }
    }
  }
}
""".strip()


def _graphql_rate_limited(headers, body):
    """True when a 200 GraphQL response reports a RATE_LIMITED error."""
    try:
        errors = json.loads(body.decode()).get("errors") or []
    except Exception:
        return False
    return any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED"
               for e in errors)


def graphql_post(token, query, variables=None, opener=None, sleep=None, clock=None):
    """POST a GraphQL query to GitHub's `/graphql`, returning the parsed `data`.

    Shares `_github_open` with http_get_json (timeout, retries, rate-limit
    back-off incl. a 200 carrying a `RATE_LIMITED` error, redirect auth-strip).
    Failures raise `GitHubAPIError` — a normal Exception, so the board layer's
    "never hard-fail" contract can catch it. When the response carries `errors`
    AND non-empty `data` (e.g. one inaccessible board among several), the partial
    `data` is KEPT and the errors are warned on stderr; only errors with no data
    raise.
    """
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=payload, headers=_github_headers(
        GRAPHQL_URL, token, {"Content-Type": "application/json"}))
    _, raw = _github_open(req, GRAPHQL_URL, opener=opener, sleep=sleep, clock=clock,
                          rate_limited=_graphql_rate_limited)
    try:
        body = json.loads(raw.decode())
    except ValueError as err:
        raise GitHubAPIError(
            f"error: GraphQL {GRAPHQL_URL} returned non-JSON: {err}") from None
    data = body.get("data") if isinstance(body, dict) else None
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        msgs = "; ".join(
            e.get("message", "") for e in errors if isinstance(e, dict))
        if not data:
            raise GitHubAPIError(
                f"error: GraphQL {GRAPHQL_URL} returned errors: {msgs}")
        sys.stderr.write(
            f"warning: GraphQL {GRAPHQL_URL} returned partial data with errors "
            f"(keeping the data): {msgs}\n")
    return data or {}


def _iter_end(start, duration):
    """Inclusive last day of the iteration = startDate + (duration - 1) days (ISO
    YYYY-MM-DD). Inclusive (not the next iteration's start) so it matches the
    inclusive [from,to] window-overlap test. None-safe."""
    if not start or duration is None:
        return None
    try:
        d = datetime.date.fromisoformat(start[:10])
    except (TypeError, ValueError):
        return None
    return (d + datetime.timedelta(days=max(0, int(duration) - 1))).isoformat()


def parse_project_board(board_nodes):
    """Pure normalization of `repository.projectsV2.nodes` into
    `(sprints, items_by_ref)`:

      sprints      = {iteration_id: {"title", "start", "end"}} — the union of every
                     board's iterations + completedIterations (end = start+duration
                     days). EMPTY for status-only boards (no ProjectV2IterationField).
      items_by_ref = {(repo_nameWithOwner, number): {"status": <name|None>,
                     "sprint_id": <iterationId|None>}} — `status` is the
                     ProjectV2ItemFieldSingleSelectValue whose field.name == "Status";
                     `sprint_id` is the ProjectV2ItemFieldIterationValue's iterationId.

    Deterministic; tolerant of missing/None field values, content, and fields.
    """
    sprints = {}
    items_by_ref = {}
    for board in board_nodes or []:
        if not isinstance(board, dict):
            continue
        # iterations (+ completed) from the (optional) ProjectV2IterationField
        for f in ((board.get("fields") or {}).get("nodes") or []):
            if not isinstance(f, dict):
                continue
            if f.get("__typename") != "ProjectV2IterationField":
                continue
            cfg = f.get("configuration") or {}
            for key in ("iterations", "completedIterations"):
                for it in (cfg.get(key) or []):
                    if not isinstance(it, dict) or not it.get("id"):
                        continue
                    sprints[it["id"]] = {
                        "title": it.get("title"),
                        "start": it.get("startDate"),
                        "end": _iter_end(it.get("startDate"), it.get("duration")),
                    }
        # items -> (repo, number) -> {status, sprint_id}
        for item in ((board.get("items") or {}).get("nodes") or []):
            if not isinstance(item, dict):
                continue
            content = item.get("content") or {}
            number = content.get("number")
            repo_slug = (content.get("repository") or {}).get("nameWithOwner")
            if number is None or not repo_slug:
                continue
            status, sprint_id = None, None
            for fv in ((item.get("fieldValues") or {}).get("nodes") or []):
                if not isinstance(fv, dict):
                    continue
                tn = fv.get("__typename")
                if tn == "ProjectV2ItemFieldSingleSelectValue":
                    if (fv.get("field") or {}).get("name") == "Status":
                        status = fv.get("name")
                elif tn == "ProjectV2ItemFieldIterationValue":
                    sprint_id = fv.get("iterationId")
            # MERGE across boards (an item can be on several boards): keep the first
            # non-None status/sprint_id seen (boards arrive in deterministic order),
            # so one board's status + another's sprint combine instead of clobbering.
            cur = items_by_ref.get((repo_slug, number))
            if cur is None:
                items_by_ref[(repo_slug, number)] = {
                    "status": status, "sprint_id": sprint_id}
            else:
                if cur.get("status") is None:
                    cur["status"] = status
                if cur.get("sprint_id") is None:
                    cur["sprint_id"] = sprint_id
    return sprints, items_by_ref


def _board_index(items_by_ref, slug):
    """{number: facts} for the board items of repo `slug`, matching the board's
    `nameWithOwner` case-insensitively (issue #70: the manifest / --owner
    spelling need not match the API's canonical case). Should two spellings of
    one item ever appear they merge like parse_project_board does (first non-None
    status / sprint_id wins), in sorted key order so the result is deterministic.
    Returns fresh dicts; `items_by_ref` is not mutated. Pure."""
    want = (slug or "").lower()
    out = {}
    for key in sorted(items_by_ref, key=lambda k: (k[0] or "", k[1])):
        repo_slug, number = key
        if (repo_slug or "").lower() != want:
            continue
        facts = items_by_ref[key]
        cur = out.get(number)
        if cur is None:
            out[number] = dict(facts)
        else:
            for k in ("status", "sprint_id"):
                if cur.get(k) is None:
                    cur[k] = facts.get(k)
    return out


# A linked Projects v2 board left untouched for this long is treated as
# deprecated-but-not-removed and skipped (overridable via the env var).
BOARD_STALE_DAYS = 365

# Per-board item cap (defensive bound on a huge board). Raise via the env var
# when a board legitimately holds more items than this and is truncated.
BOARD_MAX_ITEMS = 5000


def _board_stale_days():
    """Effective board-staleness threshold in days. `ACTIVITY_BOARD_STALE_DAYS`
    overrides BOARD_STALE_DAYS at call time (a non-int/empty value falls back)."""
    try:
        return max(0, int(os.environ.get("ACTIVITY_BOARD_STALE_DAYS",
                                         BOARD_STALE_DAYS)))
    except (TypeError, ValueError):
        return BOARD_STALE_DAYS


def _board_max_items():
    """Effective per-board item cap. `ACTIVITY_BOARD_MAX_ITEMS` overrides
    BOARD_MAX_ITEMS at call time (a non-int/empty value falls back)."""
    try:
        return max(1, int(os.environ.get("ACTIVITY_BOARD_MAX_ITEMS",
                                         BOARD_MAX_ITEMS)))
    except (TypeError, ValueError):
        return BOARD_MAX_ITEMS


def board_is_maintained(board, ref_date=None, stale_days=None):
    """Whether a linked board is worth ingesting. A board is DROPPED when it is
    `closed` (archived — the clean "deprecated" signal) or its `updatedAt` is
    older than `stale_days` before `ref_date` (abandoned but never closed). A
    board with no `updatedAt`, or when `ref_date` is None/unparseable, is KEPT
    (we can't prove it stale). Pure; date-only comparison for determinism."""
    if not isinstance(board, dict):
        return False
    if board.get("closed"):
        return False
    updated = board.get("updatedAt")
    if not updated or not ref_date:
        return True
    if stale_days is None:
        stale_days = _board_stale_days()
    # Parse BOTH dates; an unparseable ref_date OR updatedAt means we can't prove
    # the board stale, so we keep it (matches the "can't prove it stale" intent).
    try:
        ref_d = datetime.date.fromisoformat(str(ref_date)[:10])
        upd_d = datetime.date.fromisoformat(str(updated)[:10])
    except (TypeError, ValueError):
        return True
    cutoff = ref_d - datetime.timedelta(days=max(0, int(stale_days)))
    return upd_d >= cutoff


def _paginate_board_items(graphql, board, owner, repo, max_items):
    """Collect ONE board's item nodes: seed from the discovery page, then page the
    rest by NODE ID (PROJECT_BOARD_ITEMS_QUERY — a cursor that belongs to this
    board's connection), capped at `max_items`. Warns on truncation so lost
    coverage is visible. Returns the (capped) list of item nodes."""
    nodes = list(((board.get("items") or {}).get("nodes") or []))
    info = (board.get("items") or {}).get("pageInfo") or {}
    cursor, has_next = info.get("endCursor"), info.get("hasNextPage")
    board_id, guard = board.get("id"), 0
    while has_next and board_id and len(nodes) < max_items and guard < 200:
        guard += 1
        try:
            page = graphql(PROJECT_BOARD_ITEMS_QUERY,
                           {"id": board_id, "cursor": cursor})
        except (SystemExit, Exception) as err:  # noqa: BLE001 — degrade, never hard-fail
            # SystemExit too: an injected/legacy graphql callable may signal an
            # HTTP error that way, and it is a BaseException `except Exception`
            # would miss (a 502 on page 2 used to escape fetch_project_board).
            sys.stderr.write(
                "warning: partial Projects v2 board #{} for {}/{}: {}\n".format(
                    board.get("number"), owner, repo, err))
            break
        items = ((page or {}).get("node") or {}).get("items") or {}
        page_nodes = items.get("nodes") or []
        if not page_nodes:
            break
        nodes.extend(page_nodes)
        info = items.get("pageInfo") or {}
        cursor, has_next = info.get("endCursor"), info.get("hasNextPage")
    if has_next and len(nodes) >= max_items:
        sys.stderr.write(
            "warning: Projects v2 board #{} for {}/{} truncated at {} items "
            "(raise ACTIVITY_BOARD_MAX_ITEMS for full coverage).\n".format(
                board.get("number"), owner, repo, max_items))
    return nodes[:max_items]


def fetch_project_board(graphql, owner, repo, max_items=5000, ref_date=None):
    """Auto-discover + fetch EVERY maintained Projects v2 board the repo links, via
    the injected `graphql(query, variables)` callable, returning
    `(sprints, items_by_ref)` (see parse_project_board, which merges items across
    boards). Boards that are closed or stale (see `board_is_maintained`) are
    skipped with a warning so a deprecated-but-not-removed board can't pollute the
    result. Each kept board's items are paginated by NODE ID and capped per board
    at `max_items`. `ref_date` (the window's ref/`to` date) anchors the staleness
    check.

    Degrades CLEANLY to ({}, {}) on any error (a GitHubAPIError / SystemExit, a
    network failure, a null repository, or no linked/maintained board) — a board
    fetch must NEVER hard-fail the run. `graphql` is injectable so the suite tests
    this driver against fixtures with no network.
    """
    try:
        data = graphql(PROJECT_BOARD_QUERY,
                       {"owner": owner, "repo": repo, "cursor": None})
    except (SystemExit, Exception) as err:  # noqa: BLE001 — degrade, never hard-fail
        sys.stderr.write(
            f"warning: skipping Projects v2 board for {owner}/{repo}: {err}\n")
        return {}, {}

    repo_obj = (data or {}).get("repository")
    if not repo_obj:
        return {}, {}
    boards = (repo_obj.get("projectsV2") or {}).get("nodes") or []
    # Partial GraphQL data (graphql_post keeps `data` alongside `errors`) leaves a
    # null node for a board this token cannot read: drop it, keep the others.
    unreadable = sum(1 for b in boards if not isinstance(b, dict))
    if unreadable:
        sys.stderr.write(
            "warning: {} Projects v2 board(s) for {}/{} could not be read; "
            "ingesting the rest.\n".format(unreadable, owner, repo))
        boards = [b for b in boards if isinstance(b, dict)]
    if not boards:
        return {}, {}

    # Deterministic merge order: ascending board number (then id). Drop closed /
    # stale boards (warning each) so deprecated-but-not-removed boards are ignored.
    boards = sorted(
        boards, key=lambda b: (b.get("number") is None, b.get("number") or 0,
                               b.get("id") or ""))
    kept = []
    for b in boards:
        if board_is_maintained(b, ref_date):
            kept.append(b)
        else:
            reason = "closed" if b.get("closed") else "stale (updatedAt {})".format(
                b.get("updatedAt"))
            sys.stderr.write(
                "warning: skipping {} Projects v2 board #{} ({}) for {}/{}.\n".format(
                    reason, b.get("number"), b.get("title"), owner, repo))
    if not kept:
        return {}, {}

    # Fetch each maintained board fully (per-board node-id pagination), then merge.
    full_boards = []
    for b in kept:
        nodes = _paginate_board_items(graphql, b, owner, repo, max_items)
        fb = dict(b)
        fb["items"] = {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": nodes}
        full_boards.append(fb)
    return parse_project_board(full_boards)


def _next_link(link_header):
    """Parse a GitHub Link header, returning the rel="next" url or None."""
    for part in (link_header or "").split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if 'rel="next"' in section[1]:
            return url
    return None


def _paginated(token):
    """Adapter: turn http_get_json into the (items, next) shape fetch_all wants."""
    def get_page(url):
        return http_get_json(url, token)
    return get_page


# GitHub documents that `actions/runs` "will return up to 1,000 results for each
# search" when filtered by `created` (also actor/branch/event/status/...). A busy
# repo can exceed that in one month, so a capped range is split by day and each
# half re-queried; only a SINGLE day still over the cap is left truncated, and
# then it is VISIBLE: a stderr warning plus a marker in workflow_stats.
WORKFLOW_RUNS_SEARCH_CAP = 1000
# Reserved key in the name-keyed workflow_stats map (no real workflow is named
# this); present ONLY when the runs were truncated, so normal stats are unchanged.
WORKFLOW_STATS_TRUNCATED_KEY = "_truncated"


def _split_created_range(lo, hi):
    """Split an inclusive created=lo..hi range into two halves by day, or None
    when it is a single day (or the dates do not parse). Keeps the original
    outer bounds; inner bounds use the same form (date or UTC timestamp)."""
    try:
        lo_d = datetime.date.fromisoformat(str(lo)[:10])
        hi_d = datetime.date.fromisoformat(str(hi)[:10])
    except (TypeError, ValueError):
        return None
    if lo_d >= hi_d:
        return None
    mid = lo_d + datetime.timedelta(days=(hi_d - lo_d).days // 2)
    nxt = mid + datetime.timedelta(days=1)
    iso = "T" in str(lo) or "T" in str(hi)
    left_hi = f"{mid.isoformat()}T23:59:59Z" if iso else mid.isoformat()
    right_lo = f"{nxt.isoformat()}T00:00:00Z" if iso else nxt.isoformat()
    return (lo, left_hi), (right_lo, hi)


def fetch_workflow_runs(get_page, api, frm, to, cap=WORKFLOW_RUNS_SEARCH_CAP):
    """Walk actions/runs for created=frm..to without silently hitting GitHub's
    per-search result cap. Returns (runs, truncation) where truncation is None
    when complete, else {"truncated": True, "cap", "fetched", "total_count",
    "ranges"} (also warned on stderr).

    The first page's `total_count` decides: at/over the cap and spanning more
    than one day, the range is split by day (newest half first, matching the
    API's created-desc order) WITHOUT paging further. A leaf range is flagged
    when it fetched fewer runs than its `total_count`, or reached the cap (a
    `total_count` that is itself capped cannot prove completeness)."""
    capped, top_total = [], []

    def walk(lo, hi):
        url = f"{api}/actions/runs?created={lo}..{hi}&per_page=100"
        payload, url = get_page(url)
        total = payload.get("total_count") if isinstance(payload, dict) else None
        total = total if isinstance(total, int) else None
        if not top_total:
            top_total.append(total)
        if total is not None and total >= cap:
            halves = _split_created_range(lo, hi)
            if halves:
                (l_lo, l_hi), (r_lo, r_hi) = halves
                return walk(r_lo, r_hi) + walk(l_lo, l_hi)
        runs = []
        while True:
            page = (payload.get("workflow_runs", []) if isinstance(payload, dict)
                    else payload) or []
            runs.extend(page)
            # The `created=` filter bounds the range server-side; this per-page
            # `< lo` check is a defensive early-exit guard.
            if not url or (page and page[-1].get("created_at", "")[:10] < str(lo)[:10]):
                break
            payload, url = get_page(url)
        if len(runs) >= cap or (total is not None and len(runs) < total):
            capped.append({"range": f"{lo}..{hi}", "fetched": len(runs),
                           "total_count": total})
        return runs

    raw = walk(frm, to)
    seen, runs = set(), []
    for r in raw:
        rid = r.get("id") if isinstance(r, dict) else None
        if rid is not None:
            if rid in seen:
                continue
            seen.add(rid)
        runs.append(r)
    if not capped:
        return runs, None
    truncation = {"truncated": True, "cap": cap, "fetched": len(runs),
                  "total_count": top_total[0] if top_total else None,
                  "ranges": [c["range"] for c in capped]}
    sys.stderr.write(
        "warning: workflow runs for {}..{} are TRUNCATED: GitHub returns at most "
        "{} runs per search and {} still hit that cap after splitting by day "
        "(fetched {} of {} reported). workflow_stats is marked {!r}.\n".format(
            frm, to, cap, ", ".join(truncation["ranges"]), len(runs),
            truncation["total_count"] if truncation["total_count"] is not None
            else "unknown", WORKFLOW_STATS_TRUNCATED_KEY))
    return runs, truncation


def mark_workflow_stats_truncated(stats, truncation):
    """Return workflow_stats with the truncation marker added (if any)."""
    if not truncation:
        return stats
    out = dict(stats or {})
    out[WORKFLOW_STATS_TRUNCATED_KEY] = dict(truncation)
    return out


def _runs_page(get_page, api, frm, to):
    """Walk actions/runs (pages wrap the list under `workflow_runs`). Thin
    wrapper over fetch_workflow_runs that drops the truncation record."""
    return fetch_workflow_runs(get_page, api, frm, to)[0]


def _attach_timeline_xrefs(pr, timeline, current_slug):
    """Set pr['timeline_xrefs'] to the cross-repo timeline refs, but ONLY when
    there are any (conditional key: a PR with no cross-repo refs keeps its exact
    prior shape, so single-repo gathers stay byte-identical). Mutates pr."""
    xrefs = parse_timeline_xrefs(timeline, current_slug)
    if xrefs:
        pr["timeline_xrefs"] = xrefs


def dedupe_raw_prs(raws):
    """One raw PR per number (issue #56 item 7). A page shift during
    `sort=updated` paging — or a PR that changed state between the closed and
    open fetches — can list the same PR twice. Keeps the snapshot with the
    latest `updated_at` (ties: the first seen), in first-appearance order, so
    the result is deterministic. Pure."""
    best, order = {}, []
    for raw in raws or []:
        n = raw["number"]
        if n not in best:
            order.append(n)
            best[n] = raw
        elif (raw.get("updated_at") or "") > (best[n].get("updated_at") or ""):
            best[n] = raw
    return [best[n] for n in order]


def enrich_pr(pr, get_page, api, current_slug):
    """Per-PR enrichment for a kept PR (mutates `pr`): reviews, issue timeline
    (crossrefs, cross-repo xrefs, lifecycle), review comments and conversation
    comments — four paginated fetches via `get_page`, no more.

    The list-pulls payload `normalize_pr` sees omits `merged_by`, `comments` and
    `review_comments` (only GET /pulls/{n} has them, issue #61). Rather than one
    extra detail call per PR, they are derived from what is fetched here:
    `merged_by` from the timeline `merged` event actor (only when the PR is
    merged and the payload did not already name the merger), and both counts
    from the lengths of the fully-paginated comment lists."""
    num = pr["number"]
    reviews = fetch_all(get_page, f"{api}/pulls/{num}/reviews?per_page=100")
    summary = summarize_reviews(reviews)
    pr["reviewers"] = summary["reviewers"]
    pr["review_decision"] = summary["decision"]
    # Phase 10 slice 1: KEEP the individual review submissions (fold persists
    # them as `review` social nodes). Only set the key when non-empty so the
    # bundle/extract stay byte-stable for PRs with no reviews.
    submissions = [normalize_review(r) for r in reviews]
    if submissions:
        pr["reviews"] = submissions
    timeline = fetch_all(get_page, f"{api}/issues/{num}/timeline?per_page=100")
    # Keep the refs normalize_pr seeded from the body (list tails such as the #2
    # in "Fixes #1, #2": mentioned, not closed) and add the timeline's, in order.
    seeded = list(pr.get("crossref_issues") or [])
    pr["crossref_issues"] = seeded + [
        n for n in parse_timeline_crossrefs(timeline) if n not in seeded]
    _attach_timeline_xrefs(pr, timeline, current_slug)
    # Phase 10 slice 1: allowlisted lifecycle events (reopened/closed/
    # ready_for_review). Omit-when-empty for byte stability.
    lifecycle = parse_timeline_lifecycle(timeline)
    if lifecycle:
        pr["lifecycle"] = lifecycle
    if pr.get("merged") and not pr.get("merged_by"):
        pr["merged_by"] = parse_timeline_merged_by(timeline)
    review_comments = fetch_all(get_page, f"{api}/pulls/{num}/comments?per_page=100")
    pr["review_comments"] = [normalize_review_comment(c) for c in review_comments]
    conv_comments = fetch_all(get_page, f"{api}/issues/{num}/comments?per_page=100")
    pr["comments_list"] = [normalize_comment(c) for c in conv_comments]
    pr["comments"] = len(pr["comments_list"])
    pr["review_comments_count"] = len(pr["review_comments"])
    return pr


def _github_remote_url(owner, repo):
    """The clone/fetch URL for a member repo (a seam so tests can use file://)."""
    return f"https://github.com/{owner}/{repo}.git"


def acquire(args, env):
    if not (args.owner and args.repo and getattr(args, "from") and args.to):
        sys.stderr.write("error: --owner --repo --from --to are required\n")
        raise SystemExit(2)
    token = resolve_token(env)
    owner, repo = args.owner, args.repo
    frm, to = getattr(args, "from"), args.to
    clone_dir = args.clone_dir or f"workspace/{repo}-clone"
    repo_url = _github_remote_url(owner, repo)

    # Clone fresh, or fetch an existing clone of the same remote; a quiet repo
    # (nothing since `frm - margin`) falls back to --depth=1 (see prepare_clone).
    clone_mode = prepare_clone(repo_url, frm, clone_dir, no_clone=args.no_clone)
    if clone_mode == "depth1-fallback":
        sys.stderr.write(
            f"note: {owner}/{repo} has no commits since the clone bound "
            f"(--from minus {_clone_margin_days()} days); cloned at --depth=1 and "
            f"treating the commit walks as empty (meta.clone_mode=depth1-fallback)\n")
    # All three git walks need real window history on disk: skipped (empty) when
    # there is no clone (--no-clone with nothing on disk) or only the depth-1 tip
    # of a quiet repo (whose whole-tree diff is not window activity).
    walk_clone = clone_mode in ("fresh", "fetched") and os.path.isdir(clone_dir)
    if walk_clone:
        # An in-window commit on the shallow boundary (no commit in the margin
        # before `frm`) would diff as the whole tree: deepen so its parent is
        # present and its diff is real (#68). Leftovers are handled after the walks.
        deepen_window_boundary(clone_dir, frm, to)

    # Phase 1 walks the checked-out default branch only; `args.branches` is
    # recorded in meta for provenance but not yet applied to the log/clone.
    # Multi-branch commit walking arrives in a later phase.
    # The shallow clone already bounds history to ~CLONE_MARGIN_DAYS before `frm`;
    # we walk it fully and window in Python (window_records) rather than with git's
    # `--since/--until`, which prunes valid in-window commits across timezones.
    commits = []
    if walk_clone:
        raw = run_git(["git", "-C", clone_dir, "log", *COMMIT_WALK_ARGS])
        commits = window_records(parse_git_log(raw), frm, to)

    # Phase 3a: full-window file-level code-event walk (--name-status -M -C).
    # Guarded (walk_clone) so a missing clone degrades gracefully to empty.
    # `--reverse` so events arrive OLDEST→NEWEST: build_artifacts appends lifecycle
    # in walk order and derives status from the last (newest) event.
    code_events = []
    if walk_clone:
        raw_walk = run_git(["git", "-C", clone_dir, "log", *CODE_WALK_ARGS])
        code_events = window_records(parse_code_events(raw_walk), frm, to)

    # Phase 3d: symbol-granular change attribution via a single `git log -p` walk
    # (diff-local, no per-commit checkout). Same guard/degradation + oldest→newest
    # ordering (--reverse) as the file walk so symbol lifecycle/status is correct.
    symbol_events = []
    if walk_clone:
        raw_patch = run_git(["git", "-C", clone_dir, "log", *PATCH_WALK_ARGS])
        # Single pass over the patch text yields BOTH symbol events and the
        # Phase 10 slice-diffs hunks (no double-parse). The bounded, language-agnostic
        # unified-diff per changed (commit, path) comes from the SAME `git log -p`
        # walk (no extra git/network); attach it onto the matching file-level
        # code_event so it rides code_events -> file artifact -> feature_deltas and
        # persists in the ledger. Omit-when-empty for byte-stability.
        raw_symbol_events, file_diffs = parse_patch_events(raw_patch)
        symbol_events = window_records(raw_symbol_events, frm, to)
        attach_file_diffs(code_events, file_diffs)

    # Fallback when deepening could not resolve it: drop any shallow-boundary
    # commit's whole-tree phantom (its parent was grafted away by the bounded clone)
    # from BOTH walks AND clear its commit file list, so area_paths / modules /
    # touches / train code_areas are not inflated either (#68).
    _boundary = shallow_boundary_shas(clone_dir)
    code_events = drop_boundary_events(code_events, _boundary)
    symbol_events = drop_boundary_events(symbol_events, _boundary)
    _boundary_dropped = in_window_boundary_commits(_boundary, commits)
    for c in commits:
        if c.get("sha") in _boundary:
            c["files"] = []
    if _boundary_dropped:
        sys.stderr.write(
            f"warning: {len(_boundary_dropped)} in-window commit(s) sit at the shallow "
            f"clone boundary and deepening the clone did not resolve it; their "
            f"whole-tree phantom diffs and file lists were dropped (a visible gap in "
            f"meta.boundary_dropped_commits) — widen the clone margin "
            f"(ACTIVITY_CLONE_MARGIN_DAYS, default {CLONE_MARGIN_DAYS}) and re-gather "
            f"to recover: {_boundary_dropped}\n")

    # Phase 3b: code-area provider (directory-first; graphify optional). Paths come
    # from the code-event walk + the commit file lists (local, zero-token).
    area_paths = sorted(
        {e["path"] for e in code_events}
        | {e["old_path"] for e in code_events if e.get("old_path")}
        | {f for c in commits for f in c.get("files", [])})
    code_graph = select_code_area_provider(area_paths, clone_dir)

    # Phase 3c: enrich area edges via the real IaC toolchain (BUILD-ONLY — edges
    # stay [] when bicep/terraform or the registry are unavailable). No-op without
    # a clone on disk.
    if not args.no_clone or os.path.isdir(clone_dir):
        code_graph = extract_iac_edges(code_graph, clone_dir)
        # Multi-repo (project) gathers additionally fold the WHOLE module tree's
        # static `source` deps (no terraform init) so the cross-repo blast-radius
        # graph is structural, not limited to in-window-changed areas. Gated off the
        # manifest path: single-repo bundles stay byte-stable (golden-bundle gate).
        if getattr(args, "structural_iac", False) and os.path.isdir(clone_dir):
            code_graph = merge_structural_areas(
                code_graph, scan_structural_terraform_areas(clone_dir))

    # CODEOWNERS from the clone (local file; try the conventional locations).
    code_owners = {}
    for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        p = os.path.join(clone_dir, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                code_owners = parse_codeowners(fh.read())
            break

    get_page = _paginated(token)
    api = f"https://api.github.com/repos/{owner}/{repo}"
    # Closed PRs (merged + rejected) bounded by update time, as in Phase 1...
    raw_closed = fetch_until(
        get_page,
        f"{api}/pulls?state=closed&sort=updated&direction=desc&per_page=100",
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm[:10],
    )
    # ...plus all currently-open PRs (these feed in_flight / next_candidates).
    raw_open = fetch_all(
        get_page, f"{api}/pulls?state=open&sort=updated&direction=desc&per_page=100")

    prs = []
    for raw in dedupe_raw_prs(raw_closed + raw_open):
        pr = normalize_pr(raw)
        # Keep: merged-in-window, closed-unmerged-in-window, or any open PR.
        keep = (
            (pr["merged"] and in_window(pr["merged_at"], frm, to))
            or (pr["state"] == "closed" and not pr["merged"]
                and in_window(pr["closed_at"], frm, to))
            or pr["state"] == "open"
        )
        if not keep:
            continue
        enrich_pr(pr, get_page, api, f"{owner}/{repo}")
        prs.append(pr)

    # Issue set: every issue a kept PR closes or cross-references, plus open and
    # recently-closed issues (for in_flight / next_candidates / rejected buckets).
    wanted = set()
    for pr in prs:
        wanted.update(pr["closes"])
        wanted.update(pr["crossref_issues"])
    raw_repo_issues = fetch_until(
        get_page,
        f"{api}/issues?state=all&sort=updated&direction=desc&per_page=100",
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm[:10],
    )
    # Open issues untouched during the window are still relevant to the in_flight
    # / next_candidates buckets (e.g. milestone-scoped work that hasn't moved), so
    # pull every open issue too — bounded only by state, like the open PRs above —
    # and de-duplicate against the recently-updated set.
    raw_open_issues = fetch_all(
        get_page, f"{api}/issues?state=open&sort=updated&direction=desc&per_page=100")
    issues = []
    seen = set()
    raw_by_num = {}
    for raw in raw_repo_issues + raw_open_issues:
        if raw.get("pull_request"):  # the issues endpoint also lists PRs; skip them
            continue
        if raw["number"] in seen:
            continue
        issue = normalize_issue(raw)
        raw_by_num[raw["number"]] = raw
        seen.add(issue["number"])
        issues.append(issue)
    for n in sorted(wanted - seen):
        # A closed/cross-referenced issue can 404 — deleted, transferred to another
        # repo, or a cross-ref number that is not an issue in THIS repo. Tolerate it
        # (skip) rather than aborting the whole gather on one dangling reference.
        raw_issue, _ = http_get_json(f"{api}/issues/{n}", token, allow_404=True)
        if raw_issue is None or raw_issue.get("pull_request"):
            continue
        raw_by_num[raw_issue["number"]] = raw_issue
        issues.append(normalize_issue(raw_issue))

    for issue in issues:
        n = issue["number"]
        conv = fetch_all(get_page, f"{api}/issues/{n}/comments?per_page=100")
        issue["comments_list"] = [normalize_comment(c) for c in conv]
        # Phase 10 slice 1: allowlisted lifecycle events for issues (reopened in
        # particular drives reopen_count). Omit-when-empty for byte stability.
        issue_timeline = fetch_all(
            get_page, f"{api}/issues/{n}/timeline?per_page=100")
        issue_lifecycle = parse_timeline_lifecycle(issue_timeline)
        if issue_lifecycle:
            issue["lifecycle"] = issue_lifecycle
        issue["reactions"] = summarize_reactions(raw_by_num.get(n, {}).get("reactions"))
        issue["open_high_activity"] = derive_open_high_activity(issue)
        issue["issue_type"] = (raw_by_num.get(n, {}).get("type") or {}).get("name") \
            if isinstance(raw_by_num.get(n, {}).get("type"), dict) else None

    # Phase 3b: label taxonomy over every repo label seen, then stamp facets + kind.
    all_labels = sorted({lbl for it in prs + issues for lbl in (it.get("labels") or [])})
    label_taxonomy = detect_label_taxonomy(all_labels)
    # Derive from the issues already fetched (native `type` captured above) rather
    # than a separate org-scoped probe — no extra call, always consistent with data.
    types_present = any(i.get("issue_type") for i in issues)
    for pr in prs:
        pr["facets"] = apply_facets(pr, label_taxonomy)
    for issue in issues:
        issue["facets"] = apply_facets(issue, label_taxonomy)
        # native issue type when the repo uses them (acquire fetches it per issue
        # in the existing issue loop — see Step 3a); the classifier is pure.
        issue["kind"] = classify_issue_kind(issue, label_taxonomy, types_present)

    # People identity (#78): resolve each commit author (git name) to the GitHub
    # login PR/issue/review records use, so one person is one key. One paginated
    # commits-list listing (ceil(commits / 100) REST calls, none for an empty
    # window) joined on SHA; then the noreply email; else flagged unresolved.
    # Code/symbol events are re-keyed to the same author.
    if commits:
        head = clone_head_sha(clone_dir)
        api_items = fetch_all(get_page, commits_api_url(api, head, frm, to)) \
            if head else []
        known = set()
        for it in prs + issues:
            known.add(it.get("author"))
            known.add(it.get("merged_by"))
            known.update(it.get("reviewers") or [])
            for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
                known.add(c.get("author"))
        known.discard(None)
        by_sha = resolve_commit_authors(
            commits, commit_logins_from_api(api_items), known)
        remap_event_authors(code_events, by_sha)
        remap_event_authors(symbol_events, by_sha)

    workflows = []
    workflow_stats = {}
    if getattr(args, "include_workflows", True):
        raw_runs, runs_truncation = fetch_workflow_runs(get_page, api, frm, to)
        workflows = [normalize_workflow(r) for r in raw_runs]
        workflow_stats = mark_workflow_stats_truncated(
            aggregate_workflow_stats(workflows), runs_truncation)

    releases = []
    if getattr(args, "include_releases", True):
        raw_releases = fetch_all(get_page, f"{api}/releases?per_page=100")
        releases = [normalize_release(r) for r in raw_releases
                    if in_window(r.get("published_at"), frm, to)]

    raw_milestones = fetch_all(get_page, f"{api}/milestones?state=all&per_page=100")
    milestones = [normalize_milestone(m) for m in raw_milestones]

    # Phase 12: Projects v2 board. Auto-discover via GraphQL (the only GraphQL call
    # in gather), stamp each gathered pr/issue with its board_status + iteration,
    # and surface the (date-windowed) sprints. Degrades to empty on any error so a
    # missing read:project scope NEVER hard-fails the run; --no-project-board skips
    # the query entirely.
    sprints = {}
    if getattr(args, "project_board", True):
        def _graphql(query, variables=None):
            return graphql_post(token, query, variables)
        # ref_date (the window's reference point) anchors the board staleness
        # check; it is finalized below but resolves to the same value here.
        sprints, items_by_ref = fetch_project_board(
            _graphql, owner, repo, max_items=_board_max_items(),
            ref_date=getattr(args, "ref_date", None) or to)
        # Window-bound iterations to [frm, to] by date: keep a sprint that overlaps
        # the window at all (start <= to and end >= frm). A sprint with no dates is
        # kept (can't exclude it). Drop the iteration stamp on items whose sprint
        # was windowed out so fold leaves no dangling in_iteration edge.
        kept = {}
        to10, frm10 = (to or "")[:10], (frm or "")[:10]
        for sid, s in sprints.items():
            start, end = s.get("start"), s.get("end")
            overlaps = ((start is None or start[:10] <= to10)
                        and (end is None or end[:10] >= frm10))
            if overlaps:
                kept[sid] = s
        sprints = kept
        board = _board_index(items_by_ref, f"{owner}/{repo}")
        for it in prs + issues:
            facts = board.get(it.get("number"))
            if not facts:
                continue
            if facts.get("status") is not None:
                it["board_status"] = facts["status"]
            sid = facts.get("sprint_id")
            if sid is not None and sid in sprints:
                it["iteration"] = sid

    ref_date = getattr(args, "ref_date", None) or to
    meta = {
        "owner": owner, "repo": repo, "from": frm, "to": to,
        "branches": args.branches.split(","), "clone_dir": clone_dir,
        # the analyzed mainline branch: PRs merged into other branches are stacked/
        # fork contributions, not shipped-to-main (link.build_trains keys on this).
        "base_branch": args.branches.split(",")[0],
        "ref_date": ref_date, "clone_sha": clone_head_sha(clone_dir),
        "clone_mode": clone_mode,
        "boundary_dropped_commits": _boundary_dropped,
        "period": {"from": frm, "to": to}, "prev_bundle": None,
    }
    bundle = build_bundle(meta, commits, prs, issues)
    bundle["workflows"] = workflows
    bundle["workflow_stats"] = workflow_stats
    bundle["releases"] = releases
    bundle["milestones"] = milestones
    bundle["code_events"] = code_events
    bundle["symbol_events"] = symbol_events
    bundle["code_graph"] = code_graph
    bundle["code_owners"] = code_owners
    bundle["label_taxonomy"] = label_taxonomy
    bundle["sprints"] = sprints
    return bundle


# --- Backfill (slice 7c): on-demand single-node fetch on a traversal MISS -----
#
# A reader (extract) traversing a decision train over the spine edges can hit an
# edge pointing at a node never gathered (e.g. a windowed PR `closes` an issue
# opened before the window) — a `missing` id. `backfill` fetches THAT ONE node +
# its cheap immediate spine edges and upserts it. It is the ONLY network call
# outside the main Acquire pass, so it lives here in gather.
#
# The actual fetch is SEAMED behind an injectable `fetch` callable so the test
# suite never touches the network: tests pass a fixture-backed fake; production
# passes `make_backfill_fetcher(token)` (REST for social, local/`git fetch
# --depth 1` for code). `fetch(kind, local, qid)` returns either None (genuinely
# unfetchable) or `{"node": <raw record dict>, "edges": [(dst_local, edge_type),
# ...]}`. backfill shapes the result into store node/edge upserts, reusing the
# same id/edge conventions fold_bundle uses.

def classify_id(qid):
    """Classify a qualified node id into its node_class + a fetch `kind`, from
    the LOCAL id form (mirrors fold_bundle's id conventions):

      social: `pr-<n>` / `issue-<n>` / `comment-<id>` / `review-…` / `event-…`
      code:   a bare `<sha>` commit (kind `code`), or an artifact — `art:<path>`
              or `<path>#<lang>:<subkind>:<name>` (graphstore.is_artifact_local) —
              stored as `code` but given kind `artifact`: a substrate detail,
              never a spine target, and never fetched as a sha.
      structure: everything else (milestones/releases/areas/singletons/person-)

    `local` is the FULL local id (graphstore.local_id: everything after the
    first `#`), so a symbol id keeps its `<path>#` part.
    Returns {"node_class", "kind", "local"}.
    """
    local = graphstore.local_id(qid)
    # Artifact form first: a symbol's PATH may start with a prefix tested below
    # (`issue-templates/main.tf#terraform:…`).
    if graphstore.is_artifact_local(local):
        kind, node_class = "artifact", "code"
    elif local.startswith(("pr-", "issue-", "comment-", "review-", "event-")):
        kind, node_class = "social", "social"
    elif local.startswith(("milestone-", "release-", "area-", "person-",
                           "sprint-")) \
            or local in ("workflowstats", "codegraph", "codeowners",
                         "labeltaxonomy"):
        kind, node_class = "structure", "structure"
    else:
        kind, node_class = "code", "code"  # bare <sha> commit
    return {"node_class": node_class, "kind": kind, "local": local}


# Returned by a `fetch` seam to mean "this id DEFINITIVELY does not exist" (a
# 404) — distinct from None ("couldn't reach it / transient"). backfill prunes
# an ABSENT id by tombstoning it via graphstore.record_dead_ref.
ABSENT = object()


def backfill(conn, id, fetch=None):
    """Fetch and upsert ONE missing spine node + its cheap immediate edges.

    Idempotent: if `get_node(id)` already returns a node, this is a no-op and
    NOTHING is fetched. Otherwise it classifies `id`, calls the injected `fetch`
    seam for that one node, and (when the fetch yields a record) upserts the node
    plus any immediate spine edges the seam returned.

    `fetch(kind, local, qid)` is the network seam with THREE outcomes:
      - a dict `{"node": raw, "edges": [(dst_local, edge_type), ...]}` == fetched;
      - `gather.ABSENT` == the id DEFINITIVELY does not exist (a 404) —
        `backfill` tombstones it via `graphstore.record_dead_ref` so it is
        pruned and never re-chased;
      - `None` == transient/unreachable (could not be resolved this run).
    Tests pass a fixture-backed fake; production passes `make_backfill_fetcher`.

    Returns `{"fetched": bool, "absent": bool, "id": id, "edges_added": int}` —
    `absent=True` only on the ABSENT outcome.
    """
    if graphstore.get_node(conn, id) is not None:
        return {"fetched": False, "absent": False, "id": id,
                "edges_added": 0}  # already present

    info = classify_id(id)
    parsed = graphstore.parse_id(id)
    # The scope is `{project}/{repo}`; recover project/repo so the upsert lands
    # in the same identity columns fold_bundle uses.
    project, _, repo = parsed["scope"].partition("/")

    if fetch is None:
        raise ValueError("backfill needs a `fetch` seam (production: "
                         "make_backfill_fetcher(token); tests: a fake)")

    result = fetch(info["kind"], info["local"], id)
    if result is ABSENT:
        graphstore.record_dead_ref(conn, id)  # only gather writes
        return {"fetched": False, "absent": True, "id": id, "edges_added": 0}
    if not result or not result.get("node"):
        return {"fetched": False, "absent": False, "id": id,
                "edges_added": 0}  # unreachable / transient

    raw = result["node"]
    fetched_at = graphstore.now_iso()
    # ts mirrors fold_bundle's choice per class: social PR -> merged/closed/
    # created; social issue -> closed/updated; commit -> date; structure -> NULL.
    if info["kind"] == "social" and info["local"].startswith("pr-"):
        ts = raw.get("merged_at") or raw.get("closed_at") or raw.get("created_at")
    elif info["kind"] == "social" and info["local"].startswith("issue-"):
        ts = raw.get("closed_at") or raw.get("updated_at")
    elif info["kind"] == "code":
        ts = raw.get("date")
    else:
        ts = raw.get("published_at")  # release; milestone/area stay None

    graphstore.upsert_node(
        conn, id, project, repo, info["node_class"], ts, raw, fetched_at)

    # Cheap immediate spine edges the seam surfaced (e.g. issue#7 spun_off #5).
    # Only spine edge types are honored — backfill closes a TRAVERSAL gap, it does
    # not re-derive the whole non-spine substrate. Edges to still-absent nodes are
    # fine (a later traversal surfaces them as `missing` for another backfill).
    edges = []
    for dst_local, edge_type in result.get("edges") or []:
        if edge_type not in graphstore.SPINE_EDGE_TYPES:
            continue
        edges.append((id, graphstore.qualify_id(project, repo, dst_local),
                      edge_type, None, None))
    if edges:
        graphstore.upsert_edges(conn, edges)
    return {"fetched": True, "absent": False, "id": id, "edges_added": len(edges)}


def make_backfill_fetcher(token, clone_dir=None):
    """Production `fetch` seam for `backfill`: resolves one node by REST (social)
    or local git (code). Returns a `fetch(kind, local, qid)` callable.

    NOT unit-tested (it is the live-network edge); the suite exercises backfill
    with a fixture-backed fake instead. social -> GitHub REST (normalize_pr /
    normalize_issue, plus the same `closes` spine edges fold derives); code -> a
    bounded `git fetch --depth 1 <sha>` into the clone then a one-commit log;
    structure -> not backfilled on demand (graphify/whole-dict facts come from
    Acquire), so it returns None and the node stays a warned gap.

    A `Closes #N` whose #N resolves to a PR (issues + PRs share one number
    space) is followed to the real PR and returned as a node — the thread is
    traversed, not dropped. Outcomes: a fetched dict, `ABSENT` (a genuine 404),
    or `None` (transient / not resolvable this run, e.g. a code sha with no clone).
    """
    def fetch(kind, local, qid):
        parsed = graphstore.parse_id(qid)
        project, _, repo = parsed["scope"].partition("/")
        # Multi-repo (Phase 9) ids scope as `{project}/{owner}/{repo}`, so the
        # repo part is itself an `owner/repo` slug and addresses GitHub directly.
        # Single-repo ids scope as `{owner}/{repo}` (no inner slash), so fall back
        # to the legacy owner=project mapping. Without this, a multi-repo backfill
        # hits repos/{project}/{owner}/{repo} -> false 404s / dead_refs.
        slug = repo if "/" in repo else "{}/{}".format(project, repo)
        api = f"https://api.github.com/repos/{slug}"
        if kind == "social" and local.startswith("issue-"):
            num = local[len("issue-"):]
            raw, nxt = http_get_json(f"{api}/issues/{num}", token, allow_404=True)
            if raw is None and nxt == 404:
                return ABSENT
            if raw.get("pull_request"):
                # #N is actually a PR (issues + PRs share one number space). The
                # parsed `issue-N` is gather's mis-classification, not a phantom:
                # the PR is real, so follow the thread and resolve it to the PR.
                # It is returned under the referenced id so the existing `closes`
                # edge connects; its /pull/ html_url lets the reader label it a PR
                # and anchor the train's headline to in-window work, so an ancient
                # cross-ref is traversed as context without hijacking the title.
                pr_raw, pnxt = http_get_json(f"{api}/pulls/{num}", token,
                                             allow_404=True)
                if pr_raw is None and pnxt == 404:
                    return ABSENT
                pr = normalize_pr(pr_raw)
                edges = [("issue-{}".format(n), "closes")
                         for n in pr.get("closes") or []]
                return {"node": pr, "edges": edges}
            return {"node": normalize_issue(raw), "edges": []}
        if kind == "social" and local.startswith("pr-"):
            num = local[len("pr-"):]
            raw, nxt = http_get_json(f"{api}/pulls/{num}", token, allow_404=True)
            if raw is None and nxt == 404:
                return ABSENT
            pr = normalize_pr(raw)
            edges = [("issue-{}".format(n), "closes") for n in pr.get("closes") or []]
            return {"node": pr, "edges": edges}
        if kind == "code" and clone_dir:
            sha = local
            try:
                run_git(["git", "-C", clone_dir, "fetch", "--depth", "1",
                         "origin", sha])
                raw = run_git(["git", "-C", clone_dir, "log", "-1",
                               f"--pretty=format:{CODE_LOG_FORMAT}",
                               "--date=short", "--name-only", sha])
            except Exception:
                return None
            commits = parse_git_log(raw)
            if not commits:
                return None
            c = commits[0]
            prn = resolve_commit_pr(c.get("message", ""))
            edges = [("pr-{}".format(prn), "part_of")] if prn is not None else []
            return {"node": c, "edges": edges}
        return None  # structure / unfetchable on demand

    return fetch


def _social_text(item, comments):
    """The full searchable text for a PR/issue social node: its title + body,
    then each embedded comment/review author + body. Newline-joined so FTS5
    tokenizes phrases across the parts. Pure; missing fields are skipped."""
    parts = [item.get("title") or "", item.get("body") or ""]
    for c in comments:
        author = c.get("author") or ""
        body = c.get("body") or ""
        if author or body:
            parts.append("{} {}".format(author, body).strip())
    return "\n".join(p for p in parts if p)


def _cross_repo_pr_edges(pr, project, current_repo, members):
    """Resolve a PR's cross-repo references (qualified body refs +
    repo-aware timeline xrefs) for a multi-repo project. Returns
    (external_refs, edges):
      - a ref to a MEMBER repo -> an edge tuple (src_pid, dst_id, kind, None, None)
        where kind is 'closes'/'cross_ref' and dst is the target repo's issue/pr;
      - a ref to a NON-member repo -> an external_refs dict {repo, number, kind}.
    Same-repo refs are skipped (the bare-#N path already covers them).

    Repo slugs compare case-insensitively (issue #70): a ref in any spelling of
    the current repo is same-repo, and a member ref's edge targets the member's
    CANONICAL (manifest) spelling, so ids never fork into two id spaces.
    External refs keep the spelling they were written with. Pure."""
    pid = graphstore.qualify_id(project, current_repo, "pr-{}".format(pr["number"]))
    text = (pr.get("title", "") or "") + "\n" + (pr.get("body") or "")
    refs = parse_qualified_refs(text) + list(pr.get("timeline_xrefs") or [])
    index = manifest_mod.slug_index(members)
    current = (current_repo or "").lower()
    edges, external, seen = [], [], set()
    for r in refs:
        slug = "{}/{}".format(r["owner"], r["repo"])
        if slug.lower() == current:
            continue
        local = ("pr-" if r.get("is_pr") else "issue-") + str(r["number"])
        key = (slug.lower(), local, r["kind"])
        if key in seen:
            continue
        seen.add(key)
        member = manifest_mod.canonical_slug(slug, index)
        if member is not None:
            edges.append((pid, graphstore.qualify_id(project, member, local),
                          r["kind"], None, None))
        else:
            external.append({"repo": slug, "number": r["number"], "kind": r["kind"]})
    return external, edges


# The member's root module area: a repo-root main.tf classifies to area "main.tf"
# (DEFAULT_AREA_PATTERNS / _tf_dir), so a cross-repo dep targets this node.
_ROOT_AREA_LOCAL = "area-{}".format(
    classify_code_area("main.tf", DEFAULT_AREA_PATTERNS))


def parse_registry_source(src):
    """Parse a Terraform registry module source `[host/]namespace/name/provider`
    (optional `//submodule` suffix) -> (namespace, name, provider), or None if it
    is not a registry source (local path, git/http url, or wrong shape). Pure."""
    if not src or src.startswith((".", "/")) or "://" in src:
        return None
    core = src.split("//", 1)[0]                       # drop submodule path
    parts = [p for p in core.split("/") if p]
    if len(parts) == 4 and "." in parts[0]:            # strip a registry host (4-seg form: host/ns/name/provider)
        parts = parts[1:]
    if len(parts) != 3:
        return None
    # A dot in the namespace means the first segment is a HOST, not a namespace —
    # i.e. VCS shorthand like `github.com/org/repo`, not a registry address. A real
    # registry namespace never contains a dot, so reject it (don't mis-resolve it
    # via the naming convention). Registry hosts are only valid in the 4-seg form
    # handled above.
    if "." in parts[0]:
        return None
    return parts[0], parts[1], parts[2]


def resolve_registry_member(src, project, members, registry_by_slug):
    """Resolve a registry source to a member's root-area qualified id, or None.
    Exact (the manifest `registry` names the same module as `src`) wins over the
    HashiCorp naming convention (`namespace/name/provider` ->
    `{namespace}/terraform-{provider}-{name}`).

    Both sides are normalised (issue #70) through manifest.registry_identity:
    `//submodule` dropped, case-insensitive, an omitted host equal to
    `registry.terraform.io`. Any other host is a different registry: it only
    matches a member declared with that same host, and never the convention. A manifest
    `registry` value that does not parse as a registry address still matches an
    identical (case-insensitive) raw `src`. The returned id uses the member's
    manifest spelling. Pure, deterministic (members tried in sorted order)."""
    if not src:
        return None
    registry_by_slug = registry_by_slug or {}
    want = manifest_mod.registry_identity(src)
    for slug in sorted(members):                       # exact, deterministic
        reg = registry_by_slug.get(slug)
        if not reg:
            continue
        reg_id = manifest_mod.registry_identity(reg)
        if reg_id is not None:
            if reg_id == want:
                return graphstore.qualify_id(project, slug, _ROOT_AREA_LOCAL)
        elif reg.lower() == src.lower():
            return graphstore.qualify_id(project, slug, _ROOT_AREA_LOCAL)
    # The naming convention maps a PUBLIC registry address to a GitHub repo; a
    # module on another registry host says nothing about a member repo.
    if want is None or want[0] != manifest_mod.DEFAULT_REGISTRY_HOST:
        return None
    namespace, name, provider = parse_registry_source(src)
    member = manifest_mod.canonical_slug(
        "{}/terraform-{}-{}".format(namespace, provider, name),
        manifest_mod.slug_index(members))
    if member is not None:
        return graphstore.qualify_id(project, member, _ROOT_AREA_LOCAL)
    return None


def _fold_depends_on(bundle, project, repo, members, registry_by_slug):
    """Yield (src_qid, dst_qid, data) depends_on triples from a bundle's area
    edges. Resolved-local edges -> intra-repo. Unresolved registry edges resolve
    to a member's root area ONLY in multi-repo folds (`members` non-empty and
    `registry_by_slug` not None); single-repo folds skip them (byte-stable).
    `examples/`/`tests/` source areas are skipped (scaffolding, not module deps). Pure."""
    def q(slug, local):
        return graphstore.qualify_id(project, slug, local)
    areas = (bundle.get("code_graph", {}) or {}).get("areas") or []
    for area in areas:
        if _is_scaffold_area(area.get("id")):
            continue
        src = q(repo, "area-{}".format(area["id"]))
        for e in area.get("edges") or []:
            to = e.get("to")
            if e.get("resolved") and to is not None:
                dst = q(repo, "area-{}".format(to))
                data = {k: v for k, v in e.items() if k != "to"}
            elif (not e.get("resolved") and members and registry_by_slug is not None
                  and e.get("ref")):
                dst = resolve_registry_member(e["ref"], project, members,
                                              registry_by_slug)
                if dst is None:
                    continue
                data = {k: v for k, v in e.items() if k != "to"}
                data["resolved"] = True
                data["cross_repo"] = True
            else:
                continue
            yield src, dst, (data or None)


def _lifecycle_event_data(ev, parent_url, parent_local):
    """Build a lifecycle-event node's `data` blob, ensuring a citable `url`.

    Provenance (spec): the event's own `url` when present, else a stable
    synthesized `<parent html_url>#event-<id>` (falling back to the parent's
    local id when the parent carries no url) so check_provenance always passes.
    The stored blob carries {actor, event, created_at, label, url}."""
    url = ev.get("url")
    if not url:
        base = parent_url or parent_local
        url = "{}#event-{}".format(base, ev.get("id"))
    # `id` is kept in the blob so extract round-trips it and a re-fold rebuilds the
    # SAME `event-<parent>-<id>` node id (idempotency).
    return {
        "id": ev.get("id"),
        "actor": ev.get("actor"),
        "event": ev.get("event"),
        "created_at": ev.get("created_at"),
        "label": ev.get("label"),
        "url": url,
    }


def fold_bundle(conn, bundle, project=None, repo=None, members=None,
                registry_by_slug=None):
    """Fold a raw bundle into the journey-graph store by stable identity:
    upsert nodes, spine edges, and the file-level code-event ledger. Idempotent
    — re-folding an overlapping window mutates nothing already correct. See
    STORE.md for the schema and identity rules.

    Scope: social (prs/issues; comments/reviews stay embedded in the parent's
    data blob), code (commits + the artifact substrate) + the file-level and
    symbol code_events ledgers, structure (milestones/releases/areas + the
    project-scoped people nodes), the spine edges closes/cross_ref/part_of, and
    the non-spine contribution / owns / touches / depends_on / in_milestone /
    blocks / symbol-move edges — all derived on the write path via the `derive`
    leaf. See STORE.md for the full edge/node inventory and what remains
    (in_iteration — needs Projects v2 acquisition).
    """
    meta = bundle.get("meta", {})
    # Identity override (Phase 9): a multi-repo run folds each member under one
    # LOGICAL project (`project`) with `repo` = "owner/repo". Single-repo runs pass
    # neither and fall back to meta.owner/meta.repo — byte-identical to before.
    # `members` (a set/dict of "owner/repo" slugs) gates cross-repo edge emission;
    # None (single-repo) skips it entirely.
    if project is None:
        project = meta.get("owner")
    if repo is None:
        repo = meta.get("repo")
    if not project or not repo:
        raise ValueError("bundle meta needs owner and repo to qualify ids")

    fetched = graphstore.now_iso()
    nodes, edges, events = [], [], []

    def qid(local):
        return graphstore.qualify_id(project, repo, local)

    # social: PRs, with closes/cross_ref spine edges to their issues. In a
    # multi-repo project (members given) a PR's qualified refs + repo-aware
    # timeline xrefs also yield CROSS-repo spine edges (to member repos) or
    # honest external_refs (to non-members) — see _cross_repo_pr_edges. Gated on
    # `members is not None` so single-repo folds are byte-identical.
    for pr in bundle.get("prs", []):
        pid = qid("pr-{}".format(pr["number"]))
        ts = pr.get("merged_at") or pr.get("closed_at") or pr.get("created_at")
        if members is not None:
            external, xedges = _cross_repo_pr_edges(pr, project, repo, members)
            if external:
                pr = {**pr, "external_refs": external}
            edges.extend(xedges)
        nodes.append((pid, project, repo, "social", ts, pr, fetched))
        for n in pr.get("closes") or []:
            edges.append((pid, qid("issue-{}".format(n)), "closes", None, None))
        for n in pr.get("crossref_issues") or []:
            edges.append((pid, qid("issue-{}".format(n)), "cross_ref", None, None))

    # social: issues, with `blocks` (issue->issue) relation edges. A blocks ref to
    # an issue NOT gathered in this window is dropped rather than left dangling
    # (referential_integrity): only edges between two gathered issues are emitted.
    _gathered_issues = {i["number"] for i in bundle.get("issues", [])}
    for iss in bundle.get("issues", []):
        ts = iss.get("closed_at") or iss.get("updated_at")
        iid = qid("issue-{}".format(iss["number"]))
        nodes.append((iid, project, repo, "social", ts, iss, fetched))
        for b in iss.get("blocks") or []:
            if b["number"] not in _gathered_issues:
                continue
            other = qid("issue-{}".format(b["number"]))
            # normalize to blocker -> blocked
            src, dst = (iid, other) if b["direction"] == "out" else (other, iid)
            edges.append((src, dst, "blocks", None, None))

    # code: commits, with part_of spine edge to the PR named in the subject.
    for c in bundle.get("commits", []):
        cid = qid(c["sha"])
        nodes.append((cid, project, repo, "code", c.get("date"), c, fetched))
        prn = resolve_commit_pr(c.get("message", ""))
        if prn is not None:
            edges.append((cid, qid("pr-{}".format(prn)), "part_of", None, None))

    # code: file-level code-event ledger (rename/copy source -> detail).
    for ev in bundle.get("code_events", []):
        path = ev.get("path")
        if not path:
            continue
        events.append((
            qid(path), ev.get("change"), ev.get("commit"), ev.get("author"),
            ev.get("date"), ev.get("hunk"), None, None, None, ev.get("old_path"),
        ))

    # code: artifact substrate (Phase 7b-1 step 2). Derive the per-artifact
    # lifecycle ledger and symbol-identity moves from the raw bundle (pure, via
    # `derive`), then persist them additively alongside the commit `code` nodes
    # and the file-level ledger above. The artifact's own id form is the local
    # id: `art:<path>` for file artifacts, `<path>#<lang>:<subkind>:<name>` for
    # symbol/comment ones (see derive.build_artifacts).
    artifacts = derive.build_artifacts(bundle)
    derive.link_symbol_identity({**bundle, "artifacts": artifacts})
    for local, art in artifacts.items():
        # ts: the artifact's last lifecycle event date (its most-recent activity);
        # None when it has no events (degrades cleanly, excluded from window scans).
        lc = art.get("lifecycle") or []
        ts = lc[-1]["date"] if lc else None
        nodes.append((qid(local), project, repo, "code", ts, art, fetched))

    # code: symbol-granular lifecycle ledger, keyed by the SYMBOL artifact id,
    # carrying the rich before/after fields (file-level entries leave them NULL).
    for ev in bundle.get("symbol_events", []):
        local = "{}#{}:{}:{}".format(
            ev["path"], ev["lang"], ev["subkind"], ev["name"] or "")
        event = derive._SYMBOL_CHANGE_TO_EVENT.get(ev.get("change"), "change")
        events.append((
            qid(local), event, ev.get("commit"), ev.get("author"),
            ev.get("date"), None, None, ev.get("before"), ev.get("after"), None,
        ))

    # code: symbol-move edges (artifact -> artifact). Each confident move links
    # the source symbol `replaced_by` the dest and the dest `identity_from` the
    # source, both carrying move_confidence/move_basis. Idempotent by (src,dst,type).
    for m in derive.match_symbol_moves(
            bundle.get("symbol_events", []),
            [(e.get("old_path"), e["path"]) for e in bundle.get("code_events", [])
             if e.get("change") in ("rename", "copy") and e.get("old_path")]):
        lang = m["lang"] or ""
        src = qid("{}#{}:{}:{}".format(m["from_path"], lang, m["subkind"], m["name"]))
        dst = qid("{}#{}:{}:{}".format(m["to_path"], lang, m["subkind"], m["name"]))
        data = {"move_confidence": m["confidence"], "move_basis": m["basis"]}
        edges.append((src, dst, "replaced_by", None, data))
        edges.append((dst, src, "identity_from", None, data))

    # structure: milestones & areas (NULL ts -> excluded from window scans),
    # releases (dated point-in-time).
    for m in bundle.get("milestones", []):
        local = "milestone-{}".format(m.get("number") or m.get("title"))
        nodes.append((qid(local), project, repo, "structure", None, m, fetched))
    for r in bundle.get("releases", []):
        local = "release-{}".format(r.get("tag_name") or r.get("name"))
        nodes.append((qid(local), project, repo, "structure",
                      r.get("published_at"), r, fetched))
    for area in (bundle.get("code_graph", {}) or {}).get("areas") or []:
        local = "area-{}".format(area.get("id") or area.get("name") or area.get("path"))
        nodes.append((qid(local), project, repo, "structure", None, area, fetched))
    # structure: sprints (Phase 12 — Projects v2 iterations). `sprint-<id>`
    # structure node, ts = start (dated point-in-time so window scans can find the
    # current/next sprint), data = {title, start, end}. Empty for status-only
    # boards (the common case) -> nothing emitted -> goldens byte-identical.
    for sid, s in (bundle.get("sprints") or {}).items():
        nodes.append((qid("sprint-{}".format(sid)), project, repo, "structure",
                      s.get("start"), s, fetched))

    # structure: people + contribution / owns / touches / depends_on /
    # in_milestone edges (Phase 7b-1 step 3). All derived on the write path via
    # the shared `derive` leaf module; none of these leak into extract (person
    # nodes are project-scoped `person-<login>` structure nodes ignored by its
    # milestone-/release- prefix reconstruction, and every edge below is
    # non-spine, so traverse_spine never follows them).
    area_idx = derive.area_index(bundle.get("code_graph", {}) or {})
    # People derivation must see commit->PR links the same way enrich does, or a
    # reviewer whose PR's commits are only MESSAGE-resolvable to that PR (their
    # `pr` field is unset on the raw record) is silently dropped. enrich runs
    # attach_commit_prs FIRST; mirror that here. Resolve on a COPY so the stored
    # commit `code` nodes (built above) stay faithful to the raw record and
    # extract's commit reconstruction is unchanged.
    resolved = copy.deepcopy(bundle.get("commits") or [])
    attach_commit_prs(resolved)
    people_view = {**bundle, "commits": resolved,
                   "people": dict(bundle.get("people") or {})}
    # ONE shared enumerator: person nodes are created for EVERY participant with
    # any contribution edge — not just commit-authors + mapped-PR reviewers. This
    # is the anti-drift fix for the live-audit bug where 222 logins had
    # contribution edges (commented/reported/authored/reviewed) but NO person
    # node (dangling edges). Contributors carry their modules/areas; pure
    # participants get empty ones; bots are tagged (is_bot), never dropped.
    # validate.no_drift re-derives via the SAME enumerate_participants, so writer
    # and auditor can never diverge.
    people = derive.enumerate_participants(people_view, area_idx)

    def qperson(login):
        return graphstore.qualify_person(project, login)

    # People `structure` nodes (project-scoped). The `repo` column is the
    # project sentinel "*" so the SAME login folded from another repo in the
    # project upserts the identical id (idempotency is by the project-scoped id;
    # the sentinel keeps the row out of any single repo's repo_nodes view, an
    # extra no-leak guard). NULL ts -> excluded from window scans.
    for login, rec in people.items():
        if not login:
            continue
        data = {"login": login, **rec}
        # Project-scoped people aggregate across members. A later member's fold must
        # UNION its attribution with what an earlier member already stored — a plain
        # upsert would OVERWRITE, so the last member folded (e.g. one where the
        # person has no area attribution) would erase another member's. Union keeps
        # the person node the true project-wide aggregate, independent of fold order
        # and idempotent (re-folding a member unions a subset -> no change).
        prior = conn.execute("SELECT data FROM nodes WHERE id=?",
                             (qperson(login),)).fetchone()
        if prior and prior[0]:
            pd = json.loads(prior[0])
            data["areas"] = sorted(set(rec.get("areas") or []) | set(pd.get("areas") or []))
            data["modules"] = sorted(set(rec.get("modules") or []) | set(pd.get("modules") or []))
            data["is_bot"] = bool(rec.get("is_bot")) or bool(pd.get("is_bot"))
        nodes.append((qperson(login), project, "*", "structure", None, data, fetched))

    def contrib(login, dst_local, etype):
        if login:
            edges.append((qperson(login), qid(dst_local), etype, None, None))

    # Contribution edges from the RAW records (person -> node). Idempotent by
    # (src,dst,type); absent logins/fields are skipped cleanly.
    for pr in bundle.get("prs", []):
        prlocal = "pr-{}".format(pr["number"])
        contrib(pr.get("author"), prlocal, "authored")
        contrib(pr.get("merged_by"), prlocal, "merged")
        for reviewer in pr.get("reviewers") or []:
            contrib(reviewer, prlocal, "reviewed")
        for c in (pr.get("comments_list") or []) + (pr.get("review_comments") or []):
            contrib(c.get("author"), prlocal, "commented")
    for iss in bundle.get("issues", []):
        isslocal = "issue-{}".format(iss["number"])
        contrib(iss.get("author"), isslocal, "reported")
        for c in iss.get("comments_list") or []:
            contrib(c.get("author"), isslocal, "commented")
    for c in bundle.get("commits", []):
        contrib(c.get("author"), c["sha"], "authored")

    # Phase 10 slice 1: PR review submissions + lifecycle events as first-class
    # `social` nodes with a `part_of` spine edge to their parent. Folded by stable
    # id from the raw records (idempotent); review/event nodes are spine LEAVES
    # (no onward spine edge), so they ride into the PR/issue train when
    # traverse_spine walks it but can't bridge it to anything unrelated. Emitted
    # only when the data is present, so a bundle with no reviews/events stays
    # byte-identical (no fabricated nodes/edges).
    for pr in bundle.get("prs", []):
        prlocal = "pr-{}".format(pr["number"])
        prid = qid(prlocal)
        for rv in pr.get("reviews") or []:
            rid = rv.get("id")
            local = "review-{}-{}".format(pr["number"], rid)
            # Provenance: reviews normally carry an html_url, but synthesize a
            # stable `<pr url>#pullrequestreview-<id>` ref if one is absent (same
            # contract as lifecycle events) so check_provenance never fails.
            data = rv
            if not rv.get("url"):
                base = pr.get("url") or prlocal
                data = {**rv, "url": "{}#pullrequestreview-{}".format(base, rid)}
            nodes.append((qid(local), project, repo, "social",
                          rv.get("submitted_at"), data, fetched))
            edges.append((qid(local), prid, "part_of", None, None))
        for ev in pr.get("lifecycle") or []:
            data = _lifecycle_event_data(ev, pr.get("url"), prlocal)
            local = "event-{}-{}".format(prlocal, ev.get("id"))
            nodes.append((qid(local), project, repo, "social",
                          ev.get("created_at"), data, fetched))
            edges.append((qid(local), prid, "part_of", None, None))
    for iss in bundle.get("issues", []):
        isslocal = "issue-{}".format(iss["number"])
        issid = qid(isslocal)
        for ev in iss.get("lifecycle") or []:
            data = _lifecycle_event_data(ev, iss.get("url"), isslocal)
            local = "event-{}-{}".format(isslocal, ev.get("id"))
            nodes.append((qid(local), project, repo, "social",
                          ev.get("created_at"), data, fetched))
            edges.append((qid(local), issid, "part_of", None, None))

    # owns (person -> area): CODEOWNERS semantics via derive.area_owners — an
    # area is owned by the owners of the LAST rule matching the area directory
    # (graphify community areas: union over their files). Same matcher as
    # derive.annotate_people_profile's stale_owned.
    owner_rules = derive.compile_codeowners(bundle.get("code_owners"))
    if owner_rules:
        areas_by_id = {}
        for area in (bundle.get("code_graph", {}) or {}).get("areas") or []:
            areas_by_id[area.get("id") or area.get("name") or area.get("path")] = area
        for aid in sorted(a for a in areas_by_id if a):
            for login in derive.area_owners(owner_rules, aid,
                                            areas_by_id[aid].get("paths") or []):
                contrib(login, "area-{}".format(aid), "owns")

    # touches (commit/pr -> area): the distinct areas a commit's files land in.
    for c in bundle.get("commits", []):
        touched = derive._commit_areas(c, area_idx)
        for aid in sorted(touched):
            edges.append((qid(c["sha"]), qid("area-{}".format(aid)),
                          "touches", None, None))

    # depends_on (area -> area): flatten each area's resolved dependency edges
    # into store edges, carrying {version,transitive,ref,...} on the edge `data`.
    # Phase 3c stamps edges per area (code_graph.areas[].edges); there is no
    # top-level edges key. A resolved-local edge -> an intra-repo depends_on; an
    # unresolved registry edge resolves to a cross-repo member only in multi-repo
    # folds (see _fold_depends_on, threaded with members + registry_by_slug).
    _dep_dsts = set()
    for src, dst, data in _fold_depends_on(bundle, project, repo, members,
                                           registry_by_slug):
        edges.append((src, dst, "depends_on", None, data or None))
        _dep_dsts.add(dst)

    # A depends_on dst is always a real module-area path resolved from a `module`
    # source reference — an intra-repo SUB-MODULE, or a cross-repo producer's root.
    # The directory area-provider is WINDOW-SCOPED, so a referenced module that was
    # not itself changed in-window has no area node, leaving the edge dangling
    # (validate.referential_integrity). This is a real-data fact, not a bug — bicep
    # repos like avm/res/*/* reference deep sub-modules every gather; a single-repo
    # bicep window over Azure/bicep-registry-modules produced ~30 such edges. Ensure
    # EACH depends_on target (intra- and cross-repo) exists as a minimal `structure`
    # area node so the dependency graph stays referentially intact and complete.
    # Create-if-ABSENT only — never clobber a real area node, in any fold order:
    # get_node sees prior committed folds and this fold's own area nodes are already
    # in `nodes`; a later real fold of that area upserts over the stand-in. extract
    # rebuilds code_graph from the `codegraph` singleton (not area-* nodes), so the
    # stand-in is invisible to every member bundle — purely an edge anchor.
    # Ensure each NEW depends_on target (one not already staged in `nodes`) exists.
    # Batch the store existence check into a SINGLE query rather than a get_node per
    # dst — depends_on fan-out can be large (a bicep repo window has dozens) — then
    # synthesize only the truly-absent ones.
    _have = {n[0] for n in nodes}
    _cand = sorted(d for d in _dep_dsts if d not in _have)
    _existing = set()
    if _cand:
        ph = ",".join("?" for _ in _cand)
        _existing = {r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE id IN ({})".format(ph), _cand)}
    for dst in _cand:
        if dst in _existing:
            continue
        p = graphstore.parse_id(dst)
        scope = p["scope"]
        dst_repo = scope[len(project) + 1:] if scope.startswith(project + "/") else scope
        local = p["local"]
        area_id = local[len("area-"):] if local.startswith("area-") else local
        nodes.append((dst, project, dst_repo, "structure", None,
                      {"id": area_id, "synthesized": "depends_on_target"},
                      fetched))

    # in_milestone (social -> structure): a PR/issue's milestone title links to
    # the milestone node (keyed on number when present, else title — matching
    # the milestone node's own local id form above).
    ms_local = {}
    for m in bundle.get("milestones", []):
        local = "milestone-{}".format(m.get("number") or m.get("title"))
        if m.get("title") is not None:
            ms_local[m["title"]] = local
        if m.get("number") is not None:
            ms_local.setdefault(m["number"], local)
    for items, pref in ((bundle.get("prs", []), "pr-"),
                        (bundle.get("issues", []), "issue-")):
        for it in items:
            local = ms_local.get(it.get("milestone"))
            if local is not None:
                edges.append((qid("{}{}".format(pref, it["number"])),
                              qid(local), "in_milestone", None, None))

    # in_iteration (social -> structure): a PR/issue's board iteration links to its
    # `sprint-<id>` node — the sprint sibling of in_milestone. Only emitted when the
    # sprint was folded (a windowed-out iteration leaves no dangling edge). The
    # item's `board_status` rides the pr/issue node data blob (stamped in acquire),
    # so it round-trips through extract for free — no separate edge/node. Empty
    # `sprints` (status-only / no board) emits nothing -> goldens byte-identical.
    _sprint_ids = set(bundle.get("sprints") or {})
    for items, pref in ((bundle.get("prs", []), "pr-"),
                        (bundle.get("issues", []), "issue-")):
        for it in items:
            sid = it.get("iteration")
            if sid is not None and sid in _sprint_ids:
                edges.append((qid("{}{}".format(pref, it["number"])),
                              qid("sprint-{}".format(sid)), "in_iteration",
                              None, None))

    # structure: per-repo singleton facts (whole dict round-tripped under a
    # well-known local id, NULL ts so window scans skip it, identity-keyed /
    # idempotent). Only emitted when present and non-empty so extract never
    # fabricates an empty key. See STORE.md and extract._materialize_singleton.
    for local, value in (("workflowstats", bundle.get("workflow_stats")),
                         ("codegraph", bundle.get("code_graph")),
                         ("codeowners", bundle.get("code_owners")),
                         ("labeltaxonomy", bundle.get("label_taxonomy"))):
        if value:
            nodes.append((qid(local), project, repo, "structure", None,
                          value, fetched))

    # One fold = ONE transaction (issue #55): nodes, edges, code events, the FTS
    # index and the window/clone_sha provenance commit together or not at all.
    # `with conn:` commits on success and rolls back on any exception, so a crash
    # mid-fold never leaves a half-folded store for readers to serve. (A caller's
    # own uncommitted writes on this connection join the same transaction.)
    with conn:
        graphstore.upsert_nodes(conn, nodes, commit=False)
        graphstore.upsert_edges(conn, edges, commit=False)
        graphstore.add_code_events(conn, events, commit=False)

        # fts_text: index the searchable text per owning node id so `spotlight
        # grep` has something to MATCH (Phase 8a prerequisite). FTS5-gated —
        # when the SQLite build lacks FTS5 the store stays valid and we skip
        # silently. Re-indexing a node id replaces its text (via fts_map, no
        # table scan), so re-folding re-indexes cleanly (idempotent). Sources
        # (the approved scope, decision 1):
        #   - PR social nodes: title + body + embedded comment/review authors+bodies
        #   - issue social nodes: title + body + embedded comment authors+bodies
        #   - commit code nodes: the commit message
        # Empty text is skipped: it indexes nothing useful.
        if graphstore.fts5_available(conn):
            texts = []
            for pr in bundle.get("prs", []):
                texts.append(("pr-{}".format(pr["number"]), _social_text(
                    pr,
                    (pr.get("comments_list") or []) + (pr.get("review_comments") or []))))
            for iss in bundle.get("issues", []):
                texts.append(("issue-{}".format(iss["number"]),
                              _social_text(iss, iss.get("comments_list") or [])))
            for c in bundle.get("commits", []):
                texts.append((c["sha"], c.get("message") or ""))
            graphstore.index_texts(
                conn, [(qid(local), text) for local, text in texts if text],
                commit=False)

        graphstore.record_window(conn, project, repo, meta.get("from"),
                                 meta.get("to"), commit=False)
        # The window's reference date (issue #75): acquire() sets meta.ref_date
        # to `--ref-date` or `to`; the view of this window reads it back.
        # Always written (falling back to `to`), so a re-fold of the same window
        # without `--ref-date` resets an earlier override.
        ref_date = meta.get("ref_date") or meta.get("to")
        if ref_date:
            graphstore.set_ref_date(conn, project, repo, meta.get("from"),
                                    meta.get("to"), ref_date, commit=False)
        if meta.get("clone_sha"):
            graphstore.set_clone_sha(conn, project, repo, meta["clone_sha"],
                                     commit=False)


def _member_args(base, member, frm, to):
    """Clone the CLI args for one manifest member: same flags, but owner/repo and
    the window come from the manifest, and clone_dir is re-derived PER MEMBER as
    `{base}/{owner}-{repo}-clone`. Including the owner (not just the repo, which is
    acquire's single-repo default) keeps two members that share a repo name under
    different owners from colliding on the same checkout. A `--clone-dir` on the
    base args is treated as the parent directory; otherwise it defaults to
    `workspace`."""
    base_dir = (base.clone_dir or "workspace").rstrip("/")
    clone_dir = "{}/{}-{}-clone".format(base_dir, member["owner"], member["repo"])
    fields = {**vars(base), "owner": member["owner"], "repo": member["repo"],
              "clone_dir": clone_dir}
    ns = argparse.Namespace(**fields)
    setattr(ns, "from", frm)   # 'from' is a Python keyword: set via attribute
    ns.to = to
    ns.structural_iac = True   # manifest members get the whole-tree module scan
    return ns


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # A non-integer ACTIVITY_* knob fails here, before any clone or API call.
    validate_env_knobs()
    # Load + validate the manifest BEFORE touching the store, so a bad/missing
    # manifest fails fast instead of leaving an empty DB behind.
    man = manifest_mod.load_manifest(args.manifest) if getattr(
        args, "manifest", None) else None
    os.makedirs(os.path.dirname(args.store) or ".", exist_ok=True)
    conn = graphstore.open_store(args.store)
    graphstore.init_schema(conn)
    if man is not None:
        members = manifest_mod.member_slugs(man)
        registry_by_slug = {
            "{}/{}".format(m["owner"], m["repo"]): m.get("registry")
            for m in man["repos"]}
        for m in man["repos"]:
            member_args = _member_args(args, m, man["from"], man["to"])
            bundle = acquire(member_args, os.environ)
            fold_bundle(conn, bundle, project=man["project"],
                        repo="{}/{}".format(m["owner"], m["repo"]),
                        members=members, registry_by_slug=registry_by_slug)
        sys.stderr.write(
            "folded {} member repo(s) of project '{}' into store {}\n".format(
                len(man["repos"]), man["project"], args.store))
    else:
        # Store-only single-repo path (Phase 7). Same acquire->fold->return as
        # before; the store setup above is now shared with the manifest branch.
        bundle = acquire(args, os.environ)
        fold_bundle(conn, bundle)
        sys.stderr.write("folded bundle into store {}\n".format(args.store))
    conn.close()
    return args.store


if __name__ == "__main__":
    main()
