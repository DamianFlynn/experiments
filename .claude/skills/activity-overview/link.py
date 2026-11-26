"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import os
import re
import sys
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
    approved here: `review_decision` is today's, not the window's."""
    if pr.get("state") != "open" or pr.get("merged") or pr.get("draft") is True:
        return False
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
        if row["change"] == "removed":
            candidates.append({"rule": "child_module_removed", "subject": path,
                               "sources": row["sources"]})
        elif row["change"] == "moved":
            candidates.append({"rule": "child_module_moved",
                               "subject": "{} -> {}".format(row.get("from"), path),
                               "sources": row["sources"]})
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
        if not_published:
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
STORY_ITEM_CAP = 5000
# Per-slice budget for the timeline's text. The most discussed train of the
# two months (train-pr-6549: 88 human comments and review comments plus a
# 4,131-character body, 19,126 characters) fits whole, as does the largest
# issue train of October (about 23,000 once its 20,794-character body is
# capped). About 10,000 tokens of text.
STORY_SLICE_BUDGET = 40000
# When the budget is exceeded, items are cut evenly from the longest down, never
# below this many characters; past that, whole texts are left out from the
# middle of the timeline outward (the opening and the latest items stay).
STORY_ITEM_FLOOR = 500
# Budget for the code notes (comments / parameter descriptions / interface
# lines) carried on the timeline's commit items.
STORY_CODE_NOTES_BUDGET = 12000
# Relevance points by train kind (story_score).
_STORY_KIND_RELEVANCE = {"feature": 3.0, "module-request": 3.0, "bug": 1.0}

# What GitHub records but gather does not keep, named in every story slice so
# a narrator says it rather than infers it.
STORY_NOT_GATHERED = (
    "title and description edits (titles and bodies are as last gathered)",
    "force-pushes and the branch states they replaced (only each PR's current "
    "head is fetched)",
    "label, milestone and assignee changes over time",
)

_TIMELINE_KIND_ORDER = {"opened": 0, "commit": 1, "comment": 2,
                        "review_comment": 3, "review": 4, "ready_for_review": 5,
                        "reopened": 6, "closed": 7, "merged": 8}


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


def _train_plumbing(train, module_changes):
    """True when a train's work is plumbing: with the module layer, every
    merged PR of the train it classified is `plumbing` (or a sweep); without
    it, a chore / docs / other train touching more than 10 code areas (a
    mechanical sweep)."""
    if module_changes:
        rows = {r.get("number"): r for r in module_changes.get("prs") or []}
        mine = [rows[n] for n in train.get("prs", []) if n in rows]
        if mine:
            return all(r.get("plumbing") or r.get("sweep") for r in mine)
    return (len(train.get("code_areas") or []) > 10
            and train.get("kind") in ("chore", "docs", "other", None))


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
        ok = {c["sha"] for c in _pr_commits_as_of(row, instant)}
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
        for key, kind in (("comments_list", "comment"),
                          ("review_comments", "review_comment")):
            for c in rec.get(key) or []:
                if not isinstance(c, dict):
                    continue
                if derive.is_bot_login(c.get("author")):
                    bot(c.get("author"))
                    continue
                items.append({"at": c.get("created_at"), "kind": kind,
                              "actor": c.get("author"), "url": c.get("url"),
                              "on": on, "text": c.get("body") or ""})
        for r in rec.get("reviews") or []:
            if not isinstance(r, dict) or r.get("state") == "pending":
                continue
            if derive.is_bot_login(r.get("author")):
                bot(r.get("author"))
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
            items.append({"at": e.get("created_at"), "kind": e.get("event"),
                          "actor": e.get("actor"), "url": _event_url(rec, e),
                          "on": on})
        if rec.get("merged") and rec.get("merged_at"):
            items.append({"at": rec["merged_at"], "kind": "merged",
                          "actor": rec.get("merged_by"), "url": rec.get("url"),
                          "on": on})
        elif rec.get("closed_at") and not closed_seen \
                and (rec.get("state") == "closed"):
            items.append({"at": rec["closed_at"], "kind": "closed",
                          "actor": None, "url": rec.get("url"), "on": on})
    notes_by_commit, by_module, notes_info = train_code_notes(
        bundle, train, history, instant)
    seen = set()
    for p in prs:
        row = history.get(p.get("number"))
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
            if notes_by_commit.get(c["sha"]):
                it["code_notes"] = notes_by_commit[c["sha"]]
            items.append(it)
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
        if notes_by_commit.get(sha):
            it["code_notes"] = notes_by_commit[sha]
        items.append(it)
    items = [it for it in items if it.get("at")]
    items.sort(key=lambda it: (_parse_ts(it["at"]) or datetime.min.replace(
        tzinfo=timezone.utc), _TIMELINE_KIND_ORDER.get(it["kind"], 9),
        it.get("url") or ""))
    truncation = _apply_story_budget(items)
    truncation["code_notes"] = notes_info
    out = {"timeline": items,
           "turning_points": story_turning_points(
               items, instant if train.get("outcome") == "in_flight" else None),
           "truncation": truncation,
           "bots": dict(sorted(bots.items())),
           "code_notes_by_module": by_module,
           "pr_history": _pr_history_status(prs, history),
           "as_of": instant,
           "not_gathered": list(STORY_NOT_GATHERED)}
    return out


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


def check_story_evidence(slice_obj, text):
    """The mechanical check of a narrative against its train slice: every URL
    the text cites must occur in the slice (as a value or inside a slice
    text), and every quoted string (in "..." or “...”, at least 4
    characters) must occur verbatim in some slice text, whitespace-
    normalised. Returns {"ok", "urls_not_in_slice", "quotes_not_in_slice",
    "urls_checked", "quotes_checked"}. Pure."""
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
    return {"ok": not bad_urls and not bad_quotes,
            "urls_not_in_slice": bad_urls, "quotes_not_in_slice": bad_quotes,
            "urls_checked": len(cited), "quotes_checked": len(quotes)}


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


def enrich(bundle, priority_patterns=(), path_rules=()):
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
    derive.build_halls(bundle)
    # Phase 11 slice 2: flow pathology classifier + blockers in-degree (read-side
    # projections over issues/prs; placed after halls).
    derive.build_flow(bundle)
    derive.build_blockers(bundle)
    # The reader's module view (new / changed / breaking / deprecated, product
    # vs plumbing): only for a bundle that carries the gathered module_notes,
    # so every other bundle is unchanged. Reads trains / symbol_moves above.
    build_module_changes(bundle, path_rules)
    # The story layer: the discussion signal per train, then the story
    # selection (reads module_changes for product relevance / plumbing).
    # Omit-when-absent keys: a train with no human conversation, and a view
    # where no train qualifies, are unchanged.
    annotate_train_discussion(bundle)
    select_story_trains(bundle)
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
          "[--priority-label PATTERN ...] [--path-class CLASS=GLOB ...]\n"
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
  -h, --help           show this help and exit
"""


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
    parsed = _parse_argv(list(argv))
    if parsed is None:
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    path, slice_id, series_path, priority_patterns, path_rules = parsed
    bundle = _load_bundle(path)
    # Load (and shape-check) the series index BEFORE any work, so a malformed
    # index stops the run with rc 2 and nothing is written.
    installments = None
    if series_path is not None:
        installments = _load_series_index(series_path)
        problem = _series_bundle_problem(bundle.get("meta", {}), installments)
        if problem is not None:
            _series_refusal(series_path, problem)
    enrich(bundle, priority_patterns, path_rules)
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
