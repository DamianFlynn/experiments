import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import render  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _enrich_via_store(golden_name):
    """End-to-end the new way (slice 7b-2): fold the raw fixture into the store,
    extract the window (which materializes artifacts/people from the stored
    nodes), then enrich. enrich no longer DERIVES artifacts/people, so a test that
    needs them must route through extract rather than feed the raw fixture to
    enrich directly."""
    with open(os.path.join(FIX, golden_name)) as fh:
        golden = json.load(fh)
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, json.loads(json.dumps(golden)))
    meta = golden["meta"]
    extracted = extract.extract(
        conn, meta["owner"], meta["repo"], meta["from"], meta["to"])
    return link.enrich(extracted)


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
        # idiomatic form: "<date> : <event...>" — date is the period
        self.assertIn("2026-05-03", mmd)
        self.assertIn("2026-05-25", mmd)
        # artifact names surface in the event text
        self.assertIn("firewall.md", mmd)
        # each dated line has the date as the period (not as an event)
        dated_lines = [ln for ln in mmd.splitlines() if "2026-05-03" in ln]
        self.assertTrue(dated_lines)
        self.assertTrue(dated_lines[0].strip().startswith("2026-05-03"))

    def test_timeline_placeholder_when_no_artifacts(self):
        mmd = render.emit_content_timeline(
            {"meta": {"from": "2026-05-01"}, "artifacts": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("timeline"))
        # empty-case placeholder: "<from> : no content events"
        self.assertIn("2026-05-01 : no content events", mmd)

    def test_timeline_placeholder_no_from_uses_em_dash(self):
        mmd = render.emit_content_timeline(
            {"meta": {}, "artifacts": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("timeline"))
        self.assertIn("no content events", mmd)


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
            # Phase 3b grows the manifest to six diagrams; assert the earlier
            # keys remain present rather than pinning the exact set.
            self.assertLessEqual(
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"},
                set(real))
            self.assertIn("content_timeline", b["diagrams"])
            self.assertIn("deltas_bar", b["diagrams"])


class TestEndToEndOfflineP3(unittest.TestCase):
    def test_link_then_render_builds_full_substrate(self):
        bundle = _enrich_via_store("bundle_p3.json")

        # artifacts: README change (live), doc add+remove (removed),
        # example renamed (old replaced -> new live)
        arts = bundle["artifacts"]
        doc = next(a for a in arts.values() if a["path"] == "docs/firewall.md")
        self.assertEqual(doc["status"], "removed")
        old_ex = arts[derive.artifact_id("examples/basic/main.bicep")]
        self.assertEqual(old_ex["status"], "replaced")
        self.assertEqual(old_ex["replaced_by"],
                         derive.artifact_id("examples/advanced/main.bicep"))

        # timeline: both layers, sorted, well-formed refs
        tl = bundle["timeline"]
        self.assertTrue(tl)
        self.assertEqual({e["layer"] for e in tl}, {"social", "code"})
        self.assertEqual([e["ts"] for e in tl], sorted(e["ts"] for e in tl))

        # feature_deltas: add/drop/change present; c1 -> PR 42
        kinds = {d["kind"] for d in bundle["feature_deltas"]}
        self.assertEqual(kinds, {"add", "drop", "change"})
        add42 = next(d for d in bundle["feature_deltas"]
                     if d["commit"].startswith("c1") and d["kind"] == "add")
        self.assertEqual(add42["pr"], 42)

        # render: four-diagram manifest (Phase 3b grows it; assert the earlier
        # keys remain present rather than pinning the exact set), validation stubbed
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(bundle, os.path.join(d, "diagrams"))
            self.assertLessEqual(
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"},
                set(real))

            class Ok:
                returncode = 0
                stderr = ""
            render.validate_with_mmdc(list(real.values()),
                                      runner=lambda cmd, **kw: Ok(),
                                      which=lambda _n: "/usr/bin/mmdc")


def _p3b_bundle():
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "people": {
            "alice": {"modules": ["avm/res/network/firewall-policy"],
                      "areas": ["avm/res/network/firewall-policy"]},
            "carol": {"modules": ["docs"], "areas": ["docs"]},
        },
        "modules": {
            "avm/res/network/firewall-policy": {"commits": 2, "prs": 1, "files_changed": 3},
            "docs": {"commits": 1, "prs": 1, "files_changed": 1},
        },
        "issues": [
            {"number": 1, "kind": "feature"}, {"number": 2, "kind": "feature"},
            {"number": 3, "kind": "bug"}, {"number": 4, "kind": "module-request"},
            {"number": 5, "kind": "other"},
        ],
    }


class TestContributorGraph(unittest.TestCase):
    def test_flowchart_header_and_people_area_edges(self):
        mmd = render.emit_contributor_graph(_p3b_bundle())
        self.assertTrue(mmd.startswith("flowchart"))
        self.assertIn("alice", mmd)
        self.assertIn("firewall-policy", mmd)
        # an edge arrow connects a person to an area
        self.assertIn("-->", mmd)

    def test_placeholder_when_no_people(self):
        mmd = render.emit_contributor_graph({"meta": {}, "people": {}, "modules": {}})
        self.assertTrue(mmd.startswith("flowchart"))
        self.assertIn("No contributor", mmd)


class TestKindBreakdown(unittest.TestCase):
    def test_pie_counts_by_kind(self):
        mmd = render.emit_kind_breakdown(_p3b_bundle())
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn('"feature" : 2', mmd)
        self.assertIn('"bug" : 1', mmd)
        self.assertIn('"module-request" : 1', mmd)

    def test_pie_placeholder_when_no_issues(self):
        mmd = render.emit_kind_breakdown({"meta": {}, "issues": []})
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn("No issues", mmd)


class TestRenderManifestP3b(unittest.TestCase):
    def test_render_includes_phase3b_diagrams(self):
        out = render.render(_p3b_bundle())
        self.assertTrue(out["contributor_graph"].startswith("flowchart"))
        self.assertTrue(out["kind_breakdown"].startswith("pie"))
        # Phase 2/3a diagrams still present
        for key in ("buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"):
            self.assertIn(key, out)

    def test_write_diagrams_manifest_gains_two_more_keys(self):
        b = _p3b_bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
        b["prs"] = []; b["releases"] = []
        b["artifacts"] = {}; b["feature_deltas"] = []
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(b, os.path.join(d, "diagrams"))
            self.assertLessEqual(
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
                 "contributor_graph", "kind_breakdown"},
                set(real))
            self.assertIn("contributor_graph", b["diagrams"])
            self.assertIn("kind_breakdown", b["diagrams"])


class TestEndToEndOfflineP3b(unittest.TestCase):
    def test_link_then_render_attributes_areas_and_renders_six(self):
        bundle = _enrich_via_store("bundle_p3b.json")

        # code_area filled on the example artifact (covered by the AVM area)
        ex = bundle["artifacts"][
            derive.artifact_id(
                "avm/res/network/firewall-policy/examples/basic/main.bicep")]
        self.assertEqual(ex["code_area"], "avm/res/network/firewall-policy")
        # docs artifact -> docs area
        doc = bundle["artifacts"][derive.artifact_id("docs/firewall.md")]
        self.assertEqual(doc["code_area"], "docs")

        # feature_deltas carry a real area now (no longer all null)
        self.assertTrue(any(d["area"] is not None for d in bundle["feature_deltas"]))

        # train for #17/PR42 carries the firewall-policy area
        t = next(t for t in bundle["trains"] if t["id"] == "train-issue-17")
        self.assertIn("avm/res/network/firewall-policy", t["code_areas"])

        # modules populated; alice owns the firewall-policy module
        self.assertIn("avm/res/network/firewall-policy", bundle["modules"])
        self.assertIn("avm/res/network/firewall-policy",
                      bundle["people"]["alice"]["modules"])

        # render: six-diagram manifest (Phase 3c grows it; assert the earlier
        # keys remain present rather than pinning the exact set)
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(bundle, os.path.join(d, "diagrams"))
            self.assertLessEqual(
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
                 "contributor_graph", "kind_breakdown"},
                set(real))


class TestModuleGraph(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "bundle_p3c.json")) as fh:
            self.bundle = json.load(fh)

    def test_flowchart_has_resolved_area_edges(self):
        mmd = render.emit_module_graph(self.bundle)
        self.assertTrue(mmd.startswith("flowchart"))
        # an edge from the ptn area to each resolved target
        self.assertEqual(mmd.count("-->"), 2)

    def test_edge_label_shows_version(self):
        mmd = render.emit_module_graph(self.bundle)
        self.assertIn("0.9.0", mmd)

    def test_placeholder_when_no_edges(self):
        empty = {"code_graph": {"provider": "directory", "areas": [
            {"id": "a", "label": "a", "paths": ["a/main.bicep"], "edges": []}]}}
        mmd = render.emit_module_graph(empty)
        self.assertIn("No module dependencies", mmd)

    def test_local_child_edge_is_named_and_marked_multi_instance(self):
        """A local child-submodule edge renders the named child node and a marker
        that distinguishes it as a local, multi-instance (array) dependency."""
        bundle = {"code_graph": {"areas": [
            {"id": "avm/res/network/vpn-gateway", "edges": [
                {"to": "avm/res/network/vpn-gateway/nat-rule", "kind": "module",
                 "ref": "nat-rule/main.bicep", "version": None, "transitive": False,
                 "local": True, "instances": "many", "resolved": True}]}]}}
        mmd = render.emit_module_graph(bundle)
        self.assertIn("nat-rule", mmd)            # child submodule named
        # local + multi-instance marker, quoted so `[]` doesn't break Mermaid.
        self.assertIn('|"child[]"|', mmd)

    def test_registered_in_render_manifest(self):
        names = set(render.render(self.bundle))
        self.assertIn("module_graph", names)


def _train_bundle():
    """A minimal enriched bundle with one deep (issue-rooted) train and one mention train."""
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "issues": [
            {"number": 17, "title": "Support policy param", "kind": "feature",
             "url": "https://github.com/o/r/issues/17", "milestone": "v1.2.0"},
        ],
        "prs": [
            {"number": 42, "title": "Add policy param", "merged": True,
             "milestone": "v1.2.0", "url": "https://github.com/o/r/pull/42"},
        ],
        "trains": [
            {
                "id": "train-issue-17",
                "kind": "feature",
                "root_issue": 17,
                "prs": [42],
                "commits": [],
                "code_areas": ["avm/res/network/firewall-policy"],
                "outcome": "shipped",
                "evidence": [],
                "significance": 10.0,
                "tier": "deep",
            },
            {
                "id": "train-pr-99",
                "kind": "other",
                "root_issue": None,
                "prs": [99],
                "commits": [],
                "code_areas": [],
                "outcome": "shipped",
                "evidence": [],
                "significance": 1.0,
                "tier": "mention",
            },
        ],
        "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        "releases": [],
        "artifacts": {},
        "feature_deltas": [],
        "people": {},
        "modules": {},
    }


class TestEmitTrainFlowchart(unittest.TestCase):
    def test_shipped_issue_rooted_has_issue_pr_and_outcome_nodes(self):
        b = _train_bundle()
        train = b["trains"][0]
        mmd = render.emit_train_flowchart(b, train)
        self.assertTrue(mmd.startswith("flowchart"))
        # issue node (title text present)
        self.assertIn("Support policy param", mmd)
        # PR node
        self.assertIn("Add policy param", mmd)
        # outcome node: shipped
        self.assertIn("Shipped", mmd)
        # edges present
        self.assertIn("-->", mmd)

    def test_shipped_issue_rooted_has_issue_to_pr_edge(self):
        b = _train_bundle()
        train = b["trains"][0]
        issue_number = train["root_issue"]   # 17
        pr_number = train["prs"][0]          # 42
        mmd = render.emit_train_flowchart(b, train)
        lines = mmd.splitlines()
        iss_id = render._node_id("iss", str(issue_number))
        pr_id = render._node_id("pr", str(pr_number))
        out_id = render._node_id("out", train["id"])
        # The specific issue -> PR edge must be present
        self.assertIn(f"    {iss_id} --> {pr_id}", lines)
        # The PR -> outcome edge must also be present
        self.assertIn(f"    {pr_id} --> {out_id}", lines)

    def test_pr_only_train_has_no_issue_node(self):
        b = _train_bundle()
        # Build a PR-only train (root_issue=None) with a known PR
        b["prs"].append({"number": 99, "title": "Standalone fix", "merged": True,
                         "milestone": None, "url": "https://github.com/o/r/pull/99"})
        train = b["trains"][1]  # train-pr-99
        mmd = render.emit_train_flowchart(b, train)
        self.assertTrue(mmd.startswith("flowchart"))
        # The only issue is #17 and its title should NOT appear
        self.assertNotIn("Support policy param", mmd)
        # PR title should appear
        self.assertIn("Standalone fix", mmd)

    def test_outcome_rejected_label(self):
        b = _train_bundle()
        train = dict(b["trains"][0])
        train["outcome"] = "rejected"
        mmd = render.emit_train_flowchart(b, train)
        self.assertIn("Rejected", mmd)
        self.assertNotIn("Shipped", mmd)
        self.assertNotIn("In flight", mmd)

    def test_outcome_open_becomes_in_flight(self):
        b = _train_bundle()
        train = dict(b["trains"][0])
        train["outcome"] = "open"
        mmd = render.emit_train_flowchart(b, train)
        self.assertIn("In flight", mmd)
        self.assertNotIn("Shipped", mmd)
        self.assertNotIn("Rejected", mmd)

    def test_outcome_unknown_becomes_in_flight(self):
        b = _train_bundle()
        train = dict(b["trains"][0])
        train["outcome"] = "in_flight"
        mmd = render.emit_train_flowchart(b, train)
        self.assertIn("In flight", mmd)

    def test_shipped_with_milestone_appended(self):
        b = _train_bundle()
        train = b["trains"][0]  # outcome=shipped, PR milestone=v1.2.0
        mmd = render.emit_train_flowchart(b, train)
        self.assertIn("v1.2.0", mmd)

    def test_shipped_no_milestone_no_arrow(self):
        b = _train_bundle()
        # Remove milestone from PR
        b["prs"][0]["milestone"] = None
        train = b["trains"][0]
        mmd = render.emit_train_flowchart(b, train)
        # Find the outcome node line (contains the outcome label)
        outcome_lines = [ln for ln in mmd.splitlines() if "Shipped" in ln]
        self.assertTrue(outcome_lines, "Expected an outcome node line containing 'Shipped'")
        outcome_line = outcome_lines[0]
        # When there is no milestone, the arrow glyph must NOT appear in the outcome node
        self.assertNotIn("Shipped →", outcome_line)

    def test_shipped_with_milestone_has_arrow_in_outcome_line(self):
        b = _train_bundle()
        train = b["trains"][0]  # outcome=shipped, PR milestone=v1.2.0
        mmd = render.emit_train_flowchart(b, train)
        # Find the outcome node line
        outcome_lines = [ln for ln in mmd.splitlines() if "Shipped" in ln]
        self.assertTrue(outcome_lines, "Expected an outcome node line containing 'Shipped'")
        outcome_line = outcome_lines[0]
        # Milestone arrow should appear in the outcome node label
        self.assertIn("Shipped →", outcome_line)
        self.assertIn("v1.2.0", outcome_line)

    def test_mode_c_adds_area_nodes(self):
        """Under the threshold: area nodes are emitted (mode C)."""
        b = _train_bundle()
        train = b["trains"][0]  # 1 PR, 1 area — well under defaults
        mmd = render.emit_train_flowchart(b, train)
        # area tail of 'avm/res/network/firewall-policy' is 'firewall-policy'
        self.assertIn("firewall-policy", mmd)

    def test_mode_a_no_area_nodes_when_too_many_prs(self):
        """Exceeding TRAIN_FLOW_MAX_PRS drops area annotation (mode A)."""
        b = _train_bundle()
        max_prs = render.TRAIN_FLOW_MAX_PRS
        # Build a train with max_prs+1 PRs and a few code areas
        pr_nums = list(range(100, 100 + max_prs + 1))
        for n in pr_nums:
            b["prs"].append({"number": n, "title": f"PR {n}", "merged": True,
                             "milestone": None, "url": f"u/{n}"})
        train = {
            "id": "train-issue-17",
            "kind": "feature",
            "root_issue": 17,
            "prs": pr_nums,
            "commits": [],
            "code_areas": ["area/foo", "area/bar"],
            "outcome": "shipped",
            "evidence": [],
        }
        mmd = render.emit_train_flowchart(b, train)
        # No area-annotation node declarations: the ("label") rounded-node shape is
        # unique to area nodes here, so assert those specific lines are absent
        # (more precise than a bare substring that a PR/issue title could contain).
        self.assertNotIn('("foo")', mmd)
        self.assertNotIn('("bar")', mmd)

    def test_mode_a_no_area_nodes_when_too_many_areas(self):
        """Exceeding TRAIN_FLOW_MAX_AREAS drops area annotation (mode A)."""
        b = _train_bundle()
        max_areas = render.TRAIN_FLOW_MAX_AREAS
        areas = [f"area/area{i}" for i in range(max_areas + 1)]
        train = {
            "id": "train-issue-17",
            "kind": "feature",
            "root_issue": 17,
            "prs": [42],
            "commits": [],
            "code_areas": areas,
            "outcome": "shipped",
            "evidence": [],
        }
        mmd = render.emit_train_flowchart(b, train)
        # area tails should NOT appear (area0, area1,...)
        self.assertNotIn("area0", mmd)

    def test_mode_c_at_max_threshold_keeps_areas(self):
        """Exactly at threshold (not over) stays in mode C."""
        b = _train_bundle()
        # Exactly TRAIN_FLOW_MAX_PRS PRs and TRAIN_FLOW_MAX_AREAS areas
        max_prs = render.TRAIN_FLOW_MAX_PRS
        max_areas = render.TRAIN_FLOW_MAX_AREAS
        pr_nums = list(range(200, 200 + max_prs))
        for n in pr_nums:
            b["prs"].append({"number": n, "title": f"PR {n}", "merged": True,
                             "milestone": None, "url": f"u/{n}"})
        areas = [f"area/zone{i}" for i in range(max_areas)]
        train = {
            "id": "train-issue-17",
            "kind": "feature",
            "root_issue": 17,
            "prs": pr_nums,
            "commits": [],
            "code_areas": areas,
            "outcome": "shipped",
            "evidence": [],
        }
        mmd = render.emit_train_flowchart(b, train)
        # In mode C: area node tails present
        self.assertIn("zone0", mmd)

    def test_duplicate_areas_do_not_force_mode_a(self):
        """6 code_areas entries with only 2 distinct values should stay in mode C."""
        b = _train_bundle()
        max_areas = render.TRAIN_FLOW_MAX_AREAS  # 5
        # Raw length (6) exceeds max_areas (5) but distinct count (2) does not.
        areas = ["area/alpha", "area/beta"] * 3  # 6 entries, 2 distinct
        self.assertGreater(len(areas), max_areas)
        self.assertLessEqual(len(set(areas)), max_areas)
        train = {
            "id": "train-issue-17",
            "kind": "feature",
            "root_issue": 17,
            "prs": [42],
            "commits": [],
            "code_areas": areas,
            "outcome": "shipped",
            "evidence": [],
        }
        mmd = render.emit_train_flowchart(b, train)
        # Must stay in mode C: area tails are present
        self.assertIn("alpha", mmd)
        self.assertIn("beta", mmd)
        # Only one node per distinct area (node id is deterministic from area name)
        alpha_id = render._node_id("area", "area/alpha")
        beta_id = render._node_id("area", "area/beta")
        # Each distinct area node declaration appears exactly once
        node_lines = [ln for ln in mmd.splitlines()
                      if alpha_id in ln and '("' in ln]
        self.assertEqual(len(node_lines), 1, "alpha area node should appear exactly once")
        node_lines_b = [ln for ln in mmd.splitlines()
                        if beta_id in ln and '("' in ln]
        self.assertEqual(len(node_lines_b), 1, "beta area node should appear exactly once")


class TestWriteDiagramsTrainFlowcharts(unittest.TestCase):
    def test_train_flowcharts_map_registered_for_deep_trains(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            render.write_diagrams(b, outdir)
            tf = b["diagrams"].get("train_flowcharts")
            self.assertIsInstance(tf, dict)
            # Only the deep train (train-issue-17), not the mention train (train-pr-99)
            self.assertIn("train-issue-17", tf)
            self.assertNotIn("train-pr-99", tf)

    def test_train_flowcharts_workspace_relative_paths(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            render.write_diagrams(b, outdir)
            tf = b["diagrams"]["train_flowcharts"]
            path = tf["train-issue-17"]
            # workspace-relative: should be diagrams/train-issue-17.mmd
            self.assertTrue(path.endswith("train-issue-17.mmd"))
            self.assertFalse(os.path.isabs(path))

    def test_train_flowchart_file_written_to_disk(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            real_paths = render.write_diagrams(b, outdir)
            # Real path for the deep train should be a key or in return value
            # It may be returned as a separate key in real_paths or embedded
            # Check the file exists on disk
            mmd_path = os.path.join(outdir, "train-issue-17.mmd")
            self.assertTrue(os.path.exists(mmd_path))

    def test_train_flowchart_real_paths_returned_for_validation(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            real_paths = render.write_diagrams(b, outdir)
            # real_paths should contain entries for each deep train flowchart
            # so mmdc can validate them
            self.assertIn("train-issue-17", real_paths)

    def test_flat_diagrams_unaffected(self):
        """The existing flat diagram keys must still be present."""
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            real_paths = render.write_diagrams(b, outdir)
            for key in ("buckets_pie", "timeline_gantt"):
                self.assertIn(key, real_paths)
                self.assertIn(key, b["diagrams"])


class TestTrainSpotlightCLI(unittest.TestCase):
    def test_parse_args_accepts_train_option(self):
        args = render.parse_args(["bundle.json", "--train", "train-issue-17"])
        self.assertEqual(args.train, "train-issue-17")

    def test_parse_args_train_defaults_to_none(self):
        args = render.parse_args(["bundle.json"])
        self.assertIsNone(args.train)

    def test_main_train_spotlight_writes_single_train(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            bundle_path = os.path.join(d, "b.json")
            with open(bundle_path, "w") as fh:
                json.dump(b, fh)
            outdir = os.path.join(d, "diagrams")
            manifest = render.main([bundle_path, "--diagrams-dir", outdir,
                                    "--train", "train-issue-17", "--skip-validate"])
            # Only the single train file was produced in the manifest
            self.assertIn("train-issue-17", manifest)
            # Flat diagrams should NOT appear in spotlight-only manifest
            self.assertNotIn("buckets_pie", manifest)

    def test_main_train_spotlight_works_for_mention_tier(self):
        """--train can render even a mention-tier train."""
        b = _train_bundle()
        # Add a PR for the mention train
        b["prs"].append({"number": 99, "title": "Mention fix", "merged": True,
                         "milestone": None, "url": "u/99"})
        with tempfile.TemporaryDirectory() as d:
            bundle_path = os.path.join(d, "b.json")
            with open(bundle_path, "w") as fh:
                json.dump(b, fh)
            outdir = os.path.join(d, "diagrams")
            manifest = render.main([bundle_path, "--diagrams-dir", outdir,
                                    "--train", "train-pr-99", "--skip-validate"])
            self.assertIn("train-pr-99", manifest)

    def test_main_train_registers_in_bundle_train_flowcharts_map(self):
        b = _train_bundle()
        with tempfile.TemporaryDirectory() as d:
            bundle_path = os.path.join(d, "b.json")
            with open(bundle_path, "w") as fh:
                json.dump(b, fh)
            outdir = os.path.join(d, "diagrams")
            render.main([bundle_path, "--diagrams-dir", outdir,
                         "--train", "train-issue-17", "--skip-validate"])
            written = json.load(open(bundle_path))
            tf = written["diagrams"].get("train_flowcharts", {})
            self.assertIn("train-issue-17", tf)


class TestTrainFlowchartMmdc(unittest.TestCase):
    @unittest.skipUnless(_mmdc_works(), "working mmdc (with browser) not available")
    def test_train_flowcharts_compile_via_mmdc(self):
        """Live gate: emitted train flowcharts must actually compile with mmdc."""
        b = _train_bundle()
        # Ensure there is at least one deep train
        b["trains"][0]["tier"] = "deep"
        with tempfile.TemporaryDirectory() as d:
            outdir = os.path.join(d, "diagrams")
            real_paths = render.write_diagrams(b, outdir)
            # Collect only the train flowchart paths for validation
            train_paths = [v for k, v in real_paths.items()
                           if k.startswith("train-")]
            self.assertTrue(train_paths, "No train flowchart paths to validate")
            render.validate_with_mmdc(train_paths)  # raises on failure


class TestProjectModuleGraph(unittest.TestCase):
    def test_cross_repo_edge_drawn_with_subgraphs(self):
        edges = [{"src_repo": "Azure/consumer", "src_area": "main.tf",
                  "dst_repo": "Azure/kv", "dst_area": "main.tf",
                  "version": "0.1.0", "transitive": False, "cross_repo": True}]
        mmd = render.emit_project_module_graph(edges)
        self.assertIn("flowchart", mmd)
        self.assertIn("Azure/consumer", mmd)
        self.assertIn("Azure/kv", mmd)
        self.assertIn("0.1.0", mmd)

    def test_empty_edges_placeholder(self):
        mmd = render.emit_project_module_graph([])
        self.assertIn("No cross-repo module dependencies", mmd)

    def test_long_repo_name_not_truncated_in_subgraph_title(self):
        # _subgraph_label is uncapped: a 46-char AVM repo slug appears in full.
        long_repo = "Azure/terraform-azurerm-avm-res-keyvault-vault"
        edges = [{"src_repo": long_repo, "src_area": "main.tf",
                  "dst_repo": "Azure/x", "dst_area": "main.tf",
                  "version": None, "transitive": False, "cross_repo": True}]
        mmd = render.emit_project_module_graph(edges)
        self.assertIn('["{}"]'.format(long_repo), mmd)  # full name, not clipped

    def test_same_area_name_in_two_repos_distinct_nodes(self):
        # both repos have a 'main.tf' area -> distinct node ids, two subgraphs.
        edges = [{"src_repo": "Azure/a", "src_area": "main.tf",
                  "dst_repo": "Azure/b", "dst_area": "main.tf",
                  "version": None, "transitive": False, "cross_repo": True}]
        mmd = render.emit_project_module_graph(edges)
        self.assertEqual(mmd.count("subgraph"), 2)
        self.assertEqual(mmd.count("end"), 2)
        # one node defined per repo (the m_ node lines), and exactly one arrow.
        self.assertEqual(sum(1 for ln in mmd.splitlines()
                             if ln.strip().startswith("m_") and "(" in ln), 2)
        self.assertEqual(mmd.count("-->"), 1)

    def test_transitive_only_edge_labelled_transitive(self):
        edges = [{"src_repo": "r/a", "src_area": "x", "dst_repo": "r/b",
                  "dst_area": "y", "version": None, "transitive": True,
                  "cross_repo": True}]
        mmd = render.emit_project_module_graph(edges)
        self.assertIn("transitive", mmd)

    def test_intra_repo_chain_node_defined_once(self):
        # a/app -> a/base and a/base -> a/core: 'a/base' is both dst and src,
        # all in one repo -> one subgraph, base defined exactly once.
        edges = [
            {"src_repo": "Az/a", "src_area": "app", "dst_repo": "Az/a",
             "dst_area": "base", "version": None, "transitive": False,
             "cross_repo": False},
            {"src_repo": "Az/a", "src_area": "base", "dst_repo": "Az/a",
             "dst_area": "core", "version": None, "transitive": False,
             "cross_repo": False},
        ]
        mmd = render.emit_project_module_graph(edges)
        self.assertEqual(mmd.count("subgraph"), 1)
        base_defs = sum(1 for ln in mmd.splitlines()
                        if ln.strip().startswith("m_") and ln.strip().endswith('("base")'))
        self.assertEqual(base_defs, 1)


if __name__ == "__main__":
    unittest.main()
