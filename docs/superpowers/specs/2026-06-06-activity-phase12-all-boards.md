# activity-overview — Phase 12 follow-up: ingest ALL maintained boards

**Date:** 2026-06-06
**Status:** shipped — slice 1 (this PR). Verified live on `bicep-registry-modules`:
0 → 3,063 of its own items now carry board status (merged from #538 + #566; #115 kept
but contributes 0 BRM items), all three boards currently open + fresh so none filtered.
**Depends on:** Phase 12 slice 1 (board ingest) — `parse_project_board` already
merges items across multiple board nodes; only `fetch_project_board` restricted
the fetch to a single (lowest-numbered) board.

## Why

The smoke tests (Bicep / AVM-Terraform / AVM-Bicep) showed the shipped
"lowest-numbered board = primary" heuristic is **truthful but loses coverage**
on multi-board repos. `Azure/bicep-registry-modules` links **3** boards:

| Board | Title | BRM's own items |
|------:|-------|----------------:|
| #115  | Bicep | **0** ← lowest-numbered, what we picked |
| #538  | AVM - Issue Triage | 282 |
| #566  | AVM - Module Issues | **2,782** |

Picking #115 (the Bicep *compiler* board) stamped **zero** board status onto BRM
items, even though 3,064 BRM items live on #538/#566. No *lies* (statuses are
keyed by `(owner/repo, number)`, so a foreign board's entry never attaches to the
gathered repo), but the coverage is wrong.

## Goal

Ingest **every maintained board** the repo links, not just one — and merge their
items (which `parse_project_board` already does safely). Guard against
**deprecated-but-not-removed** boards (a real risk on long-lived orgs) so a stale
board can't pollute or mislead.

## Design

### Discovery (gather — `PROJECT_BOARD_QUERY`)
Add `closed` and `updatedAt` to each `projectsV2` node (they already come back per
board; we just request the two maintenance fields).

### Maintenance filter (pure helper `board_is_maintained(board, ref_date, stale_days)`)
A board is **dropped** (with a one-line `warning:` naming it + the reason) when:
- `closed == true` — the project was archived/closed (the clean "deprecated"
  signal); OR
- `updatedAt` is older than `stale_days` before `ref_date` — abandoned but never
  closed. `stale_days` defaults to `ACTIVITY_BOARD_STALE_DAYS` (env, default
  **365**); a board with no `updatedAt`, or when `ref_date` is None, is **kept**
  (can't prove it stale). Comparison is date-only (`[:10]`), deterministic.

### Fetch (`fetch_project_board(graphql, owner, repo, max_items=5000, ref_date=None)`)
- Discover boards; **keep all maintained ones** (sorted by number for a
  deterministic merge order), instead of only the lowest-numbered.
- For EACH kept board: seed from the discovery page's `items.nodes`, then paginate
  the rest **by node id** (`PROJECT_BOARD_ITEMS_QUERY`, so each cursor belongs to
  that board's connection). Cap **per board** at `max_items` (the env knob
  `ACTIVITY_BOARD_MAX_ITEMS`, default 5000); warn when a board is truncated (so
  lost coverage is visible — truth, and the warning names the env knob to raise).
  A page guard bounds a pathological board. Discovery requests `projectsV2(first:100)`
  (the API max) so all linked boards are seen.
- Pass the LIST of full board nodes to `parse_project_board` (union of iterations;
  per-item `(slug, number)` merge — first non-None status/sprint in board order).
  **Conflict rule:** boards are passed in ascending `number` order, so when the
  same item carries a status on more than one board, the **lowest-numbered board
  wins** (deterministic). Recording which board a status came from is out of scope.
- Degrade cleanly to `({}, {})` on any error, exactly as today. A repo that links
  **no** board still yields the empty layer (AVM-Terraform stays byte-identical).

### Acquire wiring
`acquire` calls `fetch_project_board(..., ref_date=ref_date)` (the window's
`ref_date`/`to`) so the staleness check has a reference point. No new CLI flag;
`--no-project-board` still skips the whole layer.

## Slices (TDD)
1. **All-boards fetch + maintenance filter (this PR).** `board_is_maintained`
   (pure, fixture-tested: open/recent kept; closed dropped; stale dropped;
   missing-updatedAt/None-ref kept); `fetch_project_board` ingests all maintained
   boards (driver tested against a multi-board fixture — confirms items from
   several boards merge, a closed/stale board is skipped, per-board pagination by
   node id, truncation warning). `parse_project_board` unchanged. Goldens
   byte-identical (no board → empty); `validate` unaffected.

## Testing & verification
- **Offline TDD:** the filter + the multi-board driver are pure / fixture-driven
  (no network), same discipline as the rest.
- **Live (the motivating case):** gather `Azure/bicep-registry-modules` and confirm
  its own items now carry `board_status` sourced from #566/#538 (all three boards
  are currently open + freshly updated, so none are filtered), and that the
  per-repo `(slug, number)` keying still attaches nothing foreign.
- **Degrade:** AVM-Terraform (no board) and a synthetic closed/stale board both
  yield the empty/again-filtered layer; goldens stay byte-identical.

## Not in scope
- Per-item board provenance (which board a status came from) — the merge is
  status-first; recording the source board is a later nicety.
- Server-side filtering of board items by repo (GitHub's API can't), so a large
  foreign-heavy board is still fully paged within the cap.
