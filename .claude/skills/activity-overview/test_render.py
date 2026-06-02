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


if __name__ == "__main__":
    unittest.main()
