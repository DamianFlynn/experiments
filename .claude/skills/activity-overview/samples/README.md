# Sample: AVM-TF constellation project digest

A worked, end-to-end **multi-repo project digest** over a real Azure
Verified Modules — Terraform constellation. It exercises the deterministic
vertical: `gather (manifest) → store → validate → digest`.

> **The product is the JSON bundle.** Per the design spec's core principle —
> *"Gather is deterministic and decoupled. Analysis is the model's judgment"* — the
> pipeline emits a verifiable, fully-sourced **project view (JSON)**; a downstream
> content agent consumes that bundle to author the narrative. The pipeline never
> invents facts, and the narrative is **not** baked into the pipeline.

- **[`digest_view.json`](digest_view.json)** — **the product.** The project view
  emitted by `digest.py` (the bundle a content agent reads). Keys: `meta`,
  `members[].bundle` (per-member raw + enriched), `trains`, `module_edges`,
  `shipped`, `people`, `modules`, `related_work`. The committed file predates two
  later additions a fresh `digest.py` run also emits: top-level `halls` (#88) and
  `meta.schema_version`; its member bundles also lack `meta.period` / `ref_date` /
  `base_branch` / `branches`, its project trains' `code_areas` are empty (filled from the
  member trains since #112), and its member trains' `effort` blocks predate
  `idle_days` / `last_activity_at` / `slow_to_land` (#112; their `stalled` used the old
  "took more than 21 days to merge" rule).
- **[`avm-tf-aiml-lz-digest.md`](avm-tf-aiml-lz-digest.md)** — **illustration only.**
  A deterministic *structural* rendering of the bundle (metadata, summary counts,
  tables, and the validated `.mmd` diagrams — no authored prose), so a reader can
  eyeball the bundle's shape. The narrative a finished report carries is the
  downstream agent's judgment, authored from the bundle — not in this file.
- **[`build_report.py`](build_report.py)** — the structural renderer that produces
  that skeleton from a project view: `--view VIEW.json --out REPORT.md
  [--diagrams-dir DIR] [--skip-validate]`. It renders every diagram it embeds with
  `render.py` (per member, plus the project module graph from
  `view["module_edges"]`) into `DIR` (default `diagrams/` next to `--out`) and
  checks them with `mmdc` unless `--skip-validate`. The report depends only on the
  view (no wall-clock date, no paths), so it is byte-stable. **No narrative.**
- **[`manifest.json`](manifest.json)** — the project manifest used for the gather.
- **[`stories/`](stories/README.md)** — two sample stories (a single-repo view of
  `Azure/bicep-registry-modules`, September 2026) written from story slices with the
  story brief, each passing `link.py --check-story`.
- **[`newsletter/`](newsletter/README.md)** — a sample release-notes style issue
  ("Azure Verified Modules for Bicep — October 2026") written to
  `reference/newsletter.md` from a live October 2026 view, passing
  `newsletter.py --check-newsletter`.

## Constellation

| Repo | Role |
|---|---|
| `Azure/terraform-azurerm-avm-ptn-aiml-landing-zone` | pattern (consumer) |
| `Azure/terraform-azurerm-avm-res-network-virtualnetwork` | resource module |
| `Azure/terraform-azurerm-avm-res-operationalinsights-workspace` | resource module |

What the bundle contains for the window `2026-03-16 → 2026-03-22` (facts, not
interpretation): **6** module-dependency edges — **4 cross-repo** (pattern root
`main.tf` and `modules/example_hub_vnet` → vnet `0.16.0` and operationalinsights
`0.4.2`, all direct) + **2 intra-repo** (vnet → subnet, peering); **21** decision
trains (**0** cross-repo); **0** `related_work` clusters; **44** shipped items.
The gather's `meta.boundary_dropped_commits` was empty for all members (see "Note
on completeness"); that key was not part of a view when this sample was made (it is
since #107, when non-empty).

**`transitive` flags re-derived for #80, not re-gathered.** The live gather marked
14 Terraform edges `transitive: true` (including both operationalinsights
`module_edges`) only because no root resource consumed that module's outputs. Since
#80 every `module {}` block an area declares is a direct dependency, and every edge
in this sample came from such a block (each carries the block's own `ref` and
`version`), so those 14 flags were set to `false` in `digest_view.json` and the two
`Transitive` cells in the digest table changed to `no`. Nothing else changed: the
edge and cross-repo counts are the same. A fresh gather would also add the nested
`module.a.module.b` calls that `terraform graph` shows, as `transitive: true` edges
with `address`/`via`. Those cannot be re-derived without re-running terraform.

Each `members[].bundle.meta` carries `owner` = the project scope
(`avm-tf-aiml-lz`) and `repo` = the `owner/name` key, plus the identity keys
`digest.py` appends (#74): `project`, `slug` (e.g.
`Azure/terraform-azurerm-avm-res-network-virtualnetwork`), `repo_owner`,
`repo_name` — build GitHub links from `slug`. (Those four keys were added to the
committed file by applying `digest._member_identity` to each member rather than by
re-gathering; they depend only on the project and member names.)

## How it was produced

Requires `git`, `terraform` (1.15.5 used here), a read-only `GITHUB_TOKEN` (`public_repo` is enough for
these public repositories) for the gather, and `mmdc` (`@mermaid-js/mermaid-cli`) for diagram validation.

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

# 3. Materialize the project view — THE PRODUCT (JSON a content agent consumes).
python3 digest.py --store workspace/journey.db --project avm-tf-aiml-lz \
    --from 2026-03-16 --to 2026-03-22 > workspace/digest_view.json

# 4. (Illustration) render the structural skeleton from that bundle. This does NOT
#    author narrative — it renders each member's diagrams into
#    workspace/diagrams/<owner>__<name>/ and the project module graph into
#    workspace/diagrams/project_module_graph.mmd, validates them with mmdc, and
#    assembles tables + diagrams for eyeballing. Add --skip-validate without mmdc.
python3 samples/build_report.py --view workspace/digest_view.json \
    --out workspace/avm-tf-aiml-lz-digest.md --diagrams-dir workspace/diagrams
```

The committed [`avm-tf-aiml-lz-digest.md`](avm-tf-aiml-lz-digest.md) is step 4 run
over the committed view, offline:

```bash
python3 samples/build_report.py --view samples/digest_view.json \
    --out samples/avm-tf-aiml-lz-digest.md --diagrams-dir workspace/diagrams
```

`test_build_report.py` checks that the committed report is exactly that output.
The committed view predates `meta.ref_date`, so the report's reference date is the
window end. Its `code_owners` holds only a `.github/CODEOWNERS` rule (older gather,
legacy dict form), so under GitHub's last-match rule no module area has an owner
and the CODEOWNERS column is `—`. This committed view
carries no `meta.boundary_dropped_commits` (it predates #107, which stores the key and
puts it in views when non-empty), so the report warns only when a member's meta carries
it and never claims completeness from its absence.

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
complete across the constellation.

Since #68 gather deepens the clone itself when an in-window commit lands on the
shallow boundary (`git fetch --deepen`, bounded), so the default margin should no
longer leave `boundary_dropped_commits` non-empty; the wider margin above is kept
as recorded for this sample. If deepening ever fails, the dropped commit's `files`
are cleared too, so it no longer makes every module look changed.

### Module-dependency graph: structural, not window-scoped

The dependency graph is **structural** — `gather` statically parses every module's
`source` references across the whole tracked tree (`scan_structural_terraform_areas`,
no `terraform init`) on multi-repo gathers, so the graph reflects the repo's actual
module structure regardless of which areas changed in-window.

This was a real correction. The DOT-based extraction (`terraform graph`) only ran
over module areas the directory provider saw, i.e. in-window-CHANGED dirs. At the
default clone margin, the dropped boundary commits' phantom whole-tree diffs made
*every* directory look changed, so the graph looked complete **by accident**.
Closing the boundary gap removed that accident and the graph collapsed to only the
in-window-built areas (2 edges) — honest but thin. The static whole-tree scan
restores the full graph (6 edges: 4 cross-repo + 2 intra-repo) **honestly**,
independent of churn. Single-repo gathers keep the original window-scoped behaviour
(byte-stable golden bundle) unless given `--structural-iac`; manifest projects always
get the scan, which since #92 covers Bicep modules (`<area>/main.bicep`) as well.
