# activity-overview skill — design

**Date:** 2026-06-01
**Status:** Approved design (pre-implementation)
**Author:** brainstormed via superpowers

## Purpose

A Claude Code skill that produces a time-boxed **engineering activity + sprint/release
digest** for a GitHub repository. The user asks Claude to "run the activity-overview
skill for `<project>` over `<period>`", and the skill produces a Markdown report that
covers: what shipped, releases published, CI/CD health, what's in flight, what was
rejected/abandoned, design decisions, community-call highlights, a **next-release
forecast**, and open risks — framed around **previous / current / next** sprint and
release.

This replaces a naive "Claude reads the whole repo and guesses" approach with a
deterministic data fetch (REST + GraphQL) plus an LLM-authored narrative.

### Host vs. target

This repository (`damianflynn/experiments`) is **only the host** for the skill — its
existing content is archive-only and is **never analyzed**. The skill is built and
committed here but always runs against **external target repos**. There are **three
target projects** in regular use; some hold a monthly/quarterly community YouTube call
whose transcript is an additional context source, and the projects use GitHub
milestones (releases) and/or Projects v2 boards (sprints) for planning.

## Core principle

**Script = facts. Claude = narrative.**

- A deterministic Python script (`fetch_activity.py`) is the *only* component that
  touches the network. It pulls commits, PRs, issues, workflow runs, releases,
  milestones, and Projects v2 board state from the GitHub **REST + GraphQL** APIs and
  writes a single JSON "activity bundle," including deterministically-computed buckets
  (shipped / in-flight / rejected / next-candidates).
- `SKILL.md` instructs Claude to run the fetcher, read the bundle, optionally read a
  community-call transcript, and write a Markdown report using a fixed template —
  including a forecast *narrative* over the script-selected candidate items.
- Claude does only the judgment work (grouping, summarizing, risk-spotting,
  forecasting prose). It never gathers data or invents the candidate buckets.

This keeps token cost low and report data reproducible.

## Non-goals (YAGNI)

- No dependency on third-party skills (`repo-analyzer`, `github-issue-analyzer`,
  `github-summary`). They are useful references but the skill is self-contained.
- No `gh` CLI dependency. Auth is via `GITHUB_TOKEN` only.
- No YouTube network access / transcript auto-fetch. The transcript is **user-provided**
  as a local file.
- No multi-repo aggregation in v1 (single target repo per run; one optional linked
  Projects v2 board).
- No write actions to GitHub. Read-only.
- No HTML/PDF output, and no automated scheduling/delivery (cron, Slack) in v1 —
  invocation is manual / on-demand. (Noted as a future add.)

## Sprint & release modeling

The digest is framed around **previous / current / next**. Two independent planning
mechanisms are supported; either or both may be present:

- **Releases → GitHub milestones.** A milestone represents a release. Ordering is by
  `due_on` (fallback: creation order). The **current** release is the user-named
  milestone (`--milestone`) or the earliest open milestone with `due_on >= ref-date`;
  **previous** = most recent closed milestone; **next** = the open milestone after
  current.
- **Sprints → Projects v2 iteration field.** Each iteration has a start date + duration.
  Given a **reference date** (`--ref-date`, default = `--to`), **current** = the
  iteration containing the ref date; **previous** / **next** are the adjacent ones.

**Buckets** (computed deterministically in the fetcher; refs only, by item number):
- **shipped** — PRs merged in window + issues closed as completed in window.
- **in_flight** — open PRs/issues assigned to the current sprint (iteration) or current
  milestone.
- **rejected/abandoned** — PRs closed **without merge** in window + issues closed as
  `not_planned` (wontfix) in window.
- **next_candidates** — open PRs/issues assigned to the next milestone or next
  iteration, plus open items labelled high-priority. Feeds the forecast narrative.

## Components

### 1. `fetch_activity.py` (deterministic fetcher — REST + GraphQL)

- **Deps:** Python 3 stdlib only (`urllib`, `json`, `argparse`, `datetime`). GraphQL is
  a plain `POST /graphql` with a JSON body via `urllib` — no pip install.
- **Auth:** reads `GITHUB_TOKEN`, falling back to `GH_TOKEN`. Exits with a clear error
  if neither is set. (Projects v2 needs a token with `read:project` scope.)
- **CLI:**
  ```
  python fetch_activity.py --owner OWNER --repo REPO \
      --from YYYY-MM-DD --to YYYY-MM-DD \
      [--branches main,develop] \
      [--include-docs] [--include-workflows] [--include-releases] \
      [--include-projects] [--project-number N] [--project-owner-type org|user] \
      [--status-field Status] [--iteration-field Sprint] \
      [--milestone "vX.Y"] [--ref-date YYYY-MM-DD] [--out PATH]
  ```
  (`--include-workflows`/`--include-releases` default **on**. `--include-projects`
  activates when a project number is provided via flag or config. When a project config
  is used, `SKILL.md` resolves these args from the config — see component 6.)
- **REST fetches / windowing:**
  - **Commits:** on the listed branches (default: default branch), author/commit date in
    `[from, to]`. `GET /repos/{o}/{r}/commits?since&until&sha`.
  - **PRs:** all closed PRs touched in range via
    `GET /repos/{o}/{r}/pulls?state=closed&sort=updated&direction=desc` (paginated).
    Split into **merged in window** (`merged_at` in range) and **closed-without-merge in
    window** (`closed_at` in range, `merged_at` null). Plus **open** PRs
    (`state=open`) for in-flight/next buckets. For each: title, number, body, labels,
    reviewers, milestone, merged flag, merged_at/closed_at, changed files
    (`.../pulls/{n}/files`), review-comment bodies (`.../pulls/{n}/comments`).
  - **Issues:** closed in window (`state=closed&since=`, excluding PRs via
    `pull_request` key) with `state_reason` (`completed`|`not_planned`); plus **open**
    issues (for in-flight/next/high-activity). Capture milestone + labels.
  - **Workflow runs** (`--include-workflows`): runs created in `[from, to]` via
    `GET /repos/{o}/{r}/actions/runs?created={from}..{to}` (paginated). Capture `name`,
    `conclusion`, `status`, `event`, `head_branch`, `created_at`, `html_url`.
  - **Releases** (`--include-releases`): `GET /repos/{o}/{r}/releases` filtered on
    `published_at` in window; capture `tag_name`, `name`, `published_at`, `prerelease`, url.
  - **Milestones:** `GET /repos/{o}/{r}/milestones?state=all`; capture `title`, `number`,
    `state`, `due_on`, `open_issues`, `closed_issues`, url. Used for release-train modeling.
  - **Pagination:** follow `Link` rel="next"; stop early using `from`/`to` where possible.
  - **Rate limiting:** on HTTP 403 with `X-RateLimit-Remaining: 0`, sleep until
    `X-RateLimit-Reset`, bounded retries.
- **GraphQL fetch (`--include-projects`):**
  - Query `organization|user(login:owner).projectV2(number:N)` for: project title; the
    **iteration field** (its iterations with `title`, `startDate`, `duration`); the
    **status (single-select) field** options; and `items` (paginated) each resolving to
    its linked issue/PR (`number`, `title`, `state`, `merged`) plus that item's `Status`
    and `Iteration` field values.
  - Field names are configurable (`--status-field`, `--iteration-field`) to match the
    board.
- **Derived fields (deterministic):**
  - `modules`: top-level dir of each changed path → `{ commits, prs, files_changed }`.
  - `workflow_stats` (`--include-workflows`): workflow name →
    `{ total, success, failure, cancelled, other }`.
  - `docsRefs` (`--include-docs`): changed files matching `docs/`, `adr/`, `adrs/`,
    `decisions/`, `*ADR*.md`/`*adr*.md`, plus doc-like paths referenced in PR bodies.
  - `release_train`: `{ previous, current, next }` milestone refs (per the model above).
  - `sprints`: `{ previous, current, next, all: [...] }` iteration refs (when projects on).
  - `buckets`: `{ shipped, in_flight, rejected, next_candidates }` — arrays of item refs
    `{ type, number, url }` (see Sprint & release modeling).
- **Output:** writes `workspace/activity-{from}-{to}.json` (override `--out`). Bundle:
  ```json
  {
    "meta": { "owner", "repo", "from", "to", "branches", "ref_date", "generated_at" },
    "commits": [ { "sha", "message", "author", "date", "files": [paths] } ],
    "prs": [ { "number", "title", "body", "labels": [], "reviewers": [], "milestone",
               "merged": bool, "merged_at", "closed_at", "files": [paths],
               "review_comments": [str], "url" } ],
    "issues": [ { "number", "title", "labels": [], "state", "state_reason",
                  "milestone", "closed_at", "url", "open_high_activity": bool } ],
    "workflows": [ { "name", "conclusion", "status", "event", "head_branch", "created_at", "url" } ],
    "releases": [ { "tag_name", "name", "published_at", "prerelease": bool, "url" } ],
    "milestones": [ { "title", "number", "state", "due_on", "open_issues", "closed_issues", "url" } ],
    "project": { "number", "title",
                 "iterations": [ { "title", "start", "end" } ],
                 "items": [ { "type", "number", "title", "state", "merged",
                              "status", "iteration", "url" } ] },
    "modules": { "<dir>": { "commits", "prs", "files_changed" } },
    "workflow_stats": { "<workflow>": { "total", "success", "failure", "cancelled", "other" } },
    "docsRefs": [ { "path", "source": "changed|referenced", "pr": number|null } ],
    "release_train": { "previous": {...}|null, "current": {...}|null, "next": {...}|null },
    "sprints": { "previous": {...}|null, "current": {...}|null, "next": {...}|null, "all": [...] },
    "buckets": { "shipped": [refs], "in_flight": [refs], "rejected": [refs], "next_candidates": [refs] }
  }
  ```
- **Scope note:** the fetcher does **not** read the transcript (Claude-side input) and
  does **not** write forecast prose (Claude-side narrative). It only produces facts +
  candidate buckets.

### 2. `SKILL.md` (procedure + narrative instructions)

Frontmatter:
```yaml
name: activity-overview
description: Use when you need a time-boxed engineering + sprint/release digest for a
  GitHub repo — shipped work, releases, CI/CD health, in-flight and abandoned items,
  design decisions, community-call highlights, a next-release forecast, and open risks,
  framed as previous/current/next sprint and release.
```

Procedure Claude follows:
1. Resolve the target: `--project <name>` from `projects.json`, or explicit
   `owner`/`repo`/options from the request (incl. project-board settings if any).
2. Resolve `from`/`to`, `ref-date`, milestone, and optional transcript path.
3. Verify `GITHUB_TOKEN`/`GH_TOKEN` is set (needs `read:project` for boards); if not, ask.
4. Run `fetch_activity.py` with resolved parameters.
5. Read the JSON bundle (facts + buckets + release_train + sprints).
6. If a transcript is present, read it and extract community-call highlights; else skip.
7. Write `workspace/activity-report-{from}-{to}.md` per `report-template.md`, including
   a **forecast narrative** over `buckets.next_candidates` (likelihood / slippage risk),
   weaving call context into relevant sections.
8. Report the output path to the user.

### 3. Community-call transcript handling

- **Source:** user-provided local file (`.txt`, `.vtt`, `.srt`, `.md`). No network.
- **Location:** from `projects.json` (`transcript`) or passed explicitly; conventionally
  in `workspace/`.
- **Use:** summarized into a dedicated **"Community call highlights"** section AND woven
  into executive summary, design decisions, forecast, and open risks.
- **Optional:** absent transcript → section notes no call; rest of report unaffected.

### 4. `report-template.md` (fixed report shape)

Sections, in order (sections gated on data are omitted gracefully when absent):
1. **Executive summary** — sprint goals vs. outcomes; 3–5 bullets covering major
   features, releases, CI health, key risks (informed by call + board context).
2. **Release train context** — previous / current / next milestone (and current sprint
   iteration window): dates, completion %, theme.
3. **Shipped this period** — merged PRs + completed issues grouped by area (`modules`);
   each links PR(s)/issues, summarizes the change, notes follow-ups.
   - **Releases** (subsection) — versions published in window (tag, date, link;
     prereleases flagged).
4. **In flight** — open items in current sprint/milestone (`buckets.in_flight`) with
   board status; flag items at risk of slipping.
5. **Rejected / abandoned** — PRs closed without merge + issues closed `not_planned`
   (`buckets.rejected`), with a one-line "why" where evident.
6. **CI/CD overview** — per-workflow success/fail counts (`workflow_stats`) + notable
   failed runs with links. (`--include-workflows`.)
7. **Bugs & reliability** — `bug`/reliability-labelled items; fixed vs still-open; themes.
8. **Infrastructure & tooling** — IaC (Terraform/Bicep/ARM), PowerShell, dependency
   changes (inferred from area + labels).
9. **Design decisions & docs** — ADRs/design docs touched or referenced (`docsRefs`),
   1–2 sentence rationale each.
10. **Community call highlights** — topics, decisions, asks, follow-ups (when transcript).
11. **Next-release forecast** — over `buckets.next_candidates`: what's likely to land in
    the next release/sprint, confidence, and slippage risks. Claude's judgment over the
    script-selected candidates.
12. **Open risks & next steps** — still-open high-activity issues + flagged PR review
    comments + call follow-ups + at-risk in-flight items.

### 5. `projects.json` (optional per-project config)

- **Purpose:** avoid re-entering details for the three target projects.
- **Schema:**
  ```json
  {
    "projects": {
      "<short-name>": {
        "owner": "string",
        "repo": "string",
        "branches": ["main"],
        "include_docs": true,
        "include_workflows": true,
        "include_releases": true,
        "transcript": "workspace/<name>-call-{period}.txt",
        "project_v2": {
          "owner_type": "org",
          "number": 0,
          "status_field": "Status",
          "iteration_field": "Sprint"
        }
      }
    }
  }
  ```
  (`project_v2` is optional — omit it for projects that don't use a board; the digest
  then relies on milestones + dates only.)
- **Resolution order:** `--config PATH` → `./projects.json` (cwd) → skill-dir
  `projects.json`. If none found or `--project` not given, fall back to explicit args.
- **Distribution:** ships `projects.example.json` (placeholders, no real names). User
  fills a real `projects.json` (commit-or-local is their choice).

### 6. `commands/activity.md` (slash-command entrypoint)

- Thin wrapper so the skill triggers as
  `/activity <owner/repo|project> <fromDate> <toDate> [options]`.
- Instructs Claude to invoke `activity-overview` with the parsed args — all logic stays
  in `SKILL.md`/`fetch_activity.py`.
- **Install note:** Claude Code discovers slash commands under `.claude/commands/`
  (project) or `~/.claude/commands/` (user). Ships at `commands/activity.md`; install
  copies/symlinks it to the commands dir. Documented in `REFERENCE.md`.

### 7. `test_fetch_activity.py` (offline tests)

- Tests the deterministic transforms against **recorded REST + GraphQL fixtures**
  (committed JSON): module grouping, doc-ref detection, window/merge filtering,
  workflow-stats aggregation, release filtering, milestone release-train resolution
  (previous/current/next), iteration sprint resolution, and bucket assignment
  (shipped / in_flight / rejected / next_candidates). Runs with no network/token.
- HTTP/GraphQL layer isolated behind small `_get(url)` / `_graphql(query, vars)`
  functions that tests monkeypatch to return fixture pages.

## Layout (committed, portable)

```
.claude/skills/activity-overview/
  SKILL.md
  fetch_activity.py
  report-template.md
  projects.example.json
  REFERENCE.md               # examples + install (incl. slash command) + troubleshooting
  commands/
    activity.md              # /activity slash-command wrapper
  test_fetch_activity.py
  fixtures/
    rest_sample.json         # recorded REST responses for offline tests
    graphql_sample.json      # recorded Projects v2 GraphQL response
```

Self-contained: copying the folder into `~/.claude/skills/` makes the skill usable in
any repo, with no setup beyond `GITHUB_TOKEN` (incl. `read:project`) and optionally a
`projects.json` + transcript.

## Implementation phasing (for the plan)

This is a large skill; the implementation plan should stage it so each phase is
independently testable:
1. **REST core** — commits/PRs/issues, modules, date-window report (template skeleton).
2. **CI + releases + milestones** — workflow stats, releases, milestone release-train.
3. **Buckets** — shipped / in_flight / rejected from REST; report sections 3–5, 12.
4. **Projects v2 (GraphQL)** — board iterations + status, sprint resolution, in-flight by
   board.
5. **Forecast + transcript + slash command** — next_candidates forecast narrative,
   community-call section, `/activity`.

## Testing strategy

- **Unit (offline):** `test_fetch_activity.py` covers all deterministic transforms +
  bucket/release-train/sprint logic + config resolution against committed fixtures.
  Must pass with no network/token.
- **Smoke (manual, optional):** run against one real target repo+board for a real window
  with a token; confirm bundle well-formed and report renders (with/without transcript,
  with/without board).

## Error handling

- Missing token → fail fast with actionable message; if `--include-projects` and token
  lacks `read:project`, name the missing scope.
- 404 (repo/project not found / no access) → clear error naming the resource.
- GraphQL errors / project number not found / missing iteration|status field → warn and
  degrade gracefully to milestone+date modeling (board sections omitted), not a hard fail.
- Unknown `--project` name → list available project names from config.
- Missing transcript (when expected) → warn, render without the call section.
- Empty window / empty buckets → valid bundle + report stating "no activity/none".
- Rate limit → sleep-until-reset with bounded retries, then fail with the reset time.

## Open questions

- The three real project coordinates (owner/repo, branches, doc layout, which have
  calls, Projects v2 numbers + field names) are not yet captured. The skill ships
  `projects.example.json`; the user supplies real values into `projects.json` after the
  skill is built (or hands them to Claude to pre-populate).
