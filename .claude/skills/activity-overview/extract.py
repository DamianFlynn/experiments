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
   window, applied to each record's latest stored state — or, for a report view
   (`as_of_end`, issue #97; view.py and digest.py), to the record as it was at
   the window's end (`record_as_of`). A record created after the window's end
   is in no view of it, projected or as stored:
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
   Milestones and sprints are not windowed (current structure, latest gather
   wins); the per-repo facts are the window's (see below). On a store holding only the viewed window
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
   `state_as_of` = that latest `to` (records are stored latest-wins); and, when
   a report view projected any record to the window's end (issue #97),
   `records_as_of` = {instant, prs, issues}.
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
idempotent. Those nodes are latest-wins.

Per-window facts (issue #94): the same fold records the window's own values
(`graphstore.set_window_facts`, meta `facts:` / `fact:`). `_window_facts` picks
a view's: exactly a gathered window -> its values; otherwise CI statistics
summed over the gathered windows inside the view and the structure of the
latest gathered window overlapping it, named in `meta.facts_windows`. Only a
store with no per-window facts for the repo (folded before #94) reads the
latest-wins nodes. Either way a key is emitted ONLY when a value was stored
(extract never fabricates an empty key — fold stores none for an empty value).
`newcomers` (the window's first-time contributors) is a per-window fact only:
a view of exactly a gathered window carries it as stored, any other view the
merge of the gathered windows inside it (`_merged_newcomers`).

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

import datetime
import sys

import graphstore
import derive
import complete

# The bundle/view CONTRACT version (BUNDLE.md), stamped as `meta.schema_version`
# in gather's in-memory bundle, every extract / view.py view, each digest member
# bundle and the digest project view (issue #107). It versions the JSON shape
# formatters read, and is NOT the store schema (`graphstore.SCHEMA_VERSION`,
# kept in the store's meta only). History: 1 was declared for the bundle but
# extract views carried no version and digest stamped the store's 2, while the
# shape changed incompatibly (project people areas `repo::area`, series v2,
# train `issues` / `pr_outcomes`). 3 is the first value no earlier view ever
# carried, so an old view never reads as this contract. Bump it on every
# incompatible view change and record the bump in BUNDLE.md.
VIEW_CONTRACT_VERSION = 3

# Keys this reader materializes from the store: the raw substrate plus the two
# derived projections fold persists into nodes (artifacts, people).
_RAW_KEYS = ("meta", "prs", "issues", "commits", "code_events", "symbol_events",
             "milestones", "releases", "workflow_stats", "code_graph",
             "code_owners", "label_taxonomy", "newcomers", "module_notes",
             "pr_history", "affiliations", "prior_activity", "artifacts",
             "people")

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
    """True for an artifact `code` node: file artifacts keyed `art:<path>`,
    symbol/comment artifacts keyed `<path>#<lang>:<subkind>:<name>`. Decided by
    the stored record (graphstore.is_artifact_node: a code node whose record
    is not a commit's), not by the id shape (#101). extract materializes the raw
    `artifacts` dict by reading these nodes' data blobs (it does NOT re-run
    build_artifacts)."""
    return graphstore.is_artifact_node(node)


def _train_anchor(node):
    """Train-anchor id for an in-window social node — the spine seed. A PR
    anchors on itself; an issue anchors on itself. (The anchor is just the seed
    we hand traverse_spine; the train is the component reachable from it.)"""
    return node["id"]


def extract(conn, project, repo, ts_from, ts_to, max_depth=6, warn=None,
            backfill=None, backfill_budget=50, ref_date=None, as_of_end=False):
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

    `as_of_end` (issue #97): project every PR / issue record to the end of
    the window (`record_as_of`) before the window rule picks the view's
    records, so a past window reads as it was then and not as the latest
    gather saw it. The report readers (view.py, digest.py) pass True. The
    default False gives every record as stored: the trust check (validate)
    self-sources its bundle this way, because it re-derives edges and people
    from the FULL stored records.
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
    if as_of_end:
        # Issue #97: a record projected to the window's end can be in the view
        # although its stored timestamp is not in the window (a PR open at
        # the end and merged since). It anchors a train of this view too, so
        # it seeds the traversal / completion below: what it reaches but the
        # store lacks is backfilled or warned about like any anchor's.
        pre = graphstore.repo_nodes(conn, project, repo, node_class="social")
        pre_as_of = _socials_as_of(pre, _social_leaves(pre), ts_to)
        seeded = set(seeds)
        seeds += sorted(
            n["id"] for n in _view_socials(conn, project, repo, pre,
                                           in_window_ids, ts_from, ts_to,
                                           as_of=pre_as_of)
            if n["id"] in pre_as_of and n["id"] not in seeded)
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
    # The repo's per-window facts for this view (issue #94); None on a store
    # folded before #94, which reads the latest-wins singleton nodes.
    wfacts = _window_facts(conn, project, repo, ts_from, ts_to)
    if wfacts is None:
        facts = {}
        for key, local in _SINGLETON_FACTS:
            value = _materialize_singleton(conn, project, repo, local)
            if value is not None:
                facts[key] = value
    else:
        facts = wfacts["facts"]
        if wfacts["missing"]:
            warn("extract: the store lost the stored value of {} for {}/{} "
                 "[{} .. {}]; left out of the view (re-gather the window)".format(
                     ", ".join(wfacts["missing"]), project, repo, ts_from,
                     ts_to))
    bundle = {"meta": _materialize_meta(conn, project, repo, ts_from, ts_to,
                                        ref_date=ref_date,
                                        problems=branch_problems,
                                        window_facts=wfacts)}

    socials = graphstore.repo_nodes(conn, project, repo, node_class="social")
    codes = graphstore.repo_nodes(conn, project, repo, node_class="code")
    commit_nodes = [n for n in codes if _is_commit_node(n)]
    leaves = _social_leaves(socials)
    # Issue #97: the records as of the window's end ({node id: projected
    # record, or None for one created after it}; only the records that
    # change). None when not asked for: every record as stored.
    as_of = _socials_as_of(socials, leaves, ts_to) if as_of_end else None
    view_socials = _view_socials(conn, project, repo, socials, in_window_ids,
                                 ts_from, ts_to, as_of=as_of)
    view_commits = [n for n in commit_nodes
                    if not n["ts"] or n["id"] in in_window_ids]

    def record(n):
        return as_of[n["id"]] if as_of and n["id"] in as_of else n["data"]
    bundle["prs"] = _by(
        [record(n) for n in view_socials if _local(n["id"]).startswith("pr-")],
        key=lambda d: d.get("number"))
    # Unknown mainline (issue #84): link then counts EVERY merged PR as merged
    # into it (the pre-#84 behaviour). Say so whenever that decides anything.
    if branch_problems and any(p.get("merged") for p in bundle["prs"]):
        warn("extract: base branch unknown for {}/{} [{} .. {}]: {}; every "
             "merged PR is counted as merged into the mainline (stacked / "
             "release-branch merges are not told apart)".format(
                 project, repo, ts_from, ts_to, branch_problems[0]))
    bundle["issues"] = _by(
        [record(n) for n in view_socials
         if _local(n["id"]).startswith("issue-")],
        key=lambda d: d.get("number"))

    # Phase 10 slice 1: re-attach the review/lifecycle social nodes onto their
    # parent pr/issue records (the inverse of fold's first-class node emission) —
    # the RAW substrate. The derived review_rounds/reopen_count are computed in
    # link.enrich (with forecast/modules), not here, so extract stays raw-only.
    # Done ONLY when such nodes exist so PRs/issues with none stay byte-identical.
    # A review/event follows its parent: it is attached only to a pr/issue in
    # this view (orphans are skipped), whatever its own timestamp. A record
    # projected to the window's end (#97) already carries its leaves up to
    # that end, so it is left as it is.
    _attach_reviews_and_lifecycle(socials, bundle["prs"], bundle["issues"],
                                  leaves=leaves)
    projected = {kind: sum(1 for r in bundle[kind] if AS_OF_KEY in r)
                 for kind in ("prs", "issues")}
    if any(projected.values()):
        bundle["meta"]["records_as_of"] = dict(
            projected, instant=as_of_instant(ts_to))
        # When the records were gathered (finding 8): what has no history
        # (title, labels, milestone, board status) is as of this instant.
        gathered = _gathered_at(view_socials)
        if gathered:
            bundle["meta"]["gathered_at"] = gathered

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
    # Key order carries no meaning (issue #100): every consumer orders
    # artifacts by an explicit key (link.build_timeline / compute_feature_deltas,
    # render's content timeline), so the dict is simply keyed in id order — the
    # same order view.py's `sort_keys` output reads back in.
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
    bundle["artifacts"] = {aid: arts[aid] for aid in sorted(arts)}

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
        _materialize_people(conn, project), bundle, facts.get("code_graph"))

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

    # Per-repo facts (issue #94: this window's; see `_window_facts`): emit each
    # key only when a value was stored.
    for key, _local_id in _SINGLETON_FACTS:
        if key in facts:
            bundle[key] = facts[key]
    # First-time contributors (per-window only, finding 12): the gathered
    # window's fact as stored, or merged over the windows inside the view
    # (`_merged_newcomers`); absent when no gather collected it.
    if "newcomers" in facts:
        bundle["newcomers"] = facts["newcomers"]
    # Module change notes (per-window only): the gathered window's fact as
    # stored, or the items of the windows overlapping the view dated in it
    # (`_merged_module_notes`); absent when no gather collected them.
    if "module_notes" in facts:
        bundle["module_notes"] = facts["module_notes"]
    # PR-internal history (per-window only): the gathered window's fact as
    # stored, or the PRs of the windows overlapping the view
    # (`_merged_pr_history`); absent when no gather collected it.
    if "pr_history" in facts:
        bundle["pr_history"] = facts["pr_history"]
    # Contributor affiliation (per-window only): the gathered window's fact
    # as stored, or each person's latest decision over the windows
    # overlapping the view (`_merged_affiliations`); absent when no gather
    # collected it.
    if "affiliations" in facts:
        bundle["affiliations"] = facts["affiliations"]
    # What the store holds of the window's newcomers / returning
    # contributors before the window (`_prior_activity`); only when the view
    # names any.
    prior = _prior_activity(conn, project, repo, socials,
                            bundle.get("newcomers"))
    if prior is not None:
        bundle["prior_activity"] = prior

    return bundle


def _attach_reviews_and_lifecycle(socials, prs, issues, leaves=None):
    """Re-attach `review`/`event` social nodes onto their parent pr/issue records.

    The inverse of `gather.fold_bundle`'s first-class node emission: a review
    node `review-<pr>-<id>` belongs on PR `<pr>`'s `reviews` list; an event node
    `event-pr-<n>-<id>` / `event-issue-<n>-<id>` belongs on that pr/issue's
    `lifecycle` list. Both lists are ordered by the node's natural timestamp
    (submitted_at / created_at) then id, so a re-extract is byte-stable. Mutates
    the pr/issue dicts in place; only sets a key when >=1 child node exists (so
    records with none stay byte-identical). The node data blobs already carry the
    exact normalized shape fold persisted, so they round-trip as-is. `leaves`
    is `_social_leaves(socials)` when the caller has it. A record projected to
    the window's end (it carries `AS_OF_KEY`, issue #97) is skipped: it holds
    its leaves up to that end already."""
    reviews_by_pr, lifecycle_by_parent = leaves or _social_leaves(socials)
    pr_by_num = {p.get("number"): p for p in prs if AS_OF_KEY not in p}
    issue_by_num = {i.get("number"): i for i in issues if AS_OF_KEY not in i}
    for prnum, revs in reviews_by_pr.items():
        pr = pr_by_num.get(prnum)
        if pr is not None:
            pr["reviews"] = revs
    for (kind, num), evs in lifecycle_by_parent.items():
        parent = (pr_by_num if kind == "pr" else issue_by_num).get(num)
        if parent is not None:
            parent["lifecycle"] = evs


def _social_leaves(socials):
    """The stored review / lifecycle-event leaves by parent: ({pr number:
    [review data]}, {("pr"|"issue", number): [event data]}), each list
    ordered by its timestamp (submitted_at / created_at) then id."""
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

    return ({num: _sorted(revs, "submitted_at")
             for num, revs in reviews_by_pr.items()},
            {key: _sorted(evs, "created_at")
             for key, evs in lifecycle_by_parent.items()})


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


def _ended_after(data, ts_to):
    """Open at the window's END though stored closed: created by the
    window's end and merged / closed only after it (both dated, as
    `gather.keep_pr_for_window` requires)."""
    ended = data.get("merged_at") if data.get("merged") else data.get("closed_at")
    if not ended or graphstore.ts_in_window(ended, None, ts_to):
        return False
    return graphstore.ts_in_window(data.get("created_at"), None, ts_to)


def _pr_in_window(data, ts_from, ts_to):
    """gather's PR rule (`gather.keep_pr_for_window`): merged in the window,
    closed (unmerged) in the window, open (here: open and created by the
    window's end), or open at the window's end and merged / closed after it
    (#97: a month gathered late keeps those)."""
    if data.get("merged"):
        return graphstore.ts_in_window(data.get("merged_at"), ts_from, ts_to) \
            or _ended_after(data, ts_to)
    if data.get("state") == "closed":
        return graphstore.ts_in_window(data.get("closed_at"), ts_from, ts_to) \
            or _ended_after(data, ts_to)
    return _open_by(data, ts_to)


def _issue_in_window(data, ts_from, ts_to):
    """gather's issue rule (minus train context, see `_view_socials`): closed
    or updated in the window, open and created by the window's end, or open
    at the window's end and closed after it (#97: gather lists every issue
    updated since the window's start, so a month gathered late holds those)."""
    return (graphstore.ts_in_window(data.get("closed_at"), ts_from, ts_to)
            or graphstore.ts_in_window(data.get("updated_at"), ts_from, ts_to)
            or _open_by(data, ts_to)
            or (data.get("state") == "closed" and _ended_after(data, ts_to)))


def _view_socials(conn, project, repo, socials, in_window_ids, ts_from, ts_to,
                  as_of=None):
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
    parent (see `_attach_reviews_and_lifecycle`).

    `as_of` (issue #97, `_socials_as_of`): the records projected to the
    window's end. The rule then reads a projected record, and the timestamp
    fold would give it (`_record_ts`), instead of the stored one. A record
    `as_of` does not name is read as stored.

    A record created after the window's end (None in `as_of`; read as stored,
    a `created_at` after the end) is in no view, not even as context: the
    report view and an `--as-stored` view of a window hold the same records
    in that respect, and `validate.py --bundle` leaves such stored records
    (and people seen only on them) out on both sides."""
    as_of = as_of or {}

    def data(n):
        return as_of[n["id"]] if n["id"] in as_of else n["data"]

    def gone(n):
        if n["id"] in as_of:
            return as_of[n["id"]] is None
        rec = n["data"] if isinstance(n["data"], dict) else {}
        return _after(rec.get("created_at"), ts_to)

    def dated_in(n):
        if not n["ts"]:
            return True
        if n["id"] in as_of:
            kind = "pr" if _local(n["id"]).startswith("pr-") else "issue"
            return graphstore.ts_in_window(_record_ts(kind, data(n)), ts_from,
                                           ts_to)
        return n["id"] in in_window_ids

    prs = {n["id"] for n in socials if _local(n["id"]).startswith("pr-")
           and not gone(n)
           and (dated_in(n) or _pr_in_window(data(n), ts_from, ts_to))}
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
        elif local.startswith("issue-") and not gone(n):
            if (n["id"] in context or dated_in(n)
                    or _issue_in_window(data(n), ts_from, ts_to)):
                out.append(n)
    return out


def _gathered_at(view_socials):
    """When the view's PR / issue records were gathered: the latest
    `fetched_at` of their nodes (the time the fold that last wrote each one
    ran, at the end of its gather), or None when none is recorded. Their
    fields with no history (title, labels, milestone, board status) are as
    of that gather, not of the window's end."""
    times = [n.get("fetched_at") for n in view_socials
             if _local(n["id"]).startswith(("pr-", "issue-"))]
    times = [t for t in times if isinstance(t, str) and t]
    return max(times) if times else None


# ---------------------------------------------------------------------------
# As of the window's end (issue #97).
#
# PR / issue records are stored latest-wins: a month gathered (or viewed)
# after it ended reads each record as the latest gather saw it — merged,
# closed, commented on since. Every timestamp needed to wind a record back is
# in the record: its created / closed / merged times, its comments, review
# comments, reviews and lifecycle (closed / reopened) events. A report view
# (`extract(..., as_of_end=True)`) projects each record carrying anything
# later than the window's end back to that end (`record_as_of`), before the
# window rule picks the view's records, so state, buckets, trains, effort,
# people and the timeline all read the window as it was. What GitHub keeps
# no history of stays as stored (`_NOT_PROJECTED`), and the record says so.
# ---------------------------------------------------------------------------

# The marker a projected record carries: {"instant", "fields",
# "not_projected"} (BUNDLE.md, "As of the window's end").
AS_OF_KEY = "as_of"

# (list key, timestamp key) of the dated activity a record holds.
_ACTIVITY_LISTS = (("comments_list", "created_at"),
                   ("review_comments", "created_at"),
                   ("reviews", "submitted_at"),
                   ("lifecycle", "created_at"))

# Leaves stored as their own nodes: omitted when empty (gather / extract
# set them only when non-empty).
_OMIT_WHEN_EMPTY = ("reviews", "lifecycle")

# Fields GitHub gives no history for: kept as stored on a projected record
# and named in its marker's `not_projected` when the record has them.
_NOT_PROJECTED = {
    "pr": ("title", "body", "labels", "facets", "milestone", "assignees",
           "base", "head", "closes", "closing_issues", "crossref_issues",
           "timeline_xrefs", "external_refs", "board_status", "iteration"),
    "issue": ("title", "body", "labels", "facets", "kind", "issue_type",
              "milestone", "assignees", "reactions", "blocks",
              "board_status", "iteration"),
}

_MISSING = object()


def as_of_instant(ts_to):
    """The instant a view of a window ending `ts_to` is projected to: the
    last second of that day (UTC) for a date, the timestamp itself else."""
    if not ts_to:
        return None
    ts_to = str(ts_to)
    if len(ts_to) == 10:
        return ts_to + "T23:59:59Z"
    return ts_to


def _after(ts, ts_to):
    """Is timestamp `ts` later than the end of a window ending `ts_to`? The
    window predicate's own rule (a date covers its whole day); False for a
    missing or non-text value (it cannot be placed)."""
    return isinstance(ts, str) and bool(ts) and \
        not graphstore.ts_in_window(ts, None, ts_to)


def _ts_key(ts):
    return ts[:19]


def _record_times(rec):
    """Every timestamp a PR / issue record carries (some may be None)."""
    for key in ("created_at", "closed_at", "merged_at", "updated_at"):
        yield rec.get(key)
    for lst, tkey in _ACTIVITY_LISTS:
        rows = rec.get(lst)
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict):
                yield row.get(tkey)


def _record_ts(kind, rec):
    """The node timestamp fold gives a record (gather.record_rows): a PR's
    merged_at, else closed_at, else created_at; an issue's closed_at, else
    updated_at."""
    if kind == "pr":
        return rec.get("merged_at") or rec.get("closed_at") \
            or rec.get("created_at")
    return rec.get("closed_at") or rec.get("updated_at")


def _state_as_of(kind, rec, out, ts_to, not_projected):
    """Set `out`'s open / closed / merged state as of the end of a window
    ending `ts_to`, from `rec` (the stored record, leaves attached).

    A merge at or before the end is final: the record keeps its state. A PR
    merged after it is not merged (`merged_at` / `merged_by` cleared). Then
    open or closed: from the record's `closed` / `reopened` lifecycle events
    when it has any — the last one at or before the end decides (closed
    then reopened before the end: open; closed → reopened → closed with the
    last close after the end: open; closed before the end and reopened after
    it: closed at that close) — else from `closed_at` (closed at or before
    the end: closed; after it, or open now: open). An issue's `state_reason`
    is kept when its closing as of the end is the stored one; an earlier
    closing's reason is not recorded (null, named in `not_projected`); an
    issue open as of the end has `reopened` when a reopening before the end
    is recorded, else null (an open record keeps its own reason when it has
    no lifecycle to say otherwise)."""
    if kind == "pr" and rec.get("merged_at") and not _after(rec.get("merged_at"),
                                                           ts_to):
        return
    if kind == "pr" and (rec.get("merged") or rec.get("merged_at")):
        out["merged"] = False
        out["merged_at"] = None
        out["merged_by"] = None
    lifecycle = rec.get("lifecycle")
    marks = [e for e in (lifecycle if isinstance(lifecycle, list) else ())
             if isinstance(e, dict) and e.get("event") in ("closed", "reopened")
             and isinstance(e.get("created_at"), str)]
    marks.sort(key=lambda e: (_ts_key(e["created_at"]), str(e.get("id"))))
    kept = [e for e in marks if not _after(e["created_at"], ts_to)]
    stored_closed = rec.get("state") == "closed"
    closed_at = rec.get("closed_at")
    # The stored closing is the one in force at the end: closed at or before
    # it, and no close / reopen recorded after it.
    same_closing = stored_closed and (not closed_at or not _after(closed_at,
                                                                 ts_to)) \
        and len(kept) == len(marks)
    if same_closing:
        closed, closed_at = True, closed_at
    elif marks:
        last = kept[-1] if kept else None
        closed = last is not None and last["event"] == "closed"
        closed_at = last["created_at"] if closed else None
    else:
        closed, closed_at = False, None
    out["state"] = "closed" if closed else "open"
    out["closed_at"] = closed_at
    if kind != "issue":
        return
    if closed and not same_closing:
        out["state_reason"] = None
        not_projected.add("state_reason")
    elif not closed:
        if marks:
            out["state_reason"] = "reopened" if any(
                e["event"] == "reopened" for e in kept) else None
        elif stored_closed:
            out["state_reason"] = None


def _reviews_summary(reviews, author):
    """(reviewers, review_decision) of the review submissions `reviews`
    (the stored review shape), by gather's own reduction."""
    import gather  # deferred: gather imports extract
    raw = [{"id": r.get("id"), "user": {"login": r.get("author")},
            "state": r.get("state"), "submitted_at": r.get("submitted_at")}
           for r in reviews if isinstance(r, dict)]
    summary = gather.summarize_reviews(raw, author=author)
    return summary["reviewers"], summary["decision"]


def record_as_of(kind, rec, ts_to):
    """A PR (`kind` "pr") or issue ("issue") record as it was at the end of
    a window ending `ts_to` (issue #97). Pure: `rec` is not changed.

    Returns `rec` itself when it carries nothing later than that end (no
    timestamp of `_record_times` after it), None when it was created after
    it (it did not exist), else a projected copy:
    - state: `_state_as_of`;
    - activity: comments, review comments, reviews and lifecycle events
      dated after the end are dropped (`reviews` / `lifecycle` left out when
      none remain); `comments` / `review_comments_count` lose the dropped
      ones; `reviewers` / `review_decision` are re-derived from the
      remaining reviews (when the record has no review submissions they
      cannot be, and are named in `not_projected`; when a remaining review
      is `dismissed` — GitHub keeps no dismissal date — the stored
      `review_decision` is kept and it and `review_rounds` are named in
      `not_projected`); `open_high_activity`
      is re-derived (an issue's reactions are as stored);
    - `updated_at`: the latest timestamp left at or before the end (its
      created / closed / merged time, comments, reviews, lifecycle events),
      the item's last recorded activity then;
    - `_NOT_PROJECTED` fields stay as stored;
    - `AS_OF_KEY`: {"instant": `as_of_instant(ts_to)`, "fields": the keys
      that changed, "not_projected": the fields kept as stored}."""
    if not ts_to or not any(_after(t, ts_to) for t in _record_times(rec)):
        return rec
    if _after(rec.get("created_at"), ts_to):
        return None
    out = dict(rec)
    not_projected = {k for k in _NOT_PROJECTED[kind] if k in rec}
    dropped = {}
    for lst, tkey in _ACTIVITY_LISTS:
        rows = rec.get(lst)
        if not isinstance(rows, list):
            continue
        kept = [r for r in rows
                if not (isinstance(r, dict) and _after(r.get(tkey), ts_to))]
        dropped[lst] = len(rows) - len(kept)
        if not dropped[lst]:
            continue
        if kept or lst not in _OMIT_WHEN_EMPTY:
            out[lst] = kept
        else:
            out.pop(lst, None)
    _state_as_of(kind, rec, out, ts_to, not_projected)
    for lst, count in (("comments_list", "comments"),
                       ("review_comments", "review_comments_count")):
        if dropped.get(lst) and isinstance(rec.get(count), int) \
                and not isinstance(rec.get(count), bool):
            out[count] = max(0, rec[count] - dropped[lst])
    if kind == "pr":
        # GitHub rewrites a dismissed review's state to `dismissed` and keeps
        # its submission date; the store records no dismissal date, so such a
        # review's state at the instant is unknown. The decision and the
        # rounds read from those states are then not projected: the stored
        # decision is kept and both are named (the reviewer set is not
        # affected: the review was submitted by the instant either way).
        dismissed = any(isinstance(r, dict)
                        and (r.get("state") or "").lower() == "dismissed"
                        for r in out.get("reviews") or [])
        if dismissed:
            not_projected.add("review_rounds")
            if "review_decision" in rec:
                not_projected.add("review_decision")
        if dropped.get("reviews"):
            reviewers, decision = _reviews_summary(out.get("reviews") or [],
                                                   rec.get("author"))
            out["reviewers"] = reviewers
            if "review_decision" in rec and not dismissed:
                out["review_decision"] = decision
        elif rec.get("reviewers") and not isinstance(rec.get("reviews"), list):
            not_projected.update(k for k in ("reviewers", "review_decision")
                                 if k in rec)
    if "open_high_activity" in rec:
        import gather  # deferred: gather imports extract
        out["open_high_activity"] = gather.derive_open_high_activity(out)
    times = [t for t in _record_times({k: v for k, v in out.items()
                                       if k != "updated_at"})
             if isinstance(t, str) and t and not _after(t, ts_to)]
    if isinstance(rec.get("updated_at"), str) \
            and not _after(rec["updated_at"], ts_to):
        times.append(rec["updated_at"])
    out["updated_at"] = max(times, key=_ts_key) if times else None
    fields = sorted(k for k in set(rec) | set(out)
                    if rec.get(k, _MISSING) != out.get(k, _MISSING))
    if not fields:
        return rec
    out[AS_OF_KEY] = {"instant": as_of_instant(ts_to), "fields": fields,
                      "not_projected": sorted(not_projected)}
    return out


def _socials_as_of(socials, leaves, ts_to):
    """{node id: record_as_of(record)} for the stored PR / issue records the
    projection changes (None for one created after the end), each read with
    its stored review / lifecycle leaves attached as the view attaches them.
    Records it leaves unchanged are not named (they are read as stored)."""
    reviews_by_pr, lifecycle_by_parent = leaves
    out = {}
    for n in socials:
        local = _local(n["id"])
        if local.startswith("pr-"):
            kind = "pr"
        elif local.startswith("issue-"):
            kind = "issue"
        else:
            continue
        rec = n["data"]
        num = rec.get("number")
        full = dict(rec)
        if kind == "pr" and num in reviews_by_pr:
            full["reviews"] = reviews_by_pr[num]
        if (kind, num) in lifecycle_by_parent:
            full["lifecycle"] = lifecycle_by_parent[(kind, num)]
        projected = record_as_of(kind, full, ts_to)
        if projected is not full:
            out[n["id"]] = projected
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


def _day(value):
    """A window bound's calendar day (date prefix), or None."""
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _contained(window, ts_from, ts_to):
    """Is every day of the gathered window {from, to} inside the view
    [ts_from, ts_to]? Date prefixes; a None view bound is open."""
    w_from, w_to = (window.get("from") or "")[:10], (window.get("to") or "")[:10]
    return bool(w_from and w_to) and (not ts_from or w_from >= ts_from[:10]) \
        and (not ts_to or w_to <= ts_to[:10])


def _covers(windows, ts_from, ts_to):
    """Do `windows` together cover every day of the view [ts_from, ts_to]?"""
    lo, hi = _day(ts_from), _day(ts_to)
    if lo is None or hi is None:
        return False
    spans = sorted((_day(w["from"]), _day(w["to"])) for w in windows)
    nxt = lo
    for a, b in spans:
        if a is None or b is None or a > nxt:
            break
        nxt = max(nxt, b + datetime.timedelta(days=1))
    return nxt > hi


def _neg_day(value):
    """Sort key putting a later day first."""
    day = _day(value)
    return -day.toordinal() if day else 0


def _window_facts(conn, project, repo, ts_from, ts_to):
    """The per-repo facts (workflow_stats / code_graph / code_owners /
    label_taxonomy) for the view [ts_from, ts_to] (issue #94).

    Returns None on a store with no per-window facts for this repo (folded
    before #94): the caller then reads the latest-wins nodes. Else
    {"facts": {key: value}, "clone_sha", "note", "missing"}:

    - a view of exactly a gathered window: that window's facts, note None;
    - any other view: CI statistics are the sum of the gathered windows that
      lie wholly inside it (a window that only partly overlaps it is left out:
      its runs cannot be split by day; so is one overlapping a window already
      summed — by start day, longest first — so no run counts twice; none
      inside -> no workflow_stats); the
      structure (code graph, owners, taxonomy) and clone sha are those of the
      latest gathered window overlapping the view, else the latest ending
      before it, else the earliest. `note` (meta.facts_windows) names them.
    `missing` lists keys whose stored value is gone (a damaged store)."""
    exact = graphstore.get_window_facts(conn, project, repo, ts_from, ts_to)
    if exact is not None:
        return dict(exact, note=None)
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    windows = sorted({(w.get("from"), w.get("to")) for w in ledger
                      if isinstance(w, dict) and w.get("project") == project
                      and w.get("repo") == repo and w.get("from") and w.get("to")})
    probes = {k: graphstore.get_window_facts(conn, project, repo, *k, keys=())
              for k in windows}       # existence probe: no value is read
    recorded = {k for k, p in probes.items() if p is not None}
    if not recorded:
        return None
    if (ts_from, ts_to) in windows:
        # A gathered window that has no facts entry of its own (folded before
        # per-window facts existed) keeps reading the latest-wins nodes, as it
        # did before another window was added to the store.
        return None
    # Windows whose CI runs were collected: only these can be summed.
    with_ci = {k for k in recorded if probes[k]["ci_collected"]}
    structure_keys = tuple(k for k in graphstore.WINDOW_FACT_KEYS
                           if k not in ("workflow_stats", "newcomers",
                                        "module_notes", "pr_history",
                                        "affiliations"))

    def win(key):
        return {"from": key[0], "to": key[1]}

    def latest(keys):
        return max(keys, key=lambda k: (k[1][:10], k[0][:10], k))

    overlapping = [k for k in windows if _overlaps(win(k), ts_from, ts_to)]
    # Sum windows that do not overlap one another, so no run is counted twice
    # (a store can hold a month and a re-folded roll-up or part of it): by
    # start day, the longest first; a window overlapping one taken is left out.
    summed, last_to = [], None
    for k in sorted((k for k in overlapping if k in with_ci
                     and _contained(win(k), ts_from, ts_to)),
                    key=lambda k: (k[0][:10], _neg_day(k[1]), k)):
        if last_to is None or k[0][:10] > last_to:
            summed.append(k)
            last_to = k[1][:10]
    excluded = [k for k in overlapping if k not in summed]
    candidates = [k for k in overlapping if k in recorded]
    if not candidates and ts_from:
        candidates = [k for k in recorded if k[1][:10] < ts_from[:10]]
    if candidates:
        structure = latest(candidates)
    else:
        structure = min(recorded, key=lambda k: (k[0][:10], k[1][:10], k))
    chosen = graphstore.get_window_facts(conn, project, repo, *structure,
                                         keys=structure_keys)
    facts = dict(chosen["facts"])
    missing = list(chosen["missing"])
    stats, lost = [], []
    for k in summed:
        got = graphstore.get_window_facts(conn, project, repo, *k,
                                          keys=("workflow_stats",))
        missing.extend(got["missing"])
        if got["missing"]:
            lost.append(k)        # its stored CI value is gone: not summed
        elif got["facts"].get("workflow_stats"):
            stats.append(got["facts"]["workflow_stats"])
    if lost:
        summed = [k for k in summed if k not in lost]
        excluded = [k for k in overlapping if k not in summed]
    if stats:
        facts["workflow_stats"] = derive.sum_workflow_stats(stats)
    newcomers = _merged_newcomers(
        conn, project, repo,
        [k for k in recorded if _contained(win(k), ts_from, ts_to)],
        ts_from, ts_to)
    if newcomers is not None:
        facts["newcomers"] = newcomers
    notes = _merged_module_notes(conn, project, repo, overlapping, recorded,
                                 ts_from, ts_to)
    if notes is not None:
        facts["module_notes"] = notes
    history = _merged_pr_history(conn, project, repo, overlapping, recorded)
    if history is not None:
        facts["pr_history"] = history
    affiliations = _merged_affiliations(conn, project, repo, overlapping,
                                        recorded, ts_from, ts_to)
    if affiliations is not None:
        facts["affiliations"] = affiliations
    note = {"structure": win(structure),
            "workflow_stats": [win(k) for k in summed],
            # every day of the view is in exactly one summed window
            "workflow_stats_complete": bool(summed)
            and _covers([win(k) for k in summed], ts_from, ts_to)}
    if excluded:
        note["workflow_stats_excluded"] = [win(k) for k in excluded]
    return {"facts": facts, "clone_sha": chosen["clone_sha"], "note": note,
            "missing": sorted(set(missing))}


def _merged_newcomers(conn, project, repo, inside, ts_from, ts_to):
    """The `newcomers` of a view that is not exactly one gathered window:
    merged from the gathered windows lying wholly inside it (`inside`) that
    collected one, taken by start day, the longest first, leaving out a
    window overlapping one taken (as CI statistics are). A login new in one
    of them is new to the view too (no item before that window's start, so
    none before the view's); `new` keeps each login's earliest entry,
    `unknown` / `not_checked` are the unions less the logins found new.
    Returns {"collected": True, "before", "new", "unknown", "not_checked",
    "windows": [{from, to}], "covers_view": every day of the view is in one
    of them} — or None when no window inside the view collected newcomers.
    `candidates` is not merged (a login can be a candidate in two windows)."""
    values = {}
    for k in inside:
        got = graphstore.get_window_facts(conn, project, repo, *k,
                                          keys=("newcomers",))
        value = (got or {}).get("facts", {}).get("newcomers")
        if isinstance(value, dict) and value.get("collected"):
            values[k] = value
    taken, last_to = [], None
    for k in sorted(values, key=lambda k: (k[0][:10], _neg_day(k[1]), k)):
        if last_to is None or k[0][:10] > last_to:
            taken.append(k)
            last_to = k[1][:10]
    if not taken:
        return None
    new, unknown, not_checked = {}, set(), set()
    returning, regular, undated, dated = {}, {}, set(), None
    for k in taken:                      # earliest window first
        v = values[k]
        for e in v.get("new") or []:
            if isinstance(e, dict) and e.get("login") not in new:
                new[e.get("login")] = e
        if "returning" in v:
            dated = v.get("gap_thresholds_days")
        for e in v.get("returning") or []:
            # regular in an earlier window: active at the view's start
            if isinstance(e, dict) and e.get("login") not in returning \
                    and e.get("login") not in regular:
                returning[e.get("login")] = e
        for e in v.get("regular") or []:
            if isinstance(e, dict) and e.get("login") not in regular:
                regular[e.get("login")] = e
        undated |= set(v.get("earlier_undated") or [])
        unknown |= set(v.get("unknown") or [])
        not_checked |= set(v.get("not_checked") or [])
    # the earliest class wins: a login new (or returning) in an earlier
    # window is that to the view, whatever a later window said
    for login in list(returning):
        if login in new:
            del returning[login]
    first_seen = set(new) | set(returning)
    regular = {k: e for k, e in regular.items() if k not in first_seen}
    undated -= first_seen | set(regular)
    unknown -= first_seen | set(regular) | undated
    not_checked -= first_seen | set(regular) | undated | unknown
    windows = [{"from": k[0], "to": k[1]} for k in taken]
    out = {"collected": True, "before": values[taken[0]].get("before"),
           "new": [new[login] for login in sorted(new, key=str)],
           "unknown": sorted(unknown), "not_checked": sorted(not_checked),
           "windows": windows,
           "covers_view": _covers(windows, ts_from, ts_to)}
    if dated is not None:
        out["returning"] = [returning[k] for k in sorted(returning, key=str)]
        out["regular"] = [regular[k] for k in sorted(regular, key=str)]
        out["gap_thresholds_days"] = dated
    if undated:
        out["earlier_undated"] = sorted(undated)
    return out


def _merged_module_notes(conn, project, repo, overlapping, recorded, ts_from,
                         ts_to):
    """The `module_notes` of a view that is not exactly one gathered window.

    Every item of the fact is dated (a changelog entry, a version move and
    an unread file by its commit's day, a tag by its date), so a view takes
    the items of every gathered window overlapping it (`overlapping`, those
    with a facts entry: `recorded`) that are dated inside the view, once
    each (changelogs / versions / unread by (path, commit), tags by name).
    A window only partly inside the view contributes its items that fall in
    it, so a sub-window or a roll-up reads exactly its own days. Returns
    {"collected": True, "changelogs", "versions", "tags", ["unread"],
    "windows": [{from, to}], "covers_view"} — or None when no overlapping
    window collected module notes. `tags.collected` is true when any of
    those windows listed tags; the windows that did not are named in
    `tags.windows_without_tags`."""
    values = {}
    for k in sorted(k for k in overlapping if k in recorded):
        got = graphstore.get_window_facts(conn, project, repo, *k,
                                          keys=("module_notes",))
        value = (got or {}).get("facts", {}).get("module_notes")
        if isinstance(value, dict) and value.get("collected"):
            values[k] = value
    if not values:
        return None

    def inside(item):
        return isinstance(item, dict) and graphstore.ts_in_window(
            item.get("date"), ts_from, ts_to)

    merged = {"changelogs": {}, "versions": {}, "unread": {}}
    tags, tagless, tag_source = {}, [], None
    for k, v in values.items():
        for field in merged:
            for item in v.get(field) or []:
                if inside(item):
                    merged[field].setdefault(
                        (item.get("path") or "", item.get("commit") or ""), item)
        t = v.get("tags") or {}
        if t.get("collected"):
            tag_source = tag_source or t.get("source")
            for item in t.get("items") or []:
                if inside(item):
                    tags.setdefault(item.get("tag"), item)
        else:
            tagless.append({"from": k[0], "to": k[1]})

    def ordered(items):
        return sorted(items, key=lambda i: (str(i.get("date") or ""),
                                            str(i.get("path") or ""),
                                            str(i.get("commit") or "")))
    out = {"collected": True,
           "changelogs": ordered(merged["changelogs"].values()),
           "versions": ordered(merged["versions"].values())}
    if len(tagless) == len(values):
        out["tags"] = {"collected": False,
                       "reason": "no gathered window of the view listed tags"}
    else:
        out["tags"] = {"collected": True, "source": tag_source or "clone",
                       "items": sorted(tags.values(), key=lambda i: (
                           str(i.get("date") or ""), str(i.get("tag") or "")))}
        if tagless:
            out["tags"]["windows_without_tags"] = tagless
    if merged["unread"]:
        out["unread"] = ordered(merged["unread"].values())
    windows = [{"from": k[0], "to": k[1]} for k in sorted(values)]
    out["windows"] = windows
    out["covers_view"] = _covers(windows, ts_from, ts_to)
    return out


def _merged_pr_history(conn, project, repo, overlapping, recorded):
    """The `pr_history` of a view that is not exactly one gathered window.

    A PR's history is what its head ref held when a window was gathered, so
    the view takes, per PR number, the entry of the LATEST gathered window
    overlapping it that collected one (by end day, then start day); the
    view's consumers drop commits dated after the view's end (the as-of
    rule, link). Returns {"collected": True, "base", "prs": [by number],
    "limits"?, "windows": [{from, to}]} or None when no overlapping window
    collected a PR history."""
    values = {}
    for k in sorted(k for k in overlapping if k in recorded):
        got = graphstore.get_window_facts(conn, project, repo, *k,
                                          keys=("pr_history",))
        value = (got or {}).get("facts", {}).get("pr_history")
        if isinstance(value, dict) and value.get("collected"):
            values[k] = value
    if not values:
        return None
    prs, base, limits = {}, None, None
    for k in sorted(values, key=lambda k: (k[1][:10], k[0][:10], k)):
        v = values[k]                      # later windows replace earlier ones
        base = v.get("base", base)
        limits = v.get("limits", limits)
        for row in v.get("prs") or []:
            if isinstance(row, dict) and isinstance(row.get("number"), int):
                prs[row["number"]] = row
    out = {"collected": True, "base": base,
           "prs": [prs[n] for n in sorted(prs)],
           "windows": [{"from": k[0], "to": k[1]} for k in sorted(values)]}
    if limits is not None:
        out["limits"] = limits
    return out


def _merged_affiliations(conn, project, repo, overlapping, recorded, ts_from,
                         ts_to):
    """The `affiliations` of a view that is not exactly one gathered window.

    A label is the answer GitHub (and the operator) gave when a window was
    gathered, so the view takes, per person, the entry of the LATEST
    gathered window overlapping it that collected one (by end day, then
    start day); `unknown` / `not_checked` are recomputed over the merged
    people (a person not checked in one window but decided in another is
    decided). Returns {"collected": True, "home_orgs", "home_names",
    "home_email_domains" (the latest window's), "people", "unknown",
    "not_checked", "windows": [{from, to}], "covers_view"} or None when no
    overlapping window collected affiliations."""
    values = {}
    for k in sorted(k for k in overlapping if k in recorded):
        got = graphstore.get_window_facts(conn, project, repo, *k,
                                          keys=("affiliations",))
        value = (got or {}).get("facts", {}).get("affiliations")
        if isinstance(value, dict) and value.get("collected"):
            values[k] = value
    if not values:
        return None
    people, not_checked, latest = {}, set(), None
    for k in sorted(values, key=lambda k: (k[1][:10], k[0][:10], k)):
        v = values[k]                      # later windows replace earlier ones
        latest = v
        for login, e in (v.get("people") or {}).items():
            if isinstance(e, dict):
                people[login] = e
        not_checked |= set(v.get("not_checked") or [])
    # a registry window lists its unchecked people in `people` too (signal
    # not_checked): only a later deciding entry takes them off the list
    not_checked -= {k for k, e in people.items() if e.get("signal") != "not_checked"}
    unknown = {k for k, e in people.items() if e.get("affiliation") == "unknown"}
    windows = [{"from": k[0], "to": k[1]} for k in sorted(values)]
    return {"collected": True,
            "home_orgs": list(latest.get("home_orgs") or []),
            "home_names": list(latest.get("home_names") or []),
            "home_email_domains": list(latest.get("home_email_domains") or []),
            "people": {k: people[k] for k in sorted(people)},
            "unknown": sorted(unknown | not_checked),
            "not_checked": sorted(not_checked),
            "windows": windows, "covers_view": _covers(windows, ts_from, ts_to)}


def _prior_activity(conn, project, repo, socials, newcomers):
    """What the STORE holds of each newcomer / returning contributor of the
    view before the view's newcomer boundary (`newcomers.before`), from
    stored records only: {"before", "store_from" (the earliest gathered
    window start of the repo: activity before it is mostly not stored),
    "people": {login: {"prs", "issues", "reviews", "comments", "earliest"}}}
    — PRs / issues they authored, reviews they submitted and comments (PR
    conversation, review and issue comments) they wrote, dated before
    `before`. Only records the store holds count: the PRs / issues of its
    gathered windows (and the older ones those windows reached), so 0 means
    "none in the store", not "none ever". None when the view names no
    newcomer or returning contributor."""
    if not isinstance(newcomers, dict) or not newcomers.get("collected"):
        return None
    logins = sorted({e.get("login") for key in ("new", "returning")
                     for e in newcomers.get(key) or [] if isinstance(e, dict)
                     and e.get("login")})
    before = newcomers.get("before")
    if not logins or not before:
        return None
    wanted = set(logins)
    acc = {login: {"prs": 0, "issues": 0, "reviews": 0, "comments": 0,
                   "earliest": None} for login in logins}

    def seen(login, field, ts):
        if login not in wanted or not ts or str(ts)[:10] >= str(before)[:10]:
            return
        a = acc[login]
        a[field] += 1
        day = str(ts)[:10]
        if a["earliest"] is None or day < a["earliest"]:
            a["earliest"] = day
    for n in socials:
        local = _local(n["id"])
        d = n.get("data") or {}
        if local.startswith(("pr-", "issue-")):
            seen(d.get("author"), "prs" if local.startswith("pr-") else "issues",
                 d.get("created_at"))
            for c in (d.get("comments_list") or []) + (d.get("review_comments") or []):
                if isinstance(c, dict):
                    seen(c.get("author"), "comments", c.get("created_at"))
        elif local.startswith("review-"):
            seen(d.get("author"), "reviews", d.get("submitted_at"))
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    starts = sorted(w.get("from") for w in ledger if isinstance(w, dict)
                    and w.get("project") == project and w.get("repo") == repo
                    and w.get("from"))
    return {"before": before, "store_from": starts[0] if starts else None,
            "people": acc}


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


def _clone_meta(conn, project, repo, ts_from, ts_to):
    """The view's clone provenance (issue #107): {clone_mode?,
    structure_as_of?, boundary_dropped_commits?}.

    - `structure_as_of` ("window_end" | "head"): like `clone_mode`, only for a
      view of exactly one gathered window.

    - `clone_mode`: the one fold stored for exactly this (project, repo,
      from, to) window; absent for any other view (it describes one clone).
    - `boundary_dropped_commits`: the sorted union of the lists stored for
      the gathered windows of this repo that lie wholly inside the view (a
      window that only partly overlaps it is left out, as for CI statistics:
      its dropped commits cannot be placed by day); absent when empty.
    Nothing is returned for a window folded before #107."""
    out = {}
    exact = graphstore.get_clone_info(conn, project, repo, ts_from, ts_to)
    if exact is not None and exact.get("clone_mode"):
        out["clone_mode"] = exact["clone_mode"]
    if exact is not None and exact.get("structure_as_of"):
        # which tree this window's code graph / owners were read at
        out["structure_as_of"] = exact["structure_as_of"]
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    windows = {(w.get("from"), w.get("to")) for w in ledger
               if isinstance(w, dict) and w.get("project") == project
               and w.get("repo") == repo
               and _contained(w, ts_from, ts_to)}
    if exact is not None:
        windows.add((ts_from, ts_to))
    dropped = set()
    for frm, to in windows:
        info = graphstore.get_clone_info(conn, project, repo, frm, to)
        if info is not None:
            dropped.update(s for s in info.get("boundary_dropped_commits") or []
                           if isinstance(s, str))
    if dropped:
        out["boundary_dropped_commits"] = sorted(dropped)
    return out


def _materialize_meta(conn, project, repo, ts_from, ts_to, ref_date=None,
                      problems=None, window_facts=None):
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

    `window_facts` (issue #94, see `_window_facts`): when given, `clone_sha`
    is the one recorded with the window whose structure the view uses (absent
    when that gather had none) and `facts_windows` names the gathered windows
    the view's facts come from (only when the view is not exactly one of
    them). When None (a store folded before #94) `clone_sha` is the repo's
    latest.

    and the clone provenance (issue #107, see `_clone_meta`): `clone_mode`
    for a view of exactly a gathered window, `boundary_dropped_commits` when
    a gathered window inside the view dropped any (omit-when-absent, so a
    store folded before #107 gives the same view as before).
    """
    meta = {"owner": project, "repo": repo, "from": ts_from, "to": ts_to,
            "schema_version": VIEW_CONTRACT_VERSION}
    if window_facts is None:
        clone_sha = graphstore.get_clone_sha(conn, project, repo)
    else:
        clone_sha = window_facts.get("clone_sha")
    if clone_sha:
        meta["clone_sha"] = clone_sha
    meta.update(_clone_meta(conn, project, repo, ts_from, ts_to))
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
    if window_facts is not None and window_facts.get("note"):
        meta["facts_windows"] = window_facts["note"]
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


def _symbol_row_test(conn, project, repo):
    """Predicate `(local, commit_sha) -> bool`: is this ledger row a SYMBOL row
    (graphstore.ledger_row_is_symbol)? A row with the symbol-tail shape is
    checked against its commit's stored file list, read once on first need, so
    a file path such as `notes#a:b:c.md` stays a file row (#101)."""
    files = []

    def is_symbol(local, sha):
        if not graphstore.is_symbol_local(local):
            return False
        if not files:
            files.append(graphstore.repo_commit_files(conn, project, repo))
        return graphstore.ledger_row_is_symbol(local, files[0].get(sha))
    return is_symbol


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
    is_symbol = _symbol_row_test(conn, project, repo)
    for ev in rows:
        if keep is not None and not keep(ev["commit_sha"], ev["date"]):
            continue
        aid = ev["artifact_id"]
        # The repo-qualified remainder is the artifact's local id form: `<path>`
        # for a file event, `<path>#<lang>:<subkind>:<name>` for a symbol one.
        # Skip symbol-granular rows — they are part of the artifact substrate, not
        # this raw file-level `code_events` array. A file path MAY contain `#`
        # (`docs/C#-guide.md`); the symbol form is told apart by its
        # `<lang>:<subkind>:<name>` tail (graphstore.split_symbol_local), unless
        # the row's commit lists that exact path (_symbol_row_test).
        remainder = _full_local(aid, project, repo)
        if is_symbol(remainder, ev["commit_sha"]):
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
    is_symbol = _symbol_row_test(conn, project, repo)
    for ev in rows:
        if keep is not None and not keep(ev["commit_sha"], ev["date"]):
            continue
        aid = ev["artifact_id"]
        remainder = _full_local(aid, project, repo)
        # Symbol rows end in `#<lang>:<subkind>:<name>`. A bare `<path>` file-level
        # row (even with `#` in the path) has no such tail and belongs to
        # code_events, not here; nor does one whose commit lists that exact path.
        if not is_symbol(remainder, ev["commit_sha"]):
            continue
        split = graphstore.split_symbol_local(remainder)
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
