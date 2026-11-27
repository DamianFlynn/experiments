"""The substance of a story (issue #97, after the first two sample stories):
what each review comment is attached to, what each PR commit changed, what
ended up in the product or what open work waits on (the outcome block, with
the derived "ball is with" rule), the open-threads ledger, and the story
check's structure rules (a year in the first date, a closing "Where it
stands" / "What landed" part that states every named item).

The gather tests drive REAL git against a throwaway local `file://`
repository (no network, no global / system git config)."""
import copy
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
from test_story import (GIT_TEST_ENV, URL, bundle, comment, debated_pr,  # noqa: E402
                        git, pr, review, train_of)


def enriched(b):
    return link.enrich(copy.deepcopy(b))


def rc(author, at, body, n, number=1, **kw):
    out = {"author": author, "created_at": at, "body": body, "id": n,
           "url": f"{URL}/pull/{number}#discussion_r{n}"}
    out.update(kw)
    return out


def history_row(number, commits, **kw):
    row = {"number": number, "status": "fetched", "commits": commits}
    row.update(kw)
    return {"collected": True, "base": "main", "prs": [row]}


def commit(sha, at, subject, paths=(), body=None, parents=1):
    c = {"sha": sha, "parents": parents, "author": "Alice Example",
         "authored_at": at, "committed_at": at, "subject": subject,
         "files": len(paths)}
    if paths:
        c["paths"] = list(paths)
    if body:
        c["body"] = body
    return c


def conversation(b, tid=None):
    b = enriched(b)
    return link.slice_train(b, tid or train_of(b)["id"])["conversation"]


# --- 1. gather: review-comment context, commit paths / bodies, patches ------

class TestReviewCommentContext(unittest.TestCase):

    RAW = {"id": 7002, "body": "This impacts every module.",
           "user": {"login": "carol"}, "author_association": "MEMBER",
           "html_url": "https://github.com/o/r/pull/42#discussion_r7002",
           "created_at": "2026-06-13T15:50:28Z",
           "path": ".github/workflows/avm.template.module.yml",
           "line": 77, "original_line": 75,
           "diff_hunk": "@@ -74,6 +74,7 @@ jobs:\n           psrulePath: x\n"
                        "+          skipPassedRulesReport: true",
           "in_reply_to_id": None, "original_commit_id": "a" * 40,
           "commit_id": "b" * 40}

    def test_context_kept_when_github_gives_it(self):
        c = gather.normalize_review_comment(self.RAW)
        self.assertEqual(c["path"], ".github/workflows/avm.template.module.yml")
        self.assertEqual(c["line"], 77)
        self.assertEqual(c["commit"], "a" * 40)
        self.assertTrue(c["diff_hunk"].endswith("skipPassedRulesReport: true"))
        self.assertNotIn("diff_hunk_lines", c)
        self.assertNotIn("in_reply_to", c)            # None: not a reply
        r = gather.normalize_review_comment(dict(self.RAW, in_reply_to_id=7001,
                                                 line=None))
        self.assertEqual((r["in_reply_to"], r["line"]), (7001, 75))

    def test_absent_context_adds_no_key(self):
        bare = {k: v for k, v in self.RAW.items()
                if k in ("id", "body", "user", "author_association", "html_url",
                         "created_at")}
        self.assertEqual(set(gather.normalize_review_comment(bare)),
                         {"id", "author", "author_association", "body", "url",
                          "created_at"})

    def test_long_hunk_keeps_header_and_the_commented_end(self):
        hunk = "@@ -1,40 +1,40 @@\n" + "\n".join(f" line {i}" for i in range(40))
        kept, total = gather.review_hunk(hunk)
        lines = kept.split("\n")
        self.assertEqual(lines[0], "@@ -1,40 +1,40 @@")
        self.assertEqual(lines[-1], " line 39")
        self.assertEqual(len(lines), gather.REVIEW_HUNK_MAX_LINES)
        self.assertEqual(total, 41)
        with mock.patch.object(gather, "REVIEW_HUNK_CAP", 20):
            kept, total = gather.review_hunk(hunk)
        self.assertEqual(len(kept), 20)
        self.assertTrue(kept.endswith(" line 39"))


class TestPrCommitParsing(unittest.TestCase):

    def test_new_format_carries_paths_and_body(self):
        raw = ("\x1eaaa\x1fp1\x1fAlice\x1f2026-08-25T16:09:02Z\x1f"
               "2026-08-25T16:09:02Z\x1ffeat: narrow scope\x1f"
               "The switch promised too much.\n\n- Remove `enableX`.\n\n"
               "Co-authored-by: Bot <b@example.invalid>\n\x1d\n\n"
               "m/main.bicep\nm/CHANGELOG.md\n"
               "\x1ebbb\x1fp1 p2\x1fAlice\x1f2026-08-26T10:00:00Z\x1f"
               "2026-08-26T10:00:00Z\x1fMerge main\x1f\x1d\n")
        c1, c2 = gather.parse_pr_commits(raw)
        self.assertEqual(c1["subject"], "feat: narrow scope")
        self.assertEqual(c1["paths"], ["m/main.bicep", "m/CHANGELOG.md"])
        self.assertEqual(c1["files"], 2)
        self.assertEqual(c1["body"],
                         "The switch promised too much.\n\n- Remove `enableX`.")
        self.assertEqual((c2["parents"], c2["files"]), (2, 0))
        self.assertNotIn("paths", c2)
        self.assertNotIn("body", c2)

    def test_caps(self):
        names = "\n".join(f"f{i}.bicep" for i in range(5))
        raw = ("\x1eaaa\x1fp1\x1fA\x1f2026-08-25T16:09:02Z\x1f2026-08-25T16:09:02Z"
               "\x1fs\x1f" + "x" * 50 + "\x1d\n\n" + names + "\n")
        with mock.patch.object(gather, "PR_HISTORY_PATHS_MAX", 2), \
                mock.patch.object(gather, "PR_HISTORY_BODY_CAP", 10):
            c = gather.parse_pr_commits(raw)[0]
        self.assertEqual((c["paths"], c["paths_omitted"], c["files"]),
                         (["f0.bicep", "f1.bicep"], 3, 5))
        self.assertEqual((c["body"], c["body_truncated_from"]), ("x" * 10, 50))

    def test_trailer_paragraphs_are_dropped_prose_is_kept(self):
        body = gather.pr_commit_body
        self.assertEqual(body("Agent-Logs-Url: https://x\n\nCo-authored-by: A\n"),
                         ("", 0))
        self.assertEqual(body("Fix\nNote: keep this\n\nSigned-off-by: B\n")[0],
                         "Fix\nNote: keep this")
        self.assertEqual(body("Why: because.\n")[0], "Why: because.")

    def test_patches_parser(self):
        raw = ("\x1eaaa\n"
               "diff --git a/m/CHANGELOG.md b/m/CHANGELOG.md\n"
               "index 1..2 100644\n--- a/m/CHANGELOG.md\n+++ b/m/CHANGELOG.md\n"
               "@@ -9 +9 @@\n-- None\n+- Disabled weak ciphers by default.\n"
               "diff --git a/.github/workflows/t.yml b/.github/workflows/t.yml\n"
               "--- a/.github/workflows/t.yml\n+++ b/.github/workflows/t.yml\n"
               "@@ -77,0 +78 @@\n+          skip: true\n")
        self.assertEqual(gather.parse_pr_patches(raw), [
            {"commit": "aaa", "path": "m/CHANGELOG.md", "kind": "changelog",
             "added": ["- Disabled weak ciphers by default."], "removed": ["- None"]},
            {"commit": "aaa", "path": ".github/workflows/t.yml", "kind": "shared",
             "added": ["          skip: true"], "removed": []}])
        with mock.patch.object(gather, "PR_HISTORY_PATCH_LINES", 1):
            p = gather.parse_pr_patches(raw)[0]
        self.assertEqual((p["removed"], p["added"], p["lines_omitted"]),
                         (["- None"], [], 1))


class TestPrHistoryRealGit(unittest.TestCase):
    """Real git: a PR that changes its module, its changelog and a shared
    workflow, then reverts the workflow change."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-substance-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        src = self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        git("config", "uploadpack.allowFilter", "true", cwd=src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
        self.write("m/main.bicep", "param a string\n")
        self.write("m/CHANGELOG.md", "# Changelog\n\n- None\n")
        self.write(".github/workflows/t.yml", "jobs:\n  x: 1\n")
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=src)
        self.write("m/main.bicep", "param a string\nparam b string\n")
        self.write("m/CHANGELOG.md", "# Changelog\n\n- Added `b`.\n")
        self.f1 = self.commit("2026-05-03", "feat: add b",
                              body="Callers need b.\n\nSigned-off-by: T <t@x>")
        self.write(".github/workflows/t.yml", "jobs:\n  x: 1\n  skip: true\n")
        self.f2 = self.commit("2026-05-04", "ci: skip a report")
        self.write(".github/workflows/t.yml", "jobs:\n  x: 1\n")
        self.f3 = self.commit("2026-05-05", "ci: revert the skip")
        git("checkout", "-q", "main", cwd=src)
        git("update-ref", "refs/pull/7/head", self.f3, cwd=src)
        self.clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "--filter=blob:none", "--single-branch",
            "file://" + src, self.clone)

    def write(self, rel, text):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, date, msg, body=None):
        git("add", "-A", cwd=self.src)
        args = ["commit", "-q", "-m", msg] + (["-m", body] if body else [])
        git(*args, cwd=self.src, date=date)
        return git("rev-parse", "HEAD", cwd=self.src).strip()

    def test_paths_bodies_and_patches(self):
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "why?"}]}]
        row = gather.collect_pr_history(prs, self.clone, base_branch="main")["prs"][0]
        c1, c2, c3 = row["commits"]
        self.assertEqual(sorted(c1["paths"]), ["m/CHANGELOG.md", "m/main.bicep"])
        self.assertEqual(c1["body"], "Callers need b.")
        self.assertNotIn("body", c2)
        self.assertEqual(c2["paths"], [".github/workflows/t.yml"])
        got = [(p["commit"], p["path"], p["kind"], p["added"], p["removed"])
               for p in row["patches"]]
        self.assertEqual(got, [
            (self.f1, "m/CHANGELOG.md", "changelog", ["- Added `b`."], ["- None"]),
            (self.f2, ".github/workflows/t.yml", "shared", ["  skip: true"], []),
            (self.f3, ".github/workflows/t.yml", "shared", [], ["  skip: true"])])
        with mock.patch.object(gather, "PR_HISTORY_PATCH_CHARS", 75):
            row = gather.collect_pr_history(prs, self.clone,
                                            base_branch="main")["prs"][0]
        self.assertEqual(len(row["patches"]), 2)        # the newest kept
        self.assertEqual(row["patches_omitted"], 1)

    def test_a_failed_patch_walk_is_recorded(self):
        def run(args, cwd=None, timeout=None):
            if "-p" in args and "--unified=0" in args:
                raise RuntimeError("command failed\nfatal: broken")
            return gather.run_git(args, cwd=cwd, timeout=timeout)
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "x"}]}]
        row = gather.collect_pr_history(prs, self.clone, run=run,
                                        base_branch="main")["prs"][0]
        self.assertEqual(row["patches_skipped"], "patch walk failed: fatal: broken")
        self.assertIn("notes", row)


# --- 2. link: the change summaries and review-comment context --------------

A, B, C = "a" * 40, "b" * 40, "c" * 40


def ciphers_pr(**kw):
    """An open PR in the shape of the API Management cipher case: a module
    change, a shared-workflow change a reviewer objects to, a change
    request, a rework, an issue opened for the CI part, a ping."""
    p = pr(1, title="fix: disable weak ciphers", body="Fixes #6", closes=[6],
           created_at="2026-06-10T04:00:00Z",
           review_comments=[
               rc("eriqua", "2026-06-13T15:50:00Z",
                  "Is this needed? It would impact all the modules.", 101,
                  path=".github/workflows/t.yml", line=78,
                  diff_hunk="@@ -77 +78 @@\n+  skip: true"),
               rc("alice", "2026-06-14T09:00:00Z", "It was needed for size.", 102,
                  in_reply_to=101, path=".github/workflows/t.yml",
                  diff_hunk="@@ -77 +78 @@\n+  skip: true"),
               rc("alice", "2026-09-05T16:13:00Z",
                  f"@eriqua i have added the issue {URL}/issues/9 please let me "
                  "know once that is fixed", 103, path="m/main.bicep"),
               rc("maint", "2026-09-18T13:20:00Z", "ping @eriqua", 104,
                  in_reply_to=103, path="m/main.bicep")],
           reviews=[review("eriqua", "2026-06-13T15:58:00Z", "changes_requested",
                           31, "Please update the changelog.")])
    p.update(kw)
    return p


def ciphers_issue(n=6, **kw):
    out = {"number": n, "title": "Disable weak ciphers?", "author": "ron",
           "url": f"{URL}/issues/{n}", "state": "open", "labels": [],
           "created_at": "2025-11-01T10:00:00Z", "body": "Should we?",
           "comments_list": []}
    out.update(kw)
    return out


def ci_issue():
    return ciphers_issue(9, title="Job summary exceeds the 1024k limit",
                         author="alice", created_at="2026-09-05T16:12:00Z",
                         body="### Check\n\n- [x] done\n\n### Description\n\n"
                              "The step summary is cut at 1024k; cap it.")


def ciphers_history():
    return history_row(1, [
        commit(A, "2026-06-10T03:30:00Z", "feat: disable weak ciphers",
               ["m/main.bicep", "m/main.json", "m/README.md"],
               body="Adds six cipher suites to the disabled defaults."),
        commit(B, "2026-06-12T02:48:00Z", "ci: skip passed rules",
               [".github/workflows/t.yml"]),
        commit(C, "2026-06-14T07:57:00Z", "docs: changelog",
               ["m/CHANGELOG.md"])],
        notes=[{"commit": A, "path": "m/main.bicep", "subkind": "param",
                "change": "change", "name": "customProperties",
                "before": "param customProperties object = {}",
                "after": "param customProperties object = {x: 1}"}],
        patches=[{"commit": B, "path": ".github/workflows/t.yml", "kind": "shared",
                  "added": ["  skip: true"], "removed": []},
                 {"commit": C, "path": "m/CHANGELOG.md", "kind": "changelog",
                  "added": ["- Disabled weak ciphers."], "removed": ["- None"]}],
        narrative_collected=True)


def ciphers_bundle(**kw):
    b = bundle([ciphers_pr()], [ciphers_issue(), ci_issue()],
               pr_history=ciphers_history(),
               code_graph={"areas": [{"id": "m", "paths": ["m/main.bicep"]}]})
    b["meta"].update({"from": "2026-09-01", "to": "2026-09-30",
                      "period": {"from": "2026-09-01", "to": "2026-09-30"},
                      "ref_date": "2026-09-30"})
    b.update(kw)
    return b


class TestChangeSummaries(unittest.TestCase):

    def setUp(self):
        self.c = conversation(ciphers_bundle(), "train-issue-6")

    def commits(self):
        return {i["sha"]: i for i in self.c["timeline"] if i["kind"] == "commit"}

    def test_each_pr_commit_says_what_it_changed(self):
        cs = self.commits()
        a = cs[A]["change"]
        self.assertEqual(a["files"], {"docs": ["m/README.md"],
                                      "generated": ["m/main.json"],
                                      "source": ["m/main.bicep"]})
        self.assertNotIn("shared", a)
        self.assertEqual(a["interface"], [{"change": "change", "kind": "param",
                                           "name": "customProperties",
                                           "path": "m/main.bicep"}])
        self.assertEqual(cs[A]["text"],
                         "Adds six cipher suites to the disabled defaults.")
        b = cs[B]["change"]
        self.assertEqual(b["files"], {"ci": [".github/workflows/t.yml"]})
        self.assertTrue(b["shared"])
        self.assertEqual(b["patches"], [{"path": ".github/workflows/t.yml",
                                         "kind": "shared", "added": ["  skip: true"],
                                         "removed": []}])
        self.assertEqual(cs[C]["change"]["patches"][0]["added"],
                         ["- Disabled weak ciphers."])

    def test_review_comments_carry_their_file_and_thread(self):
        rcs = {i["url"]: i for i in self.c["timeline"]
               if i["kind"] == "review_comment"}
        first = rcs[f"{URL}/pull/1#discussion_r101"]
        self.assertEqual((first["path"], first["line"]),
                         (".github/workflows/t.yml", 78))
        self.assertEqual(first["diff_hunk"], "@@ -77 +78 @@\n+  skip: true")
        reply = rcs[f"{URL}/pull/1#discussion_r102"]
        self.assertEqual(reply["in_reply_to"], f"{URL}/pull/1#discussion_r101")
        self.assertNotIn("diff_hunk", reply)        # the thread opener has it
        # only the lack of the (newer) pr_activity fact is a gap here
        self.assertEqual(len(self.c["gaps"]), 1)
        self.assertIn("check results, review-thread resolved flags",
                      self.c["gaps"][0])

    def test_rework_turning_point_lists_what_changed_after_the_request(self):
        tp = next(t for t in self.c["turning_points"]
                  if t["rule"] == "rework_after_change_request")
        self.assertEqual(tp["detail"]["commits_since_request"], 1)
        self.assertEqual(tp["detail"]["paths_since_request"], ["m/CHANGELOG.md"])

    def test_hunk_budget(self):
        with mock.patch.object(link, "STORY_HUNK_BUDGET", 10):
            c = conversation(ciphers_bundle(), "train-issue-6")
        self.assertEqual(c["truncation"]["hunks_omitted"], 1)
        first = next(i for i in c["timeline"] if i["kind"] == "review_comment")
        self.assertNotIn("diff_hunk", first)
        self.assertEqual(first["diff_hunk_omitted"],
                         len("@@ -77 +78 @@\n+  skip: true"))

    def test_an_older_store_says_what_it_lacks(self):
        b = ciphers_bundle()
        for c in b["prs"][0]["review_comments"]:
            for k in ("path", "line", "diff_hunk", "in_reply_to"):
                c.pop(k, None)
        for c in b["pr_history"]["prs"][0]["commits"]:
            c.pop("paths", None)
            c.pop("body", None)
        b["pr_history"]["prs"][0].pop("patches")
        b["pr_history"]["prs"][0].pop("narrative_collected")
        c = conversation(b, "train-issue-6")
        self.assertEqual(len(c["gaps"]), 4)
        self.assertIn("review comments carry no file", c["gaps"][0])
        self.assertIn("PR commits carry no file paths", c["gaps"][1])
        self.assertIn("PR commits of #1 carry no code-comment", c["gaps"][2])
        self.assertIn("are not in the store for PR(s) #1", c["gaps"][3])
        self.assertIn("check runs and their results per PR (CI pass / fail is "
                      "not stored)", c["not_gathered"])


# --- 3. the outcome block and the ball rule ---------------------------------

class TestOutcomeInFlight(unittest.TestCase):

    def setUp(self):
        self.o = conversation(ciphers_bundle(), "train-issue-6")["outcome"]

    def test_items_reviews_and_checks(self):
        self.assertEqual(self.o["train_outcome"], "in_flight")
        self.assertEqual(self.o["as_of"], "2026-09-30T23:59:59Z")
        pr_row = next(r for r in self.o["items"] if r["type"] == "pr")
        self.assertEqual(pr_row["state"], "open")
        self.assertEqual(pr_row["reviews"]["outstanding_change_requests"],
                         [{"by": "eriqua", "since": "2026-06-13T15:58:00Z",
                           "url": f"{URL}/pull/1#pullrequestreview-31"}])
        self.assertEqual(pr_row["checks"], "not gathered")
        self.assertEqual(pr_row["last_event"]["url"], f"{URL}/pull/1#discussion_r104")

    def test_ball_is_derived_from_the_last_event(self):
        ball = self.o["ball"]
        self.assertEqual((ball["state"], ball["with"], ball["rule"], ball["pr"]),
                         ("awaiting mentioned", ["eriqua"], "R1 mention", 1))
        self.assertTrue(ball["derived"])
        self.assertEqual(ball["basis"]["actor"], "maint")

    def test_linked_issue_comes_with_its_title_state_and_ask(self):
        (l9,) = self.o["linked"]
        self.assertEqual((l9["number"], l9["type"], l9["state"], l9["author"]),
                         (9, "issue", "open", "alice"))
        self.assertEqual(l9["title"], "Job summary exceeds the 1024k limit")
        self.assertEqual(l9["description"], "The step summary is cut at 1024k; cap it.")
        self.assertEqual(l9["mentioned_in"], f"{URL}/pull/1#discussion_r103")
        self.assertEqual(self.o["waiting_on"][0]["number"], 9)

    def test_net_change_of_the_open_pr(self):
        net = self.o["net_change"]["1"]
        self.assertEqual(net["files_touched"]["ci"], [".github/workflows/t.yml"])
        self.assertEqual(net["lines"]["m/CHANGELOG.md"]["net_added"],
                         ["- Disabled weak ciphers."])

    def test_a_reverted_shared_change_nets_out(self):
        rows = link._net_lines([
            {"commit": "1", "path": "t.yml", "kind": "shared", "added": ["x"],
             "removed": []},
            {"commit": "2", "path": "t.yml", "kind": "shared", "added": [],
             "removed": ["x"]}])
        self.assertEqual(rows, {"t.yml": {"kind": "shared", "commits": 2,
                                          "reverted": True}})

    def test_the_four_pr_rules(self):
        p = {"author": "alice"}
        ev = lambda kind, actor, text="", **kw: dict(kind=kind, actor=actor,
                                                     text=text, at="t", url="u", **kw)
        cr = {"bob": {"state": "changes_requested"}}
        self.assertEqual(link.ball_with(p, [ev("commit", "Alice Example")], cr,
                                        ["bob"])["state"], "awaiting reviewer")
        self.assertEqual(link.ball_with(p, [ev("commit", "Alice Example")], cr,
                                        ["bob"])["with"], ["bob"])
        self.assertEqual(link.ball_with(p, [ev("opened", "alice")], {}, [])["state"],
                         "awaiting a first review")
        self.assertEqual(link.ball_with(p, [ev("review_comment", "bob", "fix x")],
                                        cr, ["bob"])["state"], "awaiting author")
        ok = link.ball_with(p, [ev("review", "bob", state="approved")],
                            {"bob": {"state": "approved"}}, ["bob"])
        self.assertEqual((ok["state"], ok["rule"]), ("awaiting merge", "R3 approved"))
        self.assertIsNone(link.ball_with(p, [], {}, []))

    def test_as_of_projection_holds(self):
        b = ciphers_bundle()
        b["prs"][0]["review_comments"].append(
            rc("eriqua", "2026-10-02T10:00:00Z", "Approved now", 105))
        b["pr_history"]["prs"][0]["commits"].append(
            commit("d" * 40, "2026-10-03T10:00:00Z", "late", ["m/main.bicep"]))
        b["issues"][1]["state"] = "open"
        o = conversation(b, "train-issue-6")["outcome"]
        # the review comment after the window's end is still in the bundle
        # (the view projects records; this hand bundle is not projected), but
        # the PR commit after the instant never reaches the net change
        self.assertNotIn("d" * 40, str(o["net_change"]))


class TestOutcomeShipped(unittest.TestCase):

    def test_modules_carry_the_maintainers_words(self):
        import test_module_changes as tmc
        b = tmc.window()
        mc_b = link.enrich(copy.deepcopy(b))
        new = next(e for e in mc_b["module_changes"]["modules"]
                   if e["area"] == tmc.NEW)
        n = new["prs"][0]["number"]
        t = next(t for t in mc_b["trains"] if n in (t.get("prs") or []))
        o = link.story_outcome(mc_b, t, [], {}, link.window_end_instant(
            mc_b["meta"]), [])
        (m,) = [m for m in o["modules"] if m["area"] == tmc.NEW]
        self.assertEqual(m["status"], "new")
        self.assertEqual(m["first_version"], new["first_version"])
        items = [i for e in m["changelog"] for s in e["sections"] for i in s["items"]]
        want = [i["text"] for c in new["changelog"] for s in c["sections"]
                for i in s["items"]]
        self.assertEqual(items, want)
        self.assertNotIn("ball", o)


# --- 4. the open-threads ledger -----------------------------------------------

class TestThreads(unittest.TestCase):

    def ledger(self, b, tid="train-issue-6"):
        return conversation(b, tid)["threads"]

    def test_statuses_by_rule(self):
        th = {t["url"]: t for t in self.ledger(ciphers_bundle())}
        t101 = th[f"{URL}/pull/1#discussion_r101"]
        self.assertEqual((t101["kind"], t101["status"], t101["path"]),
                         ("review_thread", "answered", ".github/workflows/t.yml"))
        self.assertEqual(t101["resolution"]["url"], f"{URL}/pull/1#discussion_r102")
        self.assertEqual(t101["ask"], "Is this needed?")
        self.assertEqual(t101["replies"], 1)
        cr = th[f"{URL}/pull/1#pullrequestreview-31"]
        self.assertEqual((cr["kind"], cr["status"]),
                         ("change_request", "followed_by_commit"))
        self.assertIsNone(cr["resolution"]["same_file"])
        # the author's own thread (no question mark) is not a thread; its
        # reply "ping @eriqua" carries no question either
        self.assertNotIn(f"{URL}/pull/1#discussion_r103", th)
        self.assertEqual([t["id"] for t in self.ledger(ciphers_bundle())],
                         ["T1", "T2"])
        for t in th.values():
            self.assertIn(t["status"], link.STORY_THREAD_STATUSES)

    def test_commit_touching_the_file_and_approval(self):
        b = ciphers_bundle()
        p = b["prs"][0]
        p["review_comments"] = [rc("eriqua", "2026-06-13T15:50:00Z",
                                   "Rename this in the changelog", 101,
                                   path="m/CHANGELOG.md")]
        p["reviews"].append(review("eriqua", "2026-06-15T10:00:00Z", "approved", 32))
        th = {t["url"]: t for t in self.ledger(b)}
        t101 = th[f"{URL}/pull/1#discussion_r101"]
        self.assertEqual(t101["status"], "followed_by_commit")
        self.assertEqual(t101["resolution"]["same_file"], True)
        self.assertEqual(t101["resolution"]["url"], f"{URL}/pull/1/commits/{C}")
        self.assertEqual(th[f"{URL}/pull/1#pullrequestreview-31"]["status"],
                         "approved_after")

    def test_unresolved_question_and_old_store_fallback(self):
        b = bundle([debated_pr()])
        th = self.ledger(b, train_of(enriched(b))["id"])
        by = {t["url"]: t for t in th}
        q = by[f"{URL}/pull/1#issuecomment-11"]
        self.assertEqual((q["kind"], q["status"]), ("question", "answered"))
        rcom = by[f"{URL}/pull/1#issuecomment-21"]       # no thread links stored
        self.assertEqual(rcom["status"], "reply_on_item")
        lone = bundle([pr(1, comments_list=[
            comment("bob", "2026-05-02T10:00:00Z", "Why not reuse X?", 1)])])
        (t,) = self.ledger(lone, train_of(enriched(lone))["id"])
        self.assertEqual(t["status"], "unresolved in the record")
        self.assertNotIn("resolution", t)

    def test_threads_cap(self):
        with mock.patch.object(link, "STORY_THREADS_MAX", 1):
            c = conversation(ciphers_bundle(), "train-issue-6")
        self.assertEqual(len(c["threads"]), 1)
        self.assertEqual(c["threads_omitted"], 1)

    def test_ask_is_verbatim(self):
        text = "> quoted? no\nHola,\nregarding this: Is the resource not idempotent? We use it."
        self.assertEqual(link._ask_text(text), "regarding this: Is the resource not idempotent?")
        self.assertIn(link._ask_text(text), text)
        self.assertEqual(link._ask_text("```\na?\n```\nplain"), "plain")


# --- 5. the story check's structure rules ------------------------------------

class TestStoryStructureCheck(unittest.TestCase):

    def setUp(self):
        b = enriched(ciphers_bundle())
        self.slice = link.slice_train(b, "train-issue-6")

    def check(self, text):
        return link.check_story_evidence(self.slice, text)

    GOOD = ("**Disabling weak ciphers in module m.** On 10 June 2026 alice "
            f"opened #1 ({URL}/pull/1). A reviewer asked \"Is this needed?\" "
            "on 13 June.\n\n"
            "**Where it stands.** As of 30 September 2026 #1 is open and the "
            "linked issue #9 is open; the next move is with eriqua (derived).")

    def test_a_complete_story_passes(self):
        r = self.check(self.GOOD)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["closing_paragraph"], "Where it stands")
        self.assertEqual(r["numbers_checked"], 2)

    def test_first_date_needs_a_year(self):
        r = self.check(self.GOOD.replace("10 June 2026", "10 June"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["first_date_without_year"], "10 June")
        r = self.check(self.GOOD.replace("10 June 2026", "June 10, 2026"))
        self.assertIsNone(r["first_date_without_year"])
        r = self.check(self.GOOD.replace("On 10 June 2026", "On 2026-06-10"))
        self.assertIsNone(r["first_date_without_year"])
        # a date inside a URL is not prose
        r = link.check_story_structure(
            self.slice, "See https://example.invalid/June-10 and 2026-06-10.")
        self.assertIsNone(r["first_date_without_year"])

    def test_closing_part_is_required_and_must_state_each_number(self):
        r = self.check(self.GOOD.split("\n\n")[0])
        self.assertTrue(r["closing_paragraph_missing"])
        r = self.check(self.GOOD.replace("and the linked issue #9 is open", "")
                       .replace("on 13 June.", "on 13 June; alice opened #9."))
        self.assertEqual(r["numbers_without_state"], [9])
        r = self.check(self.GOOD.replace("#9 is open", "#9 is closed"))
        self.assertEqual(r["state_mismatch"], [{"number": 9, "stated": ["closed"],
                                                "recorded": "open"}])
        self.assertFalse(r["ok"])

    def test_shipped_train_needs_what_landed(self):
        s = dict(self.slice, train=dict(self.slice["train"], outcome="shipped"))
        r = link.check_story_structure(s, self.GOOD)
        self.assertTrue(r["closing_paragraph_missing"])
        r = link.check_story_structure(
            s, self.GOOD.replace("**Where it stands.**", "### What landed\n"))
        self.assertFalse(r["closing_paragraph_missing"])

    def test_no_notable_discussion_is_exempt(self):
        r = self.check(link.STORY_NO_DISCUSSION)
        self.assertTrue(r["ok"], r)

    def test_a_link_to_the_item_names_it_too(self):
        story = (f"On 10 June 2026 {URL}/pull/1 was opened.\n\n"
                 f"Where it stands: {URL}/pull/1 is open.")
        r = self.check(story)
        self.assertTrue(r["ok"], r)
        r = self.check(story.replace("is open.", "has a pending review."))
        self.assertEqual(r["numbers_without_state"], [1])


# --- 6. docs contract ----------------------------------------------------------

class TestSubstanceDocumented(unittest.TestCase):
    """BUNDLE.md's story-layer section names every key the substance adds."""

    def test_every_key_is_documented(self):
        import re
        import test_module_changes as tmc
        from test_story import TestDocsContract
        section = TestDocsContract.section(None)
        b = ciphers_bundle()
        b["prs"][0]["review_comments"][0]["diff_hunk_lines"] = 40
        b["pr_history"]["prs"][0]["commits"][0].update(
            body_truncated_from=9000, paths_omitted=3)
        c = conversation(b, "train-issue-6")
        o = c["outcome"]
        keys = set(c) | set(o) | set(o["ball"]) | set(o["ball"]["basis"])
        for row in o["items"]:
            keys |= set(row)
        pr_row = next(r for r in o["items"] if r["type"] == "pr")
        keys |= set(pr_row["reviews"]) | set(pr_row["reviews"]["verdicts"][0])
        keys |= set(pr_row["reviews"]["outstanding_change_requests"][0])
        keys |= set(o["linked"][0]) | set(o["waiting_on"][0])
        net = o["net_change"]["1"]
        keys |= set(net) | {k for v in net["lines"].values() for k in v}
        for t in c["threads"]:
            keys |= set(t) | set(t.get("resolution") or {})
        for it in c["timeline"]:
            keys |= set(it) | set(it.get("change") or {})
            for p in (it.get("change") or {}).get("patches") or []:
                keys |= set(p)
        for tp in c["turning_points"]:
            keys |= set(tp.get("detail") or {})
        hist = b["pr_history"]["prs"][0]
        keys |= set(hist) | {k for cm in hist["commits"] for k in cm}
        keys |= {k for p in hist["patches"] for k in p}
        keys |= {"patches_omitted", "patches_skipped", "threads_omitted",
                 "hunks_omitted", "diff_hunk_omitted", "linked_omitted",
                 "in_view", "reverted", "lines_more", "files_more",
                 "interface_more"}
        mb = link.enrich(copy.deepcopy(tmc.window()))
        for t in mb["trains"]:
            for m in link._outcome_modules(mb, t):
                keys |= set(m) | set(m.get("versions") or {})
                for e in m.get("changelog") or []:
                    keys |= set(e) | {k for s in e["sections"] for k in s}
        missing = sorted(k for k in keys if not re.search(
            r"(?<![\w]){}(?![\w])".format(re.escape(k)), section))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
