"""Readers open the store read-only (issue #107 item 7).

view.py, digest.py, spotlight.py (every query without a live --complete
backfill) and ci_spotlight_integration.py only read the store, so they open
it with graphstore.open_store_readonly (#79): a missing path is never
created and any write raises. Each reader leaves the store file's bytes
identical, on a store in WAL mode and on one in rollback-journal mode
without a `dead_refs` table (which a reader must not create). Opening a
non-WAL store read-write used to switch it to WAL (`open_store` sets the
journal mode), rewriting its header.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ci_spotlight_integration  # noqa: E402
import digest  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402
import view  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _bundle():
    with open(os.path.join(FIX, "bundle_p3b.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _write_store(path, wal):
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(_bundle()))
    conn.commit()
    if not wal:
        conn.execute("DROP TABLE IF EXISTS dead_refs")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()


def _digest_bytes(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(*args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _readers(store, tmp):
    """(name, callable) for every reader invocation under test."""
    out = os.path.join(tmp, "view.json")
    win = ["--from", "2026-05-01", "--to", "2026-05-31"]
    calls = [
        ("view", lambda: view.main(["--store", store, "--out", out] + win)),
        ("digest", lambda: digest.main(["--store", store, "--project", "o"]
                                       + win)),
        ("ci_spotlight_integration",
         lambda: ci_spotlight_integration.main(["--store", store])),
    ]
    for query, arg in (("person", "alice"), ("grep", "firewall"),
                       ("subsystem", "docs"), ("module", "docs"),
                       ("symbol", "main"), ("dependents", "o/r")):
        calls.append(("spotlight " + query,
                      lambda q=query, a=arg: spotlight.main(
                          [q, a, "--store", store, "--md"])))
    # --complete without a token stays offline: still read-only
    calls.append(("spotlight person --complete (no token)",
                  lambda: spotlight.main(["person", "alice", "--store", store,
                                          "--complete"])))
    return calls


class TestReadersLeaveTheStoreUntouched(unittest.TestCase):
    def _check(self, wal):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store.db")
            _write_store(store, wal)
            conn = sqlite3.connect(store)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            has_dead = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dead_refs'").fetchone()
            conn.close()
            self.assertEqual(mode, "wal" if wal else "delete")
            self.assertEqual(bool(has_dead), wal)
            before = _digest_bytes(store)
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            with mock.patch.dict(os.environ, env, clear=True):
                for name, call in _readers(store, tmp):
                    code, _out, err = _quiet(call)
                    self.assertIn(code, (0, 1), (name, code, err))
                    self.assertEqual(_digest_bytes(store), before, name)
            conn = sqlite3.connect("file:{}?mode=ro".format(store), uri=True)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dead_refs'"
            ).fetchone() if not wal else None)
            conn.close()

    def test_wal_store_bytes_identical(self):
        self._check(wal=True)

    def test_rollback_store_without_dead_refs_bytes_identical(self):
        self._check(wal=False)

    def test_readers_open_read_only(self):
        """Every reader goes through open_store_readonly, never open_store."""
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store.db")
            _write_store(store, wal=True)
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(graphstore, "open_store",
                                      side_effect=AssertionError("read-write")):
                for name, call in _readers(store, tmp):
                    code, _out, err = _quiet(call)
                    self.assertIn(code, (0, 1), (name, code, err))

    def test_spotlight_complete_with_token_opens_read_write(self):
        """A live --complete backfill writes fetched anchors: it keeps the
        read-write open (the fake fetch seam keeps it offline)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store.db")
            _write_store(store, wal=True)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "dummy"}), \
                    mock.patch.object(gather, "make_backfill_fetcher",
                                      return_value=lambda *_a: None), \
                    mock.patch.object(graphstore, "open_store_readonly",
                                      side_effect=AssertionError("read-only")):
                code, _out, err = _quiet(spotlight.main, [
                    "person", "alice", "--store", store, "--complete"])
            self.assertEqual(code, 0, err)

    def test_missing_store_still_exit_2_and_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.db")
            for name, call in _readers(missing, tmp):
                code, _out, err = _quiet(call)
                self.assertEqual(code, 2, name)
                self.assertIn("does not exist", err, name)
                self.assertFalse(os.path.exists(missing), name)


class TestFts5ProbeOnReadOnlyConnection(unittest.TestCase):
    """FTS5 support is a build property: probed first on a read-only reader
    connection it must not read as absent (and stay cached that way)."""

    def test_probe_does_not_depend_on_a_writable_connection(self):
        with mock.patch.object(graphstore, "_FTS5_CACHE", None):
            want = graphstore.fts5_available(sqlite3.connect(":memory:"))
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store.db")
            _write_store(store, wal=False)
            before = _digest_bytes(store)
            conn = graphstore.open_store_readonly(store)
            try:
                with mock.patch.object(graphstore, "_FTS5_CACHE", None):
                    self.assertEqual(graphstore.fts5_available(conn), want)
            finally:
                conn.close()
            self.assertEqual(_digest_bytes(store), before)


if __name__ == "__main__":
    unittest.main()
