# Bundle Schema

The bundle is a single JSON object. **The skill is store-only:** the bundle is no
longer a produced file — `gather` folds its facts into the journey-graph store
(see `STORE.md`), and the reader stage materializes this bundle shape as a
**transient view** via `extract`. This document remains the contract for that
view: the **gather**/fold path supplies `meta`, `commits`, `prs`, `issues`,
`workflow_stats`, `releases`, `milestones`, `sprints`; the **link** step
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
  `stale_owned`, `own_pr_commits`, `prs_opened`; `derive.PROFILE_COUNT_FIELDS`) are
  **summed** (a member that omits `prs_opened` contributes its `prs_authored`, which it then
  equals; the project people carry `prs_opened` when any member does); `merge_rate` is
  recomputed from the summed `prs_merged / prs_authored`; `review_latency_days` is the
  median of **every** member's latency samples (not a median of member medians);
  `first_seen` is the earliest and `last_active` the latest; `modules` / `areas` are
  `"<repo>::<area>"` (like the project `modules` keys, so two members' same-named areas
  stay apart); `is_bot` is OR'd and `login_resolved: false` when any member says so.
  `halls.fame` is rebuilt from the merged people (same rule as a bundle's), so it ranks
  on project-wide figures.
- **Project view: `trains` (project trains).** `view["trains"]` is
  `[{ id, kind, outcome, repos:[repo], prs:[qid], issues:[qid], commits:[qid],
  code_areas:["<repo>::<area>"], tickets:[str], evidence:[ref + repo] }]`, sorted by `id`
  (`digest.build_project_trains`). Each is the union of the member trains (`trains[]` of
  `members[].bundle`) whose anchors fall in one connected group of the cross-repo spine
  (`closes` / `cross_ref` edges between members), plus any member PR / issue of that group
  that has no train of its own. A `closes` edge from a member PR to an issue that PR's own
  member train does not list (a rejected attempt's other issues, #97 final gate) is not
  followed, so a project train of one member is exactly that member's train. **Ids are qualified:** a PR is `<project>/<repo>#pr-<n>`, an
  issue `<project>/<repo>#issue-<n>`, a commit `<project>/<repo>#<sha>`, where `<repo>` is the
  store's repo key (`owner/name` for a manifest member). `id` is `"ptrain-" +` the smallest
  qualified PR / issue id (so `ptrain-<project>/<repo>#issue-<n>` or `#pr-<n>`); the member's
  own single-repo train for that anchor is `train-issue-<n>` (or `train-pr-<n>`). `repos` are
  the sorted repo keys the train reaches (it can name a member outside a `--repo` subset when a
  spine edge reaches it). `outcome` is the best member-train outcome (shipped > in_flight >
  rejected > abandoned); `kind` is that of the member train with the lowest anchor among those
  with a root issue (else among all). `code_areas` (#112) is the sorted union of the member
  trains' `code_areas`, each qualified `"<repo>::<area>"` like the project `modules` keys and
  people `areas`. `evidence` is the member trains' evidence refs in member order, each with
  `repo` added. `tickets` is described under `related_work` below (`[]` unless ticket parsing
  is on). A project train has **no** `tier`, `significance`, `effort`, `root_issue`,
  `pr_outcomes` or `contributing_prs`: read those from the member train.
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
as bare SHAs in `trains[].commits`; a train slice's `commits[]` rows and every
`feature_deltas[]` row carry the commit's `url`,
`https://github.com/<meta.owner>/<meta.repo>/commit/<sha>`.)

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
  that commit is the last commit of the walked branch (first-parent) whose committer day (UTC)
  is on or before `to` — the current month's head, or for a past window re-gathered later the
  mainline as it was then (read from a temporary worktree; the clone stays on its branch
  head) — or `"head"` when the bounded clone does not hold that commit (a window with no
  commit in it or its margin, before later ones), so the structure is the head's; both `null`
  without a clone. `clone_sha` is provenance only: nothing checks it out again or rebuilds
  from it.
  **`clone_mode`** (`fresh` | `fetched` | `depth1-fallback` | `none`) records how gather
  obtained the clone (see `gather.prepare_clone`), and **`boundary_dropped_commits`** lists
  the in-window commit shas whose diffs the shallow boundary dropped (the code history is
  incomplete for them). Since #107 fold stores both per gathered window and a view carries
  them (omit-when-absent): `clone_mode` and `structure_as_of` only for a view of exactly one
  gathered window,
  `boundary_dropped_commits` as the sorted union over the gathered windows wholly inside
  the view, omitted when empty. **`closing_refs_source`** (`graphql` | `keywords`, issue
  #105) is bundle-only: where the PRs' `closes` came from — GitHub's
  `closingIssuesReferences` merged with the closing keywords, or the keywords alone because
  GraphQL was unavailable (gather prints which on stderr). **`project_board_truncated`**
  (#109, bundle-only, present only when it applies) lists each Projects v2 board whose items
  were cut short: `[{board, kept, max_items, reason}]`, `reason` one of `item_cap`
  (`ACTIVITY_BOARD_MAX_ITEMS`), `page_guard` (200 pages), `error` (a page request failed),
  `empty_page` (a page claimed more items but held none) or `no_board_id`; items past `kept`
  carry no `board_status` / `iteration`. (Store-only — see `STORE.md`;
  this bundle shape is the transient view `extract` materializes from the store, not a
  produced file.)
  A view (`extract` / `view.py` / each `digest.py` member bundle) carries `owner, repo, from,
  to, base_branch, branches` (+ `clone_sha`, `state_as_of`, `records_as_of`, `gathered_at`,
  `facts_windows` when they apply),
  **`period`** = the requested `{from, to}` and **`ref_date`** (issue #75).
  `period` is the window `compute_buckets` and `build_forecast` limit to (`link.window_period`:
  `period`, else `{from, to}`), so items the store holds from other windows — or old items
  gather pulled because they were updated in the window — fall in no bucket. `ref_date` frames
  milestones, sprints, forecast age and flow staleness: the view's `--ref-date` if given, else
  the gather `--ref-date` stored for exactly this window, else `to`.
  **`state_as_of`** (issue #85, omit-when-absent) is the `to` of the repo's latest gathered
  window, present only when that is after the view's `to`: the view is of an earlier window
  than the store's latest. Its meaning is unchanged; since #97 a report view projects
  its PR/issue records back to its `to` (below), so what is still as the latest gather saw
  it is only what GitHub keeps no history of (title, body, labels, milestone, assignees,
  closing references, board status).
  **`records_as_of`** (issue #97, omit-when-absent) says the view's records were projected
  to the end of its window: `{"instant", "prs", "issues"}` — the instant (`to`'s last
  second, UTC, for a date; `to` itself for a timestamp) and how many PR / issue records of
  the view carry an `as_of` marker. Absent when no record holds anything later than `to`
  (a current window, or a store gathered at its end), so such a view is unchanged. A
  `digest.py` project view's `meta.records_as_of` is `{"instant", "members": [repo]}`.
  **`gathered_at`** (validation finding 8, omit-when-absent; present only with
  `records_as_of`) is when the view's PR / issue records were gathered: the latest
  `fetched_at` of their store nodes (the time the fold that last wrote each one ran, at the
  end of its gather), an ISO timestamp. What GitHub keeps no history of — title, labels,
  milestone, board status ("Not projected", below) — is as of that time, not of the window's
  end. A month gathered late into its own store has `records_as_of` and `gathered_at` but no
  `state_as_of`; the report's caveat line reads both (`report-template.md`, GATHERED AFTER
  THE WINDOW).
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
  gather collects for a window, applied to each record's state as of the window's end
  (#97, see "As of the window's end"; `extract` with `as_of_end` False, as the trust check
  uses it, applies it to the latest stored state):
  `prs` merged in the window, closed (unmerged) in it, open and created by its end, or
  created by its end and merged / closed after it (#97, `gather.keep_pr_for_window`);
  `issues` closed or updated in it, open and created by its end, created by its end and
  closed after it (#97), or closed / cross-referenced
  by one of the view's PRs (**train context**: an issue from another window can be here; a PR
  never is; an issue created after the window's end never is, projected or `--as-stored`); `commits` dated in it and the `code_events` / `symbol_events` of those commits;
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
  `facts_windows` describes (#94); a store folded before #94 gives the latest gather's.
  `newcomers` is the gathered window's own, or merged over the gathered windows inside the
  view (see "First-time contributors"). Not
  windowed: `milestones`, `sprints` — current structure, as of the latest gather. On a
  store holding only the viewed window every stored record is in the view; a view spanning
  several gathered windows is their union.
- `commits` — `[{ sha, parents, author, author_name, login_resolved, login_source?, date,
  committed_at?, message, files, pr }]` (`pr` set by link).
  `date` is the **UTC calendar day of the committer timestamp** (#109; before it, the
  committer's local day, `%cd`), so commits are windowed on the same UTC calendar as PRs and
  issues: a commit at `2026-10-31T20:00-08:00` is dated `2026-11-01`, in November with the PR
  merged at that instant. The walk windows in Python on it (`window_records`) rather than via
  git `--since/--until`, which prunes valid in-window commits across committer timezones.
  `code_events[].date` and `symbol_events[].date` are the same day of the same commit, so every
  record of one commit lands in one window. `committed_at` (#82) is the full committer
  timestamp (`%cI`) in UTC, `YYYY-MM-DDTHH:MM:SSZ`; it orders the `timeline`. It is absent on
  commits gathered before #82 (commits are never backfilled, #113).
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
  PRs, issues, comments and reviews add `author_type` when GitHub's account type for the
  author is not `User` (`Bot`: an automation account, whatever its login says; records
  gathered before it lack it — a re-gather fills it).
  **`closes`** (#62, #105) = the same-repo issues the PR closes: the closing keywords in its
  title/body (text inside an HTML comment `<!-- … -->` is not read: PR templates carry
  examples such as `Fixes #123` there, #97; a store gathered before that needs a re-gather to
  drop them), in text order, then — when `meta.closing_refs_source` is `graphql` — every other
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
  commits, participants, stalled, idle_days, last_activity_at, slow_to_land},
  evidence:[ref] }]`.
  **Membership (#93).** Every PR that targets `meta.base_branch` — merged into it, open, or
  closed unmerged — is in exactly one train, whatever its own outcome. PRs are joined by the
  issues they **close** (`closes`): a train is a connected group of PRs and the issues they close,
  so two PRs closing the same issue (a rejected first attempt and the reworked one that shipped)
  are one train, and every issue a train's PRs close is in its `issues` (sorted) and carries the
  train id on its bucket ref (a Development-sidebar link counts once gather read it from
  GitHub, #105). A mention or timeline cross-reference (`crossref_issues`) never
  makes an issue a member or a root: a tracking issue that mentions three PRs leaves them three
  trains. A `closes` number that is one of the bundle's PRs is not an issue (GitHub closes no
  PR by keyword): it neither anchors nor joins a train (#110). `pr_outcomes` maps each PR number (a string key) to that PR's own `"merged"`, `"open"`
  or `"rejected"` (closed unmerged), so the narrative can tell attempts apart.
  **A closing reference takes effect only when its PR merges (#97 final gate).** Only merged
  and open PRs join issues into a train and anchor it. A rejected PR that closes an issue of
  such a train is listed in it as a rejected attempt (`prs`, `pr_outcomes`, its commits, and
  its reviewers / participants in `effort`) but adds none of its other issues and never
  changes the train's `root_issue`, `issues`, `kind`, `outcome`, `effort.opened_at` or
  `effort.elapsed_days`; when its issues touch several such trains it joins the one with the
  lowest of those issue numbers. Rejected PRs whose issues are in no merged/open train join
  each other over their issues, as before (a `"rejected"` train). So an issue only a rejected
  PR named beside a shipped one stays out of the shipped train (and its bucket ref carries no
  train). The order of the records never changes the result.
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
  `created_at` among them; `merged_at` = the latest `merged_at` of its merged PRs, else
  `null`; `elapsed_days` = whole days from `opened_at` to `merged_at`, else `null`).
  **Stall flags (#112), by `outcome`** (threshold `TRAIN_STALL_DAYS` = 21, strict `>`):
  - `in_flight` — `last_activity_at` is the latest activity timestamp recorded for the
    train: over every member PR and issue, its `created_at`, `updated_at`, `closed_at`,
    `merged_at`, `comments_list[].created_at`, `review_comments[].created_at`,
    `reviews[].submitted_at` and `lifecycle[].created_at`, and every train commit's
    `committed_at` (else `date`) — `null` when none is recorded. (Train commits are mainline
    commits, so a push to an open PR's branch counts through the PR's `updated_at`.)
    Automation is not activity (#97): a comment or review by a bot account does not count,
    nor does an `updated_at` within two minutes of one — a reminder bot that comments on
    every stale pull request would otherwise leave none stalled.
    `idle_days` = calendar days
    from that timestamp's date to the reference date (`meta.ref_date`, else `meta.to`),
    floored at 0 (activity recorded after the reference date — a `--ref-date` before the
    window's end — is not idleness); `null` without a
    timestamp or a reference date. `stalled` = `idle_days > TRAIN_STALL_DAYS`.
    `slow_to_land` is `false`.
  - `shipped` — `slow_to_land` = `elapsed_days > TRAIN_STALL_DAYS` (it took long to land);
    `stalled` is `false` and `idle_days` / `last_activity_at` are `null`, even when an open
    follow-up PR is idle.
  - `rejected` / `abandoned` — `stalled` and `slow_to_land` are `false`, `idle_days` /
    `last_activity_at` `null`.
  A train without `outcome` (hand-built) is treated as `shipped` when a PR merged, else as
  `in_flight` when a PR is open. All fields degrade to null/0/false when data is thin.
  Tunables: `TRAIN_KIND_WEIGHTS`, `TRAIN_SIGNIFICANCE_TOP_N`, `TRAIN_SIGNIFICANCE_FLOOR`,
  `TRAIN_STALL_DAYS`.
- `buckets` — `{ shipped:[ref], in_flight:[ref], rejected:[ref], next_candidates:[ref] }`
  (all four buckets are classified — see below).

### As of the window's end (issue #97)

PR / issue records are stored latest-wins, so a window gathered after it ended — or viewed
after a later gather — would read each record as the latest gather saw it. A **report view**
(`view.py`, each `digest.py` member: `extract(..., as_of_end=True)`) instead projects every
PR / issue record that carries anything later than the window's end back to that end
(`extract.record_as_of`), **before** the window-scope rule picks the view's records. The
instant is `to`'s last second (UTC) for a date, `to` itself for a timestamp; "later" is the
window predicate's own rule.

- **Existence.** A record created after the instant is in no view of that window, not even
  as train context.
- **State.** A PR merged at or before the instant keeps its state (a merge is final). A PR
  merged after it is not merged (`merged: false`, `merged_at` / `merged_by` null). Open or
  closed then comes from the record's `closed` / `reopened` `lifecycle` events when it has
  any: the last one at or before the instant decides (closed and reopened before it: open;
  closed → reopened → closed with the last close after it: open; closed before it and
  reopened after: closed, `closed_at` = that close). Without such events, `closed_at`
  decides: at or before the instant closed, after it (or never) open. An issue keeps its
  `state_reason` when its closing at the instant is the stored one; an earlier closing
  that was later undone has no recorded reason (`state_reason: null`, listed in
  `not_projected`, so such an issue is in no shipped / rejected bucket); an issue open at
  the instant has `reopened` when a recorded reopening precedes it, else `null`.
- **Activity.** `comments_list`, `review_comments`, `reviews` and `lifecycle` entries dated
  after the instant are dropped (`reviews` / `lifecycle` are left out when none remain);
  `comments` and `review_comments_count` lose the dropped ones; `reviewers` and
  `review_decision` are re-derived from the remaining reviews by gather's own rule (a PR
  whose record has `reviewers` but no review submissions keeps them, listed in
  `not_projected`); an issue's `open_high_activity` is re-derived (its `reactions` are as
  stored). `review_rounds` / `reopen_count` are derived by `link` from what remains.
  **Dismissed reviews:** GitHub rewrites a dismissed review's `state` to `dismissed` and
  keeps its `submitted_at`, and the store records no dismissal date, so a review submitted
  by the instant but dismissed later reads `dismissed` in the projected record. When a
  remaining review is `dismissed` its state at the instant cannot be established: the
  stored `review_decision` is kept (not re-derived) and `review_decision` and
  `review_rounds` are listed in `not_projected` (`reviewers` is still re-derived: the
  reviewer submitted by the instant either way).
- **`updated_at`** becomes the latest timestamp left at or before the instant (created /
  closed / merged time, comments, review comments, reviews, lifecycle events): the item's
  last recorded activity then, so the bucket rule (`in_flight` = open and updated in the
  window) and the train idle / stall rule work unchanged. Activity GitHub records nowhere
  else (a label change, a push) is not counted.
- **Not projected** (GitHub keeps no history of them; as stored): `title`, `body`,
  `labels` / `facets`, `kind` / `issue_type`, `milestone`, `assignees`, `base` / `head`,
  `closes` / `closing_issues` / `crossref_issues` / `timeline_xrefs` / `external_refs`,
  `blocks` (and the resolved `blocking` / `blocked_by`), `reactions`, `board_status` /
  `iteration`, and `author_association` (GitHub's value when gathered — e.g. a
  `FIRST_TIME_CONTRIBUTOR` then may be a `CONTRIBUTOR` now; use `newcomers` for who was new
  in the window). An item open at the window's end but deleted or transferred since is not in
  the store, so not in the view.
- **Marker.** Each projected record carries **`as_of`**: `{"instant", "fields": [the keys
  that changed, sorted], "not_projected": [sorted: the stored fields of the list above that
  the record has, plus `state_reason` / `reviewers` / `review_decision` / `review_rounds`
  when they could not be projected]}`;
  the view's `meta.records_as_of` counts them. A record with nothing later than the instant
  is exactly as stored and has no `as_of`; a view with none has no `meta.records_as_of`.

Every consumer reads the projected records: `link` (trains and their outcomes, buckets,
effort — `stalled`, `idle_days`, `last_activity_at`, `slow_to_land`, reviewer / participant
counts —, timeline, forecast, review rounds), `derive` (people profiles and halls, flow,
blockers), the view's `people` (who contributed by the instant) and `digest.py`'s project
trains, shipped list and people. Commits, code / symbol events, artifacts, releases and the
per-window facts are dated facts already windowed and are not touched. The trust check
(`validate.py`) self-sources its bundle with `as_of_end` False: it re-derives edges and
people from the **full** stored records (a PR merged after some window's end has its
`merged` edge). `spotlight.py` reads the stored graph directly and is not projected: its
train outcomes and contribution counts are as stored, also with `--from` / `--to`.
`view.py --as-stored` writes the records as stored (the form `validate.py --bundle` compares
with the store). A gather of a past window keeps the PRs merged / closed in it, those open
at gather time and created by its end, and those open at the window's end and merged / closed since
(`gather.keep_pr_for_window`); it lists every issue updated since the window's start. The
window-scope rule agrees (both with and without `as_of_end`, and so `validate.py --bundle`):
a PR or issue created by the window's end and merged / closed after it is in the window.
A PR or issue created **after** the window's end is in no view of it (projected or as
stored, not even as train context), so gather no longer keeps one from its listings
(open PRs, open / recently updated issues; nor a closed issue last touched before the
window's start, which the updated-since listing's last page reaches) — an issue a kept PR
closes or cross-references is still kept as train context. A store gathered before that
may hold such records; `validate.py --bundle` does not expect people seen only on stored
records outside the bundle's window to be derivable from it (one INFO line), so it passes
without a re-gather.
`meta.schema_version` is unchanged (3): `as_of`, `records_as_of` and `gathered_at` are
additive keys.

## First-time contributors (`newcomers`)

Validation finding 12: who is new this window. `people[].first_seen` cannot say it (the
earliest activity **inside the view**), nor can `author_association` (GitHub's value when
gathered, not projected). gather checks every author of a PR, and every reporter of an issue,
**created in the window** (bots by `derive.is_bot_login`, and GitHub's deleted-user `ghost`,
excluded) for any PR / issue of theirs in the repository created before the window's start,
with the search API (`repo:O/R is:pr|is:issue author:<login> created:<<from>`, `per_page=1`,
reading `total_count`; one call, or two when the first kind finds nothing). It is stored as a
per-window fact (`facts:` meta, like `workflow_stats`; STORE.md) and a view carries it as the
top-level key **`newcomers`**, omitted when no gather stored it (a store gathered before the
check; every fixture bundle):

```json
{"collected": true, "before": "2026-09-01", "candidates": 12,
 "new": [{"login": "octo", "first": "pr", "number": 7576,
          "url": "https://github.com/O/R/pull/7576", "created_at": "2026-09-02T16:16:55Z"}],
 "unknown": ["login-whose-check-failed"], "not_checked": ["login-over-the-cap"]}
```

- `before` — the window's first day: "new" means no PR / issue created before it.
- `candidates` — how many distinct non-bot authors created a PR / issue in the window.
- `new` — the newcomers (sorted by login), each with their **earliest** PR or issue created
  in the window (`first` "pr" | "issue").
- `unknown` — a check failed (an API error, a rate limit not waited out, `incomplete_results`);
  never guessed, never counted as new or returning. After three failures in a row the rest
  are `unknown` too.
- `not_checked` — candidates beyond `ACTIVITY_NEWCOMERS_MAX` (default 200; the earliest are
  checked).
- `{"collected": false}` alone — the window was gathered with `--no-newcomers`.

**First ever, returning or regular.** The search is sorted by creation date, newest first
(`sort=created&order=desc`, still `per_page=1`), so the one item it returns is the
candidate's most recent earlier PR / issue of that kind: its `created_at` is
`last_seen_before`. When that is more than `RETURNING_GAP_DAYS[0]` (90) days before the
candidate's first item of the window, the other kind is searched too (a more recent item of
that kind would shorten the gap), so the gap is measured to the most recent earlier PR **or**
issue; a regular contributor still costs one call. Each checked candidate is then one of:

- `new` (first ever: no PR / issue authored in the repo before `before`; the shape above);
- `returning` — `{login, first, number, url, created_at, last_seen_before, gap_days,
  gap_bucket}`: `gap_days` (whole days from `last_seen_before` to `created_at`) is more than
  90; `gap_bucket` is `over_3_months` (> 90 days), `over_6_months` (> 180) or
  `over_9_months` (> 270) — `gather.RETURNING_GAP_DAYS`, a month counted as 30 days;
  `gap_upper_bound: true` when the second kind's search failed (the true gap may be
  shorter);
- `regular` — `{login, last_seen_before, gap_days}` (gap of 90 days or less);
- `earlier_undated` (only when any) — earlier items exist but the answer carried no date.

`returning`, `regular` and `gap_thresholds_days` ([90, 180, 270]) are present on facts
gathered with this rule; a fact gathered before it has only `new` (its candidates with
earlier activity are not classified). "First" always means first PR or issue **authored**;
reviews and comments are not searched (see `prior_activity` below for what the store holds).

A view of exactly a gathered window carries the stored value verbatim (so `validate.py
--bundle` compares it like any per-window fact). A view spanning several gathered windows
merges those lying wholly inside it that collected one, not overlapping one another (by start
day, the longest first): `new` keeps each login's earliest entry, `unknown` / `not_checked`
are the unions less the logins found new, `candidates` is left out (a login can be a
candidate in two windows), and two keys are added: `windows` (the merged windows) and
`covers_view` (false when they leave days of the view uncovered — a newcomer in an uncovered
day is missing). `returning` / `regular` merge the same way, the earliest window's class
winning (a login new in August and returning in September is new to the quarter).
Multi-repo: per member bundle; a person new to one member may have worked in
another. Additive and omit-when-absent: `meta.schema_version` stays 3.

## The people layer (`affiliations`, `prior_activity`, `contributors`)

Who contributed — the maintaining organisation or the community — who is new or back after
a gap, and what each of them landed. Three keys, each omitted when its input is absent (a
store gathered before this layer; every fixture bundle), so other views are unchanged;
`meta.schema_version` stays 3.

### `affiliations` (raw, from the store)

The per-window fact gather writes (`gather.collect_affiliations`; STORE.md). For each human
person of the window — PR / issue authors, mergers, reviewers, commenters and commit authors
resolved to a login (bots, `ghost` and unresolved git names left out):

```json
{"collected": true, "home_orgs": ["Azure", "microsoft"], "home_names": ["azure", "microsoft"],
 "home_email_domains": ["microsoft.com"],
 "people": {"octo": {"affiliation": "maintainer_org", "signal": "author_association", "strength": "strong"},
            "mona": {"affiliation": "community", "signal": "profile_company", "strength": "weak"},
            "hubot": {"affiliation": "unknown", "signal": "none"}},
 "unknown": ["hubot", "login-over-the-cap"], "not_checked": ["login-over-the-cap"],
 "groups_unresolved": ["@org/secret-team"]}
```

- `affiliation` — `maintainer_org` (the organisation that owns the repository, or an
  organisation the operator lists as home), `community`, or `unknown`. **Never defaulted to
  community**: no signal means `unknown`.
- `signal` / `strength` — the first signal that decided, in this order:
  1. `override` (`operator`) — the operator's list (`--affiliation LOGIN=…`,
     `--affiliation @org/team=…`, manifest `affiliation.overrides`);
  2. `author_association` — GitHub's value on the person's own PR / issue / comment records of
     the window, **at gather time**: `OWNER` / `MEMBER` (`strong`), `COLLABORATOR`
     (`medium`). `MEMBER` means a member of the organisation that **owns the repository**
     (e.g. `Azure` for Azure/bicep-registry-modules), not of any other home organisation.
     `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR` and `NONE` decide nothing;
  3. `public_membership` (`strong`) — `GET /orgs/{org}/public_members/{login}` answered 204
     for a home organisation (404: not a public member — private membership is invisible);
  4. `profile_company` — the public profile's `company`, normalised (lower case, leading `@`
     dropped, punctuation removed, trailing "Corporation" / "Corp" / "Inc" / "Ltd" / "LLC" /
     "GmbH" / "PLC" removed: "@microsoft", "Microsoft", "Microsoft Corporation" all read
     "microsoft") and compared exactly with the home names (or any `@handle` in it): a match →
     `maintainer_org` (`medium`); a non-empty company that does not match → `community`
     (`weak`: self-declared and possibly stale); empty → no signal;
  5. `commit_email_domain` (`medium`) — a commit email of theirs in a home email domain
     (`users.noreply.github.com` never counts);
  6. none → `unknown` with signal `none`, or `lookup_failed` when a lookup could not answer.
     A failed lookup never lets a weaker signal decide `community` (a home-org public
     membership might have been missed); a weaker signal may still decide `maintainer_org`.
- `home_email_domain: true` — on any entry whose commit email was in a home domain (the only
  trace of an email that is kept).
- `unknown` — every person labelled unknown, plus `not_checked`.
- `not_checked` — people the API was not asked about: beyond `ACTIVITY_AFFILIATION_MAX`
  (default 300, the most active first) or after three people in a row whose lookups failed.
  They have no entry and count as unknown.
- `groups_unresolved` — `@org/team` overrides whose members the token could not list.
- `{"collected": false}` alone — gathered with `--no-affiliation`.

**With the people registry** (gather `--people PATH` or a manifest's `people`; next
section) every entry also carries, for the labels used in that window:

```json
"octo":          {"affiliation": "maintainer_org", "signal": "public_membership", "strength": "strong",
                  "name": "Octo Cat", "maintainer": true, "maintainer_signal": "merged_others_pr",
                  "maintainer_status": "active"},
"quiet-person":  {"affiliation": "unknown", "signal": "do_not_name"},
"not-yet-seen":  {"affiliation": "unknown", "signal": "not_checked", "maintainer": false,
                  "maintainer_signal": "none"}
```

- `name` — the public profile's display name as the registry holds it (an operator's own
  spelling wins); absent when the profile has none, or the person is `do_not_name`.
- `maintainer` / `maintainer_signal` — the maintainer property **for this window's
  repository** (see "The people registry"): `true` with the signal that shows write access
  (`operator`, `merged_others_pr`, `OWNER`, `merged_own_pr`, `COLLABORATOR`,
  `approving_reviewer`), or `false` with `operator` (the owner said so) or `none` (no
  evidence of write access in anything the registry has seen for that repository).
- `maintainer_status` — only when the registry holds maintainer evidence for the
  repository: its lifecycle at gather time, `active` / `lapsed` / `former` / `unknown`
  (see "The people registry"); `former` always comes with `maintainer: false`.
- signal `do_not_name` — the owner marked the person `do_not_name`: no name, no maintainer
  property, label `unknown`, no API call. Every output shows the login only.
- signal `not_checked` — with the registry, a person not looked up (over the cap, offline,
  after repeated failures) still has an entry, so their name and maintainer property reach
  the view; they are listed in `not_checked` and count as unknown as before.

**What is stored about a person:** the login (already in every record), the label, the
signal name, its strength, the `home_email_domain` boolean and, with the registry, the
display name and the maintainer property with its signal and status. Never the profile
company or any other profile text, never an email address or a domain of a non-home
address. `validate.py` refuses a stored `affiliations` value whose entries carry anything
else (or a `do_not_name` entry that carries a name or a maintainer property, or a
`maintainer_status` that is not one of the four, has no `maintainer` beside it, or is
`former` with `maintainer: true`).

### The people registry (`people.json` + `people-activity.json`, outside the store)

Two JSON files per workspace — one registry shared by every stream (default
`~/activity/people.json` and the generated `people-activity.json` beside it; USAGE §2.1) —
one entry per GitHub login, shared by every gather — all months, all repositories, all
streams — so a person is looked up once and refreshed rarely, and their roles and activity
are known across repositories (`people.py`; USAGE §5.7). `people.json` is the owner's to
read and edit; `people-activity.json` is generated. gather never needs them to read a
store, and no view reads them unless `link.py --people` asks for registry context.

```json
{"format": "activity-overview/people", "version": 1,
 "people": {"octo": {"login": "octo", "name": "Octo Cat",
                    "affiliation": "maintainer_org", "signal": "public_membership", "strength": "strong",
                    "maintainer_of": {"Azure/bicep-registry-modules": {"maintainer": true,
                        "signal": "merged_others_pr", "strength": "strong",
                        "first_evidence": "2025-02-03", "last_evidence": "2026-10-29"},
                        "Azure/bicep": "former"},
                    "first_seen": {"date": "2025-01-07", "repo": "Azure/bicep-registry-modules"},
                    "last_seen": {"date": "2026-10-31", "repo": "Azure/bicep-registry-modules"},
                    "looked_up_at": "2026-11-26", "source": "lookup", "tags": ["known"]}}}
```

| Key | Written by | Meaning |
|---|---|---|
| `login` | skill | the key repeated (logins match ignoring case) |
| `name` | lookup, or you | the profile's display name (from the same `users/{login}` response as the company signal) |
| `affiliation` / `signal` / `strength` | lookup, or you | the latest label decided from GitHub data (`author_association`, `public_membership`, `profile_company`, `none`) |
| `maintainer_of` | skill, or you | `{repo: evidence}` — write access per repository (`maintainer`, `signal`, `strength`, `first_evidence`, `last_evidence`); a plain `true` / `false` / `"former"` is yours |
| `tags` | you only | free-form (`known`, `mvp`, …) |
| `first_seen` / `last_seen` | skill | `{date, repo}` of the earliest / latest activity anywhere, as gathered (the global summary of the per-repository profile) |
| `looked_up_at` | skill | date of the last profile / membership lookup |
| `home` | skill | the home settings the label was decided against (home organisations, then names); reused only under the same settings |
| `source` | skill / you | `lookup` (written by the skill) or `operator` (yours: every decided field pinned) |
| `locked` | you only | `true`, or a list of `name` / `affiliation` / `maintainer_of` to pin |
| `do_not_name` | you only | `true`: login only, no label, no call, in every output |
| `note` | you only | free text |

**Operator edits win.** `source: "operator"` or `locked: true` pins `name`, `affiliation`
and `maintainer_of`; `locked: [...]` pins the fields listed; a plain `true` / `false` /
`"former"` in `maintainer_of` is the operator's for that repository. Pinned fields, `tags`,
`do_not_name` and `note` are never written by the skill; `first_seen` / `last_seen` and the
activity file always are. Any other key, a bad value or two keys that are the same login is
an error naming the entry (gather stops before touching the store); the file is never
reset. A file written before the activity profile is read as it is (the format stays
version 1: the profile only added optional fields); the next gather or seed adds them.

**The activity file** (`people-activity.json`; `people.py` "the activity profile"):

```json
{"format": "activity-overview/people-activity", "version": 1, "months_kept": 36,
 "repos": {"Azure/bicep-registry-modules": {"stream": "avm-bicep", "windows": {
     "2026-09-01..2026-09-30": {"source": "gather", "people": 81, "unresolved_commit_authors": 0}},
     "before": {"from": "2023-01-01", "to": "2023-09-30", "windows": 9}}},
 "people": {"octo": {"Azure/bicep-registry-modules": {
     "2026-09-01..2026-09-30": {"first": {"merge": ["2026-09-01", 7306], "comment": "2026-09-02"},
                                "last": {"merge": ["2026-09-30", 7428], "comment": "2026-09-24"},
                                "months": {"2026-09": {"merges": 40, "merges_others": 2, "comments": 10}},
                                "first_ever": "2026-09-01"}}}},
 "seen_before": {"octo": {"Azure/bicep-registry-modules": "2025-01-07"}}}
```

| Key | Meaning |
|---|---|
| `repos[repo].windows` | the coverage: every gathered window `FROM..TO` with its `source` (`gather` / `seed`), the people with an event in it and the commits whose author has no login |
| `repos[repo].stream` | the manifest project (or `seed --stream`) the repository belongs to: the per-stream roll-up |
| `repos[repo].before` | the range folded away beyond `months_kept` (36) months; a window inside it is not folded again |
| `people[login][repo][window]` | one contribution record: `months` {YYYY-MM: counts of `commits`, `prs_opened`, `prs_merged`, `reviews`, `approvals`, `approvals_merged`, `merges`, `merges_others`, `issues`, `comments`}, `first` / `last` {`commit`, `pr_opened`, `pr_merged`, `review`, `approval`, `merge`, `issue`: [day, number or sha]; `comment`: day}, `first_ever` (the newcomer search's `before`: no earlier PR or issue) |
| `people[login][repo].before` | the folded record (`months` {"before": counts}) |
| `seen_before[login][repo]` | the earliest day the person shows in stored records dated before a gathered window — never counted |

Only events dated inside a window enter its record; re-gathering a window replaces its
records (never counted twice); overlapping windows inside a month are not summed (the one
covering most of the month counts). Everything a reader sees is derived:
`people.repo_profile` (coverage, `observed_since`, firsts / lasts with URLs,
`first_pr_ever` / `first_issue_ever` only from the newcomer search, totals, `by_month`,
`rates` over the last 3 / 6 / 12 gathered months, `trend`, contributor `status` active /
returned / lapsed / unknown, `maintainer` with `status` active / lapsed / former / unknown
and the merges / approvals behind it), `people.cross_project` (every repository with role
and status, the repositories maintained, first / last anywhere, the per-stream roll-up) and
`people.writer_context` (what `people_context` carries). Thresholds: active within 183
days of the repository's latest gathered day, former beyond 548
(`ACTIVITY_PEOPLE_ACTIVE_DAYS` / `ACTIVITY_PEOPLE_FORMER_DAYS`), `unknown` when fewer than
90 days were gathered since the last action.

**At gather time** the registry is consulted first (a fresh entry costs no call), the API
fills the gaps (new people, then stale entries, at most `ACTIVITY_PEOPLE_REFRESH_MAX` = 50
refreshes per run, oldest lookup first; decided labels are fresh for 120 days,
`--people-refresh-days` / `ACTIVITY_PEOPLE_REFRESH_DAYS`, `unknown` ones for 30,
`ACTIVITY_PEOPLE_UNKNOWN_RETRY_DAYS`), the registry is updated (merged into the file as it
is on disk, atomically, under a lock, keys sorted), and the window's `affiliations` fact
records the labels used. **Views read only the fact**, so a report made last month is
reproduced exactly after the registry changes. `--people-offline` makes no API call:
the registry and the free signals decide.

**The maintainer property** (a role; affiliation is the employer — neither implies the
other, and a community maintainer is possible) is derived from records only, per
repository, strongest first: `operator` > `merged_others_pr` (merged a PR somebody else
authored: write access, `strong`) > `OWNER` (`strong`) > `merged_own_pr` (merged their own
PR: write access, `medium`) > `COLLABORATOR` (`medium`) > `approving_reviewer` (approved
at least 3 merged PRs of others in one window, or in one seeding pass, `medium`).
`MEMBER` is not a maintainer signal: it means a member of the organisation that owns the
repository (thousands of people for `Azure`), which is an affiliation. Evidence is kept:
once a person is a maintainer of a repository the entry keeps it, with `first_evidence` /
`last_evidence` (evidence dated after the gathered window is left out), until the owner
says otherwise — but it is no longer blindly current: its lifecycle status (above) says
whether it is recent, and a `former` maintainer is not counted as one.

### `people_context` (link `--people`, registry context)

Only when `link.py VIEW --people PATH` is run: what the people registry knows of the people
the report names (newcomers and hall rows of `contributors`, `halls.fame`, story trains'
participants, the authors of PRs that introduced a new module), beyond the window.

```json
"people_context": {
  "kind": "registry_context", "repo": "Azure/bicep-registry-modules", "as_of": "2026-10-31",
  "note": "from the people registry, not a fact of this window: ...",
  "registry": {"people_sha256": "…", "activity_sha256": "…", "latest_gathered": "2026-10-31"},
  "thresholds": {"active_days": 183, "former_days": 548},
  "coverage": {"from": "2026-08-01", "to": "2026-10-31", "windows": 3, "gaps": [], "gathered_days": 92,
               "months": ["2026-08", "2026-09", "2026-10"]},
  "people": {"octo": {
     "here": {"active_since": "2025-01-07", "active_since_basis": "seen in stored records dated before the coverage (not counted)",
              "active_since_is_first_ever": false, "observed_since": "2026-08-01",
              "prs_merged_last_6_months": 10, "months_counted": 3, "status": "active",
              "trend": "falling", "maintainer_status": "active"},
     "also_maintains": [{"repo": "Azure/bicep", "status": "lapsed"}],
     "also_contributes_to": [{"repo": "Azure/terraform-azurerm-avm-res-network-virtualnetwork", "status": "active"}],
     "first_anywhere": {"date": "2023-04-13", "repo": "Azure/bicep-registry-modules"},
     "streams": {"avm-tf": {"repos": 3, "maintains": 1, "totals": {"prs_merged": 12}, "first": "…", "last": "…"}}}}}
```

It is **not a fact of the window** and the trust check does not compare it: it is read only
on request, only from the registry's windows that end by the view's end (`as_of`), so
re-linking an old view after later gathers gives the same `people` (one exception: an
`active_since` taken from `seen_before` can move earlier when a later store holds older
records); the owner's own edits (a role set to `"former"`, `do_not_name`) apply as they
are now. The `registry` stamp says which registry state produced it. `months_counted` says how many gathered months the
"last 6 months" figure covers. A `do_not_name` person and a person the registry does not
know are left out. Without `--people` the key is absent and the view is unchanged.

A view of exactly a gathered window carries the stored value verbatim (`validate.py
--bundle` compares it). A wider view takes, per person, the entry of the latest gathered
window overlapping it (the label is the answer when that window was gathered), recomputes
`unknown` / `not_checked` and adds `windows` and `covers_view`.

### `prior_activity` (raw, from the store)

Only when the view's `newcomers` names a first-ever or returning contributor: what the
store holds of each of them dated before `newcomers.before`:

```json
{"before": "2026-10-01", "store_from": "2026-08-01",
 "people": {"octo": {"prs": 0, "issues": 0, "reviews": 1, "comments": 0, "earliest": "2026-08-31"}}}
```

`reviews` (review submissions), `comments` (PR conversation, review and issue comments),
`prs` / `issues` authored. It tells a long-time reviewer's first PR from a true first-timer.
**What the store can know:** only the records of its gathered windows (PRs and issues
updated in them, the open ones, and older ones those reached), so `0` means "none in the
store", not "none ever"; activity before `store_from` (the repo's earliest gathered window)
is mostly invisible.

### `contributors` (derived by `link.py`)

Built when the view carries a collected `affiliations` or `newcomers` fact. Facts, not
prose; every PR carries its URL.

```json
{"affiliation": {"home_orgs": [...], "people": {"maintainer_org": 8, "community": 0, "unknown": 222},
                 "signals": {"author_association": 8, "none": 221, "not_looked_up": 1},
                 "merged_prs": {"maintainer_org": {"count": 38, "share": 0.594}, "community": {...},
                                "unknown": {"count": 26, "share": 0.406}, "total": 64, "bots_excluded": 5},
                 "reviews": {...same shape...}},
 "community": {"people": 0, "prs_merged": 0, "prs_merged_numbers": [], "reviews": 0,
               "first_timers": [], "returning": [], "modules": []},
 "newcomers": [{"login": "mona", "class": "first_ever",
                "first": {"first": "pr", "number": 7558, "url": "…/pull/7558", "created_at": "…"},
                "seen_in_store_before": {"prs": 0, "issues": 0, "reviews": 0, "comments": 0, "earliest": null},
                "affiliation": "community", "affiliation_signal": "profile_company",
                "name": "Mona Octocat", "maintainer": false, "maintainer_signal": "none",
                "contributed": {"prs": [{"number": 7558, "url": "…", "title": "feat: add privatelink.redis.azure.net",
                                         "state": "merged", "class": "module", "headline": "module_change",
                                         "modules": [{"area": "avm/ptn/network/private-link-private-dns-zones",
                                                      "kind": "pattern", "status": "changed", "role": "changed"}],
                                         "changelog": [{"module": "…", "version": "0.7.4",
                                                        "items": ["Added Redis zone `privatelink.redis.azure.net`"]}]}]}}],
 "gap_thresholds_days": [90, 180, 270], "store_from": "2026-08-01",
 "hall": [{"login": "…", "score": 82, "affiliation": "…", "affiliation_signal": "…",
           "contributed": {"prs": [...], "prs_omitted": 29}, "reviewed": {"prs": 6, "modules": [...]}}],
 "inputs": {"module_layer": "module_changes"}}
```

- **`affiliation`** (with `affiliations`) — people per label, the deciding signals, and the
  window's PRs merged into the mainline (`module_changes.prs`) by their author's label and
  its reviews (each reviewer once per PR) by the reviewer's label; `share` is of `total`
  (the human-authored ones), `unknown` always its own bucket, `bots_excluded` the rest. A
  wider view adds `windows` / `covers_view`.
- **`affiliation.maintainers`** (only when the people carry the maintainer property, i.e.
  the fact was gathered with the registry) — `people` {maintainer, non_maintainer,
  unknown} (unknown: no property — `do_not_name`, or not a person of a gathered window),
  `signals` (maintainers per signal), `by_affiliation` {label: the same three counts}
  (maintainers vs non-maintainers within community and the home organisation, unknown
  separate) and `logins` (the maintainers); `by_status` {active | lapsed | former |
  unknown: [login]} when the fact carries `maintainer_status` (a `former` maintainer
  counts as a non-maintainer).
- **`community`** (with `affiliations`) — only people labelled `community` (never
  `unknown`): how many, their merged PRs (and numbers), reviews, the first-timers and
  returning contributors among them, and the module areas that received a community PR
  merged in the window.
- **`newcomers`** (with `newcomers`) — one row per first-ever or returning contributor
  (sorted by login): `class`, their first item of the window, the gap fields for a
  returning one, `seen_in_store_before` (`prior_activity`), their label, and `contributed`.
- **`hall`** — for each `halls.fame` entry: label, `contributed`, and `reviewed` {prs: how
  many of the view's PRs they reviewed, modules: the module areas of those merged in the
  window}.
- **`contributed`** — their PRs merged in the window (newest first), then their open ones;
  at most 10 (`prs_omitted`). Each: `state`, `class` (the module layer's PR class), and
  `headline`: `new_module` (the PR introduced a module that is new in the window; its row
  has `role: "introduced"` and `first_version` — e.g. a first PR ever that lands the new
  pattern module `avm/ptn/app/function-app` 0.1.0), `module_change`, `plumbing` (CI, docs
  or tests only: `class` says which), `release` (a version cut with no code change) or
  `unknown` (no commit of it in the view — an open PR: the store holds no file list for an
  open PR's commits). `modules` (at most 5, introduced first): area, kind (resource /
  pattern / utility / module / submodule / deployment), the module's status in the window,
  the PR's role; for a mechanical sweep only the modules whose source it changed, with
  `sweep`. `changelog`: the entries the PR's commits added to module changelogs, items
  verbatim (at most 5 per entry, `items_omitted`).
- **`inputs.module_layer`** — `module_changes` (the view's own, built from the gathered
  `module_notes`), or `code_events_only` for a view without them: the same build over the
  commits and file events alone (a module is `new` only from an added entry file; no
  changelog items, no tags).

**Halls by affiliation.** When the people carry `affiliation`, `halls` adds `community` and
`maintainer_org` — the same people ranked the same way as `fame` (top 10 each) within one
label — and `unranked_unknown` (how many people with a positive score are `unknown`). `fame`
is unchanged. **people[]** rows gain `affiliation` and `affiliation_signal` (non-bots; a
person the fact does not name is `unknown` with `not_checked` or `not_looked_up`) and,
with the registry, `name`, `maintainer` and `maintainer_signal`; so do the `halls` entries,
`contributors.newcomers[]` and `contributors.hall[]` rows, and a train slice's
`participants` {login: {name?, maintainer?, maintainer_signal?, affiliation?}} (only when
the view carries names or the maintainer property). The login stays the key everywhere;
names are for display. The project view (digest) keeps, per person, the member's label
with the most decisive signal (`derive.AFFILIATION_SIGNAL_ORDER`), the first member's
name, `maintainer: true` when any member says so (those members in `maintainer_of`), and
ranks the halls on the merged people.

**What the data cannot tell:** private org membership; an employer the profile does not
state (or states stale); vendors and contractors (a company that does not match is
`community`); activity before the store's first window; what an open PR changes. The label
is GitHub's and the profile's answer when the window was gathered, not as of the window.

## Module changes (`module_notes` → `module_changes`)

The reader's view of a window, shaped like a product's monthly release notes: which modules
are new, what changed in existing ones, what is breaking, what was deprecated, and how much of
the merged work was plumbing (CI, docs generation, tests). Two keys:

- **`module_notes`** (raw, from the store) — what gather read from the clone for the window
  (`gather.collect_module_notes`; a per-window fact like `newcomers`, STORE.md). Omitted when
  no gather stored it (a store gathered before this layer; every fixture bundle; a gather
  without the window's history on disk).
- **`module_changes`** (added by `link.py`) — built only when the view carries
  `module_notes` with `collected: true`, so every other bundle is unchanged.

### `module_notes`

```json
{"collected": true,
 "changelogs": [{"path": "avm/res/dev-test-lab/lab/CHANGELOG.md", "module": "avm/res/dev-test-lab/lab",
                 "commit": "574f7d71…", "date": "2026-10-03", "change": "modify",
                 "entries": [{"version": "0.6.0", "heading": "## 0.6.0", "sections": [
                   {"heading": "Changes", "kind": "changed", "items": [], "placeholder": "None"},
                   {"heading": "Breaking Changes", "kind": "breaking",
                    "items": [{"text": "Deprecated `avm/res/dev-test-lab/lab/secret` as no longer supported by Resource Provider (…)"}]}]}]}],
 "versions": [{"path": "avm/res/dev-test-lab/lab/version.json", "module": "avm/res/dev-test-lab/lab",
               "commit": "574f7d71…", "date": "2026-10-03", "change": "modify", "from": "0.5", "to": "0.6"}],
 "tags": {"collected": true, "source": "clone", "items": [
   {"tag": "avm/res/devices/iot-hub/0.4.0", "module": "avm/res/devices/iot-hub", "version": "0.4.0",
    "date": "2026-10-06T11:19:12Z", "date_source": "commit", "commit": "62b1f05b…", "first": false}]},
 "unread": [{"path": "…", "commit": "…", "date": "…", "reason": "…"}]}
```

- **`changelogs`** — one row per (changelog file, commit) whose commit **added** entries. A
  changelog is a file named `CHANGELOG.md`, `CHANGES.md`, `HISTORY.md`, `RELEASE_NOTES.md` or
  `RELEASENOTES.md` (any case). The file is parsed at the commit and at its parent (at
  `old_path` for a rename, so a moved module's changelog is not new) and compared
  (`changelog_added_entries`): a version the parent lacks is added whole; items appended to
  a version the parent already had are an entry with `amended: true`; a changelog that names
  no version yields its new bullets as one entry with `version: null`. `new_file: true` when
  the file did not exist before (an add or copy). Parsing is generic (`parse_changelog`): an
  entry starts at a heading naming a version (`## 0.6.0`, `## [1.2.0] - 2026-10-14` → `date`,
  `# v2.0.0`, `## [Unreleased]`), any heading level; deeper headings are its sections, each
  with its verbatim `heading` and a `kind` — `breaking`, `deprecated`, `removed`, `security`,
  `fixed`, `added`, `changed`, `other` (an unknown heading keeps its own heading) or
  `unsectioned` (items before any section). Items are verbatim bullet or paragraph text
  (continuation lines, nested bullets and a following fenced block included), capped at 600
  characters with `truncated_from` the original length; more than 40 items in a section →
  `items_omitted`, more than 12 entries per file per commit → `entries_omitted`. A section
  holding only placeholders ("None", "N/A", "None (interface unchanged)") keeps `items: []` and
  `placeholder` (what it said). Nothing is invented: text not in the file is never produced.
- **`versions`** — a `version.json` whose top-level `version` moved (`from` null when the file
  is new).
- **`tags`** — the version tags the clone holds dated in the window: a tag whose last path
  segment is a version; the rest is the module path it publishes (`""` = the repository
  root, e.g. `v1.2.0`). `date` is the tagger date of an annotated tag (`date_source:
  "tagger"`) or, for a lightweight tag (no date of its own), the committer date of the tagged
  commit (`"commit"`). `first`: no version tag of that module in the clone is dated before
  the window. `{"collected": false, "reason"}` when the clone holds no tag or listing them
  failed. No network call: the tags the clone / fetch brought along.
- **`unread`** (only when any) — file versions git could not read: a visible gap, never guessed.

A view of exactly a gathered window carries the stored value verbatim (`validate.py
--bundle` compares it). Any other view takes the items (all dated) of every gathered window
overlapping it that fall inside it, once each, and adds `windows` and `covers_view`;
`tags.windows_without_tags` names windows that listed none. For such a view the
module layer makes no `declared_not_published` claim (a missing tag may sit in a window that
listed none) and repeats the list in `module_changes.inputs.windows_without_tags`.

### `module_changes`

```json
{"summary": {...}, "modules": [...], "prs": [...], "releases": [...]?, "inputs": {...}, "path_rules": [...]?}
```

**`modules[]`** — one entry per module area touched in the window (sorted by `area`); an
area is a module when it is `avm/res|ptn|utl/<group>/<name>` (`kind` `resource` /
`pattern` / `utility`), the Terraform root `main.tf` area or an area with a `*.tf` directly
in it (`module`), `modules/<name>` (`submodule`), or a Bicep directory outside `avm/` with
its own `main.bicep` (`deployment`, e.g. a landing zone's deployments). Docs, CI and other
areas are not modules (they still count for the PR classes). Keys (empty ones omitted):

- `area`, `kind`, `status` — `removed` (entry file `main.bicep` / `main.tf` deleted or moved
  away; `structure.moved_to`) > `new` (entry file added, or the module's own changelog
  created with entries, or its first version tag in the clone dated in the window) >
  `deprecated` (a `DEPRECATED.md` added at the module root) > `orphaned` (an `ORPHANED.md`
  added: no owner — **not** deprecated) > `changed` > `published` (only a version tag).
- `first_version` (new modules: the oldest version of the new changelog, else the first
  tag, else `version.json`), `introduced_by` (new modules: the sources of each signal).
- `structure` — `moved_from` / `moved_to`, `lifecycle_markers` [{file, change, sources}],
  `children` [{path, change: added | removed | moved (`from`) | modified | deprecated |
  orphaned, markers?, interface?, sources}] for child modules (`<area>/…/main.bicep`).
- `versions` — `version_file` {from, to, sources}, `child_version_files`, `changelog` (the
  versions the module's own changelog gained), `published` (tags: version, tag, date,
  date_source, commit, first, `module` when a child's), `releases` (Terraform root module:
  the repo's GitHub releases of the window).
- `changelog` — [{version, date?, amended?, module? (a child's changelog), sections:
  [{heading, kind, items, placeholder?}], sources}] — the maintainers' text, verbatim.
- `interface` — from the window's symbol events of the module's interface file
  (`main.bicep`; for Terraform every `*.tf` directly in the module dir), net over the window
  (added then removed = nothing): `parameters` {added [{name, declaration, required:
  true | false | null}], removed, modified [{before, after}], moved [{from_path, to_path,
  confidence}]}, `outputs` {added, removed, modified, moved}, `resources` {added, removed,
  modified, `api_versions` [{name, type, from, to}]}, `dependencies` {added / removed [{name,
  ref, target, version?, local?}], changed [{change: "version" | "target", from, to}]}.
  `required` is true for a Bicep `param` without a default and not nullable (`?`), false
  with either, null when the declaration snippet cannot tell (Terraform `variable` headers,
  multi-line types). Declarations are the stored snippets (200 characters).
- `breaking` — **`declared`**: the changelog's "Breaking Changes" items, verbatim
  ({version, text, module?, sources}) — the maintainers' own statement. **`candidates`**:
  derived by rule from the interface, a hint to check, never a claim: `parameter_removed`,
  `parameter_moved`, `required_parameter_added`, `parameter_became_required`,
  `output_removed`, `output_moved`, `child_module_removed`, `child_module_moved`,
  `module_removed` ({rule, subject, sources}). A new module has no candidates (nobody
  consumed it). The two lists are never merged.
- `deprecations` — [{target, basis: "changelog" | "marker_file", version?, text?, file?,
  sources}]: a changelog item that says "deprecat…" (or sits under a Deprecated section;
  `target` = its first back-quoted path, else null) and every `DEPRECATED.md` added.
- `prs` [{number, url, title, author, class, plumbing}], `authors`, `source_files_changed`.
- `plumbing_only` — true when nothing reader-facing happened: no source file, changelog
  entry, version move, tag, lifecycle marker, entry-file or child change (README / test /
  CI churn only). `product_score` — 5 for a new module, 3 per declared breaking item, 2 per
  candidate, 2 per child change, 1 per interface change, 1 per changelog item, 1 when a
  source file changed; 0 when `plumbing_only`. It ranks by product change, never by PR count
  or touched files.

Every item carries **`sources`**: [{commit, pr?, url (the PR's, else the commit's), path}],
so a writer can cite it.

**`prs[]`** — every PR merged in the window into `meta.base_branch`: {number, url, title,
train?, class, module_kinds?, title_signal?, sweep?, substantive_areas?, plumbing, areas}.
`class`, by rule from the files of its commits (`path_class`; `generated` and `metadata`
files travel with any change and are ignored): source and no CI → `module` (`module_kinds`:
the kinds of the module areas whose source changed); source and CI → `mixed`; no source:
only CI → `ci`, only tests → `tests-only`, only docs → `docs-only` unless a changelog or
version file changed → `release` (a version cut with no code change); any other mix →
`mixed`; no commit of the PR in the view → `unknown` (`plumbing: null`). `title_signal`
(`ci` / `docs` / `tests`) is the secondary signal: a conventional-commit type (`ci`,
`build`, `chore`, `docs`, `test`, `style`) or a title word (CI, pipeline, workflow, lint,
readme, docs, tests). `sweep` {areas, plumbing_areas} marks a **mechanical sweep**: at least
`SWEEP_MIN_AREAS` (5) module areas touched, half or more of them only in docs / generated /
metadata / tests files; `substantive_areas` lists the modules whose source it did change —
their changes stay in `modules[]` whatever the PR's class. `plumbing` = no source file (a
`release` is not plumbing), or a sweep whose title says plumbing.

**File classes** (`link.DEFAULT_PATH_CLASS_RULES`, first match wins; a rule with a `/`
matches the path, `**` = any segments, otherwise the file name; case-insensitive):
`ci` — `.github/**`, `.azuredevops/**`, `.azure-pipelines/**`, `.pipelines/**`,
`pipelines/**`, `utilities/**`, `.devcontainer/**`, `.vscode/**`, `.config/**`, `Makefile`,
`.gitignore`, `.gitattributes`, `.editorconfig`, `.pre-commit-config.yaml`, tflint configs,
`avm` / `avm.bat` / `avm.ps1`; `metadata` — `CHANGELOG.md`, `CHANGES.md`, `HISTORY.md`,
`RELEASE_NOTES.md`, `version.json`, `metadata.json`, `ORPHANED.md`, `DEPRECATED.md`,
`CODEOWNERS`, `.terraform.lock.hcl`, `.terraform-docs.yml`; then `ci` — any other dot-file;
`generated` — `main.json`; `tests` — `**/tests/**`, `**/test/**`, `**/examples/**`,
`validation/**`, `*.test.bicep`, `*.tftest.hcl`, `*_test.go`; `docs` — `docs/**`,
`guides/**`, `*.md`, images, `LICENSE`; anything else `source`. Override with
`link.py BUNDLE --path-class CLASS=GLOB` (repeatable, before the defaults; recorded in
`module_changes.path_rules`).

**`summary`** — the roll-ups a writer needs first: `modules`, `by_status`, `new_modules`
[{area, kind, first_version, introduced_by: [pr], prs}], `new_child_modules` [{area,
child, prs}], `declared_breaking` [{area, versions, items}], `breaking_candidates` [{area,
rules}], `deprecations` [{area, target, basis}], `orphaned`, `removed`, `most_substantive`
(top 10 by `product_score`, ties by area; plumbing-only modules never rank) and `plumbing`
{merged_prs, classified, plumbing_prs, share (of the classified), by_class}.

**`releases`** — the window's GitHub releases {tag, name, published_at, url, prerelease,
notes?, notes_truncated_from?} when any (the release text: "Release notes text" below).
**`inputs`** — `tags_collected`,
`tags_reason`, `unread`, and for a multi-window view `windows` / `covers_view`.

**What the data cannot tell:** a parameter renamed inside one file reads as one removed and
one added (moves are only matched across files); Terraform `variable` defaults are not in
the stored snippet (`required: null`); a module's history before the clone's bound; for a
multi-window view, areas are mapped with the latest window's code graph (a path it lacks
falls back to the default area rules).

Window and projection: every input is dated (commits, code / symbol events, notes items),
so the view follows `extract`'s window scoping; PR records are the as-of-end projection.
Additive and omit-when-absent: `meta.schema_version` stays 3.

**Version declared vs published** (only when the window's tags were collected):
`versions.declared_not_published` — versions the module's own changelog declared in the
window with no version tag of the module dated in it (a real case: `dev-test-lab/lab`
0.6.0 declared in October 2026, tagged 2026-11-16; `Unreleased` and other labels that do
not start with a digit are not counted); `versions.published_not_declared` — versions
tagged in the window whose changelog entry the window did not add (declared earlier, or
the module keeps no changelog). Versions compare without a leading `v`. The summary's
`version_gaps` [{area, declared_not_published?, published_not_declared?}] rolls them up. A later tag outside the view is not visible to it: "not published" means "not
tagged in this window".

**Release notes text.** Each view release (`releases[]`) carries `notes` — the release's
own text (GitHub's `body`), verbatim up to `gather.RELEASE_NOTES_CAP` = 4000 characters,
`notes_truncated_from` the original length when cut — when the release has any; so do
`module_changes.releases[]` and a Terraform root module's `versions.releases[]`. A
repository without a changelog (a Terraform module repo, a landing zone) still has its
maintainers' declared notes. Releases gathered before this have no `notes`: re-gather.

## The story layer (`discussion`, `stories`, slice `conversation`, `pr_history`, `pr_activity`)

Decisions sit in the text of issues and PRs (proposals, objections, rework, agreement).
The story layer measures that conversation per train, selects the trains with the most to
tell, and gives each train's slice the whole conversation as one timeline. It is derived
by `link.enrich` from stored records only, as projected to the window's end (a story for a
past window holds nothing said after it), and is additive and omit-when-absent:
`meta.schema_version` stays 3, and a train or view without conversation is unchanged.

### `trains[].discussion`

Present when the train has at least one human (not bot: `derive.is_bot_login`) comment,
review comment or review; absent otherwise.

```json
{"comments": 2, "review_comments": 86, "participants": 2,
 "reviews": {"approved": 0, "changes_requested": 0, "commented": 0, "dismissed": 0},
 "review_rounds": 1, "reopens": 0, "text_chars": 19126, "in_window": 0,
 "first_response_days": 21.2,
 "longest_silence": {"days": 19.7, "from": "…", "to": "…", "resumed_at": "<url>"},
 "change_requests": 0, "change_requests_reworked": 0, "bot_comments": 0}
```

- `comments` / `review_comments` — human conversation comments and inline review
  comments over the train's issues and PRs.
- `participants` — distinct human authors of the items, comments, review comments and
  reviews.
- `reviews` — human review submissions by state, the PR author's own excluded (as review
  rounds do); a `commented` review counts only with text: GitHub records each review-thread
  reply as an empty `commented` review whose text is the review comment.
- `review_rounds` / `reopens` — sums of the PRs' `review_rounds.count` and the items'
  `reopen_count`.
- `text_chars` — characters of human text (item bodies, comments, review comments, review
  texts).
- `in_window` — human comments, review comments and counted reviews made inside the
  view's window: the window's own share of the debate.
- `first_response_days` — from the train's first item to the first human comment / review
  / review comment by someone other than that item's author (null when none).
- `longest_silence` — the longest gap between consecutive recorded human events
  (openings, comments, reviews, lifecycle events, merges, PR commits from `pr_history`),
  with the URL of the event that ended it (`resumed_at`). Elapsed and idle time are in
  `effort` and not repeated.
- `change_requests` — `changes_requested` reviews; `change_requests_reworked` (only when
  the PR history is known for every PR with one) — how many were followed by a commit on
  that PR.
- `bot_comments` — automation comments, review comments and reviews (counted nowhere
  else).

### Story selection: `trains[].story*` and `stories`

A separate, explicit selection for narration, independent of `tier` (which ranks by
footprint: PRs, commits and touched areas, so mechanical sweeps over hundreds of modules
fill it while the most discussed trains are `mention`). `tier` is unchanged; a train can
be deep and a story, either, or neither.

`story_score` (`link.story_score`) = discussion points + relevance:

| Discussion points | |
|---|---|
| each human comment / review comment | 1 |
| each conversation turn inside the window (`in_window`) | +1 (the window's debate weighs double) |
| each participant beyond the first | 2 |
| each change request | 3 |
| each reopen | 3 |
| each review round beyond the first on a PR | 2 |
| longest silence over `STORY_SILENCE_DAYS` (14) | 2 |
| each 2,000 characters of human text (at most 5) | 1 |

| Relevance | |
|---|---|
| kind feature / module-request | 3 |
| kind bug | 1 |
| a new module introduced by one of its PRs (module layer) | 5 |
| declared breaking items / breaking candidates from its PRs (at most 3 each) | 2 / 1 each |
| in flight (often where the debate still is) | 1 |

A train still **in flight** with no conversation turn inside the window
(`discussion.in_window` below `STORY_MIN_IN_WINDOW`, 1) is not a story of this window however
long its thread: it is listed in `stories.quiet` (`{id, score, idle_days}`, omitted when
empty) — work with history that nobody touched this window. A train that landed or was closed
in the window needs no in-window turn.

A train **qualifies** when it has a `discussion` with at least `STORY_MIN_EXCHANGES` (3)
turns (comments + review comments + counted reviews) and at least 2 participants, scores at
least `STORY_MIN_SCORE` (8), and — when its work is plumbing (`module_changes.prs` marks
every merged PR of it `plumbing` or a `sweep`; without the module layer: a chore / docs /
other train touching more than 10 areas) — its discussion points alone reach
`STORY_PLUMBING_MIN` (15). The `STORY_TOP_N` (8) best, by score then id, get:

- `story: true`, `story_rank` (1 = most to tell), `story_score`, and `story_reasons`
  (plain strings naming what scored, e.g. `"88 human comment(s) and review comment(s)"`,
  `"1 change request(s)"`, `"219 days without recorded activity before 2026-06-10"`,
  `"new module avm/res/x/y"`, `"in flight"`).

The view gets **`stories`** (absent when no train qualifies): `{selected: [{id, rank,
score}], more: [{id, score}] (the trains that qualified beyond the cap — "N more had
notable discussion"), rule: {min_exchanges, min_in_window, min_score, top_n, plumbing_min,
silence_days}}`.

### Every story of the window: `stories.all`, `plumbing_themes`

For a view of a gathered story layer (it carries `pr_history`; other views and fixtures
are unchanged), `link.build_story_list` adds **all** the stories, not a top N
(`selected` / `more` / `quiet` stay as they were for older readers):

- `stories.all` — `[{id, group, state, group_rule, title, modules, modules_source,
  module_urls, substance, score, worthy, released?}]`, ordered by group (`landed`,
  `in_flight`, `stalled`, `closed`, `plumbing`), then score.
  - `modules` — module areas only (`link.module_kind`: never a CI or utilities folder),
    from `link.train_modules`; `modules_source` says from what: `merged_commits` (the
    train's merged commits), else for a train with no merged code `pr_commits` (its
    open PRs' own commits in `pr_history`, as of the view's end), `issue_form` (an
    issue form's `Module Name` field), `title_scope` (a conventional-commit scope that
    is a module path, `fix(avm/res/sql/server): …`), or `none`. Never guessed from
    prose. `module_urls` `{area: url}` — `link.module_url`, THE module link of every
    view row (the folder's tree at `meta.clone_sha`).
  - `substance` (`link.story_substance`) — `discussion` (a person's comment, review
    comment or review text on the train's items), `decision_only` (no such turn, but a
    person's verdict review or an item merged / closed by someone who is not
    automation), `opening_only` (nothing beyond the opening posts). Automation
    accounts never count.
  - **story-worthy** (`worthy`, `link.story_worthy`), in order: a human conversation
    turn in the window (`discussion.in_window`); an issue / PR of the train opened,
    merged, closed or reopened in it; a human commit on one of its PRs in it. An open
    train with conversation but none of these is **carried over** (a stalled story)
    when its last human event is at most `STORY_CARRY_MAX_DAYS` (180) old, else listed
    in `stories.excluded` `[{id, reason}]`.
  - **state** (`link.story_group`): `landed` — the train shipped (a PR merged into the
    walked branch); `closed` — closed without landing (rejected / abandoned); an open
    train is `stalled` when carried over, when its last human event is more than
    `STORY_STALLED_DAYS` (14) old at the view's end, or when an open PR's head checks
    fail (not only awaiting approval) with no comment by its author since the head
    commit; else `in_flight`. `group_rule` says which rule fired.
  - **group** — `plumbing` when every PR of the train is authored by an automation
    account and no person took a conversation turn (`group_rule` names the accounts;
    its theme comes from its paths, `dependency_updates` for an update PR), or when
    every PR of the train is plumbing (the module layer's class, or a sweep), whatever
    its state (`state` keeps it); else the state. Every
    PR must be classified: one the module layer did not classify (an open PR) is
    classified from its own commits' paths in `pr_history` committed by the view's end;
    a PR that cannot be classified keeps the train out of plumbing.
  - `released` (landed stories) — **does landed mean released?** per module a PR of the
    train touched (`link.train_released`): `{module, version, status, tag, date, url,
    registry?}`, `status` `published` (a version tag of the module in the window: its
    tag, date, tree `url`, the public registry reference `br/public:<module>:<version>`
    for a Bicep module), `declared_not_published` (the changelog declared a version, no
    tag), `unknown` (declared, tags not read for the whole view) or `no_version_change`.
    **A version is the train's only when tied to it**: the changelog entry declaring it
    has one of the train's PRs as its source, its tag sits on one of the train's commits
    (the merge commit), or it is the module's first tag of the window on or after the
    train's last merge commit (the walked branch is first-parent linear, so that is the
    first tag containing the merge). A module whose versions in the window are not tied
    to the train is `unknown` (`version` null, with a `reason`): a PR merged after a tag
    is never reported as released in that earlier version.
  - `stories.groups` `{group: count}`; `stories.all_rule` `{stalled_days,
    carry_max_days, groups}`.
- `plumbing_themes` (`link.build_plumbing_themes`) — the window's plumbing PRs grouped
  by the shared paths they touch, so a writer can tell what was engineered: `[{id,
  title_hint, paths, paths_more?, prs: [{number, title, author, state, merged_at,
  url}], stories, related_stories?, modules, people, evidence_counts: {commits,
  comments, docs, interface, patches}, subjects: [{pr, sha, subject}], issues}]`.
  `stories` lists only the trains of the `plumbing` story group; a theme PR's train in
  another group is in `related_stories` `[{id, group}]` (group null: not a story) — the
  PR stays in the theme. `modules` `[{module, url}]` — the module areas of the theme's
  paths, with `link.module_url`. Themes (`PLUMBING_THEMES`, the
  theme covering most of a PR's files): `ci_workflows` (`.github/workflows/`,
  `.github/actions/`, pipeline folders), `pipeline_utilities` (`utilities/`),
  `test_environment` (`tests/`, `e2e/`), `docs_generation` (READMEs, `docs/`, other
  Markdown), `repository_policy` (CODEOWNERS, LICENSE, `.github/` files, editor and
  tool configuration), `dependency_updates` (an automation account's update PR),
  `other_plumbing`. No prose: `title_hint` is a neutral label.
- People rows of automation accounts gain `automation` `{kind, on_behalf_of?}`
  (`link.annotate_automation`): `kind` `registry` (the people registry's operator tag
  `automation`; the tag `not-automation` sets `automation: null` and wins over
  everything else), `account_type` (GitHub's account type for the author, kept by
  gather as `author_type` on PRs, issues, comments and reviews when it is not `User`),
  `app_login` (a `name[bot]` login) or `automation_name` (a git name automation commits
  under). Never from a display name alone. `on_behalf_of: {login, basis, pr, issue?}` — who the PR works for, from the record
  only, in order: the person who assigned the agent (`assigned`), the person assigned to
  the PR with it (`assignee`), the first person who requested a review (`requested
  review`), the author of the issue the PR closes (`closes the issue of`); absent when the
  record has none. An account with several PRs adds `on_behalf_of_by_pr` `{pr: {...}}`.
- `module_changes.modules[]` gain `url` (`link.module_url`: the module folder at the
  commit the structure was read at) and `versions.published[]` gain `url` (the tag's tree) and `registry`
  (the public Bicep registry reference, where derivable).
- Closed issues gain `closing` `{at, by, state_reason?, commit?, prs?, before_merge?}`:
  who closed it (the closing event's actor), GitHub's reason, the closing commit, the
  view's PRs that name it as closed, and those not yet merged when it closed.
- PR rows that carry `head_repo` gain `is_fork` (another repository; null head
  repository: a deleted fork). The view's own repository is `link.repo_slug`: `meta.repo`
  as is when already qualified (a manifest member's view), else `owner/repo` — the one
  place module, tag and item URLs and repository comparisons are built from.
- **As of the view's end**, everywhere: themes, open-PR classification, automation
  summaries, `closing` and story worthiness read only commits, events and closes up to
  the view's instant. A commit read by its roll-up state only (`state_only`) is
  `unknown` for a view that ends before the gather (`gathered_at`): GitHub's state has
  no check times. A nightly gather of the open window reads it the same night, so its
  view (ending after the read) keeps the state; `commits_incomplete` counts such
  commits.

### Slice `conversation` (the story material)

`link.py BUNDLE --slice <id>` adds `conversation` to every train's slice:

- **`timeline`** — one chronological list of items `{at, kind, actor, url, on: {type,
  number}, ...}`:
  - `opened` — an issue / PR, with `title` and its body as `text`;
  - `comment`, `review_comment` — with `text`; a review comment also carries what it is
    attached to when the store has it (gathered since review-comment context is kept):
    `path` (the file), `line`, `commit` (the PR commit it was written against),
    `diff_hunk` (the diff lines GitHub shows with it, ending at the commented line; at
    most 12 lines / `STORY_HUNK_CAP` 1,500 characters, `diff_hunk_lines` the original
    count when cut; all hunks of one slice share `STORY_HUNK_BUDGET` 15,000 characters,
    a hunk past it is left out and the item gets `diff_hunk_omitted`) and `in_reply_to`
    (the URL of the review comment that opened its thread; a reply carries no hunk —
    GitHub gives it the same one as the thread's opener);
  - `review` — with `state` and `text` (an empty `commented` review is a review thread's
    wrapper and is left out: its text is the review comment);
  - `ready_for_review`, `reopened`, `closed` — the lifecycle events (`url` is the item page
    anchored at the event, `<item url>#event-<id>`); a `closed` from `closed_at` when no
    event records it;
  - from the view's `pr_activity` (when stored): `renamed` (`from`, `to`: a title edit),
    `convert_to_draft`, `review_requested` / `review_request_removed` (`requested` — a
    login or `team:<slug>` — and `by`), `review_dismissed` (`review_id`, `state`,
    `message`), `head_ref_force_pushed` (`before` / `after` commit when read), each with
    `url` `<pr url>#event-<id>`; and `checks` — ONE item per PR commit whose checks were
    read (never one per check run), committed by the instant: `sha`, `url` (the PR
    commit), `rollup` as of the instant (`passing` / `failing` / `pending` / `none`,
    `gather.checks_rollup`), `failing` / `pending` (check names), `passing` (a count),
    `checks_omitted`, `head: true` on the latest such commit; `at` is the last completion
    by the instant (the commit time when all are pending). A `checks` item is automation:
    it ends no silence and is never an open PR's last event;
  - `merged` — with the merger as `actor`;
  - `commit` — `source: "pr_head"` (a PR's own commit from `pr_history`: `subject`,
    `files` count, `authored_at`, `merge: true` for a merge commit, `url`
    `<pr url>/commits/<sha>`, and when stored its message body as `text` —
    `body_truncated_from` when gather cut it — and its `change` summary) or
    `source: "base"` (the train's commit on the walked branch, e.g. the squash commit,
    with its `change` summary from the view's files and symbol events), each with the
    `code_notes` its diff added / removed / changed. A PR commit of a walked PR also
    carries `evidence` (below);
  Ordered by time, then kind, then url. Bots are not in it.
- **`change`** (on commit items; omitted when nothing is known about the commit) — the
  compact, factual summary of what the commit changed (`link.change_summary`):
  `files` `{class: [path]}` grouped by the module layer's path classes (`source`,
  `tests`, `docs`, `generated`, `metadata`, `ci`; at most `STORY_CHANGE_PATHS_PER_CLASS`
  8 per class, `files_more` `{class: n}` the rest, `paths_omitted` what gather did not
  keep), `modules` (the module areas touched), `shared: true` when a `ci`-class file
  changed (a workflow, action or pipeline utility every module depends on — what a
  reviewer means by "this would impact all the modules"), `interface` `[{change:
  add|drop|change, kind: param|variable|output|resource|module, name, path}]` (the
  module's own interface first, then its tests'; at most `STORY_CHANGE_INTERFACE_MAX` 20,
  `interface_more` the rest) and `patches` `[{path, kind: changelog|shared, added,
  removed, lines_omitted?}]` (the lines the commit wrote into a changelog or a shared
  file, from `pr_history.patches`). Each PR commit's summary is relative to its parent:
  the rework after a change request is the summary of the commits that follow it (see
  `rework_after_change_request`).
- **`evidence`** (on PR commit items, from `pr_history`; omitted when none) — what the
  commit's diff says, extracted by gather (`collect_pr_narrative`), never summarised:
  `interface` (the delta of each changed module file between the commit's parent and the
  commit: `{path, kind, name, change, before?, after?, from?, to?, entries_added?,
  entries_removed?, entries_omitted?}` — `change` is `added`, `removed`, `renamed` (`from`
  / `to`) or the field that moved: `type`, `default`, `allowed`, `description`,
  `constraints`, `nullable`, `api_version`, `ref`, `source`, `version`, `value`; a
  multi-line object / array default or allowed list gives the entries that appeared /
  went instead of before / after), `comments` (code comments added / dropped / reworded:
  `{path, change: add|drop|reword, text | before/after, decl?, line, class,
  truncated_from?}`, `decl` the declaration it documents or sits in, `class` as for code
  notes), `docs` (`{path, kind: readme|adr|docs|markdown, sections: [{section?, added,
  removed, lines_omitted?, generated_skipped?}]}`: the prose lines a commit changed in a
  Markdown file, generated README sections skipped and counted), `interface_omitted` /
  `comments_omitted` (what gather's per-commit caps left out), `interface_unread` /
  `docs_unread` (files of the commit whose before or after version exists but could not
  be read — over the size limit, a failed read — so no delta is claimed for them; a
  commit with only such files carries just these lists, never no `evidence` at all). All evidence of one slice
  shares `STORY_EVIDENCE_BUDGET` (16,000 characters), the newest commits first; a commit
  whose rows were cut gets `evidence_omitted` (`truncation.evidence`).
- **`code_notes`** (on commit items) — `{commit, path, change: add|drop|change, class,
  source, pr?, text | before/after | kind/name}`. `class` (`link.comment_class`):
  `parameter_description` (a Bicep `@description(...)` / `@metadata(...)` decorator),
  `todo` (TODO / FIXME / HACK / XXX / BUG), `note` (a developer note), `interface` (a
  parameter / variable / output line: `kind`, `name`, `before` / `after` declaration).
  **Banners** — section labels such as `// Variables //`, `// Parameters`, `// =====
  Outputs =====`: once the comment markers and decoration characters (`= - * # / _ ~`)
  are stripped, at most three words and no punctuation — are dropped and counted
  (`truncation.code_notes.banners_dropped`). One row per comment line: the symbol
  extraction keys comments by text, so the lines of one file and commit are not in file
  order and are never joined into prose. Within `STORY_CODE_NOTES_BUDGET` (12,000)
  characters, developer notes and TODOs are kept first, then parameter descriptions
  removed / changed, then added, then interface lines.
- **`code_notes_by_module`** — `{module area: {path: {added, removed, changed}}}` counts
  over every code note (kept or not).
- **`turning_points`** — marked by rule, as data: `{rule, at, index (into timeline), url,
  detail?}` — `change_request`; `reopened`; `silence_then_activity` (activity after more
  than 14 days without a recorded human event; detail `{days, since}`);
  `rework_after_change_request` (the first PR commit after a change request on that PR;
  detail `{change_request_index, commits_since_request, paths_since_request?}` — the PR's
  own non-merge commits from the request until the requester's next comment / review
  and the files they touched, at most 30);
  `history_rewritten` (a PR commit committed more than `STORY_REWRITE_HOURS` (24) after it
  was authored: amended, rebased or cherry-picked — the branch's earlier states are not
  kept); `new_attempt` (a PR of the train opened after another was closed unmerged);
  `still_silent` (an open train with no human event in the last 14 days before the
  as-of instant; `at` is that instant). A scope change cannot be marked: title edits are
  not gathered.
- **`truncation`** — `{item_cap, budget, chars_original, chars_kept, cut: [{index, url,
  kept, original}], text_omitted: [{index, url, chars}], code_notes: {banners_dropped,
  budget, kept, omitted}}`. Each text is capped at `STORY_ITEM_CAP` (5,000 characters:
  every human comment, review and review comment of Sept-Oct 2026 in
  Azure/bicep-registry-modules fits — the longest is 3,382, the 99th percentile about
  2,400 — so only long PR / issue bodies, mostly template, are cut); the total is held to
  `STORY_SLICE_BUDGET` (40,000: the most discussed train of those months, 88 comments and
  a 4,131-character body, is 19,126 characters) by cutting the longest texts evenly, never
  below `STORY_ITEM_FLOOR` (500), then leaving out whole texts from the middle of the
  timeline outward (the opening and the latest stay). A cut item carries
  `truncated_from`; an omitted one `text: null` and `text_omitted`. Nothing is cut
  silently. `hunks_omitted` (only when any) counts the review-comment hunks left out for
  `STORY_HUNK_BUDGET`. `evidence` (only when a commit item carries any) — `{budget,
  kept, omitted}` of the commit evidence rows.
- **`bots`** — `{login: count}` of the automation comments, review comments and reviews
  left out of the timeline.
- **`automation`** (only when any) — the automation accounts of the train, for what they
  are: `[{login, is_bot: true, comments, review_comments, reviews, commits, prs_opened,
  on_behalf_of?: {login, basis: assigned|requested review, pr}}]` — for a PR an
  automation account opened, the person who handed it the work when the record shows it.
- A `closed` lifecycle item carries `state_reason`, `commit` (the closing commit) and,
  for an issue, `closing_prs` (the view's PRs that name it as closed), when recorded.
- **`pr_history`** — `{pr number: status}`: `fetched` (with `, notes extracted` or `,
  notes skipped: <reason>`, or ` (commits on the base branch)`), `missing`,
  `fetch_failed`, `log_failed`, or `not_gathered` when the view has no PR history for it.
- **`as_of`** — the instant the material is projected to (`meta.records_as_of.instant`,
  else the window's last second).
- **`not_gathered`** — what GitHub records that the store does not keep, so a narrator
  says it rather than infers it: title and description edits, force-pushes and the branch
  states they replaced, label / milestone / assignee changes over time, whether a review
  thread was marked resolved, check runs and their results per PR. When the view's
  `pr_activity` holds a row for every PR of the train, the list shrinks to what is still
  missing: description edits (renames are `renamed` events), the content of the states a
  force-push replaced (the push and its before / after commits are in the record), label /
  milestone / assignee changes, and — only for PRs whose checks were capped, failed or not
  requested — "check results and review-thread resolved flags of PR(s) #N (<status>)";
  a partial read is named too: review threads beyond those read (`threads_omitted`),
  earlier lifecycle events (`events_omitted`), and the earliest commits whose checks were
  not read (`commits_total` above the commits read). What gather's per-PR budgets left
  out of the PR history is named the same way, with or without `pr_activity`: narrative
  evidence rows of older commits (`narrative_omitted`) and the responses of review
  comments (`responses_omitted`).
- **`thread_coverage`** (only with the view's `pr_activity`) — `{pr number: {flagged,
  omitted}}`: how many of the PR's review threads in the ledger carry GitHub's resolved
  flag, and how many review threads gather did not read. Resolution coverage of a PR is
  complete only when every ledger review thread of it is flagged, `omitted` is 0 and the
  ledger cap hid none (`threads_omitted`); the story check refuses an "unknown
  resolution" claim only then.
- **`gaps`** (omitted when empty) — what THIS slice cannot show because its store was
  gathered before the data was kept: review comments with no file / diff lines / thread
  link, PR commits with no file paths or message bodies, PRs with no `pr_activity` row
  (check results, resolved flags and lifecycle events), walked PRs whose history has no
  narrative evidence (`narrative_collected`). A re-gather of the window adds them.
- **`outcome`** — where the train stands as of `as_of` and what ended up in the product
  (`link.story_outcome`):
  - `train_outcome`, `as_of`;
  - `items` — per issue / PR of the train: `type`, `number`, `title`, `author`, `state`,
    `created_at`, `url`, `closed_at` (a closed issue); a PR adds `merged`, `merged_at`,
    `reviews` `{verdicts: [{reviewer, state, at, url}] (each human reviewer's latest
    approved / changes_requested / dismissed, the author's own excluded),
    outstanding_change_requests: [{by, since, url}]}` and `checks` (`link.checks_outcome`
    from the view's `pr_activity`: `{status: "gathered", head, head_url, rollup, failing,
    pending, passing, gathered_at, commits_total, commits_covered, checks_omitted?,
    completed_after_as_of?, commits_incomplete?, reason?}` — the head is the latest
    covered commit committed
    by the instant and its roll-up is as of the instant, a check completed after it
    counting as pending; a head whose checks were not all read (`checks_omitted`) is
    `incomplete` unless a visible check failed (never a pass from a partial page); when
    none of the commits read was committed by the instant (the read covers the latest
    ten), `rollup: "unavailable"`, `head: null` and a `reason`; `commits_incomplete`
    counts the covered commits whose checks were only partly read (their result is
    `incomplete`), so the history read is complete only without it;
    A PR row of the outcome adds `head_repo` and `is_fork` when the view's PR carries them
    ("from a fork of …"; `head_repo` null with `is_fork` true: the fork was deleted).
    `{status: capped|failed|not_found|not_requested, reason?, gathered_at}` when
    the checks were not read; the string `"not gathered"` when the view has no such row;
    `awaiting_approval` — the head's checks whose run waits for a maintainer's approval,
    `action_required`, e.g. a first-time contributor's fork — and `history_status` when
    not every commit was read);
    an open PR, and an open issue, add `last_event` `{at, kind, actor, url}` and `ball`;
  - `ball` — **who the next move is with, DERIVED by rule** from the sequence of recorded
    events, never from anyone's words (`derived: true`; `link.ball_with`), with the
    `rule` that fired, its `basis` (the last event) and, at train level, the `pr` it is
    for (the latest open PR, else the open issue). A PR commit counts as the PR author's
    move. For an open PR: **R1 mention** — the last event is by someone other than the
    author and names other people (`@login`): `awaiting mentioned` (them); **R2 author
    moved** — the last event is the author's (opening, commit, comment): `awaiting
    reviewer` (the reviewers whose latest verdict is a change request, else the people the
    author named, else the reviewers who reviewed; nobody: `awaiting a first review`);
    **R3 approved** — the last event is an approval with no change request outstanding:
    `awaiting merge`; **R4 reviewer moved** — otherwise: `awaiting author`; **R5 failing
    checks** — when R2 or R3 fired and the head commit's checks are failing as of the
    instant (the item's `checks`), the next move is the author's: `awaiting author`, with
    `checks_basis` `{head, failing}` (R1 and R4 stand); **R6 checks awaiting approval** —
    as R5, but every failing check waits for a maintainer's approval to run: `awaiting
    maintainer`; **R7 module owner response requested** — when R2 stands (no change
    request outstanding, checks not failing) and the PR carries the owner-response
    label: `awaiting module owner` (the owners the request names, when recorded), with
    `owner_basis` `{label, item}`; **R8 approval made stale by a later push** — when the
    PR's `review_state` is `approved_stale`, no change request is outstanding and R2 fired
    (or R4 on an event older than the author's last push): `awaiting reviewer` (the
    approver), with `review_basis` `{approved_at, last_push}`. **Precedence**, so the ball
    and the review state never disagree: failing checks (R5 / R6) over everything but R1
    and R4; an outstanding change request over R7 and R8; a named person (R1) stands; then
    R7 (the module owner was asked), then R8, then R2 / R3 / R4. A `checks` item is never the last
    event; a `review_requested` event names its requested reviewer for R2. For an open
    issue with no open PR: **I1** the reporter moved last: `awaiting maintainer
    response`; **I2** someone else named people: `awaiting mentioned`; **I3** otherwise
    `awaiting reporter`. `state` / `with` say who; `derived` is always true;
  - `waiting_on` (only when any) — open linked items that the PR author's latest message
    names (`{number, type, state, title, named_in}`);
  - `owner_response_requested` (only when any) — the items carrying the owner-response
    label: an open one **waits on the module owner** `[{item {type, number, url}, module,
    label, since, waits_on: "the module owner", owners?, owners_source?}]`, a closed /
    merged one **waited on them** (`waited_on`, `until`: its merge / close time); `owners`
    are those an automation comment named, else the module's owners at the view's end
    (`owners_source: "owner_file"`). Outcome items add `module_owners` `{module: [login]}`
    (who "the module's owner" is), `module_info` and, for a PR, `proposes_new_module`;
  - every item row carries `modules`, `modules_source` and `module_urls`
    (`link.record_modules`: as `stories.all`, per item);
  - `released` (a shipped train) — as `stories.all[].released`;
  - `linked` — the repository's issues / PRs named in the conversation (`#N`, or a
    github.com link to the repository's issue / pull; HTML comments — a template's
    "Fixes #123" — ignored; the train's own items excluded; at most `STORY_LINKED_MAX` 10,
    `linked_omitted` the rest): `{number, type, title, author, state, created_at, url,
    mentioned_in, merged (a PR), closed_at?, comments (human), last_activity_at,
    description (verbatim, from an issue form's "### Description" when present; at most
    `STORY_LINKED_BODY_CAP` 1,200 characters), description_truncated_from?}`, or
    `{number, in_view: false, mentioned_in}` for an item the view does not hold;
  - `modules` (shipped work; only when the module layer is present) — every module entry
    a train PR contributed to: `area`, `kind`, `status`, `first_version`, `versions`
    `{changelog, published: [{version, tag, date}], version_file: {from, to},
    declared_not_published, published_not_declared}`, `changelog` (the entries the
    train's PRs wrote — `version`, `sections` `[{heading, kind, items, placeholder?}]`,
    items **verbatim**, at most `STORY_OUTCOME_ITEMS_MAX` 30 per module), `interface`
    `{group: {change: [name], <change>_more?}}` (at most `STORY_OUTCOME_NAMES_MAX` 25
    names each), `children` `[{path, change}]`, `breaking` `{declared: [text],
    candidates: [{rule, subject}]}`;
  - `requests` — **requests and what followed** (only when any): per thread with a
    `response` or a `verdict_after`, `{thread, kind, by, at, url, ask?, path?,
    followed_by: [{sha, at, url, subject}], basis, verdict_after?}`, at most
    `STORY_REQUESTS_MAX` 20. A sequence from the record (asked, then these commits, then
    this verdict), never an agreement nobody wrote;
  - `net_change` (open PRs with stored commit paths / patch excerpts) — `{pr:
    {files_touched: {class: [path]}, lines: {path: {kind, commits, net_added,
    net_removed, lines_more?} | {kind, commits, reverted: true}}}}`: the files the PR's
    own commits touched, and for changelogs and shared files the lines added / removed
    net of the PR's own later commits (a change made and undone is `reverted`). Merges
    from the base branch are not counted, so this is the PR's own work, not a diff
    against the base.
- **`threads`** — the open-threads ledger (`link.story_threads`): every question or request
  raised, with its resolution status **by rule**. A thread is opened by a review comment
  that starts a thread (not a reply) by anyone but the PR author, or by the author with a
  question mark; a `changes_requested` review; a comment, review text or review-comment
  reply with a question mark (quotations and code blocks ignored). Each `{id (T1, T2,
  ...), kind: review_thread|change_request|question, on, at, by, url, ask (a verbatim
  excerpt: the first question sentence, else the opening line, at most `STORY_ASK_CAP`
  300 characters), path?, replies?, status, resolution?, signals}`. `status`, strongest
  first: `approved_after` (a change request followed by an approval from the same
  reviewer), `answered` (a reply by someone else in the same review thread — GitHub's
  thread link — or, for a conversation question, a later comment on the same item by
  someone else), `dismissed`, `followed_by_commit` (a later non-merge PR commit touching
  the thread's file, `same_file: true`), `reply_on_item` (a later reply on the same PR by
  someone else when the store holds no thread links), `followed_by_commit` with
  `same_file: null` (a later commit whose files are not known to match), else
  `unresolved in the record`. Where the view's `pr_activity` holds GitHub's own flag for
  a review thread (joined on its opening review comment) the record wins: `status` is
  `resolved on GitHub` / `not resolved on GitHub`, the rule's status moves to
  `derived_status`, and `resolved_on_github` `{resolved, by?, outdated, as_gathered,
  gathered_after_as_of?}` says what GitHub held when the window was gathered (GitHub keeps
  no time for a resolution, so a view of an earlier window marks `gathered_after_as_of`).
  `resolution` is the strongest signal `{status, at, by, url, same_file?}`; `signals` lists
  all. A thread on a PR also gets `response` (`link.thread_responses`) — `{basis, commits:
  [{index, sha, at, url, subject, match?, hunks?}], file_commits?}`: basis `anchor` when
  gather matched the thread's opening review comment against the later commits' diffs
  (`pr_history.responses`: the commits that changed the commented lines, `match` `anchor` /
  `line`, `hunks` the lines removed and added there), else basis `time`: the PR's own
  non-merge commits after the thread until its opener speaks again after one of them
  (at most `STORY_RESPONSE_COMMITS` 5; `index` points at the commit's timeline item and its
  `evidence`) — and `verdict_after` `{state, at, url}`, the opener's first approval /
  change request / dismissal after the thread (after its last response commit). These are rules over the record, not a verdict:
  "answered" means somebody replied, not that the reply settled it. At most
  `STORY_THREADS_MAX` 40 (`threads_omitted` the rest).

### `pr_history` (raw, from the store)

The per-window fact gather writes (`gather.collect_pr_history`; STORE.md), omitted when
no gather stored it (a store gathered before this layer, every fixture bundle, a gather
without the clone):

```json
{"collected": true, "base": "main",
 "limits": {"max_commits": 250, "notes_max_prs": 40, "notes_max_commits": 60, "notes_max": 400},
 "prs": [{"number": 7146, "status": "fetched", "head": "<sha>",
          "commits": [{"sha": "…", "parents": 1, "author": "<git author name>",
                       "authored_at": "…Z", "committed_at": "…Z",
                       "subject": "AVM-RES: Update changelog and fix e2e tests", "files": 5}],
          "notes": [{"commit": "…", "path": "…/main.test.bicep", "subkind": "comment",
                     "change": "add", "name": "// Enforcing location due to …",
                     "before": null, "after": "// Enforcing location due to …"}]},
         {"number": 6974, "status": "missing"}]}
```

- **The whole record** — gather passes `all_prs`: every PR with commits of its own is
  walked (not only the discussed ones), every commit of it; the walks' caps are safety
  caps (`ACTIVITY_PR_HISTORY_MAX_PRS`, default 2,000 PRs; `ACTIVITY_PR_HISTORY_MAX_COMMITS`,
  default 1,000 commits per PR; `PR_HISTORY_MAX_COMMITS` 1,000 listed; narrative 400,000
  characters and responses 100,000 per PR; patch excerpts 200,000), each with a count of
  what it left out. Fork PRs need nothing special: `refs/pull/<N>/head` lives in the base
  repository, so a fork's commits, a deleted fork's, a closed PR's and commits by authors
  with no login are read like any other.
- `replaced` — per force-push of the PR (from `pr_activity`): `{at, actor, before, after,
  status: read|unavailable, commits?, commits_omitted?, reason?}`: the commits the push
  replaced (reachable from `before`, not from `after` nor the walked branch; subject, body,
  paths; at most `PR_HISTORY_REPLACED_MAX` 100), the `before` and `after` commits fetched
  by sha when the clone lacks them; `unavailable` with the reason when either end cannot
  be established (without `after` the list could include commits the push retained).
- `status` — `fetched`, `missing` (no `refs/pull/<N>/head` on the remote), `fetch_failed`
  / `log_failed` (with `reason`). `on_base: true` when the head is on the walked branch (a
  merge-commit merge: the base walk already has its commits).
- `commits` — the PR's own commits, oldest first (reachable from its head, not from the
  walked branch), at most 250 (`commits_omitted` the rest); `author` is the git author
  name (no email is stored).
- `notes` — the comment / parameter / variable / output changes between those commits
  (`.bicep` / `.tf` files, the patch walk's symbol extraction), only for the most
  discussed PRs (human comments + review comments > 0, at most 40 PRs per window, each of
  at most 60 commits; the newest 400 events per PR, `notes_omitted` the oldest left out);
  every other fetched PR has `notes_skipped` with the reason.
- `paths` / `paths_omitted` (per commit; omitted for a merge) — the files the commit
  changed, at most `PR_HISTORY_PATHS_MAX` 60; `body` / `body_truncated_from` — its message
  body (where a commit says why), trailer paragraphs (`Co-authored-by: ...`,
  `Signed-off-by: ...`) dropped, at most `PR_HISTORY_BODY_CAP` 1,500 characters. Stores
  gathered before these were kept have neither (the slice's `gaps` says so).
- `added` (per commit; gathers that read the commits' change status) — the paths the
  commit ADDED: a new module's entry file is how a proposed module is recognised
- `patches` (walked PRs only) — `[{commit, path, kind: changelog|shared, added: [line],
  removed: [line], lines_omitted?}]`: what each commit wrote into a `CHANGELOG.md` or a
  shared repository file (`PR_HISTORY_PATCH_PATHSPECS`: `.github/`, `utilities/`,
  `.azuredevops/`, `.pipelines/`, `pipelines/`), at most `PR_HISTORY_PATCH_LINES` 40 lines
  per file and commit of at most 500 characters each, the newest within
  `PR_HISTORY_PATCH_CHARS` 8,000 characters per PR (`patches_omitted` the oldest left
  out); `patches_skipped` with the reason when that walk failed.
- narrative evidence (walked PRs; `collect_pr_narrative`, git only: one more `git log -p
  --unified=3 -M` over `PR_NARRATIVE_PATHSPECS` — `*.bicep`, `*.bicepparam`, `*.tf`,
  `*.hcl`, `*.ps1`, `*.psm1`, `*.yml`, `*.yaml`, `*.md` — and the files review comments
  are on, plus `git cat-file blob` for the before / after module files and READMEs that
  walk already fetched): per commit `comments` (at most `PR_NARRATIVE_COMMENTS` 25,
  `comments_omitted`; each at most 500 characters), `docs` (at most
  `PR_NARRATIVE_DOC_LINES` 30 lines per file), `interface` (at most
  `PR_NARRATIVE_INTERFACE_ROWS` 40, `interface_omitted`; at most 10 module files, tests
  and examples excepted; values capped at 200 characters, descriptions at 300) — shapes
  as in the slice's `evidence` — all within `PR_NARRATIVE_CHARS` 24,000 characters per
  PR, the newest commits first (`narrative_omitted` counts the rows left out); per PR
  `responses` `[{comment_id, path, line?, at, commits: [{sha, match: anchor|line, hunks:
  [{old_start, new_start, removed, added, lines_omitted?}]}], file_commits?}]` (a review
  comment's later commits that changed the lines it is on: a hunk removing or keeping one
  of its last diff-hunk lines, or within `PR_RESPONSE_NEAR` 3 lines of it in the next
  commit; at most 3 commits, 2 hunks, 12 lines each; `PR_RESPONSE_CHARS` 12,000 per PR,
  `responses_omitted`); `narrative_collected: true`, or `narrative_skipped` with the
  reason. A comment directly above a declaration documents it (`decl`); section labels
  and separators are dropped. A README with `Navigation` and `Resource Types` sections
  is a generated module README: its preamble and generated sections (Navigation,
  Resource Types, Usage examples, Parameters, Outputs, ...) are skipped, as is any
  `<!-- BEGIN_... -->` block. Markdown under an ADR folder (`adr`, `adrs`, `decisions`,
  `docs/adr`, `docs/decisions`; `ACTIVITY_ADR_DIRS`) is `adr`. The changelog stays in
  `patches`.
- `{"collected": false}` alone — the window was gathered with `--no-pr-history`.

The fact holds each PR head **as gathered** — possibly after the window — so every
consumer drops commits committed after the view's end (the as-of rule). A force-push
before the gather is invisible: only the current head is fetched. A view of exactly a
gathered window carries the stored value verbatim; a wider view takes, per PR, the entry
of the latest overlapping window that collected one, and adds `windows`.

### One derived state per fact (story-layer views)

What the October issue's writers and its top-to-bottom read found contradicting itself,
derived once in link and carried on every row that needs it:

- **`review_state`** on every PR row and on each PR item of a slice outcome
  (`link.review_state`) — `{state, rule, as_of, basis? {reviewer, at, url}, last_push?,
  requested?}`, `state` one of `none`, `requested` (a review requested, nobody reviewed:
  `requested` names who), `automated_only` (only automation accounts reviewed),
  `commented`, `changes_requested`, `approved`, `approved_stale` (approved, then the author
  changed the PR: `last_push`, the latest own commit). The author's own reviews never
  count. `link.approved_pr` (forecast, ready lists) refuses `approved_stale` and
  `changes_requested`.
- **The last human event** (`link.last_human_event`): openings, comments, review comments,
  reviews, lifecycle events, merges / closes, merged commits, the PRs' own commits
  (`pr_history`) and their recorded events (`pr_activity`: review requests, assignments),
  by someone who can act (not automation, not the deleted-account login `ghost`). An open
  train's `effort` gains `last_human_event {at, kind, actor, url}`, and its
  `last_activity_at` / `idle_days` and the group rule follow it. The failing-checks stall
  rule fires only when EVERY open PR of the train is failing with no answer by its author.
  `stories.all[]` rows add `status_reason` (`link.status_reason`: "opened N days before the
  window's end; PR #n: <review state>; last human event M days before the end (<kind> by
  <actor>)") and `new_module_proposed` (the module areas whose entry file `main.bicep` /
  `main.tf` the train's PRs' own commits ADD — `pr_history` commits' `added` paths, never
  the title; a store gathered before `added` was kept has none).
- **Own changes only.** A PR commit's diff is the PR's own change only with exactly one
  parent (`link.own_change`): a merge of the base branch and a boundary commit read
  against an empty tree carry thousands of the base's files. Net change, interface rows,
  code notes, module derivation, open-PR classification and theme paths read own changes
  only; such a commit stays on the timeline with `not_own_change` (why) and no `change`.
  A commit committed before its PR opened (a reused branch) carries `before_pr_opened`.
  A `checks` item whose commit is not in the PR's gathered history (replaced when the
  branch was rewritten) carries `commit_not_in_history {committed_at, why}`.
- **Texts as last gathered.** On a view whose records are projected to its end
  (`meta.records_as_of`): numbers are handed out in order, so a PR `closes` /
  `crossref_issues` reference to a number above every record created by the instant names
  an item that did not exist then — it moves to `refs_after_window` and no train joins
  through it; a PR or issue whose body names one, or whose description `pr_activity` saw
  edited after the instant (`body_edited_at`), gains `body_as_of {state: "as last
  gathered", why}` (also on its `opened` timeline item). PR `labels` are read as of the
  end from the label history (the PR record's `label_events`: `labeled` / `unlabeled`
  `{id, event, at, actor, [actor_type], label}`, gathered from the same timeline) with
  `labels_as_of {state: "from label history" | "as last gathered", changed_after?}`.
- **Who can hold the ball.** `ball_with` / the issue ball never take an event by an
  automation account or the deleted-account login `ghost` as the last event (an
  automation re-request never outranks an outstanding change request). An issue's other
  party answering without a question is **I4**: `awaiting maintainer follow-up`. Automation
  reviews are on the timeline as `automated_review` items (never a person's turn); people
  rows are flagged from the API account type of PR / issue / comment / review authors and
  of `pr_activity` event actors (`actor_type`).
- **Releases.** `stories.all[].released` / outcome `released` rows add `tie` (`contained`
  with `declared_by` [pr] when another PR's changelog / version file declared the version
  — a sweep touching every module is never "the" release PR — or
  `first_tag_after_merge`) and child-module versions the train declared or changed
  (`child_of`). `module_changes.modules[].versions.published[]` gain `contents`:
  `{source: "changelog", prs, items?}` or, for a tag with no changelog entry,
  `{source: "commits_between_tags", from {tag} | {window_start}, to, prs, commits [{sha,
  subject, pr, url}], commits_more?, interface?, note?}` (`note`: no commit changed the
  module up to the tag, and which commit the tag sits on); a child version whose files nothing
  changed gains `note` "republished with its parent, no change of its own". A
  `child_module_moved` / `_removed` breaking candidate cites only the commit that moved /
  removed the child.
- **Plumbing.** A sweep that changes module source (`substantive_areas`) is product work,
  not plumbing. A theme is chosen by counting each path for the FIRST theme it belongs to
  (a workflow file is CI, not also a `.github/` policy file); module-specific e2e template
  assets are test environment. Every automation account's PR open in, or carried into,
  the window is in a theme (`carried_over` true when it had no event in it). Theme PR rows
  add the whole `body`, `body_length`, `reviewers`, `review_state` and `closed_unmerged`;
  `subjects[]` add the commit `body` (at most `PLUMBING_THEME_SUBJECTS_MAX`, 1000;
  `subjects_omitted`).
- **PR rows** add `fork_status` (`fork`, `deleted_fork`, `not_fork`, `unknown` when the
  record has no `head_repo`) — also on slice outcome items — and, when closed without
  merging, `closed_unmerged`: `{superseded_by, named_in, by, quote}` only on explicit text
  ("superseded by #N", "replaced by", "consolidated into"...), else `{close_reason: "not
  stated"}` (also on outcome items).
- A review thread whose GitHub flag was read after the view's end adds `status_note`, one
  sentence with both the flag and the as-of status.
- Text caps: a timeline text keeps `STORY_ITEM_CAP` (20,000) characters within
  `STORY_SLICE_BUDGET` (120,000); a changelog net excerpt keeps `STORY_NET_LINES_MAX`
  (400) lines, the breaking section included (gather's patch excerpts:
  `PR_HISTORY_PATCH_LINES`, 400).

### Who did it, and why things belong together (story-layer views)

- **A commit is its author's.** The last human event, `status_reason` and a ball's
  `basis` name a commit's own author — the login the window's walked commits resolved its
  git name to, else the git name — never the PR's author; the people of a train include
  the authors of its PRs' own commits. `status_reason` names the item each fact is about:
  "opened N days before the window's end (PR / issue #N); PR #M: <review state>; last human
  event K days before the end (<kind> by <actor> on PR / issue #J)"; `last_human_event`
  carries `on` `{type, number}`.
- **Stacked PRs.** A commit several PRs' histories hold belongs to the PR opened first: the
  others' copies get `belongs_to_pr` (`link.annotate_stacked_commits`) and are not their own
  change (net change, modules, notes, theme paths); the timeline keeps them with
  `belongs_to_pr` and `not_own_change`.
- **Why a member is in its train.** Trains carry `joined_by` `[{item {type, number}, via:
  closes | crossref, by {type, number}, quote | quote_absent}]` — the line of the PR's
  description that names the issue, else "a closing reference GitHub records (not in the
  description as gathered)".
- **Where a closed PR's work went.** `closed_unmerged.content_landed_in` `[{pr, basis: same
  commit | same commit subject | named in the merge commit message, commits [{sha,
  subject}]}]` — merged PRs that hold the very same commit, or whose own commit subjects,
  or whose merge commit's message, contain one of its commit subjects verbatim. Evidence
  only: `superseded_by` still needs explicit text. What kind of work a PR is (its plumbing
  class, theme paths) is read from its own commits, or — when another PR opened first
  holds every one of them (an identical copy) — from those shared commits.
- **Automation identities.** When reviews by automation account Y come only on PRs where a
  review was requested from automation account X (which never submitted a review itself),
  at least twice, Y's people row adds `automation.same_identity_as` `{login: X, basis,
  prs}` and X's `reviews_as` [Y]. A slice's participant rows show an automation account's
  `on_behalf_of` for that train's own PRs only.
- **The module owner who is the author.** An owner response request whose module's only
  owners are the item's own author names no owners (`author_is_module_owner: true`, also
  on the outcome's `owner_response_requested` row) and never moves the ball to "awaiting
  module owner": R2 falls through to the reviewers requested and not yet reviewing.
- **Threads an automation account opened** are no person's thread in the story, but are
  counted: `thread_coverage[pr].by_automation` `{threads, resolved}`.
- **Theme PR rows** add `net_files` `{files, added, removed, commits, top: [{path, added,
  removed}]}` — what a PR changed as a whole (gather `pr_net_files`), beside its
  description.

### What a story can name (story-layer views)

- **Linked items outside the window** — the per-window fact `linked_items`
  (`gather.collect_linked_items`: every issue / PR number the window's records link —
  closing and cross references, `#N` and repository links in bodies, comments, review
  comments and reviews — that is not a record of the window; ONE GraphQL
  `issueOrPullRequest` request per `LINKED_ITEMS_BATCH` (50) numbers, at most
  `LINKED_ITEMS_MAX` (500, `ACTIVITY_LINKED_ITEMS_MAX`) per window, the rest `capped`):
  `{collected, gathered_at, items: [{number, type, title, state (when gathered),
  created_at, closed_at?, merged?, merged_at?, state_reason?, url, author?}], missing:
  [number], failed?, capped?, requests}`; a view of several windows takes, per number, the
  latest window's row. A slice's `outcome.linked` row for such an item
  (`link.linked_item_as_of`) gives `type`, `title`, `author`, `url`, `created_at`,
  `state` AS OF the view's end (`not yet created` / `open` / `closed` / `merged`, from the
  dates), `state_when_gathered`, `closed_at` / `merged_at` / `state_reason` when by the
  end, `in_view: false`; a number not read says `not_read` ("not found", "not read
  (capped or failed)").
- **The module's owner** — `module_ownership.owners_at_end` `{module folder: [login]}`
  (the replayed state for the view's end day: `start_state` and the window's events; also
  `--recompute-ownership`) and CODEOWNERS rules naming people (`code_owners`, teams left
  out) give `module_owner_of: [module]` on people rows and slice participants
  (`link.module_owners`). An owner response request names those owners (`owners`,
  `owners_source: "owner_file"`) when no automation comment named them. Slice
  participants also carry `automation` when the people row has it.
- **What a force-push replaced** — a `head_ref_force_pushed` timeline item (or one of its
  own, `source: "pr_history"`) carries `replaced` `{status: read | unavailable, summary
  ("force-push on <day> replaced N commit(s): <subjects>"), commits [{sha, subject,
  files}] (at most 20; `commits_more`), reason?}` from `pr_history` rows' `replaced`.
- **Net change per file** — gather `pr_net_files`: a `pr_history` row's `net_files`
  `{head, base, files: [{path, added, removed}], files_omitted?}` — `git diff --numstat`
  from the merge base to the PR's head as of the window's end (`added` / `removed` null for
  a binary file; `net_files_unavailable` with the reason when the diff failed). A slice's
  `outcome.net_change[pr].net_files` lists the files MORE than one own commit touched,
  `{path, added, removed, commits}`, when the stored head is the head as of the end.
- **Sibling PRs** — an outcome PR item's `related` `[{number, title, state, url, basis}]`
  (`link.related_prs`): PRs of the same author opened within 7 days that the record links
  explicitly (one names the other, or both reference the same item); never from timing
  alone.
- **Who a commit's author is** — a PR commit item carries `author_name` (the git name) and
  `author_login` (the login the window's walked commits resolved that name to, null when
  none did — identities are never merged by name), and `authored_by_other: true` when that
  login is not the PR author.
- **A failed pipeline** — an outcome issue item of an automated "[Failed pipeline]
  <workflow>" issue carries `failed_pipeline` `{workflow, module?, run_url, run_id,
  job_url?, jobs?, steps?}` parsed from its title and body (no log download; the October
  2026 bodies name the run only).

### `module_ownership` (raw, from the store)

Who owns which module and the window's changes to it — "maintainer needed / added /
removed" (`gather.collect_module_ownership`; a per-window fact, carried into the view
as is for a view of exactly that window). Stored whenever ownership was collected, empty
or not (an empty fact of a later window clears an earlier window's standing list); absent
for stores gathered before it existed. Issues and PRs are read as they were at the
window's end (`extract.record_as_of`: one created later is skipped, one closed later is
open). Recompute it offline into an existing store from a clone of the repository with
the window's history:
`python3 gather.py --owner O --repo R --from F --to T --store S --clone-dir C --recompute-ownership`
(no API call; the window's other facts are kept).

**Every window is self-contained.** A window's fact holds everything its state is replayed
from, so no reader ever needs another window:
- `start_state` `{module folder: {owner_file?, owners, codeowners, orphan_marker,
  marker_since?, owners_since?}}` — the COMPLETE ownership state at the window's start,
  read from the tree of the newest first-parent commit before `from`
  (`gather.ownership_start_state`): every per-module owner file and its owners (and the
  day the file last changed), every orphan marker (the day it was added), the
  individuals CODEOWNERS names for a folder (teams left out). On
  Azure/bicep-registry-modules at 1 October 2026: 267 folders, about 45 KB.
  `start_state_source` says `complete`, or `markers_only` for a caller that had no tree
  before the window;
- the window's own dated `events` in recorded order (`at`: commit / item time; `seq`:
  gather's order — commit time, then the walk's order, one change's own events by kind);
- `labelled_items` `[{module, marker, opened, closed?, source}]` (a label's own time is not
  recorded: an item counts from the day it opened);
- `window` `{from, to}`, `gathered_at`; `without_owner` and `owners_at_end` — the window's
  end state, DERIVED by the same replay (kept as a cross-check: validate replays the fact
  and refuses a stored end that differs), and `end_check` `{matches, differences}` — the
  replay against an independent read of the end tree's owner files.

The state of ANY day inside a window (`extract.ownership_state`): `start_state` plus that
window's events up to the day, in recorded order, plus its labelled items open on the day
— a module with an orphan marker, or whose owner file lists nobody (`since`: the day it
changed), or named by an open owner-needed / orphaned item unless its latest change by
the day (or before the window, as `start_state` dates it) gave it an owner on or after
the item opened; `owners` `{module: [login]}` (owner file and CODEOWNERS individuals).

**Module lifecycle and what a module is** (the same fact, the same replay). A module
folder's lifecycle comes from the tree (`extract.module_lifecycle`): `deprecated` (a
deprecation marker, `ACTIVITY_DEPRECATED_FILES`, default `DEPRECATED.md`), `orphaned` (an
orphan marker), `live` (its entry file, `ACTIVITY_ENTRY_FILES`, default `main.bicep` /
`main.tf`), `stub` (its metadata file only: reserved, not yet published or removed), `removed`
(its entry file deleted in the window), or `proposed` (no entry file on the base, but an open PR's own commits by that day add the
folder's entry and metadata files: `proposed_modules` `{module: {prs, commit,
committed_at, info}}`, what the PR's own metadata file says the module is; a stub an open
PR fills is `proposed`, not `stub`; `modules_at_end` rows add `proposed_by` [pr]), or
`unknown` (a start state read before entry files were recorded). Every lifecycle marker's
first lines are kept verbatim: `deprecated_text` for a deprecation marker and
`marker_text` for an orphan marker (on `without_owner` rows too — it is the call to action).
When several modules carry the SAME marker text (boilerplate, compared after
normalising whitespace), the most-shared notice of each kind is said once:
`marker_texts` `{deprecated | orphaned: {text, modules}}` holds it (`modules`: how many)
and its rows say `marker_text_shared: true`; any other notice — another text shared by
other modules included — stays on its own rows; `shared_marker_texts` `[{kind, text,
modules: [module], count}]` lists every notice shared by more than one module. An owner
file created with an empty `owners` list is an `owner_needed` event with `file_added:
true` (metadata without an `owners` field is no owner file). A view that is not one
stored window keeps the covering window's `proposed_modules` committed by its end. A deprecated module's `replacement` `{module, quote, file}` comes only
from explicit text — its marker, README or CHANGELOG naming the module that replaces it
("replaced by / superseded by / moved to / migrate to <module>", "use <module> instead");
on the October 2026 tree all 12 markers are the same boilerplate naming none, and no README
or CHANGELOG names one. Deleting a populated owner file is `owner_removed` with
`file_removed: true` — the file (often the module) is gone, not an owner list emptied —
so a removed module is never "without an owner". Provenance is honoured: a fact whose
`start_state_source` is not `complete` (no tree before the window was read) has NO known
ownership state — gather stores `without_owner: []` with `without_owner_unavailable`, and
a view of it says so; its events are still reported. `start_state` rows add
`entry`, `deprecated`, `deprecated_since`, `deprecated_text` (the marker's first lines,
verbatim: it usually names the replacement) and `info` — what the module's metadata
(owner) file says it is: `display_name` (`moduleDisplayName`), `description`
(`moduleDescription`), `resource_type` (`canonicalType`), `alternative_names`. Lifecycle
events in the window: `deprecated` (with `marker_text`), `undeprecated`, `module_added`
/ `module_removed` (an entry file added / removed on the mainline under a module folder —
the authoritative "new module"), `module_info_changed` (with the metadata's new `info`).
The standing lists split by lifecycle: `without_owner` holds live, orphaned (and unknown)
modules only — each row with `lifecycle` and `display_name` / `description` /
`resource_type`; `deprecated_modules` and `stub_modules` list the others
`[{module, lifecycle, display_name?, ..., deprecated_since?, deprecated_text?}]`;
`modules_at_end` `{module: {lifecycle, display_name?, description?, resource_type?,
alternative_names?, ...}}`. ONE source of module lifecycle: this tree-derived state and
its events. `module_changes.modules[]` rows take `module_lifecycle`, `display_name`,
`description`, `resource_type` and `lifecycle_events` from it, and
`module_changes.lifecycle_check` `{new | removed | deprecated: {agree, only_module_layer,
only_tree}}` cross-checks the module layer's own window statuses against the tree's
events. `stories.all[]` rows add `module_info` `{area: {display_name?, description?,
resource_type?, lifecycle}}`, and slice outcome items and `outcome.modules[]` rows carry
the same. Every train of a story-layer view carries `modules` / `modules_source` (the
module areas `stories.all` shows, `link.train_modules`) and, with no merged code,
`pr_code_areas` (every area its PRs' own commits touched by the view's end; `code_areas`
stays the merged commits' areas). Refresh offline from a clone (`--recompute-modules` is the same flag as
`--recompute-ownership`): `python3 gather.py --owner O --repo R --from F --to T --store S
--clone-dir C --recompute-modules`.

**Views.** A view that is not exactly one gathered window reads its state from the ONE
stored window covering its END day (`extract.covering_window`: the one gathered latest,
then the latest end, the latest start), named in `as_of_window`: `without_owner` and
`owners_at_end` are that window's replay for the view's end day (an owner added after a
view's end is never in it). Events REPORTED are those of every overlapping window dated
in the view, each change once (`extract.ownership_event_identity`: source commit / item,
kind, module, people, day — never `at` / `seq`; a refreshed copy replaces a legacy one and
its order is the refreshed fact's); `unread` is merged. No window covers the end day, or
the covering fact is legacy (no `start_state`) and the day is not its own end: `without_owner`
is empty, `owners_at_end` absent, and `without_owner_unavailable` says why (the legacy
reason names `--recompute-ownership`). The carry-across-windows design (a later window's
state rebuilt from earlier windows' events) was dropped: it needed every earlier window
loaded, broke whenever windows overlapped, were legacy, or ordered one second's changes
differently, and could not know an owner list emptied before the first gathered window.
The same history stored as one month, a month plus carried days, adjacent months, weekly
windows or a legacy fact plus a refreshed overlapping window reads back identically for
every day a self-contained window covers. validate checks the type of every field a reader
indexes, slices, sorts or keys on (`validate._OWNERSHIP_ROW_TYPES`,
`OWNERSHIP_STATE_SHAPE`). Rewrite a stored window in this shape, offline, from a clone:
`python3 gather.py --owner O --repo R --from F --to T --store S --clone-dir C --recompute-ownership`.

- `without_owner` — the window's end state (above): modules carrying an orphan marker,
  modules whose owner file lists nobody, and modules named by an open item with an
  owner-needed / orphaned label unless the module gained an owner (or was adopted) on or
  after that item was opened.
- `unread` — owner-file / CODEOWNERS versions that could not be read or parsed (`reason`:
  "not valid JSON"); no change is derived from them, never "no owners". A Terraform module repository has CODEOWNERS only: the other sources are
  simply absent.

### `pr_activity` (raw, from the store)

The per-window fact gather writes (`gather.collect_pr_activity`; STORE.md), omitted when
no gather stored it (a store gathered before it, every fixture bundle):

```json
{"collected": true, "gathered_at": "2026-10-02T06:00:00Z",
 "limits": {"batch": 10, "max_prs": 200, "commits": 10, "contexts": 50,
            "threads": 50, "force_pushes": 20, "events": 100},
 "requests": 16, "points": 96,
 "prs": [{"number": 7146, "checks_status": "gathered", "head": "<sha>",
          "commits_total": 9,
          "commits": [{"sha": "…", "committed_at": "…Z", "rollup": "failing",
                       "state": "failure",
                       "checks": [{"name": "…", "kind": "check_run",
                                   "status": "completed", "conclusion": "failure",
                                   "started_at": "…Z", "completed_at": "…Z",
                                   "url": "…"}]}],
          "threads_total": 2,
          "threads": [{"comment_id": 3408199937, "path": "…", "line": 78,
                       "resolved": false, "outdated": true}],
          "events": [{"id": 1, "event": "renamed", "at": "…Z", "actor": "…",
                      "from": "…", "to": "…"}]},
         {"number": 6974, "checks_status": "capped"}]}
```

- `checks_status` — `gathered`, `capped` (beyond `ACTIVITY_PR_CHECKS_MAX`: open PRs are
  asked first, then the most discussed, then the newest), `failed` (its request failed,
  or a GraphQL field error named the PR — partial data for it is not trusted; an error
  naming no PR fails its batch; `checks_reason`), `not_found` (GitHub answered null),
  `not_requested` (`--no-pr-checks`
  or no GraphQL client). Only a `gathered` row says anything about checks; nothing is
  ever "no checks" for a PR that was not asked about.
- `commits` — the latest `PR_CHECKS_COMMITS` (10) of the PR's commits, oldest first, each
  with its check runs and commit statuses (`kind` `check_run` / `status`; at most
  `PR_CHECKS_CONTEXTS` 50, `checks_omitted` the rest), `rollup` (gather.checks_rollup over
  them, as gathered: a conclusion in `failure`, `timed_out`, `cancelled`,
  `action_required`, `startup_failure`, `error` fails it; `success`, `neutral`, `skipped`
  pass; anything else is pending; failing wins over pending, pending over passing; no
  check at all is `none`; computed over the checks read — a reader passes
  `checks_omitted` and gets `incomplete` unless a failure is visible) and `state`
  (GitHub's own roll-up state). `commits_total` is the
  PR's commit count; `head` its head commit when gathered.
- `threads` — GitHub's resolved flag per review thread (`comment_id` is the thread's
  opening review comment, `resolved_by` who resolved it), at most `PR_THREADS_FIRST` 50
  (`threads_omitted`). GitHub keeps no resolution time.
- `events` — the PR lifecycle events from the issue timeline enrich_pr already fetched:
  `renamed`, `convert_to_draft`, `review_requested`, `review_request_removed`,
  `review_dismissed`, `head_ref_force_pushed`, `assigned` (with `assignee`; who handed
  a coding agent its work) (the latest `PR_EVENTS_MAX` 100,
  `events_omitted`); a force-push gains `before` / `after` from the GraphQL read; an
  event's `actor_type` is GitHub's account type of its actor when not `User` (`Bot`).
- `body_edited_at` — when the PR description was last edited (GraphQL `lastEditedAt`):
  edited after a view's end, the description is "as last gathered" for it.
- **Every commit** (`collect_check_history`, the second phase): when a PR has more
  commits than the latest 10, the older ones are read in pages of
  `PR_CHECKS_HISTORY_PAGE` (100) with GitHub's roll-up state only — rows `{sha,
  committed_at, state, rollup, checks: [], state_only: true}` (`rollup` from the state:
  success passing, failure / error failing, pending / expected pending) — and the check
  contexts of every commit whose state is not a success are then read
  (`PR_CHECKS_DETAIL_BATCH` 20 commits per request, the flag dropped); a commit cut at 50
  contexts without a visible failure is paged to the end (at most
  `PR_CHECKS_MAX_CONTEXTS` 500), so `incomplete` is left only where a cap or a failed
  request stopped the read. `history_status` (only for a PR with older commits):
  `complete`, `capped` (`ACTIVITY_PR_CHECKS_MAX_COMMITS`, default 1,000 per PR, or the
  window's `ACTIVITY_PR_CHECKS_MAX_REQUESTS`, default 400; `history_reason`) or `failed`
  (`history_reason`). Cost: a page about 1 point, a detail request about 1 point. October
  2026 of Azure/bicep-registry-modules (160 PRs, 1,284 commits, 28 PRs with more than 10):
  about 31 page requests and about 15 detail requests (38 % of the commits read in full
  were not passing) on top of the 16 first-phase requests / 96 points — about 63 requests
  and 150 points, well inside the built-in CI token's 1,000 points an hour.
- `requests` / `points` — the GraphQL requests made and the rate-limit points they cost.
- `{"collected": false}` is never written: `--no-pr-checks` still stores the events.

Everything is **as gathered** (the window's end plus however long until the gather), so
the story layer projects it: commits committed after the view's end are dropped and a
check completed after it counts as pending. A wider view takes, per PR, the row of the
latest overlapping window (each row then carries its own `gathered_at`) and adds
`windows`.

## Optional (omit-when-empty) keys

These keys are reserved in the bundle skeleton and **filled when the data exists** —
by the later pipeline steps (`link`/`render`) or when the repo actually has them (a
linked board, releases, CODEOWNERS, …). They are empty/absent when there is nothing to
report (the omit-when-empty rule that keeps the shape diff-stable), **not** permanently
empty. The groups below document each once it's populated.

`timeline, artifacts, feature_deltas, code_owners,
code_graph, label_taxonomy, modules, workflow_stats, newcomers, module_notes,
module_changes, pr_history, pr_activity, affiliations, prior_activity, contributors,
stories, releases,
milestones, sprints, diagrams`.

**Never in a view:** gather's in-memory bundle skeleton also reserves `workflows` (the raw
CI runs `workflow_stats` is counted from; never folded, so no view carries it), and
`docsRefs`, `release_train` and `project`, which nothing fills. Do not read them.

## Engagement, buckets, diagrams & forecast fields

- **prs[]** gain `created_at`, `updated_at`, `milestone`, `comments`,
  `review_comments_count`, `reviewers`, `review_decision`
  (`approved|changes_requested|commented|none`), and `crossref_issues`. See
  *Review decision & rounds* below for the exact rules.
- **issues[]** gain `milestone`, `updated_at`, `comments`. Open and
  not-planned-closed issues are now included, not just PR-closing issues.
- **Projects v2 board** (only when gather read a board — the token's `read:project` scope;
  absent otherwise): **prs[] / issues[]** gain **`board_status`** — the item's Status
  single-select value on the board (e.g. `"Todo"`, `"In Progress"`, `"Done"`), present only
  when the item is on a board and has a status — and **`iteration`** — the id of the board
  iteration (sprint) the item is in, a key of `sprints`, present only when that sprint
  overlaps the gathered window. **sprints** — `{ "<iteration id>": { title, start, end } }`
  (`start` the iteration's `YYYY-MM-DD` start date, `end` its last day, from the start date
  and duration), the board's iterations (current and completed) that overlap the gathered
  window; `{}` on a status-only board. `link.select_sprints(sprints, ref_date)` returns the
  `(previous, current, next)` `{id, title, start, end}` dicts (or `None`).
- **releases[]** — `[{ tag_name, name, published_at, prerelease:bool, url, notes?,
  notes_truncated_from? }]` (the GitHub release's tag, title, publish timestamp,
  prerelease flag and `html_url`; `notes` its text, capped at 4000 characters, only when
  it has any), the releases published in the view's window, sorted by `tag_name` (else
  `name`).
- **milestones[]** — `[{ title, number, state:"open"|"closed", due_on, open_issues,
  closed_issues, url }]` (`due_on` an ISO timestamp or `null` for an undated milestone; the
  issue counts as GitHub reported them at the latest gather). Not windowed (see "Window
  scope"). `select_milestones` (below) picks current / next from them.
- **workflow_stats{}** — `{ "<workflow name>": { total, success, failure, cancelled, other } }`
  (ints): the window's CI runs per workflow name by `conclusion`; `other` counts every other
  conclusion (skipped, neutral, timed out, still running, …), so `total = success + failure +
  cancelled + other`. `workflow_stats` is keyed by
  workflow name, plus the reserved key `_truncated` ONLY when GitHub's 1,000-run
  search cap could not be split away: `{truncated, cap, fetched, total_count,
  ranges}`. Consumers listing workflows skip keys starting with `_`. A view summing several
  gathered windows (`meta.facts_windows`) adds the counters per workflow name and merges the
  `_truncated` markers (`fetched` / `total_count` summed — `null` if any is unknown —,
  `ranges` concatenated, largest `cap`).
- **buckets** are fully classified: `shipped`, `rejected`, `in_flight`,
  `next_candidates` (one bucket per item; precedence shipped > rejected >
  next_candidates > in_flight). Each ref may carry a `train` id. An open item is a
  next candidate when it is on the next milestone, has a priority label or is an
  approved PR (both below, under **forecast**). The current/next
  milestones come from `select_milestones`: open milestones **with a due date** only
  (undated ones, e.g. "Backlog", are never selected; none dated → no current/next);
  current = earliest due on/after `ref_date`'s day, next = the one after; if all are
  overdue, current = the most recently due and next = none. Ties break by number.
- **diagrams{}** (only on a bundle view file `render.py` rewrote) — `{ buckets_pie,
  timeline_gantt, content_timeline, deltas_bar, contributor_graph, kind_breakdown,
  module_graph, blocker_graph: "<path>.mmd", train_flowcharts: {"<train-id>": "<path>.mmd"},
  truncated: {"<name or train-id>": {shown, total, unit}} }`. Every flat key is always written
  (an empty input gives a placeholder diagram, e.g. `No module dependencies`); paths are
  relative to the parent of `--diagrams-dir`. `buckets_pie` / `timeline_gantt` are
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
  notes:[str,...] } ][, notes:[str,...]][, priority_patterns:[str,...]] }`
  — forward-only next-release forecast over `buckets.next_candidates`, sorted by score
  desc. `next_milestone` is the bucket rule's next milestone (always dated; `None` when
  there is no upcoming dated one after current). Signals: `on milestone <title>` / `high-priority` /
  `approved, no later change request` / `open PR` or `work in progress`
  / `active in window` / `long-open`. **Priority labels (#97 final gate):** a label is
  high priority when it is one of `link.HIGH_PRIORITY_LABELS` (exact, case-insensitive) or
  matches one of `link.PRIORITY_LABEL_PATTERNS` (regular expressions searched
  case-insensitively: priority high / critical / urgent / P0 / P1, high priority, a bare
  P0 / P1, urgent, critical (not "non-critical"), blocker), plus any
  `link.py BUNDLE --priority-label PATTERN` (repeatable), which are then listed in
  `forecast.priority_patterns` (absent without the flag). **Approved PR:** an open,
  unmerged PR that is not a draft (`draft` true; gather does not record it yet, so it is
  read when present) with an `approved` review by someone other than its author and no
  `changes_requested` review submitted after the latest such approval, as the view's
  `reviews` record them (`link.approved_pr`; a dismissed approval reads `dismissed`; the
  PR's `review_decision` is today's and is not used). It needs no milestone discipline and
  scores 3.0 (with `open PR`, 5.0: likely); next month's `series.forecast_loop` checks
  whether it shipped. **`forecast.notes`** (absent otherwise) — when there is no
  candidate although the view holds an open PR or issue, one plain sentence per signal
  source saying why (no usable next milestone and the reason, no priority label, no
  approved PR); a view with nothing open has no notes. The in-motion signal (#87) needs live work: `open PR`
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
  `{ first_installment:bool, prior:{from,to,captured}|None, new:[row], new_opened:[row],
  carried_over:[row], forecast_loop:{ landed:[loop], not_yet:[loop], dropped:[loop] } }`.
  The comparison is against the installment recorded for the window **immediately before**
  this bundle's window (`meta.period`, else `meta.from`/`meta.to`) in the index — not the
  last one appended. A `row` is `{type, id, url, bucket}` (`bucket` = this window's bucket);
  carried rows add `prior_status` (the prior board status, else `"in_flight"`). `new` = this
  window's `shipped` / `in_flight` items the prior installment did not know at all (in none
  of its buckets nor its forecast) — new to the **series**, not newly opened: an old issue
  first touched this window is in it. `new_opened` (#97 final gate) = the rows of `new` whose
  PR / issue `created_at` falls inside this window (by calendar day; `[]` on the first
  installment): the work actually opened this window. `carried_over` = items in the prior's `in_flight` that
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
  one submitted review (`APPROVED`/`CHANGES_REQUESTED`/`COMMENTED`/`DISMISSED`),
  **never the PR author** (#110): the author's own replies in a review thread are
  `COMMENTED` reviews, and they are left out of `reviewers` and of
  `review_decision`, the same rule as review rounds. So `people[].prs_reviewed`,
  review latency, the halls and `trains[].effort.reviewers` (all read from
  `reviewers`) never count an author reviewing their own PR, and the fold writes
  no `reviewed` edge from the author to their PR. A PR whose only reviews are the
  author's has `reviewers: []` and `review_decision: "none"`. The rule applies at
  gather time: a store gathered before it keeps the author in `reviewers` until
  that window is gathered again.
- **`prs[].review_decision`** (`gather.summarize_reviews`) — per reviewer (the PR
  author excluded, above), the
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
  when known, else its `date`; ties keep git/ledger order). A view from a store holds
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
- **diagrams{}**: `content_timeline` (Mermaid `timeline`) and `deltas_bar` (Mermaid
  `xychart-beta` bar).

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
  source — the entry file's `module` references plus those of every file of the same area it
  reaches through local references (a `./resources.bicep` beside the entry file is part of the
  module, not a dependency on itself; a file in a subdirectory is a private child, named
  `<area>/<child>` with `local: true`). **Bicep has no transitive edges**: they used to be guessed from the names of nested
  ARM deployments and were mostly wrong on real repositories (final-gate finding, #97); what a
  module reaches indirectly is the closure of the direct edges. A store gathered before this
  drops its guessed edges of an area the next time that area's build resolves. **Terraform (#80):** the direct edges are the area's
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
  fold may retract a stored direct `depends_on` it no longer has). A module file the scan
  could not read (its real path leaves the checkout, a dangling symlink, unreadable; issue
  #109) is skipped with one stderr warning and its area is listed in
  `code_graph.structural_scan_unread` (sorted area ids; absent when every file was read):
  the scan does not speak for those areas. Without the flag a
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
  `{ "resolved", "timeout", "failed", "skipped" }` counts (plus `"disabled": true` when gathered
  with `--no-iac-build`, which skips every build) — so a build that timed out or failed
  is a **visible, counted gap**, never mistaken for a module with no dependencies. `resolved`
  with an empty `edges` list genuinely means "no inter-area dependencies"; `timeout`/`failed`
  mean "not determined" (re-run to resolve). The integration gate reds when the unresolved rate
  is non-trivial, so an incomplete graph is re-run rather than silently shipped.
- **code_owners** `[ { "pattern": "<path|glob>", "owners": ["login", ...] }, ... ]` —
  parsed from the clone's CODEOWNERS (`.github/`/root/`docs/`), one rule per line **in
  file order** (duplicates kept). A CODEOWNERS whose real path leaves the checkout (a
  symlink) or that cannot be read is skipped with one warning: `code_owners` stays empty and
  `code_graph.codeowners_unread` names the file, so the fold does not treat it as "no
  owners" (issue #109); undecodable bytes are replaced, never fatal. `@org/team` and `@user` are kept as logins (the `@`
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
- **trains[].code_areas** — the distinct areas of a train's commits' files, sorted. (A
  project train's `code_areas` are these, repo-qualified: see "Project view: `trains`".)
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
- **diagrams{}**: `contributor_graph` (Mermaid `flowchart`, people↔code-area edges),
  `kind_breakdown` (Mermaid `pie`, issues by kind) and `module_graph` (Mermaid `flowchart` of
  the resolved `code_graph.areas[].edges`: area → area labelled with the pinned `version`, else
  `transitive`, or `child` / `child[]` for a local child module; an unresolved edge (`to:
  null`) is not drawn; a `No module dependencies` placeholder when none resolve).
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
    "commits":        [ { sha, message*, author, date, url } ],
    "feature_deltas": [ ...this train's deltas only... ],
    "feature_deltas_diff_overflow": <int>,
    "symbol_moves":   [ ...moves whose from/to artifact is in this train's deltas... ],
    "conversation":   { timeline, turning_points, truncation, bots,
                        code_notes_by_module, pr_history, as_of, not_gathered,
                        outcome, threads, gaps }
  }
  ```

  The `train` block also carries `discussion`, `story`, `story_rank`, `story_score` and
  `story_reasons` when the train has them, and `conversation` is the train's story
  material: the whole conversation as one chronological timeline (see "The story layer"
  below for every key). The capped `comments` / `reviews` / `lifecycle` lists of the
  `issue` and `prs` blocks are unchanged for older narrators; a story narrator reads
  `conversation`.

  When the view's people carry a display name or the maintainer property (gathered with
  the people registry), or the view has `people_context` (`link.py --people`), the slice
  also has `participants` {login: {name?, maintainer?, maintainer_signal?,
  maintainer_status?, affiliation?, registry_context?}} for the train's humans ("The
  people layer"); `registry_context` is the person's `people_context` entry — registry
  context, not a fact of the window.

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
  A commit row's `url` is `https://github.com/<meta.owner>/<meta.repo>/commit/<sha>` (built
  like `feature_deltas[].url`), so a narrator can cite a commit by link.
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
  | `prs_authored` | PRs in the view with `author == login` (int): every in-scope PR — opened in the window, or opened earlier and still open or closed in it — NOT "PRs opened this month" |
  | `prs_opened` | of those, PRs whose `created_at` falls inside the window (`meta.period`, by calendar day) (int). Present on every person only when the view holds a PR created outside the window; absent otherwise, and then equal to `prs_authored` (#97 final gate) |
  | `prs_merged` | of those, PRs with truthy `merged` (int) |
  | `merge_rate` | `round(prs_merged/prs_authored, 3)`, `null` when 0 authored |
  | `prs_reviewed` | PRs where login is in `reviewers` (int) |
  | `commits_authored` | commits with `author == login` (int) — the true count |
  | `own_pr_commits` | of those, commits whose `pr` is a merged PR authored by the same login — the landing (squash) commit of their own PR (int); absent when 0 (#97 final gate) |
  | `issues_opened` | issues with `author == login` (int) |
  | `review_latency_days` | median, over reviewed PRs, of (login's earliest `reviews[].submitted_at` − `pr.created_at`) in days; `null` when no timestamped review |
  | `first_seen` / `last_active` | min / max ISO date (`YYYY-MM-DD`) across the login's activity (PRs authored/merged-by, commits, issues, reviews); `null` when none |
  | `examples_authored` / `docs_authored` / `symbols_authored` | distinct artifacts with an `add` lifecycle event by this login, bucketed by `kind` (example; doc+readme; symbol) (int) |
  | `authored_then_removed` | distinct artifacts this login added whose `status` ∈ `{removed, dropped, replaced}` (int) |
  | `stale_owned` | distinct areas in a stalled train's `code_areas` (an open train with `effort.stalled`, #112) that this login owns under `code_owners` (`derive.area_owners`: last rule matching the area directory) (int) |
  | `affiliation` / `affiliation_signal` | only when the view carries a collected `affiliations` fact, on non-bots: `maintainer_org` / `community` / `unknown` and the deciding signal ("The people layer") |
  | `name` / `maintainer` / `maintainer_signal` / `maintainer_status` | only when the view's `affiliations` fact was gathered with the people registry, on non-bots the fact names (never for a `do_not_name` person): the display name and the maintainer property for the view's repository, with its lifecycle status at gather time when the registry holds maintainer evidence ("The people registry") |

  Counts are always present (ints, may be 0), except `prs_opened` and `own_pr_commits`
  (present when applicable, above); `merge_rate`/`review_latency_days`/
  `first_seen`/`last_active` are present as `null` when N/A. Deterministic.
- **halls** `{ "fame": [ { login, score, prs_merged, prs_reviewed,
  commits_authored, areas[, own_pr_commits] } ] }` (`build_halls`) — **recognition only.** `fame`
  ranks NON-bot logins by footprint
  `score = prs_merged*2 + prs_reviewed + (commits_authored - own_pr_commits)`
  (`own_pr_commits` 0 when absent), keeps `score > 0`, sorts by `(-score, login)`, takes the top
  10 (else `[]`). A commit that lands the person's own merged PR (a squash commit) already
  scores through `prs_merged`, so it is not counted again (#97 final gate): on a squash-merge
  repo an authored-and-merged PR scores 2 and a review 1; only commits not attributable to
  the person's own merged PRs (direct pushes, commits on others' PRs) add to the score. An
  entry carries `own_pr_commits` when it is not 0. No other weight changed. **`halls.internal`/`shame`/`blame` are intentionally NOT built**
  — per the "recognition, not blame" stance.
- **issues[].blocks** — `[{ number, direction:"out"|"in" }]`, the blocking references gather
  parsed from that issue's own text (`gather.parse_blocks_refs`), in text order, deduped:
  `"out"` = this issue blocks `#number` ("blocks #N"), `"in"` = this issue is blocked by
  `#number` ("blocked by" / "depends on #N"). Present only on an issue whose text has one.
- **Blocker state.** `issue["blocking"]` / `issue["blocked_by"]` (sorted issue numbers, absent
  when empty, set by `extract._attach_blocks` from the stored `blocks` edges in both directions, so an issue
  named by another issue's text carries them too) are the **raw stored relation**, whatever
  the state of
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
