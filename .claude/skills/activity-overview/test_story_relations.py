"""How stories depend on each other (issue #97, the owner: work "depends on
other people and artifacts that have to happen sometimes as reactions to
limits"): relations between trains from the explicit record only, as of the
view's end, within a repository and across the members of a stream."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import validate  # noqa: E402
from test_story import URL, comment, pr  # noqa: E402
from test_story_groups import merged_pr, rows, talk, view  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402


def hist(*rows_):
    return {"collected": True, "base": "main", "prs": list(rows_)}


def issue(n, body="", **kw):
    out = {"number": n, "title": "issue {}".format(n), "body": body, "author": "r",
           "state": "open", "created_at": "2026-05-02T00:00:00Z",
           "url": "{}/issues/{}".format(URL, n), "labels": [], "comments_list": []}
    out.update(kw)
    return out


def edges(b, rel=None):
    return [e for e in b["story_graph"]["edges"] if rel is None or e["relation"] == rel]


def two(body_1="", body_2="", c1=(), c2=(), **kw):
    return enriched(view([pr(1, body=body_1, comments_list=list(c1) or talk(1)),
                          pr(2, body=body_2, comments_list=list(c2) or talk(2))],
                         pr_history=hist(), **kw))


class TestCues(unittest.TestCase):

    def test_each_relation_kind_and_its_negative(self):
        cases = [("blocked by #2", "blocked_by", "o/r:train-pr-1", "o/r:train-pr-2"),
                 ("This is blocking #2.", "blocked_by", "o/r:train-pr-2", "o/r:train-pr-1"),
                 ("Depends on #2", "depends_on", "o/r:train-pr-1", "o/r:train-pr-2"),
                 ("This is a prerequisite for #2", "depends_on", "o/r:train-pr-2",
                  "o/r:train-pr-1"),
                 ("Follow-up to #2", "follow_up_of", "o/r:train-pr-1", "o/r:train-pr-2"),
                 ("The rest is follow-up in #2", "follow_up_of", "o/r:train-pr-2",
                  "o/r:train-pr-1"),
                 ("Regression from #2", "caused_by", "o/r:train-pr-1", "o/r:train-pr-2"),
                 ("See #2 for context.", "mentions", "o/r:train-pr-1", "o/r:train-pr-2")]
        for body, rel, frm, to in cases:
            b = two(body_1=body)
            got = [(e["relation"], e["from"], e["to"]) for e in edges(b)]
            self.assertEqual(got, [(rel, frm, to)], body)
            self.assertEqual(edges(b)[0]["basis"]["quote"], body.strip())

    def test_a_bare_mention_is_never_blocked_by(self):
        b = two(body_1="We talked about #2 being blocked by the API, not by this.")
        self.assertEqual([e["relation"] for e in edges(b)], ["mentions"])
        b = two(body_1="Blocked by an upstream decision; unrelated: #2.")
        self.assertEqual([e["relation"] for e in edges(b)], ["mentions"])

    def test_task_list_items_are_dependencies(self):
        b = two(body_1="Plan:\n- [ ] #2 tests\n- [x] docs")
        self.assertEqual(edges(b)[0]["relation"], "depends_on")
        self.assertEqual(edges(b)[0]["basis"]["quote"], "- [ ] #2")


class TestStructural(unittest.TestCase):

    def test_stacked_and_content_landed_in(self):
        shared = commit("a" * 40, "2026-05-03T00:00:00Z", "fix(ci): x", [".github/w.yml"])
        h = hist({"number": 1, "status": "fetched", "commits": [dict(shared)]},
                 {"number": 2, "status": "fetched", "commits": [dict(shared), commit(
                     "b" * 40, "2026-05-20T00:00:00Z", "more", ["avm/res/a/b/main.bicep"])]})
        b = enriched(view([pr(1, created_at="2026-05-02T00:00:00Z", comments_list=talk(1)),
                           pr(2, created_at="2026-05-19T00:00:00Z", comments_list=talk(2))],
                          pr_history=h))
        st = edges(b, "stacked_on")
        self.assertEqual((st[0]["from"], st[0]["to"], st[0]["basis"]["commits"]),
                         ("o/r:train-pr-2", "o/r:train-pr-1", 1))
        h2 = hist({"number": 1, "status": "fetched", "commits": [dict(shared)]},
                  {"number": 2, "status": "fetched", "commits": [dict(shared)]})
        b2 = enriched(view([merged_pr(1, created_at="2026-05-02T00:00:00Z"),
                            pr(2, state="closed", closed_at="2026-05-10T00:00:00Z",
                               created_at="2026-05-03T00:00:00Z")], pr_history=h2))
        self.assertEqual(edges(b2, "stacked_on"), [])     # a closed copy: content landed
        landed = edges(b2, "content_landed_in")
        self.assertEqual((landed[0]["from"], landed[0]["to"], landed[0]["basis"]["basis"]),
                         ("o/r:train-pr-2", "o/r:train-pr-1", "same commit"))


class TestAsOf(unittest.TestCase):

    def test_a_reference_written_after_the_window_is_not_used(self):
        late = comment("bob", "2026-06-05T00:00:00Z", "Blocked by #2", 9)
        b = two(c1=talk(1) + [late])
        self.assertEqual(edges(b), [])

    def test_a_blocker_closed_by_the_end_is_not_waited_on(self):
        b = enriched(view([pr(1, body="Blocked by #2", comments_list=talk(1)),
                           pr(2, state="closed", merged=True,
                              merged_at="2026-05-20T00:00:00Z",
                              closed_at="2026-05-20T00:00:00Z", comments_list=talk(2))],
                          pr_history=hist()))
        e = edges(b, "blocked_by")[0]
        self.assertEqual((e["target_state"], e["waiting"]), ("merged", False))
        self.assertNotIn("waits on", rows(b)["train-pr-1"].get("status_reason") or "")

    def test_an_item_created_after_the_window_is_not_a_target(self):
        b = enriched(view([pr(1, body="Blocked by #9", comments_list=talk(1))],
                          pr_history=hist()))
        self.assertEqual(edges(b), [])


class TestBallAndRows(unittest.TestCase):

    def test_r9_names_the_open_blocker(self):
        b = two(body_1="This needs more work. Blocked by #2.")
        o = link.slice_train(b, "train-pr-1")["conversation"]["outcome"]
        self.assertEqual((o["ball"]["state"], o["ball"]["rule"]),
                         ("awaiting blocker", "R9 blocked by an open item"))
        self.assertEqual(o["ball"]["waits_on"][0]["story"], "o/r:train-pr-2")
        self.assertEqual(o["relations"][0]["relation"], "blocked_by")
        r1 = rows(b)["train-pr-1"]
        self.assertIn("waits on PR #2 (blocked by it", r1["status_reason"])
        r2 = rows(b)["train-pr-2"]
        self.assertEqual(r2["relations"][0]["relation"], "blocks")
        self.assertEqual(r1["group"], rows(two())["train-pr-1"]["group"])   # group unchanged

    def test_target_kinds(self):
        h = hist({"number": 2, "status": "fetched", "commits": [dict(
            commit("a" * 40, "2026-05-10T00:00:00Z", "new", ["avm/res/n/new/main.bicep"]),
            added=["avm/res/n/new/main.bicep"])]})
        b = enriched(view([pr(1, body="Depends on #2", comments_list=talk(1)),
                           pr(2, title="feat: new module", comments_list=talk(2)),
                           ], pr_history=h))
        kinds = {e["to"]: e["target_kind"] for e in edges(b)}
        self.assertEqual(kinds["o/r:train-pr-2"], "new_module_proposed")
        self.assertEqual(link.relation_target_kind(
            {"trains": [{"id": "train-issue-4", "issues": [4], "prs": []}]},
            "train-issue-4"), "issue_request")

    def test_graph_chains_and_most_blocking(self):
        b = enriched(view([pr(1, body="Blocked by #2", comments_list=talk(1)),
                           pr(2, body="Depends on #3", comments_list=talk(2)),
                           pr(3, comments_list=talk(3)),
                           pr(4, body="Depends on #3", comments_list=talk(4))],
                          pr_history=hist()))
        g = b["story_graph"]
        self.assertEqual(g["chains"][0], ["o/r:train-pr-1", "o/r:train-pr-2", "o/r:train-pr-3"])
        self.assertEqual((g["most_blocking"][0]["story"], g["most_blocking"][0]["waiting_stories"]),
                         ("o/r:train-pr-3", 2))
        self.assertIsNone(validate.story_graph_problem(b))


class TestValidate(unittest.TestCase):

    def test_rules(self):
        b = two(body_1="Blocked by #2")
        self.assertIsNone(validate.story_graph_problem(b))
        for mutate in (lambda e: e.update(relation="guessed"),
                       lambda e: e.update(relation="mentions", waiting=True),
                       lambda e: e["basis"].pop("quote"),
                       lambda e: e.update(to={"repo": "x/y", "number": 1}, external=True,
                                          target_state="open")):
            g = copy.deepcopy(b)
            mutate(g["story_graph"]["edges"][0])
            self.assertIsNotNone(validate.story_graph_problem(g))


def member(slug, prs, issues=()):
    owner, name = slug.split("/")
    b = view(prs, issues, pr_history=hist())
    b["meta"]["owner"], b["meta"]["repo"] = owner, name
    return enriched(b)


class TestAcrossRepositories(unittest.TestCase):
    """A synthetic two-member stream (no real group store exists yet)."""

    def members(self):
        a = member("o/a", [pr(1, body="Blocked by o/b#5 and o/c#7",
                              url="https://github.com/o/a/pull/1", comments_list=talk(1))])
        bb = member("o/b", [pr(5, url="https://github.com/o/b/pull/5", comments_list=talk(5))])
        return [{"repo": "o/a", "bundle": a}, {"repo": "o/b", "bundle": bb}]

    def test_a_member_reference_resolves_to_its_story(self):
        ms = self.members()
        before = edges(ms[0]["bundle"])
        self.assertTrue(all(e.get("external") for e in before))
        link.join_member_relations(ms)
        a, b2 = ms[0]["bundle"], ms[1]["bundle"]
        by_to = {str(e["to"]): e for e in edges(a)}
        e = by_to["o/b:train-pr-5"]
        self.assertEqual((e["relation"], e["cross_member"], e["target_state"], e["waiting"]),
                         ("blocked_by", True, "open", True))
        ext = [e for e in edges(a) if e.get("external")]
        self.assertEqual(ext[0]["to"]["repo"], "o/c")
        self.assertNotIn("target_state", ext[0])
        rel = rows(b2)["train-pr-5"]["relations"][0]
        self.assertEqual((rel["relation"], rel["other"], rel["cross_member"]),
                         ("blocks", "o/a:train-pr-1", True))
        self.assertIsNone(validate.story_graph_problem(a))

    def test_digest_joins_members(self):
        import digest
        src = open(os.path.join(os.path.dirname(__file__), "digest.py")).read()
        self.assertIn("join_member_relations(members)", src)
        self.assertTrue(callable(digest.build_project_view))


if __name__ == "__main__":
    unittest.main()
