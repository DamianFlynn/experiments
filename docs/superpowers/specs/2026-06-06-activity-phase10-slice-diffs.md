# activity-overview — Phase 10 enhancement: language-agnostic in-slice file diffs

**Date:** 2026-06-06
**Status:** in progress.
**Depends on:** Phase 10 slices 1–3 (#24–#26) shipped to master.

## Goal

Make the per-train slice carry the **actual code change** for *every* language — not
just graphify-supported ones. Today `feature_deltas` carry `before`/`after` only for
**symbol/comment** artifacts (graphify); `.tf`/`.bicep` *logic* changes land as
file-level deltas with `before/after = None`. The slice-3 narrator therefore can't see
the real fix from the slice alone — it relies on the PR-body prose or the **lead's
`git show`** against the clone (slice-3 #2). That handle requires the **clone on disk
at narration time**, so it doesn't help narrate a *durable, accumulated store* later.

This enhancement stores a **bounded, language-agnostic unified-diff hunk** per changed
file in the gather walk, so the diff rides `feature_deltas → slice_train → narrator` and
the slice is **self-contained and durable** (no clone needed).

## Key insight (low cost)

Two seams already exist:
1. The diff is **already parsed** — `parse_symbol_events` runs the `git log -p` walk and
   calls `parse_unified_diff(body)` → per-file `hunks`; `build_symbol_deltas` consumes
   them for symbols and **discards the file-level hunk**. We retain a **bounded** slice of
   what is already in hand — no second `git` invocation, no new network.
2. The store already has the column. The `code_events` ledger table has a `hunk TEXT`
   column, plumbed through `add_code_event(s)` / `get_code_events` / `repo_code_events`,
   but **never populated or surfaced** today. #4 *populates* that reserved field and
   *surfaces* it — **no schema change**.

## Design

1. **Capture (gather, `parse_symbol_events` path).** For each file `f` in the patch,
   in addition to symbol deltas, build a **bounded unified-diff snippet** from
   `f["hunks"]` — a new pure helper `bounded_file_diff(hunks, cap)` that emits the
   `+`/`-`/context lines (with `@@` markers) up to `FILE_DIFF_CAP` chars/lines, then a
   `…[+N lines]` marker. Produce a `{(commit, path): diff}` lookup, surfaced as a new
   bundle field or merged onto `code_events[].diff` in `acquire` (keyed by `(commit,
   path)`). **Omit when empty** (merge commits / no patch / binary).
   The fold writes it to the existing `hunk` column for the file artifact's lifecycle
   row (no schema change); `repo_code_events`/`extract` already return `hunk`.
2. **Carry through artifacts (derive.build_artifacts).** When a file artifact's
   lifecycle event is built from a `code_event`, attach its `hunk`/`diff`
   (omit-when-empty) so the diff lives on the **stored** artifact — durable in the graph.
3. **Surface (derive.compute_feature_deltas).** Emit `diff` on the file-level delta
   (omit-when-empty). Symbol deltas keep their existing `before`/`after`.
4. **Slice (link.slice_train).** `feature_deltas` already ride the slice; the `diff`
   comes along. Add a **per-train cap** (`SLICE_DIFF_CAP` total chars, with an overflow
   marker) so one churny train can't blow the slice budget.
5. **Narrator (SKILL.md).** The in-slice `feature_deltas[].diff` becomes the narrator's
   first-class "what changed" source for non-graphify languages; the lead `git show`
   handle (slice-3 #2) stays as the **deeper fallback** when the bounded diff is
   truncated.

## Bounding (must)

- Per-file: `FILE_DIFF_CAP` (~800 chars / ~30 lines), with `…[+N lines]`.
- Per-train in the slice: `SLICE_DIFF_CAP` total, with overflow count — diffs are the
  largest text in the slice, so this cap protects the sub-agent's context budget.

## Byte-stability & validate

- **No golden regen.** `fixtures/bundle_p3*.json` `code_events` carry `{author, change,
  commit, date, path}` and no patch; the new `diff` is **omit-when-empty**, so synthetic
  goldens (no patch) stay byte-identical and the `fold → extract → enrich`
  characterization gate is untouched.
- **`no_drift`:** `build_artifacts` re-derives artifacts (incl. the attached `diff`)
  deterministically from `code_events`, so re-fold reproduces the store — the standing
  audit holds.
- **Round-trip:** the `diff` lives in the file artifact's lifecycle blob, which the store
  persists and `extract` reads back verbatim (same path as the existing symbol
  `before`/`after`). A round-trip test pins this.
- No new node/edge types; provenance/schema unaffected.

## Slices (TDD)

1. **Capture + store + surface.** `bounded_file_diff` helper; attach `diff` to
   `code_events`; `build_artifacts` carries it onto the file artifact lifecycle;
   `compute_feature_deltas` emits it; extract round-trips it. Offline tests from a
   crafted patch fixture; goldens stay byte-identical; `validate` green.
2. **Slice cap + narrator.** `slice_train` caps the per-train diff total (overflow
   marker); SKILL.md "Phase 4b" uses in-slice `feature_deltas[].diff` as the primary
   "what changed" for non-graphify langs, with the lead `git show` as the fallback.

## Testing
- Unit: `bounded_file_diff` (cap + `@@`/sign lines + overflow marker); `code_events`
  gain `diff` only when a patch exists; `build_artifacts`/`compute_feature_deltas` carry
  it; extract round-trip; `slice_train` per-train cap.
- Vertical: re-gather a real `.tf` repo window (e.g. the AVM module used in the slice-3
  smoke test) and confirm `feature_deltas[].diff` carries the real logic change that was
  previously absent — without a clone at narration time.

## Not in scope
- Full-file or multi-hunk un-bounded diffs (the cap is deliberate — the slice is a
  bounded context unit, not a patch archive).
