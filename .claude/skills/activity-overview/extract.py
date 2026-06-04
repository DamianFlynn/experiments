"""extract.py — materialize a bundle view out of the journey-graph store.

This is the reader counterpart of `gather.fold_bundle`: where fold *writes* a raw
bundle into the SQLite property graph by identity, `extract` *reads* a window back
out and reconstructs the raw bundle (`meta`, `prs`, `issues`, `commits`,
`code_events`, `milestones`, `releases`) PLUS the two derived projections fold
already materialized into nodes — `artifacts` (the per-file lifecycle ledger) and
`people` (the per-login modules/areas). Slice 7b-2 moved those two out of
`link.enrich` and into the store: extract READS the stored nodes (it does NOT
re-derive via build_artifacts / attribute_people_areas), and `link.enrich`
shrank to the remaining window projections (trains/buckets/timeline/
feature_deltas/modules/forecast/code-area attribution).

Materialization approach (slice 7a part 1)
------------------------------------------
1. Range-query the in-window social + code nodes (graphstore.range_query) — these
   are conceptually `in_window: true` and are what activity counts reflect.
2. Seed a bounded spine traversal (graphstore.traverse_spine) from every in-window
   social node's train anchor to pull out-of-window spine *context* nodes
   (`in_window: false`). `missing` spine ids (referenced but not yet stored — e.g.
   an out-of-window issue a PR closes) are only WARNED about here; wiring backfill
   is slice 7c. Activity counts never include context or missing nodes.
3. Materialize the raw arrays from the node `data` blobs. The materialization
   *source* for the arrays is `graphstore.repo_nodes` (all social/code/structure
   nodes for the repo, NOT window-filtered): the raw arrays must include NULL-ts
   nodes the window scan legitimately drops (un-dated commits, degenerate
   fixtures), while the window/spine sets above still drive the in_window tagging.
4. Reconstruct `code_events` from the ledger in insertion (rowid) order via
   `graphstore.repo_code_events`, which equals the original source order. The
   SAME ledger also carries the symbol-granular rows fold persisted (keyed by the
   symbol artifact id `<path>#<lang>:<subkind>:<name>`, with before/after); those
   are split OUT of the file-level `code_events` array and reconstructed into
   `symbol_events` (see `_materialize_symbol_events`) so the self-sourced raw
   bundle is COMPLETE — `derive.build_artifacts` re-derives the symbol/comment
   artifacts exactly, and the self-sourced `validate` no_drift passes.
5. Reconstruct `meta` (owner/repo/from/to, + clone_sha if recorded) so it
   round-trips. `ref_date`/`period` are NOT stored by P6 and are intentionally not
   reconstructed — enrich falls back to meta["to"] and {from,to} deterministically.

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
            backfill=None, backfill_budget=50):
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
    """
    if warn is None:
        warn = lambda msg: sys.stderr.write(msg + "\n")  # noqa: E731

    # 1. In-window social + code nodes (conceptually in_window: true). These drive
    #    activity counts and seed the spine.
    in_window = graphstore.range_query(conn, project, [repo], ts_from, ts_to)
    in_window_ids = {n["id"] for n in in_window}

    # 2. Seed trains from in-window social nodes, then bounded spine traversal to
    #    surface out-of-window context (in_window: false).
    seeds = [_train_anchor(n) for n in in_window if n["node_class"] == "social"]
    spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth)

    # 2b. Backfill the `missing` spine ids when a seam is injected (slice 7c).
    #     Bounded by `backfill_budget`: once exhausted, warn and stop. After a
    #     successful backfill the node is in the store, so re-traverse — a
    #     backfilled node may itself reference further missing spine nodes, which
    #     we keep resolving within the remaining budget. When `backfill` is None
    #     this whole block is skipped and the original warn-only path runs.
    if backfill is not None and spine["missing"]:
        fetched = set()
        budget_hit = False
        while spine["missing"]:
            progressed = False
            for mid in sorted(spine["missing"]):
                if mid in fetched:
                    continue
                if len(fetched) >= backfill_budget:
                    budget_hit = True
                    break
                fetched.add(mid)
                res = backfill(conn, mid)
                if res and res.get("fetched"):
                    progressed = True
            if budget_hit or not progressed:
                break
            spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth)
        if budget_hit:
            warn("extract: backfill budget ({}) exhausted; {} spine context "
                 "node(s) left un-fetched: {}".format(
                     backfill_budget, len(spine["missing"]),
                     ", ".join(sorted(spine["missing"]))))

    if spine["missing"]:
        warn("extract: {} spine context node(s) referenced but not stored: "
             "{}".format(len(spine["missing"]),
                         ", ".join(sorted(spine["missing"]))))
    # context_ids: reached-but-out-of-window nodes (in_window: false). Recorded for
    # completeness / future per-node tagging; the raw arrays below are sourced from
    # repo_nodes so they already include these (including any just backfilled).
    _context_ids = set(spine["reached"]) - in_window_ids  # noqa: F841

    # 3+4. Materialize raw arrays from the full repo node set + the ledger.
    bundle = {"meta": _materialize_meta(conn, project, repo, ts_from, ts_to)}

    socials = graphstore.repo_nodes(conn, project, repo, node_class="social")
    bundle["prs"] = _by(
        [n["data"] for n in socials if _local(n["id"]).startswith("pr-")],
        key=lambda d: d.get("number"))
    bundle["issues"] = _by(
        [n["data"] for n in socials if _local(n["id"]).startswith("issue-")],
        key=lambda d: d.get("number"))

    # `code` holds both commits (local id = bare <sha>) and artifact nodes
    # (local id `art:<path>` for files, `<path>#<lang>:<subkind>:<name>` for
    # symbols). Commits reconstruct the raw `commits` array; the artifact nodes
    # (everything else) reconstruct the `artifacts` projection below.
    codes = graphstore.repo_nodes(conn, project, repo, node_class="code")
    bundle["commits"] = _by(
        [n["data"] for n in codes if _is_commit_node(n)],
        key=lambda d: str(d.get("sha")))

    # code_events / symbol_events share one ledger: the file-level rows (`<path>`)
    # reconstruct the raw `code_events` array; the symbol-keyed rows
    # (`<path>#<lang>:<subkind>:<name>`, before/after) reconstruct `symbol_events`.
    # Both readers split the ledger by that form so a symbol row is NEVER
    # double-counted into code_events (and vice-versa).
    bundle["code_events"] = _materialize_code_events(conn, project, repo)
    bundle["symbol_events"] = _materialize_symbol_events(conn, project, repo)

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
    arts = {_local(n["id"]): n["data"] for n in codes if _is_artifact_node(n)}
    bundle["artifacts"] = _order_artifacts(arts, bundle["code_events"])

    # people: the per-login modules/areas fold persisted as project-scoped person
    # `structure` nodes (repo sentinel "*", local id `person-<login>`). Read them
    # back into the dict keyed by login, dropping the redundant stored `login`
    # field so the shape matches attribute_people_areas' output. Materialized from
    # the store, NOT re-derived.
    bundle["people"] = _materialize_people(conn, project)

    structure = graphstore.repo_nodes(conn, project, repo, node_class="structure")
    bundle["milestones"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("milestone-")],
        key=lambda d: (d.get("number") is None, d.get("number"),
                       d.get("title") or ""))
    bundle["releases"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("release-")],
        key=lambda d: d.get("tag_name") or d.get("name") or "")

    # Per-repo singleton facts: emit each key only when its node was stored.
    for key, local in _SINGLETON_FACTS:
        value = _materialize_singleton(conn, project, repo, local)
        if value is not None:
            bundle[key] = value

    return bundle


def _order_artifacts(arts, code_events):
    """Order the artifact dict to match build_artifacts' insertion order (the
    code_events source order) so order-sensitive consumers (build_timeline's
    same-(ts,url) tie-break) reproduce byte-for-byte. The order is the first
    appearance of each file artifact's path in `code_events` (the artifact id is
    `art:<path>`). Any artifact never named by a code_event (e.g. symbol nodes)
    is appended last, ordered by id. Pure; does not mutate the records."""
    first_seen = {}
    for i, ev in enumerate(code_events):
        for path in (ev.get("path"), ev.get("old_path")):
            if path:
                first_seen.setdefault("art:" + path, i)
    fallback = len(code_events)

    def rank(aid):
        return (first_seen.get(aid, fallback), aid)

    return {aid: arts[aid] for aid in sorted(arts, key=rank)}


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
    Returns the stored dict, or None if the node was never written (so extract
    does not fabricate an empty key for repos that had no such fact)."""
    node = graphstore.get_node(conn, graphstore.qualify_id(project, repo, local))
    return node["data"] if node is not None else None


def _by(records, key):
    """Sort records deterministically by `key`; stable and idempotent."""
    return sorted(records, key=key)


def _materialize_meta(conn, project, repo, ts_from, ts_to):
    """Reconstruct raw `meta` so it round-trips: owner/repo/window (+clone_sha if
    recorded). ref_date/period are not P6-stored — enrich derives equivalents."""
    meta = {"owner": project, "repo": repo, "from": ts_from, "to": ts_to}
    clone_sha = graphstore.get_clone_sha(conn, project, repo)
    if clone_sha:
        meta["clone_sha"] = clone_sha
    return meta


def _materialize_code_events(conn, project, repo):
    """Reconstruct the raw `code_events` array from the file-level ledger, in
    original source order (ledger rowid order). rename/copy recover `old_path`
    from the event's `detail` (fold_bundle stored old_path there)."""
    prefix = "{}/{}#".format(project, repo)
    out = []
    for ev in graphstore.repo_code_events(conn, project, repo):
        aid = ev["artifact_id"]
        # The repo-qualified remainder is the artifact's local id form: `<path>`
        # for a file event, `<path>#<lang>:<subkind>:<name>` for a symbol one.
        # Skip symbol-granular rows — they are part of the artifact substrate, not
        # this raw file-level `code_events` array (file paths have no `#`).
        remainder = aid[len(prefix):] if aid.startswith(prefix) else _local(aid)
        if "#" in remainder:
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
        out.append(rec)
    return out


def _materialize_symbol_events(conn, project, repo):
    """Reconstruct the raw `symbol_events` array from the SYMBOL-granular ledger
    rows fold persisted, in original source order (ledger rowid order).

    fold_bundle wrote one ledger row per symbol_event, keyed by the symbol
    artifact's local id `<path>#<lang>:<subkind>:<name>` (mirrored from
    gather.fold_bundle), carrying before/after; file-level rows are `<path>` and
    are handled by `_materialize_code_events`. We split SYMBOL rows out by that
    form: the repo-qualified remainder contains a SECOND `#` (the first `#`
    separates `path` from `lang:subkind:name`). A bare `<path>` remainder (file
    event) has no second `#` and is skipped here.

    Per-row reconstruction:
      - strip the `{project}/{repo}#` prefix to recover the local id, then split
        ONCE on `#` -> `path`, `lang:subkind:name`, then `.split(":", 2)` ->
        `[lang, subkind, name]` (name kept whole — may contain `:`/parens/commas).
      - commit/author/date/before/after come straight from the row columns.
      - `change`: the row stored the MAPPED event (derive._SYMBOL_CHANGE_TO_EVENT);
        we reverse it (`_EVENT_TO_SYMBOL_CHANGE`) to a change that recomputes the
        same event in build_artifacts, so the artifact lifecycle round-trips.
    Returns [] when the repo has no symbol rows (the goldens). Source-ordered."""
    prefix = "{}/{}#".format(project, repo)
    out = []
    for ev in graphstore.repo_code_events(conn, project, repo):
        aid = ev["artifact_id"]
        remainder = aid[len(prefix):] if aid.startswith(prefix) else _local(aid)
        # Symbol rows have a second `#`: `<path>#<lang>:<subkind>:<name>`. A bare
        # `<path>` file-level row has none and belongs to code_events, not here.
        if "#" not in remainder:
            continue
        path, _, sym = remainder.partition("#")
        parts = sym.split(":", 2)
        if len(parts) != 3:
            continue  # malformed symbol id (defensive; fold never writes these)
        lang, subkind, name = parts
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
