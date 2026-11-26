"""`samples/build_report.py` — the structural reference formatter (issue #91).

Run as its README documents it: `build_report.py --view V --out R
[--diagrams-dir D] [--skip-validate]`. Every test runs the real script in a
subprocess, offline (`--skip-validate`: no mmdc), with a temp CWD so nothing
depends on the invocation directory.

Two inputs:

* the committed `samples/digest_view.json` (an older gather: no `meta.period`,
  `meta.ref_date` or `meta.schema_version`), which must still render, byte-stably,
  to the committed `samples/avm-tf-aiml-lz-digest.md`;
* a project view made NOW by `digest.py` over a small two-member store built from
  test_report_template's fixture, so every field the script reads is checked
  against current pipeline output rather than a document.
"""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import render  # noqa: E402
import test_report_template as trt  # noqa: E402  (offline store fixture)

SCRIPT = os.path.join(HERE, "samples", "build_report.py")
SAMPLE_VIEW = os.path.join(HERE, "samples", "digest_view.json")
SAMPLE_REPORT = os.path.join(HERE, "samples", "avm-tf-aiml-lz-digest.md")
MEMBERS = ("Azure/mod-a", "Azure/mod-b")
# Member diagrams the report embeds (blocker graph and train flowcharts are
# written but not embedded).
EMBEDDED = {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
            "contributor_graph", "kind_breakdown", "module_graph"}


def run_script(args, cwd):
    return subprocess.run([sys.executable, SCRIPT] + list(args), cwd=cwd,
                          capture_output=True, text=True, timeout=300)


def build_project_view(tmp):
    """digest.py over a two-member store (the test_report_template fixture)."""
    db = os.path.join(tmp, "project.db")
    members = set(MEMBERS)
    trt._fold(db, [
        (trt._rich_bundle("Azure", "mod-a", 40, edge_to="avm/res/net/pip"),
         {"project": "proj", "repo": "Azure/mod-a", "members": members}),
        (trt._rich_bundle("Azure", "mod-b", 60, area="avm/res/net/pip"),
         {"project": "proj", "repo": "Azure/mod-b", "members": members}),
    ])
    out = io.StringIO()
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
        assert digest.main(["--store", db, "--project", "proj",
                            "--from", trt.FROM, "--to", trt.TO]) == 0
    return json.loads(out.getvalue())


def build_caveat_view(tmp):
    """digest.py over test_report_template's caveat store (issue #107): mod-a
    carries meta.state_as_of, mod-b meta.facts_windows with a partial CI
    total."""
    db = os.path.join(tmp, "caveats.db")
    trt._fold(db, trt.project_folds())
    out = io.StringIO()
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
        assert digest.main(["--store", db, "--project", "proj",
                            "--from", trt.FROM, "--to", trt.TO]) == 0
    return json.loads(out.getvalue())


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)

    def write_view(self, view, name="view.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(view, fh)
        return path

    def render_report(self, view_path, name="report.md", diagrams=None):
        out = os.path.join(self.tmp, name)
        args = ["--view", view_path, "--out", out, "--skip-validate"]
        if diagrams:
            args += ["--diagrams-dir", diagrams]
        res = run_script(args, cwd=self.tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(out, encoding="utf-8") as fh:
            return fh.read()


class TestCommittedSample(_Base):
    def test_runs_on_sample_view_and_is_byte_stable(self):
        with open(SAMPLE_REPORT, "rb") as fh:
            committed_before = fh.read()
        d1, d2 = os.path.join(self.tmp, "d1"), os.path.join(self.tmp, "d2")
        a = self.render_report(SAMPLE_VIEW, "a.md", diagrams=d1)
        b = self.render_report(SAMPLE_VIEW, "b.md", diagrams=d2)
        self.assertEqual(a, b)
        # It renders the diagrams it embeds (it used to only read them).
        self.assertTrue(os.path.isfile(os.path.join(d1, "project_module_graph.mmd")))
        member = os.path.join(
            d1, "Azure__terraform-azurerm-avm-ptn-aiml-landing-zone")
        for name in ("buckets_pie", "timeline_gantt", "kind_breakdown",
                     "module_graph"):
            self.assertTrue(os.path.isfile(os.path.join(member, name + ".mmd")), name)
        # It never writes over the committed sample.
        with open(SAMPLE_REPORT, "rb") as fh:
            self.assertEqual(fh.read(), committed_before)

    def test_default_diagrams_dir_is_next_to_the_report(self):
        self.render_report(SAMPLE_VIEW, "r.md")
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "diagrams", "project_module_graph.mmd")))

    def test_committed_sample_report_is_current(self):
        """The committed report is exactly what the script makes from the
        committed view (validation does not change the bytes)."""
        out = self.render_report(SAMPLE_VIEW)
        with open(SAMPLE_REPORT, encoding="utf-8") as fh:
            self.assertEqual(out, fh.read())

    def test_no_wall_clock_date(self):
        out = self.render_report(SAMPLE_VIEW)
        self.assertNotIn("Generated", out)
        # The sample view predates meta.ref_date: the window end stands in.
        self.assertIn("| **Reference date** | 2026-03-22 |", out)


class TestCurrentPipelineView(_Base):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cls.view = build_project_view(tmp)

    def fresh_view(self):
        return copy.deepcopy(self.view)

    def test_renders_current_digest_output(self):
        view = self.fresh_view()
        out = self.render_report(self.write_view(view))
        self.assertIn("# proj — Project Activity Digest ({} → {})".format(
            trt.FROM, trt.TO), out)
        for m in view["members"]:
            meta = m["bundle"]["meta"]
            # GitHub links are built from meta.slug (BUNDLE.md: never owner/repo)
            self.assertIn("](https://github.com/{})".format(meta["slug"]), out)
        # Titles come from the item records (refs carry none).
        self.assertIn("feat: add firewall", out)
        self.assertIn("Add firewall module", out)

    def test_reference_date_is_members_ref_date(self):
        view = self.fresh_view()
        for m in view["members"]:
            m["bundle"]["meta"]["ref_date"] = "2026-05-20"
        out = self.render_report(self.write_view(view))
        self.assertIn("| **Reference date** | 2026-05-20 |", out)

    def test_no_completeness_claim_without_the_field(self):
        """This view carries no meta.boundary_dropped_commits (its fixture
        bundles record none; since #107 a view has it only when non-empty), so
        the report must not claim the ledgers are complete."""
        view = self.fresh_view()
        self.assertFalse(any("boundary_dropped_commits" in m["bundle"]["meta"]
                             for m in view["members"]))
        out = self.render_report(self.write_view(view))
        self.assertNotIn("boundary_dropped_commits", out)
        self.assertNotIn("ledgers are complete", out)

    def test_boundary_dropped_commits_warns_when_present(self):
        view = self.fresh_view()
        view["members"][0]["bundle"]["meta"]["boundary_dropped_commits"] = ["d" * 40]
        out = self.render_report(self.write_view(view))
        self.assertIn("`meta.boundary_dropped_commits` non-empty", out)
        self.assertIn("dddddddd", out)

    def test_codeowners_are_per_area_last_match(self):
        """Each area's owners come from the last matching CODEOWNERS rule, not
        the union of every owner in the file."""
        view = self.fresh_view()
        b = view["members"][0]["bundle"]
        b["code_owners"] = [{"pattern": "*", "owners": ["@everyone"]},
                            {"pattern": "/avm/res/net/", "owners": ["@net"]},
                            {"pattern": "/docs/", "owners": ["@docs"]}]
        out = self.render_report(self.write_view(view))
        rows = [ln for ln in out.splitlines()
                if ln.startswith("| `mod-a` | `{}` |".format(trt.AREA))]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("| `@net` |", rows[0])
        self.assertNotIn("@docs", rows[0])

    def test_reserved_workflow_keys_skipped(self):
        view = self.fresh_view()
        view["members"][0]["bundle"]["workflow_stats"]["_truncated"] = {
            "truncated": True, "cap": 1000, "fetched": 1000, "total_count": 1200}
        out = self.render_report(self.write_view(view))
        self.assertNotIn("_truncated", out)
        self.assertIn("| CI | 2 | 1 | 0 | 3 |", out)

    def test_truncated_diagram_is_captioned(self):
        view = self.fresh_view()
        n = render.GRAPH_MAX_EDGES + 10
        view["module_edges"] = [
            {"src_repo": "Azure/mod-a", "src_area": "src{}".format(i),
             "dst_repo": "Azure/mod-b", "dst_area": "dst{}".format(i),
             "version": None, "transitive": False, "cross_repo": True}
            for i in range(n)]
        out = self.render_report(self.write_view(view))
        self.assertEqual(out.count("_Showing {} of {} edges._".format(
            render.GRAPH_MAX_EDGES, n)), 1)
        # Every other caption is a member diagram render.py reports as capped,
        # and only those get one.
        expected = []
        for m in view["members"]:
            b = copy.deepcopy(m["bundle"])
            render.write_diagrams(b, os.path.join(self.tmp, "check", m["repo"]))
            for name, t in sorted(b["diagrams"]["truncated"].items()):
                if name in EMBEDDED:
                    expected.append("_Showing {} of {} {}._".format(
                        t["shown"], t["total"], t["unit"]))
        self.assertTrue(expected)    # the fixture's content timelines are capped
        captions = [ln for ln in out.splitlines() if ln.startswith("_Showing ")]
        self.assertEqual(sorted(captions), sorted(
            expected + ["_Showing {} of {} edges._".format(render.GRAPH_MAX_EDGES, n)]))

    def test_byte_stable(self):
        path = self.write_view(self.fresh_view())
        self.assertEqual(self.render_report(path, "a.md"),
                         self.render_report(path, "b.md"))


class TestReportCaveats(_Base):
    """Issue #107 item 10: the two caveats a view can carry are stated, each
    only when its key is present (the committed sample has neither, so its
    report is unchanged: TestCommittedSample)."""

    STATE = ("> **Records as of 2026-06-30** (`mod-a`): as last gathered on "
             "that date, after this window's end (2026-05-31); open/closed "
             "state, labels, milestones and comments may be newer than the "
             "window.")

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cls.view = build_caveat_view(tmp)

    def test_view_carries_both_keys(self):
        metas = {m["repo"]: m["bundle"]["meta"] for m in self.view["members"]}
        self.assertEqual(metas["Azure/mod-a"]["state_as_of"], "2026-06-30")
        self.assertNotIn("state_as_of", metas["Azure/mod-b"])
        fw = metas["Azure/mod-b"]["facts_windows"]
        self.assertIs(fw["workflow_stats_complete"], False)
        self.assertNotIn("facts_windows", metas["Azure/mod-a"])

    def test_state_as_of_line_under_the_title(self):
        out = self.render_report(self.write_view(copy.deepcopy(self.view)))
        lines = out.splitlines()
        self.assertEqual(lines.count(self.STATE), 1, out[:3000])
        self.assertLess(lines.index(self.STATE), lines.index("## Summary"))
        self.assertNotIn("(`mod-b`): as last gathered", out)

    def test_facts_windows_caveat_under_ci_health(self):
        out = self.render_report(self.write_view(copy.deepcopy(self.view)))
        member_b = out.split("### `Azure/mod-b`", 1)[1]
        ci = member_b.split("#### CI/CD health", 1)[1].split("####", 1)[0]
        self.assertIn("> CI counts are the sum of the gathered windows "
                      "2026-05-01 → 2026-05-20; **partial**: not every day of "
                      "the window is covered (not counted: 2026-05-15 → "
                      "2026-05-31). Code graph and owners from the 2026-05-15 "
                      "→ 2026-05-31 gather.", ci)
        member_a = out.split("### `Azure/mod-a`", 1)[1].split(
            "### `Azure/mod-b`", 1)[0]
        self.assertNotIn("CI counts are the sum", member_a)

    def test_complete_sum_is_not_called_partial(self):
        view = copy.deepcopy(self.view)
        meta = view["members"][1]["bundle"]["meta"]
        meta["facts_windows"]["workflow_stats_complete"] = True
        meta["facts_windows"].pop("workflow_stats_excluded")
        out = self.render_report(self.write_view(view))
        self.assertIn("> CI counts are the sum of the gathered windows "
                      "2026-05-01 → 2026-05-20. Code graph and owners from the "
                      "2026-05-15 → 2026-05-31 gather.", out)
        self.assertNotIn("**partial**", out)

    def test_ungathered_days_are_named(self):
        # issue #112 item 10: incomplete with nothing excluded — the days are
        # simply not gathered; the uncovered span is the view period minus the
        # summed windows, and the line names it.
        view = copy.deepcopy(self.view)
        meta = view["members"][1]["bundle"]["meta"]
        meta["facts_windows"].pop("workflow_stats_excluded")
        out = self.render_report(self.write_view(view))
        self.assertIn("> CI counts are the sum of the gathered windows "
                      "2026-05-01 → 2026-05-20; **partial**: not every day of "
                      "the window is covered (no gather covers the rest of the "
                      "window: 2026-05-21 → 2026-05-31). Code graph and owners "
                      "from the 2026-05-15 → 2026-05-31 gather.", out)

    def test_uncovered_spans_helper(self):
        sys.path.insert(0, os.path.join(HERE, "samples"))
        import build_report
        meta = {"period": {"from": "2026-05-01", "to": "2026-05-31"},
                "facts_windows": {"workflow_stats": [
                    {"from": "2026-05-05", "to": "2026-05-10"},
                    {"from": "2026-05-20", "to": "2026-05-25"}]}}
        self.assertEqual(build_report.uncovered_spans(meta), [
            {"from": "2026-05-01", "to": "2026-05-04"},
            {"from": "2026-05-11", "to": "2026-05-19"},
            {"from": "2026-05-26", "to": "2026-05-31"}])
        meta["facts_windows"]["workflow_stats"] = []
        self.assertEqual(build_report.uncovered_spans(meta),
                         [{"from": "2026-05-01", "to": "2026-05-31"}])
        self.assertEqual(build_report.uncovered_spans({"facts_windows": {}}), [])

    def test_caveat_is_shown_even_with_no_ci_rows(self):
        # a sub-window, a window gathered without workflows, or zero runs: the
        # view has a coverage caveat but no CI rows. The caveat must not vanish.
        view = copy.deepcopy(self.view)
        view["members"][1]["bundle"].pop("workflow_stats", None)
        out = self.render_report(self.write_view(view))
        member_b = out.split("### `Azure/mod-b`", 1)[1]
        self.assertIn("#### CI/CD health", member_b)
        ci = member_b.split("#### CI/CD health", 1)[1].split("####", 1)[0]
        self.assertIn("> CI counts are the sum of the gathered windows", ci)
        self.assertIn("**partial**", ci)
        self.assertIn("No CI runs are recorded for this view.", ci)
        self.assertNotIn("| Workflow |", ci)

    def test_without_the_keys_no_caveat(self):
        view = copy.deepcopy(self.view)
        for m in view["members"]:
            m["bundle"]["meta"].pop("state_as_of", None)
            m["bundle"]["meta"].pop("facts_windows", None)
        out = self.render_report(self.write_view(view))
        self.assertNotIn("Records as of", out)
        self.assertNotIn("CI counts are the sum", out)


class TestCli(_Base):
    def test_view_and_out_required(self):
        res = run_script([], cwd=self.tmp)
        self.assertEqual(res.returncode, 2)
        self.assertIn("--view", res.stderr)

    def test_rejects_non_project_view(self):
        path = self.write_view({"meta": {"owner": "o", "repo": "r"}})
        out = os.path.join(self.tmp, "r.md")
        res = run_script(["--view", path, "--out", out, "--skip-validate"],
                         cwd=self.tmp)
        self.assertEqual(res.returncode, 2)
        self.assertIn("not a digest.py project view", res.stderr)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
