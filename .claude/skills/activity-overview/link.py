"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import os
import re
import sys
import types
from datetime import datetime, timezone

import graphstore
import gather  # for resolve_commit_pr / attach_commit_prs (commit->PR resolution)
import series  # Phase 13: series continuity (installment_snapshot / compute_series)

# Phase 7b-1: the pure derivations now live in `derive` (a leaf module that does
# NOT import link or gather), so the store write-path can derive these without a
# link dependency. They are re-imported here so link's public API and `enrich`
# are byte-identical to before — `link.enrich` calls the same functions, now
# resolving to the names imported from derive (one-way: link -> derive).
import derive
import people as people_mod  # registry context (link --people)
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

# Label patterns that also mark an item high priority (#97 final gate):
# regular expressions searched case-insensitively in each label, so a repo's
# own wording ("Needs: Immediate Attention :bangbang:", "Priority: P1",
# "Severity: Critical", "blocker") is recognised without exact names. An
# operator adds patterns with `link.py BUNDLE --priority-label PATTERN`
# (repeatable); they are recorded in `forecast.priority_patterns`.
PRIORITY_LABEL_PATTERNS = (
    r"priority\W*(?:high|critical|urgent|p0|p1)\b",
    r"high\W?priority",
    r"(?<![a-z0-9])p[01](?![a-z0-9])",
    r"\burgent\b",
    r"(?<![\w-])critical\b",
    r"\bblocker\b",
)


def priority_matchers(extra=()):
    """The compiled priority-label patterns: PRIORITY_LABEL_PATTERNS plus the
    operator's `extra` ones. Raises re.error on an invalid pattern."""
    return tuple(re.compile(p, re.IGNORECASE)
                 for p in tuple(PRIORITY_LABEL_PATTERNS) + tuple(extra or ()))


_DEFAULT_PRIORITY = None


def _default_priority():
    global _DEFAULT_PRIORITY
    if _DEFAULT_PRIORITY is None:
        _DEFAULT_PRIORITY = priority_matchers()
    return _DEFAULT_PRIORITY


def _in_window(ts, period):
    """True if `ts` (ISO) falls in `period`. Permissive when either is missing,
    so dateless fixtures (and pre-window-free bundles) classify as in-window."""
    if not period or not ts:
        return True
    # Granularity-safe (issue #52): an ISO `from`/`to` no longer drops the
    # first day; a date-only ts or bound covers its whole day.
    return graphstore.ts_in_window(ts, period.get("from"), period.get("to"))


def window_period(meta):
    """The window a bundle's buckets/forecast are limited to (issue #75):
    `meta.period`, else `{from, to}`. Shared by compute_buckets and
    build_forecast so the two never disagree. With neither set, both bounds are
    None and `_in_window` stays permissive (dateless fixtures)."""
    return meta.get("period") or {"from": meta.get("from"), "to": meta.get("to")}


def _high_priority(item, matchers=None):
    """True when a label of `item` is in HIGH_PRIORITY_LABELS (exact, case-
    insensitive) or matches one of `matchers` (priority_matchers(); the
    defaults when None)."""
    matchers = _default_priority() if matchers is None else matchers
    for lbl in item.get("labels") or []:
        lbl = lbl or ""
        if lbl.lower() in HIGH_PRIORITY_LABELS \
                or any(m.search(lbl) for m in matchers):
            return True
    return False


def approved_pr(pr):
    """True for a "likely to land" PR (#97 final gate): open, unmerged, not a
    draft (`draft` true), with an `approved` review by someone other than its
    author and no `changes_requested` review submitted after the latest such
    approval, as the view's `reviews` record them (a review later dismissed
    reads `dismissed`, so it does not count). A PR without `reviews` is not
    approved here: `review_decision` is today's, not the window's. A PR
    whose `review_state` (story-layer views) is `approved_stale` — the
    author changed it after the approval — is not ready either."""
    if pr.get("state") != "open" or pr.get("merged") or pr.get("draft") is True:
        return False
    if (pr.get("review_state") or {}).get("state") in ("approved_stale",
                                                       "changes_requested"):
        return False             # approved before the author's latest change
    author = pr.get("author")
    last_approval = None
    for r in pr.get("reviews") or []:
        if not isinstance(r, dict) or not r.get("submitted_at"):
            continue
        if (r.get("state") or "").lower() == "approved" and r.get("author") \
                and r.get("author") != author:
            if last_approval is None or r["submitted_at"] > last_approval:
                last_approval = r["submitted_at"]
    if last_approval is None:
        return False
    return not any(
        isinstance(r, dict) and (r.get("state") or "").lower() == "changes_requested"
        and (r.get("submitted_at") or "") > last_approval
        for r in pr.get("reviews") or [])


def _ms_sort_key(m):
    # Only called on DATED milestones (select_milestones drops undated ones), so
    # the due day is always a real date. `or 0` (not a default arg) guarantees an
    # int secondary key even when `number` is present-but-null; real GitHub
    # milestone numbers are >= 1, so collapsing a hypothetical 0 to 0 is harmless.
    return (m["due_on"][:10], m.get("number") or 0)


def select_milestones(milestones, ref_date):
    """(current, next) open, DATED milestones relative to `ref_date` (#59).

    Undated open milestones (e.g. a "Backlog" bucket) are never selected; if no
    open milestone has a due date the result is (None, None). Dated ones are
    ordered by (due day, number). Comparison is by calendar day, so `ref_date`
    may be a date or a full ISO timestamp.
      - Some due on/after ref day: current = the earliest of those, next = the
        one after it (None if none).
      - All overdue: current = the most recently due (lowest number on a tie),
        next = None.
    """
    dated = sorted((m for m in milestones or []
                    if m.get("state") == "open" and m.get("due_on")),
                   key=_ms_sort_key)
    if not dated:
        return None, None
    ref = (ref_date or "")[:10]
    upcoming = [m for m in dated if m["due_on"][:10] >= ref]
    if upcoming:
        return upcoming[0], (upcoming[1] if len(upcoming) > 1 else None)
    latest_day = dated[-1]["due_on"][:10]
    current = next(m for m in dated if m["due_on"][:10] == latest_day)
    return current, None


def select_sprints(sprints, ref_date):
    """(previous, current, next) iterations relative to `ref_date` (Phase 12).

    `sprints` is the `{iteration_id: {title, start, end}}` map. Returns three
    `{id, title, start, end}` dicts (or None): `current` is the iteration whose
    inclusive [start, end] contains `ref_date`; if none does, the latest iteration
    that started on/before `ref_date` (else the earliest). `previous`/`next` are the
    iterations adjacent to `current` by start date. Empty map -> (None, None, None).
    Pure — dormant until a board defines an iteration field (status-only boards yield
    no sprints), but ready for sprint framing when one does."""
    ordered = sorted(
        ({"id": sid, **(s or {})} for sid, s in (sprints or {}).items()),
        key=lambda s: ((s.get("start") or "9999-12-31")[:10], s["id"]))
    if not ordered:
        return None, None, None
    ref = (ref_date or "")[:10]
    current = next(
        (s for s in ordered
         if (s.get("start") or "")[:10] <= ref <= (s.get("end") or "9999-12-31")[:10]
         and s.get("start")),
        None)
    if current is None:
        # latest iteration with a REAL start on/before ref (a start-less sprint must
        # never win the fallback — it sorts last but has no date to compare).
        started = [s for s in ordered
                   if s.get("start") and s["start"][:10] <= ref]
        current = started[-1] if started else ordered[0]
    idx = ordered.index(current)
    prev = ordered[idx - 1] if idx > 0 else None
    nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
    return prev, current, nxt


def train_index(trains):
    """Map ('pr'|'issue', number) -> train id, for cross-linking bucket refs.
    Every issue a train lists in `issues` (issue #93) is indexed; a train
    without that key (an older bundle) indexes its `root_issue`."""
    idx = {}
    for t in trains:
        issues = t.get("issues")
        if issues is None:
            issues = [t["root_issue"]] if t.get("root_issue") is not None else []
        for n in issues:
            idx[("issue", n)] = t["id"]
        for n in t.get("prs", []):
            idx[("pr", n)] = t["id"]
    return idx


def build_timeline(bundle):
    """Merge social + code events into one chronological event stream.

    Event shape: {ts, actor, layer:'social'|'code', event, ref:{type,...,url},
    subject:{kind,name,path}}. Social events come from PR/issue comments + review
    comments; code events from artifact lifecycle entries, stamped with the
    commit's `committed_at` when the bundle's commits carry it. Malformed records
    that lack created_at fall back to URL ordering as a last resort (not a normal
    case — well-formed comment objects carry created_at). Pure.

    Order (#100) is total and independent of the key order of any dict in the
    bundle: (ts, ref.url, layer — social before code —, then for a code event
    its artifact id and its position in that artifact's lifecycle). Social
    events with an equal (ts, url) keep the order of the `prs` / `issues` /
    comment lists they come from (lists, so that order is data). The insertion
    order of `artifacts` no longer breaks ties.
    """
    keyed = []   # (sort key, event)

    def social(actor, event, ref_type, number, url, subject, ts):
        ev = {
            "ts": ts or "", "actor": actor, "layer": "social", "event": event,
            # `id` (not `number`) to match the bundle-wide ref convention
            # {type, id, url} used everywhere else (and the gate's well_formed).
            "ref": {"type": ref_type, "id": number, "url": url},
            "subject": subject,
        }
        keyed.append(((ev["ts"], str(url or ""), 0, "", len(keyed)), ev))

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

    # A code event's ts is its commit's full committer timestamp (`committed_at`,
    # UTC, issue #82) so it interleaves with same-day social events by real
    # time. Commits gathered before #82 carry none: the event keeps the bare day,
    # which sorts before that day's timestamped social events.
    committed_at = {c.get("sha"): c.get("committed_at")
                    for c in bundle.get("commits") or [] if c.get("committed_at")}
    for aid, art in bundle.get("artifacts", {}).items():
        for i, ev in enumerate(art.get("lifecycle", [])):
            out = {
                "ts": committed_at.get(ev.get("commit")) or ev.get("date") or "",
                "actor": ev.get("author"),
                "layer": "code", "event": ev["event"], "ref": ev["ref"],
                "subject": {"kind": art["kind"], "name": art["name"],
                            "path": art["path"]},
            }
            keyed.append(((out["ts"], str(ev["ref"].get("url") or ""), 1,
                           str(aid), i), out))

    # No two keys are equal: artifact ids are unique and `i` is unique within an
    # artifact; a social key ends in its own position in `keyed`.
    keyed.sort(key=lambda ke: ke[0])
    return [ev for _key, ev in keyed]


_EVENT_TO_DELTA = {"add": "add", "remove": "drop", "change": "change"}


def compute_feature_deltas(bundle):
    """Project the artifacts ledger into the feature_deltas view.

    One delta per lifecycle event: add->add, remove->drop, change->change. Each
    attributes author/commit/url + (best-effort) the owning pr/train via the
    commit->PR map Link already builds. For SYMBOL/COMMENT artifacts (Phase 3d)
    `before`/`after` carry the bounded hunk snippet and `detail` is
    "<lang> <subkind> <name>"; file-level deltas leave them null. `area` is filled
    later by attribute_code_areas. Pure.

    Order (#100): by the event's time (its commit's `committed_at`, else its
    `date`, as build_timeline stamps it), then artifact id, then the event's
    position in that artifact's lifecycle — total, and independent of the key
    order of `artifacts`.
    """
    commit_to_pr = {c["sha"]: c.get("pr") for c in bundle.get("commits", [])}
    times = derive.commit_times(bundle)
    pr_to_train = {}
    for t in bundle.get("trains", []):
        for n in t.get("prs", []):
            pr_to_train[n] = t["id"]

    keyed = []   # (sort key, delta)
    for aid, art in bundle.get("artifacts", {}).items():
        for i, ev in enumerate(art.get("lifecycle", [])):
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
            ts = times.get(ev.get("commit")) or ev.get("date") or ""
            keyed.append(((ts, str(aid), i), delta))
    keyed.sort(key=lambda kd: kd[0])
    return [d for _key, d in keyed]


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


def _pr_outcome(pr, base_branch):
    """A mainline-targeting PR's own outcome in its train: "merged" (into the
    mainline), "open", "rejected" (closed unmerged), or None when the PR does
    not target the mainline (a stacked / release-branch PR) or its state is
    unknown. Pure."""
    if not _targets_mainline(pr, base_branch):
        return None
    if pr.get("merged"):
        return "merged"
    if pr.get("state") == "open":
        return "open"
    if pr.get("state") == "closed":
        return "rejected"
    return None


# A train's outcome from its PRs' own outcomes, by precedence (issue #93).
_TRAIN_OUTCOME_BY_PR = (("merged", "shipped"), ("open", "in_flight"),
                        ("rejected", "rejected"))


def _slug_of_url(url):
    """'owner/repo' (lower-case) of a github.com issue/PR url, else None."""
    parts = (url or "").split("/")
    if len(parts) >= 5 and parts[2].lower() == "github.com":
        return "{}/{}".format(parts[3], parts[4]).lower()
    return None


def _stacked_parent(pr, candidates):
    """The mainline-targeting PR whose head branch `pr` merged into, or None.

    `candidates` are the member PRs whose `head` equals `pr["base"]`. A PR's
    base branch lives in the repo it was opened in (read from its url), so the
    parent is the candidate whose `head_repo` is that repo; a candidate whose
    head lives in another repo (a fork that happens to use the same branch
    name, e.g. `patch-1`) is never the parent. A candidate with no
    `head_repo` (older stores, a deleted fork) is used only when no candidate
    matches by repo and it is the only one; more than one such candidate is
    ambiguous and the PR is attached to none (issue #93)."""
    base_repo = _slug_of_url(pr.get("url"))
    exact, unknown = [], []
    for p in candidates:
        if p["number"] == pr["number"]:
            continue
        head_repo = (p.get("head_repo") or "").lower() or None
        if head_repo is None or base_repo is None:
            unknown.append(p)
        elif head_repo == base_repo:
            exact.append(p)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(unknown) == 1:
        return unknown[0]
    return None


def build_trains(bundle):
    """Group PRs (+ their commits + the issues they close) into decision trains.

    Membership (issue #93):
      - Every PR that targets the analyzed mainline (merged into it, open, or
        closed unmerged) is a member of exactly one train, whatever its own
        outcome; that outcome is kept in `pr_outcomes` ("merged" / "open" /
        "rejected", keyed by the PR number as a string).
      - PRs are joined by the issues they CLOSE: a train is a connected group
        of member PRs and the issues they close, so two PRs closing the same
        issue are one train, and every issue a member PR closes is listed in
        `issues` (sorted) and indexed to that train. Only a PR that is merged
        or still open joins and anchors this way (#97 final gate): a closing
        reference takes effect only when its PR merges. A closed-unmerged
        (`rejected`) PR that closes an issue of such a train is listed in that
        train as a rejected attempt (`prs`, `pr_outcomes`, its commits and its
        reviewers / participants in `effort`) but adds none of its other
        issues and never moves the train's root issue, `issues`, kind,
        outcome, `effort.opened_at` or `elapsed_days`. When its issues touch
        several such trains it joins the one holding the lowest of those
        issue numbers. Rejected PRs whose issues are in no merged/open train
        join each other over their issues, as before, into rejected trains.
        A plain mention / timeline
        cross-reference (`crossref_issues`) makes an issue neither a member
        nor a root. A `closes` number that is a PR of the bundle is not an
        issue (GitHub does not close a PR by keyword; issues and PRs share one
        number sequence): it neither anchors a train nor joins one, and the
        two PRs stay in their own trains (#110 item 7).
      - Anchor: the lowest closed issue number (`root_issue`, id
        `train-issue-<n>`), else the lowest PR number (`train-pr-<n>`).
    `outcome`, by precedence: `shipped` (any member PR merged into the
    mainline) > `in_flight` (any open) > `rejected` (only closed-unmerged) >
    `abandoned` (an issue closed `not_planned` that no member PR closes).
    A PR merged into ANOTHER PR's branch (its base == that PR's head, in the
    same repo) is a stacked contribution: a `contributing_prs` entry on the
    parent PR's train — the journey-to-main context — not a member and not
    shipped-to-main.
    """
    commits_by_pr = {}
    for c in bundle["commits"]:
        commits_by_pr.setdefault(c.get("pr"), []).append(c["sha"])
    issues_by_num = {i["number"]: i for i in bundle["issues"]}
    base_branch = bundle.get("meta", {}).get("base_branch")
    prs_all = bundle["prs"]
    pr_numbers = {p["number"] for p in prs_all}

    def _closes(pr):
        # the issues `pr` closes: a number that is a PR is ignored (#110 item 7)
        return [n for n in pr.get("closes") or [] if n not in pr_numbers]

    def _kind_for(issue_nums, prs):
        # The first typed issue (lowest number first), else the PRs'
        # conventional-commit titles (feat->feature, fix->bug, docs->docs;
        # lowest # wins), else "other".
        for n in issue_nums:
            kind = (issues_by_num.get(n) or {}).get("kind") or "other"
            if kind != "other":
                return kind
        for p in prs:
            pk = gather.classify_pr_kind(p)
            if pk:
                return pk
        return "other"

    def _make_train(issue_nums, prs, attempts=()):
        # `attempts`: rejected PRs attached to a merged/open train. They are
        # members (prs, pr_outcomes, commits, evidence) but not its kind.
        kind = _kind_for(sorted(set(issue_nums)), sorted(prs, key=lambda p: p["number"]))
        prs = sorted(list(prs) + list(attempts), key=lambda p: p["number"])
        issue_nums = sorted(set(issue_nums))
        pr_numbers = [p["number"] for p in prs]
        pr_outcomes = {str(p["number"]): _pr_outcome(p, base_branch) for p in prs}
        root_issue = issue_nums[0] if issue_nums else None
        evidence, shas = [], []
        for n in issue_nums:
            if n in issues_by_num:
                evidence.append(ref("issue", n, issues_by_num[n]["url"]))
        for p in prs:
            shas.extend(commits_by_pr.get(p["number"], []))
            evidence.append(ref("pr", p["number"], p["url"]))
        outcome = "abandoned"
        for pr_outcome, train_outcome in _TRAIN_OUTCOME_BY_PR:
            if pr_outcome in pr_outcomes.values():
                outcome = train_outcome
                break
        return {
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": kind,
            "root_issue": root_issue,
            "issues": issue_nums,
            "prs": pr_numbers,
            "pr_outcomes": pr_outcomes,
            "commits": sorted(shas),
            "code_areas": [],
            "outcome": outcome,
            "contributing_prs": [],
            "evidence": evidence,
        }

    # 1. Member PRs, joined over the issues they close: union-find over
    #    ("pr", n) / ("issue", n) nodes. Order-independent: each group is
    #    sorted when its train is made, and the trains are sorted by id.
    members = [p for p in prs_all if _pr_outcome(p, base_branch) is not None]
    parent = {}

    def _find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(prs):
        for pr in prs:
            node = _find(("pr", pr["number"]))
            for n in _closes(pr):
                parent[_find(("issue", n))] = node
                node = _find(node)

    # 1a. Merged / open PRs: their closing references join and anchor trains.
    landing = [p for p in members if _pr_outcome(p, base_branch) != "rejected"]
    rejected = [p for p in members if _pr_outcome(p, base_branch) == "rejected"]
    _union(landing)
    issue_group = {}   # issue number -> group root, for issues a landing PR closes
    for pr in landing:
        for n in _closes(pr):
            issue_group[n] = _find(("issue", n))

    # 1b. A rejected PR closing an issue of a landing train is an attempt in
    #     the train of its lowest such issue; the rest join among themselves.
    attempts_of, alone = {}, []
    for pr in rejected:
        hits = sorted(n for n in _closes(pr) if n in issue_group)
        if hits:
            attempts_of.setdefault(issue_group[hits[0]], []).append(pr)
        else:
            alone.append(pr)
    _union(alone)

    groups = {}
    for pr in landing + alone:
        g = groups.setdefault(_find(("pr", pr["number"])),
                              {"prs": [], "issues": set(), "attempts": []})
        g["prs"].append(pr)
        g["issues"].update(_closes(pr))
    for root, prs in attempts_of.items():
        groups[root]["attempts"].extend(prs)

    trains, train_of_pr, closed_by_member = [], {}, set()
    for g in groups.values():
        train = _make_train(g["issues"], g["prs"], g["attempts"])
        trains.append(train)
        closed_by_member |= g["issues"]
        for n in train["prs"]:
            train_of_pr[n] = train

    # 2. abandoned: an issue closed not-planned that no member PR closes.
    for iss in bundle["issues"]:
        if iss.get("state") == "closed" and iss.get("state_reason") == "not_planned" \
                and iss["number"] not in closed_by_member:
            trains.append(_make_train([iss["number"]], []))

    # 3. Attach stacked contributions: a PR merged into a NON-mainline branch
    #    that is some member PR's head (in the same repo) feeds that PR's train.
    heads = {}
    for p in members:
        if p.get("head"):
            heads.setdefault(p["head"], []).append(p)
    for pr in prs_all:
        if not (pr.get("merged") and base_branch and pr.get("base")
                and pr["base"] != base_branch):
            continue  # only stacked (merged into a known non-mainline branch)
        found = _stacked_parent(pr, heads.get(pr["base"], []))
        train = train_of_pr.get(found["number"]) if found is not None else None
        if train is not None and pr["number"] not in train["contributing_prs"]:
            train["contributing_prs"].append(pr["number"])
            train["contributing_prs"].sort()

    return sorted(trains, key=lambda t: t["id"])


def compute_buckets(bundle, matchers=None):
    """Full four-way bucketing: one bucket per item, precedence
    shipped > rejected > next_candidates > in_flight. Refs carry their train id.
    An open item is a next candidate when it is on the next milestone, has a
    priority label (`_high_priority` with `matchers`) or is an approved PR
    (`approved_pr`)."""
    meta = bundle.get("meta", {})
    period = window_period(meta)
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
            if on_next or _high_priority(item, matchers) \
                    or (type_ == "pr" and approved_pr(item)):
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

# Days threshold for the two train-effort flags (issue #112). An OPEN train
# (outcome `in_flight`) is `stalled` when it has had no activity for more than
# this many days as of the view's reference date; a SHIPPED train is
# `slow_to_land` when it took more than this many days from first open to last
# merge.  Tune without touching logic.
TRAIN_STALL_DAYS = 21

# ---------------------------------------------------------------------------
# Phase 4a: next-release forecast tunables
# ---------------------------------------------------------------------------

# Weight for each scoring signal. Heavier signals represent stronger evidence
# that an item will land in the next release:
#   on_next_milestone — explicitly scheduled; strongest signal (5.0)
#   high_priority     — labelled urgent by the team (3.0)
#   approved          — an open PR approved with no later change request
#                       (3.0; with in_motion it clears "likely": 5.0)
#   in_motion         — active work already underway (2.0)
#   recent_activity   — touched inside the window (1.5)
#   overdue           — long-open item, mild pressure to close (1.0)
FORECAST_WEIGHTS = {
    "on_next_milestone": 5.0,
    "high_priority":     3.0,
    "approved":          3.0,
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


# An `updated_at` this close to an automation account's comment or review is
# that automation's own update (its comment, then the label it adds).
_BOT_UPDATE_SLACK_SECONDS = 120


def _train_activity_times(train_prs, train_issues, train_commits):
    """Every activity timestamp the bundle records for a train's members (issue
    #112): each member PR's and issue's `created_at`, `updated_at`, `closed_at`,
    `merged_at`, its `comments_list[].created_at`, `review_comments[].created_at`,
    `reviews[].submitted_at` and `lifecycle[].created_at`, and each train commit's
    `committed_at` (else its `date`). Unparseable / missing values are skipped.

    Automation does not count (final gate, #97): a comment or review whose
    author is a bot (derive.is_bot_login) is not activity, and neither is an
    `updated_at` within _BOT_UPDATE_SLACK_SECONDS of one. A reminder bot that
    comments on every stale pull request would otherwise make none of them
    stalled."""
    out = []

    def add(ts):
        dt = _parse_ts(ts)
        if dt is not None:
            out.append(dt)

    for rec in list(train_prs) + list(train_issues):
        bot_times = []
        for key, ts_key in (("comments_list", "created_at"),
                            ("review_comments", "created_at"),
                            ("reviews", "submitted_at")):
            for row in rec.get(key) or []:
                if not isinstance(row, dict):
                    continue
                if derive.is_bot_login(row.get("author")):
                    dt = _parse_ts(row.get(ts_key))
                    if dt is not None:
                        bot_times.append(dt)
                else:
                    add(row.get(ts_key))
        for row in rec.get("lifecycle") or []:
            if isinstance(row, dict):
                add(row.get("created_at"))
        for key in ("created_at", "closed_at", "merged_at"):
            add(rec.get(key))
        updated = _parse_ts(rec.get("updated_at"))
        if updated is not None and not any(
                abs((updated - b).total_seconds()) <= _BOT_UPDATE_SLACK_SECONDS
                for b in bot_times):
            out.append(updated)
    for c in train_commits:
        add(c.get("committed_at") or c.get("date"))
    return out


def _effort_for_train(train, pr_index, issue_index, ref_date=None,
                      commit_index=None):
    """Compute the effort dict for one train.  Pure; never mutates arguments.

    `ref_date` (the view's `meta.ref_date`, else `meta.to`) frames `idle_days`;
    `commit_index` ({sha: commit}) lets a train's commits count as activity.
    `opened_at` is the earliest `created_at` of the train's issues and of its
    merged / open PRs (a rejected attempt beside them does not date it; a
    train of rejected PRs only is dated by them). Reviewers, participants and
    commits count every PR of the train, attempts included (issue #93).
    Flags (issue #112): `stalled` only for an OPEN train (outcome `in_flight`)
    idle for more than TRAIN_STALL_DAYS days at the reference date;
    `slow_to_land` only for a SHIPPED train whose `elapsed_days` exceeds
    TRAIN_STALL_DAYS. A rejected or abandoned train has neither."""
    pr_nums = train.get("prs", [])
    train_prs = [pr_index[n] for n in pr_nums if n in pr_index]

    # Every issue of the train (issue #93); `root_issue` only on older trains.
    issue_nums = train.get("issues")
    if issue_nums is None:
        root_num = train.get("root_issue")
        issue_nums = [root_num] if root_num is not None else []
    train_issues = [issue_index[n] for n in issue_nums if n in issue_index]

    # ---- dates --------------------------------------------------------
    # A rejected attempt in a train that also holds a merged / open PR does
    # not date the train (#97 final gate): its time to land runs from the
    # train's issues and its merged / open PRs. A train of rejected PRs only
    # is dated by them.
    outcomes = train.get("pr_outcomes") or {}
    dating_prs = [p for p in train_prs
                  if outcomes.get(str(p.get("number"))) != "rejected"]
    if not dating_prs:
        dating_prs = train_prs
    candidates = [d for d in
                  (_parse_ts(x.get("created_at")) for x in train_issues + dating_prs)
                  if d is not None]
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
    # issue authors
    for i in train_issues:
        add_login(i.get("author"))
    # all reviewers
    participants |= all_reviewers
    # PR comment authors
    for p in train_prs:
        for c in p.get("comments_list", []):
            add_login(c.get("author"))
    # issue comment authors
    for i in train_issues:
        for c in i.get("comments_list", []):
            add_login(c.get("author"))

    # ---- outcome (a train built by build_trains always carries it) -----
    outcome = train.get("outcome")
    if outcome is None:
        if merged_dt is not None:
            outcome = "shipped"
        elif any(p.get("state") == "open" for p in train_prs):
            outcome = "in_flight"

    # ---- slow_to_land (shipped) / stalled + idle_days (open) ------------
    slow_to_land = (outcome == "shipped" and elapsed_days is not None
                    and elapsed_days > TRAIN_STALL_DAYS)
    last_activity_at = idle_days = None
    ref_dt = _parse_ts((ref_date or "")[:10] + "T00:00:00Z") if ref_date else None
    if outcome == "in_flight":
        commits_of = commit_index or {}
        times = _train_activity_times(
            train_prs, train_issues,
            [commits_of[s] for s in train.get("commits", []) if s in commits_of])
        if times:
            last_dt = max(times)
            last_activity_at = last_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if ref_dt is not None:
                # calendar days; activity recorded after the reference date (a
                # `--ref-date` before the window's end) means not idle. A report
                # view's records are as of the window's end (extract.record_as_of,
                # issue #97), so later activity is not in them.
                idle_days = max(0, (ref_dt.date() - last_dt.date()).days)
    stalled = idle_days is not None and idle_days > TRAIN_STALL_DAYS

    return {
        "opened_at": opened_at,
        "merged_at": merged_at,
        "elapsed_days": elapsed_days,
        "reviewers": len(all_reviewers),
        "review_comments": review_comments,
        "commits": commits,
        "participants": len(participants),
        "stalled": stalled,
        "idle_days": idle_days,
        "last_activity_at": last_activity_at,
        "slow_to_land": slow_to_land,
    }


def annotate_train_effort(bundle):
    """Annotate each train in bundle['trains'] with an `effort` dict.

    Computes time-and-effort signals purely from bundle data (prs + issues +
    commits), with `idle_days` measured to the reference date (`meta.ref_date`,
    else `meta.to`). Mutates trains in place; returns bundle for convenience."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    commit_index = {c["sha"]: c for c in bundle.get("commits", []) if c.get("sha")}
    meta = bundle.get("meta") or {}
    ref_date = meta.get("ref_date") or meta.get("to") or None
    for train in bundle.get("trains", []):
        train["effort"] = _effort_for_train(train, pr_index, issue_index,
                                            ref_date, commit_index)
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

    Walks the deltas in order and keeps a delta's `diff` only if it fits in what is
    left of SLICE_DIFF_CAP (`used + len(diff) <= SLICE_DIFF_CAP`), so the kept total
    never exceeds the cap (issue #82: the check used to run before adding, letting
    the diff that crossed the cap through). A diff that does not fit is stripped
    (copying only that delta, so the bundle is never mutated — the slice is
    read-only by contract); a later, smaller diff that still fits is kept. Returns
    (deltas_out, dropped_count) where dropped_count is how many deltas had their
    diff omitted for the budget."""
    out, used, dropped = [], 0, 0
    for d in deltas:
        diff = d.get("diff")
        if diff and used + len(diff) <= SLICE_DIFF_CAP:
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
                       code_areas, evidence, issues, pr_outcomes },
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
          "commits": [ { sha, message*, author, date, url } ],
          "feature_deltas": [ ... only this train's deltas; file-level deltas may carry
                               a bounded `diff`. The combined diff chars are capped to
                               SLICE_DIFF_CAP: a diff that does not fit in what is
                               left is OMITTED (the delta is still listed) ... ],
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
        "issues":       train.get("issues", []),
        "pr_outcomes":  train.get("pr_outcomes", {}),
    }
    # The story layer's keys, only when the train carries them.
    for key in ("discussion", "story", "story_rank", "story_score",
                "story_reasons"):
        if key in train:
            train_block[key] = train[key]

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
            # issue #112: a citable commit url, built like feature_deltas[].url
            "url":     derive._commit_url(bundle, raw_c.get("sha")),
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
        # The story material: the whole conversation as one timeline.
        "conversation":   story_material(bundle, train),
        **slice_participants(bundle, train),
    }


def _empty_forecast_notes(bundle, cur_ms, next_ms, extra_patterns):
    """Why the forecast has no candidate (#97 final gate): one plain sentence
    per missing signal source. Only called when the view holds open items."""
    notes = []
    open_ms = [m for m in bundle.get("milestones") or [] if m.get("state") == "open"]
    if next_ms is not None:
        notes.append("no open item is on the next milestone {!r}".format(
            next_ms.get("title")))
    elif not open_ms:
        notes.append("no open milestone, so no next milestone")
    elif not any(m.get("due_on") for m in open_ms):
        notes.append("no open milestone has a due date ({}), so none is the next "
                     "milestone".format(", ".join(repr(m.get("title"))
                                                  for m in open_ms)))
    elif cur_ms is not None and (cur_ms.get("due_on") or "")[:10] < \
            (bundle.get("meta", {}).get("ref_date")
             or bundle.get("meta", {}).get("to") or "")[:10]:
        notes.append("every dated open milestone is overdue (the latest, {!r}, was "
                     "due {}), so there is no next milestone".format(
                         cur_ms.get("title"), (cur_ms.get("due_on") or "")[:10]))
    else:
        notes.append("no dated open milestone is due after the current one{}, so "
                     "there is no next milestone".format(
                         " ({!r})".format(cur_ms.get("title")) if cur_ms else ""))
    notes.append("no open item has a priority label (the default patterns{})"
                 .format(" and {} operator pattern(s)".format(len(extra_patterns))
                         if extra_patterns else ""))
    notes.append("no open, non-draft PR has an approving review without a later "
                 "change request")
    return notes


def build_forecast(bundle, matchers=None, extra_patterns=()):
    """Compute the next-release forecast over bundle['buckets']['next_candidates'].

    Sets bundle['forecast'] = {next_milestone, candidates}.  Each candidate
    carries a ref (type/id/url), train id, weighted score, tier, signals, and
    non-scoring notes.  When there is no candidate although the view holds an
    open PR or issue, `forecast.notes` says why, one sentence per signal
    source (#97 final gate); `forecast.priority_patterns` lists the
    operator's `extra_patterns` when there are any. Pure aside from writing
    forecast onto the bundle; reads buckets, milestones, prs, issues, trains,
    and meta.  Forward-only: no predicted-vs-landed comparison.
    """
    meta        = bundle.get("meta", {})
    period      = window_period(meta)
    ref_date    = meta.get("ref_date") or meta.get("to") or ""

    # Resolve next milestone title via the shared helper.
    cur_ms, next_ms = select_milestones(bundle.get("milestones", []), ref_date)
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

    # Train outcome by id (#87): only an `in_flight` train is live work; a
    # rejected / shipped / abandoned train, or an id naming no train, is not.
    train_outcome = {t.get("id"): t.get("outcome")
                     for t in bundle.get("trains") or [] if t.get("id")}

    def _tier(score):
        if score >= FORECAST_TIER_LIKELY_THRESHOLD:
            return "likely"
        if score >= FORECAST_TIER_POSSIBLE_THRESHOLD:
            return "possible"
        return "longshot"

    def _score_candidate(nc_ref):
        """Return (score, signals, notes) for one next_candidate ref."""
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

        # Determine in_motion (#87: live work only):
        #   - the candidate is itself an open (unmerged) PR
        #   - OR an open PR in the bundle closes / cross-references this issue
        #   - OR the candidate's train is `in_flight`
        # A train that is rejected, shipped or abandoned is NOT work in
        # progress, and neither is a train id that names no train.
        outcome = train_outcome.get(train_id) if train_id else None
        if type_ == "pr" and item.get("state") == "open" and not item.get("merged"):
            in_motion = True
            motion_text = "open PR"
        elif type_ == "issue" and open_pr_for_issue.get(id_):
            in_motion = True
            motion_text = "open PR"
        elif outcome == "in_flight":
            in_motion = True
            motion_text = "work in progress"
        else:
            in_motion = False
            motion_text = ""
        # Non-scoring context, so a report can say why a train is not counted.
        notes = []
        if outcome == "rejected":
            notes.append("previous attempt rejected")
        elif outcome == "shipped":
            notes.append("linked PR already merged")

        # Ordered list of (present_bool, weight_key, signal_text).
        checks = [
            (next_title is not None and item.get("milestone") == next_title,
             "on_next_milestone", f"on milestone {next_title}"),
            (_high_priority(item, matchers),
             "high_priority", "high-priority"),
            (type_ == "pr" and approved_pr(item),
             "approved", "approved, no later change request"),
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
        return score, signals, notes

    candidates = []
    for nc in bundle.get("buckets", {}).get("next_candidates", []):
        score, signals, notes = _score_candidate(nc)
        candidates.append({
            "ref":   {"type": nc["type"], "id": nc["id"], "url": nc["url"]},
            "train": nc.get("train"),
            "score": score,
            "tier":  _tier(score),
            "signals": signals,
            "notes":   notes,
        })

    # Sort: score descending, then ref id ascending for determinism on ties.
    candidates.sort(key=lambda c: (-c["score"], c["ref"]["id"]))

    bundle["forecast"] = {
        "next_milestone": next_title,
        "candidates":     candidates,
    }
    # omit-when-not-applicable: nothing open means nothing to forecast
    has_open = any(x.get("state") == "open"
                   for x in list(bundle.get("prs") or []) + list(bundle.get("issues") or []))
    if not candidates and has_open:
        bundle["forecast"]["notes"] = _empty_forecast_notes(
            bundle, cur_ms, next_ms, tuple(extra_patterns or ()))
    if extra_patterns:
        bundle["forecast"]["priority_patterns"] = list(extra_patterns)
    return bundle


# --- Module changes: the reader's view -------------------------------------
#
# `module_changes` answers what a release-notes reader asks of a window: which
# modules are new, what changed in the existing ones, what is breaking, what
# was deprecated, and how much of the window's merged work was plumbing (CI,
# docs generation, tests). It is derived at view time from stored records
# only: the view's commits / code events / symbol events / symbol moves /
# PRs / releases / code graph, plus the gathered `module_notes` fact (the
# changelog entries, version-file moves and version tags read from the
# clone). It is built only when the view carries `module_notes` (a gather
# that read them), so a bundle without them is unchanged.
#
# Two sources of "breaking" are kept apart on purpose:
#   breaking.declared   — a changelog "Breaking Changes" item: the maintainers'
#                         own statement, quoted verbatim;
#   breaking.candidates — derived by rule from the interface (a removed or
#                         moved parameter, a new required parameter, a removed
#                         output, a removed or moved child module, a removed
#                         module): a candidate a writer must check, never a
#                         claim.

# File classes, by path. First matching rule wins; `link.py --path-class
# CLASS=GLOB` (repeatable) puts the operator's rules first. A GLOB with a `/`
# matches the whole path (`**` any number of segments, `*` / `?` within one);
# without one it matches the file's name. Matching ignores case. A path no
# rule matches is `source` (deployable module code).
PATH_CLASSES = ("source", "tests", "docs", "generated", "metadata", "ci")
DEFAULT_PATH_CLASS_RULES = (
    # CI, pipelines and repository tooling
    ("ci", ".github/**"), ("ci", ".azuredevops/**"),
    ("ci", ".azure-pipelines/**"), ("ci", ".pipelines/**"),
    ("ci", "pipelines/**"), ("ci", "utilities/**"), ("ci", ".devcontainer/**"),
    ("ci", ".vscode/**"), ("ci", ".config/**"), ("ci", "Makefile"),
    ("ci", ".gitignore"), ("ci", ".gitattributes"), ("ci", ".editorconfig"),
    ("ci", ".pre-commit-config.yaml"), ("ci", "*.tflint.hcl"),
    ("ci", ".tflint*.hcl"), ("ci", "avm"), ("ci", "avm.bat"), ("ci", "avm.ps1"),
    # release and ownership metadata that travels with a module
    ("metadata", "CHANGELOG.md"), ("metadata", "CHANGES.md"),
    ("metadata", "HISTORY.md"), ("metadata", "RELEASE_NOTES.md"),
    ("metadata", "version.json"), ("metadata", "metadata.json"),
    ("metadata", "ORPHANED.md"), ("metadata", "DEPRECATED.md"),
    ("metadata", "CODEOWNERS"), ("metadata", ".terraform.lock.hcl"),
    ("metadata", ".terraform-docs.yml"),
    ("ci", ".*"),          # any other dot-file: repository configuration
    # generated from the source (the compiled template next to main.bicep)
    ("generated", "main.json"),
    # tests and examples
    ("tests", "**/tests/**"), ("tests", "**/test/**"), ("tests", "**/examples/**"),
    ("tests", "validation/**"), ("tests", "*.test.bicep"),
    ("tests", "*.tftest.hcl"), ("tests", "*_test.go"),
    # documentation
    ("docs", "docs/**"), ("docs", "guides/**"), ("docs", "*.md"),
    ("docs", "*.png"), ("docs", "*.svg"), ("docs", "*.jpg"), ("docs", "*.drawio"),
    ("docs", "LICENSE"),
)
# Conventional-commit types and title words that say "plumbing" (a secondary
# signal: paths decide the class, the title only confirms a sweep is one).
_PLUMBING_CC_TYPES = {"ci": "ci", "build": "ci", "chore": "ci", "docs": "docs",
                      "test": "tests", "tests": "tests", "style": "docs"}
_PLUMBING_TITLE_WORDS = (
    (re.compile(r"\bCI\b"), "ci"),
    (re.compile(r"\b(pipelines?|workflows?|lint(er|ing)?)\b", re.I), "ci"),
    (re.compile(r"\b(readmes?|docs|documentation)\b", re.I), "docs"),
    (re.compile(r"\btests?\b", re.I), "tests"),
)
# A PR is a mechanical sweep when it touches at least this many module areas
# and at least half of them only in docs / generated / metadata files.
SWEEP_MIN_AREAS = 5
MODULE_RANK_TOP_N = 10
_AVM_MODULE_KINDS = {"res": "resource", "ptn": "pattern", "utl": "utility"}
_BICEP_MODULE_REF_RE = re.compile(r"^\s*module\s+\w+\s+'([^']*)'")
_BICEP_RESOURCE_TYPE_RE = re.compile(r"'([^'@\s]+)@([^'\s]+)'")
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+/[^`\s]+)`")
_DEPRECATION_RE = re.compile(r"\bdeprecat", re.I)


def _glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.I)


def path_class_rules(extra=()):
    """[(class, glob, compiled)] of the operator's `extra` rules (each
    "CLASS=GLOB") followed by DEFAULT_PATH_CLASS_RULES. A malformed rule or
    an unknown class raises ValueError."""
    rules = []
    for spec in extra:
        cls, sep, glob = str(spec).partition("=")
        cls, glob = cls.strip(), glob.strip()
        if not sep or not glob or cls not in PATH_CLASSES:
            raise ValueError("--path-class wants CLASS=GLOB with CLASS one of "
                             "{}, got {!r}".format(", ".join(PATH_CLASSES), spec))
        rules.append((cls, glob))
    rules.extend(DEFAULT_PATH_CLASS_RULES)
    return [(cls, glob, _glob_regex(glob)) for cls, glob in rules]


def path_class(path, rules):
    """The class of one file path under `rules` (path_class_rules)."""
    name = (path or "").rsplit("/", 1)[-1]
    for cls, glob, rx in rules:
        if rx.match(path if "/" in glob else name):
            return cls
    return "source"


def _area_dir(area):
    return "" if area in ("main.bicep", "main.tf") else area


def _in_dir(path, d):
    return path.startswith(d + "/") if d else True


def module_kind(area, files=()):
    """The module kind of a code area, or None when it is not a module:
    `resource` / `pattern` / `utility` for `avm/res|ptn|utl/<group>/<name>`;
    `module` for a Terraform module root (the repo-root `main.tf` area, or an
    area with a `*.tf` file directly in it), `submodule` for
    `modules/<name>`; `deployment` for a Bicep module directory outside
    `avm/` (an area whose own `main.bicep` is among `files`, e.g. a landing
    zone's deployments)."""
    parts = (area or "").split("/")
    if len(parts) == 4 and parts[0] == "avm" and parts[1] in _AVM_MODULE_KINDS:
        return _AVM_MODULE_KINDS[parts[1]]
    if parts[0] == "avm":
        return None
    if area == "main.tf":
        return "module"
    if len(parts) == 2 and parts[0] == "modules":
        return "submodule"
    d = _area_dir(area)
    for f in files:
        if not _in_dir(f, d):
            continue
        rest = f[len(d) + 1:] if d else f
        if rest == "main.bicep":
            return "deployment"
        if "/" not in rest and rest.endswith(".tf"):
            return "module"
    return None


def _entry_file(area, kind):
    d = _area_dir(area)
    name = "main.tf" if kind in ("module", "submodule") else "main.bicep"
    return f"{d}/{name}" if d else name


def _interface_file(path, area, kind):
    """True for a file that declares the module's interface: its entry
    `main.bicep`, or for Terraform any `*.tf` directly in the module dir."""
    d = _area_dir(area)
    if not _in_dir(path, d):
        return False
    rest = path[len(d) + 1:] if d else path
    if kind in ("module", "submodule"):
        return "/" not in rest and rest.endswith(".tf")
    return rest == "main.bicep"


def _child_dir(path, area):
    """The child module directory a `main.bicep` path names inside `area`
    (not the area's own entry, not under tests/ or examples/), else None."""
    d = _area_dir(area)
    if not path.endswith("/main.bicep") or not _in_dir(path, d):
        return None
    child = path[:-len("/main.bicep")]
    if child == d or gather._is_scaffold_path(path):
        return None
    return child


def _bicep_param_required(decl):
    """True / False when a Bicep `param` declaration says whether it is
    required (no default and not nullable `?`), None when the snippet cannot
    tell (another language, a multi-line type, a snippet cut at its cap)."""
    m = re.match(r"^\s*param\s+\w+\s+(.*)$", decl or "")
    if not m:
        return None
    rest = m.group(1).strip()
    if "=" in rest:
        return False
    if rest.endswith("?"):
        return False
    if rest.endswith(("{", "[", "(")) or len(decl) >= 200:
        return None
    return True


def _module_ref(decl):
    m = _BICEP_MODULE_REF_RE.match(decl or "")
    if not m:
        return None
    ref = m.group(1)
    registry, version, local = gather._bicep_ref_parts(ref)
    out = {"ref": ref}
    if version:
        out["version"] = version
    out["target"] = registry or local or ref.rsplit(":", 1)[0]
    if local:
        out["local"] = True
    return out


def _resource_type(decl):
    m = _BICEP_RESOURCE_TYPE_RE.search(decl or "")
    return (m.group(1), m.group(2)) if m else (None, None)


class _ModuleCtx:
    """What the module view reads from a bundle, indexed once."""

    def __init__(self, bundle, rules):
        self.bundle = bundle
        self.rules = rules
        self.period = window_period(bundle.get("meta", {}))
        self.idx = derive.area_index(bundle.get("code_graph", {}))
        self.commits = {c.get("sha"): c for c in bundle.get("commits", [])}
        events = bundle.get("code_events", [])
        # every path the view saw: decides which areas are modules
        self.seen_files = sorted(
            {f for c in bundle.get("commits", []) for f in c.get("files") or []}
            | {e.get("path") for e in events if e.get("path")}
            | {e.get("old_path") for e in events if e.get("old_path")})
        self._kinds = {}
        self.prs = {p.get("number"): p for p in bundle.get("prs", [])}
        self.train_of = {}
        for t in bundle.get("trains", []):
            for n in t.get("prs") or []:
                self.train_of.setdefault(n, t.get("id"))
        tags = (bundle.get("module_notes") or {}).get("tags") or {}
        self.tags_collected = bool(tags.get("collected"))
        # a roll-up where some gathered window listed no tags: a version
        # without a tag in the view may have been tagged in such a window
        self.tags_partial = bool(tags.get("windows_without_tags"))

    def kind(self, area):
        """module_kind of an area over every path of the view (memoised)."""
        if area not in self._kinds:
            self._kinds[area] = module_kind(area, self.seen_files) \
                if area else None
        return self._kinds[area]

    def area_of(self, path):
        area = self.idx.get(path)
        if area is None and path:
            area = gather.classify_code_area(path, gather.DEFAULT_AREA_PATTERNS)
        return area

    def source(self, sha, path=None):
        """{commit, [pr], url, [path]} citing one commit (its PR when known)."""
        c = self.commits.get(sha) or {}
        out = {"commit": sha}
        pr = c.get("pr")
        if pr is not None:
            out["pr"] = pr
            out["url"] = (self.prs.get(pr) or {}).get("url") or \
                derive._commit_url(self.bundle, sha)
        else:
            out["url"] = derive._commit_url(self.bundle, sha)
        if path:
            out["path"] = path
        return out


def _bare_version(v):
    """A version string compared across a changelog heading and a tag:
    stripped, without a leading `v` / `V`."""
    v = str(v or "").strip()
    return v[1:] if v[:1] in ("v", "V") else v


def _is_release_version(v):
    """True for a changelog version that a tag could publish (starts with a
    digit after an optional `v`): `Unreleased` and other labels are not."""
    return bool(re.match(r"\d", _bare_version(v)))


def _release_row(r, **keys):
    """A view release as a module_changes row: `keys` maps the row's field
    names to the release's, then its notes (and their truncation) when the
    gather kept any."""
    row = {out: r.get(src) for out, src in keys.items()}
    if r.get("notes"):
        row["notes"] = r["notes"]
        if r.get("notes_truncated_from"):
            row["notes_truncated_from"] = r["notes_truncated_from"]
    return row


def _dedupe_sources(sources):
    seen, out = set(), []
    for s in sources:
        key = (s.get("commit"), s.get("path"))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _net_symbols(events):
    """Net change per symbol over the window's events (oldest first):
    added (absent before, present after), removed, or modified (present on
    both ends with a different declaration). An add-then-drop is nothing."""
    by_key = {}
    for e in events:
        by_key.setdefault((e.get("path"), e.get("subkind"), e.get("name")),
                          []).append(e)
    out = []
    for (path, subkind, name), evs in by_key.items():
        first, last = evs[0], evs[-1]
        before = first.get("change") != "add"
        after = last.get("change") != "drop"
        if before and after:
            b, a = first.get("before"), last.get("after")
            if b == a:
                continue
            change = "modified"
        elif after:
            change, b, a = "added", None, last.get("after")
        elif before:
            change, b, a = "removed", first.get("before"), None
        else:
            continue
        out.append({"path": path, "subkind": subkind, "name": name,
                    "change": change, "before": b, "after": a, "events": evs})
    return sorted(out, key=lambda s: (s["path"] or "", s["subkind"] or "",
                                      s["name"] or ""))


def _interface_summary(ctx, events, moves):
    """{parameters, outputs, resources, dependencies} of one module file set
    (empty groups omitted) and its breaking candidates."""
    groups, candidates = {}, []

    def put(group, change, row):
        groups.setdefault(group, {}).setdefault(change, []).append(row)

    # a symbol a move fact accounts for is reported as moved, not as a
    # removal here plus an addition there
    moved = {(m.get("from_path"), m.get("subkind"), m.get("name")) for m in moves} \
        | {(m.get("to_path"), m.get("subkind"), m.get("name")) for m in moves}
    for s in _net_symbols(events):
        if s["change"] != "modified" and (s["path"], s["subkind"], s["name"]) in moved:
            continue
        sources = _dedupe_sources(ctx.source(e.get("commit"), s["path"])
                                  for e in s["events"])
        sub, name = s["subkind"], s["name"]
        if sub in ("param", "variable"):
            row = {"name": name, "sources": sources}
            if s["change"] == "added":
                req = _bicep_param_required(s["after"]) if sub == "param" else None
                row.update(declaration=s["after"], required=req)
                put("parameters", "added", row)
                if req:
                    candidates.append({"rule": "required_parameter_added",
                                       "subject": name, "sources": sources})
            elif s["change"] == "removed":
                row["declaration"] = s["before"]
                put("parameters", "removed", row)
                candidates.append({"rule": "parameter_removed", "subject": name,
                                   "sources": sources})
            else:
                row.update(before=s["before"], after=s["after"])
                put("parameters", "modified", row)
                if sub == "param" and _bicep_param_required(s["before"]) is False \
                        and _bicep_param_required(s["after"]) is True:
                    candidates.append({"rule": "parameter_became_required",
                                       "subject": name, "sources": sources})
        elif sub == "output":
            row = {"name": name, "sources": sources}
            if s["change"] == "modified":
                row.update(before=s["before"], after=s["after"])
            else:
                row["declaration"] = s["after"] or s["before"]
            put("outputs", s["change"], row)
            if s["change"] == "removed":
                candidates.append({"rule": "output_removed", "subject": name,
                                   "sources": sources})
        elif sub == "resource":
            row = {"name": name, "sources": sources}
            if s["change"] == "modified":
                bt, bv = _resource_type(s["before"])
                at, av = _resource_type(s["after"])
                if bt and at and bt == at and bv != av:
                    put("resources", "api_versions",
                        dict(row, type=at, **{"from": bv, "to": av}))
                    continue
                row.update(before=s["before"], after=s["after"])
            else:
                row["declaration"] = s["after"] or s["before"]
            put("resources", s["change"], row)
        elif sub == "module":
            row = {"name": name, "sources": sources}
            if s["change"] == "modified":
                b, a = _module_ref(s["before"]), _module_ref(s["after"])
                if b and a and b["target"] == a["target"]:
                    row.update(change="version", target=a["target"],
                               **{"from": b.get("version"), "to": a.get("version")})
                elif b and a:
                    row.update(change="target", **{"from": b["ref"], "to": a["ref"]})
                else:
                    row.update(before=s["before"], after=s["after"])
                put("dependencies", "changed", row)
            else:
                ref = _module_ref(s["after"] or s["before"])
                if ref:
                    row.update(ref)
                else:
                    row["declaration"] = s["after"] or s["before"]
                put("dependencies", s["change"], row)
    for m in moves:
        group = "parameters" if m.get("subkind") in ("param", "variable") \
            else "outputs" if m.get("subkind") == "output" else None
        if group is None:
            continue
        sources = _dedupe_sources(
            ctx.source(e.get("commit"), e.get("path")) for e in events
            if e.get("name") == m.get("name") and e.get("change") in ("drop", "add")
            and e.get("path") in (m.get("from_path"), m.get("to_path")))
        row = {"name": m.get("name"), "from_path": m.get("from_path"),
               "to_path": m.get("to_path"), "confidence": m.get("confidence"),
               "sources": sources}
        put(group, "moved", row)
        if m.get("leaves"):
            candidates.append({"rule": "{}_moved".format(
                "parameter" if group == "parameters" else "output"),
                "subject": m.get("name"), "sources": sources})
    return groups, candidates


def _changelog_items(entry):
    for s in entry.get("sections") or []:
        for item in s.get("items") or []:
            yield s, item


def _deprecation_target(text):
    m = _BACKTICK_PATH_RE.search(text or "")
    return m.group(1) if m else None


def classify_pr_change(pr, ctx):
    """The product-vs-plumbing class of one PR, by rule from the files of
    its commits in the view (the title is a secondary signal).

    File classes come from `path_class`. Ignoring `generated` and
    `metadata` (they travel with any change): source and no ci -> `module`
    (`module_kinds` names the kinds of the module areas whose source
    changed); source and ci -> `mixed`; no source: only ci -> `ci`, only
    tests -> `tests-only`, only docs or nothing else -> `docs-only`, unless
    a changelog or version file changed -> `release` (a version cut with
    no code change); any other mix -> `mixed`. No commit of the PR in the view -> `unknown`.
    `title_signal` (ci / docs / tests) comes from a conventional-commit
    type (ci, build, chore, docs, test, style) or a title word (CI,
    pipeline, workflow, lint, readme, docs, tests). `sweep` when the PR
    touches at least SWEEP_MIN_AREAS module areas and half or more of them
    only in docs / generated / metadata / tests files; `substantive_areas`
    are the module areas whose source it did change. `plumbing`: no source
    file (a `release` is not plumbing), or a sweep whose title says
    plumbing."""
    number = pr.get("number")
    files = sorted({f for c in ctx.bundle.get("commits", [])
                    if c.get("pr") == number for f in c.get("files") or []})
    row = {"number": number, "url": pr.get("url"), "title": pr.get("title")}
    if ctx.train_of.get(number):
        row["train"] = ctx.train_of[number]
    if not files:
        row.update({"class": "unknown", "plumbing": None})
        return row
    classes, areas, source_areas = set(), set(), set()
    kinds = {}
    for f in files:
        cls = path_class(f, ctx.rules)
        classes.add(cls)
        area = ctx.area_of(f)
        kind = ctx.kind(area)
        if kind is None:
            continue
        areas.add(area)
        if cls == "source":
            source_areas.add(area)
            kinds[area] = kind
    real = classes - {"generated", "metadata"}
    release = any(gather._note_file_kind(f) for f in files)
    if "source" in real:
        cls = "mixed" if "ci" in real else "module"
    elif real == {"ci"}:
        cls = "ci"
    elif real == {"tests"}:
        cls = "tests-only"
    elif real <= {"docs"} and release:
        cls = "release"
    elif real <= {"docs"}:
        cls = "docs-only"
    else:
        cls = "mixed"
    row["class"] = cls
    if kinds:
        row["module_kinds"] = sorted(set(kinds.values()))
    title = pr.get("title") or ""
    signal = None
    cc = gather._CC_PREFIX_RE.match(title.strip().lower())
    if cc and cc.group(1) in _PLUMBING_CC_TYPES:
        signal = _PLUMBING_CC_TYPES[cc.group(1)]
    else:
        for rx, sig in _PLUMBING_TITLE_WORDS:
            if rx.search(title):
                signal = sig
                break
    if signal:
        row["title_signal"] = signal
    sweep = len(areas) >= SWEEP_MIN_AREAS and \
        2 * len(areas - source_areas) >= len(areas)
    if sweep:
        row["sweep"] = {"areas": len(areas),
                        "plumbing_areas": len(areas - source_areas)}
        row["substantive_areas"] = sorted(source_areas)
    row["plumbing"] = ("source" not in real and cls != "release") \
        or bool(sweep and signal)
    row["areas"] = sorted(areas)
    return row


def _merged_in_window(pr, ctx):
    if not pr.get("merged") or not _in_window(pr.get("merged_at"), ctx.period):
        return False
    base = ctx.bundle.get("meta", {}).get("base_branch")
    return base is None or pr.get("base") in (None, base)


def repo_slug(meta):
    """`owner/name` of a view's repository: `meta.repo` as is when it is
    already qualified (a manifest member's view carries the project as
    `owner` and `Org/name` as `repo`), else `owner/repo`; None without a
    repository. The one place a GitHub URL or a repository comparison of
    the view is built from."""
    meta = meta or {}
    repo = meta.get("repo")
    if not repo:
        return None
    if "/" in repo:
        return repo
    return "{}/{}".format(meta["owner"], repo) if meta.get("owner") else None


def _repo_web(bundle):
    slug = repo_slug((bundle or {}).get("meta"))
    return "https://github.com/" + slug if slug else None


def registry_reference(area, kind, version):
    """The public Bicep registry reference of a published module version
    (`br/public:<area>:<version>`) for a Bicep module area under `avm/`;
    None where none is derivable (Terraform, other repositories)."""
    if not area or not version or not area.startswith("avm/") \
            or kind in ("module", "submodule"):
        return None
    return "br/public:{}:{}".format(area, version)


def _module_links(bundle, entries):
    """Stable links on the module rows (in place): `url`, the module
    folder at the commit the view's structure was read at (meta.clone_sha;
    the branch when unknown), and per published version the tag's tree
    `url` and, where derivable, its `registry` reference. Nothing without
    the repository's owner / name."""
    web = _repo_web(bundle)
    if not web:
        return
    for e in entries:
        area = e.get("area")
        if area:
            e["url"] = module_url(bundle, area)
        for p in (e.get("versions") or {}).get("published") or []:
            if p.get("tag"):
                p["url"] = "{}/tree/{}".format(web, p["tag"])
            reg = registry_reference(p.get("module") or area, e.get("kind"),
                                     p.get("version"))
            if reg:
                p["registry"] = reg


RELEASE_COMMITS_MAX = 50       # commits listed per tag without a changelog entry
RELEASE_ITEMS_MAX = 30         # changelog items listed per child version


def _under(path, folder):
    return (path or "").startswith(folder.rstrip("/") + "/")


def annotate_release_contents(bundle):
    """What is in each published version of the module layer, so no version
    is "a version and a path only" (`module_changes.modules[].versions.
    published[]`, in place):
    - `contents` {source: "changelog", prs, [items]} when a changelog entry
      of the window declares it (`items`: its verbatim items, for a child
      module's version);
    - else `contents` {source: "commits_between_tags", from {tag} |
      {window_start}, to (the tag), prs, commits [{sha, subject, pr, url}],
      [commits_more], [interface] (the module's interface rows those commits
      made), [note] (none did: the commit the tag sits on)} — the walked
      branch's commits touching the module folder after
      the module's previous tag (else the window's start) up to the tag;
    - a child module's version whose files no commit of that range changed:
      `note` "republished with its parent, no change of its own"."""
    mc = bundle.get("module_changes") or {}
    tags = [t for t in ((bundle.get("module_notes") or {}).get("tags") or {})
            .get("items") or [] if isinstance(t, dict)]
    start = window_period(bundle.get("meta") or {}).get("from") or ""
    commits = sorted((c for c in bundle.get("commits") or [] if isinstance(c, dict)),
                     key=lambda c: c.get("committed_at") or c.get("date") or "")
    for e in mc.get("modules") or []:
        area = e.get("area")
        for x in (e.get("versions") or {}).get("published") or []:
            key = x.get("module") or area
            entries = [c for c in e.get("changelog") or []
                       if (c.get("module") or area) == key and c.get("version")
                       and _bare_version(c["version"]) == _bare_version(x.get("version"))]
            if entries:
                cont = {"source": "changelog",
                        "prs": sorted({s.get("pr") for c in entries
                                       for s in c.get("sources") or [] if s.get("pr")})}
                if key != area:
                    items = [i.get("text") for c in entries for sec in c.get("sections") or []
                             for i in sec.get("items") or [] if i.get("text")]
                    if items:
                        cont["items"] = items[:RELEASE_ITEMS_MAX]
                x["contents"] = cont
            prev = [t for t in tags if t.get("module") == key
                    and (t.get("date") or "") < (x.get("date") or "")]
            prev_tag = max(prev, key=lambda t: t.get("date") or "") if prev else None
            lo = prev_tag.get("date") if prev_tag else start
            hi = x.get("date") or ""
            mine = [c for c in commits
                    if lo < (c.get("committed_at") or c.get("date") or "") <= hi
                    and any(_under(f, key if key != "main.tf" else "")
                            or key == "main.tf" for f in c.get("files") or [])]
            if key != area and not mine:
                x["note"] = "republished with its parent, no change of its own"
            if entries:
                continue
            shas = {c.get("sha") for c in mine}
            iface = []
            for group, changes in (e.get("interface") or {}).items():
                for ch, rows in (changes or {}).items():
                    for r in rows or []:
                        if any(src.get("commit") in shas for src in r.get("sources") or []):
                            iface.append({"group": group, "change": ch, "name": r.get("name")})
            cont = {"source": "commits_between_tags",
                    "from": {"tag": prev_tag.get("tag")} if prev_tag
                    else {"window_start": start},
                    "to": x.get("tag"),
                    "prs": sorted({c.get("pr") for c in mine if c.get("pr") is not None}),
                    "commits": [{"sha": c.get("sha"),
                                 "subject": (c.get("message") or "").split("\n")[0],
                                 "pr": c.get("pr"),
                                 "url": derive._commit_url(bundle, c.get("sha"))}
                                for c in mine[-RELEASE_COMMITS_MAX:]]}
            if len(mine) > RELEASE_COMMITS_MAX:
                cont["commits_more"] = len(mine) - RELEASE_COMMITS_MAX
            if not mine:
                tc = next((c for c in commits if c.get("sha") == x.get("commit")), None)
                cont["note"] = "no commit since {} changed the module up to this tag".format(
                    "the previous tag" if prev_tag else "the window's start") + (
                    "; the tag sits on {} (#{}), which did not change it".format(
                        tc.get("sha")[:7], tc.get("pr")) if tc and tc.get("pr") is not None
                    else "")
            if iface:
                cont["interface"] = iface
            x["contents"] = cont
    return bundle


def build_module_changes(bundle, path_rules=()):
    """bundle["module_changes"] (BUNDLE.md "module_changes"), when the bundle
    carries `module_notes`; otherwise nothing is added. Pure over the
    bundle (in place). `path_rules` are the operator's `--path-class`
    rules, recorded in `module_changes.path_rules`."""
    notes = bundle.get("module_notes")
    if not isinstance(notes, dict) or not notes.get("collected"):
        return bundle
    ctx = _ModuleCtx(bundle, path_class_rules(path_rules))
    commits = bundle.get("commits", [])
    kind = ctx.kind

    mods = {}

    def mod(area):
        if area not in mods:
            mods[area] = {"area": area, "kind": kind(area), "_commits": set(),
                          "_source": False, "_classes": set(), "_events": [],
                          "_symbols": [], "_children": {}, "_markers": [],
                          "_changelog": [], "_versions": [], "_tags": [],
                          "_entry": []}
        return mods[area]

    # 1. Commit files: which module areas were touched, by which class.
    for c in commits:
        for f in c.get("files") or []:
            area = ctx.area_of(f)
            if not kind(area):
                continue
            m = mod(area)
            m["_commits"].add(c.get("sha"))
            cls = path_class(f, ctx.rules)
            m["_classes"].add(cls)
            if cls == "source":
                m["_source"] = True
                m.setdefault("_source_files", set()).add(f)

    # 2. File events: entry files, child modules, lifecycle markers.
    for e in bundle.get("code_events", []):
        path, change, old = e.get("path") or "", e.get("change"), e.get("old_path")
        for p, role in ((path, "new"), (old, "old")):
            if not p:
                continue
            area = ctx.area_of(p)
            if not kind(area):
                continue
            m = mod(area)
            m["_commits"].add(e.get("commit"))
            k = kind(area)
            entry = _entry_file(area, k)
            base = p.rsplit("/", 1)[-1]
            d = p.rpartition("/")[0]
            src = ctx.source(e.get("commit"), p)
            if p == entry:
                m["_entry"].append({"change": change, "role": role, "path": p,
                                    "old_path": old, "new_path": path,
                                    "source": src})
            child = _child_dir(p, area)
            if child:
                ch = m["_children"].setdefault(child, {"path": child, "events": []})
                ch["events"].append({"change": change, "role": role,
                                     "other": path if role == "old" else old,
                                     "source": src})
            if base in ("DEPRECATED.md", "ORPHANED.md") and role == "new" \
                    and change in ("add", "delete", "rename", "copy"):
                m["_markers"].append({"file": base, "change": change, "dir": d,
                                      "source": src})

    # 3. Symbol events of interface files (the module's and its children's).
    for e in bundle.get("symbol_events", []):
        if e.get("subkind") in ("comment", "todo", "var"):
            continue
        path = e.get("path") or ""
        area = ctx.area_of(path)
        if not kind(area):
            continue
        k = kind(area)
        if _interface_file(path, area, k):
            mod(area)["_symbols"].append(e)
        else:
            child = _child_dir(path, area)
            if child:
                ch = mod(area)["_children"].setdefault(
                    child, {"path": child, "events": []})
                ch.setdefault("symbols", []).append(e)
    moves = (bundle.get("symbol_moves") or {}).get("links") or []

    # 4. The gathered notes: changelog entries, version moves, tags.
    for rec in notes.get("changelogs") or []:
        area = ctx.area_of(rec.get("path"))
        if kind(area):
            mod(area)["_changelog"].append(rec)
    for rec in notes.get("versions") or []:
        area = ctx.area_of(rec.get("path"))
        if kind(area):
            mod(area)["_versions"].append(rec)
    tags = notes.get("tags") or {}
    for t in tags.get("items") or []:
        module = t.get("module") or ""
        area = ctx.area_of(module + "/main.bicep") if module else None
        if module and not kind(area):
            area = ctx.area_of(module + "/main.tf")
        if area is None and not module:
            area = "main.tf" if kind("main.tf") else None
        if area and kind(area):
            mod(area)["_tags"].append(t)

    entries = [_module_entry(ctx, m, moves, bundle) for m in mods.values()]
    entries = [e for e in entries if e is not None]
    entries.sort(key=lambda e: e["area"])
    _module_links(bundle, entries)

    # 5. Merged PRs: product vs plumbing.
    pr_rows = [classify_pr_change(p, ctx) for p in bundle.get("prs", [])
               if _merged_in_window(p, ctx)]
    pr_rows.sort(key=lambda r: r["number"] if isinstance(r["number"], int) else -1)
    pr_class = {r["number"]: r for r in pr_rows}
    for e in entries:
        for p in e.get("prs", []):
            r = pr_class.get(p["number"])
            if r is not None:
                p["class"] = r["class"]
                p["plumbing"] = r["plumbing"]

    out = {"summary": _module_summary(entries, pr_rows), "modules": entries,
           "prs": pr_rows}
    releases = [_release_row(r, tag="tag_name", name="name",
                             published_at="published_at", url="url",
                             prerelease="prerelease")
                for r in bundle.get("releases", [])]
    if releases:
        out["releases"] = releases
    inputs = {"tags_collected": bool(tags.get("collected"))}
    if tags.get("windows_without_tags"):
        # no "declared, not published" claim is made for such a view
        inputs["windows_without_tags"] = tags["windows_without_tags"]
    if not tags.get("collected") and tags.get("reason"):
        inputs["tags_reason"] = tags["reason"]
    if notes.get("unread"):
        inputs["unread"] = notes["unread"]
    if notes.get("windows"):
        inputs["windows"] = notes["windows"]
        inputs["covers_view"] = notes.get("covers_view")
    out["inputs"] = inputs
    if path_rules:
        out["path_rules"] = list(path_rules)
    bundle["module_changes"] = out
    return bundle


def _children_summary(ctx, m):
    rows, candidates, deprecations = [], [], []
    for path in sorted(m["_children"]):
        ch = m["_children"][path]
        evs = ch["events"]
        change = None
        row = {"path": path}
        for ev in evs:
            if ev["change"] == "rename" and ev["role"] == "old":
                change = "moved"
                row["to"] = ev["other"].rpartition("/")[0]
            elif ev["change"] == "rename" and ev["role"] == "new":
                change = "moved"
                row["from"] = (ev["other"] or "").rpartition("/")[0]
            elif ev["change"] in ("add", "copy") and change is None:
                change = "added"
            elif ev["change"] == "delete":
                change = "removed"
        row["change"] = change or "modified"
        row["sources"] = _dedupe_sources(ev["source"] for ev in evs)
        if ch.get("symbols"):
            iface, _cands = _interface_summary(ctx, ch["symbols"], [])
            if iface:
                row["interface"] = iface
        if row["change"] == "moved" and "to" in row and "from" not in row:
            continue          # reported once, under the destination path
        rows.append(row)
        # a structural candidate is the commit that made the change (the
        # rename / delete), never a later commit that only touched the
        # child at its new path (its PR found the file already there)
        made = {"removed": ("delete",), "moved": ("rename",)}.get(row["change"], ())
        by = _dedupe_sources(ev["source"] for ev in evs if ev["change"] in made) \
            or row["sources"]
        if row["change"] == "removed":
            candidates.append({"rule": "child_module_removed", "subject": path,
                               "sources": by})
        elif row["change"] == "moved":
            candidates.append({"rule": "child_module_moved",
                               "subject": "{} -> {}".format(row.get("from"), path),
                               "sources": by})
    for mk in m["_markers"]:
        if mk["dir"] == _area_dir(m["area"]):
            continue
        key = "deprecated" if mk["file"] == "DEPRECATED.md" else "orphaned"
        if mk["change"] in ("add", "rename", "copy"):
            match = next((r for r in rows if r["path"] == mk["dir"]), None)
            if match is None:
                match = {"path": mk["dir"], "change": key, "sources": []}
                rows.append(match)
            match.setdefault("markers", []).append(mk["file"])
            match["sources"] = _dedupe_sources(match["sources"] + [mk["source"]])
            if key == "deprecated":
                deprecations.append({"target": mk["dir"], "basis": "marker_file",
                                     "file": mk["file"], "sources": [mk["source"]]})
    rows.sort(key=lambda r: r["path"])
    return rows, candidates, deprecations


def _module_entry(ctx, m, moves, bundle):
    area, kind = m["area"], m["kind"]
    d = _area_dir(area)
    entry = {"area": area, "kind": kind}
    # lifecycle of the module itself
    status = None
    moved_to = moved_from = None
    introduced = []
    for ev in m["_entry"]:
        if ev["role"] == "new" and ev["change"] in ("add", "copy"):
            status = status or "new"
            introduced.append(ev["source"])
        elif ev["role"] == "new" and ev["change"] == "delete":
            status = "removed"
        elif ev["role"] == "old" and ev["change"] == "rename":
            status, moved_to = "removed", ev["new_path"]
        elif ev["role"] == "new" and ev["change"] == "rename":
            moved_from = ev["old_path"]
    markers = [mk for mk in m["_markers"] if mk["dir"] == d]
    lifecycle = []
    for mk in markers:
        lifecycle.append({"file": mk["file"], "change": mk["change"],
                          "sources": [mk["source"]]})
    marker_status = None
    if any(mk["file"] == "DEPRECATED.md" and mk["change"] != "delete"
           for mk in markers):
        marker_status = "deprecated"
    elif any(mk["file"] == "ORPHANED.md" and mk["change"] != "delete"
             for mk in markers):
        marker_status = "orphaned"
    # changelog
    changelog, declared, deprecations = [], [], []
    first_version = None
    for rec in m["_changelog"]:
        src = ctx.source(rec.get("commit"), rec.get("path"))
        module = rec.get("module")
        if rec.get("new_file") and rec.get("change") == "add" and module == d \
                and rec.get("entries"):
            status = status or "new"
            introduced.append(src)
            first_version = first_version or rec["entries"][-1].get("version")
        for e in rec.get("entries") or []:
            row = {"version": e.get("version"), "sections": [
                {k: s[k] for k in ("heading", "kind", "items", "placeholder",
                                   "items_omitted") if k in s}
                for s in e.get("sections") or []], "sources": [src]}
            if module != d:
                row["module"] = module
            for k in ("date", "amended"):
                if e.get(k):
                    row[k] = e[k]
            changelog.append(row)
            for s, item in _changelog_items(e):
                if s.get("kind") == "breaking":
                    b = {"version": e.get("version"), "text": item["text"],
                         "sources": [src]}
                    if "truncated_from" in item:
                        b["truncated_from"] = item["truncated_from"]
                    if module != d:
                        b["module"] = module
                    declared.append(b)
                if s.get("kind") == "deprecated" or _DEPRECATION_RE.search(
                        item["text"]):
                    deprecations.append({
                        "target": _deprecation_target(item["text"]),
                        "basis": "changelog", "version": e.get("version"),
                        "text": item["text"], "sources": [src]})
    # versions
    versions = {}
    own_versions = [v for v in m["_versions"] if v.get("module") == d]
    if own_versions:
        versions["version_file"] = {
            "from": own_versions[0].get("from"), "to": own_versions[-1].get("to"),
            "sources": [ctx.source(v.get("commit"), v.get("path"))
                        for v in own_versions]}
    child_versions = [v for v in m["_versions"] if v.get("module") != d]
    if child_versions:
        versions["child_version_files"] = [
            {"module": v.get("module"), "from": v.get("from"), "to": v.get("to"),
             "sources": [ctx.source(v.get("commit"), v.get("path"))]}
            for v in child_versions]
    cl_versions = [c["version"] for c in changelog
                   if c.get("version") and "module" not in c]
    if cl_versions:
        versions["changelog"] = cl_versions
    own_tags = [t for t in m["_tags"] if (t.get("module") or "") == d]
    if ctx.tags_collected and (cl_versions or own_tags):
        # A version the module's changelog declared in the window with no
        # version tag of the module in it, and the reverse: a tag in the window
        # whose version the window's changelog entries did not declare (it was
        # declared earlier, or the module keeps no changelog).
        tagged = {_bare_version(t.get("version")) for t in own_tags}
        declared_v = {_bare_version(v) for v in cl_versions}
        not_published = [v for v in cl_versions
                         if _is_release_version(v)
                         and _bare_version(v) not in tagged]
        not_declared = [t.get("version") for t in own_tags
                        if _bare_version(t.get("version")) not in declared_v]
        if not_published and not ctx.tags_partial:
            versions["declared_not_published"] = not_published
        if not_declared:
            versions["published_not_declared"] = not_declared
    if m["_tags"]:
        published = []
        for t in m["_tags"]:
            row = {"version": t.get("version"), "tag": t.get("tag"),
                   "date": t.get("date"), "date_source": t.get("date_source"),
                   "commit": t.get("commit"), "first": t.get("first")}
            if (t.get("module") or "") != d:
                row["module"] = t.get("module")
            published.append(row)
            if t.get("first") and (t.get("module") or "") == d:
                status = status or "new"
                introduced.append(ctx.source(t.get("commit"),
                                             "refs/tags/" + (t.get("tag") or "")))
                first_version = first_version or t.get("version")
        versions["published"] = published
    if kind in ("module",) and area == "main.tf":
        rel = [_release_row(r, tag="tag_name", published_at="published_at",
                            url="url") for r in bundle.get("releases", [])]
        if rel:
            versions["releases"] = rel
    if status == "new" and first_version is None and own_versions:
        first_version = own_versions[-1].get("to")
    # interface
    own_moves = []
    for mv in moves:
        f_in = _interface_file(mv.get("from_path") or "", area, kind)
        t_in = _interface_file(mv.get("to_path") or "", area, kind)
        if f_in or t_in:
            own_moves.append(dict(mv, leaves=f_in and not t_in))
    iface, candidates = _interface_summary(ctx, m["_symbols"], own_moves)
    children, child_cands, child_deps = _children_summary(ctx, m)
    candidates += child_cands
    deprecations += child_deps
    if status == "removed":
        candidates.append({"rule": "module_removed", "subject": area,
                           "sources": [ev["source"] for ev in m["_entry"]]})
    if status is None:
        status = marker_status
    if status == "new":
        # nobody consumes a module that did not exist before the window
        candidates = []
    if status is None:
        status = "changed" if (m["_commits"] or changelog or own_versions) \
            or not m["_tags"] else "published"
    entry["status"] = status
    if first_version:
        entry["first_version"] = first_version
    if status == "new" and introduced:
        entry["introduced_by"] = _dedupe_sources(introduced)
    structure = {}
    if moved_to:
        structure["moved_to"] = moved_to
    if moved_from:
        structure["moved_from"] = moved_from
    if lifecycle:
        structure["lifecycle_markers"] = lifecycle
    if children:
        structure["children"] = children
    if structure:
        entry["structure"] = structure
    if versions:
        entry["versions"] = versions
    if changelog:
        entry["changelog"] = changelog
    if iface:
        entry["interface"] = iface
    breaking = {}
    if declared:
        breaking["declared"] = declared
    if candidates:
        breaking["candidates"] = candidates
    if breaking:
        entry["breaking"] = breaking
    if deprecations:
        entry["deprecations"] = deprecations
    # PRs and people behind it
    prs, authors = set(), set()
    for sha in m["_commits"]:
        c = ctx.commits.get(sha) or {}
        if c.get("pr") is not None:
            prs.add(c["pr"])
        if c.get("author"):
            authors.add(c["author"])
    for rec in m["_changelog"] + m["_versions"]:
        c = ctx.commits.get(rec.get("commit")) or {}
        if c.get("pr") is not None:
            prs.add(c["pr"])
    pr_rows = []
    for n in sorted(prs, key=lambda x: (not isinstance(x, int), x)):
        p = ctx.prs.get(n) or {}
        row = {"number": n}
        for k in ("url", "title", "author"):
            if p.get(k):
                row[k] = p[k]
        if p.get("author"):
            authors.add(p["author"])
        pr_rows.append(row)
    if m.get("_source_files"):
        entry["source_files_changed"] = len(m["_source_files"])
    if pr_rows:
        entry["prs"] = pr_rows
    if authors:
        entry["authors"] = sorted(authors, key=str)
    product = bool(m["_source"] or changelog or own_versions or m["_tags"]
                   or markers or m["_entry"] or children)
    entry["plumbing_only"] = not product
    entry["product_score"] = _product_score(entry)
    return entry


def _product_score(entry):
    """How much a reader-facing change this module carries (not PR count or
    touched files): 5 for a new module, 3 per declared breaking item, 2 per
    breaking candidate, 2 per child module change, 1 per interface change
    (parameter / output / resource / API version / dependency), 1 per
    changelog item, and 1 when any of its source files changed (a change
    inside the module that touches no declaration). 0 for a module whose
    only activity was plumbing."""
    if entry.get("plumbing_only"):
        return 0
    score = 5 if entry.get("status") == "new" else 0
    score += 1 if entry.get("source_files_changed") else 0
    b = entry.get("breaking") or {}
    score += 3 * len(b.get("declared") or []) + 2 * len(b.get("candidates") or [])
    score += 2 * len((entry.get("structure") or {}).get("children") or [])
    for group in (entry.get("interface") or {}).values():
        for rows in group.values():
            score += len(rows)
    for c in entry.get("changelog") or []:
        for s in c.get("sections") or []:
            score += len(s.get("items") or [])
    return score


def _module_summary(entries, pr_rows):
    by_status = {}
    for e in entries:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
    new = [{"area": e["area"], "kind": e["kind"],
            "first_version": e.get("first_version"),
            "introduced_by": sorted({s["pr"] for s in e.get("introduced_by", [])
                                     if s.get("pr") is not None}),
            "prs": [p["number"] for p in e.get("prs", [])]}
           for e in entries if e["status"] == "new"]
    new_children = [{"area": e["area"], "child": ch["path"],
                     "prs": sorted({s["pr"] for s in ch.get("sources", [])
                                    if s.get("pr") is not None})}
                    for e in entries
                    for ch in (e.get("structure") or {}).get("children", [])
                    if ch.get("change") == "added"]
    declared = []
    for e in entries:
        items = (e.get("breaking") or {}).get("declared") or []
        if items:
            versions = []
            for b in items:
                if b.get("version") not in versions:
                    versions.append(b.get("version"))
            declared.append({"area": e["area"], "versions": versions,
                             "items": len(items)})
    candidates = [{"area": e["area"], "rules": sorted({c["rule"] for c in (
        e.get("breaking") or {}).get("candidates") or []})}
        for e in entries if (e.get("breaking") or {}).get("candidates")]
    deprecations = [{"area": e["area"], "target": d.get("target"),
                     "basis": d.get("basis")}
                    for e in entries for d in e.get("deprecations") or []]
    ranked = sorted((e for e in entries if e["product_score"] > 0),
                    key=lambda e: (-e["product_score"], e["area"]))
    merged = len(pr_rows)
    known = [r for r in pr_rows if r["class"] != "unknown"]
    plumbing = sum(1 for r in known if r.get("plumbing"))
    by_class = {}
    for r in pr_rows:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    gaps = []
    for e in entries:
        v = e.get("versions") or {}
        if v.get("declared_not_published") or v.get("published_not_declared"):
            gap = {"area": e["area"]}
            for k in ("declared_not_published", "published_not_declared"):
                if v.get(k):
                    gap[k] = v[k]
            gaps.append(gap)
    out = {
        "modules": len(entries), "by_status": by_status,
        "new_modules": new, "new_child_modules": new_children,
        "declared_breaking": declared,
        "breaking_candidates": candidates, "deprecations": deprecations,
        "orphaned": [e["area"] for e in entries if e["status"] == "orphaned"],
        "removed": [e["area"] for e in entries if e["status"] == "removed"],
        "most_substantive": [{"area": e["area"], "status": e["status"],
                              "score": e["product_score"]}
                             for e in ranked[:MODULE_RANK_TOP_N]],
        "plumbing": {"merged_prs": merged, "classified": len(known),
                     "plumbing_prs": plumbing,
                     "share": round(plumbing / len(known), 3) if known else None,
                     "by_class": by_class},
    }
    if gaps:
        out["version_gaps"] = gaps
    return out


# ---------------------------------------------------------------------------
# The story layer: the discussion signal, story selection and story material
# ---------------------------------------------------------------------------
#
# Decisions sit in the text of issues and PRs. `discussion` measures that
# conversation per train (from the stored, as-of-projected records), a
# separate `story` selection ranks trains by it and by product relevance (not
# by touched-area count, which is what `tier` measures), and the slice of a
# train carries the whole conversation as one chronological timeline with its
# turning points marked by rule.

# Conversation turns (human comments, review comments, reviews with text or a
# verdict) a train needs before it can be a story: a back-and-forth.
STORY_MIN_EXCHANGES = 3
# A train still in flight is a story of THIS window only when somebody spoke
# in it: a long thread that has been silent since before the window is listed
# under `stories.quiet` instead (the 88-comment thread that ranked first two
# months running had its last comment six months earlier). A train that landed
# or was closed in the window needs no in-window turn: its ending is the news.
STORY_MIN_IN_WINDOW = 1
# Minimum story_score to be selected at all.
STORY_MIN_SCORE = 8.0
# At most this many trains are selected per view (like the deep tier's top 8);
# the others that qualify are counted in `stories.more`.
STORY_TOP_N = 8
# A train whose merged work is plumbing (module layer) or a mechanical sweep
# qualifies only when its discussion alone scores at least this much.
STORY_PLUMBING_MIN = 15.0
# A gap between two consecutive recorded human events longer than this many
# days is a silence (a turning point when activity resumes after it).
STORY_SILENCE_DAYS = 14
# A PR commit committed more than this many hours after it was authored was
# rewritten (amended, rebased or cherry-picked).
STORY_REWRITE_HOURS = 24
# Per-item text cap in the story timeline. Every human comment, review and
# review comment of Sept-Oct 2026 in Azure/bicep-registry-modules fits (the
# longest is 3,382 characters; the 99th percentile about 2,400), so arguments
# stay whole; PR / issue bodies are longer (median 2,100-2,600, much of it the
# PR template), so only the opening body of a long description is cut.
# Raised (issue #97, the owner: length is irrelevant): descriptions of
# 7,988 and 13,629 characters were cut at 5,000.
STORY_ITEM_CAP = 20000
# Per-slice budget for the timeline's text. The most discussed train of the
# two months (train-pr-6549: 88 human comments and review comments plus a
# 4,131-character body, 19,126 characters) fits whole, as does the largest
# issue train of October (about 23,000 once its 20,794-character body is
# capped). About 10,000 tokens of text.
STORY_SLICE_BUDGET = 120000
# When the budget is exceeded, items are cut evenly from the longest down, never
# below this many characters; past that, whole texts are left out from the
# middle of the timeline outward (the opening and the latest items stay).
STORY_ITEM_FLOOR = 500
# Budget for the code notes (comments / parameter descriptions / interface
# lines) carried on the timeline's commit items.
STORY_CODE_NOTES_BUDGET = 12000
# Budget for the narrative evidence of PR commits (`evidence`: interface
# deltas, code comments, documentation prose from `pr_history`), the newest
# commits first; past it rows are left out and counted
# (truncation.evidence).
STORY_EVIDENCE_BUDGET = 16000
# Per thread: the commits listed as what followed it; per slice: the
# requests listed in outcome.requests.
STORY_RESPONSE_COMMITS = 5
STORY_REQUESTS_MAX = 20
# Relevance points by train kind (story_score).
_STORY_KIND_RELEVANCE = {"feature": 3.0, "module-request": 3.0, "bug": 1.0}

# What GitHub records but gather does not keep, named in every story slice so
# a narrator says it rather than infers it.
STORY_NOT_GATHERED = (
    "title and description edits (titles and bodies are as last gathered)",
    "force-pushes and the branch states they replaced (only each PR's current "
    "head is fetched)",
    "label, milestone and assignee changes over time",
    "whether a review thread was marked resolved on GitHub",
    "check runs and their results per PR (CI pass / fail is not stored)",
)

_TIMELINE_KIND_ORDER = {"opened": 0, "commit": 1, "comment": 2,
                        "review_comment": 3, "review": 4, "ready_for_review": 5,
                        "reopened": 6, "closed": 7, "merged": 8,
                        "head_ref_force_pushed": 1, "checks": 2,
                        "renamed": 5, "convert_to_draft": 5,
                        "review_requested": 5, "review_request_removed": 5,
                        "assigned": 5,
                        "review_dismissed": 5}
# Timeline kinds that are not a person's move: check results arrive from
# automation. They neither end a silence nor become an open PR's last event.
_AUTOMATION_KINDS = ("checks", "automated_review")
# What the `pr_activity` fact adds to the slice: the lifecycle events copied
# onto the timeline and their extra fields.
_PR_EVENT_FIELDS = ("from", "to", "requested", "by", "review_id", "state",
                    "message", "before", "after", "assignee")


def window_end_instant(meta):
    """The as-of instant of a view: `records_as_of.instant`, else the last
    second of `period.to` / `to` (UTC) for a date, the value itself for a
    timestamp, None for a dateless bundle."""
    meta = meta or {}
    inst = (meta.get("records_as_of") or {}).get("instant")
    if inst:
        return inst
    to = window_period(meta).get("to")
    if not to:
        return None
    return to + "T23:59:59Z" if len(to) == 10 else to


def _not_after(ts, instant):
    if instant is None:
        return True
    a, b = _parse_ts(ts), _parse_ts(instant)
    return a is None or b is None or a <= b


def _human_rows(rows):
    return [r for r in rows or [] if isinstance(r, dict)
            and not derive.is_bot_login(r.get("author"))]


def _pr_history_index(bundle):
    """{pr number: pr_history row} of the view's `pr_history` fact."""
    ph = bundle.get("pr_history") or {}
    return {r["number"]: r for r in ph.get("prs") or []
            if isinstance(r, dict) and isinstance(r.get("number"), int)}


def _pr_commits_as_of(row, instant):
    """A pr_history row's commits committed by the instant (the as-of rule:
    the fact holds the head as gathered, possibly after the window)."""
    return [c for c in (row or {}).get("commits") or []
            if _not_after(c.get("committed_at"), instant)]


def own_change(c):
    """Is a PR commit's diff the PR's own change? Only a commit with exactly
    one parent: a merge (two or more parents: "Merge branch 'main'") and a
    boundary commit (no parent in the walk: its diff is against an empty
    tree) carry the base branch's files, thousands of them — never the PR's
    net change, interface or notes."""
    return c.get("parents", 1) == 1


def _not_own_reason(c):
    return "a merge of another branch: its files are that branch's" \
        if c.get("parents", 1) > 1 else \
        "a boundary commit read against an empty tree: its files are not the PR's change"


def _pr_own_commits_as_of(row, instant):
    """_pr_commits_as_of, the PR's own changes only (own_change)."""
    return [c for c in _pr_commits_as_of(row, instant) if own_change(c)]


def _train_members(train, pr_index, issue_index):
    issue_nums = train.get("issues")
    if issue_nums is None:
        root = train.get("root_issue")
        issue_nums = [root] if root is not None else []
    issues = [issue_index[n] for n in issue_nums if n in issue_index]
    prs = [pr_index[n] for n in train.get("prs", []) if n in pr_index]
    return issues, prs


def _days(a, b):
    return round((b - a).total_seconds() / 86400.0, 1)


def train_discussion(train, pr_index, issue_index, history=None, instant=None,
                     start=None):
    """The discussion signal of one train, or None when it has no human
    conversation (no human comment, review comment or review). Pure.

    Counts over the train's issues and PRs as the view holds them (already
    projected to the window's end): `comments` / `review_comments` (human),
    `participants` (distinct human authors of the items, comments, review
    comments and reviews), `reviews` by state (human, not the PR author's own;
    a `commented` review counts only with text — GitHub records each review
    thread reply as an empty `commented` review, whose text is the review
    comment), `review_rounds` and `reopens` (sums of the per-PR / issue
    annotations), `text_chars` (human bodies, comments, review comments and
    review texts), `first_response_days` (from the train's first item to the
    first human comment / review / review comment by someone other than that
    item's author), `longest_silence` (the longest gap between consecutive
    recorded human events — openings, comments, reviews, lifecycle events,
    merges and PR commits — with where it ends), `change_requests` and, when
    the PR history is known for every PR with a change request,
    `change_requests_reworked` (followed by a commit on that PR), and
    `bot_comments` (automation comments / reviews, not counted anywhere
    else). `in_window` counts the human comments, review comments and
    counted reviews made inside the view's window (from `start`, its first
    day, to the instant) — the window's own share of the debate."""
    issues, prs = _train_members(train, pr_index, issue_index)
    history = history or {}
    start_dt = _parse_ts(start + "T00:00:00Z" if start and len(start) == 10
                         else start)
    end_dt = _parse_ts(instant)
    turn_times = []
    comments = review_comments = bot = text = 0
    reviews = {"approved": 0, "changes_requested": 0, "commented": 0,
               "dismissed": 0}
    people = set()
    events = []        # (dt, url) of human events
    responses = []     # dt of a human response to someone else's item
    opened = []
    change_reqs = []   # (pr number, dt)
    for rec in issues + prs:
        author = rec.get("author")
        dt = _parse_ts(rec.get("created_at"))
        if author and not derive.is_bot_login(author):
            people.add(author)
            text += len(rec.get("body") or "")
        if dt is not None:
            opened.append(dt)
            events.append((dt, rec.get("url")))
        for key in ("comments_list", "review_comments"):
            for c in rec.get(key) or []:
                if not isinstance(c, dict):
                    continue
                if derive.is_bot_login(c.get("author")):
                    bot += 1
                    continue
                if key == "comments_list":
                    comments += 1
                else:
                    review_comments += 1
                if c.get("author"):
                    people.add(c["author"])
                text += len(c.get("body") or "")
                cdt = _parse_ts(c.get("created_at"))
                if cdt is not None:
                    turn_times.append(cdt)
                    events.append((cdt, c.get("url")))
                    if c.get("author") != author:
                        responses.append(cdt)
        for r in rec.get("reviews") or []:
            if not isinstance(r, dict) or (r.get("state") or "") == "pending":
                continue
            if derive.is_bot_login(r.get("author")):
                bot += 1
                continue
            rdt = _parse_ts(r.get("submitted_at"))
            body = r.get("body") or ""
            text += len(body)
            if r.get("author"):
                people.add(r["author"])
            if rdt is not None and (body or r.get("state") != "commented"):
                events.append((rdt, r.get("url")))
            if r.get("author") == author:
                continue
            state = r.get("state")
            if state == "commented" and not body:
                continue
            if state in reviews:
                reviews[state] += 1
            if rdt is not None:
                responses.append(rdt)
                turn_times.append(rdt)
            if state == "changes_requested":
                change_reqs.append((rec.get("number"), rdt))
        for e in rec.get("lifecycle") or []:
            if isinstance(e, dict) and not derive.is_bot_login(e.get("actor")):
                edt = _parse_ts(e.get("created_at"))
                if edt is not None:
                    events.append((edt, rec.get("url")))
        mdt = _parse_ts(rec.get("merged_at")) if rec.get("merged") else None
        if mdt is not None:
            events.append((mdt, rec.get("url")))
    for p in prs:
        for c in _pr_commits_as_of(history.get(p.get("number")), instant):
            cdt = _parse_ts(c.get("committed_at"))
            if cdt is not None and not derive.is_bot_login(c.get("author")):
                events.append((cdt, _pr_commit_url(p, c.get("sha"))))
    human_reviews = sum(reviews.values())
    if comments + review_comments + human_reviews == 0:
        return None
    out = {"comments": comments, "review_comments": review_comments,
           "participants": len(people), "reviews": reviews,
           "review_rounds": sum((p.get("review_rounds") or {}).get("count", 0)
                                for p in prs),
           "reopens": sum(r.get("reopen_count", 0) or 0 for r in issues + prs),
           "text_chars": text,
           "in_window": sum(1 for t in turn_times
                            if (start_dt is None or t >= start_dt)
                            and (end_dt is None or t <= end_dt))}
    start = min(opened) if opened else None
    later = sorted(d for d in responses if start is None or d >= start)
    out["first_response_days"] = (_days(start, later[0])
                                  if start is not None and later else None)
    events.sort(key=lambda e: (e[0], e[1] or ""))
    best = None
    for (a, _u), (b, url) in zip(events, events[1:]):
        if best is None or (b - a) > (best[1] - best[0]):
            best = (a, b, url)
    out["longest_silence"] = None if best is None else {
        "days": _days(best[0], best[1]),
        "from": best[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": best[1].strftime("%Y-%m-%dT%H:%M:%SZ"), "resumed_at": best[2]}
    out["change_requests"] = len(change_reqs)
    if change_reqs and all(n in history for n, _ in change_reqs):
        reworked = 0
        for n, cdt in change_reqs:
            times = [_parse_ts(c.get("committed_at"))
                     for c in _pr_commits_as_of(history[n], instant)]
            if cdt is not None and any(t is not None and t > cdt for t in times):
                reworked += 1
        out["change_requests_reworked"] = reworked
    out["bot_comments"] = bot
    return out


def annotate_train_discussion(bundle):
    """Add `discussion` to every train that has human conversation
    (train_discussion); a train without any gets no key. In place."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    history = _pr_history_index(bundle)
    instant = window_end_instant(bundle.get("meta"))
    start = window_period(bundle.get("meta") or {}).get("from")
    for t in bundle.get("trains", []):
        d = train_discussion(t, pr_index, issue_index, history, instant, start)
        if d is not None:
            t["discussion"] = d
    return bundle


def _train_plumbing(train, module_changes, bundle=None):
    """True when a train's work is plumbing: with the module layer, every
    merged PR of the train it classified is `plumbing` (or a sweep); without
    it, a chore / docs / other train touching more than 10 code areas (a
    mechanical sweep). With `bundle`, EVERY PR of the train must be
    classified: one the module layer did not classify (an open PR) is
    classified from its own commits' paths in the view's `pr_history`, as of
    the view's end (classify_pr_change over them); a PR that cannot be
    classified keeps the train out of plumbing."""
    if module_changes:
        rows = {r.get("number"): r for r in module_changes.get("prs") or []}
        if bundle is not None and train.get("prs"):
            verdicts = []
            for n in train["prs"]:
                r = rows.get(n) if n in rows else _classify_open_pr(bundle, n)
                if r is None or r.get("plumbing") is None:
                    return False
                verdicts.append(_plumbing_row(r))
            return all(verdicts)
        mine = [rows[n] for n in train.get("prs", []) if n in rows]
        if mine:
            return all(_plumbing_row(r) for r in mine)
    return (len(train.get("code_areas") or []) > 10
            and train.get("kind") in ("chore", "docs", "other", None))


def _plumbing_row(r):
    """A classified PR is plumbing: the module layer says so, or it is a
    sweep that changed no module's source (`substantive_areas` empty). A
    sweep that changes module source — an API version moved across 28
    modules — is product work, not plumbing."""
    return bool(r.get("plumbing") or (r.get("sweep") and not r.get("substantive_areas")))


def _classify_open_pr(bundle, n):
    """classify_pr_change of PR `n` from the paths of its own commits in the
    view's `pr_history` committed by the view's end, or None when it has
    none."""
    pr = next((p for p in bundle.get("prs") or [] if p.get("number") == n), None)
    row = _pr_history_index(bundle).get(n)
    instant = window_end_instant(bundle.get("meta"))
    paths = sorted({x for c in _pr_own_commits_as_of(row, instant)
                    for x in c.get("paths") or []})
    if pr is None or not paths:
        return None
    rules = path_class_rules(((bundle.get("module_changes") or {})
                              .get("path_rules")) or ())
    ctx = _ModuleCtx(bundle, rules)
    shim = types.SimpleNamespace(bundle={"commits": [{"pr": n, "files": paths}]},
                                 train_of=ctx.train_of, rules=ctx.rules,
                                 area_of=ctx.area_of, kind=ctx.kind)
    return classify_pr_change(pr, shim)


def _train_module_relevance(train, module_changes):
    """(points, reasons) of product relevance from the module layer: a new
    module introduced by one of the train's PRs (5), declared breaking items
    (2 each, at most 3), breaking candidates (1 each, at most 3)."""
    if not module_changes:
        return 0.0, []
    prs = set(train.get("prs") or [])
    points, reasons = 0.0, []
    for e in module_changes.get("modules") or []:
        mine = {p.get("number") for p in e.get("prs") or []} & prs
        if not mine:
            continue
        if e.get("status") == "new":
            points += 5
            reasons.append("new module {}".format(e.get("area")))
        br = e.get("breaking") or {}
        declared = [b for b in br.get("declared") or []
                    if any(s.get("pr") in prs for s in b.get("sources") or [])]
        cands = [c for c in br.get("candidates") or []
                 if any(s.get("pr") in prs for s in c.get("sources") or [])]
        if declared:
            points += 2 * min(3, len(declared))
            reasons.append("declared breaking change in {}".format(e.get("area")))
        if cands:
            points += min(3, len(cands))
            reasons.append("breaking candidate in {}".format(e.get("area")))
    return points, reasons


def story_score(train, module_changes=None, pr_index=None):
    """(discussion_points, total, reasons) for one train with a `discussion`
    (`pr_index` {number: pr} supplies each PR's review rounds).

    discussion points = human comments + review comments
                        + 1 per conversation turn inside the window
                          (`in_window`: the window's debate weighs double)
                        + 2 per participant beyond the first
                        + 3 per change request + 3 per reopen
                        + 2 per review round beyond the first on each
                          reviewed PR
                        + 2 when the longest silence exceeds
                          STORY_SILENCE_DAYS
                        + 1 per 2,000 characters of human text (at most 5)
    total = discussion points + relevance, where relevance is 3 for a
    feature / module-request train, 1 for a bug, 0 otherwise, plus the
    module layer's (a new module 5; declared breaking 2 each, candidates 1
    each, at most 3 of each) and 1 for an in-flight train (often where the
    debate still is)."""
    d = train.get("discussion") or {}
    reasons = []
    conv = d.get("comments", 0) + d.get("review_comments", 0)
    pts = float(conv)
    if conv:
        reasons.append("{} human comment(s) and review comment(s)".format(conv))
    if d.get("in_window"):
        pts += d["in_window"]
        reasons.append("{} conversation turn(s) in the window".format(
            d["in_window"]))
    if d.get("participants", 0) > 1:
        pts += 2 * (d["participants"] - 1)
        reasons.append("{} participants".format(d["participants"]))
    cr = d.get("change_requests", 0)
    if cr:
        pts += 3 * cr
        reasons.append("{} change request(s)".format(cr))
    if d.get("reopens"):
        pts += 3 * d["reopens"]
        reasons.append("reopened {}x".format(d["reopens"]))
    extra_rounds = sum(max(0, ((pr_index or {}).get(n, {}).get("review_rounds")
                               or {}).get("count", 0) - 1)
                       for n in train.get("prs") or [])
    if extra_rounds:
        pts += 2 * extra_rounds
        reasons.append("{} extra review round(s)".format(extra_rounds))
    sil = d.get("longest_silence") or {}
    if sil.get("days", 0) > STORY_SILENCE_DAYS:
        pts += 2
        reasons.append("{} days without recorded activity before {}".format(
            int(sil["days"]), sil["to"][:10]))
    vol = min(5, d.get("text_chars", 0) // 2000)
    pts += vol
    rel = _STORY_KIND_RELEVANCE.get(train.get("kind"), 0.0)
    if rel:
        reasons.append("kind {}".format(train.get("kind")))
    mpts, mreasons = _train_module_relevance(train, module_changes)
    rel += mpts
    reasons += mreasons
    if train.get("outcome") == "in_flight":
        rel += 1
        reasons.append("in flight")
    return pts, pts + rel, reasons


def select_story_trains(bundle):
    """Pick the trains with the most to tell (story_score), independently of
    the deep / mention `tier`. A train qualifies when it has a `discussion`
    with at least STORY_MIN_EXCHANGES conversation turns (comments, review
    comments, reviews with text or a verdict) and at least two participants,
    scores at least STORY_MIN_SCORE, and — when its work is plumbing or a
    mechanical sweep (_train_plumbing) — its discussion alone scores at least
    STORY_PLUMBING_MIN. The STORY_TOP_N best (score desc, then id) get
    `story: true`, `story_rank`, `story_score` and `story_reasons`; the view
    gets `stories` {selected, more, rule} (omitted when none qualifies). In
    place."""
    trains = bundle.get("trains", [])
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    mc = bundle.get("module_changes")
    eligible, quiet = [], []
    for t in trains:
        d = t.get("discussion")
        if not d:
            continue
        turns = (d["comments"] + d["review_comments"] + sum(d["reviews"].values()))
        disc, total, reasons = story_score(t, mc, pr_index)
        if turns < STORY_MIN_EXCHANGES or d["participants"] < 2:
            continue
        if total < STORY_MIN_SCORE:
            continue
        if _train_plumbing(t, mc) and disc < STORY_PLUMBING_MIN:
            continue
        if t.get("outcome") == "in_flight" \
                and (d.get("in_window") or 0) < STORY_MIN_IN_WINDOW:
            quiet.append((round(total, 1), t))
            continue
        eligible.append((round(total, 1), t, reasons))
    quiet.sort(key=lambda e: (-e[0], e[1]["id"]))
    quiet_rows = [{"id": t["id"], "score": s,
                   "idle_days": (t.get("effort") or {}).get("idle_days")}
                  for s, t in quiet]
    if not eligible:
        if quiet_rows:
            bundle["stories"] = {"selected": [], "more": [], "quiet": quiet_rows,
                                 "rule": _story_rule()}
        return bundle
    eligible.sort(key=lambda e: (-e[0], e[1]["id"]))
    selected = []
    for rank, (score, t, reasons) in enumerate(eligible[:STORY_TOP_N], 1):
        t["story"] = True
        t["story_rank"] = rank
        t["story_score"] = score
        t["story_reasons"] = reasons
        selected.append({"id": t["id"], "rank": rank, "score": score})
    bundle["stories"] = {
        "selected": selected,
        "more": [{"id": t["id"], "score": s} for s, t, _ in eligible[STORY_TOP_N:]],
        "rule": _story_rule(),
    }
    if quiet_rows:
        bundle["stories"]["quiet"] = quiet_rows
    return bundle


# --- every story of the window, grouped ---------------------------------------
#
# `stories.selected` keeps the top-N for older readers; `stories.all` is the
# whole list. A train is a story when its record has something of the
# window (story_worthy); each story gets one group by a stated rule.

# An open train whose last human event is older than this many days before
# the view's instant is stalled (it waits on someone).
STORY_STALLED_DAYS = 14
# An open train with conversation but no event in the window is carried
# over as stalled when its last human event is at most this many days old;
# older ones stay quiet (`stories.excluded` says so).
STORY_CARRY_MAX_DAYS = 180
STORY_GROUPS = ("landed", "in_flight", "stalled", "closed", "plumbing")


def _ts_in(ts, start, end):
    t = _parse_ts(ts)
    return t is not None and (start is None or t >= start) and (end is None
                                                                 or t <= end)


def _train_items(bundle, t):
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    return _train_members(t, pr_index, issue_index)


def story_worthy(bundle, t, start, end, history):
    """Why train `t` is a story of the window [start, end], or None. In
    order: a human conversation turn in the window (a comment, review
    comment or review beyond the opening post: `discussion.in_window`); an
    issue / PR of the train opened, merged, closed or reopened in it; a
    human commit on one of its PRs in it. Mechanical: nothing scores."""
    d = t.get("discussion") or {}
    if (d.get("in_window") or 0) >= 1:
        return "{} human turn(s) in the window".format(d["in_window"])
    issues, prs = _train_items(bundle, t)
    for rec in issues + prs:
        typ = "PR" if rec in prs else "issue"
        for key, what in (("created_at", "opened"), ("merged_at", "merged"),
                          ("closed_at", "closed")):
            if rec.get(key) and _ts_in(rec[key], start, end) \
                    and (key != "merged_at" or rec.get("merged")):
                return "{} #{} {} in the window".format(typ, rec.get("number"), what)
        for e in rec.get("lifecycle") or []:
            if isinstance(e, dict) and e.get("event") == "reopened" \
                    and _ts_in(e.get("created_at"), start, end):
                return "{} #{} reopened in the window".format(typ, rec.get("number"))
    for p in prs:
        for c in (history.get(p.get("number")) or {}).get("commits") or []:
            if _ts_in(c.get("committed_at"), start, end) \
                    and not derive.is_bot_login(c.get("author")):
                return "a commit on PR #{} in the window".format(p.get("number"))
    return None


def _failing_unanswered(bundle, t, instant):
    """(pr, head time) of an open PR of the train whose head checks are
    failing as of the instant (not only awaiting approval) with no event by
    its author since the head commit — only when EVERY open PR of the train
    is in that state (another open PR moving keeps the train in flight);
    else None."""
    activity = _pr_activity_index(bundle) or {}
    _issues, prs = _train_items(bundle, t)
    found = None
    for p in prs:
        if p.get("state") != "open":
            continue
        if p.get("number") not in activity:
            return None
        commits = [c for c in activity[p["number"]].get("commits") or []
                   if _not_after(c.get("committed_at"), instant)]
        if not commits:
            return None
        roll = _commit_rollup(commits[-1], instant,
                              activity[p["number"]].get("gathered_at"))
        if roll["rollup"] != "failing" or \
                set(roll["failing"]) <= set(roll["awaiting_approval"]):
            return None
        head_at = commits[-1].get("committed_at") or ""
        author = p.get("author")
        later = [c for key in ("comments_list", "review_comments")
                 for c in p.get(key) or [] if isinstance(c, dict)
                 and c.get("author") == author
                 and (c.get("created_at") or "") > head_at
                 and _not_after(c.get("created_at"), instant)]
        if later:
            return None
        found = found or (p["number"], head_at)
    return found


def story_group(bundle, t, worthy, instant, module_changes):
    """(group, state, group_rule) of a story train. State: `landed` (the
    train shipped: a PR merged into the walked branch), `closed` (closed
    without landing: rejected or abandoned), else open: `stalled` when (a)
    no story-worthy event of the window (carried over), (b) its last human
    event is more than STORY_STALLED_DAYS old at the instant, or (c) an
    open PR's head checks fail with no event by its author since; else
    `in_flight`. Group: `plumbing` when every PR of the train is authored
    by an automation account and no person took a conversation turn
    (_automation_only), or when every PR of the train is plumbing
    (_train_plumbing: the module layer's class, or a sweep), whatever the
    state; else the state."""
    outcome = t.get("outcome")
    if outcome == "shipped":
        state, rule = "landed", "landed: merged into the walked branch"
    elif outcome in ("rejected", "abandoned"):
        state, rule = "closed", "closed: {} without landing".format(outcome)
    else:
        idle = (t.get("effort") or {}).get("idle_days")
        failing = _failing_unanswered(bundle, t, instant)
        if worthy is None:
            state = "stalled"
            rule = ("stalled: carried over, no event in the window "
                    "({} days idle)".format(idle))
        elif isinstance(idle, (int, float)) and idle > STORY_STALLED_DAYS:
            state = "stalled"
            rule = "stalled: {} days since the last human event (threshold " \
                "{})".format(idle, STORY_STALLED_DAYS)
        elif failing:
            state = "stalled"
            rule = ("stalled: PR #{} head checks failing since {} with no "
                    "event by its author".format(failing[0], failing[1][:10]))
        else:
            state, rule = "in_flight", "in_flight: open, active in the window"
    auto = _automation_only(bundle, t, instant)
    if auto:
        return "plumbing", state, (
            "plumbing: every PR is authored by an automation account ({}) and no "
            "person took a conversation turn; ".format(", ".join(auto)) + rule)
    if t.get("prs") and _train_plumbing(t, module_changes, bundle):
        return "plumbing", state, "plumbing: every PR is plumbing; " + rule
    return state, state, rule


def train_released(bundle, t):
    """Does "landed" mean released? Per module a PR of the train touched
    (module layer) — and per child module whose version the train declared
    or whose files it changed (`child_of`) —: [{module, version, status,
    tag, date, url, registry?, tie?, declared_by?, child_of?}].
    A version is the train's only when TIED to it: the changelog entry
    declaring it has one of the train's PRs as its source, or its tag sits
    on one of the train's commits (the merge commit), or — on the walked
    branch, which is first-parent linear, so commit time is ancestry — it
    is the module's first tag of the window on or after the train's last
    merge commit (the first tag that contains it): then `tie` says so —
    `contained` with `declared_by` [pr] when another PR's changelog entry /
    version file declared that version (the train's change ships in it, the
    release is that PR's: a sweep touching every module is never "the"
    release PR), `first_tag_after_merge` when nobody declared it. Then
    `published` (a version tag of the module in the window), else
    `declared_not_published` (no tag in the window) or `unknown` (tags not
    read for the whole view). A module whose versions in the window are
    not tied to the train is `unknown` with a `reason` (a later PR's merge
    can be in no tag of the window, or in one this view cannot tie); a
    module with no version declared or published in the window is
    `no_version_change`. Omitted without the module layer."""
    mc = bundle.get("module_changes") or {}
    prs = set(t.get("prs") or [])
    tags_ok = (mc.get("inputs") or {}).get("tags_collected") and not \
        (mc.get("inputs") or {}).get("windows_without_tags")
    commits = set(t.get("commits") or [])
    by_sha = {c.get("sha"): c for c in bundle.get("commits") or []}
    out = []
    for e in mc.get("modules") or []:
        if not {p.get("number") for p in e.get("prs") or []} & prs:
            continue
        area = e.get("area")
        v = e.get("versions") or {}
        keys = [area] + sorted({x.get("module") for x in (v.get("published") or [])
                                + (e.get("changelog") or [])
                                if x.get("module") and x.get("module") != area})
        for key in keys:
            child = key != area
            published = [x for x in v.get("published") or []
                         if (x.get("module") or area) == key]
            declared = {}
            for c in e.get("changelog") or []:
                if c.get("version") and (c.get("module") or area) == key:
                    declared.setdefault(_bare_version(c["version"]), set()).update(
                        s.get("pr") for s in c.get("sources") or [] if s.get("pr"))
            if not child:
                vf = v.get("version_file") or {}
                for s in vf.get("sources") or []:
                    if vf.get("to") and s.get("pr"):
                        declared.setdefault("~" + str(vf["to"]), set()).add(s["pr"])
            mine = [c.get("version") for c in e.get("changelog") or []
                    if c.get("version") and (c.get("module") or area) == key
                    and any(s.get("pr") in prs for s in c.get("sources") or [])]
            mine = list(dict.fromkeys(mine))
            mine += [x.get("version") for x in published
                     if x.get("commit") in commits and x.get("version") not in mine]
            tie = {}
            if child and not mine:
                kid = key + "/"
                touched = any(f.startswith(kid) for sha in commits
                              for f in (by_sha.get(sha) or {}).get("files") or [])
                if not touched:
                    continue                 # a child the train did not change
            if not mine and published:
                # the walked branch is first-parent linear, so the first tag of
                # the module on or after the train's last merge commit is the
                # first that contains it (no earlier tag of the window does)
                merged = [c.get("committed_at") or c.get("date") for c in
                          bundle.get("commits") or [] if c.get("sha") in commits]
                merged = [m for m in merged if m]
                if merged:
                    last = max(merged)
                    later = sorted((x for x in published if (x.get("date") or "") >= last),
                                   key=lambda x: x.get("date") or "")
                    if later:
                        ver = later[0].get("version")
                        mine = [ver]
                        by = set(declared.get(_bare_version(ver)) or ())
                        if not child:
                            for k2, prs2 in declared.items():
                                if k2.startswith("~") and _bare_version(ver).startswith(
                                        k2[1:]):
                                    by |= prs2
                        by -= prs
                        tie[ver] = {"tie": "contained", "declared_by": sorted(by)} \
                            if by else {"tie": "first_tag_after_merge"}
            if not mine:
                if published or [c for c in e.get("changelog") or []
                                 if (c.get("module") or area) == key] or \
                        (not child and (v.get("version_file") or {}).get("to")):
                    out.append({"module": key, "version": None,
                                "status": "unknown", "tag": None, "date": None,
                                "url": e.get("url") if not child else module_url(bundle, key),
                                "reason": "the module's versions in the window are "
                                          "not tied to the train's PRs"})
                elif not child:
                    out.append({"module": key, "version": None,
                                "status": "no_version_change", "tag": None,
                                "date": None, "url": e.get("url")})
                continue
            tags = {_bare_version(x.get("version")): x for x in published}
            for version in mine:
                x = tags.get(_bare_version(version))
                if x is not None:
                    row = {"module": key, "version": x.get("version"),
                           "status": "published", "tag": x.get("tag"),
                           "date": x.get("date"), "url": x.get("url")}
                    if x.get("registry"):
                        row["registry"] = x["registry"]
                else:
                    row = {"module": key, "version": version,
                           "status": "declared_not_published" if tags_ok else "unknown",
                           "tag": None, "date": None,
                           "url": e.get("url") if not child else module_url(bundle, key)}
                row.update(tie.get(version) or {})
                if child:
                    row["child_of"] = area
                out.append(row)
    return out


def annotate_issue_closing(bundle):
    """`closing` on every closed issue of a story-layer view (one carrying
    `pr_history`): {at, by (the closing event's actor), state_reason,
    commit (the commit that closed it, when recorded), prs (the view's PRs
    that name it as closed, `closes`), before_merge: [pr] (those PRs that
    were not merged yet when it closed)} — each key only when recorded, so
    "closed two minutes after the PR opened, nine days before the merge, by
    the PR's author" is read off the record. In place."""
    if "pr_history" not in bundle:
        return bundle
    prs = bundle.get("prs") or []
    instant = window_end_instant(bundle.get("meta"))
    for i in bundle.get("issues") or []:
        if i.get("state") != "closed":
            continue
        ev = [e for e in i.get("lifecycle") or []
              if isinstance(e, dict) and e.get("event") == "closed"
              and _not_after(e.get("created_at"), instant)]
        last = ev[-1] if ev else {}
        row = {"at": last.get("created_at") or i.get("closed_at")}
        if last.get("actor"):
            row["by"] = last["actor"]
        reason = last.get("state_reason") or i.get("state_reason")
        if reason:
            row["state_reason"] = reason
        if last.get("commit"):
            row["commit"] = last["commit"]
        closers = [p for p in prs if i.get("number") in (p.get("closes") or [])]
        if closers:
            row["prs"] = sorted(p.get("number") for p in closers)
            early = sorted(p.get("number") for p in closers
                           if row["at"] and (not p.get("merged_at")
                                             or p["merged_at"] > row["at"]
                                             or not _not_after(p["merged_at"],
                                                               instant)))
            if early:
                row["before_merge"] = early
        i["closing"] = {k: v for k, v in row.items() if v is not None}
    return bundle


# --- automation accounts, for what they are (issue #97) ----------------------
#
# Flagged from what the records hold, never from a display name: the operator's
# registry tag (wins both ways), the account type the API reported for the
# author (`author_type`, kept by gather when it is not "User"), else the
# app-account login form (`name[bot]`) and the git names automation commits
# under (derive.is_bot_login).

AUTOMATION_TAG = "automation"          # people registry `tags`: is automation
NOT_AUTOMATION_TAG = "not-automation"  # ... is a person, whatever the record says


def registry_automation(source):
    """{login: True | False} from the people registry's operator tags
    (AUTOMATION_TAG / NOT_AUTOMATION_TAG) of `source` (link --people);
    {} without one."""
    out = {}
    for login, e in sorted(((source or {}).get("registry") or {})
                           .get("people", {}).items()):
        tags = (e or {}).get("tags") or []
        if NOT_AUTOMATION_TAG in tags:
            out[login] = False
        elif AUTOMATION_TAG in tags:
            out[login] = True
    return out


def _account_types(bundle):
    """{login: account type} the view's records keep (`author_type`)."""
    out = {}
    for key in ("prs", "issues"):
        for rec in bundle.get(key) or []:
            if not isinstance(rec, dict):
                continue
            if rec.get("author") and rec.get("author_type"):
                out.setdefault(rec["author"], rec["author_type"])
            for k in ("comments_list", "review_comments", "reviews"):
                for c in rec.get(k) or []:
                    if isinstance(c, dict) and c.get("author") and c.get("author_type"):
                        out.setdefault(c["author"], c["author_type"])
    for row in (_pr_activity_index(bundle) or {}).values():
        for e in row.get("events") or []:
            if isinstance(e, dict) and e.get("actor") and e.get("actor_type"):
                out.setdefault(e["actor"], e["actor_type"])
    return out


def _on_behalf_of(p, activity_row, instant, issues=None):
    """The person an automation account's PR `p` works for, from the record
    only, with the `basis`: the person who assigned the agent (an
    `assigned` event giving it the PR, by someone else: `assigned`), else
    the person assigned to the PR with it (`assignee`), else the first
    person who requested a review on it (`requested review`), else the
    author of the issue the PR closes (`closes the issue of`; `issues`
    {number: issue}); None when the record shows none."""
    evs = [e for e in (activity_row or {}).get("events") or []
           if isinstance(e, dict) and _not_after(e.get("at"), instant)]
    author = p.get("author")

    def person(x):
        return isinstance(x, str) and x and x != author and not derive.is_bot_login(x)
    for e in evs:
        if e.get("event") == "assigned" and e.get("assignee") == author \
                and person(e.get("actor")):
            return {"login": e["actor"], "basis": "assigned", "pr": p.get("number")}
    for e in evs:
        if e.get("event") == "assigned" and person(e.get("assignee")):
            return {"login": e["assignee"], "basis": "assignee", "pr": p.get("number")}
    for e in evs:
        if e.get("event") == "review_requested" and person(e.get("by")):
            return {"login": e["by"], "basis": "requested review", "pr": p.get("number")}
    for n in p.get("closes") or []:
        i = (issues or {}).get(n)
        if i and person(i.get("author")):
            return {"login": i["author"], "basis": "closes the issue of",
                    "pr": p.get("number"), "issue": n}
    return None


def module_owners(bundle):
    """{module: [login]} — who owns each module at the window's end: the
    owner files of the end tree (`module_ownership.owners_at_end`, a gather
    that read them) and CODEOWNERS rules naming people (`code_owners`;
    teams left out) whose pattern is a module folder."""
    out = {}
    for m, logins in ((bundle.get("module_ownership") or {}).get("owners_at_end")
                      or {}).items():
        out.setdefault(m, [])
        out[m] += [x for x in logins if x not in out[m]]
    rules = bundle.get("code_owners")
    rules = rules if isinstance(rules, list) else (rules or {}).get("rules") or []
    for r in rules:
        if not isinstance(r, dict):
            continue
        m = gather._codeowners_module(r.get("pattern"))
        if module_kind(m) is None:
            continue
        people = [o.lstrip("@") for o in r.get("owners") or []
                  if isinstance(o, str) and "/" not in o]
        if people:
            out.setdefault(m, [])
            out[m] += [x for x in people if x not in out[m]]
    return out


def annotate_module_owners(bundle):
    """`module_owner_of: [module]` on the people rows of module owners
    (module_owners). In place."""
    owners = module_owners(bundle)
    if not owners:
        return bundle
    people = bundle.setdefault("people", {})
    by_login = {}
    for m, logins in owners.items():
        for x in logins:
            by_login.setdefault(x, []).append(m)
    for x, mods in by_login.items():
        if x in people:
            people[x]["module_owner_of"] = sorted(mods)
    return bundle


def annotate_automation(bundle, registry=None):
    """`automation` on the people rows of the automation accounts:
    {kind: registry (the operator's tag) | account_type (the API's account
    type, `author_type` Bot) | app_login (a `name[bot]` login) |
    automation_name (a git name automation commits under), [on_behalf_of]
    {login, basis, pr} (_on_behalf_of over the PRs it opened)}. A login
    the registry tags NOT_AUTOMATION_TAG gets `automation: null` (the
    operator's word wins). In place."""
    registry = registry or {}
    people = bundle.setdefault("people", {})
    types_ = _account_types(bundle)
    activity = _pr_activity_index(bundle) or {}
    instant = window_end_instant(bundle.get("meta"))
    for login in sorted(set(people) | set(types_)):
        if registry.get(login) is False:
            people.setdefault(login, {})["automation"] = None
            continue
        if registry.get(login):
            kind = "registry"
        elif types_.get(login) == "Bot":
            kind = "account_type"
        elif derive.is_bot_login(login):
            kind = "app_login" if login.endswith("[bot]") else "automation_name"
        else:
            continue
        row = {"kind": kind}
        issues = {i.get("number"): i for i in bundle.get("issues") or []
                  if isinstance(i, dict)}
        by_pr = {}
        for p in bundle.get("prs") or []:
            if isinstance(p, dict) and p.get("author") == login:
                by = _on_behalf_of(p, activity.get(p.get("number")), instant, issues)
                if by:
                    by_pr[str(p.get("number"))] = by
        if by_pr:
            row["on_behalf_of"] = by_pr[min(by_pr, key=int)]
            if len(by_pr) > 1:          # several PRs: who each one works for
                row["on_behalf_of_by_pr"] = {k: by_pr[k] for k in sorted(by_pr, key=int)}
        people.setdefault(login, {})["automation"] = row
    return bundle


def _is_automation(bundle, login):
    """Is `login` an automation account in this view: its people row's
    `automation` (annotate_automation) when set, else derive.is_bot_login."""
    row = ((bundle or {}).get("people") or {}).get(login)
    if isinstance(row, dict) and "automation" in row:
        return row["automation"] is not None
    return derive.is_bot_login(login)


def _human_turns(bundle, t, instant):
    """(text turns, verdicts) by people (not automation) on the train's
    items by the instant: comments, review comments and reviews with text;
    reviews that are only a verdict (approved / changes requested /
    dismissed)."""
    issues, prs = _train_items(bundle, t)
    text = verdicts = 0
    for rec in issues + prs:
        for key in ("comments_list", "review_comments"):
            for c in rec.get(key) or []:
                if isinstance(c, dict) and c.get("author") \
                        and not _is_automation(bundle, c["author"]) \
                        and _not_after(c.get("created_at"), instant):
                    text += 1
        for r in rec.get("reviews") or []:
            if not isinstance(r, dict) or not r.get("author") \
                    or _is_automation(bundle, r["author"]) \
                    or not _not_after(r.get("submitted_at"), instant):
                continue
            if (r.get("body") or "").strip():
                text += 1
            elif r.get("state") in ("approved", "changes_requested", "dismissed"):
                verdicts += 1
    return text, verdicts


def _automation_only(bundle, t, instant):
    """The automation accounts that authored EVERY PR of train `t`, when no
    person took a conversation turn on its items; None otherwise."""
    _issues, prs = _train_items(bundle, t)
    if not t.get("prs") or len(prs) != len(t["prs"]):
        return None
    authors = sorted({p.get("author") for p in prs})
    if not all(a and _is_automation(bundle, a) for a in authors):
        return None
    if _human_turns(bundle, t, instant)[0]:
        return None
    return authors


def story_substance(bundle, t, instant):
    """What the record of a story holds beyond its opening: `discussion`
    (a person's comment, review comment or review text), `decision_only`
    (no such turn, but a person's verdict review, or an item merged /
    closed by the instant by someone who is not automation), else
    `opening_only`."""
    text, verdicts = _human_turns(bundle, t, instant)
    if text:
        return "discussion"
    if verdicts:
        return "decision_only"
    issues, prs = _train_items(bundle, t)
    for rec in issues + prs:
        if rec.get("state") != "closed" or not _not_after(rec.get("closed_at"), instant):
            continue
        closer = rec.get("merged_by") if rec.get("merged") else None
        ev = [e for e in rec.get("lifecycle") or [] if isinstance(e, dict)
              and e.get("event") == "closed" and _not_after(e.get("created_at"), instant)]
        closer = closer or (ev[-1].get("actor") if ev else None)
        if not closer or not _is_automation(bundle, closer):
            return "decision_only"
    return "opening_only"


# GitHub's stand-in login for a deleted account: its comments and requests
# are on record, but nobody can take the next move as it.
DELETED_ACCOUNT = "ghost"


def _cannot_act(bundle, login):
    """True for an actor who never holds the next move: an automation
    account (_is_automation) or a deleted account (DELETED_ACCOUNT)."""
    return isinstance(login, str) and bool(login) and (
        login == DELETED_ACCOUNT or _is_automation(bundle, login))


def last_push(bundle, pr, instant):
    """The time of the PR author's latest own change by the instant: its
    latest own commit (own_change) in `pr_history`, else the latest commit
    `pr_activity` read; None when neither is recorded."""
    row = _pr_history_index(bundle).get(pr.get("number"))
    times = [c.get("committed_at") for c in _pr_own_commits_as_of(row, instant)
             if c.get("committed_at")]
    if not times:
        arow = (_pr_activity_index(bundle) or {}).get(pr.get("number")) or {}
        times = [c.get("committed_at") for c in arow.get("commits") or []
                 if isinstance(c, dict) and c.get("committed_at")
                 and _not_after(c["committed_at"], instant)]
    return max(times) if times else None


def _pending_requests(bundle, pr, instant):
    """The review requests standing on a PR at the instant (`pr_activity`
    review_requested not removed since), people and teams, automation
    reviewers left out."""
    arow = (_pr_activity_index(bundle) or {}).get(pr.get("number")) or {}
    pending = {}
    for e in sorted((e for e in arow.get("events") or [] if isinstance(e, dict)
                     and _not_after(e.get("at"), instant)),
                    key=lambda e: e.get("at") or ""):
        who = e.get("requested")
        if not who:
            continue
        if e.get("event") == "review_requested":
            pending[who] = e.get("at")
        elif e.get("event") == "review_request_removed":
            pending.pop(who, None)
    for r in pr.get("reviews") or []:
        if isinstance(r, dict) and _not_after(r.get("submitted_at"), instant) \
                and (r.get("submitted_at") or "") >= (pending.get(r.get("author")) or "~"):
            pending.pop(r.get("author"), None)
    return sorted(w for w in pending if w.startswith("team:")
                  or not _cannot_act(bundle, w))


REVIEW_STATES = ("none", "requested", "automated_only", "commented",
                 "changes_requested", "approved", "approved_stale")
_REVIEW_PHRASE = {"none": "no review requested or given",
                  "requested": "review requested, no review by a person yet",
                  "automated_only": "only an automated review, none by a person",
                  "commented": "reviewed with comments, no verdict",
                  "changes_requested": "changes requested",
                  "approved": "approved",
                  "approved_stale": "approved before the author's latest change"}


def review_state(bundle, pr, instant):
    """THE review state of a PR as of the instant, for every reader:
    {state, rule, as_of, [basis {reviewer, at, url}], [last_push],
    [requested]}. `state` (REVIEW_STATES): `changes_requested` (a person's
    latest verdict is a change request), `approved` (a person approved, no
    change request outstanding, no own change of the author since),
    `approved_stale` (approved, but the author changed the PR after the
    latest approval: last_push), `commented` (a person reviewed or commented
    on the diff without a verdict), `automated_only` (the only reviews are
    by automation accounts), `requested` (a review is requested, nobody
    reviewed), `none`. The author's own reviews never count."""
    author = pr.get("author")
    human, auto = [], []
    for r in pr.get("reviews") or []:
        if not isinstance(r, dict) or not r.get("author") or r["author"] == author \
                or not _not_after(r.get("submitted_at"), instant):
            continue
        (auto if _cannot_act(bundle, r["author"]) else human).append(r)
    diff_comments = [c for c in pr.get("review_comments") or []
                     if isinstance(c, dict) and c.get("author")
                     and c["author"] != author and not _cannot_act(bundle, c["author"])
                     and _not_after(c.get("created_at"), instant)]
    verdicts = {}
    for r in sorted(human, key=lambda r: r.get("submitted_at") or ""):
        if r.get("state") in ("approved", "changes_requested", "dismissed"):
            verdicts[r["author"]] = r
    out = {"as_of": instant}
    changes = [r for r in verdicts.values() if r.get("state") == "changes_requested"]
    approvals = [r for r in verdicts.values() if r.get("state") == "approved"]
    push = last_push(bundle, pr, instant)
    basis = None
    if changes:
        basis = max(changes, key=lambda r: r.get("submitted_at") or "")
        state, rule = "changes_requested", "a person's latest verdict is a change request"
    elif approvals:
        basis = max(approvals, key=lambda r: r.get("submitted_at") or "")
        if push and push > (basis.get("submitted_at") or ""):
            state, rule = "approved_stale", "approved, then the author changed the PR"
            out["last_push"] = push
        else:
            state, rule = "approved", "approved, no change request, no change since"
    elif human or diff_comments:
        basis = max(human or diff_comments,
                    key=lambda r: r.get("submitted_at") or r.get("created_at") or "")
        state, rule = "commented", "reviewed by a person without a verdict"
    elif auto:
        basis = max(auto, key=lambda r: r.get("submitted_at") or "")
        state, rule = "automated_only", "reviewed by automation accounts only"
        req = _pending_requests(bundle, pr, instant)
        if req:
            out["requested"] = req          # who a person's review is asked of
    else:
        req = _pending_requests(bundle, pr, instant)
        if req:
            state, rule = "requested", "a review is requested, nobody reviewed"
            out["requested"] = req
        else:
            state, rule = "none", "no review requested or given"
    out.update(state=state, rule=rule)
    if basis is not None:
        out["basis"] = {"reviewer": basis.get("author"),
                        "at": basis.get("submitted_at") or basis.get("created_at"),
                        "url": basis.get("url")}
    return {k: out[k] for k in ("state", "rule", "as_of", "basis", "last_push",
                                 "requested") if k in out}


def annotate_review_state(bundle):
    """`review_state` (review_state) on every PR row of a story-layer view
    (one carrying `pr_history`). In place."""
    if "pr_history" not in bundle:
        return bundle
    instant = window_end_instant(bundle.get("meta"))
    for p in bundle.get("prs") or []:
        if isinstance(p, dict) and p.get("number") is not None:
            p["review_state"] = review_state(bundle, p, instant)
    return bundle


def last_human_event(bundle, t, instant):
    """THE last human event of a train by the instant — one definition for
    idle days, the group rule and the slice: the openings, comments, review
    comments, reviews and lifecycle events of its issues / PRs, their
    merges / closes, its merged commits, its PRs' own commits
    (`pr_history`) and their recorded events (`pr_activity`: review
    requests, assignments, ...), each by someone who can act
    (_cannot_act: not automation, not a deleted account). {at, kind, actor,
    url} or None."""
    issues, prs = _train_items(bundle, t)
    cands = []

    def add(at, kind, actor, url):
        if at and _not_after(at, instant) and _parse_ts(at) is not None \
                and not (actor and _cannot_act(bundle, actor)):
            cands.append((_parse_ts(at), at, kind, actor, url))
    for rec in issues + prs:
        add(rec.get("created_at"), "opened", rec.get("author"), rec.get("url"))
        for key, ts, kind in (("comments_list", "created_at", "comment"),
                              ("review_comments", "created_at", "review_comment"),
                              ("reviews", "submitted_at", "review")):
            for c in rec.get(key) or []:
                if isinstance(c, dict):
                    add(c.get(ts), kind, c.get("author"), c.get("url"))
        for e in rec.get("lifecycle") or []:
            if isinstance(e, dict):
                add(e.get("created_at"), e.get("event") or "lifecycle", e.get("actor"),
                    rec.get("url"))
        if rec.get("merged") and rec.get("merged_at"):
            add(rec["merged_at"], "merged", rec.get("merged_by"), rec.get("url"))
        elif rec.get("closed_at") and rec.get("state") == "closed":
            add(rec["closed_at"], "closed", None, rec.get("url"))
    by_sha = {c.get("sha"): c for c in bundle.get("commits") or []}
    for sha in t.get("commits") or []:
        c = by_sha.get(sha) or {}
        add(c.get("committed_at") or c.get("date"), "commit", c.get("author"),
            derive._commit_url(bundle, sha))
    history = _pr_history_index(bundle)
    activity = _pr_activity_index(bundle) or {}
    for p in prs:
        for c in _pr_own_commits_as_of(history.get(p.get("number")), instant):
            if not derive.is_bot_login(c.get("author")):
                add(c.get("committed_at"), "commit", p.get("author"),
                    _pr_commit_url(p, c.get("sha")))
        for e in (activity.get(p.get("number")) or {}).get("events") or []:
            if isinstance(e, dict):
                add(e.get("at"), e.get("event"), e.get("actor"), _event_url(p, e))
    if not cands:
        return None
    _dt, at, kind, actor, url = max(cands, key=lambda c: (c[0], c[2]))
    return {"at": at, "kind": kind, "actor": actor, "url": url}


def annotate_last_human_event(bundle):
    """For an open train (`in_flight`) of a story-layer view: `effort`
    gains `last_human_event` (last_human_event) and its `last_activity_at`
    / `idle_days` (to `meta.ref_date`, else the window's end) follow it, so
    rows, group rules and slices read one definition. In place."""
    if "pr_history" not in bundle:
        return bundle
    meta = bundle.get("meta") or {}
    instant = window_end_instant(meta)
    ref = meta.get("ref_date") or window_period(meta).get("to")
    ref_dt = _parse_ts(ref[:10] + "T00:00:00Z") if ref else None
    for t in bundle.get("trains") or []:
        if t.get("outcome") != "in_flight":
            continue
        ev = last_human_event(bundle, t, instant)
        eff = t.setdefault("effort", {})
        if ev is None:
            continue
        eff["last_human_event"] = ev
        eff["last_activity_at"] = ev["at"]
        if ref_dt is not None:
            eff["idle_days"] = max(0, (ref_dt.date() - _parse_ts(ev["at"]).date()).days)
            eff["stalled"] = eff["idle_days"] > TRAIN_STALL_DAYS
    return bundle


def status_reason(bundle, t, instant):
    """Why an open story stands where it does, in words a writer can use:
    "opened N days before the window's end; <review state of its latest
    open PR>; last human event M days before the end (<kind> by <actor>)".
    None for a train that is not open."""
    if t.get("outcome") != "in_flight":
        return None
    end = _parse_ts(instant)
    issues, prs = _train_items(bundle, t)
    parts = []
    opened = [d for d in (_parse_ts(r.get("created_at")) for r in issues + prs) if d]
    if opened and end:
        parts.append("opened {} days before the window's end".format(
            (end.date() - min(opened).date()).days))
    open_prs = sorted((p for p in prs if p.get("state") == "open"),
                      key=lambda p: p.get("created_at") or "")
    if open_prs:
        rs = open_prs[-1].get("review_state") or review_state(bundle, open_prs[-1], instant)
        parts.append("PR #{}: {}".format(open_prs[-1].get("number"),
                                         _REVIEW_PHRASE.get(rs["state"], rs["state"])))
    ev = (t.get("effort") or {}).get("last_human_event") or \
        last_human_event(bundle, t, instant)
    if ev and end:
        parts.append("last human event {} days before the end ({}{})".format(
            (end.date() - _parse_ts(ev["at"]).date()).days, ev.get("kind"),
            " by " + ev["actor"] if ev.get("actor") else ""))
    return "; ".join(parts) or None


# --- the module a story row is about -----------------------------------------

_ISSUE_FORM_MODULE_RE = re.compile(
    r"^#{2,4}[ \t]*Module Name[ \t]*\n(.*?)(?=^#{1,4}[ \t]|\Z)", re.M | re.S | re.I)
_CC_SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\(([^)]+)\)!?:")
MODULES_SOURCES = ("merged_commits", "pr_commits", "issue_form", "title_scope",
                   "none")


def module_url(bundle, area):
    """THE link of a module folder: its tree at the commit the view's
    structure was read at (meta.clone_sha; the base branch when unknown),
    the repository's tree for a repository-root module (`main.tf`). None
    without the repository or an area."""
    web = _repo_web(bundle)
    if not web or not area:
        return None
    meta = (bundle or {}).get("meta") or {}
    ref = meta.get("clone_sha") or meta.get("base_branch") or "HEAD"
    if area == "main.tf":
        return "{}/tree/{}".format(web, ref)
    return "{}/tree/{}/{}".format(web, ref, area)


def module_urls(bundle, areas):
    """{area: module_url} of the areas that have one."""
    out = {}
    for a in areas or []:
        u = module_url(bundle, a)
        if u:
            out[a] = u
    return out


def _as_module(text):
    """A module area named in text (an issue form field, a title scope):
    `avm/...` or `res|ptn|utl/...` (read as `avm/...`), kept only when it
    is a module area by its path (module_kind); None otherwise."""
    a = (text or "").strip().strip("`'\"").strip().rstrip("/")
    if a.startswith(("res/", "ptn/", "utl/")):
        a = "avm/" + a
    return a if a and " " not in a and module_kind(a) is not None else None


def _areas_of_paths(ctx, paths):
    """The module areas (module_kind) of `paths`."""
    files = sorted(set(ctx.seen_files) | set(paths))
    out = set()
    for p in paths:
        a = ctx.area_of(p)
        if a and module_kind(a, files) is not None:
            out.add(a)
    return sorted(out)


def _form_modules(rec):
    out = []
    for m in _ISSUE_FORM_MODULE_RE.finditer((rec.get("body") or "").replace("\r\n", "\n")):
        for line in m.group(1).splitlines():
            a = _as_module(line)
            if a and a not in out:
                out.append(a)
    return out


def _scope_modules(rec):
    m = _CC_SCOPE_RE.match(rec.get("title") or "")
    if not m:
        return []
    return [a for a in (_as_module(s) for s in m.group(1).split(",")) if a]


def _pr_own_paths(bundle, n, instant):
    return sorted({x for c in _pr_own_commits_as_of(_pr_history_index(bundle).get(n),
                                                     instant)
                   for x in c.get("paths") or []})


def record_modules(bundle, ctx, typ, rec, instant):
    """(modules, modules_source) of one issue / PR: a PR with merged code
    from its merged commits' paths; else a PR from its own commits' paths
    (pr_history, as of the instant); an issue from its form's `Module Name`
    field; else the title's conventional-commit scope; else ([], none).
    Module areas only (module_kind)."""
    if typ == "pr":
        merged = [f for c in bundle.get("commits") or []
                  if c.get("pr") == rec.get("number") for f in c.get("files") or []]
        if merged:
            return _areas_of_paths(ctx, merged), "merged_commits"
        got = _areas_of_paths(ctx, _pr_own_paths(bundle, rec.get("number"), instant))
        if got:
            return got, "pr_commits"
    else:
        got = _form_modules(rec)
        if got:
            return got, "issue_form"
    got = _scope_modules(rec)
    return (got, "title_scope") if got else ([], "none")


_MODULE_ENTRY_FILES = ("main.bicep", "main.tf")


def proposed_modules(bundle, pr, instant):
    """The module areas a PR proposes as NEW: its own commits (by the
    instant) add the module's entry file (`main.bicep` / `main.tf`) — the
    folder had none on the branch's base — read from the commits' `added`
    paths (gather keeps them); never from the title. [] when the history
    holds no `added` paths (gathered before they were kept)."""
    out = set()
    for c in _pr_own_commits_as_of(_pr_history_index(bundle).get(pr.get("number")),
                                   instant):
        for path in c.get("added") or []:
            folder, _, base = path.rpartition("/")
            # a module folder (gather.is_module_folder, the rule the tree's
            # `module_added` events use): a test asset's main.bicep is not one
            if base in _MODULE_ENTRY_FILES and gather.is_module_folder(folder) \
                    and module_kind(folder, [path]) is not None:
                out.add(folder)
    return sorted(out)


def train_modules(bundle, ctx, t, instant):
    """(modules, modules_source) of a train: with merged code, the module
    areas its merged commits touched (`merged_commits`); else the module
    areas of its open PRs' own commits (`pr_commits`), else its issues'
    form field (`issue_form`), else the conventional-commit scope of its
    titles (`title_scope`), else ([], `none`). Never a guess from prose."""
    if t.get("commits"):
        by_sha = {c.get("sha"): c for c in bundle.get("commits") or []}
        files = [f for s in t["commits"] for f in (by_sha.get(s) or {}).get("files") or []]
        return _areas_of_paths(ctx, files), "merged_commits"
    issues, prs = _train_items(bundle, t)
    paths = sorted({x for p in prs for x in _pr_own_paths(bundle, p.get("number"), instant)})
    got = _areas_of_paths(ctx, paths)
    if got:
        return got, "pr_commits"
    got = sorted({a for i in issues for a in _form_modules(i)})
    if got:
        return got, "issue_form"
    got = sorted({a for r in prs + issues for a in _scope_modules(r)})
    return (got, "title_scope") if got else ([], "none")


def _module_ctx(bundle):
    rules = path_class_rules(((bundle.get("module_changes") or {})
                              .get("path_rules")) or ())
    return _ModuleCtx(bundle, rules)


# --- owner response requested --------------------------------------------------

def _owner_response_patterns(bundle):
    """The label patterns of `owner_response_requested`: the gathered
    fact's own (`module_ownership.label_patterns`), else the default."""
    pats = ((bundle.get("module_ownership") or {}).get("label_patterns") or {})
    rx = pats.get("owner_response_requested")
    if rx is None:
        rx = gather.OWNERSHIP_LABEL_PATTERNS["owner_response_requested"]
    return [re.compile(x, re.I) for x in rx]


def _owner_response_label(rec, rxs):
    return next((lbl for lbl in rec.get("labels") or []
                 if isinstance(lbl, str) and any(r.search(lbl) for r in rxs)), None)


def _owners_named(bundle, rec, instant):
    """The module owners an automation comment on the item names (@login,
    in a comment that speaks of the module owner), when recorded."""
    for c in rec.get("comments_list") or []:
        if isinstance(c, dict) and _is_automation(bundle, c.get("author")) \
                and _not_after(c.get("created_at"), instant) \
                and re.search(r"\bmodule\W+owner", c.get("body") or "", re.I):
            named = [m for m in _mentions(c.get("body"), exclude={c.get("author")})
                     if m != rec.get("author")]
            if named:
                return named
    return None


def owner_response_request(bundle, ctx, typ, rec, instant, rxs):
    """The owner response request an issue / PR carries (a label matching
    the `owner_response_requested` patterns): {kind, module, item {type,
    number, url}, date (the day it opened: label times are not recorded),
    people [author], via (the label), source, [owners]}; None without
    one."""
    lbl = _owner_response_label(rec, rxs)
    if not lbl:
        return None
    module = gather._module_of_item(rec)
    if not module:
        mods, _src = record_modules(bundle, ctx, typ, rec, instant)
        module = mods[0] if len(mods) == 1 else None
    item = {"type": typ, "number": rec.get("number"), "url": rec.get("url")}
    out = {"kind": "owner_response_requested", "module": module, "item": item,
           "date": (rec.get("created_at") or "")[:10],
           "people": [p for p in [rec.get("author")] if p], "via": lbl,
           "source": dict(item)}
    owners = _owners_named(bundle, rec, instant)
    if owners:
        out["owners"] = owners
    elif module and module_owners(bundle).get(module):
        # the module's owners at the window's end (its owner file)
        out["owners"] = module_owners(bundle)[module]
        out["owners_source"] = "owner_file"
    return out


def _ownership_event_key(e):
    return (e.get("date") or "", e.get("at") or "",
            e.get("seq") if isinstance(e.get("seq"), int) else 0,
            e.get("kind") or "", e.get("module") or "")


LIFECYCLE_EVENT_KINDS = ("deprecated", "undeprecated", "module_added", "module_removed")
MODULE_INFO_KEYS = ("display_name", "description", "resource_type", "lifecycle")


def module_info(bundle, area):
    """What a module IS, from the tree (the module's metadata file and
    lifecycle markers, `module_ownership.modules_at_end`, the state at the
    view's end): {display_name?, description?, resource_type?, lifecycle};
    {} when not recorded."""
    row = (((bundle or {}).get("module_ownership") or {}).get("modules_at_end")
           or {}).get(area)
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in MODULE_INFO_KEYS if row.get(k)}


def annotate_module_lifecycle(bundle):
    """THE module lifecycle, from the tree (`module_ownership`: the state at
    the view's end and the window's lifecycle events — deprecation markers
    added / removed with their text, entry files added / removed on the
    mainline). On each `module_changes.modules[]` row: what the module is
    (module_info: display_name, description, resource_type) as
    `module_lifecycle` plus those fields, and `lifecycle_events` (the
    module's events in the view); `module_changes.lifecycle_check` compares
    the module layer's own `new` / `removed` / `deprecated` statuses with
    the tree's events: {agree, only_module_layer, only_tree} per status.
    Nothing without the fact. In place."""
    mo = bundle.get("module_ownership")
    mc = bundle.get("module_changes")
    if not isinstance(mo, dict) or not isinstance(mc, dict):
        return bundle
    evs = {}
    for e in mo.get("events") or []:
        if isinstance(e, dict) and e.get("kind") in LIFECYCLE_EVENT_KINDS:
            evs.setdefault(e.get("module"), []).append(
                {k: e[k] for k in ("kind", "date", "marker_text", "source", "people")
                 if k in e})
    for m in mc.get("modules") or []:
        info = module_info(bundle, m.get("area"))
        if info.get("lifecycle"):
            m["module_lifecycle"] = info.pop("lifecycle")
        m.update(info)
        if evs.get(m.get("area")):
            m["lifecycle_events"] = evs[m["area"]]
    check = {}
    for status, kinds in (("new", ("module_added",)), ("removed", ("module_removed",)),
                          ("deprecated", ("deprecated",))):
        layer = {m.get("area") for m in mc.get("modules") or [] if m.get("status") == status}
        tree = {mod for mod, rows in evs.items()
                if any(r["kind"] in kinds for r in rows)}
        if layer or tree:
            check[status] = {"agree": sorted(layer & tree),
                             "only_module_layer": sorted(layer - tree),
                             "only_tree": sorted(x for x in tree - layer if x)}
    if check:
        mc["lifecycle_check"] = check
    return bundle


def annotate_module_ownership(bundle):
    """On a view carrying `module_ownership`: the `owner_response_requested`
    events of the items opened in the window that carry the label (read off
    the view's records, so an existing store needs no recompute), and
    `module_url` (module_url) on every event and `without_owner` row naming
    a module. In place."""
    mo = bundle.get("module_ownership")
    if not isinstance(mo, dict):
        return bundle
    ctx = _module_ctx(bundle)
    instant = window_end_instant(bundle.get("meta"))
    start = window_period(bundle.get("meta") or {}).get("from") or ""
    rxs = _owner_response_patterns(bundle)
    events = list(mo.get("events") or [])
    have = {(e.get("kind"), json.dumps(e.get("item") or e.get("source"), sort_keys=True))
            for e in events if isinstance(e, dict)}
    for typ, key in (("issue", "issues"), ("pr", "prs")):
        for rec in bundle.get(key) or []:
            if not isinstance(rec, dict):
                continue
            e = owner_response_request(bundle, ctx, typ, rec, instant, rxs)
            if e is None or e["date"] < start[:10] or \
                    not _not_after(rec.get("created_at"), instant):
                continue
            k = (e["kind"], json.dumps(e["item"], sort_keys=True))
            if k not in have:
                have.add(k)
                events.append(e)
    for e in events:
        if isinstance(e, dict) and e.get("module") and module_url(bundle, e["module"]):
            e["module_url"] = module_url(bundle, e["module"])
    for key in ("without_owner", "deprecated_modules", "stub_modules"):
        for r in mo.get(key) or []:
            if isinstance(r, dict) and r.get("module") and module_url(bundle, r["module"]):
                r["module_url"] = module_url(bundle, r["module"])
    mo["events"] = sorted(events, key=_ownership_event_key)
    return bundle


def annotate_train_modules(bundle):
    """On every train of a story-layer view: `modules` / `modules_source`
    (train_modules — the module areas every consumer uses), and for a train
    with no merged code `pr_code_areas` (every code area its PRs' own
    commits touched as of the view's end; `code_areas` stays the merged
    commits' areas). In place."""
    if "pr_history" not in bundle:
        return bundle
    ctx = _module_ctx(bundle)
    instant = window_end_instant(bundle.get("meta"))
    for t in bundle.get("trains") or []:
        mods, source = train_modules(bundle, ctx, t, instant)
        t["modules"], t["modules_source"] = mods, source
        if not t.get("commits"):
            _issues, prs = _train_items(bundle, t)
            areas = {ctx.area_of(x) for p in prs
                     for x in _pr_own_paths(bundle, p.get("number"), instant)}
            t["pr_code_areas"] = sorted(a for a in areas if a)
    return bundle


def build_story_list(bundle):
    """`stories.all` — every story of the window: [{id, group, state,
    group_rule, title, modules (module areas only: train_modules),
    modules_source, module_urls {area: module_url}, substance
    (story_substance), score, worthy, released?}] ordered by
    STORY_GROUPS then score; `stories.excluded` [{id, reason}] — trains
    with conversation that are not stories (idle beyond
    STORY_CARRY_MAX_DAYS); `stories.groups` {group: count};
    `stories.all_rule`. `stories.selected` / `more` / `quiet` are unchanged.
    Omitted when the view has no train. In place."""
    trains = bundle.get("trains") or []
    # Only for a view of a gathered story layer (it carries `pr_history`):
    # existing views and fixtures without it keep their exact shape (their
    # tests pin "no `stories` key when no train qualifies").
    if not trains or "pr_history" not in bundle:
        return bundle
    meta = bundle.get("meta") or {}
    start_s = window_period(meta).get("from")
    start = _parse_ts(start_s + "T00:00:00Z" if start_s and len(start_s) == 10
                      else start_s)
    instant = window_end_instant(meta)
    end = _parse_ts(instant)
    history = _pr_history_index(bundle)
    mc = bundle.get("module_changes")
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    ctx = _module_ctx(bundle)
    rows, excluded = [], []
    for t in trains:
        worthy = story_worthy(bundle, t, start, end, history)
        if worthy is None:
            idle = (t.get("effort") or {}).get("idle_days")
            if not t.get("discussion") or t.get("outcome") != "in_flight":
                continue
            if not isinstance(idle, (int, float)) or idle > STORY_CARRY_MAX_DAYS:
                excluded.append({"id": t["id"], "reason": "no event in the window, "
                                 "{} days idle (over {})".format(
                                     idle, STORY_CARRY_MAX_DAYS)})
                continue
        group, state, rule = story_group(bundle, t, worthy, instant, mc)
        _d, total, _r = story_score(t, mc, pr_index)
        issues, prs = _train_items(bundle, t)
        first = (issues + prs)[0] if issues + prs else {}
        mods, source = train_modules(bundle, ctx, t, instant)
        row = {"id": t["id"], "group": group, "state": state, "group_rule": rule,
               "title": first.get("title"),
               "modules": mods, "modules_source": source,
               "module_urls": module_urls(bundle, mods),
               "module_info": {a: module_info(bundle, a) for a in mods
                               if module_info(bundle, a)},
               "substance": story_substance(bundle, t, instant),
               "status_reason": status_reason(bundle, t, instant),
               "new_module_proposed": sorted({a for p in _train_items(bundle, t)[1]
                                              for a in p.get("proposes_new_module")
                                              or []}),
               "score": round(total, 1),
               "worthy": worthy or "carried over"}
        if state == "landed":
            rel = train_released(bundle, t)
            if rel:
                row["released"] = rel
        rows.append(row)
    if not rows and not excluded:
        return bundle
    rows.sort(key=lambda r: (STORY_GROUPS.index(r["group"]), -r["score"], r["id"]))
    st = bundle.setdefault("stories", {"selected": [], "more": [],
                                       "rule": _story_rule()})
    st["all"] = rows
    st["groups"] = {g: sum(1 for r in rows if r["group"] == g) for g in STORY_GROUPS
                    if any(r["group"] == g for r in rows)}
    if excluded:
        st["excluded"] = excluded
    st["all_rule"] = {"stalled_days": STORY_STALLED_DAYS,
                      "carry_max_days": STORY_CARRY_MAX_DAYS,
                      "groups": list(STORY_GROUPS)}
    return bundle


# --- plumbing themes ----------------------------------------------------------
#
# "What's been engineered?": the plumbing PRs of the window grouped by the
# shared paths they touch. Facts only; a writer names the theme.

PLUMBING_THEMES = (
    ("ci_workflows", "CI workflows",
     lambda p: p.startswith((".github/workflows/", ".github/actions/",
                             ".azuredevops/", ".pipelines/", "pipelines/"))),
    ("pipeline_utilities", "Shared pipeline utilities",
     lambda p: p.startswith("utilities/")
     and not p.startswith("utilities/e2e-template-assets/")),
    ("test_environment", "Test fixtures and test environment",
     lambda p: "/tests/" in "/" + p or "/e2e/" in "/" + p
     or p.startswith("utilities/e2e-template-assets/")),
    ("docs_generation", "README and docs generation",
     lambda p: p.rsplit("/", 1)[-1].lower() == "readme.md" or p.startswith("docs/")
     or "/docs/" in p or (p.lower().endswith(".md")
                          and not p.lower().endswith("changelog.md"))),
    ("repository_policy", "Repository policy files",
     lambda p: p.rsplit("/", 1)[-1] in ("CODEOWNERS", "LICENSE", ".gitignore",
                                         ".gitattributes", ".editorconfig",
                                         "bicepconfig.json", "dependabot.yml")
     or p.startswith((".github/", ".vscode/", ".devcontainer/"))),
)
_DEPENDENCY_TITLE_RE = re.compile(r"\b(?:bump|update|upgrade)s?\b|\bdeps?\b", re.I)
PLUMBING_THEME_PATHS_MAX = 50
PLUMBING_THEME_SUBJECTS_MAX = 1000


def _pr_paths(bundle, n, history, instant=None):
    paths = set()
    for c in bundle.get("commits") or []:
        if c.get("pr") == n:
            paths.update(f for f in c.get("files") or [] if f)
    for c in _pr_own_commits_as_of(history.get(n), instant):
        paths.update(c.get("paths") or [])
    return paths


def plumbing_theme(pr, paths, automated=None):
    """(id, title_hint) of one plumbing PR: `dependency_updates` for an
    automation account's update PR (`automated`: whether its author is
    one, else derive.is_bot_login), else the theme of PLUMBING_THEMES
    covering most of its files (ties: the earlier theme), else
    `other_plumbing`."""
    if automated is None:
        automated = derive.is_bot_login(pr.get("author"))
    if automated and \
            _DEPENDENCY_TITLE_RE.search(pr.get("title") or ""):
        return "dependency_updates", "Dependency updates by automation"
    # each path counts for the FIRST theme it belongs to (a workflow file is
    # a CI workflow, not also a `.github/` policy file)
    first = {}
    for x in paths:
        for i, (_tid, _hint, test) in enumerate(PLUMBING_THEMES):
            if test(x):
                first[i] = first.get(i, 0) + 1
                break
    counts = [(-n, i, PLUMBING_THEMES[i][0], PLUMBING_THEMES[i][1])
              for i, n in first.items()]
    if not counts:
        return "other_plumbing", "Other plumbing"
    _n, _i, tid, hint = min(counts)
    return tid, hint


def build_plumbing_themes(bundle):
    """`plumbing_themes` — the window's plumbing PRs (module layer:
    `plumbing` true, or the PRs of a plumbing story) grouped by
    plumbing_theme: [{id, title_hint, paths (at most
    PLUMBING_THEME_PATHS_MAX; paths_more), prs: [{number, title, author,
    state, merged_at, url}], stories (the trains of those PRs in the
    `plumbing` story group), related_stories [{id, group}] (the trains of
    those PRs in another group, or in none: group null), modules [{module,
    url}] (the module areas of its paths), people, evidence_counts {commits,
    comments, docs, interface, patches}, subjects (commit subjects, at
    most PLUMBING_THEME_SUBJECTS_MAX), issues (linked closing issues)}].
    Omitted when there is none. In place."""
    mc = bundle.get("module_changes") or {}
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    history = _pr_history_index(bundle)
    instant = window_end_instant(bundle.get("meta"))
    train_of = {n: t["id"] for t in bundle.get("trains") or []
                for n in t.get("prs") or []}
    numbers = {r.get("number") for r in mc.get("prs") or [] if r.get("plumbing")}
    for r in (bundle.get("stories") or {}).get("all") or []:
        if r.get("group") == "plumbing":
            t = next((x for x in bundle.get("trains") or [] if x["id"] == r["id"]), {})
            numbers.update(t.get("prs") or [])
    # every automation account's PR open in, or carried into, the window
    # (a dependency update waiting since September is plumbing of October)
    start = window_period(bundle.get("meta") or {}).get("from") or ""
    carried = set()
    for p in bundle.get("prs") or []:
        n = p.get("number")
        if not p.get("author") or not _is_automation(bundle, p["author"]) \
                or not _not_after(p.get("created_at"), instant):
            continue
        ended = p.get("merged_at") or p.get("closed_at")
        in_window = any((x or "")[:10] >= start[:10] and _not_after(x, instant)
                        for x in (p.get("created_at"), ended) if x)
        open_then = not ended or not _not_after(ended, instant)
        if in_window or open_then:
            numbers.add(n)
            if not in_window:
                carried.add(n)
    themes = {}
    for n in sorted(x for x in numbers if x in pr_index):
        p = pr_index[n]
        paths = _pr_paths(bundle, n, history, instant)
        tid, hint = plumbing_theme(p, paths, _is_automation(bundle, p.get("author")))
        th = themes.setdefault(tid, {"id": tid, "title_hint": hint, "_paths": set(),
                                     "prs": [], "_stories": set(), "_people": set(),
                                     "evidence_counts": {"commits": 0, "comments": 0,
                                                         "docs": 0, "interface": 0,
                                                         "patches": 0},
                                     "subjects": [], "_issues": set()})
        th["_paths"] |= paths
        th["prs"].append({"number": n, "title": p.get("title"),
                          "author": p.get("author"), "state": p.get("state"),
                          "merged_at": p.get("merged_at"), "url": p.get("url"),
                          "carried_over": n in carried,
                          **({"closed_unmerged": p["closed_unmerged"]}
                             if p.get("closed_unmerged") else {}),
                          **({"review_state": p["review_state"]}
                             if p.get("review_state") else {}),
                          "body": p.get("body") or "",
                          "body_length": len(p.get("body") or ""),
                          "reviewers": sorted({r.get("author") for r in
                                               p.get("reviews") or []
                                               if isinstance(r, dict) and r.get("author")
                                               and _not_after(r.get("submitted_at"),
                                                              instant)})})
        if n in train_of:
            th["_stories"].add(train_of[n])
        th["_people"].add(p.get("author"))
        th["_people"].update(r.get("author") for r in p.get("reviews") or []
                             if isinstance(r, dict))
        th["_issues"].update(p.get("closes") or [])
        row = history.get(n) or {}
        ev = th["evidence_counts"]
        own = _pr_own_commits_as_of(row, instant)
        for c in own:
            ev["commits"] += 1
            for k in ("comments", "docs", "interface"):
                ev[k] += len(c.get(k) or [])
            if not c.get("subject"):
                continue
            if len(th["subjects"]) < PLUMBING_THEME_SUBJECTS_MAX:
                sub = {"pr": n, "sha": c.get("sha"), "subject": c["subject"]}
                if c.get("body"):
                    sub["body"] = c["body"]
                th["subjects"].append(sub)
            else:
                th["subjects_omitted"] = th.get("subjects_omitted", 0) + 1
        shas = {c.get("sha") for c in own}
        ev["patches"] += len([x for x in row.get("patches") or []
                              if x.get("commit") in shas])
    if not themes:
        return bundle
    group_of = {r.get("id"): r.get("group")
                for r in (bundle.get("stories") or {}).get("all") or []}
    ctx = _module_ctx(bundle)
    out = []
    for tid in sorted(themes, key=lambda k: (-len(themes[k]["prs"]), k)):
        th = themes[tid]
        paths = sorted(th.pop("_paths"))
        th["paths"] = paths[:PLUMBING_THEME_PATHS_MAX]
        if len(paths) > PLUMBING_THEME_PATHS_MAX:
            th["paths_more"] = len(paths) - PLUMBING_THEME_PATHS_MAX
        ids = sorted(th.pop("_stories"))
        th["stories"] = [i for i in ids if group_of.get(i) == "plumbing"]
        related = [{"id": i, "group": group_of.get(i)} for i in ids
                   if group_of.get(i) != "plumbing"]
        if related:
            th["related_stories"] = related
        th["modules"] = [{"module": a, "url": module_url(bundle, a)}
                         for a in _areas_of_paths(ctx, paths)]
        th["people"] = sorted(x for x in th.pop("_people") if x)
        th["issues"] = sorted(th.pop("_issues"))
        out.append(th)
    bundle["plumbing_themes"] = out
    return bundle


def _story_rule():
    return {"min_exchanges": STORY_MIN_EXCHANGES,
            "min_in_window": STORY_MIN_IN_WINDOW,
            "min_score": STORY_MIN_SCORE, "top_n": STORY_TOP_N,
            "plumbing_min": STORY_PLUMBING_MIN,
            "silence_days": STORY_SILENCE_DAYS}


# --- code notes: comments, parameter descriptions, interface lines -------

_PARAM_DESC_RE = re.compile(r"^\s*@(?:sys\.)?(?:description|metadata)\s*\(")
_COMMENT_MARK_RE = re.compile(r"^\s*(?://+|#+|/\*+|\*+)\s*|\s*(?:\*+/|//+)\s*$")
_BANNER_PUNCT_RE = re.compile(r"[.,:;()'\"`!?\[\]{}<>@$%&|\\]")


def comment_class(subkind, text):
    """How a code comment reads: `parameter_description` (a Bicep
    `@description(...)` / `@metadata(...)` decorator), `todo` (a TODO /
    FIXME / HACK / XXX / BUG marker), `banner` (a section label: once the
    comment markers and decoration characters `= - * # / _ ~` are stripped,
    at most three words and no punctuation, e.g. "// Variables //",
    "// Parameters", "// ===== Outputs ====="), else `note` (a developer
    note). Pure."""
    if subkind == "todo":
        return "todo"
    t = text or ""
    if _PARAM_DESC_RE.match(t):
        return "parameter_description"
    core = _COMMENT_MARK_RE.sub("", t)
    core = re.sub(r"[=\-*#/_~]+", " ", core).strip()
    if not core or (len(core.split()) <= 3 and not _BANNER_PUNCT_RE.search(core)):
        return "banner"
    return "note"


def _note_rows(events, source):
    """Code-note rows from symbol events: comments are classified
    (comment_class; banners dropped and counted), parameter / variable /
    output events are `interface`. One row per comment line: the symbol
    extraction keys a comment by its text, so the lines of one file and
    commit are not in file order and are never joined into prose. Returns
    (rows, banners_dropped)."""
    rows, banners = [], 0
    for e in events:
        sub = e.get("subkind")
        change = e.get("change")
        if sub in ("comment", "todo"):
            text = e.get("after") if change == "add" else e.get("before")
            if not text or not text.strip().startswith((e.get("name") or "")[:20]):
                text = e.get("name") or text
            cls = comment_class(sub, e.get("name") or text)
            if cls == "banner":
                banners += 1
                continue
        elif sub in ("param", "variable", "output"):
            cls = "interface"
            text = None
        else:
            continue
        row = {"commit": e.get("commit"), "path": e.get("path"),
               "change": change, "class": cls, "source": source}
        if cls == "interface":
            row["kind"] = sub
            row["name"] = e.get("name")
            if e.get("before"):
                row["before"] = e["before"]
            if e.get("after"):
                row["after"] = e["after"]
        elif change == "change":
            row["before"] = e.get("before")
            row["after"] = e.get("after")
        else:
            row["text"] = text
        rows.append(row)
    return rows, banners


def _note_chars(row):
    return sum(len(row.get(k) or "") for k in ("text", "before", "after", "name"))


_NOTE_PRIORITY = {"note": 0, "todo": 0, "parameter_description": 1,
                  "interface": 2}


def train_code_notes(bundle, train, history=None, instant=None):
    """What the code said along the way for one train: the comment /
    parameter-description / interface changes of its merged commits (the
    view's symbol events) and of its PRs' own commits (`pr_history` notes,
    as of the window's end). Returns (rows_by_commit {sha: [row]},
    by_module {module: {path: {added, removed, changed}}}, info) where info
    = {banners_dropped, budget, kept, omitted}. The rows kept are chosen
    within STORY_CODE_NOTES_BUDGET characters: developer notes and TODOs
    first, then parameter descriptions removed / changed, then added, then
    interface lines; each class in commit order."""
    history = history or {}
    shas = set(train.get("commits") or [])
    base_events = [e for e in bundle.get("symbol_events") or []
                   if e.get("commit") in shas]
    rows, banners = _note_rows(base_events, "base")
    for n in train.get("prs") or []:
        row = history.get(n) or {}
        ok = {c["sha"] for c in _pr_own_commits_as_of(row, instant)}
        evs = [e for e in row.get("notes") or [] if e.get("commit") in ok]
        r2, b2 = _note_rows(evs, "pr_head")
        for r in r2:
            r["pr"] = n
        rows += r2
        banners += b2
    idx = derive.area_index(bundle.get("code_graph", {}))

    def module_of(path):
        return idx.get(path) or gather.classify_code_area(
            path, gather.DEFAULT_AREA_PATTERNS)
    by_module = {}
    for r in rows:
        counts = by_module.setdefault(module_of(r["path"]) or "", {}).setdefault(
            r["path"], {"added": 0, "removed": 0, "changed": 0})
        counts[{"add": "added", "drop": "removed"}.get(r["change"], "changed")] \
            += 1
    order = sorted(range(len(rows)), key=lambda i: (
        _NOTE_PRIORITY.get(rows[i]["class"], 3)
        + (0.5 if rows[i]["class"] == "parameter_description"
           and rows[i]["change"] == "add" else 0), i))
    used, keep = 0, set()
    for i in order:
        c = _note_chars(rows[i])
        if used + c <= STORY_CODE_NOTES_BUDGET:
            keep.add(i)
            used += c
    by_commit = {}
    for i, r in enumerate(rows):
        if i in keep:
            by_commit.setdefault(r["commit"], []).append(r)
    info = {"banners_dropped": banners, "budget": STORY_CODE_NOTES_BUDGET,
            "kept": len(keep), "omitted": len(rows) - len(keep)}
    return by_commit, by_module, info


# --- the story timeline ----------------------------------------------------

def _event_url(rec, e):
    """A citable link for a lifecycle event: the item page anchored at the
    event (`<item url>#event-<id>`), the form GitHub's own pages use."""
    if e.get("id") is not None and rec.get("url"):
        return "{}#event-{}".format(rec["url"], e["id"])
    return rec.get("url")


def _pr_commit_url(pr, sha):
    return "{}/commits/{}".format(pr.get("url"), sha) if pr.get("url") \
        else None


def _apply_story_budget(items):
    """Cap each item's text at STORY_ITEM_CAP, then fit the total in
    STORY_SLICE_BUDGET: cut the longest texts evenly (never below
    STORY_ITEM_FLOOR) and, if that is not enough, leave out whole texts from
    the middle of the timeline outward. Mutates `items`; returns the
    truncation record {item_cap, budget, chars_original, chars_kept, cut:
    [{index, url, kept, original}], text_omitted: [{index, url, chars}]}."""
    orig = {i: len(it["text"]) for i, it in enumerate(items)
            if isinstance(it.get("text"), str)}
    limit = {i: min(n, STORY_ITEM_CAP) for i, n in orig.items()}
    if sum(limit.values()) > STORY_SLICE_BUDGET:
        lo, hi = STORY_ITEM_FLOOR, STORY_ITEM_CAP
        while lo < hi:                                # largest fitting ceiling
            mid = (lo + hi + 1) // 2
            if sum(min(n, mid) for n in limit.values()) <= STORY_SLICE_BUDGET:
                lo = mid
            else:
                hi = mid - 1
        limit = {i: min(n, lo) for i, n in limit.items()}
    omitted = set()
    keyed = sorted(limit)
    if sum(limit.values()) > STORY_SLICE_BUDGET:
        mid = len(keyed) // 2
        spiral = []
        for k in range(len(keyed)):
            j = mid + ((k + 1) // 2) * (1 if k % 2 else -1)
            if 0 < j < len(keyed) - 1:
                spiral.append(keyed[j])
        for i in spiral:
            if sum(n for j, n in limit.items() if j not in omitted) \
                    <= STORY_SLICE_BUDGET:
                break
            omitted.add(i)
    cut, dropped = [], []
    for i in keyed:
        it = items[i]
        if i in omitted:
            dropped.append({"index": i, "url": it.get("url"), "chars": orig[i]})
            it["text"] = None
            it["text_omitted"] = orig[i]
        elif limit[i] < orig[i]:
            it["text"] = it["text"][:limit[i]]
            it["truncated_from"] = orig[i]
            cut.append({"index": i, "url": it.get("url"), "kept": limit[i],
                        "original": orig[i]})
    return {"item_cap": STORY_ITEM_CAP, "budget": STORY_SLICE_BUDGET,
            "chars_original": sum(orig.values()),
            "chars_kept": sum(n for i, n in limit.items() if i not in omitted),
            "cut": cut, "text_omitted": dropped}


REPLACED_COMMITS_SHOWN = 20     # replaced commits listed per force-push item


def _replaced_summary(rec):
    """A force-push's replaced history for the timeline: {status, summary,
    [commits [{sha, subject, files}]], [commits_more], [reason]}."""
    when = (rec.get("at") or "")[:10]
    if rec.get("status") != "read":
        return {"status": "unavailable", "reason": rec.get("reason"),
                "summary": "force-push on {}: what it replaced is unavailable ({})".format(
                    when, rec.get("reason") or "not read")}
    commits = rec.get("commits") or []
    n = len(commits) + (rec.get("commits_omitted") or 0)
    out = {"status": "read",
           "summary": "force-push on {} replaced {} commit(s){}".format(
               when, n, ": " + "; ".join(c.get("subject") or "" for c in commits[:5])
               if commits else ""),
           "commits": [{"sha": c.get("sha"), "subject": c.get("subject"),
                        "files": c.get("files")} for c in commits[:REPLACED_COMMITS_SHOWN]]}
    if n > REPLACED_COMMITS_SHOWN:
        out["commits_more"] = n - REPLACED_COMMITS_SHOWN
    return out


def _git_name_logins(bundle):
    """{git author name: login} from the window's walked commits whose
    author the gather resolved to a login (commits API / noreply email),
    only where the name resolved to one login. Never a guess."""
    seen = {}
    for c in bundle.get("commits") or []:
        if isinstance(c, dict) and c.get("login_resolved") and c.get("author_name") \
                and c.get("author"):
            seen.setdefault(c["author_name"], set()).add(c["author"])
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


_FAILED_PIPELINE_RE = re.compile(r"^\s*\[Failed pipeline\]\s*(\S+)", re.I)
_RUN_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/(\d+)"
                         r"(?:/job/(\d+))?")
_FAILED_JOB_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:failed\s+)?(job|step)s?\s*[:\-]\s*(.+)$",
                            re.I | re.M)


def failed_pipeline(issue):
    """For an automated "[Failed pipeline] <workflow>" issue: {workflow,
    [module], run_url, run_id, [job_url], [jobs], [steps]} parsed from its
    title and body (no log is downloaded); None for another issue."""
    m = _FAILED_PIPELINE_RE.match(issue.get("title") or "")
    if not m:
        return None
    wf = m.group(1)
    out = {"workflow": wf}
    parts = wf.split(".")
    if len(parts) >= 4 and parts[0] == "avm":
        out["module"] = "/".join(parts[:3]) + "/" + ".".join(parts[3:]) \
            if len(parts) > 4 else "/".join(parts)
    body = issue.get("body") or ""
    r = _RUN_URL_RE.search(body)
    if r:
        out["run_url"] = r.group(0) if not r.group(2) else \
            r.group(0).split("/job/")[0]
        out["run_id"] = int(r.group(1))
        if r.group(2):
            out["job_url"] = r.group(0)
    for kind, name in _FAILED_JOB_RE.findall(body):
        out.setdefault(kind.lower() + "s", []).append(name.strip())
    return out


def related_prs(bundle, pr, days=7):
    """PRs of the same author opened within `days` of `pr` that the record
    links to it explicitly: one names the other (`#N` / a link in its body,
    comments or review comments), or both close / reference the same item.
    [{number, title, state, url, basis}] — never from timing alone."""
    by_author = [p for p in bundle.get("prs") or [] if isinstance(p, dict)
                 and p.get("author") == pr.get("author")
                 and p.get("number") != pr.get("number")]
    a = _parse_ts(pr.get("created_at"))
    if a is None:
        return []

    def texts(p):
        return [p.get("body")] + [c.get("body") for k in ("comments_list", "review_comments")
                                  for c in p.get(k) or [] if isinstance(c, dict)]

    def names(p):
        out = set()
        for t in texts(p):
            out.update(int(n) for n in _ISSUE_NUMBER_RE.findall(t or ""))
            out.update(int(n) for n in re.findall(r"/pull/(\d+)", t or ""))
        return out
    mine = names(pr)
    refs = set(pr.get("closes") or []) | set(pr.get("crossref_issues") or [])
    out = []
    for p in by_author:
        b = _parse_ts(p.get("created_at"))
        if b is None or abs((b - a).total_seconds()) > days * 86400:
            continue
        basis = []
        if p.get("number") in mine:
            basis.append("#{} names #{}".format(pr.get("number"), p.get("number")))
        if pr.get("number") in names(p):
            basis.append("#{} names #{}".format(p.get("number"), pr.get("number")))
        shared = refs & (set(p.get("closes") or []) | set(p.get("crossref_issues") or []))
        if shared:
            basis.append("both reference #" + ", #".join(str(n) for n in sorted(shared)))
        if basis:
            out.append({"number": p.get("number"), "title": p.get("title"),
                        "state": p.get("state"), "url": p.get("url"),
                        "basis": "; ".join(basis)})
    return out


def _net_files(row, instant, own):
    """The PR's per-file net change (gather pr_net_files) for the files more
    than one of its own commits touched: [{path, added, removed, commits}]
    when the stored head is the head as of the instant."""
    nf = (row or {}).get("net_files") or {}
    if not nf.get("files") or not own or nf.get("head") != own[-1].get("sha"):
        return None
    counts = {}
    for c in own:
        for p in c.get("paths") or []:
            counts[p] = counts.get(p, 0) + 1
    rows = [dict(f, commits=counts[f["path"]]) for f in nf["files"]
            if counts.get(f.get("path"), 0) > 1]
    return rows or None


def story_material(bundle, train):
    """The conversation of one train as one chronological timeline, for a
    narrator. Pure (reads the bundle; returns new objects).

    Items {at, kind, actor, url, on: {type, number}, ...}: `opened` (with
    `title` and the body as `text`), `comment`, `review_comment`, `review`
    (`state`, `text`), `ready_for_review` / `reopened` / `closed` (the
    lifecycle events; a `closed` also from `closed_at` when no event records
    it), `merged` (`by`), and `commit` — the PR's own commits from
    `pr_history` (`source: "pr_head"`, `subject`, `files`, `authored_at`) and
    the train's merged commits (`source: "base"`) — each carrying the
    `code_notes` its diff added / removed / changed. Bots are left out of the
    timeline and counted in `bots`. Everything is as of the window's end
    (records are projected by the view; PR commits after the instant are
    dropped). Turning points are marked by rule (`turning_points`), text is
    bounded with every cut recorded (`truncation`)."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    issues, prs = _train_members(train, pr_index, issue_index)
    history = _pr_history_index(bundle)
    activity = _pr_activity_index(bundle)
    instant = window_end_instant(bundle.get("meta"))
    items, bots = [], {}

    def bot(login):
        bots[login] = bots.get(login, 0) + 1

    for rec in issues + prs:
        typ = "pr" if rec in prs else "issue"
        on = {"type": typ, "number": rec.get("number")}
        if rec.get("created_at"):
            items.append({"at": rec["created_at"], "kind": "opened",
                          "actor": rec.get("author"), "url": rec.get("url"),
                          "on": on, "title": rec.get("title"),
                          "text": rec.get("body") or ""})
            if rec.get("body_as_of"):
                items[-1]["body_as_of"] = rec["body_as_of"]
        rc_urls = {c.get("id"): c.get("url") for c in rec.get("review_comments") or []
                   if isinstance(c, dict) and c.get("id") is not None}
        for key, kind in (("comments_list", "comment"),
                          ("review_comments", "review_comment")):
            for c in rec.get(key) or []:
                if not isinstance(c, dict):
                    continue
                if _is_automation(bundle, c.get("author")):
                    bot(c.get("author"))
                    continue
                it = {"at": c.get("created_at"), "kind": kind,
                      "actor": c.get("author"), "url": c.get("url"),
                      "on": on, "text": c.get("body") or ""}
                if kind == "review_comment":
                    _review_comment_context(it, c, rc_urls)
                items.append(it)
        for r in rec.get("reviews") or []:
            if not isinstance(r, dict) or r.get("state") == "pending":
                continue
            if _is_automation(bundle, r.get("author")):
                bot(r.get("author"))
                # an automated review (a security scanner, a reviewing
                # agent) is on the record, never a person's turn
                items.append({"at": r.get("submitted_at"), "kind": "automated_review",
                              "actor": r.get("author"), "url": r.get("url"),
                              "on": on, "state": r.get("state"),
                              "text": r.get("body") or ""})
                continue
            if r.get("state") == "commented" and not r.get("body"):
                continue          # a review thread's wrapper: its text is the
                                  # review comment
            items.append({"at": r.get("submitted_at"), "kind": "review",
                          "actor": r.get("author"), "url": r.get("url"),
                          "on": on, "state": r.get("state"),
                          "text": r.get("body") or ""})
        closed_seen = False
        for e in rec.get("lifecycle") or []:
            if not isinstance(e, dict):
                continue
            closed_seen = closed_seen or e.get("event") == "closed"
            it = {"at": e.get("created_at"), "kind": e.get("event"),
                  "actor": e.get("actor"), "url": _event_url(rec, e), "on": on}
            # who closed it and why: the event's actor, GitHub's
            # state_reason, the closing commit, and a PR of the view that
            # names the item as closed (closes) — each only when recorded
            if e.get("event") == "closed":
                if e.get("state_reason") or rec.get("state_reason"):
                    it["state_reason"] = e.get("state_reason") or rec["state_reason"]
                if e.get("commit"):
                    it["commit"] = e["commit"]
                if typ == "issue":
                    closers = sorted(p.get("number") for p in bundle.get("prs", [])
                                     if rec.get("number") in (p.get("closes") or []))
                    if closers:
                        it["closing_prs"] = closers
            items.append(it)
        if rec.get("merged") and rec.get("merged_at"):
            items.append({"at": rec["merged_at"], "kind": "merged",
                          "actor": rec.get("merged_by"), "url": rec.get("url"),
                          "on": on})
        elif rec.get("closed_at") and not closed_seen \
                and (rec.get("state") == "closed"):
            items.append({"at": rec["closed_at"], "kind": "closed",
                          "actor": None, "url": rec.get("url"), "on": on})
        if typ == "pr" and activity and rec.get("number") in activity:
            items += _activity_items(rec, activity[rec["number"]], instant)
    notes_by_commit, by_module, notes_info = train_code_notes(
        bundle, train, history, instant)
    rules = path_class_rules((bundle.get("module_changes") or {})
                             .get("path_rules") or ())
    ctx = _ModuleCtx(bundle, rules)
    seen = set()
    name_logins = _git_name_logins(bundle)
    for p in prs:
        row = history.get(p.get("number"))
        patches_by = {}
        for x in (row or {}).get("patches") or []:
            patches_by.setdefault(x.get("commit"), []).append(x)
        for c in _pr_commits_as_of(row, instant):
            seen.add(c.get("sha"))
            it = {"at": c.get("committed_at"), "kind": "commit",
                  "actor": c.get("author"), "url": _pr_commit_url(p, c["sha"]),
                  "on": {"type": "pr", "number": p.get("number")},
                  "source": "pr_head", "sha": c.get("sha"),
                  "subject": c.get("subject"), "files": c.get("files"),
                  "authored_at": c.get("authored_at")}
            if c.get("parents", 1) > 1:
                it["merge"] = True
            if not own_change(c):
                it["not_own_change"] = _not_own_reason(c)
            # who the git author is, as far as the record resolves it
            it["author_name"] = c.get("author")
            it["author_login"] = name_logins.get(c.get("author"))
            if it["author_login"] and it["author_login"] != p.get("author"):
                it["authored_by_other"] = True
            if c.get("committed_at") and p.get("created_at") \
                    and c["committed_at"] < p["created_at"]:
                it["before_pr_opened"] = True       # a reused / older branch
            if c.get("body"):
                it["text"] = c["body"]
                if c.get("body_truncated_from"):
                    it["body_truncated_from"] = c["body_truncated_from"]
            change = change_summary(
                c.get("paths"), rules, ctx,
                _interface_rows((row or {}).get("notes"), c.get("sha")),
                patches_by.get(c.get("sha"))) if own_change(c) else None
            if change:
                if c.get("paths_omitted"):
                    change["paths_omitted"] = c["paths_omitted"]
                it["change"] = change
            if notes_by_commit.get(c["sha"]):
                it["code_notes"] = notes_by_commit[c["sha"]]
            ev = _commit_evidence(c)
            if ev:
                it["evidence"] = ev
            items.append(it)
        # what each force-push replaced, on its timeline item (or one of its own)
        for rec in (row or {}).get("replaced") or []:
            if not _not_after(rec.get("at"), instant):
                continue
            push = next((it for it in items if it.get("kind") == "head_ref_force_pushed"
                         and it.get("on") == {"type": "pr", "number": p.get("number")}
                         and it.get("at") == rec.get("at")), None)
            if push is None:
                push = {"at": rec.get("at"), "kind": "head_ref_force_pushed",
                        "actor": rec.get("actor"), "url": p.get("url"),
                        "on": {"type": "pr", "number": p.get("number")},
                        "source": "pr_history"}
                items.append(push)
            push["replaced"] = _replaced_summary(rec)
        # a commit the checks read but the gathered history does not hold
        # (replaced when the branch was rewritten, or outside the walk): its
        # checks item says it is a commit of the PR
        acts = {c.get("sha"): c for c in ((activity or {}).get(p.get("number"))
                                          or {}).get("commits") or []
                if isinstance(c, dict) and c.get("sha")}
        for it in items:
            if it.get("kind") == "checks" and it.get("on") == {
                    "type": "pr", "number": p.get("number")} \
                    and it.get("sha") in acts and it["sha"] not in seen:
                it["commit_not_in_history"] = {
                    "committed_at": acts[it["sha"]].get("committed_at"),
                    "why": "a commit of the PR read with its checks, not in its "
                           "gathered commits (replaced when the branch was "
                           "rewritten, or outside the walk)"}
    commits_by_sha = {c.get("sha"): c for c in bundle.get("commits", [])}
    for sha in train.get("commits") or []:
        c = commits_by_sha.get(sha)
        if c is None or sha in seen:
            continue
        it = {"at": c.get("committed_at") or c.get("date"), "kind": "commit",
              "actor": c.get("author"), "url": derive._commit_url(bundle, sha),
              "source": "base", "sha": sha, "subject": c.get("message"),
              "files": len(c.get("files") or [])}
        if c.get("pr") is not None:
            it["on"] = {"type": "pr", "number": c.get("pr")}
        change = change_summary(c.get("files"), rules, ctx,
                                _interface_rows(bundle.get("symbol_events"), sha))
        if change:
            it["change"] = change
        if notes_by_commit.get(sha):
            it["code_notes"] = notes_by_commit[sha]
        items.append(it)
    items = [it for it in items if it.get("at")]
    items.sort(key=lambda it: (_parse_ts(it["at"]) or datetime.min.replace(
        tzinfo=timezone.utc), _TIMELINE_KIND_ORDER.get(it["kind"], 9),
        it.get("url") or ""))
    texts = [(it.get("text"), it.get("url")) for it in items if it.get("text")]
    truncation = _apply_story_budget(items)
    truncation["code_notes"] = notes_info
    hunks_omitted = _apply_hunk_budget(items)
    if hunks_omitted:
        truncation["hunks_omitted"] = hunks_omitted
    ev_info = _apply_evidence_budget(items)
    if ev_info:
        truncation["evidence"] = ev_info
    turning = story_turning_points(
        items, instant if train.get("outcome") == "in_flight" else None)
    _rework_detail(items, turning)
    threads = story_threads(items, pr_index, issue_index)
    _apply_thread_record(threads, prs, activity, instant)
    thread_responses(threads, items, prs, history, instant)
    requests = _outcome_requests(threads)
    automation = _automation_accounts(issues + prs, prs, history, activity,
                                      instant,
                                      lambda x: _is_automation(bundle, x),
                                      {i.get("number"): i for i in
                                       bundle.get("issues") or []})
    out = {"timeline": items,
           "turning_points": turning,
           "truncation": truncation,
           "bots": dict(sorted(bots.items())),
           "code_notes_by_module": by_module,
           "pr_history": _pr_history_status(prs, history),
           "as_of": instant,
           "not_gathered": _story_not_gathered(prs, activity)
           + _history_omissions(prs, history),
           "outcome": story_outcome(bundle, train, items, history, instant, texts,
                                    activity),
           "threads": threads[:STORY_THREADS_MAX]}
    if len(threads) > STORY_THREADS_MAX:
        out["threads_omitted"] = len(threads) - STORY_THREADS_MAX
    if automation:
        out["automation"] = automation
    coverage = _thread_coverage(prs, activity, threads)
    if coverage:
        out["thread_coverage"] = coverage
    if requests:
        out["outcome"]["requests"] = requests
    gaps = _story_gaps(items)
    unwalked = [p.get("number") for p in prs
                if "notes" in (history.get(p.get("number")) or {})
                and not (history.get(p.get("number")) or {}).get("narrative_collected")]
    if unwalked:
        gaps.append("PR commits of {} carry no code-comment, documentation or "
                    "per-commit interface evidence and review threads no "
                    "`response` from the diffs (gathered before they were "
                    "kept): what each commit did is known from its summary "
                    "only".format(", ".join("#{}".format(n) for n in unwalked)))
    if prs and any(p.get("number") not in (activity or {}) for p in prs):
        gaps.append("check results, review-thread resolved flags and PR "
                    "lifecycle events (renames, draft / ready, review "
                    "requests, dismissals, force-pushes) are not in the store "
                    "for PR(s) {} (gathered before they were kept, or with "
                    "--no-pr-checks for the checks): the record does not show "
                    "them".format(", ".join(
                        "#{}".format(p.get("number")) for p in prs
                        if p.get("number") not in (activity or {}))))
    if gaps:
        out["gaps"] = gaps
    return out


def _review_comment_context(it, c, rc_urls):
    """Copy what a stored review comment is attached to onto its timeline
    item (each key only when stored): `path`, `line`, `commit`, the
    `diff_hunk` (capped at STORY_HUNK_CAP; `diff_hunk_lines` when gather cut
    it; a thread reply carries none, its thread's opening comment has it)
    and `in_reply_to` (the URL of the comment that opened the thread, or the
    raw id when that comment is not in the record)."""
    for k in ("path", "line", "commit"):
        if c.get(k) is not None:
            it[k] = c[k]
    if c.get("in_reply_to") is not None:
        it["in_reply_to"] = rc_urls.get(c["in_reply_to"], c["in_reply_to"])
    elif c.get("diff_hunk"):
        it["diff_hunk"] = c["diff_hunk"][-STORY_HUNK_CAP:]
        if c.get("diff_hunk_lines"):
            it["diff_hunk_lines"] = c["diff_hunk_lines"]


def _apply_hunk_budget(items):
    """Hold the review comments' diff hunks to STORY_HUNK_BUDGET characters,
    keeping the earliest (the thread openers in time order); returns how
    many were left out (each such item gets `diff_hunk_omitted`)."""
    used, omitted = 0, 0
    for it in items:
        h = it.get("diff_hunk")
        if h is None:
            continue
        if used + len(h) > STORY_HUNK_BUDGET:
            del it["diff_hunk"]
            it["diff_hunk_omitted"] = len(h)
            omitted += 1
        else:
            used += len(h)
    return omitted


def _rework_detail(items, turning):
    """Give each `rework_after_change_request` turning point what changed
    after the request: `commits_since_request` and `paths_since_request`
    (the files of the PR's own commits from the request until the
    requester's next comment / review, at most 30), when the commits carry
    their paths."""
    for tp in turning:
        if tp["rule"] != "rework_after_change_request":
            continue
        cr = items[tp["detail"]["change_request_index"]]
        num, who = (cr.get("on") or {}).get("number"), cr.get("actor")
        commits, paths = 0, []
        for it in items[tp["detail"]["change_request_index"] + 1:]:
            if (it.get("on") or {}).get("number") != num:
                continue
            if it["kind"] in ("review", "review_comment", "comment") \
                    and it.get("actor") == who:
                break
            if it["kind"] == "commit" and it.get("source") == "pr_head" \
                    and not it.get("merge"):
                commits += 1
                for ps in ((it.get("change") or {}).get("files") or {}).values():
                    paths += [p for p in ps if p not in paths]
        tp["detail"]["commits_since_request"] = commits
        if paths:
            tp["detail"]["paths_since_request"] = sorted(paths)[:30]


def _story_gaps(items):
    """What this slice cannot show because the store predates it, so a
    narrator says "the record does not show ..." instead of guessing."""
    gaps = []
    rcs = [it for it in items if it["kind"] == "review_comment"]
    if rcs and not any("path" in it or "in_reply_to" in it for it in rcs):
        gaps.append("review comments carry no file, diff lines or thread link "
                    "(the store was gathered before they were kept): which "
                    "file each comment is about and which comments reply to "
                    "which are not in the record")
    pr_commits = [it for it in items if it["kind"] == "commit"
                  and it.get("source") == "pr_head" and not it.get("merge")]
    if pr_commits and not any("change" in it and "files" in it["change"]
                              for it in pr_commits):
        gaps.append("PR commits carry no file paths or message bodies (the "
                    "store was gathered before they were kept): what each "
                    "commit changed is known only from its subject")
    return gaps


def _pr_history_status(prs, history):
    """{pr number (str): status} for the train's PRs: the pr_history row's
    status (`fetched`, `missing`, `fetch_failed`, ...) with its notes state,
    or `not_gathered` when the view carries no history for it."""
    out = {}
    for p in prs:
        row = history.get(p.get("number"))
        if row is None:
            out[str(p.get("number"))] = "not_gathered"
            continue
        s = row.get("status") or "unknown"
        if row.get("on_base"):
            s += " (commits on the base branch)"
        elif "notes" in row:
            s += ", notes extracted"
        elif row.get("notes_skipped"):
            s += ", notes skipped: " + row["notes_skipped"]
        out[str(p.get("number"))] = s
    return out


def _automation_accounts(records, prs, history, activity, instant=None,
                         is_auto=None, issues_index=None):
    """The automation accounts of a train, for what they are (issue #97):
    [{login, is_bot: true, comments, review_comments, reviews, commits,
    prs_opened: [n], on_behalf_of?: {login, basis, pr}}] — counts over the
    train's items (they stay out of the conversation timeline), the PRs
    they opened, their PR commits (git author names an automation account
    commits under), and for a PR an automation account opened, the person
    who handed it the work when the record shows it (_on_behalf_of).
    `is_auto(login)`: the view's automation flag (_is_automation), else
    derive.is_bot_login."""
    is_auto = is_auto or derive.is_bot_login
    acc = {}

    def get(login):
        return acc.setdefault(login, {"login": login, "is_bot": True,
                                      "comments": 0, "review_comments": 0,
                                      "reviews": 0, "commits": 0, "prs_opened": []})
    for rec in records:
        for key, field in (("comments_list", "comments"),
                           ("review_comments", "review_comments"),
                           ("reviews", "reviews")):
            for c in rec.get(key) or []:
                if isinstance(c, dict) and c.get("author") and is_auto(c["author"]):
                    get(c["author"])[field] += 1
    for p in prs:
        if p.get("author") and is_auto(p["author"]):
            row = get(p["author"])
            row["prs_opened"].append(p.get("number"))
            by = _on_behalf_of(p, (activity or {}).get(p.get("number")), instant,
                               issues_index)
            if by and "on_behalf_of" not in row:
                row["on_behalf_of"] = by
        for c in _pr_commits_as_of(history.get(p.get("number")), instant):
            if derive.is_bot_login(c.get("author")):
                get(c["author"])["commits"] += 1
    return [acc[k] for k in sorted(acc)]


def _commit_evidence(c):
    """The narrative evidence a pr_history commit carries (collect_pr_narrative):
    {interface, comments (each with its comment_class), docs, [<key>_omitted],
    [interface_unread], [docs_unread]} — the unread lists name files whose
    version gather could not read, so "no evidence" is told from "evidence
    unavailable" — or None when it has none."""
    out = {}
    if c.get("interface"):
        out["interface"] = [dict(r) for r in c["interface"]]
    if c.get("comments"):
        rows = []
        for r in c["comments"]:
            r = dict(r)
            r["class"] = comment_class("comment", r.get("text") or r.get("after")
                                       or r.get("before"))
            rows.append(r)
        out["comments"] = rows
    if c.get("docs"):
        out["docs"] = [dict(r) for r in c["docs"]]
    for k in ("interface_omitted", "comments_omitted"):
        if c.get(k):
            out[k] = c[k]
    for k in ("interface_unread", "docs_unread"):
        if c.get(k):
            out[k] = list(c[k])
    return out or None


def _apply_evidence_budget(items):
    """Hold the commit items' `evidence` rows to STORY_EVIDENCE_BUDGET
    characters, the newest commits first (interface rows, then comments,
    then docs within a commit); a commit whose rows were cut gets
    `evidence_omitted`. Returns {budget, kept, omitted} or None when no item
    carries evidence."""
    used, kept, omitted, any_ev = 0, 0, 0, False
    for it in reversed(items):
        ev = it.get("evidence")
        if not ev:
            continue
        any_ev = True
        cut = 0
        for key in ("interface", "comments", "docs"):
            rows = ev.get(key) or []
            keep = []
            for r in rows:
                size = len(json.dumps(r, sort_keys=True))
                if used + size > STORY_EVIDENCE_BUDGET:
                    cut += 1
                    continue
                used += size
                keep.append(r)
            if keep:
                ev[key] = keep
            elif key in ev:
                del ev[key]
        kept += sum(len(ev.get(k) or []) for k in ("interface", "comments", "docs"))
        if cut:
            it["evidence_omitted"] = cut
            omitted += cut
    if not any_ev:
        return None
    return {"budget": STORY_EVIDENCE_BUDGET, "kept": kept, "omitted": omitted}


def _commit_ref(it, index):
    return {"index": index, "sha": it.get("sha"), "at": it.get("at"),
            "url": it.get("url"), "subject": it.get("subject")}


def thread_responses(threads, items, prs, history, instant):
    """What followed each thread on a PR, from the record (in place):
    `response` {basis, commits: [{index, sha, at, url, subject, [match],
    [hunks]}], [file_commits]} and `verdict_after` {state, at, url}.
    - basis `anchor`: a review thread whose opening comment gather matched
      against the later commits' diffs (pr_history `responses`): the
      commits that changed the commented lines, with the removed / added
      lines there (`match` anchor / line), as of the instant;
    - basis `time`: otherwise, the PR's own non-merge commits after the
      thread until its opener speaks again after one of them (the opener's
      other comments and verdict of the same review round, before any
      commit, do not end it); at most STORY_RESPONSE_COMMITS, each
      pointing at its timeline item, whose `evidence` / `change` say what
      it did.
    `verdict_after` is the opener's first approval / change request /
    dismissal on the PR after the thread (after its last response commit
    when there is one). A sequence, not an agreement: nothing here says
    anyone agreed to anything."""
    by_pr = {p.get("number"): p for p in prs}
    pos = {id(it): i for i, it in enumerate(items)}
    for t in threads:
        on = t.get("on") or {}
        if on.get("type") != "pr" or on.get("number") not in by_pr:
            continue
        n = on["number"]
        row = history.get(n) or {}
        start = next((i for i, it in enumerate(items) if it.get("url") == t.get("url")
                      and (it.get("on") or {}) == on), None)
        if start is None:
            continue
        resp = None
        if t.get("kind") == "review_thread" and row.get("responses"):
            ids = {c.get("url"): c.get("id") for c in
                   by_pr[n].get("review_comments") or [] if isinstance(c, dict)}
            stored = next((r for r in row["responses"]
                           if r.get("comment_id") == ids.get(t.get("url"))), None)
            if stored:
                when = {c.get("sha"): c.get("committed_at")
                        for c in row.get("commits") or []}
                commits = []
                for h in stored.get("commits") or []:
                    if not _not_after(when.get(h.get("sha")), instant):
                        continue
                    it = next((x for x in items if x.get("kind") == "commit"
                               and x.get("sha") == h.get("sha")
                               and (x.get("on") or {}) == on), None)
                    ref = _commit_ref(it, pos[id(it)]) if it else \
                        {"sha": h.get("sha"), "at": when.get(h.get("sha"))}
                    ref.update(match=h.get("match"), hunks=h.get("hunks") or [])
                    commits.append(ref)
                if commits:
                    resp = {"basis": "anchor", "commits": commits}
                    files = [x for x in stored.get("file_commits") or []
                             if _not_after(when.get(x), instant)]
                    if files:
                        resp["file_commits"] = files
        if resp is None:
            commits = []
            for i in range(start + 1, len(items)):
                it = items[i]
                if (it.get("on") or {}) != on:
                    continue
                if it["kind"] in ("comment", "review", "review_comment") \
                        and it.get("actor") == t.get("by") and commits:
                    break       # the opener spoke again after a commit
                if it["kind"] == "commit" and it.get("source") == "pr_head" \
                        and not it.get("merge"):
                    commits.append(_commit_ref(it, i))
                    if len(commits) >= STORY_RESPONSE_COMMITS:
                        break
            if commits:
                resp = {"basis": "time", "commits": commits}
        if resp:
            t["response"] = resp
        after = t.get("at")
        if resp:
            after = max([after] + [c.get("at") or "" for c in resp["commits"]])
        verdict = next((it for it in items[start + 1:]
                        if it["kind"] == "review" and (it.get("on") or {}) == on
                        and it.get("actor") == t.get("by")
                        and it.get("state") in ("approved", "changes_requested",
                                                "dismissed")
                        and (it.get("at") or "") >= (after or "")), None)
        if verdict is not None:
            t["verdict_after"] = {"state": verdict.get("state"),
                                  "at": verdict.get("at"), "url": verdict.get("url")}


def _outcome_requests(threads):
    """outcome.requests — "requests and what followed": per thread with a
    `response` or a `verdict_after`, {thread, kind, by, at, url, [ask],
    [path], followed_by: [{sha, at, url, subject}], basis, [verdict_after]},
    at most STORY_REQUESTS_MAX."""
    out = []
    for t in threads:
        if not t.get("response") and not t.get("verdict_after"):
            continue
        row = {"thread": t.get("id"), "kind": t.get("kind"), "by": t.get("by"),
               "at": t.get("at"), "url": t.get("url")}
        for k in ("ask", "path"):
            if t.get(k):
                row[k] = t[k]
        resp = t.get("response") or {}
        row["followed_by"] = [{k: c.get(k) for k in ("sha", "at", "url", "subject")
                               if c.get(k) is not None}
                              for c in resp.get("commits") or []]
        if resp:
            row["basis"] = resp.get("basis")
        if t.get("verdict_after"):
            row["verdict_after"] = t["verdict_after"]
        out.append(row)
    return out[:STORY_REQUESTS_MAX]


def _pr_activity_index(bundle):
    """{pr number: pr_activity row} of the view's `pr_activity` fact, each
    row carrying the fact's `gathered_at` (a merged view's rows have their
    own); None when the view carries no such fact (an older store)."""
    pa = bundle.get("pr_activity")
    if not isinstance(pa, dict) or not pa.get("collected"):
        return None
    out = {}
    for r in pa.get("prs") or []:
        if isinstance(r, dict) and isinstance(r.get("number"), int):
            out[r["number"]] = dict(r) if "gathered_at" in r else \
                dict(r, gathered_at=pa.get("gathered_at"))
    return out


def _check_item_time(checks, committed_at, instant):
    """When a commit's check results were known as of the instant: the
    latest completion by then, else the commit time (all still pending)."""
    done = [c.get("completed_at") for c in checks or []
            if c.get("completed_at") and _not_after(c["completed_at"], instant)]
    return max(done) if done else committed_at


def _activity_items(pr, row, instant):
    """The timeline items the `pr_activity` row of one PR adds, as of the
    instant: its lifecycle events (`renamed` with from / to,
    `convert_to_draft`, `review_requested` / `review_request_removed` with
    requested / by, `review_dismissed` with state / message,
    `head_ref_force_pushed` with before / after commits when read), and one
    `checks` item per covered commit committed by the instant: its roll-up
    as of the instant (gather.checks_rollup), the failing and pending check
    names and the passing count, collapsed (never one item per check run)."""
    on = {"type": "pr", "number": pr.get("number")}
    out = []
    for e in row.get("events") or []:
        if not isinstance(e, dict) or not e.get("at") \
                or not _not_after(e["at"], instant):
            continue
        it = {"at": e["at"], "kind": e.get("event"), "actor": e.get("actor"),
              "url": _event_url(pr, e), "on": on}
        for k in _PR_EVENT_FIELDS:
            if e.get(k) is not None:
                it[k] = e[k]
        out.append(it)
    commits = [c for c in row.get("commits") or []
               if isinstance(c, dict) and c.get("sha")
               and _not_after(c.get("committed_at"), instant)]
    head = commits[-1]["sha"] if commits else None
    for c in commits:
        roll = _commit_rollup(c, instant, row.get("gathered_at"))
        if roll["rollup"] == "none" and not c.get("checks"):
            continue
        it = {"at": _check_item_time(c.get("checks"), c.get("committed_at"),
                                     instant),
              "kind": "checks", "actor": None,
              "url": _pr_commit_url(pr, c["sha"]), "on": on, "sha": c["sha"],
              "rollup": roll["rollup"], "passing": roll["passing"]}
        if roll["failing"]:
            it["failing"] = roll["failing"]
        if roll["pending"]:
            it["pending"] = roll["pending"]
        if c.get("checks_omitted"):
            it["checks_omitted"] = c["checks_omitted"]
        if roll["awaiting_approval"]:
            it["awaiting_approval"] = roll["awaiting_approval"]
        if roll.get("state_only"):
            it["state_only"] = True
        if c["sha"] == head:
            it["head"] = True
        out.append(it)
    return out


def _commit_rollup(c, instant, gathered_at=None):
    """checks_rollup of one pr_activity commit as of the instant, plus
    `awaiting_approval` (checks whose conclusion is action_required: they
    wait for a maintainer to approve the run, e.g. a first-time
    contributor's fork). A commit read by its roll-up state only
    (`state_only`: an older commit whose state was a success) keeps that
    state as gathered (`state_only: true`) — but GitHub's roll-up state
    carries no check times, so for a view ending BEFORE the gather
    (`gathered_at` later than the instant) it is `unknown`: a check could
    have finished in between. A nightly gather of the open window reads the
    state the same night, and that view's end is after it."""
    if c.get("state_only"):
        known = not (instant and gathered_at
                     and not _not_after(gathered_at, instant))
        return {"rollup": (c.get("rollup") or "none") if known else "unknown",
                "failing": [], "pending": [], "passing": 0,
                "awaiting_approval": [], "state_only": True}
    roll = gather.checks_rollup(c.get("checks"), as_of=instant,
                                omitted=c.get("checks_omitted") or 0)
    roll["awaiting_approval"] = sorted({
        k.get("name") or "?" for k in c.get("checks") or []
        if isinstance(k, dict) and k.get("conclusion") == "action_required"
        and _not_after(k.get("completed_at") or k.get("started_at"), instant)})
    return roll


def checks_outcome(row, instant, pr_url=None):
    """The outcome block's `checks` of one PR from its `pr_activity` row:
    {status: gathered, head, [head_url], rollup, failing, pending, passing,
    gathered_at, commits_total, commits_covered, [checks_omitted],
    [completed_after_as_of], [commits_incomplete]} — the head being the latest covered commit
    committed by the instant, its roll-up as of the instant (`incomplete`
    when the read missed some of its checks and none visible failed); with
    no covered commit by the instant `rollup: unavailable`, `head: None`
    and a `reason` — or {status,
    [reason], gathered_at} when the checks were capped, failed, not found
    or not requested."""
    status = row.get("checks_status")
    out = {"status": status, "gathered_at": row.get("gathered_at")}
    if status != "gathered":
        if row.get("checks_reason"):
            out["reason"] = row["checks_reason"]
        return out
    commits = [c for c in row.get("commits") or []
               if isinstance(c, dict) and c.get("sha")
               and _not_after(c.get("committed_at"), instant)]
    out["commits_total"] = row.get("commits_total")
    out["commits_covered"] = len(commits)
    if not commits:
        # the read covers the PR's latest commits only: none of them existed
        # at the instant, so the head's checks then are not in the record
        out["head"] = None
        out["rollup"] = "unavailable"
        out["reason"] = ("the commits whose checks were read were all "
                         "committed after the as-of instant")
        return out
    c = commits[-1]
    roll = _commit_rollup(c, instant, row.get("gathered_at"))
    out.update(head=c["sha"], rollup=roll["rollup"], failing=roll["failing"],
               pending=roll["pending"], passing=roll["passing"])
    if roll["awaiting_approval"]:
        out["awaiting_approval"] = roll["awaiting_approval"]
    if row.get("history_status") and row["history_status"] != "complete":
        out["history_status"] = row["history_status"]
    if pr_url:
        out["head_url"] = "{}/commits/{}".format(pr_url, c["sha"])
    if c.get("checks_omitted"):
        out["checks_omitted"] = c["checks_omitted"]
    late = sum(1 for k in c.get("checks") or []
               if k.get("completed_at") and not _not_after(k["completed_at"],
                                                           instant))
    if late:
        out["completed_after_as_of"] = late
    # per-commit completeness of the history read: a covered commit whose
    # checks were only partly read has no complete result
    partial = sum(1 for x in commits
                  if _commit_rollup(x, instant, row.get("gathered_at"))["rollup"]
                  in ("incomplete", "unknown"))
    if partial:
        out["commits_incomplete"] = partial
    return out


def _thread_coverage(prs, activity, threads):
    """{pr number (str): {flagged, omitted}} for the train's PRs with a
    `pr_activity` row: how many of the PR's review threads carry GitHub's
    resolved flag in the ledger, and how many review threads gather did not
    read (`threads_omitted`). The story check needs it: a ledger whose
    visible threads all carry a flag is complete only with nothing
    omitted."""
    out = {}
    for p in prs:
        row = (activity or {}).get(p.get("number"))
        if row is None:
            continue
        n = p.get("number")
        out[str(n)] = {"flagged": sum(
            1 for t in threads if t.get("kind") == "review_thread"
            and (t.get("on") or {}).get("number") == n
            and "resolved_on_github" in t),
            "omitted": row.get("threads_omitted") or 0}
    return out


def _apply_thread_record(threads, prs, activity, instant):
    """Where GitHub's own resolved flag is stored for a review thread (the
    `pr_activity` row's threads, joined on the thread's opening review
    comment), the record wins over the derived status: `status` becomes
    `resolved on GitHub` / `not resolved on GitHub`, the rule's status is
    kept as `derived_status`, and `resolved_on_github` {resolved, [by],
    outdated, as_gathered} says what GitHub held when the window was
    gathered (GitHub keeps no time for a resolution). In place."""
    if not activity:
        return
    by_url = {}
    for p in prs:
        row = activity.get(p.get("number")) or {}
        ids = {t.get("comment_id"): t for t in row.get("threads") or []
               if isinstance(t, dict) and t.get("comment_id") is not None}
        for c in p.get("review_comments") or []:
            if isinstance(c, dict) and c.get("id") in ids and c.get("url"):
                by_url[c["url"]] = (ids[c["id"]], row.get("gathered_at"))
    for t in threads:
        if t.get("kind") != "review_thread" or t.get("url") not in by_url:
            continue
        rec, when = by_url[t["url"]]
        flag = {"resolved": bool(rec.get("resolved")),
                "outdated": bool(rec.get("outdated")), "as_gathered": when}
        if rec.get("resolved_by"):
            flag["by"] = rec["resolved_by"]
        if when and instant and not _not_after(when, instant):
            flag["gathered_after_as_of"] = True
        t["resolved_on_github"] = flag
        t["derived_status"] = t["status"]
        t["status"] = "resolved on GitHub" if flag["resolved"] \
            else "not resolved on GitHub"
        if flag.get("gathered_after_as_of"):
            # GitHub's flag was read after the view's end and has no time: one
            # sentence that says both, so the two statuses never disagree
            t["status_note"] = "{} when gathered on {}, after the window's end; " \
                "as of the end: {}".format(t["status"], (when or "")[:10],
                                           t["derived_status"])


def _history_omissions(prs, history):
    """What gather's per-PR budgets left out of the PR history, named per PR
    (a narrator must not read "no evidence" as "no change"): narrative
    evidence rows of older commits (`narrative_omitted`) and the responses
    of review comments (`responses_omitted`)."""
    out = []
    for p in prs:
        row = history.get(p.get("number")) or {}
        n = p.get("number")
        if row.get("narrative_omitted"):
            out.append("{} narrative evidence row(s) of the older commits of #{} "
                       "(beyond the per-PR budget)".format(row["narrative_omitted"], n))
        if row.get("responses_omitted"):
            out.append("what followed {} review comment(s) of #{} (beyond the "
                       "per-PR budget)".format(row["responses_omitted"], n))
    return out


def _story_not_gathered(prs, activity):
    """STORY_NOT_GATHERED, minus what the view's `pr_activity` holds for
    every PR of the train: check results (when gathered for each), the
    resolved flag (likewise), renames (title edits) and force-pushes (their
    before / after commits; the replaced content stays unfetched). A partial
    read is named: review threads, lifecycle events or commits beyond what
    gather read."""
    out = list(STORY_NOT_GATHERED)
    if not prs or not activity or any(p.get("number") not in activity
                                      for p in prs):
        return out
    rows = [activity[p.get("number")] for p in prs]
    asked = all(r.get("checks_status") == "gathered" for r in rows)
    out[0] = ("description edits (bodies are as last gathered; title edits "
              "are in the record as `renamed` events)")
    out[1] = ("the content of the branch states force-pushes replaced (the "
              "push and, when read, its before / after commits are in the "
              "record)")
    if asked:
        out = [x for x in out if not x.startswith(("whether a review thread",
                                                   "check runs"))]
    else:
        missing = ["#{} ({})".format(p.get("number"),
                                     activity[p.get("number")].get("checks_status"))
                   for p in prs
                   if activity[p.get("number")].get("checks_status") != "gathered"]
        out = [x for x in out if not x.startswith(("whether a review thread",
                                                   "check runs"))]
        out.append("check results and review-thread resolved flags of "
                   "PR(s) {} (not read)".format(", ".join(missing)))
    # partial reads: the record holds some, not all
    for p, r in zip(prs, rows):
        n = p.get("number")
        if r.get("threads_omitted"):
            out.append("the resolved flag of {} review thread(s) of #{} (beyond "
                       "the threads read)".format(r["threads_omitted"], n))
        if r.get("events_omitted"):
            out.append("{} earlier lifecycle event(s) of #{} (beyond the "
                       "events kept)".format(r["events_omitted"], n))
        covered = len(r.get("commits") or [])
        if r.get("checks_status") == "gathered" and \
                isinstance(r.get("commits_total"), int) and r["commits_total"] > covered:
            out.append("check results of the {} earliest commit(s) of #{} (only "
                       "the latest {} were read)".format(
                           r["commits_total"] - covered, n, covered))
    return out


# --- the substance: change summaries, outcome, open threads ----------------
#
# Who said what and when is not a story: a reader needs what the code change
# was, what each objection was about, what was decided and what ended up in
# the product, and for open work what it waits on. Everything below is built
# by rule from stored records (as of the window's end), never inferred.

# Paths listed per file class in one change summary (`more` counts the rest).
STORY_CHANGE_PATHS_PER_CLASS = 8
# Interface rows (parameter / output / resource / dependency adds, drops and
# changes) listed per change summary (`interface_more` counts the rest).
STORY_CHANGE_INTERFACE_MAX = 20
# Per review comment: the diff lines it is attached to (gather keeps at most
# this much); a thread reply carries none (GitHub gives a reply the same
# hunk as the comment that opened the thread). All hunks of one slice share
# a budget; past it they are left out (counted in truncation.hunks_omitted).
STORY_HUNK_CAP = 1500
STORY_HUNK_BUDGET = 15000
# Threads in the open-threads ledger (`threads_omitted` counts the rest).
STORY_THREADS_MAX = 40
# Characters of a thread's `ask` (a verbatim excerpt of the opening text).
STORY_ASK_CAP = 300
# Items linked from the conversation (issues / PRs of the repository that
# are not part of the train) and the characters of each one's description.
STORY_LINKED_MAX = 10
STORY_LINKED_BODY_CAP = 1200
# Names listed per interface group in the outcome block, and changelog items
# per module.
STORY_OUTCOME_NAMES_MAX = 25
STORY_OUTCOME_ITEMS_MAX = 30
# Lines listed per file in a PR's net change (changelog / shared files).
STORY_NET_LINES_MAX = 400     # a changelog entry whole, breaking section included

# The statuses a thread of the open-threads ledger can have (story_threads).
STORY_THREAD_STATUSES = ("approved_after", "answered", "dismissed",
                         "followed_by_commit", "reply_on_item",
                         "unresolved in the record", "resolved on GitHub",
                         "not resolved on GitHub")

_MENTION_RE = re.compile(r"(?<![\w/.@])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
                         r"(?![\w/-])")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_FENCE_RE = re.compile(r"```.*?```", re.S)


def _sha_of_url(url):
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def change_summary(paths, rules, ctx, interface=(), patches=()):
    """The compact, factual summary of one commit's change, or None when
    nothing is known about it: `files` {class: [path]} (path_class, at most
    STORY_CHANGE_PATHS_PER_CLASS per class, `files_more` {class: n} the
    rest), `modules` (the module areas touched, module_kind), `shared`
    (true when a `ci`-class file — a workflow, action or pipeline utility
    every module depends on — changed), `interface` [{change, kind, name,
    path}] (at most STORY_CHANGE_INTERFACE_MAX, `interface_more` the rest)
    and `patches` [{path, kind, added, removed, [lines_omitted]}] (the
    changelog / shared-file lines the commit wrote, from `pr_history`).
    Pure."""
    paths = [p for p in paths or [] if p]
    if not paths and not interface and not patches:
        return None
    out = {}
    if paths:
        by_class = {}
        for p in paths:
            by_class.setdefault(path_class(p, rules), []).append(p)
        out["files"] = {c: sorted(v)[:STORY_CHANGE_PATHS_PER_CLASS]
                        for c, v in sorted(by_class.items())}
        more = {c: len(v) - STORY_CHANGE_PATHS_PER_CLASS
                for c, v in sorted(by_class.items())
                if len(v) > STORY_CHANGE_PATHS_PER_CLASS}
        if more:
            out["files_more"] = more
        mods = sorted({a for a in (ctx.area_of(p) for p in paths)
                       if a and ctx.kind(a)})
        if mods:
            out["modules"] = mods
        if "ci" in by_class:
            out["shared"] = True
    # the module's own interface first, then the tests' / examples'
    rows = sorted(interface, key=lambda r: gather._is_scaffold_path(
        r.get("path") or ""))
    if rows:
        out["interface"] = rows[:STORY_CHANGE_INTERFACE_MAX]
        if len(rows) > STORY_CHANGE_INTERFACE_MAX:
            out["interface_more"] = len(rows) - STORY_CHANGE_INTERFACE_MAX
    if patches:
        out["patches"] = [{k: p[k] for k in ("path", "kind", "added", "removed",
                                             "lines_omitted") if k in p}
                          for p in patches]
    return out


def _interface_rows(events, sha):
    """[{change, kind, name, path}] of the interface events (parameters,
    variables, outputs, resources, module references) of one commit."""
    return [{"change": e.get("change"), "kind": e.get("subkind"),
             "name": e.get("name"), "path": e.get("path")}
            for e in events or []
            if e.get("commit") == sha
            and e.get("subkind") in ("param", "variable", "output", "resource",
                                     "module")]


def _net_lines(patches):
    """{path: {kind, net_added, net_removed, commits}} over a PR's patch
    excerpts in commit order: a line added and later removed (or removed
    and later restored) cancels out; a path with neither left is
    `reverted` (touched, no net change in the kept lines)."""
    out = {}
    for p in patches:
        row = out.setdefault(p["path"], {"kind": p.get("kind"), "added": [],
                                         "removed": [], "commits": []})
        if p.get("commit") not in row["commits"]:
            row["commits"].append(p.get("commit"))
        for ln in p.get("removed") or []:
            if ln in row["added"]:
                row["added"].remove(ln)
            else:
                row["removed"].append(ln)
        for ln in p.get("added") or []:
            if ln in row["removed"]:
                row["removed"].remove(ln)
            else:
                row["added"].append(ln)
    res = {}
    for path, row in sorted(out.items()):
        r = {"kind": row["kind"], "commits": len(row["commits"])}
        if not row["added"] and not row["removed"]:
            r["reverted"] = True
        else:
            r["net_added"] = row["added"][:STORY_NET_LINES_MAX]
            r["net_removed"] = row["removed"][:STORY_NET_LINES_MAX]
            extra = max(0, len(row["added"]) - STORY_NET_LINES_MAX) + \
                max(0, len(row["removed"]) - STORY_NET_LINES_MAX)
            if extra:
                r["lines_more"] = extra
        res[path] = r
    return res


def _latest_verdicts(pr, instant):
    """{reviewer: review} — each human reviewer's latest verdict (approved,
    changes_requested, dismissed) on a PR by the instant, the PR author's
    own reviews excluded."""
    out = {}
    for r in sorted((r for r in pr.get("reviews") or [] if isinstance(r, dict)),
                    key=lambda r: r.get("submitted_at") or ""):
        if r.get("state") not in ("approved", "changes_requested", "dismissed"):
            continue
        if r.get("author") == pr.get("author") \
                or derive.is_bot_login(r.get("author")) \
                or not _not_after(r.get("submitted_at"), instant):
            continue
        out[r.get("author")] = r
    return out


def _mentions(text, exclude=()):
    seen = []
    for m in _MENTION_RE.findall(_FENCE_RE.sub("", text or "")):
        if m not in exclude and m not in seen:
            seen.append(m)
    return seen


def _event_ref(it, actor=None):
    return {"at": it.get("at"), "kind": it.get("kind"),
            "actor": actor or it.get("actor"), "url": it.get("url")}


def ball_with(pr, pr_items, verdicts, reviewers, checks=None, owner_request=None,
              cannot_act=None, review=None):
    """Who the next move on an open PR is with — DERIVED by rule from the
    sequence of its recorded events (`pr_items`, the timeline items on that
    PR in time order), never read from anyone's words. A PR commit counts
    as the PR author's move (its git author name is not a login).
    - R1 mention: the last event is by someone other than the author and
      names other people (@login) -> `awaiting mentioned` (them);
    - R2 author moved: the last event is the author's (an opening, a
      commit, a comment) -> `awaiting reviewer`: the reviewers whose latest
      verdict is a change request, else the people the author named, else
      the reviewers who reviewed; nobody -> `awaiting a first review`;
    - R3 approved: the last event is an approval and no change request is
      outstanding -> `awaiting merge`;
    - R4 reviewer moved: otherwise -> `awaiting author`.
    - R5 failing checks (only with `checks`, the outcome block's checks of
      the PR): when R2 or R3 fired and the head commit's checks are failing
      as of the instant, the next move is the author's -> `awaiting author`
      (`checks_basis` {head, failing}); R1 (somebody was named) and R4 stand.
    - R6 checks awaiting approval: as R5, but every failing check is one
      whose run waits for a maintainer's approval (`action_required`, e.g.
      a first-time contributor's fork) -> `awaiting maintainer`.
    - R7 module owner response requested (only with `owner_request`, the
      PR's owner_response_request): when R2 stands (the author moved, no
      change request outstanding, checks not failing), the next move is
      the module owner's -> `awaiting module owner` (the owners the
      request names, when recorded; `owner_basis` {label, item}).
    - R8 approval made stale by a later push (only with `review`, the PR's
      review_state): when R2 fired, or R4 on an event older than the
      author's last push, and the state is `approved_stale` with no change
      request outstanding -> `awaiting reviewer` (the approver;
      `review_basis` {approved_at, last_push}).
    Precedence: failing checks (R5 / R6) over everything but R1 and R4; an
    outstanding change request over R7 / R8; R1 (a named person) stands;
    then R7 (module owner asked), then R8, then R2 / R3 / R4.
    Automation items (check results) are never the last event, and
    neither is an event by an actor who cannot act (`cannot_act(login)`:
    an automation account re-requesting reviews, a deleted account's
    triage comment) — so an outstanding change request is never
    outranked by an automation re-request.
    Returns {state, with, rule, basis: {at, kind, actor, url}, derived:
    true}, or None when the PR has no recorded event."""
    pr_items = [it for it in pr_items or [] if it.get("kind")
                not in _AUTOMATION_KINDS
                and not (cannot_act and it.get("actor") and cannot_act(it["actor"]))]
    if not pr_items:
        return None
    author = pr.get("author")
    last = pr_items[-1]
    actor = author if last["kind"] in ("commit", "opened", "ready_for_review",
                                       "reopened") else last.get("actor")
    outstanding = [r for r, v in verdicts.items()
                   if v.get("state") == "changes_requested"]
    named = _mentions(last.get("text"), exclude={actor})
    if last["kind"] == "review_requested" and last.get("requested"):
        named = [last["requested"]] + [m for m in named if m != last["requested"]]
    if actor != author and [m for m in named if m != author]:
        state, who, rule = "awaiting mentioned", [m for m in named if m != author], \
            "R1 mention"
    elif actor == author:
        who = outstanding or named or [r for r in reviewers if r != author]
        state = "awaiting reviewer" if who else "awaiting a first review"
        rule = "R2 author moved"
    elif last["kind"] == "review" and last.get("state") == "approved" \
            and not outstanding:
        state, who, rule = "awaiting merge", [], "R3 approved"
    else:
        state, who, rule = "awaiting author", [author] if author else [], \
            "R4 reviewer moved"
    out = {"state": state, "with": who, "rule": rule,
           "basis": _event_ref(last, actor), "derived": True}
    waiting = set((checks or {}).get("awaiting_approval") or [])
    if rule in ("R2 author moved", "R3 approved") and checks \
            and checks.get("rollup") == "failing" and waiting \
            and set(checks.get("failing") or []) <= waiting:
        out.update(state="awaiting maintainer", rule="R6 checks awaiting approval",
                   checks_basis={"head": checks.get("head"),
                                 "awaiting_approval": sorted(waiting)})
        out["with"] = []
    elif rule in ("R2 author moved", "R3 approved") and checks \
            and checks.get("rollup") == "failing":
        out.update(state="awaiting author", rule="R5 failing checks",
                   checks_basis={"head": checks.get("head"),
                                 "failing": checks.get("failing") or []})
        out["with"] = [author] if author else []
    review = review if isinstance(review, dict) else None
    basis_r = (review or {}).get("basis")
    basis_r = basis_r if isinstance(basis_r, dict) else {}
    if review and review.get("state") == "approved_stale" and not outstanding \
            and out["rule"] in ("R2 author moved", "R4 reviewer moved") \
            and (out["rule"] == "R2 author moved"
                 or str(out["basis"].get("at") or "") < str(review.get("last_push") or "")):
        # an approval made stale by the author's later push: the next move
        # is the approver's (checks failing and an outstanding change
        # request keep theirs; a named person, R1, stands)
        approver = basis_r.get("reviewer")
        out.update(state="awaiting reviewer", rule="R8 approval made stale by a later push",
                   review_basis={"approved_at": basis_r.get("at"),
                                 "last_push": review.get("last_push")})
        out["with"] = [approver] if approver else []
    if owner_request and out["rule"] == "R2 author moved" and not outstanding:
        out.update(state="awaiting module owner",
                   rule="R7 module owner response requested",
                   owner_basis={"label": owner_request.get("via"),
                                "item": owner_request.get("item")})
        out["with"] = list(owner_request.get("owners") or [])
    return out


def _issue_ball(issue, items, cannot_act=None):
    """The next move on an open issue with no open PR, derived like
    ball_with: the last event is the reporter's -> `awaiting maintainer
    response`; someone else's naming people -> `awaiting mentioned`;
    someone else's comment that asks nothing (no question) -> `awaiting
    maintainer follow-up` (them, I4); otherwise `awaiting reporter`.
    Events by actors who cannot act
    (`cannot_act`) are never the last event."""
    items = [it for it in items or []
             if not (cannot_act and it.get("actor") and cannot_act(it["actor"]))]
    if not items:
        return None
    last = items[-1]
    author = issue.get("author")
    actor = author if last["kind"] == "opened" else last.get("actor")
    named = [m for m in _mentions(last.get("text"), exclude={actor})
             if m != author]
    if actor == author:
        state, who, rule = "awaiting maintainer response", [], "I1 reporter moved"
    elif named:
        state, who, rule = "awaiting mentioned", named, "I2 mention"
    elif last["kind"] in ("comment", "review_comment") and not _has_question(
            last.get("text")):
        # someone else answered without asking the reporter anything ("pinged
        # internally"): the next move is still theirs
        state, who, rule = "awaiting maintainer follow-up", \
            [last.get("actor")] if last.get("actor") else [], "I4 answered without a question"
    else:
        state, who, rule = "awaiting reporter", [author] if author else [], \
            "I3 other moved"
    return {"state": state, "with": who, "rule": rule,
            "basis": _event_ref(last, actor), "derived": True}


def _ask_text(text):
    """A verbatim excerpt of what a comment asks: the first sentence with a
    question mark on a line that is not a quotation (`> ...`) or inside a
    code block, else the opening of its first such line; at most
    STORY_ASK_CAP characters. None for an empty text."""
    body = _FENCE_RE.sub("", text or "")
    lines = [ln.strip() for ln in body.split("\n")
             if ln.strip() and not ln.lstrip().startswith(">")]
    for ln in lines:
        if "?" in ln:
            for sent in re.split(r"(?<=[.!?])\s+", ln):
                if "?" in sent and not sent.startswith(("http://", "https://")):
                    return sent[:STORY_ASK_CAP]
    return lines[0][:STORY_ASK_CAP] if lines else None


def _signal_rank(sig):
    """Strongest resolution signal first: an approval after a change
    request, an answer in the thread, a dismissal, a commit touching the
    thread's file, a reply on the same item (thread links not stored), a
    later commit on the PR whose files are not known to match."""
    order = {"approved_after": 0, "answered": 1, "dismissed": 2,
             "followed_by_commit": 3, "reply_on_item": 4}
    if sig["status"] == "followed_by_commit" and sig.get("same_file") is None:
        return 5
    return order.get(sig["status"], 9)


def _has_question(text):
    body = _FENCE_RE.sub("", text or "")
    body = re.sub(r"https?://\S+", "", body)
    return any("?" in ln for ln in body.split("\n")
               if not ln.lstrip().startswith(">"))


def story_threads(items, pr_index, issue_index):
    """The open-threads ledger: every question or request raised in the
    conversation, with its resolution status BY RULE. A thread is opened by
    a review comment that starts a thread (not a reply) by anyone but the
    PR author, or by the author with a question mark; a `changes_requested`
    review; a comment (issue or PR conversation) or a review text with a
    question mark (quotations and code blocks ignored). Each {id, kind
    (review_thread / change_request / question), on, at, by, url, ask
    (_ask_text, verbatim), [path], [replies], status, [resolution
    {status, at, by, url, same_file?}], signals}. Status, strongest first:
    `approved_after` (a change request followed by an approval from the
    same reviewer), `answered` (a reply by someone else in the same review
    thread — GitHub's thread link, `in_reply_to` — or, for a conversation
    question, a later comment on the same item by someone else),
    `dismissed`, `followed_by_commit` (a later commit on the PR touching
    the thread's file: `same_file: true`; `same_file: null` when the file is
    not stored, any later commit on the PR), `reply_on_item` (a later reply
    on the same PR by someone else when the review thread links are not
    stored), else `unresolved in the record`. GitHub's own
    resolved / unresolved flag is not gathered. Pure over the timeline."""
    threads, replies = [], {}
    for it in items:
        if it["kind"] == "review_comment" and it.get("in_reply_to"):
            replies.setdefault(it["in_reply_to"], []).append(it)
    linked_prs = {it["on"]["number"] for it in items
                  if it["kind"] == "review_comment" and it.get("on")
                  and ("in_reply_to" in it or "path" in it)}
    for i, it in enumerate(items):
        on = it.get("on") or {}
        rec = (pr_index if on.get("type") == "pr" else issue_index).get(
            on.get("number")) or {}
        author = rec.get("author")
        kind = None
        if it["kind"] == "review_comment" and not it.get("in_reply_to") \
                and (it.get("actor") != author or _has_question(it.get("text"))):
            kind = "review_thread"
        elif it["kind"] == "review" and it.get("state") == "changes_requested":
            kind = "change_request"
        elif it["kind"] in ("comment", "review", "review_comment") \
                and _has_question(it.get("text")):
            kind = "question"
        if kind is None:
            continue
        t = {"kind": kind, "on": on, "at": it.get("at"), "by": it.get("actor"),
             "url": it.get("url")}
        ask = _ask_text(it.get("text"))
        if ask:
            t["ask"] = ask
        if it.get("path"):
            t["path"] = it["path"]
        signals = []
        later = items[i + 1:]
        same = [x for x in later if (x.get("on") or {}) == on]
        if kind == "review_thread":
            reps = replies.get(it.get("url")) or []
            if reps:
                t["replies"] = len(reps)
            other = next((r for r in reps if r.get("actor") != it.get("actor")), None)
            if other is not None:
                signals.append(("answered", other, None))
            elif on.get("number") not in linked_prs:
                rep = next((x for x in same if x["kind"] in
                            ("comment", "review_comment", "review")
                            and x.get("text") and x.get("actor") != it.get("actor")),
                           None)
                if rep is not None:
                    signals.append(("reply_on_item", rep, None))
        elif kind == "change_request":
            ok = next((x for x in same if x["kind"] == "review"
                       and x.get("actor") == it.get("actor")
                       and x.get("state") in ("approved", "dismissed")), None)
            if ok is not None:
                signals.append(("approved_after" if ok["state"] == "approved"
                                else "dismissed", ok, None))
        else:
            rep = next((x for x in same if x["kind"] in
                        ("comment", "review_comment", "review")
                        and x.get("text") and x.get("actor") != it.get("actor")),
                       None)
            if rep is not None:
                signals.append(("answered", rep, None))
        if on.get("type") == "pr" and kind != "question":
            commits = [x for x in same if x["kind"] == "commit"
                       and x.get("source") == "pr_head" and not x.get("merge")]
            path = it.get("path")
            if path:
                hit = next((x for x in commits if path in
                            [p for ps in ((x.get("change") or {}).get("files")
                                          or {}).values() for p in ps]), None)
                if hit is not None:
                    signals.append(("followed_by_commit", hit, True))
            elif commits:
                signals.append(("followed_by_commit", commits[0], None))
        t["signals"] = [{"status": s, "at": x.get("at"),
                         "by": x.get("actor"), "url": x.get("url"),
                         **({"same_file": f} if s == "followed_by_commit" else {})}
                        for s, x, f in signals]
        best = min(t["signals"], key=_signal_rank, default=None)
        t["status"] = best["status"] if best else "unresolved in the record"
        if best:
            t["resolution"] = best
        threads.append(t)
    for n, t in enumerate(threads, 1):
        t["id"] = "T{}".format(n)
    return threads


def _linked_numbers(bundle, texts, own):
    """[(number, first citing url)] of the repository's issues / PRs named in
    the conversation's texts (`#N` not preceded by a word, `/` or `.`, or
    a github.com/<owner>/<repo>/issues|pull/N link), HTML comments (a
    template's "Fixes #123") left out, the train's own numbers excluded, in
    order of first mention."""
    slug = repo_slug(bundle.get("meta"))
    link_re = re.compile(r"https://github\.com/{}/(?:issues|pull)/(\d+)".format(
        re.escape(slug or "\x00")), re.I)
    found = []
    for text, url in texts:
        t = _HTML_COMMENT_RE.sub("", text or "")
        nums = [int(n) for n in link_re.findall(t)]
        stripped = re.sub(r"https?://\S+", "", t)
        nums += [int(n) for n in re.findall(r"(?<![\w/.&])#(\d+)\b", stripped)]
        for n in nums:
            if n not in own and n not in [f[0] for f in found]:
                found.append((n, url))
    return found


def _issue_ask(body):
    """The description of a linked item, verbatim: from an issue form's
    `### Description` heading when it has one, else from the start; at most
    STORY_LINKED_BODY_CAP characters. (text, original length)."""
    b = (body or "").replace("\r\n", "\n")
    k = b.find("### Description")
    if k >= 0:
        b = b[k + len("### Description"):]
    b = _HTML_COMMENT_RE.sub("", b).strip()
    return b[:STORY_LINKED_BODY_CAP], len(b)


def linked_item_as_of(item, instant):
    """A `linked_items` row (an item outside the view, read when gathered)
    projected to the instant: {number, type, title, author, url, created_at,
    state (as of the instant: `not yet created` / `open` / `closed` /
    `merged`), state_when_gathered, [closed_at] / [merged_at] (when by the
    instant), [state_reason], in_view: false}."""
    out = {"number": item.get("number"), "type": item.get("type"),
           "title": item.get("title"), "author": item.get("author"),
           "url": item.get("url"), "created_at": item.get("created_at"),
           "state_when_gathered": item.get("state"), "in_view": False}
    if item.get("created_at") and not _not_after(item["created_at"], instant):
        out["state"] = "not yet created"
    elif item.get("merged_at") and _not_after(item["merged_at"], instant):
        out.update(state="merged", merged_at=item["merged_at"])
    elif item.get("closed_at") and _not_after(item["closed_at"], instant):
        out.update(state="closed", closed_at=item["closed_at"])
        if item.get("state_reason"):
            out["state_reason"] = item["state_reason"]
    else:
        out["state"] = "open"
    return {k: v for k, v in out.items() if v is not None}


def _linked_items(bundle, texts, own):
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    outside = {it.get("number"): it for it in
               (bundle.get("linked_items") or {}).get("items") or []
               if isinstance(it, dict)}
    instant = window_end_instant(bundle.get("meta"))
    out = []
    found = _linked_numbers(bundle, texts, own)
    for n, cited in found[:STORY_LINKED_MAX]:
        rec = issue_index.get(n) or pr_index.get(n)
        if rec is None:
            if n in outside:
                row = linked_item_as_of(outside[n], instant)
                row["mentioned_in"] = cited
                out.append(row)
            else:
                row = {"number": n, "in_view": False, "mentioned_in": cited}
                if "linked_items" in bundle:
                    row["not_read"] = "not found" if n in set(
                        bundle["linked_items"].get("missing") or []) \
                        else "not read (capped or failed)"
                out.append(row)
            continue
        typ = "issue" if n in issue_index else "pr"
        row = {"number": n, "type": typ, "title": rec.get("title"),
               "author": rec.get("author"), "state": rec.get("state"),
               "created_at": rec.get("created_at"), "url": rec.get("url"),
               "mentioned_in": cited}
        if typ == "pr":
            row["merged"] = bool(rec.get("merged"))
        if rec.get("closed_at") and rec.get("state") == "closed":
            row["closed_at"] = rec["closed_at"]
        human = _human_rows(rec.get("comments_list"))
        row["comments"] = len(human)
        last = max([c.get("created_at") or "" for c in human]
                   + [rec.get("created_at") or ""])
        row["last_activity_at"] = last or None
        ask, n_chars = _issue_ask(rec.get("body"))
        if ask:
            row["description"] = ask
            if n_chars > len(ask):
                row["description_truncated_from"] = n_chars
        out.append(row)
    return out, max(0, len(found) - STORY_LINKED_MAX)


def _names(rows):
    return [r.get("name") for r in rows or []][:STORY_OUTCOME_NAMES_MAX]


def _outcome_modules(bundle, train):
    """What ended up in the product, from the module layer: every module
    entry one of the train's PRs contributed to, with its status, versions,
    the changelog entries the train's PRs wrote (verbatim items), the
    interface names added / removed / modified, child modules, and declared
    breaking items and breaking candidates."""
    mc = bundle.get("module_changes") or {}
    prs = set(train.get("prs") or [])
    out = []
    for e in mc.get("modules") or []:
        if not {p.get("number") for p in e.get("prs") or []} & prs:
            continue
        row = {"area": e.get("area"), "kind": e.get("kind"),
               "status": e.get("status")}
        row.update(module_info(bundle, e.get("area")))
        if e.get("first_version"):
            row["first_version"] = e["first_version"]
        v = e.get("versions") or {}
        vers = {k: v[k] for k in ("changelog", "declared_not_published",
                                  "published_not_declared") if v.get(k)}
        if v.get("published"):
            vers["published"] = [{k: p.get(k) for k in ("version", "tag", "date")}
                                 for p in v["published"]]
        if v.get("version_file"):
            vers["version_file"] = {k: v["version_file"].get(k)
                                    for k in ("from", "to")}
        if vers:
            row["versions"] = vers
        entries, n_items = [], 0
        for c in e.get("changelog") or []:
            if not any(s.get("pr") in prs for s in c.get("sources") or []):
                continue
            secs = []
            for s in c.get("sections") or []:
                items = [i.get("text") for i in s.get("items") or []]
                keep = items[:max(0, STORY_OUTCOME_ITEMS_MAX - n_items)]
                n_items += len(keep)
                sec = {"heading": s.get("heading"), "kind": s.get("kind"),
                       "items": keep}
                if s.get("placeholder"):
                    sec["placeholder"] = s["placeholder"]
                secs.append(sec)
            entries.append({"version": c.get("version"), "sections": secs})
        if entries:
            row["changelog"] = entries
        iface = {}
        for group, changes in (e.get("interface") or {}).items():
            g = {ch: _names(rows) for ch, rows in changes.items() if rows}
            g.update({ch + "_more": len(rows) - STORY_OUTCOME_NAMES_MAX
                      for ch, rows in changes.items()
                      if len(rows or []) > STORY_OUTCOME_NAMES_MAX})
            if g:
                iface[group] = g
        if iface:
            row["interface"] = iface
        kids = [{"path": c.get("path"), "change": c.get("change")}
                for c in (e.get("structure") or {}).get("children") or []]
        if kids:
            row["children"] = kids
        br = e.get("breaking") or {}
        b = {}
        if br.get("declared"):
            b["declared"] = [d.get("text") for d in br["declared"]
                             if any(s.get("pr") in prs for s in d.get("sources") or [])]
        if br.get("candidates"):
            b["candidates"] = [{"rule": c.get("rule"), "subject": c.get("subject")}
                               for c in br["candidates"]
                               if any(s.get("pr") in prs for s in c.get("sources") or [])]
        b = {k: x for k, x in b.items() if x}
        if b:
            row["breaking"] = b
        out.append(row)
    return out


def story_outcome(bundle, train, items, history, instant, texts, activity=None):
    """The outcome block of a story: where the train's items stand as of
    the instant and what ended up in the product. {train_outcome, as_of,
    items: [per issue / PR {type, number, title, author, state, url, ...;
    for a PR: merged, [merged_at], reviews {verdicts: [{reviewer, state,
    at, url}], outstanding_change_requests: [{by, since, url}]}, checks
    (checks_outcome from the view's `pr_activity`, else "not gathered"),
    [last_event], [ball]; for an open issue with no open
    PR: [last_event], [ball]}], [ball] (the train's: its latest open PR's,
    else its open issue's), [waiting_on] (open linked items the PR author's
    latest message names), linked: [...] (_linked_items), [linked_omitted],
    [modules] (_outcome_modules: shipped work), [net_change] {pr number:
    {files, changelog / shared paths net of the PR's own reverts}} for open
    PRs with stored patch excerpts}. Every item row carries `modules`,
    `modules_source` and `module_urls` (record_modules); an open item
    carrying the owner-response label is listed in
    `owner_response_requested` [{item, module, label, since, waits_on:
    "the module owner", [owners]}] and an open PR's ball follows R7."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    issues, prs = _train_members(train, pr_index, issue_index)
    out = {"train_outcome": train.get("outcome"), "as_of": instant, "items": []}
    own = {r.get("number") for r in issues + prs}
    ctx = _module_ctx(bundle)
    rxs = _owner_response_patterns(bundle)
    waits = []
    open_prs, open_issue_balls = [], []
    for rec in issues + prs:
        typ = "pr" if rec in prs else "issue"
        row = {"type": typ, "number": rec.get("number"), "title": rec.get("title"),
               "author": rec.get("author"), "state": rec.get("state"),
               "created_at": rec.get("created_at"), "url": rec.get("url")}
        mods, msrc = record_modules(bundle, ctx, typ, rec, instant)
        row.update(modules=mods, modules_source=msrc,
                   module_urls=module_urls(bundle, mods))
        if any(module_info(bundle, a) for a in mods):
            row["module_info"] = {a: module_info(bundle, a) for a in mods
                                  if module_info(bundle, a)}
        if typ == "pr" and rec.get("proposes_new_module"):
            row["proposes_new_module"] = rec["proposes_new_module"]
        req = owner_response_request(bundle, ctx, typ, rec, instant, rxs)
        if req:
            w = {"item": req["item"], "module": req["module"], "label": req["via"],
                 "since": req["date"]}
            if rec.get("state") == "open":
                w["waits_on"] = "the module owner"
            else:
                # it waited on them until it closed / merged
                w["waited_on"] = "the module owner"
                w["until"] = rec.get("merged_at") or rec.get("closed_at")
            if req.get("owners"):
                w["owners"] = req["owners"]
                if req.get("owners_source"):
                    w["owners_source"] = req["owners_source"]
            waits.append(w)
            req = req if rec.get("state") == "open" else None
        owners_of = {a: module_owners(bundle)[a] for a in mods
                     if module_owners(bundle).get(a)}
        if owners_of:
            row["module_owners"] = owners_of      # who "the module's owner" is
        on_items = [it for it in items
                    if (it.get("on") or {}) == {"type": typ, "number": rec.get("number")}
                    and it["kind"] not in ("merged", "closed")
                    and it["kind"] not in _AUTOMATION_KINDS]
        if typ == "pr":
            row["merged"] = bool(rec.get("merged"))
            if rec.get("merged_at"):
                row["merged_at"] = rec["merged_at"]
            if "is_fork" in rec:
                # "from a fork of ...": the head branch's repository (null:
                # the fork was deleted)
                row["head_repo"] = rec.get("head_repo")
                row["is_fork"] = rec["is_fork"]
            if "fork_status" in rec:
                row["fork_status"] = rec["fork_status"]
            verdicts = _latest_verdicts(rec, instant)
            row["review_state"] = rec.get("review_state") or \
                review_state(bundle, rec, instant)
            row["reviews"] = {
                "verdicts": [{"reviewer": r, "state": v.get("state"),
                              "at": v.get("submitted_at"), "url": v.get("url")}
                             for r, v in sorted(verdicts.items())],
                "outstanding_change_requests": [
                    {"by": r, "since": v.get("submitted_at"), "url": v.get("url")}
                    for r, v in sorted(verdicts.items())
                    if v.get("state") == "changes_requested"]}
            arow = (activity or {}).get(rec.get("number"))
            row["checks"] = checks_outcome(arow, instant, rec.get("url")) \
                if arow is not None else "not gathered"
            if rec.get("state") == "open":
                if on_items:
                    row["last_event"] = _event_ref(on_items[-1])
                reviewers = sorted({it.get("actor") for it in on_items
                                    if it["kind"] in ("review", "review_comment")
                                    and it.get("actor") != rec.get("author")})
                ball = ball_with(rec, on_items, verdicts, reviewers,
                                 row["checks"] if isinstance(row["checks"], dict)
                                 else None, owner_request=req,
                                 cannot_act=lambda x: _cannot_act(bundle, x),
                                 review=row.get("review_state"))
                if ball:
                    row["ball"] = ball
                open_prs.append((rec.get("created_at") or "", row))
        elif rec.get("state") == "open":
            if on_items:
                row["last_event"] = _event_ref(on_items[-1])
            ball = _issue_ball(rec, on_items, lambda x: _cannot_act(bundle, x))
            if ball:
                row["ball"] = ball
                open_issue_balls.append(ball)
        elif rec.get("closed_at"):
            row["closed_at"] = rec["closed_at"]
        if typ == "pr" and rec.get("closed_unmerged"):
            row["closed_unmerged"] = rec["closed_unmerged"]
        if typ == "pr":
            rel = related_prs(bundle, rec)
            if rel:
                row["related"] = rel
        else:
            fp = failed_pipeline(rec)
            if fp:
                row["failed_pipeline"] = fp
        out["items"].append(row)
    if open_prs:
        main = max(open_prs, key=lambda e: e[0])[1]
        if main.get("ball"):
            out["ball"] = dict(main["ball"], pr=main["number"])
    elif open_issue_balls:
        out["ball"] = open_issue_balls[-1]
    if waits:
        out["owner_response_requested"] = waits   # waits on the module owner
    linked, more = _linked_items(bundle, texts, own)
    out["linked"] = linked
    if more:
        out["linked_omitted"] = more
    if open_prs:
        for _, row in open_prs:
            author = row.get("author")
            mine = [it for it in items if it.get("actor") == author
                    and it["kind"] in ("comment", "review_comment", "review")
                    and it.get("text")]
            if not mine:
                continue
            last_text = mine[-1]
            nums = [n for n, _u in _linked_numbers(bundle, [(last_text["text"],
                                                             last_text["url"])], own)]
            waiting = [{"number": l["number"], "type": l.get("type"),
                        "state": l.get("state"), "title": l.get("title"),
                        "named_in": last_text["url"]}
                       for l in linked if l["number"] in nums
                       and l.get("state") == "open"]
            if waiting:
                out["waiting_on"] = waiting
    mods = _outcome_modules(bundle, train)
    if mods:
        out["modules"] = mods
    if train.get("outcome") == "shipped":
        rel = train_released(bundle, train)
        if rel:
            out["released"] = rel   # does "landed" mean released?
    net = {}
    for p in prs:
        if p.get("state") != "open":
            continue
        row = history.get(p.get("number")) or {}
        ok = {c["sha"] for c in _pr_own_commits_as_of(row, instant)}
        if not row.get("commits"):
            continue
        paths = sorted({x for c in _pr_own_commits_as_of(row, instant)
                        for x in c.get("paths") or []})
        entry = {}
        if paths:
            rules = path_class_rules((bundle.get("module_changes") or {})
                                     .get("path_rules") or ())
            by_class = {}
            for x in paths:
                by_class.setdefault(path_class(x, rules), []).append(x)
            entry["files_touched"] = dict(sorted(by_class.items()))
        patches = [x for x in row.get("patches") or [] if x.get("commit") in ok]
        if patches:
            entry["lines"] = _net_lines(patches)
        files = _net_files(row, instant, _pr_own_commits_as_of(row, instant))
        if files:
            entry["net_files"] = files      # the PR as a whole, per file
        if entry:
            net[str(p.get("number"))] = entry
    if net:
        out["net_change"] = net
    return out


def story_turning_points(items, open_as_of=None):
    """Turning points of a story timeline, by rule (data, not prose). Each
    {rule, at, index, url, detail?}:
    - `still_silent` (only with `open_as_of`, the as-of instant of a train
      still open then): no recorded human event in the last
      STORY_SILENCE_DAYS days before it (detail: days, since; `index` is
      the last item's);
    - `change_request`: a review with state changes_requested;
    - `reopened`: an issue / PR reopened;
    - `silence_then_activity`: activity after more than STORY_SILENCE_DAYS
      days without any recorded human event (detail: days, since);
    - `rework_after_change_request`: the first PR commit after a change
      request on that PR;
    - `history_rewritten`: a PR commit committed more than
      STORY_REWRITE_HOURS after it was authored (amended, rebased or
      cherry-picked; the branch's earlier states are not kept), once per PR
      per day;
    - `new_attempt`: a PR of the train opened after another of its PRs was
      closed unmerged.
    A scope change (a title edit) cannot be marked: edits are not gathered."""
    out = []
    pending_cr = {}
    rewritten = set()
    closed_unmerged = set()
    merged = {it["on"]["number"] for it in items
              if it["kind"] == "merged" and it.get("on")}
    prev = None
    for i, it in enumerate(items):
        dt = _parse_ts(it["at"])
        on = it.get("on") or {}
        if it["kind"] in _AUTOMATION_KINDS:
            continue
        if prev is not None and dt is not None \
                and (dt - prev).total_seconds() > STORY_SILENCE_DAYS * 86400:
            out.append({"rule": "silence_then_activity", "at": it["at"],
                        "index": i, "url": it.get("url"),
                        "detail": {"days": _days(prev, dt),
                                   "since": prev.strftime("%Y-%m-%dT%H:%M:%SZ")}})
        if dt is not None:
            prev = dt
        kind = it["kind"]
        if kind == "review" and it.get("state") == "changes_requested":
            out.append({"rule": "change_request", "at": it["at"], "index": i,
                        "url": it.get("url")})
            pending_cr[on.get("number")] = i
        elif kind == "reopened":
            out.append({"rule": "reopened", "at": it["at"], "index": i,
                        "url": it.get("url")})
        elif kind == "commit" and it.get("source") == "pr_head":
            n = on.get("number")
            if n in pending_cr:
                out.append({"rule": "rework_after_change_request", "at": it["at"],
                            "index": i, "url": it.get("url"),
                            "detail": {"change_request_index": pending_cr.pop(n)}})
            a, c = _parse_ts(it.get("authored_at")), dt
            if a is not None and c is not None and \
                    (c - a).total_seconds() > STORY_REWRITE_HOURS * 3600 and \
                    (n, it["at"][:10]) not in rewritten:
                rewritten.add((n, it["at"][:10]))
                out.append({"rule": "history_rewritten", "at": it["at"],
                            "index": i, "url": it.get("url"),
                            "detail": {"authored_at": it.get("authored_at")}})
        elif kind == "closed" and on.get("type") == "pr" \
                and on.get("number") not in merged:
            closed_unmerged.add(on.get("number"))
        elif kind == "opened" and on.get("type") == "pr" and closed_unmerged \
                and on.get("number") not in closed_unmerged:
            out.append({"rule": "new_attempt", "at": it["at"], "index": i,
                        "url": it.get("url"),
                        "detail": {"after_closed": sorted(closed_unmerged)}})
    end = _parse_ts(open_as_of)
    if end is not None and prev is not None and items \
            and (end - prev).total_seconds() > STORY_SILENCE_DAYS * 86400:
        out.append({"rule": "still_silent", "at": open_as_of,
                    "index": len(items) - 1, "url": items[-1].get("url"),
                    "detail": {"days": _days(prev, end),
                               "since": prev.strftime("%Y-%m-%dT%H:%M:%SZ")}})
    return out


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
_QUOTE_RE = re.compile(r"\"([^\"\n]{4,})\"|“([^”\n]{4,})”")


def _slice_strings(obj, urls, texts):
    if isinstance(obj, dict):
        for v in obj.values():
            _slice_strings(v, urls, texts)
    elif isinstance(obj, list):
        for v in obj:
            _slice_strings(v, urls, texts)
    elif isinstance(obj, str):
        texts.append(obj)
        if obj.startswith(("http://", "https://")):
            urls.add(obj)
        urls.update(_URL_IN_TEXT_RE.findall(obj))


def _norm_ws(s):
    return " ".join((s or "").split())


_MONTH_NAMES = ("January|February|March|April|May|June|July|August|September|"
                "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|"
                "Oct|Nov|Dec")
_DATE_IN_PROSE_RE = re.compile(
    r"\b(\d{4})-\d{2}-\d{2}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MONTH_NAMES + r")\.?(?:,?\s+(\d{4}))?\b"
    r"|\b(?:" + _MONTH_NAMES + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?")
_CLOSING_RE = re.compile(r"^[#*_\s>]*(Where it stands|What landed)\b", re.I | re.M)
_STATE_WORD_RE = re.compile(r"\b(open|closed|merged|unmerged|draft)\b", re.I)
STORY_NO_DISCUSSION = "No notable discussion."


def _slice_repo_base(slice_obj):
    """`https://github.com/<owner>/<repo>/` of the slice's train, from its
    evidence or timeline URLs; None when no URL names one."""
    urls = [e.get("url") for e in (slice_obj.get("train") or {}).get("evidence") or []]
    urls += [it.get("url") for it in (slice_obj.get("conversation") or {})
             .get("timeline") or []]
    for u in urls:
        m = re.match(r"(https://github\.com/[^/\s]+/[^/\s]+/)(?:issues|pull)/\d+", u or "")
        if m:
            return m.group(1)
    return None


def _slice_states(slice_obj):
    """{number: (state, merged)} of every issue / PR the slice's outcome
    block states (the train's own items and the linked ones in the view)."""
    out = {}
    oc = (slice_obj.get("conversation") or {}).get("outcome") or {}
    for row in (oc.get("items") or []) + (oc.get("linked") or []):
        if row.get("number") is not None and row.get("state"):
            out[row["number"]] = (row["state"], bool(row.get("merged")))
    return out


def _story_numbers(text, base):
    """Issue / PR numbers a story names: `#N` (not preceded by a word, `/`,
    `.` or `&`) and links to the repository's issues / pulls."""
    nums = []
    if base:
        for n in re.findall(re.escape(base) + r"(?:issues|pull)/(\d+)", text):
            if int(n) not in nums:
                nums.append(int(n))
    for n in re.findall(r"(?<![\w/.&])#(\d+)\b", _URL_IN_TEXT_RE.sub(" ", text)):
        if int(n) not in nums:
            nums.append(int(n))
    return nums


def check_story_structure(slice_obj, text):
    """The mechanical part of the story brief's structure rules. Pure.

    - `first_date_without_year`: the first date the prose names (an ISO
      date, "10 June", "June 10", "Nov 1", ... outside URLs) must carry a
      year ("10 June 2026", "2026-06-10"); else the date as written.
    - `closing_paragraph_missing`: a story ends with a paragraph (or
      heading) starting "Where it stands" (a train in flight) or "What
      landed" (a shipped train; any other outcome accepts either).
    - `numbers_without_state`: every issue / PR number the story names
      (`#N` or a link to the repository's issue / pull) is named again in
      that closing part, in a sentence with a state word (open, closed,
      merged, unmerged, draft).
    - `state_mismatch`: such a sentence's state words must include the
      state the slice records for that number (the outcome block's items
      and linked items; a merged PR accepts "merged" or "closed").
    A story that is exactly STORY_NO_DISCUSSION is exempt. What this cannot
    check — that every thread mentioned is closed, that a turning point is
    explained in substance, that quotes are fair in context — stays the
    writer's responsibility (reference/report-sections.md)."""
    out = {"first_date_without_year": None, "closing_paragraph": None,
           "closing_paragraph_missing": False, "numbers_without_state": [],
           "state_mismatch": [], "numbers_checked": 0}
    if (text or "").strip() == STORY_NO_DISCUSSION:
        return out
    prose = _URL_IN_TEXT_RE.sub(lambda m: " " * len(m.group(0)), text or "")
    m = _DATE_IN_PROSE_RE.search(prose)
    if m and not (m.group(1) or m.group(2) or m.group(3)):
        out["first_date_without_year"] = m.group(0)
    outcome = (slice_obj.get("train") or {}).get("outcome")
    want = {"in_flight": ("where it stands",), "shipped": ("what landed",)}.get(
        outcome, ("where it stands", "what landed"))
    heads = [h for h in _CLOSING_RE.finditer(text or "")
             if h.group(1).lower() in want]
    if not heads:
        out["closing_paragraph_missing"] = True
        closing = ""
    else:
        out["closing_paragraph"] = heads[-1].group(1)
        closing = (text or "")[heads[-1].start():]
    base = _slice_repo_base(slice_obj)
    nums = _story_numbers(text or "", base)
    out["numbers_checked"] = len(nums)
    states = _slice_states(slice_obj)
    item_link = re.compile(re.escape(base) + r"(?:issues|pull)/(\d+)") \
        if base else None

    def as_numbers(m):
        """A link to the repository's issue / PR reads as `#N`; any other
        URL as blank (its words are not prose)."""
        hit = item_link.match(m.group(0)) if item_link else None
        return " #{} ".format(hit.group(1)) if hit else " "
    # split into sentences BEFORE rewriting URLs: their dots are no ends
    sentences = [_URL_IN_TEXT_RE.sub(as_numbers, s) for s in re.split(
        r"(?<=[.;!?])\s+|\n+", closing)]
    any_num = re.compile(r"(?<![\w/.&])#\d+\b")

    def state_words(n):
        """The state words said of #n: in the clause from its mention to
        the next number's mention, else (#1 and #2 are open) in its
        whole sentence."""
        pat = re.compile(r"(?<![\w/.&])#{}\b".format(n))
        words = set()
        for s in sentences:
            for m in pat.finditer(s):
                nxt = any_num.search(s, m.end())
                seg = s[m.start():nxt.start() if nxt else len(s)]
                found = {w.lower() for w in _STATE_WORD_RE.findall(seg)}
                words |= found or {w.lower() for w in _STATE_WORD_RE.findall(s)}
        return words
    for n in nums:
        words = state_words(n)
        if not words:
            out["numbers_without_state"].append(n)
            continue
        if n in states:
            state, merged = states[n]
            ok = {"merged", "closed"} if merged else {state}
            if not words & ok:
                out["state_mismatch"].append({"number": n,
                                              "stated": sorted(words),
                                              "recorded": "merged" if merged
                                              else state})
    return out


# A sentence saying something is not known / not in the record.
_UNKNOWN_RE = re.compile(
    r"\b(?:not (?:in|part of) the record|(?:is|are|were|was) not (?:gathered|"
    r"recorded|stored|known|kept)|not gathered|unknown|cannot be known|"
    r"no record of|isn't recorded|aren't recorded|not shown)\b", re.I)
_CHECKS_WORD_RE = re.compile(r"\b(?:CI|checks?|check runs?|check results?|"
                             r"pipeline (?:runs?|results?))\b", re.I)
_RESOLVED_WORD_RE = re.compile(r"\bresolved\b", re.I)


# Roll-ups that state a result: `incomplete` / `unavailable` do not, so a
# story may call those checks unknown.
_CHECKS_KNOWN = ("passing", "failing", "pending", "none")
# A sentence about commits a force-push replaced (history, not the head).
_REPLACED_RE = re.compile(r"\b(?:force[- ]?push(?:ed|es)?|replaced|rewritten)\b",
                          re.I)
# Another repository's issue / PR: a github.com link outside the slice's
# repository, or `owner/repo#N`; marked in the normalised sentence.
_FOREIGN_ITEM_URL_RE = re.compile(
    r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")
_FOREIGN_SHORT_REF_RE = re.compile(r"(?<![\w/.])([\w.-]+/[\w.-]+)#(\d+)\b")
_FOREIGN_ITEM = "\x00foreign-item\x00"
# A sentence about a PR's earlier commits / pushes (not its head).
_EARLIER_COMMITS_RE = re.compile(
    r"\b(?:earl(?:y|ier|iest)|older|first|initial|previous|prior|preceding)\b"
    r"[^.;!?]*\b(?:commits?|push(?:es)?|runs?)\b", re.I)


def check_story_record_claims(slice_obj, text):
    """Sentences of a story that call unknown what the slice holds, each
    claim scoped to what it names: `checks_said_unknown` — a sentence
    naming CI / checks / check runs and saying they are not in the record
    (not gathered, unknown, ...) about a PR (#N or its link) whose outcome
    `checks` state a result (gathered, roll-up not incomplete /
    unavailable), or naming no PR while every PR of the slice has one;
    `resolved_said_unknown` — likewise for whether a thread was resolved,
    against the PRs whose resolution coverage is complete (every review
    thread in the ledger carries GitHub's `resolved_on_github` flag, gather
    omitted none — `thread_coverage` — and the ledger cap hid none; a PR
    with no ledger entry is not covered). A claim about a PR's earlier
    commits / pushes / runs needs every commit of it covered
    (`commits_covered` = `commits_total`) with a complete result each (no
    `commits_incomplete`), not only its head. A sentence naming several items is
    refused only when all of them are covered (a linked item outside the
    outcome has no coverage, so a claim naming it passes); a general claim only when
    every PR of the slice is. A link or `owner/repo#N` reference to the
    slice's own repository (slug compared case-insensitively) is its `#N`;
    one to another repository is never covered. A PR
    with a recorded force-push never has covered history (the replaced
    commits' checks were not read), so no claim about its earlier or
    replaced commits is refused; a claim about its head still is. A true
    statement of a coverage limit always passes. Pure."""
    conv = slice_obj.get("conversation") or {}
    oc = conv.get("outcome") or {}
    prs = [r.get("number") for r in oc.get("items") or [] if r.get("type") == "pr"]
    known = {r.get("number") for r in oc.get("items") or []
             if isinstance(r.get("checks"), dict)
             and r["checks"].get("status") == "gathered"
             and r["checks"].get("rollup") in _CHECKS_KNOWN}
    # the head is known, but are all of the PR's commits covered?
    all_commits = {r.get("number") for r in oc.get("items") or []
                   if r.get("number") in known
                   and not (isinstance(r["checks"].get("commits_total"), int)
                            and r["checks"]["commits_total"]
                            > (r["checks"].get("commits_covered") or 0))
                   and not r["checks"].get("commits_incomplete")}
    # a recorded force-push replaced commits whose checks were never read:
    # such a PR's history is never covered, whatever its current commits
    forced = {(it.get("on") or {}).get("number")
              for it in conv.get("timeline") or []
              if it.get("kind") == "head_ref_force_pushed"}
    all_commits -= forced
    threads = {}
    for t in conv.get("threads") or []:
        if t.get("kind") == "review_thread":
            threads.setdefault((t.get("on") or {}).get("number"), []).append(
                "resolved_on_github" in t)
    cov = conv.get("thread_coverage") or {}
    # complete only when every visible review thread of the PR has a flag,
    # gather read all of them, and the slice's ledger cap hid none
    resolved = {n for n, flags in threads.items() if flags and all(flags)
                and str(n) in cov and not cov[str(n)].get("omitted")
                and not conv.get("threads_omitted")}
    base = _slice_repo_base(slice_obj)
    own = base[len("https://github.com/"):].strip("/").lower() if base else None

    def qualified(slug, number):
        """A reference to `slug`'s item `number`: `#N` when `slug` is the
        slice's own repository (compared case-insensitively), else an item
        the slice never covers."""
        if own and slug.lower() == own:
            return " #{} ".format(number)
        return " " + _FOREIGN_ITEM + " "

    def as_numbers(m):
        """A link to an issue / PR reads as qualified() says; any other
        URL as blank."""
        hit = _FOREIGN_ITEM_URL_RE.match(m.group(0))
        return qualified(hit.group(1), hit.group(2)) if hit else " "
    sentences = [_FOREIGN_SHORT_REF_RE.sub(
        lambda m: qualified(m.group(1), m.group(2)),
        _URL_IN_TEXT_RE.sub(as_numbers, x)).strip()
        for x in re.split(r"(?<=[.;!?])\s+|\n+", text or "") if x.strip()]

    def covered(sent, have, scope):
        """Is every item the sentence speaks of fully covered: each item it
        names (any #N / link, own or linked) in `have`, or — naming none —
        every PR of `scope`
        (a claim naming one uncovered item, or a general claim under
        partial coverage, may be true and is not refused)?"""
        # every reference counts: a linked item outside the outcome has no
        # coverage in the slice, so a claim naming it is never refused
        named = [int(n) for n in re.findall(r"(?<![\w/.&])#(\d+)\b", sent)]
        if _FOREIGN_ITEM in sent:
            return False        # another repository's item: never covered
        return bool(named or scope) and all(n in have for n in named or scope)
    out = {"checks_said_unknown": [], "resolved_said_unknown": []}
    for sent in sentences:
        if not _UNKNOWN_RE.search(sent):
            continue
        # a claim about earlier commits needs every commit covered; one
        # about the checks (the head) needs the head's result
        have = all_commits if (_EARLIER_COMMITS_RE.search(sent)
                               or _REPLACED_RE.search(sent)) else known
        if _CHECKS_WORD_RE.search(sent) and covered(sent, have, prs):
            out["checks_said_unknown"].append(sent)
        if _RESOLVED_WORD_RE.search(sent) and covered(sent, resolved, prs):
            out["resolved_said_unknown"].append(sent)
    return out


_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_CODE_COMMENT_START = ("//", "/*", "#", "<#")


def check_story_code_comments(slice_obj, text):
    """Code comments a story quotes in backticks (a span starting with //,
    /*, # or <#) that occur in no slice text (whitespace-normalised): the
    maintainers' own words must be quoted as the slice holds them. Pure."""
    urls, texts = set(), []
    _slice_strings(slice_obj, urls, texts)
    norm = [_norm_ws(t) for t in texts]
    out = []
    for m in _CODE_SPAN_RE.finditer(text or ""):
        span = m.group(1).strip()
        if re.fullmatch(r"#\d+", span):
            continue                     # an issue / PR reference, not a comment
        if span.startswith(_CODE_COMMENT_START) and len(span) > 3 \
                and not any(_norm_ws(span) in t for t in norm):
            out.append(span)
    return out


def check_story_evidence(slice_obj, text):
    """The mechanical check of a narrative against its train slice: every URL
    the text cites must occur in the slice (as a value or inside a slice
    text), and every quoted string (in "..." or “...”, at least 4
    characters) must occur verbatim in some slice text, whitespace-
    normalised; plus the structure rules of check_story_structure (a year
    in the first date, a closing "Where it stands" / "What landed" part
    that gives the state of every issue / PR the story names). Returns
    {"ok", "urls_not_in_slice", "quotes_not_in_slice", "urls_checked",
    "quotes_checked", "first_date_without_year", "closing_paragraph",
    "closing_paragraph_missing", "numbers_without_state",
    "state_mismatch", "numbers_checked", "checks_said_unknown",
    "resolved_said_unknown", "code_comments_not_in_slice"}
    (check_story_record_claims: the story must not call unknown what the
    slice holds; check_story_code_comments: a quoted code comment must be
    in the slice). Pure."""
    urls, texts = set(), []
    _slice_strings(slice_obj, urls, texts)
    norm_texts = [_norm_ws(t) for t in texts]
    cited = []
    for u in _URL_IN_TEXT_RE.findall(text or ""):
        u = u.rstrip(".,;:")
        if u not in cited:
            cited.append(u)
    bad_urls = [u for u in cited if u not in urls]
    quotes = []
    for m in _QUOTE_RE.finditer(text or ""):
        q = m.group(1) or m.group(2)
        if q.startswith(("http://", "https://")):
            continue
        quotes.append(q)
    bad_quotes = [q for q in quotes
                  if not any(_norm_ws(q) in t for t in norm_texts)]
    out = {"ok": not bad_urls and not bad_quotes,
           "urls_not_in_slice": bad_urls, "quotes_not_in_slice": bad_quotes,
           "urls_checked": len(cited), "quotes_checked": len(quotes)}
    st = check_story_structure(slice_obj, text)
    out.update(st)
    claims = check_story_record_claims(slice_obj, text)
    claims["code_comments_not_in_slice"] = check_story_code_comments(slice_obj, text)
    out.update(claims)
    out["ok"] = out["ok"] and not (st["first_date_without_year"]
                                   or st["closing_paragraph_missing"]
                                   or st["numbers_without_state"]
                                   or st["state_mismatch"]
                                   or any(claims.values()))
    return out


# Item fields that hold a list or an object. A JSON null in one of them (a
# hand-edited view, a REST object with `"labels": null`, an older store) would
# raise TypeError deep in enrich; `_null_collections_to_empty` reads it as empty.
_NULLABLE_LISTS = {
    "prs": ("labels", "reviewers", "closes", "crossref_issues", "comments_list",
            "review_comments", "reviews", "lifecycle", "timeline_xrefs"),
    "issues": ("labels", "assignees", "comments_list", "lifecycle", "blocks",
               "blocking", "blocked_by"),
    "commits": ("files", "parents"),
}
_NULLABLE_DICTS = ("facets", "reactions")


def _null_collections_to_empty(bundle):
    """Make enrich total over JSON nulls (issue #82). In place: on each PR /
    issue / commit, a list field that is null becomes [], an object field
    (`facets`, `reactions`) that is null becomes {}, and a null reaction count
    becomes 0. Only keys that are present AND null change, so a well-formed
    bundle is untouched (no key is added). A null top-level `prs` / `issues` /
    `commits` becomes []."""
    for coll, fields in _NULLABLE_LISTS.items():
        if coll in bundle and bundle[coll] is None:
            bundle[coll] = []
        for item in bundle.get(coll, []):
            for key in fields:
                if key in item and item[key] is None:
                    item[key] = []
            if coll == "commits":
                continue
            for key in _NULLABLE_DICTS:
                if key in item and item[key] is None:
                    item[key] = {}
            reactions = item.get("reactions")
            if isinstance(reactions, dict):
                for key, count in reactions.items():
                    if count is None:
                        reactions[key] = 0
    return bundle


_SUPERSEDED_RE = re.compile(
    r"\b(superseded|replaced|consolidated|continued|covered|included|merged|moved|"
    r"folded|redone)\s+(?:by|in|into|with|as|via|under)\s+"
    r"(?:(?:pull request|PR)\s+)?(?:#|https://github\.com/[\w.-]+/[\w.-]+/pull/)"
    r"(\d+)", re.I)


def close_reason(bundle, pr, instant):
    """Why a PR closed without merging, only when the record says it: an
    explicit "superseded / replaced / consolidated ... by #N" in its own
    comments, reviews or closing event by the instant -> {superseded_by: N,
    named_in, by, quote}; else {close_reason: "not stated"}."""
    texts = []
    for key, ts in (("comments_list", "created_at"), ("reviews", "submitted_at"),
                    ("review_comments", "created_at")):
        for c in pr.get(key) or []:
            if isinstance(c, dict) and c.get("body") and _not_after(c.get(ts), instant):
                texts.append((c.get(ts) or "", c["body"], c.get("url"), c.get("author")))
    for at, body, url, who in sorted(texts, key=lambda x: x[0], reverse=True):
        m = _SUPERSEDED_RE.search(_HTML_COMMENT_RE.sub("", body))
        if m and int(m.group(2)) != pr.get("number"):
            return {"superseded_by": int(m.group(2)), "named_in": url, "by": who,
                    "quote": m.group(0)}
    return {"close_reason": "not stated"}


def annotate_close_reasons(bundle):
    """`closed_unmerged` (close_reason) on every PR of a story-layer view
    closed without merging by the view's end. In place."""
    if "pr_history" not in bundle:
        return bundle
    instant = window_end_instant(bundle.get("meta"))
    for p in bundle.get("prs") or []:
        if isinstance(p, dict) and p.get("state") == "closed" and not p.get("merged") \
                and p.get("closed_at") and _not_after(p["closed_at"], instant):
            p["closed_unmerged"] = close_reason(bundle, p, instant)
    return bundle


_ISSUE_NUMBER_RE = re.compile(r"(?<![\w/&])#(\d+)\b")


def annotate_text_as_of(bundle):
    """As-of guard on texts the view holds as last gathered (a view whose
    records are projected to its end, `meta.records_as_of`). Issue / PR
    numbers are handed out in order, so a number above every record created
    by the instant names an item that did not exist then: a body naming
    one was edited after the window. Per PR: `closes` / `crossref_issues`
    references to such numbers move to `refs_after_window` (no train joins
    through them); a PR or issue whose body names one, or whose description
    `pr_activity` saw edited after the instant (`body_edited_at`), gains
    `body_as_of` {state: "as last gathered", why}. In place."""
    meta = bundle.get("meta") or {}
    if not meta.get("records_as_of"):
        return bundle
    instant = window_end_instant(meta)
    known = [r.get("number") for key in ("prs", "issues") for r in bundle.get(key) or []
             if isinstance(r, dict) and isinstance(r.get("number"), int)
             and _not_after(r.get("created_at"), instant)]
    if not known:
        return bundle
    bound = max(known)
    activity = _pr_activity_index(bundle) or {}
    for key in ("prs", "issues"):
        for r in bundle.get(key) or []:
            if not isinstance(r, dict):
                continue
            later = sorted({int(n) for n in _ISSUE_NUMBER_RE.findall(r.get("body") or "")
                            if int(n) > bound})
            why = []
            if later:
                why.append("it names #{}, created after the window's end".format(
                    ", #".join(str(n) for n in later)))
            edited = (activity.get(r.get("number")) or {}).get("body_edited_at") \
                if key == "prs" else None
            if edited and not _not_after(edited, instant):
                why.append("edited {} (after the window's end)".format(edited))
            if why:
                r["body_as_of"] = {"state": "as last gathered", "why": "; ".join(why)}
            if key != "prs":
                continue
            moved = []
            for f in ("closes", "crossref_issues"):
                refs = r.get(f)
                if isinstance(refs, list):
                    keep = [n for n in refs if not (isinstance(n, int) and n > bound)]
                    moved += [n for n in refs if isinstance(n, int) and n > bound]
                    r[f] = keep
            if moved:
                r["refs_after_window"] = sorted(set(moved))
    return bundle


def annotate_labels_as_of(bundle):
    """A PR's `labels` as of the view's end, from its label history
    (the record's `label_events`, labeled / unlabeled with their time, kept
    since gather reads them): a label added after the instant is taken off,
    one removed after
    it is put back; `labels_as_of` {state: "from label history" | "as last
    gathered", [changed_after]} says which. PRs without label events keep
    their labels, marked as last gathered. Story-layer views only. In
    place."""
    if "pr_history" not in bundle:
        return bundle
    instant = window_end_instant(bundle.get("meta"))
    for p in bundle.get("prs") or []:
        if not isinstance(p, dict):
            continue
        raw = p.get("label_events")
        evs = [e for e in (raw if isinstance(raw, list) else [])
               if isinstance(e, dict) and e.get("event") in ("labeled", "unlabeled")
               and isinstance(e.get("label"), str) and e["label"]
               and isinstance(e.get("at"), str)]
        if not evs:
            p["labels_as_of"] = {"state": "as last gathered"}
            continue
        labels = list(p.get("labels") or [])
        later = []
        for e in sorted(evs, key=lambda e: e.get("at") or "", reverse=True):
            if _not_after(e.get("at"), instant):
                continue
            later.append(e["label"])
            if e["event"] == "labeled" and e["label"] in labels:
                labels.remove(e["label"])
            elif e["event"] == "unlabeled" and e["label"] not in labels:
                labels.append(e["label"])
        p["labels"] = labels
        p["labels_as_of"] = {"state": "from label history"}
        if later:
            p["labels_as_of"]["changed_after"] = sorted(set(later))
    return bundle


def annotate_pr_forks(bundle):
    """`is_fork` on every PR row that carries `head_repo` (gather records
    the head branch's repository): true when it is another repository than
    the view's, or null — GitHub nulls a deleted head repository, and only
    a fork's can be deleted while the PR exists. Rows without `head_repo`
    (older stores, fixtures) keep no `is_fork`; on a story-layer view
    every PR row gains `fork_status` (fork_status: three-valued, `unknown`
    when the record does not say). In place."""
    slug = (repo_slug(bundle.get("meta")) or "").lower()
    story_layer = "pr_history" in bundle
    for p in bundle.get("prs") or []:
        if isinstance(p, dict) and "head_repo" in p:
            head = p.get("head_repo")
            p["is_fork"] = head is None or head.lower() != slug
        if isinstance(p, dict) and story_layer:
            p["fork_status"] = fork_status(p, slug)
    return bundle


FORK_STATUSES = ("fork", "deleted_fork", "not_fork", "unknown")


def fork_status(p, slug):
    """Where a PR's head branch lives, never read from a missing fact:
    `fork` (another repository), `deleted_fork` (GitHub nulls a deleted
    fork's repository), `not_fork` (this repository), `unknown` (the
    record has no `head_repo`: gathered before it was kept)."""
    if "head_repo" not in p:
        return "unknown"
    head = p.get("head_repo")
    if head is None:
        return "deleted_fork"
    return "not_fork" if head.lower() == (slug or "").lower() else "fork"


def enrich(bundle, priority_patterns=(), path_rules=(), people_source=None):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets,
    the window narrative projections (timeline/feature_deltas) and Phase 3b
    code-area attribution (code_area/area/modules). Label facets and `kind` are
    stamped in gather.py's acquire(), not here.

    Slice 7b-2: `artifacts` and `people` are NO LONGER derived here. fold_bundle
    materializes them into the store and extract reads them back, so enrich now
    CONSUMES them (build_timeline / attribute_code_areas read `artifacts`). The
    setdefaults below keep enrich total for callers that hand it a bundle without
    those keys (e.g. a raw fixture fed directly, not through extract): enrich
    never KeyErrors and never recomputes them. Null list/object fields on
    PRs/issues/commits are read as empty first (`_null_collections_to_empty`).
    `priority_patterns` are the operator's extra priority-label patterns
    (`--priority-label`), added to PRIORITY_LABEL_PATTERNS. `path_rules` are
    the operator's `--path-class CLASS=GLOB` rules for `module_changes`,
    which is built only when the bundle carries `module_notes`."""
    priority_patterns = tuple(priority_patterns or ())
    matchers = priority_matchers(priority_patterns)
    _null_collections_to_empty(bundle)
    bundle.setdefault("artifacts", {})
    bundle.setdefault("people", {})
    attach_commit_prs(bundle["commits"])
    annotate_text_as_of(bundle)
    annotate_labels_as_of(bundle)
    annotate_pr_forks(bundle)
    if "pr_history" in bundle:
        # automation accounts for what they are (story layer views only):
        # the registry's operator tags win, then the stored account type;
        # then the one review state every reader uses (approval lists too)
        annotate_automation(bundle, registry_automation(people_source))
        annotate_review_state(bundle)
        annotate_close_reasons(bundle)
        inst = window_end_instant(bundle.get("meta"))
        for p in bundle.get("prs") or []:
            if isinstance(p, dict) and p.get("number") is not None:
                new = proposed_modules(bundle, p, inst)
                if new:
                    p["proposes_new_module"] = new
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle, matchers)
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
    build_forecast(bundle, matchers, priority_patterns)
    build_modules(bundle, idx)
    # Phase 10 slice 1: review-round / reopen texture, derived from the
    # reviews/lifecycle that gather set (or extract resurfaces). Read-side derived
    # facts live here with forecast/modules — NOT in extract (which stays raw).
    annotate_review_rounds(bundle)
    annotate_reopen_count(bundle)
    # Phase 11 slice 1: people profile + recognition halls (read-side projections
    # over the materialized bundle; need people/prs/commits/artifacts/trains above).
    derive.annotate_people_profile(bundle)
    # affiliation labels from the gathered `affiliations` fact (only when the
    # view carries it), before the halls rank people within each label
    derive.annotate_people_affiliation(bundle)
    derive.build_halls(bundle)
    # Phase 11 slice 2: flow pathology classifier + blockers in-degree (read-side
    # projections over issues/prs; placed after halls).
    derive.build_flow(bundle)
    derive.build_blockers(bundle)
    # The reader's module view (new / changed / breaking / deprecated, product
    # vs plumbing): only for a bundle that carries the gathered module_notes,
    # so every other bundle is unchanged. Reads trains / symbol_moves above.
    build_module_changes(bundle, path_rules)
    annotate_release_contents(bundle)
    # The story layer: the discussion signal per train, then the story
    # selection (reads module_changes for product relevance / plumbing).
    # Omit-when-absent keys: a train with no human conversation, and a view
    # where no train qualifies, are unchanged.
    annotate_last_human_event(bundle)
    annotate_train_modules(bundle)
    if "pr_history" in bundle:
        annotate_module_owners(bundle)
    annotate_train_discussion(bundle)
    select_story_trains(bundle)
    # Every story of the window, grouped (issue #97: "all the stories for
    # that month ... landed, in flight, stalled, plumbing"), and the
    # plumbing themes; omit-when-absent.
    build_story_list(bundle)
    build_plumbing_themes(bundle)
    annotate_issue_closing(bundle)
    annotate_module_ownership(bundle)
    annotate_module_lifecycle(bundle)
    # The people layer: affiliation shares, community counts, newcomers /
    # returning contributors and the hall with what each person's work was
    # about (module layer). Only when the view carries the gathered
    # `affiliations` or `newcomers` facts (omit-when-absent).
    build_contributors(bundle, path_rules)
    # Registry context (link --people / `people_source`): what the people
    # registry knows of the people the report names, beyond the window, read
    # as of the view's end and stamped; nothing without a source.
    build_people_context(bundle, people_source)
    return bundle


# --- The people layer: `contributors` ---------------------------------------
#
# Facts, not prose: who contributed (by affiliation), who is new or back after
# a gap, and what each of them landed, read off the module layer
# (`module_changes`: module, kind, status, PR class, changelog items). Built
# from the view's records and gathered facts only; no call is made.

CONTRIBUTION_PRS_MAX = 10           # PRs listed per person
CONTRIBUTION_MODULES_MAX = 5        # modules listed per PR
CONTRIBUTION_ITEMS_MAX = 5          # changelog items listed per module entry
_HEADLINE_BY_CLASS = {"ci": "plumbing", "docs-only": "plumbing",
                      "tests-only": "plumbing", "release": "release",
                      "unknown": "unknown"}


def _module_layer(bundle, path_rules):
    """(module_changes, source): the view's own `module_changes`, or — for a
    view without the gathered `module_notes` — the same build over the
    view's commits and file events alone (source `code_events_only`: status
    `new` only from an added entry file, no changelog, no tags). The
    fallback is built on a shallow copy: the view is not changed."""
    mc = bundle.get("module_changes")
    if isinstance(mc, dict):
        return mc, "module_changes"
    shadow = dict(bundle)
    shadow["module_notes"] = {"collected": True, "changelogs": [],
                              "versions": [], "tags": {"collected": False}}
    build_module_changes(shadow, path_rules)
    return shadow.get("module_changes") or {}, "code_events_only"


def _contribution_index(mc):
    """({pr number: [module rows]}, {pr number: [changelog rows]}) of a
    module_changes value: per PR, each module it touched with the module's
    kind and status and whether this PR introduced it, and the changelog
    entries the PR's commits added."""
    by_pr, logs = {}, {}
    for m in mc.get("modules") or []:
        introduced = {s.get("pr") for s in m.get("introduced_by") or []}
        for p in m.get("prs") or []:
            n = p.get("number")
            row = {"area": m.get("area"), "kind": m.get("kind"),
                   "status": m.get("status")}
            if m.get("status") == "new" and n in introduced:
                row["role"] = "introduced"
                if m.get("first_version"):
                    row["first_version"] = m["first_version"]
            else:
                row["role"] = "changed"
            by_pr.setdefault(n, []).append(row)
        for entry in m.get("changelog") or []:
            items = [item.get("text") for s in entry.get("sections") or []
                     for item in s.get("items") or [] if isinstance(item, dict)]
            for n in sorted({s.get("pr") for s in entry.get("sources") or []
                             if s.get("pr") is not None}):
                row = {"module": entry.get("module") or m.get("area"),
                       "version": entry.get("version"),
                       "items": items[:CONTRIBUTION_ITEMS_MAX]}
                if len(items) > CONTRIBUTION_ITEMS_MAX:
                    row["items_omitted"] = len(items) - CONTRIBUTION_ITEMS_MAX
                logs.setdefault(n, []).append(row)
    return by_pr, logs


def _pr_contribution(pr, row, modules, changelog):
    """One PR as a contribution fact: {number, url, title, state, class?,
    headline, modules?, modules_omitted?, sweep?, changelog?}. `headline`:
    `new_module` (the PR introduced a module that is new in the window),
    `module_change`, `plumbing` (CI / docs / tests only: `class` says
    which), `release` (a version cut), `unknown` (no commit of it in the
    view, e.g. an open PR)."""
    out = {"number": pr.get("number"), "url": pr.get("url"),
           "title": pr.get("title"),
           "state": "merged" if pr.get("merged") else (pr.get("state") or None)}
    cls = (row or {}).get("class")
    if cls:
        out["class"] = cls
    mods = list(modules or [])
    if row and row.get("sweep"):
        keep = set(row.get("substantive_areas") or [])
        mods = [m for m in mods if m["area"] in keep]
        out["sweep"] = row["sweep"]
    mods.sort(key=lambda m: (m["role"] != "introduced", m["area"] or ""))
    if any(m["role"] == "introduced" for m in mods):
        headline = "new_module"
    elif cls in _HEADLINE_BY_CLASS:
        headline = _HEADLINE_BY_CLASS[cls]
    elif cls in ("module", "mixed"):
        headline = "module_change"
    else:
        headline = "unknown"
    out["headline"] = headline
    if mods:
        out["modules"] = mods[:CONTRIBUTION_MODULES_MAX]
        if len(mods) > CONTRIBUTION_MODULES_MAX:
            out["modules_omitted"] = len(mods) - CONTRIBUTION_MODULES_MAX
    if changelog:
        out["changelog"] = changelog
    return out


def person_contributions(login, bundle, rows, by_pr, logs):
    """What `login`'s work in the view was about: their PRs merged in the
    window (module layer facts), then their open PRs, newest number first
    within each, at most CONTRIBUTION_PRS_MAX (`prs_omitted` the rest).
    Pure."""
    merged, open_ = [], []
    for pr in bundle.get("prs") or []:
        if pr.get("author") != login:
            continue
        n = pr.get("number")
        if n in rows:
            merged.append(_pr_contribution(pr, rows[n], by_pr.get(n), logs.get(n)))
        elif pr.get("state") == "open" and not pr.get("merged"):
            open_.append(_pr_contribution(pr, None, None, None))
    items = sorted(merged, key=lambda c: -(c["number"] or 0)) + \
        sorted(open_, key=lambda c: -(c["number"] or 0))
    out = {"prs": items[:CONTRIBUTION_PRS_MAX]}
    if len(items) > CONTRIBUTION_PRS_MAX:
        out["prs_omitted"] = len(items) - CONTRIBUTION_PRS_MAX
    return out


def person_reviews(login, bundle, rows):
    """{prs, modules}: how many of the view's PRs `login` reviewed and the
    module areas of those that merged in the window (module layer). Pure."""
    count, areas = 0, set()
    for pr in bundle.get("prs") or []:
        if login in (pr.get("reviewers") or []):
            count += 1
            row = rows.get(pr.get("number")) or {}
            areas.update(row.get("substantive_areas") if row.get("sweep")
                         else row.get("areas") or [])
    return {"prs": count, "modules": sorted(areas)}


def _share(part, total):
    return round(part / total, 3) if total else None


def _affiliation_summary(bundle, rows, people):
    """Counts and shares by affiliation of the view's people, of its PRs
    merged in the window (by author) and of its reviews (reviewer-PR pairs),
    with `unknown` separate; bots are left out."""
    fact = bundle["affiliations"]
    labels = ("maintainer_org", "community", "unknown")
    aff = {login: p.get("affiliation") for login, p in people.items()
           if not p.get("is_bot") and p.get("affiliation")}
    counts = {k: 0 for k in labels}
    signals = {}
    for login, a in aff.items():
        counts[a] = counts.get(a, 0) + 1
        sig = people[login].get("affiliation_signal")
        signals[sig] = signals.get(sig, 0) + 1

    def split(logins):
        tally = {k: 0 for k in labels}
        excluded = 0
        for login in logins:
            if login in aff:
                tally[aff[login]] += 1
            else:
                excluded += 1        # a bot, or no person record
        total = sum(tally.values())
        return dict({k: {"count": tally[k], "share": _share(tally[k], total)}
                     for k in labels}, total=total, bots_excluded=excluded)
    merged_authors = [pr.get("author") for pr in bundle.get("prs") or []
                      if pr.get("number") in rows]
    reviewers = [rv for pr in bundle.get("prs") or []
                 for rv in sorted(set(pr.get("reviewers") or []))]
    out = {"home_orgs": list(fact.get("home_orgs") or []),
           "people": counts,
           "signals": {k: signals[k] for k in sorted(signals, key=str)},
           "merged_prs": split(merged_authors),
           "reviews": split(reviewers)}
    maint = _maintainer_summary(people)
    if maint is not None:
        out["maintainers"] = maint
    if fact.get("windows"):
        out["windows"] = fact["windows"]
        out["covers_view"] = fact.get("covers_view")
    return out


def _maintainer_summary(people):
    """Maintainers vs non-maintainers of the view's repository (the people
    registry's `maintainer` property), with `unknown` separate (no
    maintainer property: not checked, do_not_name, or not a person of a
    gathered window), by signal and crossed with the affiliation: a
    community maintainer is possible, and neither label implies the other.
    None when no person carries the property (bots left out)."""
    humans = {login: p for login, p in people.items() if not p.get("is_bot")}
    if not any("maintainer" in p for p in humans.values()):
        return None
    keys = ("maintainer", "non_maintainer", "unknown")

    def bucket(p):
        if "maintainer" not in p:
            return "unknown"
        return "maintainer" if p["maintainer"] else "non_maintainer"
    counts = {k: 0 for k in keys}
    signals, cross, logins, statuses = {}, {}, [], {}
    for login in sorted(humans, key=str):
        p = humans[login]
        b = bucket(p)
        counts[b] += 1
        if b == "maintainer":
            logins.append(login)
            sig = p.get("maintainer_signal")
            signals[sig] = signals.get(sig, 0) + 1
        if p.get("maintainer_status"):
            statuses.setdefault(p["maintainer_status"], []).append(login)
        aff = p.get("affiliation") or "unknown"
        row = cross.setdefault(aff, {k: 0 for k in keys})
        row[b] += 1
    out = {"people": counts,
           "signals": {k: signals[k] for k in sorted(signals, key=str)},
           "by_affiliation": {k: cross[k] for k in sorted(cross)},
           "logins": logins}
    if statuses:
        # the lifecycle at gather time: a `former` maintainer is counted as a
        # non-maintainer above and listed here
        out["by_status"] = {k: statuses[k] for k in people_mod.MAINTAINER_STATUSES
                            if k in statuses}
    return out


def _train_logins(bundle, train):
    """The logins of one train: authors, mergers, reviewers, commenters of
    its issues and PRs, its commit authors (None removed)."""
    pr_index = {p["number"]: p for p in bundle.get("prs", [])}
    issue_index = {i["number"]: i for i in bundle.get("issues", [])}
    issues, prs = _train_members(train, pr_index, issue_index)
    logins = set()
    for rec in issues + prs:
        logins.add(rec.get("author"))
        logins.add(rec.get("merged_by"))
        logins.update(rec.get("reviewers") or [])
        for key in ("comments_list", "review_comments", "reviews"):
            for c in rec.get(key) or []:
                if isinstance(c, dict):
                    logins.add(c.get("author"))
    commits = {c.get("sha"): c for c in bundle.get("commits", [])}
    for sha in train.get("commits") or []:
        logins.add((commits.get(sha) or {}).get("author"))
    logins.discard(None)
    return logins


def slice_participants(bundle, train):
    """{"participants": {login: {name?, maintainer?, maintainer_signal?,
    maintainer_status?, affiliation?, registry_context?}}} for the humans of
    one train (authors, mergers, reviewers, commenters of its issues and
    PRs, its commit authors), so a narrator can name them; {} when the
    view's people carry no display name or maintainer property and the view
    has no `people_context` (omit-when-absent). `registry_context` is the
    person's entry of `people_context` (link --people: registry context, not
    a fact of the window). The login stays the key; a do_not_name person has
    no name, no label and no registry context. Pure."""
    people = bundle.get("people") or {}
    context = (bundle.get("people_context") or {}).get("people") or {}
    if not any(k in p for p in people.values()
               for k in tuple(derive.PERSON_LABEL_KEYS) + ("module_owner_of", "automation")) \
            and not context:
        return {}
    logins = _train_logins(bundle, train)
    out = {}
    for login in sorted(k for k in logins if k and k in people):
        p = people[login]
        if p.get("is_bot"):
            continue
        row = derive.person_labels(p)
        if p.get("affiliation") and p.get("affiliation_signal") != "do_not_name":
            row["affiliation"] = p["affiliation"]
        if login in context:
            row["registry_context"] = context[login]
        if p.get("module_owner_of"):
            row["module_owner_of"] = p["module_owner_of"]
        if p.get("automation"):
            row["automation"] = p["automation"]
        out[login] = row
    return {"participants": out}


# --- registry context (link --people) ----------------------------------------
#
# What the people registry knows of the people a report names, beyond the
# window: "also maintains ...", "active in this repo since <date, as
# observed>", "N merged PRs here in the last 6 months", the maintainer status.
# Registry context, never a fact of the window: it is read only with an
# explicit `--people PATH`, only from the registry's windows that END BY THE
# VIEW'S END (an old report keeps the context it had, whatever was gathered
# since), and stamped with the registry files' sha256 so a reader can tell
# which registry state produced it. Without `--people` nothing is added.

def _named_people(bundle):
    """The logins a report names: newcomers and hall entries
    (`contributors`), story participants (trains with `story`), and the
    authors of the PRs that introduced a new module (`module_changes`)."""
    c = bundle.get("contributors") or {}
    names = {r.get("login") for r in c.get("newcomers") or []}
    names |= {r.get("login") for r in c.get("hall") or []}
    names |= {h.get("login") for h in (bundle.get("halls") or {}).get("fame") or []}
    for t in bundle.get("trains") or []:
        if t.get("story"):
            names |= _train_logins(bundle, t)
    authors = {p.get("number"): p.get("author") for p in bundle.get("prs") or []}
    for m in (bundle.get("module_changes") or {}).get("modules") or []:
        for s in m.get("introduced_by") or []:
            names.add(authors.get(s.get("pr")))
    people = bundle.get("people") or {}
    return sorted(k for k in names if k and not (people.get(k) or {}).get("is_bot")
                  and (people.get(k) or {}).get("affiliation_signal") != "do_not_name")


def build_people_context(bundle, source):
    """bundle["people_context"] from the people registry `source`
    (people.load_view_source), for the people the report names
    (_named_people): {"kind": "registry_context", "note", "repo", "as_of"
    (the view's end: only windows ending by then are read), "registry"
    (people.registry_stamp), "thresholds", "coverage" (this repository's,
    as of), "people": {login: people.writer_context}}. A do_not_name person
    and a person the registry does not know are left out. Nothing is added
    without a source. Pure over the bundle (in place)."""
    if not source:
        return bundle
    meta = bundle.get("meta") or {}
    repo = meta.get("repo") or ""
    slug = repo if "/" in repo else "{}/{}".format(meta.get("owner"), repo)
    as_of = str(meta.get("to") or "")[:10] or None
    reg, act, limits = source["registry"], source["activity"], source["limits"]
    out = {}
    for login in _named_people(bundle):
        ctx = people_mod.writer_context(reg, act, login, slug, as_of, limits)
        if ctx:
            out[login] = ctx
    cov = people_mod.coverage(act, slug, as_of)
    bundle["people_context"] = {
        "kind": "registry_context",
        "note": "from the people registry, not a fact of this window: windows "
                "gathered up to {}; every first and total is as observed "
                "since the coverage start".format(as_of),
        "repo": slug, "as_of": as_of,
        "registry": people_mod.registry_stamp(source["people_text"],
                                              source["activity_text"], act, as_of),
        "thresholds": dict(limits),
        "coverage": {k: v for k, v in (cov or {}).items() if k != "intervals"},
        "people": out}
    return bundle


def _community_summary(bundle, rows, people, newcomer_rows):
    """Community-specific counts: people, PRs merged in the window, reviews,
    first-timers and returning contributors labelled `community`, and the
    module areas that received a community PR merged in the window. People
    whose affiliation is `unknown` are never counted here."""
    community = {login for login, p in people.items()
                 if not p.get("is_bot") and p.get("affiliation") == "community"}
    author = {p.get("number"): p.get("author") for p in bundle.get("prs") or []}
    merged = sorted(n for n in rows if author.get(n) in community)
    areas = set()
    for n in merged:
        r = rows[n]
        areas.update(r.get("substantive_areas") if r.get("sweep")
                     else r.get("areas") or [])
    reviews = sum(1 for pr in bundle.get("prs") or []
                  for rv in set(pr.get("reviewers") or []) if rv in community)
    return {"people": len(community), "prs_merged": len(merged),
            "prs_merged_numbers": merged, "reviews": reviews,
            "first_timers": sorted(r["login"] for r in newcomer_rows
                                   if r["class"] == "first_ever"
                                   and r["login"] in community),
            "returning": sorted(r["login"] for r in newcomer_rows
                                if r["class"] == "returning"
                                and r["login"] in community),
            "modules": sorted(areas)}


def build_contributors(bundle, path_rules=()):
    """bundle["contributors"] (BUNDLE.md "The people layer"), when the view
    carries a collected `affiliations` or `newcomers` fact; otherwise
    nothing is added. Keys (each only when its input exists):
    `affiliation` (_affiliation_summary), `community`
    (_community_summary), `newcomers` [{login, class: first_ever |
    returning, first, gap_days?, gap_bucket?, last_seen_before?,
    seen_in_store_before?, affiliation?, affiliation_signal?,
    contributed}], `hall` [{login, score, affiliation?, contributed,
    reviewed}] for each `halls.fame` entry, and `inputs` {module_layer:
    module_changes | code_events_only}. Pure over the bundle (in place)."""
    aff_fact = bundle.get("affiliations")
    nc = bundle.get("newcomers")
    has_aff = isinstance(aff_fact, dict) and bool(aff_fact.get("collected"))
    has_nc = isinstance(nc, dict) and bool(nc.get("collected"))
    if not has_aff and not has_nc:
        return bundle
    people = bundle.get("people") or {}
    mc, source = _module_layer(bundle, path_rules)
    rows = {r.get("number"): r for r in mc.get("prs") or []}
    by_pr, logs = _contribution_index(mc)
    prior = (bundle.get("prior_activity") or {}).get("people") or {}

    def labels(login, row):
        p = people.get(login) or {}
        if "affiliation" in p:
            row["affiliation"] = p["affiliation"]
            row["affiliation_signal"] = p.get("affiliation_signal")
        row.update(derive.person_labels(p))
        return row
    newcomer_rows = []
    if has_nc:
        entries = [("first_ever", e) for e in nc.get("new") or []] + \
            [("returning", e) for e in nc.get("returning") or []]
        for cls, e in sorted(entries, key=lambda t: str(t[1].get("login"))):
            login = e.get("login")
            row = {"login": login, "class": cls,
                   "first": {k: e.get(k) for k in ("first", "number", "url",
                                                   "created_at")}}
            for k in ("last_seen_before", "gap_days", "gap_bucket",
                      "gap_upper_bound"):
                if k in e:
                    row[k] = e[k]
            if login in prior:
                row["seen_in_store_before"] = prior[login]
            labels(login, row)
            row["contributed"] = person_contributions(login, bundle, rows,
                                                      by_pr, logs)
            newcomer_rows.append(row)
    out = {}
    if has_aff:
        out["affiliation"] = _affiliation_summary(bundle, rows, people)
        out["community"] = _community_summary(bundle, rows, people,
                                              newcomer_rows)
    if has_nc:
        out["newcomers"] = newcomer_rows
        if "returning" in nc:
            out["gap_thresholds_days"] = nc.get("gap_thresholds_days")
        if "prior_activity" in bundle:
            out["store_from"] = bundle["prior_activity"].get("store_from")
    hall = []
    for h in (bundle.get("halls") or {}).get("fame") or []:
        login = h.get("login")
        row = labels(login, {"login": login, "score": h.get("score")})
        row["contributed"] = person_contributions(login, bundle, rows, by_pr,
                                                  logs)
        row["reviewed"] = person_reviews(login, bundle, rows)
        hall.append(row)
    out["hall"] = hall
    out["inputs"] = {"module_layer": source}
    bundle["contributors"] = out
    return bundle


def _installment_problem(installments):
    """Why the reference lists series.compute_series reads from an installment
    (`shipped`, `in_flight`, `next_candidates`, `rejected`, `forecast`) or its
    window cannot be used, or None. Each list must be a list of objects when
    present; `from` / `to` must be strings when present (they order the index)."""
    for n, inst in enumerate(installments):
        for key in ("shipped", "in_flight", "next_candidates", "rejected",
                    "forecast"):
            if key not in inst:
                continue
            refs = inst[key]
            if not isinstance(refs, list) \
                    or not all(isinstance(r, dict) for r in refs):
                return ("installment {} has a `{}` that is not a list of "
                        "reference objects".format(n, key))
        for key in ("from", "to"):
            if inst.get(key) is not None and not isinstance(inst[key], str):
                return ("installment {} has a `{}` that is not a date "
                        "string".format(n, key))
        window = series.installment_window(inst)
        if window is not None:
            # installments are keyed and ordered by their calendar days
            # (#110 item 6), so each bound must be a date or ISO timestamp
            bad = series.window_problem(window, "installment {}".format(n))
            if bad is not None:
                return "has an unusable window: {}".format(bad)
    return None


def _load_series_index(series_path):
    """Read the series index and return its list of installment objects. The
    file is `{"version": 2, "installments": [...]}` (#86) or the older bare
    JSON list of installments, which is still read (and rewritten as version 2
    by the next `--series` run). Absent, empty or whitespace-only (a freshly
    `touch`ed or truncated file) is the first installment ([]). Anything else
    (invalid JSON, another object or version, a list holding a non-object, a
    malformed installment) exits 2 with a message naming the file, rather than
    a traceback (#82); a corrupt-but-present index is never silently
    discarded."""
    try:
        with open(series_path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return []
    if not raw.strip():
        return []
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        problem = "is not valid JSON ({})".format(exc)
    else:
        installments, problem = doc, None
        if isinstance(doc, dict):
            if "version" not in doc:
                problem = "is a JSON object without a `version`, not a series index"
            elif doc["version"] != series.INDEX_VERSION:
                problem = ("has version {!r}; this link.py reads version {} (or "
                           "the older bare list)".format(doc["version"],
                                                         series.INDEX_VERSION))
            else:
                installments = doc.get("installments")
                if not isinstance(installments, list):
                    problem = "has no `installments` list"
        elif not isinstance(doc, list):
            problem = "is a JSON {}, not a list of installments".format(
                type(doc).__name__)
        if problem is None:
            if not all(isinstance(i, dict) for i in installments):
                problem = "holds an entry that is not an installment object"
            else:
                problem = _installment_problem(installments)
                if problem is None:
                    return installments
    _series_refusal(series_path, problem)


def _series_refusal(series_path, problem):
    sys.stderr.write("link.py --series: series index {} {}; fix or remove it\n"
                     .format(series_path, problem))
    raise SystemExit(2)


def _series_bundle_problem(meta, installments):
    """Why this bundle cannot be recorded in this index, or None (#86): it
    needs a window (installments are identified by it) whose bounds are dates
    or ISO timestamps, an index whose entries name a different owner/repo is
    another repo's series, and a recorded window that overlaps this one
    without being the same calendar days (a quarter beside its months, #110
    items 6 and 10) leaves "the installment before this one" undefined."""
    frm, to = series.bundle_window(meta)
    if frm is None or to is None:
        return ("cannot record this bundle: it has no window (meta.period or "
                "meta.from/meta.to), and installments are identified by it")
    if not (isinstance(frm, str) and isinstance(to, str)):
        return ("cannot record this bundle: its window {!r} to {!r} is not "
                "two date strings".format(frm, to))
    bad = series.window_problem((frm, to), "bundle window")
    if bad is not None:
        return "cannot record this bundle: {}".format(bad)
    mine = (meta.get("owner"), meta.get("repo"))
    for inst in installments:
        theirs = (inst.get("owner"), inst.get("repo"))
        if None not in theirs and None not in mine and theirs != mine:
            return ("belongs to {}/{}, not {}/{} (keep one series file per "
                    "repo)".format(theirs[0], theirs[1], mine[0], mine[1]))
    clash = series.overlapping(installments, (frm, to))
    if clash:
        return ("cannot record the window {} to {}: it overlaps the recorded "
                "window(s) {}, and one index holds windows that do not overlap "
                "(keep a separate series file for a roll-up such as a "
                "quarter)".format(
                    frm, to, ", ".join("{} to {}".format(i["from"], i["to"])
                                       for i in clash)))
    return None


def _write_json_atomic(path, obj):
    """Write `obj` as indented JSON to `path` atomically: into `<path>.tmp` (same
    directory, as view.write_view and render do), then os.replace over `path`. An
    interrupted write leaves the previous file intact and removes the temp file
    (#82). The bytes are those of a plain json.dump(obj, fh, indent=2)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_bundle(path):
    """The bundle object at `path`. A missing or unreadable file, invalid JSON
    or a JSON value that is not an object is a usage-style error (#110 item 8):
    a message on stderr, exit 2, nothing written."""
    problem = None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        problem = "cannot read {} ({})".format(path, exc.strerror or exc)
    else:
        try:
            bundle = json.loads(raw)
        except ValueError as exc:
            problem = "{} is not valid JSON ({})".format(path, exc)
        else:
            if isinstance(bundle, dict):
                return bundle
            problem = "{} is a JSON {}, not a JSON object (a bundle)".format(
                path, "null" if bundle is None else type(bundle).__name__)
    sys.stderr.write("link.py: error: {}\n".format(problem))
    sys.stderr.write(_USAGE)
    raise SystemExit(2)


_USAGE = ("usage: link.py BUNDLE.json [--slice TRAIN_ID | --series series.json] "
          "[--priority-label PATTERN ...] [--path-class CLASS=GLOB ...] "
          "[--people people.json]\n"
          "       link.py SLICE.json --check-story STORY.md\n")
_HELP = _USAGE + """
Enrich a bundle/view with trains and buckets, rewriting BUNDLE.json in place.

  --slice TRAIN_ID     print one train's self-contained slice as JSON on
                       stdout; BUNDLE.json is not rewritten
  --check-story PATH   with a slice file (what --slice printed) instead of a
                       bundle: check the narrative at PATH against it — every
                       URL it cites and every quoted string must be in the
                       slice; prints the result as JSON, exit 0 when it
                       passes, 1 when it does not
  --series PATH        also compare with, and record this window in, the
                       series index at PATH
  --priority-label PATTERN
                       also treat a label matching this regular expression
                       (case-insensitive search) as high priority, besides
                       the default patterns; repeatable
  --path-class CLASS=GLOB
                       classify files matching GLOB as CLASS (source, tests,
                       docs, generated, metadata, ci) in module_changes,
                       before the default rules; repeatable
  --people PATH        add `people_context`: registry context (not a fact of
                       the window) for the people the report names, read from
                       the people registry at PATH and its activity file,
                       only from windows ending by the view's end, stamped
                       with the files' sha256; story slices carry it per
                       participant
  -h, --help           show this help and exit
"""


def _split_people(argv):
    """(argv without `--people PATH`, PATH or None); None for the whole
    result when `--people` is repeated or has no value."""
    rest, path = [], None
    i = 0
    while i < len(argv):
        if argv[i] == "--people":
            if path is not None or i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                return None
            path = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    return rest, path


def _parse_argv(argv):
    """(path, slice_id, series_path, priority_patterns, path_rules) of the
    documented shapes: BUNDLE first, then at most one of `--slice ID` /
    `--series PATH` and any number of `--priority-label PATTERN` and
    `--path-class CLASS=GLOB`, each flag followed by its value. Anything else
    (an unknown flag, a missing value, a value or BUNDLE that looks like a
    flag, `--slice` with `--series`, a pattern that is not a valid regular
    expression, a path rule that is not CLASS=GLOB with a known class) is a
    usage error: exit 2."""
    if not argv or argv[0].startswith("-"):
        return None
    path, rest = argv[0], argv[1:]
    slice_id = series_path = None
    patterns, path_rules = [], []
    if len(rest) % 2:
        return None
    for flag, value in zip(rest[0::2], rest[1::2]):
        if value.startswith("-"):
            return None
        if flag == "--slice" and slice_id is None and series_path is None:
            slice_id = value
        elif flag == "--series" and series_path is None and slice_id is None:
            series_path = value
        elif flag == "--priority-label":
            try:
                re.compile(value)
            except re.error:
                return None
            patterns.append(value)
        elif flag == "--path-class":
            try:
                path_class_rules([value])
            except ValueError:
                return None
            path_rules.append(value)
        else:
            return None
    return path, slice_id, series_path, patterns, path_rules


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Accept ONLY the documented shapes (_parse_argv): BUNDLE first, then
    # flag/value pairs. `--slice` and `--series` are mutually exclusive.
    # `-h` / `--help` anywhere prints help (exit 0), as argparse does; a BUNDLE
    # or value that looks like a flag is an unknown flag (exit 2, usage).
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(_HELP)
        raise SystemExit(0)
    if len(argv) == 3 and argv[1] == "--check-story" \
            and not argv[0].startswith("-") and not argv[2].startswith("-"):
        sliced = _load_bundle(argv[0])
        try:
            with open(argv[2], encoding="utf-8") as fh:
                story = fh.read()
        except OSError as exc:
            sys.stderr.write("link.py: error: cannot read {} ({})\n".format(
                argv[2], exc.strerror or exc))
            raise SystemExit(2)
        result = check_story_evidence(sliced, story)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        raise SystemExit(0 if result["ok"] else 1)
    split = _split_people(list(argv))
    parsed = _parse_argv(split[0]) if split else None
    if parsed is None:
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    path, slice_id, series_path, priority_patterns, path_rules = parsed
    people_source = None
    if split[1] is not None:
        try:
            people_source = people_mod.load_view_source(split[1])
        except (OSError, ValueError) as exc:
            sys.stderr.write("link.py: error: people registry {}: {}\n".format(
                split[1], exc))
            raise SystemExit(2)
    bundle = _load_bundle(path)
    # Load (and shape-check) the series index BEFORE any work, so a malformed
    # index stops the run with rc 2 and nothing is written.
    installments = None
    if series_path is not None:
        installments = _load_series_index(series_path)
        problem = _series_bundle_problem(bundle.get("meta", {}), installments)
        if problem is not None:
            _series_refusal(series_path, problem)
    enrich(bundle, priority_patterns, path_rules, people_source=people_source)
    if series_path is not None:
        # Phase 13: series continuity, idempotent by window (#86). Compare
        # against the installment immediately BEFORE this window and record
        # this installment under its window, replacing an earlier run of the
        # same window. Without `--series` no `series` key is added, so the
        # standard digest stays byte-identical. The index is written AFTER the
        # bundle (below): a run that cannot write its bundle records no
        # installment (#82).
        window = series.bundle_window(bundle.get("meta", {}))
        later = series.later_installments(installments, window)
        bundle["series"] = series.compute_series(
            bundle, series.predecessor(installments, window))
        installments = series.place_installment(
            installments, series.installment_snapshot(
                bundle, captured="backfill" if later else "in_order"))
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
    _write_json_atomic(path, bundle)
    if series_path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(series_path)), exist_ok=True)
        _write_json_atomic(series_path, series.index_document(installments))
        if later:
            sys.stderr.write(
                "link.py --series: recorded {} to {} before later installment(s) "
                "{} (captured: backfill); their series blocks do not reflect "
                "this record: re-run link.py --series on their bundles, oldest "
                "first, to refresh them\n"
                .format(window[0], window[1],
                        ", ".join("{} to {}".format(i["from"], i["to"])
                                  for i in later)))
    sys.stderr.write(
        f"linked {len(bundle['trains'])} trains, "
        f"{len(bundle['buckets']['shipped'])} shipped into {path}\n"
    )
    return path


if __name__ == "__main__":
    main()
