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

```bash
python3 gather.py --owner OWNER --repo REPO --from 2026-05-01 --to 2026-05-31 \
    --out workspace/bundle.json
python3 link.py workspace/bundle.json
```

Then render with the skill (see `SKILL.md`).

## Tests

```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` >= 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
