import contextlib
import email.message
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402

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
        # shallow-since reaches CLONE_MARGIN_DAYS (14) BEFORE the window start so the
        # grafted boundary commit's whole-tree phantom diff falls outside the window.
        self.assertIn("--shallow-since=2026-04-17", cmd)
        self.assertIn("--no-single-branch", cmd)
        self.assertEqual(cmd[-2:], ["https://github.com/o/r.git", "/tmp/clone"])

    def test_shift_date(self):
        self.assertEqual(gather._shift_date("2026-05-01", -14), "2026-04-17")
        self.assertEqual(gather._shift_date("bad", -14), "bad")  # parse-error fallback

    def test_shallow_boundary_and_drop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".git"))
            with open(os.path.join(d, ".git", "shallow"), "w") as fh:
                fh.write("aaaa\nbbbb\n")
            self.assertEqual(gather.shallow_boundary_shas(d), {"aaaa", "bbbb"})
        # absent file -> empty set; drop filters by commit; empty boundary is a no-op
        self.assertEqual(gather.shallow_boundary_shas("/no/such/clone"), set())
        evs = [{"commit": "aaaa", "x": 1}, {"commit": "cccc", "x": 2}]
        self.assertEqual(gather.drop_boundary_events(evs, {"aaaa"}),
                         [{"commit": "cccc", "x": 2}])
        self.assertEqual(gather.drop_boundary_events(evs, set()), evs)

    def test_in_window_boundary_commits(self):
        commits = [{"sha": "aaaa"}, {"sha": "dddd"}]
        # boundary "aaaa" is also an in-window commit -> reported (visible gap);
        # "bbbb" is a pre-window boundary -> not reported.
        self.assertEqual(
            gather.in_window_boundary_commits({"aaaa", "bbbb"}, commits), ["aaaa"])
        self.assertEqual(gather.in_window_boundary_commits(set(), commits), [])

    def test_in_window_inclusive_bounds(self):
        self.assertTrue(gather.in_window("2026-05-01", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-31", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-15T08:00:00Z", "2026-05-01", "2026-05-31"))

    def test_in_window_rejects_outside_and_none(self):
        self.assertFalse(gather.in_window("2026-04-30", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window("2026-06-01", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window(None, "2026-05-01", "2026-05-31"))

    def test_window_records_filters_by_date_inclusive(self):
        recs = [{"date": "2026-05-10", "commit": "a"},
                {"date": "2026-05-25", "commit": "b"},
                {"date": "2026-06-01", "commit": "c"},
                {"date": "2026-06-02", "commit": "d"},
                {"date": "", "commit": "e"}]
        out = gather.window_records(recs, "2026-05-25", "2026-06-01")
        self.assertEqual([r["commit"] for r in out], ["b", "c"])

    def test_window_records_preserves_order(self):
        recs = [{"date": "2026-05-26"}, {"date": "2026-05-25"}, {"date": "2026-05-27"}]
        self.assertEqual([r["date"] for r in gather.window_records(
            recs, "2026-05-25", "2026-05-31")],
            ["2026-05-26", "2026-05-25", "2026-05-27"])


class TestPrNormalization(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            self.data = json.load(fh)

    def test_normalize_captures_base_and_head_refs(self):
        raw = {"number": 5, "base": {"ref": "users/x/feat"},
               "head": {"ref": "fix-branch"}}
        pr = gather.normalize_pr(raw)
        self.assertEqual(pr["base"], "users/x/feat")
        self.assertEqual(pr["head"], "fix-branch")

    def test_normalize_missing_base_head_is_none(self):
        pr = gather.normalize_pr({"number": 6})
        self.assertIsNone(pr["base"])
        self.assertIsNone(pr["head"])

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
            "--store", "workspace/store.db",
        ])
        self.assertEqual(args.owner, "o")
        self.assertEqual(args.repo, "r")
        self.assertEqual(getattr(args, "from"), "2026-05-01")
        self.assertEqual(args.to, "2026-05-31")
        self.assertEqual(args.branches, "main")
        self.assertFalse(args.no_clone)
        self.assertEqual(args.store, "workspace/store.db")

    def test_parse_args_store_is_required(self):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            gather.parse_args(["--owner", "o", "--repo", "r",
                               "--from", "2026-05-01", "--to", "2026-05-31"])

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


class TestCommentsAndReactions(unittest.TestCase):
    def test_normalize_comment_maps_body_author_url_id(self):
        raw = {
            "id": 9001, "body": "Could you split this into two functions?",
            "user": {"login": "bob"}, "author_association": "MEMBER",
            "html_url": "https://github.com/o/r/issues/17#issuecomment-9001",
        }
        c = gather.normalize_comment(raw)
        self.assertEqual(c["id"], 9001)
        self.assertEqual(c["author"], "bob")
        self.assertEqual(c["author_association"], "MEMBER")
        self.assertEqual(c["body"], "Could you split this into two functions?")
        self.assertEqual(c["url"],
                         "https://github.com/o/r/issues/17#issuecomment-9001")

    def test_normalize_comment_permissive_on_missing_fields(self):
        c = gather.normalize_comment({"id": 1})
        self.assertEqual(c["id"], 1)
        self.assertIsNone(c["author"])
        self.assertEqual(c["body"], "")
        self.assertIsNone(c["url"])
        self.assertIsNone(c["author_association"])

    def test_normalize_review_comment_maps_same_shape(self):
        raw = {
            "id": 7002, "body": "nit: rename `x`.",
            "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
            "html_url": "https://github.com/o/r/pull/42#discussion_r7002",
            "created_at": "2026-05-12T10:00:00Z",
        }
        rc = gather.normalize_review_comment(raw)
        self.assertEqual(rc, {"id": 7002, "author": "carol",
                              "author_association": "CONTRIBUTOR",
                              "body": "nit: rename `x`.",
                              "url": "https://github.com/o/r/pull/42#discussion_r7002",
                              "created_at": "2026-05-12T10:00:00Z"})

    def test_summarize_reactions_picks_the_tracked_keys(self):
        raw = {"+1": 12, "-1": 1, "laugh": 0, "hooray": 3, "confused": 0,
               "heart": 4, "rocket": 2, "eyes": 1, "total_count": 23}
        r = gather.summarize_reactions(raw)
        self.assertEqual(r, {"+1": 12, "-1": 1, "heart": 4, "hooray": 3, "total": 23})

    def test_summarize_reactions_permissive_on_missing(self):
        self.assertEqual(gather.summarize_reactions(None),
                         {"+1": 0, "-1": 0, "heart": 0, "hooray": 0, "total": 0})
        self.assertEqual(gather.summarize_reactions({})["total"], 0)

    def test_summarize_reactions_falls_back_to_summing_when_total_absent(self):
        # GitHub usually sends total_count; if it's missing we sum the tracked keys.
        r = gather.summarize_reactions({"+1": 5, "heart": 2})
        self.assertEqual(r["total"], 7)

    def test_derive_open_high_activity_true_when_open_and_engaged(self):
        issue = {"state": "open", "comments": 7,
                 "reactions": {"+1": 9, "-1": 0, "heart": 0, "hooray": 0, "total": 9}}
        self.assertTrue(gather.derive_open_high_activity(issue))

    def test_derive_open_high_activity_false_when_closed_or_quiet(self):
        self.assertFalse(gather.derive_open_high_activity(
            {"state": "closed", "comments": 50,
             "reactions": {"+1": 99, "total": 99}}))
        self.assertFalse(gather.derive_open_high_activity(
            {"state": "open", "comments": 1,
             "reactions": {"+1": 1, "total": 1}}))


class TestArtifactPathClassifier(unittest.TestCase):
    def test_readme_basename_wins(self):
        self.assertEqual(gather.classify_artifact_path("README.md"), "readme")
        self.assertEqual(gather.classify_artifact_path("modules/x/README"), "readme")
        self.assertEqual(
            gather.classify_artifact_path("docs/README.markdown"), "readme")

    def test_example_dir_or_suffix(self):
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/main.bicep"), "example")
        self.assertEqual(
            gather.classify_artifact_path("modules/x/examples/full/main.tf"), "example")
        self.assertEqual(
            gather.classify_artifact_path("config.example.json"), "example")

    def test_doc_md_or_under_docs(self):
        self.assertEqual(gather.classify_artifact_path("docs/design.md"), "doc")
        self.assertEqual(gather.classify_artifact_path("notes/CHANGELOG.md"), "doc")
        self.assertEqual(gather.classify_artifact_path("docs/spec.txt"), "doc")

    def test_unrecognized_paths_are_none(self):
        self.assertIsNone(gather.classify_artifact_path("modules/x/main.bicep"))
        self.assertIsNone(gather.classify_artifact_path("src/app.py"))
        self.assertIsNone(gather.classify_artifact_path(""))
        self.assertIsNone(gather.classify_artifact_path(None))

    def test_example_segment_not_incidental_substring(self):
        # `.example` must be a dot-segment, not any substring
        self.assertEqual(gather.classify_artifact_path("main.example.json"), "example")
        self.assertEqual(gather.classify_artifact_path("config.example"), "example")
        # "counter-example.md" is a doc, not an example
        self.assertEqual(gather.classify_artifact_path("docs/counter-example.md"), "doc")
        self.assertEqual(gather.classify_artifact_path("notes/counter-example.md"), "doc")

    def test_precedence_readme_over_example_over_doc(self):
        # a README inside an examples dir is still a README (basename wins)
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/README.md"), "readme")
        # an example markdown under examples/ is an example, not a doc
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/notes.md"), "example")


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


class TestParseCodeEvents(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            self.raw = fh.read()

    def test_parses_adds_modifies_deletes(self):
        events = gather.parse_code_events(self.raw)
        adds = [e for e in events if e["change"] == "add"]
        self.assertIn(("examples/basic/main.bicep", "Alice", "2026-05-03"),
                      [(e["path"], e["author"], e["date"]) for e in adds])
        deletes = [e for e in events if e["change"] == "delete"]
        self.assertEqual([e["path"] for e in deletes], ["docs/firewall.md"])
        modifies = [e for e in events if e["change"] == "modify"]
        self.assertIn("README.md", [e["path"] for e in modifies])

    def test_rename_carries_old_and_new_path(self):
        events = gather.parse_code_events(self.raw)
        renames = [e for e in events if e["change"] == "rename"]
        self.assertEqual(len(renames), 1)
        r = renames[0]
        self.assertEqual(r["old_path"], "examples/basic/main.bicep")
        self.assertEqual(r["path"], "examples/advanced/main.bicep")
        self.assertEqual(r["author"], "Carol")

    def test_every_event_carries_commit_author_date(self):
        for e in gather.parse_code_events(self.raw):
            self.assertEqual(len(e["commit"]), 40)
            self.assertTrue(e["author"])
            self.assertEqual(len(e["date"]), 10)
            self.assertIn(e["change"], {"add", "modify", "delete", "rename", "copy"})

    def test_non_rename_events_have_no_old_path_key(self):
        events = gather.parse_code_events(self.raw)
        add = next(e for e in events if e["change"] == "add")
        self.assertNotIn("old_path", add)

    def test_empty_input_yields_no_events(self):
        self.assertEqual(gather.parse_code_events(""), [])

    def test_copy_and_type_change_status_codes(self):
        # C### (copy) -> copy with old+new path; T (type-change) -> modify.
        raw = (
            "\x1e" + "f" * 40 + "\x1f" + "p" * 40 + "\x1fEve\x1f2026-05-20\x1fmix\n"
            "C085\told/x.bicep\tnew/x.bicep\n"
            "T\tlinks/sym.bicep\n"
        )
        events = gather.parse_code_events(raw)
        copy = next(e for e in events if e["change"] == "copy")
        self.assertEqual((copy["old_path"], copy["path"]), ("old/x.bicep", "new/x.bicep"))
        tchg = next(e for e in events if e["path"] == "links/sym.bicep")
        self.assertEqual(tchg["change"], "modify")

    def test_malformed_rename_line_is_skipped(self):
        # A rename status with only one path column is dropped, not mis-parsed.
        raw = ("\x1e" + "f" * 40 + "\x1f\x1fEve\x1f2026-05-20\x1fbad\n"
               "R096\tonly/old/path.bicep\n")
        self.assertEqual(gather.parse_code_events(raw), [])


class TestAcquireAssemblyP3(unittest.TestCase):
    """Compose the Phase 3a helpers over recorded REST + git-log, offline."""

    def _bundle(self):
        with open(os.path.join(FIX, "rest_p2_sample.json")) as fh:
            p2 = json.load(fh)
        with open(os.path.join(FIX, "rest_p3_sample.json")) as fh:
            p3 = json.load(fh)
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            code_events = gather.parse_code_events(fh.read())

        frm, to = p2["window"]["from"], p2["window"]["to"]
        prs = [gather.normalize_pr(p) for p in p2["pulls"]]
        for pr in prs:
            n = str(pr["number"])
            pr["review_comments"] = [gather.normalize_review_comment(c)
                                     for c in p3["pr_review_comments"].get(n, [])]
            pr["comments_list"] = [gather.normalize_comment(c)
                                   for c in p3["pr_comments"].get(n, [])]
        issues = [gather.normalize_issue(i) for i in p2["issues"].values()]
        for issue in issues:
            n = str(issue["number"])
            issue["comments_list"] = [gather.normalize_comment(c)
                                      for c in p3["issue_comments"].get(n, [])]
            issue["reactions"] = gather.summarize_reactions(
                p3["issue_reactions"].get(n))
            issue["open_high_activity"] = gather.derive_open_high_activity(issue)
        meta = {"owner": "o", "repo": "r", "from": frm, "to": to,
                "period": {"from": frm, "to": to}, "ref_date": to}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["code_events"] = code_events
        return bundle

    def test_pr_carries_review_comment_bodies(self):
        b = self._bundle()
        pr42 = next(p for p in b["prs"] if p["number"] == 42)
        self.assertEqual(pr42["review_comments"][0]["body"],
                         "Inline: extract this branch.")
        self.assertEqual(pr42["review_comments"][0]["author"], "bob")
        # Phase 2's integer counts are preserved alongside the new arrays.
        self.assertEqual(pr42["review_comments_count"], 1)
        self.assertEqual(pr42["comments_list"][0]["body"],
                         "LGTM once the example is added.")

    def test_issue_carries_comments_reactions_and_signal(self):
        b = self._bundle()
        issue18 = next(i for i in b["issues"] if i["number"] == 18)
        self.assertEqual(len(issue18["comments_list"]), 2)
        self.assertEqual(issue18["reactions"]["+1"], 9)
        self.assertEqual(issue18["reactions"]["total"], 12)
        self.assertTrue(issue18["open_high_activity"])  # open + 9 upvotes
        issue21 = next(i for i in b["issues"] if i["number"] == 21)
        self.assertFalse(issue21["open_high_activity"])  # open but quiet

    def test_code_events_present_on_bundle(self):
        b = self._bundle()
        kinds = {e["change"] for e in b["code_events"]}
        self.assertEqual(kinds, {"add", "modify", "delete", "rename"})


class TestDirectoryCodeAreaProvider(unittest.TestCase):
    def test_avm_module_dir_is_the_four_segment_subtree(self):
        # AVM: avm/res/<service>/<module>/...  -> area = that 4-seg dir
        self.assertEqual(
            gather.classify_code_area(
                "avm/res/network/firewall-policy/main.bicep",
                gather.DEFAULT_AREA_PATTERNS),
            "avm/res/network/firewall-policy")
        self.assertEqual(
            gather.classify_code_area(
                "avm/res/network/firewall-policy/tests/e2e/main.test.bicep",
                gather.DEFAULT_AREA_PATTERNS),
            "avm/res/network/firewall-policy")

    def test_dir_containing_main_bicep_is_an_area(self):
        # Any directory that holds a main.bicep is a module root.
        paths = ["modules/keyvault/main.bicep", "modules/keyvault/README.md"]
        areas = gather.build_directory_areas(paths, gather.DEFAULT_AREA_PATTERNS)
        ids = {a["id"] for a in areas["areas"]}
        self.assertIn("modules/keyvault", ids)

    def test_terraform_modules_and_tf_dirs(self):
        self.assertEqual(
            gather.classify_code_area("modules/vnet/main.tf",
                                      gather.DEFAULT_AREA_PATTERNS),
            "modules/vnet")
        # any dir containing *.tf becomes that dir
        self.assertEqual(
            gather.classify_code_area("infra/network/variables.tf",
                                      gather.DEFAULT_AREA_PATTERNS),
            "infra/network")

    def test_generic_fallback_is_top_two_segments(self):
        self.assertEqual(
            gather.classify_code_area("src/app/handlers/auth.py",
                                      gather.DEFAULT_AREA_PATTERNS),
            "src/app")
        # a top-level file falls back to its own segment
        self.assertEqual(
            gather.classify_code_area("README.md", gather.DEFAULT_AREA_PATTERNS),
            "README.md")

    def test_build_directory_areas_groups_paths_and_shapes_provider(self):
        paths = [
            "avm/res/network/firewall-policy/main.bicep",
            "avm/res/network/firewall-policy/README.md",
            "avm/res/storage/account/main.bicep",
            "src/app/handlers/auth.py",
        ]
        cg = gather.build_directory_areas(paths, gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(cg["provider"], "directory")
        by_id = {a["id"]: a for a in cg["areas"]}
        self.assertEqual(
            sorted(by_id["avm/res/network/firewall-policy"]["paths"]),
            ["avm/res/network/firewall-policy/README.md",
             "avm/res/network/firewall-policy/main.bicep"])
        # label is a short tail of the id; edges deferred (always empty).
        fp = by_id["avm/res/network/firewall-policy"]
        self.assertEqual(fp["label"], "firewall-policy")
        self.assertEqual(fp["edges"], [])

    def test_empty_paths_yield_empty_provider(self):
        cg = gather.build_directory_areas([], gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(cg, {"provider": "directory", "areas": []})

    def test_none_path_classifies_to_none(self):
        self.assertIsNone(
            gather.classify_code_area(None, gather.DEFAULT_AREA_PATTERNS))
        self.assertIsNone(
            gather.classify_code_area("", gather.DEFAULT_AREA_PATTERNS))


class TestGraphifyProvider(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "graphify_graph_sample.json")) as fh:
            self.graph = json.load(fh)

    def test_groups_nodes_by_community_into_areas(self):
        cg = gather.parse_graphify_graph(self.graph)
        self.assertEqual(cg["provider"], "graphify")
        ids = {a["id"] for a in cg["areas"]}
        self.assertEqual(ids, {"community:0", "community:1"})

    def test_area_paths_are_distinct_source_files_in_the_community(self):
        cg = gather.parse_graphify_graph(self.graph)
        by_id = {a["id"]: a for a in cg["areas"]}
        self.assertEqual(sorted(by_id["community:0"]["paths"]),
                         ["src/app/auth.py", "src/app/session.py"])
        # n3 and n5 share src/store/user.py -> de-duplicated to one path
        self.assertEqual(sorted(by_id["community:1"]["paths"]),
                         ["src/store/index.py", "src/store/user.py"])

    def test_area_label_is_a_representative_dir(self):
        cg = gather.parse_graphify_graph(self.graph)
        by_id = {a["id"]: a for a in cg["areas"]}
        # a representative path/dir for the community (not empty)
        self.assertTrue(by_id["community:0"]["label"])
        self.assertEqual(by_id["community:0"]["edges"], [])

    def test_no_top_level_communities_key_required(self):
        # The real shape has NO top-level `communities`; parser must not need it.
        self.assertNotIn("communities", self.graph)
        cg = gather.parse_graphify_graph(self.graph)
        self.assertTrue(cg["areas"])

    def test_empty_or_nodeless_graph_yields_empty_provider(self):
        self.assertEqual(gather.parse_graphify_graph({}),
                         {"provider": "graphify", "areas": []})
        self.assertEqual(gather.parse_graphify_graph({"nodes": [], "links": []}),
                         {"provider": "graphify", "areas": []})


class TestProviderSelection(unittest.TestCase):
    PATHS = ["avm/res/network/firewall-policy/main.bicep",
             "src/app/handlers/auth.py"]

    def test_uses_directory_provider_when_graphify_absent(self):
        cg = gather.select_code_area_provider(
            self.PATHS, "clone", which=lambda _n: None)
        self.assertEqual(cg["provider"], "directory")
        self.assertTrue(cg["areas"])

    def test_uses_directory_provider_when_graphify_emits_no_nodes(self):
        # graphify on PATH and runs, but graph.json has no nodes -> fall back.
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify",
            run=lambda cmd, **kw: None,
            read_json=lambda _p: {"nodes": [], "links": []})
        self.assertEqual(cg["provider"], "directory")

    def test_prefers_graphify_when_present_and_nodes_exist(self):
        with open(os.path.join(FIX, "graphify_graph_sample.json")) as fh:
            graph = json.load(fh)
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify",
            run=lambda cmd, **kw: None,
            read_json=lambda _p: graph)
        self.assertEqual(cg["provider"], "graphify")
        self.assertTrue(cg["areas"])

    def test_graphify_run_failure_falls_back_silently(self):
        def boom(cmd, **kw):
            raise RuntimeError("graphify exploded")
        cg = gather.select_code_area_provider(
            self.PATHS, "clone",
            which=lambda _n: "/usr/bin/graphify", run=boom,
            read_json=lambda _p: {"nodes": []})
        self.assertEqual(cg["provider"], "directory")


class TestParseCodeowners(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "codeowners_sample.txt")) as fh:
            self.text = fh.read()

    def test_maps_glob_to_logins_stripping_at(self):
        owners = gather.parse_codeowners(self.text)
        self.assertEqual(owners["avm/res/network/"], ["alice", "bob"])
        self.assertEqual(owners["avm/res/storage/"], ["carol"])
        self.assertEqual(owners["*.bicep"], ["bicep-reviewers"])

    def test_team_handles_are_kept_as_owners(self):
        owners = gather.parse_codeowners(self.text)
        self.assertEqual(owners["docs/"], ["org/docs-team", "dave"])
        self.assertEqual(owners["*"], ["org/maintainers"])

    def test_comments_and_blank_lines_ignored(self):
        owners = gather.parse_codeowners(self.text)
        self.assertNotIn("#", "".join(owners))
        self.assertEqual(len(owners), 5)

    def test_permissive_on_empty_or_none(self):
        self.assertEqual(gather.parse_codeowners(""), {})
        self.assertEqual(gather.parse_codeowners(None), {})

    def test_pattern_with_no_owners_is_skipped(self):
        self.assertEqual(gather.parse_codeowners("docs/   \n"), {})


class TestDetectLabelTaxonomy(unittest.TestCase):
    LABELS = ["area: networking", "area: storage", "priority: high",
              "status: in progress", "Type: Bug", "Class: Resource Module",
              "Needs: Triage", "lifecycle/stale", "good first issue"]

    def test_auto_detects_known_namespaces_into_facets(self):
        tax = gather.detect_label_taxonomy(self.LABELS)
        self.assertEqual(tax["source"], "auto")
        # area facet groups both area:* labels under the namespace
        self.assertIn("area", tax)
        self.assertEqual(sorted(tax["area"]["area:"]),
                         ["area: networking", "area: storage"])
        self.assertIn("priority", tax)
        self.assertIn("status", tax)
        # AVM Class:/Type:/Needs: map to kind/lifecycle facets
        self.assertIn("kind", tax)
        self.assertIn("Type:", tax["kind"])

    def test_unprefixed_labels_do_not_create_facets(self):
        tax = gather.detect_label_taxonomy(["good first issue", "bug"])
        # nothing structured -> no facet buckets, just the source marker
        self.assertEqual(set(tax) - {"source"}, set())
        self.assertEqual(tax["source"], "auto")

    def test_config_override_extends_and_marks_source_merged(self):
        config = {"area": ["component:"], "priority": ["sev/"]}
        tax = gather.detect_label_taxonomy(
            ["component: api", "sev/1", "area: networking"], config=config)
        self.assertEqual(tax["source"], "merged")
        self.assertIn("component:", tax["area"])
        self.assertIn("sev/", tax["priority"])
        # auto-detected area:* still present alongside the config namespace
        self.assertIn("area:", tax["area"])

    def test_config_only_when_no_auto_marks_source_config(self):
        tax = gather.detect_label_taxonomy(
            ["component: api"], config={"area": ["component:"]})
        self.assertEqual(tax["source"], "config")

    def test_empty_labels_yield_no_facets(self):
        self.assertEqual(gather.detect_label_taxonomy([]), {"source": "auto"})


class TestFacetsAndKind(unittest.TestCase):
    TAX = {
        "area": {"area:": ["area: networking", "area: storage"]},
        "priority": {"priority:": ["priority: high"]},
        "status": {"status:": ["status: in progress"]},
        "kind": {"Type:": ["Type: Bug", "Type: Feature"]},
        "lifecycle": {"lifecycle/": ["lifecycle/stale"]},
        "source": "auto",
    }

    def test_apply_facets_picks_one_value_per_facet_from_labels(self):
        item = {"labels": ["area: networking", "priority: high", "Type: Bug"]}
        f = gather.apply_facets(item, self.TAX)
        self.assertEqual(f["area"], "area: networking")
        self.assertEqual(f["priority"], "priority: high")
        self.assertIsNone(f["status"])
        self.assertIsNone(f["lifecycle"])

    def test_apply_facets_returns_all_four_keys_even_when_empty(self):
        f = gather.apply_facets({"labels": []}, self.TAX)
        self.assertEqual(set(f), {"area", "priority", "status", "lifecycle"})
        self.assertTrue(all(v is None for v in f.values()))

    def test_kind_native_issue_type_wins(self):
        issue = {"labels": ["Type: Bug"], "title": "crash",
                 "issue_type": "Feature"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=True),
            "feature")

    def test_kind_label_facet_when_no_native_type(self):
        issue = {"labels": ["Type: Bug"], "title": "whatever"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=False),
            "bug")

    def test_kind_template_filename_then_heuristic(self):
        # template name maps module requests
        issue = {"labels": [], "title": "x",
                 "template": "module_request.md"}
        self.assertEqual(
            gather.classify_issue_kind(issue, self.TAX, types_present=False),
            "module-request")
        # title heuristic: a question
        q = {"labels": [], "title": "How do I configure the firewall?"}
        self.assertEqual(
            gather.classify_issue_kind(q, self.TAX, types_present=False),
            "question")

    def test_kind_defaults_to_other(self):
        self.assertEqual(
            gather.classify_issue_kind({"labels": [], "title": "misc"},
                                       self.TAX, types_present=False),
            "other")


class TestAcquireAssemblyP3b(unittest.TestCase):
    """Compose the Phase 3b helpers over recorded inputs, offline."""

    def _bundle(self):
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            code_events = gather.parse_code_events(fh.read())
        with open(os.path.join(FIX, "codeowners_sample.txt")) as fh:
            code_owners = gather.parse_codeowners(fh.read())

        # paths the provider sees come from code_events + commit file lists
        paths = sorted({e["path"] for e in code_events}
                       | {e["old_path"] for e in code_events if e.get("old_path")})
        code_graph = gather.select_code_area_provider(
            paths, "clone", which=lambda _n: None)  # graphify absent -> directory

        prs = [{"number": 42, "labels": ["area: networking", "Type: Bug"],
                "title": "fix policy", "body": ""}]
        issues = [{"number": 18, "labels": ["area: storage", "priority: high"],
                   "title": "Need storage module", "body": "module please",
                   "state": "open"}]
        all_labels = sorted({l for it in prs + issues for l in it["labels"]})
        taxonomy = gather.detect_label_taxonomy(all_labels)
        for it in prs + issues:
            it["facets"] = gather.apply_facets(it, taxonomy)
        for issue in issues:
            issue["kind"] = gather.classify_issue_kind(
                issue, taxonomy, types_present=False)

        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["code_events"] = code_events
        bundle["code_graph"] = code_graph
        bundle["code_owners"] = code_owners
        bundle["label_taxonomy"] = taxonomy
        return bundle

    def test_code_graph_is_directory_provider_with_areas(self):
        b = self._bundle()
        self.assertEqual(b["code_graph"]["provider"], "directory")
        self.assertTrue(b["code_graph"]["areas"])
        for a in b["code_graph"]["areas"]:
            self.assertTrue(a["id"] and a["paths"])

    def test_label_taxonomy_and_facets_present(self):
        b = self._bundle()
        self.assertIn("source", b["label_taxonomy"])
        pr = b["prs"][0]
        self.assertEqual(pr["facets"]["area"], "area: networking")
        issue = b["issues"][0]
        self.assertEqual(issue["facets"]["priority"], "priority: high")
        self.assertIn(issue["kind"],
                      {"feature", "module-request", "bug", "idea",
                       "question", "docs", "other"})

    def test_code_owners_present(self):
        b = self._bundle()
        self.assertIn("avm/res/network/", b["code_owners"])


class TestParseBicepModuleRefs(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            self.src = fh.read()

    def test_extracts_all_three_module_refs(self):
        refs = gather.parse_bicep_module_refs(self.src)
        self.assertEqual(len(refs), 3)

    def test_registry_ref_splits_path_and_version(self):
        refs = gather.parse_bicep_module_refs(self.src)
        by_path = {r["registry_path"]: r for r in refs if r["registry_path"]}
        self.assertIn("avm/res/storage/storage-account", by_path)
        self.assertEqual(by_path["avm/res/storage/storage-account"]["version"], "0.9.0")
        self.assertEqual(by_path["avm/res/key-vault/vault"]["version"], "0.6.1")
        self.assertIsNone(by_path["avm/res/storage/storage-account"]["local_path"])

    def test_local_ref_is_kept_as_local_path(self):
        refs = gather.parse_bicep_module_refs(self.src)
        locals_ = [r for r in refs if r["local_path"]]
        self.assertEqual(len(locals_), 1)
        self.assertEqual(locals_[0]["local_path"],
                         "../../../utl/types/avm-common-types/main.bicep")
        self.assertIsNone(locals_[0]["registry_path"])

    def test_br_with_explicit_registry_host_strips_to_path(self):
        src = "module x 'br:mcr.microsoft.com/bicep/avm/res/network/vnet:1.2.3' = {}"
        r = gather.parse_bicep_module_refs(src)[0]
        self.assertEqual(r["registry_path"], "avm/res/network/vnet")
        self.assertEqual(r["version"], "1.2.3")

    def test_empty_or_none_source_yields_no_refs(self):
        self.assertEqual(gather.parse_bicep_module_refs(""), [])
        self.assertEqual(gather.parse_bicep_module_refs(None), [])

    def test_array_instantiation_marks_many_instances(self):
        src = "module x 'nat-rule/main.bicep' = [for i in r: { name: i }]"
        self.assertEqual(gather.parse_bicep_module_refs(src)[0]["instances"], "many")

    def test_single_instantiation_marks_one_instance(self):
        src = "module x 'child/main.bicep' = { name: 'c' }"
        self.assertEqual(gather.parse_bicep_module_refs(src)[0]["instances"], "one")


class TestResolveModuleRef(unittest.TestCase):
    def test_registry_path_resolves_to_avm_area_id(self):
        ri = {"registry_path": "avm/res/storage/storage-account",
              "version": "0.9.0", "local_path": None}
        self.assertEqual(
            gather.resolve_module_ref(ri, "avm/ptn/foo/bar/main.bicep",
                                      gather.DEFAULT_AREA_PATTERNS),
            "avm/res/storage/storage-account")

    def test_local_ref_resolves_relative_to_base(self):
        ri = {"registry_path": None, "version": None,
              "local_path": "../../../utl/types/avm-common-types/main.bicep"}
        # base dir avm/ptn/foo/bar -> up three (file-relative) -> avm/utl/types/...
        self.assertEqual(
            gather.resolve_module_ref(ri, "avm/ptn/foo/bar/main.bicep",
                                      gather.DEFAULT_AREA_PATTERNS),
            "avm/utl/types/avm-common-types")

    def test_unrecognised_registry_path_falls_back_to_the_path(self):
        ri = {"registry_path": "some/custom/thing", "version": "1.0.0",
              "local_path": None}
        self.assertEqual(
            gather.resolve_module_ref(ri, "x/main.bicep",
                                      gather.DEFAULT_AREA_PATTERNS),
            "some/custom/thing")

    def test_empty_ref_resolves_to_none(self):
        self.assertIsNone(
            gather.resolve_module_ref({"registry_path": None, "version": None,
                                       "local_path": None}, "x/main.bicep",
                                      gather.DEFAULT_AREA_PATTERNS))


class TestWalkArmDeployments(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            self.arm = json.load(fh)

    def test_walks_full_transitive_tree(self):
        nodes = gather.walk_arm_deployments(self.arm)
        # storageDeployment (d1) + its nested telemetry (d2) + kvDeployment (d1) = 3
        self.assertEqual(len(nodes), 3)

    def test_records_depth_and_metadata_name(self):
        nodes = gather.walk_arm_deployments(self.arm)
        by_name = {n["name"]: n for n in nodes}
        self.assertEqual(by_name["storageDeployment"]["depth"], 1)
        self.assertEqual(by_name["storageDeployment"]["metadata_name"], "storage-account")
        self.assertEqual(by_name["kvDeployment"]["metadata_name"], "vault")
        # the telemetry deployment is one level deeper
        self.assertEqual(
            by_name["46d3xbcp.res.storage-storageaccount.0-9-0.abcde"]["depth"], 2)

    def test_handles_resources_as_dict_symbolic_names(self):
        arm = {"resources": {
            "dep": {"type": "Microsoft.Resources/deployments", "name": "d",
                    "properties": {"template": {"metadata": {"name": "x"},
                                                "resources": []}}}}}
        self.assertEqual(len(gather.walk_arm_deployments(arm)), 1)

    def test_empty_or_none_arm_yields_nothing(self):
        self.assertEqual(gather.walk_arm_deployments({}), [])
        self.assertEqual(gather.walk_arm_deployments(None), [])


class TestBuildBicepEdges(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            self.src = fh.read()
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            self.arm = json.load(fh)
        # vault is a DIRECT dep (depth-1, immediate); private-endpoint is nested
        # DEEPER in the ARM tree (depth-2) -> exercises the genuine transitive case.
        self.area_ids = {"avm/ptn/foo/bar", "avm/res/key-vault/vault",
                         "avm/res/network/private-endpoint"}
        self.base = "avm/ptn/foo/bar/main.bicep"

    def test_immediate_edges_carry_area_id_and_version(self):
        edges = gather.build_bicep_edges(self.src, self.arm, self.base,
                                         self.area_ids, gather.DEFAULT_AREA_PATTERNS)
        imm = {e["to"]: e for e in edges if not e["transitive"]}
        self.assertEqual(imm["avm/res/storage/storage-account"]["version"], "0.9.0")
        self.assertEqual(imm["avm/res/storage/storage-account"]["provider"], "bicep")
        self.assertTrue(imm["avm/res/storage/storage-account"]["resolved"])
        self.assertEqual(imm["avm/res/key-vault/vault"]["version"], "0.6.1")
        # the local ref resolved to its utl area
        self.assertIn("avm/utl/types/avm-common-types", imm)

    def test_all_three_source_refs_become_immediate_edges(self):
        edges = gather.build_bicep_edges(self.src, self.arm, self.base,
                                         self.area_ids, gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(sum(1 for e in edges if not e["transitive"]), 3)

    def test_transitive_edge_for_deeply_nested_repo_area(self):
        edges = gather.build_bicep_edges(self.src, self.arm, self.base,
                                         self.area_ids, gather.DEFAULT_AREA_PATTERNS)
        trans = {e["to"] for e in edges if e["transitive"]}
        # private-endpoint is nested at depth 2 and is a repo area -> transitive edge.
        self.assertIn("avm/res/network/private-endpoint", trans)
        self.assertNotIn(None, trans)

    def test_direct_dep_not_duplicated_as_transitive(self):
        edges = gather.build_bicep_edges(self.src, self.arm, self.base,
                                         self.area_ids, gather.DEFAULT_AREA_PATTERNS)
        # vault is a DIRECT (depth-1) dependency: it must appear exactly once, as an
        # immediate edge, never re-emitted as a transitive edge.
        vault = [e for e in edges if e["to"] == "avm/res/key-vault/vault"]
        self.assertEqual(len(vault), 1)
        self.assertFalse(vault[0]["transitive"])

    def test_local_child_submodule_named_not_self_edge(self):
        """A local relative ref to a private child module is resolved to the named
        child node (`<area>/<child>`), not collapsed to a `<area> -> <area>` self
        edge, and is flagged local + carries instance cardinality."""
        src = "module child 'nat-rule/main.bicep' = [for x in r: {}]\n"
        base = "avm/res/network/vpn-gateway/main.bicep"
        edges = gather.build_bicep_edges(src, {}, base, set(),
                                         gather.DEFAULT_AREA_PATTERNS)
        e = next(e for e in edges if e["ref"] == "nat-rule/main.bicep")
        self.assertEqual(e["to"], "avm/res/network/vpn-gateway/nat-rule")
        self.assertNotEqual(e["to"], "avm/res/network/vpn-gateway")
        self.assertTrue(e["local"])
        self.assertEqual(e["instances"], "many")
        self.assertTrue(e["resolved"])

    def test_true_self_recursion_stays_self_edge(self):
        """A module that references its own main.bicep is genuine recursion and
        stays a self edge (not relabelled as a child)."""
        src = "module self 'main.bicep' = { name: 'r' }"
        base = "avm/res/network/vpn-gateway/main.bicep"
        edges = gather.build_bicep_edges(src, {}, base, set(),
                                         gather.DEFAULT_AREA_PATTERNS)
        e = edges[0]
        self.assertEqual(e["to"], "avm/res/network/vpn-gateway")
        self.assertNotIn("local", e)

    def test_empty_build_inputs_yield_no_edges(self):
        self.assertEqual(
            gather.build_bicep_edges("", {}, self.base, set(),
                                     gather.DEFAULT_AREA_PATTERNS), [])


class TestMatchRepoArea(unittest.TestCase):
    """Deterministic area matching (Copilot review): never depends on set order."""

    def test_exact_normalized_match_beats_substring(self):
        # "vault" exactly matches avm/res/key-vault/vault, not the substring
        # candidate avm/res/key-vault/vault-secret — exact must always win.
        ids = {"avm/res/key-vault/vault", "avm/res/key-vault/vault-secret"}
        self.assertEqual(gather._match_repo_area("vault", ids),
                         "avm/res/key-vault/vault")

    def test_longest_tail_substring_wins_and_is_stable(self):
        # No exact match; "storageaccountblob" should resolve to the most specific
        # (longest) tail, deterministically, regardless of set iteration order.
        ids = {"avm/res/storage/account", "avm/res/storage/account-blob-service"}
        results = {gather._match_repo_area("storage-account-blob-service", ids)
                   for _ in range(20)}
        self.assertEqual(results, {"avm/res/storage/account-blob-service"})

    def test_no_match_returns_none(self):
        self.assertIsNone(gather._match_repo_area(
            "network", {"avm/res/key-vault/vault"}))


class TestCloneHeadSha(unittest.TestCase):
    """Phase 3c.2: provenance pin for resume + roll-up."""

    def test_returns_stripped_sha(self):
        sha = "a" * 40
        got = gather.clone_head_sha("clone", run=lambda cmd, **kw: sha + "\n")
        self.assertEqual(got, sha)

    def test_missing_clone_returns_none(self):
        def boom(cmd, **kw):
            raise RuntimeError("not a git repository")
        self.assertIsNone(gather.clone_head_sha("clone", run=boom))

    def test_empty_output_returns_none(self):
        self.assertIsNone(gather.clone_head_sha("clone", run=lambda cmd, **kw: "\n"))


class TestTerraformParsers(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            self.tf = fh.read()
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            self.dot = fh.read()

    def test_module_blocks_map_name_to_source(self):
        blocks = gather.parse_terraform_module_blocks(self.tf)
        self.assertEqual(blocks["vnet"], "../../modules/vnet")
        self.assertEqual(blocks["naming"], "Azure/naming/azurerm")
        self.assertNotIn("azurerm_resource_group", blocks)  # resources are not modules

    def test_source_found_after_a_nested_block(self):
        # a non-greedy regex would stop at the first `}` and miss source; the
        # brace-balanced parser must still find it.
        tf = (
            'module "x" {\n'
            '  providers = {\n'
            '    azurerm = azurerm.alt\n'
            '  }\n'
            '  source = "../mods/x"\n'
            '}\n'
        )
        self.assertEqual(gather.parse_terraform_module_blocks(tf)["x"], "../mods/x")

    def test_graph_yields_module_dependency_pairs(self):
        pairs = gather.parse_terraform_graph(self.dot)
        # root (None) -> naming ; vnet -> naming
        self.assertIn((None, "naming"), pairs)
        self.assertIn(("vnet", "naming"), pairs)

    def test_graph_ignores_same_module_self_edges(self):
        pairs = gather.parse_terraform_graph(self.dot)
        self.assertFalse([p for p in pairs if p[0] == p[1] and p[0] is not None])

    def test_empty_inputs(self):
        self.assertEqual(gather.parse_terraform_module_blocks(""), {})
        self.assertEqual(gather.parse_terraform_graph(""), [])
        self.assertEqual(gather.parse_terraform_module_blocks(None), {})


class TestBuildTerraformEdges(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            self.tf = fh.read()
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            self.dot = fh.read()
        self.base = "live/prod/main.tf"

    def test_local_module_source_resolves_to_area(self):
        edges = gather.build_terraform_edges(
            self.tf, self.dot, self.base, set(), gather.DEFAULT_AREA_PATTERNS)
        tos = {e["to"] for e in edges}
        # ../../modules/vnet relative to live/prod -> modules/vnet
        self.assertIn("modules/vnet", tos)

    def test_registry_module_source_kept_as_ref(self):
        edges = gather.build_terraform_edges(
            self.tf, self.dot, self.base, set(), gather.DEFAULT_AREA_PATTERNS)
        # An external registry module is NOT a repo area: identity lives in `ref`
        # (+version), `to` is None and `resolved` is False (schema: to=area-id|null).
        naming = [e for e in edges if e["ref"] == "Azure/naming/azurerm"]
        self.assertTrue(naming)
        self.assertIsNone(naming[0]["to"])
        self.assertFalse(naming[0]["resolved"])
        self.assertEqual(naming[0]["version"], "0.4.0")
        self.assertEqual(naming[0]["provider"], "terraform")

    def test_only_transitive_local_module_is_marked_transitive(self):
        # subnet is reached ONLY through vnet (never from root) -> transitive=True;
        # vnet is a direct root dependency -> transitive=False.
        tf = ('module "vnet" { source = "../../modules/vnet" }\n'
              'module "subnet" { source = "../../modules/subnet" }\n')
        dot = ('digraph {\n'
               '"[root] x (expand)" -> "[root] module.vnet (expand)"\n'
               '"[root] module.vnet.y (expand)" -> "[root] module.subnet.z (expand)"\n'
               '}\n')
        edges = gather.build_terraform_edges(
            tf, dot, self.base, set(), gather.DEFAULT_AREA_PATTERNS)
        by_to = {e["to"]: e for e in edges}
        self.assertFalse(by_to["modules/vnet"]["transitive"])
        self.assertTrue(by_to["modules/subnet"]["transitive"])

    def test_edges_dedupe_and_are_marked_provider_terraform(self):
        edges = gather.build_terraform_edges(
            self.tf, self.dot, self.base, set(), gather.DEFAULT_AREA_PATTERNS)
        self.assertTrue(all(e["provider"] == "terraform" for e in edges))
        keys = [(e["to"], e["ref"]) for e in edges]
        self.assertEqual(len(keys), len(set(keys)))

    def test_empty_inputs_yield_no_edges(self):
        self.assertEqual(
            gather.build_terraform_edges("", "", self.base, set(),
                                         gather.DEFAULT_AREA_PATTERNS), [])


class TestExtractIacEdges(unittest.TestCase):
    def _code_graph(self):
        return {"provider": "directory", "areas": [
            {"id": "avm/ptn/foo/bar", "label": "bar",
             "paths": ["avm/ptn/foo/bar/main.bicep",
                       "avm/ptn/foo/bar/README.md"], "edges": []},
            {"id": "avm/res/key-vault/vault", "label": "vault",
             "paths": ["avm/res/key-vault/vault/main.bicep"], "edges": []},
            {"id": "live/prod", "label": "prod",
             "paths": ["live/prod/main.tf"], "edges": []},
        ]}

    def test_build_only_no_tools_leaves_edges_empty(self):
        cg = gather.extract_iac_edges(self._code_graph(), "clone",
                                      which=lambda _n: None)
        for a in cg["areas"]:
            self.assertEqual(a["edges"], [])

    def test_bicep_present_populates_edges_for_bicep_areas(self):
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            arm_text = fh.read()
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            bicep_src = fh.read()

        def which(name):
            return "/usr/bin/bicep" if name == "bicep" else None

        def run(cmd, **kw):
            return arm_text if cmd[:2] == ["bicep", "build"] else ""

        cg = gather.extract_iac_edges(
            self._code_graph(), "clone",
            which=which, run=run, read_text=lambda _p: bicep_src)
        bar = next(a for a in cg["areas"] if a["id"] == "avm/ptn/foo/bar")
        tos = {e["to"] for e in bar["edges"]}
        self.assertIn("avm/res/storage/storage-account", tos)
        self.assertTrue(any(e["version"] == "0.9.0" for e in bar["edges"]))
        # terraform area stays empty (terraform absent)
        prod = next(a for a in cg["areas"] if a["id"] == "live/prod")
        self.assertEqual(prod["edges"], [])

    def test_bicep_build_failure_leaves_edges_empty(self):
        def boom(cmd, **kw):
            raise RuntimeError("restore blocked by network policy")
        cg = gather.extract_iac_edges(
            self._code_graph(), "clone",
            which=lambda n: "/usr/bin/bicep" if n == "bicep" else None,
            run=boom, read_text=lambda _p: "")
        bar = next(a for a in cg["areas"] if a["id"] == "avm/ptn/foo/bar")
        self.assertEqual(bar["edges"], [])

    def test_terraform_present_populates_tf_area(self):
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            dot = fh.read()
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            tf = fh.read()

        def run(cmd, **kw):
            return dot if cmd[-1] == "graph" else ""

        cg = gather.extract_iac_edges(
            self._code_graph(), "clone",
            which=lambda n: "/usr/bin/terraform" if n == "terraform" else None,
            run=run, read_text=lambda _p: tf)
        prod = next(a for a in cg["areas"] if a["id"] == "live/prod")
        self.assertTrue(prod["edges"])
        self.assertTrue(all(e["provider"] == "terraform" for e in prod["edges"]))


class TestIacEdgeHardening(unittest.TestCase):
    """Phase 3c.1: per-build timeout, retry, and VISIBLE gaps (edges_status +
    edge_extraction summary) so a killed/slow build is never a silent empty."""

    def _cg(self):
        return {"provider": "directory", "areas": [
            {"id": "avm/ptn/foo/bar", "label": "bar",
             "paths": ["avm/ptn/foo/bar/main.bicep"], "edges": []},
            {"id": "live/prod", "label": "prod",
             "paths": ["live/prod/main.tf"], "edges": []},
        ]}

    @staticmethod
    def _bicep_only(name):
        return "/usr/bin/bicep" if name == "bicep" else None

    def test_timeout_is_recorded_not_a_silent_empty(self):
        def run(cmd, **kw):
            if cmd[:2] == ["bicep", "build"]:
                raise gather.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return ""
        cg = gather.extract_iac_edges(self._cg(), "clone", which=self._bicep_only,
                                      run=run, read_text=lambda _p: "", retries=1)
        bar = next(a for a in cg["areas"] if a["id"] == "avm/ptn/foo/bar")
        self.assertEqual(bar["edges"], [])
        self.assertEqual(bar["edges_status"], "timeout")
        self.assertEqual(cg["edge_extraction"]["timeout"], 1)
        self.assertEqual(cg["edge_extraction"]["skipped"], 1)  # the tf area

    def test_retry_recovers_a_transient_failure(self):
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            arm = fh.read()
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            src = fh.read()
        calls = {"build": 0}

        def run(cmd, **kw):
            if cmd[:2] == ["bicep", "build"]:
                calls["build"] += 1
                if calls["build"] == 1:
                    raise gather.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
                return arm
            return ""
        cg = gather.extract_iac_edges(self._cg(), "clone", which=self._bicep_only,
                                      run=run, read_text=lambda _p: src, retries=1)
        bar = next(a for a in cg["areas"] if a["id"] == "avm/ptn/foo/bar")
        self.assertEqual(bar["edges_status"], "resolved")
        self.assertTrue(bar["edges"])
        self.assertEqual(calls["build"], 2)  # failed once, retried, then succeeded

    def test_no_tools_marks_every_area_skipped(self):
        cg = gather.extract_iac_edges(self._cg(), "clone", which=lambda _n: None)
        self.assertEqual(cg["edge_extraction"]["skipped"], 2)
        self.assertTrue(all(a["edges_status"] == "skipped" for a in cg["areas"]))

    def test_summary_counts_resolved_and_skipped(self):
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            arm = fh.read()
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            src = fh.read()
        cg = gather.extract_iac_edges(
            self._cg(), "clone", which=self._bicep_only,
            run=lambda cmd, **kw: arm if cmd[:2] == ["bicep", "build"] else "",
            read_text=lambda _p: src)
        summ = cg["edge_extraction"]
        self.assertEqual(summ["resolved"], 1)  # the bicep area
        self.assertEqual(summ["skipped"], 1)   # the tf area (terraform absent)
        self.assertEqual(summ["timeout"], 0)
        self.assertEqual(summ["failed"], 0)


class TestAcquireEdgesP3c(unittest.TestCase):
    """Compose the provider + edge seam offline (graphify + bicep absent ->
    directory provider, edges empty; bicep stubbed -> edges populate)."""

    PATHS = ["avm/ptn/foo/bar/main.bicep", "avm/ptn/foo/bar/README.md",
             "avm/res/key-vault/vault/main.bicep"]

    def test_directory_provider_edges_empty_without_tools(self):
        cg = gather.select_code_area_provider(
            self.PATHS, "clone", which=lambda _n: None)
        cg = gather.extract_iac_edges(cg, "clone", which=lambda _n: None)
        self.assertEqual(cg["provider"], "directory")
        self.assertTrue(cg["areas"])
        for a in cg["areas"]:
            self.assertEqual(a["edges"], [])

    def test_edges_populate_when_bicep_present(self):
        with open(os.path.join(FIX, "arm_compiled_sample.json")) as fh:
            arm_text = fh.read()
        with open(os.path.join(FIX, "bicep_source_sample.bicep")) as fh:
            src = fh.read()
        cg = gather.select_code_area_provider(
            self.PATHS, "clone", which=lambda _n: None)
        cg = gather.extract_iac_edges(
            cg, "clone",
            which=lambda n: "/usr/bin/bicep" if n == "bicep" else None,
            run=lambda cmd, **kw: arm_text if cmd[:2] == ["bicep", "build"] else "",
            read_text=lambda _p: src)
        bar = next(a for a in cg["areas"] if a["id"] == "avm/ptn/foo/bar")
        self.assertTrue(bar["edges"])
        self.assertTrue(any(e["to"] == "avm/res/storage/storage-account"
                            for e in bar["edges"]))


BICEP_DIFF = """diff --git a/avm/res/foo/main.bicep b/avm/res/foo/main.bicep
index 1111111..2222222 100644
--- a/avm/res/foo/main.bicep
+++ b/avm/res/foo/main.bicep
@@ -1,5 +1,7 @@
 param location string = resourceGroup().location
-param oldParam string
+param newParam string
+@description('the vault')
 resource vault 'Microsoft.KeyVault/vaults@2023-01-01' = {
-  name: 'old'
+  name: 'new'
 }
"""

TF_DIFF = """diff --git a/live/prod/main.tf b/live/prod/main.tf
--- a/live/prod/main.tf
+++ b/live/prod/main.tf
@@ -1,4 +1,7 @@
 resource "azurerm_resource_group" "this" {
-  name = "old"
+  name = "new"
 }
+variable "region" {
+  type = string
+}
"""


class TestUnifiedDiffParser(unittest.TestCase):
    def test_parses_path_and_hunk_lines(self):
        files = gather.parse_unified_diff(BICEP_DIFF)
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertEqual(f["path"], "avm/res/foo/main.bicep")
        self.assertEqual(f["old_path"], "avm/res/foo/main.bicep")
        self.assertEqual(f["hunks"][0]["new_start"], 1)
        signs = [s for s, _ in f["hunks"][0]["lines"]]
        self.assertIn("+", signs)
        self.assertIn("-", signs)

    def test_added_file_old_path_is_none(self):
        diff = ("diff --git a/x.bicep b/x.bicep\n--- /dev/null\n+++ b/x.bicep\n"
                "@@ -0,0 +1,1 @@\n+param a string\n")
        f = gather.parse_unified_diff(diff)[0]
        self.assertIsNone(f["old_path"])
        self.assertEqual(f["path"], "x.bicep")


class TestDetectSymbolDecl(unittest.TestCase):
    def test_bicep_decls(self):
        self.assertEqual(gather.detect_symbol_decl("bicep", "param foo string"),
                         ("symbol", "param", "foo"))
        self.assertEqual(gather.detect_symbol_decl(
            "bicep", "resource vault 'Microsoft.KeyVault/vaults@2023-01-01' = {"),
            ("symbol", "resource", "vault"))
        self.assertEqual(gather.detect_symbol_decl("bicep", "  // a note")[0], "comment")
        self.assertIsNone(gather.detect_symbol_decl("bicep", "  name: 'x'"))

    def test_terraform_decls(self):
        self.assertEqual(gather.detect_symbol_decl(
            "terraform", 'resource "azurerm_kv" "this" {'),
            ("symbol", "resource", "azurerm_kv.this"))
        self.assertEqual(gather.detect_symbol_decl("terraform", 'variable "region" {'),
                         ("symbol", "variable", "region"))
        self.assertEqual(gather.detect_symbol_decl("terraform", "# comment")[0], "comment")

    def test_comment_subkinds_and_decorative_filter(self):
        self.assertEqual(gather.detect_symbol_decl("bicep", "// TODO: revisit"),
                         ("comment", "todo", "// TODO: revisit"))
        self.assertEqual(gather.detect_symbol_decl("bicep", "// a real note")[1], "comment")
        # decorative separators carry no decision content -> not tracked
        self.assertIsNone(gather.detect_symbol_decl("bicep", "// ============ //"))
        self.assertIsNone(gather.detect_symbol_decl("terraform", "# -----------"))

    def test_unknown_lang_is_none(self):
        self.assertIsNone(gather.detect_symbol_decl("python", "def f():"))


class TestBuildSymbolDeltas(unittest.TestCase):
    def _by_name(self, path, diff):
        f = gather.parse_unified_diff(diff)[0]
        return {(d["subkind"], d["name"]): d
                for d in gather.build_symbol_deltas(path, f["hunks"])}

    def test_bicep_add_drop_change_comment(self):
        d = self._by_name("avm/res/foo/main.bicep", BICEP_DIFF)
        self.assertEqual(d[("param", "newParam")]["change"], "add")
        self.assertEqual(d[("param", "oldParam")]["change"], "drop")
        self.assertEqual(d[("resource", "vault")]["change"], "change")
        self.assertIn("name: 'old'", d[("resource", "vault")]["before"])
        self.assertIn("name: 'new'", d[("resource", "vault")]["after"])
        # comments are keyed by TEXT (not collapsed to one per file)
        self.assertEqual(d[("comment", "@description('the vault')")]["change"], "add")

    def test_comment_replacement_keeps_both_old_and_new_text(self):
        # A comment replaced as a decision evolves -> old text dropped, new text added,
        # both preserved (the decision trail), not collapsed into one blob.
        diff = ("diff --git a/m.bicep b/m.bicep\n--- a/m.bicep\n+++ b/m.bicep\n"
                "@@ -1,2 +1,2 @@\n"
                "-// TODO: decide retention window\n"
                "+// retention fixed at 90d per #123\n"
                " param x string\n")
        f = gather.parse_unified_diff(diff)[0]
        deltas = {(d["subkind"], d["name"]): d
                  for d in gather.build_symbol_deltas("m.bicep", f["hunks"])}
        # old TODO dropped (and recognised as a decision marker)...
        old = deltas[("todo", "// TODO: decide retention window")]
        self.assertEqual(old["change"], "drop")
        # ...new note added — both texts captured as distinct deltas
        new = deltas[("comment", "// retention fixed at 90d per #123")]
        self.assertEqual(new["change"], "add")

    def test_comment_does_not_swallow_following_body_edit(self):
        # a comment must not become the enclosing symbol for a later body change
        diff = ("diff --git a/m.bicep b/m.bicep\n--- a/m.bicep\n+++ b/m.bicep\n"
                "@@ -1,3 +1,3 @@\n"
                " resource vault 'x' = {\n"
                " // note\n"
                "-  sku: 'A'\n+  sku: 'B'\n }\n")
        f = gather.parse_unified_diff(diff)[0]
        d = {(x["subkind"], x["name"]): x
             for x in gather.build_symbol_deltas("m.bicep", f["hunks"])}
        self.assertEqual(d[("resource", "vault")]["change"], "change")  # body edit -> resource

    def test_terraform_change_and_add(self):
        d = self._by_name("live/prod/main.tf", TF_DIFF)
        self.assertEqual(d[("resource", "azurerm_resource_group.this")]["change"], "change")
        self.assertEqual(d[("variable", "region")]["change"], "add")

    def test_non_source_file_yields_nothing(self):
        f = gather.parse_unified_diff(
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            "@@ -1 +1 @@\n-old\n+new\n")[0]
        self.assertEqual(gather.build_symbol_deltas("README.md", f["hunks"]), [])

    def test_before_after_bounded(self):
        long = "x" * 500
        diff = (f"diff --git a/m.bicep b/m.bicep\n--- a/m.bicep\n+++ b/m.bicep\n"
                f"@@ -1 +1 @@\n-param a string // {long}\n+param a int // {long}\n")
        f = gather.parse_unified_diff(diff)[0]
        d = gather.build_symbol_deltas("m.bicep", f["hunks"])[0]
        self.assertLessEqual(len(d["before"]), 200)
        self.assertLessEqual(len(d["after"]), 200)


class TestParseSymbolEvents(unittest.TestCase):
    def test_record_splitting_attaches_commit_metadata(self):
        sha = "a" * 40
        header = gather.FIELD_SEP.join([sha, "parent", "Alice", "2026-05-03", "subj"])
        raw = gather.RECORD_SEP + header + "\n" + BICEP_DIFF
        events = gather.parse_symbol_events(raw)
        self.assertTrue(events)
        self.assertTrue(all(e["commit"] == sha and e["author"] == "Alice"
                            and e["date"] == "2026-05-03" for e in events))
        names = {(e["subkind"], e["name"], e["change"]) for e in events}
        self.assertIn(("param", "newParam", "add"), names)
        self.assertIn(("resource", "vault", "change"), names)

    def test_empty_input_yields_no_events(self):
        self.assertEqual(gather.parse_symbol_events(""), [])


class TestClassifyPrKind(unittest.TestCase):
    """Conventional-commit PR title -> canonical kind (train fallback)."""

    def test_conventional_prefixes_map_to_kinds(self):
        self.assertEqual(gather.classify_pr_kind({"title": "feat: add redis cmk"}),
                         "feature")
        self.assertEqual(gather.classify_pr_kind({"title": "fix: null deref"}), "bug")
        self.assertEqual(gather.classify_pr_kind({"title": "docs: clarify readme"}),
                         "docs")

    def test_scope_breaking_and_case_are_handled(self):
        self.assertEqual(
            gather.classify_pr_kind({"title": "feat(avm/res/cache/redis): add"}),
            "feature")
        self.assertEqual(gather.classify_pr_kind({"title": "fix!: drop output"}), "bug")
        self.assertEqual(gather.classify_pr_kind({"title": "Feat: Case Insensitive"}),
                         "feature")

    def test_unmapped_or_absent_prefix_returns_none(self):
        for title in ("chore: bump deps", "refactor: tidy", "perf: faster",
                      "Add policy param", "feat add no colon", ""):
            self.assertIsNone(gather.classify_pr_kind({"title": title}),
                              f"expected None for {title!r}")
        self.assertIsNone(gather.classify_pr_kind({}))


class TestResolveCommitPr(unittest.TestCase):
    """Verify gather's commit->PR number extraction for merge and squash commit subjects."""

    def test_squash_subject(self):
        self.assertEqual(gather.resolve_commit_pr("Add policy param (#42)"), 42)

    def test_merge_subject(self):
        self.assertEqual(
            gather.resolve_commit_pr("Merge pull request #42 from feature/policy"), 42)

    def test_none_when_absent(self):
        self.assertIsNone(gather.resolve_commit_pr("Tidy outputs"))

    def test_attach_sets_pr_field(self):
        commits = [{"sha": "a", "message": "Fix bug (#7)", "pr": None},
                   {"sha": "b", "message": "Refactor", "pr": None}]
        gather.attach_commit_prs(commits)
        self.assertEqual(commits[0]["pr"], 7)
        self.assertIsNone(commits[1]["pr"])


def _fold_fixture_bundle():
    return {
        "meta": {"owner": "acme", "repo": "widget", "from": "2026-01-01",
                 "to": "2026-01-31", "clone_sha": "deadbeef"},
        "prs": [{
            "number": 10, "url": "u/10", "state": "closed", "merged": True,
            "merged_at": "2026-01-10T00:00:00Z", "created_at": "2026-01-05T00:00:00Z",
            "closed_at": "2026-01-10T00:00:00Z",
            "closes": [3], "crossref_issues": [4],
        }],
        "issues": [
            {"number": 3, "url": "u/3", "state": "closed",
             "closed_at": "2026-01-10T00:00:00Z", "updated_at": "2026-01-10T00:00:00Z"},
            {"number": 4, "url": "u/4", "state": "open",
             "updated_at": "2026-01-09T00:00:00Z", "closed_at": None},
        ],
        "commits": [
            {"sha": "abc123", "message": "Add thing (#10)", "author": "alice",
             "date": "2026-01-09T00:00:00Z", "files": ["a.py"]},
            {"sha": "def456", "message": "WIP no pr ref", "author": "bob",
             "date": "2026-01-08T00:00:00Z", "files": ["b.py"]},
        ],
        "code_events": [
            {"commit": "abc123", "author": "alice", "date": "2026-01-09T00:00:00Z",
             "change": "add", "path": "a.py"},
            {"commit": "abc123", "author": "alice", "date": "2026-01-09T00:00:00Z",
             "change": "rename", "path": "c.py", "old_path": "b.py"},
        ],
        "milestones": [{"number": 1, "title": "v1.0", "state": "open"}],
        "releases": [{"tag_name": "v0.9", "published_at": "2026-01-15T00:00:00Z"}],
        # areas carry id/label/paths/edges (see build_directory_areas); fold_bundle
        # keys the structure node on the area's id.
        "code_graph": {"areas": [
            {"id": "core", "label": "Core", "paths": ["src/core"], "edges": []}]},
    }


class TestFoldBundle(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _fold_fixture_bundle())

    def test_nodes_by_class_and_identity(self):
        pr = graphstore.get_node(self.conn, "acme/widget#pr-10")
        self.assertEqual(pr["node_class"], "social")
        self.assertEqual(pr["ts"], "2026-01-10T00:00:00Z")  # merged_at
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#issue-3")["node_class"], "social")
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#abc123")["node_class"], "code")
        ms = graphstore.get_node(self.conn, "acme/widget#milestone-1")
        self.assertEqual(ms["node_class"], "structure")
        self.assertIsNone(ms["ts"])  # structure: NULL ts, excluded from window scans
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#release-v0.9")["node_class"], "structure")
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#area-core")["node_class"], "structure")  # area id

    def test_spine_edges(self):
        out = graphstore.get_edges(self.conn, "acme/widget#pr-10", direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("closes", "acme/widget#issue-3"), types)
        self.assertIn(("cross_ref", "acme/widget#issue-4"), types)
        part = graphstore.get_edges(self.conn, "acme/widget#abc123",
                                    direction="out", edge_types=["part_of"])
        self.assertEqual(part[0]["dst_id"], "acme/widget#pr-10")
        # commit without a PR ref produces no part_of edge
        self.assertEqual(graphstore.get_edges(
            self.conn, "acme/widget#def456", edge_types=["part_of"]), [])

    def test_train_reachable_over_spine(self):
        # issue-3 -> pr-10 (closes) -> abc123 (part_of); issue-4 via cross_ref
        res = graphstore.traverse_spine(self.conn, ["acme/widget#issue-3"])
        self.assertIn("acme/widget#pr-10", res["reached"])
        self.assertIn("acme/widget#abc123", res["reached"])
        self.assertIn("acme/widget#issue-4", res["reached"])

    def test_code_event_ledger_file_level(self):
        evs = graphstore.get_code_events(self.conn, "acme/widget#a.py")
        self.assertEqual([e["event"] for e in evs], ["add"])
        ren = graphstore.get_code_events(self.conn, "acme/widget#c.py")
        self.assertEqual(ren[0]["event"], "rename")
        self.assertEqual(ren[0]["detail"], "b.py")  # old_path -> detail

    def test_window_and_clone_sha_recorded(self):
        self.assertIn(
            {"project": "acme", "repo": "widget", "from": "2026-01-01",
             "to": "2026-01-31"},
            graphstore.get_windows(self.conn))
        self.assertEqual(
            graphstore.get_clone_sha(self.conn, "acme", "widget"), "deadbeef")

    def test_idempotent_refold(self):
        gather.fold_bundle(self.conn, _fold_fixture_bundle())  # second fold
        # 8 spine/structure nodes + 1 `codegraph` singleton (fixture's non-empty
        # code_graph is round-tripped as a structure node; see fold_bundle) + 2
        # person nodes (alice/bob, the commit authors). Person nodes are now
        # created for EVERY participant with a contribution edge (the BUG 1 fix:
        # alice/bob's `authored` edges were previously dangling because their
        # files map to no code area, so the old attribute_people_areas-only
        # persistence skipped them).
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 11)
        # 3 spine edges (closes/cross_ref/part_of) + 2 `authored` person->commit
        # edges (alice->abc123, bob->def456) from Phase 7b-1 step 3. Re-folding
        # mutates nothing: still 5.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 5)

    def test_code_graph_singleton_node_roundtrip(self):
        # the whole code_graph dict is round-tripped under local id `codegraph`,
        # as a NULL-ts structure node (excluded from window scans).
        cg = graphstore.get_node(self.conn, "acme/widget#codegraph")
        self.assertIsNotNone(cg)
        self.assertEqual(cg["node_class"], "structure")
        self.assertIsNone(cg["ts"])
        self.assertEqual(cg["data"], _fold_fixture_bundle()["code_graph"])

    def test_absent_singletons_not_written(self):
        # the fixture has no workflow_stats/code_owners/label_taxonomy -> no node.
        for local in ("workflowstats", "codeowners", "labeltaxonomy"):
            self.assertIsNone(
                graphstore.get_node(self.conn, "acme/widget#" + local), local)

    def test_singleton_facts_persist_and_roundtrip(self):
        # fold a bundle carrying all four singleton facts; each lands as a node.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = _fold_fixture_bundle()
        bundle["workflow_stats"] = {"CI": {"total": 2, "success": 2}}
        bundle["code_owners"] = {"src/": ["alice"]}
        bundle["label_taxonomy"] = {"area": {"area:": ["area: net"]},
                                    "source": "auto"}
        gather.fold_bundle(conn, bundle)
        for local, key in (("workflowstats", "workflow_stats"),
                           ("codeowners", "code_owners"),
                           ("labeltaxonomy", "label_taxonomy")):
            node = graphstore.get_node(conn, "acme/widget#" + local)
            self.assertIsNotNone(node, local)
            self.assertEqual(node["data"], bundle[key], local)
        # empty workflow_stats must NOT create a node (no fabricated key).
        bundle["workflow_stats"] = {}
        conn2 = graphstore.open_store(":memory:")
        graphstore.init_schema(conn2)
        gather.fold_bundle(conn2, bundle)
        self.assertIsNone(graphstore.get_node(conn2, "acme/widget#workflowstats"))

    def test_range_query_excludes_structure_with_null_ts(self):
        social_code = graphstore.range_query(
            self.conn, "acme", ["widget"], "2026-01-01", "2026-01-31")
        ids = {n["id"] for n in social_code}
        self.assertIn("acme/widget#pr-10", ids)
        self.assertNotIn("acme/widget#milestone-1", ids)  # NULL ts -> excluded

    def test_requires_owner_and_repo(self):
        with self.assertRaises(ValueError):
            gather.fold_bundle(self.conn, {"meta": {}})


class TestStoreOnly(unittest.TestCase):
    """Phase 7 store-only: `gather --store` is THE deliverable. main folds the
    in-memory bundle into the journey-graph store and writes NO bundle file
    (--out is gone); --store is required."""

    def _run_main(self, extra_args):
        orig = gather.acquire
        gather.acquire = lambda args, env: _fold_fixture_bundle()
        try:
            return gather.main(["--owner", "acme", "--repo", "widget",
                                "--from", "2026-01-01", "--to", "2026-01-31"]
                               + extra_args)
        finally:
            gather.acquire = orig

    def test_main_folds_into_store(self):
        with tempfile.TemporaryDirectory() as d:
            store_path = os.path.join(d, "store.db")
            self._run_main(["--store", store_path])
            self.assertTrue(os.path.exists(store_path))
            conn = graphstore.open_store(store_path)
            try:
                self.assertEqual(graphstore.get_node(
                    conn, "acme/widget#pr-10")["node_class"], "social")
            finally:
                conn.close()
            # store-only: NO bundle JSON file is emitted alongside the store.
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".json")], [])

    def test_main_requires_store(self):
        with self.assertRaises(SystemExit):
            self._run_main([])


def _artifact_fold_bundle():
    """A bundle with file artifacts + symbol_events that yield a confident move.

    code_events introduce file-level artifacts (README/doc); symbol_events drop a
    resource `foo` in a.bicep and re-add it in b.bicep (a unique-name move) plus a
    second change on a separate symbol, so the symbol ledger has multiple entries.
    """
    return {
        "meta": {"owner": "acme", "repo": "widget", "from": "2026-01-01",
                 "to": "2026-01-31"},
        "prs": [], "issues": [], "milestones": [], "releases": [],
        "commits": [],
        "code_events": [
            {"commit": "c1", "author": "alice", "date": "2026-01-05T00:00:00Z",
             "change": "add", "path": "README.md"},
            {"commit": "c2", "author": "bob", "date": "2026-01-09T00:00:00Z",
             "change": "modify", "path": "docs/guide.md"},
        ],
        "symbol_events": [
            {"commit": "c3", "author": "alice", "date": "2026-01-10",
             "path": "a.bicep", "lang": "bicep", "subkind": "resource",
             "name": "foo", "change": "drop",
             "before": "resource foo ...", "after": None},
            {"commit": "c4", "author": "alice", "date": "2026-01-12",
             "path": "b.bicep", "lang": "bicep", "subkind": "resource",
             "name": "foo", "change": "add",
             "before": None, "after": "resource foo ..."},
            {"commit": "c3", "author": "alice", "date": "2026-01-08",
             "path": "a.bicep", "lang": "bicep", "subkind": "param",
             "name": "region", "change": "change",
             "before": "param region string", "after": "param region int"},
        ],
    }


class TestFoldArtifacts(unittest.TestCase):
    """Slice 7b-1 step 2: artifact `code` nodes, the symbol_events ledger, and
    symbol-move edges (replaced_by/identity_from) persisted on the write path."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        self.bundle = _artifact_fold_bundle()
        gather.fold_bundle(self.conn, self.bundle)
        # the canonical artifact records (write path derives the same).
        self.arts = derive.build_artifacts(_artifact_fold_bundle())

    def _qid(self, local):
        return graphstore.qualify_id("acme", "widget", local)

    def test_file_artifact_nodes_persisted(self):
        readme = graphstore.get_node(self.conn, self._qid("art:README.md"))
        self.assertIsNotNone(readme)
        self.assertEqual(readme["node_class"], "code")
        self.assertEqual(readme["data"]["kind"], "readme")
        self.assertEqual(readme["data"]["path"], "README.md")
        # ts is the artifact's last lifecycle event date.
        self.assertEqual(readme["ts"], "2026-01-05T00:00:00Z")
        doc = graphstore.get_node(self.conn, self._qid("art:docs/guide.md"))
        self.assertEqual(doc["data"]["kind"], "doc")

    def test_symbol_artifact_nodes_persisted(self):
        sid = "a.bicep#bicep:resource:foo"
        node = graphstore.get_node(self.conn, self._qid(sid))
        self.assertIsNotNone(node)
        self.assertEqual(node["node_class"], "code")
        self.assertEqual(node["data"]["kind"], "symbol")
        self.assertEqual(node["data"]["name"], "foo")
        self.assertEqual(node["ts"], "2026-01-10")  # last event date

    def test_symbol_event_ledger_retrievable_in_date_order(self):
        sid = self._qid("a.bicep#bicep:param:region")
        evs = graphstore.get_code_events(self.conn, sid)
        self.assertEqual([e["event"] for e in evs], ["change"])
        self.assertEqual(evs[0]["before"], "param region string")
        self.assertEqual(evs[0]["after"], "param region int")
        # the dropped `foo` symbol carries its remove event keyed by its own id.
        foo = graphstore.get_code_events(self.conn, self._qid("a.bicep#bicep:resource:foo"))
        self.assertEqual([e["event"] for e in foo], ["remove"])
        self.assertEqual(foo[0]["before"], "resource foo ...")

    def test_symbol_move_edges_with_confidence(self):
        src = self._qid("a.bicep#bicep:resource:foo")
        dst = self._qid("b.bicep#bicep:resource:foo")
        rep = graphstore.get_edges(self.conn, src, direction="out",
                                   edge_types=["replaced_by"])
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["dst_id"], dst)
        self.assertEqual(rep[0]["data"]["move_confidence"], "medium")
        idf = graphstore.get_edges(self.conn, dst, direction="out",
                                   edge_types=["identity_from"])
        self.assertEqual(len(idf), 1)
        self.assertEqual(idf[0]["dst_id"], src)
        self.assertEqual(idf[0]["data"]["move_confidence"], "medium")

    def test_artifact_substrate_does_not_leak_into_extract(self):
        # extract reconstructs only raw commits/code_events; the artifact `code`
        # nodes and symbol-event ledger rows must NOT pollute either.
        import extract
        b = extract.extract(self.conn, "acme", "widget", "2026-01-01", "2026-01-31")
        self.assertEqual(b["commits"], [])  # no artifact node masquerades as a commit
        paths = {e["path"] for e in b["code_events"]}
        self.assertEqual(paths, {"README.md", "docs/guide.md"})  # no symbol paths
        self.assertFalse(any("#" in e["path"] for e in b["code_events"]))

    def test_idempotent_refold(self):
        n_nodes = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_events = self.conn.execute("SELECT COUNT(*) FROM code_events").fetchone()[0]
        n_edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        gather.fold_bundle(self.conn, _artifact_fold_bundle())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], n_nodes)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM code_events").fetchone()[0], n_events)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], n_edges)


if __name__ == "__main__":
    unittest.main()
