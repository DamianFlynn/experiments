"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import sys
from datetime import datetime, timezone

import gather  # for resolve_commit_pr / attach_commit_prs (commit->PR resolution)

# Phase 7b-1: the pure derivations now live in `derive` (a leaf module that does
# NOT import link or gather), so the store write-path can derive these without a
# link dependency. They are re-imported here so link's public API and `enrich`
# are byte-identical to before — `link.enrich` calls the same functions, now
# resolving to the names imported from derive (one-way: link -> derive).
from derive import (
    ref,
    attribute_code_areas,
    _commit_areas,
    build_modules,
    link_symbol_identity,
    annotate_review_rounds,
    annotate_reopen_count,
)

# commit->PR resolution lives in gather (shared with the store writer, so trains
# and the store's part_of edges read the same signal); re-exported here to keep
# link's public entry points (and its callers/tests) stable.
resolve_commit_pr = gather.resolve_commit_pr
attach_commit_prs = gather.attach_commit_prs


HIGH_PRIORITY_LABELS = {
    "priority/high", "priority/critical", "p0", "p1", "high-priority", "critical",
}


def _in_window(ts, period):
    """True if `ts` (ISO) falls in `period`. Permissive when either is missing,
    so dateless fixtures (and pre-window-free bundles) classify as in-window."""
    if not period or not ts:
        return True
    frm, to = period.get("from"), period.get("to")
    day = ts[:10]
    return (not frm or day >= frm) and (not to or day <= to)


def _high_priority(item):
    return any((lbl or "").lower() in HIGH_PRIORITY_LABELS
               for lbl in item.get("labels", []))


def _ms_sort_key(m):
    # `or 0` (not a default arg) guarantees an int secondary key even when
    # `number` is present-but-null; real GitHub milestone numbers are >= 1, so
    # collapsing a hypothetical 0 to 0 is harmless.
    return ((m.get("due_on") or "9999-12-31")[:10], m.get("number") or 0)


def select_milestones(milestones, ref_date):
    """(current, next) open milestones by due date. current = earliest open whose
    due date is on/after ref_date (else the earliest open); next = the one after."""
    open_ms = sorted((m for m in milestones if m.get("state") == "open"),
                     key=_ms_sort_key)
    if not open_ms:
        return None, None
    current = next(
        (m for m in open_ms if (m.get("due_on") or "9999-12-31")[:10] >= ref_date),
        open_ms[0])
    idx = open_ms.index(current)
    nxt = open_ms[idx + 1] if idx + 1 < len(open_ms) else None
    return current, nxt


def train_index(trains):
    """Map ('pr'|'issue', number) -> train id, for cross-linking bucket refs."""
    idx = {}
    for t in trains:
        if t.get("root_issue") is not None:
            idx[("issue", t["root_issue"])] = t["id"]
        for n in t.get("prs", []):
            idx[("pr", n)] = t["id"]
    return idx


def build_timeline(bundle):
    """Merge social + code events into one chronological event stream.

    Event shape: {ts, actor, layer:'social'|'code', event, ref:{type,...,url},
    subject:{kind,name,path}}. Social events come from PR/issue comments + review
    comments; code events from artifact lifecycle entries. Sorted by ts.
    Malformed records that lack created_at fall back to URL ordering as a last
    resort (not a normal case — well-formed comment objects carry created_at). Pure.
    """
    events = []

    def social(actor, event, ref_type, number, url, subject, ts):
        events.append({
            "ts": ts or "", "actor": actor, "layer": "social", "event": event,
            # `id` (not `number`) to match the bundle-wide ref convention
            # {type, id, url} used everywhere else (and the gate's well_formed).
            "ref": {"type": ref_type, "id": number, "url": url},
            "subject": subject,
        })

    for pr in bundle.get("prs", []):
        url = pr.get("url")
        for c in pr.get("review_comments", []):
            curl = c.get("url") or url
            social(c.get("author"), "review_comment", "pr", pr["number"],
                   curl,
                   {"kind": "review_comment", "name": None, "path": None},
                   c.get("created_at") or curl)
        for c in pr.get("comments_list", []):
            curl = c.get("url") or url
            social(c.get("author"), "comment", "pr", pr["number"],
                   curl,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at") or curl)

    for issue in bundle.get("issues", []):
        url = issue.get("url")
        for c in issue.get("comments_list", []):
            curl = c.get("url") or url
            social(c.get("author"), "comment", "issue", issue["number"],
                   curl,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at") or curl)

    for art in bundle.get("artifacts", {}).values():
        for ev in art.get("lifecycle", []):
            events.append({
                "ts": ev.get("date") or "", "actor": ev.get("author"),
                "layer": "code", "event": ev["event"], "ref": ev["ref"],
                "subject": {"kind": art["kind"], "name": art["name"],
                            "path": art["path"]},
            })

    # Stable sort by ts (then by url so equal-ts events are deterministic).
    events.sort(key=lambda e: (e["ts"], str(e["ref"].get("url") or "")))
    return events


_EVENT_TO_DELTA = {"add": "add", "remove": "drop", "change": "change"}


def compute_feature_deltas(bundle):
    """Project the artifacts ledger into the feature_deltas view.

    One delta per lifecycle event: add->add, remove->drop, change->change. Each
    attributes author/commit/url + (best-effort) the owning pr/train via the
    commit->PR map Link already builds. For SYMBOL/COMMENT artifacts (Phase 3d)
    `before`/`after` carry the bounded hunk snippet and `detail` is
    "<lang> <subkind> <name>"; file-level deltas leave them null. `area` is filled
    later by attribute_code_areas. Pure.
    """
    commit_to_pr = {c["sha"]: c.get("pr") for c in bundle.get("commits", [])}
    pr_to_train = {}
    for t in bundle.get("trains", []):
        for n in t.get("prs", []):
            pr_to_train[n] = t["id"]

    deltas = []
    for aid, art in bundle.get("artifacts", {}).items():
        for ev in art.get("lifecycle", []):
            kind = _EVENT_TO_DELTA.get(ev["event"])
            if kind is None:
                continue
            pr = commit_to_pr.get(ev["commit"])
            is_symbol = art["kind"] in ("symbol", "comment")
            detail = (f'{art.get("lang", "")} {art.get("subkind", "")} '
                      f'{art["name"]}').strip() if is_symbol else None
            delta = {
                "area": None,
                "kind": kind,
                "subject": art["kind"],
                "name": art["name"],
                "before": ev.get("before"),
                "after": ev.get("after"),
                "detail": detail,
                "artifact": aid,
                "author": ev["author"],
                "train": pr_to_train.get(pr) if pr is not None else None,
                "pr": pr,
                "commit": ev["commit"],
                "url": ev["ref"]["url"],
            }
            # Phase 10 slice-diffs: surface the bounded file diff on FILE-level deltas
            # (the language-agnostic "what changed"); symbol/comment deltas keep their
            # before/after unchanged. Omit-when-empty for byte stability.
            if not is_symbol and ev.get("hunk"):
                delta["diff"] = ev["hunk"]
            deltas.append(delta)
    return deltas


def _targets_mainline(pr, base_branch):
    """True if `pr` TARGETS the analyzed mainline branch (base == base_branch), or
    the base/base_branch is unknown (older bundles). Independent of merge state."""
    base = pr.get("base")
    return base_branch is None or base is None or base == base_branch


def _mainline_merge(pr, base_branch):
    """True if `pr` merged INTO the analyzed mainline branch. A merged PR whose
    KNOWN base differs from `base_branch` is a stacked/fork contribution (it merged
    into another branch, not main) — tracked as a `contributing_pr` on the parent's
    train rather than counted as shipped-to-main. Pure."""
    return bool(pr.get("merged")) and _targets_mainline(pr, base_branch)


def _pr_anchor(pr):
    """A PR's train anchor: its first closing/crossref issue, else the PR itself."""
    links = list(pr.get("closes") or [])
    for n in pr.get("crossref_issues") or []:
        if n not in links:
            links.append(n)
    root = links[0] if links else None
    return ("issue", root) if root is not None else ("pr", pr["number"])


def build_trains(bundle):
    """Group PRs (+ their commits + closing issue) into decision trains.

    A train is anchored deterministically by its root issue (`train-issue-<n>`) or,
    failing that, its PR (`train-pr-<n>`). One train per anchor, by precedence
    shipped > in_flight > rejected > abandoned:
      - `shipped`   — at least one PR merged into the analyzed mainline branch.
      - `in_flight` — only OPEN PR(s) targeting mainline; the effort is in progress.
      - `rejected`  — only CLOSED-unmerged PR(s) targeting mainline; the change was dropped.
      - `abandoned` — an issue closed `not_planned` with no PR train of its own.
    A PR merged into ANOTHER PR's branch (base == that PR's head) is a stacked/fork
    contribution: it lands as a `contributing_prs` entry on the parent PR's train —
    the journey-to-main context — not its own train and not shipped-to-main.
    """
    commits_by_pr = {}
    for c in bundle["commits"]:
        commits_by_pr.setdefault(c.get("pr"), []).append(c["sha"])
    issues_by_num = {i["number"]: i for i in bundle["issues"]}
    base_branch = bundle.get("meta", {}).get("base_branch")
    prs_all = bundle["prs"]

    def _kind_for(root_issue, prs):
        kind = "other"
        if root_issue is not None and root_issue in issues_by_num:
            kind = issues_by_num[root_issue].get("kind", "other")
        if kind == "other":
            # No typed root issue: derive from the PRs' conventional-commit titles
            # (feat->feature, fix->bug, docs->docs). `prs` sorted, lowest # wins.
            for p in prs:
                pk = gather.classify_pr_kind(p)
                if pk:
                    kind = pk
                    break
        return kind

    def _make_train(anchor, prs, outcome):
        akind, key = anchor
        prs = sorted(prs, key=lambda p: p["number"])
        pr_numbers = [p["number"] for p in prs]
        root_issue = key if akind == "issue" else None
        evidence, shas = [], []
        if root_issue is not None and root_issue in issues_by_num:
            evidence.append(ref("issue", root_issue, issues_by_num[root_issue]["url"]))
        for p in prs:
            shas.extend(commits_by_pr.get(p["number"], []))
            evidence.append(ref("pr", p["number"], p["url"]))
        return {
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": _kind_for(root_issue, prs),
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": sorted(shas),
            "code_areas": [],
            "outcome": outcome,
            "contributing_prs": [],
            "evidence": evidence,
        }

    # 1. Partition mainline-targeting PRs by outcome class, grouped by anchor.
    shipped_groups, inflight_groups, rejected_groups = {}, {}, {}
    for pr in prs_all:
        anchor = _pr_anchor(pr)
        if _mainline_merge(pr, base_branch):
            shipped_groups.setdefault(anchor, []).append(pr)
        elif not _targets_mainline(pr, base_branch):
            continue  # stacked/fork PR — handled as a contribution in step 3
        elif pr.get("state") == "open" and not pr.get("merged"):
            inflight_groups.setdefault(anchor, []).append(pr)
        elif pr.get("state") == "closed" and not pr.get("merged"):
            rejected_groups.setdefault(anchor, []).append(pr)

    # 2. Materialise trains by precedence — one per anchor:
    #    shipped > in_flight > rejected > abandoned.
    train_by_anchor = {}

    def _claim(groups, outcome):
        for anchor, prs in groups.items():
            if anchor not in train_by_anchor:
                train_by_anchor[anchor] = _make_train(anchor, prs, outcome)

    _claim(shipped_groups, "shipped")
    _claim(inflight_groups, "in_flight")
    _claim(rejected_groups, "rejected")
    # abandoned: issues closed not-planned with no PR-anchored train of their own.
    for iss in bundle["issues"]:
        if iss.get("state") == "closed" and iss.get("state_reason") == "not_planned":
            anchor = ("issue", iss["number"])
            if anchor not in train_by_anchor:
                train_by_anchor[anchor] = _make_train(anchor, [], "abandoned")

    # 3. Attach stacked contributions: a PR merged into a NON-mainline branch that is
    #    some mainline-targeting PR's head feeds that parent's train (its journey).
    head_to_parent = {p["head"]: p for p in prs_all
                      if p.get("head") and _targets_mainline(p, base_branch)}
    for pr in prs_all:
        if not (pr.get("merged") and base_branch and pr.get("base")
                and pr["base"] != base_branch):
            continue  # only stacked (merged into a known non-mainline branch)
        parent = head_to_parent.get(pr["base"])
        if parent is None or parent["number"] == pr["number"]:
            continue
        train = train_by_anchor.get(_pr_anchor(parent))
        if train is not None and pr["number"] not in train["contributing_prs"]:
            train["contributing_prs"].append(pr["number"])
            train["contributing_prs"].sort()

    return sorted(train_by_anchor.values(), key=lambda t: t["id"])


def compute_buckets(bundle):
    """Full four-way bucketing: one bucket per item, precedence
    shipped > rejected > next_candidates > in_flight. Refs carry their train id."""
    meta = bundle.get("meta", {})
    period = meta.get("period")
    base_branch = meta.get("base_branch")
    ref_date = meta.get("ref_date") or meta.get("to") or ""
    current_ms, next_ms = select_milestones(bundle.get("milestones", []), ref_date)
    current_title = current_ms["title"] if current_ms else None
    next_title = next_ms["title"] if next_ms else None
    tindex = train_index(bundle.get("trains", []))

    out = {"shipped": [], "rejected": [], "next_candidates": [], "in_flight": []}

    def add(bucket, type_, num, url):
        r = ref(type_, num, url)
        tid = tindex.get((type_, num))
        if tid:
            r["train"] = tid
        out[bucket].append(r)

    def classify(item, type_):
        num, url = item["number"], item.get("url")
        state = item.get("state")
        if type_ == "pr" and _mainline_merge(item, base_branch) \
                and _in_window(item.get("merged_at"), period):
            add("shipped", type_, num, url)
        elif type_ == "issue" and state == "closed" \
                and item.get("state_reason") == "completed" \
                and _in_window(item.get("closed_at"), period):
            add("shipped", type_, num, url)
        elif state == "closed" and type_ == "pr" and not item.get("merged") \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "closed" and type_ == "issue" \
                and item.get("state_reason") == "not_planned" \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "open":
            on_next = next_title is not None and item.get("milestone") == next_title
            on_current = current_title is not None and item.get("milestone") == current_title
            if on_next or _high_priority(item):
                add("next_candidates", type_, num, url)
            elif on_current or _in_window(item.get("updated_at"), period):
                add("in_flight", type_, num, url)
        # Anything else (stale open items off any milestone; closed items with an
        # unrecognised state_reason) is intentionally left in no bucket.

    for pr in bundle.get("prs", []):
        classify(pr, "pr")
    for issue in bundle.get("issues", []):
        classify(issue, "issue")
    return out


def attribute_train_areas(bundle, idx):
    """Set each train's `code_areas` from its commits' files. In place. Pure."""
    by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    for t in bundle.get("trains", []):
        areas = set()
        for sha in t.get("commits", []):
            c = by_sha.get(sha)
            if c:
                areas |= _commit_areas(c, idx)
        t["code_areas"] = sorted(areas)
    return bundle


# ---------------------------------------------------------------------------
# Phase 4a: train significance scoring + treatment tier
# ---------------------------------------------------------------------------

# Bounding constants for slice_train (Phase 4a).  Tune without touching logic.
# Any body/message/comment text longer than this is truncated with a marker.
SLICE_TEXT_CAP = 1500
# For each comment list, keep at most this many bodies and record the overflow.
SLICE_COMMENTS_KEPT = 6
# Per-train budget (chars) for the TOTAL of feature_delta `diff` snippets carried in
# one slice. Each file diff is already per-file bounded (gather.FILE_DIFF_CAP); this
# caps a churny train's combined diffs so they can't dominate the narrator's context.
SLICE_DIFF_CAP = 6000

# Per-kind multiplier on the raw footprint.  feature/module-request represent
# the heaviest intentional work; bug is medium; idea captures light exploration;
# docs/chore/other are lightweight.  Tune these values without touching the
# scoring formula — that's why they're a named constant.
TRAIN_KIND_WEIGHTS = {
    "feature": 3.0,
    "module-request": 3.0,
    "bug": 2.0,
    "idea": 1.5,
    "docs": 1.0,
    "chore": 1.0,
    "other": 1.0,
}

# Top-N trains (by significance desc, then id) that always receive the "deep"
# treatment tier regardless of absolute score.
TRAIN_SIGNIFICANCE_TOP_N = 8

# Absolute floor: any train at or above this value is "deep" even when ranked
# outside the top-N.  Reasoning: a single multi-PR feature train with 3 PRs
# + 3 commits + 2 areas has footprint=8 and weight=3.0, so significance=8*3+2
# = 26.  Setting the floor at 20 ensures any such train always clears it while
# a minimal 1-PR docs train (footprint=2, weight=1.0, breadth=0 → sig=2.0)
# stays well below it.
TRAIN_SIGNIFICANCE_FLOOR = 20.0

# Elapsed-days threshold above which a merged train is considered "stalled"
# (took too long to land).  Tune without touching logic.
TRAIN_STALL_DAYS = 21

# ---------------------------------------------------------------------------
# Phase 4a: next-release forecast tunables
# ---------------------------------------------------------------------------

# Weight for each scoring signal. Heavier signals represent stronger evidence
# that an item will land in the next release:
#   on_next_milestone — explicitly scheduled; strongest signal (5.0)
#   high_priority     — labelled urgent by the team (3.0)
#   in_motion         — active work already underway (2.0)
#   recent_activity   — touched inside the window (1.5)
#   overdue           — long-open item, mild pressure to close (1.0)
FORECAST_WEIGHTS = {
    "on_next_milestone": 5.0,
    "high_priority":     3.0,
    "in_motion":         2.0,
    "recent_activity":   1.5,
    "overdue":           1.0,
}

# Tier thresholds (score ranges → tier label).
# Reasoning: on_next_milestone alone (5.0) clears the "likely" bar, meaning
# any explicitly scheduled item is at least likely.  high_priority alone (3.0)
# clears "possible" (≥2.0) but not "likely" (<5.0).  A bare item with only the
# mild overdue signal (1.0) stays in "longshot" (<2.0).
FORECAST_TIER_LIKELY_THRESHOLD   = 5.0   # score ≥ this → "likely"
FORECAST_TIER_POSSIBLE_THRESHOLD = 2.0   # score ≥ this → "possible" (else "longshot")

# Age threshold (days) for the "overdue / long-open" signal.
# 200 days chosen so the fixture default created_at="2026-01-01" (≈150 days before
# the typical ref_date of 2026-05-31) does NOT trigger the signal, while genuinely
# stale items created a year or more ago (e.g. "2025-01-01", 516 days) do.
FORECAST_OVERDUE_DAYS = 200


def score_train_significance(bundle):
    """Annotate each train with `significance` (float) and `tier` ('deep'|'mention').

    significance = footprint * kind_weight + breadth, where:
      footprint = len(prs) + len(commits) + len(code_areas) + len(contributing_prs)
      kind_weight from TRAIN_KIND_WEIGHTS (unknown kinds → 'other' weight)
      breadth = len(code_areas)
    tier = 'deep' for the top-TRAIN_SIGNIFICANCE_TOP_N trains OR any train
    whose significance >= TRAIN_SIGNIFICANCE_FLOOR; 'mention' otherwise.
    Outcome is intentionally ignored — a rejected train's story still matters.
    Mutates trains in place; returns bundle for convenience."""
    trains = bundle.get("trains", [])
    other_weight = TRAIN_KIND_WEIGHTS["other"]

    for t in trains:
        footprint = (len(t.get("prs", [])) + len(t.get("commits", []))
                     + len(t.get("code_areas", [])) + len(t.get("contributing_prs", [])))
        kind_weight = TRAIN_KIND_WEIGHTS.get(t.get("kind", "other"), other_weight)
        breadth = len(t.get("code_areas", []))
        t["significance"] = float(footprint * kind_weight + breadth)

    # Stable sort: significance desc, then id asc for deterministic tiebreaking.
    ranked = sorted(trains, key=lambda t: (-t["significance"], t["id"]))
    top_ids = {t["id"] for t in ranked[:TRAIN_SIGNIFICANCE_TOP_N]}

    for t in trains:
        if t["id"] in top_ids or t["significance"] >= TRAIN_SIGNIFICANCE_FLOOR:
            t["tier"] = "deep"
        else:
            t["tier"] = "mention"

    return bundle


def _parse_ts(ts):
    """Parse an ISO-8601 timestamp string (with or without trailing Z) to a
    timezone-aware datetime, or return None when ts is absent/unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _effort_for_train(train, pr_index, issue_index):
    """Compute the effort dict for one train.  Pure; never mutates arguments."""
    pr_nums = train.get("prs", [])
    train_prs = [pr_index[n] for n in pr_nums if n in pr_index]

    # ---- dates --------------------------------------------------------
    root_num = train.get("root_issue")
    root_issue = issue_index.get(root_num) if root_num is not None else None

    issue_created = _parse_ts((root_issue or {}).get("created_at"))
    pr_created_dates = [d for d in
                        (_parse_ts(p.get("created_at")) for p in train_prs)
                        if d is not None]
    earliest_pr = min(pr_created_dates) if pr_created_dates else None

    candidates = [d for d in (issue_created, earliest_pr) if d is not None]
    opened_dt = min(candidates) if candidates else None
    opened_at = opened_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if opened_dt else None

    merged_dates = [d for d in
                    (_parse_ts(p.get("merged_at")) for p in train_prs
                     if p.get("merged"))
                    if d is not None]
    merged_dt = max(merged_dates) if merged_dates else None
    merged_at = merged_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if merged_dt else None

    if opened_dt is not None and merged_dt is not None:
        elapsed_days = (merged_dt - opened_dt).days
    else:
        elapsed_days = None

    # ---- review effort ------------------------------------------------
    all_reviewers = set()
    review_comments = 0
    for p in train_prs:
        for r in p.get("reviewers", []):
            if r:
                all_reviewers.add(r)
        review_comments += p.get("review_comments_count", 0) or 0

    # ---- commits ------------------------------------------------------
    commits = len(train.get("commits", []))

    # ---- participants -------------------------------------------------
    participants = set()

    def add_login(login):
        if login:
            participants.add(login)

    # PR authors
    for p in train_prs:
        add_login(p.get("author"))
    # root issue author
    if root_issue:
        add_login(root_issue.get("author"))
    # all reviewers
    participants |= all_reviewers
    # PR comment authors
    for p in train_prs:
        for c in p.get("comments_list", []):
            add_login(c.get("author"))
    # root issue comment authors
    if root_issue:
        for c in root_issue.get("comments_list", []):
            add_login(c.get("author"))

    # ---- stalled ------------------------------------------------------
    stalled = (elapsed_days is not None and elapsed_days > TRAIN_STALL_DAYS)

    return {
        "opened_at": opened_at,
        "merged_at": merged_at,
        "elapsed_days": elapsed_days,
        "reviewers": len(all_reviewers),
        "review_comments": review_comments,
        "commits": commits,
        "participants": len(participants),
        "stalled": stalled,
    }


def annotate_train_effort(bundle):
    """Annotate each train in bundle['trains'] with an `effort` dict.

    Computes time-and-effort signals purely from bundle data (prs + issues).
    Mutates trains in place; returns bundle for convenience."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    for train in bundle.get("trains", []):
        train["effort"] = _effort_for_train(train, pr_index, issue_index)
    return bundle


def _cap_text(s):
    """Truncate `s` to SLICE_TEXT_CAP chars; append '…[+N chars]' when cut. Pure."""
    if not s or len(s) <= SLICE_TEXT_CAP:
        return s
    overflow = len(s) - SLICE_TEXT_CAP
    return s[:SLICE_TEXT_CAP] + f"…[+{overflow} chars]"


def _cap_comments(comment_objs):
    """Return (kept_bodies, overflow_count) from a list of comment dicts.

    Keeps up to SLICE_COMMENTS_KEPT comment bodies (strings, text-capped);
    overflow_count is the number of comments dropped beyond the cap.
    """
    bodies = [_cap_text(c.get("body") or "") for c in (comment_objs or [])]
    kept = bodies[:SLICE_COMMENTS_KEPT]
    return kept, max(0, len(bodies) - len(kept))


def _cap_reviews(review_objs):
    """Return (kept_reviews, overflow_count) from a list of review-submission dicts.

    Keeps up to SLICE_COMMENTS_KEPT submissions, each projected to
    {author, state, submitted_at, body} with body run through _cap_text;
    overflow_count is the number of submissions dropped beyond the cap.
    Mirrors _cap_comments but yields objects (not bare strings).
    """
    kept = [
        {
            "author":       r.get("author"),
            "state":        r.get("state"),
            "submitted_at": r.get("submitted_at"),
            "body":         _cap_text(r.get("body")),
        }
        for r in (review_objs or [])[:SLICE_COMMENTS_KEPT]
    ]
    return kept, max(0, len(review_objs or []) - len(kept))


def _cap_lifecycle(event_objs):
    """Return (kept_events, overflow_count) from a list of lifecycle-event dicts.

    Keeps up to SLICE_COMMENTS_KEPT events, each projected to
    {event, actor, created_at, label} (drops id/url); overflow_count is the
    number of events dropped beyond the cap.
    """
    kept = [
        {
            "event":      e.get("event"),
            "actor":      e.get("actor"),
            "created_at": e.get("created_at"),
            "label":      e.get("label"),
        }
        for e in (event_objs or [])[:SLICE_COMMENTS_KEPT]
    ]
    return kept, max(0, len(event_objs or []) - len(kept))


def _cap_train_diffs(deltas):
    """Bound the TOTAL `diff` chars across a train's feature_deltas. Pure.

    Keeps each delta's `diff` until SLICE_DIFF_CAP chars are used, then strips `diff`
    from the remaining deltas (copying only those, so the bundle is never mutated —
    the slice is read-only by contract). Returns (deltas_out, dropped_count) where
    dropped_count is how many deltas had their diff omitted for the budget."""
    out, used, dropped = [], 0, 0
    for d in deltas:
        diff = d.get("diff")
        if diff and used < SLICE_DIFF_CAP:
            out.append(d)            # within budget: keep as-is (by reference)
            used += len(diff)
        elif diff:
            out.append({k: v for k, v in d.items() if k != "diff"})  # copy w/o diff
            dropped += 1
        else:
            out.append(d)
    return out, dropped


def slice_train(bundle, train_id):
    """Return a bounded, self-contained dict describing one train.

    All long text is truncated to SLICE_TEXT_CAP chars (with a '…[+N chars]'
    marker).  For each comment list (issue comments, PR conversation comments,
    PR review comments) at most SLICE_COMMENTS_KEPT bodies are kept; the count
    of dropped comments is recorded as '<key>_overflow' alongside the list key
    (e.g. 'comments' / 'comments_overflow').

    Shape::

        {
          "train":   { id, kind, outcome, significance, tier, effort,
                       code_areas, evidence },
          "issue":   { number, title, body*, url, labels, kind,
                       comments*:[body*], comments_overflow,
                       lifecycle*:[{event, actor, created_at, label}],
                       lifecycle_overflow, reopen_count } | None,
          "prs":     [ { number, title, body*, state, merged, created_at,
                         merged_at, url, reviewers:[login], review_decision,
                         review_comments*:[body*], review_comments_overflow,
                         comments*:[body*], comments_overflow,
                         review_rounds:{count, states} | None,
                         reviews*:[{author, state, submitted_at, body*}],
                         reviews_overflow,
                         lifecycle*:[{event, actor, created_at, label}],
                         lifecycle_overflow, reopen_count } ],
          "commits": [ { sha, message*, author, date } ],
          "feature_deltas": [ ... only this train's deltas; file-level deltas may carry
                               a bounded `diff`. The combined diff chars are capped to
                               SLICE_DIFF_CAP: once spent, later deltas have `diff`
                               OMITTED (the delta is still listed) ... ],
          "feature_deltas_diff_overflow": <# deltas whose diff was omitted for the cap>,
          "symbol_moves":   [ ... only moves whose from/to artifact id is
                               referenced by this train's feature_deltas ... ]
        }

    Raises KeyError when train_id is not found.  Does NOT mutate the bundle.
    The `train` block's effort/evidence/code_areas and the feature_deltas /
    symbol_moves lists are taken by reference from the bundle, so callers must
    treat the returned slice as read-only and not mutate it in place.
    """
    # --- locate the train ---------------------------------------------------
    trains_by_id = {t["id"]: t for t in bundle.get("trains", [])}
    if train_id not in trains_by_id:
        raise KeyError(f"train_id {train_id!r} not found in bundle")
    train = trains_by_id[train_id]

    # --- train block (copy listed fields only) ------------------------------
    train_block = {
        "id":           train.get("id"),
        "kind":         train.get("kind"),
        "outcome":      train.get("outcome"),
        "significance": train.get("significance"),
        "tier":         train.get("tier"),
        "effort":       train.get("effort"),
        "code_areas":   train.get("code_areas", []),
        "evidence":     train.get("evidence", []),
    }

    # --- resolve issue ------------------------------------------------------
    root_num = train.get("root_issue")
    issues_by_num = {i["number"]: i for i in bundle.get("issues", [])}
    raw_issue = issues_by_num.get(root_num) if root_num is not None else None
    if raw_issue is None:
        issue_block = None
    else:
        comments, comments_overflow = _cap_comments(raw_issue.get("comments_list", []))
        issue_lifecycle, issue_lifecycle_overflow = _cap_lifecycle(raw_issue.get("lifecycle", []))
        issue_block = {
            "number":           raw_issue.get("number"),
            "title":            raw_issue.get("title"),
            "body":             _cap_text(raw_issue.get("body") or ""),
            "url":              raw_issue.get("url"),
            "labels":           raw_issue.get("labels", []),
            "kind":             raw_issue.get("kind"),
            "comments":         comments,
            "comments_overflow": comments_overflow,
            "lifecycle":        issue_lifecycle,
            "lifecycle_overflow": issue_lifecycle_overflow,
            "reopen_count":     raw_issue.get("reopen_count", 0),
        }

    # --- resolve PRs --------------------------------------------------------
    prs_by_num = {p["number"]: p for p in bundle.get("prs", [])}
    pr_blocks = []
    for pr_num in train.get("prs", []):
        raw_pr = prs_by_num.get(pr_num)
        if raw_pr is None:
            continue
        rev_comments, rev_overflow = _cap_comments(raw_pr.get("review_comments", []))
        conv_comments, conv_overflow = _cap_comments(raw_pr.get("comments_list", []))
        reviews, reviews_overflow = _cap_reviews(raw_pr.get("reviews", []))
        pr_lifecycle, pr_lifecycle_overflow = _cap_lifecycle(raw_pr.get("lifecycle", []))
        raw_rounds = raw_pr.get("review_rounds")
        review_rounds = dict(raw_rounds) if raw_rounds else None
        if review_rounds is not None and "states" in review_rounds:
            review_rounds["states"] = list(review_rounds["states"])
        pr_blocks.append({
            "number":                 raw_pr.get("number"),
            "title":                  raw_pr.get("title"),
            "body":                   _cap_text(raw_pr.get("body") or ""),
            "state":                  raw_pr.get("state"),
            "merged":                 raw_pr.get("merged"),
            "created_at":             raw_pr.get("created_at"),
            "merged_at":              raw_pr.get("merged_at"),
            "url":                    raw_pr.get("url"),
            "reviewers":              list(raw_pr.get("reviewers") or []),
            "review_decision":        raw_pr.get("review_decision"),
            "review_comments":        rev_comments,
            "review_comments_overflow": rev_overflow,
            "comments":               conv_comments,
            "comments_overflow":      conv_overflow,
            "review_rounds":          review_rounds,
            "reviews":                reviews,
            "reviews_overflow":       reviews_overflow,
            "lifecycle":              pr_lifecycle,
            "lifecycle_overflow":     pr_lifecycle_overflow,
            "reopen_count":           raw_pr.get("reopen_count", 0),
        })

    # --- resolve commits ----------------------------------------------------
    commits_by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    commit_blocks = []
    for sha in train.get("commits", []):
        raw_c = commits_by_sha.get(sha)
        if raw_c is None:
            continue
        commit_blocks.append({
            "sha":     raw_c.get("sha"),
            "message": _cap_text(raw_c.get("message") or ""),
            "author":  raw_c.get("author"),
            "date":    raw_c.get("date"),
        })

    # --- feature_deltas filtered to this train ------------------------------
    own_deltas = [d for d in bundle.get("feature_deltas", [])
                  if d.get("train") == train_id]

    # --- symbol_moves filtered to this train's artifact ids -----------------
    # Build the set of artifact ids referenced by this train's feature_deltas.
    # Each link has 'from' and 'to' fields carrying artifact ids (set by
    # link_symbol_identity).  Keep a link when either endpoint is in the set.
    own_artifact_ids = {d.get("artifact") for d in own_deltas if d.get("artifact")}
    own_moves = [
        lnk for lnk in bundle.get("symbol_moves", {}).get("links", [])
        if lnk.get("from") in own_artifact_ids or lnk.get("to") in own_artifact_ids
    ]

    # Phase 10 slice-diffs: bound the combined file-diff chars carried for this train.
    own_deltas, diff_overflow = _cap_train_diffs(own_deltas)

    return {
        "train":          train_block,
        "issue":          issue_block,
        "prs":            pr_blocks,
        "commits":        commit_blocks,
        "feature_deltas": own_deltas,
        "feature_deltas_diff_overflow": diff_overflow,
        "symbol_moves":   own_moves,
    }


def build_forecast(bundle):
    """Compute the next-release forecast over bundle['buckets']['next_candidates'].

    Sets bundle['forecast'] = {next_milestone, candidates}.  Each candidate
    carries a ref (type/id/url), train id, weighted score, tier, and signals.
    Pure aside from writing forecast onto the bundle; reads buckets, milestones,
    prs, issues, and meta.  Forward-only: no predicted-vs-landed comparison.
    """
    meta        = bundle.get("meta", {})
    # Production bundles carry meta.period; fall back to meta.from/to so bundles
    # built via build_bundle (no period key) still window `recent_activity`
    # correctly instead of _in_window treating every item as in-window.
    period      = meta.get("period") or {"from": meta.get("from"), "to": meta.get("to")}
    ref_date    = meta.get("ref_date") or meta.get("to") or ""

    # Resolve next milestone title via the shared helper.
    _cur_ms, next_ms = select_milestones(bundle.get("milestones", []), ref_date)
    next_title = next_ms["title"] if next_ms else None

    # Build lookup indexes for issues and PRs.
    issue_idx = {i["number"]: i for i in bundle.get("issues", [])}
    pr_idx    = {p["number"]: p for p in bundle.get("prs", [])}

    # Index open PRs that reference each issue number (via closes / crossref_issues).
    # Used for the in_motion signal on issue candidates.
    open_pr_for_issue: dict = {}   # issue_number -> True  (any open PR references it)
    for pr in bundle.get("prs", []):
        if pr.get("state") == "open" and not pr.get("merged"):
            for n in list(pr.get("closes") or []) + list(pr.get("crossref_issues") or []):
                open_pr_for_issue[n] = True

    def _tier(score):
        if score >= FORECAST_TIER_LIKELY_THRESHOLD:
            return "likely"
        if score >= FORECAST_TIER_POSSIBLE_THRESHOLD:
            return "possible"
        return "longshot"

    def _score_candidate(nc_ref):
        """Return (score, signals) for one next_candidate ref."""
        type_ = nc_ref.get("type")
        id_   = nc_ref.get("id")
        item  = (issue_idx if type_ == "issue" else pr_idx).get(id_, {})
        train_id = nc_ref.get("train")

        # Age in days from created_at to ref_date (for overdue signal).
        created_ts = item.get("created_at", "")
        created_dt = _parse_ts(created_ts)
        # ref_date is normally YYYY-MM-DD; slice to the date so a full ISO
        # timestamp can't produce a doubled "...ZT00:00:00Z" that fails to parse.
        ref_dt     = _parse_ts(ref_date[:10] + "T00:00:00Z") if ref_date else None
        age_days   = (ref_dt - created_dt).days if (created_dt and ref_dt) else None

        # Determine in_motion:
        #   - ref is a PR candidate (open PR is a next_candidate by definition)
        #   - OR some open PR in the bundle references this issue
        #   - OR the ref carries a train id
        if type_ == "pr" and item.get("state") == "open" and not item.get("merged"):
            in_motion = True
            motion_text = "open PR"
        elif open_pr_for_issue.get(id_):
            in_motion = True
            motion_text = "open PR"
        elif train_id:
            in_motion = True
            motion_text = "work in progress"
        else:
            in_motion = False
            motion_text = ""

        # Ordered list of (present_bool, weight_key, signal_text).
        checks = [
            (next_title is not None and item.get("milestone") == next_title,
             "on_next_milestone", f"on milestone {next_title}"),
            (_high_priority(item),
             "high_priority", "high-priority"),
            (in_motion,
             "in_motion", motion_text),
            (_in_window(item.get("updated_at"), period),
             "recent_activity", "active in window"),
            (age_days is not None and age_days >= FORECAST_OVERDUE_DAYS,
             "overdue", "long-open"),
        ]

        score   = 0.0
        signals = []
        for present, key, text in checks:
            if present:
                score += FORECAST_WEIGHTS[key]
                if text:
                    signals.append(text)
        return score, signals

    candidates = []
    for nc in bundle.get("buckets", {}).get("next_candidates", []):
        score, signals = _score_candidate(nc)
        candidates.append({
            "ref":   {"type": nc["type"], "id": nc["id"], "url": nc["url"]},
            "train": nc.get("train"),
            "score": score,
            "tier":  _tier(score),
            "signals": signals,
        })

    # Sort: score descending, then ref id ascending for determinism on ties.
    candidates.sort(key=lambda c: (-c["score"], c["ref"]["id"]))

    bundle["forecast"] = {
        "next_milestone": next_title,
        "candidates":     candidates,
    }
    return bundle


def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets,
    the window narrative projections (timeline/feature_deltas) and Phase 3b
    code-area attribution (code_area/area/modules). Label facets and `kind` are
    stamped in gather.py's acquire(), not here.

    Slice 7b-2: `artifacts` and `people` are NO LONGER derived here. fold_bundle
    materializes them into the store and extract reads them back, so enrich now
    CONSUMES them (build_timeline / attribute_code_areas read `artifacts`). The
    setdefaults below keep enrich total for callers that hand it a bundle without
    those keys (e.g. a raw fixture fed directly, not through extract): enrich
    never KeyErrors and never recomputes them."""
    bundle.setdefault("artifacts", {})
    bundle.setdefault("people", {})
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    link_symbol_identity(bundle)   # Phase 3e: window-wide symbol moves over the ledger
    # build_timeline reads bundle["artifacts"] (supplied by extract / setdefault).
    bundle["timeline"] = build_timeline(bundle)
    # compute_feature_deltas depends on build_trains having run (resolves trains).
    bundle["feature_deltas"] = compute_feature_deltas(bundle)
    # Phase 3b: attribute code areas everywhere the schema reserved a null.
    idx = attribute_code_areas(bundle)
    attribute_train_areas(bundle, idx)
    score_train_significance(bundle)   # Phase 4a: reads code_areas populated above
    annotate_train_effort(bundle)      # Phase 4a: per-train time & effort metrics
    # Phase 4a: next-release forecast (needs buckets + milestones, placed after both).
    build_forecast(bundle)
    build_modules(bundle, idx)
    # Phase 10 slice 1: review-round / reopen texture, derived from the
    # reviews/lifecycle that gather set (or extract resurfaces). Read-side derived
    # facts live here with forecast/modules — NOT in extract (which stays raw).
    annotate_review_rounds(bundle)
    annotate_reopen_count(bundle)
    return bundle


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Accept ONLY the documented shapes: `BUNDLE` or `BUNDLE --slice TRAIN_ID`.
    # `--slice` is recognized strictly as the second positional, and any other
    # argv shape is rejected (no flag-anywhere / unknown-arg surprises).
    slice_id = None
    if len(argv) == 1:
        path = argv[0]
    elif len(argv) == 3 and argv[1] == "--slice":
        path, slice_id = argv[0], argv[2]
    else:
        sys.stderr.write("usage: link.py BUNDLE.json [--slice TRAIN_ID]\n")
        raise SystemExit(2)
    with open(path, encoding="utf-8") as fh:
        bundle = json.load(fh)
    enrich(bundle)
    if slice_id is not None:
        # Phase 4b: emit ONE train's bounded, self-contained slice as JSON for a
        # narrator sub-agent. Read-only — the bundle file is NOT rewritten.
        try:
            sliced = slice_train(bundle, slice_id)
        except KeyError:
            sys.stderr.write(f"link.py --slice: train {slice_id!r} not found\n")
            raise SystemExit(2)
        json.dump(sliced, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return slice_id
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(
        f"linked {len(bundle['trains'])} trains, "
        f"{len(bundle['buckets']['shipped'])} shipped into {path}\n"
    )
    return path


if __name__ == "__main__":
    main()
