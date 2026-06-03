"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

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
        "flow": {},
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
    newline-separated file paths. Returns a list of commit dicts.
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
        files = [ln for ln in lines[1:] if ln.strip()]
        commits.append({
            "sha": sha,
            "parents": parents.split() if parents.strip() else [],
            "author": author,
            "date": date,
            "message": subject,
            "files": files,
            "pr": None,  # resolved in link.py
        })
    return commits


# git log format for the full-window code-event walk (Phase 3a). Same RECORD_SEP/
# FIELD_SEP header as parse_git_log, but the BODY is `--name-status` lines so each
# changed path carries its change type (and rename/copy detection via -M -C gives
# `R###`/`C###` with old+new paths).
CODE_LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ad%x1f%s"

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
            if change in ("rename", "copy"):
                if len(cols) < 3:
                    continue  # malformed R/C line (real `-M -C` always has old+new)
                ev["old_path"] = cols[1].strip()
                ev["path"] = cols[2].strip()
            else:
                ev["path"] = cols[1].strip()
            events.append(ev)
    return events


# Fetch a margin of history BEFORE the window so the shallow-clone boundary commit
# predates it. The boundary commit's parent is grafted away, so `git log -p` /
# `--name-status` diff it against the EMPTY tree — a whole-tree phantom that would
# otherwise flood code_events/symbol_events with thousands of spurious "add"s. With
# the margin the first in-window commit keeps its real parent; shallow_boundary_shas
# is the belt-and-suspenders that drops any boundary commit that still lands in-window.
CLONE_MARGIN_DAYS = 14


def _shift_date(date_str, days):
    """YYYY-MM-DD shifted by `days` (negative = earlier); input returned on parse error."""
    try:
        d = datetime.date.fromisoformat(date_str[:10])
        return (d + datetime.timedelta(days=days)).isoformat()
    except (ValueError, TypeError):
        return date_str


def build_clone_cmd(repo_url, from_date, clone_dir):
    """Construct the bounded, partial clone command (network-free to build).

    `--shallow-since` reaches CLONE_MARGIN_DAYS before `from_date` so the shallow
    boundary commit (whole-tree phantom diff) sits OUTSIDE the report window."""
    return [
        "git", "clone",
        "--filter=blob:none",
        f"--shallow-since={_shift_date(from_date, -CLONE_MARGIN_DAYS)}",
        "--no-single-branch",
        repo_url, clone_dir,
    ]


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


def in_window(ts, from_date, to_date):
    """True if ISO date/datetime string `ts` falls within [from_date, to_date]
    inclusive, comparing on the date prefix. None/empty is never in window."""
    if not ts:
        return False
    day = ts[:10]
    return from_date <= day <= to_date


_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)


def parse_closing_refs(text):
    """Extract issue numbers from GitHub closing keywords, de-duplicated,
    order-preserving."""
    out = []
    for m in _CLOSING_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
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
        "comments": raw.get("comments", 0) or 0,
        "review_comments_count": raw.get("review_comments", 0) or 0,
        "reviewers": [],
        "review_decision": "none",
        "crossref_issues": [],
        "closes": parse_closing_refs(
            (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")
        ),
        "url": raw.get("html_url"),
    }


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
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "comments": raw.get("comments", 0) or 0,
        "url": raw.get("html_url"),
    }


# Review states ranked by how strongly they gate a merge. A PR's decision is
# the strongest *latest-per-reviewer* signal: any outstanding changes-requested
# dominates; otherwise an approval; otherwise a bare comment.
_REVIEW_RANK = {"changes_requested": 3, "approved": 2, "commented": 1, "none": 0}


def summarize_reviews(raw_reviews):
    """Reduce raw PR reviews to {reviewers, decision}. Pure."""
    latest = {}  # login -> (submitted_at, state)
    for r in raw_reviews or []:
        login = (r.get("user") or {}).get("login")
        if not login:
            continue
        state = (r.get("state") or "").lower()
        if state not in _REVIEW_RANK:
            continue
        ts = r.get("submitted_at") or ""
        # ISO-8601 (UTC `Z`) timestamps sort correctly as plain strings.
        if login not in latest or ts >= latest[login][0]:
            latest[login] = (ts, state)
    reviewers = sorted(latest)
    decision = "none"
    for _, state in latest.values():
        if _REVIEW_RANK[state] > _REVIEW_RANK[decision]:
            decision = state
    return {"reviewers": reviewers, "decision": decision}


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


def classify_artifact_path(path):
    """Classify a changed file path into a tracked artifact kind, or None.

    File granularity only (Phase 3a). Precedence: readme > example > doc.
      - readme : basename matches README* (any/no extension)
      - example: under an `examples/` directory, or a `*.example*` filename
      - doc    : a `*.md` file, or any file under a `docs/` directory
      - else   : None (ignored at file granularity)
    `symbol`/`comment` artifacts come from the Phase 3d hunk walk (parse_symbol_events),
    not this path classifier."""
    if not path:
        return None
    parts = path.split("/")
    base = parts[-1]
    low = base.lower()
    if base.upper().startswith("README"):
        return "readme"
    # `.example` only when it is a dot-segment (config.example.json, foo.example),
    # not an incidental substring (counter-example.md must stay a doc).
    if "examples" in parts[:-1] or ".example." in low or low.endswith(".example"):
        return "example"
    if low.endswith(".md") or "docs" in parts[:-1]:
        return "doc"
    return None


# ---- Phase 3d: symbol-granular change attribution (diff-local, build-free) ----
# A single `git log -p` walk yields per-(commit,path) hunks; we attribute changes to
# symbol DECLARATIONS recognised within each hunk's lines (no per-commit `git show`,
# no build). Heuristic but pure and offline-testable. Identity across renames is 3e.

_BICEP_DECL_RE = re.compile(r"^\s*(param|var|output|resource|module)\s+([A-Za-z_]\w*)")
_BICEP_COMMENT_RE = re.compile(r"^\s*(//|/\*|\*/|\*|@sys\.description|@description)")
_TF_RES_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"')
_TF_BLOCK_RE = re.compile(r'^\s*(variable|output|module)\s+"([^"]+)"')
_TF_COMMENT_RE = re.compile(r"^\s*(#|//)")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
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


def parse_unified_diff(patch):
    """Parse a (multi-file) unified diff into per-file hunks. Pure.

    -> [{"path","old_path","hunks":[{"new_start":int,"lines":[(sign,text), ...]}]}]
    sign in {' ','+','-'}; `/dev/null` paths normalise to None (pure add/delete)."""
    files, cur, hunk = [], None, None
    for line in (patch or "").split("\n"):
        if line.startswith("diff --git "):
            cur = {"path": None, "old_path": None, "hunks": []}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("--- "):
            p = line[4:].strip()
            cur["old_path"] = None if p == "/dev/null" else (p[2:] if p[:2] in ("a/", "b/") else p)
        elif line.startswith("+++ "):
            p = line[4:].strip()
            cur["path"] = None if p == "/dev/null" else (p[2:] if p[:2] in ("a/", "b/") else p)
        else:
            m = _HUNK_RE.match(line)
            if m:
                hunk = {"new_start": int(m.group(1)), "lines": []}
                cur["hunks"].append(hunk)
            elif hunk is not None and line[:1] in (" ", "+", "-"):
                hunk["lines"].append((line[0], line[1:]))
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


def parse_symbol_events(raw):
    """Parse `git log -p` output into symbol-level change events. Pure.

    Each event: {commit, author, date, path, lang, subkind, name, change, before, after}.
    Only tracked source files (symbol_lang) contribute; file-level lifecycle still comes
    from parse_code_events. Merge commits show no patch and yield nothing."""
    events = []
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
            path = f["path"] or f["old_path"]
            for d in build_symbol_deltas(path, f["hunks"]):
                events.append({
                    "commit": sha, "author": author, "date": date, "path": d["path"],
                    "lang": d["lang"], "subkind": d["subkind"], "name": d["name"],
                    "change": d["change"], "before": d["before"], "after": d["after"],
                })
    return events


# Ordered code-area patterns for the directory provider (primary, zero-dep).
# Each entry is (name, predicate(parts) -> area_id_or_None) tried in order; the
# first match wins. `parts` is path.split("/"). Patterns are directory-first and
# match how IaC repos define a module. The generic fallback is last.
def _avm_area(parts):
    # avm/res/<service>/<module>/...  -> the 4-segment module subtree.
    if len(parts) >= 4 and parts[0] == "avm" and parts[1] in ("res", "ptn", "utl"):
        return "/".join(parts[:4])
    return None


def _main_bicep_dir(parts):
    # any directory containing a main.bicep -> that directory.
    if parts and parts[-1] == "main.bicep":
        return "/".join(parts[:-1]) or parts[0]
    return None


def _terraform_modules_dir(parts):
    # modules/<name>/... -> modules/<name>.
    if len(parts) >= 2 and parts[0] == "modules":
        return "/".join(parts[:2])
    return None


def _tf_dir(parts):
    # any directory containing a *.tf file -> that directory.
    if parts and parts[-1].endswith(".tf"):
        return "/".join(parts[:-1]) or parts[0]
    return None


def _topn_dir(parts, n=2):
    # generic fallback: the first N path segments (or the file itself if shallower).
    if not parts:
        return None
    return "/".join(parts[:n])


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
    modules/<name>, any *.tf dir, else a top-2-segment fallback). Pure."""
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


def parse_bicep_module_refs(source_text):
    """Extract `module <sym> '<ref>'` references from a .bicep source.

    Each result is {ref, registry_path, version, local_path, instances}. Registry
    refs (`br/public:<path>:<ver>` or `br:<host>/bicep/<path>:<ver>`) split into
    `registry_path` + `version`; everything else is a `local_path`. `instances` is
    `"many"` when the module is instantiated as an array (`= [for ...]`), `"one"`
    for a single `= {`, else None. Pure."""
    refs = []
    for m in _BICEP_MODULE_RE.finditer(source_text or ""):
        ref = m.group("ref")
        open_ch = m.group("open")
        instances = "many" if open_ch == "[" else "one" if open_ch == "{" else None
        registry_path = version = local_path = None
        if ref.startswith("br/public:") or ref.startswith("br:"):
            body = ref.split(":", 1)[1]              # drop the br/public or br scheme
            if ":" in body:
                path_part, version = body.rsplit(":", 1)
            else:
                path_part = body
            if "/bicep/" in path_part:               # strip an explicit registry host
                path_part = path_part.split("/bicep/", 1)[1]
            registry_path = path_part
        else:
            local_path = ref
        refs.append({"ref": ref, "registry_path": registry_path,
                     "version": version, "local_path": local_path,
                     "instances": instances})
    return refs


def resolve_module_ref(ref_info, base_path, patterns=None):
    """Map a parsed bicep/tf ref to a canonical area-id (reusing classify_code_area).

    Registry refs classify their `<path>/main.bicep`; local refs are normalised
    relative to `base_path`'s directory then classified. Returns the area-id, or
    the raw path when classification declines, or None when nothing is set. Pure."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    rp = ref_info.get("registry_path")
    if rp:
        probe = rp.rstrip("/") + "/main.bicep"
        return classify_code_area(probe, patterns) or rp
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


def build_bicep_edges(source_text, arm_json, base_path, area_ids, patterns=None):
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
        to = resolve_module_ref(ri, base_path, patterns)
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
    for node in walk_arm_deployments(arm_json):
        # depth 1 == the direct module instantiations (already emitted as immediate
        # source refs); only genuinely-nested deployments are transitive.
        if node["depth"] < 2:
            continue
        to = _match_repo_area(node.get("metadata_name"), area_ids)
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


_TF_MODULE_HEAD_RE = re.compile(r'module\s+"(?P<name>[^"]+)"\s*\{')
_TF_SOURCE_RE = re.compile(r'source\s*=\s*"(?P<src>[^"]+)"')
_TF_VERSION_RE = re.compile(r'version\s*=\s*"(?P<ver>[^"]+)"')
_TF_EDGE_RE = re.compile(r'"\[root\]\s*(?P<a>[^"]+)"\s*->\s*"\[root\]\s*(?P<b>[^"]+)"')
_TF_MODULE_TOKEN_RE = re.compile(r"module\.([A-Za-z0-9_-]+)")


def _terraform_module_bodies(tf_text):
    """Map each module name to its raw block body via brace-balancing.

    Non-greedy regexes break on nested blocks (`tags { ... }`, `dynamic`), so the
    body is scanned from the opening `{` to its matching close, counting depth.
    Internal helper. Pure."""
    text = tf_text or ""
    out = {}
    for m in _TF_MODULE_HEAD_RE.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += (text[i] == "{") - (text[i] == "}")
            i += 1
        out[m.group("name")] = text[m.end():i - 1]
    return out


def parse_terraform_module_blocks(tf_text):
    """Map each `module "<name>" { source = "..." }` to its source string. Pure.

    Brace-balanced so a `source` after a nested block is still found."""
    out = {}
    for name, body in _terraform_module_bodies(tf_text).items():
        sm = _TF_SOURCE_RE.search(body)
        if sm:
            out[name] = sm.group("src")
    return out


def _first_tf_module(node):
    m = _TF_MODULE_TOKEN_RE.search(node or "")
    return m.group(1) if m else None


def parse_terraform_graph(dot_text):
    """Extract (from_module|None, to_module) dependency pairs from terraform graph DOT.

    A node's first `module.<name>` token is its owning module (None = root). An edge
    A->B becomes (module(A), module(B)) when B is in a module and differs from A.
    Deterministic (sorted, deduped). Pure."""
    pairs = set()
    for m in _TF_EDGE_RE.finditer(dot_text or ""):
        a, b = _first_tf_module(m.group("a")), _first_tf_module(m.group("b"))
        if b and a != b:
            pairs.add((a, b))
    return sorted(pairs, key=lambda p: (p[0] or "", p[1]))


def build_terraform_edges(tf_text, dot_text, base_path, area_ids, patterns=None):
    """Build inter-area dependency edges for one Terraform area.

    Joins terraform-graph module pairs with the `module {source=}` blocks. A LOCAL
    source resolves to a repo area-id (via classify_code_area, relative to base_path)
    → `to`=area, `resolved`=True. A REGISTRY/external source cannot map to a repo area,
    so — like the Bicep path and the schema (`to`: area-id|null) — `to`=None,
    `resolved`=False, with the source kept in `ref` (+ pinned `version`). The DOT graph
    is transitive: a module the root depends on directly is `transitive`=False; one
    reached ONLY through another module is `transitive`=True (and direct wins if both).
    Pure; deterministic."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    bodies = _terraform_module_bodies(tf_text)
    sources = parse_terraform_module_blocks(tf_text)
    base_dir = os.path.dirname(base_path or "")

    def version_of(name):
        vm = _TF_VERSION_RE.search(bodies.get(name, ""))
        return vm.group("ver") if vm else None

    def resolve(name):
        """-> (to_area_or_None, ref_or_None, version). ref None = unknown module."""
        src = sources.get(name)
        if not src:
            return None, None, None
        if src.startswith(".") or src.startswith("/"):
            joined = os.path.normpath(os.path.join(base_dir, src))
            return (classify_code_area(joined + "/main.tf", patterns) or joined), src, None
        return None, src, version_of(name)        # external: to=None, identity in ref

    pairs = parse_terraform_graph(dot_text)
    direct = {b for a, b in pairs if a is None}    # modules the root depends on
    edges = []
    seen = set()
    for _a, b in pairs:
        to, ref, version = resolve(b)
        if ref is None:                            # module block with no source
            continue
        key = (to, ref)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"to": to, "kind": "module", "ref": ref, "version": version,
                      "transitive": b not in direct, "provider": "terraform",
                      "resolved": to is not None})
    return sorted(edges, key=lambda e: (str(e["to"]), str(e["ref"])))


def _read_text_file(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _bicep_entrypoint(area):
    for p in area.get("paths", []):
        if p == "main.bicep" or p.endswith("/main.bicep"):
            return p
    return None


def _tf_entrypoint(area):
    tfs = [p for p in area.get("paths", []) if p.endswith(".tf")]
    for p in tfs:
        if p == "main.tf" or p.endswith("/main.tf"):
            return p
    return tfs[0] if tfs else None


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


def parse_codeowners(text):
    """Parse a CODEOWNERS file into {pattern: [login, ...]}.

    Each non-comment line is `<pattern> <owner...>`; `@user` / `@org/team` are
    stripped of the leading `@`. Lines with a pattern but no owners are skipped.
    Order-preserving owners, last-pattern-wins on duplicate patterns. Pure."""
    owners = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, handles = parts[0], parts[1:]
        logins = [h[1:] if h.startswith("@") else h for h in handles]
        if not logins:
            continue
        owners[pattern] = logins
    return owners


# Conventional label namespace -> facet (auto-detect). AVM uses `Class:`/`Type:`/
# `Needs:`; most repos use `area`/`priority`/`status`/`lifecycle` with `:` or `/`.
_AUTO_FACET_NAMESPACES = {
    "area": "area", "component": "area",
    "priority": "priority", "p": "priority",
    "status": "status", "needs": "status",
    "lifecycle": "lifecycle",
    "type": "kind", "kind": "kind", "class": "kind",
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

# Native issue-type / label-value tokens -> canonical kind.
_KIND_TOKENS = {
    "feature": "feature", "enhancement": "feature",
    "module": "module-request", "module-request": "module-request",
    "module request": "module-request",
    "bug": "bug", "defect": "bug",
    "idea": "idea", "proposal": "idea",
    "question": "question", "support": "question",
    "doc": "docs", "docs": "docs", "documentation": "docs",
}
_VALID_KINDS = {"feature", "module-request", "bug", "idea", "question", "docs", "other"}


def _kind_from_token(text):
    """Map a free token (issue-type name, label value, template stem) to a kind."""
    low = (text or "").strip().lower()
    for token, kind in _KIND_TOKENS.items():
        if token in low:
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

    Priority: native GitHub issue type (when present) -> label `kind` facet ->
    issue-template filename -> title/body heuristic -> other. Pure."""
    # 1. native issue type
    if types_present:
        kind = _kind_from_token(issue.get("issue_type"))
        if kind:
            return kind
    # 2. label kind facet (the values carried under the taxonomy's `kind` facet)
    for lbl in _labels_in_taxonomy(issue, taxonomy, "kind"):
        value = lbl.split(":", 1)[-1] if ":" in lbl else lbl.split("/", 1)[-1]
        kind = _kind_from_token(value)
        if kind:
            return kind
    # 3. issue-template filename (e.g. module_request.md, bug_report.yml)
    kind = _kind_from_token((issue.get("template") or "").replace("_", " "))
    if kind:
        return kind
    # 4. title/body heuristic
    text = f"{issue.get('title','')} {issue.get('body','')}"
    if "?" in (issue.get("title") or ""):
        return "question"
    kind = _kind_from_token(text)
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
    p = argparse.ArgumentParser(description="Acquire an activity-overview bundle.")
    # Not argparse-required: a --resume run derives these from the prior bundle's
    # meta. acquire() enforces them for a normal (non-resume) run.
    p.add_argument("--owner")
    p.add_argument("--repo")
    p.add_argument("--from", dest="from")
    p.add_argument("--to")
    p.add_argument("--branches", default="main")
    p.add_argument("--clone-dir", default=None)
    p.add_argument("--no-clone", action="store_true")
    p.add_argument("--ref-date", default=None,
                   help="Reference date for milestone framing (default: --to).")
    p.add_argument("--include-workflows", dest="include_workflows",
                   action="store_true", default=True)
    p.add_argument("--no-workflows", dest="include_workflows", action="store_false")
    p.add_argument("--include-releases", dest="include_releases",
                   action="store_true", default=True)
    p.add_argument("--no-releases", dest="include_releases", action="store_false")
    p.add_argument("--resume", default=None,
                   help="Prior bundle JSON: re-resolve only its timeout/failed edges "
                        "against the pinned meta.clone_sha; reuse everything else.")
    p.add_argument("--rollup", nargs="+", default=None, metavar="BUNDLE",
                   help="Merge 2+ monthly bundle JSONs into one multi-period bundle "
                        "(overlap-safe union by identity; structure from the latest). "
                        "Re-run link on the result. No clone/token needed.")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


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
    """Thin wrapper around git (not unit-tested). Surfaces git's own stderr
    on failure so errors like "not a git repository" reach the user. An optional
    `timeout` (seconds) bounds the call — used by the IaC edge build so a hung
    `bicep`/`terraform` cannot stall the run (TimeoutExpired propagates)."""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def clone_head_sha(clone_dir, run=run_git):
    """Resolve the clone's HEAD commit SHA (provenance for resume + roll-up).

    Pins the exact tree the code_graph/edges were built against so a `--resume`
    rebuilds against the identical source (no mixed-SHA graph) and a multi-bundle
    roll-up can pick the newest structural snapshot deterministically. Returns the
    40-char SHA, or None when there is no clone (e.g. --no-clone with nothing on disk)."""
    try:
        return run(["git", "-C", clone_dir, "rev-parse", "HEAD"]).strip() or None
    except Exception:
        return None


# IaC edge-extraction tuning. The timeout is GENEROUS — a healthy cold bicep/
# terraform build finishes well under it, so the bound only trips a genuinely hung
# process (never a slow-but-progressing one). Speed comes from bounded concurrency,
# not a tight timeout. A timed-out/failed build is retried once, then recorded as a
# VISIBLE gap (edges_status) rather than a silent empty.
IAC_BUILD_TIMEOUT = 300   # seconds per bicep/terraform subprocess
IAC_MAX_WORKERS = 8       # parallel per-module builds
IAC_RETRIES = 1           # extra attempts after the first on timeout/failure


def _extract_area_edges(area, clone_dir, area_ids, patterns, have_bicep, have_tf,
                        run, read_text, timeout, retries):
    """Resolve ONE area's edges. Returns (edges_or_None, status). Never raises.

    status: `resolved` (build succeeded — edges may legitimately be empty),
    `timeout` (build exceeded the bound after retries), `failed` (build errored
    after retries), `skipped` (no entrypoint or its CLI absent). A timeout/failure
    leaves edges untouched but is reported via the status so the gap is visible."""
    bp = _bicep_entrypoint(area)
    tp = _tf_entrypoint(area)
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
                return build_bicep_edges(src, arm, bp, area_ids, patterns), "resolved"
            dot = _terraform_graph_dot(run, clone_dir, os.path.dirname(tp), timeout)
            src = read_text(os.path.join(clone_dir, tp))
            return build_terraform_edges(src, dot, tp, area_ids, patterns), "resolved"
        except subprocess.TimeoutExpired:
            status = "timeout"   # hung build: bounded, retried, then recorded
        except Exception:
            status = "failed"
    return None, status


def extract_iac_edges(code_graph, clone_dir, which=shutil.which, run=run_git,
                      read_text=_read_text_file, patterns=None,
                      max_workers=IAC_MAX_WORKERS, timeout=IAC_BUILD_TIMEOUT,
                      retries=IAC_RETRIES, only_status=None):
    """Enrich each area's `edges` with inter-area dependency edges (BUILD-ONLY).

    Each area with a main.bicep (and `bicep` on PATH) or a *.tf (and `terraform` on
    PATH) is built — in PARALLEL across `max_workers`, each subprocess bounded by
    `timeout` and retried `retries` times — to resolve its edges. ANY missing CLI,
    restore failure, build error, or timeout leaves `edges` as-is ([]) but stamps
    `area["edges_status"]` so the gap is VISIBLE, never a silent empty. An aggregate
    `code_graph["edge_extraction"]` counts resolved/timeout/failed/skipped. Injectable
    `which`/`run`/`read_text` keep it offline-testable. Mutates and returns code_graph.

    `only_status` (a set, e.g. {"timeout","failed"}) drives RESUME: only areas whose
    current `edges_status` is in the set are rebuilt; every other area keeps its prior
    edges + status untouched. The summary is recomputed across ALL areas so it reflects
    the merged state. Sound because an area's edges are a pure function of its source."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    areas = code_graph.get("areas", [])
    area_ids = {a["id"] for a in areas}
    have_bicep = bool(which("bicep"))
    have_tf = bool(which("terraform"))
    summary = {"resolved": 0, "timeout": 0, "failed": 0, "skipped": 0}

    # Fresh full run with no toolchain: build-only ⇒ everything skipped. (For a
    # resume, fall through so retained areas keep their prior resolved edges.)
    if only_status is None and not (have_bicep or have_tf):
        for area in areas:
            area["edges_status"] = "skipped"
        summary["skipped"] = len(areas)
        code_graph["edge_extraction"] = summary
        return code_graph

    def work(area):
        if only_status is not None and area.get("edges_status") not in only_status:
            return area, (None, area.get("edges_status") or "skipped")   # retain
        return area, _extract_area_edges(area, clone_dir, area_ids, patterns,
                                         have_bicep, have_tf, run, read_text,
                                         timeout, retries)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for area, (edges, status) in pool.map(work, areas):
            if edges is not None:
                area["edges"] = edges
            area["edges_status"] = status
            summary[status] += 1

    code_graph["edge_extraction"] = summary
    return code_graph


def resume_bundle(prior_bundle, clone_dir, **extract_kw):
    """Re-resolve ONLY the unresolved (timeout/failed) edges of a prior bundle, in
    place, reusing everything else. Cheap way to close a partial edge gap without
    recomputing the window — correct only when the clone is checked out at the prior
    `meta.clone_sha` (same source tree). Returns the updated bundle. Mutates it."""
    code_graph = prior_bundle.get("code_graph") or {}
    extract_iac_edges(code_graph, clone_dir,
                      only_status={"timeout", "failed"}, **extract_kw)
    prior_bundle["code_graph"] = code_graph
    return prior_bundle


# Stable identity keys for overlap-safe roll-up. Monthly runs may overlap by a day
# or two (a gap guarantee against late merges / clock skew); these immutable keys are
# the dedup guarantee, so overlap never double-counts. (commit,path,change) keys a
# code-event; PRs/issues by number; commits by sha; releases by tag; milestones by id.
ROLLUP_ACTIVITY_KEYS = {
    "prs": lambda x: x.get("number"),
    "issues": lambda x: x.get("number"),
    "commits": lambda x: x.get("sha"),
    "releases": lambda x: x.get("tag") or x.get("id") or x.get("name"),
    "milestones": lambda x: x.get("number") or x.get("id") or x.get("title"),
    "code_events": lambda x: (x.get("commit"), x.get("path"), x.get("change")),
}
# Structure/derived fields are a point-in-time snapshot of a moving tree, so the
# roll-up takes them whole from the LATEST installment rather than merging.
ROLLUP_LATEST_FIELDS = ("code_graph", "code_owners", "label_taxonomy",
                        "workflows", "workflow_stats")


def _period_to(bundle):
    m = bundle.get("meta") or {}
    return (m.get("period") or {}).get("to") or m.get("to") or ""


def rollup_bundles(bundles):
    """Merge monthly installments into one multi-period bundle (the cheap through-line
    for a long view; a fresh wide-window re-run stays canonical). ACTIVITY (prs/issues/
    commits/code_events/releases/milestones) is UNIONED by stable identity — overlap
    never double-counts, and the freshest observation of a mutable item (issue state,
    open PR) wins (last-write). STRUCTURE (code_graph/owners/taxonomy/workflows) is taken
    from the latest installment (newest `clone_sha`). `meta.period` spans the union window.
    Derived link/report fields are intentionally dropped — re-run link on the result."""
    if not bundles:
        raise ValueError("rollup_bundles: no bundles given")
    ordered = sorted(bundles, key=_period_to)
    latest = ordered[-1]
    merged = {}

    for field, keyfn in ROLLUP_ACTIVITY_KEYS.items():
        seen = {}
        for i, b in enumerate(ordered):
            for j, item in enumerate(b.get(field) or []):
                k = keyfn(item)
                seen[k if k is not None else (i, j)] = item   # last (freshest) wins
        merged[field] = list(seen.values())

    for field in ROLLUP_LATEST_FIELDS:
        if field in latest:
            merged[field] = latest[field]

    metas = [b.get("meta") or {} for b in ordered]
    froms = [(m.get("period") or {}).get("from") or m.get("from") for m in metas]
    froms = [f for f in froms if f]
    tos = [t for t in (_period_to(b) for b in ordered) if t]
    lm = latest.get("meta") or {}
    span = {"from": min(froms) if froms else lm.get("from"),
            "to": max(tos) if tos else lm.get("to")}
    merged["meta"] = {**lm, "from": span["from"], "to": span["to"],
                      "period": span, "clone_sha": lm.get("clone_sha"),
                      "rolled_up_from": [(m.get("period") or {"from": m.get("from"),
                                          "to": m.get("to")}) for m in metas]}
    return merged


def http_get_json(url, token):
    """GET a GitHub API URL → (parsed_json, next_url). Not unit-tested.

    On an HTTP error, GitHub explains the cause in the response body and a few
    headers (rate limit vs. SAML SSO vs. token scope). urllib discards both by
    default, leaving only a bare "HTTP Error 403", so we surface them ourselves.
    """
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "activity-overview",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            nxt = _next_link(resp.headers.get("Link", ""))
        return body, nxt
    except urllib.error.HTTPError as err:
        raise SystemExit(_format_http_error(url, err)) from err


# Headers GitHub uses to explain a refusal; surfaced verbatim on error so the
# distinction (rate limit / SAML SSO / missing scope) is visible without guessing.
_DIAGNOSTIC_HEADERS = (
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-github-sso",
    "x-accepted-oauth-scopes",
    "x-oauth-scopes",
)


def _format_http_error(url, err):
    """Build a one-screen diagnostic from an HTTPError: status, GitHub's message,
    and the headers that disambiguate a 403."""
    try:
        detail = json.loads(err.read().decode()).get("message", "")
    except Exception:
        detail = ""
    lines = [f"error: GitHub API {err.code} for {url}"]
    if detail:
        lines.append(f"  message: {detail}")
    for name in _DIAGNOSTIC_HEADERS:
        value = err.headers.get(name)
        if value:
            lines.append(f"  {name}: {value}")
    if err.code == 403:
        lines.append(
            "  hint: 403 with rate-limit headers = rate limited; with x-github-sso "
            "= authorize the PAT for the org's SAML SSO; otherwise check token scope."
        )
    return "\n".join(lines)


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


def _runs_page(get_page, api, frm, to):
    """Walk actions/runs, whose pages wrap the list under `workflow_runs`."""
    runs = []
    url = f"{api}/actions/runs?created={frm}..{to}&per_page=100"
    # The `created=` query param bounds the window server-side; the per-page
    # `< frm` check below is a defensive early-exit guard.
    while url:
        payload, url = get_page(url)
        page = payload.get("workflow_runs", []) if isinstance(payload, dict) else payload
        runs.extend(page)
        if page and page[-1].get("created_at", "")[:10] < frm:
            break
    return runs


def resume_acquire(args):
    """`--resume` path: load a prior bundle, check the clone out at its pinned
    `meta.clone_sha`, re-resolve only the timeout/failed edges, reuse everything
    else. Not unit-tested (git/IO orchestration); the merge core is resume_bundle."""
    with open(args.resume) as fh:
        prior = json.load(fh)
    meta = prior.get("meta") or {}
    owner, repo = meta.get("owner"), meta.get("repo")
    clone_dir = args.clone_dir or meta.get("clone_dir") or f"workspace/{repo}-clone"
    sha = meta.get("clone_sha")
    if not sha:
        sys.stderr.write("error: prior bundle has no meta.clone_sha to resume against\n")
        raise SystemExit(2)
    if not args.no_clone:
        os.makedirs(os.path.dirname(clone_dir) or ".", exist_ok=True)
        if not os.path.isdir(os.path.join(clone_dir, ".git")):
            run_git(["git", "clone", f"https://github.com/{owner}/{repo}.git", clone_dir])
        # fetch the exact pinned commit (works on a shallow clone) and check it out,
        # so the rebuild runs against the IDENTICAL tree the prior run resolved.
        run_git(["git", "-C", clone_dir, "fetch", "--depth", "1", "origin", sha])
        run_git(["git", "-C", clone_dir, "checkout", "--force", sha])
    before = dict((prior.get("code_graph") or {}).get("edge_extraction") or {})
    resume_bundle(prior, clone_dir)
    after = (prior.get("code_graph") or {}).get("edge_extraction") or {}
    sys.stderr.write(f"resume: edge_extraction {before} -> {after}\n")
    return prior


def acquire(args, env):
    if getattr(args, "rollup", None):
        bundles = []
        for path in args.rollup:
            with open(path) as fh:
                bundles.append(json.load(fh))
        return rollup_bundles(bundles)
    if getattr(args, "resume", None):
        return resume_acquire(args)
    if not (args.owner and args.repo and getattr(args, "from") and args.to):
        sys.stderr.write("error: --owner --repo --from --to are required "
                         "(or pass --resume <prior-bundle>)\n")
        raise SystemExit(2)
    token = resolve_token(env)
    owner, repo = args.owner, args.repo
    frm, to = getattr(args, "from"), args.to
    clone_dir = args.clone_dir or f"workspace/{repo}-clone"
    repo_url = f"https://github.com/{owner}/{repo}.git"

    if not args.no_clone:
        # git clone needs the parent dir to exist for the default workspace/ path.
        os.makedirs(os.path.dirname(clone_dir) or ".", exist_ok=True)
        run_git(build_clone_cmd(repo_url, frm, clone_dir))

    # Phase 1 walks the checked-out default branch only; `args.branches` is
    # recorded in meta for provenance but not yet applied to the log/clone.
    # Multi-branch commit walking arrives in a later phase.
    raw = run_git([
        "git", "-C", clone_dir, "log",
        f"--since={frm}", f"--until={to}",
        f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short", "--name-only",
    ])
    commits = parse_git_log(raw)

    # Phase 3a: full-window file-level code-event walk (--name-status -M -C).
    # Guarded so --no-clone / missing clone degrades gracefully to empty.
    # `--reverse` so events arrive OLDEST→NEWEST: build_artifacts appends lifecycle
    # in walk order and derives status from the last (newest) event.
    code_events = []
    if not args.no_clone or os.path.isdir(clone_dir):
        raw_walk = run_git([
            "git", "-C", clone_dir, "log", "--reverse",
            f"--since={frm}", f"--until={to}",
            f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short",
            "--name-status", "-M", "-C",
        ])
        code_events = parse_code_events(raw_walk)

    # Phase 3d: symbol-granular change attribution via a single `git log -p` walk
    # (diff-local, no per-commit checkout). Same guard/degradation + oldest→newest
    # ordering (--reverse) as the file walk so symbol lifecycle/status is correct.
    symbol_events = []
    if not args.no_clone or os.path.isdir(clone_dir):
        raw_patch = run_git([
            "git", "-C", clone_dir, "log", "--reverse",
            f"--since={frm}", f"--until={to}",
            f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short",
            "-p", "--unified=3", "-M", "-C",
        ])
        symbol_events = parse_symbol_events(raw_patch)

    # Drop any shallow-boundary commit whose diff is a whole-tree phantom (its parent
    # was grafted away by the bounded clone) — guards both walks against inflation.
    _boundary = shallow_boundary_shas(clone_dir)
    code_events = drop_boundary_events(code_events, _boundary)
    symbol_events = drop_boundary_events(symbol_events, _boundary)
    _boundary_dropped = in_window_boundary_commits(_boundary, commits)
    if _boundary_dropped:
        sys.stderr.write(
            f"warning: {len(_boundary_dropped)} in-window commit(s) sit at the shallow "
            f"clone boundary; their whole-tree phantom diffs were dropped (a visible gap "
            f"in meta.boundary_dropped_commits) — widen CLONE_MARGIN_DAYS to recover: "
            f"{_boundary_dropped}\n")

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
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm,
    )
    # ...plus all currently-open PRs (these feed in_flight / next_candidates).
    raw_open = fetch_all(
        get_page, f"{api}/pulls?state=open&sort=updated&direction=desc&per_page=100")

    prs = []
    for raw in raw_closed + raw_open:
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
        reviews = fetch_all(get_page, f"{api}/pulls/{pr['number']}/reviews?per_page=100")
        summary = summarize_reviews(reviews)
        pr["reviewers"] = summary["reviewers"]
        pr["review_decision"] = summary["decision"]
        timeline = fetch_all(
            get_page, f"{api}/issues/{pr['number']}/timeline?per_page=100")
        pr["crossref_issues"] = parse_timeline_crossrefs(timeline)
        review_comments = fetch_all(
            get_page, f"{api}/pulls/{pr['number']}/comments?per_page=100")
        pr["review_comments"] = [normalize_review_comment(c) for c in review_comments]
        conv_comments = fetch_all(
            get_page, f"{api}/issues/{pr['number']}/comments?per_page=100")
        pr["comments_list"] = [normalize_comment(c) for c in conv_comments]
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
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm,
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
        raw_issue, _ = http_get_json(f"{api}/issues/{n}", token)
        if raw_issue.get("pull_request"):
            continue
        raw_by_num[raw_issue["number"]] = raw_issue
        issues.append(normalize_issue(raw_issue))

    for issue in issues:
        n = issue["number"]
        conv = fetch_all(get_page, f"{api}/issues/{n}/comments?per_page=100")
        issue["comments_list"] = [normalize_comment(c) for c in conv]
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

    workflows = []
    workflow_stats = {}
    if getattr(args, "include_workflows", True):
        raw_runs = _runs_page(get_page, api, frm, to)
        workflows = [normalize_workflow(r) for r in raw_runs]
        workflow_stats = aggregate_workflow_stats(workflows)

    releases = []
    if getattr(args, "include_releases", True):
        raw_releases = fetch_all(get_page, f"{api}/releases?per_page=100")
        releases = [normalize_release(r) for r in raw_releases
                    if in_window(r.get("published_at"), frm, to)]

    raw_milestones = fetch_all(get_page, f"{api}/milestones?state=all&per_page=100")
    milestones = [normalize_milestone(m) for m in raw_milestones]

    ref_date = getattr(args, "ref_date", None) or to
    meta = {
        "owner": owner, "repo": repo, "from": frm, "to": to,
        "branches": args.branches.split(","), "clone_dir": clone_dir,
        "ref_date": ref_date, "clone_sha": clone_head_sha(clone_dir),
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
    return bundle


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    bundle = acquire(args, os.environ)
    period = bundle.get("meta", {}).get("period", {})
    out = (args.out or f"workspace/activity-{getattr(args, 'from', None) or period.get('from')}"
           f"-{args.to or period.get('to')}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(f"wrote {out}\n")
    return out


if __name__ == "__main__":
    main()
