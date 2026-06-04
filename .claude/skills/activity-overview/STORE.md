# STORE.md — journey-graph schema

`graphstore.py` is a stdlib-only SQLite property graph: the durable,
identity-keyed substrate that `gather` writes and `extract`/`spotlight` read.
This file is the contract those phases (and downstream renderer authors) code
against. All SQL lives in `graphstore.py`; callers use its function API.

> **Phase 7 deliverable (store-only).** This trustworthy graph IS the deliverable.
> `gather --store` writes it and nothing else — there is no longer a flat bundle
> JSON artifact. The bundle is a **transient view** that `extract` materializes
> from the store on demand. The report vertical (`extract → link → render →
> report`) is restored in **Phase 8**. The store stands alone, proven by
> `validate.py` (below), which self-sources everything it needs from the store.

## Identity (qualified ids)

Every node id is namespaced so multi-repo data cannot collide:

- Repo-scoped: `{project}/{repo}#{local}` — e.g.
  `avm/bicep-registry-modules#pr-4821`, `…#issue-17`, `…#<sha>`,
  `…#<path>#<lang>:<subkind>:<name>` (artifacts).
- Project-scoped (people only): `{project}#person-{login}` — a person
  aggregates across all repos in a project.

Helpers: `qualify_id`, `qualify_person`, `parse_id`.

## Node classes (`nodes` table)

One row per entity. Columns: `id` (PK), `project`, `repo`, `node_class`,
`ts`, `data` (JSON blob of the full record), `fetched_at`.

| node_class | holds | `ts` is |
|---|---|---|
| `social` | PRs, issues, comments, reviews | merged/closed/created date |
| `code`   | commits, artifacts (incl. symbols/comments) | author/event date |
| `structure` | code areas, milestones, releases, sprints, people | point-in-time / NULL |

Identity columns (`project`/`repo`/`node_class`) are immutable; `ts`/`data`/
`fetched_at` refresh on re-upsert. Re-folding an overlapping window mutates
nothing already correct — the dedup guarantee is durable, not recomputed.

`structure` nodes typically carry NULL `ts` and are excluded from window
range scans (they are not activity).

## Edges (`edges` table)

`(src_id, dst_id, edge_type, ts, data)`, PK `(src,dst,type)`. Re-upsert
unions, never appends. Edge types:

`closes` (pr→issue), `part_of` (commit→pr), `cross_ref` (issue↔pr↔commit),
`duplicate_of`/`spun_off` (issue→issue), `touches` (commit/pr→area),
`authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted` (person→node),
`owns` (person→area), `depends_on` (area→area, carries version/transitive in
`data`), `replaced_by`/`identity_from` (artifact→artifact), `blocks`
(issue→issue), `in_milestone`/`in_iteration` (social→structure).

### Spine edges and decision trains

A decision train is **not stored** — it is the connected component reachable
from a root issue over the *spine* edge allowlist:
`SPINE_EDGE_TYPES = (closes, part_of, cross_ref, spun_off, duplicate_of)`.
`traverse_spine(seed_ids, max_depth, edge_types)` walks these **undirected**
(both directions), depth-capped, and returns `{reached: {id: depth}, missing:
[id]}`. `missing` ids are spine nodes referenced but not yet stored (e.g. an
out-of-window issue a PR closes) — what a reader backfills.

## Code-event ledger (`code_events` table)

One row per artifact lifecycle event: `(artifact_id, event, commit_sha,
author, date, hunk, ref, before, after, detail)`, PK `(artifact, commit,
event)` → set semantics (re-seeing a commit is a no-op). An artifact's
`lifecycle[]` is `get_code_events(artifact_id)` ordered by date.

## Text index (`fts_text`, FTS5)

`index_text(node_id, text)` (delete-then-insert; idempotent) feeds an FTS5
index; `fts_search(query)` returns matching node ids ranked by relevance.
Created only when the SQLite build supports FTS5 (`fts5_available`); the
text-mining spotlight query depends on it.

`fts_search` passes `query` straight to the FTS5 `MATCH` operator, so the
argument must be a **valid FTS5 query**. Callers searching user-derived terms
(e.g. the P8 spotlight text-mining query) must quote/escape them — FTS5
operators (`AND`, `OR`, `NOT`, `*`, `"`, `-`, `:`) and unbalanced quotes are
significant and otherwise raise `OperationalError`.

## Meta (`meta` table)

Key/value provenance: `schema_version`; `gathered_windows` (JSON list of
folded `{project,repo,from,to}`, deduped — `record_window`/`get_windows`);
`clone_sha:{project}/{repo}` (the tree a repo was last gathered against, for
deterministic resume/roll-up — `set_clone_sha`/`get_clone_sha`).

## Writer (gather --store)

`gather --store PATH` (required) folds its in-memory assembled bundle into the
store via `gather.fold_bundle(conn, bundle)` — the store is the **sole output**
(no bundle file is written), idempotent by identity (re-folding an overlapping
window mutates nothing already correct, so roll-up = a wider window re-fold and
resume = a re-fold against the pinned `clone_sha`). P6
writes: `social` (PRs/issues; comments/reviews stay embedded in the parent's
`data` blob), `code` (commits) + the file-level `code_events` ledger,
`structure` (milestones/releases/areas), and the **spine** edges `closes`,
`cross_ref`, and `part_of`. It also persists the per-repo **singleton facts**
`workflow_stats`, `code_graph`, `code_owners`, and `label_taxonomy` — each as a
whole-dict `structure` node under a well-known local id (`workflowstats`,
`codegraph`, `codeowners`, `labeltaxonomy`) with NULL `ts` (excluded from window
scans), identity-keyed and idempotent. A singleton node is written only when its
source value is present and non-empty, so a reader never reconstructs a
fabricated empty key.

**Slice 7b-1 (step 2) additionally persists the artifact substrate** (derived on
the write path via the shared `derive.py` leaf module — `derive.build_artifacts`
/ `derive.link_symbol_identity`):

- **Artifact `code` nodes.** Each lifecycle-tracked artifact upserts as a `code`
  node keyed on its own id form — local id `art:<path>` for file artifacts
  (readme/doc/example) and `<path>#<lang>:<subkind>:<name>` for symbol/comment
  artifacts — with `ts` = the artifact's last lifecycle-event date and `data` =
  the full artifact record. Additive to (and distinct from) the commit `code`
  nodes, which keep their bare-`<sha>` local id. Idempotent by id.
- **`symbol_events` ledger.** The symbol-granular lifecycle
  (`gather.parse_symbol_events`) is persisted into the `code_events` table keyed
  by the SYMBOL artifact id, carrying the rich `before`/`after` fields (file-level
  rows leave them NULL). A symbol artifact's `lifecycle[]` is
  `get_code_events(<symbol artifact id>)` in date order.
- **Symbol-move edges.** Confident window-wide symbol moves
  (`derive.match_symbol_moves`) upsert `replaced_by` (src→dst) and
  `identity_from` (dst→src) artifact→artifact edges carrying
  `move_confidence`/`move_basis` in the edge `data`. Idempotent by (src,dst,type).

**Slice 7b-1 (step 3) additionally persists people + the contribution / non-spine
edges** (all derived on the write path via the shared `derive.py` leaf —
`derive.enumerate_participants` / `derive.attribute_people_areas` /
`derive.area_index` / `derive._commit_areas`):

- **Person `structure` nodes (project-scoped) = ALL participants.** `people` is
  the FULL participant set: EVERY login that carries any contribution edge gets a
  person node — `pr.author`, `pr.merged_by`, each `pr.reviewers`, `issue.author`,
  commit `author`, and comment / review-comment authors. The single source of
  truth is `derive.enumerate_participants(bundle)` (the **anti-drift enumerator**):
  the write path (`gather.fold_bundle`) AND the auditor (`validate.no_drift`) both
  call it, so they can never disagree on "who". Contributors (commit authors +
  reviewers of PRs with mapped commits) carry the `{modules, areas}` that
  `attribute_people_areas` derives; pure participants (comment-only, issue
  reporters, out-of-window PR authors, reviewers of mapped-commit-less PRs) get
  empty `modules`/`areas`. Every login is **bot-tagged** — `is_bot = True` when it
  matches `*[bot]`, `github-actions`, `microsoft-github-policy-service`,
  `*-organizer`, or `copilot-*` (`derive.is_bot_login`); bots are TAGGED, never
  dropped (dropping = losing data). Each upserts as a `structure` node id
  `qualify_person(project, login)` (`{project}#person-{login}`), NULL `ts`,
  `data` = `{login, modules, areas, is_bot}`. The `repo` column is the project
  sentinel `"*"` (people aggregate across a project's repos), so re-folding the
  same login from another repo upserts the identical id — one node, never a
  cross-repo duplicate — and the sentinel keeps the row out of any single repo's
  `repo_nodes` view. Idempotent by id.

  > Before this fix, person-node creation was driven by `attribute_people_areas`
  > alone (commit authors + reviewers of mapped-commit PRs), while contribution
  > edges were written for EVERY participant — so a real AVM gather had 222 logins
  > with contribution edges (commented/reported/authored/reviewed) but NO person
  > node, leaving thousands of dangling person→node edges. The shared enumerator
  > closes that gap.
- **Contribution edges (person→node).** From the RAW records, idempotent by
  (src,dst,type), skipping absent logins: `authored` (person→pr from `pr.author`,
  person→commit from `commit.author`), `merged` (person→pr from `pr.merged_by`),
  `reviewed` (person→pr from `pr.reviewers`), `reported` (person→issue from
  `issue.author`), `commented` (person→pr/issue from `comments_list` +
  `review_comments` authors). `reacted` is **not** written: reactions are stored
  as aggregate counts (`summarize_reactions`), with no per-reactor login to key on.
- **`owns` (person→area)** from `code_owners` — each owner of a path-prefix owns
  every area whose paths fall under that prefix. **`touches` (commit→area)** from
  `derive._commit_areas` (a commit's files → the areas they land in). **`depends_on`
  (area→area)** from `code_graph["edges"]`, carrying `{version,transitive,…}` in
  the edge `data`. **`in_milestone` (social→structure)** from a PR/issue's
  `milestone` title → the milestone node (keyed on number when present).

The contribution / `owns` / `touches` / `depends_on` / `in_milestone` edges
never leak into `extract`: every one is non-spine, so `traverse_spine` never
follows it, and extract reads none of them.

**Slice 7b-2 — reader contract: `extract` materializes `artifacts` + `people`
from the stored nodes; `link.enrich` shrank.** Because the write path now derives
those two projections identically to how `enrich` used to (store-derived ==
link-derived, locked by `test_characterization.py`), `extract` no longer emits a
RAW-only view — it additionally reconstructs:

- **`artifacts`** by reading the artifact `code` nodes (the non-commit ones —
  local id `art:<path>` / `<path>#…`), keyed by that local id (== `artifact_id`).
  extract restores build_artifacts' code_events insertion order so order-sensitive
  consumers (`build_timeline`'s same-`(ts,url)` tie-break) reproduce byte-for-byte.
- **`people`** by reading the project-scoped person `structure` nodes (repo
  sentinel `"*"`, local id `person-<login>`), keyed by login (the redundant stored
  `login` field is dropped). Because fold now persists ALL participants, this
  naturally returns the full set (each record carries `modules`/`areas`/`is_bot`).

extract READS these — it does NOT re-derive via `build_artifacts` /
`attribute_people_areas`. Correspondingly `link.enrich` removed its
`build_artifacts` + `attribute_people_areas` calls and now CONSUMES
`bundle["artifacts"]`/`bundle["people"]` (with defensive
`setdefault("artifacts", {})` / `setdefault("people", {})` so a raw bundle fed
straight to enrich never KeyErrors and never silently recomputes them). enrich
still owns the window-only projections (trains/buckets/timeline/feature_deltas/
code-area attribution/modules/forecast/symbol-identity). `build_artifacts` and
`attribute_people_areas` remain re-exported from `link` for direct callers.

Still NOT on the write path after step 3: `blocks` (issue→issue) and
`in_iteration` (social→sprint) — skipped for lack of source data in the current
fixtures/normalizers (no block/sprint signal is gathered) — and the `fts_text`
FTS5 index (`index_text` is never called on the write path). See
`docs/superpowers/specs/2026-06-04-activity-phase7-substrate.md`.

## Backfill on a traversal miss (slice 7c)

A reader (`extract`) traversing a decision train over the spine edges can hit a
`missing` id — a spine edge pointing at a node never gathered (e.g. a windowed PR
`closes` an issue opened/closed *before* the window). `gather.backfill(conn, id,
fetch=…)` closes that gap on demand: it fetches THAT ONE node + its cheap
immediate spine edges and upserts them by identity. It is the **only** network
call outside the main Acquire pass, so it lives in `gather.py` (`extract` never
fetches itself — it calls in via an injected `backfill` callable).

- **Idempotent / no-op when present.** `backfill` first checks `get_node(id)`;
  if the node already exists it returns `{"fetched": False, …}` WITHOUT fetching.
  The fetched node is **durable** (upserted), so it persists for the next window
  and a re-run is a no-op — the backfilled node appears exactly once.
- **Injectable network seam.** The actual fetch goes through a `fetch(kind,
  local, qid)` callable (`None` == unfetchable; `{"node": raw, "edges":
  [(dst_local, edge_type), …]}` == fetched). `classify_id` derives `kind`
  (social `pr-`/`issue-`/`comment-`, code bare-`<sha>`, else structure) from the
  local id form. Production wires `gather.make_backfill_fetcher(token,
  clone_dir)` (REST `normalize_pr`/`normalize_issue` for social — re-deriving the
  same `closes` spine edges fold does; bounded `git fetch --depth 1 <sha>` +
  one-commit log for code; structure not backfilled on demand). The test suite
  passes a **fixture-backed fake** — NO network. Only spine edge types from the
  seam are honored (backfill closes a traversal gap, it does not re-derive the
  non-spine substrate).
- **Reader wiring + budget.** `extract.extract(…, backfill=None,
  backfill_budget=50)`: when `backfill` is injected and traversal yields
  `missing` ids, extract calls it per id under the per-window budget ceiling, then
  re-traverses so a backfilled node that itself references further missing spine
  nodes is resolved within the **remaining** budget. Once the budget is exhausted
  it WARNS and stops (never unbounded). When `backfill is None` (the default, and
  what every existing test/characterization uses) the path is byte-identical to
  before — `missing` ids are only warned about.
- **Context-only (documented gap).** A backfilled node is pulled into the bundle
  as out-of-window **context** (it was reached via the spine but is outside the
  range query), so it does NOT count toward in-window activity. The read pipeline
  that wires `backfill=gather.backfill` in production is not landed yet (extract
  is still read only by tests today); `make_backfill_fetcher`'s live REST/git
  paths are therefore only exercisable against a real repo (relevant to the
  upcoming trust gate), not by the offline suite.

## Roll-up / resume (slice 7c)

A multi-window "wider view" is a **single wider `range_query`** over the union of
folded windows — a `WHERE ts BETWEEN …`, NOT a multi-bundle merge. Folding two
adjacent windows (March, April) then range-querying `[March-start, April-end]`
returns the union of their in-window nodes; each narrow window still returns only
its own. Overlapping re-folds are idempotent (identity-keyed nodes/edges, set
semantics on the ledger, deduped `gathered_windows`), so re-extracting an
overlapping window is byte-stable. Resume/roll-up read structure from the latest
`clone_sha` (`set_clone_sha`/`get_clone_sha`) and the `gathered_windows` ledger
(`record_window`/`get_windows`): the structure pin is the newest fold's
`clone_sha`, a `WHERE`, not a merge.

## Trust gate (validate.py)

`python3 validate.py STORE.db` audits the store for trustworthiness and exits
non-zero on any ERROR (CI-gateable). It is **self-contained on a store**: the two
real-data checks — `no_drift` (stored people/artifacts == freshly re-derived) and
`idempotency` (a re-fold changes no counts) — need a raw bundle to re-derive
against, and when none is passed they **self-source it from the store** via
`extract` over the store's full activity window. A `--bundle` argument is an
optional cross-check, never required. The auditor never mutates the store and
survives a corrupt one (a re-derive/re-fold that raises becomes a failed ERROR
check, not a crash). This is what makes the store a stand-alone deliverable.

## Determinism

`data` blobs serialize with `sort_keys=True`; `range_query` orders by
`(ts, id)`. Given fixed inputs the store is byte-stable (modulo `fetched_at`).
