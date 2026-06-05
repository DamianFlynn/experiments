import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import complete  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _pr_closes_issue(conn, pr_local="pr-10", issue_local="issue-7",
                     pr_ts="2026-03-15T00:00:00Z"):
    """Seed an in-window PR that `closes` an absent (missing) issue. Returns
    (pr_id, issue_id)."""
    pr = graphstore.qualify_id("acme", "widget", pr_local)
    issue = graphstore.qualify_id("acme", "widget", issue_local)
    graphstore.upsert_node(conn, pr, "acme", "widget", "social", pr_ts,
                           {"number": int(pr_local.split("-")[1])})
    graphstore.upsert_edge(conn, pr, issue, "closes")
    return pr, issue


class CompleteOffline(unittest.TestCase):
    def test_no_fetcher_reports_missing_as_not_gathered(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        self.assertEqual(res["fetched"], 0)
        self.assertEqual(res["gaps"], [{"id": issue, "reason": "not_gathered"}])
        self.assertIn(pr, res["reached"])

    def test_dead_ref_is_pruned_not_reported(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)
        graphstore.record_dead_ref(conn, issue)  # known phantom
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        self.assertEqual(res["gaps"], [])          # phantom NOT a gap
        self.assertEqual(res["fetched"], 0)

    def test_gaps_sorted_by_id(self):
        conn = _store()
        _pr_closes_issue(conn, "pr-10", "issue-9")
        _pr_closes_issue(conn, "pr-11", "issue-1")
        seeds = [graphstore.qualify_id("acme", "widget", x)
                 for x in ("pr-10", "pr-11")]
        reach = graphstore.traverse_spine(conn, seeds)
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        ids = [g["id"] for g in res["gaps"]]
        self.assertEqual(ids, sorted(ids))

    def test_offline_with_window_is_not_gathered_never_outside_window(self):
        # Offline (no fetcher) must NOT emit window-derived reasons: we never
        # looked, so an out-of-window-referrer ref is still `not_gathered`.
        conn = _store()
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        i7 = graphstore.qualify_id("acme", "widget", "issue-7")   # present, OOW
        i5 = graphstore.qualify_id("acme", "widget", "issue-5")   # missing
        graphstore.upsert_node(conn, pr, "acme", "widget", "social",
                               "2026-06-10T00:00:00Z", {"number": 10})
        graphstore.upsert_node(conn, i7, "acme", "widget", "social",
                               "2026-01-01T00:00:00Z", {"number": 7})
        graphstore.upsert_edge(conn, pr, i7, "closes")
        graphstore.upsert_edge(conn, i7, i5, "spun_off")  # i5's referrer is OOW
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"],
            window=("2026-06-01", "2026-06-30"), backfill=None)
        self.assertEqual(res["gaps"], [{"id": i5, "reason": "not_gathered"}])


class CompleteTransitive(unittest.TestCase):
    def _three_deep(self, conn):
        """In-window PR #10 -> closes issue #7 (absent, OUT of window) ->
        spun_off from RFC #5 (absent). A fetcher provides #7 and #5."""
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        issue = graphstore.qualify_id("acme", "widget", "issue-7")
        rfc = graphstore.qualify_id("acme", "widget", "issue-5")
        graphstore.upsert_node(conn, pr, "acme", "widget", "social",
                               "2026-06-15T00:00:00Z", {"number": 10})
        graphstore.upsert_edge(conn, pr, issue, "closes")

        def backfill(c, mid):
            if mid == issue:
                # issue #7 closed BEFORE the window; it cites RFC #5.
                graphstore.upsert_node(c, issue, "acme", "widget", "social",
                                       "2026-01-01T00:00:00Z", {"number": 7})
                graphstore.upsert_edge(c, issue, rfc, "spun_off")
                return {"fetched": True, "absent": False, "id": mid}
            if mid == rfc:
                graphstore.upsert_node(c, rfc, "acme", "widget", "social",
                                       "2025-06-01T00:00:00Z", {"number": 5})
                return {"fetched": True, "absent": False, "id": mid}
            return {"fetched": False, "absent": False, "id": mid}

        return pr, issue, rfc, backfill

    def test_level0_anchor_filled_regardless_of_its_date(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        window = ("2026-06-01", "2026-06-30")
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=window,
            backfill=backfill)
        self.assertIn(issue, res["reached"])

    def test_transitive_ref_outside_window_is_a_gap_not_chased(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        window = ("2026-06-01", "2026-06-30")
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=window,
            backfill=backfill)
        self.assertNotIn(rfc, res["reached"])
        self.assertIn({"id": rfc, "reason": "outside_window"}, res["gaps"])
        self.assertEqual(res["fetched"], 1)  # only issue #7 was fetched

    def test_no_window_chases_to_closure(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=None,
            backfill=backfill)
        self.assertIn(issue, res["reached"])
        self.assertIn(rfc, res["reached"])
        self.assertEqual(res["gaps"], [])
        self.assertEqual(res["fetched"], 2)


class CompleteGapReasons(unittest.TestCase):
    def test_unreachable_when_fetch_returns_not_fetched(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)

        def backfill(c, mid):
            return {"fetched": False, "absent": False, "id": mid}  # transient

        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill)
        self.assertEqual(res["gaps"], [{"id": issue, "reason": "unreachable"}])
        self.assertEqual(res["fetched"], 1)

    def test_budget_caps_fetches_and_marks_rest_budget(self):
        conn = _store()
        for prl, isl in (("pr-10", "issue-7"), ("pr-11", "issue-8"),
                         ("pr-12", "issue-9")):
            _pr_closes_issue(conn, prl, isl)
        seeds = [graphstore.qualify_id("acme", "widget", x)
                 for x in ("pr-10", "pr-11", "pr-12")]

        def backfill(c, mid):
            graphstore.upsert_node(c, mid, "acme", "widget", "social",
                                   "2026-03-10T00:00:00Z", {"id": mid})
            return {"fetched": True, "absent": False, "id": mid}

        reach = graphstore.traverse_spine(conn, seeds)
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill,
            budget=2)
        self.assertEqual(res["fetched"], 2)               # ceiling respected
        budget_gaps = [g for g in res["gaps"] if g["reason"] == "budget"]
        self.assertEqual(len(budget_gaps), 1)             # the third is a gap


class CompletePhantomMemory(unittest.TestCase):
    def test_absent_pruned_recorded_and_not_refetched(self):
        conn = _store()
        # PR #10 has a `Fixes #123` template placeholder that never existed.
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        phantom = graphstore.qualify_id("acme", "widget", "issue-123")
        graphstore.upsert_node(conn, pr, "acme", "widget", "social",
                               "2026-03-15T00:00:00Z", {"number": 10})
        graphstore.upsert_edge(conn, pr, phantom, "closes")

        calls = []

        def backfill(c, mid):
            calls.append(mid)
            return gather.backfill(c, mid, fetch=lambda k, l, q: gather.ABSENT)

        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill)
        self.assertEqual(res["gaps"], [])
        self.assertTrue(graphstore.is_dead_ref(conn, phantom))
        self.assertEqual(calls, [phantom])  # fetched exactly once

        # A SECOND completion does not re-fetch it (is_dead_ref short-circuits).
        calls.clear()
        reach2 = graphstore.traverse_spine(conn, [pr], skip_dead=True)
        res2 = complete.complete_train(
            conn, reach2["reached"], reach2["missing"], backfill=backfill)
        self.assertEqual(calls, [])         # never re-chased
        self.assertEqual(res2["gaps"], [])


class Annotate(unittest.TestCase):
    def test_complete_true_when_no_gaps(self):
        t = {"anchor": "x"}
        complete.annotate(t, {"reached": {"x"}, "gaps": [], "fetched": 0})
        self.assertEqual(t["complete"], True)
        self.assertEqual(t["gaps"], [])

    def test_complete_false_lists_sorted_gaps(self):
        t = {"anchor": "x"}
        result = {"reached": {"x"},
                  "gaps": [{"id": "b", "reason": "not_gathered"},
                           {"id": "a", "reason": "outside_window"}],
                  "fetched": 0}
        complete.annotate(t, result)
        self.assertEqual(t["complete"], False)
        self.assertEqual([g["id"] for g in t["gaps"]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
