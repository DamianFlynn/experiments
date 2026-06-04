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


def test_no_drift_skipped_without_bundle():
    conn = _clean_store()
    report = validate.validate(conn, project="o", repo="r")
    names = {c["name"] for c in report.checks}
    assert "no_drift" not in names
    assert "idempotency" not in names


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
