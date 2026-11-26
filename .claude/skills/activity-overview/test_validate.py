"""Tests for validate.py — the graph-trustworthiness audit (Phase 7 trust gate).

The gate must (a) pass a clean folded store on every invariant and (b) red-flag
each specific corruption on exactly the invariant it breaks. We build clean
stores from golden fixtures, then break ONE thing per test and assert the named
check fails with a useful detail.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import graphstore
import gather
import validate
from _quiet import quiet  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _bundle(name="bundle_p3b.json"):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def _clean_store(name="bundle_p3b.json"):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, _bundle(name))
    return conn


def _check(report, name):
    for c in report.checks:
        if c["name"] == name:
            return c
    raise AssertionError("no check named {} in {}".format(
        name, [c["name"] for c in report.checks]))


# --- clean store passes everything --------------------------------------------

class TestCleanStore(unittest.TestCase):
    def test_clean_store_passes_all_checks(self):
        conn = _clean_store()
        report = validate.validate(conn, project="o", repo="r")
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])
        # every individual check is ok
        for c in report.checks:
            self.assertTrue(c["ok"], c)

    def test_clean_store_with_bundle_passes_drift_and_idempotency(self):
        conn = _clean_store()
        report = validate.validate(conn, project="o", repo="r", bundle=_bundle())
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])
        names = {c["name"] for c in report.checks}
        self.assertIn("no_drift", names)
        self.assertIn("idempotency", names)

    def test_project_repo_autodetected_from_store(self):
        conn = _clean_store()
        report = validate.validate(conn)  # no project/repo passed
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])


# --- corruption catches -------------------------------------------------------

class TestCorruptionCatches(unittest.TestCase):
    def test_delete_carol_person_fails_participant_completeness(self):
        conn = _clean_store()
        conn.execute("DELETE FROM nodes WHERE id=?", ("o#person-carol",))
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "participant_completeness")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(any("carol" in str(d) for d in c["details"]), c["details"])
        self.assertIs(report.ok, False)

    def test_delete_authored_edge_fails_participant_completeness(self):
        conn = _clean_store()
        conn.execute(
            "DELETE FROM edges WHERE edge_type='authored' AND src_id=? AND dst_id=?",
            ("o#person-alice", "o/r#pr-42"))
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "participant_completeness")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(
            any("alice" in str(d) and "authored" in str(d) for d in c["details"]),
            c["details"])

    def test_dangling_nonspine_edge_fails_referential_integrity(self):
        conn = _clean_store()
        graphstore.upsert_edge(conn, "o#person-alice", "o/r#pr-9999", "authored")
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "referential_integrity")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(any("pr-9999" in str(d) for d in c["details"]), c["details"])

    def test_dangling_spine_edge_is_not_error(self):
        conn = _clean_store()
        # A PR that closes an out-of-window (not-yet-gathered) issue: legitimate
        # backfill case. closes is a spine edge -> INFO, not ERROR.
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#issue-9999", "closes")
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "referential_integrity")
        self.assertIs(c["ok"], True, c["details"])
        # but it IS surfaced as INFO / missing thread
        self.assertTrue(any(d.get("severity") == "INFO" and "issue-9999" in str(d)
                            for d in c["details"]), c["details"])

    def test_strip_pr_url_and_number_fails_provenance(self):
        conn = _clean_store()
        node = graphstore.get_node(conn, "o/r#pr-42")
        data = dict(node["data"])
        data.pop("url", None)
        data.pop("number", None)
        graphstore.upsert_node(conn, node["id"], node["project"], node["repo"],
                               node["node_class"], node["ts"], data)
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "provenance")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(any("pr-42" in str(d) for d in c["details"]), c["details"])

    def test_orphan_person_fails_no_fabrication(self):
        conn = _clean_store()
        # A person with no contribution edge whatsoever = invented contributor.
        graphstore.upsert_node(conn, "o#person-ghost", "o", "*", "structure",
                               None, {"login": "ghost", "modules": [], "areas": []})
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "no_fabrication")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(any("ghost" in str(d) for d in c["details"]), c["details"])

    def test_mutated_person_data_fails_no_drift_with_bundle(self):
        conn = _clean_store()
        node = graphstore.get_node(conn, "o#person-carol")
        data = dict(node["data"])
        data["areas"] = ["totally-made-up-area"]  # diverge from re-derivation
        graphstore.upsert_node(conn, node["id"], node["project"], node["repo"],
                               node["node_class"], node["ts"], data)
        report = validate.validate(conn, project="o", repo="r", bundle=_bundle())
        c = _check(report, "no_drift")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")
        self.assertTrue(any("carol" in str(d) for d in c["details"]), c["details"])

    def test_no_drift_self_sourced_without_bundle(self):
        """Store-only world (Phase 7): with no --bundle, no_drift + idempotency
        SELF-SOURCE the raw bundle from the store via extract and still run (and
        pass on a clean store), so the trust gate is self-contained on a store."""
        conn = _clean_store()
        report = validate.validate(conn, project="o", repo="r")
        names = {c["name"] for c in report.checks}
        self.assertIn("no_drift", names)
        self.assertIn("idempotency", names)
        self.assertIs(_check(report, "no_drift")["ok"], True,
                      _check(report, "no_drift"))
        self.assertIs(_check(report, "idempotency")["ok"], True,
                      _check(report, "idempotency"))
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])

    def test_empty_id_node_fails_no_fabrication(self):
        conn = _clean_store()
        conn.execute(
            "INSERT INTO nodes (id, project, repo, node_class, ts, data, fetched_at) "
            "VALUES ('', 'o', 'r', 'social', NULL, '{}', 'now')")
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "no_fabrication")
        self.assertIs(c["ok"], False)

    def test_schema_conformance_catches_wrong_authored_endpoint(self):
        conn = _clean_store()
        # authored src must be a person- id. Point one at a non-person src.
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#pr-42", "authored")
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "schema_conformance")
        self.assertIs(c["ok"], False)
        self.assertEqual(c["severity"], "ERROR")


# --- Phase 10 slice 1: review/event social nodes + part_of spine -------------

def _lifecycle_bundle():
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "prs": [{
            "number": 7, "url": "https://github.com/o/r/pull/7", "state": "closed",
            "merged": True, "merged_at": "2026-05-10T00:00:00Z",
            "created_at": "2026-05-02T00:00:00Z", "closed_at": "2026-05-10T00:00:00Z",
            "closes": [], "crossref_issues": [],
            "reviews": [{"id": 100, "author": "carol", "state": "approved",
                         "submitted_at": "2026-05-03T00:00:00Z", "body": None,
                         "url": "https://github.com/o/r/pull/7#r100"}],
            "lifecycle": [{"id": 200, "actor": "dan", "event": "ready_for_review",
                           "created_at": "2026-05-03T06:00:00Z", "label": None,
                           "url": None}],
        }],
        "issues": [{
            "number": 3, "url": "https://github.com/o/r/issues/3", "state": "open",
            "updated_at": "2026-05-09T00:00:00Z", "closed_at": None,
            "lifecycle": [{"id": 300, "actor": "alice", "event": "reopened",
                           "created_at": "2026-05-08T00:00:00Z", "label": None,
                           "url": None}],
        }],
        "commits": [], "milestones": [], "releases": [],
    }


def _lifecycle_store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, _lifecycle_bundle())
    return conn


class TestReviewEventSocialNodes(unittest.TestCase):
    def test_review_event_id_kinds_classified(self):
        conn = _lifecycle_store()
        cache = {}
        self.assertEqual(validate._id_kind(conn, "o/r#review-7-100", cache), "review")
        self.assertEqual(validate._id_kind(conn, "o/r#event-pr-7-200", cache), "event")
        self.assertEqual(validate._id_kind(conn, "o/r#event-issue-3-300", cache),
                         "event")

    def test_review_event_part_of_schema_conforms(self):
        conn = _lifecycle_store()
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "schema_conformance")
        self.assertIs(c["ok"], True, c["details"])

    def test_review_event_nodes_are_sourced(self):
        conn = _lifecycle_store()
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "provenance")
        self.assertIs(c["ok"], True, c["details"])

    def test_lifecycle_store_validates_green(self):
        conn = _lifecycle_store()
        report = validate.validate(conn, project="o", repo="r")
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])

    def test_review_with_orphan_parent_is_info_not_error(self):
        # a review whose parent PR is out of window: part_of is spine -> INFO.
        conn = _lifecycle_store()
        graphstore.upsert_node(conn, "o/r#review-99-1", "o", "r", "social",
                               "2026-05-03T00:00:00Z",
                               {"author": "x", "state": "approved",
                                "url": "https://github.com/o/r/pull/99#r1"})
        graphstore.upsert_edge(conn, "o/r#review-99-1", "o/r#pr-99", "part_of")
        report = validate.validate(conn, project="o", repo="r")
        c = _check(report, "referential_integrity")
        self.assertIs(c["ok"], True, c["details"])
        self.assertTrue(any(d.get("severity") == "INFO" and "pr-99" in str(d)
                            for d in c["details"]), c["details"])


# --- BUG 1: person nodes for ALL participants (live-audit regression) ---------

def _participants_bundle():
    """In-memory bundle exercising the 4 participant classes the OLD write path
    dropped (person-node creation was driven by attribute_people_areas =
    commit-authors + reviewers of MAPPED-commit PRs only, while contribution
    edges were written for EVERY participant -> dangling edges, no person node):

      1. pure commenter      : comments only, never authors/reviews/commits.
      2. issue reporter      : opens an issue, otherwise no contribution.
      3. PR author w/ commits OUT OF WINDOW (no commit row in the bundle).
      4. reviewer of a PR with NO mapped commits (no commit message-resolves it).

    Plus alice, a real contributor (commit author) so the bundle is non-trivial.
    """
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                 "to": "2026-05-31"},
        "commits": [
            # alice's commit message-resolves to PR 1 (she is a real contributor).
            {"sha": "a" * 40, "author": "alice", "date": "2026-05-02",
             "message": "feat (#1)", "files": ["avm/res/x/main.bicep"]},
        ],
        "prs": [
            {"number": 1, "author": "alice", "reviewers": ["reviewer-mapped"],
             "merged": True, "merged_at": "2026-05-03",
             "comments_list": [{"author": "pure-commenter", "body": "nice"}],
             "url": "https://github.com/o/r/pull/1"},
            # PR 2: author's commits are out of window (no commit row here); its
            # reviewer reviews a PR with NO mapped commits.
            {"number": 2, "author": "oow-author",
             "reviewers": ["reviewer-unmapped"],
             "merged": True, "merged_at": "2026-05-04", "url": "https://github.com/o/r/pull/2"},
        ],
        "issues": [
            {"number": 7, "author": "issue-reporter", "url": "https://github.com/o/r/issues/7"},
        ],
        "code_graph": {"areas": [
            {"id": "avm/res/x", "paths": ["avm/res/x/main.bicep"]}]},
    }


_DROPPED_LOGINS = ("pure-commenter", "issue-reporter", "oow-author",
                   "reviewer-unmapped")


def _fold_participants():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    bundle = _participants_bundle()
    gather.fold_bundle(conn, bundle)
    return conn, bundle


class TestAllParticipants(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_all_participants_get_person_nodes(self):
        conn, _ = _fold_participants()
        for login in _DROPPED_LOGINS:
            node = graphstore.get_node(conn, graphstore.qualify_person("o", login))
            self.assertIsNotNone(node, "missing person node for {}".format(login))
            self.assertEqual(node["data"]["login"], login)

    def test_participants_validate_clean(self):
        conn, bundle = _fold_participants()
        report = validate.validate(conn, project="o", repo="r", bundle=bundle)
        # The two checks BUG 1 broke must now pass (no dangling person edges, every
        # raw login has a person node), and the whole audit is clean.
        for name in ("participant_completeness", "referential_integrity",
                     "no_drift"):
            c = _check(report, name)
            self.assertIs(c["ok"], True, (name, c["details"]))
        self.assertIs(report.ok, True, [c for c in report.checks if not c["ok"]])

    def test_participants_appear_in_extract_people(self):
        import extract
        conn, _ = _fold_participants()
        out = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        for login in _DROPPED_LOGINS:
            self.assertIn(login, out["people"], login)


# --- BUG 2: no_drift symbol-artifact id reconstruction (auditor false positive) -

def _symbol_artifact_bundle():
    """A bundle with a SYMBOL/comment artifact whose stored id is the double-`#`
    form `{path}#{lang}:{subkind}:{name}` and whose name carries `:`, parens and
    commas. `validate._local` (parse_id) rpartitions on the LAST `#`, truncating
    the local to `{lang}:{subkind}:{name}` — so no_drift used to FALSELY report
    the artifact "derived but not stored". The graph is fine; only the auditor's
    id reconstruction was wrong."""
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                 "to": "2026-05-31"},
        "commits": [
            {"sha": "a" * 40, "author": "alice", "date": "2026-05-02",
             "message": "feat (#1)", "files": ["avm/res/x/main.bicep"]},
        ],
        "prs": [{"number": 1, "author": "alice", "merged": True,
                 "merged_at": "2026-05-03", "url": "https://github.com/o/r/pull/1"}],
        "issues": [],
        "symbol_events": [
            # a real symbol whose name is plain.
            {"commit": "a" * 40, "author": "alice", "date": "2026-05-02",
             "path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "param", "name": "location", "change": "add",
             "before": None, "after": "param location string"},
            # a comment whose NAME (the comment text) contains : , ( ) — the
            # adversarial id case.
            {"commit": "a" * 40, "author": "alice", "date": "2026-05-02",
             "path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "comment",
             "name": "TODO: handle (edge, cases): foo:bar", "change": "add",
             "before": None, "after": "// TODO: handle (edge, cases): foo:bar"},
        ],
        "code_graph": {"areas": [
            {"id": "avm/res/x", "paths": ["avm/res/x/main.bicep"]}]},
    }


class TestSymbolArtifactNoDrift(unittest.TestCase):
    def test_no_drift_no_false_positive_on_symbol_artifact(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = _symbol_artifact_bundle()
        gather.fold_bundle(conn, bundle)
        report = validate.validate(conn, project="o", repo="r", bundle=bundle)
        c = _check(report, "no_drift")
        # No "derived but not stored" (or any) artifact drift for the symbol/comment.
        bad = [d for d in c["details"]
               if d.get("kind") == "artifact" and d.get("severity") == "ERROR"]
        self.assertEqual(bad, [], bad)
        self.assertIs(c["ok"], True, c["details"])


# --- CLI ----------------------------------------------------------------------

def _write_store(tmp_path, name="bundle_p3b.json"):
    db = os.path.join(str(tmp_path), "store.db")
    conn = graphstore.open_store(db)
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, _bundle(name))
    conn.close()
    return db


def _run_cli(*args):
    here = os.path.dirname(__file__)
    return subprocess.run(
        [sys.executable, os.path.join(here, "validate.py"), *args],
        capture_output=True, text=True, cwd=here)


class _TmpDirCase(unittest.TestCase):
    """Gives each test a fresh temporary directory (the stdlib stand-in for the
    pytest ``tmp_path`` fixture these tests were originally written against)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = tmp.name


class TestCli(_TmpDirCase):
    def test_cli_passing_store_exits_zero(self):
        db = _write_store(self.tmp_path)
        r = _run_cli(db, "--project", "o", "--repo", "r")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue("OK" in r.stdout or "ok" in r.stdout.lower(), r.stdout)

    def test_cli_failing_store_exits_nonzero(self):
        db = _write_store(self.tmp_path)
        conn = graphstore.open_store(db)
        conn.execute("DELETE FROM nodes WHERE id=?", ("o#person-carol",))
        conn.commit()
        conn.close()
        r = _run_cli(db, "--project", "o", "--repo", "r")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("participant_completeness", r.stdout)
        self.assertIn("carol", r.stdout)

    def test_cli_with_bundle_runs_drift(self):
        db = _write_store(self.tmp_path)
        r = _run_cli(db, "--project", "o", "--repo", "r",
                     "--bundle", os.path.join(FIX, "bundle_p3b.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no_drift", r.stdout)


class TestValidateProject(unittest.TestCase):
    def test_two_member_store_validates_green(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo in ("Azure/mod-a", "Azure/mod-b"):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31",
                          "base_branch": "main"},
                 "prs": [], "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo,
                               members={"Azure/mod-a", "Azure/mod-b"})
        report = validate.validate_project(conn, "proj",
                                           ["Azure/mod-a", "Azure/mod-b"])
        self.assertTrue(report["ok"])
        self.assertEqual({r["repo"] for r in report["members"]},
                         {"Azure/mod-a", "Azure/mod-b"})
        self.assertTrue(all(r["ok"] for r in report["members"]))

    def test_empty_repos_raises_rather_than_vacuous_ok(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        with self.assertRaisesRegex(ValueError, "no repos"):
            validate.validate_project(conn, "proj", [])

    def _two_member_store_with_distinct_authors(self):
        # alice is active only in mod-a, bob only in mod-b. People are PROJECT-scoped
        # so both person nodes exist for the project; a PER-MEMBER no_drift would
        # flag the "foreign" author as "stored but not derivable" for the other repo.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/mod-a", "Azure/mod-b"}
        def pr(n, author, repo):
            return {"number": n, "url": "https://github.com/%s/pull/%d" % (repo, n),
                    "state": "closed", "merged": True,
                    "base": "main", "head": "h%d" % n,
                    "merged_at": "2026-01-1%dT00:00:00Z" % (n % 10),
                    "created_at": "2026-01-0%dT00:00:00Z" % (n % 10),
                    "closed_at": "2026-01-1%dT00:00:00Z" % (n % 10),
                    "closes": [], "crossref_issues": [], "title": "feat", "body": "",
                    "author": author}
        for repo, author, num in (("Azure/mod-a", "alice", 1), ("Azure/mod-b", "bob", 2)):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31", "base_branch": "main"},
                 "prs": [pr(num, author, repo)], "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo, members=members)
        return conn

    def test_per_member_no_drift_does_not_flag_foreign_authors(self):
        # The real-data failure: validating mod-b must NOT report alice (a mod-a
        # author, stored project-wide) as "stored but not derivable".
        conn = self._two_member_store_with_distinct_authors()
        report = validate.validate_project(conn, "proj",
                                           ["Azure/mod-a", "Azure/mod-b"])
        self.assertTrue(report["ok"], report)
        self.assertTrue(all(r["ok"] for r in report["members"]))
        # the project-wide people check is present and green
        names = {c["name"] for c in report["project_checks"]}
        self.assertIn("no_drift_people", names)
        self.assertTrue(all(c["ok"] for c in report["project_checks"]))
        # both authors are stored as project people
        people = validate._stored_project_people(conn, "proj")
        self.assertEqual(set(people), {"alice", "bob"})


class TestValidateProjectAggregation(unittest.TestCase):
    def test_one_failing_member_makes_aggregate_not_ok(self):
        """Monkeypatch validate_repo so one repo returns a failing Report and
        assert that validate_project aggregates ok=False correctly."""
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)

        class _Rep:
            def __init__(self, ok):
                self._ok = ok

            @property
            def ok(self):
                return self._ok

            def to_dict(self):
                return {"ok": self._ok, "checks": []}

        import validate as _v
        orig = _v.validate_repo
        _v.validate_repo = lambda c, p, r, **kw: _Rep(r != "Azure/bad")
        try:
            report = _v.validate_project(conn, "proj",
                                         ["Azure/good", "Azure/bad"])
        finally:
            _v.validate_repo = orig

        self.assertFalse(report["ok"])
        by_repo = {m["repo"]: m["ok"] for m in report["members"]}
        self.assertEqual(by_repo, {"Azure/good": True, "Azure/bad": False})


def _write_two_member_store(tmp_path):
    """Write a two-member (Azure/mod-a, Azure/mod-b) store to a temp file and
    return the db path.  Mirrors _write_store() but for a multi-repo project."""
    db = os.path.join(str(tmp_path), "multi_store.db")
    conn = graphstore.open_store(db)
    graphstore.init_schema(conn)
    for repo in ("Azure/mod-a", "Azure/mod-b"):
        b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                      "from": "2026-01-01", "to": "2026-01-31",
                      "base_branch": "main"},
             "prs": [], "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        gather.fold_bundle(conn, b, project="proj", repo=repo,
                           members={"Azure/mod-a", "Azure/mod-b"})
    conn.close()
    return db


class TestCliMultiRepo(_TmpDirCase):
    def test_cli_multi_repo_text_exits_zero(self):
        """Regression test for the line-794 format crash: the multi-repo TEXT path
        must complete without raising and must exit 0 for a clean two-member store."""
        db = _write_two_member_store(self.tmp_path)
        r = _run_cli(db, "--project", "proj")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Azure/mod-a", r.stdout)
        self.assertIn("Azure/mod-b", r.stdout)

    def test_cli_multi_repo_json_exits_zero_and_has_members(self):
        """The multi-repo JSON path exits 0, parses cleanly, and carries both
        repos in the 'members' list."""
        db = _write_two_member_store(self.tmp_path)
        r = _run_cli(db, "--project", "proj", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("members", data)
        repos = {m["repo"] for m in data["members"]}
        self.assertEqual(repos, {"Azure/mod-a", "Azure/mod-b"})


class TestArtifactStatusRoundTrip(unittest.TestCase):
    """#64: the write path (fold_bundle) and read path (no_drift re-derive) use
    the same derive.build_artifacts, so the last-event status rule round-trips:
    a renamed-then-re-added path is `live`, a rename to an untracked path is
    `removed` with no successor."""

    def _bundle(self):
        def ev(change, path, n, old_path=None):
            e = {"commit": ("%x" % n) * 40, "author": "alice",
                 "date": "2026-05-%02d" % n, "change": change, "path": path}
            if old_path:
                e["old_path"] = old_path
            return e
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                ev("add", "docs/a.md", 1),
                ev("rename", "docs/b.md", 2, old_path="docs/a.md"),
                ev("add", "docs/a.md", 3),
                ev("add", "docs/z.md", 4),
                ev("rename", "src/x.txt", 5, old_path="docs/z.md"),
            ],
        }

    def test_status_stored_and_no_drift(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = self._bundle()
        gather.fold_bundle(conn, bundle)
        stored = {}
        for n in graphstore.repo_nodes(conn, "o", "r", "code"):
            local = graphstore.local_id(n["id"], "o", "r")
            if graphstore.is_artifact_local(local):
                stored[local] = n["data"]
        self.assertEqual((stored["art:docs/a.md"]["status"],
                          stored["art:docs/a.md"]["replaced_by"]), ("live", None))
        self.assertEqual((stored["art:docs/z.md"]["status"],
                          stored["art:docs/z.md"]["replaced_by"]), ("removed", None))
        for b in (bundle, None):
            report = validate.validate(conn, project="o", repo="r", bundle=b)
            c = _check(report, "no_drift")
            self.assertTrue(c["ok"], c["details"])


# --- issue #79: no false passes, every check reported, read-only, no crashes --

EXPECTED_CHECKS = ["target", "referential_integrity", "participant_completeness",
                   "provenance", "no_fabrication", "schema_conformance",
                   "meta_integrity", "edge_set", "no_drift", "idempotency"]


def _failed_names(checks):
    return sorted(c["name"] for c in checks if not c["ok"])


class TestNoFalsePass(unittest.TestCase):
    """Each scenario used to print `RESULT: OK` with no_drift/idempotency silently
    dropped. It must now fail on a named check, and list every expected check."""

    def assert_all_checks_listed(self, report):
        self.assertEqual([c["name"] for c in report.checks], EXPECTED_CHECKS)

    def test_clean_store_lists_every_check(self):
        report = validate.validate(_clean_store(), project="o", repo="r")
        self.assert_all_checks_listed(report)
        self.assertIs(report.ok, True, _failed_names(report.checks))

    def test_unknown_repo_fails_target(self):
        report = validate.validate(_clean_store(), project="o", repo="typo")
        self.assert_all_checks_listed(report)
        self.assertIs(report.ok, False)
        self.assertEqual(_failed_names(report.checks),
                         ["edge_set", "idempotency", "no_drift", "target"])
        target = _check(report, "target")
        self.assertIn("typo", json.dumps(target["details"]))
        self.assertIn("could not self-source",
                      json.dumps(_check(report, "no_drift")["details"]))

    def test_unknown_project_fails_target(self):
        report = validate.validate(_clean_store(), project="nope", repo="r")
        self.assert_all_checks_listed(report)
        self.assertIs(_check(report, "target")["ok"], False)
        self.assertIs(report.ok, False)

    def test_unknown_project_without_repo_fails_target(self):
        report = validate.validate(_clean_store(), project="nope")
        self.assert_all_checks_listed(report)
        self.assertIs(_check(report, "target")["ok"], False)

    def test_zero_node_store_fails_with_and_without_target(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for kw in ({"project": "o", "repo": "r"}, {}):
            report = validate.validate(conn, **kw)
            self.assert_all_checks_listed(report)
            self.assertIs(report.ok, False, kw)
            self.assertIs(_check(report, "target")["ok"], False, kw)

    def test_null_ts_store_cannot_self_source(self):
        conn = _clean_store()
        conn.execute("UPDATE nodes SET ts=NULL")
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        self.assert_all_checks_listed(report)
        self.assertIs(report.ok, False)
        for name in ("edge_set", "no_drift", "idempotency"):
            c = _check(report, name)
            self.assertIs(c["ok"], False, c)
            self.assertIn("could not self-source", json.dumps(c["details"]))

    def test_repo_case_variant_resolves_to_stored_spelling(self):
        report = validate.validate(_clean_store(), project="o", repo="R")
        self.assertIs(report.ok, True, _failed_names(report.checks))

    def test_quiet_member_still_runs_drift_and_idempotency(self):
        # A member with only structure nodes (no activity in its window) is a
        # legitimate store: it self-sources from its recorded gathered window.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo in ("Azure/mod-a", "Azure/mod-b"):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31"},
                 "prs": [], "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo,
                               members={"Azure/mod-a", "Azure/mod-b"})
        agg = validate.validate_project(conn, "proj", ["Azure/mod-a", "Azure/mod-b"])
        self.assertTrue(agg["ok"], agg)
        for m in agg["members"]:
            self.assertEqual([c["name"] for c in m["checks"]], EXPECTED_CHECKS)

    def test_validate_project_unknown_member_fails(self):
        agg = validate.validate_project(_clean_store(), "o", ["r", "ghost"])
        self.assertFalse(agg["ok"])
        by_repo = {m["repo"]: m["ok"] for m in agg["members"]}
        self.assertEqual(by_repo, {"r": True, "ghost": False})
        people = agg["project_checks"][0]
        self.assertEqual(people["name"], "no_drift_people")
        self.assertIs(people["ok"], False)
        self.assertIn("could not self-source", json.dumps(people["details"]))


class TestCorruptRowsReported(unittest.TestCase):
    def test_corrupt_edge_json_is_a_failed_check_naming_the_row(self):
        conn = _clean_store()
        conn.execute("UPDATE edges SET data='{not json' WHERE src_id=? AND "
                     "dst_id=? AND edge_type='authored'",
                     ("o#person-alice", "o/r#pr-42"))
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c["name"] for c in report.checks], EXPECTED_CHECKS)
        self.assertIs(report.ok, False)
        c = _check(report, "referential_integrity")
        self.assertIs(c["ok"], False)
        self.assertIn("o/r#pr-42", json.dumps(c["details"]))

    def test_list_typed_node_blob_is_a_failed_check_naming_the_row(self):
        conn = _clean_store()
        conn.execute("UPDATE nodes SET data='[1, 2]' WHERE id='o/r#pr-42'")
        conn.commit()
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c["name"] for c in report.checks], EXPECTED_CHECKS)
        self.assertIs(report.ok, False)
        for name in ("provenance", "no_drift", "idempotency"):
            c = _check(report, name)
            self.assertIs(c["ok"], False, name)
            self.assertIn("o/r#pr-42", json.dumps(c["details"]), name)

    def test_every_check_is_guarded(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")
        names = ["check_referential_integrity", "check_participant_completeness",
                 "check_provenance", "check_no_fabrication",
                 "check_schema_conformance", "check_edge_set"]
        for fn in names:
            orig = getattr(validate, fn)
            setattr(validate, fn, boom)
            try:
                report = validate.validate(_clean_store(), project="o", repo="r")
            finally:
                setattr(validate, fn, orig)
            self.assertEqual([c["name"] for c in report.checks], EXPECTED_CHECKS)
            self.assertEqual(_failed_names(report.checks),
                             [fn[len("check_"):]], fn)


class TestReadOnlyOpen(_TmpDirCase):
    def test_open_store_readonly_missing_path_raises_and_creates_nothing(self):
        path = os.path.join(self.tmp_path, "nosuch.db")
        with self.assertRaises(FileNotFoundError):
            graphstore.open_store_readonly(path)
        self.assertFalse(os.path.exists(path))

    def test_open_store_readonly_rejects_writes(self):
        db = _write_store(self.tmp_path)
        conn = graphstore.open_store_readonly(db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0,
                         True)
        with self.assertRaises(Exception):
            conn.execute("DELETE FROM nodes")

    def test_dead_ref_readers_do_not_create_the_table(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        conn.execute("DROP TABLE dead_refs")
        self.assertFalse(graphstore.is_dead_ref(conn, "o/r#issue-1"))
        self.assertEqual(graphstore.get_dead_refs(conn), [])
        graphstore.traverse_spine(conn, ["o/r#pr-1"], skip_dead=True)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='dead_refs'").fetchone())


def _sha256(path):
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestCliIssue79(_TmpDirCase):
    def _raw(self, db, sql):
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def assert_fails_on(self, r, name):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("[FAIL] {}".format(name), r.stdout)
        self.assertIn("RESULT: FAIL", r.stdout)

    def test_cli_false_pass_scenarios_exit_one(self):
        db = _write_store(self.tmp_path)
        self.assert_fails_on(_run_cli(db, "--project", "o", "--repo", "typo"),
                             "target")
        self.assert_fails_on(_run_cli(db, "--project", "nope", "--repo", "r"),
                             "target")
        empty = os.path.join(self.tmp_path, "empty.db")
        conn = graphstore.open_store(empty)
        graphstore.init_schema(conn)
        conn.close()
        self.assert_fails_on(_run_cli(empty, "--project", "o", "--repo", "r"),
                             "target")
        self.assert_fails_on(_run_cli(empty), "target")
        self._raw(db, "UPDATE nodes SET ts=NULL")
        r = _run_cli(db, "--project", "o", "--repo", "r")
        self.assert_fails_on(r, "no_drift")
        self.assert_fails_on(r, "idempotency")

    def test_cli_missing_store_exits_two_and_creates_nothing(self):
        path = os.path.join(self.tmp_path, "nosuch.db")
        r = _run_cli(path)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("nosuch.db", r.stderr)
        self.assertFalse(os.path.exists(path))

    def test_cli_ambiguous_project_is_a_usage_error(self):
        db = _write_store(self.tmp_path)
        conn = graphstore.open_store(db)
        graphstore.upsert_node(conn, "p2/x#pr-1", "p2", "x", "social",
                               "2026-05-02T00:00:00Z", {"number": 1})
        conn.close()
        r = _run_cli(db)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("--project", r.stderr)

    def test_cli_not_a_store_exits_two(self):
        path = os.path.join(self.tmp_path, "notes.txt")
        with open(path, "w") as fh:
            fh.write("not a database\n")
        r = _run_cli(path)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_cli_audit_leaves_store_byte_identical(self):
        db = _write_store(self.tmp_path)
        before = _sha256(db)
        r = _run_cli(db, "--project", "o", "--repo", "r")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_sha256(db), before)

    def test_cli_store_without_dead_refs_is_byte_identical(self):
        db = _write_store(self.tmp_path)
        self._raw(db, "DROP TABLE dead_refs")
        before = _sha256(db)
        r = _run_cli(db, "--project", "o", "--repo", "r")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_sha256(db), before)
        import sqlite3
        conn = sqlite3.connect(db)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='dead_refs'").fetchone())
        conn.close()

    def test_cli_corrupt_rows_produce_text_and_json_reports(self):
        db = _write_store(self.tmp_path)
        self._raw(db, "UPDATE edges SET data='{not json' WHERE src_id='o#person-alice' "
                      "AND dst_id='o/r#pr-42' AND edge_type='authored'")
        self.assert_fails_on(_run_cli(db, "--project", "o", "--repo", "r"),
                             "referential_integrity")
        self._raw(db, "UPDATE nodes SET data='[1, 2]' WHERE id='o/r#pr-42'")
        self.assert_fails_on(_run_cli(db, "--project", "o", "--repo", "r"),
                             "provenance")
        r = _run_cli(db, "--project", "o", "--repo", "r", "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        self.assertIs(data["ok"], False)
        self.assertEqual([c["name"] for c in data["checks"]], EXPECTED_CHECKS)
        self.assertIn("o/r#pr-42", r.stdout)


if __name__ == "__main__":
    unittest.main()
