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
   - **Multi-repo project (Phase 9).** To fold several repos under one logical
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
4. **Community-call transcript (optional, Phase 14).** If the user provides a
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
    Append **review/lifecycle texture** when present (Phase 10): *"· K review rounds"*
    (sum of `review_rounds.count` over the train's PRs) and *"· reopened M×"* (sum of
    `reopen_count` across the train's issue + PRs). Omit each when zero/absent.
    Then fill the `<!-- narrative: <train-id> -->` slot (after the effort line) with a
    **Phase 4b per-train narrative** authored by a narrator sub-agent — see below.
  - **MENTION trains** (`tier == "mention"`) collapse to a single line:
    `- train-id — {title} — {outcome} ({PR count} PR(s))`.
- Phase 4b **Per-train narration (sub-agent pass).** For EACH deep train, fill its
  `<!-- narrative: <train-id> -->` slot with a sourced narrative produced by a narrator
  sub-agent that reads ONLY that train's bounded slice:
  1. **Get the slice:** `python3 link.py <bundle-view> --slice <train-id>` prints the
     train's self-contained JSON slice (the `slice_train` shape: train / issue / prs /
     commits + `review_rounds`, capped `reviews`/`lifecycle`, `reopen_count`,
     `feature_deltas`). Read-only — it does NOT rewrite the bundle.
  1b. **Real diffs are already in the slice — deepen only if needed (lead-only).**
     File-level `feature_deltas` now carry a bounded `diff` for EVERY language (not just
     graphify symbols/comments), so the narrator reads the actual change *from the slice*.
     Only when `feature_deltas_diff_overflow > 0` (a churny train dropped some diffs for
     the per-train cap) or a truncated file genuinely matters, the **lead** MAY fetch more
     from the gather clone if it's still on disk (`workspace/<repo>-clone`):
     `git -C workspace/<repo>-clone show <slice.commits[].sha> -- <feature_delta path>`,
     folding a **bounded** excerpt into the slice before dispatch (prefer the module path
     over `examples/*` churn). Do this in the LEAD, never the narrator — the narrator
     stays slice-only so its evidence stays verifiable. Best-effort; skip if the clone is
     gone (the in-slice diffs + PR body + commit messages still tell the story).
  2. **Dispatch one narrator sub-agent per deep train, IN PARALLEL** (one Task each, sent
     together). Hand it the (optionally deepened) slice JSON and this contract:
     > You are a release narrator. Using ONLY the supplied train slice — never outside
     > knowledge, never invented facts or URLs — return a JSON object
     > `{summary, proposed, changed, rejected, shipped, evidence:[ref]}`. The **code layer
     > is your primary source** — mine the PR **title/body** (root cause + solution are
     > usually spelled out there), the **commit messages**, the `feature_deltas` paths +
     > any folded diff excerpt — and use the review/lifecycle layer for the *decision arc*:
     > - `summary`: 1–2 sentences — what the train set out to do and how it ended.
     > - `proposed`: the problem + asked-for change (from the issue + PR body).
     > - `changed`: the actual fix/approach — which module/path/symbol changed and how
     >   (PR body, commit messages, `feature_deltas`/diff) — *and* how it shifted across
     >   review rounds / reopens (`review_rounds.states`, `reviews` bodies, `lifecycle`).
     > - `rejected`: anything explicitly dropped/declined (a changes-requested that was
     >   removed, an abandoned alternative) — `null` if the slice shows none.
     > - `shipped`: what actually merged (merged PRs + `feature_deltas` + the diff).
     > - `evidence`: refs (pr/issue/commit/review URLs) **copied verbatim from the slice**
     >   that back the above; every claim must trace to one of them.
     > Ground every statement in the slice. If a field has no support in the slice, set it
     > to `null`/`[]` rather than guessing.
  3. **Verify, then compose.** Drop any `evidence` ref whose URL is not present in the slice
     (the sub-agent must introduce no new refs; a commit `sha`/url it cites must be one from
     `slice.commits`). Render the narrative under the train's effort line: the prose
     `summary`, then **Proposed / Changed / Rejected / Shipped** bullets (omit empty ones),
     each carrying its evidence link(s). This is the analysis layer — the model's judgment
     over the slice — and MUST stay grounded in the slice's sourced facts.
  - `slice_train(bundle, train_id)` (in-process) / `link.py --slice <train-id>` (CLI) is the
    bounded, self-contained unit a Phase 4b narrator sub-agent — or a spotlight — consumes
    for one train. `render.py --train <id>` produces a spotlight flowchart on demand for any
    tier.
- Phase 4a **Next-release forecast** — `bundle["forecast"]` contains a forward-only
  prediction over `buckets.next_candidates`. Render a **Next-release forecast** section:
  - Header: *"Next milestone: {forecast.next_milestone}"* (or "none identified").
  - Group candidates by tier in order: **Likely** (score ≥ 5.0) → **Possible** (≥ 2.0) →
    **Longshot** (< 2.0). Within each group, list in score-descending order.
  - Per candidate: the item title + link (from `ref.url`), its `train` id if set, and the
    `signals` list (e.g. "on milestone v1.2 · high-priority · open PR").
  - Note: this is a forward-only forecast. The predicted-vs-landed loop (Phase 7) is not
    yet present — do not describe it as such.
- Phase 11 **Stalled, blocked & pile-ups** — render a flow-health section over data that
  already exists in the bundle. It surfaces where work is stuck this period, from three
  sources, each item CITING its url:
  - **Stalled trains** — deep trains whose `train.effort.stalled` is true; report the
    train + its `effort.elapsed_days` (longest-stalled first), citing the train's root
    issue / PR url.
  - **Blocked issues** — issues carrying `issue["blocked_by"]` (numbers blocking it)
    and/or `issue["blocking"]` (numbers it blocks); list each with its `url`, and embed the
    `diagrams.blocker_graph` Mermaid flowchart (blocker → blocked) when present.
  - **Pile-ups** — open, high-activity issues (`issue["open_high_activity"]` true), each
    cited.
  - **Frame as flow signals, not blame.** The public digest NEVER attributes a stall or
    blocker to a person — per-person stall/blocker attribution is the gated internal
    appendix (below), off by default. Omit the whole section when there is no
    stalled / blocked / pile-up data.
- Phase 11 **Contributors & community** (public) — who moved the project this period,
  rendered from `people` + the bundle's `prs`/`commits`, each contributor CITED to their
  work:
  - **Footprint ranking:** rank contributors by a footprint counted from the bundle — PRs
    authored (`prs[].author`), PRs reviewed (`prs[].reviewers`), commits authored
    (`commits[].author`) — most-active first, and show their `people[login].modules`/
    `areas`. (The same agent-side counting the executive summary already does over the
    bundle; the underlying contribution facts are the deterministic `people`/`prs`/
    `commits` data.)
  - **Humans vs automation:** `people[login].is_bot` tags bots — list them under a
    separate "Automation" subhead so the human contributor view isn't skewed by
    dependabot/CI.
  - Embed `diagrams.contributor_graph` (the people ↔ code-area relationships) here.
  - **Recognition, not blame.** This section names who contributed and where; it NEVER
    attributes stalls/blockers. Omit when there is no `people` data.
- Phase 11 **Internal appendix — stall & blocker attribution (GATED; off by default).**
  Render this ONLY when the operator explicitly requests an *internal* report (the
  request says e.g. "internal" / "with attribution"); the DEFAULT public digest NEVER
  includes it. It attributes the flow signals to people from existing bundle data —
  stalled trains → their `effort.participants` / reviewers + the owning PR/issue authors;
  blocked issues → the blocker/blocked issue authors — each line cited. Keep it factual
  (who is associated with a stalled/blocked item), never judgmental. When not explicitly
  opted in, omit the appendix entirely.
- **Module biography (on-demand, via `spotlight module`).** For a *module-centric* view —
  how one module/area has **evolved over time** (parameter/symbol CRUD across ALL gathered
  history, not one window) — produce a grounded biography. This is a narrator pass (the
  skill's job, mirroring Phase 4b), not pipeline prose:
  1. **Get the slice:** `python3 spotlight.py module <area> --store <db> --project <name>
     --json` emits a bounded, full-history `slice_module` envelope: `time_range`, `symbols`
     / `files` each with a `lifecycle` of `{event, date, commit, pr?, before?, after?,
     diff?}` (rename chains folded), and the `trains` that touched the area. Read-only.
  2. **Narrate from ONLY that slice** → a grounded object
     `{summary, surface_changes, notable_revisions, removed, evidence:[ref]}`:
     - `summary`: 1–2 sentences — what this module is and how its surface has moved.
     - `surface_changes`: the **parameter/symbol CRUD** — read "param/output/resource X
       added → default/type changed → removed" straight off each symbol's `lifecycle` +
       `before`/`after`/`diff`. Group by symbol; note the driving `pr` per change.
     - `notable_revisions`: the handful of changes that actually shifted the contract
       (new required input, a default flip, a breaking rename via the folded chain).
     - `removed`: symbols whose last lifecycle event is `remove` (dropped surface).
     - `evidence`: commit/PR/issue refs **copied verbatim from the slice** backing the
       above; every claim traces to one.
  3. **Verify + render.** Drop any evidence ref not present in the slice. Render the
     **Module biography** report section (below) — prose `summary` + the surface-change
     timeline, each line cited. Stays grounded in the slice's sourced facts (the
     deterministic CRUD); the prose is the model's judgment over it.
- Phase 12 **Board status & sprint framing** — when the repo links a Projects v2 board
  (gather auto-discovers it), PRs/issues carry `board_status` (e.g. Todo / In Progress /
  Done / Blocked) and, on iteration boards, an `iteration` (sprint id); `bundle["sprints"]`
  holds `{id: {title, start, end}}`. Render:
  - **Board status on In-flight** — annotate each `buckets.in_flight` item with its
    `board_status` when present, and add a one-line **status breakdown** (count of the
    in-flight — or all gathered — items by `board_status`). This is the universal layer
    (works on status-only boards like the common case). Omit when no item carries a status.
  - **Sprints (release-train framing)** — ONLY when `bundle["sprints"]` is non-empty (the
    board defines an iteration field). Resolve previous / current / next via
    `link.select_sprints(bundle["sprints"], ref_date)` and, per sprint, list the items
    whose `iteration` is that sprint id (shipped vs in-flight). Omit the whole sprint
    subsection for status-only boards (no iterations) — most boards, so this is usually
    absent. All deterministic data; the framing prose is the agent's.
- Phase 13 **Series continuity ("Since last installment")** — present ONLY when the run
  was linked with `--series` and `bundle["series"].first_installment` is false. From
  `bundle["series"]`: render **New this installment** (`new`), **Carried over**
  (`carried_over`, each citing its `prior_status` and whether it has now shipped), and the
  **Forecast loop** (`forecast_loop.landed` vs `not_yet` — last installment's predictions
  matched against this window's shipped). Membership is deterministic and every item cites
  its ref; the continuity narrative is the agent's. Omit on the first installment.
- Phase 14 **Community call highlights** (public) — present ONLY when a transcript was
  provided (step 4). Author it from the normalized `transcript.py` output and NOTHING else:
  summarize topics / decisions / asks / follow-ups, and ground each specific claim with a
  short **verbatim quote** from the transcript (the transcript is the source — there are no
  urls to cite). Never attribute to the call anything the transcript doesn't say. Same
  grounded-narrator discipline as the per-train analyses (Phase 4b): read the bounded
  source, summarize, quote. Call context may inform the executive summary / decision-train
  framing, clearly flagged as call context — never as a code fact. Omit when no transcript.
