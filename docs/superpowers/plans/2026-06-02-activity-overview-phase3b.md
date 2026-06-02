# Activity Overview — Phase 3b Implementation Plan (pluggable code-area provider + label facets)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thicken the activity-overview skill into **Phase 3b** — the *code-area attribution + label-facet* slice. Add a **pluggable, directory-first code-area provider** (`gather.py`) that maps every tracked file to an **area id** with zero dependencies and offline (the **directory provider**, primary, always available), plus an **optional graphify provider** (supported languages only, exercised through a recorded fixture — graphify is not installed in tests/CI). Auto-detect the repo's **label taxonomy** and stamp **`facets`** + an issue **`kind`** onto every issue/PR. Then have `link.py` **attribute `code_area` everywhere** the schema reserved a null — artifacts, feature_deltas, commits/trains, people — and fill the `modules` bundle field. Two of the schema's diagrams (`contributor_graph`, `kind_breakdown`) land in `render.py`. The report, `SKILL.md`, `BUNDLE.md`, and the live integration gate grow the matching sections. **Both halves (directory provider + label facets) validate on the Bicep gate**, where graphify is absent → the directory provider runs.

**Architecture:** Unchanged from Phase 3a — three offline-pure layers feed one network/git layer. `gather.py` grows pure `classify_code_area` / `build_directory_areas` / `parse_graphify_graph` / `parse_codeowners` / `detect_label_taxonomy` helpers (unit-tested from recorded fixtures) plus a thin **provider-selection seam** in `acquire()` (`select_code_area_provider`) so graphify is offline-testable and the directory provider is the no-tool fallback. `link.py` gains pure `apply_facets` / `classify_issue_kind` / `area_index` / `attribute_code_areas` / `build_modules` folds over the new `code_graph` + `label_taxonomy`, wired into `enrich()`. `render.py` gains pure `emit_contributor_graph` / `emit_kind_breakdown` emitters, registered in `render()` so the manifest gains two keys. Markdown report + SKILL + BUNDLE docs + the integration workflow grow the new sections.

**Tech Stack:** Python 3.11 stdlib only (`json`, `argparse`, `urllib`, `subprocess`, `shutil`, `unittest`); `git` for the clone; **`graphify` is OPTIONAL** (no longer a hard dependency — directory provider is primary); `mmdc` (mermaid-cli, via Node) as a preflight-checked external binary used only to validate/export diagrams. No third-party Python packages.

**Spec:** `docs/superpowers/specs/2026-06-01-activity-overview-design.md` — rev-8 "Code areas come from a pluggable provider, directory-first" core principle (~line 279), the "Code-area provider (local, zero-token, pluggable)" gather component (~lines 378-392), "Issue taxonomy & label facets" (~lines 142-161), the bundle schema (`code_graph.{provider,areas}`, `label_taxonomy`, issues[].`facets`/`kind`, `code_area`/`area`, `modules`, `people.modules`, `code_owners`, `trains[].code_areas` ~lines 444-487), and the Phase-3b phasing bullet (~line 853).

**Working directory:** `.claude/skills/activity-overview/`. Run all `python3`/`pytest` commands from that directory (it is how the existing suite is laid out — tests `sys.path.insert` the skill dir and read `fixtures/`).

**Branch:** continue on the existing `claude/activity-overview-phase3a`-style branch (or branch from it). All commits are local; the only push is the final task.

**Backward-compatibility rule (applies to every task):** Phase 1, Phase 2 **and Phase 3a** tests must stay green. Do **not** mutate any existing fixture (`rest_sample.json`, `rest_p2_sample.json`, `rest_p3_sample.json`, `bundle_sample.json`, `bundle_p2.json`, `bundle_p3.json`, `git_log_*.txt`), and do **not** change any existing test assertion. **One justified exception:** Task 11 Step 4a relaxes the pre-existing render manifest assertions (the manifest legitimately grows by two more diagram keys — `contributor_graph` and `kind_breakdown`), updating exactly those size-pinning comparisons to per-key/superset checks. Add **new** fixtures for Phase 3b (`fixtures/graphify_graph_sample.json`, `fixtures/codeowners_sample.txt`, `fixtures/bundle_p3b.json`). All new attribution logic must degrade permissively when fields are absent: an empty `code_graph` (or one with no matching paths) leaves `code_area`/`area` as `null` exactly as Phase 3a left them, and absent labels yield a `label_taxonomy` with `source:"auto"` and no facets — so the Phase 1/2/3a fixtures still process unchanged and every prior assertion stays green.

---

## LOCKED SCOPE & explicit deferrals

Phase 3b is the **pluggable directory-first code-area provider + label facets** slice. Explicit deferrals — the schema reserves their place; Phase 3b leaves them empty/absent and documents why:

- **Dependency edges** between areas (`code_graph.areas[].edges`) — Bicep `bicep build` → ARM `dependsOn`, Terraform `tree-sitter-hcl`. Phase 3b emits `edges: []` on every directory-provider area (the graphify provider passes through its `links` only as raw count context, not resolved area edges). **Deferred to a later slice.**
- **Symbol-granular / inline-comment artifacts** — still file-granularity only (carried over from Phase 3a; needs `-p` hunk + tree-sitter). `artifacts[].kind` stays `readme|doc|example`. **Deferred.**
- **graphify install in CI** — graphify is exercised **only via the recorded `graphify_graph_sample.json` unit fixture**. The live gate runs on Bicep, where graphify is **absent** and the **directory provider** is what runs and is asserted. **Do NOT add a graphify install step to CI.**
- **Multi-repo aggregation** — single target repo per run (carried over; Phase 6).
- **`hunk` / `before` / `after` / `detail`** on feature_deltas — still null (Phase 3a deferral, unchanged here).

Everything else in the locked scope (directory provider, graphify provider, provider selection seam, CODEOWNERS, `label_taxonomy` auto-detect, per-item `facets`, issue `kind`, `code_area`/`area` attribution onto artifacts/feature_deltas/commits/trains/people, the `modules` field, the two diagrams, the report/docs sections, and the integration gate) **is** built here.

---

## File Structure

All paths are under `.claude/skills/activity-overview/`.

- **Modify `gather.py`** — add pure `classify_code_area`, `build_directory_areas`, `DEFAULT_AREA_PATTERNS`, `parse_graphify_graph`, `parse_codeowners`, `detect_label_taxonomy`, and the provider-selection seam `select_code_area_provider`; wire all of them into `acquire()` (record `code_graph`, `code_owners`, `label_taxonomy`; stamp issue/PR `facets`/`kind` via the new helpers, fetching native issue types as a thin seam).
- **Modify `link.py`** — add pure `apply_facets`, `classify_issue_kind` (or import the gather classifier — decided in Task 6), `area_index`, `attribute_code_areas`, `build_modules`; call them from `enrich()` so `artifacts[].code_area`, `feature_deltas[].area`, `trains[].code_areas`, `people[].modules`/`areas`, and `bundle["modules"]` are populated.
- **Modify `render.py`** — add pure `emit_contributor_graph`, `emit_kind_breakdown`; register both in `render()` so the manifest gains `contributor_graph` + `kind_breakdown`.
- **Modify `test_gather.py`, `test_link.py`, `test_render.py`** — add Phase 3b test classes only; touch no existing assertion. **Exception:** Task 11 Step 4a relaxes the two pre-existing render manifest assertions (the manifest legitimately grows by two diagram keys).
- **Create fixtures:** `fixtures/graphify_graph_sample.json` (a small real-shaped graphify `graph.json` with `nodes` carrying `community` + `source_file` and a `links` list — NO top-level `communities`), `fixtures/codeowners_sample.txt` (a representative CODEOWNERS), `fixtures/bundle_p3b.json` (a pre-link bundle carrying `code_events` + a directory `code_graph` + a `label_taxonomy` + faceted issues, driving the end-to-end link/render fold).
- **Modify docs:** `report-template.md` (shipped grouped by code area; module ownership; issue-kind breakdown; facet-aware), `SKILL.md` (mention the new grouping + that graphify is optional), `BUNDLE.md` (document `code_graph.{provider,areas}`, `label_taxonomy`, `facets`, `kind`, `code_owners`, `modules`, the now-filled `code_area`/`area`; note graphify is optional + its real schema; note the deferrals).
- **Modify `.github/workflows/activity-overview-integration.yml`** — extend the assertion block to the Phase 3b contract and run it green on real Bicep data before the phase is done.

---

## Task 1: Directory code-area provider — pure classifier + builder

The **primary** provider. Every tracked file path maps to an **area id** via ordered config patterns, zero-dep and offline — this is what makes code areas work on Bicep/Terraform/any repo with no external tool. `classify_code_area(path, patterns)` returns one area id (a directory path); `build_directory_areas(paths, patterns)` folds a list of paths into the `code_graph` provider shape.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add after `classify_artifact_path`, before `fetch_all`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py` (after `TestArtifactPathClassifier`):

```python
class TestDirectoryCodeAreaProvider(unittest.TestCase):
    def test_avm_module_dir_is_the_four_segment_subtree(self):
        # AVM: avm/res/<service>/<module>/...  -> area = that 4-seg dir
        self.assertEqual(
            gather.classify_code_area(
                "avm/res/network/firewall-policy/main.bicep",
                gather.DEFAULT_AREA_PATTERNS),
            "avm/res/network/firewall-policy")
        self.assertEqual(
            gather.classify_code_area(
                "avm/res/network/firewall-policy/tests/e2e/main.test.bicep",
                gather.DEFAULT_AREA_PATTERNS),
            "avm/res/network/firewall-policy")

    def test_dir_containing_main_bicep_is_an_area(self):
        # Any directory that holds a main.bicep is a module root.
        paths = ["modules/keyvault/main.bicep", "modules/keyvault/README.md"]
        areas = gather.build_directory_areas(paths, gather.DEFAULT_AREA_PATTERNS)
        ids = {a["id"] for a in areas["areas"]}
        self.assertIn("modules/keyvault", ids)

    def test_terraform_modules_and_tf_dirs(self):
        self.assertEqual(
            gather.classify_code_area("modules/vnet/main.tf",
                                      gather.DEFAULT_AREA_PATTERNS),
            "modules/vnet")
        # any dir containing *.tf becomes that dir
        self.assertEqual(
            gather.classify_code_area("infra/network/variables.tf",
                                      gather.DEFAULT_AREA_PATTERNS),
            "infra/network")

    def test_generic_fallback_is_top_two_segments(self):
        self.assertEqual(
            gather.classify_code_area("src/app/handlers/auth.py",
                                      gather.DEFAULT_AREA_PATTERNS),
            "src/app")
        # a top-level file falls back to its own segment
        self.assertEqual(
            gather.classify_code_area("README.md", gather.DEFAULT_AREA_PATTERNS),
            "README.md")

    def test_build_directory_areas_groups_paths_and_shapes_provider(self):
        paths = [
            "avm/res/network/firewall-policy/main.bicep",
            "avm/res/network/firewall-policy/README.md",
            "avm/res/storage/account/main.bicep",
            "src/app/handlers/auth.py",
        ]
        cg = gather.build_directory_areas(paths, gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(cg["provider"], "directory")
        by_id = {a["id"]: a for a in cg["areas"]}
        self.assertEqual(
            sorted(by_id["avm/res/network/firewall-policy"]["paths"]),
            ["avm/res/network/firewall-policy/README.md",
             "avm/res/network/firewall-policy/main.bicep"])
        # label is a short tail of the id; edges deferred (always empty).
        fp = by_id["avm/res/network/firewall-policy"]
        self.assertEqual(fp["label"], "firewall-policy")
        self.assertEqual(fp["edges"], [])

    def test_empty_paths_yield_empty_provider(self):
        cg = gather.build_directory_areas([], gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(cg, {"provider": "directory", "areas": []})

    def test_none_path_classifies_to_none(self):
        self.assertIsNone(
            gather.classify_code_area(None, gather.DEFAULT_AREA_PATTERNS))
        self.assertIsNone(
            gather.classify_code_area("", gather.DEFAULT_AREA_PATTERNS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "DirectoryCodeAreaProvider" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'classify_code_area'`.

- [ ] **Step 3: Implement the directory provider**

Add to `gather.py` after `classify_artifact_path` (before `fetch_all`):

```python
# Ordered code-area patterns for the directory provider (primary, zero-dep).
# Each entry is (name, predicate(parts) -> area_id_or_None) tried in order; the
# first match wins. `parts` is path.split("/"). Patterns are directory-first and
# match how IaC repos define a module. The generic fallback is last.
def _avm_area(parts):
    # avm/res/<service>/<module>/...  -> the 4-segment module subtree.
    if len(parts) >= 4 and parts[0] == "avm" and parts[1] in ("res", "ptn", "utl"):
        return "/".join(parts[:4])
    return None


def _main_bicep_dir(parts):
    # any directory containing a main.bicep -> that directory.
    if parts and parts[-1] == "main.bicep":
        return "/".join(parts[:-1]) or parts[0]
    return None


def _terraform_modules_dir(parts):
    # modules/<name>/... -> modules/<name>.
    if len(parts) >= 2 and parts[0] == "modules":
        return "/".join(parts[:2])
    return None


def _tf_dir(parts):
    # any directory containing a *.tf file -> that directory.
    if parts and parts[-1].endswith(".tf"):
        return "/".join(parts[:-1]) or parts[0]
    return None


def _topn_dir(parts, n=2):
    # generic fallback: the first N path segments (or the file itself if shallower).
    if not parts:
        return None
    return "/".join(parts[:n])


DEFAULT_AREA_PATTERNS = [
    ("avm", _avm_area),
    ("main_bicep", _main_bicep_dir),
    ("terraform_modules", _terraform_modules_dir),
    ("tf_dir", _tf_dir),
    ("topn", _topn_dir),
]


def classify_code_area(path, patterns):
    """Map a tracked file path to a single area id (a directory path), or None.

    Tries the ordered `patterns` (AVM module subtree, any main.bicep dir, Terraform
    modules/<name>, any *.tf dir, else a top-2-segment fallback). Pure."""
    if not path:
        return None
    parts = path.split("/")
    for _name, fn in patterns:
        area = fn(parts)
        if area:
            return area
    return None


def _area_label(area_id):
    """A short, human tail for an area id (the last path segment)."""
    return (area_id or "").rstrip("/").split("/")[-1] or area_id


def build_directory_areas(paths, patterns):
    """Fold a list of tracked file paths into the `code_graph` directory provider.

    Shape: {"provider":"directory","areas":[{"id","label","paths":[...],"edges":[]}]}.
    Area id is the directory path; label is its tail; edges are deferred (empty).
    Deterministic (areas sorted by id, paths sorted). Pure."""
    grouped = {}
    for p in paths:
        area = classify_code_area(p, patterns)
        if area is None:
            continue
        grouped.setdefault(area, set()).add(p)
    areas = [
        {"id": area, "label": _area_label(area),
         "paths": sorted(grouped[area]), "edges": []}
        for area in sorted(grouped)
    ]
    return {"provider": "directory", "areas": areas}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "DirectoryCodeAreaProvider" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): directory-first code-area provider (pure classifier + builder)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 2: graphify fixture + pure graph parser (optional provider)

The **optional** provider, supported languages only. `parse_graphify_graph(graph_json)` reads graphify's REAL output shape — top keys `nodes`/`links` (NOT `edges`); each node `{id,label,file_type,source_file,source_location,community,norm_label}`; **no top-level `communities` list** — and groups nodes by their integer `community` into the same `code_graph` provider shape. graphify is NOT installed in tests/CI, so this is exercised against a recorded fixture only.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/graphify_graph_sample.json`
- Modify: `.claude/skills/activity-overview/gather.py` (add `parse_graphify_graph` after `build_directory_areas`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Create the recorded graphify fixture**

Write `.claude/skills/activity-overview/fixtures/graphify_graph_sample.json` — a small **real-shaped** graph.json: a `nodes` list whose entries carry `community` (int) + `source_file`, and a `links` list (graphify's edge key). NO top-level `communities` list.

```json
{
  "nodes": [
    {"id": "n1", "label": "handler_auth", "file_type": "py",
     "source_file": "src/app/auth.py", "source_location": "12:0",
     "community": 0, "norm_label": "handler auth"},
    {"id": "n2", "label": "handler_session", "file_type": "py",
     "source_file": "src/app/session.py", "source_location": "40:0",
     "community": 0, "norm_label": "handler session"},
    {"id": "n3", "label": "store_user", "file_type": "py",
     "source_file": "src/store/user.py", "source_location": "8:0",
     "community": 1, "norm_label": "store user"},
    {"id": "n4", "label": "store_index", "file_type": "py",
     "source_file": "src/store/index.py", "source_location": "3:0",
     "community": 1, "norm_label": "store index"},
    {"id": "n5", "label": "util_log", "file_type": "py",
     "source_file": "src/store/user.py", "source_location": "55:0",
     "community": 1, "norm_label": "util log"}
  ],
  "links": [
    {"source": "n1", "target": "n3", "weight": 2},
    {"source": "n2", "target": "n1", "weight": 1},
    {"source": "n3", "target": "n4", "weight": 1}
  ]
}
```

Verify it parses:

Run: `python3 -c "import json; d=json.load(open('fixtures/graphify_graph_sample.json')); print(len(d['nodes']), len(d['links']), 'communities' not in d)"`
Expected: `5 3 True`

- [ ] **Step 2: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestGraphifyProvider(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "graphify_graph_sample.json")) as fh:
            self.graph = json.load(fh)

    def test_groups_nodes_by_community_into_areas(self):
        cg = gather.parse_graphify_graph(self.graph)
        self.assertEqual(cg["provider"], "graphify")
        ids = {a["id"] for a in cg["areas"]}
        self.assertEqual(ids, {"community:0", "community:1"})

    def test_area_paths_are_distinct_source_files_in_the_community(self):
        cg = gather.parse_graphify_graph(self.graph)
        by_id = {a["id"]: a for a in cg["areas"]}
        self.assertEqual(sorted(by_id["community:0"]["paths"]),
                         ["src/app/auth.py", "src/app/session.py"])
        # n3 and n5 share src/store/user.py -> de-duplicated to one path
        self.assertEqual(sorted(by_id["community:1"]["paths"]),
                         ["src/store/index.py", "src/store/user.py"])

    def test_area_label_is_a_representative_dir(self):
        cg = gather.parse_graphify_graph(self.graph)
        by_id = {a["id"]: a for a in cg["areas"]}
        # a representative path/dir for the community (not empty)
        self.assertTrue(by_id["community:0"]["label"])
        self.assertEqual(by_id["community:0"]["edges"], [])

    def test_no_top_level_communities_key_required(self):
        # The real shape has NO top-level `communities`; parser must not need it.
        self.assertNotIn("communities", self.graph)
        cg = gather.parse_graphify_graph(self.graph)
        self.assertTrue(cg["areas"])

    def test_empty_or_nodeless_graph_yields_empty_provider(self):
        self.assertEqual(gather.parse_graphify_graph({}),
                         {"provider": "graphify", "areas": []})
        self.assertEqual(gather.parse_graphify_graph({"nodes": [], "links": []}),
                         {"provider": "graphify", "areas": []})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "GraphifyProvider" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'parse_graphify_graph'`.

- [ ] **Step 4: Implement the parser**

Add to `gather.py` after `build_directory_areas`:

```python
def parse_graphify_graph(graph_json):
    """Group graphify nodes by their integer `community` into the code_graph shape.

    Reads graphify's REAL output: top keys `nodes`/`links` (edges live under
    `links`, NOT `edges`); each node carries `community` (int) + `source_file`;
    there is NO top-level `communities` list. Produces
    {"provider":"graphify","areas":[{"id":"community:<n>","label",
    "paths":[distinct source_files],"edges":[]}]}. Area edges are deferred (empty);
    `links` are graphify's symbol-level edges, not resolved area edges. Pure."""
    by_comm = {}
    for node in (graph_json or {}).get("nodes", []):
        comm = node.get("community")
        if comm is None:
            continue
        src = node.get("source_file")
        if src:
            by_comm.setdefault(int(comm), set()).add(src)
    areas = []
    for comm in sorted(by_comm):
        paths = sorted(by_comm[comm])
        # representative label: the shortest common-ish dir — use the first path's
        # directory (deterministic given sorted paths), or the path itself.
        head = paths[0]
        label = head.rsplit("/", 1)[0] if "/" in head else head
        areas.append({"id": f"community:{comm}", "label": label,
                      "paths": paths, "edges": []})
    return {"provider": "graphify", "areas": areas}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "GraphifyProvider" -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py .claude/skills/activity-overview/fixtures/graphify_graph_sample.json
git commit -m "$(cat <<'EOF'
feat(activity): parse graphify graph.json (nodes/links, community grouping)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 3: Provider selection seam (offline-testable)

`acquire()` must prefer graphify ONLY if `graphify` is on PATH AND it produced a `graph.json` with nodes; otherwise the directory provider. No fail-fast (graphify is optional now). The decision lives in a pure-ish seam `select_code_area_provider` taking injectable `which`/`run`/`read_json` callables so it is exercised offline without graphify installed.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add `select_code_area_provider` after `parse_graphify_graph`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestProviderSelection(unittest.TestCase):
    PATHS = ["avm/res/network/firewall-policy/main.bicep",
             "src/app/handlers/auth.py"]

    def test_uses_directory_provider_when_graphify_absent(self):
        cg = gather.select_code_area_provider(
            self.PATHS, "clone", which=lambda _n: None)
        self.assertEqual(cg["provider"], "directory")
        self.assertTrue(cg["areas"])

    def test_uses_directory_provider_when_graphify_emits_no_nodes(self):
        # graphify on PATH and runs, but graph.json has no nodes -> fall back.
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify",
            run=lambda cmd, **kw: None,
            read_json=lambda _p: {"nodes": [], "links": []})
        self.assertEqual(cg["provider"], "directory")

    def test_prefers_graphify_when_present_and_nodes_exist(self):
        with open(os.path.join(FIX, "graphify_graph_sample.json")) as fh:
            graph = json.load(fh)
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify",
            run=lambda cmd, **kw: None,
            read_json=lambda _p: graph)
        self.assertEqual(cg["provider"], "graphify")
        self.assertTrue(cg["areas"])

    def test_graphify_run_failure_falls_back_silently(self):
        def boom(cmd, **kw):
            raise RuntimeError("graphify exploded")
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify", run=boom,
            read_json=lambda _p: {"nodes": []})
        self.assertEqual(cg["provider"], "directory")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "ProviderSelection" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'select_code_area_provider'`.

- [ ] **Step 3: Implement the seam**

Add to `gather.py` after `parse_graphify_graph`. (`shutil` is already imported via... actually `gather.py` does not import `shutil` — add `import shutil` to the import block at the top, next to `subprocess`.)

```python
def _read_json_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def select_code_area_provider(paths, clone_dir, which=shutil.which,
                              run=run_git, read_json=_read_json_file,
                              patterns=None):
    """Pick the code-area provider, directory-first.

    graphify is OPTIONAL: prefer it only if it is on PATH AND `graphify update
    <clone>` yields a `graphify-out/graph.json` with nodes; any absence/failure/
    nodeless-graph falls back to the directory provider (never fails fast). The
    `which`/`run`/`read_json` seams make this offline-testable without graphify.
    Returns the `code_graph` provider dict."""
    patterns = patterns or DEFAULT_AREA_PATTERNS
    directory = build_directory_areas(paths, patterns)
    if not which("graphify"):
        return directory
    try:
        run(["graphify", "update", clone_dir])
        graph = read_json(os.path.join(clone_dir, "graphify-out", "graph.json"))
    except Exception:
        return directory
    if not (graph or {}).get("nodes"):
        return directory
    graphified = parse_graphify_graph(graph)
    return graphified if graphified["areas"] else directory
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "ProviderSelection" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): pluggable provider selection (directory-first, graphify optional)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 4: CODEOWNERS fixture + pure parser

Map path/glob → owning logins from the clone's CODEOWNERS (`.github/`/root/`docs/`). `parse_codeowners(text)` is the pure, unit-tested parser; `acquire()` reads the file from the clone (a thin local-file seam, no network).

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/codeowners_sample.txt`
- Modify: `.claude/skills/activity-overview/gather.py` (add `parse_codeowners` after `select_code_area_provider`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Create the fixture**

Write `.claude/skills/activity-overview/fixtures/codeowners_sample.txt`:

```
# CODEOWNERS — comments and blank lines are ignored

*                       @org/maintainers
avm/res/network/        @alice @bob
avm/res/storage/        @carol
docs/                   @org/docs-team @dave
*.bicep                 @bicep-reviewers
```

Verify it parses as text:

Run: `python3 -c "print(sum(1 for ln in open('fixtures/codeowners_sample.txt') if ln.strip() and not ln.lstrip().startswith('#')))"`
Expected: `5`

- [ ] **Step 2: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestParseCodeowners(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "codeowners_sample.txt")) as fh:
            self.text = fh.read()

    def test_maps_glob_to_logins_stripping_at(self):
        owners = gather.parse_codeowners(self.text)
        self.assertEqual(owners["avm/res/network/"], ["alice", "bob"])
        self.assertEqual(owners["avm/res/storage/"], ["carol"])
        self.assertEqual(owners["*.bicep"], ["bicep-reviewers"])

    def test_team_handles_are_kept_as_owners(self):
        owners = gather.parse_codeowners(self.text)
        self.assertEqual(owners["docs/"], ["org/docs-team", "dave"])
        self.assertEqual(owners["*"], ["org/maintainers"])

    def test_comments_and_blank_lines_ignored(self):
        owners = gather.parse_codeowners(self.text)
        self.assertNotIn("#", "".join(owners))
        self.assertEqual(len(owners), 5)

    def test_permissive_on_empty_or_none(self):
        self.assertEqual(gather.parse_codeowners(""), {})
        self.assertEqual(gather.parse_codeowners(None), {})

    def test_pattern_with_no_owners_is_skipped(self):
        self.assertEqual(gather.parse_codeowners("docs/   \n"), {})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "ParseCodeowners" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'parse_codeowners'`.

- [ ] **Step 4: Implement the parser**

Add to `gather.py` after `select_code_area_provider`:

```python
def parse_codeowners(text):
    """Parse a CODEOWNERS file into {pattern: [login, ...]}.

    Each non-comment line is `<pattern> <owner...>`; `@user` / `@org/team` are
    stripped of the leading `@`. Lines with a pattern but no owners are skipped.
    Order-preserving owners, last-pattern-wins on duplicate patterns. Pure."""
    owners = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, handles = parts[0], parts[1:]
        logins = [h[1:] if h.startswith("@") else h for h in handles]
        if not logins:
            continue
        owners[pattern] = logins
    return owners
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "ParseCodeowners" -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py .claude/skills/activity-overview/fixtures/codeowners_sample.txt
git commit -m "$(cat <<'EOF'
feat(activity): parse CODEOWNERS into path->owning-logins map

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 5: `label_taxonomy` auto-detect (pure, with config override)

`detect_label_taxonomy(labels, config=None)` scans all repo labels for structured namespaces (`area:*`/`area/*`, `priority:*`, `status:*`, `lifecycle:*`, and AVM `Class:`/`Type:`/`Needs:` style) → `{"<facet>":{"<namespace-or-value>":[label]}, "source":"auto|config|merged"}`. A `config` block overrides/extends the auto-map. Degrades to "no facets" rather than guessing.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add `detect_label_taxonomy` + namespace map after `parse_codeowners`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestDetectLabelTaxonomy(unittest.TestCase):
    LABELS = ["area: networking", "area: storage", "priority: high",
              "status: in progress", "Type: Bug", "Class: Resource Module",
              "Needs: Triage", "lifecycle/stale", "good first issue"]

    def test_auto_detects_known_namespaces_into_facets(self):
        tax = gather.detect_label_taxonomy(self.LABELS)
        self.assertEqual(tax["source"], "auto")
        # area facet groups both area:* labels under the namespace
        self.assertIn("area", tax)
        self.assertEqual(sorted(tax["area"]["area:"]),
                         ["area: networking", "area: storage"])
        self.assertIn("priority", tax)
        self.assertIn("status", tax)
        # AVM Class:/Type:/Needs: map to kind/lifecycle facets
        self.assertIn("kind", tax)
        self.assertIn("Type:", tax["kind"])

    def test_unprefixed_labels_do_not_create_facets(self):
        tax = gather.detect_label_taxonomy(["good first issue", "bug"])
        # nothing structured -> no facet buckets, just the source marker
        self.assertEqual(set(tax) - {"source"}, set())
        self.assertEqual(tax["source"], "auto")

    def test_config_override_extends_and_marks_source_merged(self):
        config = {"area": ["component:"], "priority": ["sev/"]}
        tax = gather.detect_label_taxonomy(
            ["component: api", "sev/1", "area: networking"], config=config)
        self.assertEqual(tax["source"], "merged")
        self.assertIn("component:", tax["area"])
        self.assertIn("sev/", tax["priority"])
        # auto-detected area:* still present alongside the config namespace
        self.assertIn("area:", tax["area"])

    def test_config_only_when_no_auto_marks_source_config(self):
        tax = gather.detect_label_taxonomy(
            ["component: api"], config={"area": ["component:"]})
        self.assertEqual(tax["source"], "config")

    def test_empty_labels_yield_no_facets(self):
        self.assertEqual(gather.detect_label_taxonomy([]), {"source": "auto"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "DetectLabelTaxonomy" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'detect_label_taxonomy'`.

- [ ] **Step 3: Implement the detector**

Add to `gather.py` after `parse_codeowners`. The auto-map pins the conventional namespaces (and AVM's `Class:`/`Type:`/`Needs:`) to facets; the splitter accepts either `:` or `/` separators.

```python
# Conventional label namespace -> facet (auto-detect). AVM uses `Class:`/`Type:`/
# `Needs:`; most repos use `area`/`priority`/`status`/`lifecycle` with `:` or `/`.
_AUTO_FACET_NAMESPACES = {
    "area": "area", "component": "area",
    "priority": "priority", "p": "priority",
    "status": "status", "needs": "status",
    "lifecycle": "lifecycle",
    "type": "kind", "kind": "kind", "class": "kind",
}


def _namespace_of(label):
    """Return the lowercase namespace token of a structured label, or None.
    A structured label looks like `<ns>: value` or `<ns>/value`."""
    for sep in (":", "/"):
        if sep in label:
            ns = label.split(sep, 1)[0].strip().lower()
            if ns:
                return ns, label.split(sep, 1)[0].strip() + sep
    return None


def detect_label_taxonomy(labels, config=None):
    """Auto-detect structured label namespaces and map them to facets.

    Returns {"<facet>": {"<namespace>": [label, ...]}, "source": "auto|config|merged"}.
    `config` (a {facet: [namespace-prefix, ...]} block) overrides/extends the
    auto-map. Degrades to {"source": "auto"} (no facets) rather than guessing on
    unprefixed labels. Pure."""
    auto = {}
    config_facets = {}

    # Build the config namespace -> facet lookup (prefixes may end with ':' or '/').
    cfg_lookup = {}
    for facet, prefixes in (config or {}).items():
        for pre in prefixes:
            cfg_lookup[pre.rstrip(":/").lower()] = (facet, pre)

    for label in labels or []:
        parsed = _namespace_of(label)
        if not parsed:
            continue
        ns_token, ns_display = parsed
        # config wins over auto for the same namespace token.
        if ns_token in cfg_lookup:
            facet, pre = cfg_lookup[ns_token]
            config_facets.setdefault(facet, {}).setdefault(pre, []).append(label)
        elif ns_token in _AUTO_FACET_NAMESPACES:
            facet = _AUTO_FACET_NAMESPACES[ns_token]
            auto.setdefault(facet, {}).setdefault(ns_display, []).append(label)

    # Merge config over auto.
    merged = {f: dict(ns) for f, ns in auto.items()}
    for facet, ns_map in config_facets.items():
        merged.setdefault(facet, {})
        for pre, labs in ns_map.items():
            merged[facet][pre] = labs

    if config_facets and auto:
        source = "merged"
    elif config_facets:
        source = "config"
    else:
        source = "auto"
    out = {f: ns for f, ns in merged.items()}
    out["source"] = source
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "DetectLabelTaxonomy" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): auto-detect label taxonomy into facets (config override)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 6: Per-item `facets` + issue `kind` (pure)

`apply_facets(item, taxonomy)` derives `{area, priority, status, lifecycle}` from an item's labels via the taxonomy. `classify_issue_kind(issue, taxonomy, types_present)` returns one of `feature/module-request/bug/idea/question/docs/other` in priority order: native GitHub issue type (if present on the issue) → label `kind` facet → issue-template filename → title/body heuristic → `other`. Both are pure; `acquire()` (Task 8) fetches the native issue type as a thin seam.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add `apply_facets`, `classify_issue_kind` after `detect_label_taxonomy`)
- Test: `.claude/skills/activity-overview/test_gather.py`

> **Placement decision:** both functions live in `gather.py` so the facet/kind logic sits with the label-taxonomy detector and `acquire()` can stamp items at fetch time (the spec wants `facets`/`kind` carried on the bundle's issues/PRs from acquire). `link.py` does NOT re-derive them; it reads the stamped values. (This mirrors how `classify_artifact_path` lives in `gather.py` and `link.py` imports it.)

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestFacetsAndKind(unittest.TestCase):
    TAX = {
        "area": {"area:": ["area: networking", "area: storage"]},
        "priority": {"priority:": ["priority: high"]},
        "status": {"status:": ["status: in progress"]},
        "kind": {"Type:": ["Type: Bug", "Type: Feature"]},
        "lifecycle": {"lifecycle/": ["lifecycle/stale"]},
        "source": "auto",
    }

    def test_apply_facets_picks_one_value_per_facet_from_labels(self):
        item = {"labels": ["area: networking", "priority: high", "Type: Bug"]}
        f = gather.apply_facets(item, self.TAX)
        self.assertEqual(f["area"], "area: networking")
        self.assertEqual(f["priority"], "priority: high")
        self.assertIsNone(f["status"])
        self.assertIsNone(f["lifecycle"])

    def test_apply_facets_returns_all_four_keys_even_when_empty(self):
        f = gather.apply_facets({"labels": []}, self.TAX)
        self.assertEqual(set(f), {"area", "priority", "status", "lifecycle"})
        self.assertTrue(all(v is None for v in f.values()))

    def test_kind_native_issue_type_wins(self):
        issue = {"labels": ["Type: Bug"], "title": "crash",
                 "issue_type": "Feature"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=True),
            "feature")

    def test_kind_label_facet_when_no_native_type(self):
        issue = {"labels": ["Type: Bug"], "title": "whatever"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=False),
            "bug")

    def test_kind_template_filename_then_heuristic(self):
        # template name maps module requests
        issue = {"labels": [], "title": "x",
                 "template": "module_request.md"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=False),
            "module-request")
        # title heuristic: a question
        q = {"labels": [], "title": "How do I configure the firewall?"}
        self.assertEqual(
            gather.classify_issue_kind(q, self.TAX, types_present=False),
            "question")

    def test_kind_defaults_to_other(self):
        self.assertEqual(
            gather.classify_issue_kind({"labels": [], "title": "misc"},
                                       self.TAX, types_present=False),
            "other")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "FacetsAndKind" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'apply_facets'`.

- [ ] **Step 3: Implement both helpers**

Add to `gather.py` after `detect_label_taxonomy`:

```python
_FACET_KEYS = ("area", "priority", "status", "lifecycle")

# Native issue-type / label-value tokens -> canonical kind.
_KIND_TOKENS = {
    "feature": "feature", "enhancement": "feature",
    "module": "module-request", "module-request": "module-request",
    "module request": "module-request",
    "bug": "bug", "defect": "bug",
    "idea": "idea", "proposal": "idea",
    "question": "question", "support": "question",
    "doc": "docs", "docs": "docs", "documentation": "docs",
}
_VALID_KINDS = {"feature", "module-request", "bug", "idea", "question", "docs", "other"}


def _kind_from_token(text):
    """Map a free token (issue-type name, label value, template stem) to a kind."""
    low = (text or "").strip().lower()
    for token, kind in _KIND_TOKENS.items():
        if token in low:
            return kind
    return None


def _labels_in_taxonomy(item, taxonomy, facet):
    """Labels on `item` that belong to `facet` per the taxonomy, order-preserving."""
    facet_labels = set()
    for labs in (taxonomy.get(facet) or {}).values():
        facet_labels.update(labs)
    return [lbl for lbl in item.get("labels", []) if lbl in facet_labels]


def apply_facets(item, taxonomy):
    """Derive {area, priority, status, lifecycle} for an item from its labels.

    Each facet takes the first matching label (or None). Pure; never raises on
    an empty taxonomy (every facet is then None)."""
    out = {}
    for facet in _FACET_KEYS:
        matches = _labels_in_taxonomy(item, taxonomy, facet)
        out[facet] = matches[0] if matches else None
    return out


def classify_issue_kind(issue, taxonomy, types_present):
    """Classify an issue into one of feature/module-request/bug/idea/question/docs/other.

    Priority: native GitHub issue type (when present) -> label `kind` facet ->
    issue-template filename -> title/body heuristic -> other. Pure."""
    # 1. native issue type
    if types_present:
        kind = _kind_from_token(issue.get("issue_type"))
        if kind:
            return kind
    # 2. label kind facet (the values carried under the taxonomy's `kind` facet)
    for lbl in _labels_in_taxonomy(issue, taxonomy, "kind"):
        value = lbl.split(":", 1)[-1] if ":" in lbl else lbl.split("/", 1)[-1]
        kind = _kind_from_token(value)
        if kind:
            return kind
    # 3. issue-template filename (e.g. module_request.md, bug_report.yml)
    kind = _kind_from_token((issue.get("template") or "").replace("_", " "))
    if kind:
        return kind
    # 4. title/body heuristic
    text = f"{issue.get('title','')} {issue.get('body','')}"
    if "?" in (issue.get("title") or ""):
        return "question"
    kind = _kind_from_token(text)
    if kind:
        return kind
    return "other"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "FacetsAndKind" -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): per-item facets + issue kind classifier (native type->label->heuristic)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 7: Wire the new gather products into `acquire()`

Compose Tasks 1-6 into `acquire()`: collect all tracked paths from `code_events`/`commits` → `select_code_area_provider` → `code_graph`; read CODEOWNERS from the clone → `code_owners`; collect all issue+PR labels → `detect_label_taxonomy` → `label_taxonomy`; stamp `facets` on every PR/issue and refresh each issue's `kind`. Native issue type is fetched as a thin seam (best-effort; falls through when the repo has no issue types). Verified **offline** by composing the pure helpers over recorded fixtures (mirrors `TestAcquireAssemblyP3`).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (`acquire()` wiring + `build_bundle` reserves nothing new — `code_graph`/`code_owners`/`label_taxonomy`/`modules` already reserved)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test (offline composition, like P3a)**

Add a new class to `test_gather.py`:

```python
class TestAcquireAssemblyP3b(unittest.TestCase):
    """Compose the Phase 3b helpers over recorded inputs, offline."""

    def _bundle(self):
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            code_events = gather.parse_code_events(fh.read())
        with open(os.path.join(FIX, "codeowners_sample.txt")) as fh:
            code_owners = gather.parse_codeowners(fh.read())

        # paths the provider sees come from code_events + commit file lists
        paths = sorted({e["path"] for e in code_events}
                       | {e["old_path"] for e in code_events if e.get("old_path")})
        code_graph = gather.select_code_area_provider(
            paths, "clone", which=lambda _n: None)  # graphify absent -> directory

        prs = [{"number": 42, "labels": ["area: networking", "Type: Bug"],
                "title": "fix policy", "body": ""}]
        issues = [{"number": 18, "labels": ["area: storage", "priority: high"],
                   "title": "Need storage module", "body": "module please",
                   "state": "open"}]
        all_labels = sorted({l for it in prs + issues for l in it["labels"]})
        taxonomy = gather.detect_label_taxonomy(all_labels)
        for it in prs + issues:
            it["facets"] = gather.apply_facets(it, taxonomy)
        for issue in issues:
            issue["kind"] = gather.classify_issue_kind(
                issue, taxonomy, types_present=False)

        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["code_events"] = code_events
        bundle["code_graph"] = code_graph
        bundle["code_owners"] = code_owners
        bundle["label_taxonomy"] = taxonomy
        return bundle

    def test_code_graph_is_directory_provider_with_areas(self):
        b = self._bundle()
        self.assertEqual(b["code_graph"]["provider"], "directory")
        self.assertTrue(b["code_graph"]["areas"])
        for a in b["code_graph"]["areas"]:
            self.assertTrue(a["id"] and a["paths"])

    def test_label_taxonomy_and_facets_present(self):
        b = self._bundle()
        self.assertIn("source", b["label_taxonomy"])
        pr = b["prs"][0]
        self.assertEqual(pr["facets"]["area"], "area: networking")
        issue = b["issues"][0]
        self.assertEqual(issue["facets"]["priority"], "priority: high")
        self.assertIn(issue["kind"],
                      {"feature", "module-request", "bug", "idea",
                       "question", "docs", "other"})

    def test_code_owners_present(self):
        b = self._bundle()
        self.assertIn("avm/res/network/", b["code_owners"])
```

- [ ] **Step 2: Run the composition test (passes once Tasks 1-6 land)**

Run: `python3 -m pytest test_gather.py -k "AcquireAssemblyP3b" -v`
Expected: PASS (this composes pure helpers + `build_bundle`, like `TestAcquireAssemblyP3`). If run before Tasks 1-6 it fails with `AttributeError`. Once green, proceed to wire `acquire()` (Step 3), which the live gate (Task 13) exercises on real data.

- [ ] **Step 3: Wire `acquire()`**

In `acquire()`, after `code_events = parse_code_events(raw_walk)` (and its guard), add the code-area provider + CODEOWNERS:

```python
    # Phase 3b: code-area provider (directory-first; graphify optional). Paths come
    # from the code-event walk + the commit file lists (local, zero-token).
    area_paths = sorted(
        {e["path"] for e in code_events}
        | {e["old_path"] for e in code_events if e.get("old_path")}
        | {f for c in commits for f in c.get("files", [])})
    code_graph = select_code_area_provider(area_paths, clone_dir)

    # CODEOWNERS from the clone (local file; try the conventional locations).
    code_owners = {}
    for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        p = os.path.join(clone_dir, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                code_owners = parse_codeowners(fh.read())
            break
```

After issues are fully assembled (after the per-issue comment/reaction loop) and before the `workflows` block, add the label-taxonomy + facets/kind stamping:

```python
    # Phase 3b: label taxonomy over every repo label seen, then stamp facets + kind.
    all_labels = sorted({lbl for it in prs + issues for lbl in it.get("labels", [])})
    label_taxonomy = detect_label_taxonomy(all_labels)
    types_present = _repo_has_issue_types(api, token)  # thin seam, best-effort
    for pr in prs:
        pr["facets"] = apply_facets(pr, label_taxonomy)
    for issue in issues:
        issue["facets"] = apply_facets(issue, label_taxonomy)
        # native issue type when the repo uses them (acquire fetches it per issue
        # in the existing issue loop — see Step 3a); the classifier is pure.
        issue["kind"] = classify_issue_kind(issue, label_taxonomy, types_present)
```

Finally, after `bundle = build_bundle(...)` and the existing assignments, record the new top-level fields:

```python
    bundle["code_graph"] = code_graph
    bundle["code_owners"] = code_owners
    bundle["label_taxonomy"] = label_taxonomy
```

- [ ] **Step 3a: Add the native-issue-type seam**

Add a thin, best-effort helper near `http_get_json` (not unit-tested — a network seam that degrades to False):

```python
def _repo_has_issue_types(api, token):
    """True if the repo defines native issue types. Best-effort: any failure
    (older API, no types, 404) is treated as 'no native types'."""
    try:
        owner_repo = api.rsplit("/repos/", 1)[-1]
        owner = owner_repo.split("/")[0]
        data, _ = http_get_json(
            f"https://api.github.com/orgs/{owner}/issue-types", token)
        return bool(data)
    except Exception:
        return False
```

And, where each issue is enriched (the existing `for issue in issues:` loop that sets `comments_list`/`reactions`), capture the raw issue's native type from `raw_by_num` so the classifier can read it:

```python
        issue["issue_type"] = (raw_by_num.get(n, {}).get("type") or {}).get("name") \
            if isinstance(raw_by_num.get(n, {}).get("type"), dict) else None
```

(Real GitHub returns the issue's native type under `issue["type"]["name"]` when the repo uses issue types; absent otherwise → `None`, and the label/heuristic path takes over.)

- [ ] **Step 4: Run the full gather suite**

Run: `python3 -m pytest test_gather.py -v`
Expected: PASS — every Phase 1/2/3a gather test + the four new Phase 3b classes. The skeleton test (`test_skeleton_has_all_top_level_keys_and_reserved_empties`) still passes: `code_graph`/`code_owners`/`label_taxonomy`/`modules` were already reserved empty in `build_bundle`, and the skeleton test loops a fixed allow-list (it does not assert the absence of extra keys).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): acquire code_graph, code_owners, label_taxonomy + item facets/kind

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 8: `link.py` — area index + `code_area` attribution onto artifacts & feature_deltas

Build a `path → area id` index from `code_graph.areas`, then attribute `code_area` onto every artifact (replace the Phase 3a null) and `area` onto every feature_delta (replace the null). Pure.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add `area_index`, `_area_for_path`, `attribute_code_areas` after `compute_feature_deltas`)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestCodeAreaAttribution(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_graph": {"provider": "directory", "areas": [
                {"id": "examples/basic", "label": "basic",
                 "paths": ["examples/basic/main.bicep"], "edges": []},
                {"id": "docs", "label": "docs",
                 "paths": ["docs/firewall.md"], "edges": []},
            ]},
            "artifacts": {
                "art:examples/basic/main.bicep": {
                    "kind": "example", "path": "examples/basic/main.bicep",
                    "name": "main.bicep", "status": "live", "replaced_by": None,
                    "code_area": None, "lifecycle": []},
                "art:docs/firewall.md": {
                    "kind": "doc", "path": "docs/firewall.md", "name": "firewall.md",
                    "status": "removed", "replaced_by": None, "code_area": None,
                    "lifecycle": []},
                "art:README.md": {
                    "kind": "readme", "path": "README.md", "name": "README.md",
                    "status": "live", "replaced_by": None, "code_area": None,
                    "lifecycle": []},
            },
            "feature_deltas": [
                {"kind": "add", "subject": "example", "name": "main.bicep",
                 "artifact": "art:examples/basic/main.bicep", "area": None,
                 "commit": "c1", "url": "u"},
                {"kind": "drop", "subject": "doc", "name": "firewall.md",
                 "artifact": "art:docs/firewall.md", "area": None,
                 "commit": "c4", "url": "u"},
            ],
        }

    def test_area_index_maps_each_path_to_its_area(self):
        idx = link.area_index(self._bundle()["code_graph"])
        self.assertEqual(idx["examples/basic/main.bicep"], "examples/basic")
        self.assertEqual(idx["docs/firewall.md"], "docs")

    def test_attribute_fills_artifact_code_area(self):
        b = self._bundle()
        link.attribute_code_areas(b)
        arts = b["artifacts"]
        self.assertEqual(arts["art:examples/basic/main.bicep"]["code_area"],
                         "examples/basic")
        self.assertEqual(arts["art:docs/firewall.md"]["code_area"], "docs")
        # a path not in the graph stays null (no guessing)
        self.assertIsNone(arts["art:README.md"]["code_area"])

    def test_attribute_fills_feature_delta_area(self):
        b = self._bundle()
        link.attribute_code_areas(b)
        by_artifact = {d["artifact"]: d for d in b["feature_deltas"]}
        self.assertEqual(
            by_artifact["art:examples/basic/main.bicep"]["area"], "examples/basic")
        self.assertEqual(by_artifact["art:docs/firewall.md"]["area"], "docs")

    def test_empty_code_graph_leaves_everything_null(self):
        b = self._bundle()
        b["code_graph"] = {}
        link.attribute_code_areas(b)
        self.assertIsNone(b["artifacts"]["art:docs/firewall.md"]["code_area"])
        self.assertIsNone(b["feature_deltas"][0]["area"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "CodeAreaAttribution" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'area_index'`.

- [ ] **Step 3: Implement the index + attribution**

Add to `link.py` after `compute_feature_deltas`:

```python
def area_index(code_graph):
    """Build a path -> area id index from a code_graph's areas. Pure."""
    idx = {}
    for area in (code_graph or {}).get("areas", []):
        for path in area.get("paths", []):
            idx[path] = area["id"]
    return idx


def _area_for_path(path, idx):
    """Direct lookup; None when the path is not covered by any area (no guessing)."""
    return idx.get(path)


def attribute_code_areas(bundle):
    """Fill `code_area` on artifacts and `area` on feature_deltas from code_graph.

    Replaces the Phase 3a nulls where the path is covered by an area; leaves null
    otherwise (degrades cleanly on an empty/absent code_graph). Mutates in place,
    returns the index for reuse by trains/people attribution. Pure-ish (in-place)."""
    idx = area_index(bundle.get("code_graph", {}))
    for art in bundle.get("artifacts", {}).values():
        area = _area_for_path(art.get("path"), idx)
        if area is not None:
            art["code_area"] = area
    for delta in bundle.get("feature_deltas", []):
        art = bundle.get("artifacts", {}).get(delta.get("artifact"), {})
        area = art.get("code_area") or _area_for_path(delta.get("name"), idx)
        if area is not None:
            delta["area"] = area
    return idx
```

> Note: a feature_delta's `area` prefers its artifact's resolved `code_area` (artifacts carry the full path; deltas carry only `name`), so attribution order is artifacts → deltas. The `_area_for_path(delta["name"], idx)` is a defensive fallback and will usually miss (name != path) — the artifact lookup is the real source.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -k "CodeAreaAttribution" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
feat(activity): attribute code_area onto artifacts + feature_deltas

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 9: `link.py` — trains/people area attribution + `modules` field

Attribute `code_areas` onto trains (from their commits' files), `modules`/`areas` onto people (areas they authored/reviewed in), and populate the `modules` bundle field `{<area>: {commits, prs, files_changed}}`. Pure. Wired into `enrich()`.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add `attribute_train_areas`, `build_modules`, `attribute_people_areas`; extend `enrich()`)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestTrainsModulesPeopleAreas(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_graph": {"provider": "directory", "areas": [
                {"id": "avm/res/network/firewall-policy", "label": "firewall-policy",
                 "paths": ["avm/res/network/firewall-policy/main.bicep"],
                 "edges": []},
                {"id": "docs", "label": "docs",
                 "paths": ["docs/firewall.md"], "edges": []},
            ]},
            "commits": [
                {"sha": "c1", "author": "alice", "pr": 42,
                 "files": ["avm/res/network/firewall-policy/main.bicep"]},
                {"sha": "c2", "author": "bob", "pr": 42,
                 "files": ["docs/firewall.md"]},
            ],
            "prs": [{"number": 42, "author": "alice", "reviewers": ["carol"],
                     "url": "https://github.com/o/r/pull/42"}],
            "issues": [],
            "trains": [{"id": "train-pr-42", "prs": [42], "commits": ["c1", "c2"],
                        "root_issue": None, "code_areas": []}],
            "people": {},
        }

    def test_trains_gain_their_commits_code_areas(self):
        b = self._bundle()
        link.attribute_train_areas(b, link.area_index(b["code_graph"]))
        t = b["trains"][0]
        self.assertEqual(set(t["code_areas"]),
                         {"avm/res/network/firewall-policy", "docs"})

    def test_modules_field_aggregates_per_area(self):
        b = self._bundle()
        link.build_modules(b, link.area_index(b["code_graph"]))
        mods = b["modules"]
        fp = mods["avm/res/network/firewall-policy"]
        self.assertEqual(fp["commits"], 1)
        self.assertEqual(fp["files_changed"], 1)
        self.assertIn(42, [] if isinstance(fp["prs"], int) else None) or True
        # prs is a count of distinct PRs that touched the area
        self.assertEqual(fp["prs"], 1)

    def test_people_gain_modules_and_areas(self):
        b = self._bundle()
        idx = link.area_index(b["code_graph"])
        link.attribute_people_areas(b, idx)
        alice = b["people"]["alice"]
        self.assertIn("avm/res/network/firewall-policy", alice["modules"])

    def test_enrich_fills_all_phase3b_attribution(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            bundle = link.enrich(json.load(fh))
        # at least one artifact and one feature_delta now carry a real area
        arts = bundle["artifacts"]
        self.assertTrue(any(a["code_area"] is not None for a in arts.values()))
        self.assertTrue(any(d["area"] is not None for d in bundle["feature_deltas"]))
        # trains carry code_areas; modules populated
        self.assertTrue(any(t.get("code_areas") for t in bundle["trains"]))
        self.assertTrue(bundle["modules"])
```

> `bundle_p3b.json` is created in Task 12; this `test_enrich_fills_all_phase3b_attribution` will fail at the `open(...)` until then. Either land Task 12's fixture first, or mark this single method `@unittest.skipUnless(os.path.exists(os.path.join(FIX, "bundle_p3b.json")), "fixture pending")` and remove the guard once Task 12 commits the fixture. (The other three methods in this class do not need the fixture.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "TrainsModulesPeopleAreas" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'attribute_train_areas'`.

- [ ] **Step 3: Implement the three folds + extend `enrich()`**

Add to `link.py` after `attribute_code_areas`:

```python
def _commit_areas(commit, idx):
    """Distinct area ids touched by a commit's files."""
    areas = set()
    for f in commit.get("files", []):
        area = idx.get(f)
        if area is not None:
            areas.add(area)
    return areas


def attribute_train_areas(bundle, idx):
    """Set each train's `code_areas` from its commits' files. In place. Pure."""
    by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    for t in bundle.get("trains", []):
        areas = set()
        for sha in t.get("commits", []):
            c = by_sha.get(sha)
            if c:
                areas |= _commit_areas(c, idx)
        t["code_areas"] = sorted(areas)
    return bundle


def build_modules(bundle, idx):
    """Populate bundle['modules'] = {<area>: {commits, prs, files_changed}}.

    Counts per area: distinct commits, distinct PRs, and distinct files changed
    across the window's commits. Pure (in place)."""
    mods = {}
    for c in bundle.get("commits", []):
        pr = c.get("pr")
        for f in c.get("files", []):
            area = idx.get(f)
            if area is None:
                continue
            m = mods.setdefault(
                area, {"_commits": set(), "_prs": set(), "_files": set()})
            m["_commits"].add(c["sha"])
            if pr is not None:
                m["_prs"].add(pr)
            m["_files"].add(f)
    bundle["modules"] = {
        area: {"commits": len(m["_commits"]), "prs": len(m["_prs"]),
               "files_changed": len(m["_files"])}
        for area, m in mods.items()}
    return bundle


def attribute_people_areas(bundle, idx):
    """Give each authoring/reviewing person their modules + areas.

    A person's modules = the areas of files in commits they authored; areas mirror
    modules (the directory-provider id doubles as the area). Reviewers inherit the
    areas of the PRs they reviewed. Creates minimal people entries as needed. Pure
    (in place)."""
    people = bundle.setdefault("people", {})

    def touch(login, area):
        if not login or area is None:
            return
        p = people.setdefault(login, {"modules": [], "areas": []})
        if area not in p["modules"]:
            p.setdefault("modules", []).append(area)
        if area not in p.setdefault("areas", []):
            p["areas"].append(area)

    by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    for c in bundle.get("commits", []):
        for area in _commit_areas(c, idx):
            touch(c.get("author"), area)
    # reviewers inherit their PR's commit areas via the trains map.
    pr_commits = {}
    for c in bundle.get("commits", []):
        if c.get("pr") is not None:
            pr_commits.setdefault(c["pr"], []).append(c["sha"])
    for pr in bundle.get("prs", []):
        areas = set()
        for sha in pr_commits.get(pr.get("number"), []):
            c = by_sha.get(sha)
            if c:
                areas |= _commit_areas(c, idx)
        for reviewer in pr.get("reviewers", []):
            for area in areas:
                touch(reviewer, area)
    # normalize lists deterministically
    for p in people.values():
        if "modules" in p:
            p["modules"] = sorted(p["modules"])
        if "areas" in p:
            p["areas"] = sorted(p["areas"])
    return bundle
```

Then extend `enrich()` (after `compute_feature_deltas`):

```python
def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets,
    the Phase 3a narrative substrate (artifacts/timeline/feature_deltas), and the
    Phase 3b code-area attribution + label facets."""
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    bundle["artifacts"] = build_artifacts(bundle)
    bundle["timeline"] = build_timeline(bundle)
    bundle["feature_deltas"] = compute_feature_deltas(bundle)
    # Phase 3b: attribute code areas everywhere the schema reserved a null.
    idx = attribute_code_areas(bundle)
    attribute_train_areas(bundle, idx)
    build_modules(bundle, idx)
    attribute_people_areas(bundle, idx)
    return bundle
```

> `build_trains` already builds trains without `code_areas`; `attribute_train_areas` adds the key. Add `"code_areas": []` to the train dict built in `build_trains` so the key is always present (a one-line addition next to `"commits"`), keeping the schema stable even on bundles with no `code_graph`. **This does not change any existing assertion** (no current test asserts the exact train-dict key set; the integration gate checks specific keys, not equality).

- [ ] **Step 4: Add `code_areas` to the train dict in `build_trains`**

In `build_trains`, the appended train dict gains `"code_areas": []` (filled later by `attribute_train_areas`):

```python
        trains.append({
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": train_kind,
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": sorted(shas),
            "code_areas": [],
            "outcome": "shipped",
            "evidence": evidence,
        })
```

- [ ] **Step 5: Run the full link suite**

Run: `python3 -m pytest test_link.py -v`
Expected: PASS — all Phase 1/2/3a link tests (incl. idempotency: the new folds are pure functions of `code_graph`/`commits`, so re-running `enrich` recomputes identical maps) + the four Phase 3b classes. The Phase 1/2/3a fixtures have no `code_graph`, so `idx` is empty, all `code_area`/`area` stay null, `modules` stays `{}`, and trains get `code_areas: []` — every prior assertion holds.

> Idempotency check: `test_enrich_is_idempotent_and_populates_both` (Phase 2) re-runs `enrich` and compares trains. Trains now carry `code_areas`, but it is recomputed deterministically (empty on those fixtures), so the comparison still holds. Confirm by reading that assertion — it compares the trains list, which is stable.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
feat(activity): attribute code areas to trains/people + build modules field

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 10: `link.py` provenance — verify attribution does not break the lint

Phase 3a/2 carry a provenance lint asserting every narrative-bearing fact carries a well-formed ref. Phase 3b adds attribution fields (`code_area`, `area`, `code_areas`, `modules`, `people.modules`) but **adds no new narrative claims that need their own ref** — areas resolve to the existing artifact/commit refs. This task is a guard: a focused test that the attribution did not strip or malform any existing ref, plus a check that `modules` counts are internally consistent.

**Files:**
- Test: `.claude/skills/activity-overview/test_link.py` (add a small consistency class; no production change expected)

- [ ] **Step 1: Write the consistency test**

Add a new class to `test_link.py`:

```python
class TestPhase3bConsistency(unittest.TestCase):
    def test_attribution_preserves_artifact_and_delta_refs(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = link.enrich(json.load(fh))
        for d in b["feature_deltas"]:
            self.assertTrue(str(d["url"]).startswith("https://"))
            self.assertIn(d["artifact"], b["artifacts"])
        for a in b["artifacts"].values():
            for ev in a["lifecycle"]:
                self.assertTrue(str(ev["ref"]["url"]).startswith("https://"))

    def test_modules_counts_are_non_negative_and_sum_sane(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = link.enrich(json.load(fh))
        for area, m in b["modules"].items():
            self.assertGreaterEqual(m["commits"], 1)
            self.assertGreaterEqual(m["files_changed"], 1)
            self.assertGreaterEqual(m["prs"], 0)

    def test_train_code_areas_are_known_area_ids(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = link.enrich(json.load(fh))
        known = {a["id"] for a in b["code_graph"]["areas"]}
        for t in b["trains"]:
            for area in t.get("code_areas", []):
                self.assertIn(area, known)
```

> These read `bundle_p3b.json` (Task 12). If implementing strictly in order, land Task 12's fixture first or guard with `@unittest.skipUnless(os.path.exists(...))` as in Task 9, removing the guard after Task 12.

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest test_link.py -k "Phase3bConsistency" -v`
Expected: PASS once `bundle_p3b.json` exists (Task 12). No production code change should be needed; if a ref is malformed, fix the offending fold rather than the test.

- [ ] **Step 3: Commit** (only if there is a change — otherwise fold this test into Task 12's commit)

```bash
git add .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
test(activity): guard Phase 3b attribution preserves refs + module counts

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 11: `render.py` — contributor_graph + kind_breakdown diagrams

Two new pure emitters, registered in `render()` so the manifest gains `contributor_graph` + `kind_breakdown`. `emit_contributor_graph` is a Mermaid **flowchart** of people↔code-area (or people↔train) edges. `emit_kind_breakdown` is issues-by-`kind` (the spec palette says `pie`; this plan uses `pie` to match `kind_breakdown — pie`).

**Diagram-type choice (recorded):** `contributor_graph` → `flowchart` (the spec palette entry; people↔module/train edges are a node-link graph). `kind_breakdown` → `pie` (the spec palette entry; an at-a-glance feature/bug/idea mix where proportions are the point). The xychart-beta bar already serves `deltas_bar`; reusing `pie` here matches the palette and keeps the kind mix readable as shares.

**Files:**
- Modify: `.claude/skills/activity-overview/render.py` (add both emitters + register in `render()`)
- Test: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Add new classes to `test_render.py`. Add a Phase-3b helper bundle:

```python
def _p3b_bundle():
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "people": {
            "alice": {"modules": ["avm/res/network/firewall-policy"],
                      "areas": ["avm/res/network/firewall-policy"]},
            "carol": {"modules": ["docs"], "areas": ["docs"]},
        },
        "modules": {
            "avm/res/network/firewall-policy": {"commits": 2, "prs": 1, "files_changed": 3},
            "docs": {"commits": 1, "prs": 1, "files_changed": 1},
        },
        "issues": [
            {"number": 1, "kind": "feature"}, {"number": 2, "kind": "feature"},
            {"number": 3, "kind": "bug"}, {"number": 4, "kind": "module-request"},
            {"number": 5, "kind": "other"},
        ],
    }


class TestContributorGraph(unittest.TestCase):
    def test_flowchart_header_and_people_area_edges(self):
        mmd = render.emit_contributor_graph(_p3b_bundle())
        self.assertTrue(mmd.startswith("flowchart"))
        self.assertIn("alice", mmd)
        self.assertIn("firewall-policy", mmd)
        # an edge arrow connects a person to an area
        self.assertIn("-->", mmd)

    def test_placeholder_when_no_people(self):
        mmd = render.emit_contributor_graph({"meta": {}, "people": {}, "modules": {}})
        self.assertTrue(mmd.startswith("flowchart"))
        self.assertIn("No contributor", mmd)


class TestKindBreakdown(unittest.TestCase):
    def test_pie_counts_by_kind(self):
        mmd = render.emit_kind_breakdown(_p3b_bundle())
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn('"feature" : 2', mmd)
        self.assertIn('"bug" : 1', mmd)
        self.assertIn('"module-request" : 1', mmd)

    def test_pie_placeholder_when_no_issues(self):
        mmd = render.emit_kind_breakdown({"meta": {}, "issues": []})
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn("No issues", mmd)


class TestRenderManifestP3b(unittest.TestCase):
    def test_render_includes_phase3b_diagrams(self):
        out = render.render(_p3b_bundle())
        self.assertTrue(out["contributor_graph"].startswith("flowchart"))
        self.assertTrue(out["kind_breakdown"].startswith("pie"))
        # Phase 2/3a diagrams still present
        for key in ("buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"):
            self.assertIn(key, out)

    def test_write_diagrams_manifest_gains_two_more_keys(self):
        b = _p3b_bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
        b["prs"] = []; b["releases"] = []
        b["artifacts"] = {}; b["feature_deltas"] = []
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(b, os.path.join(d, "diagrams"))
            self.assertEqual(
                set(real),
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
                 "contributor_graph", "kind_breakdown"})
            self.assertIn("contributor_graph", b["diagrams"])
            self.assertIn("kind_breakdown", b["diagrams"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "ContributorGraph or KindBreakdown or RenderManifestP3b" -v`
Expected: FAIL with `AttributeError: module 'render' has no attribute 'emit_contributor_graph'`.

- [ ] **Step 3: Implement both emitters + register them**

Add to `render.py` after `emit_deltas_bar`:

```python
def _node_id(prefix, text):
    """A safe Mermaid node id from arbitrary text (alnum + underscore)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in (text or ""))
    return f"{prefix}_{safe}"[:60]


def emit_contributor_graph(bundle):
    """A Mermaid `flowchart` of people <-> code-area edges.

    Each person links to the areas they authored/reviewed in (from `people.modules`).
    Falls back to people<->train edges only if no module data exists. Derived from
    existing bundle fields."""
    people = bundle.get("people", {})
    lines = ["flowchart LR"]
    edges = []
    area_nodes = {}
    person_nodes = {}
    for login, p in sorted(people.items()):
        mods = p.get("modules") or p.get("areas") or []
        if not mods:
            continue
        pid = _node_id("p", login)
        person_nodes[pid] = login
        for area in mods:
            aid = _node_id("a", area)
            area_nodes[aid] = _area_tail(area)
            edges.append((pid, aid))
    if not edges:
        lines.append("    none[No contributor data]")
        return "\n".join(lines) + "\n"
    for pid, login in sorted(person_nodes.items()):
        lines.append(f'    {pid}["{_flow_label(login)}"]')
    for aid, label in sorted(area_nodes.items()):
        lines.append(f'    {aid}("{_flow_label(label)}")')
    for pid, aid in sorted(set(edges)):
        lines.append(f"    {pid} --> {aid}")
    return "\n".join(lines) + "\n"


def _area_tail(area):
    return (area or "").rstrip("/").split("/")[-1] or area


def _flow_label(text):
    """Sanitise a flowchart label: drop quotes/newlines that would break the node."""
    clean = (text or "").replace('"', "'").replace("\n", " ")
    return clean.strip()[:40] or "?"


def emit_kind_breakdown(bundle):
    """A Mermaid `pie` of issues by `kind` (feature/bug/idea/...). The at-a-glance
    kind mix; proportions are the point, so `pie` (the spec palette entry)."""
    counts = {}
    for issue in bundle.get("issues", []):
        kind = issue.get("kind") or "other"
        counts[kind] = counts.get(kind, 0) + 1
    lines = ["pie showData", "    title Issues by kind"]
    if not counts:
        lines.append('    "No issues" : 1')
        return "\n".join(lines) + "\n"
    for kind in sorted(counts, key=lambda k: (-counts[k], k)):
        lines.append(f'    "{kind}" : {counts[kind]}')
    return "\n".join(lines) + "\n"
```

Then register both in `render()`:

```python
def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
        "content_timeline": emit_content_timeline(bundle),
        "deltas_bar": emit_deltas_bar(bundle),
        "contributor_graph": emit_contributor_graph(bundle),
        "kind_breakdown": emit_kind_breakdown(bundle),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -k "ContributorGraph or KindBreakdown or RenderManifestP3b" -v`
Expected: PASS.

- [ ] **Step 4a: Reconcile the Phase 2/3a manifest assertions (required)**

The Phase 2/3a render tests pin the manifest set. Phase 3b legitimately grows it by two more keys (`contributor_graph`, `kind_breakdown`), so any **size-pinning** assertion must be relaxed to a superset/per-key check — the single justified edit to pre-existing assertions in Phase 3b, because the manifest size is exactly the contract this phase extends (and the spec schema already lists both diagrams as manifest members ~line 497).

Find every assertion in `test_render.py` that pins the manifest set and relax it. Phase 3a's `TestEndToEndOfflineP3.test_link_then_render_builds_full_substrate` and `TestRenderManifestP3.test_write_diagrams_manifest_gains_two_keys` assert `set(real) == {four keys}`. Change those to a subset/superset check:

```python
            # Phase 3b grows the manifest to six diagrams; assert the earlier
            # keys remain present rather than pinning the exact set.
            self.assertLessEqual(
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"},
                set(real))
```

(Apply the same relaxation to any other `assertEqual(set(...), {<4 keys>})` / `assertEqual(set(...), {<2 keys>})` manifest comparison left in the file. Leave every non-manifest assertion untouched.) Note the relaxations in the commit body.

- [ ] **Step 5: Run the full render suite**

Run: `python3 -m pytest test_render.py -v`
Expected: PASS — Phase 2/3a render tests (with the relaxed manifest assertions) + the new Phase 3b emitters; the real-mmdc test stays skipped.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "$(cat <<'EOF'
feat(activity): emit contributor_graph + kind_breakdown diagrams

Relaxes the render manifest-size assertions to superset checks, since the
diagrams manifest legitimately grows by contributor_graph + kind_breakdown.

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 12: Phase 3b fixture + end-to-end offline integration test

A single pre-link fixture `bundle_p3b.json` (carrying `code_events` + a directory `code_graph` + a `label_taxonomy` + faceted issues) drives an end-to-end `link.enrich → render` test asserting the full Phase 3b slice: filled `code_area`/`area`, train `code_areas`, `modules`, `people` areas, and the six-diagram manifest. This fixture also satisfies the Task 9/10 tests that read it.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/bundle_p3b.json`
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Create the fixture**

Write `.claude/skills/activity-overview/fixtures/bundle_p3b.json` (a pre-link bundle; `artifacts`/`timeline`/`feature_deltas`/`trains`/`buckets`/`modules`/`people` filled by `enrich`):

```json
{
  "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31",
           "ref_date": "2026-05-31", "period": {"from": "2026-05-01", "to": "2026-05-31"}},
  "commits": [
    {"sha": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1",
     "message": "Add policy example (#42)", "pr": null, "author": "alice",
     "files": ["avm/res/network/firewall-policy/examples/basic/main.bicep",
               "avm/res/network/firewall-policy/main.bicep"]},
    {"sha": "c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4",
     "message": "Drop stale firewall doc", "pr": null, "author": "dave",
     "files": ["docs/firewall.md"]}
  ],
  "code_events": [
    {"commit": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1", "author": "alice",
     "date": "2026-05-03", "change": "add",
     "path": "avm/res/network/firewall-policy/examples/basic/main.bicep"},
    {"commit": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1", "author": "alice",
     "date": "2026-05-03", "change": "add", "path": "docs/firewall.md"},
    {"commit": "c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4", "author": "dave",
     "date": "2026-05-25", "change": "delete", "path": "docs/firewall.md"}
  ],
  "code_graph": {"provider": "directory", "areas": [
    {"id": "avm/res/network/firewall-policy", "label": "firewall-policy",
     "paths": ["avm/res/network/firewall-policy/examples/basic/main.bicep",
               "avm/res/network/firewall-policy/main.bicep"], "edges": []},
    {"id": "docs", "label": "docs", "paths": ["docs/firewall.md"], "edges": []}
  ]},
  "code_owners": {"avm/res/network/": ["alice", "bob"], "docs/": ["dave"]},
  "label_taxonomy": {
    "area": {"area:": ["area: networking", "area: storage"]},
    "kind": {"Type:": ["Type: Bug", "Type: Feature"]},
    "source": "auto"},
  "prs": [
    {"number": 42, "title": "Add policy example", "merged": true, "state": "closed",
     "merged_at": "2026-05-10T12:00:00Z", "closed_at": "2026-05-10T12:00:00Z",
     "milestone": "v1.2.0", "labels": ["area: networking", "Type: Feature"],
     "facets": {"area": "area: networking", "priority": null, "status": null,
                "lifecycle": null},
     "author": "alice", "reviewers": ["carol"], "closes": [17],
     "crossref_issues": [], "url": "https://github.com/o/r/pull/42",
     "review_comments": [], "comments_list": []}
  ],
  "issues": [
    {"number": 17, "title": "Support policy param", "kind": "feature",
     "facets": {"area": "area: networking", "priority": null, "status": null,
                "lifecycle": null},
     "state": "closed", "state_reason": "completed", "milestone": "v1.2.0",
     "closed_at": "2026-05-10T12:00:00Z", "updated_at": "2026-05-10T12:00:00Z",
     "labels": ["area: networking", "Type: Feature"],
     "url": "https://github.com/o/r/issues/17",
     "comments_list": [], "reactions": {"+1": 0, "-1": 0, "heart": 0, "hooray": 0, "total": 0},
     "open_high_activity": false},
    {"number": 18, "title": "Add storage module", "kind": "module-request",
     "facets": {"area": "area: storage", "priority": null, "status": null,
                "lifecycle": null},
     "state": "open", "state_reason": null, "milestone": "v1.3.0",
     "closed_at": null, "updated_at": "2026-05-22T00:00:00Z",
     "labels": ["area: storage"], "url": "https://github.com/o/r/issues/18",
     "comments_list": [], "reactions": {"+1": 3, "-1": 0, "heart": 0, "hooray": 0, "total": 3},
     "open_high_activity": false}
  ],
  "milestones": [
    {"title": "v1.2.0", "number": 4, "state": "open", "due_on": "2026-05-31T00:00:00Z"},
    {"title": "v1.3.0", "number": 5, "state": "open", "due_on": "2026-06-30T00:00:00Z"}
  ],
  "releases": [], "trains": [], "artifacts": {}, "timeline": [],
  "feature_deltas": [], "modules": {}, "people": {},
  "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
}
```

Verify it parses:

Run: `python3 -c "import json; d=json.load(open('fixtures/bundle_p3b.json')); print(len(d['code_graph']['areas']), d['issues'][1]['kind'])"`
Expected: `2 module-request`

- [ ] **Step 2: Write the end-to-end test**

Add to `test_render.py`:

```python
class TestEndToEndOfflineP3b(unittest.TestCase):
    def test_link_then_render_attributes_areas_and_renders_six(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            bundle = link.enrich(json.load(fh))

        # code_area filled on the example artifact (covered by the AVM area)
        ex = bundle["artifacts"][
            link.artifact_id(
                "avm/res/network/firewall-policy/examples/basic/main.bicep")]
        self.assertEqual(ex["code_area"], "avm/res/network/firewall-policy")
        # docs artifact -> docs area
        doc = bundle["artifacts"][link.artifact_id("docs/firewall.md")]
        self.assertEqual(doc["code_area"], "docs")

        # feature_deltas carry a real area now (no longer all null)
        self.assertTrue(any(d["area"] is not None for d in bundle["feature_deltas"]))

        # train for #17/PR42 carries the firewall-policy area
        t = next(t for t in bundle["trains"] if t["id"] == "train-issue-17")
        self.assertIn("avm/res/network/firewall-policy", t["code_areas"])

        # modules populated; alice owns the firewall-policy module
        self.assertIn("avm/res/network/firewall-policy", bundle["modules"])
        self.assertIn("avm/res/network/firewall-policy",
                      bundle["people"]["alice"]["modules"])

        # render: six-diagram manifest
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(bundle, os.path.join(d, "diagrams"))
            self.assertEqual(
                set(real),
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
                 "contributor_graph", "kind_breakdown"})
```

- [ ] **Step 3: Run the test**

Run: `python3 -m pytest test_render.py -k "EndToEndOfflineP3b" -v`
Expected: PASS.

> Note: `link.enrich` builds the train from PR 42 (merged, closes #17) → `train-issue-17`; commit c1 message `"Add policy example (#42)"` resolves to PR 42 via `attach_commit_prs`, and its files live under `avm/res/network/firewall-policy`, so the train's `code_areas` and alice's `modules` both gain that area.

- [ ] **Step 4: Remove any `skipUnless` guards added in Tasks 9/10**

If Tasks 9/10 guarded their fixture-reading methods with `@unittest.skipUnless(os.path.exists(...))`, remove those guards now that `bundle_p3b.json` exists, and re-run:

Run: `python3 -m pytest test_link.py -k "TrainsModulesPeopleAreas or Phase3bConsistency" -v`
Expected: PASS (no skips).

- [ ] **Step 5: Run the entire suite**

Run: `python3 -m pytest -v` (from the skill dir)
Expected: PASS — every Phase 1/2/3a/3b test green; the one real-mmdc test skipped.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/fixtures/bundle_p3b.json .claude/skills/activity-overview/test_render.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
test(activity): end-to-end offline code-area attribution + six-diagram render

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 13: Report template + SKILL + BUNDLE docs

Surface the new data: shipped grouped by **code area**, module ownership (`code_owners` + `people.modules`), the issue-kind breakdown, and facet-aware grouping. Document `code_graph.{provider,areas}`, `label_taxonomy`, `facets`, `kind`, `code_owners`, `modules`, and the now-filled `code_area`/`area` — noting graphify is optional (+ its real schema) and the deferrals.

**Files:**
- Modify: `.claude/skills/activity-overview/report-template.md`
- Modify: `.claude/skills/activity-overview/SKILL.md`
- Modify: `.claude/skills/activity-overview/BUNDLE.md`

- [ ] **Step 1: Extend `report-template.md`**

Update the "Shipped this period" section to group by code area, and append the new sections (module ownership + issue-kind breakdown). Replace the existing "## Shipped this period" block body with a code-area-grouped form, and append at the end:

```markdown

## Shipped by code area

Group `buckets.shipped` by each item's train `code_areas` (from `code_graph`).
For each area, list the shipped PRs/issues with their train link. Items with no
resolved area fall under "Unattributed".

### {area label} (`{area id}`)

- [{title}]({url}) (#{number}) — train `{train.id}`

## Module ownership

From `code_owners` + `people.modules`/`modules`: who owns and who touched each
module this window.

| Module | Owners (CODEOWNERS) | Top contributors | Commits | PRs | Files |
|--------|---------------------|------------------|---------|-----|-------|
| `{area}` | {code_owners[glob]} | {people whose modules include area} | {modules[area].commits} | {modules[area].prs} | {modules[area].files_changed} |

Embeds `diagrams.contributor_graph` (people ↔ code-area edges):

```mermaid
{contents of diagrams.contributor_graph}
```

## Issue kinds

The `kind` mix across the window's issues (feature / module-request / bug / idea /
question / docs / other), derived from native issue types → label facets →
template → heuristic.

```mermaid
{contents of diagrams.kind_breakdown}
```
```

Also add a one-line note under "Feature changes" that `area` is now populated (no longer null) when the path resolves to a code area.

- [ ] **Step 2: Update `SKILL.md`**

Add a Phase 3b rules bullet after the Phase 3a one, and note graphify is optional in the preflight step. Append to `## Rules`:

```markdown
- Phase 3b reports additionally: **group Shipped by code area**, add **Module
  ownership** (`code_owners` + `people.modules`/`modules`, embedding
  `diagrams.contributor_graph`) and an **Issue kinds** breakdown (embedding
  `diagrams.kind_breakdown`). Each issue/PR carries `facets`
  (area/priority/status/lifecycle) and each issue a `kind`; group and label using
  them. `code_area`/`area` are now populated where a path resolves to an area.
```

And soften the preflight wording so graphify reads as optional (it is no longer required). In the preflight step, change any "graphify is required" phrasing to:

```markdown
   `graphify` is **optional** (used only for its supported languages); when it is
   absent — e.g. on Bicep/Terraform repos — the **directory provider** supplies
   code areas, so no install is required for code-area attribution to work.
```

- [ ] **Step 3: Update `BUNDLE.md`**

Append a Phase 3b section to `BUNDLE.md`:

```markdown

## Phase 3b fields (code areas + label facets)

- **code_graph** `{ "provider": "directory|graphify", "areas": [{ "id", "label",
  "paths": [...], "edges": [] }] }`. The **directory provider** (primary,
  zero-dep, offline) maps each tracked file to its module directory (AVM
  `avm/res/<svc>/<module>/`, any `main.bicep` dir, Terraform `modules/<name>/` or
  any `*.tf` dir, else a top-2-segment fallback); the area `id` is that directory.
  **graphify** is an OPTIONAL provider for its ~25 tree-sitter languages — it reads
  graphify's real `graph.json` (top keys `nodes`/`links`; each node carries an
  integer `community` + `source_file`; **no** top-level `communities` list; edges
  live under `links`) and groups nodes by `community` into `community:<n>` areas.
  graphify does NOT parse Bicep/HCL, so on those repos the directory provider runs.
  **Deferred:** dependency `edges` between areas (Bicep `dependsOn`, `tree-sitter-hcl`).
- **code_owners** `{ "<path|glob>": ["login", ...] }` — parsed from the clone's
  CODEOWNERS (`.github/`/root/`docs/`); `@org/team` and `@user` kept as logins.
- **label_taxonomy** `{ "<facet>": { "<namespace>": ["label", ...] }, "source":
  "auto|config|merged" }` — auto-detected structured label namespaces mapped to
  facets (`area`/`priority`/`status`/`lifecycle`/`kind`), with optional config
  override/extend. Degrades to `{ "source": "auto" }` (no facets) on unstructured labels.
- **issues[]/prs[]** gain **facets** `{ area, priority, status, lifecycle }`
  (each the first matching label or null). **issues[]** gain **kind** ∈
  `feature|module-request|bug|idea|question|docs|other` (native issue type →
  label kind facet → template filename → title/body heuristic → other).
- **artifacts[].code_area** and **feature_deltas[].area** are now **populated**
  (were null in Phase 3a) when the path resolves to a `code_graph` area; null otherwise.
- **trains[].code_areas** — the distinct areas of a train's commits' files.
- **modules** `{ "<area>": { "commits", "prs", "files_changed" } }` — per-area
  activity counts across the window's commits.
- **people[].modules / people[].areas** — the areas a person authored (their
  commits' files) or reviewed (their reviewed PRs' areas).
- **diagrams{}** now also maps `contributor_graph` (Mermaid `flowchart`,
  people↔code-area edges) and `kind_breakdown` (Mermaid `pie`, issues by kind).
- **Still deferred:** symbol/inline-comment artifacts, dependency-edge enrichment,
  `hunk`/`before`/`after`/`detail` on feature_deltas, multi-repo aggregation.
```

- [ ] **Step 4: Verify the docs mention the new pieces**

Run:
```bash
grep -c "code area\|Module ownership\|Issue kinds\|contributor_graph\|kind_breakdown" report-template.md
grep -c "code area\|graphify\|facets\|kind\|optional" SKILL.md
grep -c "code_graph\|label_taxonomy\|code_owners\|modules\|facets\|kind\|contributor_graph" BUNDLE.md
```
Expected: each `grep -c` prints a non-zero count (≥3, ≥2, ≥5 respectively).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/SKILL.md .claude/skills/activity-overview/BUNDLE.md
git commit -m "$(cat <<'EOF'
docs(activity): document Phase 3b code_graph/facets/kind/modules + report sections

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 14: Extend the live integration smoke test (per-phase gate — REQUIRED)

Extend `.github/workflows/activity-overview-integration.yml`'s assertion block to the **Phase 3b** contract, and **run it green on real Bicep data** before the phase is done. On Bicep, graphify is absent → the directory provider runs and is what is asserted. KEEP every Phase 1/2/3a assertion. Do NOT add a graphify install step.

**Files:**
- Modify: `.github/workflows/activity-overview-integration.yml`

- [ ] **Step 1: Extend the assertion block**

In the `Assert ...` step's inline Python, add Phase 3b assertions **after** the Phase 3a block (before the final `print`). Append:

```python
          # 11. Phase 3b: code_graph is the directory provider with non-empty areas.
          cg = b.get("code_graph", {})
          assert cg.get("provider") == "directory", \
              f"expected directory provider on Bicep (graphify absent), got {cg.get('provider')}"
          areas = cg.get("areas", [])
          assert areas, "code_graph.areas is empty — directory provider produced no areas"
          area_ids = set()
          for a in areas:
              assert a.get("id") and isinstance(a.get("paths"), list) and a["paths"], \
                  f"area missing id/paths: {a}"
              assert a.get("edges") == [], "area edges are deferred (must be empty)"
              area_ids.add(a["id"])

          # 12. Phase 3b: code_area/area now RESOLVE (no longer all null) where covered.
          arts = b.get("artifacts", {})
          attributed_art = sum(1 for a in arts.values() if a.get("code_area") is not None)
          for a in arts.values():
              if a.get("code_area") is not None:
                  assert a["code_area"] in area_ids, \
                      f"artifact code_area {a['code_area']} not a known area"
          deltas = b.get("feature_deltas", [])
          for d in deltas:
              if d.get("area") is not None:
                  assert d["area"] in area_ids, f"delta area {d['area']} not a known area"
          # On a busy Bicep window the directory provider must attribute SOMETHING.
          if arts:
              assert attributed_art > 0, \
                  "no artifact resolved to a code area — attribution is broken"

          # 13. Phase 3b: label_taxonomy present (possibly empty) + facets/kind on issues.
          tax = b.get("label_taxonomy", {})
          assert "source" in tax, "label_taxonomy missing source marker"
          assert tax["source"] in {"auto", "config", "merged"}, tax["source"]
          valid_kinds = {"feature", "module-request", "bug", "idea",
                         "question", "docs", "other"}
          for i in issues:
              f = i.get("facets", {})
              assert set(f) >= {"area", "priority", "status", "lifecycle"}, \
                  f"issue {i['number']} facets missing keys: {f}"
              assert i.get("kind") in valid_kinds, \
                  f"issue {i['number']} kind {i.get('kind')} invalid"
          for p in prs:
              f = p.get("facets", {})
              assert set(f) >= {"area", "priority", "status", "lifecycle"}, \
                  f"pr {p['number']} facets missing keys: {f}"

          # 14. Phase 3b: trains carry code_areas; modules populated; people areas.
          for t in trains:
              assert isinstance(t.get("code_areas", []), list), t["id"]
              for area in t.get("code_areas", []):
                  assert area in area_ids, f"{t['id']} code_area {area} unknown"
          mods = b.get("code_owners", {})
          assert isinstance(mods, dict), "code_owners must be a dict"
          modules = b.get("modules", {})
          assert isinstance(modules, dict), "modules must be a dict"
          for area, m in modules.items():
              assert m["commits"] == m["commits"] and m["files_changed"] >= 1, area

          # 15. Phase 3b: diagrams manifest now includes the two new diagrams.
          assert set(dg) >= {"buckets_pie", "timeline_gantt", "content_timeline",
                             "deltas_bar", "contributor_graph", "kind_breakdown"}, \
              "diagrams manifest missing Phase 3b keys"
```

Also extend the final `print(...)` to surface the new counts:

```python
          print(f"  phase3b: provider={cg.get('provider')} areas={len(areas)} "
                f"attributed_artifacts={attributed_art} modules={len(modules)} "
                f"label_taxonomy_source={tax.get('source')} "
                f"code_owners={len(b.get('code_owners', {}))}")
```

And update the step name / maintenance header to say **Phase 3b**:

- Change the assert step `name:` from `... (Phase 3a)` to `... (Phase 3b)`.
- Update the `MAINTENANCE` comment to mention the Phase 3b additions (directory code-area provider + non-empty areas on Bicep, resolved `code_area`/`area`, label_taxonomy + facets/kind, trains code_areas, modules, code_owners, and the contributor_graph + kind_breakdown manifest entries).
- Update the stale comment block (the `# Phase 3 will add graphify here ...` note around line 98) to state graphify is **optional and intentionally not installed** — the directory provider is what runs on Bicep, and graphify is exercised via the unit fixture only.

- [ ] **Step 2: Validate the workflow YAML + embedded Python parse locally**

Run (from the repo root):
```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/activity-overview-integration.yml'))" 2>/dev/null \
  || python3 -c "print('PyYAML absent; skip YAML lint (CI validates on push)')"
```
Expected: no error (or the skip notice if PyYAML is absent).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/activity-overview-integration.yml
git commit -m "$(cat <<'EOF'
ci(activity): assert Phase 3b contract (directory provider, areas, facets/kind)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

- [ ] **Step 4: RUN THE GATE ON REAL DATA (required before the phase is "done")**

This workflow **MUST** be run manually and be **green on real Bicep data** before Phase 3b is complete. After pushing (Task 15), trigger it:

```bash
gh workflow run "activity-overview integration (live smoke test)" \
  -f owner=Azure -f repo=bicep-registry-modules
# then watch it:
gh run watch "$(gh run list --workflow='activity-overview integration (live smoke test)' \
  --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Confirm the run is green: `code_graph.provider == "directory"` with a non-empty `areas` list, at least one artifact attributed to a real area, `label_taxonomy` present, every issue carrying `facets` + a valid `kind`, and the manifest including `contributor_graph` + `kind_breakdown`. If it goes red on real-repo data (e.g. the directory provider produced zero areas, an area path mismatch, or an unexpected `kind`), fix `gather.py`/`link.py` and re-run until green — do NOT mark the phase done on a red gate. (An `ACTIVITY_TEST_TOKEN` expiry is a token issue, not a code regression, but the gate still must be made green before sign-off.)

---

## Task 15: Push the branch

- [ ] **Step 1: Confirm clean tree and full suite**

Run:
```bash
git status --short
cd .claude/skills/activity-overview && python3 -m pytest -q && cd -
```
Expected: no uncommitted changes; all tests pass (one skip — the real-mmdc test).

- [ ] **Step 2: Push**

```bash
git push -u origin "$(git rev-parse --abbrev-ref HEAD)"
```
(Retry on network error with exponential backoff: 2s, 4s, 8s, 16s.)

- [ ] **Step 3: Run the live gate (Task 14 Step 4) and confirm green.**

Phase 3b is done only when the full offline suite is green AND the live integration workflow is green on real Bicep data with the Phase 3b assertions.

---

## Self-Review

### 1. Locked-scope coverage (every A-E item → task)

| Scope | Locked-scope item | Task(s) |
|-------|---|---|
| A.1 | Directory provider (primary, pure): `classify_code_area` + `build_directory_areas` → `code_graph.provider="directory"` | Task 1 |
| A.2 | graphify provider (optional): `parse_graphify_graph` over recorded `graphify_graph_sample.json` (real `nodes`/`links` shape, group by `community`) | Task 2 |
| A.3 | Provider selection in `acquire()` (graphify only if on PATH + nodes; else directory; no fail-fast; injectable seam) | Task 3 (`select_code_area_provider`) + Task 7 wiring |
| A.4 | CODEOWNERS → `code_owners` (pure `parse_codeowners`) | Task 4 + Task 7 wiring (read from clone) |
| B.5 | `label_taxonomy` auto-detect (pure, config override, degrades to no-facets) | Task 5 |
| B.6 | Per-issue/PR `facets` (`apply_facets`) | Task 6 + Task 7 wiring |
| B.7 | Issue `kind` (`classify_issue_kind`: native type → label facet → template → heuristic → other; native type via thin seam) | Task 6 + Task 7 (`_repo_has_issue_types` seam, `issue["type"]` capture) |
| C.8 | `path → area id` index + attribute `code_area`/`area` onto artifacts, feature_deltas, commits/trains, people; populate `modules` | Task 8 (artifacts/deltas) + Task 9 (trains/people/modules) + `enrich()` wiring |
| D.9 | `emit_contributor_graph` (flowchart) + `emit_kind_breakdown` (pie) + manifest registration | Task 11 |
| E.10 | Report + SKILL + BUNDLE docs (grouped by code area, ownership, kind breakdown, facet-aware; graphify optional + real schema; deferrals) | Task 13 |
| E.11 | Integration gate Phase 3b assertions (directory provider + non-empty areas, resolved area, label_taxonomy + facets/kind, manifest); KEEP P1/2/3a; run green; no graphify install | Task 14 |
| E.12 | Push the branch | Task 15 |

All A-E items map to a task. Provenance/consistency is additionally guarded in Task 10 and Task 12. Diagram-type choices (`flowchart` for `contributor_graph`, `pie` for `kind_breakdown`) match the spec palette (~lines 564/566) and are justified in Task 11.

### 2. Placeholder scan

No "TBD/TODO/handle the edge cases" left as work: every implementation step shows complete function bodies; every test step shows full assertions; every run step gives the exact `pytest`/`grep`/`gh` command and expected output. The two places that could read as open: (a) the native-issue-type fetch (Task 7 Step 3a) is a deliberately best-effort seam that degrades to `False`/`None` and is explicitly documented as such, not a stub; (b) Task 11 Step 4a relaxes only the manifest-size assertions (the single justified edit to pre-existing tests). The three fixtures are concrete literal JSON/text, not sketches.

### 3. Type / name consistency across tasks

- `classify_code_area(path, patterns) -> area_id|None` and `build_directory_areas(paths, patterns) -> {provider:"directory", areas:[{id,label,paths,edges:[]}]}` — Task 1; consumed by `select_code_area_provider` (Task 3) and `area_index` (Task 8). Area `id` is a directory path; `edges` always `[]`.
- `parse_graphify_graph(graph) -> {provider:"graphify", areas:[{id:"community:<n>",label,paths,edges:[]}]}` — Task 2; same `code_graph` shape as the directory provider, so `area_index`/`attribute_code_areas` consume either provider identically.
- `select_code_area_provider(paths, clone_dir, which, run, read_json, patterns) -> code_graph` — Task 3; produces exactly the shape `link.area_index` (Task 8) reads (`{"areas":[{"id","paths"}]}`).
- `parse_codeowners(text) -> {pattern:[login]}` — Task 4; stored as `bundle["code_owners"]` (Task 7), read by the report (Task 13) + asserted in the gate (Task 14 #14).
- `detect_label_taxonomy(labels, config) -> {facet:{namespace:[label]}, source}` — Task 5; consumed by `apply_facets`/`classify_issue_kind` (Task 6, via `_labels_in_taxonomy`) and stored as `bundle["label_taxonomy"]` (Task 7), asserted in the gate (#13).
- `apply_facets(item, taxonomy) -> {area,priority,status,lifecycle}` (all four keys, values nullable) and `classify_issue_kind(issue, taxonomy, types_present) -> kind∈_VALID_KINDS` — Task 6; stamped in `acquire()` (Task 7), read by the kind diagram (Task 11) + report (Task 13) + gate (#13). `_VALID_KINDS` is the single source of the kind set, matched by the gate's `valid_kinds`.
- `area_index(code_graph) -> {path:area_id}` and `attribute_code_areas(bundle) -> idx` — Task 8; `idx` is reused by `attribute_train_areas`/`build_modules`/`attribute_people_areas` (Task 9). `artifacts[].code_area`, `feature_deltas[].area`, `trains[].code_areas`, `modules[area].{commits,prs,files_changed}`, `people[login].{modules,areas}` are the exact fields the gate (#12/#14) and report (Task 13) read.
- `enrich()` order: attach_commit_prs → trains → buckets → artifacts → timeline → feature_deltas → attribute_code_areas → attribute_train_areas → build_modules → attribute_people_areas (Task 9). Attribution runs AFTER artifacts/feature_deltas exist; `attribute_code_areas` returns the index reused by the three subsequent folds.
- `render()` keys grow to `{buckets_pie, timeline_gantt, content_timeline, deltas_bar, contributor_graph, kind_breakdown}` — Task 11; matched by `write_diagrams`, the relaxed earlier-phase tests (Task 11 Step 4a), the Task 11/12 manifest tests, and the gate (#15).

### 4. Backward-compatibility check

- No existing fixture is mutated; three new fixtures are added (`graphify_graph_sample.json`, `codeowners_sample.txt`, `bundle_p3b.json`).
- `code_graph`/`code_owners`/`label_taxonomy`/`modules` were already reserved empty in `build_bundle`; the skeleton test loops a fixed allow-list and does not assert the absence of extra keys, so adding values keeps it green (Task 7 Step 4).
- All new attribution folds are pure functions of `code_graph`/`commits`/labels and degrade to no-ops on the Phase 1/2/3a fixtures (no `code_graph`): `idx` is empty, `code_area`/`area` stay null exactly as Phase 3a left them, `modules` stays `{}`, `people` unchanged, trains get `code_areas: []`. So `test_enrich_is_idempotent_and_populates_both` and every prior link test stay green (Task 9 Step 5).
- `build_trains` gains a `"code_areas": []` key; no existing assertion pins the train-dict key set (the gate checks specific keys, not equality), so this is non-breaking (Task 9 Step 4).
- Exactly the render manifest-size assertions change — relaxed to superset/per-key checks (Task 11 Step 4a) — justified because the diagrams manifest is the contract Phase 3b deliberately extends, and the spec schema already lists both new manifest members.
- The integration gate KEEPS every Phase 1/2/3a assertion and only appends blocks #11-#15 (Task 14).

### 5. Explicit deferrals (flagged)

- **Dependency-edge enrichment** (`code_graph.areas[].edges`) — Bicep `dependsOn`, `tree-sitter-hcl`; every area emits `edges: []` and the gate asserts it (#11). Deferred.
- **Symbol / inline-comment artifacts** — still file-granularity only (carried from Phase 3a); `artifacts[].kind` stays `readme|doc|example`. Deferred.
- **graphify in CI** — exercised ONLY via `graphify_graph_sample.json`; never installed in CI; the Bicep gate runs the directory provider and asserts `provider == "directory"` (#11). Intentionally not installed.
- **Multi-repo aggregation** — single repo per run. Deferred (Phase 6).
- **`hunk`/`before`/`after`/`detail`** on feature_deltas — still null (Phase 3a deferral, unchanged).

All deferrals are stated in the LOCKED SCOPE section, repeated at their point of use, documented in BUNDLE.md (Task 13), and enforced where checkable in the integration gate (Task 14 #11) so a future slice that populates them deliberately flips those asserts.
