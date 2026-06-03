---
name: activity-overview
description: Generate a verifiable repository activity digest for a date window — clones the repo (bounded), pulls merged PRs and their closing issues, links them into decision trains, and renders a sourced Markdown report. Use when asked to summarize what shipped in a repo over a period.
---

# Activity Overview

Produce a **fact-based activity digest** for `OWNER/REPO` over `[FROM, TO]`. Every
claim in the report resolves to a source ref in the bundle — never invent facts.

## Procedure

1. **Acquire.** Run the gather CLI (requires `GITHUB_TOKEN` with `repo` scope and `git`
   on PATH; `read:project` is only needed once later phases enable Projects v2):
   ```bash
   python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --out workspace/bundle.json
   ```
   This writes a schema-complete bundle (see `BUNDLE.md`).
   - **IaC dependency edges (Phase 3c):** if `bicep` and/or `terraform` are on `PATH`, gather
     resolves inter-area dependency edges (build-only) into `code_graph.areas[].edges` and records
     `code_graph.edge_extraction` (`resolved`/`timeout`/`failed`/`skipped`); absent the CLIs (or
     the module registry), edges are left empty and the rest of the run is unaffected.

   **Alternative acquire modes (Phase 3c.2):**
   - **Resume a partial edge gap.** If a prior run left areas `timeout`/`failed` (see
     `edge_extraction`), re-resolve *only those* against the bundle's pinned `meta.clone_sha`
     (same source tree) — no full re-gather:
     ```bash
     python3 gather.py --resume workspace/bundle.json --out workspace/bundle.json
     ```
     Then continue at **Link** (re-run link + render so the diagrams pick up the new edges).
   - **Roll up a long view.** Merge monthly installments (overlap-safe — union by stable
     identity, structure from the latest) into one multi-period bundle:
     ```bash
     python3 gather.py --rollup apr.json may.json jun.json --out half.json
     ```
     Roll-up emits a *raw* bundle (derived fields dropped), so continue at **Link** then Render.
     (A fresh wide-window re-gather always yields a correct bundle and stays canonical.)
2. **Link.** Enrich it offline (no network):
   ```bash
   python3 link.py workspace/bundle.json
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
   python3 render.py workspace/bundle.json
   ```
   This writes `workspace/diagrams/*.mmd`, records `bundle.diagrams`, and **fails
   if any diagram does not compile** under `mmdc`.
   The manifest now also includes `content_timeline`, `deltas_bar`,
   `contributor_graph`, and `kind_breakdown`.
4. **Write the report.** Read `workspace/bundle.json` and fill `report-template.md`,
   embedding each `bundle.diagrams` file as a ```mermaid block. Cite each fact with
   its `url`. Do not state anything the bundle does not contain.

## Rules

- The bundle is the only source of truth. If a fact is not in the bundle, omit it.
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
