import json
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "examples", "formatters"))
import shipped_changelog  # noqa: E402
import transcript  # noqa: E402


class TestShippedChangelogFormatter(unittest.TestCase):
    def test_groups_by_repo_and_links(self):
        view = {
            "meta": {"project": "demo", "from": "2026-05-01", "to": "2026-05-31"},
            "shipped": [
                {"type": "pr", "id": 2, "url": "u2", "repo": "o/b", "train": "train-pr-2"},
                {"type": "pr", "id": 1, "url": "u1", "repo": "o/a"},
                {"type": "issue", "id": 9, "url": "u9", "repo": "o/a"},
            ],
        }
        out = shipped_changelog.render(view)
        self.assertIn("# demo — shipped 2026-05-01 → 2026-05-31", out)
        self.assertIn("## o/a", out)
        self.assertIn("## o/b", out)
        self.assertIn("[#1](u1)", out)
        self.assertIn("train `train-pr-2`", out)
        # o/a sorts before o/b (sorted repos); issue 9 + pr 1 both under o/a
        self.assertLess(out.index("## o/a"), out.index("## o/b"))

    def test_empty_shipped(self):
        out = shipped_changelog.render({"meta": {"owner": "o", "repo": "r"}, "shipped": []})
        self.assertIn("Nothing shipped", out)

    def test_cli_rejects_non_view(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w") as fh:
            json.dump({"not": "a view"}, fh)
        with self.assertRaises(SystemExit) as cm:
            shipped_changelog.main([path])
        self.assertEqual(cm.exception.code, 2)


class TestExampleTranscript(unittest.TestCase):
    def test_example_vtt_normalizes_clean(self):
        with open(os.path.join(HERE, "examples", "community-call.vtt"),
                  encoding="utf-8") as fh:
            out = transcript.normalize_transcript(fh.read())
        # structure stripped; spoken content + decisions/asks survive
        self.assertNotIn("-->", out)
        self.assertNotIn("WEBVTT", out)
        self.assertNotIn("<v", out)
        self.assertIn("Welcome everyone to the monthly AVM community call.", out)
        self.assertIn("breaking-change policy", out)


if __name__ == "__main__":
    unittest.main()
