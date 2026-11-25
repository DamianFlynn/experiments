"""Bicep whole-tree (structural) module graph and cross-repo `br/public:`
dependencies (issue #92).

* The structural scan reads every tracked module entrypoint (`<area>/main.bicep`)
  once, with no `bicep build`, so the module graph does not depend on which
  modules changed in the window.
* In a manifest fold, a public-registry ref that this repo cannot resolve maps to
  the member that declares the registry path prefix (`bicep_registry_prefix`).
* `spotlight dependents owner/repo::<area>` walks the consumers of one area.

Offline: injected listing/reading seams, in-memory stores, and (for the acquire
acceptance test) real git against a throwaway local repository.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gather  # noqa: E402
import graphstore  # noqa: E402
import manifest  # noqa: E402
import spotlight  # noqa: E402
import validate  # noqa: E402

# A bicep-registry-modules-like tree: one pattern module (ptn) consumes a resource
# module (res) which consumes a leaf resource module, plus the scaffolding every
# AVM module carries.
BRM_FILES = {
    "avm/ptn/app/web/main.bicep": (
        "param name string\n"
        "module site 'br/public:avm/res/web/site:0.3.0' = {\n"
        "  name: 'site'\n}\n"
        "module gone 'br/public:avm/res/other/missing:1.0.0' = {\n  name: 'x'\n}\n"
        "module priv 'br:contoso.azurecr.io/bicep/modules/x:1.0' = {\n"
        "  name: 'p'\n}\n"),
    "avm/ptn/app/web/README.md": "# web\n",
    "avm/res/web/site/main.bicep": (
        "module plan 'br/public:avm/res/web/serverfarm:0.2.0' = {\n"
        "  name: 'plan'\n}\n"
        "module slot 'slot/main.bicep' = [for s in slots: {\n  name: s\n}]\n"),
    # a private child module: classified to its parent area, never read itself
    "avm/res/web/site/slot/main.bicep": (
        "module never 'br/public:avm/res/never/read:9.9.9' = {\n  name: 'n'\n}\n"),
    "avm/res/web/site/tests/e2e/defaults/main.test.bicep": (
        "module testDeployment '../../../main.bicep' = {\n  name: 't'\n}\n"),
    "avm/res/web/site/README.md": "# site\n",
    "avm/res/web/serverfarm/main.bicep": (
        "resource p 'Microsoft.Web/serverfarms@2022-09-01' = {\n  name: 'p'\n}\n"),
    "avm/res/web/README.md": "# the web group\n",
    "avm/utl/types/avm-common-types/main.bicep": "@export()\ntype t = string\n",
}

PTN, SITE, FARM, UTL = ("avm/ptn/app/web", "avm/res/web/site",
                        "avm/res/web/serverfarm", "avm/utl/types/avm-common-types")

# A landing-zone repo consuming AVM modules from the public registry.
LZ_FILES = {
    "main.bicep": (
        "targetScope = 'subscription'\n"
        "module hub 'modules/hub/main.bicep' = {\n  name: 'hub'\n}\n"
        "module kv 'br/public:avm/res/key-vault/vault:0.9.0' = {\n  name: 'kv'\n}\n"),
    "modules/hub/main.bicep": (
        "module vnet 'br:mcr.microsoft.com/bicep/avm/res/network/virtual-network:0.5.0'"
        " = {\n  name: 'vnet'\n}\n"
        "module grp 'br/public:avm/res/network:1.0.0' = {\n  name: 'g'\n}\n"
        "module other 'br/public:samples/hello-world:1.0.2' = {\n  name: 'h'\n}\n"
        "module mine 'br:contoso.azurecr.io/bicep/avm/res/network/nsg:1.0' = {\n"
        "  name: 'm'\n}\n"),
    "README.md": "# lz\n",
}


def _scan(files, reads=None):
    def read_text(rel):
        if reads is not None:
            reads.append(rel)
        return files[rel]
    return gather.scan_structural_bicep_areas(
        "/clone", list_files=lambda: list(files), read_text=read_text)


def _edge_set(edges):
    return {(e["to"], e["ref"], e["version"], e["resolved"], e["transitive"])
            for e in edges}


def _graph_edges(code_graph):
    return {(a["id"], e["to"], e["ref"])
            for a in code_graph["areas"] for e in a.get("edges", [])}


class TestStructuralBicepScan(unittest.TestCase):
    def test_every_module_area_and_its_direct_edges(self):
        out = _scan(BRM_FILES)
        # module areas only: no group area, no scaffold, no child-as-area
        self.assertEqual(set(out), {PTN, SITE, FARM, UTL})
        self.assertEqual(_edge_set(out[PTN]["edges"]), {
            (SITE, "br/public:avm/res/web/site:0.3.0", "0.3.0", True, False),
            # a public module this repo does not hold: unresolved, never phantom
            (None, "br/public:avm/res/other/missing:1.0.0", "1.0.0", False, False),
            # a private registry says nothing about this repo
            (None, "br:contoso.azurecr.io/bicep/modules/x:1.0", "1.0", False, False),
        })
        site = {e["to"]: e for e in out[SITE]["edges"]}
        self.assertEqual(set(site), {FARM, SITE + "/slot"})
        self.assertEqual(site[FARM]["version"], "0.2.0")
        self.assertTrue(site[FARM]["resolved"])
        # the private child keeps the build path's naming and markers
        self.assertTrue(site[SITE + "/slot"]["local"])
        self.assertEqual(site[SITE + "/slot"]["instances"], "many")
        self.assertEqual(out[FARM]["edges"], [])
        self.assertEqual(out[UTL]["edges"], [])
        for info in out.values():
            for e in info["edges"]:
                self.assertEqual(e["provider"], "bicep")
                self.assertFalse(e["transitive"])

    def test_paths_are_the_areas_bicep_files_without_scaffolding(self):
        out = _scan(BRM_FILES)
        self.assertEqual(out[SITE]["paths"], ["avm/res/web/site/main.bicep",
                                              "avm/res/web/site/slot/main.bicep"])
        self.assertEqual(out[PTN]["paths"], ["avm/ptn/app/web/main.bicep"])

    def test_reads_each_entrypoint_once_and_nothing_else(self):
        reads = []
        _scan(BRM_FILES, reads)
        self.assertEqual(sorted(reads), sorted(
            a + "/main.bicep" for a in (PTN, SITE, FARM, UTL)))

    def test_root_entrypoint_and_local_module_dirs(self):
        out = _scan(LZ_FILES)
        self.assertEqual(set(out), {"main.bicep", "modules/hub"})
        self.assertEqual(_edge_set(out["main.bicep"]["edges"]), {
            ("modules/hub", "modules/hub/main.bicep", None, True, False),
            (None, "br/public:avm/res/key-vault/vault:0.9.0", "0.9.0", False, False),
        })
        self.assertTrue(all(e["to"] is None for e in out["modules/hub"]["edges"]))
        self.assertEqual(len(out["modules/hub"]["edges"]), 4)

    def test_deterministic(self):
        rev = dict(reversed(list(BRM_FILES.items())))
        self.assertEqual(json.dumps(_scan(BRM_FILES), sort_keys=True),
                         json.dumps(_scan(rev), sort_keys=True))

    def test_build_direct_set_equals_structural_scan(self):
        # The build path (immediate edges from the source refs) and the scan use
        # one rule, so both report the same direct set for a module.
        scanned = _scan(BRM_FILES)
        modules = set(scanned)
        for aid in (PTN, SITE):
            entry = aid + "/main.bicep"
            built = gather.build_bicep_edges(
                BRM_FILES[entry], {}, entry, {aid}, gather.DEFAULT_AREA_PATTERNS,
                module_exists=modules.__contains__)
            self.assertEqual(built, scanned[aid]["edges"], aid)


class TestApplyStructuralScan(unittest.TestCase):
    def _window_graph(self):
        # Only the pattern module changed in the window; no bicep CLI, so the
        # build path left it with no edges.
        return {"provider": "directory", "areas": [
            {"id": PTN, "label": "web", "paths": ["avm/ptn/app/web/main.bicep"],
             "edges": [], "edges_status": "skipped"}]}

    def _apply(self, graph, files):
        return gather.apply_structural_scan(
            graph, "/clone", list_files=lambda: list(files),
            read_text=lambda rel: files[rel])

    def test_untouched_leaf_still_yields_full_edge_set(self):
        g = self._apply(self._window_graph(), BRM_FILES)
        self.assertEqual(
            {(s, t) for s, t, _r in _graph_edges(g) if t is not None},
            {(PTN, SITE), (SITE, FARM), (SITE, SITE + "/slot")})
        ids = [a["id"] for a in g["areas"]]
        self.assertEqual(ids[0], PTN)                        # in-window area first
        self.assertEqual(ids[1:], sorted([SITE, FARM, UTL]))  # appended by id

    def test_build_edges_win_and_are_not_duplicated(self):
        g = self._window_graph()
        built = {"to": SITE, "kind": "module",
                 "ref": "br/public:avm/res/web/site:0.3.0", "version": "0.3.0",
                 "transitive": False, "provider": "bicep", "resolved": True}
        nested = {"to": FARM, "kind": "module", "ref": "plan-deploy",
                  "version": None, "transitive": True, "provider": "bicep",
                  "resolved": True}
        g["areas"][0]["edges"] = [built, nested]
        g["areas"][0]["edges_status"] = "resolved"
        self._apply(g, BRM_FILES)
        ptn = g["areas"][0]["edges"]
        self.assertEqual(ptn[:2], [built, nested])            # kept, in order
        self.assertEqual(sum(1 for e in ptn if e["to"] == SITE), 1)
        self.assertEqual(len(ptn), 4)                         # + the 2 unresolved

    def test_terraform_and_bicep_trees_share_one_listing(self):
        files = dict(BRM_FILES)
        files["modules/net/main.tf"] = 'module "a" { source = "../sub" }\n'
        calls = []

        def list_files():
            calls.append(1)
            return list(files)
        g = gather.apply_structural_scan(
            {"areas": []}, "/clone", list_files=list_files,
            read_text=lambda rel: files[rel])
        self.assertEqual(len(calls), 1)
        ids = {a["id"] for a in g["areas"]}
        self.assertIn("modules/net", ids)
        self.assertIn(SITE, ids)


class TestStructuralIacFlag(unittest.TestCase):
    BASE = ["--owner", "o", "--repo", "r", "--from", "2026-01-01",
            "--to", "2026-01-31", "--store", "s.db"]

    def test_single_repo_default_off_and_opt_in(self):
        self.assertFalse(gather.parse_args(self.BASE).structural_iac)
        self.assertTrue(gather.parse_args(self.BASE + ["--structural-iac"])
                        .structural_iac)

    def test_manifest_members_always_scan(self):
        base = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
        m = gather._member_args(base, {"owner": "A", "repo": "b", "registry": None,
                                       "bicep_registry_prefix": None}, "x", "y")
        self.assertTrue(m.structural_iac)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestAcquireStructuralBicep(unittest.TestCase):
    """acquire() end to end against a local git repo: the leaf module's last
    change is before the window, yet the gathered graph has every edge."""

    def setUp(self):
        from test_clone_handling import GIT_TEST_ENV, _git
        self._git = _git
        self.tmp = tempfile.mkdtemp(prefix="ao-i92-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "src")
        _git("init", "-q", "-b", "main", self.src)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        old = {k: v for k, v in BRM_FILES.items() if not k.startswith("avm/ptn/")}
        self._commit(old, "2026-03-01")
        ptn = {k: v for k, v in BRM_FILES.items() if k.startswith("avm/ptn/")}
        self._commit(ptn, "2026-05-20")

    def _commit(self, files, date):
        for rel, text in files.items():
            path = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self._git("add", "-A", cwd=self.src)
        self._git("commit", "-q", "-m", "c " + date, cwd=self.src, date=date)

    def _acquire(self, *extra):
        clone = os.path.join(self.tmp, "ws", "r-clone")
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", "2026-05-18",
            "--to", "2026-05-31", "--clone-dir", clone,
            "--store", os.path.join(self.tmp, "s.db"), "--no-workflows",
            "--no-releases", "--no-project-board", *extra])
        real_provider = gather.select_code_area_provider
        with mock.patch.object(gather, "_github_remote_url",
                               lambda o, r: "file://" + self.src), \
                mock.patch.object(gather, "_paginated",
                                  lambda token: (lambda url: ([], None))), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d: real_provider(
                                      paths, d, which=lambda _n: None)), \
                mock.patch.object(gather, "extract_iac_edges", lambda g, d: g):
            return gather.acquire(args, {"GITHUB_TOKEN": "dummy"})

    def test_window_scoped_without_flag(self):
        g = self._acquire()["code_graph"]
        self.assertEqual([a["id"] for a in g["areas"]], [PTN])
        self.assertEqual(_graph_edges(g), set())

    def test_full_edge_set_with_flag(self):
        g = self._acquire("--structural-iac")["code_graph"]
        self.assertEqual(
            {(s, t) for s, t, _r in _graph_edges(g) if t is not None},
            {(PTN, SITE), (SITE, FARM), (SITE, SITE + "/slot")})


# --- cross-repo resolution -----------------------------------------------------

BRM = "Azure/bicep-registry-modules"
LZ = "Contoso/landing-zone"


def _bundle(slug, areas, frm="2026-01-01", to="2026-01-31"):
    owner, repo = slug.split("/")
    return {"meta": {"owner": owner, "repo": repo, "from": frm, "to": to,
                     "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": areas}}


def _areas(scanned):
    return [{"id": aid, "label": gather._area_label(aid), "paths": info["paths"],
             "edges": info["edges"]} for aid, info in sorted(scanned.items())]


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _deps(conn, src):
    return {e["dst_id"]: e["data"] for e in graphstore.get_edges(
        conn, src, direction="out", edge_types=["depends_on"])}


class TestResolveBicepRegistryMember(unittest.TestCase):
    MEMBERS = {BRM, LZ, "Contoso/platform"}
    PREFIX = {BRM: "avm", "Contoso/platform": "contoso/platform"}

    def _r(self, ref, exclude=LZ, prefixes=None):
        return gather.resolve_bicep_registry_member(
            ref, "proj", self.MEMBERS,
            self.PREFIX if prefixes is None else prefixes, exclude=exclude)

    def test_public_alias_and_explicit_registry_resolve_alike(self):
        want = "proj/{}#area-avm/res/key-vault/vault".format(BRM)
        self.assertEqual(self._r("br/public:avm/res/key-vault/vault:0.9.0"), want)
        self.assertEqual(
            self._r("br:mcr.microsoft.com/bicep/avm/res/key-vault/vault:0.9.0"), want)
        self.assertEqual(
            self._r("br:MCR.microsoft.com/bicep/avm/res/key-vault/vault@sha256:ab"),
            want)

    def test_prefix_is_whole_segments_and_case_insensitive(self):
        self.assertIsNone(self._r("br/public:avmx/res/a/b:1.0"))
        self.assertEqual(self._r("br/public:contoso/platform/net/hub:1.0"),
                         "proj/Contoso/platform#area-contoso/platform/net/hub")

    def test_unresolvable_refs(self):
        for ref in ("br/public:samples/hello-world:1.0.2",       # no publisher
                    "br/public:avm/res/network:1.0.0",           # an AVM group
                    "br:contoso.azurecr.io/bicep/avm/res/a/b:1",  # private registry
                    "ts:sub/rg/spec:1.0",                        # template spec
                    "br/myalias:avm/res/a/b:1.0",                # user alias
                    "modules/hub/main.bicep",                    # local
                    "", None):
            with self.subTest(ref=ref):
                self.assertIsNone(self._r(ref))

    def test_no_prefixes_declared(self):
        self.assertIsNone(self._r("br/public:avm/res/a/b:1", prefixes={}))
        self.assertIsNone(self._r("br/public:avm/res/a/b:1", prefixes=None or {}))

    def test_never_resolves_to_the_referencing_member_itself(self):
        self.assertIsNone(self._r("br/public:avm/res/a/b:1", exclude=BRM))

    def test_longest_prefix_wins(self):
        prefixes = {BRM: "avm", "Contoso/platform": "avm/ptn/contoso"}
        self.assertEqual(self._r("br/public:avm/ptn/contoso/hub:1", prefixes=prefixes),
                         "proj/Contoso/platform#area-avm/ptn/contoso/hub")
        self.assertEqual(self._r("br/public:avm/ptn/other/hub:1", prefixes=prefixes),
                         "proj/{}#area-avm/ptn/other/hub".format(BRM))

    def test_member_lookup_is_case_insensitive_and_keeps_manifest_spelling(self):
        self.assertEqual(
            gather.resolve_bicep_registry_member(
                "br/public:avm/res/a/b:1", "proj", {"azure/BICEP-registry-modules"},
                {"azure/BICEP-registry-modules": "avm/"}),
            "proj/azure/BICEP-registry-modules#area-avm/res/a/b")


class TestCrossRepoBicepFold(unittest.TestCase):
    MEMBERS = {BRM, LZ}
    PREFIX = {BRM: "avm", LZ: None}

    def _fold(self, conn, slug, bundle, prefixes="default"):
        gather.fold_bundle(
            conn, bundle, project="proj", repo=slug, members=self.MEMBERS,
            registry_by_slug={BRM: None, LZ: None},
            bicep_prefix_by_slug=self.PREFIX if prefixes == "default" else prefixes)

    def _consumer(self):
        return _bundle(LZ, _areas(_scan(LZ_FILES)))

    def test_consumer_ref_becomes_cross_repo_depends_on(self):
        conn = _store()
        self._fold(conn, LZ, self._consumer())
        root = _deps(conn, "proj/{}#area-main.bicep".format(LZ))
        kv = "proj/{}#area-avm/res/key-vault/vault".format(BRM)
        self.assertEqual(set(root), {"proj/{}#area-modules/hub".format(LZ), kv})
        self.assertEqual(root[kv], {
            "kind": "module", "ref": "br/public:avm/res/key-vault/vault:0.9.0",
            "version": "0.9.0", "transitive": False, "provider": "bicep",
            "resolved": True, "cross_repo": True})
        hub = _deps(conn, "proj/{}#area-modules/hub".format(LZ))
        # explicit-registry form resolves; group / non-member / private do not
        self.assertEqual(set(hub),
                         {"proj/{}#area-avm/res/network/virtual-network".format(BRM)})
        # the publisher was not folded: the target is a stand-in anchor
        node = graphstore.get_node(conn, kv)
        self.assertEqual(node["node_class"], "structure")
        self.assertEqual(node["data"], {"id": "avm/res/key-vault/vault",
                                        "synthesized": graphstore.STANDIN_MARK})

    def test_without_prefix_nothing_resolves(self):
        conn = _store()
        self._fold(conn, LZ, self._consumer(), prefixes=None)
        self.assertEqual(set(_deps(conn, "proj/{}#area-main.bicep".format(LZ))),
                         {"proj/{}#area-modules/hub".format(LZ)})

    def test_single_repo_fold_is_unchanged(self):
        conn = _store()
        gather.fold_bundle(conn, self._consumer())
        self.assertEqual(set(_deps(conn, "Contoso/landing-zone#area-main.bicep")),
                         {"Contoso/landing-zone#area-modules/hub"})

    def test_publisher_own_unresolved_ref_is_not_self_resolved(self):
        conn = _store()
        self._fold(conn, BRM, _bundle(BRM, _areas(_scan(BRM_FILES))))
        ptn = _deps(conn, "proj/{}#area-{}".format(BRM, PTN))
        self.assertEqual(set(ptn), {"proj/{}#area-{}".format(BRM, SITE)})

    def test_terraform_edges_keep_terraform_resolution(self):
        conn = _store()
        tf = {"to": None, "kind": "module", "ref": "Azure/avm-res-x-y/azurerm",
              "version": "1.0", "transitive": False, "provider": "terraform",
              "resolved": False}
        gather.fold_bundle(
            conn, _bundle(LZ, [{"id": "main.tf", "label": "main.tf",
                                "paths": ["main.tf"], "edges": [tf]}]),
            project="proj", repo=LZ, members={LZ, "Azure/terraform-azurerm-avm-res-x-y"},
            registry_by_slug={}, bicep_prefix_by_slug={})
        self.assertEqual(set(_deps(conn, "proj/{}#area-main.tf".format(LZ))),
                         {"proj/Azure/terraform-azurerm-avm-res-x-y#area-main.tf"})

    def test_validate_accepts_store_in_either_fold_order(self):
        for order in ((LZ, BRM), (BRM, LZ)):
            with self.subTest(order=order):
                conn = _store()
                bundles = {LZ: self._consumer(),
                           BRM: _bundle(BRM, _areas(_scan(BRM_FILES)))}
                for slug in order:
                    self._fold(conn, slug, bundles[slug])
                # the publisher holds the module, so the target is a real area
                # only when it is in the publisher's tree; key-vault is not.
                rep = validate.validate_project(
                    conn, "proj", graphstore.project_repos(conn, "proj"))
                self.assertTrue(rep["ok"], json.dumps(rep, indent=1)[:3000])

    def test_validate_rejects_bicep_dependency_pointed_at_the_wrong_area(self):
        # The trust check knows where a Bicep public-registry ref resolves: the
        # publisher's MODULE area. The same edge data re-pointed at the
        # publisher's root area (the Terraform rule) is not derivable.
        conn = _store()
        self._fold(conn, LZ, self._consumer())
        self._fold(conn, BRM, _bundle(BRM, _areas(_scan(BRM_FILES))))
        rows = conn.execute(
            "SELECT src_id, dst_id, data FROM edges WHERE edge_type='depends_on' "
            "AND src_id LIKE ? AND dst_id LIKE ?",
            ("proj/{}#%".format(LZ), "proj/{}#%".format(BRM))).fetchall()
        self.assertTrue(rows)
        src, dst, data = rows[0]
        wrong = "proj/{}#area-main.tf".format(BRM)
        self.assertNotEqual(dst, wrong)
        conn.execute("UPDATE edges SET dst_id=? WHERE src_id=? AND dst_id=? "
                     "AND edge_type='depends_on'", (wrong, src, dst))
        graphstore.upsert_nodes(conn, [
            (wrong, "proj", BRM, "structure", None,
             {"id": "main.tf", "synthesized": graphstore.STANDIN_MARK}, None)])
        conn.commit()
        rep = validate.validate_project(
            conn, "proj", graphstore.project_repos(conn, "proj"))
        self.assertFalse(rep["ok"])
        failed = {c["name"] for m in rep["members"] for c in m["checks"]
                  if not c["ok"]}
        self.assertIn("edge_set", failed)

    def test_real_publisher_area_wins_over_stand_in(self):
        files = dict(BRM_FILES)
        files["avm/res/key-vault/vault/main.bicep"] = "param x string\n"
        for order in ((LZ, BRM), (BRM, LZ)):
            with self.subTest(order=order):
                conn = _store()
                bundles = {LZ: self._consumer(),
                           BRM: _bundle(BRM, _areas(_scan(files)))}
                for slug in order:
                    self._fold(conn, slug, bundles[slug])
                node = graphstore.get_node(
                    conn, "proj/{}#area-avm/res/key-vault/vault".format(BRM))
                self.assertNotIn("synthesized", node["data"])


class TestManifestBicepRegistryPrefix(unittest.TestCase):
    def _load(self, repos):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"project": "p",
                           "window": {"from": "2026-01-01", "to": "2026-01-31"},
                           "repos": repos}, fh)
            return manifest.load_manifest(path)

    def test_optional_and_normalised(self):
        m = self._load([
            {"owner": "Azure", "repo": "bicep-registry-modules",
             "bicep_registry_prefix": "avm/"},
            {"owner": "Contoso", "repo": "lz"}])
        self.assertEqual(m["repos"][0]["bicep_registry_prefix"], "avm")
        self.assertIsNone(m["repos"][1]["bicep_registry_prefix"])

    def test_rejects_malformed(self):
        for bad in ("", "  ", 5, ["avm"], "/avm", "br/public:avm/", "avm//res",
                    "AVM", "avm/../x", "avm res", "-avm"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,
                                            r"repos\[0\].*'bicep_registry_prefix'"):
                    self._load([{"owner": "o", "repo": "a",
                                 "bicep_registry_prefix": bad}])

    def test_rejects_duplicate_prefix(self):
        with self.assertRaisesRegex(ValueError, "duplicate bicep_registry_prefix"):
            self._load([
                {"owner": "o", "repo": "a", "bicep_registry_prefix": "avm"},
                {"owner": "o", "repo": "b", "bicep_registry_prefix": "avm/"}])

    def test_nested_prefixes_are_allowed(self):
        m = self._load([
            {"owner": "o", "repo": "a", "bicep_registry_prefix": "avm"},
            {"owner": "o", "repo": "b", "bicep_registry_prefix": "avm/ptn/contoso"}])
        self.assertEqual([r["bicep_registry_prefix"] for r in m["repos"]],
                         ["avm", "avm/ptn/contoso"])


class TestGatherManifestThreadsPrefixes(unittest.TestCase):
    def test_fold_receives_bicep_prefixes(self):
        man = {"project": "proj", "from": "2026-01-01", "to": "2026-01-31",
               "repos": [{"owner": "Azure", "repo": "bicep-registry-modules",
                          "registry": None, "bicep_registry_prefix": "avm"},
                         {"owner": "Contoso", "repo": "landing-zone",
                          "registry": None, "bicep_registry_prefix": None}],
               "ticket_prefixes": []}
        seen = []
        args = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
        conn = _store()
        with mock.patch.object(gather, "acquire",
                               lambda a, env: _bundle("{}/{}".format(a.owner, a.repo),
                                                      [])), \
                mock.patch.object(gather, "fold_bundle",
                                  lambda c, b, **kw: seen.append(kw)):
            gather._gather_manifest(conn, args, man)
        self.assertEqual(len(seen), 2)
        for kw in seen:
            self.assertEqual(kw["bicep_prefix_by_slug"],
                             {BRM: "avm", LZ: None})


class TestAreaDependents(unittest.TestCase):
    """`spotlight dependents owner/repo::<area>`: the consumers of one module."""

    def _single_repo_store(self):
        conn = _store()
        gather.fold_bundle(conn, _bundle(BRM, _areas(_scan(BRM_FILES))))
        return conn

    def test_in_repo_consumers_direct_and_transitive(self):
        conn = self._single_repo_store()
        res = spotlight.area_dependents(conn, "Azure", BRM + "::" + FARM)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["focus_kind"], "area")
        self.assertEqual(res["focus"], BRM + "::" + FARM)
        self.assertEqual(res["direct"], [BRM + "::" + SITE])
        self.assertEqual(res["dependents"], [BRM + "::" + PTN, BRM + "::" + SITE])

    def test_case_insensitive_repo_and_leaf_with_no_consumers(self):
        conn = self._single_repo_store()
        res = spotlight.area_dependents(conn, "Azure",
                                        "azure/Bicep-Registry-Modules::" + PTN)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["focus"], BRM + "::" + PTN)
        self.assertEqual(res["dependents"], [])

    def test_validate_accepts_single_repo_structural_store(self):
        rep = validate.validate(self._single_repo_store())
        self.assertTrue(rep.ok, json.dumps(rep.to_dict(), indent=1)[:3000])

    def test_unknown_area_needs_gather(self):
        conn = self._single_repo_store()
        res = spotlight.area_dependents(conn, "Azure", BRM + "::avm/res/no/such")
        self.assertEqual(res["status"], "needs_gather")

    def test_cross_repo_consumers_in_a_project(self):
        conn = _store()
        files = dict(BRM_FILES)
        files["avm/res/key-vault/vault/main.bicep"] = "param x string\n"
        for slug, b in ((BRM, _bundle(BRM, _areas(_scan(files)))),
                        (LZ, _bundle(LZ, _areas(_scan(LZ_FILES))))):
            gather.fold_bundle(conn, b, project="proj", repo=slug,
                               members={BRM, LZ}, registry_by_slug={},
                               bicep_prefix_by_slug={BRM: "avm"})
        res = spotlight.area_dependents(conn, "proj",
                                        BRM + "::avm/res/key-vault/vault")
        self.assertEqual(res["dependents"], [LZ + "::main.bicep"])
        # the member-level query sees the landing zone too
        self.assertEqual(spotlight.member_dependents(conn, "proj", BRM)["dependents"],
                         [LZ])

    def test_cli_dispatches_area_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, _bundle(BRM, _areas(_scan(BRM_FILES))))
            conn.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = spotlight.main(["dependents", BRM + "::" + SITE,
                                     "--store", db])
            self.assertEqual(rc, 0)
            res = json.loads(out.getvalue())
            self.assertEqual(res["dependents"], [BRM + "::" + PTN])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                spotlight.main(["dependents", BRM + "::" + SITE, "--store", db,
                                "--md"])
            self.assertIn(PTN, out.getvalue())


if __name__ == "__main__":
    unittest.main()
