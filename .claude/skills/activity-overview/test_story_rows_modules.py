"""Story rows name their module, automation is plumbing, owner response
requests, one module URL (issue #97, defects found on the October
newsletter). Each test reproduces its defect before the fix."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_story import URL, comment, pr, review  # noqa: E402
from test_story_groups import merged_pr, rows, talk, view  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

SHA = "c" * 40


def hist(*rows_):
    return {"collected": True, "base": "main", "prs": list(rows_)}


def hrow(n, paths, at="2026-05-10T00:00:00Z", sha=None):
    return {"number": n, "status": "fetched",
            "commits": [commit(sha or ("%040d" % n), at, "work", paths)]}


def issue(n, title, body="", **kw):
    out = {"number": n, "title": title, "body": body, "author": "rita",
           "url": "{}/issues/{}".format(URL, n), "state": "open",
           "created_at": "2026-05-02T10:00:00Z", "labels": [], "comments_list": []}
    out.update(kw)
    return out


def with_meta(b):
    b["meta"]["clone_sha"] = SHA
    return b


# --- 1. stories.all modules: module areas only, derived when nothing merged --

class TestRowModules(unittest.TestCase):

    def test_merged_code_keeps_module_areas_only(self):
        b = view([merged_pr(1, title="fix(avm/res/a/b): x")])
        b["commits"] = [{"sha": "a" * 40, "message": "fix: x (#1)", "date": "2026-05-20",
                         "committed_at": "2026-05-20T10:00:00Z", "author": "alice",
                         "files": ["avm/res/a/b/main.bicep", ".github/workflows/ci.yml",
                                   "utilities/pipelines/x.ps1"]}]
        r = rows(enriched(b))["train-pr-1"]
        self.assertEqual(r["modules"], ["avm/res/a/b"])
        self.assertEqual(r["modules_source"], "merged_commits")

    def test_open_pr_from_its_own_commit_paths(self):
        b = view([pr(1, title="feat: New module", comments_list=talk(1))],
                 pr_history=hist(hrow(1, ["avm/res/n/new/main.bicep",
                                          ".github/workflows/avm.res.n.new.yml"])))
        r = rows(enriched(b))["train-pr-1"]
        self.assertEqual((r["modules"], r["modules_source"]),
                         (["avm/res/n/new"], "pr_commits"))

    def test_issue_form_module_field(self):
        body = "### Module Name\n\navm/res/net-app/net-app-account\n\n### Description\n\nx"
        b = view([pr(1, title="fix: add site", body="Fixes #5", closes=[5],
                     comments_list=talk(1))],
                 [issue(5, "[AVM Module Issue]: site", body)])
        r = next(x for x in rows(enriched(b)).values())
        self.assertEqual((r["modules"], r["modules_source"]),
                         (["avm/res/net-app/net-app-account"], "issue_form"))

    def test_title_scope(self):
        b = view([pr(1, title="fix(avm/res/sql/server): y", comments_list=talk(1))])
        r = rows(enriched(b))["train-pr-1"]
        self.assertEqual((r["modules"], r["modules_source"]),
                         (["avm/res/sql/server"], "title_scope"))

    def test_a_scope_that_is_not_a_module_is_not_guessed(self):
        b = view([pr(1, title="fix(net-app-account): y", comments_list=talk(1))])
        r = rows(enriched(b))["train-pr-1"]
        self.assertEqual((r["modules"], r["modules_source"]), ([], "none"))

    def test_outcome_rows_carry_modules_and_source(self):
        b = view([pr(1, title="feat: z", comments_list=talk(1))],
                 pr_history=hist(hrow(1, ["avm/res/n/new/main.bicep"])))
        b = enriched(b)
        out = link.slice_train(b, "train-pr-1")["conversation"]["outcome"]
        row = out["items"][0]
        self.assertEqual((row["modules"], row["modules_source"]),
                         (["avm/res/n/new"], "pr_commits"))


# --- 2. plumbing themes list plumbing stories only ----------------------------

class TestThemeStories(unittest.TestCase):

    def test_non_plumbing_trains_are_related_with_their_group(self):
        prs = [merged_pr(1, title="ci: cache"), merged_pr(2, title="feat: thing")]
        b = view(prs, pr_history=hist(hrow(1, [".github/workflows/a.yml"]),
                                      hrow(2, [".github/workflows/b.yml"])))
        b["module_changes"] = {"prs": [{"number": 1, "plumbing": True},
                                       {"number": 2, "plumbing": True}]}
        b["trains"] = [{"id": "train-pr-1", "prs": [1]}, {"id": "train-pr-2", "prs": [2]}]
        b["stories"] = {"all": [{"id": "train-pr-1", "group": "plumbing"},
                                {"id": "train-pr-2", "group": "landed"}]}
        link.build_plumbing_themes(b)
        th = b["plumbing_themes"][0]
        self.assertEqual([p["number"] for p in th["prs"]], [1, 2])   # the PR stays
        self.assertEqual(th["stories"], ["train-pr-1"])
        self.assertEqual(th["related_stories"], [{"id": "train-pr-2", "group": "landed"}])


# --- 3. automation-only trains are plumbing -----------------------------------

class TestAutomationTrains(unittest.TestCase):

    def test_bot_bump_with_no_human_turn_is_plumbing(self):
        b = view([pr(7, title="fix: bump actions/upload-artifact from 5 to 7",
                     author="dependabot[bot]")],
                 pr_history=hist(hrow(7, [".github/workflows/a.yml"])))
        b = enriched(b)
        r = rows(b)["train-pr-7"]
        self.assertEqual((r["group"], r["state"]), ("plumbing", "in_flight"))
        self.assertIn("automation account", r["group_rule"])
        th = {t["id"]: t for t in b["plumbing_themes"]}
        self.assertEqual([p["number"] for p in th["dependency_updates"]["prs"]], [7])

    def test_a_human_turn_keeps_it_a_story(self):
        b = view([pr(7, title="fix: bump x", author="dependabot[bot]",
                     comments_list=[comment("bob", "2026-05-20T00:00:00Z",
                                            "Is this safe?", 3)])],
                 pr_history=hist(hrow(7, [".github/workflows/a.yml"])))
        self.assertEqual(rows(enriched(b))["train-pr-7"]["group"], "in_flight")


# --- 4. automation accounts: account type, registry override, never a name --

class TestAutomationFlag(unittest.TestCase):

    def people_bundle(self, author_type=None):
        p = pr(1, author="agent-x", title="fix: x", comments_list=talk(1))
        if author_type:
            p["author_type"] = author_type
        b = view([p])
        b["people"] = {"agent-x": {"is_bot": False}, "alice": {"is_bot": False},
                       "bob": {"is_bot": False},
                       "dependabot[bot]": {"is_bot": True}}
        return b

    def test_a_name_alone_is_not_automation(self):
        b = enriched(self.people_bundle())
        self.assertNotIn("automation", b["people"]["agent-x"])
        self.assertEqual(b["people"]["dependabot[bot]"]["automation"]["kind"],
                         "app_login")

    def test_the_stored_account_type_flags_it(self):
        b = enriched(self.people_bundle("Bot"))
        self.assertEqual(b["people"]["agent-x"]["automation"], {"kind": "account_type"})

    def test_registry_override_wins(self):
        src = {"registry": {"people": {
            "agent-x": {"login": "agent-x", "tags": ["automation"]},
            "dependabot[bot]": {"login": "dependabot[bot]", "tags": ["not-automation"]}}}}
        reg = link.registry_automation(src)
        self.assertEqual(reg, {"agent-x": True, "dependabot[bot]": False})
        b = self.people_bundle()
        link.annotate_automation(b, reg)
        self.assertEqual(b["people"]["agent-x"]["automation"], {"kind": "registry"})
        self.assertIsNone(b["people"]["dependabot[bot]"]["automation"])
        self.assertFalse(link._is_automation(b, "dependabot[bot]"))
        self.assertTrue(link._is_automation(b, "agent-x"))

    def test_on_behalf_of_from_the_assignment(self):
        b = self.people_bundle("Bot")
        b["pr_activity"] = {"collected": True, "prs": [{"number": 1, "events": [
            {"id": 9, "event": "assigned", "at": "2026-05-01T10:00:00Z",
             "actor": "carol", "assignee": "agent-x"}]}]}
        link.annotate_automation(b, {})
        self.assertEqual(b["people"]["agent-x"]["automation"], {
            "kind": "account_type",
            "on_behalf_of": {"login": "carol", "basis": "assigned", "pr": 1}})


# --- 5. owner response requested ----------------------------------------------

NEEDS = "Needs: Module Owner :mega:"


class TestOwnerResponseRequested(unittest.TestCase):

    def b(self, **kw):
        p = pr(3, title="feat(avm/res/x/y): add z", labels=[NEEDS],
               created_at="2026-05-09T00:00:00Z",
               comments_list=[comment("policy-service[bot]", "2026-05-09T00:05:00Z",
                                      "Calling the module owner(s) @owner1 @owner2 "
                                      "to respond.", 4)], **kw)
        b = view([p])
        b["module_ownership"] = {"collected": True, "events": [], "without_owner": [],
                                 "label_patterns": {"orphaned": ["orphan"]}}
        return b

    def test_event_from_the_label(self):
        b = enriched(self.b())
        ev = [e for e in b["module_ownership"]["events"]
              if e["kind"] == "owner_response_requested"]
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["module"], e["date"], e["via"]),
                         ("avm/res/x/y", "2026-05-09", NEEDS))
        self.assertEqual(e["item"], {"type": "pr", "number": 3,
                                     "url": "{}/pull/3".format(URL)})
        self.assertEqual(e["owners"], ["owner1", "owner2"])

    def test_it_is_not_owner_needed(self):
        b = enriched(self.b())
        self.assertEqual(b["module_ownership"]["without_owner"], [])

    def test_slice_outcome_and_ball(self):
        b = enriched(self.b())
        out = link.slice_train(b, "train-pr-3")["conversation"]["outcome"]
        req = out["owner_response_requested"]
        self.assertEqual(req[0]["waits_on"], "the module owner")
        self.assertEqual(req[0]["owners"], ["owner1", "owner2"])
        self.assertEqual(out["ball"]["rule"], "R7 module owner response requested")
        self.assertEqual(out["ball"]["with"], ["owner1", "owner2"])

    def test_pattern_is_separate_and_configurable(self):
        b = self.b()
        b["module_ownership"]["label_patterns"]["owner_response_requested"] = ["nothing"]
        b = enriched(b)
        self.assertFalse([e for e in b["module_ownership"]["events"]
                          if e["kind"] == "owner_response_requested"])

    def test_a_change_request_outranks_it(self):
        b = self.b(reviews=[review("bob", "2026-05-10T00:00:00Z", "changes_requested",
                                   5, "Fix it.")])
        b = enriched(b)
        out = link.slice_train(b, "train-pr-3")["conversation"]["outcome"]
        self.assertEqual(out["ball"]["rule"], "R4 reviewer moved")


# --- 6. one module URL ---------------------------------------------------------

class TestOneModuleUrl(unittest.TestCase):

    def test_pinned_to_the_clone_sha_everywhere(self):
        b = with_meta(view([pr(1, title="fix(avm/res/a/b): x", comments_list=talk(1))],
                           pr_history=hist(hrow(1, ["avm/res/a/b/main.bicep",
                                                    "avm/res/a/b/tests/e2e/x/main.test.bicep"]))))
        want = "https://github.com/o/r/tree/{}/avm/res/a/b".format(SHA)
        self.assertEqual(link.module_url(b, "avm/res/a/b"), want)
        b["module_ownership"] = {"collected": True, "without_owner": [], "events": [
            {"kind": "orphaned", "module": "avm/res/a/b", "people": [],
             "date": "2026-05-03", "source": {"type": "commit", "sha": "1" * 40},
             "via": "ORPHANED.md"}]}
        b = enriched(b)
        r = rows(b)["train-pr-1"]
        self.assertEqual(r["module_urls"], {"avm/res/a/b": want})
        self.assertEqual(b["module_ownership"]["events"][0]["module_url"], want)
        e = [{"area": "avm/res/a/b"}]
        link._module_links(b, e)
        self.assertEqual(e[0]["url"], want)

    def test_theme_modules(self):
        b = with_meta(view([merged_pr(2, title="test: fixtures")],
                           pr_history=hist(hrow(2, ["avm/res/a/b/tests/e2e/x/main.test.bicep"]))))
        b["module_changes"] = {"prs": [{"number": 2, "plumbing": True}]}
        link.build_plumbing_themes(b)
        self.assertEqual(b["plumbing_themes"][0]["modules"], [
            {"module": "avm/res/a/b",
             "url": "https://github.com/o/r/tree/{}/avm/res/a/b".format(SHA)}])


# --- 7. substance ----------------------------------------------------------------

class TestSubstance(unittest.TestCase):

    def test_three_kinds(self):
        b = view([pr(1, comments_list=talk(1)),
                  pr(2, reviews=[review("bob", "2026-05-20T00:00:00Z", "approved", 7)]),
                  pr(3)])
        r = rows(enriched(b))
        self.assertEqual(r["train-pr-1"]["substance"], "discussion")
        self.assertEqual(r["train-pr-2"]["substance"], "decision_only")
        self.assertEqual(r["train-pr-3"]["substance"], "opening_only")

    def test_automation_comments_are_not_discussion(self):
        b = view([pr(1, comments_list=[comment("bot-x[bot]", "2026-05-20T00:00:00Z",
                                                "Stale.", 3)])])
        self.assertEqual(rows(enriched(b))["train-pr-1"]["substance"], "opening_only")


if __name__ == "__main__":
    unittest.main()
