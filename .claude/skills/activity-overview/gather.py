"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""
import argparse
import atexit
import concurrent.futures
import contextlib
import copy
import datetime
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import derive
import extract
import graphstore
import manifest as manifest_mod
import people as people_mod

# The bundle carries the bundle/view contract version (issue #107); the
# store's own schema version is graphstore.SCHEMA_VERSION.
SCHEMA_VERSION = extract.VIEW_CONTRACT_VERSION
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


def _log_records(raw):
    """Split `git log --pretty=format:<LOG_FORMAT>` output into records, each a
    list of lines: [header, body line, ...]. A record starts with RECORD_SEP at
    the start of the output or of a line (git puts every separator there), so a
    RECORD_SEP inside a subject (mid-line) does not start a new record. Lines
    are split on "\\n" only: `str.splitlines()` also splits on U+2028/U+2029,
    U+0085, form feed, vertical tab and the separator control characters,
    which can appear unquoted in a subject or (non-ASCII with
    core.quotepath=false) a path (#109)."""
    for chunk in re.split("(?:^|\n)" + RECORD_SEP, raw or ""):
        if chunk.strip():
            yield chunk.split("\n")


def _parse_log_header(line):
    """The header line of a LOG_FORMAT record as a dict {sha, parents, author,
    author_email, committed_at?, date, subject}, or None when it has too few
    fields. The subject is the LAST field, so a FIELD_SEP inside it stays in it.
    `committed_at` is the strict-ISO committer timestamp (`%cI`) normalised to
    UTC `YYYY-MM-DDTHH:MM:SSZ` (absent when unparseable); `date` is its UTC
    calendar day, the day every record of the commit is windowed on (#109:
    PRs and issues are windowed in UTC too). Only an unparseable timestamp
    falls back to its own first ten characters."""
    fields = line.split(FIELD_SEP, 5)
    if len(fields) < 6:
        return None
    sha, parents, author, email, stamp = (f.strip() for f in fields[:5])
    head = {"sha": sha, "parents": parents.split(), "author": author,
            "author_email": email, "subject": fields[5].strip(" \t\r")}
    committed_at = _utc_timestamp(stamp)
    if committed_at:
        head["committed_at"] = committed_at
    head["date"] = committed_at[:10] if committed_at else stamp[:10]
    return head


def parse_git_log(raw):
    """Parse `git log` output formatted with LOG_FORMAT (COMMIT_WALK_ARGS).

    Each record: the LOG_FORMAT header followed by newline-separated file paths
    (`--name-only`). Returns a list of commit dicts. The author email (`%ae`)
    is kept as a TRANSIENT `author_email` key: `resolve_commit_authors` reads
    it to find the author's GitHub login and removes it, so no email reaches
    the bundle (#78). `committed_at` is the committer timestamp in UTC
    `YYYY-MM-DDTHH:MM:SSZ` (the GitHub API's form) so it sorts against comment
    timestamps (#82); `date` is its UTC day, used for windowing (#109). An
    empty email adds no `author_email`, an unparseable timestamp no
    `committed_at`.
    """
    commits = []
    for lines in _log_records(raw):
        head = _parse_log_header(lines[0])
        if head is None:
            continue
        # `--name-only` paths: C-quoted ones (quote/backslash/control chars, or
        # non-ASCII without core.quotepath=false) are unquoted.
        files = [unquote_git_path(ln) for ln in lines[1:] if ln]
        commit = {
            "sha": head["sha"],
            "parents": head["parents"],
            "author": head["author"],
            "date": head["date"],
            "message": head["subject"],
            "files": files,
            "pr": None,  # resolved in link.py
        }
        if head["author_email"]:
            commit["author_email"] = head["author_email"]
        if "committed_at" in head:
            commit["committed_at"] = head["committed_at"]
        commits.append(commit)
    return commits


def _utc_timestamp(value):
    """A strict-ISO timestamp with an offset (`%cI`) as UTC
    `YYYY-MM-DDTHH:MM:SSZ`, or None when it is empty, unparseable or has no
    offset. Pure."""
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.utcoffset() is None:
        return None
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The one `git log` format of every walk (#109): sha, parents, author name,
# author email, strict-ISO committer timestamp, subject. The subject is free
# text, so it is LAST: nothing can follow it on the header line. The body after
# the header depends on the walk (`--name-only` paths, `--name-status` lines or
# a `-p` patch). The committer timestamp (`%cI`) gives every record of a commit
# (the commit, its code events, its symbol events) the same UTC day, the day
# PRs and issues are windowed on too. The author email is only used to resolve
# the commit author's GitHub login and is never stored (#78); the code-event
# and patch parsers ignore it.
LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%cI%x1f%s"

# `git log` arguments (after `git -C <clone> log`) for acquire()'s three walks,
# shared with the real-repo tests so they exercise the exact commands. Paths in
# their output are made comparable by run_git (core.quotepath=false) plus
# unquote_git_path in each parser. The code-event walk's body is
# `--name-status` lines so each changed path carries its change type (and
# rename/copy detection via -M -C gives `R###`/`C###` with old+new paths).
# Belt and braces to run_git's config isolation (issue #109): flags that pin
# what user config could otherwise change -- colour escapes, an external diff
# or textconv driver, signature lines, rename detection (`-M` is git's default
# `diff.renames=true`) and the `a/` / `b/` patch prefixes the diff parser reads.
_WALK_PIN_ARGS = ("--no-color", "--no-ext-diff", "--no-textconv",
                  "--no-show-signature")
COMMIT_WALK_ARGS = (f"--pretty=format:{LOG_FORMAT}",
                    "--name-only", "-M") + _WALK_PIN_ARGS
CODE_WALK_ARGS = ("--reverse", f"--pretty=format:{LOG_FORMAT}",
                  "--name-status", "-M", "-C") + _WALK_PIN_ARGS
PATCH_WALK_ARGS = ("--reverse", f"--pretty=format:{LOG_FORMAT}",
                   "-p", "--unified=3", "-M", "-C") + _WALK_PIN_ARGS + (
                   "--src-prefix=a/", "--dst-prefix=b/")

_STATUS_TO_CHANGE = {"A": "add", "M": "modify", "D": "delete",
                     "R": "rename", "C": "copy", "T": "modify"}


def parse_code_events(raw):
    """Parse `git log --name-status -M -C` output into raw code-events.

    Each event: {commit, author, date, change, path[, old_path]} where change is
    add|modify|delete|rename|copy. Rename/copy lines (`R###`/`C###`) carry the old
    path in `old_path` and the new path in `path`. Pure; permissive on junk lines.
    """
    events = []
    for lines in _log_records(raw):
        head = _parse_log_header(lines[0])
        if head is None:
            continue
        sha, author, date = head["sha"], head["author"], head["date"]
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


def _clone_default_branch(clone_dir, run):
    """The remote default branch recorded in an existing clone, without network:
    `refs/remotes/origin/HEAD` (set by `git clone`, refreshed by prepare_clone's
    `remote set-head --auto` on a reused clone), else the checked-out branch
    (prepare_clone checks the default out). None when neither resolves."""
    for ref in ("refs/remotes/origin/HEAD", "HEAD"):
        try:
            name = run(["git", "-C", clone_dir, "symbolic-ref", "--short",
                        ref]).strip()
        except RuntimeError:
            continue
        if ref != "HEAD":
            if not name.startswith("origin/"):
                continue
            name = name[len("origin/"):]
        if name and name != "HEAD":
            return name
    return None


def resolve_branches(explicit, clone_dir, clone_mode, get_repo=None, run=None):
    """Decide the branches a gather analyzes (issue #84) ->
    (branches, base_branch, source). `base_branch` is the mainline: a PR merged
    into any other branch is a stacked / release-branch merge, not shipped.

    1. `explicit` (the `--branches` value, or a manifest member's `branch`):
       comma-separated, the first is the mainline.
    2. The clone's remote default branch (`_clone_default_branch`) -- the same
       branch the commit walk covers.
    3. `get_repo()` -> the GitHub repo object's `default_branch` (one REST
       call; used when there is no clone, e.g. `--no-clone` with none on disk).
    4. Unknown: ([], None, None). Never a guessed `main`: the caller warns and
       the view reports the base branch as unknown.
    """
    run = run or run_git
    if explicit:
        branches = [b.strip() for b in str(explicit).split(",") if b.strip()]
        if branches:
            return branches, branches[0], "--branches"
    if clone_mode != "none" and os.path.isdir(os.path.join(clone_dir, ".git")):
        name = _clone_default_branch(clone_dir, run)
        if name:
            return [name], name, "clone"
    if get_repo is not None:
        repo_obj = get_repo()
        name = repo_obj.get("default_branch") if isinstance(repo_obj, dict) else None
        if isinstance(name, str) and name.strip():
            return [name.strip()], name.strip(), "api"
    return [], None, None


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
        # An earlier current-month run built IaC in place (issue #109).
        reset_build_byproducts(clone_dir, run)
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


# What gather's own IaC builds leave in a checkout (issue #109): `terraform
# init` writes `.terraform/` and `.terraform.lock.hcl` in each module dir it
# builds (bicep restore/build write nothing there: restore caches under
# ~/.bicep, build prints to stdout). For the current month the builds run in
# the clone itself (structure_tree yields the clone), so these are left behind.
_BUILD_BYPRODUCT_PATHSPECS = (":(glob)**/.terraform/**", ":(glob)**/.terraform.lock.hcl")


def reset_build_byproducts(clone_dir, run=None):
    """Remove an earlier run's IaC build output so the next branch switch
    cannot be blocked by it (issue #109): untracked `.terraform/` dirs and
    untracked `.terraform.lock.hcl` files anywhere in the clone are deleted
    (`git clean`, restricted to those pathspecs), and a TRACKED
    `.terraform.lock.hcl` that `terraform init` rewrote is restored to its
    committed content. Nothing else is touched: a user's own untracked or
    modified files keep blocking a checkout exactly as before, never
    silently deleted. Ignored files under those paths are cleaned too (-x):
    a repo's `.gitignore` normally lists `.terraform/`."""
    run = run or run_git
    run(["git", "-C", clone_dir, "clean", "-f", "-d", "-x", "-q", "--",
         *_BUILD_BYPRODUCT_PATHSPECS])
    changed = [unquote_git_path(ln) for ln in run(
        ["git", "-C", clone_dir, "ls-files", "-m", "--",
         _BUILD_BYPRODUCT_PATHSPECS[1]]).splitlines() if ln]
    if changed:
        run(["git", "-C", clone_dir, "checkout", "-q", "--", *changed])


class MissingBranchError(RuntimeError):
    """The base branch a gather was told to walk does not exist on the remote."""


# git's message when a fetched ref is not on the remote.
_MISSING_REMOTE_REF = "couldn't find remote ref"


def build_branch_fetch_cmd(clone_dir, branch, from_date, depth1=False):
    """Fetch ONE branch into `refs/remotes/origin/<branch>` under the window's
    history bound (the same `_shallow_selector` a fresh clone gets)."""
    return ["git", "-C", clone_dir, "fetch", _shallow_selector(from_date, depth1),
            "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]


def checkout_base_branch(clone_dir, branch, from_date, run=None):
    """Make the clone walk `branch` (issue #107): fetch `origin/<branch>` with
    the window's `--shallow-since` bound and check it out as the local branch
    `<branch>`, on a fresh or a reused clone alike. Called after
    resolve_branches, so the one clone serves both the default-branch
    detection and the walk.

    Returns True when the branch has no commit since the bound (a quiet
    branch) and was fetched at `--depth=1` instead, as prepare_clone does for
    a quiet repo: the caller then treats the walks as empty. A branch the
    remote does not have raises MissingBranchError naming it, with the clone
    left on the branch it was on; any other git failure raises RuntimeError."""
    run = run or run_git
    try:
        depth1 = _shallow_or_depth1(
            run, lambda d1: build_branch_fetch_cmd(clone_dir, branch, from_date, d1))
    except RuntimeError as exc:
        if _MISSING_REMOTE_REF in str(exc):
            raise MissingBranchError(
                f"base branch {branch!r} does not exist on the remote") from None
        raise
    reset_build_byproducts(clone_dir, run)
    run(["git", "-C", clone_dir, "checkout", "-q", "-B", branch,
         f"refs/remotes/origin/{branch}"])
    return depth1


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
    raw = run(["git", "-C", clone_dir, "log", f"--pretty=format:{LOG_FORMAT}"])
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


def created_by_end(record, to_date):
    """True when a PR / issue record existed at the window's end: created on
    or before `to_date` (a record with no `created_at` cannot be placed and
    counts as existing). Pure."""
    created = record.get("created_at")
    return not created or in_window(created, None, to_date)


def keep_pr_for_window(pr, from_date, to_date):
    """True when a window's gather keeps PR `pr` (a normalized record):
      - merged in the window, or closed unmerged in the window;
      - open now and created on or before `to_date`. A PR opened after the
        window's end is in no view of the window (extract leaves out a record
        created after it), so storing it only stored people no view of the
        window can derive and failed `validate --bundle` (USAGE §5.5) on a
        month gathered late. For a window ending today nothing changes;
      - open at the window's END: created on or before `to_date` and merged /
        closed only after it. A month gathered late needs these: the report
        view shows each record as of the window's end (extract.record_as_of),
        and a PR that was in flight then but has landed since would otherwise
        not be in the store at all (final gate, #97). The closed-PR listing
        already reaches them (it runs back to `from_date` by update time).
    Pure."""
    if pr.get("state") == "open":
        return created_by_end(pr, to_date)
    ended = pr.get("merged_at") if pr.get("merged") else pr.get("closed_at")
    if in_window(ended, from_date, to_date):
        return True
    return bool(ended) and not in_window(ended, None, to_date) \
        and in_window(pr.get("created_at"), None, to_date)


def keep_issue_for_window(issue, from_date, to_date):
    """True when a window's gather keeps an issue from its LISTINGS (every
    open issue, and every issue updated since `from_date`):
      - created on or before `to_date` (an issue opened after the window's
        end is in no view of it, like a PR: see keep_pr_for_window), and
      - open now, or updated (closed, commented on, ...) on or after
        `from_date`. The updated-since listing is read a whole page at a
        time, so its last page also holds issues closed and last touched
        before the window: no view of the window holds one, and storing it
        stored its people with nothing to derive them from.
    An issue a kept PR closes or cross-references is train context and is
    kept whatever its dates (acquire); a view of the window still leaves it
    out when it was created after the window's end. Pure."""
    if not created_by_end(issue, to_date):
        return False
    if issue.get("state") == "open":
        return True
    return in_window(issue.get("updated_at"), from_date, None) \
        or in_window(issue.get("closed_at"), from_date, None)


def window_records(records, from_date, to_date):
    """Keep only commits/events whose `date` (the UTC day of the commit's
    committer timestamp, #109) falls in [from,to] inclusive. Pure. Used instead
    of git's `--since/--until`, which silently prunes valid in-window commits
    when commit dates are non-monotonic across committer timezones (and
    excludes the upper boundary day)."""
    return [r for r in records if in_window(r.get("date"), from_date, to_date)]


# GitHub closing-keyword grammar (#62). A keyword (close/closes/closed,
# fix/fixes/fixed, resolve/resolves/resolved; any case), an optional `:`, then
# ONE reference: `#N`, `owner/repo#N`, or a full
# `https://github.com/owner/repo/(issues|pull)/N` URL. Each closed issue needs its
# own keyword: in "Fixes #1, #2" / "Fixes #1 and #2" only #1 closes; #2 is a
# *listed* ref (GitHub links it as a mention, never closes it). Closing keywords
# link issues only, so a keyword before a `/pull/` URL is a cross-ref, not a
# close. No Markdown parsing: a keyword inside a code block / code span matches
# like anywhere else. HTML comments are the exception (_visible_text): GitHub
# does not act on text inside `<!-- ... -->`, and PR templates routinely carry
# an example there ("<!-- Fixes #123 -->"), which made half the PRs of a real
# repository "close" issue 123 (final-gate finding, #97).
# Owners/repos: GitHub names — alnum start, then word/.-.
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


# A terminated comment anywhere, or an unterminated one that starts a line (an
# HTML block, which then runs to the end of the text).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->|(?m:^[ \t]{0,3})<!--.*\Z", re.DOTALL)
# Markdown code: fenced blocks and inline spans. A `<!--` written there is a
# literal, not a comment opener.
_MD_CODE_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}(?P=fence)[ \t]*$|\Z)"
    r"|(?P<tick>`+)(?!`).+?(?<!`)(?P=tick)(?!`)", re.DOTALL)


def _visible_text(text):
    """`text` without its HTML comments (`<!-- ... -->`; an unterminated one
    that starts a line runs to the end): what GitHub renders and scans for
    references. A `<!--` inside Markdown code (a fenced block or an inline
    span) is a literal and opens nothing; code content itself stays scanned,
    as before. Pure."""
    text = text or ""
    if "<!--" not in text:
        return text
    # find comments on a copy whose code has no `<` (same length, same offsets)
    probe = _MD_CODE_RE.sub(lambda m: m.group(0).replace("<", " "), text)
    out, pos = [], 0
    for m in _HTML_COMMENT_RE.finditer(probe):
        out.append(text[pos:m.start()])
        out.append(" ")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


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
    (", #2", " and #2") has closes=False. HTML comments are not scanned
    (_visible_text). Pure."""
    text = _visible_text(text)
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
    for m in _QUALIFIED_URL_RE.finditer(_visible_text(text)):
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
    for m in _BLOCKS_RE.finditer(_visible_text(text)):
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
        "labels": [lbl["name"] for lbl in raw.get("labels") or []],
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
        # the repo the head branch lives in (a fork's full name for a fork PR;
        # None when GitHub no longer knows it, e.g. a deleted fork) — a stacked
        # PR's parent is matched by (head_repo, head), not the bare branch name
        # (issue #93: two forks' `patch-1` are different branches).
        "head_repo": (((raw.get("head") or {}).get("repo")) or {}).get("full_name"),
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
        "labels": [lbl["name"] for lbl in raw.get("labels") or []],
        "assignees": [a["login"] for a in raw.get("assignees") or []],
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


def summarize_reviews(raw_reviews, author=None):
    """Reduce raw PR reviews to {reviewers, decision}. Pure.

    `reviewers`: sorted logins with at least one SUBMITTED review (PENDING
    drafts and unknown states are ignored). The PR's `author` is never a
    reviewer of their own PR (issue #110 item 5): GitHub records the author's
    reply in a review thread as a COMMENTED review, and those are left out of
    `reviewers` and of the decision, as review rounds already leave them out.
    With only the author's own reviews the decision is `none`.

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
        if not login or (author is not None and login == author):
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


def resolve_commit_authors(commits, api_logins, known_logins=(),
                           home_domains=(), home_email=None):
    """Resolve each commit's `author` to a GitHub login, in place (#78).

    Sets `author` (the login, or the unresolved person key), `author_name` (the
    git `%an`), `login_resolved` and, when resolved, `login_source`; removes the
    transient `author_email`. A noreply-email login takes the spelling of a
    `known_logins` entry that differs only in case (GitHub logins are
    case-insensitive). Returns {sha: author} so the code/symbol events of the
    same commits can be re-keyed (`remap_event_authors`). Deterministic.
    With `home_domains`, the resolved login of a commit whose email is in one
    of them (email_domain_is_home) is added to the set `home_email`: the only
    trace of the email that survives (the affiliation signal)."""
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
            if home_email is not None and email_domain_is_home(email, home_domains):
                home_email.add(login)
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


def _payload_in_repo(repository, current):
    """True when a timeline payload's `repository` object names the repo
    `current` (lower-case 'owner/repo'), compared case-insensitively (#70).
    A payload with no repository name at all counts as the current repo, so
    older fixtures and partial payloads keep parsing (#109)."""
    full = ((repository or {}).get("full_name")) or ""
    return not full or full.lower() == current


def parse_timeline_crossrefs(raw_timeline, current_slug):
    """Issue numbers of the repo `current_slug` ('owner/repo') cross-referenced
    or connected to a PR via timeline events.

    De-duplicated, order-preserving (first appearance). Skips cross-refs whose
    source is itself a pull request (we only want issue links) and, since #109,
    events whose source / subject issue lives in another repo (its
    `repository.full_name`, compared case-insensitively): their number is not
    an issue of this repo (parse_timeline_xrefs keeps them as cross-repo refs,
    so each `cross-referenced` event is counted by exactly one of the two).
    Events are read in order: a `connected` link later undone by a
    `disconnected` event for the same issue does not count (a reconnect counts
    again); a disconnect never cancels a cross-reference (a mention). Pure."""
    current = (current_slug or "").lower()
    order, mentioned, connected = [], set(), set()
    for ev in raw_timeline or []:
        kind = ev.get("event")
        num = None
        if kind == "cross-referenced":
            issue = (ev.get("source") or {}).get("issue") or {}
            if issue.get("pull_request") is None \
                    and _payload_in_repo(issue.get("repository"), current):
                num = issue.get("number")
                if num is not None:
                    mentioned.add(num)
        elif kind in ("connected", "disconnected"):
            # Tolerant: an event without a usable issue number (no subject,
            # a null / non-integer number) is not a link (issue #105).
            subject = ev.get("subject") or {}
            num = subject.get("number")
            if not isinstance(num, int) or isinstance(num, bool) \
                    or not _payload_in_repo(subject.get("repository"), current):
                num = None
            elif kind == "connected":
                connected.add(num)
            else:
                connected.discard(num)
        if num is not None and num not in order:
            order.append(num)
    return [n for n in order if n in mentioned or n in connected]


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


# A release's notes text (the `body` GitHub already returns with the release
# list) is kept up to this many characters, so a repository without a changelog
# (a Terraform module repo, a landing zone) still has its maintainers' own
# release text. Generated notes run to a few thousand characters (a "What's
# changed" list of PR titles); the cap keeps a long one from dominating a view.
RELEASE_NOTES_CAP = 4000


def normalize_release(raw):
    """Map a GitHub release object to the bundle's release shape.

    `notes` (only when the release has text) is its `body`, verbatim up to
    RELEASE_NOTES_CAP characters; a longer one is cut there and
    `notes_truncated_from` records its original length."""
    out = {
        "tag_name": raw.get("tag_name"),
        "name": raw.get("name"),
        "published_at": raw.get("published_at"),
        "prerelease": bool(raw.get("prerelease")),
        "url": raw.get("html_url"),
    }
    body = raw.get("body")
    if isinstance(body, str) and body.strip():
        body = body.replace("\r\n", "\n")
        if len(body) > RELEASE_NOTES_CAP:
            out["notes"] = body[:RELEASE_NOTES_CAP]
            out["notes_truncated_from"] = len(body)
        else:
            out["notes"] = body
    return out


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


# What a review comment is attached to: the diff hunk GitHub returns with
# it ends at the commented line, so its LAST lines are kept (the hunk header
# `@@ ... @@` first), at most this many lines and characters.
REVIEW_HUNK_MAX_LINES = 12
REVIEW_HUNK_CAP = 1500


def review_hunk(hunk):
    """(kept hunk, original line count) of a review comment's `diff_hunk`:
    the `@@` header line plus the last REVIEW_HUNK_MAX_LINES - 1 lines,
    capped at REVIEW_HUNK_CAP characters (from the end: the commented line
    is last). The count is returned only when lines were cut, else None.
    Pure."""
    lines = (hunk or "").replace("\r\n", "\n").split("\n")
    total = len(lines)
    if total > REVIEW_HUNK_MAX_LINES:
        head = [lines[0]] if lines[0].startswith("@@") else []
        lines = head + lines[-(REVIEW_HUNK_MAX_LINES - len(head)):]
    text = "\n".join(lines)
    if len(text) > REVIEW_HUNK_CAP:
        text = text[-REVIEW_HUNK_CAP:]
    return text, (total if text != (hunk or "").replace("\r\n", "\n") else None)


def normalize_review_comment(raw):
    """Map a GitHub PR review comment (inline diff comment) to the same
    shape, plus what it is attached to when GitHub gives it (each key
    omitted when absent): `path` (the file), `line` (the line it is on in
    the diff's new side, else its original line), `diff_hunk` (review_hunk;
    `diff_hunk_lines` the original line count when cut), `in_reply_to` (the
    id of the comment that opened the thread it replies to) and `commit`
    (the PR commit it was written against)."""
    out = _normalize_comment_obj(raw)
    if raw.get("path"):
        out["path"] = raw["path"]
    line = raw.get("line") if raw.get("line") is not None \
        else raw.get("original_line")
    if isinstance(line, int):
        out["line"] = line
    if raw.get("diff_hunk"):
        out["diff_hunk"], total = review_hunk(raw["diff_hunk"])
        if total is not None:
            out["diff_hunk_lines"] = total
    if raw.get("in_reply_to_id") is not None:
        out["in_reply_to"] = raw["in_reply_to_id"]
    commit = raw.get("original_commit_id") or raw.get("commit_id")
    if commit:
        out["commit"] = commit
    return out


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
    for lines in _log_records(raw):
        head = _parse_log_header(lines[0])
        if head is None:
            continue
        body = "\n".join(lines[1:])
        for f in parse_unified_diff(body):
            yield head["sha"], head["author"], head["date"], f


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


# --- Module change notes (per-window fact `module_notes`) ---------------------
#
# What the maintainers themselves wrote about a module, read from the clone:
# the changelog entries a window's commits ADDED, the version-file movement
# (`version.json` of a Bicep AVM module) and the version tags created in the
# window. Work is per changed file: only the changelog / version files the
# window's file-level code events name are read (two blob reads per changed
# file: the commit's version and its parent's), never the whole tree.
#
# Why per commit and not from the patch walk: a unified diff with 3 lines of
# context often lacks the `## <version>` heading an added item sits under (an
# item appended to an existing section), so the hunk alone cannot say which
# version or section it belongs to. Parsing the file at the commit and at its
# parent and comparing the two is exact, attributes each entry to the commit
# (and so the PR) that added it, and costs the same blobs the patch walk
# already fetched. A rename (git `R`) reads the parent at the old path, so a
# moved module's changelog is not mistaken for a new one.
#
# Everything network- or git-facing goes through two seams: `read_blob(rev,
# path)` and `list_tags()` (production: git_blob_reader / git_tag_lister).

# Changelog files, matched on the lower-cased basename.
CHANGELOG_FILE_NAMES = ("changelog.md", "changes.md", "history.md",
                        "release_notes.md", "releasenotes.md")
# Version files: a JSON object with a top-level "version" (Bicep AVM).
VERSION_FILE_NAMES = ("version.json",)
MODULE_NOTE_TEXT_CAP = 600       # characters kept of one changelog item
MODULE_NOTE_ITEMS_CAP = 40       # items kept per section
MODULE_NOTE_ENTRIES_CAP = 12     # entries kept per changelog file per commit
# An item that only says "nothing here" (the AVM template writes `- None`).
CHANGELOG_PLACEHOLDERS = frozenset(("none", "none.", "n/a", "na", "-", "—"))
# ... also when such a word opens the item and only an aside follows it
# ("None (external interface unchanged)", "N/A - docs only").
_PLACEHOLDER_LEAD_RE = re.compile(r"^(?:none|n/a|na)\s*(?:[.:(—–-]|$)", re.I)

_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_MD_LINK_RE = re.compile(r"^\[([^\]]+)\](?:\([^)]*\)|\[[^\]]*\])?")
_CHANGELOG_VERSION_RE = re.compile(
    r"^(?:version|release)?[ \t]*v?(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.+-]*)?)"
    r"(?=$|[\s\]\)(:,-])", re.IGNORECASE)
_ISO_DAY_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# a Markdown link reference definition (`[1.2.0]: https://...`), not text
_MD_LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")
_MD_BULLET_RE = re.compile(r"^( {0,1})(?:[-*+]|\d{1,3}[.)])[ \t]+(.*)$")
_VERSION_TAG_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.+-]*)?$")
TAG_LIST_FORMAT = ("%(refname:strip=2)%1f%(objecttype)%1f"
                   "%(creatordate:iso-strict)%1f%(objectname)%1f%(*objectname)")


def changelog_placeholder(text):
    """True for a changelog item that only says there is nothing ("None",
    "N/A", "None (interface unchanged)"). Pure."""
    t = (text or "").strip()
    return t.lower() in CHANGELOG_PLACEHOLDERS or bool(_PLACEHOLDER_LEAD_RE.match(t))


class BlobUnreadable(RuntimeError):
    """A file version that exists but could not be read (a git error)."""


def _changelog_version(title):
    """(version, date) of a changelog heading that names a version, else
    None. Accepts `1.2.0`, `v1.2.0`, `[1.2.0] - 2024-01-31`, `[1.2.0](url)`,
    `Version 1.2.0 (2024-01-31)` and `[Unreleased]` (version "Unreleased").
    Pure."""
    text = title.strip()
    m = _MD_LINK_RE.match(text)
    if m:                       # `[1.2.0](url) - date` / `[Unreleased]`
        inner, rest = m.group(1).strip(), text[m.end():]
    else:
        inner, rest = text, ""
    if inner.lower() == "unreleased":
        return "Unreleased", None
    v = _CHANGELOG_VERSION_RE.match(inner)
    if not v:
        return None
    day = _ISO_DAY_RE.search(inner[v.end():] + " " + rest)
    return v.group(1), (day.group(1) if day else None)


def changelog_section_kind(heading):
    """The kind of a changelog section heading: breaking, deprecated,
    removed, security, fixed, added, changed or other (an unknown heading is
    kept under its own heading with kind `other`). Pure."""
    h = (heading or "").lower()
    if "breaking" in h:
        return "breaking"
    if "deprecat" in h:
        return "deprecated"
    if re.search(r"\bremov", h):
        return "removed"
    if re.search(r"\bsecurity\b", h):
        return "security"
    if re.search(r"\bfix|\bbug", h):
        return "fixed"
    if re.search(r"\b(added|adds|new|features?)\b", h):
        return "added"
    if re.search(r"\b(changes?|changed|updates?|updated|improvements?|"
                 r"enhancements?)\b", h):
        return "changed"
    return "other"


def _cap_item(text):
    if len(text) <= MODULE_NOTE_TEXT_CAP:
        return {"text": text}
    return {"text": text[:MODULE_NOTE_TEXT_CAP], "truncated_from": len(text)}


def _new_section(heading):
    return {"heading": heading, "kind": changelog_section_kind(heading)
            if heading is not None else "unsectioned", "items": []}


def parse_changelog(text):
    """The version entries of a changelog, in file order. Pure.

    An entry starts at a heading naming a version (`_changelog_version`);
    the first one sets the entry heading level, and a later heading at that
    level or above that names no version (a title, a footer) ends the entry.
    Deeper headings inside an entry are its sections (`### Changes`, `###
    Breaking Changes`, `### Added`, ... — any heading, kept verbatim with a
    `kind`); items before any section go to a section with heading None.
    An item is a list bullet at the line start (its continuation lines and
    nested bullets belong to it) or a paragraph; fenced code is kept inside
    the item it follows; link reference definitions (`[1.2.0]: url`) are
    not text. Item text is verbatim (lines joined with a newline,
    stripped; a fenced block right after an item, even past a blank line,
    is part of it) and capped at MODULE_NOTE_TEXT_CAP characters
    (`truncated_from` keeps the original length). A section whose only
    items are placeholders (`changelog_placeholder`: "None", "N/A") keeps
    no items and records `placeholder` (the text it said). Entry: {"version", "heading",
    ["date"], "sections": [{"heading", "kind", "items": [{"text",
    ["truncated_from"]}], ["placeholder"], ["items_omitted"]}]}."""
    entries, level = [], None
    entry = section = None
    item_lines, item_open, in_code = None, False, False

    def flush():
        nonlocal item_lines
        if item_lines is not None and section is not None:
            body = "\n".join(item_lines).strip()
            if body:
                section["items"].append(body)
        item_lines = None

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_code and item_lines is None and section is not None \
                    and section["items"]:
                # a fenced block after a blank line belongs to the item
                # before it ("Before:" followed by an example)
                item_lines = [section["items"].pop()]
            in_code = not in_code
            if item_lines is not None:
                item_lines.append(line)
            continue
        if in_code:
            if item_lines is not None:
                item_lines.append(line)
            continue
        m = _MD_HEADING_RE.match(line)
        if m:
            flush()
            item_open = False
            depth, title = len(m.group(1)), m.group(2).strip()
            ver = _changelog_version(title)
            if ver is not None and (level is None or depth <= level):
                level = depth
                entry = {"version": ver[0], "heading": line.strip(),
                         "sections": []}
                if ver[1]:
                    entry["date"] = ver[1]
                entries.append(entry)
                section = None
            elif entry is not None and level is not None and depth > level:
                section = _new_section(title)
                entry["sections"].append(section)
            elif level is not None and depth <= level:
                entry = section = None          # a heading that ends the entry
            continue
        if entry is None or _MD_LINK_DEF_RE.match(line):
            continue
        if not stripped:
            flush()
            item_open = False
            continue
        bullet = _MD_BULLET_RE.match(line)
        if bullet:
            flush()
            if section is None:
                section = _new_section(None)
                entry["sections"].append(section)
            item_lines, item_open = [bullet.group(2)], True
        elif item_open and item_lines is not None:
            item_lines.append(stripped)          # continuation / nested bullet
        else:
            flush()
            if section is None:
                section = _new_section(None)
                entry["sections"].append(section)
            item_lines, item_open = [stripped], True   # a paragraph
    flush()
    for e in entries:
        for s in e["sections"]:
            texts = s.pop("items")
            real = [t for t in texts if not changelog_placeholder(t)]
            if texts and not real:
                s["placeholder"] = texts[0]
            s["items"] = [_cap_item(t) for t in real[:MODULE_NOTE_ITEMS_CAP]]
            if len(real) > MODULE_NOTE_ITEMS_CAP:
                s["items_omitted"] = len(real) - MODULE_NOTE_ITEMS_CAP
    return entries


def _entry_key(entry):
    v = (entry.get("version") or "").lower()
    return v[1:] if v.startswith("v") else v


def _changelog_bullets(text):
    """The top-level bullet texts of a changelog that names no version, in
    order (the fallback of changelog_added_entries)."""
    out, cur = [], None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        bullet = _MD_BULLET_RE.match(line)
        if bullet:
            if cur is not None:
                out.append("\n".join(cur).strip())
            cur = [bullet.group(2)]
        elif cur is not None and line.strip() and not _MD_HEADING_RE.match(line):
            cur.append(line.strip())
        elif cur is not None:
            out.append("\n".join(cur).strip())
            cur = None
    if cur is not None:
        out.append("\n".join(cur).strip())
    return [t for t in out if t and not changelog_placeholder(t)]


def changelog_added_entries(before, after):
    """The changelog entries `after` adds to `before` (either None when the
    file is absent), in `after`'s order. Pure.

    A version `before` does not have is added whole. A version both have
    is added with only the items `before`'s same section (same heading) did
    not hold, marked `amended: true` (an item appended to a version not yet
    released); nothing new -> not listed. A changelog that names no version
    at all: its new top-level bullets, as one entry with version None.
    Removed entries are not reported (the file's history keeps them)."""
    new = parse_changelog(after)
    old = parse_changelog(before) if before else []
    if not new and not old:
        # compared on the capped text, as the versioned path compares items
        known = {_cap_item(t)["text"] for t in _changelog_bullets(before)} \
            if before else set()
        items = [t for t in _changelog_bullets(after)
                 if _cap_item(t)["text"] not in known]
        if not items:
            return []
        return [{"version": None, "heading": None, "sections": [
            {"heading": None, "kind": "unsectioned",
             "items": [_cap_item(t) for t in items[:MODULE_NOTE_ITEMS_CAP]]}]}]
    old_by_key = {}
    for e in old:
        old_by_key.setdefault(_entry_key(e), e)
    out = []
    for e in new:
        prior = old_by_key.get(_entry_key(e))
        if prior is None:
            out.append(e)
            continue
        seen = {}
        for s in prior["sections"]:
            seen.setdefault((s["heading"] or "").lower(), set()).update(
                i["text"] for i in s["items"])
        sections = []
        for s in e["sections"]:
            known = seen.get((s["heading"] or "").lower(), set())
            items = [i for i in s["items"] if i["text"] not in known]
            if items:
                kept = {k: v for k, v in s.items()
                        if k not in ("items", "placeholder")}
                kept["items"] = items
                sections.append(kept)
        if sections:
            out.append(dict(e, sections=sections, amended=True))
    return out


def parse_version_file(text):
    """The `version` of a version.json text, or None. Pure."""
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("version"), (str, int, float)) \
            and not isinstance(doc.get("version"), bool):
        return str(doc["version"])
    return None


def parse_version_tags(raw, from_date, to_date):
    """The window's version tags from `git for-each-ref` output in
    TAG_LIST_FORMAT. Pure.

    A version tag's last path segment is a version (`v1.2.0`, `0.6.0`); the
    rest is the module path it publishes (`avm/res/x/y/0.6.0` -> module
    `avm/res/x/y`; `v1.2.0` -> module "" = the repository root). Its date is
    the tagger date of an annotated tag (`date_source: "tagger"`), else the
    committer date of the tagged commit (`"commit"`: a lightweight tag holds
    no date of its own), as a UTC timestamp. Tags dated in [from, to] are
    returned, each with `first`: no version tag of the same module in the
    clone is dated before `from`. -> {"items": [...], "undated": n}."""
    rows = []
    undated = 0
    for line in (raw or "").split("\n"):
        cols = line.split("\x1f")
        if len(cols) < 5 or not cols[0]:
            continue
        name, otype, created, oid, peeled = cols[:5]
        module, _, last = name.rpartition("/")
        if not _VERSION_TAG_RE.match(last):
            continue
        stamp = _utc_timestamp(created) if created else None
        if stamp is None:
            undated += 1
            continue
        rows.append({"tag": name, "module": module, "version": last,
                     "date": stamp,
                     "date_source": "tagger" if otype == "tag" else "commit",
                     "commit": peeled or oid})
    earlier = {r["module"] for r in rows
               if not in_window(r["date"], from_date, None)}
    items = [dict(r, first=r["module"] not in earlier) for r in rows
             if in_window(r["date"], from_date, to_date)]
    items.sort(key=lambda r: (r["date"], r["tag"]))
    return {"items": items, "undated": undated}


def git_blob_reader(clone_dir, run=None):
    """The production `read_blob(rev, path)` seam: the text of `path` at
    `rev`, None when the commit has no such file (or `rev` does not resolve:
    a root commit's parent), BlobUnreadable when the file is there but git
    could not read it. Trees are local in the partial clone, so the lookup
    never needs the network; the blob may be fetched (the patch walk usually
    fetched it already)."""
    run = run or run_git

    def read(rev, path):
        if "\n" in path:
            return None
        try:
            oid = run(["git", "-C", clone_dir, "rev-parse", "--verify", "-q",
                       f"{rev}:{path}"]).strip()
        except RuntimeError:
            return None
        if not oid:
            return None
        try:
            return run(["git", "-C", clone_dir, "cat-file", "blob", oid])
        except RuntimeError as exc:
            raise BlobUnreadable(str(exc).splitlines()[0]) from None
    return read


def git_tag_lister(clone_dir, run=None):
    """The production `list_tags()` seam: one local `git for-each-ref
    refs/tags` (no network: the tags the clone/fetch brought along)."""
    run = run or run_git
    return lambda: run(["git", "-C", clone_dir, "for-each-ref", "refs/tags",
                        f"--format={TAG_LIST_FORMAT}"])


def _note_file_kind(path):
    base = (path or "").rsplit("/", 1)[-1].lower()
    if base in CHANGELOG_FILE_NAMES:
        return "changelog"
    if base in VERSION_FILE_NAMES:
        return "version"
    return None


def collect_module_notes(code_events, read_blob, list_tags, from_date, to_date):
    """The window's `module_notes` fact. Pure given its two seams.

    For every file-level code event (the window's, boundary commits already
    dropped) of a changelog or version file that was added, modified,
    renamed or copied: the file at the commit and at its first parent (at
    `old_path` for a rename; none for an add or copy) are compared.
    - `changelogs`: [{path, module (the file's directory, "" at the root),
      commit, date, change, [old_path], [new_file], entries
      (changelog_added_entries, at most MODULE_NOTE_ENTRIES_CAP),
      [entries_omitted]}] — only commits that added something;
    - `versions`: [{path, module, commit, date, change, [old_path], from,
      to}] — version.json `version` before -> after, when it moved (or the
      file was added with one);
    - `tags`: {"collected": true, "source": "clone", "items", ["undated"]}
      (parse_version_tags), or {"collected": false, "reason"} when the clone
      holds no tag or listing them failed;
    - `unread` (only when any): [{path, commit, date, reason}] file versions git
      could not read (with the commit date) — a visible gap, never guessed.
    Deleted files add nothing (the code events record the deletion)."""
    changelogs, versions, unread = [], [], []
    for ev in code_events:
        path, change = ev.get("path"), ev.get("change")
        kind = _note_file_kind(path)
        if kind is None or change not in ("add", "modify", "rename", "copy"):
            continue
        sha, old_path = ev.get("commit"), ev.get("old_path")
        try:
            after = read_blob(sha, path)
            before = None if change in ("add", "copy") else \
                read_blob(f"{sha}^", old_path or path)
        except BlobUnreadable as exc:
            unread.append({"path": path, "commit": sha, "date": ev.get("date"),
                           "reason": str(exc)})
            continue
        if after is None:
            unread.append({"path": path, "commit": sha, "date": ev.get("date"),
                           "reason": "the file is not in the commit"})
            continue
        rec = {"path": path, "module": path.rpartition("/")[0],
               "commit": sha, "date": ev.get("date"), "change": change}
        if old_path:
            rec["old_path"] = old_path
        if kind == "changelog":
            entries = changelog_added_entries(before, after)
            if not entries:
                continue
            if before is None:
                rec["new_file"] = True
            rec["entries"] = entries[:MODULE_NOTE_ENTRIES_CAP]
            if len(entries) > MODULE_NOTE_ENTRIES_CAP:
                rec["entries_omitted"] = len(entries) - MODULE_NOTE_ENTRIES_CAP
            changelogs.append(rec)
        else:
            vb = parse_version_file(before) if before is not None else None
            va = parse_version_file(after)
            if va is not None and va != vb:
                versions.append(dict(rec, **{"from": vb, "to": va}))
    try:
        raw_tags = list_tags() if list_tags is not None else None
    except RuntimeError as exc:
        tags = {"collected": False,
                "reason": "listing tags failed: {}".format(
                    str(exc).splitlines()[0] if str(exc) else "error")}
    else:
        if not (raw_tags or "").strip():
            tags = {"collected": False, "reason": "the clone holds no tags"}
        else:
            parsed = parse_version_tags(raw_tags, from_date, to_date)
            tags = {"collected": True, "source": "clone",
                    "items": parsed["items"]}
            if parsed["undated"]:
                tags["undated"] = parsed["undated"]
    notes = {"collected": True, "changelogs": changelogs,
             "versions": versions, "tags": tags}
    if unread:
        notes["unread"] = unread
    return notes


# --- PR-internal history (per-window fact `pr_history`) ---------------------
#
# The base-branch walk sees one commit per squash-merged PR, so the work inside
# a PR (its first attempt, the rework after a review, a comment added and then
# removed) is not in it. GitHub keeps every PR's commits at
# `refs/pull/<N>/head`, fetchable with plain git after the merge. gather lists
# those refs once (`ls-remote`), fetches the window's PR heads in batches into
# `refs/remotes/pr/<N>` (blobless, like the main clone), lists each PR's own
# commits (reachable from its head, not from the walked branch) and, for the
# PRs most likely to carry a story, the comment / parameter adds and removes
# between those commits (the patch walk's own symbol extraction).

# Refspecs per `git fetch`. One fetch per batch keeps the command line short and
# lets one failed batch be recorded without losing the others.
PR_HISTORY_FETCH_BATCH = 100
# Commits kept per PR (oldest first); the rest are counted in commits_omitted.
# GitHub's own PR commit list stops at 250.
PR_HISTORY_MAX_COMMITS = 250
# The comment / parameter walk (a `git log -p` over the PR's commits, which
# fetches the blobs of the .bicep / .tf files they touch) runs only for PRs
# with human conversation (the gather-time sign of a story), at most this many
# PRs per window, the most discussed first ...
PR_HISTORY_NOTES_MAX_PRS = 40
# ... and only for PRs of at most this many commits.
PR_HISTORY_NOTES_MAX_COMMITS = 60
# Comment / parameter events kept per PR (the newest; notes_omitted counts the
# oldest left out). A new module's first commit alone can add a few hundred.
PR_HISTORY_NOTES_MAX = 400
# The symbol kinds the walk keeps: comments (developer notes, parameter
# descriptions) and the interface (parameters, Terraform variables, outputs).
PR_HISTORY_NOTE_SUBKINDS = ("comment", "todo", "param", "variable", "output")
# One header per PR commit: sha, parents, author name, author timestamp,
# committer timestamp, subject, then the message body, closed by a group
# separator so the body's own lines are not read as file names.
PR_COMMIT_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%aI%x1f%cI%x1f%s%x1f%b%x1d"
_PR_BODY_END = "\x1d"
# Paths kept per PR commit (the change summary's file list; `paths_omitted`
# counts the rest). A module's first commit touches a few dozen files.
PR_HISTORY_PATHS_MAX = 60
# Characters of a PR commit's message body kept (`body_truncated_from` when
# cut). The body is where a commit says why ("narrow v0.1 scope ... because").
# Trailer lines at its end (`Co-authored-by: ...`, `Signed-off-by: ...`) are
# not part of the explanation and are dropped first.
PR_HISTORY_BODY_CAP = 1500
# The patch excerpts kept for the walked PRs: what each commit added to /
# removed from a changelog, and from shared repository files (CI workflows,
# actions, pipeline utilities) that every module depends on.
PR_HISTORY_PATCH_PATHSPECS = ("*CHANGELOG.md", ".github/", "utilities/",
                              ".azuredevops/", ".pipelines/", "pipelines/")
# Lines kept per file per commit (added + removed; `lines_omitted` the rest),
# characters per line, and characters per PR (the newest excerpts are kept,
# `patches_omitted` counts the oldest left out).
PR_HISTORY_PATCH_LINES = 40
PR_HISTORY_PATCH_LINE_CAP = 500
PR_HISTORY_PATCH_CHARS = 8000
# A git trailer line: a hyphenated token (`Co-authored-by`, `Signed-off-by`,
# `Change-Id`), a colon, a value. "Note: ..." is prose, not a trailer.
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+: \S")


def parse_pr_head_refs(raw):
    """`git ls-remote origin 'refs/pull/*/head'` output -> {number: sha}. Pure."""
    out = {}
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        m = re.fullmatch(r"refs/pull/(\d+)/head", parts[1])
        if m:
            out[int(m.group(1))] = parts[0]
    return out


def pr_commit_body(text):
    """A PR commit's message body for the story material: the trailing block
    of trailer lines (`Token: value`, e.g. Co-authored-by / Signed-off-by)
    dropped, surrounding blank lines stripped, capped at PR_HISTORY_BODY_CAP.
    Returns (body, original length) — ("", 0) when nothing is left. Pure."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    while True:
        while lines and not lines[-1].strip():
            lines.pop()
        end = len(lines)
        while end and lines[end - 1].strip() and _TRAILER_RE.match(lines[end - 1]):
            end -= 1
        if end < len(lines) and (end == 0 or not lines[end - 1].strip()):
            lines = lines[:end]          # a whole trailer paragraph: drop it
            continue
        break
    body = "\n".join(lines).strip()
    if not body:
        return "", 0
    return body[:PR_HISTORY_BODY_CAP], len(body)


def parse_pr_commits(raw):
    """A `git log --format=PR_COMMIT_FORMAT --name-only` listing -> commits
    [{sha, parents, author, authored_at, committed_at, subject, files,
    [paths], [paths_omitted], [body], [body_truncated_from]}] in the
    listing's order. `parents` is a count (2 for a merge), `files` the
    number of paths the commit changed (a merge lists none), `paths` those
    paths (at most PR_HISTORY_PATHS_MAX), `body` the message body
    (pr_commit_body); both omitted when empty. A listing in the older
    format (no body, no closing separator) still parses, without `paths`
    and `body`. Pure."""
    out = []
    for lines in _log_records(raw):
        chunk = "\n".join(lines)
        if _PR_BODY_END in chunk:
            head, _, names = chunk.partition(_PR_BODY_END)
            fields = head.split(FIELD_SEP, 6)
            if len(fields) < 7:
                continue
            paths = [ln.strip("\r") for ln in names.split("\n") if ln.strip()]
            body, body_len = pr_commit_body(fields[6])
            subject = fields[5]
        else:
            fields = lines[0].split(FIELD_SEP, 5)
            if len(fields) < 6:
                continue
            paths, body, body_len = None, "", 0
            subject = fields[5]
            names_count = len([ln for ln in lines[1:] if ln.strip()])
        sha, parents, author, authored, committed = (f.strip() for f in fields[:5])
        row = {
            "sha": sha, "parents": len(parents.split()), "author": author,
            "authored_at": _utc_timestamp(authored),
            "committed_at": _utc_timestamp(committed),
            "subject": subject.strip(" \t\r\n"),
            "files": len(paths) if paths is not None else names_count,
        }
        if paths:
            row["paths"] = paths[:PR_HISTORY_PATHS_MAX]
            if len(paths) > PR_HISTORY_PATHS_MAX:
                row["paths_omitted"] = len(paths) - PR_HISTORY_PATHS_MAX
        if body:
            row["body"] = body
            if body_len > len(body):
                row["body_truncated_from"] = body_len
        out.append(row)
    return out


def _patch_kind(path):
    return "changelog" if (path or "").rsplit("/", 1)[-1].lower() \
        == "changelog.md" else "shared"


def parse_pr_patches(raw):
    """A `git log -p --format=%x1e%H` listing over a PR's commits, limited to
    PR_HISTORY_PATCH_PATHSPECS -> [{commit, path, kind (`changelog` /
    `shared`), added: [line], removed: [line], [lines_omitted]}] in the
    listing's order: what each commit added to and removed from a changelog
    or a shared repository file, at most PR_HISTORY_PATCH_LINES lines per
    file and commit, each line capped at PR_HISTORY_PATCH_LINE_CAP. A file
    whose diff has no +/- line (a mode change, a binary file) is left out.
    Pure."""
    out, cur, sha = [], None, None

    def close():
        if cur is not None and (cur["added"] or cur["removed"]
                                or cur.get("lines_omitted")):
            out.append(cur)
    for lines in _log_records(raw):
        sha = lines[0].strip()
        for ln in lines[1:]:
            if ln.startswith("diff --git "):
                close()
                m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
                path = m.group(2) if m else ln[len("diff --git "):]
                cur = {"commit": sha, "path": path, "kind": _patch_kind(path),
                       "added": [], "removed": []}
            elif cur is None or ln.startswith(("+++ ", "--- ")):
                continue
            elif ln.startswith(("+", "-")):
                key = "added" if ln[0] == "+" else "removed"
                if len(cur["added"]) + len(cur["removed"]) \
                        >= PR_HISTORY_PATCH_LINES:
                    cur["lines_omitted"] = cur.get("lines_omitted", 0) + 1
                    continue
                cur[key].append(ln[1:][:PR_HISTORY_PATCH_LINE_CAP])
        close()
        cur = None
    return out


def _patch_chars(p):
    return sum(len(x) for x in p["added"] + p["removed"]) + len(p["path"])


def _shallow_parent_exclusions(clone_dir, run):
    """The real parents of the clone's shallow boundary commits that exist
    locally, sorted (revisions to list after `--not`, which already negates
    them). The walked branch is cut at the boundary (its
    oldest commit has no parents in a shallow clone), so `<pr head> --not HEAD`
    would list the base-branch commits older than the boundary that a PR head
    reaches (fetched with it); excluding the boundary commits' real parents
    (read from the commit object, which keeps them) leaves the PR's own."""
    try:
        path = run(["git", "-C", clone_dir, "rev-parse", "--git-path",
                    "shallow"]).strip()
    except RuntimeError:
        return []
    if path and not os.path.isabs(path):
        path = os.path.join(clone_dir, path)
    try:
        with open(path, encoding="utf-8") as fh:
            boundary = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []
    out = []
    for sha in boundary:
        try:
            body = run(["git", "-C", clone_dir, "cat-file", "commit", sha])
        except RuntimeError:
            continue
        for line in body.splitlines():
            if not line:
                break
            if line.startswith("parent "):
                parent = line.split()[1]
                try:
                    run(["git", "-C", clone_dir, "cat-file", "-e",
                         parent + "^{commit}"])
                except RuntimeError:
                    continue
                out.append(parent)
    return sorted(set(out))


def _human_conversation(pr):
    """How many human (non-bot) comments and review comments a PR has."""
    return sum(1 for key in ("comments_list", "review_comments")
               for c in pr.get(key) or []
               if isinstance(c, dict)
               and not derive.is_bot_login(c.get("author")))


def collect_pr_history(prs, clone_dir, base_ref="HEAD", run=None,
                       base_branch=None):
    """The window's `pr_history` fact. All git goes through `run` (default
    run_git, which retries a dropped connection); a refusal is recorded,
    never fatal.

    1. `git ls-remote origin refs/pull/*/head` lists the PR head refs once.
       A PR of the window without one is `status: "missing"`.
    2. The listed heads are fetched in batches of PR_HISTORY_FETCH_BATCH
       (`git fetch --filter=blob:none --no-tags origin
       +refs/pull/<N>/head:refs/remotes/pr/<N> ...`); every PR of a batch
       whose fetch failed is `status: "fetch_failed"`.
    3. Each fetched PR's own commits: `git log --reverse --date-order` of
       its head, not reachable from `base_ref` (the walked branch) nor from
       the real parents of the shallow boundary, at most
       PR_HISTORY_MAX_COMMITS (the rest counted in `commits_omitted`). No
       commit at all means the head is on the walked branch (`on_base`:
       a merge-commit merge, whose commits the base walk already has).
    4. For the most discussed PRs (human comments and review comments > 0,
       at most PR_HISTORY_NOTES_MAX_PRS of them, each of at most
       PR_HISTORY_NOTES_MAX_COMMITS commits) a `git log -p` over the same
       commits, limited to `*.bicep` / `*.tf`, run through the patch walk's
       symbol extraction; the comment / parameter / variable / output events
       are `notes` (the newest PR_HISTORY_NOTES_MAX; `notes_omitted` counts
       the oldest left out).
       A merge commit has no patch. Every other PR records `notes_skipped`
       with the reason.

    Returns {"collected": True, "base": base_branch, "prs": [per PR, by
    number], "limits": {...}} or, when the refs cannot be listed,
    {"collected": True, "base", "error", "prs": []}."""
    run = run or run_git
    numbers = sorted({p["number"] for p in prs if isinstance(p.get("number"), int)})
    limits = {"max_commits": PR_HISTORY_MAX_COMMITS,
              "notes_max_prs": PR_HISTORY_NOTES_MAX_PRS,
              "notes_max_commits": PR_HISTORY_NOTES_MAX_COMMITS,
              "notes_max": PR_HISTORY_NOTES_MAX}
    out = {"collected": True, "base": base_branch, "prs": [], "limits": limits}
    if not numbers:
        return out
    try:
        heads = parse_pr_head_refs(run(["git", "-C", clone_dir, "ls-remote",
                                        "origin", "refs/pull/*/head"]))
    except RuntimeError as exc:
        out["error"] = "listing the PR head refs failed: {}".format(
            str(exc).splitlines()[-1] if str(exc) else "unknown error")
        return out
    rows = {n: {"number": n} for n in numbers}
    available = [n for n in numbers if n in heads]
    for n in numbers:
        if n not in heads:
            rows[n]["status"] = "missing"
    fetched = []
    for i in range(0, len(available), PR_HISTORY_FETCH_BATCH):
        batch = available[i:i + PR_HISTORY_FETCH_BATCH]
        specs = ["+refs/pull/{0}/head:refs/remotes/pr/{0}".format(n) for n in batch]
        try:
            run(["git", "-C", clone_dir, "fetch", "--quiet", "--filter=blob:none",
                 "--no-tags", "--no-write-fetch-head", "origin"] + specs)
        except RuntimeError as exc:
            reason = (str(exc).splitlines() or ["fetch failed"])[-1][:200]
            for n in batch:
                rows[n].update(status="fetch_failed", reason=reason)
            continue
        fetched.extend(batch)
    exclude = _shallow_parent_exclusions(clone_dir, run) if fetched else []
    for n in fetched:
        row = rows[n]
        row.update(status="fetched", head=heads[n])
        ref = "refs/remotes/pr/{}".format(n)
        try:
            listing = run(["git", "-C", clone_dir, "log", "--reverse",
                           "--date-order", "--format=" + PR_COMMIT_FORMAT,
                           "--name-only", "--no-renames"] + list(_WALK_PIN_ARGS)
                          + [ref, "--not", base_ref] + exclude + ["--"])
        except RuntimeError as exc:
            row.update(status="log_failed",
                       reason=(str(exc).splitlines() or ["log failed"])[-1][:200])
            continue
        commits = parse_pr_commits(listing)
        if not commits:
            row["on_base"] = True
        if len(commits) > PR_HISTORY_MAX_COMMITS:
            row["commits_omitted"] = len(commits) - PR_HISTORY_MAX_COMMITS
            commits = commits[:PR_HISTORY_MAX_COMMITS]
        row["commits"] = commits
    by_number = {p["number"]: p for p in prs if isinstance(p.get("number"), int)}
    candidates = sorted(
        (n for n in fetched if rows[n].get("status") == "fetched"),
        key=lambda n: (-_human_conversation(by_number.get(n, {})), n))
    walked = 0
    for n in candidates:
        row = rows[n]
        commits = row.get("commits") or []
        if not commits:
            row["notes_skipped"] = "no commits of its own"
        elif _human_conversation(by_number.get(n, {})) == 0:
            row["notes_skipped"] = "no human conversation"
        elif len(commits) + row.get("commits_omitted", 0) \
                > PR_HISTORY_NOTES_MAX_COMMITS:
            row["notes_skipped"] = "more than {} commits".format(
                PR_HISTORY_NOTES_MAX_COMMITS)
        elif walked >= PR_HISTORY_NOTES_MAX_PRS:
            row["notes_skipped"] = "over the {}-PR cap of the window".format(
                PR_HISTORY_NOTES_MAX_PRS)
        else:
            walked += 1
            try:
                raw = run(["git", "-C", clone_dir, "log"] + list(PATCH_WALK_ARGS)
                          + ["refs/remotes/pr/{}".format(n), "--not", base_ref]
                          + exclude + ["--", "*.bicep", "*.tf"])
            except RuntimeError as exc:
                row["notes_skipped"] = "patch walk failed: {}".format(
                    (str(exc).splitlines() or ["?"])[-1][:200])
                continue
            notes = [{k: e[k] for k in ("commit", "path", "subkind", "change",
                                        "name", "before", "after")}
                     for e in parse_symbol_events(raw)
                     if e.get("subkind") in PR_HISTORY_NOTE_SUBKINDS]
            if len(notes) > PR_HISTORY_NOTES_MAX:
                # The oldest go first: the first commit of a new module adds
                # every parameter and description, which the merged result
                # (the base walk) carries too; the later rework is what only
                # this history has.
                row["notes_omitted"] = len(notes) - PR_HISTORY_NOTES_MAX
                notes = notes[-PR_HISTORY_NOTES_MAX:]
            row["notes"] = notes
            _collect_pr_patches(row, n, clone_dir, base_ref, exclude, run)
    out["prs"] = [rows[n] for n in numbers]
    return out


def _collect_pr_patches(row, n, clone_dir, base_ref, exclude, run):
    """Add `patches` (parse_pr_patches; the newest within
    PR_HISTORY_PATCH_CHARS, `patches_omitted` the oldest left out) to a
    walked PR's row: what its commits wrote into a changelog or a shared
    repository file. Omitted when there is none; a failed walk is recorded
    as `patches_skipped`, never fatal."""
    try:
        raw = run(["git", "-C", clone_dir, "log", "--reverse", "-p",
                   "--unified=0", "--format=" + RECORD_SEP + "%H", "--no-renames"]
                  + list(_WALK_PIN_ARGS) + ["--src-prefix=a/", "--dst-prefix=b/",
                                            "refs/remotes/pr/{}".format(n),
                                            "--not", base_ref] + exclude
                  + ["--"] + list(PR_HISTORY_PATCH_PATHSPECS))
    except RuntimeError as exc:
        row["patches_skipped"] = "patch walk failed: {}".format(
            (str(exc).splitlines() or ["?"])[-1][:200])
        return
    patches = parse_pr_patches(raw)
    kept, used = [], 0
    for p in reversed(patches):
        c = _patch_chars(p)
        if used + c > PR_HISTORY_PATCH_CHARS:
            break
        kept.append(p)
        used += c
    kept.reverse()
    if len(kept) < len(patches):
        row["patches_omitted"] = len(patches) - len(kept)
    if kept:
        row["patches"] = kept


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
    # A *.tf file in a NESTED non-scaffold dir (modules/<name>/inner/x.tf) is a
    # module of its own, as terraform reads one directory as one module: its area
    # is that dir (#101). Other files there, and nested examples/tests, stay in
    # modules/<name>.
    if len(parts) >= 3 and parts[0] == "modules":
        nested = "/".join(parts[:-1])
        if len(parts) > 3 and parts[-1].endswith(".tf") \
                and not _is_scaffold_area(nested):
            return nested
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


def bicep_module_dirs(paths):
    """The Bicep module directories among `paths` (issue #107): the directory of
    every `main.bicep` that is not under `examples/`/`tests/` scaffolding, not
    in the AVM layout (`_avm_area` already gives every file there its module)
    and not at the repo root (a root `main.bicep` would swallow the whole
    repo; it keeps its own `main.bicep` area). Pure; a frozenset."""
    out = set()
    for p in paths:
        head, sep, name = (p or "").rpartition("/")
        if not sep or not head or name != "main.bicep":
            continue
        if _is_scaffold_path(p) or _avm_area(p.split("/")):
            continue
        out.add(head)
    return frozenset(out)


def _bicep_module_dir_rule(module_dirs):
    """Area predicate for `module_area_patterns`: a file at or below a Bicep
    module directory (the deepest one that holds it) belongs to it. Terraform
    files keep the Terraform rules (one directory is one Terraform module), and
    a file in an `examples/`/`tests/` subtree below the module directory keeps
    today's classification (scaffolding is never the module itself)."""
    def rule(parts):
        if parts[-1].endswith(".tf"):
            return None
        for i in range(len(parts) - 1, 0, -1):
            d = "/".join(parts[:i])
            if d in module_dirs:
                if _is_scaffold_area("/".join(parts[i:-1])):
                    return None
                return d
        return None
    return rule


def module_area_patterns(module_dirs, patterns=None):
    """`patterns` (default DEFAULT_AREA_PATTERNS) with the Bicep module
    directory rule for `module_dirs` (bicep_module_dirs) slotted in just
    before `main_bicep`, i.e. after the AVM rule. With no module dirs the
    given list itself is returned, so callers keep today's behaviour. Pure."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    if not module_dirs:
        return patterns
    rule = ("bicep_module_dir", _bicep_module_dir_rule(frozenset(module_dirs)))
    out = []
    for name, fn in patterns:
        if name == "main_bicep":
            out.append(rule)
        out.append((name, fn))
    return out


def classify_code_area(path, patterns, module_dirs=None):
    """Map a tracked file path to a single area id (a directory path), or None.

    Tries the ordered `patterns` (AVM module subtree, any main.bicep dir, Terraform
    modules/<name>, any *.tf dir, else the top 2 segments of the file's directory;
    a repo-root file keeps its own name). With `module_dirs` (bicep_module_dirs)
    a file at or below a Bicep module directory belongs to that module
    (module_area_patterns); without it, today's per-path rules. Pure."""
    if not path:
        return None
    if module_dirs:
        patterns = module_area_patterns(module_dirs, patterns)
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


def build_directory_areas(paths, patterns, module_dirs=None):
    """Fold a list of tracked file paths into the `code_graph` directory provider.

    Shape: {"provider":"directory","areas":[{"id","label","paths":[...],"edges":[]}]}.
    Area id is the directory path; label is its tail; edges are deferred (empty).
    `module_dirs` as in classify_code_area. Deterministic (areas sorted by id,
    paths sorted). Pure."""
    if module_dirs:
        patterns = module_area_patterns(module_dirs, patterns)
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


def _read_json_file(path, root=None):
    """JSON from a file; with `root`, refused when the file resolves outside it
    (as _read_text_file, issue #109)."""
    if root is not None and not _inside_root(path, root):
        raise RepoFileRefused("{} resolves outside {}".format(path, root))
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
    if read_json is _read_json_file:
        def read_json(path):
            return _read_json_file(path, root=clone_dir)
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

# Matched on a Bicep "mask" of the source (_bicep_mask: comments and string
# contents blanked at the same offsets, top-level quotes kept), so a `module`
# inside a comment or a string never matches (issue #109). The keyword must
# start a statement: the start of a line after whitespace, optionally after
# decorators on the same line (decorators on earlier lines are their own
# lines). The ref's text is read from the original source at the match span.
_BICEP_MODULE_RE = re.compile(
    r"(?m)^[ \t]*(?:@[\w.]+(?:\((?:[^()\n]|\([^()\n]*\))*\))?[ \t]+)*"
    r"module\s+\w+\s+'(?P<ref>[^'\n]*)'(?:\s*=\s*(?P<open>[\[{]))?")


def _bicep_mask(text):
    """Return Bicep `text` with comments and string-literal contents replaced
    by spaces (newlines kept), so offsets are unchanged -- the Bicep
    counterpart of _hcl_mask.

    Handles `//` line comments, `/* */` block comments, `'...'` strings with
    `\\` escapes and `${...}` interpolation (nested braces, nested strings and
    comments inside the expression), and `'''...'''` multi-line strings (no
    escapes, no interpolation; blanked whole, quotes included). The quote
    characters of a top-level single-quoted string are kept so a module ref's
    span can be located. An unterminated string ends at the newline; an
    unterminated block comment or multi-line string runs to the end. Pure."""
    out = list(text)
    n = len(text)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    stack = []          # "str" | [interp_brace_depth]; empty = code context
    i = 0
    while i < n:
        c = text[i]
        if stack and stack[-1] == "str":
            if c == "\\":
                blank(i, i + 2)
                i += 2
            elif text.startswith("${", i):
                blank(i, i + 2)
                stack.append([0])
                i += 2
            elif c == "'" or (c == "\n" and len(stack) == 1):
                stack.pop()
                if stack and c == "'":
                    blank(i, i + 1)                     # nested string's quote
                i += 1
            else:
                blank(i, i + 1)
                i += 1
            continue
        if text.startswith("//", i):
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
        if text.startswith("'''", i):
            j = text.find("'''", i + 3)
            j = n if j < 0 else j + 3
            blank(i, j)
            i = j
            continue
        if stack:                                       # inside an interpolation
            top = stack[-1]
            if c == "'":
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
        if c == "'":
            stack.append("str")
        i += 1
    return "".join(out)


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


def _bicep_ref_parts(ref):
    """(registry_path, version, local_path) of one Bicep module ref string.

    Only a PUBLIC Bicep registry ref (`br/public:<path>:<tag>`, or the same
    registry named explicitly, `br:mcr.microsoft.com/bicep/<path>:<tag>`; the host
    compares case-insensitively) sets `registry_path`. A private registry, a
    template spec or a user alias sets neither path (only its `version`). A ref
    with no scheme is a `local_path`. Pure; shared by the source parser and the
    multi-repo fold, which sees only the raw `ref` stored on an edge."""
    registry_path = version = local_path = None
    sm = _BICEP_SCHEME_RE.match(ref or "")
    if sm:
        path_part, version = _split_ref_tag(sm.group("body"))
        alias = sm.group("alias")
        if sm.group("scheme") == "br" and alias == "public":
            registry_path = path_part
        elif (sm.group("scheme") == "br" and alias is None
              and path_part.lower().startswith(_BICEP_PUBLIC_REGISTRY)):
            registry_path = path_part[len(_BICEP_PUBLIC_REGISTRY):]
        # else: private registry / template spec / user alias -> raw ref only
    elif ref:
        local_path = ref
    return registry_path, version, local_path


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
    `= {`, else None. A `module` inside a comment or a string is not a
    reference (matched on _bicep_mask; issue #109). Pure."""
    refs = []
    text = source_text or ""
    for m in _BICEP_MODULE_RE.finditer(_bicep_mask(text)):
        ref = text[m.start("ref"):m.end("ref")]
        if not ref:
            continue
        open_ch = m.group("open")
        instances = "many" if open_ch == "[" else "one" if open_ch == "{" else None
        registry_path, version, local_path = _bicep_ref_parts(ref)
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


def build_bicep_edges(source_text, arm_json, base_path, area_ids, patterns=None,
                      module_exists=None, read_local=None):
    """Build inter-area dependency edges for one Bicep area.

    Immediate edges (transitive=False) come from the SOURCE refs — fully identified
    (area-id + version), build-validated by the caller. They correspond to the
    DEPTH-1 module instantiations in the ARM tree. No transitive edges are
    produced (see the note at the end of the body); `arm_json` is accepted for
    the callers' sake and not read. Pure; deterministic (deduped, then sorted)."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    edges = []
    seen = set()
    self_id = classify_code_area(base_path, patterns)

    # The entry file, then every Bicep file of THIS area it reaches through
    # local references (`read_local(rel_path) -> text | None`; without it only
    # the entry file is read). A landing zone's `main.bicep` often does little
    # but call `./resources.bicep`, which holds the real module calls.
    entry_path = os.path.normpath(base_path or "")
    queue, visited = [(source_text, base_path)], {entry_path}
    while queue:
        text, from_path = queue.pop(0)
        for ri in parse_bicep_module_refs(text):
            to = resolve_module_ref(ri, from_path, patterns, area_ids, module_exists)
            local = False
            # A local relative ref that resolves back to THIS area is one of its
            # own files, not a dependency on itself. A file in a subdirectory is
            # a private child (e.g. `nat-rule/main.bicep`): name the child node
            # (`<area>/<child>`) so the graph shows which private submodule is
            # used. A file beside the entry file (`./resources.bicep`) is just
            # more of the module: no edge, its own references are followed. A
            # ref to the entry file itself is genuine recursion and is left as
            # a self edge.
            if ri.get("local_path") and to is not None and to == self_id:
                joined = os.path.normpath(
                    os.path.join(os.path.dirname(from_path or ""), ri["local_path"]))
                if joined != entry_path:
                    if read_local is not None and joined not in visited:
                        visited.add(joined)
                        inner = read_local(joined)
                        if inner is not None:
                            queue.append((inner, joined))
                    child_dir = os.path.dirname(joined)
                    if child_dir != self_id and child_dir.startswith(str(self_id) + "/"):
                        to = child_dir
                        local = True
                    else:
                        continue
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

    # No transitive edges for Bicep. They used to be guessed by matching each
    # nested ARM deployment's `metadata.name` (a human title such as "Key Vault
    # Access Policies") against area names by substring; on real repositories
    # most of those guesses were wrong (a `...-AccessPolicies` deployment
    # became a dependency on `.github/policies`, a Service Bus `subscription`
    # one on an Event Hub namespace). The compiled template carries no module
    # path, so a nested deployment cannot be tied to a module with certainty;
    # the direct edges above come from the source and are exact. What a module
    # reaches indirectly is the closure of the direct edges.
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
    injectable for offline tests; defaults list tracked *.tf via git + read disk.

    A module file that cannot be read (outside the checkout, dangling, not
    readable; `read_text` raising OSError) is skipped with one warning and its
    area carries `"unread": True` (issue #109): its edges come from the files
    that were read, and apply_structural_scan records it as not scanned."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    if list_files is None:
        def list_files():
            return git_ls_files(clone_dir)
    read_text = _guarded_reader(clone_dir, read_text)

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
        texts = [read_text(p) for p in own]
        text = "\n".join(t for t in texts if t is not None)
        out[aid] = {"paths": paths, "edges": _tf_direct_edges(text, mod_dir, patterns)}
        if any(t is None for t in texts):
            out[aid]["unread"] = True
    return out


def _guarded_reader(clone_dir, read_text=None):
    """`rel -> text | None` for the structural scans: the injected `read_text`
    (OSError -> one warning, None) or, by default, _read_repo_text under
    `clone_dir` (never outside it)."""
    if read_text is None:
        return lambda rel: _read_repo_text(clone_dir, rel)

    def guarded(rel):
        try:
            return read_text(rel)
        except OSError as exc:
            sys.stderr.write("warning: skipped {}: {} (not read)\n".format(
                rel, exc.strerror or exc))
            return None
    return guarded


def merge_structural_areas(code_graph, structural):
    """Union static structural areas/edges (scan_structural_terraform_areas or
    scan_structural_bicep_areas) into `code_graph` (whole-tree blast-radius
    augmentation). An in-window area keeps its build-derived edges and gains any
    structural edge not already present (dedup by (to, ref)); module areas the
    in-window walk never saw are appended (deterministic by id) with their IaC
    paths + edges. Mutates and returns code_graph."""
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


def scan_structural_bicep_areas(clone_dir, patterns=None, list_files=None,
                                read_text=None):
    """Statically discover EVERY Bicep module area in the clone tree and its
    DIRECT `module <sym> '<ref>'` edges, WITHOUT `bicep build` (issue #92).

    The Bicep counterpart of scan_structural_terraform_areas. A module area is an
    area (classify_code_area) whose own entrypoint `<area>/main.bicep` is tracked
    (the repo-root `main.bicep` for the root area); an AVM group area, an area
    with no entrypoint of its own, and `examples/`/`tests/` scaffolding are not
    modules. Its edges are build_bicep_edges over that ONE file with no ARM tree,
    i.e. exactly the build path's immediate (`transitive: false`) edges: a local
    ref resolves to its area (a private child keeps the `<area>/<child>` naming
    and `local`/`instances` markers), a public-registry ref resolves in-repo when
    its module area is one of the tracked module areas, and anything else
    (another repo's module, a private registry, a template spec, an alias) is
    kept unresolved (`to: null`) with its `ref` + `version`. Transitive edges
    need the ARM tree and come only from the build path.

    Cost: one pass over the listing, one read per module entrypoint; no other
    file is opened. Returns {area_id: {"paths": [the area's non-scaffold .bicep
    files], "edges": [...]}}. `list_files`/`read_text` are injectable for offline
    tests; defaults list tracked files via git and read from disk. An entry
    file that cannot be read (as in scan_structural_terraform_areas) gives
    its area no edges and `"unread": True` (issue #109)."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    if list_files is None:
        def list_files():
            return git_ls_files(clone_dir)
    read_text = _guarded_reader(clone_dir, read_text)

    by_area, entry = {}, {}
    for rel in list_files():
        if not rel.endswith(".bicep"):
            continue
        aid = classify_code_area(rel, patterns)
        if aid is None or _is_scaffold_area(aid) or _is_scaffold_path(rel):
            continue
        by_area.setdefault(aid, set()).add(rel)
        if rel == _rel_join(_area_rel_dir(aid, _BICEP_ROOT_ID), "main.bicep"):
            entry[aid] = rel

    modules = {aid for aid in entry if not _is_avm_group_area(aid)}
    out = {}
    for aid in sorted(modules):
        src = read_text(entry[aid])
        if src is None:
            out[aid] = {"paths": sorted(by_area[aid]), "edges": [], "unread": True}
            continue
        missed = []

        def read_local(rel, _missed=missed):
            text = read_text(rel)
            if text is None:
                _missed.append(rel)
            return text
        edges = build_bicep_edges(src, None, entry[aid],
                                  modules, patterns,
                                  module_exists=modules.__contains__,
                                  read_local=read_local)
        out[aid] = {"paths": sorted(by_area[aid]), "edges": edges}
        # a file of the area that could not be read: the edges found are kept,
        # but the scan does not speak for the area (no retraction, #109)
        if missed:
            out[aid]["unread"] = True
    return out


def apply_structural_scan(code_graph, clone_dir, patterns=None, list_files=None,
                          read_text=None):
    """Merge the whole-tree Terraform and Bicep module scans into `code_graph`
    (merge_structural_areas; Terraform first, as before). The tracked-file
    listing is taken ONCE and shared by both scans. Runs for manifest members
    and for a single-repo gather given `--structural-iac`. Mutates and returns
    code_graph."""
    if list_files is None:
        files = git_ls_files(clone_dir)
    else:
        files = list(list_files())

    def listed():
        return files
    unread = set()
    for scan in (scan_structural_terraform_areas, scan_structural_bicep_areas):
        found = scan(clone_dir, patterns, list_files=listed, read_text=read_text)
        unread.update(aid for aid, info in found.items() if info.get("unread"))
        merge_structural_areas(code_graph, found)
    # The graph now holds the WHOLE tree's direct module edges: an area with no
    # direct edge here has none in the checkout. Fold relies on this marker to
    # retract stale direct depends_on edges (issue #95); a scan that cannot
    # list the tree raises before it is set. An area with a module file the
    # scan could not read (issue #109) is listed in `structural_scan_unread`
    # and is NOT spoken for by the marker (StructureReplay).
    code_graph["structural_scan"] = True
    if unread:
        code_graph["structural_scan_unread"] = sorted(unread)
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


class RepoFileRefused(OSError):
    """A repo file gather will not read: its real path leaves the clone."""


def _inside_root(path, root):
    """True when `path`, all symlinks resolved, is `root` or below it (root's
    own symlinks resolved too)."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)


def _decode_text(data):
    """Bytes of a repo text file -> str. A UTF-16 / UTF-32 BOM selects that
    codec, a UTF-8 BOM is dropped; anything else is UTF-8 with undecodable
    bytes replaced (U+FFFD), so one latin-1 byte never fails a member. Line
    ends are translated as text-mode `open` does (`\\r\\n` / `\\r` -> `\\n`)."""
    text = None
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8-sig"),
                       (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                       (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            text = data.decode(codec, "replace")
            break
    if text is None:
        text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_text_file(path, root=None):
    """Read one repo text file (issue #109). With `root` (the clone or the
    structure worktree), a path whose real location -- symlinks resolved --
    is outside `root` is refused with RepoFileRefused and never opened: a
    tracked symlink must not make gather read a file of the machine it runs
    on. A symlink that stays inside the tree is followed. Decoding never
    fails (_decode_text). A missing file or dangling symlink raises OSError
    as before; callers that must not fail catch it (_read_repo_text)."""
    if root is not None and not _inside_root(path, root):
        raise RepoFileRefused("{} resolves outside {}".format(path, root))
    with open(path, "rb") as fh:
        return _decode_text(fh.read())


def _read_repo_text(root, rel, warn=None):
    """`_read_text_file(<root>/<rel>, root)`, or None after ONE stderr warning
    naming the file when it is refused (outside the tree) or cannot be read
    (dangling symlink, permission, a directory)."""
    try:
        return _read_text_file(os.path.join(root, rel), root=root)
    except OSError as exc:
        reason = ("it resolves outside the checkout" if isinstance(exc, RepoFileRefused)
                  else exc.strerror or type(exc).__name__)
        (warn or sys.stderr.write)(
            "warning: skipped {}: {} (not read)\n".format(rel, reason))
        return None


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
# The title heuristic (#101) uses the same phrases, except that a bare "support"
# in a title ("Add support for X") is not a question: only "support request" is.
# As an issue-type / label / template value, "support" still means a question.
_TITLE_KIND_PHRASES = tuple(
    ((("support", "request"), "question") if phrase == ("support",) else (phrase, kind))
    for phrase, kind in _KIND_PHRASES)
# A title is a question when it ends with `?` written right after a word (not a
# lone `?`, as in "name contains ?"), or starts with one of these words and
# carries a `?` ("How to pin a version? (v2)").
_INTERROGATIVES = frozenset((
    "how", "what", "why", "when", "where", "which", "who", "is", "are", "can",
    "could", "does", "do", "did", "should", "would", "will"))
_VALID_KINDS = {"feature", "module-request", "bug", "idea", "question", "docs", "other"}
_KIND_WORD_RE = re.compile(r"[a-z0-9]+")


def _kind_words(text):
    """Lowercase ASCII-alphanumeric words of `text` (punctuation, `_`, `-`, `/`
    and emoji are separators)."""
    return tuple(_KIND_WORD_RE.findall((text or "").lower()))


def _kind_from_token(text, phrases=_KIND_PHRASES):
    """Map free text (issue-type name, label value, template stem, title) to a
    kind: the first `phrases` entry occurring as a whole-word sequence."""
    words = _kind_words(text)
    for phrase, kind in phrases:
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
    # 4. title heuristic (title only: bodies mention modules, bugs, "support" ...).
    # A question-shaped title (interrogative start + `?`) is a question; else a
    # kind phrase in the title decides ("Bug: crash when name contains ?"); else
    # a title ending in a word followed by `?` is a question (#101).
    title = (issue.get("title") or "").strip()
    words = _kind_words(title)
    if "?" in title and words and words[0] in _INTERROGATIVES:
        return "question"
    kind = _kind_from_token(title, _TITLE_KIND_PHRASES)
    if kind:
        return kind
    if len(title) >= 2 and title.endswith("?") and not title[-2].isspace():
        return "question"
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
    p.add_argument("--branches", default=None,
                   help="comma-separated branches; the first is the mainline "
                        "(meta.base_branch), fetched and walked when it is not "
                        "the remote default. Default: the repo's default "
                        "branch (from the clone, else the GitHub API).")
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
    p.add_argument("--no-newcomers", dest="newcomers", action="store_false",
                   default=True,
                   help="skip the first-time-contributor check (one or two "
                        "search API calls per author of a PR / issue created "
                        "in the window); the window's `newcomers` fact then "
                        "says it was not collected")
    p.add_argument("--no-affiliation", dest="affiliation_enabled",
                   action="store_false", default=True,
                   help="skip the contributor affiliation check (up to one "
                        "public-membership call per home organisation and "
                        "one profile call per person not decided by the "
                        "override list or author_association); the window's "
                        "`affiliations` fact then says it was not collected")
    p.add_argument("--home-org", action="append", default=[], metavar="ORG",
                   help="a GitHub organisation whose members count as the "
                        "maintainers (repeatable; the repo's owner always "
                        "does). Also matched against profile companies.")
    p.add_argument("--home-name", action="append", default=[], metavar="NAME",
                   help="another company name meaning a home organisation in "
                        "a profile (repeatable; compared after normalisation, "
                        "e.g. 'Microsoft' matches '@microsoft' and 'Microsoft "
                        "Corporation')")
    p.add_argument("--home-email-domain", action="append", default=[],
                   metavar="DOMAIN",
                   help="a commit email domain meaning a home organisation "
                        "(repeatable; checked at gather time, only a boolean "
                        "is kept)")
    p.add_argument("--affiliation", action="append", default=[],
                   metavar="LOGIN=AFFILIATION",
                   help="operator override, highest priority: LOGIN or "
                        "@org/team mapped to maintainer_org, community or "
                        "unknown (repeatable)")
    p.add_argument("--people", default=None, metavar="PATH",
                   help="the workspace's retained people registry (JSON, one "
                        "entry per login; recommended: <stream>/people.json). "
                        "Consulted before any lookup and updated by the "
                        "gather: a person is looked up once and refreshed "
                        "rarely; holds display names, labels, the maintainer "
                        "property per repository and your own edits "
                        "(USAGE 5.7). Beats a manifest's `people`")
    p.add_argument("--people-offline", action="store_true", default=False,
                   help="with --people: no API call for people at all (no "
                        "lookups, no names, no team expansion); the registry "
                        "and the free signals decide, everyone else is "
                        "unknown (not_checked)")
    p.add_argument("--people-refresh-days", type=int, default=None, metavar="N",
                   help="re-check a registry entry looked up more than N days "
                        "ago (default {}; ACTIVITY_PEOPLE_REFRESH_DAYS)".format(
                            people_mod.REFRESH_DAYS))
    p.add_argument("--no-pr-history", dest="pr_history", action="store_false",
                   default=True,
                   help="skip the PR-internal history: fetching every window "
                        "PR's refs/pull/<N>/head (blobless; the first gather of "
                        "a clone also brings the commit and tree history those "
                        "heads reach) and the comment / parameter walk over the "
                        "most discussed PRs' commits; the window's `pr_history` "
                        "fact then says it was not collected")
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
    # reader stage). Roll-up = a wider range_query; resume = a re-gather of the
    # window (its structure is read at the window's end, issue #107; clone_sha
    # records that commit) — both store-native, so the flat-bundle flags are gone.
    # Whole-tree module graph (issue #92): statically scan every tracked
    # Terraform module dir and Bicep `<area>/main.bicep` for direct module deps,
    # not just the areas changed in the window. Always on for --manifest members.
    p.add_argument("--structural-iac", dest="structural_iac", action="store_true",
                   help="also scan the whole tracked tree for Terraform and Bicep "
                        "module dependencies (static, no build), so modules not "
                        "changed in the window still appear with their direct "
                        "edges; always on for --manifest members")
    p.add_argument("--no-iac-build", dest="no_iac_build", action="store_true",
                   help="do not run `bicep restore/build` or `terraform init/"
                        "graph` (they download and run third-party modules and "
                        "providers chosen by the gathered repo); every area is "
                        "recorded edges_status=skipped. The structural scan "
                        "(source parsing) still runs. Use it for repos you do "
                        "not trust.")
    p.add_argument("--store", required=True,
                   help="path to the SQLite journey-graph store to fold into "
                        "(the sole gather deliverable)")
    p.add_argument("--manifest",
                   help="path to a multi-repo project manifest (JSON). Mutually "
                        "exclusive with --owner/--repo; folds every member repo "
                        "into one store under the manifest's logical project name.")
    p.add_argument("--resume", action="store_true",
                   help="with --manifest: gather only the members whose last "
                        "attempt for the manifest's window is not 'ok' (failed "
                        "or not attempted, see meta member_status); members "
                        "already ok for that window are left as they are.")
    args = p.parse_args(argv)
    if args.resume and not args.manifest:
        p.error("--resume needs --manifest")
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
        for text in args.affiliation:
            parse_affiliation_override(text)
        for org in args.home_org:
            manifest_mod.check_owner(org, "--home-org")
        if args.people_offline and not args.people and not args.manifest:
            raise ValueError("--people-offline needs --people PATH (or a "
                             "manifest's `people`)")
        if args.people_refresh_days is not None and args.people_refresh_days < 1:
            raise ValueError("--people-refresh-days must be at least 1")
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


# --- Child-process environment (issue #109) -----------------------------------
#
# Every subprocess gather starts (git, bicep, terraform, graphify) goes through
# run_git and gets the environment `_child_env` builds from os.environ:
#
#   * Secrets removed (item 14): GITHUB_TOKEN, GH_TOKEN, GITHUB_PAT, the GitHub
#     Enterprise token names, ACTIVITY_TEST_TOKEN and every other variable whose
#     name ends in `_TOKEN` or `_PAT`. gather sends the token only in HTTPS
#     headers to api.github.com; the IaC builds run third-party code that
#     must never see it, and git clones without it -- unless the user has a
#     git credential helper configured, which may read GH_TOKEN / GITHUB_TOKEN
#     to clone a private repo (those names are then kept for git alone). Terraform's
#     own registry credentials (`TF_TOKEN_<host>`) do not end in `_TOKEN` and are
#     kept, as is everything else (PATH, HOME, TMPDIR, proxy/TLS variables,
#     TF_PLUGIN_CACHE_DIR, locale, Azure credentials a private `br:` registry
#     restore needs).
#   * git only (item 2): the user's global and system config are not read
#     (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1), so `color.ui`,
#     `diff.noprefix`, `diff.mnemonicPrefix`, `log.showSignature`,
#     `diff.renames`, `clone.defaultRemoteName`, `i18n.logOutputEncoding` and
#     the like cannot change what the walks print; GIT_TERMINAL_PROMPT=0 (fail,
#     never prompt), GIT_PAGER=cat, and LC_ALL=C so the git messages matched
#     below ("couldn't find remote ref", "no commits selected for shallow
#     requests") are the English ones. What a clone/fetch legitimately needs
#     from that config is read ONCE (`_git_passthrough_config`) and re-applied
#     through GIT_CONFIG_COUNT (never argv, so no value appears in a process
#     listing or in run_git's error text): see _GIT_PASSTHROUGH_HTTP_VARS.
#   * terraform only (item 10): TF_CLI_CONFIG_FILE names a CLI config with an
#     explicit `provider_installation` block (`_terraform_cli_config`), which
#     turns off terraform's implied local mirrors, in particular a
#     `terraform.d/plugins` directory shipped inside the gathered repo.
_SECRET_ENV_NAMES = frozenset((
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN", "ACTIVITY_TEST_TOKEN"))
_SECRET_ENV_SUFFIXES = ("_TOKEN", "_PAT")
# Kept for git (only) when the user has a credential helper configured.
_GIT_HELPER_TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                           "GITHUB_ENTERPRISE_TOKEN")

_GIT_ISOLATION_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}

# The user's global/system git config is read for these keys only: settings
# that route or authorise the network connection and never change git's
# output. `http.*` / `http.<url>.*` variables are kept when named here; the
# others (`extraHeader`, `sslCert`, `sslKey`, `proxySSLCert`, `proxySSLKey`,
# `cookieFile`, `userAgent`, ...) are dropped, as is every key outside the
# sections below (`include*` is followed while reading, not copied).
_GIT_PASSTHROUGH_HTTP_VARS = frozenset((
    "proxy", "proxyauthmethod", "proxysslcainfo", "sslcainfo", "sslcapath",
    "sslverify", "sslbackend", "sslversion", "sslcipherlist",
    "schannelcheckrevoke", "schannelusesslcainfo", "version", "postbuffer",
    "lowspeedlimit", "lowspeedtime", "followredirects", "curloptresolve",
    "maxrequests", "minsessions", "noepsv", "emptyauth"))
_GIT_PASSTHROUGH_REGEXP = (r"^(http|https|url|credential|safe|protocol)\."
                           r"|^core\.sshcommand$")
_git_passthrough_cache = None
_terraform_cli_config_cache = None
_child_env_lock = threading.Lock()


def _is_secret_env(name):
    up = name.upper()
    return up in _SECRET_ENV_NAMES or up.endswith(_SECRET_ENV_SUFFIXES)


def _git_passthrough_key(key):
    """True when a global/system git config key is carried over (see
    _GIT_PASSTHROUGH_HTTP_VARS)."""
    low = key.lower()
    section, _, rest = low.partition(".")
    var = low.rsplit(".", 1)[-1]
    if section in ("http", "https"):
        return var in _GIT_PASSTHROUGH_HTTP_VARS
    if section == "url":
        return bool(rest) and var == "insteadof"
    if section == "credential":
        return var in ("helper", "username", "usehttppath")
    if section == "safe":
        return low == "safe.directory"
    if section == "protocol":
        return var == "allow"
    return low == "core.sshcommand"


def _read_git_passthrough_config(env):
    """[(key, value)] from the user's system then global git config (includes
    followed), filtered by _git_passthrough_key. Reads with `env` as given
    (the caller's, without the isolation), so GIT_CONFIG_GLOBAL /
    GIT_CONFIG_NOSYSTEM set by the user are honoured. Any failure (no git, no
    such file) contributes nothing."""
    pairs = []
    for scope in ("--system", "--global"):
        # `git config --system` reads the system file even when the user has
        # switched it off; honour GIT_CONFIG_NOSYSTEM here.
        if scope == "--system" and env.get("GIT_CONFIG_NOSYSTEM", "").lower() \
                in ("1", "true", "yes", "on"):
            continue
        try:
            proc = subprocess.run(
                ["git", "config", scope, "--includes", "-z", "--get-regexp",
                 _GIT_PASSTHROUGH_REGEXP],
                capture_output=True, env=env, cwd=tempfile.gettempdir(),
                timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for entry in proc.stdout.decode("utf-8", "replace").split("\0"):
            if not entry:
                continue
            key, nl, value = entry.partition("\n")
            if _git_passthrough_key(key):
                pairs.append((key, value if nl else "true"))
    return pairs


def _git_passthrough_config():
    """The carried-over git config pairs, read once per process."""
    global _git_passthrough_cache
    with _child_env_lock:
        if _git_passthrough_cache is None:
            env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
            _git_passthrough_cache = _read_git_passthrough_config(env)
        return list(_git_passthrough_cache)


def _env_config_pairs(env):
    """The GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> pairs
    already in `env`, removed from it."""
    try:
        count = int(env.pop("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        count = 0
    pairs = []
    for i in range(max(count, 0)):
        key = env.pop("GIT_CONFIG_KEY_%d" % i, None)
        value = env.pop("GIT_CONFIG_VALUE_%d" % i, "")
        if key:
            pairs.append((key, value))
    return pairs


def _terraform_user_config_path(env):
    """The CLI config terraform would read on its own: TF_CLI_CONFIG_FILE, else
    ~/.terraformrc (%APPDATA%/terraform.rc on Windows). None when absent."""
    path = env.get("TF_CLI_CONFIG_FILE")
    if not path:
        if os.name == "nt":
            path = os.path.join(env.get("APPDATA", ""), "terraform.rc")
        else:
            path = os.path.join(os.path.expanduser("~"), ".terraformrc")
    return path if os.path.isfile(path) else None


_TF_PROVIDER_INSTALLATION_RE = re.compile(r"(?m)^[ \t]*provider_installation[ \t]*\{")


def _terraform_cli_config_text(user_text, home):
    """The CLI config gather runs terraform with: the user's own config (kept:
    credentials, host blocks, plugin_cache_dir) plus an explicit
    `provider_installation` block that installs providers directly from their
    registries -- and from the user's own `~/.terraform.d/plugins` when it
    exists -- so no implied mirror, in particular a repo-shipped
    `terraform.d/plugins`, is ever consulted. TF_PLUGIN_CACHE_DIR still
    applies. Pure."""
    lines = ["", "# added by activity-overview gather (issue #109)",
             "provider_installation {"]
    user_plugins = os.path.join(home, ".terraform.d", "plugins") if home else None
    if user_plugins and os.path.isdir(user_plugins):
        lines.append("  filesystem_mirror {")
        lines.append("    path = %s" % json.dumps(user_plugins))
        lines.append("  }")
    lines += ["  direct {}", "}", ""]
    return (user_text or "") + "\n".join(lines)


def _terraform_cli_config():
    """Path of the TF_CLI_CONFIG_FILE every terraform command gets (created
    once per process, removed at exit). A user config that already has its
    own `provider_installation` block disables the implied mirrors itself and
    is used as it is."""
    global _terraform_cli_config_cache
    with _child_env_lock:
        if _terraform_cli_config_cache is not None:
            return _terraform_cli_config_cache
        user = _terraform_user_config_path(os.environ)
        user_text = ""
        if user:
            try:
                with open(user, encoding="utf-8", errors="replace") as fh:
                    user_text = fh.read()
            except OSError:
                user_text = ""
            if _TF_PROVIDER_INSTALLATION_RE.search(user_text):
                _terraform_cli_config_cache = user
                return user
        fd, path = tempfile.mkstemp(prefix="activity-terraform-", suffix=".tfrc")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_terraform_cli_config_text(user_text, os.path.expanduser("~")))
        atexit.register(_remove_quietly, path)
        _terraform_cli_config_cache = path
        return path


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _child_env(args):
    """The environment for one subprocess (see the section comment above)."""
    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
    tool = os.path.basename(args[0]).lower() if args else ""
    if tool.endswith(".exe"):
        tool = tool[:-4]
    if tool == "git":
        carried = _git_passthrough_config()
        # A configured credential helper (`gh auth git-credential`, a CI
        # helper) is git's own child and may read the GitHub token from the
        # environment to clone a private repo: keep those names for git then.
        # Without a helper git has no use for them and they stay removed.
        if any(k.lower().startswith("credential.") and k.lower().endswith(".helper")
               or k.lower() == "credential.helper" for k, _ in carried):
            for name in _GIT_HELPER_TOKEN_NAMES:
                if name in os.environ:
                    env[name] = os.environ[name]
        pairs = carried + _env_config_pairs(env)
        for i, (key, value) in enumerate(pairs):
            env["GIT_CONFIG_KEY_%d" % i] = key
            env["GIT_CONFIG_VALUE_%d" % i] = value
        if pairs:
            env["GIT_CONFIG_COUNT"] = str(len(pairs))
        env.pop("LANGUAGE", None)
        env.update(_GIT_ISOLATION_ENV)
    elif tool == "terraform":
        env["TF_CLI_CONFIG_FILE"] = _terraform_cli_config()
    return env


# Seconds to wait before each retry of a git command that failed on the
# network (three retries). Read-only and fetch commands are safe to repeat.
GIT_NETWORK_RETRY_DELAYS = (2, 8, 30)
_GIT_NETWORK_ERRORS = (
    "SSL_ERROR_SYSCALL", "SSL_read", "RPC failed", "early EOF",
    "Connection reset", "Connection timed out", "Could not resolve host",
    "unexpected disconnect", "The remote end hung up unexpectedly",
    "could not fetch", "Failed to connect", "HTTP 502", "HTTP 503", "HTTP 504",
    "error: 502", "error: 503", "error: 504", "TLS connection was non-properly terminated",
    "gnutls_handshake", "Operation timed out",
)


def _git_network_failure(stderr):
    """The marker of a transient network failure found in git's stderr, or
    None. LC_ALL=C (see _child_env) keeps these messages in English."""
    text = stderr or ""
    return next((m for m in _GIT_NETWORK_ERRORS if m in text), None)


def _sleep(seconds):
    time.sleep(seconds)


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
    Non-git commands routed through here (bicep, terraform, graphify) get no
    extra arguments.

    Every command runs with the environment `_child_env` builds (issue #109):
    no GitHub token or other `*_TOKEN` variable, git isolated from the user's
    global and system config (only network/auth routing settings are carried
    over), and terraform pointed at a CLI config that never uses a repo-local
    plugin mirror."""
    args = list(args)
    if args and os.path.basename(args[0]) == "git":
        args = [args[0], "-c", "core.quotepath=false"] + args[1:]
    is_git = bool(args) and os.path.basename(args[0]) == "git"
    attempt = 0
    while True:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=_child_env(args))
        if proc.returncode == 0:
            return proc.stdout
        # A dropped connection is retried (git only): the clone is partial
        # (`--filter=blob:none`), so even a local-looking `git log -p` fetches
        # file contents from the remote while it runs, and one reset used to
        # end a gather after minutes of work with nothing stored.
        if is_git and attempt < len(GIT_NETWORK_RETRY_DELAYS) \
                and _git_network_failure(proc.stderr):
            delay = GIT_NETWORK_RETRY_DELAYS[attempt]
            attempt += 1
            sys.stderr.write(
                "note: git lost its connection ({}); retry {} of {} in {}s\n".format(
                    _git_network_failure(proc.stderr), attempt,
                    len(GIT_NETWORK_RETRY_DELAYS), delay))
            _sleep(delay)
            continue
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )


def git_ls_files(clone_dir, run=None):
    """Tracked paths of the clone (`git ls-files`), C-quoting undone."""
    run = run or run_git
    return [unquote_git_path(ln)
            for ln in run(["git", "-C", clone_dir, "ls-files"]).splitlines() if ln]


def clone_head_sha(clone_dir, run=run_git):
    """Resolve the clone's HEAD commit SHA, or None when there is no clone
    (e.g. --no-clone with nothing on disk). In acquire() it is the walked
    branch's head: the commits-API listing starts there, and it is the
    structure commit for the current month. `meta.clone_sha` is the commit the
    structure was read at (issue #107: the window's end when the clone has it,
    see window_end_commit / structure_tree) — provenance only: nothing checks
    it out or rebuilds from it later."""
    try:
        return run(["git", "-C", clone_dir, "rev-parse", "HEAD"]).strip() or None
    except Exception:
        return None


def window_end_commit(clone_dir, to_date, run=None):
    """The mainline as of the window's end (issue #107): the newest commit on
    the checked-out branch's first-parent chain whose committer day (the UTC
    `date` the walks window on, #109) is on or before `to_date`, or None when
    the bounded clone holds no such commit. First-parent, so a topic branch
    merged after `to_date` is not counted as on the mainline before its merge."""
    run = run or run_git
    raw = run(["git", "-C", clone_dir, "log", "--first-parent",
               f"--pretty=format:{LOG_FORMAT}"])
    for rec in parse_git_log(raw):
        if in_window(rec.get("date"), None, to_date):
            return rec["sha"]
    return None


@contextlib.contextmanager
def structure_tree(clone_dir, sha, run=None):
    """The directory to read the structure of commit `sha` from (issue #107).

    `sha` None or the clone's HEAD (the current month): the clone itself,
    with no git call beyond the HEAD lookup. Any other commit: a temporary
    detached `git worktree` of it, removed (and pruned) when the `with` ends,
    also when a read raises. The clone's HEAD, branch, index and working tree
    are never touched, so a reused clone stays on its branch for the next run
    and by-products of the IaC builds (a `.terraform/`, a lock file) cannot
    block a checkout in either direction. For the head the builds run in the
    clone itself (no second full checkout of a large repo); the next run's
    prepare_clone / checkout_base_branch remove their output first
    (reset_build_byproducts, issue #109)."""
    run = run or run_git
    if not sha or sha == clone_head_sha(clone_dir, run):
        yield clone_dir
        return
    run(["git", "-C", clone_dir, "worktree", "prune"])
    tmp = tempfile.mkdtemp(prefix="activity-structure-")
    tree = os.path.join(tmp, "tree")
    try:
        run(["git", "-C", clone_dir, "worktree", "add", "-q", "--detach", tree, sha])
        yield tree
    finally:
        try:
            run(["git", "-C", clone_dir, "worktree", "remove", "--force", tree])
        except RuntimeError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            run(["git", "-C", clone_dir, "worktree", "prune"])
        except RuntimeError:
            pass


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
                # A file of the area that cannot be read (refused symlink,
                # unreadable) fails this attempt: a partial edge list must not
                # be reported as `resolved`, or the replay would retract the
                # stored edges of the part that was not read.
                def read_local(rel):
                    return read_text(os.path.join(clone_dir, rel))
                return build_bicep_edges(
                    src, arm, bp, area_ids, patterns,
                    module_exists=_clone_module_exists(clone_dir),
                    read_local=read_local), "resolved"
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
    # The default reader never follows a symlink out of the tree (issue #109):
    # a refused / unreadable file fails that area's build (`failed`), never
    # the member.
    if read_text is _read_text_file:
        def read_text(path):
            return _read_text_file(path, root=clone_dir)
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
    _newcomers_max(env)
    _affiliation_max(env)
    _people_settings(env)


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
                 rate_limited=None, allow_statuses=()):
    """Send `req` with timeout, retries and rate-limit back-off. Returns
    (headers, body_bytes), or (None, status) for a 404 when allow_404 or for
    an HTTP status listed in `allow_statuses` (neither retried). Raises
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
            if (allow_404 and err.code == 404) or err.code in allow_statuses:
                return None, err.code
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


# HTTP statuses for a single issue / PR that is not (or no longer) in the repo:
# 404 (never existed, or not visible) and 410 (deleted). A caller fetching
# one referenced item by number skips it on either, instead of failing the
# whole gather on one dangling reference (#109).
GONE_STATUSES = (404, 410)


def _repo_of_url(url):
    """'owner/repo' (lower-case) named by a GitHub API `repository_url`
    (`…/repos/{owner}/{repo}`) or web `html_url`
    (`https://github.com/{owner}/{repo}/…`), or None. Pure."""
    parts = [p for p in urllib.parse.urlparse(url or "").path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "repos":
        parts = parts[1:]
    elif urllib.parse.urlparse(url or "").netloc.startswith("api."):
        return None
    if len(parts) < 2:
        return None
    return "{}/{}".format(parts[0], parts[1]).lower()


def fetched_item_mismatch(raw, slug, number):
    """Why a REST issue / PR object fetched as #`number` of `slug`
    ('owner/repo') is NOT that item, or None when it is (#109). GitHub answers
    a transferred issue through a redirect to its new home, so the object
    must carry the requested `number`, and its `repository_url` and `html_url`
    (whichever it has; compared case-insensitively) must name `slug`. A
    payload with neither URL is not rejected for it (it cannot be checked).
    Pure."""
    if not isinstance(raw, dict):
        return "not an object"
    if raw.get("number") != number:
        return "it is #{}".format(raw.get("number"))
    want = (slug or "").lower()
    for key in ("repository_url", "html_url"):
        if raw.get(key):
            got = _repo_of_url(raw[key])
            if got != want:
                return "{} names {}".format(key, got or raw[key])
    return None


def http_get_json(url, token, allow_404=False, opener=None, sleep=None, clock=None,
                  allow_statuses=()):
    """GET a GitHub API URL → (parsed_json, next_url).

    Timeouts, retries, rate-limit back-off and redirect auth-stripping come from
    `_github_open` (see the section comment above). On an HTTP error, GitHub
    explains the cause in the response body and a few headers (rate limit vs.
    SAML SSO vs. token scope); `_format_http_error` surfaces both.

    With allow_404=True a 404 returns (None, 404) instead of raising, so the
    backfill seam can map a definitively-absent ref to ABSENT; each HTTP status
    in `allow_statuses` (e.g. GONE_STATUSES: 404 and 410, a deleted issue)
    likewise returns (None, status). Only the statuses a caller names are
    tolerated: all other failures raise SystemExit with the diagnostic (fail
    fast).
    """
    req = urllib.request.Request(url, headers=_github_headers(url, token))
    try:
        headers, body = _github_open(req, url, allow_404=allow_404, opener=opener,
                                     sleep=sleep, clock=clock,
                                     allow_statuses=allow_statuses)
    except GitHubAPIError as err:
        raise SystemExit(str(err)) from None
    if headers is None:
        return None, body
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


# Page guard of _paginate_board_items: at most this many item pages per board
# after the discovery page (each up to 100 items), whatever `max_items` says.
BOARD_MAX_PAGES = 200


def _paginate_board_items(graphql, board, owner, repo, max_items,
                          truncations=None):
    """Collect ONE board's item nodes: seed from the discovery page, then page the
    rest by NODE ID (PROJECT_BOARD_ITEMS_QUERY — a cursor that belongs to this
    board's connection), capped at `max_items` items and BOARD_MAX_PAGES pages.
    Returns the (capped) list of item nodes.

    Whenever items are left behind — more pages remain when the loop stops (the
    item cap, the page guard, a failed or empty page, no board id) or the last
    page overshot the cap — it warns on stderr naming the reason and, when
    `truncations` is a list, appends {board, kept, max_items, reason} to it
    (acquire records them as `meta.project_board_truncated`, #109), so lost
    coverage is visible in the data, not only on stderr."""
    nodes = list(((board.get("items") or {}).get("nodes") or []))
    info = (board.get("items") or {}).get("pageInfo") or {}
    cursor, has_next = info.get("endCursor"), info.get("hasNextPage")
    board_id, guard, reason = board.get("id"), 0, None
    while has_next and board_id and len(nodes) < max_items \
            and guard < BOARD_MAX_PAGES:
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
            reason = "error"
            break
        items = ((page or {}).get("node") or {}).get("items") or {}
        page_nodes = items.get("nodes") or []
        if not page_nodes:
            reason = "empty_page"
            break
        nodes.extend(page_nodes)
        info = items.get("pageInfo") or {}
        cursor, has_next = info.get("endCursor"), info.get("hasNextPage")
    if not has_next and len(nodes) <= max_items:
        return nodes
    if len(nodes) >= max_items:
        reason = "item_cap"
    elif reason is None:
        reason = "page_guard" if guard >= BOARD_MAX_PAGES else "no_board_id"
    kept = nodes[:max_items]
    if reason != "error":   # an error was warned about above
        sys.stderr.write(
            "warning: Projects v2 board #{} for {}/{} truncated at {} items "
            "({}; raise ACTIVITY_BOARD_MAX_ITEMS for full coverage when it is "
            "the item cap); recorded in meta.project_board_truncated.\n".format(
                board.get("number"), owner, repo, len(kept),
                _BOARD_TRUNCATION_REASONS[reason]))
    if truncations is not None:
        truncations.append({"board": board.get("number"), "kept": len(kept),
                            "max_items": max_items, "reason": reason})
    return kept


_BOARD_TRUNCATION_REASONS = {
    "item_cap": "the item cap was reached",
    "page_guard": "the page guard of {} pages was reached".format(BOARD_MAX_PAGES),
    "empty_page": "a page claimed more items but returned none",
    "no_board_id": "the board has no id to page by",
    "error": "a page request failed",
}


def fetch_project_board(graphql, owner, repo, max_items=5000, ref_date=None,
                        truncations=None):
    """Auto-discover + fetch EVERY maintained Projects v2 board the repo links, via
    the injected `graphql(query, variables)` callable, returning
    `(sprints, items_by_ref)` (see parse_project_board, which merges items across
    boards). Boards that are closed or stale (see `board_is_maintained`) are
    skipped with a warning so a deprecated-but-not-removed board can't pollute the
    result. Each kept board's items are paginated by NODE ID and capped per board
    at `max_items`. `ref_date` (the window's ref/`to` date) anchors the staleness
    check. A board whose items were cut short is appended to the optional
    `truncations` list (see _paginate_board_items).

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
        nodes = _paginate_board_items(graphql, b, owner, repo, max_items,
                                      truncations)
        fb = dict(b)
        fb["items"] = {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": nodes}
        full_boards.append(fb)
    return parse_project_board(full_boards)


# --- Closing issues as GitHub reports them (issue #105) ------------------------
#
# `PullRequest.closingIssuesReferences` lists the issues a PR will close / closed,
# from closing keywords AND from manual "Development" sidebar links (which leave
# no keyword in the text). One request aliases CLOSING_ISSUES_BATCH PRs of the
# repo, each with its first CLOSING_ISSUES_FIRST closing issues; a PR with more
# is paged on its own (PR_CLOSING_ISSUES_PAGE_QUERY). 100 PRs = 2 requests (plus
# one per page of a PR with more than CLOSING_ISSUES_FIRST closing issues), each
# 1 point of the GraphQL rate limit (50 connections of 25 nodes, well under 100
# requests per query).
CLOSING_ISSUES_BATCH = 50
CLOSING_ISSUES_FIRST = 25

_CLOSING_ISSUES_FIELDS = (
    "nodes { number repository { nameWithOwner } } "
    "pageInfo { hasNextPage endCursor }")

PR_CLOSING_ISSUES_PAGE_QUERY = """
query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      closingIssuesReferences(first: 100, after: $cursor) { %s }
    }
  }
}
""".strip() % _CLOSING_ISSUES_FIELDS


def closing_issues_query(numbers):
    """The GraphQL query reading `closingIssuesReferences` for every PR number
    in `numbers` (ascending), one `pr<N>: pullRequest(number: N)` alias each.
    The numbers are ints written into the query text. Pure."""
    lines = ["query($owner:String!, $repo:String!) {",
             "  repository(owner:$owner, name:$repo) {"]
    for n in sorted({int(n) for n in numbers}):
        lines.append(
            "    pr{0}: pullRequest(number: {0}) {{ number "
            "closingIssuesReferences(first: {1}) {{ {2} }} }}".format(
                n, CLOSING_ISSUES_FIRST, _CLOSING_ISSUES_FIELDS))
    lines += ["  }", "}"]
    return "\n".join(lines)


def _closing_refs_of(conn_obj):
    """[{owner, repo, number}] from one closingIssuesReferences page, in
    GitHub's order; nodes without a number or repository are skipped."""
    out = []
    for node in (conn_obj or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        num = node.get("number")
        full = ((node.get("repository") or {}).get("nameWithOwner")) or ""
        owner, _, repo = full.partition("/")
        if isinstance(num, int) and not isinstance(num, bool) and owner and repo:
            out.append({"owner": owner, "repo": repo, "number": num})
    return out


def fetch_closing_issues(graphql, owner, repo, numbers):
    """GitHub's closing issues of the PRs `numbers` of owner/repo, through the
    injected `graphql(query, variables)` callable. Returns (refs, reason):
    refs = {pr_number: [{owner, repo, number}, ...]} (GitHub's order, deduped
    case-insensitively) for every PR GitHub answered; a PR it returned null for
    (not a pull request) is left out. reason is None on success, else why the
    whole answer is unusable (an error, a null repository, a PR object without
    `closingIssuesReferences`); then refs is {} — never half an answer."""
    numbers = sorted({int(n) for n in numbers})
    refs = {}
    try:
        for i in range(0, len(numbers), CLOSING_ISSUES_BATCH):
            batch = numbers[i:i + CLOSING_ISSUES_BATCH]
            data = graphql(closing_issues_query(batch),
                           {"owner": owner, "repo": repo})
            repo_obj = (data or {}).get("repository")
            if not isinstance(repo_obj, dict):
                return {}, "repository {}/{} not found".format(owner, repo)
            for n in batch:
                pr_obj = repo_obj.get("pr{}".format(n))
                if pr_obj is None:
                    continue
                conn_obj = pr_obj.get("closingIssuesReferences") \
                    if isinstance(pr_obj, dict) else None
                if not isinstance(conn_obj, dict):
                    return {}, "no closingIssuesReferences for PR #{}".format(n)
                got = _closing_refs_of(conn_obj)
                info = conn_obj.get("pageInfo") or {}
                guard = 0
                while info.get("hasNextPage") and info.get("endCursor") \
                        and guard < 100:
                    guard += 1
                    page = graphql(PR_CLOSING_ISSUES_PAGE_QUERY, {
                        "owner": owner, "repo": repo, "number": n,
                        "cursor": info["endCursor"]})
                    conn_obj = ((((page or {}).get("repository") or {})
                                 .get("pullRequest") or {})
                                .get("closingIssuesReferences"))
                    if not isinstance(conn_obj, dict):
                        return {}, "no closingIssuesReferences for PR #{}".format(n)
                    got += _closing_refs_of(conn_obj)
                    info = conn_obj.get("pageInfo") or {}
                seen, refs[n] = set(), []
                for r in got:
                    key = (r["owner"].lower(), r["repo"].lower(), r["number"])
                    if key not in seen:
                        seen.add(key)
                        refs[n].append(r)
    except (SystemExit, Exception) as err:  # noqa: BLE001 — degrade, never hard-fail
        # SystemExit too: an injected/legacy graphql callable may signal an
        # HTTP error that way (see _paginate_board_items).
        return {}, str(err) or type(err).__name__
    return refs, None


def apply_closing_issues(pr, refs, current_slug):
    """Merge GitHub's closing issues `refs` ([{owner, repo, number}]) into a PR
    record (mutates `pr`); returns how many it adds beyond the text's closing
    keywords. Same-repo issues (slug compared case-insensitively) not already
    in `closes` are appended to it in ascending order after the keyword ones
    (which keep their text order), and leave `crossref_issues` (they were only
    mentions there). Other-repo issues that are not keyword-closed in the text
    are new too; `_cross_repo_pr_edges` turns them into `closes` refs. When
    anything is new the full GitHub list is stored as `closing_issues`
    (sorted by owner/repo, case-insensitively, then number), so fold derives
    the same edges from the stored record alone. Nothing new -> the record is
    left exactly as it was (existing bundles stay byte-identical)."""
    current = (current_slug or "").lower()
    closes = list(pr.get("closes") or [])
    keyword_cross = {
        (r["owner"].lower(), r["repo"].lower(), r["number"])
        for r in parse_qualified_refs(_pr_text(pr)) if r["kind"] == "closes"}
    new_same, new_cross = set(), 0
    for r in refs or []:
        slug = "{}/{}".format(r["owner"], r["repo"]).lower()
        if slug == current:
            if r["number"] not in closes:
                new_same.add(r["number"])
        elif (r["owner"].lower(), r["repo"].lower(), r["number"]) \
                not in keyword_cross:
            new_cross += 1
    if not new_same and not new_cross:
        return 0
    pr["closes"] = closes + sorted(new_same)
    pr["crossref_issues"] = [n for n in pr.get("crossref_issues") or []
                             if n not in new_same]
    pr["closing_issues"] = sorted(
        ({"owner": r["owner"], "repo": r["repo"], "number": r["number"]}
         for r in refs),
        key=lambda r: (r["owner"].lower(), r["repo"].lower(), r["number"]))
    return len(new_same) + new_cross


def resolve_closing_issues(prs, graphql, owner, repo):
    """Read GitHub's closing issues for every PR in `prs` and merge them in
    (apply_closing_issues). Writes ONE stderr summary line and returns the
    source for `meta.closing_refs_source`: "graphql", or "keywords" when
    `graphql` is None or the answer is unusable (fetch_closing_issues) — then
    no PR is changed and the gather carries on with the closing keywords."""
    if graphql is None:
        refs, reason = {}, "no GraphQL client"
    else:
        refs, reason = fetch_closing_issues(
            graphql, owner, repo, [pr["number"] for pr in prs])
    if reason is not None:
        sys.stderr.write("closing issues: GraphQL unavailable ({}), using "
                         "closing keywords only\n".format(reason))
        return "keywords"
    slug = "{}/{}".format(owner, repo)
    checked = gainers = gained = 0
    for pr in prs:
        if pr["number"] not in refs:
            continue
        checked += 1
        n = apply_closing_issues(pr, refs[pr["number"]], slug)
        if n:
            gainers += 1
            gained += n
    sys.stderr.write(
        "closing issues: {} PR(s) checked via GraphQL, {} gained {} closing "
        "issue(s) not in their text (source: graphql)\n".format(
            checked, gainers, gained))
    return "graphql"


def _graphql_client(token):
    """The `graphql(query, variables)` callable acquire hands to the GraphQL
    readers (a seam so tests inject fixtures instead of the network)."""
    def _graphql(query, variables=None):
        return graphql_post(token, query, variables)
    return _graphql


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


_PUBLIC_MEMBER_RE = re.compile(r"^https://api\.github\.com/orgs/[^/?#]+/public_members/[^/?#]+$")


def public_member_probe(url, token, opener=None, sleep=None, clock=None):
    """GET /orgs/{org}/public_members/{login} -> ({"public_member": bool},
    None): GitHub answers 204 (a public member) or 404 (not one, or the
    membership is private). Any other failure raises SystemExit, as
    http_get_json does."""
    req = urllib.request.Request(url, headers=_github_headers(url, token))
    try:
        headers, _body = _github_open(req, url, opener=opener, sleep=sleep,
                                      clock=clock, allow_statuses=(404,))
    except GitHubAPIError as err:
        raise SystemExit(str(err)) from None
    return {"public_member": headers is not None}, None


def _paginated(token):
    """Adapter: turn http_get_json into the (items, next) shape fetch_all wants.
    A public-membership probe (no JSON body) answers {"public_member": bool}."""
    def get_page(url):
        if _PUBLIC_MEMBER_RE.match(url or ""):
            return public_member_probe(url, token)
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


# --- First-time contributors (per-window fact `newcomers`) --------------------
#
# Who is new this window: an author of a PR created in the window, or the
# reporter of an issue created in it, with no PR / issue of theirs in the repo
# created before the window's start. `people[].first_seen` cannot say it (it
# is the earliest activity inside the view) and `author_association` is
# GitHub's value at gather time. The answer comes from the issue search API,
# `repo:O/R is:<kind> author:<login> created:<<from>` with `per_page=1`,
# reading `total_count`: one call for a candidate with an earlier item of the
# kind they started with in the window, two (the other kind too) for one
# without. Search has its own rate limit (30 requests a minute with a token);
# the calls go through the gather's GitHub client, which waits out a rate
# limit and retries transient errors. A candidate whose check fails is
# "unknown", never guessed; after NEWCOMER_FAILURE_STOP failures in a row the
# rest are "unknown" too. Bots (derive.is_bot_login) and the deleted-user
# placeholder `ghost` are not candidates. More than ACTIVITY_NEWCOMERS_MAX
# candidates: the earliest are checked, the rest listed as `not_checked`.
#
# The fetch is behind the `search(login, kind)` seam (find_newcomers) so the
# test suite never touches the network.

NEWCOMERS_MAX = 200          # candidates checked per window (ACTIVITY_NEWCOMERS_MAX, min 0)
NEWCOMER_FAILURE_STOP = 3    # consecutive failed checks before giving up
_GHOST_LOGIN = "ghost"       # GitHub's placeholder for a deleted account
SEARCH_ISSUES_URL = "https://api.github.com/search/issues"


def _newcomers_max(env=None):
    return _env_int("ACTIVITY_NEWCOMERS_MAX", NEWCOMERS_MAX, 0, env)


def newcomer_candidates(prs, issues, from_date, to_date):
    """The candidate first-time contributors of a window: one entry per login
    that authored a PR or reported an issue CREATED in [from_date, to_date],
    {"login", "first": "pr" | "issue", "number", "url", "created_at"} for the
    earliest such item, sorted by (created_at, login). Bots and `ghost` are
    left out. Pure."""
    first = {}
    for kind, items in (("pr", prs), ("issue", issues)):
        for it in items:
            login = it.get("author")
            created = it.get("created_at")
            if not login or login == _GHOST_LOGIN or derive.is_bot_login(login):
                continue
            if not in_window(created, from_date, to_date):
                continue
            entry = {"login": login, "first": kind, "number": it.get("number"),
                     "url": it.get("url"), "created_at": created}
            cur = first.get(login)
            if cur is None or (created, kind, entry["number"] or 0) < (
                    cur["created_at"], cur["first"], cur["number"] or 0):
                first[login] = entry
    return sorted(first.values(), key=lambda e: (e["created_at"], e["login"]))


def newcomer_search_url(owner, repo, login, before, kind):
    """The search URL counting `login`'s `kind` ("pr" | "issue") items in
    owner/repo created before `before` (the window's first day), newest
    first, so the one item returned (`per_page=1`) is the most recent
    earlier one: its `created_at` is `last_seen_before`. Pure."""
    q = "repo:{}/{} is:{} author:{} created:<{}".format(owner, repo, kind,
                                                        login, before)
    return "{}?{}".format(SEARCH_ISSUES_URL, urllib.parse.urlencode(
        {"q": q, "sort": "created", "order": "desc", "per_page": 1}))


def search_latest_created(page):
    """`created_at` of the first item of one search response (sorted by
    created, newest first: the most recent earlier item), or None. Pure."""
    if not isinstance(page, dict):
        return None
    items = page.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    created = items[0].get("created_at")
    return created if isinstance(created, str) and created else None


# Returning contributors: a candidate whose most recent earlier PR / issue is
# more than RETURNING_GAP_DAYS[0] days before their first item of the window
# is `returning`, bucketed by the largest threshold the gap exceeds (a month
# counted as 30 days); anyone else with earlier activity is `regular`.
RETURNING_GAP_DAYS = (90, 180, 270)
RETURNING_GAP_BUCKETS = {90: "over_3_months", 180: "over_6_months",
                         270: "over_9_months"}


def _gap_days(earlier, later):
    a, b = derive._iso_date(earlier), derive._iso_date(later)
    if a is None or b is None:
        return None
    return (b - a).days


def gap_bucket(gap_days):
    """The RETURNING_GAP_BUCKETS label of a gap, or None when it is not more
    than the smallest threshold (or unknown). Pure."""
    if not isinstance(gap_days, int):
        return None
    hit = [t for t in RETURNING_GAP_DAYS if gap_days > t]
    return RETURNING_GAP_BUCKETS[max(hit)] if hit else None


def _search_answer(answer):
    """(count, latest created_at) of a `search` seam answer: an int count
    (no date), a {"count", "latest"} dict, or None (cannot tell)."""
    if isinstance(answer, dict):
        count, latest = answer.get("count"), answer.get("latest")
    else:
        count, latest = answer, None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None, None
    return count, latest if count else None


def search_total_count(page):
    """`total_count` of one search response, or None when it cannot be trusted
    (not a search response, or GitHub flags it `incomplete_results`). Pure."""
    if not isinstance(page, dict) or page.get("incomplete_results"):
        return None
    count = page.get("total_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def find_newcomers(candidates, search, before, max_candidates=NEWCOMERS_MAX,
                   stop_after=NEWCOMER_FAILURE_STOP):
    """Decide which `candidates` (newcomer_candidates) are new before `before`.

    `search(login, kind)` returns how many `kind` items `login` created in the
    repo before `before` — an int, or {"count", "latest"} with the most
    recent such item's `created_at` — or None (or raises) when it cannot
    tell. A candidate is checked for the kind they started with first and,
    when that count is 0, for the other kind; when it is not 0 and its
    latest date is more than RETURNING_GAP_DAYS[0] days before the
    candidate's first item, the other kind is checked too (a more recent
    item of that kind would make the gap shorter), so `last_seen_before` is
    the most recent earlier PR or issue. Returns the per-window fact value:
    {"collected": True, "before", "candidates": n, "new": [{"login", "first",
    "number", "url", "created_at"}] (by login), "unknown": [login],
    "not_checked": [login]} — `unknown`: a check failed (never guessed);
    `not_checked`: beyond `max_candidates`. When the seam answers dates, also
    "returning": [candidate + {"last_seen_before", "gap_days",
    "gap_bucket"}] (`gap_upper_bound: true` when the second kind's check
    failed), "regular": [{"login", "last_seen_before", "gap_days"}],
    "earlier_undated" (only when any: earlier activity, no date) and
    "gap_thresholds_days". Network-free given `search`."""
    checked = candidates[:max_candidates]
    not_checked = sorted(c["login"] for c in candidates[max_candidates:])
    new, unknown, returning, regular, undated = [], [], [], [], []
    dated = False
    failures = 0

    def ask(login, kind):
        try:
            return _search_answer(search(login, kind))
        except (Exception, SystemExit):   # a failed call: http_get_json exits
            return None, None
    for i, cand in enumerate(checked):
        if failures >= stop_after:
            unknown.extend(c["login"] for c in checked[i:])
            break
        first = cand["first"]
        other = "issue" if first == "pr" else "pr"
        count, latest = ask(cand["login"], first)
        if count is None:
            unknown.append(cand["login"])
            failures += 1
            continue
        dated = dated or latest is not None
        upper_bound = False
        gap = _gap_days(latest, cand["created_at"]) if latest else None
        if count == 0 or (gap is not None and gap > RETURNING_GAP_DAYS[0]):
            count2, latest2 = ask(cand["login"], other)
            if count2 is None:
                if count == 0:
                    unknown.append(cand["login"])
                    failures += 1
                    continue
                upper_bound = True
            else:
                dated = dated or latest2 is not None
                count += count2
                if latest2 and (latest is None or latest2 > latest):
                    latest = latest2
        failures = 0
        if count == 0:
            new.append(dict(cand))
            continue
        if latest is None:
            undated.append(cand["login"])
            continue
        gap = _gap_days(latest, cand["created_at"])
        bucket = gap_bucket(gap)
        if bucket:
            row = dict(cand, last_seen_before=latest, gap_days=gap,
                       gap_bucket=bucket)
            if upper_bound:
                row["gap_upper_bound"] = True
            returning.append(row)
        else:
            regular.append({"login": cand["login"], "last_seen_before": latest,
                            "gap_days": gap})
    out = {"collected": True, "before": before, "candidates": len(candidates),
           "new": sorted(new, key=lambda e: e["login"]),
           "unknown": sorted(unknown), "not_checked": not_checked}
    if dated:
        out["returning"] = sorted(returning, key=lambda e: e["login"])
        out["regular"] = sorted(regular, key=lambda e: e["login"])
        out["gap_thresholds_days"] = list(RETURNING_GAP_DAYS)
    if undated:
        out["earlier_undated"] = sorted(undated)
    return out


def collect_newcomers(prs, issues, owner, repo, from_date, to_date, get_page,
                      max_candidates=None):
    """The window's `newcomers` fact, searched through `get_page` (the
    gather's GitHub client: (json, next_url) per URL). One stderr line says
    what it cost and found; another names candidates left unknown or
    unchecked."""
    cands = newcomer_candidates(prs, issues, from_date, to_date)
    cap = _newcomers_max() if max_candidates is None else max_candidates
    calls = [0]

    def search(login, kind):
        calls[0] += 1
        page, _next = get_page(newcomer_search_url(owner, repo, login,
                                                   from_date, kind))
        count = search_total_count(page)
        if count is None:
            return None
        return {"count": count, "latest": search_latest_created(page)}
    if len(cands) > cap:
        sys.stderr.write(
            f"warning: {owner}/{repo}: {len(cands)} candidate first-time "
            f"contributors, more than ACTIVITY_NEWCOMERS_MAX={cap}; the earliest "
            f"{cap} are checked, the rest are listed in newcomers.not_checked\n")
    result = find_newcomers(cands, search, from_date, max_candidates=cap)
    sys.stderr.write(
        f"newcomers: {owner}/{repo}: {len(cands)} candidate author(s), "
        f"{calls[0]} search call(s), {len(result['new'])} with no PR / issue "
        f"before {from_date}, {len(result.get('returning') or [])} returning "
        f"after more than {RETURNING_GAP_DAYS[0]} days\n")
    if result["unknown"]:
        sys.stderr.write(
            f"warning: {owner}/{repo}: could not tell whether "
            f"{len(result['unknown'])} author(s) are new (search failed); "
            f"recorded as newcomers.unknown: {', '.join(result['unknown'])}\n")
    return result


# --- Contributor affiliation (per-window fact `affiliations`) -----------------
#
# For each human person of the window (PR / issue authors, mergers, reviewers,
# commenters, resolved commit authors): is that person with the organisation
# that maintains the repository (`maintainer_org`: the repo's owner, plus any
# organisation the operator names as home), `community`, or `unknown`? The
# first signal that decides wins, in this order, and the fact records which
# signal it was and how strong:
#   1. the operator's override list (`--affiliation LOGIN=…`, `@org/team=…`,
#      or the manifest's `affiliation.overrides`)               strength operator
#   2. `author_association` on the person's own records in the window: OWNER /
#      MEMBER (of the organisation that owns the repo, GitHub's value when
#      gathered) -> strong, COLLABORATOR -> medium; never decides community
#   3. public membership of a home organisation:
#      GET /orgs/{org}/public_members/{login}, 204 = member        strong
#   4. the public profile's `company` (GET /users/{login}), normalised
#      (normalize_company) and compared with the home names: a match ->
#      maintainer_org (medium); a non-empty company that does not match ->
#      community (weak); empty -> no signal
#   5. a commit author email in a home email domain, read at gather time and
#      kept only as a boolean                                      medium
#   6. nothing -> unknown (never community by default).
# A failed lookup never lets a weaker signal decide `community` (that person
# is `unknown`, signal `lookup_failed`). Signals 3 and 4 cost up to one call
# per home organisation plus one per person, through the gather's GitHub
# client (rate-limit waits, retries); answers are cached per URL for the run
# (a manifest's members share it). Only the label, the signal name and its
# strength are stored: never the company text, profile fields or an email.

AFFILIATIONS = manifest_mod.AFFILIATION_VALUES
AFFILIATION_SIGNALS = ("override", "author_association", "public_membership",
                       "profile_company", "commit_email_domain", "none",
                       "lookup_failed", "not_checked", "do_not_name")
AFFILIATION_STRENGTHS = ("operator", "strong", "medium", "weak")
AFFILIATION_MAX = 300             # people looked up per window (ACTIVITY_AFFILIATION_MAX)
AFFILIATION_FAILURE_STOP = 3      # people in a row whose lookups failed
_HOME_ASSOCIATIONS = {"OWNER": "strong", "MEMBER": "strong",
                      "COLLABORATOR": "medium"}
_ASSOCIATION_RANK = {"OWNER": 3, "MEMBER": 2, "COLLABORATOR": 1}
_COMPANY_SUFFIXES = ("corporation", "corp", "incorporated", "inc", "limited",
                     "ltd", "llc", "gmbh", "plc")
USERS_API = "https://api.github.com/users/"
ORGS_API = "https://api.github.com/orgs/"
NOREPLY_EMAIL_DOMAIN = "users.noreply.github.com"
_GROUP_RE = manifest_mod._GROUP_RE      # `@org/team`


def _affiliation_max(env=None):
    return _env_int("ACTIVITY_AFFILIATION_MAX", AFFILIATION_MAX, 0, env)


def _people_settings(env=None):
    """The registry's refresh policy from the environment: {refresh_days,
    unknown_retry_days, refresh_max, name_max} (people.py constants by
    default)."""
    return {"refresh_days": _env_int("ACTIVITY_PEOPLE_REFRESH_DAYS",
                                     people_mod.REFRESH_DAYS, 1, env),
            "unknown_retry_days": _env_int("ACTIVITY_PEOPLE_UNKNOWN_RETRY_DAYS",
                                           people_mod.UNKNOWN_RETRY_DAYS, 1, env),
            "refresh_max": _env_int("ACTIVITY_PEOPLE_REFRESH_MAX",
                                    people_mod.REFRESH_MAX, 0, env),
            "name_max": _env_int("ACTIVITY_PEOPLE_NAME_MAX",
                                 people_mod.NAME_MAX, 0, env)}


def people_run_from_args(args, env=None):
    """The run's people.Run (the retained registry), created once and kept on
    `args.people_run` so a manifest's members share it; None without
    --people / a manifest `people`. A malformed registry stops the gather
    with a message naming the entry (never a silent reset)."""
    run = getattr(args, "people_run", None)
    if run is not None:
        return run
    path = getattr(args, "people", None) or getattr(args, "people_manifest", None)
    if not path:
        return None
    settings = _people_settings(env)
    if getattr(args, "people_refresh_days", None):
        settings["refresh_days"] = args.people_refresh_days
    try:
        run = people_mod.Run(path, offline=bool(getattr(args, "people_offline", False)),
                             **settings)
    except people_mod.RegistryError as exc:
        raise SystemExit("error: {}".format(exc)) from None
    args.people_run = run
    return run


def normalize_company(text):
    """A profile company (or a home name) in the form they are compared in:
    lower case, a leading `@` dropped, `.` `,` and surrounding punctuation
    removed, whitespace collapsed, and trailing legal suffixes
    (_COMPANY_SUFFIXES: "Corporation", "Corp", "Inc", "Ltd", "LLC", "GmbH",
    …) removed. "@microsoft", "Microsoft", "Microsoft Corporation" and
    "Microsoft Corp." all give "microsoft". Pure."""
    s = (text or "").strip().lower()
    s = s.lstrip("@").strip()
    s = re.sub(r"[.,;:!()\[\]\"']", " ", s)
    words = s.split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def company_matches(company, home_names):
    """True when the profile `company` names a home organisation, False when
    it names something else, None when it is empty. Matches are exact after
    normalize_company, on the whole text or on any `@handle` in it ("@Azure
    @microsoft"); `home_names` are already normalised. Pure."""
    whole = normalize_company(company)
    if not whole:
        return None
    names = set(home_names or ())
    if whole in names:
        return True
    handles = {normalize_company(t) for t in re.split(r"[\s,/|;]+", company or "")
               if t.startswith("@")}
    return bool(handles & names)


def email_domain_is_home(email, domains):
    """True when `email`'s domain is one of `domains` (or a subdomain of
    one); `users.noreply.github.com` never is. Only this boolean leaves the
    function: the address is not kept. Pure."""
    if not email or "@" not in email or not domains:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or domain == NOREPLY_EMAIL_DOMAIN \
            or domain.endswith("." + NOREPLY_EMAIL_DOMAIN):
        return False
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _check_affiliation_value(value, what):
    if value not in AFFILIATIONS:
        raise ValueError("{} must be one of {}, got {!r}".format(
            what, ", ".join(AFFILIATIONS), value))
    return value


def parse_affiliation_override(text):
    """`LOGIN=AFFILIATION` or `@org/team=AFFILIATION` -> (key, affiliation);
    raises ValueError. Pure."""
    key, sep, value = (text or "").partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not key:
        raise ValueError("--affiliation takes LOGIN=AFFILIATION or "
                         "@org/team=AFFILIATION, got {!r}".format(text))
    _check_affiliation_value(value, "--affiliation {}".format(key))
    if key.startswith("@"):
        if not _GROUP_RE.fullmatch(key):
            raise ValueError("--affiliation group must be @org/team, got "
                             "{!r}".format(key))
    else:
        manifest_mod.check_owner(key, "--affiliation login")
    return key, value


def affiliation_config(owner, home_orgs=(), home_names=(), email_domains=(),
                       overrides=None):
    """The resolved affiliation settings of one repository: {"home_orgs"
    (the repo's owner first, then the operator's, de-duplicated without
    regard to case), "home_names" (normalised: the home orgs and the
    operator's extra names), "home_email_domains" (lower case), "overrides"
    {login: affiliation}, "groups" {"@org/team": affiliation}}. Pure."""
    orgs, seen = [], set()
    for org in [owner] + list(home_orgs or ()):
        if org and org.lower() not in seen:
            seen.add(org.lower())
            orgs.append(org)
    names = sorted({normalize_company(n) for n in orgs + list(home_names or ())
                    if normalize_company(n)})
    domains = sorted({d.strip().lower().lstrip("@") for d in email_domains or ()
                      if d and d.strip()})
    logins, groups = {}, {}
    for key, value in (overrides or {}).items():
        (groups if key.startswith("@") else logins)[key] = value
    return {"home_orgs": orgs, "home_names": names,
            "home_email_domains": domains, "overrides": logins,
            "groups": groups}


def affiliation_people(prs, issues, commits):
    """The window's human people and what the records say of them: ([logins
    by (-activity, login)], {login: best home author_association}). Bots,
    `ghost` and commit authors gather could not resolve to a login are left
    out. Activity counts PRs and issues authored, merges, reviews, comments
    and commits. Pure."""
    weight, assoc = {}, {}

    def add(login, n=1, association=None):
        if not login or login == _GHOST_LOGIN or derive.is_bot_login(login):
            return
        weight[login] = weight.get(login, 0) + n
        if association in _ASSOCIATION_RANK and _ASSOCIATION_RANK[association] > \
                _ASSOCIATION_RANK.get(assoc.get(login), 0):
            assoc[login] = association
    for it in list(prs or []) + list(issues or []):
        add(it.get("author"), association=it.get("author_association"))
        if it.get("merged_by"):
            add(it.get("merged_by"))
        for rv in it.get("reviewers") or []:
            add(rv)
        for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
            add(c.get("author"), association=c.get("author_association"))
    for c in commits or []:
        if c.get("login_resolved") is not False:
            add(c.get("author"))
    order = sorted(weight, key=lambda k: (-weight[k], k))
    return order, assoc


def resolve_affiliation_groups(groups, get_page, cache=None):
    """Expand `@org/team` override groups to logins through the team members
    listing (GET /orgs/{org}/teams/{team}/members: the token must be able
    to see the team). Returns ({login: affiliation}, [unresolved groups]).
    A later group in sorted order does not replace an earlier one's login."""
    cache = {} if cache is None else cache
    out, unresolved = {}, []
    for group in sorted(groups or {}):
        m = _GROUP_RE.fullmatch(group)
        if not m:
            unresolved.append(group)
            continue
        url = "{}{}/teams/{}/members?per_page=100".format(ORGS_API, m.group(1),
                                                          m.group(2))
        if url not in cache:
            try:
                members = fetch_all(get_page, url)
                cache[url] = sorted({(u or {}).get("login") for u in members
                                     if isinstance(u, dict) and u.get("login")})
            except (Exception, SystemExit):
                cache[url] = None
        if cache[url] is None:
            unresolved.append(group)
            continue
        for login in cache[url]:
            out.setdefault(login, groups[group])
    return out, unresolved


def make_affiliation_lookup(get_page, cache=None):
    """The network seam of find_affiliations over the gather's GitHub client
    (`get_page`): lookup("public_member", login, org) -> True / False / None
    (cannot tell), lookup("company", login) -> the profile company text ("" for
    none) or None, lookup("name", login) -> the profile's display name ("" for
    none) or None, read from the SAME `users/{login}` response (no extra
    call). The company text is handed to the classifier and never returned
    anywhere else; of the profile only these two fields are kept, and only
    in the run's memory. Answers are cached per URL in `cache`."""
    cache = {} if cache is None else cache

    def lookup(kind, login, org=None):
        if kind == "public_member":
            url = "{}{}/public_members/{}".format(ORGS_API, org, login)
        else:
            url = USERS_API + login
        if url not in cache:
            try:
                page, _next = get_page(url)
            except (Exception, SystemExit):
                return None              # not cached: a later window may retry
            if kind == "public_member":
                got = page.get("public_member") if isinstance(page, dict) else None
                answer = got if isinstance(got, bool) else None
            elif isinstance(page, dict) and page.get("login"):
                company, name = page.get("company"), page.get("name")
                answer = {"company": company if isinstance(company, str) else "",
                          "name": name if isinstance(name, str) else ""}
            else:
                answer = None
            if answer is None:
                return None
            cache[url] = answer
        answer = cache[url]
        if kind == "public_member":
            return answer
        return answer.get("name" if kind == "name" else "company", "")
    return lookup


def decide_affiliation(login, config, association=None, email_home=False,
                       lookup=None):
    """(entry, looked_up, failed) for one person: `entry` {"affiliation",
    "signal"[, "strength"][, "home_email_domain": true]} by the rule in the
    section comment; `looked_up` when the API seam was asked; `failed` when
    a lookup could not tell. `lookup` None: only the free signals."""
    def entry(aff, signal, strength=None):
        e = {"affiliation": aff, "signal": signal}
        if strength:
            e["strength"] = strength
        if email_home:
            e["home_email_domain"] = True
        return e
    if login in config["overrides"]:
        return entry(config["overrides"][login], "override", "operator"), False, False
    if association in _HOME_ASSOCIATIONS:
        return (entry("maintainer_org", "author_association",
                      _HOME_ASSOCIATIONS[association]), False, False)
    if lookup is None:
        return None, False, False
    failed = False
    for org in config["home_orgs"]:
        member = lookup("public_member", login, org)
        if member is True:
            return entry("maintainer_org", "public_membership", "strong"), True, False
        if member is None:
            failed = True
    company = lookup("company", login)
    if company is None:
        failed = True
    else:
        match = company_matches(company, config["home_names"])
        if match is True:
            return entry("maintainer_org", "profile_company", "medium"), True, failed
        if match is False and not failed:
            return entry("community", "profile_company", "weak"), True, False
    if email_home:
        return entry("maintainer_org", "commit_email_domain", "medium"), True, failed
    return entry("unknown", "lookup_failed" if failed else "none"), True, failed


def find_affiliations(people, config, associations=None, email_home=(),
                      lookup=None, max_lookups=AFFILIATION_MAX,
                      stop_after=AFFILIATION_FAILURE_STOP):
    """The `affiliations` fact value for `people` (logins, most active
    first). The free signals (override, author_association) apply to
    everyone; the looked-up ones to at most `max_lookups` people, and none
    after `stop_after` people in a row whose lookups failed: those are
    `not_checked` (no entry, counted unknown). Returns {"collected": True,
    "home_orgs", "home_names", "home_email_domains", "people": {login:
    entry}, "unknown": [login], "not_checked": [login]}. Network-free given
    `lookup`."""
    associations = associations or {}
    email_home = set(email_home or ())
    out, not_checked = {}, []
    looked, failures = 0, 0
    for login in people:
        e, _l, _f = decide_affiliation(login, config, associations.get(login),
                                       login in email_home, lookup=None)
        if e is not None:
            out[login] = e
            continue
        if lookup is None or looked >= max_lookups or failures >= stop_after:
            not_checked.append(login)
            continue
        e, was_looked, failed = decide_affiliation(
            login, config, associations.get(login), login in email_home,
            lookup=lookup)
        looked += was_looked
        failures = failures + 1 if failed else 0
        out[login] = e
    unknown = sorted([k for k, e in out.items() if e["affiliation"] == "unknown"]
                     + not_checked)
    return {"collected": True, "home_orgs": list(config["home_orgs"]),
            "home_names": list(config["home_names"]),
            "home_email_domains": list(config["home_email_domains"]),
            "people": {k: out[k] for k in sorted(out)},
            "unknown": unknown, "not_checked": sorted(not_checked)}


def affiliation_home(config):
    """The home settings a label is decided against, as one compact string
    kept with the label in the registry (`home`): the home organisations
    (lower case, sorted) and the home names, "orgs|names". The email
    domains are left out: a commit email never decides a stored label.
    Pure."""
    return "{}|{}".format(",".join(sorted({o.lower() for o in config["home_orgs"]})),
                          ",".join(sorted(config["home_names"])))


def _registry_label(entry, home=None):
    """The label a per-window decision leaves in the registry: what GitHub
    data decided (author_association, public_membership, profile_company or
    none), without the per-window commit-email boolean, with the `home`
    settings it was decided against. None for a decision the registry must
    not keep (override, a failed lookup, ...)."""
    sig = (entry or {}).get("signal")
    if sig == "commit_email_domain":
        return {"affiliation": "unknown", "signal": "none"}
    if sig not in ("author_association", "public_membership", "profile_company",
                   "none"):
        return None
    out = {"affiliation": entry["affiliation"], "signal": sig}
    if entry.get("strength"):
        out["strength"] = entry["strength"]
    if home:
        out["home"] = home
    return out


def _reused_entry(reg_entry, email_home):
    """The per-window entry for a person decided from the registry: its label
    (an operator-locked one as `override` / operator), the commit email
    deciding an `unknown` one, as decide_affiliation would."""
    if people_mod.is_locked(reg_entry, "affiliation"):
        e = {"affiliation": reg_entry["affiliation"], "signal": "override",
             "strength": "operator"}
    elif reg_entry.get("affiliation") == "unknown" and email_home:
        e = {"affiliation": "maintainer_org", "signal": "commit_email_domain",
             "strength": "medium"}
    else:
        e = {"affiliation": reg_entry["affiliation"],
             "signal": reg_entry.get("signal") or "none"}
        if reg_entry.get("strength"):
            e["strength"] = reg_entry["strength"]
    if email_home:
        e["home_email_domain"] = True
    return e


def find_affiliations_registry(people, config, run, repo_slug, associations=None,
                               email_home=(), lookup=None, records=None,
                               name_first=(), max_lookups=AFFILIATION_MAX,
                               stop_after=AFFILIATION_FAILURE_STOP):
    """find_affiliations with the retained people registry (`run`, a
    people.Run): the registry is consulted first, the API fills the gaps,
    the registry is updated (in memory; run.save() writes it) and the fact
    records the labels used for this window, each person's display `name`
    and their `maintainer` property for `repo_slug`.

    Per person, the first rule that applies:
      do_not_name in the registry   unknown, signal do_not_name, no name, no
                                    maintainer, no call
      override list / author_association   as find_affiliations (free; an
                                    author_association label is stored)
      registry affiliation locked by the operator   signal override
      registry entry fresh          its label (no call)          `reused`
      never looked up               looked up (within `max_lookups`)
                                                                 `looked_up`
      stale                         refreshed, at most run.refresh_left per
                                    run, oldest lookup first     `refreshed`
      anything else                 a stale label is reused; else the commit
                                    email; else unknown, signal not_checked
    A label is relative to the home settings (affiliation_home) it was
    decided against, kept with it as `home`: it is fresh only under the same
    settings, and one decided against other settings is never reused (an
    entry without a recorded `home` is stale, and still reusable).
    A lookup reads the display name from the same profile response. People
    decided without a lookup who have no name yet get one profile call for
    it, at most run.names_left per run, `name_first` (the newcomers) first,
    then the most active. `lookup` None (offline): no call at all.
    `records` (prs, issues, commits) give the seen dates and the maintainer
    evidence (people.maintainer_evidence). Network-free given `lookup`."""
    associations = associations or {}
    email_home = set(email_home or ())
    prs, issues, commits = records or ((), (), ())
    home = affiliation_home(config)
    dates = people_mod.activity_dates(prs, issues, commits)
    evidence = people_mod.maintainer_evidence(prs, issues)
    for login in people:
        fields = {}
        if login in dates:
            fields["seen"] = [(dates[login][0], repo_slug),
                              (dates[login][1], repo_slug)]
        if login in evidence:
            fields["maintainer"] = {repo_slug: evidence[login]}
        if fields:
            run.patch(login, **fields)
    # the refresh budget goes to the oldest lookups among the people a
    # lookup would decide (not the override list, author_association, an
    # operator-locked label or do_not_name)
    def wants_lookup(login):
        reg = run.entry(login) or {}
        return not (login in config["overrides"] or reg.get("do_not_name")
                    or associations.get(login) in _HOME_ASSOCIATIONS
                    or people_mod.is_locked(reg, "affiliation"))
    stale = set(sorted((login for login in people
                        if run.state(login, home) == "stale"
                        and wants_lookup(login)),
                       key=lambda k: ((run.entry(k) or {}).get("looked_up_at") or "",
                                      k))[:max(run.refresh_left, 0)])
    counts = {"looked_up": 0, "refreshed": 0, "reused": 0, "names_fetched": 0,
              "free": 0, "do_not_name": 0, "lookup_failed": 0}
    out, not_checked, looked_now = {}, [], set()
    looked, failures = 0, 0
    for login in people:
        reg = run.entry(login) or {}
        if reg.get("do_not_name"):
            out[login] = {"affiliation": "unknown", "signal": "do_not_name"}
            counts["do_not_name"] += 1
            continue
        e, _l, _f = decide_affiliation(login, config, associations.get(login),
                                       login in email_home, lookup=None)
        if e is not None:
            if e["signal"] == "author_association":
                run.patch(login, label=_registry_label(e, home))
            out[login] = e
            counts["free"] += 1
            continue
        if people_mod.is_locked(reg, "affiliation") and reg.get("affiliation"):
            out[login] = _reused_entry(reg, login in email_home)
            counts["free"] += 1
            continue
        state = run.state(login, home)
        # a label decided against other home settings is never used here
        # (one without a recorded home is: decided before homes were kept)
        own = reg.get("home") in (None, home)
        if state == "fresh":
            out[login] = _reused_entry(reg, login in email_home)
            counts["reused"] += 1
            continue
        can = lookup is not None and failures < stop_after and (
            (state == "never" and looked < max_lookups)
            or (state == "stale" and run.refresh_left > 0 and login in stale))
        if can:
            e, _was, failed = decide_affiliation(
                login, config, associations.get(login), login in email_home,
                lookup=lookup)
            looked_now.add(login)
            failures = failures + 1 if failed else 0
            if state == "never":
                looked += 1
            if failed:
                counts["lookup_failed"] += 1
                if reg.get("affiliation") and own and e["affiliation"] == "unknown":
                    e = _reused_entry(reg, login in email_home)
            else:
                name = lookup("name", login)
                fields = {"label": _registry_label(e, home), "looked_up_at": run.now}
                if name is not None:
                    fields["name"] = name
                run.patch(login, **fields)
                if state == "stale":
                    run.refresh_left -= 1
                    counts["refreshed"] += 1
                else:
                    counts["looked_up"] += 1
            out[login] = e
            continue
        if reg.get("affiliation") and own:
            out[login] = _reused_entry(reg, login in email_home)
            counts["reused"] += 1
        elif login in email_home:
            out[login] = {"affiliation": "maintainer_org",
                          "signal": "commit_email_domain", "strength": "medium",
                          "home_email_domain": True}
        else:
            out[login] = {"affiliation": "unknown", "signal": "not_checked"}
            not_checked.append(login)
    # display names for people decided without a lookup (newcomers first)
    if lookup is not None and failures < stop_after:
        order = [k for k in name_first if k in out] + \
            [k for k in people if k not in set(name_first)]
        for login in order:
            if run.names_left <= 0:
                break
            reg = run.entry(login) or {}
            if login in looked_now or reg.get("do_not_name") or reg.get("name") \
                    or reg.get("looked_up_at") or people_mod.is_locked(reg, "name"):
                continue
            name = lookup("name", login)
            run.names_left -= 1
            if name is None:
                continue
            run.patch(login, name=name, looked_up_at=run.now)
            counts["names_fetched"] += 1
    for login, e in out.items():
        if e["signal"] == "do_not_name":
            continue
        reg = run.entry(login) or {}
        name = people_mod.display_name(reg)
        if name:
            e["name"] = name
        e["maintainer"], e["maintainer_signal"] = people_mod.maintainer_for(
            reg, repo_slug)
    for key in ("looked_up", "refreshed", "reused", "names_fetched"):
        run.totals[key] = run.totals.get(key, 0) + counts[key]
    unknown = sorted(k for k, e in out.items() if e["affiliation"] == "unknown")
    result = {"collected": True, "home_orgs": list(config["home_orgs"]),
              "home_names": list(config["home_names"]),
              "home_email_domains": list(config["home_email_domains"]),
              "people": {k: out[k] for k in sorted(out)},
              "unknown": unknown, "not_checked": sorted(not_checked)}
    return result, counts


def args_affiliation_config(args, owner):
    """affiliation_config from the CLI (`--home-org`, `--home-name`,
    `--home-email-domain`, `--affiliation`) and, for a manifest member, the
    manifest's `affiliation` object (`args.affiliation_manifest`): lists are
    joined; a CLI override beats the manifest's for the same key."""
    man = getattr(args, "affiliation_manifest", None) or {}
    overrides = dict(man.get("overrides") or {})
    for text in getattr(args, "affiliation", None) or []:
        key, value = parse_affiliation_override(text)
        overrides[key] = value
    return affiliation_config(
        owner,
        list(man.get("home_orgs") or []) + list(getattr(args, "home_org", None) or []),
        list(man.get("home_names") or []) + list(getattr(args, "home_name", None) or []),
        list(man.get("home_email_domains") or [])
        + list(getattr(args, "home_email_domain", None) or []),
        overrides)


def collect_affiliations(prs, issues, commits, owner, repo, get_page, config,
                         email_home=(), cache=None, max_people=None,
                         people_run=None, name_first=()):
    """The window's `affiliations` fact, looked up through `get_page` (the
    gather's GitHub client). One stderr line says what it cost and found.
    With `people_run` (a people.Run: the retained registry) the registry is
    consulted first (find_affiliations_registry) and a second line reports
    looked up / refreshed / reused / still unknown; an offline run makes no
    call at all (an `@org/team` override is then left unresolved)."""
    cache = {} if cache is None else cache
    people, assoc = affiliation_people(prs, issues, commits)
    calls = [0]
    offline = people_run is not None and people_run.offline

    def counted(url):
        calls[0] += 1
        return get_page(url)
    if offline:
        group_logins, unresolved = {}, sorted(config.get("groups") or {})
    else:
        group_logins, unresolved = resolve_affiliation_groups(
            config.get("groups"), counted, cache)
    cfg = dict(config, overrides={**group_logins, **config["overrides"]})
    cap = _affiliation_max() if max_people is None else max_people
    lookup = None if offline else make_affiliation_lookup(counted, cache)
    if people_run is None:
        result = find_affiliations(people, cfg, assoc, email_home, lookup,
                                   max_lookups=cap)
    else:
        result, counts = find_affiliations_registry(
            people, cfg, people_run, "{}/{}".format(owner, repo), assoc,
            email_home, lookup, records=(prs, issues, commits),
            name_first=name_first, max_lookups=cap)
        maint = sum(1 for e in result["people"].values() if e.get("maintainer"))
        sys.stderr.write(
            "people registry: {}/{}: {} looked up, {} refreshed, {} reused from "
            "the registry, {} decided without a lookup, {} still unknown, {} "
            "name(s) fetched, {} maintainer(s){}\n".format(
                owner, repo, counts["looked_up"], counts["refreshed"],
                counts["reused"], counts["free"], len(result["unknown"]),
                counts["names_fetched"], maint,
                " (offline: no API call)" if offline else ""))
    if unresolved:
        result["groups_unresolved"] = unresolved
    by = {}
    for e in result["people"].values():
        by[e["affiliation"]] = by.get(e["affiliation"], 0) + 1
    sys.stderr.write(
        "affiliation: {}/{}: {} person(s), {} API call(s) (cached answers "
        "reused): {} maintainer_org, {} community, {} unknown ({} not "
        "checked)\n".format(owner, repo, len(people), calls[0],
                            by.get("maintainer_org", 0), by.get("community", 0),
                            len(result["unknown"]), len(result["not_checked"])))
    if unresolved:
        sys.stderr.write("warning: {}/{}: affiliation group(s) not resolved "
                         "(team not visible to the token?): {}\n".format(
                             owner, repo, ", ".join(unresolved)))
    return result


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
    summary = summarize_reviews(reviews, author=pr.get("author"))
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
        n for n in parse_timeline_crossrefs(timeline, current_slug)
        if n not in seeded]
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
    has_clone = clone_mode != "none" and os.path.isdir(clone_dir)

    # The mainline (issue #84): `--branches` / a manifest member's `branch`,
    # else the clone's default branch, else the API's `default_branch`.
    branches, base_branch, branch_source = resolve_branches(
        args.branches, clone_dir, clone_mode,
        get_repo=lambda: _paginated(token)(
            f"https://api.github.com/repos/{owner}/{repo}")[0])
    # The walked branch is the mainline (issue #107): prepare_clone checked out
    # the remote default branch (it needs no branch name, so the one clone also
    # answers resolve_branches); a different base branch is fetched under the
    # same shallow bound and checked out before any walk or structure read.
    remote_default = _clone_default_branch(clone_dir, run_git) if has_clone else None
    if has_clone and base_branch and base_branch != remote_default:
        try:
            quiet = checkout_base_branch(clone_dir, base_branch, frm)
        except MissingBranchError as exc:
            raise SystemExit(f"error: {owner}/{repo}: {exc} (from {branch_source}); "
                             f"name an existing branch") from None
        if quiet:
            clone_mode = "depth1-fallback"
        elif clone_mode == "depth1-fallback":
            clone_mode = "fetched"     # the walked branch has window history
        sys.stderr.write(
            f"note: {owner}/{repo}: walking the base branch {base_branch!r}, not "
            f"the remote default {remote_default!r}: commits, code events and "
            f"structure come from {base_branch!r}\n")
    if clone_mode == "depth1-fallback":
        sys.stderr.write(
            f"note: {owner}/{repo} has no commits on the walked branch since the "
            f"clone bound (--from minus {_clone_margin_days()} days); fetched at "
            f"--depth=1 and treating the commit walks as empty "
            f"(meta.clone_mode=depth1-fallback)\n")
    # All three git walks need real window history on disk: skipped (empty) when
    # there is no clone (--no-clone with nothing on disk) or only the depth-1 tip
    # of a quiet repo (whose whole-tree diff is not window activity).
    walk_clone = clone_mode in ("fresh", "fetched") and os.path.isdir(clone_dir)
    if walk_clone:
        # An in-window commit on the shallow boundary (no commit in the margin
        # before `frm`) would diff as the whole tree: deepen so its parent is
        # present and its diff is real (#68). Leftovers are handled after the walks.
        deepen_window_boundary(clone_dir, frm, to)
    if base_branch is None:
        sys.stderr.write(
            f"warning: {owner}/{repo}: could not determine the base branch "
            f"(no --branches, no clone default branch, no API default_branch); "
            f"meta.base_branch=null, so every merged PR will count as merged "
            f"into the mainline\n")
    elif branch_source != "--branches":
        sys.stderr.write(f"note: {owner}/{repo}: base branch {base_branch!r} "
                         f"(the repo default branch, from the {branch_source})\n")
    # The walks read the checked-out base branch only; the other `branches` are
    # recorded in meta for provenance but not applied to the log/clone.
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

    # Module change notes (per-window fact `module_notes`): the changelog
    # entries, version.json moves and version tags of the window, read from
    # the clone for the changelog / version files the code events name only
    # (collect_module_notes). Needs the window's history on disk.
    module_notes = None
    if walk_clone:
        module_notes = collect_module_notes(
            code_events, git_blob_reader(clone_dir), git_tag_lister(clone_dir),
            frm, to)
        sys.stderr.write(
            f"module notes: {owner}/{repo}: {len(module_notes['changelogs'])} "
            f"changelog update(s), {len(module_notes['versions'])} version "
            f"change(s), " + (
                f"{len(module_notes['tags']['items'])} version tag(s) in the "
                f"window" if module_notes["tags"].get("collected")
                else f"tags not collected ({module_notes['tags']['reason']})")
            + (f", {len(module_notes['unread'])} file version(s) unreadable"
               if module_notes.get("unread") else "") + "\n")

    # Phase 3b: code-area provider (directory-first; graphify optional). Paths come
    # from the code-event walk + the commit file lists (local, zero-token).
    area_paths = sorted(
        {e["path"] for e in code_events}
        | {e["old_path"] for e in code_events if e.get("old_path")}
        | {f for c in commits for f in c.get("files", [])})

    # Structure reads (issue #107) see the mainline as of the window's end: the
    # last commit of the walked branch on or before `to` (window_end_commit)
    # when the bounded clone has it, else the head. For the current month that
    # IS the head and the clone itself is read; otherwise a temporary worktree
    # of that commit is (structure_tree), and the clone never leaves its branch.
    # The walks above read the branch; the commits-API listing below starts at
    # its head (`walk_head`).
    walk_head = clone_head_sha(clone_dir) if has_clone else None
    structure_sha, structure_as_of = walk_head, ("head" if walk_head else None)
    if walk_head:
        end = window_end_commit(clone_dir, to)
        if end:
            structure_sha, structure_as_of = end, "window_end"
            if end != walk_head:
                sys.stderr.write(
                    f"note: {owner}/{repo}: structure (code graph, IaC, "
                    f"CODEOWNERS) read at {end[:12]}, the last {base_branch!r} "
                    f"commit on or before {to} (meta.structure_as_of=window_end)\n")
        else:
            sys.stderr.write(
                f"note: {owner}/{repo}: the last commit on or before {to} is not "
                f"in the bounded clone; structure read at the branch head "
                f"{walk_head[:12]} (meta.structure_as_of=head)\n")
    clone_sha = structure_sha if has_clone else clone_head_sha(clone_dir)

    with structure_tree(clone_dir, structure_sha) as tree:
        # Bicep module dirs (issue #107): every file at or below a dir holding
        # a main.bicep is that module's area. Known from the changed paths plus
        # the tracked files of the structure tree (one listing, shared with the
        # structural scan). With none, the default patterns and the calls are
        # exactly as before.
        tracked = git_ls_files(tree) if has_clone else None
        module_dirs = bicep_module_dirs(area_paths + (tracked or []))
        area_kw = {"patterns": module_area_patterns(module_dirs)} \
            if module_dirs else {}
        code_graph = select_code_area_provider(area_paths, tree, **area_kw)

        # Phase 3c: enrich area edges via the real IaC toolchain (BUILD-ONLY —
        # edges stay [] when bicep/terraform or the registry are unavailable).
        # No-op without a clone on disk.
        if not args.no_clone or os.path.isdir(clone_dir):
            # --no-iac-build (issue #109): the builds run third-party code
            # (bicep restore / terraform init download modules and providers);
            # skip them and record every area as `edges_status: skipped`. The
            # structural scan below is source parsing only and still runs.
            if getattr(args, "no_iac_build", False):
                sys.stderr.write(
                    f"note: {owner}/{repo}: --no-iac-build: bicep/terraform "
                    f"builds skipped (edges_status=skipped)\n")
                code_graph = extract_iac_edges(code_graph, tree,
                                               which=lambda _name: None, **area_kw)
                code_graph["edge_extraction"]["disabled"] = True
            else:
                code_graph = extract_iac_edges(code_graph, tree, **area_kw)
            # Multi-repo (project) gathers -- and a single-repo gather given
            # `--structural-iac` -- additionally fold the WHOLE module tree's
            # static Terraform `source` and Bicep `module` deps (no terraform
            # init, no bicep build) so the blast-radius graph is structural, not
            # limited to in-window-changed areas. Off by default for a single
            # repo: its bundles stay byte-stable (golden-bundle gate).
            if getattr(args, "structural_iac", False) and os.path.isdir(clone_dir):
                code_graph = apply_structural_scan(
                    code_graph, tree,
                    list_files=(lambda: tracked) if tracked is not None else None,
                    **area_kw)

        # CODEOWNERS from the tree (local file; try the conventional locations;
        # the first one present is used). Read through _read_repo_text (issue
        # #109): never through a symlink that leaves the tree, never failing
        # the member on an undecodable byte; a file that is there but cannot
        # be read is skipped with a warning and recorded so the replay does
        # not take it as "no owners".
        code_owners = {}
        for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            p = os.path.join(tree, rel)
            if os.path.isfile(p):
                text = _read_repo_text(tree, rel)
                if text is None:
                    code_graph["codeowners_unread"] = rel
                else:
                    code_owners = parse_codeowners(text)
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
        if not keep_pr_for_window(pr, frm, to):
            continue
        enrich_pr(pr, get_page, api, f"{owner}/{repo}")
        prs.append(pr)
    # GitHub's own closing-issue list (keywords AND Development-sidebar links,
    # issue #105): batched GraphQL over the kept PRs; on any failure the closing
    # keywords stand alone (one stderr line either way, meta.closing_refs_source).
    closing_refs_source = resolve_closing_issues(
        prs, _graphql_client(token), owner, repo)

    # PR-internal history (per-window fact `pr_history`): each kept PR's own
    # commits from refs/pull/<N>/head, and the comment / parameter changes
    # between them for the most discussed PRs (collect_pr_history). Needs the
    # clone; --no-pr-history records that it was not looked at.
    pr_history = None
    if not getattr(args, "pr_history", True):
        pr_history = {"collected": False}
    elif walk_clone and prs:
        _started = time.monotonic()
        pr_history = collect_pr_history(prs, clone_dir, base_branch=base_branch)
        _rows = pr_history["prs"]
        sys.stderr.write(
            "pr history: {}/{}: {} PR head(s) fetched, {} missing, {} failed, "
            "{} commit(s), notes for {} PR(s) in {:.0f}s{}\n".format(
                owner, repo,
                sum(1 for r in _rows if r.get("status") == "fetched"),
                sum(1 for r in _rows if r.get("status") == "missing"),
                sum(1 for r in _rows if r.get("status") in ("fetch_failed",
                                                            "log_failed")),
                sum(len(r.get("commits") or []) for r in _rows),
                sum(1 for r in _rows if "notes" in r),
                time.monotonic() - _started,
                " ({})".format(pr_history["error"]) if pr_history.get("error")
                else ""))

    # Issue set: every issue a kept PR closes or cross-references, plus open and
    # recently-closed issues (for in_flight / next_candidates / rejected buckets)
    # that existed at the window's end (keep_issue_for_window).
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
        seen.add(issue["number"])     # listed: never fetched again below
        # Not the window's (created after its end, or closed and untouched
        # since before its start: keep_issue_for_window), unless a kept PR
        # closes or cross-references it (train context).
        if issue["number"] not in wanted \
                and not keep_issue_for_window(issue, frm, to):
            continue
        raw_by_num[raw["number"]] = raw
        issues.append(issue)
    for n in sorted(wanted - seen):
        # A closed/cross-referenced issue can be gone: 404 (a number that is not
        # an issue in THIS repo) or 410 (deleted). Tolerate it (skip, one note)
        # rather than aborting the whole gather on one dangling reference. A
        # transferred issue is answered through a redirect to its new repo: an
        # object that is not #n of this repo is skipped too, never stored as
        # this repo's issue (#109).
        raw_issue, status = http_get_json(f"{api}/issues/{n}", token,
                                          allow_statuses=GONE_STATUSES)
        if raw_issue is None:
            sys.stderr.write(f"note: {owner}/{repo}: referenced issue #{n} "
                             f"skipped (HTTP {status})\n")
            continue
        why = fetched_item_mismatch(raw_issue, f"{owner}/{repo}", n)
        if why:
            sys.stderr.write(f"note: {owner}/{repo}: referenced issue #{n} "
                             f"skipped: the API answered with another item "
                             f"({why}; transferred?)\n")
            continue
        if raw_issue.get("pull_request"):
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
    # Code/symbol events are re-keyed to the same author. The same pass
    # notes, as a boolean per login, a commit email in a home domain (the
    # affiliation signal); the email itself is dropped.
    want_affiliation = getattr(args, "affiliation_enabled", True)
    aff_config = args_affiliation_config(args, owner) if want_affiliation else None
    email_home = set()
    if commits:
        # from the walked branch's head (the clone is back on it by now)
        api_items = fetch_all(get_page, commits_api_url(api, walk_head, frm, to)) \
            if walk_head else []
        known = set()
        for it in prs + issues:
            known.add(it.get("author"))
            known.add(it.get("merged_by"))
            known.update(it.get("reviewers") or [])
            for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
                known.add(c.get("author"))
        known.discard(None)
        by_sha = resolve_commit_authors(
            commits, commit_logins_from_api(api_items), known,
            home_domains=(aff_config or {}).get("home_email_domains") or (),
            home_email=email_home)
        remap_event_authors(code_events, by_sha)
        remap_event_authors(symbol_events, by_sha)

    workflows = []
    workflow_stats = {}
    if getattr(args, "include_workflows", True):
        raw_runs, runs_truncation = fetch_workflow_runs(get_page, api, frm, to)
        workflows = [normalize_workflow(r) for r in raw_runs]
        workflow_stats = mark_workflow_stats_truncated(
            aggregate_workflow_stats(workflows), runs_truncation)

    # First-time contributors (per-window fact `newcomers`): see
    # collect_newcomers. --no-newcomers records that it was not looked at.
    if getattr(args, "newcomers", True):
        newcomers = collect_newcomers(prs, issues, owner, repo, frm, to, get_page)
    else:
        newcomers = {"collected": False}

    # Contributor affiliation (per-window fact `affiliations`): see
    # collect_affiliations. --no-affiliation records that it was not looked at.
    # With the retained people registry (--people / manifest `people`) the
    # registry is consulted first and updated here, before the fold.
    if want_affiliation:
        cache = getattr(args, "affiliation_cache", None)
        people_run = people_run_from_args(args)
        affiliations = collect_affiliations(
            prs, issues, commits, owner, repo, get_page, aff_config,
            email_home=email_home, cache={} if cache is None else cache,
            people_run=people_run,
            name_first=[e.get("login") for key in ("new", "returning")
                        for e in newcomers.get(key) or [] if isinstance(e, dict)])
        if people_run is not None:
            try:
                people_run.save()
            except (OSError, people_mod.RegistryError) as exc:
                raise SystemExit("error: people registry {}: {}".format(
                    people_run.path, exc)) from None
    else:
        affiliations = {"collected": False}

    releases = []
    if getattr(args, "include_releases", True):
        raw_releases = fetch_all(get_page, f"{api}/releases?per_page=100")
        releases = [normalize_release(r) for r in raw_releases
                    if in_window(r.get("published_at"), frm, to)]

    raw_milestones = fetch_all(get_page, f"{api}/milestones?state=all&per_page=100")
    milestones = [normalize_milestone(m) for m in raw_milestones]

    # Phase 12: Projects v2 board. Auto-discover via GraphQL (with the closing-issue
    # read above, one of gather's two GraphQL uses), stamp each gathered pr/issue with its board_status + iteration,
    # and surface the (date-windowed) sprints. Degrades to empty on any error so a
    # missing read:project scope NEVER hard-fails the run; --no-project-board skips
    # the query entirely.
    sprints = {}
    board_truncated = []
    if getattr(args, "project_board", True):
        _graphql = _graphql_client(token)
        # ref_date (the window's reference point) anchors the board staleness
        # check; it is finalized below but resolves to the same value here.
        sprints, items_by_ref = fetch_project_board(
            _graphql, owner, repo, max_items=_board_max_items(),
            ref_date=getattr(args, "ref_date", None) or to,
            truncations=board_truncated)
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
        "branches": branches, "clone_dir": clone_dir,
        # the analyzed mainline branch: PRs merged into other branches are stacked/
        # fork contributions, not shipped-to-main (link.build_trains keys on this).
        # None when unknown (see resolve_branches); fold stores it per window.
        "base_branch": base_branch,
        # the commit the structure (code graph, IaC, CODEOWNERS) was read at,
        # and whether it is the window's end or the head (issue #107)
        "ref_date": ref_date, "clone_sha": clone_sha,
        "structure_as_of": structure_as_of,
        "clone_mode": clone_mode,
        "boundary_dropped_commits": _boundary_dropped,
        "period": {"from": frm, "to": to}, "prev_bundle": None,
    }
    bundle = build_bundle(meta, commits, prs, issues)
    bundle["workflows"] = workflows
    bundle["workflow_stats"] = workflow_stats
    # Whether CI runs were collected at all (--no-workflows skips them): lets a
    # reader tell "no runs" from "not looked at" (per-window facts, issue #94).
    bundle["meta"]["workflows_collected"] = bool(
        getattr(args, "include_workflows", True))
    # Where `closes` came from (issue #105): "graphql" (keywords + GitHub's
    # closingIssuesReferences) or "keywords" (GraphQL unavailable).
    bundle["meta"]["closing_refs_source"] = closing_refs_source
    # Boards whose items were cut short (item cap / page guard / failed page,
    # #109): present only when one was, so a complete board changes nothing.
    if board_truncated:
        bundle["meta"]["project_board_truncated"] = board_truncated
    bundle["releases"] = releases
    bundle["milestones"] = milestones
    bundle["newcomers"] = newcomers
    bundle["affiliations"] = affiliations
    # Omitted when the window's history was not on disk (nothing was read).
    if module_notes is not None:
        bundle["module_notes"] = module_notes
    if pr_history is not None:
        bundle["pr_history"] = pr_history
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
# passes `make_backfill_fetcher(token)` (REST, PRs and issues only).
# `fetch(kind, local, qid)` returns either None (genuinely
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


def _backfill_record_kind(local):
    """"pr" / "issue" for a `pr-<n>` / `issue-<n>` local id (the only ids a
    spine traversal can miss: the dst of closes / cross_ref / part_of), else
    None. Returns (kind, number)."""
    for kind in ("pr", "issue"):
        head = kind + "-"
        if local.startswith(head) and local[len(head):].isdigit():
            return kind, int(local[len(head):])
    return None, None


def _latest_code_graph(conn, project, repo):
    """The code graph of the repo's latest gathered window by date (#94
    per-window facts) — the one a view spanning the store reads — else the
    latest-wins `codegraph` singleton (a store folded before #94), else {}."""
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    windows = sorted({(w.get("from"), w.get("to")) for w in ledger
                      if isinstance(w, dict) and w.get("project") == project
                      and w.get("repo") == repo and w.get("from") and w.get("to")})
    for frm, to in reversed(windows):
        wf = graphstore.get_window_facts(conn, project, repo, frm, to,
                                         keys=("code_graph",))
        if wf is not None:
            return (wf.get("facts") or {}).get("code_graph") or {}
    node = graphstore.get_node(conn, graphstore.qualify_id(project, repo,
                                                           "codegraph"))
    return ((node or {}).get("data") or {}) if node else {}


def _backfill_people(conn, kind, record, project, repo, rid):
    """The person records for the logins of ONE backfilled PR / issue, derived
    as fold_bundle derives them (derive.enumerate_participants, after
    attach_commit_prs): a PR reviewer inherits the areas of the stored commits
    that are `part_of` the PR, under the repo's latest code graph. Only the
    record's own logins are returned (commit authors were folded already)."""
    view = {"prs": [record]} if kind == "pr" else {"issues": [record]}
    logins = set(derive.enumerate_participants(view, {}))
    commits = []
    if kind == "pr":
        for e in graphstore.get_edges(conn, rid, direction="in",
                                      edge_types=["part_of"]):
            node = graphstore.get_node(conn, e["src_id"])
            data = (node or {}).get("data") or {}
            if node and node["node_class"] == "code" and "sha" in data:
                commits.append(copy.deepcopy(data))
    attach_commit_prs(commits)
    idx = derive.area_index(_latest_code_graph(conn, project, repo))
    people = derive.enumerate_participants({**view, "commits": commits}, idx)
    return {login: people[login] for login in sorted(logins)}


def _backfill_members(conn, project, repo):
    """The member set a manifest fold would pass for a project store (the
    stored repos plus those with recorded member run status, as
    validate._project_members reads it), or None for a single-repo store
    (repo without an owner/ part)."""
    if "/" not in repo:
        return None
    members = set(graphstore.project_repos(conn, project))
    try:
        members |= {e["repo"] for e in graphstore.get_member_status(conn, project)}
    except (ValueError, TypeError, KeyError):
        pass
    members.add(repo)
    return members


def backfill(conn, id, fetch=None):
    """Fetch and upsert ONE missing PR / issue, written exactly as a fold
    writes a record (issue #113).

    Idempotent: if `get_node(id)` already returns a node, this is a no-op and
    NOTHING is fetched. Only a `pr-<n>` / `issue-<n>` id is fetched — the only
    ids a spine traversal can miss (the dst of closes / cross_ref / part_of);
    any other id returns fetched=False without calling the seam (a commit,
    artifact, review or structure node written from a seam alone would be a
    row the trust check rejects).

    `fetch(kind, local, qid)` is the network seam with THREE outcomes:
      - a dict `{"node": raw, ...}` == fetched (any `edges` the seam returns
        are ignored: the edges are derived from the record, below);
      - `gather.ABSENT` == the id DEFINITIVELY does not exist (a 404) —
        `backfill` tombstones it via `graphstore.record_dead_ref` so it is
        pruned and never re-chased;
      - `None` == transient/unreachable (could not be resolved this run).
    Tests pass a fixture-backed fake; production passes `make_backfill_fetcher`.

    A fetched record is written through the fold's own derivation, in one
    transaction: `record_rows` (the record node, its closes / cross_ref and
    — in a project store — cross-repo spine edges, its contribution edges,
    its review / lifecycle leaves), the person nodes of its logins
    (`_backfill_people` + `_person_rows`, the fold's union), its FTS text,
    and the item-relation edges (blocks / in_milestone / in_iteration) that
    `item_relation_edges` derives for the stored records and that touch it.
    The store's other rows are left alone. A record is stored under its id's
    kind: an `issue-N` whose #N is a PR (the seam follows the thread and
    returns the PR) is an issue record to every reader and to the trust
    check, so it gets an issue's edges. A record whose `number` differs from
    the id's is not written (fetched=False); a record without one takes it.

    Returns `{"fetched": bool, "absent": bool, "id": id, "edges_added": int}` —
    `absent=True` only on the ABSENT outcome; `edges_added` counts the edges
    derived for the record.
    """
    miss = {"fetched": False, "absent": False, "id": id, "edges_added": 0}
    if graphstore.get_node(conn, id) is not None:
        return miss  # already present

    if fetch is None:
        raise ValueError("backfill needs a `fetch` seam (production: "
                         "make_backfill_fetcher(token); tests: a fake)")
    info = classify_id(id)
    kind, number = _backfill_record_kind(info["local"])
    if kind is None:
        return miss  # not a record a traversal can miss: never fetched
    # The scope is `{project}/{repo}`; recover project/repo so the upsert lands
    # in the same identity columns fold_bundle uses.
    project, _, repo = graphstore.parse_id(id)["scope"].partition("/")

    result = fetch(info["kind"], info["local"], id)
    if result is ABSENT:
        graphstore.record_dead_ref(conn, id)  # only gather writes
        return dict(miss, absent=True)
    if not result or not result.get("node"):
        return miss  # unreachable / transient

    raw = dict(result["node"])
    if raw.get("number") is None:
        raw["number"] = number
    elif raw["number"] != number:
        return miss  # not the record this id names
    fetched_at = graphstore.now_iso()
    rows = record_rows(kind, raw, project, repo, fetched_at,
                       _backfill_members(conn, project, repo))
    people = _backfill_people(conn, kind, raw, project, repo, id)
    comments = (raw.get("comments_list") or []) + (
        (raw.get("review_comments") or []) if kind == "pr" else [])
    text = _social_text(raw, comments)
    with conn:
        graphstore.upsert_nodes(
            conn, rows["nodes"] + _person_rows(conn, project, people, fetched_at),
            commit=False)
        graphstore.upsert_edges(conn, rows["edges"], commit=False)
        if text and graphstore.fts5_available(conn):
            graphstore.index_texts(conn, [(id, text)], commit=False)
        # Item relations now implied for (or by another stored record towards)
        # this record; a fold rebuilds the whole set, backfill only adds these.
        relations = [(k[0], k[1], k[2], None, v) for k, v in sorted(
            item_relation_edges(conn, project, repo).items(),
            key=lambda kv: kv[0]) if id in (k[0], k[1])]
        graphstore.upsert_edges(conn, relations, commit=False)
    return {"fetched": True, "absent": False, "id": id,
            "edges_added": len(rows["edges"]) + len(relations)}


def _backfilled_is(raw, slug, num, qid):
    """True when the REST object fetched for backfill id `qid` really is
    #`num` of `slug` (fetched_item_mismatch); else one stderr note and False,
    so the node stays a warned gap rather than storing another repo's item
    under this id (a transferred issue answers through a redirect, #109)."""
    try:
        number = int(num)
    except (TypeError, ValueError):
        number = num
    why = fetched_item_mismatch(raw, slug, number)
    if why:
        sys.stderr.write(f"note: backfill {qid}: the API answered with another "
                         f"item ({why}; transferred?); not stored\n")
        return False
    return True


def make_backfill_fetcher(token):
    """Production `fetch` seam for `backfill`: resolves one PR / issue by
    GitHub REST (normalize_pr / normalize_issue). Returns a `fetch(kind, local,
    qid)` callable.

    NOT unit-tested (it is the live-network edge); the suite exercises backfill
    with a fixture-backed fake instead. Only social PR / issue ids are
    resolved; anything else returns None and stays an honest gap. Commits are
    not backfilled (#113 m4): a commit is never the dst of a spine edge, so no
    traversal misses one, and a commit written on demand would lack the
    author resolution, `committed_at`, file list and touches a gather derives.

    A `Closes #N` whose #N resolves to a PR (issues + PRs share one number
    space) is followed to the real PR and returned as a node — the thread is
    traversed, not dropped. Outcomes: a fetched dict, `ABSENT` (a genuine 404,
    or 410 for a deleted issue), or `None` (transient / not resolvable this
    run, or an answer that is not the requested item of this repo: a
    transferred issue reached through a redirect, #109). The returned `edges`
    are empty: backfill derives a record's edges itself (record_rows).
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
            raw, nxt = http_get_json(f"{api}/issues/{num}", token,
                                     allow_statuses=GONE_STATUSES)
            if raw is None:
                return ABSENT
            if not _backfilled_is(raw, slug, num, qid):
                return None
            if raw.get("pull_request"):
                # #N is actually a PR (issues + PRs share one number space). The
                # parsed `issue-N` is gather's mis-classification, not a phantom:
                # the PR is real, so follow the thread and resolve it to the PR.
                # It is returned under the referenced id so the existing `closes`
                # edge connects; its /pull/ html_url lets the reader label it a PR
                # and anchor the train's headline to in-window work, so an ancient
                # cross-ref is traversed as context without hijacking the title.
                pr_raw, pnxt = http_get_json(f"{api}/pulls/{num}", token,
                                             allow_statuses=GONE_STATUSES)
                if pr_raw is None:
                    return ABSENT
                if not _backfilled_is(pr_raw, slug, num, qid):
                    return None
                return {"node": normalize_pr(pr_raw), "edges": []}
            return {"node": normalize_issue(raw), "edges": []}
        if kind == "social" and local.startswith("pr-"):
            num = local[len("pr-"):]
            raw, nxt = http_get_json(f"{api}/pulls/{num}", token,
                                     allow_statuses=GONE_STATUSES)
            if raw is None:
                return ABSENT
            if not _backfilled_is(raw, slug, num, qid):
                return None
            return {"node": normalize_pr(raw), "edges": []}
        return None  # commit / structure / artifact: not fetched on demand

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
    External refs keep the spelling they were written with.

    GitHub-reported closing issues stored on the PR (`closing_issues`, issue
    #105: keywords AND Development-sidebar links) are `closes` refs like a
    keyword-closed `owner/repo#N`, and an issue mention they also close is
    not a separate cross_ref. Pure."""
    pid = graphstore.qualify_id(project, current_repo, "pr-{}".format(pr["number"]))
    text = (pr.get("title", "") or "") + "\n" + (pr.get("body") or "")
    closing = [dict(r, kind="closes", is_pr=False)
               for r in pr.get("closing_issues") or []]
    closed = {(r["owner"].lower(), r["repo"].lower(), r["number"])
              for r in closing}
    refs = parse_qualified_refs(text) + list(pr.get("timeline_xrefs") or []) \
        + closing
    index = manifest_mod.slug_index(members)
    current = (current_repo or "").lower()
    edges, external, seen = [], [], set()
    for r in refs:
        slug = "{}/{}".format(r["owner"], r["repo"])
        if slug.lower() == current:
            continue
        if (r["kind"] == "cross_ref" and not r.get("is_pr")
                and (r["owner"].lower(), r["repo"].lower(), r["number"]) in closed):
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


def resolve_bicep_registry_member(ref, project, members, prefix_by_slug,
                                  exclude=None):
    """Resolve a Bicep public-registry module ref to the qualified id of the
    publishing member's module area, or None (issue #92).

    `ref` is the raw ref an edge carries. Only a PUBLIC registry ref
    (`br/public:<path>:<tag>` / `br:mcr.microsoft.com/bicep/<path>:<tag>`,
    _bicep_ref_parts) can resolve; a private registry, template spec, alias or
    local ref never does. The publisher is the member whose manifest
    `bicep_registry_prefix` (`prefix_by_slug`, normalised without a trailing '/')
    is a whole-segment, case-insensitive prefix of `<path>`; the longest prefix
    wins, then the sorted slug. Its module area is classify_code_area of
    `<path>/main.bicep` (so `avm/res/<group>/<module>`): the registry path is the
    module's directory in the publisher's repo, as in bicep-registry-modules. An
    AVM group path is never a module. `exclude` (the referencing member) is never
    a target: its own unresolved ref names a module it does not hold, and an
    in-repo edge is the build/scan's job. The id uses the member's manifest
    spelling (members match case-insensitively). Pure, deterministic."""
    if not ref or not prefix_by_slug:
        return None
    path = _bicep_ref_parts(ref)[0]
    if not path:
        return None
    path = path.strip("/")
    low = path.lower()
    prefixes = {}
    for slug, prefix in sorted(prefix_by_slug.items()):
        if prefix:
            prefixes.setdefault(slug.lower(), prefix.strip("/").lower())
    skip = exclude.lower() if exclude else None
    best = None
    for slug in sorted(members or ()):
        pre = prefixes.get(slug.lower())
        if not pre or slug.lower() == skip:
            continue
        if low != pre and not low.startswith(pre + "/"):
            continue
        if best is None or len(pre) > best[0]:
            best = (len(pre), slug)
    if best is None:
        return None
    area = classify_code_area(path + "/main.bicep", DEFAULT_AREA_PATTERNS)
    if not area or _is_avm_group_area(area):
        return None
    return graphstore.qualify_id(project, best[1], "area-{}".format(area))


def _fold_depends_on(bundle, project, repo, members, registry_by_slug,
                     bicep_prefix_by_slug=None):
    """Yield (src_qid, dst_qid, data) depends_on triples from a bundle's area
    edges. Resolved-local edges -> intra-repo. Unresolved registry edges resolve
    cross-repo ONLY in multi-repo folds (`members` non-empty and
    `registry_by_slug` not None); single-repo folds skip them (byte-stable). A
    Bicep edge (`provider: "bicep"`) resolves through the members' public Bicep
    registry prefixes (`bicep_prefix_by_slug`, resolve_bicep_registry_member) to
    the publisher's module area; any other edge through the Terraform registry
    (resolve_registry_member) to a member's root area. Either way the stored edge
    gains `resolved`/`cross_repo` true. `examples/`/`tests/` source areas are
    skipped (scaffolding, not module deps). Pure."""
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
                if e.get("provider") == "bicep":
                    dst = resolve_bicep_registry_member(
                        e["ref"], project, members, bicep_prefix_by_slug,
                        exclude=repo)
                else:
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


def record_rows(kind, record, project, repo, fetched, members=None):
    """The store rows ONE PR (`kind` "pr") or issue (`kind` "issue") record
    implies, as fold_bundle writes them — shared by fold_bundle and
    gather.backfill so a backfilled record is written exactly as a folded one
    (issue #113). Pure. Returns {"nodes": [node rows], "edges": [edge rows]}:

      - the record's `social` node (`pr-<n>` / `issue-<n>`; ts = merged/closed/
        created for a PR, closed/updated for an issue). In a multi-repo project
        (`members` given) a PR's qualified refs and repo-aware timeline xrefs
        also yield CROSS-repo spine edges (to member repos) or honest
        `external_refs` on the record (to non-members) — see
        _cross_repo_pr_edges; `members=None` (single repo) skips that;
      - a PR's `closes` / `cross_ref` spine edges to its issues;
      - the contribution edges (person -> record): PR authored / merged /
        reviewed / commented, issue reported / commented. Absent logins are
        skipped; the person NODES are the caller's (_person_rows, over
        derive.enumerate_participants);
      - Phase 10: PR review submissions and PR/issue lifecycle events as
        `social` leaves with a `part_of` spine edge to the record (a review
        without a url gets a stable `<pr url>#pullrequestreview-<id>`, an event
        `_lifecycle_event_data`'s). Leaves have no onward spine edge, so they
        ride into the record's train but cannot bridge it to anything else.
    Current-state edges (blocks / in_milestone / in_iteration) are not here:
    they need the stored milestones / sprints / issues (item_relation_edges)."""
    def qid(local):
        return graphstore.qualify_id(project, repo, local)
    nodes, edges = [], []
    num = record["number"]
    local = "{}-{}".format(kind, num)
    rid = qid(local)

    def contrib(login, etype):
        if login:
            edges.append((graphstore.qualify_person(project, login), rid, etype,
                          None, None))

    if kind == "pr":
        ts = record.get("merged_at") or record.get("closed_at") \
            or record.get("created_at")
        stored = record
        if members is not None:
            external, xedges = _cross_repo_pr_edges(record, project, repo, members)
            if external:
                stored = {**record, "external_refs": external}
            edges.extend(xedges)
        nodes.append((rid, project, repo, "social", ts, stored, fetched))
        for n in record.get("closes") or []:
            edges.append((rid, qid("issue-{}".format(n)), "closes", None, None))
        for n in record.get("crossref_issues") or []:
            edges.append((rid, qid("issue-{}".format(n)), "cross_ref", None, None))
        contrib(record.get("author"), "authored")
        contrib(record.get("merged_by"), "merged")
        for reviewer in record.get("reviewers") or []:
            contrib(reviewer, "reviewed")
        for c in (record.get("comments_list") or []) \
                + (record.get("review_comments") or []):
            contrib(c.get("author"), "commented")
        for rv in record.get("reviews") or []:
            rvid = rv.get("id")
            data = rv
            if not rv.get("url"):
                base = record.get("url") or local
                data = {**rv, "url": "{}#pullrequestreview-{}".format(base, rvid)}
            leaf = qid("review-{}-{}".format(num, rvid))
            nodes.append((leaf, project, repo, "social", rv.get("submitted_at"),
                          data, fetched))
            edges.append((leaf, rid, "part_of", None, None))
    elif kind == "issue":
        ts = record.get("closed_at") or record.get("updated_at")
        nodes.append((rid, project, repo, "social", ts, record, fetched))
        contrib(record.get("author"), "reported")
        for c in record.get("comments_list") or []:
            contrib(c.get("author"), "commented")
    else:
        raise ValueError("record_rows: kind must be 'pr' or 'issue', got "
                         "{!r}".format(kind))
    for ev in record.get("lifecycle") or []:
        data = _lifecycle_event_data(ev, record.get("url"), local)
        leaf = qid("event-{}-{}".format(local, ev.get("id")))
        nodes.append((leaf, project, repo, "social", ev.get("created_at"), data,
                      fetched))
        edges.append((leaf, rid, "part_of", None, None))
    return {"nodes": nodes, "edges": edges}


def _person_rows(conn, project, people, fetched, replace=False):
    """People `structure` node rows (project-scoped) for `people` ({login:
    {modules, areas, is_bot, ...}} from derive.enumerate_participants), as
    fold_bundle and gather.backfill write them. The `repo` column is the
    project sentinel "*" so the SAME login folded from another repo in the
    project upserts the identical id (the sentinel keeps the row out of any
    single repo's repo_nodes view). NULL ts -> excluded from window scans.

    Project-scoped people aggregate across members: a later fold UNIONs its
    attribution with what is already stored — a plain upsert would OVERWRITE,
    so the last member folded (e.g. one where the person has no area
    attribution) would erase another member's. Union keeps the person node the
    true project-wide aggregate, independent of fold order and idempotent
    (re-folding unions a subset -> no change). `replace` (a re-fold of the
    project's only gathered window, #111) writes this derivation as it is."""
    rows = []
    for login, rec in people.items():
        if not login:
            continue
        pid = graphstore.qualify_person(project, login)
        data = {"login": login, **rec}
        prior = None if replace else conn.execute(
            "SELECT data FROM nodes WHERE id=?", (pid,)).fetchone()
        if prior and prior[0]:
            pd = json.loads(prior[0])
            data["areas"] = sorted(set(rec.get("areas") or []) | set(pd.get("areas") or []))
            data["modules"] = sorted(set(rec.get("modules") or []) | set(pd.get("modules") or []))
            data["is_bot"] = bool(rec.get("is_bot")) or bool(pd.get("is_bot"))
        rows.append((pid, project, "*", "structure", None, data, fetched))
    return rows


def _fold_artifacts(conn, bundle, project, repo, fetched):
    """Artifact `code` node rows for one fold, rebuilt from the repo's WHOLE
    stored ledger (#76). Call inside the fold's transaction, after this bundle's
    ledger rows and move edges are written.

    The ledger keeps every window's rows, so each node is
    derive.artifact_projection over all of them (lifecycle in date order, status
    from the last event) plus the moves recorded by every fold — the same readers
    (extract.ledger_events / recorded_moves) and rule validate's no_drift
    re-runs. The store therefore equals its re-derivation by construction, and
    folding windows in any order, or re-folding one, converges.

    Only nodes this bundle can change are rewritten: the artifacts its events
    name, plus the other endpoint of any recorded move touching one of them (a
    move's ranking reads both endpoints' lifecycles). A bundle with no code or
    symbol events changes no artifact and skips the ledger read."""
    if not (bundle.get("code_events") or bundle.get("symbol_events")):
        return []
    affected = set(derive.build_artifacts(bundle))
    moves = extract.recorded_moves(conn, project, repo)
    for m in moves:
        if m["from"] in affected or m["to"] in affected:
            affected.update((m["from"], m["to"]))
    code_events, symbol_events = extract.ledger_events(conn, project, repo)
    artifacts = derive.artifact_projection(
        {"meta": bundle.get("meta", {}), "code_events": code_events,
         "symbol_events": symbol_events,
         "commits": extract.commit_times(conn, project, repo)}, moves)
    nodes = []
    for local, art in artifacts.items():
        if local not in affected:
            continue
        # ts: the artifact's last lifecycle event date (its most-recent activity);
        # None when it has no events (degrades cleanly, excluded from window scans).
        lc = art.get("lifecycle") or []
        ts = lc[-1]["date"] if lc else None
        nodes.append((graphstore.qualify_id(project, repo, local), project, repo,
                      "code", ts, art, fetched))
    return nodes


# --- current-state edges (issue #95) -------------------------------------------
#
# History edges (touches, review / event part_of, symbol moves) record
# something that happened and are only ever added; the record-derived ones
# (authored, reviewed, merged, commented, reported, closes, cross_ref, commit
# part_of) follow the record they come from (issue #111, "record-derived
# edges" below). CURRENT-STATE edges describe the latest known state of
# an input that can change, so a fold keeps them equal to what the repo's
# STORED inputs imply and retracts the ones those inputs no longer imply:
#
#   blocks / in_milestone / in_iteration  <- the stored PR/issue records (the
#       record a fold upserts replaces the earlier one) and the stored
#       milestone / sprint nodes;
#   depends_on / owns  <- the per-window code graph and CODEOWNERS facts
#       (graphstore.set_window_facts), replayed in window order.
#
# A source is only ever re-derived from input that speaks for it: a record
# that is stored, an area that a window's build resolved, a window whose code
# graph carries the whole-tree structural scan, an area that a window's code
# graph lists together with that window's CODEOWNERS. Absence of evidence (a
# window-scoped directory graph that does not mention an area, a build that
# timed out, a member that failed) never retracts anything.
CURRENT_STATE_EDGE_TYPES = ("blocks", "depends_on", "in_iteration",
                            "in_milestone", "owns")
_ITEM_RELATION_TYPES = ("blocks", "in_iteration", "in_milestone")


def _window_order(window):
    """Sort key of a gathered window (from, to): by end day, then start day —
    the order extract._window_facts uses to pick the latest window."""
    frm, to = window
    return (to[:10], frm[:10], frm, to)


def _owns_pairs(code_graph, code_owners, project, repo):
    """[(person_qid, area_qid)] owns edges for one code graph + CODEOWNERS, in
    write order: an area is owned by the owners of the LAST rule matching the
    area directory (derive.area_owners; graphify community areas: union over
    their files). Pure."""
    rules = derive.compile_codeowners(code_owners)
    out = []
    if not rules:
        return out
    areas_by_id = {}
    for area in (code_graph or {}).get("areas") or []:
        areas_by_id[area.get("id") or area.get("name") or area.get("path")] = area
    for aid in sorted(a for a in areas_by_id if a):
        for login in derive.area_owners(rules, aid,
                                        areas_by_id[aid].get("paths") or []):
            if login:
                out.append((graphstore.qualify_person(project, login),
                            graphstore.qualify_id(project, repo,
                                                  "area-{}".format(aid))))
    return out


def _owns_authority(code_graph, code_owners, clone_sha, project, repo):
    """The area qids whose owners a window's facts state: every area its code
    graph lists, when the window read a CODEOWNERS file (non-empty
    `code_owners`) or a checkout (`clone_sha`: then a missing file means no
    owners). Else none — a window without either says nothing, nor does one
    whose CODEOWNERS file was there but could not be read (`code_graph`
    carries `codeowners_unread`, issue #109). Pure."""
    if not (code_owners or clone_sha):
        return set()
    if (code_graph or {}).get("codeowners_unread"):
        return set()
    out = set()
    for area in (code_graph or {}).get("areas") or []:
        aid = area.get("id") or area.get("name") or area.get("path")
        if aid:
            out.add(graphstore.qualify_id(project, repo, "area-{}".format(aid)))
    return out


def _dep_is_transitive(data):
    return bool((data or {}).get("transitive"))


class StructureReplay:
    """depends_on / owns of one repo replayed over its gathered windows' facts
    (issue #95). Pure: built from [(from, to, code_graph, code_owners,
    clone_sha)] (any order; replayed in _window_order) plus the fold's member
    set and registries (cross-repo depends_on, as _fold_depends_on).

    Authority — which window speaks for a source:
      - depends_on of area A, DIRECT edges (`transitive` false): a window whose
        build of A resolved (`edges_status: "resolved"`), or whose code graph
        carries the whole-tree structural scan (`structural_scan: true`; then
        for EVERY area of the repo, listed or not, except the areas in its
        `structural_scan_unread`, whose module files it could not read);
      - depends_on of area A, TRANSITIVE edges: only a window whose build of A
        resolved (the scan sees direct `module` refs only);
      - owns of area A: a window whose code graph lists A and that read a
        CODEOWNERS file or a checkout (_owns_authority).
    The in-effect windows of a source are its latest authoritative window and
    every later one (all windows when none is authoritative); its edges are
    the union of the in-effect windows' derivations, each edge carrying the
    data of the latest window that derives it. So the result depends only on
    the SET of windows' facts, never on the order they were folded in."""

    def __init__(self, windows, project, repo, members=None,
                 registry_by_slug=None, bicep_prefix_by_slug=None):
        self.project, self.repo = project, repo
        self.windows = sorted(windows, key=lambda w: _window_order(w[:2]))
        self.dep, self.resolved, self.scan, self.scan_unread = [], [], [], []
        self.owns, self.own_auth, self.graphs = [], [], []
        for frm, to, cg, co, sha in self.windows:
            cg = cg or {}
            self.graphs.append(cg)
            self.dep.append(list(_fold_depends_on(
                {"code_graph": cg}, project, repo, members, registry_by_slug,
                bicep_prefix_by_slug)))
            self.resolved.append({
                graphstore.qualify_id(project, repo, "area-{}".format(a["id"]))
                for a in cg.get("areas") or []
                if a.get("id") is not None and not _is_scaffold_area(a.get("id"))
                and a.get("edges_status") == "resolved"})
            self.scan.append(cg.get("structural_scan") is True)
            # Areas whose module files that scan could not read (issue #109):
            # the scan does not speak for them.
            self.scan_unread.append({
                graphstore.qualify_id(project, repo, "area-{}".format(aid))
                for aid in cg.get("structural_scan_unread") or ()})
            self.owns.append(_owns_pairs(cg, co, project, repo))
            self.own_auth.append(_owns_authority(cg, co, sha, project, repo))

    def dep_latest(self, src, transitive):
        """Index of the latest window authoritative for `src`'s direct
        (transitive=False) or transitive depends_on edges, or None."""
        for i in range(len(self.windows) - 1, -1, -1):
            if src in self.resolved[i] or (not transitive and self.scan[i]
                                           and src not in self.scan_unread[i]):
                return i
        return None

    def own_latest(self, area):
        """Index of the latest window authoritative for `area`'s owners."""
        for i in range(len(self.windows) - 1, -1, -1):
            if area in self.own_auth[i]:
                return i
        return None

    def dep_in_effect(self, src, transitive):
        """The window indices whose depends_on edges of `src` (that class)
        are in effect, oldest first."""
        latest = self.dep_latest(src, transitive)
        return list(range(latest or 0, len(self.windows)))

    def desired(self):
        """{(src, dst, type): data} — the repo's depends_on and owns edges."""
        out = {}
        dep_cache, own_cache = {}, {}
        for i in range(len(self.windows)):
            for src, dst, data in self.dep[i]:
                ck = (src, _dep_is_transitive(data))
                if ck not in dep_cache:
                    dep_cache[ck] = self.dep_latest(*ck)
                if dep_cache[ck] is None or i >= dep_cache[ck]:
                    out[(src, dst, "depends_on")] = data or None
            for person, area in self.owns[i]:
                if area not in own_cache:
                    own_cache[area] = self.own_latest(area)
                if own_cache[area] is None or i >= own_cache[area]:
                    out[(person, area, "owns")] = None
        return out

    def retractable(self, key, data):
        """True when a stored depends_on / owns edge not in desired() has an
        authoritative window that says it is gone."""
        src, dst, etype = key
        if etype == "depends_on":
            return self.dep_latest(src, _dep_is_transitive(data)) is not None
        if etype == "owns":
            return self.own_latest(dst) is not None
        return False


def repo_fact_windows(conn, project, repo):
    """[(from, to, code_graph, code_owners, clone_sha)] for every gathered
    window of (project, repo), or None when one of them has no per-window
    facts (folded before issue #94, or without a from/to): depends_on / owns
    cannot then be replayed."""
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        return None
    keys = sorted({(w.get("from"), w.get("to")) for w in ledger
                   if isinstance(w, dict) and w.get("project") == project
                   and w.get("repo") == repo},
                  key=lambda k: (str(k[0]), str(k[1])))
    out = []
    for frm, to in keys:
        if not frm or not to:
            return None
        got = graphstore.get_window_facts(conn, project, repo, frm, to,
                                          keys=("code_graph", "code_owners"))
        if got is None or got.get("missing"):
            return None
        facts = got.get("facts") or {}
        out.append((frm, to, facts.get("code_graph"), facts.get("code_owners"),
                    got.get("clone_sha")))
    return out


def item_relation_edges(conn, project, repo, bundle_milestones=(),
                        bundle_sprints=()):
    """{(src, dst, type): None} — the blocks / in_milestone / in_iteration
    edges the repo's STORED PR/issue records imply (issue #95), read back
    after a fold has upserted its records:

      - in_milestone: a record's `milestone` (title, or number) names a stored
        milestone node (the fold's own milestones first, in bundle order, as
        before; then the stored ones by id);
      - in_iteration: a record's `iteration` is a sprint of the fold or a
        stored `sprint-<id>` node;
      - blocks: an issue record's `blocks` ref names another STORED issue
        (blocker -> blocked); an edge stands while either end's record states
        it.
    On a fold into an empty store this is exactly what the bundle alone
    implies."""
    prefix = graphstore.qualify_id(project, repo, "")

    def qid(local):
        return graphstore.qualify_id(project, repo, local)
    ms_local = {}
    for m in bundle_milestones or ():
        local = "milestone-{}".format(m.get("number") or m.get("title"))
        if m.get("title") is not None:
            ms_local[m["title"]] = local
        if m.get("number") is not None:
            ms_local.setdefault(m["number"], local)
    for nid, data in graphstore.nodes_with_prefix(conn, prefix + "milestone-"):
        local = nid[len(prefix):]
        if isinstance(data, dict):
            if data.get("title") is not None:
                ms_local.setdefault(data["title"], local)
            if data.get("number") is not None:
                ms_local.setdefault(data["number"], local)
    sprints = set(bundle_sprints or ())
    sprints |= {nid[len(prefix) + len("sprint-"):] for nid in
                graphstore.node_ids_with_prefix(conn, prefix + "sprint-")}
    issues = set(graphstore.node_ids_with_prefix(conn, prefix + "issue-"))
    out = {}
    for iid, rec in graphstore.item_relation_rows(conn, prefix):
        if not isinstance(rec, dict):
            continue
        local = ms_local.get(rec.get("milestone"))
        if local is not None:
            out[(iid, qid(local), "in_milestone")] = None
        sid = rec.get("iteration")
        if sid is not None and sid in sprints:
            out[(iid, qid("sprint-{}".format(sid)), "in_iteration")] = None
        if not iid.startswith(prefix + "issue-"):
            continue
        for b in rec.get("blocks") or []:
            other = qid("issue-{}".format(b["number"]))
            if other not in issues:
                continue
            src, dst = (iid, other) if b["direction"] == "out" else (other, iid)
            out[(src, dst, "blocks")] = None
    return out


# --- record-derived edges (issue #111) -------------------------------------------
#
# A PR / issue / commit record is stored latest-wins: the record a fold upserts
# replaces the earlier one. The edges derived from ONE record are a function of
# its current content, so after a fold upserts a record they must be exactly
# what the new content implies:
#
#   closes / cross_ref  src = the PR      <- its `closes` / `crossref_issues`
#                                            (+ cross-repo refs in a manifest
#                                            fold, _cross_repo_pr_edges)
#   part_of             src = the commit  <- its message (resolve_commit_pr)
#   authored / merged / reviewed / commented / reported
#                       dst = the PR / issue / commit, src = a person
#                                         <- its author / merged_by /
#                                            reviewers / comment authors
#   touches             src = the commit  <- its files AND the code graph:
#                                            re-derived only when the commit's
#                                            stored file list changed
#
# Only the records the bundle carries are touched (absence from a bundle is not
# evidence). A single-repo fold derives no cross-repo edge, so it leaves the
# cross-repo `closes` / `cross_ref` a manifest fold wrote alone. Review / event
# `part_of` edges (src: their own nodes), symbol moves and artifact nodes are
# ledger- or node-derived and not covered here. A person node no contribution
# edge references any more is deleted (people are project-scoped: an edge of
# any member keeps it).
RECORD_EDGE_TYPES = ("authored", "closes", "commented", "cross_ref", "merged",
                     "part_of", "reported", "reviewed")
CONTRIBUTION_EDGE_TYPES = ("authored", "commented", "merged", "reported",
                           "reviewed")
# Every edge type a person node is a contribution source of (validate's
# _CONTRIB_EDGE_TYPES): a person with none of these is not a participant.
_PERSON_KEEPING_TYPES = CONTRIBUTION_EDGE_TYPES + ("reacted",)
_SQL_CHUNK = 500


def _pr_spine_edges(pr, qid):
    """A PR's same-repo `closes` / `cross_ref` edge rows. Pure."""
    pid = qid("pr-{}".format(pr["number"]))
    out = [(pid, qid("issue-{}".format(n)), "closes", None, None)
           for n in pr.get("closes") or []]
    out += [(pid, qid("issue-{}".format(n)), "cross_ref", None, None)
            for n in pr.get("crossref_issues") or []]
    return out


def _commit_part_of(c, qid):
    """A commit's `part_of` edge row to the PR its subject names. Pure."""
    prn = resolve_commit_pr(c.get("message", ""))
    if prn is None:
        return []
    return [(qid(c["sha"]), qid("pr-{}".format(prn)), "part_of", None, None)]


def _contribution_edges(prs, issues, commits, qid, qperson):
    """The person -> record contribution edge rows of the RAW records, in write
    order; absent logins/fields are skipped. Pure."""
    out = []

    def contrib(login, dst_local, etype):
        if login:
            out.append((qperson(login), qid(dst_local), etype, None, None))
    for pr in prs:
        prlocal = "pr-{}".format(pr["number"])
        contrib(pr.get("author"), prlocal, "authored")
        contrib(pr.get("merged_by"), prlocal, "merged")
        for reviewer in pr.get("reviewers") or []:
            contrib(reviewer, prlocal, "reviewed")
        for c in (pr.get("comments_list") or []) + (pr.get("review_comments") or []):
            contrib(c.get("author"), prlocal, "commented")
    for iss in issues:
        isslocal = "issue-{}".format(iss["number"])
        contrib(iss.get("author"), isslocal, "reported")
        for c in iss.get("comments_list") or []:
            contrib(c.get("author"), isslocal, "commented")
    for c in commits:
        contrib(c.get("author"), c["sha"], "authored")
    return out


def record_edge_keys(prs, issues, commits, project, repo, members):
    """{(src, dst, type)} of the record-derived edges (RECORD_EDGE_TYPES) the
    given records imply — what fold_bundle writes for them, through the same
    helpers. Cross-repo `closes` / `cross_ref` only when `members` is given.
    Pure."""
    def qid(local):
        return graphstore.qualify_id(project, repo, local)

    def qperson(login):
        return graphstore.qualify_person(project, login)
    rows = []
    for pr in prs:
        if members is not None:
            rows.extend(_cross_repo_pr_edges(pr, project, repo, members)[1])
        rows.extend(_pr_spine_edges(pr, qid))
    for c in commits:
        rows.extend(_commit_part_of(c, qid))
    rows.extend(_contribution_edges(prs, issues, commits, qid, qperson))
    return {r[:3] for r in rows}


def _chunks(ids):
    ids = sorted(set(ids))
    for i in range(0, len(ids), _SQL_CHUNK):
        yield ids[i:i + _SQL_CHUNK]


def _edges_where(conn, column, ids, types):
    """{(src, dst, type)} of the edges whose `column` (src_id / dst_id) is in
    `ids` and whose type is in `types`: chunked IN lists on the (column,
    edge_type) index, so the cost is linear in len(ids)."""
    out = set()
    tph = ",".join("?" for _ in types)
    for chunk in _chunks(ids):
        sql = ("SELECT src_id, dst_id, edge_type FROM edges WHERE {} IN ({}) "
               "AND edge_type IN ({})").format(
                   column, ",".join("?" for _ in chunk), tph)
        out.update(tuple(r) for r in conn.execute(sql, list(chunk) + list(types)))
    return out


def stored_record_edges(conn, project, repo, members, pr_ids=(), issue_ids=(),
                        commit_ids=(), touch_ids=()):
    """{(src, dst, type)} of the stored record-derived edges of the given
    record ids: `closes` / `cross_ref` from the PRs (same-repo targets only
    when `members` is None), `part_of` from the commits, `touches` from the
    commits in `touch_ids`, and the contribution edges from a person of the
    project into any of them."""
    prefix = graphstore.qualify_id(project, repo, "")
    person_prefix = graphstore.qualify_person(project, "")
    out = {k for k in _edges_where(conn, "src_id", pr_ids, ("closes", "cross_ref"))
           if members is not None or k[1].startswith(prefix)}
    out |= _edges_where(conn, "src_id", commit_ids, ("part_of",))
    out |= _edges_where(conn, "src_id", touch_ids, ("touches",))
    out |= {k for k in _edges_where(conn, "dst_id",
                                    set(pr_ids) | set(issue_ids) | set(commit_ids),
                                    CONTRIBUTION_EDGE_TYPES)
            if k[0].startswith(person_prefix)}
    return out


def delete_orphan_people(conn, ids):
    """Delete each person node in `ids` that no contribution edge
    (_PERSON_KEEPING_TYPES) has as its source any more, in any repo of the
    project. Returns the deleted ids, sorted. Call inside the fold's
    transaction."""
    removed = []
    tph = ",".join("?" for _ in _PERSON_KEEPING_TYPES)
    for pid in sorted(set(ids)):
        if conn.execute("SELECT 1 FROM edges WHERE src_id=? AND edge_type IN "
                        "({}) LIMIT 1".format(tph),
                        [pid] + list(_PERSON_KEEPING_TYPES)).fetchone():
            continue
        if conn.execute("DELETE FROM nodes WHERE id=?", (pid,)).rowcount:
            removed.append(pid)
    return removed


def _stored_commit_files(conn, project, repo, shas):
    """{sha: files} of the stored commit records among `shas` (absent ones
    left out), read by id before a fold replaces them."""
    out = {}
    ids = {graphstore.qualify_id(project, repo, s): s for s in shas}
    for chunk in _chunks(ids):
        for nid, raw in conn.execute(
                "SELECT id, data FROM nodes WHERE id IN ({}) AND "
                "node_class='code'".format(",".join("?" for _ in chunk)), chunk):
            try:
                data = json.loads(raw) if raw else None
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("sha") == ids[nid]:
                out[ids[nid]] = list(data.get("files") or [])
    return out


def _stored_records(conn, project, repo):
    """(prs, issues, commits): every stored PR / issue / commit record of the
    repo (for the one-time heal of a repo last folded before #111)."""
    prefix = graphstore.qualify_id(project, repo, "")
    lo, hi = graphstore._prefix_range(prefix)
    prs, issues, commits = [], [], []
    for local, sink in (("pr-", prs), ("issue-", issues)):
        for nid, data in graphstore.nodes_with_prefix(conn, prefix + local):
            if isinstance(data, dict) and str(data.get("number")) == \
                    nid[len(prefix) + len(local):]:
                sink.append(data)
    for nid, raw in conn.execute(
            "SELECT id, data FROM nodes WHERE id >= ? AND id < ? AND "
            "node_class='code' AND length(id) IN (?, ?) ORDER BY id",
            (lo, hi, len(prefix) + 40, len(prefix) + 64)):
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("sha") == nid[len(prefix):]:
            commits.append(data)
    return prs, issues, commits


def _maintain_record_edges(conn, bundle, project, repo, members, written,
                           prior_files, heal):
    """Delete the record-derived edges of the bundle's records that their new
    content no longer implies, then the person nodes left without a
    contribution edge. Inside the fold's transaction, after its nodes and
    edges are written. `written`: every edge key the fold wrote;
    `prior_files`: the stored file lists of the bundle's commits before the
    fold; `heal` (a repo last folded before #111): apply the rule once to
    every stored record of the repo, from its stored content. Returns
    (retracted keys, removed person ids)."""
    def qid(local):
        return graphstore.qualify_id(project, repo, local)
    prs = bundle.get("prs") or []
    issues = bundle.get("issues") or []
    commits = bundle.get("commits") or []
    touch = [c["sha"] for c in commits if c["sha"] in prior_files
             and list(c.get("files") or []) != prior_files[c["sha"]]]
    stored = stored_record_edges(
        conn, project, repo, members,
        pr_ids=[qid("pr-{}".format(p["number"])) for p in prs],
        issue_ids=[qid("issue-{}".format(i["number"])) for i in issues],
        commit_ids=[qid(c["sha"]) for c in commits],
        touch_ids=[qid(sha) for sha in touch])
    retract = stored - set(written)
    if heal:
        s_prs, s_issues, s_commits = _stored_records(conn, project, repo)
        want = record_edge_keys(s_prs, s_issues, s_commits, project, repo,
                                members)
        retract |= stored_record_edges(
            conn, project, repo, members,
            pr_ids=[qid("pr-{}".format(p["number"])) for p in s_prs],
            issue_ids=[qid("issue-{}".format(i["number"])) for i in s_issues],
            commit_ids=[qid(c["sha"]) for c in s_commits]) - want
    graphstore.delete_edges(conn, retract, commit=False)
    person_prefix = graphstore.qualify_person(project, "")
    removed = delete_orphan_people(
        conn, [k[0] for k in retract if k[0].startswith(person_prefix)])
    return retract, removed


def _repo_has_nodes(conn, project, repo):
    """True when any node id of (project, repo) is stored."""
    lo, hi = graphstore._prefix_range(graphstore.qualify_id(project, repo, ""))
    return conn.execute("SELECT 1 FROM nodes WHERE id >= ? AND id < ? LIMIT 1",
                        (lo, hi)).fetchone() is not None


def _fts_text_of(nid, node_class, data):
    """The text fold_bundle indexes for a STORED node (see its fts_text
    block): a PR / issue social record's title + body + embedded comments, a
    commit's message; "" for anything else."""
    if not isinstance(data, dict):
        return ""
    local = graphstore.parse_id(nid)["local"]
    for kind in ("pr-", "issue-"):
        if (node_class == "social" and local.startswith(kind)
                and str(data.get("number")) == local[len(kind):]):
            comments = list(data.get("comments_list") or [])
            if kind == "pr-":
                comments += data.get("review_comments") or []
            return _social_text(data, comments)
    if node_class == "code" and data.get("sha") == local:
        return data.get("message") or ""
    return ""


def _backfill_fts(conn, refresh=False):
    """Index every stored node the FTS index lacks (issue #111): one pass
    over the social / code nodes not in fts_map, inside the fold's
    transaction. With `refresh` (a fold ran without FTS5 since the index was
    last complete) the already-indexed nodes are re-indexed too: that fold
    may have changed their text. Nodes with no text stay unindexed, as fold
    leaves them."""
    graphstore.index_texts(conn, [
        (nid, text) for nid, text in
        ((nid, _fts_text_of(nid, cls, data))
         for nid, cls, data in graphstore.fts_unindexed_nodes(
             conn, include_indexed=refresh)) if text],
        commit=False)


def _is_sole_window(conn, project, repo, meta):
    """True when the bundle re-folds the ONLY window the store has gathered
    for any repo of `project` (gathered_windows holds exactly this repo's
    (from, to)). False without from/to or with an unreadable ledger."""
    frm, to = meta.get("from"), meta.get("to")
    if not frm or not to:
        return False
    try:
        wins = graphstore.get_windows(conn)
    except ValueError:
        return False
    mine = {(w.get("repo"), w.get("from"), w.get("to")) for w in wins
            if isinstance(w, dict) and w.get("project") == project}
    return mine == {(repo, frm, to)}


def _standin_rows(conn, project, dsts, fetched):
    """Stand-in `structure` node rows for the depends_on targets in `dsts`
    that are not stored (create-if-absent: a real area node is never
    clobbered). See fold_bundle."""
    cand = sorted(set(dsts))
    existing = set()
    for i in range(0, len(cand), 500):
        chunk = cand[i:i + 500]
        existing |= {r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE id IN ({})".format(
                ",".join("?" for _ in chunk)), chunk)}
    rows = []
    for dst in cand:
        if dst in existing:
            continue
        p = graphstore.parse_id(dst)
        scope = p["scope"]
        dst_repo = scope[len(project) + 1:] if scope.startswith(project + "/") else scope
        local = p["local"]
        area_id = local[len("area-"):] if local.startswith("area-") else local
        rows.append((dst, project, dst_repo, "structure", None,
                     {"id": area_id, "synthesized": graphstore.STANDIN_MARK},
                     fetched))
    return rows


def _maintain_current_state(conn, bundle, project, repo, members,
                            registry_by_slug, bicep_prefix_by_slug, fetched,
                            records=None):
    """Write the repo's current-state edges and retract the stale ones, inside
    the fold's transaction, after its nodes and window facts are written.
    `records` is _maintain_record_edges' (retracted keys, removed people) of
    the same fold (issue #111), logged with these in one ledger entry.
    Returns the fold summary {"retracted": {type: n}, "edges": [...],
    "standins_removed": [...], "people_removed": [...], "structure": bool}.
    See the block comment above CURRENT_STATE_EDGE_TYPES and STORE.md
    "Current-state edges"."""
    record_retract, people_removed = records or (set(), [])
    meta = bundle.get("meta", {})
    prefix = graphstore.qualify_id(project, repo, "")
    writes, retract = {}, set()

    # Item relations: rebuilt from the stored records.
    want = item_relation_edges(conn, project, repo,
                               bundle.get("milestones") or (),
                               bundle.get("sprints") or {})
    for etype in _ITEM_RELATION_TYPES:
        stored = graphstore.edges_from_prefix(conn, prefix, etype)
        for key, data in stored.items():
            if key not in want:
                retract.add(key)
        for key, data in want.items():
            if key[2] == etype and (key not in stored or stored[key] != data):
                writes[key] = data

    # depends_on / owns: replayed over every window's facts when each has them.
    windows = repo_fact_windows(conn, project, repo)
    dep_dsts = set()
    if windows is None:
        # A window folded before #94 (or without from/to): add this bundle's
        # edges, as every fold did before #95, and retract nothing.
        for src, dst, data in _fold_depends_on(bundle, project, repo, members,
                                               registry_by_slug,
                                               bicep_prefix_by_slug):
            writes[(src, dst, "depends_on")] = data or None
            dep_dsts.add(dst)
        for person, area in _owns_pairs(bundle.get("code_graph"),
                                        bundle.get("code_owners"),
                                        project, repo):
            writes[(person, area, "owns")] = None
    else:
        replay = StructureReplay(windows, project, repo, members,
                                 registry_by_slug, bicep_prefix_by_slug)
        want = replay.desired()
        stored = {k: v for k, v in graphstore.edges_from_prefix(
                      conn, prefix, "depends_on").items()
                  # a single-repo fold derives no cross-repo edge: leave
                  # the ones a manifest fold wrote alone.
                  if members is not None or k[1].startswith(prefix)}
        person_prefix = graphstore.qualify_person(project, "")
        stored.update({k: v for k, v in graphstore.edges_into_prefix(
                           conn, prefix, "owns").items()
                       if k[0].startswith(person_prefix)})
        for key, data in stored.items():
            if key not in want and replay.retractable(key, data):
                retract.add(key)
        for key, data in want.items():
            if key not in stored or stored[key] != data:
                writes[key] = data
            if key[2] == "depends_on":
                dep_dsts.add(key[1])

    graphstore.delete_edges(conn, retract, commit=False)
    graphstore.upsert_edges(
        conn, [(k[0], k[1], k[2], None, writes[k]) for k in writes],
        commit=False)
    # A depends_on target that is not itself stored gets a stand-in node (see
    # fold_bundle); one no edge references any more after a retraction goes.
    graphstore.upsert_nodes(conn, _standin_rows(conn, project, dep_dsts,
                                                fetched), commit=False)
    removed = graphstore.delete_orphan_standins(
        conn, [k[1] for k in retract if k[2] == "depends_on"], commit=False)

    retract = retract | set(record_retract)
    counts = {}
    for key in sorted(retract):
        counts[key[2]] = counts.get(key[2], 0) + 1
    counts = {k: counts[k] for k in sorted(counts)}
    edges = sorted(list(k) for k in retract)
    people_removed = sorted(people_removed)
    summary = {"retracted": counts, "edges": edges,
               "standins_removed": removed, "people_removed": people_removed,
               "structure": windows is not None}
    if retract or removed or people_removed:
        graphstore.record_retractions(conn, project, repo, {
            "from": meta.get("from"), "to": meta.get("to"),
            "counts": {k: counts[k] for k in sorted(counts)},
            "edges": edges[:graphstore.RETRACTION_LEDGER_EDGES],
            "standins_removed": removed,
            "people_removed": people_removed}, commit=False)
    # `records` (issue #111): every stored record's record-derived edges are
    # what its content implies (this fold's records, and every other record
    # once a fold since #111 healed the repo).
    graphstore.set_current_state(conn, project, repo, {
        "items": True, "structure": windows is not None, "records": True,
        "from": meta.get("from"), "to": meta.get("to")}, commit=False)
    return summary


def describe_retractions(summary, project, repo):
    """One stderr line for a fold that retracted edges, or None."""
    counts = (summary or {}).get("retracted") or {}
    removed = (summary or {}).get("standins_removed") or []
    people = (summary or {}).get("people_removed") or []
    if not counts and not removed and not people:
        return None

    def described(types):
        sub = {k: counts[k] for k in counts if k in types}
        parts = ", ".join("{} {}".format(sub[k], k) for k in sorted(sub))
        return sum(sub.values()), " ({})".format(parts) if parts else ""
    clauses = []
    n, parts = described(CURRENT_STATE_EDGE_TYPES)
    if n:
        clauses.append("retracted {} stale current-state edge(s) of {}/{}{}"
                       .format(n, project, repo, parts))
    n, parts = described(set(counts) - set(CURRENT_STATE_EDGE_TYPES))
    if n:
        clauses.append("retracted {} edge(s) of {}/{} that the re-folded "
                       "records no longer imply{}".format(n, project, repo,
                                                          parts))
    if removed:
        clauses.append("removed {} orphaned depends_on stand-in(s)".format(
            len(removed)))
    if people:
        clauses.append("removed {} person node(s) no contribution references "
                       "any more".format(len(people)))
    return "; ".join(clauses) + " (meta retractions:{}/{})".format(project, repo)


def fold_bundle(conn, bundle, project=None, repo=None, members=None,
                registry_by_slug=None, bicep_prefix_by_slug=None):
    """Fold a raw bundle into the journey-graph store by stable identity:
    upsert nodes, spine edges, and the file-level code-event ledger. Idempotent
    — re-folding an overlapping window mutates nothing already correct. See
    STORE.md for the schema and identity rules.

    Scope: social (prs/issues; comments/reviews stay embedded in the parent's
    data blob), code (commits + the artifact substrate) + the file-level and
    symbol code_events ledgers, structure (milestones/releases/areas + the
    project-scoped people nodes), the spine edges closes/cross_ref/part_of, and
    the non-spine contribution / owns / touches / depends_on / in_milestone /
    in_iteration / blocks / symbol-move edges — all derived on the write path
    via the `derive` leaf. See STORE.md for the full edge/node inventory.

    Current-state edges (blocks, in_milestone, in_iteration, depends_on, owns;
    issue #95) are written last, by _maintain_current_state, equal to what the
    repo's stored inputs imply; the stale ones are retracted in the same
    transaction. Before them, the record-derived edges of the bundle's records
    that their new content no longer implies are deleted, with the person
    nodes left without a contribution edge (_maintain_record_edges, issue
    #111). Returns the summary of both: {"retracted": {type: n}, "edges":
    [[src, dst, type], ...], "standins_removed": [...], "people_removed":
    [...], "structure": bool}.
    """
    meta = bundle.get("meta", {})
    # Identity override (Phase 9): a multi-repo run folds each member under one
    # LOGICAL project (`project`) with `repo` = "owner/repo". Single-repo runs pass
    # neither and fall back to meta.owner/meta.repo — byte-identical to before.
    # `members` (a set/dict of "owner/repo" slugs) gates cross-repo edge emission;
    # None (single-repo) skips it entirely. `registry_by_slug` (Terraform
    # registry) and `bicep_prefix_by_slug` (public Bicep registry path prefix,
    # issue #92) map each member to what it publishes, for _fold_depends_on.
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

    # social: PRs and issues — each record's node, its spine edges, its
    # contribution edges and its review / lifecycle leaves, derived by
    # record_rows (shared with gather.backfill, so a backfilled record is
    # written exactly as a folded one). An issue's `blocks` (issue->issue)
    # relation edges are current-state: written by _maintain_current_state
    # from the stored records, between two STORED issues.
    for pr in bundle.get("prs", []):
        rows = record_rows("pr", pr, project, repo, fetched, members)
        nodes.extend(rows["nodes"])
        edges.extend(rows["edges"])
    for iss in bundle.get("issues", []):
        rows = record_rows("issue", iss, project, repo, fetched, members)
        nodes.extend(rows["nodes"])
        edges.extend(rows["edges"])

    # code: commits, with part_of spine edge to the PR named in the subject.
    for c in bundle.get("commits", []):
        cid = qid(c["sha"])
        nodes.append((cid, project, repo, "code", c.get("date"), c, fetched))
        edges.extend(_commit_part_of(c, qid))

    # code: file-level code-event ledger (rename/copy source -> detail).
    for ev in bundle.get("code_events", []):
        path = ev.get("path")
        if not path:
            continue
        events.append((
            qid(path), ev.get("change"), ev.get("commit"), ev.get("author"),
            ev.get("date"), ev.get("hunk"), None, None, None, ev.get("old_path"),
        ))

    # code: artifact substrate (file `art:<path>` and symbol/comment
    # `<path>#<lang>:<subkind>:<name>` nodes) is CUMULATIVE (#76): rebuilt from
    # the repo's whole stored ledger once this bundle's rows are in it — see
    # _fold_artifacts, called in the transaction below.

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
    # Matched within THIS window only; _fold_artifacts applies the recorded
    # moves of every window onto the nodes (#76).
    for m in derive.match_symbol_moves(
            bundle.get("symbol_events", []),
            [(e.get("old_path"), e["path"]) for e in bundle.get("code_events", [])
             if e.get("change") in ("rename", "copy") and e.get("old_path")]):
        lang = m["lang"] or ""
        src = qid("{}#{}:{}:{}".format(m["from_path"], lang, m["subkind"], m["name"]))
        dst = qid("{}#{}:{}:{}".format(m["to_path"], lang, m["subkind"], m["name"]))
        data = {"move_confidence": m["confidence"], "move_basis": m["basis"]}
        if m.get("commit"):
            # the source's drop commit: lets the cumulative projection tell
            # WHICH removal of the source this move explains.
            data["move_commit"] = m["commit"]
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

    # People `structure` nodes (project-scoped), unioned with what an earlier
    # member already stored: see _person_rows.
    # A re-fold of the project's ONLY gathered window (issue #111): this bundle
    # is then everything the store knows about its people, so its derivation
    # REPLACES the stored one instead of being unioned with it — a commit
    # re-attributed by a later gather of the window takes its areas along.
    nodes.extend(_person_rows(conn, project, people, fetched,
                              replace=_is_sole_window(conn, project, repo, meta)))

    # Contribution edges of a commit's author (person -> commit); a PR's and an
    # issue's contribution edges came with their record_rows above.
    for c in bundle.get("commits", []):
        if c.get("author"):
            edges.append((graphstore.qualify_person(project, c["author"]),
                          qid(c["sha"]), "authored", None, None))

    # owns (person -> area): CODEOWNERS semantics via derive.area_owners — an
    # area is owned by the owners of the LAST rule matching the area directory
    # (graphify community areas: union over their files). Same matcher as
    # derive.annotate_people_profile's stale_owned. Current-state: written by
    # _maintain_current_state (_owns_pairs over the windows' facts).

    # touches (commit/pr -> area): the distinct areas a commit's files land in.
    # History: only added (they also depend on the code graph), except that a
    # commit whose stored file list changed gets this fold's touches (#111,
    # _maintain_record_edges).
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
    # folds (see _fold_depends_on, threaded with members + registry_by_slug and,
    # for Bicep public-registry refs, bicep_prefix_by_slug). Current-state:
    # written by _maintain_current_state (StructureReplay over the windows'
    # facts), together with a stand-in node per target that is not stored.
    #
    # Stand-ins: a depends_on dst is always a real module-area path resolved from
    # a `module` source reference — an intra-repo SUB-MODULE, or a cross-repo
    # producer's root. The directory area-provider is WINDOW-SCOPED, so a
    # referenced module that was not itself changed in-window has no area node,
    # leaving the edge dangling (validate.referential_integrity). This is a
    # real-data fact, not a bug — bicep repos like avm/res/*/* reference deep
    # sub-modules every gather; a single-repo bicep window over
    # Azure/bicep-registry-modules produced ~30 such edges. EACH depends_on target
    # (intra- and cross-repo) is ensured as a minimal `structure` area node
    # (`synthesized: STANDIN_MARK`) so the dependency graph stays referentially
    # intact. Create-if-ABSENT only — never clobber a real area node, in any fold
    # order; a later real fold of that area upserts over the stand-in. extract
    # rebuilds code_graph from the window facts / `codegraph` singleton (not
    # area-* nodes), so the stand-in is invisible to every member bundle —
    # purely an edge anchor. A stand-in no edge references after a retraction
    # is deleted (issue #95).

    # in_milestone (social -> structure): a PR/issue's milestone title links to
    # the milestone node (keyed on number when present, else title — matching
    # the milestone node's own local id form above). in_iteration (social ->
    # structure): a PR/issue's board iteration links to its `sprint-<id>` node —
    # the sprint sibling of in_milestone; only to a sprint that is stored (a
    # windowed-out iteration leaves no dangling edge). The item's `board_status`
    # rides the pr/issue node data blob (stamped in acquire), so it round-trips
    # through extract for free — no separate edge/node. Both are current-state:
    # written by _maintain_current_state from the stored records.

    # structure: per-repo singleton facts (whole dict round-tripped under a
    # well-known local id, NULL ts so window scans skip it, identity-keyed /
    # idempotent). Only emitted when present and non-empty so extract never
    # fabricates an empty key. See STORE.md and extract._materialize_singleton.
    # Latest-wins: a view reads them only on a store folded before issue #94;
    # the per-window copies are written with the window provenance below.
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
    # Record-derived edges (issue #111): read what the fold needs to know about
    # the records it replaces before it writes them. A repo whose last fold
    # predates #111 (no `records` in its current_state marker) but already
    # holds data is healed once, from its stored records.
    state = graphstore.get_current_state(conn, project, repo) or {}
    had_nodes = _repo_has_nodes(conn, project, repo)
    heal = not state.get("records") and had_nodes
    fold_context = _next_fold_context(conn, project, repo, members,
                                      registry_by_slug, bicep_prefix_by_slug,
                                      had_nodes)
    with conn:
        # FTS coverage (issue #111): a store folded on a host without FTS5 has
        # records the index lacks; the first fold with FTS5 since then indexes
        # every stored node the index lacks, before this fold writes anything
        # (so a fold into an empty store indexes exactly as before).
        coverage = graphstore.get_meta(conn, graphstore.FTS_COVERAGE_KEY)
        if graphstore.fts5_available(conn) and coverage != graphstore.FTS_COMPLETE:
            _backfill_fts(conn, refresh=coverage == graphstore.FTS_PARTIAL)
        prior_files = _stored_commit_files(
            conn, project, repo, [c["sha"] for c in bundle.get("commits", [])])
        graphstore.upsert_nodes(conn, nodes, commit=False)
        graphstore.upsert_edges(conn, edges, commit=False)
        records = _maintain_record_edges(
            conn, bundle, project, repo, members, {e[:3] for e in edges},
            prior_files, heal)
        graphstore.add_code_events(conn, events, commit=False)
        graphstore.upsert_nodes(
            conn, _fold_artifacts(conn, bundle, project, repo, fetched),
            commit=False)

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
        # Coverage (issue #111): the index now holds every stored record (the
        # back-fill above ran first when it did not); a fold without FTS5
        # marks it partial.
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
            graphstore.set_meta(conn, graphstore.FTS_COVERAGE_KEY,
                                graphstore.FTS_COMPLETE, commit=False)
        else:
            graphstore.set_meta(conn, graphstore.FTS_COVERAGE_KEY,
                                graphstore.FTS_PARTIAL, commit=False)

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
        # The window's mainline (issue #84): acquire() always sets
        # meta.base_branch (None when it could not be determined), so a re-fold
        # replaces it. A bundle without the key (hand-built, pre-#84) stores
        # nothing and its view reports the base branch as unknown.
        if "base_branch" in meta:
            graphstore.set_branches(conn, project, repo, meta.get("from"),
                                    meta.get("to"), meta.get("base_branch"),
                                    meta.get("branches"), commit=False)
        if meta.get("clone_sha"):
            graphstore.set_clone_sha(conn, project, repo, meta["clone_sha"],
                                     commit=False)
        # The window's clone provenance (issue #107): how the clone was made
        # and the in-window commits whose diffs the shallow boundary dropped,
        # so a view can say its code history is incomplete. A bundle without
        # either key (hand-built, pre-#107) stores nothing.
        if meta.get("from") and meta.get("to") and (
                "clone_mode" in meta or "boundary_dropped_commits" in meta
                or "structure_as_of" in meta):
            graphstore.set_clone_info(conn, project, repo, meta["from"],
                                      meta["to"], meta.get("clone_mode"),
                                      meta.get("boundary_dropped_commits"),
                                      structure_as_of=meta.get("structure_as_of"),
                                      commit=False)
        # The window's own CI statistics and structure (issue #94): the
        # latest-wins nodes above serve stores folded before #94; a view of
        # this window reads these (extract._window_facts).
        if meta.get("from") and meta.get("to"):
            graphstore.set_window_facts(conn, project, repo, meta["from"],
                                        meta["to"], bundle,
                                        clone_sha=meta.get("clone_sha"),
                                        commit=False)
        # Current-state edges (issue #95), last: they read the records and
        # window facts written above, and retract what those no longer imply.
        summary = _maintain_current_state(conn, bundle, project, repo, members,
                                          registry_by_slug,
                                          bicep_prefix_by_slug, fetched,
                                          records=records)
        # The member set and registries this fold derived cross-repo edges
        # with (issue #113), so validate can resolve them exactly.
        if fold_context is not None:
            graphstore.set_fold_context(conn, project, repo, fold_context,
                                        commit=False)
    return summary


def _next_fold_context(conn, project, repo, members, registry_by_slug,
                       bicep_prefix_by_slug, had_nodes):
    """The fold context (graphstore.set_fold_context) this fold records, or
    None to leave the stored one alone. A manifest fold (`members` given)
    records its member set and registries; `members_changed` turns (and
    stays) true when the recorded member set differs, or when the repo held
    records before any fold recorded one. A single-repo fold of a repo that
    has a context marks it changed: its records' cross-repo edges were not
    derived with that member set."""
    prior = graphstore.get_fold_context(conn, project, repo)
    if members is None:
        if prior is None or prior.get("members_changed") is True:
            return None
        return dict(prior, members_changed=True)
    slugs = sorted(members)
    if prior is None:
        # records folded before (a stand-in another member's fold made for
        # this repo is not one)
        changed = bool(had_nodes) and not graphstore._only_standins(
            conn, project, repo)
    else:
        changed = (prior.get("members_changed") is not False
                   or prior.get("members") != slugs)

    def as_map(m):
        return None if m is None else {k: m[k] for k in sorted(m)}
    return {"members": slugs, "registry": as_map(registry_by_slug),
            "bicep_registry_prefix": as_map(bicep_prefix_by_slug),
            "members_changed": changed}


def _member_clone_dir(base_dir, owner, repo, run=None):
    """A manifest member's clone dir: `{base}/{owner}/{repo}` (issue #109). A
    GitHub owner or repo name never contains `/`, so two members can never
    share a dir -- the former `{base}/{owner}-{repo}-clone` did for `a-b`/`c`
    and `a`/`b-c`. A dir in that former layout that is a clone of THIS member's
    remote is still used (no re-clone for existing users) as long as the new
    dir does not exist yet."""
    new = "{}/{}/{}".format(base_dir, owner, repo)
    if os.path.exists(new):
        return new
    old = "{}/{}-{}-clone".format(base_dir, owner, repo)
    if os.path.isdir(os.path.join(old, ".git")):
        try:
            have = (run or run_git)(["git", "-C", old, "config", "--get",
                                     "remote.origin.url"]).strip()
        except RuntimeError:
            have = ""
        if have and _normalize_remote(have) == _normalize_remote(
                _github_remote_url(owner, repo)):
            return old
    return new


def _member_args(base, member, frm, to):
    """Clone the CLI args for one manifest member: same flags, but owner/repo and
    the window come from the manifest, and clone_dir is re-derived PER MEMBER as
    `{base}/{owner}/{repo}` (_member_clone_dir; an existing clone in the former
    `{base}/{owner}-{repo}-clone` layout is reused). Including the owner (not
    just the repo, which is acquire's single-repo default) keeps two members
    that share a repo name under different owners from colliding on the same
    checkout. A `--clone-dir` on the base args is treated as the parent
    directory; otherwise it defaults to `workspace`."""
    base_dir = (base.clone_dir or "workspace").rstrip("/")
    clone_dir = _member_clone_dir(base_dir, member["owner"], member["repo"])
    fields = {**vars(base), "owner": member["owner"], "repo": member["repo"],
              "clone_dir": clone_dir}
    # A member's own `branch` (issue #84) beats a global --branches; with
    # neither, acquire resolves each member's default branch on its own.
    if member.get("branch"):
        fields["branches"] = member["branch"]
    ns = argparse.Namespace(**fields)
    setattr(ns, "from", frm)   # 'from' is a Python keyword: set via attribute
    ns.to = to
    ns.structural_iac = True   # manifest members get the whole-tree module scan
    return ns


def _failure_reason(exc):
    """One-line, deterministic reason for a member that failed with `exc`. The
    HTTP layer and acquire() stop with SystemExit(<message>); anything else is
    `<ExceptionType>: <message>`. Capped at 500 characters."""
    if isinstance(exc, SystemExit):
        text = exc.code if isinstance(exc.code, str) else \
            "exit status {}".format(exc.code)
    else:
        text = "{}: {}".format(type(exc).__name__, exc) if str(exc) \
            else type(exc).__name__
    return " ".join(str(text).split())[:500]


def _gather_manifest(conn, args, man):
    """Gather + fold every manifest member, one at a time (issue #77). A member
    that fails (any Exception, or the SystemExit the HTTP layer raises) is
    recorded and the run goes on with the next member; its fold, if it got that
    far, rolled back as one transaction. Every member to attempt is first
    recorded `not_attempted` for the window, then `ok` or `failed` (with the
    reason) when its turn ends, so an interrupted run is visible in the store
    too. With `--resume`, members already `ok` for this window are skipped.
    Returns [(slug, reason)] for the members that failed, in manifest order."""
    project, frm, to = man["project"], man["from"], man["to"]
    members = manifest_mod.member_slugs(man)
    registry_by_slug = {
        "{}/{}".format(m["owner"], m["repo"]): m.get("registry")
        for m in man["repos"]}
    bicep_prefix_by_slug = {
        "{}/{}".format(m["owner"], m["repo"]): m.get("bicep_registry_prefix")
        for m in man["repos"]}
    slugs = ["{}/{}".format(m["owner"], m["repo"]) for m in man["repos"]]
    done = set()
    if args.resume:
        done = {e["repo"] for e in graphstore.get_member_status(conn, project)
                if e["from"] == frm and e["to"] == to
                and e["status"] == graphstore.MEMBER_OK}
    todo = [s for s in slugs if s not in done]
    # One affiliation answer cache for the whole run: a person active in
    # several members is looked up once.
    affiliation_cache = {}
    if not todo:
        sys.stderr.write("nothing to resume: every member of project '{}' is "
                         "ok for {}..{}\n".format(project, frm, to))
        return []
    with conn:
        graphstore.set_member_status(conn, project, todo, frm, to,
                                     graphstore.MEMBER_NOT_ATTEMPTED,
                                     commit=False)
    failures = []
    for m, slug in zip(man["repos"], slugs):
        if slug not in todo:
            continue
        try:
            member_args = _member_args(args, m, frm, to)
            member_args.affiliation_manifest = man.get("affiliation")
            member_args.affiliation_cache = affiliation_cache
            bundle = acquire(member_args, os.environ)
            summary = fold_bundle(conn, bundle, project=project, repo=slug,
                                  members=members,
                                  registry_by_slug=registry_by_slug,
                                  bicep_prefix_by_slug=bicep_prefix_by_slug)
            line = describe_retractions(summary, project, slug)
            if line:
                sys.stderr.write(line + "\n")
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - per member
            reason = _failure_reason(exc)
            failures.append((slug, reason))
            sys.stderr.write("member {} failed: {}\n".format(slug, reason))
            status, error = graphstore.MEMBER_FAILED, reason
        else:
            status, error = graphstore.MEMBER_OK, None
        with conn:
            graphstore.set_member_status(conn, project, [slug], frm, to,
                                         status, error, commit=False)
    sys.stderr.write(
        "folded {} of {} member repo(s) of project '{}' into store {}\n".format(
            len(todo) - len(failures), len(todo), project, args.store))
    return failures


def main(argv=None):
    """Exit: returns the store path when every repo folded. A manifest run in
    which any member failed still folds the others, then prints a summary
    (member -> reason) and exits 1 (issue #77)."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # A non-integer ACTIVITY_* knob fails here, before any clone or API call.
    validate_env_knobs()
    # Load + validate the manifest BEFORE touching the store, so a bad/missing
    # manifest fails fast instead of leaving an empty DB behind.
    man = manifest_mod.load_manifest(args.manifest) if getattr(
        args, "manifest", None) else None
    # The retained people registry: loaded (and checked) before the store is
    # touched; --people beats the manifest's `people`.
    if man is not None and man.get("people") and not getattr(args, "people", None):
        args.people_manifest = man["people"]
    if getattr(args, "affiliation_enabled", True):
        if people_run_from_args(args) is None and getattr(args, "people_offline", False):
            raise SystemExit("error: --people-offline needs a people registry "
                             "(--people PATH or the manifest's `people`)")
    os.makedirs(os.path.dirname(args.store) or ".", exist_ok=True)
    conn = graphstore.open_store(args.store)
    failures = []
    try:
        graphstore.init_schema(conn)
        if man is not None:
            failures = _gather_manifest(conn, args, man)
        else:
            # Store-only single-repo path (Phase 7): acquire -> fold. A failure
            # propagates as before: there is no other member to go on with.
            bundle = acquire(args, os.environ)
            summary = fold_bundle(conn, bundle)
            line = describe_retractions(summary, bundle["meta"].get("owner"),
                                        bundle["meta"].get("repo"))
            if line:
                sys.stderr.write(line + "\n")
            sys.stderr.write("folded bundle into store {}\n".format(args.store))
    finally:
        conn.close()
    if failures:
        lines = ["gather: {} of {} member(s) failed for project '{}' "
                 "({}..{}):".format(len(failures), len(man["repos"]),
                                    man["project"], man["from"], man["to"])]
        lines += ["  {}: {}".format(slug, reason) for slug, reason in failures]
        lines.append("the other members are folded; re-run with --resume to "
                     "gather only the failed member(s)")
        sys.stderr.write("\n".join(lines) + "\n")
        raise SystemExit(1)
    return args.store


if __name__ == "__main__":
    main()
