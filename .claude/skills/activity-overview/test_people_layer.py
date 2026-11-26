"""The people layer: affiliation, first-ever / returning contributors, what
each person contributed, and recognition by affiliation (issue #97).

* gather records each window person's affiliation (`maintainer_org`,
  `community`, `unknown`) with the deciding signal and its strength as the
  per-window fact `affiliations`, through an injectable API seam;
* the newcomer check tells first-ever, returning (after more than 3 / 6 / 9
  months) and regular contributors apart, and a view says what the store
  holds of them before the window (`prior_activity`);
* link derives `contributors` (affiliation shares, community counts, the
  newcomers and the hall with what their work was about) and the halls by
  affiliation; digest merges the labels across members.

No network: the GitHub API is stubbed (seams, fake openers, acquire's
`_paginated`). Privacy: no profile text, company or email is ever stored.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import manifest  # noqa: E402
import validate  # noqa: E402
import test_module_changes as tmc  # noqa: E402
from test_github_api import FakeOpener, FakeSleep, _Resp, _http_error  # noqa: E402
from test_late_month_gather import (FROM, TO, LateMonthRepo,  # noqa: E402
                                    cross_check, fold_into)

COMPANY_SECRET = "Fabrikam Widgets Unit 7"
EMAIL_SECRET = "dev.person@bigco.example"


def cfg(**kw):
    return gather.affiliation_config("o", **kw)


class TestCompanyAndEmail(unittest.TestCase):

    def test_normalisation(self):
        for text in ("@microsoft", "Microsoft", "Microsoft Corporation",
                     "Microsoft Corp.", "  MICROSOFT  inc ", "microsoft, inc."):
            self.assertEqual(gather.normalize_company(text), "microsoft", text)
        self.assertEqual(gather.normalize_company("Microsoft MVP"), "microsoft mvp")
        self.assertEqual(gather.normalize_company(""), "")
        self.assertEqual(gather.normalize_company(None), "")

    def test_company_matches(self):
        home = ["azure", "microsoft"]
        self.assertTrue(gather.company_matches("Microsoft Corporation", home))
        self.assertTrue(gather.company_matches("@Azure @contoso", home))
        self.assertFalse(gather.company_matches("Contoso Ltd", home))
        # exact after normalisation: a qualified name is not a match
        self.assertFalse(gather.company_matches("Microsoft MVP", home))
        self.assertIsNone(gather.company_matches("", home))
        self.assertIsNone(gather.company_matches("   ", home))

    def test_email_domain(self):
        d = ["bigco.example"]
        self.assertTrue(gather.email_domain_is_home("a@bigco.example", d))
        self.assertTrue(gather.email_domain_is_home("a@eu.bigco.example", d))
        self.assertTrue(gather.email_domain_is_home("a@BIGCO.example", d))
        self.assertFalse(gather.email_domain_is_home("a@notbigco.example", d))
        self.assertFalse(gather.email_domain_is_home(
            "1+a@users.noreply.github.com", ["users.noreply.github.com"]))
        self.assertFalse(gather.email_domain_is_home("", d))
        self.assertFalse(gather.email_domain_is_home("a@bigco.example", []))

    def test_config(self):
        c = gather.affiliation_config(
            "Azure", home_orgs=["microsoft", "azure"], home_names=["Microsoft Corp"],
            email_domains=[" Microsoft.com "],
            overrides={"x": "community", "@Azure/core": "maintainer_org"})
        self.assertEqual(c["home_orgs"], ["Azure", "microsoft"])
        self.assertEqual(c["home_names"], ["azure", "microsoft"])
        self.assertEqual(c["home_email_domains"], ["microsoft.com"])
        self.assertEqual(c["overrides"], {"x": "community"})
        self.assertEqual(c["groups"], {"@Azure/core": "maintainer_org"})

    def test_override_parsing(self):
        self.assertEqual(gather.parse_affiliation_override("al-ice=community"),
                         ("al-ice", "community"))
        self.assertEqual(gather.parse_affiliation_override("@o/core=maintainer_org"),
                         ("@o/core", "maintainer_org"))
        for bad in ("alice", "alice=friend", "=community", "@o=community",
                    "a/b=community"):
            with self.assertRaises(ValueError, msg=bad):
                gather.parse_affiliation_override(bad)

    def test_cli_and_manifest_settings_combine(self):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", FROM, "--to", TO,
            "--store", "s.db", "--home-org", "bigco", "--home-name", "Big Co",
            "--home-email-domain", "bigco.example",
            "--affiliation", "x=maintainer_org"])
        args.affiliation_manifest = {"home_orgs": ["other"], "overrides": {
            "x": "community", "y": "community"}}
        c = gather.args_affiliation_config(args, "o")
        self.assertEqual(c["home_orgs"], ["o", "other", "bigco"])
        self.assertIn("big co", c["home_names"])
        self.assertEqual(c["overrides"], {"x": "maintainer_org", "y": "community"})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gather.parse_args(["--owner", "o", "--repo", "r", "--store", "s.db",
                               "--affiliation", "x=friend"])


class TestDecideAffiliation(unittest.TestCase):

    def lookup(self, members=(), companies=None, fail=()):
        calls = []

        def fn(kind, login, org=None):
            calls.append((kind, login, org))
            if login in fail:
                return None
            if kind == "public_member":
                return (org, login) in members
            return (companies or {}).get(login, "")
        fn.calls = calls
        return fn

    def test_each_signal(self):
        c = cfg(home_orgs=["bigco"], home_names=["Big Co"],
                email_domains=["bigco.example"], overrides={"ovr": "community"})
        lk = self.lookup(members={("bigco", "pm")},
                         companies={"co": "Big Co Inc.", "other": COMPANY_SECRET})
        got = gather.find_affiliations(
            ["ovr", "mem", "col", "pm", "co", "other", "mail", "nobody"], c,
            associations={"ovr": "MEMBER", "mem": "MEMBER", "col": "COLLABORATOR"},
            email_home={"mail"}, lookup=lk)
        p = got["people"]
        self.assertEqual(p["ovr"], {"affiliation": "community", "signal": "override",
                                    "strength": "operator"})
        self.assertEqual(p["mem"], {"affiliation": "maintainer_org",
                                    "signal": "author_association",
                                    "strength": "strong"})
        self.assertEqual(p["col"]["strength"], "medium")
        self.assertEqual(p["pm"], {"affiliation": "maintainer_org",
                                   "signal": "public_membership", "strength": "strong"})
        self.assertEqual(p["co"], {"affiliation": "maintainer_org",
                                   "signal": "profile_company", "strength": "medium"})
        self.assertEqual(p["other"], {"affiliation": "community",
                                      "signal": "profile_company", "strength": "weak"})
        self.assertEqual(p["mail"], {"affiliation": "maintainer_org",
                                     "signal": "commit_email_domain",
                                     "strength": "medium", "home_email_domain": True})
        self.assertEqual(p["nobody"], {"affiliation": "unknown", "signal": "none"})
        self.assertEqual(got["unknown"], ["nobody"])
        self.assertEqual(got["not_checked"], [])
        # the free signals cost no call
        self.assertNotIn("ovr", {c[1] for c in lk.calls})
        self.assertNotIn("mem", {c[1] for c in lk.calls})
        # nothing of the profile is kept
        self.assertNotIn(COMPANY_SECRET, json.dumps(got))

    def test_association_never_decides_community(self):
        for assoc in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", None):
            got = gather.find_affiliations(["x"], cfg(), {"x": assoc},
                                           lookup=self.lookup())
            self.assertEqual(got["people"]["x"]["affiliation"], "unknown", assoc)

    def test_a_failed_lookup_never_decides_community(self):
        def lk(kind, login, org=None):
            if kind == "public_member":
                return None                  # cannot tell
            return COMPANY_SECRET            # a non-matching company
        got = gather.find_affiliations(["x"], cfg(), lookup=lk)
        self.assertEqual(got["people"]["x"], {"affiliation": "unknown",
                                              "signal": "lookup_failed"})

        def lk2(kind, login, org=None):
            return None if kind == "public_member" else "O"
        got = gather.find_affiliations(["x"], cfg(), lookup=lk2)
        self.assertEqual(got["people"]["x"]["affiliation"], "maintainer_org")

    def test_cap_and_stop(self):
        lk = self.lookup()
        got = gather.find_affiliations(["a", "b", "c"], cfg(),
                                       {"c": "OWNER"}, lookup=lk, max_lookups=1)
        self.assertEqual(sorted(got["people"]), ["a", "c"])
        self.assertEqual(got["not_checked"], ["b"])
        self.assertEqual(got["unknown"], ["a", "b"])
        lk = self.lookup(fail={"a", "b", "c", "d"})
        got = gather.find_affiliations(["a", "b", "c", "d"], cfg(), lookup=lk,
                                       stop_after=2)
        self.assertEqual(got["not_checked"], ["c", "d"])
        self.assertEqual(got["people"]["a"]["signal"], "lookup_failed")

    def test_people_of_the_window(self):
        prs = [{"author": "alice", "author_association": "CONTRIBUTOR",
                "merged_by": "maint", "reviewers": ["rev", "dependabot[bot]"],
                "comments_list": [{"author": "bob", "author_association": "MEMBER"}],
                "review_comments": [{"author": "ghost"}]},
               {"author": "alice", "author_association": "MEMBER"}]
        issues = [{"author": "carol", "comments_list": []}]
        commits = [{"author": "dev", "login_resolved": True},
                   {"author": "Some Name", "login_resolved": False}]
        order, assoc = gather.affiliation_people(prs, issues, commits)
        self.assertEqual(order, ["alice", "bob", "carol", "dev", "maint", "rev"])
        self.assertEqual(assoc, {"alice": "MEMBER", "bob": "MEMBER"})


class TestLookupSeam(unittest.TestCase):

    def test_answers_are_cached_failures_are_not(self):
        urls = []

        def get_page(url):
            urls.append(url)
            if url.endswith("/users/flaky"):
                raise SystemExit("boom")
            if "/public_members/" in url:
                return {"public_member": url.endswith("/pm")}, None
            return {"login": "x", "company": COMPANY_SECRET}, None
        cache = {}
        lk = gather.make_affiliation_lookup(get_page, cache)
        self.assertTrue(lk("public_member", "pm", "o"))
        self.assertFalse(lk("public_member", "pm2", "o"))
        self.assertEqual(lk("company", "x"), COMPANY_SECRET)
        self.assertIsNone(lk("company", "flaky"))
        lk("company", "x")
        lk("public_member", "pm", "o")
        self.assertEqual(len(urls), 4)
        self.assertNotIn("https://api.github.com/users/flaky", cache)
        lk2 = gather.make_affiliation_lookup(lambda u: ([], None))
        self.assertIsNone(lk2("company", "x"))
        self.assertIsNone(lk2("public_member", "x", "o"))

    def test_groups(self):
        def get_page(url):
            if "/teams/core/" in url:
                return [{"login": "a"}, {"login": "b"}], None
            raise SystemExit("404")
        got, unresolved = gather.resolve_affiliation_groups(
            {"@o/core": "maintainer_org", "@o/secret": "community"}, get_page)
        self.assertEqual(got, {"a": "maintainer_org", "b": "maintainer_org"})
        self.assertEqual(unresolved, ["@o/secret"])

    def test_public_member_probe(self):
        url = "https://api.github.com/orgs/o/public_members/x"
        op = FakeOpener([_Resp(b"", status=204)])
        self.assertEqual(gather.public_member_probe(url, "t", opener=op,
                                                    sleep=FakeSleep()),
                         ({"public_member": True}, None))
        op = FakeOpener([_http_error(url, 404, "Not Found")])
        self.assertEqual(gather.public_member_probe(url, "t", opener=op,
                                                    sleep=FakeSleep()),
                         ({"public_member": False}, None))
        op = FakeOpener([_http_error(url, 401, "Bad credentials")])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gather.public_member_probe(url, "t", opener=op, sleep=FakeSleep())

    def test_production_client_routes_the_probe(self):
        with mock.patch.object(gather, "public_member_probe",
                               lambda url, token: ("probe", None)), \
                mock.patch.object(gather, "http_get_json",
                                  lambda url, token: ("json", None)):
            gp = gather._paginated("t")
            self.assertEqual(gp("https://api.github.com/orgs/o/public_members/x")[0],
                             "probe")
            self.assertEqual(gp("https://api.github.com/users/x")[0], "json")

    def test_commit_email_is_kept_only_as_a_boolean(self):
        commits = [{"sha": "a", "author": "Dev", "author_email": EMAIL_SECRET},
                   {"sha": "b", "author": "Other", "author_email": "o@else.example"},
                   {"sha": "c", "author": "Ann",
                    "author_email": "12+ann@users.noreply.github.com"}]
        hits = set()
        gather.resolve_commit_authors(commits, {"a": "dev", "b": "other"},
                                      home_domains=["bigco.example"],
                                      home_email=hits)
        self.assertEqual(hits, {"dev"})
        self.assertNotIn(EMAIL_SECRET, json.dumps(commits))
        self.assertNotIn("bigco.example", json.dumps(commits))


class TestNewcomerClasses(unittest.TestCase):

    def cand(self, login, created="2026-10-10T00:00:00Z", first="pr"):
        return {"login": login, "first": first, "number": 1, "url": "u",
                "created_at": created}

    def test_first_ever_returning_regular(self):
        answers = {
            ("new", "pr"): {"count": 0, "latest": None},
            ("new", "issue"): {"count": 0, "latest": None},
            ("reg", "pr"): {"count": 4, "latest": "2026-09-20T00:00:00Z"},
            # PR 400 days ago, but an issue 200 days ago: the gap is 200 days
            ("back", "pr"): {"count": 1, "latest": "2025-09-05T00:00:00Z"},
            ("back", "issue"): {"count": 2, "latest": "2026-03-24T00:00:00Z"},
            ("long", "pr"): {"count": 1, "latest": "2025-10-01T00:00:00Z"},
            ("long", "issue"): {"count": 0, "latest": None},
            ("q3", "pr"): {"count": 1, "latest": "2026-07-01T00:00:00Z"},
            ("q3", "issue"): {"count": 0, "latest": None},
        }
        calls = []

        def search(login, kind):
            calls.append((login, kind))
            if (login, kind) == ("half", "issue"):
                raise SystemExit("rate limit")
            if (login, kind) == ("half", "pr"):
                return {"count": 1, "latest": "2025-01-01T00:00:00Z"}
            return answers[(login, kind)]
        got = gather.find_newcomers(
            [self.cand(x) for x in ("new", "reg", "back", "long", "q3", "half")],
            search, "2026-10-01")
        self.assertEqual([e["login"] for e in got["new"]], ["new"])
        ret = {e["login"]: e for e in got["returning"]}
        self.assertEqual((ret["back"]["gap_days"], ret["back"]["gap_bucket"],
                          ret["back"]["last_seen_before"]),
                         (200, "over_6_months", "2026-03-24T00:00:00Z"))
        self.assertEqual(ret["long"]["gap_bucket"], "over_9_months")
        self.assertEqual((ret["q3"]["gap_days"], ret["q3"]["gap_bucket"]),
                         (101, "over_3_months"))
        self.assertTrue(ret["half"]["gap_upper_bound"])
        self.assertEqual(got["regular"], [{"login": "reg", "gap_days": 20,
                                           "last_seen_before": "2026-09-20T00:00:00Z"}])
        self.assertEqual(got["gap_thresholds_days"], [90, 180, 270])
        self.assertEqual(got["unknown"], [])
        # a regular contributor costs one call: no second kind is asked
        self.assertEqual([c for c in calls if c[0] == "reg"], [("reg", "pr")])

    def test_buckets(self):
        self.assertIsNone(gather.gap_bucket(90))
        self.assertEqual(gather.gap_bucket(91), "over_3_months")
        self.assertEqual(gather.gap_bucket(181), "over_6_months")
        self.assertEqual(gather.gap_bucket(271), "over_9_months")
        self.assertIsNone(gather.gap_bucket(None))

    def test_search_url_sorts_newest_first(self):
        url = gather.newcomer_search_url("o", "r", "x", "2026-10-01", "pr")
        self.assertIn("sort=created&order=desc&per_page=1", url)
        self.assertEqual(gather.search_latest_created(
            {"items": [{"created_at": "2026-01-01T00:00:00Z"}]}),
            "2026-01-01T00:00:00Z")
        self.assertIsNone(gather.search_latest_created({"items": []}))

    def test_merge_across_windows(self):
        def month(frm, to, new=(), ret=(), reg=()):
            return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
                    "prs": [], "issues": [], "commits": [],
                    "newcomers": {
                        "collected": True, "before": frm, "candidates": 3,
                        "new": [{"login": x, "first": "pr", "number": 1, "url": "u",
                                 "created_at": frm + "T01:00:00Z"} for x in new],
                        "returning": [{"login": x, "first": "pr", "number": 2,
                                       "url": "u", "created_at": frm + "T01:00:00Z",
                                       "last_seen_before": "2025-01-01T00:00:00Z",
                                       "gap_days": 500, "gap_bucket": "over_9_months"}
                                      for x in ret],
                        "regular": [{"login": x, "last_seen_before": "x",
                                     "gap_days": 3} for x in reg],
                        "gap_thresholds_days": [90, 180, 270],
                        "unknown": [], "not_checked": []}}
        tmp = tempfile.mkdtemp()
        store = fold_into(os.path.join(tmp, "q.db"),
                          month("2026-08-01", "2026-08-31", new=["ann"], ret=["bo"]),
                          month("2026-09-01", "2026-09-30", ret=["ann"],
                                reg=["bo", "cy"]))
        conn = graphstore.open_store_readonly(store)
        try:
            v = extract.extract(conn, "o", "r", "2026-08-01", "2026-09-30",
                                warn=lambda _m: None, as_of_end=True)
        finally:
            conn.close()
        nc = v["newcomers"]
        self.assertEqual([e["login"] for e in nc["new"]], ["ann"])
        self.assertEqual([e["login"] for e in nc["returning"]], ["bo"])
        self.assertEqual([e["login"] for e in nc["regular"]], ["cy"])


def _with_people(b):
    b.setdefault("people", derive.enumerate_participants(
        {**b, "commits": copy.deepcopy(b["commits"])}))
    return b


def _window_with_facts(affiliations=True, newcomers=True):
    b = tmc.window()
    for p in b["prs"]:
        p["reviewers"] = ["rev"] if p["number"] in (100, 7454) else []
    if newcomers:
        b["newcomers"] = {
            "collected": True, "before": "2026-10-01", "candidates": 3,
            "new": [{"login": "dev100", "first": "pr", "number": 100,
                     "url": "https://github.com/o/r/pull/100",
                     "created_at": "2026-10-01T00:00:00Z"}],
            "returning": [{"login": "dev103", "first": "pr", "number": 103,
                           "url": "https://github.com/o/r/pull/103",
                           "created_at": "2026-10-01T00:00:00Z",
                           "last_seen_before": "2026-03-01T00:00:00Z",
                           "gap_days": 214, "gap_bucket": "over_6_months"}],
            "regular": [], "gap_thresholds_days": [90, 180, 270],
            "unknown": [], "not_checked": []}
        b["prior_activity"] = {"before": "2026-10-01", "store_from": "2026-08-01",
                               "people": {"dev100": {"prs": 0, "issues": 0,
                                                     "reviews": 3, "comments": 1,
                                                     "earliest": "2026-08-12"},
                                          "dev103": {"prs": 0, "issues": 0,
                                                     "reviews": 0, "comments": 0,
                                                     "earliest": None}}}
    if affiliations:
        b["affiliations"] = {
            "collected": True, "home_orgs": ["o"], "home_names": ["o"],
            "home_email_domains": [], "people": {
                "dev100": {"affiliation": "community", "signal": "profile_company",
                           "strength": "weak"},
                "dev7454": {"affiliation": "maintainer_org",
                            "signal": "author_association", "strength": "strong"},
                "rev": {"affiliation": "maintainer_org", "signal": "public_membership",
                        "strength": "strong"},
                "dev103": {"affiliation": "unknown", "signal": "none"}},
            "unknown": ["dev103", "dev104"], "not_checked": ["dev104"]}
    return _with_people(b)


class TestContributors(unittest.TestCase):

    def test_new_module_contribution_and_affiliation(self):
        b = link.enrich(_window_with_facts())
        people = b["people"]
        self.assertEqual((people["dev100"]["affiliation"],
                          people["dev100"]["affiliation_signal"]),
                         ("community", "profile_company"))
        self.assertEqual(people["dev104"]["affiliation_signal"], "not_checked")
        self.assertEqual(people["dev105"]["affiliation_signal"], "not_looked_up")
        c = b["contributors"]
        first = {r["login"]: r for r in c["newcomers"]}
        d = first["dev100"]
        self.assertEqual(d["class"], "first_ever")
        self.assertEqual(d["seen_in_store_before"]["reviews"], 3)
        self.assertEqual(d["affiliation"], "community")
        pr100 = d["contributed"]["prs"][0]
        self.assertEqual(pr100["headline"], "new_module")
        self.assertEqual(pr100["modules"][0], {
            "area": tmc.NEW, "kind": "resource", "status": "new",
            "role": "introduced", "first_version": "0.1.0"})
        self.assertEqual(pr100["changelog"], [{"module": tmc.NEW, "version": "0.1.0",
                                               "items": ["Initial version"]}])
        self.assertEqual(pr100["url"], "https://github.com/o/r/pull/100")
        r = first["dev103"]
        self.assertEqual((r["class"], r["gap_days"], r["gap_bucket"]),
                         ("returning", 214, "over_6_months"))
        self.assertEqual(r["contributed"]["prs"][0]["headline"], "plumbing")
        self.assertEqual(r["contributed"]["prs"][0]["class"], "ci")
        # affiliation shares: unknown is its own bucket
        a = c["affiliation"]
        self.assertEqual(a["merged_prs"]["community"]["count"], 1)
        self.assertEqual(a["merged_prs"]["maintainer_org"]["count"], 1)
        self.assertEqual(a["merged_prs"]["total"],
                         sum(a["merged_prs"][k]["count"]
                             for k in ("maintainer_org", "community", "unknown")))
        self.assertEqual(a["reviews"]["maintainer_org"]["count"], 2)
        com = c["community"]
        self.assertEqual((com["prs_merged"], com["first_timers"], com["modules"]),
                         (1, ["dev100"], [tmc.NEW]))
        self.assertEqual(com["returning"], [])     # dev103 is unknown, not community
        self.assertEqual(c["inputs"], {"module_layer": "module_changes"})
        # the hall: what each top person landed and reviewed
        hall = {h["login"]: h for h in c["hall"]}
        self.assertEqual(hall["rev"]["reviewed"]["prs"], 2)
        self.assertIn(tmc.NEW, hall["rev"]["reviewed"]["modules"])

    def test_halls_by_affiliation(self):
        b = link.enrich(_window_with_facts())
        halls = b["halls"]
        plain = link.enrich(_window_with_facts(affiliations=False))
        self.assertEqual(halls["fame"], plain["halls"]["fame"])
        self.assertEqual([h["login"] for h in halls["community"]], ["dev100"])
        self.assertIn("rev", [h["login"] for h in halls["maintainer_org"]])
        everyone = {h["login"] for h in halls["community"] + halls["maintainer_org"]}
        self.assertNotIn("dev103", everyone)        # unknown: never ranked
        self.assertGreaterEqual(halls["unranked_unknown"], 1)
        self.assertNotIn("community", plain["halls"])

    def test_without_module_notes_uses_the_code_events(self):
        b = _window_with_facts()
        del b["module_notes"]
        b = link.enrich(b)
        self.assertNotIn("module_changes", b)
        c = b["contributors"]
        self.assertEqual(c["inputs"], {"module_layer": "code_events_only"})
        d = next(r for r in c["newcomers"] if r["login"] == "dev100")
        pr100 = d["contributed"]["prs"][0]
        self.assertEqual(pr100["headline"], "new_module")
        self.assertNotIn("changelog", pr100)

    def test_absent_without_the_facts(self):
        b = link.enrich(_window_with_facts(affiliations=False, newcomers=False))
        self.assertNotIn("contributors", b)
        self.assertNotIn("affiliation", b["people"]["dev100"])
        only_nc = link.enrich(_window_with_facts(affiliations=False))
        self.assertNotIn("affiliation", only_nc["contributors"])
        self.assertIn("newcomers", only_nc["contributors"])

    def test_digest_takes_the_most_decisive_signal(self):
        a = {"x": {"affiliation": "unknown", "affiliation_signal": "none",
                   "prs_merged": 1}}
        b = {"x": {"affiliation": "community", "affiliation_signal": "profile_company",
                   "prs_merged": 2}}
        c = {"x": {"affiliation": "maintainer_org",
                   "affiliation_signal": "author_association", "prs_merged": 0}}
        members = [{"repo": r, "bundle": {"people": p, "prs": []}}
                   for r, p in (("a", a), ("b", b), ("c", c))]
        got = digest._merge_people(members)["x"]
        self.assertEqual((got["affiliation"], got["affiliation_signal"]),
                         ("maintainer_org", "author_association"))
        got = digest._merge_people(members[:2])["x"]
        self.assertEqual(got["affiliation"], "community")


class TestPriorActivity(unittest.TestCase):

    def test_store_activity_before_the_window(self):
        aug = {"meta": {"owner": "o", "repo": "r", "from": "2026-08-01",
                        "to": "2026-08-31"},
               "prs": [{"number": 5, "author": "maint", "title": "t",
                        "url": "https://github.com/o/r/pull/5", "state": "closed",
                        "merged": True, "merged_at": "2026-08-10T00:00:00Z",
                        "created_at": "2026-08-05T00:00:00Z",
                        "updated_at": "2026-08-10T00:00:00Z",
                        "reviewers": ["newbie"],
                        "reviews": [{"id": 9, "author": "newbie", "state": "approved",
                                     "submitted_at": "2026-08-07T00:00:00Z",
                                     "url": "https://github.com/o/r/pull/5#r9"}],
                        "comments_list": [{"id": 1, "author": "newbie", "body": "x",
                                           "created_at": "2026-08-06T00:00:00Z",
                                           "url": "https://github.com/o/r/pull/5#c1"}],
                        "closes": [], "crossref_issues": []}],
               "issues": [], "commits": []}
        sep = {"meta": {"owner": "o", "repo": "r", "from": "2026-09-01",
                        "to": "2026-09-30"},
               "prs": [], "issues": [], "commits": [],
               "newcomers": {"collected": True, "before": "2026-09-01",
                             "candidates": 1, "new": [
                                 {"login": "newbie", "first": "pr", "number": 7,
                                  "url": "u", "created_at": "2026-09-02T00:00:00Z"}],
                             "unknown": [], "not_checked": []}}
        tmp = tempfile.mkdtemp()
        store = fold_into(os.path.join(tmp, "p.db"), aug, sep)
        conn = graphstore.open_store_readonly(store)
        try:
            v = extract.extract(conn, "o", "r", "2026-09-01", "2026-09-30",
                                warn=lambda _m: None, as_of_end=True)
            v_aug = extract.extract(conn, "o", "r", "2026-08-01", "2026-08-31",
                                    warn=lambda _m: None, as_of_end=True)
        finally:
            conn.close()
        self.assertEqual(v["prior_activity"], {
            "before": "2026-09-01", "store_from": "2026-08-01",
            "people": {"newbie": {"prs": 0, "issues": 0, "reviews": 1,
                                  "comments": 1, "earliest": "2026-08-06"}}})
        self.assertNotIn("prior_activity", v_aug)


class TestManifestSettings(unittest.TestCase):

    def write(self, extra):
        raw = {"project": "p", "window": {"from": FROM, "to": TO},
               "repos": [{"owner": "o", "repo": "r"}]}
        raw.update(extra)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(raw, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_valid_and_absent(self):
        self.assertNotIn("affiliation", manifest.load_manifest(self.write({})))
        got = manifest.load_manifest(self.write({"affiliation": {
            "home_orgs": ["microsoft"], "home_names": ["Microsoft"],
            "home_email_domains": ["microsoft.com"],
            "overrides": {"a": "community", "@Azure/core": "maintainer_org"}}}))
        self.assertEqual(got["affiliation"]["overrides"]["@Azure/core"],
                         "maintainer_org")

    def test_invalid(self):
        for bad in ({"home_orgs": "microsoft"}, {"overrides": {"a": "friend"}},
                    {"overrides": {"@bad": "community"}}, {"company": "x"},
                    {"home_email_domains": ["not a domain"]}, []):
            with self.assertRaises(ValueError, msg=bad):
                manifest.load_manifest(self.write({"affiliation": bad}))


class TestManifestMembersShareSettingsAndCache(unittest.TestCase):

    def test_members(self):
        seen = []

        def fake_acquire(args, env):
            seen.append(args)
            raise RuntimeError("stop here")
        man = {"project": "p", "from": FROM, "to": TO, "ticket_prefixes": [],
               "repos": [{"owner": "o", "repo": "a"}, {"owner": "o", "repo": "b"}],
               "affiliation": {"home_orgs": ["bigco"], "home_names": [],
                               "home_email_domains": [], "overrides": {}}}
        args = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        with mock.patch.object(gather, "acquire", fake_acquire), \
                contextlib.redirect_stderr(io.StringIO()):
            gather._gather_manifest(conn, args, man)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0].affiliation_cache, seen[1].affiliation_cache)
        self.assertEqual(gather.args_affiliation_config(seen[1], "o")["home_orgs"],
                         ["o", "bigco"])


class AffiliationRepo(LateMonthRepo):
    """September of LateMonthRepo, with profiles and org memberships served by
    the stubbed API."""

    EXTRA = ("--home-org", "bigco", "--home-name", "Big Co")

    def setUp(self):
        super().setUp()
        self.closed_prs[0]["author_association"] = "MEMBER"     # alice
        self.profile_urls = []

    def get_page(self, url):
        if "/public_members/" in url:
            self.profile_urls.append(url)
            org, login = url.split("/orgs/")[1].split("/public_members/")
            return {"public_member": (org, login) == ("bigco", "bob")}, None
        if url.startswith(gather.USERS_API):
            self.profile_urls.append(url)
            login = url[len(gather.USERS_API):]
            company = {"carol": "Big Co Inc.", "maint": COMPANY_SECRET}.get(login)
            return {"login": login, "company": company, "bio": "PRIVATE BIO",
                    "email": EMAIL_SECRET}, None
        return super().get_page(url)


class TestAffiliationsInAcquire(AffiliationRepo):

    def test_fact_view_and_privacy(self):
        store = os.path.join(self.tmp, "s.db")
        bundle, err = self.gather_into(store, *self.EXTRA)
        fact = bundle["affiliations"]
        self.assertEqual(fact["home_orgs"], ["o", "bigco"])
        p = fact["people"]
        self.assertEqual(p["alice"]["signal"], "author_association")
        self.assertEqual(p["bob"]["signal"], "public_membership")
        self.assertEqual((p["carol"]["affiliation"], p["carol"]["signal"]),
                         ("maintainer_org", "profile_company"))
        self.assertEqual((p["maint"]["affiliation"], p["maint"]["signal"]),
                         ("community", "profile_company"))
        self.assertIn("affiliation: o/r: 5 person(s)", err)
        # alice was decided for free: no call for her
        self.assertFalse(any(u.endswith("/alice") for u in self.profile_urls))
        # the cross-check: the view carries the stored fact verbatim
        code, text, view = cross_check(store)
        self.assertEqual(code, 0, text)
        self.assertEqual(view["affiliations"], fact)
        # nothing of a profile or an email reaches the store
        raw = sqlite3.connect(store)
        try:
            dump = "\n".join(str(r) for t in ("nodes", "meta", "edges")
                             for r in raw.execute("SELECT * FROM " + t))
        finally:
            raw.close()
        for secret in (COMPANY_SECRET, "Big Co Inc.", "PRIVATE BIO", EMAIL_SECRET):
            self.assertNotIn(secret, dump)

    def test_no_affiliation_makes_no_call(self):
        bundle, _err = self.gather_into(os.path.join(self.tmp, "s.db"),
                                        "--no-affiliation", *self.EXTRA)
        self.assertEqual(bundle["affiliations"], {"collected": False})
        self.assertEqual(self.profile_urls, [])

    def test_validate_refuses_profile_text_in_the_fact(self):
        store = os.path.join(self.tmp, "s.db")
        self.gather_into(store, *self.EXTRA)
        conn = graphstore.open_store(store)
        key = [k for (k,) in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'facts:%'")][0]
        entry = json.loads(graphstore.get_meta(conn, key))
        blob = "fact:o/r:" + entry["facts"]["affiliations"]
        value = json.loads(graphstore.get_meta(conn, blob))
        value["people"]["maint"]["company"] = COMPANY_SECRET
        text = json.dumps(value, sort_keys=True)
        new = hashlib.sha256(text.encode("utf-8")).hexdigest()
        graphstore.set_meta(conn, "fact:o/r:" + new, text)
        entry["facts"]["affiliations"] = new
        graphstore.set_meta(conn, key, json.dumps(entry, sort_keys=True))
        report = validate.validate(conn, project="o", repo="r")
        conn.close()
        meta = [c for c in report.checks if c["name"] == "meta_integrity"][0]
        self.assertFalse(meta["ok"])
        self.assertIn("holds more than the label", json.dumps(meta))


if __name__ == "__main__":
    unittest.main()
