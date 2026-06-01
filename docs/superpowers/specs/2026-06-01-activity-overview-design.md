# activity-overview skill — design

**Date:** 2026-06-01
**Status:** Approved design (pre-implementation)
**Author:** brainstormed via superpowers

## Purpose

A Claude Code skill that produces a time-boxed engineering activity report for a
GitHub repository. The user asks Claude to "run the activity-overview skill for
`<project>` from `<fromDate>` to `<toDate>`", and the skill produces a Markdown
report summarizing what shipped, what broke, what changed in infra, what design
decisions were made, what the community call surfaced, and what risks remain open.

This replaces a naive "Claude reads the whole repo and guesses" approach with a
deterministic data fetch plus an LLM-authored narrative.

### Host vs. target

This repository (`damianflynn/experiments`) is **only the host** for the skill —
its existing content is archive-only and is **never analyzed**. The skill is built
and committed here but always runs against **external target repos**. There are
**three target projects** in regular use; some of them hold a monthly/quarterly
community YouTube call whose transcript is an additional context source.

## Core principle

**Script = facts. Claude = narrative.**

- A deterministic Python script (`fetch_activity.py`) is the *only* component that
  touches the network. It pulls commits, PRs, and issues from the GitHub REST API
  and writes a single JSON "activity bundle."
- `SKILL.md` instructs Claude to run the fetcher, read the bundle, optionally read a
  community-call transcript, and write a Markdown report using a fixed template.
  Claude does only the judgment work (grouping, summarizing, risk-spotting) — never
  the data gathering.

This keeps token cost low and report data reproducible.

## Non-goals (YAGNI)

- No dependency on third-party skills (`repo-analyzer`, `github-issue-analyzer`).
  All area-grouping and issue-categorization logic is built in.
- No `gh` CLI dependency. Auth is via `GITHUB_TOKEN` only.
- No YouTube network access / transcript auto-fetch. The transcript is **user-provided**
  as a local file.
- No multi-repo aggregation in v1 (single target repo per run).
- No HTML/PDF output. Markdown only.

## Components

### 1. `fetch_activity.py` (deterministic fetcher)

- **Deps:** Python 3 stdlib only (`urllib`, `json`, `argparse`, `datetime`). No pip install.
- **Auth:** reads `GITHUB_TOKEN`, falling back to `GH_TOKEN`. Exits with a clear
  error message if neither is set.
- **CLI:**
  ```
  python fetch_activity.py --owner OWNER --repo REPO \
      --from YYYY-MM-DD --to YYYY-MM-DD \
      [--branches main,develop] [--include-docs] [--out PATH]
  ```
  (When a project config is used, `SKILL.md` resolves these args from the config —
  see component 5.)
- **Behaviour / windowing:**
  - **Commits:** commits on the listed branches (default: repo default branch) with
    author/commit date within `[from, to]`. Endpoints: `GET /repos/{o}/{r}/commits?since&until&sha`.
  - **PRs:** pull requests **merged** within `[from, to]`. List via
    `GET /repos/{o}/{r}/pulls?state=closed&sort=updated&direction=desc` (paginated),
    filter on `merged_at` in window. For each: title, number, body, labels,
    reviewers, merged_at, and changed files (`GET .../pulls/{n}/files`), plus
    review-comment bodies (`GET .../pulls/{n}/comments`).
  - **Issues:** issues **closed** within `[from, to]` (`GET /repos/{o}/{r}/issues?state=closed&since=`,
    excluding PRs via the `pull_request` key), plus **still-open** issues with high
    recent activity (comments/updates in window).
  - **Pagination:** follow `Link` rel="next" headers; respect `--from`/`--to` to stop early where possible.
  - **Rate limiting:** on HTTP 403 with `X-RateLimit-Remaining: 0`, sleep until
    `X-RateLimit-Reset`, then retry (bounded retries).
- **Derived fields (computed in-script, deterministic):**
  - `modules`: map of top-level directory (first path segment of each changed file)
    → `{ commits, prs, files_changed }` counts.
  - `docsRefs` (only when `--include-docs`): changed files whose path matches
    `docs/`, `adr/`, `adrs/`, `decisions/`, or `*ADR*.md` / `*adr*.md`, plus any
    doc-like paths referenced in PR bodies (regex for `docs/...`, `*.md`).
- **Output:** writes `workspace/activity-{from}-{to}.json` (override with `--out`).
  Bundle schema:
  ```json
  {
    "meta": { "owner", "repo", "from", "to", "branches", "generated_at" },
    "commits": [ { "sha", "message", "author", "date", "files": [paths] } ],
    "prs": [ { "number", "title", "body", "labels": [], "reviewers": [],
               "merged_at", "files": [paths], "review_comments": [str], "url" } ],
    "issues": [ { "number", "title", "labels": [], "state",
                  "closed_at", "url", "open_high_activity": bool } ],
    "modules": { "<dir>": { "commits", "prs", "files_changed" } },
    "docsRefs": [ { "path", "source": "changed|referenced", "pr": number|null } ]
  }
  ```
- **Scope note:** the fetcher does **not** read the transcript. The transcript is a
  Claude-side input (see component 2/3), keeping the network/parse layer focused.

### 2. `SKILL.md` (procedure + narrative instructions)

Frontmatter:
```yaml
name: activity-overview
description: Use when you need a time-boxed engineering activity report for a GitHub
  repo — summarizing shipped features, bug fixes, infra changes, design decisions,
  community-call highlights, and open risks over a date range.
```

Procedure Claude follows:
1. Resolve the target: either a `--project <name>` looked up in `projects.json`, or
   explicit `owner`/`repo`/options from the user request.
2. Resolve `from`/`to` and the optional transcript path (from config or the request).
3. Verify `GITHUB_TOKEN` (or `GH_TOKEN`) is set; if not, ask the user to provide one.
4. Run `fetch_activity.py` with the resolved parameters.
5. Read the produced JSON bundle.
6. If a transcript file is present, read it and extract community-call highlights.
   If absent, skip the call section gracefully (note "no call this period").
7. Write `workspace/activity-report-{from}-{to}.md` following `report-template.md`,
   weaving call context into the relevant sections.
8. Report the output path to the user.

### 3. Community-call transcript handling

- **Source:** user-provided local file (`.txt`, `.vtt`, `.srt`, or `.md`). No network.
- **Location:** resolved from `projects.json` (`transcript` field) or passed explicitly;
  conventionally dropped in `workspace/`.
- **Use:** Claude summarizes it into a dedicated **"Community call highlights"** report
  section AND weaves relevant points into executive summary, design decisions, and
  open risks / next steps.
- **Optional:** if no transcript is provided for the period, the section states that
  no call occurred and the rest of the report is unaffected.

### 4. `report-template.md` (fixed report shape)

Sections, in order:
1. **Executive summary** — 3–5 bullets: major features, key risks, notable infra work
   (informed by call context where relevant).
2. **Shipped features** — grouped by area (from `modules`); each item links its PR(s)
   and related issues, summarizes the behaviour change, notes follow-ups.
3. **Bugs & reliability** — issues/PRs labelled `bug`/reliability; fixed vs still-open;
   recurring themes.
4. **Infrastructure & tooling** — CI/CD, IaC (Terraform/Bicep/ARM), PowerShell,
   dependency changes (inferred from area + labels).
5. **Design decisions & docs** — ADRs / design docs touched or referenced (`docsRefs`),
   with a 1–2 sentence rationale each.
6. **Community call highlights** — key topics, decisions, asks, and follow-ups from the
   transcript. Omitted/short-circuited when no transcript is provided.
7. **Open risks & next steps** — from still-open high-activity issues + PR review
   comments flagged as concerns + call follow-ups.

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
        "transcript": "workspace/<name>-call-{period}.txt"
      }
    }
  }
  ```
- **Resolution order:** `--config PATH` → `./projects.json` (cwd) → skill-dir
  `projects.json`. If none found or `--project` not given, fall back to explicit
  `owner`/`repo` args.
- **Distribution:** the skill ships `projects.example.json` (placeholder values, no
  real project names). The user fills in a real `projects.json`; whether that real
  file is committed or kept local is the user's choice (the example is what ships in
  the portable skill).

### 6. `test_fetch_activity.py` (offline tests)

- Tests the deterministic transforms (module grouping, doc-ref detection, window
  filtering, bundle assembly, config resolution) against a **recorded API fixture**
  (committed JSON), so they run with no network and no token.
- Network/HTTP layer is isolated behind a small `_get(url)` function that tests
  monkeypatch to return fixture pages.

## Layout (committed, portable)

```
.claude/skills/activity-overview/
  SKILL.md
  fetch_activity.py
  report-template.md
  projects.example.json
  test_fetch_activity.py
  fixtures/
    sample_api.json          # recorded responses for offline tests
```

Self-contained: copying the folder into `~/.claude/skills/` makes the skill usable
in any repo, with no other setup beyond `GITHUB_TOKEN` and (optionally) a
`projects.json` and a transcript file.

## Testing strategy

- **Unit (offline):** `test_fetch_activity.py` covers the pure transforms + config
  resolution against the fixture. Must pass with no network/token.
- **Smoke (manual, optional):** run the fetcher against one of the real target repos
  for a real date range with a token, confirm the bundle is well-formed and a report
  renders (with and without a transcript).

## Error handling

- Missing token → fail fast with an actionable message.
- 404 (repo not found / no access) → clear error naming the repo.
- Unknown `--project` name → list the available project names from config.
- Missing transcript file (when one was expected) → warn, render report without the
  call section.
- Empty window (no activity) → still produce a valid bundle and a report stating
  "no activity in this period."
- Rate limit → sleep-until-reset with bounded retries, then fail with the reset time.

## Open questions

- The three real project coordinates (owner/repo, branches, doc layout, which have
  calls) are not yet captured. The skill ships with `projects.example.json`; the user
  can supply real values into `projects.json` after the skill is built (or hand them
  to Claude to pre-populate).
