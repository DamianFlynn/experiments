# activity-overview skill — design

**Date:** 2026-06-01
**Status:** rev 15 — shipped.

**Where we are (2026-06-05):**
- **Shipped to `master`:** Phases 1–4a (the flat-bundle digest); the rev-14 substrate phases **5** (graphstore foundation, #11), **6** (gather-as-writer + on-demand `backfill`, #12) and **7** (full graph substrate — `extract` slices 7a/7b/7c, #13); **Phase 8 — spotlight** (the four substrate-analytics queries + focused renders + CI gate) and **Phase 8d — the train-completion orchestrator** (`complete.py`: dead-ref tombstone + `gather.ABSENT` seam, the transitive window-bounded orchestrator + `extract` refactor, and spotlight's honest `complete`/`gaps` contract + `--complete` + gap render), both via **#14**; and **Phase 9 — multi-repo** (qualified-id namespacing, cross-repo trains, project-scoped people, and **Terraform multi-repo aggregation** — the AVM-TF constellation; #15–#18, #20–#23), with a multi-repo **constellation CI gate** running real `terraform init`/`graph`. The persistent SQLite graph is the live seam; `extract` reproduces the rev-13 bundle byte-for-byte under the equivalence gate. **Verified end-to-end against real AVM data** (gather → `validate` → spotlight → live `--complete`) on both a fixed historical week and a trailing recent week, across both Bicep and Terraform toolchains. Design: `docs/superpowers/specs/2026-06-04-activity-phase8d-completion.md`; plan: `docs/superpowers/plans/2026-06-04-activity-phase8d-completion.md`. **Phase 10 — 4b sub-agent train narration** (review/lifecycle graph nodes → bounded `slice_train` texture → parallel grounded narrator sub-agents over `link.py --slice`; #24–#26; spec: `docs/superpowers/specs/2026-06-06-activity-phase10-narration.md`).
- **Future:** Phases 9–14 (multi-repo, 4b sub-agent narration, report/community/forecast views) — see the **P1–P14** ledger under *Implementation phasing*.

**Rev history.** (**rev 15 promotes backfill into a shared train-completion orchestrator** — `complete.py` makes train completion *transitive* and *window-bounded* and gives every train an *honest edge contract* (`complete`/`gaps` with reasons; 404 phantoms pruned + remembered dead via a `dead_refs` tombstone), shared by `extract` and `spotlight`; see the new *`docs/superpowers/specs/2026-06-04-activity-phase8d-completion.md`* and **Phase 8d** in the ledger. Prior — rev 14, Phases 1/2/3a/3b/3c/3c.1/3c.2 shipped; + Phase 3d symbol-granular artifacts shipped — diff-local `git log -p` walk → `symbol_events` folded into `kind:symbol|comment` artifacts + `feature_deltas` with bounded `before`/`after`/`detail`, for Bicep/Terraform with graphify-language symbols best-effort; + Phase 3e symbol-identity tracking shipped — window-wide symbol MOVES with precision-over-recall guards + confidence, in `symbol_moves` + artifact `replaced_by`/`identity_from`; + Phase 4a significance+tier scoring, per-train `effort` block, `slice_train` bounded helper, `forecast` scaffold, and `train_flowcharts` live — 4b sub-agent narration + gather review/lifecycle slice still pending. **Rev 14 re-seats the storage/read seam onto a persistent SQLite graph substrate** — graphstore → gather-as-writer → extract → spotlight — keeping the rev-1–13 fact model verbatim while making full-history chronology, random-access analytics, and durable accumulation tractable; see the new *Architecture (rev 14) — persistent graph substrate* section below and the unified **P1–P14** ledger under *Implementation phasing*. The deferred 4b and the original P5–P8 features are re-sequenced as P10–P14, rebuilt on the store.)
**Author:** brainstormed via superpowers

## Purpose

A Claude Code skill that produces a time-boxed **engineering activity + sprint/release
digest** for a GitHub repository. The user asks Claude to "run the activity-overview
skill for `<project>` over `<period>`", and the skill produces a Markdown report that
covers: what shipped, releases published, CI/CD health, what's in flight, what was
rejected/abandoned, **the decision trains** behind the work, design decisions,
community-call highlights, a **next-release forecast**, and open risks — framed around
**previous / current / next** sprint and release.

This replaces a naive "Claude reads the whole repo and guesses" approach with a
deterministic **gather** step (local clone + REST/GraphQL + optional code graph) that
writes a persisted bundle, followed by an **offline analysis** step that traces decision
trains and authors the narrative.

### Host vs. target

This repository (`damianflynn/experiments`) is **only the host** for the skill — its
existing content is archive-only and is **never analyzed**. The skill is built and
committed here but always runs against **external target repos** (the lead validation
target is the large **AVM-Bicep** repo). There are **three target projects** in regular
use; some hold a monthly/quarterly community YouTube call whose transcript is an
additional context source, and the projects use GitHub milestones (releases) and/or
Projects v2 boards (sprints) for planning.

## Core principle

**Gather is deterministic and decoupled. Analysis is the model's judgment.**

The skill is a four-layer pipeline with a **persisted bundle as the seam** between the
online and offline halves:

```
  ┌─────────── online, deterministic ───────────┐   ┌──────── offline ────────┐
  Acquire ──▶ Bundle (on-disk) ──▶ Link ──▶ Analyze (sub-agents) ──▶ Synthesize
```

1. **Acquire** (the *only* layer that touches the network) — shallow/partial **local
   clone** of the target repo bounded to the window (commit tree + diffs, free, no rate
   limits), plus a REST/GraphQL pull of the **social layer** (issues, PRs, comments,
   reviews, timeline links, workflow runs, releases, milestones, Projects v2), plus a
   required local **graphify** code-graph pass. Writes one self-describing bundle.
2. **Bundle** — a portable, inspectable, diffable on-disk artifact. Gather once; analyze
   many times without re-fetching or re-touching GitHub. The bundle is the audit record
   of exactly which facts a report was built from.
3. **Link** (offline, deterministic) — builds the **decision-train graph**: connects each
   issue → its linked PRs → their commits, via timeline cross-references and commit
   trailers (`Fixes #123`, merge commits), and attributes each train to code areas via
   the graphify communities / changed-path modules.
4. **Analyze + Synthesize** (offline, the model) — **parallel sub-agents**, one per train,
   read that train's full thread + local diffs and emit a structured decision narrative
   (proposed → changed direction → rejected → spun off → shipped). The lead agent
   synthesizes the per-train analyses + buckets into the report and forecast.

Claude does only the judgment work (tracing trains, grouping, summarizing, risk-spotting,
forecasting prose). It never invents facts: commits/diffs come from the clone, the social
layer from the API, code areas from graphify, and the candidate buckets are computed
deterministically in Link.

This keeps token cost bounded (diffs are local; sub-agents are scoped to one train each)
and report data reproducible.

## Architecture (rev 14) — persistent graph substrate

**Rev 14 changes where facts live and how they are read back — not what a fact is.**
Through rev 13 the **JSON bundle was the seam**: `gather.py` wrote one
`activity-{from}-{to}.json` and `link`/`render`/report read it. That is correct for a
single window but does not compose along the dimension this design itself calls central —
**continuity**. Three pressures break the flat-file seam:

1. **Full-history chronology.** The product is a *journey* — "deferred in April, shipped in
   June", a symbol's birth-and-death across months, a person's arc — not a month in
   isolation. Under the flat file the only correct long view is a **full re-gather**,
   because JSON has no incremental read path.
2. **Random-access queries.** A flat bundle is built for one traversal (window → buckets →
   trains → report). Any other cut — "everything this person touched across all repos",
   "how did this subsystem's edges evolve", "every comment mentioning `breaking change`" —
   is a full scan of every bundle, with no index.
3. **Accumulation without re-fetch.** Overlap-dedup-by-identity is recomputed in memory each
   run; there is no durable accumulated state.

Rev 14 keeps the rev-1–13 **fact model verbatim** (trains, provenance, people, flow,
artifacts, feature deltas, diagrams) and swaps the **substrate**: a persistent property
graph that gather accumulates into **by identity** and that readers query. The bundle does
not disappear — it becomes a **view materialized out of the graph** for a window,
byte-compatible with what `link`/`render`/report already consume. Everything described
below under "the bundle" still holds as the **fact model / materialized view**; only its
storage and read paths change.

```
  rev ≤13: Acquire → Bundle (one JSON/window) → Link → Analyze → Synthesize
                        └─ re-unioned on every long read ─┘

  rev 14:  Acquire → graphstore (persistent property graph, accumulated by identity)
                       ├─ extract(window)  → Bundle view → Link → Analyze → Synthesize   (downstream unchanged)
                       └─ spotlight(query) → analytics views (person / subsystem / pattern / text)
```

`gather` becomes a **writer** (source facts → upsert by stable identity; re-running an
overlapping window is a durable no-op). `extract` is the **primary reader** (a window is a
range query plus bounded train-spine traversal, emitting the same bundle shape). `spotlight`
is a **second reader** (parameterized analytics the flat bundle could never answer without a
full scan). Claude still does only judgment; the store holds only recorded facts, each still
carrying its `{type,id,url}` source ref — provenance is a *column*, not an afterthought.

### Decisions (locked)

1. **SQLite property graph.** A single embedded file via stdlib `sqlite3`: zero server, zero
   network, portable (copy the `.db` like you copied the bundle), transactional upserts, real
   indexes for range + traversal, **FTS5** for text mining, recursive CTEs for bounded
   traversal. No new hard dependency. *Rejected:* Neo4j (server + driver dep, breaks
   portability); staying on JSON (the very thing that doesn't scale); a document store (loses
   the relational range-scan that *is* the window query).
2. **Project/repo-qualified identity.** Every node id is namespaced `{project}/{repo}#{local}`
   (people are `{project}#person-{login}`, project-scoped so one person aggregates across the
   project's repos). This makes **multi-repo** a first-class store property: a cross-repo
   train (a PR in repo A closing an issue in repo B) is just an edge between two qualified ids
   that cannot collide. Single-repo runs are the degenerate one-prefix case.
3. **Local cache, opt-in sync.** The store lives in the workspace
   (`workspace/<project>-journey.db`, one per project) as a **rebuildable cache** — git stays
   source of truth, so a lost `.db` is always reconstructable by re-gathering. Cross-machine
   sharing is **opt-in** via a configured remote (the manifest `store` block), not automatic.
   Mirrors the existing stance: re-run is canonical, the `.db` is the cheap path.
4. **Spotlight is in-scope** for the first substrate release, not a fast-follow — the whole
   point of the substrate is random-access analytics; shipping the store without a non-window
   reader would leave its rationale unproven.

### Section 1 — artifact → node mapping (the schema)

The rev-13 bundle is a bag of arrays (`commits`, `prs`, `issues`, `trains`, `artifacts`,
`feature_deltas`, `people`, `flow`, …). The store **normalizes** that bag into three node
classes + one typed edge table + ancillary tables; nothing in the fact model is dropped.

Every node carries `id` (qualified, PK), `project`, `repo`, `node_class`, `ts` (primary
chronological key that window scans use), `data` (the full rev-13 record as a JSON blob, so
the bundle view round-trips losslessly), and `fetched_at`.

| node_class | rev-13 origin | id form | `ts` is |
|---|---|---|---|
| **social** | `prs[]`, `issues[]`, comments, reviews, reactions | `repo#pr-<n>` / `repo#issue-<n>` / `repo#comment-<id>` | merged/closed/created |
| **code** | `commits[]`, `artifacts[]` (incl. 3d/3e symbols), `symbol_events` | `repo#<sha>` / `repo#<path>#<lang>:<subkind>:<name>` | author/event date |
| **structure** | `code_graph.areas[]`, `code_owners`, `milestones[]`, `releases[]`, `project`/sprints, `people[]` | `repo#area-<id>` / `{project}#person-<login>` / `repo#milestone-<n>` | point-in-time / NULL |

People are **project-scoped, not repo-scoped** — exactly the cross-repo people view rev 13
wanted but couldn't express per-bundle.

**Edge table** `edges(src_id, dst_id, edge_type, ts, data)`, indexed both directions, makes
the rev-13 relationships explicit and queryable: `closes` (pr→issue), `part_of` (commit→pr),
`cross_ref` (issue↔pr↔commit), `duplicate_of`/`spun_off` (issue→issue), `touches`
(commit/pr→area), `authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted`
(person→node), `owns` (person→area), `depends_on` (area→area, carries
`{version,transitive,provider,resolved}`), `replaced_by`/`identity_from` (artifact→artifact,
carries `move_confidence`), `blocks` (issue→issue), `in_milestone`/`in_iteration`
(social→structure).

A **decision train is no longer a stored array** — it is the connected component reachable
from a root issue over the **spine** edge types (`closes`/`part_of`/`cross_ref`/`spun_off`/
`duplicate_of`). `train.id` stays the rev-13 stable anchor (`train-issue-<n>`/`train-pr-<n>`),
now the identity of a *traversal seed* rather than a row. This is what makes a train spanning
months read as one thread with no roll-up — the edges simply exist in the store.

**Ancillary tables:** `code_events` (the artifact lifecycle ledger, one row per
`add|change|remove`, set-keyed by `(artifact,commit,event)` → an artifact's `lifecycle[]` is
a query); `fts_text` (FTS5 over comment/review/commit/body text → `O(matches)` text mining
instead of a full scan); `meta` (per-repo `clone_sha`, `gathered_windows[]`, schema version).

**Identity & idempotency (durable dedup):** upsert key is the qualified `id`; social/structure
nodes update mutable fields in place, code lifecycles are sets keyed by `(artifact,commit,
event)`, edges are keyed by `(src,dst,type)` → unioned never appended. So the rev-13 "overlap
by a day so nothing falls in the seam" becomes safe *and permanent*: the overlap is the gap
guarantee, the PK is the dedup guarantee — now **persisted** instead of recomputed per read.

### Section 2 — gather as a writer (+ backfill)

`gather.py`'s Acquire responsibilities are unchanged (clone + REST/GraphQL + code-area
provider + IaC edges + full-window code-event walk all carry over). What changes is its
**sink**: instead of assembling one in-memory bundle and serializing JSON, it **upserts each
fact into the store by identity** inside a transaction per source batch (a crashed gather
leaves a consistent partial store). It still takes `--from/--to` (bounding the clone + API
pull exactly as before) but writes land in the *shared* store; `meta.gathered_windows` records
the fold; `clone_sha` is pinned per repo for deterministic resume/roll-up.

**`backfill(id)` — the single-node bridge (genuinely new).** Because the store is a *graph*, a
reader traversing a train can hit an edge pointing at a node never gathered (e.g. a windowed PR
`closes` an issue opened before the window). On a flat bundle that fact is lost. `backfill(id)`
is a single-node, on-demand fetch (REST for social, local git/`git fetch --depth 1 <sha>` for
code, graphify for structure) → upsert that one node + its cheap immediate edges; idempotent,
a no-op if present, called only on a traversal *miss*, bounded by a per-window budget. It is
the only network call outside the main Acquire pass, so it stays inside `gather.py` (extract
calls `gather.backfill(id)`; it never fetches itself — the "only gather touches the network"
invariant holds).

### Section 3 — extract as the primary reader

`extract.py` replaces "open the JSON bundle" with "**materialize a bundle view for a window out
of the store**", byte-compatible with the rev-13 schema so `link`/`render`/`report` and their
tests are untouched. The window read is three bounded steps: (1) **range query** —
`WHERE project=? AND repo IN (…) AND ts BETWEEN ? AND ?` → in-window nodes, each flagged
`in_window: true`; (2) **seed the trains** — each in-window social node → its train anchor id;
(3) **bounded spine traversal** — a recursive CTE from each seed over the spine allowlist,
depth-capped, pulling out-of-window spine nodes as `in_window: false` (context, not activity)
and `backfill`-ing absent ones under a budget ceiling (warn, don't fetch unboundedly). Activity
counts/buckets only ever sum `in_window` nodes; the out-of-window spine is there purely so a
train reads as a complete thread. The view then assembles exactly the rev-13 arrays; from
`link` onward, nothing knows the substrate changed. **Roll-up and resume** (3c.2) collapse into
the store — a "6-month view" is a wider range query, no multi-bundle union; structure still
comes from the latest `clone_sha`, now a `WHERE` instead of a merge.

### Section 4 — spotlight as a second reader

`spotlight.py` is the reader the flat bundle could never support: **parameterized analytics
queries** orthogonal to the window, each bounded SQL (+ CTE / FTS) returning a
deterministically-ordered result the model narrates with the same citation discipline. The
in-scope set: **person impact** (aggregate a `login` across all project repos — modules,
authored/reviewed, symbols authored + `authored_then_removed`, trains anchored); **subsystem
split** (an area's contributors / shipped / `depends_on` blast radius / stalled items over a
range); **pattern evolution** (a symbol's full lifecycle across history via `code_events` +
identity edges); **commit-text mining** (FTS5 over `fts_text` — every comment/commit/review
mentioning a phrase, the `O(matches)` query that is a full file scan on JSON). Spotlight does
not re-fetch — a missing answer is "gather that window first", surfaced as guidance.

### Section 5 — testing strategy (substrate)

Testable layer-by-layer because the layers are isolated; **TDD applies**. Existing fixtures
(`git_log_*`, `rest_*`/`graphql_*`, golden `bundle_*.json`) are reused as the equivalence
oracle. Layers: **graphstore unit** (in-memory SQLite); **idempotency/accumulation** (fold
twice → identical store; overlapping windows union); **determinism** (byte-stable store + view
via `(ts,id)` sort); **⭐ golden-bundle equivalence** (build store from existing fixtures →
`extract` a window → assert output matches existing `bundle_*.json` so the existing pipeline +
all its tests pass unchanged — *the linchpin that guards the whole swap*); **traversal &
backfill bounds** (spine allowlist, depth cap, budget; backfill only on miss, idempotent);
**cross-repo identity** (qualified ids don't collide; cross-repo train traverses);
**multi-commit chronology**; **spotlight queries**; **scale smoke** (~50k synthetic nodes →
window query under threshold, guarding the JSON-breaks rationale). No substrate phase ships
without the golden-bundle test green.

### New & changed components (rev 14)

- **`graphstore.py` (new)** — SQLite property graph: schema, upsert-by-id, range query, bounded
  spine traversal, FTS. Owns *all* SQL; readers/writers call its function API.
- **`gather.py` (changed)** — Acquire writes upserts into the store; adds `backfill(id)`.
- **`extract.py` (new)** — window range query + spine traversal → materialized bundle view
  (rev-13 schema).
- **`spotlight.py` (new)** — parameterized analytics queries + focused renders.
- **`link.py` / `render.py` / `report-template.md` (unchanged)** — read the materialized view
  exactly as before; guarded by golden-bundle equivalence.
- **`STORE.md` (new)** — graph schema reference (node classes, edge types, ancillary tables,
  identity rules), the contract later phases and downstream renderers code against. `BUNDLE.md`
  gains a cross-ref noting the bundle is now a materialized view.
- **`projects.json` (changed)** — adds `repos[]` (the multi-repo seam; a single-repo project
  lists one) and a `store` block (`{path, sync:{enabled:false, remote:null}}` — local-cache
  default). All existing per-project fields carry over.

## The bundle is the product (fact base for many outputs)

The deliverable is the **provenance-rich bundle**, not just the digest. The user renders
multiple downstream formats from the same fact base — a detailed **L400 long-form blog
post**, a **video transcript / script**, and **social carousels** — so the bundle is the
single source of truth those all draw on. v1 ships **the bundle + one renderer (the
Markdown digest)** plus a documented schema (`BUNDLE.md`); the other renderers are the
user's to build on top. This shapes two hard requirements below.

## Fact discipline & provenance (hard requirement)

Because downstream content **amplifies** errors (an L400 deep-dive that misstates a
parameter change is worse than a wrong line in an internal note), the fact base is held to
a citation standard:

- **Every fact carries a source ref** — a `{ type, id/number/sha, url }` pointing at the
  exact PR, issue, commit, comment, review, run, or release it came from. Refs are the
  spine of the bundle; nothing narrative-bearing is unsourced.
- **No unsourced claim.** The Analyze sub-agents and the lead **cite, they do not
  paraphrase from memory.** Each per-train narrative returns an `evidence: [ref]` list, and
  every statement in any rendered output must resolve to a bundle ref. (Enforced by
  `superpowers:verification-before-completion` discipline before any output is published.)
- **Hybrid evidence persistence** (chosen): the bundle persists, *inline*, the evidence
  behind every claim that backs a narrative — the **actual diff hunk** for each feature
  delta, the **quoted comment/review text** (with author + url) used as rationale, and a
  source ref on every fact. **Bulk raw diffs stay in the referenced clone** (`clone_dir`),
  not copied into the bundle. Net: the bundle alone is fact-checkable for everything that
  drives a narrative, without re-cloning or re-hitting the API, while staying lean on a
  repo the size of AVM-Bicep.
- **Depth for L400.** Evidence is kept at the granularity a deep technical write-up needs:
  precise param/resource/output names, before→after values, and the commit/PR/comment that
  changed them — not just "module X changed".

## Why a local clone (the scale unlock)

Walking the commit tree and per-PR file lists **via the API** is what blows the rate
limit on a busy month of a large repo like AVM-Bicep. A single shallow/partial clone
bounded to the window gives the **entire commit tree + diffs locally, for free, with no
rate-limit exposure**:

- `git clone --filter=blob:none --shallow-since=<from>` (partial + windowed) keeps the
  clone small and fast even on large histories.
- Commit messages, diffs, parameter changes, and merge structure are then read with local
  `git log` / `git show` / `git diff` — no API calls.

What the clone does **not** contain (and so still needs the API) is the **social layer**:
issue/PR bodies, review + issue comments, reactions, the issue↔PR↔commit linkage
(timeline / cross-reference events), labels, milestones, Projects v2 board state, and
Actions runs. That layer is bounded by **item count, not file count**, and is batched via
GraphQL — hundreds of calls for a busy month, comfortably within budget.

## Decision trains (first-class concept)

The value of the digest is not a flat list of merged PRs — it is the **narrative thread**
of how a decision moved through the project. A *train* is the linked unit:

```
  issue (idea / request)  ──┐
        maybe-duplicate of ─┤
                            ├─▶ PR(s)  ──▶ commits ──▶ diffs (param/feature changes)
        spun-off issue   ◀──┤            (direction changes, rejects, additions)
                            └─▶ outcome: shipped | rejected | abandoned | deferred
```

**Link** builds these deterministically from:
- **Closing references** in PR bodies / commit trailers (`Fixes #`, `Closes #`, `Resolves #`).
- **Timeline cross-reference events** (issue ↔ PR ↔ commit mentions, `connected`/`disconnected`).
- **Merge commits** → which commits belong to which PR.
- **Duplicate / spun-off** signals (`duplicate` label, `Duplicate of #`, references between issues).

**Analyze** then narrates each train with a per-train sub-agent. The bundle reserves a
`trains` array so every later phase can thicken the same structure.

## Issue taxonomy & label facets (kind)

Issues are not monolithic; a train's shape differs by what kind of issue seeded it. Each
issue is classified into `kind` — `feature` / `module-request` / `bug` / `idea` /
`question` / `docs` / `other` — in priority order: GitHub **native issue types** (queried via
`list_issue_types` when the repo uses them) → **label facets** (below) → **issue template**
filename → title/body heuristic. `kind` is carried onto the train so feature/module
proposals, bug-fix threads, and ideas can be counted, charted, and narrated separately.

**Label taxonomy (auto-detect + config override).** Many of these repos use a structured
label scheme, so labels are a strong typing signal — but only if the *structure* is read,
not the flat strings. Link **auto-detects label namespaces** by splitting on the conventional
separators (`area/…`, `type/…`, `priority/…`, `status/…`, `needs-…`, `Class:…`) and maps
each namespace to a **facet** — `kind`, `area`, `priority`, `status`, `lifecycle`. A
per-project `projects.json` `label_taxonomy` block can **override or extend** the auto-map
(e.g. pin AVM's `Class: …`/`Type: …`/`Needs: …` labels to facets, or add repo-specific
namespaces). Detected facets are stored top-level in `label_taxonomy` and applied to each
issue/PR, feeding `kind`, code-`area` attribution, the `priority` used in forecasting, and
the `status`/`lifecycle` signals the flow analysis keys on. Unprefixed labels stay as a flat
list; auto-detect degrades to "no facets" rather than guessing.

## People & contribution graph (first-class)

Engagement is a product goal for these repos, so **people are first-class entities**, not
just names on a PR. The intersection of *people × code areas × time* is what lets us draw
who is contributing, reviewing, blocking, and maintaining — and feature them.

- **Per-train participants:** each train records `participants: [{ login, role,
  author_association }]` with roles **reporter / author / reviewer / merger / commenter /
  blocker / reactor**. This is the edge list of the contribution graph.
- **`people` aggregate (top-level):** per `login`, a profile with `internal:bool`, role
  counts, **modules** touched/owned (joined via graphify communities + CODEOWNERS), tech
  **areas**, `prs_authored` / `prs_reviewed`, `review_latency`, `merge_rate`,
  `issues_reported`, `stale_owned:[#]`, and `first_seen`/`last_active`. Many dimensions, one
  object — the substrate for every people view.
- **Authored-code dimensions (both layers, not just social):** a person's contribution is
  not only comments/reviews but the **code, docs, and examples they authored**. From the
  code-event timeline (below), `people` also carries `examples_authored`, `docs_authored`,
  `symbols_authored`, and **`authored_then_removed`** (churn — their work later removed or
  replaced, with the removing commit/author). So fame can say *"wrote 9 of the examples"* and
  the narrative can trace a contribution all the way to where it lived and died.
- **Internal vs. community:** derived from GitHub `author_association`
  (MEMBER/COLLABORATOR/CONTRIBUTOR/NONE) + org/team membership + CODEOWNERS, **supplemented
  by a `projects.json` internal config** (handle list and/or email domains/orgs — e.g.
  Microsoft staff on AVM repos). Config supplements the derived signal; it doesn't replace it.
- **Halls — fame public, shame/blame internal (your call):** `halls.fame` (top shippers,
  reviewers, maintainers, rising community contributors) is **publishable**;
  `halls.internal.{shame,blame}` (slow reviews, stale-PR owners, regressions) is computed
  but kept in a **private/internal section of the bundle and excluded from public renders by
  default**, to avoid chilling the community engagement these repos exist to encourage.
- **CODEOWNERS (`code_owners`):** parsed to map path/glob → owning logins, giving precise
  person↔module ownership for the people graph and for "who maintains X".
- **Provenance still applies:** every person stat resolves to source refs (the PRs/reviews/
  comments it was counted from) — people facts are cited like any other.

## Flow & stall analysis (why issues hang)

Beyond *what shipped*, the bundle explains *what didn't move and why*. Over the issue
lifecycle + cross-reference timeline, `flow` classifies each notable issue:

- **hung** — high engagement, no assignee/PR, aging.
- **upvoted-but-ignored** — high 👍 `reactions`, no movement (needs reaction counts from the API).
- **traction-then-abandoned** — activity → silence → closed `not_planned`.
- **blocked** — `blocked` label / "blocked by #" / depends-on references.

…with `age_days`, `reactions`, `blocked_by:[#]`, and the `signals` that drove the call.
**Common blockers / pile-ups** are surfaced as `blockers`: nodes that many trains reference
as blocking them, **ranked by in-degree** — the structural reason work is stacking up. This
analysis leans directly on graphify's graph (another reason it's now core).

## Authored-content provenance — the timeline as an event stream

The social layer is only half the record. A person also ships **code**, and that code
carries embedded artifacts — **examples, READMEs/doc sections, code symbols, and inline
code-comments/doc-comments** — each with its *own* lifecycle: introduced in one commit,
changed in another, **removed or replaced** in a third. An example that shipped in March and
was dropped in May is real context for a retrospective, and git already records every step
of it in the clone. So the **timeline is the spine**, modeled as a single **event-sourced
stream**: social events (comment, review, reaction) and **code events** (artifact
add / change / remove, parsed from commit diffs) sit side by side, every event carrying
`{ actor, timestamp, ref, subject }`. People, feature deltas, examples, docs, and flow all
**derive** from this one stream rather than living in separate silos.

- **Artifact lifecycle ledger (`artifacts`).** Each tracked artifact — `kind ∈
  {example, doc, readme, symbol, comment}` — gets `path`/`name`, an ordered `lifecycle:
  [{ event: add|change|remove, commit, author, date, hunk, ref }]`, a current `status`
  (`live` | `removed` | `replaced`), and `replaced_by` when a rename/supersede is detected.
  Inline code-comments and doc-comments are tracked as first-class artifacts here (your
  call), so a single comment's birth and death are traceable — not just folded into an
  enclosing diff.
- **Full-window commit walk (your call).** Build the code-event stream by walking **every
  commit in the window** (local clone, zero-token) bounded to recognized artifacts, with
  git **rename/copy detection** (`-M -C`), so *added-then-removed-within-the-window* churn is
  captured even when it isn't attached to any train. This is CPU-heavy at AVM scale, so it's
  **phased**: Phase 1 computes net/tip-level deltas along trains; the full-window walk and
  inline-comment granularity land in **Phase 3** (see phasing), behind the same schema.
- **Feature deltas become a view.** `feature_deltas` (add/drop/change per code area) is now a
  **projection over `artifacts`** — every delta resolves to the artifact's lifecycle events
  and their authors, so "dropped" features carry *who removed it, when, and in which commit*.
- **Evidence rule unchanged.** Both the introducing hunk *and* the removing hunk are
  persisted inline as evidence; bulk diffs stay in the clone and are fetched on demand.

## Continuity & time-series (publish as a series)

Git is chronological, so reports are too: each run covers a **period**, and successive
runs form a **series** the user can publish over time and look back across (last 6 months,
last year). The bundle is therefore a **time-series record**, not a one-shot snapshot.

- **Stable train identity.** A train's `id` is **deterministic from its anchor** — the root
  issue number (`train-issue-<n>`), or the earliest PR number when issueless
  (`train-pr-<n>`). The same thread keeps the same id across every period it appears in, so
  "the AzureFirewall-policy thread" is recognizable whether it opened in March or is still
  open in June. This is the single change that makes everything below composable.
- **Bundles chain.** `meta.period` records the window; `meta.prev_bundle` points at the
  prior installment. A run **loads the previous bundle/index first** so it can compute
  cross-period state.
- **Overlap is safe because merge is by stable identity.** Monthly runs may deliberately
  **overlap by a day or two** so no item falls in the seam between windows (late merges,
  timezone/clock skew, backdating). Overlap never double-counts because a roll-up **unions by
  immutable identity** — PRs/issues by **number**, commits/code-events by **SHA (+path)** — not
  by appending. The overlap is a *gap guarantee*, the identity keys are the *dedup guarantee*.
- **Roll-up separates activity from structure.** A multi-month view (e.g. 6 months from existing
  monthly bundles) **unions the time-bound activity** (PRs/issues/commits/code-events/decision
  trains, deduped as above) across all installments, but takes **structure from the latest
  bundle** — `code_graph` areas/edges are a point-in-time snapshot of a moving tree, so the most
  recent `clone_sha` (Phase 3c.2) is the authoritative structural picture; older edge sets aren't
  merged in. Decision-train linking re-runs across the merged activity so a train spanning months
  (issue opened in April, PR merged in June) reads as one thread.
- **Train lifecycle across periods.** `first_seen` (period a train appeared),
  `last_activity`, `carried_over` (open at the prior period's end), and `prior_status` let
  a report say *"deferred in April, shipped in June"* — the through-line that makes a series
  read as a story, not disconnected snapshots.
- **Closing the forecast loop (default).** Each report **revisits the previous report's
  `next_candidates`** — did the predicted next-steps land? This is both a strong series
  hook ("last month we expected X; here's what happened") and a free, honest accuracy check
  on the forecast.
- **Series index (`series.json`).** A small running ledger in the workspace records each
  generated bundle (period, path, headline + open/carried trains). It's the entry point for
  "show me the series" and the cheap path for long views.
- **Long lookbacks: re-run is canonical, index is the fast path.** Because git is the source
  of truth, **re-running over a wide window (e.g. 6-month `from/to`) always yields a correct
  bundle** and is the authoritative way to produce a long view. The `series.json` index is
  the inexpensive through-line for stitching installments without re-cloning; it never
  overrides a fresh re-run.

## Non-goals (YAGNI)

- No dependency on third-party **skills** (`repo-analyzer`, `github-issue-analyzer`,
  `github-summary`). Useful references, but the skill is self-contained.
- **Code areas come from a pluggable provider, directory-first** (rev 8). The primary
  provider is a **directory-subtree** map (`area = module directory`) — zero-dep, offline,
  and matching how IaC repos define a module (AVM `avm/res/<svc>/<module>/`, Terraform
  `modules/<name>/`) — so code-area attribution works on Bicep/Terraform/any repo with no
  external tool. **graphify** is an *optional* provider used only for its ~25 tree-sitter
  languages (Python/TS/Go/Java/…); it does **not** parse Bicep/HCL, so it is no longer a hard
  dependency — when absent or inapplicable, code areas fall back to the directory provider.
  The provider yields an **area id** that every `code_area`/`area` field carries (a directory
  path, or `community:<n>` from graphify). **Dependency-edge enrichment lands in Phase 3c
  (rev 9; hardened in 3c.1/3c.2 — see rev 10 status):** inter-area
  `code_graph.areas[].edges` are resolved **authoritatively** — Bicep
  via `bicep build`→ARM (walking the full **transitive** nested-deployment tree, joined with
  source-ref parsing to recover each `br/public:…:<version>` identity), Terraform via
  `terraform init && terraform graph` (the resolved transitive module/resource graph). Edge
  extraction is **build-only**: edges populate solely from a successful build/restore and are
  left **empty** when the toolchain or registry is unavailable — never best-effort static text
  with unresolved versions/sub-deps. The `bicep` and `terraform` CLIs are therefore required
  for the edge gate (installed in CI/setup). **Symbol-granular artifacts (3d)** and **symbol-
  identity tracking across renames/moves (3e)** remain sequenced as later slices. (`git` and
  `GITHUB_TOKEN` remain required.)
- **mermaid-cli (`mmdc`)** is a **required dependency for Render** (Phase 2+): `render.py`
  validates every emitted `.mmd` by compiling it (and optionally exports SVG/PNG), so a
  diagram that would not render fails the run rather than shipping broken. The preflight
  checks `mmdc` is on `PATH` and **fails fast** with install guidance if absent.
- No `gh` CLI dependency. Auth is via `GITHUB_TOKEN` only. `git` is assumed present.
- No YouTube network access / transcript auto-fetch. The transcript is **user-provided**
  as a local file.
- No multi-repo aggregation in v1 (single target repo per run; one optional linked
  Projects v2 board). **Multi-repo — including Terraform aggregation — lands as Phase 9** on
  the rev-14 graph substrate (qualified ids make it a first-class store property).
- No write actions to GitHub. Read-only.
- The report is **Markdown with embedded Mermaid** diagrams (timelines, decision-train
  flowcharts, bucket charts) — renders on GitHub and most viewers with **no deps to view**,
  text-diffable. (`mmdc` is a build-time dependency used by Render to validate/pre-render the
  diagrams, not something a reader needs.) graphify's interactive `graph.html` is **linked**,
  not embedded. No
  **standalone HTML/PDF** report and no automated scheduling/delivery (cron, Slack) in
  v1 — invocation is manual / on-demand. (Noted as future adds.)

## Sprint & release modeling

The digest is framed around **previous / current / next**. Two independent planning
mechanisms are supported; either or both may be present:

- **Releases → GitHub milestones.** A milestone represents a release. Ordering is by
  `due_on` (fallback: creation order). The **current** release is the user-named
  milestone (`--milestone`) or the earliest open milestone with `due_on >= ref-date`;
  **previous** = most recent closed milestone; **next** = the open milestone after
  current.
- **Sprints → Projects v2 iteration field.** Each iteration has a start date + duration.
  Given a **reference date** (`--ref-date`, default = `--to`), **current** = the
  iteration containing the ref date; **previous** / **next** are the adjacent ones.

**Buckets** (computed deterministically in Link; refs only, by item number):
- **shipped** — PRs merged in window + issues closed as completed in window.
- **in_flight** — open PRs/issues assigned to the current sprint (iteration) or current
  milestone.
- **rejected/abandoned** — PRs closed **without merge** in window + issues closed as
  `not_planned` (wontfix) in window.
- **next_candidates** — open PRs/issues assigned to the next milestone or next
  iteration, plus open items labelled high-priority. Feeds the forecast narrative.

Each bucket item carries its **train id** so the report can cross-link a shipped PR to the
issue that started it and the decisions along the way.

## Components

### 1. `gather.py` (deterministic Acquire — clone + REST + GraphQL + graphify)

The single component that touches the network. Produces the bundle; touches neither the
transcript (Analyze input) nor the report prose (Synthesize output).

- **Deps:** Python 3 stdlib only (`urllib`, `json`, `argparse`, `datetime`, `subprocess`).
  GraphQL is a plain `POST /graphql` via `urllib`. Shells out to **`git`** (clone + log +
  show) and, when available, **`graphify`** — no pip install required for the core.
- **Auth:** reads `GITHUB_TOKEN`, falling back to `GH_TOKEN`. Exits with a clear error if
  neither is set. (Projects v2 needs a token with `read:project` scope.)
- **CLI:**
  ```
  python gather.py --owner OWNER --repo REPO \
      --from YYYY-MM-DD --to YYYY-MM-DD \
      [--branches main,develop] [--clone-dir PATH] [--no-clone] \
      [--include-docs] [--include-workflows] [--include-releases] [--include-internal] \
      [--include-projects] [--project-number N] [--project-owner-type org|user] \
      [--status-field Status] [--iteration-field Sprint] \
      [--milestone "vX.Y"] [--ref-date YYYY-MM-DD] [--out PATH]
  ```
  (`--include-workflows`/`--include-releases` default **on**; **graphify always runs**
  (required — preflight fails fast if absent). `--include-internal` opts the
  shame/blame appendix into the output (off by default). `--include-projects` activates
  when a project number is provided. When a project config is used, `SKILL.md` resolves
  these from config — see component 6.)

- **Clone + local git (the commit/diff layer, network-free after clone):**
  - `git clone --filter=blob:none --shallow-since=<from> --no-single-branch <repo> <clone-dir>`
    (bounded, partial). `--no-clone` reuses an existing `--clone-dir`.
  - Commits on the listed branches with author/commit date in `[from, to]` via local
    `git log --since --until --pretty --name-only` (+ `git show` for diffs when a train
    needs them). Captures `sha`, `message`, `author`, `date`, `files`, `parents`, and
    `pr` (resolved from merge structure / trailers in Link).
  - **Full-window code-event walk (zero-token, local; Phase 3):** `git log -p -M -C
    --since --until` over the window, **bounded to recognized artifact paths/blocks**
    (examples dirs, `*.md`/README headings, fenced code blocks, language-aware symbols, and
    inline code-/doc-comment blocks). Rename/copy detection (`-M -C`) links removals to
    replacements. Emits per-artifact `add|change|remove` events with `{ commit, author, date,
    hunk }` — the raw material Link folds into the `artifacts` ledger and the unified
    `timeline`. Captures *added-then-removed-within-window* churn that a tip-only diff misses.
    (Phase 1 ships tip/train-level deltas under the same schema; this walk turns on in Phase 3.)

- **Code-area provider (local, zero-token, pluggable; rev 8):**
  - **Directory provider (primary, always available).** Map each tracked file to its **module
    directory** via config patterns (AVM `avm/res/<svc>/<module>/`, any dir with a
    `main.bicep`; Terraform `modules/<name>/` or any dir with `*.tf`; otherwise a top-N-level
    dir). Zero-dep, offline — this is what makes code areas work on Bicep/Terraform.
  - **graphify provider (optional, supported languages only).** If `graphify` is on `PATH`
    *and* the repo has files in its ~25 tree-sitter languages, run `graphify update
    <clone-dir>` (no API/tokens) → read `graphify-out/graph.json` (real shape: each **node**
    carries a `community` integer + `source_file`; **edges are under `links`**; no top-level
    `communities` list and no labels without an LLM). Group nodes by `community` → areas; map
    `source_file` → area. **Not required**: if absent or the repo is unsupported (e.g. Bicep),
    silently use the directory provider.
  - Areas (from whichever provider) are stored under `code_graph` and used by Link to
    attribute trains/commits/people/artifacts/feature_deltas to code areas.
  - **Dependency edges (Phase 3c, rev 9; `gather.py`, build-only).** After the provider
    selects areas, `gather.py` enriches each area's `edges` with **inter-area dependency
    edges** by running the real IaC toolchain against the working-tree clone:
    - **Bicep:** `bicep restore` + `bicep build <area-entrypoint>` → ARM JSON; walk the
      **full transitive** `Microsoft.Resources/deployments` tree for the validated dependency
      structure, and parse the entrypoint **source** for `module … '<ref>'` / `br/public:…:<ver>`
      references to recover each immediate edge's resolved **area-id + version** label.
    - **Terraform:** `terraform init -backend=false` + `terraform graph` → parse the DOT
      output for the resolved **transitive** `module.*` / resource dependency graph.
    - Each edge is `{to, kind, ref, version, transitive, provider, resolved}` (see schema).
      **Build-only:** if the CLI/registry is unavailable the build is skipped and `edges`
      stays `[]` — the skill never emits static, unvalidated edges. (`bicep`/`terraform`
      are required for the edge gate; absent them edges are simply empty.)

- **REST fetches / windowing (the social layer):**
  - **PRs:** closed PRs touched in range via
    `GET /repos/{o}/{r}/pulls?state=closed&sort=updated&direction=desc` (paginated). Split
    into **merged in window** (`merged_at` in range) and **closed-without-merge in window**
    (`closed_at` in range, `merged_at` null). Plus **open** PRs for in-flight/next. For
    each: title, number, body, **author + `author_association`**, labels, reviewers,
    milestone, merged flag, **merged_by**, merged_at/closed_at, **closing-issue refs**,
    review comments with author + `author_association` (`.../pulls/{n}/comments`).
    **Diffs/changed-files come from the local clone, not the API.**
  - **Issues:** closed in window (`state=closed&since=`, excluding PRs via `pull_request`
    key) with `state_reason` (`completed`|`not_planned`); plus **open** issues. Capture
    milestone, labels, body, author + `author_association`, assignees, comments (with
    author + `author_association`), and **reaction counts** (`reactions` summary, for the
    upvoted-but-ignored signal). **Issue `kind`:** GitHub **issue types**
    (`GET /repos/{o}/{r}/issues/{n}` type, or repo issue-types list) → labels → template →
    heuristic.
  - **CODEOWNERS:** read from the clone (`.github/`/root/`docs/` CODEOWNERS) → `code_owners`
    map (path/glob → owning logins) for person↔module ownership. Network-free (local file).
  - **Timeline events** (for trains): `GET /repos/{o}/{r}/issues/{n}/timeline` for
    cross-references, `connected`/`cross-referenced`/`closed`-by-commit events linking
    issues ↔ PRs ↔ commits.
  - **Workflow runs** (`--include-workflows`): `GET /repos/{o}/{r}/actions/runs?created={from}..{to}`
    (paginated). Capture `name`, `conclusion`, `status`, `event`, `head_branch`,
    `created_at`, `html_url`.
  - **Releases** (`--include-releases`): `GET /repos/{o}/{r}/releases` filtered on
    `published_at` in window; capture `tag_name`, `name`, `published_at`, `prerelease`, url.
  - **Milestones:** `GET /repos/{o}/{r}/milestones?state=all`; capture `title`, `number`,
    `state`, `due_on`, `open_issues`, `closed_issues`, url.
  - **Pagination:** follow `Link` rel="next"; stop early using `from`/`to` where possible.
  - **Rate limiting:** on HTTP 403 with `X-RateLimit-Remaining: 0`, sleep until
    `X-RateLimit-Reset`, bounded retries. (GraphQL batching keeps the social layer well
    within budget.)

- **GraphQL fetch (`--include-projects`):** Projects v2 board — project title; the
  **iteration field** (iterations: `title`, `startDate`, `duration`); the **status
  (single-select) field** options; and `items` (paginated) each resolving to its linked
  issue/PR (`number`, `title`, `state`, `merged`) plus that item's `Status` and
  `Iteration` values. Field names configurable (`--status-field`, `--iteration-field`).

- **Output:** writes `workspace/activity-{from}-{to}.json` (override `--out`), alongside
  the reusable `--clone-dir` and `graphify-out/`. Bundle:
  ```json
  {
    "meta": { "owner","repo","from","to","branches","ref_date","clone_dir","generated_at",
              "period":{ "from","to" },"prev_bundle":{ "period":{},"path","url" }|null },
    "commits": [ { "sha","message","author","date","files":[paths],"parents":[],"pr":num|null } ],
    "prs": [ { "number","title","body","author","author_association","reviewers":[],
               "labels":[],"milestone","merged":bool,"merged_by","merged_at","closed_at",
               "files":[paths],"closes":[issue#],
               "review_comments":[{ "author","author_association","body","url","id" }],"url" } ],
    "issues": [ { "number","title","body","kind","facets":{ "area","priority","status","lifecycle" },
                  "author","author_association","assignees":[],
                  "labels":[],"state","state_reason","milestone","closed_at",
                  "reactions":{ "+1","-1","heart","hooray","total" },
                  "comments":[{ "author","author_association","body","url","id" }],"url",
                  "open_high_activity":bool } ],
    "label_taxonomy": { "<facet>": { "<namespace-or-value>":[label] }, "source":"auto|config|merged" },
    "timeline": [ { "ts","actor","layer":"social|code","event",
                    "ref":{ "type","number|sha","url" },"subject":{ "kind","name","path" } } ],
    "workflows": [ { "name","conclusion","status","event","head_branch","created_at","url" } ],
    "releases": [ { "tag_name","name","published_at","prerelease":bool,"url" } ],
    "milestones": [ { "title","number","state","due_on","open_issues","closed_issues","url" } ],
    "project": { "number","title","iterations":[{ "title","start","end" }],
                 "items":[{ "type","number","title","state","merged","status","iteration","url" }] },
    "code_graph": { "provider":"directory|graphify",
                    "edge_extraction":{ "resolved","timeout","failed","skipped" },
                    "areas":[{ "id","label","paths":[],
                               "edges_status":"resolved|timeout|failed|skipped",
                               "edges":[{ "to":area-id|null,"kind":"module|resource",
                                          "ref":"<raw bicep/tf reference>","version":str|null,
                                          "transitive":bool,"provider":"bicep|terraform",
                                          "resolved":bool }] }] },
    "modules": { "<dir>": { "commits","prs","files_changed" } },
    "workflow_stats": { "<workflow>": { "total","success","failure","cancelled","other" } },
    "docsRefs": [ { "path","source":"changed|referenced","pr":num|null } ],
    "release_train": { "previous":{}|null,"current":{}|null,"next":{}|null },
    "sprints": { "previous":{}|null,"current":{}|null,"next":{}|null,"all":[] },
    "trains": [ { "id","kind","root_issue":num|null,"prs":[num],"commits":[sha],
                  "spun_off":[issue#],"duplicate_of":issue#|null,"code_areas":[community],
                  "participants":[{ "login","role","author_association" }],
                  "outcome":"shipped|rejected|abandoned|deferred",
                  "first_seen":period,"last_activity","carried_over":bool,"prior_status",
                  "evidence":[{ "type","id","url" }] } ],
    "artifacts": { "<artifact-id>": { "kind":"example|doc|readme|symbol|comment",
                  "path","name","status":"live|removed|replaced","replaced_by":id|null,
                  "code_area":community,
                  "lifecycle":[{ "event":"add|change|remove","commit":sha,"author","date",
                                 "hunk","ref":{ "type","id","url" } }] } },
    "feature_deltas": [ { "area":community,"kind":"add|drop|change",
                          "subject":"param|resource|module|output|example|doc|comment|...",
                          "name","before","after","hunk","detail","artifact":id,"author",
                          "train":id,"pr":num,"commit":sha,"url" } ],
    "buckets": { "shipped":[ref],"in_flight":[ref],"rejected":[ref],"next_candidates":[ref] },
    "code_owners": { "<path|glob>":[login] },
    "people": { "<login>": { "internal":bool,"author_association","roles":{ "<role>":count },
                  "modules":[community],"areas":[],"prs_authored","prs_reviewed",
                  "review_latency","merge_rate","issues_reported","stale_owned":[issue#],
                  "examples_authored","docs_authored","symbols_authored",
                  "authored_then_removed":[{ "artifact":id,"removed_by":login,"commit":sha }],
                  "first_seen","last_active","evidence":[ref] } },
    "halls": { "fame":{ "top_shippers":[login],"top_reviewers":[login],"maintainers":[login],
                        "rising_community":[login] },
               "internal":{ "shame":[{ "login","metric","value","evidence":[ref] }],
                            "blame":[{ "login","metric","value","evidence":[ref] }] } },
    "flow": { "<issue#>": { "state":"hung|upvoted-but-ignored|traction-then-abandoned|blocked|healthy",
                  "age_days","reactions","blocked_by":[issue#],"signals":[],"evidence":[ref] } },
    "blockers": [ { "ref","kind","blocks":[issue#],"in_degree" } ],
    "diagrams": { "timeline_gantt":"diagrams/timeline_gantt.mmd","buckets_pie":"diagrams/buckets_pie.mmd",
                  "deltas_bar":"diagrams/deltas_bar.mmd","train_flowcharts":{ "<id>":"diagrams/train-<id>.mmd" },
                  "contributor_graph":"diagrams/contributor_graph.mmd","blocker_graph":"diagrams/blocker_graph.mmd",
                  "kind_breakdown":"diagrams/kind_breakdown.mmd","content_timeline":"diagrams/content_timeline.mmd" }
  }
  ```
  (`diagrams` is a **manifest**: each value is the workspace-relative path of a standalone
  `.mmd` file emitted by `render.py`, not an inline Mermaid string — see the Diagrams
  component below. References elsewhere to `diagrams.<name>` mean "the diagram named
  `<name>`", which now resolves to its `.mmd` file.)
  (`code_graph`, `timeline`, `trains`, `artifacts`, `feature_deltas`, and `diagrams` may be
  thin/empty in early vertical slices and thicken per phase — the schema reserves their place
  from Phase 1; the full-window `artifacts`/`timeline` code-event detail lands in Phase 3.)
  - **Ref convention:** every `url` field and every `evidence`/source entry is a
    `{ type, id|number|sha, url }` so any fact in any downstream renderer can be traced to
    its origin. The schema is documented for renderer authors in `BUNDLE.md`.

- **Artifact ledger + unified timeline (`artifacts`/`timeline`, computed in Link):** Link
  folds the code-event walk into per-artifact `lifecycle` chains (add→change→remove, with
  author + commit + hunk), sets each artifact's `status`/`replaced_by` (via rename
  detection), and **merges code events with social events into one chronological `timeline`**
  keyed by `ts`/`actor`. This stream is the substrate the people churn stats, feature deltas,
  and `content_timeline` diagram all derive from. (Phase 3 detail; tip-level in Phase 1.)

- **Feature delta ledger (`feature_deltas`, a projection over `artifacts`):** the
  deterministic add/drop/change record per code area, derived from artifact lifecycles +
  train diffs:
  - **add** — a new parameter/resource/module/output/example/doc appears (artifact's first
    `add` event, with the train/PR + author that introduced it).
  - **drop** — an artifact removed or deprecated (its `remove` event, **carrying who removed
    it, when, and in which commit**), *or* a PR closed-without-merge / an issue closed
    `not_planned`.
  - **change** — a default/type/allowed-values/content change across a train's commits.
  (Subject extraction is language-aware where cheap — Bicep/ARM `param`/`resource`/`output`,
  Terraform `variable`/`resource`/`output`, Markdown headings/examples — else a generic
  added/removed-symbol heuristic. Reserved Phase 1, populated from Phase 3.)

- **Label facets (`label_taxonomy`, computed in Link):** auto-detect label namespaces, map
  to facets (`kind`/`area`/`priority`/`status`/`lifecycle`), merge with any `projects.json`
  `label_taxonomy` override (override wins), and stamp `facets` onto each issue/PR — feeding
  `kind`, area attribution, forecast priority, and flow signals.

- **People graph (`people`/`halls`, computed in Link):** from train `participants` +
  `author_association` + `code_owners` + the `projects.json` internal config, Link builds
  per-login profiles (roles, modules, areas, review latency, merge rate, stale-owned) and
  ranks the **halls**. `halls.fame` is publishable; `halls.internal.{shame,blame}` is
  computed but flagged **internal-only** and excluded from public renders by default.

- **Flow & blockers (`flow`/`blockers`, computed in Link):** per-issue lifecycle state
  (hung / upvoted-but-ignored / traction-then-abandoned / blocked / healthy) from age +
  reactions + assignee/PR linkage + `blocked by #` refs; and a `blockers` list ranking
  nodes by how many trains they block (in-degree over the cross-reference graph).

- **Diagrams (`diagrams`, generated deterministically by `render.py`):** A dedicated
  offline stage (`render.py`) reads the **enriched bundle's existing fields** and emits each
  visual as a standalone Mermaid file under `workspace/diagrams/*.mmd` — built *from the
  data*, not hand-drawn by the model, so visuals are reproducible facts. The diagram inputs
  are **derived from the fields already in the bundle** (buckets, trains, prs/issues dates,
  releases, milestones, …); no presentation-specific data is duplicated into the schema.
  `bundle.diagrams` is a **manifest** mapping each diagram name to its `.mmd` path, so any
  post-stage (the Markdown digest, or another output) can discover and embed the files it
  needs. Adding a new diagram = a new emitter reading existing fields + a manifest entry.
  - **Each diagram uses the Mermaid type that fits its data** — not one shape forced
    everywhere. The set and their types:
    - `buckets_pie` — **`pie`** (shipped/in-flight/rejected/next counts).
    - `timeline_gantt` — **`gantt`** (item lifespans + releases over the window).
    - `train_flowcharts[id]` — **`flowchart`** per notable train (issue → PR(s) → commits → outcome).
    - `content_timeline` — **`timeline`** (artifact lifecycles — added/changed/removed).
    - `deltas_bar` — **`xychart-beta`** bar (add/drop/change per code area).
    - `contributor_graph` — **`flowchart`** graph (people↔module/train edges).
    - `blocker_graph` — **`flowchart`** graph (pile-ups ranked by in-degree).
    - `kind_breakdown` — **`pie`** (feature/bug/idea mix).
    - Reserved in the palette for later diagrams where they fit best: **`gitGraph`** (commit/
      branch/merge history of a train or window) and **`sequenceDiagram`** (review/CI
      interaction on a PR). Emitters pick the clearest type for the data they read.
  - **Validation (required):** `render.py` compiles every emitted `.mmd` with **`mmdc`**
    (mermaid-cli) and **fails the run** if any diagram does not render; optional SVG/PNG
    export lands beside the `.mmd`. This makes "the visual is a reproducible fact" literally
    enforced — a malformed diagram cannot ship. `mmdc` is a preflight-checked dependency.
  - Post-stages embed the referenced `.mmd` files. (Public renders omit shame/blame.)

### 2. `link.py` (offline, deterministic — train graph + buckets + people + flow)

Reads the bundle, writes it back enriched. No network. Builds `trains` from
closing-refs + commit trailers + timeline cross-references + merge structure; resolves
`label_taxonomy` facets and issue/train `kind`; folds the code-event walk into the
`artifacts` ledger + unified `timeline`; attributes each train to `code_areas` (graphify
communities) and to `participants`; computes `feature_deltas` (a projection over
`artifacts`), `buckets`, `release_train`, `sprints`, the `people`/`halls` graph
(internal-vs-community via `author_association` + `code_owners` + config, plus authored-code
churn), and `flow`/`blockers`; and emits the deterministic `diagrams` from that data. Pure
transforms over recorded data → fully unit-testable.

### 3. `SKILL.md` (procedure + analysis instructions)

Frontmatter:
```yaml
name: activity-overview
description: Use when you need a time-boxed engineering + sprint/release digest for a
  GitHub repo — shipped work, releases, CI/CD health, in-flight and abandoned items, the
  decision trains behind the work, design decisions, community-call highlights, a
  next-release forecast, and open risks, framed as previous/current/next sprint and release.
```

Procedure Claude follows:
1. Resolve the target: `--project <name>` from `projects.json`, or explicit
   `owner`/`repo`/options from the request (incl. project-board settings if any).
2. Resolve `from`/`to`, `ref-date`, milestone, clone dir, and optional transcript path.
3. **Preflight:** verify `GITHUB_TOKEN`/`GH_TOKEN` is set (needs `read:project` for boards)
   **and `graphify` + `git` + `mmdc` are on `PATH`** — fail fast with guidance if any is missing.
3a. **Load prior installment:** read `series.json` + the `prev_bundle` it names (if any), so
    Link/Analyze can compute carry-over and the forecast loop. Absent → treat as series start.
4. **Acquire:** run `gather.py` (clone + API + graphify + CODEOWNERS) → bundle.
5. **Link:** run `link.py` → bundle enriched with trains (with **stable, anchor-derived ids**
   + cross-period `first_seen`/`carried_over`/`prior_status`), issue/train `kind`, buckets,
   release_train, sprints, the `people`/`halls` graph, and `flow`/`blockers`; sets
   `meta.period`/`meta.prev_bundle`.
5b. **Render diagrams:** run `render.py` → emits `workspace/diagrams/*.mmd` derived from the
    enriched bundle's existing fields, recording the name→path manifest in `bundle.diagrams`.
    Each `.mmd` is **validated by compiling it with `mmdc`** (optional SVG/PNG export); a
    diagram that fails to render fails the run.
6. **Analyze:** for each significant train, **dispatch a parallel sub-agent** (see
   `superpowers:dispatching-parallel-agents`) that reads the train's thread + local diffs
   and returns a structured decision narrative **with an `evidence: [ref]` list — citing,
   not paraphrasing from memory** (every claim resolves to a bundle ref). (Early phases: a
   single inline pass.)
7. If a transcript is present, read it and extract community-call highlights; else skip.
8. **Synthesize:** write `workspace/activity-report-{from}-{to}.md` per `report-template.md`,
   weaving per-train narratives + call context + a **forecast** over `buckets.next_candidates`.
   Every claim must carry/resolve to a bundle ref; **verify provenance before reporting
   done** (`superpowers:verification-before-completion`).
9. **Update the series index:** append this run to `series.json` (period, bundle path,
   headline + open/carried trains) so the next installment chains to it.
10. Report the output paths to the user — both the **bundle** (the reusable fact base) and
    the digest — since downstream renderers consume the bundle.

### 4. Community-call transcript handling

- **Source:** user-provided local file (`.txt`, `.vtt`, `.srt`, `.md`). No network.
- **Location:** from `projects.json` (`transcript`) or passed explicitly; conventionally
  in `workspace/`.
- **Use:** summarized into a dedicated **"Community call highlights"** section AND woven
  into executive summary, design decisions, forecast, and open risks.
- **Optional:** absent transcript → section notes no call; rest of report unaffected.

### 5. `report-template.md` (fixed report shape)

Sections, in order (sections gated on data are omitted gracefully when absent):
1. **Executive summary** — sprint goals vs. outcomes; 3–5 bullets (features, releases, CI
   health, key risks), informed by call + board context. Embeds `diagrams.buckets_pie`
   (at-a-glance shipped/in-flight/rejected/next).
1a. **Since last installment** (omitted on the first report in a series) — closes the
    forecast loop against the previous report's `next_candidates` (predicted → landed /
    slipped / dropped) and lists **continuing threads** (`carried_over` trains with their
    `prior_status` → current status), so the report reads as the next chapter of a series.
2. **Release train context** — previous / current / next milestone (+ current sprint
   iteration window): dates, completion %, theme. Embeds `diagrams.timeline_gantt`.
3. **Shipped this period** — merged PRs + completed issues grouped by **code area**
   (graphify community / `modules`); each links its **train** (root issue → PR(s) →
   commits), summarizes the change, notes follow-ups. Links graphify's `graph.html`.
   - **Releases** (subsection) — versions published in window (tag, date, link;
     prereleases flagged).
4. **Decision trains** — the notable threads: how an idea moved from issue → PR →
   commits, where direction changed, what was rejected or spun off, and the outcome.
   (Authored from the per-train sub-agent analyses.) Each notable train embeds its
   `diagrams.train_flowcharts[id]`.
4a. **Feature changes (add / drop / change)** — the `feature_deltas` ledger as a table per
   code area (subject, name, kind, the train/PR/commit), with `diagrams.deltas_bar`.
4b. **Content lifecycle (built, changed, dropped)** — from `artifacts`: examples/docs/
   symbols (and notable comments) introduced, revised, or **removed/replaced within the
   window** — *who* authored and *who* removed each, with dates. Surfaces "we shipped an
   example for X in March and dropped it in May", which a tip-only diff hides. Embeds
   `diagrams.content_timeline`.
5. **In flight** — open items in current sprint/milestone (`buckets.in_flight`) with board
   status; flag items at risk of slipping.
6. **Rejected / abandoned** — PRs closed without merge + issues closed `not_planned`
   (`buckets.rejected`), with a one-line "why" where evident.
7. **CI/CD overview** — per-workflow success/fail counts (`workflow_stats`) + notable
   failed runs with links. (`--include-workflows`.)
8. **Bugs & reliability** — `bug`/reliability-labelled items; fixed vs still-open; themes.
9. **Infrastructure & tooling** — IaC (Terraform/Bicep/ARM), PowerShell, dependency
   changes (inferred from area + labels).
10. **Design decisions & docs** — ADRs/design docs touched or referenced (`docsRefs`) +
    decisions surfaced from commit diffs along trains, 1–2 sentence rationale each.
11. **Community call highlights** — topics, decisions, asks, follow-ups (when transcript).
11a. **Contributors & community** — the people view: internal-vs-community split, top
    shippers/reviewers/maintainers and **rising community contributors** (`halls.fame`),
    who owns/maintains which modules (`code_owners` + `people.modules`), and the
    `contributor_graph`. Recognition-focused; **public**.
11b. **Stalled, blocked & pile-ups** — `flow` pathologies (hung, upvoted-but-ignored,
    traction-then-abandoned, blocked) and the ranked **common blockers** holding up the most
    trains (`blockers` + `blocker_graph`), with the "why" where evident.
12. **Next-release forecast** — over `buckets.next_candidates`: what's likely to land next,
    confidence, slippage risks. The model's judgment over script-selected candidates.
13. **Open risks & next steps** — still-open high-activity issues + flagged PR review
    comments + call follow-ups + at-risk in-flight items.
- **Internal appendix (not in public renders):** `halls.internal.{shame,blame}` — slow
  reviews, stale-PR ownership, regression attribution. Gated behind an explicit
  `--include-internal` flag; **never emitted into the public-facing digest by default.**

### 5b. `BUNDLE.md` (schema doc for downstream renderers)

A human-readable reference for the bundle: every field, the **ref convention**
(`{ type, id|number|sha, url }`), where evidence lives inline vs. in the clone, and which
fields back which narrative claims. This is the contract the user's other renderers (L400
blog, video script, carousels) code against, so it ships with the skill and is kept in
sync with `link.py` output. Includes a "how to fact-check a claim" walkthrough and **marks
which fields are internal-only** (`halls.internal.{shame,blame}`) so renderers never leak
them into public output.

### 5c. `series.json` (running series index — generated state)

A small ledger in the workspace (one per repo) tracking every generated installment:
`{ repo, installments: [ { period, bundle_path, report_path, generated_at, headline,
open_trains:[id], carried_trains:[id] } ] }`. It's **generated output, not shipped config**
— it lets a run chain to its predecessor (forecast loop + carry-over) and is the entry point
for "show me the last 6 months as a series". Re-running a wide window stays canonical; the
index is the cheap through-line, never an override.

### 6. `projects.json` (optional per-project config)

- **Purpose:** avoid re-entering details for the three target projects.
- **Schema:**
  ```json
  {
    "projects": {
      "<short-name>": {
        "owner": "string", "repo": "string", "branches": ["main"],
        "clone_dir": "workspace/<name>-clone",
        "include_docs": true, "include_workflows": true, "include_releases": true,
        "transcript": "workspace/<name>-call-{period}.txt",
        "internal": { "domains": ["microsoft.com"], "orgs": ["Azure"], "logins": [] },
        "label_taxonomy": { "kind": ["Type:"], "area": ["area/"],
                            "priority": ["priority/"], "status": ["Status:","needs-"] },
        "project_v2": { "owner_type": "org", "number": 0,
                        "status_field": "Status", "iteration_field": "Sprint" }
      }
    }
  }
  ```
  (`project_v2` optional — omit for projects without a board; the digest then relies on
  milestones + dates only. `graphify` is no longer a per-project toggle — it's always
  required. `internal` **supplements** the derived author_association/CODEOWNERS/team
  signal for classifying maintainers/staff — e.g. Microsoft folks on AVM repos — it does
  not replace it. `label_taxonomy` **overrides/extends** the auto-detected label→facet map
  for repos whose scheme the auto-detector misses; omit it to rely on auto-detect alone.)
- **Resolution order:** `--config PATH` → `./projects.json` (cwd) → skill-dir
  `projects.json`. If none found or `--project` not given, fall back to explicit args.
- **Distribution:** ships `projects.example.json` (placeholders). User fills a real
  `projects.json` (commit-or-local is their choice).

### 7. `commands/activity.md` (slash-command entrypoint)

- Thin wrapper so the skill triggers as
  `/activity <owner/repo|project> <fromDate> <toDate> [options]`.
- Instructs Claude to invoke `activity-overview` with the parsed args — all logic stays
  in `SKILL.md` / `gather.py` / `link.py`.
- **Install note:** Claude Code discovers slash commands under `.claude/commands/`
  (project) or `~/.claude/commands/` (user). Ships at `commands/activity.md`; install
  copies/symlinks it. Documented in `REFERENCE.md`.

### 8. Tests (offline)

- `test_gather.py` — HTTP/GraphQL and `git`/`graphify` layers isolated behind small
  `_get(url)` / `_graphql(query,vars)` / `_git(args)` / `_graphify(dir)` seams that tests
  monkeypatch to return recorded fixtures. Covers window/merge filtering, commit parsing
  from `git log` fixtures, code_graph ingestion, workflow-stats, release filtering.
- `test_render.py` — diagram generation: each `.mmd` is deterministic given a fixture bundle,
  derives solely from existing bundle fields, and carries the correct Mermaid type header per
  diagram. The pure `.mmd`-string emitters run with no network/token (mmdc not required); the
  `mmdc` compile-validation runs in `test_render.py` only when a working `mmdc` is on PATH
  (skip-guarded otherwise, so the suite stays green without it).
- `test_link.py` — train construction (closing-refs + trailers + timeline + merges),
  duplicate/spin-off detection, code-area attribution, bucket assignment,
  release-train/sprint resolution, and `feature_deltas` extraction (add/drop/change, incl.
  Bicep/Terraform subjects). Includes a **provenance lint**: asserts every narrative-bearing fact
  (train, feature delta, quoted comment) carries a well-formed source ref. Runs with no
  network/token.

### 8b. Live integration smoke test (per-phase gate)

`.github/workflows/activity-overview-integration.yml` runs the **real** gather → link →
render pipeline against a live repo (default `Azure/bicep-registry-modules`) and asserts the
resulting bundle against the **current** contract. It runs automatically **twice a month**
(1st & 15th, trailing-14-day window) and **on demand** (`workflow_dispatch`, with an optional
owner/repo/window), authenticated by the `ACTIVITY_TEST_TOKEN` secret — a classic PAT with
`public_repo`; Microsoft-enterprise targets (e.g. `Azure/*`) cap PAT lifetime at ≤90 days, so
scheduled runs go red once it expires until the token is rotated.

**This is a required per-phase gate, not just background CI.** Every phase that changes what
the pipeline produces (new fields, buckets, sections) MUST, in the same PR: (1) update this
workflow's assertion block to the new bundle contract, and (2) run it — manually via
*Run workflow*, or by waiting for a scheduled run — and confirm it is **green on real data**
before the phase is considered done. The offline unit tests prove the units in isolation;
this proves the whole vertical slice still works end-to-end against a real repository. The
runner has no headless browser, so render runs with `--skip-validate` here (mmdc
compile-validation lives in `test_render.py`); the bundle and emitted `.mmd` files are
uploaded as a build artifact for inspection.

## Layout (committed, portable)

```
.claude/skills/activity-overview/
  SKILL.md
  gather.py                  # Acquire: clone + REST/GraphQL + graphify → bundle
  link.py                    # offline: train graph + buckets
  render.py                  # offline: bundle → workspace/diagrams/*.mmd (Mermaid)
  report-template.md
  projects.example.json
  BUNDLE.md                  # bundle schema + ref convention, for downstream renderer authors
  REFERENCE.md               # examples + install (incl. slash command) + troubleshooting
  commands/
    activity.md              # /activity slash-command wrapper
  test_gather.py
  test_link.py
  test_render.py
  fixtures/
    rest_sample.json         # recorded REST responses
    graphql_sample.json      # recorded Projects v2 GraphQL response
    git_log_sample.txt       # recorded git log/show output
    graph_sample.json        # recorded graphify graph.json
```

Self-contained for the core (Python stdlib + `git`); **`graphify` and `mmdc` (mermaid-cli)
are required external binaries** (preflight fails fast if absent — `mmdc` from Render in
Phase 2+). Copying the folder into `~/.claude/skills/` makes the skill usable in any repo,
with setup being `git` + `graphify` + `mmdc` on `PATH`, `GITHUB_TOKEN` (incl. `read:project`),
and optionally a `projects.json` + transcript.

## Implementation phasing — **vertical slices**

Each phase is a **complete vertical slice** (acquire → link → buckets → report) that
produces a real, **verifiable report** against the target repo. Later phases *thicken
every layer* rather than building one layer to completion. Test (and eyeball the report
against GitHub) after each.

- **Phase 1 — walking skeleton.**
  *Acquire:* shallow/partial clone (window) + minimal API (merged PRs + their closing
  issues + commit shas). *Link:* PR→commits (merge) + PR→issue (closing refs) → basic
  trains; coarse `shipped` bucket. *Report:* a real digest — "N PRs across M trains,
  bucketed as…, notable trains" — verifiable line-by-line against GitHub.
- **Phase 2 — social layer + full buckets + first visuals.**
  *Acquire:* + comments (counts), reviews (decision), timeline events (cross-refs), workflow
  runs (aggregated `workflow_stats`), releases, milestones; PRs gain `created_at`. *Link:*
  fold timeline cross-refs into PR↔issue linking; full `shipped` / `rejected` / `in_flight` /
  `next_candidates` buckets (one bucket per item, precedence shipped > rejected >
  next_candidates > in_flight; `in_flight` = open items active in window ∪ on the current
  (earliest-open) milestone; `next_candidates` = open items on the next milestone ∪
  high-priority-labelled). *Render:* new `render.py`
  emits `diagrams/buckets_pie.mmd` (`pie`) + `diagrams/timeline_gantt.mmd` (`gantt`), derived
  from existing bundle fields and **validated by `mmdc`** (preflight-checked dependency).
  *Report:* + CI/CD, releases, rejected/abandoned, in-flight sections embedding the two
  `.mmd` files.
- **Phase 3a — narrative substrate (shipped).** The actual discussion text (issue/PR comment
  bodies, review + review-comment bodies), reactions + `open_high_activity`, and the
  full-window code-event walk (`git log --name-status -M -C`) → file-level `artifacts` ledger
  + unified `timeline` + `feature_deltas` projection + `content_timeline`/`deltas_bar`
  diagrams. (File-level artifacts; symbol/inline-comment granularity deferred.)
- **Phase 3b — code areas (pluggable, directory-first) + label facets.**
  *Acquire:* the **code-area provider** (directory primary; graphify optional for supported
  languages) → `code_graph.areas`; CODEOWNERS. *Link:* attribute trains/commits/people/
  artifacts/feature_deltas to `code_area`s; resolve `label_taxonomy` facets + issue `kind`
  (native issue types → label facets → template → heuristic); `contributor_graph` diagram.
  *Report:* shipped grouped by code area; module ownership (`code_owners` + `people.modules`);
  facet-aware grouping. (Dependency-edge enrichment and symbol-granular artifacts are later
  slices — 3c/3d/3e below.)
- **Phase 3c — IaC dependency edges (build-only, full-transitive).**
  *Acquire:* after the code-area provider selects areas, `gather.py` enriches
  `code_graph.areas[].edges` with **inter-area dependency edges** resolved by the real IaC
  toolchain against the working-tree clone — **Bicep** via `bicep restore`+`bicep build`→ARM
  (walk the full transitive `Microsoft.Resources/deployments` tree, joined with source-ref
  parsing for the `br/public:…:<version>` identity) and **Terraform** via
  `terraform init -backend=false`+`terraform graph` (parse the resolved transitive DOT graph).
  **Build-only:** edges populate solely from a successful build/restore and stay `[]` when the
  CLI/registry is unavailable (no static fallback). *Render:* a `module_graph` flowchart of the
  resolved area→area edges. *Report:* a "Module dependency graph / blast radius" subsection.
  *Gate:* the integration workflow installs `bicep`+`terraform` and flips its prior
  `edges == []` assertion to the new edge contract, asserting Bicep edges resolve (with
  versions) on real data. (Symbol-granular artifacts and identity tracking still deferred to
  3d/3e.)
- **Phase 3c.1 — edge-build hardening (shipped).** Per-module builds run in **parallel** across
  a bounded worker pool (cuts the AVM-scale edge build ~24.5→~13 min on the live gate); each
  `bicep`/`terraform` subprocess is bounded by a **generous timeout** (only trips a genuinely
  hung process) and **retried once**. Gaps are made **visible, never silent**: each area carries
  `edges_status` ∈ `{resolved, timeout, failed, skipped}` and `code_graph.edge_extraction` carries
  the aggregate counts — `resolved`+empty means "no deps", `timeout`/`failed` means "not
  determined". The gate surfaces the summary and reds when the unresolved rate is non-trivial.
- **Phase 3c.2 — targeted edge re-resolution / resume (follow-on, post-review).** Make a partial
  failure cheap to close *without* recomputing the whole window. Edge resolution for an area is a
  **pure function of (its source at a commit, toolchain)** — independent of PRs/issues/comments —
  so only the `timeout`/`failed` areas need rebuilding to converge to a full run's result.
  Requires: (a) **pin provenance** — record `meta.clone_sha` (`git rev-parse HEAD` after clone)
  so a resume rebuilds against the *identical* tree (avoids a mixed-SHA graph if repo HEAD moved);
  (b) a **`--resume <prior-bundle>`** path that loads the prior `code_graph`, checks out
  `clone_sha`, re-runs `extract_iac_edges` over only the unresolved areas, merges, and recomputes
  `edge_extraction`; (c) optional **`--edges-only`** to skip the PR/issue re-pull entirely (under
  "nothing landed" they're unchanged), making resume just *re-clone-at-SHA + rebuild the gap*.
  The same `clone_sha` underpins deterministic multi-bundle roll-up (below).
  *Coverage (shipped):* the merge cores (`extract_iac_edges(only_status=…)`, `resume_bundle`,
  `rollup_bundles`) are offline-tested; the **`--rollup` CLI** has an offline end-to-end test;
  and the **resume git orchestration** (`fetch --depth 1 origin <clone_sha>` + checkout, in
  `resume_acquire`) is exercised by a **resume smoke step on the integration gate** against the
  live clone (round-trips the bundle, must not regress resolution). Both modes are wired into the
  SKILL procedure as alternative acquire steps (resume → re-run link+render; roll-up → link then
  render). A fresh wide-window re-gather remains the canonical long view.
- **Phase 3d — symbol-granular artifacts (shipped).** A single full-window `git log -p
  --unified=3` walk → `symbol_events`, attributed **diff-locally** (symbol declarations
  recognised within each hunk's lines — no per-commit checkout, no build). Link folds them into
  `kind:symbol|comment` artifacts (`<path>#<lang>:<subkind>:<name>`) and `feature_deltas` with a
  **bounded** (≤200 char) `before`/`after` + `detail` (`<lang> <subkind> <name>`). Covers Bicep
  (`param`/`var`/`output`/`resource`/`module`) and Terraform (`resource`/`variable`/`output`/
  `module`); graphify-language symbols are best-effort where the optional CLI is present.
  **Comments** are tracked by text, so one **replaced as a decision evolves** is captured as
  old-dropped + new-added (the decision trail — context for how ideas break into follow-on
  issues/PRs); markers (`TODO`/`FIXME`/`HACK`/`XXX`/`BUG`) carry subkind `todo`; decorative
  banners are ignored. The gate
  asserts the symbol ledger lights up (with bounded snippets) on a busy code window. *Original
  scope note:* extend the artifact ledger from file-granularity to **symbols** with `-p` hunk
  parsing; `artifacts[].kind` gains `symbol`/`comment`
  and `feature_deltas` gain `hunk`/`before`/`after`/`detail`. Builds on the 3c edge/area
  foundation. (Sequenced slice.)
- **Phase 3e — symbol-identity tracking (shipped).** Window-wide symbol MOVES: link a symbol
  dropped in one file to the same `(lang, subkind, name)` added in another (`link.match_symbol_moves`
  / `link_symbol_identity` → `symbol_moves` + artifact `replaced_by`/`identity_from`/
  `move_confidence`). **Precision over recall** (the spec's highest-risk slice): only UNIQUE
  pairings link (ambiguous/boilerplate names skipped) — validated on a refactor-heavy AVM window
  where a telemetry param/resource dropped-in-4/added-in-72 correctly yields **0** links instead
  of 288 false ones. `confidence` is `high` when git also flags the file pair as a rename/copy,
  else `medium`. Name-changed renames via body fingerprint (fuzzy) and 1→many splits are deferred.
- **Phase 4a — train significance + effort + forecast scaffold + flowcharts live (shipped).**
  *Link:* `score_train_significance` annotates each train with `significance` (float =
  `footprint × kind_weight + breadth`) and `tier` (`"deep"` for top-N by significance ∪ any
  train ≥ the floor, else `"mention"`); `annotate_train_effort` adds an `effort` block
  (`opened_at`, `merged_at`, `elapsed_days`, `reviewers`, `review_comments`, `commits`,
  `participants`, `stalled`). `slice_train(bundle, train_id)` is a pure, bounded, read-only
  per-train slice (six blocks: train / issue / prs / commits / feature_deltas / symbol_moves;
  text capped at `SLICE_TEXT_CAP`=1500 chars, comment lists capped at `SLICE_COMMENTS_KEPT`=6
  with `<key>_overflow` counts; raises `KeyError` on unknown id). `build_forecast` computes
  `bundle["forecast"]` over `buckets.next_candidates` — scored candidates with `tier`
  (`"likely"|"possible"|"longshot"`) and `signals`. *Render:* `diagrams.train_flowcharts` is
  now LIVE — a map `{ "<train-id>": "diagrams/<train-id>.mmd" }` written for every DEEP train
  (adaptive flowchart: mode C with code-area nodes when within `TRAIN_FLOW_MAX_PRS`/
  `TRAIN_FLOW_MAX_AREAS` thresholds, else mode A bare chain); on-demand spotlight render via
  `render.py --train <id>` (any tier). *Report:* DEEP trains embed their flowchart + an effort
  line; MENTION trains collapse to one-liners; Next-release forecast section renders tiered
  candidates with signals. Tunables: `TRAIN_KIND_WEIGHTS`, `TRAIN_SIGNIFICANCE_TOP_N`,
  `TRAIN_SIGNIFICANCE_FLOOR`, `TRAIN_STALL_DAYS`, `FORECAST_WEIGHTS`,
  `FORECAST_TIER_LIKELY_THRESHOLD`, `FORECAST_TIER_POSSIBLE_THRESHOLD`, `FORECAST_OVERDUE_DAYS`.
  *Still pending (4b):* sub-agent per-train narration; gather review/lifecycle slice.
- **Phase 4b — sub-agent train narratives (deferred, re-seated as Phase 10).** Originally the
  narration half of Phase 4. Deferred under the flat-bundle architecture; **rebuilt on the
  graph substrate as Phase 10** (below) so it is written once, reading a per-train slice over
  the store rather than the JSON bundle. Its gather review/lifecycle prereq moves with it.

**Rev-14 substrate + re-seated features (the unified ledger continues here).** P1–P4a above
are shipped under the flat-bundle seam. P5–P9 build the persistent graph substrate; P10–P14
then rebuild the deferred 4b and the original P5–P8 features **on the store** (several of which
shrink because the store does their heavy lifting — noted per phase). Each remains a verifiable
vertical slice; the **golden-bundle equivalence test gates every substrate phase**.

- **Phase 5 — graphstore foundation (substrate; shipped, #11).** `graphstore.py`: 3 node classes + typed
  edge table + `code_events` + FTS5 + `meta`; identity-keyed idempotent upserts; windowed range
  query; bounded spine traversal; `STORE.md`. Full offline unit + idempotency + determinism +
  scale tests. *Verifiable:* seed from existing fixtures, round-trip, assert idempotent union.
  Plan: `docs/superpowers/plans/2026-06-04-activity-overview-phase5-graphstore.md`.
- **Phase 6 — gather as writer + backfill (substrate; shipped, #12).** Repoint `gather.py`'s sink from
  JSON-assembly to store upserts (per-batch transactions); pin `clone_sha`; record
  `gathered_windows`. `backfill(id)` single-node on-demand bridge. *Verifiable:* re-gather an
  overlapping window into a fresh `.db` → no duplication; a backfilled out-of-window node
  appears exactly once. **P5 seam note (from the P5 final review):** graphstore's writers
  (`upsert_node`/`upsert_edge`/`add_code_event`/`index_text`) each `commit()` per call — fine
  for single ops, but P6's high-volume writes want per-batch atomicity/throughput, so P6 adds a
  defer-commit path (e.g. an optional `commit=True` arg or a batch/transaction context) rather
  than 50k individual commits.
- **Phase 7 — full graph substrate: extract + persisted derived facts (substrate; the gate; shipped, #13).**
  Expanded from "raw extract only" to stand up the **complete** substrate, built as three gated
  vertical slices that hold the same end-to-end equivalence gate green: **7a** `extract.py`
  (range query → train seeds → bounded spine traversal → materialized rev-13 view) + the ⭐
  golden-bundle equivalence test with `link`/`render`/report unchanged; **7b** lift link's pure
  derivations into a shared write-path module → persist **artifact** + **person** nodes and the
  non-spine edges (`authored`/`reviewed`/`owns`/`touches`/`depends_on`/`replaced_by`/…),
  `extract` materializes `artifacts[]`/`people[]`/`modules{}` from the store, `link` shrinks to
  window projections — *this unblocks P8*; **7c** wire `backfill(id)` on a traversal miss +
  roll-up/resume hardening. Persistence model: *facts (nodes/edges/ledgers, incl. backfilled
  threads) are durable; only roll-ups are recomputed, from those facts.* Detailed design:
  `docs/superpowers/specs/2026-06-04-activity-phase7-substrate.md`.
- **Phase 8 — spotlight (substrate analytics; absorbs original-P5 people view) (shipped, #14).** `spotlight.py`:
  person-impact, subsystem-split, pattern-evolution, commit-text-mining queries + focused
  renders. *Verifiable:* each query against a seeded multi-window store returns the expected
  aggregate with citations; the FTS query returns matches `O(matches)`.
- **Phase 8d — train completion (the completion orchestrator) (substrate; rev 15; shipped, #14).** Promote
  7c's single-node `backfill` + extract's inline loop into a shared **`complete.py`**: transitive
  spine completion **bounded by the query window** (else closure; level-0 directly-referenced
  anchors always filled), an **honest edge contract** on every train (`complete` + `gaps[{id,
  reason}]`, reasons `not_gathered`/`outside_window`/`unreachable`/`budget`), and **phantom
  pruning** (a 404 ref is removed, not reported, and remembered in a `dead_refs` tombstone so it
  is never re-chased). Shared by `extract` (refactored onto it, byte-identical default) and
  `spotlight` (the four queries gain optional `--complete`; trains carry `complete`/`gaps`
  offline-by-default, no network). gather stays the only writer/network (the seam is injected;
  `gather.backfill` gains an `absent`/`ABSENT` outcome and records dead refs). *Verifiable:*
  transitive reach + each gap reason on a fixture-backed fake; phantom recorded once and never
  re-fetched; extract 7c suite + trust gate green. Detailed design:
  `docs/superpowers/specs/2026-06-04-activity-phase8d-completion.md`.
- **Phase 9 — multi-repo (substrate; absorbs original-P8 multi-repo) — shipped (#15–#18,
  #20–#23).** Qualified-id namespacing end-to-end: `repos[]` manifest, cross-repo trains (PR in
  repo A closing an issue in repo B traverses as one thread), project-scoped people aggregating
  across repos, and **Terraform multi-repo aggregation** (the AVM-TF constellation). Hardened on
  real data: `blocks` (issue→issue) on the write path (#21), zero-dangling `depends_on` under the
  bicep trust gate (#22), and a multi-repo **constellation CI gate** running real `terraform
  init`/`graph` over real modules (#20, #23). *Verified:* cross-repo identity + cross-repo train
  tests green on real AVM-Bicep + AVM-Terraform data.
- **Phase 10 — 4b: sub-agent train narration, rebuilt on the store — shipped (#24–#26)** (spec:
  `docs/superpowers/specs/2026-06-06-activity-phase10-narration.md`). **Slice 1 (#24):** PR review
  submissions + timeline lifecycle events (already fetched, previously discarded) persist as `social`
  nodes attached to their PR/issue via **`part_of`** (spine, so the rounds ride inside the train
  slice; train identity unchanged) → true `review_rounds`/`reopen_count` + review-round texture.
  **Slice 2 (#25):** that texture folded into the bounded `slice_train`. **Slice 3 (#26):** `link.py
  --slice <id>` emits one train's enriched slice as JSON; the lead dispatches a parallel narrator
  sub-agent per deep train that reads ONLY its slice → a `{summary, proposed, changed, rejected,
  shipped, evidence:[ref]}` narrative (code layer — PR body/commits/`feature_deltas` — as primary
  source; reviews/lifecycle for the decision arc), every ref copied verbatim from the slice (lead
  verifies + composes into the `<!-- narrative: <id> -->` slot, with an optional lead-driven
  `git show` diff handle); the report's deep-train effort line surfaces the review-rounds/reopen
  texture. **Verified end-to-end against real AVM data** (Azure/terraform-azurerm-avm-res-network-
  virtualnetwork: review-round arcs + reopen counts persisted, slice carries the decision thread).
  **In-slice diffs enhancement — shipped (#28–#29):** a bounded, language-agnostic
  unified-diff hunk per changed file is captured in the gather code-walk (the existing
  `code_events.hunk` column), surfaced as `feature_deltas[].diff`, and bounded per train
  in `slice_train` — so `.tf`/`.bicep` *logic* changes carry their real diff in-slice
  (durable in the graph; no clone needed at narration time). Spec:
  `docs/superpowers/specs/2026-06-06-activity-phase10-slice-diffs.md`. **Follow-on —
  module evolution ("module biography") — v1 shipped (#30 + slice 2):** a store-backed
  `slice_module` (`spotlight module <area>`) over an area's full artifact lifecycle (CRUD
  + diffs across all history, rename chains folded) → the bounded biography slice a
  skill-side narrator turns into grounded prose + a Module biography report section
  (v2 = a structured IaC param-CRUD extractor, good-to-have). Spec:
  `docs/superpowers/specs/2026-06-06-activity-module-evolution.md`.
- **Phase 11 — people & community + flow/stall report views (original-P5 report half) —
  shipped (#31 + slice 2).** The data model + aggregation live in P5 (schema) / P6 (gather) /
  P8 (spotlight); this phase ships the **report sections** over them: **Contributors & community**
  (public) and **Stalled, blocked & pile-ups** (#31 — `blocks` surfaced + `blocker_graph`),
  the `contributor_graph`/`blocker_graph`/`kind_breakdown` diagrams, and the internal
  shame/blame appendix **gated off by default** (slice 2). Spec:
  `docs/superpowers/specs/2026-06-06-activity-phase11-people-community.md`.
- **Phase 12 — Projects v2 + sprint framing (original P6) — shipped (#34 + slice 2).**
  gather's first GraphQL call auto-discovers the repo's board → `board_status` on PRs/issues
  + `sprint-<id>` structure nodes + `in_iteration` edges when the board has an iteration
  field (mirrors `in_milestone`); `link.select_sprints` resolves previous/current/next; the
  report frames **board status** (status on in-flight + breakdown, universal) and **sprints**
  (release-train framing, only on iteration boards). Verified live on Azure/bicep #115 (a
  status-only board: 799 items with statuses, no sprints). Spec:
  `docs/superpowers/specs/2026-06-06-activity-phase12-projects-sprints.md`.
  **Follow-up (2026-06-06, this PR):** the smoke tests (Bicep / AVM-Terraform / AVM-Bicep)
  showed the "lowest-numbered board = primary" heuristic loses coverage on multi-board repos
  (`bicep-registry-modules` links #115/#538/#566; #115 has 0 BRM items). gather now ingests
  EVERY *maintained* board (skipping closed/stale via `board_is_maintained`) and merges their
  items — verified live: BRM's own items went from 0 → 3,063 statuses, still truthful
  (per-repo `(slug, number)` keying). Spec:
  `docs/superpowers/specs/2026-06-06-activity-phase12-all-boards.md`. (Also fixed `run_git`
  to decode non-UTF-8 git output, surfaced gathering Azure/bicep, #37.)
- **Phase 13 — series continuity (original P7; mostly absorbed by the store).** SHIPPED
  (slice 1, this PR). Cross-window state is now a wide range query, so this phase ships the
  **"Since last installment"** report section + the forecast-loop (predicted-vs-landed) and
  carry-over (`new`/`carried_over`/`prior_status`) via a pure `series.py`
  (`installment_snapshot` / `compute_series`) wired through `link.py --series series.json`.
  `series.json` is a thin **append-only** convenience index over the store, never an override
  of a re-gather; without `--series` the digest is byte-identical (no `series` key).
- **Phase 14 — transcript + slash command (original-P8 remainder).** Community-call section +
  the `/activity` slash-command entrypoint.

## Testing strategy

- **Unit (offline):** `test_gather.py` + `test_link.py` cover all deterministic transforms
  + train/bucket/release-train/sprint logic + config resolution against committed
  fixtures. Must pass with no network/token.
- **Vertical verification (per phase):** run the slice against a real target window
  (AVM-Bicep) with a token; confirm the bundle is well-formed and the report renders and
  **checks out against GitHub** (with/without transcript, board, graphify).

## Error handling

- Missing token → fail fast with actionable message; if `--include-projects` and token
  lacks `read:project`, name the missing scope.
- `git` missing / clone failure → fail fast naming the cause; `--no-clone` requires an
  existing `--clone-dir`.
- `graphify` absent or failing → **fail fast** with install guidance (it is a required core
  dependency with a preflight check; the run does not silently degrade without it).
- 404 (repo/project not found / no access) → clear error naming the resource.
- GraphQL errors / project number not found / missing iteration|status field → warn and
  degrade gracefully to milestone+date modeling (board sections omitted), not a hard fail.
- Unknown `--project` name → list available project names from config.
- Missing transcript (when expected) → warn, render without the call section.
- Empty window / empty buckets → valid bundle + report stating "no activity/none".
- Rate limit → sleep-until-reset with bounded retries, then fail with the reset time.

## Open questions

- The three real project coordinates (owner/repo, branches, doc layout, which have calls,
  Projects v2 numbers + field names) are not yet captured. The skill ships
  `projects.example.json`; the user supplies real values into `projects.json` after the
  skill is built (or hands them to Claude to pre-populate).
- graphify install/runtime cost on AVM-Bicep-scale clones to be measured during Phase 3
  (tree-sitter is local/zero-token, but wall-clock on a large tree needs a real timing).
