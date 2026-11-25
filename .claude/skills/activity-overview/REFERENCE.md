# activity-overview — Reference

> Design and operations docs: [`docs/HLD.md`](docs/HLD.md) (high-level design + scenarios),
> [`docs/LLD.md`](docs/LLD.md) (L400 low-level design), [`docs/USAGE.md`](docs/USAGE.md)
> (operator runbook: monthly progressive updates, forecasting, per-estate playbooks).

## Install

Copy `.claude/skills/activity-overview/` into your repo (or `~/.claude/skills/`).
Requirements: Python 3 (stdlib only), `git` on PATH, and a `GITHUB_TOKEN`
(or `GH_TOKEN`) with `repo` scope (read access). Add **`read:project`** to ingest
Projects v2 boards (gather auto-discovers them); without it — or with
`--no-project-board` — the board layer cleanly degrades to empty and everything
else is unaffected.

### Token notes

For public targets a classic PAT with just the `public_repo` scope is enough.
Mind two org/enterprise policies that surface as a `403` on the first API call
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

**The store persists and grows.** `--store workspace/journey.db` is a normal SQLite
file on disk — reuse the **same path** for a project across windows (and across its
member repos) and gather folds into it **idempotently** (re-running an overlapping
window never double-counts), so the journey graph accretes over time. It's a local,
rebuildable cache; delete the file to start fresh, or re-gather to refresh against the
pinned clone SHA (see `STORE.md`). A single store *can* hold **multiple** projects/repos,
but then the readers need to be told which: `validate.py` (and `view.py`/`digest.py`/
`spotlight.py`) error asking for `--project` (and `--repo` when a project spans several;
`spotlight.py` instead unions an area across members unless `--repo` or `owner/repo::area`
names one) —
the bare `validate.py workspace/journey.db` example above assumes a single-project store.
`validate.py` opens the store read-only and exits 0 (trusted), 1 (a check failed; a
mistyped `--project`/`--repo` or an empty store fails the `target` check) or 2 (usage
error: missing or non-store path, ambiguous store).

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

`view.py --store DB [--project P] [--repo R] --from F --to T --out PATH [--ref-date D]` auto-detects
`--project`/`--repo` when the store holds exactly one (single-repo gather: project =
owner, repo = name; manifest: project = manifest name, repo = `owner/repo`), writes
exactly what `extract.extract` returns (raw keys, extract's deterministic order) — with
`meta.period` = `{F, T}` (the window `link.py` buckets to) and `meta.ref_date` = `D`, else
gather's `--ref-date` stored for exactly this window, else `T` — does
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
```

Manifest: `{ "project", "window": {"from","to"}, "repos": [ {"owner","repo",
"registry"?} ], "ticket_prefixes"? }` — `ticket_prefixes` (e.g. `["ABC"]`) is
optional and only read by `digest.py --manifest` to turn on `related_work` ticket
clustering (off by default). `registry` is an optional exact Terraform registry path; absent,
a member is matched by the HashiCorp naming convention
(`namespace/name/provider` → `{namespace}/terraform-{provider}-{name}`).
`load_manifest` rejects (with `ValueError`): non-string fields; `/` or `#` in
`project`; dates that are not `YYYY-MM-DD` or an inverted window; owner/repo names
GitHub would not accept; duplicate members (case-insensitive) and a `registry`
declared twice. The full rule list is in `STORE.md` § Multi-repo project; the same
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
`meta.boundary_dropped_commits`, with its diffs dropped **and** its `files` cleared
(no whole-tree inflation of areas/modules); widen this knob and re-gather to recover it.

An existing `--clone-dir` that is a clone of the same repo is **fetched** to that bound
(and the default branch head checked out) instead of re-cloned; `--no-clone` never creates
a clone (it fetches an existing one, or gathers API data only when there is none). A repo
with no commits since the bound is cloned at `--depth=1` and contributes no commits.
`meta.clone_mode` records `fresh` / `fetched` / `depth1-fallback` / `none`.

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

The CI `spotlight` job's gate can also be run on any store:

```bash
python3 ci_spotlight_integration.py --store workspace/journey.db [--project P] [--require-all]
```

It discovers a person, an area, an artifact and a search term inside the project and checks
the four spotlight answers against the store (see `docs/LLD.md` §15). Exit 0 (all verified,
or skipped for lack of input), 1 (a check failed, or anything skipped with
`--require-all`), 2 (usage error).

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` >= 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
