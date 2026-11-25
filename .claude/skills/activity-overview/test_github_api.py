"""GitHub API layer hardening (#56): timeouts + retries, rate-limit back-off,
no Authorization on foreign redirects, the workflow-runs 1,000-result cap,
board pagination degrading on API errors, and validated env knobs.

No network: every test injects a fake opener / sleep / clock."""
import contextlib
import email.message
import io
import json
import os
import socket
import sys
import unittest
import unittest.mock as mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402

TOKEN = "ghp_SECRETTOKEN123"


def _headers(d=None):
    h = email.message.Message()
    for k, v in (d or {}).items():
        h[k] = v
    return h


class _Resp:
    def __init__(self, body, headers=None, status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = _headers(headers)
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code, message="", headers=None):
    body = io.BytesIO(json.dumps({"message": message}).encode())
    return urllib.error.HTTPError(url, code, "err", _headers(headers), body)


class FakeOpener:
    """Plays a script of outcomes: a _Resp is returned, an exception raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req, timeout))
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, secs):
        self.calls.append(secs)


URL = "https://api.github.com/repos/o/r/pulls"


def _get(opener, sleep=None, clock=None, **kw):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = gather.http_get_json(URL, TOKEN, opener=opener,
                                   sleep=sleep or FakeSleep(),
                                   clock=clock or (lambda: 1000.0), **kw)
    return out, err.getvalue()


class TestTimeoutsAndRetries(unittest.TestCase):
    def test_every_request_passes_a_timeout(self):
        op = FakeOpener([_Resp([1])])
        with mock.patch.dict(os.environ, {"ACTIVITY_HTTP_TIMEOUT": "17"}):
            (body, nxt), _ = _get(op)
        self.assertEqual(body, [1])
        self.assertEqual(op.calls[0][1], 17)

    def test_default_timeout_is_60(self):
        op = FakeOpener([_Resp([1])])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACTIVITY_HTTP_TIMEOUT", None)
            _get(op)
        self.assertEqual(op.calls[0][1], 60)

    def test_timeout_is_retried_then_succeeds(self):
        sl = FakeSleep()
        op = FakeOpener([socket.timeout("timed out"),
                         urllib.error.URLError(TimeoutError("timed out")),
                         _Resp([7])])
        (body, _), err = _get(op, sleep=sl)
        self.assertEqual(body, [7])
        self.assertEqual(len(op.calls), 3)
        self.assertEqual(len(sl.calls), 2)          # backed off between tries
        self.assertLess(sl.calls[0], sl.calls[1])   # growing back-off
        self.assertIn("retrying", err)

    def test_timeout_exhausts_retries_with_clear_error(self):
        sl = FakeSleep()
        op = FakeOpener([socket.timeout("timed out")] * 4)
        with mock.patch.dict(os.environ, {"ACTIVITY_HTTP_RETRIES": "3"}):
            with self.assertRaises(SystemExit) as cm:
                _get(op, sleep=sl)
        msg = str(cm.exception.code)
        self.assertIn("4 attempt", msg)
        self.assertIn(URL, msg)
        self.assertNotIn(TOKEN, msg)
        self.assertEqual(len(op.calls), 4)

    def test_5xx_is_retried(self):
        op = FakeOpener([_http_error(URL, 502, "Bad Gateway"), _Resp({"ok": 1})])
        (body, _), _ = _get(op)
        self.assertEqual(body, {"ok": 1})

    def test_404_with_allow_404_is_not_retried(self):
        op = FakeOpener([_http_error(URL, 404, "Not Found")])
        (body, nxt), _ = _get(op, allow_404=True)
        self.assertEqual((body, nxt), (None, 404))
        self.assertEqual(len(op.calls), 1)

    def test_next_link_is_returned(self):
        op = FakeOpener([_Resp([1], {"Link": '<https://api.github.com/x?page=2>; rel="next"'})])
        (_, nxt), _ = _get(op)
        self.assertEqual(nxt, "https://api.github.com/x?page=2")


class TestRateLimits(unittest.TestCase):
    def test_primary_limit_waits_until_reset_and_continues(self):
        sl = FakeSleep()
        op = FakeOpener([
            _http_error(URL, 403, "API rate limit exceeded",
                        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1120"}),
            _Resp([1])])
        (body, _), err = _get(op, sleep=sl, clock=lambda: 1000.0)
        self.assertEqual(body, [1])
        self.assertEqual(len(sl.calls), 1)
        self.assertGreaterEqual(sl.calls[0], 120)
        self.assertLessEqual(sl.calls[0], 125)
        self.assertIn("primary rate limit", err)
        self.assertIn("1970-01-01T00:18", err)   # announces "until" (UTC)
        self.assertNotIn(TOKEN, err)

    def test_secondary_limit_honours_retry_after(self):
        sl = FakeSleep()
        op = FakeOpener([
            _http_error(URL, 429, "You have exceeded a secondary rate limit",
                        {"Retry-After": "42", "x-ratelimit-remaining": "4000"}),
            _Resp([1])])
        (body, _), err = _get(op, sleep=sl)
        self.assertEqual(body, [1])
        self.assertEqual(sl.calls, [42])
        self.assertIn("secondary rate limit", err)

    def test_secondary_limit_without_headers_waits_a_minute(self):
        sl = FakeSleep()
        op = FakeOpener([
            _http_error(URL, 403, "You have exceeded a secondary rate limit. "
                        "Please wait a few minutes before you try again."),
            _Resp([1])])
        (body, _), _ = _get(op, sleep=sl)
        self.assertEqual(body, [1])
        self.assertEqual(sl.calls, [60])

    def test_genuine_403_fails_fast_with_diagnostic(self):
        sl = FakeSleep()
        op = FakeOpener([_http_error(
            URL, 403, "Resource protected by organization SAML enforcement.",
            {"x-github-sso": "required; organizations=Azure",
             "x-ratelimit-remaining": "4999"})])
        with self.assertRaises(SystemExit) as cm:
            _get(op, sleep=sl)
        msg = str(cm.exception.code)
        self.assertIn("403", msg)
        self.assertIn("SAML", msg)
        self.assertIn("x-github-sso", msg)
        self.assertEqual(sl.calls, [])
        self.assertEqual(len(op.calls), 1)

    def test_wait_longer_than_cap_fails_instead_of_sleeping(self):
        sl = FakeSleep()
        op = FakeOpener([_http_error(
            URL, 403, "API rate limit exceeded",
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"})])
        with mock.patch.dict(os.environ, {"ACTIVITY_RATE_LIMIT_MAX_WAIT": "60"}):
            with self.assertRaises(SystemExit) as cm:
                _get(op, sleep=sl, clock=lambda: 1000.0)
        self.assertEqual(sl.calls, [])
        msg = str(cm.exception.code)
        self.assertIn("ACTIVITY_RATE_LIMIT_MAX_WAIT", msg)
        self.assertIn("x-ratelimit-remaining: 0", msg)

    def test_rate_limit_waits_are_bounded(self):
        sl = FakeSleep()
        op = FakeOpener([_http_error(URL, 429, "secondary rate limit",
                                     {"Retry-After": "1"}) for _ in range(20)])
        with self.assertRaises(SystemExit):
            _get(op, sleep=sl)
        self.assertEqual(len(sl.calls), gather.RATE_LIMIT_MAX_WAITS)


class TestRedirectAuth(unittest.TestCase):
    def _redirect(self, newurl):
        h = gather._GitHubRedirectHandler()
        req = urllib.request.Request(URL, headers={
            "Authorization": "Bearer " + TOKEN, "Accept": "x"})
        return h.redirect_request(req, None, 302, "Found", _headers(), newurl)

    def test_redirect_to_other_host_drops_authorization(self):
        new = self._redirect("https://objects.example.net/blob?sig=1")
        self.assertIsNone(new.get_header("Authorization"))
        self.assertNotIn(TOKEN, json.dumps(dict(new.header_items())))

    def test_redirect_within_api_github_keeps_authorization(self):
        new = self._redirect("https://api.github.com/repositories/1/pulls")
        self.assertEqual(new.get_header("Authorization"), "Bearer " + TOKEN)

    def test_redirect_to_http_downgrade_drops_authorization(self):
        new = self._redirect("http://api.github.com/repositories/1/pulls")
        self.assertIsNone(new.get_header("Authorization"))

    def test_default_opener_uses_the_stripping_handler(self):
        op = gather._default_opener()
        self.assertTrue(any(isinstance(h, gather._GitHubRedirectHandler)
                            for h in op.handlers))

    def test_token_never_sent_to_non_github_url(self):
        op = FakeOpener([_Resp([1])])
        with contextlib.redirect_stderr(io.StringIO()):
            gather.http_get_json("https://elsewhere.example/x", TOKEN, opener=op,
                                 sleep=FakeSleep(), clock=lambda: 0.0)
        self.assertIsNone(op.calls[0][0].get_header("Authorization"))


class TestGraphqlPost(unittest.TestCase):
    def _post(self, script, **kw):
        op = FakeOpener(script)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = gather.graphql_post(TOKEN, "query{}", {}, opener=op,
                                      sleep=kw.get("sleep", FakeSleep()),
                                      clock=lambda: 1000.0)
        return out, err.getvalue(), op

    def test_http_error_is_a_normal_exception(self):
        with self.assertRaises(gather.GitHubAPIError) as cm:
            self._post([_http_error(gather.GRAPHQL_URL, 401, "Bad credentials")])
        self.assertIsInstance(cm.exception, Exception)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_errors_without_data_raise_normal_exception(self):
        with self.assertRaises(gather.GitHubAPIError):
            self._post([_Resp({"data": None, "errors": [{"message": "nope"}]})])

    def test_partial_data_is_kept_with_warning(self):
        data = {"repository": {"projectsV2": {"nodes": [{"id": "A"}, None]}}}
        out, err, _ = self._post([_Resp({"data": data, "errors": [
            {"message": "Resource not accessible by integration"}]})])
        self.assertEqual(out, data)
        self.assertIn("Resource not accessible", err)

    def test_graphql_rate_limited_waits_and_retries(self):
        sl = FakeSleep()
        out, err, op = self._post([
            _Resp({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
                  {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}),
            _Resp({"data": {"ok": 1}})], sleep=sl)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(sl.calls), 1)
        self.assertGreaterEqual(sl.calls[0], 30)


class TestBoardDegrades(unittest.TestCase):
    def _board(self):
        return {"id": "B1", "number": 1, "title": "b", "fields": {"nodes": []},
                "items": {"pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                          "nodes": [{"content": {
                              "__typename": "Issue", "number": 3,
                              "repository": {"nameWithOwner": "acme/widget"}},
                              "fieldValues": {"nodes": []}}]}}

    def _run(self, exc):
        board = self._board()

        def graphql(query, variables=None):
            if variables and variables.get("id"):
                raise exc
            return {"repository": {"projectsV2": {"nodes": [board]}}}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = gather.fetch_project_board(graphql, "acme", "widget")
        return out, err.getvalue()

    def test_systemexit_on_page_two_degrades(self):
        (sprints, items), err = self._run(SystemExit("error: GitHub API 502"))
        self.assertIn(("acme/widget", 3), items)   # page 1 kept
        self.assertIn("partial Projects v2 board", err)

    def test_api_error_on_page_two_degrades(self):
        (sprints, items), err = self._run(gather.GitHubAPIError("GitHub API 502"))
        self.assertIn(("acme/widget", 3), items)

    def test_null_board_in_partial_data_is_skipped(self):
        board = self._board()
        board["items"]["pageInfo"] = {"hasNextPage": False, "endCursor": None}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sprints, items = gather.fetch_project_board(
                lambda q, v=None: {"repository": {"projectsV2": {"nodes": [None, board]}}},
                "acme", "widget")
        self.assertIn(("acme/widget", 3), items)


class TestWorkflowRunCap(unittest.TestCase):
    """GitHub's documented cap: actions/runs with `created=` returns at most
    1,000 results per search. A capped range is split by day."""

    @staticmethod
    def _api(per_day, cap=1000, lie_total=False):
        """Fake get_page serving `per_day` {date: n_runs}. Each query returns
        total_count = true count but only min(n, cap) runs (pages of 100)."""
        import datetime as dt
        import urllib.parse as up
        calls = []

        def runs_for(lo, hi):
            out = []
            for day in sorted(per_day, reverse=True):
                if lo <= day <= hi:
                    out += [{"id": "%s-%d" % (day, i),
                             "created_at": day + "T12:00:00Z",
                             "name": "ci", "conclusion": "success"}
                            for i in range(per_day[day])]
            return out

        def get_page(url):
            calls.append(url)
            q = up.parse_qs(up.urlsplit(url).query)
            lo, hi = q["created"][0].split("..")
            page = int(q.get("page", ["1"])[0])
            allr = runs_for(lo[:10], hi[:10])
            served = allr[:cap]
            chunk = served[(page - 1) * 100: page * 100]
            nxt = None
            if page * 100 < len(served):
                base = url.split("&page=")[0]
                nxt = base + "&page=%d" % (page + 1)
            total = min(len(allr), cap) if lie_total else len(allr)
            return {"total_count": total, "workflow_runs": chunk}, nxt
        del dt
        return get_page, calls

    def test_under_cap_single_query(self):
        gp, calls = self._api({"2026-03-01": 5, "2026-03-02": 3})
        runs, trunc = gather.fetch_workflow_runs(gp, "https://api", "2026-03-01", "2026-03-31")
        self.assertEqual(len(runs), 8)
        self.assertIsNone(trunc)
        self.assertEqual(len(calls), 1)

    def test_over_cap_is_split_by_day_and_complete(self):
        per_day = {"2026-03-%02d" % d: 150 for d in range(1, 11)}   # 1,500 runs
        gp, calls = self._api(per_day)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            runs, trunc = gather.fetch_workflow_runs(
                gp, "https://api", "2026-03-01", "2026-03-31")
        self.assertEqual(len(runs), 1500)
        self.assertEqual(len({r["id"] for r in runs}), 1500)
        self.assertIsNone(trunc)
        # newest first, as the API orders them
        self.assertEqual(runs[0]["created_at"][:10], "2026-03-10")
        self.assertEqual(runs[-1]["created_at"][:10], "2026-03-01")

    def test_split_preserves_iso_bounds(self):
        per_day = {"2026-03-%02d" % d: 150 for d in range(1, 11)}
        gp, calls = self._api(per_day)
        with contextlib.redirect_stderr(io.StringIO()):
            runs, _ = gather.fetch_workflow_runs(
                gp, "https://api", "2026-03-01T00:00:00Z", "2026-03-31T23:59:59Z")
        self.assertEqual(len(runs), 1500)
        self.assertTrue(any("created=2026-03-01T00:00:00Z.." in c for c in calls))
        self.assertTrue(any("..2026-03-31T23:59:59Z" in c for c in calls))

    def test_single_day_over_cap_is_marked_truncated_with_warning(self):
        gp, _ = self._api({"2026-03-05": 1200, "2026-03-06": 10})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            runs, trunc = gather.fetch_workflow_runs(
                gp, "https://api", "2026-03-01", "2026-03-31")
        self.assertEqual(len(runs), 1010)
        self.assertTrue(trunc["truncated"])
        self.assertEqual(trunc["fetched"], 1010)
        self.assertEqual(trunc["total_count"], 1210)
        self.assertIn("2026-03-05", trunc["ranges"][0])
        self.assertIn("truncated", err.getvalue())

    def test_capped_total_count_still_triggers_split(self):
        # if GitHub reports total_count capped at 1,000 we still split.
        per_day = {"2026-03-%02d" % d: 150 for d in range(1, 11)}
        gp, _ = self._api(per_day, lie_total=True)
        with contextlib.redirect_stderr(io.StringIO()):
            runs, trunc = gather.fetch_workflow_runs(
                gp, "https://api", "2026-03-01", "2026-03-31")
        self.assertEqual(len(runs), 1500)
        self.assertIsNone(trunc)

    def test_stats_carry_truncation_marker(self):
        stats = gather.aggregate_workflow_stats([{"name": "ci", "conclusion": "success"}])
        marked = gather.mark_workflow_stats_truncated(
            stats, {"truncated": True, "fetched": 1, "total_count": 5, "ranges": ["x"]})
        self.assertEqual(marked["ci"]["total"], 1)
        self.assertTrue(marked[gather.WORKFLOW_STATS_TRUNCATED_KEY]["truncated"])


class TestEnvKnobs(unittest.TestCase):
    def test_non_integer_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            gather._env_int("ACTIVITY_IAC_MAX_WORKERS", 8, 1,
                            env={"ACTIVITY_IAC_MAX_WORKERS": "eight"})
        msg = str(cm.exception.code)
        self.assertIn("ACTIVITY_IAC_MAX_WORKERS", msg)
        self.assertIn("eight", msg)

    def test_below_minimum_is_clamped_with_warning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            v = gather._env_int("ACTIVITY_IAC_MAX_WORKERS", 8, 1,
                                env={"ACTIVITY_IAC_MAX_WORKERS": "0"})
        self.assertEqual(v, 1)
        self.assertIn("ACTIVITY_IAC_MAX_WORKERS", err.getvalue())

    def test_unset_or_blank_uses_default(self):
        self.assertEqual(gather._env_int("X", 8, 1, env={}), 8)
        self.assertEqual(gather._env_int("X", 8, 1, env={"X": " "}), 8)

    def test_validate_env_knobs_rejects_bad_value(self):
        with self.assertRaises(SystemExit):
            gather.validate_env_knobs({"ACTIVITY_HTTP_TIMEOUT": "1m"})
        with self.assertRaises(SystemExit):
            gather.validate_env_knobs({"ACTIVITY_IAC_BUILD_TIMEOUT": "abc"})

    def test_iac_workers_zero_does_not_crash_executor(self):
        # Previously ThreadPoolExecutor(max_workers=0) raised ValueError.
        cg = {"areas": [{"id": "avm/res/a", "label": "a",
                         "paths": ["avm/res/a/main.bicep"], "edges": []}]}

        def run(cmd, **kw):
            raise RuntimeError("no build here")

        with mock.patch.dict(os.environ, {"ACTIVITY_IAC_MAX_WORKERS": "0"}), \
                contextlib.redirect_stderr(io.StringIO()):
            out = gather.extract_iac_edges(
                cg, "clone", which=lambda n: "/x/bicep" if n == "bicep" else None,
                run=run, read_text=lambda p: "")
        self.assertIn("edges_status", out["areas"][0])

    def test_import_does_not_crash_on_bad_env(self):
        import subprocess
        env = dict(os.environ, ACTIVITY_IAC_MAX_WORKERS="nope",
                   ACTIVITY_IAC_BUILD_TIMEOUT="x", ACTIVITY_IAC_RETRIES="")
        proc = subprocess.run(
            [sys.executable, "-c", "import gather"], cwd=os.path.dirname(
                os.path.abspath(__file__)), env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

if __name__ == "__main__":
    unittest.main()
