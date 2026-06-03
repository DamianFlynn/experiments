# {repo} — Activity Digest ({from} → {to})

## Executive summary

{N} merged PRs across {M} decision trains. {shipped_count} items shipped.

## Shipped this period

For each ref in `buckets.shipped`, one line: the PR/issue title, number, and link.

- [{title}]({url}) (#{number})

## Decision trains

DEEP trains (`tier == "deep"`) get a full sub-section with flowchart + effort line.
MENTION trains (`tier == "mention"`) are one-liners at the end of the section.

### {train.id} — {issue or PR title}  *(DEEP)*

```mermaid
{contents of diagrams.train_flowcharts[train.id]}
```

*Effort: landed in {effort.elapsed_days} days · {effort.reviewers} reviewer(s) · {effort.participants} contributor(s)*
*(or "open N days" when effort.merged_at is null; append "— stalled" when effort.stalled is true)*

<!-- narrative: {train.id} -->

- **Root issue:** #{root_issue} (or "none — PR-anchored")
- **PRs:** {prs}
- **Commits:** {commit count}
- **Outcome:** {outcome}
- **Evidence:** {evidence urls}

---

*MENTION trains (one line each):*

- `{train.id}` — {title} — {outcome} ({PR count} PR(s))

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

## Module ownership

From `code_owners` + `people.modules`/`modules`: who owns and who touched each
module this window.

| Module | Owners (CODEOWNERS) | Top contributors | Commits | PRs | Files |
|--------|---------------------|------------------|---------|-----|-------|
| `{area}` | {code_owners[glob]} | {people whose modules include area} | {modules[area].commits} | {modules[area].prs} | {modules[area].files_changed} |

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
