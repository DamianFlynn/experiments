"""Review fixes on the whole-record story layer (issue #97): the repository
slug in one place, plumbing classification covering every PR, releases
tied to their PR, and the view's as-of instant everywhere the new code
reads a fact or a record. Each reproduces the finding before its fix."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
from test_pr_activity import _fact, check_run  # noqa: E402
from test_story import URL, bundle, comment, pr, review  # noqa: E402
from test_story_groups import merged_pr, talk, view  # noqa: E402
from test_story_substance import commit, conversation, enriched  # noqa: E402

AFTER = "2026-06-03T00:00:00Z"        # the views below end 2026-05-31
N = gather.normalize_check_context


class TestRepositorySlug(unittest.TestCase):

    def test_one_helper(self):
        self.assertEqual(link.repo_slug({"owner": "o", "repo": "r"}), "o/r")
        self.assertEqual(link.repo_slug({"owner": "proj", "repo": "Azure/example"}),
                         "Azure/example")
        self.assertIsNone(link.repo_slug({"owner": "o"}))

    def test_member_view_urls_and_forks(self):
        b = {"meta": {"owner": "proj", "repo": "Azure/example", "clone_sha": "c" * 40}}
        e = [{"area": "avm/res/x", "kind": "resource",
              "versions": {"published": [{"version": "1.0.0", "tag": "avm/res/x/1.0.0"}]}}]
        link._module_links(b, e)
        self.assertTrue(e[0]["url"].startswith("https://github.com/Azure/example/tree/"))
        b["prs"] = [pr(1, head_repo="Azure/example"), pr(2, head_repo="someone/example")]
        link.annotate_pr_forks(b)
        self.assertEqual([p["is_fork"] for p in b["prs"]], [False, True])

    def test_linked_numbers_in_a_member_view(self):
        b = {"meta": {"owner": "proj", "repo": "Azure/example"}}
        found = link._linked_numbers(
            b, [("see https://github.com/Azure/example/issues/9", "u")], set())
        self.assertEqual(found, [(9, "u")])


class TestPlumbingNeedsEveryPrClassified(unittest.TestCase):

    def test_open_follow_up_is_classified_from_its_history(self):
        b = view([merged_pr(1, title="ci: fix"), pr(2, title="feat: user facing")])
        b["pr_history"]["prs"] = [{"number": 2, "status": "fetched", "commits": [
            commit("a" * 40, "2026-05-10T00:00:00Z", "feat", ["avm/res/x/main.bicep"])]}]
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        t = {"id": "t", "prs": [1, 2], "outcome": "in_flight"}
        self.assertFalse(link._train_plumbing(t, b["module_changes"], b))
        # an open PR whose files are CI only is plumbing too
        b["pr_history"]["prs"][0]["commits"][0]["paths"] = [".github/workflows/a.yml"]
        self.assertTrue(link._train_plumbing(t, b["module_changes"], b))
        # a PR that cannot be classified keeps the train out of plumbing
        b["pr_history"]["prs"] = []
        self.assertFalse(link._train_plumbing(t, b["module_changes"], b))


class TestReleasedTiedToThePr(unittest.TestCase):

    def mc(self, changelog_pr, tag_commit="d" * 40):
        return {"inputs": {"tags_collected": True}, "modules": [{
            "area": "avm/res/x", "kind": "resource", "url": "u",
            "prs": [{"number": 1}],
            "changelog": [{"version": "0.2.0", "sources": [{"pr": changelog_pr}]}],
            "versions": {"changelog": ["0.2.0"], "published": [{
                "version": "0.2.0", "tag": "avm/res/x/0.2.0", "date": "d",
                "commit": tag_commit, "url": "tu"}]}}]}

    def test_published_only_when_tied(self):
        t = {"prs": [1], "commits": ["e" * 40]}
        self.assertEqual(link.train_released({"module_changes": self.mc(1)}, t)[0]
                         ["status"], "published")
        other = link.train_released({"module_changes": self.mc(5)}, t)[0]
        self.assertEqual((other["status"], other["version"]), ("unknown", None))
        self.assertIn("not tied", other["reason"])
        on_merge = link.train_released({"module_changes": self.mc(5, "e" * 40)}, t)[0]
        self.assertEqual(on_merge["status"], "published")

    def test_first_tag_after_the_merge_on_the_linear_branch(self):
        mc = self.mc(5)
        mc["modules"][0]["versions"]["published"] = [
            {"version": "0.1.9", "tag": "avm/res/x/0.1.9", "date": "2026-05-01T00:00:00Z",
             "commit": "a" * 40},
            {"version": "0.2.0", "tag": "avm/res/x/0.2.0", "date": "2026-05-20T00:00:00Z",
             "commit": "d" * 40},
            {"version": "0.2.1", "tag": "avm/res/x/0.2.1", "date": "2026-05-25T00:00:00Z",
             "commit": "f" * 40}]
        mc["modules"][0]["changelog"] = []
        b = {"module_changes": mc,
             "commits": [{"sha": "e" * 40, "date": "2026-05-10T00:00:00Z"}]}
        got = link.train_released(b, {"prs": [1], "commits": ["e" * 40]})
        self.assertEqual([(r["version"], r["status"]) for r in got],
                         [("0.2.0", "published")])


class TestAsOf(unittest.TestCase):

    def test_themes_ignore_commits_after_the_instant(self):
        hist = {"collected": True, "base": "main", "prs": [{"number": 1, "status":
                "fetched", "commits": [
                    commit("a" * 40, "2026-05-10T00:00:00Z", "ci: a", [".github/workflows/a.yml"]),
                    commit("b" * 40, AFTER, "test: later",
                           ["x/tests/e2e/a.bicep", "x/tests/e2e/b.bicep"])]}]}
        b = view([merged_pr(1)], pr_history=hist)
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        link.build_plumbing_themes(b)
        th = b["plumbing_themes"][0]
        self.assertEqual(th["id"], "ci_workflows")
        self.assertEqual(th["evidence_counts"]["commits"], 1)
        self.assertEqual([s["subject"] for s in th["subjects"]], ["ci: a"])

    def test_automation_ignores_events_and_commits_after_the_instant(self):
        f = _fact()
        f["prs"][0]["events"].append({"id": 11, "event": "assigned", "at": AFTER,
                                      "actor": "carol", "assignee": "agent[bot]"})
        hist = {"collected": True, "base": "main", "prs": [{"number": 1, "status":
                "fetched", "commits": [dict(commit("a" * 40, AFTER, "bot", ["a"]),
                                            author="github-actions[bot]")]}]}
        b = view([pr(1, author="agent[bot]", comments_list=talk(1),
                     reviews=[review("bob", "2026-05-25T11:00:00Z",
                                     "changes_requested", 31, "No")])],
                 pr_activity=f, pr_history=hist)
        c = conversation(b)
        acc = {a["login"]: a for a in c["automation"]}
        self.assertNotIn("on_behalf_of", acc["agent[bot]"])
        self.assertNotIn("github-actions[bot]", acc)

    def test_state_only_result_read_after_the_instant_is_unknown(self):
        f = _fact(commits_total=3)
        f["prs"][0]["commits"].insert(0, {
            "sha": "9" * 40, "committed_at": "2026-05-01T00:00:00Z", "rollup": "passing",
            "checks": [], "state_only": True, "state": "success"})
        c = conversation(view([pr(1, comments_list=talk(1))], pr_activity=f))
        item = next(i for i in c["timeline"] if i.get("sha") == "9" * 40)
        self.assertEqual(item["rollup"], "unknown")
        ch = c["outcome"]["items"][0]["checks"]
        self.assertEqual(ch["commits_incomplete"], 1)
        # gathered by the instant (a nightly gather of the open window): usable
        f["gathered_at"] = "2026-05-31T22:00:00Z"
        c = conversation(view([pr(1, comments_list=talk(1))], pr_activity=f))
        item = next(i for i in c["timeline"] if i.get("sha") == "9" * 40)
        self.assertEqual(item["rollup"], "passing")

    def test_closing_ignores_a_close_after_the_instant(self):
        issue = {"number": 9, "title": "t", "author": "col", "url": f"{URL}/issues/9",
                 "state": "closed", "labels": [], "created_at": "2026-05-08T00:00:00Z",
                 "closed_at": "2026-05-09T00:00:00Z", "body": "", "comments_list": [],
                 "lifecycle": [{"id": 5, "event": "closed", "actor": "early",
                                "created_at": "2026-05-09T00:00:00Z", "label": None,
                                "url": "u"},
                               {"id": 6, "event": "closed", "actor": "late",
                                "created_at": AFTER, "label": None, "url": "u"}]}
        b = enriched(view([pr(1, comments_list=talk(1))], [issue]))
        self.assertEqual(b["issues"][0]["closing"]["by"], "early")

    def test_open_pr_classification_ignores_commits_after_the_instant(self):
        b = view([merged_pr(1, title="ci: fix"), pr(2, title="ci: follow-up")])
        b["pr_history"]["prs"] = [{"number": 2, "status": "fetched", "commits": [
            commit("a" * 40, AFTER, "ci", [".github/workflows/a.yml"])]}]
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        t = {"id": "t", "prs": [1, 2], "outcome": "in_flight"}
        self.assertFalse(link._train_plumbing(t, b["module_changes"], b))

    def test_story_worthy_ignores_history_after_the_instant(self):
        hist = {"collected": True, "base": "main", "prs": [{"number": 1, "status":
                "fetched", "commits": [commit("a" * 40, AFTER, "later", ["a"])]}]}
        old = "2026-01-01T00:00:00Z"
        b = enriched(view([pr(1, created_at=old, updated_at=old)], pr_history=hist))
        self.assertNotIn("train-pr-1", {r["id"] for r in
                                        (b.get("stories") or {}).get("all") or []})


if __name__ == "__main__":
    unittest.main()
