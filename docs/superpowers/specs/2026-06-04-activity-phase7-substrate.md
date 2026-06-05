# Phase 7 — full graph substrate (extract + persisted derived facts) — design

**Status: IMPLEMENTED (slices 7a–7c + trust gate + store-only wrap-up).**
**Phase 7's deliverable is the trustworthy journey-graph store** — proven by the
trust gate (`validate.py`) and self-contained on the store alone. `gather` writes
only the store — there is no flat-bundle file. The end-to-end report vertical
(`extract → link → render → report`) **composes from the store**: `extract`
materializes the bundle view and the existing (preserved) `link`/`render`/`report`
code consumes it, guarded by the golden-bundle equivalence gate (and exercised by
`test_render` via `extract`). Wiring this into a single one-shot reader command is
a minor, decoupled integration — **not** a roadmap phase (rev-14 Phase 8 is
`spotlight`). See *Wrap-up* below.

Detailed design for **Phase 7** of the journey-graph substrate,
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

**Gate (the ⭐ linchpin):** seed a store by folding a golden `bundle_*.json` (`fold_bundle`
consumes only raw keys) → `extract` its window → run **`link` / `render` / `report` unchanged**
→ assert `enrich(extract) == enrich(golden_raw)`. Here `link` still derives
`trains`/`artifacts`/`people` from `extract`'s raw output — that is the proof the swap is
invisible. **Note (verified during 7a):** the checked-in goldens were last regenerated at
Phase 3a and are *stale* relative to the current `link.py` (which since grew
people/modules/forecast/symbol_moves), so `enrich(golden_raw) ≠ golden` and the literal
`extract→enrich == golden` is unachievable without regenerating fixtures. The faithful,
stronger-isolating gate runs **both** `extract`'s reconstruction and the golden's own raw keys
through the *current* `enrich` and asserts equality — pinning exactly the raw substrate
`extract` owns, immune to churn in the derived layer. (`test_link.py` already treats these
goldens as raw *input* to `enrich`.) A few non-reconstructible/volatile `meta` fields
(`ref_date`, `period`, `generated_at`, `schema_version` — none consumed by `enrich`) are
dropped in the comparison.

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

### Slice 7c — backfill on traversal miss + roll-up/resume hardening — IMPLEMENTED

**Goal:** close the train completeness loop and the cross-window story.

- **Backfill wired (`gather.backfill(conn, id, fetch=…)`).** On a traversal MISS, `extract`
  asks gather (via an injected `backfill` callable) to fetch THAT ONE node + its cheap
  immediate spine edges and upsert them. Backfill is the only network call outside Acquire and
  stays in `gather.py` (`extract` never fetches itself — it has no top-level `import gather`;
  the dependency is injected). It is **idempotent**: `get_node(id)` present ⇒ no-op, NO fetch.
  Returns `{"fetched", "id", "edges_added"}`. The fetched node is **durable** (upserted) and
  appears in the projection as out-of-window **context** (`in_window: false`) — a backfilled
  node does NOT count toward in-window activity (documented gap).
  - **Network seam (no real network in the suite).** `fetch(kind, local, qid)` is injectable;
    `gather.classify_id` derives `kind` from the local id (`pr-`/`issue-`/`comment-` social,
    bare `<sha>` code, else structure). Production wires `gather.make_backfill_fetcher(token,
    clone_dir)` (REST `normalize_pr`/`normalize_issue` re-deriving fold's `closes` edges; bounded
    `git fetch --depth 1 <sha>` + one-commit log for code; structure not on-demand). Tests pass
    a **fixture-backed fake**. Only spine edge types from the seam are honored.
  - **Budget ceiling.** `extract.extract(…, backfill=None, backfill_budget=50)` calls backfill
    per `missing` id under the budget, re-traversing so a backfilled node referencing further
    missing spine nodes resolves within the remaining budget; budget-exhausted ⇒ WARN + stop.
    `backfill=None` (the default) is byte-identical to the prior warn-only path (characterization
    untouched).
- **Roll-up / resume hardening.** A multi-window "wider view" is one wider `range_query` over the
  union of folded windows (a `WHERE ts BETWEEN …`, NOT a multi-bundle merge); overlapping re-fold
  is idempotent (proven at the extract level). Resume/roll-up read structure from the latest
  `clone_sha` (`set_clone_sha`/`get_clone_sha`) + the `gathered_windows` ledger
  (`record_window`/`get_windows`) — a `WHERE`, not a merge.

**Gate (met, `test_backfill.py`):** a windowed PR closing an **out-of-window** issue traverses as
**one complete train** after backfill (`traverse_spine` reaches it with `missing == []`);
re-running is a no-op (the backfilled node appears exactly once and persists); traversal/backfill
respect the spine allowlist, depth cap, and budget ceiling.

**Verifiable exit (met):** cross-thread trains read complete; backfilled facts are durable across
windows; overlapping re-gather/re-extract never duplicates. **Deferred:** no production reader
wires `backfill=gather.backfill` yet (extract is read only by tests); `make_backfill_fetcher`'s
live REST/git paths are exercisable only against a real repo — relevant to the upcoming trust gate.

---

## Wrap-up — store-only deliverable (the Phase 7 close)

After 7a–7c and the trust gate, Phase 7 makes the **store the SOLE deliverable**
and removes the now-dead flat-bundle code paths. Approved scope: *"Full:
store-only"*: `gather` produces only the store. The report vertical still
**composes from the store** (`extract → link → render`, equivalence-gated); only
the flat-bundle *file* and its one-shot driver are gone — re-wiring a single reader
command is a minor decoupled integration, not a roadmap phase.

- **`gather` → store-only.** `--store` is **required**; `gather` folds the
  in-memory bundle into the store and writes **no bundle JSON file**. Removed:
  the `--out` flag and the bundle-file `json.dump` emission; `--resume`
  (flat-bundle resume) and `--rollup` (multi-bundle union) flags **and their
  implementations** (`resume_acquire`, `resume_bundle`, `rollup_bundles`,
  `_period_to`, `ROLLUP_ACTIVITY_KEYS`/`ROLLUP_LATEST_FIELDS`, and the
  `only_status` resume branch in `extract_iac_edges`). Both are **superseded by
  the store**: roll-up = a wider `range_query`; resume = a re-fold against the
  pinned `clone_sha` (idempotent dedup). `build_bundle`/`fold_bundle` are KEPT
  (the bundle still exists transiently in-memory; only the FILE artifact and the
  flat resume/rollup paths go).
- **`validate` → self-sourcing.** `validate STORE.db` (no `--bundle`) now
  reconstructs the raw bundle FROM THE STORE via `extract` (over the store's full
  window) and runs `no_drift` + `idempotency` against it, so the trust gate is
  fully self-contained on a store. `--bundle` remains an optional cross-check but
  is **unnecessary**. The auditor survives a corrupt store (a re-derive/re-fold
  that raises becomes a failed ERROR check, not a crash).
- **Kept (the vertical composes from the store):** `extract`, `derive`, `link`,
  `render`, `report`, and the `bundle_*.json`/`char_*.json` fixtures are all
  preserved. `extract → link → render` materializes the bundle view from the store
  and produces the report — no derivation/render code is rewritten (the equivalence
  gate guards this; `test_render` exercises it via `extract`). A one-shot reader
  command is an optional follow-up integration, decoupled from the roadmap.

## The equivalence gate (invariant across all slices)

> **At every slice, for each golden, `enrich(extract(store)) == enrich(golden_raw)` — i.e.
> `extract` → (`link`) → `render`/`report` yields what the golden's raw keys do through the
> current `enrich` — and all existing pipeline tests pass unchanged.** (See the 7a note above on
> why this enrich-equivalence form, not literal byte-for-byte against the stale goldens.)

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
| `gather.py` | 7a: persist remaining raw facts (`symbol_events`, `workflow_stats`, code_graph/owners projections). 7b: invoke shared derivations on the write path to persist artifact/person nodes + non-spine edges. 7c: `backfill(id)` wired as the on-miss bridge. **Wrap-up: store-only** — `--store` required, no bundle file; `--out`/`--resume`/`--rollup` + their impls removed. |
| `link.py` | 7b: **shrinks** — stops calling the lifted derivations; keeps the window projections (trains/buckets/timeline/feature_deltas/significance/effort/areas/forecast). **Wrap-up:** drop the dead `derive` re-exports used only by tests (`artifact_id`/`build_artifacts`/`area_index`/`_area_for_path`/`attribute_people_areas`/`match_symbol_moves`); KEPT as a deferred Phase-8 module. |
| `validate.py` | trust gate (per-invariant store audit). **Wrap-up:** `no_drift`/`idempotency` **self-source** the raw bundle from the store via `extract`; `--bundle` optional. |
| `STORE.md` | update the *Writer* section: artifact/person nodes, `symbol_events`, and non-spine edges are now written (in 7b), no longer "a later phase." **Wrap-up:** store is the sole deliverable; add the *Trust gate* section. |
| `render.py` / `report-template.md` | **unchanged and KEPT** — read the materialized view exactly as before; the vertical composes from the store via `extract` (equivalence-gated; exercised by `test_render`). Only the one-shot flat-bundle driver is gone. |

---

## Out of scope (later phases, unchanged)

- **Spotlight queries themselves (P8)** — person-impact / subsystem-split / pattern-evolution /
  commit-text-mining renders. 7b persists the substrate they read; the queries land in P8.
- **Multi-repo end-to-end (P9)** — qualified-id namespacing exists in the schema and is exercised
  in unit tests here, but cross-repo trains / project-scoped people across repos / Terraform
  aggregation are P9.
- **Sub-agent train narration (P10)**, people/community + flow report views (P11), Projects v2
  (P12), series continuity (P13), transcript + slash command (P14).
