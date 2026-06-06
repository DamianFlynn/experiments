import contextlib
import copy
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


class TestRunGitDecoding(unittest.TestCase):
    def test_non_utf8_output_is_replaced_not_crashing(self):
        # A real commit patch can carry non-UTF-8 bytes (latin-1 source, binary
        # hunks). run_git must decode with errors="replace" rather than crash the
        # whole gather on a single bad byte. Drive the current Python interpreter
        # (hermetic/portable — no reliance on a shell `printf`) to emit a lone
        # 0xa1 byte (the exact byte that crashed gathering Azure/bicep).
        out = gather.run_git(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xa1')"])
        self.assertIn("�", out)  # decoded to the replacement char, no raise


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

    def test_clone_margin_env_override(self):
        # ACTIVITY_CLONE_MARGIN_DAYS widens the pre-window reach at call time so an
        # in-window commit's parent is captured (recovering boundary-dropped commits).
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"ACTIVITY_CLONE_MARGIN_DAYS": "90"}):
            self.assertEqual(gather._clone_margin_days(), 90)
            cmd = gather.build_clone_cmd("https://github.com/o/r.git",
                                         "2026-05-01", "/tmp/clone")
            # 2026-05-01 shifted back 90 days
            self.assertIn("--shallow-since=2026-01-31", cmd)
        # absent / non-int -> the CLONE_MARGIN_DAYS default (14)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACTIVITY_CLONE_MARGIN_DAYS", None)
            self.assertEqual(gather._clone_margin_days(), gather.CLONE_MARGIN_DAYS)
        with mock.patch.dict(os.environ, {"ACTIVITY_CLONE_MARGIN_DAYS": "oops"}):
            self.assertEqual(gather._clone_margin_days(), gather.CLONE_MARGIN_DAYS)
        # negative -> clamped to 0 (never shifts --shallow-since past from_date)
        with mock.patch.dict(os.environ, {"ACTIVITY_CLONE_MARGIN_DAYS": "-30"}):
            self.assertEqual(gather._clone_margin_days(), 0)
            cmd = gather.build_clone_cmd("https://github.com/o/r.git",
                                         "2026-05-01", "/tmp/clone")
            self.assertIn("--shallow-since=2026-05-01", cmd)  # == from_date, not after

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

    def test_parse_args_manifest_alone_is_accepted(self):
        args = gather.parse_args([
            "--manifest", "workspace/project.json",
            "--store", "workspace/store.db",
        ])
        self.assertEqual(args.manifest, "workspace/project.json")
        self.assertIsNone(args.owner)
        self.assertIsNone(args.repo)

    def test_parse_args_manifest_conflicts_with_single_repo_flags(self):
        for extra in (["--owner", "o"], ["--repo", "r"],
                      ["--from", "2026-05-01"], ["--to", "2026-05-31"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()):
                    gather.parse_args(["--manifest", "workspace/project.json",
                                       "--store", "workspace/store.db", *extra])

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

    # --- Phase 10 slice 1: normalize_review (keep the submissions) -----------
    def test_normalize_review_maps_fields(self):
        raw = {"id": 555, "user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
               "submitted_at": "2026-05-09T10:00:00Z", "body": "needs work",
               "html_url": "https://gh/x/pull/7#pullrequestreview-555"}
        out = gather.normalize_review(raw)
        self.assertEqual(out, {
            "id": 555, "author": "bob", "state": "changes_requested",
            "submitted_at": "2026-05-09T10:00:00Z", "body": "needs work",
            "url": "https://gh/x/pull/7#pullrequestreview-555"})

    def test_normalize_review_handles_missing_user_and_body(self):
        raw = {"id": 1, "state": "APPROVED",
               "submitted_at": "2026-05-10T10:00:00Z", "html_url": "u"}
        out = gather.normalize_review(raw)
        self.assertIsNone(out["author"])
        self.assertIsNone(out["body"])
        self.assertEqual(out["state"], "approved")

    def test_summarize_reviews_output_unchanged_when_normalizing(self):
        # normalize_review must NOT change summarize_reviews' contract.
        raw = [{"id": 1, "user": {"login": "bob"}, "state": "APPROVED",
                "submitted_at": "2026-05-10T10:00:00Z", "html_url": "u"}]
        self.assertEqual(gather.summarize_reviews(raw),
                         {"reviewers": ["bob"], "decision": "approved"})

    # --- Phase 10 slice 1: parse_timeline_lifecycle (allowlist) -------------
    def test_parse_timeline_lifecycle_allowlist_only(self):
        raw = [
            {"id": 1, "event": "reopened", "actor": {"login": "bob"},
             "created_at": "2026-05-01T00:00:00Z", "url": "u1"},
            {"id": 2, "event": "closed", "actor": {"login": "carol"},
             "created_at": "2026-05-02T00:00:00Z", "url": "u2"},
            {"id": 3, "event": "ready_for_review", "actor": {"login": "dan"},
             "created_at": "2026-05-03T00:00:00Z"},
            # NOT in the allowlist -> dropped here (cross-referenced stays on its
            # own path; labeled/assigned/etc are ignored).
            {"id": 4, "event": "cross-referenced",
             "created_at": "2026-05-04T00:00:00Z"},
            {"id": 5, "event": "labeled", "created_at": "2026-05-05T00:00:00Z"},
        ]
        out = gather.parse_timeline_lifecycle(raw)
        self.assertEqual([e["event"] for e in out],
                         ["reopened", "closed", "ready_for_review"])
        self.assertEqual(out[0], {
            "id": 1, "actor": "bob", "event": "reopened",
            "created_at": "2026-05-01T00:00:00Z", "label": None, "url": "u1"})

    def test_parse_timeline_lifecycle_captures_label(self):
        raw = [{"id": 9, "event": "reopened", "actor": {"login": "x"},
                "created_at": "2026-05-01T00:00:00Z",
                "label": {"name": "bug"}}]
        out = gather.parse_timeline_lifecycle(raw)
        self.assertEqual(out[0]["label"], "bug")
        self.assertIsNone(out[0]["url"])  # no url -> synthesized later by fold

    def test_parse_timeline_lifecycle_empty(self):
        self.assertEqual(gather.parse_timeline_lifecycle([]), [])
        self.assertEqual(gather.parse_timeline_lifecycle(None), [])


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

    def test_repo_root_tf_files_collapse_to_one_main_tf_area(self):
        # A Terraform root module keeps many root .tf files; they must form ONE
        # area ("main.tf"), not one area per file (the old fragmentation bug).
        for f in ("main.tf", "variables.tf", "outputs.tf", "locals.tf", "terraform.tf"):
            self.assertEqual(
                gather.classify_code_area(f, gather.DEFAULT_AREA_PATTERNS),
                "main.tf", f)
        areas = gather.build_directory_areas(
            ["main.tf", "variables.tf", "outputs.tf", "locals.tf"],
            gather.DEFAULT_AREA_PATTERNS)["areas"]
        self.assertEqual([a["id"] for a in areas], ["main.tf"])
        self.assertEqual(areas[0]["paths"],
                         ["locals.tf", "main.tf", "outputs.tf", "variables.tf"])

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

    def test_graph_parses_modern_unprefixed_dot(self):
        # Modern `terraform graph` (>=~1.x) omits the `[root] ` node prefix and
        # uses fully-qualified resource node names; pairs must still be recovered.
        dot = (
            'digraph G {\n'
            '  "azurerm_resource_group.this" -> "module.vnet.azapi_resource.vnet";\n'
            '  "module.vnet.azapi_resource.vnet" -> "module.naming.random_string.x";\n'
            '  "module.vnet.azapi_resource.vnet" -> "module.vnet.random_uuid.t";\n'
            '}\n'
        )
        pairs = gather.parse_terraform_graph(dot)
        self.assertIn((None, "vnet"), pairs)          # root -> module.vnet
        self.assertIn(("vnet", "naming"), pairs)       # cross-module edge
        self.assertNotIn(("vnet", "vnet"), pairs)      # same-module self-edge dropped

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

    def test_local_source_to_repo_root_resolves_to_main_tf(self):
        # An example at examples/<name>/ with `source = "../.."` points at the repo
        # ROOT module. It must resolve to the root area id "main.tf" (the same id the
        # root files classify to) -> NOT a phantom "." area (the dangling-edge bug).
        tf = 'module "this" { source = "../.." }\n'
        dot = 'digraph {\n"x" -> "module.this.y"\n}\n'
        edges = gather.build_terraform_edges(
            tf, dot, "examples/default/main.tf", set(), gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["to"], "main.tf")
        self.assertTrue(edges[0]["resolved"])

    def test_local_source_escaping_repo_is_unresolved_not_phantom(self):
        # A source resolving ABOVE the repo root cannot be a repo area -> to=None,
        # resolved=False (no dangling phantom), identity kept in ref.
        tf = 'module "up" { source = "../../.." }\n'
        dot = 'digraph {\n"x" -> "module.up.y"\n}\n'
        edges = gather.build_terraform_edges(
            tf, dot, "examples/default/main.tf", set(), gather.DEFAULT_AREA_PATTERNS)
        self.assertEqual(len(edges), 1)
        self.assertIsNone(edges[0]["to"])
        self.assertFalse(edges[0]["resolved"])
        self.assertEqual(edges[0]["ref"], "../../..")


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

    def test_terraform_module_source_in_sibling_file_resolves(self):
        # An AVM consumer declares its `module` blocks across main.<topic>.tf files,
        # not main.tf. Extraction must read the WHOLE dir, so a source in a sibling
        # file still resolves (the entrypoint-only read missed it -> 0 edges).
        import tempfile
        with tempfile.TemporaryDirectory() as clone:
            with open(os.path.join(clone, "main.tf"), "w") as fh:
                fh.write('terraform { required_version = ">= 1.0" }\n')
            with open(os.path.join(clone, "main.networking.tf"), "w") as fh:
                fh.write('module "vnet" {\n'
                         '  source  = "Azure/avm-res-network-virtualnetwork/azurerm"\n'
                         '  version = "0.7.1"\n}\n')
            dot = ('digraph {\n'
                   '"azurerm_x.a" -> "module.vnet.azurerm_virtual_network.this"\n}\n')

            def run(cmd, **kw):
                return dot if cmd[-1] == "graph" else ""

            cg = {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf",
                 "paths": ["main.tf", "main.networking.tf"], "edges": []}]}
            gather.extract_iac_edges(
                cg, clone,
                which=lambda n: "/usr/bin/terraform" if n == "terraform" else None,
                run=run, read_text=gather._read_text_file)
            edges = cg["areas"][0]["edges"]
            refs = {e["ref"] for e in edges}
            self.assertIn("Azure/avm-res-network-virtualnetwork/azurerm", refs)
            vnet = next(e for e in edges
                        if e["ref"] == "Azure/avm-res-network-virtualnetwork/azurerm")
            self.assertEqual(vnet["version"], "0.7.1")

    def test_terraform_prewarm_warms_all_areas_before_parallel(self):
        # With a shared TF_PLUGIN_CACHE_DIR + parallel workers, EVERY build area is
        # warmed serially BEFORE the per-area builds, so the parallel inits are cache
        # hits — race-free for any provider mix (not just shared-provider members).
        import unittest.mock as mock
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            dot = fh.read()
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            tf = fh.read()
        calls = []
        lock = __import__("threading").Lock()

        def run(cmd, **kw):
            with lock:
                calls.append(cmd)
            return dot if cmd[-1] == "graph" else ""

        cg = {"provider": "directory", "areas": [
            {"id": "modules/a", "label": "a", "paths": ["modules/a/main.tf"],
             "edges": []},
            {"id": "modules/b", "label": "b", "paths": ["modules/b/main.tf"],
             "edges": []},
        ]}
        with mock.patch.dict(os.environ, {"TF_PLUGIN_CACHE_DIR": "/tmp/x"}):
            gather.extract_iac_edges(
                cg, "clone",
                which=lambda n: "/usr/bin/terraform" if n == "terraform" else None,
                run=run, read_text=lambda _p: tf, max_workers=4)
        # both warm-up inits run before ANY graph (the whole prewarm precedes the pool)
        first_graph = next(i for i, c in enumerate(calls) if c[-1] == "graph")
        self.assertGreaterEqual(first_graph, 2)
        self.assertTrue(all(c[-1] == "-input=false" for c in calls[:2]))  # 2 warm-ups
        # 2 warm-up inits + 2 per-area build inits (every area warmed, not just one)
        self.assertEqual(sum(1 for c in calls if "init" in c), 4)

    def test_no_prewarm_without_shared_cache(self):
        # Without TF_PLUGIN_CACHE_DIR there is no race to avoid -> no warm-up.
        import unittest.mock as mock
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            dot = fh.read()
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            tf = fh.read()
        calls = []
        lock = __import__("threading").Lock()

        def run(cmd, **kw):
            with lock:
                calls.append(cmd)
            return dot if cmd[-1] == "graph" else ""

        cg = {"provider": "directory", "areas": [
            {"id": "modules/a", "label": "a", "paths": ["modules/a/main.tf"],
             "edges": []}]}
        env = {k: v for k, v in os.environ.items() if k != "TF_PLUGIN_CACHE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            gather.extract_iac_edges(
                cg, "clone",
                which=lambda n: "/usr/bin/terraform" if n == "terraform" else None,
                run=run, read_text=lambda _p: tf, max_workers=4)
        self.assertEqual(sum(1 for c in calls if "init" in c), 1)  # one area, no warm-up

    def test_scaffold_areas_are_skipped_not_built(self):
        # examples/ and tests/ areas must NOT be built (no terraform run) — they are
        # marked skipped, so the heavy AVM example stacks are never init'd.
        with open(os.path.join(FIX, "terraform_graph_sample.dot")) as fh:
            dot = fh.read()
        with open(os.path.join(FIX, "terraform_source_sample.tf")) as fh:
            tf = fh.read()
        built_dirs = []

        def run(cmd, **kw):
            if cmd[-1] == "graph":
                built_dirs.append(cmd[1])   # the -chdir=... arg
                return dot
            built_dirs.append(cmd[1])
            return ""

        cg = {"provider": "directory", "areas": [
            {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"], "edges": []},
            {"id": "examples/default", "label": "default",
             "paths": ["examples/default/main.tf"], "edges": []},
            {"id": "tests/e2e", "label": "e2e",
             "paths": ["tests/e2e/main.tf"], "edges": []},
        ]}
        cg = gather.extract_iac_edges(
            cg, "clone",
            which=lambda n: "/usr/bin/terraform" if n == "terraform" else None,
            run=run, read_text=lambda _p: tf)
        by_id = {a["id"]: a for a in cg["areas"]}
        self.assertEqual(by_id["main.tf"]["edges_status"], "resolved")
        self.assertEqual(by_id["examples/default"]["edges_status"], "skipped")
        self.assertEqual(by_id["tests/e2e"]["edges_status"], "skipped")
        self.assertEqual(by_id["examples/default"]["edges"], [])
        # terraform was invoked ONLY for the root module, never for scaffold dirs
        self.assertFalse(any("examples/default" in d or "tests/e2e" in d
                             for d in built_dirs))
        self.assertEqual(cg["edge_extraction"]["skipped"], 2)


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


class TestBoundedFileDiff(unittest.TestCase):
    def test_renders_marker_and_sign_lines(self):
        f = gather.parse_unified_diff(TF_DIFF)[0]
        out = gather.bounded_file_diff(f["hunks"])
        self.assertTrue(out.startswith("@@ +1 @@"))
        self.assertIn('-  name = "old"', out)
        self.assertIn('+  name = "new"', out)
        self.assertIn(' resource "azurerm_resource_group" "this" {', out)
        # un-truncated short diff carries no overflow marker
        self.assertNotIn("…[+", out)

    def test_no_hunks_is_none(self):
        self.assertIsNone(gather.bounded_file_diff([]))
        self.assertIsNone(gather.bounded_file_diff([{"new_start": 1, "lines": []}]))

    def test_char_cap_truncates_with_overflow_marker(self):
        lines = [(" ", "x" * 50) for _ in range(40)]
        hunks = [{"new_start": 1, "lines": lines}]
        out = gather.bounded_file_diff(hunks, cap=200, line_cap=999)
        self.assertLessEqual(len(out.split("…[+")[0]), 260)  # body under ~cap
        self.assertRegex(out, r"…\[\+\d+ lines\]")
        # the dropped count is total - kept
        kept = sum(1 for ln in out.split("\n") if ln and not ln.startswith(("@@", "…")))
        dropped = int(out.split("…[+")[1].split(" ")[0])
        self.assertEqual(kept + dropped, 40)

    def test_line_cap_truncates_with_overflow_marker(self):
        lines = [(" ", "ln{}".format(i)) for i in range(50)]
        hunks = [{"new_start": 1, "lines": lines}]
        out = gather.bounded_file_diff(hunks, cap=99999, line_cap=10)
        body = [ln for ln in out.split("\n") if not ln.startswith(("@@", "…"))]
        self.assertEqual(len(body), 10)
        self.assertIn("…[+40 lines]", out)

    def test_multi_hunk_markers(self):
        hunks = [{"new_start": 1, "lines": [("+", "a")]},
                 {"new_start": 9, "lines": [("-", "b")]}]
        out = gather.bounded_file_diff(hunks)
        self.assertEqual(out, "@@ +1 @@\n+a\n@@ +9 @@\n-b")

    def test_huge_first_line_kept_truncated_not_dropped(self):
        # a single body line longer than cap (minified/lockfile) must still yield a
        # (truncated) diff, not silently vanish to None.
        hunks = [{"new_start": 1, "lines": [("+", "z" * 5000)]}]
        out = gather.bounded_file_diff(hunks, cap=200)
        self.assertIsNotNone(out)
        self.assertIn("@@ +1 @@", out)
        self.assertTrue(out.rstrip().endswith("…"))
        self.assertLessEqual(len(out), 200 + 40)  # cap + marker/ellipsis slack

    def test_markers_counted_toward_cap(self):
        # many single-line hunks: marker bytes are bounded too, so the result can't
        # overshoot `cap` on markers alone.
        hunks = [{"new_start": i, "lines": [("+", "x")]} for i in range(100)]
        out = gather.bounded_file_diff(hunks, cap=50, line_cap=999)
        self.assertLessEqual(len(out.split("…[+")[0]), 50 + 20)

    def test_hunk_without_lines_key_does_not_crash(self):
        self.assertIsNone(gather.bounded_file_diff([{"new_start": 1}]))


class TestParseFileDiffs(unittest.TestCase):
    def _raw(self, *diffs, sha="a" * 40):
        header = gather.FIELD_SEP.join([sha, "p", "Alice", "2026-05-03", "subj"])
        return gather.RECORD_SEP + header + "\n" + "".join(diffs)

    def test_one_bounded_diff_per_changed_file(self):
        out = gather.parse_file_diffs(self._raw(BICEP_DIFF, TF_DIFF))
        by_path = {d["path"]: d for d in out}
        self.assertEqual(set(by_path), {"avm/res/foo/main.bicep", "live/prod/main.tf"})
        self.assertTrue(all(d["commit"] == "a" * 40 for d in out))
        self.assertIn("+  name = \"new\"", by_path["live/prod/main.tf"]["hunk"])

    def test_language_agnostic_non_source_file_included(self):
        # README.md yields NO symbol events but DOES get a bounded file diff.
        md = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
              "@@ -1 +1 @@\n-old\n+new\n")
        out = gather.parse_file_diffs(self._raw(md))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["path"], "README.md")
        self.assertIn("+new", out[0]["hunk"])
        # ...while the symbol walk stays empty for it (unchanged behaviour)
        self.assertEqual(gather.parse_symbol_events(self._raw(md)), [])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(gather.parse_file_diffs(""), [])

    def test_merge_commit_no_patch_yields_nothing(self):
        header = gather.FIELD_SEP.join(["b" * 40, "p1 p2", "Bob", "2026-05-04", "merge"])
        self.assertEqual(
            gather.parse_file_diffs(gather.RECORD_SEP + header + "\n"), [])

    def test_parse_patch_events_single_pass_matches_separate(self):
        # acquire walks the patch ONCE via parse_patch_events; it must produce exactly
        # what the two separate parsers do (no double-parse, no behaviour change).
        raw = self._raw(BICEP_DIFF, TF_DIFF)
        sym, diffs = gather.parse_patch_events(raw)
        self.assertEqual(sym, gather.parse_symbol_events(raw))
        self.assertEqual(diffs, gather.parse_file_diffs(raw))


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

    def test_no_review_or_event_nodes_without_data(self):
        # the base fixture carries no reviews/lifecycle -> no review-/event- nodes.
        rows = self.conn.execute(
            "SELECT id FROM nodes WHERE id LIKE '%#review-%' OR id LIKE '%#event-%'"
        ).fetchall()
        self.assertEqual(rows, [])


def _fold_lifecycle_bundle():
    """The base fold fixture, extended with PR review submissions and lifecycle
    events on both a PR and an issue (Phase 10 slice 1)."""
    b = _fold_fixture_bundle()
    b["prs"][0]["reviews"] = [
        {"id": 100, "author": "carol", "state": "changes_requested",
         "submitted_at": "2026-01-06T00:00:00Z", "body": "tweak",
         "url": "https://gh/acme/widget/pull/10#r100"},
        {"id": 101, "author": "carol", "state": "approved",
         "submitted_at": "2026-01-07T00:00:00Z", "body": None,
         "url": "https://gh/acme/widget/pull/10#r101"},
    ]
    b["prs"][0]["lifecycle"] = [
        {"id": 200, "actor": "bob", "event": "ready_for_review",
         "created_at": "2026-01-06T12:00:00Z", "label": None, "url": "u200"},
    ]
    # an issue lifecycle event with NO url -> fold synthesizes a provenance ref.
    b["issues"][0]["lifecycle"] = [
        {"id": 300, "actor": "alice", "event": "reopened",
         "created_at": "2026-01-08T00:00:00Z", "label": None, "url": None},
        {"id": 301, "actor": "alice", "event": "reopened",
         "created_at": "2026-01-09T00:00:00Z", "label": None, "url": None},
    ]
    return b


class TestFoldReviewsAndLifecycle(unittest.TestCase):
    """Phase 10 slice 1: review submissions + lifecycle events as first-class
    social nodes with `part_of` spine edges to their parent pr/issue."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _fold_lifecycle_bundle())

    def test_review_nodes_are_social_with_ts_and_data(self):
        n = graphstore.get_node(self.conn, "acme/widget#review-10-100")
        self.assertEqual(n["node_class"], "social")
        self.assertEqual(n["ts"], "2026-01-06T00:00:00Z")  # submitted_at
        self.assertEqual(n["data"]["author"], "carol")
        self.assertEqual(n["data"]["state"], "changes_requested")
        self.assertEqual(n["data"]["url"],
                         "https://gh/acme/widget/pull/10#r100")

    def test_review_part_of_edge_to_pr(self):
        out = graphstore.get_edges(self.conn, "acme/widget#review-10-101",
                                   direction="out", edge_types=["part_of"])
        self.assertEqual(out[0]["dst_id"], "acme/widget#pr-10")

    def test_pr_lifecycle_event_node_and_edge(self):
        n = graphstore.get_node(self.conn, "acme/widget#event-pr-10-200")
        self.assertEqual(n["node_class"], "social")
        self.assertEqual(n["ts"], "2026-01-06T12:00:00Z")  # created_at
        self.assertEqual(n["data"]["event"], "ready_for_review")
        out = graphstore.get_edges(self.conn, "acme/widget#event-pr-10-200",
                                   direction="out", edge_types=["part_of"])
        self.assertEqual(out[0]["dst_id"], "acme/widget#pr-10")

    def test_issue_lifecycle_event_node_and_edge(self):
        n = graphstore.get_node(self.conn, "acme/widget#event-issue-3-300")
        self.assertEqual(n["node_class"], "social")
        self.assertEqual(n["data"]["event"], "reopened")
        out = graphstore.get_edges(self.conn, "acme/widget#event-issue-3-300",
                                   direction="out", edge_types=["part_of"])
        self.assertEqual(out[0]["dst_id"], "acme/widget#issue-3")

    def test_event_without_url_gets_synthesized_provenance(self):
        # parent issue url is "u/3"; synthesized ref = "<parent>#event-<id>".
        n = graphstore.get_node(self.conn, "acme/widget#event-issue-3-300")
        self.assertEqual(n["data"]["url"], "u/3#event-300")

    def test_review_without_url_gets_synthesized_provenance(self):
        # a review missing its html_url still gets a citable ref (parity with
        # lifecycle events): "<pr url>#pullrequestreview-<id>".
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = _fold_fixture_bundle()  # prs[0] url == "u/10"
        b["prs"][0]["reviews"] = [
            {"id": 555, "author": "carol", "state": "approved",
             "submitted_at": "2026-01-06T00:00:00Z", "body": None, "url": None}]
        gather.fold_bundle(conn, b)
        n = graphstore.get_node(conn, "acme/widget#review-10-555")
        self.assertEqual(n["data"]["url"], "u/10#pullrequestreview-555")

    def test_review_and_events_reachable_over_spine(self):
        # spine pulls reviews/events into the PR/issue train (they are leaves).
        res = graphstore.traverse_spine(self.conn, ["acme/widget#pr-10"])
        self.assertIn("acme/widget#review-10-100", res["reached"])
        self.assertIn("acme/widget#event-pr-10-200", res["reached"])
        res2 = graphstore.traverse_spine(self.conn, ["acme/widget#issue-3"])
        self.assertIn("acme/widget#event-issue-3-300", res2["reached"])

    def test_idempotent_refold(self):
        before_n = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        before_e = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        gather.fold_bundle(self.conn, _fold_lifecycle_bundle())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], before_n)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], before_e)


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


class TestFoldBundleOverride(unittest.TestCase):
    def test_override_project_and_slug_repo(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle(),
                           project="avm-tf", repo="Azure/widget")
        # node ids are qualified with the OVERRIDE project + owner/repo slug
        pr = graphstore.get_node(conn, "avm-tf/Azure/widget#pr-10")
        self.assertEqual(pr["node_class"], "social")
        self.assertEqual(pr["project"], "avm-tf")
        self.assertEqual(pr["repo"], "Azure/widget")
        # spine edge dst is qualified the same way (parse_id splits scope on first /)
        out = graphstore.get_edges(conn, "avm-tf/Azure/widget#pr-10", direction="out")
        self.assertIn(("closes", "avm-tf/Azure/widget#issue-3"),
                      {(e["edge_type"], e["dst_id"]) for e in out})
        # window + clone_sha recorded under the override identity
        self.assertIn({"project": "avm-tf", "repo": "Azure/widget",
                       "from": "2026-01-01", "to": "2026-01-31"},
                      graphstore.get_windows(conn))
        self.assertEqual(graphstore.get_clone_sha(conn, "avm-tf", "Azure/widget"),
                         "deadbeef")

    def test_default_identity_unchanged(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle())  # no override
        self.assertIsNotNone(graphstore.get_node(conn, "acme/widget#pr-10"))


class TestManifestMain(unittest.TestCase):
    def test_member_args_clones_namespace_with_overrides(self):
        base = gather.parse_args([
            "--owner", "x", "--repo", "y", "--from", "a", "--to", "b",
            "--store", "s.db", "--no-clone"])
        member = gather._member_args(
            base, {"owner": "Azure", "repo": "mod-a", "registry": None},
            "2026-03-01", "2026-03-31")
        self.assertEqual(member.owner, "Azure")
        self.assertEqual(member.repo, "mod-a")
        self.assertEqual(getattr(member, "from"), "2026-03-01")
        self.assertEqual(member.to, "2026-03-31")
        # clone_dir is re-derived per member and includes the owner so members
        # sharing a repo name across owners don't collide on disk.
        self.assertEqual(member.clone_dir, "workspace/Azure-mod-a-clone")
        self.assertTrue(member.no_clone)               # other flags carried through

    def test_member_args_clone_dirs_distinct_for_same_repo_name(self):
        base = gather.parse_args([
            "--manifest", "m.json", "--store", "s.db", "--no-clone"])
        a = gather._member_args(
            base, {"owner": "Azure", "repo": "mod", "registry": None}, "x", "y")
        b = gather._member_args(
            base, {"owner": "Contoso", "repo": "mod", "registry": None}, "x", "y")
        self.assertNotEqual(a.clone_dir, b.clone_dir)
        self.assertEqual(a.clone_dir, "workspace/Azure-mod-clone")
        self.assertEqual(b.clone_dir, "workspace/Contoso-mod-clone")

    def test_main_folds_each_member_under_logical_project(self):
        import tempfile
        man = {
            "project": "proj",
            "window": {"from": "2026-01-01", "to": "2026-01-31"},
            "repos": [{"owner": "Azure", "repo": "mod-a"},
                      {"owner": "Azure", "repo": "mod-b"}],
        }
        calls = []

        def fake_acquire(args, env):
            calls.append((args.owner, args.repo))
            b = _fold_fixture_bundle()
            b["meta"] = {**b["meta"], "owner": args.owner, "repo": args.repo,
                         "from": getattr(args, "from"), "to": args.to}
            return b

        with tempfile.TemporaryDirectory() as tmp:
            mpath = os.path.join(tmp, "m.json")
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            store = os.path.join(tmp, "j.db")
            orig = gather.acquire
            gather.acquire = fake_acquire
            try:
                gather.main(["--manifest", mpath, "--store", store])
            finally:
                gather.acquire = orig
            conn = graphstore.open_store(store)
        # both members acquired, folded under the logical project + owner/repo slug
        self.assertEqual(set(calls), {("Azure", "mod-a"), ("Azure", "mod-b")})
        self.assertIsNotNone(graphstore.get_node(conn, "proj/Azure/mod-a#pr-10"))
        self.assertIsNotNone(graphstore.get_node(conn, "proj/Azure/mod-b#pr-10"))

    def test_main_invalid_manifest_leaves_no_store(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mpath = os.path.join(tmp, "m.json")
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump({"project": "p"}, fh)   # no window/repos -> invalid
            store = os.path.join(tmp, "j.db")
            with self.assertRaises(ValueError):
                gather.main(["--manifest", mpath, "--store", store])
            # validation happens before open_store, so no empty DB is left behind
            self.assertFalse(os.path.exists(store))


class TestParseQualifiedRefs(unittest.TestCase):
    def test_closing_keyword_owner_repo_hash(self):
        refs = gather.parse_qualified_refs(
            "Closes Azure/Azure-Verified-Modules#1234 and more")
        self.assertEqual(refs, [{"owner": "Azure", "repo": "Azure-Verified-Modules",
                                 "number": 1234, "kind": "closes", "is_pr": False}])

    def test_full_url_issue_and_pull(self):
        refs = gather.parse_qualified_refs(
            "see https://github.com/Azure/mod-b/issues/7 and "
            "https://github.com/Azure/mod-c/pull/9")
        self.assertEqual(refs, [
            {"owner": "Azure", "repo": "mod-b", "number": 7,
             "kind": "cross_ref", "is_pr": False},
            {"owner": "Azure", "repo": "mod-c", "number": 9,
             "kind": "cross_ref", "is_pr": True},
        ])

    def test_dedup_order_preserving(self):
        refs = gather.parse_qualified_refs(
            "Fixes Azure/a#1\nResolves Azure/a#1\nFixes Azure/b#2")
        self.assertEqual([(r["repo"], r["number"]) for r in refs], [("a", 1), ("b", 2)])

    def test_bare_hash_not_captured(self):
        self.assertEqual(gather.parse_qualified_refs("Closes #5"), [])

    def test_empty(self):
        self.assertEqual(gather.parse_qualified_refs(None), [])


class TestParseTimelineXrefs(unittest.TestCase):
    def _ev(self, full_name, number, is_pr):
        src = {"number": number,
               "repository": {"full_name": full_name},
               "pull_request": ({} if is_pr else None)}
        return {"event": "cross-referenced", "source": {"issue": src}}

    def test_keeps_cross_repo_drops_same_repo(self):
        tl = [self._ev("Azure/other", 7, False),     # cross-repo issue
              self._ev("Azure/self", 3, False),       # same repo -> dropped
              self._ev("Azure/other2", 9, True)]      # cross-repo PR
        out = gather.parse_timeline_xrefs(tl, "Azure/self")
        self.assertEqual(out, [
            {"owner": "Azure", "repo": "other", "number": 7,
             "kind": "cross_ref", "is_pr": False},
            {"owner": "Azure", "repo": "other2", "number": 9,
             "kind": "cross_ref", "is_pr": True},
        ])

    def test_dedup_and_ignores_non_crossref_events(self):
        tl = [self._ev("Azure/other", 7, False),
              self._ev("Azure/other", 7, False),
              {"event": "labeled"}]
        out = gather.parse_timeline_xrefs(tl, "Azure/self")
        self.assertEqual(len(out), 1)

    def test_empty(self):
        self.assertEqual(gather.parse_timeline_xrefs(None, "o/r"), [])


class TestAcquireTimelineXrefs(unittest.TestCase):
    def test_sets_timeline_xrefs_only_when_cross_repo_present(self):
        pr_same = {"number": 1, "crossref_issues": []}
        tl_same = [{"event": "cross-referenced",
                    "source": {"issue": {"number": 5,
                                         "repository": {"full_name": "Azure/self"},
                                         "pull_request": None}}}]
        gather._attach_timeline_xrefs(pr_same, tl_same, "Azure/self")
        self.assertNotIn("timeline_xrefs", pr_same)   # same-repo only -> key absent

        pr_cross = {"number": 2, "crossref_issues": []}
        tl_cross = [{"event": "cross-referenced",
                     "source": {"issue": {"number": 8,
                                          "repository": {"full_name": "Azure/other"},
                                          "pull_request": None}}}]
        gather._attach_timeline_xrefs(pr_cross, tl_cross, "Azure/self")
        self.assertEqual(pr_cross["timeline_xrefs"],
                         [{"owner": "Azure", "repo": "other", "number": 8,
                           "kind": "cross_ref", "is_pr": False}])


def _xrepo_fold_bundle():
    """A PR in member 'Azure/mod-a' that closes an issue in member 'Azure/mod-b'
    and mentions a NON-member 'Other/ext' via a qualified body ref."""
    return {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                 "to": "2026-01-31", "clone_sha": "sha-a"},
        "prs": [{
            "number": 10, "url": "u/10", "state": "closed", "merged": True,
            "merged_at": "2026-01-10T00:00:00Z", "created_at": "2026-01-05T00:00:00Z",
            "closed_at": "2026-01-10T00:00:00Z", "closes": [], "crossref_issues": [],
            "title": "feat: cross-module",
            "body": "Closes Azure/mod-b#3\nalso Closes Other/ext#99",
            "timeline_xrefs": [{"owner": "Azure", "repo": "mod-b", "number": 5,
                                "kind": "cross_ref", "is_pr": True}],
        }],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }


class TestFoldCrossRepoEdges(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        self.members = {"Azure/mod-a", "Azure/mod-b"}
        gather.fold_bundle(self.conn, _xrepo_fold_bundle(),
                           project="proj", repo="Azure/mod-a", members=self.members)

    def test_member_close_becomes_cross_repo_edge(self):
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("closes", "proj/Azure/mod-b#issue-3"), types)

    def test_member_timeline_xref_becomes_cross_repo_pr_edge(self):
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("cross_ref", "proj/Azure/mod-b#pr-5"), types)  # is_pr -> pr-

    def test_non_member_ref_is_external_not_edge(self):
        pr = graphstore.get_node(self.conn, "proj/Azure/mod-a#pr-10")
        self.assertEqual(pr["data"]["external_refs"],
                         [{"repo": "Other/ext", "number": 99, "kind": "closes"}])
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        dsts = {e["dst_id"] for e in out}
        self.assertNotIn("proj/Other/ext#issue-99", dsts)

    def test_cross_repo_train_traverses_repo_boundary(self):
        res = graphstore.traverse_spine(self.conn, ["proj/Azure/mod-b#issue-3"])
        self.assertIn("proj/Azure/mod-a#pr-10", res["reached"])

    def test_single_repo_path_emits_no_external_refs(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle())   # members=None
        pr = graphstore.get_node(conn, "acme/widget#pr-10")
        self.assertNotIn("external_refs", pr["data"])


class TestMultiRepoIntegration(unittest.TestCase):
    def test_two_member_manifest_builds_cross_repo_train(self):
        import tempfile
        man = {
            "project": "avm",
            "window": {"from": "2026-01-01", "to": "2026-01-31"},
            "repos": [{"owner": "Azure", "repo": "mod-a"},
                      {"owner": "Azure", "repo": "mod-b"}],
        }

        def fake_acquire(args, env):
            base = {"meta": {"owner": args.owner, "repo": args.repo,
                             "from": getattr(args, "from"), "to": args.to,
                             "clone_sha": "sha-" + args.repo},
                    "issues": [], "commits": [], "code_events": [],
                    "milestones": [], "releases": [], "code_graph": {"areas": []}}
            if args.repo == "mod-a":
                base["prs"] = [{
                    "number": 10, "url": "u/10", "merged": True,
                    "merged_at": "2026-01-10T00:00:00Z",
                    "created_at": "2026-01-05T00:00:00Z",
                    "closed_at": "2026-01-10T00:00:00Z",
                    "closes": [], "crossref_issues": [],
                    "title": "feat", "body": "Closes Azure/mod-b#3"}]
            else:
                base["prs"] = []
                base["issues"] = [{"number": 3, "url": "u/3", "state": "closed",
                                   "closed_at": "2026-01-08T00:00:00Z",
                                   "updated_at": "2026-01-08T00:00:00Z"}]
            return base

        with tempfile.TemporaryDirectory() as tmp:
            mpath = os.path.join(tmp, "m.json")
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            store = os.path.join(tmp, "j.db")
            orig = gather.acquire
            gather.acquire = fake_acquire
            try:
                gather.main(["--manifest", mpath, "--store", store])
            finally:
                gather.acquire = orig
            conn = graphstore.open_store(store)

        # both windows recorded under the logical project + owner/repo slugs
        windows = graphstore.get_windows(conn)
        self.assertIn({"project": "avm", "repo": "Azure/mod-a",
                       "from": "2026-01-01", "to": "2026-01-31"}, windows)
        self.assertIn({"project": "avm", "repo": "Azure/mod-b",
                       "from": "2026-01-01", "to": "2026-01-31"}, windows)
        # the cross-repo train: B's issue reaches A's PR over the spine
        res = graphstore.traverse_spine(conn, ["avm/Azure/mod-b#issue-3"])
        self.assertIn("avm/Azure/mod-a#pr-10", res["reached"])


class TestFoldDependsOnFlatten(unittest.TestCase):
    def test_resolved_area_edges_become_store_depends_on(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "modules/app", "label": "app", "paths": ["modules/app/main.tf"],
                 "edges": [{"to": "modules/base", "kind": "module", "ref": "../base",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]},
                {"id": "modules/base", "label": "base",
                 "paths": ["modules/base/main.tf"], "edges": []},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-modules/app",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["dst_id"], "p/Az/r#area-modules/base")
        self.assertEqual(deps[0]["data"].get("transitive"), False)

    def test_unresolved_registry_edge_dropped_in_single_repo(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                 "edges": [{"to": None, "kind": "module",
                            "ref": "Azure/avm-res-keyvault-vault/azurerm",
                            "version": "0.1.0", "transitive": False,
                            "provider": "terraform", "resolved": False}]},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(deps, [])

    def test_transitive_flag_preserved(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "a", "label": "a", "paths": ["a/main.tf"],
                 "edges": [{"to": "b", "kind": "module", "ref": "../b",
                            "version": "2.0", "transitive": True,
                            "provider": "terraform", "resolved": True},
                           {"to": "c", "kind": "module", "ref": "../c",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]},
                {"id": "b", "label": "b", "paths": ["b/main.tf"], "edges": []},
                {"id": "c", "label": "c", "paths": ["c/main.tf"], "edges": []},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-a", direction="out",
                                    edge_types=["depends_on"])
        by_dst = {d["dst_id"]: d for d in deps}
        self.assertEqual(set(by_dst), {"p/Az/r#area-b", "p/Az/r#area-c"})
        self.assertEqual(by_dst["p/Az/r#area-b"]["data"]["transitive"], True)
        self.assertEqual(by_dst["p/Az/r#area-b"]["data"]["version"], "2.0")

    def test_examples_and_tests_areas_are_excluded_from_depends_on(self):
        # examples/ and tests/ are module test/doc scaffolding: their module blocks
        # (here: the example referencing the root module, and a test referencing
        # another registry module) must NOT become stored depends_on edges. The
        # root module's own edge IS kept.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Az/r", "Az/other"}
        registry_by_slug = {"Az/other": "Other/avm-res-thing/azurerm"}
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                 "edges": [{"to": "modules/x", "kind": "module", "ref": "./modules/x",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]},
                {"id": "modules/x", "label": "x", "paths": ["modules/x/main.tf"],
                 "edges": []},
                {"id": "examples/default", "label": "default",
                 "paths": ["examples/default/main.tf"],
                 "edges": [{"to": "main.tf", "kind": "module", "ref": "../..",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]},
                {"id": "tests/e2e", "label": "e2e", "paths": ["tests/e2e/main.tf"],
                 "edges": [{"to": None, "kind": "module",
                            "ref": "Other/avm-res-thing/azurerm", "version": "1.0",
                            "transitive": False, "provider": "terraform",
                            "resolved": False}]},
            ]},
        }
        gather.fold_bundle(conn, bundle, project="proj", repo="Az/r",
                           members=members, registry_by_slug=registry_by_slug)
        # the root module's intra-repo edge survives
        root = graphstore.get_edges(conn, "proj/Az/r#area-main.tf", direction="out",
                                    edge_types=["depends_on"])
        self.assertEqual([d["dst_id"] for d in root], ["proj/Az/r#area-modules/x"])
        # the example's edge to the root module is NOT stored
        ex = graphstore.get_edges(conn, "proj/Az/r#area-examples/default",
                                  direction="out", edge_types=["depends_on"])
        self.assertEqual(ex, [])
        # the test's cross-repo registry ref is NOT resolved into a member edge
        te = graphstore.get_edges(conn, "proj/Az/r#area-tests/e2e",
                                  direction="out", edge_types=["depends_on"])
        self.assertEqual(te, [])

    def test_is_scaffold_area(self):
        self.assertTrue(gather._is_scaffold_area("examples/default"))
        self.assertTrue(gather._is_scaffold_area("tests/e2e"))
        self.assertTrue(gather._is_scaffold_area("modules/x/examples/foo"))
        self.assertFalse(gather._is_scaffold_area("main.tf"))
        self.assertFalse(gather._is_scaffold_area("modules/examples-helper"))
        self.assertFalse(gather._is_scaffold_area(None))

    def test_resolved_true_but_to_none_is_dropped(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "a", "label": "a", "paths": ["a/main.tf"],
                 "edges": [{"to": None, "kind": "module", "ref": "x",
                            "version": None, "transitive": False,
                            "provider": "terraform", "resolved": True}]}]},
        }
        # even WITH members+registry available, a resolved/to=None edge is dropped
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r",
                           members={"Az/r", "Az/other"}, registry_by_slug={})
        self.assertEqual(graphstore.get_edges(conn, "p/Az/r#area-a",
                         direction="out", edge_types=["depends_on"]), [])

    def _consumer_bundle(self):
        # a consumer whose root references a producer registry module (unresolved
        # -> resolves cross-repo via the naming convention)
        return {
            "meta": {"owner": "Az", "repo": "consumer", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                 "edges": [{"to": None, "kind": "module",
                            "ref": "Az/avm-res-thing/azurerm", "version": "1.2.3",
                            "transitive": False, "provider": "terraform",
                            "resolved": False}]}]},
        }

    def test_cross_repo_target_root_node_is_ensured(self):
        # A cross-repo depends_on targets the producer's module-root area. The
        # producer's main.tf is usually NOT an in-window area, so its own fold
        # creates no area-main.tf node. The consumer fold must ENSURE that target
        # node exists, or the edge dangles (validate.referential_integrity).
        # Regression: surfaced when widening the clone margin removed the phantom
        # whole-tree diffs that used to create the producer root area by accident.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Az/consumer", "Az/terraform-azurerm-avm-res-thing"}
        # producer is NOT folded -> its area-main.tf node does not pre-exist
        gather.fold_bundle(conn, self._consumer_bundle(), project="p",
                           repo="Az/consumer", members=members, registry_by_slug={})
        dst = "p/Az/terraform-azurerm-avm-res-thing#area-main.tf"
        deps = graphstore.get_edges(conn, "p/Az/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual([d["dst_id"] for d in deps], [dst])
        node = graphstore.get_node(conn, dst)
        self.assertIsNotNone(node)                      # no dangling edge
        self.assertEqual(node["node_class"], "structure")

    def test_ensured_root_node_does_not_clobber_real_producer_area(self):
        # Create-if-ABSENT only: a real producer area-main.tf (folded in any order)
        # must win over the minimal stand-in.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        dst = "p/Az/terraform-azurerm-avm-res-thing#area-main.tf"
        producer = {
            "meta": {"owner": "Az", "repo": "terraform-azurerm-avm-res-thing",
                     "from": "2026-01-01", "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "root", "paths": ["main.tf"],
                 "edges": []}]},
        }
        gather.fold_bundle(conn, producer, project="p",
                           repo="Az/terraform-azurerm-avm-res-thing")
        real = graphstore.get_node(conn, dst)["data"]
        self.assertNotIn("synthesized", real)
        # consumer folds AFTER, referencing the producer -> must not overwrite
        gather.fold_bundle(
            conn, self._consumer_bundle(), project="p", repo="Az/consumer",
            members={"Az/consumer", "Az/terraform-azurerm-avm-res-thing"},
            registry_by_slug={})
        after = graphstore.get_node(conn, dst)["data"]
        self.assertEqual(after, real)                   # real producer area preserved
        self.assertNotIn("synthesized", after)

    def test_intra_repo_depends_on_target_is_ensured(self):
        # An intra-repo depends_on dst is a real module-area path (a sub-module the
        # window-scoped area provider didn't surface). It IS ensured so the edge is
        # not dangling — this is the avm/res/*/* bicep sub-module case that left
        # ~30 dangling depends_on edges on Azure/bicep-registry-modules.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "milestones": [], "releases": [],
            "code_graph": {"provider": "directory", "areas": [
                {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"], "edges": [
                    {"to": "modules/subnet", "kind": "module", "ref": "./modules/subnet",
                     "version": None, "transitive": False, "provider": "terraform",
                     "resolved": True}]}]},  # 'modules/subnet' area NOT in code_graph
        }
        gather.fold_bundle(conn, bundle, project="p", repo="Az/r")
        deps = graphstore.get_edges(conn, "p/Az/r#area-main.tf", direction="out",
                                    edge_types=["depends_on"])
        self.assertEqual([d["dst_id"] for d in deps], ["p/Az/r#area-modules/subnet"])
        # intra-repo target ensured (minimal structure stand-in -> not dangling)
        node = graphstore.get_node(conn, "p/Az/r#area-modules/subnet")
        self.assertIsNotNone(node)
        self.assertEqual(node["node_class"], "structure")


class TestStructuralTerraformScan(unittest.TestCase):
    FILES = {
        "main.tf": (
            'module "vnet" { source = "Azure/avm-res-network-virtualnetwork/azurerm"\n'
            '  version = "0.16.0" }\n'
            'module "subnet" { source = "./modules/subnet" }\n'),
        "variables.tf": "variable \"x\" {}\n",
        "modules/subnet/main.tf": 'resource "azurerm_subnet" "s" {}\n',
        "examples/default/main.tf": (
            'module "root" { source = "../.." }\n'),   # scaffold -> skipped
        "README.md": "not terraform\n",
    }

    def _scan(self):
        return gather.scan_structural_terraform_areas(
            "/clone",
            list_files=lambda: list(self.FILES),
            read_text=lambda rel: self.FILES[rel])

    def test_whole_tree_areas_and_edges(self):
        out = self._scan()
        # scaffold example area is skipped; module dirs + root are present
        self.assertEqual(set(out), {"main.tf", "modules/subnet"})
        root = {(e["to"], e["ref"], e["version"], e["resolved"]) for e in out["main.tf"]["edges"]}
        self.assertEqual(root, {
            (None, "Azure/avm-res-network-virtualnetwork/azurerm", "0.16.0", False),
            ("modules/subnet", "./modules/subnet", None, True)})
        # every statically-parsed block is a DIRECT edge
        self.assertTrue(all(e["transitive"] is False for e in out["main.tf"]["edges"]))
        self.assertEqual(out["modules/subnet"]["edges"], [])  # no module blocks

    def test_skips_non_tf_and_is_offline(self):
        # README.md never read (only *.tf grouped); no git/terraform invoked
        out = self._scan()
        self.assertNotIn(None, out)
        self.assertTrue(all(p.endswith(".tf") for info in out.values() for p in info["paths"]))

    def test_merge_unions_edges_and_adds_areas(self):
        code_graph = {"provider": "directory", "areas": [
            {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"], "edges": [
                # an in-window DOT edge already present -> preserved, not duplicated
                {"to": "modules/subnet", "kind": "module", "ref": "./modules/subnet",
                 "version": None, "transitive": False, "provider": "terraform",
                 "resolved": True}]}]}
        gather.merge_structural_areas(code_graph, self._scan())
        by_id = {a["id"]: a for a in code_graph["areas"]}
        # new module area appended
        self.assertIn("modules/subnet", by_id)
        # main.tf keeps its 1 DOT edge + gains only the registry edge (no dup of subnet)
        refs = sorted(e["ref"] for e in by_id["main.tf"]["edges"])
        self.assertEqual(refs, ["./modules/subnet",
                                "Azure/avm-res-network-virtualnetwork/azurerm"])


class TestBlocksEdges(unittest.TestCase):
    def test_parse_blocks_refs_directions_and_dedup(self):
        refs = gather.parse_blocks_refs(
            "This blocks #5 and is blocked by #7. Depends on #7. Blocking #9.")
        self.assertEqual(refs, [
            {"number": 5, "direction": "out"},    # blocks #5
            {"number": 7, "direction": "in"},     # blocked by #7 (depends on #7 dedups)
            {"number": 9, "direction": "out"},     # blocking #9
        ])

    def test_parse_blocks_refs_ignores_prose(self):
        # needs a closing '#N' tied to a dependency keyword; loose prose is ignored
        self.assertEqual(gather.parse_blocks_refs("this unblocks the path, see #5"), [])
        self.assertEqual(gather.parse_blocks_refs("blocked yesterday on #5"), [])
        self.assertEqual(gather.parse_blocks_refs("no refs here"), [])

    def _issues_bundle(self, issues):
        return {
            "meta": {"owner": "Az", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [], "commits": [], "code_events": [], "milestones": [],
            "releases": [], "code_graph": {"provider": "directory", "areas": []},
            "issues": issues,
        }

    def test_fold_emits_blocks_between_gathered_issues_only(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        issues = [
            gather.normalize_issue({"number": 1, "title": "A",
                                    "body": "blocked by #2", "html_url": "u"}),
            gather.normalize_issue({"number": 2, "title": "B",
                                    "body": "blocks #3 and depends on #99",
                                    "html_url": "u"}),
            gather.normalize_issue({"number": 3, "title": "C", "body": "",
                                    "html_url": "u"}),
        ]
        gather.fold_bundle(conn, self._issues_bundle(issues), project="p", repo="Az/r")
        got = {(e["src_id"], e["dst_id"])
               for e in graphstore.edges_by_type(conn, "blocks", "p")}
        # #1 blocked-by #2 -> (2 blocks 1); #2 blocks #3 -> (2 blocks 3);
        # #99 is not a gathered issue -> dropped (no dangling edge).
        self.assertEqual(got, {("p/Az/r#issue-2", "p/Az/r#issue-1"),
                               ("p/Az/r#issue-2", "p/Az/r#issue-3")})
        # blocks is a NON-spine relation: traverse_spine must not follow it.
        self.assertNotIn("blocks", graphstore.SPINE_EDGE_TYPES)
        reached = graphstore.traverse_spine(conn, ["p/Az/r#issue-2"])["reached"]
        self.assertNotIn("p/Az/r#issue-3", reached)


class TestRegistryResolution(unittest.TestCase):
    def test_parse_registry_source_plain(self):
        self.assertEqual(
            gather.parse_registry_source("Azure/avm-res-keyvault-vault/azurerm"),
            ("Azure", "avm-res-keyvault-vault", "azurerm"))

    def test_parse_registry_source_with_host_and_submodule(self):
        self.assertEqual(
            gather.parse_registry_source(
                "registry.terraform.io/Azure/avm-res-keyvault-vault/azurerm//sub"),
            ("Azure", "avm-res-keyvault-vault", "azurerm"))

    def test_parse_registry_source_rejects_non_registry(self):
        self.assertIsNone(gather.parse_registry_source("./local"))
        self.assertIsNone(gather.parse_registry_source("two/parts"))

    def test_parse_registry_source_rejects_vcs_host_shorthand(self):
        # A 3-segment source whose first segment is a HOST (contains a dot) is VCS
        # shorthand (github.com/org/repo), NOT a registry triple -> must be None,
        # so it is never mis-resolved to a member via the naming convention.
        self.assertIsNone(gather.parse_registry_source("github.com/Azure/foo"))
        self.assertIsNone(gather.parse_registry_source("example.com/a/b//sub"))
        # a real registry namespace has no dot -> still parses
        self.assertEqual(gather.parse_registry_source("Azure/avm-res-x/azurerm"),
                         ("Azure", "avm-res-x", "azurerm"))

    def test_resolve_exact_registry_match_wins(self):
        members = {"Azure/kv-repo"}
        reg = {"Azure/kv-repo": "Azure/avm-res-keyvault-vault/azurerm"}
        dst = gather.resolve_registry_member(
            "Azure/avm-res-keyvault-vault/azurerm", "p", members, reg)
        self.assertEqual(dst, "p/Azure/kv-repo#area-main.tf")

    def test_resolve_convention_match(self):
        members = {"Azure/terraform-azurerm-avm-res-keyvault-vault"}
        dst = gather.resolve_registry_member(
            "Azure/avm-res-keyvault-vault/azurerm", "p", members, {})
        self.assertEqual(
            dst, "p/Azure/terraform-azurerm-avm-res-keyvault-vault#area-main.tf")

    def test_resolve_no_match_returns_none(self):
        self.assertIsNone(gather.resolve_registry_member(
            "Hashicorp/consul/aws", "p", {"Azure/other"}, {}))

    def test_exact_wins_over_convention_peer(self):
        members = {"Azure/custom-kv",
                   "Azure/terraform-azurerm-avm-res-keyvault-vault"}
        reg = {"Azure/custom-kv": "Azure/avm-res-keyvault-vault/azurerm"}
        dst = gather.resolve_registry_member(
            "Azure/avm-res-keyvault-vault/azurerm", "p", members, reg)
        self.assertEqual(dst, "p/Azure/custom-kv#area-main.tf")  # exact beats convention


class TestCrossRepoDependsOn(unittest.TestCase):
    def _member_a(self):
        return {"meta": {"owner": "Azure", "repo": "consumer", "from": "2026-01-01",
                         "to": "2026-01-31", "base_branch": "main"},
                "prs": [], "issues": [], "commits": [], "code_events": [],
                "milestones": [], "releases": [],
                "code_graph": {"provider": "directory", "areas": [
                    {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                     "edges": [{"to": None, "kind": "module",
                                "ref": "Azure/avm-res-keyvault-vault/azurerm",
                                "version": "0.1.0", "transitive": False,
                                "provider": "terraform", "resolved": False}]}]}}

    def test_fold_emits_cross_repo_depends_on_via_convention(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/consumer",
                   "Azure/terraform-azurerm-avm-res-keyvault-vault"}
        gather.fold_bundle(conn, self._member_a(), project="proj",
                           repo="Azure/consumer", members=members,
                           registry_by_slug={})
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertEqual(
            deps[0]["dst_id"],
            "proj/Azure/terraform-azurerm-avm-res-keyvault-vault#area-main.tf")
        self.assertEqual(deps[0]["data"].get("cross_repo"), True)
        self.assertEqual(deps[0]["data"].get("version"), "0.1.0")

    def test_fold_emits_cross_repo_depends_on_via_exact_registry(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/consumer", "Azure/kv"}
        reg = {"Azure/kv": "Azure/avm-res-keyvault-vault/azurerm"}
        gather.fold_bundle(conn, self._member_a(), project="proj",
                           repo="Azure/consumer", members=members,
                           registry_by_slug=reg)
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["dst_id"], "proj/Azure/kv#area-main.tf")

    def test_fold_drops_registry_edge_when_target_not_a_member(self):
        # the convention target repo is absent from members -> no cross-repo edge.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, self._member_a(), project="proj",
                           repo="Azure/consumer",
                           members={"Azure/consumer", "Azure/unrelated"},
                           registry_by_slug={})
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual(deps, [])


# --- Phase 12 slice 1: Projects v2 board ingest -----------------------------

def _project_board_data():
    """A crafted GraphQL `data` response with TWO boards under one repo:
      - board #1: status-only (a `Status` single-select, NO iteration field).
      - board #2: an iteration board (Status + a ProjectV2IterationField with one
        live + one completed iteration, and an item carrying an iteration value).
    Mirrors the live shapes in the spec. `parse_project_board` consumes the
    `repository.projectsV2.nodes` array (one or many boards)."""
    return {
        "repository": {
            "projectsV2": {
                "nodes": [
                    {
                        "id": "BOARD_status_only",
                        "number": 115,
                        "title": "Bicep",
                        "fields": {"nodes": [
                            {"__typename": "ProjectV2FieldCommon"},
                            {"__typename": "ProjectV2SingleSelectField",
                             "name": "Status"},
                        ]},
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "content": {"__typename": "Issue", "number": 3,
                                                "repository": {"nameWithOwner": "acme/widget"}},
                                    "fieldValues": {"nodes": [
                                        {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                         "name": "Todo",
                                         "field": {"name": "Status"}},
                                    ]},
                                },
                                {
                                    "content": {"__typename": "PullRequest", "number": 10,
                                                "repository": {"nameWithOwner": "acme/widget"}},
                                    "fieldValues": {"nodes": [
                                        # a non-Status single-select must NOT be read as status
                                        {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                         "name": "P1",
                                         "field": {"name": "Priority"}},
                                        {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                         "name": "In Progress",
                                         "field": {"name": "Status"}},
                                    ]},
                                },
                                {
                                    # an item with no Status value -> status None
                                    "content": {"__typename": "Issue", "number": 4,
                                                "repository": {"nameWithOwner": "acme/widget"}},
                                    "fieldValues": {"nodes": []},
                                },
                            ],
                        },
                    },
                    {
                        "id": "BOARD_iter",
                        "number": 200,
                        "title": "Sprints",
                        "fields": {"nodes": [
                            {"__typename": "ProjectV2SingleSelectField",
                             "name": "Status"},
                            {"__typename": "ProjectV2IterationField",
                             "name": "Sprint",
                             "configuration": {
                                 "iterations": [
                                     {"id": "IT_current", "title": "Sprint 5",
                                      "startDate": "2026-01-12", "duration": 14},
                                 ],
                                 "completedIterations": [
                                     {"id": "IT_done", "title": "Sprint 4",
                                      "startDate": "2025-12-29", "duration": 14},
                                 ],
                             }},
                        ]},
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "content": {"__typename": "Issue", "number": 7,
                                                "repository": {"nameWithOwner": "acme/widget"}},
                                    "fieldValues": {"nodes": [
                                        {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                         "name": "In Progress",
                                         "field": {"name": "Status"}},
                                        {"__typename": "ProjectV2ItemFieldIterationValue",
                                         "title": "Sprint 5",
                                         "iterationId": "IT_current"},
                                    ]},
                                },
                            ],
                        },
                    },
                ],
            },
        },
    }


class TestParseProjectBoard(unittest.TestCase):
    """Pure normalization of the GraphQL board response into (sprints, items)."""

    def setUp(self):
        nodes = _project_board_data()["repository"]["projectsV2"]["nodes"]
        self.sprints, self.items = gather.parse_project_board(nodes)

    def test_status_only_board_items(self):
        self.assertEqual(self.items[("acme/widget", 3)]["status"], "Todo")
        self.assertEqual(self.items[("acme/widget", 10)]["status"], "In Progress")
        # status-only board items carry no sprint
        self.assertIsNone(self.items[("acme/widget", 10)]["sprint_id"])

    def test_status_field_name_match(self):
        # PR 10 had a non-Status single-select (Priority=P1) before its Status
        # value; only the field.name=="Status" one wins.
        self.assertEqual(self.items[("acme/widget", 10)]["status"], "In Progress")

    def test_missing_status_value_is_none(self):
        self.assertIsNone(self.items[("acme/widget", 4)]["status"])
        self.assertIsNone(self.items[("acme/widget", 4)]["sprint_id"])

    def test_iterations_unioned_with_end_dates(self):
        # iterations + completedIterations; end = inclusive last day = start+(dur-1).
        self.assertIn("IT_current", self.sprints)
        self.assertIn("IT_done", self.sprints)
        cur = self.sprints["IT_current"]
        self.assertEqual(cur["title"], "Sprint 5")
        self.assertEqual(cur["start"], "2026-01-12")
        self.assertEqual(cur["end"], "2026-01-25")  # inclusive: 2026-01-12 + (14-1)
        self.assertEqual(self.sprints["IT_done"]["end"], "2026-01-11")  # 2025-12-29 +13

    def test_iteration_value_on_item(self):
        self.assertEqual(self.items[("acme/widget", 7)]["sprint_id"], "IT_current")
        self.assertEqual(self.items[("acme/widget", 7)]["status"], "In Progress")

    def test_status_only_board_yields_no_sprints_alone(self):
        # the status-only board (#115) on its own produces an empty sprints dict.
        only = [n for n in
                _project_board_data()["repository"]["projectsV2"]["nodes"]
                if n["id"] == "BOARD_status_only"]
        sprints, items = gather.parse_project_board(only)
        self.assertEqual(sprints, {})
        self.assertEqual(items[("acme/widget", 3)]["status"], "Todo")

    def test_deterministic(self):
        a = gather.parse_project_board(
            _project_board_data()["repository"]["projectsV2"]["nodes"])
        b = gather.parse_project_board(
            _project_board_data()["repository"]["projectsV2"]["nodes"])
        self.assertEqual(a, b)

    def test_tolerates_none_and_missing(self):
        # a node with no fields/items/content must not crash.
        sprints, items = gather.parse_project_board([
            {"id": "B", "number": 1, "title": None,
             "fields": {"nodes": []},
             "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [{"content": None, "fieldValues": {"nodes": []}}]}},
        ])
        self.assertEqual(sprints, {})
        self.assertEqual(items, {})

    def test_item_on_two_boards_merges_status_and_sprint(self):
        # the SAME issue on two boards: one supplies Status, the other a sprint —
        # they MERGE (not clobber). (Finder finding 1.)
        def board(num, item_fvs, fields=None):
            return {"id": "B%d" % num, "number": num, "title": "b%d" % num,
                    "fields": {"nodes": fields or []},
                    "items": {"nodes": [{
                        "content": {"__typename": "Issue", "number": 3,
                                    "repository": {"nameWithOwner": "a/r"}},
                        "fieldValues": {"nodes": item_fvs}}]}}
        status_board = board(1, [{"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                  "name": "Todo", "field": {"name": "Status"}}])
        sprint_board = board(2, [{"__typename": "ProjectV2ItemFieldIterationValue",
                                  "title": "S1", "iterationId": "IT1"}],
                             fields=[{"__typename": "ProjectV2IterationField",
                                      "name": "Sprint", "configuration": {
                                          "iterations": [{"id": "IT1", "title": "S1",
                                                          "startDate": "2026-01-01",
                                                          "duration": 14}],
                                          "completedIterations": []}}])
        sprints, items = gather.parse_project_board([status_board, sprint_board])
        self.assertEqual(items[("a/r", 3)], {"status": "Todo", "sprint_id": "IT1"})


def _project_fold_bundle():
    """The base fold fixture + a parsed project board: sprints + per-item
    board_status/iteration already stamped onto the pr/issue records (acquire's
    job; here we emulate it so fold's behavior is what's under test)."""
    b = _fold_fixture_bundle()
    b["sprints"] = {
        "IT_current": {"title": "Sprint 5", "start": "2026-01-12", "end": "2026-01-25"},
    }
    # PR 10 -> in-progress + current sprint; issue 3 -> a status, no sprint.
    b["prs"][0]["board_status"] = "In Progress"
    b["prs"][0]["iteration"] = "IT_current"
    b["issues"][0]["board_status"] = "Todo"
    return b


class TestFoldProjectBoard(unittest.TestCase):
    """Phase 12 slice 1: sprint structure nodes, in_iteration edges, board_status."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _project_fold_bundle())

    def test_sprint_structure_node(self):
        n = graphstore.get_node(self.conn, "acme/widget#sprint-IT_current")
        self.assertIsNotNone(n)
        self.assertEqual(n["node_class"], "structure")
        self.assertEqual(n["ts"], "2026-01-12")  # ts = start
        self.assertEqual(n["data"], {"title": "Sprint 5", "start": "2026-01-12",
                                     "end": "2026-01-25"})

    def test_in_iteration_edge(self):
        out = graphstore.get_edges(self.conn, "acme/widget#pr-10",
                                   direction="out", edge_types=["in_iteration"])
        self.assertEqual([e["dst_id"] for e in out],
                         ["acme/widget#sprint-IT_current"])
        # issue with a status but no iteration -> no in_iteration edge
        self.assertEqual(graphstore.get_edges(
            self.conn, "acme/widget#issue-3", edge_types=["in_iteration"]), [])

    def test_board_status_stamped_on_node(self):
        pr = graphstore.get_node(self.conn, "acme/widget#pr-10")
        self.assertEqual(pr["data"].get("board_status"), "In Progress")
        iss = graphstore.get_node(self.conn, "acme/widget#issue-3")
        self.assertEqual(iss["data"].get("board_status"), "Todo")

    def test_omit_when_empty(self):
        # the base fixture has no project board -> NO sprint nodes / in_iteration.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle())
        rows = conn.execute(
            "SELECT id FROM nodes WHERE id LIKE '%#sprint-%'").fetchall()
        self.assertEqual(rows, [])
        rows = conn.execute(
            "SELECT * FROM edges WHERE edge_type='in_iteration'").fetchall()
        self.assertEqual(rows, [])

    def test_idempotent_refold(self):
        before = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        before_e = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        gather.fold_bundle(self.conn, _project_fold_bundle())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], before_e)

    def test_in_iteration_edge_dropped_when_sprint_absent(self):
        # an item iteration whose sprint_id is not in bundle["sprints"] (e.g.
        # windowed out) must NOT leave a dangling edge.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = _project_fold_bundle()
        b["prs"][0]["iteration"] = "IT_unknown"
        gather.fold_bundle(conn, b)
        self.assertEqual(graphstore.get_edges(
            conn, "acme/widget#pr-10", edge_types=["in_iteration"]), [])


class TestFetchProjectBoard(unittest.TestCase):
    """The acquire-level board fetch seam: auto-discovery + pagination via an
    injected graphql callable, degrading cleanly on errors / no board."""

    def test_happy_path_parses_and_paginates(self):
        # first page returns one board with hasNextPage=True; second page closes it.
        page1 = copy.deepcopy(_project_board_data())
        b0 = page1["repository"]["projectsV2"]["nodes"][0]
        b0["id"] = "BOARD1"
        b0["items"]["pageInfo"] = {"hasNextPage": True, "endCursor": "C1"}
        first_item = b0["items"]["nodes"][:1]
        rest_items = b0["items"]["nodes"][1:]
        b0["items"]["nodes"] = first_item
        # only keep board #1 (status-only) for the pagination case to keep it simple
        page1["repository"]["projectsV2"]["nodes"] = [b0]

        calls = []

        def fake_graphql(query, variables=None):
            calls.append(variables)
            # board-scoped pagination (PROJECT_BOARD_ITEMS_QUERY: node(id)) -> the rest
            if variables and variables.get("id") is not None:
                self.assertEqual(variables["cursor"], "C1")  # primary's own cursor
                return {"node": {"items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": copy.deepcopy(rest_items)}}}
            # discovery / first page -> page1 (board, hasNextPage)
            return copy.deepcopy(page1)

        sprints, items = gather.fetch_project_board(fake_graphql, "acme", "widget")
        self.assertEqual(sprints, {})
        self.assertEqual(items[("acme/widget", 3)]["status"], "Todo")
        self.assertEqual(items[("acme/widget", 4)]["status"], None)
        self.assertGreaterEqual(len(calls), 2)  # paginated

    def test_degrades_on_graphql_errors(self):
        def boom(query, variables=None):
            raise SystemExit("error: GraphQL errors: missing read:project scope")

        sprints, items = gather.fetch_project_board(boom, "acme", "widget")
        self.assertEqual(sprints, {})
        self.assertEqual(items, {})

    def test_degrades_when_no_board(self):
        def none_board(query, variables=None):
            return {"repository": {"projectsV2": {"nodes": []}}}

        sprints, items = gather.fetch_project_board(none_board, "acme", "widget")
        self.assertEqual(sprints, {})
        self.assertEqual(items, {})

    def test_degrades_on_null_repository(self):
        def null_repo(query, variables=None):
            return {"repository": None}

        sprints, items = gather.fetch_project_board(null_repo, "acme", "widget")
        self.assertEqual((sprints, items), ({}, {}))

    def test_item_cap_is_defensive(self):
        # a board that always claims another page must terminate at the cap.
        def _item(n):
            return {"content": {"__typename": "Issue", "number": n,
                                "repository": {"nameWithOwner": "acme/widget"}},
                    "fieldValues": {"nodes": []}}
        runaway_page = {"pageInfo": {"hasNextPage": True, "endCursor": "C"},
                        "nodes": [_item(1)]}

        def runaway(query, variables=None):
            if variables and variables.get("id") is not None:   # node-scoped paging
                return {"node": {"items": dict(runaway_page,
                                               nodes=[_item(variables["cursor"] and 2)])}}
            return {"repository": {"projectsV2": {"nodes": [{
                "id": "B", "number": 1, "title": "x", "fields": {"nodes": []},
                "items": runaway_page}]}}}

        sprints, items = gather.fetch_project_board(
            runaway, "acme", "widget", max_items=5)
        self.assertEqual(sprints, {})
        self.assertTrue(len(items) <= 5)  # terminated rather than looping forever

    def test_multiple_boards_ingests_only_primary(self):
        # a repo linking >1 board ingests only the lowest-numbered (primary) one.
        def board(num, item_number, status):
            return {"id": "B%d" % num, "number": num, "title": "b%d" % num,
                    "fields": {"nodes": []},
                    "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                              "nodes": [{
                                  "content": {"__typename": "Issue", "number": item_number,
                                              "repository": {"nameWithOwner": "acme/widget"}},
                                  "fieldValues": {"nodes": [
                                      {"__typename": "ProjectV2ItemFieldSingleSelectValue",
                                       "name": status, "field": {"name": "Status"}}]}}]}}
        data = {"repository": {"projectsV2": {"nodes": [
            board(5, 50, "Done"), board(2, 20, "Todo")]}}}  # primary = #2
        sprints, items = gather.fetch_project_board(
            lambda q, v=None: copy.deepcopy(data), "acme", "widget")
        self.assertIn(("acme/widget", 20), items)         # primary board #2
        self.assertNotIn(("acme/widget", 50), items)      # #5 ignored


if __name__ == "__main__":
    unittest.main()
