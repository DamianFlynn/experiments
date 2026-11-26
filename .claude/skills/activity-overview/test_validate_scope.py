"""The trust check audits each repo's own rows (issue #107 item 3).

In project mode a member's report used to list every member's errors: the
row-level checks (referential_integrity, provenance, no_fabrication,
schema_conformance) read the whole store. Each now audits the rows the
audited repo owns (a node by its project/repo columns, an edge by the repo of
its src, a person's edge by the repo of its dst); the rows no member owns and
the store-wide parts (the per-window facts index, case-variant scopes) run
once, project-wide, and stay in the report of a single-repo audit.
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
A, B = "Azure/mod-a", "Azure/mod-b"
ROW_CHECKS = ("referential_integrity", "provenance", "no_fabrication",
              "schema_conformance")


def _pr(n, author, repo):
    return {"number": n, "url": "https://github.com/%s/pull/%d" % (repo, n),
            "state": "closed", "merged": True, "base": "main", "head": "h%d" % n,
            "merged_at": "2026-01-1%dT00:00:00Z" % (n % 10),
            "created_at": "2026-01-0%dT00:00:00Z" % (n % 10),
            "closed_at": "2026-01-1%dT00:00:00Z" % (n % 10),
            "closes": [], "crossref_issues": [], "title": "feat", "body": "",
            "author": author}


def _two_member_store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for repo, author, num in ((A, "alice", 1), (B, "bob", 2)):
        bundle = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                           "from": "2026-01-01", "to": "2026-01-31",
                           "base_branch": "main"},
                  "prs": [_pr(num, author, repo)], "issues": [], "commits": [],
                  "code_events": [], "milestones": [], "releases": [],
                  "code_graph": {"areas": []}}
        gather.fold_bundle(conn, bundle, project="proj", repo=repo,
                           members={A, B})
    conn.commit()
    return conn


def _by_name(checks):
    return {c["name"]: c for c in checks}


def _member(agg, repo):
    return _by_name(next(m for m in agg["members"] if m["repo"] == repo)["checks"])


def _tamper_member_a(conn):
    """Break member A on each row-level check, with A's own rows only."""
    pr_id = graphstore.qualify_id("proj", A, "pr-1")
    row = conn.execute("SELECT data FROM nodes WHERE id=?", (pr_id,)).fetchone()
    data = json.loads(row[0])
    data["url"] = "https://github.com/Azure/elsewhere/pull/1"
    conn.execute("UPDATE nodes SET data=? WHERE id=?", (json.dumps(data), pr_id))
    # a dangling non-spine edge out of A, and an edge whose kinds break the
    # schema (pr -[touches]-> pr), both owned by A (their src is A's)
    conn.execute("INSERT INTO edges (src_id, dst_id, edge_type, ts, data) "
                 "VALUES (?, ?, 'in_milestone', NULL, NULL)",
                 (pr_id, graphstore.qualify_id("proj", A, "milestone-gone")))
    conn.execute("INSERT INTO edges (src_id, dst_id, edge_type, ts, data) "
                 "VALUES (?, ?, 'touches', NULL, NULL)", (pr_id, pr_id))
    # an artifact node of A with no lifecycle history
    art = graphstore.qualify_id("proj", A, "art:x.md")
    conn.execute("INSERT INTO nodes VALUES (?, 'proj', ?, 'code', NULL, ?, ?)",
                 (art, A, json.dumps({"path": "x.md", "kind": "file",
                                      "lifecycle": []}), "2026-01-31"))
    conn.commit()
    return pr_id, art


class TestProjectModeScopesRowChecks(unittest.TestCase):
    def test_clean_two_member_store_passes_with_project_row_checks(self):
        agg = validate.validate_project(_two_member_store(), "proj", [A, B])
        self.assertTrue(agg["ok"], agg)
        self.assertEqual([c["name"] for c in agg["project_checks"]],
                         ["no_drift_people", *ROW_CHECKS, "member_status"])

    def test_tampered_member_a_fails_a_and_not_b(self):
        conn = _two_member_store()
        pr_id, art = _tamper_member_a(conn)
        agg = validate.validate_project(conn, "proj", [A, B])
        self.assertFalse(agg["ok"])
        a, b = _member(agg, A), _member(agg, B)
        for name in ROW_CHECKS:
            self.assertFalse(a[name]["ok"], (name, a[name]))
            self.assertTrue(b[name]["ok"], (name, b[name]))
        self.assertIn(pr_id, json.dumps(a["provenance"]["details"]))
        self.assertIn(art, json.dumps(a["no_fabrication"]["details"]))
        self.assertNotIn("mod-a", json.dumps([b[n]["details"] for n in ROW_CHECKS]))
        # the errors are not repeated project-wide either
        project = _by_name(agg["project_checks"])
        for name in ROW_CHECKS:
            self.assertTrue(project[name]["ok"], (name, project[name]))

    def test_corrupt_row_fails_only_its_owner(self):
        conn = _two_member_store()
        pr_id = graphstore.qualify_id("proj", A, "pr-1")
        conn.execute("UPDATE nodes SET data='{not json' WHERE id=?", (pr_id,))
        conn.commit()
        agg = validate.validate_project(conn, "proj", [A, B])
        a, b = _member(agg, A), _member(agg, B)
        self.assertFalse(a["provenance"]["ok"])
        self.assertIn(pr_id, json.dumps(a["provenance"]["details"]))
        for name in ROW_CHECKS:
            self.assertTrue(b[name]["ok"], (name, b[name]))
            self.assertTrue(_by_name(agg["project_checks"])[name]["ok"],
                            (name, _by_name(agg["project_checks"])[name]))

    def test_person_edge_belongs_to_the_repo_of_its_target(self):
        conn = _two_member_store()
        conn.execute("INSERT INTO edges (src_id, dst_id, edge_type, ts, data) "
                     "VALUES (?, ?, 'authored', NULL, NULL)",
                     (graphstore.qualify_person("proj", "bob"),
                      graphstore.qualify_id("proj", B, "pr-404")))
        conn.commit()
        agg = validate.validate_project(conn, "proj", [A, B])
        self.assertFalse(_member(agg, B)["referential_integrity"]["ok"])
        self.assertTrue(_member(agg, A)["referential_integrity"]["ok"])
        self.assertTrue(_by_name(agg["project_checks"])
                        ["referential_integrity"]["ok"])

    def test_store_wide_problems_reported_once_project_wide(self):
        conn = _two_member_store()
        # an orphan person (a shared row) and a dangling per-window facts entry
        conn.execute("INSERT INTO nodes VALUES (?, 'proj', '*', 'structure', "
                     "NULL, ?, '2026-01-31')",
                     (graphstore.qualify_person("proj", "ghost"),
                      json.dumps({"login": "ghost"})))
        graphstore.set_meta(conn, "facts:proj/{}:2026-01-01:2026-01-31".format(A),
                            json.dumps({"facts": {"workflow_stats": "nope"}}))
        agg = validate.validate_project(conn, "proj", [A, B])
        self.assertFalse(agg["ok"])
        for repo in (A, B):
            member = _member(agg, repo)
            for name in ROW_CHECKS:
                self.assertTrue(member[name]["ok"], (repo, name, member[name]))
        project = _by_name(agg["project_checks"])
        self.assertFalse(project["no_fabrication"]["ok"])
        self.assertIn("person-ghost", json.dumps(project["no_fabrication"]))
        self.assertFalse(project["referential_integrity"]["ok"])
        self.assertIn("per-window facts",
                      json.dumps(project["referential_integrity"]))

    def test_single_repo_audit_of_b_ignores_a_but_keeps_shared_parts(self):
        conn = _two_member_store()
        _tamper_member_a(conn)
        self.assertTrue(validate.validate(conn, "proj", B).ok)
        self.assertFalse(validate.validate(conn, "proj", A).ok)
        conn.execute("INSERT INTO nodes VALUES (?, 'proj', '*', 'structure', "
                     "NULL, ?, '2026-01-31')",
                     (graphstore.qualify_person("proj", "ghost"),
                      json.dumps({"login": "ghost"})))
        conn.commit()
        rep = validate.validate(conn, "proj", B)
        self.assertFalse(_by_name(rep.checks)["no_fabrication"]["ok"])


class TestSingleRepoReportUnchanged(unittest.TestCase):
    """On a one-repo store the scoped audit IS the whole store: the report is
    identical to an audit of every row, on every fixture store."""

    def test_every_fixture_store_reports_as_an_audit_of_every_row(self):
        names = sorted(n for n in os.listdir(FIX)
                       if n.startswith("bundle_") and n.endswith(".json"))
        self.assertTrue(names)
        for name in names:
            with open(os.path.join(FIX, name), encoding="utf-8") as fh:
                bundle = json.load(fh)
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, copy.deepcopy(bundle))
            project, repo = validate._resolve_target(conn, None, None)
            scoped = validate.validate_repo(conn, project, repo).to_dict()
            every_row = validate.validate_repo(
                conn, project, repo,
                scope=validate._Scope(project, (), None, True)).to_dict()
            # (bundle_sample.json is a toy with non-sha commit ids: it fails
            # the gate either way, identically.)
            self.assertEqual(scoped, every_row, name)
            # and the unscoped functions (no scope) agree check for check
            for fn, check in ((validate.check_referential_integrity,
                               "referential_integrity"),
                              (validate.check_provenance, "provenance"),
                              (validate.check_no_fabrication, "no_fabrication"),
                              (validate.check_schema_conformance,
                               "schema_conformance")):
                self.assertEqual(
                    fn(conn), _by_name(scoped["checks"])[check], (name, check))


if __name__ == "__main__":
    unittest.main()
