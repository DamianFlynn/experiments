# Report sections — authoring guide

How to fill each section of `report-template.md` from the materialized bundle view.
Loaded on demand (progressive disclosure): `SKILL.md` carries the procedure; this is
the per-section detail. **Universal rules:** the store/view is the only source of truth
(omit anything not in it); quote PR/issue numbers and link their `url`; a section with no
backing data is omitted, never padded. Sections appear in `report-template.md` order.

## Executive summary
3–5 bullets — features, releases, CI health, key risks — informed by board + call context.
Embed `diagrams.buckets_pie`.

## Since last installment
Present ONLY when the run was linked with `--series` and `bundle["series"].first_installment`
is false. From `bundle["series"]`: **New this installment** (`new`), **Carried over**
(`carried_over`, each citing its `prior_status` and whether it has now shipped), and the
**Forecast loop** (`forecast_loop.landed` vs `not_yet` — last installment's predictions
matched against this window's shipped). Membership is deterministic and every item cites its
ref; the continuity narrative is the agent's. Omit on the first installment.

## Community call highlights
Present ONLY when a transcript was provided (procedure step 4). Author it from the normalized
`transcript.py` output and NOTHING else: summarize topics / decisions / asks / follow-ups,
grounding each specific claim with a short **verbatim quote** from the transcript (the
transcript is the source — there are no urls to cite). Never attribute to the call anything
the transcript doesn't say. Same grounded-narrator discipline as the per-train analyses
(below). Call context may inform the executive summary / decision-train framing, clearly
flagged as call context — never as a code fact. Omit when no transcript.

## Shipped this period (+ by code area)
Merged PRs + completed issues from `buckets.shipped`. **Group by code area** (`code_area`/
`area`; `modules`) and link each item's `url`. Releases published in-window are a subsection
(tag, date, link; prereleases flagged).

## Decision trains
Each train in `bundle["trains"]` carries `significance`, `tier` (`"deep"`/`"mention"`), and an
`effort` block.
- **DEEP trains** (`tier == "deep"`) get a full sub-section. Embed the train's flowchart from
  `diagrams.train_flowcharts[id]` (a file path — read and inline as a fenced mermaid block). Add an
  **effort line** from `effort`:
  - Merged: *"landed in N days · R reviewers · P contributors"* (`elapsed_days`, `reviewers`,
    `participants`; note `stalled: true` when present).
  - Open: *"open N days"* (from `effort.opened_at` to today; `merged_at` is null).
  - Append review/lifecycle texture when present: *"· K review rounds"* (sum
    `review_rounds.count` over the train's PRs) and *"· reopened M×"* (sum `reopen_count` across
    its issue + PRs). Omit each when zero/absent.
  Then fill the `<!-- narrative: <train-id> -->` slot with a per-train narrative (next).
- **MENTION trains** (`tier == "mention"`) collapse to one line:
  `- train-id — {title} — {outcome} ({PR count} PR(s))`.

`render.py --train <id>` produces a spotlight flowchart on demand for any tier.

### Per-train narration (sub-agent pass)
For EACH deep train, fill its `<!-- narrative: <train-id> -->` slot with a sourced narrative
from a narrator sub-agent that reads ONLY that train's bounded slice:
1. **Get the slice:** `python3 link.py <bundle-view> --slice <train-id>` prints the train's
   self-contained JSON slice (`slice_train` shape: train / issue / prs / commits +
   `review_rounds`, capped `reviews`/`lifecycle`, `reopen_count`, `feature_deltas`). Read-only.
1b. **Real diffs are already in the slice — deepen only if needed (lead-only).** File-level
   `feature_deltas` carry a bounded `diff` for every language, so the narrator reads the actual
   change from the slice. Only when `feature_deltas_diff_overflow > 0` (a churny train dropped
   some diffs for the per-train cap) or a truncated file genuinely matters, the **lead** MAY
   fetch more from the gather clone if still on disk (`workspace/<repo>-clone`):
   `git -C workspace/<repo>-clone show <slice.commits[].sha> -- <feature_delta path>`, folding a
   **bounded** excerpt into the slice before dispatch (prefer the module path over `examples/*`
   churn). Do this in the LEAD, never the narrator — the narrator stays slice-only so its
   evidence stays verifiable. Best-effort; skip if the clone is gone.
2. **Dispatch one narrator sub-agent per deep train, IN PARALLEL** (one Task each, sent
   together). Hand it the (optionally deepened) slice JSON and this contract:
   > You are a release narrator. Using ONLY the supplied train slice — never outside knowledge,
   > never invented facts or URLs — return a JSON object
   > `{summary, proposed, changed, rejected, shipped, evidence:[ref]}`. The **code layer is your
   > primary source** — mine the PR **title/body** (root cause + solution are usually spelled
   > out there), the **commit messages**, the `feature_deltas` paths + any folded diff excerpt —
   > and use the review/lifecycle layer for the *decision arc*:
   > - `summary`: 1–2 sentences — what the train set out to do and how it ended.
   > - `proposed`: the problem + asked-for change (from the issue + PR body).
   > - `changed`: the actual fix/approach — which module/path/symbol changed and how (PR body,
   >   commit messages, `feature_deltas`/diff) — *and* how it shifted across review rounds /
   >   reopens (`review_rounds.states`, `reviews` bodies, `lifecycle`).
   > - `rejected`: anything explicitly dropped/declined — `null` if the slice shows none.
   > - `shipped`: what actually merged (merged PRs + `feature_deltas` + the diff).
   > - `evidence`: refs (pr/issue/commit/review URLs) **copied verbatim from the slice** that
   >   back the above; every claim must trace to one of them.
   > Ground every statement in the slice. If a field has no support in the slice, set it to
   > `null`/`[]` rather than guessing.
3. **Verify, then compose.** Drop any `evidence` ref whose URL is not in the slice (the
   sub-agent introduces no new refs; a cited commit `sha`/url must be one from `slice.commits`).
   Render the narrative under the train's effort line: prose `summary`, then **Proposed /
   Changed / Rejected / Shipped** bullets (omit empty ones), each carrying its evidence link(s).
   This is the analysis layer — the model's judgment over the slice — and MUST stay grounded in
   the slice's sourced facts.

## Next-release forecast
`bundle["forecast"]` is a forward-only prediction over `buckets.next_candidates`.
- Header: *"Next milestone: {forecast.next_milestone}"* (or "none identified").
- Group candidates by tier: **Likely** (score ≥ 5.0) → **Possible** (≥ 2.0) → **Longshot**
  (< 2.0); within each, score-descending.
- Per candidate: item title + link (`ref.url`), its `train` id if set, and the `signals` list
  (e.g. "on milestone v1.2 · high-priority · open PR").
- The predicted-vs-landed loop is NOT here — it lives in "Since last installment" (only when
  that section is present).

## Board status & sprints
When the repo links a Projects v2 board (gather auto-discovers ALL maintained boards), PRs/
issues carry `board_status` (Todo / In Progress / Done / Blocked …) and, on iteration boards,
an `iteration` (sprint id); `bundle["sprints"]` holds `{id: {title, start, end}}`.
- **Board status on In-flight** — annotate each `buckets.in_flight` item with its `board_status`
  when present, plus a one-line status **breakdown** (count by `board_status`). Universal layer
  (works on status-only boards, the common case). Omit when no item carries a status.
- **Sprints (release-train framing)** — ONLY when `bundle["sprints"]` is non-empty (the board
  defines an iteration field). Resolve previous / current / next via
  `link.select_sprints(bundle["sprints"], ref_date)` and per sprint list the items whose
  `iteration` is that sprint id (shipped vs in-flight). Omit for status-only boards (usually
  absent). Deterministic data; the framing prose is the agent's.

## Feature changes (add / drop / change)
The `feature_deltas` ledger as a table grouped by code area; each row cites its artifact and the
commit/PR that changed it.
- **Symbol-level changes** subsection — `feature_deltas` with `subject` `symbol`/`comment` carry
  a bounded `before`/`after` + `detail` (`<lang> <subkind> <name>`); show the actual change text.
- **Moves** — when `symbol_moves.links` is non-empty, collapse the matching add/drop deltas into
  a single **"moved"** row (`from_path → to_path`), labelling `confidence`; present
  `medium`-confidence moves as likely-not-certain.

## Content lifecycle (built / changed / dropped)
From `artifacts` (example/doc/readme/symbol/comment) + the `timeline` event stream; embed
`diagrams.content_timeline` and `diagrams.deltas_bar`. PR/issue comment + review-comment bodies
and issue reactions are in the bundle for narrative grounding.

## Module dependency graph / blast radius
A section embedding `diagrams.module_graph` from `code_graph.areas[].edges` (and, multi-repo,
the cross-repo module graph).

## Module ownership & Issue kinds
- **Module ownership** — `code_owners` + `people[*].modules`/`modules`; embed
  `diagrams.contributor_graph` here (once).
- **Issue kinds** — breakdown by each issue's `kind`; embed `diagrams.kind_breakdown`. Group/
  label using each issue/PR's `facets` (area/priority/status/lifecycle).

## Contributors & community (public)
Who moved the project this period — recognition, not blame — from the **deterministic** `people`
profile + `halls.fame`, each contributor CITED to their work:
- **Footprint ranking:** read it off `halls.fame` (score `prs_merged*2 + prs_reviewed +
  commits_authored`, bots already excluded, highest first) — no agent-side counting. Per
  contributor pull detail from `people[login]`: `prs_authored`/`prs_merged`/`merge_rate`,
  `prs_reviewed`, `commits_authored`, `issues_opened`, `review_latency_days`,
  `first_seen`/`last_active`, `examples_authored`/`docs_authored`/`symbols_authored`, `areas`.
  Note responsiveness/tenure (review_latency, first→last active) only where non-null.
- **Humans vs automation:** `people[login].is_bot` tags bots — list them under a separate
  "Automation" subhead. (`halls.internal`/shame/blame is intentionally NOT built — recognition
  only.)
- **Recognition, not blame** — never attributes stalls/blockers. Omit when `people` is empty.

## Stalled, blocked & pile-ups
Flow-health from the **deterministic** `flow` + `blockers` projections (plus train-level stall),
each item CITING its url:
- **Flow pathologies** — `bundle["flow"]` is the per-OPEN-issue classification (`blocked` /
  `upvoted_but_ignored` / `traction_then_abandoned` / `hung`; healthy omitted). Each entry
  carries `state`, `age_days`, `reactions`, `blocked_by`, and a ready `signals` list — group by
  `state` (blocked → upvoted_but_ignored → traction_then_abandoned → hung) and render the
  signals; no agent-side classifying.
- **Top blockers** — `bundle["blockers"]` ranks issues by `blocks_count` (how many each blocks,
  highest first) — the ones to unblock first. Embed `diagrams.blocker_graph` (blocker → blocked)
  when present.
- **Stalled trains** — deep trains whose `train.effort.stalled` is true (train-level, distinct
  from the issue-level flow); report the train + `effort.elapsed_days` (longest-stalled first),
  citing the train's root issue / PR url.
- **Frame as flow signals, not blame.** The public digest NEVER attributes a stall/blocker to a
  person — that is the gated internal appendix (below), off by default. Omit when `flow`/
  `blockers` are empty and there are no stalled trains.

## Module biography (on-demand, via `spotlight module`)
For a *module-centric* view — how one module/area has **evolved over time** (parameter/symbol
CRUD across ALL gathered history, not one window) — produce a grounded biography (a narrator
pass, mirroring per-train narration; not pipeline prose):
1. **Get the slice:** `python3 spotlight.py module <area> --store <db> --project <name> --json`
   emits a bounded, full-history `slice_module` envelope: `time_range`, `symbols`/`files` each
   with a `lifecycle` of `{event, date, commit, pr?, before?, after?, diff?}` (rename chains
   folded), and the `trains` that touched the area. Read-only.
2. **Narrate from ONLY that slice** → a grounded object `{summary, surface_changes,
   notable_revisions, removed, evidence:[ref]}`:
   - `summary`: 1–2 sentences — what this module is and how its surface has moved.
   - `surface_changes`: the **parameter/symbol CRUD** — read "param/output/resource X added →
     default/type changed → removed" off each symbol's `lifecycle` + `before`/`after`/`diff`.
     Group by symbol; note the driving `pr` per change.
   - `notable_revisions`: the handful that shifted the contract (new required input, a default
     flip, a breaking rename via the folded chain).
   - `removed`: symbols whose last lifecycle event is `remove`.
   - `evidence`: commit/PR/issue refs **copied verbatim from the slice**; every claim traces to
     one.
3. **Verify + render.** Drop any evidence ref not in the slice. Render the **Module biography**
   section — prose `summary` + the surface-change timeline, each line cited. Grounded in the
   slice's deterministic CRUD; the prose is the model's judgment over it.

## Internal appendix — stall & blocker attribution (GATED; off by default)
Render ONLY when the operator explicitly requests an *internal* report (the request says e.g.
"internal" / "with attribution"); the DEFAULT public digest NEVER includes it. It attributes the
flow signals to people from existing bundle data — stalled trains → their `effort.participants` /
reviewers + the owning PR/issue authors; blocked issues → the blocker/blocked issue authors —
each line cited. Factual (who is associated with a stalled/blocked item), never judgmental. When
not explicitly opted in, omit entirely.

## Releases · CI/CD health · In-flight · Rejected
Standard sections from `releases`, `workflow_stats`, `buckets.in_flight`, and
`buckets.rejected`/abandoned respectively — each item cited; omit when empty.
