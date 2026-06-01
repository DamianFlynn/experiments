# activity-overview skill — design

**Date:** 2026-06-01
**Status:** Approved design (pre-implementation)
**Author:** brainstormed via superpowers

## Purpose

A Claude Code skill that produces a time-boxed engineering activity report for a
GitHub repository. The user asks Claude to "run the activity-overview skill for
`owner/repo` from `<fromDate>` to `<toDate>`", and the skill produces a Markdown
report summarizing what shipped, what broke, what changed in infra, what design
decisions were made, and what risks remain open.

This replaces a naive "Claude reads the whole repo and guesses" approach with a
deterministic data fetch plus an LLM-authored narrative.

## Core principle

**Script = facts. Claude = narrative.**

- A deterministic Python script (`fetch_activity.py`) is the *only* component that
  touches the network. It pulls commits, PRs, and issues from the GitHub REST API
  and writes a single JSON "activity bundle."
- `SKILL.md` instructs Claude to run the fetcher, read the bundle, and write a
  Markdown report using a fixed template. Claude does only the judgment work
  (grouping, summarizing, risk-spotting) — never the data gathering.

This keeps token cost low and report data reproducible.

## Non-goals (YAGNI)

- No dependency on third-party skills (`repo-analyzer`, `github-issue-analyzer`).
  All area-grouping and issue-categorization logic is built in.
- No `gh` CLI dependency. Auth is via `GITHUB_TOKEN` only.
- No multi-repo aggregation in v1 (single repo per run).
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

### 2. `SKILL.md` (procedure + narrative instructions)

Frontmatter:
```yaml
name: activity-overview
description: Use when you need a time-boxed engineering activity report for a GitHub
  repo — summarizing shipped features, bug fixes, infra changes, design decisions,
  and open risks over a date range.
```

Procedure Claude follows:
1. Resolve `owner`, `repo`, `from`, `to`, and options from the user request.
2. Verify `GITHUB_TOKEN` (or `GH_TOKEN`) is set; if not, ask the user to provide one.
3. Run `fetch_activity.py` with the parameters.
4. Read the produced JSON bundle.
5. Write `workspace/activity-report-{from}-{to}.md` following `report-template.md`.
6. Report the output path to the user.

### 3. `report-template.md` (fixed report shape)

Sections, in order:
1. **Executive summary** — 3–5 bullets: major features, key risks, notable infra work.
2. **Shipped features** — grouped by area (from `modules`); each item links its PR(s)
   and related issues, summarizes the behaviour change, notes follow-ups.
3. **Bugs & reliability** — issues/PRs labelled `bug`/reliability; fixed vs still-open;
   recurring themes.
4. **Infrastructure & tooling** — CI/CD, IaC (Terraform/Bicep/ARM), PowerShell,
   dependency changes (inferred from area + labels).
5. **Design decisions & docs** — ADRs / design docs touched or referenced (`docsRefs`),
   with a 1–2 sentence rationale each.
6. **Open risks & next steps** — from still-open high-activity issues + PR review
   comments flagged as concerns.

### 4. `test_fetch_activity.py` (offline tests)

- Tests the deterministic transforms (module grouping, doc-ref detection, window
  filtering, bundle assembly) against a **recorded API fixture** (committed JSON),
  so they run with no network and no token.
- Network/HTTP layer is isolated behind a small `_get(url)` function that tests
  monkeypatch to return fixture pages.

## Layout (committed, portable)

```
.claude/skills/activity-overview/
  SKILL.md
  fetch_activity.py
  report-template.md
  test_fetch_activity.py
  fixtures/
    sample_api.json          # recorded responses for offline tests
```

Self-contained: copying the folder into `~/.claude/skills/` makes the skill usable
in any repo, with no other setup beyond `GITHUB_TOKEN`.

## Testing strategy

- **Unit (offline):** `test_fetch_activity.py` covers the pure transforms against the
  fixture. Must pass with no network/token.
- **Smoke (manual, optional):** run the fetcher against `damianflynn/experiments`
  for a real date range with a token, confirm the bundle is well-formed and a report
  renders.

## Error handling

- Missing token → fail fast with an actionable message.
- 404 (repo not found / no access) → clear error naming the repo.
- Empty window (no activity) → still produce a valid bundle and a report stating
  "no activity in this period."
- Rate limit → sleep-until-reset with bounded retries, then fail with the reset time.

## Open questions

None — all design decisions resolved during brainstorming.
