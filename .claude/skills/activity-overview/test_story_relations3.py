"""Second review round on the relation layer (PR #123): one reference-and-cue
parser, evidence kept per item pair, one predicate (relation_holds), one
stream graph. Each test reproduces its finding before the fix."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import validate  # noqa: E402
from test_story import comment, pr  # noqa: E402
from test_story_groups import rows, talk, view  # noqa: E402
from test_story_relations import edges, hist, issue, member, two  # noqa: E402
from test_story_substance import enriched  # noqa: E402


def claims(text, own="o/r", default=None):
    return link.parse_relation_claims(text, own, default)


def targets(cs):
    return [(c["relation"], t["slug"], t["number"]) for c in cs for t in c["targets"]]


class TestParser(unittest.TestCase):

    def test_a_markdown_link_to_an_item_is_one_reference(self):
        # finding 4727222610: parsed as local #5 AND as o/b#5
        cs = claims("Blocked by [#5](https://github.com/o/b/pull/5)")
        self.assertEqual(targets(cs), [("blocked_by", "o/b", 5)])
        self.assertEqual(cs[0]["targets"][0]["url"], "https://github.com/o/b/pull/5")
        b = enriched(view([pr(1, body="Blocked by [#5](https://github.com/o/b/pull/5)",
                              comments_list=talk(1)), pr(5, comments_list=talk(5))],
                          pr_history=hist()))
        self.assertEqual([(e["relation"], e["to"]) for e in edges(b)],
                         [("blocked_by", {"repo": "o/b", "number": 5,
                                          "url": "https://github.com/o/b/pull/5"})])

    def test_a_task_line_with_a_bare_item_url(self):
        # finding 4727222632: no "#" on the line made it a mention
        cs = claims("- [ ] https://github.com/o/r/pull/2")
        self.assertEqual(targets(cs), [("depends_on", None, 2)])
        self.assertEqual(cs[0]["task"], "open")
        b = two(body_1="Plan:\n- [ ] https://github.com/o/r/pull/2")
        e = edges(b)[0]
        self.assertEqual((e["relation"], e["to"], e["basis"]["task_done"], e["waiting"]),
                         ("depends_on", "o/r:train-pr-2", False, True))

    def test_evidence_links_are_transparent_and_not_items(self):
        text = ("Blocked by #3 ([comment](https://github.com/o/r/pull/3#issuecomment-9)) "
                "and #4, see https://github.com/o/r/commit/abcdef1")
        self.assertEqual(targets(claims(text)), [("blocked_by", None, 3), ("blocked_by", None, 4)])
        # a link label is never read: "#7" here is not a reference
        self.assertEqual(targets(claims(
            "Depends on [#7 review](https://github.com/o/r/pull/7#pullrequestreview-1)")), [])
        # a label naming the anchor's own item is that item, from the destination
        cs = claims("Superseded by [#7](https://github.com/o/r/pull/7#issuecomment-3).")
        self.assertEqual(targets(cs), [("content_landed_in", None, 7)])
        self.assertEqual(cs[0]["targets"][0]["url"], "https://github.com/o/r/pull/7")
        # a link to another host is no github.com item, whatever its label says
        self.assertEqual(targets(claims("Follow-up in [o/x#5](https://example.com/o/x/pull/5)")),
                         [])

    def test_subject_carried_across_coordinated_predicates(self):
        cs = claims("#31 is a follow-up to #11 and was caused by #12.")
        got = [(c["subject"]["number"], c["relation"], c["targets"][0]["number"], c["tense"])
               for c in cs]
        self.assertEqual(got, [(31, "follow_up_of", 11, "present"), (31, "caused_by", 12, "past")])

    def test_targets_stop_at_other_text(self):
        cs = claims("Blocked by #3, #4 and o/x#5 until the release; see #6.")
        self.assertEqual(targets(cs), [("blocked_by", None, 3), ("blocked_by", None, 4),
                                       ("blocked_by", "o/x", 5), ("mentions", None, 6)])
        cs = claims("Blocked by #3 for now, unlike #4.")
        self.assertEqual(targets(cs), [("blocked_by", None, 3), ("mentions", None, 4)])

    def test_polarity_and_default_subject(self):
        me = {"slug": None, "number": 1}
        cs = claims("This is no longer blocked by #2.", default=me)
        self.assertEqual((cs[0]["relation"], cs[0]["negated"], cs[0]["subject"]),
                         ("blocked_by", True, me))
        self.assertEqual(link.relation_mentions("This is no longer blocked by #2.")[0]
                         ["relation"], "mentions")


def story(b, tid):
    return rows(b)[tid]


class TestEvidencePerPair(unittest.TestCase):

    def test_waiting_ignores_a_body_edited_after_the_window(self):
        # finding 4727222641: text_current false still waited
        b = enriched(view([pr(1, body="Blocked by #2", comments_list=talk(1),
                              body_as_of={"state": "as last gathered", "why": "edited"}),
                           pr(2, comments_list=talk(2))], pr_history=hist()))
        e = edges(b, "blocked_by")[0]
        self.assertFalse(e["waiting"])
        self.assertEqual(e["evidence"][0]["text_current"], False)
        self.assertNotIn("waits on", story(b, "train-pr-1").get("status_reason") or "")
        r = link.relation_holds(b, 1, 2, "blocked_by", present=True)
        self.assertEqual((r["holds"], r["reason"]), (False, "text_not_current"))
        self.assertTrue(link.relation_holds(b, 1, 2, "blocked_by")["holds"])

    def test_inner_relations_keyed_per_pair_and_repeats_keep_the_earliest(self):
        # finding 4727222645: keyed by train while tested by pair
        late = comment("m", "2026-05-22T00:00:00Z", "Still depends on #2.", 7)
        b = enriched(view([pr(1, closes=[9], body="Depends on #2", comments_list=talk(1) + [late],
                              created_at="2026-05-03T00:00:00Z"),
                           pr(2, closes=[9], body="Follow-up to #9", comments_list=talk(2))],
                          [issue(9)], pr_history=hist()))
        inner = story(b, "train-issue-9")["inner_relations"]
        keys = [(x["from_item"]["number"], x["to_item"]["number"], x["relation"]) for x in inner]
        self.assertIn((1, 2, "depends_on"), keys)
        self.assertIn((2, 9, "follow_up_of"), keys)
        self.assertEqual(len(keys), len(set(keys)))
        dep = next(x for x in inner if x["relation"] == "depends_on")
        self.assertEqual(dep["as_of"], "2026-05-03")
        self.assertEqual([p["as_of"] for p in dep["evidence"]], ["2026-05-03", "2026-05-22"])

    def test_a_later_active_pair_beyond_the_bound_is_kept(self):
        # finding 4727222671: 20 closed contributions first dropped the open one
        prs = [pr(n, closes=[90], body="Blocked by #50", state="closed",
                  closed_at="2026-05-05T00:00:00Z", created_at="2026-05-02T00:00:00Z",
                  comments_list=[]) for n in range(1, 26)]
        prs.append(pr(30, closes=[90], body="Blocked by #50", created_at="2026-05-20T00:00:00Z",
                      comments_list=talk(30)))
        prs.append(pr(50, comments_list=talk(50)))
        b = enriched(view(prs, [issue(90)], pr_history=hist()))
        e = next(e for e in edges(b, "blocked_by") if e["from"] == "o/r:train-issue-90")
        self.assertTrue(e["waiting"])
        self.assertEqual(e["bases"], 26)
        self.assertLessEqual(len(e["evidence"]), link.STORY_EDGE_EVIDENCE_MAX)
        self.assertEqual(e["evidence_omitted"], 26 - len(e["evidence"]))
        self.assertIn(30, [p["from_item"]["number"] for p in e["evidence"] if p.get("waiting")])
        self.assertEqual(e["from_item"]["number"], 30)

    def test_as_of_is_the_earliest_evidence(self):
        # finding 4727222691: the waiting pair's date replaced the earlier one
        b = enriched(view([pr(1, closes=[9], body="Blocked by #3", state="closed",
                              closed_at="2026-05-05T00:00:00Z", created_at="2026-05-02T00:00:00Z"),
                           pr(2, closes=[9], body="Blocked by #3", comments_list=talk(2),
                              created_at="2026-05-12T00:00:00Z"),
                           pr(3, comments_list=talk(3))], [issue(9)], pr_history=hist()))
        e = edges(b, "blocked_by")[0]
        self.assertEqual((e["as_of"], e["waiting"], e["from_item"]["number"]),
                         ("2026-05-02", True, 2))
        self.assertEqual(e["basis"]["item"]["number"], 2)
        self.assertIsNone(validate.story_graph_problem(b))
        e["as_of"] = "2026-05-12"
        self.assertIn("earliest evidence", validate.story_graph_problem(b))
        e["as_of"] = "2026-05-02"
        for p in e["evidence"]:
            p["waiting"] = False
        self.assertIn("waiting", validate.story_graph_problem(b))


class TestAcrossMembers(unittest.TestCase):

    def test_cross_member_waiting_is_per_pair(self):
        # finding 4727222657: the first source item's state decided, and a
        # ticked or edited text still waited
        a = member("o/a", [pr(1, closes=[9], body="Blocked by o/b#5", state="closed",
                              closed_at="2026-05-05T00:00:00Z", created_at="2026-05-02T00:00:00Z",
                              url="https://github.com/o/a/pull/1"),
                           pr(2, closes=[9], body="Blocked by o/b#5", comments_list=talk(2),
                              created_at="2026-05-10T00:00:00Z", url="https://github.com/o/a/pull/2"),
                           pr(3, body="- [x] o/b#5", comments_list=talk(3),
                              url="https://github.com/o/a/pull/3"),
                           pr(4, body="Depends on o/b#5", comments_list=talk(4),
                              body_as_of={"state": "as last gathered", "why": "edited"},
                              url="https://github.com/o/a/pull/4")],
                   [issue(9, url="https://github.com/o/a/issues/9")])
        bb = member("o/b", [pr(5, url="https://github.com/o/b/pull/5", comments_list=talk(5))])
        ms = link.join_member_relations([{"repo": "o/a", "bundle": a},
                                         {"repo": "o/b", "bundle": bb}])
        by = {(e["from"], e["relation"]): e for e in edges(ms[0]["bundle"])}
        self.assertTrue(by[("o/a:train-issue-9", "blocked_by")]["waiting"])
        self.assertEqual(by[("o/a:train-issue-9", "blocked_by")]["from_item"]["number"], 2)
        self.assertFalse(by[("o/a:train-pr-3", "depends_on")]["waiting"])
        self.assertFalse(by[("o/a:train-pr-4", "depends_on")]["waiting"])
        for k in by:
            self.assertTrue(by[k]["cross_member"])
        self.assertIsNone(validate.story_graph_problem(ms[0]["bundle"]))

    def test_an_external_inverse_is_checkable_and_the_source_is_the_pair(self):
        # 4727222733 / 4727222801: "this blocks o/x#1" stored as written
        b = member("o/a", [pr(1, closes=[9], comments_list=talk(1),
                              url="https://github.com/o/a/pull/1"),
                           pr(2, closes=[9], body="This blocks o/x#1.", comments_list=talk(2),
                              url="https://github.com/o/a/pull/2")],
                   [issue(9, url="https://github.com/o/a/issues/9")])
        e = next(e for e in edges(b) if e.get("external"))
        self.assertEqual((e["relation"], e["from_item"]["number"]), ("blocks", 2))
        for args in (("o/x#1", "o/a#2", "blocked_by"), ("o/a#2", "o/x#1", "blocks")):
            r = link.relation_holds(b, *args)
            self.assertTrue(r["holds"], args)
            self.assertEqual(r["evidence"][0]["from_item"]["number"], 2)
        self.assertFalse(link.relation_holds(b, "o/a#1", "o/x#1", "blocks")["holds"])
        r = link.relation_holds(b, "o/x#1", "o/a#2", "blocked_by", present=True)
        self.assertEqual((r["holds"], r["reason"]), (False, "state_unknown"))
        self.assertEqual(link.relation_holds(b, "o/x#1", "o/a#2", "blocked_by",
                                             day="2026-04-30")["reason"], "after_day")

    def test_relation_holds_both_ends_open_on_the_day(self):
        b = enriched(view([pr(1, body="Blocked by #2", comments_list=talk(1)),
                           pr(2, state="closed", merged=True, merged_at="2026-05-20T00:00:00Z",
                              closed_at="2026-05-20T00:00:00Z", comments_list=talk(2))],
                          pr_history=hist()))
        self.assertTrue(link.relation_holds(b, 1, 2, "blocked_by", day="2026-05-15",
                                            present=True)["holds"])
        r = link.relation_holds(b, 1, 2, "blocked_by", present=True)
        self.assertEqual(r["reason"], "target_not_open")
        self.assertTrue(link.relation_holds(b, 2, 1, "blocks", day="2026-05-15",
                                            present=True)["holds"])
        self.assertEqual(link.item_state({"state": "closed", "merged": True,
                                          "merged_at": "2026-05-20T00:00:00Z",
                                          "created_at": "2026-05-01T00:00:00Z"},
                                         "2026-04-30"), "not_created")
        # merged later, no closed_at recorded: open on the day before
        self.assertEqual(link.item_state({"state": "closed", "merged": True,
                                          "merged_at": "2026-05-20T00:00:00Z"},
                                         "2026-05-15"), "open")

    def test_stream_graph_chains_and_most_blocking(self):
        # 4727222711: A -> B -> C across three members is one chain
        a = member("o/a", [pr(1, body="Blocked by o/b#2", url="https://github.com/o/a/pull/1",
                              comments_list=talk(1))])
        bb = member("o/b", [pr(2, body="Depends on o/c#3", url="https://github.com/o/b/pull/2",
                               comments_list=talk(2))])
        c = member("o/c", [pr(3, url="https://github.com/o/c/pull/3", comments_list=talk(3))])
        d = member("o/d", [pr(4, body="Blocked by o/c#3", url="https://github.com/o/d/pull/4",
                              comments_list=talk(4))])
        ms = [{"repo": s, "bundle": x} for s, x in (("o/a", a), ("o/b", bb), ("o/c", c),
                                                     ("o/d", d))]
        link.join_member_relations(ms)
        g = link.stream_story_graph(ms)
        self.assertIn(["o/a:train-pr-1", "o/b:train-pr-2", "o/c:train-pr-3"], g["chains"])
        top = g["most_blocking"][0]
        self.assertEqual((top["story"], top["waiting_stories"]), ("o/c:train-pr-3", 2))
        again = link.stream_story_graph(link.join_member_relations(list(reversed(ms))))
        self.assertEqual(json.dumps(g, sort_keys=True), json.dumps(again, sort_keys=True))
        r = link.relation_holds(ms, "o/a#1", "o/b#2", "blocked_by", present=True)
        self.assertTrue(r["holds"])


if __name__ == "__main__":
    unittest.main()
