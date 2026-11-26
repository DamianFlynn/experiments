# {meta.project} — Activity Digest ({meta.from} → {meta.to})

<!-- Two inputs (SKILL.md "Procedure" says how each is made):
     * `bundle` = the per-repo BUNDLE VIEW file: `view.py --out B`, then
       `link.py B [--series S]` and `render.py B --diagrams-dir D` (both rewrite B).
       It is the only file with `series` and `diagrams`. `bundle.diagrams` paths
       are relative to the PARENT of D (D=workspace/diagrams -> resolve
       `diagrams/x.mmd` as workspace/diagrams/x.mmd).
     * `view` = the PROJECT VIEW (multi-repo only):
       `digest.py --store <path> --project <name> --from ... --to ...`, with the
       merged keys (trains, shipped, people, halls, modules, related_work,
       module_edges).
     Single-repo report: there is no `view`; read everything from `bundle`:
       view["shipped"] -> bundle["buckets"]["shipped"] (its refs carry no `repo`),
       view["people"] -> bundle["people"], view["halls"] -> bundle["halls"],
       view["modules"] -> bundle["modules"]
       (keyed by area id, not "<repo>::<area>"), view["members"][i]["bundle"] ->
       bundle. There are NO project trains: render "Decision trains" from the
       deep-train and mention-train blocks over bundle["trains"] only. The title
       uses `meta.owner`/`meta.repo` (a bundle `meta` has no `project`). Omit
       Related work and Cross-repo module dependency graph.
     Multi-repo report: project-wide sections read `view`; per-member sections
       render once per member from that member's bundle view file. digest's
       embedded copy (`view["members"][i]["bundle"]`) has no `series` and no
       `diagrams`: read those two from the member's own bundle view file. -->

<!-- placeholder-rules:begin
PLACEHOLDER RULES — read before filling anything below.

* `{ref.url}`-style placeholders are fields read straight from the data: the part
  before the first dot is a ROW VARIABLE (the row the section is iterating) and
  the rest is a real key path on that row. Nothing else may be read off a row: a
  name that is not in the data is never "probably there" (test_report_template.py
  checks every path here against real view.py -> link.py -> render.py and
  digest.py output).
* Row variables and where each row comes from:
  - `meta` — bundle["meta"] (`owner`, `repo`, `from`, `to`) or, multi-repo,
    view["meta"] (`project`, `repos`, `from`, `to`, `schema_version`).
  - `ref` — a ref `{type, id, url, train?}` from `buckets.shipped` /
    `buckets.in_flight` / `buckets.rejected` / `buckets.next_candidates`;
    view["shipped"] rows add `repo`; `series.new` / `series.carried_over` rows add
    `bucket` (carried rows also `prior_status`); `cand.ref` is one too. A ref has
    NO `title`, `number`, `state`, `author` or `board_status` — those are on `item`.
  - `item` — the PR or issue a ref or number points at (LOOKUP RULE below).
  - `pr` / `issue` / `commit` — a row of bundle["prs"] / bundle["issues"] /
    bundle["commits"].
  - `loop` — a `series.forecast_loop.landed` / `not_yet` / `dropped` row `{type, id, tier}`;
    it has no `url` — cite `item.url`.
  - `train` — a single-repo train, bundle["trains"][] (`id`, `kind`, `outcome`,
    `root_issue`, `issues`, `prs`, `pr_outcomes`, `commits`, `code_areas`, `tier`,
    `significance`, `effort`, `evidence`). It has no `title` and no `repos`.
    `train.prs` holds every attempt (rejected, merged, still open); each PR's own
    outcome is `train.pr_outcomes["<pr number>"]` ("merged" / "open" / "rejected").
    `train.issues` is every issue its PRs close; `train.root_issue` is the lowest.
  - `ptrain` — a project train, view["trains"][] (`id`, `repos`, `kind`, `outcome`,
    `prs`, `issues`, `commits`, `code_areas`, `tickets`, `evidence`). It has no
    `tier`, `effort`, `root_issue` or title.
  - `member` — view["members"][] `{repo, bundle}`.
  - `related` — view["related_work"][] `{ticket, train_ids}`.
  - `release` — bundle["releases"][]. `stats` — one value of
    bundle["workflow_stats"] (its key is the {workflow} name; skip keys starting `_`).
  - `sprint` — one of the `{id, title, start, end}` dicts returned by
    `link.select_sprints(bundle["sprints"], <reference date>)`.
  - `flow_entry` — bundle["flow"][]. `blocker` — bundle["blockers"][]. Both are
    issues: `number` + `url` only, no title.
  - `cand` — bundle["forecast"]["candidates"][] `{ref, train, score, tier, signals, notes}`.
  - `delta` — bundle["feature_deltas"][]. `move` — bundle["symbol_moves"]["links"][].
  - `artifact` — a value of bundle["artifacts"]; `ev` — one of its `lifecycle` events.
  - `area` — bundle["code_graph"]["areas"][]. `rule` — bundle["code_owners"][]
    `{pattern, owners}`. `module` — a value of `modules` (`commits`, `prs`,
    `files_changed`). `edge` — view["module_edges"][].
  - `fame` — bundle["halls"]["fame"][] (multi-repo: view["halls"]["fame"][]).
    `person` — a value of `people` (keyed by login).
  - `mod` — the `spotlight.py module ... --json` slice; `sym` — one of its
    `symbols` / `files`; `sev` — one `lifecycle` event of a `sym`; `mtl` — one row
    of `mod.trains[].timeline`.
* LOOKUP RULE (`item`): `ref.type` "pr" -> the bundle["prs"] row whose `number`
  equals `ref.id`; `ref.type` "issue" -> the bundle["issues"] row whose `number`
  equals `ref.id`. Numbers that are issue numbers: `flow_entry.number`,
  `flow_entry.blocked_by`, `blocker.number`, `blocker.blocks`, `train.root_issue`,
  `train.issues`;
  PR numbers: `train.prs`, `delta.pr`. In the project view, look the item up in
  the bundle of the member whose `member.repo` equals `ref.repo`. A project-train
  id is `ptrain-<project>/<repo>#issue-<n>` (or `#pr-<n>`); its qualified
  `ptrain.prs` / `ptrain.issues` entries are `<project>/<repo>#pr-<n>` /
  `#issue-<n>`: look `<n>` up in member `<repo>`, whose own single-repo train for
  the same anchor is `train-issue-<n>` (or `train-pr-<n>`).
* REFERENCE DATE: `meta.ref_date` (every view carries it; it defaults to `meta.to`).
* BASE BRANCH: `meta.base_branch` is the mainline "shipped" means merged into; a
  PR merged into any other branch is not shipped (it may appear in
  `train.contributing_prs`). In the project view read it per member
  (`member.bundle.meta`). When it is `null` the store did not know it, so every
  merged PR was counted as shipped: say so in one line under the report title.
* TRUNCATED DIAGRAMS: when `diagrams.truncated` has an entry for an embedded
  diagram, say so under it: "Showing {diagrams.truncated[].shown} of
  {diagrams.truncated[].total} {diagrams.truncated[].unit}."
* AGENT-DERIVED placeholders (no field of that name exists; fill by the rule):
  - {short title} — `item.title` of the train's root issue (`train.root_issue`),
    else of its first PR (`train.prs`). For a project train, the same rule on the
    anchor `<n>` in its id.
  - {effort line} — see the deep-train block under "Decision trains".
  - {workflow} — the bundle["workflow_stats"] key the `stats` row sits under.
  - {owners} — the `rule.owners` of the LAST `code_owners` rule whose
    `rule.pattern` matches the area directory `<area id>/` (GitHub CODEOWNERS
    matching, last match wins, `owners: []` un-assigns — BUNDLE.md `code_owners`).
  - {top contributors} — logins whose `person.modules` contains the area id.
  - {what was discussed}, {what was decided}, {verbatim quote},
    {requests raised}, {action items / who owns what} — from the normalized
    transcript only.
  - {one-line what-this-module-is}, {change} — written by the module-biography
    narrator from the `mod` slice only.
placeholder-rules:end -->

## Executive summary

<!-- source: project view view["shipped"] (refs) and view["trains"]; single repo
     bundle["buckets"]["shipped"] and bundle["trains"]. Count, never estimate.
     3–5 bullets — features, releases, CI health, key risks. -->

{count of ref in view.shipped where ref.type is "pr"} merged PRs and {count of ref in view.shipped where ref.type is "issue"} completed issues shipped; {count of ptrain in view.trains where ptrain.outcome is "shipped"} decision trains landed.

## Since last installment

<!-- Series continuity. A BUNDLE-scoped section, like Board status / Sprints:
     `series` is set by `link.py --series` on each member's bundle view file, so
     render it per member from that file's bundle["series"] (there is no
     project-wide `series`, and digest's embedded member copy has none). OMIT a
     member's block when its bundle["series"] is absent (no `--series` index) or
     `series.first_installment` is true — the first digest of a project has no
     prior to compare against. Framing prose is the agent's; the membership is
     deterministic and every item cites its ref. -->

**New this installment:** rows of `series.new` (first seen this window).

- [{item.title}]({ref.url}) ({ref.type} #{ref.id})

**Carried over:** rows of `series.carried_over` — in flight last installment and in a
bucket this window, each with where it stood last time (`ref.prior_status`) and where it
is now (`ref.bucket`: "shipped", "in_flight", "next_candidates" — re-planned — or
"rejected" — closed without shipping).

- [{item.title}]({ref.url}) ({ref.type} #{ref.id}) — *was {ref.prior_status}, now {ref.bucket}*

**Forecast loop** (last installment's predictions vs. what landed) — rows of
`series.forecast_loop.landed`, `series.forecast_loop.not_yet` and
`series.forecast_loop.dropped`. When `series.prior.captured` is "backfill" the prior
installment was recorded after a later month, so say its predictions were made late and
keep them out of any hit rate:

- **Landed:** predicted (tier `{loop.tier}`) and shipped this window: [{item.title}]({item.url}) ({loop.type} #{loop.id}).
- **Not yet:** predicted (tier `{loop.tier}`) but still not shipped: [{item.title}]({item.url}) ({loop.type} #{loop.id}).
- **Dropped:** predicted (tier `{loop.tier}`) but closed without shipping this window: [{item.title}]({item.url}) ({loop.type} #{loop.id}).

## Community call highlights

<!-- Community call. OMIT this whole section when NO transcript was provided. When one
     was, the skill ran `python3 transcript.py <path>` to get clean text; author
     this section ONLY from that text (it is the source — there are no PR/issue
     urls, so GROUND every specific claim with a short verbatim quote from the
     transcript). Cover: topics discussed, decisions made, asks / requests, and
     follow-ups / action items. Do not attribute anything to the call that the
     transcript does not say. Call context may also inform the Executive summary
     and Decision trains — clearly as call context, never as a code fact. -->

- **Topics:** {what was discussed}
- **Decisions:** {what was decided} — *"{verbatim quote}"*
- **Asks:** {requests raised}
- **Follow-ups:** {action items / who owns what}

## Shipped this period

<!-- source: multi-repo view["shipped"] (each ref carries `repo`, added by
     digest's `_merge_shipped`); single repo bundle["buckets"]["shipped"] (drop
     the Repo column). Title from `item` (LOOKUP RULE). Grouping by code area is
     the separate "Shipped by code area" section. -->

| Repo | Title | Ref |
|------|-------|-----|
| `{ref.repo}` | [{item.title}]({ref.url}) | {ref.type} #{ref.id} |

## Decision trains

<!-- Multi-repo: one project-train block per `ptrain` in view["trains"], then
     drill into the owning member's single-repo train (LOOKUP RULE) for the
     deep-train block. Single repo: no project-train block — one deep-train block
     per `train` in bundle["trains"] with `train.tier` "deep", one mention line
     per `train.tier` "mention".
     Order most-substantial first (rank by the number of `ptrain.prs` plus
     `ptrain.commits`; ties by outcome shipped > in_flight > rejected >
     abandoned), cross-repo trains (more than one `ptrain.repos`) first.
     `ptrain.tickets` is [] unless digest ran with ticket parsing switched on. -->

### {short title} — `{ptrain.id}`

- **Repos:** {ptrain.repos} — say "cross-repo" when there is more than one
- **Kind / outcome:** {ptrain.kind} / {ptrain.outcome}
- **PRs:** {ptrain.prs} — qualified ids; display as `<repo>#<n>`
- **Issues:** {ptrain.issues}
- **Commits:** {count of ptrain.commits}
- **Tickets:** {ptrain.tickets} — or "—" when empty
- **Evidence:** {ptrain.evidence[].url}

#### Deep train `{train.id}` — {short title}

<!-- Deep-train block (`train.tier` "deep"). Flowchart = the file named by
     diagrams.train_flowcharts[train.id] in the member's bundle view (inline it).
     {effort line}: merged trains — "landed in {train.effort.elapsed_days} days ·
     {train.effort.reviewers} reviewers · {train.effort.participants} contributors"
     (`train.effort.reviewers` / `train.effort.participants` are COUNTS); add
     "stalled" when `train.effort.stalled`. Open trains (`train.effort.merged_at` is
     null) — "open N days" from `train.effort.opened_at` to the reference date. Then
     "· K review rounds" (sum `pr.review_rounds.count` over the train's PRs) and
     "· reopened M×" (sum `pr.reopen_count` / `issue.reopen_count` over its PRs and
     root issue); omit each when zero or absent. The narrative slot below is
     filled by the per-train narrator over `link.py <member-bundle> --slice
     <train-id>` — see "Per-train narration" in reference/report-sections.md. -->

```mermaid
{contents of diagrams.train_flowcharts[train.id]}
```

*{effort line}*

<!-- narrative: <train-id> -->

**Mention trains** (`train.tier` "mention") — one line each:

- `{train.id}` — {short title} — {train.outcome} ({count of train.prs} PR(s))

## Related work (cross-repo, ticket-linked)

<!-- Source: view["related_work"] — `{ticket, train_ids}` objects. Each entry is
     two or more project trains that share an internal ticket reference
     (Jira/ADO-style, e.g. ABC-1234) but are NOT connected by any GitHub
     closes/cross_ref edge. Opt-in: always [] unless digest.py ran with a
     manifest declaring `ticket_prefixes` or with `--ticket-pattern`.
     Omit the section entirely when view["related_work"] is empty. -->

| Ticket | Trains |
|--------|--------|
| `{related.ticket}` | {related.train_ids} |

## Activity at a glance

Embed the rendered diagrams (the `.mmd` file contents) as fenced mermaid blocks:

```mermaid
{contents of diagrams.buckets_pie}
```

```mermaid
{contents of diagrams.timeline_gantt}
```

## Releases

For each `release` in bundle["releases"] (newest first); flag a prerelease when
`release.prerelease` is true.

- **{release.name}** (`{release.tag_name}`) — published {release.published_at}. [release]({release.url})

## CI/CD health

For each `stats` row of bundle["workflow_stats"]: total runs and success/failure
split. Skip the reserved `_truncated` key; when it is present, say the counts are
partial (GitHub's 1,000-run search cap).

- **{workflow}** — {stats.success}/{stats.total} succeeded ({stats.failure} failed, {stats.cancelled} cancelled).

## In flight

For each `ref` in `buckets.in_flight`: title and board status come from `item`
(LOOKUP RULE); add the train when `ref.train` is set and the Projects v2 board
status when `item.board_status` is set.

- [{item.title}]({ref.url}) ({ref.type} #{ref.id}) — train `{ref.train}` — _{item.board_status}_

<!-- Board status breakdown: when any in-flight item carries `item.board_status`,
     add a one-line count by status. Omit when no board is linked. -->

**Board status:** {count of in-flight items per item.board_status}

## Sprints

<!-- Sprints — ONLY when bundle["sprints"] is non-empty (an iteration board). Most
     boards are status-only (no iterations) -> OMIT this whole section. Resolve
     previous / current / next with `link.select_sprints(bundle["sprints"], ref)`
     where ref is the reference date (`meta.ref_date`);
     per sprint, list the items whose `item.iteration` equals `sprint.id`. -->

- **Current sprint:** {sprint.title} ({sprint.start} → {sprint.end}) — {count of items whose item.iteration is sprint.id}
- **Previous:** {sprint.title} — what shipped
- **Next:** {sprint.title} — committed items

## Rejected / abandoned

For each `ref` in `buckets.rejected`: PRs closed without merge + issues closed as
not planned.

- [{item.title}]({ref.url}) ({ref.type} #{ref.id})

## Stalled, blocked & pile-ups

Where flow is stuck this period. **Flow signals, not blame** — the public digest
never attributes a stall/blocker to a person (that is the gated internal appendix).
Omit the whole section when `flow`/`blockers` are empty and there are no stalled trains.

**Flow pathologies** — the per-issue classification in bundle["flow"] (deterministic;
each `flow_entry` carries `state`, `age_days`, `reactions`, `blocked_by` (open
blockers only), `blocked_by_unknown`, and `signals`; the title comes from `item`).
`blocked` means at least one **open** blocker; a closed blocker no longer blocks;
`hung` means old **and** stale (no update for 30 days). Group by `flow_entry.state`
in this order — **blocked → upvoted_but_ignored → traction_then_abandoned → hung** —
each issue cited:

- [{item.title}]({flow_entry.url}) (#{flow_entry.number}) — *{flow_entry.state}* — {flow_entry.signals}

**Top blockers** — open issues ranked by how many other open issues they block, from
bundle["blockers"] (highest `blocker.blocks_count` first) — the ones to unblock first:

- [{item.title}]({blocker.url}) (#{blocker.number}) — blocks {blocker.blocks_count}: {blocker.blocks}

Embed the dependency graph when `diagrams.blocker_graph` is present. It follows the
same open-only rule as the lists above: an edge is drawn only when the blocker and the
blocked issue are both open; a closed (resolved) blocker, a closed dependant and an
issue of unknown state are omitted, so the graph shows exactly the relations
bundle["blockers"] counts:

```mermaid
{contents of diagrams.blocker_graph}
```

**Stalled trains** — trains with `train.effort.stalled` true (train-level, distinct
from the issue-level flow above), longest `train.effort.elapsed_days` first; cite the
root issue's url (`item.url` of `train.root_issue`), else the first PR's:

- `{train.id}` — {short title} — stalled {train.effort.elapsed_days}d — [source]({item.url})

## Next-release forecast

From bundle["forecast"] (forward-only). The predicted-vs-landed loop is NOT
here — it lives in the "Since last installment" section above, and
only when that section is present (i.e. the run was linked with `--series` and
this is not the first installment); otherwise there is no loop to show.

**Next milestone:** {forecast.next_milestone} *(or "none identified" when null)*

### Likely

For each `cand` with `cand.tier` "likely" (score ≥ 5.0), `cand.score` descending:

- [{item.title}]({cand.ref.url}) ({cand.ref.type} #{cand.ref.id}) — train `{cand.train}` — *{cand.signals}* — {cand.notes}

### Possible

For each `cand` with `cand.tier` "possible" (score ≥ 2.0):

- [{item.title}]({cand.ref.url}) ({cand.ref.type} #{cand.ref.id}) — train `{cand.train}` — *{cand.signals}* — {cand.notes}

### Longshot

For each `cand` with `cand.tier` "longshot" (score < 2.0):

- [{item.title}]({cand.ref.url}) ({cand.ref.type} #{cand.ref.id}) — train `{cand.train}` — *{cand.signals}* — {cand.notes}

(Omit "train …" when `cand.train` is null; join `cand.signals` with " · ". `cand.notes` is
non-scoring context, e.g. "previous attempt rejected": join with " · " and omit the "— …"
part when it is empty.)

## Feature changes (add / drop / change)

The `feature_deltas` ledger as a table grouped by `delta.kind`. Each row cites its
artifact (`delta.artifact`) and the commit/PR that changed it. `delta.area` is the
code area when the path resolves to one, else null.

```mermaid
{contents of diagrams.deltas_bar}
```

| Kind | Subject | Name | Author | PR | Commit |
|------|---------|------|--------|----|--------|
| {delta.kind} | {delta.subject} | {delta.name} | {delta.author} | {delta.pr} (or "—") | [{delta.commit} (first 7)]({delta.url}) |

**Moves** — when `symbol_moves.links` is non-empty, collapse the matching add/drop
rows into one "moved" row per `move`: `{move.name}` {move.from_path} → {move.to_path}
(confidence {move.confidence}; present `medium` as likely-not-certain).

### Symbol-level changes

For `feature_deltas` whose `delta.subject` is `symbol`/`comment`, show the actual
change using the bounded `delta.before`/`delta.after` and `delta.detail`
(`<lang> <subkind> <name>`). Omit when no symbol deltas resolved. **Call out decision
context:** comment deltas whose `delta.detail` contains the `todo` subkind (e.g.
`bicep todo // TODO: …`) and comment **drops/changes** (a note or `@description`
replaced as a decision evolved) are strong focus signals for follow-on issues/PRs —
surface these first.

| Detail | Kind | Before | After | PR | Commit |
|--------|------|--------|-------|----|--------|
| {delta.detail} | {delta.kind} | `{delta.before}` (or "—") | `{delta.after}` (or "—") | {delta.pr} (or "—") | [{delta.commit} (first 7)]({delta.url}) |

## Content lifecycle (built / changed / dropped)

From `artifacts`: examples, docs, READMEs, symbols and comments introduced, revised,
or removed/replaced within the window — *who* authored and *who* removed each, with
dates. Surfaces "we shipped an example in March and dropped it in May", which a
tip-only diff hides.

```mermaid
{contents of diagrams.content_timeline}
```

For each `artifact` (`artifact.status` "removed"/"replaced" first), one entry per
lifecycle event `ev`:

- **{artifact.name}** ({artifact.kind}, `{artifact.path}`) — {artifact.status}. Lifecycle: {ev.event} by {ev.author} on {ev.date} ([{ev.commit} (first 7)]({ev.ref.url})), …; replaced by `{artifact.replaced_by}` when set.

## Shipped by code area

Group the `buckets.shipped` refs by their train's code areas: `ref.train` is a
`train.id`; that train's `train.code_areas` are `area.id` values. Items with no
train or no resolved area fall under "Unattributed".

### {area.label} (`{area.id}`)

- [{item.title}]({ref.url}) ({ref.type} #{ref.id}) — train `{ref.train}`

## Module dependency graph / blast radius

Resolved inter-area dependencies from `code_graph.areas[].edges` (Bicep
`bicep build`→ARM; Terraform `terraform graph`). Each edge names its target area
(`to`, null when external — identity then in `ref`), pinned `version` and whether it
is `transitive`; cross-module edges show which areas a change ripples into. When no
edges resolve, the diagram is a `No module dependencies` placeholder (always present).

```mermaid
{contents of diagrams.module_graph}
```

## Cross-repo module dependency graph

<!-- Source: view["module_edges"] (each `edge` is {src_repo, src_area, dst_repo,
     dst_area, version, transitive, cross_repo}) and the diagram
     render.emit_project_module_graph(view["module_edges"]). Lead with the
     cross-repo edges (`edge.cross_repo` true): a member's module depending on
     another member's published module. For "if member X changes, who is
     affected?", cite `spotlight dependents <owner/repo>` (its `dependents` list).
     Omit the whole section when view["module_edges"] is empty. -->

```mermaid
{render.emit_project_module_graph(view["module_edges"])}
```

| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo |
|---|---|---|---|
| `{edge.src_repo}` · `{edge.src_area}` | `{edge.dst_repo}` · `{edge.dst_area}` | {edge.version} (or "—") | "yes" when {edge.cross_repo} |

## Module ownership

<!-- Multi-repo: view["modules"] keys are "<repo>::<area>" — split on "::" for the
     Repo and Module columns; view["people"] logins are merged across all member
     repos (union by login; a person's `areas` / `modules` are "<repo>::<area>"
     too); CODEOWNERS rules are per member
     (view["members"][i]["bundle"]["code_owners"]). Single repo: bundle["modules"]
     is keyed by the area id and the repo is `meta.repo`. -->

From `code_owners` + `people`/`modules`: who owns and who touched each module this
window.

| Repo | Module | Owners (CODEOWNERS) | Top contributors | Commits | PRs | Files |
|------|--------|---------------------|------------------|---------|-----|-------|
| `{member.repo}` | `{area.id}` | {owners} | {top contributors} | {module.commits} | {module.prs} | {module.files_changed} |

Embed `diagrams.contributor_graph` (people ↔ code-area edges) here, once:

```mermaid
{contents of diagrams.contributor_graph}
```

## Issue kinds

The `issue.kind` mix across the window's issues (feature / module-request / bug /
idea / question / docs / other), derived from native issue types → label facets →
template → heuristic. Group or label with `issue.facets` (area / priority / status /
lifecycle) where useful.

```mermaid
{contents of diagrams.kind_breakdown}
```

## Contributors & community

Who moved the project this period (public — recognition, not blame). The ranking is
**deterministic** — read it straight off `halls.fame` (footprint score
`prs_merged*2 + prs_reviewed + commits_authored`, highest first), and pull each person's
detail from their profile, `person` = the `people` entry for `fame.login`
(`person.prs_authored`, `person.prs_merged`, `person.merge_rate`, `person.prs_reviewed`,
`person.commits_authored`, `person.issues_opened`, `person.review_latency_days`,
`person.first_seen`/`person.last_active`, `person.examples_authored`/
`person.docs_authored`/`person.symbols_authored`, `person.areas`). **Cite** each
contributor's work — link a representative PR they authored (`pr.author`) or
reviewed (`pr.reviewers`) so the footprint is grounded. Omit the whole section when
`people` is empty.

<!-- Multi-repo: rank off view["halls"]["fame"] and read `person` from
     view["people"]: each figure is combined across the members (counts summed,
     `merge_rate` recomputed, `review_latency_days` the median of all members'
     samples, earliest `first_seen` / latest `last_active`) and `person.areas`
     are "<repo>::<area>". A member's own section reads that member's bundle,
     whose `people` are only the people with a contribution in that member. -->

| Contributor | Authored | Merged | Reviewed | Commits | Areas | Evidence |
|-------------|----------|--------|----------|---------|-------|----------|
| `{fame.login}` | {person.prs_authored} | {person.prs_merged} | {person.prs_reviewed} | {person.commits_authored} | {person.areas} | [#{pr.number}]({pr.url}), … |

Optionally note responsiveness/tenure where it adds signal — e.g. *"median review
latency {person.review_latency_days}d · active {person.first_seen} →
{person.last_active}"* — only when those fields are non-null.

<!-- Automation: when any `person.is_bot` is true, list those logins under this
     subhead so the human view above isn't skewed by dependabot/CI bots. OMIT the
     subhead entirely when there are no bot contributors (no empty "Automation:"
     stanza). Bots are excluded from `halls.fame` already. -->

**Automation:** the `people` logins whose `person.is_bot` is true.

The people ↔ code-area relationships are shown by `diagrams.contributor_graph`, embedded
once under **Module ownership** above (do not repeat the Mermaid block here).

## Module biography

<!-- ON-DEMAND deep-dive: render only when a module/area is in focus (the request asks
     for a module's history, or a module is notably active). Source: the narrator pass
     over `python3 spotlight.py module <area> --store <db> --project <name> --json`
     (see SKILL.md "Module biography"). Full-history, NOT window-bounded. Omit from
     the standard windowed digest. A lifecycle event's `sev.pr` is a qualified id
     (`<project>/<repo>#pr-<n>`) with no url: link it with the `mtl.url` of the
     `mod.trains[].timeline` row whose `mtl.id` equals `sev.pr`; when there is no
     such row, cite `sev.commit` as plain text (no invented link). -->

### `{mod.focus}` — {one-line what-this-module-is}

_Summary: 1–2 sentences on how the module's surface has moved over
`{mod.time_range.first}` → `{mod.time_range.last}`._

**Surface changes** (parameter / symbol CRUD, from each `sym.lifecycle`):

- `{sym.name}` ({sym.subkind}) — {sev.event} on {sev.date} → …, each change citing its driving PR ([{sev.pr}]({mtl.url}))

**Notable revisions** — the changes that shifted the contract (new required input, a
default flip, a breaking rename):

- {change} — [{sev.pr}]({mtl.url})

**Removed** — symbols whose last lifecycle event is `remove` (`sym.status` "removed"):

- `{sym.name}` — dropped in [{sev.pr}]({mtl.url})

## Internal appendix — stall & blocker attribution

<!-- GATED: render this section ONLY when the operator explicitly requests an *internal*
     report (e.g. the request says "internal" / "with attribution"). The DEFAULT public
     digest OMITS this section entirely. It attributes the flow signals (from the
     "Stalled, blocked & pile-ups" section) to people, using existing bundle data only.
     Factual association, never judgmental. EACH line cites its own url.
     Attribute ONLY from login fields: `pr.author`, `pr.reviewers` (a login list),
     `issue.author`, `issue.assignees` (a login list). The train's effort block
     holds COUNTS of reviewers and participants, not names — never read logins
     from it. -->

**Stalled trains** — for each stalled `train`: the `pr.author` and `pr.reviewers` of
its PRs (`train.prs`), and the `issue.author` and `issue.assignees` of its root issue
(`train.root_issue`), each with that role:

- `{pr.author}` (PR author) — stalled train `{train.id}` ({train.effort.elapsed_days}d) — [#{pr.number}]({pr.url})
- `{issue.author}` (opened the root issue) — stalled train `{train.id}` — [#{issue.number}]({issue.url})

**Blocked issues** — for each `flow_entry` with `flow_entry.state` "blocked": the
`issue.author` and `issue.assignees` of the blocked issue and of each open blocker in
`flow_entry.blocked_by`:

- `{issue.author}` — blocked issue #{flow_entry.number} — [link]({flow_entry.url})
