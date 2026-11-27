"""Review fixes of the people layer (issue #97, PR #119):

* a `do_not_name` person never keeps or regains a display name, an
  affiliation or the maintainer property: not from labels a profile carried
  before (a re-enriched view), not from another member of a project;
* a cached registry label is relative to the home settings it was decided
  against (`maintainer_org` / `community` mean "of THESE home
  organisations"), so it is reused only under the same home settings; names
  stay shared;
* window roll-ups keep the earliest newcomer class and the people a window
  left `not_checked`.

No network: the lookup seam is stubbed.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import people  # noqa: E402
from test_late_month_gather import fold_into  # noqa: E402
from test_people_layer import _window_with_facts  # noqa: E402

LABEL_KEYS = {"name", "maintainer", "maintainer_signal", "maintainer_of"}
SUPPRESSED = {"affiliation": "unknown", "signal": "do_not_name"}


def _named_view():
    """An enriched view whose `rev` carries a display name, an affiliation
    and the maintainer property from the registry."""
    b = _window_with_facts()
    b["affiliations"]["people"]["rev"].update(
        name="Rev Iewer", maintainer=True, maintainer_signal="approving_reviewer")
    return link.enrich(b)


def _rows_naming(b, login):
    """Every row of an enriched view that describes `login`."""
    rows = [b["people"][login]]
    rows += [h for h in (b.get("halls") or {}).get("fame") or []
             if h.get("login") == login]
    c = b.get("contributors") or {}
    rows += [r for key in ("hall", "newcomers") for r in c.get(key) or []
             if r.get("login") == login]
    for t in b.get("trains") or []:
        row = link.slice_participants(b, t).get("participants", {}).get(login)
        if row is not None:
            rows.append(row)
    return rows


class TestSuppressionClearsEarlierLabels(unittest.TestCase):
    """Finding 1: annotate_people_affiliation copies only the keys the
    current entry carries, so labels from an earlier enrichment survived a
    `do_not_name` entry (or an entry the fact no longer has)."""

    def test_do_not_name_entry_clears_name_and_maintainer(self):
        b = _named_view()
        self.assertEqual(b["people"]["rev"]["name"], "Rev Iewer")   # precondition
        b["affiliations"]["people"]["rev"] = dict(SUPPRESSED)
        b = link.enrich(b)
        rows = _rows_naming(b, "rev")
        self.assertGreater(len(rows), 2)        # person, halls, participants
        for row in rows:
            self.assertFalse(set(row) & LABEL_KEYS, row)
            self.assertNotEqual(row.get("affiliation"), "maintainer_org", row)
        self.assertEqual(b["people"]["rev"]["affiliation_signal"], "do_not_name")
        m = b["contributors"]["affiliation"].get("maintainers") or {}
        self.assertNotIn("rev", m.get("logins") or [])

    def test_a_person_the_fact_no_longer_names_has_no_labels(self):
        b = _named_view()
        del b["affiliations"]["people"]["rev"]
        b = link.enrich(b)
        for row in _rows_naming(b, "rev"):
            self.assertFalse(set(row) & LABEL_KEYS, row)
        self.assertEqual(b["people"]["rev"]["affiliation"], "unknown")

    def test_without_a_fact_the_people_are_untouched(self):
        b = {"people": {"x": {"name": "Ex", "maintainer": True}}}
        derive.annotate_people_affiliation(b)
        self.assertEqual(b["people"]["x"], {"name": "Ex", "maintainer": True})


class TestProjectMergeSuppressionWins(unittest.TestCase):
    """Finding 2: in the project roll-up a known affiliation outranked
    `do_not_name` and the name / maintainer merge restored the labels."""

    SUPPRESSED_REC = {"affiliation": "unknown", "affiliation_signal": "do_not_name",
                      "prs_merged": 1}
    NAMED_REC = {"affiliation": "maintainer_org",
                 "affiliation_signal": "public_membership", "name": "Ex Person",
                 "maintainer": True, "maintainer_signal": "merged_others_pr",
                 "prs_merged": 2}

    def merged(self, first, second):
        members = [{"repo": r, "bundle": {"people": {"x": dict(p)}, "prs": []}}
                   for r, p in (("o/a", first), ("o/b", second))]
        return digest._merge_people(members)

    def test_suppressed_in_either_member_order(self):
        for order in ((self.SUPPRESSED_REC, self.NAMED_REC),
                      (self.NAMED_REC, self.SUPPRESSED_REC)):
            ppl = self.merged(*order)
            got = ppl["x"]
            self.assertEqual((got["affiliation"], got["affiliation_signal"]),
                             ("unknown", "do_not_name"))
            self.assertFalse(set(got) & LABEL_KEYS, got)
            self.assertEqual(got["prs_merged"], 3)      # counts still combine
            fame = derive.build_halls({"people": ppl})["halls"]["fame"]
            self.assertEqual([h["login"] for h in fame], ["x"])
            self.assertFalse(set(fame[0]) & LABEL_KEYS, fame[0])

    def test_unsuppressed_merge_is_unchanged(self):
        other = dict(self.NAMED_REC, affiliation="community",
                     affiliation_signal="profile_company", name="Other",
                     maintainer=False, maintainer_signal="none")
        got = self.merged(self.NAMED_REC, other)["x"]
        self.assertEqual((got["affiliation"], got["name"], got["maintainer"],
                          got["maintainer_of"]),
                         ("maintainer_org", "Ex Person", True, ["o/a"]))


def _lookup(members=(), companies=None, names=None, fail=()):
    calls = []

    def fn(kind, login, org=None):
        calls.append((kind, login, org))
        if login in fail:
            return None
        if kind == "public_member":
            return (org.lower(), login) in members
        if kind == "name":
            return (names or {}).get(login, "")
        return (companies or {}).get(login, "")
    fn.calls = calls
    return fn


class TestLabelsScopedToTheHomeSettings(unittest.TestCase):
    """Finding 3: a cached `maintainer_org` / `community` label was reused
    for a repository with other home settings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "people.json")

    def tearDown(self):
        self._tmp.cleanup()

    HOME_A = gather.affiliation_config("o", home_orgs=["bigco"])
    HOME_B = gather.affiliation_config("elsewhere")

    def decide(self, config, slug="o/r", lk=None, **run_kw):
        run = people.Run(self.path, now=run_kw.pop("now", "2026-11-26"), **run_kw)
        fact, counts = gather.find_affiliations_registry(
            ["x"], config, run, slug, {}, lookup=lk, records=([], [], []))
        run.save()
        return fact["people"]["x"], counts, run

    def seed_a(self):
        lk = _lookup(members={("bigco", "x")}, names={"x": "Ex Person"})
        e, counts, _run = self.decide(self.HOME_A, lk=lk)
        self.assertEqual((e["affiliation"], counts["looked_up"]),
                         ("maintainer_org", 1))
        return people.load_registry(self.path)["people"]["x"]

    def test_the_lookup_records_the_home_and_the_same_home_reuses_it(self):
        entry = self.seed_a()
        self.assertEqual(entry["home"], gather.affiliation_home(self.HOME_A))
        lk = _lookup()
        e, counts, _run = self.decide(self.HOME_A, lk=lk, now="2026-11-27")
        self.assertEqual(lk.calls, [])
        self.assertEqual((e["affiliation"], counts["reused"]), ("maintainer_org", 1))

    def test_another_home_looks_the_person_up_again(self):
        self.seed_a()
        lk = _lookup(names={"x": "Ex Person"})    # not a member, no company
        e, counts, _run = self.decide(self.HOME_B, "elsewhere/r", lk=lk,
                                      now="2026-11-27")
        self.assertEqual(counts["refreshed"], 1)
        self.assertIn(("public_member", "x", "elsewhere"), lk.calls)
        self.assertEqual(e["affiliation"], "unknown")
        self.assertEqual(e["name"], "Ex Person")                # names stay shared
        entry = people.load_registry(self.path)["people"]["x"]
        self.assertEqual(entry["home"], gather.affiliation_home(self.HOME_B))

    def test_another_home_never_reuses_the_label_beyond_the_budget(self):
        self.seed_a()
        for kw in ({"refresh_max": 0}, {"offline": True}):
            e, counts, _run = self.decide(self.HOME_B, "elsewhere/r",
                                          lk=None if kw.get("offline") else _lookup(),
                                          now="2026-11-27", **kw)
            self.assertEqual((e["affiliation"], e["signal"]),
                             ("unknown", "not_checked"), kw)
            self.assertEqual(e["name"], "Ex Person")
            self.assertEqual(counts["reused"], 0)

    def test_a_failed_lookup_under_another_home_does_not_fall_back(self):
        self.seed_a()
        e, _counts, _run = self.decide(self.HOME_B, "elsewhere/r",
                                       lk=_lookup(fail={"x"}), now="2026-11-27")
        self.assertEqual((e["affiliation"], e["signal"]), ("unknown", "lookup_failed"))

    def test_an_entry_without_a_recorded_home_is_refreshed_then_kept(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(people.dumps({"format": people.FORMAT, "version": 1, "people": {
                "x": {"login": "x", "affiliation": "community",
                      "signal": "profile_company", "strength": "weak",
                      "looked_up_at": "2026-11-20", "source": "lookup"}}}))
        # beyond the budget: the existing stale-label path
        e, counts, _run = self.decide(self.HOME_A, refresh_max=0, lk=_lookup())
        self.assertEqual((e["affiliation"], counts["reused"]), ("community", 1))
        # within it: looked up again and the home recorded
        lk = _lookup(members={("bigco", "x")})
        e, counts, _run = self.decide(self.HOME_A, lk=lk)
        self.assertEqual((e["affiliation"], counts["refreshed"]),
                         ("maintainer_org", 1))
        self.assertEqual(people.load_registry(self.path)["people"]["x"]["home"],
                         gather.affiliation_home(self.HOME_A))

    def test_an_operator_label_is_used_under_any_home(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(people.dumps({"format": people.FORMAT, "version": 1, "people": {
                "x": {"login": "x", "affiliation": "community", "source": "operator",
                      "home": "somewhere|else"}}}))
        lk = _lookup()
        e, _counts, _run = self.decide(self.HOME_B, "elsewhere/r", lk=lk)
        self.assertEqual((e["affiliation"], e["signal"]), ("community", "override"))
        self.assertFalse([c for c in lk.calls if c[0] != "name"])

    def test_the_home_key(self):
        a = gather.affiliation_config("BigCo", home_orgs=["parent"],
                                      email_domains=["parent.example"])
        b = gather.affiliation_config("parent", home_orgs=["BigCo"])
        self.assertEqual(gather.affiliation_home(a), gather.affiliation_home(b))
        self.assertEqual(gather.affiliation_home(a), "bigco,parent|bigco,parent")
        self.assertNotEqual(gather.affiliation_home(a), gather.affiliation_home(
            gather.affiliation_config("BigCo", home_names=["Contoso"])))

    def test_the_registry_accepts_home_and_keeps_version_1(self):
        reg = {"format": people.FORMAT, "version": 1, "people": {
            "x": {"login": "x", "affiliation": "community", "home": "o|o"}}}
        self.assertEqual(people.parse_registry(people.dumps(reg))["people"]["x"]["home"],
                         "o|o")
        reg["people"]["x"]["home"] = 7
        with self.assertRaises(people.RegistryError):
            people.parse_registry(people.dumps(reg))
        self.assertEqual(people.VERSION, 1)


def _month(frm, to, newcomers=None, affiliations=None):
    b = {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
         "prs": [], "issues": [], "commits": []}
    if newcomers is not None:
        b["newcomers"] = newcomers
    if affiliations is not None:
        b["affiliations"] = affiliations
    return b


def _view(store, frm, to):
    conn = graphstore.open_store_readonly(store)
    try:
        return extract.extract(conn, "o", "r", frm, to, warn=lambda _m: None,
                               as_of_end=True)
    finally:
        conn.close()


class TestRollUps(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self._tmp.name, "q.db")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def newcomers(frm, ret=(), reg=()):
        return {"collected": True, "before": frm, "candidates": 3, "new": [],
                "returning": [{"login": x, "first": "pr", "number": 2, "url": "u",
                               "created_at": frm + "T01:00:00Z",
                               "last_seen_before": "2026-01-20T00:00:00Z",
                               "gap_days": 130, "gap_bucket": "over_3_months"}
                              for x in ret],
                "regular": [{"login": x, "last_seen_before": "x", "gap_days": 3}
                            for x in reg],
                "gap_thresholds_days": [90, 180, 270],
                "unknown": [], "not_checked": []}

    def test_regular_in_an_earlier_window_stays_regular(self):
        """Finding 4: regular in January, returning in June after a gap, is
        regular to the January-June view (active at its start)."""
        fold_into(self.store,
                  _month("2026-01-01", "2026-01-31",
                         newcomers=self.newcomers("2026-01-01", reg=["pat"])),
                  _month("2026-06-01", "2026-06-30",
                         newcomers=self.newcomers("2026-06-01", ret=["pat", "lou"])))
        nc = _view(self.store, "2026-01-01", "2026-06-30")["newcomers"]
        self.assertEqual([e["login"] for e in nc["regular"]], ["pat"])
        self.assertEqual([e["login"] for e in nc["returning"]], ["lou"])

    @staticmethod
    def affiliations(people_map, not_checked=()):
        return {"collected": True, "home_orgs": ["o"], "home_names": ["o"],
                "home_email_domains": [], "people": people_map,
                "unknown": sorted(k for k, e in people_map.items()
                                  if e["affiliation"] == "unknown"),
                "not_checked": sorted(not_checked)}

    def test_not_checked_people_stay_not_checked(self):
        """Finding 5: with the registry a person left unchecked is in both
        `people` (signal not_checked) and `not_checked`; the roll-up must
        keep them not_checked unless a later window decided them."""
        nc_entry = {"affiliation": "unknown", "signal": "not_checked"}
        fold_into(self.store,
                  _month("2026-08-01", "2026-08-31", affiliations=self.affiliations(
                      {"pat": dict(nc_entry), "lou": dict(nc_entry)},
                      ["lou", "pat"])),
                  _month("2026-09-01", "2026-09-30", affiliations=self.affiliations(
                      {"lou": {"affiliation": "community", "signal": "profile_company",
                               "strength": "weak"}})))
        for frm, to in (("2026-08-01", "2026-09-30"), ("2026-08-01", "2026-08-15")):
            aff = _view(self.store, frm, to)["affiliations"]
            self.assertIn("pat", aff["not_checked"], (frm, to))
            self.assertIn("pat", aff["unknown"], (frm, to))
        roll = _view(self.store, "2026-08-01", "2026-09-30")["affiliations"]
        self.assertNotIn("lou", roll["not_checked"])     # decided later
        self.assertEqual(roll["people"]["lou"]["affiliation"], "community")

    def test_label_from_other_home_settings_is_not_carried_over(self):
        """A label decided against a window's home settings must not appear
        under the latest window's different settings in a roll-up."""
        sep = self.affiliations(
            {"pat": {"affiliation": "maintainer_org", "signal": "public_membership",
                     "name": "Pat P", "maintainer": True,
                     "maintainer_signal": "merged_others_pr",
                     "maintainer_status": "active"},
             "ops": {"affiliation": "community", "signal": "override"},
             "lou": {"affiliation": "maintainer_org", "signal": "public_membership"}})
        sep["home_orgs"] = ["o", "bigco"]
        octo = self.affiliations(
            {"lou": {"affiliation": "community", "signal": "profile_company",
                     "strength": "weak"}})
        octo["home_orgs"] = ["o", "otherco"]
        fold_into(self.store,
                  _month("2026-09-01", "2026-09-30", affiliations=sep),
                  _month("2026-10-01", "2026-10-31", affiliations=octo))
        aff = _view(self.store, "2026-09-01", "2026-10-31")["affiliations"]
        self.assertEqual(aff["home_orgs"], ["o", "otherco"])
        self.assertEqual(aff["people"]["pat"],
                         {"affiliation": "unknown", "signal": "not_checked",
                          "name": "Pat P", "maintainer": True,
                          "maintainer_signal": "merged_others_pr",
                          "maintainer_status": "active"})
        self.assertIn("pat", aff["not_checked"])
        self.assertIn("pat", aff["unknown"])
        self.assertEqual(aff["people"]["ops"]["affiliation"], "community")
        self.assertEqual(aff["people"]["lou"]["affiliation"], "community")
        self.assertNotIn("lou", aff["not_checked"])

    def test_same_home_settings_in_another_order_carry_over(self):
        sep = self.affiliations(
            {"pat": {"affiliation": "maintainer_org", "signal": "public_membership"}})
        sep["home_orgs"] = ["O", "bigco"]
        octo = self.affiliations({})
        octo["home_orgs"] = ["bigco", "o"]
        fold_into(self.store,
                  _month("2026-09-01", "2026-09-30", affiliations=sep),
                  _month("2026-10-01", "2026-10-31", affiliations=octo))
        aff = _view(self.store, "2026-09-01", "2026-10-31")["affiliations"]
        self.assertEqual(aff["people"]["pat"]["affiliation"], "maintainer_org")


class TestOperatorUnknownWinsInProjectMerge(unittest.TestCase):
    """An operator's explicit `unknown` is not undone by another member's
    automatically derived label."""

    def test_override_unknown_in_either_member_order(self):
        op = {"affiliation": "unknown", "affiliation_signal": "override"}
        auto = {"affiliation": "maintainer_org",
                "affiliation_signal": "public_membership"}
        for first, second in ((op, auto), (auto, op)):
            members = [{"repo": r, "bundle": {"people": {"x": dict(p)}, "prs": []}}
                       for r, p in (("o/a", first), ("o/b", second))]
            got = digest._merge_people(members)["x"]
            self.assertEqual((got["affiliation"], got["affiliation_signal"]),
                             ("unknown", "override"))


if __name__ == "__main__":
    unittest.main()
