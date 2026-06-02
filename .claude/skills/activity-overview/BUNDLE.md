# Bundle Schema

The bundle is a single JSON object produced by the pipeline. The **gather** step
(`gather.py`) fills `meta`, `commits`, `prs`, `issues`, and — from Phase 2 —
`workflows`, `workflow_stats`, `releases`, `milestones`; the **link** step
(`link.py`) then fills `trains` and `buckets`; the **render** step (`render.py`)
fills `diagrams`. Every other top-level key is reserved empty by gather and
populated in later phases. (See **Phase 2 fields** below for the fields added
after the walking skeleton.)

## Ref convention

Every provenance reference is `{ "type": "pr|issue|commit", "id": <number|sha>,
"url": "https://..." }`. Every narrative-bearing fact (a train, a bucket entry)
resolves to at least one such ref. To fact-check any claim in a report, follow its
ref `url` to GitHub. (Phase 1 emits `pr` and `issue` refs; commits appear as bare
SHAs in `trains[].commits` — wrapped `commit` refs arrive in a later phase.)

## Top-level keys

Scope notes mark where Phase 2 widens what Phase 1 collected; field lists below
are the Phase 1 baseline shapes, extended by **Phase 2 fields**.

- `meta` — `owner, repo, from, to, branches, clone_dir, period, prev_bundle, schema_version`.
- `commits` — `[{ sha, parents, author, date, message, files, pr }]` (`pr` set by link).
- `prs` — PRs in scope (Phase 1: merged-in-window only; **Phase 2** also includes
  open and closed-unmerged PRs): `{ number, title, body, author, author_association,
  labels, merged, merged_by, merged_at, closed_at, state, closes:[issue#], url }`.
- `issues` — issues in scope (Phase 1: PR-closing issues only; **Phase 2** also
  includes open and not-planned-closed issues): `{ number, title, body, kind, author,
  author_association, labels, assignees, state, state_reason, closed_at, url }`.
- `trains` — `[{ id, kind, root_issue, prs:[#], commits:[sha], outcome, evidence:[ref] }]`.
- `buckets` — `{ shipped:[ref], in_flight:[ref], rejected:[ref], next_candidates:[ref] }`
  (Phase 1 fills only `shipped`; Phase 2 classifies all four — see below).

## Reserved (empty in Phase 1)

`timeline, artifacts, feature_deltas, people, halls, flow, blockers, code_owners,
code_graph, label_taxonomy, modules, workflow_stats, workflows, releases, milestones,
docsRefs, release_train, sprints, project, diagrams`.

## Phase 2 fields

- **prs[]** gain `created_at`, `updated_at`, `milestone`, `comments`,
  `review_comments_count`, `reviewers`, `review_decision`
  (`approved|changes_requested|commented|none`), and `crossref_issues`.
- **issues[]** gain `milestone`, `updated_at`, `comments`. Open and
  not-planned-closed issues are now included, not just PR-closing issues.
- **workflows[]**, **workflow_stats{}**, **releases[]**, **milestones[]** are
  populated (see the schema block in the design spec).
- **buckets** are fully classified: `shipped`, `rejected`, `in_flight`,
  `next_candidates` (one bucket per item; precedence shipped > rejected >
  next_candidates > in_flight). Each ref may carry a `train` id.
- **diagrams{}** maps `buckets_pie` / `timeline_gantt` to their `.mmd` paths,
  written and mmdc-validated by `render.py`.

## Phase 3a fields (narrative substrate)

- **prs[]** gain `review_comments: [{author, author_association, body, url, id, created_at}]`
  (inline diff comments) and `comments_list: [{...same shape}]` (conversation
  comments). The Phase 2 integer count stays under `comments` /
  `review_comments_count` — the spec's `comments` *body-array* name was already
  taken by the Phase 2 count, so bodies live under `comments_list`.
- **issues[]** gain `comments_list: [{...}]`, `reactions: {"+1","-1","heart",
  "hooray","total"}`, and `open_high_activity: bool` (open issue with notable
  comments/upvotes).
- **code_events** (gather) — raw file-level events from the full-window
  `git log --name-status -M -C` walk: `[{commit, author, date, change:
  add|modify|delete|rename|copy, path, old_path?}]`. The raw material Link folds.
- **artifacts** `{ "<id>": { kind:"example|doc|readme", path, name,
  status:"live|removed|replaced", replaced_by:id|null, code_area:null,
  lifecycle:[{event:"add|change|remove", commit, author, date, ref}] } }`.
  File granularity only in Phase 3a. **Deferred:** `symbol`/`comment` kinds (need
  `-p` hunk + tree-sitter), `code_area` (graphify, Phase 3b), and per-event `hunk`.
- **timeline** `[{ ts, actor, layer:"social|code", event, ref:{type, id, url},
  subject:{kind, name, path} }]` — sorted social (comments/reviews) + code
  (artifact lifecycle) events. `ref.id` is the PR/issue number for social events
  and the commit sha for code events (the bundle-wide `{type, id, url}` ref
  convention). Social events have no file `subject.path`; code events carry both.
- **feature_deltas** `[{ area:null, kind:"add|drop|change", subject, name, before,
  after, detail, artifact:id, author, train:id|null, pr:num|null, commit:sha, url
  }]` — a projection over `artifacts` (add→add, remove→drop, change→change).
  `area`/`before`/`after`/`detail`/`hunk` are null/absent until graphify + hunk
  parsing (later slice). `pr`/`train` resolve best-effort via the commit→PR map.
- **diagrams{}** now also maps `content_timeline` (Mermaid `timeline`) and
  `deltas_bar` (Mermaid `xychart-beta` bar).
