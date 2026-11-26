"""Issue #93: decision-train membership.

A train holds EVERY mainline-targeting PR of its anchor (not only the PRs of
the winning outcome class), every issue those PRs close, and is anchored on an
issue only through a closing reference (a mention / cross-reference does not
make an issue a train root). Stacked-PR parents are matched by head repo AND
head branch, so two forks' `patch-1` branches cannot be confused."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import digest  # noqa: E402
import gather  # noqa: E402
import link  # noqa: E402
import render  # noqa: E402

PERIOD = {"from": "2026-05-01", "to": "2026-05-31"}


def _pr(number, state="closed", merged=False, closes=(), crossref=(),
        author=None, base="main", head=None, head_repo=None, title=None,
        repo="o/r"):
    pr = {"number": number, "title": title or f"pr {number}", "state": state,
          "merged": merged, "base": base, "head": head or f"b{number}",
          "labels": [], "milestone": None,
          "author": author or f"author{number}",
          "created_at": "2026-05-02T00:00:00Z",
          "updated_at": "2026-05-10T00:00:00Z",
          "merged_at": "2026-05-10T00:00:00Z" if merged else None,
          "closed_at": "2026-05-10T00:00:00Z" if state == "closed" else None,
          "closes": list(closes), "crossref_issues": list(crossref),
          "url": f"https://github.com/{repo}/pull/{number}"}
    if head_repo is not None:
        pr["head_repo"] = head_repo
    return pr


def _issue(number, state="closed", state_reason="completed", kind="feature",
           author=None):
    return {"number": number, "title": f"Issue {number}", "kind": kind,
            "state": state, "state_reason": state_reason, "labels": [],
            "milestone": None, "author": author or f"reporter{number}",
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-10T00:00:00Z",
            "closed_at": "2026-05-10T00:00:00Z" if state == "closed" else None,
            "url": f"https://github.com/o/r/issues/{number}"}


def _bundle(prs, issues, commits=()):
    return {"meta": {"base_branch": "main", "period": dict(PERIOD),
                     "ref_date": "2026-05-31"},
            "milestones": [], "commits": list(commits),
            "prs": prs, "issues": issues}


def _trains(bundle):
    link.attach_commit_prs(bundle["commits"])
    return link.build_trains(bundle)


def _bucket_trains(bundle):
    """{(type, id): (bucket, train)} after trains + buckets."""
    bundle["trains"] = _trains(bundle)
    bundle["buckets"] = link.compute_buckets(bundle)
    out = {}
    for bucket, refs in bundle["buckets"].items():
        for r in refs:
            out[(r["type"], r["id"])] = (bucket, r.get("train"))
    return out


class TestAllAttemptsInOneTrain(unittest.TestCase):
    """Problem 1: issue #7 with merged PR#1, open follow-up PR#2 and
    closed-unmerged PR#3 used to give ('train-issue-7', 'shipped', [1]) and
    leave PR#2 / PR#3 in no train."""

    def _repro(self):
        return _bundle(
            [_pr(1, "closed", True, closes=[7]),
             _pr(2, "open", False, closes=[7]),
             _pr(3, "closed", False, closes=[7])],
            [_issue(7)],
            commits=[{"sha": "c1", "message": "x (#1)", "pr": None},
                     {"sha": "c3", "message": "y (#3)", "pr": None}])

    def test_one_train_holds_every_attempt(self):
        trains = _trains(self._repro())
        self.assertEqual([t["id"] for t in trains], ["train-issue-7"])
        t = trains[0]
        self.assertEqual(t["outcome"], "shipped")
        self.assertEqual(t["prs"], [1, 2, 3])
        self.assertEqual(t["pr_outcomes"],
                         {"1": "merged", "2": "open", "3": "rejected"})
        self.assertEqual(t["commits"], ["c1", "c3"])
        self.assertEqual([(e["type"], e["id"]) for e in t["evidence"]],
                         [("issue", 7), ("pr", 1), ("pr", 2), ("pr", 3)])

    def test_bucket_refs_of_every_attempt_carry_the_train(self):
        got = _bucket_trains(self._repro())
        self.assertEqual(got[("pr", 1)], ("shipped", "train-issue-7"))
        self.assertEqual(got[("pr", 2)], ("in_flight", "train-issue-7"))
        self.assertEqual(got[("pr", 3)], ("rejected", "train-issue-7"))
        self.assertEqual(got[("issue", 7)], ("shipped", "train-issue-7"))

    def test_effort_and_significance_cover_every_attempt(self):
        b = self._repro()
        b["prs"][2]["reviewers"] = ["rev3"]
        b["trains"] = _trains(b)
        link.score_train_significance(b)
        link.annotate_train_effort(b)
        t = b["trains"][0]
        # authors of PR#1..#3, reporter of #7, reviewer of the rejected PR#3
        self.assertEqual(t["effort"]["participants"], 5)
        self.assertEqual(t["effort"]["reviewers"], 1)
        # footprint = 3 prs + 2 commits; kind feature (3.0); no areas
        self.assertEqual(t["significance"], 15.0)

    def test_in_flight_outranks_rejected_and_keeps_both(self):
        b = _bundle([_pr(3, "closed", False, closes=[7]),
                     _pr(4, "open", False, closes=[7])], [_issue(7, "open", None)])
        t = _trains(b)[0]
        self.assertEqual(t["outcome"], "in_flight")
        self.assertEqual(t["prs"], [3, 4])
        self.assertEqual(t["pr_outcomes"], {"3": "rejected", "4": "open"})

    def test_slice_lists_every_attempt_with_its_outcome(self):
        b = self._repro()
        b["trains"] = _trains(b)
        s = link.slice_train(b, "train-issue-7")
        self.assertEqual([p["number"] for p in s["prs"]], [1, 2, 3])
        self.assertEqual(s["train"]["pr_outcomes"],
                         {"1": "merged", "2": "open", "3": "rejected"})
        self.assertEqual(s["train"]["issues"], [7])

    def test_flowchart_shows_every_attempt_and_its_own_outcome(self):
        b = self._repro()
        b["trains"] = _trains(b)
        t = b["trains"][0]
        mmd = render.emit_train_flowchart(b, t)
        self.assertIn('"pr 1"', mmd)
        self.assertIn('"open: pr 2"', mmd)
        self.assertIn('"rejected: pr 3"', mmd)
        node = {}   # label -> node id
        for ln in mmd.splitlines():
            ln = ln.strip()
            for opener in ('["', '(["'):
                if opener in ln and "-->" not in ln:
                    nid, label = ln.split(opener, 1)
                    node[label.rstrip('"])')] = nid
        out = node["Shipped"]
        into_out = sorted(ln.strip().split(" --> ")[0]
                          for ln in mmd.splitlines()
                          if ln.strip().endswith(f"--> {out}"))
        # only the attempt that decided the outcome leads to "Shipped"
        self.assertEqual(into_out, [node["pr 1"]])
        # every attempt still hangs off the issue
        for label in ("pr 1", "open: pr 2", "rejected: pr 3"):
            self.assertIn(f"    {node['Issue 7']} --> {node[label]}",
                          mmd.splitlines())


class TestEveryClosedIssueIsIndexed(unittest.TestCase):
    """Problem 2: PR#1 "closes #7, closes #8" used to root the train on #7
    only; issue #8 had no train and no evidence ref."""

    def test_extra_closed_issue_belongs_to_the_train(self):
        b = _bundle([_pr(1, "closed", True, closes=[7, 8])],
                    [_issue(7), _issue(8)])
        got = _bucket_trains(b)
        t = b["trains"][0]
        self.assertEqual(t["id"], "train-issue-7")
        self.assertEqual(t["root_issue"], 7)
        self.assertEqual(t["issues"], [7, 8])
        self.assertIn(("issue", 8),
                      [(e["type"], e["id"]) for e in t["evidence"]])
        self.assertEqual(got[("issue", 8)], ("shipped", "train-issue-7"))
        self.assertEqual(link.train_index(b["trains"])[("issue", 8)],
                         "train-issue-7")

    def test_root_issue_is_the_lowest_closed_number(self):
        b = _bundle([_pr(1, "closed", True, closes=[8, 7])],
                    [_issue(7), _issue(8)])
        t = _trains(b)[0]
        self.assertEqual(t["id"], "train-issue-7")
        self.assertEqual(t["issues"], [7, 8])

    def test_prs_sharing_a_closed_issue_form_one_train(self):
        """PR#1 closes #7,#8 and PR#2 closes #8,#9: #8 links them, so all
        three issues and both PRs are one train (an issue is in one train)."""
        b = _bundle([_pr(1, "closed", True, closes=[7, 8]),
                     _pr(2, "open", False, closes=[8, 9])],
                    [_issue(7), _issue(8), _issue(9, "open", None)])
        trains = _trains(b)
        self.assertEqual([t["id"] for t in trains], ["train-issue-7"])
        self.assertEqual(trains[0]["issues"], [7, 8, 9])
        self.assertEqual(trains[0]["prs"], [1, 2])

    def test_effort_counts_every_closed_issue(self):
        b = _bundle([_pr(1, "closed", True, closes=[7, 8], author="a")],
                    [_issue(7, author="r7"), _issue(8, author="r8")])
        b["issues"][1]["created_at"] = "2026-04-20T00:00:00Z"
        b["trains"] = _trains(b)
        link.annotate_train_effort(b)
        e = b["trains"][0]["effort"]
        self.assertEqual(e["participants"], 3)    # a, r7, r8
        self.assertEqual(e["opened_at"], "2026-04-20T00:00:00Z")

    def test_kind_falls_back_to_another_typed_closed_issue(self):
        b = _bundle([_pr(1, "closed", True, closes=[7, 8])],
                    [_issue(7, kind="other"), _issue(8, kind="bug")])
        self.assertEqual(_trains(b)[0]["kind"], "bug")

    def test_not_planned_issue_closed_by_a_pr_is_not_a_second_train(self):
        b = _bundle([_pr(1, "closed", False, closes=[7, 8])],
                    [_issue(7), _issue(8, state_reason="not_planned")])
        trains = _trains(b)
        self.assertEqual([t["id"] for t in trains], ["train-issue-7"])
        self.assertEqual(trains[0]["outcome"], "rejected")
        self.assertEqual(trains[0]["issues"], [7, 8])

    def test_abandoned_train_lists_its_issue(self):
        b = _bundle([], [_issue(70, state_reason="not_planned")])
        t = _trains(b)[0]
        self.assertEqual((t["id"], t["outcome"], t["issues"], t["pr_outcomes"]),
                         ("train-issue-70", "abandoned", [70], {}))


class TestMentionDoesNotAnchor(unittest.TestCase):
    """Problem 3: a tracking issue that merely mentions three unrelated merged
    PRs used to yield one `train-issue-50` holding all three."""

    def test_tracking_issue_does_not_swallow_unrelated_prs(self):
        b = _bundle([_pr(1, "closed", True, crossref=[50], title="feat: a"),
                     _pr(2, "closed", True, crossref=[50], title="fix: b"),
                     _pr(3, "closed", True, crossref=[50], title="docs: c")],
                    [_issue(50, "open", None)])
        got = _bucket_trains(b)
        self.assertEqual([(t["id"], t["kind"], t["prs"]) for t in b["trains"]],
                         [("train-pr-1", "feature", [1]),
                          ("train-pr-2", "bug", [2]),
                          ("train-pr-3", "docs", [3])])
        self.assertTrue(all(t["root_issue"] is None and t["issues"] == []
                            for t in b["trains"]))
        self.assertEqual(got[("issue", 50)], ("in_flight", None))

    def test_mention_alongside_a_closing_ref_is_not_a_member(self):
        b = _bundle([_pr(1, "closed", True, closes=[7], crossref=[50])],
                    [_issue(7), _issue(50, "open", None)])
        t = _trains(b)[0]
        self.assertEqual((t["id"], t["issues"]), ("train-issue-7", [7]))
        self.assertNotIn(("issue", 50), link.train_index([t]))


class TestStackedParentByHeadRepo(unittest.TestCase):
    """Problem 4: `head_to_parent` was keyed by the bare head branch name, so
    two PRs with head `patch-1` (one from a fork) made a stacked PR attach to
    whichever came last."""

    def _bundle(self, order):
        own = _pr(10, "open", False, closes=[1], head="patch-1",
                  head_repo="o/r")
        fork = _pr(20, "open", False, closes=[2], head="patch-1",
                   head_repo="someone/r")
        stacked = _pr(30, "closed", True, base="patch-1", head="patch-1-sub",
                      head_repo="o/r")
        prs = [own, fork] if order == "own-first" else [fork, own]
        return _bundle(prs + [stacked],
                       [_issue(1, "open", None), _issue(2, "open", None)])

    def test_stacked_pr_attaches_to_the_parent_in_the_base_repo(self):
        for order in ("own-first", "fork-first"):
            with self.subTest(order=order):
                trains = {t["id"]: t for t in _trains(self._bundle(order))}
                self.assertEqual(trains["train-issue-1"]["contributing_prs"], [30])
                self.assertEqual(trains["train-issue-2"]["contributing_prs"], [])

    def test_head_repo_match_is_case_insensitive(self):
        b = self._bundle("fork-first")
        b["prs"][1]["head_repo"] = "O/R"
        trains = {t["id"]: t for t in _trains(b)}
        self.assertEqual(trains["train-issue-1"]["contributing_prs"], [30])

    def test_two_parents_of_unknown_head_repo_are_ambiguous(self):
        """Older stores carry no head_repo: with two candidate parents the
        stacked PR is attached to neither rather than to a guess."""
        b = self._bundle("own-first")
        for p in b["prs"]:
            p.pop("head_repo", None)
        trains = _trains(b)
        self.assertEqual([t["contributing_prs"] for t in trains], [[], []])

    def test_single_parent_of_unknown_head_repo_still_attaches(self):
        b = self._bundle("own-first")
        b["prs"] = [b["prs"][0], b["prs"][2]]
        for p in b["prs"]:
            p.pop("head_repo", None)
        trains = {t["id"]: t for t in _trains(b)}
        self.assertEqual(trains["train-issue-1"]["contributing_prs"], [30])

    def test_gather_carries_the_head_repo(self):
        raw = {"number": 5, "base": {"ref": "main", "repo": {"full_name": "o/r"}},
               "head": {"ref": "patch-1", "repo": {"full_name": "someone/r"}}}
        self.assertEqual(gather.normalize_pr(raw)["head_repo"], "someone/r")
        # a deleted fork: GitHub sends head.repo = null
        raw["head"]["repo"] = None
        self.assertIsNone(gather.normalize_pr(raw)["head_repo"])


class TestRejectedAttemptDoesNotJoinIssues(unittest.TestCase):
    """#97 final gate (validation finding 13): merged PR #7264 closes #3813
    only; the rejected PR #5163 listed #3813 and #2422. A closing reference
    takes effect only when its PR merges, so #2422 (still open) is not pulled
    into the shipped train, and the train is neither rooted on it nor dated
    from it."""

    def _repro(self, order=None):
        merged = _pr(7264, "closed", True, closes=[3813])
        merged["created_at"] = "2026-08-11T11:23:32Z"
        merged["merged_at"] = "2026-08-11T14:32:52Z"
        rejected = _pr(5163, "closed", False, closes=[3813, 2422])
        rejected["created_at"] = "2025-05-07T21:45:26Z"
        rejected["reviewers"] = ["rev"]
        old_open = _issue(2422, "open", None, kind="bug")
        old_open["created_at"] = "2023-11-21T15:24:32Z"
        closed = _issue(3813, kind="feature")
        closed["created_at"] = "2024-11-20T10:42:17Z"
        prs, issues = [merged, rejected], [old_open, closed]
        if order == "reversed":
            prs, issues = prs[::-1], issues[::-1]
        return _bundle(prs, issues)

    def test_train_is_rooted_on_the_issue_the_merged_pr_closes(self):
        for order in (None, "reversed"):
            with self.subTest(order=order):
                trains = _trains(self._repro(order))
                self.assertEqual([t["id"] for t in trains], ["train-issue-3813"])
                t = trains[0]
                self.assertEqual(t["issues"], [3813])
                self.assertEqual(t["prs"], [5163, 7264])
                self.assertEqual(t["pr_outcomes"],
                                 {"5163": "rejected", "7264": "merged"})
                self.assertEqual(t["outcome"], "shipped")
                self.assertEqual(t["kind"], "feature")

    def test_open_issue_is_in_no_train_and_its_refs_carry_none(self):
        got = _bucket_trains(self._repro())
        self.assertEqual(got[("pr", 5163)], ("rejected", "train-issue-3813"))
        idx = link.train_index(_trains(self._repro()))
        self.assertEqual(idx[("pr", 7264)], "train-issue-3813")
        self.assertNotIn(("issue", 2422), idx)

    def test_effort_is_dated_from_the_train_not_the_attempt(self):
        b = self._repro()
        b["trains"] = _trains(b)
        link.annotate_train_effort(b)
        e = b["trains"][0]["effort"]
        self.assertEqual(e["opened_at"], "2024-11-20T10:42:17Z")
        self.assertEqual(e["elapsed_days"], 629)
        # the attempt's review work still counts (issue #93)
        self.assertEqual(e["reviewers"], 1)

    def test_rejected_pr_alone_still_forms_its_own_train(self):
        b = _bundle([_pr(5, "closed", False, closes=[2, 3]),
                     _pr(6, "closed", False, closes=[3])],
                    [_issue(2, "open", None), _issue(3, "open", None)])
        trains = _trains(b)
        self.assertEqual([(t["id"], t["outcome"], t["prs"], t["issues"])
                          for t in trains],
                         [("train-issue-2", "rejected", [5, 6], [2, 3])])

    def test_attempt_touching_two_trains_joins_the_lowest_issue(self):
        b = _bundle([_pr(1, "closed", True, closes=[20]),
                     _pr(2, "open", False, closes=[10]),
                     _pr(3, "closed", False, closes=[20, 10, 30])],
                    [_issue(10, "open", None), _issue(20), _issue(30, "open", None)])
        for prs in (b["prs"], b["prs"][::-1]):
            with self.subTest(order=[p["number"] for p in prs]):
                b2 = dict(b, prs=list(prs))
                got = {t["id"]: (t["prs"], t["issues"]) for t in _trains(b2)}
                self.assertEqual(got, {"train-issue-10": ([2, 3], [10]),
                                       "train-issue-20": ([1], [20])})

    def test_attempt_with_a_not_planned_issue_leaves_it_abandoned(self):
        b = _bundle([_pr(1, "closed", True, closes=[7]),
                     _pr(2, "closed", False, closes=[7, 9])],
                    [_issue(7), _issue(9, state_reason="not_planned")])
        got = {t["id"]: (t["outcome"], t["prs"], t["issues"]) for t in _trains(b)}
        self.assertEqual(got, {"train-issue-7": ("shipped", [1, 2], [7]),
                               "train-issue-9": ("abandoned", [], [9])})


class TestProjectTrainsFollow(unittest.TestCase):
    def test_project_train_lists_every_member_issue(self):
        members = [{"repo": "Azure/a", "bundle": {"trains": [
            {"id": "train-issue-7", "kind": "feature", "root_issue": 7,
             "issues": [7, 8], "prs": [1, 2], "commits": [],
             "outcome": "shipped", "evidence": []}]}}]
        for comps in ([frozenset({"proj/Azure/a#issue-7", "proj/Azure/a#issue-8",
                                  "proj/Azure/a#pr-1", "proj/Azure/a#pr-2"})],
                      []):   # [] : anchor in no component -> solo project train
            with self.subTest(components=len(comps)):
                trains = digest.build_project_trains(members, comps, "proj")
                self.assertEqual(len(trains), 1)
                self.assertEqual(trains[0]["issues"],
                                 ["proj/Azure/a#issue-7", "proj/Azure/a#issue-8"])
                self.assertEqual(trains[0]["id"], "ptrain-proj/Azure/a#issue-7")


if __name__ == "__main__":
    unittest.main()
