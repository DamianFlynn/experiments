"""Issue #109 (fresh review of collection), items 1, 6, 7, 8 and 11.

1. A timeline cross-reference from ANOTHER repo's issue is not an issue of this
   repo (it is a cross-repo ref only); a `connected` link later undone by
   `disconnected` does not count.
6. A wanted issue answered with HTTP 410 (deleted) is skipped like a 404; an
   answer that is not #n of this repo (a transferred issue reached through a
   redirect) is never stored as this repo's issue. Same for backfill.
7. Commits, code events and symbol events are windowed on the UTC day of the
   committer timestamp, as PRs and issues are.
8. git-log records are split on "\\n" only, with the subject last, so U+2028 /
   U+0085 / form feed / separator characters in a subject or path cannot split
   a record.
11. A Projects v2 board cut short (item cap, page guard, failed page) is warned
   about and recorded in `meta.project_board_truncated`.

No network: REST/GraphQL are stubbed; git runs on local repositories.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402
from test_github_api import FakeOpener, FakeSleep, _http_error, _Resp  # noqa: E402
from test_walked_branch import GIT_TEST_ENV, GitRepoCase, git  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HAS_GIT = shutil.which("git") is not None
FROM, TO = "2026-05-01", "2026-05-31"


def _xref(number, full_name=None, is_pr=False):
    issue = {"number": number, "pull_request": ({"url": "u"} if is_pr else None)}
    if full_name is not None:
        issue["repository"] = {"full_name": full_name}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def _link(kind, number, full_name=None):
    subject = {"number": number}
    if full_name is not None:
        subject["repository"] = {"full_name": full_name}
    return {"event": kind, "subject": subject}


def _raw_pr(body="Implements the widget."):
    with open(os.path.join(FIX, "graphql_closing_issues_sample.json")) as fh:
        raw = json.load(fh)["pulls"][0]
    return dict(raw, body=body)


def _raw_issue(number, slug="o/r"):
    return {"number": number, "title": "Issue {}".format(number), "body": "",
            "state": "closed", "state_reason": "completed",
            "user": {"login": "rep"}, "labels": [], "assignees": [],
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-10T00:00:00Z",
            "closed_at": "2026-05-10T00:00:00Z",
            "repository_url": "https://api.github.com/repos/{}".format(slug),
            "html_url": "https://github.com/{}/issues/{}".format(slug, number)}


class _NoClosingRefs:
    """GraphQL stub: no closing issues; optional Projects v2 board pages."""

    def __init__(self, board=None, page=None):
        self.board, self.page = board, page

    def __call__(self, query, variables=None):
        if "closingIssuesReferences" in query:
            return {"repository": {}}
        if variables and variables.get("id") is not None:
            return self.page(variables)
        return {"repository": {"projectsV2": {
            "nodes": [copy.deepcopy(self.board)] if self.board else []}}}


def _acquire(test, timeline=(), get_json=None, graphql=None, board=False):
    """acquire() with no clone and the GitHub API stubbed -> (bundle, stderr,
    issue URLs fetched one by one)."""
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    argv = ["--owner", "o", "--repo", "r", "--from", FROM, "--to", TO,
            "--clone-dir", os.path.join(tmp, "clone"),
            "--store", os.path.join(tmp, "s.db"), "--no-clone",
            "--no-workflows", "--no-releases"]
    if not board:
        argv.append("--no-project-board")
    args = gather.parse_args(argv)
    fetched = []

    def get_page(url):
        if url == "https://api.github.com/repos/o/r":
            return {"default_branch": "main"}, None
        if "/pulls?state=closed" in url:
            return [_raw_pr(test.pr_body)], None
        if url.startswith("https://api.github.com/repos/o/r/issues/1/timeline"):
            return copy.deepcopy(list(timeline)), None
        return [], None

    def default_get_json(url, token, allow_404=False, **_kw):
        return _raw_issue(int(url.rsplit("/", 1)[1])), None

    def record(url, token, **kw):
        fetched.append(url)
        return (get_json or default_get_json)(url, token, **kw)

    err = io.StringIO()
    with mock.patch.object(gather, "_paginated", lambda token: get_page), \
            mock.patch.object(gather, "http_get_json", record), \
            mock.patch.object(gather, "_graphql_client",
                              lambda token: graphql or _NoClosingRefs()), \
            contextlib.redirect_stderr(err):
        bundle = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
    return bundle, err.getvalue(), fetched


def _assert_validates(test, bundle):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(bundle))
    report = validate.validate(conn, project="o", repo="r")
    bad = [c for c in report.checks if not c["ok"]]
    test.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))


# --- item 1: timeline cross-references -----------------------------------------

class TestCrossrefsAreThisRepoOnly(unittest.TestCase):
    def test_foreign_source_is_not_an_issue_of_this_repo(self):
        tl = [_xref(7, "other/repo"), _xref(8, "o/r")]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [8])

    def test_same_repo_compared_case_insensitively(self):
        self.assertEqual(
            gather.parse_timeline_crossrefs([_xref(8, "O/R")], "o/r"), [8])
        self.assertEqual(
            gather.parse_timeline_crossrefs([_xref(8, "o/r")], "O/r"), [8])

    def test_event_without_repository_counts_as_this_repo(self):
        self.assertEqual(gather.parse_timeline_crossrefs([_xref(8)], "o/r"), [8])

    def test_each_cross_reference_is_counted_exactly_once(self):
        tl = [_xref(7, "other/repo"), _xref(8, "o/r"), _xref(9),
              _xref(10, "Other/Repo", is_pr=True), _xref(11, "o/r", is_pr=True)]
        same = gather.parse_timeline_crossrefs(tl, "o/r")
        cross = [(x["owner"] + "/" + x["repo"], x["number"])
                 for x in gather.parse_timeline_xrefs(tl, "o/r")]
        self.assertEqual(same, [8, 9])
        self.assertEqual(cross, [("other/repo", 7), ("Other/Repo", 10)])
        # 11 is a same-repo PR: neither an issue link nor a cross-repo ref
        self.assertFalse({7, 10} & set(same))

    def test_connected_then_disconnected_does_not_count(self):
        tl = [_link("connected", 5), _link("disconnected", 5)]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [])

    def test_reconnected_counts_again(self):
        tl = [_link("connected", 5), _link("disconnected", 5),
              _link("connected", 5)]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [5])

    def test_events_are_read_in_order(self):
        # a disconnect BEFORE the connect does not cancel it
        tl = [_link("disconnected", 5), _link("connected", 5)]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [5])

    def test_disconnect_does_not_cancel_a_mention(self):
        tl = [_xref(5, "o/r"), _link("connected", 5), _link("disconnected", 5)]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [5])

    def test_connected_issue_of_another_repo_does_not_count(self):
        tl = [_link("connected", 6, "other/repo"), _link("connected", 7, "o/r")]
        self.assertEqual(gather.parse_timeline_crossrefs(tl, "o/r"), [7])

    def test_enrich_pr_keeps_a_foreign_ref_out_of_crossref_issues(self):
        pr = {"number": 1, "merged": False, "crossref_issues": []}
        tl = [_xref(7, "other/repo"), _xref(8, "o/r")]

        def get_page(url):
            return (copy.deepcopy(tl) if "/timeline" in url else []), None

        gather.enrich_pr(pr, get_page, "https://api.github.com/repos/o/r", "o/r")
        self.assertEqual(pr["crossref_issues"], [8])
        self.assertEqual(pr["timeline_xrefs"],
                         [{"owner": "other", "repo": "repo", "number": 7,
                           "kind": "cross_ref", "is_pr": False}])


class TestAcquireDoesNotFetchForeignNumbers(unittest.TestCase):
    pr_body = "Implements the widget."

    def test_foreign_cross_reference_fetches_and_links_nothing_local(self):
        b, _err, fetched = _acquire(self, timeline=[_xref(7, "other/repo")])
        self.assertNotIn("https://api.github.com/repos/o/r/issues/7", fetched)
        self.assertEqual(b["issues"], [])
        self.assertEqual(b["prs"][0]["crossref_issues"], [])
        self.assertEqual(b["prs"][0]["timeline_xrefs"][0]["number"], 7)
        _assert_validates(self, b)

    def test_same_repo_cross_reference_is_still_fetched(self):
        b, _err, fetched = _acquire(self, timeline=[_xref(8, "o/r")])
        self.assertIn("https://api.github.com/repos/o/r/issues/8", fetched)
        self.assertEqual([i["number"] for i in b["issues"]], [8])
        self.assertEqual(b["prs"][0]["crossref_issues"], [8])
        _assert_validates(self, b)


# --- item 6: deleted / transferred issues --------------------------------------

URL = "https://api.github.com/repos/o/r/issues/7"


class TestAllowStatuses(unittest.TestCase):
    def _get(self, opener, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return gather.http_get_json(URL, "tok", opener=opener,
                                        sleep=FakeSleep(), clock=lambda: 1000.0,
                                        **kw)

    def test_410_is_returned_when_allowed_and_not_retried(self):
        op = FakeOpener([_http_error(URL, 410, "Gone")])
        self.assertEqual(self._get(op, allow_statuses=gather.GONE_STATUSES),
                         (None, 410))
        self.assertEqual(len(op.calls), 1)

    def test_404_is_returned_with_gone_statuses(self):
        op = FakeOpener([_http_error(URL, 404, "Not Found")])
        self.assertEqual(self._get(op, allow_statuses=gather.GONE_STATUSES),
                         (None, 404))

    def test_410_still_fails_fast_when_not_allowed(self):
        for kw in ({}, {"allow_404": True}):
            op = FakeOpener([_http_error(URL, 410, "Gone")])
            with self.assertRaises(SystemExit):
                self._get(op, **kw)

    def test_other_errors_still_fail_fast(self):
        op = FakeOpener([_http_error(URL, 401, "Bad credentials")])
        with self.assertRaises(SystemExit):
            self._get(op, allow_statuses=gather.GONE_STATUSES)

    def test_success_is_unchanged(self):
        op = FakeOpener([_Resp({"number": 7})])
        self.assertEqual(self._get(op, allow_statuses=gather.GONE_STATUSES),
                         ({"number": 7}, None))


class TestFetchedItemMismatch(unittest.TestCase):
    def test_the_requested_item_matches(self):
        self.assertIsNone(gather.fetched_item_mismatch(_raw_issue(7), "o/r", 7))

    def test_repository_compared_case_insensitively(self):
        self.assertIsNone(
            gather.fetched_item_mismatch(_raw_issue(7, "O/R"), "o/r", 7))

    def test_pr_html_url_matches(self):
        raw = {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
        self.assertIsNone(gather.fetched_item_mismatch(raw, "o/r", 7))

    def test_other_number_is_a_mismatch(self):
        self.assertIn("#3", gather.fetched_item_mismatch(_raw_issue(3), "o/r", 7))

    def test_other_repository_is_a_mismatch(self):
        self.assertIsNotNone(
            gather.fetched_item_mismatch(_raw_issue(7, "new/home"), "o/r", 7))
        raw = dict(_raw_issue(7), html_url="https://github.com/new/home/issues/7")
        self.assertIn("html_url", gather.fetched_item_mismatch(raw, "o/r", 7))
        raw = dict(_raw_issue(7),
                   repository_url="https://api.github.com/repos/new/home")
        self.assertIn("repository_url",
                      gather.fetched_item_mismatch(raw, "o/r", 7))


class TestWantedIssueLoop(unittest.TestCase):
    pr_body = "Fixes #7"

    def test_deleted_issue_410_is_skipped_with_a_note(self):
        def gone(url, token, allow_404=False, allow_statuses=(), **_kw):
            self.assertIn(410, allow_statuses)
            return None, 410

        b, err, _ = _acquire(self, get_json=gone)
        self.assertEqual(b["issues"], [])
        self.assertIn("referenced issue #7 skipped (HTTP 410)", err)
        _assert_validates(self, b)

    def test_transferred_issue_is_not_stored_as_this_repos(self):
        def redirected(url, token, **_kw):
            # GitHub follows the transfer: the issue now lives elsewhere
            return _raw_issue(3, "new/home"), None

        b, err, _ = _acquire(self, get_json=redirected)
        self.assertEqual(b["issues"], [])
        self.assertIn("referenced issue #7 skipped", err)
        self.assertIn("transferred?", err)
        _assert_validates(self, b)

    def test_same_number_in_another_repo_is_not_stored(self):
        def redirected(url, token, **_kw):
            return _raw_issue(7, "new/home"), None

        b, err, _ = _acquire(self, get_json=redirected)
        self.assertEqual(b["issues"], [])
        self.assertIn("new/home", err)

    def test_the_requested_issue_is_kept(self):
        b, err, _ = _acquire(self)
        self.assertEqual([i["number"] for i in b["issues"]], [7])
        self.assertNotIn("skipped", err)
        _assert_validates(self, b)


class TestBackfillFetcher(unittest.TestCase):
    def _fetch(self, local, answer):
        fetch = gather.make_backfill_fetcher("tok")
        err = io.StringIO()
        with mock.patch.object(gather, "http_get_json",
                               lambda url, token, **kw: answer(url, kw)), \
                contextlib.redirect_stderr(err):
            return fetch("social", local, "o/r#" + local), err.getvalue()

    def test_deleted_issue_is_absent(self):
        res, _ = self._fetch("issue-7", lambda url, kw: (None, 410))
        self.assertIs(res, gather.ABSENT)

    def test_deleted_pr_is_absent(self):
        res, _ = self._fetch("pr-7", lambda url, kw: (None, 410))
        self.assertIs(res, gather.ABSENT)

    def test_transferred_issue_is_a_gap_not_a_node(self):
        res, err = self._fetch("issue-7",
                               lambda url, kw: (_raw_issue(3, "new/home"), None))
        self.assertIsNone(res)
        self.assertIn("transferred?", err)

    def test_pr_of_another_repo_is_a_gap(self):
        raw = {"number": 7, "title": "x", "user": {"login": "a"},
               "html_url": "https://github.com/new/home/pull/7"}
        res, _ = self._fetch("pr-7", lambda url, kw: (raw, None))
        self.assertIsNone(res)

    def test_the_requested_issue_is_returned(self):
        res, err = self._fetch("issue-7", lambda url, kw: (_raw_issue(7), None))
        self.assertEqual(res["node"]["number"], 7)
        self.assertEqual(err, "")


# --- items 7 and 8: the git walks ----------------------------------------------

def _local_repo(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    env = mock.patch.dict(os.environ, {
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "a@example.invalid",
        "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "a@example.invalid"})
    env.start()
    test.addCleanup(env.stop)
    return tmp.name


def _git_in(repo, *args, date=None):
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", repo, *args],
        check=True, capture_output=True, text=True, env=env).stdout


def _write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _walks(repo):
    def log(args):
        return gather.run_git(["git", "-C", repo, "log", *args])
    commits = gather.parse_git_log(log(gather.COMMIT_WALK_ARGS))
    events = gather.parse_code_events(log(gather.CODE_WALK_ARGS))
    symbols, diffs = gather.parse_patch_events(log(gather.PATCH_WALK_ARGS))
    return commits, events, symbols, diffs


LATE = "2026-10-31T20:00:00-08:00"      # 2026-11-01T04:00:00Z


@unittest.skipUnless(HAS_GIT, "git not installed")
class TestCommitsWindowedOnTheUtcDay(unittest.TestCase):
    def setUp(self):
        self.repo = _local_repo(self)
        _git_in(self.repo, "init", "-q")
        _write(self.repo, "main.bicep", "param a string\n")
        _git_in(self.repo, "add", "-A")
        _git_in(self.repo, "commit", "-q", "-m", "early",
                date="2026-10-10T12:00:00Z")
        self.early = _git_in(self.repo, "rev-parse", "HEAD").strip()
        _write(self.repo, "main.bicep", "param a string\nparam b string\n")
        _git_in(self.repo, "add", "-A")
        _git_in(self.repo, "commit", "-q", "-m", "late", date=LATE)
        self.late = _git_in(self.repo, "rev-parse", "HEAD").strip()

    def test_every_record_of_the_commit_has_its_utc_day(self):
        commits, events, symbols, _ = _walks(self.repo)
        late = next(c for c in commits if c["sha"] == self.late)
        self.assertEqual(late["committed_at"], "2026-11-01T04:00:00Z")
        self.assertEqual(late["date"], "2026-11-01")
        ev = [e for e in events if e["commit"] == self.late]
        sym = [e for e in symbols if e["commit"] == self.late]
        self.assertTrue(ev and sym)
        self.assertEqual({e["date"] for e in ev + sym}, {"2026-11-01"})

    def test_the_commit_is_in_the_utc_month_of_its_pr(self):
        commits, events, symbols, _ = _walks(self.repo)
        for records in (commits, events, symbols):
            nov = gather.window_records(records, "2026-11-01", "2026-11-30")
            octo = gather.window_records(records, "2026-10-01", "2026-10-31")
            sha = "sha" if records is commits else "commit"
            self.assertIn(self.late, {r[sha] for r in nov})
            self.assertNotIn(self.late, {r[sha] for r in octo})

    def test_window_end_commit_uses_the_utc_day(self):
        self.assertEqual(gather.window_end_commit(self.repo, "2026-10-31"),
                         self.early)
        self.assertEqual(gather.window_end_commit(self.repo, "2026-11-01"),
                         self.late)


@unittest.skipUnless(HAS_GIT, "git not installed")
class TestAcquireWindowsCommitsInUtc(GitRepoCase):
    """A real acquire() over a local repo: the commit made on Oct 31 at
    20:00 -08:00 belongs to November (UTC), with its code and symbol events;
    each window's store passes the trust check."""

    def setUp(self):
        super().setUp()
        self.commit({"main.bicep": "param a string\n"}, "2026-10-10")
        _write(self.src, "main.bicep", "param a string\nparam b string\n")
        git("add", "-A", cwd=self.src)
        env = dict(os.environ, **GIT_TEST_ENV)
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = LATE
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "late"],
            cwd=self.src, check=True, capture_output=True, env=env)
        self.late = self.rev("HEAD")

    def test_october_does_not_hold_the_late_commit(self):
        b, _ = self.acquire("2026-10-01", "2026-10-31")
        self.assertNotIn(self.late, {c["sha"] for c in b["commits"]})
        self.assertNotIn(self.late, {e["commit"] for e in b["code_events"]})
        self.assertNotIn(self.late, {e["commit"] for e in b["symbol_events"]})
        self.assertNotEqual(b["meta"]["clone_sha"], self.late)
        self.assertValidates(b)

    def test_november_holds_it_with_its_events(self):
        b, _ = self.acquire("2026-11-01", "2026-11-30")
        (c,) = b["commits"]
        self.assertEqual((c["sha"], c["date"]), (self.late, "2026-11-01"))
        self.assertEqual({e["date"] for e in b["code_events"]}, {"2026-11-01"})
        self.assertTrue(b["symbol_events"])
        self.assertEqual({e["date"] for e in b["symbol_events"]}, {"2026-11-01"})
        self.assertEqual(b["meta"]["clone_sha"], self.late)
        self.assertValidates(b)


# Characters str.splitlines() splits on besides "\n" (and "\r").
ODD = "  \x85\x0c\x0b\x1c\x1d"


class TestRecordSplitting(unittest.TestCase):
    def _record(self, subject, *files):
        header = gather.FIELD_SEP.join(
            ["a" * 40, "", "Alice", "a@x", "2026-05-03T12:00:00Z", subject])
        return gather.RECORD_SEP + header + "\n" + "\n".join(files) + "\n"

    def test_odd_characters_in_a_subject_stay_in_it(self):
        subject = "fix" + ODD + gather.RECORD_SEP + gather.FIELD_SEP + "end"
        (c,) = gather.parse_git_log(self._record(subject, "docs/a.md"))
        self.assertEqual(c["message"], subject)
        self.assertEqual(c["files"], ["docs/a.md"])
        self.assertEqual(c["date"], "2026-05-03")

    def test_odd_characters_in_a_path_stay_in_it(self):
        path = "docs/a" + "  \x85" + "b.md"
        (c,) = gather.parse_git_log(self._record("s", path, "x.md"))
        self.assertEqual(c["files"], [path, "x.md"])
        raw = self._record("s" + ODD, "M\t" + path)
        (ev,) = gather.parse_code_events(raw)
        self.assertEqual((ev["path"], ev["date"]), (path, "2026-05-03"))

    def test_subject_is_the_last_field_of_every_walk(self):
        self.assertTrue(gather.LOG_FORMAT.endswith("%x1f%s"))
        for args in (gather.COMMIT_WALK_ARGS, gather.CODE_WALK_ARGS,
                     gather.PATCH_WALK_ARGS):
            self.assertIn("--pretty=format:" + gather.LOG_FORMAT, args)


@unittest.skipUnless(HAS_GIT, "git not installed")
class TestRealRepoOddCharacters(unittest.TestCase):
    SUBJECT = ("s t u\x85v\x0cw\x0bx\x1cy\x1dz"
               + gather.RECORD_SEP + "0" + gather.FIELD_SEP + "1 end")
    # raw (non-ASCII, core.quotepath=false) and C-quoted (control) names
    PATHS = ["docs/a b.md", "docs/c\x85d.md", "docs/g h.md",
             "docs/e\x0cf.md", "docs/i\x1ej.md", "docs/k\x0bl.md"]

    def setUp(self):
        self.repo = _local_repo(self)
        _git_in(self.repo, "init", "-q")
        _write(self.repo, "README.md", "r\n")
        _git_in(self.repo, "add", "-A")
        _git_in(self.repo, "commit", "-q", "-m", "first",
                date="2026-05-02T12:00:00Z")
        for p in self.PATHS:
            _write(self.repo, p, "x\n")
        _git_in(self.repo, "add", "-A")
        _git_in(self.repo, "commit", "-q", "--cleanup=verbatim",
                "-m", self.SUBJECT, date="2026-05-03T12:00:00Z")
        self.head = _git_in(self.repo, "rev-parse", "HEAD").strip()

    def test_all_three_walks_keep_the_record_whole(self):
        commits, events, _symbols, diffs = _walks(self.repo)
        self.assertEqual(len(commits), 2)
        head = next(c for c in commits if c["sha"] == self.head)
        self.assertEqual(head["message"], self.SUBJECT)
        self.assertEqual(sorted(head["files"]), sorted(self.PATHS))
        self.assertEqual(head["date"], "2026-05-03")
        self.assertEqual(
            sorted(e["path"] for e in events if e["commit"] == self.head),
            sorted(self.PATHS))
        self.assertEqual(
            sorted(d["path"] for d in diffs if d["commit"] == self.head),
            sorted(self.PATHS))


# --- item 11: Projects v2 board truncation -------------------------------------

def _item(n):
    return {"content": {"__typename": "Issue", "number": n,
                        "repository": {"nameWithOwner": "o/r"}},
            "fieldValues": {"nodes": []}}


def _board(has_next=True, nodes=None):
    return {"id": "B1", "number": 3, "title": "Roadmap", "closed": False,
            "fields": {"nodes": []},
            "items": {"pageInfo": {"hasNextPage": has_next, "endCursor": "C0"},
                      "nodes": nodes if nodes is not None else [_item(1)]}}


def _endless(variables):
    """Every page holds one item and claims another page."""
    n = int(variables["cursor"][1:]) + 2
    return {"node": {"items": {
        "pageInfo": {"hasNextPage": True, "endCursor": "C{}".format(n - 1)},
        "nodes": [_item(n)]}}}


class TestBoardTruncation(unittest.TestCase):
    def _paginate(self, board, page, max_items):
        out, err = [], io.StringIO()
        with contextlib.redirect_stderr(err):
            nodes = gather._paginate_board_items(
                lambda q, v: page(v), board, "o", "r", max_items, out)
        return nodes, out, err.getvalue()

    def test_page_guard_is_warned_and_recorded(self):
        nodes, out, err = self._paginate(_board(), _endless, 5000)
        self.assertEqual(len(nodes), gather.BOARD_MAX_PAGES + 1)
        self.assertEqual(out, [{"board": 3, "kept": len(nodes),
                                "max_items": 5000, "reason": "page_guard"}])
        self.assertIn("Projects v2 board #3 for o/r truncated", err)
        self.assertIn("page guard", err)

    def test_item_cap_is_warned_and_recorded(self):
        nodes, out, err = self._paginate(_board(), _endless, 5)
        self.assertEqual(len(nodes), 5)
        self.assertEqual(out[0]["reason"], "item_cap")
        self.assertIn("truncated at 5 items", err)

    def test_last_page_over_the_cap_is_recorded(self):
        def last(variables):
            return {"node": {"items": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [_item(2), _item(3)]}}}
        nodes, out, _ = self._paginate(_board(), last, 2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(out[0]["reason"], "item_cap")

    def test_failed_page_is_recorded(self):
        def boom(variables):
            raise gather.GitHubAPIError("HTTP 502")
        _, out, err = self._paginate(_board(), boom, 5000)
        self.assertEqual(out[0]["reason"], "error")
        self.assertIn("partial Projects v2 board #3", err)

    def test_complete_board_records_nothing(self):
        def last(variables):
            return {"node": {"items": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [_item(2)]}}}
        nodes, out, err = self._paginate(_board(), last, 5000)
        self.assertEqual(len(nodes), 2)
        self.assertEqual((out, err), ([], ""))


class TestBoardTruncationInMeta(unittest.TestCase):
    pr_body = "Implements the widget."

    def test_truncated_board_is_in_meta(self):
        gql = _NoClosingRefs(board=_board(), page=_endless)
        b, err, _ = _acquire(self, graphql=gql, board=True)
        self.assertEqual(b["meta"]["project_board_truncated"],
                         [{"board": 3, "kept": gather.BOARD_MAX_PAGES + 1,
                           "max_items": 5000, "reason": "page_guard"}])
        self.assertIn("meta.project_board_truncated", err)
        _assert_validates(self, b)

    def test_complete_board_adds_no_key(self):
        gql = _NoClosingRefs(board=_board(has_next=False))
        b, _, _ = _acquire(self, graphql=gql, board=True)
        self.assertNotIn("project_board_truncated", b["meta"])


# --- the trust check on every fixture store ------------------------------------

class TestFixtureStoresStillValidate(unittest.TestCase):
    def test_every_fixture_bundle_folds_to_a_valid_store(self):
        names = sorted(n for n in os.listdir(FIX)
                       if n.startswith("bundle_") and n.endswith(".json")
                       # a toy with non-sha commit ids: fails the gate by design
                       and n != "bundle_sample.json")
        self.assertTrue(names)
        for name in names:
            with open(os.path.join(FIX, name), encoding="utf-8") as fh:
                bundle = json.load(fh)
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, bundle)
            project, repo = validate._resolve_target(conn, None, None)
            report = validate.validate(conn, project=project, repo=repo)
            bad = [c["name"] for c in report.checks if not c["ok"]]
            self.assertEqual(bad, [], name)


if __name__ == "__main__":
    unittest.main()
