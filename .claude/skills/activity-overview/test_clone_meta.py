"""`meta.clone_mode` and `meta.boundary_dropped_commits` reach the store and
the view (issue #107 item 5).

gather records how it made the clone (`clone_mode`) and the in-window commits
whose diffs the shallow boundary dropped (`boundary_dropped_commits`). Both used
to stop at the in-memory bundle, so a view could not say its code history was
incomplete. fold now stores them per (project, repo, from, to) inside the fold
transaction (meta `clone:`, the pattern of `ref_date:` / `branches:` /
`facts:`), and extract carries them in the view meta: `clone_mode` for a view
of exactly a gathered window; `boundary_dropped_commits` as the sorted union
over the gathered windows inside the view, omitted when empty.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402
import view  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _golden():
    with open(os.path.join(FIX, "bundle_p3b.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _window(frm, to, clone_mode, dropped):
    """The golden bundle re-windowed to [frm, to], with clone provenance."""
    b = copy.deepcopy(_golden())
    b["meta"].update({"from": frm, "to": to, "ref_date": to,
                      "period": {"from": frm, "to": to}})
    if clone_mode is not None:
        b["meta"]["clone_mode"] = clone_mode
    if dropped is not None:
        b["meta"]["boundary_dropped_commits"] = dropped
    return b


def _store(*bundles):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b))
    conn.commit()
    return conn


def _view(conn, frm, to):
    return extract.extract(conn, "o", "r", frm, to, warn=lambda _m: None)


MAY = ("2026-05-01", "2026-05-31")


class TestStored(unittest.TestCase):
    def test_fold_stores_per_window(self):
        conn = _store(_window(*MAY, "fresh", [SHA_B, SHA_A]))
        self.assertEqual(graphstore.get_clone_info(conn, "o", "r", *MAY),
                         {"clone_mode": "fresh",
                          "boundary_dropped_commits": [SHA_A, SHA_B]})
        self.assertIsNone(graphstore.get_clone_info(
            conn, "o", "r", "2026-04-01", "2026-04-30"))

    def test_refold_of_the_window_replaces_it(self):
        conn = _store(_window(*MAY, "fresh", [SHA_A]),
                      _window(*MAY, "fetched", []))
        self.assertEqual(graphstore.get_clone_info(conn, "o", "r", *MAY),
                         {"clone_mode": "fetched",
                          "boundary_dropped_commits": []})

    def test_bundle_without_the_keys_stores_nothing(self):
        conn = _store(_window(*MAY, None, None))
        self.assertIsNone(graphstore.get_clone_info(conn, "o", "r", *MAY))

    def test_written_inside_the_fold_transaction(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        with mock.patch.object(graphstore, "set_window_facts",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gather.fold_bundle(conn, _window(*MAY, "fresh", [SHA_A]))
        self.assertIsNone(graphstore.get_clone_info(conn, "o", "r", *MAY))


class TestView(unittest.TestCase):
    def test_exact_window_carries_both(self):
        conn = _store(_window(*MAY, "depth1-fallback", [SHA_A]))
        meta = _view(conn, *MAY)["meta"]
        self.assertEqual(meta["clone_mode"], "depth1-fallback")
        self.assertEqual(meta["boundary_dropped_commits"], [SHA_A])

    def test_empty_boundary_list_is_omitted(self):
        meta = _view(_store(_window(*MAY, "fresh", [])), *MAY)["meta"]
        self.assertEqual(meta["clone_mode"], "fresh")
        self.assertNotIn("boundary_dropped_commits", meta)

    def test_wider_view_unions_the_windows_inside_it(self):
        conn = _store(_window("2026-05-01", "2026-05-15", "fresh", [SHA_C, SHA_A]),
                      _window("2026-05-16", "2026-05-31", "fetched", [SHA_B, SHA_A]),
                      _window("2026-06-01", "2026-06-30", "fresh", ["d" * 40]))
        meta = _view(conn, *MAY)["meta"]
        self.assertEqual(meta["boundary_dropped_commits"], [SHA_A, SHA_B, SHA_C])
        self.assertNotIn("clone_mode", meta)        # not one gathered window

    def test_narrower_view_carries_neither(self):
        conn = _store(_window(*MAY, "fresh", [SHA_A]))
        meta = _view(conn, "2026-05-10", "2026-05-20")["meta"]
        self.assertNotIn("clone_mode", meta)
        self.assertNotIn("boundary_dropped_commits", meta)

    def test_store_without_the_keys_view_unchanged(self):
        meta = _view(_store(_window(*MAY, None, None)), *MAY)["meta"]
        self.assertNotIn("clone_mode", meta)
        self.assertNotIn("boundary_dropped_commits", meta)

    def test_view_cli_writes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(store)
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, _window(*MAY, "fresh", [SHA_A]))
            conn.commit()
            conn.close()
            out = os.path.join(tmp, "v.json")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(view.main(["--store", store, "--from", MAY[0],
                                            "--to", MAY[1], "--out", out]), 0)
            with open(out, encoding="utf-8") as fh:
                meta = json.load(fh)["meta"]
            self.assertEqual((meta["clone_mode"], meta["boundary_dropped_commits"]),
                             ("fresh", [SHA_A]))


class TestTrustCheckStaysGreen(unittest.TestCase):
    def test_single_window(self):
        conn = _store(_window(*MAY, "fresh", [SHA_A]))
        rep = validate.validate(conn, "o", "r")
        self.assertTrue(rep.ok, [c for c in rep.checks if not c["ok"]])

    def test_two_windows(self):
        conn = _store(_window("2026-05-01", "2026-05-15", "fresh", [SHA_A]),
                      _window("2026-05-16", "2026-05-31", "fetched", [SHA_B]))
        rep = validate.validate(conn, "o", "r")
        self.assertTrue(rep.ok, [c for c in rep.checks if not c["ok"]])


if __name__ == "__main__":
    unittest.main()
