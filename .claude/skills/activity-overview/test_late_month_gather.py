"""A past month gathered late into its own store (validation findings 7, 8, 12).

Finding 7 — the USAGE §5.5 cross-check (`view.py --as-stored`, then
`validate.py <store> --bundle`) failed on every fresh store of a past month:
gather kept every PR open at gather time and every listed issue, including
ones CREATED AFTER the window's end (and the updated-since listing's last
page, which reaches issues closed and untouched since before the window),
while every view of the window leaves those out, so their people were
"stored but not derivable". Now:
  * gather keeps an open PR / a listed issue only when it existed at the
    window's end (and a closed listed issue only when touched since the
    window's start); an issue a kept PR closes or cross-references is train
    context and kept whatever its dates;
  * a view of the window, projected or `--as-stored`, leaves out a record
    created after its end, even as context;
  * `validate --bundle` does not expect people seen only on stored records
    outside the bundle's window to be derivable from it, so a store gathered
    before the fix passes too.

Finding 8 — a view whose records were projected to the window's end carries
`meta.gathered_at` (the records' gather time), for the report's caveat.

Finding 12 — gather records the window's first-time contributors as a
per-window fact (`newcomers`), searched through an injectable seam; the view
surfaces it as stored (or merged over the windows a wider view spans).

No network: the GitHub API is stubbed through acquire's `_paginated` seam
and git runs against a local file:// repository (test_walked_branch).
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
import validate  # noqa: E402
import view  # noqa: E402
from test_walked_branch import GitRepoCase  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FROM, TO = "2026-09-01", "2026-09-30"
REPO_URL = "https://github.com/o/r"


def raw_pr(number, author, created, state="open", merged_at=None, closed_at=None,
           merged_by=None):
    return {"number": number, "title": "PR {}".format(number), "body": "",
            "user": {"login": author}, "labels": [], "milestone": None,
            "merged_at": merged_at, "closed_at": closed_at or merged_at,
            "created_at": created, "updated_at": closed_at or merged_at or created,
            "state": state, "merged_by": {"login": merged_by} if merged_by else None,
            "base": {"ref": "main", "repo": {"full_name": "o/r"}},
            "head": {"ref": "b{}".format(number), "repo": {"full_name": "o/r"}},
            "html_url": "{}/pull/{}".format(REPO_URL, number)}


def raw_issue(number, author, created, updated, state="open", closed_at=None):
    return {"number": number, "title": "Issue {}".format(number), "body": "",
            "user": {"login": author}, "labels": [], "assignees": [],
            "milestone": None, "state": state,
            "state_reason": "completed" if state == "closed" else None,
            "created_at": created, "updated_at": updated, "closed_at": closed_at,
            "comments": 0, "html_url": "{}/issues/{}".format(REPO_URL, number)}


_KEEP_PR = gather.keep_pr_for_window


def _old_keep_pr(pr, from_date, to_date):
    """The PR keep rule before finding 7: every open PR was kept."""
    if pr.get("state") == "open":
        return True
    return _KEEP_PR(pr, from_date, to_date)


def cross_check(store, frm=FROM, to=TO):
    """USAGE §5.5 on `store`: the --as-stored view, then validate --bundle.
    Returns (exit code, validate's report text, the view)."""
    out = os.path.join(os.path.dirname(store), "at-gather.json")
    with contextlib.redirect_stderr(io.StringIO()):
        assert view.main(["--store", store, "--from", frm, "--to", to,
                          "--as-stored", "--out", out]) == 0
    text = io.StringIO()
    with contextlib.redirect_stdout(text), \
            contextlib.redirect_stderr(io.StringIO()):
        code = validate.main([store, "--bundle", out])
    with open(out, encoding="utf-8") as fh:
        return code, text.getvalue(), json.load(fh)


def fold_into(path, *bundles):
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b))
    conn.commit()
    conn.close()
    return path


def stored_locals(path, prefix):
    conn = graphstore.open_store_readonly(path)
    try:
        return sorted(graphstore.local_id(r[0]) for r in conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ?", ("%#" + prefix + "%",)))
    finally:
        conn.close()


class TestKeepRules(unittest.TestCase):
    """The gather keep rules for a window gathered late."""

    def test_open_pr_created_after_the_window_is_not_kept(self):
        keep = lambda pr: gather.keep_pr_for_window(pr, FROM, TO)  # noqa: E731
        self.assertTrue(keep({"state": "open", "created_at": "2026-09-30T23:59:59Z"}))
        self.assertFalse(keep({"state": "open", "created_at": "2026-10-01T00:00:00Z"}))
        self.assertTrue(keep({"state": "open", "created_at": None}))

    def test_issue_rule(self):
        keep = lambda **kw: gather.keep_issue_for_window(kw, FROM, TO)  # noqa: E731
        # created after the window's end: not the window's, open or closed
        self.assertFalse(keep(state="open", created_at="2026-10-20T00:00:00Z",
                              updated_at="2026-10-20T00:00:00Z"))
        self.assertFalse(keep(state="closed", created_at="2026-10-20T00:00:00Z",
                              updated_at="2026-10-21T00:00:00Z",
                              closed_at="2026-10-21T00:00:00Z"))
        # open at the window's end
        self.assertTrue(keep(state="open", created_at="2025-01-01T00:00:00Z",
                             updated_at="2025-01-02T00:00:00Z"))
        # closed in the window, or touched since its start
        self.assertTrue(keep(state="closed", created_at="2026-08-01T00:00:00Z",
                             updated_at="2026-09-20T00:00:00Z",
                             closed_at="2026-09-20T00:00:00Z"))
        self.assertTrue(keep(state="closed", created_at="2026-08-01T00:00:00Z",
                             updated_at="2026-11-02T00:00:00Z",
                             closed_at="2026-08-20T00:00:00Z"))
        # closed and untouched since before the window (the listing's last page)
        self.assertFalse(keep(state="closed", created_at="2026-05-01T00:00:00Z",
                              updated_at="2026-06-01T00:00:00Z",
                              closed_at="2026-06-01T00:00:00Z"))

    def test_a_window_ending_today_keeps_every_open_item(self):
        today = "2026-11-26"
        self.assertTrue(gather.keep_pr_for_window(
            {"state": "open", "created_at": "2026-11-26T08:00:00Z"}, "2026-11-01", today))
        self.assertTrue(gather.keep_issue_for_window(
            {"state": "open", "created_at": "2026-11-26T08:00:00Z",
             "updated_at": "2026-11-26T08:00:00Z"}, "2026-11-01", today))


class LateMonthRepo(GitRepoCase):
    """September gathered late (in November). The raw listings hold, beside
    the month's own PRs and issues, a PR and an issue opened after September
    by people seen nowhere else, an issue closed in June (the updated-since
    listing's last page), and an issue opened in October that cross-references
    September's merged PR (train context)."""

    def setUp(self):
        super().setUp()
        self.commit({"README.md": "x\n"}, "2026-08-25")
        self.commit({"README.md": "y\n"}, "2026-09-05", msg="feat: y (#1)")
        self.closed_prs = [raw_pr(1, "alice", "2026-09-03T00:00:00Z", state="closed",
                                  merged_at="2026-09-05T10:00:00Z", merged_by="maint")]
        self.open_prs = [raw_pr(2, "bob", "2026-09-10T00:00:00Z"),
                         raw_pr(3, "latepr", "2026-10-15T00:00:00Z")]
        self.issues = [
            raw_issue(13, "refd", "2026-10-25T00:00:00Z", "2026-10-25T00:00:00Z"),
            raw_issue(11, "lateissue", "2026-10-20T00:00:00Z", "2026-10-20T00:00:00Z"),
            raw_issue(10, "carol", "2026-09-02T00:00:00Z", "2026-10-05T00:00:00Z",
                      state="closed", closed_at="2026-09-20T00:00:00Z"),
            raw_issue(12, "oldie", "2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z",
                      state="closed", closed_at="2026-06-01T00:00:00Z"),
        ]
        self.searches = []
        # alice has an earlier PR; bob has nothing earlier; carol's check fails
        self.earlier = {("alice", "pr"): 3, ("bob", "pr"): 0, ("bob", "issue"): 0}

    def get_page(self, url):
        self.urls.append(url)
        if url.startswith(gather.SEARCH_ISSUES_URL):
            q = url.split("q=", 1)[1]
            login = q.split("author%3A", 1)[1].split("+", 1)[0]
            kind = "pr" if "is%3Apr" in q else "issue"
            self.searches.append((login, kind))
            if (login, kind) not in self.earlier:
                raise RuntimeError("search failed")
            return {"total_count": self.earlier[(login, kind)],
                    "incomplete_results": False, "items": []}, None
        if "/pulls?state=closed" in url:
            return self.closed_prs, None
        if "/pulls?state=open" in url:
            return self.open_prs, None
        if "/issues?state=all" in url:
            return self.issues, None
        if "/issues?state=open" in url:
            return [i for i in self.issues if i["state"] == "open"], None
        if url.endswith("/issues/1/timeline?per_page=100"):
            return [{"event": "cross-referenced", "source": {"issue": {
                "number": 13, "repository": {"full_name": "o/r"}}}}], None
        return [], None

    def gather_into(self, store, *extra, old_rules=False):
        patches = [mock.patch.object(gather, "_graphql_client", lambda token: None)]
        if old_rules:
            patches += [mock.patch.object(gather, "keep_pr_for_window", _old_keep_pr),
                        mock.patch.object(gather, "keep_issue_for_window",
                                          lambda *a: True)]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            bundle, err = self.acquire(FROM, TO, *extra)
        fold_into(store, bundle)
        return bundle, err


class TestLateMonthCrossCheck(LateMonthRepo):

    def test_gather_keeps_only_what_existed_at_the_window_end(self):
        store = os.path.join(self.tmp, "sep.db")
        bundle, _err = self.gather_into(store)
        self.assertEqual(sorted(p["number"] for p in bundle["prs"]), [1, 2])
        # 11 (opened after September) and 12 (closed in June) are not kept;
        # 13 is: September's PR #1 is cross-referenced by it (train context)
        self.assertEqual(sorted(i["number"] for i in bundle["issues"]), [10, 13])
        people = stored_locals(store, "person-")
        for login in ("latepr", "lateissue", "oldie"):
            self.assertNotIn("person-" + login, people)
        self.assertIn("person-refd", people)

    def test_cross_check_passes_on_a_fresh_late_store(self):
        store = os.path.join(self.tmp, "sep.db")
        self.gather_into(store)
        code, text, at_gather = cross_check(store)
        self.assertEqual(code, 0, text)
        self.assertIn("RESULT: OK", text)
        # the referenced issue opened in October is in no view of September
        self.assertEqual([i["number"] for i in at_gather["issues"]], [10])
        self.assertNotIn("refd", at_gather["people"])

    def test_cross_check_passes_on_a_store_gathered_before_the_fix(self):
        """A store that already holds the later-created records (and the
        June issue) passes without a re-gather: no view of September holds
        them, and validate --bundle does not expect their people."""
        store = os.path.join(self.tmp, "old.db")
        self.gather_into(store, old_rules=True)
        self.assertIn("pr-3", stored_locals(store, "pr-"))
        self.assertEqual(stored_locals(store, "issue-"),
                         ["issue-10", "issue-11", "issue-12", "issue-13"])
        code, text, at_gather = cross_check(store)
        self.assertEqual(code, 0, text)
        self.assertNotIn("stored but not derivable", text)
        for login in ("latepr", "lateissue", "oldie", "refd"):
            self.assertIn(login, text)       # named in the INFO note
            self.assertNotIn(login, at_gather["people"])
        self.assertEqual(sorted(p["number"] for p in at_gather["prs"]), [1, 2])

    def test_report_view_is_the_same_record_set(self):
        store = os.path.join(self.tmp, "old.db")
        self.gather_into(store, old_rules=True)
        out = os.path.join(self.tmp, "report.json")
        with contextlib.redirect_stderr(io.StringIO()):
            assert view.main(["--store", store, "--from", FROM, "--to", TO,
                              "--out", out]) == 0
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        _code, _text, at_gather = cross_check(store)
        for key in ("prs", "issues"):
            self.assertEqual([r["number"] for r in report[key]],
                             [r["number"] for r in at_gather[key]], key)
        self.assertEqual(sorted(report["people"]), sorted(at_gather["people"]))


class TestPeopleOutsideTheWindow(unittest.TestCase):
    """The validate rule is exact: only people every one of whose
    contribution edges points at a stored record outside the --bundle's
    window are not expected to be derivable."""

    def _bundle(self, prs, issues):
        return {"meta": {"owner": "o", "repo": "r", "from": FROM, "to": TO,
                         "period": {"from": FROM, "to": TO}},
                "prs": prs, "issues": issues}

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        sep = gather.normalize_pr(raw_pr(1, "alice", "2026-09-03T00:00:00Z"))
        late = gather.normalize_pr(raw_pr(3, "latepr", "2026-10-15T00:00:00Z"))
        late["comments_list"] = [{"id": 5, "author": "alice", "body": "x",
                                  "created_at": "2026-10-16T00:00:00Z", "url": None}]
        self.sep = sep
        gather.fold_bundle(self.conn, {
            "meta": {"owner": "o", "repo": "r", "from": FROM, "to": TO},
            "prs": [sep, late], "issues": [], "commits": []})

    def tearDown(self):
        self.conn.close()

    def test_only_people_seen_only_outside_are_left_out(self):
        bundle = self._bundle([self.sep], [])
        got = validate._outside_window_people(
            self.conn, "o", "r", bundle, {"alice", "latepr", "ghostly"})
        # alice also authored the September PR; ghostly has no edge at all
        self.assertEqual(got, {"latepr"})

    def test_a_record_the_bundle_must_hold_is_never_outside(self):
        # the September PR is in the window: its author is not excused when
        # the bundle lacks it (the span check reports the missing record)
        got = validate._outside_window_people(
            self.conn, "o", "r", self._bundle([], []), {"alice"})
        self.assertEqual(got, set())

    def test_a_bundle_without_a_window_excuses_nobody(self):
        got = validate._outside_window_people(
            self.conn, "o", "r", {"meta": {}, "prs": []}, {"latepr"})
        self.assertEqual(got, set())


class TestAsStoredViewLeavesOutLaterRecords(unittest.TestCase):

    def test_context_issue_created_after_the_end(self):
        pr = gather.normalize_pr(raw_pr(1, "alice", "2026-09-03T00:00:00Z",
                                        state="closed",
                                        merged_at="2026-09-05T00:00:00Z"))
        pr["crossref_issues"] = [13, 14]
        later = gather.normalize_issue(raw_issue(13, "refd", "2026-10-25T00:00:00Z",
                                                 "2026-10-25T00:00:00Z"))
        older = gather.normalize_issue(raw_issue(14, "old", "2026-01-25T00:00:00Z",
                                                 "2026-01-25T00:00:00Z"))
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        try:
            gather.fold_bundle(conn, {"meta": {"owner": "o", "repo": "r",
                                               "from": FROM, "to": TO},
                                      "prs": [pr], "issues": [later, older],
                                      "commits": []})
            for as_of_end in (False, True):
                got = extract.extract(conn, "o", "r", FROM, TO,
                                      warn=lambda _m: None, as_of_end=as_of_end)
                self.assertEqual([i["number"] for i in got["issues"]], [14],
                                 as_of_end)
        finally:
            conn.close()


class TestGatheredAt(LateMonthRepo):
    """Finding 8: the report view of a window gathered after it ended says
    when its records were gathered."""

    def test_projected_view_carries_the_gather_time(self):
        store = os.path.join(self.tmp, "sep.db")
        self.gather_into(store)
        conn = graphstore.open_store_readonly(store)
        try:
            fetched = {r[0] for r in conn.execute(
                "SELECT fetched_at FROM nodes WHERE node_class='social'")}
            got = extract.extract(conn, "o", "r", FROM, TO, warn=lambda _m: None,
                                  as_of_end=True)
            stored = extract.extract(conn, "o", "r", FROM, TO,
                                     warn=lambda _m: None)
        finally:
            conn.close()
        self.assertEqual(len(fetched), 1)
        # issue 10 was updated in October: projected back to September's end
        self.assertEqual(got["meta"]["records_as_of"]["issues"], 1)
        self.assertEqual(got["meta"]["gathered_at"], fetched.pop())
        # as stored nothing is projected, and nothing is said
        self.assertNotIn("records_as_of", stored["meta"])
        self.assertNotIn("gathered_at", stored["meta"])

    def test_fixture_views_carry_no_gather_time(self):
        import glob
        for path in sorted(glob.glob(os.path.join(HERE, "fixtures", "bundle_*.json"))):
            with open(path, encoding="utf-8") as fh:
                b = json.load(fh)
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            try:
                gather.fold_bundle(conn, b)
                got = extract.extract(conn, b["meta"]["owner"], b["meta"]["repo"],
                                      b["meta"]["from"], b["meta"]["to"],
                                      warn=lambda _m: None, as_of_end=True)
            finally:
                conn.close()
            self.assertNotIn("gathered_at", got["meta"], path)
            self.assertNotIn("newcomers", got, path)


# --- finding 12: first-time contributors -------------------------------------

def _item(number, author, created, url=None):
    return {"number": number, "author": author, "created_at": created,
            "url": url or "{}/x/{}".format(REPO_URL, number)}


class TestNewcomerCandidates(unittest.TestCase):

    def test_authors_of_items_created_in_the_window(self):
        prs = [_item(1, "alice", "2026-09-03T00:00:00Z"),
               _item(2, "alice", "2026-09-01T00:00:00Z"),       # her earliest
               _item(3, "bob", "2026-08-30T00:00:00Z"),         # before the window
               _item(4, "dependabot[bot]", "2026-09-04T00:00:00Z"),
               _item(5, "ghost", "2026-09-04T00:00:00Z"),
               _item(6, None, "2026-09-04T00:00:00Z")]
        issues = [_item(10, "carol", "2026-09-02T00:00:00Z"),
                  _item(11, "alice", "2026-09-02T00:00:00Z"),
                  _item(12, "dan", "2026-10-02T00:00:00Z")]    # after the window
        got = gather.newcomer_candidates(prs, issues, FROM, TO)
        self.assertEqual([(c["login"], c["first"], c["number"]) for c in got],
                         [("alice", "pr", 2), ("carol", "issue", 10)])

    def test_search_url(self):
        url = gather.newcomer_search_url("o", "r", "al-ice", FROM, "pr")
        self.assertTrue(url.startswith("https://api.github.com/search/issues?"))
        self.assertIn("q=repo%3Ao%2Fr+is%3Apr+author%3Aal-ice+created%3A%3C2026-09-01",
                      url)
        self.assertTrue(url.endswith("&per_page=1"))

    def test_total_count(self):
        self.assertEqual(gather.search_total_count(
            {"total_count": 0, "incomplete_results": False}), 0)
        self.assertEqual(gather.search_total_count({"total_count": 4}), 4)
        for bad in ({"total_count": 0, "incomplete_results": True}, [], None,
                    {"total_count": True}, {"total_count": -1}, {"items": []}):
            self.assertIsNone(gather.search_total_count(bad), bad)


class TestFindNewcomers(unittest.TestCase):

    def cands(self, *logins, first="pr"):
        return [{"login": x, "first": first, "number": i + 1, "url": "u",
                 "created_at": "2026-09-0{}T00:00:00Z".format(i + 1)}
                for i, x in enumerate(logins)]

    def test_new_returning_unknown(self):
        calls = []
        counts = {("ann", "pr"): 0, ("ann", "issue"): 0,       # new
                  ("ben", "pr"): 2,                             # returning, one call
                  ("cy", "pr"): 0, ("cy", "issue"): 1}          # earlier issue only

        def search(login, kind):
            calls.append((login, kind))
            if login == "dee":
                raise RuntimeError("boom")
            return counts.get((login, kind))
        got = gather.find_newcomers(self.cands("ann", "ben", "cy", "dee", "eve"),
                                    search, FROM)
        self.assertEqual([e["login"] for e in got["new"]], ["ann"])
        # eve: search answered None (not a usable response) -> unknown
        self.assertEqual(got["unknown"], ["dee", "eve"])
        self.assertEqual(got["not_checked"], [])
        self.assertEqual(got["candidates"], 5)
        self.assertEqual((got["collected"], got["before"]), (True, FROM))
        self.assertEqual(calls[:5], [("ann", "pr"), ("ann", "issue"), ("ben", "pr"),
                                     ("cy", "pr"), ("cy", "issue")])

    def test_an_issue_reporter_is_checked_for_issues_first(self):
        calls = []
        got = gather.find_newcomers(
            self.cands("ann", first="issue"),
            lambda login, kind: calls.append(kind) or 1, FROM)
        self.assertEqual((calls, got["new"]), (["issue"], []))

    def test_cap(self):
        got = gather.find_newcomers(self.cands("a", "b", "c"),
                                    lambda login, kind: 0, FROM, max_candidates=1)
        self.assertEqual([e["login"] for e in got["new"]], ["a"])
        self.assertEqual(got["not_checked"], ["b", "c"])

    def test_gives_up_after_repeated_failures(self):
        calls = []

        def search(login, kind):
            calls.append(login)
            raise SystemExit("rate limit: giving up")
        got = gather.find_newcomers(self.cands("a", "b", "c", "d", "e"), search,
                                    FROM, stop_after=3)
        self.assertEqual(calls, ["a", "b", "c"])
        self.assertEqual(got["unknown"], ["a", "b", "c", "d", "e"])
        self.assertEqual(got["new"], [])


class TestNewcomersInAcquire(LateMonthRepo):

    def test_acquire_records_the_fact(self):
        bundle, err = self.gather_into(os.path.join(self.tmp, "s.db"))
        nc = bundle["newcomers"]
        self.assertEqual([(e["login"], e["first"], e["number"]) for e in nc["new"]],
                         [("bob", "pr", 2)])
        self.assertEqual(nc["unknown"], ["carol"])
        self.assertEqual((nc["candidates"], nc["before"]), (3, FROM))
        # latepr / lateissue / refd created nothing in September: not candidates
        self.assertEqual(sorted({s[0] for s in self.searches}),
                         ["alice", "bob", "carol"])
        self.assertIn("newcomers: o/r: 3 candidate author(s), 4 search call(s)", err)
        self.assertIn("newcomers.unknown: carol", err)

    def test_no_newcomers_flag_makes_no_call(self):
        bundle, _err = self.gather_into(os.path.join(self.tmp, "s.db"),
                                        "--no-newcomers")
        self.assertEqual(bundle["newcomers"], {"collected": False})
        self.assertEqual(self.searches, [])

    def test_cap_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ACTIVITY_NEWCOMERS_MAX": "1"}):
            bundle, err = self.gather_into(os.path.join(self.tmp, "s.db"))
        # the earliest candidate (carol's issue of 09-02) is checked
        self.assertEqual(bundle["newcomers"]["not_checked"], ["alice", "bob"])
        self.assertEqual(bundle["newcomers"]["unknown"], ["carol"])
        self.assertIn("more than ACTIVITY_NEWCOMERS_MAX=1", err)

    def test_view_and_cross_check_carry_it(self):
        store = os.path.join(self.tmp, "s.db")
        bundle, _err = self.gather_into(store)
        code, text, at_gather = cross_check(store)
        self.assertEqual(code, 0, text)
        self.assertEqual(at_gather["newcomers"], bundle["newcomers"])
        # a changed stored value is caught by the per-window meta cross-check
        conn = graphstore.open_store(store)
        key = [k for (k,) in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'facts:%'")][0]
        entry = json.loads(graphstore.get_meta(conn, key))
        digest = entry["facts"]["newcomers"]
        blob_key = "fact:o/r:" + digest
        value = json.loads(graphstore.get_meta(conn, blob_key))
        value["new"] = []
        graphstore.set_meta(conn, blob_key, json.dumps(value, sort_keys=True))
        conn.close()
        out = os.path.join(self.tmp, "at-gather.json")
        text = io.StringIO()
        with contextlib.redirect_stdout(text), \
                contextlib.redirect_stderr(io.StringIO()):
            code = validate.main([store, "--bundle", out])
        self.assertEqual(code, 1)
        self.assertIn("stored per-window newcomers differs from --bundle",
                      text.getvalue())


class TestNewcomersAcrossWindows(unittest.TestCase):
    """A view that is not exactly one gathered window merges the newcomers of
    the windows inside it; validate's meta integrity accepts the fact."""

    def _month(self, frm, to, new, unknown=()):
        return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
                "prs": [], "issues": [], "commits": [],
                "newcomers": {"collected": True, "before": frm, "candidates": 3,
                              "new": [{"login": x, "first": "pr", "number": n,
                                       "url": "u", "created_at": frm + "T01:00:00Z"}
                                      for x, n in new],
                              "unknown": list(unknown), "not_checked": []}}

    def test_merge(self):
        tmp = tempfile.mkdtemp()
        store = fold_into(os.path.join(tmp, "q.db"),
                          self._month("2026-08-01", "2026-08-31", [("ann", 1)],
                                      unknown=["cy"]),
                          self._month("2026-09-01", "2026-09-30", [("cy", 7)]))
        conn = graphstore.open_store_readonly(store)
        try:
            quarter = extract.extract(conn, "o", "r", "2026-08-01", "2026-10-31",
                                      warn=lambda _m: None, as_of_end=True)
            aug = extract.extract(conn, "o", "r", "2026-08-01", "2026-08-31",
                                  warn=lambda _m: None, as_of_end=True)
            report = validate.validate(conn, project="o", repo="r")
        finally:
            conn.close()
        nc = quarter["newcomers"]
        self.assertEqual([e["login"] for e in nc["new"]], ["ann", "cy"])
        self.assertEqual(nc["unknown"], [])          # cy was found new in September
        self.assertEqual(nc["windows"], [{"from": "2026-08-01", "to": "2026-08-31"},
                                         {"from": "2026-09-01", "to": "2026-09-30"}])
        self.assertFalse(nc["covers_view"])         # October is not gathered
        self.assertNotIn("candidates", nc)
        self.assertEqual(aug["newcomers"]["unknown"], ["cy"])
        self.assertNotIn("windows", aug["newcomers"])
        meta = [c for c in report.checks if c["name"] == "meta_integrity"][0]
        self.assertTrue(meta["ok"], meta)


class TestReportDocs(unittest.TestCase):
    """The report caveat and the New contributors section (findings 8, 12)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "report-template.md"), encoding="utf-8") as fh:
            cls.template = fh.read()
        with open(os.path.join(HERE, "reference", "report-sections.md"),
                  encoding="utf-8") as fh:
            cls.sections = fh.read()

    SENTENCE = ("Records are shown as of {meta.records_as_of.instant}, the end of "
                "this window; titles, labels, milestones and board status are as "
                "gathered on {meta.gathered_at}.")

    def test_caveat_fires_on_records_as_of(self):
        rules = self.template.split("placeholder-rules:begin", 1)[1].split(
            "placeholder-rules:end", 1)[0]
        rule = rules.split("GATHERED AFTER THE WINDOW:", 1)[1].split("\n* ", 1)[0]
        self.assertIn("`meta.records_as_of` is present", rule)
        self.assertIn("under the report title", rule)
        self.assertIn(self.SENTENCE, " ".join(rule.split()))
        self.assertIn(self.SENTENCE, " ".join(self.sections.split()))

    def test_new_contributors_section(self):
        self.assertIn("\n## New contributors\n", self.template)
        self.assertIn("\n## New contributors\n", self.sections)
        self.assertIn("{newcomer.login}", self.template)


class TestBuildReport(unittest.TestCase):

    def test_prints_the_gathered_line_and_newcomers(self):
        import test_build_report as tbr
        with tempfile.TemporaryDirectory() as tmp:
            pv = tbr.build_caveat_view(tmp)
            meta_a = pv["members"][0]["bundle"]["meta"]
            self.assertTrue(meta_a.get("records_as_of"))
            self.assertTrue(meta_a.get("gathered_at"))
            meta_a.pop("state_as_of")
            path = os.path.join(tmp, "v.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(pv, fh)
            out = os.path.join(tmp, "r.md")
            res = tbr.run_script(["--view", path, "--out", out, "--skip-validate"],
                                 cwd=tmp)
            self.assertEqual(res.returncode, 0, res.stderr)
            with open(out, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("> **Gathered after the window** (`mod-a`): records are shown "
                      "as of 2026-05-31T23:59:59Z, the end of this window; titles, "
                      "labels, milestones and board status are as gathered on {}."
                      .format(meta_a["gathered_at"][:10]), text)
        self.assertIn("#### New contributors", text)
        self.assertIn("| `alice` | pr [#7](https://github.com/Azure/mod-a/pull/7) |",
                      text)
        self.assertIn("Could not check whether these authors are new: `bob`.", text)

    def test_partial_newcomer_coverage_is_disclosed(self):
        # review finding: a wider view whose windows were not all checked
        # must not present its list (or its absence) as the whole window's
        import test_build_report as tbr
        for new, expect in (
                ([], "No first-time contributors in the part of this window "
                     "that was checked."),
                ([{"login": "zed", "first": "pr", "number": 3,
                   "url": "https://github.com/Azure/mod-a/pull/3"}], "| `zed` |")):
            with self.subTest(new=bool(new)), tempfile.TemporaryDirectory() as tmp:
                pv = tbr.build_caveat_view(tmp)
                pv["members"][0]["bundle"]["newcomers"] = {
                    "collected": True, "before": "2026-05-01", "new": new,
                    "unknown": [], "not_checked": [], "covers_view": False,
                    "windows": [{"from": "2026-05-01", "to": "2026-05-15"}]}
                path = os.path.join(tmp, "v.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(pv, fh)
                out = os.path.join(tmp, "r.md")
                res = tbr.run_script(
                    ["--view", path, "--out", out, "--skip-validate"], cwd=tmp)
                self.assertEqual(res.returncode, 0, res.stderr)
                with open(out, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn(expect, text)
                self.assertIn("Checked only for 2026-05-01 → 2026-05-15; the rest "
                              "of this window was gathered without the check",
                              text)
                self.assertNotIn("No first-time contributors this window.", text)


if __name__ == "__main__":
    unittest.main()
