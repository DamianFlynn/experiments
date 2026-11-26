"""Issue #112: report facts that mean what the report says.

* `trains[].effort` (item 1): `stalled` is true only for an OPEN train (outcome
  `in_flight`) with no recorded activity for more than `TRAIN_STALL_DAYS` days
  at the view's reference date (`meta.ref_date`, else `meta.to`), with the
  measured `idle_days` and `last_activity_at`; a SHIPPED train is never
  `stalled`, and `slow_to_land` marks one whose `elapsed_days` exceeds
  `TRAIN_STALL_DAYS`. Rejected / abandoned trains carry neither flag.
* project trains (item 5): `code_areas` is the member trains' areas,
  repo-qualified as `"<repo>::<area>"`, sorted.
* train slice (item 8): every commit row carries a citable `url`.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import link  # noqa: E402

N = link.TRAIN_STALL_DAYS


def _pr(number, state="open", merged=False, created="2026-01-01T00:00:00Z",
        updated=None, merged_at=None, closed_at=None, **extra):
    pr = {"number": number, "title": "PR {}".format(number), "author": "a",
          "url": "https://github.com/o/r/pull/{}".format(number),
          "state": state, "merged": merged, "merged_at": merged_at,
          "closed_at": closed_at, "created_at": created, "updated_at": updated,
          "reviewers": [], "comments_list": []}
    pr.update(extra)
    return pr


def _issue(number, created="2026-01-01T00:00:00Z", updated=None, **extra):
    iss = {"number": number, "title": "Issue {}".format(number), "author": "b",
           "url": "https://github.com/o/r/issues/{}".format(number),
           "created_at": created, "updated_at": updated, "comments_list": []}
    iss.update(extra)
    return iss


def _effort(outcome, prs, issues=(), commits=(), meta=None, train_commits=None):
    train = {"id": "t", "outcome": outcome, "prs": [p["number"] for p in prs],
             "issues": [i["number"] for i in issues],
             "commits": train_commits if train_commits is not None
             else [c["sha"] for c in commits]}
    bundle = {"meta": meta if meta is not None
              else {"from": "2026-05-01", "to": "2026-05-31",
                    "ref_date": "2026-05-31"},
              "trains": [train], "prs": list(prs), "issues": list(issues),
              "commits": list(commits)}
    link.annotate_train_effort(bundle)
    return train["effort"]


class TestOpenTrainStall(unittest.TestCase):
    def test_open_train_idle_beyond_threshold_is_stalled(self):
        eff = _effort("in_flight", [_pr(1, updated="2026-04-01T10:00:00Z")])
        self.assertEqual(eff["last_activity_at"], "2026-04-01T10:00:00Z")
        self.assertEqual(eff["idle_days"], 60)
        self.assertTrue(eff["stalled"])
        self.assertFalse(eff["slow_to_land"])
        self.assertIsNone(eff["merged_at"])
        self.assertIsNone(eff["elapsed_days"])

    def test_threshold_is_strict(self):
        # exactly N idle days is not stalled; N + 1 is.
        at = "2026-05-{:02d}T23:00:00Z".format(31 - N)
        eff = _effort("in_flight", [_pr(1, updated=at)])
        self.assertEqual(eff["idle_days"], N)
        self.assertFalse(eff["stalled"])
        at = "2026-05-{:02d}T00:00:00Z".format(31 - N - 1)
        eff = _effort("in_flight", [_pr(1, updated=at)])
        self.assertEqual(eff["idle_days"], N + 1)
        self.assertTrue(eff["stalled"])

    def test_recent_activity_on_any_member_counts(self):
        old = "2026-02-01T00:00:00Z"
        cases = {
            "issue updated_at": dict(issues=[_issue(9, updated="2026-05-20T00:00:00Z")]),
            "pr comment": dict(prs=[_pr(1, updated=old, comments_list=[
                {"author": "x", "created_at": "2026-05-20T00:00:00Z"}])]),
            "review comment": dict(prs=[_pr(1, updated=old, review_comments=[
                {"author": "x", "created_at": "2026-05-20T00:00:00Z"}])]),
            "review": dict(prs=[_pr(1, updated=old, reviews=[
                {"author": "x", "state": "commented",
                 "submitted_at": "2026-05-20T00:00:00Z"}])]),
            "lifecycle event": dict(prs=[_pr(1, updated=old, lifecycle=[
                {"event": "labeled", "created_at": "2026-05-20T00:00:00Z"}])]),
            "issue comment": dict(issues=[_issue(9, updated=old, comments_list=[
                {"author": "x", "created_at": "2026-05-20T00:00:00Z"}])]),
            "train commit": dict(commits=[{"sha": "c" * 40, "date": "2026-05-20",
                                           "committed_at": "2026-05-20T09:00:00Z"}]),
            "rejected attempt closed": dict(prs=[_pr(
                2, state="closed", updated=old, closed_at="2026-05-20T00:00:00Z")]),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                prs = kw.get("prs") or []
                if not any(p["number"] == 1 for p in prs):
                    prs = [_pr(1, updated=old)] + prs
                eff = _effort("in_flight", prs, kw.get("issues", ()),
                              kw.get("commits", ()))
                self.assertEqual(eff["idle_days"], 11, name)
                self.assertFalse(eff["stalled"], name)

    def test_commit_date_used_when_no_committed_at(self):
        eff = _effort("in_flight", [_pr(1, updated="2026-01-01T00:00:00Z")],
                      commits=[{"sha": "d" * 40, "date": "2026-05-30"}])
        self.assertEqual(eff["idle_days"], 1)

    def test_commit_outside_the_train_does_not_count(self):
        eff = _effort("in_flight", [_pr(1, updated="2026-01-01T00:00:00Z")],
                      commits=[{"sha": "d" * 40, "date": "2026-05-30"}],
                      train_commits=[])
        self.assertTrue(eff["stalled"])

    def test_reference_date_falls_back_to_to(self):
        eff = _effort("in_flight", [_pr(1, updated="2026-05-01T00:00:00Z")],
                      meta={"from": "2026-05-01", "to": "2026-05-10"})
        self.assertEqual(eff["idle_days"], 9)

    def test_ref_date_wins_over_to(self):
        eff = _effort("in_flight", [_pr(1, updated="2026-05-01T00:00:00Z")],
                      meta={"from": "2026-05-01", "to": "2026-05-31",
                            "ref_date": "2026-06-30"})
        self.assertEqual(eff["idle_days"], 60)
        self.assertTrue(eff["stalled"])

    def test_activity_after_the_reference_date_is_not_idle(self):
        # a view of an earlier window (meta.state_as_of): records are newer.
        eff = _effort("in_flight", [_pr(1, updated="2026-06-15T00:00:00Z")])
        self.assertEqual(eff["idle_days"], 0)
        self.assertFalse(eff["stalled"])

    def test_no_timestamps_or_no_reference_date_is_not_stalled(self):
        eff = _effort("in_flight", [_pr(1, created=None)])
        self.assertIsNone(eff["last_activity_at"])
        self.assertIsNone(eff["idle_days"])
        self.assertFalse(eff["stalled"])
        eff = _effort("in_flight", [_pr(1, updated="2026-01-01T00:00:00Z")],
                      meta={})
        self.assertEqual(eff["last_activity_at"], "2026-01-01T00:00:00Z")
        self.assertIsNone(eff["idle_days"])
        self.assertFalse(eff["stalled"])


class TestShippedSlowToLand(unittest.TestCase):
    def _shipped(self, opened, merged, updated=None):
        return _effort("shipped", [_pr(1, state="closed", merged=True,
                                       created=opened, merged_at=merged,
                                       updated=updated or merged)])

    def test_shipped_after_72_days_is_slow_not_stalled(self):
        eff = self._shipped("2026-03-01T00:00:00Z", "2026-05-12T00:00:00Z")
        self.assertEqual(eff["elapsed_days"], 72)
        self.assertTrue(eff["slow_to_land"])
        self.assertFalse(eff["stalled"])
        self.assertIsNone(eff["idle_days"])
        self.assertIsNone(eff["last_activity_at"])
        self.assertEqual(eff["opened_at"], "2026-03-01T00:00:00Z")
        self.assertEqual(eff["merged_at"], "2026-05-12T00:00:00Z")

    def test_slow_to_land_threshold_is_strict(self):
        eff = self._shipped("2026-05-01T00:00:00Z", "2026-05-22T00:00:00Z")
        self.assertEqual(eff["elapsed_days"], N)
        self.assertFalse(eff["slow_to_land"])
        eff = self._shipped("2026-05-01T00:00:00Z", "2026-05-23T00:00:00Z")
        self.assertTrue(eff["slow_to_land"])

    def test_shipped_train_with_an_idle_open_follow_up_is_not_stalled(self):
        eff = _effort("shipped", [
            _pr(1, state="closed", merged=True, created="2026-05-01T00:00:00Z",
                merged_at="2026-05-02T00:00:00Z"),
            _pr(2, created="2026-01-01T00:00:00Z", updated="2026-01-02T00:00:00Z")])
        self.assertFalse(eff["stalled"])
        self.assertIsNone(eff["idle_days"])


class TestRejectedAndAbandoned(unittest.TestCase):
    def test_neither_flag(self):
        old = "2025-01-01T00:00:00Z"
        for outcome, prs, issues in (
                ("rejected", [_pr(1, state="closed", created=old, updated=old,
                                  closed_at=old)], []),
                ("abandoned", [], [_issue(5, created=old, updated=old)])):
            with self.subTest(outcome):
                eff = _effort(outcome, prs, issues)
                self.assertFalse(eff["stalled"])
                self.assertFalse(eff["slow_to_land"])
                self.assertIsNone(eff["idle_days"])
                self.assertIsNone(eff["last_activity_at"])


class TestOutcomeFallback(unittest.TestCase):
    """A train without `outcome` (a hand-built or older train): merged -> shipped
    rules, an open PR -> open rules."""

    def test_without_outcome(self):
        bundle = {"meta": {"to": "2026-05-31"}, "issues": [], "commits": [],
                  "prs": [_pr(1, updated="2026-01-01T00:00:00Z"),
                          _pr(2, state="closed", merged=True,
                              created="2026-01-01T00:00:00Z",
                              merged_at="2026-03-01T00:00:00Z")],
                  "trains": [{"id": "a", "prs": [1], "commits": []},
                             {"id": "b", "prs": [2], "commits": []}]}
        link.annotate_train_effort(bundle)
        a, b = (t["effort"] for t in bundle["trains"])
        self.assertTrue(a["stalled"])
        self.assertFalse(a["slow_to_land"])
        self.assertTrue(b["slow_to_land"])
        self.assertFalse(b["stalled"])


class TestEffortShape(unittest.TestCase):
    KEYS = {"opened_at", "merged_at", "elapsed_days", "reviewers",
            "review_comments", "commits", "participants", "stalled",
            "idle_days", "last_activity_at", "slow_to_land"}

    def test_every_outcome_has_the_same_keys(self):
        for outcome in ("shipped", "in_flight", "rejected", "abandoned"):
            eff = _effort(outcome, [_pr(1, updated="2026-05-01T00:00:00Z")])
            self.assertEqual(set(eff), self.KEYS, outcome)

    def test_deterministic(self):
        prs = [_pr(1, updated="2026-04-01T00:00:00Z", reviews=[
            {"author": "r", "submitted_at": "2026-04-03T00:00:00Z"}])]
        self.assertEqual(_effort("in_flight", prs), _effort("in_flight", prs))


class TestSliceCommitUrl(unittest.TestCase):
    def _bundle(self, meta):
        sha = "e" * 40
        return {"meta": meta,
                "trains": [{"id": "train-pr-1", "prs": [1], "commits": [sha],
                            "outcome": "shipped"}],
                "prs": [_pr(1)], "issues": [],
                "commits": [{"sha": sha, "message": "m", "author": "a",
                             "date": "2026-05-02"}]}

    def test_commit_row_has_the_commit_url(self):
        s = link.slice_train(self._bundle({"owner": "o", "repo": "r"}), "train-pr-1")
        self.assertEqual(s["commits"][0]["url"],
                         "https://github.com/o/r/commit/" + "e" * 40)

    def test_url_built_like_feature_deltas(self):
        import derive
        for meta in ({"owner": "o", "repo": "r"}, {}):
            b = self._bundle(meta)
            s = link.slice_train(b, "train-pr-1")
            self.assertEqual(s["commits"][0]["url"],
                             derive._commit_url(b, "e" * 40))


class TestProjectTrainCodeAreas(unittest.TestCase):
    def _member(self, repo, trains):
        return {"repo": repo, "bundle": {"trains": trains}}

    def _train(self, n, areas, outcome="shipped"):
        return {"id": "train-pr-{}".format(n), "kind": "feature",
                "root_issue": None, "issues": [], "prs": [n], "commits": [],
                "code_areas": areas, "outcome": outcome, "evidence": []}

    def test_repo_qualified_sorted_union(self):
        members = [self._member("Azure/b", [self._train(2, ["z/area", "a/area"])]),
                   self._member("Azure/a", [self._train(1, ["m/area"])])]
        pid_a = "proj/Azure/a#pr-1"
        pid_b = "proj/Azure/b#pr-2"
        out = digest.build_project_trains(members, [{pid_a, pid_b}], "proj")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code_areas"],
                         ["Azure/a::m/area", "Azure/b::a/area", "Azure/b::z/area"])

    def test_singleton_and_empty(self):
        members = [self._member("Azure/a", [self._train(1, ["x"]),
                                            self._train(3, [])])]
        out = {t["id"]: t for t in digest.build_project_trains(members, [], "proj")}
        self.assertEqual(out["ptrain-proj/Azure/a#pr-1"]["code_areas"], ["Azure/a::x"])
        self.assertEqual(out["ptrain-proj/Azure/a#pr-3"]["code_areas"], [])

    def test_same_area_in_two_trains_is_listed_once(self):
        members = [self._member("Azure/a", [self._train(1, ["x"]),
                                            self._train(2, ["x"])])]
        comp = [{"proj/Azure/a#pr-1", "proj/Azure/a#pr-2"}]
        out = digest.build_project_trains(members, comp, "proj")
        self.assertEqual(out[0]["code_areas"], ["Azure/a::x"])


if __name__ == "__main__":
    unittest.main()
