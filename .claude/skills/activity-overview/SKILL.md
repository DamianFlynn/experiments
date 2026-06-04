---
name: activity-overview
description: Generate a verifiable repository activity digest for a date window — clones the repo (bounded), pulls merged PRs and their closing issues, links them into decision trains, and renders a sourced Markdown report. Use when asked to summarize what shipped in a repo over a period.
---

# Activity Overview

Produce a **fact-based activity digest** for `OWNER/REPO` over `[FROM, TO]`. Every
claim in the report resolves to a source ref — never invent facts.

> **Phase 7 status (store-only).** `gather` now folds facts directly into the
> SQLite **journey-graph store** — that trustworthy graph is the deliverable. The
> flat bundle JSON is no longer a produced artifact; it is a **transient view**
> the reader stage materializes from the store via `extract`. The report vertical
> (`extract → link → render → report`) **composes from the store** today — each
> stage reads the materialized view, guarded by the golden-bundle equivalence gate
> (steps 2–4 below). A single one-shot reader command is not yet wired (compose the
> stages); that is a minor integration nicety, decoupled from the phase roadmap
> (Phase 8 is `spotlight`, the analytics reader). Validate the store with
> `python3 validate.py workspace/journey.db` (self-contained — no bundle file).

## Procedure

1. **Acquire → store.** Run the gather CLI (requires `GITHUB_TOKEN` with `repo` scope and
   `git` on PATH; `read:project` is only needed once later phases enable Projects v2). The
   **store is the sole output** — gather writes no bundle file:
   ```bash
   python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --store workspace/journey.db
   ```
   This folds a schema-complete graph into the SQLite substrate (see `STORE.md`); the fold is
   idempotent, so re-running over an overlapping window never double-counts.
   - **IaC dependency edges (Phase 3c):** if `bicep` and/or `terraform` are on `PATH`, gather
     resolves inter-area dependency edges (build-only) into `code_graph.areas[].edges` and records
     `code_graph.edge_extraction` (`resolved`/`timeout`/`failed`/`skipped`); absent the CLIs (or
     the module registry), edges are left empty and the rest of the run is unaffected.
   - **Roll-up / resume are store-native.** A long view is a wider `range_query` over the store
     (no separate roll-up artifact); a refresh re-folds the same window against the pinned
     `meta.clone_sha` (idempotent dedup keeps it overlap-safe). The flat-bundle `--rollup` /
     `--resume` / `--out` flags were retired with the bundle file.
   - **Validate the store (trust gate).** Audit the graph for trustworthiness; this is the
     Phase 7 deliverable's acceptance check and is fully self-contained on a store:
     ```bash
     python3 validate.py workspace/journey.db
     ```
     `no_drift` / `idempotency` self-source their raw bundle from the store via `extract`; an
     external `--bundle` is an optional cross-check, never required.
2. **Materialize + Link.** The reader stage rebuilds the bundle view from the store
   (`extract`) and enriches it offline (no network):
   ```bash
   python3 link.py <bundle-view>
   ```
   This adds `trains` and classifies all four `buckets` (`shipped`, `rejected`,
   `in_flight`, `next_candidates`).
3. **Render diagrams.** Preflight: `mmdc` must be on PATH
   (install mermaid-cli with `npm install -g @mermaid-js/mermaid-cli`).
   `graphify` is **optional** (used only for its supported languages); when it is
   absent — e.g. on Bicep/Terraform repos — the **directory provider** supplies
   code areas, so no install is required for code-area attribution to work.

   Then:
   ```bash
   python3 render.py <bundle-view>
   ```
   This writes `workspace/diagrams/*.mmd`, records `bundle.diagrams`, and **fails
   if any diagram does not compile** under `mmdc`.
   The manifest now also includes `content_timeline`, `deltas_bar`,
   `contributor_graph`, and `kind_breakdown`.
4. **Write the report.** Read the materialized bundle view and fill
   `report-template.md`, embedding each `bundle.diagrams` file as a ```mermaid block.
   Cite each fact with its `url`. Do not state anything the view does not contain.

## Rules

- The **store** is the only source of truth; the bundle view is materialized from it.
  If a fact is not in the store/view, omit it.
- Quote PR/issue numbers and link their `url`.
- Phase 2 reports cover: executive summary, shipped, decision trains, **activity-at-a-glance
  diagrams, releases, CI/CD health, in-flight, rejected/abandoned, and next-up candidates**.
  Sections with no backing data are omitted rather than padded.
- Phase 3a reports additionally cover: **Feature changes (add/drop/change)** and
  **Content lifecycle (built/changed/dropped)**, embedding `diagrams.deltas_bar`
  and `diagrams.content_timeline` and citing `feature_deltas`/`artifacts` refs.
  PR/issue **comment and review-comment bodies** and issue **reactions** are now
  in the bundle for narrative grounding. Sections with no backing data are omitted.
- Phase 3b reports additionally: **group Shipped by code area**, add **Module
  ownership** (`code_owners` + `people.modules`/`modules`, embedding
  `diagrams.contributor_graph`) and an **Issue kinds** breakdown (embedding
  `diagrams.kind_breakdown`). Each issue/PR carries `facets`
  (area/priority/status/lifecycle) and each issue a `kind`; group and label using
  them. `code_area`/`area` are now populated where a path resolves to an area.
- Phase 3c reports additionally: a **Module dependency graph / blast radius** section
  embedding `diagrams.module_graph` from `code_graph.areas[].edges`.
- Phase 3d reports additionally: a **Symbol-level changes** subsection under Feature
  changes — `feature_deltas` with `subject` `symbol`/`comment` carry a bounded
  `before`/`after` + `detail` (`<lang> <subkind> <name>`); show the actual change text.
- Phase 3e: when `symbol_moves.links` is non-empty, collapse the matching add/drop
  deltas into a single **"moved"** row (`from_path → to_path`), labelling `confidence`;
  `medium`-confidence moves are heuristic, so present them as likely-not-certain.
- Phase 4a **Decision trains** — each train in `bundle["trains"]` now carries `significance`,
  `tier` (`"deep"` or `"mention"`), and an `effort` block. Structure the section as follows:
  - **DEEP trains** (`tier == "deep"`) get a full sub-section. Embed the train's Mermaid
    flowchart from `diagrams.train_flowcharts[id]` (a file path; read and inline as a
    ```mermaid block). Add an **effort line** derived from the `effort` block:
    - Merged: *"landed in N days · R reviewers · P contributors"*
      (`elapsed_days`, `reviewers`, `participants`; note `stalled: true` when present).
    - Open: *"open N days"* (compute from `effort.opened_at` to today; merged_at is null).
    The per-train narrative is authored in Phase 4b (sub-agent pass) — for now leave a
    `<!-- narrative: <train-id> -->` placeholder after the effort line.
  - **MENTION trains** (`tier == "mention"`) collapse to a single line:
    `- train-id — {title} — {outcome} ({PR count} PR(s))`.
  - `slice_train(bundle, train_id)` is the bounded, self-contained unit that a Phase 4b
    sub-agent (or a spotlight) consumes for one train. Call it to scope the context passed
    to a sub-agent. `render.py --train <id>` produces a spotlight flowchart on demand for
    any tier.
- Phase 4a **Next-release forecast** — `bundle["forecast"]` contains a forward-only
  prediction over `buckets.next_candidates`. Render a **Next-release forecast** section:
  - Header: *"Next milestone: {forecast.next_milestone}"* (or "none identified").
  - Group candidates by tier in order: **Likely** (score ≥ 5.0) → **Possible** (≥ 2.0) →
    **Longshot** (< 2.0). Within each group, list in score-descending order.
  - Per candidate: the item title + link (from `ref.url`), its `train` id if set, and the
    `signals` list (e.g. "on milestone v1.2 · high-priority · open PR").
  - Note: this is a forward-only forecast. The predicted-vs-landed loop (Phase 7) is not
    yet present — do not describe it as such.
