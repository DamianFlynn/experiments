# {repo} — Activity Digest ({from} → {to})

## Executive summary

{N} merged PRs across {M} decision trains. {shipped_count} items shipped.

## Shipped this period

For each ref in `buckets.shipped`, one line: the PR/issue title, number, and link.

- [{title}]({url}) (#{number})

## Decision trains

For each train in `trains`:

### {train.id} — {issue or PR title}

- **Root issue:** #{root_issue} (or "none — PR-anchored")
- **PRs:** {prs}
- **Commits:** {commit count}
- **Outcome:** {outcome}
- **Evidence:** {evidence urls}

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

## Next up (forecast candidates)

For each ref in `buckets.next_candidates`: open items on the next milestone or
flagged high-priority — the basis for the next-release forecast.

- [{title}]({url}) (#{number}){ — train `{train}`}

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
