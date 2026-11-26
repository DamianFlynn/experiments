"""The story layer (issue #97): the discussion signal per train, the story
selection, the story material in a train's slice, the PR-internal history
gathered from refs/pull/<N>/head, the narrative evidence check, and two
module-layer follow-ups (release notes text, version declared vs published).

The PR-history tests drive REAL git against throwaway local `file://`
repositories (no network, no global / system git config).
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
from _quiet import quiet  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://github.com/o/r"

GIT_TEST_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def git(*args, cwd=None, date=None, author_date=None):
    env = dict(os.environ)
    env.update(GIT_TEST_ENV)
    if date:
        env["GIT_COMMITTER_DATE"] = f"{date}T12:00:00Z"
        env["GIT_AUTHOR_DATE"] = f"{author_date or date}T12:00:00Z"
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "commit.gpgsign=false",
         "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


# --- builders ---------------------------------------------------------------

def comment(author, at, body, n):
    return {"author": author, "created_at": at, "body": body, "id": n,
            "url": f"{URL}/pull/1#issuecomment-{n}"}


def review(author, at, state, n, body=""):
    return {"author": author, "submitted_at": at, "state": state, "body": body,
            "id": n, "url": f"{URL}/pull/1#pullrequestreview-{n}"}


def pr(number=1, **kw):
    out = {"number": number, "title": f"feat: thing {number}", "author": "alice",
           "url": f"{URL}/pull/{number}", "state": "open", "merged": False,
           "base": "main", "created_at": "2026-05-01T10:00:00Z",
           "updated_at": "2026-05-20T10:00:00Z", "body": "Adds the thing.",
           "closes": [], "crossref_issues": [], "labels": []}
    out.update(kw)
    return out


def bundle(prs, issues=(), **kw):
    b = {"meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                  "to": "2026-05-31", "period": {"from": "2026-05-01",
                                                 "to": "2026-05-31"},
                  "ref_date": "2026-05-31", "base_branch": "main"},
         "prs": list(prs), "issues": list(issues), "commits": [],
         "milestones": [], "releases": [], "code_graph": {"areas": []}}
    b.update(kw)
    return b


def debated_pr(number=1, **kw):
    """A PR with a back-and-forth: three human comments by two people, a
    change request, an empty review-thread wrapper, a bot comment."""
    return pr(number, comments_list=[
        comment("bob", "2026-05-02T10:00:00Z", "Why not reuse the shared type?", 11),
        comment("alice", "2026-05-03T10:00:00Z", "Because it lacks the field.", 12),
        comment("github-actions[bot]", "2026-05-03T10:05:00Z", "CI passed", 13),
        comment("bob", "2026-05-25T10:00:00Z", "OK, makes sense now.", 14)],
        review_comments=[comment("bob", "2026-05-04T09:00:00Z",
                                 "Rename this parameter.", 21)],
        reviews=[review("bob", "2026-05-04T09:00:00Z", "commented", 31),
                 review("bob", "2026-05-04T09:01:00Z", "changes_requested", 32,
                        "Please split the module."),
                 review("alice", "2026-05-05T09:00:00Z", "commented", 33,
                        "Done, see the new commit.")], **kw)


def enriched(b):
    return link.enrich(copy.deepcopy(b))


def train_of(b, tid=None):
    return next(t for t in b["trains"] if tid is None or t["id"] == tid)


# --- 1. the discussion signal ------------------------------------------------

class TestDiscussion(unittest.TestCase):

    def test_counts_humans_and_leaves_bots_and_thread_wrappers_out(self):
        d = train_of(enriched(bundle([debated_pr()])))["discussion"]
        self.assertEqual(d["comments"], 3)
        self.assertEqual(d["review_comments"], 1)
        self.assertEqual(d["bot_comments"], 1)
        self.assertEqual(d["participants"], 2)
        # the empty `commented` wrapper and the author's own reply do not count
        self.assertEqual(d["reviews"], {"approved": 0, "changes_requested": 1,
                                        "commented": 0, "dismissed": 0})
        self.assertEqual(d["change_requests"], 1)
        self.assertNotIn("change_requests_reworked", d)   # no PR history
        self.assertEqual(d["first_response_days"], 1.0)
        self.assertEqual(d["in_window"], 5)
        self.assertEqual(d["longest_silence"]["days"], 20.0)
        self.assertEqual(d["longest_silence"]["to"], "2026-05-25T10:00:00Z")
        self.assertEqual(d["longest_silence"]["resumed_at"],
                         f"{URL}/pull/1#issuecomment-14")
        self.assertGreater(d["text_chars"], 0)

    def test_omitted_without_human_conversation(self):
        b = enriched(bundle([pr(1, comments_list=[
            comment("dependabot[bot]", "2026-05-02T10:00:00Z", "bump", 1)])]))
        self.assertNotIn("discussion", train_of(b))
        self.assertNotIn("stories", b)

    def test_in_window_counts_only_the_window(self):
        p = debated_pr(created_at="2026-04-01T10:00:00Z")
        p["comments_list"][0]["created_at"] = "2026-04-02T10:00:00Z"
        d = train_of(enriched(bundle([p])))["discussion"]
        self.assertEqual(d["in_window"], 4)

    def test_rework_after_change_request_needs_the_pr_history(self):
        history = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched", "commits": [
                {"sha": "a" * 40, "parents": 1, "author": "Alice",
                 "authored_at": "2026-05-04T12:00:00Z",
                 "committed_at": "2026-05-04T12:00:00Z", "subject": "split",
                 "files": 2}]}]}
        d = train_of(enriched(bundle([debated_pr()], pr_history=history)))[
            "discussion"]
        self.assertEqual(d["change_requests_reworked"], 1)

    def test_pr_commits_after_the_window_end_do_not_count(self):
        history = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched", "commits": [
                {"sha": "a" * 40, "parents": 1, "author": "Alice",
                 "authored_at": "2026-06-04T12:00:00Z",
                 "committed_at": "2026-06-04T12:00:00Z", "subject": "late",
                 "files": 1}]}]}
        b = enriched(bundle([debated_pr()], pr_history=history))
        self.assertEqual(train_of(b)["discussion"]["change_requests_reworked"], 0)
        s = link.slice_train(b, train_of(b)["id"])
        self.assertFalse([i for i in s["conversation"]["timeline"]
                          if i["kind"] == "commit"])


# --- 2. story selection ------------------------------------------------------

class TestStorySelection(unittest.TestCase):

    def test_selected_train_gets_the_fields_and_reasons(self):
        b = enriched(bundle([debated_pr()]))
        t = train_of(b)
        self.assertTrue(t["story"])
        self.assertEqual(t["story_rank"], 1)
        # 4 conversation + 5 in window + 2 (second person) + 3 (change
        # request) + 2 (silence > 14 days) + 0 volume + 3 (feature) + 1
        # (in flight)
        self.assertEqual(t["story_score"], 20.0)
        self.assertIn("1 change request(s)", t["story_reasons"])
        self.assertIn("in flight", t["story_reasons"])
        self.assertEqual(b["stories"]["selected"],
                         [{"id": t["id"], "rank": 1, "score": 20.0}])
        self.assertEqual(b["stories"]["more"], [])
        self.assertEqual(t["tier"], "deep")       # tier is untouched

    def test_one_person_talking_to_themselves_is_no_story(self):
        p = pr(1, comments_list=[comment("alice", f"2026-05-0{i}T10:00:00Z",
                                         "note to self", i) for i in range(2, 9)])
        b = enriched(bundle([p]))
        self.assertIn("discussion", train_of(b))
        self.assertNotIn("story", train_of(b))
        self.assertNotIn("stories", b)

    def test_too_few_exchanges_is_no_story(self):
        p = pr(1, comments_list=[
            comment("bob", "2026-05-02T10:00:00Z", "LGTM", 1),
            comment("alice", "2026-05-02T11:00:00Z", "thanks", 2)])
        self.assertNotIn("story", train_of(enriched(bundle([p]))))

    def test_cap_and_more(self):
        prs = [debated_pr(number=n, url=f"{URL}/pull/{n}",
                          title=f"feat: thing {n}") for n in range(1, 12)]
        b = enriched(bundle(prs))
        sel = b["stories"]["selected"]
        self.assertEqual(len(sel), link.STORY_TOP_N)
        self.assertEqual([s["id"] for s in sel],
                         sorted(s["id"] for s in sel))   # equal scores: by id
        self.assertEqual(len(b["stories"]["more"]), 11 - link.STORY_TOP_N)
        self.assertEqual(sum(1 for t in b["trains"] if t.get("story")),
                         link.STORY_TOP_N)

    def test_plumbing_sweep_needs_substantial_discussion(self):
        t = {"id": "t", "kind": "chore", "prs": [1],
             "code_areas": [f"a{i}" for i in range(20)]}
        self.assertTrue(link._train_plumbing(t, None))
        mc = {"prs": [{"number": 1, "plumbing": False}]}
        self.assertFalse(link._train_plumbing(t, mc))
        mc = {"prs": [{"number": 1, "plumbing": True}]}
        self.assertTrue(link._train_plumbing(dict(t, kind="feature"), mc))
        # the same modest debate (3 comments in the window, 2 people: score 8) is a story
        # on a one-module change and not on a sweep over 12 modules
        def sweep(n_modules):
            p = pr(1, title="chore: sweep", state="closed", merged=True,
                   merged_at="2026-05-06T10:00:00Z", created_at="2026-05-01T00:00:00Z",
                   comments_list=[
                       comment("bob", "2026-05-02T10:00:00Z", "Is this safe?", 1),
                       comment("alice", "2026-05-02T11:00:00Z", "Yes, docs only.", 2),
                       comment("bob", "2026-05-02T12:00:00Z", "OK.", 3)])
            files = [f"m{i}/main.bicep" for i in range(n_modules)]
            b = bundle([p], commits=[{"sha": "c1", "date": "2026-05-06",
                                      "message": "chore: sweep (#1)",
                                      "files": files, "author": "alice"}],
                       code_graph={"areas": [{"id": f.split("/")[0], "paths": [f]}
                                             for f in files]})
            return train_of(enriched(b))
        tr = sweep(12)
        self.assertGreater(len(tr["code_areas"]), 10)
        self.assertIn("discussion", tr)
        self.assertNotIn("story", tr)
        self.assertEqual(sweep(1)["story_score"], 8.0)

    def test_new_module_adds_relevance(self):
        t = {"id": "t", "prs": [5]}
        mc = {"modules": [{"area": "avm/res/x/y", "status": "new",
                           "prs": [{"number": 5}]}]}
        pts, reasons = link._train_module_relevance(t, mc)
        self.assertEqual(pts, 5)
        self.assertEqual(reasons, ["new module avm/res/x/y"])


# --- 3. the story material ---------------------------------------------------

class TestStoryMaterial(unittest.TestCase):

    def slice_(self, b, tid=None):
        b = enriched(b)
        return link.slice_train(b, tid or train_of(b)["id"])

    def test_one_chronological_timeline_without_bots(self):
        c = self.slice_(bundle([debated_pr()]))["conversation"]
        kinds = [(i["kind"], i.get("actor")) for i in c["timeline"]]
        self.assertEqual(kinds, [
            ("opened", "alice"), ("comment", "bob"), ("comment", "alice"),
            ("review_comment", "bob"), ("review", "bob"), ("review", "alice"),
            ("comment", "bob")])
        self.assertEqual(c["timeline"][0]["title"], "feat: thing 1")
        self.assertEqual(c["timeline"][0]["text"], "Adds the thing.")
        self.assertEqual(c["timeline"][4]["state"], "changes_requested")
        self.assertEqual(c["bots"], {"github-actions[bot]": 1})
        times = [i["at"] for i in c["timeline"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(c["as_of"], "2026-05-31T23:59:59Z")
        self.assertEqual(c["not_gathered"], list(link.STORY_NOT_GATHERED))
        self.assertEqual(c["pr_history"], {"1": "not_gathered"})

    def test_lifecycle_merge_and_closed_items(self):
        p = debated_pr(state="closed", merged=True,
                       merged_at="2026-05-26T10:00:00Z", merged_by="carol",
                       lifecycle=[{"event": "reopened", "actor": "bob", "id": 9,
                                   "created_at": "2026-05-24T10:00:00Z",
                                   "url": "https://api.github.com/x/9"}])
        q = pr(2, state="closed", closed_at="2026-05-10T10:00:00Z",
               created_at="2026-05-08T10:00:00Z", closes=[], merged=False)
        c = self.slice_(bundle([p]))["conversation"]
        reopened = next(i for i in c["timeline"] if i["kind"] == "reopened")
        self.assertEqual(reopened["url"], f"{URL}/pull/1#event-9")
        merged = c["timeline"][-1]
        self.assertEqual((merged["kind"], merged["actor"]), ("merged", "carol"))
        c2 = self.slice_(bundle([q]))["conversation"]
        self.assertEqual([i["kind"] for i in c2["timeline"]], ["opened", "closed"])

    def test_turning_points_by_rule(self):
        history = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched", "commits": [
                {"sha": "a" * 40, "parents": 1, "author": "Alice",
                 "authored_at": "2026-05-01T12:00:00Z",
                 "committed_at": "2026-05-04T12:00:00Z", "subject": "split",
                 "files": 2}]}]}
        p = debated_pr(lifecycle=[{"event": "reopened", "actor": "bob", "id": 9,
                                   "created_at": "2026-05-24T10:00:00Z"}])
        c = self.slice_(bundle([p], pr_history=history))["conversation"]
        rules = [(t["rule"], c["timeline"][t["index"]]["kind"])
                 for t in c["turning_points"]]
        self.assertEqual(rules, [
            ("change_request", "review"),
            ("rework_after_change_request", "commit"),
            ("history_rewritten", "commit"),
            ("silence_then_activity", "reopened"),
            ("reopened", "reopened")])
        commit = next(i for i in c["timeline"] if i["kind"] == "commit")
        self.assertEqual(commit["url"], f"{URL}/pull/1/commits/{'a' * 40}")
        self.assertEqual(commit["source"], "pr_head")
        self.assertEqual(c["pr_history"], {"1": "fetched"})

    def test_still_silent_and_new_attempt(self):
        first = pr(1, state="closed", closed_at="2026-05-02T10:00:00Z",
                   closes=[7], comments_list=[comment(
                       "bob", "2026-05-01T12:00:00Z", "not like this", 1)])
        second = pr(2, created_at="2026-05-03T10:00:00Z", closes=[7],
                    url=f"{URL}/pull/2")
        issue = {"number": 7, "title": "Need X", "author": "carol",
                 "url": f"{URL}/issues/7", "state": "open",
                 "created_at": "2026-04-30T10:00:00Z", "body": "Please add X",
                 "comments_list": [], "labels": []}
        b = enriched(bundle([first, second], [issue]))
        t = train_of(b)
        c = link.slice_train(b, t["id"])["conversation"]
        rules = [tp["rule"] for tp in c["turning_points"]]
        self.assertIn("new_attempt", rules)
        self.assertEqual(rules[-1], "still_silent")
        self.assertEqual(c["turning_points"][-1]["at"], "2026-05-31T23:59:59Z")

    def test_item_cap_and_budget_are_recorded(self):
        long_body = "x" * (link.STORY_ITEM_CAP + 100)
        p = debated_pr(body=long_body)
        c = self.slice_(bundle([p]))["conversation"]
        tr = c["truncation"]
        self.assertEqual(tr["cut"], [{"index": 0, "url": f"{URL}/pull/1",
                                      "kept": link.STORY_ITEM_CAP,
                                      "original": link.STORY_ITEM_CAP + 100}])
        self.assertEqual(c["timeline"][0]["truncated_from"],
                         link.STORY_ITEM_CAP + 100)
        self.assertEqual(len(c["timeline"][0]["text"]), link.STORY_ITEM_CAP)

    def test_budget_cuts_evenly_then_omits_from_the_middle(self):
        items = [{"at": str(i), "url": f"u{i}", "text": "y" * 4000}
                 for i in range(12)]
        with mock.patch.object(link, "STORY_SLICE_BUDGET", 24000):
            tr = link._apply_story_budget(items)
        self.assertEqual({c["kept"] for c in tr["cut"]}, {2000})
        self.assertEqual(tr["text_omitted"], [])
        self.assertEqual(tr["chars_kept"], 24000)
        items = [{"at": str(i), "url": f"u{i}", "text": "y" * 4000}
                 for i in range(12)]
        with mock.patch.object(link, "STORY_SLICE_BUDGET", 3000):
            tr = link._apply_story_budget(items)
        omitted = [d["index"] for d in tr["text_omitted"]]
        self.assertNotIn(0, omitted)
        self.assertNotIn(11, omitted)
        self.assertLessEqual(tr["chars_kept"], 3000)
        for d in tr["text_omitted"]:
            self.assertIsNone(items[d["index"]]["text"])
            self.assertEqual(items[d["index"]]["text_omitted"], 4000)

    def test_slice_carries_the_story_fields(self):
        s = self.slice_(bundle([debated_pr()]))
        for key in ("discussion", "story", "story_rank", "story_score",
                    "story_reasons"):
            self.assertIn(key, s["train"])
        s2 = self.slice_(bundle([pr(1)]))
        self.assertNotIn("discussion", s2["train"])
        self.assertEqual(s2["conversation"]["turning_points"][-1]["rule"],
                         "still_silent")


# --- 4. what the code said ---------------------------------------------------

class TestCodeNotes(unittest.TestCase):

    def test_comment_classes(self):
        cc = link.comment_class
        self.assertEqual(cc("comment", "// Variables //"), "banner")
        self.assertEqual(cc("comment", "// Parameters"), "banner")
        self.assertEqual(cc("comment", "// ===== Outputs ====="), "banner")
        self.assertEqual(cc("comment", "// General resources"), "banner")
        self.assertEqual(cc("comment", "@description('Optional. Tags.')"),
                         "parameter_description")
        self.assertEqual(cc("comment", "@sys.description('x')"),
                         "parameter_description")
        self.assertEqual(cc("comment", "// Plan family detection."), "note")
        self.assertEqual(cc("comment", "// Filter out reserved keys from "
                                       "user-supplied app settings so "
                                       "module-managed values always win."),
                         "note")
        self.assertEqual(cc("todo", "// TODO: remove"), "todo")
        self.assertEqual(cc("comment", "# Terraform note about count"), "note")

    def test_merged_and_pr_commits_carry_their_notes(self):
        sha, psha = "c" * 40, "d" * 40
        events = [
            {"commit": sha, "path": "m/main.bicep", "lang": "bicep",
             "subkind": "comment", "change": "add", "date": "2026-05-06",
             "name": "// Variables //", "before": None, "after": "// Variables //"},
            {"commit": sha, "path": "m/main.bicep", "lang": "bicep",
             "subkind": "comment", "change": "add", "date": "2026-05-06",
             "name": "// Keep the user's value when set.", "before": None,
             "after": "// Keep the user's value when set."},
            {"commit": sha, "path": "m/main.bicep", "lang": "bicep",
             "subkind": "param", "change": "add", "date": "2026-05-06",
             "name": "keep", "before": None, "after": "param keep bool = true"}]
        history = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched",
             "commits": [{"sha": psha, "parents": 1, "author": "Alice",
                          "authored_at": "2026-05-04T12:00:00Z",
                          "committed_at": "2026-05-04T12:00:00Z",
                          "subject": "try", "files": 1}],
             "notes": [{"commit": psha, "path": "m/main.bicep",
                        "subkind": "comment", "change": "drop",
                        "name": "// first attempt, inline it", "before": "// first attempt, inline it",
                        "after": None}]}]}
        p = debated_pr(state="closed", merged=True,
                       merged_at="2026-05-06T12:00:00Z")
        b = bundle([p], pr_history=history, symbol_events=events,
                   commits=[{"sha": sha, "date": "2026-05-06",
                             "committed_at": "2026-05-06T12:00:00Z",
                             "message": "feat: thing 1 (#1)", "author": "alice",
                             "files": ["m/main.bicep"], "pr": 1}])
        b = enriched(b)
        c = link.slice_train(b, train_of(b)["id"])["conversation"]
        commits = [i for i in c["timeline"] if i["kind"] == "commit"]
        self.assertEqual([i["source"] for i in commits], ["pr_head", "base"])
        self.assertEqual(commits[0]["code_notes"], [
            {"commit": psha, "path": "m/main.bicep", "change": "drop",
             "class": "note", "source": "pr_head", "text": "// first attempt, inline it",
             "pr": 1}])
        self.assertEqual([(n["class"], n.get("text") or n.get("name"))
                          for n in commits[1]["code_notes"]],
                         [("note", "// Keep the user's value when set."),
                          ("interface", "keep")])
        self.assertEqual(c["truncation"]["code_notes"]["banners_dropped"], 1)
        self.assertEqual(c["code_notes_by_module"],
                         {"m": {"m/main.bicep": {"added": 2, "removed": 1,
                                                 "changed": 0}}})

    def test_notes_budget_keeps_developer_notes_first(self):
        sha = "e" * 40
        events = [{"commit": sha, "path": "m/main.bicep", "lang": "bicep",
                   "subkind": "comment", "change": "add", "date": "2026-05-06",
                   "name": "@description('" + "p" * 100 + "')", "before": None,
                   "after": "@description('" + "p" * 100 + "')"}] * 5 + [
                  {"commit": sha, "path": "m/main.bicep", "lang": "bicep",
                   "subkind": "comment", "change": "add", "date": "2026-05-06",
                   "name": "// why this matters: x.", "before": None,
                   "after": "// why this matters: x."}]
        b = bundle([debated_pr()], symbol_events=events,
                   commits=[{"sha": sha, "date": "2026-05-06", "pr": 1,
                             "message": "x (#1)", "author": "a", "files": []}])
        b["trains"] = [{"id": "t", "commits": [sha], "prs": [1]}]
        with mock.patch.object(link, "STORY_CODE_NOTES_BUDGET", 150):
            by_commit, _m, info = link.train_code_notes(b, b["trains"][0])
        self.assertEqual([r["class"] for r in by_commit[sha]],
                         ["parameter_description", "note"])
        self.assertEqual(info["omitted"], 4)


# --- 4b. PR-internal history at gather time ----------------------------------

class TestPrHistoryGather(unittest.TestCase):
    """Real git: a squash-merging origin with refs/pull/<N>/head, cloned
    blobless and shallow like gather's clone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-story-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        src = self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        git("config", "uploadpack.allowFilter", "true", cwd=src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
        self.write("m/main.bicep", "// Parameters //\nparam a string\n")
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=src)
        self.write("m/main.bicep", "// Parameters //\nparam a string\n"
                   "// first attempt: inline the value\nparam b string\n")
        self.f1 = self.commit("2026-05-03", "first attempt")
        self.write("m/main.bicep", "// Parameters //\nparam a string\n"
                   "// reworked after review: derive it\nparam b string\n")
        self.f2 = self.commit("2026-05-05", "rework after review",
                              author_date="2026-05-03")
        git("checkout", "-q", "main", cwd=src)
        self.write("m/main.bicep", "// Parameters //\nparam a string\n"
                   "// reworked after review: derive it\nparam b string\n")
        self.commit("2026-05-06", "feat: b (#7)")
        git("update-ref", "refs/pull/7/head", self.f2, cwd=src)
        git("update-ref", "refs/pull/8/head", self.f1, cwd=src)
        self.clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "--filter=blob:none", "--shallow-since=2026-05-02",
            "--single-branch", "file://" + src, self.clone)

    def write(self, rel, text):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, date, msg, author_date=None):
        git("add", "-A", cwd=self.src)
        git("commit", "-q", "-m", msg, cwd=self.src, date=date,
            author_date=author_date)
        return git("rev-parse", "HEAD", cwd=self.src).strip()

    def test_commits_notes_missing_and_skips(self):
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "why?"}]},
               {"number": 8},
               {"number": 9, "comments_list": [{"author": "bob", "body": "?"}]}]
        h = gather.collect_pr_history(prs, self.clone, base_branch="main")
        self.assertTrue(h["collected"])
        self.assertEqual(h["base"], "main")
        rows = {r["number"]: r for r in h["prs"]}
        self.assertEqual(rows[9], {"number": 9, "status": "missing"})
        r7 = rows[7]
        self.assertEqual(r7["status"], "fetched")
        self.assertEqual(r7["head"], self.f2)
        self.assertEqual([c["sha"] for c in r7["commits"]], [self.f1, self.f2])
        self.assertEqual([c["subject"] for c in r7["commits"]],
                         ["first attempt", "rework after review"])
        self.assertEqual(r7["commits"][1]["authored_at"], "2026-05-03T12:00:00Z")
        self.assertEqual(r7["commits"][1]["committed_at"], "2026-05-05T12:00:00Z")
        self.assertEqual(r7["commits"][0]["files"], 1)
        self.assertEqual(r7["commits"][0]["author"], "Test")
        notes = [(n["commit"], n["change"], n["subkind"], n["name"])
                 for n in r7["notes"] if n["subkind"] == "comment"]
        self.assertIn((self.f1, "add", "comment",
                       "// first attempt: inline the value"), notes)
        self.assertIn((self.f2, "drop", "comment",
                       "// first attempt: inline the value"), notes)
        self.assertIn((self.f2, "add", "comment",
                       "// reworked after review: derive it"), notes)
        self.assertIn((self.f1, "add", "param", "b"),
                      [(n["commit"], n["change"], n["subkind"], n["name"])
                       for n in r7["notes"]])
        self.assertEqual(rows[8]["notes_skipped"], "no human conversation")
        self.assertEqual([c["sha"] for c in rows[8]["commits"]], [self.f1])
        # the refs landed where documented
        self.assertEqual(git("-C", self.clone, "rev-parse",
                             "refs/remotes/pr/7").strip(), self.f2)

    def test_notes_caps(self):
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "x"}]}]
        with mock.patch.object(gather, "PR_HISTORY_NOTES_MAX_COMMITS", 1):
            h = gather.collect_pr_history(prs, self.clone, base_branch="main")
        self.assertEqual(h["prs"][0]["notes_skipped"], "more than 1 commits")
        with mock.patch.object(gather, "PR_HISTORY_NOTES_MAX", 1):
            h = gather.collect_pr_history(prs, self.clone, base_branch="main")
        self.assertEqual(len(h["prs"][0]["notes"]), 1)
        self.assertGreater(h["prs"][0]["notes_omitted"], 0)
        with mock.patch.object(gather, "PR_HISTORY_NOTES_MAX_PRS", 0):
            h = gather.collect_pr_history(prs, self.clone, base_branch="main")
        self.assertEqual(h["prs"][0]["notes_skipped"],
                         "over the 0-PR cap of the window")

    def test_failures_are_recorded_not_fatal(self):
        def no_listing(args, cwd=None, timeout=None):
            raise RuntimeError("command failed\nfatal: unable to access")
        h = gather.collect_pr_history([{"number": 7}], self.clone,
                                      run=no_listing, base_branch="main")
        self.assertEqual(h["prs"], [])
        self.assertIn("unable to access", h["error"])

        def no_fetch(args, cwd=None, timeout=None):
            if "ls-remote" in args:
                return "{}\trefs/pull/7/head\n".format(self.f2)
            if "fetch" in args:
                raise RuntimeError("command failed\nfatal: refusing")
            return gather.run_git(args, cwd=cwd, timeout=timeout)
        h = gather.collect_pr_history([{"number": 7}], self.clone, run=no_fetch,
                                      base_branch="main")
        self.assertEqual(h["prs"], [{"number": 7, "status": "fetch_failed",
                                     "reason": "fatal: refusing"}])

    def test_pure_parsers(self):
        self.assertEqual(gather.parse_pr_head_refs(
            "abc\trefs/pull/12/head\ndef\trefs/pull/12/merge\nx y z\n"),
            {12: "abc"})
        raw = ("\x1eaaa\x1fp1 p2\x1fAlice\x1f2026-05-01T10:00:00+02:00\x1f"
               "2026-05-01T12:00:00+02:00\x1fMerge main\n\n"
               "\x1ebbb\x1fp1\x1fBob\x1f2026-05-02T10:00:00Z\x1f"
               "2026-05-02T10:00:00Z\x1ffix: x\n\na.bicep\nb.md\n")
        self.assertEqual(gather.parse_pr_commits(raw), [
            {"sha": "aaa", "parents": 2, "author": "Alice",
             "authored_at": "2026-05-01T08:00:00Z",
             "committed_at": "2026-05-01T10:00:00Z", "subject": "Merge main",
             "files": 0},
            {"sha": "bbb", "parents": 1, "author": "Bob",
             "authored_at": "2026-05-02T10:00:00Z",
             "committed_at": "2026-05-02T10:00:00Z", "subject": "fix: x",
             "files": 2}])

    def test_cli_switch(self):
        args = gather.parse_args(["--owner", "o", "--repo", "r", "--from",
                                  "2026-05-01", "--to", "2026-05-31",
                                  "--store", "s.db", "--no-pr-history"])
        self.assertFalse(args.pr_history)
        args = gather.parse_args(["--owner", "o", "--repo", "r", "--from",
                                  "2026-05-01", "--to", "2026-05-31",
                                  "--store", "s.db"])
        self.assertTrue(args.pr_history)


# --- store round trip ----------------------------------------------------------

def _store_bundle(frm, to, history):
    with open(os.path.join(HERE, "fixtures", "bundle_sample.json"),
              encoding="utf-8") as fh:
        b = json.load(fh)
    b["meta"].update({"from": frm, "to": to, "period": {"from": frm, "to": to}})
    b["pr_history"] = history
    return b


class TestPrHistoryStore(unittest.TestCase):

    def setUp(self):
        quiet(self)

    def fold(self, conn, b):
        gather.fold_bundle(conn, copy.deepcopy(b))

    def view(self, conn, frm, to):
        with open(os.path.join(HERE, "fixtures", "bundle_sample.json"),
                  encoding="utf-8") as fh:
            meta = json.load(fh)["meta"]
        return extract.extract(conn, meta["owner"], meta["repo"], frm, to)

    def test_window_fact_round_trip_and_merge(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        h1 = {"collected": True, "base": "main", "limits": {"max_commits": 250},
              "prs": [{"number": 42, "status": "fetched", "commits": []},
                      {"number": 43, "status": "missing"}]}
        h2 = {"collected": True, "base": "main", "limits": {"max_commits": 250},
              "prs": [{"number": 42, "status": "fetch_failed", "reason": "r"}]}
        self.fold(conn, _store_bundle("2026-05-01", "2026-05-15", h1))
        self.fold(conn, _store_bundle("2026-05-16", "2026-05-31", h2))
        self.assertEqual(self.view(conn, "2026-05-01", "2026-05-15")["pr_history"],
                         h1)
        merged = self.view(conn, "2026-05-01", "2026-05-31")["pr_history"]
        self.assertEqual(merged["prs"], [h2["prs"][0], h1["prs"][1]])
        self.assertEqual(merged["windows"], [
            {"from": "2026-05-01", "to": "2026-05-15"},
            {"from": "2026-05-16", "to": "2026-05-31"}])
        self.assertIn("pr_history", validate._fact_signatures(h1))
        self.assertIsNone(validate._fact_shape_problem("pr_history", h1))
        self.assertIsNotNone(validate._fact_shape_problem("module_notes", h1))

    def test_not_collected_is_kept(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        self.fold(conn, _store_bundle("2026-05-01", "2026-05-31",
                                      {"collected": False}))
        self.assertEqual(self.view(conn, "2026-05-01", "2026-05-31")["pr_history"],
                         {"collected": False})

    def test_a_past_window_story_holds_nothing_after_its_end(self):
        """Gathered later (records hold June activity), viewed as of May's
        end: no June comment, review or PR commit reaches the story."""
        p = debated_pr(number=42, updated_at="2026-06-20T10:00:00Z")
        p["comments_list"].append(comment("carol", "2026-06-10T10:00:00Z",
                                          "Late objection after the window", 15))
        p["reviews"].append(review("carol", "2026-06-11T10:00:00Z",
                                   "changes_requested", 34, "June change request"))
        history = {"collected": True, "base": "main", "prs": [
            {"number": 42, "status": "fetched", "commits": [
                {"sha": c * 40, "parents": 1, "author": "Alice",
                 "authored_at": at, "committed_at": at, "subject": s, "files": 1}
                for c, at, s in (("a", "2026-05-04T12:00:00Z", "in May"),
                                 ("b", "2026-06-12T12:00:00Z", "in June"))]}]}
        b = bundle([p], pr_history=history)
        b["meta"]["gathered_at"] = "2026-06-30T00:00:00Z"
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        self.fold(conn, b)
        view = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                               as_of_end=True)
        link.enrich(view)
        t = train_of(view)
        d = t["discussion"]
        self.assertEqual((d["comments"], d["change_requests"]), (3, 1))
        c = link.slice_train(view, t["id"])["conversation"]
        texts = " ".join(i.get("text") or i.get("subject") or ""
                         for i in c["timeline"])
        self.assertIn("in May", texts)
        for later in ("Late objection", "June change request", "in June"):
            self.assertNotIn(later, texts)
        self.assertTrue(all(i["at"] <= "2026-05-31T23:59:59Z"
                            for i in c["timeline"]))


# --- 5. the evidence check -----------------------------------------------------

class TestEvidenceCheck(unittest.TestCase):

    def setUp(self):
        b = enriched(bundle([debated_pr()]))
        self.slice = link.slice_train(b, train_of(b)["id"])

    def test_cited_urls_and_quotes_must_be_in_the_slice(self):
        ok = ("Bob asked \"Why not reuse the shared type?\" "
              f"({URL}/pull/1#issuecomment-11); alice replied that it "
              f"“lacks the field” ({URL}/pull/1#issuecomment-12).")
        r = link.check_story_evidence(self.slice, ok)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["urls_checked"], r["quotes_checked"]), (2, 2))
        bad = (f"Bob \"insisted on a rewrite\" ({URL}/pull/1#issuecomment-99).")
        r = link.check_story_evidence(self.slice, bad)
        self.assertFalse(r["ok"])
        self.assertEqual(r["urls_not_in_slice"], [f"{URL}/pull/1#issuecomment-99"])
        self.assertEqual(r["quotes_not_in_slice"], ["insisted on a rewrite"])

    def test_cli(self):
        tmp = tempfile.mkdtemp(prefix="ao-check-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sp, good, bad = (os.path.join(tmp, n) for n in ("s.json", "g.md", "b.md"))
        with open(sp, "w") as fh:
            json.dump(self.slice, fh)
        with open(good, "w") as fh:
            fh.write(f"Opened at {URL}/pull/1.")
        with open(bad, "w") as fh:
            fh.write(f"See {URL}/pull/2.")
        for path, code in ((good, 0), (bad, 1)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    link.main([sp, "--check-story", path])
            self.assertEqual(cm.exception.code, code)
            self.assertEqual(json.loads(out.getvalue())["ok"], code == 0)


# --- docs contract ---------------------------------------------------------------

class TestDocsContract(unittest.TestCase):
    """BUNDLE.md names every key the story layer emits."""

    def section(self):
        with open(os.path.join(HERE, "BUNDLE.md"), encoding="utf-8") as fh:
            doc = fh.read()
        return doc[doc.index("## The story layer"):doc.index("## Optional")]

    def test_every_key_is_documented(self):
        import re
        history = {"collected": True, "base": "main", "prs": [
            {"number": 1, "status": "fetched", "commits": [
                {"sha": "a" * 40, "parents": 2, "author": "A",
                 "authored_at": "2026-05-01T12:00:00Z",
                 "committed_at": "2026-05-04T12:00:00Z", "subject": "s",
                 "files": 0}]}]}
        p = debated_pr(lifecycle=[{"event": "reopened", "actor": "bob", "id": 9,
                                   "created_at": "2026-05-24T10:00:00Z"}])
        b = enriched(bundle([p], pr_history=history))
        t = train_of(b)
        s = link.slice_train(b, t["id"])
        c = s["conversation"]
        keys = (set(t["discussion"]) | set(t["discussion"]["longest_silence"])
                | set(b["stories"]) | set(b["stories"]["rule"]) | set(c)
                | set(c["truncation"]) | set(c["truncation"]["code_notes"])
                | {k for i in c["timeline"] for k in i}
                | {k for tp in c["turning_points"] for k in tp}
                | {tp["rule"] for tp in c["turning_points"]}
                | {"story", "story_rank", "story_score", "story_reasons"}
                | set(history["prs"][0]) | set(history["prs"][0]["commits"][0]))
        section = self.section()
        missing = sorted(k for k in keys if not re.search(
            r"(?<![\w]){}(?![\w])".format(re.escape(k)), section))
        self.assertEqual(missing, [])
        for rule in ("new_attempt", "still_silent", "silence_then_activity"):
            self.assertIn(rule, section)

    def test_conversation_keys_in_the_slice_block(self):
        import test_link_small_fixes as tl
        doc = tl._documented_slice()
        s = link.slice_train(tl._contract_bundle(), "t")
        self.assertEqual(set(doc["conversation"]), set(s["conversation"]))


# --- 6. module-layer follow-ups ------------------------------------------------

class TestReleaseNotes(unittest.TestCase):

    def test_notes_kept_and_capped(self):
        raw = {"tag_name": "v1.2.0", "name": "1.2.0", "body": "## What's new\r\n- x",
               "published_at": "2026-05-15T00:00:00Z", "prerelease": False,
               "html_url": "https://github.com/o/r/releases/tag/v1.2.0"}
        rel = gather.normalize_release(raw)
        self.assertEqual(rel["notes"], "## What's new\n- x")
        self.assertNotIn("notes_truncated_from", rel)
        rel = gather.normalize_release(dict(raw, body="y" * 5000))
        self.assertEqual(len(rel["notes"]), gather.RELEASE_NOTES_CAP)
        self.assertEqual(rel["notes_truncated_from"], 5000)
        for empty in (None, "", "  \n"):
            self.assertNotIn("notes", gather.normalize_release(dict(raw, body=empty)))

    def test_module_changes_releases_carry_the_notes(self):
        # a Terraform module repo without a changelog: the release text is
        # the maintainers' own notes
        b = {"meta": {"owner": "o", "repo": "terraform-x-y", "from": "2026-10-01",
                      "to": "2026-10-31", "base_branch": "main",
                      "period": {"from": "2026-10-01", "to": "2026-10-31"}},
             "prs": [], "issues": [], "milestones": [],
             "commits": [{"sha": "t1", "author": "a", "date": "2026-10-03",
                          "message": "feat: tags (#5)", "files": ["main.tf"]}],
             "code_events": [{"commit": "t1", "date": "2026-10-03",
                              "change": "modify", "path": "main.tf",
                              "author": "a"}],
             "symbol_events": [],
             "releases": [{"tag_name": "v1.2.0", "name": "v1.2.0",
                           "prerelease": False,
                           "published_at": "2026-10-04T00:00:00Z",
                           "url": "https://github.com/o/terraform-x-y/releases/v1.2.0",
                           "notes": "Fixed the thing.",
                           "notes_truncated_from": 9000}],
             "code_graph": {"areas": [{"id": "main.tf", "paths": ["main.tf"]}]},
             "module_notes": {"collected": True, "changelogs": [], "versions": [],
                              "tags": {"collected": False, "reason": "none"}}}
        mc = link.enrich(b)["module_changes"]
        self.assertEqual(mc["releases"][0]["notes"], "Fixed the thing.")
        self.assertEqual(mc["releases"][0]["notes_truncated_from"], 9000)
        rows = [e["versions"]["releases"] for e in mc["modules"]
                if (e.get("versions") or {}).get("releases")]
        self.assertTrue(rows)
        self.assertEqual(rows[0][0]["notes"], "Fixed the thing.")


class TestVersionGaps(unittest.TestCase):

    def test_declared_not_published_and_the_reverse(self):
        import test_module_changes as tmc
        mc = tmc.enriched()["module_changes"]
        mods = {e["area"]: e for e in mc["modules"]}
        self.assertEqual(mods[tmc.LAB]["versions"]["declared_not_published"],
                         ["0.6.0"])
        self.assertEqual(mods[tmc.HUB]["versions"]["published_not_declared"],
                         ["0.4.0"])
        self.assertNotIn("declared_not_published",
                         mods[tmc.NEW]["versions"])          # 0.1.0 is tagged
        gaps = {g["area"]: g for g in mc["summary"]["version_gaps"]}
        self.assertEqual(gaps[tmc.LAB], {"area": tmc.LAB,
                                         "declared_not_published": ["0.6.0"]})

    def test_no_gaps_without_tags_or_for_unreleased(self):
        import test_module_changes as tmc
        b = tmc.window()
        b["module_notes"]["tags"] = {"collected": False, "reason": "no tags"}
        mc = link.enrich(b)["module_changes"]
        self.assertNotIn("version_gaps", mc["summary"])
        self.assertFalse(link._is_release_version("Unreleased"))
        self.assertTrue(link._is_release_version("v1.0.0"))
        self.assertEqual(link._bare_version("v0.6.0"), "0.6.0")


if __name__ == "__main__":
    unittest.main()
