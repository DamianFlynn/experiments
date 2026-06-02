"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""
import argparse
import json
import os
import re
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


def build_clone_cmd(repo_url, from_date, clone_dir):
    """Construct the bounded, partial clone command (network-free to build)."""
    return [
        "git", "clone",
        "--filter=blob:none",
        f"--shallow-since={from_date}",
        "--no-single-branch",
        repo_url, clone_dir,
    ]


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
    shape {id, author, author_association, body, url}. Pure, permissive."""
    return {
        "id": raw.get("id"),
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "body": raw.get("body") or "",
        "url": raw.get("html_url"),
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
    `symbol` and `comment` kinds need hunk/AST parsing and are deferred."""
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
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--from", dest="from", required=True)
    p.add_argument("--to", required=True)
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


def run_git(args, cwd=None):
    """Thin wrapper around git (not unit-tested). Surfaces git's own stderr
    on failure so errors like "not a git repository" reach the user."""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


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


def acquire(args, env):
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
    log_fmt = "%x1e%H%x1f%P%x1f%an%x1f%ad%x1f%s"
    raw = run_git([
        "git", "-C", clone_dir, "log",
        f"--since={frm}", f"--until={to}",
        f"--pretty=format:{log_fmt}", "--date=short", "--name-only",
    ])
    commits = parse_git_log(raw)

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
    for raw in raw_repo_issues + raw_open_issues:
        if raw.get("pull_request"):  # the issues endpoint also lists PRs; skip them
            continue
        if raw["number"] in seen:
            continue
        issue = normalize_issue(raw)
        seen.add(issue["number"])
        issues.append(issue)
    for n in sorted(wanted - seen):
        raw_issue, _ = http_get_json(f"{api}/issues/{n}", token)
        if raw_issue.get("pull_request"):
            continue
        issues.append(normalize_issue(raw_issue))

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
        "ref_date": ref_date,
        "period": {"from": frm, "to": to}, "prev_bundle": None,
    }
    bundle = build_bundle(meta, commits, prs, issues)
    bundle["workflows"] = workflows
    bundle["workflow_stats"] = workflow_stats
    bundle["releases"] = releases
    bundle["milestones"] = milestones
    return bundle


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    bundle = acquire(args, os.environ)
    out = args.out or f"workspace/activity-{getattr(args, 'from')}-{args.to}.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(f"wrote {out}\n")
    return out


if __name__ == "__main__":
    main()
