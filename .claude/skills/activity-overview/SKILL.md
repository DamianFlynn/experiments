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
   This adds `trains` and `buckets.shipped`.
3. **Render.** Read `workspace/bundle.json` and fill `report-template.md`. Cite each
   fact with its `url`. Do not state anything the bundle does not contain.

## Rules

- The bundle is the only source of truth. If a fact is not in the bundle, omit it.
- Quote PR/issue numbers and link their `url`.
- Phase 1 reports cover: executive summary, shipped this period, and decision trains.
  Other sections arrive in later phases — leave them out rather than padding.
