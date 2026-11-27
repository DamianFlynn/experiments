"""The whole record of every PR (issue #97, the owner: "we need all the
commits to get the context, and for PRs these might be forked in from
outside"): checks for every commit, commit evidence for every PR and
commit, fork PRs, and the commits a force-push replaced.

The GraphQL answers are stubs in the documented response shapes
(PullRequest.commits with Commit.statusCheckRollup.state; Repository.object
with Commit.statusCheckRollup.contexts); the git tests drive REAL git
against a throwaway local `file://` repository."""
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
import validate  # noqa: E402
from test_pr_activity import (FakeGraphQL, _fact, _story_bundle, check_run,  # noqa: E402
                              clock, commit_node, gpr, pr_node)
from test_story import GIT_TEST_ENV, URL, bundle, git, pr, review  # noqa: E402
from test_story_substance import conversation  # noqa: E402

N = gather.normalize_check_context


def sha(i):
    return "{:040x}".format(i)


class HistoryGraphQL(FakeGraphQL):
    """FakeGraphQL plus the second phase: `history[n]` = [(sha, committed,
    state)] oldest first, served in pages; `contexts[sha]` = [nodes],
    served 100 at a time from a cursor."""

    def __init__(self, nodes, history=None, contexts=None, fail_pages=False):
        super().__init__(nodes)
        self.history = history or {}
        self.contexts = contexts or {}
        self.fail_pages = fail_pages
        self.pages = self.detail = 0

    def __call__(self, query, variables=None, errors=None):
        if "commits(first:" in query:
            self.pages += 1
            self.queries.append(query)
            if self.fail_pages:
                raise gather.GitHubAPIError("error: HTTP 502")
            hist = self.history.get(variables["number"], [])
            start = int(variables.get("cursor") or 0)
            page = hist[start:start + gather.PR_CHECKS_HISTORY_PAGE]
            more = start + len(page) < len(hist)
            return {"rateLimit": {"cost": 1, "remaining": 900},
                    "repository": {"pullRequest": {"commits": {
                        "totalCount": len(hist),
                        "pageInfo": {"hasNextPage": more,
                                     "endCursor": str(start + len(page))},
                        "nodes": [{"commit": {"oid": s, "committedDate": at,
                                              "statusCheckRollup": None if st is None
                                              else {"state": st}}}
                                  for s, at, st in page]}}}}
        if "object(oid:" in query:
            self.detail += 1
            self.queries.append(query)
            repo = {}
            import re
            for m in re.finditer(r'(c\d+): object\(oid: "([0-9a-f]{40})"\) \{ \.\.\. on '
                                 r'Commit \{ oid statusCheckRollup \{ state '
                                 r'contexts\(first: 100(?:, after: "(\d+)")?\)', query):
                alias, s, cur = m.group(1), m.group(2), int(m.group(3) or 0)
                nodes = self.contexts.get(s, [])
                page = nodes[cur:cur + 100]
                repo[alias] = {"oid": s, "statusCheckRollup": {"state": "FAILURE",
                    "contexts": {"totalCount": len(nodes),
                                 "pageInfo": {"hasNextPage": cur + len(page) < len(nodes),
                                              "endCursor": str(cur + len(page))},
                                 "nodes": page}}}
            return {"rateLimit": {"cost": 1, "remaining": 900}, "repository": repo}
        return super().__call__(query, variables)


def latest_ten(base=100, at="2026-09-20T00:00:00Z"):
    return [commit_node(sha(base + i), at, [check_run("t")]) for i in range(10)]


class TestEveryCommitsChecks(unittest.TestCase):

    def test_older_commits_by_state_and_contexts_where_not_a_success(self):
        hist = [(sha(i), "2026-09-0{}T00:00:00Z".format(1 + i % 9),
                 "SUCCESS" if i != 3 else "FAILURE") for i in range(15)]
        hist += [(sha(100 + i), "2026-09-20T00:00:00Z", "SUCCESS") for i in range(10)]
        node = pr_node(1, latest_ten(), total=25)
        g = HistoryGraphQL({1: node}, history={1: hist},
                           contexts={sha(3): [check_run("lint", "FAILURE")]})
        out = gather.collect_pr_activity([gpr(1)], g, "o", "r", clock=clock)
        row = out["prs"][0]
        self.assertEqual(row["history_status"], "complete")
        self.assertEqual(len(row["commits"]), 25)
        self.assertEqual(row["commits"][0], {"sha": sha(0), "committed_at":
                                             "2026-09-01T00:00:00Z", "rollup": "passing",
                                             "checks": [], "state_only": True,
                                             "state": "success"})
        failed = row["commits"][3]
        self.assertNotIn("state_only", failed)
        self.assertEqual((failed["rollup"], failed["checks"][0]["name"]),
                         ("failing", "lint"))
        self.assertEqual((g.pages, g.detail), (1, 1))
        self.assertEqual(out["requests"], 3)            # 1 batch + 1 page + 1 detail
        self.assertIsNone(validate._fact_shape_problem("pr_activity", out))

    def test_contexts_beyond_the_first_page_are_read(self):
        many = [check_run("c{}".format(i)) for i in range(160)]
        first = commit_node(sha(5), "2026-09-20T00:00:00Z", many[:50], total=160)
        first["commit"]["statusCheckRollup"]["contexts"]["pageInfo"]["endCursor"] = "50"
        g = HistoryGraphQL({1: pr_node(1, [first])}, contexts={sha(5): many})
        row = gather.collect_pr_activity([gpr(1)], g, "o", "r", clock=clock)["prs"][0]
        c = row["commits"][0]
        self.assertEqual((len(c["checks"]), c["rollup"]), (160, "passing"))
        self.assertNotIn("checks_omitted", c)
        self.assertNotIn("_cursor", c)

    def test_caps_and_failures_are_recorded(self):
        hist = [(sha(i), "2026-09-01T00:00:00Z", "SUCCESS") for i in range(30)]
        node = pr_node(1, latest_ten(), total=30)
        with mock.patch.dict(os.environ, {"ACTIVITY_PR_CHECKS_MAX_COMMITS": "12"}):
            row = gather.collect_pr_activity([gpr(1)], HistoryGraphQL(
                {1: node}, history={1: hist}), "o", "r", clock=clock)["prs"][0]
        self.assertEqual((row["history_status"], len(row["commits"])), ("capped", 12))
        self.assertIn("per-PR cap of 12", row["history_reason"])
        with mock.patch.dict(os.environ, {"ACTIVITY_PR_CHECKS_MAX_REQUESTS": "0"}):
            row = gather.collect_pr_activity([gpr(1)], HistoryGraphQL(
                {1: node}, history={1: hist}), "o", "r", clock=clock)["prs"][0]
        self.assertEqual(row["history_status"], "capped")
        self.assertIn("request budget", row["history_reason"])
        row = gather.collect_pr_activity([gpr(1)], HistoryGraphQL(
            {1: node}, history={1: hist}, fail_pages=True), "o", "r",
            clock=clock)["prs"][0]
        self.assertEqual((row["history_status"], len(row["commits"])), ("failed", 10))


class TestSliceReadsTheWholeHistory(unittest.TestCase):

    def test_state_only_commits_are_items_and_complete(self):
        f = _fact(commits_total=3)
        f["prs"][0]["commits"].insert(0, {
            "sha": sha(1), "committed_at": "2026-05-01T00:00:00Z", "rollup": "passing",
            "checks": [], "state_only": True, "state": "success"})
        f["prs"][0]["history_status"] = "complete"
        c = conversation(_story_bundle(f))
        item = next(i for i in c["timeline"] if i.get("sha") == sha(1))
        self.assertEqual((item["kind"], item["rollup"], item["state_only"]),
                         ("checks", "passing", True))
        ch = c["outcome"]["items"][0]["checks"]
        self.assertEqual((ch["commits_covered"], ch["commits_total"]), (3, 3))
        self.assertNotIn("commits_incomplete", ch)


class TestChecksAwaitingApproval(unittest.TestCase):

    def test_action_required_is_the_maintainers_move(self):
        f = _fact()
        f["prs"][0]["commits"][1]["checks"] = [N(check_run(
            "deploy", "ACTION_REQUIRED", started="2026-05-20T00:01:00Z",
            completed="2026-05-20T00:02:00Z"))]
        b = _story_bundle(f)
        b["prs"][0]["reviews"] = []
        b["prs"][0]["review_comments"] = []
        c = conversation(b)
        ch = c["outcome"]["items"][0]["checks"]
        self.assertEqual(ch["awaiting_approval"], ["deploy"])
        ball = c["outcome"]["items"][0]["ball"]
        self.assertEqual((ball["rule"], ball["state"]),
                         ("R6 checks awaiting approval", "awaiting maintainer"))


class TestForkRows(unittest.TestCase):

    def test_is_fork_from_head_repo(self):
        b = bundle([pr(1, head_repo="someone/r"), pr(2, head_repo="O/R"),
                    pr(3, head_repo=None), pr(4)])
        link.annotate_pr_forks(b)
        self.assertEqual([p.get("is_fork") for p in b["prs"]], [True, False, True, None])
        self.assertNotIn("is_fork", b["prs"][3])
        c = conversation(bundle([pr(1, head_repo="someone/r", comments_list=[],
                                    reviews=[review("bob", "2026-05-04T00:00:00Z",
                                                    "changes_requested", 3, "No")])]))
        row = c["outcome"]["items"][0]
        self.assertEqual((row["head_repo"], row["is_fork"]), ("someone/r", True))


class TestForkHistoryWithGit(unittest.TestCase):
    """A fork PR whose commits exist only under refs/pull/9/head (no branch
    of the base repository has them), by an author with no login, closed
    unmerged, force-pushed once; a second push whose old head is gone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-fork-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        src = self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        git("config", "uploadpack.allowFilter", "true", cwd=src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
        self.write("m/main.bicep", "param a string\n")
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "fork-work", cwd=src)
        self.write("m/main.bicep", "param a string\n// first try\nparam b string\n")
        self.old = self.commit("2026-05-03", "first try", author="Outside Person")
        git("checkout", "-q", "main", cwd=src)
        git("checkout", "-q", "-b", "fork-work-2", "main", cwd=src)
        self.write("m/main.bicep", "param a string\n// second try\nparam c string\n")
        self.new = self.commit("2026-05-05", "second try", author="Outside Person")
        git("checkout", "-q", "main", cwd=src)
        git("update-ref", "refs/pull/9/head", self.new, cwd=src)
        git("update-ref", "refs/keep/old", self.old, cwd=src)   # still served by sha
        git("branch", "-q", "-D", "fork-work", "fork-work-2", cwd=src)
        self.clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "--filter=blob:none", "--shallow-since=2026-03-02",
            "--single-branch", "file://" + src, self.clone)

    def write(self, rel, text):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, date, msg, author=None):
        git("add", "-A", cwd=self.src)
        args = ["commit", "-q", "-m", msg]
        if author:
            args += ["--author", "{} <outside@example.invalid>".format(author)]
        git(*args, cwd=self.src, date=date)
        return git("rev-parse", "HEAD", cwd=self.src).strip()

    def test_fork_commits_are_read_and_replaced_commits_listed(self):
        prs = [{"number": 9, "state": "closed", "merged": False}]  # no conversation
        pushes = {9: [{"event": "head_ref_force_pushed", "at": "2026-05-05T12:00:00Z",
                       "actor": "outsider", "before": self.old, "after": self.new},
                      {"event": "head_ref_force_pushed", "at": "2026-05-06T12:00:00Z",
                       "actor": "outsider", "before": "f" * 40, "after": self.new}]}
        row = gather.collect_pr_history(prs, self.clone, base_branch="main",
                                        all_prs=True, force_pushes=pushes)["prs"][0]
        self.assertEqual([c["subject"] for c in row["commits"]], ["second try"])
        self.assertEqual(row["commits"][0]["author"], "Outside Person")
        self.assertTrue(row["narrative_collected"])           # walked: all PRs
        self.assertNotIn("notes_skipped", row)
        read, gone = row["replaced"]
        self.assertEqual((read["status"], [c["subject"] for c in read["commits"]]),
                         ("read", ["first try"]))
        self.assertEqual(read["commits"][0]["paths"], ["m/main.bicep"])
        self.assertEqual(gone["status"], "unavailable")
        self.assertIn("not fetchable", gone["reason"])

    def test_without_all_prs_the_conversation_rule_stands(self):
        row = gather.collect_pr_history([{"number": 9}], self.clone,
                                        base_branch="main")["prs"][0]
        self.assertEqual(row["notes_skipped"], "no human conversation")


if __name__ == "__main__":
    unittest.main()
