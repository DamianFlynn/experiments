# STORE.md — journey-graph schema

`graphstore.py` is a stdlib-only SQLite property graph: the durable,
identity-keyed substrate that `gather` writes and `extract`/`spotlight` read.
Readers, writers, and downstream renderer authors rely on this file as the
contract. `graphstore.py` owns the schema and the core read/write API; callers
use that API (a couple of readers also run their own read-only queries).

**Readers open the store read-only (#107).** `view.py`, `digest.py`,
`spotlight.py` and `ci_spotlight_integration.py` open it with
`graphstore.open_store_readonly` (never creates a file; any write raises), so
the store's bytes are unchanged after a read. The one exception is
`spotlight.py --complete` with a `GITHUB_TOKEN`, which writes the anchors it
backfills and opens the store read-write.

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
  shape, so it is skipped. An id starting `art:` is never a symbol.
- **Stored nodes are classified by class and record, not id shape (#101).** The
  shape rule above is the fallback for a **dangling** id only (backfill's
  `gather.classify_id`, an edge endpoint that is not stored). A stored `code`
  node is a commit when its record carries `sha`, else an artifact
  (`graphstore.is_artifact_node`); a symbol/comment artifact is one whose record
  `kind` is `symbol`/`comment` (`is_symbol_node`). A stored non-code node is
  never an artifact, so a milestone titled `v1#a:b:c` (`milestone-v1#a:b:c`) is
  a milestone to `validate`'s edge-endpoint check and to spotlight.
- **`code_events` ledger rows.** File-level rows are keyed by the bare `<path>`
  and have no node of their own, so the row's commit decides: a row whose
  commit's stored `files` list names its exact local id is a file row, whatever
  its shape (`notes#a:b:c.md`); otherwise a row is a symbol row iff its local id
  has the symbol tail (`graphstore.ledger_row_is_symbol`, used by `extract`'s
  ledger readers — hence by fold's artifact rebuild and `no_drift` — and by
  spotlight `person`).
- **Residual ambiguity.** A file row with that tail shape whose commit is not
  stored or has an empty file list (a shallow-boundary commit, #68) is still
  read as a symbol row. Git paths with `:` are rare (and invalid on Windows).

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
unions, never appends. History edges are only ever added; the current-state
edges (`blocks`, `in_milestone`, `in_iteration`, `depends_on`, `owns`) are kept
equal to what the stored inputs imply and stale ones are deleted (see
*Current-state edges* below, #95). Edge types:

`closes` (pr→issue), `part_of` (commit/review/event→pr/issue), `cross_ref`
(pr→issue/pr), `duplicate_of`/`spun_off` (issue→issue), `touches` (commit→area),
`authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted` (person→node),
`owns` (person→area), `depends_on` (area→area, carries version/transitive in
`data`), `replaced_by`/`identity_from` (artifact→artifact), `blocks`
(issue→issue), `in_milestone`/`in_iteration` (social→structure). These are
the endpoint rules `validate` enforces (`schema_conformance`, #89); any other
edge type is an error. Both ends of an edge are in one project, and `touches`,
`part_of`, `blocks`, `in_milestone`, `in_iteration`, `replaced_by`,
`identity_from`, `spun_off` and `duplicate_of` stay inside one repo.

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
`/pull/` URL is a `cross_ref`, never a close. A timeline `cross-referenced` /
`connected` event joins `crossref_issues` only when its source issue is in this repo
(#109; another repo's issue is a cross-repo ref only), and a `connected` link later
`disconnected` does not. Keywords are matched in code blocks
too (no Markdown parsing). GitHub's own list (#105) is merged in at gather time: the
issues `closingIssuesReferences` reports (keywords AND manual Development-sidebar links)
join the PR's `closes` (same repo; removed from `crossref_issues`) and, when anything is
new, are stored on the PR record as `closing_issues` (`[{owner, repo, number}]`), whose
other-repo entries the multi-repo fold turns into cross-repo `closes` edges. Both fields
are on the stored record, so the trust check re-derives these edges store-only. With
GraphQL unavailable gather keeps the keywords alone (`meta.closing_refs_source:
keywords`). `part_of` uses the commit subject's **trailing**
`(#N)` first (so `Revert "x (#4012)" (#4020)` → PR 4020), then `Merge pull
request #N`, then the last `(#N)`.

## Code-event ledger (`code_events` table)

One row per artifact lifecycle event: `(artifact_id, event, commit_sha,
author, date, hunk, ref, before, after, detail)`, PK `(artifact, commit,
event)` → set semantics (re-seeing a commit is a no-op). The ledger keeps every
folded window, and it is the source of truth for the artifact nodes: an
artifact's `lifecycle[]` is all of its ledger rows in commit order: by the commit's UTC
committer time (`committed_at` on the commit node) when it is stored, else by the
event's `date`, the commit's UTC committer day (the committer's local day for windows
gathered before #109; ties keep ledger order). File-level rows are
keyed by the bare `<path>` (not the `art:<path>` node id) and keep git's change verb
(`add`/`modify`/`delete`/`rename`/`copy`); symbol rows carry the mapped event
(`add`/`change`/`remove`). A reader maps `art:<path>` to `<path>` before reading the
ledger and treats both `delete` and `remove` as the artifact's end (`spotlight symbol`).

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
`clone_sha:{project}/{repo}` (the commit the structure of the repo's latest fold was
read at — its window's end, issue #107 — provenance only; `set_clone_sha`/`get_clone_sha`);
`ref_date:{project}/{repo}:{from}:{to}` (the reference date a gathered window was
folded with: gather `--ref-date`, else the window's `to`; a re-fold of the same
window replaces it — `set_ref_date`/`get_ref_date`). `extract` stamps it into the
view's `meta.ref_date` when the view's `--from/--to` equal that window exactly
(else the view's `to`; a view/digest `--ref-date` overrides both), and stamps
`meta.period = {from, to}` from the view's own window (issue #75). Stores folded
before #75 have no `ref_date:` keys; their views fall back to `to`.
`branches:{project}/{repo}:{from}:{to}` (issue #84): JSON
`{"base_branch": str|null, "branches": [str]}`, the mainline a gathered window was
analyzed against (gather `--branches` / manifest member `branch`, else the repo default
branch; `null` when gather could not determine it); a re-fold of the same window replaces
it — `set_branches`/`get_branches`. Written only when the folded bundle's `meta` has a
`base_branch` key (every gather since #84). `extract` stamps `meta.base_branch` /
`meta.branches` from the exact window, else from the gathered windows of that repo the
view overlaps when their non-null values agree, else `null` / `[]` with a stderr warning
(when a merged PR is in the view). Stores folded before #84 have no `branches:` keys, so
their views report the base branch unknown and count every merged PR as shipped, as
before; re-gather to get stacked / release-branch PRs told apart.

`clone:{project}/{repo}:{from}:{to}` (issue #107): JSON `{"clone_mode": str|null,
"boundary_dropped_commits": [sha]}` (sorted), how a gathered window's clone was made
and the in-window commits whose diffs its shallow boundary dropped; a re-fold of the
same window replaces it — `set_clone_info`/`get_clone_info`. Written in the fold
transaction when the bundle's `meta` has `from`, `to` and either key (every gather).
`extract` stamps `meta.clone_mode` for a view of exactly that window and
`meta.boundary_dropped_commits` as the union over the gathered windows wholly inside
the view (each omitted when absent or empty). Stores folded before #107 have no
`clone:` keys and their views carry neither.

`facts:{project}/{repo}:{from}:{to}` (issue #94): JSON `{"clone_sha": str|null,
"facts": {key: sha256}, "ci_collected": bool}` (`ci_collected` is false when the
window was gathered with `--no-workflows`: no CI facts, as distinct from zero runs;
an entry without the key counts as collected), the per-repo facts a gathered window computed —
`workflow_stats` (the CI runs created in that window), `code_graph` (the areas of
the files that window changed, with their dependency edges), `code_owners` and
`label_taxonomy` (read from that window's checkout) — each named by the SHA-256 of
its JSON text (encoded as node `data` is: sorted keys), and the clone sha the
window was gathered against. A key is present only when the bundle's value is
present and non-empty (the latest-wins node rule). Each value is stored once per
repo under `fact:{project}/{repo}:{sha256}` (its JSON text), so windows with
identical facts (an unchanged CODEOWNERS file, the same taxonomy) share one copy.
Written in the fold transaction whenever the bundle's `meta` has `from` and `to`;
a re-fold of the same window replaces its entry and deletes any `fact:` value no
window of the repo references any more — `set_window_facts`/`get_window_facts`.
The `code_graph` is window-scoped (the directory provider only sees the window's
changed paths), so it is keyed by window, not by clone sha: two windows gathered
from the same checkout can hold different graphs. Stores folded before #94 have no
`facts:` keys; their views read the latest-wins singleton nodes as before. No
schema change. How a view picks its facts: "Roll-up / resume" below. Since #95
the `depends_on` / `owns` edges are replayed from these entries (*Current-state
edges* below); a code graph from the whole-tree scan carries
`structural_scan: true`.

`current_state:{project}/{repo}` and `retractions:{project}/{repo}` (issue #95):
how the repo's last fold kept its current-state edges, and the ledger of what
folds retracted — see *Current-state edges* below.

`member_status` (issue #77; written only by a manifest gather): a JSON list of
`{project, repo, from, to, status[, error]}`, one entry per member and window,
sorted on `(project, repo, from, to)` — `set_member_status` / `get_member_status`
/ `incomplete_members`. `status` is the outcome of the member's LATEST attempt for
that window: `ok`, `failed` (with `error`, a one-line reason of at most 500
characters, e.g. the HTTP error message) or `not_attempted`. `gather --manifest`
first records every member it is about to attempt as `not_attempted`, then each as
`ok` (after its fold committed) or `failed` when its turn ends, so a run that is
killed mid-way leaves the rest `not_attempted`. The member's own fold is still one
transaction: a failed member leaves no partial fold. `validate.py` fails a member
that is not `ok` for any recorded window (`target`, and the project-wide
`member_status` check); `digest.py` / `view.py` warn on stderr for a window that
overlaps the one they read. `gather --manifest --resume` re-attempts only the
members that are not `ok` for the manifest's window. Single-repo stores have no
`member_status` key.

**Membership.** `project_repos` lists the repos with stored nodes, except a repo
whose ONLY nodes are `depends_on` stand-ins (`data.synthesized ==
"depends_on_target"`, `graphstore.STANDIN_MARK`) and for which no window was
gathered: that is a phantom another member's fold created (typically for a member
whose own gather failed), not a member. A quiet member with a gathered window
stays listed even if it holds only stand-ins.

### Schema version and migration

`graphstore.SCHEMA_VERSION` is **2**. `init_schema` creates a new store at the
current version and migrates an older one **in place**; every step is additive
and idempotent, and the stored version is raised, never lowered.

This versions the STORE only and lives only in the store's meta. Views carry a
separate bundle/view contract version, `meta.schema_version` =
`extract.VIEW_CONTRACT_VERSION` (BUNDLE.md, #107); the two are bumped
independently.

| Version | Change | Migration from the previous version |
|---|---|---|
| 1 | initial schema | — |
| 2 | `fts_map` side table; `idx_edges_type_src` index (issue #55) | create the index; create `fts_map`, collapse any duplicate `fts_text` rows per node (newest kept) and back-fill the map from `fts_text` |

Code older than v2 writing into a v2 store would add `fts_text` rows without
`fts_map` entries (duplicate text on re-index); do not mix versions on one store.

## Transactions (atomic folds)

`fold_bundle` writes a fold as **one transaction** (`with conn:`): nodes,
edges (including the retraction of stale current-state edges and orphaned
stand-ins, #95), code events, the FTS index, `gathered_windows`, `ref_date`,
`branches`, `clone_sha`, the window facts and the `current_state:` /
`retractions:` meta commit together, and any exception rolls all of them back, so readers never see a
half-folded window. The batch writers (`upsert_nodes`, `upsert_edges`,
`add_code_events`, `index_text(s)`, `set_meta`, `record_window`,
`set_clone_sha`) take `commit=True` by default and `commit=False` to join the
caller's open transaction. A caller's own uncommitted writes on the same
connection are committed (or rolled back) with the fold. A manifest member's
`member_status` is written in its own transaction right after its fold: a crash
between the two leaves the member `not_attempted` (re-folding it is idempotent).

## Writer (gather --store)

`gather --store PATH` (required) folds its in-memory assembled bundle into the
store via `gather.fold_bundle(conn, bundle)` — the store is the **sole output**
(no bundle file is written), idempotent by identity (re-folding an overlapping
window mutates nothing already correct, so a refresh is a re-gather of the same window (its structure is read at the
window's end, issue #107);
a roll-up is a wider view over the folded windows, or a wider gather). P6
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
survives the round trip, and CODEOWNERS precedence depends on it. These nodes are
latest-wins (the last fold's values); since issue #94 the same fold also records
the window's own values under `facts:` (see "Meta"), and a view reads those. The
nodes remain for stores folded before #94 and for code that reads the latest
structure.

**Slice 7b-1 (step 2) additionally persists the artifact substrate** (derived on
the write path via the shared `derive.py` leaf module — `derive.build_artifacts`
/ `derive.link_symbol_identity`):

- **Artifact `code` nodes.** Each lifecycle-tracked artifact upserts as a `code`
  node keyed on its own id form — local id `art:<path>` for file artifacts
  (readme/doc/example) and `<path>#<lang>:<subkind>:<name>` for symbol/comment
  artifacts — with `ts` = the artifact's last lifecycle-event date and `data` =
  the full artifact record. Additive to (and distinct from) the commit `code`
  nodes, which keep their bare-`<sha>` local id. Idempotent by id.
  **Cumulative (#76):** after writing its ledger rows, a fold rebuilds every
  artifact its bundle names (plus the other end of any recorded move touching
  one) from the repo's WHOLE ledger — `extract.ledger_events` →
  `derive.artifact_projection` (lifecycle in date order via
  `derive.chronological`, status from the last event, then the recorded moves).
  `validate.no_drift` re-derives with the same readers and rule, so a store
  holding several windows passes it, and folding windows in any order, or
  re-folding one, gives the same nodes. A fold with no code/symbol events skips
  this; one that has them reads the repo's ledger once (linear in its size).
  No schema change. A single-window store is unchanged. A multi-window store
  written before #76 keeps window-only artifact nodes until a fold names them:
  re-fold each of its windows (or rebuild it) to repair them all.
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
  `move_confidence`/`move_basis` and `move_commit` (the source's drop commit, so a
  reader can tell which removal of the source the move explains) in the edge `data`.
  Idempotent by (src,dst,type). A source is `replaced` by a move only when that move is
  its final removal; a source revived and deleted again without a new move is `removed`.
  Moves are matched **per fold window** (the bundle's own events), not over the
  whole ledger: over months, a symbol's creation `add` and its later move `add`
  would make every move ambiguous. The edges are the record of the moves; the
  artifact nodes apply all of them (`derive.apply_recorded_moves`): a dest
  takes `identity_from` from its most recent incoming move, and a source is
  `replaced` by the dest of its most recent outgoing move only while its last
  lifecycle event is a remove (a later re-add makes it `live` again). So a drop
  in one window and an add in another are not a move. Move edges are history:
  never retracted.

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
  (area→area)** from `code_graph.areas[].edges`, carrying `{version,transitive,…}` in
  the edge `data`. **`in_milestone` (social→structure)** from a PR/issue's
  `milestone` title → the milestone node (keyed on number when present).

The contribution / `owns` / `touches` / `depends_on` / `in_milestone` edges
never leak into `extract`: every one is non-spine, so `traverse_spine` never
follows it, and extract reads none of them.

### Current-state edges (#95)

A store is kept across months, so an edge must not outlive the input it was
derived from. Edges fall in two classes:

- **History** — `authored`, `merged`, `reviewed`, `reported`, `commented`,
  `closes`, `cross_ref`, `part_of`, `touches` (a commit's files never change)
  and the symbol moves `replaced_by` / `identity_from`. Only ever added.
- **Current state** — `blocks`, `in_milestone`, `in_iteration`, `depends_on`,
  `owns` (`gather.CURRENT_STATE_EDGE_TYPES`). Each fold, inside its
  transaction and after its nodes and window facts are written,
  `_maintain_current_state` makes the repo's edges of these types equal to
  what the repo's **stored inputs** imply, writing the missing ones and
  deleting the ones those inputs no longer imply.

| Edge type | Input it follows | Who speaks for a source (authoritative input) |
|---|---|---|
| `in_milestone` | the stored PR/issue record's `milestone` → a stored milestone node (the fold's own milestones first, in bundle order as before, then the stored ones by id) | the stored record; a fold replaces it when its bundle holds the item |
| `in_iteration` | the stored record's `iteration` → a sprint of the fold or a stored `sprint-<id>` node | the stored record |
| `blocks` | the `blocks` refs of the stored issue records, between two STORED issues; an edge stands while either end's record states it | both ends' stored records |
| `depends_on`, direct (`transitive` false) | the code graph of the source area's latest authoritative window and of every later window | a window whose build of the area resolved (`edges_status: "resolved"`), or whose code graph carries the whole-tree structural scan (`structural_scan: true`, set by `apply_structural_scan`; then for every area of the repo, listed or not, except the areas in `structural_scan_unread`: module files the scan could not read, issue #109) |
| `depends_on`, transitive | as above | only a window whose build of the area resolved (the scan sees direct `module` refs only) |
| `owns` | `_owns_pairs` over the code graph + CODEOWNERS of the area's latest authoritative window and of every later one | a window whose code graph lists the area and that read a CODEOWNERS file (non-empty `code_owners`) or a checkout (`clone_sha`; then a missing CODEOWNERS means no owners), unless its code graph has `codeowners_unread` (the file was there but could not be read, issue #109) |

For `depends_on` / `owns` the windows are the repo's gathered windows
(`gathered_windows`) with their `facts:` entries (#94), replayed in window
order — end day, then start day, the order `extract` uses to pick the latest
window (`gather.StructureReplay`). A source's edges are the union of its
in-effect windows (the latest authoritative one and every later one; all
windows when none is authoritative), each edge carrying the data of the latest
window that derives it.

**Never on absence of evidence.** A window-scoped directory graph that does not
list an area, a build that timed out / failed / was skipped, a window without
CODEOWNERS or checkout, a member whose fold failed (it rolls back as one
transaction) or that was quiet (its scan sees the same tree) retract nothing.
A single-repo fold (`members` None) derives no cross-repo `depends_on`, so it
leaves the ones a manifest fold wrote alone. When a gathered window of the
repo has **no `facts:` entry** (folded before #94, or a bundle without
`from`/`to`), the replay is skipped: the fold adds this bundle's `depends_on` /
`owns` edges as before #95 and retracts none (`current_state.structure`
false). History edges are never touched.

**Stand-ins.** Every `depends_on` target the fold keeps that is not stored gets
the stand-in node (create-if-absent). After a retraction, a stand-in
(`data.synthesized == "depends_on_target"`) that no edge references any more is
deleted, so a member that only existed as another member's dependency target
leaves the project (`project_repos`). A real area node is never deleted.

**Order.** `depends_on` / `owns` depend only on the SET of windows' facts:
folding windows in any order converges. The item relations follow the stored
record, which is upserted last-fold-wins, so re-folding an OLDER bundle after
a newer one brings back the older record and with it its milestone, iteration
and blockers (the store stays consistent with its records either way; order
matters exactly as it already did for the record). A window's structure is
read at its last mainline commit (issue #107), so a window re-gathered later reads
its own tree; only when the bounded clone does not reach that commit
(`meta.structure_as_of: "head"`) does it carry a newer tree than its dates say.

**What was retracted** is in the summary `fold_bundle` returns (`gather` prints
`retracted N stale current-state edge(s) of P/R (…) (meta retractions:P/R)` to
stderr) and in the meta ledger `retractions:{project}/{repo}`: a JSON list
(oldest first, at most 100 entries) of `{from, to, counts: {type: n}, edges:
[[src, dst, type], …] (first 200, sorted), standins_removed: [id, …]}`. A fold
that retracts nothing appends nothing, so a re-fold stays a no-op.
`current_state:{project}/{repo}` = `{items: true, structure: <bool>, from, to}`
records how the last fold kept the repo's current-state edges; `validate`
reads it (*Multi-window stores* below). The first fold since #95 into an older
store heals it: stale edges earlier folds left are retracted on the same rules
(and logged).

**Slice 7b-2 — reader contract: `extract` materializes `artifacts` + `people`
from the stored nodes; `link.enrich` shrank.** Because the write path now derives
those two projections identically to how `enrich` used to (store-derived ==
link-derived, locked by `test_characterization.py`), `extract` no longer emits a
RAW-only view — it additionally reconstructs:

- **`artifacts`** by reading the artifact `code` nodes (the non-commit ones —
  local id `art:<path>` / `<path>#…`), keyed by that local id (== `artifact_id`).
  extract keys the dict in artifact-id order; no consumer depends on that order
  (issue #100: `build_timeline`, `compute_feature_deltas` and render's content
  timeline each sort by an explicit key), so a view read back from `sort_keys`
  JSON enriches byte-for-byte the same.
- **`people`** by reading the project-scoped person `structure` nodes (repo
  sentinel `"*"`, local id `person-<login>`), keyed by login (the redundant stored
  `login` field is dropped). Because fold now persists ALL participants, the
  stored set is complete (each record carries `modules`/`areas`/`is_bot`). The
  view keeps only the logins with a contribution in the view's own records (this
  repo, this window; issues #85, #88) and replaces each kept record's
  `modules`/`areas` with what `derive.enumerate_participants` derives from those
  records with this repo's stored `code_graph` (`extract._view_people`). The
  person node itself is never changed by a read: it stays the project-wide union.
  On a one-window, one-repo store the view holds every stored record, so the
  view's people equal the stored ones.
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
`attribute_people_areas` (the one exception, since #88: a view's people keep the
stored record but take `modules`/`areas` from the view's own records, see
`people` above). Correspondingly `link.enrich` removed its
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
STORED issue (a ref to an issue the store does not hold is dropped, never left
dangling; it appears once a later fold stores that issue, #95).
`blocks` is a NON-spine relation (not in `SPINE_EDGE_TYPES`), so `traverse_spine`
never follows it and decision-train composition is unchanged; its issue→issue shape
already matched `validate.check_schema_conformance`'s allowlist.

`in_iteration` (social→sprint) is written when the gather read a Projects v2
board (Phase 12; the `read:project` scope). The
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
  range query), so it does NOT count toward in-window activity. In production only
  `spotlight.py --complete` wires a fetcher (`gather.make_backfill_fetcher`, through
  `complete.complete_train`); `view.py`, `digest.py` and `validate.py` call
  `extract` with `backfill=None` (offline, warn-only). `make_backfill_fetcher`'s live
  REST/git paths are therefore only exercisable against a real repo, not by the
  offline suite.

## Train completion + dead-ref memory

The single-node `backfill` bridge is promoted into a shared
**completion orchestrator** (`complete.py`) that closes a train **transitively**
along the causal spine, **bounded by the query's time window** (level-0
directly-referenced anchors always filled; transitive expansion stops at the
window edge; closure when no window). Both readers call it — `extract` (refactored
off its inline loop) and `spotlight` (its train queries — `person`, `subsystem`,
`grep`, `module` — take optional `--complete`).
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

A Bicep publisher declares the module path prefix it publishes under in the public
Bicep registry instead (issue #92), e.g. for an enterprise landing zone consuming AVM:

```json
{ "project": "contoso-lz",
  "window": { "from": "2026-03-01", "to": "2026-03-31" },
  "repos": [
    { "owner": "Azure", "repo": "bicep-registry-modules", "bicep_registry_prefix": "avm/" },
    { "owner": "Contoso", "repo": "landing-zone" } ] }
```

`manifest.load_manifest` validates the file before the store is touched and raises
`ValueError` (`manifest: …`) on: a non-string or empty field; a `project` containing
`/` or `#`; a window bound that is not a real `YYYY-MM-DD` date, or `from` after `to`;
an `owner` that is not a GitHub user/org name (letters, digits, `-` and `_`, not
starting with `-`, ≤ 39 chars); a `repo` outside GitHub's alphabet (letters, digits,
`-`, `_`, `.`; ≤ 100 chars; not `.`/`..`); two members naming the same repo
**case-insensitively** (`o/a` and `O/A`); a `registry` that is present but not a
non-empty string, or declared by two members (case-insensitively); a member
`branch` (optional, issue #84: that member's mainline, beating a global
`--branches`) that is present but not a non-empty string without whitespace or
control characters, or starts with `-`; a
`bicep_registry_prefix` that is present but not a lowercase public-registry path
prefix (OCI path segments such as `avm/`; no `br/public:` scheme, no leading `/`),
or declared by two members (a trailing `/` is ignored; nested prefixes such as
`avm` and `avm/ptn/contoso` are allowed and the longer one wins); a
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
  on the node's `external_refs` (honest, not a fetchable gap). An other-repo entry of
  the PR's stored `closing_issues` (GitHub-reported, #105) is a `closes` ref the same
  way, and an issue mention it also closes is not a separate `cross_ref`.
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
- **Cross-repo `depends_on` (Bicep, #92).** A Bicep edge (`provider: "bicep"`) that
  the gather left unresolved (`to=None`) resolves through
  `resolve_bicep_registry_member`: only a **public**-registry ref
  (`br/public:<path>:<tag>` or `br:mcr.microsoft.com/bicep/<path>:<tag>`) can, and it
  maps to the member whose `bicep_registry_prefix` is a whole-segment prefix of
  `<path>` (case-insensitive; longest prefix wins). The target is that member's
  **module area**, `classify_code_area("<path>/main.bicep")` — e.g.
  `br/public:avm/res/key-vault/vault:0.9.0` →
  `{project}/Azure/bicep-registry-modules#area-avm/res/key-vault/vault` — because a
  public-registry path is the module's directory in the publisher's repo (as in
  bicep-registry-modules). The edge keeps `ref`/`version`/`provider: "bicep"` and
  gains `resolved: true`, `cross_repo: true`. **Never resolved:** a private
  registry (`br:<other-host>/…`), a template spec (`ts:…`), a user alias
  (`br/<alias>:…`), a public path no member declares, an AVM group path
  (`avm/res/<group>`), and the referencing member's own prefix (its own unresolved
  ref names a module its tree does not hold; in-repo resolution is the
  gather's job). Those stay `to: null` in the bundle and produce no stored edge.
  Bicep edges never go through the Terraform resolver, and Terraform edges never
  through this one.
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
**A nested module directory is its own area (#101):** a `*.tf` file in a
non-scaffold directory below `modules/<name>/` (`modules/sub/inner/main.tf`) maps
to that directory (`modules/sub/inner`), as terraform reads one directory as one
module, so its own `module` blocks are reported on it and `modules/sub`'s
`source = "./inner"` targets it (it used to be a self-edge, and the inner dir's
references were reported nowhere). Other files there (README, templates) stay in
`modules/<name>`. A store gathered before this keeps the old `modules/<name>`
area for those files and its self-edge; re-gather for the new ids (a later fold
retracts the old self-edge once a resolved build or the structural scan speaks
for that area, #95; the old area node itself stays).
The module dir is resolved on disk in the clone, whichever of its files changed in
the window (see BUNDLE.md, *Entrypoint*). `terraform graph` runs through the same parallel pool
as the bicep build; with a shared `TF_PLUGIN_CACHE_DIR` a one-shot serial provider
warm-up precedes the parallel inits (terraform's plugin cache is not write-safe
under concurrency). The IaC build knobs are env-overridable for heavy runs:
`ACTIVITY_IAC_BUILD_TIMEOUT` (default 300s), `ACTIVITY_IAC_MAX_WORKERS` (8),
`ACTIVITY_IAC_RETRIES` (1).

A cross-repo `depends_on` resolves to the producer member's module-root area
(`area-main.tf`), or for Bicep to the publisher's module area
(`area-avm/res/<group>/<module>`). That node is only created by the producer's own
fold when it is an area of the producer's bundle — an in-window code area, or a
module the structural scan appended — and often it is not. The consumer fold
therefore **ensures the target node exists** (a minimal `structure` stand-in,
create-if-absent so a producer's real area always wins), keeping the edge from
dangling. The node is an edge anchor only: `extract` rebuilds `code_graph` from
the `codegraph` singleton, never from `area-*` nodes, so it is invisible to every
member bundle. When the consumer's reference goes (#95) the edge is retracted and
a stand-in no edge references any more is deleted. The shallow-clone margin is `ACTIVITY_CLONE_MARGIN_DAYS`
(default 14); widen it when `meta.boundary_dropped_commits` is non-empty.

The DOT-based extraction (`terraform graph`) only resolves edges for module areas
the directory provider sees — i.e. in-window-CHANGED dirs — so the dependency graph
would otherwise track churn rather than structure. On **multi-repo (manifest)**
gathers, `scan_structural_terraform_areas` additionally parses every module's
`source` references across the whole tracked tree (static, no `terraform init`) and
`merge_structural_areas` unions them into `code_graph` (existing areas keep their
DOT edges + gain missing structural ones; untouched module dirs are appended). The
blast-radius graph is then the repo's full module structure regardless of in-window
activity. `git ls-files` lists only tracked `.tf`, excluding vendored `.terraform/`
modules.

**Bicep structural scan (#92).** `scan_structural_bicep_areas` does the same for
Bicep, from the same single `git ls-files` listing (`apply_structural_scan` runs
the Terraform then the Bicep scan and merges both with `merge_structural_areas`).
A Bicep **module area** is an area whose own entrypoint `<area>/main.bicep` (the
root `main.bicep` for the root area) is tracked; AVM group areas, areas without an
own `main.bicep`, and `examples/`/`tests/` scaffolding are not modules, and a
private child (`<module>/<child>/main.bicep`) belongs to its parent area and is not
read. Each module's edges are `build_bicep_edges` over that one file with no ARM
tree — exactly the build path's immediate (`transitive: false`) edges, so the
merge's `(to, ref)` dedup never doubles a built edge and build-derived edges (and
their transitive edges) win. A public-registry ref resolves in-repo when its
module area is one of the tracked module areas; anything else stays `to: null`
for the cross-repo fold above. Cost: one pass over the listing and one read per
module entrypoint, no `bicep` CLI (about 0.2 s for 1,000 module dirs).

**When the scan runs.** Every manifest member (`_member_args` sets
`structural_iac`), and a single-repo gather given `--structural-iac` (Terraform and
Bicep). A single-repo gather without the flag stays window-scoped and build-gated,
so its bundle (and the golden-bundle gate) is byte-stable. With the scan, every
module is an area node, so `spotlight dependents owner/repo::<area>` (the
consumers of one module) works inside a monorepo such as bicep-registry-modules.

**Terraform direct vs transitive (#80).** The build path and the structural scan
use one rule for direct edges: every `module {}` block in the area dir's own
`*.tf` is a `transitive: false` edge, consumed or not. So a structural edge never
contradicts a built one, and the merge dedups on `(to, ref)`. `terraform graph`
only adds **nested** calls (`module.a.module.b`, both DOT layouts) as
`transitive: true` edges carrying `address`/`via`. Their targets are read from
init's `.terraform/modules/modules.json`. A nested target already reached by an
edge is dropped: the same area, or the same registry module ignoring host case
and `//submodule`, which is exactly what `_fold_depends_on` resolves to one
member. So a transitive duplicate cannot overwrite a direct `depends_on`, which
is keyed `(src, dst, depends_on)`. A nested edge with no known target (no
manifest) has `to = ref = null` and is never folded.

**People aggregate across members.** Person nodes are project-scoped (`repo="*"`).
Each per-member fold **unions** a person's `areas`/`modules` (and ORs `is_bot`)
with the already-stored node — a plain upsert would overwrite, so a member where a
contributor has no attribution would erase another's. The aggregate is fold-order
independent and idempotent (re-folding a member unions a subset → no change).
The node is the project-wide union over members and windows, and it is what
`validate`'s `no_drift_people` checks; it is **not** what a view shows. Area ids
are repo-local (two members can both have a `core` or `main.tf` area), so the
union cannot say which repo an area belongs to: a member view re-derives a
person's areas from its own records, and the project view qualifies them as
`"<repo>::<area>"` (issue #88; BUNDLE.md "Project view: `people` and `halls`").

**Readers + validate aggregate the member set.** `digest.py` is the project
aggregation layer above `extract` (per-member extract+enrich, then merge:
spine-linked trains stitched across members; trains sharing only an internal ticket
clustered into `related_work` — opt-in, only with the manifest's `ticket_prefixes`
(`digest.py --manifest`) or `--ticket-pattern`, else `[]`; Shipped/modules merged;
people merged with each figure summed / combined across members and `halls.fame` rebuilt
from them (#88); cross-repo
`module_edges` for the project-wide blast-radius graph). `spotlight dependents
<owner/repo>` answers the reverse-dependency (blast-radius) question per member;
`spotlight dependents <owner/repo>::<area>` answers it per module area (direct and
transitive consumers, in-repo and cross-repo; #92). `validate.py
--project <name>` runs the per-member checks **plus** a project-wide
`no_drift_people` check that re-derives people across **all** members and unions
them (the read-side mirror of the fold) — people are project-scoped, so they are
audited once project-wide, not per member.

## Roll-up / resume (slice 7c)

A multi-window "wider view" is a view over a wider window of the same store — the
window rule below applied to the union of folded windows, NOT a multi-bundle merge.
Folding two adjacent windows (March, April) then viewing `[March-start, April-end]`
gives the union of their records; each narrow window still gives only its own.
(The manifest `gather --resume` flag is a different thing: it re-attempts the members
whose last gather of the window did not complete — see `member_status` above.)

**Window-scoped views (issue #85).** `extract` builds a view from the records of
its window only, not from the repo's whole node set: PRs merged/closed in the
window or open by its end, issues closed/updated in it or open by its end or
closed/cross-referenced by a view PR (train context), commits dated in it and
their ledger rows, releases published in it, reviews/events with their parent,
any node whose `ts` is in the window and any undated node (BUNDLE.md "Window
scope"). Artifact nodes stay cumulative in the store (#76); a view keeps each
artifact's lifecycle events of its window, with status as of the window's end.
So one store can grow window by window: a view of one gathered window equals the
view of a store holding only that window (up to PR/issue records a later gather
refreshed — nodes are upserted latest-wins, and the view then carries
`meta.state_as_of`), and a view over `[March-start, April-end]` is the union of
the two. Not windowed: milestones and sprints (latest fetch); people's stored
`modules`/`areas` are the union over windows, and a view re-derives them (#88).

**Per-window facts (issue #94).** A view reads `workflow_stats`, `code_graph`,
`code_owners`, `label_taxonomy` and `meta.clone_sha` from the `facts:` entries of
the repo's gathered windows (`extract._window_facts`):

- **Exactly a gathered window** — that window's values (and nothing else in
  `meta`), so each month of a growing store shows its own CI statistics,
  structure and clone sha, and its people areas / code-area attribution use its
  own code graph.
- **Any other window** — CI statistics are the **sum** of the gathered windows
  lying wholly inside the view (`derive.sum_workflow_stats`: counters added per
  workflow, truncation markers merged). Summed windows never overlap one another
  (by start day, the longest first: a roll-up window is summed instead of the
  months it covers, never with them). A gathered window that only partly
  overlaps the view is not summed (its runs cannot be split by day); with no
  gathered window inside the view `workflow_stats` is omitted. The structure
  (code graph, owners, taxonomy, clone sha) is that of the latest gathered window
  overlapping the view (by `to`), else the latest ending before it, else the
  earliest. `meta.facts_windows` says which (BUNDLE.md).
- **No `facts:` entry for the repo** (folded before #94) — the latest-wins
  singleton nodes and the repo's latest `clone_sha`, as before.

Overlapping re-folds are idempotent (identity-keyed nodes/edges, set
semantics on the ledger, deduped `gathered_windows`, per-window meta keys
replaced), so re-extracting an overlapping window is byte-stable. A roll-up view
reads the `gathered_windows` ledger (`record_window`/`get_windows`) and the windows'
`facts:` entries; nothing is merged.

## Trust gate (validate.py)

`python3 validate.py STORE.db` audits the store for trustworthiness and exits
non-zero on any ERROR (CI-gateable). It is **self-contained on a store**: the
real-data checks — `edge_set` (stored edges == re-derived from the stored records),
`no_drift` (stored people/artifacts == freshly re-derived) and `idempotency` (a
re-fold changes no counts) — need a raw bundle to re-derive against, and they
**self-source it from the store** via `extract` over the store's full activity
window. A `--bundle` argument is an optional cross-check of the records
themselves, never required (`edge_set` always uses the self-sourced one). This is what makes the store a
stand-alone deliverable.

The gate can only fail, never pass by omission (issue #79):

- **The target must exist.** The first check, `target`, fails when the store
  holds no nodes, when `--project` has no nodes, or when `--repo` is not one of
  the project's stored member repos (a `--repo` in another case resolves to the
  stored spelling, as in the readers). A mistyped name is a failure, not an
  audit of nothing.
- **Every check is always reported.** When `edge_set`/`no_drift`/`idempotency`
  cannot self-source a bundle they are reported as FAILED with
  `could not self-source: <reason>`: the target has no nodes, a node row cannot
  be decoded, or the repo stores social/code nodes but none carries a `ts` (the
  activity window cannot be recovered). A member holding only structure nodes
  (nothing happened in its window) self-sources over its recorded gathered
  window instead. In project mode `no_drift_people` fails the same way when a
  member cannot be self-sourced.
- **No crashes.** Every check is guarded: a corrupt row (data that is not valid
  JSON, or a social/code/person blob that is not a JSON object) or any other
  exception becomes a failed ERROR check naming the error and the row id, and
  `--json` output is still produced.
- **Read-only.** The CLI opens the store with `graphstore.open_store_readonly`
  (SQLite `mode=ro` URI): it never creates a missing file, sets no journal mode
  and runs no schema step. The idempotency re-fold runs in an in-memory copy.
  Readers no longer create `dead_refs` either (`traverse_spine(skip_dead=True)`,
  `is_dead_ref` and `get_dead_refs` treat a missing table as "no tombstones"),
  so auditing a store leaves its file byte-identical.

Strictness (issue #89). Besides the checks above:

- **`edge_set`** — a repo's stored edges equal the edges its stored records
  imply, in **both** directions: the self-sourced bundle is folded by
  `gather.fold_bundle` into an empty scratch store and its edges compared with
  the stored ones (an edge belongs to the repo of its src; a person's edge to
  the repo of its dst). An extra edge, a missing one, or different edge `data`
  is an ERROR. A stored edge into another member repo (manifest `closes` /
  `cross_ref` from a PR's qualified refs, or a registry `depends_on` to a
  member's root area) must be one the manifest fold derives from the stored PR
  / code graph; a *missing* cross-repo edge is not detected (it needs the
  manifest's registries).
- **`provenance`** — a PR / issue cites `https://github.com/{owner}/{repo}/pull|issues/N`
  of its own repo (case-insensitive) and number, and `data.number == N`; a
  review / event cites its parent (or fold's synthesized fallback, or a
  timeline event's `api.github.com/repos/{owner}/{repo}/` url); a commit id is
  a 40/64-hex sha equal to `data.sha`.
- **`no_fabrication`** — ids follow the grammar above, their scope equals the
  row's `project`/`repo` (people: `{project}` with repo `*`), the local form
  fits the node class, and no scope is stored under two case spellings. Every
  `code` node is one of exactly three forms (#101): a commit record (`sha`)
  under a 40/64-hex sha id, a file-artifact record (`kind` not symbol/comment)
  under `art:<path>`, or a symbol/comment record under
  `<path>#<lang>:<subkind>:<name>`; any other combination is an ERROR
  ("unknown code-node form"). `no_drift` audits every non-commit code node as an
  artifact, so no code node escapes both checks.
- **`no_drift`** — people are re-derived from the records alone (never seeded
  from the stored person nodes, which made the areas check tautological) and
  compare `modules`, `areas`, `is_bot` and `login_resolved`. A stored person
  that is a pre-#78 git-name identity gets a hint naming the commits' current
  author and "re-gather into a fresh store". `validate --repo <member>` of a
  multi-member project checks only the people that member derives (derived ⊆
  stored); the full equality is `no_drift_people` (no `--repo`).

**Multi-window stores** (more than one gathered window recorded for the repo).
`edge_set` and `no_drift` re-derive from ONE self-sourced bundle over the whole
store (every stored record; the structure of the latest gathered window, #94).
The current-state edge types are compared **exactly** (ERROR) when the store
shows they can be proven (`validate._multi_window_plan`, #95); otherwise a
difference is a WARN that names why:

| Edge type | Exact (ERROR) when | Expected set |
|---|---|---|
| `blocks`, `in_milestone`, `in_iteration` | the repo's last fold was since #95 (`current_state.items`) | the re-fold of the self-sourced bundle (every stored record, milestone and sprint) |
| `depends_on`, `owns` | the last fold replayed them (`current_state.structure`) and every gathered window still has its `facts:` entry | `gather.StructureReplay` over the windows' facts, without the manifest's registries; a stored cross-repo `depends_on` must match an unresolved registry edge of its source in a window in effect for it |
| `touches` | every gathered window has a stored code graph, none is a graphify one, and no path is in two different areas across windows | each stored commit's files against the union of the windows' path indexes |

The WARN stays for a repo last folded before #95 (fold any window once more), a
window without facts (re-gather it), and for `touches` a graphify window or a
path re-mapped between windows. A missing cross-repo `depends_on` is not
detected (it needs the manifest's registries). People's areas compare as
derived ⊆ stored (an extra stored area is a WARN): a person's areas are a union
over every window's activity — history, never retracted — and re-deriving them
exactly would need each window's own view (LLD §14.5).
`referential_integrity` also fails a `facts:` entry that cannot be read or names
a `fact:` value that is not stored.
Symbol moves are matched per fold window: each stored move must be supported by
the ledger (its twin edge, a drop of the source — by `move_commit` — and an add
of the destination, a consistent basis), and a move only a whole-store re-match
finds is not required. The idempotency probe re-folds every window as one; the
move and current-state edges that adds are reported as INFO, not counted (edges
are keyed by `(src, dst, type)`, so they are never duplicates). Current-state
edges the probe RETRACTS are a WARN, not counted, on a repo last folded before
#95 (the re-fold heals what that fold left behind) and a failure otherwise. On
a one-window store all of this is exact.

**What the gate proves, and what it does not.** Self-sourced, it proves the
store is *consistent*: every edge, person and artifact is exactly what the
stored records and ledger imply, and every citation points at the node's own
GitHub object. It does **not** prove the records are what GitHub holds:

- a record deleted together with its edges (and a person left with no other
  edge), or a blob rewritten consistently (title, `merged`, a comment body, a
  reviewer dropped with its edge), leaves a consistent store;
- the artifact part of `no_drift` re-derives artifacts from the store's own
  ledger plus recorded moves (#76), so a wrong ledger (rows missing or invented)
  yields matching nodes — the check proves nodes == ledger, not ledger == git.

Pass `--bundle` with a bundle saved when the store was gathered — a `view.py`
JSON of the fresh store, or a raw bundle — to catch changed or deleted records:
every PR / issue / commit in it must be stored with the same content (reviews and
lifecycle compare without the synthesized `url`). It must hold the latest fetch of
its records. The store keeps no per-fold content digest.

Exit codes: 0 no ERROR; 1 an ERROR check failed (a report is printed); 2 usage
error with no report (the store path does not exist or is not a store, several
projects or repos and none named, unreadable `--bundle`).

## Determinism

`data` blobs serialize with `sort_keys=True`; `range_query` orders by
`(ts, id)` and applies the granularity-safe window rule above (`ts_in_window`),
so the same window gives the same node set whether its bounds are written as
dates or as start/end-of-day datetimes. Given fixed inputs the store is byte-stable (modulo `fetched_at`).
