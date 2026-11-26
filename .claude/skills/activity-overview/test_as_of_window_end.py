"""Issue #97 — a report view of a past window reads its records as they were
at the window's end.

PR / issue records are stored latest-wins, so a month gathered late (or
viewed after a later gather) read each record as the latest gather saw it:
an item open at the month's end but merged / closed since was not in flight,
showed its later outcome, and counted later comments, reviews and
participants. `extract(..., as_of_end=True)` — what view.py and digest.py
use — projects every record holding anything later than the window's end
back to that end (`extract.record_as_of`) before the window rule picks the
view's records. The trust check (validate) keeps the full stored records.

Scenario: October 2026 gathered on 2026-11-26 ("late"):
  (a) PR 10, open at the end, reviewed / commented in October and again in
      November, merged on 2026-11-15; it closes issue 21;
  (b) issue 21, open at the end, closed on 2026-11-15;
  (c) issue 22, closed on 2026-10-15, reopened on 2026-11-02;
  (d) PR 11, opened on 2026-11-03, merged on 2026-11-20, closes issue 23
      (opened on 2026-10-02, open at the end);
  (e) PR 12, merged on 2026-10-08, untouched since.

Offline: stores are built via gather.fold_bundle.
"""

import contextlib
import copy
import glob
import io
import json
import os
import sys
import tempfile
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

OCT = ("2026-10-01", "2026-10-31")
LATE = ("2026-10-01", "2026-11-30")     # ends after everything stored
URL = "https://github.com/o/r"
INSTANT = "2026-10-31T23:59:59Z"


def _comment(cid, author, ts, kind="issuecomment"):
    return {"id": cid, "author": author, "author_association": "MEMBER",
            "body": "c{}".format(cid), "created_at": ts,
            "url": "{}/pull/0#{}-{}".format(URL, kind, cid)}


def _review(rid, author, state, ts, pr):
    return {"id": rid, "author": author, "state": state, "submitted_at": ts,
            "body": "", "url": "{}/pull/{}#pullrequestreview-{}".format(
                URL, pr, rid)}


def _event(eid, actor, event, ts, kind, num):
    return {"id": eid, "actor": actor, "event": event, "created_at": ts,
            "label": None, "url": "{}/{}/{}#event-{}".format(
                URL, "pull" if kind == "pr" else "issues", num, eid)}


def _pr(number, author, created_at, updated_at, merged_at=None, closes=(),
        reviewers=(), decision="none", reviews=(), comments=(),
        review_comments=(), lifecycle=()):
    rec = {"number": number, "title": "feat: change {}".format(number),
           "body": "", "author": author, "author_association": "MEMBER",
           "labels": ["area/x"], "milestone": None,
           "merged": merged_at is not None, "merged_at": merged_at,
           "merged_by": "maint" if merged_at else None,
           "created_at": created_at, "updated_at": updated_at,
           "closed_at": merged_at, "state": "closed" if merged_at else "open",
           "base": "main", "head": "f{}".format(number), "head_repo": "o/r",
           "comments": len(comments),
           "review_comments_count": len(review_comments),
           "reviewers": list(reviewers), "review_decision": decision,
           "crossref_issues": [], "closes": list(closes),
           "comments_list": list(comments),
           "review_comments": list(review_comments),
           "url": "{}/pull/{}".format(URL, number)}
    if reviews:
        rec["reviews"] = list(reviews)
    if lifecycle:
        rec["lifecycle"] = list(lifecycle)
    return rec


def _issue(number, author, created_at, updated_at, state="open",
           closed_at=None, state_reason=None, comments=(), lifecycle=()):
    rec = {"number": number, "title": "issue {}".format(number), "body": "",
           "kind": "feature", "author": author, "author_association": "MEMBER",
           "labels": [], "assignees": [], "milestone": None, "state": state,
           "state_reason": state_reason, "created_at": created_at,
           "updated_at": updated_at, "closed_at": closed_at,
           "comments": len(comments), "comments_list": list(comments),
           "blocks": [], "url": "{}/issues/{}".format(URL, number)}
    if lifecycle:
        rec["lifecycle"] = list(lifecycle)
    return rec


# (a) open at the end; reviewed, commented on and merged after it.
PR10 = _pr(
    10, "alice", "2026-10-05T09:00:00Z", "2026-11-15T10:00:00Z",
    merged_at="2026-11-15T10:00:00Z", closes=[21],
    reviewers=["bob", "carol"], decision="changes_requested",
    reviews=[_review(101, "bob", "approved", "2026-10-20T12:00:00Z", 10),
             _review(102, "carol", "changes_requested",
                     "2026-11-10T12:00:00Z", 10)],
    comments=[_comment(1001, "dave", "2026-10-12T08:00:00Z"),
              _comment(1002, "erin", "2026-11-05T08:00:00Z")],
    review_comments=[_comment(2001, "bob", "2026-10-21T08:00:00Z", "r"),
                     _comment(2002, "carol", "2026-11-11T08:00:00Z", "r")],
    lifecycle=[_event(3001, "maint", "closed", "2026-11-15T10:00:00Z",
                      "pr", 10)])
# (b) open at the end, closed after it (by PR 10's merge).
ISSUE21 = _issue(
    21, "gina", "2026-09-20T00:00:00Z", "2026-11-15T10:00:00Z",
    state="closed", closed_at="2026-11-15T10:00:00Z",
    state_reason="completed",
    comments=[_comment(1101, "frank", "2026-10-03T00:00:00Z")],
    lifecycle=[_event(3101, "maint", "closed", "2026-11-15T10:00:00Z",
                      "issue", 21)])
# (c) closed in the window, reopened after it.
ISSUE22 = _issue(
    22, "gina", "2026-09-01T00:00:00Z", "2026-11-02T00:00:00Z",
    state="open", state_reason="reopened",
    lifecycle=[_event(3201, "hank", "closed", "2026-10-15T00:00:00Z",
                      "issue", 22),
               _event(3202, "hank", "reopened", "2026-11-02T00:00:00Z",
                      "issue", 22)])
# (d) opened after the window; closes an issue open at the window's end.
PR11 = _pr(11, "ivan", "2026-11-03T00:00:00Z", "2026-11-20T00:00:00Z",
           merged_at="2026-11-20T00:00:00Z", closes=[23])
ISSUE23 = _issue(23, "gina", "2026-10-02T00:00:00Z", "2026-11-20T00:00:00Z",
                 state="closed", closed_at="2026-11-20T00:00:00Z",
                 state_reason="completed")
# (e) untouched since the window.
PR12 = _pr(12, "alice", "2026-10-06T00:00:00Z", "2026-10-08T00:00:00Z",
           merged_at="2026-10-08T00:00:00Z", reviewers=["bob"],
           decision="approved",
           reviews=[_review(121, "bob", "approved", "2026-10-07T00:00:00Z",
                            12)],
           comments=[_comment(1201, "alice", "2026-10-07T00:00:00Z")])

# Open, commented on after everything else: its node is dated by its
# created_at, so the trust check's self-sourced window (min..max node ts)
# ends before kim's comment.
PR13 = _pr(13, "jo", "2026-10-25T00:00:00Z", "2026-11-25T00:00:00Z",
           comments=[_comment(1301, "kim", "2026-11-25T00:00:00Z")])


def _late_october():
    """October as gathered on 2026-11-26."""
    return {"meta": {"owner": "o", "repo": "r", "from": OCT[0], "to": OCT[1],
                     "base_branch": "main", "branches": ["main"]},
            "prs": [copy.deepcopy(p) for p in (PR10, PR11, PR12, PR13)],
            "issues": [copy.deepcopy(i) for i in (ISSUE21, ISSUE22, ISSUE23)],
            "commits": [], "code_events": [], "symbol_events": [],
            "milestones": [], "releases": []}


def _store(path=":memory:", bundle=None):
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    with contextlib.redirect_stderr(io.StringIO()):
        gather.fold_bundle(conn, bundle or _late_october())
    return conn


def _quiet_extract(conn, window, as_of_end):
    with contextlib.redirect_stderr(io.StringIO()):
        return extract.extract(conn, "o", "r", *window, as_of_end=as_of_end)


def _by_number(records):
    return {r["number"]: r for r in records}


def _bucket(bundle, name):
    return sorted((r["type"], r["id"]) for r in bundle["buckets"][name])


class LateGatheredMonth(unittest.TestCase):
    """The report view of October, read from a store folded on 2026-11-26."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "store.db")
        _store(cls.path).close()
        cls.view = cls._view(OCT)
        cls.enriched = link.enrich(copy.deepcopy(cls.view))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _view(cls, window):
        out = os.path.join(cls.tmp.name, "view-{}.json".format(window[1]))
        with contextlib.redirect_stderr(io.StringIO()):
            rc = view.main(["--store", cls.path, "--from", window[0],
                            "--to", window[1], "--out", out])
        assert rc == 0
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)

    def test_items_in_the_view(self):
        self.assertEqual(sorted(_by_number(self.view["prs"])), [10, 12, 13])
        self.assertEqual(sorted(_by_number(self.view["issues"])), [21, 22, 23])

    def test_pr_open_at_the_end_is_open(self):
        pr = _by_number(self.view["prs"])[10]
        self.assertEqual((pr["state"], pr["merged"], pr["merged_at"],
                          pr["merged_by"], pr["closed_at"]),
                         ("open", False, None, None, None))
        self.assertNotIn("lifecycle", pr)

    def test_pr_activity_as_of_the_end(self):
        pr = _by_number(self.view["prs"])[10]
        self.assertEqual([c["id"] for c in pr["comments_list"]], [1001])
        self.assertEqual([c["id"] for c in pr["review_comments"]], [2001])
        self.assertEqual([r["id"] for r in pr["reviews"]], [101])
        self.assertEqual((pr["comments"], pr["review_comments_count"]), (1, 1))
        self.assertEqual((pr["reviewers"], pr["review_decision"]),
                         (["bob"], "approved"))
        self.assertEqual(pr["updated_at"], "2026-10-21T08:00:00Z")

    def test_marker(self):
        pr = _by_number(self.view["prs"])[10]
        marker = pr[extract.AS_OF_KEY]
        self.assertEqual(marker["instant"], INSTANT)
        for field in ("state", "merged", "merged_at", "merged_by", "closed_at",
                      "updated_at", "comments_list", "comments", "reviews",
                      "reviewers", "review_decision", "lifecycle"):
            self.assertIn(field, marker["fields"])
        self.assertIn("labels", marker["not_projected"])
        self.assertIn("closes", marker["not_projected"])
        self.assertEqual(self.view["meta"]["records_as_of"],
                         {"instant": INSTANT, "prs": 2, "issues": 3})
        self.assertNotIn(extract.AS_OF_KEY, _by_number(self.view["prs"])[12])

    def test_issue_closed_after_the_end_is_open(self):
        iss = _by_number(self.view["issues"])[21]
        self.assertEqual((iss["state"], iss["closed_at"], iss["state_reason"],
                          iss["updated_at"]),
                         ("open", None, None, "2026-10-03T00:00:00Z"))

    def test_issue_reopened_after_the_end_is_closed(self):
        iss = _by_number(self.view["issues"])[22]
        self.assertEqual((iss["state"], iss["closed_at"], iss["updated_at"]),
                         ("closed", "2026-10-15T00:00:00Z",
                          "2026-10-15T00:00:00Z"))
        # the reason of a closing later undone is not recorded
        self.assertIsNone(iss["state_reason"])
        self.assertIn("state_reason", iss[extract.AS_OF_KEY]["not_projected"])
        self.assertEqual([e["event"] for e in iss["lifecycle"]], ["closed"])

    def test_issue_closed_by_a_later_pr_is_open(self):
        iss = _by_number(self.view["issues"])[23]
        self.assertEqual((iss["state"], iss["updated_at"]),
                         ("open", "2026-10-02T00:00:00Z"))

    def test_untouched_pr_is_as_stored(self):
        stored = copy.deepcopy(PR12)
        got = _by_number(self.view["prs"])[12]
        for key, value in stored.items():
            self.assertEqual(got[key], value, key)

    def test_buckets(self):
        b = self.enriched
        self.assertEqual(_bucket(b, "shipped"), [("pr", 12)])
        # PR 10 was approved on 2026-10-20 and the change request came on
        # 2026-11-10, after the end: as of the end it is an approved open PR,
        # a next candidate (#97 final gate), not merely in flight
        self.assertEqual(_bucket(b, "in_flight"),
                         [("issue", 21), ("issue", 23), ("pr", 13)])
        self.assertEqual(_bucket(b, "next_candidates"), [("pr", 10)])
        self.assertEqual(_bucket(b, "rejected"), [])

    def test_train_outcomes(self):
        trains = {t["id"]: t for t in self.enriched["trains"]}
        self.assertEqual(sorted(trains),
                         ["train-issue-21", "train-pr-12", "train-pr-13"])
        self.assertEqual(trains["train-issue-21"]["outcome"], "in_flight")
        self.assertEqual(trains["train-issue-21"]["pr_outcomes"],
                         {"10": "open"})
        self.assertEqual(trains["train-pr-12"]["outcome"], "shipped")

    def test_effort_as_of_the_end(self):
        effort = {t["id"]: t["effort"]
                  for t in self.enriched["trains"]}["train-issue-21"]
        self.assertEqual(effort["last_activity_at"], "2026-10-21T08:00:00Z")
        self.assertEqual(effort["idle_days"], 10)
        self.assertFalse(effort["stalled"])
        self.assertFalse(effort["slow_to_land"])
        self.assertIsNone(effort["merged_at"])
        self.assertEqual(effort["reviewers"], 1)
        self.assertEqual(effort["review_comments"], 1)
        # alice (author), gina (issue), bob (reviewer), dave, frank (comments)
        self.assertEqual(effort["participants"], 5)

    def test_people_as_of_the_end(self):
        people = self.enriched["people"]
        self.assertEqual(sorted(people),
                         ["alice", "bob", "dave", "frank", "gina", "jo",
                          "maint"])
        self.assertNotIn("carol", people)     # reviewed after the end only
        self.assertNotIn("erin", people)      # commented after the end only
        self.assertNotIn("ivan", people)      # PR opened after the end
        self.assertNotIn("kim", people)       # commented after the end only
        self.assertEqual(people["alice"]["prs_authored"], 2)
        self.assertEqual(people["bob"]["prs_reviewed"], 2)
        # PR 10 was not merged yet: alice's merged PRs are PR 12 only, and
        # its merger's last activity is PR 12's merge
        self.assertEqual(people["alice"]["prs_merged"], 1)
        self.assertEqual(people["maint"]["last_active"][:10], "2026-10-08")

    def test_timeline_holds_nothing_after_the_end(self):
        stamps = [e["ts"] for e in self.enriched["timeline"]]
        self.assertTrue(stamps)
        self.assertTrue(all(t[:10] <= OCT[1] for t in stamps), stamps)

    def test_window_after_everything_is_unprojected(self):
        late = self._view(LATE)
        self.assertNotIn("records_as_of", late["meta"])
        for kind in ("prs", "issues"):
            for rec in late[kind]:
                self.assertNotIn(extract.AS_OF_KEY, rec)
        conn = graphstore.open_store_readonly(self.path)
        try:
            stored = _quiet_extract(conn, LATE, as_of_end=False)
        finally:
            conn.close()
        self.assertEqual(json.dumps(late, sort_keys=True),
                         json.dumps(stored, sort_keys=True))
        pr10 = _by_number(late["prs"])[10]
        self.assertEqual((pr10["merged"], pr10["reviewers"]),
                         (True, ["bob", "carol"]))
        self.assertEqual(sorted(_by_number(late["prs"])), [10, 11, 12, 13])

    def test_digest_is_as_of_the_end(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = digest.main(["--store", self.path, "--project", "o",
                              "--from", OCT[0], "--to", OCT[1]])
        self.assertEqual(rc, 0)
        pv = json.loads(buf.getvalue())
        self.assertEqual(pv["meta"]["records_as_of"],
                         {"instant": INSTANT, "members": ["r"]})
        member = pv["members"][0]["bundle"]
        self.assertEqual(json.dumps(member["trains"], sort_keys=True),
                         json.dumps(self.enriched["trains"], sort_keys=True))
        outcomes = {t["id"]: t["outcome"] for t in pv["trains"]}
        self.assertEqual(outcomes, {"ptrain-o/r#issue-21": "in_flight",
                                    "ptrain-o/r#pr-12": "shipped",
                                    "ptrain-o/r#pr-13": "in_flight"})
        self.assertNotIn("carol", pv["people"])
        self.assertEqual(pv["people"]["bob"]["prs_reviewed"], 2)


class LateGatherOfOneMonthAlone(unittest.TestCase):
    """What a gather of October run on 2026-11-26 collects on its own: PRs
    merged / closed in October or open now (not PR 10, merged in November,
    nor PR 11), every issue updated since October 1st or open. Its own view
    is projected too: the issues closed since are in flight again."""

    def test_in_flight_as_of_the_end(self):
        bundle = _late_october()
        bundle["prs"] = [p for p in bundle["prs"] if p["number"] in (12, 13)]
        conn = _store(bundle=bundle)
        try:
            got = link.enrich(_quiet_extract(conn, OCT, as_of_end=True))
        finally:
            conn.close()
        self.assertEqual(_bucket(got, "in_flight"),
                         [("issue", 21), ("issue", 23), ("pr", 13)])
        self.assertEqual(_bucket(got, "shipped"), [("pr", 12)])
        self.assertEqual(got["meta"]["records_as_of"],
                         {"instant": INSTANT, "prs": 1, "issues": 3})


class TrustCheckKeepsFullRecords(unittest.TestCase):
    """validate self-sources its bundle from the store: it must read the full
    stored records (PR 10's `merged` edge exists although it was merged after
    the window's end), while the report view is projected."""

    def test_late_store_validates_green_and_view_is_projected(self):
        conn = _store()
        try:
            report = validate.validate(conn, project="o", repo="r")
            failing = [c for c in report.checks if not c["ok"]]
            self.assertEqual(failing, [],
                             json.dumps(failing, indent=1, default=str))
            full = _quiet_extract(conn, OCT, as_of_end=False)
            projected = _quiet_extract(conn, OCT, as_of_end=True)
            # validate's self-sourced window ends at the latest node ts
            # (PR 11's merge, 2026-11-20); kim's comment on PR 13 is later.
            span = validate._store_window(conn, "o", "r")
            sourced = extract.extract(conn, "o", "r", *span,
                                      warn=lambda _m: None)
            trap = _quiet_extract(conn, span, as_of_end=True)
        finally:
            conn.close()
        self.assertEqual(span[1], "2026-11-20T00:00:00Z")
        self.assertIn("kim", sourced["people"])       # what validate reads
        self.assertTrue(_by_number(sourced["prs"])[10]["merged"])
        self.assertNotIn("records_as_of", sourced["meta"])
        self.assertNotIn("kim", trap["people"])       # projected: dropped
        self.assertNotIn("records_as_of", full["meta"])
        # kept as stored (merged in November): open at the window's end, so
        # in the window, as gather.keep_pr_for_window keeps it
        self.assertTrue(_by_number(full["prs"])[10]["merged"])
        self.assertEqual(projected["meta"]["records_as_of"]["prs"], 2)
        self.assertFalse(_by_number(projected["prs"])[10]["merged"])

    def test_as_stored_view_is_the_unprojected_view(self):
        """`view.py --as-stored` writes the records as stored — what
        `validate.py --bundle` compares with the store — exactly as before
        issue #97."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "store.db")
            _store(path).close()
            out = os.path.join(d, "as_stored.json")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = view.main(["--store", path, "--from", OCT[0],
                                "--to", OCT[1], "--out", out, "--as-stored"])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                got = json.load(fh)
            conn = graphstore.open_store_readonly(path)
            try:
                stored = _quiet_extract(conn, OCT, as_of_end=False)
            finally:
                conn.close()
        self.assertEqual(json.dumps(got, sort_keys=True),
                         json.dumps(stored, sort_keys=True))
        self.assertNotIn("records_as_of", got["meta"])

    def test_extract_default_reads_records_as_stored(self):
        conn = _store()
        try:
            got = _quiet_extract(conn, LATE, False)
            default = extract.extract(conn, "o", "r", *OCT,
                                      warn=lambda _m: None)
        finally:
            conn.close()
        self.assertTrue(_by_number(got["prs"])[10]["merged"])
        for rec in default["prs"] + default["issues"]:
            self.assertNotIn(extract.AS_OF_KEY, rec)


class RecordAsOf(unittest.TestCase):
    """The projection rule on single records."""

    END = "2026-10-31"

    def _issue(self, **kw):
        rec = _issue(1, "gina", "2026-09-01T00:00:00Z",
                     kw.pop("updated_at", "2026-11-20T00:00:00Z"), **kw)
        return rec

    def test_nothing_later_returns_the_record_itself(self):
        rec = copy.deepcopy(PR12)
        self.assertIs(extract.record_as_of("pr", rec, self.END), rec)

    def test_created_after_the_end_is_gone(self):
        self.assertIsNone(extract.record_as_of("pr", copy.deepcopy(PR11),
                                               self.END))

    def test_record_is_not_changed(self):
        rec = copy.deepcopy(PR10)
        before = json.dumps(rec, sort_keys=True)
        extract.record_as_of("pr", rec, self.END)
        self.assertEqual(json.dumps(rec, sort_keys=True), before)

    def test_closed_and_reopened_before_the_end_is_open(self):
        rec = self._issue(
            state="closed", closed_at="2026-11-20T00:00:00Z",
            state_reason="completed",
            lifecycle=[_event(1, "h", "closed", "2026-10-05T00:00:00Z",
                              "issue", 1),
                       _event(2, "h", "reopened", "2026-10-09T00:00:00Z",
                              "issue", 1),
                       _event(3, "h", "closed", "2026-11-20T00:00:00Z",
                              "issue", 1)])
        got = extract.record_as_of("issue", rec, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["state_reason"],
                          got["updated_at"]),
                         ("open", None, "reopened", "2026-10-09T00:00:00Z"))

    def test_last_close_after_the_end_is_open(self):
        rec = self._issue(
            state="closed", closed_at="2026-11-20T00:00:00Z",
            state_reason="completed",
            lifecycle=[_event(3, "h", "closed", "2026-11-20T00:00:00Z",
                              "issue", 1)])
        got = extract.record_as_of("issue", rec, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["state_reason"]),
                         ("open", None, None))

    def test_closed_before_the_end_keeps_its_reason(self):
        rec = self._issue(
            state="closed", closed_at="2026-10-20T00:00:00Z",
            state_reason="not_planned",
            comments=[_comment(9, "x", "2026-11-21T00:00:00Z")],
            lifecycle=[_event(3, "h", "closed", "2026-10-20T00:00:00Z",
                              "issue", 1)])
        got = extract.record_as_of("issue", rec, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["state_reason"],
                          got["comments"], got["comments_list"]),
                         ("closed", "2026-10-20T00:00:00Z", "not_planned", 0,
                          []))
        self.assertEqual(got[extract.AS_OF_KEY]["fields"],
                         ["comments", "comments_list", "updated_at"])

    def test_no_lifecycle_falls_back_to_closed_at(self):
        after = self._issue(state="closed", closed_at="2026-11-20T00:00:00Z",
                            state_reason="completed")
        got = extract.record_as_of("issue", after, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["state_reason"]),
                         ("open", None, None))
        before = self._issue(state="closed", closed_at="2026-10-20T00:00:00Z",
                             state_reason="completed")
        got = extract.record_as_of("issue", before, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["state_reason"]),
                         ("closed", "2026-10-20T00:00:00Z", "completed"))
        self.assertEqual(got["updated_at"], "2026-10-20T00:00:00Z")

    def test_pr_closed_unmerged_before_the_end_stays_closed(self):
        rec = _pr(5, "a", "2026-10-01T00:00:00Z", "2026-11-03T00:00:00Z",
                  comments=[_comment(1, "b", "2026-11-03T00:00:00Z")])
        rec.update(state="closed", closed_at="2026-10-04T00:00:00Z")
        got = extract.record_as_of("pr", rec, self.END)
        self.assertEqual((got["state"], got["closed_at"], got["merged"]),
                         ("closed", "2026-10-04T00:00:00Z", False))

    def test_reviewers_without_submissions_are_not_projected(self):
        rec = _pr(5, "a", "2026-10-01T00:00:00Z", "2026-11-03T00:00:00Z",
                  reviewers=["z"], decision="approved")
        got = extract.record_as_of("pr", rec, self.END)
        self.assertEqual(got["reviewers"], ["z"])
        self.assertIn("reviewers", got[extract.AS_OF_KEY]["not_projected"])
        self.assertIn("review_decision",
                      got[extract.AS_OF_KEY]["not_projected"])

    def test_timestamp_end_is_an_instant(self):
        # PR 12: created 10-06T00:00, reviewed and commented 10-07T00:00,
        # merged 10-08T00:00
        rec = copy.deepcopy(PR12)
        got = extract.record_as_of("pr", rec, "2026-10-06T12:00:00Z")
        self.assertEqual((got["state"], got["merged"]), ("open", False))
        self.assertEqual(got[extract.AS_OF_KEY]["instant"],
                         "2026-10-06T12:00:00Z")
        self.assertEqual((got["reviewers"], got["review_decision"]),
                         ([], "none"))
        self.assertNotIn("reviews", got)
        self.assertEqual((got["comments"], got["updated_at"]),
                         (0, "2026-10-06T00:00:00Z"))
        # the same second as the end is not after it
        same = extract.record_as_of("pr", rec, "2026-10-07T00:00:00Z")
        self.assertEqual(same["reviewers"], ["bob"])
        self.assertEqual(extract.as_of_instant("2026-10-31"), INSTANT)


class FixturesUnchanged(unittest.TestCase):
    """Every fixtures/bundle_*.json folded and viewed over its own window is
    as stored: nothing in it is later than its window's end."""

    def test_fixture_views_are_unprojected(self):
        paths = sorted(glob.glob(os.path.join(HERE, "fixtures",
                                              "bundle_*.json")))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    bundle = json.load(fh)
                window = (bundle["meta"]["from"], bundle["meta"]["to"])
                conn = _store(bundle=bundle)
                try:
                    as_of = _quiet_extract(conn, window, True)
                    stored = _quiet_extract(conn, window, False)
                finally:
                    conn.close()
                self.assertEqual(json.dumps(as_of, sort_keys=True),
                                 json.dumps(stored, sort_keys=True))
                self.assertNotIn("records_as_of", as_of["meta"])


class ProjectedAnchorsSeedTheTraversal(unittest.TestCase):
    """Review finding 1: a PR created in October and merged in November is in
    October's projected view, but its stored timestamp (the merge) is not in
    the window, so range_query does not seed it. What it alone reaches and
    the store lacks (an ungathered issue it closes) must still be warned
    about, or backfilled, like any anchor's train context."""

    def _store(self):
        bundle = _late_october()
        pr = _pr(30, "alice", "2026-10-05T00:00:00Z", "2026-11-10T00:00:00Z",
                 merged_at="2026-11-10T00:00:00Z", closes=[99])
        bundle["prs"], bundle["issues"] = [pr], []
        return _store(bundle=bundle)

    def test_missing_context_is_warned(self):
        conn = self._store()
        warnings = []
        try:
            got = extract.extract(conn, "o", "r", *OCT, warn=warnings.append,
                                  as_of_end=True)
        finally:
            conn.close()
        self.assertFalse(_by_number(got["prs"])[30]["merged"])
        self.assertTrue(any("o/r#issue-99" in w for w in warnings), warnings)

    def test_missing_context_is_backfilled_and_in_the_view(self):
        conn = self._store()
        asked = []
        payload = {"node": {"number": 99, "title": "the bug", "state": "closed",
                            "state_reason": "completed",
                            "created_at": "2026-09-01T00:00:00Z",
                            "updated_at": "2026-11-10T00:00:00Z",
                            "closed_at": "2026-11-10T00:00:00Z",
                            "author": "gina", "comments_list": [],
                            "url": URL + "/issues/99"}}

        def fetch(kind, local, qid):
            asked.append(local)
            return payload if local == "issue-99" else None
        try:
            got = extract.extract(
                conn, "o", "r", *OCT, warn=lambda _m: None, as_of_end=True,
                backfill=lambda c, i: gather.backfill(c, i, fetch=fetch))
        finally:
            conn.close()
        self.assertIn("issue-99", asked)
        issue = _by_number(got["issues"])[99]        # read after the backfill
        self.assertEqual((issue["state"], issue["closed_at"]), ("open", None))


class DismissedReviewsAreNotProjected(unittest.TestCase):
    """Review finding 2: GitHub rewrites a dismissed review's state without
    moving its submission date, and the store keeps no dismissal date. An
    October approval dismissed in November reads `dismissed` in October's
    record: the decision cannot be re-derived as of the end, so the stored
    one is kept and named in `not_projected`."""

    def test_decision_with_a_dismissed_review_is_not_projected(self):
        rec = _pr(40, "alice", "2026-10-01T00:00:00Z", "2026-11-05T00:00:00Z",
                  reviewers=["bob", "carol"], decision="changes_requested",
                  reviews=[_review(401, "bob", "dismissed",
                                   "2026-10-05T00:00:00Z", 40),
                           _review(402, "carol", "changes_requested",
                                   "2026-11-05T00:00:00Z", 40)])
        got = extract.record_as_of("pr", rec, "2026-10-31")
        self.assertEqual([r["id"] for r in got["reviews"]], [401])
        self.assertEqual(got["reviewers"], ["bob"])
        self.assertEqual(got["review_decision"], "changes_requested")
        marker = got[extract.AS_OF_KEY]
        self.assertIn("review_decision", marker["not_projected"])
        self.assertIn("review_rounds", marker["not_projected"])
        self.assertNotIn("review_decision", marker["fields"])

    def test_decision_without_a_dismissed_review_is_projected(self):
        got = extract.record_as_of("pr", copy.deepcopy(PR10), "2026-10-31")
        self.assertEqual(got["review_decision"], "approved")
        self.assertNotIn("review_decision",
                         got[extract.AS_OF_KEY]["not_projected"])


class LateMonthBundleCrossCheck(unittest.TestCase):
    """Review finding 3: a month gathered late keeps a PR open at its end and
    merged since (gather.keep_pr_for_window) and an issue open at its end and
    closed since (updated since the window's start). `view.py --as-stored`
    must hold them, so `validate.py --bundle` on that view agrees with the
    store; the store-only audit is green too."""

    def _bundle(self):
        # what a gather of October run on 2026-11-26 returns
        prs = [_pr(50, "zed", "2026-10-10T00:00:00Z", "2026-11-12T00:00:00Z",
                   merged_at="2026-11-12T00:00:00Z"),
               copy.deepcopy(PR12)]
        issues = [_issue(60, "gina", "2026-10-01T00:00:00Z",
                         "2026-11-03T00:00:00Z", state="closed",
                         closed_at="2026-11-03T00:00:00Z",
                         state_reason="completed",
                         comments=[_comment(6001, "yan",
                                            "2026-10-04T00:00:00Z")])]
        for pr in prs:
            self.assertTrue(gather.keep_pr_for_window(pr, *OCT))
        bundle = _late_october()
        bundle["prs"], bundle["issues"] = prs, issues
        return bundle

    def test_as_stored_view_passes_validate_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "store.db")
            _store(db, bundle=self._bundle()).close()
            out = os.path.join(d, "at-gather.json")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = view.main(["--store", db, "--from", OCT[0], "--to", OCT[1],
                                "--as-stored", "--out", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(sorted(_by_number(saved["prs"])), [12, 50])
            self.assertEqual(sorted(_by_number(saved["issues"])), [60])
            self.assertIn("zed", saved["people"])
            self.assertIn("yan", saved["people"])
            for argv in ([db], [db, "--bundle", out]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = validate.main(argv)
                self.assertEqual(rc, 0, (argv, buf.getvalue()))


if __name__ == "__main__":
    unittest.main()


class TestGatherKeepsPrsOpenAtTheWindowEnd(unittest.TestCase):
    """A month gathered late: a PR that was open at the month's end and has
    landed since must be gathered, or the as-of view cannot show it."""

    def _pr(self, **kw):
        base = {"state": "closed", "merged": False, "merged_at": None,
                "closed_at": None, "created_at": "2026-10-10T00:00:00Z"}
        return {**base, **kw}

    def test_keep_rule(self):
        keep = lambda pr: gather.keep_pr_for_window(pr, "2026-10-01", "2026-10-31")
        # open now: kept when it existed at the window's end; one opened
        # after it is in no view of the window (validation finding 7)
        self.assertTrue(keep(self._pr(state="open", created_at="2026-10-31T23:59:59Z")))
        self.assertFalse(keep(self._pr(state="open", created_at="2026-11-20T00:00:00Z")))
        # as before
        self.assertTrue(keep(self._pr(merged=True, merged_at="2026-10-31T23:59:59Z",
                                      closed_at="2026-10-31T23:59:59Z")))
        self.assertTrue(keep(self._pr(closed_at="2026-10-05T00:00:00Z")))
        self.assertFalse(keep(self._pr(merged=True, merged_at="2026-09-30T23:00:00Z",
                                       closed_at="2026-09-30T23:00:00Z",
                                       created_at="2026-09-01T00:00:00Z")))
        # open at the window end, landed or closed since
        self.assertTrue(keep(self._pr(merged=True, merged_at="2026-11-15T00:00:00Z",
                                      closed_at="2026-11-15T00:00:00Z")))
        self.assertTrue(keep(self._pr(closed_at="2026-11-02T00:00:00Z",
                                      created_at="2026-08-01T00:00:00Z")))
        # opened after the window and already landed: not this window's
        self.assertFalse(keep(self._pr(merged=True, merged_at="2026-11-15T00:00:00Z",
                                       closed_at="2026-11-15T00:00:00Z",
                                       created_at="2026-11-03T00:00:00Z")))
