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


if __name__ == "__main__":
    unittest.main()
