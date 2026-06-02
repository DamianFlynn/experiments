import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import render  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _bundle():
    with open(os.path.join(FIX, "bundle_p2.json")) as fh:
        return json.load(fh)


class TestBucketsPie(unittest.TestCase):
    def test_pie_header_and_counts(self):
        b = _bundle()
        b["buckets"] = {"shipped": [{"type": "pr", "id": 42, "url": "u"},
                                    {"type": "issue", "id": 17, "url": "u"}],
                        "in_flight": [{"type": "issue", "id": 21, "url": "u"}],
                        "rejected": [{"type": "pr", "id": 43, "url": "u"}],
                        "next_candidates": []}
        mmd = render.emit_buckets_pie(b)
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn('"Shipped" : 2', mmd)
        self.assertIn('"In flight" : 1', mmd)
        self.assertIn('"Rejected" : 1', mmd)
        # zero-count slices are omitted so mmdc never sees an empty slice
        self.assertNotIn("Next candidates", mmd)

    def test_pie_all_empty_has_placeholder(self):
        b = _bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
        mmd = render.emit_buckets_pie(b)
        self.assertIn('"No activity" : 1', mmd)


class TestTimelineGantt(unittest.TestCase):
    def test_gantt_header_and_tasks(self):
        mmd = render.emit_timeline_gantt(_bundle())
        self.assertTrue(mmd.startswith("gantt"))
        self.assertIn("dateFormat YYYY-MM-DD", mmd)
        self.assertIn("section Pull requests", mmd)
        # merged PR -> done; open PR -> active; closed-unmerged -> crit
        self.assertIn(":done,", mmd)
        self.assertIn(":active,", mmd)
        self.assertIn(":crit,", mmd)
        self.assertIn("section Releases", mmd)
        self.assertIn(":milestone,", mmd)

    def test_gantt_labels_have_no_colons(self):
        b = _bundle()
        b["prs"][0]["title"] = "Fix: thing: with colons"
        mmd = render.emit_timeline_gantt(b)
        for line in mmd.splitlines():
            if line.strip().startswith("#42"):
                # only the status-separator colon may remain
                self.assertEqual(line.count(":"), 1)

    def test_gantt_clamps_end_after_start(self):
        b = _bundle()
        b["prs"] = [{"number": 9, "title": "weird", "state": "open",
                     "created_at": "2026-05-20T00:00:00Z",
                     "merged_at": None, "closed_at": None, "merged": False}]
        b["releases"] = []
        mmd = render.emit_timeline_gantt(b)
        # open PR with no end uses `to`; start <= end always
        self.assertIn("2026-05-20", mmd)


if __name__ == "__main__":
    unittest.main()
