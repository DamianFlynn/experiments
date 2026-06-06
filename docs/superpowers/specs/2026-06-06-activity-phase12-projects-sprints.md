# activity-overview — Phase 12: Projects v2 + sprint framing

**Date:** 2026-06-06
**Status:** shipped — slice 1 (#34, board ingest: status + optional sprints) and slice 2
(this PR: `select_sprints` resolver + board-status / sprint-framing report sections).
**Depends on:** the store's structure-node + edge model; the existing `in_milestone`
sibling (pr/issue → `milestone-<n>` structure node) as the pattern to mirror.

## Goal

Bring **GitHub Projects v2** (the org/user "board") into the graph so the digest can
frame work by **board status** and, when a board defines them, **sprints/iterations** —
not just calendar windows. Real boards are commonly **status-only** (e.g. the verified
target, Azure org #115 "Bicep" — a `Status` single-select, no iteration field), so
status is the universal layer and iterations are the conditional one:
- **Acquire:** a GraphQL fetch of the auto-discovered board → each item's board
  **`status`** (the `Status` single-select), PLUS **sprint (iteration)** structure nodes
  + `in_iteration` edges (pr/issue → sprint) **when the board has an iteration field**.
- **Link:** status surfacing; iteration resolution (prev/current/next sprint by date)
  when sprints exist.
- **Report:** board **status** on the in-flight items (+ a status breakdown); a sprint /
  release-train framing section when the board defines iterations.

The substrate is half-built: `in_iteration` (pr/issue → sprint) is already a reserved
edge type in `validate._EDGE_SCHEMA`, and `bundle["sprints"]` is a reserved key — but
nothing emits them ("needs Projects v2 acquisition"). gather is **REST-only** today; this
phase adds its first **GraphQL** call.

## What's new vs. the milestone pattern

`in_milestone` already works: a PR/issue's `milestone` title → a `milestone-<n>`
structure node + an `in_milestone` edge. Sprints mirror that exactly, with two
differences: the data comes from the **Projects v2 GraphQL API** (not the REST
issue/PR payload), and an iteration carries a **date range** (start + duration) so
"current/previous/next" is resolvable.

## Design

### Source the board (auto-discover)
gather **auto-discovers** the board from the repo it's gathering: a GraphQL query for
`repository(owner, name).projectsV2(first: N)` → the linked Projects v2 board(s). When a
repo links **multiple** boards, ingest only the **primary** (lowest-numbered) one and
warn about + ignore the rest (one board per repo for now — see "Not in scope"); when it
links **none**, the sprint layer is simply empty and every existing output is unchanged. No new required
flag — it rides the existing `--owner/--repo` (and each manifest member). An optional
`--no-project-board` escape hatch skips the query (e.g. when the token lacks
`read:project` scope, so a run never hard-fails on a permissions error — degrade to empty).

### Acquire (gather — first GraphQL call)
- Add a minimal `graphql_post(token, query, variables)` helper (POST `/graphql`,
  Bearer auth, same error-surfacing as `http_get_json`).
- Auto-discover the repo's board(s) (`repository.projectsV2`), then per board query:
  - its **fields** → the `Status` single-select (universal) and, IF present, the
    `ProjectV2IterationField`'s `configuration.iterations[] = {id, title, startDate,
    duration}` (boards without one simply yield no sprints);
  - its **items** (paginated) → each item's content (`Issue`/`PullRequest` `number` +
    `repository.nameWithOwner`) and `fieldValues`: the
    `ProjectV2ItemFieldSingleSelectValue` whose `field.name == "Status"` → status, and
    the `ProjectV2ItemFieldIterationValue` → `{title, iterationId}`.
- Normalize (pure `parse_project_board`) to: `sprints = {sprint_id: {title, start,
  end}}` (empty for status-only boards) and, per item, `(repo, number) → {status,
  sprint_id?}`. Iteration scope is window-bounded by date intersecting `[from, to]`.

### Store (fold)
- **Sprint node:** `sprint-<id>` (`structure`), `ts = start`, `data = {title, start, end}`.
- **Edge:** pr/issue → `sprint-<id>` of type **`in_iteration`** (already schema-allowed).
- **Status:** stamp the board status onto the pr/issue node data (`board_status`), like
  the existing facets — surfaced for the in-flight render.
- Idempotent, omit-when-empty (no board → no sprint nodes/edges → goldens byte-identical).

### `extract` + `link`
- `extract` materializes `sprints` (from sprint structure nodes) + surfaces each
  pr/issue's `in_iteration` (sprint id) + `board_status`, mirroring `in_milestone`.
- `link`: **iteration resolution** — given `ref_date` (meta.to), classify sprints as
  previous / current / next by their date ranges (sibling of `select_milestones`).

### Report (SKILL.md + report-template.md)
- A **Sprints / release-train framing** section: current sprint (its items + status
  split), previous (what shipped), next (committed). Plus **board status** annotations
  on the In-flight section.

## Slices (TDD)
1. **Acquire + store (shipped, #34).** `graphql_post`; board auto-discovery +
   `parse_project_board` (pure, tested from crafted GraphQL fixtures);
   `--project-board`/`--no-project-board`; fold sprint nodes + `in_iteration` edges +
   `board_status`; `extract` surfaces them. Goldens byte-identical; `validate` green.
   Verified live on Azure/bicep #115.
2. **Resolution + report (this slice).** `link.select_sprints` (previous/current/next by
   date, offline-tested — dormant until an iteration board exists); SKILL.md +
   `report-template.md` **board-status** framing (status on in-flight + breakdown,
   universal) and a **Sprints** section (release-train framing, only when the board has
   iterations).

## Testing & verification (read this)
- **Offline TDD:** `parse_project_board` + the fold + resolution are pure and tested from
  crafted GraphQL response fixtures (no network) — the same discipline as the rest.
- **Live verification (status):** gather **`Azure/bicep`** (which links org board #115
  "Bicep") with the `read:project`-scoped token — confirm auto-discovery finds #115, items
  parse, and `board_status` folds + surfaces (#115 is a `Status`-only board, so sprints are
  legitimately empty). Bound the window so the bicep clone/fetch stays tractable.
- **Iteration path:** tested **offline** from a crafted GraphQL fixture that DOES define an
  iteration field (no live iteration board is available yet); live confirmation deferred.
- **Degrade cleanly:** a missing `read:project` scope, no linked board, or a board with no
  iteration field all yield an empty layer (never hard-fail), so the default AVM runs
  (no board) stay byte-identical.

## Not in scope
- Status automation / writing to the board (read-only).
- ~~Ingesting more than ONE board per repo~~ — SUPERSEDED by the 2026-06-06 follow-up
  (`2026-06-06-activity-phase12-all-boards.md`): gather now ingests EVERY maintained
  board a repo links (skipping closed/stale ones) and merges their items, after the
  smoke tests showed the lowest-numbered heuristic picked the wrong board (zero
  coverage) for `bicep-registry-modules`.
