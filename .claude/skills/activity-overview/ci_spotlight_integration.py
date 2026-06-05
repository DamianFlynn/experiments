#!/usr/bin/env python3
"""CI integration gate: run all four `spotlight` queries against a REAL gathered
journey-graph store and assert each returns a correct, cited, non-trivial result.

This is the "build a db and query it, exactly as real execution does" check — not
a mock and not a crafted fixture. The CI workflow gathers a fixed historical AVM
window (early March 2026 — historical, so the data is stable) into the store; this
script then exercises person / subsystem / symbol / grep over it.

Query arguments are *discovered* from the store (the most-active person, the most-
touched area, the artifact with the richest lifecycle, a frequent search term) so
the gate verifies real behaviour without hard-coding brittle ids — yet still
asserts real, cited, non-empty results. Exits non-zero with a clear message on the
first failed expectation.

Usage: python3 ci_spotlight_integration.py --store PATH [--project NAME]
"""

import argparse
import json
import sys

import graphstore
import spotlight

_CONTRIB = ("authored", "reviewed", "merged", "reported", "commented")
# search terms common to AVM discussion/code; the first with FTS hits is used.
_GREP_CANDIDATES = ("module", "avm", "bicep", "resource", "version", "fix")


class CheckError(AssertionError):
    """A failed integration expectation (printed, then exit 1)."""


def _require(cond, msg):
    if not cond:
        raise CheckError(msg)


def _is_cited(row):
    """A row/touchpoint carries a source citation."""
    return any(row.get(k) is not None
               for k in ("url", "number", "sha", "ref_sha"))


def _train_has_cited_timeline(train):
    return any(_is_cited(r) for r in train.get("timeline", []))


# --------------------------------------------------------------------------
# argument discovery (deterministic) over the real store
# --------------------------------------------------------------------------

def _most_active_person(conn, project):
    rows = conn.execute(
        "SELECT src_id, COUNT(*) c FROM edges "
        "WHERE edge_type IN ('authored','reviewed','merged','reported','commented') "
        "AND src_id LIKE ? GROUP BY src_id ORDER BY c DESC, src_id",
        ("{}#person-%".format(project),)).fetchall()
    if not rows:
        return None
    pid = rows[0][0]
    return pid.split("#person-", 1)[1]


def _most_touched_area(conn, project):
    rows = conn.execute(
        "SELECT dst_id, COUNT(*) c FROM edges WHERE edge_type='touches' "
        "AND dst_id LIKE '%#area-%' GROUP BY dst_id ORDER BY c DESC, dst_id"
    ).fetchall()
    if not rows:
        # fall back to any area node
        r = conn.execute(
            "SELECT id FROM nodes WHERE id LIKE '%#area-%' ORDER BY id LIMIT 1"
        ).fetchone()
        if not r:
            return None
        return r[0].split("#area-", 1)[1]
    return rows[0][0].split("#area-", 1)[1]


def _richest_artifact(conn):
    rows = conn.execute(
        "SELECT artifact_id, COUNT(*) c FROM code_events "
        "GROUP BY artifact_id ORDER BY c DESC, artifact_id").fetchall()
    return rows[0][0] if rows else None


def _grep_term(conn):
    if not graphstore.fts5_available(conn):
        return None
    for term in _GREP_CANDIDATES:
        if graphstore.fts_search(conn, '"{}"'.format(term)):
            return term
    return None


# --------------------------------------------------------------------------
# per-query checks
# --------------------------------------------------------------------------

def check_person(conn, project):
    login = _most_active_person(conn, project)
    _require(login is not None, "no person nodes in the store — gather produced no contributors")
    res = spotlight.person_impact(conn, project, login)
    _require(res["status"] == "ok", "person({}): status={}".format(login, res["status"]))
    _require(len(res["delivered"]) > 0, "person({}): no delivered trains".format(login))
    _require(any(_train_has_cited_timeline(t) for t in res["delivered"]),
             "person({}): no train carries a cited timeline".format(login))
    _require(any(_is_cited(tp) for t in res["delivered"] for tp in t["touchpoints"]),
             "person({}): no touchpoint carries a citation".format(login))
    # determinism: identical JSON across two runs
    a = json.dumps(spotlight.person_impact(conn, project, login), sort_keys=True)
    b = json.dumps(spotlight.person_impact(conn, project, login), sort_keys=True)
    _require(a == b, "person({}): non-deterministic output".format(login))
    return "person `{}`: {} trains, cited ✓ (deterministic)".format(
        login, len(res["delivered"]))


def check_subsystem(conn, project):
    area = _most_touched_area(conn, project)
    _require(area is not None, "no area nodes in the store")
    res = spotlight.subsystem_split(conn, project, area)
    _require(res["status"] == "ok", "subsystem({}): status={}".format(area, res["status"]))
    _require(len(res["delivered"]) > 0,
             "subsystem({}): no delivered trains for the most-touched area".format(area))
    _require(all(isinstance(t.get("timeline"), list) for t in res["delivered"]),
             "subsystem({}): a train has no timeline".format(area))
    _require(any(_train_has_cited_timeline(t) for t in res["delivered"]),
             "subsystem({}): no train carries a cited timeline".format(area))
    return "subsystem `{}`: {} trains, {} contributors, cited ✓".format(
        area, len(res["delivered"]), len(res["contributors"]))


def check_symbol(conn, project):
    aid = _richest_artifact(conn)
    _require(aid is not None, "no code_events in the store — gather walked no commits")
    res = spotlight.pattern_evolution(conn, project, aid)
    _require(res["status"] == "ok", "symbol({}): status={}".format(aid, res["status"]))
    _require(len(res["delivered"]) == 1, "symbol: expected exactly one lifecycle train")
    tl = res["delivered"][0]["timeline"]
    _require(len(tl) >= 1, "symbol({}): empty lifecycle".format(aid))
    _require(all("kind" in r for r in tl), "symbol: a lifecycle row lacks 'kind'")
    _require(all(_is_cited(r) for r in tl), "symbol: a lifecycle row lacks a commit citation")
    _require(res["identity_chain"] and res["identity_chain"][0]["id"] == aid,
             "symbol: identity_chain head is not the artifact")
    return "symbol `{}`: {} lifecycle events, cited ✓".format(
        aid.split("#", 1)[-1], len(tl))


def check_grep(conn, project):
    if not graphstore.fts5_available(conn):
        raise CheckError("FTS5 unavailable in this build — cannot verify grep on real data")
    term = _grep_term(conn)
    _require(term is not None,
             "no FTS matches for any common term — fts_text was not populated by the gather")
    res = spotlight.text_mining(conn, project, term)
    _require(res["status"] == "ok", "grep({}): status={}".format(term, res["status"]))
    _require(res["summary"]["matches"] > 0, "grep({}): zero matches".format(term))
    _require(len(res["delivered"]) > 0, "grep({}): matches but no delivered trains".format(term))
    _require(all(_train_has_cited_timeline(t) for t in res["delivered"]),
             "grep({}): a delivered train has no cited timeline".format(term))
    # input hardening on real data: an operator/quote-bearing phrase must not raise
    hardened = spotlight.text_mining(conn, project, 'a AND "b" :c -d')
    _require(hardened["status"] == "ok", "grep input-hardening: operator phrase did not return ok")
    return "grep `{}`: {} matches across {} trains, cited ✓ (input-hardened)".format(
        term, res["summary"]["matches"], len(res["delivered"]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="spotlight CI integration gate over a real store")
    ap.add_argument("--store", required=True)
    ap.add_argument("--project", default=None)
    args = ap.parse_args(argv)

    conn = graphstore.open_store(args.store)
    project = args.project or spotlight._detect_project(conn, None)
    print("spotlight integration gate — project '{}', store {}".format(project, args.store))

    checks = (("person", check_person), ("subsystem", check_subsystem),
              ("symbol", check_symbol), ("grep", check_grep))
    failed = 0
    for name, fn in checks:
        try:
            print("  ✓ {}".format(fn(conn, project)))
        except CheckError as e:
            failed += 1
            print("  ✗ {}: {}".format(name, e))
    conn.close()

    if failed:
        print("\nFAILED: {}/{} spotlight queries did not verify on real data".format(
            failed, len(checks)))
        return 1
    print("\nOK: all four spotlight queries verified on real gathered data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
