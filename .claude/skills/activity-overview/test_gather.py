import contextlib
import email.message
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestBuildBundle(unittest.TestCase):
    def test_skeleton_has_all_top_level_keys_and_reserved_empties(self):
        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, commits=[{"sha": "abc"}],
                                     prs=[{"number": 42}], issues=[{"number": 17}])

        # supplied data is carried through
        self.assertEqual(bundle["meta"]["owner"], "o")
        self.assertEqual(bundle["commits"], [{"sha": "abc"}])
        self.assertEqual(bundle["prs"], [{"number": 42}])
        self.assertEqual(bundle["issues"], [{"number": 17}])

        # later-phase fields are reserved but empty
        for key in ["timeline", "feature_deltas", "trains", "blockers",
                    "releases", "milestones", "docsRefs", "workflows"]:
            self.assertEqual(bundle[key], [], f"{key} should be reserved empty list")
        for key in ["artifacts", "people", "modules", "code_owners", "flow",
                    "label_taxonomy", "diagrams", "workflow_stats", "halls",
                    "code_graph", "release_train", "sprints", "project"]:
            self.assertEqual(bundle[key], {}, f"{key} should be reserved empty dict")
        self.assertEqual(
            bundle["buckets"],
            {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        )
        self.assertEqual(bundle["meta"]["schema_version"], 1)


class TestParseGitLog(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "git_log_sample.txt")) as fh:
            self.raw = fh.read()

    def test_parses_three_commits_in_order(self):
        commits = gather.parse_git_log(self.raw)
        self.assertEqual(len(commits), 3)
        self.assertEqual(commits[0]["sha"], "a1" * 20)
        self.assertEqual(commits[0]["author"], "Alice")
        self.assertEqual(commits[0]["date"], "2026-05-10")
        self.assertEqual(commits[0]["message"], "Add policy param (#42)")
        self.assertEqual(
            commits[0]["files"],
            ["modules/firewall/main.bicep", "modules/firewall/README.md"],
        )

    def test_merge_commit_has_two_parents_and_no_files(self):
        commits = gather.parse_git_log(self.raw)
        merge = commits[1]
        self.assertEqual(len(merge["parents"]), 2)
        self.assertEqual(merge["files"], [])

    def test_empty_input_yields_no_commits(self):
        self.assertEqual(gather.parse_git_log(""), [])


class TestCloneAndWindow(unittest.TestCase):
    def test_build_clone_cmd_is_bounded_and_partial(self):
        cmd = gather.build_clone_cmd("https://github.com/o/r.git",
                                     "2026-05-01", "/tmp/clone")
        self.assertEqual(cmd[0], "git")
        self.assertIn("clone", cmd)
        self.assertIn("--filter=blob:none", cmd)
        self.assertIn("--shallow-since=2026-05-01", cmd)
        self.assertIn("--no-single-branch", cmd)
        self.assertEqual(cmd[-2:], ["https://github.com/o/r.git", "/tmp/clone"])

    def test_in_window_inclusive_bounds(self):
        self.assertTrue(gather.in_window("2026-05-01", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-31", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-15T08:00:00Z", "2026-05-01", "2026-05-31"))

    def test_in_window_rejects_outside_and_none(self):
        self.assertFalse(gather.in_window("2026-04-30", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window("2026-06-01", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window(None, "2026-05-01", "2026-05-31"))


class TestPrNormalization(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            self.data = json.load(fh)

    def test_parse_closing_refs_all_keywords(self):
        self.assertEqual(gather.parse_closing_refs("Fixes #17"), [17])
        self.assertEqual(
            gather.parse_closing_refs("Resolves #18 and closes #19"), [18, 19]
        )
        self.assertEqual(gather.parse_closing_refs("no references here"), [])
        self.assertEqual(gather.parse_closing_refs(None), [])
        # de-duplicates while preserving order
        self.assertEqual(gather.parse_closing_refs("fix #5 fixed #5"), [5])

    def test_normalize_pr_maps_fields_and_parses_closes(self):
        pr = gather.normalize_pr(self.data["pulls"][0])
        self.assertEqual(pr["number"], 42)
        self.assertEqual(pr["author"], "alice")
        self.assertEqual(pr["author_association"], "MEMBER")
        self.assertTrue(pr["merged"])
        self.assertEqual(pr["merged_by"], "bob")
        self.assertEqual(pr["labels"], ["enhancement"])
        self.assertEqual(pr["closes"], [17])
        self.assertEqual(pr["url"], "https://github.com/o/r/pull/42")

    def test_normalize_pr_unmerged_has_merged_false(self):
        pr = gather.normalize_pr(self.data["pulls"][1])
        self.assertFalse(pr["merged"])
        self.assertIsNone(pr["merged_by"])

    def test_normalize_pr_captures_phase2_fields(self):
        raw = {
            "number": 44, "title": "Still open", "body": "Resolves #18",
            "state": "open", "merged_at": None, "closed_at": None,
            "created_at": "2026-05-03T09:00:00Z", "updated_at": "2026-05-20T09:00:00Z",
            "user": {"login": "alice"}, "author_association": "MEMBER",
            "labels": [{"name": "priority/high"}],
            "milestone": {"title": "v1.3.0"},
            "comments": 4, "review_comments": 2,
            "html_url": "https://github.com/o/r/pull/44",
        }
        pr = gather.normalize_pr(raw)
        self.assertEqual(pr["created_at"], "2026-05-03T09:00:00Z")
        self.assertEqual(pr["updated_at"], "2026-05-20T09:00:00Z")
        self.assertEqual(pr["milestone"], "v1.3.0")
        self.assertEqual(pr["comments"], 4)
        self.assertEqual(pr["review_comments_count"], 2)
        self.assertEqual(pr["state"], "open")
        self.assertFalse(pr["merged"])

    def test_normalize_pr_milestone_none_when_absent(self):
        pr = gather.normalize_pr({"number": 1, "html_url": "u"})
        self.assertIsNone(pr["milestone"])
        self.assertEqual(pr["comments"], 0)

    def test_select_merged_prs_only_in_window(self):
        prs = [gather.normalize_pr(p) for p in self.data["pulls"]]
        merged = gather.select_merged_prs(prs, "2026-05-01", "2026-05-31")
        self.assertEqual([p["number"] for p in merged], [42])


class TestIssueAndFetch(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            self.data = json.load(fh)

    def test_normalize_issue_maps_kind_and_state_reason(self):
        issue = gather.normalize_issue(self.data["issues"]["17"])
        self.assertEqual(issue["number"], 17)
        self.assertEqual(issue["kind"], "other")
        self.assertEqual(issue["author"], "dave")
        self.assertEqual(issue["state_reason"], "completed")
        self.assertEqual(issue["labels"], ["enhancement"])
        self.assertEqual(issue["assignees"], ["alice"])
        self.assertEqual(issue["url"], "https://github.com/o/r/issues/17")

    def test_normalize_issue_captures_phase2_fields(self):
        raw = {
            "number": 18, "title": "Open feature", "body": "",
            "state": "open", "state_reason": None,
            "updated_at": "2026-05-22T00:00:00Z",
            "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
            "labels": [{"name": "priority/high"}],
            "assignees": [{"login": "alice"}],
            "milestone": {"title": "v1.3.0"},
            "comments": 7,
            "html_url": "https://github.com/o/r/issues/18",
        }
        issue = gather.normalize_issue(raw)
        self.assertEqual(issue["milestone"], "v1.3.0")
        self.assertEqual(issue["updated_at"], "2026-05-22T00:00:00Z")
        self.assertEqual(issue["comments"], 7)
        self.assertEqual(issue["state"], "open")

    def test_fetch_all_follows_pages_until_short_page(self):
        pages = {
            "u?page=1": (["a", "b"], "u?page=2"),
            "u?page=2": (["c"], None),
        }
        calls = []

        def fake_get(url):
            calls.append(url)
            body, nxt = pages[url]
            return body, nxt

        items = gather.fetch_all(fake_get, "u?page=1")
        self.assertEqual(items, ["a", "b", "c"])
        self.assertEqual(calls, ["u?page=1", "u?page=2"])

    def test_fetch_until_stops_once_page_falls_before_window(self):
        pages = {
            "u?p=1": ([{"updated_at": "2026-05-20"}, {"updated_at": "2026-05-10"}], "u?p=2"),
            "u?p=2": ([{"updated_at": "2026-04-25"}, {"updated_at": "2026-04-20"}], "u?p=3"),
            "u?p=3": ([{"updated_at": "2026-03-01"}], None),
        }
        calls = []

        def fake_get(url):
            calls.append(url)
            return pages[url]

        items = gather.fetch_until(
            fake_get, "u?p=1",
            lambda page: bool(page) and page[-1]["updated_at"][:10] >= "2026-05-01",
        )
        # Page 2 ends before the window, so it is included but page 3 is never fetched.
        self.assertEqual(len(items), 4)
        self.assertEqual(calls, ["u?p=1", "u?p=2"])


class TestHttpErrorDiagnostics(unittest.TestCase):
    def _http_error(self, code, message, headers):
        hdrs = email.message.Message()
        for k, v in headers.items():
            hdrs[k] = v
        body = io.BytesIO(json.dumps({"message": message}).encode())
        return urllib.error.HTTPError("https://api/x", code, "Forbidden", hdrs, body)

    def test_rate_limited_403_reports_message_and_remaining(self):
        out = gather._format_http_error("https://api/x", self._http_error(
            403, "API rate limit exceeded for 1.2.3.4.",
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1780395758"},
        ))
        self.assertIn("403", out)
        self.assertIn("rate limit exceeded", out)
        self.assertIn("x-ratelimit-remaining: 0", out)

    def test_saml_sso_403_surfaces_the_sso_header(self):
        out = gather._format_http_error("https://api/x", self._http_error(
            403, "Resource protected by organization SAML enforcement.",
            {"x-github-sso": "required; organizations=Azure"},
        ))
        self.assertIn("x-github-sso: required; organizations=Azure", out)
        self.assertIn("SAML SSO", out)


class TestCliAndAuth(unittest.TestCase):
    def test_parse_args_required_and_defaults(self):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r",
            "--from", "2026-05-01", "--to", "2026-05-31",
        ])
        self.assertEqual(args.owner, "o")
        self.assertEqual(args.repo, "r")
        self.assertEqual(getattr(args, "from"), "2026-05-01")
        self.assertEqual(args.to, "2026-05-31")
        self.assertEqual(args.branches, "main")
        self.assertFalse(args.no_clone)

    def test_resolve_token_prefers_github_token(self):
        self.assertEqual(
            gather.resolve_token({"GITHUB_TOKEN": "gh", "GH_TOKEN": "alt"}), "gh"
        )

    def test_resolve_token_falls_back_to_gh_token(self):
        self.assertEqual(gather.resolve_token({"GH_TOKEN": "alt"}), "alt")

    def test_resolve_token_missing_raises(self):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            gather.resolve_token({})


class TestReviewsAndTimeline(unittest.TestCase):
    def test_summarize_reviews_picks_latest_decision_per_reviewer(self):
        raw = [
            {"user": {"login": "bob"}, "state": "COMMENTED",
             "submitted_at": "2026-05-08T10:00:00Z"},
            {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-05-09T10:00:00Z"},
            {"user": {"login": "carol"}, "state": "APPROVED",
             "submitted_at": "2026-05-10T10:00:00Z"},
        ]
        out = gather.summarize_reviews(raw)
        self.assertEqual(sorted(out["reviewers"]), ["bob", "carol"])
        # changes_requested outranks approved when any reviewer still blocks
        self.assertEqual(out["decision"], "changes_requested")

    def test_summarize_reviews_approved_when_all_clear(self):
        raw = [{"user": {"login": "carol"}, "state": "APPROVED",
                "submitted_at": "2026-05-10T10:00:00Z"}]
        self.assertEqual(gather.summarize_reviews(raw)["decision"], "approved")

    def test_summarize_reviews_empty_is_none(self):
        self.assertEqual(gather.summarize_reviews([])["decision"], "none")

    def test_parse_timeline_crossrefs_collects_issue_numbers(self):
        raw = [
            {"event": "cross-referenced",
             "source": {"issue": {"number": 18, "pull_request": None}}},
            {"event": "connected", "subject": {"number": 19}},
            {"event": "labeled"},
            {"event": "cross-referenced",
             "source": {"issue": {"number": 18, "pull_request": None}}},
        ]
        self.assertEqual(gather.parse_timeline_crossrefs(raw), [18, 19])

    def test_parse_timeline_crossrefs_skips_pr_sourced_xref(self):
        raw = [{"event": "cross-referenced",
                "source": {"issue": {"number": 99, "pull_request": {"url": "x"}}}}]
        self.assertEqual(gather.parse_timeline_crossrefs(raw), [])

    def test_parse_timeline_crossrefs_ignores_disconnected(self):
        raw = [
            {"event": "connected", "subject": {"number": 5}},
            {"event": "disconnected", "subject": {"number": 5}},
        ]
        self.assertEqual(gather.parse_timeline_crossrefs(raw), [5])


class TestWorkflowsReleasesMilestones(unittest.TestCase):
    def test_normalize_workflow_maps_fields(self):
        raw = {"name": "CI", "conclusion": "success", "status": "completed",
               "event": "push", "head_branch": "main",
               "created_at": "2026-05-10T00:00:00Z",
               "html_url": "https://github.com/o/r/actions/runs/1"}
        wf = gather.normalize_workflow(raw)
        self.assertEqual(wf["name"], "CI")
        self.assertEqual(wf["conclusion"], "success")
        self.assertEqual(wf["url"], "https://github.com/o/r/actions/runs/1")

    def test_aggregate_workflow_stats_counts_by_conclusion(self):
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CI", "conclusion": "failure"},
            {"name": "CI", "conclusion": "success"},
            {"name": "CI", "conclusion": "cancelled"},
            {"name": "Release", "conclusion": "success"},
            {"name": "CI", "conclusion": "neutral"},
        ]
        stats = gather.aggregate_workflow_stats(runs)
        self.assertEqual(stats["CI"],
                         {"total": 5, "success": 2, "failure": 1,
                          "cancelled": 1, "other": 1})
        self.assertEqual(stats["Release"]["success"], 1)

    def test_normalize_release_and_milestone(self):
        rel = gather.normalize_release({
            "tag_name": "v1.2.0", "name": "1.2.0",
            "published_at": "2026-05-15T00:00:00Z", "prerelease": False,
            "html_url": "https://github.com/o/r/releases/tag/v1.2.0"})
        self.assertEqual(rel["tag_name"], "v1.2.0")
        self.assertFalse(rel["prerelease"])
        ms = gather.normalize_milestone({
            "title": "v1.3.0", "number": 5, "state": "open",
            "due_on": "2026-06-30T00:00:00Z", "open_issues": 3,
            "closed_issues": 7,
            "html_url": "https://github.com/o/r/milestone/5"})
        self.assertEqual(ms["title"], "v1.3.0")
        self.assertEqual(ms["open_issues"], 3)
        self.assertEqual(ms["state"], "open")


class TestAcquireAssemblyP2(unittest.TestCase):
    """Compose the pure helpers over recorded REST, as acquire() does, offline."""

    def _bundle_from_fixture(self):
        with open(os.path.join(FIX, "rest_p2_sample.json")) as fh:
            data = json.load(fh)
        frm, to = data["window"]["from"], data["window"]["to"]
        prs = [gather.normalize_pr(p) for p in data["pulls"]]
        for pr in prs:
            rv = gather.summarize_reviews(data["reviews"].get(str(pr["number"]), []))
            pr["reviewers"] = rv["reviewers"]
            pr["review_decision"] = rv["decision"]
            pr["crossref_issues"] = gather.parse_timeline_crossrefs(
                data["timeline"].get(str(pr["number"]), []))
        issues = [gather.normalize_issue(i) for i in data["issues"].values()]
        workflows = [gather.normalize_workflow(w) for w in data["workflows"]]
        releases = [gather.normalize_release(r) for r in data["releases"]]
        milestones = [gather.normalize_milestone(m) for m in data["milestones"]]
        meta = {"owner": "o", "repo": "r", "from": frm, "to": to,
                "period": {"from": frm, "to": to}, "ref_date": to}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["workflows"] = workflows
        bundle["workflow_stats"] = gather.aggregate_workflow_stats(workflows)
        bundle["releases"] = releases
        bundle["milestones"] = milestones
        return bundle

    def test_bundle_has_social_layer(self):
        b = self._bundle_from_fixture()
        self.assertEqual({p["number"] for p in b["prs"]}, {42, 43, 44})
        pr44 = next(p for p in b["prs"] if p["number"] == 44)
        self.assertEqual(pr44["crossref_issues"], [18])
        pr43 = next(p for p in b["prs"] if p["number"] == 43)
        self.assertEqual(pr43["review_decision"], "changes_requested")
        self.assertEqual(b["workflow_stats"]["CI"]["total"], 3)
        self.assertEqual(b["releases"][0]["tag_name"], "v1.2.0")
        self.assertEqual(len(b["milestones"]), 3)


if __name__ == "__main__":
    unittest.main()
