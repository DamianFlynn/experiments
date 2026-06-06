# {project} — Activity Digest ({from} → {to})

<!-- Multi-repo digest. All sections below consume the JSON view emitted by
     `python3 digest.py --store <path> --project <name> --from ... --to ...`.
     Per-member (single-repo) sections render once per entry in
     `view["members"][i]["bundle"]`; project-wide sections read the merged keys
     (trains, shipped, people, modules, related_work). -->

## Executive summary

<!-- source: len(view["shipped"]) shipped, len(view["trains"]) trains -->

{N} merged PRs across {M} decision trains. {shipped_count} items shipped.

## Shipped this period

<!-- Multi-repo: source from `view["shipped"]` (each row has a `repo` key added
     by `_merge_shipped`). Single-repo fallback: per-member
     `view["members"][i]["bundle"]["buckets"]["shipped"]`. -->

| Repo | Title | Number |
|------|-------|--------|
| `{view["shipped"][].repo}` | [{title}]({url}) | #{number} |

## Decision trains

<!-- Multi-repo: source from `view["trains"]` (project-wide trains keyed off
     qualified node ids). Each train carries:
       - `repos`    — list of member repos contributing to this train
       - `kind`     — "feature" | "bug" | "other"
       - `outcome`  — "shipped" | "in_flight" | "rejected" | "abandoned"
       - `prs`      — qualified PR ids, e.g. "proj/Azure/mod-a#pr-10"
       - `issues`   — qualified issue ids
       - `tickets`  — internal ticket refs parsed from PR/issue text (may be [])
     When `len(train["repos"]) > 1`, note "spans: {repo1}, {repo2}" in the header.
     Single-repo fallback: per-member trains from
     `view["members"][i]["bundle"]["trains"]`. -->

<!-- Project trains carry NO `tier`/`effort`/`root_issue`/flowchart of their own —
     those are single-repo projections. Order trains most-substantial first
     (rank by `len(train["prs"]) + len(train["commits"])`, break ties by
     `outcome` shipped > in_flight > rejected > abandoned). Lead with the
     cross-repo trains (`len(train["repos"]) > 1`).
     For a DEEP per-train flowchart + effort line + Phase 4b narrative, drop into the
     owning member's single-repo bundle: match the project train's local `pr-`/`issue-`
     ids to a train in `view["members"][i]["bundle"]["trains"]` (which DO carry `tier`,
     `significance`, `effort`) and that member's rendered
     `diagrams.train_flowcharts[...]`. The per-train narrative (summary + Proposed /
     Changed / Rejected / Shipped, each sourced) is authored by a narrator sub-agent over
     `link.py <member-bundle> --slice <train-id>` — see SKILL.md "Phase 4b". -->

### {train.id} — {short title}

<!-- spans: {", ".join(train["repos"])} — omit this line when only one repo -->

- **Repos:** {", ".join(train["repos"])} {"— cross-repo" when len > 1}
- **Kind / outcome:** {train["kind"]} / {train["outcome"]}
- **PRs:** {train["prs"]} *(qualified ids; strip the "{project}/" prefix for display)*
- **Issues:** {train["issues"]}
- **Commits:** {len(train["commits"])}
- **Tickets:** {", ".join(train["tickets"]) or "—"}
- **Evidence:** {e["url"] for e in train["evidence"]}

## Related work (cross-repo, ticket-linked)

<!-- Source: `view["related_work"]` — a list of `{ticket, train_ids}` objects.
     Each entry represents two or more project trains that share an internal
     ticket reference (Jira/ADO-style, e.g. ABC-1234) but are NOT connected by
     any GitHub closes/cross_ref edge. They are distinct-repo deliverables whose
     only shared signal is the internal ticket. This section makes that
     invisible coupling visible.
     Omit the section entirely when `view["related_work"]` is empty. -->

| Ticket | Trains |
|--------|--------|
| `{related_work[].ticket}` | {related_work[].train_ids joined with ", "} |

## Activity at a glance

Embed the rendered diagrams from `bundle.diagrams` (the `.mmd` file contents) as
fenced ```mermaid blocks:

```mermaid
{contents of diagrams.buckets_pie}
```

```mermaid
{contents of diagrams.timeline_gantt}
```

## Releases

For each release in `releases` (newest first): tag, name, date, and link.

- **{name}** (`{tag_name}`) — published {published_at}. [release]({url})

## CI/CD health

For each workflow in `workflow_stats`: total runs and success/failure split.

- **{workflow}** — {success}/{total} succeeded ({failure} failed, {cancelled} cancelled).

## In flight

For each ref in `buckets.in_flight`: title, number, link, and train id if present.

- [{title}]({url}) (#{number}){ — train `{train}`}

## Rejected / abandoned

For each ref in `buckets.rejected`: PRs closed without merge + issues closed as
not planned.

- [{title}]({url}) (#{number})

## Stalled, blocked & pile-ups

Where flow is stuck this period. **Flow signals, not blame** — the public digest
never attributes a stall/blocker to a person (that is the gated internal appendix).
Omit the whole section when there is no stalled / blocked / pile-up data.

**Stalled trains** — deep trains with `train.effort.stalled` true, longest
`effort.elapsed_days` first:

- train `{train.id}` — {short title} — stalled {train.effort.elapsed_days}d (cite the
  train's root issue / PR url)

**Blocked issues** — issues carrying `blocked_by` (and/or `blocking`), each cited:

- [{title}]({url}) (#{number}) — blocked by {blocked_by joined with ", " as #N}{; blocks
  {blocking joined as #N, …}}

{Embed the dependency graph when `diagrams.blocker_graph` is present:}

```mermaid
{diagrams.blocker_graph}
```

**Pile-ups** — open, high-activity issues (`issue["open_high_activity"]` true), each
cited:

- [{title}]({url}) (#{number})

## Next-release forecast

From `bundle["forecast"]` (forward-only; predicted-vs-landed loop is Phase 7).

**Next milestone:** {forecast.next_milestone} *(or "none identified")*

### Likely

For each candidate with `tier == "likely"` (score ≥ 5.0), score descending:

- [{title}]({url}) (#{number}){ — train `{train}`} — *{signals joined with " · "}*

### Possible

For each candidate with `tier == "possible"` (score ≥ 2.0):

- [{title}]({url}) (#{number}){ — train `{train}`} — *{signals joined with " · "}*

### Longshot

For each candidate with `tier == "longshot"` (score < 2.0):

- [{title}]({url}) (#{number}){ — train `{train}`} — *{signals joined with " · "}*

## Feature changes (add / drop / change)

The `feature_deltas` ledger as a table grouped by kind. Each row cites its
artifact and the commit/PR that changed it. `area` is now populated where the
path resolves to a code area (no longer null when a `code_graph` area covers it).

```mermaid
{contents of diagrams.deltas_bar}
```

| Kind | Subject | Name | Author | PR | Commit |
|------|---------|------|--------|----|--------|
| {kind} | {subject} | {name} | {author} | {pr or "—"} | [{commit:7}]({url}) |

### Symbol-level changes (Phase 3d)

For `feature_deltas` with `subject` of `symbol`/`comment`, show the actual change using
the bounded `before`/`after` and `detail` (`<lang> <subkind> <name>`). Omit when no
symbol deltas resolved. **Call out decision context:** comment deltas whose `detail`
contains the `todo` subkind (`detail` format is `<lang> <subkind> <name>`, e.g.
`bicep todo // TODO: …`) and comment **drops/changes** (a note or `@description`
replaced as a decision evolved) are strong focus signals for follow-on issues/PRs —
surface these first.

| Detail | Kind | Before | After | PR | Commit |
|--------|------|--------|-------|----|--------|
| {detail} | {kind} | `{before or "—"}` | `{after or "—"}` | {pr or "—"} | [{commit:7}]({url}) |

## Content lifecycle (built / changed / dropped)

From `artifacts`: examples, docs, and READMEs introduced, revised, or
removed/replaced within the window — *who* authored and *who* removed each, with
dates. Surfaces "we shipped an example in March and dropped it in May", which a
tip-only diff hides. (Inline code-symbols and comments are a later slice;
`code_area` lands with graphify.)

```mermaid
{contents of diagrams.content_timeline}
```

For each artifact in `artifacts` (status `removed`/`replaced` first):

- **{name}** ({kind}) — {status}. Lifecycle: {for each event} {event} by
  {author} on {date} ([{commit:7}]({ref.url})){end}.{ if replaced_by } Replaced by
  `{replaced_by}`.{end}

## Shipped by code area

Group `buckets.shipped` by each item's train `code_areas` (from `code_graph`).
For each area, list the shipped PRs/issues with their train link. Items with no
resolved area fall under "Unattributed".

### {area label} (`{area id}`)

- [{title}]({url}) (#{number}) — train `{train.id}`

## Module dependency graph / blast radius

Resolved inter-area dependencies from `code_graph.areas[].edges` (Bicep
`bicep build`→ARM; Terraform `terraform graph`), embedding `diagrams.module_graph`.
Direct dependencies carry pinned versions; cross-module edges show which areas a
change ripples into. When no edges resolve, `module_graph` renders a
`No module dependencies` placeholder (the diagram is always present).

## Cross-repo module dependency graph

<!-- Source: `view["module_edges"]` (each {src_repo, src_area, dst_repo, dst_area,
     version, transitive, cross_repo}) and the diagram
     `render.emit_project_module_graph(view["module_edges"])`. Lead with the
     cross-repo edges (cross_repo == true): a member's module depending on another
     member's published module. For "if member X changes, who is affected?", cite
     `spotlight dependents <owner/repo>` (its `dependents` list). Omit the whole
     section when `view["module_edges"]` is empty. -->

```mermaid
{render.emit_project_module_graph(view["module_edges"])}
```

| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo |
|---|---|---|---|
| `{src_repo}` · `{src_area}` | `{dst_repo}` · `{dst_area}` | {version or "—"} | {"yes" when cross_repo} |

## Module ownership

<!-- Multi-repo: source from `view["modules"]` and `view["people"]`.
     `view["modules"]` keys are `"{repo}::{area}"` — split on `::` for display.
     `view["people"]` logins are merged across all member repos (union by login).
     Top contributors: people whose `modules` list includes the area id. -->

From `code_owners` + `view["people"]`/`view["modules"]`: who owns and who touched
each module this window.

| Repo | Module | Owners (CODEOWNERS) | Top contributors | Commits | PRs | Files |
|------|--------|---------------------|------------------|---------|-----|-------|
| `{repo}` *(from key split on `::`)* | `{area}` | {code_owners[glob]} | {people whose modules include area} | {modules["{repo}::{area}"].commits} | {modules["{repo}::{area}"].prs} | {modules["{repo}::{area}"].files_changed} |

<!-- Per-file sections (Content lifecycle, Feature changes) render once per
     member repo from `view["members"][i]["bundle"]`. Activity at a glance,
     Releases, In flight, Rejected, Content lifecycle, and Feature changes all
     read from view["members"][i]["bundle"][...] — NOT a top-level view[...] key. -->

Embeds `diagrams.contributor_graph` (people ↔ code-area edges):

```mermaid
{contents of diagrams.contributor_graph}
```

## Issue kinds

The `kind` mix across the window's issues (feature / module-request / bug / idea /
question / docs / other), derived from native issue types → label facets →
template → heuristic.

```mermaid
{contents of diagrams.kind_breakdown}
```

## Contributors & community

Who moved the project this period (public — recognition, not blame). Rank contributors
by a footprint counted from the bundle: PRs authored (`prs[].author`), PRs reviewed
(`prs[].reviewers`), commits authored (`commits[].author`); most-active first. Show each
person's areas from `people[login].modules`. **Cite** each contributor's work — link a
representative authored/reviewed PR (`prs[].url`) so the footprint is grounded. Omit the
whole section when there is no `people` data.

| Contributor | Authored | Reviewed | Commits | Areas | Evidence |
|-------------|----------|----------|---------|-------|----------|
| `{login}` | {authored} | {reviewed} | {commits} | {", ".join(people[login].modules)} | [#{pr.number}]({pr.url}), … |

<!-- Automation: when any `people[login]` has `is_bot` true, list them under this subhead
     so the human view above isn't skewed by dependabot/CI bots. OMIT the subhead entirely
     when there are no bot contributors (no empty "Automation:" stanza). -->

**Automation:** {", ".join(bot logins)}

The people ↔ code-area relationships are shown by `diagrams.contributor_graph`, embedded
once under **Module ownership** above (do not repeat the Mermaid block here).

## Internal appendix — stall & blocker attribution

<!-- GATED: render this section ONLY when the operator explicitly requests an *internal*
     report (e.g. the request says "internal" / "with attribution"). The DEFAULT public
     digest OMITS this section entirely. It attributes the flow signals (from the
     "Stalled, blocked & pile-ups" section) to people, using existing bundle data only.
     Factual association, never judgmental. EACH line cites its own url. -->

**Stalled trains** — attribute to `effort.participants` / reviewers + the owning PR/issue
authors:

- {login} — stalled train `{train.id}` ({effort.elapsed_days}d) — [{ref}]({train root issue/PR url})

**Blocked issues** — attribute to the blocker/blocked issue authors:

- {login} — blocked issue #{number} — [link]({issue.url})
