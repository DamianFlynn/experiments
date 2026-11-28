"""The shared relation API, round three (PR #123): the gaps the claim checker
found once it used the API only — stacked cues and generic waiting, explicit
and compound subjects, one lifecycle replay, a public reference resolver, one
supersession answer — and the sentence shapes the parser must read."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_story import comment, pr  # noqa: E402
from test_story_groups import talk, view  # noqa: E402
from test_story_relations import edges, hist  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

R = "https://github.com/o/r"


def claims(text, default=None):
    return link.parse_relation_claims(text, "o/r", default)


def flat(cs):
    """[(subject number or None, relation, target number, negated)]"""
    return [((c["subject"] or {}).get("number"), c["relation"], t["number"], c["negated"])
            for c in cs for t in c["targets"]]


class TestStackedAndGenericWaiting(unittest.TestCase):

    def test_written_stacked_cues(self):
        for text in ("This is stacked on #5.", "Stacked on top of #5.",
                     "This is built on top of #5.", "It is based on #5's branch.",
                     "Based on the branch of #5."):
            self.assertEqual(flat(claims(text)), [(None, "stacked_on", 5, False)], text)
        # "based on" without a branch is no stacking
        self.assertEqual(flat(claims("Based on #5, we chose a new name.")),
                         [(None, "mentions", 5, False)])
        # "built on" alone stays a dependency
        self.assertEqual(flat(claims("Built on #5.")), [(None, "depends_on", 5, False)])
        b = enriched(view([pr(1, body="This is stacked on #2.", comments_list=talk(1)),
                           pr(2, comments_list=talk(2))], pr_history=hist()))
        e = edges(b)[0]
        self.assertEqual((e["relation"], e["to"], e["waiting"]),
                         ("stacked_on", "o/r:train-pr-2", True))

    def test_waits_on_is_a_generic_waiting_query(self):
        c = claims("#7 still waits on #5.")[0]
        self.assertEqual((c["relation"], c["query_kind"]), ("blocked_by", "waits_on"))
        self.assertEqual(claims("#7 is blocked by #5.")[0]["query_kind"], "blocked_by")
        # the record links them by shared commits only: stacked_on
        shared = commit("a" * 40, "2026-05-03T00:00:00Z", "fix(ci): x", [".github/w.yml"])
        h = hist({"number": 5, "status": "fetched", "commits": [dict(shared)]},
                 {"number": 7, "status": "fetched", "commits": [dict(shared), commit(
                     "b" * 40, "2026-05-20T00:00:00Z", "more", ["avm/res/a/b/main.bicep"])]})
        b = enriched(view([pr(5, created_at="2026-05-02T00:00:00Z", comments_list=talk(5)),
                           pr(7, created_at="2026-05-19T00:00:00Z", comments_list=talk(7))],
                          pr_history=h))
        self.assertEqual([e["relation"] for e in edges(b)], ["stacked_on"])
        self.assertFalse(link.relation_holds(b, 7, 5, "blocked_by")["holds"])
        r = link.relation_holds(b, 7, 5, "waits_on", present=True)
        self.assertTrue(r["holds"])
        self.assertEqual(r["evidence"][0]["relation"], "stacked_on")
        self.assertTrue(link.relation_holds(b, 5, 7, "waited_on_by")["holds"])
        self.assertFalse(link.relation_holds(b, 5, 7, "waits_on")["holds"])
        self.assertEqual(set(link.GENERIC_RELATIONS["waits_on"]),
                         {"blocked_by", "depends_on", "stacked_on"})


class TestSubjects(unittest.TestCase):

    def test_a_negated_claim_keeps_its_explicit_subject(self):
        for text in ("#2 is not blocked by #3.", "#2 is no longer blocked by #3.",
                     "#2 does not depend on #3."):
            cs = claims(text, {"slug": None, "number": 9})
            self.assertEqual([(x[0], x[3]) for x in flat(cs)], [(2, True)], text)

    def test_compound_subjects_each_get_a_pair(self):
        cs = claims("#30 and #31 depended on #12.")
        self.assertEqual(flat(cs), [(30, "depends_on", 12, False), (31, "depends_on", 12, False)])
        self.assertEqual([s["number"] for s in cs[0]["subjects"]], [30, 31])
        cs = claims("#30, #31 and #32 are blocked by #12 and #13.")
        self.assertEqual(sorted({(x[0], x[2]) for x in flat(cs)}),
                         [(30, 12), (30, 13), (31, 12), (31, 13), (32, 12), (32, 13)])
        b = enriched(view([pr(1, body="#2 and #3 are blocked by #4.", comments_list=talk(1)),
                           pr(2, comments_list=talk(2)), pr(3, comments_list=talk(3)),
                           pr(4, comments_list=talk(4))], pr_history=hist()))
        got = {(e["from"], e["to"]) for e in edges(b, "blocked_by")}
        self.assertEqual(got, {("o/r:train-pr-2", "o/r:train-pr-4"),
                               ("o/r:train-pr-3", "o/r:train-pr-4")})


class TestLifecycleReplay(unittest.TestCase):

    def test_reopen_events_are_replayed(self):
        rec = {"state": "open", "created_at": "2026-05-01T00:00:00Z",
               "lifecycle": [{"event": "closed", "created_at": "2026-05-05T00:00:00Z"},
                             {"event": "reopened", "created_at": "2026-05-10T00:00:00Z"}]}
        self.assertEqual([link.item_state(rec, d) for d in
                          ("2026-04-30", "2026-05-04", "2026-05-06", "2026-05-12")],
                         ["not_created", "open", "closed", "open"])
        rec2 = dict(rec, state="closed", closed_at="2026-05-20T00:00:00Z")
        self.assertEqual(link.item_state(rec2, "2026-05-12"), "open")
        self.assertEqual(link.item_state(rec2, "2026-05-21"), "closed")
        merged = {"state": "closed", "merged": True, "created_at": "2026-05-01T00:00:00Z",
                  "merged_at": "2026-05-15T00:00:00Z", "closed_at": "2026-05-15T00:00:00Z",
                  "lifecycle": [{"event": "merged", "created_at": "2026-05-15T00:00:00Z"},
                                {"event": "closed", "created_at": "2026-05-15T00:00:01Z"}]}
        self.assertEqual(link.item_state(merged, "2026-05-20"), "merged")
        # an item reopened by the day is an open end of a relation
        b = enriched(view([pr(1, body="Blocked by #9", comments_list=talk(1)),
                           pr(9, comments_list=talk(9), lifecycle=[
                               {"event": "closed", "created_at": "2026-05-05T00:00:00Z"},
                               {"event": "reopened", "created_at": "2026-05-08T00:00:00Z"}])],
                          pr_history=hist()))
        self.assertEqual(link.relation_holds(b, 1, 9, "blocked_by", day="2026-05-06",
                                             present=True)["reason"], "target_not_open")
        self.assertTrue(link.relation_holds(b, 1, 9, "blocked_by", day="2026-05-09",
                                            present=True)["holds"])


class TestReferenceResolver(unittest.TestCase):

    def test_item_refs(self):
        text = ("Blocked by [#5](https://github.com/o/b/pull/5), o/c#6 and #7 "
                "([comment](https://github.com/o/r/pull/7#issuecomment-1)); see "
                "https://github.com/o/r/commit/abcdef1 and https://example.com/o/r/pull/8. "
                "`#9` is code.\n> quoted #10\n<!-- #11 -->")
        got = [(r["repo"], r["number"], r["evidence"], r["form"]) for r in link.item_refs(text, "o/r")]
        self.assertEqual(got, [("o/b", 5, False, "markdown"), ("o/c", 6, False, "short"),
                               ("o/r", 7, False, "local"), ("o/r", 7, True, "markdown"),
                               ("o/r", None, True, "url")])
        refs = link.item_refs(text, "o/r")
        a, b = refs[0]["span"]
        self.assertEqual(text[a:b], "[#5](https://github.com/o/b/pull/5)")
        self.assertEqual(refs[4]["sha"], "abcdef1")
        self.assertIsNone(refs[2]["slug"])


class TestSuperseded(unittest.TestCase):

    def test_one_answer_from_the_close_reason(self):
        b = enriched(view([pr(1, state="closed", closed_at="2026-05-10T00:00:00Z",
                              comments_list=[comment("m", "2026-05-10T00:00:00Z",
                                                     "Superseded by #2.", 3)]),
                           pr(2, comments_list=talk(2)), pr(3, comments_list=talk(3))],
                          pr_history=hist()))
        r = link.record_superseded(b, 1, 2)
        self.assertTrue(r["holds"])
        self.assertEqual((r["evidence"]["by"], r["evidence"]["at"][:10]), ("m", "2026-05-10"))
        self.assertEqual(link.record_superseded(b, 1, 3)["reason"], "other_item")
        self.assertEqual(link.record_superseded(b, 1, 2, day="2026-05-09")["reason"],
                         "after_day")
        self.assertEqual(link.record_superseded(b, 2, 1)["reason"], "not_stated")
        self.assertEqual(link.record_superseded(b, 99, 1)["reason"], "no_record")
        # the parser reads the same words as content_landed_in
        self.assertEqual(flat(claims("Superseded by #2.")),
                         [(None, "content_landed_in", 2, False)])


class TestSentenceShapes(unittest.TestCase):

    def test_shapes(self):
        cases = [
            ("#12 is needed by #30.", [(12, "needed_by", 30, False)]),
            ("#8, which blocks #9, landed first.", [(8, "blocks", 9, False)]),
            ("Blocked by #1, #2 and #3.", [(None, "blocked_by", 1, False),
                                          (None, "blocked_by", 2, False),
                                          (None, "blocked_by", 3, False)]),
            ("Blocked by (#4).", [(None, "blocked_by", 4, False)]),
            ("This change (see #5) depends on #6.", [(None, "depends_on", 6, False),
                                                    (None, "mentions", 5, False)]),
            ("Blocked by `#7` in the example.", []),
            ("> Blocked by #7", []),
            ("See #8.", [(None, "mentions", 8, False)]),
            ("Fixes #9.", [(None, "mentions", 9, False)]),
            ("Closes #9 and needs #10.", [(None, "depends_on", 10, False),
                                          (None, "mentions", 9, False)]),
            ("This is not blocked by #2 but by #3.", [(None, "blocked_by", 2, True),
                                                      (None, "blocked_by", 3, False)]),
        ]
        for text, want in cases:
            self.assertEqual(flat(claims(text)), want, text)
        # code is not read, but the quote keeps the words as written
        c = claims("Blocked by #2 in `the-module`.")[0]
        self.assertEqual(c["quote"], "Blocked by #2 in `the-module`.")

    def test_quoted_record_words_make_no_edge(self):
        b = enriched(view([pr(1, comments_list=talk(1) + [
            comment("m", "2026-05-20T00:00:00Z", "> Blocked by #2\n\nThat is resolved.", 7)]),
            pr(2, comments_list=talk(2))], pr_history=hist()))
        self.assertEqual(edges(b), [])


if __name__ == "__main__":
    unittest.main()
