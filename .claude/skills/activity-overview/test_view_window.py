"""Issue #75 — a view carries its window; buckets are bounded; --ref-date kept.

Before the fix `extract._materialize_meta` emitted only owner/repo/from/to, so
`link.compute_buckets` (which read `meta.period` with no fallback) treated
every stored item as in-window, and `--ref-date` given to gather was dropped at
fold. These tests pin the fixed behaviour through the store path:

- a view (extract, view.py, digest member bundles) carries `meta.period` and
  `meta.ref_date`;
- buckets are limited to the view window, also on a store holding two months;
- `ref_date` is persisted per gathered window at fold and read back for the
  matching window; view.py / digest.py `--ref-date` override it; otherwise it
  is the window's `to`.

Offline: stores are built in memory / a temp dir via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import view  # noqa: E402
from _quiet import quiet  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
MAY = ("2026-05-01", "2026-05-31")
JUNE = ("2026-06-01", "2026-06-30")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _open():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _sample_plus_old_items():
    """fixtures/bundle_sample.json (window May 2026, PR 42 closes issue 17) plus
    three issues gather really does pull into a May window (state=all by
    updated_at, plus every open item) that do NOT belong to May's buckets."""
    b = _load("bundle_sample.json")
    b["issues"] += [
        # closed completed in 2024, commented on in May 2026
        {"number": 900, "title": "old done", "state": "closed",
         "state_reason": "completed", "created_at": "2023-12-01T00:00:00Z",
         "closed_at": "2024-01-05T00:00:00Z",
         "updated_at": "2026-05-20T00:00:00Z", "url": "u/900"},
        # closed not_planned in 2024, commented on in May 2026
        {"number": 901, "title": "old rejected", "state": "closed",
         "state_reason": "not_planned", "created_at": "2023-12-01T00:00:00Z",
         "closed_at": "2024-03-01T00:00:00Z",
         "updated_at": "2026-05-10T00:00:00Z", "url": "u/901"},
        # open, last touched in 2023, on no milestone
        {"number": 902, "title": "stale open", "state": "open",
         "created_at": "2023-01-01T00:00:00Z",
         "updated_at": "2023-06-01T00:00:00Z", "url": "u/902"},
    ]
    return b


def _ids(bucket, type_):
    return sorted(r["id"] for r in bucket if r["type"] == type_)


def _all_bucketed(buckets):
    return sorted((r["type"], r["id"]) for refs in buckets.values() for r in refs)


def _month_bundle(pr, frm, to, ref_date=None):
    meta = {"owner": "o", "repo": "r", "from": frm, "to": to,
            "base_branch": "main"}
    if ref_date is not None:
        meta["ref_date"] = ref_date
    merged = "{}T12:00:00Z".format(frm[:8] + "10")
    return {
        "meta": meta,
        "prs": [{"number": pr, "title": "pr {}".format(pr), "state": "closed",
                 "merged": True, "base": "main", "head": "f{}".format(pr),
                 "created_at": merged, "merged_at": merged, "closed_at": merged,
                 "updated_at": merged, "closes": [], "url": "u/{}".format(pr)}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [],
    }


class SingleWindowBuckets(unittest.TestCase):
    """The issue's confirmed repro: one window, store path."""

    def setUp(self):
        quiet(self)
        self.conn = _open()
        gather.fold_bundle(self.conn, _sample_plus_old_items())

    def tearDown(self):
        self.conn.close()

    def _enriched(self):
        return link.enrich(extract.extract(self.conn, "o", "r", *MAY))

    def test_view_meta_carries_period(self):
        meta = extract.extract(self.conn, "o", "r", *MAY)["meta"]
        self.assertEqual(meta["period"], {"from": MAY[0], "to": MAY[1]})

    def test_shipped_issues_only_in_window(self):
        b = self._enriched()
        self.assertEqual(_ids(b["buckets"]["shipped"], "issue"), [17])

    def test_old_rejected_and_stale_open_land_in_no_bucket(self):
        bucketed = _all_bucketed(self._enriched()["buckets"])
        for num in (900, 901, 902):
            self.assertNotIn(("issue", num), bucketed)

    def test_view_cli_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            conn = graphstore.open_store(db)
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, _sample_plus_old_items())
            conn.close()
            out = os.path.join(tmp, "bundle.json")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(view.main(["--store", db, "--from", MAY[0],
                                            "--to", MAY[1], "--out", out]), 0)
            with open(out, encoding="utf-8") as fh:
                b = link.enrich(json.load(fh))
        self.assertEqual(b["meta"]["period"], {"from": MAY[0], "to": MAY[1]})
        self.assertEqual(_ids(b["buckets"]["shipped"], "issue"), [17])


class TwoMonthsInOneStore(unittest.TestCase):
    def setUp(self):
        self.conn = _open()
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY))
        gather.fold_bundle(self.conn, _month_bundle(1042, *JUNE))

    def tearDown(self):
        self.conn.close()

    def _shipped_prs(self, window):
        b = link.enrich(extract.extract(self.conn, "o", "r", *window))
        return _ids(b["buckets"]["shipped"], "pr")

    def test_each_month_ships_only_its_own_merges(self):
        self.assertEqual(self._shipped_prs(MAY), [42])
        self.assertEqual(self._shipped_prs(JUNE), [1042])

    def test_wide_view_ships_both(self):
        self.assertEqual(self._shipped_prs((MAY[0], JUNE[1])), [42, 1042])

    def test_digest_member_bundles_are_bounded(self):
        members = digest.member_bundles(self.conn, "o", ["r"], *MAY,
                                        warn=lambda _m: None)
        b = members[0]["bundle"]
        self.assertEqual(b["meta"]["period"], {"from": MAY[0], "to": MAY[1]})
        self.assertEqual(_ids(b["buckets"]["shipped"], "pr"), [42])
        view_ = digest.build_project_view(self.conn, "o", ["r"], *JUNE,
                                          warn=lambda _m: None)
        self.assertEqual(sorted(s["id"] for s in view_["shipped"]
                                if s["type"] == "pr"), [1042])


class RefDate(unittest.TestCase):
    def setUp(self):
        self.conn = _open()

    def tearDown(self):
        self.conn.close()

    def _meta(self, window=MAY, **kw):
        return extract.extract(self.conn, "o", "r", *window, **kw)["meta"]

    def test_default_ref_date_is_to(self):
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY))
        self.assertEqual(self._meta()["ref_date"], MAY[1])

    def test_gathered_ref_date_is_stored_and_read_back(self):
        # acquire() puts `--ref-date` into meta.ref_date; fold persists it.
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY,
                                                    ref_date="2026-05-15"))
        self.assertEqual(self._meta()["ref_date"], "2026-05-15")
        self.assertEqual(
            graphstore.get_ref_date(self.conn, "o", "r", *MAY), "2026-05-15")

    def test_stored_ref_date_is_per_window(self):
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY,
                                                    ref_date="2026-05-15"))
        gather.fold_bundle(self.conn, _month_bundle(1042, *JUNE))
        self.assertEqual(self._meta(MAY)["ref_date"], "2026-05-15")
        self.assertEqual(self._meta(JUNE)["ref_date"], JUNE[1])
        # A window that was never gathered as such falls back to its own `to`.
        self.assertEqual(self._meta((MAY[0], JUNE[1]))["ref_date"], JUNE[1])

    def test_refold_without_ref_date_resets_to_to(self):
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY,
                                                    ref_date="2026-05-15"))
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY))
        self.assertEqual(self._meta()["ref_date"], MAY[1])

    def test_explicit_override_wins(self):
        gather.fold_bundle(self.conn, _month_bundle(42, *MAY,
                                                    ref_date="2026-05-15"))
        self.assertEqual(self._meta(ref_date="2026-05-20")["ref_date"],
                         "2026-05-20")

    def test_ref_date_drives_milestone_selection(self):
        b = _month_bundle(42, *MAY, ref_date="2026-05-10")
        b["milestones"] = [
            {"number": 1, "title": "M1", "state": "open",
             "due_on": "2026-05-12T00:00:00Z"},
            {"number": 2, "title": "M2", "state": "open",
             "due_on": "2026-05-25T00:00:00Z"},
        ]
        gather.fold_bundle(self.conn, b)
        bundle = extract.extract(self.conn, "o", "r", *MAY)
        cur, _nxt = link.select_milestones(bundle["milestones"],
                                           bundle["meta"]["ref_date"])
        self.assertEqual(cur["title"], "M1")


class RefDateCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "s.db")
        conn = graphstore.open_store(self.db)
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _month_bundle(42, *MAY, ref_date="2026-05-15"))
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _view(self, *extra):
        out = os.path.join(self._tmp.name, "bundle.json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = view.main(["--store", self.db, "--from", MAY[0],
                                  "--to", MAY[1], "--out", out] + list(extra))
            except SystemExit as exc:
                code = exc.code
        if code != 0:
            return code, None
        with open(out, encoding="utf-8") as fh:
            return code, json.load(fh)["meta"]

    def _digest(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                code = digest.main(["--store", self.db, "--project", "o",
                                    "--from", MAY[0], "--to", MAY[1]]
                                   + list(extra))
            except SystemExit as exc:
                code = exc.code
        if code != 0:
            return code, None
        return code, json.loads(buf.getvalue())

    def test_view_reads_stored_ref_date(self):
        self.assertEqual(self._view()[1]["ref_date"], "2026-05-15")

    def test_view_ref_date_flag_overrides(self):
        self.assertEqual(self._view("--ref-date", "2026-05-20")[1]["ref_date"],
                         "2026-05-20")

    def test_view_rejects_bad_ref_date(self):
        self.assertEqual(self._view("--ref-date", "2026-13-40")[0], 2)
        self.assertEqual(self._view("--ref-date", "soon")[0], 2)

    def test_digest_reads_stored_ref_date(self):
        code, out = self._digest()
        self.assertEqual(code, 0)
        self.assertEqual(out["members"][0]["bundle"]["meta"]["ref_date"],
                         "2026-05-15")

    def test_digest_ref_date_flag_overrides(self):
        code, out = self._digest("--ref-date", "2026-05-20")
        self.assertEqual(code, 0)
        self.assertEqual(out["members"][0]["bundle"]["meta"]["ref_date"],
                         "2026-05-20")

    def test_digest_rejects_bad_ref_date(self):
        self.assertEqual(self._digest("--ref-date", "2026-02-30")[0], 2)


class GatherRefDateFlag(unittest.TestCase):
    def test_gather_validates_ref_date(self):
        base = ["--owner", "o", "--repo", "r", "--from", MAY[0],
                "--to", MAY[1], "--store", "s.db"]
        self.assertEqual(gather.parse_args(base + ["--ref-date", "2026-05-15"])
                         .ref_date, "2026-05-15")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gather.parse_args(base + ["--ref-date", "15/05/2026"])


class ComputeBucketsWindowFallback(unittest.TestCase):
    """compute_buckets and build_forecast share one window rule: `meta.period`,
    else `{from, to}`."""

    def _bundle(self, meta):
        return {"meta": meta, "trains": [], "milestones": [], "issues": [],
                "prs": [{"number": 1, "state": "closed", "merged": True,
                         "merged_at": "2026-04-30T10:00:00Z", "url": "u/1"},
                        {"number": 2, "state": "closed", "merged": True,
                         "merged_at": "2026-05-02T10:00:00Z", "url": "u/2"}]}

    def test_falls_back_to_from_to(self):
        out = link.compute_buckets(self._bundle(
            {"from": MAY[0], "to": MAY[1]}))
        self.assertEqual(_ids(out["shipped"], "pr"), [2])

    def test_period_wins_over_from_to(self):
        out = link.compute_buckets(self._bundle(
            {"from": MAY[0], "to": MAY[1],
             "period": {"from": "2026-04-01", "to": "2026-04-30"}}))
        self.assertEqual(_ids(out["shipped"], "pr"), [1])

    def test_no_window_at_all_stays_permissive(self):
        out = link.compute_buckets(self._bundle({}))
        self.assertEqual(_ids(out["shipped"], "pr"), [1, 2])

    def test_shared_helper(self):
        self.assertEqual(link.window_period({"from": "a", "to": "b"}),
                         {"from": "a", "to": "b"})
        self.assertEqual(link.window_period({"period": {"from": "x", "to": "y"},
                                             "from": "a", "to": "b"}),
                         {"from": "x", "to": "y"})


if __name__ == "__main__":
    unittest.main()
