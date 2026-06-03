# Phase 3d — symbol-granular artifacts (implementation plan)

**Status:** in progress. Branch `claude/activity-phase3d`. Builds on the merged 3c/3c.1/3c.2
edge+area foundation. Scope (confirmed): **everything in one slice** — Bicep + Terraform symbols,
graphify-node symbols (best-effort, optional dep), and language-agnostic `comment` changes — with
**bounded before/after** snippets in `feature_deltas`.

## Goal

Push the artifact ledger from FILE granularity (Phase 3a: readme/doc/example) to **symbol**
granularity. `artifacts[].kind` gains `symbol` and `comment`; `feature_deltas` fill the reserved
`before`/`after`/`detail` (currently null) with a size-bounded snippet of what changed.

## Approach — single hunk walk, diff-local attribution

The window is bounded but can hold thousands of (commit, file) pairs, so per-commit `git show`
is too many subprocesses. Instead:

1. **gather: one `git log -p --unified=3` walk** over the window → per-(commit, path) **hunks**.
   Parsed by a pure `parse_unified_diff(patch)` into `{old_start, new_start, lines:[(sign,text)]}`.
2. **Diff-local symbol detection.** Per language, a pure `symbol_decls(line)` recogniser matches a
   declaration in a single diff line:
   - **Bicep:** `param`/`var`/`output`/`resource`/`module <name>` (+ `@description`/`//`/`/* */`
     → `comment`).
   - **Terraform:** `resource "<type>" "<name>"`, `variable`/`output`/`module "<name>"`, `# `/`//`
     → `comment`.
   - **graphify (optional):** when `graphify` is on PATH and supports the file's language, map its
     node spans onto hunk new-line ranges; absent it, generic source files contribute only
     `comment` deltas (language-agnostic) — best-effort, never required.
3. **Classify per hunk** (`build_symbol_deltas`, pure): a declaration on a `+` line ⇒ symbol
   **add**; on a `-` line ⇒ **drop**; a changed hunk whose nearest enclosing declaration is on a
   context/added line ⇒ symbol **change**. `before` = first removed decl/line, `after` = first
   added decl/line, each capped (≤3 lines / ≤200 chars). `detail` = `"<lang> <kind> <name>"`.

## Ledger wiring

- **gather** emits `symbol_events` alongside `code_events` (commit, path, change, symbol kind/name,
  before/after) for tracked source files (Bicep/TF/graphify-langs). File-level events unchanged.
- **link.build_artifacts**: fold `symbol_events` into `kind:"symbol"`/`"comment"` artifact entries
  (id = `path#<lang>:<kind>:<name>`), with the same lifecycle/status machinery + `code_area`
  attribution via `code_graph`.
- **link.compute_feature_deltas**: emit symbol deltas with the bounded `before`/`after`/`detail`.
- `classify_artifact_path` stays file-level; symbol kinds come from the symbol path, not the path
  classifier (its docstring's "deferred" note is now realised here).

## Validation

- **Offline unit tests** (pure, fixture-driven): `parse_unified_diff`; Bicep/TF `symbol_decls`;
  `build_symbol_deltas` (add/drop/change + comment + bounded snippet); link folding of
  `symbol_events` into artifacts + feature_deltas. New fixtures: a Bicep diff, a TF diff.
- **Live integration gate**: assert the bundle now carries `kind:"symbol"` artifacts and
  `feature_deltas` with non-null `before`/`after` on a busy Bicep window (graphify absent ⇒ the
  gate validates the Bicep symbol path; graphify-node symbols are exercised only where present).
- **Docs**: BUNDLE.md (symbol/comment kinds + before/after/detail), SKILL.md, report-template
  (a symbol-level "what changed" subsection), spec → rev 11 (3d shipped).

## Deferred to 3e

Symbol-identity tracking across renames/moves/file-splits (fingerprint + cross-diff matching).
3d attributes within a commit's diff; it does not yet follow a symbol's identity across a rename.
