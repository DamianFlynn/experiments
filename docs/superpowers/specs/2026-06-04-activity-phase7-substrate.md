# Phase 7 — full graph substrate (extract + persisted derived facts) — design

**Status: PROPOSED.** Detailed design for **Phase 7** of the journey-graph substrate,
expanding and superseding the one-line P7 ledger entry in the rev-14 design
(`2026-06-01-activity-overview-design.md` → *Implementation phasing*). The rev-14 ledger
split "extract + raw round-trip" (P7) from "the link-derived graph" (folded into P8). This
spec **collapses that split**: Phase 7 stands up the **complete** substrate — the persisted
*facts* a reader needs — built as three **gated vertical slices** so the tree is never red
for long and the linchpin equivalence test stays green at every step. Spotlight (P8) is
unchanged in scope but is now *unblocked* by slice 7b, which persists the people/artifact
graph spotlight queries.

This is a design/spec; the per-slice implementation **plans** land under
`docs/superpowers/plans/` as each slice starts, in the established pattern (cf. the P5
graphstore plan).

---

## Why full substrate now (the decision)

The rev-14 schema (design doc *Section 1 — artifact → node mapping*) already makes the
"link-derived" entities **first-class stored nodes**: `code` nodes include `artifacts[]`
(incl. symbols/comments); `structure` nodes include `people[]`; edges include
`replaced_by`/`identity_from` (symbol moves) and the contribution edges
`authored`/`reviewed`/`owns`/`touches`/`depends_on`. STORE.md already says these are written
"by a later phase." Deferring them to P8 would mean **writing that persistence later anyway**
— and, worse, keeping a window where the graph is only half the fact base. So Phase 7 builds
the whole substrate. Two facts make this tractable rather than a big-bang risk:

1. **The "link-derived" data is *derived-from-raw via pure functions*.** `build_artifacts`,
   `match_symbol_moves`, `attribute_people_areas`, `build_modules` already exist in `link.py`
   and take only the raw bundle as input. We make them **shared** (exactly as `resolve_commit_pr`
   was shared) and call them on the **write path** to populate the store. No new data source,
   no networked "link-as-writer."
2. **`link` doesn't disappear — it *shrinks*** to the pure **window projections** that genuinely
   aren't stored (trains via traversal, buckets, timeline, feature_deltas, train
   significance/effort/areas, forecast). The design doc anticipates exactly this ("later phases
   shrink because the store does the heavy lifting").

### The persistence model (the conceptual core)

The decision rests on one rule, and the slices below are just its mechanics:

> **Facts are persistent; only the denormalized roll-ups are recomputed — and they are
> recomputed *from* persistent facts, so nothing is ever lost.**

Concretely:

- **Persistent (the graph is source of truth):** every node (social/code/structure, including
  artifact and person nodes) and every edge (spine + contribution + structural), the
  `code_events`/`symbol_events` ledgers, and the FTS text index. These are upserted by identity,
  unioned never appended — durable dedup, not recomputed-per-read.
- **A decision train *is* persistent — as its subgraph, not as a row.** Every node in a train
  and every spine edge (`closes`/`part_of`/`cross_ref`/`spun_off`/`duplicate_of`) is durably
  stored. "Extract a train" means **traverse** those durable edges (`traverse_spine`), which is
  cheap and lossless *because the edges already exist*. The only thing rev-14 kills is the
  redundant precomputed `trains[]` **array** — storing the roll-up would store the same facts
  twice and invite drift. Train = *stored as subgraph, materialized as a view.*
- **Backfill persists too.** When a reader traversing a train hits a spine edge pointing at a
  node never gathered (a *missing thread* — e.g. a windowed PR closing an out-of-window issue),
  `backfill(id)` fetches that one node + its immediate edges and **upserts it into the store
  (permanent)**; it then naturally appears in this window's projection. Stored in the graph
  **and** present in the projection — and durable for every later overlapping window.
- **Recomputed (never stored as standalone arrays):** the roll-ups/projections — `trains[]`,
  `buckets`, `timeline`, `feature_deltas`, `modules`, `forecast`, and the window-scoped
  `artifacts[]`/`people[]` *views*. Each is a deterministic read of persistent facts for a
  window, so nothing is lost by not caching it.

This is why the equivalence gate is **end-to-end**: it must prove that *traversing the
persistent graph and reading persistent facts* reproduces the rev-13 roll-ups **identically**
— i.e. that "stored as subgraph / facts, materialized as view" is behaviorally indistinguishable
from "stored as arrays."

---

## Starting point — what P6 already gives us

From STORE.md (*Writer*), `gather --store` (P6, merged) already persists, idempotently by
identity:

- `social` — PRs/issues (comments/reviews embedded in the parent's `data` blob).
- `code` — commits + the file-level `code_events` lifecycle ledger.
- `structure` — milestones / releases / code areas.
- **spine edges** — `closes`, `cross_ref`, `part_of`.

Explicitly **deferred** by P6 (STORE.md): "People & artifact nodes (link-derived),
`symbol_events`, and all non-spine edges (`authored`/`reviewed`/`touches`/`owns`/…)" — plus
there is **no reader yet** (`extract.py` does not exist). Phase 7 closes exactly that gap.

---

## The slices

Each slice ships **green** under the same end-to-end equivalence gate (below). The only thing
that changes between 7a → 7b is **where derivation happens**, never the bundle the reader emits.

### Slice 7a — `extract.py` + raw round-trip (the linchpin)

**Goal:** stand up the reader and prove the substrate swap is invisible to everything from
`link` onward, using the facts already (or trivially) stored.

- **`extract.py` (new reader).** Window read in three bounded steps (design doc Section 3):
  1. **range query** — `WHERE project=? AND repo IN (…) AND ts BETWEEN ? AND ?` → in-window
     nodes, each flagged `in_window: true`;
  2. **seed trains** — each in-window social node → its train anchor id;
  3. **bounded spine traversal** — recursive CTE per seed over the spine allowlist, depth-capped,
     pulling out-of-window spine nodes as `in_window: false` (context, not activity); absent
     spine nodes recorded as `missing` (backfill is wired in 7c, **not** here — 7a warns).
  Then **materialize the rev-13 raw bundle arrays** from node `data` blobs + the `code_events`
  ledger. Activity counts/buckets only ever sum `in_window` nodes.
- **Complete the *raw* writer gap** so every raw array round-trips: persist `symbol_events`
  (the symbol-granular ledger), `workflow_stats`, and the `code_graph`/`code_owners` raw
  projections + label taxonomy in whatever node/edge/meta form `extract` reads them back from.
  (Contribution/owns/touches/depends_on **edges** and artifact/person **nodes** are 7b — 7a only
  needs what the *raw* arrays require.)
- **Roll-up / resume collapse into the store** — a "6-month view" is a wider range query (no
  multi-bundle union); structure comes from the latest `clone_sha` (a `WHERE`, not a merge).

**Gate (the ⭐ linchpin):** seed a store from the existing raw fixtures (`git_log_*`,
`rest_*`/`graphql_*`) → `extract` a window → run **`link` / `render` / `report` unchanged** →
assert the full **enriched** golden `bundle_*.json` reproduces byte-for-byte. Here `link` still
derives `trains`/`artifacts`/`people` from `extract`'s raw output — that is the proof the swap
is invisible.

**Verifiable exit:** the golden-bundle test is green with `link` untouched; re-extracting an
overlapping window is identical; a wider window is a single range query.

### Slice 7b — persist the derived facts; `link` shrinks to projections

**Goal:** make the **graph the single source of truth** for artifacts and people (the
substrate spotlight needs), without computing them twice.

- **Shared derivation module.** Lift `build_artifacts`, `match_symbol_moves`,
  `attribute_people_areas`, `build_modules`, and the contribution/structural edge derivations
  out of `link.py` into a shared module both the write path and `link` can import (the
  `resolve_commit_pr` precedent).
- **Write path persists the derived facts:** **artifact nodes** (`code`, incl. symbols/comments),
  **person nodes** (`structure`, project-scoped), and the **non-spine edges** —
  `authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted` (person→node), `owns`
  (person→area), `touches` (commit/pr→area), `depends_on` (area→area), `replaced_by`/
  `identity_from` (artifact→artifact), `blocks`, `in_milestone`/`in_iteration`. All
  identity-keyed, unioned never appended.
- **`extract` materializes `artifacts[]` / `people[]` / `modules{}` by *reading* the stored
  nodes/edges** (window-scoped), instead of re-deriving them.
- **`link.enrich` shrinks** to the genuine window projections it still owns: traverse-based
  `trains`, `buckets`, `timeline`, `feature_deltas`, `score_train_significance`,
  `annotate_train_effort`, `attribute_train_areas`, `build_forecast`. It no longer calls the
  lifted derivations — the store/extract supply them.

**Gate:** the **same** end-to-end golden-bundle test stays green — now additionally proving
**store-derived == link-derived** (no drift between the persisted facts and the old in-line
derivation). New unit tests assert artifact/person nodes and each edge type round-trip and that
the window-scoped `artifacts[]`/`people[]` views match the legacy arrays.

**Verifiable exit:** golden bundle still byte-identical; the store now contains artifact +
person nodes and all contribution edges; **P8 spotlight queries are unblocked** (person impact,
subsystem split, pattern evolution all have their nodes/edges).

### Slice 7c — backfill on traversal miss + roll-up/resume hardening

**Goal:** close the train completeness loop and the cross-window story.

- **Wire backfill.** `extract`'s spine traversal calls `gather.backfill(id)` on each `missing`
  spine node, under a **per-window budget** (warn, don't fetch unboundedly). Backfill is the
  only network call outside Acquire, stays in `gather.py` (`extract` calls
  `gather.backfill(id)`; it never fetches itself). The fetched node + its immediate edges are
  **upserted (durable)** and appear in this projection; idempotent, a no-op if already present.
- **Roll-up / resume hardening.** A multi-window "6-month view" is one wider range query;
  resume uses `clone_sha` + `gathered_windows` (the structure is the latest `clone_sha`, a
  `WHERE`, not a merge).

**Gate:** a windowed PR closing an **out-of-window** issue traverses as **one complete train**;
re-running is a no-op (the backfilled node appears exactly once and persists for the next
window); traversal/backfill respect the spine allowlist, depth cap, and budget ceiling.

**Verifiable exit:** cross-thread trains read complete; backfilled facts are durable across
windows; overlapping re-gather/re-extract never duplicates.

---

## The equivalence gate (invariant across all slices)

> **At every slice, `extract` → (`link`) → `render`/`report` reproduces the existing enriched
> golden `bundle_*.json` byte-for-byte, and all existing pipeline tests pass unchanged.**

The fixtures (`git_log_*`, `rest_*`/`graphql_*`, golden `bundle_*.json`) are the oracle. The
*output* never changes across 7a → 7b → 7c; only the *provenance* of each field moves
(7a: `link` derives from raw; 7b: store/extract supply the derived facts, `link` shrinks; 7c:
backfill completes traversal). **No slice ships without this test green.** Per the substrate
testing strategy (design doc Section 5), it is layered with: graphstore unit (in-memory
SQLite); idempotency/accumulation (fold twice → identical store; overlapping windows union);
determinism (`(ts, id)` sort → byte-stable view); traversal & backfill bounds; cross-repo
identity; multi-commit chronology; and a ~50k-node scale smoke for the window query.

TDD throughout (`superpowers:test-driven-development`); verification-before-completion before
any "green" claim.

---

## Component changes

| Component | Change |
|---|---|
| `extract.py` | **new** — window range query + train seeds + bounded spine traversal → materialized rev-13 bundle view. 7a: raw arrays. 7b: also materializes `artifacts[]`/`people[]`/`modules{}` from stored nodes/edges. 7c: calls `gather.backfill(id)` on traversal miss. |
| `graphstore.py` | additions as needed for symbol_events ledger access, artifact/person node + non-spine edge writers/readers, window-scoped artifact/people queries. Still owns *all* SQL. |
| shared derive module | **new (7b)** — `build_artifacts` / `match_symbol_moves` / `attribute_people_areas` / `build_modules` + edge derivations lifted from `link.py`, imported by both the write path and `link`. |
| `gather.py` | 7a: persist remaining raw facts (`symbol_events`, `workflow_stats`, code_graph/owners projections). 7b: invoke shared derivations on the write path to persist artifact/person nodes + non-spine edges. 7c: `backfill(id)` wired as the on-miss bridge. |
| `link.py` | 7b: **shrinks** — stops calling the lifted derivations; keeps the window projections (trains/buckets/timeline/feature_deltas/significance/effort/areas/forecast). |
| `STORE.md` | update the *Writer* section: artifact/person nodes, `symbol_events`, and non-spine edges are now written (in 7b), no longer "a later phase." |
| `render.py` / `report-template.md` | **unchanged** — read the materialized view exactly as before; guarded by the equivalence gate. |

---

## Out of scope (later phases, unchanged)

- **Spotlight queries themselves (P8)** — person-impact / subsystem-split / pattern-evolution /
  commit-text-mining renders. 7b persists the substrate they read; the queries land in P8.
- **Multi-repo end-to-end (P9)** — qualified-id namespacing exists in the schema and is exercised
  in unit tests here, but cross-repo trains / project-scoped people across repos / Terraform
  aggregation are P9.
- **Sub-agent train narration (P10)**, people/community + flow report views (P11), Projects v2
  (P12), series continuity (P13), transcript + slash command (P14).
