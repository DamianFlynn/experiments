# activity-overview — Reference

## Install

Copy `.claude/skills/activity-overview/` into your repo (or `~/.claude/skills/`).
Requirements: Python 3 (stdlib only), `git` on PATH, and a `GITHUB_TOKEN`
(or `GH_TOKEN`) with `repo` + `read:project` scope.

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
