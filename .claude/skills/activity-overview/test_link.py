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

    def test_closed_pr_outside_window_is_excluded(self):
        bundle = {
            "meta": {"period": {"from": "2026-05-01", "to": "2026-05-31"},
                     "ref_date": "2026-05-31"},
            "prs": [
                {"number": 1, "merged": True, "state": "closed",
                 "merged_at": "2026-04-15T00:00:00Z",
                 "url": "https://github.com/o/r/pull/1"},
                {"number": 2, "merged": True, "state": "closed",
                 "merged_at": "2026-05-15T00:00:00Z",
                 "url": "https://github.com/o/r/pull/2"},
            ],
            "issues": [], "milestones": [], "trains": [],
        }
        buckets = link.compute_buckets(bundle)
        shipped = {(r["type"], r["id"]) for r in buckets["shipped"]}
        self.assertIn(("pr", 2), shipped)       # in window -> shipped
        self.assertNotIn(("pr", 1), shipped)    # before window -> excluded
        # and #1 lands in no bucket at all
        all_refs = [(r["type"], r["id"]) for k in buckets for r in buckets[k]]
        self.assertNotIn(("pr", 1), all_refs)


class TestBuildArtifacts(unittest.TestCase):
    def _events(self):
        return [
            {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
             "change": "add", "path": "examples/basic/main.bicep"},
            {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
             "change": "add", "path": "docs/firewall.md"},
            {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
             "change": "modify", "path": "README.md"},
            {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
             "change": "modify", "path": "examples/basic/main.bicep"},
            {"commit": "c3"*20, "author": "Carol", "date": "2026-05-18",
             "change": "rename", "old_path": "examples/basic/main.bicep",
             "path": "examples/advanced/main.bicep"},
            {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
             "change": "delete", "path": "docs/firewall.md"},
            {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
             "change": "modify", "path": "src/app.py"},
        ]

    def _bundle(self):
        return {"meta": {"owner": "o", "repo": "r"}, "code_events": self._events(),
                "commits": [], "prs": [], "issues": []}

    def test_unrecognized_paths_are_ignored(self):
        arts = link.build_artifacts(self._bundle())
        paths = {a["path"] for a in arts.values()}
        self.assertNotIn("src/app.py", paths)  # not a tracked artifact kind

    def test_add_then_change_builds_ordered_lifecycle(self):
        arts = link.build_artifacts(self._bundle())
        readme = next(a for a in arts.values() if a["path"] == "README.md")
        self.assertEqual(readme["kind"], "readme")
        self.assertEqual([e["event"] for e in readme["lifecycle"]], ["change"])
        self.assertEqual(readme["status"], "live")
        self.assertIsNone(readme["code_area"])  # graphify deferred to Phase 3b

    def test_delete_sets_status_removed(self):
        arts = link.build_artifacts(self._bundle())
        doc = next(a for a in arts.values() if a["path"] == "docs/firewall.md")
        self.assertEqual([e["event"] for e in doc["lifecycle"]],
                         ["add", "remove"])
        self.assertEqual(doc["status"], "removed")

    def test_rename_links_replaced_and_replaced_by(self):
        arts = link.build_artifacts(self._bundle())
        old_id = link.artifact_id("examples/basic/main.bicep")
        new_id = link.artifact_id("examples/advanced/main.bicep")
        self.assertEqual(arts[old_id]["status"], "replaced")
        self.assertEqual(arts[old_id]["replaced_by"], new_id)
        # the new artifact records an `add` event from the rename commit
        self.assertEqual(arts[new_id]["lifecycle"][0]["event"], "add")
        self.assertEqual(arts[new_id]["status"], "live")

    def test_lifecycle_refs_are_well_formed_commit_refs(self):
        arts = link.build_artifacts(self._bundle())
        for a in arts.values():
            for ev in a["lifecycle"]:
                self.assertEqual(ev["ref"]["type"], "commit")
                self.assertEqual(len(ev["ref"]["id"]), 40)
                self.assertTrue(ev["ref"]["url"].startswith("https://"))

    def test_empty_code_events_yields_empty_map(self):
        self.assertEqual(link.build_artifacts({"code_events": []}), {})


class TestBuildTimeline(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "docs/firewall.md"},
            ],
            "commits": [], "prs": [
                {"number": 42, "url": "https://github.com/o/r/pull/42",
                 "review_comments": [
                     {"id": 7001, "author": "bob", "body": "x",
                      "url": "https://github.com/o/r/pull/42#discussion_r7001"}],
                 "comments_list": [
                     {"id": 8001, "author": "carol", "body": "y",
                      "url": "https://github.com/o/r/pull/42#issuecomment-8001"}]},
            ],
            "issues": [
                {"number": 18, "url": "https://github.com/o/r/issues/18",
                 "comments_list": [
                     {"id": 9001, "author": "dave", "body": "z",
                      "url": "https://github.com/o/r/issues/18#issuecomment-9001"}],
                 "reactions": {"+1": 9, "total": 12}, "open_high_activity": True},
            ],
        }

    def test_timeline_merges_social_and_code_layers(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        tl = link.build_timeline(b)
        layers = {e["layer"] for e in tl}
        self.assertEqual(layers, {"social", "code"})

    def test_every_event_has_required_shape(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        for e in link.build_timeline(b):
            self.assertIn(e["layer"], {"social", "code"})
            self.assertTrue(e["ts"])
            self.assertIn("actor", e)
            self.assertIn("event", e)
            self.assertIn("type", e["ref"])
            self.assertTrue(str(e["ref"]["url"]).startswith("https://"))
            self.assertIn("kind", e["subject"])

    def test_timeline_sorted_by_ts(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        tl = link.build_timeline(b)
        self.assertEqual([e["ts"] for e in tl], sorted(e["ts"] for e in tl))

    def test_code_event_subject_carries_path_and_kind(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        code = [e for e in link.build_timeline(b) if e["layer"] == "code"][0]
        self.assertEqual(code["subject"]["path"], "docs/firewall.md")
        self.assertEqual(code["subject"]["kind"], "doc")

    def test_empty_bundle_yields_empty_timeline(self):
        self.assertEqual(link.build_timeline(
            {"prs": [], "issues": [], "artifacts": {}}), [])


if __name__ == "__main__":
    unittest.main()
