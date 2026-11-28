"""Data defects the story writers of the October issue reported, and the
contradictions the assembled issue's read found between sections (issue
#97). Each test reproduces its defect before the fix."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
from test_pr_activity import _fact  # noqa: E402
from test_story import URL, comment, pr, review  # noqa: E402
from test_story_groups import merged_pr, rows, talk, view  # noqa: E402
from test_story_rows_modules import hist  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

AREA = "avm/res/x/y"


def own(sha, at, paths, parents=1, **kw):
    c = commit(sha, at, "work", paths, parents=parents)
    c.update(kw)
    return c


def slice_of(b, tid):
    return link.slice_train(b, tid)["conversation"]


def as_of_view(prs, issues=(), **kw):
    b = view(prs, issues, **kw)
    b["meta"]["records_as_of"] = {"instant": "2026-05-31T23:59:59Z"}
    return b


# --- P1 release attribution --------------------------------------------------

class TestReleaseAttribution(unittest.TestCase):

    def mc(self):
        return {"inputs": {"tags_collected": True}, "modules": [{
            "area": AREA, "kind": "resource", "url": "u", "prs": [{"number": 1}, {"number": 2}],
            "changelog": [{"version": "0.5.4", "sources": [{"pr": 2}]},
                          {"version": "0.2.3", "module": AREA + "/kid",
                           "sources": [{"pr": 1}]}],
            "versions": {"changelog": ["0.5.4"], "published": [
                {"version": "0.5.4", "tag": AREA + "/0.5.4",
                 "date": "2026-05-14T00:00:00Z", "commit": "f" * 40, "url": "t1"},
                {"version": "0.2.3", "tag": AREA + "/kid/0.2.3", "module": AREA + "/kid",
                 "date": "2026-05-14T00:00:00Z", "commit": "f" * 40, "url": "t2"}]}}]}

    def test_a_later_pr_declared_the_version(self):
        b = {"module_changes": self.mc(),
             "commits": [{"sha": "e" * 40, "date": "2026-05-10T00:00:00Z",
                          "files": [AREA + "/metadata.json"]}]}
        got = link.train_released(b, {"prs": [1], "commits": ["e" * 40]})
        main = next(r for r in got if r["module"] == AREA)
        self.assertEqual((main["version"], main["tie"], main["declared_by"]),
                         ("0.5.4", "contained", [2]))

    def test_the_child_version_the_train_declared(self):
        b = {"module_changes": self.mc(), "commits": []}
        got = link.train_released(b, {"prs": [1], "commits": ["e" * 40]})
        kid = next(r for r in got if r["module"] == AREA + "/kid")
        self.assertEqual((kid["version"], kid["status"], kid["child_of"]),
                         ("0.2.3", "published", AREA))


class TestReleaseContents(unittest.TestCase):

    def test_commits_between_tags_and_a_republished_child(self):
        b = view([])
        b["module_notes"] = {"tags": {"collected": True, "items": [
            {"module": AREA, "tag": AREA + "/0.5.0", "date": "2026-05-03T00:00:00Z"}]}}
        b["commits"] = [
            {"sha": "a" * 40, "committed_at": "2026-05-02T00:00:00Z", "pr": 7,
             "message": "old", "files": [AREA + "/main.bicep"]},
            {"sha": "b" * 40, "committed_at": "2026-05-09T00:00:00Z", "pr": 8,
             "message": "fix: thing\n\nbody", "files": [AREA + "/main.bicep"]}]
        b["module_changes"] = {"modules": [{"area": AREA, "versions": {"published": [
            {"version": "0.5.1", "tag": AREA + "/0.5.1", "date": "2026-05-10T00:00:00Z"},
            {"version": "0.1.1", "tag": AREA + "/kid/0.1.1", "module": AREA + "/kid",
             "date": "2026-05-10T00:00:00Z"}]}}]}
        link.annotate_release_contents(b)
        pub = b["module_changes"]["modules"][0]["versions"]["published"]
        c = pub[0]["contents"]
        self.assertEqual((c["source"], c["from"], c["prs"]),
                         ("commits_between_tags", {"tag": AREA + "/0.5.0"}, [8]))
        self.assertEqual(c["commits"][0]["subject"], "fix: thing")
        self.assertEqual(pub[1]["note"], "republished with its parent, no change of its own")


# --- P1 merge-from-base commits ----------------------------------------------

class TestOwnChangeOnly(unittest.TestCase):

    def b(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
            own("a" * 40, "2026-05-10T00:00:00Z", ["avm/res/other/z/main.bicep",
                                                   ".github/workflows/x.yml"], parents=0),
            own("b" * 40, "2026-05-11T00:00:00Z", [AREA + "/main.bicep"]),
            own("c" * 40, "2026-05-12T00:00:00Z", ["avm/res/third/w/main.bicep"],
                parents=2)],
            "patches": [{"commit": "a" * 40, "path": ".github/workflows/x.yml",
                         "kind": "shared", "added": ["x"], "removed": []}]})
        return enriched(view([pr(1, title="feat: z", comments_list=talk(1))],
                             pr_history=h))

    def test_net_change_and_modules(self):
        b = self.b()
        c = slice_of(b, "train-pr-1")
        net = c["outcome"]["net_change"]["1"]
        self.assertEqual(net["files_touched"], {"source": [AREA + "/main.bicep"]})
        self.assertNotIn("lines", net)
        self.assertEqual(rows(b)["train-pr-1"]["modules"], [AREA])
        merges = [i for i in c["timeline"] if i.get("not_own_change")]
        self.assertEqual(len(merges), 2)
        self.assertFalse([i for i in merges if i.get("change")])


# --- P1 as-of leaks through bodies -------------------------------------------

class TestBodiesAsOf(unittest.TestCase):

    def test_refs_to_items_created_after_the_window(self):
        p = pr(5, body="Fixes #3 and #9", closes=[3, 9], comments_list=talk(5))
        i3 = {"number": 3, "title": "bug", "body": "", "author": "r", "state": "open",
              "created_at": "2026-05-02T00:00:00Z", "url": URL + "/issues/3",
              "labels": [], "comments_list": []}
        b = enriched(as_of_view([p], [i3]))
        pr5 = next(x for x in b["prs"] if x["number"] == 5)
        self.assertEqual((pr5["closes"], pr5["refs_after_window"]), ([3], [9]))
        self.assertIn("#9", pr5["body_as_of"]["why"])
        t = next(t for t in b["trains"] if 5 in (t.get("prs") or []))
        self.assertNotIn(9, t.get("issues") or [])

    def test_body_edited_after_the_window(self):
        f = _fact(body_edited_at="2026-06-20T00:00:00Z")
        b = enriched(as_of_view([pr(1, comments_list=talk(1))], pr_activity=f))
        self.assertIn("edited 2026-06-20", b["prs"][0]["body_as_of"]["why"])
        got = gather.parse_pr_activity_node({"headRefOid": "h",
                                             "lastEditedAt": "2026-06-20T00:00:00Z"})
        self.assertEqual(got["body_edited_at"], "2026-06-20T00:00:00Z")


# --- P1 automation and deleted accounts never hold the ball ------------------

class TestBall(unittest.TestCase):

    def test_an_automation_re_request_does_not_outrank_a_change_request(self):
        f = _fact(events=[{"id": 9, "event": "review_requested", "at": "2026-05-28T00:00:00Z",
                           "actor": "policy-app[bot]", "requested": "team:owners"}])
        p = pr(1, reviews=[review("bob", "2026-05-25T00:00:00Z", "changes_requested", 31,
                                  "Please rework.")], comments_list=talk(1, "2026-05-20T00:00:00Z"))
        c = slice_of(enriched(view([p], pr_activity=f)), "train-pr-1")
        self.assertEqual((c["outcome"]["ball"]["state"], c["outcome"]["ball"]["rule"]),
                         ("awaiting author", "R4 reviewer moved"))

    def issue_items(self, *last):
        return [{"at": "2026-05-02T00:00:00Z", "kind": "opened", "actor": "r", "url": "u0"},
                {"at": "2026-05-03T00:00:00Z", "kind": "comment", "actor": "r",
                 "text": "Any news?", "url": "u1"}] + list(last)

    def test_a_deleted_accounts_triage_comment_is_not_the_last_event(self):
        items = self.issue_items({"at": "2026-05-04T00:00:00Z", "kind": "comment",
                                  "actor": "ghost", "text": "Thanks @peter", "url": "u2"})
        ball = link._issue_ball({"author": "r"}, items,
                                lambda x: link._cannot_act({}, x))
        self.assertEqual(ball["rule"], "I1 reporter moved")

    def test_a_maintainer_answer_without_a_question(self):
        items = self.issue_items({"at": "2026-05-04T00:00:00Z", "kind": "comment",
                                  "actor": "m", "text": "Pinged internally.", "url": "u2"})
        ball = link._issue_ball({"author": "r"}, items)
        self.assertEqual((ball["state"], ball["with"]), ("awaiting maintainer follow-up", ["m"]))

    def test_an_automated_review_is_on_the_timeline(self):
        p = pr(1, comments_list=talk(1), reviews=[
            review("scanner[bot]", "2026-05-21T00:00:00Z", "commented", 41, "Finding.")])
        c = slice_of(enriched(view([p])), "train-pr-1")
        self.assertEqual([(i["actor"], i["kind"]) for i in c["timeline"]
                          if i["kind"] == "automated_review"],
                         [("scanner[bot]", "automated_review")])


# --- P1 one last human event; the failing-checks rule on a multi-PR train ---

class TestLastHumanEvent(unittest.TestCase):

    def test_a_pr_commit_counts(self):
        p = pr(1, created_at="2026-05-01T00:00:00Z", updated_at="2026-05-02T00:00:00Z",
               comments_list=[comment("bob", "2026-05-02T00:00:00Z", "?", 3),
                              comment("alice", "2026-05-02T01:00:00Z", "!", 4)])
        h = hist({"number": 1, "status": "fetched",
                  "commits": [own("a" * 40, "2026-05-29T00:00:00Z", [AREA + "/main.bicep"])]})
        b = enriched(view([p], pr_history=h))
        t = next(t for t in b["trains"] if t["id"] == "train-pr-1")
        self.assertEqual(t["effort"]["idle_days"], 2)
        self.assertEqual(t["effort"]["last_human_event"]["kind"], "commit")
        self.assertEqual(rows(b)["train-pr-1"]["state"], "in_flight")

    def test_one_quiet_failing_pr_does_not_stall_an_active_train(self):
        from test_pr_activity import check_run
        f = _fact()
        f["prs"][0]["commits"][1]["checks"] = [gather.normalize_check_context(check_run(
            "tests", "FAILURE", started="2026-05-26T00:01:00Z",
            completed="2026-05-26T00:09:00Z"))]
        f["prs"][0]["commits"][1]["committed_at"] = "2026-05-26T00:00:00Z"
        f["prs"].append(dict(copy.deepcopy(f["prs"][0]), number=2))
        f["prs"][1]["commits"][1]["checks"] = [gather.normalize_check_context(check_run(
            "tests", started="2026-05-27T00:01:00Z", completed="2026-05-27T00:09:00Z"))]
        i = {"number": 9, "title": "t", "body": "", "author": "r", "state": "open",
             "created_at": "2026-05-01T00:00:00Z", "url": URL + "/issues/9", "labels": [],
             "comments_list": []}
        prs = [pr(1, closes=[9], comments_list=talk(1, "2026-05-25T10:00:00Z")),
               pr(2, closes=[9], comments_list=talk(2, "2026-05-28T10:00:00Z"))]
        b = enriched(view(prs, [i], pr_activity=f))
        self.assertEqual(rows(b)["train-issue-9"]["state"], "in_flight")


class TestStatusReason(unittest.TestCase):

    def test_a_writer_can_say_why(self):
        b = enriched(view([pr(1, created_at="2026-05-19T00:00:00Z", comments_list=talk(1))]))
        r = rows(b)["train-pr-1"]["status_reason"]
        self.assertTrue(r.startswith("opened 12 days before the window's end"), r)
        self.assertIn("no review requested or given", r)


# --- P1 one review state per PR ----------------------------------------------

class TestReviewState(unittest.TestCase):

    def state(self, p, **kw):
        b = enriched(view([p], **kw))
        return b["prs"][0]["review_state"], b

    def test_states(self):
        self.assertEqual(self.state(pr(1))[0]["state"], "none")
        rs, _ = self.state(pr(1, reviews=[review("scan[bot]", "2026-05-03T00:00:00Z",
                                                 "commented", 5, "x")]))
        self.assertEqual(rs["state"], "automated_only")
        f = _fact(events=[{"id": 9, "event": "review_requested", "at": "2026-05-04T00:00:00Z",
                           "actor": "alice", "requested": "bob"}])
        rs, _ = self.state(pr(1), pr_activity=f)
        self.assertEqual((rs["state"], rs["requested"]), ("requested", ["bob"]))
        rs, _ = self.state(pr(1, reviews=[review("bob", "2026-05-05T00:00:00Z",
                                                 "changes_requested", 6, "no")]))
        self.assertEqual(rs["state"], "changes_requested")

    def test_approval_before_the_latest_push_is_stale_and_not_ready(self):
        h = hist({"number": 1, "status": "fetched",
                  "commits": [own("a" * 40, "2026-05-20T00:00:00Z", [AREA + "/main.bicep"])]})
        p = pr(1, reviews=[review("bob", "2026-05-05T00:00:00Z", "approved", 6)])
        rs, b = self.state(p, pr_history=h)
        self.assertEqual((rs["state"], rs["last_push"]),
                         ("approved_stale", "2026-05-20T00:00:00Z"))
        self.assertFalse(link.approved_pr(b["prs"][0]))
        rs, b = self.state(p, pr_history=hist({"number": 1, "status": "fetched",
                                               "commits": [own("a" * 40, "2026-05-02T00:00:00Z",
                                                               [AREA + "/main.bicep"])]}))
        self.assertEqual(rs["state"], "approved")
        self.assertTrue(link.approved_pr(b["prs"][0]))
        out = slice_of(b, "train-pr-1")["outcome"]["items"][0]
        self.assertEqual(out["review_state"]["state"], "approved")


# --- P1 themes -----------------------------------------------------------------

class TestThemes(unittest.TestCase):

    def test_first_matching_theme_per_path(self):
        paths = [".github/workflows/a.yml", ".github/workflows/b.yml", ".github/dependabot.yml"]
        self.assertEqual(link.plumbing_theme({"author": "a", "title": "ci"}, paths)[0],
                         "ci_workflows")
        mod = ["utilities/e2e-template-assets/module-specific/x/a.bicep",
               "utilities/e2e-template-assets/module-specific/x/b.ps1",
               AREA + "/tests/e2e/defaults/main.test.bicep"]
        self.assertEqual(link.plumbing_theme({"author": "a", "title": "t"}, mod)[0],
                         "test_environment")

    def test_a_sweep_that_changes_module_source_is_not_plumbing(self):
        self.assertFalse(link._plumbing_row({"plumbing": False, "sweep": {"areas": 58},
                                             "substantive_areas": [AREA]}))
        self.assertTrue(link._plumbing_row({"plumbing": False, "sweep": {"areas": 6},
                                            "substantive_areas": []}))

    def test_automation_prs_carried_into_the_window(self):
        old = pr(4, title="build(deps): bump x from 1 to 2", author="dependabot[bot]",
                 created_at="2026-04-01T00:00:00Z", updated_at="2026-04-01T00:00:00Z")
        b = enriched(view([old, pr(5, comments_list=talk(5))]))
        th = {t["id"]: t for t in b.get("plumbing_themes") or []}
        row = th["dependency_updates"]["prs"][0]
        self.assertEqual((row["number"], row["carried_over"]), (4, True))


# --- P1 breaking candidate: the commit that made the move ---------------------

class TestMovedChild(unittest.TestCase):

    def test_candidate_source_is_the_move(self):
        m = {"area": AREA, "_markers": [], "_children": {AREA + "/new/kid": {"events": [
            {"change": "rename", "role": "new", "other": AREA + "/kid/main.bicep",
             "source": {"commit": "a" * 40, "pr": 1}},
            {"change": "modify", "role": None, "source": {"commit": "b" * 40, "pr": 2}}]}}}
        _rows, cands, _dep = link._children_summary(None, m)
        self.assertEqual(cands[0]["sources"], [{"commit": "a" * 40, "pr": 1}])


class TestChangelogExcerptWhole(unittest.TestCase):

    def test_breaking_section_is_kept(self):
        lines = ["## 1.0.0", "", "### Changes", ""] + ["- c{}".format(i) for i in range(25)] \
            + ["", "### Breaking Changes", "", "- b"]
        got = link._net_lines([{"commit": "a", "path": "CHANGELOG.md", "kind": "changelog",
                                "added": lines, "removed": []}])
        self.assertIn("### Breaking Changes", got["CHANGELOG.md"]["net_added"])
        self.assertGreaterEqual(gather.PR_HISTORY_PATCH_LINES, 400)


# --- P2 ------------------------------------------------------------------------

class TestP2(unittest.TestCase):

    def test_fork_status_is_three_valued(self):
        b = enriched(view([pr(1, head_repo="o/r"), pr(2, head_repo="x/r"),
                           pr(3, head_repo=None), pr(4)]))
        self.assertEqual([p["fork_status"] for p in b["prs"]],
                         ["not_fork", "fork", "deleted_fork", "unknown"])

    def test_superseded_or_not_stated(self):
        p = pr(1, state="closed", closed_at="2026-05-10T00:00:00Z", comments_list=[
            comment("dependabot[bot]", "2026-05-10T00:00:00Z", "Superseded by #7.", 3)])
        q = pr(2, state="closed", closed_at="2026-05-10T00:00:00Z")
        b = enriched(view([p, q]))
        self.assertEqual(b["prs"][0]["closed_unmerged"]["superseded_by"], 7)
        self.assertEqual(b["prs"][1]["closed_unmerged"], {"close_reason": "not stated"})

    def test_a_new_module_from_the_added_entry_file(self):
        raw = ("\x1e" + "a" * 40 + "\x1fp1\x1fA\x1f2026-05-01T00:00:00Z\x1f"
               "2026-05-01T00:00:00Z\x1ffeat: new\x1f\x1d\n"
               "A\tavm/utl/general/naming/main.bicep\nM\tREADME.md\n")
        got = gather.parse_pr_commits(raw)[0]
        self.assertEqual((got["paths"], got["added"]),
                         (["avm/utl/general/naming/main.bicep", "README.md"],
                          ["avm/utl/general/naming/main.bicep"]))
        h = hist({"number": 1, "status": "fetched", "commits": [got]})
        b = enriched(view([pr(1, title="feat: add naming utility", comments_list=talk(1))],
                          pr_history=h))
        self.assertEqual(b["prs"][0]["proposes_new_module"], ["avm/utl/general/naming"])
        self.assertEqual(rows(b)["train-pr-1"]["new_module_proposed"],
                         ["avm/utl/general/naming"])

    def test_long_descriptions_are_whole(self):
        self.assertGreaterEqual(link.STORY_ITEM_CAP, 13629)

    def test_a_commit_only_the_checks_read_and_a_reused_branch(self):
        f = _fact()
        h = hist({"number": 1, "status": "fetched", "commits": [
            own("c" * 40, "2026-04-20T00:00:00Z", [AREA + "/main.bicep"])]})
        c = slice_of(enriched(view([pr(1, comments_list=talk(1))], pr_activity=f,
                                   pr_history=h)), "train-pr-1")
        checks = [i for i in c["timeline"] if i["kind"] == "checks"]
        self.assertTrue(all(i.get("commit_not_in_history") for i in checks))
        mine = next(i for i in c["timeline"] if i.get("sha") == "c" * 40)
        self.assertTrue(mine["before_pr_opened"])

    def test_one_thread_status_sentence(self):
        from test_pr_activity import _story_bundle
        from test_story_substance import conversation
        c = conversation(_story_bundle(_fact()))
        t = next(t for t in c["threads"] if t["kind"] == "review_thread")
        self.assertIn("after the window's end; as of the end: ", t["status_note"])

    def test_theme_rows_carry_whole_bodies_and_commit_bodies(self):
        h = hist({"number": 1, "status": "fetched", "commits": [
            own("a" * 40, "2026-05-10T00:00:00Z", [".github/workflows/a.yml"],
                body="Why: the cache.")]})
        b = view([merged_pr(1, title="ci: cache", body="x" * 900)], pr_history=h)
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True}]}
        link.build_plumbing_themes(b)
        th = b["plumbing_themes"][0]
        self.assertEqual((len(th["prs"][0]["body"]), th["prs"][0]["body_length"]), (900, 900))
        self.assertEqual(th["subjects"][0]["body"], "Why: the cache.")


class TestLabelsAndActors(unittest.TestCase):

    def test_label_history_reads_labels_as_of_the_end(self):
        raw = [{"event": "labeled", "id": 1, "created_at": "2026-06-02T00:00:00Z",
                "actor": {"login": "policy", "type": "Bot"}, "label": {"name": "Stale"}},
               {"event": "unlabeled", "id": 2, "created_at": "2026-06-03T00:00:00Z",
                "actor": {"login": "m", "type": "User"}, "label": {"name": "Needs: Triage"}}]
        evs = gather.parse_timeline_pr_events(raw, gather.PR_LABEL_EVENTS)
        self.assertEqual([(e["event"], e["label"], e.get("actor_type")) for e in evs],
                         [("labeled", "Stale", "Bot"), ("unlabeled", "Needs: Triage", None)])
        self.assertEqual(gather.parse_timeline_pr_events(raw), [])
        b = enriched(view([pr(1, labels=["Stale"], label_events=evs,
                              comments_list=talk(1))]))
        self.assertEqual(b["prs"][0]["labels"], ["Needs: Triage"])
        self.assertEqual(b["prs"][0]["labels_as_of"]["changed_after"],
                         ["Needs: Triage", "Stale"])

    def test_an_event_actor_of_type_bot_is_automation(self):
        f = _fact(events=[{"id": 9, "event": "review_requested", "at": "2026-05-04T00:00:00Z",
                           "actor": "reviewer-agent", "actor_type": "Bot",
                           "requested": "bob"}])
        b = enriched(view([pr(1, comments_list=talk(1))], pr_activity=f))
        self.assertEqual(b["people"]["reviewer-agent"]["automation"]["kind"],
                         "account_type")


if __name__ == "__main__":
    unittest.main()
