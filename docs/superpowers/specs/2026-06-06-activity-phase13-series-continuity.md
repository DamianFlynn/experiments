# activity-overview — Phase 13: series continuity ("Since last installment")

**Date:** 2026-06-06
**Status:** in progress.
**Depends on:** the durable store (cross-window state is a wide `range_query`); the
enriched bundle's `buckets` (shipped / in_flight / rejected / next_candidates) +
`forecast` + (Phase 12) `board_status`.

## Goal

A digest is usually one in a **series** of installments for the same project. Phase 13
adds the **"Since last installment"** framing on top of what the store already holds:
- **Carry-over** — which items are **new** this installment vs **carried over** from the
  prior one, and each carried item's **`prior_status`**.
- **Forecast loop** — the prior installment's `forecast` predictions vs. what actually
  **landed** this window (predicted-and-shipped vs predicted-but-not-yet).
- A thin **`series.json`** index: the ordered list of past installments' key facts. It is
  a *convenience index over the store*, **never an override** — a re-gather is always
  truth; `series.json` only records what each installment reported.

"Mostly absorbed by the store": the data is already cross-window queryable, so this is a
**comparison + report** layer, not new acquisition. No network.

## Design

### `series.py` (pure)
- **`installment_snapshot(bundle)`** → the compact record this installment contributes to
  the index: `{from, to, ref_date, shipped:[ref], in_flight:[{ref, board_status?}],
  forecast:[{ref, tier}]}` (refs are the `{type, id}` the buckets/forecast already use;
  url dropped — the store/ re-extract is truth).
- **`compute_series(bundle, prior)`** → a `series` block (does NOT mutate items):
  - `first_installment`: `prior is None`.
  - `new`: bucket items (shipped+in_flight) whose `(type,id)` was in neither the prior's
    shipped nor in_flight.
  - `carried_over`: items present in the prior's `in_flight` and still here, each with
    `prior_status` (prior board_status/bucket).
  - `forecast_loop`: over the prior's `forecast` refs → `landed` (now in this window's
    shipped), `not_yet` (still not shipped). Empty on the first installment.
  Deterministic ordering (by type, id).

### Reader wiring (`link.py --series <path>`)
After `enrich`, when `--series series.json` is given: load it (an ordered list of
installments; `[]`/absent ⇒ first installment), `bundle["series"] =
compute_series(bundle, installments[-1] if any)`, then **append**
`installment_snapshot(bundle)` and write the file back. Without `--series`, no `series`
key is added (byte-stable; the standard digest is unchanged). `series.json` lives beside
the run, is append-only, and is regenerable (drop it ⇒ next run is "first installment").

### Report (`SKILL.md` + `report-template.md`)
A **"Since last installment"** section (omitted on the first installment / no `--series`):
new vs carried-over items (with `prior_status`), and the forecast loop (predicted →
landed / not-yet), each cited. Deterministic data; framing prose is the agent's.

## Slices (TDD)
1. **`series.py` + `--series` wiring + report.** `installment_snapshot`/`compute_series`
   (pure, offline-tested: first installment, carry-over, forecast-loop landed/not-yet);
   `link.py --series`; the report section. `series` appears only with `--series`, so
   goldens/characterization stay byte-identical; `validate` unaffected (no store change).

## Testing
- Unit (offline): first installment (all `new`, empty loop); second installment with a
  crafted prior (carry-over + `prior_status`; forecast landed vs not-yet); snapshot shape;
  deterministic ordering; `--series` round-trips + appends.
- Vertical: two successive `extract → link --series` runs over adjacent windows of a real
  repo — confirm the second reports the first's still-open items as carried-over and any
  predicted-and-shipped in the loop.

## Not in scope
- Auto-discovering the window cadence — installments are whatever windows are run; the
  index records them in order.
- Any mutation of the store from `series.json` (it is read-only convenience).
