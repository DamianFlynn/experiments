import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _enrich_via_store(golden_name):
    """Slice 7b-2 end-to-end: enrich no longer DERIVES artifacts/people — fold
    materializes them into the store and extract reads them back. A test that
    needs the enriched artifacts/people (rather than a derivation it calls
    directly) must route the raw fixture through fold -> extract -> enrich."""
    with open(os.path.join(FIX, golden_name)) as fh:
        golden = json.load(fh)
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, json.loads(json.dumps(golden)))
    meta = golden["meta"]
    extracted = extract.extract(
        conn, meta["owner"], meta["repo"], meta["from"], meta["to"])
    return link.enrich(extracted)


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

    def test_kind_falls_back_to_pr_title_when_no_typed_issue(self):
        """A PR-anchored train (no root issue) derives kind from the PR's
        conventional-commit title prefix instead of defaulting to 'other'."""
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []
        bundle["prs"][0]["title"] = "feat: add policy param"
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["id"], "train-pr-42")
        self.assertEqual(trains[0]["kind"], "feature")

    def test_typed_root_issue_kind_wins_over_pr_title(self):
        """A typed root issue takes precedence over the PR title prefix."""
        bundle = _sample_bundle()
        bundle["prs"][0]["title"] = "fix: tweak"   # would be 'bug' on its own
        link.attach_commit_prs(bundle["commits"])  # issue 17 kind == 'feature'
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["root_issue"], 17)
        self.assertEqual(trains[0]["kind"], "feature")

    def test_untyped_root_issue_falls_back_to_pr_title(self):
        """An 'other'-kind root issue still falls back to the PR title prefix."""
        bundle = _sample_bundle()
        bundle["issues"][0]["kind"] = "other"
        bundle["prs"][0]["title"] = "fix: tweak"
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["root_issue"], 17)
        self.assertEqual(trains[0]["kind"], "bug")

    def test_kind_stays_other_when_pr_title_has_no_prefix(self):
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []            # PR-anchored
        link.attach_commit_prs(bundle["commits"])  # title "Add policy param"
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["kind"], "other")

    def test_stacked_pr_to_non_main_base_excluded_from_trains(self):
        """A PR merged into another branch (base != meta.base_branch) is a stacked/
        fork contribution, not shipped-to-main, so it builds no main-line train."""
        bundle = _sample_bundle()
        bundle["meta"] = {"base_branch": "main"}
        bundle["prs"][0]["base"] = "users/x/feature"
        link.attach_commit_prs(bundle["commits"])
        self.assertEqual(link.build_trains(bundle), [])

    def test_pr_merged_to_main_base_included(self):
        bundle = _sample_bundle()
        bundle["meta"] = {"base_branch": "main"}
        bundle["prs"][0]["base"] = "main"
        link.attach_commit_prs(bundle["commits"])
        self.assertEqual(len(link.build_trains(bundle)), 1)

    def test_unknown_base_treated_as_mainline(self):
        """Older bundles without a base/base_branch keep the prior behaviour."""
        bundle = _sample_bundle()              # no meta.base_branch, pr has no base
        link.attach_commit_prs(bundle["commits"])
        self.assertEqual(len(link.build_trains(bundle)), 1)

    def test_inflight_train_from_open_pr_to_main(self):
        """An OPEN PR targeting main (its work not yet shipped) builds an in_flight
        train so in-progress efforts are tracked, not just merged ones."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [],
            "issues": [{"number": 50, "title": "Feature X", "kind": "feature",
                        "url": "https://github.com/o/r/issues/50"}],
            "prs": [{"number": 80, "title": "feat: wip X", "merged": False,
                     "state": "open", "base": "main", "head": "feat-x",
                     "closes": [50], "crossref_issues": [],
                     "url": "https://github.com/o/r/pull/80"}],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-50"])
        self.assertEqual(trains[0]["outcome"], "in_flight")
        self.assertEqual(trains[0]["prs"], [80])
        self.assertEqual(trains[0]["contributing_prs"], [])

    def test_stacked_pr_attaches_as_contributing_to_parent_train(self):
        """A PR merged into another PR's branch (base == that PR's head) is tracked
        as a contributing_pr on the parent's train — the journey-to-main context —
        not as its own train and not as shipped."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [],
            "issues": [{"number": 62, "title": "Bug Y", "kind": "bug",
                        "url": "https://github.com/o/r/issues/62"}],
            "prs": [
                {"number": 116, "title": "fix: Y", "merged": False, "state": "open",
                 "base": "main", "head": "issue62", "closes": [62],
                 "crossref_issues": [], "url": "https://github.com/o/r/pull/116"},
                {"number": 118, "title": "fix: Y part", "merged": True,
                 "state": "closed", "base": "issue62", "head": "issue62-sub",
                 "closes": [], "crossref_issues": [],
                 "url": "https://github.com/o/r/pull/118"},
            ],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-62"])
        self.assertEqual(trains[0]["prs"], [116])
        self.assertEqual(trains[0]["contributing_prs"], [118])

    def test_open_pr_for_already_shipped_anchor_does_not_duplicate(self):
        bundle = _sample_bundle()
        bundle["meta"] = {"base_branch": "main"}
        bundle["prs"][0]["base"] = "main"
        bundle["prs"][0]["head"] = "h1"
        bundle["prs"].append({"number": 99, "title": "feat: more", "merged": False,
                              "state": "open", "base": "main", "head": "h2",
                              "closes": [17], "crossref_issues": [],
                              "url": "https://github.com/o/r/pull/99"})
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        ids = [t["id"] for t in trains]
        self.assertEqual(ids.count("train-issue-17"), 1)
        self.assertEqual(trains[0]["outcome"], "shipped")

    def test_rejected_train_from_closed_unmerged_pr(self):
        """A PR closed WITHOUT merging (a dropped/rejected change) builds a
        `rejected` train so the dead-end is still on record."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [],
            "issues": [{"number": 62, "title": "Bug Y", "kind": "bug",
                        "url": "https://github.com/o/r/issues/62"}],
            "prs": [{"number": 116, "title": "fix: Y", "merged": False,
                     "state": "closed", "base": "main", "head": "issue62",
                     "closes": [62], "crossref_issues": [],
                     "url": "https://github.com/o/r/pull/116"}],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-62"])
        self.assertEqual(trains[0]["outcome"], "rejected")

    def test_stacked_pr_attaches_to_rejected_parent_train(self):
        """Contributions to an abandoned effort attach to the (rejected) parent
        train — the real #7116/#7117/#7118 case where the parent was closed."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [],
            "issues": [{"number": 62, "kind": "bug",
                        "url": "https://github.com/o/r/issues/62"}],
            "prs": [
                {"number": 116, "title": "fix: Y", "merged": False, "state": "closed",
                 "base": "main", "head": "issue62", "closes": [62],
                 "crossref_issues": [], "url": "https://github.com/o/r/pull/116"},
                {"number": 118, "title": "fix: Y part", "merged": True,
                 "state": "closed", "base": "issue62", "head": "issue62-sub",
                 "closes": [], "crossref_issues": [],
                 "url": "https://github.com/o/r/pull/118"},
            ],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-62"])
        self.assertEqual(trains[0]["outcome"], "rejected")
        self.assertEqual(trains[0]["contributing_prs"], [118])

    def test_abandoned_train_from_not_planned_issue(self):
        """An issue closed `not_planned` with no PR is an abandoned train (an
        idea that went nowhere)."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [], "prs": [],
            "issues": [{"number": 70, "title": "Idea", "kind": "feature",
                        "state": "closed", "state_reason": "not_planned",
                        "url": "https://github.com/o/r/issues/70"}],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-70"])
        self.assertEqual(trains[0]["outcome"], "abandoned")
        self.assertEqual(trains[0]["prs"], [])

    def test_shipped_outranks_closed_unmerged_sibling(self):
        """An anchor with both a merged and a closed-unmerged PR stays shipped."""
        bundle = {
            "meta": {"base_branch": "main"}, "commits": [],
            "issues": [{"number": 80, "kind": "feature",
                        "url": "https://github.com/o/r/issues/80"}],
            "prs": [
                {"number": 200, "merged": True, "state": "closed", "base": "main",
                 "head": "a", "closes": [80], "crossref_issues": [],
                 "url": "https://github.com/o/r/pull/200"},
                {"number": 201, "merged": False, "state": "closed", "base": "main",
                 "head": "b", "closes": [80], "crossref_issues": [],
                 "url": "https://github.com/o/r/pull/201"},
            ],
        }
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual([t["id"] for t in trains], ["train-issue-80"])
        self.assertEqual(trains[0]["outcome"], "shipped")


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

    def test_stacked_pr_not_in_shipped_bucket(self):
        """A merged PR whose base isn't the analyzed branch is kept in the bundle
        but is not classified as shipped-to-main."""
        bundle = _sample_bundle()
        bundle["meta"] = {"base_branch": "main"}
        bundle["prs"][0]["base"] = "users/x/feature"
        bundle.setdefault("buckets", {"shipped": [], "in_flight": [],
                                      "rejected": [], "next_candidates": []})
        link.attach_commit_prs(bundle["commits"])
        buckets = link.compute_buckets(bundle)
        self.assertNotIn(("pr", 42), {(r["type"], r["id"]) for r in buckets["shipped"]})

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
        arts = derive.build_artifacts(self._bundle())
        paths = {a["path"] for a in arts.values()}
        self.assertNotIn("src/app.py", paths)  # not a tracked artifact kind

    def test_add_then_change_builds_ordered_lifecycle(self):
        arts = derive.build_artifacts(self._bundle())
        readme = next(a for a in arts.values() if a["path"] == "README.md")
        self.assertEqual(readme["kind"], "readme")
        self.assertEqual([e["event"] for e in readme["lifecycle"]], ["change"])
        self.assertEqual(readme["status"], "live")
        self.assertIsNone(readme["code_area"])  # graphify deferred to Phase 3b

    def test_delete_sets_status_removed(self):
        arts = derive.build_artifacts(self._bundle())
        doc = next(a for a in arts.values() if a["path"] == "docs/firewall.md")
        self.assertEqual([e["event"] for e in doc["lifecycle"]],
                         ["add", "remove"])
        self.assertEqual(doc["status"], "removed")

    def test_rename_links_replaced_and_replaced_by(self):
        arts = derive.build_artifacts(self._bundle())
        old_id = derive.artifact_id("examples/basic/main.bicep")
        new_id = derive.artifact_id("examples/advanced/main.bicep")
        self.assertEqual(arts[old_id]["status"], "replaced")
        self.assertEqual(arts[old_id]["replaced_by"], new_id)
        # the new artifact records an `add` event from the rename commit
        self.assertEqual(arts[new_id]["lifecycle"][0]["event"], "add")
        self.assertEqual(arts[new_id]["status"], "live")

    def test_lifecycle_refs_are_well_formed_commit_refs(self):
        arts = derive.build_artifacts(self._bundle())
        for a in arts.values():
            for ev in a["lifecycle"]:
                self.assertEqual(ev["ref"]["type"], "commit")
                self.assertEqual(len(ev["ref"]["id"]), 40)
                self.assertTrue(ev["ref"]["url"].startswith("https://"))

    def test_empty_code_events_yields_empty_map(self):
        self.assertEqual(derive.build_artifacts({"code_events": []}), {})

    def test_copy_creates_new_artifact_but_leaves_source_live(self):
        """A 'copy' event introduces the new path but must NOT supersede the source."""
        bundle = {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                {"commit": "c1" * 20, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "examples/a.bicep"},
                {"commit": "c2" * 20, "author": "Bob", "date": "2026-05-10",
                 "change": "copy", "old_path": "examples/a.bicep",
                 "path": "examples/b.bicep"},
            ],
        }
        arts = derive.build_artifacts(bundle)
        src_id = derive.artifact_id("examples/a.bicep")
        dst_id = derive.artifact_id("examples/b.bicep")

        # source artifact must still be live — copy does not supersede it
        self.assertIn(src_id, arts)
        self.assertEqual(arts[src_id]["status"], "live")
        self.assertIsNone(arts[src_id]["replaced_by"])
        src_events = [e["event"] for e in arts[src_id]["lifecycle"]]
        self.assertNotIn("remove", src_events)

        # destination artifact must exist with a leading 'add' event
        self.assertIn(dst_id, arts)
        self.assertEqual(arts[dst_id]["lifecycle"][0]["event"], "add")
        self.assertEqual(arts[dst_id]["status"], "live")


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
                      "created_at": "2026-05-12T10:00:00Z",
                      "url": "https://github.com/o/r/pull/42#discussion_r7001"}],
                 "comments_list": [
                     {"id": 8001, "author": "carol", "body": "y",
                      "created_at": "2026-05-13T09:00:00Z",
                      "url": "https://github.com/o/r/pull/42#issuecomment-8001"}]},
            ],
            "issues": [
                {"number": 18, "url": "https://github.com/o/r/issues/18",
                 "comments_list": [
                     {"id": 9001, "author": "dave", "body": "z",
                      "created_at": "2026-05-15T08:00:00Z",
                      "url": "https://github.com/o/r/issues/18#issuecomment-9001"}],
                 "reactions": {"+1": 9, "total": 12}, "open_high_activity": True},
            ],
        }

    def test_timeline_merges_social_and_code_layers(self):
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
        tl = link.build_timeline(b)
        layers = {e["layer"] for e in tl}
        self.assertEqual(layers, {"social", "code"})

    def test_every_event_has_required_shape(self):
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
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
        b["artifacts"] = derive.build_artifacts(b)
        tl = link.build_timeline(b)
        self.assertEqual([e["ts"] for e in tl], sorted(e["ts"] for e in tl))

    def test_code_event_subject_carries_path_and_kind(self):
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
        code = [e for e in link.build_timeline(b) if e["layer"] == "code"][0]
        self.assertEqual(code["subject"]["path"], "docs/firewall.md")
        self.assertEqual(code["subject"]["kind"], "doc")

    def test_empty_bundle_yields_empty_timeline(self):
        self.assertEqual(link.build_timeline(
            {"prs": [], "issues": [], "artifacts": {}}), [])

    def test_social_events_carry_iso_timestamps_not_urls(self):
        """Social events must have a real ISO date in ts, not a URL."""
        import re
        iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}")
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
        tl = link.build_timeline(b)
        social = [e for e in tl if e["layer"] == "social"]
        self.assertTrue(social, "must have social events")
        for ev in social:
            self.assertRegex(ev["ts"], iso_re,
                             f"social ts looks like a URL or is blank: {ev['ts']!r}")

    def test_timeline_is_sorted_chronologically_code_and_social_interleaved(self):
        """Code event (2026-05-03) precedes social events (2026-05-12+)."""
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
        tl = link.build_timeline(b)
        ts_list = [e["ts"] for e in tl]
        self.assertEqual(ts_list, sorted(ts_list))
        layers = [e["layer"] for e in tl]
        # code event at 2026-05-03 is first; social events follow
        self.assertEqual(layers[0], "code")
        self.assertTrue(all(lay == "social" for lay in layers[1:]))


class TestComputeFeatureDeltas(unittest.TestCase):
    def _bundle(self):
        b = {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "examples/basic/main.bicep"},
                {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
                 "change": "delete", "path": "docs/firewall.md"},
                {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
                 "change": "modify", "path": "README.md"},
            ],
            # commit c1 resolves to PR 42 via its message; others do not.
            "commits": [
                {"sha": "c1"*20, "message": "Add basic example (#42)", "pr": None},
            ],
            "prs": [{"number": 42, "url": "https://github.com/o/r/pull/42"}],
            "issues": [], "trains": [
                {"id": "train-pr-42", "prs": [42], "root_issue": None}],
        }
        link.attach_commit_prs(b["commits"])
        b["artifacts"] = derive.build_artifacts(b)
        return b

    def test_add_remove_change_map_to_delta_kinds(self):
        deltas = link.compute_feature_deltas(self._bundle())
        kinds = {(d["subject"], d["kind"]) for d in deltas}
        self.assertIn(("example", "add"), kinds)
        self.assertIn(("readme", "change"), kinds)
        self.assertIn(("doc", "drop"), kinds)

    def test_delta_attributes_author_commit_and_artifact(self):
        deltas = link.compute_feature_deltas(self._bundle())
        add = next(d for d in deltas if d["kind"] == "add")
        self.assertEqual(add["author"], "Alice")
        self.assertEqual(add["commit"], "c1"*20)
        self.assertEqual(add["artifact"], derive.artifact_id("examples/basic/main.bicep"))
        self.assertTrue(add["url"].startswith("https://"))
        self.assertIsNone(add["area"])  # graphify deferred

    def test_delta_resolves_owning_pr_and_train_when_known(self):
        deltas = link.compute_feature_deltas(self._bundle())
        add = next(d for d in deltas if d["kind"] == "add")
        self.assertEqual(add["pr"], 42)          # c1 -> (#42)
        self.assertEqual(add["train"], "train-pr-42")
        drop = next(d for d in deltas if d["kind"] == "drop")
        self.assertIsNone(drop["pr"])            # c4 has no resolvable PR
        self.assertIsNone(drop["train"])

    def test_empty_artifacts_yield_no_deltas(self):
        self.assertEqual(link.compute_feature_deltas(
            {"artifacts": {}, "commits": [], "trains": []}), [])


class TestCodeAreaAttribution(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_graph": {"provider": "directory", "areas": [
                {"id": "examples/basic", "label": "basic",
                 "paths": ["examples/basic/main.bicep"], "edges": []},
                {"id": "docs", "label": "docs",
                 "paths": ["docs/firewall.md"], "edges": []},
            ]},
            "artifacts": {
                "art:examples/basic/main.bicep": {
                    "kind": "example", "path": "examples/basic/main.bicep",
                    "name": "main.bicep", "status": "live", "replaced_by": None,
                    "code_area": None, "lifecycle": []},
                "art:docs/firewall.md": {
                    "kind": "doc", "path": "docs/firewall.md", "name": "firewall.md",
                    "status": "removed", "replaced_by": None, "code_area": None,
                    "lifecycle": []},
                "art:README.md": {
                    "kind": "readme", "path": "README.md", "name": "README.md",
                    "status": "live", "replaced_by": None, "code_area": None,
                    "lifecycle": []},
            },
            "feature_deltas": [
                {"kind": "add", "subject": "example", "name": "main.bicep",
                 "artifact": "art:examples/basic/main.bicep", "area": None,
                 "commit": "c1", "url": "u"},
                {"kind": "drop", "subject": "doc", "name": "firewall.md",
                 "artifact": "art:docs/firewall.md", "area": None,
                 "commit": "c4", "url": "u"},
            ],
        }

    def test_area_index_maps_each_path_to_its_area(self):
        idx = derive.area_index(self._bundle()["code_graph"])
        self.assertEqual(idx["examples/basic/main.bicep"], "examples/basic")
        self.assertEqual(idx["docs/firewall.md"], "docs")

    def test_attribute_fills_artifact_code_area(self):
        b = self._bundle()
        link.attribute_code_areas(b)
        arts = b["artifacts"]
        self.assertEqual(arts["art:examples/basic/main.bicep"]["code_area"],
                         "examples/basic")
        self.assertEqual(arts["art:docs/firewall.md"]["code_area"], "docs")
        # a path not in the graph stays null (no guessing)
        self.assertIsNone(arts["art:README.md"]["code_area"])

    def test_attribute_fills_feature_delta_area(self):
        b = self._bundle()
        link.attribute_code_areas(b)
        by_artifact = {d["artifact"]: d for d in b["feature_deltas"]}
        self.assertEqual(
            by_artifact["art:examples/basic/main.bicep"]["area"], "examples/basic")
        self.assertEqual(by_artifact["art:docs/firewall.md"]["area"], "docs")

    def test_empty_code_graph_leaves_everything_null(self):
        b = self._bundle()
        b["code_graph"] = {}
        link.attribute_code_areas(b)
        self.assertIsNone(b["artifacts"]["art:docs/firewall.md"]["code_area"])
        self.assertIsNone(b["feature_deltas"][0]["area"])


class TestTrainsModulesPeopleAreas(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_graph": {"provider": "directory", "areas": [
                {"id": "avm/res/network/firewall-policy", "label": "firewall-policy",
                 "paths": ["avm/res/network/firewall-policy/main.bicep"],
                 "edges": []},
                {"id": "docs", "label": "docs",
                 "paths": ["docs/firewall.md"], "edges": []},
            ]},
            "commits": [
                {"sha": "c1", "author": "alice", "pr": 42,
                 "files": ["avm/res/network/firewall-policy/main.bicep"]},
                {"sha": "c2", "author": "bob", "pr": 42,
                 "files": ["docs/firewall.md"]},
            ],
            "prs": [{"number": 42, "author": "alice", "reviewers": ["carol"],
                     "url": "https://github.com/o/r/pull/42"}],
            "issues": [],
            "trains": [{"id": "train-pr-42", "prs": [42], "commits": ["c1", "c2"],
                        "root_issue": None, "code_areas": []}],
            "people": {},
        }

    def test_trains_gain_their_commits_code_areas(self):
        b = self._bundle()
        link.attribute_train_areas(b, derive.area_index(b["code_graph"]))
        t = b["trains"][0]
        self.assertEqual(set(t["code_areas"]),
                         {"avm/res/network/firewall-policy", "docs"})

    def test_modules_field_aggregates_per_area(self):
        b = self._bundle()
        link.build_modules(b, derive.area_index(b["code_graph"]))
        mods = b["modules"]
        fp = mods["avm/res/network/firewall-policy"]
        self.assertEqual(fp["commits"], 1)
        self.assertEqual(fp["files_changed"], 1)
        # prs is a count of distinct PRs that touched the area (an int, not a list)
        self.assertEqual(fp["prs"], 1)

    def test_people_gain_modules_and_areas(self):
        b = self._bundle()
        idx = derive.area_index(b["code_graph"])
        derive.attribute_people_areas(b, idx)
        alice = b["people"]["alice"]
        self.assertIn("avm/res/network/firewall-policy", alice["modules"])

    def test_enrich_fills_all_phase3b_attribution(self):
        bundle = _enrich_via_store("bundle_p3b.json")
        # at least one artifact and one feature_delta now carry a real area
        arts = bundle["artifacts"]
        self.assertTrue(any(a["code_area"] is not None for a in arts.values()))
        self.assertTrue(any(d["area"] is not None for d in bundle["feature_deltas"]))
        # trains carry code_areas; modules populated
        self.assertTrue(any(t.get("code_areas") for t in bundle["trains"]))
        self.assertTrue(bundle["modules"])


class TestPhase3bConsistency(unittest.TestCase):
    def test_attribution_preserves_artifact_and_delta_refs(self):
        b = _enrich_via_store("bundle_p3b.json")
        for d in b["feature_deltas"]:
            self.assertTrue(str(d["url"]).startswith("https://"))
            self.assertIn(d["artifact"], b["artifacts"])
        for a in b["artifacts"].values():
            for ev in a["lifecycle"]:
                self.assertTrue(str(ev["ref"]["url"]).startswith("https://"))

    def test_modules_counts_are_non_negative_and_sum_sane(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = link.enrich(json.load(fh))
        for area, m in b["modules"].items():
            self.assertGreaterEqual(m["commits"], 1)
            self.assertGreaterEqual(m["files_changed"], 1)
            self.assertGreaterEqual(m["prs"], 0)

    def test_train_code_areas_are_known_area_ids(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = link.enrich(json.load(fh))
        known = {a["id"] for a in b["code_graph"]["areas"]}
        for t in b["trains"]:
            for area in t.get("code_areas", []):
                self.assertIn(area, known)


class TestSymbolArtifacts(unittest.TestCase):
    """Phase 3d: symbol_events fold into kind:symbol/comment artifacts + feature_deltas
    carry the bounded before/after/detail."""

    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"}, "code_events": [], "commits": [],
            "prs": [], "issues": [], "trains": [],
            "symbol_events": [
                {"commit": "a" * 40, "author": "Alice", "date": "2026-05-03",
                 "path": "avm/res/foo/main.bicep", "lang": "bicep",
                 "subkind": "resource", "name": "vault", "change": "change",
                 "before": "name: 'old'", "after": "name: 'new'"},
                {"commit": "a" * 40, "author": "Alice", "date": "2026-05-03",
                 "path": "avm/res/foo/main.bicep", "lang": "bicep",
                 "subkind": "param", "name": "newParam", "change": "add",
                 "before": None, "after": "param newParam string"},
            ],
        }

    def test_symbol_events_become_symbol_artifacts(self):
        arts = derive.build_artifacts(self._bundle())
        sym = [a for a in arts.values() if a["kind"] == "symbol"]
        self.assertEqual(len(sym), 2)
        vault = next(a for a in sym if a["name"] == "vault")
        self.assertEqual(vault["subkind"], "resource")
        self.assertEqual(vault["lang"], "bicep")
        self.assertEqual(vault["lifecycle"][0]["after"], "name: 'new'")

    def test_feature_deltas_carry_before_after_detail(self):
        b = self._bundle()
        b["artifacts"] = derive.build_artifacts(b)
        deltas = link.compute_feature_deltas(b)
        vault = next(d for d in deltas if d["name"] == "vault")
        self.assertEqual(vault["kind"], "change")
        self.assertEqual(vault["subject"], "symbol")
        self.assertEqual(vault["before"], "name: 'old'")
        self.assertEqual(vault["after"], "name: 'new'")
        self.assertEqual(vault["detail"], "bicep resource vault")

    def test_symbol_artifacts_get_code_area(self):
        b = self._bundle()
        b["code_graph"] = {"areas": [{"id": "avm/res/foo",
                                      "paths": ["avm/res/foo/main.bicep"]}]}
        b["artifacts"] = derive.build_artifacts(b)
        link.attribute_code_areas(b)
        vault = next(a for a in b["artifacts"].values() if a["name"] == "vault")
        self.assertEqual(vault["code_area"], "avm/res/foo")

    def test_todo_comment_event_folds_into_comment_artifact(self):
        # subkind:"todo" (TODO/FIXME markers) must fold into a kind:"comment" artifact,
        # not "symbol", so feature_deltas[].subject and comment reporting are correct.
        b = {"meta": {"owner": "o", "repo": "r"}, "code_events": [], "commits": [],
             "prs": [], "issues": [], "trains": [],
             "symbol_events": [
                 {"commit": "a" * 40, "author": "A", "date": "2026-05-03",
                  "path": "avm/res/foo/main.bicep", "lang": "bicep", "subkind": "todo",
                  "name": "// TODO: revisit retention", "change": "add",
                  "before": None, "after": "// TODO: revisit retention"}]}
        b["artifacts"] = derive.build_artifacts(b)
        todo = next(iter(b["artifacts"].values()))
        self.assertEqual(todo["kind"], "comment")
        self.assertEqual(todo["subkind"], "todo")
        delta = link.compute_feature_deltas(b)[0]
        self.assertEqual(delta["subject"], "comment")
        self.assertEqual(delta["detail"], "bicep todo // TODO: revisit retention")


class TestSymbolIdentity(unittest.TestCase):
    """Phase 3e: window-wide symbol move detection (precision over recall)."""

    def _ev(self, path, subkind, name, change):
        return {"commit": "c", "author": "A", "date": "2026-05-03", "path": path,
                "lang": "bicep", "subkind": subkind, "name": name, "change": change,
                "before": None, "after": None}

    def test_unique_move_linked_medium(self):
        evs = [self._ev("a/main.bicep", "resource", "vault", "drop"),
               self._ev("b/main.bicep", "resource", "vault", "add")]
        moves = derive.match_symbol_moves(evs)
        self.assertEqual(len(moves), 1)
        self.assertEqual((moves[0]["from_path"], moves[0]["to_path"]),
                         ("a/main.bicep", "b/main.bicep"))
        self.assertEqual(moves[0]["confidence"], "medium")
        self.assertEqual(moves[0]["basis"], "unique_name")

    def test_file_rename_pair_is_high_confidence(self):
        evs = [self._ev("a/main.bicep", "resource", "vault", "drop"),
               self._ev("b/main.bicep", "resource", "vault", "add")]
        moves = derive.match_symbol_moves(evs, [("a/main.bicep", "b/main.bicep")])
        self.assertEqual(moves[0]["confidence"], "high")
        self.assertEqual(moves[0]["basis"], "file_rename")

    def test_ambiguous_name_is_skipped(self):
        # boilerplate `location` dropped in two files and added in two -> NOT a move
        evs = [self._ev("a/main.bicep", "param", "location", "drop"),
               self._ev("c/main.bicep", "param", "location", "drop"),
               self._ev("b/main.bicep", "param", "location", "add"),
               self._ev("d/main.bicep", "param", "location", "add")]
        self.assertEqual(derive.match_symbol_moves(evs), [])

    def test_same_file_readd_is_not_a_move(self):
        evs = [self._ev("a/main.bicep", "param", "x", "drop"),
               self._ev("a/main.bicep", "param", "x", "add")]
        self.assertEqual(derive.match_symbol_moves(evs), [])

    def test_comments_excluded(self):
        evs = [self._ev("a/m.bicep", "comment", "// note", "drop"),
               self._ev("b/m.bicep", "comment", "// note", "add")]
        self.assertEqual(derive.match_symbol_moves(evs), [])

    def test_cross_language_not_linked(self):
        # same subkind+name but different language -> NOT the same symbol
        evs = [{**self._ev("a/main.bicep", "module", "net", "drop")},
               {**self._ev("b/main.tf", "module", "net", "add"), "lang": "terraform"}]
        self.assertEqual(derive.match_symbol_moves(evs), [])

    def test_link_symbol_identity_sets_replaced_by_and_confidence(self):
        b = {"meta": {"owner": "o", "repo": "r"}, "commits": [], "prs": [], "issues": [],
             "code_events": [], "symbol_events": [
                 self._ev("a/main.bicep", "resource", "vault", "drop"),
                 self._ev("b/main.bicep", "resource", "vault", "add")]}
        b["artifacts"] = derive.build_artifacts(b)
        link.link_symbol_identity(b)
        src = "a/main.bicep#bicep:resource:vault"
        dst = "b/main.bicep#bicep:resource:vault"
        self.assertEqual(b["artifacts"][src]["status"], "replaced")
        self.assertEqual(b["artifacts"][src]["replaced_by"], dst)
        self.assertEqual(b["artifacts"][dst]["identity_from"], src)
        self.assertEqual(b["artifacts"][dst]["move_confidence"], "medium")
        self.assertEqual(b["symbol_moves"]["by_confidence"], {"high": 0, "medium": 1})


class TestTrainSignificance(unittest.TestCase):
    """Phase 4a: significance score + treatment tier on each train."""

    def _train(self, id_, kind, prs, commits, code_areas):
        """Minimal in-memory train fixture; code_areas already populated."""
        return {
            "id": id_,
            "kind": kind,
            "root_issue": None,
            "prs": prs,
            "commits": commits,
            "code_areas": code_areas,
            "outcome": "shipped",
            "evidence": [],
        }

    def _bundle(self, trains):
        return {"trains": trains}

    def test_contributing_prs_add_to_footprint(self):
        """Stacked/fork contributions on a train earn it significance — a train
        with contributing_prs outscores an otherwise-identical train without."""
        a = self._train("train-pr-1", "other", [1], [], [])
        b = self._train("train-pr-2", "other", [2], [], [])
        b["contributing_prs"] = [10, 11]
        link.score_train_significance(self._bundle([a, b]))
        self.assertGreater(b["significance"], a["significance"])

    # ----- ranking order -----

    def test_larger_heavier_train_scores_above_small_one(self):
        """A feature train with more PRs/commits/areas scores above a tiny other train."""
        big = self._train("train-issue-1", "feature", [1, 2, 3], ["s1", "s2", "s3", "s4"], ["area-a", "area-b", "area-c"])
        small = self._train("train-pr-99", "other", [99], ["x1"], [])
        bundle = self._bundle([big, small])
        link.score_train_significance(bundle)
        self.assertGreater(big["significance"], small["significance"])

    # ----- kind_weight applied -----

    def test_kind_weight_differentiates_equal_footprint_trains(self):
        """Two trains with identical prs/commits/areas rank by kind_weight alone."""
        feature_train = self._train("train-issue-10", "feature", [10], ["c1"], ["area-a"])
        bug_train = self._train("train-issue-11", "bug", [11], ["c2"], ["area-b"])
        bundle = self._bundle([feature_train, bug_train])
        link.score_train_significance(bundle)
        # feature weight > bug weight -> feature scores higher
        self.assertGreater(feature_train["significance"], bug_train["significance"])

    def test_unknown_kind_falls_back_to_other_weight(self):
        """An unknown kind falls back to 'other' weight, not zero/error."""
        t = self._train("train-pr-5", "unknown-kind-xyz", [5], ["c1"], ["area-a"])
        bundle = self._bundle([t])
        link.score_train_significance(bundle)
        # must produce the same score as an explicit 'other' train with equal footprint
        other = self._train("train-pr-6", "other", [6], ["c2"], ["area-b"])
        link.score_train_significance(self._bundle([other]))
        self.assertEqual(t["significance"], other["significance"])

    # ----- breadth contributes -----

    def test_breadth_raises_score_for_cross_cutting_work(self):
        """Same kind+prs+commits, more code_areas -> higher significance."""
        narrow = self._train("train-issue-20", "bug", [20], ["c1"], ["area-a"])
        wide = self._train("train-issue-21", "bug", [21], ["c2"], ["area-b", "area-c", "area-d"])
        bundle = self._bundle([narrow, wide])
        link.score_train_significance(bundle)
        self.assertGreater(wide["significance"], narrow["significance"])

    # ----- tier selection: deep vs mention -----

    def test_top_n_trains_get_deep_tier(self):
        """The top-N trains by significance should be tier 'deep'."""
        N = link.TRAIN_SIGNIFICANCE_TOP_N
        trains = [
            self._train(f"train-issue-{i}", "feature",
                        [i] * (N + 2 - i),  # descending size
                        [f"c{i}"],
                        [f"area-{i}"])
            for i in range(1, N + 3)
        ]
        bundle = self._bundle(trains)
        link.score_train_significance(bundle)
        # sort by significance desc to identify the top-N
        ranked = sorted(trains, key=lambda t: (-t["significance"], t["id"]))
        for t in ranked[:N]:
            self.assertEqual(t["tier"], "deep", f"{t['id']} should be deep")
        # at least one mention below the top-N (the smallest trains)
        mention_found = any(t["tier"] == "mention" for t in ranked[N:])
        self.assertTrue(mention_found, "expected at least one mention-tier train below top-N")

    def test_high_significance_train_gets_deep_regardless_of_rank(self):
        """A train at or above the floor threshold is always deep, even if ranked > N."""
        # Create N+1 identical large feature trains so the (N+1)th still clears the floor
        N = link.TRAIN_SIGNIFICANCE_TOP_N
        floor = link.TRAIN_SIGNIFICANCE_FLOOR
        trains = []
        for i in range(N + 1):
            # Build a train whose significance >= floor
            # footprint = prs + commits + areas; weight for feature = 3.0
            # sig = footprint * 3.0 + breadth, breadth = len(areas)
            # use 5 PRs + 5 commits + 5 areas -> footprint=15, breadth=5 -> 15*3+5=50
            trains.append(self._train(
                f"train-issue-{100 + i}", "feature",
                list(range(i * 10, i * 10 + 5)),
                [f"sha{i}{j}" for j in range(5)],
                [f"area-x{i}", f"area-y{i}", f"area-z{i}", f"area-w{i}", f"area-v{i}"],
            ))
        bundle = self._bundle(trains)
        link.score_train_significance(bundle)
        # all should clear the floor -> all should be deep
        for t in trains:
            self.assertGreaterEqual(t["significance"], floor,
                                    f"{t['id']} significance {t['significance']} < floor {floor}")
            self.assertEqual(t["tier"], "deep", f"{t['id']} should be deep (clears floor)")

    def test_large_rejected_train_is_still_deep(self):
        """Outcome does not influence tier — a large rejected train is deep."""
        big_rejected = self._train("train-issue-30", "feature", [30, 31, 32], ["c1", "c2", "c3"], ["area-a", "area-b"])
        big_rejected["outcome"] = "rejected"
        bundle = self._bundle([big_rejected])
        link.score_train_significance(bundle)
        # significance should reflect footprint, not outcome
        self.assertGreater(big_rejected["significance"], 0)
        # given it's a large feature train, it should be deep
        self.assertEqual(big_rejected["tier"], "deep")
        # prove outcome-independence: an equivalently-sized shipped train gets the same tier
        equiv_shipped = self._train("train-issue-31", "feature", [30, 31, 32], ["c1", "c2", "c3"], ["area-a", "area-b"])
        b2 = self._bundle([equiv_shipped])
        link.score_train_significance(b2)
        self.assertEqual(big_rejected["tier"], equiv_shipped["tier"])

    def test_tiny_lightweight_trains_are_mention(self):
        """Tiny docs and chore trains with minimal footprint are both mention tier
        when outranked by enough larger trains to fall outside the top-N."""
        N = link.TRAIN_SIGNIFICANCE_TOP_N
        # Populate N large feature trains so both tiny trains fall below top-N
        # and also below the floor (their sig = 2*1.0+0 = 2.0, well under floor).
        large = [
            self._train(f"train-issue-{i}", "feature",
                        list(range(i * 10, i * 10 + 5)),
                        [f"sha{i}{j}" for j in range(5)],
                        [f"area-x{i}", f"area-y{i}", f"area-z{i}"])
            for i in range(N)
        ]
        tiny_docs = self._train("train-pr-200", "docs", [200], ["c200"], [])
        tiny_chore = self._train("train-pr-201", "chore", [201], ["c201"], [])
        bundle = self._bundle(large + [tiny_docs, tiny_chore])
        link.score_train_significance(bundle)
        self.assertEqual(tiny_docs["tier"], "mention")
        self.assertEqual(tiny_chore["tier"], "mention")

    # ----- determinism / stable ordering -----

    def test_tier_is_deterministic_on_repeated_calls(self):
        """Calling score_train_significance twice yields the same tiers."""
        trains = [
            self._train("train-issue-40", "feature", [40, 41], ["c1", "c2"], ["area-a"]),
            self._train("train-pr-50", "docs", [50], ["c3"], []),
        ]
        bundle = self._bundle(trains)
        link.score_train_significance(bundle)
        tiers_first = [t["tier"] for t in trains]
        sigs_first = [t["significance"] for t in trains]
        # re-run (enrich pipeline calls it; significance + tier must be stable)
        link.score_train_significance(bundle)
        self.assertEqual([t["tier"] for t in trains], tiers_first)
        self.assertEqual([t["significance"] for t in trains], sigs_first)

    def test_tied_significance_breaks_by_id(self):
        """When two trains tie on significance and only one fits in top-N, the
        lexicographically-lower id wins the 'deep' slot."""
        t1 = self._train("train-issue-60", "bug", [60], ["c1"], ["area-a"])
        t2 = self._train("train-issue-70", "bug", [70], ["c2"], ["area-b"])
        orig_n = link.TRAIN_SIGNIFICANCE_TOP_N
        link.TRAIN_SIGNIFICANCE_TOP_N = 1
        try:
            bundle = self._bundle([t1, t2])
            link.score_train_significance(bundle)
            self.assertEqual(t1["tier"], "deep")
            self.assertEqual(t2["tier"], "mention")
        finally:
            link.TRAIN_SIGNIFICANCE_TOP_N = orig_n

    # ----- wired into enrich -----

    def test_enrich_stamps_significance_and_tier_on_all_trains(self):
        """enrich() must leave significance + tier on every train."""
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = link.enrich(json.load(fh))
        self.assertTrue(bundle["trains"], "fixture must have at least one train")
        for t in bundle["trains"]:
            self.assertIn("significance", t, f"train {t['id']} missing significance")
            self.assertIn("tier", t, f"train {t['id']} missing tier")
            self.assertIsInstance(t["significance"], float)
            self.assertIn(t["tier"], {"deep", "mention"})

    def test_significance_formula_matches_spec(self):
        """Verify the exact formula: sig = footprint * kind_weight + breadth."""
        kind = "feature"
        prs = [1, 2]
        commits = ["c1", "c2", "c3"]
        areas = ["area-a", "area-b"]
        t = self._train("train-issue-80", kind, prs, commits, areas)
        bundle = self._bundle([t])
        link.score_train_significance(bundle)

        footprint = len(prs) + len(commits) + len(areas)   # 2+3+2 = 7
        kind_weight = link.TRAIN_KIND_WEIGHTS.get(kind, link.TRAIN_KIND_WEIGHTS["other"])
        breadth = len(areas)                                # 2
        expected = footprint * kind_weight + breadth        # 7*3.0+2 = 23.0
        self.assertAlmostEqual(t["significance"], expected)


class TestTrainEffort(unittest.TestCase):
    """Phase 4a: per-train effort metrics block."""

    # ------------------------------------------------------------------
    # Helpers / fixtures
    # ------------------------------------------------------------------

    def _pr(self, number, author, created_at, merged=True, merged_at=None,
            reviewers=None, review_comments_count=0, comments_list=None):
        return {
            "number": number,
            "title": f"PR {number}",
            "url": f"https://github.com/o/r/pull/{number}",
            "author": author,
            "created_at": created_at,
            "merged": merged,
            "merged_at": merged_at,
            "reviewers": reviewers or [],
            "review_comments_count": review_comments_count,
            "comments_list": comments_list or [],
        }

    def _issue(self, number, author, created_at, comments_list=None):
        return {
            "number": number,
            "title": f"Issue {number}",
            "url": f"https://github.com/o/r/issues/{number}",
            "author": author,
            "created_at": created_at,
            "comments_list": comments_list or [],
        }

    def _train(self, id_, root_issue, pr_numbers, commit_shas):
        return {
            "id": id_,
            "kind": "feature",
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": commit_shas,
            "code_areas": [],
            "outcome": "shipped",
            "evidence": [],
        }

    def _bundle(self, trains, prs, issues):
        return {"trains": trains, "prs": prs, "issues": issues}

    # ------------------------------------------------------------------
    # Happy-path: all fields populated correctly
    # ------------------------------------------------------------------

    def test_full_happy_path(self):
        """opened_at = earliest of issue/PR; merged_at = latest merged PR;
        elapsed correct; reviewers de-duplicated; review_comments summed;
        commits counted; participants de-duplicated."""
        issue = self._issue(
            1, "alice", "2026-05-01T00:00:00Z",
            comments_list=[{"author": "bob", "body": "looks good"}],
        )
        pr_a = self._pr(
            10, "alice", "2026-05-03T00:00:00Z",
            merged=True, merged_at="2026-05-10T00:00:00Z",
            reviewers=["carol", "dave"],
            review_comments_count=3,
            comments_list=[{"author": "eve", "body": "nit"}],
        )
        pr_b = self._pr(
            11, "frank", "2026-05-04T00:00:00Z",
            merged=True, merged_at="2026-05-12T00:00:00Z",
            reviewers=["carol", "grace"],   # carol appears in both PRs -> dedup
            review_comments_count=2,
            comments_list=[{"author": "alice", "body": "lgtm"}],  # alice is also PR author/issue author
        )
        train = self._train("train-issue-1", 1, [10, 11], ["sha1", "sha2", "sha3"])
        bundle = self._bundle([train], [pr_a, pr_b], [issue])

        link.annotate_train_effort(bundle)

        eff = train["effort"]

        # opened_at: issue created 2026-05-01, earliest PR created 2026-05-03 -> issue wins
        self.assertEqual(eff["opened_at"], "2026-05-01T00:00:00Z")

        # merged_at: latest of 2026-05-10 and 2026-05-12 -> 2026-05-12
        self.assertEqual(eff["merged_at"], "2026-05-12T00:00:00Z")

        # elapsed_days: 11 days (May 1 -> May 12)
        self.assertEqual(eff["elapsed_days"], 11)

        # reviewers: {carol, dave, grace} -> 3 (carol deduped)
        self.assertEqual(eff["reviewers"], 3)

        # review_comments: 3 + 2 = 5
        self.assertEqual(eff["review_comments"], 5)

        # commits: 3
        self.assertEqual(eff["commits"], 3)

        # participants: alice (PR author + issue author + PR commenter), frank (PR author),
        #   carol, dave, grace (reviewers), bob (issue commenter), eve (PR commenter)
        # = {alice, frank, carol, dave, grace, bob, eve} -> 7
        self.assertEqual(eff["participants"], 7)

    # ------------------------------------------------------------------
    # opened_at: earliest wins
    # ------------------------------------------------------------------

    def test_opened_at_uses_pr_when_issue_is_later(self):
        """When PR created_at < issue created_at, PR wins."""
        issue = self._issue(2, "alice", "2026-05-10T00:00:00Z")
        pr = self._pr(20, "alice", "2026-05-05T00:00:00Z",
                      merged=True, merged_at="2026-05-20T00:00:00Z")
        train = self._train("train-issue-2", 2, [20], ["s1"])
        bundle = self._bundle([train], [pr], [issue])
        link.annotate_train_effort(bundle)
        self.assertEqual(train["effort"]["opened_at"], "2026-05-05T00:00:00Z")

    # ------------------------------------------------------------------
    # Null degradation: no merged PR
    # ------------------------------------------------------------------

    def test_no_merged_pr_gives_null_merged_and_elapsed_and_stalled_false(self):
        """A train with only an unmerged PR has merged_at=None, elapsed_days=None, stalled=False."""
        issue = self._issue(3, "alice", "2026-05-01T00:00:00Z")
        pr = self._pr(30, "alice", "2026-05-02T00:00:00Z",
                      merged=False, merged_at=None)
        train = self._train("train-issue-3", 3, [30], [])
        bundle = self._bundle([train], [pr], [issue])
        link.annotate_train_effort(bundle)
        eff = train["effort"]
        self.assertIsNone(eff["merged_at"])
        self.assertIsNone(eff["elapsed_days"])
        self.assertFalse(eff["stalled"])

    # ------------------------------------------------------------------
    # Null degradation: PR-only train (root_issue=None)
    # ------------------------------------------------------------------

    def test_pr_only_train_opened_at_from_pr_no_issue_author_in_participants(self):
        """A PR-only train (root_issue=None) uses PR created_at for opened_at;
        no issue author is counted in participants."""
        pr = self._pr(
            40, "bob", "2026-05-07T00:00:00Z",
            merged=True, merged_at="2026-05-14T00:00:00Z",
            reviewers=["carol"],
        )
        train = self._train("train-pr-40", None, [40], ["s1", "s2"])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        eff = train["effort"]
        self.assertEqual(eff["opened_at"], "2026-05-07T00:00:00Z")
        # participants: bob (author), carol (reviewer) -> 2
        self.assertEqual(eff["participants"], 2)
        # elapsed: 7 days (May 7 -> May 14)
        self.assertEqual(eff["elapsed_days"], 7)

    # ------------------------------------------------------------------
    # stalled boundary: just over vs under TRAIN_STALL_DAYS
    # ------------------------------------------------------------------

    def test_stalled_true_when_elapsed_exceeds_threshold(self):
        """A merged train whose elapsed_days exceeds TRAIN_STALL_DAYS is stalled."""
        # Use 22 days apart — should be over any reasonable threshold (>21)
        pr = self._pr(50, "alice", "2026-05-01T00:00:00Z",
                      merged=True, merged_at="2026-05-23T00:00:00Z")
        train = self._train("train-pr-50", None, [50], [])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        eff = train["effort"]
        self.assertEqual(eff["elapsed_days"], 22)
        # Only stalled if elapsed > TRAIN_STALL_DAYS (e.g. >21 -> True for 22)
        self.assertEqual(eff["stalled"], eff["elapsed_days"] > link.TRAIN_STALL_DAYS)
        self.assertTrue(eff["stalled"])

    def test_stalled_false_when_elapsed_under_threshold(self):
        """A merged train whose elapsed_days is well below TRAIN_STALL_DAYS is not stalled."""
        # Use 5 days apart — well under any reasonable threshold
        pr = self._pr(51, "alice", "2026-05-01T00:00:00Z",
                      merged=True, merged_at="2026-05-06T00:00:00Z")
        train = self._train("train-pr-51", None, [51], [])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        eff = train["effort"]
        self.assertEqual(eff["elapsed_days"], 5)
        self.assertFalse(eff["stalled"])

    def test_stalled_false_at_exact_boundary(self):
        """elapsed_days == TRAIN_STALL_DAYS is NOT stalled (strict >)."""
        # PR opened 2026-05-01, merged exactly TRAIN_STALL_DAYS (21) days later -> 2026-05-22
        pr = self._pr(52, "alice", "2026-05-01T00:00:00Z",
                      merged=True, merged_at="2026-05-22T00:00:00Z")
        train = self._train("train-pr-52", None, [52], [])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        eff = train["effort"]
        self.assertEqual(eff["elapsed_days"], link.TRAIN_STALL_DAYS)
        self.assertFalse(eff["stalled"])

    # ------------------------------------------------------------------
    # Distinctness: same login in multiple roles counts once
    # ------------------------------------------------------------------

    def test_same_login_across_roles_counted_once_in_participants(self):
        """alice as PR author AND reviewer AND commenter is only 1 participant."""
        pr = self._pr(
            60, "alice", "2026-05-01T00:00:00Z",
            merged=True, merged_at="2026-05-08T00:00:00Z",
            reviewers=["alice"],   # also a reviewer
            review_comments_count=2,
            comments_list=[{"author": "alice", "body": "self-review"}],  # also a commenter
        )
        train = self._train("train-pr-60", None, [60], ["s1"])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        self.assertEqual(train["effort"]["participants"], 1)

    # ------------------------------------------------------------------
    # reviewers: distinct across multiple PRs
    # ------------------------------------------------------------------

    def test_reviewers_deduplicated_across_prs(self):
        """Same reviewer on two PRs counts once."""
        pr_a = self._pr(70, "alice", "2026-05-01T00:00:00Z",
                        merged=True, merged_at="2026-05-05T00:00:00Z",
                        reviewers=["bob", "carol"])
        pr_b = self._pr(71, "dave", "2026-05-02T00:00:00Z",
                        merged=True, merged_at="2026-05-08T00:00:00Z",
                        reviewers=["bob", "eve"])  # bob appears in both
        train = self._train("train-pr-70", None, [70, 71], [])
        bundle = self._bundle([train], [pr_a, pr_b], [])
        link.annotate_train_effort(bundle)
        # distinct reviewers: {bob, carol, eve} -> 3
        self.assertEqual(train["effort"]["reviewers"], 3)

    # ------------------------------------------------------------------
    # None/empty logins ignored in participants
    # ------------------------------------------------------------------

    def test_none_logins_ignored_in_participants(self):
        """None/empty comment authors do not inflate the participant count."""
        pr = self._pr(
            80, "alice", "2026-05-01T00:00:00Z",
            merged=True, merged_at="2026-05-03T00:00:00Z",
            comments_list=[
                {"author": None, "body": "ghost"},
                {"author": "", "body": "empty"},
                {"author": "bob", "body": "real"},
            ],
        )
        train = self._train("train-pr-80", None, [80], [])
        bundle = self._bundle([train], [pr], [])
        link.annotate_train_effort(bundle)
        # participants: alice (author) + bob (commenter) = 2
        self.assertEqual(train["effort"]["participants"], 2)

    # ------------------------------------------------------------------
    # Wired into enrich()
    # ------------------------------------------------------------------

    def test_enrich_stamps_effort_on_all_trains(self):
        """enrich() must leave an effort block on every train."""
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = link.enrich(json.load(fh))
        self.assertTrue(bundle["trains"], "fixture must have at least one train")
        for t in bundle["trains"]:
            self.assertIn("effort", t, f"train {t['id']} missing effort")
            eff = t["effort"]
            for field in ("opened_at", "merged_at", "elapsed_days",
                          "reviewers", "review_comments", "commits",
                          "participants", "stalled"):
                self.assertIn(field, eff, f"train {t['id']} effort missing '{field}'")

    def test_effort_after_significance_in_enrich(self):
        """effort block must coexist with significance/tier (both wired in enrich)."""
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = link.enrich(json.load(fh))
        for t in bundle["trains"]:
            self.assertIn("significance", t)
            self.assertIn("tier", t)
            self.assertIn("effort", t)


class TestSliceTrain(unittest.TestCase):
    """Phase 4a: bounded, self-contained per-train slice (slice_train)."""

    # ------------------------------------------------------------------
    # Shared fixture builder
    # ------------------------------------------------------------------

    def _bundle(self):
        """In-memory bundle with two trains, two PRs, two issues, commits,
        feature_deltas across both trains, and a symbol_moves links list."""
        long_body = "x" * 2000   # longer than SLICE_TEXT_CAP (1500)

        issue_1 = {
            "number": 10,
            "title": "Support feature A",
            "body": "short body",
            "url": "https://github.com/o/r/issues/10",
            "labels": ["feature"],
            "kind": "feature",
            "state": "closed",
            "comments_list": [
                {"author": "alice", "body": f"comment {i}"} for i in range(8)
            ],  # 8 comments -> overflow with cap=6
        }
        issue_2 = {
            "number": 11,
            "title": "Support feature B",
            "body": "short body B",
            "url": "https://github.com/o/r/issues/11",
            "labels": [],
            "kind": "bug",
            "state": "closed",
            "comments_list": [],
        }
        pr_1 = {
            "number": 100,
            "title": "Implement feature A part 1",
            "body": long_body,
            "state": "closed",
            "merged": True,
            "created_at": "2026-05-01T00:00:00Z",
            "merged_at": "2026-05-10T00:00:00Z",
            "url": "https://github.com/o/r/pull/100",
            "reviewers": ["carol"],
            "review_decision": "APPROVED",
            "review_comments": [
                {"author": "carol", "body": f"review {i}"} for i in range(7)
            ],  # 7 review comments -> overflow
            "comments_list": [
                {"author": "dave", "body": f"conv {i}"} for i in range(3)
            ],  # 3 < 6 -> no overflow
            "closes": [10],
        }
        pr_2 = {
            "number": 101,
            "title": "Implement feature A part 2",
            "body": "pr body 2",
            "state": "closed",
            "merged": True,
            "created_at": "2026-05-11T00:00:00Z",
            "merged_at": "2026-05-20T00:00:00Z",
            "url": "https://github.com/o/r/pull/101",
            "reviewers": ["eve"],
            "review_decision": "APPROVED",
            "review_comments": [],
            "comments_list": [],
            "closes": [10],
        }
        pr_3 = {
            "number": 200,
            "title": "Fix bug B",
            "body": "bug fix body",
            "state": "closed",
            "merged": True,
            "created_at": "2026-05-15T00:00:00Z",
            "merged_at": "2026-05-22T00:00:00Z",
            "url": "https://github.com/o/r/pull/200",
            "reviewers": [],
            "review_decision": None,
            "review_comments": [],
            "comments_list": [],
            "closes": [11],
        }
        commits = [
            {"sha": "aaa111", "message": "Add feature A part 1 (#100)",
             "author": "alice", "date": "2026-05-01", "pr": 100},
            {"sha": "aaa222", "message": "Add feature A part 2 (#101)",
             "author": "bob", "date": "2026-05-11", "pr": 101},
            {"sha": "bbb111", "message": "Fix bug B (#200)",
             "author": "frank", "date": "2026-05-15", "pr": 200},
        ]
        # train-issue-10 has PRs 100+101 and commits aaa111, aaa222
        # train-issue-11 has PR 200 and commit bbb111
        train_a = {
            "id": "train-issue-10",
            "kind": "feature",
            "root_issue": 10,
            "prs": [100, 101],
            "commits": ["aaa111", "aaa222"],
            "code_areas": ["area-api"],
            "outcome": "shipped",
            "evidence": [
                {"type": "issue", "id": 10, "url": "https://github.com/o/r/issues/10"},
            ],
            "significance": 15.0,
            "tier": "deep",
            "effort": {"opened_at": "2026-05-01T00:00:00Z", "merged_at": "2026-05-20T00:00:00Z",
                        "elapsed_days": 19, "reviewers": 2, "review_comments": 7,
                        "commits": 2, "participants": 4, "stalled": False},
        }
        train_b = {
            "id": "train-issue-11",
            "kind": "bug",
            "root_issue": 11,
            "prs": [200],
            "commits": ["bbb111"],
            "code_areas": ["area-core"],
            "outcome": "shipped",
            "evidence": [
                {"type": "issue", "id": 11, "url": "https://github.com/o/r/issues/11"},
            ],
            "significance": 5.0,
            "tier": "mention",
            "effort": {"opened_at": "2026-05-15T00:00:00Z", "merged_at": "2026-05-22T00:00:00Z",
                        "elapsed_days": 7, "reviewers": 0, "review_comments": 0,
                        "commits": 1, "participants": 1, "stalled": False},
        }
        # feature_deltas: two belong to train_a, one to train_b, one unowned
        art_a = "art:examples/main.bicep"
        art_b = "art:src/api.py"
        art_c = "art:docs/guide.md"
        art_sym_src = "src/old.py#py:function:do_it"
        art_sym_dst = "src/new.py#py:function:do_it"
        feature_deltas = [
            {"artifact": art_a, "train": "train-issue-10", "kind": "add",
             "subject": "example", "name": "main.bicep", "area": "area-api",
             "author": "alice", "pr": 100, "commit": "aaa111",
             "url": "https://github.com/o/r/commit/aaa111",
             "before": None, "after": None, "detail": None},
            {"artifact": art_b, "train": "train-issue-10", "kind": "change",
             "subject": "symbol", "name": "api.py", "area": "area-api",
             "author": "bob", "pr": 101, "commit": "aaa222",
             "url": "https://github.com/o/r/commit/aaa222",
             "before": None, "after": None, "detail": None},
            {"artifact": art_c, "train": "train-issue-11", "kind": "change",
             "subject": "doc", "name": "guide.md", "area": "area-core",
             "author": "frank", "pr": 200, "commit": "bbb111",
             "url": "https://github.com/o/r/commit/bbb111",
             "before": None, "after": None, "detail": None},
            # delta belonging to art_sym_src -> train-issue-10 (for symbol_moves test)
            {"artifact": art_sym_src, "train": "train-issue-10", "kind": "drop",
             "subject": "symbol", "name": "do_it", "area": "area-api",
             "author": "alice", "pr": 100, "commit": "aaa111",
             "url": "https://github.com/o/r/commit/aaa111",
             "before": "def do_it(): pass", "after": None, "detail": "py function do_it"},
        ]
        # symbol_moves: one move whose endpoints are in train-issue-10's artifacts,
        # one unrelated move
        symbol_moves = {
            "links": [
                {
                    "lang": "py", "subkind": "function", "name": "do_it",
                    "from_path": "src/old.py", "to_path": "src/new.py",
                    "confidence": "medium", "basis": "unique_name",
                    "from": art_sym_src,   # artifact id -> in train_a's deltas
                    "to": art_sym_dst,
                },
                {
                    "lang": "tf", "subkind": "resource", "name": "bucket",
                    "from_path": "infra/old.tf", "to_path": "infra/new.tf",
                    "confidence": "high", "basis": "file_rename",
                    "from": "infra/old.tf#tf:resource:bucket",  # unrelated
                    "to": "infra/new.tf#tf:resource:bucket",
                },
            ],
            "by_confidence": {"high": 1, "medium": 1},
        }
        return {
            "issues": [issue_1, issue_2],
            "prs": [pr_1, pr_2, pr_3],
            "commits": commits,
            "trains": [train_a, train_b],
            "feature_deltas": feature_deltas,
            "symbol_moves": symbol_moves,
        }

    # ------------------------------------------------------------------
    # Structure / self-containment
    # ------------------------------------------------------------------

    def test_slice_returns_all_top_level_keys(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        for key in ("train", "issue", "prs", "commits", "feature_deltas", "symbol_moves"):
            self.assertIn(key, s, f"missing top-level key '{key}'")

    def test_train_block_carries_phase4a_fields(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        t = s["train"]
        for field in ("id", "kind", "outcome", "significance", "tier", "effort",
                      "code_areas", "evidence"):
            self.assertIn(field, t, f"train block missing '{field}'")
        self.assertEqual(t["id"], "train-issue-10")
        self.assertEqual(t["significance"], 15.0)
        self.assertEqual(t["tier"], "deep")

    def test_issue_block_resolved_from_bundle(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        issue = s["issue"]
        self.assertIsNotNone(issue)
        self.assertEqual(issue["number"], 10)
        self.assertEqual(issue["title"], "Support feature A")
        self.assertIn("url", issue)
        self.assertIn("labels", issue)
        self.assertIn("kind", issue)

    def test_prs_resolved_and_contain_required_fields(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        self.assertEqual(len(s["prs"]), 2)
        pr_nums = {p["number"] for p in s["prs"]}
        self.assertEqual(pr_nums, {100, 101})
        for pr in s["prs"]:
            for field in ("number", "title", "body", "state", "merged", "created_at",
                          "merged_at", "url", "reviewers", "review_decision"):
                self.assertIn(field, pr, f"PR block missing field '{field}'")

    def test_commits_resolved_with_required_fields(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        self.assertEqual(len(s["commits"]), 2)
        shas = {c["sha"] for c in s["commits"]}
        self.assertEqual(shas, {"aaa111", "aaa222"})
        for c in s["commits"]:
            for field in ("sha", "message", "author", "date"):
                self.assertIn(field, c, f"commit block missing field '{field}'")

    def test_only_trains_own_feature_deltas_included(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        for d in s["feature_deltas"]:
            self.assertEqual(d["train"], "train-issue-10")
        # train-issue-11's delta must not appear
        self.assertTrue(all(d["train"] != "train-issue-11" for d in s["feature_deltas"]))

    # ------------------------------------------------------------------
    # Text truncation
    # ------------------------------------------------------------------

    def test_long_body_is_truncated_to_cap_with_marker(self):
        bundle = self._bundle()
        cap = link.SLICE_TEXT_CAP
        s = link.slice_train(bundle, "train-issue-10")
        pr_100 = next(p for p in s["prs"] if p["number"] == 100)
        body = pr_100["body"]
        # fixture PR body is "x" * 2000; overflow = 2000 - 1500 = 500
        self.assertTrue(body.endswith("…[+500 chars]"),
                        f"expected exact '…[+500 chars]' marker, got: {body[-30]!r}")
        self.assertLess(len(body), 2000)

    def test_short_body_is_unchanged(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        pr_101 = next(p for p in s["prs"] if p["number"] == 101)
        self.assertEqual(pr_101["body"], "pr body 2")

    def test_issue_body_truncated_when_long(self):
        bundle = self._bundle()
        bundle["issues"][0]["body"] = "y" * 2000
        cap = link.SLICE_TEXT_CAP
        s = link.slice_train(bundle, "train-issue-10")
        self.assertLessEqual(len(s["issue"]["body"]), cap + 50)

    def test_commit_message_truncated_when_long(self):
        """Commit messages over SLICE_TEXT_CAP are truncated with the exact marker."""
        bundle = self._bundle()
        # Override aaa111's message with a long one: 2000 chars -> overflow = 500
        bundle["commits"][0]["message"] = "z" * 2000
        s = link.slice_train(bundle, "train-issue-10")
        commit = next(c for c in s["commits"] if c["sha"] == "aaa111")
        self.assertTrue(
            commit["message"].endswith("…[+500 chars]"),
            f"expected '…[+500 chars]' marker, got tail: {commit['message'][-30:]!r}",
        )
        self.assertLess(len(commit["message"]), 2000)

    # ------------------------------------------------------------------
    # Comment overflow
    # ------------------------------------------------------------------

    def test_issue_comments_overflow_when_above_cap(self):
        bundle = self._bundle()
        cap = link.SLICE_COMMENTS_KEPT
        s = link.slice_train(bundle, "train-issue-10")
        issue = s["issue"]
        # fixture has 8 comments; cap is 6
        self.assertIn("comments", issue)
        self.assertIn("comments_overflow", issue)
        self.assertEqual(len(issue["comments"]), cap)
        self.assertEqual(issue["comments_overflow"], 8 - cap)

    def test_pr_review_comments_overflow(self):
        bundle = self._bundle()
        cap = link.SLICE_COMMENTS_KEPT
        s = link.slice_train(bundle, "train-issue-10")
        pr_100 = next(p for p in s["prs"] if p["number"] == 100)
        # fixture has 7 review comments -> overflow
        self.assertEqual(len(pr_100["review_comments"]), cap)
        self.assertEqual(pr_100["review_comments_overflow"], 7 - cap)

    def test_pr_conversation_comments_no_overflow_when_under_cap(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        pr_100 = next(p for p in s["prs"] if p["number"] == 100)
        # fixture has 3 conv comments; cap is 6 -> no overflow
        self.assertEqual(len(pr_100["comments"]), 3)
        self.assertEqual(pr_100["comments_overflow"], 0)

    def test_comment_bodies_are_strings_not_dicts(self):
        """Comment lists emit just the body text, not the full comment object."""
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        for body in s["issue"]["comments"]:
            self.assertIsInstance(body, str)
        pr_100 = next(p for p in s["prs"] if p["number"] == 100)
        for body in pr_100["review_comments"]:
            self.assertIsInstance(body, str)

    # ------------------------------------------------------------------
    # feature_deltas filtered correctly
    # ------------------------------------------------------------------

    def test_feature_deltas_count_correct(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        # fixture: 3 deltas for train-issue-10 (art_a, art_b, art_sym_src)
        self.assertEqual(len(s["feature_deltas"]), 3)

    def test_other_train_delta_excluded(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        artifacts = {d["artifact"] for d in s["feature_deltas"]}
        self.assertNotIn("art:docs/guide.md", artifacts)

    # ------------------------------------------------------------------
    # symbol_moves filtered to train's artifacts
    # ------------------------------------------------------------------

    def test_symbol_moves_filtered_to_train_artifacts(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        # Only the py:function:do_it move (from=art_sym_src) should appear
        self.assertEqual(len(s["symbol_moves"]), 1)
        move = s["symbol_moves"][0]
        self.assertEqual(move["name"], "do_it")

    def test_unrelated_symbol_move_excluded(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-10")
        names = [m["name"] for m in s["symbol_moves"]]
        self.assertNotIn("bucket", names)

    # ------------------------------------------------------------------
    # PR-only train (root_issue=None) -> issue is None
    # ------------------------------------------------------------------

    def test_pr_only_train_has_null_issue(self):
        bundle = self._bundle()
        # Directly build a PR-only train
        bundle["trains"].append({
            "id": "train-pr-999",
            "kind": "other",
            "root_issue": None,
            "prs": [],
            "commits": [],
            "code_areas": [],
            "outcome": "shipped",
            "evidence": [],
            "significance": 1.0,
            "tier": "mention",
            "effort": {},
        })
        s = link.slice_train(bundle, "train-pr-999")
        self.assertIsNone(s["issue"])

    # ------------------------------------------------------------------
    # Unknown train_id raises
    # ------------------------------------------------------------------

    def test_unknown_train_id_raises(self):
        bundle = self._bundle()
        with self.assertRaises((KeyError, ValueError)):
            link.slice_train(bundle, "train-issue-99999")

    # ------------------------------------------------------------------
    # Does NOT mutate the bundle
    # ------------------------------------------------------------------

    def test_slice_does_not_mutate_bundle(self):
        import copy
        bundle = self._bundle()
        original = copy.deepcopy(bundle)
        link.slice_train(bundle, "train-issue-10")
        # Scalar fields and list lengths of the bundle itself must be unchanged.
        self.assertEqual(bundle["issues"][0]["body"], original["issues"][0]["body"])
        self.assertEqual(len(bundle["feature_deltas"]), len(original["feature_deltas"]))
        # By-reference aliased fields on the train dict must not have been mutated
        # (effort, evidence, code_areas are assigned by reference into the slice).
        train_orig = original["trains"][0]
        train_now = bundle["trains"][0]
        self.assertEqual(train_now["effort"], train_orig["effort"])
        self.assertEqual(train_now["evidence"], train_orig["evidence"])
        self.assertEqual(train_now["code_areas"], train_orig["code_areas"])
        # Delta dicts themselves must be unchanged (they are aliased into own_deltas).
        self.assertEqual(bundle["feature_deltas"], original["feature_deltas"])

    # ------------------------------------------------------------------
    # slice for the other train (train-issue-11)
    # ------------------------------------------------------------------

    def test_slice_train_b_has_correct_prs_and_commits(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-11")
        self.assertEqual(len(s["prs"]), 1)
        self.assertEqual(s["prs"][0]["number"], 200)
        self.assertEqual(len(s["commits"]), 1)
        self.assertEqual(s["commits"][0]["sha"], "bbb111")
        self.assertEqual(len(s["feature_deltas"]), 1)
        self.assertEqual(s["feature_deltas"][0]["artifact"], "art:docs/guide.md")

    def test_slice_train_b_symbol_moves_empty_when_no_artifacts_match(self):
        bundle = self._bundle()
        s = link.slice_train(bundle, "train-issue-11")
        # train-issue-11's deltas only reference art:docs/guide.md -> no symbol move endpoint matches
        self.assertEqual(s["symbol_moves"], [])


class TestBuildForecast(unittest.TestCase):
    """Phase 4a: next-release forecast over next_candidates."""

    # ------------------------------------------------------------------
    # Shared fixture builder
    # ------------------------------------------------------------------

    def _bundle(self, next_candidates=None, milestones=None, prs=None, issues=None,
                period=None, ref_date=None):
        """In-memory bundle with meta, milestones, prs, issues, and buckets."""
        return {
            "meta": {
                "period": period or {"from": "2026-05-01", "to": "2026-05-31"},
                "ref_date": ref_date or "2026-05-31",
            },
            "milestones": milestones or [],
            "prs": prs or [],
            "issues": issues or [],
            "buckets": {
                "shipped": [],
                "rejected": [],
                "next_candidates": next_candidates or [],
                "in_flight": [],
            },
            "trains": [],
        }

    def _open_ms(self, title, due_on, number):
        return {"title": title, "state": "open", "due_on": due_on, "number": number}

    def _issue(self, number, title="Issue", state="open", labels=None,
               milestone=None, created_at="2026-01-01T00:00:00Z",
               updated_at="2026-04-01T00:00:00Z", url=None,
               closes=None, crossref_issues=None):
        return {
            "number": number,
            "title": title,
            "state": state,
            "labels": labels or [],
            "milestone": milestone,
            "created_at": created_at,
            "updated_at": updated_at,
            "url": url or f"https://github.com/o/r/issues/{number}",
        }

    def _pr(self, number, title="PR", state="open", merged=False, labels=None,
            milestone=None, created_at="2026-05-01T00:00:00Z",
            updated_at="2026-04-01T00:00:00Z", url=None,
            closes=None, crossref_issues=None):
        return {
            "number": number,
            "title": title,
            "state": state,
            "merged": merged,
            "labels": labels or [],
            "milestone": milestone,
            "created_at": created_at,
            "updated_at": updated_at,
            "url": url or f"https://github.com/o/r/pull/{number}",
            "closes": closes or [],
            "crossref_issues": crossref_issues or [],
        }

    def _nc_ref(self, type_, id_, url=None, train=None):
        """Build a next_candidate ref (possibly with train key)."""
        r = {"type": type_, "id": id_,
             "url": url or f"https://github.com/o/r/{type_}/{id_}"}
        if train is not None:
            r["train"] = train
        return r

    # ------------------------------------------------------------------
    # Structure: one candidate per next_candidate
    # ------------------------------------------------------------------

    def test_candidates_one_per_next_candidate(self):
        """Exactly one forecast candidate per next_candidate ref."""
        issues = [
            self._issue(10, milestone="v1.3.0"),
            self._issue(11, milestone="v1.3.0"),
        ]
        ncs = [
            self._nc_ref("issue", 10),
            self._nc_ref("issue", 11),
        ]
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        bundle = self._bundle(next_candidates=ncs, milestones=milestones, issues=issues)
        link.build_forecast(bundle)
        fc = bundle["forecast"]
        self.assertEqual(len(fc["candidates"]), 2)

    def test_empty_next_candidates_yields_empty_candidates_list(self):
        """Empty next_candidates -> empty candidates list, next_milestone still resolved."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        bundle = self._bundle(next_candidates=[], milestones=milestones)
        link.build_forecast(bundle)
        fc = bundle["forecast"]
        self.assertEqual(fc["candidates"], [])
        self.assertEqual(fc["next_milestone"], "v1.3.0")

    def test_train_id_preserved_on_candidate(self):
        """Candidate carries train id from the next_candidate ref when present."""
        issues = [self._issue(20)]
        ncs = [self._nc_ref("issue", 20, train="train-issue-20")]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertEqual(cand["train"], "train-issue-20")

    def test_no_train_on_candidate_when_ref_lacks_it(self):
        """Candidate train is None when the next_candidate ref has no train key."""
        issues = [self._issue(21)]
        ncs = [self._nc_ref("issue", 21)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertIsNone(cand["train"])

    # ------------------------------------------------------------------
    # ref sub-dict: type/id/url copied from next_candidate
    # ------------------------------------------------------------------

    def test_candidate_ref_carries_type_id_url(self):
        """candidate.ref copies type/id/url from the next_candidate ref."""
        url = "https://github.com/o/r/issues/30"
        issues = [self._issue(30, url=url)]
        ncs = [self._nc_ref("issue", 30, url=url)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertEqual(cand["ref"]["type"], "issue")
        self.assertEqual(cand["ref"]["id"], 30)
        self.assertEqual(cand["ref"]["url"], url)

    # ------------------------------------------------------------------
    # next_milestone resolution
    # ------------------------------------------------------------------

    def test_next_milestone_resolved_from_milestones(self):
        """next_milestone = title of the NEXT open milestone after ref_date."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        bundle = self._bundle(milestones=milestones)
        link.build_forecast(bundle)
        self.assertEqual(bundle["forecast"]["next_milestone"], "v1.3.0")

    def test_next_milestone_none_when_no_open_milestones(self):
        """next_milestone is None when there are no open milestones."""
        bundle = self._bundle(milestones=[])
        link.build_forecast(bundle)
        self.assertIsNone(bundle["forecast"]["next_milestone"])

    def test_next_milestone_none_when_only_one_open_milestone(self):
        """next_milestone is None when there is only one open milestone (no 'next')."""
        milestones = [self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1)]
        bundle = self._bundle(milestones=milestones)
        link.build_forecast(bundle)
        self.assertIsNone(bundle["forecast"]["next_milestone"])

    # ------------------------------------------------------------------
    # Signal: on_next_milestone (heavy)
    # ------------------------------------------------------------------

    def test_on_next_milestone_signal_present_and_score_high(self):
        """Issue on the next milestone scores heavily and includes signal text."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        issues = [self._issue(40, milestone="v1.3.0")]
        ncs = [self._nc_ref("issue", 40)]
        bundle = self._bundle(next_candidates=ncs, milestones=milestones, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["on_next_milestone"],
            places=4,
        )
        self.assertTrue(any("milestone" in s for s in cand["signals"]))

    # ------------------------------------------------------------------
    # Signal: high_priority (uses _high_priority helper)
    # ------------------------------------------------------------------

    def test_high_priority_signal_added_for_high_priority_label(self):
        """A 'priority/high' label triggers the high-priority signal."""
        issues = [self._issue(50, labels=["priority/high"])]
        ncs = [self._nc_ref("issue", 50)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertIn("high-priority", cand["signals"])
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["high_priority"],
            places=4,
        )

    # ------------------------------------------------------------------
    # Signal + tier: on_next_milestone alone clears "likely"; combined scores higher
    # ------------------------------------------------------------------

    def test_milestone_plus_high_priority_scores_higher_than_bare(self):
        """Issue on next milestone + high-priority scores more than a bare issue."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        issues = [
            self._issue(60, milestone="v1.3.0", labels=["priority/high"]),
            self._issue(61),
        ]
        ncs = [self._nc_ref("issue", 60), self._nc_ref("issue", 61)]
        bundle = self._bundle(next_candidates=ncs, milestones=milestones, issues=issues)
        link.build_forecast(bundle)
        cands = {c["ref"]["id"]: c for c in bundle["forecast"]["candidates"]}
        self.assertGreater(cands[60]["score"], cands[61]["score"])
        # likely tier requires hitting the high threshold
        self.assertEqual(cands[60]["tier"], "likely")

    def test_bare_issue_no_signals_is_longshot(self):
        """An issue with no signals scores 0 and lands in longshot tier."""
        issues = [self._issue(70)]
        ncs = [self._nc_ref("issue", 70)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(cand["score"], 0.0, places=4)
        self.assertEqual(cand["tier"], "longshot")
        self.assertEqual(cand["signals"], [])

    # ------------------------------------------------------------------
    # Tier bands: likely / possible / longshot
    # ------------------------------------------------------------------

    def test_tier_likely_at_high_score(self):
        """Score >= FORECAST_TIER_LIKELY_THRESHOLD -> likely."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        # on_next_milestone (5.0) + high_priority (3.0) = 8.0 >= likely threshold (5.0)
        issues = [self._issue(80, milestone="v1.3.0", labels=["priority/high"])]
        ncs = [self._nc_ref("issue", 80)]
        bundle = self._bundle(next_candidates=ncs, milestones=milestones, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertEqual(cand["tier"], "likely")

    def test_tier_possible_at_mid_score(self):
        """Score >= FORECAST_TIER_POSSIBLE_THRESHOLD (but < likely) -> possible."""
        # high_priority alone = 3.0 -> possible (between 2.0 and 5.0)
        issues = [self._issue(81, labels=["priority/high"])]
        ncs = [self._nc_ref("issue", 81)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertEqual(cand["tier"], "possible")

    def test_tier_longshot_below_mid_threshold(self):
        """Score < FORECAST_TIER_POSSIBLE_THRESHOLD -> longshot."""
        issues = [self._issue(82)]
        ncs = [self._nc_ref("issue", 82)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertEqual(cand["tier"], "longshot")

    # ------------------------------------------------------------------
    # Signal: in_motion via open PR referencing an issue candidate
    # ------------------------------------------------------------------

    def test_in_motion_via_open_pr_referencing_issue(self):
        """An open PR whose closes list references the issue candidate triggers in_motion."""
        issues = [self._issue(90)]
        prs = [self._pr(900, state="open", merged=False, closes=[90])]
        ncs = [self._nc_ref("issue", 90)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, prs=prs)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertTrue(any("PR" in s or "work" in s or "motion" in s or "open" in s.lower()
                             for s in cand["signals"]))
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["in_motion"],
            places=4,
        )

    def test_in_motion_via_crossref_issues(self):
        """An open PR with crossref_issues referencing the candidate also triggers in_motion."""
        issues = [self._issue(91)]
        prs = [self._pr(910, state="open", merged=False, crossref_issues=[91])]
        ncs = [self._nc_ref("issue", 91)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, prs=prs)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["in_motion"],
            places=4,
        )

    def test_in_motion_for_pr_candidate(self):
        """A PR next_candidate is itself open -> in_motion signal."""
        prs = [self._pr(920, state="open", merged=False)]
        ncs = [self._nc_ref("pr", 920)]
        bundle = self._bundle(next_candidates=ncs, prs=prs)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["in_motion"],
            places=4,
        )

    def test_in_motion_via_train_id_on_ref(self):
        """A next_candidate ref with a train id triggers in_motion."""
        issues = [self._issue(93)]
        ncs = [self._nc_ref("issue", 93, train="train-issue-93")]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["in_motion"],
            places=4,
        )

    # ------------------------------------------------------------------
    # Signal: recent_activity via updated_at inside vs outside the window
    # ------------------------------------------------------------------

    def test_recent_activity_inside_window(self):
        """updated_at inside the period window triggers recent_activity signal."""
        issues = [self._issue(100, updated_at="2026-05-15T00:00:00Z")]
        ncs = [self._nc_ref("issue", 100)]
        bundle = self._bundle(
            next_candidates=ncs, issues=issues,
            period={"from": "2026-05-01", "to": "2026-05-31"},
        )
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertTrue(any("active" in s or "window" in s for s in cand["signals"]))
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["recent_activity"],
            places=4,
        )

    def test_no_recent_activity_outside_window(self):
        """updated_at outside the period window does NOT trigger recent_activity signal."""
        issues = [self._issue(101, updated_at="2026-03-01T00:00:00Z")]
        ncs = [self._nc_ref("issue", 101)]
        bundle = self._bundle(
            next_candidates=ncs, issues=issues,
            period={"from": "2026-05-01", "to": "2026-05-31"},
        )
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertAlmostEqual(cand["score"], 0.0, places=4)

    # ------------------------------------------------------------------
    # Signal: overdue (age >= FORECAST_OVERDUE_DAYS)
    # ------------------------------------------------------------------

    def test_overdue_signal_for_old_issue(self):
        """An issue older than FORECAST_OVERDUE_DAYS triggers the overdue signal."""
        # Use a created_at far enough in the past (definitely > 90 days before ref_date)
        issues = [self._issue(110, created_at="2025-01-01T00:00:00Z")]
        ncs = [self._nc_ref("issue", 110)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, ref_date="2026-05-31")
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertTrue(any("long" in s or "overdue" in s for s in cand["signals"]))
        self.assertAlmostEqual(
            cand["score"],
            link.FORECAST_WEIGHTS["overdue"],
            places=4,
        )

    def test_no_overdue_for_new_issue(self):
        """A recently created issue does NOT trigger the overdue signal."""
        issues = [self._issue(111, created_at="2026-05-20T00:00:00Z")]
        ncs = [self._nc_ref("issue", 111)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, ref_date="2026-05-31")
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertNotIn("long-open", cand["signals"])

    def test_overdue_fires_just_above_threshold(self):
        """Issue created just over FORECAST_OVERDUE_DAYS ago triggers overdue (boundary +1)."""
        # ref_date = 2026-05-31; FORECAST_OVERDUE_DAYS = 200.
        # 2026-05-31 - 201 days = 2025-11-11; age_days = 201 -> fires.
        issues = [self._issue(112, created_at="2025-11-11T00:00:00Z")]
        ncs = [self._nc_ref("issue", 112)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, ref_date="2026-05-31")
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertIn("long-open", cand["signals"])

    def test_overdue_does_not_fire_just_below_threshold(self):
        """Issue created just under FORECAST_OVERDUE_DAYS ago does NOT trigger overdue (boundary -1)."""
        # ref_date = 2026-05-31; FORECAST_OVERDUE_DAYS = 200.
        # 2026-05-31 - 199 days = 2025-11-13; age_days = 199 -> does not fire.
        issues = [self._issue(113, created_at="2025-11-13T00:00:00Z")]
        ncs = [self._nc_ref("issue", 113)]
        bundle = self._bundle(next_candidates=ncs, issues=issues, ref_date="2026-05-31")
        link.build_forecast(bundle)
        cand = bundle["forecast"]["candidates"][0]
        self.assertNotIn("long-open", cand["signals"])

    # ------------------------------------------------------------------
    # Sorting: score descending, ref id ascending for ties
    # ------------------------------------------------------------------

    def test_sorted_by_score_descending(self):
        """Higher-scored candidates appear first."""
        milestones = [
            self._open_ms("v1.2.0", "2026-05-31T00:00:00Z", 1),
            self._open_ms("v1.3.0", "2026-06-30T00:00:00Z", 2),
        ]
        issues = [
            self._issue(120, milestone="v1.3.0"),  # on next milestone -> high score
            self._issue(121),                       # no signals -> score 0
        ]
        ncs = [self._nc_ref("issue", 121), self._nc_ref("issue", 120)]
        bundle = self._bundle(next_candidates=ncs, milestones=milestones, issues=issues)
        link.build_forecast(bundle)
        cands = bundle["forecast"]["candidates"]
        self.assertEqual(cands[0]["ref"]["id"], 120)
        self.assertEqual(cands[1]["ref"]["id"], 121)

    def test_deterministic_order_on_score_tie(self):
        """When scores tie, lower ref id appears first."""
        issues = [self._issue(130), self._issue(131)]
        ncs = [self._nc_ref("issue", 131), self._nc_ref("issue", 130)]
        bundle = self._bundle(next_candidates=ncs, issues=issues)
        link.build_forecast(bundle)
        cands = bundle["forecast"]["candidates"]
        # both score 0 -> id-ascending order: 130 before 131
        self.assertEqual(cands[0]["ref"]["id"], 130)
        self.assertEqual(cands[1]["ref"]["id"], 131)

    # ------------------------------------------------------------------
    # Wired into enrich()
    # ------------------------------------------------------------------

    def test_enrich_sets_forecast_key(self):
        """enrich() must produce bundle['forecast'] with the required shape."""
        with open(os.path.join(FIX, "bundle_p2.json")) as fh:
            bundle = link.enrich(json.load(fh))
        self.assertIn("forecast", bundle)
        fc = bundle["forecast"]
        self.assertIn("next_milestone", fc)
        self.assertIn("candidates", fc)
        self.assertIsInstance(fc["candidates"], list)
        for cand in fc["candidates"]:
            for key in ("ref", "train", "score", "tier", "signals"):
                self.assertIn(key, cand, f"candidate missing '{key}'")
            self.assertIn(cand["tier"], {"likely", "possible", "longshot"})
            self.assertIsInstance(cand["score"], float)
            self.assertIsInstance(cand["signals"], list)

    # ------------------------------------------------------------------
    # Module-level constants exist
    # ------------------------------------------------------------------

    def test_module_constants_exist(self):
        """FORECAST_WEIGHTS, FORECAST_TIER_LIKELY_THRESHOLD,
        FORECAST_TIER_POSSIBLE_THRESHOLD, and FORECAST_OVERDUE_DAYS are defined."""
        self.assertTrue(hasattr(link, "FORECAST_WEIGHTS"))
        self.assertTrue(hasattr(link, "FORECAST_TIER_LIKELY_THRESHOLD"))
        self.assertTrue(hasattr(link, "FORECAST_TIER_POSSIBLE_THRESHOLD"))
        self.assertTrue(hasattr(link, "FORECAST_OVERDUE_DAYS"))
        w = link.FORECAST_WEIGHTS
        for key in ("on_next_milestone", "high_priority", "in_motion",
                    "recent_activity", "overdue"):
            self.assertIn(key, w, f"FORECAST_WEIGHTS missing '{key}'")

    # ------------------------------------------------------------------
    # Review regressions: window from meta.from/to; in_motion needs open PR
    # ------------------------------------------------------------------

    def test_recent_activity_windows_when_meta_has_from_to_not_period(self):
        """A bundle whose meta carries `from`/`to` (no `period` key) must still
        window recent_activity — an item updated OUTSIDE the window must NOT
        score recent_activity, and one inside MUST. Guards against _in_window
        treating every item as in-window when `period` is absent."""
        b_out = self._bundle(
            next_candidates=[self._nc_ref("issue", 1)],
            issues=[self._issue(1, updated_at="2026-04-01T00:00:00Z")],  # before window
        )
        b_out["meta"] = {"from": "2026-05-01", "to": "2026-05-31",
                         "ref_date": "2026-05-31"}  # no "period"
        link.build_forecast(b_out)
        self.assertNotIn("active in window",
                         b_out["forecast"]["candidates"][0]["signals"])

        b_in = self._bundle(
            next_candidates=[self._nc_ref("issue", 1)],
            issues=[self._issue(1, updated_at="2026-05-15T00:00:00Z")],  # inside window
        )
        b_in["meta"] = {"from": "2026-05-01", "to": "2026-05-31",
                        "ref_date": "2026-05-31"}  # no "period"
        link.build_forecast(b_in)
        self.assertIn("active in window",
                      b_in["forecast"]["candidates"][0]["signals"])

    def test_in_motion_not_awarded_to_merged_or_missing_pr_candidate(self):
        """in_motion (and its weight) must only fire for an OPEN PR candidate.
        A merged/closed PR candidate, or one missing from the bundle's prs,
        must not be awarded the in_motion signal."""
        # Merged PR candidate present in prs -> not in motion.
        merged = self._pr(50, state="closed", merged=True,
                          updated_at="2026-04-01T00:00:00Z")
        b = self._bundle(next_candidates=[self._nc_ref("pr", 50)], prs=[merged])
        link.build_forecast(b)
        self.assertNotIn("open PR", b["forecast"]["candidates"][0]["signals"])

        # PR candidate missing from prs (item == {}) -> not in motion.
        b2 = self._bundle(next_candidates=[self._nc_ref("pr", 99)], prs=[])
        link.build_forecast(b2)
        self.assertNotIn("open PR", b2["forecast"]["candidates"][0]["signals"])

        # Open PR candidate -> in motion.
        b3 = self._bundle(
            next_candidates=[self._nc_ref("pr", 60)],
            prs=[self._pr(60, state="open", merged=False)],
        )
        link.build_forecast(b3)
        self.assertIn("open PR", b3["forecast"]["candidates"][0]["signals"])


if __name__ == "__main__":
    unittest.main()
