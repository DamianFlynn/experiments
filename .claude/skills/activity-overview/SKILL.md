---
name: activity-overview
description: Generate a verifiable, fully-sourced repository activity digest for a date window. Clones the repo (bounded), pulls merged PRs + closing issues + commits + Projects v2 board, folds them into a persistent SQLite journey-graph, and renders a sourced Markdown report (or hands the structured view to other formatters).
when_to_use: Use when asked to summarize/report what shipped in a repo (or a multi-repo project) over a period — "activity digest", "what changed", "release notes", "monthly/sprint summary", "contributor/flow report", or to build the structured fact base other formatters consume. Works on single repos and multi-repo manifests (Bicep & Terraform/AVM).
---

# Activity Overview

Produce a **fact-based activity digest** for `OWNER/REPO` (or a multi-repo project)
over `[FROM, TO]`. Every claim in the report resolves to a source ref — **never invent
facts.** The pipeline is deterministic and emits a verifiable structured view; the
narrative is the model's judgment grounded in that view (and is the skill's job, not the
pipeline's).

> **Store-native.** `gather` folds facts directly into a persistent SQLite
> **journey-graph store** — that graph is the deliverable and it **accretes over time**
> (idempotent fold; re-running over an overlapping window never double-counts, so the
> store grows as you gather more windows/repos). The flat bundle is a **transient view**
> the reader stage (`extract`) materializes from the store; `link`/`render` consume it,
> guarded by the golden-bundle equivalence gate. Validate any store with
> `python3 validate.py workspace/journey.db` (self-contained — no bundle file).

All scripts referenced below ship inside this skill directory
(`${CLAUDE_SKILL_DIR}`); run them with `python3` from the skill dir (stdlib only).
For deeper detail: **`reference/report-sections.md`** (how to author each report
section), **`BUNDLE.md`** (the structured view / output contract for downstream
formatters), **`STORE.md`** (the graph schema), **`REFERENCE.md`** (install, env knobs,
the `/activity` command, transcript flow), **`samples/`** (a worked end-to-end
example — the structured view + a rendered digest), and **`examples/`** (input
examples: a transcript, manifest, series index, plus a second formatter consuming
the structured view).

## Procedure

1. **Acquire → store.** Run the gather CLI (requires `GITHUB_TOKEN` with `repo` scope and
   `git` on PATH). gather auto-discovers and ingests the repo's Projects v2 board(s) when
   the token also has `read:project`; without that scope (or with `--no-project-board`) the
   board layer cleanly degrades to empty and the rest of the run is unaffected. The
   **store is the sole output** — gather writes no bundle file:
   ```bash
   python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --store workspace/journey.db
   ```
   This folds a schema-complete graph into the SQLite substrate (see `STORE.md`); the fold is
   idempotent, so re-running over an overlapping window never double-counts.
   - **IaC dependency edges:** if `bicep` and/or `terraform` are on `PATH`, gather
     resolves inter-area dependency edges (build-only) into `code_graph.areas[].edges` and records
     `code_graph.edge_extraction` (`resolved`/`timeout`/`failed`/`skipped`); absent the CLIs (or
     the module registry), edges are left empty and the rest of the run is unaffected.
   - **Roll-up / resume are store-native.** A long view is a wider `range_query` over the store
     (no separate roll-up artifact); a refresh re-folds the same window against the pinned
     `meta.clone_sha` (idempotent dedup keeps it overlap-safe). The flat-bundle `--rollup` /
     `--resume` / `--out` flags were retired with the bundle file.
   - **Validate the store (trust gate).** Audit the graph for trustworthiness; this is the
     store's acceptance check and is fully self-contained on a store:
     ```bash
     python3 validate.py workspace/journey.db
     ```
     `no_drift` / `idempotency` self-source their raw bundle from the store via `extract`; an
     external `--bundle` is an optional cross-check, never required.
   - **Multi-repo project.** To fold several repos under one logical
     project (e.g. an Azure Verified Modules — Terraform constellation, where each
     module is its own repo), pass a **manifest** instead of `--owner/--repo`:
     ```bash
     python3 gather.py --manifest workspace/manifest.json --store workspace/journey.db
     ```
     The manifest is `{project, window:{from,to}, repos:[{owner, repo, registry?}]}`
     (`registry` optional — an exact Terraform registry path; absent → resolved by
     the HashiCorp naming convention).
     - **Generate/refresh the manifest from the AVM index first** (recommended), so
       it tracks the current module set. The published AVM index is the source of
       truth for which member repos and registry paths exist, so regenerating before
       a gather avoids drift as modules are added, renamed, or promoted — don't rely
       on a stale hand-curated list:
       ```bash
       python3 manifest_from_index.py --avm res --avm ptn \
         --project avm-tf-aiml-lz --from FROM --to TO \
         --name-contains aiml-landing-zone --status Available > workspace/manifest.json
       ```
       `--avm res|ptn|utl` pulls the canonical AVM index CSV(s) (or `--index FILE|URL|-`
       for a local/pinned copy); filter with `--kind`/`--status`/`--name-contains`/
       `--include`/`--exclude`/`--limit`. The output is exactly the contract
       `gather --manifest` folds; hand-authoring is the fallback for a fixed set.

     gather folds each
     member under `project=<manifest.project>`, `repo="owner/repo"`. Cross-repo
     decision-train edges (qualified `owner/repo#N` refs + repo-aware timeline
     cross-refs) and cross-repo Terraform `depends_on` (registry source → the member
     that publishes it) form automatically; `examples/`/`tests/` Terraform subtrees
     are treated as scaffolding (not module dependencies). Validate the whole project with
     `python3 validate.py workspace/journey.db --project <name>` (adds a
     project-wide people check). A project digest view (cross-repo trains, merged
     Shipped/ownership, `related_work` ticket clusters, the project module-dependency
     graph) comes from `python3 digest.py --store workspace/journey.db --project <name>
     --from FROM --to TO`; the blast radius for one member is `python3 spotlight.py
     dependents <owner/repo> --store workspace/journey.db --project <name>`.
     - **Heavy live runs.** A real AVM pattern module's `terraform init` pulls dozens
       of registry modules; set a shared `TF_PLUGIN_CACHE_DIR` (gather warms it once,
       then extracts in parallel) and, if needed, raise `ACTIVITY_IAC_BUILD_TIMEOUT`
       / tune `ACTIVITY_IAC_MAX_WORKERS` (defaults 300s / 8).
     - **Boundary-dropped commits.** If a run warns that in-window commit(s) sit at
       the shallow-clone boundary (their whole-tree phantom diffs were dropped — see
       `meta.boundary_dropped_commits`), widen `ACTIVITY_CLONE_MARGIN_DAYS` (default
       `14`) and re-gather so each in-window commit keeps its real parent.
2. **Materialize + Link.** The reader stage rebuilds the bundle view from the store
   (`extract`) and enriches it offline (no network):
   ```bash
   python3 link.py <bundle-view>
   ```
   This adds `trains` and classifies all four `buckets` (`shipped`, `rejected`,
   `in_flight`, `next_candidates`).

   **Series continuity (optional).** For a recurring digest, pass an append-only
   index so this installment is framed against the last one:
   ```bash
   python3 link.py <bundle-view> --series series.json
   ```
   This adds `bundle["series"]` (`new` / `carried_over` / `forecast_loop`) and
   appends this installment's snapshot to `series.json`. The file is a thin
   convenience index over the store — never an override; delete it to start a
   fresh series (the next run is then a "first installment"). Without `--series`
   the digest is byte-identical to before.
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
4. **Community-call transcript (optional).** If the user provides a
   community-call transcript (a local file passed explicitly, or a `transcript`
   path in `projects.json`) — no network, user-provided only — normalize it to
   clean prose first:
   ```bash
   python3 transcript.py path/to/call.vtt
   ```
   `transcript.py` strips WebVTT/SRT structure (headers, NOTE/STYLE blocks, cue
   timings, SRT indices, inline tags) and collapses rolling-caption duplicates;
   plain `.txt`/`.md` passes through. Read its stdout and author the **Community
   call highlights** section grounded ONLY in that text (see the report rules
   below). No transcript ⇒ skip this step and omit the section.
5. **Write the report.** Read the materialized bundle view and fill
   `report-template.md`, embedding each `bundle.diagrams` file as a fenced mermaid block.
   Cite each fact with its `url`. Do not state anything the view does not contain.

## Report rules

- **Source of truth:** the store/view. If a fact is not in it, omit it — never invent.
  Quote PR/issue numbers and link their `url`; every narrative-bearing claim cites a ref.
- **Deterministic data, authored prose:** the pipeline emits the structured facts
  (`buckets`, `trains`, `feature_deltas`, `people`/`halls`, `flow`/`blockers`, `forecast`,
  `series`, `sprints`, `diagrams`, …); the narrative is your judgment over them. Narrator
  passes (per-train, module biography, community call) read ONLY a bounded slice and cite
  it verbatim — the lead drops any ref not present in the slice.
- **Omit-when-empty:** a section with no backing data is dropped, not padded.
- **Internal attribution is gated:** the public digest never attributes a stall/blocker to
  a person; the internal appendix is opt-in only.

**How to fill each section** of `report-template.md` — executive summary, since-last-
installment, community-call highlights, shipped (+ by area), decision trains (+ the
per-train narrator contract), forecast, board/sprints, feature changes, content lifecycle,
module graph, ownership, issue kinds, contributors, stalled/blocked, module biography, the
gated appendix, releases/CI/in-flight/rejected — is in **`reference/report-sections.md`**.
Read it when authoring the report.

## On-demand readers (other dimensions)

The window digest is one lens. The same persistent store answers other questions — read the
relevant slice and narrate it grounded:

- **Module biography** (how one module evolved across ALL history): `python3 spotlight.py
  module <area> --store <db> --project <name> --json` → narrate per `reference/report-sections.md`.
- **Blast radius / dependents** of a module: `python3 spotlight.py dependents <owner/repo>
  --store <db> --project <name>`.
- **One train's slice** (for narration or a spotlight): `python3 link.py <view> --slice <id>`;
  `python3 render.py --train <id>` for its flowchart.
- **Multi-repo project view** (the structured fact base other formatters consume): `python3
  digest.py --store <db> --project <name> --from FROM --to TO` — see `BUNDLE.md` for the
  contract.
