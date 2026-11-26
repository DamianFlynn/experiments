# Bundle Schema

The bundle is a single JSON object. **The skill is store-only:** the bundle is no
longer a produced file — `gather` folds its facts into the journey-graph store
(see `STORE.md`), and the reader stage materializes this bundle shape as a
**transient view** via `extract`. This document remains the contract for that
view: the **gather**/fold path supplies `meta`, `commits`, `prs`, `issues`,
`workflows`, `workflow_stats`, `releases`, `milestones`; the **link** step
(`link.py`) then fills `trains` and `buckets`; the **render** step
(`render.py`) fills `diagrams`. (The grouped sections below document everything
beyond the core keys.)

## Output contract (for downstream formatters)

The pipeline's **product is this structured view**, not the Markdown report — the
report is just one renderer over it, and other formatters (changelog, social,
video-script, dashboards) consume the same view. The contract:

- **Versioned.** `meta.schema_version` (currently `3`,
  `extract.VIEW_CONTRACT_VERSION`) identifies the shape on BOTH entry points — the
  single-repo bundle and the multi-repo project view (and each
  `members[].bundle.meta`); gather's in-memory bundle carries it too. A formatter
  should read it and refuse a version it doesn't understand (`samples/build_report.py`
  refuses a newer one). It is the view contract only: the store's own schema
  version is `graphstore.SCHEMA_VERSION`, kept in the store's meta (STORE.md).
  Bumps are recorded here: `1` was declared while extract views carried no version
  and digest stamped the store's `2`; `3` (#107) is the first version every view
  carries, covering the incompatible changes since `1` (project people areas
  `repo::area`, series v2, train `issues` / `pr_outcomes`), and no earlier view ever
  carried it. A view without the key predates #107.
- **Two entry points, same shape family.** Single-repo: the enriched bundle view
  (`extract` → `link.py`) — keys documented below. Multi-repo project: `python3
  digest.py --store <db> --project <name> --from … --to …` emits the merged
  **project view** `{meta, members[].bundle, trains, module_edges, shipped, people,
  halls, modules, related_work}` (each `members[].bundle` is a single-repo view; the
  project `meta` is `{project, repos, from, to, schema_version}`). `digest.py` and
  `view.py` write `sort_keys` JSON; `link.py` and `render.py` rewrite the bundle view with
  `indent=2` without re-sorting keys — still deterministic, because **object key order
  carries no meaning (issue #100):** every list derived
  from an object (`timeline`, `feature_deltas`, the content-timeline diagram) has an
  explicit, total sort key, so enriching a bundle gives the same result whatever order its
  objects' keys are in — a member taken from `digest.py` output and re-enriched equals the
  same member produced by `view.py` + `link.py`. (The legacy `{pattern: owners}` form of
  `code_owners` is the one exception: its key order is the CODEOWNERS rule order; current
  gathers write the ordered list form.)
- **Project view: `people` and `halls` (issue #88).** `people` is the union of the
  members' `people` by login, each figure combined across members. A member bundle's
  `people` hold only that member's contributors and count only its records, so the
  parts are disjoint and nobody is counted twice: the counts (`prs_authored`,
  `prs_merged`, `prs_reviewed`, `commits_authored`, `issues_opened`,
  `examples_authored`, `docs_authored`, `symbols_authored`, `authored_then_removed`,
  `stale_owned`; `derive.PROFILE_COUNT_FIELDS`) are **summed**; `merge_rate` is
  recomputed from the summed `prs_merged / prs_authored`; `review_latency_days` is the
  median of **every** member's latency samples (not a median of member medians);
  `first_seen` is the earliest and `last_active` the latest; `modules` / `areas` are
  `"<repo>::<area>"` (like the project `modules` keys, so two members' same-named areas
  stay apart); `is_bot` is OR'd and `login_resolved: false` when any member says so.
  `halls.fame` is rebuilt from the merged people (same rule as a bundle's), so it ranks
  on project-wide figures.
- **Project view: `related_work` is opt-in.** Each project train carries a
  `tickets` list and `related_work` is `[{ticket, train_ids}]` — trains that share
  an internal ticket id but have no spine link. Ticket parsing is **off by
  default** (`tickets: []`, `related_work: []` on every train / view): the former
  default regex (`[A-Z]{2,}-\d+`) matched `UTF-8`, `SHA-256`, `HTTP-200`,
  `ISO-8601`, `CVE-2024` and glued unrelated trains together (#74). Turn it on
  with `digest.py --manifest M` when the manifest declares `"ticket_prefixes":
  ["ABC", "PLAT"]` (pattern `\b(?:ABC|PLAT)-\d+\b`), or with `--ticket-pattern RE`
  (wins over the manifest).
- **Project view: `module_edges` (#107).** One row per stored `depends_on` edge out
  of a member: `{src_repo, src_area, dst_repo, dst_area, version, transitive,
  cross_repo, ref}`, plus `address` and `via` only on a nested `module.a.module.b`
  call. **Window rule:** `depends_on` is a current-state edge, kept as of the latest
  fold (#95); `module_edges` lists the edges as of the store's latest gather, not
  the edges of the view's `--from`/`--to` window.
- **Project view: member `meta` identity.** In `members[].bundle.meta`, `owner` is
  the store's **project scope** and `repo` the store's repo key — for a manifest
  member `owner` is the project name and `repo` is already `owner/name`, so never
  build a URL from `{meta.owner}/{meta.repo}`. `digest.py` appends four keys to
  every member's `meta` (append-only; the single-repo bundle view from
  `view.py`/`link.py` is unchanged): `project` (the project scope), `slug` (the
  GitHub `owner/name`), `repo_owner` (the GitHub owner) and `repo_name` (the bare
  repo name). Build GitHub URLs from `slug`. For a single-repo store (project =
  owner, repo = name) these are `project=owner`, `slug=owner/name`.
- **Project view CLI errors.** `digest.py` exits **2** (nothing on stdout) for an
  invalid or inverted `--from/--to` (same rules as `view.py`), a missing/unreadable
  store, an unknown `--project` (the message lists the store's projects), an
  unknown `--repo` (lists the project's repos), a bad `--manifest` or
  `--ticket-pattern`, or a `--project` that differs from the `--manifest` project.
- **Project view members.** `meta.repos` / `members[]` are the project's stored
  members (`graphstore.project_repos`): a repo present only as another member's
  `depends_on` stand-in is not one (#77). A manifest member whose last gather of a
  window overlapping the view failed or was not attempted is absent (or holds only
  older data); `digest.py` and `view.py` then print a `warning: project … is
  incomplete` on stderr (exit code unchanged) and `validate.py` fails. The view JSON
  does not list such members; do not publish a view while that warning stands.
- **Every fact carries a ref** (see *Ref convention* below), so any consumer can
  cite/fact-check without re-fetching. Narrative is NOT in the view — it's the
  model's job, authored from the view.
- **Stability.** Top-level keys are append-only within a `schema_version`;
  omit-when-empty containers keep the shape diff-stable across windows.
- **Reference formatters.** `samples/build_report.py` (structural Markdown skeleton)
  and `examples/formatters/shipped_changelog.py` (a compact per-repo "shipped"
  changelog) both consume the view unchanged — copy either as a starting point.

## Ref convention

Every provenance reference is `{ "type": "pr|issue|commit", "id": <number|sha>,
"url": "https://..." }`. Every narrative-bearing fact (a train, a bucket entry)
resolves to at least one such ref. To fact-check any claim in a report, follow its
ref `url` to GitHub. (`pr` and `issue` refs are emitted directly; commits appear
as bare SHAs in `trains[].commits`.)

## Top-level keys

Field lists below are the core shapes; the grouped sections further down document
the rest (engagement, code areas, trains, people/flow).

- `meta` — the bundle `gather` assembles in memory (and folds; never written to disk) has
  `owner, repo, from, to, branches, base_branch, clone_dir, ref_date, clone_sha, clone_mode,
  boundary_dropped_commits, period, prev_bundle, schema_version`; a **view** carries the
  subset listed under "A view … carries" below. **`base_branch`** is the analyzed mainline branch: a PR whose `base` differs
  from it is a stacked/fork contribution (merged into another branch, not main) — kept in `prs`
  as context but not counted as shipped-to-main (see `commits`/`date` note below and trains).
  gather takes it from `--branches` (first entry) or a manifest member's `branch`, else the
  repo's default branch (the clone's `origin/HEAD`, else the GitHub API `default_branch`);
  there is no hard-wired `main`. `branches` is the list it was given or resolved. Views carry
  both (issue #84): fold stores them per gathered window and `extract` re-emits them —
  the exact window's value, else the agreeing value of the gathered windows the view
  overlaps. **`base_branch: null`** (with `branches: []`) means unknown — gather could not
  determine it, the overlapped windows disagree, or the store was folded before #84; `link`
  then counts every merged PR as merged into the mainline (no stacked-PR detection) and
  `extract` warns on stderr when that decides anything. The walked branch IS `base_branch`
  (issue #107): when it is not the remote default, gather fetches `origin/<base_branch>` under
  the same shallow bound and checks it out, so `commits`, `code_events`, `symbol_events`, the
  code graph and CODEOWNERS all describe it (one stderr note says so); a `base_branch` the
  remote does not have stops the gather (a manifest member is recorded failed).
  Plus **`clone_sha`** — the commit the structure (`code_graph` and its edges, the structural
  scan, CODEOWNERS) was read at, and **`structure_as_of`** (issue #107): `"window_end"` when
  that commit is the last commit of the walked branch (first-parent) whose committer date is
  on or before `to` — the current month's head, or for a past window re-gathered later the
  mainline as it was then (read from a temporary worktree; the clone stays on its branch
  head) — or `"head"` when the bounded clone does not hold that commit (a window with no
  commit in it or its margin, before later ones), so the structure is the head's; both `null`
  without a clone. `clone_sha` is provenance only: nothing checks it out again or rebuilds
  from it.
  **`clone_mode`** (`fresh` | `fetched` | `depth1-fallback` | `none`) records how gather
  obtained the clone (see `gather.prepare_clone`), and **`boundary_dropped_commits`** lists
  the in-window commit shas whose diffs the shallow boundary dropped (the code history is
  incomplete for them). Since #107 fold stores both per gathered window and a view carries
  them (omit-when-absent): `clone_mode` only for a view of exactly one gathered window,
  `boundary_dropped_commits` as the sorted union over the gathered windows wholly inside
  the view, omitted when empty. **`closing_refs_source`** (`graphql` | `keywords`, issue
  #105) is bundle-only: where the PRs' `closes` came from — GitHub's
  `closingIssuesReferences` merged with the closing keywords, or the keywords alone because
  GraphQL was unavailable (gather prints which on stderr). (Store-only — see `STORE.md`;
  this bundle shape is the transient view `extract` materializes from the store, not a
  produced file.)
  A view (`extract` / `view.py` / each `digest.py` member bundle) carries `owner, repo, from,
  to, base_branch, branches` (+ `clone_sha`, `state_as_of`, `facts_windows` when they apply),
  **`period`** = the requested `{from, to}` and **`ref_date`** (issue #75).
  `period` is the window `compute_buckets` and `build_forecast` limit to (`link.window_period`:
  `period`, else `{from, to}`), so items the store holds from other windows — or old items
  gather pulled because they were updated in the window — fall in no bucket. `ref_date` frames
  milestones, sprints, forecast age and flow staleness: the view's `--ref-date` if given, else
  the gather `--ref-date` stored for exactly this window, else `to`.
  **`state_as_of`** (issue #85, omit-when-absent) is the `to` of the repo's latest gathered
  window, present only when that is after the view's `to`: the view is of an earlier window
  than the store's latest, and its PR/issue records (state, labels, milestone, `updated_at`,
  comments) are as the latest gather saw them, not as they were at the view's `to`.
  **`facts_windows`** (issue #94, omit-when-absent) is present only when the store keeps
  per-window facts for the repo and the view is **not** exactly one gathered window. It names
  where the view's `workflow_stats` / `code_graph` / `code_owners` / `label_taxonomy` /
  `clone_sha` come from: `{"structure": {from, to}, "workflow_stats": [{from, to}, ...],
  "workflow_stats_complete": bool, "workflow_stats_excluded"?: [{from, to}, ...]}`.
  `structure` is the gathered window whose code graph, owners, taxonomy and clone sha the view
  carries (the latest overlapping the view, else the latest before it, else the earliest).
  `workflow_stats` lists the gathered windows lying wholly inside the view whose CI statistics
  are **summed** into the view's `workflow_stats` (`[]` → the key is absent).
  Summed windows never overlap one another (taken by start day, the longest first), so no run
  is counted twice when the store also holds a roll-up window or part of a month.
  `workflow_stats_complete` is true only when the summed windows cover every day of the view.
  Only windows whose CI runs were collected are summed: a window gathered with `--no-workflows`
  (gather's `meta.workflows_collected: false`), or one whose stored CI value is missing, is
  excluded and makes the sum incomplete; a collected window with zero runs counts.
  `workflow_stats_excluded` lists the gathered windows overlapping the view that were not
  summed — they overlap the view only partly (their runs cannot be split by day), overlap a
  summed window, or have no per-window facts (folded before #94). A view of exactly a gathered window has no
  `facts_windows`: its facts are that window's.
  **Window scope** (issue #85): a view holds only its window's records — the rule mirrors what
  gather collects for a window, applied to each record's latest stored state:
  `prs` merged in the window, closed (unmerged) in it, or open and created by its end;
  `issues` closed or updated in it, open and created by its end, or closed / cross-referenced
  by one of the view's PRs (**train context**: an issue from another window can be here; a PR
  never is); `commits` dated in it and the `code_events` / `symbol_events` of those commits;
  `releases` published in it; reviews / lifecycle events with their parent; any record whose
  stored timestamp is in the window, and any undated one (it cannot be placed in a window).
  `artifacts` keep only the window's lifecycle events (an artifact with none is absent), with
  `status` / `replaced_by` / move fields as of the window's end. `people` (issue #88) holds
  only the logins with a contribution **in this view** — author / merger / reviewer /
  commenter of a view PR, author / commenter of a view issue, author of a view commit — so a
  member bundle never lists another member's people, nor someone who worked in this repo only
  in another window. A person's `modules` / `areas` are re-derived from the view's records
  with this repo's `code_graph` (the attribution fold applies): this repo's areas the person
  touched in this window. The stored person node keeps the project-wide union. The per-repo
  facts `workflow_stats`, `code_graph`, `code_owners`, `label_taxonomy` (and `meta.clone_sha`)
  are the gathered window's own for a view of exactly that window, else summed / picked as
  `facts_windows` describes (#94); a store folded before #94 gives the latest gather's. Not
  windowed: `milestones`, `sprints` — current structure, as of the latest gather. On a
  store holding only the viewed window every stored record is in the view; a view spanning
  several gathered windows is their union.
- `commits` — `[{ sha, parents, author, author_name, login_resolved, login_source?, date,
  committed_at?, message, files, pr }]` (`pr` set by link).
  `date` is the **committer/landing date** (`%cd`); the walk windows in Python on it
  (`window_records`) rather than via git `--since/--until`, which prunes valid in-window commits
  across committer timezones. `committed_at` (#82) is the full committer timestamp (`%cI`)
  in UTC, `YYYY-MM-DDTHH:MM:SSZ`; it orders the `timeline` only (windowing stays on `date`).
  It is absent on commits gathered before #82 and on backfilled commits.
  **People identity (#78):** `author` is the author's **GitHub login** — the same key PR, issue,
  review and comment records use — so one person is one `people` entry. `gather.resolve_commit_authors`
  resolves it from real data, in order: the commits-list API's `author.login` for that SHA
  (`login_source: "commits_api"`), then a `users.noreply.github.com` author email
  (`<id>+<login>@…` or `<login>@…`; `login_source: "noreply_email"`; the spelling of a PR/issue
  login that differs only in case wins). The PR author is never assumed. `author_name` is the
  git author name (`%an`). When neither source resolves it, `login_resolved` is `false`, there is
  no `login_source`, and `author` is the git name — suffixed `" (git)"` when the name could itself
  be a login (`derive.unresolved_person_key`), so an unresolved name never equals, and is never
  merged with, a real login. The author email is read only to resolve the login and is **not
  stored**. `code_events[].author` / `symbol_events[].author` carry the same resolved value.
  Bundles gathered before #78 have no `author_name`/`login_resolved` and keep the git name in
  `author`.
- `prs` — PRs in scope (merged-in-window, plus open and closed-unmerged PRs): `{ number, title, body, author, author_association,
  labels, merged, merged_by, merged_at, closed_at, state, base, head, head_repo, closes:[issue#], url }`.
  **`closes`** (#62, #105) = the same-repo issues the PR closes: the closing keywords in its
  title/body, in text order, then — when `meta.closing_refs_source` is `graphql` — every other
  same-repo issue GitHub reports in `closingIssuesReferences` (a manual **Development**-sidebar
  link leaves no keyword), ascending. Such an issue is removed from `crossref_issues` (it was
  only a mention there). When GitHub reports anything the keywords miss, the PR also carries
  **`closing_issues`**: GitHub's whole list as `[{owner, repo, number}]`, sorted by owner/repo
  (case-insensitive) then number; its other-repo entries are cross-repo `closes` refs, like a
  keyword `Fixes owner/repo#N` (see `STORE.md`, multi-repo). Absent otherwise, so a PR whose
  closing issues are all keyword ones is unchanged.
  `base`/`head` are branch refs; a `base` other than `meta.base_branch` marks a stacked/fork PR.
  **`head_repo`** (#93) is the `owner/repo` the head branch lives in (a fork's name for a fork
  PR; `null` when GitHub no longer knows it, e.g. a deleted fork, and absent from bundles
  gathered before #93).
  PRs are de-duplicated by `number` (a PR listed twice across pages keeps its latest snapshot).
  `merged` is `merged_at != null`. GitHub's list-pulls payload omits `merged_by` and the comment
  counts, so `acquire` takes `merged_by` from the actor of the PR's issue-timeline `merged` event
  (`null` for an unmerged PR or a deleted account) — no extra per-PR call.
- `issues` — issues in scope (PR-closing, plus open and not-planned-closed issues): `{ number, title, body, kind, author,
  author_association, labels, assignees, state, state_reason, closed_at, url }`.
- `trains` — `[{ id, kind, root_issue, issues:[#], prs:[#], pr_outcomes:{"<pr#>": outcome},
  commits:[sha], code_areas:[id], outcome, contributing_prs:[#], significance:float,
  tier:"deep"|"mention", effort:{opened_at, merged_at, elapsed_days, reviewers, review_comments,
  commits, participants, stalled}, evidence:[ref] }]`.
  **Membership (#93).** Every PR that targets `meta.base_branch` — merged into it, open, or
  closed unmerged — is in exactly one train, whatever its own outcome. PRs are joined by the
  issues they **close** (`closes`): a train is a connected group of PRs and the issues they close,
  so two PRs closing the same issue (a rejected first attempt and the reworked one that shipped)
  are one train, and every issue a train's PRs close is in its `issues` (sorted) and carries the
  train id on its bucket ref (a Development-sidebar link counts once gather read it from
  GitHub, #105). A mention or timeline cross-reference (`crossref_issues`) never
  makes an issue a member or a root: a tracking issue that mentions three PRs leaves them three
  trains. `pr_outcomes` maps each PR number (a string key) to that PR's own `"merged"`, `"open"`
  or `"rejected"` (closed unmerged), so the narrative can tell attempts apart.
  A train is anchored by its lowest closed issue — `root_issue`, id `train-issue-<n>` — or, when
  its PRs close no issue (process not followed / a forked PR), by its lowest **PR**
  (`train-pr-<n>`). `outcome`, by precedence shipped > in_flight > rejected > abandoned:
  `"shipped"` (≥1 PR merged to `meta.base_branch`), `"in_flight"` (else ≥1 OPEN PR targeting it),
  `"rejected"` (only CLOSED-unmerged PRs; change dropped), `"abandoned"` (an issue closed
  `not_planned` that no PR closes — an idea that went nowhere; `prs: []`, `issues: [<n>]`).
  **`contributing_prs`** are PRs merged into another PR's branch (`base` == that PR's `head`, the
  head living in this repo: matched by `head_repo` + `head`, so a fork's same-named branch is
  never the parent; with no `head_repo` on record a sole same-named candidate is used, several
  are ambiguous and the PR is attached to none) — stacked/fork contributions to this train's
  journey-to-main, tracked but not counted as shipped. A PR that targets another branch and is
  not merged is in no train.
  `kind` derives from the first typed issue of the train (lowest number first); when none is
  typed it falls back to the PRs' conventional-commit title prefix (`feat→feature`, `fix→bug`,
  `docs→docs`; lowest PR first), else
  `other` — so significance still differentiates work on repos that title PRs but rarely type issues.
  `significance = footprint × kind_weight + breadth` (footprint counts prs + commits + code_areas +
  contributing_prs); `tier` = `"deep"` for the top-N by significance ∪ any train ≥ the significance
  floor, else `"mention"`.
  `effort`: `reviewers` = count of distinct reviewer logins (int); `review_comments` = summed
  `review_comments_count` (int); `participants` = count of distinct authors + reviewers +
  comment-authors (int); all over every PR and issue of the train (`opened_at` = the earliest
  `created_at` among them);
  `stalled` = merged but `elapsed_days > TRAIN_STALL_DAYS`. All fields degrade to null/0
  when data is thin. Tunables: `TRAIN_KIND_WEIGHTS`, `TRAIN_SIGNIFICANCE_TOP_N`,
  `TRAIN_SIGNIFICANCE_FLOOR`, `TRAIN_STALL_DAYS`.
- `buckets` — `{ shipped:[ref], in_flight:[ref], rejected:[ref], next_candidates:[ref] }`
  (all four buckets are classified — see below).

## Optional (omit-when-empty) keys

These keys are reserved in the bundle skeleton and **filled when the data exists** —
by the later pipeline steps (`link`/`render`) or when the repo actually has them (a
linked board, releases, CODEOWNERS, …). They are empty/absent when there is nothing to
report (the omit-when-empty rule that keeps the shape diff-stable), **not** permanently
empty. The groups below document each once it's populated.

`timeline, artifacts, feature_deltas, code_owners,
code_graph, label_taxonomy, modules, workflow_stats, workflows, releases, milestones,
docsRefs, release_train, sprints, project, diagrams`.

## Engagement, buckets, diagrams & forecast fields

- **prs[]** gain `created_at`, `updated_at`, `milestone`, `comments`,
  `review_comments_count`, `reviewers`, `review_decision`
  (`approved|changes_requested|commented|none`), and `crossref_issues`. See
  *Review decision & rounds* below for the exact rules.
- **issues[]** gain `milestone`, `updated_at`, `comments`. Open and
  not-planned-closed issues are now included, not just PR-closing issues.
- **workflows[]**, **workflow_stats{}**, **releases[]**, **milestones[]** are
  populated (see the schema block in the design spec). `workflow_stats` is keyed by
  workflow name, plus the reserved key `_truncated` ONLY when GitHub's 1,000-run
  search cap could not be split away: `{truncated, cap, fetched, total_count,
  ranges}`. Consumers listing workflows skip keys starting with `_`. A view summing several
  gathered windows (`meta.facts_windows`) adds the counters per workflow name and merges the
  `_truncated` markers (`fetched` / `total_count` summed — `null` if any is unknown —,
  `ranges` concatenated, largest `cap`).
- **buckets** are fully classified: `shipped`, `rejected`, `in_flight`,
  `next_candidates` (one bucket per item; precedence shipped > rejected >
  next_candidates > in_flight). Each ref may carry a `train` id. The current/next
  milestones come from `select_milestones`: open milestones **with a due date** only
  (undated ones, e.g. "Backlog", are never selected; none dated → no current/next);
  current = earliest due on/after `ref_date`'s day, next = the one after; if all are
  overdue, current = the most recently due and next = none. Ties break by number.
- **diagrams{}** maps `buckets_pie` / `timeline_gantt` to their `.mmd` paths,
  written and mmdc-validated by `render.py`. **`diagrams.train_flowcharts`** is a nested
  map `{ "<train-id>": "diagrams/<train-id>.mmd" }` written by `render.py` (one adaptive
  Mermaid `flowchart` per DEEP train; mode C with code-area annotation nodes when
  `len(prs) ≤ TRAIN_FLOW_MAX_PRS` and `len(distinct code_areas) ≤ TRAIN_FLOW_MAX_AREAS`, else
  mode A bare chain). On-demand spotlight render via `render.py <bundle> --train <id>` (any
  tier). **`diagrams.truncated`** maps each diagram that hit a size cap (flat key or train
  id) to `{ shown:int, total:int, unit:"PRs"|"releases"|"PRs+releases"|"events"|"edges"|"kinds" }`;
  it is `{}` when nothing was cut (caps and ranking keys: `docs/LLD.md` §7).
- **forecast** `{ next_milestone:<title|None>, candidates:[ { ref:{type,id,url},
  train:<id|None>, score:float, tier:"likely"|"possible"|"longshot", signals:[str,...],
  notes:[str,...] } ] }`
  — forward-only next-release forecast over `buckets.next_candidates`, sorted by score
  desc. `next_milestone` is the bucket rule's next milestone (always dated; `None` when
  there is no upcoming dated one after current). Signals: `on milestone <title>` / `high-priority` / `open PR` or `work in progress`
  / `active in window` / `long-open`. The in-motion signal (#87) needs live work: `open PR`
  when the candidate is an open PR or an open PR `closes`/cross-references the issue;
  `work in progress` when the candidate's train (looked up in `trains` by `train`) has
  `outcome` `in_flight`. A `rejected`, `shipped` or `abandoned` train, or a `train` id that
  names no train in the view, scores nothing. `notes` is non-scoring context (`[]` when
  none): `previous attempt rejected` (train `rejected`) or `linked PR already merged` (train
  `shipped`; since #93 this includes an open follow-up PR whose train already shipped). An
  issue that PRs only mention has no train (#93), so neither the train signal nor a note
  applies to it. Tunables: `FORECAST_WEIGHTS`,
  `FORECAST_TIER_LIKELY_THRESHOLD` (≥5.0 → likely), `FORECAST_TIER_POSSIBLE_THRESHOLD`
  (≥2.0 → possible), `FORECAST_OVERDUE_DAYS`.
- **series** (only with `link.py BUNDLE --series INDEX`; absent otherwise) —
  `{ first_installment:bool, prior:{from,to,captured}|None, new:[row],
  carried_over:[row], forecast_loop:{ landed:[loop], not_yet:[loop], dropped:[loop] } }`.
  The comparison is against the installment recorded for the window **immediately before**
  this bundle's window (`meta.period`, else `meta.from`/`meta.to`) in the index — not the
  last one appended. A `row` is `{type, id, url, bucket}` (`bucket` = this window's bucket);
  carried rows add `prior_status` (the prior board status, else `"in_flight"`). `new` = this
  window's `shipped` / `in_flight` items the prior installment did not know at all (in none
  of its buckets nor its forecast). `carried_over` = items in the prior's `in_flight` that
  are in **any** of this window's four buckets (so in-flight work that was rejected or
  moved to `next_candidates` is reported, with that `bucket`). A `loop` row is
  `{type, id, tier}` for each prior forecast candidate: `landed` = now in `shipped`,
  `dropped` = now in `rejected` (PR closed unmerged, issue closed not planned), `not_yet`
  otherwise. `prior.captured` is `"backfill"` when that installment was recorded after a
  later window (its open work and forecast are as of that run, so leave its loop out of
  hit-rate statistics), `"in_order"` otherwise, `None` for an entry written before #86.
  Rows sort by `(type, id)`. The index file itself (`{"version": 2, "installments": [...]}`,
  one entry per window) is described in `docs/LLD.md` §6.9.

### Review decision & rounds

GitHub review states are `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`
and `PENDING`. `PENDING` is an unsubmitted draft and never counts anywhere. GitHub
records every review-thread reply as a `COMMENTED` review, and rewrites a dismissed
review's state to `DISMISSED`.

- **`prs[].reviewers`** (`gather.summarize_reviews`) — sorted logins with at least
  one submitted review (`APPROVED`/`CHANGES_REQUESTED`/`COMMENTED`/`DISMISSED`).
- **`prs[].review_decision`** (`gather.summarize_reviews`) — per reviewer, the
  *effective state* is their latest `APPROVED`, `CHANGES_REQUESTED` or `DISMISSED`
  review (chronological by `submitted_at`, then review id); a later `COMMENTED`
  review never overrides it. A reviewer with none of those three is `commented`. An
  effective `DISMISSED` contributes nothing. Overall: `changes_requested` if any
  reviewer's effective state is that, else `approved` if any, else `commented` if
  any, else `none`. So approve → comment = `approved`; changes requested → comment
  = `changes_requested`; changes requested → approve = `approved`; an approval
  that was dismissed (and not re-given) is no longer an approval.
- **`prs[].review_rounds`** `{count, states}` (`derive.annotate_review_rounds`,
  enrich-time; omitted when the PR has no counted review). A **counted review** is a
  submission with a `submitted_at` and state `approved`/`changes_requested`/
  `commented`/`dismissed` (never `pending`) whose author is **not the PR author**
  (when `pr.author` is unknown, nobody is excluded on that ground). `states` lists
  every counted review's state, oldest first (`submitted_at`, then integer id, then
  input order). A **change-request block** is a maximal run of consecutive
  `changes_requested` counted reviews from distinct reviewers (a reviewer repeating
  a change request starts a new block — that is a re-review). A **review round**
  is: the first counted review opens round 1, and each change-request block that is
  followed by at least one later counted review opens one more round. So
  `count = 0` with no counted review, else `1 + (change-request blocks followed by a
  later counted review)`. Examples: `approved` → 1; `commented, approved,
  approved` → 1; `changes_requested, approved` → 2; bob `changes_requested`, carol
  `changes_requested`, bob `commented`, carol `approved` → 2 (one block); bob
  `changes_requested`, bob `changes_requested`, bob `approved` → 3; `approved,
  changes_requested` (nothing after) → 1. Because the bundle carries no push data,
  a round is inferred from review order only.

## Narrative-substrate fields

- **prs[]** gain `review_comments: [{author, author_association, body, url, id, created_at}]`
  (inline diff comments) and `comments_list: [{...same shape}]` (conversation
  comments). The integer count stays under `comments` / `review_comments_count`
  (in `acquire`, the lengths of `comments_list` / `review_comments`);
  the body arrays live under `comments_list` (the `comments` name was already taken
  by the count).
- **issues[]** gain `comments_list: [{...}]`, `reactions: {"+1","-1","heart",
  "hooray","total"}`, and `open_high_activity: bool` (open issue with notable
  comments/upvotes).
- **code_events** (gather) — raw file-level events from the full-window
  `git log --name-status -M -C` walk: `[{commit, author, date, change:
  add|modify|delete|rename|copy, path, old_path?}]`. The raw material Link folds.
  Paths here, in `commits[].files` and in the patch walk are the plain repo-relative
  path: git runs with `core.quotepath=false` and any C-quoted path (`"…\303\251…"`,
  or one containing a quote/backslash/TAB/newline) is unquoted, so non-ASCII and
  spaced names match across the walks and the bounded `hunk` attaches.
- **symbol_events** (gather) — symbol-granular change events from a single
  full-window `git log -p --unified=3` walk, attributed **diff-locally**: `[{commit,
  author, date, path, lang:"bicep|terraform", subkind:"param|var|output|resource|
  module|variable|comment|todo", name, change:"add|drop|change", before, after}]`.
  `before`/`after` are bounded (≤200 char) hunk snippets. **Comments** are identified by
  their TEXT (`name` = the comment, capped), so a comment **replaced as a decision
  evolves** is tracked as the old text dropped + the new text added (the decision trail)
  rather than collapsed; actionable/decision markers (`TODO`/`FIXME`/`HACK`/`XXX`/`BUG`)
  get subkind **`todo`**; decorative banner comments (no alphanumeric content) are
  ignored. The raw material Link folds into symbol artifacts. (graphify-language symbols
  are best-effort where present.)
- **artifacts** `{ "<id>": { kind:"example|doc|readme|symbol|comment", path, name,
  status:"live|removed|replaced", replaced_by:id|null, code_area, lifecycle:[{event:
  "add|change|remove", commit, author, date, ref[, before, after]}] } }`. File-level
  ids are `art:<path>` (via `artifact_id`); **symbol/comment** ids are
  `<path>#<lang>:<subkind>:<name>` and also carry `lang`/`subkind`. `feature_deltas[].artifact`
  joins back to these keys. `code_area` is filled by area attribution (below). Symbol lifecycle entries
  carry the bounded `before`/`after`. `lifecycle` is in commit order (the commit's UTC `committed_at`
  when known, else the local `date`; ties keep git/ledger order). A view from a store holds
  only the view's window's events (issue #85; the stored node is cumulative over every folded
  window, #76) and `status`/`replaced_by` are as of the window's end. `status` is derived from the artifact's **last**
  lifecycle event: `replaced` only when that event is a rename to a tracked path
  (`replaced_by` = that direct successor; consumers walk A→B→C); a later re-add makes
  it `live` again with `replaced_by:null`; a delete, or a rename to an untracked path,
  is `removed` with `replaced_by:null`.
- **symbol_moves** (link) — window-wide symbol-identity links: `{ links:[{subkind,
  name, from_path, to_path, from:<src aid>, to:<dst aid>, confidence:"high|medium", basis:
  "file_rename|unique_name"}], by_confidence:{high,medium} }`. Each link object also carries
  `lang`. A move = the same `(lang, subkind, name)`
  symbol dropped in ONE file and added in ONE other (precision over recall — ambiguous/boilerplate
  names are skipped). On the linked artifacts the source is `status:"replaced"` + `replaced_by`
  the dest, the dest carries `identity_from`, and both carry `move_confidence`/`move_basis`.
  In a store the moves are matched per folded window and the stored artifacts apply all of
  them (STORE.md, symbol-move edges); a source re-added later is `live` again. `link.enrich`
  still re-matches over the view's `symbol_events`: a view of one gathered window holds only
  that window's events (the set fold matched), but a view spanning several gathered windows
  can add a move no single window had (LLD §14.5).
  `confidence` is `high` only when git also flags the file pair as a rename/copy. (Name-changed
  renames via body fingerprint are deferred.)
- **timeline** `[{ ts, actor, layer:"social|code", event, ref:{type, id, url},
  subject:{kind, name, path} }]` — sorted social (comments/reviews) + code
  (artifact lifecycle) events. `ref.id` is the PR/issue number for social events
  and the commit sha for code events (the bundle-wide `{type, id, url}` ref
  convention). Social events have no file `subject.path`; code events carry both.
  `ts` is the comment's `created_at` for social events and the commit's `committed_at`
  for code events (#82), so same-day events interleave by real time; a code event whose
  commit has no `committed_at` falls back to its date (`YYYY-MM-DD`), which sorts before
  that day's timestamped social events. Order is `(ts, ref.url, layer)` — social before
  code — then, for code events, artifact id and the event's position in that artifact's
  lifecycle (social events with an equal `(ts, ref.url)` keep their list order); it never
  depends on the key order of `artifacts` (#100).
- **feature_deltas** `[{ area, kind:"add|drop|change", subject, name, before,
  after, detail, artifact:id, author, train:id|null, pr:num|null, commit:sha, url
  }]` — a projection over `artifacts` (add→add, remove→drop, change→change). For
  **symbol/comment** subjects `before`/`after` carry the bounded hunk
  snippet and `detail` is `"<lang> <subkind> <name>"`; file-level deltas leave them
  null. `area` is filled by area attribution. `pr`/`train` resolve best-effort via
  the commit→PR map. Ordered by event time (the commit's `committed_at`, else the
  event's date), then `artifact`, then lifecycle position (#100).
- **diagrams{}** now also maps `content_timeline` (Mermaid `timeline`) and
  `deltas_bar` (Mermaid `xychart-beta` bar).

## Code-area & label-facet fields

- **code_graph** `{ "provider": "directory|graphify", "areas": [{ "id", "label",
  "paths": [...], "edges": [] }] }`. The **directory provider** (primary,
  zero-dep, offline) maps each tracked file to its module directory (AVM
  `avm/res/<svc>/<module>/`, any `main.bicep` dir, Terraform `modules/<name>/` or
  any `*.tf` dir, else the top 2 segments of the file's directory — a repo-root file
  keeps its own name); the area `id` is that directory. A `*.tf` file in a nested,
  non-`examples/`/`tests/` directory under `modules/<name>/` (`modules/sub/inner/x.tf`)
  is a module of its own: its area is that directory (#101); other files there stay in
  `modules/<name>`. A file directly under an AVM
  group (`avm/res/<svc>/README.md`) is not a module: it maps to the group area `avm/res/<svc>`.
  A **Bicep module directory** (issue #107) — one holding a tracked or changed `main.bicep`,
  outside the AVM layout, the repo root and `examples/`/`tests/` — is the area of every
  non-`*.tf` file in it and below it, down to the next such directory; files under an
  `examples/`/`tests/` subtree keep the rules above. So `infra/modules/net.bicep` next to
  `infra/main.bicep` is in `infra` (was `infra/modules`) and `a/b/c/README.md` next to
  `a/b/c/main.bicep` is in `a/b/c` (was `a/b`); a two-segment module dir
  (`platform/hub/README.md`) and the AVM / Terraform layouts are unchanged. gather knows the
  module directories from the window's changed paths plus the tracked files of the structure
  commit; without a clone, from the changed paths alone.
  **graphify** is an OPTIONAL provider for its ~25 tree-sitter languages — it reads
  graphify's real `graph.json` (top keys `nodes`/`links`; each node carries an
  integer `community` + `source_file`; **no** top-level `communities` list; edges
  live under `links`) and groups nodes by `community` into `community:<n>` areas.
  graphify does NOT parse Bicep/HCL, so on those repos the directory provider runs.
  **`code_graph.areas[].edges` — inter-area dependency edges.** Each edge:

  | field | meaning |
  |-------|---------|
  | `to` | canonical target **repo area-id** (`avm/res/<svc>/<module>` or a local dir), or `null` when the target is external/unresolvable — its identity is then in `ref`. A Bicep registry ref maps to an area **only** when it is the public registry (`br/public:<path>:<tag>` or `br:mcr.microsoft.com/bicep/<path>:<tag>`) **and** the classified area (`<path>`) is a module of this repo (changed in the window, or present in the clone as `<path>/main.bicep`); a private registry (`br:<other-host>/…`), template spec (`ts:<sub>/<rg>/<name>:<ver>`, `ts/<alias>:…`), user alias (`br/<alias>:…`) or a public module this repo does not contain is `null` |
  | `kind` | `"module"` — inter-area module dependency (resource-level `dependsOn` edges are deferred) |
  | `ref` | the raw reference (`br/public:…:<ver>`, `br:<host>/…:<tag>`, `ts:…`, a local path, or a TF `source`), kept verbatim |
  | `version` | pinned version when present, else `null` |
  | `transitive` | `false` = a direct source reference; `true` = discovered deeper in the resolved build tree. Terraform (#80): every `module {}` block in the area's own `*.tf` is `false`, consumed or not; `true` only for a **nested** call (`module.a.module.b`) found in the `terraform graph` DOT |
  | `address` | Terraform nested edges only: the call's module address, e.g. `module.a.module.b` |
  | `via` | Terraform nested edges only: the parent call it is reached through, e.g. `module.a`; its first segment is one of the area's own `module "<name>"` blocks |
  | `local` | present + `true` when the ref is a **private child submodule** of this area (a local relative path resolving under the area); `to` is then the named child node `<area>/<child>` instead of a self edge. A ref to the area's own `main.bicep` is recursion and stays a self edge. Child edges come from building the **parent**: a child module is never the area's entrypoint |
  | `instances` | `"many"` for an array instantiation (`= [for …]`, i.e. multiple instances), `"one"` for a single `= {`, on local child edges |
  | `provider` | `"bicep"` or `"terraform"` |
  | `resolved` | `true` when `to` is non-null (`to: null` ⇔ `resolved: false`; `version` still carries the ref's tag) |

  **Build-only (per area).** An area's edges are populated *only* from a successful `bicep restore`+`bicep build`
  (Bicep) or `terraform init -backend=false`+`terraform graph` (Terraform) against the cloned
  working tree. When the CLI or registry is unavailable, or a build fails, that area's `edges`
  stays `[]`. Immediate (`transitive:false`) edges are fully identified (area-id + version) from
  source. Bicep transitive edges connect to *other areas in the repo* (deep external-module
  internals are not fabricated into edges). **Terraform (#80):** the direct edges are the area's
  own `module {}` blocks, parsed from source, so a module nobody consumes still has its edge. The
  build only adds nested calls. Each nested call is `transitive:true` with `address`/`via`, and
  its target (`to`/`ref`/`version`) comes from the `.terraform/modules/modules.json` that the same
  `terraform init` wrote. A call inside a downloaded package is attributed to that package's
  module. Without the manifest, `to` and `ref` are `null`. A nested target that is already an
  edge (same area, or the same registry module ignoring `//submodule`) is not repeated: direct
  wins. **Static edges (structural scan, #92):** a multi-repo (manifest) gather, and a
  single-repo gather given `--structural-iac`, also adds the whole-tree structural scan. It
  emits the same direct edges, without `terraform` or `bicep`, for every Terraform module dir
  and every Bicep module (`<area>/main.bicep`) in the clone, including modules not changed in
  the window, which are appended as areas (see STORE.md). A Bicep scan edge is exactly a
  build-path immediate edge (`transitive: false`, same `to`/`ref`/`version`/`local`/
  `instances`); a public-registry ref resolves in-repo when `<path>/main.bicep` is a tracked
  module. Transitive Bicep edges still need the build. A graph the scan ran over carries
  `code_graph.structural_scan: true` (issue #95: it holds the whole tree's direct edges, so
  fold may retract a stored direct `depends_on` it no longer has). Without the flag a
  single-repo gather does not run the scan and stays build-gated (byte-stable). The `bicep` and
  `terraform` CLIs are therefore required to populate edges (see REFERENCE.md / the integration
  workflow for install). **Deferred:** resource-level `dependsOn` edges.

  **Entrypoint.** Each area is built from its *own* module, resolved on disk in the clone
  regardless of which files changed in the window: Bicep builds `<area id>/main.bicep`
  (never `<area>/<child>/main.bicep`, so a child-only change still builds the parent);
  Terraform runs in the area dir and reads its whole `*.tf` set. A path missing from the
  checkout (deleted in the window) and any file under a nested `examples/`/`tests/` subtree
  (e.g. `modules/sub/examples/x/main.tf`) are never the entrypoint. Only when the area id is
  not a directory (a graphify `community:<n>` area) is the shallowest existing match among
  the area's `paths` used. No entrypoint on disk ⇒ `edges_status: skipped`.

  **Visible gaps.** Per-module builds run in parallel, each bounded by a generous
  per-subprocess timeout (a healthy build finishes well under it; the bound only trips a
  genuinely hung process) and retried once. Each area carries `edges_status` ∈
  `{resolved, timeout, failed, skipped}` and `code_graph.edge_extraction` carries the aggregate
  `{ "resolved", "timeout", "failed", "skipped" }` counts — so a build that timed out or failed
  is a **visible, counted gap**, never mistaken for a module with no dependencies. `resolved`
  with an empty `edges` list genuinely means "no inter-area dependencies"; `timeout`/`failed`
  mean "not determined" (re-run to resolve). The integration gate reds when the unresolved rate
  is non-trivial, so an incomplete graph is re-run rather than silently shipped.
- **code_owners** `[ { "pattern": "<path|glob>", "owners": ["login", ...] }, ... ]` —
  parsed from the clone's CODEOWNERS (`.github/`/root/`docs/`), one rule per line **in
  file order** (duplicates kept). `@org/team` and `@user` are kept as logins (the `@`
  stripped), emails as-is. Blank/`#` lines, inline `# comments` and GitLab `[Section]`
  headers are dropped; a pattern with no owners is kept with `owners: []` (it
  un-assigns ownership). Matching follows GitHub's CODEOWNERS rules
  (`derive.codeowners_owners`): leading or middle `/` anchors at the root, trailing
  `/` = directory, `*`/`?` within a segment, `**` across segments, a slash-less
  pattern matches at any depth, a matched directory covers everything under it
  (except an anchored `dir/*`, which covers only direct files), `!` negation is
  unsupported, and **the last matching rule wins** (no union). **Area ownership**
  (`derive.area_owners`): a directory area is owned by the owners of the last rule
  that matches the area directory itself (`<area>/`), so a file-only rule such as
  `*.tf` does not own the area even though it may own files in it; a graphify
  `community:<n>` area is owned by the union of its files' owners. Readers also
  accept the legacy `{pattern: owners}` dict (stores written before #65); that form
  was stored key-sorted, so its file order — and therefore last-match-wins — is
  only as good as the sorted order until the repo is re-gathered.
- **label_taxonomy** `{ "<facet>": { "<namespace>": ["label", ...] }, "source":
  "auto|config|merged" }` — auto-detected structured label namespaces mapped to
  facets (`area`/`priority`/`status`/`lifecycle`/`kind`/`class`), with optional config
  override/extend. Only `type:`/`kind:` namespaces form the `kind` facet; AVM's
  `Class:` (Resource/Pattern/Utility Module) is a scope facet `class`, recorded but never
  used for kind. Degrades to `{ "source": "auto" }` (no facets) on unstructured labels.
- **issues[]/prs[]** gain **facets** `{ area, priority, status, lifecycle }`
  (each the first matching label or null). **issues[]** gain **kind** ∈
  `feature|module-request|bug|idea|question|docs|other` (native issue type →
  label kind (`type:`/`kind:` facet labels, then a bare label that is exactly a kind word,
  e.g. `bug`/`enhancement`/`documentation`) → template filename → **title** heuristic → other).
  Matching is on whole words, never substrings; a bare "module" is not a module request
  (only "module request"/"module proposal" are); issue bodies are never read. In the
  title (#101) a bare "support" is not a question ("Add support for X"; only "support
  request" is — as a type/label/template value `support` still is), and a `?` makes a
  question only when the title starts with an interrogative (`how`, `is`, `can`, …) and
  holds a `?`, or — when no kind phrase matched — ends with a `?` right after a word
  ("Bug: crash when name contains ?" is a bug).
- **artifacts[].code_area** and **feature_deltas[].area** are **populated** when the
  path resolves to a `code_graph` area; null otherwise.
- **trains[].code_areas** — the distinct areas of a train's commits' files.
- **modules** `{ "<area>": { "commits", "prs", "files_changed" } }` — per-area
  activity counts across the window's commits.
- **people[].modules / people[].areas** — the areas a person authored (their
  commits' files) or reviewed (their reviewed PRs' areas), over this view's records
  (this repo, this window; issue #88). In the project view they are
  `"<repo>::<area>"`, unioned over members.
- **people{}** keys are GitHub logins (see `commits[].author`). An entry keyed by an
  unresolved commit author's git name carries `login_resolved: false` (absent on every
  other entry); `is_bot` also recognises git-name forms (`GitHub Actions`, `Dependabot`,
  `Renovate Bot`, a `[bot]` name, and those with the `" (git)"` suffix).
- **diagrams{}** now also maps `contributor_graph` (Mermaid `flowchart`,
  people↔code-area edges) and `kind_breakdown` (Mermaid `pie`, issues by kind).
- **Symbol/comment artifacts** carry `before`/`after`/`detail` on feature_deltas
  (see `symbol_events` above), diff-local from a `git log -p` walk.
- **Deferred:** resource-level `dependsOn` edges.

## Train significance, effort & the per-train slice

- **trains[]** carry `significance`, `tier`, and `effort` (see the top-level `trains`
  bullet above for the exact shapes and tunables).
- **forecast** — top-level `forecast` block (see the engagement/forecast fields above).
- **diagrams.train_flowcharts** — the per-deep-train `.mmd` map (see the diagrams note above).
- **`slice_train(bundle, train_id)`** — a pure, read-only, bounded helper (not a bundle
  field; called by the report ranker and future sub-agents). Returns a self-contained dict:

  ```
  {
    "train":          { id, kind, outcome, significance, tier, effort,
                        code_areas, evidence, issues, pr_outcomes },
    "issue":          { number, title, body*, url, labels, kind,
                        comments*:[body*], comments_overflow,
                        lifecycle:[{event, actor, created_at, label}],
                        lifecycle_overflow, reopen_count } | None,
    "prs":            [ { number, title, body*, state, merged, created_at,
                          merged_at, url, reviewers:[login], review_decision,
                          review_comments*:[body*], review_comments_overflow,
                          comments*:[body*], comments_overflow,
                          review_rounds:{count, states} | None,
                          reviews:[{author, state, submitted_at, body*}],
                          reviews_overflow,
                          lifecycle:[{event, actor, created_at, label}],
                          lifecycle_overflow, reopen_count } ],
    "commits":        [ { sha, message*, author, date } ],
    "feature_deltas": [ ...this train's deltas only... ],
    "feature_deltas_diff_overflow": <int>,
    "symbol_moves":   [ ...moves whose from/to artifact is in this train's deltas... ]
  }
  ```

  (`*` = text-capped: any body/message/comment body truncated to `SLICE_TEXT_CAP` = 1500
  chars with a `…[+N chars]` marker. Each comment list, `reviews` and `lifecycle` is
  capped at `SLICE_COMMENTS_KEPT` = 6 entries, overflow count in `<key>_overflow`.
  `reviews` are the PR's review submissions; `lifecycle` the allowlisted timeline
  events (id/url dropped); `reopen_count` how often the issue/PR was reopened;
  `review_rounds` the PR's `{count, states}` or `None` when it has no counted review.
  `feature_deltas` file-level entries may carry a bounded `diff`; the combined diff
  chars kept in one slice never exceed `SLICE_DIFF_CAP` = 6000: walking the deltas in
  order, a diff is kept only if it fits in what is left of the budget, otherwise it is
  omitted (the delta is still listed) and counted in `feature_deltas_diff_overflow`.)
  `issue` is the train's `root_issue`; the train's other closed issues are listed by number
  in `train.issues`, and `prs` holds every attempt (its own outcome in `train.pr_outcomes`).
  Raises `KeyError` on unknown id. Does not mutate the bundle. The key lists above are
  checked against `slice_train`'s output by `test_link_small_fixes.py`.

## People profile, recognition halls, flow & blockers

These are **enrich-time projections** computed by `link.enrich` over the
materialized bundle (siblings of `forecast`/`modules`) — not stored person-node
data. The `people` projection (`{modules, areas, is_bot[, login_resolved:false]}`, the stored
person nodes limited to the view, see "Window scope") is what `enrich` MERGES the profile
fields onto. Each figure counts only the bundle's own records; the project view sums them
across members (see "Project view: `people` and `halls`").

- **people[]** — each `{login: {...}}` entry, in addition to its stored
  `modules`/`areas`/`is_bot`, gains a window-derived profile
  (`annotate_people_profile`):

  | field | meaning |
  |-------|---------|
  | `prs_authored` | PRs with `author == login` (int) |
  | `prs_merged` | of those, PRs with truthy `merged` (int) |
  | `merge_rate` | `round(prs_merged/prs_authored, 3)`, `null` when 0 authored |
  | `prs_reviewed` | PRs where login is in `reviewers` (int) |
  | `commits_authored` | commits with `author == login` (int) |
  | `issues_opened` | issues with `author == login` (int) |
  | `review_latency_days` | median, over reviewed PRs, of (login's earliest `reviews[].submitted_at` − `pr.created_at`) in days; `null` when no timestamped review |
  | `first_seen` / `last_active` | min / max ISO date (`YYYY-MM-DD`) across the login's activity (PRs authored/merged-by, commits, issues, reviews); `null` when none |
  | `examples_authored` / `docs_authored` / `symbols_authored` | distinct artifacts with an `add` lifecycle event by this login, bucketed by `kind` (example; doc+readme; symbol) (int) |
  | `authored_then_removed` | distinct artifacts this login added whose `status` ∈ `{removed, dropped, replaced}` (int) |
  | `stale_owned` | distinct areas in a stalled train's `code_areas` that this login owns under `code_owners` (`derive.area_owners`: last rule matching the area directory) (int) |

  Counts are always present (ints, may be 0); `merge_rate`/`review_latency_days`/
  `first_seen`/`last_active` are present as `null` when N/A. Deterministic.
- **halls** `{ "fame": [ { login, score, prs_merged, prs_reviewed,
  commits_authored, areas } ] }` (`build_halls`) — **recognition only.** `fame`
  ranks NON-bot logins by footprint `score = prs_merged*2 + prs_reviewed +
  commits_authored`, keeps `score > 0`, sorts by `(-score, login)`, takes the top
  10 (else `[]`). **`halls.internal`/`shame`/`blame` are intentionally NOT built**
  — per the "recognition, not blame" stance.
- **Blocker state.** `issue["blocking"]` / `issue["blocked_by"]` (set by
  `extract._attach_blocks`) are the **raw stored relation**, whatever the state of
  the issues at either end. `flow`, `blockers` and `diagrams.blocker_graph` (#101)
  apply an **open-only** filter on top, using the `state` of the
  bundle's own issues: an issue at the other end counts only when it is in the
  bundle with `state == "open"`. A number **absent from the bundle** has **unknown**
  state; it never counts as open (gather pulls every open issue of the repo, so an
  issue it did not gather was not open at gather time, or is not an issue of this
  repo), and `flow` lists such blockers separately in `blocked_by_unknown` rather
  than dropping them silently.
- **flow** `[ { number, url, state, age_days, reactions, blocked_by:[num],
  blocked_by_unknown:[num], signals:[str] } ]` (`build_flow`) — one entry per OPEN
  issue classified into a non-healthy **pathology** (healthy issues are omitted to
  stay lean). `blocked_by` holds only the issue's **open** blockers (sorted,
  unique); closed blockers are dropped; `blocked_by_unknown` holds blockers absent
  from the bundle (always present, `[]` when none). `state` is the pathology, in
  precedence order (first match wins):
  - `blocked` — at least one **open** blocker (`blocked_by` non-empty). An issue
    whose blockers are all closed or unknown is not `blocked` and is classified
    by its own signals below.
  - `upvoted_but_ignored` — `+1` reactions `≥ FLOW_UPVOTE_MIN` (5) AND stale
    (`updated_at` `≥ FLOW_STALE_DAYS` = 30 days before `ref_date`) AND no open
    linked PR.
  - `traction_then_abandoned` — `≥ FLOW_TRACTION_MIN` (5) comments AND stale AND
    no open linked PR.
  - `hung` — `age_days ≥ FLOW_HUNG_DAYS` (60) AND stale AND no open linked PR. An
    old issue with recent activity (updated < 30 days before `ref_date`) is not
    `hung`; neither is one without `updated_at` (recency unknown, so not stale).
  An issue with an OPEN (unmerged) linked PR (via PR `closes`/`crossref_issues`)
  is exempt from the activity-starved pathologies (work is in flight). `age_days`
  is `null` when the issue has no `created_at`. `signals` is a human-readable
  list (`"blocked by N issue(s)"` counting open blockers only, `"N blocker(s) of
  unknown state"`, `"N reactions"`, `"stale Nd"`, `"N comments"`, `"open Nd"`),
  each included only when relevant. `age_days`/staleness use `meta.ref_date`
  (fallback `meta.to`). Sorted by `number`. Empty `[]` when all open issues are
  healthy.
- **blockers** `[ { number, url, blocks_count, blocks:[num] } ]`
  (`build_blockers`) — **open** issues ranked by **how many other open issues each
  one blocks** (`blocks_count`, i.e. out-degree in the blocker→blocked graph read
  off `issue["blocking"]`, restricted to open issues). A closed blocker is not
  listed; `blocks` is the sorted unique set of blocked issues that are in the
  bundle with `state == "open"` (closed and unknown-state dependants are not
  counted); a blocker left with no open dependant is omitted. Sorted by
  `blocks_count` desc then `number`. Empty `[]` when nothing open blocks anything
  open.
