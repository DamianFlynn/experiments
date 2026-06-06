# activity-overview — Reference

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

- **PAT lifetime caps.** Some enterprises reject classic PATs whose lifetime is
  too long. Notably **Microsoft Open Source** (which owns `Azure/*`) forbids any
  classic PAT with a lifetime **> 90 days** — a "no expiration" token is refused.
  Use an expiry of 90 days or fewer, and rotate it before it lapses.
- **SAML SSO.** If the org enforces SAML SSO, authorize the token for that org
  (token settings → *Configure SSO*) or the call 403s with an `x-github-sso`
  header.

## Usage

Phase 7 is **store-only**: `gather` folds facts into the SQLite journey-graph
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
but then the readers need to be told which: `validate.py` (and `digest.py`/`spotlight.py`)
error asking for `--project` (and `--repo` when a project spans several) — the bare
`validate.py workspace/journey.db` example above assumes a single-project store.

The report vertical (`extract → link → render → report`) **composes from the
store** — `extract` materializes the bundle as a transient view, then `link`/
`render` consume it (guarded by the golden-bundle equivalence gate). Render with
the skill (see `SKILL.md`).

### Multi-repo project (Phase 9)

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
"registry"?} ] }` — `registry` is an optional exact Terraform registry path; absent,
a member is matched by the HashiCorp naming convention
(`namespace/name/provider` → `{namespace}/terraform-{provider}-{name}`).

### IaC build env knobs (heavy live runs)

`terraform`/`bicep` edge extraction runs in parallel. For a real AVM Terraform
constellation (each `terraform init` pulls many registry modules), set a shared
`TF_PLUGIN_CACHE_DIR` (gather warms it once before the parallel inits). Override the
defaults when needed: `ACTIVITY_IAC_BUILD_TIMEOUT` (seconds/subprocess, default
`300`), `ACTIVITY_IAC_MAX_WORKERS` (`8`), `ACTIVITY_IAC_RETRIES` (`1`).

The shallow clone reaches `ACTIVITY_CLONE_MARGIN_DAYS` (default `14`) before the
window so an in-window commit keeps its real parent. If `meta.boundary_dropped_commits`
is non-empty (an in-window commit still landed on the shallow boundary, so its
whole-tree phantom diff was dropped), widen this knob and re-gather to recover it.

### Projects v2 board knobs

When a repo links Projects v2 boards, gather ingests **every maintained** board it
links (merging items; per-repo `(slug, number)` keying, so no foreign-repo status
leaks) and skips **closed/stale** ones. Override the defaults when needed:
`ACTIVITY_BOARD_STALE_DAYS` (a board untouched longer than this before the window's
ref date is treated as deprecated, default `365`) and `ACTIVITY_BOARD_MAX_ITEMS`
(per-board item cap, default `5000`; a truncated board warns). `--no-project-board`
skips the layer entirely (e.g. a token without `read:project`).

### Community-call transcript (Phase 14)

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

Then: `/activity <owner/repo|project> <from> <to> [--transcript PATH] [--series PATH]
[options]` — it parses the args and defers to `SKILL.md` (gather → link → render →
report).

## Tests

```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` >= 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
