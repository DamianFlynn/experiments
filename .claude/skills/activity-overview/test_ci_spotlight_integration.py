"""Offline tests for ci_spotlight_integration.py (issue #90).

The live `spotlight` CI job gathers a real AVM week and runs the gate against
it; it cannot run on a pull request. These tests build small stores in a temp
directory from fixture bundles and run the gate's `main` in-process, so a
regression in the gate (a false failure on a correct store, a check that reads
another project's ids, a check that no longer bites) is caught by the unit
suite. No network.
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
import ci_spotlight_integration as gate  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
BUNDLES = sorted(n for n in os.listdir(FIX)
                 if n.startswith("bundle_") and n.endswith(".json"))


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _sym(path, change, sha, author, date, before, after):
    return {"path": path, "lang": "bicep", "subkind": "resource", "name": "fw",
            "change": change, "commit": sha, "author": author, "date": date,
            "before": before, "after": after}


def _commit(sha, date, msg, author="alice"):
    return {"sha": sha, "message": msg, "author": author, "date": date,
            "url": "https://github.com/acme/r1/commit/{}".format(sha)}


def _move_windows():
    """Three windows of one repo: `fw` is created in old.bicep (add, change),
    moved to new.bicep (drop + add, linked as a move), then changed three times
    in new.bicep. The destination of the move has the most ledger events (4
    against the source's 3), so it is the artifact the gate picks for `symbol`
    — and its identity chain starts at the SOURCE (old.bicep), not at the
    artifact itself."""
    def window(frm, to, commits, events):
        return {"meta": {"owner": "acme", "repo": "r1", "from": frm, "to": to},
                "prs": [], "issues": [], "commits": commits,
                "symbol_events": events}
    old, new = "old.bicep", "new.bicep"
    return [
        window("2026-01-01", "2026-01-15",
               [_commit("aaa1111", "2026-01-05T00:00:00Z", "add fw"),
                _commit("bbb2222", "2026-01-10T00:00:00Z", "tweak fw")],
               [_sym(old, "add", "aaa1111", "alice", "2026-01-05T00:00:00Z",
                     None, "v1"),
                _sym(old, "change", "bbb2222", "alice", "2026-01-10T00:00:00Z",
                     "v1", "v2")]),
        window("2026-01-16", "2026-01-31",
               [_commit("ccc3333", "2026-01-20T00:00:00Z", "move fw", "bob")],
               [_sym(old, "drop", "ccc3333", "bob", "2026-01-20T00:00:00Z",
                     "v2", None),
                _sym(new, "add", "ccc3333", "bob", "2026-01-20T00:00:00Z",
                     None, "v2")]),
        window("2026-02-01", "2026-02-28",
               [_commit("ddd4444", "2026-02-05T00:00:00Z", "fw v3"),
                _commit("eee5555", "2026-02-10T00:00:00Z", "fw v4"),
                _commit("fff6666", "2026-02-15T00:00:00Z", "fw v5")],
               [_sym(new, "change", "ddd4444", "alice", "2026-02-05T00:00:00Z",
                     "v2", "v3"),
                _sym(new, "change", "eee5555", "alice", "2026-02-10T00:00:00Z",
                     "v3", "v4"),
                _sym(new, "change", "fff6666", "alice", "2026-02-15T00:00:00Z",
                     "v4", "v5")]),
    ]


def _decoy(bundle, login="zed"):
    """A copy of `bundle` that is RICHER than the original on every discovery
    axis, with every login renamed to `login`: three extra commits by `login`
    touching the firewall-policy area, each with a ledger event on the same
    file. Folded under another project it out-ranks the real project's person,
    area and artifact, so an unscoped discovery query picks the decoy's ids."""
    b = json.loads(json.dumps(bundle).replace('"alice"', json.dumps(login))
                   .replace('"bob"', json.dumps(login))
                   .replace('"carol"', json.dumps(login))
                   .replace('"dave"', json.dumps(login)))
    path = "avm/res/network/firewall-policy/main.bicep"
    for i in range(3):
        sha = "{}".format(i + 5) * 40
        b["commits"].append({"sha": sha, "message": "tune policy module",
                             "pr": None, "author": login,
                             "files": [path]})
        b["code_events"].append({"commit": sha, "author": login,
                                 "date": "2026-05-1{}".format(i + 1),
                                 "change": "modify", "path": path})
    return b


class _GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "store.db")

    def build(self, folds):
        """folds: [(bundle, project, repo)]; project/repo None -> meta."""
        conn = graphstore.open_store(self.db)
        graphstore.init_schema(conn)
        for bundle, project, repo in folds:
            gather.fold_bundle(conn, copy.deepcopy(bundle),
                               project=project, repo=repo)
        conn.commit()
        conn.close()

    def run_gate(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = gate.main(["--store", self.db] + list(extra))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def line(self, out, name):
        """The gate's result line for check `name` ('' when absent)."""
        for ln in out.splitlines():
            if ln.strip()[2:].startswith(name):
                return ln.strip()
        return ""


class TestMoveDestination(_GateCase):
    """Problem 1: the richest artifact is the destination of a move."""

    def setUp(self):
        super().setUp()
        self.build([(b, None, None) for b in _move_windows()])
        self.dst = graphstore.qualify_id("acme", "r1",
                                         "new.bicep#bicep:resource:fw")
        self.src = graphstore.qualify_id("acme", "r1",
                                         "old.bicep#bicep:resource:fw")

    def test_store_shape_is_the_issue_case(self):
        conn = graphstore.open_store(self.db)
        try:
            self.assertEqual(gate._richest_artifact(conn, "acme"), self.dst)
            res = spotlight.pattern_evolution(conn, "acme", self.dst)
        finally:
            conn.close()
        ids = [c["id"] for c in res["identity_chain"]]
        self.assertEqual(ids, [self.src, self.dst])  # head is NOT the artifact

    def test_symbol_check_passes(self):
        code, out, _ = self.run_gate()
        sym = self.line(out, "symbol")
        self.assertTrue(sym.startswith("✓"), out)
        self.assertIn("new.bicep#bicep:resource:fw", sym)
        self.assertIn("4 lifecycle events", sym)
        self.assertNotIn("identity_chain", out)
        self.assertEqual(code, 0, out)


class TestProjectScoping(_GateCase):
    """Problem 2: on a multi-project store, --project inspects only that
    project. The decoys out-rank the target on every discovery axis and their
    names collide with the target under LIKE (`_` wildcard, case-insensitive)."""

    TARGET = "a_c"
    DECOYS = ("abc", "A_C")

    def setUp(self):
        super().setUp()
        base = _load("bundle_p3b.json")
        folds = [(base, self.TARGET, "o/r")]
        folds += [(_decoy(base), p, "o/r") for p in self.DECOYS]
        self.build(folds)

    def test_discovery_reads_only_the_project(self):
        conn = graphstore.open_store(self.db)
        try:
            self.assertEqual(gate._most_active_person(conn, "abc"), "zed")
            login = gate._most_active_person(conn, self.TARGET)
            self.assertNotEqual(login, "zed")
            self.assertIsNotNone(graphstore.get_node(
                conn, graphstore.qualify_person(self.TARGET, login)))
            repo, area, n = gate._most_touched_area(conn, self.TARGET)
            self.assertEqual(repo, "o/r")
            self.assertEqual(area, "avm/res/network/firewall-policy")
            self.assertEqual(n, 1)  # the decoy's area has 4 touches
            aid = gate._richest_artifact(conn, self.TARGET)
            self.assertTrue(aid.startswith(self.TARGET + "/o/r#"), aid)
            self.assertEqual(aid, "a_c/o/r#docs/firewall.md")
        finally:
            conn.close()

    def test_gate_passes_on_the_target_only(self):
        code, out, _ = self.run_gate("--project", self.TARGET, "--require-all")
        self.assertEqual(code, 0, out)
        self.assertNotIn("zed", out)
        self.assertIn("a_c/o/r#docs/firewall.md", self.line(out, "symbol"))

    def test_each_decoy_also_passes_on_its_own(self):
        for p in self.DECOYS:
            code, out, _ = self.run_gate("--project", p, "--require-all")
            self.assertEqual(code, 0, out)
            self.assertIn("person `zed`", out)

    def test_grep_term_is_found_in_the_project(self):
        # A term indexed only in another project must not be chosen.
        conn = graphstore.open_store(self.db)
        try:
            if not graphstore.fts5_available(conn):
                self.skipTest("FTS5 unavailable")
            graphstore.index_text(conn, "abc/o/r#pr-42", "module avm bicep")
            for nid in [r[0] for r in conn.execute(
                    "SELECT node_id FROM fts_map WHERE "
                    "(node_id >= 'a_c/' AND node_id < 'a_c0') OR "
                    "(node_id >= 'a_c#' AND node_id < 'a_c$')")]:
                graphstore.index_text(conn, nid, "nothing generic here")
            term = gate._grep_term(conn, self.TARGET)
            self.assertNotIn(term, gate._GREP_CANDIDATES)
            hits = gate._project_hits(conn, self.TARGET, term)
            self.assertTrue(hits)
        finally:
            conn.close()

    def test_ambiguous_store_without_project_is_usage_error(self):
        code, _, err = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("multiple projects", err)


class TestFixtureStores(_GateCase):
    """Acceptance: the gate exits 0 on a store folded from each fixture
    bundle. A check whose input the store does not hold is reported as skipped
    (named, with the reason); --require-all turns any skip into a failure, which
    is how the live job runs."""

    def test_fixture_bundles_present(self):
        self.assertGreaterEqual(len(BUNDLES), 5, BUNDLES)

    def test_each_fixture_store_exits_zero(self):
        for name in BUNDLES:
            with self.subTest(bundle=name):
                if os.path.exists(self.db):
                    os.remove(self.db)
                self.build([(_load(name), None, None)])
                code, out, _ = self.run_gate()
                self.assertEqual(code, 0, out)
                self.assertNotIn("✗", out)
                for check in ("person", "subsystem", "symbol", "grep"):
                    ln = self.line(out, check)
                    self.assertTrue(ln.startswith(("✓", "-")), (check, out))

    def test_full_fixture_verifies_all_four(self):
        self.build([(_load("bundle_p3b.json"), None, None)])
        code, out, _ = self.run_gate("--require-all")
        self.assertEqual(code, 0, out)
        for check in ("person", "subsystem", "symbol", "grep"):
            self.assertTrue(self.line(out, check).startswith("✓"), out)

    def test_require_all_fails_on_skips(self):
        self.build([(_load("bundle_p3c.json"), None, None)])
        code, out, _ = self.run_gate()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)
        code, out, _ = self.run_gate("--require-all")
        self.assertEqual(code, 1, out)

    def test_deleted_file_reads_removed(self):
        # p3b's docs/firewall.md is added then deleted: the gate checks the
        # outcome against the last ledger event.
        self.build([(_load("bundle_p3b.json"), None, None)])
        _, out, _ = self.run_gate()
        self.assertIn("removed", self.line(out, "symbol"))


class TestChecksBite(_GateCase):
    """The checks fail when spotlight's answer is wrong — not only when the
    store is empty. Each case corrupts one spotlight result."""

    def setUp(self):
        super().setUp()
        self.build([(_load("bundle_p3b.json"), None, None)])

    def _with(self, name, fn):
        real = getattr(spotlight, name)

        def wrapped(*a, **k):
            return fn(real(*a, **k))
        with mock.patch.object(spotlight, name, wrapped):
            return self.run_gate()

    def test_dropped_lifecycle_event_fails(self):
        def drop(res):
            res["delivered"][0]["timeline"].pop()
            return res
        code, out, _ = self._with("pattern_evolution", drop)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "symbol").startswith("✗"), out)

    def test_wrong_symbol_outcome_fails(self):
        def alive(res):
            res["delivered"][0]["outcome"] = "alive"
            return res
        code, out, _ = self._with("pattern_evolution", alive)
        self.assertEqual(code, 1)
        self.assertIn("outcome", self.line(out, "symbol"))

    def test_unknown_train_outcome_fails(self):
        def merged(res):
            for t in res["delivered"]:
                t["outcome"] = "merged"
            return res
        code, out, _ = self._with("person_impact", merged)
        self.assertEqual(code, 1)
        self.assertIn("outcome", self.line(out, "person"))

    def test_foreign_repo_on_train_fails(self):
        def foreign(res):
            res["delivered"][0]["repos"] = ["other/repo"]
            return res
        code, out, _ = self._with("subsystem_split", foreign)
        self.assertEqual(code, 1)
        self.assertIn("repo", self.line(out, "subsystem"))

    def test_grep_leaking_another_project_fails(self):
        def leak(res):
            for t in res["delivered"][:1]:
                t["touchpoints"].append(
                    {"id": "zz/o/r#pr-1", "role": "mention", "number": 1})
            return res
        code, out, _ = self._with("text_mining", leak)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "grep").startswith("✗"), out)

    def test_grep_missing_a_match_fails(self):
        def lose(res):
            for t in res["delivered"]:
                t["touchpoints"] = []
            return res
        code, out, _ = self._with("text_mining", lose)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "grep").startswith("✗"), out)


class TestCli(_GateCase):
    def test_unknown_project_is_usage_error(self):
        self.build([(_load("bundle_p3b.json"), None, None)])
        code, out, err = self.run_gate("--project", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        self.assertEqual(out, "")

    def test_missing_store_is_usage_error_and_not_created(self):
        code, out, err = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
