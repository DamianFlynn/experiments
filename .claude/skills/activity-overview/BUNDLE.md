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

- `meta` — `owner, repo, from, to, branches, clone_dir, period, prev_bundle, schema_version`,
  plus **`clone_sha`** — the clone's HEAD commit the `code_graph`/edges were built against.
  It pins the exact source tree so `gather.py --resume <prior-bundle>` can re-resolve **only**
  the `timeout`/`failed` edges against the identical tree (edges are a pure function of source),
  and a multi-bundle roll-up can pick the newest structural snapshot deterministically.
- `commits` — `[{ sha, parents, author, date, message, files, pr }]` (`pr` set by link).
- `prs` — PRs in scope (Phase 1: merged-in-window only; **Phase 2** also includes
  open and closed-unmerged PRs): `{ number, title, body, author, author_association,
  labels, merged, merged_by, merged_at, closed_at, state, closes:[issue#], url }`.
- `issues` — issues in scope (Phase 1: PR-closing issues only; **Phase 2** also
  includes open and not-planned-closed issues): `{ number, title, body, kind, author,
  author_association, labels, assignees, state, state_reason, closed_at, url }`.
- `trains` — `[{ id, kind, root_issue, prs:[#], commits:[sha], code_areas:[id], outcome,
  significance:float, tier:"deep"|"mention", effort:{opened_at, merged_at, elapsed_days,
  reviewers, review_comments, commits, participants, stalled}, evidence:[ref] }]`.
  `significance = footprint × kind_weight + breadth`; `tier` = `"deep"` for the top-N
  by significance ∪ any train ≥ the significance floor, else `"mention"` (Phase 4a).
  `effort`: `reviewers` = distinct reviewer logins; `review_comments` = summed
  `review_comments_count`; `participants` = distinct authors + reviewers + comment-authors;
  `stalled` = merged but `elapsed_days > TRAIN_STALL_DAYS`. All fields degrade to null/0
  when data is thin. Tunables: `TRAIN_KIND_WEIGHTS`, `TRAIN_SIGNIFICANCE_TOP_N`,
  `TRAIN_SIGNIFICANCE_FLOOR`, `TRAIN_STALL_DAYS`.
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
  written and mmdc-validated by `render.py`. **`diagrams.train_flowcharts`** is a nested
  map `{ "<train-id>": "diagrams/<train-id>.mmd" }` written by `render.py` (one adaptive
  Mermaid `flowchart` per DEEP train; mode C with code-area annotation nodes when
  `len(prs) ≤ TRAIN_FLOW_MAX_PRS` and `len(code_areas) ≤ TRAIN_FLOW_MAX_AREAS`, else
  mode A bare chain). On-demand spotlight render via `render.py --train <id>` (any tier).
- **forecast** `{ next_milestone:<title|None>, candidates:[ { ref:{type,id,url},
  train:<id|None>, score:float, tier:"likely"|"possible"|"longshot", signals:[str,...] } ] }`
  — forward-only next-release forecast over `buckets.next_candidates`, sorted by score
  desc. Signals: `on milestone <title>` / `high-priority` / `open PR` or `work in progress`
  / `active in window` / `long-open`. Tunables: `FORECAST_WEIGHTS`,
  `FORECAST_TIER_LIKELY_THRESHOLD` (≥5.0 → likely), `FORECAST_TIER_POSSIBLE_THRESHOLD`
  (≥2.0 → possible), `FORECAST_OVERDUE_DAYS` (Phase 4a).

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
- **symbol_events** (gather, Phase 3d) — symbol-granular change events from a single
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
  joins back to these keys. `code_area` is filled in Phase 3b. Symbol lifecycle entries
  carry the bounded `before`/`after`.
- **symbol_moves** (link, Phase 3e) — window-wide symbol-identity links: `{ links:[{subkind,
  name, from_path, to_path, from:<src aid>, to:<dst aid>, confidence:"high|medium", basis:
  "file_rename|unique_name"}], by_confidence:{high,medium} }`. Each link object also carries
  `lang`. A move = the same `(lang, subkind, name)`
  symbol dropped in ONE file and added in ONE other (precision over recall — ambiguous/boilerplate
  names are skipped). On the linked artifacts the source is `status:"replaced"` + `replaced_by`
  the dest, the dest carries `identity_from`, and both carry `move_confidence`/`move_basis`.
  `confidence` is `high` only when git also flags the file pair as a rename/copy. (Name-changed
  renames via body fingerprint are deferred.)
- **timeline** `[{ ts, actor, layer:"social|code", event, ref:{type, id, url},
  subject:{kind, name, path} }]` — sorted social (comments/reviews) + code
  (artifact lifecycle) events. `ref.id` is the PR/issue number for social events
  and the commit sha for code events (the bundle-wide `{type, id, url}` ref
  convention). Social events have no file `subject.path`; code events carry both.
- **feature_deltas** `[{ area, kind:"add|drop|change", subject, name, before,
  after, detail, artifact:id, author, train:id|null, pr:num|null, commit:sha, url
  }]` — a projection over `artifacts` (add→add, remove→drop, change→change). For
  **symbol/comment** subjects (Phase 3d) `before`/`after` carry the bounded hunk
  snippet and `detail` is `"<lang> <subkind> <name>"`; file-level deltas leave them
  null. `area` is filled by area attribution. `pr`/`train` resolve best-effort via
  the commit→PR map.
- **diagrams{}** now also maps `content_timeline` (Mermaid `timeline`) and
  `deltas_bar` (Mermaid `xychart-beta` bar).

## Phase 3b fields (code areas + label facets)

- **code_graph** `{ "provider": "directory|graphify", "areas": [{ "id", "label",
  "paths": [...], "edges": [] }] }`. The **directory provider** (primary,
  zero-dep, offline) maps each tracked file to its module directory (AVM
  `avm/res/<svc>/<module>/`, any `main.bicep` dir, Terraform `modules/<name>/` or
  any `*.tf` dir, else a top-2-segment fallback); the area `id` is that directory.
  **graphify** is an OPTIONAL provider for its ~25 tree-sitter languages — it reads
  graphify's real `graph.json` (top keys `nodes`/`links`; each node carries an
  integer `community` + `source_file`; **no** top-level `communities` list; edges
  live under `links`) and groups nodes by `community` into `community:<n>` areas.
  graphify does NOT parse Bicep/HCL, so on those repos the directory provider runs.
  **`code_graph.areas[].edges` (Phase 3c) — inter-area dependency edges.** Each edge:

  | field | meaning |
  |-------|---------|
  | `to` | canonical target **repo area-id** (`avm/res/<svc>/<module>` or a local dir), or `null` when the target is external/unresolvable (e.g. a registry module — its identity is in `ref`) |
  | `kind` | `"module"` — inter-area module dependency (resource-level `dependsOn` edges are deferred to 3d) |
  | `ref` | the raw reference (`br/public:…:<ver>`, a local path, or a TF `source`) |
  | `version` | pinned version when present, else `null` |
  | `transitive` | `false` = a direct source reference; `true` = discovered deeper in the resolved build tree |
  | `provider` | `"bicep"` or `"terraform"` |
  | `resolved` | `true` when `to` is non-null |

  **Build-only.** Edges are populated *only* from a successful `bicep restore`+`bicep build`
  (Bicep) or `terraform init -backend=false`+`terraform graph` (Terraform) against the cloned
  working tree. When the CLI or registry is unavailable, or a build fails, `edges` stays `[]` —
  the skill never emits static, unvalidated edges. Immediate (`transitive:false`) edges are fully
  identified (area-id + version) from source; transitive edges connect to *other areas in the
  repo* (deep external-module internals are not fabricated into edges). The `bicep` and
  `terraform` CLIs are therefore required to populate edges (see REFERENCE.md / the integration
  workflow for install). **Deferred:** symbol-granular artifacts (3d), symbol-identity tracking
  (3e), resource-level `dependsOn` edges.

  **Visible gaps (Phase 3c.1).** Per-module builds run in parallel, each bounded by a generous
  per-subprocess timeout (a healthy build finishes well under it; the bound only trips a
  genuinely hung process) and retried once. Each area carries `edges_status` ∈
  `{resolved, timeout, failed, skipped}` and `code_graph.edge_extraction` carries the aggregate
  `{ "resolved", "timeout", "failed", "skipped" }` counts — so a build that timed out or failed
  is a **visible, counted gap**, never mistaken for a module with no dependencies. `resolved`
  with an empty `edges` list genuinely means "no inter-area dependencies"; `timeout`/`failed`
  mean "not determined" (re-run to resolve). The integration gate reds when the unresolved rate
  is non-trivial, so an incomplete graph is re-run rather than silently shipped.
- **code_owners** `{ "<path|glob>": ["login", ...] }` — parsed from the clone's
  CODEOWNERS (`.github/`/root/`docs/`); `@org/team` and `@user` kept as logins.
- **label_taxonomy** `{ "<facet>": { "<namespace>": ["label", ...] }, "source":
  "auto|config|merged" }` — auto-detected structured label namespaces mapped to
  facets (`area`/`priority`/`status`/`lifecycle`/`kind`), with optional config
  override/extend. Degrades to `{ "source": "auto" }` (no facets) on unstructured labels.
- **issues[]/prs[]** gain **facets** `{ area, priority, status, lifecycle }`
  (each the first matching label or null). **issues[]** gain **kind** ∈
  `feature|module-request|bug|idea|question|docs|other` (native issue type →
  label kind facet → template filename → title/body heuristic → other).
- **artifacts[].code_area** and **feature_deltas[].area** are now **populated**
  (were null in Phase 3a) when the path resolves to a `code_graph` area; null otherwise.
- **trains[].code_areas** — the distinct areas of a train's commits' files.
- **modules** `{ "<area>": { "commits", "prs", "files_changed" } }` — per-area
  activity counts across the window's commits.
- **people[].modules / people[].areas** — the areas a person authored (their
  commits' files) or reviewed (their reviewed PRs' areas).
- **diagrams{}** now also maps `contributor_graph` (Mermaid `flowchart`,
  people↔code-area edges) and `kind_breakdown` (Mermaid `pie`, issues by kind).
- **Phase 3d (shipped):** symbol/comment artifacts + `before`/`after`/`detail` on
  feature_deltas (see `symbol_events` above), diff-local from a `git log -p` walk.
- **Still deferred:** symbol-identity tracking across renames/moves (3e), resource-level
  `dependsOn` edges, multi-repo aggregation.

## Phase 4a fields (train significance + effort + forecast + per-train slice)

- **trains[]** gain `significance`, `tier`, and `effort` (see top-level `trains` bullet
  above for the exact shapes and tunables).
- **forecast** — top-level `forecast` block (see Phase 2 fields above).
- **diagrams.train_flowcharts** — now LIVE: the per-deep-train `.mmd` map (see Phase 2
  fields above).
- **`slice_train(bundle, train_id)`** — a pure, read-only, bounded helper (not a bundle
  field; called by the report ranker and future sub-agents). Returns a self-contained dict:

  ```
  {
    "train":          { id, kind, outcome, significance, tier, effort,
                        code_areas, evidence },
    "issue":          { number, title, body*, url, labels, kind,
                        comments*:[body*], comments_overflow } | None,
    "prs":            [ { number, title, body*, state, merged, created_at,
                          merged_at, url, reviewers:[login], review_decision,
                          review_comments*:[body*], review_comments_overflow,
                          comments*:[body*], comments_overflow } ],
    "commits":        [ { sha, message*, author, date } ],
    "feature_deltas": [ ...this train's deltas only... ],
    "symbol_moves":   [ ...moves whose from/to artifact is in this train's deltas... ]
  }
  ```

  (`*` = text-capped: any body/message/comment body truncated to `SLICE_TEXT_CAP` = 1500
  chars with a `…[+N chars]` marker; each comment list capped at `SLICE_COMMENTS_KEPT` = 6
  bodies, overflow count in `<key>_overflow`.) Raises `KeyError` on unknown id. Does not
  mutate the bundle.
