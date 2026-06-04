"""Tests for slice 7c — backfill on a spine-traversal miss + roll-up/resume.

`gather.backfill(conn, id, fetch=...)` is the only network call outside the main
Acquire pass: on a traversal MISS (a spine edge pointing at a node never
gathered — e.g. a windowed PR `closes` an out-of-window issue) a reader asks
gather to fetch THAT ONE node + its cheap immediate edges and upsert it. It is
idempotent (no-op if already present) and budget-bounded by the caller.

The network is SEAMED via an injectable `fetch` callable: production wires
gather's real REST/git fetchers; these tests pass a fixture-backed fake so the
suite makes NO network call. extract wires backfill via an injected `backfill`
parameter (default None == today's warn-only behavior).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


# A windowed PR (#10, merged in-window) that `closes` issue #7 which was opened
# (and closed) BEFORE the window — so the issue is never gathered in this
# window's Acquire pass and shows up as a `missing` spine node when extract
# traverses PR #10's train.
def _cross_window_bundle():
    return {
        "meta": {"owner": "acme", "repo": "widget",
                 "from": "2026-03-01", "to": "2026-03-31"},
        "prs": [{
            "number": 10, "title": "Fix the leak", "merged": True,
            "merged_at": "2026-03-15T00:00:00Z", "author": "alice",
            "closes": [7], "url": "https://github.com/acme/widget/pull/10",
        }],
        "issues": [],          # issue #7 is OUT of window — not gathered here
        "commits": [],
        "code_events": [],
        "milestones": [], "releases": [],
    }


# A fixture-backed fake fetcher. Records each id it was asked to fetch (so a test
# can assert the call count / budget). `social_payloads` maps a social local id
# -> the raw record the REST API would have returned; an absent id yields None
# (genuinely unfetchable). NO network.
class FakeFetcher:
    def __init__(self, social_payloads=None):
        self.social_payloads = social_payloads or {}
        self.calls = []

    def __call__(self, kind, local, qid):
        self.calls.append(local)
        if kind == "social" and local in self.social_payloads:
            return self.social_payloads[local]
        return None


def _issue7_payload():
    # The shape gather.backfill expects for a social node: a node `data` dict
    # plus its cheap immediate spine edges (issue #7 was spun off from #5, say).
    return {
        "node": {"number": 7, "title": "Original leak report", "state": "closed",
                 "closed_at": "2026-02-01T00:00:00Z", "author": "bob",
                 "url": "https://github.com/acme/widget/issues/7"},
        "edges": [],
    }


class BackfillIdempotency(unittest.TestCase):
    def test_present_id_is_noop_without_fetching(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        # PR #10 is present; backfilling it must NOT fetch.
        fake = FakeFetcher()
        pid = graphstore.qualify_id("acme", "widget", "pr-10")
        res = gather.backfill(conn, pid, fetch=fake)
        self.assertFalse(res["fetched"])
        self.assertEqual(fake.calls, [])

    def test_fetches_missing_then_idempotent_on_second_call(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        iid = graphstore.qualify_id("acme", "widget", "issue-7")
        self.assertIsNone(graphstore.get_node(conn, iid))  # missing
        fake = FakeFetcher({"issue-7": _issue7_payload()})

        res1 = gather.backfill(conn, iid, fetch=fake)
        self.assertTrue(res1["fetched"])
        self.assertEqual(res1["id"], iid)
        node = graphstore.get_node(conn, iid)
        self.assertIsNotNone(node)
        self.assertEqual(node["data"]["number"], 7)
        self.assertEqual(node["node_class"], "social")

        # Second call: present now -> no-op, no further fetch.
        res2 = gather.backfill(conn, iid, fetch=fake)
        self.assertFalse(res2["fetched"])
        self.assertEqual(fake.calls, ["issue-7"])  # exactly one fetch ever


class ExtractBackfillWiring(unittest.TestCase):
    def test_missing_spine_node_backfilled_as_out_of_window_context(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        fake = FakeFetcher({"issue-7": _issue7_payload()})
        backfill = lambda c, mid: gather.backfill(c, mid, fetch=fake)  # noqa: E731

        b = extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31",
                            backfill=backfill)
        # The backfilled issue is now in the bundle (out-of-window context),
        # reachable as part of PR #10's train.
        issue_nums = {i["number"] for i in b["issues"]}
        self.assertIn(7, issue_nums)
        # Exactly one fetch happened.
        self.assertEqual(fake.calls, ["issue-7"])

    def test_backfilled_node_does_not_count_as_in_window_activity(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        fake = FakeFetcher({"issue-7": _issue7_payload()})
        backfill = lambda c, mid: gather.backfill(c, mid, fetch=fake)  # noqa: E731
        extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31",
                        backfill=backfill)
        # issue #7 closed 2026-02-01, OUTSIDE the window: the in-window range
        # query must still NOT include it (it is context, not activity).
        in_window = graphstore.range_query(
            conn, "acme", ["widget"], "2026-03-01", "2026-03-31")
        iid = graphstore.qualify_id("acme", "widget", "issue-7")
        self.assertNotIn(iid, {n["id"] for n in in_window})

    def test_one_complete_cross_window_train_after_backfill(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        fake = FakeFetcher({"issue-7": _issue7_payload()})
        backfill = lambda c, mid: gather.backfill(c, mid, fetch=fake)  # noqa: E731
        extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31",
                        backfill=backfill)
        # After backfill the spine from PR #10 reaches issue #7 with NO missing.
        pid = graphstore.qualify_id("acme", "widget", "pr-10")
        iid = graphstore.qualify_id("acme", "widget", "issue-7")
        spine = graphstore.traverse_spine(conn, [pid])
        self.assertIn(iid, spine["reached"])      # one complete train
        self.assertEqual(spine["missing"], [])    # nothing left dangling

    def test_budget_ceiling_stops_further_fetches(self):
        conn = _store()
        # Three PRs, each closing a distinct out-of-window issue => 3 missing.
        bundle = _cross_window_bundle()
        bundle["prs"] = [
            {"number": 10, "merged": True, "merged_at": "2026-03-15T00:00:00Z",
             "closes": [7], "title": "a", "url": "u10"},
            {"number": 11, "merged": True, "merged_at": "2026-03-16T00:00:00Z",
             "closes": [8], "title": "b", "url": "u11"},
            {"number": 12, "merged": True, "merged_at": "2026-03-17T00:00:00Z",
             "closes": [9], "title": "c", "url": "u12"},
        ]
        gather.fold_bundle(conn, bundle)
        payloads = {"issue-7": _issue7_payload(),
                    "issue-8": _issue7_payload(),
                    "issue-9": _issue7_payload()}
        fake = FakeFetcher(payloads)
        warnings = []
        backfill = lambda c, mid: gather.backfill(c, mid, fetch=fake)  # noqa: E731
        extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31",
                        backfill=backfill, backfill_budget=2,
                        warn=warnings.append)
        # No more than the budget were fetched.
        self.assertEqual(len(fake.calls), 2)
        # A budget-exhausted warning was emitted.
        self.assertTrue(any("budget" in w.lower() for w in warnings))

    def test_default_backfill_none_is_warn_only_unchanged(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        warnings = []
        b = extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31",
                            warn=warnings.append)  # backfill defaults to None
        # No backfill: issue #7 stays missing, only a warning.
        self.assertNotIn(7, {i["number"] for i in b["issues"]})
        self.assertTrue(any("missing" not in w or "spine" in w for w in warnings))
        self.assertTrue(warnings, "warn-only path must still warn")

    def test_determinism_same_inputs_same_view(self):
        b1conn = _store()
        b2conn = _store()
        for c in (b1conn, b2conn):
            gather.fold_bundle(c, copy.deepcopy(_cross_window_bundle()))
        f1 = FakeFetcher({"issue-7": _issue7_payload()})
        f2 = FakeFetcher({"issue-7": _issue7_payload()})
        v1 = extract.extract(b1conn, "acme", "widget", "2026-03-01", "2026-03-31",
                             backfill=lambda c, m: gather.backfill(c, m, fetch=f1))
        v2 = extract.extract(b2conn, "acme", "widget", "2026-03-01", "2026-03-31",
                             backfill=lambda c, m: gather.backfill(c, m, fetch=f2))
        self.assertEqual(v1, v2)


class RollupResumeHardening(unittest.TestCase):
    """A multi-window "wider view" is a single wider range_query over the union
    of folded windows (no multi-bundle merge); overlapping re-fold is idempotent;
    resume/roll-up read structure from the latest clone_sha + gathered_windows."""

    def _two_window_bundles(self):
        march = {
            "meta": {"owner": "acme", "repo": "widget",
                     "from": "2026-03-01", "to": "2026-03-31",
                     "clone_sha": "sha-march"},
            "prs": [{"number": 10, "merged": True,
                     "merged_at": "2026-03-15T00:00:00Z", "title": "march",
                     "url": "u10", "closes": []}],
            "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
        }
        april = {
            "meta": {"owner": "acme", "repo": "widget",
                     "from": "2026-04-01", "to": "2026-04-30",
                     "clone_sha": "sha-april"},
            "prs": [{"number": 20, "merged": True,
                     "merged_at": "2026-04-15T00:00:00Z", "title": "april",
                     "url": "u20", "closes": []}],
            "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
        }
        return march, april

    def test_wider_window_is_union_range_query(self):
        conn = _store()
        march, april = self._two_window_bundles()
        gather.fold_bundle(conn, copy.deepcopy(march))
        gather.fold_bundle(conn, copy.deepcopy(april))
        pid10 = graphstore.qualify_id("acme", "widget", "pr-10")
        pid20 = graphstore.qualify_id("acme", "widget", "pr-20")

        # The "wider view" is a SINGLE wider range_query over the union of folded
        # windows — NOT a multi-bundle merge. One query [march-start, april-end]
        # returns the union of in-window activity.
        wide = graphstore.range_query(
            conn, "acme", ["widget"], "2026-03-01", "2026-04-30")
        self.assertEqual({n["id"] for n in wide}, {pid10, pid20})

        # Each narrow window's range_query sees only its own in-window PR; the
        # wider view is exactly their union (a WHERE over ts, not a merge step).
        m = graphstore.range_query(
            conn, "acme", ["widget"], "2026-03-01", "2026-03-31")
        a = graphstore.range_query(
            conn, "acme", ["widget"], "2026-04-01", "2026-04-30")
        self.assertEqual({n["id"] for n in m}, {pid10})
        self.assertEqual({n["id"] for n in a}, {pid20})
        self.assertEqual({n["id"] for n in wide},
                         {n["id"] for n in m} | {n["id"] for n in a})

        # And the wider extract bundle materializes both PRs (full repo arrays).
        wide_bundle = extract.extract(
            conn, "acme", "widget", "2026-03-01", "2026-04-30")
        self.assertEqual({p["number"] for p in wide_bundle["prs"]}, {10, 20})

    def test_two_windows_recorded_and_latest_clone_sha(self):
        conn = _store()
        march, april = self._two_window_bundles()
        gather.fold_bundle(conn, copy.deepcopy(march))
        gather.fold_bundle(conn, copy.deepcopy(april))
        windows = graphstore.get_windows(conn)
        self.assertIn({"project": "acme", "repo": "widget",
                       "from": "2026-03-01", "to": "2026-03-31"}, windows)
        self.assertIn({"project": "acme", "repo": "widget",
                       "from": "2026-04-01", "to": "2026-04-30"}, windows)
        # Latest fold's clone_sha is the recorded structure pin.
        self.assertEqual(graphstore.get_clone_sha(conn, "acme", "widget"),
                         "sha-april")

    def test_overlapping_refold_is_idempotent_at_extract(self):
        conn = _store()
        march, _ = self._two_window_bundles()
        gather.fold_bundle(conn, copy.deepcopy(march))
        v1 = extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31")
        w1 = graphstore.get_windows(conn)
        # Re-fold the SAME (overlapping) window: must not duplicate anything.
        gather.fold_bundle(conn, copy.deepcopy(march))
        v2 = extract.extract(conn, "acme", "widget", "2026-03-01", "2026-03-31")
        w2 = graphstore.get_windows(conn)
        self.assertEqual(v1, v2)
        self.assertEqual(w1, w2)


if __name__ == "__main__":
    unittest.main()


class BackfillAbsent(unittest.TestCase):
    def test_absent_records_dead_ref_and_reports_absent(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        iid = graphstore.qualify_id("acme", "widget", "issue-123")  # never existed

        def fetch(kind, local, qid):
            return gather.ABSENT

        res = gather.backfill(conn, iid, fetch=fetch)
        self.assertFalse(res["fetched"])
        self.assertTrue(res["absent"])
        self.assertTrue(graphstore.is_dead_ref(conn, iid))  # remembered dead
        self.assertIsNone(graphstore.get_node(conn, iid))    # not upserted

    def test_unreachable_none_is_not_absent(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        iid = graphstore.qualify_id("acme", "widget", "issue-7")

        def fetch(kind, local, qid):
            return None  # transient / unreachable, NOT a 404

        res = gather.backfill(conn, iid, fetch=fetch)
        self.assertFalse(res["fetched"])
        self.assertFalse(res["absent"])
        self.assertFalse(graphstore.is_dead_ref(conn, iid))  # NOT tombstoned
