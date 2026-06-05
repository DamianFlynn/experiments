import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import graphstore  # noqa: E402
import gather  # noqa: E402
import digest  # noqa: E402


def _seed_two_member_store(conn):
    """Member A (Azure/mod-a): PR #10 merged, closes mod-b#3 (cross-repo, via
    Part 1 fold). Member B (Azure/mod-b): issue #3 closed. Both in window 2026-01."""
    graphstore.init_schema(conn)
    bundle_a = {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [{"number": 10, "url": "uA/10", "state": "closed", "merged": True,
                 "base": "main", "head": "f10",
                 "merged_at": "2026-01-10T00:00:00Z",
                 "created_at": "2026-01-05T00:00:00Z",
                 "closed_at": "2026-01-10T00:00:00Z",
                 "closes": [], "crossref_issues": [],
                 "title": "feat: thing", "body": "Closes Azure/mod-b#3"}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    bundle_b = {
        "meta": {"owner": "Azure", "repo": "mod-b", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [], "issues": [{"number": 3, "url": "uB/3", "state": "closed",
                               "closed_at": "2026-01-08T00:00:00Z",
                               "updated_at": "2026-01-08T00:00:00Z"}],
        "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    members = {"Azure/mod-a", "Azure/mod-b"}
    gather.fold_bundle(conn, bundle_a, project="proj", repo="Azure/mod-a",
                       members=members)
    gather.fold_bundle(conn, bundle_b, project="proj", repo="Azure/mod-b",
                       members=members)


class TestMemberBundles(unittest.TestCase):
    def test_one_enriched_bundle_per_member(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        members = digest.member_bundles(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual([m["repo"] for m in members],
                         ["Azure/mod-a", "Azure/mod-b"])
        self.assertIn("trains", members[0]["bundle"])
        self.assertIn("buckets", members[0]["bundle"])
        self.assertEqual([p["number"] for p in members[0]["bundle"]["prs"]], [10])
        self.assertEqual(members[1]["bundle"]["prs"], [])


class TestSpineComponents(unittest.TestCase):
    def test_cross_repo_edge_unifies_two_members(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        # exactly one component, containing A's PR and B's issue
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0],
                         frozenset({"proj/Azure/mod-a#pr-10",
                                    "proj/Azure/mod-b#issue-3"}))

    def test_unconnected_socials_are_separate_components(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("proj/Azure/a#pr-1", "proj", "Azure/a", "social", "2026-01-01T00:00:00Z", {}, None),
            ("proj/Azure/b#pr-9", "proj", "Azure/b", "social", "2026-01-02T00:00:00Z", {}, None),
        ])
        comps = digest.spine_components(
            conn, "proj", ["Azure/a", "Azure/b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual({frozenset(c) for c in comps},
                         {frozenset({"proj/Azure/a#pr-1"}),
                          frozenset({"proj/Azure/b#pr-9"})})

    def test_three_anchor_chain_is_one_component(self):
        # PR#1 and PR#3 both close issue#2 -> issue-2 bridges them into ONE
        # component of three social anchors. Exercises `seen` dedup (issue-2 and
        # pr-3 are absorbed by pr-1's traversal before they are seen as seeds).
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Azure", "repo": "mod", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [
                {"number": 1, "url": "u/1", "state": "closed", "merged": True,
                 "base": "main", "head": "h1", "merged_at": "2026-01-05T00:00:00Z",
                 "created_at": "2026-01-02T00:00:00Z",
                 "closed_at": "2026-01-05T00:00:00Z",
                 "closes": [2], "crossref_issues": [], "title": "feat", "body": ""},
                {"number": 3, "url": "u/3", "state": "closed", "merged": True,
                 "base": "main", "head": "h3", "merged_at": "2026-01-06T00:00:00Z",
                 "created_at": "2026-01-03T00:00:00Z",
                 "closed_at": "2026-01-06T00:00:00Z",
                 "closes": [2], "crossref_issues": [], "title": "feat", "body": ""},
            ],
            "issues": [{"number": 2, "url": "u/2", "state": "closed",
                        "closed_at": "2026-01-04T00:00:00Z",
                        "updated_at": "2026-01-04T00:00:00Z"}],
            "commits": [], "code_events": [], "milestones": [], "releases": [],
            "code_graph": {"areas": []},
        }
        gather.fold_bundle(conn, bundle, project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0], frozenset({
            "proj/Azure/mod#pr-1", "proj/Azure/mod#issue-2", "proj/Azure/mod#pr-3"}))

    def test_two_separate_multi_node_components_are_isolated(self):
        # Two independent PR->issue pairs -> two disjoint 2-node components.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        def _pair(pr_n, iss_n):
            return {
                "meta": {"owner": "Azure", "repo": "mod", "from": "2026-01-01",
                         "to": "2026-01-31", "base_branch": "main"},
                "prs": [{"number": pr_n, "url": "u/{}".format(pr_n),
                         "state": "closed", "merged": True, "base": "main",
                         "head": "h{}".format(pr_n),
                         "merged_at": "2026-01-05T00:00:00Z",
                         "created_at": "2026-01-02T00:00:00Z",
                         "closed_at": "2026-01-05T00:00:00Z",
                         "closes": [iss_n], "crossref_issues": [],
                         "title": "feat", "body": ""}],
                "issues": [{"number": iss_n, "url": "u/i{}".format(iss_n),
                            "state": "closed", "closed_at": "2026-01-04T00:00:00Z",
                            "updated_at": "2026-01-04T00:00:00Z"}],
                "commits": [], "code_events": [], "milestones": [],
                "releases": [], "code_graph": {"areas": []},
            }
        gather.fold_bundle(conn, _pair(1, 2), project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        gather.fold_bundle(conn, _pair(5, 6), project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual({frozenset(c) for c in comps}, {
            frozenset({"proj/Azure/mod#pr-1", "proj/Azure/mod#issue-2"}),
            frozenset({"proj/Azure/mod#pr-5", "proj/Azure/mod#issue-6"})})


class TestBuildProjectTrains(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        _seed_two_member_store(self.conn)
        self.frm, self.to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        self.members = digest.member_bundles(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)
        self.comps = digest.spine_components(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)

    def test_cross_repo_train_is_single_and_spans_repos(self):
        trains = digest.build_project_trains(self.members, self.comps, "proj")
        self.assertEqual(len(trains), 1)
        t = trains[0]
        self.assertEqual(set(t["repos"]), {"Azure/mod-a", "Azure/mod-b"})
        self.assertIn("proj/Azure/mod-a#pr-10", t["prs"])
        self.assertIn("proj/Azure/mod-b#issue-3", t["issues"])
        self.assertEqual(t["outcome"], "shipped")

    def test_single_repo_train_preserved_with_qualified_ids(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = {"meta": {"owner": "Azure", "repo": "solo", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [{"number": 7, "url": "u/7", "state": "closed", "merged": True,
                      "base": "main", "head": "h7",
                      "merged_at": "2026-01-09T00:00:00Z",
                      "created_at": "2026-01-02T00:00:00Z",
                      "closed_at": "2026-01-09T00:00:00Z",
                      "closes": [], "crossref_issues": [], "title": "fix: x",
                      "body": ""}],
             "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        gather.fold_bundle(conn, b, project="proj", repo="Azure/solo",
                           members={"Azure/solo"})
        frm, to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        members = digest.member_bundles(conn, "proj", ["Azure/solo"], frm, to)
        comps = digest.spine_components(conn, "proj", ["Azure/solo"], frm, to)
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["repos"], ["Azure/solo"])
        self.assertEqual(trains[0]["prs"], ["proj/Azure/solo#pr-7"])


class TestBuildProjectTrainsUnit(unittest.TestCase):
    def _train(self, tid, *, root_issue=None, prs=(), commits=(),
               outcome="shipped", kind="feature", evidence=()):
        return {"id": tid, "kind": kind, "root_issue": root_issue,
                "prs": list(prs), "commits": list(commits), "outcome": outcome,
                "evidence": list(evidence)}

    def test_outcome_precedence_across_members(self):
        # one component spanning two members; shipped must win over rejected.
        members = [
            {"repo": "Azure/a",
             "bundle": {"trains": [self._train("train-pr-1", prs=[1],
                                               outcome="rejected", kind="bug")]}},
            {"repo": "Azure/b",
             "bundle": {"trains": [self._train("train-pr-2", prs=[2],
                                               outcome="shipped", kind="feature")]}},
        ]
        comps = [frozenset({"proj/Azure/a#pr-1", "proj/Azure/b#pr-2"})]
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["outcome"], "shipped")
        self.assertEqual(sorted(trains[0]["repos"]), ["Azure/a", "Azure/b"])

    def test_evidence_is_repo_tagged(self):
        members = [{"repo": "Azure/a", "bundle": {"trains": [
            self._train("train-pr-1", prs=[1],
                        evidence=[{"type": "pr", "id": 1, "url": "u/1"}])]}}]
        comps = [frozenset({"proj/Azure/a#pr-1"})]
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(trains[0]["evidence"],
                         [{"type": "pr", "id": 1, "url": "u/1", "repo": "Azure/a"}])

    def test_orphan_component_node_is_folded(self):
        # component contains a cross-repo issue with NO member train -> it must
        # still appear in issues + its repo in repos.
        members = [{"repo": "Azure/a", "bundle": {"trains": [
            self._train("train-pr-1", prs=[1])]}}]
        comps = [frozenset({"proj/Azure/a#pr-1", "proj/Azure/b#issue-9"})]
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(len(trains), 1)
        self.assertIn("proj/Azure/b#issue-9", trains[0]["issues"])
        self.assertIn("Azure/b", trains[0]["repos"])

    def test_kind_falls_back_to_min_anchor_when_no_root_issue(self):
        members = [{"repo": "Azure/a", "bundle": {"trains": [
            self._train("train-pr-1", prs=[1], kind="bug", outcome="shipped")]}}]
        comps = [frozenset({"proj/Azure/a#pr-1"})]
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(trains[0]["kind"], "bug")


class TestTicketGrouping(unittest.TestCase):
    def test_parse_ticket_refs_default_pattern(self):
        self.assertEqual(
            digest.parse_ticket_refs("see ABC-1234 and ABC-1234 and XY-7, not A1"),
            ["ABC-1234", "XY-7"])  # ordered, deduped; needs >=2 letters then -digits

    def test_parse_ticket_refs_empty(self):
        self.assertEqual(digest.parse_ticket_refs(None), [])

    def test_related_work_clusters_trains_sharing_a_ticket(self):
        trains = [
            {"id": "ptrain-a", "tickets": ["ABC-1"]},
            {"id": "ptrain-b", "tickets": ["ABC-1", "ZZ-9"]},
            {"id": "ptrain-c", "tickets": ["QQ-2"]},
        ]
        groups = digest.group_related_work(trains)
        self.assertEqual(groups, [{"ticket": "ABC-1",
                                   "train_ids": ["ptrain-a", "ptrain-b"]}])


class TestBuildProjectView(unittest.TestCase):
    def test_view_spans_members_with_merged_sections(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        view = digest.build_project_view(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual(view["meta"]["project"], "proj")
        self.assertEqual(view["meta"]["repos"], ["Azure/mod-a", "Azure/mod-b"])
        self.assertEqual(len(view["trains"]), 1)
        self.assertEqual(set(view["trains"][0]["repos"]),
                         {"Azure/mod-a", "Azure/mod-b"})
        shipped_repos = {s["repo"] for s in view["shipped"]}
        self.assertIn("Azure/mod-a", shipped_repos)
        self.assertEqual([m["repo"] for m in view["members"]],
                         ["Azure/mod-a", "Azure/mod-b"])

    def test_contributor_in_both_members_appears_once(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo in ("Azure/x", "Azure/y"):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31",
                          "base_branch": "main"},
                 "prs": [{"number": 1, "url": "u", "state": "closed",
                          "merged": True, "base": "main", "head": "h",
                          "merged_at": "2026-01-05T00:00:00Z",
                          "created_at": "2026-01-02T00:00:00Z",
                          "closed_at": "2026-01-05T00:00:00Z",
                          "closes": [], "crossref_issues": [],
                          "title": "feat: a", "body": "", "author": "alice"}],
                 "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo,
                               members={"Azure/x", "Azure/y"})
        view = digest.build_project_view(
            conn, "proj", ["Azure/x", "Azure/y"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertIn("alice", view["people"])
        self.assertEqual(len(view["people"]), 1)


class TestProjectViewTicketIntegration(unittest.TestCase):
    def test_ticket_attached_and_related_work_clustered(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/r1", "Azure/r2"}
        def _b(repo, pr_n, iss_n):
            return {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                             "from": "2026-01-01", "to": "2026-01-31",
                             "base_branch": "main"},
                    "prs": [{"number": pr_n, "url": "u/{}".format(pr_n),
                             "state": "closed", "merged": True, "base": "main",
                             "head": "h{}".format(pr_n),
                             "merged_at": "2026-01-05T00:00:00Z",
                             "created_at": "2026-01-02T00:00:00Z",
                             "closed_at": "2026-01-05T00:00:00Z",
                             "closes": [iss_n], "crossref_issues": [],
                             "title": "feat", "body": "Implements ABC-123"}],
                    "issues": [{"number": iss_n, "url": "u/i{}".format(iss_n),
                                "state": "closed",
                                "closed_at": "2026-01-04T00:00:00Z",
                                "updated_at": "2026-01-04T00:00:00Z"}],
                    "commits": [], "code_events": [], "milestones": [],
                    "releases": [], "code_graph": {"areas": []}}
        gather.fold_bundle(conn, _b("Azure/r1", 1, 2), project="proj",
                           repo="Azure/r1", members=members)
        gather.fold_bundle(conn, _b("Azure/r2", 3, 4), project="proj",
                           repo="Azure/r2", members=members)
        view = digest.build_project_view(
            conn, "proj", ["Azure/r1", "Azure/r2"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        # each train carries the ticket parsed from its PR body
        self.assertTrue(all("ABC-123" in t["tickets"] for t in view["trains"]))
        self.assertEqual(len(view["trains"]), 2)
        # related_work clusters the two unlinked trains by the shared ticket
        self.assertEqual(len(view["related_work"]), 1)
        self.assertEqual(view["related_work"][0]["ticket"], "ABC-123")
        self.assertEqual(len(view["related_work"][0]["train_ids"]), 2)


class TestMergeHelpers(unittest.TestCase):
    def test_merge_modules_repo_qualifies_area_keys(self):
        members = [
            {"repo": "Azure/a", "bundle": {"modules": {"core": {"commits": 1}}}},
            {"repo": "Azure/b", "bundle": {"modules": {"core": {"commits": 2}}}},
        ]
        mods = digest._merge_modules(members)
        self.assertEqual(set(mods), {"Azure/a::core", "Azure/b::core"})
        self.assertEqual(mods["Azure/b::core"], {"commits": 2})

    def test_merge_people_preserves_extra_fields_and_unions(self):
        members = [
            {"repo": "Azure/a", "bundle": {"people": {
                "alice": {"modules": ["m1"], "areas": ["a1"], "is_bot": False,
                          "display_name": "Alice"}}}},
            {"repo": "Azure/b", "bundle": {"people": {
                "alice": {"modules": ["m2"], "areas": ["a1"], "is_bot": False,
                          "display_name": "Alice"}}}},
        ]
        people = digest._merge_people(members)
        self.assertEqual(set(people), {"alice"})
        self.assertEqual(people["alice"]["modules"], ["m1", "m2"])
        self.assertEqual(people["alice"]["areas"], ["a1"])
        self.assertEqual(people["alice"]["display_name"], "Alice")  # preserved
        # source records not mutated
        self.assertEqual(members[0]["bundle"]["people"]["alice"]["modules"], ["m1"])


class TestDigestCli(unittest.TestCase):
    def _run(self, args):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = digest.main(args)
        return rc, buf.getvalue()

    def test_main_emits_project_view_json(self):
        import io
        import tempfile
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store)
            _seed_two_member_store(disk)
            disk.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = digest.main(["--store", store, "--project", "proj",
                                  "--from", "2026-01-01T00:00:00Z",
                                  "--to", "2026-01-31T23:59:59Z"])
        self.assertEqual(rc, 0)
        view = json.loads(buf.getvalue())
        self.assertEqual(view["meta"]["project"], "proj")
        self.assertEqual(len(view["trains"]), 1)

    def test_main_repo_subset(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store); _seed_two_member_store(disk); disk.close()
            rc, out = self._run(["--store", store, "--project", "proj",
                                 "--repo", "Azure/mod-a",
                                 "--from", "2026-01-01T00:00:00Z",
                                 "--to", "2026-01-31T23:59:59Z"])
        self.assertEqual(rc, 0)
        view = json.loads(out)
        self.assertEqual(view["meta"]["repos"], ["Azure/mod-a"])
        self.assertEqual([m["repo"] for m in view["members"]], ["Azure/mod-a"])

    def test_main_unknown_project_is_empty_view(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store); _seed_two_member_store(disk); disk.close()
            rc, out = self._run(["--store", store, "--project", "nope",
                                 "--from", "2026-01-01T00:00:00Z",
                                 "--to", "2026-01-31T23:59:59Z"])
        self.assertEqual(rc, 0)
        view = json.loads(out)
        self.assertEqual(view["meta"]["repos"], [])
        self.assertEqual(view["trains"], [])

    def test_main_custom_ticket_pattern_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store); _seed_two_member_store(disk); disk.close()
            rc, out = self._run(["--store", store, "--project", "proj",
                                 "--from", "2026-01-01T00:00:00Z",
                                 "--to", "2026-01-31T23:59:59Z",
                                 "--ticket-pattern", r"(TASK-\d+)"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["meta"]["project"], "proj")

    def test_main_invalid_ticket_pattern_exits_2(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store); _seed_two_member_store(disk); disk.close()
            import io, contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = digest.main(["--store", store, "--project", "proj",
                                  "--from", "2026-01-01T00:00:00Z",
                                  "--to", "2026-01-31T23:59:59Z",
                                  "--ticket-pattern", "((unclosed"])
            self.assertEqual(rc, 2)
            self.assertIn("invalid --ticket-pattern", err.getvalue())


if __name__ == "__main__":
    unittest.main()
