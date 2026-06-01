# activity-overview skill — design

**Date:** 2026-06-01
**Status:** Approved design (pre-implementation) — rev 4 (bundle-as-product + provenance)
**Author:** brainstormed via superpowers

## Purpose

A Claude Code skill that produces a time-boxed **engineering activity + sprint/release
digest** for a GitHub repository. The user asks Claude to "run the activity-overview
skill for `<project>` over `<period>`", and the skill produces a Markdown report that
covers: what shipped, releases published, CI/CD health, what's in flight, what was
rejected/abandoned, **the decision trains** behind the work, design decisions,
community-call highlights, a **next-release forecast**, and open risks — framed around
**previous / current / next** sprint and release.

This replaces a naive "Claude reads the whole repo and guesses" approach with a
deterministic **gather** step (local clone + REST/GraphQL + optional code graph) that
writes a persisted bundle, followed by an **offline analysis** step that traces decision
trains and authors the narrative.

### Host vs. target

This repository (`damianflynn/experiments`) is **only the host** for the skill — its
existing content is archive-only and is **never analyzed**. The skill is built and
committed here but always runs against **external target repos** (the lead validation
target is the large **AVM-Bicep** repo). There are **three target projects** in regular
use; some hold a monthly/quarterly community YouTube call whose transcript is an
additional context source, and the projects use GitHub milestones (releases) and/or
Projects v2 boards (sprints) for planning.

## Core principle

**Gather is deterministic and decoupled. Analysis is the model's judgment.**

The skill is a four-layer pipeline with a **persisted bundle as the seam** between the
online and offline halves:

```
  ┌─────────── online, deterministic ───────────┐   ┌──────── offline ────────┐
  Acquire ──▶ Bundle (on-disk) ──▶ Link ──▶ Analyze (sub-agents) ──▶ Synthesize
```

1. **Acquire** (the *only* layer that touches the network) — shallow/partial **local
   clone** of the target repo bounded to the window (commit tree + diffs, free, no rate
   limits), plus a REST/GraphQL pull of the **social layer** (issues, PRs, comments,
   reviews, timeline links, workflow runs, releases, milestones, Projects v2), plus an
   optional local **graphify** code-graph pass. Writes one self-describing bundle.
2. **Bundle** — a portable, inspectable, diffable on-disk artifact. Gather once; analyze
   many times without re-fetching or re-touching GitHub. The bundle is the audit record
   of exactly which facts a report was built from.
3. **Link** (offline, deterministic) — builds the **decision-train graph**: connects each
   issue → its linked PRs → their commits, via timeline cross-references and commit
   trailers (`Fixes #123`, merge commits), and attributes each train to code areas via
   the graphify communities / changed-path modules.
4. **Analyze + Synthesize** (offline, the model) — **parallel sub-agents**, one per train,
   read that train's full thread + local diffs and emit a structured decision narrative
   (proposed → changed direction → rejected → spun off → shipped). The lead agent
   synthesizes the per-train analyses + buckets into the report and forecast.

Claude does only the judgment work (tracing trains, grouping, summarizing, risk-spotting,
forecasting prose). It never invents facts: commits/diffs come from the clone, the social
layer from the API, code areas from graphify, and the candidate buckets are computed
deterministically in Link.

This keeps token cost bounded (diffs are local; sub-agents are scoped to one train each)
and report data reproducible.

## The bundle is the product (fact base for many outputs)

The deliverable is the **provenance-rich bundle**, not just the digest. The user renders
multiple downstream formats from the same fact base — a detailed **L400 long-form blog
post**, a **video transcript / script**, and **social carousels** — so the bundle is the
single source of truth those all draw on. v1 ships **the bundle + one renderer (the
Markdown digest)** plus a documented schema (`BUNDLE.md`); the other renderers are the
user's to build on top. This shapes two hard requirements below.

## Fact discipline & provenance (hard requirement)

Because downstream content **amplifies** errors (an L400 deep-dive that misstates a
parameter change is worse than a wrong line in an internal note), the fact base is held to
a citation standard:

- **Every fact carries a source ref** — a `{ type, id/number/sha, url }` pointing at the
  exact PR, issue, commit, comment, review, run, or release it came from. Refs are the
  spine of the bundle; nothing narrative-bearing is unsourced.
- **No unsourced claim.** The Analyze sub-agents and the lead **cite, they do not
  paraphrase from memory.** Each per-train narrative returns an `evidence: [ref]` list, and
  every statement in any rendered output must resolve to a bundle ref. (Enforced by
  `superpowers:verification-before-completion` discipline before any output is published.)
- **Hybrid evidence persistence** (chosen): the bundle persists, *inline*, the evidence
  behind every claim that backs a narrative — the **actual diff hunk** for each feature
  delta, the **quoted comment/review text** (with author + url) used as rationale, and a
  source ref on every fact. **Bulk raw diffs stay in the referenced clone** (`clone_dir`),
  not copied into the bundle. Net: the bundle alone is fact-checkable for everything that
  drives a narrative, without re-cloning or re-hitting the API, while staying lean on a
  repo the size of AVM-Bicep.
- **Depth for L400.** Evidence is kept at the granularity a deep technical write-up needs:
  precise param/resource/output names, before→after values, and the commit/PR/comment that
  changed them — not just "module X changed".

## Why a local clone (the scale unlock)

Walking the commit tree and per-PR file lists **via the API** is what blows the rate
limit on a busy month of a large repo like AVM-Bicep. A single shallow/partial clone
bounded to the window gives the **entire commit tree + diffs locally, for free, with no
rate-limit exposure**:

- `git clone --filter=blob:none --shallow-since=<from>` (partial + windowed) keeps the
  clone small and fast even on large histories.
- Commit messages, diffs, parameter changes, and merge structure are then read with local
  `git log` / `git show` / `git diff` — no API calls.

What the clone does **not** contain (and so still needs the API) is the **social layer**:
issue/PR bodies, review + issue comments, reactions, the issue↔PR↔commit linkage
(timeline / cross-reference events), labels, milestones, Projects v2 board state, and
Actions runs. That layer is bounded by **item count, not file count**, and is batched via
GraphQL — hundreds of calls for a busy month, comfortably within budget.

## Decision trains (first-class concept)

The value of the digest is not a flat list of merged PRs — it is the **narrative thread**
of how a decision moved through the project. A *train* is the linked unit:

```
  issue (idea / request)  ──┐
        maybe-duplicate of ─┤
                            ├─▶ PR(s)  ──▶ commits ──▶ diffs (param/feature changes)
        spun-off issue   ◀──┤            (direction changes, rejects, additions)
                            └─▶ outcome: shipped | rejected | abandoned | deferred
```

**Link** builds these deterministically from:
- **Closing references** in PR bodies / commit trailers (`Fixes #`, `Closes #`, `Resolves #`).
- **Timeline cross-reference events** (issue ↔ PR ↔ commit mentions, `connected`/`disconnected`).
- **Merge commits** → which commits belong to which PR.
- **Duplicate / spun-off** signals (`duplicate` label, `Duplicate of #`, references between issues).

**Analyze** then narrates each train with a per-train sub-agent. The bundle reserves a
`trains` array so every later phase can thicken the same structure.

## Non-goals (YAGNI)

- No dependency on third-party **skills** (`repo-analyzer`, `github-issue-analyzer`,
  `github-summary`). Useful references, but the skill is self-contained.
- **graphify** is an *optional* tool dependency (code-area mapping). If `graphify` is not
  on `PATH`, Acquire skips the code-graph pass and Link falls back to changed-path module
  attribution — the rest of the pipeline is unaffected (graceful degradation).
- No `gh` CLI dependency. Auth is via `GITHUB_TOKEN` only. `git` is assumed present.
- No YouTube network access / transcript auto-fetch. The transcript is **user-provided**
  as a local file.
- No multi-repo aggregation in v1 (single target repo per run; one optional linked
  Projects v2 board). **Terraform multi-repo aggregation is deferred to Phase 6.**
- No write actions to GitHub. Read-only.
- The report is **Markdown with embedded Mermaid** diagrams (timelines, decision-train
  flowcharts, bucket charts) — renders on GitHub and most viewers, no new deps,
  text-diffable. graphify's interactive `graph.html` is **linked**, not embedded. No
  **standalone HTML/PDF** report and no automated scheduling/delivery (cron, Slack) in
  v1 — invocation is manual / on-demand. (Noted as future adds.)

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

**Buckets** (computed deterministically in Link; refs only, by item number):
- **shipped** — PRs merged in window + issues closed as completed in window.
- **in_flight** — open PRs/issues assigned to the current sprint (iteration) or current
  milestone.
- **rejected/abandoned** — PRs closed **without merge** in window + issues closed as
  `not_planned` (wontfix) in window.
- **next_candidates** — open PRs/issues assigned to the next milestone or next
  iteration, plus open items labelled high-priority. Feeds the forecast narrative.

Each bucket item carries its **train id** so the report can cross-link a shipped PR to the
issue that started it and the decisions along the way.

## Components

### 1. `gather.py` (deterministic Acquire — clone + REST + GraphQL + graphify)

The single component that touches the network. Produces the bundle; touches neither the
transcript (Analyze input) nor the report prose (Synthesize output).

- **Deps:** Python 3 stdlib only (`urllib`, `json`, `argparse`, `datetime`, `subprocess`).
  GraphQL is a plain `POST /graphql` via `urllib`. Shells out to **`git`** (clone + log +
  show) and, when available, **`graphify`** — no pip install required for the core.
- **Auth:** reads `GITHUB_TOKEN`, falling back to `GH_TOKEN`. Exits with a clear error if
  neither is set. (Projects v2 needs a token with `read:project` scope.)
- **CLI:**
  ```
  python gather.py --owner OWNER --repo REPO \
      --from YYYY-MM-DD --to YYYY-MM-DD \
      [--branches main,develop] [--clone-dir PATH] [--no-clone] \
      [--graphify | --no-graphify] \
      [--include-docs] [--include-workflows] [--include-releases] \
      [--include-projects] [--project-number N] [--project-owner-type org|user] \
      [--status-field Status] [--iteration-field Sprint] \
      [--milestone "vX.Y"] [--ref-date YYYY-MM-DD] [--out PATH]
  ```
  (`--include-workflows`/`--include-releases` default **on**; `--graphify` auto-enables
  when `graphify` is on `PATH`. `--include-projects` activates when a project number is
  provided. When a project config is used, `SKILL.md` resolves these from config — see
  component 6.)

- **Clone + local git (the commit/diff layer, network-free after clone):**
  - `git clone --filter=blob:none --shallow-since=<from> --no-single-branch <repo> <clone-dir>`
    (bounded, partial). `--no-clone` reuses an existing `--clone-dir`.
  - Commits on the listed branches with author/commit date in `[from, to]` via local
    `git log --since --until --pretty --name-only` (+ `git show` for diffs when a train
    needs them). Captures `sha`, `message`, `author`, `date`, `files`, `parents`, and
    `pr` (resolved from merge structure / trailers in Link).

- **graphify code graph (optional, local, zero-token):**
  - When enabled, run `graphify update <clone-dir>` (tree-sitter AST, no API/tokens) →
    reads `graphify-out/graph.json`. Captures **communities** (≈ logical modules),
    node→file mapping, and edges. Stored under `code_graph` in the bundle and used by Link
    to attribute trains/commits to code areas. Absent graphify → this is omitted.

- **REST fetches / windowing (the social layer):**
  - **PRs:** closed PRs touched in range via
    `GET /repos/{o}/{r}/pulls?state=closed&sort=updated&direction=desc` (paginated). Split
    into **merged in window** (`merged_at` in range) and **closed-without-merge in window**
    (`closed_at` in range, `merged_at` null). Plus **open** PRs for in-flight/next. For
    each: title, number, body, labels, reviewers, milestone, merged flag,
    merged_at/closed_at, **closing-issue refs**, review-comment bodies
    (`.../pulls/{n}/comments`). **Diffs/changed-files come from the local clone, not the
    API.**
  - **Issues:** closed in window (`state=closed&since=`, excluding PRs via `pull_request`
    key) with `state_reason` (`completed`|`not_planned`); plus **open** issues. Capture
    milestone, labels, body, issue comments.
  - **Timeline events** (for trains): `GET /repos/{o}/{r}/issues/{n}/timeline` for
    cross-references, `connected`/`cross-referenced`/`closed`-by-commit events linking
    issues ↔ PRs ↔ commits.
  - **Workflow runs** (`--include-workflows`): `GET /repos/{o}/{r}/actions/runs?created={from}..{to}`
    (paginated). Capture `name`, `conclusion`, `status`, `event`, `head_branch`,
    `created_at`, `html_url`.
  - **Releases** (`--include-releases`): `GET /repos/{o}/{r}/releases` filtered on
    `published_at` in window; capture `tag_name`, `name`, `published_at`, `prerelease`, url.
  - **Milestones:** `GET /repos/{o}/{r}/milestones?state=all`; capture `title`, `number`,
    `state`, `due_on`, `open_issues`, `closed_issues`, url.
  - **Pagination:** follow `Link` rel="next"; stop early using `from`/`to` where possible.
  - **Rate limiting:** on HTTP 403 with `X-RateLimit-Remaining: 0`, sleep until
    `X-RateLimit-Reset`, bounded retries. (GraphQL batching keeps the social layer well
    within budget.)

- **GraphQL fetch (`--include-projects`):** Projects v2 board — project title; the
  **iteration field** (iterations: `title`, `startDate`, `duration`); the **status
  (single-select) field** options; and `items` (paginated) each resolving to its linked
  issue/PR (`number`, `title`, `state`, `merged`) plus that item's `Status` and
  `Iteration` values. Field names configurable (`--status-field`, `--iteration-field`).

- **Output:** writes `workspace/activity-{from}-{to}.json` (override `--out`), alongside
  the reusable `--clone-dir` and `graphify-out/`. Bundle:
  ```json
  {
    "meta": { "owner","repo","from","to","branches","ref_date","clone_dir","generated_at" },
    "commits": [ { "sha","message","author","date","files":[paths],"parents":[],"pr":num|null } ],
    "prs": [ { "number","title","body","labels":[],"reviewers":[],"milestone",
               "merged":bool,"merged_at","closed_at","files":[paths],"closes":[issue#],
               "review_comments":[{ "author","body","url","id" }],"url" } ],
    "issues": [ { "number","title","body","labels":[],"state","state_reason","milestone",
                  "closed_at","comments":[{ "author","body","url","id" }],"url",
                  "open_high_activity":bool } ],
    "timeline": [ { "issue":num,"event","source":{ "type","number","sha" } } ],
    "workflows": [ { "name","conclusion","status","event","head_branch","created_at","url" } ],
    "releases": [ { "tag_name","name","published_at","prerelease":bool,"url" } ],
    "milestones": [ { "title","number","state","due_on","open_issues","closed_issues","url" } ],
    "project": { "number","title","iterations":[{ "title","start","end" }],
                 "items":[{ "type","number","title","state","merged","status","iteration","url" }] },
    "code_graph": { "source":"graphify|null","communities":[{ "id","label","files":[] }],
                    "nodes":[...],"edges":[...] },
    "modules": { "<dir>": { "commits","prs","files_changed" } },
    "workflow_stats": { "<workflow>": { "total","success","failure","cancelled","other" } },
    "docsRefs": [ { "path","source":"changed|referenced","pr":num|null } ],
    "release_train": { "previous":{}|null,"current":{}|null,"next":{}|null },
    "sprints": { "previous":{}|null,"current":{}|null,"next":{}|null,"all":[] },
    "trains": [ { "id","root_issue":num|null,"prs":[num],"commits":[sha],
                  "spun_off":[issue#],"duplicate_of":issue#|null,"code_areas":[community],
                  "outcome":"shipped|rejected|abandoned|deferred",
                  "evidence":[{ "type","id","url" }] } ],
    "feature_deltas": [ { "area":community,"kind":"add|drop|change",
                          "subject":"param|resource|module|output|target-scope|...",
                          "name","before","after","hunk","detail",
                          "train":id,"pr":num,"commit":sha,"url" } ],
    "buckets": { "shipped":[ref],"in_flight":[ref],"rejected":[ref],"next_candidates":[ref] },
    "diagrams": { "timeline_gantt":"<mermaid>","buckets_pie":"<mermaid>",
                  "deltas_bar":"<mermaid>","train_flowcharts":{ "<id>":"<mermaid>" } }
  }
  ```
  (`code_graph`, `timeline`, `trains`, `feature_deltas`, and `diagrams` may be thin/empty
  in early vertical slices and thicken per phase — the schema reserves their place from
  Phase 1.)
  - **Ref convention:** every `url` field and every `evidence`/source entry is a
    `{ type, id|number|sha, url }` so any fact in any downstream renderer can be traced to
    its origin. The schema is documented for renderer authors in `BUNDLE.md`.

- **Feature delta ledger (`feature_deltas`, computed in Link):** the deterministic
  add/drop/change record per code area, derived from the local diffs along each train:
  - **add** — a new parameter/resource/module/output appears in a diff (with the train/PR
    that introduced it).
  - **drop** — a parameter/resource removed or deprecated, *or* a PR closed-without-merge
    (a rejected feature) / an issue closed `not_planned`.
  - **change** — a parameter's default/type/allowed-values changed across a train's commits.
  (Subject extraction is language-aware where cheap — e.g. Bicep/ARM `param`/`resource`/
  `output`, Terraform `variable`/`resource`/`output` — else a generic added/removed-symbol
  heuristic from the diff. Reserved Phase 1, populated from Phase 3.)

- **Diagrams (`diagrams`, generated deterministically in Link):** Mermaid blocks built
  *from the data*, not hand-drawn by the model, so visuals are reproducible facts:
  `timeline_gantt` (releases/sprints over the window), `buckets_pie` (shipped/in-flight/
  rejected/next counts), `deltas_bar` (add/drop/change per area), and one `flowchart` per
  notable train (issue → PR(s) → commits → outcome). `SKILL.md`/template embed them verbatim.

### 2. `link.py` (offline, deterministic — train graph + buckets)

Reads the bundle, writes it back enriched. No network. Builds `trains` from
closing-refs + commit trailers + timeline cross-references + merge structure; attributes
each train to `code_areas` (graphify communities, else `modules`); computes
`feature_deltas` (add/drop/change per area from train diffs), `buckets`, `release_train`,
and `sprints`; and emits the deterministic `diagrams` (Mermaid timeline/pie/bar/train
flowcharts) from that data. Pure transforms over recorded data → fully unit-testable.

### 3. `SKILL.md` (procedure + analysis instructions)

Frontmatter:
```yaml
name: activity-overview
description: Use when you need a time-boxed engineering + sprint/release digest for a
  GitHub repo — shipped work, releases, CI/CD health, in-flight and abandoned items, the
  decision trains behind the work, design decisions, community-call highlights, a
  next-release forecast, and open risks, framed as previous/current/next sprint and release.
```

Procedure Claude follows:
1. Resolve the target: `--project <name>` from `projects.json`, or explicit
   `owner`/`repo`/options from the request (incl. project-board settings if any).
2. Resolve `from`/`to`, `ref-date`, milestone, clone dir, and optional transcript path.
3. Verify `GITHUB_TOKEN`/`GH_TOKEN` is set (needs `read:project` for boards); if not, ask.
4. **Acquire:** run `gather.py` (clone + API + optional graphify) → bundle.
5. **Link:** run `link.py` → bundle enriched with trains + buckets + release_train + sprints.
6. **Analyze:** for each significant train, **dispatch a parallel sub-agent** (see
   `superpowers:dispatching-parallel-agents`) that reads the train's thread + local diffs
   and returns a structured decision narrative **with an `evidence: [ref]` list — citing,
   not paraphrasing from memory** (every claim resolves to a bundle ref). (Early phases: a
   single inline pass.)
7. If a transcript is present, read it and extract community-call highlights; else skip.
8. **Synthesize:** write `workspace/activity-report-{from}-{to}.md` per `report-template.md`,
   weaving per-train narratives + call context + a **forecast** over `buckets.next_candidates`.
   Every claim must carry/resolve to a bundle ref; **verify provenance before reporting
   done** (`superpowers:verification-before-completion`).
9. Report the output paths to the user — both the **bundle** (the reusable fact base) and
   the digest — since downstream renderers consume the bundle.

### 4. Community-call transcript handling

- **Source:** user-provided local file (`.txt`, `.vtt`, `.srt`, `.md`). No network.
- **Location:** from `projects.json` (`transcript`) or passed explicitly; conventionally
  in `workspace/`.
- **Use:** summarized into a dedicated **"Community call highlights"** section AND woven
  into executive summary, design decisions, forecast, and open risks.
- **Optional:** absent transcript → section notes no call; rest of report unaffected.

### 5. `report-template.md` (fixed report shape)

Sections, in order (sections gated on data are omitted gracefully when absent):
1. **Executive summary** — sprint goals vs. outcomes; 3–5 bullets (features, releases, CI
   health, key risks), informed by call + board context. Embeds `diagrams.buckets_pie`
   (at-a-glance shipped/in-flight/rejected/next).
2. **Release train context** — previous / current / next milestone (+ current sprint
   iteration window): dates, completion %, theme. Embeds `diagrams.timeline_gantt`.
3. **Shipped this period** — merged PRs + completed issues grouped by **code area**
   (graphify community / `modules`); each links its **train** (root issue → PR(s) →
   commits), summarizes the change, notes follow-ups. Links graphify's `graph.html`.
   - **Releases** (subsection) — versions published in window (tag, date, link;
     prereleases flagged).
4. **Decision trains** — the notable threads: how an idea moved from issue → PR →
   commits, where direction changed, what was rejected or spun off, and the outcome.
   (Authored from the per-train sub-agent analyses.) Each notable train embeds its
   `diagrams.train_flowcharts[id]`.
4a. **Feature changes (add / drop / change)** — the `feature_deltas` ledger as a table per
   code area (subject, name, kind, the train/PR/commit), with `diagrams.deltas_bar`.
5. **In flight** — open items in current sprint/milestone (`buckets.in_flight`) with board
   status; flag items at risk of slipping.
6. **Rejected / abandoned** — PRs closed without merge + issues closed `not_planned`
   (`buckets.rejected`), with a one-line "why" where evident.
7. **CI/CD overview** — per-workflow success/fail counts (`workflow_stats`) + notable
   failed runs with links. (`--include-workflows`.)
8. **Bugs & reliability** — `bug`/reliability-labelled items; fixed vs still-open; themes.
9. **Infrastructure & tooling** — IaC (Terraform/Bicep/ARM), PowerShell, dependency
   changes (inferred from area + labels).
10. **Design decisions & docs** — ADRs/design docs touched or referenced (`docsRefs`) +
    decisions surfaced from commit diffs along trains, 1–2 sentence rationale each.
11. **Community call highlights** — topics, decisions, asks, follow-ups (when transcript).
12. **Next-release forecast** — over `buckets.next_candidates`: what's likely to land next,
    confidence, slippage risks. The model's judgment over script-selected candidates.
13. **Open risks & next steps** — still-open high-activity issues + flagged PR review
    comments + call follow-ups + at-risk in-flight items.

### 5b. `BUNDLE.md` (schema doc for downstream renderers)

A human-readable reference for the bundle: every field, the **ref convention**
(`{ type, id|number|sha, url }`), where evidence lives inline vs. in the clone, and which
fields back which narrative claims. This is the contract the user's other renderers (L400
blog, video script, carousels) code against, so it ships with the skill and is kept in
sync with `link.py` output. Includes a "how to fact-check a claim" walkthrough.

### 6. `projects.json` (optional per-project config)

- **Purpose:** avoid re-entering details for the three target projects.
- **Schema:**
  ```json
  {
    "projects": {
      "<short-name>": {
        "owner": "string", "repo": "string", "branches": ["main"],
        "clone_dir": "workspace/<name>-clone",
        "graphify": true,
        "include_docs": true, "include_workflows": true, "include_releases": true,
        "transcript": "workspace/<name>-call-{period}.txt",
        "project_v2": { "owner_type": "org", "number": 0,
                        "status_field": "Status", "iteration_field": "Sprint" }
      }
    }
  }
  ```
  (`project_v2` optional — omit for projects without a board; the digest then relies on
  milestones + dates only.)
- **Resolution order:** `--config PATH` → `./projects.json` (cwd) → skill-dir
  `projects.json`. If none found or `--project` not given, fall back to explicit args.
- **Distribution:** ships `projects.example.json` (placeholders). User fills a real
  `projects.json` (commit-or-local is their choice).

### 7. `commands/activity.md` (slash-command entrypoint)

- Thin wrapper so the skill triggers as
  `/activity <owner/repo|project> <fromDate> <toDate> [options]`.
- Instructs Claude to invoke `activity-overview` with the parsed args — all logic stays
  in `SKILL.md` / `gather.py` / `link.py`.
- **Install note:** Claude Code discovers slash commands under `.claude/commands/`
  (project) or `~/.claude/commands/` (user). Ships at `commands/activity.md`; install
  copies/symlinks it. Documented in `REFERENCE.md`.

### 8. Tests (offline)

- `test_gather.py` — HTTP/GraphQL and `git`/`graphify` layers isolated behind small
  `_get(url)` / `_graphql(query,vars)` / `_git(args)` / `_graphify(dir)` seams that tests
  monkeypatch to return recorded fixtures. Covers window/merge filtering, commit parsing
  from `git log` fixtures, code_graph ingestion, workflow-stats, release filtering.
- `test_link.py` — train construction (closing-refs + trailers + timeline + merges),
  duplicate/spin-off detection, code-area attribution, bucket assignment,
  release-train/sprint resolution, `feature_deltas` extraction (add/drop/change, incl.
  Bicep/Terraform subjects), and `diagrams` generation (Mermaid blocks are deterministic
  given fixtures). Includes a **provenance lint**: asserts every narrative-bearing fact
  (train, feature delta, quoted comment) carries a well-formed source ref. Runs with no
  network/token.

## Layout (committed, portable)

```
.claude/skills/activity-overview/
  SKILL.md
  gather.py                  # Acquire: clone + REST/GraphQL + graphify → bundle
  link.py                    # offline: train graph + buckets
  report-template.md
  projects.example.json
  BUNDLE.md                  # bundle schema + ref convention, for downstream renderer authors
  REFERENCE.md               # examples + install (incl. slash command) + troubleshooting
  commands/
    activity.md              # /activity slash-command wrapper
  test_gather.py
  test_link.py
  fixtures/
    rest_sample.json         # recorded REST responses
    graphql_sample.json      # recorded Projects v2 GraphQL response
    git_log_sample.txt       # recorded git log/show output
    graph_sample.json        # recorded graphify graph.json
```

Self-contained for the core (stdlib + `git`); graphify is an optional enricher. Copying
the folder into `~/.claude/skills/` makes the skill usable in any repo, with no setup
beyond `GITHUB_TOKEN` (incl. `read:project`) and optionally a `projects.json` + transcript.

## Implementation phasing — **vertical slices**

Each phase is a **complete vertical slice** (acquire → link → buckets → report) that
produces a real, **verifiable report** against the target repo. Later phases *thicken
every layer* rather than building one layer to completion. Test (and eyeball the report
against GitHub) after each.

- **Phase 1 — walking skeleton.**
  *Acquire:* shallow/partial clone (window) + minimal API (merged PRs + their closing
  issues + commit shas). *Link:* PR→commits (merge) + PR→issue (closing refs) → basic
  trains; coarse `shipped` bucket. *Report:* a real digest — "N PRs across M trains,
  bucketed as…, notable trains" — verifiable line-by-line against GitHub.
- **Phase 2 — social layer + full buckets + first visuals.**
  *Acquire:* + comments, reviews, timeline events, workflow runs, releases, milestones.
  *Link:* full cross-references, `in_flight` / `rejected` / `next_candidates`; emit
  `diagrams.buckets_pie` + `diagrams.timeline_gantt`. *Report:* + CI/CD, releases,
  rejected/abandoned, in-flight sections with embedded bucket pie + timeline.
- **Phase 3 — code areas (graphify) + feature deltas.**
  *Acquire:* graphify code-graph pass. *Link:* attribute trains/commits to communities;
  compute `feature_deltas` + `diagrams.deltas_bar`. *Report:* shipped grouped by code
  area; **Feature changes (add/drop/change)** ledger; infrastructure & tooling;
  decision-docs from diffs; graphify `graph.html` link.
- **Phase 4 — sub-agent train narratives + train graphs + forecast.**
  *Analyze:* parallel sub-agent per train → decision narratives. *Link:* emit
  `diagrams.train_flowcharts`. *Report:* deepened "Decision trains" section with embedded
  flowcharts + next-release forecast.
- **Phase 5 — Projects v2 + sprint framing.**
  *Acquire:* GraphQL board. *Link:* iteration/status resolution. *Report:* previous/current/
  next sprint + release-train framing, board status on in-flight.
- **Phase 6 — transcript, slash command, multi-repo.**
  Community-call section, `/activity` entrypoint, and **Terraform multi-repo aggregation**.

## Testing strategy

- **Unit (offline):** `test_gather.py` + `test_link.py` cover all deterministic transforms
  + train/bucket/release-train/sprint logic + config resolution against committed
  fixtures. Must pass with no network/token.
- **Vertical verification (per phase):** run the slice against a real target window
  (AVM-Bicep) with a token; confirm the bundle is well-formed and the report renders and
  **checks out against GitHub** (with/without transcript, board, graphify).

## Error handling

- Missing token → fail fast with actionable message; if `--include-projects` and token
  lacks `read:project`, name the missing scope.
- `git` missing / clone failure → fail fast naming the cause; `--no-clone` requires an
  existing `--clone-dir`.
- `graphify` absent or failing → warn, omit `code_graph`, fall back to `modules`.
- 404 (repo/project not found / no access) → clear error naming the resource.
- GraphQL errors / project number not found / missing iteration|status field → warn and
  degrade gracefully to milestone+date modeling (board sections omitted), not a hard fail.
- Unknown `--project` name → list available project names from config.
- Missing transcript (when expected) → warn, render without the call section.
- Empty window / empty buckets → valid bundle + report stating "no activity/none".
- Rate limit → sleep-until-reset with bounded retries, then fail with the reset time.

## Open questions

- The three real project coordinates (owner/repo, branches, doc layout, which have calls,
  Projects v2 numbers + field names) are not yet captured. The skill ships
  `projects.example.json`; the user supplies real values into `projects.json` after the
  skill is built (or hands them to Claude to pre-populate).
- graphify install/runtime cost on AVM-Bicep-scale clones to be measured during Phase 3
  (tree-sitter is local/zero-token, but wall-clock on a large tree needs a real timing).
