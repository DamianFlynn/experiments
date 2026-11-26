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
> the reader stage (`extract`, CLI `view.py`) materializes from the store; `link`/`render` consume it,
> guarded by the golden-bundle equivalence gate. Validate any store with
> `python3 validate.py workspace/journey.db` (self-contained — no bundle file).

All scripts referenced below ship inside this skill directory
(`${CLAUDE_SKILL_DIR}`); run them with `python3` from the skill dir (stdlib only).
For deeper detail: **`docs/HLD.md`** (high-level design + usage scenarios), **`docs/LLD.md`**
(L400 low-level design), **`docs/USAGE.md`** (operator runbook: monthly series, forecasting,
per-estate playbooks), **`ARCHITECTURE.md`** (the L400 deep dive — every tool's parameters
+ internals, the C4/data-model diagrams, end-to-end scenarios, and a diagnostics
matrix), **`reference/report-sections.md`** (how to author each report
section), **`BUNDLE.md`** (the structured view / output contract for downstream
formatters), **`STORE.md`** (the graph schema), **`REFERENCE.md`** (install, env knobs,
the `/activity` command, transcript flow), **`samples/`** (a worked end-to-end
example — the structured view + a rendered digest), and **`examples/`** (input
examples: a transcript, manifest, series index, plus a second formatter consuming
the structured view).

## Procedure

Every step below is one command with explicit input and output paths. Run them from
the skill directory; `workspace/` is this run's working directory (any path works, as
long as each step reads what the previous one wrote). Use a **new store per
installment** (`docs/USAGE.md` §2.1 explains when one store that grows month by month also
works: views are window-scoped, but a past month re-viewed later shows today's record
state).

**Two artifacts feed the report:**

| Artifact | Made by | Holds | Used for |
|---|---|---|---|
| **Bundle view** — one repo, one window (`workspace/view/bundle.json`) | `view.py`, then enriched in place by `link.py`; `render.py` adds `diagrams` | raw facts + `trains`, `buckets`, `forecast`, `flow`, `people`/`halls`, … + `series` (with `--series`) + `diagrams` | every section of a single-repo report; the per-member sections of a multi-repo report |
| **Project view** — multi-repo only (`workspace/view/project.json`) | `digest.py` (stdout) | merged `trains`, `shipped`, `people` / `halls` (per-person totals summed across members), `modules`, `related_work`, `module_edges`, and `members[i].bundle` | the project-wide sections of a multi-repo report |

`related_work` (ticket-linked trains) is **opt-in**: it stays `[]` unless you pass
`digest.py --manifest M` with `"ticket_prefixes": ["ABC", …]` declared in the
manifest, or `--ticket-pattern RE`. In `members[i].bundle.meta`, `owner` is the
project scope — use the added `slug` (`owner/name`), `repo_owner`, `repo_name` for
GitHub links (BUNDLE.md). Unknown project/repo or a bad window → exit 2.

Store scope: a single-repo gather stores **project = OWNER, repo = REPO**; a manifest
gather stores **project = the manifest's `project`, repo = `owner/repo`** per member.

1. **Gather → store.** Run the gather CLI (requires `GITHUB_TOKEN` with `repo` scope and
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
     the module registry), those build edges are left empty and the rest of the run is
     unaffected. Add **`--structural-iac`** to also scan the whole tracked tree statically (no
     CLI needed) for every Terraform module dir's and Bicep `<area>/main.bicep`'s direct
     module references, so modules nobody touched in the window keep their edges (manifest
     members always get this scan); use it on a monorepo such as
     `Azure/bicep-registry-modules`.
   - **Roll-up / refresh are store-native.** A long view is a wider window over a store
     that holds its months (no separate roll-up artifact; its CI statistics are the sum of
     the gathered windows inside it, its structure the latest one's — `meta.facts_windows`).
     A refresh re-gathers the same window into the same store: the fold is idempotent, and
     the clone is fetched and checked out at the default branch's current head
     (`meta.clone_sha` records which tree; it is not re-used). gather writes no bundle file
     (there is no `--out`); its only `--resume` is the manifest one below.
   - **Multi-repo project.** To fold several repos under one logical
     project (e.g. an Azure Verified Modules — Terraform constellation, where each
     module is its own repo), pass a **manifest** instead of `--owner/--repo/--from/--to`
     (the window comes from the manifest):
     ```bash
     python3 gather.py --manifest workspace/manifest.json --store workspace/journey.db
     ```
     The manifest is `{project, window:{from,to}, repos:[{owner, repo, registry?, branch?}]}`
     (`registry` optional — an exact Terraform registry path; absent → resolved by
     the HashiCorp naming convention; `branch` optional — the member's mainline when
     it is not the repo's default branch, which gather otherwise detects per member).
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

     gather folds each member under `project=<manifest.project>`, `repo="owner/repo"`.
     Cross-repo decision-train edges (qualified `owner/repo#N` refs + repo-aware timeline
     cross-refs) and cross-repo Terraform `depends_on` (registry source → the member
     that publishes it) form automatically; `examples/`/`tests/` Terraform subtrees
     are treated as scaffolding (not module dependencies).
     - **Heavy live runs.** A real AVM pattern module's `terraform init` pulls dozens
       of registry modules; set a shared `TF_PLUGIN_CACHE_DIR` (gather warms it once,
       then extracts in parallel) and, if needed, raise `ACTIVITY_IAC_BUILD_TIMEOUT`
       / tune `ACTIVITY_IAC_MAX_WORKERS` (defaults 300s / 8).
     - **Boundary-dropped commits.** If a run warns that in-window commit(s) sit at
       the shallow-clone boundary (their whole-tree phantom diffs were dropped — see
       `meta.boundary_dropped_commits`), widen `ACTIVITY_CLONE_MARGIN_DAYS` (default
       `14`) and re-gather so each in-window commit keeps its real parent.
2. **Validate the store (trust gate).** Stop and fix the cause unless it prints
   `RESULT: OK` (exit 0):
   ```bash
   python3 validate.py workspace/journey.db
   ```
   Self-contained on a store (`edge_set` / `no_drift` / `idempotency` self-source their raw
   bundle via `extract`; an external `--bundle` is an optional single-repo cross-check,
   never required). It reports nine checks per repo — `target`, `referential_integrity`,
   `participant_completeness`, `provenance`, `no_fabrication`, `schema_conformance`,
   `edge_set`, `no_drift`, `idempotency` — and proves the store consistent with its own
   records, not that the records match GitHub (`docs/USAGE.md` §5.5). For a multi-repo
   project: `python3 validate.py workspace/journey.db --project <name>` (adds the
   project-wide `no_drift_people` and `member_status` checks).
3. **Materialize the bundle view** (store → `workspace/view/bundle.json`, offline):
   ```bash
   python3 view.py --store workspace/journey.db --from FROM --to TO --out workspace/view/bundle.json
   ```
   Use the FROM/TO you gathered (for a manifest, its window): the view records them as
   `meta.period` and `link.py` buckets only that window's items. `meta.ref_date` is the
   `--ref-date` you gathered with (when FROM/TO match the gathered window exactly), else TO;
   `view.py --ref-date D` overrides it. `--project`/`--repo` are
   auto-detected when the store holds one project with one repo; otherwise pass them
   (single repo: `--project OWNER --repo REPO`). For a **multi-repo project**, run it once
   per member you report on — `python3 view.py --store workspace/journey.db --project <name>
   --repo <owner/repo> --from FROM --to TO --out workspace/view/<owner>__<repo>.json` — and
   use that file in steps 4–6. Keep members apart in every later step too: give each member its own bundle, series and diagrams paths — `workspace/view/<owner>__<repo>.json`, `workspace/series/<owner>__<repo>.json` (`link.py` creates the directory), `--diagrams-dir workspace/diagrams/<owner>__<repo>` — so one member never overwrites another's files or is compared with a different repo in the series; each member's `bundle.diagrams` paths resolve against the parent of ITS diagrams dir. It writes the **raw** view only (no trains/buckets yet) and
   exits 2 with a message when the scope is ambiguous, unknown or empty.
4. **Link** (enriches the bundle view **in place**, offline). For a recurring digest pass
   the series index so this installment is framed against the previous month's — it
   keeps one entry per window, so re-running the same window replaces that entry:
   ```bash
   python3 link.py workspace/view/bundle.json --series workspace/series.json
   ```
   This adds `trains`, the four `buckets` (`shipped`, `rejected`, `in_flight`,
   `next_candidates`), `forecast`, `flow`, … and `series` (`new` / `carried_over` /
   `forecast_loop`). The series file is a thin convenience index over the store — never an
   override; delete it to start a fresh series. For a one-off digest run
   `python3 link.py workspace/view/bundle.json` instead (no `series` key is added).
   Multi-repo: keep one series file per member (`workspace/series/<owner>__<repo>.json`).
5. **Render diagrams** (rewrites the bundle view with `diagrams`). Preflight: `mmdc` must
   be on PATH (install mermaid-cli with `npm install -g @mermaid-js/mermaid-cli`).
   `graphify` is **optional** (used only for its supported languages); when it is absent —
   e.g. on Bicep/Terraform repos — the **directory provider** supplies code areas.
   ```bash
   python3 render.py workspace/view/bundle.json --diagrams-dir workspace/diagrams
   ```
   Writes `workspace/diagrams/*.mmd` (the flat diagrams plus one flowchart per deep-tier
   train) and **fails if any diagram does not compile** under `mmdc`. **`bundle.diagrams`
   paths are relative to the parent of `--diagrams-dir`** — here `workspace/`, so
   `diagrams/buckets_pie.mmd` is the file `workspace/diagrams/buckets_pie.mmd`. Only the
   bundle view file `render.py` rewrote carries `diagrams`. If `mmdc` is unavailable, add
   `--skip-validate` and say in the report that the diagrams are unvalidated.
6. **Train slices** (read-only; one per train you narrate — at least every
   `tier: "deep"` train). The bounded, self-contained evidence a per-train narrator reads:
   ```bash
   python3 link.py workspace/view/bundle.json --slice <train-id> > workspace/view/<train-id>.slice.json
   ```
   Train ids are `bundle["trains"][].id`.
7. **Project view** (multi-repo only; skip for a single repo):
   ```bash
   python3 digest.py --store workspace/journey.db --project <name> --from FROM --to TO > workspace/view/project.json
   ```
   Its `members[i].bundle` is enriched but has no `series` or `diagrams` — read those from
   the member's own bundle view file (steps 3–5). The cross-repo module graph is
   `render.emit_project_module_graph(view["module_edges"])`.
8. **Community-call transcript (optional).** If the user provides a
   community-call transcript (a local file passed explicitly, e.g. `/activity …
   --transcript PATH`) — no network, user-provided only — normalize it to
   clean prose first:
   ```bash
   python3 transcript.py path/to/call.vtt > workspace/view/transcript.txt
   ```
   `transcript.py` strips WebVTT/SRT structure (headers, NOTE/STYLE blocks, cue
   timings, SRT indices, inline tags) and collapses rolling-caption duplicates;
   plain `.txt`/`.md` passes through. Author the **Community call highlights** section
   grounded ONLY in that text (see the report rules below). No transcript ⇒ skip this
   step and omit the section.
9. **Write the report** (e.g. `workspace/report.md`) by filling `report-template.md`.
   Cite each fact with its `url`; do not state anything the views do not contain.
   - **Single repo:** every section reads the bundle view. Where the template says
     `view["shipped"]` read `bundle["buckets"]["shipped"]`; `view["people"]` /
     `view["halls"]` / `view["modules"]` → `bundle["people"]` / `bundle["halls"]` /
     `bundle["modules"]` (keyed by area id);
     `view["members"][i]["bundle"]` → the bundle view itself. There are no project
     trains: render *Decision trains* from the template's deep-train and mention-train
     blocks over `bundle["trains"]`. Omit *Related work* and *Cross-repo module
     dependency graph* (project view only).
   - **Field names:** fill placeholders by the template's "Placeholder rules" block.
     Refs (`buckets`, `view["shipped"]`, `series`, forecast candidates) and `flow` /
     `blockers` entries carry an id and url but no title: look the title, state,
     author and board status up in `bundle["prs"]` / `bundle["issues"]` by number.
   - **Multi-repo:** project-wide sections (executive summary, shipped, decision trains,
     related work, cross-repo module graph, module ownership) read the project view;
     per-member sections (since last installment, activity at a glance, releases, CI/CD,
     in flight, sprints, rejected, stalled/blocked, forecast, feature changes, content
     lifecycle, module graph, contributors, issue kinds) read each member's bundle view.
   - Embed each `bundle.diagrams` file (resolved against the parent of `--diagrams-dir`)
     as a fenced mermaid block, and ground each train's narrative in its slice.

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
  An area found in several members (a multi-repo project) is unioned and each row names its
  `repo`; write `owner/repo::<area>` (or add `--repo owner/repo`) for one member.
- **Blast radius / dependents** of a module: `python3 spotlight.py dependents <owner/repo>
  --store <db> --project <name>` (which members depend on it), or
  `dependents <owner/repo>::<area>` (which module areas, in-repo or in other members,
  depend on one module — gather a single monorepo with `--structural-iac` first).
- **One train's slice** (for narration or a spotlight): `python3 link.py <view> --slice <id>`;
  `python3 render.py <view> --train <id>` for its flowchart.
- **Multi-repo project view** (the structured fact base other formatters consume): `python3
  digest.py --store <db> --project <name> --from FROM --to TO` — see `BUNDLE.md` for the
  contract.
