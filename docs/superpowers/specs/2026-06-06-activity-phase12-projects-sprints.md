# activity-overview — Phase 12: Projects v2 + sprint framing

**Date:** 2026-06-06
**Status:** in progress (spec / scoping).
**Depends on:** the store's structure-node + edge model; the existing `in_milestone`
sibling (pr/issue → `milestone-<n>` structure node) as the pattern to mirror.

## Goal

Bring **GitHub Projects v2** (the org/user "board") into the graph so the digest can
frame work in **sprints/iterations**, not just calendar windows:
- **Acquire:** a GraphQL fetch of the project board → **sprint (iteration)** structure
  nodes + `in_iteration` edges (pr/issue → sprint) + each item's board **status**.
- **Link:** iteration/status resolution — previous / current / next sprint by date.
- **Report:** a sprint / release-train framing section, and board **status** on the
  in-flight items.

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

### Source the board
A new gather input names the board (Projects v2 is an **org/user-level** entity, not a
repo one): `--project-board <owner>/<number>` (owner = org or user login; number = the
project's board number). Optional — when absent, the sprint layer is simply empty
(every existing output unchanged). For a manifest run, the board is named once at the
manifest/project level. **Decision to confirm:** explicit flag vs. auto-discovering the
repo's linked project(s) via `repository.projectsV2`.

### Acquire (gather — first GraphQL call)
- Add a minimal `graphql_post(token, query, variables)` helper (POST `/graphql`,
  same auth/error-surfacing as `http_get_json`).
- Query the board's **iteration field** (its `configuration.iterations[]` →
  `{id, title, startDate, duration}`) and the board **items** → each item's content
  (issue/PR `number` + repo) + its **iteration** field value + **status** field value.
- Normalize to: `sprints = {sprint_id: {title, start, end}}` and, per item,
  `(repo, number) → {sprint_id, status}`. Scope: window-bounded by the iteration dates
  intersecting `[from, to]` (so a multi-year board doesn't flood the slice).

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
1. **Acquire + store.** `graphql_post`; the board query + `parse_project_board` (pure,
   tested from a crafted GraphQL response fixture); `--project-board` flag; fold sprint
   nodes + `in_iteration` edges + `board_status`; `extract` surfaces them. Offline tests;
   omit-when-empty keeps goldens byte-identical; `validate` green.
2. **Resolution + report.** `select_sprints` (previous/current/next by date); SKILL.md +
   `report-template.md` sprint-framing section + in-flight board status.

## Testing & verification (read this)
- **Offline TDD:** `parse_project_board` + the fold + resolution are pure and tested from
  crafted GraphQL response fixtures (no network) — the same discipline as the rest.
- **Live verification is conditional:** Projects v2 requires a token with `read:project`
  scope **and** an actual board on the target org/user. If neither is available (e.g. the
  AVM repos expose no Projects v2 board / the PAT lacks `project` scope), slice 1 ships
  **offline-verified only**, and live vertical confirmation is deferred until a board is
  available. This is called out so the "verified on real data" bar is set honestly.

## Not in scope
- Status automation / writing to the board (read-only).
- Multiple boards per project (one board per project for now).
