# activity-overview — Phase 11: people & community + flow/stall report views

**Date:** 2026-06-06
**Status:** in progress.
**Depends on:** the data model already in the store — `people` (P5 schema / P6 gather /
P8 spotlight, extract-materialized), the `stalled` train flag + `effort` (Phase 4a),
`open_high_activity` on issues (Phase 3a), and the `blocks` issue→issue edges (Phase 9
#21). The `contributor_graph` + `kind_breakdown` diagrams (Phase 3b) already render.

## Goal

Ship the **report half** of the original-P5 people/community + flow work — two new
report sections over data that already exists, plus the one missing diagram:

1. **Contributors & community** (public) — who moved the project this period: the
   contributor graph, per-person modules/areas, review/authoring footprint.
2. **Stalled, blocked & pile-ups** — where flow is stuck: stalled trains
   (`effort.stalled`), blocked issues (`blocks` edges, rendered as a `blocker_graph`),
   and pile-ups (`open_high_activity` issues).
3. A **shame/blame appendix** (per-person stall/blocker attribution) — **gated OFF by
   default** (internal-only; never in the public digest unless explicitly enabled).

**Narration is the skill's (agent's) job.** Per the project principle, the pipeline
ships the deterministic *data + diagrams*; the report **prose** (who/what/why) is
written by the lead agent from those facts, never pipeline-generated. These sections
are agent-rendered over the bundle, like every other report section.

## What exists vs. what's new

| Piece | Status |
|-------|--------|
| `people` (modules/areas/authoring/reviewing) | exists — extract-materialized |
| `contributor_graph`, `kind_breakdown` diagrams | exist (Phase 3b) |
| `stalled` train flag + `effort` | exist (Phase 4a) |
| `open_high_activity` issues | exists (Phase 3a) |
| `blocks` issue→issue edges | exist in the **store** (Phase 9 #21) — **not yet surfaced** in the bundle |
| `blocker_graph` diagram | **new** |
| "Contributors & community" / "Stalled, blocked & pile-ups" report sections | **new** |
| shame/blame appendix (gated) | **new** |

## Design

### Surface `blocks` into the bundle (extract)
`fold` already writes `blocks` (issue→issue) edges; `extract` must materialize them
onto issues so the report + diagram can read them without touching the store:
`issue["blocks"]` (issue ids this one blocks) and `issue["blocked_by"]` (inbound).
Omit-when-empty (byte-stability; goldens carry none).

### `blocker_graph` diagram (render.py)
`emit_blocker_graph(bundle)` — a Mermaid graph over the `blocks` edges among in-window
issues (node = issue #, edge = "blocks"), mirroring `emit_module_graph`'s style; emitted
into `diagrams` only when non-empty. Bounded (cap nodes/edges with an overflow note).

### Report sections (SKILL.md + report-template.md)
- **Contributors & community:** rank contributors by footprint (authored/reviewed/
  areas), embed `contributor_graph`, cite people→PR/area refs. Public-safe (no blame).
- **Stalled, blocked & pile-ups:** stalled deep trains (`effort.stalled`,
  elapsed/opened days), blocked issues (`blocked_by`/`blocks`, embed `blocker_graph`),
  pile-ups (`open_high_activity`), each cited. Frame as flow signals, not blame.
- **Shame/blame appendix (gated):** per-person stalled/blocking attribution — rendered
  ONLY when an explicit opt-in is set (a `--internal`/config flag the agent passes);
  the public digest never includes it. Document the gate clearly in SKILL.md.

## Slices (TDD)

1. **Flow data + blocker_graph + the "Stalled, blocked & pile-ups" section.** Surface
   `blocks` onto issues in `extract` (omit-when-empty); `emit_blocker_graph` (render);
   SKILL.md + `report-template.md` section over stalled trains / blocked issues /
   pile-ups. Goldens stay byte-identical; `validate` green.
2. **"Contributors & community" section + gated shame/blame appendix.** SKILL.md +
   `report-template.md` over `people` + `contributor_graph`; the appendix behind an
   explicit internal opt-in, OFF by default.

## Testing
- Unit (offline): `extract` surfaces `blocks`/`blocked_by` (omit-when-empty, goldens
  byte-identical); `emit_blocker_graph` (nodes/edges, empty→omitted, bounding);
  deterministic ordering. `validate`/characterization green.
- Vertical: re-gather a real repo window with blocked issues / stalled trains and
  confirm the sections + `blocker_graph` populate and cite real refs.

## Not in scope
- Projects v2 / sprint framing (Phase 12).
- Any non-public attribution in the default render (the shame/blame appendix is gated).
