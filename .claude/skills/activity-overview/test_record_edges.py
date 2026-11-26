"""Issue #111 item 1 — record-derived edges follow the record that implies them.

A stored PR / issue / commit record is replaced latest-wins on every fold, and
the edges derived from it — `closes` / `cross_ref` (src: the PR), the
contribution edges `authored` / `merged` / `reviewed` / `commented` /
`reported` (dst: the PR / issue / commit) and a commit's `part_of` (src: the
commit) — are a function of that record's CURRENT content. They used to be
only ever added, so a PR open in January (closing #150, commented on by
mallory) and merged in February with an edited body and no comments kept the
January edges; the February view followed the stale `closes` edge to #150 and
gained its reporter; `validate` failed `edge_set` / `no_drift` on a normally
gathered store; a commit whose author resolved to a login on a later gather
kept the git-name person and the ledger kept the old author.

The rules (gather.fold_bundle, STORE.md "Record-derived edges (#111)"):

- after a fold upserts a PR / issue / commit record, that record's
  record-derived edges are exactly what its new content implies; the ones it
  no longer implies are deleted. Only records the bundle carries are touched:
  absence from a bundle is not evidence;
- cross-repo `closes` / `cross_ref` follow the same rule in a manifest fold; a
  single-repo fold leaves cross-repo edges alone;
- a commit's `touches` are re-derived only when its stored file list changed;
- a person node no contribution edge references any more is deleted (people
  are project-scoped: another member's edge keeps it);
- the code-event ledger updates `author` / `hunk` / ... of an existing
  (artifact, commit, event) row, so the artifact nodes re-credit it;
- retractions are recorded in the retraction ledger and the fold summary,
  inside the fold transaction; a re-fold of the same bundle is a no-op.

Offline: stores are built in memory via gather.fold_bundle.
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

JAN = ("2026-01-01", "2026-01-31")
FEB = ("2026-02-01", "2026-02-28")
URL = "https://github.com/o/r"
SHA = "a" * 40


def _bundle(window, prs=(), issues=(), commits=(), code_events=(), repo="r",
            owner="o", areas=None):
    cg = {"provider": "directory",
          "areas": copy.deepcopy(areas) if areas is not None else []}
    return {"meta": {"owner": owner, "repo": repo, "from": window[0],
                     "to": window[1], "base_branch": "main",
                     "branches": ["main"]},
            "prs": [copy.deepcopy(p) for p in prs],
            "issues": [copy.deepcopy(i) for i in issues],
            "commits": [copy.deepcopy(c) for c in commits],
            "code_events": [copy.deepcopy(e) for e in code_events],
            "symbol_events": [], "milestones": [], "releases": [],
            "code_graph": cg}


def _pr(n, repo="r", owner="o", **kw):
    pr = {"number": n, "title": "pr {}".format(n), "body": "",
          "author": "alice", "state": "open", "merged": False,
          "created_at": "2026-01-03T00:00:00Z", "merged_at": None,
          "closed_at": None, "merged_by": None, "reviewers": [],
          "closes": [], "crossref_issues": [], "comments_list": [],
          "review_comments": [],
          "url": "https://github.com/{}/{}/pull/{}".format(owner, repo, n)}
    pr.update(kw)
    return pr


def _issue(n, author="zed", repo="r", owner="o", **kw):
    iss = {"number": n, "title": "issue {}".format(n), "body": "",
           "author": author, "state": "open",
           "created_at": "2026-01-02T00:00:00Z",
           "updated_at": "2026-01-10T00:00:00Z", "closed_at": None,
           "url": "https://github.com/{}/{}/issues/{}".format(owner, repo, n),
           "labels": [], "milestone": None, "comments_list": []}
    iss.update(kw)
    return iss


def _commit(author, sha=SHA, files=("docs/x.md",), resolved=True,
            message="docs: x", date="2026-01-05"):
    c = {"sha": sha, "author": author, "date": date, "message": message,
         "files": list(files)}
    if not resolved:
        c["login_resolved"] = False
    return c


def _event(author, sha=SHA, path="docs/x.md", hunk="+x"):
    return {"path": path, "change": "add", "commit": sha, "author": author,
            "date": "2026-01-05", "hunk": hunk}


# Reviewer's Repro A: PR 100 open in January (closes #150, a comment by
# mallory; #150 reported by zed and closed in January), merged in February with `closes: []` and no
# comments.
def _jan():
    return _bundle(JAN, prs=[_pr(100, closes=[150],
                                 comments_list=[{"author": "mallory",
                                                 "body": "looks risky"}])],
                   issues=[_issue(150, author="zed", state="closed",
                                  closed_at="2026-01-20T00:00:00Z",
                                  updated_at="2026-01-20T00:00:00Z")])


def _feb():
    return _bundle(FEB, prs=[_pr(100, state="closed", merged=True,
                                 merged_at="2026-02-10T00:00:00Z",
                                 closed_at="2026-02-10T00:00:00Z",
                                 merged_by="bob", closes=[])])


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _fold(conn, *bundles, **kw):
    out = None
    for b in bundles:
        out = gather.fold_bundle(conn, copy.deepcopy(b), **kw)
    return out


def _q(local, repo="r", project="o"):
    return graphstore.qualify_id(project, repo, local)


def _p(login, project="o"):
    return graphstore.qualify_person(project, login)


def _edges(conn, etype=None):
    if etype is None:
        return sorted(tuple(r) for r in conn.execute(
            "SELECT src_id, dst_id, edge_type FROM edges"))
    return sorted((r[0], r[1]) for r in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE edge_type=?", (etype,)))


def _people(conn, project="o"):
    prefix = graphstore.qualify_person(project, "")
    return [nid[len(prefix):] for nid in
            graphstore.node_ids_with_prefix(conn, prefix)]


def _dump(conn):
    """Every row of every table a fold writes (fetched_at dropped)."""
    return {
        "nodes": sorted((r[0], r[1], r[2], r[3], r[4], r[5]) for r in
                        conn.execute("SELECT id, project, repo, node_class, ts, "
                                     "data FROM nodes")),
        "edges": sorted(tuple(r) for r in conn.execute(
            "SELECT src_id, dst_id, edge_type, ts, data FROM edges")),
        "code_events": sorted(tuple(r) for r in conn.execute(
            "SELECT * FROM code_events")),
        "meta": sorted(tuple(r) for r in conn.execute(
            "SELECT key, value FROM meta")),
    }


class _Case(unittest.TestCase):
    def assertClean(self, conn, project="o", repo="r"):
        report = validate.validate(conn, project=project, repo=repo)
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        return report

    def assertCleanProject(self, conn, project, repos):
        report = validate.validate_project(conn, project, repos)
        self.assertTrue(report["ok"], json.dumps(report, indent=1, default=str))


class TestReproA(_Case):
    def test_january_then_february_drops_what_the_merged_record_no_longer_implies(self):
        conn = _store()
        _fold(conn, _jan())
        self.assertIn((_q("pr-100"), _q("issue-150")), _edges(conn, "closes"))
        summary = _fold(conn, _feb())
        self.assertEqual(_edges(conn, "closes"), [])
        self.assertNotIn((_p("mallory"), _q("pr-100")), _edges(conn, "commented"))
        self.assertNotIn("mallory", _people(conn))
        self.assertIn((_p("bob"), _q("pr-100")), _edges(conn, "merged"))
        # #150 is still a stored record: its reporter keeps that edge.
        self.assertIn((_p("zed"), _q("issue-150")), _edges(conn, "reported"))
        self.assertEqual(summary["retracted"], {"closes": 1, "commented": 1})
        self.assertEqual(summary["people_removed"], [_p("mallory")])
        view = extract.extract(conn, "o", "r", FEB[0], FEB[1])
        self.assertEqual([i["number"] for i in view["issues"]], [])
        self.assertNotIn("zed", view.get("people") or {})
        self.assertNotIn("mallory", view.get("people") or {})
        self.assertClean(conn)

    def test_february_then_january_ends_in_the_last_folded_record(self):
        conn = _store()
        _fold(conn, _feb(), _jan())
        self.assertEqual(_edges(conn, "closes"), [(_q("pr-100"), _q("issue-150"))])
        self.assertIn((_p("mallory"), _q("pr-100")), _edges(conn, "commented"))
        # January's record has no merged_by: bob's merge is no longer implied.
        self.assertEqual(_edges(conn, "merged"), [])
        self.assertNotIn("bob", _people(conn))
        self.assertClean(conn)

    def test_both_orders_equal_a_fresh_fold_of_the_last_record(self):
        for first, last in ((_jan, _feb), (_feb, _jan)):
            conn = _store()
            _fold(conn, first(), last())
            fresh = _store()
            # the last record plus the other window's records the last bundle
            # does not carry (issue 150 is in January only)
            _fold(fresh, first(), last())
            self.assertEqual(_edges(conn), _edges(fresh))
            pr = graphstore.get_node(conn, _q("pr-100"))["data"]
            self.assertEqual(pr["closes"], last()["prs"][0]["closes"])

    def test_retraction_is_logged_in_the_ledger_and_on_stderr(self):
        conn = _store()
        _fold(conn, _jan())
        summary = _fold(conn, _feb())
        ledger = graphstore.get_retractions(conn, "o", "r")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["counts"], {"closes": 1, "commented": 1})
        self.assertEqual(ledger[0]["edges"], [
            [_p("mallory"), _q("pr-100"), "commented"],
            [_q("pr-100"), _q("issue-150"), "closes"]])
        self.assertEqual(ledger[0]["people_removed"], [_p("mallory")])
        self.assertEqual((ledger[0]["from"], ledger[0]["to"]), FEB)
        line = gather.describe_retractions(summary, "o", "r")
        self.assertIn("retracted 2 edge(s) of o/r that the re-folded records no longer imply", line)
        self.assertIn("1 closes", line)
        self.assertIn("1 commented", line)
        self.assertIn("removed 1 person node(s)", line)
        self.assertNotIn("current-state", line)


class TestReproB(_Case):
    def _first(self):
        return _bundle(JAN, commits=[_commit("Alice Smith", resolved=False)],
                       code_events=[_event("Alice Smith")])

    def _second(self):
        return _bundle(JAN, commits=[_commit("alice")],
                       code_events=[_event("alice", hunk="+x\n+y")])

    def test_author_resolved_on_a_later_gather_recredits_everything(self):
        conn = _store()
        _fold(conn, self._first())
        self.assertEqual(_people(conn), ["Alice Smith"])
        summary = _fold(conn, self._second())
        self.assertEqual(_people(conn), ["alice"])
        self.assertEqual(_edges(conn, "authored"), [(_p("alice"), _q(SHA))])
        self.assertEqual(summary["people_removed"], [_p("Alice Smith")])
        rows = conn.execute("SELECT author, hunk FROM code_events").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("alice", "+x\n+y")])
        art = graphstore.get_node(conn, _q("art:docs/x.md"))["data"]
        self.assertEqual({e.get("author") for e in art["lifecycle"]}, {"alice"})
        self.assertClean(conn)

    def test_refold_equals_a_fresh_store_of_the_second_bundle(self):
        conn = _store()
        _fold(conn, self._first(), self._second())
        fresh = _store()
        _fold(fresh, self._second())
        a, b = _dump(conn), _dump(fresh)
        for key in ("nodes", "edges", "code_events"):
            self.assertEqual(a[key], b[key], key)


class TestScope(_Case):
    def test_a_record_absent_from_the_bundle_keeps_its_edges(self):
        conn = _store()
        jan = _bundle(JAN, prs=[_pr(100, closes=[150]),
                                _pr(101, closes=[151],
                                    comments_list=[{"author": "mallory",
                                                    "body": "x"}])],
                      issues=[_issue(150), _issue(151)])
        _fold(conn, jan)
        # February re-gathers PR 100 only; PR 101 is not in it.
        _fold(conn, _bundle(FEB, prs=[_pr(100, closes=[])]))
        self.assertEqual(_edges(conn, "closes"), [(_q("pr-101"), _q("issue-151"))])
        self.assertIn((_p("mallory"), _q("pr-101")), _edges(conn, "commented"))
        self.assertIn("mallory", _people(conn))
        self.assertClean(conn)

    def test_review_reviewer_merger_and_comment_changes(self):
        conn = _store()
        before = _pr(7, reviewers=["rita", "sam"], merged_by="max",
                     review_comments=[{"author": "carl", "body": "nit"}])
        after = _pr(7, reviewers=["sam"], merged_by="moe",
                    review_comments=[])
        _fold(conn, _bundle(JAN, prs=[before]), _bundle(JAN, prs=[after]))
        self.assertEqual(_edges(conn, "reviewed"), [(_p("sam"), _q("pr-7"))])
        self.assertEqual(_edges(conn, "merged"), [(_p("moe"), _q("pr-7"))])
        self.assertEqual(_edges(conn, "commented"), [])
        self.assertEqual(_people(conn), ["alice", "moe", "sam"])
        self.assertClean(conn)

    def test_issue_author_and_comment_changes(self):
        conn = _store()
        _fold(conn, _bundle(JAN, issues=[_issue(5, author="zed",
                                                comments_list=[{"author": "kim",
                                                                "body": "+1"}])]),
              _bundle(JAN, issues=[_issue(5, author="ghost")]))
        self.assertEqual(_edges(conn, "reported"), [(_p("ghost"), _q("issue-5"))])
        self.assertEqual(_edges(conn, "commented"), [])
        self.assertEqual(_people(conn), ["ghost"])
        self.assertClean(conn)

    def test_cross_ref_and_commit_part_of_follow_the_record(self):
        conn = _store()
        _fold(conn, _bundle(JAN, prs=[_pr(7, crossref_issues=[9]), _pr(8)],
                            issues=[_issue(9)],
                            commits=[_commit("alice", message="fix (#7)")]),
              _bundle(JAN, prs=[_pr(7), _pr(8)], issues=[_issue(9)],
                      commits=[_commit("alice", message="fix (#8)")]))
        self.assertEqual(_edges(conn, "cross_ref"), [])
        self.assertEqual(_edges(conn, "part_of"), [(_q(SHA), _q("pr-8"))])
        self.assertClean(conn)

    def test_touches_follow_a_changed_file_list_only(self):
        areas = [{"id": "modules/a", "paths": ["modules/a/main.tf"]},
                 {"id": "modules/b", "paths": ["modules/b/main.tf"]}]
        conn = _store()
        _fold(conn, _bundle(JAN, commits=[_commit("alice", files=["modules/a/main.tf"])],
                            areas=areas))
        # The same commit re-gathered with a corrected file list.
        _fold(conn, _bundle(JAN, commits=[_commit("alice", files=["modules/b/main.tf"])],
                            areas=areas))
        self.assertEqual(_edges(conn, "touches"),
                         [(_q(SHA), _q("area-modules/b"))])
        # An unchanged file list folded with a graph that no longer maps the
        # file keeps its touches (they also depend on the code graph).
        _fold(conn, _bundle(FEB, commits=[_commit("alice", files=["modules/b/main.tf"])],
                            areas=[]))
        self.assertEqual(_edges(conn, "touches"),
                         [(_q(SHA), _q("area-modules/b"))])

    def test_single_repo_fold_leaves_cross_repo_edges_alone(self):
        members = {"o/app", "o/lib"}
        conn = _store()
        pr = _pr(7, repo="app", body="Closes o/lib#3")
        gather.fold_bundle(conn, _bundle(JAN, prs=[pr], repo="app"),
                           project="p", repo="o/app", members=members)
        cross = (graphstore.qualify_id("p", "o/app", "pr-7"),
                 graphstore.qualify_id("p", "o/lib", "issue-3"))
        self.assertIn(cross, _edges(conn, "closes"))
        # a single-repo fold of the same member derives no cross-repo edge
        gather.fold_bundle(conn, _bundle(JAN, prs=[pr], repo="app"),
                           project="p", repo="o/app")
        self.assertIn(cross, _edges(conn, "closes"))
        # a manifest fold of the edited record retracts it
        summary = gather.fold_bundle(
            conn, _bundle(JAN, prs=[dict(pr, body="")], repo="app"),
            project="p", repo="o/app", members=members)
        self.assertNotIn(cross, _edges(conn, "closes"))
        self.assertEqual(summary["retracted"], {"closes": 1})


class TestPeopleInAProject(_Case):
    MEMBERS = {"o/app", "o/lib"}

    def _fold_member(self, conn, b, repo):
        return gather.fold_bundle(conn, copy.deepcopy(b), project="p",
                                  repo=repo, members=self.MEMBERS)

    def test_person_still_referenced_by_another_member_is_kept(self):
        conn = _store()
        self._fold_member(conn, _bundle(JAN, repo="app", prs=[_pr(
            1, repo="app", comments_list=[{"author": "mallory", "body": "x"}])]),
            "o/app")
        self._fold_member(conn, _bundle(JAN, repo="lib", issues=[_issue(
            2, author="mallory", repo="lib")]), "o/lib")
        summary = self._fold_member(conn, _bundle(JAN, repo="app", prs=[
            _pr(1, repo="app")]), "o/app")
        self.assertEqual(summary["retracted"], {"commented": 1})
        self.assertEqual(summary["people_removed"], [])
        self.assertIn("mallory", _people(conn, "p"))
        self.assertCleanProject(conn, "p", ["o/app", "o/lib"])
        # ...and goes once the other member's record no longer names them.
        summary = self._fold_member(conn, _bundle(JAN, repo="lib", issues=[
            _issue(2, author="zed", repo="lib")]), "o/lib")
        self.assertEqual(summary["people_removed"],
                         [graphstore.qualify_person("p", "mallory")])
        self.assertNotIn("mallory", _people(conn, "p"))
        self.assertCleanProject(conn, "p", ["o/app", "o/lib"])


class TestPeopleAreas(_Case):
    AREAS = [{"id": "modules/a", "paths": ["modules/a/main.tf"]}]

    def test_reattributed_commit_takes_its_areas_on_a_one_window_store(self):
        conn = _store()
        pr = _pr(9, comments_list=[{"author": "alice", "body": "x"}])
        _fold(conn, _bundle(JAN, prs=[pr], areas=self.AREAS, commits=[
            _commit("alice", files=["modules/a/main.tf"])]))
        self.assertEqual(graphstore.get_node(conn, _p("alice"))["data"]["areas"],
                         ["modules/a"])
        _fold(conn, _bundle(JAN, prs=[pr], areas=self.AREAS, commits=[
            _commit("bob", files=["modules/a/main.tf"])]))
        # alice still commented, but authors nothing in the window any more
        self.assertEqual(graphstore.get_node(conn, _p("alice"))["data"]["areas"],
                         [])
        self.assertEqual(graphstore.get_node(conn, _p("bob"))["data"]["areas"],
                         ["modules/a"])
        self.assertClean(conn)


class TestFoldProperties(_Case):
    def test_first_fold_into_an_empty_store_is_the_bundle_derivation(self):
        conn = _store()
        summary = _fold(conn, _jan())
        self.assertEqual(summary["retracted"], {})
        self.assertEqual(summary["people_removed"], [])
        self.assertEqual(graphstore.get_retractions(conn, "o", "r"), [])
        self.assertEqual(_edges(conn), sorted([
            (_q("pr-100"), _q("issue-150"), "closes"),
            (_p("alice"), _q("pr-100"), "authored"),
            (_p("mallory"), _q("pr-100"), "commented"),
            (_p("zed"), _q("issue-150"), "reported")]))
        self.assertTrue(graphstore.get_current_state(conn, "o", "r")["records"])

    def test_refold_of_the_same_bundle_is_a_no_op(self):
        conn = _store()
        _fold(conn, _jan(), _feb())
        before = _dump(conn)
        ledger = graphstore.get_retractions(conn, "o", "r")
        summary = _fold(conn, _feb())
        self.assertEqual(summary["retracted"], {})
        self.assertEqual(summary["people_removed"], [])
        self.assertIsNone(gather.describe_retractions(summary, "o", "r"))
        self.assertEqual(graphstore.get_retractions(conn, "o", "r"), ledger)
        after = _dump(conn)
        for key in ("nodes", "edges", "code_events", "meta"):
            self.assertEqual(before[key], after[key], key)

    def test_failed_fold_rolls_back_the_record_retractions(self):
        conn = _store()
        _fold(conn, _jan())
        before = _dump(conn)
        real = gather._maintain_current_state

        def boom(*a, **kw):
            real(*a, **kw)
            raise RuntimeError("fold failed after the retraction")
        gather._maintain_current_state = boom
        try:
            with self.assertRaises(RuntimeError):
                _fold(conn, _feb())
        finally:
            gather._maintain_current_state = real
        after = _dump(conn)
        for key in ("nodes", "edges", "code_events", "meta"):
            self.assertEqual(before[key], after[key], key)
        self.assertIn("mallory", _people(conn))

    def test_gather_prints_the_summary_line(self):
        line = gather.describe_retractions(
            {"retracted": {"closes": 1, "depends_on": 2},
             "people_removed": ["o#person-x"], "standins_removed": []},
            "o", "r")
        self.assertIn("retracted 2 stale current-state edge(s) of o/r "
                      "(2 depends_on)", line)
        self.assertIn("1 edge(s) of o/r that the re-folded records no longer imply (1 closes)",
                      line)
        self.assertIn("removed 1 person node(s) no contribution references",
                      line)
        self.assertTrue(line.endswith("(meta retractions:o/r)"))


class TestStoreFoldedBeforeThisChange(_Case):
    def _stale_store(self):
        """A store whose Jan+Feb folds ran before #111: the stale January
        edges and person are still there and the marker has no `records`."""
        conn = _store()
        _fold(conn, _jan())
        stale = _dump(conn)
        _fold(conn, _feb())
        # put back what a pre-#111 fold left behind
        conn.executemany(
            "INSERT OR IGNORE INTO edges (src_id, dst_id, edge_type, ts, data) "
            "VALUES (?, ?, ?, ?, ?)", stale["edges"])
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, project, repo, node_class, ts, "
            "data, fetched_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
            [n for n in stale["nodes"] if n[0] == _p("mallory")])
        state = graphstore.get_current_state(conn, "o", "r")
        state.pop("records")
        graphstore.set_current_state(conn, "o", "r", state)
        conn.execute("DELETE FROM meta WHERE key LIKE 'retractions:%'")
        conn.commit()
        return conn

    def test_validate_errors_and_says_fold_once_more(self):
        conn = self._stale_store()
        report = validate.validate(conn, project="o", repo="r")
        es = [c for c in report.checks if c["name"] == "edge_set"][0]
        self.assertFalse(es["ok"])
        errs = [d for d in es["details"] if d.get("severity") == "ERROR"]
        self.assertTrue(errs)
        for d in errs:
            self.assertIn("fold any window of it once more", d.get("hint", ""))
        # the re-fold probe heals them: a WARN there, not a second failure
        idem = [c for c in report.checks if c["name"] == "idempotency"][0]
        self.assertTrue(idem["ok"], json.dumps(idem, indent=1))
        warns = [d for d in idem["details"] if d.get("severity") == "WARN"]
        self.assertEqual(len(warns), 1)
        self.assertIn("2 stale record-derived edge(s)", warns[0]["note"])

    def test_one_more_fold_of_any_window_heals_every_record(self):
        conn = self._stale_store()
        # a window that does not carry PR 100 at all
        summary = _fold(conn, _bundle(("2026-03-01", "2026-03-31"),
                                      issues=[_issue(160, author="kim")]))
        self.assertEqual(summary["retracted"], {"closes": 1, "commented": 1})
        self.assertEqual(summary["people_removed"], [_p("mallory")])
        self.assertEqual(_edges(conn, "closes"), [])
        self.assertTrue(graphstore.get_current_state(conn, "o", "r")["records"])
        self.assertClean(conn)


if __name__ == "__main__":
    unittest.main()
