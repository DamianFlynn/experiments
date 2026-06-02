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


if __name__ == "__main__":
    unittest.main()
