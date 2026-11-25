"""Issue #52 — window bounds are granularity-safe.

Node `ts` mixes date-only strings (commits, artifacts: `YYYY-MM-DD`) with ISO
datetimes (PRs, issues, reviews, events). Window bounds arrive in either form
(`--to 2026-05-31` or `...T23:59:59Z`). Every window comparison must treat a
date-only value as covering its whole day, so that:

- date-only bounds keep ISO-ts nodes on the first AND last day, and
- ISO bounds keep date-only-ts nodes (commits) on the first AND last day.

The single home of the rule is `graphstore.ts_in_window` (Python) and the
`range_query` SQL; every other window check delegates to it.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import complete  # noqa: E402
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import spotlight  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


DATE_BOUNDS = ("2026-05-01", "2026-05-31")
ISO_BOUNDS = ("2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z")


def _ids(nodes):
    return sorted(n["id"] for n in nodes)


class RangeQueryGranularity(unittest.TestCase):
    def setUp(self):
        self.conn = _store()
        rows = [
            # ISO ts on the first / last day
            ("o/r#pr-1", "2026-05-01T00:30:00Z"),
            ("o/r#pr-2", "2026-05-31T15:00:00Z"),
            ("o/r#pr-3", "2026-05-31T23:59:59Z"),
            # date-only ts (commits) on the first / last day
            ("o/r#commit-a", "2026-05-01"),
            ("o/r#commit-b", "2026-05-31"),
            # just outside, both granularities
            ("o/r#pr-0", "2026-04-30T23:59:59Z"),
            ("o/r#pr-9", "2026-06-01T00:00:00Z"),
            ("o/r#commit-0", "2026-04-30"),
            ("o/r#commit-9", "2026-06-01"),
        ]
        for nid, ts in rows:
            graphstore.upsert_node(self.conn, nid, "o", "r", "social", ts,
                                   {"url": "u"})
        self.inside = sorted(["o/r#pr-1", "o/r#pr-2", "o/r#pr-3",
                              "o/r#commit-a", "o/r#commit-b"])

    def test_issue_repro_last_day_iso_ts_with_date_bounds(self):
        c = _store()
        graphstore.upsert_node(c, "o/r#pr-1", "o", "r", "social",
                               "2026-05-31T15:00:00Z", {"url": "u"})
        got = graphstore.range_query(c, "o", ["r"], "2026-05-01", "2026-05-31")
        self.assertEqual(_ids(got), ["o/r#pr-1"])

    def test_date_bounds_iso_ts_first_and_last_day(self):
        got = _ids(graphstore.range_query(self.conn, "o", ["r"], *DATE_BOUNDS))
        for nid in ("o/r#pr-1", "o/r#pr-2", "o/r#pr-3"):
            self.assertIn(nid, got)

    def test_date_bounds_date_ts_first_and_last_day(self):
        got = _ids(graphstore.range_query(self.conn, "o", ["r"], *DATE_BOUNDS))
        self.assertIn("o/r#commit-a", got)
        self.assertIn("o/r#commit-b", got)

    def test_iso_bounds_date_ts_first_and_last_day(self):
        got = _ids(graphstore.range_query(self.conn, "o", ["r"], *ISO_BOUNDS))
        self.assertIn("o/r#commit-a", got)
        self.assertIn("o/r#commit-b", got)

    def test_iso_bounds_iso_ts_first_and_last_day(self):
        got = _ids(graphstore.range_query(self.conn, "o", ["r"], *ISO_BOUNDS))
        for nid in ("o/r#pr-1", "o/r#pr-2", "o/r#pr-3"):
            self.assertIn(nid, got)

    def test_all_four_combinations_are_exact_and_agree(self):
        for bounds in (DATE_BOUNDS, ISO_BOUNDS,
                       ("2026-05-01", "2026-05-31T23:59:59Z"),
                       ("2026-05-01T00:00:00Z", "2026-05-31")):
            got = _ids(graphstore.range_query(self.conn, "o", ["r"], *bounds))
            self.assertEqual(got, self.inside, bounds)

    def test_iso_bounds_still_precise_against_iso_ts(self):
        # A sub-day ISO bound keeps full precision against an ISO ts, but a
        # date-only ts on that day overlaps the window (its whole day counts).
        got = _ids(graphstore.range_query(
            self.conn, "o", ["r"], "2026-05-31T16:00:00Z", "2026-05-31T23:00:00Z"))
        self.assertEqual(got, ["o/r#commit-b"])
        got = _ids(graphstore.range_query(
            self.conn, "o", ["r"], "2026-05-01T01:00:00Z", "2026-05-31T15:00:00Z"))
        self.assertEqual(got, sorted(["o/r#commit-a", "o/r#commit-b",
                                      "o/r#pr-2"]))

    def test_python_predicate_agrees_with_sql(self):
        all_nodes = graphstore.repo_nodes(self.conn, "o", "r")
        bounds_list = [DATE_BOUNDS, ISO_BOUNDS,
                       ("2026-05-31T16:00:00Z", "2026-05-31T23:00:00Z"),
                       ("2026-05-01T01:00:00Z", "2026-05-31T15:00:00Z"),
                       ("2026-05-31", "2026-05-31")]
        for frm, to in bounds_list:
            sql = _ids(graphstore.range_query(self.conn, "o", ["r"], frm, to))
            py = sorted(n["id"] for n in all_nodes
                        if graphstore.ts_in_window(n["ts"], frm, to))
            self.assertEqual(sql, py, (frm, to))

    def test_order_is_still_ts_then_id(self):
        got = graphstore.range_query(self.conn, "o", ["r"], *DATE_BOUNDS)
        keys = [(n["ts"], n["id"]) for n in got]
        self.assertEqual(keys, sorted(keys))

    def test_window_scan_still_uses_index(self):
        sql, params = graphstore._range_query_sql(
            "o", ["r"], "2026-05-01", "2026-05-31", None)
        plan = self.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn("idx_nodes_window", detail)


class TsInWindow(unittest.TestCase):
    def test_semantics(self):
        f = graphstore.ts_in_window
        self.assertTrue(f("2026-05-31T15:00:00Z", "2026-05-01", "2026-05-31"))
        self.assertTrue(f("2026-05-01", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z"))
        self.assertTrue(f("2026-05-31", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z"))
        self.assertFalse(f("2026-06-01T00:00:00Z", "2026-05-01", "2026-05-31"))
        self.assertFalse(f("2026-04-30", "2026-05-01T00:00:00Z", None))
        self.assertTrue(f("2026-05-02", None, None))
        self.assertFalse(f(None, None, None))
        self.assertFalse(f("", "2026-05-01", None))


class CallersAreConsistent(unittest.TestCase):
    def test_gather_in_window_iso_from_keeps_first_day(self):
        self.assertTrue(gather.in_window("2026-03-01", "2026-03-01T00:00:00Z",
                                         "2026-03-31T23:59:59Z"))
        self.assertTrue(gather.in_window("2026-03-01T08:00:00Z",
                                         "2026-03-01T00:00:00Z", "2026-03-31"))

    def test_gather_runs_paging_does_not_stop_on_first_day_with_iso_from(self):
        pages = {
            "start": ([{"id": 2, "created_at": "2026-03-01T09:00:00Z"}], "p2"),
            "p2": ([{"id": 1, "created_at": "2026-03-01T01:00:00Z"}], None),
        }
        calls = []

        def get_page(url):
            key = "start" if "actions/runs" in url else url
            calls.append(key)
            items, nxt = pages[key]
            return {"workflow_runs": items}, nxt

        runs = gather._runs_page(get_page, "https://api", "2026-03-01T00:00:00Z",
                                 "2026-03-31T23:59:59Z")
        self.assertEqual([r["id"] for r in runs], [2, 1])

    def test_link_in_window_iso_from_keeps_first_day(self):
        period = {"from": "2026-03-01T00:00:00Z", "to": "2026-03-31T23:59:59Z"}
        self.assertTrue(link._in_window("2026-03-01T08:00:00Z", period))
        self.assertTrue(link._in_window("2026-03-01", period))
        self.assertFalse(link._in_window("2026-02-28T23:00:00Z", period))

    def test_complete_in_window_keeps_first_day_commit(self):
        win = ("2026-03-01T00:00:00Z", "2026-03-31T23:59:59Z")
        self.assertTrue(complete._in_window({"ts": "2026-03-01"}, win))
        self.assertTrue(complete._in_window({"ts": "2026-03-31"}, win))
        self.assertTrue(complete._in_window(
            {"ts": "2026-03-31T15:00:00Z"}, ("2026-03-01", "2026-03-31")))

    def test_spotlight_window_keeps_first_day_commit(self):
        win = spotlight._completion_window("2026-03-01", "2026-03-31")
        self.assertTrue(spotlight._ts_in_window("2026-03-01", win))
        self.assertTrue(spotlight._ts_in_window("2026-03-31T15:00:00Z", win))
        win = spotlight._completion_window("2026-03-01T00:00:00Z",
                                           "2026-03-31T23:59:59Z")
        self.assertTrue(spotlight._ts_in_window("2026-03-01", win))
        self.assertTrue(spotlight._date_in_range("2026-03-01",
                                                 "2026-03-01T00:00:00Z", None))


class _FakeFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, kind, local, qid):
        self.calls.append(local)
        return self.payloads.get(local) if kind == "social" else None


class ExtractLastDay(unittest.TestCase):
    def test_pr_merged_on_last_day_seeds_a_train_with_date_only_to(self):
        bundle = {
            "meta": {"owner": "acme", "repo": "widget",
                     "from": "2026-05-01", "to": "2026-05-31"},
            "prs": [{"number": 10, "title": "Fix", "merged": True,
                     "merged_at": "2026-05-31T15:00:00Z", "author": "alice",
                     "closes": [7],
                     "url": "https://github.com/acme/widget/pull/10"}],
            "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
        }
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(bundle))
        fake = _FakeFetcher({"issue-7": {
            "node": {"number": 7, "title": "leak", "state": "closed",
                     "closed_at": "2026-04-01T00:00:00Z", "author": "bob",
                     "url": "https://github.com/acme/widget/issues/7"},
            "edges": []}})
        b = extract.extract(conn, "acme", "widget", "2026-05-01", "2026-05-31",
                            backfill=lambda c, i: gather.backfill(c, i, fetch=fake),
                            warn=lambda m: None)
        # The last-day PR seeded its train, so its closing issue was backfilled.
        self.assertEqual(fake.calls, ["issue-7"])
        self.assertIn(7, {i["number"] for i in b["issues"]})


def _seed_last_day_cross_repo(conn):
    bundle_a = {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-03-01",
                 "to": "2026-03-31", "base_branch": "main"},
        "prs": [{"number": 10, "url": "uA/10", "state": "closed", "merged": True,
                 "base": "main", "head": "f10",
                 "merged_at": "2026-03-31T15:00:00Z",
                 "created_at": "2026-03-20T00:00:00Z",
                 "closed_at": "2026-03-31T15:00:00Z",
                 "closes": [], "crossref_issues": [],
                 "title": "feat: thing", "body": "Closes Azure/mod-b#3"}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    bundle_b = {
        "meta": {"owner": "Azure", "repo": "mod-b", "from": "2026-03-01",
                 "to": "2026-03-31", "base_branch": "main"},
        "prs": [], "issues": [{"number": 3, "url": "uB/3", "state": "closed",
                               "closed_at": "2026-03-31T15:00:00Z",
                               "updated_at": "2026-03-31T15:00:00Z"}],
        "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    members = {"Azure/mod-a", "Azure/mod-b"}
    gather.fold_bundle(conn, bundle_a, project="proj", repo="Azure/mod-a",
                       members=members)
    gather.fold_bundle(conn, bundle_b, project="proj", repo="Azure/mod-b",
                       members=members)


class DigestLastDay(unittest.TestCase):
    def test_last_day_cross_repo_train_stitched_with_date_only_bounds(self):
        conn = _store()
        _seed_last_day_cross_repo(conn)
        expected = [frozenset({"proj/Azure/mod-a#pr-10",
                               "proj/Azure/mod-b#issue-3"})]
        for bounds in (("2026-03-01", "2026-03-31"),
                       ("2026-03-01T00:00:00Z", "2026-03-31T23:59:59Z")):
            comps = digest.spine_components(
                conn, "proj", ["Azure/mod-a", "Azure/mod-b"], *bounds)
            self.assertEqual(comps, expected, bounds)


if __name__ == "__main__":
    unittest.main()
