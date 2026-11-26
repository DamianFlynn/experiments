"""Issue #105: a PR's closing issues come from GitHub, not only from keywords.

GitHub's GraphQL `PullRequest.closingIssuesReferences` lists every issue a PR
will close / closed, from closing keywords AND from manual "Development"
sidebar links. gather reads it in batches over the PRs it already lists and
merges it into `closes` (same repo) and the `closes`-kind qualified-ref path
(other repos); keyword parsing stays the fallback when GraphQL is unavailable,
and `meta.closing_refs_source` says which source was used. No network: the
GraphQL responses are fixture payloads (fixtures/graphql_closing_issues_sample.json).
"""

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

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
FROM, TO = "2026-05-01", "2026-05-31"


def _data():
    with open(os.path.join(FIX, "graphql_closing_issues_sample.json")) as fh:
        return json.load(fh)


class _FakeGraphQL:
    """Answers gather's closing-issue queries from the fixture; records calls."""

    def __init__(self, data=None):
        self.data = data or _data()
        self.calls = []

    def __call__(self, query, variables=None):
        self.calls.append((query, dict(variables or {})))
        if "after:$cursor" in query.replace(" ", ""):
            key = "page_2_pr{}".format(variables["number"])
            return copy.deepcopy(self.data[key])
        repo = {}
        for alias, value in self.data["response"]["repository"].items():
            if "{}:".format(alias) in query.replace(" ", ""):
                repo[alias] = copy.deepcopy(value)
        return {"repository": repo}


def _prs():
    return [gather.normalize_pr(raw) for raw in _data()["pulls"]]


def _issue(number):
    return {"number": number, "title": "Issue {}".format(number),
            "kind": "feature", "state": "closed", "state_reason": "completed",
            "labels": [], "milestone": None, "author": "rep",
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-10T00:00:00Z",
            "closed_at": "2026-05-10T00:00:00Z",
            "url": "https://github.com/o/r/issues/{}".format(number)}


def _resolve(prs, graphql, owner="o", repo="r"):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        source = gather.resolve_closing_issues(prs, graphql, owner, repo)
    return source, err.getvalue()


def _bundle(prs, issues, source=None):
    meta = {"owner": "o", "repo": "r", "from": FROM, "to": TO,
            "period": {"from": FROM, "to": TO}, "ref_date": TO,
            "base_branch": "main", "branches": ["main"]}
    if source is not None:
        meta["closing_refs_source"] = source
    return gather.build_bundle(meta, [], prs, issues)


def _store(bundle, **kw):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(bundle), **kw)
    return conn


def _view(conn):
    return link.enrich(extract.extract(conn, "o", "r", FROM, TO,
                                       warn=lambda _m: None))


def _bucket_train(view, kind, number):
    for bucket, refs in view["buckets"].items():
        for r in refs:
            if r["type"] == kind and r["id"] == number:
                return bucket, r.get("train")
    return None


class TestQuery(unittest.TestCase):
    def test_one_request_aliases_a_batch_of_prs(self):
        q = gather.closing_issues_query([3, 1])
        self.assertIn("pr1: pullRequest(number: 1)", q)
        self.assertIn("pr3: pullRequest(number: 3)", q)
        self.assertIn("closingIssuesReferences(first: {})".format(
            gather.CLOSING_ISSUES_FIRST), q)
        self.assertIn("nameWithOwner", q)
        self.assertIn("hasNextPage endCursor", q)

    def test_request_count_is_two_per_hundred_prs(self):
        gql = _FakeGraphQL({"response": {"repository": {}}})
        refs, reason = gather.fetch_closing_issues(gql, "o", "r", range(1, 101))
        self.assertIsNone(reason)
        self.assertEqual(len(gql.calls), 2)
        self.assertEqual(gather.CLOSING_ISSUES_BATCH, 50)

    def test_no_prs_no_request(self):
        gql = _FakeGraphQL()
        self.assertEqual(gather.fetch_closing_issues(gql, "o", "r", []), ({}, None))
        self.assertEqual(gql.calls, [])

    def test_parses_nodes_and_follows_a_long_connection(self):
        gql = _FakeGraphQL()
        refs, reason = gather.fetch_closing_issues(gql, "o", "r", [1, 3, 4, 5])
        self.assertIsNone(reason)
        self.assertEqual(refs[1], [{"owner": "o", "repo": "r", "number": 7}])
        self.assertEqual(refs[3], [         # GitHub's order, as reported
            {"owner": "o", "repo": "r", "number": 9},
            {"owner": "O", "repo": "R", "number": 5},
            {"owner": "o", "repo": "other", "number": 4}])
        self.assertNotIn(4, refs)          # null: not a PR, not checked
        self.assertEqual([r["number"] for r in refs[5]], [11, 12])
        page = gql.calls[-1]
        self.assertEqual(page[1], {"owner": "o", "repo": "r", "number": 5,
                                   "cursor": "Y3Vyc29yOjE="})


class TestApply(unittest.TestCase):
    def test_sidebar_only_link_joins_closes_and_leaves_the_mentions(self):
        pr = _prs()[0]
        pr["crossref_issues"] = [7, 3]       # the timeline mentioned #7 too
        gained = gather.apply_closing_issues(
            pr, [{"owner": "o", "repo": "r", "number": 7}], "o/r")
        self.assertEqual(gained, 1)
        self.assertEqual(pr["closes"], [7])
        self.assertEqual(pr["crossref_issues"], [3])
        self.assertEqual(pr["closing_issues"],
                         [{"owner": "o", "repo": "r", "number": 7}])

    def test_keywords_first_then_github_extras_ascending(self):
        pr = gather.normalize_pr(dict(_data()["pulls"][2], body="Fixes #9"))
        refs = [{"owner": "o", "repo": "r", "number": n} for n in (9, 6, 2)]
        self.assertEqual(gather.apply_closing_issues(pr, refs, "o/r"), 2)
        self.assertEqual(pr["closes"], [9, 2, 6])

    def test_nothing_new_leaves_the_record_byte_identical(self):
        pr = _prs()[1]                       # "Fixes #8"; GitHub reports #8
        before = json.dumps(pr, sort_keys=True)
        self.assertEqual(gather.apply_closing_issues(
            pr, [{"owner": "o", "repo": "r", "number": 8}], "o/r"), 0)
        self.assertEqual(json.dumps(pr, sort_keys=True), before)
        self.assertEqual(gather.apply_closing_issues(pr, [], "o/r"), 0)
        self.assertEqual(json.dumps(pr, sort_keys=True), before)

    def test_keyword_closed_cross_repo_ref_is_not_new(self):
        pr = gather.normalize_pr(dict(_data()["pulls"][0],
                                      body="Closes o/other#4"))
        before = json.dumps(pr, sort_keys=True)
        self.assertEqual(gather.apply_closing_issues(
            pr, [{"owner": "O", "repo": "Other", "number": 4}], "o/r"), 0)
        self.assertEqual(json.dumps(pr, sort_keys=True), before)

    def test_cross_repo_extra_is_stored_but_not_in_closes(self):
        pr = _prs()[2]                       # "Fixes #9", mentions O/Other#4
        refs = [{"owner": "o", "repo": "other", "number": 4},
                {"owner": "o", "repo": "r", "number": 9}]
        self.assertEqual(gather.apply_closing_issues(pr, refs, "o/r"), 1)
        self.assertEqual(pr["closes"], [9])
        self.assertEqual(pr["closing_issues"], sorted(
            refs, key=lambda r: (r["owner"].lower(), r["repo"].lower(),
                                 r["number"])))


class TestResolve(unittest.TestCase):
    def test_summary_line_and_source(self):
        prs = _prs()
        source, err = _resolve(prs, _FakeGraphQL())
        self.assertEqual(source, "graphql")
        # PR 1 gains #7; PR 3 gains #5 (same repo, other case) and o/other#4.
        self.assertIn("closing issues: 3 PR(s) checked via GraphQL, 2 gained 3 "
                      "closing issue(s) not in their text (source: graphql)\n", err)
        self.assertEqual([p["closes"] for p in prs], [[7], [8], [9, 5]])

    def test_graphql_error_falls_back_to_keywords(self):
        prs = _prs()
        before = json.dumps(prs, sort_keys=True)

        def boom(query, variables=None):
            raise gather.GitHubAPIError("error: GraphQL returned errors: denied")
        source, err = _resolve(prs, boom)
        self.assertEqual(source, "keywords")
        self.assertIn("closing issues: GraphQL unavailable (error: GraphQL "
                      "returned errors: denied), using closing keywords only\n", err)
        self.assertEqual(json.dumps(prs, sort_keys=True), before)

    def test_system_exit_from_a_legacy_callable_falls_back(self):
        def legacy(query, variables=None):
            raise SystemExit("HTTP 502")
        source, err = _resolve(_prs(), legacy)
        self.assertEqual(source, "keywords")
        self.assertIn("GraphQL unavailable (HTTP 502)", err)

    def test_no_client_falls_back(self):
        prs = _prs()
        source, err = _resolve(prs, None)
        self.assertEqual(source, "keywords")
        self.assertIn("closing issues: GraphQL unavailable (no GraphQL client), "
                      "using closing keywords only\n", err)
        self.assertEqual(prs[0]["closes"], [])

    def test_missing_field_falls_back(self):
        data = _data()
        del data["response"]["repository"]["pr2"]["closingIssuesReferences"]
        prs = _prs()
        source, err = _resolve(prs, _FakeGraphQL(data))
        self.assertEqual(source, "keywords")
        self.assertIn("GraphQL unavailable (no closingIssuesReferences for "
                      "PR #2), using closing keywords only", err)
        self.assertEqual(prs[0]["closes"], [])   # nothing half-applied

    def test_null_repository_falls_back(self):
        source, err = _resolve(_prs(), lambda q, v=None: {"repository": None})
        self.assertEqual(source, "keywords")
        self.assertIn("GraphQL unavailable (repository o/r not found)", err)


class TestSidebarLinkAnchorsATrain(unittest.TestCase):
    """Acceptance: a PR linked to issue #7 only through the Development
    sidebar gets #7 in `closes`, anchors train-issue-7, and #7's bucket ref
    carries that train; the trust check passes store-only."""

    def _bundle(self, graphql):
        prs = _prs()[:1]
        prs[0]["crossref_issues"] = []
        source, _ = _resolve(prs, graphql)
        return _bundle(prs, [_issue(7)], source)

    def test_sidebar_link_anchors_the_issue_train(self):
        view = _view(_store(self._bundle(_FakeGraphQL())))
        self.assertEqual([t["id"] for t in view["trains"]], ["train-issue-7"])
        self.assertEqual(view["trains"][0]["prs"], [1])
        self.assertEqual(_bucket_train(view, "issue", 7),
                         ("shipped", "train-issue-7"))
        self.assertEqual(_bucket_train(view, "pr", 1),
                         ("shipped", "train-issue-7"))

    def test_without_graphql_the_issue_has_no_train(self):
        b = self._bundle(None)
        self.assertEqual(b["meta"]["closing_refs_source"], "keywords")
        view = _view(_store(b))
        self.assertNotIn("train-issue-7", [t["id"] for t in view["trains"]])
        self.assertIsNone((_bucket_train(view, "issue", 7) or (None, None))[1])

    def test_trust_check_passes_store_only(self):
        conn = _store(self._bundle(_FakeGraphQL()))
        report = validate.validate(conn, project="o", repo="r")
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        edge = [c for c in report.checks if c["name"] == "edge_set"][0]
        self.assertTrue(edge["ok"])

    def test_meta_source_is_not_copied_into_the_view(self):
        view = _view(_store(self._bundle(_FakeGraphQL())))
        self.assertNotIn("closing_refs_source", view["meta"])


class TestCrossRepoClosingIssues(unittest.TestCase):
    """A GitHub-reported closing issue in another repo takes the path a
    keyword `Fixes owner/repo#N` takes: a `closes` edge to a member, an
    external `closes` ref otherwise. A mention of the same issue is dropped."""

    MEMBERS = {"o/r", "o/other"}

    def _a(self):
        prs = _prs()[2:3]                    # PR 3: "Fixes #9", mentions O/Other#4
        _resolve(prs, _FakeGraphQL())
        return _bundle(prs, [_issue(9), _issue(5)])

    def _b(self):
        iss = dict(_issue(4), url="https://github.com/o/other/issues/4")
        b = gather.build_bundle(
            {"owner": "o", "repo": "other", "from": FROM, "to": TO,
             "period": {"from": FROM, "to": TO}, "ref_date": TO,
             "base_branch": "main"}, [], [], [iss])
        return b

    def _project_store(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, self._a(), project="p", repo="o/r",
                           members=self.MEMBERS)
        gather.fold_bundle(conn, self._b(), project="p", repo="o/other",
                           members=self.MEMBERS)
        return conn

    def _edges(self, conn):
        return sorted((e["src_id"], e["dst_id"], e["edge_type"])
                      for e in conn.execute(
                          "SELECT src_id, dst_id, edge_type FROM edges"))

    def test_member_issue_gets_a_closes_edge_and_no_mention(self):
        edges = self._edges(self._project_store())
        pr = graphstore.qualify_id("p", "o/r", "pr-3")
        x = graphstore.qualify_id("p", "o/other", "issue-4")
        self.assertIn((pr, x, "closes"), edges)
        self.assertNotIn((pr, x, "cross_ref"), edges)
        for n in (9, 5):
            self.assertIn((pr, graphstore.qualify_id(
                "p", "o/r", "issue-{}".format(n)), "closes"), edges)

    def test_project_trust_check_passes_store_only(self):
        conn = self._project_store()
        repos = graphstore.project_repos(conn, "p")
        result = validate.validate_project(conn, "p", repos)
        self.assertTrue(result["ok"], json.dumps(result, indent=1, default=str))

    def test_non_member_becomes_an_external_closes_ref(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, self._a(), project="p", repo="o/r",
                           members={"o/r"})
        node = graphstore.get_node(conn, graphstore.qualify_id("p", "o/r", "pr-3"))
        self.assertEqual(node["data"]["external_refs"],
                         [{"repo": "o/other", "number": 4, "kind": "closes"}])

    def test_single_repo_fold_ignores_cross_repo_as_before(self):
        conn = _store(self._a())
        x = [e for e in self._edges(conn) if "other" in e[1]]
        self.assertEqual(x, [])


class TestTimelineConnected(unittest.TestCase):
    def test_connected_without_a_usable_number_is_not_a_link(self):
        raw = [{"event": "connected"},
               {"event": "connected", "subject": None},
               {"event": "connected", "subject": {"number": None}},
               {"event": "connected", "subject": {"number": "7"}},
               {"event": "connected", "subject": {"number": True}},
               {"event": "connected", "subject": {"number": 19}}]
        self.assertEqual(gather.parse_timeline_crossrefs(raw), [19])


class TestAcquireRecordsTheSource(unittest.TestCase):
    """acquire() end to end (no clone, REST and GraphQL stubbed)."""

    RAW_PR = _data()["pulls"][0]

    def _acquire(self, graphql):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", FROM, "--to", TO,
            "--clone-dir", os.path.join(tmp, "clone"),
            "--store", os.path.join(tmp, "s.db"), "--no-clone",
            "--no-workflows", "--no-releases", "--no-project-board"])
        fetched = []

        def get_page(url):
            if url == "https://api.github.com/repos/o/r":
                return {"default_branch": "main"}, None
            if "/pulls?state=closed" in url:
                return [copy.deepcopy(self.RAW_PR)], None
            return [], None

        def get_json(url, token, allow_404=False, **_kw):
            fetched.append(url)
            n = int(url.rsplit("/", 1)[1])
            return dict(_issue(n), user={"login": "rep"}, labels=[]), None

        err = io.StringIO()
        with mock.patch.object(gather, "_paginated", lambda token: get_page), \
                mock.patch.object(gather, "http_get_json", get_json), \
                mock.patch.object(gather, "_graphql_client",
                                  lambda token: graphql), \
                contextlib.redirect_stderr(err):
            bundle = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        return bundle, err.getvalue(), fetched

    def test_graphql_source_and_gained_issue_is_fetched(self):
        b, err, fetched = self._acquire(_FakeGraphQL())
        self.assertEqual(b["meta"]["closing_refs_source"], "graphql")
        self.assertEqual(b["prs"][0]["closes"], [7])
        self.assertIn("https://api.github.com/repos/o/r/issues/7", fetched)
        self.assertEqual([i["number"] for i in b["issues"]], [7])
        self.assertIn("closing issues: 1 PR(s) checked via GraphQL, 1 gained 1 "
                      "closing issue(s) not in their text (source: graphql)", err)

    def test_unavailable_graphql_is_recorded_and_does_not_fail(self):
        def boom(query, variables=None):
            raise gather.GitHubAPIError("error: HTTP 401 Bad credentials")
        b, err, _ = self._acquire(boom)
        self.assertEqual(b["meta"]["closing_refs_source"], "keywords")
        self.assertEqual(b["prs"][0]["closes"], [])
        self.assertIn("closing issues: GraphQL unavailable (error: HTTP 401 Bad "
                      "credentials), using closing keywords only", err)


if __name__ == "__main__":
    unittest.main()
