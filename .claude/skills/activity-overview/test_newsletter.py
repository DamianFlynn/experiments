"""The newsletter layout (reference/newsletter.md): the ordered, pre-selected
material per section of a release-notes style issue (`newsletter.outline`)
and the mechanical check of a written issue against its view
(`newsletter.check_newsletter`). Both read a linked view and never write it."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import newsletter  # noqa: E402
from test_story import bundle, comment, enriched, pr, review  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
R = "https://github.com/o/r"


def src(n, path="avm/res/a/b/CHANGELOG.md"):
    return [{"commit": "c%d" % n, "pr": n, "url": "%s/pull/%d" % (R, n),
             "path": path}]


def module(area, score, **kw):
    out = {"area": area, "kind": "resource", "status": "changed",
           "product_score": score, "prs": [], "plumbing_only": score == 0}
    out.update(kw)
    return out


def view(**over):
    """A small linked view holding just what the newsletter reads."""
    v = {
        "meta": {"owner": "o", "repo": "r", "from": "2026-10-01",
                 "to": "2026-10-31", "ref_date": "2026-10-31",
                 "period": {"from": "2026-10-01", "to": "2026-10-31"},
                 "base_branch": "main"},
        "prs": [
            {"number": 10, "title": "feat: git config object", "author": "tomaz",
             "url": R + "/pull/10", "state": "closed", "merged": True,
             "merged_by": "alex", "created_at": "2026-10-02T10:00:00Z",
             "merged_at": "2026-10-05T10:00:00Z"},
            {"number": 11, "title": "feat: add redis zone", "author": "nik",
             "url": R + "/pull/11", "state": "closed", "merged": True,
             "merged_by": "alex", "created_at": "2026-10-03T10:00:00Z",
             "merged_at": "2026-10-06T10:00:00Z"},
            {"number": 12, "title": "ci: bump action", "author": "jared",
             "url": R + "/pull/12", "state": "closed", "merged": True,
             "merged_by": "jared", "created_at": "2026-10-04T10:00:00Z",
             "merged_at": "2026-10-04T12:00:00Z"},
            {"number": 13, "title": "feat: new module", "author": "nik",
             "url": R + "/pull/13", "state": "closed", "merged": True,
             "merged_by": "alex", "created_at": "2026-10-07T10:00:00Z",
             "merged_at": "2026-10-09T10:00:00Z"},
        ],
        "issues": [{"number": 5, "title": "Git config lost", "author": "kim",
                    "url": R + "/issues/5", "state": "closed",
                    "created_at": "2026-10-01T09:00:00Z"}],
        "trains": [
            {"id": "train-issue-5", "outcome": "shipped", "root_issue": 5,
             "issues": [5], "prs": [10], "code_areas": ["avm/res/data/factory"],
             "story": True, "story_rank": 1, "story_score": 30},
            {"id": "train-pr-11", "outcome": "shipped", "issues": [],
             "prs": [11], "code_areas": ["avm/ptn/net/dns"],
             "story": True, "story_rank": 2, "story_score": 20},
            {"id": "train-pr-12", "outcome": "shipped", "issues": [],
             "prs": [12], "code_areas": [".github"]},
            {"id": "train-pr-13", "outcome": "shipped", "issues": [],
             "prs": [13], "code_areas": ["avm/res/new/thing"]},
        ],
        "stories": {"selected": [{"id": "train-issue-5", "rank": 1, "score": 30},
                                 {"id": "train-pr-11", "rank": 2, "score": 20}],
                    "more": []},
        "module_changes": {
            "summary": {
                "modules": 4,
                "new_modules": [{"area": "avm/res/new/thing", "kind": "resource",
                                 "first_version": "0.1.0",
                                 "introduced_by": [13], "prs": [13]}],
                "new_child_modules": [],
                "declared_breaking": [{"area": "avm/res/data/factory",
                                       "versions": ["0.13.0"], "items": 1}],
                "breaking_candidates": [
                    {"area": "avm/res/data/factory", "rules": ["parameter_removed"]},
                    {"area": "avm/res/hci/cluster", "rules": ["parameter_removed"]}],
                "deprecations": [], "orphaned": [], "removed": [],
                "plumbing": {"merged_prs": 4, "classified": 4, "plumbing_prs": 1,
                             "share": 0.25, "by_class": {"ci": 1, "module": 3}},
            },
            "modules": [
                module("avm/ptn/net/dns", 2, kind="pattern",
                       versions={"changelog": ["0.7.4"],
                                 "published": [{"version": "0.7.4",
                                                "tag": "avm/ptn/net/dns/0.7.4"}]},
                       changelog=[{"version": "0.7.4", "sections": [
                           {"heading": "Changes", "kind": "changed",
                            "items": [{"text": "Added Redis zone `privatelink.redis.azure.net`"}]}],
                           "sources": src(11)}],
                       prs=[{"number": 11, "url": R + "/pull/11",
                             "title": "feat: add redis zone", "author": "nik"}]),
                module("avm/res/data/factory", 40,
                       versions={"version_file": {"from": "0.12", "to": "0.13"},
                                 "changelog": ["0.13.0"],
                                 "published": [{"version": "0.13.0",
                                                "tag": "avm/res/data/factory/0.13.0"},
                                               {"version": "0.1.2", "module":
                                                "avm/res/data/factory/pipe",
                                                "tag": "x/0.1.2"}]},
                       changelog=[{"version": "0.13.0", "sections": [
                           {"heading": "Changes", "kind": "changed", "items": [
                               {"text": "Replaced the flat `git*` parameters with `gitConfiguration`."}]},
                           {"heading": "Breaking Changes", "kind": "breaking", "items": [
                               {"text": "The parameters `gitRepoType` and `gitAccountName` were removed."}]}],
                           "sources": src(10)}],
                       breaking={"declared": [
                           {"version": "0.13.0", "text": "The parameters `gitRepoType` and `gitAccountName` were removed.",
                            "sources": src(10)}],
                           "candidates": [{"rule": "parameter_removed",
                                           "subject": "gitRepoType",
                                           "sources": src(10, "avm/res/data/factory/main.bicep")}]},
                       prs=[{"number": 10, "url": R + "/pull/10",
                             "title": "feat: git config object", "author": "tomaz"}]),
                module("avm/res/hci/cluster", 3,
                       versions={"changelog": ["0.3.0"]},
                       changelog=[{"version": "0.3.0", "sections": [
                           {"heading": "Changes", "kind": "changed", "items": [
                               {"text": "Updated API versions."}]}], "sources": src(12)}],
                       breaking={"candidates": [{"rule": "parameter_removed",
                                                 "subject": "hciSecurityProfile",
                                                 "sources": src(12)}]},
                       prs=[]),
                module("avm/res/key/vault", 0, plumbing_only=True,
                       prs=[{"number": 12, "url": R + "/pull/12",
                             "title": "ci: bump action", "author": "jared"}]),
                module("avm/res/new/thing", 5, status="new", first_version="0.1.0",
                       versions={"changelog": ["0.1.0"],
                                 "published": [{"version": "0.1.0", "tag": "t/0.1.0"}]},
                       changelog=[{"version": "0.1.0", "sections": [
                           {"heading": "Changes", "kind": "changed", "items": [
                               {"text": "Initial version: deploys a thing."}]}],
                           "sources": src(13)}],
                       prs=[{"number": 13, "url": R + "/pull/13",
                             "title": "feat: new module", "author": "nik"}]),
            ],
            "prs": [
                {"number": 10, "url": R + "/pull/10", "title": "feat: git config object",
                 "class": "module", "plumbing": False, "areas": ["avm/res/data/factory"]},
                {"number": 11, "url": R + "/pull/11", "title": "feat: add redis zone",
                 "class": "module", "plumbing": False, "areas": ["avm/ptn/net/dns"]},
                {"number": 12, "url": R + "/pull/12", "title": "ci: bump action",
                 "class": "ci", "plumbing": True, "areas": [".github", "avm/res/key/vault"]},
                {"number": 13, "url": R + "/pull/13", "title": "feat: new module",
                 "class": "module", "plumbing": False, "areas": ["avm/res/new/thing"]},
            ],
            "inputs": {"tags_collected": True},
        },
        "module_notes": {"collected": True, "tags": {"collected": True, "items": [
            {"tag": "avm/ptn/net/dns/0.7.4", "module": "avm/ptn/net/dns", "version": "0.7.4"},
            {"tag": "avm/res/data/factory/0.13.0", "module": "avm/res/data/factory",
             "version": "0.13.0"}]}},
        "affiliations": {"collected": True, "home_orgs": ["o", "microsoft"],
                         "home_names": ["o", "microsoft"], "people": {
            "alex": {"affiliation": "maintainer_org", "signal": "public_membership",
                     "name": "Alex Sehr", "maintainer": True},
            "jared": {"affiliation": "maintainer_org", "signal": "public_membership",
                      "name": "Jared Holt", "maintainer": True},
            "nik": {"affiliation": "unknown", "signal": "none", "name": "Nik Peters"},
            "tomaz": {"affiliation": "community", "signal": "profile_company",
                      "strength": "weak", "name": "Tomaz M"},
            "kim": {"affiliation": "unknown", "signal": "do_not_name"},
            "sam": {"affiliation": "community", "signal": "profile_company",
                    "strength": "weak", "name": "Sam Smith"}}},
        "people": {
            "alex": {"is_bot": False, "prs_reviewed": 3, "prs_merged": 0,
                     "affiliation": "maintainer_org", "name": "Alex Sehr"},
            "jared": {"is_bot": False, "prs_reviewed": 0, "prs_merged": 1,
                      "affiliation": "maintainer_org", "name": "Jared Holt"},
            "nik": {"is_bot": False, "prs_reviewed": 0, "prs_merged": 2,
                    "affiliation": "unknown", "name": "Nik Peters"},
            "tomaz": {"is_bot": False, "prs_reviewed": 0, "prs_merged": 1,
                      "affiliation": "community", "name": "Tomaz M"},
            "kim": {"is_bot": False, "affiliation": "unknown"},
            "sam": {"is_bot": False, "affiliation": "community", "name": "Sam Smith"},
        },
        "newcomers": {"collected": True, "before": "2026-10-01", "candidates": 4,
                      "new": [], "unknown": [], "not_checked": []},
        "contributors": {
            "affiliation": {
                "people": {"maintainer_org": 2, "community": 2, "unknown": 2},
                "merged_prs": {"maintainer_org": {"count": 1, "share": 0.25},
                               "community": {"count": 1, "share": 0.25},
                               "unknown": {"count": 2, "share": 0.5},
                               "total": 4, "bots_excluded": 0},
                "reviews": {"maintainer_org": {"count": 3, "share": 1.0},
                            "community": {"count": 0, "share": 0.0},
                            "unknown": {"count": 0, "share": 0.0},
                            "total": 3, "bots_excluded": 0},
                "maintainers": {"logins": ["alex", "jared"]}},
            "community": {"people": 2, "prs_merged": 1, "prs_merged_numbers": [10]},
            "newcomers": [
                {"login": "nik", "class": "first_ever", "name": "Nik Peters",
                 "affiliation": "unknown", "affiliation_signal": "none",
                 "first": {"first": "pr", "number": 11, "url": R + "/pull/11",
                           "created_at": "2026-10-03T10:00:00Z"},
                 "contributed": {"prs": [
                     {"number": 13, "url": R + "/pull/13", "title": "feat: new module",
                      "state": "merged", "class": "module", "headline": "new_module",
                      "modules": [{"area": "avm/res/new/thing", "role": "introduced"}]},
                     {"number": 11, "url": R + "/pull/11", "title": "feat: add redis zone",
                      "state": "merged", "class": "module", "headline": "module_change",
                      "modules": [{"area": "avm/ptn/net/dns", "role": "changed"}]}]}},
                {"login": "kim", "class": "first_ever", "affiliation": "unknown",
                 "affiliation_signal": "do_not_name",
                 "first": {"first": "issue", "number": 5, "url": R + "/issues/5",
                           "created_at": "2026-10-01T09:00:00Z"},
                 "contributed": {"prs": []}},
                {"login": "sam", "class": "returning", "name": "Sam Smith",
                 "affiliation": "community", "affiliation_signal": "profile_company",
                 "gap_days": 200, "gap_bucket": "over_6_months",
                 "first": {"first": "issue", "number": 5, "url": R + "/issues/5",
                           "created_at": "2026-10-01T09:00:00Z"},
                 "contributed": {"prs": [{"number": 99, "url": R + "/pull/99",
                                          "title": "fix: x", "state": "open",
                                          "headline": "unknown"}]}},
            ],
            "hall": [{"login": "jared", "score": 3, "affiliation": "maintainer_org",
                      "name": "Jared Holt",
                      "contributed": {"prs": [{"number": 12, "state": "merged",
                                               "headline": "plumbing", "class": "ci"}]},
                      "reviewed": {"prs": 0, "modules": []}}],
        },
    }
    v.update(over)
    return v


def section(out, sid):
    return next(s for s in out["sections"] if s["id"] == sid)


# --- the outline ------------------------------------------------------------

class TestOutline(unittest.TestCase):

    def test_sections_in_order_with_headings(self):
        out = newsletter.outline([view()])
        self.assertEqual([s["id"] for s in out["sections"]],
                         list(newsletter.SECTION_ORDER))
        self.assertEqual(out["kind"], "newsletter_outline")
        self.assertEqual(out["period_label"], "October 2026")
        self.assertEqual(out["cadence"], "month")

    def test_breaking_separates_declared_from_candidates(self):
        b = section(newsletter.outline([view()]), "breaking")
        self.assertIsNone(b["omit"])
        self.assertEqual([d["area"] for d in b["declared"]], ["avm/res/data/factory"])
        self.assertEqual(b["declared"][0]["items"][0]["text"],
                         "The parameters `gitRepoType` and `gitAccountName` were removed.")
        self.assertTrue(b["declared"][0]["items"][0]["published"])
        # a candidate whose subject the declared text already names is covered
        cands = {c["area"]: c for c in b["candidates"]}
        self.assertTrue(cands["avm/res/data/factory"]["rules"][0]["covered_by_declared"])
        self.assertFalse(cands["avm/res/hci/cluster"]["declared_too"])
        self.assertEqual(cands["avm/res/hci/cluster"]["wording"],
                         "check before upgrading")

    def test_breaking_omitted_with_reason_when_nothing(self):
        v = view()
        for m in v["module_changes"]["modules"]:
            m.pop("breaking", None)
        b = section(newsletter.outline([v]), "breaking")
        self.assertIn("no declared breaking change", b["omit"])

    def test_new_modules_and_their_authors(self):
        n = section(newsletter.outline([view()]), "new_modules")
        self.assertIsNone(n["omit"])
        m = n["modules"][0]
        self.assertEqual((m["area"], m["first_version"]), ("avm/res/new/thing", "0.1.0"))
        self.assertEqual(m["changelog"], ["Initial version: deploys a thing."])
        self.assertEqual(m["delivered_by"][0]["login"], "nik")
        self.assertEqual(m["delivered_by"][0]["name"], "Nik Peters")
        self.assertEqual(m["delivered_by"][0]["affiliation"], "unknown")

    def test_no_new_module_is_an_omission(self):
        v = view()
        v["module_changes"]["summary"]["new_modules"] = []
        n = section(newsletter.outline([v]), "new_modules")
        self.assertIn("no module is new", n["omit"])
        self.assertEqual(n["modules"], [])

    def test_module_changes_lead_by_product_score_and_skip_plumbing_and_new(self):
        mc = section(newsletter.outline([view()]), "module_changes")
        self.assertEqual([m["area"] for m in mc["modules"]],
                         ["avm/res/data/factory", "avm/res/hci/cluster",
                          "avm/ptn/net/dns"])
        self.assertEqual(mc["plumbing_only"], ["avm/res/key/vault"])
        top = mc["modules"][0]
        self.assertEqual(top["version"]["from"], "0.12")
        self.assertEqual(top["version"]["to"], "0.13.0")
        self.assertEqual(top["version"]["published"], ["0.13.0"])
        self.assertEqual(top["version"]["child_published"],
                         [{"module": "avm/res/data/factory/pipe", "version": "0.1.2"}])
        # breaking items stay in the breaking section; changes keep the rest
        self.assertEqual(top["changelog"],
                         [{"version": "0.13.0", "items": [
                             "Replaced the flat `git*` parameters with `gitConfiguration`."]}])
        self.assertEqual(top["breaking"], "declared")
        self.assertEqual(top["story"], "train-issue-5")
        self.assertEqual(mc["modules"][1]["breaking"], "candidate")

    def test_every_changed_module_is_listed_whatever_the_cadence(self):
        # the owner: no length target; the cap is only a safety limit
        v = view()
        for i in range(10):
            v["module_changes"]["modules"].append(module("avm/res/x/m%d" % i, 1))
        month = section(newsletter.outline([v]), "module_changes")
        week = section(newsletter.outline([v], cadence="week"), "module_changes")
        self.assertEqual(len(month["modules"]), 13)
        self.assertEqual(len(week["modules"]), 13)
        self.assertEqual(week["also"], [])
        self.assertEqual(newsletter.CADENCE["month"]["modules"], newsletter.SAFETY_LIMIT)

    def test_every_story_in_full_grouped_by_state(self):
        st = section(newsletter.outline([view()]), "stories")
        # within a group: new, then changed, then breaking (the owner's order);
        # train-issue-5 is about the data factory's declared breaking change
        self.assertEqual([(s["id"], s["story_kind"]) for s in st["full"]],
                         [("train-pr-11", "changed"), ("train-issue-5", "breaking")])
        self.assertEqual(st["full"][1]["title"], "Git config lost")
        self.assertIn("--slice train-issue-5", st["full"][1]["slice"])
        self.assertEqual(st["group_source"], "provisional")
        self.assertEqual([s["id"] for s in st["groups"]["landed"]],
                         ["train-pr-11", "train-issue-5"])
        self.assertNotIn("pointers", st)
        week = section(newsletter.outline([view()], cadence="week"), "stories")
        self.assertEqual([s["id"] for s in week["full"]], ["train-pr-11", "train-issue-5"])
        # the intro's top story stays the highest-scoring one
        lead = section(newsletter.outline([view()]), "intro")
        self.assertEqual(next(f for f in lead["lead_with"] if f["fact"] == "stories")["top_story"],
                         "train-issue-5")

    def test_provisional_groups_and_the_view_groups(self):
        v = view()
        v["trains"].append({"id": "train-pr-20", "outcome": "in_flight", "issues": [],
                            "prs": [10], "story": True, "story_rank": 4})
        v["trains"].append({"id": "train-pr-21", "outcome": "in_flight", "issues": [],
                            "prs": [10], "story": True, "story_rank": 3,
                            "effort": {"stalled": True}})
        v["trains"].append({"id": "train-pr-22", "outcome": "shipped", "issues": [],
                            "prs": [12], "story": True, "story_rank": 5})
        v["stories"]["selected"] += [{"id": "train-pr-21", "rank": 3, "score": 10},
                                     {"id": "train-pr-20", "rank": 4, "score": 9},
                                     {"id": "train-pr-22", "rank": 5, "score": 8}]
        v["trains"].append({"id": "train-pr-23", "outcome": "in_flight", "issues": [],
                            "prs": [10]})
        v["stories"]["quiet"] = [{"id": "train-pr-23", "score": 3, "idle_days": 40}]
        st = section(newsletter.outline([v]), "stories")
        g = {k: [x["id"] for x in rows] for k, rows in st["groups"].items() if rows}
        self.assertEqual(g, {"landed": ["train-pr-11", "train-issue-5"],
                             "in_flight": ["train-pr-20"],
                             "stalled": ["train-pr-21", "train-pr-23"],
                             "plumbing": ["train-pr-22"]})
        # a quiet train is one line (the delivered contract renamed thin -> lines)
        self.assertEqual([(x["id"], x["plan"], x["idle_days"]) for x in st["lines"]],
                         [("train-pr-23", "line", 40)])
        # the view's own grouping wins
        v["stories"]["all"] = [{"id": "train-pr-20", "group": "stalled", "state": "stalled",
                                "group_rule": "idle", "modules": ["avm/res/hci/cluster"],
                                "score": 9, "worthy": "1 human turn(s) in the window"}]
        st = section(newsletter.outline([v]), "stories")
        self.assertEqual(st["group_source"], "view")
        self.assertEqual([x["id"] for x in st["groups"]["stalled"]], ["train-pr-20"])
        self.assertEqual(st["full"][0]["modules"][0]["url"],
                         "https://github.com/o/r/tree/main/avm/res/hci/cluster")

    def test_people_classify_contributions_and_hide_do_not_name(self):
        p = section(newsletter.outline([view()]), "people")
        logins = [r["login"] for r in p["newcomers"]]
        self.assertEqual(logins, ["nik", "sam"])
        self.assertEqual(p["not_named"], 1)
        nik = p["newcomers"][0]
        self.assertEqual(nik["contribution"], "new_module")
        self.assertEqual(nik["merged"][0]["modules"], ["avm/res/new/thing"])
        sam = p["newcomers"][1]
        self.assertEqual(sam["contribution"], "open_pr")
        self.assertEqual(sam["gap_bucket"], "over_6_months")
        self.assertEqual(p["returning_by_gap"], {"over_6_months": 1})
        self.assertEqual(p["affiliation"]["merged_prs"]["unknown"]["share"], 0.5)
        m = {r["login"]: r for r in p["maintainers"]}
        self.assertEqual(m["alex"]["merged_others"], 3)
        self.assertEqual(m["alex"]["reviews"], 3)
        self.assertEqual(m["jared"]["merged_others"], 0)
        self.assertEqual(m["jared"]["authored_by_class"], {"plumbing": 1})
        self.assertEqual(p["top"][0]["merged_by_class"], {"plumbing": 1})
        self.assertEqual(newsletter._merged_by_class(view(), "nik"),
                         {"module_change": 1, "new_module": 1})

    def test_plumbing_section(self):
        pl = section(newsletter.outline([view()]), "plumbing")
        self.assertEqual(pl["share"], 0.25)
        self.assertEqual([p["number"] for p in pl["prs"]], [12])

    def test_numbers_and_coverage(self):
        nb = section(newsletter.outline([view()]), "numbers")
        f = nb["facts"]
        self.assertEqual(f["merged_prs"], 4)
        self.assertEqual(f["plumbing_prs"], 1)
        self.assertEqual(f["plumbing_share_pct"], 25)
        self.assertEqual(f["modules_reader_facing"], 4)
        self.assertEqual(f["versions_published"], 2)
        self.assertEqual((f["people_labelled"], f["people_unknown_pct"]), (6, 33))
        self.assertTrue(any("people registry" in c or "registry" in c
                            for c in nb["coverage"]))

    def test_without_module_layer_the_module_sections_are_omitted(self):
        v = view()
        del v["module_changes"]
        out = newsletter.outline([v])
        for sid in ("breaking", "new_modules", "module_changes", "plumbing"):
            self.assertIn("module layer", section(out, sid)["omit"])

    def test_outline_does_not_modify_the_view(self):
        v = view()
        before = json.dumps(v, sort_keys=True)
        newsletter.outline([v])
        self.assertEqual(json.dumps(v, sort_keys=True), before)

    def test_multi_repo_rows_name_their_repo(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], owner="o", repo="s")
        out = newsletter.outline([a, b])
        mc = section(out, "module_changes")
        self.assertEqual({m["repo"] for m in mc["modules"]}, {"o/r", "o/s"})
        self.assertEqual(out["repos"], ["o/r", "o/s"])
        self.assertEqual(section(out, "numbers")["facts"]["merged_prs"], 8)


class TestComingAndStuck(unittest.TestCase):

    def _view(self):
        b = bundle([
            pr(1, created_at="2026-05-03T10:00:00Z", reviews=[
                review("bob", "2026-05-10T10:00:00Z", "approved", 1)]),
            pr(2, created_at="2026-05-04T10:00:00Z", title="feat: New module `avm/res/x/y`"),
        ])
        return enriched(b)

    def test_approved_and_waiting_for_a_first_review(self):
        c = section(newsletter.outline([self._view()]), "coming")
        self.assertIsNone(c["omit"])
        self.assertEqual([r["number"] for r in c["ready"]], [1])
        self.assertEqual([r["number"] for r in c["needs_first_review"]], [2])
        self.assertEqual(c["counts"]["open_prs_active"], 2)
        r = c["needs_first_review"][0]
        self.assertEqual(r["ball"]["state"], "awaiting a first review")
        self.assertTrue(r["ball"]["derived"])
        self.assertNotIn("with", r["ball"])   # the newsletter names no one
        # a proposed new module comes from the view's `new_module_proposed`,
        # never from a PR title (the owner's update pass)
        self.assertNotIn("title_signal", r)
        self.assertEqual(c["proposed_modules"], [])
        self.assertTrue(c["ready"][0]["approved"])

    def test_omitted_when_nothing_open(self):
        b = enriched(bundle([pr(1, state="closed", merged=True,
                                merged_at="2026-05-05T10:00:00Z")]))
        c = section(newsletter.outline([b]), "coming")
        self.assertIn("no open pull request", c["omit"])


# --- the check --------------------------------------------------------------

T = "https://github.com/o/r/tree/main/"
DNS = "[`avm/ptn/net/dns`](" + T + "avm/ptn/net/dns)"
DNS_V = "[0.7.4](https://github.com/o/r/tree/avm/ptn/net/dns/0.7.4)"

GOOD = """# o/r — October 2026

One new module landed, the data factory module changed how Git is configured
(a breaking change, see below), and the DNS zones pattern gained a zone.

## New modules

- [`avm/res/new/thing`](https://github.com/o/r/tree/main/avm/res/new/thing) [0.1.0](https://github.com/o/r/tree/t/0.1.0), delivered by Nik Peters (@nik): "Initial version: deploys a thing." ([#13](https://github.com/o/r/pull/13))

## What changed in existing modules

- """ + DNS + " " + DNS_V + """: "Added Redis zone `privatelink.redis.azure.net`" ([#11](https://github.com/o/r/pull/11))
- [`avm/res/data/factory`](https://github.com/o/r/tree/main/avm/res/data/factory) [0.13.0](https://github.com/o/r/tree/avm/res/data/factory/0.13.0): "Replaced the flat `git*` parameters with `gitConfiguration`." ([#10](https://github.com/o/r/pull/10))

## Breaking changes and deprecations

- [`avm/res/data/factory`](https://github.com/o/r/tree/main/avm/res/data/factory) [0.13.0](https://github.com/o/r/tree/avm/res/data/factory/0.13.0): "The parameters `gitRepoType` and `gitAccountName` were removed." ([#10](https://github.com/o/r/pull/10))
- [`avm/res/hci/cluster`](https://github.com/o/r/tree/main/avm/res/hci/cluster): a parameter was removed from the module file; check before upgrading.

## Stories

### Landed

<!-- story: train-issue-5 -->
Landed. Something happened.

**What landed.** It landed.
<!-- /story -->

<!-- story pending: train-pr-11 — written from the next store -->

<!-- omitted: ownership — needs a store gathered with module ownership -->

## People

From the community, Tomaz M (@tomaz) landed the Git change on 5 October 2026 ([#10](https://github.com/o/r/pull/10)). It was released as [0.13.0](https://github.com/o/r/tree/avm/res/data/factory/0.13.0).
Nik Peters (@nik), whose affiliation is not known, landed a new module ([#13](https://github.com/o/r/pull/13)), published as [0.1.0](https://github.com/o/r/tree/t/0.1.0).

<!-- omitted: coming — nothing open in the window -->

## Plumbing: what was engineered

### CI workflows and pipeline utilities

A pipeline action was bumped in [#12](https://github.com/o/r/pull/12).

One of the 4 merged pull requests was plumbing (25%).

## By the numbers

4 pull requests merged.
"""


SLICE = {"train": {"id": "train-issue-5", "outcome": "shipped", "evidence": []},
         "conversation": {"timeline": [], "outcome": {"items": []}}}


class TestCheck(unittest.TestCase):

    def check(self, text, **kw):
        kw.setdefault("slices", {"train-issue-5": SLICE})
        return newsletter.check_newsletter([view()], text, **kw)

    def test_good_issue_passes(self):
        r = self.check(GOOD)
        self.assertTrue(r["ok"], json.dumps(r, indent=1))

    def test_unknown_module_version_number_and_person(self):
        t = GOOD.replace(DNS + " " + DNS_V, "`avm/ptn/net/dnz` 0.7.9") \
            .replace("4 pull requests merged.", "47 pull requests merged.") \
            .replace("(@tomaz)", "(@ghost-user)")
        r = self.check(t)
        self.assertFalse(r["ok"])
        self.assertIn("avm/ptn/net/dnz", r["modules_not_in_view"])
        self.assertIn("47", r["numbers_not_in_view"])
        self.assertIn("ghost-user", r["people_not_in_view"])

    def test_version_bound_to_its_module(self):
        t = GOOD.replace(DNS + " " + DNS_V, DNS + " 0.13.0")
        r = self.check(t)
        self.assertIn({"module": "avm/ptn/net/dns", "version": "0.13.0"},
                      r["versions_not_in_view"])

    def test_candidate_worded_as_declared(self):
        t = GOOD.replace("a parameter was removed from the module file; check before upgrading.",
                         "breaking change: the parameter was removed.")
        r = self.check(t)
        self.assertEqual(r["candidates_worded_as_declared"][0]["module"],
                         "avm/res/hci/cluster")

    def test_unknown_called_community(self):
        t = GOOD.replace("Nik Peters (@nik), whose affiliation is not known,",
                         "From the community, Nik Peters (@nik)")
        r = self.check(t)
        self.assertEqual(r["affiliation_mismatch"][0]["login"], "nik")
        self.assertEqual(r["affiliation_mismatch"][0]["said"], "community")

    def test_do_not_name(self):
        r = self.check(GOOD + "\nThanks to @kim for the report.\n")
        self.assertIn("kim", r["do_not_name_named"])
        r = self.check(GOOD + "\nThanks to Kim Secret.\n",
                       registry_names={"kim": "Kim Secret"})
        self.assertIn("kim", r["do_not_name_named"])

    def test_name_must_match_login(self):
        r = self.check(GOOD.replace("Tomaz M (@tomaz)", "Tom Other (@tomaz)"))
        self.assertEqual(r["name_mismatch"][0]["login"], "tomaz")

    def test_sections_present_omitted_and_ordered(self):
        r = self.check(GOOD.replace("<!-- omitted: coming — nothing open in the window -->\n", ""))
        self.assertIn("coming", r["sections_missing"])
        moved = GOOD.replace("## Plumbing\n", "## Plumbing-x\n").replace(
            "## People\n", "## Plumbing\n\n## People\n")
        r = self.check(moved)
        self.assertTrue(r["sections_out_of_order"])
        # a section the outline omits must not be padded
        v = view()
        v["module_changes"]["summary"]["new_modules"] = []
        r = newsletter.check_newsletter([v], GOOD, slices={"train-issue-5": SLICE})
        self.assertIn("new_modules", r["omitted_section_present"])

    def test_dates_need_years(self):
        r = self.check(GOOD.replace("5 October 2026", "5 October"))
        self.assertEqual(r["dates_without_year"], ["5 October"])

    def test_quotes_and_urls_must_be_in_view(self):
        r = self.check(GOOD.replace("Initial version: deploys a thing.",
                                    "Initial version: deploys everything."))
        self.assertTrue(r["quotes_not_in_view"])
        r = self.check(GOOD.replace("https://github.com/o/r/pull/13",
                                    "https://github.com/o/r/pull/1300"))
        self.assertIn("https://github.com/o/r/pull/1300", r["urls_not_in_view"])
        r = self.check(GOOD + "\nThe open #12 names `avm/res/key/vault`.\n")
        self.assertEqual(r["modules_not_in_view"], [])
        r = self.check(GOOD + "\nSee #4242.\n")
        self.assertIn(4242, r["items_not_in_view"])

    def test_characterising_words(self):
        r = self.check(GOOD + "\nThe maintainers finally merged it.\n")
        self.assertIn("finally", r["characterising_words"])

    def test_story_blocks_are_checked_against_their_slices(self):
        r = newsletter.check_newsletter([view()], GOOD)
        self.assertEqual(r["stories_not_checked"], ["train-issue-5"])
        self.assertFalse(r["ok"])
        bad = GOOD.replace("**What landed.** It landed.", "It landed.")
        r = self.check(bad)
        self.assertFalse(r["stories"]["train-issue-5"]["ok"])   # no closing part
        self.assertFalse(r["ok"])
        r = self.check(GOOD)
        self.assertTrue(r["stories"]["train-issue-5"]["ok"], r["stories"])
        r = self.check(GOOD.replace("story: train-issue-5", "story: train-nope"))
        self.assertIn("train-nope", r["stories_not_in_outline"])


class TestReviewFixes(unittest.TestCase):
    """The automated-review findings on the first version (one test each)."""

    def check(self, text, views=None, **kw):
        kw.setdefault("slices", {"train-issue-5": SLICE})
        return newsletter.check_newsletter(views or [view()], text, **kw)

    # 1
    def test_duplicate_story_block_keeps_the_failure(self):
        text = GOOD.replace("<!-- story: train-issue-5 -->",
                            "<!-- story: train-issue-5 -->\nIt landed.\n<!-- /story -->"
                            "\n\n<!-- story: train-issue-5 -->", 1)
        r = self.check(text)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stories_duplicate"], ["train-issue-5"])
        self.assertFalse(r["stories"]["train-issue-5"]["ok"])

    # 2
    def test_suppressed_login_inside_a_supported_quote(self):
        v = view()
        v["issues"][0]["body"] = "ping @kim please"
        r = self.check(GOOD + '\nThe issue said "ping @kim please".\n', views=[v])
        self.assertEqual(r["quotes_not_in_view"], [])
        self.assertIn("kim", r["do_not_name_named"])
        self.assertFalse(r["ok"])

    # 3
    def _two_repos(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        return a, b

    def test_multi_repo_story_identities_are_qualified(self):
        a, b = self._two_repos()
        st = section(newsletter.outline([a, b]), "stories")
        keys = [x["key"] for x in st["full"]]
        self.assertEqual(sorted(keys), ["o/r:train-issue-5", "o/r:train-pr-11",
                                        "o/s:train-issue-5", "o/s:train-pr-11"])
        self.assertIn("o__s__train-issue-5.slice.json",
                      next(x for x in st["full"] if x["repo"] == "o/s"
                           and x["id"] == "train-issue-5")["slice"])
        # single repo keeps the plain id
        one = section(newsletter.outline([a]), "stories")
        self.assertEqual(one["full"][0]["key"], "train-pr-11")

    def test_multi_repo_story_blocks_use_their_own_slice(self):
        a, b = self._two_repos()
        bad = {"train": {"id": "train-issue-5", "outcome": "in_flight", "evidence": []},
               "conversation": {"timeline": [], "outcome": {"items": []}}}
        block = "Landed. Something happened.\n\n**What landed.** It landed."
        text = GOOD.replace(
            "<!-- story: train-issue-5 -->\n" + block + "\n<!-- /story -->",
            "<!-- story: o/r:train-issue-5 -->\n" + block + "\n<!-- /story -->\n\n"
            "<!-- story: o/s:train-issue-5 -->\n" + block + "\n<!-- /story -->")
        r = newsletter.check_newsletter([a, b], text, slices={
            "o/r:train-issue-5": SLICE, "o/s:train-issue-5": bad})
        self.assertTrue(r["stories"]["o/r:train-issue-5"]["ok"])
        self.assertFalse(r["stories"]["o/s:train-issue-5"]["ok"])
        self.assertEqual(r["stories_not_in_outline"], [])
        r = newsletter.check_newsletter([a, b], GOOD, slices={"train-issue-5": SLICE})
        self.assertEqual(r["stories_not_in_outline"], ["train-issue-5"])

    # 4
    def test_member_view_slug_is_kept(self):
        v = view()
        v["meta"] = dict(v["meta"], owner="avm-tf", repo="Azure/terraform-x")
        self.assertEqual(newsletter.outline([v])["repos"], ["Azure/terraform-x"])

    # 5
    def test_child_breaking_published_matches_module_and_version(self):
        v = view()
        fac = next(m for m in v["module_changes"]["modules"]
                   if m["area"] == "avm/res/data/factory")
        fac["breaking"]["declared"] = [
            {"version": "0.1.2", "module": "avm/res/data/factory/pipe", "text": "child",
             "sources": src(10)},
            {"version": "0.1.2", "text": "parent", "sources": src(10)}]
        b = section(newsletter.outline([v]), "breaking")
        items = {i["text"]: i["published"] for i in b["declared"][0]["items"]}
        self.assertEqual(items, {"child": True, "parent": False})

    # 6
    def test_release_notes_are_kept_on_the_version_row(self):
        v = view()
        rel = [{"tag": "v1.2.0", "url": R + "/releases/tag/v1.2.0",
                "published_at": "2026-10-05T00:00:00Z", "notes": "Adds a thing."}]
        m = next(m for m in v["module_changes"]["modules"] if m["area"] == "avm/ptn/net/dns")
        m["versions"]["releases"] = rel
        mc = section(newsletter.outline([v]), "module_changes")
        row = next(x for x in mc["modules"] if x["area"] == "avm/ptn/net/dns")
        self.assertEqual(row["version"]["releases"], rel)

    # 7
    def test_open_follow_up_of_a_shipped_train_is_listed(self):
        iss = {"number": 5, "title": "x", "author": "carol", "url": R + "/issues/5",
               "state": "closed", "created_at": "2026-05-01T09:00:00Z",
               "closed_at": "2026-05-06T10:00:00Z", "labels": []}
        b = enriched(bundle([
            pr(1, state="closed", merged=True, merged_at="2026-05-06T10:00:00Z",
               closes=[5]),
            pr(2, created_at="2026-05-10T10:00:00Z", closes=[5])], issues=[iss]))
        t = next(t for t in b["trains"] if 2 in t["prs"])
        self.assertEqual(t["outcome"], "shipped")
        c = section(newsletter.outline([b]), "coming")
        self.assertEqual([r["number"] for r in c["needs_first_review"]], [2])
        self.assertEqual(c["counts"]["open_prs_active"], 1)

    # 8
    def test_retained_approval_does_not_override_the_next_move(self):
        b = enriched(bundle([pr(1, created_at="2026-05-03T10:00:00Z",
                                reviews=[review("bob", "2026-05-10T10:00:00Z", "approved", 1)],
                                comments_list=[comment("bob", "2026-05-12T10:00:00Z",
                                                       "One more thing to fix here.", 7)])]))
        c = section(newsletter.outline([b]), "coming")
        self.assertEqual(c["ready"], [])
        self.assertEqual(c["other"], {"awaiting author": 1})

    # 9
    def test_partial_activity_reads_reach_the_coverage_notes(self):
        v = view(pr_activity={"collected": True, "prs": [
            {"number": 10, "checks_status": "gathered", "commits_total": 14,
             "commits": [{"sha": "a"}, {"sha": "b", "checks_omitted": 3}],
             "threads_omitted": 2}]})
        notes = section(newsletter.outline([v]), "numbers")["coverage"]
        self.assertTrue(any("partly read for 1 PR" in n for n in notes), notes)

    # 10
    def test_name_check_accepts_a_comma_after_the_login(self):
        r = self.check(GOOD.replace("Tomaz M (@tomaz)", "Tom Other (@tomaz, community)"))
        self.assertEqual(r["name_mismatch"][0]["login"], "tomaz")
        r = self.check(GOOD.replace("Tomaz M (@tomaz)", "Tomaz M (@tomaz, community)"))
        self.assertEqual(r["name_mismatch"], [])


class TestSuppressionEverywhere(unittest.TestCase):
    """A do_not_name person is named nowhere in the issue: the check reads
    the complete text (stories, quotes, inline and fenced code, comments,
    headings, tables, link and image text, profile URLs)."""

    def dnn(self, extra, story=None, **kw):
        text = GOOD + extra
        if story is not None:
            text = text.replace("Something happened.", story)
        r = newsletter.check_newsletter([view()], text,
                                        slices={"train-issue-5": SLICE}, **kw)
        return r["do_not_name_named"]

    def test_story_block(self):
        self.assertIn("kim", self.dnn("", story="Something happened, said @kim."))

    def test_fenced_code(self):
        self.assertIn("kim", self.dnn("\n```\nping @kim\n```\n"))

    def test_inline_code(self):
        self.assertIn("kim", self.dnn("\nSee `@kim`.\n"))

    def test_every_other_place(self):
        names = {"kim": "Kim Secret"}
        for extra in ("\nThanks to kim.\n", "\nThanks to @KIM.\n",
                      "\n<!-- thanks @kim -->\n", "\n### Thanks kim\n",
                      "\n| who |\n|---|\n| kim |\n",
                      "\nSee [Kim Secret](https://github.com/o/r/pull/12).\n",
                      "\n![Kim Secret](x.png)\n",
                      "\nSee [the profile](https://github.com/kim).\n",
                      "\nThe fork https://github.com/kim/r was used.\n",
                      '\nThey wrote "Kim Secret said so".\n'):
            self.assertIn("kim", self.dnn(extra, registry_names=names), extra)

    def test_links_and_words_that_do_not_name_them(self):
        self.assertEqual(self.dnn("\nSee https://github.com/o/r/pull/12 and the kimono.\n"),
                         [])


class TestRepositoryScopedChecks(unittest.TestCase):
    """Module areas, versions, declarations and #N are repository-local."""

    def views(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        for m in b["module_changes"]["modules"]:
            if m["area"] == "avm/ptn/net/dns":
                m["versions"] = {"changelog": ["0.9.0"]}
                m["changelog"][0]["version"] = "0.9.0"
            if m["area"] == "avm/res/data/factory":
                m["breaking"].pop("declared")
        b["prs"] = [p for p in b["prs"] if p["number"] != 13]
        return a, b

    def run_(self, extra):
        return newsletter.check_newsletter(list(self.views()), GOOD + extra,
                                           slices={"o/r:train-issue-5": SLICE})

    def test_version_of_one_repository_does_not_validate_the_other(self):
        r = self.run_("\n### o/s\n\n- `avm/ptn/net/dns` 0.7.4 changed.\n")
        self.assertIn({"module": "avm/ptn/net/dns", "version": "0.7.4", "repo": "o/s"},
                      r["versions_not_in_view"])
        r = self.run_("\nIn https://github.com/o/s/pull/11, `avm/ptn/net/dns` 0.7.4.\n")
        self.assertIn({"module": "avm/ptn/net/dns", "version": "0.7.4", "repo": "o/s"},
                      r["versions_not_in_view"])
        r = self.run_("\n### o/r\n\n- `avm/ptn/net/dns` 0.7.4 changed.\n")
        self.assertNotIn("o/r", [x.get("repo") for x in r["versions_not_in_view"]])

    def test_unresolved_repository_is_reported(self):
        r = self.run_("\n- `avm/ptn/net/dns` 0.7.4 changed.\n")
        self.assertTrue(any(u["module"] == "avm/ptn/net/dns"
                            for u in r["repository_unresolved"]))

    def test_declaration_in_one_repository_does_not_mask_a_candidate(self):
        r = self.run_("\n### o/s\n\n- `avm/res/data/factory`: a breaking change.\n")
        self.assertEqual([(c["module"], c["repo"]) for c in r["candidates_worded_as_declared"]],
                         [("avm/res/data/factory", "o/s")])

    def test_item_numbers_are_per_repository(self):
        r = self.run_("\n### o/s\n\nSee #13.\n")
        self.assertIn({"repo": "o/s", "number": 13}, r["items_not_in_view"])
        r = self.run_("\n### o/r\n\nSee #13.\n")
        self.assertNotIn({"repo": "o/r", "number": 13}, r["items_not_in_view"])

    def test_single_repository_reports_plain_rows(self):
        r = newsletter.check_newsletter([view()], GOOD + "\nSee #4242.\n",
                                        slices={"train-issue-5": SLICE})
        self.assertEqual(r["items_not_in_view"], [4242])
        self.assertEqual(r["repository_unresolved"], [])


class TestUnknownContributionAndVersionEvidence(unittest.TestCase):

    def test_unknown_headline_is_its_own_class(self):
        row = {"contributed": {"prs": [{"state": "merged", "headline": "unknown",
                                        "class": "unknown"}]}}
        self.assertEqual(newsletter.contribution_class(row), "unknown")
        self.assertIn("unknown", newsletter.CONTRIBUTION_ORDER)

    def test_child_version_files_and_release_tags_are_version_evidence(self):
        v = view()
        m = next(m for m in v["module_changes"]["modules"] if m["area"] == "avm/ptn/net/dns")
        m["versions"]["child_version_files"] = [
            {"module": "avm/ptn/net/dns/child", "from": "0.1.0", "to": "0.2.0"}]
        m["versions"]["releases"] = [{"tag": "v1.2.0"}]
        r = newsletter.check_newsletter(
            [v], GOOD + "\n- `avm/ptn/net/dns/child` 0.2.0, and `avm/ptn/net/dns` 1.2.0.\n",
            slices={"train-issue-5": SLICE})
        self.assertEqual(r["versions_not_in_view"], [])
        self.assertEqual(r["modules_not_in_view"], [])


class TestSweep(unittest.TestCase):
    """The deliberate sweep after the review rounds: suppression and
    repository identity in every place the first rounds did not test."""

    def test_registry_name_across_a_line_break(self):
        r = newsletter.check_newsletter([view()], GOOD + "\nThanks to Pat\nHidden.\n",
                                        slices={"train-issue-5": SLICE},
                                        registry_names={"kim": "Pat Hidden"})
        self.assertEqual(r["do_not_name_named"], ["kim"])

    def test_version_at_the_end_of_a_sentence_is_checked(self):
        r = newsletter.check_newsletter([view()], GOOD + "\nNow `avm/ptn/net/dns` 0.9.9.\n",
                                        slices={"train-issue-5": SLICE})
        self.assertIn({"module": "avm/ptn/net/dns", "version": "0.9.9"},
                      r["versions_not_in_view"])

    def test_quote_is_checked_against_its_paragraphs_repository(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        a["issues"][0]["body"] = "only in r"
        r = newsletter.check_newsletter([a, b], GOOD + '\n### o/s\n\nIt said "only in r".\n',
                                        slices={"o/r:train-issue-5": SLICE})
        self.assertIn("only in r", r["quotes_not_in_view"])
        r = newsletter.check_newsletter([a, b], GOOD + '\n### o/r\n\nIt said "only in r".\n',
                                        slices={"o/r:train-issue-5": SLICE})
        self.assertNotIn("only in r", r["quotes_not_in_view"])

    def test_multi_repo_outline_qualifies_repository_local_names(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        out = newsletter.outline([a, b])
        mc = section(out, "module_changes")
        self.assertEqual(sorted(mc["plumbing_only"]),
                         ["o/r::avm/res/key/vault", "o/s::avm/res/key/vault"])
        lead = {x["fact"]: x for x in section(out, "intro")["lead_with"]}
        self.assertIn("o/s::avm/res/data/factory", lead["declared_breaking"]["areas"])
        self.assertIn("o/r::nik", lead["newcomers_landed_work"]["logins"])
        self.assertEqual(lead["stories"]["top_story"].split(":", 1)[1], "train-issue-5")
        # a single repository keeps plain names
        one = {x["fact"]: x for x in section(newsletter.outline([a]), "intro")["lead_with"]}
        self.assertEqual(one["declared_breaking"]["areas"], ["avm/res/data/factory"])


class TestThirdRound(unittest.TestCase):
    """Unknown stays unknown, malformed input fails, identifiers are
    normalised once."""

    def check(self, text, views=None, **kw):
        kw.setdefault("slices", {"train-issue-5": SLICE})
        return newsletter.check_newsletter(views or [view()], text, **kw)

    # 1
    def test_publication_unknown_without_complete_tag_coverage(self):
        v = view()
        fac = next(m for m in v["module_changes"]["modules"]
                   if m["area"] == "avm/res/data/factory")
        fac["versions"]["published"] = []
        new = next(m for m in v["module_changes"]["modules"] if m["area"] == "avm/res/new/thing")
        new["versions"]["published"] = []
        out = newsletter.outline([v])
        self.assertIs(section(out, "breaking")["declared"][0]["items"][0]["published"], False)
        self.assertIs(section(out, "new_modules")["modules"][0]["published"], False)
        v["module_changes"]["inputs"] = {"tags_collected": False}
        out = newsletter.outline([v])
        self.assertIsNone(section(out, "breaking")["declared"][0]["items"][0]["published"])
        self.assertIsNone(section(out, "new_modules")["modules"][0]["published"])
        v["module_changes"]["inputs"] = {"tags_collected": True,
                                         "windows_without_tags": ["2026-10-01..2026-10-15"]}
        out = newsletter.outline([v])
        self.assertIsNone(section(out, "breaking")["declared"][0]["items"][0]["published"])

    # 2
    def test_windows_without_tags_reach_the_coverage_notes(self):
        v = view()
        v["module_changes"]["inputs"] = {"tags_collected": True,
                                         "windows_without_tags": ["2026-10-01..2026-10-15"]}
        notes = section(newsletter.outline([v]), "numbers")["coverage"]
        self.assertTrue(any("2026-10-01..2026-10-15" in n for n in notes), notes)

    # 3
    def test_shared_basename_stays_ambiguous(self):
        pars = newsletter._scoped_paragraphs("### r\n\ntext\n", ["o/r", "p/r"])
        self.assertIsNone(pars[-1][1])
        pars = newsletter._scoped_paragraphs("### r\n\ntext\n", ["o/r", "p/s"])
        self.assertEqual(pars[-1][1], "o/r")

    # 4
    def test_registry_do_not_name_without_a_name(self):
        r = self.check(GOOD, registry_names={"amy": None})
        self.assertTrue(r["ok"], r)
        r = self.check(GOOD + "\nThanks @amy.\n", registry_names={"amy": None})
        self.assertEqual(r["do_not_name_named"], ["amy"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "people.json")
            with open(path, "w") as fh:
                json.dump({"format": "activity-overview/people", "version": 1,
                           "people": {"amy": {"login": "amy", "do_not_name": True}}}, fh)
            self.assertEqual(newsletter._registry_hidden(path), {"amy": None})

    # 5
    def test_unbalanced_story_markers_fail(self):
        for text in (GOOD.replace("\n<!-- /story -->", "", 1),
                     GOOD.replace("<!-- story: train-issue-5 -->\n", "", 1),
                     GOOD.replace("<!-- story: train-issue-5 -->",
                                  "<!-- story: train-issue-5 -->\n<!-- story: train-pr-11 -->",
                                  1)):
            r = self.check(text)
            self.assertFalse(r["ok"])
            self.assertTrue(r["markers_malformed"], text[-300:])
        self.assertEqual(self.check(GOOD)["markers_malformed"], [])

    # 6
    def _root_view(self):
        v = view()
        v["module_changes"]["modules"].append(module(
            "main.tf", 3, kind="module",
            breaking={"candidates": [{"rule": "parameter_removed", "subject": "location",
                                      "sources": src(12, "main.tf")}]}))
        return v

    def test_root_module_breaking_claim(self):
        r = self.check(GOOD + "\nThe `location` variable was removed: a breaking change.\n",
                       views=[self._root_view()])
        self.assertEqual([c["module"] for c in r["candidates_worded_as_declared"]],
                         ["main.tf"])
        r = self.check(GOOD + "\nThe `location` variable was removed; check before upgrading.\n",
                       views=[self._root_view()])
        self.assertEqual(r["candidates_worded_as_declared"], [])
        # a heading is not a module claim
        r = self.check(GOOD.replace("## Breaking changes and deprecations",
                                    "## Breaking changes and deprecations"),
                       views=[self._root_view()])
        self.assertEqual(r["candidates_worded_as_declared"], [])

    # 7
    def test_logins_resolve_case_insensitively(self):
        r = self.check(GOOD.replace("(@nik)", "(@NIK)"))
        self.assertEqual(r["people_not_in_view"], [])
        self.assertEqual(r["name_mismatch"], [])
        r = self.check(GOOD.replace("Tomaz M (@tomaz)", "Tom Other (@TOMAZ)"))
        self.assertEqual(r["name_mismatch"][0]["login"], "tomaz")
        r = self.check(GOOD.replace("Nik Peters (@nik), whose affiliation is not known,",
                                    "From the community, Nik Peters (@Nik)"))
        self.assertEqual(r["affiliation_mismatch"][0]["login"], "nik")


class TestRobustness(unittest.TestCase):
    """Malformed and edge inputs never raise."""

    TEXTS = ("", "# Only a title\n", GOOD.replace("\n", "\r\n"),
             GOOD + "\n<!-- story: x -->\n", GOOD + "\n<!-- /story -->\n",
             GOOD + "\n<!-- omitted: coming -->\n", GOOD + "\n<!-- omitted: coming",
             GOOD + "\n<!-- unclosed comment\n", GOOD + "\n<!-- story: -->\n<!-- /story -->\n",
             GOOD + "\nZoë Ñandú (@nik) and 渡辺 (@tomaz).\n")

    def views(self):
        out = [view()]
        for key in ("module_changes", "module_notes", "stories", "contributors",
                    "affiliations", "pr_activity", "people_context", "forecast", "people",
                    "newcomers", "trains", "prs", "issues"):
            v = view()
            v.pop(key, None)
            out.append(v)
        v = view()
        v["module_notes"]["tags"] = {"collected": False, "reason": "none"}
        v["module_changes"]["inputs"] = {"tags_collected": False}
        out.append(v)
        return out

    def test_never_raises(self):
        names = {"a": None, "b": "", "c": "Pat Hidden"}
        for v in self.views():
            for views in ([v], [v, dict(view(), meta=dict(view()["meta"], repo="s"))]):
                for t in self.TEXTS:
                    r = newsletter.check_newsletter(views, t, slices={},
                                                    registry_names=names)
                    self.assertIn("ok", r)
                    if not t:
                        self.assertFalse(r["ok"])

    def test_cli_rejects_bad_inputs_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            vp, ip, rp, bad = (os.path.join(d, n) for n in
                               ("v.json", "i.md", "p.json", "bad.json"))
            with open(vp, "w") as fh:
                json.dump(view(), fh)
            open(ip, "w").close()
            with open(rp, "w") as fh:
                json.dump({"people": {"amy": {"login": "amy", "do_not_name": True}}}, fh)
            with open(bad, "w") as fh:
                json.dump([1, 2], fh)
            cases = (([vp, "--check-newsletter", ip, "--people", rp], 1),
                     ([vp, "--check-newsletter", ip, "--people", bad], 2),
                     ([bad, "--check-newsletter", ip], 2),
                     ([bad, "--outline"], 2))
            for argv, code in cases:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        newsletter.main(argv)
                self.assertEqual(cm.exception.code, code, argv)

    def test_crlf_issue_passes_like_lf(self):
        r = newsletter.check_newsletter([view()], GOOD.replace("\n", "\r\n"),
                                        slices={"train-issue-5": SLICE})
        self.assertTrue(r["ok"], {k: x for k, x in r.items() if x and k != "cannot_check"})


class TestAbsentIsNotFalse(unittest.TestCase):
    """The robustness pass: where coverage is not established, the outline
    says unknown (None) instead of false / zero / empty."""

    def test_first_pr_with_no_listed_pr_is_unknown_not_issue(self):
        row = {"first": {"first": "pr", "number": 9}, "contributed": {"prs": []}}
        self.assertEqual(newsletter.contribution_class(row), "unknown")
        row = {"first": {"first": "issue", "number": 9}, "contributed": {"prs": []}}
        self.assertEqual(newsletter.contribution_class(row), "issue")

    def test_shares_and_counts_without_their_input(self):
        v = view()
        del v["module_changes"]["summary"]["plumbing"]["share"]
        f = section(newsletter.outline([v]), "numbers")["facts"]
        self.assertIsNone(f.get("plumbing_share_pct"))
        v = view()
        del v["module_changes"]
        v["people"].pop("jared")
        m = {r["login"]: r for r in section(newsletter.outline([v]), "people")["maintainers"]}
        self.assertIsNone(m["jared"]["reviews"])
        self.assertIsNone(m["alex"]["authored_by_class"])

    def test_publication_coverage_on_version_rows_and_numbers(self):
        v = view()
        v["module_changes"]["inputs"] = {"tags_collected": True,
                                         "windows_without_tags": ["2026-10-01..2026-10-15"]}
        out = newsletter.outline([v])
        row = section(out, "module_changes")["modules"][0]
        self.assertIs(row["version"]["publication_known"], False)
        self.assertIs(section(out, "numbers")["facts"]["versions_published_partial"], True)
        row = section(newsletter.outline([view()]), "module_changes")["modules"][0]
        self.assertIs(row["version"]["publication_known"], True)

    def test_story_keys_compare_with_the_repository_case_insensitive(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        text = GOOD.replace("story: train-issue-5", "story: O/R:train-issue-5")
        r = newsletter.check_newsletter([a, b], text, slices={"o/r:train-issue-5": SLICE})
        self.assertEqual(r["stories_not_in_outline"], [])
        self.assertEqual(r["stories_not_checked"], [])
        self.assertIn("O/R:train-issue-5", r["stories"])

    def test_identifier_helpers(self):
        self.assertEqual(newsletter.login_key("NIK"), "nik")
        self.assertEqual(newsletter.slug_key("Azure/Repo"), "azure/repo")
        self.assertEqual(newsletter.version_key("v1.2.0"), "1.2.0")
        self.assertEqual(newsletter.story_key_norm("O/R:train-issue-5"), "o/r:train-issue-5")
        self.assertEqual(newsletter.story_key_norm("train-issue-5"), "train-issue-5")


class TestFourthRound(unittest.TestCase):
    """Versions belong to their module, landed work means merged, partial
    stream totals say so."""

    # 1
    def test_landed_work_means_a_merged_pr(self):
        v = view()
        v["contributors"]["newcomers"].append(
            {"login": "zed", "class": "first_ever", "affiliation": "unknown",
             "first": {"first": "pr", "number": 98, "url": R + "/pull/98",
                       "created_at": "2026-10-04T10:00:00Z"},
             "contributed": {"prs": []}})
        out = newsletter.outline([v])
        row = next(r for r in section(out, "people")["newcomers"] if r["login"] == "zed")
        self.assertEqual(row["contribution"], "unknown")
        lead = {x["fact"]: x for x in section(out, "intro")["lead_with"]}
        self.assertNotIn("zed", lead["newcomers_landed_work"]["logins"])
        self.assertIn("nik", lead["newcomers_landed_work"]["logins"])

    # 2
    def _child_view(self):
        v = view()
        fac = next(m for m in v["module_changes"]["modules"]
                   if m["area"] == "avm/res/data/factory")
        fac["versions"]["child_version_files"] = [
            {"module": "avm/res/data/factory/flow", "from": "0.2.0", "to": "0.2.1"}]
        return v

    def check(self, extra, v=None):
        return newsletter.check_newsletter([v or self._child_view()], GOOD + extra,
                                           slices={"train-issue-5": SLICE})

    def test_swapped_parent_and_child_versions_fail(self):
        r = self.check("\n- `avm/res/data/factory` 0.1.2, with its child "
                       "`avm/res/data/factory/pipe` 0.13.0.\n")
        self.assertIn({"module": "avm/res/data/factory", "version": "0.1.2"},
                      r["versions_not_in_view"])
        self.assertIn({"module": "avm/res/data/factory/pipe", "version": "0.13.0"},
                      r["versions_not_in_view"])

    def test_correct_parent_and_child_versions_pass(self):
        r = self.check("\n- `avm/res/data/factory` 0.13.0, with its child "
                       "`avm/res/data/factory/pipe` 0.1.2 and `avm/res/data/factory/flow` "
                       "0.2.0 → 0.2.1.\n")
        self.assertEqual(r["versions_not_in_view"], [])

    def test_implied_child_version_in_a_parent_paragraph_passes(self):
        r = self.check("\n- `avm/res/data/factory` 0.13.0, with a child module "
                       "republished as 0.1.2.\n")
        self.assertEqual(r["versions_not_in_view"], [])
        # a version no module of the paragraph holds still fails
        r = self.check("\n- `avm/res/data/factory` 0.13.0, with a child module "
                       "republished as 0.7.4.\n")
        self.assertIn({"module": None, "version": "0.7.4"}, r["versions_not_in_view"])

    # 3
    def test_stream_total_of_versions_is_marked_partial(self):
        a, b = view(), view()
        b["meta"] = dict(b["meta"], repo="s")
        f = section(newsletter.outline([a, b]), "numbers")["facts"]
        self.assertEqual(f["versions_published"], 4)
        self.assertNotIn("versions_published_partial", f)
        b["module_changes"]["inputs"] = {"tags_collected": True,
                                         "windows_without_tags": ["2026-10-01..2026-10-15"]}
        f = section(newsletter.outline([a, b]), "numbers")["facts"]
        self.assertIs(f["versions_published_partial"], True)
        b = view()
        b["meta"] = dict(b["meta"], repo="s")
        b["module_notes"]["tags"] = {"collected": False}
        f = section(newsletter.outline([a, b]), "numbers")["facts"]
        self.assertEqual(f["versions_published"], 2)
        self.assertIs(f["versions_published_partial"], True)


class TestOwnersLayout(unittest.TestCase):
    """The owner's layout: new, changed, breaking; every story in full by
    state; links and release status; plumbing themes; module ownership."""

    def v(self):
        v = view()
        v["prs"][0]["closes"] = [5]
        v["issues"][0]["body"] = "### Description\n\nThe Git settings are wiped."
        v["people"]["helper[bot]"] = {"is_bot": True, "prs_authored": 1, "prs_merged": 1}
        v["prs"].append({"number": 14, "title": "chore: sync list", "author": "helper[bot]",
                         "url": R + "/pull/14", "state": "closed", "merged": True,
                         "merged_at": "2026-10-08T10:00:00Z"})
        return v

    def check(self, text, v=None, **kw):
        kw.setdefault("slices", {"train-issue-5": SLICE})
        return newsletter.check_newsletter([v or self.v()], text, **kw)

    def test_section_order_is_new_changed_breaking(self):
        self.assertEqual(newsletter.SECTION_ORDER,
                         ("intro", "new_modules", "module_changes", "breaking", "stories",
                          "ownership", "people", "coming", "plumbing", "numbers"))

    def test_change_rows_explain_themselves(self):
        mc = section(newsletter.outline([self.v()]), "module_changes")
        fac = mc["modules"][0]
        self.assertEqual(fac["url"], "https://github.com/o/r/tree/main/avm/res/data/factory")
        self.assertEqual(fac["release"][0]["status"], "published")
        self.assertEqual(fac["release"][0]["url"],
                         "https://github.com/o/r/tree/avm/res/data/factory/0.13.0")
        self.assertEqual(fac["prompted_by"][0]["number"], 5)
        self.assertEqual(fac["prompted_by"][0]["asked"], "The Git settings are wiped.")
        self.assertEqual(fac["pr_details"][0]["author_name"], "Tomaz M")
        hci = next(m for m in mc["modules"] if m["area"] == "avm/res/hci/cluster")
        self.assertEqual(hci["release"], [{"module": "avm/res/hci/cluster", "version": "0.3.0",
                                           "status": "declared_not_published"}])

    def test_release_status_of_a_pr(self):
        v = self.v()
        self.assertEqual(newsletter.pr_release(v, 11)[0]["status"], "published")
        self.assertEqual(newsletter.pr_release(v, 12), [{"status": "no_version_change"}])
        self.assertEqual(newsletter.pr_release(v, 99), [{"status": "unknown"}])
        v["module_changes"]["inputs"] = {"tags_collected": False}
        self.assertEqual(newsletter.module_release(
            v, v["module_changes"]["modules"][2])[0]["status"], "unknown")

    def test_automation_accounts_listed_for_what_they_are(self):
        out = newsletter.outline([self.v()])
        people = section(out, "people")
        self.assertEqual([b["login"] for b in people["automation"]], ["helper[bot]"])
        self.assertEqual(people["automation"][0]["prs"][0]["number"], 14)
        self.assertNotIn("helper[bot]", [r["login"] for r in people["top"]])
        # an operator-declared automation account
        out = newsletter.outline([self.v()], automation=["tomaz"])
        self.assertIn("tomaz", [b["login"] for b in section(out, "people")["automation"]])

    def test_ownership_section(self):
        self.assertIn("module ownership", section(newsletter.outline([self.v()]),
                                                  "ownership")["omit"])
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_needed", "module": "avm/res/hci/cluster", "date": "2026-10-03",
             "source": {"type": "issue", "number": 5, "url": R + "/issues/5"}}],
            "without_owner": [{"module": "avm/res/key/vault", "marker": "ORPHANED.md"}]}
        own = section(newsletter.outline([v]), "ownership")
        self.assertIsNone(own["omit"])
        self.assertEqual(own["events"][0]["module_url"],
                         "https://github.com/o/r/tree/main/avm/res/hci/cluster")
        self.assertEqual(own["without_owner"][0]["module"], "avm/res/key/vault")

    def test_plumbing_themes(self):
        pl = section(newsletter.outline([self.v()]), "plumbing")
        self.assertEqual([(t["id"], t["theme_source"]) for t in pl["themes"]],
                         [("ci", "provisional")])
        self.assertEqual(pl["themes"][0]["prs"][0]["number"], 12)
        v = self.v()
        v["plumbing_themes"] = [{"id": "test-env", "title_hint": "Test environment",
                                 "paths": [".github/"], "prs": [{"number": 12}],
                                 "stories": [], "people": ["jared"]}]
        pl = section(newsletter.outline([v]), "plumbing")
        self.assertEqual([(t["id"], t["theme_source"]) for t in pl["themes"]],
                         [("test-env", "view")])
        self.assertEqual(pl["themes"][0]["people"][0]["name"], "Jared Holt")

    def test_untraceable_tag_makes_a_pr_release_unknown(self):
        v = self.v()
        hci = next(m for m in v["module_changes"]["modules"] if m["area"] == "avm/res/hci/cluster")
        hci["changelog"] = []
        hci["prs"] = [{"number": 11}]
        hci["versions"] = {"published": [{"version": "0.3.1"}],
                           "published_not_declared": ["0.3.1"]}
        rows = newsletter.pr_release(v, 11)
        self.assertIn({"module": "avm/res/hci/cluster", "status": "unknown",
                       "note": "tagged 0.3.1 in the window without a changelog entry"}, rows)

    def test_issue_form_module_for_trains_without_code_areas(self):
        v = self.v()
        v["issues"].append({"number": 50, "title": "Budget thresholds", "author": "kim",
                            "url": R + "/issues/50", "state": "open",
                            "body": "### Module Name\n\navm/res/consumption/budget\n"})
        v["trains"].append({"id": "train-issue-50", "outcome": "in_flight", "root_issue": 50,
                            "issues": [50], "prs": []})
        v["stories"]["quiet"] = [{"id": "train-issue-50", "score": 5, "idle_days": 40}]
        st = section(newsletter.outline([v]), "stories")
        self.assertEqual(st["lines"][0]["modules"][0]["area"], "avm/res/consumption/budget")
        self.assertEqual(st["lines"][0]["idle_days"], 40)
        r = self.check(GOOD + "\nQuiet: [`avm/res/consumption/budget`](" + T +
                       "avm/res/consumption/budget).\n", v=v)
        self.assertEqual(r["modules_not_in_view"], [])

    # the check
    def test_good_passes_under_the_new_rules(self):
        r = self.check(GOOD)
        self.assertTrue(r["ok"], {k: x for k, x in r.items() if x and k != "cannot_check"})

    def test_story_under_the_wrong_group_or_without_its_state(self):
        r = self.check(GOOD.replace("### Landed", "### In flight"))
        self.assertEqual(r["stories_misplaced"][0]["story"], "train-issue-5")
        r = self.check(GOOD.replace("Landed. Something happened.", "Something happened."))
        self.assertEqual(r["stories_state_missing"][0]["story"], "train-issue-5")

    def test_every_story_written_or_pending(self):
        r = self.check(GOOD.replace(
            "<!-- story pending: train-pr-11 — written from the next store -->", ""))
        self.assertEqual(r["stories_missing"], ["train-pr-11"])
        self.assertFalse(r["ok"])

    def test_module_mentions_are_links_and_published_versions_link_the_tag(self):
        r = self.check(GOOD.replace(DNS + " " + DNS_V, "`avm/ptn/net/dns` 0.7.4"))
        self.assertIn({"section": "module_changes", "module": "avm/ptn/net/dns"},
                      r["modules_not_linked"])
        self.assertIn({"section": "module_changes", "module": "avm/ptn/net/dns",
                       "version": "0.7.4"}, r["versions_not_linked"])

    def test_landed_work_states_its_release(self):
        r = self.check(GOOD.replace(" It was released as [0.13.0]"
                                    "(https://github.com/o/r/tree/avm/res/data/factory/0.13.0).",
                                    "").replace(", published as [0.1.0]"
                                                "(https://github.com/o/r/tree/t/0.1.0)", ""))
        self.assertEqual([x["section"] for x in r["release_status_missing"]],
                         ["people", "people"])

    def test_ownership_rows_must_exist(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_needed", "module": "avm/res/hci/cluster"}], "without_owner": []}
        text = GOOD.replace(
            "<!-- omitted: ownership — needs a store gathered with module ownership -->",
            "## Module ownership\n\n- [`avm/res/hci/cluster`](" + T + "avm/res/hci/cluster)"
            " needs an owner.\n- [`avm/ptn/net/dns`](" + T + "avm/ptn/net/dns) was adopted.")
        r = self.check(text, v=v)
        self.assertEqual(r["ownership_not_in_view"], [{"module": "avm/ptn/net/dns"}])

    def test_automation_not_presented_as_a_person(self):
        r = self.check(GOOD + "\nThanks to Helper (@helper[bot]).\n")
        self.assertEqual(r["automation_as_person"][0]["login"], "helper[bot]")
        r = self.check(GOOD + "\nThe automation account @helper[bot] synced the list.\n")
        self.assertEqual(r["automation_as_person"], [])
        self.assertEqual(r["people_not_in_view"], [])
        r = self.check(GOOD + "\nTomaz M (@tomaz) synced it.\n", automation=["tomaz"])
        self.assertTrue(r["automation_as_person"])


class TestDeliveredContract(unittest.TestCase):
    """The delivered contract: stories.all with five groups and the story
    plan, released rows, themes, closing, links from the view."""

    def v(self):
        v = view()
        v["trains"] += [
            {"id": "train-pr-30", "outcome": "rejected", "issues": [], "prs": [10]},
            {"id": "train-pr-31", "outcome": "in_flight", "issues": [], "prs": [98],
             "effort": {"stalled": True, "idle_days": 50}},
            {"id": "train-pr-32", "outcome": "in_flight", "issues": [], "prs": [10],
             "effort": {"stalled": True, "idle_days": 20}},
            {"id": "train-pr-33", "outcome": "shipped", "issues": [], "prs": [12],
             "discussion": {"in_window": 4, "participants": 2}},
        ]
        rel = [{"module": "avm/ptn/net/dns", "version": "0.7.4", "status": "published",
                "url": "https://example.invalid/tag", "registry": "br/public:avm/ptn/net/dns:0.7.4"}]
        v["stories"]["all"] = [
            {"id": "train-issue-5", "group": "landed", "state": "landed", "score": 30,
             "worthy": "3 human turn(s) in the window", "modules": [".github", "avm/res/data/factory"]},
            {"id": "train-pr-11", "group": "landed", "state": "landed", "score": 20,
             "worthy": "2 human turn(s) in the window", "released": rel},
            {"id": "train-pr-30", "group": "closed", "state": "closed", "score": 5,
             "worthy": "1 human turn(s) in the window"},
            {"id": "train-pr-31", "group": "stalled", "state": "stalled", "score": 4,
             "worthy": "carried over"},
            {"id": "train-pr-32", "group": "stalled", "state": "stalled", "score": 3,
             "worthy": "PR #10 opened in the window"},
            {"id": "train-pr-12", "group": "plumbing", "state": "landed", "score": 2,
             "worthy": "PR #12 opened in the window"},
            {"id": "train-pr-33", "group": "plumbing", "state": "landed", "score": 1,
             "worthy": "4 human turn(s) in the window"},
        ]
        v["plumbing_themes"] = [{"id": "ci_workflows", "title_hint": "CI workflows",
                                 "prs": [{"number": 12}], "stories": ["train-pr-12", "train-pr-33"],
                                 "subjects": ["ci"], "issues": [], "people": ["jared"]}]
        v["module_changes"]["modules"][1]["url"] = "https://example.invalid/factory"
        v["issues"][0]["closing"] = {"at": "2026-10-05T10:00:00Z", "by": "alex",
                                     "state_reason": "completed", "prs": [10]}
        v["prs"][0]["closes"] = [5]
        v["prs"].append({"number": 98, "title": "feat: old work", "author": "sam",
                         "url": R + "/pull/98", "state": "open", "merged": False,
                         "created_at": "2026-08-01T10:00:00Z"})
        return v

    def test_story_plan_by_the_owners_rule(self):
        st = section(newsletter.outline([self.v()]), "stories")
        plan = {r["key"]: r["plan"] for r in st["story_plan"]}
        self.assertEqual(plan, {"train-issue-5": "full", "train-pr-11": "full",
                                "train-pr-32": "full", "train-pr-31": "line",
                                "train-pr-30": "full", "train-pr-33": "full",
                                "train-pr-12": "in_theme"})
        self.assertEqual([r["key"] for r in st["lines"]], ["train-pr-31"])
        self.assertEqual(st["lines"][0]["idle_days"], 50)
        self.assertEqual(st["in_theme"][0]["theme"], "ci_workflows")
        self.assertEqual(st["plan_counts"]["stalled"], {"line": 1, "full": 1})
        self.assertEqual(list(st["groups"]), list(newsletter.STORY_GROUPS))
        self.assertEqual([r["key"] for r in st["groups"]["closed"]], ["train-pr-30"])

    def test_rows_from_the_view(self):
        out = newsletter.outline([self.v()])
        st = section(out, "stories")
        row = next(r for r in st["full"] if r["key"] == "train-pr-11")
        self.assertEqual(row["released"][0]["registry"], "br/public:avm/ptn/net/dns:0.7.4")
        row = next(r for r in st["full"] if r["key"] == "train-issue-5")
        self.assertEqual([m["area"] for m in row["modules"]], ["avm/res/data/factory"])
        self.assertEqual(row["modules"][0]["url"], "https://example.invalid/factory")
        fac = section(out, "module_changes")["modules"][0]
        self.assertEqual(fac["prompted_by"][0]["closing"]["by"], "alex")
        th = section(out, "plumbing")["themes"][0]
        self.assertEqual((th["id"], th["theme_source"], th["subjects"]),
                         ("ci_workflows", "view", ["ci"]))
        self.assertEqual(th["counts"], {"prs": 1, "merged": 1, "closed_unmerged": 0,
                                        "open": 0, "by_author": {"jared": 1},
                                        "merged_by_author": {"jared": 1}})

    def _text(self, extra_story="", theme=True):
        t = GOOD.replace("<!-- story pending: train-pr-11 — written from the next store -->",
                         "<!-- story pending: train-pr-11 — later -->\n\n"
                         "### Stalled\n\n<!-- story pending: train-pr-32 — later -->\n\n"
                         "- [#98](https://github.com/o/r/pull/98) carried over.\n\n"
                         "### Closed without landing\n\n"
                         "<!-- story pending: train-pr-30 — later -->" + extra_story)
        if theme:
            t = t.replace("### CI workflows and pipeline utilities\n\n"
                          "A pipeline action was bumped in [#12](https://github.com/o/r/pull/12).",
                          "### CI workflows\n\nA pipeline action was bumped in "
                          "[#12](https://github.com/o/r/pull/12).\n\n"
                          "<!-- story pending: train-pr-33 — later -->")
        else:
            t = t.replace("### CI workflows and pipeline utilities\n\n", "") + \
                "\n<!-- story pending: train-pr-33 — later -->\n"
        return t

    def test_ownership_event_naming_no_module_is_kept_apart(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "orphaned", "module": None, "date": "2026-10-03",
             "source": {"type": "pr", "number": 12, "url": R + "/pull/12"}},
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "people": ["alex"]}],
            "without_owner": [], "start_without_owner": [
                {"module": "avm/res/key/vault", "marker": "ORPHANED.md"}],
            "labelled_items": [{"module": "avm/ptn/net/dns", "marker": "orphan label"}]}
        own = section(newsletter.outline([v]), "ownership")
        self.assertEqual([e["module"] for e in own["events"]], ["avm/res/hci/cluster"])
        self.assertEqual(own["unattributed"][0]["source"]["number"], 12)
        self.assertEqual(own["start_without_owner"][0]["module"], "avm/res/key/vault")
        text = self._text().replace(
            "<!-- omitted: ownership — needs a store gathered with module ownership -->",
            "## Module ownership\n\n- [`avm/ptn/net/dns`](" + T + "avm/ptn/net/dns) was "
            "labelled; [`avm/res/key/vault`](" + T + "avm/res/key/vault) had no owner.")
        v["module_changes"] = view()["module_changes"]
        r = newsletter.check_newsletter([v], text, slices={"train-issue-5": SLICE})
        self.assertEqual(r["ownership_not_in_view"], [])

    def test_a_module_only_the_ownership_fact_names_is_known(self):
        v = self.v()
        v["module_changes"] = view()["module_changes"]
        v["module_ownership"] = {"collected": True, "events": [], "without_owner": [
            {"module": "avm/res/code/signing", "marker": "ORPHANED.md"}]}
        text = self._text().replace(
            "<!-- omitted: ownership — needs a store gathered with module ownership -->",
            "## Module ownership\n\n- [`avm/res/code/signing`](" + T + "avm/res/code/signing)"
            " has no owner.")
        r = newsletter.check_newsletter([v], text, slices={"train-issue-5": SLICE})
        self.assertEqual((r["modules_not_in_view"], r["urls_not_in_view"],
                          r["ownership_not_in_view"]), ([], [], []))

    def test_automation_only_train_is_told_in_its_theme(self):
        v = self.v()
        v["people"]["bump[bot]"] = {"is_bot": True}
        v["prs"].append({"number": 97, "title": "fix: bump x", "author": "bump[bot]",
                         "url": R + "/pull/97", "state": "open", "merged": False})
        v["trains"].append({"id": "train-pr-97", "outcome": "in_flight", "prs": [97],
                            "issues": []})
        v["stories"]["all"].append({"id": "train-pr-97", "group": "in_flight",
                                    "state": "in_flight", "worthy": "PR #97 opened in the window"})
        v["plumbing_themes"].append({"id": "deps", "title_hint": "Dependency updates",
                                     "prs": [{"number": 97}], "stories": []})
        st = section(newsletter.outline([v]), "stories")
        row = next(r for r in st["all"] if r["key"] == "train-pr-97")
        self.assertEqual((row["plan"], row["theme"]), ("in_theme", "deps"))
        self.assertNotIn("train-pr-97", [r["key"] for r in st["full"]])
        # a human turn keeps it a story
        next(t for t in v["trains"] if t["id"] == "train-pr-97")["discussion"] = {
            "comments": 1, "participants": 1}
        st = section(newsletter.outline([v]), "stories")
        self.assertEqual(next(r for r in st["all"] if r["key"] == "train-pr-97")["plan"], "full")

    def test_a_theme_train_is_named_under_its_own_theme(self):
        v = self.v()
        v["module_changes"] = view()["module_changes"]
        v["plumbing_themes"].append({"id": "other", "title_hint": "Other plumbing",
                                     "prs": [], "stories": []})
        t = self._text()
        r = newsletter.check_newsletter([v], t, slices={"train-issue-5": SLICE})
        self.assertEqual(r["theme_trains_missing"], [])
        # #12 named only under another theme's heading does not count
        moved = t.replace("### CI workflows\n\nA pipeline action was bumped in "
                          "[#12](https://github.com/o/r/pull/12).",
                          "### CI workflows\n\nNothing here.\n\n### Other plumbing\n\n"
                          "A pipeline action was bumped in [#12](https://github.com/o/r/pull/12).")
        self.assertNotEqual(moved, t)
        r = newsletter.check_newsletter([v], moved, slices={"train-issue-5": SLICE})
        self.assertEqual(r["theme_trains_missing"], [{"key": "train-pr-12",
                                                      "theme": "ci_workflows"}])

    def test_story_may_link_any_module_the_view_knows(self):
        t = self._text().replace(
            "Landed. Something happened.",
            "Landed. Something happened to [`avm/ptn/net/dns`](" + T + "avm/ptn/net/dns).")
        v = self.v()
        v["module_changes"] = view()["module_changes"]
        r = newsletter.check_newsletter([v], t, slices={"train-issue-5": SLICE})
        self.assertTrue(r["stories"]["train-issue-5"]["ok"], r["stories"])

    def test_a_paragraph_may_state_what_its_linked_items_say(self):
        v = self.v()
        v["module_changes"] = view()["module_changes"]
        v["prs"][2]["body"] = "Bumps the action from 5.0.0 to 7.0.1 for 403 errors, see #4242."
        t = self._text() + ("\nIn [#12](https://github.com/o/r/pull/12) the action moved from "
                            "5.0.0 to 7.0.1 after 403 errors (#4242), one item.\n")
        r = newsletter.check_newsletter([v], t, slices={"train-issue-5": SLICE})
        self.assertEqual((r["versions_not_in_view"], r["numbers_not_in_view"],
                          r["items_not_in_view"]), ([], [], []))
        t = self._text() + "\nThe action moved from 5.0.0 to 7.0.1 after 403 errors (#4242).\n"
        r = newsletter.check_newsletter([v], t, slices={"train-issue-5": SLICE})
        self.assertTrue(r["versions_not_in_view"] and r["items_not_in_view"]
                        and "403" in r["numbers_not_in_view"])

    def test_ignored_about_tests_is_not_a_characterisation(self):
        r = newsletter.check_newsletter([view()], GOOD + "\nIgnored tests are skipped.\n",
                                        slices={"train-issue-5": SLICE})
        self.assertEqual(r["characterising_words"], [])
        r = newsletter.check_newsletter([view()], GOOD + "\nThe request was ignored.\n",
                                        slices={"train-issue-5": SLICE})
        self.assertEqual(r["characterising_words"], ["was ignored"])

    def test_story_may_link_its_modules_and_tags(self):
        t = self._text().replace(
            "Landed. Something happened.",
            "Landed. Something happened to [`avm/res/data/factory`](https://example.invalid/factory).")
        r = newsletter.check_newsletter([self.v()], t, slices={"train-issue-5": SLICE})
        self.assertTrue(r["stories"]["train-issue-5"]["ok"], r["stories"])
        t = t.replace("https://example.invalid/factory", "https://example.invalid/other")
        r = newsletter.check_newsletter([self.v()], t, slices={"train-issue-5": SLICE})
        self.assertFalse(r["stories"]["train-issue-5"]["ok"])

    def test_check_lines_themes_and_theme_trains(self):
        v0 = self.v
        self.v = lambda: dict(v0(), module_changes=view()["module_changes"])
        r = newsletter.check_newsletter([self.v()], self._text(),
                                        slices={"train-issue-5": SLICE})
        self.assertTrue(r["ok"], {k: x for k, x in r.items() if x and k != "cannot_check"})
        r = newsletter.check_newsletter(
            [self.v()], self._text().replace("- [#98](https://github.com/o/r/pull/98) carried over.", ""),
            slices={"train-issue-5": SLICE})
        self.assertEqual(r["story_lines_missing"], ["train-pr-31"])
        r = newsletter.check_newsletter([self.v()], self._text(theme=False),
                                        slices={"train-issue-5": SLICE})
        self.assertIn("ci_workflows", r["themes_missing"])
        t = self._text().replace("A pipeline action was bumped in "
                                 "[#12](https://github.com/o/r/pull/12).", "Actions were bumped.")
        r = newsletter.check_newsletter([self.v()], t, slices={"train-issue-5": SLICE})
        self.assertEqual(r["theme_trains_missing"], [{"key": "train-pr-12", "theme": "ci_workflows"}])
        t = self._text(theme=False).replace(
            "## Plumbing: what was engineered",
            "## Plumbing: what was engineered\n\n<!-- theme pending: ci_workflows — later -->")
        r = newsletter.check_newsletter([self.v()], t, slices={"train-issue-5": SLICE})
        self.assertEqual(r["themes_missing"], [])
        self.assertEqual(r["themes_pending"], ["ci_workflows"])


class TestWorkPackage(unittest.TestCase):
    """newsletter.py --work-package DIR: the files a set of parallel writers
    needs, one batch each."""

    def test_package_files_manifest_and_batches(self):
        with tempfile.TemporaryDirectory() as d:
            doc = newsletter.work_package([view()], d)
            self.assertEqual(doc["kind"], "newsletter_work_package")
            self.assertEqual([(m["key"], m["plan"], m["batch"]) for m in doc["stories"]],
                             [("train-pr-11", "full", "stories-01"),
                              ("train-issue-5", "full", "stories-01")])
            sl = json.load(open(os.path.join(d, "slices", "train-issue-5.slice.json")))
            self.assertIn("newsletter_links", sl)
            self.assertEqual(sl["train"]["id"], "train-issue-5")
            row = json.load(open(os.path.join(d, "rows", "train-issue-5.row.json")))
            self.assertEqual(row["prompted_by"], [])
            self.assertTrue(os.path.exists(os.path.join(d, "rows", "theme-ci.json")))
            batch = open(os.path.join(d, "batches", "stories-01", "BATCH.md")).read()
            self.assertIn("--check-story", batch)
            self.assertIn("**Landed — ", batch)
            brief = open(os.path.join(d, "batches", "stories-01", "STORY_BRIEF.md")).read()
            self.assertIn("Stories (the story brief)", brief)
            self.assertIn("Where it stands", brief)

    def test_theme_and_row_files_carry_full_descriptions_and_child_releases(self):
        v = view()
        long = "### Description\n\n" + "word " * 400 + "THE END"
        v["prs"][2]["body"] = long
        v["prs"][0]["body"] = long
        with tempfile.TemporaryDirectory() as d:
            newsletter.work_package([v], d, view_paths=["/x/v.json"])
            th = json.load(open(os.path.join(d, "rows", "theme-ci.json")))
            pr12 = next(p for p in th["theme"]["prs"] if p["number"] == 12)
            self.assertTrue(pr12["description"].endswith("THE END"))
            self.assertIn("THE END", pr12["body"])
            row = json.load(open(os.path.join(d, "rows", "train-issue-5.row.json")))
            self.assertTrue(row["pr_details"][0]["description"].endswith("THE END"))
            fac = row["module_releases"]["avm/res/data/factory"]
            self.assertEqual(fac["child_releases"][0]["module"], "avm/res/data/factory/pipe")
            batch = open(os.path.join(d, "batches", "theme-ci", "BATCH.md")).read()
            self.assertIn("--check-theme", batch)
            self.assertIn("/x/v.json", batch)
        # the outline keeps its cut, and says so
        mc = section(newsletter.outline([v]), "module_changes")
        fac = next(m for m in mc["modules"] if m["area"] == "avm/res/data/factory")
        self.assertEqual(len(fac["pr_details"][0]["description"]), newsletter.PR_DESCRIPTION_CAP)
        self.assertTrue(fac["pr_details"][0]["description_cut"])

    def test_batches_respect_the_story_limit(self):
        v = view()
        for n in range(20, 31):
            v["trains"].append({"id": "train-pr-%d" % n, "outcome": "shipped", "issues": [],
                                "prs": [10]})
            v["stories"]["selected"].append({"id": "train-pr-%d" % n, "rank": n, "score": 1})
        with tempfile.TemporaryDirectory() as d:
            doc = newsletter.work_package([v], d)
        sizes = [len(b["keys"]) for b in doc["batches"] if b["kind"] == "stories"]
        self.assertEqual(sum(sizes), 13)
        self.assertTrue(all(n <= newsletter.WORK_PACKAGE_BATCH for n in sizes), sizes)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            vp = os.path.join(d, "v.json")
            json.dump(view(), open(vp, "w"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                newsletter.main([vp, "--work-package", os.path.join(d, "wp")])
            self.assertEqual(json.loads(buf.getvalue())["stories"], 2)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    newsletter.main([vp, "--work-package", d, "--outline"])
            self.assertEqual(cm.exception.code, 2)


class TestUpdatePass(unittest.TestCase):
    """One state per fact across sections; module lifecycle and what a
    module is; ownership from the delivered fact; automation from the view."""

    def v(self):
        v = view()
        v["prs"] += [
            {"number": 20, "title": "feat: thing", "author": "sam", "url": R + "/pull/20",
             "state": "open", "merged": False, "created_at": "2026-10-10T10:00:00Z",
             "review_state": {"state": "approved_stale", "basis": "alex 2026-10-12"},
             "fork_status": "fork"},
            {"number": 21, "title": "feat: other", "author": "sam", "url": R + "/pull/21",
             "state": "open", "merged": False, "created_at": "2026-10-10T10:00:00Z",
             "review_state": {"state": "automated_only"}, "fork_status": "not_fork"},
            {"number": 22, "title": "feat: old", "author": "sam", "url": R + "/pull/22",
             "state": "closed", "merged": False, "created_at": "2026-10-10T10:00:00Z",
             "closed_unmerged": {"superseded_by": 10}},
        ]
        return v

    def claims(self, text, v=None):
        return newsletter.state_claims([v or self.v()], text)

    # one state per fact
    def test_two_review_states_for_one_pr_fail(self):
        text = ("## Stories\n\n[#20](" + R + "/pull/20) was approved before the last push.\n\n"
                "## Coming up and stuck\n\n[#20](" + R + "/pull/20) is ready to merge.\n")
        r = self.claims(text)["review_state_mismatch"]
        self.assertEqual(r[0]["pr"], 20)
        self.assertEqual(r[0]["view"], "approved_stale")
        self.assertEqual([x["section"] for x in r[0]["stated"]], ["coming"])
        ok = text.replace("is ready to merge", "had an approval that predates later changes")
        self.assertEqual(self.claims(ok)["review_state_mismatch"], [])
        ok = text.replace("is ready to merge", "was approved on 12 October 2026, before the "
                          "author's latest changes, so it is not ready to merge")
        self.assertEqual(self.claims(ok)["review_state_mismatch"], [])

    def test_review_wording_against_the_view(self):
        r = self.claims("## People\n\n[#21](" + R + "/pull/21) was approved.\n")
        self.assertEqual(r["review_state_mismatch"][0]["view"], "automated_only")
        r = self.claims("## People\n\n[#21](" + R + "/pull/21) is awaiting a first review.\n")
        self.assertEqual(r["review_state_mismatch"], [])
        # "approved" as an adjective is no review state
        r = self.claims("## People\n\n[#21](" + R + "/pull/21) runs one test against an "
                        "approved test subscription.\n")
        self.assertEqual(r["review_state_mismatch"], [])
        r = self.claims("## People\n\n@alex approved [#21](" + R + "/pull/21).\n")
        self.assertEqual(r["review_state_mismatch"][0]["stated"][0]["claim"], "approved")
        # a sentence naming two PRs or a condition states nothing about one
        r = self.claims("## People\n\nOnce [#21](" + R + "/pull/21) is approved it can merge.\n")
        self.assertEqual(r["review_state_mismatch"], [])

    def test_a_count_of_many_prs_states_nothing_about_one(self):
        r = self.claims("## Plumbing: what was engineered\n\n2 were merged, 1 was closed without "
                        "merging and one, [#21](" + R + "/pull/21), is an open draft.\n")
        self.assertEqual(r["pr_state_mismatch"], [])

    def test_sections_disagreeing_without_a_view_state_fail(self):
        v = self.v()
        v["prs"][-2].pop("review_state")
        text = ("## People\n\n[#21](" + R + "/pull/21) is ready to merge.\n\n"
                "## Coming up and stuck\n\n[#21](" + R + "/pull/21) awaits a first review.\n")
        r = self.claims(text, v)["review_state_mismatch"]
        self.assertEqual((r[0]["pr"], r[0]["view"]), (21, None))
        self.assertEqual(len(r[0]["stated"]), 2)

    def test_pr_state_and_release_status_across_sections(self):
        r = self.claims("## People\n\n[#10](" + R + "/pull/10) is still open.\n\n"
                        "## Stories\n\n[#22](" + R + "/pull/22) was closed without being merged.\n")
        self.assertEqual([(x["pr"], x["view"]) for x in r["pr_state_mismatch"]],
                         [(10, "merged")])
        r = self.claims("## Stories\n\n`avm/res/hci/cluster` 0.3.0 was published.\n\n"
                        "## People\n\nThe module `avm/ptn/net/dns` is not yet published "
                        "as 0.7.4.\n")
        self.assertEqual(sorted((x["version"], x["view"]) for x in r["release_status_mismatch"]),
                         [("avm/ptn/net/dns/0.7.4", "published"),
                          ("avm/res/hci/cluster/0.3.0", "declared_not_published")])

    def test_check_fails_on_a_story_block_that_disagrees(self):
        v = self.v()
        text = GOOD.replace("**What landed.** It landed.",
                            "**What landed.** [#20](" + R + "/pull/20) is ready to merge.")
        r = newsletter.check_newsletter([v], text, slices={"train-issue-5": SLICE})
        self.assertEqual(r["review_state_mismatch"][0]["stated"][0]["section"], "stories")
        self.assertFalse(r["ok"])
        r = newsletter.check_newsletter([v], GOOD, slices={"train-issue-5": SLICE})
        self.assertEqual((r["review_state_mismatch"], r["pr_state_mismatch"],
                          r["release_status_mismatch"]), ([], [], []))

    # PR rows and coming up
    def test_pr_rows_carry_one_review_state_fork_status_and_closing(self):
        v = self.v()
        row = newsletter._pr_row(v, 20)
        self.assertEqual((row["review_state"]["state"], row["fork_status"], row["is_fork"]),
                         ("approved_stale", "fork", True))
        self.assertEqual(newsletter._pr_row(v, 22)["closed_unmerged"], {"superseded_by": 10})
        self.assertEqual(newsletter._pr_row(v, 11)["fork_status"], "unknown")

    def test_approved_stale_is_not_ready_and_proposed_modules_come_from_the_view(self):
        b = enriched(bundle([pr(1, created_at="2026-05-03T10:00:00Z", reviews=[
            review("bob", "2026-05-10T10:00:00Z", "approved", 1)])]))
        b["prs"][0]["review_state"] = {"state": "approved_stale"}
        b["trains"] = [{"id": "train-pr-1", "outcome": "in_flight", "prs": [1], "issues": []}]
        b["stories"] = dict(b.get("stories") or {}, all=[
            {"id": "train-pr-1", "group": "in_flight", "state": "in_flight",
             "new_module_proposed": ["avm/res/x/y"],
             "module_info": {"avm/res/x/y": {"display_name": "X Y"}}}])
        c = section(newsletter.outline([b]), "coming")
        self.assertEqual(c["ready"], [])
        self.assertEqual([r["number"] for r in c["approval_stale"]], [1])
        self.assertFalse(c["approval_stale"][0]["approved"])
        self.assertEqual(c["proposed_modules"][0]["module"], "avm/res/x/y")
        self.assertEqual(c["proposed_modules"][0]["info"]["display_name"], "X Y")
        self.assertEqual(c["proposed_modules"][0]["prs"][0]["review_state"], "approved_stale")

    def test_story_rows_carry_what_the_view_says_beyond_the_group(self):
        v = self.v()
        v["trains"].append({"id": "train-pr-20", "outcome": "in_flight", "prs": [20],
                            "issues": [], "code_areas": ["avm/res/x/y"]})
        v["stories"]["all"] = [
            {"id": "train-pr-20", "group": "in_flight", "state": "in_flight",
             "worthy": "PR #20 opened in the window", "modules": ["avm/res/x/y"],
             "module_info": {"avm/res/x/y": {"display_name": "X Y"}},
             "status_reason": "open, active in the window", "new_module_proposed": ["avm/res/x/y"]}]
        st = section(newsletter.outline([v]), "stories")
        row = next(r for r in st["full"] if r["key"] == "train-pr-20")
        self.assertEqual(row["status_reason"], "open, active in the window")
        self.assertEqual(row["new_module_proposed"], ["avm/res/x/y"])
        self.assertEqual(row["review_states"], {"20": "approved_stale"})
        self.assertEqual(row["modules"][0]["info"], {"display_name": "X Y"})

    # automation from the view
    def test_automation_from_the_view_marking(self):
        v = self.v()
        v["people"]["agent"] = {"automation": {"kind": "account_type", "on_behalf_of": "alex"}}
        v["people"]["helper[bot]"] = {"automation": None}
        self.assertTrue(newsletter.is_automation(v, "agent"))
        self.assertFalse(newsletter.is_automation(v, "helper[bot]"))
        self.assertTrue(newsletter.is_automation(v, "other[bot]"))
        v["prs"][-3]["author"] = "agent"
        auto = {a["login"]: a for a in section(newsletter.outline([v]), "people")["automation"]}
        self.assertEqual((auto["agent"]["kind"], auto["agent"]["on_behalf_of"]),
                         ("account_type", "alex"))
        self.assertEqual(auto["agent"]["prs"][0]["review_state"]["state"], "approved_stale")
        self.assertNotIn("helper[bot]", auto)

    # what a module is; release contents; deprecations
    def test_module_rows_say_what_the_module_is_and_releases_their_contents(self):
        v = self.v()
        dns = v["module_changes"]["modules"][0]
        dns.update(display_name="DNS zones", description="Deploys private DNS zones.",
                   resource_type="Microsoft.Network/privateDnsZones")
        dns["versions"]["published"][0]["contents"] = {"source": "commits", "prs": [11]}
        mc = section(newsletter.outline([v]), "module_changes")
        row = next(m for m in mc["modules"] if m["area"] == "avm/ptn/net/dns")
        self.assertEqual(row["info"]["display_name"], "DNS zones")
        self.assertEqual(row["release"][0]["contents"], {"source": "commits", "prs": [11]})
        fac = v["module_changes"]["modules"][1]
        fac["versions"]["published"][1]["contents"] = {"source": "commits_between_tags",
                                                       "prs": [], "commits": []}
        mc = section(newsletter.outline([v]), "module_changes")
        row = next(m for m in mc["modules"] if m["area"] == "avm/res/data/factory")
        self.assertEqual([(c["module"], c["version"], c["own_change"], c.get("note"))
                          for c in row["child_releases"]],
                         [("avm/res/data/factory/pipe", "0.1.2", False,
                           "republished with its parent")])
        v["module_ownership"] = {"collected": True, "modules_at_end": {
            "avm/res/hci/cluster": {"display_name": "HCI cluster", "lifecycle": "live"}}}
        self.assertEqual(newsletter.module_info(v, "avm/res/hci/cluster"),
                         {"display_name": "HCI cluster", "lifecycle": "live"})

    def test_deprecation_from_a_lifecycle_event(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "deprecated", "module": "avm/res/data/factory/pipe", "date": "2026-10-03",
             "marker_text": "Use the parent module instead.",
             "source": {"type": "pr", "number": 10, "url": R + "/pull/10"}}]}
        br = section(newsletter.outline([v]), "breaking")
        d = next(x for x in br["deprecations"] if x["target"] == "avm/res/data/factory/pipe")
        self.assertEqual((d["area"], d["basis"], d["date"]),
                         ("avm/res/data/factory", ["lifecycle"], "2026-10-03"))
        own = section(newsletter.outline([v]), "ownership")
        self.assertEqual(own["lifecycle_events"][0]["kind"], "deprecated")
        self.assertEqual(own["events"], [])

    # ownership from the delivered fact
    def test_ownership_from_start_state_and_lifecycle_lists(self):
        v = self.v()
        v["module_ownership"] = {
            "collected": True, "events": [],
            "without_owner": [
                {"module": "avm/res/key/vault", "lifecycle": "live", "marker": None},
                {"module": "avm/res/code/signing", "lifecycle": "orphaned",
                 "marker": "ORPHAN.md"}],
            "deprecated_modules": [{"module": "avm/res/old/one", "deprecated_text": "Use x."}],
            "stub_modules": [{"module": "avm/res/stub/a"}, {"module": "avm/res/stub/b"}],
            "start_state": {
                "avm/res/key/vault": {"owners": [], "owner_file": "CODEOWNERS", "entry": True},
                "avm/res/hci/cluster": {"owners": ["alex"], "owner_file": "CODEOWNERS",
                                        "entry": True},
                "avm/res/old/one": {"owners": [], "deprecated": True, "entry": True}}}
        own = section(newsletter.outline([v]), "ownership")
        self.assertIsNone(own["omit"])
        self.assertEqual(own["without_owner_by_kind"],
                         {"empty_owner_list": ["avm/res/key/vault"],
                          "orphan_marker": ["avm/res/code/signing"]})
        self.assertEqual([x["module"] for x in own["start_without_owner"]],
                         ["avm/res/key/vault"])
        self.assertEqual(own["deprecated_modules"][0]["module"], "avm/res/old/one")
        self.assertEqual(own["stub_modules_count"], 2)
        text = GOOD.replace(
            "<!-- omitted: ownership — needs a store gathered with module ownership -->",
            "## Module ownership\n\n[`avm/res/old/one`](" + T + "avm/res/old/one) is "
            "deprecated.")
        r = newsletter.check_newsletter([v], text, slices={"train-issue-5": SLICE})
        self.assertEqual(r["ownership_not_in_view"], [])

    def test_unavailable_ownership_never_says_no_module_without_an_owner(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [],
                                 "without_owner": [{"module": "avm/res/key/vault"}],
                                 "without_owner_unavailable": "owner file not read at end"}
        out = newsletter.outline([v])
        own = section(out, "ownership")
        self.assertEqual(own["without_owner"], [])
        self.assertIn("not known", own["omit"])
        self.assertNotIn("no module without an owner", own["omit"])
        cov = section(out, "numbers")["coverage"]
        self.assertTrue(any("no owner is not known" in c for c in cov), cov)
        # a stream: one member quiet, one unavailable
        a, b = view(), self.v()
        a["module_ownership"] = {"collected": True, "events": [], "without_owner": []}
        b["meta"] = dict(b["meta"], repo="s")
        b["module_ownership"] = v["module_ownership"]
        own = section(newsletter.outline([a, b]), "ownership")
        self.assertIn("not known", own["omit"])
        self.assertEqual(own["without_owner_unavailable"],
                         [{"repo": "o/s", "reason": "owner file not read at end"}])

    # cadence
    def test_short_window_is_weekly_unless_monthly_is_asked(self):
        v = self.v()
        v["meta"]["period"] = {"from": "2026-10-01", "to": "2026-10-14"}
        out = newsletter.outline([v])
        self.assertEqual((out["cadence"], out["cadence_source"], out["window_days"]),
                         ("week", "window", 14))
        self.assertEqual(newsletter.outline([v], cadence="month")["cadence"], "month")
        self.assertEqual(newsletter.outline([self.v()])["cadence"], "month")
        with tempfile.TemporaryDirectory() as d:
            vp = os.path.join(d, "v.json")
            with open(vp, "w") as fh:
                json.dump(v, fh)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                newsletter.main([vp, "--outline", "--monthly"])
            self.assertEqual(json.loads(buf.getvalue())["cadence"], "month")


class TestRefreshPass(unittest.TestCase):
    """Orphan and shared notices, proposed modules from the tree, where a
    closed PR's work landed, why a member joined, net change, the person an
    automation account worked for."""

    def v(self):
        v = TestUpdatePass().v()
        v["module_ownership"] = {
            "collected": True, "events": [],
            "without_owner": [{"module": "avm/res/code/signing", "lifecycle": "orphaned",
                               "marker": "ORPHANED.md",
                               "marker_text": "THIS MODULE IS CURRENTLY ORPHANED."}],
            "deprecated_modules": [{"module": "avm/res/old/one", "marker_text_shared": True},
                                   {"module": "avm/res/old/two", "marker_text_shared": True}],
            "marker_texts": {"deprecated": {"modules": 2, "text": "THIS MODULE IS DEPRECATED."}},
            "proposed_modules": {"avm/res/x/new": {"prs": [21], "info": {
                "display_name": "X New", "resource_type": "Microsoft.X/new"}}}}
        v["trains"].append({"id": "train-pr-21", "outcome": "in_flight", "prs": [21],
                            "issues": [], "joined_by": [
                                {"item": {"type": "issue", "number": 5}, "via": "closes",
                                 "by": {"type": "pr", "number": 21}, "quote": "Fixes #5"}]})
        v["stories"]["all"] = [{"id": "train-pr-21", "group": "in_flight", "state": "in_flight",
                                "worthy": "PR #21 opened in the window"}]
        v["prs"][-1]["closed_unmerged"] = {"close_reason": "not stated", "content_landed_in": [
            {"pr": 10, "basis": "same commit", "commits": [{"sha": "abc", "subject": "fix: x"}]}]}
        v["plumbing_themes"] = [{"id": "ci", "title_hint": "CI", "prs": [
            {"number": 12, "net_files": {"files": 3, "added": 10, "removed": 2, "commits": 2}}],
            "stories": [], "people": []}]
        v["people"]["agent"] = {"automation": {"kind": "account_type", "on_behalf_of_by_pr": {
            "20": {"login": "alex", "basis": "assignee", "pr": 20}}}}
        v["prs"][-3]["author"] = "agent"
        return v

    def test_ownership_quotes_notices_once_and_lists_proposed_modules(self):
        own = section(newsletter.outline([self.v()]), "ownership")
        self.assertEqual(own["without_owner"][0]["marker_text"],
                         "THIS MODULE IS CURRENTLY ORPHANED.")
        self.assertEqual(own["marker_texts"]["deprecated"]["modules"], 2)
        self.assertEqual(own["deprecated_replacements"], [])
        self.assertEqual([(p["module"], p["info"]["display_name"]) for p in own["proposed_modules"]],
                         [("avm/res/x/new", "X New")])

    def test_coming_lists_every_proposed_module_with_what_it_is(self):
        c = section(newsletter.outline([self.v()]), "coming")
        row = next(p for p in c["proposed_modules"] if p["module"] == "avm/res/x/new")
        self.assertEqual((row["story"], row["info"]["resource_type"], row["prs"][0]["number"]),
                         ("train-pr-21", "Microsoft.X/new", 21))

    def test_rows_carry_join_reason_net_change_and_principal(self):
        v = self.v()
        st = section(newsletter.outline([v]), "stories")
        row = next(r for r in st["all"] if r["key"] == "train-pr-21")
        self.assertEqual(row["joined_by"][0]["quote"], "Fixes #5")
        self.assertEqual(newsletter._pr_row(v, 12)["net_files"]["files"], 3)
        self.assertEqual(newsletter._pr_row(v, 20)["author_on_behalf_of"]["login"], "alex")
        self.assertEqual(newsletter._pr_row(v, 22)["closed_unmerged"]["content_landed_in"][0]["pr"],
                         10)
        with tempfile.TemporaryDirectory() as d:
            newsletter.work_package([v], d)
            th = json.load(open(os.path.join(d, "rows", "theme-ci.json")))
            self.assertEqual(th["theme"]["prs"][0]["net_files"]["added"], 10)

    def test_superseded_only_where_the_record_says_so(self):
        v = self.v()
        bad = "## Stories\n\n[#22](" + R + "/pull/22) was superseded by [#10](" + R + "/pull/10).\n"
        r = newsletter.state_claims([v], bad)["superseded_not_in_view"]
        self.assertEqual((r[0]["pr"], r[0]["content_landed_in"]), (22, [10]))
        ok = ("## Stories\n\n[#22](" + R + "/pull/22) was closed unmerged; its commit is part of "
              "[#10](" + R + "/pull/10).\n")
        self.assertEqual(newsletter.state_claims([v], ok)["superseded_not_in_view"], [])
        v["prs"][-1]["closed_unmerged"] = {"superseded_by": 10, "quote": "superseded by #10"}
        self.assertEqual(newsletter.state_claims([v], bad)["superseded_not_in_view"], [])


class TestSocialPosts(unittest.TestCase):
    """Daily social posts: one per story at the first event a reader can act
    on, the release hold, the ledger, the limits and the post check."""

    def v(self):
        v = view()
        rel = {"module": "avm/ptn/net/dns", "version": "0.7.4", "status": "published",
               "url": "https://github.com/o/r/tree/avm/ptn/net/dns/0.7.4",
               "date": "2026-10-07T09:00:00Z"}
        v["stories"]["all"] = [
            {"id": "train-pr-11", "group": "landed", "state": "landed", "score": 20,
             "worthy": "x", "released": [rel], "modules": ["avm/ptn/net/dns"]},
            {"id": "train-issue-5", "group": "landed", "state": "landed", "score": 30,
             "worthy": "x", "modules": ["avm/res/data/factory"],
             "released": [{"module": "avm/res/data/factory", "version": "0.13.0",
                           "status": "declared_not_published"}]},
            {"id": "train-pr-13", "group": "landed", "state": "landed", "score": 5,
             "worthy": "x", "released": [{"status": "no_version_change"}]},
            {"id": "train-pr-12", "group": "plumbing", "state": "landed", "score": 1,
             "worthy": "x"}]
        v["plumbing_themes"] = [{"id": "ci", "title_hint": "CI workflows", "prs": [{"number": 12}],
                                 "stories": ["train-pr-12"], "people": ["jared"]}]
        return v

    def cand(self, v=None):
        return newsletter.social_candidates([v or self.v()])

    def test_trigger_rule_and_hold(self):
        c = self.cand()
        by = {p["story"]: p for p in c["posts"] if p["kind"] == "story"}
        self.assertEqual((by["train-pr-11"]["day"], by["train-pr-11"]["trigger"]),
                         ("2026-10-07", "published"))
        self.assertEqual(by["train-pr-11"]["must_state"]["released"][0]["version"], "0.7.4")
        # merged 5 October, no release: held 3 days, then "merged, not yet published"
        self.assertEqual((by["train-issue-5"]["day"], by["train-issue-5"]["trigger"]),
                         ("2026-10-08", "merged_not_published"))
        self.assertEqual(by["train-issue-5"]["must_state"]["breaking"], "declared")
        # work that never releases posts on its merge day
        self.assertEqual((by["train-pr-13"]["day"], by["train-pr-13"]["trigger"]),
                         ("2026-10-09", "merged_no_release"))
        # plumbing: no daily post, one weekly item per theme with activity
        self.assertNotIn("train-pr-12", by)
        # ONE weekly post per week (the owner's decision), every theme in it
        self.assertEqual([(w["post_id"], w["day"]) for w in c["weekly"]],
                         [("weekly:o/r:2026-W40", "2026-10-04")])
        self.assertEqual([t["theme"] for t in c["weekly"][0]["must_state"]["themes"]], ["ci"])
        d = newsletter.daily_outline([self.v()], "2026-10-06", candidates=c)
        self.assertEqual([h["story"] for h in d["held"]], ["train-issue-5"])
        self.assertEqual(d["posts"], [])
        # a release later than the hold does not move the post
        v = self.v()
        v["stories"]["all"][0]["released"][0]["date"] = "2026-10-20T00:00:00Z"
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-pr-11")
        self.assertEqual((p["day"], p["trigger"]), ("2026-10-09", "merged_not_published"))

    def test_one_post_per_story_and_the_ledger(self):
        c = self.cand()
        ids = [p["post_id"] for p in c["posts"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("story:o/r:train-pr-11", ids)
        posted = newsletter.read_ledger({"kind": "social_ledger", "posts": [
            {"post_id": "story:o/r:train-pr-11", "day": "2026-10-07",
             "channels": ["x", "linkedin"], "urls": {"x": "https://example.invalid/1"}}]})
        d = newsletter.daily_outline([self.v()], "2026-10-07", posted=posted, candidates=c)
        self.assertEqual((d["posts"], d["already_posted"]), ([], ["story:o/r:train-pr-11"]))
        with self.assertRaises(ValueError):
            newsletter.read_ledger({"posts": [{"day": "2026-10-07"}]})

    def test_module_events_and_open_pr_declarations(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
             "people": ["alex"], "source": {"type": "commit", "url": R + "/commit/abc"}}]}
        v["prs"].append({"number": 30, "title": "fix: x", "author": "sam", "url": R + "/pull/30",
                         "state": "open", "merged": False, "created_at": "2026-10-10T10:00:00Z"})
        v["trains"].append({"id": "train-pr-30", "outcome": "in_flight", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight",
                                    "state": "in_flight", "worthy": "x"})
        v["pr_history"] = {"prs": [{"number": 30, "commits": [
            {"sha": "c1", "committed_at": "2026-10-08T10:00:00Z"}], "patches": [
            {"path": "avm/res/hci/cluster/CHANGELOG.md", "commit": "c1", "added": [
                "## 0.4.0", "", "### Breaking Changes", "", "- Removed `x`."]}]}]}
        c = self.cand(v)
        ev = next(p for p in c["posts"] if p["trigger"] == "owner_added")
        self.assertEqual((ev["day"], ev["must_state"]["people"]), ("2026-10-06", ["Alex Sehr"]))
        br = next(p for p in c["posts"] if p.get("story") == "train-pr-30")
        # not before the pull request exists
        self.assertEqual((br["trigger"], br["day"]), ("breaking_declared_open", "2026-10-10"))
        self.assertEqual(br["must_state"]["items"], ["Removed `x`."])

    POST = ("<!-- post: story:o/r:train-pr-11 channel: x day: 2026-10-07 -->\n"
            "The private DNS zones pattern avm/ptn/net/dns was published as 0.7.4 with the "
            "Redis zone: https://github.com/o/r/tree/avm/ptn/net/dns/0.7.4\n<!-- /post -->\n"
            "<!-- post: story:o/r:train-pr-11 channel: linkedin day: 2026-10-07 -->\n"
            "Version 0.7.4 of the private DNS zones pattern (avm/ptn/net/dns) was published on "
            "7 October 2026. It adds the Redis zone.\n\n"
            "https://github.com/o/r/tree/avm/ptn/net/dns/0.7.4\n\n#Bicep #Azure\n<!-- /post -->\n")

    def check(self, text, **kw):
        return newsletter.check_posts([self.v()], text, **kw)

    def test_good_posts_pass(self):
        r = self.check(self.POST)
        self.assertTrue(r["ok"], r["posts"])
        self.assertTrue(r["posts"][0]["length"] <= newsletter.X_LIMIT)

    def test_limits_and_form(self):
        self.assertEqual(newsletter.x_length("see https://example.invalid/" + "a" * 200),
                         4 + newsletter.X_LINK_WEIGHT)
        long = self.POST.replace("with the Redis zone", "with the Redis zone " + "and more " * 40)
        self.assertIn("too_long", self.check(long)["posts"][0])
        tags = self.POST.replace("#Bicep #Azure", "#Bicep #Azure #IaC #Cloud")
        self.assertIn("too_many_hashtags", self.check(tags)["posts"][1])
        md = self.POST.replace("It adds the Redis zone.", "It adds the **Redis** zone.")
        self.assertIn("markdown", self.check(md)["posts"][1])

    def test_must_state_and_record(self):
        r = self.check(self.POST.replace("was published as 0.7.4", "was merged"))
        self.assertIn("published version 0.7.4", r["posts"][0]["must_state_missing"])
        r = self.check(self.POST.replace("It adds the Redis zone.",
                                         "It adds the Redis zone, \"as nobody ever said\"."))
        self.assertTrue(r["posts"][1]["quotes_not_in_view"])
        r = self.check(self.POST.replace("It adds the Redis zone.", "It is a breaking change."))
        self.assertTrue(r["posts"][1]["breaking_not_declared"])
        self.assertFalse(r["ok"])

    def test_quotes_are_plain_text_without_code_spans(self):
        # the record's changelog has `privatelink.redis.azure.net` in backticks
        t = self.POST.replace("It adds the Redis zone.",
                              "Its changelog: \"Added Redis zone privatelink.redis.azure.net\".")
        r = self.check(t)
        self.assertTrue(r["ok"], r["posts"])
        r = self.check(t.replace("privatelink.redis.azure.net\"", "privatelink.redis.example\""))
        self.assertTrue(r["posts"][1]["quotes_not_in_view"])

    def test_handles_only_when_confirmed(self):
        t = self.POST.replace("with the Redis zone", "with the Redis zone by @nikp")
        self.assertEqual(self.check(t)["posts"][0]["handles_not_confirmed"], ["nikp"])
        reg = {"people": {"nik": {"social": {"x": {"handle": "nikp", "confirmed": True}}}}}
        self.assertNotIn("handles_not_confirmed", self.check(t, registry=reg)["posts"][0])
        reg["people"]["nik"]["social"]["x"]["confirmed"] = False
        self.assertIn("handles_not_confirmed", self.check(t, registry=reg)["posts"][0])

    def test_voice_pass_may_change_wording_not_facts(self):
        voiced = self.POST.replace(
            "The private DNS zones pattern avm/ptn/net/dns was published as 0.7.4 with the Redis zone",
            "We published 0.7.4 of avm/ptn/net/dns, our private DNS zones pattern, with the Redis zone")
        r = self.check(voiced, against=self.POST)
        self.assertTrue(r["ok"], r["posts"])
        changed = self.POST.replace("published as 0.7.4", "published as 0.7.5")
        r = self.check(changed, against=self.POST)
        self.assertIn("facts_changed", r["posts"][0])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            vp = os.path.join(d, "v.json")
            json.dump(self.v(), open(vp, "w"))
            led = os.path.join(d, "ledger.json")
            json.dump({"kind": "social_ledger", "posts": [
                {"post_id": "story:o/r:train-pr-11", "day": "2026-10-07", "channels": ["x"]}]},
                open(led, "w"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                newsletter.main([vp, "--outline", "--daily", "--day", "2026-10-07",
                                 "--posted", led])
            out = json.loads(buf.getvalue())
            self.assertEqual((out["kind"], out["already_posted"]),
                             ("daily_outline", ["story:o/r:train-pr-11"]))
            pp = os.path.join(d, "p.md")
            open(pp, "w").write(self.POST)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    newsletter.main([vp, "--check-post", pp])
            self.assertEqual(cm.exception.code, 0)


class TestSocialDecisionsAndReview(TestSocialPosts):
    """The owner's decisions on social posts and the PR #122 review findings."""

    def _closed_issue(self, v, n=40, created="2026-10-10T09:00:00Z", at="2026-10-14T09:00:00Z",
                      by="kim", reason="not_planned"):
        v["issues"].append({"number": n, "title": "[AVM Module Issue]: x", "author": "kim",
                            "url": R + "/issues/%d" % n, "state": "closed", "created_at": created,
                            "closing": {"at": at, "by": by, "state_reason": reason}})
        v["trains"].append({"id": "train-issue-%d" % n, "outcome": "abandoned",
                            "issues": [n], "prs": []})
        v["stories"]["all"].append({"id": "train-issue-%d" % n, "group": "closed",
                                    "state": "closed", "worthy": "x"})
        return v

    # owner's decisions
    def test_one_weekly_post_biggest_theme_first_with_folded_closures(self):
        v = self.v()
        v["plumbing_themes"].append({"id": "docs", "title_hint": "Docs", "prs": [{"number": 13}],
                                     "stories": [], "people": []})
        v["plumbing_themes"][0]["prs"].append({"number": 10})
        self._closed_issue(v, n=41, created="2026-10-01T09:00:00Z", at="2026-10-01T10:00:00Z")
        c = self.cand(v)
        w = [x for x in c["weekly"] if x["post_id"] == "weekly:o/r:2026-W40"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["must_state"]["themes"][0]["theme"], "ci")
        self.assertEqual(w[0]["must_state"]["closures_not_posted"][0]["exclusion"],
                         "quick_own_close")

    def test_closed_request_posts_with_its_reason_and_exclusions(self):
        v = self._closed_issue(self.v())
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-issue-40")
        self.assertEqual((p["trigger"], p["day"], p["must_state"]["reason"]),
                         ("closed_not_planned", "2026-10-14", "not_planned"))
        post = ("<!-- post: story:o/r:train-issue-40 channel: x day: 2026-10-14 -->\n"
                "A request for the data factory module was closed. " + R + "/issues/40\n"
                "<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("the stated reason 'not planned'", r["posts"][0]["must_state_missing"])
        r = newsletter.check_posts([v], post.replace("was closed.", "was closed as not planned."))
        self.assertTrue(r["ok"], r["posts"])
        # closed by its own author within a day of opening: folded, not posted
        v = self._closed_issue(self.v(), created="2026-10-14T01:00:00Z")
        c = self.cand(v)
        self.assertFalse(any(x.get("story") == "train-issue-40" for x in c["posts"]))
        self.assertEqual(c["excluded"][0]["exclusion"], "quick_own_close")

    def test_closing_the_loop_once_with_where_the_content_landed(self):
        v = self.v()
        v["prs"].append({"number": 30, "title": "feat: new module", "author": "sam",
                         "url": R + "/pull/30", "state": "closed", "merged": False,
                         "created_at": "2026-10-07T10:00:00Z", "closed_at": "2026-10-20T10:00:00Z",
                         "closed_unmerged": {"close_reason": "not stated", "content_landed_in": [
                             {"pr": 13, "basis": "same commit"}]}})
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-07T09:00:00Z"}}}
        c = self.cand(v)
        first = next(x for x in c["posts"] if x["post_id"] == "story:o/r:train-pr-30")
        loop = next(x for x in c["posts"] if x["post_id"] == "story:o/r:train-pr-30:closed")
        self.assertEqual((first["trigger"], loop["trigger"], loop["first_post_id"], loop["day"]),
                         ("new_module_proposed", "closed_after_post", "story:o/r:train-pr-30",
                          "2026-10-20"))
        post = ("<!-- post: story:o/r:train-pr-30:closed channel: x day: 2026-10-20 -->\n"
                "The proposal was closed without merging; no reason is stated. Its commit is part of "
                "#13. " + R + "/pull/30\n<!-- /post -->\n")
        self.assertTrue(newsletter.check_posts([v], post)["ok"])
        r = newsletter.check_posts([v], post.replace(" Its commit is part of #13.", ""))
        self.assertIn("where its content landed (#13)", r["posts"][0]["must_state_missing"])
        r = newsletter.check_posts([v], post.replace("Its commit is part of #13.",
                                                     "It was superseded by #13."))
        self.assertTrue(r["posts"][0]["superseded_not_in_view"])

    def test_label_only_orphan_is_suppressed_when_an_owner_follows(self):
        v = self.v()
        ev = {"kind": "orphaned", "module": "avm/res/hci/cluster", "date": "2026-10-02",
              "source": {"type": "pr", "number": 10, "url": R + "/pull/10"}, "via": "label"}
        own = {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
               "people": ["alex"], "source": {"type": "commit", "url": R + "/commit/a"}}
        v["module_ownership"] = {"collected": True, "events": [ev, own]}
        kinds = [p["trigger"] for p in self.cand(v)["posts"] if p["kind"] == "module_event"]
        self.assertEqual(kinds, ["owner_added"])
        # no owner within ORPHAN_LABEL_HOLD_DAYS: the label-only signal posts after the hold
        v["module_ownership"]["events"] = [ev]
        p = next(p for p in self.cand(v)["posts"] if p["kind"] == "module_event")
        self.assertEqual((p["trigger"], p["day"]), ("orphaned", "2026-10-09"))
        ev2 = dict(ev, source={"type": "commit", "url": R + "/commit/b"}, via="ORPHANED.md")
        v["module_ownership"]["events"] = [ev2, own]
        kinds = [p["trigger"] for p in self.cand(v)["posts"] if p["kind"] == "module_event"]
        self.assertEqual(sorted(kinds), ["orphaned", "owner_added"])

    def test_deprecation_states_its_replacement_or_none(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "deprecated", "module": "avm/res/hci/cluster", "date": "2026-10-03",
             "source": {"type": "commit", "url": R + "/commit/d"}}],
            "deprecated_modules": [{"module": "avm/res/hci/cluster"}]}
        url = T + "avm/res/hci/cluster"
        post = ("<!-- post: deprecated:o/r:avm/res/hci/cluster:2026-10-03 channel: x "
                "day: 2026-10-03 -->\nThe module avm/res/hci/cluster was deprecated. " + url +
                "\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("that no replacement is named", r["posts"][0]["must_state_missing"])
        self.assertTrue(newsletter.check_posts([v], post.replace(
            "was deprecated.", "was deprecated; no replacement is named."))["ok"])
        v["module_ownership"]["deprecated_modules"][0]["replacement"] = {
            "module": "avm/ptn/net/dns", "quote": "use avm/ptn/net/dns instead"}
        r = newsletter.check_posts([v], post.replace("was deprecated.",
                                                     "was deprecated; no replacement is named."))
        self.assertIn("the replacement avm/ptn/net/dns", r["posts"][0]["must_state_missing"])

    # review findings
    def test_f1_suppressed_person_handle_never_allowed(self):
        v = self.v()
        v["affiliations"]["people"]["nik"] = {"signal": "do_not_name"}
        reg = {"people": {"NIK": {"social": {"x": {"handle": "nikp", "confirmed": True}}}}}
        t = self.POST.replace("with the Redis zone", "with the Redis zone by @nikp")
        r = newsletter.check_posts([v], t, registry=reg)
        self.assertEqual(r["posts"][0]["handles_not_confirmed"], ["nikp"])

    def test_f2_breaking_heading_and_item_in_different_commits(self):
        v = self.v()
        v["prs"].append({"number": 30, "title": "fix", "author": "sam", "url": R + "/pull/30",
                         "state": "open", "merged": False, "created_at": "2026-10-05T10:00:00Z"})
        v["pr_history"] = {"prs": [{"number": 30, "commits": [
            {"sha": "c1", "committed_at": "2026-10-06T10:00:00Z"},
            {"sha": "c2", "committed_at": "2026-10-08T10:00:00Z"}], "patches": [
            {"path": "x/CHANGELOG.md", "commit": "c1",
             "added": ["## 0.4.0", "### Breaking Changes", "- None"]},
            {"path": "x/CHANGELOG.md", "commit": "c2", "removed": ["- None"],
             "added": ["- Removed `y`."]}]}]}
        b = newsletter._open_pr_breaking(v, 30)
        self.assertEqual((b[0]["items"], b[0]["commit"]), (["Removed `y`."], "c2"))
        # a later item that removes no placeholder is not under the Breaking heading
        hist = v["pr_history"]["prs"][0]["patches"]
        hist[1] = {"path": "x/CHANGELOG.md", "commit": "c2", "added": ["- Fixed ordering."]}
        self.assertEqual(newsletter._open_pr_breaking(v, 30), [])
        # nor when another section's placeholder could be the removed one
        hist[0]["added"] = ["## 0.4.0", "### Changes", "- None", "### Breaking Changes", "- None"]
        hist[1] = {"path": "x/CHANGELOG.md", "commit": "c2", "removed": ["- None"],
                   "added": ["- Fixed ordering."]}
        self.assertEqual(newsletter._open_pr_breaking(v, 30), [])

    def test_f3_breaking_only_from_this_trains_declarations(self):
        v = self.v()
        v["prs"].append({"number": 31, "title": "docs", "author": "sam", "url": R + "/pull/31",
                         "state": "closed", "merged": True, "merged_at": "2026-10-09T10:00:00Z"})
        v["trains"].append({"id": "train-pr-31", "outcome": "shipped", "prs": [31], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-31", "group": "landed", "state": "landed",
                                    "worthy": "x", "modules": ["avm/res/data/factory"],
                                    "released": [{"status": "no_version_change"}]})
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-pr-31")
        self.assertIsNone(p["must_state"]["breaking"])
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-issue-5")
        self.assertEqual(p["must_state"]["breaking"], "declared")

    def test_f4_proposal_waits_for_its_own_pr(self):
        v = self.v()
        for n, created in ((30, "2026-10-02T10:00:00Z"), (31, "2026-10-12T10:00:00Z")):
            v["prs"].append({"number": n, "title": "x", "author": "sam", "url": R + "/pull/%d" % n,
                             "state": "open", "merged": False, "created_at": created})
        v["trains"].append({"id": "train-pr-30", "outcome": "in_flight", "prs": [30, 31],
                            "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [31], "committed_at": "2026-10-03T09:00:00Z"}}}
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-pr-30")
        self.assertEqual(p["day"], "2026-10-12")

    def test_f5_voice_pass_may_not_swap_a_person(self):
        a = self.POST.replace("It adds the Redis zone.", "Nik Peters added the Redis zone.")
        b = a.replace("Nik Peters", "Alex Sehr")
        r = self.check(b, against=a)
        self.assertEqual(r["posts"][1]["facts_changed"]["people"],
                         {"added": ["alex"], "removed": ["nik"]})

    def test_f6_module_event_must_state_the_event(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
             "people": ["alex"], "source": {"type": "commit", "url": R + "/commit/abc"}}]}
        post = ("<!-- post: owner_added:o/r:avm/res/hci/cluster:2026-10-06 channel: x "
                "day: 2026-10-06 -->\navm/res/hci/cluster " + R + "/commit/abc\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("the event (owner added)", r["posts"][0]["must_state_missing"])
        self.assertTrue(newsletter.check_posts([v], post.replace(
            "avm/res/hci/cluster ", "avm/res/hci/cluster has a new owner, Alex Sehr. "))["ok"])

    def test_f7_not_yet_published_is_true_before_a_later_release(self):
        v = self.v()
        v["stories"]["all"][0]["released"][0]["date"] = "2026-10-20T00:00:00Z"
        v["module_changes"]["modules"][0]["versions"]["published"][0]["date"] = "2026-10-20T00:00:00Z"
        post = ("<!-- post: story:o/r:train-pr-11 channel: x day: 2026-10-09 -->\n"
                "Merged: avm/ptn/net/dns 0.7.4 is not yet published. " + R + "/pull/11\n"
                "<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertTrue(r["ok"], r["posts"])

    def test_f8_published_post_links_the_tag(self):
        r = self.check(self.POST.replace("https://github.com/o/r/tree/avm/ptn/net/dns/0.7.4",
                                         R + "/pull/11"))
        self.assertIn("the published version's tag link", r["posts"][0]["must_state_missing"])

    def test_f9_module_event_links_are_validated(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
             "people": ["alex"], "source": {"type": "commit", "url": R + "/commit/abc"}}]}
        post = ("<!-- post: owner_added:o/r:avm/res/hci/cluster:2026-10-06 channel: x "
                "day: 2026-10-06 -->\navm/res/hci/cluster has a new owner. " + R + "/pull/11\n"
                "<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("a link to the event's source or the module", r["posts"][0]["must_state_missing"])

    def test_f10_a_removed_block_fails_the_voice_pass(self):
        one = self.POST.split("<!-- post: story:o/r:train-pr-11 channel: linkedin")[0]
        r = self.check(one, against=self.POST)
        self.assertFalse(r["ok"])
        self.assertEqual(r["posts"][-1]["channel"], "linkedin")

    def test_f11_community_by_display_name(self):
        r = self.check(self.POST.replace("It adds the Redis zone.",
                                         "Community contributor Nik Peters added the Redis zone."))
        self.assertEqual(r["posts"][1]["affiliation_mismatch"][0]["name"], "Nik Peters")


class TestSocialReviewTwo(TestSocialDecisionsAndReview):
    """The second review round on PR #122 and its sibling defects."""

    def _pr(self, v, n, **kw):
        row = {"number": n, "title": "x", "author": "sam", "url": R + "/pull/%d" % n,
               "state": "open", "merged": False, "created_at": "2026-10-05T10:00:00Z"}
        row.update(kw)
        v["prs"].append(row)
        return row

    def test_g1_final_closed_event_decides_who_closed(self):
        v = self.v()
        pr = self._pr(v, 30, state="closed", closed_at="2026-10-09T10:00:00Z",
                      created_at="2026-10-08T10:00:00Z", lifecycle=[
                          {"event": "closed", "actor": "sam", "created_at": "2026-10-08T12:00:00Z"},
                          {"event": "reopened", "actor": "alex", "created_at": "2026-10-08T13:00:00Z"},
                          {"event": "closed", "actor": "alex", "created_at": "2026-10-09T10:00:00Z"}])
        c = newsletter._closing(v, "pr", pr)
        self.assertEqual(c["by"], "alex")
        self.assertIsNone(newsletter._closure_exclusion(v, "pr", pr, c))

    def test_g2_unknown_release_keeps_the_uncertainty(self):
        v = self.v()
        v["stories"]["all"][1]["released"][0]["status"] = "unknown"
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-issue-5")
        self.assertEqual(p["trigger"], "merged_release_unknown")
        url = R + "/pull/10"
        post = ("<!-- post: story:o/r:train-issue-5 channel: x day: 2026-10-08 -->\n"
                "Merged: a breaking change to avm/res/data/factory; it is not yet published. " + url +
                "\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("'release status not known'", r["posts"][0]["must_state_missing"])
        ok = post.replace("it is not yet published", "its release status is not known")
        self.assertTrue(newsletter.check_posts([v], ok)["ok"])

    def test_g3_a_closed_breaking_pr_keeps_its_first_post_and_gets_a_closing_one(self):
        v = self.v()
        self._pr(v, 30, state="closed", closed_at="2026-10-20T10:00:00Z")
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "closed", "state": "closed",
                                    "worthy": "x"})
        v["pr_history"] = {"prs": [{"number": 30, "commits": [
            {"sha": "c1", "committed_at": "2026-10-06T10:00:00Z"}], "patches": [
            {"path": "avm/res/hci/cluster/CHANGELOG.md", "commit": "c1",
             "added": ["### Breaking Changes", "- Removed `x`."]}]}]}
        ids = {p["post_id"]: p for p in self.cand(v)["posts"]}
        self.assertEqual(ids["story:o/r:train-pr-30"]["trigger"], "breaking_declared_open")
        self.assertEqual((ids["story:o/r:train-pr-30:closed"]["trigger"],
                          ids["story:o/r:train-pr-30:closed"]["day"]),
                         ("closed_after_post", "2026-10-20"))

    def test_g4_the_closing_post_uses_the_final_closure(self):
        v = self.v()
        for n, at in ((30, "2026-10-12T10:00:00Z"), (31, "2026-10-22T10:00:00Z")):
            self._pr(v, n, state="closed", closed_at=at)
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30, 31],
                            "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-06T09:00:00Z"}}}
        loop = next(p for p in self.cand(v)["posts"] if p["post_id"].endswith(":closed"))
        self.assertEqual((loop["day"], loop["must_state"]["item"]["number"]), ("2026-10-22", 31))

    def test_g5_folded_closures_stay_in_their_repository(self):
        a = self._closed_issue(self.v(), created="2026-10-14T01:00:00Z")
        b = self.v()
        b["meta"] = dict(b["meta"], repo="s")
        for x in b["prs"] + b["issues"]:
            x["url"] = x["url"].replace("/o/r/", "/o/s/")
        b["plumbing_themes"] = []
        c = newsletter.social_candidates([a, b])
        for w in c["weekly"]:
            if w["post_id"].startswith("weekly:o/s:"):
                self.assertEqual(w["must_state"]["closures_not_posted"], [])
        self.assertFalse(any(w["post_id"] == "weekly:o/s:2026-W42" for w in c["weekly"]))
        self.assertTrue(any(w["post_id"] == "weekly:o/r:2026-W42" for w in c["weekly"]))

    def test_g6_publication_dates_are_per_repository(self):
        # a same-path, same-version release in another member must not move the date
        v = self.v()
        v["stories"]["all"][0]["released"][0]["date"] = "2026-10-20T00:00:00Z"
        v["module_changes"]["modules"][0]["versions"]["published"][0]["date"] = "2026-10-20T00:00:00Z"
        other = json.loads(json.dumps(v))
        other["meta"] = dict(other["meta"], repo="s")
        other["module_changes"]["modules"][0]["versions"]["published"][0]["date"] = \
            "2026-10-02T00:00:00Z"
        post = ("<!-- post: story:o/r:train-pr-11 channel: x day: 2026-10-09 -->\n"
                "Merged: avm/ptn/net/dns 0.7.4 is not yet published. " + R + "/pull/11\n"
                "<!-- /post -->\n")
        cand = newsletter.social_candidates([v])
        r = newsletter.check_posts([v, other], post.replace("story:o/r:", "story:o/r:"),
                                   candidates={"posts": [dict(p, repo="o/r") for p in cand["posts"]],
                                               "weekly": []})
        self.assertEqual(r["posts"][0].get("release_status_mismatch", []), [])

    def test_g7_deprecation_links_its_replacement(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "deprecated", "module": "avm/res/hci/cluster", "date": "2026-10-03",
             "source": {"type": "commit", "url": R + "/commit/d"}}],
            "deprecated_modules": [{"module": "avm/res/hci/cluster", "replacement": {
                "module": "avm/ptn/net/dns", "quote": "use avm/ptn/net/dns instead"}}]}
        post = ("<!-- post: deprecated:o/r:avm/res/hci/cluster:2026-10-03 channel: x "
                "day: 2026-10-03 -->\nThe module avm/res/hci/cluster was deprecated; use "
                "avm/ptn/net/dns instead. " + T + "avm/res/hci/cluster\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("the replacement's link " + T + "avm/ptn/net/dns",
                      r["posts"][0]["must_state_missing"])
        self.assertTrue(newsletter.check_posts([v], post.replace(
            "instead. ", "instead (" + T + "avm/ptn/net/dns). "))["ok"])

    def test_g8_a_proposal_says_not_published(self):
        v = self.v()
        self._pr(v, 30, created_at="2026-10-07T10:00:00Z")
        v["trains"].append({"id": "train-pr-30", "outcome": "in_flight", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-07T09:00:00Z"}}}
        post = ("<!-- post: story:o/r:train-pr-30 channel: x day: 2026-10-07 -->\n"
                "Proposed: avm/res/x/new in an open pull request. " + R + "/pull/30\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("'not published'", r["posts"][0]["must_state_missing"])
        self.assertTrue(newsletter.check_posts([v], post.replace(
            "pull request.", "pull request, not published."))["ok"])

    def test_g9_merged_not_published_states_the_declared_version(self):
        post = ("<!-- post: story:o/r:train-issue-5 channel: x day: 2026-10-08 -->\n"
                "Merged: a breaking change to avm/res/data/factory, not yet published. " + R +
                "/pull/10\n<!-- /post -->\n")
        v = self.v()
        # the fixture's module layer publishes 0.13.0 later in the window
        v["module_changes"]["modules"][1]["versions"]["published"][0]["date"] = "2026-10-20T00:00:00Z"
        r = newsletter.check_posts([v], post)
        self.assertIn("declared version 0.13.0", r["posts"][0]["must_state_missing"])
        r = newsletter.check_posts([v], post.replace("factory,", "factory 0.13.0,"))
        self.assertTrue(r["ok"], r["posts"])

    # siblings
    def test_s1_open_claim_is_read_as_of_the_post_day(self):
        v = self.v()
        self._pr(v, 30, state="closed", created_at="2026-10-07T10:00:00Z",
                 closed_at="2026-10-25T10:00:00Z")
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-07T09:00:00Z"}}}
        post = ("<!-- post: story:o/r:train-pr-30 channel: x day: 2026-10-07 -->\n"
                "Proposed: avm/res/x/new; #30 is still open, not published. " + R + "/pull/30\n"
                "<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertTrue(r["ok"], r["posts"])

    def test_s2_owner_added_names_the_owner(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
             "people": ["alex"], "source": {"type": "commit", "url": R + "/commit/abc"}}]}
        post = ("<!-- post: owner_added:o/r:avm/res/hci/cluster:2026-10-06 channel: x "
                "day: 2026-10-06 -->\navm/res/hci/cluster has a new owner. " + R + "/commit/abc\n"
                "<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertIn("the new owner Alex Sehr", r["posts"][0]["must_state_missing"])

    def test_s3_a_version_published_before_the_merge_is_not_declared(self):
        v = self.v()
        v["stories"]["all"][1]["released"].append(
            {"module": "avm/res/data/factory", "version": "0.12.0", "status": "published",
             "date": "2026-10-01T00:00:00Z"})
        p = next(x for x in self.cand(v)["posts"] if x.get("story") == "train-issue-5")
        self.assertEqual([d["version"] for d in p["must_state"]["declared"]], ["0.13.0"])


class TestSocialRecordAsOf(unittest.TestCase):
    """Third review round on PR #122: every claim of a post is judged against
    ONE projection of the record as of the post's day (`RecordAsOf`)."""

    def v(self):
        return TestSocialPosts().v()

    def test_h1_close_and_reopen_are_replayed(self):
        pr = {"number": 30, "created_at": "2026-10-01T10:00:00Z", "state": "open",
              "merged": False, "lifecycle": [
                  {"event": "closed", "created_at": "2026-10-03T10:00:00Z"},
                  {"event": "reopened", "created_at": "2026-10-08T10:00:00Z"}]}
        self.assertEqual(newsletter.pr_state_on(pr, "2026-10-05"), "closed_unmerged")
        self.assertEqual(newsletter.pr_state_on(pr, "2026-10-09"), "open")
        self.assertEqual(newsletter.pr_state_on(pr, "2026-09-30"), "not_opened")

    def test_h2_a_release_claim_past_the_coverage_stays_pending(self):
        v = self.v()
        v["prs"][0]["merged_at"] = "2026-10-30T10:00:00Z"
        c = newsletter.social_candidates([v])
        p = next(x for x in c["posts"] if x.get("story") == "train-issue-5")
        self.assertEqual(p["day"], "2026-11-02")
        self.assertIn("coverage ends 2026-10-31", p["must_state"]["pending"])
        d = newsletter.daily_outline([v], "2026-11-02", candidates=c)
        self.assertEqual((d["posts"], d["pending"][0]["story"]), ([], "train-issue-5"))
        post = ("<!-- post: story:o/r:train-issue-5 channel: x day: 2026-11-02 -->\n"
                "Merged: avm/res/data/factory 0.13.0, a breaking change, not yet published. " + R +
                "/pull/10\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post, candidates=c)
        self.assertIn("post_pending", r["posts"][0])
        self.assertEqual(newsletter.release_on(v, "avm/res/data/factory", "0.13.0", "2026-11-02"),
                         "unknown")

    def test_h3_a_sentence_starting_with_an_item_is_not_a_heading(self):
        r = newsletter.state_claims([self.v()], "## People\n\n#10 is still open.\n")
        self.assertEqual(r["pr_state_mismatch"][0]["pr"], 10)
        r = newsletter.state_claims([self.v()], "## People\n\n### #10 is still open\n")
        self.assertEqual(r["pr_state_mismatch"], [])

    def test_h4_a_claim_true_only_later_fails_on_the_day(self):
        v = self.v()
        # merged on 6 October: on 5 October it was open
        r = newsletter.state_claims([v], "## P\n\n[#11](" + R + "/pull/11) was merged.\n",
                                    as_of="2026-10-05")
        self.assertEqual((r["pr_state_mismatch"][0]["view"]), "open")
        self.assertEqual(newsletter.state_claims(
            [v], "## P\n\n[#11](" + R + "/pull/11) was merged.\n",
            as_of="2026-10-06")["pr_state_mismatch"], [])
        # published on 7 October: on 6 October it was not
        r = newsletter.state_claims([v], "## P\n\n`avm/ptn/net/dns` 0.7.4 was published.\n",
                                    as_of="2026-10-06")
        self.assertEqual(r["release_status_mismatch"][0]["view"], "declared_not_published")
        # the post check runs every claim against the post's day
        c = newsletter.social_candidates([v])
        post = TestSocialPosts.POST.replace("day: 2026-10-07", "day: 2026-10-07")
        self.assertTrue(newsletter.check_posts([v], post, candidates=c)["ok"])
        early = post.replace("with the Redis zone", "with the Redis zone; #13 was merged")
        v["prs"][3]["merged_at"] = "2026-10-09T10:00:00Z"
        r = newsletter.check_posts([v], early, candidates=c)
        self.assertEqual(r["posts"][0]["pr_state_mismatch"][0]["pr"], 13)

    def test_h5_nameless_owners_are_counted_not_named(self):
        v = self.v()
        v["module_ownership"] = {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/hci/cluster", "date": "2026-10-06",
             "people": ["nik"], "source": {"type": "commit", "url": R + "/commit/abc"}}]}
        v["affiliations"]["people"]["nik"].pop("name", None)
        v["people"].get("nik", {}).pop("name", None)
        p = next(x for x in newsletter.social_candidates([v])["posts"] if x["kind"] == "module_event")
        self.assertEqual((p["must_state"]["people"], p["must_state"]["people_unnamed"]), ([], 1))
        post = ("<!-- post: owner_added:o/r:avm/res/hci/cluster:2026-10-06 channel: x "
                "day: 2026-10-06 -->\navm/res/hci/cluster has a new owner. " + R + "/commit/abc\n"
                "<!-- /post -->\n")
        self.assertTrue(newsletter.check_posts([v], post)["ok"])

    def test_h6_module_paths_are_in_the_fingerprint(self):
        a = TestSocialPosts.POST
        b = a.replace("pattern avm/ptn/net/dns was", "pattern avm/res/data/factory was")
        r = newsletter.check_posts([self.v()], b, against=a)
        self.assertEqual(r["posts"][0]["facts_changed"]["modules"],
                         {"added": ["avm/res/data/factory"], "removed": ["avm/ptn/net/dns"]})

    def test_h7_a_url_followed_by_a_period(self):
        t = TestSocialPosts.POST.replace("0.7.4\n<!-- /post -->", "0.7.4.\n<!-- /post -->", 1)
        r = newsletter.check_posts([self.v()], t)
        self.assertTrue(r["ok"], r["posts"])
        self.assertEqual(newsletter.x_length("https://example.invalid/a."), 24)

    def test_t1_must_state_prs_are_as_of_the_post_day(self):
        v = self.v()
        v["prs"].append({"number": 30, "title": "x", "author": "sam", "url": R + "/pull/30",
                         "state": "closed", "merged": False, "created_at": "2026-10-07T10:00:00Z",
                         "closed_at": "2026-10-25T10:00:00Z"})
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30], "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-07T09:00:00Z"}}}
        p = next(x for x in newsletter.social_candidates([v])["posts"]
                 if x["post_id"] == "story:o/r:train-pr-30")
        self.assertEqual(p["must_state"]["prs"][0]["state"], "open")

    def test_t2_no_closing_post_while_a_pr_was_reopened_that_day(self):
        v = self.v()
        v["prs"].append({"number": 30, "title": "x", "author": "sam", "url": R + "/pull/30",
                         "state": "closed", "merged": False, "created_at": "2026-10-05T10:00:00Z",
                         "closed_at": "2026-10-12T10:00:00Z"})
        v["prs"].append({"number": 31, "title": "x", "author": "sam", "url": R + "/pull/31",
                         "state": "closed", "merged": False, "created_at": "2026-10-05T10:00:00Z",
                         "closed_at": "2026-10-28T10:00:00Z", "lifecycle": [
                             {"event": "closed", "created_at": "2026-10-10T10:00:00Z"},
                             {"event": "reopened", "created_at": "2026-10-11T10:00:00Z"},
                             {"event": "closed", "created_at": "2026-10-28T10:00:00Z"}]})
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30, 31],
                            "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-06T09:00:00Z"}}}
        loop = next(p for p in newsletter.social_candidates([v])["posts"]
                    if p["post_id"].endswith(":closed"))
        self.assertEqual(loop["day"], "2026-10-28")

    def test_h8_the_closing_post_may_link_the_final_closed_item(self):
        v = self.v()
        for n, at in ((30, "2026-10-12T10:00:00Z"), (31, "2026-10-22T10:00:00Z")):
            v["prs"].append({"number": n, "title": "x", "author": "sam", "url": R + "/pull/%d" % n,
                             "state": "closed", "merged": False, "created_at": "2026-10-05T10:00:00Z",
                             "closed_at": at})
        v["trains"].append({"id": "train-pr-30", "outcome": "rejected", "prs": [30, 31],
                            "issues": []})
        v["stories"]["all"].append({"id": "train-pr-30", "group": "in_flight", "state": "in_flight",
                                    "worthy": "x", "new_module_proposed": ["avm/res/x/new"]})
        v["module_ownership"] = {"collected": True, "events": [], "proposed_modules": {
            "avm/res/x/new": {"prs": [30], "committed_at": "2026-10-06T09:00:00Z"}}}
        post = ("<!-- post: story:o/r:train-pr-30:closed channel: x day: 2026-10-22 -->\n"
                "The proposal of avm/res/x/new was closed without merging; no reason is stated. " +
                R + "/pull/31\n<!-- /post -->\n")
        r = newsletter.check_posts([v], post)
        self.assertTrue(r["ok"], r["posts"])


class TestThemeCheck(unittest.TestCase):
    """A theme narrative is checked like a story: quotes, PR and review
    states, release claims, numbers, items, people, not only heading and
    links."""

    THEME = ("### CI workflows and pipeline utilities\n\n"
             "A pipeline action was bumped in [#12](https://github.com/o/r/pull/12) by "
             "Jared Holt (@jared).\n")

    def check(self, text, v=None):
        return newsletter.check_theme([v or view()], text)

    def test_good_theme_passes(self):
        r = self.check(self.THEME)
        self.assertTrue(r["ok"], {k: x for k, x in r.items() if x and k not in ("ok", "theme")})
        self.assertEqual(r["theme"], "ci")

    def test_a_bare_item_reference_states_what_its_description_says(self):
        v = view()
        v["prs"][2]["body"] = "Pins tool 0.4.2 and runs it 37 times."
        r = self.check(self.THEME + "\n#12 pins tool 0.4.2 and runs it 37 times.\n", v)
        self.assertEqual((r["versions_not_in_view"], r["numbers_not_in_view"]), ([], []))
        r = self.check(self.THEME + "\nIt pins tool 0.4.9.\n", v)
        self.assertTrue(r["versions_not_in_view"])

    def test_heading_and_trains(self):
        r = self.check(self.THEME.replace("### CI workflows and pipeline utilities", "### Other"))
        self.assertTrue(r["heading_missing"])
        self.assertFalse(r["ok"])
        v = TestDeliveredContract().v()
        r = newsletter.check_theme([v], "### CI workflows\n\nNothing named here.\n")
        self.assertEqual(r["theme"], "ci_workflows")
        self.assertEqual([x["key"] for x in r["theme_trains_missing"]], ["train-pr-12"])
        r = newsletter.check_theme([v], "### CI workflows\n\nA bump in "
                                   "[#12](https://github.com/o/r/pull/12).\n")
        self.assertEqual(r["theme_trains_missing"], [])

    def test_quotes_numbers_items_and_people(self):
        r = self.check(self.THEME + "\nIts description says \"this never happened at all\".\n")
        self.assertEqual(r["quotes_not_in_view"], ["this never happened at all"])
        r = self.check(self.THEME + "\nIt ran 37 times.\n")
        self.assertEqual(r["numbers_not_in_view"], ["37"])
        r = self.check(self.THEME + "\nSee [#99](https://github.com/o/r/pull/99).\n")
        self.assertIn(99, r["items_not_in_view"])
        r = self.check(self.THEME.replace("Jared Holt", "Jarred Holt"))
        self.assertTrue(r["name_mismatch"])
        self.assertFalse(r["ok"])

    def test_pr_state_review_state_and_release_claims(self):
        r = self.check(self.THEME + "\n[#12](https://github.com/o/r/pull/12) is still open.\n")
        self.assertEqual(r["pr_state_mismatch"][0]["pr"], 12)
        v = view()
        v["prs"][2]["review_state"] = {"state": "approved"}
        r = self.check(self.THEME + "\n[#12](https://github.com/o/r/pull/12) is awaiting a first "
                       "review.\n", v)
        self.assertEqual(r["review_state_mismatch"][0]["view"], "approved")
        r = self.check(self.THEME + "\n[`avm/res/hci/cluster`](" + T + "avm/res/hci/cluster) "
                       "0.3.0 was published.\n")
        self.assertEqual(r["release_status_mismatch"][0]["view"], "declared_not_published")
        self.assertFalse(r["ok"])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            vp = os.path.join(d, "v.json")
            json.dump(view(), open(vp, "w"))
            tp = os.path.join(d, "t.md")
            open(tp, "w").write(self.THEME)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    newsletter.main([vp, "--check-theme", tp, "--theme", "ci"])
            self.assertEqual(cm.exception.code, 0, buf.getvalue())


class TestCli(unittest.TestCase):

    def test_outline_and_check_commands(self):
        with tempfile.TemporaryDirectory() as d:
            vp = os.path.join(d, "bundle.json")
            with open(vp, "w") as fh:
                json.dump(view(), fh)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                newsletter.main([vp, "--outline"])
            self.assertEqual(json.loads(buf.getvalue())["kind"], "newsletter_outline")
            ip = os.path.join(d, "issue.md")
            sdir = os.path.join(d, "slices")
            os.mkdir(sdir)
            with open(os.path.join(sdir, "train-issue-5.slice.json"), "w") as fh:
                json.dump(SLICE, fh)
            with open(ip, "w") as fh:
                fh.write(GOOD)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    newsletter.main([vp, "--check-newsletter", ip, "--slices", sdir])
            self.assertEqual(cm.exception.code, 0, buf.getvalue())
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    newsletter.main([vp])
            self.assertEqual(cm.exception.code, 2)

    def test_fixture_views_are_untouched_by_the_outline(self):
        path = os.path.join(HERE, "fixtures", "bundle_sample.json")
        with open(path, "rb") as fh:
            before = fh.read()
        v = link.enrich(json.loads(before))
        out = newsletter.outline([v])
        self.assertEqual(out["kind"], "newsletter_outline")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
