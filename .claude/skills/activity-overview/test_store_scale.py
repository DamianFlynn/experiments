"""Store performance and atomicity (issue #55).

- FTS re-indexing must not scan the whole `fts_text` table per node (fold time
  was quadratic in store size).
- The prefix filters in `edges_by_type` / `repo_code_events` must be
  index-backed and case-sensitive.
- A fold is one transaction: an exception mid-fold leaves the store unchanged.
- A store created by schema v1 is migrated in place by `init_schema`, and a v1
  store folded WITHOUT `init_schema` (validate's in-memory idempotency copy)
  still indexes text without duplicates.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _has_fts5():
    return graphstore.fts5_available(_store())


def _commit_bundle(start, n, owner="o", repo="r"):
    return {
        "meta": {"owner": owner, "repo": repo,
                 "from": "2026-01-01", "to": "2026-01-31"},
        "commits": [{"sha": "%040x" % i, "author": "a", "date": "2026-01-15",
                     "message": "commit message {} fixes thing".format(i),
                     "files": []}
                    for i in range(start, start + n)],
    }


def _plans(conn, statements):
    """EXPLAIN QUERY PLAN detail strings for each traced statement."""
    out = []
    for sql in statements:
        out.append([r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)])
    return out


def _trace(conn, fn):
    """Run fn() and return the (parameter-expanded) SQL it executed."""
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return seen


# ---------------------------------------------------------------- problem 1

@unittest.skipUnless(_has_fts5(), "SQLite build lacks FTS5")
class TestFoldScalesLinearly(unittest.TestCase):
    """6,000 commit nodes folded as six 1,000-commit bundles. The work for a
    1,000-node fold into a store that already holds 5,000 nodes must stay within
    3x the work for the same size fold into an empty store. Before the fix the
    per-node FTS delete scanned the whole fts_text table, so the last fold cost
    ~10x the first."""

    BATCH = 1000
    BATCHES = 6

    def test_vm_steps_last_fold_within_3x_first(self):
        # Deterministic: count SQLite VM steps (progress handler every 100 ops)
        # instead of wall time, so the assertion cannot flake under load.
        conn = _store()
        steps = []
        counter = [0]

        def tick():
            counter[0] += 1
            return 0
        for b in range(self.BATCHES):
            counter[0] = 0
            conn.set_progress_handler(tick, 100)
            gather.fold_bundle(conn, _commit_bundle(b * self.BATCH, self.BATCH))
            conn.set_progress_handler(None, 0)
            steps.append(counter[0])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM nodes WHERE node_class='code'"
                         ).fetchone()[0],
            self.BATCH * self.BATCHES)
        self.assertLess(steps[-1], 3 * steps[0], steps)
        first_half = sum(steps[:self.BATCHES // 2])
        second_half = sum(steps[self.BATCHES // 2:])
        self.assertLess(second_half, 3 * first_half, steps)

    def test_wall_time_refold_into_large_store_within_3x_small(self):
        # Wall-clock companion: re-fold the same 1,000-commit bundle (idempotent,
        # same work each time) into the store at 1,000 nodes and again at
        # 6,000 nodes; best of 3 each to damp scheduler noise.
        conn = _store()
        first = _commit_bundle(0, self.BATCH)
        gather.fold_bundle(conn, first)

        def best_of(k):
            best = None
            for _ in range(k):
                t = time.perf_counter()
                gather.fold_bundle(conn, first)
                d = time.perf_counter() - t
                best = d if best is None else min(best, d)
            return best
        small = best_of(3)
        for b in range(1, self.BATCHES):
            gather.fold_bundle(conn, _commit_bundle(b * self.BATCH, self.BATCH))
        large = best_of(3)
        self.assertLess(large, 3 * small, (small, large))


# ---------------------------------------------------------------- query plans

def _full_scans(plans):
    """Plan lines that read a whole table: `SCAN <table>` (any index-ordered
    full scan included) or an FTS5 scan with no constraint (`INDEX 0:`)."""
    bad = []
    for plan in plans:
        for line in plan:
            if line.startswith("SCAN "):
                bad.append(line)
    return bad


class TestQueryPlans(unittest.TestCase):
    @unittest.skipUnless(_has_fts5(), "SQLite build lacks FTS5")
    def test_index_text_reindex_does_no_full_scan(self):
        conn = _store()
        graphstore.index_text(conn, "p/r#pr-1", "alpha")
        stmts = _trace(conn, lambda: graphstore.index_text(conn, "p/r#pr-1", "beta"))
        lookups = [s for s in stmts
                   if s.lstrip().upper().startswith(("DELETE", "SELECT", "UPDATE"))]
        self.assertTrue(lookups, stmts)
        plans = _plans(conn, lookups)
        for plan in plans:
            for line in plan:
                # An FTS5 rowid lookup plans as `SCAN fts_text VIRTUAL TABLE
                # INDEX 0:=`; the old unconstrained scan was `INDEX 0:` (empty
                # idxStr). Any other `SCAN` is a real-table full scan.
                # `SCAN sqlite_master` (the one-row-per-schema-object catalogue,
                # read once per call to check the fts_map migration) does not
                # grow with the data and is allowed.
                if line == "SCAN sqlite_master":
                    continue
                if "VIRTUAL TABLE" in line:
                    self.assertNotRegex(line, r"INDEX \d+:$", (stmts, plans))
                else:
                    self.assertFalse(line.startswith("SCAN "), (stmts, plans))
        self.assertEqual(graphstore.fts_search(conn, "beta"), ["p/r#pr-1"])
        self.assertEqual(graphstore.fts_search(conn, "alpha"), [])

    def test_edges_by_type_is_index_backed(self):
        conn = _store()
        stmts = _trace(conn, lambda: graphstore.edges_by_type(conn, "blocks", "proj"))
        plans = _plans(conn, [s for s in stmts if s.lstrip().upper().startswith("SELECT")])
        self.assertTrue(plans)
        self.assertEqual(_full_scans(plans), [], plans)
        self.assertTrue(any(l.startswith("SEARCH edges") for p in plans for l in p), plans)

    def test_repo_code_events_is_index_backed(self):
        conn = _store()
        stmts = _trace(conn, lambda: graphstore.repo_code_events(conn, "proj", "o/r"))
        plans = _plans(conn, [s for s in stmts if s.lstrip().upper().startswith("SELECT")])
        self.assertTrue(plans)
        self.assertEqual(_full_scans(plans), [], plans)
        self.assertTrue(any(l.startswith("SEARCH code_events") for p in plans for l in p),
                        plans)


# ---------------------------------------------------------------- case / prefix

class TestPrefixFiltersCaseSensitive(unittest.TestCase):
    def setUp(self):
        self.conn = _store()
        graphstore.upsert_edges(self.conn, [
            ("azure/r#issue-1", "azure/r#issue-2", "blocks", None, None),
            ("Azure/r#issue-1", "Azure/r#issue-2", "blocks", None, None),
            ("a_b/r#issue-1", "a_b/r#issue-2", "blocks", None, None),
            ("axb/r#issue-1", "axb/r#issue-2", "blocks", None, None),
            ("azure-x/r#issue-1", "azure-x/r#issue-2", "blocks", None, None),
            ("azure/r#issue-3", "azure/r#issue-4", "closes", None, None),
        ])
        graphstore.add_code_events(self.conn, [
            ("azure/o/r#main.tf", "add", "s1", None, "2026-01-01", None, None, None, None, None),
            ("Azure/o/r#main.tf", "add", "s2", None, "2026-01-01", None, None, None, None, None),
            ("azure/o/rr#main.tf", "add", "s3", None, "2026-01-01", None, None, None, None, None),
            ("azure/o/r#b.tf", "add", "s4", None, "2026-01-01", None, None, None, None, None),
            ("a%b/o/r#x.tf", "add", "s5", None, "2026-01-01", None, None, None, None, None),
            ("aXb/o/r#x.tf", "add", "s6", None, "2026-01-01", None, None, None, None, None),
        ])

    def _srcs(self, project):
        return [e["src_id"] for e in graphstore.edges_by_type(self.conn, "blocks", project)]

    def test_edges_by_type_does_not_mix_case(self):
        self.assertEqual(self._srcs("Azure"), ["Azure/r#issue-1"])
        self.assertEqual(self._srcs("azure"), ["azure/r#issue-1"])

    def test_edges_by_type_metacharacters_are_literal(self):
        self.assertEqual(self._srcs("a_b"), ["a_b/r#issue-1"])
        self.assertEqual(self._srcs("azure-x"), ["azure-x/r#issue-1"])
        self.assertEqual(self._srcs("a"), [])

    def test_repo_code_events_does_not_mix_case(self):
        got = [e["artifact_id"] for e in graphstore.repo_code_events(self.conn, "Azure", "o/r")]
        self.assertEqual(got, ["Azure/o/r#main.tf"])
        got = [e["commit_sha"] for e in graphstore.repo_code_events(self.conn, "azure", "o/r")]
        self.assertEqual(got, ["s1", "s4"])  # insertion order, not rr, not Azure

    def test_repo_code_events_metacharacters_are_literal(self):
        got = [e["artifact_id"] for e in graphstore.repo_code_events(self.conn, "a%b", "o/r")]
        self.assertEqual(got, ["a%b/o/r#x.tf"])


# ---------------------------------------------------------------- problem 3

def _fold_bundle_v2():
    return {
        "meta": {"owner": "acme", "repo": "widget", "from": "2026-02-01",
                 "to": "2026-02-28", "clone_sha": "cafe"},
        "prs": [{"number": 11, "url": "u/11", "state": "closed", "merged": True,
                 "title": "second pr", "body": "does stuff",
                 "merged_at": "2026-02-10T00:00:00Z",
                 "created_at": "2026-02-05T00:00:00Z",
                 "closed_at": "2026-02-10T00:00:00Z", "closes": [5]}],
        "issues": [{"number": 5, "url": "u/5", "state": "closed", "title": "bug",
                    "body": "broken", "closed_at": "2026-02-10T00:00:00Z",
                    "updated_at": "2026-02-10T00:00:00Z"}],
        "commits": [{"sha": "b" * 40, "author": "dev", "date": "2026-02-09",
                     "message": "fix bug (#11)", "files": ["main.tf"]}],
        "code_events": [{"commit": "b" * 40, "author": "dev", "date": "2026-02-09",
                         "change": "modify", "path": "main.tf"}],
    }


def _fold_bundle_v1():
    b = _fold_bundle_v2()
    b["meta"].update({"from": "2026-01-01", "to": "2026-01-31", "clone_sha": "beef"})
    b["prs"][0].update({"number": 10, "title": "first pr", "closes": [4]})
    b["issues"][0].update({"number": 4})
    b["commits"][0].update({"sha": "a" * 40, "message": "first (#10)"})
    b["code_events"][0].update({"commit": "a" * 40, "change": "add"})
    return b


def _snapshot(conn):
    snap = {}
    tables = ["nodes", "edges", "code_events", "meta", "dead_refs"]
    if graphstore.fts5_available(conn):
        tables.append("fts_text")
    for t in tables:
        snap[t] = sorted(tuple(r) for r in conn.execute("SELECT * FROM {}".format(t)))
    return snap


class TestFoldIsAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "store.db")
        conn = graphstore.open_store(self.path)
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_bundle_v1())
        self.before = _snapshot(conn)
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_unchanged(self):
        # A fresh connection sees only what was committed.
        other = graphstore.open_store(self.path)
        try:
            self.assertEqual(_snapshot(other), self.before)
        finally:
            other.close()
        self.assertEqual(_snapshot(self.conn), self.before)
        self.assertFalse(self.conn.in_transaction)

    def test_exception_after_nodes_and_edges_leaves_store_unchanged(self):
        with mock.patch.object(graphstore, "add_code_events",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gather.fold_bundle(self.conn, _fold_bundle_v2())
        self._assert_unchanged()

    def test_exception_at_window_record_leaves_store_unchanged(self):
        # record_window runs after nodes, edges, events and FTS are written.
        with mock.patch.object(graphstore, "record_window",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gather.fold_bundle(self.conn, _fold_bundle_v2())
        self._assert_unchanged()

    def test_successful_fold_after_failed_fold(self):
        with mock.patch.object(graphstore, "record_window",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gather.fold_bundle(self.conn, _fold_bundle_v2())
        gather.fold_bundle(self.conn, _fold_bundle_v2())
        other = graphstore.open_store(self.path)
        try:
            self.assertIsNotNone(graphstore.get_node(other, "acme/widget#pr-11"))
            self.assertEqual(len(graphstore.get_windows(other)), 2)
            self.assertEqual(graphstore.get_clone_sha(other, "acme", "widget"), "cafe")
        finally:
            other.close()

    def test_fold_commits_once(self):
        # One fold = one transaction: the writes are committed together at the end.
        commits = []
        real = self.conn

        class Spy:
            def __getattr__(self, name):
                if name == "commit":
                    def _c():
                        commits.append(1)
                        return real.commit()
                    return _c
                return getattr(real, name)

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                if exc[0] is None:
                    commits.append(1)
                return real.__exit__(*exc)
        gather.fold_bundle(Spy(), _fold_bundle_v2())
        self.assertEqual(len(commits), 1)


# ---------------------------------------------------------------- migration

# The v1 schema exactly as graphstore created it before issue #55.
_V1_SCHEMA = """
CREATE TABLE nodes (id TEXT PRIMARY KEY, project TEXT NOT NULL, repo TEXT NOT NULL,
    node_class TEXT NOT NULL, ts TEXT, data TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE INDEX idx_nodes_window ON nodes (project, repo, node_class, ts);
CREATE INDEX idx_nodes_ts ON nodes (ts);
CREATE TABLE edges (src_id TEXT NOT NULL, dst_id TEXT NOT NULL, edge_type TEXT NOT NULL,
    ts TEXT, data TEXT, PRIMARY KEY (src_id, dst_id, edge_type));
CREATE INDEX idx_edges_src ON edges (src_id, edge_type);
CREATE INDEX idx_edges_dst ON edges (dst_id, edge_type);
CREATE TABLE code_events (artifact_id TEXT NOT NULL, event TEXT NOT NULL,
    commit_sha TEXT NOT NULL, author TEXT, date TEXT, hunk TEXT, ref TEXT,
    before TEXT, after TEXT, detail TEXT, PRIMARY KEY (artifact_id, commit_sha, event));
CREATE INDEX idx_code_events_artifact ON code_events (artifact_id, date);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE dead_refs (id TEXT PRIMARY KEY, project TEXT, reason TEXT, first_seen TEXT);
"""


def _v1_store(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_V1_SCHEMA)
    if graphstore.fts5_available(conn):
        conn.execute("CREATE VIRTUAL TABLE fts_text USING fts5(node_id UNINDEXED, text)")
        conn.executemany("INSERT INTO fts_text (node_id, text) VALUES (?, ?)", [
            ("acme/widget#pr-10", "first pr old text"),
            ("acme/widget#issue-4", "bug broken"),
            # a stray duplicate row (two rows for one node) must collapse
            ("acme/widget#issue-4", "bug broken stale"),
        ])
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('keep', 'me')")
    conn.execute(
        "INSERT INTO edges VALUES ('acme/widget#issue-4', 'acme/widget#issue-9', "
        "'blocks', NULL, NULL)")
    conn.commit()
    return conn


class TestSchemaMigration(unittest.TestCase):
    def test_version_bumped(self):
        self.assertGreaterEqual(graphstore.SCHEMA_VERSION, 2)
        self.assertEqual(graphstore.get_meta(_store(), "schema_version"),
                         str(graphstore.SCHEMA_VERSION))

    def test_init_schema_migrates_v1_store_in_place(self):
        conn = _v1_store()
        graphstore.init_schema(conn)
        self.assertEqual(graphstore.get_meta(conn, "schema_version"),
                         str(graphstore.SCHEMA_VERSION))
        self.assertEqual(graphstore.get_meta(conn, "keep"), "me")
        # existing data still served, via the new index-backed paths
        self.assertEqual(
            [e["dst_id"] for e in graphstore.edges_by_type(conn, "blocks", "acme")],
            ["acme/widget#issue-9"])
        plans = _plans(conn, _trace(
            conn, lambda: graphstore.edges_by_type(conn, "blocks", "acme")))
        self.assertEqual(_full_scans(plans), [], plans)
        # idempotent: a second init_schema changes nothing
        snap = _snapshot(conn)
        graphstore.init_schema(conn)
        self.assertEqual(_snapshot(conn), snap)

    @unittest.skipUnless(_has_fts5(), "SQLite build lacks FTS5")
    def test_migrated_fts_reindexes_without_duplicates(self):
        conn = _v1_store()
        graphstore.init_schema(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM fts_text WHERE node_id=?",
                         ("acme/widget#issue-4",)).fetchone()[0], 1)
        self.assertEqual(graphstore.fts_search(conn, "broken"), ["acme/widget#issue-4"])
        gather.fold_bundle(conn, _fold_bundle_v1())
        for nid in ("acme/widget#pr-10", "acme/widget#issue-4"):
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM fts_text WHERE node_id=?",
                             (nid,)).fetchone()[0], 1, nid)
        self.assertEqual(graphstore.fts_search(conn, "old"), [])
        self.assertEqual(graphstore.fts_search(conn, "stale"), [])

    @unittest.skipUnless(_has_fts5(), "SQLite build lacks FTS5")
    def test_fold_into_v1_store_without_init_schema(self):
        # validate.check_idempotency backs the audited store up into memory and
        # folds there WITHOUT init_schema; an un-migrated v1 store must still
        # re-index cleanly (one FTS row per node).
        conn = _v1_store()
        gather.fold_bundle(conn, _fold_bundle_v1())
        gather.fold_bundle(conn, _fold_bundle_v1())
        dup = conn.execute(
            "SELECT node_id, COUNT(*) FROM fts_text GROUP BY node_id HAVING COUNT(*) > 1"
        ).fetchall()
        self.assertEqual([tuple(r) for r in dup], [])
        self.assertEqual(graphstore.fts_search(conn, "stale"), [])

    def test_file_store_v1_migrates_and_validates(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "v1.db")
            _v1_store(path).close()
            conn = graphstore.open_store(path)
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, _fold_bundle_v1())
            conn.close()
            conn = graphstore.open_store(path)
            self.assertEqual(graphstore.get_meta(conn, "schema_version"),
                             str(graphstore.SCHEMA_VERSION))
            report = validate.check_idempotency(conn, _fold_bundle_v1())
            self.assertTrue(report["ok"], report)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestIdempotencyProbeSingleTransaction(unittest.TestCase):
    def test_check_idempotency_on_folded_store(self):
        conn = _store()
        gather.fold_bundle(conn, _fold_bundle_v1())
        gather.fold_bundle(conn, _fold_bundle_v2())
        report = validate.check_idempotency(conn, _fold_bundle_v2())
        self.assertTrue(report["ok"], report)
        self.assertFalse(conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
