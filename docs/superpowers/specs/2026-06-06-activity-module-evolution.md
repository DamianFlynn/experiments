# activity-overview — Module evolution (the "module biography")

**Date:** 2026-06-06
**Status:** v1 **shipped** — slice 1 (#30, `slice_module` + `spotlight module <area>`)
and slice 2 (this PR, skill-side biography narrator + Module biography report section);
v2 (structured IaC param·output·variable extractor → API-surface changelog) is a
good-to-have, future.
**Depends on:** the durable journey-graph store; the artifact lifecycle ledger
(`code_events`, now carrying `hunk`/`before`/`after` — Phase 10 + the in-slice diffs
enhancement #28/#29); `area_index` (path→area); the Phase 10 narrator pattern.

## Vision

Pivot the lens from *"what happened in this window"* (trains) to **"how has THIS
module evolved over time."** Pick a module (an `area`, e.g. `modules/subnet` or an AVM
module path) and tell its **biography**: the **parameter/symbol CRUD** at the detail
level (create → update → delete across the whole accumulated history) plus **prose**
that explains the whole — what changed, why, and how the API surface moved.

This is the durable store's payoff: the store *accumulates* a module's lifecycle across
every gathered window, so the biography spans all of history, not one digest window.

## What the substrate already gives us

- **CRUD over time** — the `code_events` ledger holds each artifact's full lifecycle
  (`add`/`change`/`remove`), date-ordered, keyed by `artifact_id`, with `before`/`after`
  (graphify symbols/comments) and now `hunk` (the bounded file diff, every language).
  For a Bicep param symbol that *is* create/update/delete with the actual change text.
- **Renames** — Phase 3e `symbol_moves` (`replaced_by`/`identity_from`) chains a symbol
  across path/name changes, so a param's history survives a rename.
- **Driving change** — each lifecycle event carries its `commit` → (via the store's
  commit→PR map) the PR/train that drove it.
- **Narration** — the Phase 10 grounded-narrator pattern (read a bounded slice → a
  sourced `{summary, …, evidence:[ref]}` narrative; lead verifies).

## Design

### `slice_module` — a store-backed, full-history module slice
Unlike `slice_train` (a window bundle helper), a module biography spans **all** gathered
history, so it reads the **store** directly (spotlight-family). A new spotlight query:

`module <area>` → `slice_module(conn, project, area, repos=None)`:
1. Resolve the area's artifact set: all `code` artifact nodes whose `path` falls under
   the area (`area_index`/`_area_for_path`), file + symbol/comment.
2. For each artifact, read its **full lifecycle** from the ledger
   (`graphstore.get_code_events(artifact_id)` — date-ordered): the CRUD timeline with
   `event`, `date`, `commit`, and the change detail (`before`/`after` for symbols,
   bounded `hunk` for files). Follow `symbol_moves` so a renamed symbol's history is one
   chain.
3. Attribute each event to its PR/train (commit→PR map) so the biography links to the
   decisions.
4. Assemble a **bounded** slice:
   ```
   { area, time_range:{first, last}, repos,
     symbols: [ { id, kind, subkind, name, status,            # e.g. bicep param
                  lifecycle:[ {event, date, commit, pr, before*, after*, diff*} ] } ],
     files:   [ { path, lifecycle:[ {event, date, commit, pr, diff*} ] } ],
     trains:  [ train ids that touched the area ] }
   ```
   Bounding mirrors the slice caps (per-text `_cap_text`, a per-module diff budget like
   `SLICE_DIFF_CAP`, and a symbol/event cap with overflow counts) so the slice stays a
   context unit, not an archive.

### v1 — prose-over-diffs/CRUD (this phase)
A **module-biography narrator** (Phase 10 pattern) reads the `slice_module` slice and
returns a grounded narrative: `{summary, surface_changes, notable_revisions, removed,
evidence:[ref]}` — `surface_changes` reads the param/symbol CRUD straight off the
lifecycle + `before`/`after`/`diff` (the model extracts "param X added → default changed
→ removed" from the in-slice detail); `evidence` cites commit/PR refs verbatim from the
slice. The lead verifies (drop refs not in the slice) and renders a **Module biography**
report section. No new gather capability — rides entirely on what's stored.

### v2 — structured param extractor (future, separate)
A Bicep/Terraform **param·output·variable extractor** (a real symbol parser for `.bicep`
params/outputs and `.tf` variables/outputs, beyond graphify's best-effort) → a
**deterministic, queryable API-surface changelog** table (`param | CRUD | when | PR`),
with prose on top. Bigger (new gather capability + schema), sequenced separately.

## Slices (TDD)

1. **`slice_module` + CLI (shipped, #30).** The store-backed slice above + a
   `spotlight.py module <area>` subcommand that emits the bounded slice JSON (and a
   markdown render). Connected-component rename folding; bounded; deterministic.
   Verified on real AVM data.
2. **Biography narrator + report (this PR).** The narrator is the **skill's (agent's)
   job, never pipeline code** — a `SKILL.md` protocol (mirroring Phase 4b) where the
   lead reads a `slice_module` slice and emits a grounded, sourced narrative
   (`{summary, surface_changes, notable_revisions, removed, evidence:[ref]}`, every ref
   verbatim from the slice; lead verifies), then renders a **Module biography** section
   in `report-template.md`. The pipeline stays deterministic; the prose is the model's.
3. **(v2, good-to-have, future)** the structured IaC param·output·variable extractor +
   a deterministic API-surface changelog table (`param | CRUD | when | PR`).

## Testing
- Unit (offline, crafted store): area→artifact resolution; full-history lifecycle
  assembly (CRUD ordered by date, with diff/before/after); rename chain folds into one
  symbol history; bounding caps + overflow; deterministic ordering. `validate` stays
  green (read-only query; no new nodes/edges).
- Vertical: `spotlight.py module <area>` on the real AVM module store from the
  enhancement smoke test — confirm `modules/subnet`'s lifecycle (incl. the `outputs.tf`
  fix) reads back as a coherent biography slice.

## Not in scope (v1)
- The structured param extractor / changelog table (v2).
- New gather/network work — v1 is pure read over the existing store.
