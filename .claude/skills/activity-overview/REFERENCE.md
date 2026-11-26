# activity-overview — Reference

> Design and operations docs: [`docs/HLD.md`](docs/HLD.md) (high-level design + scenarios),
> [`docs/LLD.md`](docs/LLD.md) (L400 low-level design), [`docs/USAGE.md`](docs/USAGE.md)
> (operator runbook: monthly progressive updates, forecasting, per-estate playbooks).

## Install

Copy `.claude/skills/activity-overview/` into your repo (or `~/.claude/skills/`; the
symlinked install is in `docs/USAGE.md` §2). Requirements: Python 3.11+ (stdlib only),
`git` on PATH, and a `GITHUB_TOKEN`
(or `GH_TOKEN`): a classic PAT with `public_repo` for public targets or `repo` for
private ones. The skill only reads from GitHub (REST GETs, GraphQL queries, a git
clone/fetch); it never writes. Add **`read:project`** to ingest
Projects v2 boards (gather auto-discovers them); without it — or with
`--no-project-board` — the board layer cleanly degrades to empty and everything
else is unaffected.

### Token notes

`public_repo` is enough for public targets; `repo` is only needed for private ones
(`docs/USAGE.md` §1.1). Mind two org/enterprise policies that surface as a `403` on the first API call
(gather now prints GitHub's own message and the deciding headers, so the cause
is named rather than guessed):

- **PAT lifetime caps.** Some enterprises reject personal access tokens whose
  lifetime is too long. Notably **Microsoft Open Source** (which owns `Azure/*`)
  refuses any personal access token — **classic or fine-grained** — with a
  lifetime **over 8 days** (observed 2026-11; the limit was previously longer, so
  read the 403 message, which states the current figure). For a laptop run, create
  a token with an expiry of 7 days or less when you need one.
- **CI needs no personal token for public targets.** The bundled workflow's live
  jobs use the workflow's built-in token by default (`token_source: builtin`),
  which is not a personal token and is accepted for public `Azure/*` repos. Set
  `token_source: secret` (and the `ACTIVITY_TEST_TOKEN` secret) only for private
  targets.
- **SAML SSO.** If the org enforces SAML SSO, authorize the token for that org
  (token settings → *Configure SSO*) or the call 403s with an `x-github-sso`
  header.

## Usage

The skill is **store-only**: `gather` folds facts into the SQLite journey-graph
store (the sole deliverable; see `STORE.md`) and writes no bundle file. Audit the
store with `validate.py` (self-contained — no bundle needed):

```bash
python3 gather.py --owner OWNER --repo REPO --from 2026-05-01 --to 2026-05-31 \
    --store workspace/journey.db
python3 validate.py workspace/journey.db
```

**The store persists and can grow.** `--store workspace/journey.db` is a normal SQLite
file on disk. gather folds into it **idempotently** (re-running an overlapping window
never double-counts), so you can either start a new store per report installment (the
simplest, fully exact choice) or reuse the **same path** across windows and let it grow:
views are window-scoped, with the limits `docs/USAGE.md` §2.1 lists (a past month
re-viewed later shows today's record state). It's a local, rebuildable file; delete it to
start fresh, or re-gather a window to refresh it (its structure is read as of the
window's end; `STORE.md`). A single store *can* hold **multiple** projects/repos,
but then the readers need to be told which: `validate.py` (and `view.py`/`digest.py`/
`spotlight.py`) error asking for `--project` (and `--repo` when a project spans several;
`spotlight.py` instead unions an area across members unless `--repo` or `owner/repo::area`
names one; `dependents`, `subsystem` and `module` take a repo as `owner/repo` in any case or
`repo` alone when unambiguous, on a single-repo or a project store, and exit 2 listing the
store's repos for one that names none) —
the bare `validate.py workspace/journey.db` example above assumes a single-project store.
`validate.py` opens the store read-only and exits 0 (trusted), 1 (a check failed; a
mistyped `--project`/`--repo` or an empty store fails the `target` check) or 2 (usage
error: missing or non-store path, ambiguous store, unreadable `--bundle`). It lists ten
checks per repo in this order — `target`, `referential_integrity`,
`participant_completeness`, `provenance`, `no_fabrication`, `schema_conformance`,
`meta_integrity`, `edge_set`, `no_drift`, `idempotency` — plus `no_drift_people` and
`member_status` for a multi-repo project. It proves the store is consistent
with its own records — every edge is one the stored records imply (`edge_set`, both
directions; cross-repo edges with the member set and registries the fold recorded),
people and artifacts are re-derived from the records and ledger, every PR/issue cites its
own GitHub link and has a contributor, every commit id is a real sha, and the store's
markers and per-window meta are what folds write (`meta_integrity`) — but not that the
records match GitHub: a record deleted with its edges, a consistently rewritten blob, an
inserted record that is internally consistent (a PR with its author edge, a dated
release, a milestone, an area), a fact value rewritten with its key, or a plausible
changed base branch / reference date passes. To catch those for a window, save the
view of the freshly gathered store (`view.py … --out view.json`, before `link.py` enriches it) and later run
`validate.py STORE --bundle view.json` (single repo only; a project-mode run ignores
`--bundle` with a warning). On a store holding several windows, the
dependency, ownership, touch, milestone, sprint and blocker edges are checked exactly
once a fold since #95 maintained them and every window has its per-window facts
(otherwise, and for person areas, a difference is a WARN naming why); `--repo` of a multi-repo
project checks only that member's people (the full check runs without `--repo`). See
`STORE.md` "Trust gate".

**Stale edges are retracted on evidence.** Each fold keeps the current-state edges
(`depends_on`, `owns`, `in_milestone`, `in_iteration`, `blocks`) equal to what the store's
inputs imply and deletes the ones they no longer imply — a removed module reference, a
CODEOWNERS change, an item moved off a milestone or sprint, a blocker edited away — only
where the input speaks for that source (a resolved build or the whole-tree scan for a
dependency; never a window that simply does not mention it). A re-gathered PR / issue /
commit's closing references, commit → PR link and contribution edges are made what its new
content implies, and a person no contribution references any more is deleted (#111;
`STORE.md` "Record-derived edges"). `touches` and symbol moves are never deleted. `gather` prints what it retracted and keeps it in `meta.retractions:<project>/<repo>`
(`STORE.md` "Current-state edges").

**Whole-tree module graph (`--structural-iac`).** By default a single-repo gather builds
IaC edges only for the areas changed in the window, and only with the `bicep`/`terraform`
CLIs. `gather.py ... --structural-iac` also scans every tracked Terraform module dir and
Bicep `<area>/main.bicep` for its direct module references (static: no build, no CLI; one
read per module entrypoint), so modules nobody touched in the window still appear with
their edges — use it on a monorepo such as `Azure/bicep-registry-modules` before asking
`spotlight.py dependents owner/repo::<area>`. Manifest members always get the scan. The
flag adds areas and edges to the bundle (and `code_graph.structural_scan: true`), so a store
gathered with it differs from one gathered without it. On a store kept across months it is
also what lets a fold retract a dependency of a module nobody rebuilt that month.

The report vertical (`view → link → render → report`) **composes from the
store** — `view.py` (the CLI over `extract`) materializes one repo's raw bundle view,
then `link.py` enriches it in place and `render.py` adds `diagrams` (guarded by the
golden-bundle equivalence gate):

```bash
python3 view.py --store workspace/journey.db --from 2026-05-01 --to 2026-05-31 \
    --out workspace/view/bundle.json        # [--project OWNER --repo REPO]
python3 link.py workspace/view/bundle.json --series workspace/series.json
python3 render.py workspace/view/bundle.json --diagrams-dir workspace/diagrams
```

`view.py --store DB [--project P] [--repo R] --from F --to T --out PATH [--ref-date D] [--as-stored]` auto-detects
`--project`/`--repo` when the store holds exactly one (single-repo gather: project =
owner, repo = name; manifest: project = manifest name, repo = `owner/repo`), writes
exactly what `extract.extract` returns (raw keys, extract's deterministic order) — with
`meta.period` = `{F, T}` (the window `link.py` buckets to) and `meta.ref_date` = `D`, else
gather's `--ref-date` stored for exactly this window, else `T` — holds only the window's
records (on a store holding several windows; BUNDLE.md "Window scope", with
`meta.state_as_of` when the store also holds a later window; PR / issue records projected
to the window's end, `meta.records_as_of`, issue #97, unless `--as-stored`) with that window's CI
statistics and structure (`meta.facts_windows` when the window is not exactly a gathered
one; STORE.md "Per-window facts"), does
no network backfill, and exits 2 when the scope is ambiguous, unknown or empty, or the
store is missing. For a multi-repo project, `--repo` selects one member; the merged
project view comes from `digest.py`. The full step-by-step procedure (and which report
sections read which view) is in `SKILL.md`.

### Multi-repo project

Generate the manifest from the AVM module index (or hand-author it), then fold the
members under one logical project and validate/read the whole project:

```bash
# Generate a manifest from the canonical AVM index CSV(s). --avm res|ptn|utl pulls
# the published index; filter with --kind/--status/--name-contains/--include/
# --exclude/--limit. (--index FILE|URL|- reads a local/remote/stdin CSV instead.)
python3 manifest_from_index.py --avm res --avm ptn \
    --project avm-tf-storage --from 2026-03-01 --to 2026-03-31 \
    --name-contains storage --status Available > workspace/manifest.json

python3 gather.py --manifest workspace/manifest.json --store workspace/journey.db
python3 validate.py workspace/journey.db --project avm-tf-storage
python3 digest.py --store workspace/journey.db --project avm-tf-storage \
    --from 2026-03-01 --to 2026-03-31           # merged project view (JSON)
python3 spotlight.py dependents Azure/terraform-azurerm-avm-res-keyvault-vault \
    --store workspace/journey.db --project avm-tf-storage   # blast radius
python3 spotlight.py dependents Azure/bicep-registry-modules::avm/res/network/virtual-network \
    --store workspace/journey.db --project contoso-lz   # consumers of one module area
```

Manifest: `{ "project", "window": {"from","to"}, "repos": [ {"owner","repo",
"registry"?, "branch"?, "bicep_registry_prefix"?} ], "ticket_prefixes"? }` — `branch` (optional) is that member's
mainline when it is not the repo's default branch (it beats a global `--branches`;
absent, gather resolves each member's own default branch, so members with different
defaults are fine). `ticket_prefixes` (e.g. `["ABC"]`) is
optional and only read by `digest.py --manifest` to turn on `related_work` ticket
clustering (off by default). `registry` is an optional exact Terraform registry path; absent,
a member is matched by the HashiCorp naming convention
(`namespace/name/provider` → `{namespace}/terraform-{provider}-{name}`).
`bicep_registry_prefix` (e.g. `"avm/"` on `Azure/bicep-registry-modules`) is the
optional module path prefix a member publishes under in the **public** Bicep
registry: another member's `br/public:<path>:<tag>` (or
`br:mcr.microsoft.com/bicep/<path>:<tag>`) ref under it becomes a cross-repo
`depends_on` to that member's module area `<path>`. Private registries, template
specs and aliases are never resolved.
`load_manifest` rejects (with `ValueError`): non-string fields; `/` or `#` in
`project`; dates that are not `YYYY-MM-DD` or an inverted window; owner/repo names
GitHub would not accept; duplicate members (case-insensitive) and a `registry`
declared twice, a malformed `bicep_registry_prefix` (lowercase registry path
segments only, no `br/public:` scheme) or one declared twice. The full rule list is in `STORE.md` § Multi-repo project; the same
owner/repo/date checks apply to `gather.py --owner/--repo/--from/--to`.
`manifest_from_index.py` strips `.git`, `?query` and `#fragment` from `RepoURL`,
dedupes members case-insensitively, exits 2 on bad/inverted dates, `--limit < 1` or
a duplicate `registry` among the selected members, warns when an `--include` slug is
not in the index, and times out an index fetch after 30 s.

A member that fails to gather (HTTP error, clone failure, a fold error) does not stop
the run: the other members are folded, gather prints `member -> reason` and exits 1,
and the store records each member's outcome for the window in meta `member_status`
(`ok` / `failed` / `not_attempted`, `STORE.md`). `validate.py` then fails the project
(`member_status`, and `target` for that member) and `digest.py` / `view.py` warn on
stderr. `gather.py --manifest M --store S --resume` re-attempts only the members that
are not `ok` for the manifest's window; a plain re-run gathers every member again. A repo
that exists in the store only as another member's `depends_on` stand-in is not listed
as a member.

### IaC build env knobs (heavy live runs)

`terraform`/`bicep` edge extraction runs in parallel. For a real AVM Terraform
constellation (each `terraform init` pulls many registry modules), set a shared
`TF_PLUGIN_CACHE_DIR` (gather warms it once before the parallel inits). Override the
defaults when needed: `ACTIVITY_IAC_BUILD_TIMEOUT` (seconds/subprocess, default
`300`), `ACTIVITY_IAC_MAX_WORKERS` (`8`), `ACTIVITY_IAC_RETRIES` (`1`).

The shallow clone reaches `ACTIVITY_CLONE_MARGIN_DAYS` (default `14`) before the
window so an in-window commit keeps its real parent. If the margin holds no earlier
commit (common on quiet per-module repos), the first in-window commit lands on the
shallow boundary; gather then deepens the clone (`git fetch --deepen`, at most four
bounded attempts) until that commit's parent is present, so its diff and file list
are real. Only if deepening fails is the commit recorded in
`meta.boundary_dropped_commits` (printed as a warning; it is not stored, so views do not
carry it), with its diffs dropped **and** its `files` cleared (no whole-tree inflation of
areas/modules); widen this knob and re-gather to recover it.

An existing `--clone-dir` that is a clone of the same repo is **fetched** to that bound
(and the base branch head checked out: the default, or `--branches`' first entry fetched
under the same bound) instead of re-cloned; `--no-clone` never creates
a clone (it fetches an existing one, or gathers API data only when there is none). A repo
with no commits since the bound is cloned at `--depth=1` and contributes no commits.
`meta.clone_mode` records `fresh` / `fetched` / `depth1-fallback` / `none`.
The clone is also partial (`--filter=blob:none`): file contents are fetched from GitHub
while the history walks run, so a gather needs the network for its whole run. A git
command that loses its connection is retried three times (2 s, 8 s, 30 s); if it still
fails the gather stops before folding anything — re-run the same window into the same
store.

### GitHub API knobs

Every GitHub request has a timeout and is retried with back-off on timeouts and 5xx;
on a rate limit gather waits for the reset (it prints how long and until when) and
continues. Override the defaults when needed: `ACTIVITY_HTTP_TIMEOUT` (seconds per
request, default `60`), `ACTIVITY_HTTP_RETRIES` (extra attempts, `3`) and
`ACTIVITY_RATE_LIMIT_MAX_WAIT` (longest single rate-limit wait in seconds, `3600`;
`0` fails instead of waiting). A 403 that is not a rate limit (SAML SSO, scope)
still fails at once. A non-integer value for an `ACTIVITY_HTTP_*`,
`ACTIVITY_RATE_LIMIT_MAX_WAIT` or `ACTIVITY_IAC_*` knob is an error; a value below
its minimum is clamped with a warning.

If a window has more than 1,000 workflow runs (GitHub's per-search cap) gather
splits the query by day; a single day still over the cap is reported on stderr and
marked in `workflow_stats._truncated`.

First-time contributors (`newcomers`, BUNDLE.md): one or two search API calls per author of
a PR / issue created in the window (the search API allows 30 requests a minute; a limit is
waited out like any other). `ACTIVITY_NEWCOMERS_MAX` (default `200`, min `0`) caps the
candidates checked (the rest are listed as `not_checked`); `--no-newcomers` skips the check.

### Projects v2 board knobs

When a repo links Projects v2 boards, gather ingests **every maintained** board it
links (merging items; per-repo `(slug, number)` keying, so no foreign-repo status
leaks) and skips **closed/stale** ones. Override the defaults when needed:
`ACTIVITY_BOARD_STALE_DAYS` (a board untouched longer than this before the window's
ref date is treated as deprecated, default `365`) and `ACTIVITY_BOARD_MAX_ITEMS`
(per-board item cap, default `5000`; a truncated board warns). `--no-project-board`
skips the layer entirely (e.g. a token without `read:project`).

### Community-call transcript

To fold a community call into the report, pass a **local** transcript file (no
network) and normalize it to clean prose:

```bash
python3 transcript.py workspace/<name>-call-2026-05.vtt   # .vtt/.srt/.txt/.md
```

The skill reads that output and authors the **Community call highlights** section,
grounded in (and quoting) the transcript. No transcript ⇒ the section is omitted.

### Slash command

The skill ships a thin `/activity` entrypoint at `commands/activity.md`. Claude Code
discovers slash commands under `.claude/commands/` (project) or `~/.claude/commands/`
(user), so install it by copying/symlinking that file there:

```bash
ln -s ../skills/activity-overview/commands/activity.md .claude/commands/activity.md
```

Then: `/activity <owner/repo> <from> <to> [options]` or `/activity --manifest PATH
[options]` (options: `--store PATH`, `--transcript PATH`, `--series PATH`, gather
flags) — it parses the args and defers to `SKILL.md` (gather → validate → view → link
→ render → slices → report).

## Tests

```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

The result must be `OK`; how many tests skip depends on the machine (`docs/USAGE.md` §2
says which conditions skip which tests); appending `2>&1 | tail -1` prints just the
verdict line.

The CI `spotlight` job's gate can also be run on any store:

```bash
python3 ci_spotlight_integration.py --store workspace/journey.db [--project P] [--require-all]
```

It discovers a person, an area, an artifact and a search term inside the project and checks
the four spotlight answers against the store, not against spotlight's own output: the
person's trains and roles against their contribution edges, every train outcome against one
computed from the stored records, every citation against its node, each lifecycle row
against its ledger row, each grep mention against the stored text (see `docs/LLD.md` §15). Exit 0 (all verified,
or skipped for lack of input), 1 (a check failed, or anything skipped with
`--require-all`), 2 (usage error).

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` >= 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
