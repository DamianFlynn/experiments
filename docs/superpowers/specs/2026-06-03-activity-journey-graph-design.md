# activity-overview → journey-graph substrate — design

**Status: SUPERSEDED / MERGED.** This standalone design has been folded into the canonical,
evolving spec as **rev 14**:

→ **`docs/superpowers/specs/2026-06-01-activity-overview-design.md`**
  (see *Architecture (rev 14) — persistent graph substrate*, and the unified **P1–P14**
  ledger under *Implementation phasing*).

The decision to keep a single evolving spec (one source of truth carrying the full phase
ledger from the shipped flat-bundle work through the graph-substrate redesign) means this
file is retained only as a pointer so historical links don't dangle. All substance — the
SQLite property-graph substrate, the four locked decisions, the artifact→node mapping,
gather-as-writer + `backfill`, `extract` (with the golden-bundle equivalence gate),
`spotlight`, and the testing strategy — lives in the rev-14 spec above.
