"""Issue #85 — window-scoped views on a store holding several windows.

One store that grows month after month must give each month its own view.
Before the fix `extract` materialized the repo's WHOLE stored node set, so a
June view of a May+June store carried May's PRs, issues, commits, code and
symbol events, artifact lifecycle events, releases and people: trains,
timeline, feature_deltas, people profiles and halls for "June" included May.

The rule (see extract.py, BUNDLE.md "Window scope"): a window's view holds
what a gather of that window holds, with each record at its latest stored
state —

- a PR merged / closed (unmerged) in the window, or open and created by its end;
- an issue closed or updated in the window, open and created by its end, or
  closed / cross-referenced by one of the view's PRs (train context);
- a commit dated in the window; the ledger rows and artifact lifecycle events
  of those commits; a release published in the window;
- every node whose own timestamp is in the window, and every undated node;
- the stored people with a contribution in the view's records (issue #88:
  their modules / areas re-derived from those records).

Scenario: May and June folded into one store (in both orders). PR 2 and
issue 18 stay open across both months and are re-gathered unchanged in June;
issue 17 (closed in May) is cross-referenced by June's PR 1001, so June's
gather fetches it too. A symbol is dropped from `src/a.py` in May and added to
`src/b.py` in June: no window holds a move, so no per-window view may show one.

Offline: stores are built in memory via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
import view  # noqa: E402

MAY = ("2026-05-01", "2026-05-31")
JUNE = ("2026-06-01", "2026-06-30")
SPAN = ("2026-05-01", "2026-06-30")
URL = "https://github.com/o/r"


def _pr(number, author, merged_at=None, created_at=None, state="closed",
        closes=(), crossref=(), reviewers=()):
    created = created_at or merged_at
    return {"number": number, "title": "feat: change {}".format(number),
            "body": "", "author": author, "labels": [], "state": state,
            "merged": merged_at is not None, "merged_at": merged_at,
            "merged_by": author if merged_at else None,
            "closed_at": merged_at, "created_at": created,
            "updated_at": merged_at or created, "base": "main",
            "head": "f{}".format(number), "closes": list(closes),
            "crossref_issues": list(crossref), "milestone": None,
            "reviewers": list(reviewers), "comments_list": [],
            "review_comments": [], "url": "{}/pull/{}".format(URL, number)}


def _issue(number, author, created_at, updated_at, closed_at=None,
           state_reason=None):
    return {"number": number, "title": "issue {}".format(number), "body": "",
            "author": author, "labels": [], "kind": "feature",
            "state": "closed" if closed_at else "open",
            "state_reason": state_reason if closed_at else None,
            "created_at": created_at, "updated_at": updated_at,
            "closed_at": closed_at, "milestone": None, "comments_list": [],
            "url": "{}/issues/{}".format(URL, number)}


def _commit(sha, author, date, message):
    return {"sha": sha, "parents": [], "author": author, "date": date,
            "committed_at": date + "T12:00:00Z" if date else None,
            "message": message, "files": [], "pr": None}


def _ev(sha, author, date, change, path):
    return {"commit": sha, "author": author, "date": date, "change": change,
            "path": path}


def _sym(sha, author, date, change, path):
    return {"path": path, "lang": "python", "subkind": "function",
            "name": "helper", "change": change, "commit": sha,
            "author": author, "date": date, "before": None, "after": None}


A1, A2 = "a1" * 20, "a2" * 20
D1, D2 = "d1" * 20, "d2" * 20

# Open across both months, re-gathered unchanged in June.
PR2 = _pr(2, "carol", created_at="2026-05-20T09:00:00Z", state="open")
ISSUE18 = dict(_issue(18, "gina", "2026-04-01T00:00:00Z",
                      "2026-05-22T00:00:00Z"), milestone="v2")
# The one open milestone; issue 18 sits on it in both months.
MILESTONES = [{"title": "v2", "number": 7, "state": "open",
               "due_on": "2026-06-30T00:00:00Z"}]
# Closed in May; June's PR 1001 cross-references it, so June fetches it too.
ISSUE17 = _issue(17, "frank", "2026-04-20T00:00:00Z", "2026-05-10T12:00:00Z",
                 closed_at="2026-05-10T12:00:00Z", state_reason="completed")


def _may():
    return {
        "meta": {"owner": "o", "repo": "r", "from": MAY[0], "to": MAY[1],
                 "base_branch": "main", "branches": ["main"]},
        "prs": [_pr(1, "alice", merged_at="2026-05-10T12:00:00Z",
                    created_at="2026-05-08T09:00:00Z", closes=[17],
                    reviewers=["bob"]),
                copy.deepcopy(PR2)],
        "issues": [copy.deepcopy(ISSUE17), copy.deepcopy(ISSUE18),
                   _issue(19, "gina", "2026-05-02T00:00:00Z",
                          "2026-05-15T00:00:00Z",
                          closed_at="2026-05-15T00:00:00Z",
                          state_reason="not_planned")],
        "commits": [_commit(A1, "alice", "2026-05-09", "feat: a (#1)"),
                    _commit(A2, "bob", "2026-05-12", "docs: readme")],
        "code_events": [_ev(A1, "alice", "2026-05-09", "add", "src/a.py"),
                        _ev(A2, "bob", "2026-05-12", "modify", "README.md")],
        "symbol_events": [_sym(A2, "bob", "2026-05-12", "drop", "src/a.py")],
        "milestones": copy.deepcopy(MILESTONES),
        "releases": [{"tag_name": "v1.0", "name": "v1.0",
                      "published_at": "2026-05-28T00:00:00Z",
                      "prerelease": False, "url": URL + "/releases/v1.0"}],
    }


def _june():
    return {
        "meta": {"owner": "o", "repo": "r", "from": JUNE[0], "to": JUNE[1],
                 "base_branch": "main", "branches": ["main"]},
        "prs": [_pr(1001, "dave", merged_at="2026-06-10T12:00:00Z",
                    created_at="2026-06-08T09:00:00Z", closes=[1017],
                    crossref=[17], reviewers=["erin"]),
                copy.deepcopy(PR2)],
        "issues": [copy.deepcopy(ISSUE17), copy.deepcopy(ISSUE18),
                   _issue(1017, "dave", "2026-06-01T00:00:00Z",
                          "2026-06-10T12:00:00Z",
                          closed_at="2026-06-10T12:00:00Z",
                          state_reason="completed")],
        "commits": [_commit(D1, "dave", "2026-06-09", "feat: b (#1001)"),
                    _commit(D2, "bob", "2026-06-12", "docs: readme again")],
        "code_events": [_ev(D1, "dave", "2026-06-09", "add", "src/b.py"),
                        _ev(D2, "bob", "2026-06-12", "modify", "README.md")],
        "symbol_events": [_sym(D1, "dave", "2026-06-09", "add", "src/b.py")],
        "milestones": copy.deepcopy(MILESTONES),
        "releases": [{"tag_name": "v1.1", "name": "v1.1",
                      "published_at": "2026-06-28T00:00:00Z",
                      "prerelease": False, "url": URL + "/releases/v1.1"}],
    }


def _store(*bundles):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b))
    return conn


def _raw(conn, window, **kw):
    return extract.extract(conn, "o", "r", *window, warn=lambda _m: None, **kw)


def _view(conn, window):
    return link.enrich(_raw(conn, window))


def _nums(items):
    return [i["number"] for i in items]


def _shas(items):
    return [c["sha"][:2] for c in items]


def _summary(b):
    """The facts the issue names, in a comparable shape."""
    return {
        "prs": _nums(b["prs"]), "issues": _nums(b["issues"]),
        "commits": _shas(b["commits"]),
        "code_events": sorted((e["commit"][:2], e["path"])
                              for e in b["code_events"]),
        "symbol_events": sorted((e["commit"][:2], e["path"], e["change"])
                                for e in b["symbol_events"]),
        "releases": [r["tag_name"] for r in b["releases"]],
        "trains": [t["id"] for t in b["trains"]],
        "people": sorted(b["people"]),
        "deltas": sorted((d["commit"][:2], d["artifact"])
                         for d in b["feature_deltas"]),
        "timeline": sorted((e["ts"], str(e["ref"].get("url")))
                           for e in b["timeline"]),
        "fame": [h["login"] for h in b["halls"]["fame"]],
        "shipped": sorted((r["type"], r["id"]) for r in b["buckets"]["shipped"]),
        "symbol_moves": b["symbol_moves"]["links"],
    }


def _ok(testcase, conn):
    report = validate.validate(conn, project="o", repo="r")
    failing = [c for c in report.checks if not c["ok"]]
    testcase.assertEqual(failing, [], json.dumps(failing, indent=1, default=str))


# Each month's view must be exactly what a store holding only that month
# gives, whichever order the months were folded in.
_SKIP_META = ("state_as_of",)


def _without(bundle, keys=_SKIP_META):
    b = copy.deepcopy(bundle)
    for k in keys:
        b["meta"].pop(k, None)
    return b


class TwoMonthStore(unittest.TestCase):

    def setUp(self):
        self.stores = [_store(_may(), _june()), _store(_june(), _may())]

    def tearDown(self):
        for c in self.stores:
            c.close()

    def test_june_view_holds_only_june(self):
        for conn in self.stores:
            s = _summary(_view(conn, JUNE))
            self.assertEqual(s["prs"], [2, 1001])
            # 17 is train context: June's PR 1001 cross-references it.
            self.assertEqual(s["issues"], [17, 18, 1017])
            self.assertEqual(s["commits"], ["d1", "d2"])
            self.assertEqual(s["code_events"],
                             [("d1", "src/b.py"), ("d2", "README.md")])
            self.assertEqual(s["symbol_events"],
                             [("d1", "src/b.py", "add")])
            self.assertEqual(s["releases"], ["v1.1"])
            self.assertEqual(s["trains"], ["train-issue-1017", "train-pr-2"])
            self.assertNotIn("train-issue-17", s["trains"])
            self.assertEqual(s["deltas"],
                             [("d1", "src/b.py#python:function:helper"),
                              ("d2", "art:README.md")])
            self.assertEqual(s["shipped"], [("issue", 1017), ("pr", 1001)])
            # alice worked in this repo only in May: not a June person.
            self.assertNotIn("alice", s["people"])
            self.assertEqual(s["people"],
                             ["bob", "carol", "dave", "erin", "frank", "gina"])
            self.assertEqual(s["fame"], ["dave", "bob", "erin"])

    def test_may_view_holds_only_may(self):
        for conn in self.stores:
            s = _summary(_view(conn, MAY))
            self.assertEqual(s["prs"], [1, 2])
            self.assertEqual(s["issues"], [17, 18, 19])
            self.assertEqual(s["commits"], ["a1", "a2"])
            self.assertEqual(s["code_events"],
                             [("a1", "src/a.py"), ("a2", "README.md")])
            self.assertEqual(s["symbol_events"],
                             [("a2", "src/a.py", "drop")])
            self.assertEqual(s["releases"], ["v1.0"])
            self.assertEqual(s["trains"],
                             ["train-issue-17", "train-issue-19", "train-pr-2"])
            self.assertEqual(s["deltas"],
                             [("a2", "art:README.md"),
                              ("a2", "src/a.py#python:function:helper")])
            self.assertNotIn("dave", s["people"])
            self.assertNotIn("erin", s["people"])
            self.assertEqual(s["fame"], ["alice", "bob"])

    def test_each_month_equals_a_store_holding_only_that_month(self):
        for window, alone in ((MAY, _may()), (JUNE, _june())):
            single = _store(alone)
            want = _without(_view(single, window))
            for conn in self.stores:
                self.assertEqual(_without(_view(conn, window)), want, window)
            single.close()

    def test_raw_view_equals_single_month_store(self):
        for window, alone in ((MAY, _may()), (JUNE, _june())):
            single = _store(alone)
            want = _without(_raw(single, window))
            for conn in self.stores:
                self.assertEqual(_without(_raw(conn, window)), want, window)
            single.close()

    def test_whole_span_view_is_the_union(self):
        for conn in self.stores:
            may, june = _summary(_view(conn, MAY)), _summary(_view(conn, JUNE))
            span = _summary(_view(conn, SPAN))
            for key in ("prs", "issues", "commits", "code_events",
                        "symbol_events", "releases", "people", "deltas"):
                want = sorted(set(map(_hashable, may[key]))
                              | set(map(_hashable, june[key])))
                got = sorted(set(map(_hashable, span[key])))
                self.assertEqual(got, want, key)
            self.assertEqual(sorted(span["trains"]), sorted(
                set(may["trains"]) | set(june["trains"])))

    def test_whole_span_view_matches_full_store(self):
        # Over the store's whole span nothing is out of window: every stored
        # node is in the view (what extract returned before the fix).
        conn = self.stores[0]
        raw = _raw(conn, SPAN)
        self.assertEqual(len(raw["prs"]), 3)
        self.assertEqual(len(raw["issues"]), 4)
        self.assertEqual(len(raw["commits"]), 4)
        self.assertEqual(len(raw["code_events"]), 4)
        self.assertEqual(len(raw["symbol_events"]), 2)
        self.assertEqual(sorted(raw["people"]), [
            "alice", "bob", "carol", "dave", "erin", "frank", "gina"])
        self.assertNotIn("state_as_of", raw["meta"])

    def test_artifact_lifecycle_is_window_scoped(self):
        for conn in self.stores:
            june = _raw(conn, JUNE)["artifacts"]
            # (src/*.py files are not tracked artifacts; their symbols are.)
            self.assertEqual(sorted(june), [
                "art:README.md", "src/b.py#python:function:helper"])
            self.assertEqual([e["commit"][:2] for e in
                              june["art:README.md"]["lifecycle"]], ["d2"])
            may = _raw(conn, MAY)["artifacts"]
            self.assertEqual(sorted(may), [
                "art:README.md", "src/a.py#python:function:helper"])
            # The whole-span view keeps every window's lifecycle (= the store).
            span = _raw(conn, SPAN)["artifacts"]
            self.assertEqual([e["commit"][:2] for e in
                              span["art:README.md"]["lifecycle"]], ["a2", "d2"])

    def test_no_cross_window_symbol_move_in_a_month_view(self):
        # fold matched moves per window and recorded none; a month view must
        # not invent one by matching May's drop against June's add.
        for conn in self.stores:
            self.assertEqual(extract.recorded_moves(conn, "o", "r"), [])
            for window in (MAY, JUNE):
                b = _view(conn, window)
                self.assertEqual(b["symbol_moves"]["links"], [], window)
                self.assertFalse(any(a.get("status") == "replaced"
                                     for a in b["artifacts"].values()))

    def test_historical_view_says_when_its_state_is_from(self):
        for conn in self.stores:
            self.assertEqual(_raw(conn, MAY)["meta"]["state_as_of"], JUNE[1])
            self.assertNotIn("state_as_of", _raw(conn, JUNE)["meta"])
            self.assertNotIn("state_as_of", _raw(conn, SPAN)["meta"])

    def test_open_items_stay_in_flight_in_both_months(self):
        # Open items are kept in every view whose window they were open in:
        # issue 18 (on the current milestone, last touched in May) is still
        # in flight in June; PR 2 (off any milestone, last touched in May) is
        # in flight in May and stays in June's arrays for the forecast.
        for conn in self.stores:
            for window in (MAY, JUNE):
                b = _view(conn, window)
                self.assertIn(2, _nums(b["prs"]))
                self.assertIn(18, _nums(b["issues"]))
                inflight = sorted((r["type"], r["id"])
                                  for r in b["buckets"]["in_flight"])
                self.assertIn(("issue", 18), inflight, window)
                if window == MAY:
                    self.assertIn(("pr", 2), inflight)

    def test_validate_green(self):
        # Without the cross-window symbol pair (see the expected failure
        # below) the trust check passes on the accumulated store, in both
        # fold orders, and on each month alone.
        may, june = _may(), _june()
        may["symbol_events"], june["symbol_events"] = [], []
        for conn in (_store(may, june), _store(june, may)):
            _ok(self, conn)
            conn.close()
        for alone in (_may(), _june()):
            single = _store(alone)
            _ok(self, single)
            single.close()

    def test_validate_green_with_cross_window_symbol_pair(self):
        # A symbol dropped from one file in May and added to another in June
        # is not a move in either window. The trust check judges each recorded
        # move against the ledger and reports a whole-store re-match as INFO,
        # so such a store passes.
        for conn in self.stores:
            _ok(self, conn)

    def test_view_cli_is_window_scoped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "store.db")
            conn = graphstore.open_store(path)
            graphstore.init_schema(conn)
            for b in (_may(), _june()):
                gather.fold_bundle(conn, b)
            conn.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = view.main(["--store", path, "--from", JUNE[0],
                                "--to", JUNE[1], "--out",
                                os.path.join(d, "june.json")])
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "june.json"), encoding="utf-8") as fh:
                got = json.load(fh)
            self.assertEqual(_nums(got["prs"]), [2, 1001])
            self.assertEqual(_shas(got["commits"]), ["d1", "d2"])


class ProjectDigestTwoMonths(unittest.TestCase):
    """digest's project trains fold in spine-component nodes that have no
    member train; a node from another window must not ride in that way."""

    def test_june_project_trains_exclude_may(self):
        conn = _store(_may(), _june())
        pv = digest.build_project_view(conn, "o", ["r"], *JUNE,
                                       warn=lambda _m: None)
        refs = sorted(r for t in pv["trains"] for r in t["prs"] + t["issues"])
        self.assertNotIn("o/r#pr-1", refs)
        # issue 17 is only MENTIONED by PR 1001: a mention joins no train
        # (#110 item 1), so it is in no project train, as in link's trains.
        self.assertEqual(refs, ["o/r#issue-1017", "o/r#pr-1001", "o/r#pr-2"])
        member = pv["members"][0]["bundle"]
        self.assertEqual(_nums(member["prs"]), [2, 1001])
        self.assertNotIn("alice", pv["people"])
        conn.close()

    def test_june_digest_equals_june_only_store(self):
        def trains(conn):
            pv = digest.build_project_view(conn, "o", ["r"], *JUNE,
                                           warn=lambda _m: None)
            return pv["trains"]
        both, alone = _store(_may(), _june()), _store(_june())
        self.assertEqual(trains(both), trains(alone))
        both.close()
        alone.close()


class UndatedAndOutOfWindowNodes(unittest.TestCase):

    def test_undated_commit_is_in_every_view(self):
        # A commit without a date cannot be placed in a window; it is kept
        # (as before) rather than silently dropped from the trust check.
        may = _may()
        may["commits"].append(_commit("e1" * 20, "hal", None, "undated"))
        june = _june()
        may["symbol_events"], june["symbol_events"] = [], []
        conn = _store(may, june)
        for window in (MAY, JUNE, SPAN):
            self.assertIn("e1", _shas(_raw(conn, window)["commits"]))
        _ok(self, conn)
        conn.close()

    def test_null_lists_in_stored_records_do_not_break_the_view(self):
        # The people filter reads every stored record of the repo; JSON nulls
        # there (link reads them as empty, #82) must not make extract raise.
        conn = _store(_may(), _june())
        conn.execute(
            "UPDATE nodes SET data = json_set(data, '$.files', json('null')) "
            "WHERE id = ?", ("o/r#" + A1,))
        conn.execute(
            "UPDATE nodes SET data = json_set(data, '$.reviewers', json('null'),"
            " '$.comments_list', json('null')) WHERE id = 'o/r#pr-1'")
        self.assertEqual(sorted(_raw(conn, JUNE)["people"]),
                         ["bob", "carol", "dave", "erin", "frank", "gina"])
        self.assertIn("alice", _raw(conn, MAY)["people"])
        conn.close()

    def test_open_item_created_after_the_window_is_not_in_it(self):
        june = _june()
        june["prs"].append(_pr(1002, "ivan", created_at="2026-06-20T00:00:00Z",
                               state="open"))
        conn = _store(_may(), june)
        self.assertNotIn(1002, _nums(_raw(conn, MAY)["prs"]))
        self.assertIn(1002, _nums(_raw(conn, JUNE)["prs"]))
        conn.close()

    def test_issue_updated_in_window_but_closed_before_is_in_it(self):
        # gather pulls issues by updated_at: an old closed issue commented on
        # in June belongs to June's view (and not May's).
        june = _june()
        june["issues"].append(_issue(5, "kim", "2025-01-01T00:00:00Z",
                                     "2026-06-15T00:00:00Z",
                                     closed_at="2025-02-01T00:00:00Z",
                                     state_reason="completed"))
        conn = _store(_may(), june)
        self.assertIn(5, _nums(_raw(conn, JUNE)["issues"]))
        self.assertNotIn(5, _nums(_raw(conn, MAY)["issues"]))
        conn.close()


class ArtifactStatusAsOfWindowEnd(unittest.TestCase):
    """A stored artifact is cumulative (#76): its status is that of the
    latest folded event. A view of an earlier window shows the status at that
    window's END, not one decided by a later window."""

    def _bundle(self, window, code_events=(), symbol_events=(), commits=()):
        return {"meta": {"owner": "o", "repo": "r", "from": window[0],
                         "to": window[1]},
                "prs": [], "issues": [], "commits": list(commits),
                "code_events": list(code_events),
                "symbol_events": list(symbol_events)}

    def test_file_deleted_later_is_live_in_the_earlier_view(self):
        may = self._bundle(MAY, [_ev(A1, "alice", "2026-05-09", "add",
                                     "docs/x.md")],
                           commits=[_commit(A1, "alice", "2026-05-09", "x")])
        june = self._bundle(JUNE, [_ev(D1, "dave", "2026-06-09", "delete",
                                       "docs/x.md")],
                            commits=[_commit(D1, "dave", "2026-06-09", "y")])
        for conn in (_store(may, june), _store(june, may)):
            stored = [n["data"] for n in graphstore.repo_nodes(
                conn, "o", "r", "code") if n["id"] == "o/r#art:docs/x.md"]
            self.assertEqual(stored[0]["status"], "removed")
            got = _raw(conn, MAY)["artifacts"]["art:docs/x.md"]
            self.assertEqual(got["status"], "live")
            self.assertEqual([e["event"] for e in got["lifecycle"]], ["add"])
            got = _raw(conn, JUNE)["artifacts"]["art:docs/x.md"]
            self.assertEqual(got["status"], "removed")
            self.assertEqual([e["event"] for e in got["lifecycle"]],
                             ["remove"])
            self.assertEqual(_raw(conn, SPAN)["artifacts"]["art:docs/x.md"],
                             stored[0])
            _ok(self, conn)

    def test_symbol_moved_later_is_not_replaced_in_the_earlier_view(self):
        a = "a.bicep#bicep:resource:fw"
        b = "b.bicep#bicep:resource:fw"

        def sym(path, change, sha, date):
            return {"path": path, "lang": "bicep", "subkind": "resource",
                    "name": "fw", "change": change, "commit": sha,
                    "author": "a", "date": date, "before": None,
                    "after": None}
        may = self._bundle(MAY, symbol_events=[
            sym("a.bicep", "add", A1, "2026-05-02")])
        june = self._bundle(JUNE, symbol_events=[
            sym("a.bicep", "drop", D1, "2026-06-10"),
            sym("b.bicep", "add", D1, "2026-06-10")])
        conn = _store(may, june)
        stored = {graphstore.local_id(n["id"], "o", "r"): n["data"]
                  for n in graphstore.repo_nodes(conn, "o", "r", "code")}
        self.assertEqual((stored[a]["status"], stored[a]["replaced_by"]),
                         ("replaced", b))
        arts = _raw(conn, MAY)["artifacts"]
        self.assertEqual(sorted(arts), [a])
        self.assertEqual((arts[a]["status"], arts[a]["replaced_by"]),
                         ("live", None))
        self.assertNotIn("move_basis", arts[a])
        arts = _raw(conn, JUNE)["artifacts"]
        self.assertEqual((arts[a]["status"], arts[a]["replaced_by"]),
                         ("replaced", b))
        self.assertEqual(arts[b]["identity_from"], a)
        self.assertEqual(_raw(conn, SPAN)["artifacts"][a], stored[a])
        _ok(self, conn)


def _hashable(x):
    return tuple(x) if isinstance(x, list) else x


if __name__ == "__main__":
    unittest.main()


class ReviewFollowUps(unittest.TestCase):
    def test_pruned_bridge_pr_no_longer_joins_two_trains(self):
        # issue-1 and issue-2 are separate trains this month; a PR from another
        # month references both. Once that PR is out of view the two issues
        # must be two components again, as on a store holding only this month.
        import digest
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        q = lambda local: graphstore.qualify_id("p", "o/r", local)  # noqa: E731
        graphstore.upsert_nodes(conn, [
            (q(l), "p", "o/r", "social", "2026-05-10T00:00:00Z", {}, None)
            for l in ("issue-1", "issue-2", "pr-9")])
        graphstore.upsert_edges(conn, [
            (q("pr-9"), q("issue-1"), "cross_ref", None, {}),
            (q("pr-9"), q("issue-2"), "cross_ref", None, {})])
        members = [{"repo": "o/r", "bundle": {
            "prs": [], "issues": [{"number": 1}, {"number": 2}]}}]
        comps = digest.window_components(
            conn, "p", members,
            [frozenset({q("issue-1"), q("issue-2"), q("pr-9")})])
        self.assertEqual(sorted(sorted(c) for c in comps),
                         [[q("issue-1")], [q("issue-2")]])
        # still joined when something in view connects them
        graphstore.upsert_nodes(conn, [
            (q("pr-3"), "p", "o/r", "social", "2026-05-11T00:00:00Z", {}, None)])
        graphstore.upsert_edges(conn, [
            (q("pr-3"), q("issue-1"), "closes", None, {}),
            (q("pr-3"), q("issue-2"), "closes", None, {})])
        members[0]["bundle"]["prs"] = [{"number": 3}]
        comps = digest.window_components(
            conn, "p", members,
            [frozenset({q("issue-1"), q("issue-2"), q("pr-9"), q("pr-3")})])
        self.assertEqual([sorted(c) for c in comps],
                         [[q("issue-1"), q("issue-2"), q("pr-3")]])

    def test_transitive_edge_is_not_a_direct_dependent(self):
        import spotlight
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        q = lambda a: graphstore.qualify_id("o", "r", "area-" + a)  # noqa: E731
        graphstore.upsert_nodes(conn, [
            (q(a), "o", "r", "structure", None, {"id": a}, None)
            for a in ("a", "b", "c")])
        graphstore.upsert_edges(conn, [
            (q("b"), q("c"), "depends_on", None, {"transitive": False}),
            (q("a"), q("b"), "depends_on", None, {"transitive": False}),
            (q("a"), q("c"), "depends_on", None, {"transitive": True})])
        res = spotlight.area_dependents(conn, "o", "o/r::c")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["direct"], ["o/r::b"])
        self.assertEqual(res["dependents"], ["o/r::a", "o/r::b"])
