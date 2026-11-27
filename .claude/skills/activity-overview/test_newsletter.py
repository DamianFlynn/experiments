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
from test_story import bundle, enriched, pr, review  # noqa: E402

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

    def test_weekly_caps_the_lead_modules(self):
        v = view()
        for i in range(10):
            v["module_changes"]["modules"].append(module("avm/res/x/m%d" % i, 1))
        month = section(newsletter.outline([v]), "module_changes")
        week = section(newsletter.outline([v], cadence="week"), "module_changes")
        self.assertEqual(len(month["modules"]), newsletter.CADENCE["month"]["modules"])
        self.assertEqual(len(week["modules"]), newsletter.CADENCE["week"]["modules"])
        self.assertEqual(len(week["modules"]) + len(week["also"]), 13)

    def test_stories_full_and_pointers(self):
        st = section(newsletter.outline([view()]), "stories")
        self.assertEqual([s["id"] for s in st["full"]], ["train-issue-5", "train-pr-11"])
        self.assertEqual(st["full"][0]["title"], "Git config lost")
        self.assertIn("--slice train-issue-5", st["full"][0]["slice"])
        week = section(newsletter.outline([view()], cadence="week"), "stories")
        self.assertEqual([s["id"] for s in week["full"]], ["train-issue-5"])
        self.assertEqual([s["id"] for s in week["pointers"]], ["train-pr-11"])

    def test_story_choice_mixes_shipped_and_in_flight(self):
        v = view()
        v["trains"].append({"id": "train-pr-20", "outcome": "in_flight", "issues": [],
                            "prs": [10], "story": True, "story_rank": 4})
        v["trains"].append({"id": "train-pr-21", "outcome": "shipped", "issues": [],
                            "prs": [10], "story": True, "story_rank": 3})
        v["stories"]["selected"] += [{"id": "train-pr-21", "rank": 3, "score": 10},
                                     {"id": "train-pr-20", "rank": 4, "score": 9}]
        st = section(newsletter.outline([v]), "stories")
        self.assertEqual([s["id"] for s in st["full"]], ["train-issue-5", "train-pr-20"])
        self.assertEqual([s["id"] for s in st["pointers"]], ["train-pr-11", "train-pr-21"])

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
        self.assertEqual(r["title_signal"], "new_module")
        self.assertTrue(c["ready"][0]["approved"])

    def test_omitted_when_nothing_open(self):
        b = enriched(bundle([pr(1, state="closed", merged=True,
                                merged_at="2026-05-05T10:00:00Z")]))
        c = section(newsletter.outline([b]), "coming")
        self.assertIn("no open pull request", c["omit"])


# --- the check --------------------------------------------------------------

GOOD = """# o/r — October 2026

The data factory module changed how Git is configured, and one new module landed.

## Breaking changes and deprecations

- `avm/res/data/factory` 0.13.0: "The parameters `gitRepoType` and `gitAccountName` were removed." ([#10](https://github.com/o/r/pull/10))
- `avm/res/hci/cluster`: a parameter was removed from the module file; check before upgrading.

## New modules

- `avm/res/new/thing` 0.1.0, delivered by Nik Peters (@nik): "Initial version: deploys a thing." ([#13](https://github.com/o/r/pull/13))

## What changed in existing modules

- `avm/ptn/net/dns` 0.7.4: "Added Redis zone `privatelink.redis.azure.net`" ([#11](https://github.com/o/r/pull/11))

## Stories

<!-- story: train-issue-5 -->
Something happened.

**What landed.** It landed.
<!-- /story -->

## People

From the community, Tomaz M (@tomaz) landed the Git change on 5 October 2026.
Nik Peters (@nik), whose affiliation is not known, landed a new module.

<!-- omitted: coming — nothing open in the window -->

## Plumbing

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
        t = GOOD.replace("`avm/ptn/net/dns` 0.7.4", "`avm/ptn/net/dnz` 0.7.9") \
            .replace("4 pull requests merged.", "47 pull requests merged.") \
            .replace("(@tomaz)", "(@ghost-user)")
        r = self.check(t)
        self.assertFalse(r["ok"])
        self.assertIn("avm/ptn/net/dnz", r["modules_not_in_view"])
        self.assertIn("47", r["numbers_not_in_view"])
        self.assertIn("ghost-user", r["people_not_in_view"])

    def test_version_bound_to_its_module(self):
        t = GOOD.replace("`avm/ptn/net/dns` 0.7.4", "`avm/ptn/net/dns` 0.13.0")
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
