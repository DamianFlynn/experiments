"""PR check results, review-thread state and lifecycle events (issue #97,
the owner's feedback on the sample stories: "Whether CI passes on the open
PR ... is in the GitHub graph history").

gather reads them into the per-window fact `pr_activity`: the lifecycle
events from the issue timeline enrich_pr already fetches, the checks /
thread state / force-push commits from one batched GraphQL request per
PR_ACTIVITY_BATCH PRs. The network is never touched: the GraphQL answers are
stubs in the documented response shapes (GitHub GraphQL reference:
PullRequest.commits / Commit.statusCheckRollup / StatusCheckRollup.contexts
with CheckRun and StatusContext nodes, PullRequest.reviewThreads,
PullRequest.timelineItems HEAD_REF_FORCE_PUSHED_EVENT; REST issue timeline
event objects for renamed / review_requested / review_dismissed /
convert_to_draft / head_ref_force_pushed)."""
import contextlib
import copy
import io
import json
import os
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
from test_story import URL, bundle, pr, review, train_of  # noqa: E402
from test_story_substance import (A, B, C, ciphers_bundle, conversation,  # noqa: E402
                                  enriched, rc)

T0 = "2026-09-10T08:00:00Z"


def check_run(name, conclusion="SUCCESS", status="COMPLETED",
              started="2026-09-10T08:01:00Z", completed="2026-09-10T08:09:00Z"):
    """A CheckRun node as StatusCheckRollup.contexts returns it."""
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "startedAt": started,
            "completedAt": completed,
            "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2"}


def status_ctx(context, state="SUCCESS", created="2026-09-10T08:02:00Z"):
    """A StatusContext node (a legacy commit status)."""
    return {"__typename": "StatusContext", "context": context, "state": state,
            "createdAt": created, "targetUrl": "https://ci.example/1"}


def commit_node(oid, committed, contexts, state="SUCCESS", total=None):
    roll = None if contexts is None else {
        "state": state,
        "contexts": {"totalCount": len(contexts) if total is None else total,
                     "pageInfo": {"hasNextPage": bool(total and total > len(contexts))},
                     "nodes": contexts}}
    return {"commit": {"oid": oid, "committedDate": committed,
                       "statusCheckRollup": roll}}


def pr_node(number, commits, head=None, threads=(), pushes=(), total=None):
    return {"number": number,
            "headRefOid": head or (commits[-1]["commit"]["oid"] if commits else None),
            "commits": {"totalCount": len(commits) if total is None else total,
                        "nodes": list(commits)},
            "reviewThreads": {"totalCount": len(threads), "nodes": list(threads)},
            "timelineItems": {"nodes": list(pushes)}}


def thread(comment_id, resolved=False, by=None, outdated=False, path="m/main.bicep",
           line=10):
    return {"isResolved": resolved, "isOutdated": outdated, "path": path,
            "line": line, "resolvedBy": {"login": by} if by else None,
            "comments": {"nodes": [{"databaseId": comment_id}]}}


def push_node(at, actor, before, after):
    return {"createdAt": at, "actor": {"login": actor},
            "beforeCommit": {"oid": before}, "afterCommit": {"oid": after}}


class FakeGraphQL:
    """Answers pr_activity_query from {number: pullRequest node}; records
    each query; `fail` lists 1-based request numbers that raise."""

    def __init__(self, nodes, fail=(), cost=6):
        self.nodes, self.fail, self.cost = nodes, set(fail), cost
        self.queries = []

    def __call__(self, query, variables=None):
        self.queries.append(query)
        if len(self.queries) in self.fail:
            raise gather.GitHubAPIError("error: HTTP 502 Bad Gateway")
        if "statusCheckRollup" not in query:          # another reader's query
            return {"repository": {}}
        repo = {}
        for line in query.splitlines():
            line = line.strip()
            if line.startswith("pr") and ": pullRequest(number: " in line:
                n = int(line.split("(number: ", 1)[1].split(")", 1)[0])
                repo["pr{}".format(n)] = copy.deepcopy(self.nodes.get(n))
        return {"rateLimit": {"cost": self.cost, "remaining": 900},
                "repository": repo}


def clock():
    import datetime
    return datetime.datetime(2026, 10, 2, 6, 0, 0, tzinfo=datetime.timezone.utc)


# --- 1. gather: the pure readers --------------------------------------------

class TestQueryAndNormalisation(unittest.TestCase):

    def test_query_aliases_every_pr_and_reads_the_rollup(self):
        q = gather.pr_activity_query([12, 3])
        self.assertLess(q.index("pr3: pullRequest(number: 3)"),
                        q.index("pr12: pullRequest(number: 12)"))
        for part in ("rateLimit { cost remaining }",
                     "commits(last: {})".format(gather.PR_CHECKS_COMMITS),
                     "statusCheckRollup", "contexts(first: {})".format(
                         gather.PR_CHECKS_CONTEXTS), "... on CheckRun",
                     "... on StatusContext", "reviewThreads(first: {})".format(
                         gather.PR_THREADS_FIRST), "isResolved", "resolvedBy",
                     "HEAD_REF_FORCE_PUSHED_EVENT", "beforeCommit", "afterCommit"):
            self.assertIn(part, q)

    def test_check_run_and_status_context(self):
        self.assertEqual(gather.normalize_check_context(check_run("lint", "FAILURE")), {
            "name": "lint", "kind": "check_run", "status": "completed",
            "conclusion": "failure", "started_at": "2026-09-10T08:01:00Z",
            "completed_at": "2026-09-10T08:09:00Z",
            "url": "https://github.com/o/r/actions/runs/1/job/2"})
        self.assertEqual(gather.normalize_check_context(status_ctx("license/cla")), {
            "name": "license/cla", "kind": "status", "status": "completed",
            "conclusion": "success", "started_at": "2026-09-10T08:02:00Z",
            "completed_at": "2026-09-10T08:02:00Z", "url": "https://ci.example/1"})
        pend = gather.normalize_check_context(status_ctx("x", "PENDING"))
        self.assertEqual((pend["status"], "conclusion" in pend), ("pending", False))
        queued = gather.normalize_check_context(
            check_run("y", None, "QUEUED", None, None))
        self.assertEqual(queued, {"name": "y", "kind": "check_run",
                                  "status": "queued",
                                  "url": "https://github.com/o/r/actions/runs/1/job/2"})
        self.assertIsNone(gather.normalize_check_context({"__typename": "Other"}))

    def test_rollup_rule(self):
        n = gather.normalize_check_context
        ok, bad = n(check_run("a")), n(check_run("b", "FAILURE"))
        skipped = n(check_run("c", "SKIPPED"))
        running = n(check_run("d", None, "IN_PROGRESS", completed=None))
        self.assertEqual(gather.checks_rollup([ok, skipped]),
                         {"rollup": "passing", "failing": [], "pending": [],
                          "passing": 2})
        self.assertEqual(gather.checks_rollup([ok, bad, running])["rollup"], "failing")
        self.assertEqual(gather.checks_rollup([ok, running]),
                         {"rollup": "pending", "failing": [], "pending": ["d"],
                          "passing": 1})
        self.assertEqual(gather.checks_rollup([])["rollup"], "none")
        for concl in ("TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            self.assertEqual(gather.checks_rollup(
                [n(check_run("x", concl))])["rollup"], "failing", concl)

    def test_rollup_as_of_an_instant(self):
        n = gather.normalize_check_context
        late = n(check_run("slow", "FAILURE", completed="2026-10-01T01:00:00Z"))
        future = n(check_run("next", started="2026-10-01T02:00:00Z",
                             completed="2026-10-01T03:00:00Z"))
        ok = n(check_run("fast"))
        r = gather.checks_rollup([ok, late, future], as_of="2026-09-30T23:59:59Z")
        self.assertEqual(r, {"rollup": "pending", "failing": [], "pending": ["slow"],
                             "passing": 1})
        self.assertEqual(gather.checks_rollup([ok, late, future])["rollup"], "failing")

    def test_parse_node(self):
        node = pr_node(5, [
            commit_node(A, "2026-09-10T08:00:00Z",
                        [check_run("tests", "FAILURE"), status_ctx("license/cla")],
                        state="FAILURE"),
            commit_node(B, "2026-09-11T08:00:00Z", [check_run("tests")], total=60),
            commit_node(C, "2026-09-12T08:00:00Z", None)],
            threads=[thread(101, True, "eriqua"), thread(103, outdated=True)],
            pushes=[push_node("2026-09-11T07:00:00Z", "alice", A, B)], total=14)
        got = gather.parse_pr_activity_node(node)
        self.assertEqual((got["head"], got["commits_total"]), (C, 14))
        self.assertEqual([(c["sha"], c["rollup"]) for c in got["commits"]],
                         [(A, "failing"), (B, "passing"), (C, "none")])
        self.assertEqual(got["commits"][0]["state"], "failure")
        self.assertEqual([k["name"] for k in got["commits"][0]["checks"]],
                         ["license/cla", "tests"])
        self.assertEqual(got["commits"][1]["checks_omitted"], 59)
        self.assertEqual(got["commits"][2]["checks"], [])
        self.assertNotIn("state", got["commits"][2])
        self.assertEqual(got["threads"], [
            {"comment_id": 101, "path": "m/main.bicep", "line": 10,
             "resolved": True, "outdated": False, "resolved_by": "eriqua"},
            {"comment_id": 103, "path": "m/main.bicep", "line": 10,
             "resolved": False, "outdated": True}])
        self.assertEqual(got["force_pushes"], [{"at": "2026-09-11T07:00:00Z",
                                                "actor": "alice", "before": A,
                                                "after": B}])

    def test_timeline_events(self):
        tl = [
            {"id": 1, "event": "renamed", "created_at": "2026-09-01T00:00:00Z",
             "actor": {"login": "alice"},
             "rename": {"from": "feat: x", "to": "feat: x (v0.1 scope)"}},
            {"id": 2, "event": "review_requested", "created_at": "2026-09-02T00:00:00Z",
             "actor": {"login": "alice"}, "review_requester": {"login": "alice"},
             "requested_reviewer": {"login": "bob"}},
            {"id": 3, "event": "review_requested", "created_at": "2026-09-02T00:00:01Z",
             "actor": {"login": "alice"}, "review_requester": {"login": "alice"},
             "requested_team": {"name": "Core", "slug": "core"}},
            {"id": 4, "event": "review_dismissed", "created_at": "2026-09-03T00:00:00Z",
             "actor": {"login": "maint"},
             "dismissed_review": {"state": "changes_requested", "review_id": 31,
                                  "dismissal_message": "Addressed"}},
            {"id": 5, "event": "convert_to_draft", "created_at": "2026-09-04T00:00:00Z",
             "actor": {"login": "alice"}},
            {"id": 6, "event": "head_ref_force_pushed",
             "created_at": "2026-09-05T00:00:00Z", "actor": {"login": "alice"},
             "commit_id": None, "commit_url": None},
            {"id": 7, "event": "labeled", "created_at": "2026-09-06T00:00:00Z",
             "actor": {"login": "alice"}, "label": {"name": "x"}},
            {"id": 8, "event": "ready_for_review", "created_at": "2026-09-06T00:00:00Z",
             "actor": {"login": "alice"}}]
        self.assertEqual(gather.parse_timeline_pr_events(tl), [
            {"id": 1, "event": "renamed", "at": "2026-09-01T00:00:00Z",
             "actor": "alice", "from": "feat: x", "to": "feat: x (v0.1 scope)"},
            {"id": 2, "event": "review_requested", "at": "2026-09-02T00:00:00Z",
             "actor": "alice", "requested": "bob", "by": "alice"},
            {"id": 3, "event": "review_requested", "at": "2026-09-02T00:00:01Z",
             "actor": "alice", "requested": "team:core", "by": "alice"},
            {"id": 4, "event": "review_dismissed", "at": "2026-09-03T00:00:00Z",
             "actor": "maint", "review_id": 31, "state": "changes_requested",
             "message": "Addressed"},
            {"id": 5, "event": "convert_to_draft", "at": "2026-09-04T00:00:00Z",
             "actor": "alice"},
            {"id": 6, "event": "head_ref_force_pushed", "at": "2026-09-05T00:00:00Z",
             "actor": "alice"}])


# --- 2. gather: collect_pr_activity -------------------------------------------

def gpr(n, state="closed", talk=0):
    return {"number": n, "state": state,
            "comments_list": [{"author": "x", "body": "?"}] * talk}


class TestCollect(unittest.TestCase):

    def test_every_pr_has_a_row_and_the_cap_prefers_open_then_discussed(self):
        prs = [gpr(1), gpr(2, talk=3), gpr(3, "open"), gpr(4, talk=1), gpr(5)]
        nodes = {n: pr_node(n, [commit_node(A, T0, [check_run("t")])])
                 for n in range(1, 6)}
        gql = FakeGraphQL(nodes)
        with mock.patch.object(gather, "PR_ACTIVITY_BATCH", 2):
            out = gather.collect_pr_activity(prs, gql, "o", "r", max_prs=3,
                                             clock=clock)
        status = {r["number"]: r["checks_status"] for r in out["prs"]}
        self.assertEqual(status, {1: "capped", 2: "gathered", 3: "gathered",
                                  4: "gathered", 5: "capped"})
        self.assertEqual((out["requests"], out["points"]), (2, 12))
        self.assertEqual(out["gathered_at"], "2026-10-02T06:00:00Z")
        self.assertEqual(out["limits"]["max_prs"], 3)
        capped = next(r for r in out["prs"] if r["number"] == 1)
        self.assertNotIn("commits", capped)        # never "no checks"
        self.assertEqual(next(r for r in out["prs"] if r["number"] == 3)
                         ["commits"][0]["rollup"], "passing")

    def test_failed_request_is_recorded_and_stops_after_three(self):
        prs = [gpr(n) for n in range(1, 7)]
        gql = FakeGraphQL({}, fail=(1, 2, 3))
        with mock.patch.object(gather, "PR_ACTIVITY_BATCH", 1):
            out = gather.collect_pr_activity(prs, gql, "o", "r", clock=clock)
        self.assertEqual(len(gql.queries), 3)
        rows = {r["number"]: r for r in out["prs"]}
        self.assertTrue(all(r["checks_status"] == "failed" for r in rows.values()))
        self.assertIn("HTTP 502", rows[6]["checks_reason"])   # newest asked first
        self.assertIn("not asked: 3 requests in a row failed",
                      rows[3]["checks_reason"])

    def test_null_pr_is_not_found_and_disabled_is_not_requested(self):
        out = gather.collect_pr_activity([gpr(1)], FakeGraphQL({1: None}), "o", "r",
                                         clock=clock)
        self.assertEqual(out["prs"][0]["checks_status"], "not_found")
        for kw in ({"enabled": False}, {}):
            g = FakeGraphQL({}) if kw else None
            out = gather.collect_pr_activity([gpr(1)], g, "o", "r", clock=clock, **kw)
            self.assertEqual(out["prs"][0]["checks_status"], "not_requested")
            self.assertEqual(out["requests"], 0)

    def test_force_push_events_get_their_commits(self):
        ev = {"id": 6, "event": "head_ref_force_pushed", "at": "2026-09-05T00:00:00Z",
              "actor": "alice"}
        nodes = {1: pr_node(1, [commit_node(B, T0, [])],
                            pushes=[push_node("2026-09-05T00:00:00Z", "alice", A, B)])}
        out = gather.collect_pr_activity([gpr(1)], FakeGraphQL(nodes), "o", "r",
                                         timeline_events={1: [ev]}, clock=clock)
        self.assertEqual(out["prs"][0]["events"],
                         [dict(ev, before=A, after=B)])

    def test_env_cap(self):
        self.assertEqual(gather._pr_checks_max({}), gather.PR_CHECKS_MAX_PRS)
        self.assertEqual(gather._pr_checks_max({"ACTIVITY_PR_CHECKS_MAX": "7"}), 7)
        with self.assertRaises(SystemExit):
            gather._pr_checks_max({"ACTIVITY_PR_CHECKS_MAX": "lots"})


# --- 3. acquire, fold, validate, extract ------------------------------------

def _raw_pr(n=1):
    return {"number": n, "title": "feat: x", "state": "open", "user": {"login": "alice"},
            "html_url": f"{URL}/pull/{n}", "created_at": "2026-05-02T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z", "merged_at": None,
            "closed_at": None, "body": "Adds x.", "labels": [],
            "base": {"ref": "main"}, "head": {"sha": B}}


class TestAcquire(unittest.TestCase):

    def _acquire(self, extra=(), graphql=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", "2026-05-01",
            "--to", "2026-05-31", "--clone-dir", os.path.join(tmp, "clone"),
            "--store", os.path.join(tmp, "s.db"), "--no-clone", "--no-workflows",
            "--no-releases", "--no-project-board", "--no-newcomers",
            "--no-affiliation"] + list(extra))
        urls = []

        def get_page(url):
            urls.append(url)
            if url == "https://api.github.com/repos/o/r":
                return {"default_branch": "main"}, None
            if "/pulls?state=open" in url:
                return [_raw_pr()], None
            if "/issues/1/timeline" in url:
                return [{"id": 9, "event": "renamed",
                         "created_at": "2026-05-03T00:00:00Z",
                         "actor": {"login": "alice"},
                         "rename": {"from": "a", "to": "feat: x"}}], None
            return [], None
        gql = graphql or FakeGraphQL({1: pr_node(1, [commit_node(
            B, "2026-05-02T00:00:00Z", [check_run("tests", "FAILURE")])])})
        err = io.StringIO()
        with mock.patch.object(gather, "_paginated", lambda token: get_page), \
                mock.patch.object(gather, "_graphql_client", lambda token: gql), \
                contextlib.redirect_stderr(err):
            b = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        return b, err.getvalue(), urls

    def test_acquire_writes_the_fact_with_no_extra_rest_call(self):
        b, err, urls = self._acquire()
        row = b["pr_activity"]["prs"][0]
        self.assertEqual(row["checks_status"], "gathered")
        self.assertEqual(row["events"][0]["event"], "renamed")
        self.assertEqual(row["commits"][0]["rollup"], "failing")
        self.assertNotIn("events", b["prs"][0])        # the PR record is unchanged
        self.assertEqual(len([u for u in urls if "/pulls/1/" in u
                              or "/issues/1/" in u]), 4)
        self.assertIn("pr activity: o/r: checks gathered for 1 PR(s), 0 capped", err)

    def test_no_pr_checks(self):
        gql = FakeGraphQL({})
        b, _err, _ = self._acquire(["--no-pr-checks"], graphql=gql)
        row = b["pr_activity"]["prs"][0]
        self.assertEqual(row["checks_status"], "not_requested")
        self.assertEqual(row["events"][0]["to"], "feat: x")
        self.assertFalse([q for q in gql.queries if "statusCheckRollup" in q])


def _fact(**row):
    base = {"number": 1, "checks_status": "gathered", "head": B,
            "commits_total": 2, "commits": [
                {"sha": A, "committed_at": "2026-05-02T00:00:00Z",
                 "rollup": "failing", "checks": [gather.normalize_check_context(
                     check_run("tests", "FAILURE", started="2026-05-02T00:01:00Z",
                               completed="2026-05-02T00:09:00Z"))]},
                {"sha": B, "committed_at": "2026-05-20T00:00:00Z",
                 "rollup": "passing", "checks": [gather.normalize_check_context(
                     check_run("tests", started="2026-05-20T00:01:00Z",
                               completed="2026-05-20T00:09:00Z"))]}],
            "threads_total": 1,
            "threads": [{"comment_id": 21, "path": "m/main.bicep", "line": 3,
                         "resolved": True, "resolved_by": "bob", "outdated": False}],
            "events": [{"id": 9, "event": "renamed", "at": "2026-05-03T00:00:00Z",
                        "actor": "alice", "from": "a", "to": "feat: x"}]}
    base.update(row)
    return {"collected": True, "gathered_at": "2026-06-02T06:00:00Z",
            "limits": {"batch": 10}, "requests": 1, "points": 6, "prs": [base]}


def _folded(fact, frm="2026-05-01", to="2026-05-31"):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    b = {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                  "period": {"from": frm, "to": to}, "ref_date": to,
                  "base_branch": "main", "branches": ["main"], "clone_sha": None},
         "prs": [pr(1, created_at="2026-05-02T00:00:00Z", comments_list=[],
                    review_comments=[], reviews=[])],
         "issues": [], "commits": [], "pr_activity": fact}
    gather.fold_bundle(conn, b)
    return conn


class TestStoreAndView(unittest.TestCase):

    def test_folded_validated_and_carried_into_the_view(self):
        conn = _folded(_fact())
        report = validate.validate(conn, project="o", repo="r")
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(v["pr_activity"], _fact())

    def test_shape_problems_are_meta_integrity_errors(self):
        for bad in (dict(_fact(), extra=1),
                    _fact(checks_status="maybe"),
                    _fact(threads=[{"comment_id": 1, "resolved": "yes"}]),
                    _fact(events=[{"event": "labeled"}])):
            self.assertIsNotNone(validate._fact_shape_problem("pr_activity", bad))
        self.assertIsNone(validate._fact_shape_problem("pr_activity", _fact()))
        # a pr_history value under the pr_activity key is a swapped ref
        self.assertIsNotNone(validate._fact_shape_problem(
            "pr_activity", {"collected": True, "base": "main", "prs": []}))
        self.assertIn("swapped", validate._fact_shape_problem(
            "pr_history", _fact()))

    def test_a_merged_view_takes_each_pr_from_the_latest_window(self):
        conn = _folded(_fact())
        later = _fact(checks_status="capped")
        later["prs"][0] = {"number": 1, "checks_status": "capped"}
        later["gathered_at"] = "2026-07-02T06:00:00Z"
        b = {"meta": {"owner": "o", "repo": "r", "from": "2026-06-01",
                      "to": "2026-06-30", "period": {"from": "2026-06-01",
                                                     "to": "2026-06-30"},
                      "ref_date": "2026-06-30", "base_branch": "main",
                      "branches": ["main"], "clone_sha": None},
             "prs": [], "issues": [], "commits": [], "pr_activity": later}
        gather.fold_bundle(conn, b)
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-06-30")
        self.assertEqual(v["pr_activity"]["prs"], [
            {"number": 1, "checks_status": "capped",
             "gathered_at": "2026-07-02T06:00:00Z"}])
        self.assertEqual(len(v["pr_activity"]["windows"]), 2)

    def test_an_older_store_has_no_fact(self):
        conn = _folded(None)
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertNotIn("pr_activity", v)


# --- 4. link: the slice ---------------------------------------------------------

def _story_bundle(fact=None, **prkw):
    p = pr(1, review_comments=[rc("bob", "2026-05-04T09:00:00Z",
                                  "Rename this parameter.", 21,
                                  path="m/main.bicep", line=3)],
           reviews=[review("bob", "2026-05-05T09:00:00Z", "approved", 32, "LGTM")],
           **prkw)
    b = bundle([p])
    b["pr_activity"] = fact if fact is not None else _fact()
    return b


class TestSlice(unittest.TestCase):

    def setUp(self):
        self.c = conversation(_story_bundle())

    def test_checks_and_events_on_the_timeline(self):
        tl = self.c["timeline"]
        checks = [i for i in tl if i["kind"] == "checks"]
        self.assertEqual([(i["sha"], i["rollup"], i.get("failing")) for i in checks],
                         [(A, "failing", ["tests"]), (B, "passing", None)])
        self.assertEqual(checks[0]["at"], "2026-05-02T00:09:00Z")
        self.assertTrue(checks[1]["head"])
        self.assertEqual(checks[0]["url"], f"{URL}/pull/1/commits/{A}")
        ren = next(i for i in tl if i["kind"] == "renamed")
        self.assertEqual((ren["from"], ren["to"], ren["url"]),
                         ("a", "feat: x", f"{URL}/pull/1#event-9"))

    def test_outcome_checks_replace_not_gathered(self):
        row = self.c["outcome"]["items"][0]
        self.assertEqual(row["checks"]["status"], "gathered")
        self.assertEqual((row["checks"]["head"], row["checks"]["rollup"],
                          row["checks"]["failing"]), (B, "passing", []))
        self.assertEqual(row["checks"]["head_url"], f"{URL}/pull/1/commits/{B}")

    def test_record_wins_for_a_resolved_thread(self):
        t = next(t for t in self.c["threads"] if t["kind"] == "review_thread")
        self.assertEqual(t["status"], "resolved on GitHub")
        self.assertEqual(t["resolved_on_github"]["by"], "bob")
        self.assertTrue(t["resolved_on_github"]["gathered_after_as_of"])
        self.assertIn("derived_status", t)
        self.assertIn(t["status"], link.STORY_THREAD_STATUSES)

    def test_not_gathered_and_gaps(self):
        ng = self.c["not_gathered"]
        self.assertFalse([x for x in ng if x.startswith(("check runs",
                                                          "whether a review"))])
        self.assertNotIn("gaps", self.c)
        old = conversation(bundle([pr(1, comments_list=[], reviews=[review(
            "bob", "2026-05-05T09:00:00Z", "changes_requested", 32, "No")])]))
        self.assertIn("check runs and their results per PR (CI pass / fail is "
                      "not stored)", old["not_gathered"])
        self.assertIn("are not in the store for PR(s) #1", old["gaps"][-1])
        self.assertEqual(old["outcome"]["items"][0]["checks"], "not gathered")

    def test_capped_checks_say_so(self):
        c = conversation(_story_bundle(_fact(checks_status="capped", commits=[],
                                             threads=[])))
        self.assertEqual(c["outcome"]["items"][0]["checks"],
                         {"status": "capped", "gathered_at": "2026-06-02T06:00:00Z"})
        self.assertIn("check results and review-thread resolved flags of PR(s) "
                      "#1 (capped) (not read)", c["not_gathered"])

    def test_checks_completed_after_the_instant_are_pending(self):
        f = _fact()
        f["prs"][0]["commits"][1]["checks"][0]["completed_at"] = "2026-06-01T01:00:00Z"
        c = conversation(_story_bundle(f))
        ch = c["outcome"]["items"][0]["checks"]
        self.assertEqual((ch["rollup"], ch["pending"], ch["completed_after_as_of"]),
                         ("pending", ["tests"], 1))

    def test_check_items_do_not_end_a_silence(self):
        f = _fact()
        f["prs"][0]["commits"].append(
            {"sha": C, "committed_at": "2026-05-04T00:00:00Z", "rollup": "passing",
             "checks": [gather.normalize_check_context(check_run(
                 "t", started="2026-05-25T00:00:00Z",
                 completed="2026-05-25T00:10:00Z"))]})
        c = conversation(_story_bundle(f))
        self.assertFalse([t for t in c["turning_points"]
                          if t["rule"] == "silence_then_activity"
                          and c["timeline"][t["index"]]["kind"] == "checks"])


class TestBall(unittest.TestCase):

    def ev(self, kind, actor, **kw):
        return dict({"at": "2026-05-10T00:00:00Z", "kind": kind, "actor": actor,
                     "url": "u"}, **kw)

    def test_failing_head_puts_the_ball_with_the_author(self):
        p = pr(1)
        failing = {"status": "gathered", "head": B, "rollup": "failing",
                   "failing": ["tests"]}
        b = link.ball_with(p, [self.ev("commit", "Alice")], {}, ["bob"], failing)
        self.assertEqual((b["state"], b["with"], b["rule"]),
                         ("awaiting author", ["alice"], "R5 failing checks"))
        self.assertEqual(b["checks_basis"], {"head": B, "failing": ["tests"]})
        ok = link.ball_with(p, [self.ev("review", "bob", state="approved")], {}, [],
                            failing)
        self.assertEqual(ok["rule"], "R5 failing checks")
        named = link.ball_with(p, [self.ev("comment", "bob", text="@carol?")], {},
                               [], failing)
        self.assertEqual(named["rule"], "R1 mention")
        passing = dict(failing, rollup="passing", failing=[])
        self.assertEqual(link.ball_with(p, [self.ev("commit", "A")], {}, ["bob"],
                                        passing)["rule"], "R2 author moved")

    def test_check_items_are_never_the_last_event(self):
        p = pr(1)
        b = link.ball_with(p, [self.ev("commit", "A"), self.ev("checks", None)],
                           {}, ["bob"])
        self.assertEqual(b["rule"], "R2 author moved")

    def test_review_request_names_the_requested_reviewer(self):
        b = link.ball_with(pr(1), [self.ev("review_requested", "alice",
                                           requested="carol")], {}, ["bob"])
        self.assertEqual((b["state"], b["with"]), ("awaiting reviewer", ["carol"]))


class TestStoryCheck(unittest.TestCase):

    def test_unknown_checks_or_resolution_are_refused_when_the_slice_has_them(self):
        b = enriched(_story_bundle(state="open"))
        s = link.slice_train(b, train_of(b)["id"])
        text = ("On 2 May 2026 alice opened #1. CI results are not in the record. "
                "Whether the thread was resolved is not recorded.\n\n"
                "Where it stands: #1 is open.")
        r = link.check_story_evidence(s, text)
        self.assertFalse(r["ok"])
        self.assertEqual(r["checks_said_unknown"], ["CI results are not in the record."])
        self.assertEqual(r["resolved_said_unknown"],
                         ["Whether the thread was resolved is not recorded."])
        good = ("On 2 May 2026 alice opened #1; checks failed on the first push "
                "and passed on the head.\n\nWhere it stands: #1 is open.")
        self.assertTrue(link.check_story_evidence(s, good)["ok"])

    def test_without_the_fact_saying_unknown_is_fine(self):
        b = enriched(ciphers_bundle())
        s = link.slice_train(b, "train-issue-6")
        r = link.check_story_record_claims(s, "CI results are not in the record.")
        self.assertEqual(r, {"checks_said_unknown": [], "resolved_said_unknown": []})


class TestDocumented(unittest.TestCase):

    def test_every_written_key_is_in_bundle_md(self):
        import re
        with open(os.path.join(os.path.dirname(__file__), "BUNDLE.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        f = _fact()
        row = f["prs"][0]
        c = conversation(_story_bundle())
        keys = (set(f) | set(row) | set(row["commits"][0])
                | set(row["commits"][0]["checks"][0]) | set(row["threads"][0])
                | set(row["events"][0]) | set(gather.PR_TIMELINE_EVENTS)
                | {k for i in c["timeline"] if i["kind"] == "checks" for k in i}
                | set(c["outcome"]["items"][0]["checks"])
                | {k for t in c["threads"] for k in t.get("resolved_on_github", {})}
                | {"derived_status", "resolved_on_github", "checks_basis",
                   "checks_reason", "events_omitted", "threads_omitted",
                   "checks_omitted", "completed_after_as_of"}
                | set(gather.PR_ACTIVITY_CHECKS_STATUSES))
        missing = sorted(k for k in keys if not re.search(
            r"(?<![\w]){}(?![\w])".format(re.escape(k)), text))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
