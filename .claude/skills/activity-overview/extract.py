"""extract.py — materialize a bundle view out of the journey-graph store.

This is the reader counterpart of `gather.fold_bundle`: where fold *writes* a raw
bundle into the SQLite property graph by identity, `extract` *reads* a window back
out and reconstructs the raw bundle (`meta`, `prs`, `issues`, `commits`,
`code_events`, `milestones`, `releases`) PLUS the two derived projections fold
already materialized into nodes — `artifacts` (the per-file lifecycle ledger) and
`people` (the per-login modules/areas). Slice 7b-2 moved those two out of
`link.enrich` and into the store: extract READS the stored nodes (it does NOT
re-derive via build_artifacts; a person's modules/areas are the one exception,
taken from the view's own records since issue #88), and `link.enrich`
shrank to the remaining window projections (trains/buckets/timeline/
feature_deltas/modules/forecast/code-area attribution).

Materialization approach (slice 7a part 1)
------------------------------------------
1. Range-query the in-window social + code nodes (graphstore.range_query) — these
   are conceptually `in_window: true` and are what activity counts reflect.
2. Seed a bounded spine traversal (graphstore.traverse_spine) from every in-window
   social node's train anchor to find out-of-window spine nodes. `missing` spine
   ids (referenced but not yet stored — e.g. an out-of-window issue a PR closes)
   are WARNED about, or fetched through the `backfill` seam (slice 7c). Which
   out-of-window records enter the arrays as train context is decided in step 3
   (issues a view PR closes / cross-references), not by this traversal.
3. Materialize the raw arrays from the node `data` blobs of the records that
   belong to THIS window (issue #85 — a store holding several windows must not
   leak the others into the view). The rule mirrors what gather collects for a
   window, applied to each record's latest stored state:
   - a PR merged in the window, closed (unmerged) in the window, or open and
     created by the window's end;
   - an issue closed or updated in the window, open and created by the
     window's end, or closed / cross-referenced by one of the view's PRs
     (train context — the issues gather fetches for a window's PRs);
   - a commit dated in the window; reviews / lifecycle events of a view
     pr/issue; releases published in the window;
   - any node whose own `ts` is in the window (range_query) and any undated
     node (it cannot be placed in a window; un-dated commits, degenerate
     fixtures).
   Milestones, sprints and the per-repo singletons are not windowed (current
   structure, latest gather wins). On a store holding only the viewed window
   this selects every stored record, so single-window views are unchanged; a
   view spanning several gathered windows is their union.
4. Reconstruct `code_events` from the ledger in insertion (rowid) order via
   `graphstore.repo_code_events`, which equals the original source order,
   keeping the rows of the view's commits (a row whose commit is not stored
   is kept when its own date is in the window). The
   SAME ledger also carries the symbol-granular rows fold persisted (keyed by the
   symbol artifact id `<path>#<lang>:<subkind>:<name>`, with before/after); those
   are split OUT of the file-level `code_events` array and reconstructed into
   `symbol_events` (see `_materialize_symbol_events`) so the self-sourced raw
   bundle is COMPLETE — `derive.build_artifacts` re-derives the symbol/comment
   artifacts exactly, and the self-sourced `validate` no_drift passes.
5. Reconstruct `meta` (owner/repo/from/to, + clone_sha if recorded) and stamp
   the view's window (issue #75): `period` = the requested {from, to}, so
   `link.compute_buckets` limits shipped/rejected/in_flight to this window even
   when the store holds other windows' items; `ref_date` = the explicit
   override, else the value fold stored for exactly this gathered window
   (gather `--ref-date`, default its `to`), else this view's `to`; and, when
   the repo has a gathered window ending after this view (issue #85),
   `state_as_of` = that latest `to` (records are stored latest-wins).
6. `artifacts`: the stored artifact nodes with each lifecycle limited to the
   view's events (an artifact with none is left out) and status /
   replaced_by / move fields as of the window's end; `people`: the stored
   project people with a contribution in this view (this member, this
   window), their modules / areas re-derived from the view's records (issue
   #88). See `_artifacts_as_of` / `_view_people`.

Deterministic ordering matches the golden bundles: prs/issues by number, commits
by sha, milestones by number, releases by tag/name, code_events by source order.

Per-repo singleton facts (slice 7a part 2)
------------------------------------------
`workflow_stats`, `code_graph`, `code_owners`, and `label_taxonomy` are per-repo
singleton dicts (aggregate facts, not arrays). fold_bundle persists each whole
dict as a `structure` node under a well-known local id (`workflowstats` /
`codegraph` / `codeowners` / `labeltaxonomy`) with NULL ts, identity-keyed and
idempotent. extract reconstructs each by reading that node's `data` back, and
emits the key ONLY when the node exists (extract never fabricates an empty key —
fold only wrote the node when the source value was present and non-empty).

symbol_events (slice 7b — the self-sourced no_drift fix)
--------------------------------------------------------
`derive.build_artifacts` reads `bundle["symbol_events"]` to produce the
symbol/comment artifacts (`<path>#<lang>:<subkind>:<name>`). fold_bundle persists
each symbol_event into the `code_events` ledger keyed by that symbol artifact id
(non-NULL before/after; the file-level rows are `<path>`). extract now
reconstructs `symbol_events` from those symbol-keyed rows so the self-sourced raw
bundle is complete and build_artifacts re-derives the stored symbol artifacts
exactly. The key is always emitted (an empty `[]` for repos with no symbol rows,
e.g. the existing goldens). See `_materialize_symbol_events`.
"""

import sys

import graphstore
import derive
import complete

# Keys this reader materializes from the store: the raw substrate plus the two
# derived projections fold persists into nodes (artifacts, people).
_RAW_KEYS = ("meta", "prs", "issues", "commits", "code_events", "symbol_events",
             "milestones", "releases", "workflow_stats", "code_graph",
             "code_owners", "label_taxonomy", "artifacts", "people")

# Reverse of derive._SYMBOL_CHANGE_TO_EVENT: the ledger stored the MAPPED event
# (not the raw symbol `change`). build_artifacts only uses `change` to recompute
# that same event, so any `change` that maps back to the stored `event` round-trips
# the artifact lifecycle exactly. The forward map is injective today, so this is a
# clean inverse; if it were ever non-injective, a representative change suffices.
_EVENT_TO_SYMBOL_CHANGE = {
    event: change for change, event in derive._SYMBOL_CHANGE_TO_EVENT.items()}

# Per-repo singleton facts: raw bundle key -> well-known structure-node local id.
_SINGLETON_FACTS = (
    ("workflow_stats", "workflowstats"),
    ("code_graph", "codegraph"),
    ("code_owners", "codeowners"),
    ("label_taxonomy", "labeltaxonomy"),
)


def _local(node_id):
    return graphstore.parse_id(node_id)["local"]


def _full_local(node_id, project, repo):
    """The node's local id with ONLY the `{project}/{repo}#` scope prefix stripped
    (graphstore.local_id). Preserves any internal `#` — so symbol/comment
    artifact ids `<path>#<lang>:<subkind>:<name>` round-trip intact."""
    return graphstore.local_id(node_id, project, repo)


def _is_commit_node(node):
    """True for a real commit `code` node, False for an artifact one. Commits are
    keyed by a bare `<sha>` local id and their record carries `sha`; artifact
    nodes use the `art:`/`<path>#…` id form and have no `sha`. Lets extract keep
    reconstructing only commits while artifact `code` nodes coexist in the store."""
    if "sha" not in node["data"]:
        return False
    local = _local(node["id"])
    return not local.startswith("art:") and "#" not in local


def _is_artifact_node(node):
    """True for an artifact `code` node (the non-commit ones `_is_commit_node`
    excludes): file artifacts keyed `art:<path>`, symbol/comment artifacts keyed
    `<path>#<lang>:<subkind>:<name>`. extract materializes the raw `artifacts`
    dict by reading these nodes' data blobs (it does NOT re-run build_artifacts)."""
    return not _is_commit_node(node)


def _train_anchor(node):
    """Train-anchor id for an in-window social node — the spine seed. A PR
    anchors on itself; an issue anchors on itself. (The anchor is just the seed
    we hand traverse_spine; the train is the component reachable from it.)"""
    return node["id"]


def extract(conn, project, repo, ts_from, ts_to, max_depth=6, warn=None,
            backfill=None, backfill_budget=50, ref_date=None):
    """Materialize a rev-13 RAW bundle view for project/repo over [ts_from, ts_to].

    Returns a bundle dict carrying ONLY raw keys; run `link.enrich` on it to add
    the derived projections. `warn` is an optional callable(str) for the spine
    `missing` notice (defaults to stderr).

    `backfill` is the slice-7c seam: an optional callable `backfill(conn, id)`
    (production passes `lambda c, i: gather.backfill(c, i, fetch=...)`). When
    provided and the spine traversal yields `missing` ids, extract asks it to
    fetch each one — up to `backfill_budget` per call — so a cross-window train
    (e.g. a windowed PR closing an out-of-window issue) reads COMPLETE: the
    backfilled node lands in the store and is pulled in below as out-of-window
    CONTEXT (in_window: false), never counted as in-window activity. Once the
    budget is exhausted extract WARNS and stops (it never fetches unboundedly).
    When `backfill` is None (the default) behavior is EXACTLY as before — the
    `missing` ids are only warned about — so existing callers/characterization
    are byte-identical.

    `ref_date` (optional) overrides the reference date stamped into
    `meta.ref_date` (view.py / digest.py `--ref-date`); see `_materialize_meta`.
    """
    if warn is None:
        warn = lambda msg: sys.stderr.write(msg + "\n")  # noqa: E731

    # 1. In-window social + code nodes (conceptually in_window: true). These drive
    #    activity counts and seed the spine.
    in_window = graphstore.range_query(conn, project, [repo], ts_from, ts_to)
    in_window_ids = {n["id"] for n in in_window}

    # 2. Seed trains from in-window social ANCHORS (pr/issue), then bounded spine
    #    traversal to surface out-of-window context (in_window: false). Phase 10's
    #    review/event social leaves are reached FROM their parent anchor — they
    #    must not seed, or a late in-window review on an out-of-window PR would
    #    pull that PR in (a behaviour change + backfill-budget cost vs pre-P10).
    seeds = [_train_anchor(n) for n in in_window if n["node_class"] == "social"
             and _local(n["id"]).startswith(("pr-", "issue-"))]
    spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth,
                                      skip_dead=True)

    # 2b. Complete the train via the shared orchestrator (slice 7c, now 8d).
    #     window=None preserves 7c semantics: extract fills the whole reachable
    #     spine (level-0 + transitive) within budget, exactly as the inline loop
    #     did. backfill=None (the default) is byte-identical warn-only behaviour
    #     (every missing id becomes a not_gathered gap and is warned, nothing is
    #     fetched). After fetches, re-read the spine so the materialization below
    #     sees the backfilled nodes.
    # warn=None: extract emits its own budget/context warnings from result["gaps"]
    # below, so letting complete_train also warn would duplicate them.
    result = complete.complete_train(
        conn, spine["reached"], spine["missing"], window=None,
        backfill=backfill, budget=backfill_budget)
    if backfill is not None:
        spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth,
                                          skip_dead=True)

    budget_gaps = [g["id"] for g in result["gaps"] if g["reason"] == "budget"]
    real_gaps = [g["id"] for g in result["gaps"]]
    if budget_gaps:
        warn("extract: backfill budget ({}) exhausted; {} spine context "
             "node(s) left un-fetched: {}".format(
                 backfill_budget, len(budget_gaps), ", ".join(sorted(budget_gaps))))
    if real_gaps:
        warn("extract: {} spine context node(s) referenced but not stored: "
             "{}".format(len(real_gaps), ", ".join(sorted(real_gaps))))
    # 3+4. Materialize the raw arrays: the repo's stored nodes + ledger rows
    #      that belong to THIS window (issue #85; see `_view_socials`).
    branch_problems = []
    bundle = {"meta": _materialize_meta(conn, project, repo, ts_from, ts_to,
                                        ref_date=ref_date,
                                        problems=branch_problems)}

    socials = graphstore.repo_nodes(conn, project, repo, node_class="social")
    codes = graphstore.repo_nodes(conn, project, repo, node_class="code")
    commit_nodes = [n for n in codes if _is_commit_node(n)]
    view_socials = _view_socials(conn, project, repo, socials, in_window_ids,
                                 ts_from, ts_to)
    view_commits = [n for n in commit_nodes
                    if not n["ts"] or n["id"] in in_window_ids]
    bundle["prs"] = _by(
        [n["data"] for n in view_socials if _local(n["id"]).startswith("pr-")],
        key=lambda d: d.get("number"))
    # Unknown mainline (issue #84): link then counts EVERY merged PR as merged
    # into it (the pre-#84 behaviour). Say so whenever that decides anything.
    if branch_problems and any(p.get("merged") for p in bundle["prs"]):
        warn("extract: base branch unknown for {}/{} [{} .. {}]: {}; every "
             "merged PR is counted as merged into the mainline (stacked / "
             "release-branch merges are not told apart)".format(
                 project, repo, ts_from, ts_to, branch_problems[0]))
    bundle["issues"] = _by(
        [n["data"] for n in view_socials
         if _local(n["id"]).startswith("issue-")],
        key=lambda d: d.get("number"))

    # Phase 10 slice 1: re-attach the review/lifecycle social nodes onto their
    # parent pr/issue records (the inverse of fold's first-class node emission) —
    # the RAW substrate. The derived review_rounds/reopen_count are computed in
    # link.enrich (with forecast/modules), not here, so extract stays raw-only.
    # Done ONLY when such nodes exist so PRs/issues with none stay byte-identical.
    # A review/event follows its parent: it is attached only to a pr/issue in
    # this view (orphans are skipped), whatever its own timestamp.
    _attach_reviews_and_lifecycle(socials, bundle["prs"], bundle["issues"])

    # Phase 11 slice 1: surface the store's `blocks` issue->issue edges as resolved
    # numbers (issue["blocking"] / issue["blocked_by"]) for the report + blocker_graph.
    # The raw `blocks` ref-list (fold-consumed) is left intact. Omit-when-empty.
    _attach_blocks(conn, project, repo, bundle["issues"])

    # `code` holds both commits (local id = bare <sha>) and artifact nodes
    # (local id `art:<path>` for files, `<path>#<lang>:<subkind>:<name>` for
    # symbols). Commits reconstruct the raw `commits` array; the artifact nodes
    # (everything else) reconstruct the `artifacts` projection below.
    bundle["commits"] = _by(
        [n["data"] for n in view_commits],
        key=lambda d: str(d.get("sha")))

    # code_events / symbol_events share one ledger: the file-level rows (`<path>`)
    # reconstruct the raw `code_events` array; the symbol-keyed rows
    # (`<path>#<lang>:<subkind>:<name>`, before/after) reconstruct `symbol_events`.
    # Both readers split the ledger by that form so a symbol row is NEVER
    # double-counted into code_events (and vice-versa). Only the rows of this
    # view's commits are kept (`in_view`); a row whose commit is not stored at
    # all is kept when its own date is in the window (or it has none).
    in_view = _event_filter(
        {str(n["data"].get("sha")) for n in view_commits},
        {str(n["data"].get("sha")) for n in commit_nodes}, ts_from, ts_to)
    ledger = graphstore.repo_code_events(conn, project, repo)
    bundle["code_events"] = _materialize_code_events(
        conn, project, repo, rows=ledger, keep=in_view)
    bundle["symbol_events"] = _materialize_symbol_events(
        conn, project, repo, rows=ledger, keep=in_view)

    # artifacts: the per-file lifecycle ledger fold persisted as artifact `code`
    # nodes (the non-commit ones). Read the stored data blobs back into the dict
    # keyed by the artifact's local id (`art:<path>` / `<path>#…`) — the same key
    # build_artifacts uses — so enrich/render consume it unchanged. Materialized
    # from the store, NOT re-derived.
    #
    # ORDER MATTERS: build_timeline iterates `artifacts.values()` and its stable
    # ts-sort lets the iteration order break ties between same-(ts,url) lifecycle
    # events (e.g. a rename's remove-old + add-new from one commit). repo_nodes
    # returns nodes by (ts, id), but build_artifacts inserts artifacts in
    # code_events SOURCE order, so we restore that order here — first appearance of
    # each artifact path in the (already source-ordered) code_events — to reproduce
    # the same timeline byte-for-byte. Artifacts with no code_event (none today)
    # fall to the end, ordered by id.
    #
    # Window scope (issue #85): each artifact's `lifecycle` keeps only the events
    # of this view (`in_view`) and an artifact with none is left out, so
    # timeline / feature_deltas / modules see this window's code history only.
    # `status` / `replaced_by` / `identity_from` are as of the window's END: the
    # stored (cumulative) node when the ledger holds nothing dated after the
    # window, else the same projection over the ledger up to the window's end
    # (`_artifacts_as_of`).
    arts = _artifacts_as_of(conn, project, repo, codes, ledger, ts_to)
    arts = _window_lifecycles(arts, in_view)
    bundle["artifacts"] = _order_artifacts(arts, bundle["code_events"],
                                           derive.commit_times(bundle))

    # people: the project-scoped person `structure` nodes fold persisted (repo
    # sentinel "*", local id `person-<login>`), read back into the dict keyed by
    # login (the redundant stored `login` field dropped). Member scope (issue
    # #88) and window scope (issue #85): only the logins with a contribution in
    # THIS view are kept, and their `modules` / `areas` are re-derived from the
    # view's records with this repo's code graph (the attribution fold applies),
    # so a person's areas name this repo's areas touched in this window. The
    # stored node (the project-wide union) is not changed; its flags (`is_bot`,
    # `login_resolved`) are kept. See `_view_people`.
    bundle["people"] = _view_people(
        _materialize_people(conn, project), bundle,
        _materialize_singleton(conn, project, repo, "codegraph"))

    structure = graphstore.repo_nodes(conn, project, repo, node_class="structure")
    bundle["milestones"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("milestone-")],
        key=lambda d: (d.get("number") is None, d.get("number"),
                       d.get("title") or ""))
    # Releases published in the window (gather's own rule), or undated.
    bundle["releases"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("release-")
         and (not n["ts"] or graphstore.ts_in_window(n["ts"], ts_from, ts_to))],
        key=lambda d: d.get("tag_name") or d.get("name") or "")

    # Phase 12 slice 1: sprints materialize from the `sprint-<id>` structure nodes
    # keyed by iteration id (the inverse of fold's emission), mirroring milestones.
    # The pr/issue `board_status` + `iteration` ride the social node data blob, so
    # they round-trip with the prs/issues arrays above — no extra work. Omit the
    # `sprints` key entirely when no board was folded (byte-identical to before).
    sprints = {_local(n["id"])[len("sprint-"):]: n["data"] for n in structure
               if _local(n["id"]).startswith("sprint-")}
    if sprints:
        bundle["sprints"] = sprints

    # Per-repo singleton facts: emit each key only when its node was stored.
    for key, local in _SINGLETON_FACTS:
        value = _materialize_singleton(conn, project, repo, local)
        if value is not None:
            bundle[key] = value

    return bundle


def _attach_reviews_and_lifecycle(socials, prs, issues):
    """Re-attach `review`/`event` social nodes onto their parent pr/issue records.

    The inverse of `gather.fold_bundle`'s first-class node emission: a review
    node `review-<pr>-<id>` belongs on PR `<pr>`'s `reviews` list; an event node
    `event-pr-<n>-<id>` / `event-issue-<n>-<id>` belongs on that pr/issue's
    `lifecycle` list. Both lists are ordered by the node's natural timestamp
    (submitted_at / created_at) then id, so a re-extract is byte-stable. Mutates
    the pr/issue dicts in place; only sets a key when >=1 child node exists (so
    records with none stay byte-identical). The node data blobs already carry the
    exact normalized shape fold persisted, so they round-trip as-is."""
    pr_by_num = {p.get("number"): p for p in prs}
    issue_by_num = {i.get("number"): i for i in issues}
    reviews_by_pr = {}        # pr number -> [review data]
    lifecycle_by_parent = {}  # ("pr"|"issue", number) -> [event data]
    for n in socials:
        local = _local(n["id"])
        if local.startswith("review-"):
            rest = local[len("review-"):]
            prnum, _, _eid = rest.rpartition("-")
            try:
                key = int(prnum)
            except ValueError:
                continue
            reviews_by_pr.setdefault(key, []).append(n["data"])
        elif local.startswith("event-pr-") or local.startswith("event-issue-"):
            kind = "pr" if local.startswith("event-pr-") else "issue"
            rest = local[len("event-{}-".format(kind)):]
            num, _, _eid = rest.rpartition("-")
            try:
                key = int(num)
            except ValueError:
                continue
            lifecycle_by_parent.setdefault((kind, key), []).append(n["data"])

    def _sorted(items, ts_key):
        return sorted(items, key=lambda d: (d.get(ts_key) or "", d.get("id") or 0))

    for prnum, revs in reviews_by_pr.items():
        pr = pr_by_num.get(prnum)
        if pr is not None:
            pr["reviews"] = _sorted(revs, "submitted_at")
    for (kind, num), evs in lifecycle_by_parent.items():
        parent = (pr_by_num if kind == "pr" else issue_by_num).get(num)
        if parent is not None:
            parent["lifecycle"] = _sorted(evs, "created_at")


def _order_artifacts(arts, code_events, times=None):
    """Order the artifact dict to match build_artifacts' INSERTION order so
    order-sensitive consumers (build_timeline's same-(ts,url) tie-break) reproduce
    byte-for-byte. build_artifacts, per code_event in source order, ensures the
    `path` artifact first and — on a rename — the `old_path` (replaced) artifact
    second; first occurrence wins. We replay that exact first-ensure sequence as a
    monotonic ordinal, so a rename whose old/new ids sort against insertion order
    (e.g. a->z, which must stay [new, old]) is not flipped by a lexical tie-break.
    Artifacts never named by a code_event (symbol/comment nodes) fall last, ordered
    by id. The replay walks the events in build_artifacts' own order
    (derive.chronological), so a multi-window store reads back the same order
    whichever window was folded first. Pure; does not mutate the records."""
    order = {}

    def _see(path):
        if path:
            order.setdefault("art:" + path, len(order))

    for ev in derive.chronological(code_events, times):
        change = ev.get("change")
        if change in ("rename", "copy") and ev.get("old_path"):
            _see(ev.get("path"))            # new-path artifact: ensured first
            if change == "rename":
                _see(ev.get("old_path"))    # then the replaced old-path artifact
        else:
            _see(ev.get("path"))
    fallback = len(order)

    def rank(aid):
        return (order.get(aid, fallback), aid)

    return {aid: arts[aid] for aid in sorted(arts, key=rank)}


# ---------------------------------------------------------------------------
# Window scope (issue #85): which stored records belong to a window's view.
#
# A store grows window by window, so a view must not read the repo's whole
# node set. The rule mirrors what `gather` collects for a window, applied to
# each record's latest stored state, so a view of one gathered window on an
# accumulated store equals the view of a store holding only that window (up
# to records a later gather refreshed), and a view spanning several windows is
# their union. Every window test is `graphstore.ts_in_window` (#52).
# ---------------------------------------------------------------------------

def _open_by(data, ts_to):
    """An item whose stored state is open and that was created by the window's
    end (or has no created_at). Its state is the latest gather's, so it was
    open at the end of every window since its creation."""
    if data.get("state") != "open" or data.get("merged"):
        return False
    created = data.get("created_at")
    return not created or graphstore.ts_in_window(created, None, ts_to)


def _pr_in_window(data, ts_from, ts_to):
    """gather's PR rule: merged in the window, closed (unmerged) in the
    window, or open (here: open and created by the window's end)."""
    if data.get("merged"):
        return graphstore.ts_in_window(data.get("merged_at"), ts_from, ts_to)
    if data.get("state") == "closed":
        return graphstore.ts_in_window(data.get("closed_at"), ts_from, ts_to)
    return _open_by(data, ts_to)


def _issue_in_window(data, ts_from, ts_to):
    """gather's issue rule (minus train context, see `_view_socials`): closed
    or updated in the window, or open and created by the window's end."""
    return (graphstore.ts_in_window(data.get("closed_at"), ts_from, ts_to)
            or graphstore.ts_in_window(data.get("updated_at"), ts_from, ts_to)
            or _open_by(data, ts_to))


def _view_socials(conn, project, repo, socials, in_window_ids, ts_from, ts_to):
    """The pr/issue social nodes of one window's view, in `socials` order.

    A node is in the view when it is undated (it cannot be placed in any
    window), its own `ts` is in the window (`in_window_ids`, from
    range_query), or its record passes `_pr_in_window` / `_issue_in_window`.
    An issue is also in the view as TRAIN CONTEXT when a PR of the view
    closes or cross-references it (a `closes` / `cross_ref` edge), which is
    exactly the set gather fetches for a window's PRs. PRs are never pulled in
    as context: link counts every PR in the arrays (trains, people, timeline),
    so a PR from another window would be counted as this window's activity.
    Review and lifecycle-event nodes are not returned; they follow their
    parent (see `_attach_reviews_and_lifecycle`)."""
    def dated_in(n):
        return not n["ts"] or n["id"] in in_window_ids

    prs = {n["id"] for n in socials if _local(n["id"]).startswith("pr-")
           and (dated_in(n) or _pr_in_window(n["data"], ts_from, ts_to))}
    issue_prefix = graphstore.qualify_id(project, repo, "issue-")
    context = set()
    for edge_type in ("closes", "cross_ref"):
        for e in graphstore.edges_by_type(conn, edge_type, project):
            if e["src_id"] in prs and e["dst_id"].startswith(issue_prefix):
                context.add(e["dst_id"])
    out = []
    for n in socials:
        local = _local(n["id"])
        if local.startswith("pr-"):
            if n["id"] in prs:
                out.append(n)
        elif local.startswith("issue-"):
            if (n["id"] in context or dated_in(n)
                    or _issue_in_window(n["data"], ts_from, ts_to)):
                out.append(n)
    return out


def _event_filter(view_shas, stored_shas, ts_from, ts_to):
    """keep(commit_sha, date) for ledger rows and artifact lifecycle entries:
    the event of a commit in the view; never that of a stored commit outside
    it; for a commit the store does not hold, the event's own date decides
    (an undated one is kept)."""
    def keep(sha, date):
        if sha is not None and str(sha) in view_shas:
            return True
        if sha is not None and str(sha) in stored_shas:
            return False
        return not date or graphstore.ts_in_window(date, ts_from, ts_to)
    return keep


# Artifact fields that depend on the WHOLE lifecycle (last event / moves), so
# they differ between "as of the window's end" and "as of the latest gather".
_AS_OF_FIELDS = ("status", "replaced_by")
_MOVE_FIELDS = ("identity_from", "move_confidence", "move_basis")


def _artifacts_as_of(conn, project, repo, codes, ledger, ts_to):
    """The repo's artifact records {local id: data} with status as of the end
    of the window.

    The stored nodes are cumulative (#76): status / replaced_by / move fields
    are those of the latest folded event. When the ledger holds no event dated
    after `ts_to` that IS the status at the window's end, and the stored nodes
    are returned untouched. Otherwise the same projection fold writes
    (derive.artifact_projection, with the recorded moves) is run over the
    ledger rows dated up to `ts_to`, and its status / replaced_by / move fields
    are laid over copies of the stored records (whose lifecycle entries, refs
    and code_area are kept as stored)."""
    stored = {_full_local(n["id"], project, repo): n["data"]
              for n in codes if _is_artifact_node(n)}
    later = [r for r in ledger if r["date"]
             and not graphstore.ts_in_window(r["date"], None, ts_to)]
    if not ts_to or not later:
        return stored

    def upto(_sha, date):
        return not date or graphstore.ts_in_window(date, None, ts_to)

    as_of = derive.artifact_projection(
        {"meta": {"owner": project, "repo": repo},
         "code_events": _materialize_code_events(
             conn, project, repo, rows=ledger, keep=upto),
         "symbol_events": _materialize_symbol_events(
             conn, project, repo, rows=ledger, keep=upto),
         "commits": commit_times(conn, project, repo)},
        recorded_moves(conn, project, repo))
    out = {}
    for aid, data in stored.items():
        then = as_of.get(aid)
        if then is None:
            out[aid] = data   # no event by the window's end: trimmed out later
            continue
        rec = dict(data)
        for key in _AS_OF_FIELDS:
            if key in then:
                rec[key] = then[key]
        for key in _MOVE_FIELDS:
            if key in then:
                rec[key] = then[key]
            else:
                rec.pop(key, None)
        out[aid] = rec
    return out


def _window_lifecycles(arts, keep):
    """Each artifact with its lifecycle limited to the view's events (`keep`);
    an artifact left with none is dropped. An artifact with no lifecycle at all
    cannot be placed in a window and is kept. Records whose lifecycle is
    entirely in the view are returned as-is (single-window stores read back
    byte-identical)."""
    out = {}
    for aid, art in arts.items():
        lc = art.get("lifecycle") or []
        if not lc:
            out[aid] = art
            continue
        kept = [e for e in lc if keep(e.get("commit"), e.get("date"))]
        if not kept:
            continue
        out[aid] = art if len(kept) == len(lc) else {**art, "lifecycle": kept}
    return out


def _contribution_records(view):
    """The login- and file-bearing fields of the view's PR / issue / commit
    records, null lists read as empty (link reads them so, #82), with each
    commit's `pr` resolved from its message on the copy — exactly the input
    fold hands the shared enumerator (it runs attach_commit_prs on a copy)."""
    import gather  # deferred: gather imports extract

    def lst(value):
        return list(value or [])
    prs = [{"number": p.get("number"), "author": p.get("author"),
            "merged_by": p.get("merged_by"), "reviewers": lst(p.get("reviewers")),
            "comments_list": lst(p.get("comments_list")),
            "review_comments": lst(p.get("review_comments"))}
           for p in view.get("prs") or []]
    issues = [{"author": i.get("author"),
               "comments_list": lst(i.get("comments_list"))}
              for i in view.get("issues") or []]
    commits = [{"sha": c.get("sha"), "author": c.get("author"),
                "login_resolved": c.get("login_resolved"),
                "message": c.get("message") or "", "files": lst(c.get("files"))}
               for c in view.get("commits") or []]
    gather.attach_commit_prs(commits)
    return {"prs": prs, "issues": issues, "commits": commits, "people": {}}


def _view_people(people, view, code_graph):
    """The stored project `people` limited to this view (issues #85, #88).

    A login is kept when it has a contribution edge in the view's records
    (PR author / merger / reviewer / commenter, issue author / commenter,
    commit author: `derive.enumerate_participants`, the enumerator fold uses
    to write person nodes). Another member's people, and people who worked in
    this repo only in another window, are left out. Each kept record's
    `modules` / `areas` are replaced by what that enumerator derives from the
    view's records with `code_graph` (this repo's stored code graph): the
    areas of the files in the person's commits here, plus the areas of the
    view PRs they reviewed. Every other stored field (`is_bot`,
    `login_resolved`) is kept as stored. On a one-window, one-repo store the
    view holds every stored record, so this equals the stored people."""
    derived = derive.enumerate_participants(
        _contribution_records(view), derive.area_index(code_graph or {}))
    out = {}
    for login, rec in people.items():
        mine = derived.get(login)
        if mine is None:
            continue
        rec["modules"] = mine["modules"]
        rec["areas"] = mine["areas"]
        out[login] = rec
    return out


def _latest_gathered_to(conn, project, repo):
    """The latest `to` of this repo's gathered windows, or None."""
    try:
        windows = graphstore.get_windows(conn)
    except ValueError:
        return None
    tos = [w.get("to") for w in windows if isinstance(w, dict)
           and w.get("project") == project and w.get("repo") == repo
           and w.get("to")]
    return max(tos, key=lambda t: t[:10]) if tos else None


def _attach_blocks(conn, project, repo, issues):
    """Surface the store's `blocks` issue->issue edges (Phase 9 #21) onto the issue
    records as RESOLVED gathered-issue numbers, for the report + blocker_graph. fold
    normalized every edge to blocker->blocked, so for an issue's qualified id:
    OUTbound edges (this is the blocker) name the issues it blocks -> `issue["blocking"]`;
    INbound edges (this is blocked) name the issues blocking it -> `issue["blocked_by"]`.
    Both are sorted lists of issue NUMBERS, omit-when-empty.

    NOTE: we do NOT touch the issue's raw `blocks` field — that is the parsed
    directed-ref list ([{number, direction}]) `normalize_issue` wrote, and
    `gather.fold_bundle` re-reads it (e.g. validate's idempotency gate re-folds this
    very extracted bundle). Overwriting it would break re-fold, so the resolved view
    lives under the separate `blocking`/`blocked_by` keys. Mirrors
    `_attach_reviews_and_lifecycle`: a post-attach reading the store directly. Only
    sets a key when non-empty so issues with no dependency edges stay byte-identical."""
    # One query for all of the project's `blocks` edges, then build per-issue maps —
    # O(E+N) instead of two SQL lookups per issue. `blocks` edges are same-repo
    # (gather only parses bare `#N`), so we key on numbers scoped to THIS repo.
    prefix = "{}/{}#".format(project, repo)

    def _num(node_id):
        if not node_id.startswith(prefix):
            return None
        local = node_id[len(prefix):]
        if not local.startswith("issue-"):
            return None
        try:
            return int(local[len("issue-"):])
        except ValueError:
            return None

    blocking, blocked_by = {}, {}  # number -> set(numbers)
    for e in graphstore.edges_by_type(conn, "blocks", project):
        s, d = _num(e["src_id"]), _num(e["dst_id"])
        if s is None or d is None:
            continue
        blocking.setdefault(s, set()).add(d)     # s blocks d
        blocked_by.setdefault(d, set()).add(s)
    for iss in issues:
        number = iss.get("number")
        if number is None:
            continue
        if blocking.get(number):
            iss["blocking"] = sorted(blocking[number])
        if blocked_by.get(number):
            iss["blocked_by"] = sorted(blocked_by[number])


def _materialize_people(conn, project):
    """Reconstruct the `people` dict {login: {modules, areas, ...}} from the
    project-scoped person `structure` nodes fold persisted (repo sentinel "*",
    local id `person-<login>`). The stored blob carries a redundant `login`
    field (fold wrote `{"login": login, **rec}`); pop it so the per-login record
    matches attribute_people_areas' output exactly. Read, never re-derived."""
    people = {}
    for node in graphstore.repo_nodes(conn, project, "*", node_class="structure"):
        if not _local(node["id"]).startswith("person-"):
            continue
        rec = dict(node["data"])
        login = rec.pop("login", None)
        if login:
            people[login] = rec
    return people


def _materialize_singleton(conn, project, repo, local):
    """Read back a per-repo singleton fact persisted as a structure node under a
    well-known local id (workflow_stats/code_graph/code_owners/label_taxonomy).
    Returns the stored value, or None if the node was never written (so extract
    does not fabricate an empty key for repos that had no such fact)."""
    node = graphstore.get_node(conn, graphstore.qualify_id(project, repo, local))
    return node["data"] if node is not None else None


def _by(records, key):
    """Sort records deterministically by `key`; stable and idempotent."""
    return sorted(records, key=key)


def _overlaps(window, ts_from, ts_to):
    """Does a gathered window {from, to} share at least one day with the view
    window [ts_from, ts_to]? Compared on the date prefix; a None bound on the
    view side is open."""
    w_from, w_to = (window.get("from") or "")[:10], (window.get("to") or "")[:10]
    if ts_to and w_from and w_from > ts_to[:10]:
        return False
    if ts_from and w_to and w_to < ts_from[:10]:
        return False
    return True


def _resolve_branches(conn, project, repo, ts_from, ts_to):
    """The view's mainline (issue #84): (base_branch, branches, problem).

    - The value fold stored for exactly this (project, repo, from, to) window.
    - Otherwise (a roll-up or sub-window view) the gathered windows of this
      repo that overlap the view: their recorded base branch when they agree.
    - Otherwise None, with `problem` saying why: the windows disagree (e.g. the
      default branch was renamed), gather could not determine it, or nothing
      is stored (a store folded before #84). Never a guessed `main`.
    `problem` is None when a base branch was found."""
    exact = graphstore.get_branches(conn, project, repo, ts_from, ts_to)
    if exact is not None:
        entries = [exact]
    else:
        windows = sorted(
            (w for w in graphstore.get_windows(conn)
             if w.get("project") == project and w.get("repo") == repo
             and _overlaps(w, ts_from, ts_to)),
            key=lambda w: (w.get("from") or "", w.get("to") or ""))
        entries = [e for e in (graphstore.get_branches(
            conn, project, repo, w.get("from"), w.get("to")) for w in windows)
            if e is not None]
    known = sorted({e["base_branch"] for e in entries if e.get("base_branch")})
    if len(known) == 1:
        base = known[0]
        branches = [base]
        for e in entries:
            if e.get("base_branch") != base:
                continue
            for b in e.get("branches") or []:
                if b not in branches:
                    branches.append(b)
        return base, branches, None
    if len(known) > 1:
        problem = ("the gathered windows it spans recorded different base "
                   "branches ({}); view each window on its own".format(
                       ", ".join(known)))
    elif entries:
        problem = ("gather could not determine it (no `--branches`, no clone "
                   "default branch, no API answer); re-gather with `--branches`")
    else:
        problem = ("no base branch is stored for this window (store folded "
                   "before issue #84); re-gather it")
    return None, [], problem


def _materialize_meta(conn, project, repo, ts_from, ts_to, ref_date=None,
                      problems=None):
    """Reconstruct raw `meta`: owner/repo/window (+clone_sha if recorded), plus
    the view's window (issue #75):

    - `period` = {from, to} exactly as requested: the bounds the in-window
      range query uses, in the shape gather's own bundle carries, so
      `link.compute_buckets` / `build_forecast` window this view.
    - `ref_date` = `ref_date` if given, else the value fold stored for exactly
      this (project, repo, from, to) window, else `ts_to`.

    and the analyzed mainline (issue #84): `base_branch` (None when unknown,
    see `_resolve_branches`) and `branches`, so `link` can tell a merge into
    the mainline from a stacked / release-branch merge. When it is unknown
    the reason is appended to `problems` (if a list is passed).
    """
    meta = {"owner": project, "repo": repo, "from": ts_from, "to": ts_to}
    clone_sha = graphstore.get_clone_sha(conn, project, repo)
    if clone_sha:
        meta["clone_sha"] = clone_sha
    meta["base_branch"], meta["branches"], problem = _resolve_branches(
        conn, project, repo, ts_from, ts_to)
    if problem and problems is not None:
        problems.append(problem)
    meta["period"] = {"from": ts_from, "to": ts_to}
    meta["ref_date"] = (
        ref_date
        or graphstore.get_ref_date(conn, project, repo, ts_from, ts_to)
        or ts_to)
    # Issue #85: records are stored latest-wins, so a view of an earlier
    # window shows open/closed state, labels, milestones and comments as of a
    # later gather. Say so: `state_as_of` = the end of the repo's latest
    # gathered window, present only when that is after this view's end.
    latest = _latest_gathered_to(conn, project, repo)
    if latest and ts_to and not graphstore.ts_in_window(latest, None, ts_to):
        meta["state_as_of"] = latest
    return meta


def ledger_events(conn, project, repo):
    """The repo's WHOLE code-event ledger as raw `(code_events, symbol_events)`
    arrays (every folded window, ledger order). fold_bundle rebuilds artifact
    nodes from it and validate re-derives them from it (#76): one reader for the
    writer and the auditor."""
    return (_materialize_code_events(conn, project, repo),
            _materialize_symbol_events(conn, project, repo))


def recorded_moves(conn, project, repo):
    """The repo's symbol moves as fold recorded them (one match per window, as
    `replaced_by` edges): [{"from", "to", "confidence", "basis"}] with local ids,
    in edge order. Input to derive.apply_recorded_moves (#76)."""
    prefix = graphstore.qualify_id(project, repo, "")
    out = []
    for e in graphstore.edges_by_type(conn, "replaced_by", project):
        if not (e["src_id"].startswith(prefix) and e["dst_id"].startswith(prefix)):
            continue
        data = e["data"] or {}
        out.append({"from": e["src_id"][len(prefix):],
                    "to": e["dst_id"][len(prefix):],
                    "confidence": data.get("move_confidence"),
                    "basis": data.get("move_basis"),
                    "commit": data.get("move_commit")})
    return out


def commit_times(conn, project, repo):
    """The repo's stored commits that carry a full UTC committer timestamp, as
    `[{"sha", "committed_at"}]` (sorted by sha): the `commits` input
    derive.chronological needs to order lifecycle events by real commit time
    rather than the committer's local day."""
    out = []
    for n in graphstore.repo_nodes(conn, project, repo, "code"):
        data = n["data"] if isinstance(n["data"], dict) else {}
        # commit nodes only (artifact nodes carry a lifecycle, never a sha)
        if data.get("sha") and data.get("committed_at") and "lifecycle" not in data:
            out.append({"sha": data["sha"], "committed_at": data["committed_at"]})
    return sorted(out, key=lambda c: c["sha"])


def _materialize_code_events(conn, project, repo, rows=None, keep=None):
    """Reconstruct the raw `code_events` array from the file-level ledger, in
    original source order (ledger rowid order). rename/copy recover `old_path`
    from the event's `detail` (fold_bundle stored old_path there).

    `rows` (optional) are ledger rows already read by the caller; `keep` an
    optional predicate keep(commit_sha, date) -> bool limiting the result to
    one window's rows (issue #85). Both None = the WHOLE ledger."""
    out = []
    if rows is None:
        rows = graphstore.repo_code_events(conn, project, repo)
    for ev in rows:
        if keep is not None and not keep(ev["commit_sha"], ev["date"]):
            continue
        aid = ev["artifact_id"]
        # The repo-qualified remainder is the artifact's local id form: `<path>`
        # for a file event, `<path>#<lang>:<subkind>:<name>` for a symbol one.
        # Skip symbol-granular rows — they are part of the artifact substrate, not
        # this raw file-level `code_events` array. A file path MAY contain `#`
        # (`docs/C#-guide.md`); the symbol form is told apart by its
        # `<lang>:<subkind>:<name>` tail (graphstore.split_symbol_local).
        remainder = _full_local(aid, project, repo)
        if graphstore.is_symbol_local(remainder):
            continue
        rec = {
            "commit": ev["commit_sha"],
            "author": ev["author"],
            "date": ev["date"],
            "change": ev["event"],
            "path": remainder,
        }
        if ev["event"] in ("rename", "copy") and ev["detail"]:
            rec["old_path"] = ev["detail"]
        # Phase 10 slice-diffs: carry the bounded file diff back onto the
        # reconstructed code_event so a re-derived build_artifacts reproduces the same
        # lifecycle (and feature_deltas `diff`). Omit-when-empty for byte stability.
        if ev["hunk"]:
            rec["hunk"] = ev["hunk"]
        out.append(rec)
    return out


def _materialize_symbol_events(conn, project, repo, rows=None, keep=None):
    """Reconstruct the raw `symbol_events` array from the SYMBOL-granular ledger
    rows fold persisted, in original source order (ledger rowid order).

    fold_bundle wrote one ledger row per symbol_event, keyed by the symbol
    artifact's local id `<path>#<lang>:<subkind>:<name>` (mirrored from
    gather.fold_bundle), carrying before/after; file-level rows are `<path>` and
    are handled by `_materialize_code_events`. We split SYMBOL rows out by that
    form (graphstore.split_symbol_local): the remainder after the repo prefix
    ends in a `#<lang>:<subkind>:<name>` tail. A bare `<path>` remainder (file
    event — even one whose path contains `#`) has no such tail and is skipped.

    Per-row reconstruction:
      - strip the `{project}/{repo}#` prefix to recover the local id, then split
        it with graphstore.split_symbol_local -> `path`, `lang`, `subkind`,
        `name` (`#` allowed in path and name; name kept whole — may contain
        `:`/`#`/parens/commas).
      - commit/author/date/before/after come straight from the row columns.
      - `change`: the row stored the MAPPED event (derive._SYMBOL_CHANGE_TO_EVENT);
        we reverse it (`_EVENT_TO_SYMBOL_CHANGE`) to a change that recomputes the
        same event in build_artifacts, so the artifact lifecycle round-trips.
    Returns [] when the repo has no symbol rows (the goldens). Source-ordered.
    `rows` / `keep`: as for `_materialize_code_events`."""
    out = []
    if rows is None:
        rows = graphstore.repo_code_events(conn, project, repo)
    for ev in rows:
        if keep is not None and not keep(ev["commit_sha"], ev["date"]):
            continue
        aid = ev["artifact_id"]
        remainder = _full_local(aid, project, repo)
        # Symbol rows end in `#<lang>:<subkind>:<name>`. A bare `<path>` file-level
        # row (even with `#` in the path) has no such tail and belongs to
        # code_events, not here.
        split = graphstore.split_symbol_local(remainder)
        if split is None:
            continue
        path, lang, subkind, name = split
        out.append({
            "path": path,
            "lang": lang,
            "subkind": subkind,
            "name": name,
            "change": _EVENT_TO_SYMBOL_CHANGE.get(ev["event"], "change"),
            "commit": ev["commit_sha"],
            "author": ev["author"],
            "date": ev["date"],
            "before": ev["before"],
            "after": ev["after"],
        })
    return out
