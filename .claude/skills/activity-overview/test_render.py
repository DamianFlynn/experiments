import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import render  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _bundle():
    with open(os.path.join(FIX, "bundle_p2.json")) as fh:
        return json.load(fh)


def _mmdc_works():
    """True only if a real `mmdc` can compile a trivial diagram — guards the
    live-validation test so a missing OR non-functional mmdc (e.g. Puppeteer
    Chrome absent) skips rather than fails."""
    if not shutil.which("mmdc"):
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "probe.mmd")
            out = os.path.join(d, "probe.svg")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write('pie\n    "A" : 1\n')
            result = subprocess.run([shutil.which("mmdc"), "-i", src, "-o", out, "-q"],
                                    capture_output=True, text=True)
            return result.returncode == 0
    except Exception:
        return False


_MMDC_OK = _mmdc_works()


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
        b["meta"]["to"] = "2026-05-31"
        b["prs"] = [{"number": 9, "title": "late", "state": "open",
                     "created_at": "2026-06-15T00:00:00Z",
                     "merged_at": None, "closed_at": None, "merged": False}]
        b["releases"] = []
        mmd = render.emit_timeline_gantt(b)
        # created_at (2026-06-15) is after window end; end is clamped to start,
        # so the task line uses 2026-06-15 for BOTH start and end.
        task = [ln for ln in mmd.splitlines() if "late" in ln.strip()][0]
        self.assertEqual(task.count("2026-06-15"), 2)

    def test_gantt_label_strips_comment_marker(self):
        b = _bundle()
        b["prs"][0]["title"] = "Fix %% parser"
        mmd = render.emit_timeline_gantt(b)
        self.assertNotIn("%%", mmd)


class TestWriteDiagrams(unittest.TestCase):
    def test_writes_files_and_manifest(self):
        b = _bundle()
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(b, d)
            self.assertEqual(set(manifest), {"buckets_pie", "timeline_gantt"})
            for name, path in manifest.items():
                self.assertTrue(os.path.exists(path))
                self.assertTrue(path.endswith(f"{name}.mmd"))
            # manifest is recorded back onto the bundle for downstream stages
            self.assertEqual(b["diagrams"], manifest)

    def test_render_returns_mmd_text_per_diagram(self):
        out = render.render(_bundle())
        self.assertTrue(out["buckets_pie"].startswith("pie"))
        self.assertTrue(out["timeline_gantt"].startswith("gantt"))


class TestMmdcValidation(unittest.TestCase):
    def test_ensure_mmdc_raises_with_hint_when_absent(self):
        with self.assertRaises(SystemExit) as ctx:
            render.ensure_mmdc(which=lambda _name: None)
        self.assertEqual(ctx.exception.code, 3)

    def test_validate_raises_on_nonzero_mmdc(self):
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            class R:
                returncode = 1
                stderr = "Parse error on line 2"
            return R()

        with self.assertRaises(RuntimeError) as ctx:
            render.validate_with_mmdc(["a.mmd"], runner=fake_run,
                                      which=lambda _n: "/usr/bin/mmdc")
        self.assertIn("Parse error", str(ctx.exception))

    @unittest.skipUnless(_MMDC_OK, "working mmdc (with browser) not available")
    def test_real_mmdc_compiles_emitted_diagrams(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(_bundle(), d)
            render.validate_with_mmdc(list(manifest.values()))  # raises on failure


if __name__ == "__main__":
    unittest.main()
