"""Every story of the window, grouped (issue #97, the owner: "All the
stories for that month"; "state it's landed, in flight, stalled, plumbing
— and group by this also"; "does landed imply a new release?"; "What's the
narrative [of the plumbing]?"; automation accounts "for what they are"),
plus two record fixes found on the sample newsletter: a decorator block's
entries belong to the declaration below it, and who closed an issue and
why reaches the view."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
from test_pr_activity import _fact, check_run  # noqa: E402
from test_story import URL, bundle, comment, pr, review  # noqa: E402
from test_story_substance import commit, conversation, enriched  # noqa: E402

N = gather.normalize_check_context


def view(prs, issues=(), **kw):
    kw.setdefault("pr_history", {"collected": True, "base": "main", "prs": []})
    return bundle(prs, issues, **kw)


def merged_pr(n, **kw):
    return pr(n, state="closed", merged=True, merged_at="2026-05-20T10:00:00Z",
              closed_at="2026-05-20T10:00:00Z", **kw)


def talk(n=1, at="2026-05-25T10:00:00Z"):
    return [comment("bob", at, "Why this?", 10 + n), comment("alice", at, "Because.",
                                                            20 + n)]


def rows(b):
    return {r["id"]: r for r in (b.get("stories") or {}).get("all") or []}


class TestStoryWorthyAndGroups(unittest.TestCase):

    def test_every_train_with_a_window_event_is_a_story(self):
        b = enriched(view([merged_pr(1), pr(2, comments_list=talk(2)),
                           pr(3, created_at="2026-01-01T00:00:00Z",
                              updated_at="2026-01-02T00:00:00Z")]))
        r = rows(b)
        self.assertEqual(r["train-pr-1"]["worthy"], "PR #1 opened in the window")
        self.assertEqual(r["train-pr-2"]["worthy"], "2 human turn(s) in the window")
        self.assertNotIn("train-pr-3", r)            # no event, no conversation
        self.assertEqual(r["train-pr-1"]["group"], "landed")
        self.assertEqual(b["stories"]["all_rule"]["stalled_days"],
                         link.STORY_STALLED_DAYS)

    def test_groups_by_rule(self):
        old = "2026-01-01T00:00:00Z"
        stale = pr(4, created_at=old, updated_at=old,
                   comments_list=[comment("bob", "2026-05-02T00:00:00Z", "?", 41),
                                  comment("alice", "2026-05-03T00:00:00Z", "!", 42)])
        carried = pr(5, created_at=old, updated_at=old,
                     comments_list=[comment("bob", "2026-03-02T00:00:00Z", "?", 51),
                                    comment("alice", "2026-03-03T00:00:00Z", "!", 52)])
        rejected = pr(6, state="closed", closed_at="2026-05-10T00:00:00Z")
        b = enriched(view([stale, carried, rejected,
                           pr(7, comments_list=talk(7))]))
        r = rows(b)
        self.assertEqual(r["train-pr-4"]["state"], "stalled")
        self.assertIn("days since the last human event", r["train-pr-4"]["group_rule"])
        self.assertEqual(r["train-pr-5"]["state"], "stalled")
        self.assertIn("carried over", r["train-pr-5"]["group_rule"])
        self.assertEqual(r["train-pr-6"]["state"], "closed")
        self.assertEqual(r["train-pr-7"]["state"], "in_flight")
        groups = [x["group"] for x in b["stories"]["all"]]
        self.assertEqual(groups, sorted(groups, key=link.STORY_GROUPS.index))

    def test_failing_head_without_the_authors_answer_is_stalled(self):
        f = _fact()
        f["prs"][0]["commits"][1]["checks"] = [N(check_run(
            "tests", "FAILURE", started="2026-05-26T00:01:00Z",
            completed="2026-05-26T00:09:00Z"))]
        f["prs"][0]["commits"][1]["committed_at"] = "2026-05-26T00:00:00Z"
        b = enriched(view([pr(1, comments_list=talk(1, "2026-05-25T10:00:00Z"))],
                          pr_activity=f))
        self.assertIn("head checks failing", rows(b)["train-pr-1"]["group_rule"])

    def test_plumbing_group_keeps_its_state(self):
        t = {"id": "t", "prs": [1], "outcome": "shipped"}
        mc = {"prs": [{"number": 1, "plumbing": True}]}
        self.assertEqual(link.story_group({}, t, "x", None, mc)[:2],
                         ("plumbing", "landed"))

    def test_without_the_story_layer_nothing_changes(self):
        b = enriched(bundle([merged_pr(1)]))
        self.assertNotIn("stories", b)


class TestReleased(unittest.TestCase):

    def mc(self, versions, tags=True):
        return {"inputs": {"tags_collected": tags},
                "modules": [{"area": "avm/res/x", "kind": "resource",
                             "url": "u", "versions": versions,
                             "prs": [{"number": 1}]}]}

    def test_statuses(self):
        t = {"prs": [1]}
        pub = {"published": [{"version": "0.2.0", "tag": "avm/res/x/0.2.0",
                              "date": "d", "url": "tu", "registry": "br/public:avm/res/x:0.2.0"}]}
        self.assertEqual(link.train_released({"module_changes": self.mc(pub)}, t), [
            {"module": "avm/res/x", "version": "0.2.0", "status": "published",
             "tag": "avm/res/x/0.2.0", "date": "d", "url": "tu",
             "registry": "br/public:avm/res/x:0.2.0"}])
        decl = {"changelog": ["0.2.0"]}
        self.assertEqual(link.train_released({"module_changes": self.mc(decl)}, t)[0]
                         ["status"], "declared_not_published")
        self.assertEqual(link.train_released({"module_changes": self.mc(decl, False)},
                                             t)[0]["status"], "unknown")
        self.assertEqual(link.train_released({"module_changes": self.mc({})}, t)[0]
                         ["status"], "no_version_change")

    def test_module_links(self):
        b = {"meta": {"owner": "o", "repo": "r", "clone_sha": "c" * 40}}
        e = [{"area": "avm/res/x", "kind": "resource",
              "versions": {"published": [{"version": "0.2.0",
                                          "tag": "avm/res/x/0.2.0"}]}}]
        link._module_links(b, e)
        self.assertEqual(e[0]["url"], "https://github.com/o/r/tree/{}/avm/res/x".format(
            "c" * 40))
        self.assertEqual(e[0]["versions"]["published"][0]["url"],
                         "https://github.com/o/r/tree/avm/res/x/0.2.0")
        self.assertEqual(e[0]["versions"]["published"][0]["registry"],
                         "br/public:avm/res/x:0.2.0")


class TestPlumbingThemes(unittest.TestCase):

    def test_themes_by_shared_paths(self):
        prs = [merged_pr(1, title="ci: cache"), merged_pr(2, title="test: fixtures"),
               merged_pr(3, title="chore(deps): bump actions/checkout",
                         author="dependabot[bot]")]
        hist = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched", "commits": [
                dict(commit("a" * 40, "2026-05-10T00:00:00Z", "ci: cache modules",
                            [".github/workflows/a.yml", ".github/actions/b/action.yml"]),
                     comments=[{"path": ".github/workflows/a.yml", "change": "add",
                                "text": "# keep the cache warm"}])]},
            {"number": 2, "status": "fetched", "commits": [
                commit("b" * 40, "2026-05-11T00:00:00Z", "test: fixtures",
                       ["avm/res/x/tests/e2e/max/main.test.bicep"])]},
            {"number": 3, "status": "fetched", "commits": [
                commit("c" * 40, "2026-05-12T00:00:00Z", "bump",
                       [".github/workflows/a.yml"])]}]}
        b = view(prs, pr_history=hist)
        b["module_changes"] = {"prs": [{"number": n, "plumbing": True}
                                       for n in (1, 2, 3)]}
        link.build_plumbing_themes(b)
        th = {t["id"]: t for t in b["plumbing_themes"]}
        self.assertEqual([p["number"] for p in th["ci_workflows"]["prs"]], [1])
        self.assertEqual(th["ci_workflows"]["title_hint"], "CI workflows")
        self.assertEqual(th["ci_workflows"]["evidence_counts"]["comments"], 1)
        self.assertEqual(th["ci_workflows"]["subjects"][0]["subject"],
                         "ci: cache modules")
        self.assertEqual([p["number"] for p in th["test_environment"]["prs"]], [2])
        self.assertEqual([p["number"] for p in th["dependency_updates"]["prs"]], [3])


class TestAutomationAccounts(unittest.TestCase):

    def test_bot_pr_on_behalf_of_the_assigner(self):
        f = _fact()
        f["prs"][0]["events"].append({"id": 11, "event": "assigned",
                                      "at": "2026-05-02T00:00:00Z", "actor": "carol",
                                      "assignee": "coding-agent[bot]"})
        b = view([pr(1, author="coding-agent[bot]", comments_list=talk(1),
                     reviews=[review("bob", "2026-05-25T11:00:00Z",
                                     "changes_requested", 31, "No")])],
                 pr_activity=f)
        c = conversation(b)
        self.assertEqual(c["automation"], [{
            "login": "coding-agent[bot]", "is_bot": True, "comments": 0,
            "review_comments": 0, "reviews": 0, "commits": 0, "prs_opened": [1],
            "on_behalf_of": {"login": "carol", "basis": "assigned", "pr": 1}}])


class TestDecoratorBelongsBelow(unittest.TestCase):
    """#7502: an `@allowed` entry added above `loadBalancerType` was
    attributed to the parameter above it."""

    def test_allowed_entry_is_the_next_parameters(self):
        lines = [(" ", "])"), (" ", "param personalDesktopAssignmentType string = ''"),
                 (" ", ""), ("-", "@sys.description('Optional. Type.')"),
                 ("+", "@sys.description('Optional. Type. Use MultiplePersistent.')"),
                 (" ", "@allowed(["), (" ", "  'BreadthFirst'"),
                 ("+", "  'MultiplePersistent'"), (" ", "])"),
                 (" ", "param loadBalancerType string = 'BreadthFirst'")]
        deltas = gather.build_symbol_deltas("m/main.bicep",
                                            [{"new_start": 41, "lines": lines}])
        params = [(d["name"], d["change"]) for d in deltas if d["subkind"] == "param"]
        self.assertEqual(params, [("loadBalancerType", "change")])


class TestIssueClosing(unittest.TestCase):

    def test_who_closed_it_and_before_which_merge(self):
        issue = {"number": 9, "title": "Add MultiplePersistent", "author": "col",
                 "url": f"{URL}/issues/9", "state": "closed", "labels": [],
                 "created_at": "2026-05-08T00:00:00Z", "closed_at": "2026-05-09T16:50:03Z",
                 "state_reason": "completed", "body": "", "comments_list": [],
                 "lifecycle": [{"id": 5, "event": "closed", "actor": "rei",
                                "created_at": "2026-05-09T16:50:03Z", "label": None,
                                "url": "u"}]}
        p = pr(1, author="rei", closes=[9], created_at="2026-05-09T16:48:19Z",
               state="closed", merged=True, merged_at="2026-05-18T12:23:11Z",
               comments_list=talk(1))
        b = enriched(view([p], [issue]))
        self.assertEqual(b["issues"][0]["closing"], {
            "at": "2026-05-09T16:50:03Z", "by": "rei", "state_reason": "completed",
            "prs": [1], "before_merge": [1]})
        c = conversation(view([p], [issue]))
        closed = next(i for i in c["timeline"] if i["kind"] == "closed"
                      and i["on"]["type"] == "issue")
        self.assertEqual((closed["actor"], closed["state_reason"], closed["closing_prs"]),
                         ("rei", "completed", [1]))

    def test_timeline_keeps_state_reason_and_commit(self):
        ev = gather.parse_timeline_lifecycle([{
            "id": 1, "event": "closed", "actor": {"login": "a"},
            "created_at": "t", "state_reason": "not_planned", "commit_id": "d" * 40}])
        self.assertEqual((ev[0]["state_reason"], ev[0]["commit"]),
                         ("not_planned", "d" * 40))


if __name__ == "__main__":
    unittest.main()
