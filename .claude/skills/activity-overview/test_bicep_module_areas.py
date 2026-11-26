"""Every file of a Bicep module directory is in the module's area (issue #107,
item 9).

`_main_bicep_dir` maps only the `main.bicep` file itself to its directory, so
before #107 a module's README, child templates and parameter files fell into
whichever other pattern matched them (mostly the two-segment fallback). Now a
directory holding a tracked, non-scaffold `main.bicep` (outside the AVM
layout and the repo root) is the area of every non-Terraform file in it and
below it, down to the next such directory; `examples/` / `tests/` subtrees
keep today's classification. The set of module directories comes from the
window's changed paths plus the clone's tracked files (one `git ls-files`,
shared with the structural scan). The per-path classifier keeps today's
behaviour unless it is given that set.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
from test_walked_branch import GitRepoCase  # noqa: E402

P = gather.DEFAULT_AREA_PATTERNS

MODULE_DIRS = frozenset({"infra", "platform/hub", "platform/hub/spoke",
                         "a/b/c", "modules/web"})

# path -> (area before #107 / without the set, area with the set)
CASES = {
    # a one-segment module dir: its subdirectories used to be areas of their own
    "infra/main.bicep": ("infra", "infra"),
    "infra/README.md": ("infra", "infra"),
    "infra/modules/net.bicep": ("infra/modules", "infra"),
    "infra/modules/deep/x.json": ("infra/modules", "infra"),
    # a two-segment module dir: the fallback already named it
    "platform/hub/main.bicep": ("platform/hub", "platform/hub"),
    "platform/hub/README.md": ("platform/hub", "platform/hub"),
    "platform/hub/parts/nsg.bicep": ("platform/hub", "platform/hub"),
    # a nested module dir is its own area, down from it
    "platform/hub/spoke/main.bicep": ("platform/hub/spoke", "platform/hub/spoke"),
    "platform/hub/spoke/README.md": ("platform/hub", "platform/hub/spoke"),
    "platform/hub/spoke/x/y.bicep": ("platform/hub", "platform/hub/spoke"),
    # a deep module dir
    "a/b/c/main.bicep": ("a/b/c", "a/b/c"),
    "a/b/c/README.md": ("a/b", "a/b/c"),
    # examples/ and tests/ scaffolding keeps today's area
    "infra/examples/main.bicep": ("infra/examples", "infra/examples"),
    "infra/examples/README.md": ("infra/examples", "infra/examples"),
    "platform/hub/tests/e2e/main.test.bicep": ("platform/hub", "platform/hub"),
    "a/b/c/tests/x.bicep": ("a/b", "a/b"),
    # Terraform files keep the Terraform rules
    "infra/tf/main.tf": ("infra/tf", "infra/tf"),
    # a Bicep module under modules/<name>: same area as before
    "modules/web/main.bicep": ("modules/web", "modules/web"),
    "modules/web/README.md": ("modules/web", "modules/web"),
    # not under any module dir: unchanged
    "docs/guide.md": ("docs", "docs"),
    "platform/README.md": ("platform", "platform"),
    "README.md": ("README.md", "README.md"),
    "main.bicep": ("main.bicep", "main.bicep"),
    # the AVM layout: unchanged
    "avm/res/net/vnet/main.bicep": ("avm/res/net/vnet", "avm/res/net/vnet"),
    "avm/res/net/vnet/README.md": ("avm/res/net/vnet", "avm/res/net/vnet"),
    "avm/res/net/vnet/subnet/main.bicep": ("avm/res/net/vnet", "avm/res/net/vnet"),
    "avm/res/net/vnet/tests/e2e/defaults/main.test.bicep":
        ("avm/res/net/vnet", "avm/res/net/vnet"),
    "avm/res/net/README.md": ("avm/res/net", "avm/res/net"),
    # Terraform layouts: unchanged
    "modules/storage/main.tf": ("modules/storage", "modules/storage"),
    "modules/storage/inner/x.tf": ("modules/storage/inner", "modules/storage/inner"),
    "modules/storage/README.md": ("modules/storage", "modules/storage"),
    "main.tf": ("main.tf", "main.tf"),
    "variables.tf": ("main.tf", "main.tf"),
    "envs/prod/main.tf": ("envs/prod", "envs/prod"),
}


class TestClassifier(unittest.TestCase):
    def test_without_the_set_nothing_changes(self):
        for path, (before, _after) in CASES.items():
            self.assertEqual(gather.classify_code_area(path, P), before, path)
            self.assertEqual(gather.classify_code_area(path, P, None), before, path)
            self.assertEqual(gather.classify_code_area(path, P, frozenset()),
                             before, path)

    def test_with_the_set(self):
        for path, (_before, after) in CASES.items():
            self.assertEqual(gather.classify_code_area(path, P, MODULE_DIRS),
                             after, path)

    def test_patterns_factory_matches_the_argument(self):
        pats = gather.module_area_patterns(MODULE_DIRS)
        for path, (_before, after) in CASES.items():
            self.assertEqual(gather.classify_code_area(path, pats), after, path)
        self.assertIs(gather.module_area_patterns(frozenset()), P)

    def test_module_dirs_from_paths(self):
        paths = ["infra/main.bicep", "infra/README.md", "main.bicep",
                 "avm/res/net/vnet/main.bicep", "avm/res/net/main.bicep",
                 "platform/hub/examples/main.bicep", "x/tests/main.bicep",
                 "platform/hub/main.bicep", "platform/hub/main.bicepparam",
                 "a/b/c/main.bicep", "a/b/notmain.bicep"]
        self.assertEqual(gather.bicep_module_dirs(paths),
                         frozenset({"infra", "platform/hub", "a/b/c"}))
        self.assertEqual(gather.bicep_module_dirs([]), frozenset())

    def test_directory_areas_group_the_module(self):
        paths = ["infra/main.bicep", "infra/README.md", "infra/modules/net.bicep"]
        before = gather.build_directory_areas(paths, P)
        self.assertEqual([a["id"] for a in before["areas"]], ["infra", "infra/modules"])
        after = gather.build_directory_areas(paths, P, frozenset({"infra"}))
        self.assertEqual([(a["id"], a["paths"]) for a in after["areas"]],
                         [("infra", sorted(paths))])


class TestAcquireModuleAreas(GitRepoCase):
    """A module whose main.bicep did not change in the window still owns the
    files that did: the module dirs come from the clone's tracked files."""

    FROM, TO = "2026-05-01", "2026-05-31"

    def setUp(self):
        super().setUp()
        self.commit({
            "infra/main.bicep": "module net './modules/net.bicep' = {\n"
                                "  name: 'net'\n}\n",
            "infra/modules/net.bicep": "param p string\n",
            "infra/README.md": "infra\n",
            "platform/hub/spoke/main.bicep": "param s string\n",
            "platform/hub/spoke/README.md": "spoke\n",
            "docs/guide.md": "guide\n",
        }, "2026-03-01")
        self.commit({
            "infra/modules/net.bicep": "param p string\nparam q string\n",
            "infra/README.md": "infra v2\n",
            "platform/hub/spoke/README.md": "spoke v2\n",
            "docs/guide.md": "guide v2\n",
        }, "2026-05-10")

    @staticmethod
    def areas(bundle):
        return {a["id"]: a["paths"] for a in bundle["code_graph"]["areas"]}

    def test_window_areas_use_the_clone_module_dirs(self):
        b, _ = self.acquire(self.FROM, self.TO)
        self.assertEqual(self.areas(b), {
            "infra": ["infra/README.md", "infra/modules/net.bicep"],
            "platform/hub/spoke": ["platform/hub/spoke/README.md"],
            "docs": ["docs/guide.md"]})

    def test_structural_scan_agrees(self):
        b, _ = self.acquire(self.FROM, self.TO, "--structural-iac")
        areas = self.areas(b)
        self.assertEqual(areas["infra"],
                         ["infra/README.md", "infra/modules/net.bicep"])
        self.assertNotIn("infra/modules", areas)
        self.assertNotIn("platform/hub", areas)
        # each path is in exactly one area
        paths = [p for ps in areas.values() for p in ps]
        self.assertEqual(len(paths), len(set(paths)))
        # infra's local child template is a private child edge, not an edge
        # to an area of its own
        edges = [e for a in b["code_graph"]["areas"] if a["id"] == "infra"
                 for e in a["edges"]]
        self.assertEqual([(e["to"], e.get("local")) for e in edges],
                         [("infra/modules", True)])

    def test_trust_check_green(self):
        for extra in ((), ("--structural-iac",)):
            b, _ = self.acquire(self.FROM, self.TO, *extra)
            self.assertValidates(b)

    def test_module_added_between_windows(self):
        # June adds a main.bicep above an existing file: April (re-gathered
        # later, read at its own end) keeps the old area, June uses the module;
        # one store holding both stays green (a path two windows map
        # differently is a WARN, as for any re-mapped path)
        self.commit({"lib/net/x.bicep": "param x string\n"}, "2026-04-10")
        self.commit({"lib/net/x.bicep": "param x string\n// v2\n",
                     "lib/main.bicep": "param m string\n"}, "2026-06-10")
        june, _ = self.acquire("2026-06-01", "2026-06-30")
        april, _ = self.acquire("2026-04-01", "2026-04-30")
        self.assertIn("lib/net", self.areas(april))
        self.assertEqual(self.areas(june)["lib"],
                         ["lib/main.bicep", "lib/net/x.bicep"])
        self.assertValidates(april, june)

    def test_without_a_clone_only_changed_paths_decide(self):
        # no clone: the main.bicep files did not change, so no module dir is
        # known and the areas are today's
        b, _ = self.acquire(self.FROM, self.TO, "--no-clone")
        self.assertEqual(b["code_graph"]["areas"], [])
        paths = ["infra/main.bicep", "infra/modules/net.bicep",
                 "platform/hub/spoke/README.md"]
        g = gather.select_code_area_provider(
            paths, os.path.join(self.tmp, "none"), which=lambda _n: None,
            patterns=gather.module_area_patterns(gather.bicep_module_dirs(paths)))
        self.assertEqual([a["id"] for a in g["areas"]],
                         ["infra", "platform/hub"])


if __name__ == "__main__":
    unittest.main()
