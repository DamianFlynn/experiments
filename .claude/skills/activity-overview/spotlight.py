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
import re
import sqlite3
import sys

import graphstore
import complete
import manifest
import derive
import gather


def _detect_project(conn, project):
    """Derive the project from the store when not passed (mirrors validate):
    the single non-sentinel project in `nodes`. Raises ValueError on an empty
    store, an ambiguous multi-project store, or a `project` the store does not
    hold — each message lists the store's projects (the CLI turns it into a
    usage error, exit 2)."""
    projects = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL")
    })
    if not projects:
        raise ValueError("the store is empty: nothing has been gathered into "
                         "it (did the gather fail?); nothing to query")
    if project is not None:
        if project not in projects:
            raise ValueError("project {!r} is not in the store (projects: "
                             "{})".format(project, projects))
        return project
    if len(projects) == 1:
        return projects[0]
    raise ValueError(
        "store holds multiple projects {} — pass --project".format(projects))


def _project_repos(conn, project):
    """The non-sentinel repos for a project (people aggregate across them)."""
    return graphstore.project_repos(conn, project)


# Separator of a repo-qualified area argument: `owner/repo::area`. An area id
# is a path and may contain '/', so the repo cannot be split off on '/'.
_AREA_REPO_SEP = "::"


def _store_repos(conn, project):
    """Every repo of `project` that holds a stored node (the members, and a
    repo only another member's depends_on stand-ins created), sorted — the
    repos a reader can name."""
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
        (project,)))


def resolve_repo_arg(conn, project, name):
    """The stored repo a reader's repo argument names, or None (issue #113
    m3). One rule for every query (`dependents`, `subsystem`, `module`, the
    `owner/repo::area` qualifier and `--repo`), on any store:

      - the stored spelling in any case (`Az/A` for `az/a`), as
        graphstore.resolve_repo;
      - `owner/repo` on a single-repo store, which is keyed project=<owner>,
        repo=<name>: the owner must equal the project (any case);
      - `repo` alone, when exactly one stored `owner/repo` has that name.
    No match, or several, is None: the library answers needs_gather and the
    CLI exits 2 listing the repos it knows."""
    if not name:
        return None
    repos = _store_repos(conn, project)

    def match(cand):
        if cand in repos:
            return cand
        hits = [r for r in repos if r.lower() == cand.lower()]
        return hits[0] if len(hits) == 1 else None

    hit = match(name)
    if hit is not None:
        return hit
    owner, sep, rest = name.partition("/")
    if sep and rest and "/" not in rest and owner.lower() == project.lower():
        hit = match(rest)
        if hit is not None:
            return hit
    if not sep:
        hits = [r for r in repos
                if "/" in r and r.split("/", 1)[1].lower() == name.lower()]
        if len(hits) == 1:
            return hits[0]
    return None


def _split_area_arg(area):
    """(repo or None, area) for an area argument: `<repo>::<area>` names the
    repo (`owner/repo`, or `repo` alone — see resolve_repo_arg). The text
    before the first `::` is the qualifier, returned as written (resolve it
    with resolve_repo_arg); an area id itself containing `::` is therefore
    named qualified (`owner/repo::a::b`)."""
    head, sep, tail = area.partition(_AREA_REPO_SEP)
    if sep and head and tail:
        return head, tail
    return None, area


def _area_scope(conn, project, area, repo=None):
    """Resolve an area argument to (repos, area). `area` may be qualified as
    `owner/repo::area`; `repo` (CLI `--repo`) narrows the same way. Both name a
    repo by resolve_repo_arg's rule (any case, `repo` alone where unambiguous,
    `owner/repo` on a single-repo store). Unqualified, the area is looked up
    in EVERY project repo and the results are unioned — the same area id (a
    root area such as `main.tf`, `docs`) is common across the members of a
    multi-repo project. An unknown repo yields `[]` (the caller answers
    needs_gather); a `repo` that disagrees with the qualifier raises
    ValueError."""
    qual, area = _split_area_arg(area)
    if qual is not None and repo is not None:
        if (resolve_repo_arg(conn, project, qual) or qual) != (
                resolve_repo_arg(conn, project, repo) or repo):
            raise ValueError("--repo {} disagrees with the area qualifier "
                             "{}".format(repo, qual))
    name = qual if qual is not None else repo
    if name is None:
        return _project_repos(conn, project), area
    stored = resolve_repo_arg(conn, project, name)
    return ([stored] if stored is not None else []), area


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


def _node_kind(node_id, data, node_class=None):
    """Derive a node's kind for a timeline row from its id / data: one of
    pr / issue / commit / release / comment / symbol / file, else the node's
    structural prefix. Deterministic.

    With the stored node's `node_class` (#101), an artifact is told by its
    class and record (graphstore.is_artifact_node / is_symbol_node), and a
    non-code node is never an artifact whatever its id holds
    (`milestone-v1#a:b:c`). Without it (a dangling id) the id form decides."""
    local = graphstore.local_id(node_id)
    if node_class is not None:
        node = {"node_class": node_class, "data": data}
        if graphstore.is_artifact_node(node):
            return "symbol" if graphstore.is_symbol_node(node) else "file"
    # Artifacts first, by id form (graphstore.split_symbol_local's tail rule):
    # `art:<path>` (path may contain `#`) is a file; `<path>#<lang>:<subkind>:
    # <name>` is a symbol/comment — whose PATH may start with a prefix tested
    # below (`issue-templates/…`, `release-notes/…`).
    elif local.startswith("art:"):
        return "file"
    elif graphstore.is_symbol_local(local):
        return "symbol"
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
    row["kind"] = _node_kind(nid, data, node.get("node_class"))
    row["ts"] = node.get("ts") or ""
    if row["kind"] == "comment":
        row["excerpt"] = _excerpt(data.get("body"))
    return row


# Issue close reasons that mean "not done" (GitHub `state_reason`).
_ISSUE_DROPPED = ("not_planned", "duplicate")


def _outcome(reached_nodes):
    """Deterministic train outcome over its reached spine nodes, in the
    vocabulary and precedence of `link.build_trains` (shipped > in_flight >
    rejected > abandoned), plus `direct_push` for work that landed without a PR:
      shipped     — a release, a merged PR, or (no PR) an issue closed as
                    completed (a closed issue without a `state_reason` reads as
                    completed, GitHub's default close reason);
      in_flight   — an open PR, or (no PR) an open issue;
      rejected    — PR(s) present, all closed unmerged;
      direct_push — no PR and no open/completed issue, but commits: code that
                    reached the branch without a PR;
      abandoned   — only issue(s) closed `not_planned` / `duplicate`;
      in_flight   — anything else (nothing that resolves the train).
    `reached_nodes` is an iterable of node dicts."""
    prs, issues, has_commit = [], [], False
    for n in reached_nodes:
        if n is None:
            continue
        data = n.get("data") or {}
        kind = _node_kind(n["id"], data, n.get("node_class"))
        if kind == "release":
            return "shipped"
        if kind == "pr":
            prs.append(data)
        elif kind == "issue":
            issues.append(data)
        elif kind == "commit":
            has_commit = True
    if any(p.get("merged") for p in prs):
        return "shipped"
    if prs:
        if all(p.get("state") == "closed" for p in prs):
            return "rejected"
        return "in_flight"
    if any(i.get("state") != "closed" for i in issues):
        return "in_flight"
    if any(i.get("state_reason") not in _ISSUE_DROPPED for i in issues):
        return "shipped"
    if has_commit:
        return "direct_push"
    if issues:
        return "abandoned"
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
        kind = _node_kind(n["id"], n.get("data") or {}, n.get("node_class"))
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
            row["kind"] = _node_kind(tid, tdata,
                                     tnode.get("node_class") if tnode else None)
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
        # the member repos the train's nodes live in (a cross-repo train spans
        # several); person nodes carry the "*" sentinel and are not a repo.
        "repos": sorted({n["repo"] for n in nodes
                         if n.get("repo") and n["repo"] != "*"}),
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
        # the exact, case-sensitive `{project}/{repo}#` prefix as an index
        # range (LIKE is case-insensitive and cannot use the index)
        lo, hi = graphstore._prefix_range(graphstore.qualify_id(project, repo, ""))
        rows = conn.execute(
            "SELECT artifact_id, commit_sha FROM code_events "
            "WHERE artifact_id >= ? AND artifact_id < ? AND author=? "
            "AND event='add' ORDER BY artifact_id, commit_sha",
            (lo, hi, login)).fetchall()
        commit_files = None
        seen = set()
        for aid, sha in rows:
            local = graphstore.local_id(aid, project, repo)
            if aid in seen or not graphstore.is_symbol_local(local):
                continue  # file-level artifact (path may contain `#`), not a symbol
            if commit_files is None:
                commit_files = graphstore.repo_commit_files(conn, project, repo)
            # a row whose commit lists that exact path is a file row (#101)
            if not graphstore.ledger_row_is_symbol(local, commit_files.get(sha)):
                continue
            seen.add(aid)
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


def _completion_window(ts_from, ts_to):
    """The (lo, hi) bounds for completion's window check, or None when
    unbounded. Bounds are passed through as given (date-only or ISO): the
    granularity rule — a date-only ts or bound covers its whole day — lives in
    `graphstore.ts_in_window`, which both `_ts_in_window` and
    `complete._in_window` delegate to (issue #52)."""
    if ts_from is None and ts_to is None:
        return None
    return (ts_from, ts_to)


def _ts_in_window(ts, window):
    """True if `ts` falls within a (lo, hi) completion window (either bound may
    be None). An empty ts cannot be proven in-window -> False."""
    if window is None:
        return True
    lo, hi = window
    return graphstore.ts_in_window(ts, lo, hi)


def _date_in_range(ts, ts_from, ts_to):
    """A train/event date filter. An empty ts is excluded once a bound is set.
    Granularity-safe via `graphstore.ts_in_window`: an inclusive `--to` of
    `2026-01-12` still matches a same-day ISO datetime like
    `2026-01-12T14:00:00Z`, and a date-only ts (a commit day) matches an ISO
    bound on that same day."""
    return graphstore.ts_in_window(ts, ts_from, ts_to)


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
    repos = _project_repos(conn, project)
    # Qualified iff it starts with a known `{project}/{repo}#` scope — NOT merely
    # `{project}/`, which a local path (`{project}/main.tf#...`) can also start with.
    for repo in repos:
        if artifact_id.startswith(graphstore.qualify_id(project, repo, "")):
            return artifact_id  # already project-qualified
    for repo in repos:
        cand = graphstore.qualify_id(project, repo, artifact_id)
        if graphstore.get_node(conn, cand) is not None:
            return cand
        row = conn.execute(
            "SELECT 1 FROM code_events WHERE artifact_id=? LIMIT 1",
            (_ledger_key(cand),)).fetchone()
        if row:
            return cand
    if artifact_id.startswith("{}/".format(project)):
        return artifact_id  # qualified for a repo not (yet) stored: keep as given
    return None


def _ledger_key(aid):
    """The `code_events` key for an artifact id. The file-level ledger is keyed
    by the BARE path (`{project}/{repo}#<path>`) while a file artifact NODE is
    `{project}/{repo}#art:<path>` (gather folds them under different keys), so
    an `art:` id maps to its bare-path row key; every other id is its own key."""
    parts = graphstore.parse_id(aid)
    if parts["local"].startswith("art:"):
        return "{}#{}".format(parts["scope"], parts["local"][len("art:"):])
    return aid


# Ledger events that end an artifact: a symbol's mapped `remove`, a file's raw
# git `delete` (the file-level ledger stores git's change verb).
_REMOVAL_EVENTS = ("remove", "delete")


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
    train's `outcome` is "removed" if the last event is a removal (`remove`
    for a symbol, `delete` for a file), else "alive". A file artifact may be
    named by its node id (`art:<path>`) or its bare path; both read the
    bare-path ledger (`_ledger_key`).
    Context: `identity_chain` — the cross-path history walked over
    replaced_by/identity_from. `--from/--to` bound the lifecycle window.
    Determinism: timeline by (date, commit); chain in link order (depth-capped).
    """
    qid = _normalize_artifact_id(conn, project, artifact_id)
    events = graphstore.get_code_events(conn, _ledger_key(qid)) if qid else []
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

    outcome = "removed" if (timeline and timeline[-1]["event"] in _REMOVAL_EVENTS) \
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
                    *, repo=None, backfill=None, complete_budget=50):
    """An area's activity + blast radius as the chronological delivery trains
    that TOUCHED the area (optional time range). Returns the unified
    citation-bearing envelope; an unknown area (no `area-<area>` structure node
    in any project repo) yields a needs_gather result.

    Repo scope: the area is looked up in EVERY project repo and the repos that
    have it are unioned (the same area id is common across multi-repo members);
    `owner/repo::area` or `repo=` (any case) narrows it to one member. `repos`
    lists the members whose area node answered.

    `delivered`: one `_train` per train that touched the area (seed the spine
    from the area's touching commits/PRs); each train's `touchpoints` are the
    area-touching nodes within it. `summary`: trains + shipped + contributors.
    Context blocks:
      contributors  — codeowners (inverse `owns`) plus the authors of commits/
                      PRs that `touches` the area, deduped (`owns` beats
                      `touches`), ordered by login; `repos` = where.
      depends_on    — blast radius: `depends_on` edges OUT (what it depends on)
                      and IN (what depends on it), each carrying version/
                      transitive and the `repo` of the area node it hangs off.
    `--from/--to` filter trains by their key date.
    Determinism: trains by (key_date, anchor); contributors by login; deps by
    (id, repo).
    """
    # resolve the area node in every project repo that has it (or the one repo
    # named by `owner/repo::area` / `repo`), and union them
    repos, area = _area_scope(conn, project, area, repo)
    area_local = "area-{}".format(area)
    area_qids = []  # (repo, area node id), in repo order
    for r in repos:
        cand = graphstore.qualify_id(project, r, area_local)
        if graphstore.get_node(conn, cand) is not None:
            area_qids.append((r, cand))
    if not area_qids:
        return {
            "query": "subsystem",
            "focus": area,
            "focus_kind": "subsystem",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no area node for {} in {}; gather a window that touches it "
                "(areas are sparse in short windows)".format(
                    area, ", ".join(repos) or "the named repo")),
        }

    def _author_login(node):
        return (node["data"] or {}).get("author") if node else None

    # --- in-edges to each area node: owns (person) + touches (commit/pr) ---
    relations = {}  # login -> relation ('owns' beats 'touches')
    login_repos = {}  # login -> repos of the area nodes they own/touch
    touching = {}  # touching src id -> repo of the area node it touches
    for r, area_qid in area_qids:
        for e in graphstore.get_edges(conn, area_qid, direction="in",
                                      edge_types=["owns", "touches"]):
            if e["edge_type"] == "owns":
                # src is a person node; recover the login
                person = graphstore.get_node(conn, e["src_id"])
                login = (person["data"] or {}).get("login") if person else None
                if login is None:
                    login = graphstore.parse_id(e["src_id"])["local"]
                    if login.startswith("person-"):
                        login = login[len("person-"):]
                relations[login] = "owns"
                login_repos.setdefault(login, set()).add(r)
            elif e["edge_type"] == "touches":
                touching.setdefault(e["src_id"], r)
    touching_src_ids = sorted(touching)

    # touching commits/PRs -> their authors are touches contributors, and the
    # PRs they belong to are the area's attributed items.
    attributed_prs = {}  # pr_id -> pr node
    for src_id in touching_src_ids:
        src = graphstore.get_node(conn, src_id)
        login = _author_login(src)
        if login is not None:
            relations.setdefault(login, "touches")
            login_repos.setdefault(login, set()).add(touching[src_id])
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
        {"login": login, "relation": relations[login],
         "repos": sorted(login_repos.get(login, ()))}
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
    # `repo` on each row is the member whose area node the edge hangs off.
    def _dep_row(other_id, data, r):
        d = data or {}
        local = graphstore.parse_id(other_id)["local"]
        if local.startswith("area-"):
            local = local[len("area-"):]
        return {
            "area": local,
            "id": other_id,
            "repo": r,
            "version": d.get("version"),
            "transitive": d.get("transitive"),
        }

    deps_out, deps_in = [], []
    for r, area_qid in area_qids:
        for e in graphstore.get_edges(conn, area_qid, direction="out",
                                      edge_types=["depends_on"]):
            deps_out.append(_dep_row(e["dst_id"], e["data"], r))
        for e in graphstore.get_edges(conn, area_qid, direction="in",
                                      edge_types=["depends_on"]):
            deps_in.append(_dep_row(e["src_id"], e["data"], r))
    deps_out.sort(key=lambda d: (d["id"], d["repo"]))
    deps_in.sort(key=lambda d: (d["id"], d["repo"]))

    summary = {
        "trains": len(delivered),
        "shipped": shipped,
        "contributors": len(contributors),
    }
    return _result(
        "subsystem", area, "subsystem", project, _scope(ts_from, ts_to),
        summary, delivered,
        repos=[r for r, _ in area_qids],
        contributors=contributors,
        depends_on={"out": deps_out, "in": deps_in},
    )


def _repo_windows(conn, project, repo):
    """The repo's gathered windows, (from, to) sorted by date."""
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    return sorted({(w.get("from"), w.get("to")) for w in ledger
                   if isinstance(w, dict) and w.get("project") == project
                   and w.get("repo") == repo and w.get("from") and w.get("to")})


def _code_graphs(conn, project, repo):
    """Every stored code graph of `repo`, for a reader that spans all history
    (issue #113 M6): the code graph of each gathered window that carries #94
    per-window facts, in window-date order, plus the latest-wins `codegraph`
    singleton when the store has none or some gathered window has no facts
    entry (folded before #94). Never depends on fold order."""
    graphs, legacy = [], False
    windows = _repo_windows(conn, project, repo)
    for frm, to in windows:
        wf = graphstore.get_window_facts(conn, project, repo, frm, to,
                                         keys=("code_graph",))
        if wf is None:
            legacy = True
            continue
        cg = (wf.get("facts") or {}).get("code_graph")
        if cg:
            graphs.append(cg)
    if legacy or not windows:
        node = graphstore.get_node(
            conn, graphstore.qualify_id(project, repo, "codegraph"))
        if node is not None and node.get("data"):
            graphs.append(node["data"])
    return graphs


def _area_paths(conn, project, repo):
    """{path: {area ids}} — the union of the area index (derive.area_index)
    over every stored code graph of `repo` (_code_graphs). A path two windows
    put in different areas (a re-organised tree, a graphify run) is in both."""
    out = {}
    for cg in _code_graphs(conn, project, repo):
        for path, aid in derive.area_index(cg).items():
            out.setdefault(path, set()).add(aid)
    return out


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


def slice_module(conn, project, area, ts_from=None, ts_to=None,
                 *, repo=None, backfill=None, complete_budget=50):
    """A store-backed, FULL-HISTORY slice of one module (an `area`): every
    artifact under the area + each artifact's complete lifecycle (CRUD across all
    gathered windows, with the change detail), plus the trains that touched it —
    a bounded "module biography". Reads the store only; never writes/networks.

    Resolution: the union of the area index over EVERY stored code graph of
    the repo (`_area_paths`: each gathered window's #94 per-window code graph,
    plus the latest-wins `codegraph` singleton on a store folded before #94),
    so the answer never depends on fold order (issue #113); keep `code`
    artifact nodes whose `path` some window's graph puts in the requested
    area. An unknown/empty area (no stored code graph lists it in any repo)
    yields the shared `needs_gather` envelope. Repo scope as in subsystem_split:
    union across every repo whose code graphs have the area (listed in `repos`),
    or one member via `owner/repo::area` / `repo=`. Every symbol and file row
    carries its `repo` (the same path in two members is two rows).

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
    symbols by id, files by (path, repo), lifecycle by (date, commit).
    `backfill`/`complete_budget` feed the trains' opt-in completion (`--complete`).
    """
    repos, area = _area_scope(conn, project, area, repo)

    # --- resolve the area per repo (NEVER merge indexes: a path string can map to
    #     different areas in different repos, so match each artifact against ITS OWN
    #     repo's codegraph) ---
    area_idx_by_repo = {r: _area_paths(conn, project, r) for r in repos}
    area_repos = [r for r in repos
                  if any(area in areas
                         for areas in area_idx_by_repo[r].values())]
    if not area_repos:
        return {
            "query": "module",
            "focus": area,
            "focus_kind": "module",
            "project": project,
            "status": "needs_gather",
            "scope": _scope(ts_from, ts_to),
            "guidance": (
                "no codegraph area covering {} in {}; gather a window that "
                "touches the module".format(
                    area, ", ".join(repos) or "the named repo")),
        }

    # --- collect the area's artifact code nodes (file + symbol/comment) ---
    artifacts = {}  # qid -> node
    for r in area_repos:
        idx = area_idx_by_repo[r]
        for node in graphstore.repo_nodes(conn, project, r, node_class="code"):
            data = node["data"] or {}
            # artifact `code` nodes are the non-commit ones: a commit carries a
            # `sha` and a bare `<sha>` id; artifacts use `art:<path>` / `<path>#…`
            # and have no `sha` (mirrors extract._is_commit_node).
            if "sha" in data:
                continue
            path = data.get("path")
            if path is None or area not in idx.get(path, ()):
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
    # NODE is `art:<path>` (gather folds them under different keys); see
    # `_ledger_key`.
    all_shas = set()
    events_by_member = {}
    for aid in artifacts:
        evs = graphstore.get_code_events(conn, _ledger_key(aid))
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
        # symbol/comment artifacts vs file artifacts, told by the stored record's
        # kind (graphstore.is_symbol_node, #101), not the id form.
        if graphstore.is_symbol_node(node):
            entry = {
                "id": rep,
                "repo": node["repo"],
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
                "repo": node["repo"],
                "lifecycle": lifecycle,
                "lifecycle_overflow": overflow,
            }
            files.append(entry)

    symbols.sort(key=lambda s: s["id"])
    files.sort(key=lambda f: (f["path"] or "", f["repo"]))
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
    trains = _module_trains(conn, project, area, area_repos, ts_from, ts_to,
                            backfill=backfill, complete_budget=complete_budget)

    summary = {
        "symbols": len(symbols),
        "files": len(files),
        "trains": len(trains),
    }
    return _result(
        "module", area, "module", project, _scope(ts_from, ts_to),
        summary, [],
        time_range=time_range,
        repos=area_repos,
        symbols=symbols,
        symbols_overflow=symbols_overflow,
        files=files,
        files_overflow=files_overflow,
        trains=trains,
    )


def _module_trains(conn, project, area, repos, ts_from, ts_to,
                   *, backfill=None, complete_budget=50):
    """The decision trains that TOUCHED the area, reusing subsystem_split's
    area-node resolution + `touches`-edge logic. Returns a list of train dicts
    (anchor/key_date/title/outcome/…), ordered by (key_date, anchor). An absent
    area node simply yields no trains (the artifact lifecycles still stand)."""
    # Union the `touches` edges from the area node in EVERY repo that has one — the
    # same area can live in multiple repos (matches slice_module's per-repo handling).
    area_local = "area-{}".format(area)
    touching_src_ids = []
    for r in repos:
        area_qid = graphstore.qualify_id(project, r, area_local)
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
                       window=_completion_window(ts_from, ts_to),
                       backfill=backfill, complete_budget=complete_budget)
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
    # A NUL byte ends the string for SQLite's FTS5 parser ("unterminated
    # string"); it can never be in indexed text, so treat it as a space.
    cleaned = str(phrase or "").replace("\x00", " ").strip()
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
    The index is store-wide, so matches outside `project` are dropped first.
    `--from/--to` filter trains by their key date; `summary.matches` counts the
    matches inside the trains that survive that filter. focus_kind="grep".
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

    # fts_text is store-wide: keep only this project's ids (repo-scoped
    # `{project}/…` and project-scoped `{project}#…`).
    prefixes = ("{}/".format(project), "{}#".format(project))
    matched = [m for m in graphstore.fts_search(conn, _fts_query(phrase))
               if m.startswith(prefixes)]
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
    counted = set()  # matches inside a train that survived the scope filter
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
        counted.update(focus_touch_ids)
        delivered.append(train)

    summary = {"matches": len(counted), "trains": len(delivered)}
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
    "abandoned": "[abandoned]",
    "direct_push": "[direct-push]",
    "removed": "[removed]",
    "alive": "[alive]",
}


# Untrusted text in `--md` (issue #113 m1). Titles, bodies, logins, paths, area
# ids, the focus and the grep phrase come from GitHub or the user, so each is
# rendered through _md: control characters (newlines included) collapse to a
# space, so a value can never start a new markdown block; in running text the
# markdown / HTML syntax characters are backslash-escaped or written as
# entities (`<` `>` `&`), and a leading block marker before a space (`#`,
# `-`, `+`, `=`, `1.`) is escaped, so a value cannot add a heading, a quote,
# a list, a link, an image, raw HTML or a table cell; inside a code span the
# span's fence is longer than any backtick run in the value, so it cannot be
# broken. `--json` stays raw (it is data).
_MD_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_MD_ESCAPED = "\\`*_[]|~"
_MD_BLOCK_START = re.compile(r"^(\s*)(?:([#]+|[-+=]+)(?=\s|$)|(\d+)([.)])(?=\s|$))")


def _md(value, code=False):
    """`value` as safe markdown text — or, with `code`, as a code span. The one
    escaping helper every --md renderer uses for a value read from the store
    or the command line. Readable: nothing is deleted, only escaped."""
    text = _MD_CONTROL.sub(" ", "" if value is None else str(value))
    if code:
        runs = re.findall("`+", text)
        fence = "`" * (max((len(r) for r in runs), default=0) + 1)
        pad = " " if (text[:1] == "`" or text[-1:] == "`"
                      or (text[:1] == " " and text[-1:] == " ")) else ""
        return "{0}{1}{2}{1}{0}".format(fence, pad, text)
    out = []
    for ch in text:
        if ch in _MD_ESCAPED:
            out.append("\\" + ch)
        elif ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        else:
            out.append(ch)
    text = "".join(out)
    m = _MD_BLOCK_START.match(text)
    if m:
        if m.group(2):
            text = m.group(1) + "\\" + text[len(m.group(1)):]
        else:
            cut = m.end(3)
            text = text[:cut] + "\\" + text[cut:]
    return text


def _mermaid_label(value):
    """A value as the text of a quoted mermaid node label: one line, its quote
    and angle brackets as mermaid entity codes."""
    text = _MD_CONTROL.sub(" ", "" if value is None else str(value))
    return (text.replace("#", "#35;").replace('"', "#quot;")
            .replace("<", "#lt;").replace(">", "#gt;").replace("`", "#96;"))


def _scope_label(scope):
    if scope == "all-history":
        return "all-history"
    return "{}..{}".format(_md(scope.get("from") or ""), _md(scope.get("to") or ""))


def member_dependents(conn, project, member):
    """Blast radius: the project members whose areas transitively depend on
    `member`'s areas, via inbound depends_on edges (A depends_on B => edge A->B,
    so 'who depends on B' walks in-edges). `member` is an 'owner/repo' slug.

    Unlike the train-oriented queries this returns a FLAT `dependents` list (no
    `summary`/`delivered`) and is time-independent (`scope` carries no window). A
    member with no area nodes (ungathered/unknown slug) yields a needs_gather
    result, mirroring the sibling queries. `member` is named by
    resolve_repo_arg's rule (any case, issue #70; `repo` alone where
    unambiguous; `owner/repo` on a single-repo store, #113) and resolves to
    the stored spelling, which `focus` reports. Deterministic."""
    member = resolve_repo_arg(conn, project, member) or member
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


def _area_label_of(project, node_id):
    """`owner/repo::area` for an area node id (single-repo or project store)."""
    p = graphstore.parse_id(node_id)
    rest = p["scope"][len(project) + 1:]
    repo = rest if "/" in rest else p["scope"]
    return "{}{}{}".format(repo, _AREA_REPO_SEP, p["local"][len("area-"):])


def area_dependents(conn, project, area):
    """Blast radius of ONE module area (issue #92): every area that depends on
    it, directly or transitively, via inbound depends_on edges — in the same repo
    (e.g. the bicep-registry-modules modules consuming
    `avm/res/network/virtual-network`) and, in a project store, in other members.
    `area` is `owner/repo::<area id>` (the repo in any case; the area id exact).

    Returns `dependents` (all, sorted `owner/repo::area`) and `direct` (those
    with an edge straight to it). Time-independent, like member_dependents. An
    unknown repo or an area with no node is a needs_gather answer. Deterministic."""
    qual, local = _split_area_arg(area)
    stored = resolve_repo_arg(conn, project, qual) if qual else None
    seed = (graphstore.qualify_id(project, stored, "area-" + local)
            if stored is not None else None)
    if seed is None or graphstore.get_node(conn, seed) is None:
        return {
            "query": "dependents", "focus": area, "focus_kind": "area",
            "project": project, "status": "needs_gather",
            "scope": _scope(None, None),
            "guidance": ("no area node for {}; name it owner/repo::<area id> and "
                         "gather that repo (with --structural-iac for a "
                         "single repo, so modules unchanged in the window are "
                         "areas too)".format(area)),
        }
    seen, frontier, direct = {seed}, [seed], set()
    while frontier:
        nxt = []
        for nid in frontier:
            for e in graphstore.get_edges(conn, nid, direction="in",
                                          edge_types=["depends_on"]):
                src = e["src_id"]
                # A stored edge marked transitive (a nested deployment the
                # build resolved) reaches the seed through another module: it
                # widens the blast radius but is not a direct consumer.
                if nid == seed and not (e.get("data") or {}).get("transitive"):
                    direct.add(src)
                if src not in seen:
                    seen.add(src)
                    nxt.append(src)
        frontier = nxt
    seen.discard(seed)
    direct.discard(seed)
    return {
        "query": "dependents", "focus": _area_label_of(project, seed),
        "focus_kind": "area", "project": project, "status": "ok",
        "scope": _scope(None, None),
        "direct": sorted(_area_label_of(project, n) for n in direct),
        "dependents": sorted(_area_label_of(project, n) for n in seen),
    }


def _render_train_md(t):
    """One delivery train as deterministic markdown: outcome badge + the
    focus's touchpoints, then the cited spine timeline. Every stored value
    goes through _md."""
    lines = []
    badge = _OUTCOME_BADGE.get(t["outcome"]) or "[{}]".format(_md(t["outcome"]))
    head = "#### {} {}".format(badge, _md(t.get("title") or t["anchor"]))
    if t.get("roles"):
        head += " ({})".format(", ".join(_md(r) for r in t["roles"]))
    lines.append(head)
    if t.get("repos"):
        lines.append("- repos: {}".format(", ".join(_md(r) for r in t["repos"])))
    if t.get("areas"):
        lines.append("- areas: {}".format(", ".join(_md(a) for a in t["areas"])))
    if t["touchpoints"]:
        lines.append("- touchpoints:")
        for tp in t["touchpoints"]:
            ref = tp.get("url") or tp.get("sha") or tp["id"]
            extra = ""
            if tp.get("excerpt"):
                extra = " — \"{}\"".format(_md(tp["excerpt"]))
            lines.append("  - [{}] {} — {}{}".format(
                _md(tp.get("role") or "?"), _md(tp.get("title") or tp["id"]),
                _md(ref), extra))
    lines.append("- timeline:")
    for r in t["timeline"]:
        ref = r.get("url") or r.get("sha") or r["id"]
        when = r.get("ts") or "?"
        kind = r.get("kind") or "?"
        ev = " {}".format(_md(r["event"])) if r.get("event") else ""
        excerpt = " — \"{}\"".format(_md(r["excerpt"])) if r.get("excerpt") else ""
        lines.append("  - {} {}{} — {}{}".format(_md(when), _md(kind), ev,
                                                _md(ref), excerpt))
    gaps = t.get("gaps") or []
    if gaps:
        counts = {}
        for g in gaps:
            counts[g["reason"]] = counts.get(g["reason"], 0) + 1
        parts = ["{} {}".format(counts[r], _md(r.replace("_", "-")))
                 for r in sorted(counts)]
        lines.append("> ⚠ {} gaps: {}".format(len(gaps), ", ".join(parts)))
    return "\n".join(lines)


def _render_delivered_md(res):
    lines = ["", "### delivered ({} trains)".format(len(res["delivered"]))]
    for t in res["delivered"]:
        lines.append(_render_train_md(t))
        lines.append("")
    return lines


def _needs_gather_md(label, res):
    return "## spotlight: {} {}\n\n_needs gather:_ {}".format(
        label, _md(res["focus"], code=True), _md(res.get("guidance", "")))


def _render_person_md(res):
    if res["status"] == "needs_gather":
        return _needs_gather_md("person", res)
    if res["status"] == "fts_unavailable":
        return "## spotlight\n\n_FTS unavailable:_ {}".format(
            _md(res.get("guidance", "")))
    s = res["summary"]
    lines = ["## spotlight: person {}{}".format(
        _md(res["focus"], code=True), " (bot)" if res["is_bot"] else "")]
    lines.append("")
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- modules: {}".format(
        ", ".join(_md(m) for m in res["modules"]) or "—"))
    lines.append("- areas: {}".format(
        ", ".join(_md(a) for a in res["areas"]) or "—"))
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
        return _needs_gather_md("symbol", res)
    s = res["summary"]
    lines = ["## spotlight: symbol {}".format(_md(res["focus"], code=True)), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- summary: {} events, outcome {}".format(
        s.get("events", 0), _md(s.get("outcome", "?"))))
    lines.append("")
    chain = res["identity_chain"]
    lines.append("### identity chain ({})".format(len(chain)))
    for c in chain:
        if c.get("confidence"):
            lines.append("- {} ({} / {})".format(
                _md(c["id"]), _md(c.get("confidence")), _md(c.get("basis"))))
        else:
            lines.append("- {}".format(_md(c["id"])))
    # Mermaid only where it adds signal: a multi-link rename/move chain reads
    # better as a left-to-right graph than as a flat list.
    if len(chain) > 1:
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph LR")
        for i, c in enumerate(chain):
            lines.append('  n{}["{}"]'.format(i, _mermaid_label(c["id"])))
        for i in range(len(chain) - 1):
            lines.append("  n{} --> n{}".format(i, i + 1))
        lines.append("```")
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_subsystem_md(res):
    if res["status"] == "needs_gather":
        return _needs_gather_md("subsystem", res)
    s = res["summary"]
    lines = ["## spotlight: subsystem {}".format(_md(res["focus"], code=True)), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    if res.get("repos"):
        lines.append("- repos: {}".format(", ".join(_md(r) for r in res["repos"])))
    lines.append("- summary: {} trains, {} shipped, {} contributors".format(
        s.get("trains", 0), s.get("shipped", 0), s.get("contributors", 0)))
    lines.append("")
    lines.append("### contributors ({})".format(len(res["contributors"])))
    for c in res["contributors"]:
        where = "; {}".format(", ".join(_md(r) for r in c["repos"])) \
            if c.get("repos") else ""
        lines.append("- {} ({}{})".format(_md(c["login"]), _md(c["relation"]),
                                          where))
    lines.append("")
    deps = res["depends_on"]
    lines.append("### depends_on blast radius (out {} / in {})".format(
        len(deps["out"]), len(deps["in"])))

    def _dep_suffix(d):
        bits = []
        if d.get("version") is not None:
            bits.append("v{}".format(_md(d["version"])))
        if d.get("transitive"):
            bits.append("transitive")
        return " ({})".format(", ".join(bits)) if bits else ""

    def _dep_repo(d):
        return "[{}] ".format(_md(d["repo"])) if d.get("repo") else ""

    def _dep_target(d):
        # name the other end's repo when it is not the row's own repo
        scope = graphstore.parse_id(d.get("id") or "")["scope"]
        other = scope.split("/", 1)[1] if "/" in scope else None
        if d.get("repo") and other and other != d["repo"]:
            return "{} in {}".format(_md(d["area"]), _md(other))
        return _md(d["area"])
    for d in deps["out"]:
        lines.append("- {}depends on {}{}".format(
            _dep_repo(d), _dep_target(d), _dep_suffix(d)))
    for d in deps["in"]:
        lines.append("- {}depended on by {}{}".format(
            _dep_repo(d), _dep_target(d), _dep_suffix(d)))
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_grep_md(res):
    if res["status"] == "fts_unavailable":
        return "## spotlight: grep {}\n\n_FTS unavailable:_ {}".format(
            _md(res["focus"], code=True), _md(res.get("guidance", "")))
    s = res["summary"]
    lines = ["## spotlight: grep {}".format(_md(res["focus"], code=True)), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- summary: {} matches across {} trains".format(
        s.get("matches", 0), s.get("trains", 0)))
    lines.extend(_render_delivered_md(res))
    return "\n".join(lines).rstrip()


def _render_dependents_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: blast radius {}\n\n_needs gather:_ {}".format(
            _md(res["focus"], code=True), _md(res.get("guidance", "")))
    deps = res.get("dependents") or []
    head = "## spotlight: blast radius {}\n".format(_md(res["focus"], code=True))
    if res.get("focus_kind") == "area":
        if not deps:
            return head + "\nNothing in the store depends on this area.\n"
        direct = set(res.get("direct") or [])
        return head + "\nAreas that depend on it (direct, then transitive):\n\n" \
            + "\n".join("- {}{}".format(_md(d, code=True),
                                        "" if d in direct else " (transitive)")
                        for d in sorted(deps, key=lambda d: (d not in direct, d))) \
            + "\n"
    if not deps:
        return head + "\nNothing in the project depends on this member.\n"
    return head + "\nMembers that (transitively) depend on it:\n\n" + "\n".join(
        "- {}".format(_md(d, code=True)) for d in deps) + "\n"


def _render_module_md(res):
    if res["status"] == "needs_gather":
        return _needs_gather_md("module", res)
    s = res["summary"]
    tr = res["time_range"]
    lines = ["## spotlight: module {}".format(_md(res["focus"], code=True)), ""]
    lines.append("- scope: {}".format(_scope_label(res["scope"])))
    lines.append("- time range: {} → {}".format(
        _md(tr.get("first") or "—"), _md(tr.get("last") or "—")))
    lines.append("- summary: {} symbols, {} files, {} trains".format(
        s.get("symbols", 0), s.get("files", 0), s.get("trains", 0)))

    def _ev_suffix(r):
        bits = []
        if r.get("pr"):
            bits.append(_md(r["pr"]))
        return " ({})".format(", ".join(bits)) if bits else ""

    def _event_line(r):
        return "  - {} {} {}{}".format(_md(r["date"]), _md(r["event"]),
                                       _md(r["commit"]), _ev_suffix(r))

    lines.append("")
    lines.append("### symbols ({}{})".format(
        len(res["symbols"]),
        ", +{} more".format(res["symbols_overflow"])
        if res["symbols_overflow"] else ""))
    for sym in res["symbols"]:
        lines.append("- {} [{}]".format(_md(sym["id"], code=True),
                                            _md(sym.get("status") or "?")))
        for r in sym["lifecycle"]:
            lines.append(_event_line(r))
        if sym["lifecycle_overflow"]:
            lines.append("  - …(+{} more events)".format(
                sym["lifecycle_overflow"]))

    lines.append("")
    lines.append("### files ({}{})".format(
        len(res["files"]),
        ", +{} more".format(res["files_overflow"])
        if res["files_overflow"] else ""))
    for f in res["files"]:
        lines.append("- {} ({})".format(_md(f["path"], code=True), _md(f["repo"])))
        for r in f["lifecycle"]:
            lines.append(_event_line(r))
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

# Queries that build delivery trains (and so can complete them with --complete),
# and the ones whose argument is an area (and so take --repo).
_TRAIN_QUERIES = ("person", "subsystem", "grep", "module")
_AREA_QUERIES = ("subsystem", "module")


def main(argv=None):
    """Exit codes: 0 a result on stdout (`needs_gather` / `fts_unavailable` are
    valid answers); 2 usage error — unknown query, not exactly one query
    argument, a flag the query does not take, bad --from/--to, a missing or
    unreadable store, an empty store, an unknown or (with several in the store)
    unnamed --project, an unknown --repo. Nothing is written to stdout on 2."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="spotlight — cross-cutting analytics over a journey store.")
    parser.add_argument("query", help="query: {}".format(", ".join(_RENDERERS)))
    parser.add_argument("args", nargs="*",
                        help="the query's one argument (quote a multi-word grep "
                             "phrase)")
    parser.add_argument("--store", required=True, help="path to the store db")
    parser.add_argument("--project", default=None,
                        help="project (auto-detected when the store holds one)")
    parser.add_argument("--repo", default=None,
                        help="subsystem/module: only this repo (owner/repo in "
                             "any case, or repo alone when unambiguous); same "
                             "as the argument <repo>::<area>")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="bound the delivery train at/after this ts "
                             "(person: train key date; symbol: lifecycle "
                             "event date; subsystem: train key date)")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="bound the delivery train at/before this ts "
                             "(see --from)")
    parser.add_argument("--complete", action="store_true",
                        help="fetch missing cross-window PRs/issues via the "
                             "GitHub API (needs GITHUB_TOKEN) and write them as "
                             "a gather would; default is offline/honest-only. "
                             "Train queries only: "
                             + ", ".join(_TRAIN_QUERIES))
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
        parser.error("unknown query: {} (choose from {})".format(
            args.query, ", ".join(_RENDERERS)))
    if len(args.args) != 1:
        parser.error("query '{}' takes exactly one argument, got {}{}".format(
            args.query, len(args.args),
            " (quote a multi-word phrase)" if args.query == "grep" else ""))
    arg = args.args[0]
    if args.complete and args.query not in _TRAIN_QUERIES:
        parser.error("--complete does not apply to '{}' (train queries: "
                     "{})".format(args.query, ", ".join(_TRAIN_QUERIES)))
    if args.repo is not None and args.query not in _AREA_QUERIES:
        parser.error("--repo does not apply to '{}' (area queries: {})".format(
            args.query, ", ".join(_AREA_QUERIES)))
    if args.query == "dependents" and (args.ts_from or args.ts_to):
        parser.error("--from/--to do not apply to 'dependents' "
                     "(it is time-independent)")
    try:
        if args.ts_from is not None:
            args.ts_from = manifest.check_reader_window(
                args.ts_from, args.ts_to or args.ts_from)[0]
        if args.ts_to is not None:
            args.ts_to = manifest.check_reader_window(
                args.ts_from or args.ts_to, args.ts_to)[1]
    except ValueError as exc:
        parser.error(str(exc))

    # sqlite3.connect would silently CREATE a missing file; refuse instead.
    if not os.path.isfile(args.store):
        parser.error("store {!r} does not exist (run gather.py first)".format(
            args.store))
    # Only a live --complete backfill (with a token) writes fetched anchors
    # into the store; every other query opens it read-only (#107).
    token = os.environ.get("GITHUB_TOKEN") if args.complete else None
    try:
        if token:
            conn = graphstore.open_store(args.store)
        else:
            conn = graphstore.open_store_readonly(args.store)
        project = _detect_project(conn, args.project)
        # Every repo the arguments name resolves by one rule
        # (resolve_repo_arg); an unknown or ambiguous one is a usage error
        # listing the repos the store knows, for every query alike (#113 m3).
        named = []
        if args.query in _AREA_QUERIES or args.query == "dependents":
            qual = _split_area_arg(arg)[0]
            if qual is not None:
                named.append(("area qualifier", qual))
            elif args.query == "dependents":
                named.append(("member", arg))
            if args.repo is not None:
                named.append(("--repo", args.repo))
        for name, value in named:
            if resolve_repo_arg(conn, project, value) is None:
                raise ValueError(
                    "{} {!r} names no single repo of project {!r} "
                    "(repos: {})".format(name, value, project,
                                         _store_repos(conn, project)))
        if args.query in _AREA_QUERIES:
            _area_scope(conn, project, arg, args.repo)  # --repo vs qualifier
    except sqlite3.DatabaseError as exc:
        parser.error("cannot read store {!r}: {}".format(args.store, exc))
    except ValueError as exc:
        parser.error(str(exc))

    # Build the (optional) backfill seam. --complete needs a token; without one
    # we stay offline/honest-only rather than failing.
    backfill = None
    if args.complete:
        if token:
            fetch = gather.make_backfill_fetcher(token)
            backfill = lambda c, mid: gather.backfill(c, mid, fetch=fetch)  # noqa: E731
        else:
            sys.stderr.write("spotlight: --complete needs GITHUB_TOKEN; "
                             "running offline (honest-only)\n")
    cb = args.complete_budget

    if args.query == "person":
        res = person_impact(conn, project, arg,
                            ts_from=args.ts_from, ts_to=args.ts_to,
                            backfill=backfill, complete_budget=cb)
    elif args.query == "symbol":
        res = pattern_evolution(conn, project, arg,
                                ts_from=args.ts_from, ts_to=args.ts_to)
    elif args.query == "subsystem":
        res = subsystem_split(conn, project, arg,
                              ts_from=args.ts_from, ts_to=args.ts_to,
                              repo=args.repo,
                              backfill=backfill, complete_budget=cb)
    elif args.query == "grep":
        res = text_mining(conn, project, arg,
                          ts_from=args.ts_from, ts_to=args.ts_to,
                          backfill=backfill, complete_budget=cb)
    elif args.query == "module":
        res = slice_module(conn, project, arg,
                           ts_from=args.ts_from, ts_to=args.ts_to,
                           repo=args.repo,
                           backfill=backfill, complete_budget=cb)
    elif args.query == "dependents":
        if _split_area_arg(arg)[0] is not None:   # repo::area
            res = area_dependents(conn, project, arg)
        else:
            res = member_dependents(conn, project, arg)
    conn.close()

    if args.md:
        print(render_md(res))
    else:
        print(json.dumps(res, sort_keys=True, indent=2))
    return 0  # needs_gather / fts_unavailable are valid answers (exit 0)


if __name__ == "__main__":
    sys.exit(main())
