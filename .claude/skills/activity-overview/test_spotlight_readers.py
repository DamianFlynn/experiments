"""spotlight reader fixes from the fresh review (issue #113): M5 end to end
(`--complete` leaves a store the trust check accepts), M6 (module reads every
window's code graph, not the last folded one), m1 (untrusted text escaped in
`--md`), m3 (one set of argument forms for naming a repo and an area)."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402
import validate  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = spotlight.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _module_window(frm, to, area, sha, day):
    path = "{}/README.md".format(area)
    return {
        "meta": {"owner": "acme", "repo": "mods", "from": frm, "to": to},
        "prs": [], "issues": [], "milestones": [], "releases": [],
        "commits": [{"sha": sha, "author": "alice", "date": day,
                     "message": "touch {}".format(area), "files": [path]}],
        "code_events": [{"path": path, "change": "add", "commit": sha,
                         "author": "alice", "date": day}],
        "code_graph": {"provider": "directory",
                       "areas": [{"id": area, "paths": [path]}]},
    }


JAN = ("2026-01-01", "2026-01-31", "mod/a", SHA_A, "2026-01-05")
FEB = ("2026-02-01", "2026-02-28", "mod/b", SHA_B, "2026-02-05")


class ModuleReadsEveryWindowsCodeGraph(unittest.TestCase):
    """M6: on a multi-window store `module` used to read the `codegraph`
    singleton — the LAST FOLDED window's — so an area only an earlier window's
    graph lists answered needs_gather, depending on fold order."""

    def _fold(self, *windows):
        conn = _store()
        for w in windows:
            gather.fold_bundle(conn, _module_window(*w))
        return conn

    def test_both_fold_orders_give_the_same_answer(self):
        for area in ("mod/a", "mod/b"):
            a = spotlight.slice_module(self._fold(JAN, FEB), "acme", area)
            b = spotlight.slice_module(self._fold(FEB, JAN), "acme", area)
            self.assertEqual(a["status"], "ok", area)
            self.assertEqual(a, b, area)
            self.assertEqual([f["path"] for f in a["files"]],
                             ["{}/README.md".format(area)])

    def test_legacy_store_without_window_facts_reads_the_singleton(self):
        conn = self._fold(JAN)
        conn.execute("DELETE FROM meta WHERE key LIKE 'facts:%'")
        conn.commit()
        self.assertEqual(spotlight.slice_module(conn, "acme", "mod/a")["status"],
                         "ok")

    def test_unknown_area_still_needs_gather(self):
        res = spotlight.slice_module(self._fold(JAN, FEB), "acme", "mod/c")
        self.assertEqual(res["status"], "needs_gather")


def _area_store(conn, project, repos):
    """Area `docs` (depending on area `lib`) in each repo, with a code graph, a
    file artifact and a touching commit."""
    nodes, edges = [], []
    for i, repo in enumerate(repos):
        q = lambda local, repo=repo: graphstore.qualify_id(project, repo, local)
        sha = "{:040d}".format(i + 1)
        nodes += [
            (q("area-docs"), project, repo, "structure", None, {"id": "docs"}, None),
            (q("area-lib"), project, repo, "structure", None, {"id": "lib"}, None),
            (q("codegraph"), project, repo, "structure", None,
             {"areas": [{"id": "docs", "paths": ["docs/x.md"]}]}, None),
            (q("art:docs/x.md"), project, repo, "code", "2026-01-05",
             {"kind": "doc", "path": "docs/x.md", "status": "live"}, None),
            (q(sha), project, repo, "code", "2026-01-05",
             {"sha": sha, "author": "alice", "message": "edit docs"}, None)]
        edges += [(q(sha), q("area-docs"), "touches", None, None),
                  (q("area-docs"), q("area-lib"), "depends_on", None,
                   {"version": None, "transitive": False})]
    graphstore.upsert_nodes(conn, nodes)
    graphstore.upsert_edges(conn, edges)
    return conn


class ArgumentForms(unittest.TestCase):
    """m3: `dependents`, `subsystem` and `module` take the same forms for a
    repo and an area on a single-repo store (project=<owner>, repo=<name>) and
    on a project store (members `owner/repo`)."""

    def test_single_repo_store(self):
        conn = _area_store(_store(), "o", ["r"])
        for form in ("o/r", "O/R", "r", "R"):
            res = spotlight.member_dependents(conn, "o", form)
            self.assertEqual((res["status"], res["focus"]), ("ok", "r"), form)
            res = spotlight.area_dependents(conn, "o", form + "::lib")
            self.assertEqual(res["status"], "ok", form)
            self.assertEqual(res["dependents"], ["o/r::docs"], form)
            for fn in (spotlight.subsystem_split, spotlight.slice_module):
                a = fn(conn, "o", form + "::docs")
                b = fn(conn, "o", "docs", repo=form)
                self.assertEqual((a["status"], a["repos"]), ("ok", ["r"]), form)
                self.assertEqual(a, b, form)

    def test_project_store(self):
        conn = _area_store(_store(), "P", ["Az/a", "Az/b"])
        for form in ("Az/a", "az/A", "a", "A"):
            res = spotlight.member_dependents(conn, "P", form)
            self.assertEqual((res["status"], res["focus"]), ("ok", "Az/a"), form)
            res = spotlight.area_dependents(conn, "P", form + "::lib")
            self.assertEqual(res["dependents"], ["Az/a::docs"], form)
            for fn in (spotlight.subsystem_split, spotlight.slice_module):
                a = fn(conn, "P", form + "::docs")
                self.assertEqual((a["status"], a["repos"]), ("ok", ["Az/a"]), form)
                self.assertEqual(a, fn(conn, "P", "docs", repo=form), form)

    def test_ambiguous_and_unknown_names_need_gather(self):
        conn = _area_store(_store(), "P", ["Az/a", "Other/a"])
        for form in ("a", "zz", "Az/zz", "P/zz"):
            self.assertIsNone(spotlight.resolve_repo_arg(conn, "P", form), form)
            self.assertEqual(spotlight.member_dependents(conn, "P", form)["status"],
                             "needs_gather", form)
            self.assertEqual(spotlight.area_dependents(
                conn, "P", form + "::lib")["status"], "needs_gather", form)
            for fn in (spotlight.subsystem_split, spotlight.slice_module):
                self.assertEqual(fn(conn, "P", form + "::docs")["status"],
                                 "needs_gather", form)
                self.assertEqual(fn(conn, "P", "docs", repo=form)["status"],
                                 "needs_gather", form)

    def test_cli_unknown_repo_exits_2_listing_repos_for_every_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            _area_store(conn, "P", ["Az/a", "Other/a"])
            conn.close()
            for argv in (["dependents", "zz"], ["dependents", "a"],
                         ["dependents", "zz::lib"], ["subsystem", "zz::docs"],
                         ["module", "a::docs"], ["subsystem", "docs", "--repo", "zz"],
                         ["module", "docs", "--repo", "a"]):
                code, out, err = _run(argv + ["--store", db])
                self.assertEqual(code, 2, argv)
                self.assertEqual(out, "", argv)
                self.assertIn("'Az/a', 'Other/a'", err, argv)
            for argv in (["dependents", "az/A"], ["dependents", "other/a::lib"],
                         ["subsystem", "AZ/A::docs"], ["module", "docs", "--repo",
                                                       "other/A"]):
                code, out, err = _run(argv + ["--store", db])
                self.assertEqual(code, 0, (argv, err))
                self.assertEqual(json.loads(out)["status"], "ok", argv)

    def test_cli_single_repo_owner_repo_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            _area_store(conn, "o", ["r"])
            conn.close()
            for argv in (["dependents", "o/r"], ["dependents", "o/r::lib"],
                         ["subsystem", "o/r::docs"], ["module", "o/r::docs"],
                         ["module", "docs", "--repo", "O/R"],
                         ["subsystem", "r::docs"]):
                code, out, err = _run(argv + ["--store", db])
                self.assertEqual(code, 0, (argv, err))
                self.assertEqual(json.loads(out)["status"], "ok", argv)


EVIL = [
    "\n\n## Injected heading",
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "a `back`tick` phrase",
    "a | pipe | title",
    "> quoted\r\n- listed\n1. ordered",
    "# leading hash",
    "![img](http://x/y.png) **bold** <b>raw</b>",
]


def _train(v):
    return {"anchor": "P/o/r#pr-1", "key_date": "2026-01-01", "title": v,
            "outcome": "shipped", "repos": [v], "areas": [v], "roles": ["author"],
            "touchpoints": [{"id": "P/o/r#pr-1", "role": "author", "title": v,
                             "url": v, "excerpt": v, "ts": "2026-01-01"}],
            "timeline": [{"id": v, "kind": "comment", "ts": "2026-01-01",
                          "url": v, "excerpt": v, "event": v}],
            "gaps": [], "complete": True}


def _results(v):
    base = {"project": "P", "scope": {"from": "2026-01-01", "to": "2026-01-31"},
            "status": "ok", "focus": v}
    return [
        dict(base, query="person", focus_kind="person", is_bot=False,
             modules=[v], areas=[v], symbols_authored=[], authored_then_removed=[],
             summary={"trains_touched": 1, "shipped": 1, "authored": 1},
             delivered=[_train(v)]),
        dict(base, query="grep", focus_kind="grep",
             summary={"matches": 1, "trains": 1}, delivered=[_train(v)]),
        dict(base, query="symbol", focus_kind="symbol",
             summary={"events": 1, "outcome": "alive"},
             identity_chain=[{"id": v}, {"id": v, "confidence": v, "basis": v}],
             delivered=[_train(v)]),
        dict(base, query="subsystem", focus_kind="subsystem", repos=[v],
             summary={"trains": 1, "shipped": 1, "contributors": 1},
             contributors=[{"login": v, "relation": "touches", "repos": [v]}],
             depends_on={"out": [{"area": v, "id": "P/o/r#area-x", "repo": v,
                                  "version": v, "transitive": True}], "in": []},
             delivered=[_train(v)]),
        dict(base, query="module", focus_kind="module", repos=[v],
             time_range={"first": v, "last": v},
             summary={"symbols": 1, "files": 1, "trains": 1},
             symbols=[{"id": v, "status": v, "lifecycle": [
                 {"date": v, "event": v, "commit": v, "pr": v}],
                 "lifecycle_overflow": 0}], symbols_overflow=0,
             files=[{"path": v, "repo": v, "lifecycle": [], "lifecycle_overflow": 0}],
             files_overflow=0, trains=[_train(v)], delivered=[]),
        dict(base, query="dependents", focus_kind="area", dependents=[v, "x"],
             direct=[v]),
        dict(base, query="dependents", focus_kind="member", dependents=[v]),
        dict(base, query="person", focus_kind="person", status="needs_gather",
             guidance=v),
        dict(base, query="dependents", focus_kind="member", status="needs_gather",
             guidance=v),
        dict(base, query="grep", focus_kind="grep", status="fts_unavailable",
             guidance=v),
    ]


# A CommonMark code span: a backtick run, then the shortest text up to a run
# of the same length.
_CODE_SPAN = __import__("re").compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


_MERMAID = __import__("re").compile(r"(?ms)^```mermaid\n.*?^```$")


# A backslash-escaped punctuation character (cannot open a code span).
_ESCAPED = __import__("re").compile(r"\\[!-/:-@\[-`{-~]")


class MarkdownEscaping(unittest.TestCase):
    """m1: no stored or user-typed value can add markdown structure to
    `spotlight --md`: a heading, a block quote, a list, a link or image, raw
    HTML, a table cell, or a broken code span."""

    def _check(self, md, v, label):
        self.assertNotIn("\r", md, label)
        self.assertNotRegex(md, r"(?m)^\s*(?:[-*+]\s+)*#{1,6}\s+(Injected|leading)",
                            label)
        self.assertNotRegex(md, r"(?m)^\s*(?:[-*+]\s+)*(>|\d+\.)\s+(quoted|ordered)",
                            label)
        self.assertNotRegex(md, r"(?m)^\s*-\s+listed", label)
        # a code span shows its text literally (HTML, links and pipes inside
        # one are inert) as long as its fence is intact: check outside spans
        md = _CODE_SPAN.sub("", _ESCAPED.sub("§", _MERMAID.sub("", md)))
        self.assertNotIn("`", md, label)   # no stray (broken) fence left
        self.assertNotIn("<img", md, label)
        self.assertNotIn("<b>", md, label)
        self.assertNotIn("[click](", md, label)   # escaped ones are now §
        self.assertNotIn("[img", md, label)
        self.assertNotIn("|", md, label)

    def test_every_renderer_every_hostile_value(self):
        plain = [spotlight.render_md(r) for r in _results("plain")]
        for v in EVIL:
            for res, ref in zip(_results(v), plain):
                md = spotlight.render_md(res)
                label = (res["query"], res["status"], v)
                self._check(md, v, label)
                # the same shape as a harmless value: no line was added
                self.assertEqual(md.count("\n"), ref.count("\n"), label)

    def test_code_span_survives_backticks(self):
        res = _results("a `back`tick` phrase")[1]
        head = spotlight.render_md(res).splitlines()[0]
        self.assertEqual(head, "## spotlight: grep ``a `back`tick` phrase``")
        self.assertEqual(spotlight._md("`x`", code=True), "`` `x` ``")

    def test_text_stays_readable(self):
        self.assertEqual(spotlight._md("a | b"), "a \\| b")
        self.assertEqual(spotlight._md("<img src=x>"), "&lt;img src=x&gt;")
        self.assertEqual(spotlight._md("[click](javascript:alert(1))"),
                         "\\[click\\](javascript:alert(1))")
        self.assertEqual(spotlight._md("\n\n## Injected heading"),
                         " \\## Injected heading")
        self.assertEqual(spotlight._md("v1.2 of avm/res/net-fw"),
                         "v1.2 of avm/res/net-fw")
        self.assertEqual(spotlight._md("1. first"), "1\\. first")
        self.assertEqual(spotlight._md(None), "")

    def test_json_output_stays_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            title = "\n\n## Injected heading <img src=x onerror=alert(1)>"
            gather.fold_bundle(conn, {
                "meta": {"owner": "o", "repo": "r", "from": "2026-01-01",
                         "to": "2026-01-31"},
                "prs": [{"number": 1, "title": title, "author": "alice",
                         "merged": True, "merged_at": "2026-01-05T00:00:00Z",
                         "url": "https://github.com/o/r/pull/1"}],
                "issues": [], "commits": [], "code_events": [],
                "milestones": [], "releases": []})
            conn.close()
            code, out, _err = _run(["person", "alice", "--store", db])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["delivered"][0]["title"], title)
            code, out, _err = _run(["person", "alice", "--store", db, "--md"])
            self.assertNotIn("<img", out)
            self.assertNotRegex(out, r"(?m)^#+ Injected")



class CompleteLeavesAStoreTheGateAccepts(unittest.TestCase):
    """M5 end to end: `spotlight person --complete` with a token backfills the
    out-of-window issue a PR closes; the store then still passes validate."""

    def test_person_complete_then_validate(self):
        import test_backfill_invariants as tbi
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, tbi._window())
            conn.close()
            fetch = tbi.Fetcher({"issue-7": tbi.ISSUE7})
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "dummy"}), \
                    mock.patch.object(gather, "make_backfill_fetcher",
                                      return_value=fetch) as factory:
                code, out, err = _run(["person", "alice", "--store", db,
                                       "--from", "2026-03-01", "--to",
                                       "2026-03-31", "--complete"])
            self.assertEqual(code, 0, err)
            factory.assert_called_once_with("dummy")
            self.assertEqual(fetch.calls, ["issue-7"])
            res = json.loads(out)
            ids = {r["id"] for t in res["delivered"] for r in t["timeline"]}
            self.assertIn("acme/widget#issue-7", ids)
            conn = graphstore.open_store_readonly(db)
            try:
                report = validate.validate(conn)
                self.assertTrue(report.ok, [c for c in report.checks
                                            if not c["ok"]])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
