# activity-overview → journey-graph substrate — design

**Date:** 2026-06-03
**Status:** Approved design — rev 1 (greenfield substrate redesign; supersedes the bundle-as-JSON seam of the 2026-06-01 design for the *storage + read* layers while inheriting all of its semantics — trains, provenance, people, flow, artifacts, feature deltas. No code written yet; sequenced as Phases 5–9 below.)
**Author:** brainstormed via superpowers
**Inherits:** `2026-06-01-activity-overview-design.md` (the shipped pipeline through Phase 4a). Every concept defined there — decision trains, fact-discipline/provenance, the people graph, flow/blockers, the artifact lifecycle ledger, feature deltas, sprint/release modeling, the diagram manifest — is carried forward unchanged in *meaning*. This document changes only **where those facts live and how they are read back**.

## Why this redesign

The 06-01 design made **the JSON bundle the seam**: `gather.py` writes one self-describing `activity-{from}-{to}.json`, and `link` / `render` / report all read it. That seam is correct for a single time-boxed window, but it does not compose across the dimension the 06-01 design itself calls out as central — **continuity** ("Git is chronological, so reports are too… the bundle is a time-series record, not a one-shot snapshot"). Three pressures break the flat-file seam:

1. **Full-history chronology.** The product goal is a *journey* — "deferred in April, shipped in June", a symbol's birth-and-death across months, a person's contribution arc — not a month-in-isolation. Reconstructing that today means loading N monthly JSON bundles and re-unioning them by identity *on every read*. At AVM-Bicep scale (a busy month is already a large bundle) a multi-year journey is gigabytes of JSON re-parsed per question. The 06-01 roll-up section concedes this: "re-run over a wide window… always yields a correct bundle and is the authoritative way to produce a long view" — i.e. the only correct long view is a **full re-gather**, because the JSON has no incremental read path.

2. **Random-access queries.** A flat bundle is built for *one* traversal — window → buckets → trains → report. The moment you want a *different* cut — "everything this person touched across all repos", "how did this subsystem's dependency edges evolve", "every comment that mentions `breaking change`" — you are doing a full scan of every bundle. There is no index. The 06-01 `series.json` is a thin ledger, not a queryable store.

3. **Accumulation without re-fetch.** 06-01 dedups overlapping windows "by immutable identity… unions by number/SHA, not by appending" — but that union is recomputed in memory each run from the raw bundles. There is no durable accumulated state; the dedup guarantee is real but **ephemeral**.

The fix is to keep the 06-01 *fact model* verbatim and swap the *substrate*: replace "one JSON file per window, re-unioned on read" with **a persistent property graph that gather accumulates into by identity, and that readers query**. The bundle does not disappear — it becomes a **view materialized out of the graph** for a window, byte-compatible with what `link`/`render`/report already consume. That compatibility is the linchpin (see Testing).

## Core principle (unchanged, re-seated)

**Gather is deterministic and decoupled. Analysis is the model's judgment.** The four-layer pipeline stands. What changes is the seam between Acquire and the offline half:

```
  06-01:  Acquire ──▶ Bundle (one JSON/window) ──▶ Link ──▶ Analyze ──▶ Synthesize
                          └─ re-unioned on every long read ─┘

  this:   Acquire ──▶ graphstore (persistent property graph, accumulated by identity)
                          │
                          ├─▶ extract(window)  ──▶ Bundle view ──▶ Link ──▶ Analyze ──▶ Synthesize   (unchanged downstream)
                          └─▶ spotlight(query) ──▶ analytics views (person / subsystem / pattern / text)
```

- **gather** becomes a **writer**: source facts → upsert into the store **by stable identity**. Re-running an overlapping window is a no-op on already-seen nodes (the dedup guarantee is now *durable*, not recomputed).
- **extract** is the **primary reader**: a window is a *range query* over the store, plus bounded graph traversal to pull in the decision-train spine, emitting the **same bundle shape** `link`/`render`/report already accept.
- **spotlight** is a **second reader**: parameterized analytics queries (person impact, subsystem split, pattern evolution, commit-text mining) that the flat bundle could never answer without a full scan.

Claude still does only judgment. The store holds only recorded facts, each still carrying its `{type, id, url}` source ref — provenance is a *column*, not an afterthought.

## Decisions (settled in brainstorming)

Four decisions are locked:

1. **Substrate = SQLite property graph (not Neo4j, not JSON, not a doc store).** A single-file embedded DB: zero server, zero network, portable (copy the `.db` like you copied the bundle), transactional upserts, real indexes for range + traversal, and **FTS5** for the text-mining queries. It ships in the Python stdlib (`sqlite3`) — no new hard dependency, consistent with 06-01's "stdlib only" core. A property graph is modeled as **node tables + a typed edge table** (below); recursive traversal uses SQLite recursive CTEs, bounded. Rationale for rejecting alternatives: Neo4j adds a server + driver dep and breaks portability; staying on JSON is the very thing that doesn't scale; a document store loses the cheap relational range-scan that *is* the window query.

2. **Scope keys are project- and repo-qualified.** Every node identity is namespaced `{project}/{repo}#{local-id}` (e.g. `avm/bicep-registry-modules#pr-4821`, commits by `repo#sha`, symbols by `repo#path#lang:subkind:name`). This is what makes **multi-repo** (06-01 Phase 8 / Terraform aggregation) a first-class store property rather than a post-hoc merge: a cross-repo train (a PR in repo A closing an issue in repo B) is just an edge between two qualified ids that cannot collide. Single-repo runs are the degenerate case where every id shares one prefix.

3. **Persistence = local cache by default, opt-in sync.** The store lives in the workspace (`workspace/journey.db`, one per project) as a **local, rebuildable cache** — git is still the source of truth, so a lost `.db` is always reconstructable by re-gathering. Sharing/persisting the store across machines or CI is **opt-in** via a configured remote location (the project manifest's store block), not automatic. This mirrors 06-01's stance that re-run is canonical and the index is the cheap path: here the `.db` is the cheap path, a wide re-gather is canonical.

4. **Spotlight is in-scope for the initial build, not a fast-follow.** The first release ships the chronological report **and** a first set of spotlight queries (person impact, subsystem split, pattern evolution, commit-text mining). The whole point of the substrate is random-access analytics; shipping the store without a reader that exercises non-window queries would leave the core rationale unproven. Spotlight is therefore a core phase (P8), ahead of multi-repo (P9).

---

## Section 1 — Artifact → node mapping (the schema)

The 06-01 bundle is a bag of arrays (`commits`, `prs`, `issues`, `trains`, `artifacts`, `feature_deltas`, `people`, `flow`, …). The store factors that bag into **three node classes + one typed edge table + ancillary tables**, with every 06-01 array becoming either a node table, an edge type, or a query over them. Nothing in the 06-01 fact model is dropped; it is *normalized*.

### Node classes

Every node carries `id` (qualified, PK), `project`, `repo`, `node_class`, `ts` (the node's primary chronological timestamp — what window range-scans key on), `data` (JSON blob of the full 06-01 record for that entity, so the bundle view can round-trip losslessly), and `fetched_at`.

| node_class | 06-01 origin | id form | `ts` is | notes |
|---|---|---|---|---|
| **social** | `prs[]`, `issues[]`, review/issue comments, reviews, reactions | `repo#pr-<n>` / `repo#issue-<n>` / `repo#comment-<id>` | merged_at / closed_at / created_at | the social layer; comment/review bodies live here (FTS-indexed) |
| **code** | `commits[]`, `artifacts[]` (incl. `symbol`/`comment` from 3d/3e), `symbol_events` | `repo#<sha>` / `repo#<path>#<lang>:<subkind>:<name>` | author/commit date / lifecycle event date | the code-event layer; one node per *artifact identity*, its lifecycle is an ordered set of code-event rows (below) |
| **structure** | `code_graph.areas[]`, `code_owners`, `milestones[]`, `releases[]`, `project`/sprints, `people[]` | `repo#area-<id>` / `login` (people are project-scoped, not repo) / `repo#milestone-<n>` | point-in-time / null | the slow-moving structural layer; people are **project-scoped** so one person aggregates across repos (decision 2) |

People are deliberately **project-scoped, not repo-scoped** — the contribution graph is a person *across* the project's repos, which is exactly the cross-repo people view 06-01 wanted but couldn't express in a per-repo bundle.

### Edge table (the graph)

One `edges` table: `(src_id, dst_id, edge_type, ts, data)`, indexed both directions. Edge types are the 06-01 relationships made explicit and queryable:

| edge_type | from → to | 06-01 origin |
|---|---|---|
| `closes` | pr → issue | closing refs / trailers (`Fixes #`) |
| `part_of` | commit → pr | merge structure |
| `cross_ref` | issue↔pr↔commit | timeline cross-reference events |
| `duplicate_of` / `spun_off` | issue → issue | duplicate/spin-off signals |
| `touches` | commit/pr → area | code-area attribution |
| `authored` / `reviewed` / `merged` / `reported` / `commented` / `reacted` | person → social/code | train `participants[]` roles |
| `owns` | person → area | CODEOWNERS |
| `depends_on` | area → area | IaC dependency edges (3c), carries `{version, transitive, provider, resolved}` in `data` |
| `replaced_by` / `identity_from` | artifact → artifact | symbol-identity moves (3e), carries `move_confidence` |
| `blocks` | issue → issue | flow `blocked_by` |
| `in_milestone` / `in_iteration` | social → structure | bucket/sprint membership |

A **decision train is no longer a stored array** — it is the connected component reachable from a root issue via the *spine* edge types (`closes`, `part_of`, `cross_ref`, `spun_off`, `duplicate_of`). `train.id` stays exactly the 06-01 stable anchor id (`train-issue-<n>` / `train-pr-<n>`), computed by the same rule; it is now the *identity of a traversal seed*, not a row. This is what makes a train that spans months read as one thread without any roll-up: the edges simply exist in the store.

### Ancillary tables

- **`code_events`** — the artifact lifecycle ledger, one row per `add|change|remove` event: `(artifact_id, event, commit, author, date, hunk, ref, before, after, detail)`. A `code` node's `lifecycle[]` (06-01) is `SELECT … WHERE artifact_id=? ORDER BY date`. `status`/`replaced_by` derive from the last event + the `replaced_by` edge.
- **`fts_text`** — FTS5 virtual table over comment/review/commit-message/body text, keyed by node id. This is the index that makes "every comment mentioning `breaking change`" an `O(matches)` query instead of a full scan — impossible on flat JSON.
- **`meta`** — store-level provenance: per `(project, repo)` the `clone_sha` last gathered against (06-01 `meta.clone_sha`, pinned for deterministic resume/roll-up), `gathered_windows[]` (what ranges have been folded in, so an overlapping re-gather knows what's already durable), and schema version.

### Identity & idempotency (the dedup guarantee, now durable)

Upsert key is the qualified `id`. Folding a window:
- a **social/structure** node already present → update mutable fields (state, labels, reaction counts) in place, no duplicate.
- a **code** node → its lifecycle is a *set* of `code_events` keyed by `(artifact_id, commit, event)`; re-seeing a commit is a no-op insert-or-ignore.
- edges are keyed by `(src, dst, edge_type)` → unioned, never appended.

So 06-01's "overlap by a day or two so nothing falls in the seam" becomes safe *for free and permanently*: re-folding an overlapping window mutates nothing already correct. The overlap is still the gap guarantee; the PK is the dedup guarantee — but now the dedup state **persists** instead of being recomputed per read.

---

## Section 2 — gather as a writer (+ backfill)

`gather.py`'s Acquire responsibilities are unchanged (clone + REST/GraphQL + code-area provider + IaC edges + full-window code-event walk — every 06-01 acquire detail carries over). What changes is its **sink**: instead of assembling one in-memory bundle and serializing JSON, it **upserts each fact into the store by identity** inside a transaction per source batch.

- **Writer, not assembler.** Each REST page / git-log hunk / graphify area becomes an upsert (`INSERT … ON CONFLICT DO UPDATE`) keyed by qualified id. The transaction boundary per batch means a crashed gather leaves a consistent partial store, not a corrupt half-bundle.
- **Windowed but accumulating.** gather still takes `--from/--to` (it bounds the *clone* and the *API pull* exactly as 06-01), but writes land in the *shared* store. `meta.gathered_windows` records the fold so the next run knows the seam is covered.
- **`clone_sha` pinned per repo** (06-01 3c.2) on every fold, so resume/roll-up rebuild against the identical tree.

### backfill — the single-node bridge

The one genuinely new gather capability the store enables: **`backfill(id)`**. Because the store is a *graph*, a reader traversing a train can hit an edge pointing at a node that was never gathered (e.g. a PR in the window `closes` an issue opened **before** the window, so the issue node is absent). On a flat bundle that fact is simply lost — 06-01 can only see what its window pulled. Here the reader can request the missing node:

- `backfill(id)` is a **single-node, on-demand fetch**: resolve the qualified id → its source (REST for social, local git/`git fetch --depth 1 <sha>` for code, graphify for structure) → upsert that one node (and its immediate cheap edges). Idempotent; a no-op if already present.
- It is **bounded by construction** — one node per call, called only on a traversal *miss*, never speculative. The reader's traversal budget (Section 3) caps total backfills per window so a pathological train can't trigger an unbounded fetch storm.
- It is the bridge that lets a *windowed* gather still produce a *complete* train: pull the window cheaply, backfill only the handful of out-of-window spine nodes a train actually references.

backfill is the only network-touching call outside the main Acquire pass, so it stays inside `gather.py` (the "only layer that touches the network" invariant from 06-01 holds — extract calls `gather.backfill(id)`, it does not fetch itself).

---

## Section 3 — extract as the primary reader

`extract.py` replaces "open the JSON bundle" with "**materialize a bundle view for a window out of the store**". Its output is **byte-compatible with the 06-01 bundle schema**, so `link.py` / `render.py` / `report-template.md` and their 382 existing tests are untouched. extract is pure read (plus bounded backfill on miss).

The window read is three bounded steps:

1. **Range query (the window).** `SELECT … WHERE project=? AND repo IN (…) AND ts BETWEEN ? AND ?` across the node tables → the in-window nodes. This is the cheap indexed scan that the flat bundle had to simulate by re-unioning files. Every node gets an `in_window: true` flag.

2. **Seed the trains.** For each in-window social node, compute its train anchor id (the 06-01 rule) → the set of train seeds touching the window.

3. **Bounded spine traversal.** From each seed, walk the **spine edge types** (`closes`/`part_of`/`cross_ref`/`spun_off`/`duplicate_of`) via a recursive CTE, **depth-capped** and **allowlist-restricted** to spine edges (so traversal can't wander into the whole graph). Nodes reached *outside* the window are pulled in with `in_window: false` (they're context, not activity) — and if a spine node is *absent*, `gather.backfill(id)` fetches it, subject to a per-window **backfill budget** (a ceiling that emits a warning rather than fetching unboundedly). Activity counts/buckets only ever sum `in_window` nodes; the out-of-window spine is there purely so a train reads as a complete thread (the "opened in April, shipped in June" case).

The materialized view then assembles exactly the 06-01 arrays — `commits`/`prs`/`issues` from the nodes, `trains` from the traversed components, `artifacts`/`feature_deltas` from `code_events`, `code_graph` from structure nodes + `depends_on` edges, `people`/`flow`/`buckets`/`diagrams` precisely as 06-01 defines — and writes the same `activity-{from}-{to}.json`. From `link` onward, **nothing knows the substrate changed**.

Roll-up and resume (06-01 3c.2) collapse into the store: a "6-month view" is just a wider range query, no multi-bundle union step; structure still comes from the latest `clone_sha` (a `WHERE` on the structure nodes' provenance), exactly the 06-01 rule, now expressed as a query instead of a merge.

---

## Section 4 — spotlight as a second reader

`spotlight.py` is the reader the flat bundle could never support: **parameterized analytics queries** that cut the store along axes orthogonal to the window. Each is a bounded SQL query (+ recursive CTE / FTS where needed) returning a structured, deterministically-ordered result the model narrates with the same citation discipline. The initial in-scope set (decision 4):

- **Person impact** — given a `login`, aggregate across **all repos in the project** (people are project-scoped): modules touched/owned, PRs authored/reviewed, symbols authored and `authored_then_removed` (churn), review latency, the trains they anchored — the 06-01 `people` profile, but computed on demand across the whole journey instead of one window. Joins `authored`/`reviewed`/`owns` edges + `code_events`.
- **Subsystem split** — given an `area` (or area glob), the contribution/feature/flow breakdown for that subsystem over a range: who works it, what shipped, its `depends_on` blast radius over time, its stalled/blocked items. Joins `touches`/`owns`/`depends_on`.
- **Pattern evolution** — given a symbol or symbol pattern, its full lifecycle across the history: every `add|change|remove`, every move (`replaced_by`/`identity_from` with confidence), reconstructed from `code_events` + identity edges — the intra-/inter-window symbol journey 06-01's 3d/3e tracked but could only show within one bundle.
- **Commit-text mining** — FTS5 query over `fts_text`: "every comment/commit/review mentioning `<phrase>`" (e.g. `breaking change`, a CVE id, a design term), with source refs. This is the query that is `O(matches)` on FTS and a full file scan on JSON — the clearest demonstration of why the substrate exists.

Each spotlight result resolves to bundle-style refs and feeds a focused render (a person spotlight, a subsystem deep-dive), reusing the 06-01 provenance + verification discipline. Spotlight does **not** re-fetch — it reads what gather has accumulated (a missing answer means "gather that window first", surfaced as guidance, not a silent gap).

---

## Section 5 — testing strategy

The redesign is testable layer-by-layer because the layers are isolated, and **TDD applies** (tests first per layer, per `superpowers:test-driven-development`). Critical fixtures already exist on disk from the 06-01 build — `git_log_*`, `rest_*`/`graphql_*`, and the golden `bundle_*.json` files — and are reused as the equivalence oracle.

| test | proves | how |
|---|---|---|
| **graphstore unit** | upsert / range query / traversal correctness | in-memory SQLite, pure unit tests, no network |
| **idempotency / accumulation** | re-folding a window is a no-op; overlapping windows union without dupes | fold same fixture twice → identical store (modulo `fetched_at`); overlapping windows → union |
| **determinism** | fixed fixtures → byte-stable store and byte-stable bundle view | golden compare with `(ts,id)` sort |
| **⭐ golden-bundle equivalence** | **the 382 existing tests still pass** — `link`/`render`/report unchanged | build store from existing `git_log/rest` fixtures → `extract` a window → assert output matches existing `bundle_*.json`. This is the contract that guards the whole swap |
| **traversal & backfill bounds** | spine-edge allowlist, depth cap, and backfill budget all hold; backfill fires only on a miss and is idempotent | mock `gather.backfill`, assert exact call set + ceiling warning |
| **cross-repo identity** | qualified ids don't collide; a cross-repo train traverses | seed repo A PR `closes` repo B issue, assert one thread |
| **multi-commit chronology** | N-commit PR enumerates topo+ts; intra-PR symbol evolution reconstructs | seed a PR with staged `code_events` |
| **spotlight queries** | each template returns expected aggregates, deterministically ordered | seeded store → person-impact / subsystem-split / pattern-evolution / FTS text-mining |
| **scale smoke** | window query + traversal stays fast at full-history volume | seed ~50k synthetic nodes, assert under a threshold (guards the "JSON breaks at scale" rationale) |

The ⭐ golden-bundle equivalence test is the linchpin: it lets the entire substrate swap underneath while *proving* the downstream pipeline and its existing tests are unaffected. No phase ships without it green.

The 06-01 **live integration gate** (`activity-overview-integration.yml`) carries over unchanged in spirit: each store phase runs the real gather→store→extract→link→render pipeline against the live repo and asserts the materialized bundle still satisfies the current contract.

---

## Components (new + changed)

```
.claude/skills/activity-overview/
  graphstore.py    # NEW — SQLite property graph: schema, upsert-by-id, range query, bounded traversal, FTS
  gather.py        # CHANGED — Acquire writes upserts into the store; adds backfill(id)
  extract.py       # NEW — window range query + spine traversal → materialized bundle view (06-01 schema)
  spotlight.py     # NEW — parameterized analytics queries (person / subsystem / pattern / text)
  link.py          # UNCHANGED — reads the materialized bundle exactly as before
  render.py        # UNCHANGED
  report-template.md  # UNCHANGED
  BUNDLE.md        # CHANGED — documents that the bundle is now a materialized view; adds STORE.md cross-ref
  STORE.md         # NEW — graph schema (node classes, edge types, ancillary tables, identity rules)
  projects.json    # CHANGED — adds a `store` block (location + opt-in sync), repos[] for multi-repo
  test_graphstore.py / test_extract.py / test_spotlight.py  # NEW
  test_gather.py / test_link.py / test_render.py            # CHANGED/UNCHANGED
  fixtures/        # reuse existing git_log/rest/graphql/bundle fixtures + a seeded-store fixture
```

`graphstore.py` owns *all* SQL; `gather`/`extract`/`spotlight` call its API (`upsert_node`, `upsert_edge`, `add_code_event`, `range_query`, `traverse_spine`, `fts_search`, `backfill_missing`) — no raw SQL leaks into the readers, keeping the substrate swappable and the readers unit-testable against an in-memory store.

### projects.json additions

```json
{
  "projects": {
    "<name>": {
      "repos": [ { "owner": "Azure", "repo": "bicep-registry-modules", "branches": ["main"] } ],
      "store": { "path": "workspace/<name>-journey.db",
                 "sync": { "enabled": false, "remote": null } },
      "...": "all existing 06-01 fields (internal, label_taxonomy, project_v2, transcript) carry over"
    }
  }
}
```

`repos[]` (plural) is the multi-repo seam (decision 2); a single-repo project lists one. `store.sync.enabled:false` is the local-cache default (decision 3).

---

## Phasing — vertical slices (continues 06-01's Phase 4a)

Each phase is a complete vertical slice that produces a verifiable artifact, thickening every layer — same discipline as 06-01. The golden-bundle equivalence test gates every phase.

- **Phase 5 — graphstore foundation.** `graphstore.py`: schema (3 node classes + edge table + `code_events` + `fts_text` + `meta`), upsert-by-id, range query, bounded spine traversal (recursive CTE, depth-capped, allowlisted), FTS5 wiring. `STORE.md`. Full `test_graphstore.py` (unit + idempotency + determinism + scale smoke). *Verifiable:* seed from existing fixtures, round-trip, assert idempotent union.
- **Phase 6 — gather as writer + backfill.** Repoint `gather.py`'s sink from JSON-assembly to store upserts inside per-batch transactions; pin `clone_sha`; record `gathered_windows`. Implement `backfill(id)` (single-node, idempotent, network-bounded). *Verifiable:* gather a window into a fresh `.db`, re-gather an overlapping window, assert no duplication; assert a backfilled out-of-window node appears exactly once.
- **Phase 7 — extract as primary reader (the equivalence phase).** `extract.py`: range query → train seeds → bounded spine traversal (with `in_window` flags + backfill budget) → **materialized bundle view** in the 06-01 schema. *Verifiable:* the ⭐ golden-bundle equivalence test — `extract` reproduces the existing `bundle_*.json` such that `link`/`render`/report and all 382 existing tests pass unchanged. Roll-up/resume collapse into wider range queries here.
- **Phase 8 — spotlight (core, in-scope).** `spotlight.py`: person-impact, subsystem-split, pattern-evolution, commit-text-mining queries + focused renders. `test_spotlight.py`. *Verifiable:* each query against a seeded multi-window store returns the expected aggregate with citations; FTS query returns matches `O(matches)`.
- **Phase 9 — multi-repo.** Exercise the qualified-id namespacing end-to-end: `repos[]` in the manifest, cross-repo trains (a PR in repo A closing an issue in repo B traverses as one thread), project-scoped people aggregating across repos, Terraform multi-repo aggregation (the 06-01 Phase 8 deferral, now a first-class store property). *Verifiable:* cross-repo identity + cross-repo train tests green on real data.

## Non-goals (carried from 06-01, plus)

- All 06-01 non-goals stand (no third-party skills, no `gh` CLI, no YouTube fetch, read-only, Markdown+Mermaid output, etc.).
- **No server.** SQLite single-file only; no Neo4j/Postgres/graph server (decision 1).
- **No automatic cross-machine sync.** The `.db` is a local rebuildable cache; sync is opt-in config (decision 3). A lost store is always reconstructable by re-gathering — git remains source of truth.
- **No schema break for downstream.** The materialized bundle view stays byte-compatible with the 06-01 schema; `link`/`render`/report are not rewritten. If a future need can't be met within that schema, that's a *new* design, not this one.

## Open questions

- **Migration of existing bundles.** Whether to bulk-import already-generated 06-01 JSON bundles into a fresh store (a one-time `import_bundle(path)` loader) or only ever populate via gather. Leaning toward a loader so historical bundles seed the journey without re-cloning — but it's not required for P5–P9 and can be a P8.5 add.
- **FTS tokenizer choice** (`unicode61` vs `porter`) for commit-text mining — porter stemming helps "breaking"/"breaks" but can over-match identifiers; to be decided against real AVM comment text during P8.
- **Backfill budget default** — the per-window ceiling on out-of-window spine fetches needs a real number measured against a refactor-heavy AVM window in P7 (same open-question shape as 06-01's graphify timing).
- Real project coordinates / `repos[]` lists for the three target projects still to be captured (inherited from 06-01).
