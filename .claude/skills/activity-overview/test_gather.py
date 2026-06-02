import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestBuildBundle(unittest.TestCase):
    def test_skeleton_has_all_top_level_keys_and_reserved_empties(self):
        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, commits=[{"sha": "abc"}],
                                     prs=[{"number": 42}], issues=[{"number": 17}])

        # supplied data is carried through
        self.assertEqual(bundle["meta"]["owner"], "o")
        self.assertEqual(bundle["commits"], [{"sha": "abc"}])
        self.assertEqual(bundle["prs"], [{"number": 42}])
        self.assertEqual(bundle["issues"], [{"number": 17}])

        # later-phase fields are reserved but empty
        for key in ["timeline", "feature_deltas", "trains", "blockers",
                    "releases", "milestones", "docsRefs", "workflows"]:
            self.assertEqual(bundle[key], [], f"{key} should be reserved empty list")
        for key in ["artifacts", "people", "modules", "code_owners", "flow",
                    "label_taxonomy", "diagrams", "workflow_stats", "halls",
                    "code_graph", "release_train", "sprints", "project"]:
            self.assertEqual(bundle[key], {}, f"{key} should be reserved empty dict")
        self.assertEqual(
            bundle["buckets"],
            {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        )
        self.assertEqual(bundle["meta"]["schema_version"], 1)


class TestParseGitLog(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "git_log_sample.txt")) as fh:
            self.raw = fh.read()

    def test_parses_three_commits_in_order(self):
        commits = gather.parse_git_log(self.raw)
        self.assertEqual(len(commits), 3)
        self.assertEqual(commits[0]["sha"], "a1" * 20)
        self.assertEqual(commits[0]["author"], "Alice")
        self.assertEqual(commits[0]["date"], "2026-05-10")
        self.assertEqual(commits[0]["message"], "Add policy param (#42)")
        self.assertEqual(
            commits[0]["files"],
            ["modules/firewall/main.bicep", "modules/firewall/README.md"],
        )

    def test_merge_commit_has_two_parents_and_no_files(self):
        commits = gather.parse_git_log(self.raw)
        merge = commits[1]
        self.assertEqual(len(merge["parents"]), 2)
        self.assertEqual(merge["files"], [])

    def test_empty_input_yields_no_commits(self):
        self.assertEqual(gather.parse_git_log(""), [])


class TestCloneAndWindow(unittest.TestCase):
    def test_build_clone_cmd_is_bounded_and_partial(self):
        cmd = gather.build_clone_cmd("https://github.com/o/r.git",
                                     "2026-05-01", "/tmp/clone")
        self.assertEqual(cmd[0], "git")
        self.assertIn("clone", cmd)
        self.assertIn("--filter=blob:none", cmd)
        self.assertIn("--shallow-since=2026-05-01", cmd)
        self.assertIn("--no-single-branch", cmd)
        self.assertEqual(cmd[-2:], ["https://github.com/o/r.git", "/tmp/clone"])

    def test_in_window_inclusive_bounds(self):
        self.assertTrue(gather.in_window("2026-05-01", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-31", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-15T08:00:00Z", "2026-05-01", "2026-05-31"))

    def test_in_window_rejects_outside_and_none(self):
        self.assertFalse(gather.in_window("2026-04-30", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window("2026-06-01", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window(None, "2026-05-01", "2026-05-31"))


if __name__ == "__main__":
    unittest.main()
