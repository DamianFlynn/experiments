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
2. **Link.** Enrich it offline (no network):
   ```bash
   python3 link.py workspace/bundle.json
   ```
   This adds `trains` and classifies all four `buckets` (`shipped`, `rejected`,
   `in_flight`, `next_candidates`).
3. **Render diagrams.** Preflight: `mmdc` must be on PATH
   (install mermaid-cli with `npm install -g @mermaid-js/mermaid-cli`). Then:
   ```bash
   python3 render.py workspace/bundle.json
   ```
   This writes `workspace/diagrams/*.mmd`, records `bundle.diagrams`, and **fails
   if any diagram does not compile** under `mmdc`.
4. **Write the report.** Read `workspace/bundle.json` and fill `report-template.md`,
   embedding each `bundle.diagrams` file as a ```mermaid block. Cite each fact with
   its `url`. Do not state anything the bundle does not contain.

## Rules

- The bundle is the only source of truth. If a fact is not in the bundle, omit it.
- Quote PR/issue numbers and link their `url`.
- Phase 2 reports cover: executive summary, shipped, decision trains, **activity-at-a-glance
  diagrams, releases, CI/CD health, in-flight, rejected/abandoned, and next-up candidates**.
  Sections with no backing data are omitted rather than padded.
