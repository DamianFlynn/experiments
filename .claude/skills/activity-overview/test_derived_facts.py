"""Regression tests for the derived-facts findings of issue #110: digest project
trains (item 1), the PR author as a reviewer (5), series window keys and
overlapping windows (6, 10), `closes` numbers that are PRs (7), link.py on an
unreadable bundle (8) and CODEOWNERS directory rules (9). The diagram findings
(2, 3, 4) are in test_diagram_safety.py."""

import contextlib
import copy
import glob
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import series  # noqa: E402
import test_digest  # noqa: E402
import validate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def _pr(n, *, merged=False, state="open", closes=(), mentions=(), base="main",
        author="alice", reviewers=(), reviews=None):
    pr = {"number": n, "url": "https://github.com/o/r/pull/{}".format(n),
          "state": state, "merged": merged, "base": base, "head": "h{}".format(n),
          "merged_at": "2026-01-10T00:00:00Z" if merged else None,
          "created_at": "2026-01-05T00:00:00Z",
          "closed_at": "2026-01-10T00:00:00Z" if state == "closed" else None,
          "closes": list(closes), "crossref_issues": list(mentions),
          "title": "feat: change {}".format(n), "body": "", "author": author,
          "reviewers": list(reviewers), "review_decision": "none"}
    if reviews is not None:
        pr["reviews"] = reviews
    return pr


def _issue(n, state="closed", reason=None):
    return {"number": n, "url": "https://github.com/o/r/issues/{}".format(n),
            "state": state, "state_reason": reason,
            "closed_at": "2026-01-09T00:00:00Z" if state == "closed" else None,
            "updated_at": "2026-01-09T00:00:00Z",
            "created_at": "2026-01-02T00:00:00Z", "title": "issue {}".format(n),
            "author": "carol"}


def _raw_bundle(prs, issues, commits=()):
    return {"meta": {"owner": "o", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": prs, "issues": issues, "commits": list(commits),
            "code_events": [], "milestones": [], "releases": [],
            "code_graph": {"areas": []}}


def _store(bundle):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(bundle))
    return conn


def _project_view(bundle):
    meta = bundle["meta"]
    conn = _store(bundle)
    return digest.build_project_view(conn, meta["owner"], [meta["repo"]],
                                     meta["from"], meta["to"],
                                     warn=lambda _m: None)


def _shape_of_link_train(project, repo, t):
    """A member (link) train in the project-train vocabulary."""
    def q(local):
        return graphstore.qualify_id(project, repo, local)
    anchor = (q("issue-{}".format(t["root_issue"])) if t["root_issue"] is not None
              else q("pr-{}".format(t["prs"][0])))
    return ("ptrain-" + anchor, t["outcome"],
            tuple(sorted(q("pr-{}".format(n)) for n in t["prs"])),
            tuple(sorted(q("issue-{}".format(n)) for n in t["issues"])))


def _shape_of_project_train(t):
    return (t["id"], t["outcome"], tuple(t["prs"]), tuple(t["issues"]))


# --------------------------------------------------------------------- item 1
class TestProjectTrainsAreLinkTrains(unittest.TestCase):
    """Item 1: the project trains of a single-member project are exactly that
    member's link trains: same grouping, same outcomes. A plain mention
    (cross_ref) joins no trains."""

    def assertSameTrains(self, view):
        member = view["members"][0]
        meta = member["bundle"]["meta"]
        expected = sorted(_shape_of_link_train(meta["owner"], member["repo"], t)
                          for t in member["bundle"]["trains"])
        self.assertEqual(sorted(_shape_of_project_train(t) for t in view["trains"]),
                         expected)

    def test_reviewer_case_mention_does_not_merge_trains(self):
        b = _raw_bundle(
            [_pr(1, merged=True, state="closed", closes=[2]),
             _pr(3, closes=[4], mentions=[2])],
            [_issue(2), _issue(4, state="open")])
        view = _project_view(b)
        self.assertEqual(
            [(t["id"], t["outcome"]) for t in view["trains"]],
            [("ptrain-o/r#issue-2", "shipped"), ("ptrain-o/r#issue-4", "in_flight")])
        self.assertSameTrains(view)

    def test_every_bundle_fixture(self):
        for path in sorted(glob.glob(os.path.join(FIX, "bundle_*.json"))):
            with self.subTest(fixture=os.path.basename(path)):
                with open(path) as fh:
                    self.assertSameTrains(_project_view(json.load(fh)))

    def test_closes_still_joins_and_cross_repo_closes_still_joins(self):
        # two PRs closing one issue: one train (closes makes link trains)
        b = _raw_bundle([_pr(1, merged=True, state="closed", closes=[5]),
                         _pr(2, state="closed", closes=[5])], [_issue(5)])
        view = _project_view(b)
        self.assertEqual([t["id"] for t in view["trains"]], ["ptrain-o/r#issue-5"])
        self.assertSameTrains(view)
        # the cross-repo closes case of test_digest still spans both members
        conn = graphstore.open_store(":memory:")
        test_digest._seed_two_member_store(conn)
        comps = digest.spine_components(conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
                                        "2026-01-01", "2026-01-31")
        self.assertEqual(len(comps), 1)

    def test_cross_repo_mention_does_not_join_members(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/a", "Azure/b"}
        a = _raw_bundle([_pr(1, merged=True, state="closed")], [])
        a["prs"][0]["body"] = "see https://github.com/Azure/b/issues/3"
        a["prs"][0]["url"] = "https://github.com/Azure/a/pull/1"
        bb = _raw_bundle([_pr(4, merged=True, state="closed", closes=[3])],
                         [_issue(3)])
        gather.fold_bundle(conn, a, project="p", repo="Azure/a", members=members)
        gather.fold_bundle(conn, bb, project="p", repo="Azure/b", members=members)
        view = digest.build_project_view(conn, "p", ["Azure/a", "Azure/b"],
                                         "2026-01-01", "2026-01-31",
                                         warn=lambda _m: None)
        self.assertEqual([t["repos"] for t in view["trains"]],
                         [["Azure/a"], ["Azure/b"]])

    def test_pr_that_closes_a_pr_number_does_not_join(self):
        # item 7 seen from digest: "Fixes #1" where #1 is a PR is no issue, so
        # PR 1 and PR 2 stay two trains, in link and in the project view.
        b = _raw_bundle([_pr(1, merged=True, state="closed"),
                         _pr(2, merged=True, state="closed", closes=[1]),
                         _pr(3, state="open", closes=[1])], [])
        view = _project_view(b)
        self.assertEqual([t["id"] for t in view["trains"]],
                         ["ptrain-o/r#pr-1", "ptrain-o/r#pr-2", "ptrain-o/r#pr-3"])
        self.assertSameTrains(view)

    def test_stored_issue_alias_of_a_pr_does_not_join(self):
        # the same case when the store also holds `issue-1` for PR 1 (a
        # backfilled "Closes #1" target that turned out to be a PR): the alias
        # must not pull PR 2 and the open PR 3 into one train (review finding)
        b = _raw_bundle([_pr(1, merged=True, state="closed"),
                         _pr(2, merged=True, state="closed", closes=[1]),
                         _pr(3, state="open", closes=[1])], [])
        meta = b["meta"]
        conn = _store(b)
        alias = graphstore.qualify_id(meta["owner"], meta["repo"], "issue-1")
        graphstore.upsert_node(
            conn, alias, meta["owner"], meta["repo"], "social",
            "2026-01-05T00:00:00Z",
            {"number": 1, "state": "closed", "title": "pr one",
             "url": "https://github.com/o/r/pull/1"}, graphstore.now_iso())
        conn.commit()
        view = digest.build_project_view(conn, meta["owner"], [meta["repo"]],
                                         meta["from"], meta["to"],
                                         warn=lambda _m: None)
        self.assertEqual([t["id"] for t in view["trains"]],
                         ["ptrain-o/r#pr-1", "ptrain-o/r#pr-2", "ptrain-o/r#pr-3"])
        self.assertEqual([t["outcome"] for t in view["trains"]],
                         ["shipped", "shipped", "in_flight"])

    def test_stacked_pr_closing_an_issue_does_not_join(self):
        # a PR merged into another PR's branch is not a train member (link adds
        # it as a contributing PR), so its closing reference joins nothing.
        stacked = _pr(9, merged=True, state="closed", closes=[2], base="h3")
        b = _raw_bundle([_pr(1, merged=True, state="closed", closes=[2]),
                         _pr(3, state="open", closes=[4]), stacked],
                        [_issue(2), _issue(4, state="open")])
        view = _project_view(b)
        self.assertSameTrains(view)

    def test_spine_constant_unchanged(self):
        # extract (train context) and spotlight still walk every spine edge.
        self.assertIn("cross_ref", graphstore.SPINE_EDGE_TYPES)
        self.assertEqual(digest.TRAIN_EDGE_TYPES, ("closes", "part_of"))


# --------------------------------------------------------------------- item 5
def _review(rid, login, state, at="2026-01-06T00:00:00Z"):
    return {"id": rid, "user": {"login": login}, "state": state,
            "submitted_at": at, "html_url": "https://github.com/o/r/pull/1#r{}".format(rid)}


class TestAuthorIsNotAReviewer(unittest.TestCase):
    def test_summarize_reviews_excludes_author(self):
        raw = [_review(1, "alice", "COMMENTED"), _review(2, "bob", "APPROVED")]
        self.assertEqual(gather.summarize_reviews(raw, author="alice"),
                         {"reviewers": ["bob"], "decision": "approved"})
        # without the author the old reduction is unchanged
        self.assertEqual(gather.summarize_reviews(raw)["reviewers"], ["alice", "bob"])

    def test_only_author_reviews_decide_nothing(self):
        raw = [_review(1, "alice", "COMMENTED"), _review(2, "alice", "COMMENTED")]
        self.assertEqual(gather.summarize_reviews(raw, author="alice"),
                         {"reviewers": [], "decision": "none"})

    def test_enrich_pr_passes_the_author(self):
        pr = {"number": 1, "merged": False, "author": "alice", "crossref_issues": []}
        reviews = [_review(1, "alice", "COMMENTED"), _review(2, "bob", "COMMENTED")]

        def get_page(url):
            return (reviews if url.split("?")[0].endswith("/reviews") else []), None

        gather.enrich_pr(pr, get_page, "https://api.github.com/repos/o/r", "o/r")
        self.assertEqual(pr["reviewers"], ["bob"])
        self.assertEqual(pr["review_decision"], "commented")
        # the submissions themselves are kept (review rounds already skip the
        # author's own)
        self.assertEqual([r["author"] for r in pr["reviews"]], ["alice", "bob"])

    def _self_comment_bundle(self):
        raw = [_review(11, "alice", "COMMENTED")]
        summary = gather.summarize_reviews(raw, author="alice")
        pr = _pr(1, merged=True, state="closed", closes=[2], author="alice",
                 reviewers=summary["reviewers"],
                 reviews=[gather.normalize_review(r) for r in raw])
        pr["review_decision"] = summary["decision"]
        return _raw_bundle([pr], [_issue(2)])

    def test_store_with_author_self_comment_validates_and_counts_no_review(self):
        b = self._self_comment_bundle()
        conn = _store(b)
        report = validate.validate(conn, project="o", repo="r")
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        reviewed = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE edge_type='reviewed'").fetchone()[0]
        self.assertEqual(reviewed, 0)
        view = link.enrich(extract.extract(
            conn, "o", "r", "2026-01-01", "2026-01-31", warn=lambda _m: None))
        self.assertEqual(view["people"]["alice"].get("prs_reviewed", 0), 0)
        self.assertEqual(view["trains"][0]["effort"]["reviewers"], 0)
        self.assertEqual(derive.review_latency_samples(view["prs"]), {})


# ------------------------------------------------------------- items 6 and 10
def _min_enrichable(frm, to, owner="o", repo="r"):
    return {"meta": {"owner": owner, "repo": repo, "from": frm, "to": to,
                     "ref_date": to, "period": {"from": frm, "to": to}},
            "commits": [], "prs": [], "issues": [], "milestones": []}


class _SeriesDisk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.spath = os.path.join(self.dir, "series.json")

    def run_link(self, bundle):
        p = os.path.join(self.dir, "b.json")
        with open(p, "w") as fh:
            json.dump(bundle, fh)
        with contextlib.redirect_stderr(io.StringIO()):
            link.main([p, "--series", self.spath])
        with open(p) as fh:
            return json.load(fh)["series"]

    def installments(self):
        with open(self.spath) as fh:
            return json.load(fh)["installments"]

    def refused(self, bundle):
        p = os.path.join(self.dir, "b.json")
        with open(p, "w") as fh:
            json.dump(bundle, fh)
        with open(self.spath) as fh:
            index_before = fh.read()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            link.main([p, "--series", self.spath])
        self.assertEqual(cm.exception.code, 2)
        with open(self.spath) as fh:
            self.assertEqual(fh.read(), index_before)
        with open(p) as fh:
            self.assertNotIn("series", json.load(fh))
        return err.getvalue()


class TestSeriesCalendarDayKeys(_SeriesDisk):
    """Item 6: an installment is identified by its window's calendar days."""

    def test_timestamp_spelling_replaces_the_date_spelling(self):
        self.run_link(_min_enrichable("2025-12-01", "2025-12-31"))
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        jan = self.run_link(_min_enrichable("2026-01-01T00:00:00Z",
                                            "2026-01-31T23:59:59Z"))
        self.assertEqual(jan["prior"]["from"], "2025-12-01")    # not itself
        inst = self.installments()
        self.assertEqual([(i["from"], i["to"]) for i in inst],
                         [("2025-12-01", "2025-12-31"),
                          ("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")])

    def test_window_key_and_order_use_days(self):
        self.assertEqual(series.window_key(("2026-01-01T00:00:00Z",
                                            "2026-01-31T23:59:59+02:00")),
                         ("2026-01-01", "2026-01-31"))
        # an offset that crosses midnight is the same UTC day (review finding)
        self.assertEqual(
            series.window_key(("2025-12-31T19:00:00-05:00", "2026-01-31")),
            series.window_key(("2026-01-01T00:00:00Z", "2026-01-31")))
        idx = [{"from": "2026-02-01T00:00:00Z", "to": "2026-02-28T23:59:59Z"},
               {"from": "2026-01-01", "to": "2026-01-31"}]
        self.assertEqual(series.predecessor(idx, ("2026-02-01", "2026-02-28"))["from"],
                         "2026-01-01")
        self.assertEqual(series.later_installments(
            idx, ("2026-01-01T00:00:00Z", "2026-01-31T00:00:00Z"))[0]["to"],
            "2026-02-28T23:59:59Z")

    def test_installment_window_that_is_not_a_date_is_refused(self):
        with open(self.spath, "w") as fh:
            json.dump({"version": 2, "installments": [
                {"from": "January", "to": "2026-01-31", "shipped": []}]}, fh)
        self.assertIn("January", self.refused(_min_enrichable("2026-02-01",
                                                              "2026-02-28")))

    def test_bundle_window_that_is_not_a_date_is_refused(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.assertIn("Feb", self.refused(_min_enrichable("Feb", "2026-02-28")))


class TestSeriesOverlappingWindows(_SeriesDisk):
    """Items 6 and 10: one index holds non-overlapping windows; a roll-up
    belongs in its own index."""

    def test_quarter_after_months_is_refused_naming_both(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.run_link(_min_enrichable("2026-02-01", "2026-02-28"))
        err = self.refused(_min_enrichable("2026-01-01", "2026-03-31"))
        self.assertIn("2026-01-01 to 2026-03-31", err)
        self.assertIn("2026-01-01 to 2026-01-31", err)
        self.assertIn("overlaps", err)

    def test_month_inside_a_recorded_quarter_is_refused(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-03-31"))
        err = self.refused(_min_enrichable("2026-02-01", "2026-02-28"))
        self.assertIn("2026-01-01 to 2026-03-31", err)

    def test_same_days_differently_spelled_is_not_an_overlap(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.run_link(_min_enrichable("2026-01-01T00:00:00Z", "2026-01-31T12:00:00Z"))
        self.assertEqual(len(self.installments()), 1)

    def test_adjacent_windows_do_not_overlap(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.run_link(_min_enrichable("2026-02-01", "2026-02-28"))
        self.assertEqual(len(self.installments()), 2)

    def test_overlap_helper(self):
        idx = [{"from": "2026-01-01", "to": "2026-03-31"}]
        self.assertEqual(series.overlapping(idx, ("2026-03-31", "2026-04-30")), idx)
        self.assertEqual(series.overlapping(idx, ("2026-04-01", "2026-04-30")), [])
        self.assertEqual(series.overlapping(idx, ("2026-01-01T00:00:00Z",
                                                  "2026-03-31T23:00:00Z")), [])


# --------------------------------------------------------------------- item 7
class TestClosesNumberThatIsAPR(unittest.TestCase):
    def _trains(self, prs, issues=()):
        b = _raw_bundle(prs, list(issues))
        b["meta"]["period"] = {"from": "2026-01-01", "to": "2026-01-31"}
        return link.build_trains(b)

    def test_pr_number_neither_anchors_nor_joins(self):
        trains = self._trains([_pr(1, merged=True, state="closed"),
                               _pr(2, merged=True, state="closed", closes=[1])])
        self.assertEqual([(t["id"], t["issues"], t["prs"]) for t in trains],
                         [("train-pr-1", [], [1]), ("train-pr-2", [], [2])])

    def test_real_issue_beside_a_pr_number_still_anchors(self):
        trains = self._trains([_pr(1, state="open"),
                               _pr(2, merged=True, state="closed", closes=[1, 7])],
                              [_issue(7)])
        self.assertEqual([(t["id"], t["issues"]) for t in trains],
                         [("train-issue-7", [7]), ("train-pr-1", [])])


# --------------------------------------------------------------------- item 8
class TestLinkUnreadableBundle(unittest.TestCase):
    def _run(self, content=None):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.json")
            if content is not None:
                with open(p, "w") as fh:
                    fh.write(content)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                link.main([p])
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(sorted(os.listdir(d)), [] if content is None else ["b.json"])
            if content is not None:
                with open(p) as fh:
                    self.assertEqual(fh.read(), content)
            return err.getvalue()

    def test_missing_file(self):
        err = self._run()
        self.assertIn("link.py: error:", err)
        self.assertIn("b.json", err)
        self.assertNotIn("Traceback", err)

    def test_invalid_json(self):
        self.assertIn("not valid JSON", self._run("{nope"))

    def test_non_object(self):
        for doc in ("[]", "3", '"x"', "null"):
            with self.subTest(doc=doc):
                self.assertIn("not a JSON object", self._run(doc))


# --------------------------------------------------------------------- item 9
class TestCodeownersDirectoryRules(unittest.TestCase):
    def _rules(self, *pairs):
        return derive.compile_codeowners(
            [{"pattern": p, "owners": [o]} for p, o in pairs])

    def test_double_star_rule_owns_the_area_like_a_trailing_slash(self):
        for pat in ("docs/**", "/docs/**", "docs/", "/docs/"):
            with self.subTest(pattern=pat):
                self.assertEqual(derive.area_owners(self._rules((pat, "d")), "docs"),
                                 ["d"])
                self.assertEqual(
                    derive.area_owners(self._rules((pat, "d")), "docs/sub"), ["d"])

    def test_single_star_rule_owns_only_direct_files(self):
        self.assertEqual(derive.area_owners(self._rules(("docs/*", "d")), "docs"), [])
        self.assertEqual(derive.codeowners_owners(self._rules(("docs/*", "d")),
                                                  "docs/a.md"), ["d"])

    def test_double_star_does_not_own_a_sibling_prefix(self):
        self.assertEqual(
            derive.area_owners(self._rules(("docs/**", "d")), "docs-old"), [])

    def test_last_match_still_wins(self):
        rules = self._rules(("docs/**", "d"), ("*", "c"))
        self.assertEqual(derive.area_owners(rules, "docs"), ["c"])
        rules = self._rules(("*", "c"), ("docs/**", "d"))
        self.assertEqual(derive.area_owners(rules, "docs"), ["d"])
        rules = self._rules(("docs/**", "d"), ("docs/sub/", "e"))
        self.assertEqual(derive.area_owners(rules, "docs"), ["d"])
        self.assertEqual(derive.area_owners(rules, "docs/sub"), ["e"])

    def test_fold_writes_the_owns_edge_and_validates(self):
        b = _raw_bundle([], [])
        b["code_graph"] = {"provider": "directory", "areas": [
            {"id": "docs", "label": "docs", "paths": ["docs/a.md"], "edges": []}]}
        b["code_owners"] = [{"pattern": "docs/**", "owners": ["@d"]}]
        conn = _store(b)
        owns = [r[0] for r in conn.execute(
            "SELECT src_id FROM edges WHERE edge_type='owns'")]
        self.assertEqual(len(owns), 1)
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c["name"] for c in report.checks if not c["ok"]], [])


if __name__ == "__main__":
    unittest.main()
