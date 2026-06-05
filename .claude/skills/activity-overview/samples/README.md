# Sample: AVM-TF constellation project digest

A worked, end-to-end **multi-repo (Phase 9) project digest** rendered from the
journey-graph store over a real Azure Verified Modules — Terraform constellation.
This is the proof that the full project vertical composes:
`gather (manifest) → store → digest → render → report`.

- **[`avm-tf-aiml-lz-digest.md`](avm-tf-aiml-lz-digest.md)** — the finished
  Markdown digest (the deliverable). Every fact resolves to a store value or a
  GitHub URL; all 22 embedded Mermaid diagrams compile under `mmdc`.
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
pattern's root `main.tf` and its `modules/example_hub_vnet` both pin the vnet
module at `0.16.0` and pull operationalinsights `0.4.2` transitively — 4 cross-repo
edges, plus 2 intra-repo vnet sub-module edges), but **not by shared work** — zero
cross-repo decision trains and zero ticket-linked clusters this window.

## How it was produced

Window `2026-03-16 → 2026-03-22`. Requires `git`, `terraform` (1.15.5 used here),
a `GITHUB_TOKEN` with `repo` scope for the gather, and `mmdc`
(`@mermaid-js/mermaid-cli`) for diagram validation.

```bash
cd .claude/skills/activity-overview

# 1. Acquire → store (manifest = multi-repo project). Heavy: a real AVM pattern
#    pulls dozens of registry modules, so warm a shared plugin cache. The wider
#    clone margin keeps in-window commits off the shallow boundary (no dropped
#    diffs — see "Note on completeness" below).
export TF_PLUGIN_CACHE_DIR=$PWD/workspace/.tfcache && mkdir -p "$TF_PLUGIN_CACHE_DIR"
export ACTIVITY_CLONE_MARGIN_DAYS=120
python3 gather.py --manifest samples/manifest.json --store workspace/journey.db

# 2. Validate the project graph (trust gate; adds a project-wide people check).
python3 validate.py workspace/journey.db --project avm-tf-aiml-lz

# 3. Materialize the project view (cross-repo trains, merged shipped/people/
#    modules, module-dependency edges).
python3 digest.py --store workspace/journey.db --project avm-tf-aiml-lz \
    --from 2026-03-16 --to 2026-03-22 > workspace/digest_view.json

# 4. Render per-member + project diagrams, then fill the report.
#    build_report.py renders each member's bundle into workspace/diagrams/<repo>/
#    and emits the project module graph, then composes report-template.md.
python3 samples/build_report.py   # → samples/avm-tf-aiml-lz-digest.md
```

### mmdc as root

Prefer running `mmdc` as a **non-root user**, where Chromium's sandbox works
normally and no flags are needed. Only when you cannot (e.g. a root-only CI
container) does Chromium refuse to launch without `--no-sandbox` — disabling the
sandbox reduces isolation, so scope it to controlled, throwaway environments and
never point it at untrusted `.mmd` input. In that case pass a puppeteer config:

```bash
printf '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > workspace/puppeteer.json
mmdc -p workspace/puppeteer.json -i <diagram>.mmd -o /tmp/out.svg -q
```

## Note on completeness

At the default margin (`ACTIVITY_CLONE_MARGIN_DAYS=14`) one in-window commit per
member sat at the shallow-clone boundary, so its whole-tree phantom diff was
dropped (`meta.boundary_dropped_commits`) — leaving the resource modules' code
ledgers empty. Re-gathering with `ACTIVITY_CLONE_MARGIN_DAYS=120` keeps every
in-window commit's real parent in the clone, so `boundary_dropped_commits` is
empty for all three members and the feature-change / content-lifecycle ledgers are
complete across the constellation (the report renders a ✅ completeness line).

### Module-dependency graph: structural, not window-scoped

The dependency graph is **structural** — `gather` statically parses every module's
`source` references across the whole tracked tree (`scan_structural_terraform_areas`,
no `terraform init`) on multi-repo gathers, so the blast-radius graph reflects the
repo's actual module structure regardless of which areas changed in-window.

This was a real correction. The DOT-based extraction (`terraform graph`) only ran
over module areas the directory provider saw, i.e. in-window-CHANGED dirs. At the
default clone margin, the dropped boundary commits' phantom whole-tree diffs made
*every* directory look changed, so the graph looked complete **by accident**.
Closing the boundary gap removed that accident and the graph collapsed to only the
in-window-built areas (2 edges) — honest but thin. The static whole-tree scan
restores the full graph (6 edges: 4 cross-repo + 2 intra-repo) **honestly**,
independent of churn. Single-repo gathers keep the original window-scoped behaviour
(byte-stable golden bundle); the structural scan is gated to manifest projects.
