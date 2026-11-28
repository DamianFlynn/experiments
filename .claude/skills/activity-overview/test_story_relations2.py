"""Review findings on the relation layer (PR #123) and the relations between
items of one story. Each test reproduces its finding before the fix."""
import copy
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import validate  # noqa: E402
from test_story import comment, pr  # noqa: E402
from test_story_groups import rows, talk, view  # noqa: E402
from test_story_relations import edges, hist, issue, member  # noqa: E402
from test_story_substance import enriched  # noqa: E402


class TestChainsBounded(unittest.TestCase):

    def test_a_branching_graph_stays_bounded(self):
        # a layered graph with many simple paths
        es = []
        layers = [["s{}_{}".format(i, j) for j in range(6)] for i in range(9)]
        for a, b in zip(layers, layers[1:]):
            for x in a:
                for y in b:
                    es.append({"from": "o/r:" + x, "to": "o/r:" + y, "relation": "depends_on",
                               "basis": {"kind": "text", "quote": "q"}, "as_of": None,
                               "target_kind": "other"})
        t0 = time.time()
        g = link.story_graph(es)
        self.assertLess(time.time() - t0, 2.0)
        self.assertTrue(g["chains_capped"])
        self.assertLessEqual(len(g["chains"]), link.STORY_CHAINS_SHOWN)
        self.assertLessEqual(max(len(c) for c in g["chains"]), link.STORY_CHAIN_MAX_DEPTH)


class TestOneSentence(unittest.TestCase):

    def test_superseded_never_crosses_a_sentence(self):
        p = pr(1, state="closed", closed_at="2026-05-10T00:00:00Z", comments_list=[
            comment("m", "2026-05-10T00:00:00Z", "Merged in October 2026. See #123 for details.",
                    3)])
        self.assertEqual(link.close_reason({}, p, "2026-05-31T23:59:59Z"),
                         {"close_reason": "not stated"})
        self.assertEqual(link.relation_mentions("Blocked by the release.\nSee #2.")[0]
                         ["relation"], "mentions")


class TestAffirmedAndWhose(unittest.TestCase):

    def test_negation_is_no_relation(self):
        b = enriched(view([pr(1, body="This is not blocked by #2.", comments_list=talk(1)),
                           pr(2, comments_list=talk(2))], pr_history=hist()))
        self.assertEqual([e["relation"] for e in edges(b)], ["mentions"])

    def test_the_cues_subject_owns_the_relation(self):
        c = comment("m", "2026-05-20T00:00:00Z", "FYI #2 is blocked by #3.", 7)
        b = enriched(view([pr(1, comments_list=talk(1) + [c]), pr(2, comments_list=talk(2)),
                           pr(3, comments_list=talk(3))], pr_history=hist()))
        got = {(e["from"], e["to"], e["relation"]) for e in edges(b)}
        self.assertIn(("o/r:train-pr-2", "o/r:train-pr-3", "blocked_by"), got)
        self.assertNotIn(("o/r:train-pr-1", "o/r:train-pr-3", "blocked_by"), got)
        e = next(e for e in edges(b) if e["relation"] == "blocked_by")
        self.assertEqual((e["basis"]["item"]["number"], e["basis"]["subject"]["number"]),
                         (1, 2))


class TestActivePairWins(unittest.TestCase):

    def test_an_open_pr_of_the_train_still_waits(self):
        i = issue(9)
        b = enriched(view([pr(1, closes=[9], body="Blocked by #3", state="closed",
                              closed_at="2026-05-05T00:00:00Z", created_at="2026-05-02T00:00:00Z"),
                           pr(2, closes=[9], body="Blocked by #3", comments_list=talk(2),
                              created_at="2026-05-10T00:00:00Z"),
                           pr(3, comments_list=talk(3))], [i], pr_history=hist()))
        e = next(e for e in edges(b) if e["relation"] == "blocked_by")
        self.assertTrue(e["waiting"])
        self.assertEqual(e["basis"]["item"]["number"], 2)
        self.assertEqual(len(e["items"]), 2)
        self.assertIn("waits on PR #3", rows(b)["train-issue-9"]["status_reason"])


class TestInnerRelations(unittest.TestCase):

    def test_items_of_one_story(self):
        i = issue(9)
        c = comment("r", "2026-05-20T00:00:00Z", "Opened #8, which is blocking #9.", 5)
        b = enriched(view([pr(1, closes=[9, 8], comments_list=talk(1) + [c])],
                          [i, issue(8)], pr_history=hist()))
        inner = rows(b)["train-issue-8"]["inner_relations"] if "train-issue-8" in rows(b) \
            else rows(b)["train-issue-9"]["inner_relations"]
        rel = next(r for r in inner if r["relation"] == "blocked_by")
        self.assertEqual((rel["from_item"]["number"], rel["to_item"]["number"],
                          rel["author"], rel["waiting"]), (9, 8, "r", True))
        self.assertEqual(rel["quote"], "Opened #8, which is blocking #9.")

    def test_task_done_and_text_current(self):
        b = enriched(view([pr(1, body="- [x] #2 ready", comments_list=talk(1),
                              body_as_of={"state": "as last gathered", "why": "edited"}),
                           pr(2, comments_list=talk(2))], pr_history=hist()))
        e = edges(b)[0]
        self.assertEqual((e["relation"], e["basis"]["task_done"], e["basis"]["text_current"],
                          e["waiting"]), ("depends_on", True, False, False))


class TestAcrossMembers(unittest.TestCase):

    def members(self, order=("a", "b")):
        a = member("o/a", [pr(1, body="This blocks o/b#5.", url="https://github.com/o/a/pull/1",
                              comments_list=talk(1))])
        bb = member("o/b", [pr(5, body="Blocked by o/a#1", url="https://github.com/o/b/pull/5",
                               comments_list=talk(5)),
                            pr(6, body="Depends on o/a#1", state="closed",
                               closed_at="2026-05-10T00:00:00Z",
                               url="https://github.com/o/b/pull/6", comments_list=talk(6))])
        ms = {"a": {"repo": "o/a", "bundle": a}, "b": {"repo": "o/b", "bundle": bb}}
        return [ms[k] for k in order]

    def test_inverse_cues_become_the_waiting_sides_edge_in_both_orders(self):
        results = []
        for order in (("a", "b"), ("b", "a")):
            ms = link.join_member_relations(self.members(order))
            by = {m["repo"]: m["bundle"] for m in ms}
            b_edges = {(e["from"], e["to"], e["relation"]): e for e in edges(by["o/b"])}
            e = b_edges[("o/b:train-pr-5", "o/a:train-pr-1", "blocked_by")]
            self.assertTrue(e["waiting"])
            # a closed member item waits on nothing
            e6 = b_edges[("o/b:train-pr-6", "o/a:train-pr-1", "depends_on")]
            self.assertFalse(e6["waiting"])
            a_rows = rows(by["o/a"])["train-pr-1"]["relations"]
            self.assertTrue(any(r["relation"] == "blocks" and r["other"] == "o/b:train-pr-5"
                                for r in a_rows))
            self.assertTrue(any(r["relation"] == "needed_by" and r["other"] == "o/b:train-pr-6"
                                for r in a_rows))
            b_rows = rows(by["o/b"])["train-pr-5"]["relations"]
            self.assertTrue(any(r["relation"] == "blocked_by" for r in b_rows))
            self.assertIsNone(validate.story_graph_problem(by["o/a"]))
            self.assertIsNone(validate.story_graph_problem(by["o/b"]))
            results.append(json.dumps({k: (v["story_graph"]["edges"],
                                           rows(v)["train-pr-1" if k == "o/a" else "train-pr-5"])
                                       for k, v in by.items()}, sort_keys=True))
        self.assertEqual(results[0], results[1])

    def test_idempotent(self):
        ms = self.members()
        link.join_member_relations(ms)
        once = copy.deepcopy([m["bundle"]["story_graph"] for m in ms] +
                             [rows(m["bundle"]) for m in ms])
        link.join_member_relations(ms)
        twice = [m["bundle"]["story_graph"] for m in ms] + [rows(m["bundle"]) for m in ms]
        self.assertEqual(json.dumps(once, sort_keys=True), json.dumps(twice, sort_keys=True))
        self.assertEqual(rows(ms[1]["bundle"])["train-pr-5"]["status_reason"].count("waits on"), 1)


if __name__ == "__main__":
    unittest.main()
