# Phase 8d — train completion (the completion orchestrator) — design

**Status: PROPOSED (rev 15).** Detailed design for **Phase 8d** of the
journey-graph substrate. Phase 7c shipped `gather.backfill` — a *single-node*
bridge that closes one spine-traversal miss on demand — and wired it into
`extract` as an inline, one-hop loop. Phase 8 shipped `spotlight`, the
cross-cutting reader, which today simply groups what is reachable and leaves a
dangling reference missing. **Phase 8d promotes backfill from a primitive +
inline loop into a first-class *completion orchestrator* (`complete.py`)** shared
by both readers, makes train completion **transitive and window-bounded**, and
gives every train an **honest edge contract**: a train is either whole or it
names exactly what it could not complete — without resurfacing template-
placeholder noise.

This is the rev that makes a decision train *answerable about its own edges*.

---

## Why (the problem this closes)

A decision train is the connected component over the causal spine
(`closes`/`part_of`/`spun_off`/`duplicate_of`; `cross_ref` excluded — see
spotlight's `_CAUSAL_SPINE`). Real trains reference nodes that the windowed
gather never pulled:

- **Cross-window anchors (the *why*).** A PR merged in-window `closes` an issue
  opened and closed *before* the window. The issue is the train's origin — its
  headline and rationale — yet it is `missing`. Phase 7c already backfills *this*
  one hop for the report.
- **Transitive context.** That backfilled issue was itself `spun_off` from an
  older RFC, which references a still-older design note. The *story* runs deeper
  than one hop.
- **Template-placeholder noise.** PR/issue bodies routinely contain dead
  references — `Fixes #123`, "see #456" — pointing at numbers that never existed
  (copied templates, typos). These surface as `missing` ids identical in shape to
  real cross-window anchors, so a naïve "report every dangling ref" makes a
  complete train *look* incomplete because of phantoms.

Three gaps in today's behaviour:
1. **One hop only.** 7c's extract loop re-traverses, but completion policy
   (how far, bounded by what) is inline and report-only.
2. **Spotlight can't complete at all.** Its four queries return `needs_gather`
   or silently drop the missing ref; a person/subsystem/grep train is narrated
   with a hole and no honest marker.
3. **No honesty contract, no phantom handling.** Nothing distinguishes "exists
   but out of scope" from "never existed", and a 404 phantom is re-chased on
   every run.

---

## What completion is (and is not)

`complete.py` is the **one home for completion policy**: given a train's reached
set and its `missing` spine references, decide *which* to fill, follow the causal
spine **transitively** to the bound, and return a completed reached set plus an
**honest gap list**. It owns the transitive BFS, the window bound, the budget,
and the **dead-ref memory**.

- **It is not a network layer.** The single-node fetch+upsert stays
  `gather.backfill` (the only code that touches the network and writes the
  store). `complete.py` *orchestrates* `backfill` through an **injected
  callable** — exactly the seam `extract` already uses — so the offline suite
  makes zero network calls and `spotlight` stays reader-only (the effect is the
  injected backfill's, not spotlight's).
- **It is not eager.** Completion runs at *read* time, bounded by the *query's*
  window, never at fold time (a fold has no query window to bound against and
  would fetch data no query needs — rejected, see Decisions).
- **It is the shared primitive both readers call.** `extract`'s inline 7c loop
  is refactored onto `complete.py`; `spotlight`'s four queries gain an optional
  completion step. One policy, one set of tests, two callers.

---

## The honest edge contract (the output)

Every train carries its completeness, always — computed from the store with **no
network** required:

```
train += {
  "complete": bool,                       # true ⇒ no unresolved spine refs
  "gaps": [ {"id": <qualified id>,
             "reason": "not_gathered" | "outside_window"
                     | "unreachable"  | "budget"} , … ]   # sorted by id
}
```

Reasons — the irreducible honest states:

| reason | meaning | when |
|---|---|---|
| `not_gathered` | referenced, never fetched, **no completion attempted** | offline default (no fetcher injected) — the honest "haven't looked" state |
| `outside_window` | exists but lies **beyond the query window**, deliberately not chased | completion ran, hit the window edge |
| `unreachable` | a fetch was **attempted and failed** (repo out of scope / network / rate limit) | transient or scope error from the seam |
| `budget` | completion **stopped here**, the per-query fetch ceiling was hit | budget exhausted mid-frontier |

**Phantoms are never gaps.** A reference the seam reports *definitively absent*
(a 404 — the number never existed) is **pruned**: removed from the frontier, not
listed in `gaps`, and **recorded dead** so no future query re-chases it. A
`#123` PR-template placeholder was never a real edge; reporting it would be
noise, not honesty. (The dangling edge stays in the store as a tombstoned
reference — we never destructively delete — but `is_dead_ref` keeps traversal
from re-surfacing it.)

So: **honest about real edges, quiet about phantoms.** A train with `complete:
true` is genuinely whole; a train with `gaps` names precisely why each hole
remains and which are worth a wider gather.

---

## Reach — transitive, window-bounded

Completion walks the **causal spine** (`_CAUSAL_SPINE`), breadth-first, in sorted
id order (deterministic). The **query's time window** is the bound; absent a
window, it runs to closure.

- **Level-0 anchors — always filled.** Every `missing` id *directly referenced by
  an in-window train node* is fetched, **regardless of its own date**. These are
  the train's immediate what/why (the cross-window issue a windowed PR closes).
  This is exactly 7c's existing one-hop behaviour, preserved.
- **Transitive expansion — window-bounded.** After a fetch, re-traverse. A newly
  surfaced `missing` id joins the frontier **only if the node that referenced it
  is itself within the query window**. A fetched node that landed *outside* the
  window is a **context boundary**: its own further missing refs are **not**
  chased — they are recorded as `outside_window` gaps.
- **No window ⇒ closure.** With no `--from/--to`, there is no boundary: the spine
  is chased to its connected-component closure, budget permitting.

Worked example (the redis story): a deprecation issue closed just before a
12-month window is a **level-0 anchor** → pulled in as context (the train's
*why*). The ancient RFC that issue cites is referenced by an *out-of-window*
node → **not** chased; it appears as one `outside_window` gap. The reader sees
the whole in-scope story plus an honest pointer to what a wider gather would add
— and never sees the `Fixes #123` phantom from the PR template (pruned).

**Determinism:** frontier processed in sorted id order; gaps sorted by id;
identical store + window ⇒ byte-identical result. Backfilled nodes are durable
upserts, so a second run is a no-op and equally byte-stable (modulo
`fetched_at`).

---

## Architecture & seams

```
  spotlight.<query> ─┐                      ┌─ gather.backfill(id, fetch)   (single node + cheap edges; WRITES)
                     ├─▶ complete.complete_train(conn, reached, missing,    │     └─ fetch(kind, local, qid) → fetched | ABSENT | None(unreachable)
  extract.extract ───┘        window, backfill=…, budget=…) ──────────────┘     └─ on ABSENT: graphstore.record_dead_ref(id)
                                   │  reads is_dead_ref to prune phantoms
                                   └─ returns {reached, gaps[], fetched_count}
```

**`complete.py` (new) — the orchestrator.** Pure-ish: reads the store via
`graphstore`, calls the **injected** `backfill` per frontier id, never imports a
network library, never writes (the write is `gather.backfill`'s). Public API:

- `complete_train(conn, reached, missing, *, window=None, backfill=None,
  budget=50, warn=None) -> {"reached": set, "gaps": [ {id, reason} ],
  "fetched": int}` — the transitive BFS above. `backfill=None` ⇒ no fetch
  (offline): every non-dead `missing` becomes a `not_gathered` gap; phantoms
  already recorded dead are pruned. `window=None` ⇒ closure.
- `annotate(train, result)` — stamp `complete`/`gaps` onto a `_train` dict.

**`gather.backfill` (extended).** Today returns `{"fetched": bool, "id"}`. Add
`"absent": bool` so the orchestrator can distinguish a definitive 404 (prune +
dead) from an unreachable error (gap). The **fetch seam contract grows a third
outcome**: today `fetch(kind, local, qid)` returns the payload or `None`
(conflating absent and unreachable). Split it:
`fetched payload` | `gather.ABSENT` sentinel (definitive 404) | `None`
(transient/unreachable). `backfill`, on `ABSENT`, calls
`graphstore.record_dead_ref(qid)` itself (preserving "only gather writes") and
returns `absent=True`.

**`graphstore.py` (extended) — dead-ref memory.** A new tiny table:

```sql
CREATE TABLE dead_refs (
  id         TEXT PRIMARY KEY,   -- qualified id known not to exist
  project    TEXT,
  reason     TEXT,               -- "absent" (404); room for more later
  first_seen TEXT                -- ISO ts (modulo: excluded from determinism asserts)
);
```

Helpers: `record_dead_ref(conn, id, reason="absent")` (idempotent upsert),
`is_dead_ref(conn, id) -> bool`, `get_dead_refs(conn) -> [id]`. `traverse_spine`
gains an optional `skip_dead=True` that drops known-dead ids from its `missing`
list, so even the report path stops re-surfacing pruned phantoms.

**`extract.py` (refactored).** Its inline 7c backfill loop is replaced by a
`complete.complete_train` call (window = the extract range). Behaviour for the
existing tests is preserved: `backfill=None` default is byte-identical (now via
the orchestrator's offline path); the budget ceiling, warn, and "context-only,
not in-window activity" guarantees are unchanged. The bundle additionally
carries each train's `complete`/`gaps` (additive).

**`spotlight.py` (extended).** Each of the four queries gains optional
`complete=`/`complete_budget=` params and threads `complete.annotate` onto every
`_train`. **Default (no fetcher) is offline and always-honest:** trains carry
`complete`/`gaps` computed from the store (no network), phantoms pruned. With a
fetcher injected (CLI `--complete`, which wires `gather.make_backfill_fetcher`
from a token), gaps shrink to the irreducible `outside_window`/`unreachable`/
`budget` set. The markdown renderers grow a compact, deterministic gap line
(e.g. `⚠ 2 gaps: 1 outside-window, 1 not-gathered`).

This keeps the invariants intact: **gather is the only writer / network**;
**spotlight is a reader** (its only effect is the injected backfill's);
**`complete.py` is policy, not I/O**.

---

## Testing strategy

TDD, fixture-backed fake fetcher (the `FakeFetcher` pattern from
`test_backfill.py`, extended to return `ABSENT` for phantom ids) — **no network**
in the suite.

- **Transitive reach** — a 3-deep cross-window spine (PR → issue → RFC → note);
  assert level-0 anchor always filled, transitive fill stops at the window edge,
  closure with no window.
- **Honest gaps** — each reason produced on demand: `not_gathered` (offline),
  `outside_window` (windowed, out-of-scope onward ref), `unreachable` (fetch
  returns `None`), `budget` (frontier larger than ceiling). Gaps sorted, cited.
- **Phantom pruning + dead-ref memory** — a `Fixes #123` placeholder returns
  `ABSENT`: it is *not* in `gaps`, `record_dead_ref` is called once, a re-run
  does **not** re-fetch it (`is_dead_ref` short-circuits), and
  `traverse_spine(skip_dead=True)` omits it.
- **Idempotency / determinism** — second completion is a no-op; identical store +
  window ⇒ byte-identical `complete`/`gaps` across runs.
- **Extract parity** — the existing `test_backfill.py` suite stays green on the
  refactor (default warn-only byte-identical; budget; one-complete-train).
- **Spotlight goldens updated** — person/subsystem/symbol/grep goldens gain the
  `complete`/`gaps` field (the rev's expected, justified golden churn); a new
  golden asserts a windowed person query with a real `outside_window` gap and a
  pruned phantom.
- **Trust gate green** — `validate.py` + Phase 7/8 suites unchanged otherwise;
  Phase 8d is additive (a new reader-side policy + a new tombstone table the
  auditor ignores).
- **Real-data smoke** — `--complete` against the AVM store fills at least one
  cross-window anchor and reports a coherent (small) gap set.

---

## Gated slices

- **8d-1 — dead-ref memory + seam split.** `graphstore.dead_refs` table +
  `record_dead_ref`/`is_dead_ref`/`get_dead_refs`; `traverse_spine(skip_dead=)`;
  `gather.ABSENT` sentinel; `gather.backfill` returns `absent`; on `ABSENT` it
  records the dead ref. Tests: phantom recorded once, never re-fetched.
- **8d-2 — `complete.py` orchestrator + extract refactor.** `complete_train`
  (transitive, window-bounded, budget) + `annotate`; `extract` refactored onto
  it with byte-identical default behaviour. Tests: transitive reach, the four
  gap reasons, extract parity (7c suite green).
- **8d-3 — spotlight completion + renders.** Optional `complete=`/CLI
  `--complete` on the four queries; `complete`/`gaps` on every train; gap line in
  the renderers. Tests: spotlight goldens updated, windowed-gap-with-phantom
  golden, real-data `--complete` smoke.

Each slice ships green under the existing trust gate; completion never mutates a
fact (only adds tombstones and durable backfilled nodes), so it cannot regress
Phase 7's guarantees.

---

## Decisions (resolved in brainstorming)

1. **Reach = transitive along the causal spine, bounded by the query window**
   (else closure). Level-0 directly-referenced anchors are always filled (the
   train's why); transitive expansion stops at the window edge.
2. **Honesty = real gaps only, prune phantoms.** `complete`/`gaps` with reasons
   `not_gathered`/`outside_window`/`unreachable`/`budget`; a 404 phantom is
   pruned and remembered dead, never reported.
3. **Home = a new `complete.py` orchestrator** shared by extract + spotlight;
   `gather.backfill` stays the single-node primitive; the network/write seam
   stays gather's, injected. *Rejected:* inlining into spotlight (duplicates
   extract's loop, two divergent policies); eager close at fold time (unbounded,
   no query window to bound against, fetches data no query needs).

> **Open for spec review:** (a) `budget` as a *distinct* gap reason vs folding
> into `unreachable` — proposed distinct, for honesty about *why* a hole remains.
> (b) Whether spotlight should compute `complete`/`gaps` **always** (offline,
> changing Phase 8 goldens) or only when `--complete` is passed (preserving
> goldens) — proposed **always**, because an answer that states its own edges
> every time is the whole point of the honesty contract.
</content>
</invoke>
