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
