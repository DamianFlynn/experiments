"""The retained people registry (issue #97): one human-editable record per
GitHub login, shared by every gather of a workspace, so a person is looked up
once and refreshed rarely; display names; the maintainer property per
repository; operator edits that always win; seeding from stores without the
API; and the per-window `affiliations` fact that keeps every view
reproducible after the registry changes.

No network: the GitHub API is stubbed (the lookup seam, acquire's
`_paginated`). Privacy: of a profile only the display name is kept; never a
company, bio or email.
"""
import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import digest  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import manifest  # noqa: E402
import people  # noqa: E402
import validate  # noqa: E402
from test_late_month_gather import FROM, TO, cross_check  # noqa: E402
from test_people_layer import (COMPANY_SECRET, EMAIL_SECRET,  # noqa: E402
                               AffiliationRepo, _window_with_facts)

SLUG = "o/r"


def reg_of(*entries):
    return {"format": people.FORMAT, "version": people.VERSION,
            "people": {e["login"]: e for e in entries}}


class TmpDir(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = os.path.join(self.tmp, "people.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, registry_or_text):
        text = registry_or_text if isinstance(registry_or_text, str) \
            else json.dumps(registry_or_text)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()


class TestRegistryFile(TmpDir):

    def test_missing_file_is_an_empty_registry(self):
        self.assertEqual(people.load_registry(self.path), people.empty_registry())

    def test_stable_text_and_atomic_write(self):
        reg = reg_of({"login": "zed", "name": "Zoë Ünal", "source": "lookup"},
                     {"login": "amy", "tags": ["known"], "source": "operator"})
        self.assertTrue(people.write_registry(self.path, reg))
        text = self.read()
        self.assertTrue(text.endswith("}\n"))
        self.assertIn("Zoë Ünal", text)                  # readable, not \\u escapes
        self.assertLess(text.index('"amy"'), text.index('"zed"'))
        self.assertEqual(text, people.dumps(json.loads(text)))
        mtime = os.stat(self.path).st_mtime_ns
        self.assertFalse(people.write_registry(self.path, json.loads(text)))
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)
        # no temporary file is left behind
        self.assertEqual(sorted(os.listdir(self.tmp)), ["people.json"])

    def test_a_malformed_registry_is_an_error_naming_the_entry(self):
        cases = [
            ("{", "not valid JSON"),
            (json.dumps({"people": {}}), "not a people registry"),
            (json.dumps(reg_of({"login": "a", "company": COMPANY_SECRET})),
             "entry 'a': unknown key(s) company"),
            (json.dumps({"format": people.FORMAT, "version": 1,
                         "people": {"a": {"login": "b"}}}), "entry 'a'"),
            (json.dumps(reg_of({"login": "a", "affiliation": "friend"})),
             "entry 'a': `affiliation`"),
            (json.dumps(reg_of({"login": "a", "maintainer_of": {"o/r": "yes"}})),
             "entry 'a': `maintainer_of['o/r']`"),
            (json.dumps(reg_of({"login": "a", "locked": ["company"]})),
             "entry 'a': `locked`"),
            (json.dumps(reg_of({"login": "a", "name": "two\nlines"})),
             "entry 'a': `name`"),
            (json.dumps(reg_of({"login": "Amy"}, {"login": "amy"})),
             "same GitHub login"),
            (json.dumps({"format": people.FORMAT, "version": 9, "people": {}}),
             "version 9"),
        ]
        for text, want in cases:
            self.write(text)
            with self.assertRaises(people.RegistryError, msg=want) as cm:
                people.load_registry(self.path)
            self.assertIn(want, str(cm.exception))
            self.assertIn(self.path, str(cm.exception))
            self.assertEqual(self.read(), text)          # never reset

    def test_hand_written_entry_is_accepted(self):
        self.write(reg_of({"login": "amy", "name": "Amy A", "tags": ["mvp"],
                           "affiliation": "community",
                           "maintainer_of": {"o/r": True}, "source": "operator",
                           "do_not_name": False, "note": "met at a conference"}))
        reg = people.load_registry(self.path)
        self.assertEqual(reg["people"]["amy"]["tags"], ["mvp"])


class TestOperatorEditsWin(unittest.TestCase):

    PATCH = {"label": {"affiliation": "community", "signal": "profile_company",
                       "strength": "weak"},
             "name": "From Profile", "looked_up_at": "2026-11-01",
             "seen": [("2026-09-03", SLUG), ("2026-09-20", SLUG)],
             "maintainer": {SLUG: {"signal": "merged_others_pr",
                                   "last_evidence": "2026-09-20"}}}

    def test_new_entry_takes_the_lookup(self):
        e = people.apply_patch(None, self.PATCH, "amy")
        self.assertEqual(e["source"], "lookup")
        self.assertEqual((e["affiliation"], e["signal"], e["strength"]),
                         ("community", "profile_company", "weak"))
        self.assertEqual(e["name"], "From Profile")
        self.assertEqual(e["first_seen"], {"date": "2026-09-03", "repo": SLUG})
        self.assertEqual(e["last_seen"], {"date": "2026-09-20", "repo": SLUG})
        self.assertEqual(e["maintainer_of"][SLUG], {
            "maintainer": True, "signal": "merged_others_pr", "strength": "strong",
            "last_evidence": "2026-09-20"})
        people.check_entry("amy", e)

    def test_source_operator_keeps_every_decided_field(self):
        mine = {"login": "amy", "source": "operator", "name": "Amy Mine",
                "affiliation": "maintainer_org", "tags": ["known"],
                "maintainer_of": {SLUG: {"maintainer": False}}}
        e = people.apply_patch(mine, self.PATCH, "amy")
        for k in ("name", "affiliation", "tags", "maintainer_of", "source"):
            self.assertEqual(e[k], mine[k], k)
        self.assertNotIn("signal", e)
        # observations are still recorded
        self.assertEqual(e["last_seen"]["date"], "2026-09-20")

    def test_locked_list_pins_only_those_fields(self):
        mine = {"login": "amy", "source": "lookup", "name": "Amy Mine",
                "locked": ["name"]}
        e = people.apply_patch(mine, self.PATCH, "amy")
        self.assertEqual(e["name"], "Amy Mine")
        self.assertEqual(e["affiliation"], "community")

    def test_plain_maintainer_value_is_the_operators(self):
        mine = {"login": "amy", "maintainer_of": {SLUG: False, "o/other": True}}
        e = people.apply_patch(mine, self.PATCH, "amy")
        self.assertEqual(e["maintainer_of"], {SLUG: False, "o/other": True})
        self.assertEqual(people.maintainer_for(e, SLUG), (False, "operator"))
        self.assertEqual(people.maintainer_for(e, "o/other"), (True, "operator"))
        self.assertEqual(people.maintainer_for(e, "o/third"), (False, "none"))

    def test_stronger_maintainer_signal_stays(self):
        e = people.apply_patch(None, self.PATCH, "amy")
        weaker = {"maintainer": {SLUG: {"signal": "approving_reviewer",
                                        "last_evidence": "2026-10-30"}}}
        e = people.apply_patch(e, weaker, "amy")
        self.assertEqual(e["maintainer_of"][SLUG]["signal"], "merged_others_pr")
        self.assertEqual(e["maintainer_of"][SLUG]["last_evidence"], "2026-10-30")

    def test_do_not_name_never_gets_a_name_and_tags_are_never_written(self):
        mine = {"login": "amy", "do_not_name": True, "tags": ["private"]}
        e = people.apply_patch(mine, dict(self.PATCH, tags=["x"]), "amy")
        self.assertNotIn("name", e)
        self.assertEqual(e["tags"], ["private"])
        self.assertIsNone(people.display_name(dict(e, name="Should Not Show")))

    def test_empty_profile_name_stores_none(self):
        e = people.apply_patch({"login": "amy", "name": "Old"}, {"name": ""}, "amy")
        self.assertNotIn("name", e)


class TestConcurrentEdits(TmpDir):

    def test_save_merges_over_the_file_as_it_is_now(self):
        self.write(reg_of({"login": "amy", "source": "lookup"}))
        run = people.Run(self.path, now="2026-11-26")
        run.patch("amy", name="Amy Profile", looked_up_at="2026-11-26",
                  label={"affiliation": "community", "signal": "profile_company",
                         "strength": "weak"})
        run.patch("Bob", seen=[("2026-11-02", SLUG)])
        # the owner edits the file while the gather runs
        self.write(reg_of({"login": "amy", "source": "lookup", "tags": ["mvp"],
                           "name": "Amy Mine", "locked": ["name"]}))
        self.assertTrue(run.save())
        reg = people.load_registry(self.path)
        amy = reg["people"]["amy"]
        self.assertEqual((amy["name"], amy["tags"]), ("Amy Mine", ["mvp"]))
        self.assertEqual(amy["affiliation"], "community")
        self.assertEqual(reg["people"]["Bob"]["first_seen"]["date"], "2026-11-02")
        self.assertFalse(run.save())                 # nothing pending

    def test_logins_match_ignoring_case(self):
        self.write(reg_of({"login": "AlexS", "name": "Alex S"}))
        run = people.Run(self.path, now="2026-11-26")
        run.patch("alexs", seen=[("2026-11-02", SLUG)])
        run.save()
        reg = people.load_registry(self.path)
        self.assertEqual(sorted(reg["people"]), ["AlexS"])


class TestRefreshPolicy(unittest.TestCase):

    def test_states(self):
        now = "2026-11-26"
        self.assertEqual(people.lookup_state(None, now), "never")
        self.assertEqual(people.lookup_state({"name": "x"}, now), "never")
        decided = {"affiliation": "community", "looked_up_at": "2026-08-01"}
        self.assertEqual(people.lookup_state(decided, now), "fresh")      # 117 days
        self.assertEqual(people.lookup_state(dict(decided, looked_up_at="2026-07-01"),
                                             now), "stale")               # 148 days
        unknown = {"affiliation": "unknown", "looked_up_at": "2026-11-01"}
        self.assertEqual(people.lookup_state(unknown, now), "fresh")
        self.assertEqual(people.lookup_state(dict(unknown, looked_up_at="2026-10-01"),
                                             now), "stale")               # retried sooner
        self.assertEqual(people.lookup_state(decided, now, refresh_days=30), "stale")


class TestMaintainerEvidence(unittest.TestCase):

    def pr(self, n, author, merged_by=None, approvals=(), assoc="NONE"):
        return {"number": n, "author": author, "author_association": assoc,
                "merged": merged_by is not None, "merged_by": merged_by,
                "merged_at": "2026-09-{:02d}T10:00:00Z".format(n) if merged_by else None,
                "created_at": "2026-09-01T00:00:00Z",
                "reviews": [{"author": a, "state": "approved",
                             "submitted_at": "2026-09-{:02d}T09:00:00Z".format(n)}
                            for a in approvals]}

    def test_rules(self):
        prs = [self.pr(1, "dev", merged_by="lead"),
               self.pr(2, "lead", merged_by="lead"),
               self.pr(3, "dev", merged_by="dev"),
               self.pr(4, "a", merged_by="lead", approvals=["rev", "rev2"]),
               self.pr(5, "b", merged_by="lead", approvals=["rev", "rev2"]),
               self.pr(6, "c", merged_by="lead", approvals=["rev", "c"]),
               self.pr(7, "d", approvals=["rev2"]),                # not merged
               self.pr(8, "e", merged_by="azure-bot[bot]"),
               self.pr(9, "owner1", assoc="OWNER"),
               self.pr(10, "member1", assoc="MEMBER"),
               self.pr(11, "collab", assoc="COLLABORATOR")]
        prs[0]["comments_list"] = [{"author": "collab2",
                                    "author_association": "COLLABORATOR",
                                    "created_at": "2026-09-02T00:00:00Z"}]
        got = people.maintainer_evidence(prs)
        sig = {k: v["signal"] for k, v in got.items()}
        self.assertEqual(sig, {"lead": "merged_others_pr", "dev": "merged_own_pr",
                               "rev": "approving_reviewer", "owner1": "OWNER",
                               "collab": "COLLABORATOR", "collab2": "COLLABORATOR"})
        # rev2 approved two merged PRs: below the threshold; a self-approval
        # (c on #6) does not count; MEMBER is an affiliation, not a role
        self.assertNotIn("rev2", got)
        self.assertNotIn("member1", got)
        self.assertNotIn("azure-bot[bot]", got)
        self.assertEqual(got["lead"]["strength"], "strong")
        self.assertEqual(got["lead"]["last_evidence"], "2026-09-06")
        self.assertEqual(got["rev"]["strength"], "medium")
        self.assertEqual(people.APPROVALS_MIN, 3)

    def test_activity_dates(self):
        prs = [self.pr(4, "a", merged_by="lead", approvals=["rev"])]
        commits = [{"author": "a", "date": "2026-08-30", "login_resolved": True},
                   {"author": "Some Name", "date": "2026-08-01",
                    "login_resolved": False}]
        got = people.activity_dates(prs, [], commits)
        self.assertEqual(got["a"], ("2026-08-30", "2026-09-01"))
        self.assertEqual(got["lead"], ("2026-09-04", "2026-09-04"))
        self.assertNotIn("Some Name", got)


def _lookup(members=(), companies=None, names=None, fail=()):
    calls = []

    def fn(kind, login, org=None):
        calls.append((kind, login, org))
        if login in fail:
            return None
        if kind == "public_member":
            return (org, login) in members
        if kind == "name":
            return (names or {}).get(login, "")
        return (companies or {}).get(login, "")
    fn.calls = calls
    return fn


def _profile_calls(lk):
    """Distinct API calls the stub saw: a `name` and a `company` question
    about one login are answered by ONE users/{login} response."""
    return sorted({(k if k == "public_member" else "users", login, org)
                   for k, login, org in lk.calls})


class TestRegistryDecisions(TmpDir):

    def cfg(self, **kw):
        return gather.affiliation_config("o", home_orgs=["bigco"],
                                         home_names=["Big Co"], **kw)

    def run_(self, now="2026-11-26", **kw):
        return people.Run(self.path, now=now, **kw)

    RECORDS = ([{"number": 1, "author": "newbie", "merged": True,
                 "merged_by": "lead", "merged_at": "2026-11-05T00:00:00Z",
                 "created_at": "2026-11-01T00:00:00Z",
                 "author_association": "NONE"}], [], [])

    def decide(self, run, people_list, lk, assoc=None, **kw):
        return gather.find_affiliations_registry(
            people_list, self.cfg(**kw.pop("cfg", {})), run, SLUG, assoc or {},
            lookup=lk, records=kw.pop("records", self.RECORDS), **kw)

    def test_lookup_once_then_reuse_without_a_call(self):
        lk = _lookup(members={("bigco", "lead")}, companies={"newbie": COMPANY_SECRET},
                     names={"newbie": "New Person", "lead": "Lead Person"})
        run = self.run_()
        fact, counts = self.decide(run, ["lead", "newbie"], lk)
        run.save()
        self.assertEqual(counts["looked_up"], 2)
        p = fact["people"]
        self.assertEqual(p["lead"], {"affiliation": "maintainer_org",
                                     "signal": "public_membership", "strength": "strong",
                                     "name": "Lead Person", "maintainer": True,
                                     "maintainer_signal": "merged_others_pr"})
        self.assertEqual(p["newbie"], {"affiliation": "community",
                                       "signal": "profile_company", "strength": "weak",
                                       "name": "New Person", "maintainer": False,
                                       "maintainer_signal": "none"})
        # newbie: one users/{login} call gave the company AND the name; lead,
        # decided by a public membership, costs one users call for the name
        self.assertEqual(_profile_calls(lk), [("public_member", "lead", "bigco"),
                                              ("public_member", "lead", "o"),
                                              ("public_member", "newbie", "bigco"),
                                              ("public_member", "newbie", "o"),
                                              ("users", "lead", None),
                                              ("users", "newbie", None)])
        text = self.read()
        self.assertNotIn(COMPANY_SECRET, text)
        reg = people.load_registry(self.path)["people"]
        self.assertEqual(reg["newbie"]["looked_up_at"], "2026-11-26")
        self.assertEqual(reg["newbie"]["source"], "lookup")
        # the next month: no call at all, the same labels
        lk2 = _lookup()
        run2 = self.run_(now="2026-12-20")
        fact2, counts2 = self.decide(run2, ["lead", "newbie"], lk2)
        self.assertEqual(lk2.calls, [])
        self.assertEqual(counts2["reused"], 2)
        self.assertEqual(fact2["people"], fact["people"])

    def test_stale_entries_refresh_oldest_first_within_the_budget(self):
        self.write(reg_of(
            {"login": "old", "affiliation": "community", "signal": "profile_company",
             "strength": "weak", "looked_up_at": "2026-01-01"},
            {"login": "older", "affiliation": "community", "signal": "profile_company",
             "strength": "weak", "looked_up_at": "2025-12-01"},
            {"login": "fresh", "affiliation": "community", "signal": "profile_company",
             "strength": "weak", "looked_up_at": "2026-11-01"}))
        lk = _lookup(companies={"older": "Big Co"})
        run = self.run_(refresh_max=1)
        fact, counts = self.decide(run, ["old", "older", "fresh"], lk)
        self.assertEqual(counts["refreshed"], 1)
        self.assertEqual({c[1] for c in lk.calls}, {"older"})
        self.assertEqual(fact["people"]["older"]["affiliation"], "maintainer_org")
        # beyond the budget: the stale label is reused as it is
        self.assertEqual(fact["people"]["old"]["signal"], "profile_company")
        self.assertEqual(counts["reused"], 2)
        run.save()
        reg = people.load_registry(self.path)["people"]
        self.assertEqual(reg["older"]["looked_up_at"], "2026-11-26")
        self.assertEqual(reg["old"]["looked_up_at"], "2026-01-01")

    def test_the_refresh_budget_skips_people_decided_for_free(self):
        stale = {"affiliation": "community", "signal": "profile_company",
                 "strength": "weak"}
        self.write(reg_of(dict(stale, login="member", looked_up_at="2025-01-01"),
                          dict(stale, login="older", looked_up_at="2025-06-01")))
        lk = _lookup()
        fact, counts = self.decide(self.run_(refresh_max=1, name_max=0),
                                   ["member", "older"], lk,
                                   assoc={"member": "MEMBER"})
        self.assertEqual({c[1] for c in lk.calls}, {"older"})
        self.assertEqual(counts["refreshed"], 1)
        self.assertEqual(fact["people"]["member"]["signal"], "author_association")

    def test_unknown_is_retried_sooner_than_decided(self):
        home = gather.affiliation_home(self.cfg())
        self.write(reg_of(
            {"login": "u", "affiliation": "unknown", "signal": "none",
             "looked_up_at": "2026-10-01", "home": home},
            {"login": "d", "affiliation": "community", "signal": "profile_company",
             "strength": "weak", "looked_up_at": "2026-10-01", "home": home}))
        lk = _lookup()
        self.decide(self.run_(), ["u", "d"], lk)
        self.assertEqual({c[1] for c in lk.calls}, {"u"})

    def test_operator_label_and_do_not_name(self):
        self.write(reg_of(
            {"login": "pinned", "affiliation": "community", "source": "operator",
             "name": "Pinned Person"},
            {"login": "quiet", "do_not_name": True, "name": "Quiet Person",
             "maintainer_of": {SLUG: True}}))
        lk = _lookup()
        fact, counts = self.decide(run=self.run_(), people_list=["pinned", "quiet"],
                                   lk=lk)
        self.assertEqual(lk.calls, [])
        self.assertEqual(fact["people"]["pinned"], {
            "affiliation": "community", "signal": "override", "strength": "operator",
            "name": "Pinned Person", "maintainer": False, "maintainer_signal": "none"})
        self.assertEqual(fact["people"]["quiet"], {"affiliation": "unknown",
                                                   "signal": "do_not_name"})
        self.assertIn("quiet", fact["unknown"])
        self.assertEqual(counts["do_not_name"], 1)

    def test_offline_makes_no_call(self):
        self.write(reg_of({"login": "known", "affiliation": "community",
                           "signal": "profile_company", "strength": "weak",
                           "looked_up_at": "2025-01-01", "name": "Known Person"}))
        fact, counts = self.decide(self.run_(offline=True),
                                   ["known", "lead", "stranger"], None,
                                   assoc={"stranger": "MEMBER"})
        p = fact["people"]
        self.assertEqual(p["known"]["signal"], "profile_company")    # stale, reused
        self.assertEqual(p["known"]["name"], "Known Person")
        self.assertEqual(p["stranger"]["signal"], "author_association")
        self.assertEqual(p["lead"], {"affiliation": "unknown", "signal": "not_checked",
                                     "maintainer": True,
                                     "maintainer_signal": "merged_others_pr"})
        self.assertEqual(fact["not_checked"], ["lead"])
        self.assertEqual(counts["looked_up"] + counts["refreshed"], 0)

    def test_names_for_people_decided_without_a_lookup(self):
        lk = _lookup(names={"a": "Ann", "b": "Bea", "c": "Cy"})
        run = self.run_(name_max=2)
        fact, counts = self.decide(run, ["a", "b", "c"], lk,
                                   assoc={k: "MEMBER" for k in "abc"},
                                   name_first=["c"])
        self.assertEqual(counts["names_fetched"], 2)
        self.assertEqual({c[1] for c in lk.calls}, {"c", "a"})   # newcomer first
        self.assertEqual(fact["people"]["c"]["name"], "Cy")
        self.assertNotIn("name", fact["people"]["b"])            # the long tail
        run.save()
        reg = people.load_registry(self.path)["people"]
        self.assertEqual(reg["c"]["affiliation"], "maintainer_org")
        self.assertEqual(reg["c"]["signal"], "author_association")
        # a name-only call is not repeated for someone whose profile has none
        lk2 = _lookup()
        self.decide(self.run_(), ["a", "c"], lk2, assoc={"a": "MEMBER", "c": "MEMBER"})
        self.assertEqual(lk2.calls, [])

    def test_a_failed_lookup_leaves_the_registry_alone(self):
        lk = _lookup(fail={"flaky"})
        run = self.run_()
        fact, counts = self.decide(run, ["flaky"], lk)
        run.save()
        self.assertEqual(fact["people"]["flaky"]["signal"], "lookup_failed")
        self.assertEqual(counts["lookup_failed"], 1)
        e = people.load_registry(self.path)["people"].get("flaky") or {}
        self.assertNotIn("looked_up_at", e)
        self.assertNotIn("affiliation", e)

    def test_maintainer_is_per_repository(self):
        self.write(reg_of({"login": "lead", "maintainer_of": {
            "o/other": {"maintainer": True, "signal": "merged_others_pr",
                        "strength": "strong"}}}))
        run = self.run_(offline=True)
        fact, _ = gather.find_affiliations_registry(
            ["lead"], self.cfg(), run, SLUG, {}, records=([], [], []))
        self.assertEqual((fact["people"]["lead"]["maintainer"],
                          fact["people"]["lead"]["maintainer_signal"]), (False, "none"))
        fact, _ = gather.find_affiliations_registry(
            ["lead"], self.cfg(), run, "o/other", {}, records=([], [], []))
        self.assertTrue(fact["people"]["lead"]["maintainer"])


class NamedProfiles(AffiliationRepo):
    """AffiliationRepo whose profiles also carry a display name (and fields
    that must never be kept)."""

    NAMES = {"bob": "Bob Builder", "carol": "Carol C", "maint": "Main Tainer",
             "alice": "Alice A"}

    def get_page(self, url):
        page, nxt = super().get_page(url)
        if url.startswith(gather.USERS_API) and isinstance(page, dict):
            page = dict(page, name=self.NAMES.get(page["login"]),
                        blog="https://private.example", location="Somewhere")
        return page, nxt


class TestRegistryInAcquire(NamedProfiles):

    def setUp(self):
        super().setUp()
        self.people_path = os.path.join(self.tmp, "people.json")

    def gather(self, *extra, store="s.db"):
        with mock.patch.object(people, "today", lambda: "2026-11-26"):
            return self.gather_into(os.path.join(self.tmp, store), *self.EXTRA,
                                    "--people", self.people_path, *extra)

    def test_registry_fact_view_and_privacy(self):
        bundle, err = self.gather()
        fact = bundle["affiliations"]
        p = fact["people"]
        self.assertEqual(p["carol"]["name"], "Carol C")
        self.assertEqual((p["maint"]["maintainer"], p["maint"]["maintainer_signal"]),
                         (True, "merged_others_pr"))
        self.assertEqual(p["alice"]["maintainer"], False)
        self.assertIn("people registry: o/r:", err)
        with open(self.people_path, encoding="utf-8") as fh:
            text = fh.read()
        reg = people.parse_registry(text)["people"]
        self.assertEqual(reg["maint"]["maintainer_of"]["o/r"]["signal"],
                         "merged_others_pr")
        self.assertEqual(reg["carol"]["first_seen"]["repo"], "o/r")
        for secret in (COMPANY_SECRET, "Big Co Inc.", "PRIVATE BIO", EMAIL_SECRET,
                       "private.example", "Somewhere"):
            self.assertNotIn(secret, text)
        # the trust check passes and the view carries the stored fact
        store = os.path.join(self.tmp, "s.db")
        code, out, view_ = cross_check(store)
        self.assertEqual(code, 0, out)
        self.assertEqual(view_["affiliations"], fact)
        raw = sqlite3.connect(store)
        try:
            dump = "\n".join(str(r) for t in ("nodes", "meta", "edges")
                             for r in raw.execute("SELECT * FROM " + t))
        finally:
            raw.close()
        for secret in (COMPANY_SECRET, "PRIVATE BIO", EMAIL_SECRET, "Somewhere"):
            self.assertNotIn(secret, dump)

    def test_second_gather_makes_no_profile_call(self):
        self.gather()
        first = list(self.profile_urls)
        self.assertTrue(first)
        self.profile_urls.clear()
        bundle, err = self.gather(store="again.db")
        self.assertEqual(self.profile_urls, [])
        self.assertIn("0 looked up", err)
        self.assertEqual(bundle["affiliations"]["people"]["carol"]["name"], "Carol C")

    def test_offline_and_old_reports_stay_reproducible(self):
        bundle, _ = self.gather()
        self.profile_urls.clear()
        # the owner renames a person afterwards: the old window's fact (and so
        # its view) is unchanged; a new gather uses the new name
        reg = people.load_registry(self.people_path)
        reg["people"]["carol"].update(name="Carol Renamed", locked=["name"])
        people.write_registry(self.people_path, reg)
        code, out, view_ = cross_check(os.path.join(self.tmp, "s.db"))
        self.assertEqual(view_["affiliations"]["people"]["carol"]["name"], "Carol C")
        b2, err = self.gather("--people-offline", store="off.db")
        self.assertEqual(self.profile_urls, [])
        self.assertIn("offline: no API call", err)
        self.assertEqual(b2["affiliations"]["people"]["carol"]["name"], "Carol Renamed")

    def test_without_people_nothing_changes(self):
        bundle, err = self.gather_into(os.path.join(self.tmp, "s.db"), *self.EXTRA)
        self.assertFalse(os.path.exists(self.people_path))
        self.assertNotIn("people registry", err)
        for e in bundle["affiliations"]["people"].values():
            self.assertFalse(set(e) & {"name", "maintainer", "maintainer_signal"})

    def test_no_affiliation_leaves_the_registry_alone(self):
        self.gather("--no-affiliation")
        self.assertFalse(os.path.exists(self.people_path))
        self.assertEqual(self.profile_urls, [])

    def test_a_malformed_registry_stops_the_gather(self):
        with open(self.people_path, "w", encoding="utf-8") as fh:
            json.dump(reg_of({"login": "carol", "company": "x"}), fh)
        with self.assertRaises(SystemExit) as cm:
            self.gather()
        self.assertIn("entry 'carol'", str(cm.exception.code))

    def test_validate_refuses_a_bad_maintainer_signal(self):
        value = {"collected": True, "home_orgs": ["o"], "home_names": ["o"],
                 "home_email_domains": [], "unknown": [], "not_checked": [],
                 "people": {"x": {"affiliation": "community", "signal": "override",
                                  "strength": "operator", "name": "X",
                                  "maintainer": True,
                                  "maintainer_signal": "MEMBER"}}}
        self.assertIn("maintainer_signal", validate._affiliations_problem(value))
        value["people"]["x"]["maintainer_signal"] = "merged_others_pr"
        self.assertIsNone(validate._affiliations_problem(value))
        value["people"]["x"]["company"] = COMPANY_SECRET
        self.assertIn("holds more than", validate._affiliations_problem(value))
        value["people"]["x"] = {"affiliation": "unknown", "signal": "do_not_name",
                                "name": "Leak"}
        self.assertIn("do_not_name", validate._affiliations_problem(value))


class TestSettings(TmpDir):

    def test_flags_and_env(self):
        args = gather.parse_args(["--owner", "o", "--repo", "r", "--store", "s.db",
                                  "--people", self.path, "--people-offline",
                                  "--people-refresh-days", "60"])
        run = gather.people_run_from_args(
            args, env={"ACTIVITY_PEOPLE_REFRESH_MAX": "5",
                       "ACTIVITY_PEOPLE_NAME_MAX": "0"})
        self.assertIs(gather.people_run_from_args(args), run)
        self.assertEqual((run.refresh_days, run.refresh_left, run.names_left,
                          run.offline), (60, 5, 0, True))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gather.parse_args(["--owner", "o", "--repo", "r", "--store", "s.db",
                               "--people-offline"])
        with self.assertRaises(SystemExit):
            gather.validate_env_knobs({"ACTIVITY_PEOPLE_REFRESH_DAYS": "soon"})
        self.assertEqual(gather._people_settings({}), {
            "refresh_days": 120, "unknown_retry_days": 30, "refresh_max": 50,
            "name_max": 30})

    def test_manifest_people_path_is_relative_to_the_manifest(self):
        d = os.path.join(self.tmp, "stream")
        os.makedirs(d)
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"project": "p", "window": {"from": FROM, "to": TO},
                       "repos": [{"owner": "o", "repo": "a"}],
                       "people": "people.json"}, fh)
        self.assertEqual(manifest.load_manifest(path)["people"],
                         os.path.join(d, "people.json"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"project": "p", "window": {"from": FROM, "to": TO},
                       "repos": [{"owner": "o", "repo": "a"}], "people": 3}, fh)
        with self.assertRaises(ValueError):
            manifest.load_manifest(path)

    def test_offline_without_a_registry_is_refused(self):
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"project": "p", "window": {"from": FROM, "to": TO},
                       "repos": [{"owner": "o", "repo": "a"}]}, fh)
        store = os.path.join(self.tmp, "s.db")
        with self.assertRaises(SystemExit) as cm:
            gather.main(["--manifest", path, "--store", store, "--people-offline"])
        self.assertIn("--people-offline needs", str(cm.exception.code))
        self.assertFalse(os.path.exists(store))           # nothing touched

    def test_manifest_members_share_one_run(self):
        seen = []

        def fake_acquire(args, env):
            seen.append(gather.people_run_from_args(args))
            raise RuntimeError("stop here")
        man = {"project": "p", "from": FROM, "to": TO, "ticket_prefixes": [],
               "repos": [{"owner": "o", "repo": "a"}, {"owner": "o", "repo": "b"}],
               "people": self.path}
        args = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
        args.people_manifest = man["people"]
        gather.people_run_from_args(args)
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        with mock.patch.object(gather, "acquire", fake_acquire), \
                contextlib.redirect_stderr(io.StringIO()):
            gather._gather_manifest(conn, args, man)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])
        self.assertEqual(seen[0].path, self.path)

    def test_the_example_manifest_carries_the_azure_defaults(self):
        here = os.path.dirname(os.path.abspath(__file__))
        man = manifest.load_manifest(os.path.join(here, "examples", "manifest.json"))
        aff = man["affiliation"]
        self.assertEqual(aff["home_orgs"], ["Azure", "microsoft"])
        self.assertEqual(aff["home_names"], ["Microsoft"])
        self.assertEqual(aff["home_email_domains"], ["microsoft.com"])
        self.assertEqual(os.path.basename(man["people"]), "people.json")


def _fact_with_registry():
    b = _window_with_facts()
    p = b["affiliations"]["people"]
    p["dev100"].update(name="Dev Hundred", maintainer=False, maintainer_signal="none")
    p["rev"].update(name="Rev Iewer", maintainer=True,
                    maintainer_signal="approving_reviewer")
    p["dev7454"].update(maintainer=True, maintainer_signal="merged_others_pr")
    p["dev103"] = {"affiliation": "unknown", "signal": "do_not_name"}
    return b


class TestViews(unittest.TestCase):

    def test_people_halls_contributors_and_summary(self):
        b = link.enrich(_fact_with_registry())
        ppl = b["people"]
        self.assertEqual((ppl["rev"]["name"], ppl["rev"]["maintainer"],
                          ppl["rev"]["maintainer_signal"]),
                         ("Rev Iewer", True, "approving_reviewer"))
        self.assertNotIn("name", ppl["dev103"])               # do_not_name
        self.assertNotIn("maintainer", ppl["dev103"])
        c = b["contributors"]
        nc = {r["login"]: r for r in c["newcomers"]}
        self.assertEqual(nc["dev100"]["name"], "Dev Hundred")
        self.assertIs(nc["dev100"]["maintainer"], False)
        self.assertFalse(set(nc["dev103"]) & {"name", "maintainer"})
        hall = {h["login"]: h for h in c["hall"]}
        self.assertEqual(hall["rev"]["name"], "Rev Iewer")
        fame = {h["login"]: h for h in b["halls"]["fame"]}
        self.assertTrue(fame["rev"]["maintainer"])
        m = c["affiliation"]["maintainers"]
        self.assertEqual(m["people"]["maintainer"], 2)
        self.assertEqual(m["signals"], {"approving_reviewer": 1,
                                        "merged_others_pr": 1})
        self.assertEqual(m["by_affiliation"]["maintainer_org"]["maintainer"], 2)
        self.assertEqual(m["by_affiliation"]["community"]["non_maintainer"], 1)
        self.assertEqual(sum(m["people"].values()),
                         sum(c["affiliation"]["people"].values()))
        self.assertEqual(m["logins"], ["dev7454", "rev"])

    def test_without_registry_entries_nothing_is_added(self):
        b = link.enrich(_window_with_facts())
        self.assertNotIn("maintainers", b["contributors"]["affiliation"])
        for h in b["halls"]["fame"]:
            self.assertNotIn("name", h)
        self.assertEqual(link.slice_participants(b, b["trains"][0]), {})

    def test_story_slice_participants(self):
        b = link.enrich(_fact_with_registry())
        train = next(t for t in b["trains"] if 100 in (t.get("prs") or []))
        s = link.slice_train(b, train["id"])
        part = s["participants"]
        self.assertEqual(part["dev100"], {"name": "Dev Hundred", "maintainer": False,
                                          "maintainer_signal": "none",
                                          "affiliation": "community"})
        self.assertEqual(part["rev"]["name"], "Rev Iewer")

    def test_digest_merges_name_and_maintainer(self):
        a = {"x": {"name": "Ex", "maintainer": False, "maintainer_signal": "none"}}
        b = {"x": {"maintainer": True, "maintainer_signal": "merged_others_pr"}}
        members = [{"repo": r, "bundle": {"people": p, "prs": []}}
                   for r, p in (("o/a", a), ("o/b", b))]
        got = digest._merge_people(members)["x"]
        self.assertEqual((got["name"], got["maintainer"], got["maintainer_signal"],
                          got["maintainer_of"]),
                         ("Ex", True, "merged_others_pr", ["o/b"]))
        got = digest._merge_people(members[:1])["x"]
        self.assertEqual((got["maintainer"], got.get("maintainer_of")), (False, None))


class TestSeed(TmpDir):

    def test_seed_from_a_store_and_summary(self):
        repo = NamedProfiles(methodName="runTest")
        repo.runTest = lambda: None
        repo.setUp()
        try:
            store = os.path.join(repo.tmp, "s.db")
            repo.gather_into(store, *repo.EXTRA)
            urls_before = len(repo.urls)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                people.main(["seed", "--people", self.path, store])
            self.assertEqual(len(repo.urls), urls_before)      # no API call
        finally:
            repo.tearDown()
        reg = people.load_registry(self.path)["people"]
        self.assertEqual(reg["maint"]["maintainer_of"]["o/r"]["signal"],
                         "merged_others_pr")
        self.assertEqual(reg["alice"]["signal"], "author_association")
        self.assertEqual(reg["bob"]["signal"], "public_membership")   # stored fact
        self.assertNotIn("looked_up_at", reg["bob"])
        self.assertNotIn("name", reg["bob"])
        self.assertIn("maintainers of o/r: 1", out.getvalue())
        self.assertIn("merged_others_pr (1): maint", out.getvalue())
        # a second seed changes nothing (idempotent, no reordering churn)
        before = self.read()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            people.main(["summary", "--people", self.path, "--json"])
        self.assertEqual(self.read(), before)
        s = people.summarize(people.load_registry(self.path))
        self.assertEqual(s["maintainers"], {"o/r": {"merged_others_pr": ["maint"]}})


if __name__ == "__main__":
    unittest.main()
