# activity-overview — Reference

## Install

Copy `.claude/skills/activity-overview/` into your repo (or `~/.claude/skills/`).
Requirements: Python 3 (stdlib only), `git` on PATH, and a `GITHUB_TOKEN`
(or `GH_TOKEN`) with `repo` scope (read access). `read:project` is only required
once Projects v2 support lands in a later phase.

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

## Tests

```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` >= 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
