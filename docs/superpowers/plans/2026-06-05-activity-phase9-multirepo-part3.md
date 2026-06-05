# Phase 9 Multi-Repo Project — Implementation Plan (Part 3: cross-repo Terraform `depends_on` + blast radius, S4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a project digest see **cross-repo Terraform dependencies**: when member A's `main.tf` declares `module "kv" { source = "Azure/avm-res-keyvault-vault/azurerm" }` and member B (`terraform-azurerm-avm-res-keyvault-vault`) is in the manifest, the store gains a cross-repo `depends_on` edge A-area → B-root-area; a project-wide module graph renders the blast radius; and a spotlight query answers "if B changes, who depends on it?" (→ A).

**Architecture:** Additive, keyed on the manifest as in Parts 1–2. `build_terraform_edges` stays **pure and byte-stable** — a registry source still yields `to=None, resolved=False, ref=<src>`. Both the **edge flattening** and the **cross-repo registry resolution** happen in `fold_bundle`'s `depends_on` step, where `project`/`members` already live (the `_cross_repo_pr_edges` pattern). The cross-repo target is the deterministic `area-main.tf` root area of the resolved member. The project-wide render + spotlight blast-radius read the resolved `depends_on` edges back from the store.

**Tech Stack:** Python 3 stdlib only (`re`, `os`, `json`, `sqlite3`, `argparse`), `unittest` under `pytest`. All work in `.claude/skills/activity-overview/`.

**Spec:** `docs/superpowers/specs/2026-06-05-activity-phase9-multirepo.md` (slice **S4**). Parts 1 (S1+S2) and 2 (S3) are landed on `master` (#15, #16).

**Scope of Part 3:** spec slice **S4** in full — including a **prerequisite latent-bug fix** (Slice 1 below). S5 (real-data trust gate + docs) remains Part 4.

---

## Prerequisite finding (verified) — `depends_on` edges never reach the store today

`extract_iac_edges` stamps inter-area dependency edges **per area** at `code_graph["areas"][i]["edges"]` (gather.py:1568-1569) and only an aggregate `code_graph["edge_extraction"]` summary at the top level. But `fold_bundle`'s `depends_on` step (gather.py:2359-2366) iterates a **top-level** `code_graph["edges"]` list that **nothing ever assembles** — so it is dead code, and a real gather inserts **zero** `depends_on` edges. Evidence:
- `render.emit_module_graph` reads `code_graph.areas[].edges` straight from the **bundle** (render.py:330) — that's why the single-repo module diagram works.
- `test_spotlight`'s blast-radius test comment: *"Seed a store directly (areas/owns/**depends_on are sparse via fold**, so we …)"* — it inserts `depends_on` edges **by hand**.

S4's gate ("blast-radius query from B returns A") reads `depends_on` edges **from the store**, so activating that insertion (Slice 1) is a prerequisite, not optional. Slice 1 fixes the flatten for the intra-repo case; Slices 2–3 extend the same flatten to resolve cross-repo registry sources.

---

## Key design decisions (locked before tasks)

1. **`build_terraform_edges` is untouched.** Registry sources keep `to=None, resolved=False, ref=<src>, version=<pin>` (gather.py:1133-1141). The single-repo digest path stays byte-stable; no `members`/registry threading reaches edge-build time.
2. **Resolution lives in `fold_bundle`.** The new `depends_on` flatten iterates `code_graph["areas"][i]["edges"]`. For a **resolved-local** edge (`resolved and to is not None`) it emits an intra-repo `depends_on`. For an **unresolved** edge carrying a registry `ref`, *and only when the fold is multi-repo* (`members` + `registry_by_slug` supplied), it attempts member resolution; on a hit it emits a **cross-repo** `depends_on` to the member's root area. Single-repo folds pass neither, so they emit only intra-repo edges (byte-stable behaviour: same edges that *should* have been there).
3. **Member resolution: exact then convention.** Exact = a member whose manifest `registry` equals the source. Convention = parse `[host/]namespace/name/provider` → expected slug `{namespace}/terraform-{provider}-{name}` (the HashiCorp registry publishing rule), match against `members`. Exact wins.
4. **Cross-repo target = the member's root area.** A repo-root `main.tf` classifies to area id `main.tf` (gather.py:748 `_tf_dir`, via `classify_code_area`), so the dst is `qualify_id(project, member, "area-main.tf")`. When the member is gathered, its own root area is the same node → the edge resolves; if it was not gathered, the dst is an honest `missing` structure node (the 8d contract).
5. **`depends_on` is a non-spine edge** (graphstore.py:17 `SPINE_EDGE_TYPES` excludes it), so `traverse_spine` ignores it. The blast-radius walk is a small directed BFS over `depends_on` in-edges (`get_edges(..., direction="in", edge_types=["depends_on"])`).
6. **Project-wide render/spotlight read the store**, not a single bundle — cross-repo edges live only in the store (and the merged digest view), never in one member's `code_graph`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.claude/skills/activity-overview/gather.py` | Rewrite the `fold_bundle` `depends_on` flatten (areas → store edges) + cross-repo registry resolution; thread `registry_by_slug`; producer builds the map. New pure helpers `parse_registry_source`, `resolve_registry_member`. | **Modify** |
| `.claude/skills/activity-overview/graphstore.py` | `edges_by_type(conn, edge_type, project)` — all edges of a type within a project. | **Modify** |
| `.claude/skills/activity-overview/digest.py` | `project_depends_on(conn, project, repos)` → project module-edge list; add `module_edges` to `build_project_view`. | **Modify** |
| `.claude/skills/activity-overview/render.py` | `emit_project_module_graph(module_edges)` — project-wide blast-radius flowchart (per-repo subgraphs, cross-repo edges marked). | **Modify** |
| `.claude/skills/activity-overview/spotlight.py` | `member_dependents(conn, project, member)` reverse-dependency query + a `dependents` subcommand + md renderer. | **Modify** |
| `.claude/skills/activity-overview/report-template.md` | A "Cross-repo module dependencies / blast radius" project section. | **Modify** |
| Tests: `test_gather.py`, `test_graphstore.py`, `test_digest.py`, `test_render.py`, `test_spotlight.py` | Coverage per slice. | **Modify** |

**Run convention (all tasks):** from the skill dir —
```bash
cd .claude/skills/activity-overview && python3 -m pytest <file> -k <name> -v
```
Full guard (single-repo digest path byte-stable): `python3 -m pytest -q`.

---

## Task 1 (Slice 1): flatten `area[].edges` → store `depends_on` (intra-repo)

Replace the dead `fold_bundle` block (gather.py:2359-2366) with a real flatten over `code_graph["areas"][i]["edges"]`. This activates intra-repo `depends_on` in the store and makes spotlight's blast-radius work on real data.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:2359-2366`
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestFoldDependsOnFlatten(unittest.TestCase):
    def test_resolved_area_edges_become_store_depends_on(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "modules/app", "label": "app", "paths": ["modules/app/main.tf"],
                 "edges": [{"to": "modules/base", "kind": "module", "ref": "../base",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]},
                {"id": "modules/base", "label": "base",
                 "paths": ["modules/base/main.tf"], "edges": []},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-modules/app",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["dst_id"], "p/Az/r#area-modules/base")
        self.assertEqual(deps[0]["data"].get("transitive"), False)

    def test_unresolved_registry_edge_dropped_in_single_repo(self):
        # single-repo fold (no members) ignores registry/external edges entirely.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                 "edges": [{"to": None, "kind": "module",
                            "ref": "Azure/avm-res-keyvault-vault/azurerm",
                            "version": "0.1.0", "transitive": False,
                            "provider": "terraform", "resolved": False}]},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(deps, [])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldDependsOnFlatten -v
```
Expected: `test_resolved_area_edges_become_store_depends_on` FAILS (no `depends_on` edge produced — dead code).

- [ ] **Step 3: Rewrite the flatten**

In `gather.py`, replace the block at 2359-2366:

```python
    # depends_on (area -> area): the code_graph dependency edges, carrying
    # {version,transitive,...} on the edge `data`.
    for e in (bundle.get("code_graph", {}) or {}).get("edges") or []:
        src, dst = e.get("from"), e.get("to")
        if src and dst:
            data = {k: v for k, v in e.items() if k not in ("from", "to")}
            edges.append((qid("area-{}".format(src)), qid("area-{}".format(dst)),
                          "depends_on", None, data or None))
```

with:

```python
    # depends_on (area -> area): flatten each area's resolved dependency edges
    # into store edges, carrying {version,transitive,ref,...} on the edge `data`.
    # Phase 3c stamps edges per area (code_graph.areas[].edges); there is no
    # top-level edges key. A resolved-local edge -> an intra-repo depends_on; an
    # unresolved registry edge resolves to a cross-repo member only in multi-repo
    # folds (see _fold_depends_on, threaded with members + registry_by_slug).
    for src, dst, data in _fold_depends_on(bundle, project, repo, members,
                                           registry_by_slug):
        edges.append((src, dst, "depends_on", None, data or None))
```

Then add the helper near `_cross_repo_pr_edges` (gather.py ~2117), referencing the local `qualify_id`:

```python
def _fold_depends_on(bundle, project, repo, members, registry_by_slug):
    """Yield (src_qid, dst_qid, data) depends_on triples from a bundle's area
    edges. Resolved-local edges -> intra-repo. Unresolved registry edges resolve
    to a member's root area ONLY when members + registry_by_slug are supplied
    (multi-repo); single-repo folds skip them (byte-stable). Pure."""
    def q(slug, local):
        return graphstore.qualify_id(project, slug, local)
    areas = (bundle.get("code_graph", {}) or {}).get("areas") or []
    for area in areas:
        src = q(repo, "area-{}".format(area["id"]))
        for e in area.get("edges") or []:
            to = e.get("to")
            if e.get("resolved") and to is not None:
                dst = q(repo, "area-{}".format(to))
                data = {k: v for k, v in e.items() if k != "to"}
            elif members and registry_by_slug and e.get("ref"):
                dst = resolve_registry_member(e["ref"], project, members,
                                              registry_by_slug)
                if dst is None:
                    continue
                data = {k: v for k, v in e.items() if k != "to"}
                data["resolved"] = True
                data["cross_repo"] = True
            else:
                continue
            yield src, dst, (data or None)
```

(`resolve_registry_member` is added in Task 2; for Task 1 the `elif` branch is never taken because `registry_by_slug` is `None` for these tests — but define a temporary stub `def resolve_registry_member(*a, **k): return None` now so the module imports, and replace it in Task 2. Add the stub immediately above `_fold_depends_on`.)

Also extend `fold_bundle`'s signature (gather.py:2146) to accept the new optional map (default `None`, single-repo byte-stable):

```python
def fold_bundle(conn, bundle, project=None, repo=None, members=None,
                registry_by_slug=None):
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldDependsOnFlatten -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Full-suite guard**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS. If a pre-existing `test_gather`/`test_spotlight` fixture set `area["edges"]` and asserted an exact total edge count, update that count to include the now-emitted `depends_on` edges (they were always *supposed* to be there). Do NOT weaken any assertion about edge *content*.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "fix(activity): flatten area edges into store depends_on (S4 prereq)"
```

---

## Task 2 (Slice 2): registry-source parsing + member resolution

Pure helpers: parse a Terraform registry source and resolve it to a member (exact `registry` match, then HashiCorp naming convention).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (replace the Task-1 stub)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestRegistryResolution(unittest.TestCase):
    def test_parse_registry_source_plain(self):
        self.assertEqual(
            gather.parse_registry_source("Azure/avm-res-keyvault-vault/azurerm"),
            ("Azure", "avm-res-keyvault-vault", "azurerm"))

    def test_parse_registry_source_with_host_and_submodule(self):
        self.assertEqual(
            gather.parse_registry_source(
                "registry.terraform.io/Azure/avm-res-keyvault-vault/azurerm//sub"),
            ("Azure", "avm-res-keyvault-vault", "azurerm"))

    def test_parse_registry_source_rejects_non_registry(self):
        self.assertIsNone(gather.parse_registry_source("./local"))
        self.assertIsNone(gather.parse_registry_source("two/parts"))

    def test_resolve_exact_registry_match_wins(self):
        members = {"Azure/kv-repo"}
        reg = {"Azure/kv-repo": "Azure/avm-res-keyvault-vault/azurerm"}
        dst = gather.resolve_registry_member(
            "Azure/avm-res-keyvault-vault/azurerm", "p", members, reg)
        self.assertEqual(dst, "p/Azure/kv-repo#area-main.tf")

    def test_resolve_convention_match(self):
        members = {"Azure/terraform-azurerm-avm-res-keyvault-vault"}
        dst = gather.resolve_registry_member(
            "Azure/avm-res-keyvault-vault/azurerm", "p", members, {})
        self.assertEqual(
            dst, "p/Azure/terraform-azurerm-avm-res-keyvault-vault#area-main.tf")

    def test_resolve_no_match_returns_none(self):
        self.assertIsNone(gather.resolve_registry_member(
            "Hashicorp/consul/aws", "p", {"Azure/other"}, {}))
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestRegistryResolution -v
```
Expected: FAIL — `module 'gather' has no attribute 'parse_registry_source'`.

- [ ] **Step 3: Implement (replace the Task-1 stub)**

Remove the temporary `resolve_registry_member` stub and add, near `_cross_repo_pr_edges`:

```python
# The member's root module area: a repo-root main.tf classifies to area "main.tf"
# (DEFAULT_AREA_PATTERNS / _tf_dir), so a cross-repo dep targets this node.
_ROOT_AREA_LOCAL = "area-{}".format(
    classify_code_area("main.tf", DEFAULT_AREA_PATTERNS))


def parse_registry_source(src):
    """Parse a Terraform registry module source `[host/]namespace/name/provider`
    (optional `//submodule` suffix) -> (namespace, name, provider), or None if it
    is not a registry source (local path, git/http url, or wrong shape). Pure."""
    if not src or src.startswith((".", "/")) or "://" in src:
        return None
    core = src.split("//", 1)[0]                       # drop submodule path
    parts = [p for p in core.split("/") if p]
    if len(parts) == 4 and "." in parts[0]:            # strip a registry host
        parts = parts[1:]
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def resolve_registry_member(src, project, members, registry_by_slug):
    """Resolve a registry source to a member's root-area qualified id, or None.
    Exact (manifest `registry` equals `src`) wins over the HashiCorp naming
    convention (`namespace/name/provider` -> `{namespace}/terraform-{provider}-{name}`).
    Pure."""
    for slug in sorted(members):                       # exact, deterministic
        if registry_by_slug.get(slug) == src:
            return graphstore.qualify_id(project, slug, _ROOT_AREA_LOCAL)
    parsed = parse_registry_source(src)
    if parsed is None:
        return None
    namespace, name, provider = parsed
    slug = "{}/terraform-{}-{}".format(namespace, provider, name)
    if slug in members:
        return graphstore.qualify_id(project, slug, _ROOT_AREA_LOCAL)
    return None
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestRegistryResolution -v
```
Expected: PASS (6 tests). Confirm `_ROOT_AREA_LOCAL == "area-main.tf"` (add a one-line assertion in the first test if you want to lock it).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): registry source parse + member resolution (S4)"
```

---

## Task 3 (Slice 3): emit cross-repo `depends_on` from fold + producer threading

Wire `registry_by_slug` from the manifest producer into `fold_bundle`, so a multi-repo fold emits the cross-repo edge. (`_fold_depends_on` already calls `resolve_registry_member`; this task proves the end-to-end fold path and threads the map.)

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (producer loop at 2451-2467)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestCrossRepoDependsOn(unittest.TestCase):
    def _member_a(self):
        return {"meta": {"owner": "Azure", "repo": "consumer", "from": "2026-01-01",
                         "to": "2026-01-31", "base_branch": "main"},
                "prs": [], "issues": [], "commits": [], "code_events": [],
                "milestones": [], "releases": [],
                "code_graph": {"provider": "directory", "areas": [
                    {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                     "edges": [{"to": None, "kind": "module",
                                "ref": "Azure/avm-res-keyvault-vault/azurerm",
                                "version": "0.1.0", "transitive": False,
                                "provider": "terraform", "resolved": False}]}]}}

    def test_fold_emits_cross_repo_depends_on_via_convention(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/consumer",
                   "Azure/terraform-azurerm-avm-res-keyvault-vault"}
        gather.fold_bundle(conn, self._member_a(), project="proj",
                           repo="Azure/consumer", members=members,
                           registry_by_slug={})
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertEqual(
            deps[0]["dst_id"],
            "proj/Azure/terraform-azurerm-avm-res-keyvault-vault#area-main.tf")
        self.assertEqual(deps[0]["data"].get("cross_repo"), True)
        self.assertEqual(deps[0]["data"].get("version"), "0.1.0")

    def test_fold_emits_cross_repo_depends_on_via_exact_registry(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/consumer", "Azure/kv"}
        reg = {"Azure/kv": "Azure/avm-res-keyvault-vault/azurerm"}
        gather.fold_bundle(conn, self._member_a(), project="proj",
                           repo="Azure/consumer", members=members,
                           registry_by_slug=reg)
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(deps[0]["dst_id"], "proj/Azure/kv#area-main.tf")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestCrossRepoDependsOn -v
```
Expected: PASS already if Tasks 1–2 are correct (the fold path is wired). If it FAILS, the `_fold_depends_on` multi-repo branch is mis-wired — fix it, do not weaken the test. (This task's real delta is the producer threading in Step 3; the test guards the fold contract.)

- [ ] **Step 3: Thread `registry_by_slug` in the producer**

In `gather.py` `main` (the `if man is not None:` branch, ~2460), build the map once and pass it:

```python
    if man is not None:
        members = manifest_mod.member_slugs(man)
        registry_by_slug = {
            "{}/{}".format(m["owner"], m["repo"]): m.get("registry")
            for m in man["repos"]}
        for m in man["repos"]:
            member_args = _member_args(args, m, man["from"], man["to"])
            bundle = acquire(member_args, os.environ)
            fold_bundle(conn, bundle, project=man["project"],
                        repo="{}/{}".format(m["owner"], m["repo"]),
                        members=members, registry_by_slug=registry_by_slug)
```

- [ ] **Step 4: Run the focused + full suite**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k "TestCrossRepoDependsOn or TestFoldDependsOnFlatten" -v && python3 -m pytest -q
```
Expected: PASS. Single-repo `fold_bundle(conn, bundle)` is unchanged (members/registry default `None`).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): emit cross-repo depends_on edges from fold (S4)"
```

---

## Task 4 (Slice 4a): `graphstore.edges_by_type` + `digest.project_depends_on`

Read the project's `depends_on` edges back from the store as a render-ready module-edge list, and surface it in the project view.

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`, `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`, `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_graphstore.py`:

```python
class TestEdgesByType(unittest.TestCase):
    def test_returns_project_edges_of_type_sorted(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_edges(conn, [
            ("p/A/x#area-main.tf", "p/A/y#area-main.tf", "depends_on", None,
             {"version": "1.0"}),
            ("q/A/z#area-main.tf", "q/A/w#area-main.tf", "depends_on", None, None),
        ])
        rows = graphstore.edges_by_type(conn, "depends_on", "p")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["src_id"], "p/A/x#area-main.tf")
        self.assertEqual(rows[0]["data"]["version"], "1.0")
```

Append to `test_digest.py`:

```python
class TestProjectDependsOn(unittest.TestCase):
    def test_project_depends_on_lists_cross_repo_edges(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_edges(conn, [
            ("proj/Azure/consumer#area-main.tf",
             "proj/Azure/kv#area-main.tf", "depends_on", None,
             {"version": "0.1.0", "cross_repo": True, "transitive": False}),
        ])
        edges = digest.project_depends_on(conn, "proj", ["Azure/consumer", "Azure/kv"])
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual(e["src_repo"], "Azure/consumer")
        self.assertEqual(e["dst_repo"], "Azure/kv")
        self.assertEqual(e["src_area"], "main.tf")
        self.assertEqual(e["dst_area"], "main.tf")
        self.assertTrue(e["cross_repo"])
        self.assertEqual(e["version"], "0.1.0")
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_graphstore.py -k TestEdgesByType test_digest.py -k TestProjectDependsOn -v
```
Expected: FAIL — missing `edges_by_type` / `project_depends_on`.

- [ ] **Step 3: Implement `edges_by_type`**

In `graphstore.py`, after `get_edges`:

```python
def edges_by_type(conn, edge_type, project):
    """All edges of `edge_type` whose src node belongs to `project`, sorted by
    (src_id, dst_id). src_id is the qualified '{project}/{repo}#local', so the
    project filter is a prefix match on '{project}/'."""
    rows = conn.execute(
        "SELECT * FROM edges WHERE edge_type=? AND src_id LIKE ? "
        "ORDER BY src_id, dst_id",
        (edge_type, project + "/%"))
    return [_row_to_edge(r) for r in rows]
```

- [ ] **Step 4: Implement `project_depends_on`**

In `digest.py`, after `build_project_view`:

```python
def _area_of(qid):
    """('owner/repo', 'area-tail') from a qualified area id
    '{project}/{owner}/{repo}#area-<tail>'."""
    parsed = graphstore.parse_id(qid)
    repo = parsed["scope"].split("/", 1)[1]            # strip leading project/
    local = parsed["local"]
    area = local[len("area-"):] if local.startswith("area-") else local
    return repo, area


def project_depends_on(conn, project, repos):
    """The project's module-dependency edges as render-ready rows:
    [{src_repo, src_area, dst_repo, dst_area, version, transitive, cross_repo}, ...]
    sorted by (src_id, dst_id). Reads resolved depends_on edges from the store."""
    repo_set = set(repos)
    out = []
    for e in graphstore.edges_by_type(conn, "depends_on", project):
        src_repo, src_area = _area_of(e["src_id"])
        dst_repo, dst_area = _area_of(e["dst_id"])
        if src_repo not in repo_set:
            continue
        d = e["data"] or {}
        out.append({
            "src_repo": src_repo, "src_area": src_area,
            "dst_repo": dst_repo, "dst_area": dst_area,
            "version": d.get("version"), "transitive": d.get("transitive"),
            "cross_repo": bool(d.get("cross_repo")) or src_repo != dst_repo,
        })
    return out
```

Then add `module_edges` to `build_project_view`'s return dict:

```python
        "modules": _merge_modules(members),
        "module_edges": project_depends_on(conn, project, repos),
    }
```

- [ ] **Step 5: Run them to verify they pass**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_graphstore.py -k TestEdgesByType test_digest.py -k "TestProjectDependsOn or TestBuildProjectView" -v
```
Expected: PASS. (Update `TestBuildProjectView`'s key-count assertion if it checks the exact view key set — `module_edges` is a new key.)

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_graphstore.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): project_depends_on module-edge view (S4)"
```

---

## Task 5 (Slice 4b): `render.emit_project_module_graph`

A project-wide Mermaid flowchart of the module-edge list: per-repo subgraphs, cross-repo edges labelled with the version.

**Files:**
- Modify: `.claude/skills/activity-overview/render.py`
- Test: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Append to `test_render.py`:

```python
class TestProjectModuleGraph(unittest.TestCase):
    def test_cross_repo_edge_drawn_with_subgraphs(self):
        edges = [{"src_repo": "Azure/consumer", "src_area": "main.tf",
                  "dst_repo": "Azure/kv", "dst_area": "main.tf",
                  "version": "0.1.0", "transitive": False, "cross_repo": True}]
        mmd = render.emit_project_module_graph(edges)
        self.assertIn("flowchart", mmd)
        self.assertIn("Azure/consumer", mmd)
        self.assertIn("Azure/kv", mmd)
        self.assertIn("0.1.0", mmd)

    def test_empty_edges_placeholder(self):
        mmd = render.emit_project_module_graph([])
        self.assertIn("No cross-repo module dependencies", mmd)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_render.py -k TestProjectModuleGraph -v
```
Expected: FAIL — no `emit_project_module_graph`.

- [ ] **Step 3: Implement**

In `render.py`, after `emit_module_graph`, mirroring its `_node_id`/`_flow_label` helpers:

```python
def emit_project_module_graph(module_edges):
    """A Mermaid flowchart of project module dependencies (Slice S4 blast radius).
    Nodes are grouped into a subgraph per member repo; each edge draws
    src-area --> dst-area labelled with its version (or 'transitive'). Cross-repo
    edges are the point of the diagram; intra-repo edges are included for context.
    Pure; deterministic (inputs are pre-sorted by digest.project_depends_on)."""
    if not module_edges:
        return "flowchart LR\n    none[No cross-repo module dependencies]\n"
    by_repo = {}
    drawn = []
    for e in module_edges:
        src = _node_id("m", "{}::{}".format(e["src_repo"], e["src_area"]))
        dst = _node_id("m", "{}::{}".format(e["dst_repo"], e["dst_area"]))
        by_repo.setdefault(e["src_repo"], {})[src] = e["src_area"]
        by_repo.setdefault(e["dst_repo"], {})[dst] = e["dst_area"]
        label = e.get("version") or ("transitive" if e.get("transitive") else "")
        drawn.append((src, dst, label))
    lines = ["flowchart LR"]
    for repo in sorted(by_repo):
        lines.append('    subgraph {}["{}"]'.format(_node_id("r", repo),
                                                     _flow_label(repo)))
        for nid, area in sorted(by_repo[repo].items()):
            lines.append('        {}("{}")'.format(nid, _flow_label(area)))
        lines.append("    end")
    for src, dst, label in sorted(set(drawn)):
        if label:
            lines.append('    {} -->|"{}"| {}'.format(src, _flow_label(label), dst))
        else:
            lines.append("    {} --> {}".format(src, dst))
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_render.py -k TestProjectModuleGraph -v
```
Expected: PASS (2 tests). The single-repo `render()` dict (render.py:362) is untouched — this is a project-level emitter the digest narrative calls directly.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "feat(activity): project-wide module blast-radius diagram (S4)"
```

---

## Task 6 (Slice 5): spotlight reverse-dependency (blast-radius) query

`member_dependents(conn, project, member)` — the members whose areas transitively depend on `member`'s areas (inbound `depends_on` BFS) — plus a `dependents` subcommand and md renderer.

**Files:**
- Modify: `.claude/skills/activity-overview/spotlight.py`
- Test: `.claude/skills/activity-overview/test_spotlight.py`

- [ ] **Step 1: Write the failing test**

Append to `test_spotlight.py`:

```python
class TestMemberDependents(unittest.TestCase):
    def _seed(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        # A depends_on B (A/main.tf -> B/main.tf); C depends_on A.
        graphstore.upsert_nodes(conn, [
            ("proj/Az/B#area-main.tf", "proj", "Az/B", "structure", None,
             {"id": "area-main.tf"}, None),
            ("proj/Az/A#area-main.tf", "proj", "Az/A", "structure", None,
             {"id": "area-main.tf"}, None),
            ("proj/Az/C#area-main.tf", "proj", "Az/C", "structure", None,
             {"id": "area-main.tf"}, None),
        ])
        graphstore.upsert_edges(conn, [
            ("proj/Az/A#area-main.tf", "proj/Az/B#area-main.tf", "depends_on",
             None, {"cross_repo": True}),
            ("proj/Az/C#area-main.tf", "proj/Az/A#area-main.tf", "depends_on",
             None, {"cross_repo": True}),
        ])
        return conn

    def test_blast_radius_from_b_returns_a_and_c(self):
        conn = self._seed()
        res = spotlight.member_dependents(conn, "proj", "Az/B")
        self.assertEqual(res["focus"], "Az/B")
        self.assertEqual(set(res["dependents"]), {"Az/A", "Az/C"})  # transitive

    def test_no_dependents(self):
        conn = self._seed()
        res = spotlight.member_dependents(conn, "proj", "Az/C")
        self.assertEqual(res["dependents"], [])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_spotlight.py -k TestMemberDependents -v
```
Expected: FAIL — no `member_dependents`.

- [ ] **Step 3: Implement the query + subcommand**

In `spotlight.py`, add the query (near `subsystem_split`):

```python
def member_dependents(conn, project, member):
    """Blast radius: the project members whose areas transitively depend on
    `member`'s areas, via inbound depends_on edges (A depends_on B => edge A->B,
    so 'who depends on B' walks in-edges). Returns a cited envelope. Deterministic.
    `member` is an 'owner/repo' slug."""
    prefix = graphstore.qualify_id(project, member, "")      # 'proj/owner/repo#'
    seed_areas = [n["id"] for n in graphstore.repo_nodes(
        conn, project, member, "structure")
        if graphstore.parse_id(n["id"])["local"].startswith("area-")]
    seen, frontier, dependents = set(seed_areas), list(seed_areas), set()
    while frontier:
        nxt = []
        for nid in frontier:
            for e in graphstore.get_edges(conn, nid, direction="in",
                                          edge_types=["depends_on"]):
                src = e["src_id"]
                if src in seen:
                    continue
                seen.add(src)
                nxt.append(src)
                dep_repo = graphstore.parse_id(src)["scope"].split("/", 1)[1]
                if dep_repo != member:
                    dependents.add(dep_repo)
        frontier = nxt
    return {
        "query": "dependents", "focus": member, "focus_kind": "member",
        "project": project, "status": "ok",
        "dependents": sorted(dependents),
    }
```

Add an md renderer and register both. After the existing `_render_*` md functions:

```python
def _render_dependents_md(res):
    deps = res.get("dependents") or []
    head = "# Blast radius — `{}`\n".format(res["focus"])
    if not deps:
        return head + "\nNothing in the project depends on this member.\n"
    return head + "\nMembers that (transitively) depend on it:\n\n" + "\n".join(
        "- `{}`".format(d) for d in deps) + "\n"
```

Register in `_RENDERERS` (spotlight.py:1153):

```python
_RENDERERS = {
    "person": _render_person_md,
    "symbol": _render_symbol_md,
    "subsystem": _render_subsystem_md,
    "grep": _render_grep_md,
    "dependents": _render_dependents_md,
}
```

And dispatch in `main` (after the `grep` branch, ~spotlight.py:1236):

```python
    elif args.query == "dependents":
        res = member_dependents(conn, project, args.args[0])
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_spotlight.py -k TestMemberDependents -v
```
Expected: PASS (2 tests). Verify `repo_nodes(conn, project, member, "structure")` is the correct accessor for a member's area nodes; if its signature differs, adapt the seed walk (the contract: enumerate `area-*` structure nodes for `member`).

- [ ] **Step 5: Full-suite guard + commit**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
git add .claude/skills/activity-overview/spotlight.py .claude/skills/activity-overview/test_spotlight.py
git commit -m "feat(activity): spotlight dependents (cross-repo blast radius) query (S4)"
```

---

## Task 7 (Slice 6): S4 gate — end-to-end integration + report section

Prove the gate: A's `main.tf` registry source → cross-repo `depends_on` A→B in the store → project module graph draws it → blast-radius from B returns A → `validate_project` green. Plus the report-template section.

**Files:**
- Modify: `.claude/skills/activity-overview/report-template.md`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the gate test**

Append to `test_digest.py` (it already imports `validate`, `render`):

```python
class TestS4Gate(unittest.TestCase):
    def test_cross_repo_depends_on_render_and_blast_radius(self):
        import render
        import spotlight
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/consumer",
                   "Azure/terraform-azurerm-avm-res-keyvault-vault"}
        consumer = {
            "meta": {"owner": "Azure", "repo": "consumer", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                 "edges": [{"to": None, "kind": "module",
                            "ref": "Azure/avm-res-keyvault-vault/azurerm",
                            "version": "0.1.0", "transitive": False,
                            "provider": "terraform", "resolved": False}]}]}}
        kv = {"meta": {"owner": "Azure",
                       "repo": "terraform-azurerm-avm-res-keyvault-vault",
                       "from": "2026-01-01", "to": "2026-01-31",
                       "base_branch": "main"},
              "prs": [], "issues": [], "commits": [], "code_events": [],
              "milestones": [], "releases": [],
              "code_graph": {"provider": "directory", "areas": [
                  {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                   "edges": []}]}}
        gather.fold_bundle(conn, consumer, project="proj", repo="Azure/consumer",
                           members=members, registry_by_slug={})
        gather.fold_bundle(conn, kv, project="proj",
                           repo="Azure/terraform-azurerm-avm-res-keyvault-vault",
                           members=members, registry_by_slug={})

        frm, to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        repos = graphstore.project_repos(conn, "proj")
        view = digest.build_project_view(conn, "proj", repos, frm, to)

        # one resolved cross-repo dependency edge in the view
        xrepo = [e for e in view["module_edges"] if e["cross_repo"]]
        self.assertEqual(len(xrepo), 1)
        self.assertEqual(xrepo[0]["src_repo"], "Azure/consumer")
        self.assertEqual(xrepo[0]["dst_repo"],
                         "Azure/terraform-azurerm-avm-res-keyvault-vault")

        # the diagram draws both members
        mmd = render.emit_project_module_graph(view["module_edges"])
        self.assertIn("Azure/consumer", mmd)
        self.assertIn("terraform-azurerm-avm-res-keyvault-vault", mmd)

        # blast radius from B returns A
        res = spotlight.member_dependents(
            conn, "proj", "Azure/terraform-azurerm-avm-res-keyvault-vault")
        self.assertEqual(res["dependents"], ["Azure/consumer"])

        # validate is green across the member set
        self.assertTrue(validate.validate_project(conn, "proj", repos)["ok"])
```

- [ ] **Step 2: Run it**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestS4Gate -v
```
Expected: PASS — Tasks 1–6 compose into the S4 gate. If it fails, the failure localizes: no `cross_repo` edge → Task 3 fold/resolution; empty diagram → Task 5; wrong blast radius → Task 6; validate red → a genuine multi-repo store issue to diagnose (report, don't weaken).

- [ ] **Step 3: Update `report-template.md`**

Add a project section (additive; keep the existing single-repo "Module dependency graph" guidance):

```markdown
## Cross-repo module dependencies (blast radius)

<!-- Source: `view["module_edges"]` (each {src_repo, src_area, dst_repo, dst_area,
     version, transitive, cross_repo}) and the diagram
     `render.emit_project_module_graph(view["module_edges"])`. Lead with the
     cross-repo edges (cross_repo == true): a member's module depending on another
     member's published module. For "if member X changes, who is affected?", cite
     `spotlight dependents <owner/repo>`. Omit the section when no module_edges. -->

```mermaid
{render.emit_project_module_graph(view["module_edges"])}
```

| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo |
|---|---|---|---|
| `{src_repo}` · `{src_area}` | `{dst_repo}` · `{dst_area}` | {version or "—"} | {"yes" when cross_repo} |
```

- [ ] **Step 4: Full-suite guard**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/test_digest.py
git commit -m "test(activity): S4 cross-repo depends_on gate + report section"
```

---

## Part 3 done — verification checklist

- [ ] A real fold now inserts intra-repo `depends_on` edges into the store (Slice 1 fixed the latent dead-code gap); spotlight's blast-radius works on gathered data.
- [ ] A registry source resolves to a member by exact manifest `registry` or the `terraform-{provider}-{name}` convention; a cross-repo `depends_on` edge to `area-main.tf` is emitted in multi-repo folds.
- [ ] Single-repo `fold_bundle(conn, bundle)` is byte-stable (no `members`/`registry_by_slug` → intra-repo edges only; `build_terraform_edges` untouched).
- [ ] `build_project_view` carries `module_edges`; `render.emit_project_module_graph` draws the per-repo blast-radius diagram.
- [ ] `spotlight dependents <owner/repo>` returns the transitive dependent members.
- [ ] The S4 gate test is green and `validate_project` is green on the two-member store; the full existing suite passes.

---

## Roadmap — Part 4 (S5, separate plan)

Real-data trust gate on a small AVM-TF constellation (2–3 interdependent module repos) over a fixed window: cross-repo trains form, cross-repo `depends_on` resolves, `validate` green. Plus `SKILL.md` (the `--manifest` producer + `digest` reader + the new blast-radius sections), `STORE.md` (manifest producer, cross-repo edges, `external_refs`, the now-live `depends_on` flatten), and `REFERENCE.md`.

---

## Self-Review (against the spec, S4)

- **S4 coverage:** registry→member resolution exact + convention (Task 2); cross-repo `depends_on` emitted by fold to the member root area (Tasks 1+3); render module graph spans members (Tasks 4+5); spotlight reverse-dependency blast-radius query (Task 6); the gate (Task 7) ✓.
- **Prerequisite:** the latent "depends_on never reaches the store" bug is fixed as Slice 1 (Task 1), which the gate depends on ✓.
- **Byte-stability:** `build_terraform_edges` untouched; `fold_bundle` single-repo path adds only the intra-repo edges it always should have (new `registry_by_slug` defaults `None`); `render()`'s single-repo diagram dict untouched (project emitter is separate); spotlight's existing subcommands unchanged. Full-suite guards in Tasks 1, 3, 6, 7 ✓.
- **Type consistency:** the edge `data` carries `version`/`transitive`/`ref`/`resolved`/`cross_repo`; `project_depends_on` rows use `{src_repo, src_area, dst_repo, dst_area, version, transitive, cross_repo}` consumed identically by `emit_project_module_graph` (Task 5), the report (Task 7), and the gate (Task 7); `member_dependents` returns `{query, focus, focus_kind, project, status, dependents}` consumed by `_render_dependents_md` ✓.
- **Determinism:** `edges_by_type` sorts by (src_id, dst_id); `resolve_registry_member` iterates `sorted(members)`; `member_dependents` returns `sorted(dependents)`; the diagram sorts nodes/edges ✓.
- **Deferred:** S5 to Part 4 ✓.
