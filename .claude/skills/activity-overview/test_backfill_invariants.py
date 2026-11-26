"""A backfilled record is indistinguishable, for the store's invariants, from
one a fold wrote (issue #113 M5 / m4).

`spotlight --complete` (and extract with a backfill seam) upserts an
out-of-window PR / issue through `gather.backfill`. It used to write the record
node and the seam's spine edges only, so the trust check rejected the store
(participant_completeness, edge_set, no_drift, idempotency). backfill now
writes the record through `gather.record_rows` — the code fold_bundle uses —
plus the person nodes (derive.enumerate_participants + the fold's union), the
FTS text and the item-relation edges the stored records imply. Every test
here runs `validate` over the backfilled store with a fake fetch seam (no
network).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _failed(report):
    checks = report.checks if hasattr(report, "checks") else [
        c for m in report["members"] for c in m["checks"]] + report["project_checks"]
    return {c["name"]: c["details"] for c in checks if not c["ok"]}


class Fetcher:
    """A fixture-backed fetch seam: local id -> raw record (or ABSENT)."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def __call__(self, kind, local, qid):
        self.calls.append(local)
        rec = self.records.get(local)
        if rec is gather.ABSENT:
            return rec
        return None if rec is None else {"node": copy.deepcopy(rec), "edges": []}


PR10 = {
    "number": 10, "title": "Fix the leak", "body": "Closes #7, see #8",
    "merged": True, "state": "closed", "merged_at": "2026-03-15T00:00:00Z",
    "author": "alice", "merged_by": "bob", "reviewers": ["carol", "dave"],
    "closes": [7], "crossref_issues": [8],
    "url": "https://github.com/acme/widget/pull/10",
    "comments_list": [{"author": "erin", "body": "lgtm"}],
    "review_comments": [{"author": "carol", "body": "nit: rename"}],
    "reviews": [{"id": 99, "author": "carol", "state": "APPROVED",
                 "submitted_at": "2026-03-14T00:00:00Z"}],
    "lifecycle": [{"id": 5, "actor": "bob", "event": "labeled",
                   "created_at": "2026-03-12T00:00:00Z", "label": "bug"}],
}

ISSUE7 = {
    "number": 7, "title": "Original leak report", "state": "closed",
    "state_reason": "completed", "closed_at": "2026-02-01T00:00:00Z",
    "author": "gina", "url": "https://github.com/acme/widget/issues/7",
    "milestone": "v1", "blocks": [{"number": 8, "direction": "out"}],
    "comments_list": [{"author": "alice", "body": "on it"},
                      {"author": "hal", "body": "zanzibar repro"}],
    "lifecycle": [{"id": 6, "actor": "gina", "event": "closed",
                   "created_at": "2026-02-01T00:00:00Z"}],
}

ISSUE8 = {"number": 8, "title": "Blocked thing", "state": "open",
          "updated_at": "2026-03-20T00:00:00Z", "author": "hal",
          "url": "https://github.com/acme/widget/issues/8"}

COMMIT = {"sha": "a" * 40, "author": "alice", "date": "2026-03-15",
          "message": "Fix the leak (#10)", "files": ["src/leak.py"]}


def _window(**over):
    b = {"meta": {"owner": "acme", "repo": "widget",
                  "from": "2026-03-01", "to": "2026-03-31"},
         "prs": [copy.deepcopy(PR10)], "issues": [copy.deepcopy(ISSUE8)],
         "commits": [copy.deepcopy(COMMIT)], "code_events": [],
         "milestones": [{"number": 1, "title": "v1"}], "releases": [],
         "code_graph": {"provider": "directory",
                        "areas": [{"id": "src", "paths": ["src/leak.py"]}]}}
    b.update(over)
    return b


def _qid(local, project="acme", repo="widget"):
    return graphstore.qualify_id(project, repo, local)


def _snapshot(conn):
    nodes = {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(
        "SELECT id, project, repo, node_class, ts, data FROM nodes")}
    edges = {(r[0], r[1], r[2]): (r[3], r[4]) for r in conn.execute(
        "SELECT src_id, dst_id, edge_type, ts, data FROM edges")}
    return nodes, edges


class BackfilledIssuePassesTheGate(unittest.TestCase):
    def test_issue_with_people_milestone_blocks_and_events(self):
        conn = _store()
        gather.fold_bundle(conn, _window())
        self.assertTrue(validate.validate(conn).ok)
        fetch = Fetcher({"issue-7": ISSUE7})
        res = gather.backfill(conn, _qid("issue-7"), fetch=fetch)
        self.assertTrue(res["fetched"])
        self.assertEqual(_failed(validate.validate(conn)), {})
        # the fold's derivations are all there
        edges = {(e[0], e[1], e[2]) for e in conn.execute(
            "SELECT src_id, dst_id, edge_type FROM edges")}
        for key in [
                (graphstore.qualify_person("acme", "gina"), _qid("issue-7"), "reported"),
                (graphstore.qualify_person("acme", "hal"), _qid("issue-7"), "commented"),
                (_qid("issue-7"), _qid("milestone-1"), "in_milestone"),
                (_qid("issue-7"), _qid("issue-8"), "blocks"),
                (_qid("event-issue-7-6"), _qid("issue-7"), "part_of")]:
            self.assertIn(key, edges)
        self.assertIsNotNone(graphstore.get_node(
            conn, graphstore.qualify_person("acme", "gina")))

    def test_backfilled_text_is_searchable(self):
        conn = _store()
        if not graphstore.fts5_available(conn):
            self.skipTest("FTS5 unavailable")
        gather.fold_bundle(conn, _window())
        gather.backfill(conn, _qid("issue-7"), fetch=Fetcher({"issue-7": ISSUE7}))
        self.assertIn(_qid("issue-7"), graphstore.fts_search(conn, '"zanzibar"'))

    def test_second_backfill_is_a_noop(self):
        conn = _store()
        gather.fold_bundle(conn, _window())
        fetch = Fetcher({"issue-7": ISSUE7})
        gather.backfill(conn, _qid("issue-7"), fetch=fetch)
        before = _snapshot(conn)
        res = gather.backfill(conn, _qid("issue-7"), fetch=fetch)
        self.assertFalse(res["fetched"])
        self.assertEqual(_snapshot(conn), before)
        self.assertEqual(fetch.calls, ["issue-7"])


class BackfilledPrEqualsFoldedPr(unittest.TestCase):
    """The strongest form of 'indistinguishable': a store whose PR #10 was
    backfilled holds exactly the rows of a store whose PR #10 was folded."""

    def _pair(self):
        folded = _store()
        gather.fold_bundle(folded, _window())
        back = _store()
        gather.fold_bundle(back, _window(prs=[]))
        res = gather.backfill(back, _qid("pr-10"), fetch=Fetcher({"pr-10": PR10}))
        self.assertTrue(res["fetched"])
        return folded, back

    def test_same_nodes_and_edges_as_the_fold(self):
        folded, back = self._pair()
        fn, fe = _snapshot(folded)
        bn, be = _snapshot(back)
        self.assertEqual(sorted(fe), sorted(be))
        self.assertEqual(set(fn), set(bn))
        for nid in fn:
            self.assertEqual(fn[nid], bn[nid], nid)

    def test_reviewer_inherits_the_mapped_commit_areas(self):
        _folded, back = self._pair()
        carol = graphstore.get_node(back, graphstore.qualify_person("acme", "carol"))
        self.assertEqual(carol["data"]["areas"], ["src"])

    def test_gate_is_green(self):
        _folded, back = self._pair()
        self.assertEqual(_failed(validate.validate(back)), {})


class BackfillKeepsPeopleUnion(unittest.TestCase):
    def test_existing_person_attribution_is_not_erased(self):
        conn = _store()
        gather.fold_bundle(conn, _window())
        alice = graphstore.qualify_person("acme", "alice")
        before = graphstore.get_node(conn, alice)["data"]
        self.assertEqual(before["areas"], ["src"])
        gather.backfill(conn, _qid("issue-7"), fetch=Fetcher({"issue-7": ISSUE7}))
        self.assertEqual(graphstore.get_node(conn, alice)["data"], before)
        self.assertEqual(_failed(validate.validate(conn)), {})


class BackfilledIssueThatIsAPr(unittest.TestCase):
    """`Closes #N` where #N is a PR: the seam returns the PR's record under the
    referenced `issue-N` id. Stored as an issue (that is how extract and the
    gate read an `issue-*` id), so only what an issue record implies is
    written — not the seam's `closes` edges, which no re-derivation implies."""

    def test_gate_is_green(self):
        conn = _store()
        gather.fold_bundle(conn, _window(prs=[dict(PR10, closes=[12])]))
        pr12 = {"number": 12, "title": "Earlier PR", "merged": True,
                "state": "closed", "merged_at": "2026-01-10T00:00:00Z",
                "author": "ivan", "closes": [3],
                "url": "https://github.com/acme/widget/pull/12"}
        res = gather.backfill(conn, _qid("issue-12"),
                              fetch=Fetcher({"issue-12": pr12}))
        self.assertTrue(res["fetched"])
        self.assertEqual(_failed(validate.validate(conn)), {})
        self.assertEqual(graphstore.get_edges(
            conn, _qid("issue-12"), direction="out", edge_types=["closes"]), [])


class BackfillInAProjectStore(unittest.TestCase):
    def test_member_backfill_keeps_the_project_green(self):
        conn = _store()
        members = {"acme/widget", "acme/gadget"}
        gather.fold_bundle(conn, _window(), project="P", repo="acme/widget",
                           members=members)
        gather.fold_bundle(
            conn, {"meta": {"owner": "acme", "repo": "gadget",
                            "from": "2026-03-01", "to": "2026-03-31"},
                   "prs": [], "issues": [dict(ISSUE8, number=4,
                                              url="https://github.com/acme/gadget/issues/4")],
                   "commits": [], "code_events": [], "milestones": [],
                   "releases": []},
            project="P", repo="acme/gadget", members=members)
        issue7 = dict(ISSUE7, body="see acme/gadget#4")
        res = gather.backfill(conn, _qid("issue-7", "P", "acme/widget"),
                              fetch=Fetcher({"issue-7": issue7}))
        self.assertTrue(res["fetched"])
        node = graphstore.get_node(conn, _qid("issue-7", "P", "acme/widget"))
        self.assertEqual((node["project"], node["repo"]), ("P", "acme/widget"))
        pr13 = {"number": 13, "title": "Fix it", "body": "Fixes acme/gadget#4",
                "merged": True, "state": "closed", "author": "ivan",
                "merged_at": "2026-01-10T00:00:00Z",
                "url": "https://github.com/acme/widget/pull/13"}
        res = gather.backfill(conn, _qid("pr-13", "P", "acme/widget"),
                              fetch=Fetcher({"pr-13": pr13}))
        self.assertTrue(res["fetched"])
        self.assertEqual(
            [e["dst_id"] for e in graphstore.get_edges(
                conn, _qid("pr-13", "P", "acme/widget"), direction="out",
                edge_types=["closes"])],
            [_qid("issue-4", "P", "acme/gadget")])   # the cross-repo edge
        report = validate.validate_project(conn, "P", ["acme/gadget", "acme/widget"])
        self.assertEqual(_failed(report), {})


class BackfillInAMultiWindowStore(unittest.TestCase):
    def test_gate_stays_green_in_both_fold_orders(self):
        feb = _window(prs=[], issues=[], commits=[], milestones=[],
                      meta={"owner": "acme", "repo": "widget",
                            "from": "2026-02-01", "to": "2026-02-28"},
                      code_graph={"provider": "directory", "areas": [
                          {"id": "src", "paths": ["src/leak.py"]}]})
        for order in ((_window(), feb), (feb, _window())):
            conn = _store()
            for b in order:
                gather.fold_bundle(conn, copy.deepcopy(b))
            self.assertEqual(_failed(validate.validate(conn)), {})
            for local, rec in (("issue-7", ISSUE7),
                               ("pr-12", dict(PR10, number=12, closes=[],
                                              crossref_issues=[],
                                              url="https://github.com/acme/widget/pull/12"))):
                res = gather.backfill(conn, _qid(local), fetch=Fetcher({local: rec}))
                self.assertTrue(res["fetched"], local)
            self.assertEqual(_failed(validate.validate(conn)), {})


class BackfillScope(unittest.TestCase):
    def test_only_pr_and_issue_ids_are_fetched(self):
        # A spine traversal can only miss a PR or an issue (the dst of closes /
        # cross_ref / part_of). A commit, an artifact, a review or a structure
        # node written from a seam alone would be a row the gate rejects, so
        # backfill does not fetch one (#113 m4: commit backfill removed).
        conn = _store()
        fetch = Fetcher({})
        for local in ("a" * 40, "art:src/x.py", "review-10-99", "milestone-1",
                      "comment-5"):
            res = gather.backfill(conn, _qid(local), fetch=fetch)
            self.assertFalse(res["fetched"], local)
            self.assertIsNone(graphstore.get_node(conn, _qid(local)), local)
        self.assertEqual(fetch.calls, [])

    def test_record_number_must_match_the_id(self):
        conn = _store()
        res = gather.backfill(conn, _qid("issue-7"),
                              fetch=Fetcher({"issue-7": dict(ISSUE7, number=9)}))
        self.assertFalse(res["fetched"])
        self.assertIsNone(graphstore.get_node(conn, _qid("issue-7")))

    def test_record_without_number_takes_the_ids(self):
        conn = _store()
        rec = {k: v for k, v in ISSUE7.items() if k != "number"}
        res = gather.backfill(conn, _qid("issue-7"), fetch=Fetcher({"issue-7": rec}))
        self.assertTrue(res["fetched"])
        self.assertEqual(graphstore.get_node(conn, _qid("issue-7"))["data"]["number"], 7)

    def test_production_fetcher_takes_no_clone_dir(self):
        import inspect
        self.assertEqual(list(inspect.signature(
            gather.make_backfill_fetcher).parameters), ["token"])


class RecordRowsIsTheFoldsDerivation(unittest.TestCase):
    def test_fold_writes_exactly_record_rows_for_each_record(self):
        conn = _store()
        b = _window(issues=[copy.deepcopy(ISSUE8), copy.deepcopy(ISSUE7)])
        gather.fold_bundle(conn, copy.deepcopy(b))
        stored = {(e[0], e[1], e[2]) for e in conn.execute(
            "SELECT src_id, dst_id, edge_type FROM edges")}
        ids = {r[0] for r in conn.execute("SELECT id FROM nodes")}
        for kind, recs in (("pr", b["prs"]), ("issue", b["issues"])):
            for rec in recs:
                rows = gather.record_rows(kind, rec, "acme", "widget", None)
                self.assertTrue({(e[0], e[1], e[2]) for e in rows["edges"]} <= stored)
                self.assertTrue({n[0] for n in rows["nodes"]} <= ids)

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            gather.record_rows("commit", {"number": 1}, "o", "r", None)


if __name__ == "__main__":
    unittest.main()
