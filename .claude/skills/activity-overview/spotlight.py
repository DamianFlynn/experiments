"""spotlight — the substrate analytics reader (Phase 8).

`extract` answers "what happened in this window?"; `spotlight` answers
cross-cutting questions orthogonal to any one window — "what has this person
done across all repos?" and (in later slices) symbol lifecycle, subsystem
blast radius, and full-text mining. Bounded SQL over the journey-graph store,
deterministically ordered, every row carrying its source citation.

Reader-only by default: imports `graphstore` (+ `derive` where shared shaping
helps) and, offline, neither writes the store nor touches the network. The one
exception is opt-in completion (Phase 8d): when a `backfill` seam is injected
(CLI `--complete`, which wires `gather.make_backfill_fetcher` from a token), the
honest-edge step may fetch and upsert missing cross-window anchors *through
gather* — gather stays the only writer/network. Primary output is raw, cited
JSON for the AI that builds reports; `--md` is a thin deterministic render over
the same JSON. See docs/.../activity-phase8-spotlight.md.

Stdlib only (sqlite3, argparse, json). Python 3.
"""

import argparse
import json
import os
import sys

import graphstore
import complete
import derive
import gather


def _detect_project(conn, project):
    """Derive the project from the store when not passed (mirrors validate):
    the single non-sentinel project in `nodes`. Raises on an empty store or an
    ambiguous multi-project store so the caller disambiguates with --project."""
    if project is not None:
        return project
    projects = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL")
    })
    if len(projects) == 1:
        return projects[0]
    if not projects:
        raise ValueError("empty store: no project to query")
    raise ValueError(
        "store holds multiple projects {} — pass --project".format(projects))


def _project_repos(conn, project):
    """The non-sentinel repos for a project (people aggregate across them)."""
    return graphstore.project_repos(conn, project)


def _like_prefix(s):
    """Escape a LIKE prefix (mirrors graphstore.repo_code_events)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Contribution edge types, in the deterministic group order person-impact emits.
_CONTRIB_TYPES = ("authored", "reviewed", "merged", "reported", "commented")

# Defensive cap on the identity-chain walk (renames/moves should be shallow).
_CHAIN_DEPTH_CAP = 64

# Bounded excerpt for a comment body carried on a touchpoint / timeline row.
_EXCERPT_CAP = 200

# slice_module bounds — a module biography is a CONTEXT UNIT, not an archive.
# Per-text cap for a lifecycle row's before/after/diff; per-artifact lifecycle
# row cap; total artifact cap. Overflow beyond each is reported as a *_overflow
# count rather than silently dropped.
_SLICE_TEXT_CAP = 800
_MODULE_LIFECYCLE_CAP = 40
_MODULE_ARTIFACT_CAP = 60

# Causal spine edges for spotlight's decision-train grouping: graphstore's
# SPINE_EDGE_TYPES minus `cross_ref`. A cross-reference is a casual mention
# ("related to #123"), not causality, so following it over-groups unrelated
# trains and mis-headlines them (e.g. a 2026 PR filed under a 2022 issue it
# merely mentioned). Spotlight groups by causal links only; the report's
# traversal (graphstore default) is intentionally left unchanged.
_CAUSAL_SPINE = tuple(t for t in graphstore.SPINE_EDGE_TYPES if t != "cross_ref")


def _cite(node_id, data):
    """A citation row for a contribution target: its id plus whichever of
    number / sha / url / title the node data carries (a PR/issue has
    number+url+title; a commit has sha). Always JSON-serializable."""
    row = {"id": node_id}
    if data.get("number") is not None:
        row["number"] = data["number"]
    if data.get("sha") is not None:
        row["sha"] = data["sha"]
    if data.get("url") is not None:
        row["url"] = data["url"]
    if data.get("title") is not None:
        row["title"] = data["title"]
    return row


# --------------------------------------------------------------------------
# Shared output helpers — the one chronological delivery-train contract that
# every query routes through (no per-query divergence).
# --------------------------------------------------------------------------

def _scope(ts_from, ts_to):
    """Echo the query's time scope: {"from","to"} when bounded, else the
    string "all-history". Carried on every Result."""
    if ts_from is None and ts_to is None:
        return "all-history"
    return {"from": ts_from, "to": ts_to}


def _node_kind(node_id, data):
    """Derive a node's kind for a timeline row from its id / data: one of
    pr / issue / commit / release / comment / symbol / file, else the node's
    structural prefix. Deterministic."""
    local = graphstore.parse_id(node_id)["local"]
    # Phase 10: review submissions / lifecycle events are social leaves on the
    # PR/issue spine. Classify by their own prefix BEFORE the /pull/ url heuristic
    # below — their url carries the parent PR's /pull/ path and would otherwise be
    # misread as a PR (phantom PR rows / double counting in the timeline).
    if local.startswith("review-"):
        return "review"
    if local.startswith("event-"):
        return "event"
    # A backfilled `Closes #N` whose #N turned out to be a PR is stored under the
    # referenced `issue-N` id but carries the PR's /pull/ url — label it a PR.
    if local.startswith("pr-") or "/pull/" in ((data or {}).get("url") or ""):
        return "pr"
    if local.startswith("issue-"):
        return "issue"
    if local.startswith("release-"):
        return "release"
    if local.startswith("comment-"):
        return "comment"
    if local.startswith("area-"):
        return "area"
    if local.startswith("milestone-"):
        return "milestone"
    if local.startswith("person-"):
        return "person"
    if "#" in local:
        return "symbol"
    if local.startswith("art:"):
        return "file"
    if (data or {}).get("sha") is not None:
        return "commit"
    return "node"


def _excerpt(text):
    """Bounded excerpt of a body for a comment touchpoint/row."""
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text[:_EXCERPT_CAP]


def _timeline_row(node):
    """A cited chronological row for a spine node: {id, kind, ts, number?|sha?,
    url?, title?, excerpt?} (reusing _cite for the citation fields). A comment
    node carries a bounded excerpt of its body."""
    nid = node["id"]
    data = node.get("data") or {}
    row = _cite(nid, data)
    row["kind"] = _node_kind(nid, data)
    row["ts"] = node.get("ts") or ""
    if row["kind"] == "comment":
        row["excerpt"] = _excerpt(data.get("body"))
    return row


def _outcome(reached_nodes):
    """Deterministic train outcome over its reached spine nodes:
      shipped   — a merged PR or a release node is present;
      rejected  — the train's anchor/PR is closed-but-unmerged (no merge);
      in_flight — otherwise (still open / unresolved).
    `reached_nodes` is an iterable of node dicts."""
    nodes = [n for n in reached_nodes if n is not None]
    has_closed_unmerged = False
    for n in nodes:
        kind = _node_kind(n["id"], n.get("data") or {})
        data = n.get("data") or {}
        if kind == "release":
            return "shipped"
        if kind == "pr":
            if data.get("merged"):
                return "shipped"
            if data.get("state") == "closed":
                has_closed_unmerged = True
    if has_closed_unmerged:
        return "rejected"
    return "in_flight"


# Origin kind priority for choosing a train's anchor: the originating
# issue/PR headlines a train, never a commit sha. Lower wins.
_ORIGIN_PRIORITY = {"issue": 0, "pr": 1, "release": 2, "commit": 3}


def _origin_anchor(nodes):
    """Choose a train's anchor as its ORIGIN: the reached node with the best
    (kind_priority, ts, id) where issue < pr < release < commit < other. This
    keeps the headline on the originating issue/PR (its title) rather than a
    commit message, and is fully deterministic — the same reached set always
    yields the same origin however the train was entered. `nodes` is the list
    of hydrated node dicts (None-filtered)."""
    def key(n):
        kind = _node_kind(n["id"], n.get("data") or {})
        prio = _ORIGIN_PRIORITY.get(kind, 4)
        return (prio, n.get("ts") or "", n["id"])
    return min(nodes, key=key)["id"]


def _reached_anchor(conn, reached):
    """The origin anchor for a reached id set (hydrates the ids and applies
    `_origin_anchor`), so the dedup/seed key a query groups trains by is the
    SAME origin `_train` will headline — issue/PR over a commit sha — and two
    seeds entering one train collapse to one entry. Falls back to min(id) when
    nothing hydrates (e.g. a lone toucher with no node yet)."""
    nodes = [graphstore.get_node(conn, nid) for nid in reached]
    nodes = [n for n in nodes if n is not None]
    if not nodes:
        return min(reached)
    return _origin_anchor(nodes)


def _train(conn, anchor, reached, focus_touch_ids, role_of,
           *, window=None, backfill=None, complete_budget=50):
    """Build one decision-train entry routed through the shared contract.

    `anchor`           — the train's deterministic anchor id; superseded here by
                         the train's ORIGIN (see `_origin_anchor`) when the
                         reached set is non-empty, so the headline lands on the
                         originating issue/PR rather than a commit sha.
    `reached`          — iterable of node ids in the train.
    `focus_touch_ids`  — the subset of `reached` the focus touched.
    `role_of`          — maps a touched id -> the focus's role there
                         (author/reviewer/commenter/reporter/owns/touches),
                         OR id -> a list of {role, excerpt?} for multi-role /
                         comment touchpoints.

    Returns {anchor, key_date, title, outcome, areas[], roles[],
             touchpoints[ cited focus events; comment carries excerpt ],
             timeline[ _timeline_row…, chronological by (ts,id) ]}.
    Determinism: timeline + touchpoints by (ts, id).

    Completion runs FIRST (offline-by-default): the body — anchor, timeline,
    touchpoints — is built from the POST-completion reached set, so `--complete`
    actually surfaces fetched cross-window anchors instead of leaving the same
    holes. `missing` is self-derived from `reached`; skip_dead prunes phantoms."""
    reach = graphstore.traverse_spine(
        conn, sorted(reached), edge_types=_CAUSAL_SPINE, skip_dead=True)
    comp = complete.complete_train(
        conn, reach["reached"], reach["missing"], window=window,
        backfill=backfill, budget=complete_budget, edge_types=_CAUSAL_SPINE)
    reached = comp["reached"]  # present, post-completion train nodes

    nodes = [graphstore.get_node(conn, nid) for nid in sorted(reached)]
    nodes = [n for n in nodes if n is not None]

    # the anchor is the train's ORIGIN (issue beats pr beats release beats
    # commit), so the headline reads as the originating issue/PR title — not a
    # commit message. Anchor it to the train's IN-WINDOW work so pulled-in
    # out-of-window context (e.g. an ancient cross-referenced PR) is traversed
    # but cannot hijack the headline. Falls back to all nodes (then the passed
    # anchor) if nothing hydrated / nothing is in-window.
    if nodes:
        in_window_nodes = [n for n in nodes if _ts_in_window(n.get("ts"), window)]
        anchor = _origin_anchor(in_window_nodes or nodes)
    anchor_node = graphstore.get_node(conn, anchor)
    adata = (anchor_node["data"] if anchor_node else {}) or {}

    # full cited spine timeline, chronological
    timeline = [_timeline_row(n) for n in nodes]
    timeline.sort(key=lambda r: (r["ts"], r["id"]))

    # areas the train touches (area nodes reached, or area edges off members)
    areas = set()
    for n in nodes:
        for e in graphstore.get_edges(conn, n["id"], direction="out",
                                      edge_types=["touches"]):
            if "#area-" in e["dst_id"]:
                local = graphstore.parse_id(e["dst_id"])["local"]
                areas.add(local[len("area-"):])

    # the focus's own touchpoints inside the train, each carrying role(s)
    by_id = {n["id"]: n for n in nodes}
    touchpoints = []
    roles = set()
    for tid in focus_touch_ids:
        if tid not in by_id and tid != anchor:
            # the touched node may not itself be on the spine (e.g. a commit);
            # still cite it from its node.
            tnode = graphstore.get_node(conn, tid)
        else:
            tnode = by_id.get(tid) or anchor_node
        tdata = (tnode["data"] if tnode else {}) or {}
        spec = role_of.get(tid)
        # normalize role spec -> list of (role, excerpt)
        if isinstance(spec, list):
            entries = spec
        elif isinstance(spec, dict):
            entries = [spec]
        else:
            entries = [{"role": spec}]
        for entry in entries:
            row = _cite(tid, tdata)
            row["kind"] = _node_kind(tid, tdata)
            row["ts"] = (tnode["ts"] if tnode else None) or ""
            row["role"] = entry.get("role")
            roles.add(entry.get("role"))
            if entry.get("excerpt") is not None:
                row["excerpt"] = entry["excerpt"]
            touchpoints.append(row)
    touchpoints.sort(key=lambda r: (r["ts"], r["id"], r.get("role") or ""))

    # the train's key date = earliest spine ts (story start). Anchor it to the
    # train's IN-WINDOW activity so completion's out-of-window CONTEXT (level-0
    # anchors fetched by --complete) enriches the timeline without shifting the
    # train's scope/sort position out of the query window. timeline is already
    # (ts, id)-sorted, so the first in-window row is the earliest.
    if timeline:
        in_window_ts = [r["ts"] for r in timeline if _ts_in_window(r["ts"], window)]
        key_date = in_window_ts[0] if in_window_ts else timeline[0]["ts"]
    else:
        key_date = (anchor_node["ts"] if anchor_node else None) or ""

    train = {
        "anchor": anchor,
        "key_date": key_date,
        "title": adata.get("title"),
        "outcome": _outcome(nodes),
        "areas": sorted(areas),
        "roles": sorted(r for r in roles if r),
        "touchpoints": touchpoints,
        "timeline": timeline,
    }
    # Stamp the honest edge contract (complete/gaps) from the completion above.
    complete.annotate(train, comp)
    return train


def _result(query, focus, focus_kind, project, scope, summary, delivered,
            **context):
    """The unified envelope. `delivered` is sorted chronologically by the
    train's (key_date, anchor); the per-train `outcome` flags shipped work so
    the narration can emphasize it (order stays chronological, never resorted
    by outcome)."""
    delivered = sorted(delivered, key=lambda t: (t.get("key_date") or "",
                                                  t["anchor"]))
    res = {
        "query": query,
        "focus": focus,
        "focus_kind": focus_kind,
        "project": project,
        "status": "ok",
        "scope": scope,
        "summary": summary,
    }
    res.update(context)
    res["delivered"] = delivered
    return res


# The focus's contribution edge_type -> the role label on a touchpoint.
_ROLE_OF_EDGE = {
    "authored": "author",
    "reviewed": "reviewer",
    "merged": "merger",
    "reported": "reporter",
    "commented": "commenter",
}


def person_impact(conn, project, login, ts_from=None, ts_to=None,
                  *, backfill=None, complete_budget=50):
    """A contributor's impact across all repos in `project` (people are
    project-scoped), as the chronological delivery trains they TOUCHED in any
    role. Returns the unified citation-bearing envelope; a miss (no person
    node) returns a needs_gather guidance result.

    `delivered`: one `_train` per decision train the login touched (seed the
    spine from authored/reviewed/merged/reported AND commented dsts that are
    spine nodes; dedupe to anchors). A train's `touchpoints` are the login's
    contribution events whose dst falls in that train, each with its role and —
    for a comment — the bounded excerpt of the login's own comment body.
    `summary`: per-role counts + trains_touched + shipped (a small header).
    Context blocks: is_bot, modules, areas, symbols_authored,
    authored_then_removed. `--from/--to` filter trains by their key date.
    Determinism: trains by (key_date, anchor); touchpoints/timeline by (ts,id).
    """
    person_id = graphstore.qualify_person(project, login)
    person = graphstore.get_node(conn, person_id)
    if person is None:
        return {
            "query": "person",
            "focus": login,
            "focus_kind": "person",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no person node for {}; gather a window where they "
                "contributed".format(login)),
        }

    pdata = person["data"] or {}

    # --- contribution edges, grouped by type; collect spine seeds ---
    edges = graphstore.get_edges(conn, person_id, direction="out",
                                 edge_types=list(_CONTRIB_TYPES))
    # touch_edges: (edge_type, dst_id) for every contribution; seed_ids feeds the
    # train traversal. Per-role counts are computed later over the trains actually
    # delivered, so a time-scoped query's summary stays consistent with its body.
    touch_edges = []  # (edge_type, dst_id)
    seed_ids = []
    for e in edges:
        etype = e["edge_type"]
        touch_edges.append((etype, e["dst_id"]))
        # Seed train traversal from EVERY touched spine node, not just PR/issue
        # dsts: a commit a contributor authored (e.g. a direct push with no PR)
        # anchors a train of its own that would otherwise be counted in summary
        # yet missing from `delivered`.
        seed_ids.append(e["dst_id"])

    # --- symbols authored (+ authored_then_removed) — context ---
    symbols_authored = []
    removed = []
    for repo in _project_repos(conn, project):
        prefix = "{}/{}#".format(project, repo)
        like = _like_prefix(prefix) + "%"
        rows = conn.execute(
            "SELECT DISTINCT artifact_id FROM code_events "
            "WHERE author=? AND event='add' AND artifact_id LIKE ? ESCAPE '\\'",
            (login, like)).fetchall()
        for (aid,) in rows:
            local = aid[len(prefix):] if aid.startswith(prefix) else aid
            if "#" not in local:
                continue  # file-level artifact, not a symbol
            symbols_authored.append({"id": aid})
            rm = conn.execute(
                "SELECT 1 FROM code_events "
                "WHERE artifact_id=? AND event='remove' LIMIT 1",
                (aid,)).fetchone()
            if rm:
                removed.append({"id": aid})
    symbols_authored.sort(key=lambda r: r["id"])
    removed.sort(key=lambda r: r["id"])

    # --- delivered: one train per anchor the login touched ---
    # First map each seed to its train's reached set + anchor.
    trains_by_anchor = {}  # anchor -> reached set (ids)
    for seed in sorted(set(seed_ids)):
        reach = graphstore.traverse_spine(conn, [seed], edge_types=_CAUSAL_SPINE,
                                          skip_dead=True)
        reached = set(reach["reached"])
        if not reached:
            continue
        anchor = _reached_anchor(conn, reached)
        # union reached sets sharing an anchor (a seed entering the same train)
        trains_by_anchor.setdefault(anchor, set()).update(reached)

    delivered = []
    shipped = 0
    role_counts = {}  # per-role, over the trains actually delivered (scope-aware)
    for anchor, reached in trains_by_anchor.items():
        # which of the login's touched dsts fall in this train?
        focus_touch_ids = []
        role_of = {}
        train_etypes = []  # contribution edge types landing in this train
        for etype, dst_id in touch_edges:
            if dst_id not in reached:
                continue
            train_etypes.append(etype)
            role = _ROLE_OF_EDGE.get(etype, etype)
            entry = {"role": role}
            if etype == "commented":
                entry["excerpt"] = _person_comment_excerpt(conn, dst_id, login)
            role_of.setdefault(dst_id, []).append(entry)
            if dst_id not in focus_touch_ids:
                focus_touch_ids.append(dst_id)
        train = _train(conn, anchor, reached, focus_touch_ids, role_of,
                       window=_completion_window(ts_from, ts_to),
                       backfill=backfill, complete_budget=complete_budget)
        if (ts_from is not None or ts_to is not None) and not _date_in_range(
                train["key_date"], ts_from, ts_to):
            continue
        # count roles only for trains that survive the scope filter, so summary
        # stays consistent with delivered (trains_touched / shipped are scoped too).
        for etype in train_etypes:
            role_counts[etype] = role_counts.get(etype, 0) + 1
        if train["outcome"] == "shipped":
            shipped += 1
        delivered.append(train)

    summary = dict(role_counts)
    summary["trains_touched"] = len(delivered)
    summary["shipped"] = shipped

    return _result(
        "person", login, "person", project, _scope(ts_from, ts_to),
        summary, delivered,
        is_bot=bool(pdata.get("is_bot")),
        modules=pdata.get("modules") or [],
        areas=pdata.get("areas") or [],
        symbols_authored=symbols_authored,
        authored_then_removed=removed,
    )


def _as_iso(bound, end_of_day=False):
    """Normalize a date-only value (`YYYY-MM-DD`) to a full ISO datetime so
    lexicographic comparison stays correct against ISO datetimes. A value that
    already carries a time (`T...`) — or None — is returned unchanged."""
    if bound is None or "T" in bound:
        return bound
    return bound + ("T23:59:59Z" if end_of_day else "T00:00:00Z")


def _completion_window(ts_from, ts_to):
    """The normalized (lo, hi) ISO bounds for completion's window check, or None
    when unbounded. Date-only bounds are widened to start/end-of-day so they
    compare correctly against ISO datetimes (mirrors `_date_in_range`)."""
    if ts_from is None and ts_to is None:
        return None
    return (_as_iso(ts_from), _as_iso(ts_to, end_of_day=True))


def _ts_in_window(ts, window):
    """True if `ts` falls within a normalized (lo, hi) completion window (either
    bound may be None). An empty ts cannot be proven in-window -> False."""
    if window is None:
        return True
    if not ts:
        return False
    lo, hi = window
    if lo is not None and ts < lo:
        return False
    if hi is not None and ts > hi:
        return False
    return True


def _date_in_range(ts, ts_from, ts_to):
    """A train/event date filter. An empty ts is excluded once a bound is set.
    Date-only bounds are normalized to the start/end of day, so an inclusive
    `--to` of `2026-01-12` still matches a same-day ISO datetime like
    `2026-01-12T14:00:00Z` (a raw string `>` would otherwise exclude it)."""
    if not ts:
        return False
    t = _as_iso(ts)
    lo = _as_iso(ts_from)
    hi = _as_iso(ts_to, end_of_day=True)
    if lo is not None and t < lo:
        return False
    if hi is not None and t > hi:
        return False
    return True


def _person_comment_excerpt(conn, dst_id, login):
    """The bounded excerpt of `login`'s own comment body on a PR/issue node
    (comments live embedded in the node's data `comments_list`/
    `review_comments`). The first matching comment by the login wins; empty
    string if none is recoverable."""
    node = graphstore.get_node(conn, dst_id)
    data = (node["data"] if node else {}) or {}
    comments = (data.get("comments_list") or []) + (
        data.get("review_comments") or [])
    for c in comments:
        if c.get("author") == login:
            return _excerpt(c.get("body"))
    return ""


def _normalize_artifact_id(conn, project, artifact_id):
    """Accept an artifact id in qualified (`{project}/{repo}#{local}`) or local
    (`{local}`) form and return the qualified id, or None if it can't be
    resolved. A qualified id is returned as-is. A local id is matched against
    the project's repos: the first repo for which the qualified id has a node OR
    a code_events row wins (deterministic by repo order)."""
    if artifact_id.startswith("{}/".format(project)):
        return artifact_id  # already project-qualified
    for repo in _project_repos(conn, project):
        cand = graphstore.qualify_id(project, repo, artifact_id)
        if graphstore.get_node(conn, cand) is not None:
            return cand
        row = conn.execute(
            "SELECT 1 FROM code_events WHERE artifact_id=? LIMIT 1",
            (cand,)).fetchone()
        if row:
            return cand
    return None


def _ordered_lifecycle(events, ts_from=None, ts_to=None):
    """An artifact's `code_events` rows in (date, commit) order, optionally
    bounded by [ts_from, ts_to]. The single ordering+filter contract both the
    per-artifact symbol query and the area-level module slice route through, so a
    lifecycle reads the same chronological way wherever it is assembled."""
    rows = sorted(events, key=lambda e: ((e["date"] or ""), e["commit_sha"]))
    if ts_from is not None or ts_to is not None:
        rows = [e for e in rows
                if _date_in_range(e["date"] or "", ts_from, ts_to)]
    return rows


def pattern_evolution(conn, project, artifact_id, ts_from=None, ts_to=None):
    """A symbol/file artifact's FULL lifecycle across all history (not one
    window), as a SINGLE chronological delivery train. Returns the unified
    citation-bearing envelope; an unknown id (no node and no code_events)
    yields a needs_gather result.

    `delivered`: one train whose `timeline` is the artifact's `code_events` in
    (date, commit) order, each row cited via the shared `_timeline_row` shape
    ({id=artifact, kind, event, ts=date, sha, url?, before?, after?}); the
    train's `outcome` is "removed" if the last event is a remove, else "alive".
    Context: `identity_chain` — the cross-path history walked over
    replaced_by/identity_from. `--from/--to` bound the lifecycle window.
    Determinism: timeline by (date, commit); chain in link order (depth-capped).
    """
    qid = _normalize_artifact_id(conn, project, artifact_id)
    events = graphstore.get_code_events(conn, qid) if qid else []
    node = graphstore.get_node(conn, qid) if qid else None
    if not events and node is None:
        return {
            "query": "symbol",
            "focus": artifact_id,
            "focus_kind": "symbol",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no node or code_events for {}; gather a window that touches "
                "this artifact".format(artifact_id)),
        }

    # --- timeline: code_events in (date, commit) order, each cited ---
    events = _ordered_lifecycle(events, ts_from, ts_to)
    timeline = []
    for e in events:
        ref = e.get("ref")
        row = {
            "id": qid,
            "kind": _node_kind(qid, {}),
            "event": e["event"],
            "ts": e["date"] or "",
            "sha": e["commit_sha"],
            "author": e["author"],
            "before": e["before"],
            "after": e["after"],
        }
        # cite by the commit ref's url when present.
        if isinstance(ref, dict) and ref.get("url") is not None:
            row["url"] = ref["url"]
        timeline.append(row)

    outcome = "removed" if (timeline and timeline[-1]["event"] == "remove") \
        else "alive"
    anchor_node = node
    title = (anchor_node["data"].get("title") if anchor_node else None) \
        if anchor_node else None
    key_date = timeline[0]["ts"] if timeline else ""
    train = {
        "anchor": qid,
        "key_date": key_date,
        "title": title,
        "outcome": outcome,
        "areas": [],
        "roles": [],
        "touchpoints": [],
        "timeline": timeline,
        # A symbol lifecycle is a code-identity chain, not a causal-spine train,
        # so it has no spine refs to complete — trivially whole.
        "complete": True,
        "gaps": [],
    }

    # --- identity_chain: walk replaced_by forward + identity_from backward ---
    # Each artifact stores both edges (replaced_by src->dst, identity_from
    # dst->src, both carrying move_confidence/move_basis). Walk forward via
    # replaced_by out-edges, backward via identity_from out-edges, to assemble
    # the ordered chain A->B->C and the move link metadata.
    forward = []  # ids after qid, in move order
    backward = []  # ids before qid, in reverse move order
    link_meta = {}  # id -> {confidence, basis} for the move that PRODUCED it

    if qid:
        seen = {qid}
        # forward: qid replaced_by next
        cur = qid
        depth = 0
        while depth < _CHAIN_DEPTH_CAP:
            edges = graphstore.get_edges(conn, cur, direction="out",
                                         edge_types=["replaced_by"])
            if not edges:
                break
            e = edges[0]  # deterministic (get_edges orders by dst_id)
            nxt = e["dst_id"]
            if nxt in seen:
                break
            d = e["data"] or {}
            link_meta[nxt] = {
                "confidence": d.get("move_confidence"),
                "basis": d.get("move_basis"),
            }
            forward.append(nxt)
            seen.add(nxt)
            cur = nxt
            depth += 1
        # backward: qid identity_from prev (i.e. prev replaced_by qid)
        cur = qid
        depth = 0
        while depth < _CHAIN_DEPTH_CAP:
            edges = graphstore.get_edges(conn, cur, direction="out",
                                         edge_types=["identity_from"])
            if not edges:
                break
            e = edges[0]
            prv = e["dst_id"]
            if prv in seen:
                break
            d = e["data"] or {}
            # this is the move that produced `cur` from `prv`
            link_meta[cur] = {
                "confidence": d.get("move_confidence"),
                "basis": d.get("move_basis"),
            }
            backward.append(prv)
            seen.add(prv)
            cur = prv
            depth += 1

    ordered = list(reversed(backward)) + [qid] + forward
    identity_chain = []
    for i, aid in enumerate(ordered):
        c = {"id": aid}
        meta = link_meta.get(aid)
        if meta and (meta.get("confidence") or meta.get("basis")):
            c["confidence"] = meta.get("confidence")
            c["basis"] = meta.get("basis")
        identity_chain.append(c)

    summary = {"events": len(timeline), "outcome": outcome}
    return _result(
        "symbol", qid, "symbol", project, _scope(ts_from, ts_to),
        summary, [train],
        identity_chain=identity_chain,
    )


def subsystem_split(conn, project, area, ts_from=None, ts_to=None,
                    *, backfill=None, complete_budget=50):
    """An area's activity + blast radius as the chronological delivery trains
    that TOUCHED the area (optional time range). Returns the unified
    citation-bearing envelope; an unknown area (no `area-<area>` structure node
    in any project repo) yields a needs_gather result.

    `delivered`: one `_train` per train that touched the area (seed the spine
    from the area's touching commits/PRs); each train's `touchpoints` are the
    area-touching nodes within it. `summary`: trains + shipped + contributors.
    Context blocks:
      contributors  — codeowners (inverse `owns`) plus the authors of commits/
                      PRs that `touches` the area, deduped (`owns` beats
                      `touches`), ordered by login.
      depends_on    — blast radius: `depends_on` edges OUT (what it depends on)
                      and IN (what depends on it), each carrying version/
                      transitive.
    `--from/--to` filter trains by their key date.
    Determinism: trains by (key_date, anchor); contributors by login; deps by id.
    """
    # resolve the area node across the project's repos
    area_qid = None
    area_local = "area-{}".format(area)
    for repo in _project_repos(conn, project):
        cand = graphstore.qualify_id(project, repo, area_local)
        if graphstore.get_node(conn, cand) is not None:
            area_qid = cand
            break
    if area_qid is None:
        return {
            "query": "subsystem",
            "focus": area,
            "focus_kind": "subsystem",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no area node for {}; gather a window that touches it (areas "
                "are sparse in short windows)".format(area)),
        }

    def _author_login(node):
        return (node["data"] or {}).get("author") if node else None

    # --- in-edges to the area: owns (person) + touches (commit/pr) ---
    in_edges = graphstore.get_edges(conn, area_qid, direction="in",
                                    edge_types=["owns", "touches"])
    relations = {}  # login -> relation ('owns' beats 'touches')
    touching_src_ids = []
    for e in in_edges:
        if e["edge_type"] == "owns":
            # src is a person node; recover the login
            person = graphstore.get_node(conn, e["src_id"])
            login = (person["data"] or {}).get("login") if person else None
            if login is None:
                login = graphstore.parse_id(e["src_id"])["local"]
                if login.startswith("person-"):
                    login = login[len("person-"):]
            relations[login] = "owns"
        elif e["edge_type"] == "touches":
            touching_src_ids.append(e["src_id"])

    # touching commits/PRs -> their authors are touches contributors, and the
    # PRs they belong to are the area's attributed items.
    attributed_prs = {}  # pr_id -> pr node
    for src_id in touching_src_ids:
        src = graphstore.get_node(conn, src_id)
        login = _author_login(src)
        if login is not None and login not in relations:
            relations[login] = "touches"
        # if the toucher is itself a PR node, attribute it directly
        if "#pr-" in src_id and src is not None:
            attributed_prs[src_id] = src
        # follow part_of (commit -> pr) to attribute the PR
        for pe in graphstore.get_edges(conn, src_id, direction="out",
                                       edge_types=["part_of"]):
            if "#pr-" in pe["dst_id"]:
                pr = graphstore.get_node(conn, pe["dst_id"])
                if pr is not None:
                    attributed_prs[pe["dst_id"]] = pr

    contributors = [
        {"login": login, "relation": relations[login]}
        for login in sorted(relations)
    ]

    # --- delivered: trains the area touched ---
    # Seed the spine from the area's touching commits/PRs (and the PRs those
    # commits are part_of), dedupe to anchors. Each train's touchpoints are the
    # area-touching nodes that fall in it.
    seed_ids = set(touching_src_ids) | set(attributed_prs)
    trains_by_anchor = {}  # anchor -> reached set (ids)
    for seed in sorted(seed_ids):
        reach = graphstore.traverse_spine(conn, [seed], edge_types=_CAUSAL_SPINE,
                                          skip_dead=True)
        reached = set(reach["reached"])
        if not reached:
            # a lone toucher with no spine still forms a single-node train
            reached = {seed}
        anchor = _reached_anchor(conn, reached)
        trains_by_anchor.setdefault(anchor, set()).update(reached)

    touch_set = set(touching_src_ids)
    delivered = []
    shipped = 0
    for anchor, reached in trains_by_anchor.items():
        focus_touch_ids = sorted(touch_set & reached)
        role_of = {tid: "touches" for tid in focus_touch_ids}
        train = _train(conn, anchor, reached, focus_touch_ids, role_of,
                       window=_completion_window(ts_from, ts_to),
                       backfill=backfill, complete_budget=complete_budget)
        if (ts_from is not None or ts_to is not None) and not _date_in_range(
                train["key_date"], ts_from, ts_to):
            continue
        if train["outcome"] == "shipped":
            shipped += 1
        delivered.append(train)

    # --- depends_on blast radius (out = depends on; in = depended upon by) ---
    def _dep_row(other_id, data):
        d = data or {}
        local = graphstore.parse_id(other_id)["local"]
        if local.startswith("area-"):
            local = local[len("area-"):]
        return {
            "area": local,
            "id": other_id,
            "version": d.get("version"),
            "transitive": d.get("transitive"),
        }

    deps_out, deps_in = [], []
    for e in graphstore.get_edges(conn, area_qid, direction="out",
                                  edge_types=["depends_on"]):
        deps_out.append(_dep_row(e["dst_id"], e["data"]))
    for e in graphstore.get_edges(conn, area_qid, direction="in",
                                  edge_types=["depends_on"]):
        deps_in.append(_dep_row(e["src_id"], e["data"]))
    deps_out.sort(key=lambda d: d["id"])
    deps_in.sort(key=lambda d: d["id"])

    summary = {
        "trains": len(delivered),
        "shipped": shipped,
        "contributors": len(contributors),
    }
    return _result(
        "subsystem", area, "subsystem", project, _scope(ts_from, ts_to),
        summary, delivered,
        contributors=contributors,
        depends_on={"out": deps_out, "in": deps_in},
    )


def _slice_cap_text(s):
    """Bound a lifecycle row's before/after/diff text to _SLICE_TEXT_CAP chars,
    appending an overflow marker when cut (mirrors link._cap_text). Pure."""
    if not s or len(s) <= _SLICE_TEXT_CAP:
        return s
    return s[:_SLICE_TEXT_CAP] + "…[+{} chars]".format(len(s) - _SLICE_TEXT_CAP)


def _commit_pr_map(conn, shas, project, repos):
    """Map each commit sha -> its PR node id (the `part_of` dst that is a PR),
    for cheap event->PR attribution. Built once over the slice's shas; a sha with
    no part_of PR (or no commit node) is simply absent. Deterministic."""
    out = {}
    for sha in sorted({s for s in shas if s}):
        cid = None
        for repo in repos:
            cand = graphstore.qualify_id(project, repo, sha)
            if graphstore.get_node(conn, cand) is not None:
                cid = cand
                break
        if cid is None:
            continue
        for e in graphstore.get_edges(conn, cid, direction="out",
                                      edge_types=["part_of"]):
            if "#pr-" in e["dst_id"]:
                out[sha] = e["dst_id"]
                break
    return out


def slice_module(conn, project, area, ts_from=None, ts_to=None):
    """A store-backed, FULL-HISTORY slice of one module (an `area`): every
    artifact under the area + each artifact's complete lifecycle (CRUD across all
    gathered windows, with the change detail), plus the trains that touched it —
    a bounded "module biography". Reads the store only; never writes/networks.

    Resolution: read the stored `codegraph` singleton (`area_index`/
    `_area_for_path`) and keep `code` artifact nodes whose `path` maps to the
    requested area. An unknown/empty area (no matching codegraph area in any repo)
    yields the shared `needs_gather` envelope.

    For each kept artifact, `get_code_events` gives its full lifecycle; a renamed
    symbol's history is folded into ONE entry via CONNECTED COMPONENTS over in-area
    `replaced_by` edges (robust to diamonds/cycles/out-of-area terminals) so the
    move's events read as one symbol. Each lifecycle row is bounded (text-capped
    before/after/diff, where diff = the row's `hunk`) and attributed to its PR via
    the commit->PR `part_of` map when cheap. Artifacts split into `symbols` (id
    has `#`) and `files` (`art:`/bare path).

    Bounding (a context unit, not an archive): per-artifact lifecycle rows capped
    at _MODULE_LIFECYCLE_CAP (with `lifecycle_overflow`), total artifacts capped
    at _MODULE_ARTIFACT_CAP per group (with `*_overflow`). Deterministic ordering:
    symbols by id, files by path, lifecycle by (date, commit).
    """
    repos = _project_repos(conn, project)

    # --- resolve the area per repo (NEVER merge indexes: a path string can map to
    #     different areas in different repos, so match each artifact against ITS OWN
    #     repo's codegraph) ---
    area_idx_by_repo = {}
    for repo in repos:
        cg = graphstore.get_node(
            conn, graphstore.qualify_id(project, repo, "codegraph"))
        if cg is not None:
            area_idx_by_repo[repo] = derive.area_index(cg["data"] or {})
    has_area = any(a == area for idx in area_idx_by_repo.values()
                   for a in idx.values())
    if not has_area:
        return {
            "query": "module",
            "focus": area,
            "focus_kind": "module",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no codegraph area covering {} in any project repo; gather a "
                "window that touches the module".format(area)),
        }

    # --- collect the area's artifact code nodes (file + symbol/comment) ---
    artifacts = {}  # qid -> node
    for repo in repos:
        idx = area_idx_by_repo.get(repo, {})
        for node in graphstore.repo_nodes(conn, project, repo, node_class="code"):
            data = node["data"] or {}
            # artifact `code` nodes are the non-commit ones: a commit carries a
            # `sha` and a bare `<sha>` id; artifacts use `art:<path>` / `<path>#…`
            # and have no `sha` (mirrors extract._is_commit_node).
            if "sha" in data:
                continue
            path = data.get("path")
            if path is None or derive._area_for_path(path, idx) != area:
                continue
            artifacts[node["id"]] = node

    # --- rename folding via CONNECTED COMPONENTS over in-area replaced_by edges ---
    # A renamed/moved symbol's history spans several artifact ids linked by
    # `replaced_by`. Grouping by connected component (over in-area replaced_by edges,
    # either direction) folds the whole identity into ONE entry — robust to diamonds
    # (two ids replaced by one), cycles, and a chain whose in-area terminal was
    # replaced by an OUT-of-area successor (that successor isn't in `artifacts`, so
    # the in-area members still group together). Each component is one symbol/file.
    adj = {aid: set() for aid in artifacts}
    succ = {aid: set() for aid in artifacts}   # directed in-area replaced_by targets
    for aid in artifacts:
        for e in graphstore.get_edges(conn, aid, direction="out",
                                      edge_types=["replaced_by"]):
            dst = e["dst_id"]
            if dst in adj:                 # only fold links between in-area artifacts
                adj[aid].add(dst)
                adj[dst].add(aid)
                succ[aid].add(dst)
    components, comp_of = [], {}
    for aid in sorted(artifacts):
        if aid in comp_of:
            continue
        comp, stack = [], [aid]
        while stack:
            x = stack.pop()
            if x in comp_of:
                continue
            comp_of[x] = len(components)
            comp.append(x)
            stack.extend(y for y in adj[x] if y not in comp_of)
        components.append(sorted(comp))    # rep = comp[0] (deterministic)

    # --- pre-fetch the commit->PR map over every sha in the slice's events ---
    # The file-level ledger is keyed by the BARE-path qid, but a file artifact
    # NODE is `art:<path>` (gather folds them under different keys). Resolve the
    # ledger key per artifact: a `art:<path>` node's events live under `<path>`.
    def _event_key(aid, node):
        prefix = "{}/{}#".format(node["project"], node["repo"])
        local = aid[len(prefix):] if aid.startswith(prefix) else aid
        if local.startswith("art:"):
            return prefix + local[len("art:"):]
        return aid

    all_shas = set()
    events_by_member = {}
    for aid, node in artifacts.items():
        evs = graphstore.get_code_events(conn, _event_key(aid, node))
        events_by_member[aid] = evs
        all_shas.update(e["commit_sha"] for e in evs)
    pr_of = _commit_pr_map(conn, all_shas, project, repos)

    def _lifecycle(members):
        """The folded, ordered, bounded lifecycle for a component (its members'
        events merged). Returns (rows, overflow, ordered) — `ordered` is the FULL
        windowed event list (pre row-cap) so time_range can be computed from it."""
        merged = []
        for m in members:
            merged.extend(events_by_member.get(m, []))
        ordered = _ordered_lifecycle(merged, ts_from, ts_to)
        rows = []
        for e in ordered[:_MODULE_LIFECYCLE_CAP]:
            row = {
                "event": e["event"],
                "date": e["date"] or "",
                "commit": e["commit_sha"],
                "pr": pr_of.get(e["commit_sha"]),
            }
            if e["before"] is not None:
                row["before"] = _slice_cap_text(e["before"])
            if e["after"] is not None:
                row["after"] = _slice_cap_text(e["after"])
            if e["hunk"] is not None:
                row["diff"] = _slice_cap_text(e["hunk"])
            rows.append(row)
        return rows, max(0, len(ordered) - _MODULE_LIFECYCLE_CAP), ordered

    symbols, files = [], []
    all_dates = []  # ALL windowed event dates (pre row-cap AND pre artifact-cap)
    for comp in components:
        members = comp
        # representative = the chain's CURRENT artifact (a terminal with no in-area
        # replaced_by successor), so a folded rename shows the present name; fall back
        # to the min id for a cycle (every node has a successor). Deterministic.
        comp_set = set(comp)
        terminals = sorted(a for a in comp if not (succ[a] & comp_set))
        rep = terminals[0] if terminals else comp[0]
        node = artifacts[rep]
        data = node["data"] or {}
        lifecycle, overflow, ordered = _lifecycle(members)
        all_dates.extend(e["date"] for e in ordered if e["date"])
        # symbol/comment artifacts vs file artifacts. The id form is the tell:
        # a symbol's local id is `<path>#<lang>:<subkind>:<name>` (a `#` after the
        # repo scope), a file's is `art:<path>` / a bare path. Strip the
        # `{project}/{repo}#` qualifier so the symbol `#` isn't masked by the
        # scope separator (parse_id splits on the LAST `#`).
        prefix = "{}/{}#".format(node["project"], node["repo"])
        art_local = rep[len(prefix):] if rep.startswith(prefix) else rep
        if "#" in art_local:
            entry = {
                "id": rep,
                "kind": data.get("kind"),
                "subkind": data.get("subkind"),
                "name": data.get("name"),
                "status": data.get("status"),
                "lifecycle": lifecycle,
                "lifecycle_overflow": overflow,
            }
            symbols.append(entry)
        else:
            entry = {
                "path": data.get("path"),
                "lifecycle": lifecycle,
                "lifecycle_overflow": overflow,
            }
            files.append(entry)

    symbols.sort(key=lambda s: s["id"])
    files.sort(key=lambda f: f["path"] or "")
    symbols_overflow = max(0, len(symbols) - _MODULE_ARTIFACT_CAP)
    files_overflow = max(0, len(files) - _MODULE_ARTIFACT_CAP)
    symbols = symbols[:_MODULE_ARTIFACT_CAP]
    files = files[:_MODULE_ARTIFACT_CAP]

    # --- time range over ALL windowed lifecycle dates (collected pre row-cap and
    #     pre artifact-cap above), so an overflowing artifact never under-reports the
    #     module's true first/last activity ---
    time_range = {
        "first": min(all_dates) if all_dates else None,
        "last": max(all_dates) if all_dates else None,
    }

    # --- trains that touched the area (reuse subsystem's area resolution) ---
    trains = _module_trains(conn, project, area, repos, ts_from, ts_to)

    summary = {
        "symbols": len(symbols),
        "files": len(files),
        "trains": len(trains),
    }
    return _result(
        "module", area, "module", project, _scope(ts_from, ts_to),
        summary, [],
        time_range=time_range,
        repos=repos,
        symbols=symbols,
        symbols_overflow=symbols_overflow,
        files=files,
        files_overflow=files_overflow,
        trains=trains,
    )


def _module_trains(conn, project, area, repos, ts_from, ts_to):
    """The decision trains that TOUCHED the area, reusing subsystem_split's
    area-node resolution + `touches`-edge logic. Returns a list of train dicts
    (anchor/key_date/title/outcome/…), ordered by (key_date, anchor). An absent
    area node simply yields no trains (the artifact lifecycles still stand)."""
    # Union the `touches` edges from the area node in EVERY repo that has one — the
    # same area can live in multiple repos (matches slice_module's per-repo handling).
    area_local = "area-{}".format(area)
    touching_src_ids = []
    for repo in repos:
        area_qid = graphstore.qualify_id(project, repo, area_local)
        if graphstore.get_node(conn, area_qid) is None:
            continue
        touching_src_ids.extend(
            e["src_id"] for e in graphstore.get_edges(
                conn, area_qid, direction="in", edge_types=["touches"]))
    if not touching_src_ids:
        return []
    seed_ids = set(touching_src_ids)
    for src_id in touching_src_ids:
        if "#pr-" in src_id:
            seed_ids.add(src_id)
        for pe in graphstore.get_edges(conn, src_id, direction="out",
                                       edge_types=["part_of"]):
            if "#pr-" in pe["dst_id"]:
                seed_ids.add(pe["dst_id"])
    trains_by_anchor = {}
    for seed in sorted(seed_ids):
        reach = graphstore.traverse_spine(conn, [seed], edge_types=_CAUSAL_SPINE,
                                          skip_dead=True)
        reached = set(reach["reached"]) or {seed}
        anchor = _reached_anchor(conn, reached)
        trains_by_anchor.setdefault(anchor, set()).update(reached)
    touch_set = set(touching_src_ids)
    trains = []
    for anchor, reached in trains_by_anchor.items():
        focus_touch_ids = sorted(touch_set & reached)
        role_of = {tid: "touches" for tid in focus_touch_ids}
        train = _train(conn, anchor, reached, focus_touch_ids, role_of,
                       window=_completion_window(ts_from, ts_to))
        if (ts_from is not None or ts_to is not None) and not _date_in_range(
                train["key_date"], ts_from, ts_to):
            continue
        trains.append(train)
    trains.sort(key=lambda t: (t.get("key_date") or "", t["anchor"]))
    return trains


def _fts_query(phrase):
    """Sanitize a raw user phrase into a safe FTS5 MATCH expression.

    Design choice: a LITERAL phrase match. We wrap the whole phrase in double
    quotes (FTS5's string literal) and double any embedded `"`. Inside a quoted
    string FTS5 treats the operators (AND OR NOT * - : ^) and parentheses as
    ordinary tokens, not grammar — so operator/quote-bearing user text never
    raises a syntax error and is searched as the words the user typed. An empty
    or whitespace-only phrase becomes `""` (matches nothing) rather than raising.
    """
    cleaned = str(phrase or "").strip()
    if not cleaned:
        return '""'
    return '"' + cleaned.replace('"', '""') + '"'


def _match_excerpt(node):
    """Bounded excerpt for a matched (mention) touchpoint: the node's body, then
    commit message, then title — whichever searchable text it carries."""
    data = (node["data"] if node else {}) or {}
    for key in ("body", "message", "title"):
        if data.get(key):
            return _excerpt(data[key])
    return ""


def text_mining(conn, project, phrase, ts_from=None, ts_to=None,
                *, backfill=None, complete_budget=50):
    """Every comment / commit message / review / PR-issue body mentioning
    `phrase`, grouped by the decision train each occurrence belongs to —
    chronological, each cited. The O(matches) FTS query: only the matched nodes
    and their trains are hydrated, never the full history.

    FTS5-gated: if the SQLite build lacks FTS5 the store carries no searchable
    index, so this returns a `fts_unavailable` status (a valid answer, exit 0).
    Otherwise `fts_search` returns matched node ids; for each we `traverse_spine`
    to its train, dedupe to origin anchors, and build one `_train` per train
    where the matched nodes are the focus touchpoints (role "mention", excerpt =
    bounded match text). No matches -> status "ok" with an empty `delivered`
    (a valid answer — the phrase simply isn't present), not needs_gather.
    `--from/--to` filter trains by their key date. focus_kind="grep".
    Determinism: trains by (key_date, anchor); touchpoints/timeline by (ts,id).
    """
    if not graphstore.fts5_available(conn):
        return {
            "query": "grep",
            "focus": phrase,
            "focus_kind": "grep",
            "project": project,
            "status": "fts_unavailable",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "FTS5 is unavailable in this SQLite build, so the store carries "
                "no searchable text index; rebuild with FTS5 to run grep"),
        }

    matched = graphstore.fts_search(conn, _fts_query(phrase))
    matched_set = set(matched)

    # group matches by the train each belongs to (seed the spine from each
    # match, dedupe to origin anchors). O(matches): only matches + their
    # trains are hydrated.
    trains_by_anchor = {}  # anchor -> reached set (ids)
    for mid in matched:
        reach = graphstore.traverse_spine(conn, [mid], edge_types=_CAUSAL_SPINE,
                                          skip_dead=True)
        reached = set(reach["reached"])
        if not reached:
            reached = {mid}
        anchor = _reached_anchor(conn, reached)
        trains_by_anchor.setdefault(anchor, set()).update(reached)

    delivered = []
    shipped = 0
    for anchor, reached in trains_by_anchor.items():
        # the matched nodes that fall in this train are the focus touchpoints,
        # each role "mention" with a bounded excerpt of its match text.
        focus_touch_ids = sorted(matched_set & reached)
        role_of = {}
        for tid in focus_touch_ids:
            node = graphstore.get_node(conn, tid)
            role_of[tid] = {"role": "mention", "excerpt": _match_excerpt(node)}
        train = _train(conn, anchor, reached, focus_touch_ids, role_of,
                       window=_completion_window(ts_from, ts_to),
                       backfill=backfill, complete_budget=complete_budget)
        if (ts_from is not None or ts_to is not None) and not _date_in_range(
                train["key_date"], ts_from, ts_to):
            continue
        if train["outcome"] == "shipped":
            shipped += 1
        delivered.append(train)

    summary = {"matches": len(matched), "trains": len(delivered)}
    return _result(
        "grep", phrase, "grep", project, _scope(ts_from, ts_to),
        summary, delivered,
    )


# --------------------------------------------------------------------------
# Markdown render (thin, deterministic formatter over the same JSON).
# --------------------------------------------------------------------------

_OUTCOME_BADGE = {
    "shipped": "[shipped]",
    "rejected": "[rejected]",
    "in_flight": "[in-flight]",
    "removed": "[removed]",
    "alive": "[alive]",
}


def _scope_label(scope):
    if scope == "all-history":
        return "all-history"
    return "{}..{}".format(scope.get("from") or "", scope.get("to") or "")


def member_dependents(conn, project, member):
    """Blast radius: the project members whose areas transitively depend on
    `member`'s areas, via inbound depends_on edges (A depends_on B => edge A->B,
    so 'who depends on B' walks in-edges). `member` is an 'owner/repo' slug.

    Unlike the train-oriented queries this returns a FLAT `dependents` list (no
    `summary`/`delivered`) and is time-independent (`scope` carries no window). A
    member with no area nodes (ungathered/unknown slug) yields a needs_gather
    result, mirroring the sibling queries. Deterministic."""
    seed_areas = [n["id"] for n in graphstore.repo_nodes(
        conn, project, member, "structure")
        if graphstore.parse_id(n["id"])["local"].startswith("area-")]
    if not seed_areas:
        return {
            "query": "dependents", "focus": member, "focus_kind": "member",
            "project": project, "status": "needs_gather",
            "scope": _scope(None, None),
            "guidance": ("no area nodes for member {}; gather it into the project "
                         "store first (areas are sparse in short windows)".format(
                             member)),
        }
    seen, frontier, dependents = set(seed_areas), list(seed_areas), set()
    while frontier:
        nxt = []
        for nid in frontier:
            for e in graphstore.get_edges(conn, nid, direction="in",
                                          edge_types=["depends_on"]):
                src = e["src_id"]
                if src in seen:
                    continue
                seen.add(src)
                nxt.append(src)
                dep_repo = graphstore.parse_id(src)["scope"].split("/", 1)[1]
                if dep_repo != member:
                    dependents.add(dep_repo)
        frontier = nxt
    return {
        "query": "dependents", "focus": member, "focus_kind": "member",
        "project": project, "status": "ok",
        "scope": _scope(None, None),
        "dependents": sorted(dependents),
    }


def _render_train_md(t):
    """One delivery train as deterministic markdown: outcome badge + the
    focus's touchpoints, then the cited spine timeline."""
    lines = []
    badge = _OUTCOME_BADGE.get(t["outcome"], "[{}]".format(t["outcome"]))
    title = t.get("title") or t["anchor"]
    head = "#### {} {}".format(badge, title)
    if t.get("roles"):
        head += " ({})".format(", ".join(t["roles"]))
    lines.append(head)
    if t.get("areas"):
        lines.append("- areas: {}".format(", ".join(t["areas"])))
    if t["touchpoints"]:
        lines.append("- touchpoints:")
        for tp in t["touchpoints"]:
            ref = tp.get("url") or tp.get("sha") or tp["id"]
            extra = ""
            if tp.get("excerpt"):
                extra = " — \"{}\"".format(tp["excerpt"])
            lines.append("  - [{}] {} — {}{}".format(
                tp.get("role") or "?", tp.get("title") or tp["id"], ref, extra))
    lines.append("- timeline:")
    for r in t["timeline"]:
        ref = r.get("url") or r.get("sha") or r["id"]
        when = r.get("ts") or "?"
        kind = r.get("kind") or "?"
        ev = " {}".format(r["event"]) if r.get("event") else ""
        excerpt = " — \"{}\"".format(r["excerpt"]) if r.get("excerpt") else ""
        lines.append("  - {} {}{} — {}{}".format(when, kind, ev, ref, excerpt))
    gaps = t.get("gaps") or []
    if gaps:
        counts = {}
        for g in gaps:
            counts[g["reason"]] = counts.get(g["reason"], 0) + 1
        parts = ["{} {}".format(counts[r], r.replace("_", "-"))
                 for r in sorted(counts)]
        lines.append("> ⚠ {} gaps: {}".format(len(gaps), ", ".join(parts)))
    return "\n".join(lines)


def _render_delivered_md(res):
    lines = ["", "### delivered ({} trains)".format(len(res["delivered"]))]
    for t in res["delivered"]:
        lines.append(_render_train_md(t))
        lines.append("")
    return lines


def _render_person_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: person `{}`\n\n_needs gather:_ {}".format(
            res["focus"], res["guidance"])
    if res["status"] == "fts_unavailable":
        return "## spotlight\n\n_FTS unavailable:_ {}".format(
            res.get("guidance", ""))
    s = res["summary"]
    lines = ["## spotlight: person `{}`{}".format(
        res["focus"], " (bot)" if res["is_bot"] else "")]
    lines.append("")
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- modules: {}".format(", ".join(res["modules"]) or "—"))
    lines.append("- areas: {}".format(", ".join(res["areas"]) or "—"))
    lines.append("- summary: {} trains touched, {} shipped".format(
        s.get("trains_touched", 0), s.get("shipped", 0)))
    roles = ", ".join("{} {}".format(s[r], r) for r in _CONTRIB_TYPES
                      if r in s)
    if roles:
        lines.append("- roles: {}".format(roles))
    lines.append("- symbols authored: {} ({} later removed)".format(
        len(res["symbols_authored"]), len(res["authored_then_removed"])))
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_symbol_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: symbol `{}`\n\n_needs gather:_ {}".format(
            res["focus"], res["guidance"])
    s = res["summary"]
    lines = ["## spotlight: symbol `{}`".format(res["focus"]), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- summary: {} events, outcome {}".format(
        s.get("events", 0), s.get("outcome", "?")))
    lines.append("")
    chain = res["identity_chain"]
    lines.append("### identity chain ({})".format(len(chain)))
    for c in chain:
        if c.get("confidence"):
            lines.append("- {} ({} / {})".format(
                c["id"], c.get("confidence"), c.get("basis")))
        else:
            lines.append("- {}".format(c["id"]))
    # Mermaid only where it adds signal: a multi-link rename/move chain reads
    # better as a left-to-right graph than as a flat list.
    if len(chain) > 1:
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph LR")
        for i, c in enumerate(chain):
            lines.append('  n{}["{}"]'.format(i, c["id"]))
        for i in range(len(chain) - 1):
            lines.append("  n{} --> n{}".format(i, i + 1))
        lines.append("```")
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_subsystem_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: subsystem `{}`\n\n_needs gather:_ {}".format(
            res["focus"], res["guidance"])
    s = res["summary"]
    lines = ["## spotlight: subsystem `{}`".format(res["focus"]), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- summary: {} trains, {} shipped, {} contributors".format(
        s.get("trains", 0), s.get("shipped", 0), s.get("contributors", 0)))
    lines.append("")
    lines.append("### contributors ({})".format(len(res["contributors"])))
    for c in res["contributors"]:
        lines.append("- {} ({})".format(c["login"], c["relation"]))
    lines.append("")
    deps = res["depends_on"]
    lines.append("### depends_on blast radius (out {} / in {})".format(
        len(deps["out"]), len(deps["in"])))
    def _dep_suffix(d):
        bits = []
        if d.get("version") is not None:
            bits.append("v{}".format(d["version"]))
        if d.get("transitive"):
            bits.append("transitive")
        return " ({})".format(", ".join(bits)) if bits else ""
    for d in deps["out"]:
        lines.append("- depends on {}{}".format(d["area"], _dep_suffix(d)))
    for d in deps["in"]:
        lines.append("- depended on by {}{}".format(d["area"], _dep_suffix(d)))
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_grep_md(res):
    if res["status"] == "fts_unavailable":
        return "## spotlight: grep `{}`\n\n_FTS unavailable:_ {}".format(
            res["focus"], res.get("guidance", ""))
    s = res["summary"]
    lines = ["## spotlight: grep `{}`".format(res["focus"]), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- summary: {} matches across {} trains".format(
        s.get("matches", 0), s.get("trains", 0)))
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_dependents_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: blast radius `{}`\n\n_needs gather:_ {}".format(
            res["focus"], res["guidance"])
    deps = res.get("dependents") or []
    head = "## spotlight: blast radius `{}`\n".format(res["focus"])
    if not deps:
        return head + "\nNothing in the project depends on this member.\n"
    return head + "\nMembers that (transitively) depend on it:\n\n" + "\n".join(
        "- `{}`".format(d) for d in deps) + "\n"


def _render_module_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: module `{}`\n\n_needs gather:_ {}".format(
            res["focus"], res["guidance"])
    s = res["summary"]
    tr = res["time_range"]
    lines = ["## spotlight: module `{}`".format(res["focus"]), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- time range: {} → {}".format(
        tr.get("first") or "—", tr.get("last") or "—"))
    lines.append("- summary: {} symbols, {} files, {} trains".format(
        s.get("symbols", 0), s.get("files", 0), s.get("trains", 0)))

    def _ev_suffix(r):
        bits = []
        if r.get("pr"):
            bits.append(r["pr"])
        return " ({})".format(", ".join(bits)) if bits else ""

    lines.append("")
    lines.append("### symbols ({}{})".format(
        len(res["symbols"]),
        ", +{} more".format(res["symbols_overflow"])
        if res["symbols_overflow"] else ""))
    for sym in res["symbols"]:
        lines.append("- `{}` [{}]".format(sym["id"], sym.get("status") or "?"))
        for r in sym["lifecycle"]:
            lines.append("  - {} {} {}{}".format(
                r["date"], r["event"], r["commit"], _ev_suffix(r)))
        if sym["lifecycle_overflow"]:
            lines.append("  - …(+{} more events)".format(
                sym["lifecycle_overflow"]))

    lines.append("")
    lines.append("### files ({}{})".format(
        len(res["files"]),
        ", +{} more".format(res["files_overflow"])
        if res["files_overflow"] else ""))
    for f in res["files"]:
        lines.append("- `{}`".format(f["path"]))
        for r in f["lifecycle"]:
            lines.append("  - {} {} {}{}".format(
                r["date"], r["event"], r["commit"], _ev_suffix(r)))
        if f["lifecycle_overflow"]:
            lines.append("  - …(+{} more events)".format(f["lifecycle_overflow"]))

    lines.append("")
    lines.append("### trains touched ({})".format(len(res["trains"])))
    for t in res["trains"]:
        lines.append(_render_train_md(t))
        lines.append("")
    return "\n".join(lines).rstrip()


_RENDERERS = {
    "person": _render_person_md,
    "symbol": _render_symbol_md,
    "subsystem": _render_subsystem_md,
    "grep": _render_grep_md,
    "dependents": _render_dependents_md,
    "module": _render_module_md,
}


def render_md(res):
    """Deterministic markdown over a Result dict (a thin formatter, not a
    second data path)."""
    return _RENDERERS[res["query"]](res)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="spotlight — cross-cutting analytics over a journey store.")
    parser.add_argument("query", help="query subcommand (e.g. 'person')")
    parser.add_argument("args", nargs="*", help="query arguments")
    parser.add_argument("--store", required=True, help="path to the store db")
    parser.add_argument("--project", default=None,
                        help="project (auto-detected from the store if omitted)")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="bound the delivery train at/after this ts "
                             "(person: train key date; symbol: lifecycle "
                             "event date; subsystem: train key date)")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="bound the delivery train at/before this ts "
                             "(see --from)")
    parser.add_argument("--complete", action="store_true",
                        help="fetch missing cross-window spine anchors via the "
                             "GitHub API (needs GITHUB_TOKEN); default is "
                             "offline/honest-only")
    parser.add_argument("--complete-budget", dest="complete_budget", type=int,
                        default=50,
                        help="max nodes to backfill per train when --complete")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true",
                     help="emit raw cited JSON (default)")
    fmt.add_argument("--md", action="store_true",
                     help="emit a deterministic markdown render")
    args = parser.parse_args(argv)

    if args.query not in _RENDERERS:
        parser.error("unknown query: {}".format(args.query))
    if not args.args:
        parser.error("query '{}' needs an argument".format(args.query))

    conn = graphstore.open_store(args.store)
    project = _detect_project(conn, args.project)

    # Build the (optional) backfill seam. --complete needs a token; without one
    # we stay offline/honest-only rather than failing.
    backfill = None
    if args.complete:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            fetch = gather.make_backfill_fetcher(token)
            backfill = lambda c, mid: gather.backfill(c, mid, fetch=fetch)  # noqa: E731
        else:
            sys.stderr.write("spotlight: --complete needs GITHUB_TOKEN; "
                             "running offline (honest-only)\n")
    cb = args.complete_budget

    if args.query == "person":
        res = person_impact(conn, project, args.args[0],
                            ts_from=args.ts_from, ts_to=args.ts_to,
                            backfill=backfill, complete_budget=cb)
    elif args.query == "symbol":
        res = pattern_evolution(conn, project, args.args[0],
                                ts_from=args.ts_from, ts_to=args.ts_to)
    elif args.query == "subsystem":
        res = subsystem_split(conn, project, args.args[0],
                              ts_from=args.ts_from, ts_to=args.ts_to,
                              backfill=backfill, complete_budget=cb)
    elif args.query == "grep":
        res = text_mining(conn, project, args.args[0],
                          ts_from=args.ts_from, ts_to=args.ts_to,
                          backfill=backfill, complete_budget=cb)
    elif args.query == "module":
        res = slice_module(conn, project, args.args[0],
                           ts_from=args.ts_from, ts_to=args.ts_to)
    elif args.query == "dependents":
        res = member_dependents(conn, project, args.args[0])
    conn.close()

    if args.md:
        print(render_md(res))
    else:
        print(json.dumps(res, sort_keys=True, indent=2))
    return 0  # needs_gather / fts_unavailable are valid answers (exit 0)


if __name__ == "__main__":
    sys.exit(main())
