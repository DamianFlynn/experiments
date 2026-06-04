"""Phase 7b-1: the pure derivations are usable directly from `derive` (a leaf
module that imports neither link nor gather). These tests exercise derive's
public surface WITHOUT importing link, proving the write-path can derive the
same facts without a link dependency. Behavior parity with link is already
covered by test_link.py (which calls the same objects via re-export)."""

import unittest

import derive


class DeriveIsALeafModule(unittest.TestCase):
    def test_imports_no_forbidden_modules(self):
        # derive must be a leaf: its source must not import link or gather.
        # (A direct AST/source check avoids mutating global sys.modules, which
        # other test files in the run rely on.)
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(derive))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("link", imported)
        self.assertNotIn("gather", imported)


class ArtifactId(unittest.TestCase):
    def test_stable_prefix(self):
        self.assertEqual(derive.artifact_id("docs/x.md"), "art:docs/x.md")

    def test_none_path(self):
        self.assertEqual(derive.artifact_id(None), "art:")


class BuildArtifacts(unittest.TestCase):
    def test_classifies_and_folds_lifecycle(self):
        bundle = {
            "code_events": [
                {"path": "README.md", "change": "add", "commit": "c1",
                 "author": "a", "date": "2026-01-01"},
                {"path": "src/app.py", "change": "add", "commit": "c2",
                 "author": "a", "date": "2026-01-02"},
            ]
        }
        arts = derive.build_artifacts(bundle)
        self.assertIn("art:README.md", arts)
        self.assertEqual(arts["art:README.md"]["kind"], "readme")
        # untracked code path is ignored at file granularity
        self.assertNotIn("art:src/app.py", arts)
        self.assertEqual(arts["art:README.md"]["status"], "live")

    def test_empty(self):
        self.assertEqual(derive.build_artifacts({"code_events": []}), {})


class AreaIndexAndAttribution(unittest.TestCase):
    def test_area_index_and_lookup(self):
        cg = {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]}
        idx = derive.area_index(cg)
        self.assertEqual(idx, {"docs/x.md": "area:a"})
        self.assertEqual(derive._area_for_path("docs/x.md", idx), "area:a")
        self.assertIsNone(derive._area_for_path("nope", idx))

    def test_attribute_code_areas_fills_nulls(self):
        bundle = {
            "code_graph": {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]},
            "artifacts": {"art:docs/x.md": {"path": "docs/x.md", "code_area": None}},
            "feature_deltas": [{"artifact": "art:docs/x.md", "name": "x.md",
                                "area": None}],
        }
        idx = derive.attribute_code_areas(bundle)
        self.assertEqual(bundle["artifacts"]["art:docs/x.md"]["code_area"], "area:a")
        self.assertEqual(bundle["feature_deltas"][0]["area"], "area:a")
        self.assertEqual(idx, {"docs/x.md": "area:a"})


class Modules(unittest.TestCase):
    def test_build_modules_counts(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {"commits": [
            {"sha": "c1", "pr": 1, "files": ["docs/x.md"]},
            {"sha": "c2", "pr": 1, "files": ["docs/x.md", "other"]},
        ]}
        derive.build_modules(bundle, idx)
        self.assertEqual(bundle["modules"],
                         {"area:a": {"commits": 2, "prs": 1, "files_changed": 1}})


class People(unittest.TestCase):
    def test_attribute_people_areas(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {
            "commits": [{"sha": "c1", "pr": 1, "author": "alice",
                         "files": ["docs/x.md"]}],
            "prs": [{"number": 1, "reviewers": ["bob"]}],
        }
        derive.attribute_people_areas(bundle, idx)
        self.assertEqual(bundle["people"]["alice"]["areas"], ["area:a"])
        self.assertEqual(bundle["people"]["bob"]["areas"], ["area:a"])


class EnumerateParticipants(unittest.TestCase):
    """The shared enumerator returns the FULL participant set (anti-drift)."""

    def test_includes_pure_participants_and_contributors(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {
            "code_graph": {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]},
            "commits": [{"sha": "c1", "pr": 1, "author": "alice",
                         "files": ["docs/x.md"]}],
            "prs": [{"number": 1, "author": "alice", "merged_by": "mallory",
                     "reviewers": ["bob"],
                     "comments_list": [{"author": "erin"}],
                     "review_comments": [{"author": "frank"}]}],
            "issues": [{"number": 9, "author": "grace",
                        "comments_list": [{"author": "heidi"}]}],
        }
        ppl = derive.enumerate_participants(bundle, idx)
        # everyone with any contribution edge appears.
        self.assertEqual(
            set(ppl),
            {"alice", "mallory", "bob", "erin", "frank", "grace", "heidi"})
        # contributors carry modules/areas; pure participants are empty.
        self.assertEqual(ppl["alice"]["areas"], ["area:a"])
        self.assertEqual(ppl["bob"]["areas"], ["area:a"])  # reviewer inherits PR areas
        self.assertEqual(ppl["erin"]["areas"], [])
        self.assertEqual(ppl["grace"]["modules"], [])

    def test_bots_tagged_not_dropped(self):
        bundle = {"commits": [], "prs": [
            {"number": 1, "author": "dependabot[bot]",
             "comments_list": [{"author": "github-actions"}]}],
            "issues": [{"number": 2, "author": "real-human"}]}
        ppl = derive.enumerate_participants(bundle)
        self.assertIn("dependabot[bot]", ppl)
        self.assertTrue(ppl["dependabot[bot]"]["is_bot"])
        self.assertTrue(ppl["github-actions"]["is_bot"])
        self.assertFalse(ppl["real-human"]["is_bot"])

    def test_is_bot_login_patterns(self):
        for b in ("foo[bot]", "copilot-swe-agent[bot]", "github-actions",
                  "microsoft-github-policy-service", "release-organizer",
                  "copilot-anything"):
            self.assertTrue(derive.is_bot_login(b), b)
        for h in ("alice", "bob", "robot-cat", "", None):
            self.assertFalse(derive.is_bot_login(h), h)


class SymbolMoves(unittest.TestCase):
    def test_unique_name_move_medium(self):
        events = [
            {"lang": "bicep", "subkind": "resource", "name": "foo",
             "change": "drop", "path": "a.bicep"},
            {"lang": "bicep", "subkind": "resource", "name": "foo",
             "change": "add", "path": "b.bicep"},
        ]
        moves = derive.match_symbol_moves(events)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["confidence"], "medium")
        self.assertEqual(moves[0]["basis"], "unique_name")

    def test_link_symbol_identity_records_summary(self):
        bundle = {
            "symbol_events": [
                {"lang": "bicep", "subkind": "resource", "name": "foo",
                 "change": "drop", "path": "a.bicep"},
                {"lang": "bicep", "subkind": "resource", "name": "foo",
                 "change": "add", "path": "b.bicep"},
            ],
            "code_events": [],
            "artifacts": {
                "a.bicep#bicep:resource:foo": {"status": "live"},
                "b.bicep#bicep:resource:foo": {"status": "live"},
            },
        }
        derive.link_symbol_identity(bundle)
        self.assertEqual(bundle["symbol_moves"]["by_confidence"]["medium"], 1)
        self.assertEqual(
            bundle["artifacts"]["a.bicep#bicep:resource:foo"]["status"], "replaced")


if __name__ == "__main__":
    unittest.main()
