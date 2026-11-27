# Report sections — authoring guide

How to fill each section of `report-template.md` from the materialized bundle view.
Loaded on demand (progressive disclosure): `SKILL.md` carries the procedure; this is
the per-section detail. **Universal rules:** the store/view is the only source of truth
(omit anything not in it); quote PR/issue numbers and link their `url`; a section with no
backing data is omitted, never padded. This guide groups sections by topic; the order
they appear in the report is `report-template.md`'s.

**Field names.** The template's "Placeholder rules" block defines the row variables
(`ref`, `item`, `train`, `ptrain`, `cand`, `flow_entry`, …) used below and in every
placeholder. Bucket refs, `series` rows, forecast candidates' `ref`, `flow` and
`blockers` entries carry an id/number and (mostly) a `url` — **never a title, state,
author or board status**. Resolve those with the template's LOOKUP RULE: the
`bundle["prs"]` / `bundle["issues"]` row whose `number` equals `ref.id` (in the project
view, in the bundle of the member whose `repo` equals `ref.repo`). Trains have no title:
use the root issue's title, else the first PR's. The reference date is `meta.ref_date`
(every view carries it; it defaults to `meta.to`).

**Length.** Lists are capped like diagrams (template rule LONG LISTS): at most 40 rows per
list or table that renders one row per item (shipped, each code area, mention trains or each
theme group of them, feature changes, symbol changes, content lifecycle, in flight, rejected,
each flow group, each forecast tier), in the section's own order, and a cut list always ends
with "Showing N of M <unit> — the full list is in the bundle (`<key>`)". A train touching more
than 10 code areas is **cross-cutting**: it is described once, under "Cross-cutting changes",
never repeated under each area it touches. Deep-train blocks are not cut.

**State as of (#85).** When `meta.state_as_of` is present (per member in the project
view), the view's records are as last gathered at that date, later than the view's end:
write one caveat line under the report title saying so. Omit it when the key is absent.
Since #97 a view projects each record's state, comments, reviews and lifecycle events to
its end (BUNDLE.md "As of the window's end"), so the caveat covers only what has no history: titles,
labels, milestones, board status.

**Gathered after the window.** When `meta.records_as_of` is present and `meta.state_as_of`
is absent (a window gathered after it ended — e.g. a past month caught up into its own
store), write one caveat line under the report title, exactly: "Records are shown as of
{meta.records_as_of.instant}, the end of this window; titles, labels, milestones and board
status are as gathered on {meta.gathered_at}." (the date part of `meta.gathered_at`, the
time the records were gathered). With `meta.state_as_of` present the state-as-of line
replaces it; omit it when `meta.records_as_of` is absent.

## Executive summary
3–5 bullets — features, releases, CI health, key risks — informed by board + call context.
Counts come from the data (shipped refs by `ref.type`, trains by `ptrain.outcome` /
`train.outcome`), never estimated.

## Since last installment
Present ONLY when the run was linked with `--series` and `bundle["series"]` (the member's own
bundle view file — digest's embedded member copy has no `series`) has `first_installment`
false. The comparison is with the installment recorded for the window just before this one.
**New to the series** (`series.new`: shipped or in-flight items the prior installment did
not know at all — new to the report series, which includes old items first touched this
window; it is not "newly opened". `series.new_opened` is the subset created inside this
window: report that count as the window's new work), **Carried over** (`series.carried_over`: last installment's in-flight items,
each citing its `ref.prior_status` and its current `ref.bucket` — shipped, still in flight,
re-planned as a next candidate, or rejected), and the **Forecast loop**
(`series.forecast_loop.landed` / `series.forecast_loop.not_yet` /
`series.forecast_loop.dropped` — last installment's predictions matched against this
window's shipped and rejected; when `series.prior.captured` is "backfill" those predictions
were recorded late, so keep them out of any hit rate). Every row is a ref
without a title; forecast-loop rows (`{type, id, tier}`) have no `url` either — take both
from `item`. Membership is deterministic and every item cites its ref; the continuity
narrative is the agent's. Omit on the first installment.

## Community call highlights
Present ONLY when a transcript was provided (`SKILL.md` procedure step 8). Author it from the normalized
`transcript.py` output and NOTHING else: summarize topics / decisions / asks / follow-ups,
grounding each specific claim with a short **verbatim quote** from the transcript (the
transcript is the source — there are no urls to cite). Never attribute to the call anything
the transcript doesn't say. Same grounded-narrator discipline as the per-train analyses
(below). Call context may inform the executive summary / decision-train framing, clearly
flagged as call context — never as a code fact. Omit when no transcript.

## Shipped this period (+ by code area)
Merged PRs + completed issues from `buckets.shipped` (multi-repo: `view["shipped"]`, whose
refs add `repo`), each titled via `item` and linked by `ref.url`. **Group by code area** in
the "Shipped by code area" section: a ref's `ref.train` names a `train.id`, and that train's
`train.code_areas` are `area.id` values (`code_graph.areas[].label` for display); no train or
no area → "Unattributed". A shipped item whose train is cross-cutting (more than 10
`train.code_areas`, typically a mechanical sweep across modules) is listed once under
"Cross-cutting changes" with its area count, not under every area. Cap each list at 40 items
(and the areas at 40, most shipped first), each cut list ending with its "Showing N of M" line
naming `buckets.shipped`. Releases are their own section (see "Releases · CI/CD health ·
In-flight · Rejected" below), not part of this one.

## Activity at a glance
Embed `diagrams.buckets_pie` (items per bucket) and `diagrams.timeline_gantt` here — this is
their one canonical place.

## Decision trains
Two train shapes exist. **Project trains** (`view["trains"]`, multi-repo only) carry `repos`,
`kind`, `outcome`, qualified `prs`/`issues`/`commits`, `code_areas` (the member trains' areas
as `<repo>::<area>`), `tickets`, `evidence` — no `tier`, `significance`, `effort` or
`root_issue`. **Single-repo trains**
(`bundle["trains"]`, and each member bundle's) carry `significance`, `tier` (`"deep"`/
`"mention"`), `root_issue`, numeric `issues` and `prs`, `pr_outcomes`, and an `effort` block.
A train holds every attempt for its issues: `train.prs` can mix a rejected first try, the PR
that shipped and an open follow-up — tell them apart by `train.pr_outcomes` (`"merged"` /
`"open"` / `"rejected"` per PR number) rather than calling every PR of a shipped train shipped.
`train.issues` lists every issue its PRs close (a merely mentioned issue is not a member). A single-repo report has no
project trains; a multi-repo report renders each project train and drills into the owning
member's train (`ptrain-<project>/<repo>#issue-<n>` ↔ member `<repo>`'s `train-issue-<n>`).
- **DEEP trains** (`train.tier` "deep") get a full sub-section. Embed the train's flowchart from
  `diagrams.train_flowcharts[id]` (a file path in the member's bundle view — read and inline as a
  fenced mermaid block). Add an **effort line** from `train.effort`:
  - Shipped: *"landed in N days · R reviewers · P contributors"* (`train.effort.elapsed_days`,
    `train.effort.reviewers`, `train.effort.participants` — the last two are integer COUNTS);
    when `train.effort.slow_to_land` is true (it took more than 21 days) say *"slow to land
    (N days)"*. A shipped train is never "stalled".
  - Open: *"open N days"* (from `train.effort.opened_at` to the reference date;
    `train.effort.merged_at` is null); when `train.effort.stalled` is true add *"· stalled: no
    activity for N days"* (`train.effort.idle_days`).
  - Append review/lifecycle texture when present: *"· K review rounds"* (sum
    `pr.review_rounds.count` over the train's PRs; definition in BUNDLE.md "Review decision &
    rounds") and *"· reopened M×"* (sum `pr.reopen_count` / `issue.reopen_count` across its
    root issue + PRs; the key is absent when zero). Omit each when zero/absent.
  Then fill the template's `<!-- narrative: <train-id> -->` slot (in its deep-train block) with
  a per-train narrative (next).
- **MENTION trains** (`train.tier` "mention") collapse to one line:
  `- {train.id} — {short title} — {train.outcome} ({count of train.prs} PR(s))` — unless the
  train is a **story train** (`train.story` true): it gets its own sub-section with a story
  (see "Stories (the story brief)" below).
- **A monorepo of independent PRs** (most trains are one PR; the largest holds one or two):
  campaigns that run all month are dozens of trains with no closing reference between them,
  so a flat list of 100+ lines says little. Group the mention lines by **theme** — from the
  PR titles (conventional prefix, the module or path named) and `train.code_areas` — one bold
  line per theme with its train count, the train lines under it. The theme names are the
  writer's framing; every theme is backed by the train ids listed under it, and the trains
  stay the unit of evidence. Put cross-cutting trains (more than 10 `train.code_areas`) in one
  "Cross-cutting changes" group. When the deep tier is taken by mechanical sweeps (one PR,
  kind chore / docs / other, hundreds of areas), give each its flowchart and effort line and a
  short narrative — say what was swept and how widely — rather than a full arc. Cap each group
  at 40 lines (Length rule above).

`render.py <bundle> --train <id>` produces a spotlight flowchart on demand for any tier.

Large windows: every diagram is capped (with a visible "+K more" marker) to stay inside
Mermaid's limits. When `diagrams.truncated[<name or train id>]` is present, say so under the
embedded diagram, e.g. *"Showing 200 of 700 PRs."* (`shown` of `total` `unit`).

### Per-train narration (sub-agent pass)
For EACH deep train, fill its `<!-- narrative: <train-id> -->` slot with a sourced narrative
from a narrator sub-agent that reads ONLY that train's bounded slice (a train that is also a
story train gets the story brief below instead):
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
2b. **Batched mode (many deep trains).** One narrator per train is the default. When a view
   has many deep trains of ordinary size (a monorepo month often has 8–18, a roll-up more),
   you MAY instead dispatch one narrator per **batch** — per view (one per month), or per N
   trains with N at most 8 — and run the batches in parallel. The contract does not change;
   the hand-over does:
   - Give each train's slice to the batch as its **own block** — a heading `train <id>`
     followed by that slice's JSON exactly as `--slice` printed it (deepened per 1b if you
     did) — never merged into one object or summarised.
   - Prepend to the contract above: *"You are given several train slices, each under its own
     heading. Narrate each train from ITS OWN slice only: a fact, ref or URL from one train's
     slice must never appear in another train's narrative. Return one JSON object keyed by
     train id, `{"<train-id>": {summary, proposed, changed, rejected, shipped, evidence},
     …}`, with exactly the train ids you were given."*
   - Verify **per train** (step 3), against that train's own slice, never the batch's union:
     every `evidence` URL of train T must be in T's slice. A train missing from the reply is
     narrated again on its own; a key for a train you did not hand over is discarded.
   - Keep single mode for any train whose slice is large — `feature_deltas_diff_overflow > 0`,
     a slice you deepened in 1b, or one far longer than the rest — so it gets a narrator's
     whole attention; batch the others.
3. **Verify, then compose.** Drop any `evidence` ref whose URL is not in the slice (the
   sub-agent introduces no new refs; a cited commit must be one of `slice.commits`, linked
   by its `url`).
   Render the narrative under the train's effort line: prose `summary`, then **Proposed /
   Changed / Rejected / Shipped** bullets (omit empty ones), each carrying its evidence link(s).
   This is the analysis layer — the model's judgment over the slice — and MUST stay grounded in
   the slice's sourced facts.

### Stories (the story brief)
The deep tier ranks by footprint, so mechanical sweeps fill it while the most discussed work
is a one-line mention. **Story trains** (`train.story` true, ranked by `train.story_rank`;
`bundle["stories"]`, BUNDLE.md "The story layer") get a story whatever their tier — in
flight included: that is often where the debate is. A story explains the substance — what
changed, what each objection meant, what was decided, what landed or what open work waits
on — so a reader does not have to guess (a guess can be unfair to the people involved). Put the story in the train's
sub-section (a story train that is a mention train gets one: its effort line, then the
story); end the section with one line when `stories.more` is not empty: *"N more trains had
notable discussion: <ids>"*. No story train → no stories, and nothing is said.

1. **Get the slice** as for any train (`python3 link.py <bundle-view> --slice <train-id>`).
   The story material is `slice.conversation` (BUNDLE.md "Slice `conversation`"):
   - `timeline` — openings with their body, every human comment, review with its state,
     review comment (with the file, line and diff lines it is attached to and the thread it
     replies to, when stored), lifecycle event, PR commit (with its message body as `text`
     and a `change` summary: files by class, modules, `shared`, interface rows, changelog /
     shared-file lines) and merged commit, the merge;
   - `turning_points` marked by rule (a rework carries what changed after the request);
   - `outcome` — where it stands as of `as_of` and what landed: the items' state, review
     verdicts and outstanding change requests, `ball` (who the next move is with, DERIVED by
     rule), `waiting_on`, `linked` items with title / author / state / description,
     `modules` (shipped: the maintainers' changelog items verbatim, version, interface
     names, breaking), `net_change` (an open PR's own net change);
   - `threads` — the open-threads ledger, every question / request with a status by rule;
   - `truncation`, `bots`, `pr_history`, `not_gathered`, and `gaps` (what an older store
     cannot show).
2. **Dispatch one story narrator per story train** (the batching of step 2b applies, with
   each slice as its own block). Hand it the slice and this brief:
   > You write the story of one decision train for a reader who uses the module, from ONLY
   > the supplied slice — never outside knowledge, never an invented fact, name, number or
   > URL. A story says what happened IN SUBSTANCE: what the change was, what each objection
   > was about, what was decided and what ended up in the product — or, for open work, what
   > it waits on. Who said what and when is the evidence, not the story.
   >
   > **Shape.** One-sentence headline (bold) that says what this is about for a user of the
   > module ("Disabling six weak TLS cipher suites by default in the API Management module").
   > Then the story — 200–300 words, up to 400 for a long-running open item (the headline
   > and the closing part not counted); spend them on substance, not on who replied when —
   > then the closing part: **Where it stands** (work in flight) or **What landed** (shipped
   > work), as a paragraph of at most about 120 words starting with those words.
   >
   > **Rules.**
   > 1. *Dates carry the year* on first mention and whenever the year changes ("1 November
   >    2025", then "10 June 2026"). Durations are anchored to a date: "219 days later, on 10
   >    June 2026", never a bare "219 days later".
   > 2. *Each turning point in substance.* For every change request, objection, rework or
   >    constraint the story mentions, say in one plain sentence a module user can follow
   >    what the change or objection was technically, tied to the file, parameter or module it
   >    concerns — from the review comment's `path` / `diff_hunk`, the commit `change`
   >    summaries and `patches`, the commit `text`, `paths_since_request`. "would impact all
   >    the modules" becomes "the change was to `.github/workflows/avm.template.module.yml`,
   >    the workflow template every module's pipeline runs, where it added
   >    `skipPassedRulesReport: true`" (a commit `change` summary marks such a file `shared`). Define
   >    repository-specific jargon once ("the `defaults` test", the rule check it runs) using only what
   >    the slice states.
   > 3. *Decisions and their effect, explicitly.* What was dropped, kept, renamed, deferred —
   >    taken from the outcome block's `modules` (changelog items, interface names), the `change`
   >    summaries (parameters dropped / added) and commit `text` (where a commit says why).
   >    Never infer a decision from tone or from who spoke last. If the slice does not show
   >    how something was resolved, say "the record does not show how this was resolved".
   > 4. *No dangling threads.* Every question, constraint, request or second issue the story
   >    mentions is either resolved in the story (what happened, with its evidence: a reply,
   >    a commit, a changelog item) or explicitly called out as open with what it waits on.
   >    Use `threads` (status by rule: `answered` only means somebody replied — read the
   >    reply) and `outcome.linked` (a linked issue comes with its title, author, state and
   >    what it asks: give them). Do not mention a thread you cannot close or call open.
   > 5. *Where it stands* (in flight): what is blocking (an outstanding change request — by
   >    whom, since when; an open linked item it waits on; a question nobody answered), who
   >    the next move is with — the outcome block's `ball`, introduced as derived ("by the record's
   >    sequence of events, the next move is with …"), never as anyone's fault — and what a
   >    reader could do to help, ONLY actions the record itself points to (review the PR,
   >    answer the question asked, take the ownership the maintainers asked for, work on the
   >    linked issue). Give the state (open / closed / merged) of every issue and PR the story
   >    names.
   > 6. *What landed* (shipped): the version and the outcome in the maintainers' own
   >    changelog words (quote the `changelog` items of the outcome block's `modules`), the PR's state
   >    (merged), and whether anything discussed was deferred or left out (say so, with the
   >    changelog or commit text that says it).
   > 7. *Fairness.* Quote requests and answers in context: a short answer ("Agreed, no need
   >    for both.") comes with what it agreed to and what changed as a result. Do not let the
   >    choice of quotes suggest the work was compromised, rushed or watered down unless the
   >    record says so; a scope reduction is a decision — give its stated reason (a commit
   >    body, a changelog line) — and where the record gives none, say that. Do not
   >    characterise people: no judgement of competence, mood, tone, effort or motive (not
   >    "frustrated", "pushed back hard", "finally", "sloppy", "patiently"), no blame, no
   >    praise. Describe disagreement by positions: "X asked for …; Y preferred … because …"
   >    — the because only when the item says it.
   > 8. *Evidence.* Every claim cites the item it rests on by its `url`, copied verbatim.
   >    Short verbatim quotes are allowed — in double quotes, attributed to the item's
   >    `actor`, exactly as in the slice. Dates and durations come from `at` values and the
   >    effort / discussion blocks; say "N days without recorded activity", never why nobody
   >    acted. Code notes (`code_notes`) are not in file order: never join their lines into
   >    prose.
   > 9. *Limits of the record.* When `truncation.cut` / `text_omitted` is not empty, say once
   >    that the material was truncated and which items. When `pr_history` says
   >    `not_gathered` / `missing`, do not speak about the PR's internal history. Never claim
   >    anything in `not_gathered` (title edits, force-pushes, label changes, review-thread
   >    resolution, CI results). When `gaps` is not empty and the story needs that detail
   >    (which file a comment was on), say the record does not show it.
   > 10. When the slice does not support a story — no turn changes the course, the
   >    conversation is acknowledgements — return exactly "No notable discussion." rather
   >    than inflate.
   > Return `{story, evidence: [url]}`.
3. **Verify, then compose.** Run the mechanical check against the train's own slice file:
   ```bash
   python3 link.py workspace/view/<train-id>.slice.json --check-story workspace/view/<train-id>.story.md
   ```
   It exits 0 when every URL the story cites occurs in the slice and every quoted string
   (in `"…"` or `“…”`, at least 4 characters) occurs verbatim in a slice text (whitespace
   normalised), AND the structure holds: the first date in the prose carries a year
   (`first_date_without_year`), the story has its closing part — "Where it stands" for a
   train in flight, "What landed" for a shipped one (`closing_paragraph_missing`) — and
   every issue / PR number the story names (`#N` or a link to the repository's issue /
   pull) is named again in that closing part with a state word (open, closed, merged,
   unmerged, draft; `numbers_without_state`) that matches the state the slice's outcome
   block records (`state_mismatch`, read from the clause between that number and the next).
   Else exit 1 with the lists. Drop or fix each listed sentence and check again; never
   publish a story that fails it.
   **What the check does not cover** — read the story once against rules 2–7: that each
   turning point is explained in substance, that decisions come from the outcome block
   and change summaries rather than tone, that every thread mentioned is closed or called
   open, that a later date names its year when the year changes, that the "ball" is
   presented as derived, and that the quotes are fair in context. A story can pass the
   check and still fail these.

## Next-release forecast
`bundle["forecast"]` is a forward-only prediction over `buckets.next_candidates`.
- Header: *"Next milestone: {forecast.next_milestone}"* (or "none identified" when null).
- Candidates come from three signal sources: the next milestone, a priority label (default
  patterns plus any `--priority-label` patterns, listed in `forecast.priority_patterns`) and
  an open, non-draft PR approved with no later change request (signal
  "approved, no later change request" — needs no milestone discipline).
- **Empty forecast:** when `forecast.candidates` is empty, say "No forecast this installment",
  list the reasons in `forecast.notes` (one per missing signal source), and point the reader to
  USAGE §6.3 for what the repo would need. No `forecast.notes` means nothing was open.
- Group candidates by `cand.tier`: **Likely** (score ≥ 5.0) → **Possible** (≥ 2.0) →
  **Longshot** (< 2.0); within each, `cand.score` descending.
- Per candidate: the item's title (`item`, looked up from `cand.ref`) + link (`cand.ref.url`),
  its `cand.train` id if set, and the `cand.signals` list (e.g. "on milestone v1.2 ·
  high-priority · open PR"), then any `cand.notes` (non-scoring, e.g. "previous attempt
  rejected"). A train id alone is not work in progress: only an open PR or an `in_flight`
  train scores "open PR" / "work in progress".
- The predicted-vs-landed loop is NOT here — it lives in "Since last installment" (only when
  that section is present).

## Board status & sprints
When the repo links a Projects v2 board (gather auto-discovers ALL maintained boards), PRs/
issues carry `board_status` (Todo / In Progress / Done / Blocked …) and, on iteration boards,
an `iteration` (sprint id) — on the `bundle["prs"]` / `bundle["issues"]` rows, NOT on the
bucket refs; `bundle["sprints"]` holds `{id: {title, start, end}}`.
- **Board status on In-flight** — annotate each `buckets.in_flight` ref with its
  `item.board_status` when present, plus a one-line status **breakdown** (count by
  `item.board_status`). Universal layer (works on status-only boards, the common case). Omit
  when no item carries a status.
- **Sprints (release-train framing)** — ONLY when `bundle["sprints"]` is non-empty (the board
  defines an iteration field). Resolve previous / current / next via
  `link.select_sprints(bundle["sprints"], ref)` with the reference date (`meta.ref_date`); each result is `{id, title, start, end}`. Per sprint list the
  items whose `item.iteration` equals `sprint.id` (shipped vs in-flight). Omit for status-only
  boards (usually absent). Deterministic data; the framing prose is the agent's.

## Feature changes (add / drop / change)
The `feature_deltas` ledger as a table grouped by `delta.kind`; each row cites its artifact
(`delta.artifact`) and the commit/PR that changed it (`delta.commit`, `delta.url`,
`delta.pr`). Embed `diagrams.deltas_bar` here — this is its one canonical place. A month on a
large monorepo holds thousands of deltas: show 40 rows and the "Showing N of M feature changes"
line naming `feature_deltas` (the bar diagram still shows the whole mix).
- **Symbol-level changes** subsection — `feature_deltas` with `subject` `symbol`/`comment` carry
  a bounded `delta.before`/`delta.after` + `delta.detail` (`<lang> <subkind> <name>`); show the
  actual change text.
- **Moves** — when `symbol_moves.links` is non-empty, collapse the matching add/drop deltas into
  a single **"moved"** row (`move.from_path` → `move.to_path`), labelling `move.confidence`;
  present `medium`-confidence moves as likely-not-certain.

## Content lifecycle (built / changed / dropped)
From `artifacts` (example/doc/readme/symbol/comment; `artifact.status` comes from the last
lifecycle event) + the `timeline` event stream; embed `diagrams.content_timeline`. PR/issue
comment + review-comment bodies and issue reactions are in the bundle for narrative grounding.
List at most 40 artifacts (removed / replaced first) and end a cut list with "Showing N of M
artifacts" naming `artifacts`.

## Module dependency graph / blast radius
A section embedding `diagrams.module_graph` from `code_graph.areas[].edges` (and, multi-repo,
the cross-repo module graph from `view["module_edges"]`).

## New contributors
Who is new this window, from the gather's per-window `newcomers` fact — never from
`people` (`first_seen` is only the earliest activity inside the view; `author_association`
is GitHub's value when gathered, not as of the window). Each `newcomers.new` entry is a
person whose first PR / issue in the repository was created in the window: `login`,
`first` ("pr" | "issue"), `number`, `url`. Cite each entry's item. Bots are excluded by the
gather. `newcomers.unknown` (the check failed) and `newcomers.not_checked` (over the
gather's cap) are neither new nor returning: name them in one line. `newcomers.collected`
false means the window was gathered with `--no-newcomers`: say it was not looked up. A
view spanning several gathered windows merges theirs (adding `windows` and
`covers_view`, false when they leave days of the view uncovered). Multi-repo: per member (`member.bundle.newcomers`). Omit the
section when the key is absent.

## Module ownership & Issue kinds
- **Module ownership** — `code_owners` (owners of the last rule matching the area directory) +
  `person.modules` / `modules`; embed `diagrams.contributor_graph` here (once).
- **Issue kinds** — breakdown by each issue's `issue.kind`; embed `diagrams.kind_breakdown`.
  Group/label using each issue/PR's `facets` (area/priority/status/lifecycle).

## Contributors & community (public)
Who moved the project this period — recognition, not blame — from the **deterministic** `people`
profile + `halls.fame`, each contributor CITED to their work:
- **Footprint ranking:** read it off `halls.fame` (score `prs_merged*2 + prs_reviewed +
  commits_authored - own_pr_commits`, bots already excluded, highest first) — no agent-side
  counting. `own_pr_commits` (absent when 0) are commits that land the person's own merged
  PRs (squash commits): that work already scores through `prs_merged`, so it is not counted
  again; `commits_authored` stays the true commit count. Per
  contributor pull detail from `people[login]`: `prs_authored`/`prs_merged`/`merge_rate`,
  `prs_reviewed`, `commits_authored`, `issues_opened`, `review_latency_days`,
  `first_seen`/`last_active`, `examples_authored`/`docs_authored`/`symbols_authored`, `areas`.
  Note responsiveness/tenure (review_latency, first→last active) only where non-null.
  `prs_authored` is every PR of the person in the view (opened earlier and still open or
  closed in the window included) — label it "PRs in scope", never "opened this month". PRs
  opened inside the window are `prs_opened` (absent when every PR in the view was opened in
  the window; it then equals `prs_authored`).
- **Humans vs automation:** `people[login].is_bot` tags bots — list them under a separate
  "Automation" subhead. (Only the `fame` hall exists — no internal/shame/blame hall is built;
  recognition only.)
- **Recognition, not blame** — never attributes stalls/blockers. Omit when `people` is empty.
- **Multi-repo:** use the project view's `halls.fame` and `people` — figures combined across
  members (counts summed, `merge_rate` recomputed, `review_latency_days` the median of all
  members' samples, earliest `first_seen` / latest `last_active`), `areas` as
  `<repo>::<area>`. A member bundle's `people` are only that member's contributors.

## Stalled, blocked & pile-ups
Flow-health from the **deterministic** `flow` + `blockers` projections (plus train-level stall),
each item CITING its url (titles via `item`; `flow`/`blockers` entries are issue numbers):
- **Whole backlog, not the month.** `flow` and `blockers` classify every OPEN issue the view
  holds — the repo's whole open backlog as of the reference date, issues years old included —
  not this month's issues. Say so in one line where flow is presented ("open backlog of the
  whole repo as of <ref date>") and never report a flow count as this month's activity.
- **Flow pathologies** — `bundle["flow"]` is the per-OPEN-issue classification (`blocked` /
  `upvoted_but_ignored` / `traction_then_abandoned` / `hung`; healthy omitted). Each entry
  carries `state`, `age_days`, `reactions`, `blocked_by` (OPEN blockers only),
  `blocked_by_unknown` (blockers not in the bundle), and a ready `signals` list — group by
  `flow_entry.state` (blocked → upvoted_but_ignored → traction_then_abandoned → hung) and render
  the signals; no agent-side classifying. A closed blocker does not make an issue `blocked`;
  `hung` = open ≥ 60 d AND no update for ≥ 30 d.
- **Top blockers** — `bundle["blockers"]` ranks OPEN issues by `blocker.blocks_count` (how many
  other OPEN issues each blocks, highest first) — the ones to unblock first. Embed
  `diagrams.blocker_graph` (blocker → blocked) when present; it follows the same open-only
  rule: an edge is drawn only when both the blocker and the blocked issue are open (a closed
  blocker, a closed dependant and an issue of unknown state are left out).
- **Stalled trains** — OPEN trains (`train.outcome` "in_flight") whose `train.effort.stalled`
  is true: no activity on any of their PRs or issues for more than 21 days at the reference
  date (train-level, distinct from the issue-level flow). Report the train +
  `train.effort.idle_days` (longest idle first; `train.effort.last_activity_at` is the last
  recorded activity), citing the root issue's url (`train.root_issue` via `item`), else the
  first PR's. Shipped trains never appear here, however long they took
  (`train.effort.slow_to_land` belongs on the deep-train effort line).
- **Frame as flow signals, not blame.** The public digest NEVER attributes a stall/blocker to a
  person — that is the gated internal appendix (below), off by default. Omit when `flow`/
  `blockers` are empty and there are no stalled trains.

## Module biography (on-demand, via `spotlight module`)
For a *module-centric* view — how one module/area has **evolved over time** (parameter/symbol
CRUD across ALL gathered history, not one window) — produce a grounded biography (a narrator
pass, mirroring per-train narration; not pipeline prose):
1. **Get the slice:** `python3 spotlight.py module <area> --store <db> --project <name> --json`
   emits a bounded, full-history `slice_module` envelope: `focus`, `time_range`,
   `symbols`/`files` each with a `lifecycle` of `{event, date, commit, pr?, before?, after?,
   diff?}` (rename chains folded), and the `trains` that touched the area. A lifecycle `pr` is
   a qualified id (`<project>/<repo>#pr-<n>`) without a url: its url is the
   `trains[].timeline` row with the same `id`. Read-only.
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
     one. Spotlight output carries no commit url (a lifecycle `commit`, a `trains[].timeline`
     row of kind `commit`): cite a commit by its sha as plain text, never an invented link.
3. **Verify + render.** Drop any evidence ref not in the slice. Render the **Module biography**
   section — prose `summary` + the surface-change timeline, each line cited. Grounded in the
   slice's deterministic CRUD; the prose is the model's judgment over it.

## Internal appendix — stall & blocker attribution (GATED; off by default)
Render ONLY when the operator explicitly requests an *internal* report (the request says e.g.
"internal" / "with attribution"); the DEFAULT public digest NEVER includes it. It attributes the
flow signals to people from existing bundle data, reading ONLY login fields: stalled trains
(open trains with `train.effort.stalled`; shipped work is never attributed a stall) →
the `pr.author` and `pr.reviewers` (login list) of the train's PRs and the `issue.author` and
`issue.assignees` (login list) of its root issue; blocked issues → the `issue.author` and
`issue.assignees` of the blocked issue and of its open blockers (`flow_entry.blocked_by`) —
each line cited. The train effort block only COUNTS reviewers and participants (integers), so
it can never name anyone. Factual (who is associated with a stalled/blocked item), never
judgmental. When not explicitly opted in, omit entirely.

## Releases · CI/CD health · In-flight · Rejected
Standard sections from `releases`, `workflow_stats` (skip keys starting `_`),
`buckets.in_flight`, and `buckets.rejected`/abandoned respectively — each item cited (titles
via `item`); omit when empty. **CI caveat (#94):** when `meta.facts_windows` is present, the
CI counts are a sum over the gathered windows it lists (`meta.facts_windows.workflow_stats`);
when `meta.facts_windows.workflow_stats_complete` is false the total is partial and
`meta.facts_windows.workflow_stats_excluded` names the windows left out — with none left out,
the missing days were never gathered: name the view's period (`meta.period`) minus the summed
windows as "no gather covers …"; the code graph and owners come from
`meta.facts_windows.structure`. Say so in one line under CI/CD health; omit it when the key
is absent. **Releases** (`release.tag_name`, `release.published_at`, `release.url`; flag
`release.prerelease`) is its own section, after Activity at a glance.
