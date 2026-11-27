"""What the owner's readers asked the stories to say (issue #97): linked
items outside the window with their state, the module's owner, what a
force-push replaced, a PR's net change per file, sibling PRs the record
links, who a commit's author is, a failed pipeline's run, and the brief's
closing / versioning rules. Each test reproduces its gap before the fix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
from test_module_ownership import Blobs, meta  # noqa: E402
from test_story import URL, comment, pr  # noqa: E402
from test_story_groups import talk, view  # noqa: E402
from test_story_rows_modules import hist  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

HERE = os.path.dirname(__file__)
AREA = "avm/res/x/y"


def outcome(b, tid):
    return link.slice_train(b, tid)["conversation"]["outcome"]


def timeline(b, tid):
    return link.slice_train(b, tid)["conversation"]["timeline"]


class TestLinkedItems(unittest.TestCase):

    def test_numbers_and_one_batched_request(self):
        prs = [pr(1, body="Fixes #9, see #12 and https://github.com/o/r/pull/14",
                  closes=[9], comments_list=[comment("bob", "2026-05-02T00:00:00Z",
                                                     "Like #1? <!-- #99 -->", 3)])]
        self.assertEqual(gather.linked_numbers(prs, [], "o/r"), [9, 12, 14])
        asked = []

        def graphql(q, variables, errors=None):
            asked.append(q)
            return {"repository": {
                "n9": {"__typename": "Issue", "number": 9, "title": "Bug", "state": "CLOSED",
                       "stateReason": "COMPLETED", "createdAt": "2026-04-01T00:00:00Z",
                       "closedAt": "2026-06-03T00:00:00Z", "url": "u9",
                       "author": {"login": "r"}},
                "n12": {"__typename": "PullRequest", "number": 12, "title": "Fix",
                        "state": "MERGED", "createdAt": "2026-04-02T00:00:00Z",
                        "closedAt": "2026-05-03T00:00:00Z",
                        "mergedAt": "2026-05-03T00:00:00Z", "merged": True, "url": "u12"},
                "n14": None}}
        got = gather.collect_linked_items(prs, [], graphql, "o", "r")
        self.assertEqual(len(asked), 1)
        self.assertEqual([i["number"] for i in got["items"]], [9, 12])
        self.assertEqual(got["missing"], [14])
        return got

    def test_the_outcome_names_them_with_their_state_as_of_the_end(self):
        fact = self.test_numbers_and_one_batched_request()
        p = pr(1, body="Fixes #9, see #12 and https://github.com/o/r/pull/14",
               closes=[9], comments_list=talk(1))
        b = enriched(view([p], linked_items=fact))
        tid = next(t["id"] for t in b["trains"] if 1 in (t.get("prs") or []))
        linked = {r["number"]: r for r in outcome(b, tid)["linked"]}
        self.assertEqual((linked[9]["title"], linked[9]["state"],
                          linked[9]["state_when_gathered"]), ("Bug", "open", "closed"))
        self.assertEqual((linked[12]["type"], linked[12]["state"]), ("pr", "merged"))
        self.assertEqual(linked[14]["not_read"], "not found")

    def test_stored_validated_and_merged(self):
        import extract
        import validate
        from test_pr_activity import _folded
        conn = _folded(None)
        for frm, to, items in (("2026-05-01", "2026-05-31",
                                [{"number": 9, "type": "issue", "state": "open"}]),
                               ("2026-06-01", "2026-06-30",
                                [{"number": 9, "type": "issue", "state": "closed"},
                                 {"number": 12, "type": "pr", "state": "merged"}])):
            gather.fold_bundle(conn, {
                "meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                         "period": {"from": frm, "to": to}, "ref_date": to,
                         "base_branch": "main", "branches": ["main"], "clone_sha": None},
                "prs": [], "issues": [], "commits": [],
                "linked_items": {"collected": True, "gathered_at": to + "T06:00:00Z",
                                 "items": items, "missing": [], "requests": 1}})
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c for c in report.checks if not c["ok"]], [])
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(v["linked_items"]["items"][0]["state"], "open")
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-06-30")
        self.assertEqual([(i["number"], i["state"]) for i in v["linked_items"]["items"]],
                         [(9, "closed"), (12, "merged")])
        self.assertIsNotNone(validate._fact_shape_problem(
            "linked_items", {"collected": True, "items": [{"number": "9"}], "missing": []}))


class TestModuleOwner(unittest.TestCase):

    def test_owners_at_the_window_end(self):
        got = gather.collect_module_ownership(
            [], [], [], [], Blobs({("e" * 40, AREA + "/metadata.json"): meta(["olga"])}),
            [AREA + "/metadata.json", AREA + "/main.bicep"], "2026-05-01", "2026-05-31",
            start_files=[], end_rev="e" * 40)
        self.assertEqual(got["owners_at_end"], {AREA: ["olga"]})
        b = view([pr(1, title="fix(avm/res/x/y): z", labels=["Needs: Module Owner :mega:"],
                     comments_list=talk(1) + [comment("olga", "2026-05-26T00:00:00Z",
                                                      "Looking.", 9)])])
        b["module_ownership"] = got
        b["people"] = {"olga": {"is_bot": False, "name": "Olga"}, "alice": {}, "bob": {}}
        b = enriched(b)
        self.assertEqual(b["people"]["olga"]["module_owner_of"], [AREA])
        s = link.slice_train(b, "train-pr-1")
        self.assertEqual(s["participants"]["olga"]["module_owner_of"], [AREA])
        req = [e for e in b["module_ownership"]["events"]
               if e["kind"] == "owner_response_requested"][0]
        self.assertEqual((req["owners"], req["owners_source"]), (["olga"], "owner_file"))


class TestForcePushReplaced(unittest.TestCase):

    def test_the_timeline_says_what_was_replaced(self):
        h = hist({"number": 1, "status": "fetched",
                  "commits": [commit("b" * 40, "2026-05-12T00:00:00Z", "rework", ["a.bicep"])],
                  "replaced": [{"at": "2026-05-11T00:00:00Z", "actor": "alice",
                                "before": "1" * 40, "after": "2" * 40, "status": "read",
                                "commits": [{"sha": "a" * 40, "subject": "first try",
                                             "files": 2}]},
                               {"at": "2026-05-13T00:00:00Z", "actor": "alice",
                                "status": "unavailable",
                                "reason": "the push's before commit is not recorded"}]})
        tl = timeline(enriched(view([pr(1, comments_list=talk(1))], pr_history=h)),
                      "train-pr-1")
        pushes = [i for i in tl if i["kind"] == "head_ref_force_pushed"]
        self.assertEqual(pushes[0]["replaced"]["summary"],
                         "force-push on 2026-05-11 replaced 1 commit(s): first try")
        self.assertEqual(pushes[1]["replaced"]["status"], "unavailable")


class TestNetFiles(unittest.TestCase):

    def test_gather_reads_the_diff_from_the_merge_base(self):
        calls = []

        def run(argv):
            calls.append(argv[3:5])
            if argv[3] == "merge-base":
                return "m" * 40 + "\n"
            return "5\t2\ta.bicep\n-\t-\tlogo.png\n"
        row = {"commits": [{"sha": "a" * 40, "committed_at": "2026-05-02T00:00:00Z"},
                           {"sha": "b" * 40, "committed_at": "2026-06-02T00:00:00Z"}]}
        gather.pr_net_files(row, "/c", "HEAD", run, as_of="2026-05-31T23:59:59Z")
        self.assertEqual(row["net_files"]["head"], "a" * 40)       # as of the end
        self.assertEqual(row["net_files"]["files"],
                         [{"path": "a.bicep", "added": 5, "removed": 2},
                          {"path": "logo.png", "added": None, "removed": None}])

    def test_outcome_lists_files_more_than_one_commit_touched(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
            commit("a" * 40, "2026-05-10T00:00:00Z", "one", [AREA + "/main.bicep", "x.md"]),
            commit("b" * 40, "2026-05-11T00:00:00Z", "two", [AREA + "/main.bicep"])],
            "net_files": {"head": "b" * 40, "base": "m" * 40, "files": [
                {"path": AREA + "/main.bicep", "added": 3, "removed": 1},
                {"path": "x.md", "added": 1, "removed": 0}]}})
        o = outcome(enriched(view([pr(1, comments_list=talk(1))], pr_history=h)),
                    "train-pr-1")
        self.assertEqual(o["net_change"]["1"]["net_files"],
                         [{"path": AREA + "/main.bicep", "added": 3, "removed": 1,
                           "commits": 2}])


class TestSiblingPrs(unittest.TestCase):

    def test_only_explicit_references(self):
        a = pr(1, created_at="2026-05-10T00:00:00Z", body="Companion of #2.",
               comments_list=talk(1))
        b2 = pr(2, created_at="2026-05-11T00:00:00Z", body="x", comments_list=talk(2))
        c = pr(3, created_at="2026-05-11T00:00:00Z", body="unrelated", comments_list=talk(3))
        o = outcome(enriched(view([a, b2, c])), "train-pr-1")
        self.assertEqual([(r["number"], r["basis"]) for r in o["items"][0]["related"]],
                         [(2, "#1 names #2")])


class TestCommitAuthor(unittest.TestCase):

    def test_resolved_login_and_another_author(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
            commit("a" * 40, "2026-05-10T00:00:00Z", "one", [AREA + "/main.bicep"]),
            dict(commit("b" * 40, "2026-05-11T00:00:00Z", "two", [AREA + "/main.bicep"]),
                 author="Nobody Known")]})
        b = view([pr(1, comments_list=talk(1))], pr_history=h)
        b["commits"] = [{"sha": "c" * 40, "author": "carol", "author_name": "Alice Example",
                         "login_resolved": True, "files": [], "message": "m"}]
        tl = timeline(enriched(b), "train-pr-1")
        mine = {i["sha"]: i for i in tl if i.get("source") == "pr_head"}
        self.assertEqual((mine["a" * 40]["author_login"], mine["a" * 40]["authored_by_other"]),
                         ("carol", True))
        self.assertEqual((mine["b" * 40]["author_name"], mine["b" * 40]["author_login"]),
                         ("Nobody Known", None))


class TestFailedPipeline(unittest.TestCase):

    def test_run_from_the_bot_issue(self):
        i = {"title": "[Failed pipeline] avm.res.devices.iot-hub",
             "body": "Failed run: https://github.com/o/r/actions/runs/37128115526"}
        self.assertEqual(link.failed_pipeline(i), {
            "workflow": "avm.res.devices.iot-hub", "module": "avm/res/devices/iot-hub",
            "run_url": "https://github.com/o/r/actions/runs/37128115526",
            "run_id": 37128115526})
        self.assertIsNone(link.failed_pipeline({"title": "bug", "body": ""}))


class TestBrief(unittest.TestCase):

    def test_closed_story_and_versioning_rules(self):
        with open(os.path.join(HERE, "reference", "report-sections.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("*A closed story*", doc)
        self.assertIn("changelog declares it breaking; the version moved from", doc)


if __name__ == "__main__":
    unittest.main()
