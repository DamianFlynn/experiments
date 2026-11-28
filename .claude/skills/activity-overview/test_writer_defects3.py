"""Data defects the October writers reported in review rounds 1-2 and the
pipeline / CI / test-environment themes (issue #97), each checked against
the October store first. Each test reproduces its defect before the fix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_pr_activity import _fact  # noqa: E402
from test_story import URL, comment, pr, review  # noqa: E402
from test_story_groups import merged_pr, rows, talk, view  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

AREA = "avm/res/x/y"


def hist(*rows_):
    return {"collected": True, "base": "main", "prs": list(rows_)}


def c(sha, at, subject="work", paths=(AREA + "/main.bicep",), author="Alice Example"):
    out = commit(sha * 40 if len(sha) == 1 else sha, at, subject, list(paths))
    out["author"] = author
    return out


def resolved(name, login):
    return {"sha": "f" * 40, "author": login, "author_name": name, "login_resolved": True,
            "files": [], "message": "m", "committed_at": "2026-04-01T00:00:00Z"}


def outcome(b, tid):
    return link.slice_train(b, tid)["conversation"]["outcome"]


class TestCommitActor(unittest.TestCase):

    def b(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
            c("a", "2026-05-28T00:00:00Z", author="Jared Example")]})
        b = view([pr(1, comments_list=talk(1, "2026-05-20T00:00:00Z"))], pr_history=h)
        b["commits"] = [resolved("Jared Example", "jared")]
        return enriched(b)

    def test_last_event_ball_and_people_name_the_commits_author(self):
        b = self.b()
        t = next(t for t in b["trains"] if t["id"] == "train-pr-1")
        self.assertEqual(t["effort"]["last_human_event"]["actor"], "jared")
        self.assertIn("commit by jared on PR #1", rows(b)["train-pr-1"]["status_reason"])
        o = outcome(b, "train-pr-1")
        self.assertEqual(o["ball"]["basis"]["actor"], "jared")
        self.assertIn("jared", link._train_logins(b, t))


class TestStatusReasonNamesItsItems(unittest.TestCase):

    def test_each_fact_names_its_item(self):
        i = {"number": 9, "title": "t", "body": "", "author": "r", "state": "open",
             "created_at": "2026-05-06T00:00:00Z", "url": URL + "/issues/9", "labels": [],
             "comments_list": []}
        b = enriched(view([pr(1, closes=[9], created_at="2026-05-12T00:00:00Z",
                              comments_list=talk(1, "2026-05-29T00:00:00Z"))], [i]))
        r = rows(b)["train-issue-9"]["status_reason"]
        self.assertTrue(r.startswith("opened 25 days before the window's end (issue #9)"), r)
        self.assertIn("on PR #1", r)


class TestSliceAutomationPerPr(unittest.TestCase):

    def test_another_prs_person_is_not_shown(self):
        auto = {"kind": "account_type",
                "on_behalf_of": {"login": "fb", "basis": "assignee", "pr": 7032},
                "on_behalf_of_by_pr": {"7032": {"login": "fb", "basis": "assignee",
                                                "pr": 7032},
                                       "7422": {"login": "mp", "basis": "requested review",
                                                "pr": 7422}}}
        self.assertEqual(link._automation_for_train(auto, {"prs": [7530]}),
                         {"kind": "account_type"})
        self.assertEqual(link._automation_for_train(auto, {"prs": [7422]})["on_behalf_of"],
                         {"login": "mp", "basis": "requested review", "pr": 7422})


class TestJoinedBy(unittest.TestCase):

    def test_the_reference_that_joined_a_member(self):
        i = {"number": 9, "title": "t", "body": "", "author": "r", "state": "open",
             "created_at": "2026-05-02T00:00:00Z", "url": URL + "/issues/9", "labels": [],
             "comments_list": []}
        b = enriched(view([pr(1, body="Fixes #9 by pinning the key vault API.", closes=[9],
                              comments_list=talk(1)),
                           pr(2, body="", closes=[10], comments_list=talk(2))],
                          [i, dict(i, number=10, url=URL + "/issues/10")]))
        t = next(t for t in b["trains"] if t["id"] == "train-issue-9")
        self.assertEqual(t["joined_by"], [{"item": {"type": "issue", "number": 9},
                                           "via": "closes", "by": {"type": "pr", "number": 1},
                                           "quote": "Fixes #9 by pinning the key vault API."}])
        t2 = next(t for t in b["trains"] if t["id"] == "train-issue-10")
        self.assertIn("closing reference GitHub records", t2["joined_by"][0]["quote_absent"])


class TestStackedPr(unittest.TestCase):

    def test_commits_of_the_base_pr_are_not_the_stacked_prs_own(self):
        base = [c("a", "2026-05-03T00:00:00Z", paths=["avm/res/a/b/main.bicep"]),
                c("b", "2026-05-04T00:00:00Z", paths=["avm/res/a/c/main.bicep"])]
        h = hist({"number": 1, "status": "fetched", "commits": [dict(x) for x in base]},
                 {"number": 2, "status": "fetched", "commits": [dict(x) for x in base] + [
                     c("d", "2026-05-20T00:00:00Z", paths=[".github/workflows/w.yml"])]})
        b = enriched(view([pr(1, created_at="2026-05-02T00:00:00Z", comments_list=talk(1)),
                           pr(2, created_at="2026-05-19T00:00:00Z", comments_list=talk(2))],
                          pr_history=h))
        self.assertEqual(rows(b)["train-pr-2"]["modules"], [])
        tl = link.slice_train(b, "train-pr-2")["conversation"]["timeline"]
        theirs = [i for i in tl if i.get("belongs_to_pr") == 1]
        self.assertEqual(len(theirs), 2)
        self.assertTrue(all("PR #1" in i["not_own_change"] for i in theirs))
        self.assertEqual(rows(b)["train-pr-1"]["modules"], ["avm/res/a/b", "avm/res/a/c"])


class TestContentLandedIn(unittest.TestCase):

    def test_evidence_not_superseded(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
                     c("a", "2026-05-03T00:00:00Z", "fix(ci): retry regional capacity")]},
                 {"number": 2, "status": "fetched", "commits": [
                     c("b", "2026-05-04T00:00:00Z", "fix(ci): retry regional capacity")]},
                 {"number": 3, "status": "fetched", "commits": [
                     c("c", "2026-05-05T00:00:00Z", "fix(ci): reconcile deletion progress")]})
        closed = dict(state="closed", closed_at="2026-05-10T00:00:00Z")
        b = view([pr(1, **closed), merged_pr(2), pr(3, **closed), merged_pr(4)],
                 pr_history=h)
        b["commits"] = [{"sha": "e" * 40, "pr": 4, "message": "ci: consolidate (#4)\n\n"
                         "* fix(ci): reconcile deletion progress", "files": [],
                         "committed_at": "2026-05-20T10:00:00Z"}]
        b = enriched(b)
        p = {x["number"]: x for x in b["prs"]}
        self.assertEqual(p[1]["closed_unmerged"]["content_landed_in"], [
            {"pr": 2, "basis": "same commit subject",
             "commits": [{"sha": "a" * 40, "subject": "fix(ci): retry regional capacity"}]}])
        self.assertNotIn("superseded_by", p[1]["closed_unmerged"])
        self.assertEqual(p[3]["closed_unmerged"]["content_landed_in"][0]["basis"],
                         "named in the merge commit message")

    def test_an_identical_commit_keeps_its_kind_of_work(self):
        # the October shape: a closed PR whose only commit an earlier PR,
        # merged, also holds — content landed there; still plumbing
        shared = c("a", "2026-05-03T00:00:00Z", "fix(ci): classify capacity errors",
                   [".github/workflows/a.yml"])
        h = hist({"number": 1, "status": "fetched", "commits": [dict(shared)]},
                 {"number": 2, "status": "fetched", "commits": [dict(shared)]})
        b = view([merged_pr(1, created_at="2026-05-02T00:00:00Z"),
                  pr(2, state="closed", closed_at="2026-05-10T00:00:00Z",
                     created_at="2026-05-03T00:00:00Z")], pr_history=h)
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        b = enriched(b)
        p2 = next(x for x in b["prs"] if x["number"] == 2)
        self.assertEqual(p2["closed_unmerged"]["content_landed_in"][0]["basis"], "same commit")
        self.assertEqual(rows(b)["train-pr-2"]["group"], "plumbing")


class TestAutomatedReviewerAlias(unittest.TestCase):

    def test_alias_rule(self):
        people = {"agent": {"automation": {"kind": "account_type"}},
                  "agent-reviews[bot]": {"automation": {"kind": "app_login"}},
                  "scanner[bot]": {"automation": {"kind": "app_login"}}}
        act = {n: {"events": [{"event": "review_requested", "at": "2026-05-02T00:00:00Z",
                               "requested": "agent"}]} for n in (1, 2)}
        prs = [{"number": n, "reviews": [
            {"author": "agent-reviews[bot]", "submitted_at": "2026-05-03T00:00:00Z"}]}
            for n in (1, 2)] + [{"number": 3, "reviews": [
                {"author": "scanner[bot]", "submitted_at": "2026-05-03T00:00:00Z"}]}]
        link._alias_automated_reviewers({"prs": prs}, people, act, "2026-05-31T23:59:59Z")
        self.assertEqual(people["agent-reviews[bot]"]["automation"]["same_identity_as"]
                         ["login"], "agent")
        self.assertEqual(people["agent"]["automation"]["reviews_as"], ["agent-reviews[bot]"])
        self.assertNotIn("same_identity_as", people["scanner[bot]"]["automation"])


class TestOwnerIsTheAuthor(unittest.TestCase):

    def test_falls_through_to_the_requested_reviewers(self):
        f = _fact(events=[{"id": 9, "event": "review_requested", "at": "2026-05-26T00:00:00Z",
                           "actor": "gp", "requested": "olga"}])
        b = view([pr(1, author="gp", title="fix(avm/res/x/y): z",
                     labels=["Needs: Module Owner :mega:"],
                     created_at="2026-05-25T00:00:00Z")], pr_activity=f, pr_history=hist())
        b["module_ownership"] = {"collected": True, "events": [], "without_owner": [],
                                 "owners_at_end": {AREA: ["gp"]}}
        o = outcome(enriched(b), "train-pr-1")
        self.assertEqual((o["ball"]["state"], o["ball"]["with"]),
                         ("awaiting reviewer", ["olga"]))
        self.assertTrue(o["owner_response_requested"][0]["author_is_module_owner"])


class TestAutomationThreads(unittest.TestCase):

    def test_counted_not_hidden(self):
        from test_pr_activity import _story_bundle
        f = _fact()
        b = _story_bundle(f)
        for p in b["prs"]:
            for rc in p.get("review_comments") or []:
                rc["author"] = "reviewer-agent[bot]"
        cov = link.slice_train(enriched(b), "train-pr-1")["conversation"]["thread_coverage"]
        self.assertEqual(cov["1"]["by_automation"], {"threads": 1, "resolved": 1})


class TestThemeNetFiles(unittest.TestCase):

    def test_what_a_merged_pr_changed_as_a_whole(self):
        h = hist({"number": 1, "status": "fetched",
                  "commits": [c("a", "2026-05-10T00:00:00Z", paths=[".github/workflows/a.yml"]),
                              c("b", "2026-05-11T00:00:00Z", paths=[".github/workflows/b.yml"])],
                  "net_files": {"head": "b" * 40, "base": "m" * 40, "files": [
                      {"path": ".github/workflows/a.yml", "added": 120, "removed": 100},
                      {"path": ".github/workflows/b.yml", "added": 3, "removed": 1}]}})
        b = view([merged_pr(1, title="chore(ci): preparation only")], pr_history=h)
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        link.build_plumbing_themes(b)
        row = b["plumbing_themes"][0]["prs"][0]
        self.assertEqual({k: row["net_files"][k] for k in ("files", "added", "removed",
                                                           "commits")},
                         {"files": 2, "added": 123, "removed": 101, "commits": 2})
        self.assertEqual(row["net_files"]["top"][0]["path"], ".github/workflows/a.yml")


if __name__ == "__main__":
    unittest.main()
