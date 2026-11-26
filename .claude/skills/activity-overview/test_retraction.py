"""Issue #95 — current-state edges are retracted when their inputs change.

Edges used to be unioned on every fold and never deleted, so a store kept
across months kept every dependency, owner, milestone, iteration and blocker
any earlier gather had seen. The rules (gather.py "current-state edges",
STORE.md "Current-state edges"):

- history edges (authored, reviewed, merged, commented, reported, closes,
  cross_ref, part_of, touches, symbol moves) are only ever added;
- blocks / in_milestone / in_iteration are what the STORED PR/issue records
  and milestone / sprint nodes imply (a record a fold upserts replaces the
  earlier one, and its edges follow);
- depends_on / owns are replayed over every gathered window's facts in window
  order: a source's edges are those of its latest AUTHORITATIVE window (a
  resolved build of the area; the whole-tree structural scan for direct
  edges; a code graph listing the area plus CODEOWNERS / a checkout for
  owns) and of every later window. A window that says nothing about a source
  (a window-scoped directory graph that does not list it, a build that timed
  out, a quiet or failed member) retracts nothing;
- a depends_on stand-in no edge references any more is deleted;
- what a fold retracted is in its summary and the meta ledger
  `retractions:{project}/{repo}`;
- validate compares these edges exactly on a multi-window store once a fold
  since #95 maintained them.

Offline: stores are built in memory via gather.fold_bundle.
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402
import validate  # noqa: E402

JAN = ("2026-01-01", "2026-01-31")
FEB = ("2026-02-01", "2026-02-28")
MAR = ("2026-03-01", "2026-03-31")
URL = "https://github.com/o/r"


def _edge(to, transitive=False, ref=None, provider="terraform"):
    return {"to": to, "kind": "module", "ref": ref or "./" + str(to),
            "version": None, "transitive": transitive, "provider": provider,
            "resolved": to is not None}


def _area(aid, edges=(), status="resolved", paths=None):
    a = {"id": aid, "label": aid.split("/")[-1],
         "paths": list(paths or ["{}/main.tf".format(aid)]),
         "edges": [copy.deepcopy(e) for e in edges]}
    if status is not None:
        a["edges_status"] = status
    return a


def _bundle(window, areas=(), owners=None, issues=(), prs=(), milestones=(),
            sprints=None, scan=False, sha=None, commits=(), repo="r",
            owner="o"):
    cg = {"provider": "directory", "areas": [copy.deepcopy(a) for a in areas]}
    if scan:
        cg["structural_scan"] = True
    b = {"meta": {"owner": owner, "repo": repo, "from": window[0],
                  "to": window[1], "base_branch": "main", "branches": ["main"],
                  "clone_sha": sha},
         "prs": [copy.deepcopy(p) for p in prs],
         "issues": [copy.deepcopy(i) for i in issues],
         "commits": [copy.deepcopy(c) for c in commits],
         "code_events": [], "symbol_events": [],
         "milestones": [copy.deepcopy(m) for m in milestones],
         "releases": [], "code_graph": cg}
    if owners is not None:
        b["code_owners"] = copy.deepcopy(owners)
    if sprints is not None:
        b["sprints"] = copy.deepcopy(sprints)
    return b


def _issue(n, body="", milestone=None, iteration=None, blocks=None,
           updated="2026-01-10T00:00:00Z"):
    i = {"number": n, "title": "issue {}".format(n), "body": body,
         "author": "alice", "state": "open", "created_at": "2026-01-02T00:00:00Z",
         "updated_at": updated, "closed_at": None,
         "url": "{}/issues/{}".format(URL, n), "labels": [],
         "milestone": milestone, "comments_list": [],
         "blocks": list(blocks or [])}
    if iteration is not None:
        i["iteration"] = iteration
    return i


def _out(n):
    return {"number": n, "direction": "out"}


def _in(n):
    return {"number": n, "direction": "in"}


MILESTONES = [{"number": 1, "title": "v1", "state": "open"},
              {"number": 2, "title": "v2", "state": "open"}]
SPRINTS = {"S1": {"title": "Sprint 1", "start": "2026-01-01",
                  "end": "2026-01-14"},
           "S2": {"title": "Sprint 2", "start": "2026-01-15",
                  "end": "2026-02-14"}}


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _fold(conn, *bundles, **kw):
    out = None
    for b in bundles:
        out = gather.fold_bundle(conn, copy.deepcopy(b), **kw)
    return out


def _edges(conn, etype):
    return sorted((r[0], r[1]) for r in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE edge_type=?", (etype,)))


def _all_edges(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT src_id, dst_id, edge_type, data FROM edges"))


def _q(local, repo="r"):
    return graphstore.qualify_id("o", repo, local)


def _person(login):
    return graphstore.qualify_person("o", login)


class _Case(unittest.TestCase):
    def assertClean(self, conn, project="o", repo="r"):
        report = validate.validate(conn, project=project, repo=repo)
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        return report

    def assertNoCurrentStateWarn(self, report):
        es = [c for c in report.checks if c["name"] == "edge_set"][0]
        warns = [d for d in es["details"] if d.get("severity") == "WARN"]
        self.assertEqual(warns, [], json.dumps(warns, indent=1))


# --- (a) a module reference removed between two months ------------------------

A, B, C = "modules/a", "modules/b", "modules/c"


class TestDependsOnRetraction(_Case):
    def test_removed_source_leaves_no_depends_on(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]))
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B))])
        self.assertEqual(spotlight.area_dependents(
            conn, "o", "o/r::" + B)["dependents"], ["o/r::" + A])
        summary = _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertEqual(_edges(conn, "depends_on"), [])
        self.assertEqual(summary["retracted"], {"depends_on": 1})
        # readers: digest.module_edges and spotlight dependents
        self.assertEqual(digest.project_depends_on(conn, "o", ["r"]), [])
        self.assertEqual(spotlight.area_dependents(
            conn, "o", "o/r::" + B)["dependents"], [])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_history_edges_are_untouched(self):
        conn = _store()
        jan = _bundle(JAN, [_area(A, [_edge(B)]), _area(B)],
                      commits=[{"sha": "a" * 40, "author": "alice",
                                "date": "2026-01-05", "message": "x",
                                "files": [A + "/main.tf"]}])
        _fold(conn, jan)
        before = [e for e in _all_edges(conn) if e[2] != "depends_on"]
        _fold(conn, _bundle(FEB, [_area(A, [])]))
        after = [e for e in _all_edges(conn) if e[2] != "depends_on"]
        self.assertEqual(before, after)
        self.assertIn((_q("a" * 40), _q("area-" + A)), _edges(conn, "touches"))

    def test_window_scoped_graph_that_omits_the_area_retracts_nothing(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]),
              _bundle(FEB, [_area(C)]))          # A untouched in February
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B))])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_build_that_did_not_resolve_retracts_nothing(self):
        for status in ("timeout", "failed", "skipped", None):
            conn = _store()
            _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]),
                  _bundle(FEB, [_area(A, [], status=status)]))
            self.assertEqual(_edges(conn, "depends_on"),
                             [(_q("area-" + A), _q("area-" + B))], status)
            self.assertClean(conn)

    def test_structural_scan_retracts_direct_edges_of_unlisted_areas(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]))
        # February's whole-tree scan lists no module A any more (deleted).
        _fold(conn, _bundle(FEB, [_area(B, status=None)], scan=True))
        self.assertEqual(_edges(conn, "depends_on"), [])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_structural_scan_keeps_transitive_edges(self):
        # The scan sees direct `module` refs only: a transitive edge from an
        # earlier resolved build stands until a newer build of the area.
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B), _edge(C, transitive=True)]),
                                  _area(B), _area(C)]))
        _fold(conn, _bundle(FEB, [_area(A, [_edge(B)], status=None)], scan=True))
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B)),
                          (_q("area-" + A), _q("area-" + C))])
        _fold(conn, _bundle(MAR, [_area(A, [_edge(B)])]))      # rebuilt
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B))])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_retraction_is_recorded(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]))
        summary = _fold(conn, _bundle(FEB, [_area(A, [])]))
        ledger = graphstore.get_retractions(conn, "o", "r")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["counts"], {"depends_on": 1})
        self.assertEqual(ledger[0]["edges"],
                         [[_q("area-" + A), _q("area-" + B), "depends_on"]])
        self.assertEqual((ledger[0]["from"], ledger[0]["to"]), FEB)
        line = gather.describe_retractions(summary, "o", "r")
        self.assertIn("retracted 1 stale current-state edge(s) of o/r", line)
        self.assertIn("1 depends_on", line)
        # a re-fold of the same window retracts nothing more and logs nothing
        again = _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertEqual(again["retracted"], {})
        self.assertIsNone(gather.describe_retractions(again, "o", "r"))
        self.assertEqual(len(graphstore.get_retractions(conn, "o", "r")), 1)

    def test_orphaned_standin_is_deleted(self):
        conn = _store()
        # B is referenced but not in the window: a stand-in anchors the edge.
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)])]))
        node = graphstore.get_node(conn, _q("area-" + B))
        self.assertEqual(node["data"]["synthesized"], graphstore.STANDIN_MARK)
        summary = _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertIsNone(graphstore.get_node(conn, _q("area-" + B)))
        self.assertEqual(summary["standins_removed"], [_q("area-" + B)])
        self.assertClean(conn)

    def test_standin_still_referenced_is_kept_and_real_area_never_deleted(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(C, [_edge(B)])]))
        _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertIsNotNone(graphstore.get_node(conn, _q("area-" + B)))
        conn2 = _store()
        _fold(conn2, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)]),
              _bundle(FEB, [_area(A, [])]))
        self.assertIsNotNone(graphstore.get_node(conn2, _q("area-" + B)))


# --- (b) a CODEOWNERS change removes the old owns ------------------------------

class TestOwnsRetraction(_Case):
    def test_codeowners_change_replaces_the_owner(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A)], owners=[
            {"pattern": "/modules/a/", "owners": ["alice"]}]))
        self.assertEqual(_edges(conn, "owns"),
                         [(_person("alice"), _q("area-" + A))])
        summary = _fold(conn, _bundle(FEB, [_area(A)], owners=[
            {"pattern": "/modules/a/", "owners": ["bob"]}]))
        self.assertEqual(_edges(conn, "owns"),
                         [(_person("bob"), _q("area-" + A))])
        self.assertEqual(summary["retracted"], {"owns": 1})
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_codeowners_deleted_from_a_checkout_removes_owners(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A)], owners=[
            {"pattern": "*", "owners": ["alice"]}]))
        _fold(conn, _bundle(FEB, [_area(A)], sha="f" * 40))   # no CODEOWNERS
        self.assertEqual(_edges(conn, "owns"), [])
        self.assertClean(conn)

    def test_no_codeowners_and_no_checkout_says_nothing(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A)], owners=[
            {"pattern": "*", "owners": ["alice"]}]),
              _bundle(FEB, [_area(A)]))
        self.assertEqual(_edges(conn, "owns"),
                         [(_person("alice"), _q("area-" + A))])
        self.assertClean(conn)

    def test_area_not_in_the_new_graph_keeps_its_owners(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A)], owners=[
            {"pattern": "*", "owners": ["alice"]}]),
              _bundle(FEB, [_area(B)], owners=[
                  {"pattern": "*", "owners": ["bob"]}]))
        self.assertEqual(_edges(conn, "owns"),
                         [(_person("alice"), _q("area-" + A)),
                          (_person("bob"), _q("area-" + B))])
        self.assertNoCurrentStateWarn(self.assertClean(conn))


# --- (c) milestone / iteration / blocker changes -------------------------------

class TestItemRelationRetraction(_Case):
    def test_issue_moved_off_a_milestone(self):
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[_issue(5, milestone="v1")],
                            milestones=MILESTONES))
        self.assertEqual(_edges(conn, "in_milestone"),
                         [(_q("issue-5"), _q("milestone-1"))])
        _fold(conn, _bundle(FEB, issues=[_issue(5, milestone="v2",
                                                updated="2026-02-03T00:00:00Z")],
                            milestones=MILESTONES))
        self.assertEqual(_edges(conn, "in_milestone"),
                         [(_q("issue-5"), _q("milestone-2"))])
        _fold(conn, _bundle(MAR, issues=[_issue(5, updated="2026-03-03T00:00:00Z")],
                            milestones=MILESTONES))
        self.assertEqual(_edges(conn, "in_milestone"), [])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_issue_moved_off_an_iteration(self):
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[_issue(5, iteration="S1")],
                            sprints=SPRINTS))
        self.assertEqual(_edges(conn, "in_iteration"),
                         [(_q("issue-5"), _q("sprint-S1"))])
        # February's board no longer lists the sprint (windowed out) and the
        # item has none: the edge goes, the sprint node stays.
        _fold(conn, _bundle(FEB, issues=[_issue(5, updated="2026-02-03T00:00:00Z")],
                            sprints={"S2": SPRINTS["S2"]}))
        self.assertEqual(_edges(conn, "in_iteration"), [])
        self.assertIsNotNone(graphstore.get_node(conn, _q("sprint-S1")))
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_blocker_removed_from_the_text(self):
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[_issue(5, "blocks #6", blocks=[_out(6)]),
                                         _issue(6)]))
        self.assertEqual(_edges(conn, "blocks"), [(_q("issue-5"), _q("issue-6"))])
        # February re-gathers only #5, edited: #6's stored record says nothing.
        _fold(conn, _bundle(FEB, issues=[_issue(5, "unblocked",
                                                updated="2026-02-03T00:00:00Z")]))
        self.assertEqual(_edges(conn, "blocks"), [])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_blocker_still_stated_by_the_other_record_stays(self):
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[_issue(5, "blocks #6", blocks=[_out(6)]),
                                         _issue(6, "blocked by #5",
                                                blocks=[_in(5)])]))
        _fold(conn, _bundle(FEB, issues=[_issue(5, "edited",
                                                updated="2026-02-03T00:00:00Z")]))
        self.assertEqual(_edges(conn, "blocks"), [(_q("issue-5"), _q("issue-6"))])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_blocker_to_an_issue_gathered_later_appears(self):
        jan = _bundle(JAN, issues=[_issue(5, "blocks #9", blocks=[_out(9)])])
        feb = _bundle(FEB, issues=[_issue(9, updated="2026-02-03T00:00:00Z")])
        for order in ((jan, feb), (feb, jan)):
            conn = _store()
            _fold(conn, *order)
            self.assertEqual(_edges(conn, "blocks"),
                             [(_q("issue-5"), _q("issue-9"))])
            self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_pr_milestone_follows_its_record(self):
        pr = {"number": 7, "title": "t", "body": "", "author": "alice",
              "merged": False, "state": "open", "closes": [],
              "crossref_issues": [], "created_at": "2026-01-03T00:00:00Z",
              "url": URL + "/pull/7", "milestone": "v1"}
        conn = _store()
        _fold(conn, _bundle(JAN, prs=[pr], milestones=MILESTONES),
              _bundle(FEB, prs=[dict(pr, milestone=None)], milestones=MILESTONES))
        self.assertEqual(_edges(conn, "in_milestone"), [])
        self.assertClean(conn)


# --- (d) re-folding the same window after an upstream change -------------------

class TestRefoldSameWindow(_Case):
    def _before(self):
        return _bundle(JAN, [_area(A, [_edge(B)]), _area(B)],
                       owners=[{"pattern": "*", "owners": ["alice"]}],
                       issues=[_issue(5, "blocks #6", milestone="v1",
                                      iteration="S1", blocks=[_out(6)]),
                               _issue(6)],
                       milestones=MILESTONES, sprints=SPRINTS)

    def _after(self):
        return _bundle(JAN, [_area(A, []), _area(B)],
                       owners=[{"pattern": "*", "owners": ["bob"]}],
                       issues=[_issue(5, "nothing", milestone="v2"), _issue(6)],
                       milestones=MILESTONES, sprints=SPRINTS)

    def test_refold_equals_a_fresh_store_of_the_new_bundle(self):
        conn = _store()
        _fold(conn, self._before())
        summary = _fold(conn, self._after())
        fresh = _store()
        _fold(fresh, self._after())
        self.assertEqual(_all_edges(conn), _all_edges(fresh))
        self.assertEqual(summary["retracted"], {
            "blocks": 1, "depends_on": 1, "in_iteration": 1,
            "in_milestone": 1, "owns": 2})
        self.assertIsNone(graphstore.get_node(conn, _q("area-" + B))["data"]
                          .get("synthesized"))
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_refold_of_an_unchanged_window_is_a_no_op(self):
        conn = _store()
        _fold(conn, self._before())
        before = _all_edges(conn)
        summary = _fold(conn, self._before())
        self.assertEqual(_all_edges(conn), before)
        self.assertEqual(summary["retracted"], {})
        self.assertEqual(graphstore.get_retractions(conn, "o", "r"), [])


# --- (e) a failed or quiet member retracts nothing ------------------------------

MEMBERS = {"o/app", "o/terraform-azurerm-mod"}
REG = {m: None for m in MEMBERS}
ROOT = "area-main.tf"


def _app(window, refs=True, scan=True):
    edges = [_edge(None, ref="o/mod/azurerm")] if refs else []
    return _bundle(window, [_area("main.tf", edges, status=None,
                                  paths=["main.tf"])],
                   scan=scan, repo="app")


def _mod(window):
    return _bundle(window, [_area("main.tf", [], paths=["main.tf"])],
                   repo="terraform-azurerm-mod", scan=True)


class TestFailedAndQuietMembers(_Case):
    def _fold_member(self, conn, b, repo):
        return gather.fold_bundle(conn, copy.deepcopy(b), project="o", repo=repo,
                                  members=MEMBERS, registry_by_slug=REG)

    def test_failed_fold_rolls_back_and_retracts_nothing(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        before = _all_edges(conn)
        real = gather._maintain_current_state

        def boom(*a, **kw):
            real(*a, **kw)              # retracts inside the transaction...
            raise RuntimeError("member failed after the retraction")
        gather._maintain_current_state = boom
        try:
            with self.assertRaises(RuntimeError):
                self._fold_member(conn, _app(FEB, refs=False), "o/app")
        finally:
            gather._maintain_current_state = real
        self.assertEqual(_all_edges(conn), before)        # ...and rolls back
        self.assertEqual(graphstore.get_retractions(conn, "o", "o/app"), [])
        self.assertNotIn(list(FEB), [[w["from"], w["to"]] for w in
                                     graphstore.get_windows(conn)])

    def test_quiet_member_retracts_nothing(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        before = _all_edges(conn)
        # A quiet window: no activity; its scan sees the same tree.
        summary = self._fold_member(conn, _app(FEB), "o/app")
        self.assertEqual(_all_edges(conn), before)
        self.assertEqual(summary["retracted"], {})
        # A quiet member whose bundle has no code graph at all says nothing.
        empty = _bundle(MAR, repo="app")
        del empty["code_graph"]
        summary = self._fold_member(conn, empty, "o/app")
        self.assertEqual(_all_edges(conn), before)
        self.assertEqual(summary["retracted"], {})

    def test_gather_manifest_failure_retracts_nothing(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        before = _all_edges(conn)
        man = {"project": "o", "from": FEB[0], "to": FEB[1],
               "repos": [{"owner": "o", "repo": "app", "registry": None,
                          "bicep_registry_prefix": None}]}

        def failing_acquire(args, env):
            raise SystemExit("clone failed")
        real = gather.acquire
        gather.acquire = failing_acquire
        try:
            args = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
            import io
            err, sys.stderr = sys.stderr, io.StringIO()
            try:
                failures = gather._gather_manifest(conn, args, man)
            finally:
                sys.stderr = err
        finally:
            gather.acquire = real
        self.assertEqual([f[0] for f in failures], ["o/app"])
        self.assertEqual(_all_edges(conn), before)


# --- (f) a cross-repo edge and its stand-in -----------------------------------

class TestCrossRepo(_Case):
    def _fold_member(self, conn, b, repo, members=MEMBERS):
        return gather.fold_bundle(conn, copy.deepcopy(b), project="o", repo=repo,
                                  members=members,
                                  registry_by_slug=REG if members else None)

    def _xedges(self, conn):
        return [(s, d) for s, d in _edges(conn, "depends_on")
                if s.startswith("o/o/app#") and
                d.startswith("o/o/terraform-azurerm-mod#")]

    def test_removed_registry_ref_retracts_edge_and_standin(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")     # mod never folded
        standin = _q(ROOT, "o/terraform-azurerm-mod")
        self.assertEqual(self._xedges(conn), [(_q(ROOT, "o/app"), standin)])
        self.assertEqual(graphstore.get_node(conn, standin)["data"]["synthesized"],
                         graphstore.STANDIN_MARK)
        summary = self._fold_member(conn, _app(FEB, refs=False), "o/app")
        self.assertEqual(self._xedges(conn), [])
        self.assertIsNone(graphstore.get_node(conn, standin))
        self.assertEqual(summary["standins_removed"], [standin])
        self.assertNotIn("o/terraform-azurerm-mod",
                         graphstore.project_repos(conn, "o"))
        self.assertClean(conn, project="o", repo="o/app")

    def test_real_target_stays_when_the_edge_goes(self):
        conn = _store()
        self._fold_member(conn, _mod(JAN), "o/terraform-azurerm-mod")
        self._fold_member(conn, _app(JAN), "o/app")
        self._fold_member(conn, _app(FEB, refs=False), "o/app")
        self.assertEqual(self._xedges(conn), [])
        node = graphstore.get_node(conn, _q(ROOT, "o/terraform-azurerm-mod"))
        self.assertNotIn("synthesized", node["data"])
        agg = validate.validate_project(conn, "o", sorted(MEMBERS))
        bad = [(m["repo"], c) for m in agg["members"] for c in m["checks"]
               if not c["ok"]] + [c for c in agg["project_checks"] if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))

    def test_kept_cross_repo_edge_passes_the_exact_check(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        self._fold_member(conn, _app(FEB), "o/app")
        self.assertEqual(len(self._xedges(conn)), 1)
        self.assertNoCurrentStateWarn(
            self.assertClean(conn, project="o", repo="o/app"))

    def test_stale_cross_repo_edge_fails_the_exact_check(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        self._fold_member(conn, _app(FEB, refs=False), "o/app")
        graphstore.upsert_node(conn, _q(ROOT, "o/terraform-azurerm-mod"), "o",
                               "o/terraform-azurerm-mod", "structure", None,
                               {"id": "main.tf"})
        graphstore.upsert_edge(conn, _q(ROOT, "o/app"),
                               _q(ROOT, "o/terraform-azurerm-mod"), "depends_on",
                               data={"kind": "module", "ref": "o/mod/azurerm",
                                     "version": None, "transitive": False,
                                     "provider": "terraform", "resolved": True,
                                     "cross_repo": True})
        report = validate.validate(conn, project="o", repo="o/app")
        es = [c for c in report.checks if c["name"] == "edge_set"][0]
        self.assertIs(es["ok"], False, es)

    def test_single_repo_fold_leaves_cross_repo_edges_alone(self):
        conn = _store()
        self._fold_member(conn, _app(JAN), "o/app")
        self._fold_member(conn, _app(FEB, refs=False), "o/app", members=None)
        self.assertEqual(len(self._xedges(conn)), 1)


# --- (g) both fold orders -----------------------------------------------------

class TestFoldOrder(_Case):
    def _windows(self):
        jan = _bundle(JAN, [_area(A, [_edge(B)]), _area(B)],
                      owners=[{"pattern": "*", "owners": ["alice"]}],
                      issues=[_issue(6)])
        feb = _bundle(FEB, [_area(A, [_edge(C)]), _area(C)],
                      owners=[{"pattern": "*", "owners": ["bob"]}],
                      issues=[_issue(7, updated="2026-02-03T00:00:00Z")])
        return jan, feb

    def test_structure_edges_do_not_depend_on_fold_order(self):
        jan, feb = self._windows()
        one, two = _store(), _store()
        _fold(one, jan, feb)
        _fold(two, feb, jan)
        self.assertEqual(_all_edges(one), _all_edges(two))
        # February is the latest window: A depends on C, bob owns A and C, and
        # alice still owns B (February's graph does not list it).
        self.assertEqual(_edges(one, "depends_on"),
                         [(_q("area-" + A), _q("area-" + C))])
        self.assertEqual(_edges(one, "owns"),
                         [(_person("alice"), _q("area-" + B)),
                          (_person("bob"), _q("area-" + A)),
                          (_person("bob"), _q("area-" + C))])
        for conn in (one, two):
            self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_item_edges_follow_the_last_folded_record(self):
        # A record is upserted last-fold-wins, so ORDER decides which record
        # of #5 the store keeps — and its edges follow that record.
        jan = _bundle(JAN, issues=[_issue(5, milestone="v1")],
                      milestones=MILESTONES)
        feb = _bundle(FEB, issues=[_issue(5, updated="2026-02-03T00:00:00Z")],
                      milestones=MILESTONES)
        one, two = _store(), _store()
        _fold(one, jan, feb)
        _fold(two, feb, jan)
        self.assertEqual(_edges(one, "in_milestone"), [])
        self.assertEqual(_edges(two, "in_milestone"),
                         [(_q("issue-5"), _q("milestone-1"))])
        for conn in (one, two):
            self.assertNoCurrentStateWarn(self.assertClean(conn))


# --- first fold unchanged; validate is exact where it can be ------------------

class TestFirstFoldAndTrustCheck(_Case):
    def test_first_fold_writes_the_bundle_derivation(self):
        b = _bundle(JAN, [_area(A, [_edge(B)]), _area(C, [_edge(B)])],
                    owners=[{"pattern": "*", "owners": ["alice"]}],
                    issues=[_issue(5, "blocks #6 blocks #8", milestone="v1",
                                   iteration="S1", blocks=[_out(6), _out(8)]),
                            _issue(6)],
                    milestones=MILESTONES, sprints=SPRINTS)
        conn = _store()
        summary = _fold(conn, b)
        self.assertEqual(summary["retracted"], {})
        self.assertEqual(_edges(conn, "blocks"), [(_q("issue-5"), _q("issue-6"))])
        self.assertEqual(_edges(conn, "in_iteration"),
                         [(_q("issue-5"), _q("sprint-S1"))])
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B)),
                          (_q("area-" + C), _q("area-" + B))])
        self.assertEqual(graphstore.get_current_state(conn, "o", "r"),
                         {"items": True, "structure": True,
                          "from": JAN[0], "to": JAN[1]})

    def _multi(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)]), _area(B)],
                            owners=[{"pattern": "*", "owners": ["alice"]}],
                            issues=[_issue(5, milestone="v1")],
                            milestones=MILESTONES),
              _bundle(FEB, [_area(A, [])],
                      owners=[{"pattern": "*", "owners": ["bob"]}],
                      issues=[_issue(6, updated="2026-02-03T00:00:00Z")],
                      milestones=MILESTONES))
        return conn

    def _edge_set(self, conn):
        report = validate.validate(conn, project="o", repo="r")
        return [c for c in report.checks if c["name"] == "edge_set"][0]

    def test_stale_edges_are_errors_on_a_maintained_multi_window_store(self):
        stale = [(_q("area-" + A), _q("area-" + B), "depends_on",
                  json.dumps(_edge(None) | {"resolved": True})),
                 (_person("alice"), _q("area-" + A), "owns", None),
                 (_q("issue-5"), _q("milestone-2"), "in_milestone", None)]
        for src, dst, et, data in stale:
            conn = self._multi()
            self.assertTrue(self._edge_set(conn)["ok"])
            conn.execute("INSERT OR REPLACE INTO edges VALUES (?, ?, ?, NULL, ?)",
                         (src, dst, et, data))
            conn.commit()
            es = self._edge_set(conn)
            self.assertIs(es["ok"], False, (et, es))
            errs = [d for d in es["details"] if d.get("severity") == "ERROR"]
            self.assertEqual([d["edge_type"] for d in errs], [et])

    def test_store_folded_before_95_only_warns_and_says_so(self):
        conn = self._multi()
        conn.execute("DELETE FROM meta WHERE key LIKE 'current_state:%'")
        conn.execute("INSERT INTO edges VALUES (?, ?, 'in_milestone', NULL, NULL)",
                     (_q("issue-5"), _q("milestone-2")))
        conn.commit()
        es = self._edge_set(conn)
        self.assertTrue(es["ok"], es)
        warns = [d for d in es["details"] if d.get("severity") == "WARN"]
        # the injected stale milestone edge, and alice's January owns of B
        # (exact replay needs a fold since #95): WARNs that name the reason
        self.assertEqual(sorted(d["edge_type"] for d in warns),
                         ["in_milestone", "owns"])
        for d in warns:
            self.assertIn("#95", d["problem"])
        # idempotency: the re-fold heals the stale edge -> WARN, not a failure
        report = validate.validate(conn, project="o", repo="r")
        idem = [c for c in report.checks if c["name"] == "idempotency"][0]
        self.assertTrue(idem["ok"], idem)
        self.assertIn("#95", json.dumps(idem["details"]))

    def test_pre_95_stale_dependency_and_standin_heal_without_failing(self):
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A, [_edge(B)])]),
              _bundle(FEB, [_area(A, [])]))
        # What a fold before #95 left: the edge and its stand-in, no marker.
        graphstore.upsert_node(conn, _q("area-" + B), "o", "r", "structure", None,
                               {"id": B, "synthesized": graphstore.STANDIN_MARK})
        graphstore.upsert_edge(conn, _q("area-" + A), _q("area-" + B),
                               "depends_on", data={k: v for k, v in _edge(B).items()
                                                   if k != "to"})
        conn.execute("DELETE FROM meta WHERE key LIKE 'current_state:%'")
        conn.commit()
        report = self.assertClean(conn)
        idem = [c for c in report.checks if c["name"] == "idempotency"][0]
        self.assertIn("#95", json.dumps(idem["details"]))
        # the next fold heals it, and logs what it retracted
        summary = _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertEqual(summary["retracted"], {"depends_on": 1})
        self.assertEqual(summary["standins_removed"], [_q("area-" + B)])
        self.assertNoCurrentStateWarn(self.assertClean(conn))

    def test_prefilter_never_hides_a_record_with_a_milestone(self):
        tricky = _issue(5, 'says "milestone": null and "iteration": null',
                        milestone="v1")
        tricky["comments_list"] = [{"author": "bob", "body": '"milestone": null'}]
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[tricky], milestones=MILESTONES))
        self.assertEqual(_edges(conn, "in_milestone"),
                         [(_q("issue-5"), _q("milestone-1"))])
        self.assertEqual([r[0] for r in graphstore.item_relation_rows(
            conn, "o/r#")], [_q("issue-5")])
        plain = _store()
        _fold(plain, _bundle(JAN, issues=[_issue(6)], milestones=MILESTONES))
        self.assertEqual(graphstore.item_relation_rows(plain, "o/r#"), [])

    def test_window_without_facts_keeps_structure_lenient(self):
        conn = self._multi()
        conn.execute("DELETE FROM meta WHERE key LIKE 'facts:o/r:2026-01-01:%'")
        conn.commit()
        es = self._edge_set(conn)
        note = [d for d in es["details"] if d.get("summary") == "per_type_counts"]
        self.assertIn("WARN only: depends_on, owns, touches", note[0]["note"])

    def test_window_without_facts_retracts_no_structure_edge(self):
        conn = _store()
        jan = _bundle(JAN, [_area(A, [_edge(B)]), _area(B)])
        del jan["meta"]["from"], jan["meta"]["to"]       # no per-window facts
        _fold(conn, jan)
        summary = _fold(conn, _bundle(FEB, [_area(A, [])]))
        self.assertEqual(_edges(conn, "depends_on"),
                         [(_q("area-" + A), _q("area-" + B))])
        self.assertFalse(summary["structure"])
        self.assertFalse(graphstore.get_current_state(conn, "o", "r")["structure"])

    def test_touches_are_exact_on_a_multi_window_store(self):
        def commit(sha, date, path):
            return {"sha": sha, "author": "alice", "date": date,
                    "message": "x", "files": [path]}
        conn = _store()
        _fold(conn, _bundle(JAN, [_area(A)],
                            commits=[commit("a" * 40, "2026-01-05", A + "/main.tf")]),
              _bundle(FEB, [_area(B)],
                      commits=[commit("b" * 40, "2026-02-05", B + "/main.tf")]))
        es = self._edge_set(conn)
        self.assertTrue(es["ok"], es)
        note = [d for d in es["details"] if d.get("summary") == "per_type_counts"]
        self.assertIn("touches", note[0]["note"].split("WARN only")[0])
        conn.execute("DELETE FROM edges WHERE edge_type='touches' AND src_id=?",
                     (_q("a" * 40),))
        conn.commit()
        self.assertIs(self._edge_set(conn)["ok"], False)

    def test_apply_structural_scan_marks_the_graph(self):
        g = gather.apply_structural_scan({"areas": []}, "/clone",
                                         list_files=lambda: [],
                                         read_text=lambda rel: "")
        self.assertIs(g["structural_scan"], True)


if __name__ == "__main__":
    unittest.main()
