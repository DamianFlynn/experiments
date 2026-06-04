# Phase 8 — spotlight (substrate analytics reader) — design

**Status: PROPOSED.** Detailed design for **Phase 8** of the journey-graph substrate
(rev-14 ledger: *"Phase 8 — spotlight; absorbs original-P5 people view"*). Phase 7
made the graph the trustworthy source of truth; Phase 8 adds **`spotlight.py`**, a
second reader for **parameterized analytics queries orthogonal to the window** —
the questions the flat bundle could never answer cheaply.

Per-slice implementation plans land under `docs/superpowers/plans/` as each slice
starts, in the established pattern.

---

## What spotlight is (and is not)

`extract` answers *"what happened in this window?"* (a range query + spine
traversal → the rev-13 bundle view). **`spotlight` answers cross-cutting questions
orthogonal to any one window** — *"what has this person done across all repos?",
"how did this symbol evolve over its whole history?", "who/what touches this
subsystem?", "everywhere this phrase appears."*

Design rules (rev-14 Section 4):
- **Bounded SQL (+ CTE / FTS)** over the store — no full scans of JSON. Each query
  is `O(matches)` / index-bounded, not `O(history)`.
- **Deterministically ordered** results (explicit `ORDER BY`), so the model narrates
  the same output every run and tests are byte-stable.
- **Citation discipline** — every row carries its source ref (`url` / `sha` /
  `number`), same as the report. Spotlight surfaces facts the model *cites*, never
  prose it invents.
- **Never re-fetches.** A query whose answer needs un-gathered data returns a
  structured *"gather that window/repo first"* guidance result, not a partial lie.
- **Reader only.** `spotlight` imports `graphstore` (+ `derive` for shared shaping);
  it never writes the store and never calls the network.

`spotlight` reuses the **trust gate's** guarantee: it reads the same nodes/edges/
ledgers `validate.py` audits, so a spotlight answer is exactly as trustworthy as the
graph (which Phase 7 proved).

---

## Output contract — the chronological delivery train

Every query returns a **full, chronological "delivery train"**: a time-ordered,
fully-cited sequence of what the focus delivered — **not aggregate counts**. The AI
narrates it as a story; the citations are the evidence.

- **Person / subsystem foci group by *decision train*** — the unit of delivery.
  Each train the focus **touched in any role** appears (authored, reviewed, merged,
  reported, or even a *single pivotal comment*), ordered chronologically, carrying:
  the train's `outcome` (`shipped` / `rejected` / `in_flight`), its full spine
  `timeline` (issue → PR → review → merge → release, each cited) for **context**,
  and the focus's **own touchpoints** within it (their specific events — incl. a
  lone comment, with excerpt + ref). **Impact is not volume:** a small number of
  contributors may have outsized impact, or vice-versa; showing each touchpoint
  inside the train's full context lets the reader judge *influence*, not tally
  activity.
- **Symbol focus is its own chronological train** — the artifact's lifecycle
  (`code_events`) + identity chain across moves, in time order.
- **Time scope:** optional `--from/--to` answers *"impact in a timeframe"*; omitted,
  the train spans the focus's whole history in the store (*"over the project"*). The
  scope is echoed in the result so the narration stays contextual.
- **Both delivered and not:** all activity is included, with **shipped outcomes
  emphasized** (sorted/flagged) so "what landed" is foregrounded while in-flight /
  rejected work stays visible.

> The 8a interim person-impact shape (grouped counts) is **superseded** by this
> contract and is realigned as part of Phase 8 (counts survive only as a small
> summary header; the body is the chronological trains).

---

## The four queries

Each query: a Python API `spotlight.<name>(conn, project, **params) -> Result` and a
CLI subcommand. `Result` is a deterministically-ordered, citation-bearing dict
(JSON-serializable); a miss yields `{"status": "needs_gather", "guidance": ...}`.

### 1. person-impact  `spotlight person <login> [--from --to]`
**Q:** a contributor's **impact** across all project repos (people are
project-scoped) — the decision trains they touched, chronologically.
- **Trains touched** — every train containing a node the login authored / reviewed
  / merged / reported / **commented** on (seed `traverse_spine` from those nodes,
  dedupe to anchors). Each carries `outcome`, the full cited spine `timeline`, and
  the login's **touchpoints** in that train (their events — incl. a single
  influential comment, with excerpt + ref). Ordered chronologically by the train's
  key date.
- **modules / areas / is_bot** — from the person `structure` node (context header).
- **summary** — per-role counts + trains-touched + shipped, as a small header for
  scale; the body is the chronological trains, not the counts.
- *Determinism:* trains by `(key_date, anchor id)`; touchpoints/timeline by date.
- **Real data ✓.**

### 2. subsystem-split  `spotlight subsystem <area> [--from --to]`
**Q:** an area's activity + blast radius over an optional range.
- **Contributors** — `owns` (codeowners) + `touches` (commits/PRs → area) inverted.
- **Shipped / stalled** — PRs/issues attributed to the area, split by status.
- **`depends_on` blast radius** — area→area `depends_on` edges (with version /
  transitive in edge `data`), forward + reverse (what this area breaks if changed).
- *Determinism:* areas/items ordered by id / number.
- **Data caveat:** `owns`/`depends_on`/`touches` are **sparse in short real
  windows** (0/0/4 in the 5-day AVM store). Built + tested primarily against
  **seeded fixtures**; real-data coverage grows with window size / code_owners.

### 3. pattern-evolution  `spotlight symbol <artifact-id>`
**Q:** a symbol/file artifact's **full lifecycle across all history**, not one window.
- **Lifecycle** — `get_code_events(artifact_id)` in date order (add/change/remove,
  with bounded `before`/`after` for symbols).
- **Identity chain** — follow `replaced_by` / `identity_from` edges across renames
  /moves to assemble the artifact's true cross-path history (A→B→C).
- *Determinism:* events by `(date, commit)`; chain by edge order.
- **Real data:** ✓ available (109 code_events in the 5-day store).

### 4. commit-text-mining  `spotlight grep <phrase>`
**Q:** every comment / commit message / review / PR-issue body mentioning a phrase —
the `O(matches)` FTS query that is a full file scan on the flat JSON.
- **FTS5** via `graphstore.fts_search(query)` → matching node ids ranked by
  relevance; spotlight hydrates each to a cited result.
- **Input hardening:** user phrases must be quoted/escaped for the FTS5 `MATCH`
  grammar (operators `AND/OR/NOT/*/"/-/:` and unbalanced quotes otherwise raise) —
  spotlight owns this sanitization so callers can pass raw text.
- **GATHER PREREQUISITE (slice 8a):** `fts_text` is currently **empty** —
  `fold_bundle` does not yet `index_text`. Phase 8 must index the searchable text
  on the write path: PR/issue titles+bodies, commit messages, and the
  comment/review authors+bodies embedded in social node `data`. Without this the
  query has nothing to search. FTS is created only when the SQLite build supports
  FTS5 (`fts5_available`); the query degrades to a clear "FTS unavailable" result
  otherwise.
- **Real data:** ✓ after the indexing prerequisite lands.

---

## Focused renders (secondary)

The **primary output is structured, cited JSON** for the model to build reports /
media from — the skill's end objective. As a convenience, `--md` emits a compact,
deterministic markdown render per query (person card, subsystem table, symbol
lifecycle, grep hit-list) — text tables (no `mmdc`); Mermaid only where it adds
signal (e.g. a symbol identity chain). Renders are thin formatters over the JSON,
never a separate data path.

## CLI / contract

`python3 spotlight.py <query> <args> --store PATH [--json|--md]` → a deterministic,
cited result on stdout. **Default is `--json`** (raw structured result): spotlight's
primary consumer is the AI that builds the report / media from these answers, so the
contract is clean, citation-bearing JSON the model narrates; `--md` renders are a
convenience. Exit non-zero only on bad input (unknown query/param), **never** on an
empty / needs-gather result (those are valid structured answers). Importable API
mirrors the CLI. No network, no writes.

## Testing strategy (rev-14: *"each query against a seeded multi-window store
returns the expected aggregate with citations; FTS returns matches `O(matches)`"*)

- **Seeded multi-window store fixtures** — fold 2–3 crafted windows (incl. a
  cross-repo person, a rename/move chain, an area with owners + `depends_on`, and
  searchable text) so each query has deterministic expected output. TDD: failing
  query test first.
- **Per-query golden** — assert the exact ordered, cited result.
- **FTS** — assert `O(matches)` (only matching nodes hydrated) + input-sanitization
  (operator-bearing phrases don't raise).
- **Determinism** — same store → byte-identical result across runs.
- **Trust still green** — `validate.py` and the Phase 7 suites unchanged; spotlight
  is additive (reader-only).
- **Real-data smoke** — person-impact / pattern-evolution / grep against the real
  AVM store; subsystem-split where area data exists.

## Gated slices

- **8a — gather text-indexing prerequisite + spotlight scaffold + person-impact.**
  **DONE.** `fold_bundle` indexes searchable text into `fts_text` (idempotent;
  FTS5-gated) — PR/issue titles+bodies + comment/review authors+bodies, and
  commit messages. `spotlight.py` is stood up (CLI defaulting to raw cited JSON
  + `--md` render; Result shape with `ok`/`needs_gather`/`fts_unavailable`;
  project auto-detect), and **person-impact** ships end-to-end (contributions
  grouped+cited, symbols authored/authored_then_removed, trains anchored) with a
  seeded golden, cross-repo aggregation, needs_gather, determinism, and a
  real-data smoke (`test_spotlight.py`). *Gate met:* person-impact golden green;
  FTS populated on fold; Phase 7 suites unchanged (537 passed, 2 skipped).
- **8b — pattern-evolution + subsystem-split.** **DONE.** The graph-traversal
  queries ship end-to-end in `spotlight.py`. **pattern-evolution** (`symbol`):
  full lifecycle from `get_code_events` ordered by `(date, commit)`, each row
  cited by its commit ref; **identity_chain** assembled by walking `replaced_by`
  forward + `identity_from` backward (depth-capped) into an ordered A->B->C
  chain, each move link carrying `confidence`/`basis` from edge `data`
  (`move_confidence`/`move_basis`); accepts qualified or local artifact ids;
  unknown id -> `needs_gather`. **subsystem-split** (`subsystem [--from --to]`):
  contributors = inverse `owns` (codeowner logins) + the authors of commits/PRs
  that `touches` the area (`owns` beats `touches`), deduped + ordered by login;
  shipped/stalled = PRs attributed to the area by following touching commits'
  `part_of` edges (or a PR that `touches` directly), split merged/closed vs open,
  optionally `ts`-range filtered; `depends_on` blast radius out (depends on) +
  in (depended on by) with version/transitive. Seeded goldens (a real
  rename/move chain folded across two windows so the move set stays unique; an
  area with a codeowner, mixed-status touching PRs, and a `depends_on` each
  direction), needs_gather, determinism, and real-data smokes (a real file
  artifact's lifecycle; subsystem-split runs cleanly on the sparse real store —
  redis area attributes PR 7120 as shipped via its touching commit, deps empty).
  *Gate met:* both query goldens green; full suite 549 passed, 2 skipped
  (additive over 8a's 537+2); store byte-identical after queries (reader-only).
  *Store-shape note:* PRs attribute to an area transitively (commit `touches`
  area + commit `part_of` PR), not via a direct PR->area edge in the real store;
  identity-chain edges are stored bidirectionally (`replaced_by` src->dst and
  `identity_from` dst->src both on the artifacts), so the walk recovers the same
  chain from either endpoint.
- **8c — commit-text-mining + focused renders.** FTS query (input hardening,
  `O(matches)`), plus the markdown renders for all four. *Gate:* FTS golden +
  render goldens green; real-data smoke.

Each slice ships green under the existing trust gate + suite; spotlight never
mutates the store, so it cannot regress Phase 7's guarantees.

---

## Decisions (resolved in review)

1. **Text-indexing scope (8a):** index **all four** sources — PR/issue
   titles+bodies, commit messages, and comment/review bodies. "Every mention of a
   phrase" includes discussion text, where much of the *why* lives.
2. **subsystem-split:** **build it now** (slice 8b), tested against seeded fixtures;
   sparse real data is a data property, not a spotlight gap.
3. **Output default:** **`--json` (raw, cited)** — spotlight feeds the AI that builds
   the report / media (the skill's objective); `--md` renders are a convenience.
4. **Slice grouping:** 8a (FTS prereq + scaffold + person-impact) → 8b
   (pattern-evolution + subsystem-split) → 8c (text-mining + renders).
