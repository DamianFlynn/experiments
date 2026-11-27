"""Review fixes on the PR-activity / narrative layer (issue #97): checks
coverage honesty, narrative reader edge cases, validator shapes. One test
class per review finding; each reproduces the finding before its fix."""
import contextlib
import copy
import io
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
from test_github_api import TOKEN, FakeOpener, FakeSleep, _Resp  # noqa: E402
from test_pr_activity import (FakeGraphQL, _fact, _story_bundle, check_run,  # noqa: E402
                              clock, commit_node, gpr, pr_node)
from test_pr_narrative import hunk  # noqa: E402
from test_story import GIT_TEST_ENV, URL, bundle, git, pr, train_of  # noqa: E402
from test_story_substance import A, B, C, commit, conversation, enriched  # noqa: E402

N = gather.normalize_check_context


# --- 1. block comments ---------------------------------------------------------

class TestBlockCommentBodies(unittest.TestCase):

    def test_powershell_help_block_with_plain_body_lines(self):
        h = hunk([("+", "<#"), ("+", ".SYNOPSIS"),
                  ("+", "Truncates the summary above 900KB."), ("+", "#>"),
                  (" ", "function Set-Summary {")])
        rows = gather.narrative_comments("powershell", [h])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "<#\n.SYNOPSIS\nTruncates the summary above 900KB.\n#>")
        self.assertEqual(rows[0]["decl"], "function Set-Summary")

    def test_bicep_block_with_plain_body_lines(self):
        h = hunk([("+", "/*"), ("+", "Private networking is out of scope:"),
                  ("+", "compose the resource modules instead."), ("+", "*/"),
                  (" ", "param name string")])
        rows = gather.narrative_comments("bicep", [h])
        self.assertEqual([r["text"] for r in rows], [
            "/*\nPrivate networking is out of scope:\n"
            "compose the resource modules instead.\n*/"])


# --- 2. / siblings: values compared whole --------------------------------------

class TestReaderKeepsValuesWhole(unittest.TestCase):

    def test_terraform_default_change_past_the_cap(self):
        b = 'variable "v" {\n  default = "%s"\n}\n' % ("x" * 250 + "a")
        a = 'variable "v" {\n  default = "%s"\n}\n' % ("x" * 250 + "b")
        rows = gather.diff_interface("m/variables.tf", gather.read_tf_interface(b),
                                     gather.read_tf_interface(a))
        self.assertEqual([r["change"] for r in rows], ["default"])
        self.assertTrue(rows[0]["after"].endswith("…"))

    def test_terraform_heredoc_and_description_past_the_cap(self):
        b = 'variable "v" {\n  description = <<EOT\n%s\nEOT\n}\n' % ("d" * 400 + "1")
        a = 'variable "v" {\n  description = <<EOT\n%s\nEOT\n}\n' % ("d" * 400 + "2")
        rows = gather.diff_interface("m/variables.tf", gather.read_tf_interface(b),
                                     gather.read_tf_interface(a))
        self.assertEqual([r["change"] for r in rows], ["description"])
        self.assertEqual(len(rows[0]["after"]),
                         gather.PR_NARRATIVE_DESCRIPTION_CHARS + 1)

    def test_bicep_description_and_type_past_the_cap(self):
        b = "@description('%s')\nparam p string\n" % ("d" * 400 + "1")
        a = "@description('%s')\nparam p string\n" % ("d" * 400 + "2")
        rows = gather.diff_interface("m/main.bicep", gather.read_bicep_interface(b),
                                     gather.read_bicep_interface(a))
        self.assertEqual([r["change"] for r in rows], ["description"])


# --- 3. responses must come after the comment ----------------------------------

class TestResponsesAfterTheComment(unittest.TestCase):

    def test_comment_on_an_older_commit_ignores_commits_made_before_it(self):
        commits = [commit(A, "2026-09-01T00:00:00Z", "a"),
                   commit(B, "2026-09-03T00:00:00Z", "b, before the comment"),
                   commit(C, "2026-09-06T00:00:00Z", "c, after it")]
        h = hunk([("-", "param firewallRules array")], old_start=9, new_start=9)
        files = {B: {"m/main.bicep": [h]}, C: {"m/main.bicep": [h]}}
        c = {"id": 1, "created_at": "2026-09-05T00:00:00Z", "path": "m/main.bicep",
             "line": 9, "commit": A,
             "diff_hunk": "@@ -9 +9 @@\n+param firewallRules array"}
        got = gather.review_responses([c], commits, files)
        self.assertEqual([x["sha"] for x in got[0]["commits"]], [C])
        self.assertNotIn(B, got[0].get("file_commits", []))


# --- 4. / 5. renames and unreadable versions (real git) -------------------------

class _Repo(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-fix-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", self.src)
        git("config", "uploadpack.allowFilter", "true", cwd=self.src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.src)

    def write(self, rel, text):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, date, msg):
        git("add", "-A", cwd=self.src)
        git("commit", "-q", "-m", msg, cwd=self.src, date=date)
        return git("rev-parse", "HEAD", cwd=self.src).strip()

    def clone_and_collect(self, head):
        git("update-ref", "refs/pull/7/head", head, cwd=self.src)
        clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "--filter=blob:none", "--shallow-since=2026-03-02",
            "--single-branch", "file://" + self.src, clone)
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "why?"}]}]
        return gather.collect_pr_history(prs, clone, base_branch="main")["prs"][0]


MODULE = "".join("param p{0} string = '{0}'\n".format(i) for i in range(20))


class TestRenamedModuleFile(_Repo):

    def test_a_renamed_file_is_compared_with_its_old_path(self):
        self.write("m/web/main.bicep", MODULE)
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=self.src)
        os.remove(os.path.join(self.src, "m/web/main.bicep"))
        self.write("m/app/main.bicep", MODULE.replace("= '19'", "= 'changed'"))
        head = self.commit("2026-05-03", "move the module")
        git("checkout", "-q", "main", cwd=self.src)
        row = self.clone_and_collect(head)
        iface = row["commits"][0].get("interface") or []
        self.assertEqual([(r["name"], r["change"]) for r in iface],
                         [("p19", "default")])


class TestUnreadableVersion(_Repo):

    def test_a_version_over_the_size_limit_is_skipped_not_all_removed(self):
        self.write("m/main.bicep", MODULE)
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=self.src)
        self.write("m/main.bicep", MODULE + "// " + "x" * 3000 + "\nparam q string\n")
        head = self.commit("2026-05-03", "grow")
        git("checkout", "-q", "main", cwd=self.src)
        with mock.patch.object(gather, "PR_NARRATIVE_BLOB_BYTES", 2000):
            row = self.clone_and_collect(head)
        c = row["commits"][0]
        self.assertNotIn("interface", c)
        self.assertEqual(c["interface_unread"], ["m/main.bicep"])

    def test_an_added_file_is_still_all_added(self):
        self.write("x.txt", "base\n")
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=self.src)
        self.write("m/main.bicep", "param q string\n")
        head = self.commit("2026-05-03", "new module")
        git("checkout", "-q", "main", cwd=self.src)
        row = self.clone_and_collect(head)
        self.assertEqual([(r["name"], r["change"])
                          for r in row["commits"][0]["interface"]], [("q", "added")])


# --- 6. GraphQL field errors ------------------------------------------------------

class ErroringGraphQL(FakeGraphQL):
    """Answers like GitHub with partial data: `errors` names the failed
    field's path; the failed field comes back null."""

    def __init__(self, nodes, failed):
        super().__init__(nodes)
        self.failed = failed

    def __call__(self, query, variables=None, errors=None):
        data = super().__call__(query, variables)
        for n in self.failed:
            key = "pr{}".format(n)
            if data["repository"].get(key):
                data["repository"][key]["commits"] = None
                if errors is not None:
                    errors.append({"message": "Something went wrong",
                                   "path": ["repository", key, "commits"]})
        return data


class TestGraphqlFieldErrors(unittest.TestCase):

    def test_post_hands_errors_to_a_caller_that_asks(self):
        data = {"repository": {"pr1": None}}
        errs = []
        with contextlib.redirect_stderr(io.StringIO()):
            out = gather.graphql_post(TOKEN, "query{}", {}, opener=FakeOpener([_Resp(
                {"data": data, "errors": [{"message": "boom",
                                           "path": ["repository", "pr1"]}]})]),
                sleep=FakeSleep(), clock=lambda: 1000.0, errors=errs)
        self.assertEqual(out, data)
        self.assertEqual(errs, [{"message": "boom", "path": ["repository", "pr1"]}])

    def test_a_pr_whose_field_failed_is_failed_not_gathered(self):
        nodes = {n: pr_node(n, [commit_node(A, "2026-09-10T08:00:00Z",
                                            [check_run("t")])]) for n in (1, 2)}
        out = gather.collect_pr_activity([gpr(1), gpr(2)], ErroringGraphQL(nodes, [2]),
                                         "o", "r", clock=clock)
        rows = {r["number"]: r for r in out["prs"]}
        self.assertEqual(rows[1]["checks_status"], "gathered")
        self.assertEqual(rows[2]["checks_status"], "failed")
        self.assertIn("Something went wrong", rows[2]["checks_reason"])
        self.assertNotIn("commits", rows[2])


# --- 7. a partial page of checks is never a pass ---------------------------------

class TestIncompleteChecks(unittest.TestCase):

    def test_rollup_with_omitted_contexts(self):
        ok = [N(check_run("a"))]
        self.assertEqual(gather.checks_rollup(ok, omitted=3)["rollup"], "incomplete")
        bad = ok + [N(check_run("b", "FAILURE"))]
        self.assertEqual(gather.checks_rollup(bad, omitted=3)["rollup"], "failing")
        self.assertIn("incomplete", gather.CHECK_ROLLUPS)

    def test_gather_and_slice_say_incomplete(self):
        node = pr_node(1, [commit_node(A, "2026-05-02T00:00:00Z",
                                       [check_run("a", started="2026-05-02T00:01:00Z",
                                                  completed="2026-05-02T00:09:00Z")],
                                       total=60)])
        got = gather.parse_pr_activity_node(node)
        self.assertEqual(got["commits"][0]["checks_omitted"], 59)
        self.assertEqual(gather.checks_rollup(
            got["commits"][0]["checks"],
            omitted=got["commits"][0]["checks_omitted"])["rollup"], "incomplete")
        f = _fact()
        f["prs"][0]["commits"][1]["checks_omitted"] = 9
        c = conversation(_story_bundle(f))
        self.assertEqual(c["outcome"]["items"][0]["checks"]["rollup"], "incomplete")
        item = next(i for i in c["timeline"] if i["kind"] == "checks" and i.get("head"))
        self.assertEqual(item["rollup"], "incomplete")


# --- 8. as-of coverage ------------------------------------------------------------

class TestAsOfCoverage(unittest.TestCase):

    def test_no_covered_commit_by_the_instant_is_unavailable(self):
        f = _fact(commits_total=14)
        for c in f["prs"][0]["commits"]:
            c["committed_at"] = "2026-06-10T00:00:00Z"
        c = conversation(_story_bundle(f))
        ch = c["outcome"]["items"][0]["checks"]
        self.assertEqual(ch["rollup"], "unavailable")
        self.assertIn("committed after the as-of instant", ch["reason"])
        b = enriched(_story_bundle(f, state="open"))
        s = link.slice_train(b, train_of(b)["id"])
        r = link.check_story_record_claims(
            s, "CI results for #1 as of the window's end are not in the record.")
        self.assertEqual(r["checks_said_unknown"], [])


# --- 9. claims scoped to the PR they name -----------------------------------------

class TestScopedClaims(unittest.TestCase):

    def slice_two(self):
        f = _fact()
        f["prs"].append({"number": 2, "checks_status": "capped"})
        p1 = _story_bundle(f, state="open")["prs"][0]
        p2 = pr(2, title="feat: other", created_at="2026-05-03T00:00:00Z")
        b = bundle([p1, p2], pr_activity=f)
        b["trains"] = None
        b = enriched(b)
        t = next(t for t in b["trains"] if 1 in (t.get("prs") or []))
        t["prs"] = [1, 2]
        return link.slice_train(b, t["id"])

    def test_unknown_claim_about_the_capped_pr_is_allowed(self):
        s = self.slice_two()
        r = link.check_story_record_claims(s, "CI results for #2 were not gathered.")
        self.assertEqual(r["checks_said_unknown"], [])
        r = link.check_story_record_claims(s, "CI results for #1 were not gathered.")
        self.assertEqual(len(r["checks_said_unknown"]), 1)
        r = link.check_story_record_claims(s, "CI results are not in the record.")
        self.assertEqual(r["checks_said_unknown"], [])     # coverage is partial


class TestPartialReadsAreNamed(unittest.TestCase):
    """Sibling of 7: a partial read of threads / events / commits is not
    reported as complete."""

    def test_not_gathered_names_the_partial_reads(self):
        c = conversation(_story_bundle(_fact(threads_omitted=3, events_omitted=2,
                                             commits_total=14)))
        ng = c["not_gathered"]
        self.assertIn("the resolved flag of 3 review thread(s) of #1 (beyond the "
                      "threads read)", ng)
        self.assertIn("2 earlier lifecycle event(s) of #1 (beyond the events "
                      "kept)", ng)
        self.assertIn("check results of the 12 earliest commit(s) of #1 (only the "
                      "latest 2 were read)", ng)


# --- 10. / 11. validator shapes -------------------------------------------------------

class TestValidatorShapes(unittest.TestCase):

    def test_pr_history_bad_containers_are_shape_errors(self):
        base = {"collected": True, "base": "main"}
        for bad in (dict(base, prs=1),
                    dict(base, prs=[{"number": 1, "commits": [1]}]),
                    dict(base, prs=[{"number": 1, "commits": 1}]),
                    dict(base, prs=[{"number": 1, "responses": [
                        {"commits": [1]}]}]),
                    dict(base, prs=[{"number": 1, "commits": [
                        {"docs": [{"sections": 1}]}]}]),
                    dict(base, prs=[{"number": 1, "responses": [
                        {"commits": [{"match": "anchor", "hunks": 1}]}]}])):
            self.assertIsNotNone(validate._fact_shape_problem("pr_history", bad), bad)

    def test_pr_activity_bad_containers_are_shape_errors(self):
        for row in ({"commits": 1}, {"threads": 1}, {"events": 1},
                    {"commits": [{"sha": A, "rollup": "passing", "checks": 1}]},
                    {"commits": [{"sha": A, "rollup": "passing", "checks": [1]}]}):
            bad = _fact(**row)
            self.assertIsNotNone(validate._fact_shape_problem("pr_activity", bad), row)
        self.assertIsNotNone(validate._fact_shape_problem(
            "pr_activity", {"collected": True, "gathered_at": "x", "prs": [1]}))


if __name__ == "__main__":
    unittest.main()
