# Phase 3e — symbol-identity tracking (implementation plan)

**Status:** in progress. Branch `claude/activity-phase3e`. Builds on the merged 3d symbol
ledger. Scope (confirmed): **window-wide matching** — link a symbol dropped in one file to the
same symbol added in another, anywhere in the window, with strong guards + confidence. The
highest-risk slice, so precision (no false links) is valued over recall.

## What it adds

Today a symbol moved/extracted between files shows as `drop(old)` + `add(new)` with no link.
3e links them (mirroring the file-level `replaced_by` model) so a relocated symbol reads as one
identity — the decision trail when an idea is broken out into a new module/file.

## Why drop+add (not git rename) is the signal

`git log -p -M` renders a *whole-file* rename as a rename header (often no hunks), so symbols that
didn't change produce no events. The valuable case — a `resource`/`module` **extracted** from
`main.bicep` into a new file — is a *content* move across different files, which appears as a
**drop in A + add in B**. So matching keys on the symbol, not on git's file-rename.

## Matching — pure `match_symbol_moves(symbol_events, rename_pairs)`

Over all window symbol_events (kind `symbol` only; comments excluded — their text identity already
captures evolution and would be noisy):

1. Index drops by `(subkind, name)` → set of source paths; adds likewise → dest paths.
2. **Link only UNIQUE pairings:** a `(subkind, name)` dropped in exactly ONE file `A` and added in
   exactly ONE different file `B` (`A != B`) → a move `A → B`. **Ambiguous names** (dropped or
   added in >1 file — e.g. boilerplate `param location`) are **skipped** (the key false-positive
   guard).
3. **Confidence:** `high` when `(A, B)` is also a git rename/copy pair (from `code_events`
   `-M -C`), else `medium`. Records `basis` = `file_rename` | `unique_name`.

Deferred (not this slice): name-changed renames via body fingerprint (fuzzy, option 2), and
splits (one symbol → many).

## Link wiring — `link_symbol_identity(bundle)` (link.py, after build_artifacts)

For each detected move, on the symbol artifacts: source `status="replaced"`,
`replaced_by=<dest aid>`; dest `identity_from=<source aid>`; both carry `move_confidence` +
`move_basis`. A `symbol_moves` summary (count by confidence) goes on the bundle. Feature_deltas
for the linked add/drop gain `moved_from`/`moved_to` so the report can collapse them into one
"moved" row.

## Validation

- **Offline unit tests:** `match_symbol_moves` — unique move linked; ambiguous name skipped;
  same-file add/drop not a move; confidence high vs medium; comments excluded. Link wiring sets
  replaced_by/identity_from + confidence.
- **Live gate:** assert `symbol_moves` is well-formed; every link's endpoints exist; links carry a
  valid confidence/basis. (Real moves may be rare in a given window — assert shape, not presence.)
- **Spot-check** the bundle against real moves in the window (the established discipline).
- **Docs:** BUNDLE.md, SKILL.md, report-template, spec → rev 12 (3e shipped).
