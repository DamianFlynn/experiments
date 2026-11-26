"""Issue #88 — people in a multi-repo project.

Person nodes are project-scoped (`{project}#person-<login>`): one node per
login for the whole project, its `modules` / `areas` the union over every
member and window folded. Two read-path defects followed from that:

1. Every member view carried every project person. A login that never touched
   member `o/a` appeared in `o/a`'s view as a zero-activity participant, with
   the project-wide union of areas (other repos' area ids among them).
2. The project view (digest) kept the FIRST member's record for a login, so a
   person active only in the second member had zero counts at project level,
   and a person active in both had the first member's counts only. The project
   view had no `halls` at all.

The rules (extract.py, digest.py, BUNDLE.md "People"):

- A member view's people are the logins with a contribution in THAT view
  (author / merger / reviewer / commenter of a view PR, author / commenter of
  a view issue, author of a view commit) — the same enumerator fold uses. Their
  `modules` / `areas` are re-derived from the view's records with this repo's
  code graph (the attribution fold applies), so they name this repo's areas
  touched in this window only. Flags (`is_bot`, `login_resolved`) are the
  stored node's.
- The project view's people are the union of the member views' people. Counts
  are summed over members (each member counts only its own records, so nothing
  is counted twice); `merge_rate` is recomputed from the sums;
  `review_latency_days` is the median of every member's samples; `first_seen`
  is the earliest, `last_active` the latest; `modules` / `areas` are
  `"{repo}::{area}"` (as the project `modules` keys); `is_bot` is OR'd and
  `login_resolved` is false when any member says so. `halls.fame` is rebuilt
  from the merged people.

The stored person nodes and the fold are unchanged, so the trust check passes
in project mode and in single-member mode, in either fold order.

Offline: stores are built in memory via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402

PROJECT = "proj"
A, B = "o/a", "o/b"
MEMBERS = {A, B}
JAN = ("2026-01-01", "2026-01-31")
FEB = ("2026-02-01", "2026-02-28")
SPAN = ("2026-01-01", "2026-02-28")

# Both repos have an area called `core`; each has one of its own.
GRAPH_A = {"areas": [{"id": "core", "paths": ["core/main.tf"]},
                     {"id": "mod-x", "paths": ["x/main.tf"]}]}
GRAPH_B = {"areas": [{"id": "core", "paths": ["core/main.tf"]},
                     {"id": "mod-y", "paths": ["y/main.tf"]}]}


def _url(repo, kind, number):
    return "https://github.com/{}/{}/{}".format(repo, kind, number)


def _pr(repo, number, author, created, merged, reviews=(), comments=()):
    """A merged PR; `reviews` is [(login, submitted_at)]."""
    return {"number": number, "title": "feat: change {}".format(number),
            "body": "", "author": author, "labels": [], "state": "closed",
            "merged": True, "merged_at": merged, "merged_by": author,
            "closed_at": merged, "created_at": created, "updated_at": merged,
            "base": "main", "head": "f{}".format(number), "closes": [],
            "crossref_issues": [], "milestone": None,
            "reviewers": [r for r, _ in reviews],
            "reviews": [{"id": number * 100 + i, "author": r,
                         "state": "APPROVED", "submitted_at": at}
                        for i, (r, at) in enumerate(reviews)],
            "comments_list": [{"author": c, "created_at": merged,
                               "body": "lgtm"} for c in comments],
            "review_comments": [], "url": _url(repo, "pull", number)}


def _commit(sha, author, date, number, path):
    return {"sha": sha, "parents": [], "author": author, "date": date,
            "committed_at": date + "T12:00:00Z",
            "message": "feat: change {} (#{})".format(number, number),
            "files": [path], "pr": None}


def _ev(sha, author, date, path, change="modify"):
    return {"commit": sha, "author": author, "date": date, "change": change,
            "path": path}


def _bundle(repo, window, prs, commits, graph):
    owner, name = repo.split("/")
    return {"meta": {"owner": owner, "repo": name, "from": window[0],
                     "to": window[1], "base_branch": "main",
                     "branches": ["main"]},
            "prs": prs, "issues": [], "commits": commits,
            "code_events": [_ev(c["sha"], c["author"], c["date"],
                                c["files"][0]) for c in commits],
            "symbol_events": [], "milestones": [], "releases": [],
            "code_graph": copy.deepcopy(graph)}


H1, H2, H3 = "a1" * 20, "a2" * 20, "a3" * 20     # o/a, January
K1, K2, K3 = "b1" * 20, "b2" * 20, "b3" * 20     # o/b, January
F1 = "f1" * 20                                    # o/a, February


def _jan_a():
    """alice (only here) and hank (also in o/b)."""
    return _bundle(A, JAN, [
        _pr(A, 1, "alice", "2026-01-05T09:00:00Z", "2026-01-10T12:00:00Z",
            reviews=[("hank", "2026-01-06T10:00:00Z")]),          # 1 day
        _pr(A, 2, "hank", "2026-01-03T09:00:00Z", "2026-01-12T12:00:00Z"),
    ], [
        _commit(H1, "alice", "2026-01-09", 1, "core/main.tf"),
        _commit(H2, "hank", "2026-01-04", 2, "x/main.tf"),
    ], GRAPH_A)


def _jan_b():
    """zed, gina and ivy (only here) and hank (also in o/a)."""
    return _bundle(B, JAN, [
        _pr(B, 5, "zed", "2026-01-15T09:00:00Z", "2026-01-22T12:00:00Z",
            reviews=[("hank", "2026-01-20T10:00:00Z")],           # 5 days
            comments=["ivy"]),
        _pr(B, 6, "hank", "2026-01-16T09:00:00Z", "2026-01-25T12:00:00Z"),
        _pr(B, 7, "gina", "2026-01-17T09:00:00Z", "2026-01-24T12:00:00Z",
            reviews=[("hank", "2026-01-26T10:00:00Z")]),          # 9 days
    ], [
        _commit(K1, "zed", "2026-01-21", 5, "y/main.tf"),
        _commit(K2, "hank", "2026-01-18", 6, "core/main.tf"),
        _commit(K3, "gina", "2026-01-23", 7, "y/main.tf"),
    ], GRAPH_B)


def _feb_a():
    """alice again in o/a, touching mod-x this time."""
    return _bundle(A, FEB, [
        _pr(A, 3, "alice", "2026-02-02T09:00:00Z", "2026-02-05T12:00:00Z"),
    ], [_commit(F1, "alice", "2026-02-04", 3, "x/main.tf")], GRAPH_A)


_FOLDS = {"a": (A, _jan_a), "b": (B, _jan_b), "f": (A, _feb_a)}


def _store(order="ab"):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for key in order:
        repo, make = _FOLDS[key]
        gather.fold_bundle(conn, make(), project=PROJECT, repo=repo,
                           members=MEMBERS)
    return conn


def _view(conn, repo, window=JAN):
    return link.enrich(extract.extract(conn, PROJECT, repo, *window,
                                       warn=lambda _m: None))


def _project(conn, window=JAN, repos=(A, B)):
    return digest.build_project_view(conn, PROJECT, list(repos), *window,
                                     warn=lambda _m: None)


def _failing(report_checks):
    return [c for c in report_checks if not c["ok"]]


class MemberViewPeople(unittest.TestCase):
    """Problem 1: a member view holds only the people of that member."""

    def setUp(self):
        self.stores = [_store("ab"), _store("ba")]

    def tearDown(self):
        for c in self.stores:
            c.close()

    def test_member_view_holds_only_its_contributors(self):
        for conn in self.stores:
            self.assertEqual(sorted(_view(conn, A)["people"]), ["alice", "hank"])
            self.assertEqual(sorted(_view(conn, B)["people"]),
                             ["gina", "hank", "ivy", "zed"])

    def test_raw_extract_people_are_scoped(self):
        # The issue's own repro: extract o/a must not list zed.
        for conn in self.stores:
            raw = extract.extract(conn, PROJECT, A, *JAN, warn=lambda _m: None)
            self.assertNotIn("zed", raw["people"])
            self.assertNotIn("gina", raw["people"])

    def test_member_areas_are_this_repos_areas(self):
        # The stored node is the project-wide union ...
        for conn in self.stores:
            stored = extract._materialize_people(conn, PROJECT)
            self.assertEqual(stored["hank"]["areas"], ["core", "mod-x", "mod-y"])
            # ... a member view names only that member's areas the person
            # touched: in o/a hank committed to mod-x and reviewed alice's
            # PR 1 (core); o/b's mod-y is not o/a's.
            a, b = _view(conn, A)["people"], _view(conn, B)["people"]
            self.assertEqual(a["hank"]["areas"], ["core", "mod-x"])
            self.assertEqual(a["hank"]["modules"], ["core", "mod-x"])
            self.assertEqual(b["hank"]["areas"], ["core", "mod-y"])
            self.assertEqual(a["alice"]["areas"], ["core"])
            self.assertEqual(b["ivy"]["areas"], [])

    def test_member_counts_are_this_members(self):
        for conn in self.stores:
            a, b = _view(conn, A)["people"], _view(conn, B)["people"]
            self.assertEqual((a["hank"]["prs_authored"], a["hank"]["prs_reviewed"],
                              a["hank"]["commits_authored"]), (1, 1, 1))
            self.assertEqual((b["hank"]["prs_authored"], b["hank"]["prs_reviewed"],
                              b["hank"]["commits_authored"]), (1, 2, 1))
            self.assertEqual(a["hank"]["review_latency_days"], 1)
            self.assertEqual(b["hank"]["review_latency_days"], 7)

    def test_member_halls_rank_only_member_people(self):
        for conn in self.stores:
            fame = [h["login"] for h in _view(conn, A)["halls"]["fame"]]
            self.assertEqual(fame, ["hank", "alice"])


class ProjectViewPeople(unittest.TestCase):
    """Problem 2: project totals are summed across members."""

    def setUp(self):
        self.stores = [_store("ab"), _store("ba")]

    def tearDown(self):
        for c in self.stores:
            c.close()

    def test_second_member_only_person_has_their_counts(self):
        # The issue's gina repro.
        for conn in self.stores:
            gina = _project(conn)["people"]["gina"]
            self.assertEqual(gina["prs_authored"], 1)
            self.assertEqual(gina["prs_merged"], 1)
            self.assertEqual(gina["commits_authored"], 1)
            self.assertEqual(gina["merge_rate"], 1.0)
            self.assertEqual(gina["first_seen"], "2026-01-17")
            self.assertEqual(gina["areas"], ["o/b::mod-y"])

    def test_person_in_both_members_has_summed_counts(self):
        for conn in self.stores:
            hank = _project(conn)["people"]["hank"]
            self.assertEqual(hank["prs_authored"], 2)
            self.assertEqual(hank["prs_merged"], 2)
            self.assertEqual(hank["merge_rate"], 1.0)
            self.assertEqual(hank["prs_reviewed"], 3)
            self.assertEqual(hank["commits_authored"], 2)
            self.assertEqual(hank["first_seen"], "2026-01-03")
            self.assertEqual(hank["last_active"], "2026-01-26")
            # median of all samples [1, 5, 9], not of the member medians (1, 7)
            self.assertEqual(hank["review_latency_days"], 5)
            self.assertEqual(hank["areas"],
                             ["o/a::core", "o/a::mod-x", "o/b::core",
                              "o/b::mod-y"])
            self.assertEqual(hank["modules"], hank["areas"])
            self.assertFalse(hank["is_bot"])

    def test_nobody_counted_twice(self):
        # Every count summed over the project people equals the same count
        # summed over the member views.
        fields = ("prs_authored", "prs_merged", "prs_reviewed",
                  "commits_authored", "issues_opened")
        for conn in self.stores:
            pv = _project(conn)
            for f in fields:
                members = sum(p[f] for m in pv["members"]
                              for p in m["bundle"]["people"].values())
                self.assertEqual(sum(p[f] for p in pv["people"].values()),
                                 members, f)
            self.assertEqual(sum(p["prs_authored"]
                                 for p in pv["people"].values()), 5)
            self.assertEqual(sum(p["commits_authored"]
                                 for p in pv["people"].values()), 5)

    def test_project_halls_rank_on_merged_figures(self):
        for conn in self.stores:
            fame = _project(conn)["halls"]["fame"]
            self.assertEqual([(h["login"], h["score"]) for h in fame],
                             [("hank", 9), ("alice", 3), ("gina", 3),
                              ("zed", 3)])
            self.assertEqual(fame[0]["areas"],
                             ["o/a::core", "o/a::mod-x", "o/b::core",
                              "o/b::mod-y"])

    def test_alice_is_not_in_o_b(self):
        for conn in self.stores:
            pv = _project(conn)
            self.assertEqual(pv["people"]["alice"]["areas"], ["o/a::core"])
            self.assertNotIn("alice", pv["members"][1]["bundle"]["people"])

    def test_one_member_project_view(self):
        # --repo o/b: only o/b's people, with o/b's figures.
        for conn in self.stores:
            pv = _project(conn, repos=(B,))
            self.assertEqual(sorted(pv["people"]),
                             ["gina", "hank", "ivy", "zed"])
            self.assertEqual(pv["people"]["hank"]["prs_reviewed"], 2)

    def test_project_view_is_fold_order_independent(self):
        a, b = (_project(c) for c in self.stores)
        self.assertEqual(json.dumps(a["people"], sort_keys=True),
                         json.dumps(b["people"], sort_keys=True))
        self.assertEqual(a["halls"], b["halls"])

    def test_digest_cli_people(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.db")
            conn = graphstore.open_store(path)
            graphstore.init_schema(conn)
            for key in "ab":
                repo, make = _FOLDS[key]
                gather.fold_bundle(conn, make(), project=PROJECT, repo=repo,
                                   members=MEMBERS)
            conn.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = digest.main(["--store", path, "--project", PROJECT,
                                  "--from", JAN[0], "--to", JAN[1]])
            self.assertEqual(rc, 0)
            pv = json.loads(out.getvalue())
            self.assertEqual(pv["people"]["gina"]["prs_authored"], 1)
            self.assertEqual(pv["halls"]["fame"][0]["login"], "hank")


class MultiWindowMemberPeople(unittest.TestCase):
    """A member holding two windows: a view's areas are its window's."""

    def test_january_view_areas_exclude_february(self):
        for order in ("abf", "fba", "bfa"):
            conn = _store(order)
            jan, feb = _view(conn, A, JAN), _view(conn, A, FEB)
            self.assertEqual(jan["people"]["alice"]["areas"], ["core"])
            self.assertEqual(feb["people"]["alice"]["areas"], ["mod-x"])
            self.assertEqual(sorted(feb["people"]), ["alice"])
            span = _view(conn, A, SPAN)["people"]["alice"]
            self.assertEqual(span["areas"], ["core", "mod-x"])
            pv = _project(conn, FEB)
            self.assertEqual(sorted(pv["people"]), ["alice"])
            self.assertEqual(pv["people"]["alice"]["prs_authored"], 1)
            conn.close()


class TrustCheckStaysGreen(unittest.TestCase):
    """The stored people and the fold are unchanged: validate passes in
    project mode and in single-member mode, in either fold order."""

    def _assert_green(self, conn, repos):
        rep = validate.validate_project(conn, PROJECT, list(repos))
        bad = [c for m in rep["members"] for c in _failing(m["checks"])]
        bad += _failing(rep["project_checks"])
        self.assertTrue(rep["ok"], json.dumps(bad, indent=1, default=str))
        for repo in repos:
            single = validate.validate(conn, project=PROJECT, repo=repo)
            bad = _failing(single.checks)
            self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))

    def test_validate_green_both_orders(self):
        for order in ("ab", "ba"):
            conn = _store(order)
            self._assert_green(conn, (A, B))
            conn.close()

    def test_validate_green_multi_window(self):
        for order in ("abf", "fba"):
            conn = _store(order)
            self._assert_green(conn, (A, B))
            conn.close()

    def test_views_do_not_change_the_store(self):
        conn = _store("ab")
        before = extract._materialize_people(conn, PROJECT)
        _project(conn)
        self.assertEqual(extract._materialize_people(conn, PROJECT), before)
        conn.close()


class MergeHelpers(unittest.TestCase):

    def test_merge_people_sums_and_qualifies(self):
        def rec(**kw):
            base = {"modules": [], "areas": [], "is_bot": False,
                    "prs_authored": 0, "prs_merged": 0, "merge_rate": None,
                    "prs_reviewed": 0, "commits_authored": 0,
                    "issues_opened": 0, "review_latency_days": None,
                    "first_seen": None, "last_active": None,
                    "examples_authored": 0, "docs_authored": 0,
                    "symbols_authored": 0, "authored_then_removed": 0,
                    "stale_owned": 0}
            base.update(kw)
            return base
        members = [
            {"repo": "Az/a", "bundle": {"prs": [], "people": {
                "x": rec(areas=["core"], modules=["core"], prs_authored=2,
                         prs_merged=1, merge_rate=0.5, first_seen="2026-01-05",
                         last_active="2026-01-07")}}},
            {"repo": "Az/b", "bundle": {"prs": [], "people": {
                "x": rec(areas=["core"], modules=["core"], prs_authored=2,
                         prs_merged=2, merge_rate=1.0, first_seen="2026-01-02",
                         last_active="2026-01-06", login_resolved=False)}}},
        ]
        x = digest._merge_people(members)["x"]
        self.assertEqual(x["prs_authored"], 4)
        self.assertEqual(x["merge_rate"], 0.75)
        self.assertEqual((x["first_seen"], x["last_active"]),
                         ("2026-01-02", "2026-01-07"))
        self.assertEqual(x["areas"], ["Az/a::core", "Az/b::core"])
        self.assertIs(x["login_resolved"], False)
        self.assertIsNone(x["review_latency_days"])
        # inputs untouched
        self.assertEqual(members[0]["bundle"]["people"]["x"]["areas"], ["core"])


if __name__ == "__main__":
    unittest.main()
