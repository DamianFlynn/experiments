# STORE.md — journey-graph schema

`graphstore.py` is a stdlib-only SQLite property graph: the durable,
identity-keyed substrate that `gather` writes and `extract`/`spotlight` read.
Readers, writers, and downstream renderer authors rely on this file as the
contract. `graphstore.py` owns the schema and the core read/write API; callers
use that API (a couple of readers also run their own read-only queries).

> **Store-only.** This trustworthy graph IS the deliverable.
> `gather --store` writes it and nothing else — there is no longer a flat bundle
> JSON artifact. The bundle is a **transient view** that `extract` materializes
> from the store on demand. The report vertical (`extract → link → render →
> report`) **composes from the store** — each stage reads the materialized view,
> guarded by the golden-bundle equivalence gate. The store stands alone, proven by
> `validate.py` (below), which self-sources everything it needs from the store.

## Identity (qualified ids)

Every node id is namespaced so multi-repo data cannot collide:

- Repo-scoped: `{project}/{repo}#{local}` — e.g.
  `avm/bicep-registry-modules#pr-4821`, `…#issue-17`, `…#<sha>`,
  `…#<path>#<lang>:<subkind>:<name>` (artifacts).
- Project-scoped (people only): `{project}#person-{login}` — a person
  aggregates across all repos in a project.

Helpers: `qualify_id`, `qualify_person`, `parse_id`, `local_id`,
`split_symbol_local` / `is_symbol_local` / `is_artifact_local`.

### Parsing grammar (readers)

- **Scope vs local: split on the FIRST `#`.** The scope (`{project}/{repo}` or
  `{project}`) never contains `#` (GitHub owner/repo names and logins cannot;
  a manifest `project` name cannot — `manifest.load_manifest` rejects `/` and
  `#` in it; `local_id(qid, project, repo)` strips a known prefix exactly). A local id MAY contain `#`, so
  everything after the first `#` is the local id. `parse_id` implements this;
  `local_id(qid, project=None, repo=None)` returns the full local id (stripping
  exactly `{project}/{repo}#` when both are given). Never `rpartition` on `#` —
  that truncates `<path>#<lang>:<subkind>:<name>` to `<lang>:<subkind>:<name>`
  (and to an empty local for a comment named `#### networking ####`).
- **Local forms.** `pr-N`, `issue-N`, `comment-…`, `review-…`, `event-…`
  (social); a bare `<sha>` (commit); `art:<path>` (file artifact; the path may
  contain `#`); `<path>#<lang>:<subkind>:<name>` (symbol/comment artifact; both
  `path` and `name` may contain `#`, `name` may contain `:` or be empty, `lang`
  may be empty); `area-…`, `milestone-…`, `release-…`, `sprint-…`, `person-…`
  and the singletons (structure). Readers test the artifact forms BEFORE the
  `pr-`/`issue-`/`release-`/… prefixes, because a symbol's path can start with
  one of them (`issue-templates/main.tf#terraform:…`).
- **File path containing `#` vs symbol id** (`split_symbol_local`). The symbol
  separator is the FIRST `#` that follows a non-empty path and is followed by
  the tail shape `<lang>:<subkind>:` where `lang` matches `[A-Za-z0-9_+-]*` and
  `subkind` matches `[A-Za-z0-9_-]+` (no `#`, `:`, `/`, `.` or whitespace);
  the rest is the name. A `#` inside a path is followed by more path text
  (`docs/C#-guide.md`, `C#/main.tf#terraform:resource:x`), which never has that
  shape, so it is skipped. An id starting `art:` is never a symbol. This matters
  for the `code_events` ledger, whose file-level rows are keyed by the bare
  `<path>`: a row is a symbol row iff its local id has that tail.
- **Residual ambiguity.** A file path in which a `#` is directly followed by
  `<token>:<token>:` (e.g. `notes#a:b:c.md`) would be read as a symbol id. Git
  paths with `:` are rare (and invalid on Windows); no such path is expected.

## Node classes (`nodes` table)

One row per entity. Columns: `id` (PK), `project`, `repo`, `node_class`,
`ts`, `data` (JSON blob of the full record), `fetched_at`.

| node_class | holds | `ts` is |
|---|---|---|
| `social` | PRs, issues, comments, reviews | merged/closed/created date |
| `code`   | commits, artifacts (incl. symbols/comments) | author/event date |
| `structure` | code areas, milestones, releases, sprints, people | point-in-time / NULL |

Identity columns (`project`/`repo`/`node_class`) are immutable; `ts`/`data`/
`fetched_at` refresh on re-upsert. Re-folding an overlapping window mutates
nothing already correct — the dedup guarantee is durable, not recomputed.

`structure` nodes typically carry NULL `ts` and are excluded from window
range scans (they are not activity).

**`ts` granularity and window bounds.** `ts` is mixed: commits and artifacts
store a date-only `YYYY-MM-DD`; PRs, issues, reviews and events store an ISO
datetime. Window bounds may also be either form (`--to 2026-05-31` or
`2026-05-31T23:59:59Z`). Every window check — `range_query`'s SQL and the
Python predicate `graphstore.ts_in_window(ts, from, to)` that `complete`,
`spotlight`, `gather.in_window` and `link._in_window` delegate to — applies one
rule: each bound is compared at the **coarser** granularity of the pair, so a
date-only `ts` or bound covers its whole day; only datetime-vs-datetime is
compared exactly. Both ends are inclusive, either bound may be None. So a PR
merged `2026-05-31T15:00:00Z` is inside `--to 2026-05-31`, and a commit dated
`2026-03-01` is inside `from=2026-03-01T00:00:00Z`.

## Edges (`edges` table)

`(src_id, dst_id, edge_type, ts, data)`, PK `(src,dst,type)`. Re-upsert
unions, never appends. Edge types:

`closes` (pr→issue), `part_of` (commit→pr), `cross_ref` (issue↔pr↔commit),
`duplicate_of`/`spun_off` (issue→issue), `touches` (commit/pr→area),
`authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted` (person→node),
`owns` (person→area), `depends_on` (area→area, carries version/transitive in
`data`), `replaced_by`/`identity_from` (artifact→artifact), `blocks`
(issue→issue), `in_milestone`/`in_iteration` (social→structure).

`edges_by_type(edge_type, project)` selects the edges whose `src_id` starts
with `{project}/` — **case-sensitively** (`Azure` and `azure` are different
projects) and as an index range (`src_id >= '{project}/' AND src_id <
'{project}0'` on `idx_edges_type_src (edge_type, src_id, dst_id)`), never a
`LIKE` (case-insensitive in SQLite, so it cannot use an index and mixed
projects that differ only in case). `repo_code_events(project, repo)` filters
`code_events.artifact_id` on the `{project}/{repo}#` prefix the same way.

### Spine edges and decision trains

A decision train is **not stored** — it is the connected component reachable
from a root issue over the *spine* edge allowlist:
`SPINE_EDGE_TYPES = (closes, part_of, cross_ref, spun_off, duplicate_of)`.
`traverse_spine(seed_ids, max_depth, edge_types)` walks these **undirected**
(both directions), depth-capped, and returns `{reached: {id: depth}, missing:
[id]}`. `missing` ids are spine nodes referenced but not yet stored (e.g. an
out-of-window issue a PR closes) — what a reader backfills.

Where the spine edges come from (gather, #62). `closes` follows GitHub's own
closing-keyword rule: `close[sd]`/`fix(e[sd])`/`resolve[sd]` (any case), optional
`:`, then one `#N`, `owner/repo#N` or `https://github.com/owner/repo/issues/N`.
Each closed issue needs its own keyword — `Fixes #1, #2` closes only #1 (#2 is
kept as a same-repo cross-reference in `crossref_issues`), while `Fixes #1, fixes
#2` closes both. A same-repo qualified ref or URL is a same-repo `closes`; a
cross-repo one goes to the cross-repo edge path below; a keyword before a
`/pull/` URL is a `cross_ref`, never a close. Keywords are matched in code blocks
too (no Markdown parsing). `part_of` uses the commit subject's **trailing**
`(#N)` first (so `Revert "x (#4012)" (#4020)` → PR 4020), then `Merge pull
request #N`, then the last `(#N)`.

## Code-event ledger (`code_events` table)

One row per artifact lifecycle event: `(artifact_id, event, commit_sha,
author, date, hunk, ref, before, after, detail)`, PK `(artifact, commit,
event)` → set semantics (re-seeing a commit is a no-op). An artifact's
`lifecycle[]` is `get_code_events(artifact_id)` ordered by date.

## Text index (`fts_text`, FTS5)

`index_text(node_id, text)` / `index_texts([(node_id, text), ...])` (replace
any text already indexed for the node; idempotent) feed an FTS5 index;
`fts_search(query)` returns matching node ids ranked by relevance. Created only
when the SQLite build supports FTS5 (`fts5_available`); the text-mining
spotlight query depends on it.

`fts_text(node_id UNINDEXED, text)` cannot look a row up by `node_id` without
scanning the whole virtual table, so (schema v2) the side table
`fts_map(node_id PK, fts_rowid)` records which `fts_text` rowid holds each
node's text. Re-indexing is a primary-key lookup in `fts_map` plus a rowid
delete/insert in `fts_text` (same rowid reused), so a fold's FTS cost is linear
in the fold, not in the store. `fts_map` is created and back-filled by
`init_schema`, and lazily by `index_text(s)` on a store that skipped
`init_schema` (e.g. `validate`'s in-memory idempotency copy).

**Populated on the fold path.** `fold_bundle` indexes searchable
text per owning node id, keyed so a hit hydrates back to the node it came from:
PR/issue `social` nodes index their title + body + embedded comment/review
authors+bodies; commit `code` nodes index the commit message. The call is
FTS5-gated (skipped silently when the build lacks FTS5 — the store stays valid)
and idempotent (re-indexing a node id replaces its text, so re-folding an
overlapping window re-indexes cleanly without duplicates).

`fts_search` passes `query` straight to the FTS5 `MATCH` operator, so the
argument must be a **valid FTS5 query**. Callers searching user-derived terms
(e.g. the P8 spotlight text-mining query) must quote/escape them — FTS5
operators (`AND`, `OR`, `NOT`, `*`, `"`, `-`, `:`) and unbalanced quotes are
significant and otherwise raise `OperationalError`.

## Meta (`meta` table)

Key/value provenance: `schema_version` (see below); `gathered_windows` (JSON list of
folded `{project,repo,from,to}`, deduped — `record_window`/`get_windows`);
`clone_sha:{project}/{repo}` (the tree a repo was last gathered against, for
deterministic resume/roll-up — `set_clone_sha`/`get_clone_sha`).

### Schema version and migration

`graphstore.SCHEMA_VERSION` is **2**. `init_schema` creates a new store at the
current version and migrates an older one **in place**; every step is additive
and idempotent, and the stored version is raised, never lowered.

| Version | Change | Migration from the previous version |
|---|---|---|
| 1 | initial schema | — |
| 2 | `fts_map` side table; `idx_edges_type_src` index (issue #55) | create the index; create `fts_map`, collapse any duplicate `fts_text` rows per node (newest kept) and back-fill the map from `fts_text` |

Code older than v2 writing into a v2 store would add `fts_text` rows without
`fts_map` entries (duplicate text on re-index); do not mix versions on one store.

## Transactions (atomic folds)

`fold_bundle` writes a fold as **one transaction** (`with conn:`): nodes,
edges, code events, the FTS index, `gathered_windows` and `clone_sha` commit
together, and any exception rolls all of them back, so readers never see a
half-folded window. The batch writers (`upsert_nodes`, `upsert_edges`,
`add_code_events`, `index_text(s)`, `set_meta`, `record_window`,
`set_clone_sha`) take `commit=True` by default and `commit=False` to join the
caller's open transaction. A caller's own uncommitted writes on the same
connection are committed (or rolled back) with the fold.

## Writer (gather --store)

`gather --store PATH` (required) folds its in-memory assembled bundle into the
store via `gather.fold_bundle(conn, bundle)` — the store is the **sole output**
(no bundle file is written), idempotent by identity (re-folding an overlapping
window mutates nothing already correct, so roll-up = a wider window re-fold and
resume = a re-fold against the pinned `clone_sha`). P6
writes: `social` (PRs/issues; comments/reviews stay embedded in the parent's
`data` blob), `code` (commits) + the file-level `code_events` ledger,
`structure` (milestones/releases/areas), and the **spine** edges `closes`,
`cross_ref`, and `part_of`. It also persists the per-repo **singleton facts**
`workflow_stats`, `code_graph`, `code_owners`, and `label_taxonomy` — each as a
whole-value `structure` node under a well-known local id (`workflowstats`,
`codegraph`, `codeowners`, `labeltaxonomy`) with NULL `ts` (excluded from window
scans), identity-keyed and idempotent. A singleton node is written only when its
source value is present and non-empty, so a reader never reconstructs a
fabricated empty key. Node `data` is JSON-encoded with sorted keys, so
`code_owners` is an ordered rule **list** (not a pattern-keyed dict): list order
survives the round trip, and CODEOWNERS precedence depends on it.

**Slice 7b-1 (step 2) additionally persists the artifact substrate** (derived on
the write path via the shared `derive.py` leaf module — `derive.build_artifacts`
/ `derive.link_symbol_identity`):

- **Artifact `code` nodes.** Each lifecycle-tracked artifact upserts as a `code`
  node keyed on its own id form — local id `art:<path>` for file artifacts
  (readme/doc/example) and `<path>#<lang>:<subkind>:<name>` for symbol/comment
  artifacts — with `ts` = the artifact's last lifecycle-event date and `data` =
  the full artifact record. Additive to (and distinct from) the commit `code`
  nodes, which keep their bare-`<sha>` local id. Idempotent by id.
- **`symbol_events` ledger.** The symbol-granular lifecycle
  (`gather.parse_symbol_events`) is persisted into the `code_events` table keyed
  by the SYMBOL artifact id `<path>#<lang>:<subkind>:<name>`, carrying the rich
  `before`/`after` fields (file-level rows leave them NULL) and the MAPPED `event`
  (`derive._SYMBOL_CHANGE_TO_EVENT[change]`). A symbol artifact's `lifecycle[]` is
  `get_code_events(<symbol artifact id>)` in date order. The raw `symbol_events`
  array round-trips losslessly: `extract` reconstructs it from these rows (see the
  reader contract below), so a self-sourced bundle re-derives the symbol/comment
  artifacts exactly — which is what `validate.no_drift` requires.
- **Symbol-move edges.** Confident window-wide symbol moves
  (`derive.match_symbol_moves`) upsert `replaced_by` (src→dst) and
  `identity_from` (dst→src) artifact→artifact edges carrying
  `move_confidence`/`move_basis` in the edge `data`. Idempotent by (src,dst,type).

**Slice 7b-1 (step 3) additionally persists people + the contribution / non-spine
edges** (all derived on the write path via the shared `derive.py` leaf —
`derive.enumerate_participants` / `derive.attribute_people_areas` /
`derive.area_index` / `derive._commit_areas`):

- **Person `structure` nodes (project-scoped) = ALL participants.** `people` is
  the FULL participant set: EVERY login that carries any contribution edge gets a
  person node — `pr.author`, `pr.merged_by`, each `pr.reviewers`, `issue.author`,
  commit `author`, and comment / review-comment authors. The single source of
  truth is `derive.enumerate_participants(bundle)` (the **anti-drift enumerator**):
  the write path (`gather.fold_bundle`) AND the auditor (`validate.no_drift`) both
  call it, so they can never disagree on "who". Contributors (commit authors +
  reviewers of PRs with mapped commits) carry the `{modules, areas}` that
  `attribute_people_areas` derives; pure participants (comment-only, issue
  reporters, out-of-window PR authors, reviewers of mapped-commit-less PRs) get
  empty `modules`/`areas`. Every login is **bot-tagged** — `is_bot = True` when it
  matches `*[bot]`, `github-actions`, `microsoft-github-policy-service`,
  `*-organizer`, or `copilot-*` (`derive.is_bot_login`); bots are TAGGED, never
  dropped (dropping = losing data). Each upserts as a `structure` node id
  `qualify_person(project, login)` (`{project}#person-{login}`), NULL `ts`,
  `data` = `{login, modules, areas, is_bot[, login_resolved]}`.
  **Identity (#78):** the key is the GitHub **login** for every participant,
  commit authors included — gather resolves a commit's git author to its login
  (commits-list API `author.login` by SHA, then a `users.noreply.github.com`
  email; see BUNDLE.md `commits`), so a PR author and the same person's commits
  share ONE person node. A commit author gather could not resolve keeps its git
  name as the key (suffixed `" (git)"` when the name is login-shaped, so it can
  never equal a login) and its node carries `login_resolved: false`; the key is
  absent on every other person. `is_bot` also recognises git-name bot forms
  (`GitHub Actions`, `Dependabot`, `Renovate Bot`, a `[bot]` name, with or
  without the suffix). The commit `code` node's data keeps both `author` (the
  person key) and `author_name` (the git name); the author email is never
  stored. **Person ids changed with #78:** a store folded before it holds commit
  authors as `person-<git name>` nodes; folding a post-#78 bundle into it adds
  the login-keyed nodes beside them (the old ones are not retracted), so
  **re-gather such a store into a fresh file** rather than folding on top. The `repo` column is the project
  sentinel `"*"` (people aggregate across a project's repos), so re-folding the
  same login from another repo upserts the identical id — one node, never a
  cross-repo duplicate — and the sentinel keeps the row out of any single repo's
  `repo_nodes` view. Idempotent by id.

  > Before this fix, person-node creation was driven by `attribute_people_areas`
  > alone (commit authors + reviewers of mapped-commit PRs), while contribution
  > edges were written for EVERY participant — so a real AVM gather had 222 logins
  > with contribution edges (commented/reported/authored/reviewed) but NO person
  > node, leaving thousands of dangling person→node edges. The shared enumerator
  > closes that gap.
- **Contribution edges (person→node).** From the RAW records, idempotent by
  (src,dst,type), skipping absent logins: `authored` (person→pr from `pr.author`,
  person→commit from `commit.author`), `merged` (person→pr from `pr.merged_by`),
  `reviewed` (person→pr from `pr.reviewers`), `reported` (person→issue from
  `issue.author`), `commented` (person→pr/issue from `comments_list` +
  `review_comments` authors). `reacted` is **not** written: reactions are stored
  as aggregate counts (`summarize_reactions`), with no per-reactor login to key on.
- **`owns` (person→area)** from `code_owners` — each owner returned by
  `derive.area_owners` for the area: the owners of the **last** CODEOWNERS rule
  matching the area directory (`<area>/`; graphify community areas: union over
  their files), with GitHub CODEOWNERS pattern semantics (see BUNDLE.md
  `code_owners`). Rules are not unioned; an owner-less last match emits no edge.
  `stale_owned` uses the same matcher. **`touches` (commit→area)** from
  `derive._commit_areas` (a commit's files → the areas they land in). **`depends_on`
  (area→area)** from `code_graph["edges"]`, carrying `{version,transitive,…}` in
  the edge `data`. **`in_milestone` (social→structure)** from a PR/issue's
  `milestone` title → the milestone node (keyed on number when present).

The contribution / `owns` / `touches` / `depends_on` / `in_milestone` edges
never leak into `extract`: every one is non-spine, so `traverse_spine` never
follows it, and extract reads none of them.

**Slice 7b-2 — reader contract: `extract` materializes `artifacts` + `people`
from the stored nodes; `link.enrich` shrank.** Because the write path now derives
those two projections identically to how `enrich` used to (store-derived ==
link-derived, locked by `test_characterization.py`), `extract` no longer emits a
RAW-only view — it additionally reconstructs:

- **`artifacts`** by reading the artifact `code` nodes (the non-commit ones —
  local id `art:<path>` / `<path>#…`), keyed by that local id (== `artifact_id`).
  extract restores build_artifacts' code_events insertion order so order-sensitive
  consumers (`build_timeline`'s same-`(ts,url)` tie-break) reproduce byte-for-byte.
- **`people`** by reading the project-scoped person `structure` nodes (repo
  sentinel `"*"`, local id `person-<login>`), keyed by login (the redundant stored
  `login` field is dropped). Because fold now persists ALL participants, this
  naturally returns the full set (each record carries `modules`/`areas`/`is_bot`).
- **`symbol_events`** by splitting the shared `code_events` ledger: the
  SYMBOL-keyed rows (local id `<path>#<lang>:<subkind>:<name>` — a SECOND `#` past
  the repo prefix, with `before`/`after`) reconstruct one symbol_event dict each
  (file-level `<path>` rows stay in the raw `code_events` array and are never
  double-counted). The id parse strips the `{project}/{repo}#` prefix, splits once
  on `#` → `path` + `lang:subkind:name`, then `split(":", 2)` → `lang`/`subkind`/
  `name` (name kept whole). The stored `event` is reversed back to a `change` via
  the inverse of `derive._SYMBOL_CHANGE_TO_EVENT`; since `build_artifacts` only
  uses `change` to recompute that same `event`, the artifact lifecycle round-trips
  exactly. The key is always emitted (empty `[]` when the repo has no symbol rows).
  This closes the self-sourced `no_drift` gap the live AVM audit found (86 symbol/
  comment artifacts were "stored but not derivable" because `extract` dropped
  `symbol_events`); it also un-forces the previously always-empty `symbol_moves`.

extract READS these — it does NOT re-derive via `build_artifacts` /
`attribute_people_areas`. Correspondingly `link.enrich` removed its
`build_artifacts` + `attribute_people_areas` calls and now CONSUMES
`bundle["artifacts"]`/`bundle["people"]` (with defensive
`setdefault("artifacts", {})` / `setdefault("people", {})` so a raw bundle fed
straight to enrich never KeyErrors and never silently recomputes them). enrich
still owns the window-only projections (trains/buckets/timeline/feature_deltas/
code-area attribution/modules/forecast/symbol-identity). The pure derivations
`build_artifacts`/`attribute_people_areas` live in `derive.py`; `link` no longer
re-exports them — call `derive.*` directly. (`link` re-exports only the
window-projection helpers it still uses: `attribute_code_areas`/`build_modules`/
`link_symbol_identity`/`ref`, plus `resolve_commit_pr`/`attach_commit_prs`.)

`blocks` (issue→issue) IS now on the write path: `normalize_issue` parses directed
dependency phrasing from an issue's title+body (`parse_blocks_refs` — "blocks #N" /
"blocking #N" → outbound; "blocked by #N" / "depends on #N" → inbound; same-repo
bare `#N`), and fold emits a `blocks` edge normalized to **blocker → blocked**. To
keep referential integrity, an edge is emitted ONLY when the target #N is itself a
gathered issue (a ref to an out-of-window issue is dropped, never left dangling).
`blocks` is a NON-spine relation (not in `SPINE_EDGE_TYPES`), so `traverse_spine`
never follows it and decision-train composition is unchanged; its issue→issue shape
already matched `validate.check_schema_conformance`'s allowlist.

Still NOT on the write path: `in_iteration` (social→sprint) — needs **Projects v2**
acquisition (the `read:project` scope), which gather does not yet pull. The
`fts_text` FTS5 index IS populated on fold (see *Text index* above). See
`docs/superpowers/specs/2026-06-04-activity-phase7-substrate.md`.

## Backfill on a traversal miss (slice 7c)

A reader (`extract`) traversing a decision train over the spine edges can hit a
`missing` id — a spine edge pointing at a node never gathered (e.g. a windowed PR
`closes` an issue opened/closed *before* the window). `gather.backfill(conn, id,
fetch=…)` closes that gap on demand: it fetches THAT ONE node + its cheap
immediate spine edges and upserts them by identity. It is the **only** network
call outside the main Acquire pass, so it lives in `gather.py` (`extract` never
fetches itself — it calls in via an injected `backfill` callable).

- **Idempotent / no-op when present.** `backfill` first checks `get_node(id)`;
  if the node already exists it returns `{"fetched": False, …}` WITHOUT fetching.
  The fetched node is **durable** (upserted), so it persists for the next window
  and a re-run is a no-op — the backfilled node appears exactly once.
- **Injectable network seam.** The actual fetch goes through a `fetch(kind,
  local, qid)` callable (`None` == unfetchable; `{"node": raw, "edges":
  [(dst_local, edge_type), …]}` == fetched). `classify_id` derives `kind`
  (social `pr-`/`issue-`/`comment-`, code bare-`<sha>`, else structure) from the
  local id form. Production wires `gather.make_backfill_fetcher(token,
  clone_dir)` (REST `normalize_pr`/`normalize_issue` for social — re-deriving the
  same `closes` spine edges fold does; bounded `git fetch --depth 1 <sha>` +
  one-commit log for code; structure not backfilled on demand). The test suite
  passes a **fixture-backed fake** — NO network. Only spine edge types from the
  seam are honored (backfill closes a traversal gap, it does not re-derive the
  non-spine substrate).
- **Reader wiring + budget.** `extract.extract(…, backfill=None,
  backfill_budget=50)`: when `backfill` is injected and traversal yields
  `missing` ids, extract calls it per id under the per-window budget ceiling, then
  re-traverses so a backfilled node that itself references further missing spine
  nodes is resolved within the **remaining** budget. Once the budget is exhausted
  it WARNS and stops (never unbounded). When `backfill is None` (the default, and
  what every existing test/characterization uses) the path is byte-identical to
  before — `missing` ids are only warned about.
- **Context-only (documented gap).** A backfilled node is pulled into the bundle
  as out-of-window **context** (it was reached via the spine but is outside the
  range query), so it does NOT count toward in-window activity. The read pipeline
  that wires `backfill=gather.backfill` in production is not landed yet (extract
  is still read only by tests today); `make_backfill_fetcher`'s live REST/git
  paths are therefore only exercisable against a real repo (relevant to the
  upcoming trust gate), not by the offline suite.

## Train completion + dead-ref memory

The single-node `backfill` bridge is promoted into a shared
**completion orchestrator** (`complete.py`) that closes a train **transitively**
along the causal spine, **bounded by the query's time window** (level-0
directly-referenced anchors always filled; transitive expansion stops at the
window edge; closure when no window). Both readers call it — `extract` (refactored
off its inline loop) and `spotlight` (its four queries gain optional `--complete`).
`gather.backfill` stays the single-node primitive `complete.py` orchestrates
through the **injected** fetch seam, so `complete.py` does no I/O and `spotlight`
stays reader-only.

Every train then carries an **honest edge contract**: `complete: bool` and
`gaps: [{id, reason}]` with reason `not_gathered` (offline, no fetch attempted) /
`outside_window` (beyond the query window, not chased) / `unreachable` (a fetch
was attempted and failed) / `budget` (the per-query fetch ceiling was hit). A
reference the seam reports **definitively absent** (a 404 phantom — e.g. a
`Fixes #123` PR-template placeholder) is **pruned**, never a gap, and recorded in
the `dead_refs` tombstone so no future query re-chases it.

**`dead_refs` table** — `(id PK, project, reason, first_seen)`. Written *only* by
`gather.backfill` when the seam returns the `ABSENT` outcome (preserving "only
gather writes"); read via `is_dead_ref`/`get_dead_refs`. `traverse_spine(…,
skip_dead=True)` drops known-dead ids from `missing` so even the report path stops
re-surfacing pruned phantoms. The fetch seam grows a third outcome to make this
possible: `fetched payload` | `ABSENT` (definitive 404 → prune + record dead) |
`None` (transient/unreachable → an `unreachable` gap). The tombstone is additive,
ignored by the trust gate, and `first_seen` is excluded from determinism asserts
(like `fetched_at`). See
`docs/superpowers/specs/2026-06-04-activity-phase8d-completion.md`.

## Multi-repo project (the constellation)

A **project can span several repos** folded under one logical name (the lead case
is Azure Verified Modules — Terraform, where each module is its own repo). The
producer takes a **manifest** instead of `--owner/--repo`:

```bash
python3 gather.py --manifest workspace/manifest.json --store workspace/journey.db
```

```json
{ "project": "avm-tf-storage",
  "window": { "from": "2026-03-01", "to": "2026-03-31" },
  "repos": [
    { "owner": "Azure", "repo": "terraform-azurerm-avm-res-storage-storageaccount",
      "registry": "Azure/avm-res-storage-storageaccount/azurerm" },
    { "owner": "Azure", "repo": "terraform-azurerm-avm-res-keyvault-vault" } ] }
```

`manifest.load_manifest` validates the file before the store is touched and raises
`ValueError` (`manifest: …`) on: a non-string or empty field; a `project` containing
`/` or `#`; a window bound that is not a real `YYYY-MM-DD` date, or `from` after `to`;
an `owner` that is not a GitHub user/org name (letters, digits, `-` and `_`, not
starting with `-`, ≤ 39 chars); a `repo` outside GitHub's alphabet (letters, digits,
`-`, `_`, `.`; ≤ 100 chars; not `.`/`..`); two members naming the same repo
**case-insensitively** (`o/a` and `O/A`); a `registry` that is present but not a
non-empty string, or declared by two members (case-insensitively); a
`ticket_prefixes` that is present but not a list of distinct, non-empty ASCII
letter/digit strings. `gather.py` applies the same owner/repo/date checks to
`--owner/--repo/--from/--to`.

The optional top-level `"ticket_prefixes": ["ABC", "PLAT"]` is read only by
`digest.py --manifest` (gather ignores it): it turns on ticket clustering into
`related_work` with the pattern `\b(?:ABC|PLAT)-\d+\b`. Without it (or
`digest.py --ticket-pattern`) `related_work` is `[]`.

gather loops the members, running the existing acquire→fold path once per member
into the **same store** under `project = manifest.project`, `repo = "{owner}/{repo}"`.
The slash in `repo` is benign — every join is on the full string, every split is
first-`/` (scope) or last-`#` (local). The single-repo `--owner/--repo` path is
byte-unchanged (`project = owner`, `repo = <name>`).

- **Cross-repo spine edges.** A PR body's **qualified** ref (`owner/repo#N`, full
  `github.com/.../issues|pull/N` URL) or a repo-aware timeline cross-ref to a
  **member** repo emits a `closes`/`cross_ref` spine edge across the repo boundary
  (`traverse_spine` walks it unchanged); a ref to a **non-member** repo is recorded
  on the node's `external_refs` (honest, not a fetchable gap).
- **Cross-repo `depends_on` (Terraform).** `build_terraform_edges` keeps a registry
  source as `to=None, ref=<src>`; `fold_bundle` resolves it to the member that
  publishes that module — **exact** (a member whose manifest `registry` equals the
  source) then the **HashiCorp naming convention**
  (`namespace/name/provider` → `{namespace}/terraform-{provider}-{name}`) — and
  emits an area→area `depends_on` to that member's root area
  (`#area-main.tf`), `data.cross_repo = True`. A registry source whose first segment
  is a host (a dot, e.g. `github.com/org/repo`) is VCS shorthand, never resolved.
  Resolution only runs in a multi-repo fold (`members` + `registry_by_slug` present);
  single-repo folds emit intra-repo edges only.
- **Repo slugs and registry sources compare case-insensitively** (GitHub names and
  registry addresses are). Ids keep the **manifest's spelling**
  (`{project}/{owner}/{repo}#…`); only comparison/lookup is normalised, mapping any
  spelling to that canonical slug (`manifest.slug_index`), so two spellings never
  make two id spaces and no store migration is needed. This covers qualified refs,
  timeline cross-refs (a same-repo ref in another case is not cross-repo), the
  Projects v2 board lookup, and registry resolution, where **both** sides go through
  `parse_registry_source` (host and `//submodule` stripped) before an exact or
  convention match — `registry.terraform.io/azure/foo/azurerm//modules/bar` matches a
  member declared with `registry: "Azure/foo/azurerm"`. Readers (`spotlight
  dependents`, `digest --repo`, `view --repo`) accept any casing of a stored repo and
  resolve it to the stored spelling (`graphstore.resolve_repo`). The **project**
  name stays case-sensitive.

**Terraform extraction, corrected for real modules.** A Terraform **root module is
one area** — repo-root `*.tf` files (`main.tf`/`variables.tf`/`main.<topic>.tf`/…)
collapse to a single `main.tf` area (not one per file), and edge extraction reads
**every `*.tf` in the module directory** (a module's `module` blocks span its files;
`terraform graph` already spans the whole dir). A local source landing on the repo
root (e.g. an example's `source = "../.."`) resolves to that `main.tf` area; a source
escaping the repo is unresolved, never a phantom dangling area. **`examples/` and
`tests/` subtrees are scaffolding** (they reference the module-under-test and test
fixtures, often other registry modules, via CI-manipulated paths) — they are
**skipped at extraction and excluded at fold**, so they never become bogus intra-
or cross-repo dependencies; that includes scaffold files nested inside a module
(`modules/sub/examples/x/main.tf`), which are never read or built as `modules/sub`.
The module dir is resolved on disk in the clone, whichever of its files changed in
the window (see BUNDLE.md, *Entrypoint*). `terraform graph` runs through the same parallel pool
as the bicep build; with a shared `TF_PLUGIN_CACHE_DIR` a one-shot serial provider
warm-up precedes the parallel inits (terraform's plugin cache is not write-safe
under concurrency). The IaC build knobs are env-overridable for heavy runs:
`ACTIVITY_IAC_BUILD_TIMEOUT` (default 300s), `ACTIVITY_IAC_MAX_WORKERS` (8),
`ACTIVITY_IAC_RETRIES` (1).

A cross-repo `depends_on` resolves to the producer member's module-root area
(`area-main.tf`). That root is only created by the producer's own fold when its
`main.tf` is an in-window code area — usually it is not. The consumer fold
therefore **ensures the target node exists** (a minimal `structure` stand-in,
create-if-absent so a producer's real area always wins), keeping the edge from
dangling. The node is an edge anchor only: `extract` rebuilds `code_graph` from
the `codegraph` singleton, never from `area-*` nodes, so it is invisible to every
member bundle. The shallow-clone margin is `ACTIVITY_CLONE_MARGIN_DAYS`
(default 14); widen it when `meta.boundary_dropped_commits` is non-empty.

The DOT-based extraction (`terraform graph`) only resolves edges for module areas
the directory provider sees — i.e. in-window-CHANGED dirs — so the dependency graph
would otherwise track churn rather than structure. On **multi-repo (manifest)**
gathers, `scan_structural_terraform_areas` additionally parses every module's
`source` references across the whole tracked tree (static, no `terraform init`) and
`merge_structural_areas` unions them into `code_graph` (existing areas keep their
DOT edges + gain missing structural ones; untouched module dirs are appended). The
blast-radius graph is then the repo's full module structure regardless of in-window
activity. Gated to manifest folds so single-repo bundles stay byte-stable; `git
ls-files` lists only tracked `.tf`, excluding vendored `.terraform/` modules.

**People aggregate across members.** Person nodes are project-scoped (`repo="*"`).
Each per-member fold **unions** a person's `areas`/`modules` (and ORs `is_bot`)
with the already-stored node — a plain upsert would overwrite, so a member where a
contributor has no attribution would erase another's. The aggregate is fold-order
independent and idempotent (re-folding a member unions a subset → no change).

**Readers + validate aggregate the member set.** `digest.py` is the project
aggregation layer above `extract` (per-member extract+enrich, then merge:
spine-linked trains stitched across members; trains sharing only an internal ticket
clustered into `related_work` — opt-in, only with the manifest's `ticket_prefixes`
(`digest.py --manifest`) or `--ticket-pattern`, else `[]`; Shipped/people/modules merged; cross-repo
`module_edges` for the project-wide blast-radius graph). `spotlight dependents
<owner/repo>` answers the reverse-dependency (blast-radius) question. `validate.py
--project <name>` runs the per-member checks **plus** a project-wide
`no_drift_people` check that re-derives people across **all** members and unions
them (the read-side mirror of the fold) — people are project-scoped, so they are
audited once project-wide, not per member.

## Roll-up / resume (slice 7c)

A multi-window "wider view" is a **single wider `range_query`** over the union of
folded windows — a `WHERE ts BETWEEN …`, NOT a multi-bundle merge. Folding two
adjacent windows (March, April) then range-querying `[March-start, April-end]`
returns the union of their in-window nodes; each narrow window still returns only
its own. Overlapping re-folds are idempotent (identity-keyed nodes/edges, set
semantics on the ledger, deduped `gathered_windows`), so re-extracting an
overlapping window is byte-stable. Resume/roll-up read structure from the latest
`clone_sha` (`set_clone_sha`/`get_clone_sha`) and the `gathered_windows` ledger
(`record_window`/`get_windows`): the structure pin is the newest fold's
`clone_sha`, a `WHERE`, not a merge.

## Trust gate (validate.py)

`python3 validate.py STORE.db` audits the store for trustworthiness and exits
non-zero on any ERROR (CI-gateable). It is **self-contained on a store**: the two
real-data checks — `no_drift` (stored people/artifacts == freshly re-derived) and
`idempotency` (a re-fold changes no counts) — need a raw bundle to re-derive
against, and when none is passed they **self-source it from the store** via
`extract` over the store's full activity window. A `--bundle` argument is an
optional cross-check, never required. The auditor never mutates the store and
survives a corrupt one (a re-derive/re-fold that raises becomes a failed ERROR
check, not a crash). This is what makes the store a stand-alone deliverable.

## Determinism

`data` blobs serialize with `sort_keys=True`; `range_query` orders by
`(ts, id)` and applies the granularity-safe window rule above (`ts_in_window`),
so the same window gives the same node set whether its bounds are written as
dates or as start/end-of-day datetimes. Given fixed inputs the store is byte-stable (modulo `fetched_at`).
