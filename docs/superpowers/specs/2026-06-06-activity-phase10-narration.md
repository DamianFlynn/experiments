# activity-overview — Phase 10: sub-agent train narration (4b), on the store

**Date:** 2026-06-06
**Status:** active — slice 1 (gather prereq) in progress. Edge model **confirmed:
`part_of`/spine**.
**Depends on:** Phase 9 (multi-repo) shipped to master (#15–#18, #20–#23); the
journey-graph store + `traverse_spine`/`slice_train`.

## Goal

Deliver the deferred **Phase 4b**: a **per-train decision narrative** authored by a
model from the store, plus the **gather prerequisite** that gives those narratives
real texture. Per the design spec's core principle — *gather is deterministic;
analysis is the model's judgment* — the pipeline persists the **facts** (review
submissions + lifecycle events as graph nodes/edges) and an **upstream content
agent** turns a bounded per-train slice into a narrative. The pipeline never writes
the prose.

## What's missing today (grounded in the code)

`acquire()` already **fetches** two signals and **discards** all but a reduction:

- **PR review submissions** — `gather.py` fetches `/pulls/{n}/reviews`, then
  `summarize_reviews` keeps only `{reviewers, decision}` (latest-per-reviewer). The
  individual submissions (author, `state`, `submitted_at`, body) are dropped → no
  `review_rounds`, no approve→changes-requested→re-review texture.
- **Timeline lifecycle events** — `gather.py` fetches `/issues/{n}/timeline`, then
  parses only `cross-referenced` events (for `closes`/`cross_ref`). `reopened`,
  `closed` (manual), `ready_for_review`, etc. are dropped → no `reopen_count`.

(Conversation + review *comments* are already persisted, embedded in the parent's
data blob — Phase 3a.)

## Node & edge model (decision: first-class nodes + edges)

We persist both signals as first-class **`social`** nodes so spotlight/extract can
traverse them and a sub-agent slice carries them. New local-id forms and edges:

### Review submission
- **Node** `review-<pr>-<review_id>` (`social`), `ts = submitted_at`,
  `data = {author, state, submitted_at, body}`. **Provenance:** the review's
  `html_url` when present, else a synthesized `<pr url>#pullrequestreview-<id>`
  ref (same fallback contract as lifecycle events).
- **Edge** `review-<pr>-<review_id>` —`part_of`→ `pr-<pr>`. We **reuse `part_of`**
  (a review is part of the PR thread, like a commit is): this keeps reviews inside
  the train when `traverse_spine` walks it, so a sub-agent reading the train slice
  sees the review rounds. `part_of` src kinds extend to `{commit, review}`.
- The existing `person —reviewed→ pr` contribution edge is **unchanged** (who
  reviewed the PR); per-submission author lives in the node `data` (no per-review
  person edge in this slice — avoids a `reviewed` overload).

### Lifecycle event
- **Node** `event-<parent>-<event_id>` (`social`), `ts = created_at`,
  `data = {actor, event, created_at, label?}` where `event ∈ {reopened, closed,
  ready_for_review, …}` (a conservative allowlist; `cross-referenced` stays on its
  existing path, not duplicated here). **Provenance:** the event `url` when present,
  else the parent's `html_url` + `#event-<id>` (a stable synthesized ref so
  `check_provenance` passes).
- **Edge** `event-<parent>-<event_id>` —`part_of`→ `pr-<parent>` / `issue-<parent>`.
  `part_of` dst kinds extend to `{pr, issue}`.

### Derived (read side, like `forecast`/`modules`)
- `review_rounds` per PR = count of its review submission nodes (and the ordered
  `state` sequence for texture).
- `reopen_count` per issue/PR = count of its `reopened` lifecycle nodes.

### validate impact
- `_EDGE_SCHEMA`: `part_of` becomes `({commit, review, event}, {pr, issue})`.
- `_id_kind`: classify `review-`/`event-` locals as `review`/`event` (social).
- `check_provenance`: review/event nodes carry a `url`/source ref (above) → pass.
- `referential_integrity`: `part_of` is spine, so a review/event whose parent is
  out-of-window is a tolerated backfill miss (INFO), never a dangling ERROR — same
  as `closes`. No new dangling class.
- `no_drift`/`idempotency`: review/event nodes are folded by stable id from the raw
  records (idempotent); they are not people/artifacts, so `no_drift` is unaffected.

**Spine note (confirmed `part_of`):** making reviews/events `part_of` pulls them
into `traverse_spine`, so trains gain review/event members. `extract` keys trains on
PR/issue **social anchors**, so train *identity* is unchanged, but the per-train
**slice** grows to include the rounds/lifecycle — which is the point (the sub-agent
needs them, with no extra plumbing in `slice_train`). Review/event nodes are spine
**leaves** (no onward spine edges), so they can't bridge a train to unrelated nodes.
The non-spine `on`-edge alternative was rejected (it would force `slice_train` to
re-gather the texture out of band).

## Slices (TDD, task-by-task, each its own PR)

**Status:** slices 1 (#24) + 2 (#25) shipped; slice 3 in progress.

1. **Gather prereq (shipped, #24).** Persist review submissions + lifecycle events as
   the nodes/edges above; derive `review_rounds`/`reopen_count`. Offline-testable
   from fixtures. Network: PR reviews and the PR timeline are already fetched; this
   slice adds **one** new call — the issue `/timeline` (issues did not fetch it
   before) — to capture issue lifecycle (`reopen_count`). Validate stays green.
   **Byte-stability:** single-repo extract gains the new keys only when the data
   exists — gate the golden-bundle equivalence accordingly.
2. **`slice_train` enrichment (shipped, #25).** Fold the review/lifecycle texture into
   the bounded per-train slice `slice_train(bundle, id)` so a sub-agent gets the full
   thread (`review_rounds`, capped `reviews`/`lifecycle`, `reopen_count`).
3. **Sub-agent train narration (this slice).** Mechanism: `link.py <bundle> --slice
   <train-id>` emits one train's enriched slice as JSON (read-only). The lead agent runs
   it per deep train and dispatches **one narrator sub-agent per deep train, in parallel**;
   each reads ONLY its slice → a structured narrative `{summary, proposed, changed,
   rejected, shipped, evidence:[ref]}`, every claim traced to a ref copied verbatim from
   the slice (never invents). The lead **verifies** (drops any evidence ref not in the
   slice), then composes the narrative into the deep train's `<!-- narrative: <id> -->`
   slot. Protocol authored in `SKILL.md` ("Phase 4b"); `report-template.md` points the
   project view at it. The narration is the *model's judgment over the slice* — gather
   stays deterministic; the prose is never pipeline-written.
4. **Report.** Deepen the "Decision trains" section: narrative + `review_rounds` /
   `reopen_count` / effort texture per deep train.

## Testing
- Unit (offline): `normalize_review` / lifecycle-event parse (direction, allowlist,
  provenance ref); fold emits the nodes + `part_of` edges; derive `review_rounds`/
  `reopen_count`; validate green on a store carrying them.
- Vertical (real data): the existing `live`/`spotlight`/`constellation` gates already
  exercise reviews/timeline on real repos — confirm validate stays green and the
  new counts populate.

## Not in scope
- `in_iteration` (Projects v2) — Phase 12.
- The narration *orchestration* mechanics (how sub-agents are launched) — defined in
  slice 3, not this slice.
