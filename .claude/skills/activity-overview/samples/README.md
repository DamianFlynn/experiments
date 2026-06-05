# Sample: AVM-TF constellation project digest

A worked, end-to-end **multi-repo (Phase 9) project digest** rendered from the
journey-graph store over a real Azure Verified Modules — Terraform constellation.
This is the proof that the full project vertical composes:
`gather (manifest) → store → digest → render → report`.

- **[`avm-tf-aiml-lz-digest.md`](avm-tf-aiml-lz-digest.md)** — the finished
  Markdown digest (the deliverable). Every fact resolves to a store value or a
  GitHub URL; all 18 embedded Mermaid diagrams compile under `mmdc`.
- **[`manifest.json`](manifest.json)** — the project manifest used for the gather.
- **[`build_report.py`](build_report.py)** — the renderer that composes
  `digest.py`'s project view + the validated `.mmd` diagrams into the report
  (fills `../report-template.md`). Deterministic; re-running is byte-stable.

## Constellation

| Repo | Role |
|---|---|
| `Azure/terraform-azurerm-avm-ptn-aiml-landing-zone` | pattern (consumer) |
| `Azure/terraform-azurerm-avm-res-network-virtualnetwork` | resource module |
| `Azure/terraform-azurerm-avm-res-operationalinsights-workspace` | resource module |

The headline finding: the repos are coupled by **module dependencies** (the
pattern pins the vnet module at `0.16.0` and pulls operationalinsights `0.4.2`
transitively — 4 cross-repo edges), but **not by shared work** — zero cross-repo
decision trains and zero ticket-linked clusters this window.

## How it was produced

Window `2026-03-16 → 2026-03-22`. Requires `git`, `terraform` (1.15.5 used here),
a `GITHUB_TOKEN` with `repo` scope for the gather, and `mmdc`
(`@mermaid-js/mermaid-cli`) for diagram validation.

```bash
cd .claude/skills/activity-overview

# 1. Acquire → store (manifest = multi-repo project). Heavy: a real AVM pattern
#    pulls dozens of registry modules, so warm a shared plugin cache.
export TF_PLUGIN_CACHE_DIR=$PWD/workspace/.tfcache && mkdir -p "$TF_PLUGIN_CACHE_DIR"
python3 gather.py --manifest samples/manifest.json --store workspace/journey_live.db

# 2. Validate the project graph (trust gate; adds a project-wide people check).
python3 validate.py workspace/journey_live.db --project avm-tf-aiml-lz

# 3. Materialize the project view (cross-repo trains, merged shipped/people/
#    modules, module-dependency edges).
python3 digest.py --store workspace/journey_live.db --project avm-tf-aiml-lz \
    --from 2026-03-16 --to 2026-03-22 > workspace/digest_view.json

# 4. Render per-member + project diagrams, then fill the report.
#    build_report.py renders each member's bundle into workspace/diagrams/<repo>/
#    and emits the project module graph, then composes report-template.md.
python3 samples/build_report.py   # → samples/avm-tf-aiml-lz-digest.md
```

### mmdc as root

`mmdc` launches headless Chromium, which refuses to run as root without
`--no-sandbox`. Pass a puppeteer config when validating diagrams:

```bash
printf '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > workspace/puppeteer.json
mmdc -p workspace/puppeteer.json -i <diagram>.mmd -o /tmp/out.svg -q
```

## Note on completeness

One in-window commit per member sat at the shallow-clone boundary, so its
whole-tree phantom diff was dropped (visible in `meta.boundary_dropped_commits`).
Code-level ledgers (feature changes / content lifecycle) are therefore complete
only for the pattern repo; the two resource modules' in-window merges were
`pre-commit` chores whose diffs fell on that boundary. Widen `CLONE_MARGIN_DAYS`
to recover them.
