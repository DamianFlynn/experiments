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
import unittest

import graphstore
import gather
import validate

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

def test_clean_store_passes_all_checks():
    conn = _clean_store()
    report = validate.validate(conn, project="o", repo="r")
    assert report.ok is True, [c for c in report.checks if not c["ok"]]
    # every individual check is ok
    for c in report.checks:
        assert c["ok"], c


def test_clean_store_with_bundle_passes_drift_and_idempotency():
    conn = _clean_store()
    report = validate.validate(conn, project="o", repo="r", bundle=_bundle())
    assert report.ok is True, [c for c in report.checks if not c["ok"]]
    names = {c["name"] for c in report.checks}
    assert "no_drift" in names
    assert "idempotency" in names


def test_project_repo_autodetected_from_store():
    conn = _clean_store()
    report = validate.validate(conn)  # no project/repo passed
    assert report.ok is True, [c for c in report.checks if not c["ok"]]


# --- corruption catches -------------------------------------------------------

def test_delete_carol_person_fails_participant_completeness():
    conn = _clean_store()
    conn.execute("DELETE FROM nodes WHERE id=?", ("o#person-carol",))
    conn.commit()
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "participant_completeness")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("carol" in str(d) for d in c["details"]), c["details"]
    assert report.ok is False


def test_delete_authored_edge_fails_participant_completeness():
    conn = _clean_store()
    conn.execute(
        "DELETE FROM edges WHERE edge_type='authored' AND src_id=? AND dst_id=?",
        ("o#person-alice", "o/r#pr-42"))
    conn.commit()
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "participant_completeness")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("alice" in str(d) and "authored" in str(d) for d in c["details"]), \
        c["details"]


def test_dangling_nonspine_edge_fails_referential_integrity():
    conn = _clean_store()
    graphstore.upsert_edge(conn, "o#person-alice", "o/r#pr-9999", "authored")
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "referential_integrity")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("pr-9999" in str(d) for d in c["details"]), c["details"]


def test_dangling_spine_edge_is_not_error():
    conn = _clean_store()
    # A PR that closes an out-of-window (not-yet-gathered) issue: legitimate
    # backfill case. closes is a spine edge -> INFO, not ERROR.
    graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#issue-9999", "closes")
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "referential_integrity")
    assert c["ok"] is True, c["details"]
    # but it IS surfaced as INFO / missing thread
    assert any(d.get("severity") == "INFO" and "issue-9999" in str(d)
               for d in c["details"]), c["details"]


def test_strip_pr_url_and_number_fails_provenance():
    conn = _clean_store()
    node = graphstore.get_node(conn, "o/r#pr-42")
    data = dict(node["data"])
    data.pop("url", None)
    data.pop("number", None)
    graphstore.upsert_node(conn, node["id"], node["project"], node["repo"],
                           node["node_class"], node["ts"], data)
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "provenance")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("pr-42" in str(d) for d in c["details"]), c["details"]


def test_orphan_person_fails_no_fabrication():
    conn = _clean_store()
    # A person with no contribution edge whatsoever = invented contributor.
    graphstore.upsert_node(conn, "o#person-ghost", "o", "*", "structure",
                           None, {"login": "ghost", "modules": [], "areas": []})
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "no_fabrication")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("ghost" in str(d) for d in c["details"]), c["details"]


def test_mutated_person_data_fails_no_drift_with_bundle():
    conn = _clean_store()
    node = graphstore.get_node(conn, "o#person-carol")
    data = dict(node["data"])
    data["areas"] = ["totally-made-up-area"]  # diverge from re-derivation
    graphstore.upsert_node(conn, node["id"], node["project"], node["repo"],
                           node["node_class"], node["ts"], data)
    report = validate.validate(conn, project="o", repo="r", bundle=_bundle())
    c = _check(report, "no_drift")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"
    assert any("carol" in str(d) for d in c["details"]), c["details"]


def test_no_drift_self_sourced_without_bundle():
    """Store-only world (Phase 7): with no --bundle, no_drift + idempotency
    SELF-SOURCE the raw bundle from the store via extract and still run (and
    pass on a clean store), so the trust gate is self-contained on a store."""
    conn = _clean_store()
    report = validate.validate(conn, project="o", repo="r")
    names = {c["name"] for c in report.checks}
    assert "no_drift" in names
    assert "idempotency" in names
    assert _check(report, "no_drift")["ok"] is True, _check(report, "no_drift")
    assert _check(report, "idempotency")["ok"] is True, _check(report, "idempotency")
    assert report.ok is True, [c for c in report.checks if not c["ok"]]


def test_empty_id_node_fails_no_fabrication():
    conn = _clean_store()
    conn.execute(
        "INSERT INTO nodes (id, project, repo, node_class, ts, data, fetched_at) "
        "VALUES ('', 'o', 'r', 'social', NULL, '{}', 'now')")
    conn.commit()
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "no_fabrication")
    assert c["ok"] is False


def test_schema_conformance_catches_wrong_authored_endpoint():
    conn = _clean_store()
    # authored src must be a person- id. Point one at a non-person src.
    graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#pr-42", "authored")
    report = validate.validate(conn, project="o", repo="r")
    c = _check(report, "schema_conformance")
    assert c["ok"] is False
    assert c["severity"] == "ERROR"


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
             "url": "https://x/1"},
            # PR 2: author's commits are out of window (no commit row here); its
            # reviewer reviews a PR with NO mapped commits.
            {"number": 2, "author": "oow-author",
             "reviewers": ["reviewer-unmapped"],
             "merged": True, "merged_at": "2026-05-04", "url": "https://x/2"},
        ],
        "issues": [
            {"number": 7, "author": "issue-reporter", "url": "https://x/i7"},
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


def test_all_participants_get_person_nodes():
    conn, _ = _fold_participants()
    for login in _DROPPED_LOGINS:
        node = graphstore.get_node(conn, graphstore.qualify_person("o", login))
        assert node is not None, "missing person node for {}".format(login)
        assert node["data"]["login"] == login


def test_participants_validate_clean():
    conn, bundle = _fold_participants()
    report = validate.validate(conn, project="o", repo="r", bundle=bundle)
    # The two checks BUG 1 broke must now pass (no dangling person edges, every
    # raw login has a person node), and the whole audit is clean.
    for name in ("participant_completeness", "referential_integrity", "no_drift"):
        c = _check(report, name)
        assert c["ok"] is True, (name, c["details"])
    assert report.ok is True, [c for c in report.checks if not c["ok"]]


def test_participants_appear_in_extract_people():
    import extract
    conn, _ = _fold_participants()
    out = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
    for login in _DROPPED_LOGINS:
        assert login in out["people"], login


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
                 "merged_at": "2026-05-03", "url": "https://x/1"}],
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


def test_no_drift_no_false_positive_on_symbol_artifact():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    bundle = _symbol_artifact_bundle()
    gather.fold_bundle(conn, bundle)
    report = validate.validate(conn, project="o", repo="r", bundle=bundle)
    c = _check(report, "no_drift")
    # No "derived but not stored" (or any) artifact drift for the symbol/comment.
    bad = [d for d in c["details"]
           if d.get("kind") == "artifact" and d.get("severity") == "ERROR"]
    assert bad == [], bad
    assert c["ok"] is True, c["details"]


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


def test_cli_passing_store_exits_zero(tmp_path):
    db = _write_store(tmp_path)
    r = _run_cli(db, "--project", "o", "--repo", "r")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout or "ok" in r.stdout.lower()


def test_cli_failing_store_exits_nonzero(tmp_path):
    db = _write_store(tmp_path)
    conn = graphstore.open_store(db)
    conn.execute("DELETE FROM nodes WHERE id=?", ("o#person-carol",))
    conn.commit()
    conn.close()
    r = _run_cli(db, "--project", "o", "--repo", "r")
    assert r.returncode != 0
    assert "participant_completeness" in r.stdout
    assert "carol" in r.stdout


def test_cli_with_bundle_runs_drift(tmp_path):
    db = _write_store(tmp_path)
    r = _run_cli(db, "--project", "o", "--repo", "r",
                 "--bundle", os.path.join(FIX, "bundle_p3b.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no_drift" in r.stdout


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
        def pr(n, author):
            return {"number": n, "url": "u/%d" % n, "state": "closed", "merged": True,
                    "base": "main", "head": "h%d" % n,
                    "merged_at": "2026-01-1%dT00:00:00Z" % (n % 10),
                    "created_at": "2026-01-0%dT00:00:00Z" % (n % 10),
                    "closed_at": "2026-01-1%dT00:00:00Z" % (n % 10),
                    "closes": [], "crossref_issues": [], "title": "feat", "body": "",
                    "author": author}
        for repo, author, num in (("Azure/mod-a", "alice", 1), ("Azure/mod-b", "bob", 2)):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31", "base_branch": "main"},
                 "prs": [pr(num, author)], "issues": [], "commits": [], "code_events": [],
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


def test_cli_multi_repo_text_exits_zero(tmp_path):
    """Regression test for the line-794 format crash: the multi-repo TEXT path
    must complete without raising and must exit 0 for a clean two-member store."""
    db = _write_two_member_store(tmp_path)
    r = _run_cli(db, "--project", "proj")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Azure/mod-a" in r.stdout
    assert "Azure/mod-b" in r.stdout


def test_cli_multi_repo_json_exits_zero_and_has_members(tmp_path):
    """The multi-repo JSON path exits 0, parses cleanly, and carries both
    repos in the 'members' list."""
    db = _write_two_member_store(tmp_path)
    r = _run_cli(db, "--project", "proj", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert "members" in data
    repos = {m["repo"] for m in data["members"]}
    assert repos == {"Azure/mod-a", "Azure/mod-b"}
