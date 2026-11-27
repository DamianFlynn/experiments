"""The people registry's activity profile (issue #97): per repository and
gathered window one contribution record per person, with every total, rate,
trend and lifecycle status derived from the set of windows (so nothing is
counted twice), the coverage honesty rule (as observed since the coverage
start, a true first only from the newcomer search), the maintainer and
contributor lifecycle, cross-project context, older entries read as they are, the
owner's commands and the registry context `link.py --people` adds to a view.

No network: records are built here; the gather test stubs the GitHub API.
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import people  # noqa: E402
import validate  # noqa: E402
from test_late_month_gather import cross_check  # noqa: E402
from test_people_layer import _window_with_facts  # noqa: E402
from test_people_registry import NamedProfiles, reg_of  # noqa: E402

SLUG = "o/r"
OTHER = "o/other"
LIMITS = people.thresholds({})


def pr(n, author, created, merged=None, merged_by=None, reviews=()):
    return {"number": n, "author": author, "created_at": created + "T10:00:00Z",
            "merged": merged is not None,
            "merged_at": merged + "T12:00:00Z" if merged else None,
            "merged_by": merged_by,
            "reviews": [{"author": a, "state": s, "submitted_at": d + "T11:00:00Z"}
                        for a, s, d in reviews]}


def commit(sha, author, day, resolved=True):
    return {"sha": sha * 40 if len(sha) == 1 else sha, "author": author,
            "committed_at": day + "T09:00:00Z", "login_resolved": resolved}


def month_records(month, n=1, author="dev", lead="lead"):
    """n merged PRs of `author` in `month`, merged by `lead` after an
    approval by `rev`; one commit each."""
    prs, commits = [], []
    for i in range(n):
        day = "{}-{:02d}".format(month, 10 + i)
        num = int(month.replace("-", "")) * 10 + i
        prs.append(pr(num, author, day, merged=day, merged_by=lead,
                      reviews=[("rev", "approved", day)]))
        commits.append(commit("{:040x}".format(num), author, day))
    return prs, [], commits


def fold_month(act, month, slug=SLUG, n=1, **kw):
    frm, to = people._month_bounds(month)
    prs, issues, commits = month_records(month, n, **kw)
    contrib, stats = people.window_contributions(prs, issues, commits, frm, to)
    return people.fold_window(act, slug, frm, to, contrib, stats)


def fold_people(act, month, authors, slug=SLUG):
    """One monthly window in which each of `authors` has one merged PR."""
    frm, to = people._month_bounds(month)
    prs, commits = [], []
    for a in authors:
        p, _i, c = month_records(month, author=a)
        prs += p
        commits += c
    contrib, stats = people.window_contributions(prs, (), commits, frm, to)
    return people.fold_window(act, slug, frm, to, contrib, stats)


class TestWindowContributions(unittest.TestCase):

    def test_counts_firsts_lasts_and_scope(self):
        prs = [pr(1, "dev", "2026-09-02", merged="2026-09-05", merged_by="lead",
                  reviews=[("rev", "commented", "2026-09-03"),
                           ("rev", "approved", "2026-09-04"),
                           ("rev", "approved", "2026-09-04"),
                           ("dev", "commented", "2026-09-04")]),     # self: no
               pr(2, "dev", "2026-08-20", merged="2026-09-10", merged_by="dev"),
               pr(3, "dev", "2026-09-28", merged="2026-10-02", merged_by="lead"),
               pr(4, "bot[bot]", "2026-09-03", merged="2026-09-04", merged_by="lead")]
        prs[0]["comments_list"] = [{"author": "fan", "created_at": "2026-09-06T00:00:00Z"}]
        issues = [{"number": 9, "author": "fan", "created_at": "2026-09-07T00:00:00Z"}]
        commits = [commit("a", "dev", "2026-09-05"), commit("b", "Some Name", "2026-09-06",
                                                             resolved=False)]
        got, stats = people.window_contributions(prs, issues, commits,
                                                 "2026-09-01", "2026-09-30")
        self.assertEqual(stats, {"people": 4, "unresolved_commit_authors": 1})
        self.assertNotIn("bot[bot]", got)
        self.assertNotIn("Some Name", got)
        dev = got["dev"]
        self.assertEqual(dev["months"], {"2026-09": {
            "prs_opened": 2, "prs_merged": 2, "merges": 1, "commits": 1}})
        # #2 opened in August, #3 merged in October: other windows' events
        self.assertEqual(dev["first"]["pr_opened"], ["2026-09-02", 1])
        self.assertEqual(dev["last"]["pr_opened"], ["2026-09-28", 3])
        self.assertEqual(dev["first"]["commit"], ["2026-09-05", "a" * 40])
        # seen in the records before the window: noted, never counted
        self.assertEqual(dev["seen_before"], "2026-08-20")
        lead = got["lead"]["months"]["2026-09"]
        self.assertEqual((lead["merges"], lead["merges_others"]), (2, 2))  # #1, #4
        rev = got["rev"]["months"]["2026-09"]
        self.assertEqual((rev["reviews"], rev["approvals"], rev["approvals_merged"]),
                         (3, 2, 1))
        self.assertEqual(got["fan"]["first"], {"comment": "2026-09-06",
                                               "issue": ["2026-09-07", 9]})

    def test_newcomer_search_marks_first_ever(self):
        prs = [pr(5, "newbie", "2026-09-03")]
        nc = {"collected": True, "before": "2026-09-01",
              "new": [{"login": "newbie", "first": "pr", "number": 5}]}
        got, _ = people.window_contributions(prs, (), (), "2026-09-01", "2026-09-30", nc)
        self.assertEqual(got["newbie"]["first_ever"], "2026-09-01")


class TestFoldingIsIdempotent(unittest.TestCase):

    def test_same_window_twice_and_any_order(self):
        a = fold_month(fold_month(None, "2026-08"), "2026-09")
        b = fold_month(fold_month(fold_month(None, "2026-09"), "2026-08"), "2026-09")
        self.assertEqual(a, b)
        prof = people.repo_profile(a, "dev", SLUG)
        self.assertEqual(prof["totals"]["prs_merged"], 2)

    def test_regathering_replaces_the_window(self):
        act = fold_month(None, "2026-09", n=3)
        act = fold_month(act, "2026-09", n=1)       # the window gathered again
        self.assertEqual(people.repo_profile(act, "dev", SLUG)["totals"]["prs_merged"], 1)
        # a person absent from the new gather of that window loses its record
        act = fold_month(act, "2026-09", n=1, author="other")
        self.assertIsNone(people.repo_profile(act, "dev", SLUG))
        self.assertNotIn("dev", act["people"])

    def test_overlapping_windows_are_never_summed(self):
        act = None
        for m in ("2026-08", "2026-09", "2026-10"):
            act = fold_month(act, m)
        prs, i, c = [], [], []
        for m in ("2026-08", "2026-09", "2026-10"):
            p, ii, cc = month_records(m)
            prs += p
            c += cc
        contrib, stats = people.window_contributions(prs, i, c, "2026-08-01", "2026-10-31")
        act = people.fold_window(act, SLUG, "2026-08-01", "2026-10-31", contrib, stats)
        prof = people.repo_profile(act, "dev", SLUG)
        self.assertEqual(prof["totals"]["prs_merged"], 3)
        self.assertEqual(prof["by_month"]["2026-09"]["prs_merged"], 1)
        # two half-month windows inside one month are disjoint: both count
        act = None
        for frm, to, day in (("2026-09-01", "2026-09-15", "2026-09-10"),
                             ("2026-09-16", "2026-09-30", "2026-09-20")):
            contrib, stats = people.window_contributions(
                [pr(1 if day < "2026-09-15" else 2, "dev", day)], (), (), frm, to)
            act = people.fold_window(act, SLUG, frm, to, contrib, stats)
        self.assertEqual(people.repo_profile(act, "dev", SLUG)["totals"]["prs_opened"], 2)

    def test_old_windows_fold_into_before(self):
        act = None
        months = [people._month_shift("2023-01", i) for i in range(40)]
        for m in months:
            act = fold_month(act, m)
        recs = act["people"]["dev"][SLUG]
        self.assertEqual(len([k for k in recs if k != people.BEFORE]), 36)
        self.assertEqual(recs[people.BEFORE]["months"], {"before": {
            "prs_opened": 4, "prs_merged": 4, "commits": 4}})
        self.assertEqual(act["repos"][SLUG]["before"],
                         {"from": "2023-01-01", "to": "2023-04-30", "windows": 4})
        prof = people.repo_profile(act, "dev", SLUG)
        self.assertEqual(prof["totals"]["prs_merged"], 40)
        self.assertEqual(prof["first"]["pr_opened"]["date"], "2023-01-10")
        self.assertEqual(prof["observed_since"], "2023-01-01")
        # a folded window gathered again is skipped (it would count twice)
        self.assertEqual(fold_month(act, "2023-02"), act)
        people.check_activity(act)


class TestCoverageHonesty(unittest.TestCase):

    def test_as_observed_unless_the_search_says_first_ever(self):
        act = fold_month(None, "2026-09")
        prof = people.repo_profile(act, "dev", SLUG)
        self.assertEqual(prof["observed_since"], "2026-09-01")
        self.assertNotIn("first_pr_ever", prof)
        nc = {"collected": True, "before": "2026-10-01",
              "new": [{"login": "newbie", "first": "pr", "number": 7}]}
        contrib, stats = people.window_contributions(
            [pr(7, "newbie", "2026-10-04")], (), (), "2026-10-01", "2026-10-31", nc)
        act = people.fold_window(act, SLUG, "2026-10-01", "2026-10-31", contrib, stats)
        self.assertTrue(people.repo_profile(act, "newbie", SLUG)["first_pr_ever"])

    def test_seen_only_before_the_coverage(self):
        old = pr(3, "early", "2025-11-12")
        old["comments_list"] = [{"author": "early", "created_at": "2025-11-13T00:00:00Z"}]
        prs, _i, commits = month_records("2026-09")
        contrib, stats = people.window_contributions(prs + [old], (), commits,
                                                     "2026-09-01", "2026-09-30")
        self.assertEqual(contrib["early"], {"months": {}, "seen_before": "2025-11-12"})
        self.assertEqual(stats["people"], 3)                # dev, lead, rev
        act = people.fold_window(None, SLUG, "2026-09-01", "2026-09-30", contrib, stats)
        # one line per person and repository, not a record per window
        self.assertNotIn("early", act["people"])
        self.assertEqual(act["seen_before"], {"early": {SLUG: "2025-11-12"}})
        # once a gathered window covers that day, the sighting is not shown
        act2 = fold_month(act, "2025-11", author="x")
        self.assertIsNone(people._seen_before(act2, "early", SLUG,
                                              people.coverage(act2, SLUG)))
        prof = people.repo_profile(act, "early", SLUG)
        self.assertEqual((prof["seen_before"], prof["totals"]), ("2025-11-12", {}))
        ctx = people.writer_context(reg_of({"login": "early"}), act, "early", SLUG)
        self.assertEqual(ctx["here"]["active_since"], "2025-11-12")
        self.assertIn("not counted", ctx["here"]["active_since_basis"])
        people.check_activity(act)

    def test_gaps_are_reported(self):
        act = fold_month(fold_month(None, "2026-01"), "2026-06")
        cov = people.coverage(act, SLUG)
        self.assertEqual(cov["gaps"], [["2026-02-01", "2026-05-31"]])
        self.assertEqual(cov["months"], ["2026-01", "2026-06"])


class TestRatesTrendLifecycle(unittest.TestCase):

    def test_rates_and_trend(self):
        act = None
        for m, n in (("2026-05", 1), ("2026-06", 1), ("2026-07", 1),
                     ("2026-08", 4), ("2026-09", 4), ("2026-10", 4)):
            act = fold_month(act, m, n=n)
        prof = people.repo_profile(act, "dev", SLUG)
        self.assertEqual(prof["rates"]["3"]["prs_merged"], 4.0)
        self.assertEqual(prof["rates"]["6"]["prs_merged"], 2.5)
        self.assertEqual(prof["rates"]["12"]["months"], 6)       # what was gathered
        self.assertEqual(prof["trend"]["value"], "rising")
        self.assertIn("last 3 vs previous 3", prof["trend"]["basis"])
        rev = people.repo_profile(act, "rev", SLUG)
        self.assertEqual(rev["trend"]["value"], "rising")
        one = people.repo_profile(fold_month(None, "2026-10"), "dev", SLUG)
        self.assertEqual(one["trend"]["value"], "insufficient")

    def test_contributor_lifecycle(self):
        act = None
        for i in range(12):
            m = people._month_shift("2025-11", i)
            act = fold_people(act, m, ["steady", "gone"] if m == "2025-11" else ["steady"])
        self.assertEqual(people.repo_profile(act, "steady", SLUG)["status"], "active")
        gone = people.repo_profile(act, "gone", SLUG)
        self.assertEqual(gone["status"], "lapsed")
        self.assertIn("day(s) gathered since", gone["status_basis"])
        # returned: active again after six or more gathered months without
        act = fold_people(act, "2026-10", ["steady", "gone"])
        back = people.repo_profile(act, "gone", SLUG)
        self.assertEqual(back["status"], "returned")
        self.assertIn("back after 10 gathered month(s)", back["status_basis"])
        # unknown: absent, but the repository was hardly gathered since
        sparse = fold_people(fold_people(None, "2025-01", ["x"]), "2026-01", ["y"])
        self.assertEqual(people.repo_profile(sparse, "x", SLUG)["status"], "unknown")

    def test_maintainer_lifecycle(self):
        act = None
        for i in range(16):
            act = fold_month(act, people._month_shift("2025-07", i), author="dev",
                             lead="lead")
        entry = {"login": "old", "maintainer_of": {SLUG: {
            "maintainer": True, "signal": "merged_others_pr", "strength": "strong",
            "first_evidence": "2025-01-05", "last_evidence": "2025-07-03"}}}
        st = people.maintainer_status(entry, SLUG, act, limits=LIMITS)
        self.assertEqual(st["status"], "lapsed")
        self.assertTrue(st["maintainer"])                  # evidence is kept
        self.assertEqual(st["first_evidence"], "2025-01-05")
        tight = people.thresholds({}, active_days=30, former_days=60)
        st = people.maintainer_status(entry, SLUG, act, limits=tight)
        self.assertEqual((st["status"], st["maintainer"]), ("former", False))
        lead = people.maintainer_status({"login": "lead", "maintainer_of": {SLUG: {
            "maintainer": True, "signal": "merged_others_pr"}}}, SLUG, act,
            limits=LIMITS)
        self.assertEqual(lead["status"], "active")          # from the merges
        self.assertEqual(lead["merges_others"], 16)
        self.assertEqual(people.maintainer_status(
            {"login": "x", "maintainer_of": {SLUG: "former"}}, SLUG, act)["status"],
            "former")
        self.assertEqual(people.maintainer_status(
            {"login": "x", "maintainer_of": {SLUG: True}}, SLUG, act)["status"], "active")
        self.assertIsNone(people.maintainer_status(
            {"login": "x", "maintainer_of": {SLUG: False}}, SLUG, act))
        # as of a day before the evidence: not yet a maintainer
        self.assertIsNone(people.maintainer_status(entry, SLUG, act, as_of="2024-12-31"))

    def test_owner_settings_are_never_overwritten(self):
        ev = {"maintainer": {SLUG: {"signal": "merged_others_pr",
                                    "first_evidence": "2026-09-01",
                                    "last_evidence": "2026-09-03"}}}
        for mine in ("former", True, False):
            e = people.apply_patch({"login": "a", "maintainer_of": {SLUG: mine}}, ev, "a")
            self.assertEqual(e["maintainer_of"][SLUG], mine)
            people.check_entry("a", e)
        e = people.apply_patch(None, ev, "a")
        e = people.apply_patch(e, {"maintainer": {SLUG: {
            "signal": "approving_reviewer", "first_evidence": "2026-01-02",
            "last_evidence": "2026-10-30"}}}, "a")
        self.assertEqual(e["maintainer_of"][SLUG], {
            "maintainer": True, "signal": "merged_others_pr", "strength": "strong",
            "first_evidence": "2026-01-02", "last_evidence": "2026-10-30"})

    def test_thresholds_are_checked(self):
        self.assertEqual(people.thresholds({}), {"active_days": 183, "former_days": 548})
        self.assertEqual(people.thresholds({"ACTIVITY_PEOPLE_ACTIVE_DAYS": "90"})
                         ["active_days"], 90)
        for env in ({"ACTIVITY_PEOPLE_ACTIVE_DAYS": "x"},
                    {"ACTIVITY_PEOPLE_FORMER_DAYS": "0"},
                    {"ACTIVITY_PEOPLE_ACTIVE_DAYS": "400",
                     "ACTIVITY_PEOPLE_FORMER_DAYS": "300"}):
            with self.assertRaises(ValueError):
                people.thresholds(env)


class TestCrossProject(unittest.TestCase):

    def setUp(self):
        act = None
        for m in ("2026-08", "2026-09", "2026-10"):
            act = fold_month(act, m, author="dev", lead="lead")
            act = fold_month(act, m, slug=OTHER, author="lead", lead="dev")
        self.act = act
        self.reg = reg_of(
            {"login": "dev", "maintainer_of": {OTHER: {
                "maintainer": True, "signal": "merged_others_pr",
                "last_evidence": "2026-10-10"}}},
            {"login": "lead", "maintainer_of": {SLUG: {
                "maintainer": True, "signal": "merged_others_pr",
                "last_evidence": "2026-10-10"}}},
            {"login": "rev", "do_not_name": True})

    def test_summary_across_repositories(self):
        cp = people.cross_project(self.reg, self.act, "dev")
        self.assertEqual([(r["repo"], r["role"]) for r in cp["repos"]],
                         [(OTHER, "maintainer"), (SLUG, "contributor")])
        self.assertEqual(cp["maintains"], [OTHER])
        self.assertEqual(cp["first_anywhere"]["date"], "2026-08-10")

    def test_writer_context_and_as_of(self):
        ctx = people.writer_context(self.reg, self.act, "dev", SLUG)
        self.assertEqual(ctx["also_maintains"], [{"repo": OTHER, "status": "active"}])
        self.assertEqual(ctx["here"]["prs_merged_last_6_months"], 3)
        self.assertEqual(ctx["here"]["active_since"], "2026-08-10")
        self.assertFalse(ctx["here"]["active_since_is_first_ever"])
        old = people.writer_context(self.reg, self.act, "dev", SLUG, as_of="2026-08-31")
        self.assertEqual(old["here"]["prs_merged_last_6_months"], 1)
        self.assertIsNone(people.writer_context(self.reg, self.act, "rev", SLUG))

    def test_streams_roll_up(self):
        act = self.act
        for slug in ("o/m1", "o/m2"):
            frm, to = people._month_bounds("2026-10")
            c, s = people.window_contributions(*month_records("2026-10", author="dev"),
                                               frm, to)
            act = people.fold_window(act, slug, frm, to, c, s, stream="avm-tf")
        cp = people.cross_project(self.reg, act, "dev")
        self.assertEqual(cp["streams"]["avm-tf"]["repos"], 2)
        self.assertEqual(cp["streams"]["avm-tf"]["totals"]["prs_merged"], 2)


class TmpDir(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.path = os.path.join(self.tmp, "people.json")

    def tearDown(self):
        self._tmp.cleanup()


class TestFiles(TmpDir):

    V1 = {"format": people.FORMAT, "version": 1, "people": {
        "amy": {"login": "amy", "name": "Amy", "tags": ["mvp"], "note": "hi",
                "source": "operator", "maintainer_of": {SLUG: True},
                "first_seen": {"date": "2025-01-01", "repo": SLUG},
                "last_seen": {"date": "2026-01-01", "repo": SLUG}},
        "bo": {"login": "bo", "maintainer_of": {SLUG: {
            "maintainer": True, "signal": "merged_others_pr", "strength": "strong",
            "last_evidence": "2026-01-01"}}, "source": "lookup"}}}

    def test_an_old_shape_file_loads_and_is_rewritten_in_the_new_shape(self):
        # written before the activity profile: maintainer evidence without
        # first_evidence; still version 1, read as it is
        text = json.dumps(self.V1, indent=1)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertEqual(people.VERSION, 1)
        reg = people.load_registry(self.path)
        self.assertEqual(reg["people"], self.V1["people"])       # nothing lost
        run = people.Run(self.path, now="2026-11-27")
        run.patch("bo", maintainer={SLUG: {"signal": "merged_others_pr",
                                           "first_evidence": "2026-09-01",
                                           "last_evidence": "2026-09-03"}})
        run.patch("amy", maintainer={SLUG: {"signal": "merged_own_pr",
                                            "last_evidence": "2026-09-03"}})
        self.assertTrue(run.save())
        reg = people.load_registry(self.path)
        self.assertEqual(reg["version"], 1)
        self.assertEqual(reg["people"]["amy"], self.V1["people"]["amy"])  # owner's
        self.assertEqual(reg["people"]["bo"]["maintainer_of"][SLUG], {
            "maintainer": True, "signal": "merged_others_pr", "strength": "strong",
            "first_evidence": "2026-09-01", "last_evidence": "2026-09-03"})
        # idempotent: the same evidence again writes nothing; no side file
        run.patch("bo", maintainer={SLUG: {"signal": "merged_others_pr",
                                           "first_evidence": "2026-09-01",
                                           "last_evidence": "2026-09-03"}})
        self.assertFalse(run.save())
        self.assertEqual(sorted(f for f in os.listdir(self.tmp)
                                if not f.endswith(".lock")), ["people.json"])

    def test_activity_text_is_stable_and_compact(self):
        act = fold_month(fold_month(None, "2026-09"), "2026-10")
        text = people.dumps_activity(act)
        self.assertEqual(json.loads(text), act)
        self.assertEqual(people.dumps_activity(json.loads(text)), text)
        self.assertIn('"2026-09": {"commits": 1, "prs_merged": 1, "prs_opened": 1}', text)
        self.assertIn('"pr_opened": ["2026-09-10", 2026090]', text)
        path = people.activity_path(self.path)
        self.assertTrue(people.write_activity(path, act))
        self.assertFalse(people.write_activity(path, json.loads(text)))
        self.assertEqual(people.activity_path("/w/team.json"), "/w/team-activity.json")

    def test_activity_file_is_checked(self):
        act = fold_month(None, "2026-09")
        people.check_activity(act)
        bad = []
        b = copy.deepcopy(act)
        b["people"]["dev"][SLUG]["2026-09-01..2026-09-30"]["months"]["2026-10"] = {"commits": 1}
        bad.append((b, "not a month of the window"))
        b = copy.deepcopy(act)
        b["people"]["dev"][SLUG]["2026-08-01..2026-08-31"] = {"months": {}}
        bad.append((b, "not in the repository's coverage"))
        b = copy.deepcopy(act)
        b["people"]["dev"][SLUG]["2026-09-01..2026-09-30"]["company"] = "x"
        bad.append((b, "must be {months"))
        b = copy.deepcopy(act)
        b["people"]["dev"][SLUG]["2026-09-01..2026-09-30"]["months"]["2026-09"]["commits"] = 0
        bad.append((b, "positive whole numbers"))
        for raw, want in bad:
            with self.assertRaises(people.RegistryError, msg=want) as cm:
                people.check_activity(raw, "act.json")
            self.assertIn(want, str(cm.exception))
            self.assertIn("people['dev']['o/r']", str(cm.exception))

    def test_commands_seed_show_summary_check(self):
        act = None
        for m in ("2026-09", "2026-10"):
            act = fold_month(act, m)
        people.write_registry(self.path, reg_of({"login": "dev", "source": "lookup"}))
        people.write_activity(people.activity_path(self.path), act)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(people.main(["check", "--people", self.path]), 0)
        self.assertIn("'lead' has activity but no entry", out.getvalue())
        self.assertIn("OK: 0 error(s)", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            people.main(["show", "dev", "--people", self.path])
        text = out.getvalue()
        self.assertIn("as observed since 2026-09-01", text)
        self.assertIn("first pr_opened 2026-09-10", text)
        self.assertIn("contributor status: active", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            people.main(["summary", "--people", self.path, "--json"])
        s = json.loads(out.getvalue())
        self.assertEqual(s["busiest"][SLUG][0]["login"], "dev")
        self.assertEqual(s["coverage"][SLUG]["from"], "2026-09-01")
        with open(people.activity_path(self.path), "w", encoding="utf-8") as fh:
            fh.write("{}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(people.main(["check", "--people", self.path]), 1)
        self.assertIn("not a people activity file", out.getvalue())


def _source(reg, act):
    return {"registry": reg, "activity": act, "people_text": people.dumps(reg),
            "activity_text": people.dumps(act), "limits": LIMITS}


class TestLinkPeopleContext(TmpDir):

    def setUp(self):
        super().setUp()
        act = None
        for m in ("2026-08", "2026-09"):
            act = fold_month(act, m, author="dev100", lead="dev7454")
            act = fold_month(act, m, slug=OTHER, author="dev7454", lead="dev100")
        self.act = act
        self.reg = reg_of({"login": "dev100", "maintainer_of": {OTHER: {
                              "maintainer": True, "signal": "merged_others_pr",
                              "last_evidence": "2026-09-10"}}},
                          {"login": "dev7454"},
                          {"login": "dev103", "do_not_name": True})

    def test_context_is_added_only_on_request_and_stamped(self):
        plain = link.enrich(_window_with_facts())
        self.assertNotIn("people_context", plain)
        b = link.enrich(_window_with_facts(), people_source=_source(self.reg, self.act))
        pc = b["people_context"]
        self.assertEqual((pc["kind"], pc["repo"], pc["as_of"]),
                         ("registry_context", SLUG, "2026-10-31"))
        self.assertEqual(len(pc["registry"]["people_sha256"]), 64)
        self.assertEqual(pc["registry"]["latest_gathered"], "2026-09-30")
        dev = pc["people"]["dev100"]
        self.assertEqual(dev["here"]["active_since"], "2026-08-10")
        self.assertEqual(dev["also_maintains"], [{"repo": OTHER, "status": "active"}])
        self.assertNotIn("dev103", pc["people"])                   # do_not_name
        # everything else in the view is what it was without --people
        b.pop("people_context")
        self.assertEqual(json.dumps(b, sort_keys=True), json.dumps(plain, sort_keys=True))

    def test_old_report_keeps_its_context(self):
        src = _source(self.reg, self.act)
        before = link.enrich(_window_with_facts(), people_source=src)["people_context"]
        later = fold_month(self.act, "2026-11", author="dev100")
        after = link.enrich(_window_with_facts(),
                            people_source=_source(self.reg, later))["people_context"]
        self.assertEqual(before["people"], after["people"])
        self.assertNotEqual(before["registry"], after["registry"])

    def test_slice_participants_carry_it(self):
        b = link.enrich(_window_with_facts(), people_source=_source(self.reg, self.act))
        train = next(t for t in b["trains"] if 100 in (t.get("prs") or []))
        part = link.slice_train(b, train["id"])["participants"]
        self.assertEqual(part["dev100"]["registry_context"],
                         b["people_context"]["people"]["dev100"])
        self.assertNotIn("registry_context", part.get("dev103", {}))

    def test_cli_flag(self):
        bundle = os.path.join(self.tmp, "view.json")
        with open(bundle, "w", encoding="utf-8") as fh:
            json.dump(_window_with_facts(), fh)
        people.write_registry(self.path, self.reg)
        people.write_activity(people.activity_path(self.path), self.act)
        with contextlib.redirect_stderr(io.StringIO()):
            link.main([bundle, "--people", self.path])
        with open(bundle, encoding="utf-8") as fh:
            self.assertIn("dev100", json.load(fh)["people_context"]["people"])
        for argv in ([bundle, "--people"], [bundle, "--people", "a", "--people", "b"]):
            with self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()):
                link.main(argv)
            self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            link.main([bundle, "--people", os.path.join(self.tmp, "missing.json")])
        self.assertEqual(cm.exception.code, 2)

    def test_maintainer_summary_by_status(self):
        b = _window_with_facts()
        p = b["affiliations"]["people"]
        p["dev7454"].update(maintainer=True, maintainer_signal="merged_others_pr",
                            maintainer_status="active")
        p["dev100"].update(maintainer=False, maintainer_signal="merged_others_pr",
                           maintainer_status="former")
        m = link.enrich(b)["contributors"]["affiliation"]["maintainers"]
        self.assertEqual(m["by_status"], {"active": ["dev7454"], "former": ["dev100"]})
        self.assertEqual(m["logins"], ["dev7454"])


class TestGatherRecordsTheWindow(NamedProfiles):

    def setUp(self):
        super().setUp()
        self.people_path = os.path.join(self.tmp, "people.json")

    def gather(self, store="s.db"):
        with mock.patch.object(people, "today", lambda: "2026-11-26"):
            return self.gather_into(os.path.join(self.tmp, store), *self.EXTRA,
                                    "--people", self.people_path)

    def test_activity_and_maintainer_status(self):
        bundle, _err = self.gather()
        apath = people.activity_path(self.people_path)
        with open(apath, encoding="utf-8") as fh:
            first = fh.read()
        act = people.parse_activity(first)
        self.assertEqual(act["repos"][SLUG]["windows"]["2026-09-01..2026-09-30"]
                         ["source"], "gather")
        self.assertEqual(act["people"]["maint"][SLUG]["2026-09-01..2026-09-30"]
                         ["months"]["2026-09"]["merges_others"], 1)
        e = bundle["affiliations"]["people"]["maint"]
        self.assertEqual((e["maintainer"], e["maintainer_status"]), (True, "active"))
        self.assertNotIn("maintainer_status", bundle["affiliations"]["people"]["alice"])
        code, out, view_ = cross_check(os.path.join(self.tmp, "s.db"))
        self.assertEqual(code, 0, out)
        self.assertEqual(view_["affiliations"], bundle["affiliations"])
        # the same window gathered again changes nothing in the activity file
        self.gather(store="again.db")
        with open(apath, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first)

    def test_validate_refuses_a_bad_maintainer_status(self):
        value = {"collected": True, "home_orgs": ["o"], "home_names": ["o"],
                 "home_email_domains": [], "unknown": [], "not_checked": [],
                 "people": {"x": {"affiliation": "community", "signal": "none",
                                  "maintainer": True,
                                  "maintainer_signal": "merged_others_pr",
                                  "maintainer_status": "active"}}}
        self.assertIsNone(validate._affiliations_problem(value))
        for bad in ("retired", "former"):
            value["people"]["x"]["maintainer_status"] = bad
            self.assertIn("maintainer_status", validate._affiliations_problem(value))
        value["people"]["x"]["maintainer"] = False          # former is not one
        self.assertIsNone(validate._affiliations_problem(value))


if __name__ == "__main__":
    unittest.main()
