import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestCommitPrResolution(unittest.TestCase):
    def test_resolve_commit_pr_from_squash_subject(self):
        self.assertEqual(link.resolve_commit_pr("Add policy param (#42)"), 42)

    def test_resolve_commit_pr_from_merge_subject(self):
        self.assertEqual(
            link.resolve_commit_pr("Merge pull request #42 from feature/policy"), 42
        )

    def test_resolve_commit_pr_none_when_absent(self):
        self.assertIsNone(link.resolve_commit_pr("Tidy outputs"))

    def test_attach_commit_prs_sets_pr_field(self):
        commits = [
            {"sha": "a", "message": "Add policy param (#42)", "pr": None},
            {"sha": "b", "message": "Tidy outputs", "pr": None},
        ]
        link.attach_commit_prs(commits)
        self.assertEqual(commits[0]["pr"], 42)
        self.assertIsNone(commits[1]["pr"])


def _sample_bundle():
    return {
        "commits": [
            {"sha": "a", "message": "Add policy param (#42)", "pr": None},
            {"sha": "b", "message": "Merge pull request #42 from x", "pr": None},
            {"sha": "c", "message": "Tidy outputs", "pr": None},
        ],
        "prs": [
            {"number": 42, "title": "Add policy param", "merged": True,
             "closes": [17], "url": "https://github.com/o/r/pull/42"},
        ],
        "issues": [
            {"number": 17, "title": "Support policy param", "kind": "feature",
             "state": "closed", "state_reason": "completed",
             "url": "https://github.com/o/r/issues/17"},
        ],
    }


class TestBuildTrains(unittest.TestCase):
    def test_train_id_uses_root_issue(self):
        bundle = _sample_bundle()
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(len(trains), 1)
        t = trains[0]
        self.assertEqual(t["id"], "train-issue-17")
        self.assertEqual(t["root_issue"], 17)
        self.assertEqual(t["prs"], [42])
        self.assertEqual(sorted(t["commits"]), ["a", "b"])
        self.assertEqual(t["outcome"], "shipped")
        self.assertEqual(t["kind"], "feature")

    def test_train_id_falls_back_to_pr_when_issueless(self):
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["id"], "train-pr-42")
        self.assertIsNone(trains[0]["root_issue"])

    def test_train_anchors_on_crossref_when_no_closing_keyword(self):
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []
        bundle["prs"][0]["crossref_issues"] = [17]
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["id"], "train-issue-17")
        self.assertEqual(trains[0]["root_issue"], 17)

    def test_train_evidence_refs_are_well_formed(self):
        bundle = _sample_bundle()
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        for ev in trains[0]["evidence"]:
            self.assertIn("type", ev)
            self.assertIn("id", ev)
            self.assertTrue(ev["url"].startswith("https://"))


class TestBucketsAndEnrich(unittest.TestCase):
    def test_shipped_bucket_has_merged_prs_and_completed_issues(self):
        bundle = _sample_bundle()
        bundle.setdefault("buckets", {"shipped": [], "in_flight": [],
                                      "rejected": [], "next_candidates": []})
        link.attach_commit_prs(bundle["commits"])
        buckets = link.compute_buckets(bundle)
        kinds = {(r["type"], r["id"]) for r in buckets["shipped"]}
        self.assertIn(("pr", 42), kinds)
        self.assertIn(("issue", 17), kinds)

    def test_enrich_is_idempotent_and_populates_both(self):
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = json.load(fh)
        once = link.enrich(bundle)
        self.assertEqual(once["trains"][0]["id"], "train-issue-17")
        self.assertTrue(once["buckets"]["shipped"])
        # running again yields the same trains (deterministic, no duplication)
        twice = link.enrich(once)
        self.assertEqual(
            [t["id"] for t in once["trains"]],
            [t["id"] for t in twice["trains"]],
        )
        self.assertEqual(len(once["trains"]), len(twice["trains"]))


def _well_formed(r):
    return (
        isinstance(r, dict)
        and isinstance(r.get("type"), str)
        and r.get("id") is not None
        and isinstance(r.get("url"), str)
        and r["url"].startswith("https://")
    )


class TestProvenanceAndEndToEnd(unittest.TestCase):
    def test_every_train_and_bucket_ref_is_well_formed(self):
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = link.enrich(json.load(fh))
        for t in bundle["trains"]:
            self.assertTrue(t["evidence"], "train must carry evidence")
            for ev in t["evidence"]:
                self.assertTrue(_well_formed(ev), f"bad ref {ev}")
        for r in bundle["buckets"]["shipped"]:
            self.assertTrue(_well_formed(r), f"bad ref {r}")

    def test_gather_assembly_into_link_offline(self):
        # Build a bundle purely from fixtures — no git, no network.
        with open(os.path.join(FIX, "git_log_sample.txt")) as fh:
            commits = gather.parse_git_log(fh.read())
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            data = json.load(fh)
        prs = gather.select_merged_prs(
            [gather.normalize_pr(p) for p in data["pulls"]],
            "2026-05-01", "2026-05-31",
        )
        issues = [gather.normalize_issue(data["issues"][str(n)])
                  for p in prs for n in p["closes"] if str(n) in data["issues"]]
        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, commits, prs, issues)

        link.enrich(bundle)

        self.assertEqual([p["number"] for p in bundle["prs"]], [42])
        self.assertEqual(bundle["trains"][0]["id"], "train-issue-17")
        shipped = {(r["type"], r["id"]) for r in bundle["buckets"]["shipped"]}
        self.assertEqual(shipped, {("pr", 42), ("issue", 17)})


class TestSelectMilestonesAndBuckets(unittest.TestCase):
    def _p2_bundle(self):
        with open(os.path.join(FIX, "bundle_p2.json")) as fh:
            return link.enrich(json.load(fh))

    def test_select_milestones_current_and_next(self):
        ms = [
            {"title": "v1.1.0", "state": "closed", "due_on": "2026-04-30T00:00:00Z", "number": 3},
            {"title": "v1.2.0", "state": "open", "due_on": "2026-05-31T00:00:00Z", "number": 4},
            {"title": "v1.3.0", "state": "open", "due_on": "2026-06-30T00:00:00Z", "number": 5},
        ]
        current, nxt = link.select_milestones(ms, "2026-05-20")
        self.assertEqual(current["title"], "v1.2.0")
        self.assertEqual(nxt["title"], "v1.3.0")

    def test_buckets_classify_each_item_once(self):
        b = self._p2_bundle()["buckets"]
        def nums(key):
            return {(r["type"], r["id"]) for r in b[key]}
        self.assertIn(("pr", 42), nums("shipped"))
        self.assertIn(("issue", 17), nums("shipped"))
        self.assertIn(("pr", 43), nums("rejected"))
        self.assertIn(("issue", 20), nums("rejected"))
        # open #44 + #18 are on the NEXT milestone (v1.3.0) -> next_candidates
        self.assertIn(("pr", 44), nums("next_candidates"))
        self.assertIn(("issue", 18), nums("next_candidates"))
        # open #21 is on the CURRENT milestone (v1.2.0) -> in_flight
        self.assertIn(("issue", 21), nums("in_flight"))
        # no item appears in two buckets
        all_refs = [(r["type"], r["id"]) for k in b for r in b[k]]
        self.assertEqual(len(all_refs), len(set(all_refs)))

    def test_bucket_refs_carry_train_id_when_known(self):
        b = self._p2_bundle()["buckets"]
        pr42 = next(r for r in b["shipped"] if (r["type"], r["id"]) == ("pr", 42))
        self.assertEqual(pr42["train"], "train-issue-17")


if __name__ == "__main__":
    unittest.main()
