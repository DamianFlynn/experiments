import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import render  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _bundle():
    with open(os.path.join(FIX, "bundle_p2.json")) as fh:
        return json.load(fh)


def _mmdc_works():
    """True only if a real `mmdc` can compile a trivial diagram — guards the
    live-validation test so a missing OR non-functional mmdc (e.g. Puppeteer
    Chrome absent) skips rather than fails."""
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "probe.mmd")
            out = os.path.join(d, "probe.svg")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write('pie\n    "A" : 1\n')
            result = subprocess.run([mmdc, "-i", src, "-o", out, "-q"],
                                    capture_output=True, text=True)
            return result.returncode == 0
    except Exception:
        return False


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
        # even a run longer than two percents must not leave a `%%` comment marker
        for title in ("Fix %% parser", "weird %%%% title"):
            b = _bundle()
            b["prs"][0]["title"] = title
            mmd = render.emit_timeline_gantt(b)
            self.assertNotIn("%%", mmd)


class TestWriteDiagrams(unittest.TestCase):
    def test_writes_files_and_manifest(self):
        b = _bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            real_paths = render.write_diagrams(b, outdir)
            # return value is the real on-disk paths (for validation)
            # Phase 3a grows the manifest; assert Phase 2 keys are still present.
            self.assertLessEqual({"buckets_pie", "timeline_gantt"}, set(real_paths))
            for name, path in real_paths.items():
                self.assertTrue(os.path.exists(path))
                self.assertTrue(path.endswith(f"{name}.mmd"))
            # Phase 3a grows the manifest; assert the Phase 2 entries are still
            # present and correct rather than pinning the full set.
            self.assertEqual(b["diagrams"]["buckets_pie"],
                             os.path.join("diagrams", "buckets_pie.mmd"))
            self.assertEqual(b["diagrams"]["timeline_gantt"],
                             os.path.join("diagrams", "timeline_gantt.mmd"))

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

    def test_real_mmdc_compiles_emitted_diagrams(self):
        # Probe lazily inside the test (not at import time) so collecting/​running
        # other tests never spawns mmdc, and an installed-but-broken mmdc skips
        # rather than fails.
        if not _mmdc_works():
            self.skipTest("working mmdc (with browser) not available")
        with tempfile.TemporaryDirectory() as d:
            real_paths = render.write_diagrams(_bundle(), d)
            render.validate_with_mmdc(list(real_paths.values()))  # raises on failure

    def test_ensure_mmdc_returns_path_when_present(self):
        self.assertEqual(render.ensure_mmdc(which=lambda _n: "/usr/bin/mmdc"),
                         "/usr/bin/mmdc")

    def test_validate_removes_temp_svg_when_no_export(self):
        created = []

        def fake_run(cmd, **kw):
            out = cmd[cmd.index("-o") + 1]
            with open(out, "w") as fh:
                fh.write("<svg/>")
            created.append(out)

            class R:
                returncode = 0
                stderr = ""
            return R()

        with tempfile.TemporaryDirectory() as d:
            mmd = os.path.join(d, "x.mmd")
            with open(mmd, "w") as fh:
                fh.write("pie\n")
            render.validate_with_mmdc([mmd], runner=fake_run,
                                      which=lambda _n: "/usr/bin/mmdc")
            self.assertTrue(created)
            self.assertFalse(os.path.exists(created[0]))  # temp svg cleaned up

    def test_validate_no_export_preserves_existing_sibling_svg(self):
        def fake_run(cmd, **kw):
            out = cmd[cmd.index("-o") + 1]
            with open(out, "w") as fh:
                fh.write("<svg/>")

            class R:
                returncode = 0
                stderr = ""
            return R()

        with tempfile.TemporaryDirectory() as d:
            mmd = os.path.join(d, "x.mmd")
            with open(mmd, "w") as fh:
                fh.write("pie\n")
            sibling = os.path.join(d, "x.svg")  # a user's pre-existing export
            with open(sibling, "w") as fh:
                fh.write("USER EXPORT")
            render.validate_with_mmdc([mmd], runner=fake_run,
                                      which=lambda _n: "/usr/bin/mmdc")
            self.assertTrue(os.path.exists(sibling))               # not deleted
            self.assertEqual(open(sibling).read(), "USER EXPORT")  # not overwritten

    def test_validate_keeps_exported_image(self):
        def fake_run(cmd, **kw):
            out = cmd[cmd.index("-o") + 1]
            with open(out, "w") as fh:
                fh.write("<svg/>")

            class R:
                returncode = 0
                stderr = ""
            return R()

        with tempfile.TemporaryDirectory() as d:
            mmd = os.path.join(d, "x.mmd")
            with open(mmd, "w") as fh:
                fh.write("pie\n")
            render.validate_with_mmdc([mmd], export="svg", runner=fake_run,
                                      which=lambda _n: "/usr/bin/mmdc")
            self.assertTrue(os.path.exists(os.path.join(d, "x.svg")))  # kept

    def test_main_skip_validate_writes_diagrams_and_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            bundle_path = os.path.join(d, "b.json")
            with open(bundle_path, "w") as fh:
                json.dump(_bundle(), fh)
            manifest = render.main([bundle_path, "--diagrams-dir",
                                    os.path.join(d, "dg"), "--skip-validate"])
            self.assertLessEqual({"buckets_pie", "timeline_gantt"}, set(manifest))
            written = json.load(open(bundle_path))
            self.assertLessEqual({"buckets_pie", "timeline_gantt"},
                                 set(written["diagrams"]))


class TestEndToEndOffline(unittest.TestCase):
    def test_link_then_render_produces_validated_diagrams(self):
        with open(os.path.join(FIX, "bundle_p2.json")) as fh:
            bundle = link.enrich(json.load(fh))
        # buckets populated by link
        self.assertTrue(bundle["buckets"]["shipped"])
        self.assertTrue(bundle["buckets"]["next_candidates"])
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(bundle, d)
            # validation path runs with a stubbed mmdc (returncode 0)
            class Ok:
                returncode = 0
                stderr = ""
            render.validate_with_mmdc(list(manifest.values()),
                                      runner=lambda cmd, **kw: Ok(),
                                      which=lambda _n: "/usr/bin/mmdc")
            self.assertLessEqual({"buckets_pie", "timeline_gantt"}, set(bundle["diagrams"]))
            self.assertTrue(os.path.exists(manifest["buckets_pie"]))


def _p3_bundle():
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "artifacts": {
            "art:docs/firewall.md": {
                "kind": "doc", "path": "docs/firewall.md", "name": "firewall.md",
                "status": "removed", "replaced_by": None, "code_area": None,
                "lifecycle": [
                    {"event": "add", "commit": "c1"*20, "author": "Alice",
                     "date": "2026-05-03", "ref": {"type": "commit", "id": "c1"*20,
                     "url": "https://github.com/o/r/commit/" + "c1"*20}},
                    {"event": "remove", "commit": "c4"*20, "author": "Dave",
                     "date": "2026-05-25", "ref": {"type": "commit", "id": "c4"*20,
                     "url": "https://github.com/o/r/commit/" + "c4"*20}},
                ]},
            "art:examples/basic/main.bicep": {
                "kind": "example", "path": "examples/basic/main.bicep",
                "name": "main.bicep", "status": "live", "replaced_by": None,
                "code_area": None,
                "lifecycle": [
                    {"event": "add", "commit": "c1"*20, "author": "Alice",
                     "date": "2026-05-03", "ref": {"type": "commit", "id": "c1"*20,
                     "url": "https://github.com/o/r/commit/" + "c1"*20}}]},
        },
        "feature_deltas": [
            {"kind": "add", "subject": "example", "name": "main.bicep",
             "artifact": "art:examples/basic/main.bicep", "author": "Alice",
             "url": "u", "area": None, "pr": 42, "train": "train-pr-42",
             "commit": "c1"*20},
            {"kind": "add", "subject": "doc", "name": "firewall.md",
             "artifact": "art:docs/firewall.md", "author": "Alice", "url": "u",
             "area": None, "pr": None, "train": None, "commit": "c1"*20},
            {"kind": "drop", "subject": "doc", "name": "firewall.md",
             "artifact": "art:docs/firewall.md", "author": "Dave", "url": "u",
             "area": None, "pr": None, "train": None, "commit": "c4"*20},
        ],
    }


class TestContentTimeline(unittest.TestCase):
    def test_timeline_header_and_dated_events(self):
        mmd = render.emit_content_timeline(_p3_bundle())
        self.assertTrue(mmd.startswith("timeline"))
        self.assertIn("2026-05-03", mmd)
        self.assertIn("2026-05-25", mmd)
        # artifact names surface in the event text
        self.assertIn("firewall.md", mmd)

    def test_timeline_placeholder_when_no_artifacts(self):
        mmd = render.emit_content_timeline(
            {"meta": {}, "artifacts": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("timeline"))
        self.assertIn("No content events", mmd)


class TestDeltasBar(unittest.TestCase):
    def test_bar_uses_xychart_and_counts_by_kind(self):
        mmd = render.emit_deltas_bar(_p3_bundle())
        self.assertTrue(mmd.startswith("xychart-beta"))
        # 2 add, 1 drop, 0 change -> bar series [2, 1, 0]
        self.assertIn("bar [2, 1, 0]", mmd)
        self.assertIn("add", mmd)
        self.assertIn("drop", mmd)
        self.assertIn("change", mmd)

    def test_bar_placeholder_when_no_deltas(self):
        mmd = render.emit_deltas_bar({"meta": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("xychart-beta"))
        self.assertIn("bar [0, 0, 0]", mmd)


class TestRenderManifestP3(unittest.TestCase):
    def test_render_includes_phase3a_diagrams(self):
        out = render.render(_p3_bundle())
        self.assertTrue(out["content_timeline"].startswith("timeline"))
        self.assertTrue(out["deltas_bar"].startswith("xychart-beta"))
        # Phase 2 diagrams still present
        self.assertIn("buckets_pie", out)
        self.assertIn("timeline_gantt", out)

    def test_write_diagrams_manifest_gains_two_keys(self):
        b = _p3_bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [],
                        "next_candidates": []}
        b["prs"] = []
        b["releases"] = []
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(b, os.path.join(d, "diagrams"))
            self.assertEqual(
                set(real),
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"})
            self.assertIn("content_timeline", b["diagrams"])
            self.assertIn("deltas_bar", b["diagrams"])


if __name__ == "__main__":
    unittest.main()
