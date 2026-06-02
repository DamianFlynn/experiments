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

## Top-level keys (Phase 1)

- `meta` — `owner, repo, from, to, branches, clone_dir, period, prev_bundle, schema_version`.
- `commits` — `[{ sha, parents, author, date, message, files, pr }]` (`pr` set by link).
- `prs` — merged-in-window PRs: `{ number, title, body, author, author_association,
  labels, merged, merged_by, merged_at, closed_at, state, closes:[issue#], url }`.
- `issues` — closing issues: `{ number, title, body, kind, author, author_association,
  labels, assignees, state, state_reason, closed_at, url }`.
- `trains` — `[{ id, kind, root_issue, prs:[#], commits:[sha], outcome, evidence:[ref] }]`.
- `buckets` — `{ shipped:[ref], in_flight:[], rejected:[], next_candidates:[] }`.

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
