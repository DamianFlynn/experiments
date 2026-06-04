# STORE.md — journey-graph schema

`graphstore.py` is a stdlib-only SQLite property graph: the durable,
identity-keyed substrate that `gather` writes and `extract`/`spotlight` read.
This file is the contract those phases (and downstream renderer authors) code
against. All SQL lives in `graphstore.py`; callers use its function API.

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

`gather --store PATH` folds its assembled bundle into the store via
`gather.fold_bundle(conn, bundle)` — additive to the JSON bundle, idempotent by
identity (re-folding an overlapping window mutates nothing already correct). P6
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

Still written by a later **slice 7b-1 step 3** (lifting the remaining link
derivations onto the write path — see
`docs/superpowers/specs/2026-06-04-activity-phase7-substrate.md`): the
**normalized** people nodes (link-derived) and the contribution / non-spine edges
(`authored`/`reviewed`/`touches`/`owns`/…).

## Determinism

`data` blobs serialize with `sort_keys=True`; `range_query` orders by
`(ts, id)`. Given fixed inputs the store is byte-stable (modulo `fetched_at`).
