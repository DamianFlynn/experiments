"""Regression tests for the diagram findings of issue #110: an abandoned train
drawn as "In flight" (item 2), Mermaid directive injection through untrusted
labels (item 3) and gantt release names read as gantt keywords (item 4).

The compile checks run the real mmdc through render.validate_with_mmdc. They
are skipped locally when no working mmdc is available and FAIL under CI
(test_render._require_mmdc)."""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import render  # noqa: E402
from test_render import _require_mmdc  # noqa: E402

# Untrusted text reaches the diagrams through PR / issue titles, release names,
# milestone titles, area ids and file paths, logins and unresolved git author
# names, label (kind) names, repo slugs and versions.
HOSTILE = [
    'Fix %%{init: {"theme":"dark"}}%% bug',
    '%%{init: {"theme":"dark"}}%%',
    "%%{init: {'theme':'forest', 'themeVariables': {'primaryColor': '#f00'}}}%%",
    '%%{ init : { "securityLevel" : "loose" } }%%',
    '"]; click A href "https://x"',
    'click A href "https://x"',
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "#lt;script#gt;alert(1)#lt;/script#gt;",
    "`backticks` and ``more``",
    "end",
    "graph TD",
    "#quot;",
    "x" * 300 + " very long",
    "Ünïcödé ✓ 日本語 — façade",
    "line one\nline two\r\nline three\rfour",
    "tab\there",
    "sep arator s",
    "50% done",
    "%starts with percent",
    "a %% b %%% c",
    "%%",
    ": starts with a colon",
    "accTitle: hijack",
    "section Danger",
    "title Hijack",
    "[brackets] (parens) {braces} |pipes|",
]


def _pr(n, title, **kw):
    pr = {"number": n, "title": title, "merged": True, "state": "closed",
          "created_at": "2026-01-03T00:00:00Z", "merged_at": "2026-01-05T00:00:00Z",
          "milestone": None}
    pr.update(kw)
    return pr


_META = {"from": "2026-01-01", "to": "2026-01-31"}


def _gantt_bundle(texts):
    return {"meta": dict(_META),
            "prs": [_pr(i + 1, t) for i, t in enumerate(texts)],
            "releases": [{"name": t, "tag_name": "v{}".format(i),
                          "published_at": "2026-01-{:02d}".format(i % 28 + 1)}
                         for i, t in enumerate(texts)]}


def _timeline_bundle(texts):
    arts = {}
    for i, t in enumerate(texts):
        arts["a{}".format(i)] = {"name": t, "kind": t, "lifecycle": [
            {"event": "add", "date": "2026-01-{:02d}".format(i % 28 + 1),
             "author": t}]}
    # a hostile date becomes "undated", never a period label
    arts["zz"] = {"name": "n", "kind": "k", "lifecycle": [
        {"event": "change", "date": texts[0], "author": "a"}]}
    return {"meta": dict(_META), "artifacts": arts}


def _contributor_bundle(texts):
    return {"people": {t: {"modules": [t, "mod/" + t]} for t in texts}}


def _kind_bundles(texts):
    # the pie keeps the 9 largest kinds; one bundle per 9 hostile kinds
    out = []
    for start in range(0, len(texts), render.PIE_MAX_SLICES - 1):
        chunk = texts[start:start + render.PIE_MAX_SLICES - 1]
        out.append({"issues": [{"number": i, "kind": t}
                               for i, t in enumerate(chunk)]})
    return out


def _train_bundle(texts):
    prs = [_pr(i + 10, t, milestone=t) for i, t in enumerate(texts)]
    issues = [{"number": 1, "title": texts[0]}]
    train = {"id": texts[1], "root_issue": 1, "outcome": texts[1],
             "prs": [p["number"] for p in prs], "code_areas": [],
             "pr_outcomes": {str(p["number"]): texts[i % len(texts)]
                             for i, p in enumerate(prs)}}
    return {"prs": prs, "issues": issues}, train


def _train_area_bundle(texts):
    prs = [_pr(10, texts[0], milestone=texts[0])]
    train = {"id": "train-pr-10", "root_issue": None, "outcome": "shipped",
             "prs": [10], "code_areas": list(texts)[:render.TRAIN_FLOW_MAX_AREAS],
             "pr_outcomes": {"10": "merged"}}
    return {"prs": prs, "issues": []}, train


def _module_bundle(texts):
    areas = []
    for i, t in enumerate(texts):
        areas.append({"id": t, "edges": [
            {"to": texts[(i + 1) % len(texts)], "resolved": True, "version": t}]})
    return {"code_graph": {"areas": areas}}


def _project_edges(texts):
    return [{"src_repo": t, "src_area": t, "dst_repo": texts[(i + 1) % len(texts)],
             "dst_area": texts[(i + 2) % len(texts)], "version": t,
             "transitive": False}
            for i, t in enumerate(texts)]


def emitted(texts):
    """[(name, diagram type, mermaid text)] for EVERY emitter, with `texts` in
    every label kind it draws."""
    out = [
        ("buckets_pie", "pie", render.emit_buckets_pie(
            {"meta": {"from": texts[0], "to": texts[-1]}, "buckets": {}})),
        ("timeline_gantt", "gantt", render.emit_timeline_gantt(_gantt_bundle(texts))),
        ("content_timeline", "timeline",
         render.emit_content_timeline(_timeline_bundle(texts))),
        ("deltas_bar", "xychart", render.emit_deltas_bar({"feature_deltas": []})),
        ("contributor_graph", "flowchart",
         render.emit_contributor_graph(_contributor_bundle(texts))),
        ("module_graph", "flowchart", render.emit_module_graph(_module_bundle(texts))),
        ("blocker_graph", "flowchart", render.emit_blocker_graph({"issues": [
            {"number": 1, "state": "open", "blocking": [2]},
            {"number": 2, "state": "open"}]})),
        ("project_module_graph", "flowchart",
         render.emit_project_module_graph(_project_edges(texts))),
    ]
    for n, b in enumerate(_kind_bundles(texts)):
        out.append(("kind_breakdown_{}".format(n), "pie", render.emit_kind_breakdown(b)))
    b, t = _train_bundle(texts)
    out.append(("train_flowchart", "flowchart", render.emit_train_flowchart(b, t)))
    b, t = _train_area_bundle(texts)
    out.append(("train_flowchart_areas", "flowchart",
                render.emit_train_flowchart(b, t)))
    return out


# Benign diagrams, one per diagram type, whose compiled <style> is the default
# theme's (a directive that reached Mermaid would change it).
def _baselines():
    plain = ["plain label"]
    return {
        "pie": render.emit_kind_breakdown({"issues": [{"number": 1, "kind": "bug"}]}),
        "gantt": render.emit_timeline_gantt(_gantt_bundle(plain)),
        "timeline": render.emit_content_timeline(_timeline_bundle(["2026-01-02"])),
        "flowchart": render.emit_contributor_graph(_contributor_bundle(plain)),
        "xychart": render.emit_deltas_bar({"feature_deltas": []}),
    }


def _first_style(svg):
    m = re.search(r"<style>(.*?)</style>", svg, re.S)
    return m.group(1) if m else None


def _compile(testcase, named_texts):
    """{name: svg} for each (name, mermaid text), compiled by the real mmdc
    through render.validate_with_mmdc (which also rejects error SVGs)."""
    out = {}
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in named_texts:
            path = os.path.join(d, name + ".mmd")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            paths.append((name, path))
        try:
            render.validate_with_mmdc([p for _, p in paths], export="svg")
        except RuntimeError as exc:
            testcase.fail("mmdc rejected a diagram: {}".format(exc))
        for name, path in paths:
            with open(os.path.splitext(path)[0] + ".svg", encoding="utf-8") as fh:
                out[name] = fh.read()
    return out


# --------------------------------------------------------------------- item 3
class TestSharedSanitiser(unittest.TestCase):
    def test_every_emitter_every_hostile_string_text_only(self):
        """(a) for each hostile string on its own: no `%%` (so no `%%{`
        directive and no comment marker) and no raw line break in a label,
        through every emitter."""
        for h in HOSTILE:
            for name, _kind, text in emitted([h, h]):
                with self.subTest(emitter=name, text=h[:40]):
                    self.assertNotIn("%%", text)
                    for line in text.splitlines():
                        self.assertNotIn("\t", line)
                    for bad in (" ", " ", "\r"):
                        self.assertNotIn(bad, text)

    def test_one_sanitiser_behind_every_label_helper(self):
        for fn in (render._flow_label, render._subgraph_label, render._pie_label,
                   render._gantt_label, render._timeline_text):
            with self.subTest(helper=fn.__name__):
                out = fn('Fix %%{init: {"theme":"dark"}}%% <b>`x`</b>\nbug')
                self.assertNotIn("%%", out)
                for ch in '"`<>\n':
                    self.assertNotIn(ch, out)
                self.assertTrue(out.startswith("Fix"))
        # readable: words, digits, unicode and single % survive
        self.assertEqual(render._mermaid_text('50% "done" 日本'), "50% 'done' 日本")
        self.assertEqual(render._mermaid_text("#quot; #12"), "＃quot; #12")
        self.assertEqual(render._mermaid_text("a %%% b"), "a % b")

    def test_hostile_strings_compile_with_mmdc_and_change_nothing(self):
        """(b) every emitter's diagram, with all hostile strings in every label
        kind, compiles; (c) its SVG has no script, no link the label asked
        for, no injected image, and the default theme's style."""
        _require_mmdc(self)
        diagrams = emitted(HOSTILE)
        svgs = _compile(self, [(n, t) for n, _k, t in diagrams])
        base = _compile(self, sorted(_baselines().items()))
        for name, kind, _text in diagrams:
            svg = svgs[name]
            with self.subTest(emitter=name):
                low = svg.lower()
                self.assertNotIn("<script", low)
                # the hostile text may be SHOWN; it must never become markup
                self.assertNotIn("<img", low)
                self.assertNotRegex(low, r"<[^>]*\sonerror\s*=")
                self.assertNotRegex(svg, r'href="https://x"')
                self.assertNotIn("<a ", svg)
                self.assertEqual(_first_style(svg), _first_style(base[kind]))

    def test_directive_only_label_does_not_fail_the_render(self):
        _require_mmdc(self)
        b = {"people": {"%%{init: {\"theme\":\"dark\"}}%%": {"modules": ["a"]}}}
        _compile(self, [("g", render.emit_contributor_graph(b))])


# --------------------------------------------------------------------- item 2
def _abandoned():
    bundle = {"issues": [{"number": 20, "title": "Drop the old API"}], "prs": []}
    train = {"id": "train-issue-20", "root_issue": 20, "issues": [20], "prs": [],
             "pr_outcomes": {}, "code_areas": [], "outcome": "abandoned"}
    return bundle, train


class TestTrainOutcomeNodes(unittest.TestCase):
    def test_abandoned_train_has_its_outcome_wired_to_the_issue(self):
        bundle, train = _abandoned()
        mmd = render.emit_train_flowchart(bundle, train)
        self.assertIn("Abandoned (not planned)", mmd)
        self.assertNotIn("In flight", mmd)
        issue = render._stable_node_id("iss", "20")
        outcome = render._stable_node_id("out", "train-issue-20")
        self.assertIn("    {} --> {}\n".format(issue, outcome), mmd)

    def test_no_outcome_falls_through_to_in_flight(self):
        bundle, train = _abandoned()
        for outcome, label in (("shipped", "Shipped"), ("in_flight", "In flight"),
                               ("rejected", "Rejected"),
                               ("abandoned", "Abandoned (not planned)"),
                               ("open", "open"), ("stalled", "stalled"),
                               ("", "Unknown outcome"), (None, "Unknown outcome")):
            with self.subTest(outcome=outcome):
                mmd = render.emit_train_flowchart(bundle, dict(train, outcome=outcome))
                out_line = [ln for ln in mmd.splitlines()
                            if ln.strip().startswith(render._stable_node_id(
                                "out", "train-issue-20") + "([")]
                self.assertEqual(len(out_line), 1)
                self.assertIn('"{}"'.format(label), out_line[0])
                if label != "In flight":
                    self.assertNotIn("In flight", mmd)

    def test_issue_with_prs_keeps_the_pr_edges_only(self):
        bundle = {"issues": [{"number": 1, "title": "t"}],
                  "prs": [{"number": 2, "title": "p"}]}
        train = {"id": "train-issue-1", "root_issue": 1, "prs": [2],
                 "pr_outcomes": {"2": "merged"}, "code_areas": [],
                 "outcome": "shipped"}
        mmd = render.emit_train_flowchart(bundle, train)
        self.assertNotIn("iss_1 --> out_", mmd)
        self.assertIn("pr_2 --> out_", mmd)

    def test_gantt_unknown_pr_state_is_not_active(self):
        mmd = render.emit_timeline_gantt({"meta": dict(_META), "prs": [
            {"number": 1, "title": "a", "merged": False, "state": None,
             "created_at": "2026-01-02T00:00:00Z"},
            {"number": 2, "title": "b", "merged": False, "state": "open",
             "created_at": "2026-01-02T00:00:00Z"}]})
        rows = [ln.strip() for ln in mmd.splitlines() if ln.strip().startswith("#")]
        self.assertEqual(rows, ["#1 a :2026-01-02, 2026-01-31",
                                "#2 b :active, 2026-01-02, 2026-01-31"])

    def test_abandoned_and_unknown_compile_with_mmdc(self):
        _require_mmdc(self)
        bundle, train = _abandoned()
        _compile(self, [
            ("abandoned", render.emit_train_flowchart(bundle, train)),
            ("unknown", render.emit_train_flowchart(
                bundle, dict(train, outcome="something else")))])


# --------------------------------------------------------------------- item 4
GANTT_KEYWORDS = ("section", "title", "dateFormat", "axisFormat", "excludes",
                  "includes", "todayMarker", "tickInterval", "weekday",
                  "weekend", "inclusiveEndDates", "topAxis", "click", "accTitle",
                  "accDescr", "accDescription", "gantt", "call", "href")


def _keyword_releases():
    """One release per keyword in a varied case (the lexer is
    case-insensitive), with and without trailing text, plus the statement
    forms; fewer than GANTT_MAX_RELEASES so no row is capped away."""
    names = []
    for i, kw in enumerate(GANTT_KEYWORDS):
        cased = (kw, kw.upper(), kw.capitalize())[i % 3]
        names.append(cased + " 508 compliance" if i % 2 else cased)
    names += ["weekday monday", "todayMarker off", "dateFormat YYYY",
              "excludes weekends", "accTitle: x", "accDescr { y }"]
    assert len(names) < render.GANTT_MAX_RELEASES
    return names


class TestGanttReleaseRows(unittest.TestCase):
    def _gantt(self, names):
        return render.emit_timeline_gantt({
            "meta": dict(_META), "prs": [],
            "releases": [{"name": n, "tag_name": "v{}".format(i),
                          "published_at": "2026-01-{:02d}".format(i % 28 + 1)}
                         for i, n in enumerate(names)]})

    def test_release_rows_carry_a_safe_prefix(self):
        names = _keyword_releases()
        mmd = self._gantt(names)
        lines = mmd.splitlines()
        self.assertEqual(lines[:4], ["gantt", "    title Timeline (2026-01-01 → 2026-01-31)",
                                     "    dateFormat YYYY-MM-DD",
                                     "    axisFormat %m-%d"])
        rows = [ln.strip() for ln in lines[5:]]
        self.assertEqual(len(rows), len(names))
        for row in rows:
            self.assertTrue(row.startswith("Release "), row)
            self.assertRegex(row, r" :milestone, 2026-01-\d\d, 0d$")

    def test_tag_used_when_unnamed_also_prefixed(self):
        mmd = render.emit_timeline_gantt({"meta": dict(_META), "releases": [
            {"name": None, "tag_name": "section", "published_at": "2026-01-02"}]})
        self.assertIn("    Release section :milestone, 2026-01-02, 0d", mmd)

    def test_every_keyword_release_renders_with_mmdc(self):
        _require_mmdc(self)
        names = _keyword_releases()
        svg = _compile(self, [("g", self._gantt(names))])["g"]
        self.assertIn("Timeline (2026-01-01 → 2026-01-31)", svg)
        for n in names:
            with self.subTest(release=n):
                shown = render._gantt_label("Release " + n)
                self.assertRegex(svg, r">{}\s*</text>".format(re.escape(shown)))
        # the axis still reads the window's dates (axisFormat %m-%d untouched)
        ticks = re.findall(r">(\d\d-\d\d)</text>", svg)
        self.assertTrue(ticks)
        self.assertTrue(all(t.startswith(("12-", "01-", "02-")) for t in ticks), ticks)


if __name__ == "__main__":
    unittest.main()
