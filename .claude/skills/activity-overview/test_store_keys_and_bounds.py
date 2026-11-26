"""Issue #111 items 2-4 — reader bounds, per-window meta keys, FTS coverage.

2. A reader window bound with a numeric UTC offset or fractional seconds was
   accepted (manifest.check_reader_window) but compared as a raw string with
   stored `...Z` timestamps, so two spellings of one instant gave different
   views. check_reader_window now returns the bounds normalised (dates as
   given, timestamps as UTC `YYYY-MM-DDTHH:MM:SSZ`) and every reader queries
   with them; graphstore.ts_in_window / range_query compare two timestamps to
   the second, so a stored ts with fractional seconds is not misordered.
3. Per-window meta keys are `prefix:{project}/{repo}:{from}:{to}`; a project
   name may contain `:` and bounds may be timestamps, so
   window_facts_problems read the wrong scope and validate reported false
   dangling `fact:` references. graphstore.split_window_key parses the scope
   from the first `/` instead.
4. A store folded on a host without FTS5 and later on one with it had an
   index of the later folds only, so `spotlight grep` silently missed older
   records. The first fold with FTS5 since a fold without it (meta
   `fts_coverage` not "complete") indexes every stored node the index lacks.

Offline: stores are built via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import manifest  # noqa: E402
import spotlight  # noqa: E402
import validate  # noqa: E402
import view  # noqa: E402


def _pr(n, merged_at, owner="o", repo="r", author="alice", body=""):
    return {"number": n, "title": "pr {}".format(n), "body": body,
            "author": author, "state": "closed", "merged": True,
            "created_at": "2026-01-02T00:00:00Z", "merged_at": merged_at,
            "closed_at": merged_at, "merged_by": "bob", "reviewers": [],
            "closes": [], "crossref_issues": [], "comments_list": [],
            "review_comments": [], "base": "main",
            "url": "https://github.com/{}/{}/pull/{}".format(owner, repo, n)}


def _bundle(frm, to, prs=(), commits=(), owner="o", repo="r", areas=()):
    return {"meta": {"owner": owner, "repo": repo, "from": frm, "to": to,
                     "base_branch": "main", "branches": ["main"]},
            "prs": [copy.deepcopy(p) for p in prs], "issues": [],
            "commits": [copy.deepcopy(c) for c in commits],
            "code_events": [], "symbol_events": [], "milestones": [],
            "releases": [],
            "code_graph": {"provider": "directory",
                           "areas": [copy.deepcopy(a) for a in areas]}}


def _store(path=":memory:"):
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    return conn


# --- item 2: reader bounds ------------------------------------------------------

# Three PRs around 2026-01-10T00:00:00Z.
PRS = [_pr(1, "2026-01-09T23:30:00Z"), _pr(2, "2026-01-10T00:00:00Z"),
       _pr(3, "2026-01-10T00:30:00Z")]
# Each tuple spells ONE instant (the reviewer's equal-instant pairs).
FROM_SPELLINGS = ("2026-01-10T00:00:00Z", "2026-01-10T01:00:00+01:00",
                  "2026-01-09T19:00:00-05:00", "2026-01-10T00:00:00.000Z",
                  "2026-01-10T00:00:00.999Z")
TO_SPELLINGS = ("2026-01-10T00:00:00Z", "2026-01-10T05:30:00+05:30",
                "2026-01-10T00:00:00.400Z", "2026-01-09T23:00:00-01:00")


class TestReaderBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "store.db")
        conn = _store(cls.path)
        gather.fold_bundle(conn, _bundle("2026-01-01", "2026-01-31", PRS))
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _view(self, frm, to):
        out = os.path.join(self.tmp.name, "view.json")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = view.main(["--store", self.path, "--from", frm, "--to", to,
                            "--out", out])
        self.assertEqual(rc, 0)
        with open(out) as fh:
            return json.load(fh)

    def _digest(self, frm, to):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = digest.main(["--store", self.path, "--project", "o",
                              "--from", frm, "--to", to])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def _spotlight(self, frm, to):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = spotlight.main(["person", "alice", "--store", self.path,
                                 "--from", frm, "--to", to])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_equal_instants_give_equal_views(self):
        base = self._view(FROM_SPELLINGS[0], "2026-01-31")
        self.assertEqual(sorted(p["number"] for p in base["prs"]), [2, 3])
        for frm in FROM_SPELLINGS[1:]:
            with self.subTest(frm=frm):
                self.assertEqual(self._view(frm, "2026-01-31"), base)
        upper = self._view("2026-01-01", TO_SPELLINGS[0])
        self.assertEqual(sorted(p["number"] for p in upper["prs"]), [1, 2])
        for to in TO_SPELLINGS[1:]:
            with self.subTest(to=to):
                self.assertEqual(self._view("2026-01-01", to), upper)

    def test_equal_instants_give_equal_digests(self):
        base = self._digest(FROM_SPELLINGS[0], TO_SPELLINGS[0])
        for frm, to in zip(FROM_SPELLINGS[1:], TO_SPELLINGS[1:]):
            with self.subTest(frm=frm, to=to):
                self.assertEqual(self._digest(frm, to), base)

    def test_equal_instants_give_equal_spotlight_results(self):
        base = self._spotlight(FROM_SPELLINGS[0], "2026-01-31")
        for frm in FROM_SPELLINGS[1:]:
            with self.subTest(frm=frm):
                self.assertEqual(self._spotlight(frm, "2026-01-31"), base)

    def test_normalised_bounds(self):
        for value, want in (("2026-01-10", "2026-01-10"),
                            ("2026-01-10T01:00:00+01:00", "2026-01-10T00:00:00Z"),
                            ("2026-01-10T00:00:00.999Z", "2026-01-10T00:00:00Z"),
                            ("2026-01-01T00:30:00+01:00", "2025-12-31T23:30:00Z")):
            with self.subTest(value=value):
                self.assertEqual(manifest.normalize_reader_bound(value), want)


class TestFractionalStoredTimestamps(unittest.TestCase):
    CASES = (
        # (ts, from, to, in window)
        ("2026-01-10T00:00:00.5Z", "2026-01-10T00:00:00Z", None, True),
        ("2026-01-10T00:00:00.5Z", None, "2026-01-10T00:00:00Z", True),
        ("2026-01-09T23:59:59.9Z", "2026-01-10T00:00:00Z", None, False),
        ("2026-01-10T00:00:01.1Z", None, "2026-01-10T00:00:00Z", False),
        ("2026-01-10T00:00:00Z", "2026-01-10T00:00:00Z", "2026-01-10T00:00:00Z",
         True),
    )

    def test_predicate_compares_to_the_second(self):
        for ts, frm, to, want in self.CASES:
            with self.subTest(ts=ts, frm=frm, to=to):
                self.assertIs(graphstore.ts_in_window(ts, frm, to), want)

    def test_range_query_agrees_with_the_predicate(self):
        conn = _store()
        for i, (ts, _f, _t, _w) in enumerate(self.CASES):
            graphstore.upsert_node(conn, "o/r#pr-{}".format(i), "o", "r",
                                   "social", ts, {"number": i})
        for ts, frm, to, want in self.CASES:
            with self.subTest(ts=ts, frm=frm, to=to):
                got = {n["ts"] for n in graphstore.range_query(
                    conn, "o", ["r"], frm, to)}
                self.assertIs(ts in got, want)


# --- item 3: per-window meta keys -------------------------------------------------

class TestWindowKeys(unittest.TestCase):
    PROJECT = "team:avm"
    MEMBERS = {"o/r"}
    FROM, TO = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
    AREAS = [{"id": "modules/a", "paths": ["modules/a/main.tf"]}]

    def _fold(self, conn, frm, to):
        day = frm[:8] + "10"
        b = _bundle(frm, to, [_pr(1, day + "T00:00:00Z")],
                    commits=[{"sha": "a" * 40, "author": "alice",
                              "date": day, "message": "x (#1)",
                              "files": ["modules/a/main.tf"]}],
                    areas=self.AREAS)
        b["code_owners"] = [{"pattern": "*", "owners": ["alice"]}]
        b["workflow_stats"] = {"runs": 1}
        return gather.fold_bundle(conn, b, project=self.PROJECT, repo="o/r",
                                  members=self.MEMBERS)

    def test_split_window_key(self):
        for key, want in (
                ("facts:team:avm/o/r:2026-01-01T00:00:00Z:2026-01-31T23:59:59Z",
                 ("team:avm/o/r", "2026-01-01T00:00:00Z:2026-01-31T23:59:59Z")),
                ("facts:o/r:2026-01-01:2026-01-31",
                 ("o/r", "2026-01-01:2026-01-31")),
                ("clone:a:b/o/r:2026-01-01:2026-01-31",
                 ("a:b/o/r", "2026-01-01:2026-01-31"))):
            with self.subTest(key=key):
                prefix = key.split(":", 1)[0] + ":"
                self.assertEqual(graphstore.split_window_key(key, prefix), want)
        self.assertIsNone(graphstore.split_window_key("facts:nokey", "facts:"))

    def test_colon_project_and_timestamp_window_end_to_end(self):
        for frm, to in ((self.FROM, self.TO), ("2026-02-01", "2026-02-28")):
            conn = _store()
            self._fold(conn, frm, to)
            with self.subTest(frm=frm):
                self.assertEqual(graphstore.window_facts_problems(conn), [])
                got = graphstore.get_window_facts(conn, self.PROJECT, "o/r",
                                                  frm, to)
                self.assertEqual(got["facts"]["code_graph"]["areas"][0]["id"],
                                 "modules/a")
                with contextlib.redirect_stderr(io.StringIO()):
                    v = extract.extract(conn, self.PROJECT, "o/r", frm, to)
                self.assertEqual([p["number"] for p in v["prs"]], [1])
                self.assertEqual(v["workflow_stats"], {"runs": 1})
                report = validate.validate_project(conn, self.PROJECT, ["o/r"])
                self.assertTrue(report["ok"],
                                json.dumps(report, indent=1, default=str))

    def test_damaged_entry_is_still_reported(self):
        conn = _store()
        self._fold(conn, self.FROM, self.TO)
        conn.execute("DELETE FROM meta WHERE key LIKE 'fact:%'")
        problems = graphstore.window_facts_problems(conn)
        self.assertTrue(problems)
        self.assertTrue(all("is not stored" in p for _k, p in problems))


# --- item 4: FTS coverage ---------------------------------------------------------

@unittest.skipUnless(graphstore.fts5_available(None), "FTS5 unavailable")
class TestFtsCoverage(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, graphstore, "_FTS5_CACHE",
                        graphstore._FTS5_CACHE)

    def _jan(self):
        return _bundle("2026-01-01", "2026-01-31",
                       [_pr(1, "2026-01-10T00:00:00Z", body="firewall rules")])

    def _feb(self):
        return _bundle("2026-02-01", "2026-02-28",
                       [_pr(2, "2026-02-10T00:00:00Z", body="dns zones")])

    def test_store_folded_without_fts_is_indexed_by_the_next_fold(self):
        graphstore._FTS5_CACHE = False      # a host without FTS5
        conn = _store()
        gather.fold_bundle(conn, self._jan())
        self.assertEqual(graphstore.get_meta(conn, graphstore.FTS_COVERAGE_KEY),
                         graphstore.FTS_PARTIAL)
        graphstore._FTS5_CACHE = True       # the same store on a host with it
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, self._feb())
        self.assertEqual(graphstore.fts_search(conn, "firewall"), ["o/r#pr-1"])
        self.assertEqual(graphstore.fts_search(conn, "dns"), ["o/r#pr-2"])
        self.assertEqual(graphstore.get_meta(conn, graphstore.FTS_COVERAGE_KEY),
                         graphstore.FTS_COMPLETE)
        res = spotlight.text_mining(conn, "o", "firewall")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["delivered"])

    def test_record_edited_without_fts_is_reindexed_by_the_next_fold(self):
        # an indexed PR is re-folded with a new body on a host without FTS5:
        # recovery must replace its old text, not only add missing nodes
        conn = _store()
        gather.fold_bundle(conn, self._jan())
        graphstore._FTS5_CACHE = False
        gather.fold_bundle(conn, _bundle(
            "2026-01-01", "2026-01-31",
            [_pr(1, "2026-01-10T00:00:00Z", body="gateway routes")]))
        graphstore._FTS5_CACHE = True
        gather.fold_bundle(conn, self._feb())
        self.assertEqual(graphstore.fts_search(conn, "gateway"), ["o/r#pr-1"])
        self.assertEqual(graphstore.fts_search(conn, "firewall"), [])
        self.assertEqual(graphstore.get_meta(conn, graphstore.FTS_COVERAGE_KEY),
                         graphstore.FTS_COMPLETE)

    def test_index_created_empty_on_an_older_store_is_rebuilt(self):
        conn = _store()
        gather.fold_bundle(conn, self._jan())
        # an older store: index empty, no coverage key
        conn.execute("DELETE FROM fts_text")
        conn.execute("DELETE FROM fts_map")
        conn.execute("DELETE FROM meta WHERE key=?",
                     (graphstore.FTS_COVERAGE_KEY,))
        conn.commit()
        gather.fold_bundle(conn, self._feb())
        self.assertEqual(graphstore.fts_search(conn, "firewall"), ["o/r#pr-1"])

    def test_first_fold_and_refold_index_the_same_rows(self):
        conn = _store()
        gather.fold_bundle(conn, self._jan())
        rows = sorted(tuple(r) for r in conn.execute(
            "SELECT rowid, node_id, text FROM fts_text"))
        gather.fold_bundle(conn, self._jan())
        self.assertEqual(sorted(tuple(r) for r in conn.execute(
            "SELECT rowid, node_id, text FROM fts_text")), rows)
        self.assertEqual(rows, [(1, "o/r#pr-1", "pr 1\nfirewall rules")])


if __name__ == "__main__":
    unittest.main()
