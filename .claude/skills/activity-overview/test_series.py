import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import series  # noqa: E402


def _bundle(meta=None, shipped=(), in_flight=(), next_candidates=(),
            forecast=(), prs=(), issues=(), rejected=()):
    """A minimal already-enriched bundle: just the keys series.py reads."""
    def _refs(items):
        return [{"type": t, "id": i, "url": f"http://x/{t}/{i}"} for t, i in items]
    return {
        "meta": meta or {"from": "2026-01-01", "to": "2026-01-31",
                         "ref_date": "2026-01-31"},
        "buckets": {
            "shipped": _refs(shipped),
            "rejected": _refs(rejected),
            "next_candidates": _refs(next_candidates),
            "in_flight": _refs(in_flight),
        },
        "forecast": {
            "next_milestone": None,
            "candidates": [
                {"ref": {"type": t, "id": i, "url": f"http://x/{t}/{i}"},
                 "tier": tier, "score": 1.0, "signals": []}
                for t, i, tier in forecast
            ],
        },
        "prs": list(prs),
        "issues": list(issues),
    }


class TestInstallmentSnapshot(unittest.TestCase):
    def test_snapshot_shape_drops_url_keeps_status_and_tier(self):
        b = _bundle(
            shipped=[("pr", 10), ("pr", 9)],
            in_flight=[("issue", 5)],
            forecast=[("issue", 7, "likely")],
            issues=[{"number": 5, "board_status": "In progress"}],
        )
        snap = series.installment_snapshot(b)
        self.assertEqual(snap["from"], "2026-01-01")
        self.assertEqual(snap["to"], "2026-01-31")
        self.assertEqual(snap["ref_date"], "2026-01-31")
        # shipped: refs are {type,id} only, sorted by (type, id).
        self.assertEqual(snap["shipped"],
                         [{"type": "pr", "id": 9}, {"type": "pr", "id": 10}])
        # in_flight carries board_status when the board defines one.
        self.assertEqual(snap["in_flight"],
                         [{"type": "issue", "id": 5, "board_status": "In progress"}])
        # forecast carries the predicted tier, no url.
        self.assertEqual(snap["forecast"],
                         [{"type": "issue", "id": 7, "tier": "likely"}])

    def test_in_flight_without_board_status_omits_key(self):
        b = _bundle(in_flight=[("pr", 3)])
        snap = series.installment_snapshot(b)
        self.assertEqual(snap["in_flight"], [{"type": "pr", "id": 3}])

    def test_ref_date_falls_back_to_to(self):
        b = _bundle(meta={"from": "2026-01-01", "to": "2026-01-31"})
        self.assertEqual(series.installment_snapshot(b)["ref_date"], "2026-01-31")


class TestComputeSeriesFirstInstallment(unittest.TestCase):
    def test_no_prior_is_first_installment(self):
        b = _bundle(shipped=[("pr", 1)], in_flight=[("issue", 2)])
        s = series.compute_series(b, None)
        self.assertTrue(s["first_installment"])
        self.assertEqual(s["new"], [])
        self.assertEqual(s["carried_over"], [])
        self.assertEqual(s["forecast_loop"],
                         {"landed": [], "not_yet": [], "dropped": []})
        self.assertIsNone(s["prior"])


class TestComputeSeriesCarryOver(unittest.TestCase):
    def test_new_carried_and_already_shipped(self):
        prior = series.installment_snapshot(_bundle(
            shipped=[("pr", 100)],
            in_flight=[("issue", 5), ("pr", 6)],
            issues=[{"number": 5, "board_status": "In review"}],
        ))
        # This window: pr 100 was shipped last time (neither new nor carried);
        # issue 5 still in flight (carried, prior_status "In review");
        # pr 6 now shipped (carried — was in prior in_flight); pr 200 is new.
        cur = _bundle(
            shipped=[("pr", 100), ("pr", 6), ("pr", 200)],
            in_flight=[("issue", 5)],
        )
        s = series.compute_series(cur, prior)
        self.assertFalse(s["first_installment"])
        self.assertEqual(s["new"],
                         [{"type": "pr", "id": 200, "url": "http://x/pr/200",
                           "bucket": "shipped"}])
        carried_keys = [(c["type"], c["id"], c["bucket"], c["prior_status"])
                        for c in s["carried_over"]]
        self.assertEqual(carried_keys, [
            ("issue", 5, "in_flight", "In review"),
            ("pr", 6, "shipped", "in_flight"),
        ])

    def test_new_opened_is_the_new_work_created_in_the_window(self):
        # validation finding 18: `new` is new to the series; an issue open for
        # years and first touched this window is in it, but not in new_opened
        prior = series.installment_snapshot(_bundle(shipped=[("pr", 1)]))
        cur = _bundle(
            shipped=[("pr", 300)], in_flight=[("issue", 2308), ("pr", 301)],
            prs=[{"number": 300, "created_at": "2026-01-04T10:00:00Z"},
                 {"number": 301, "created_at": "2025-12-31T23:00:00Z"}],
            issues=[{"number": 2308, "created_at": "2024-02-01T00:00:00Z"}])
        s = series.compute_series(cur, prior)
        self.assertEqual([(e["type"], e["id"]) for e in s["new"]],
                         [("issue", 2308), ("pr", 300), ("pr", 301)])
        self.assertEqual(s["new_opened"],
                         [{"type": "pr", "id": 300, "url": "http://x/pr/300",
                           "bucket": "shipped"}])
        self.assertEqual(series.compute_series(cur, None)["new_opened"], [])

    def test_carried_without_prior_board_status_uses_bucket(self):
        prior = series.installment_snapshot(_bundle(in_flight=[("pr", 9)]))
        cur = _bundle(in_flight=[("pr", 9)])
        s = series.compute_series(cur, prior)
        self.assertEqual(s["carried_over"][0]["prior_status"], "in_flight")


class TestForecastLoop(unittest.TestCase):
    def test_landed_vs_not_yet(self):
        prior = series.installment_snapshot(_bundle(
            forecast=[("issue", 7, "likely"), ("pr", 8, "possible")]))
        # issue 7 shipped this window; pr 8 did not.
        cur = _bundle(shipped=[("issue", 7)])
        s = series.compute_series(cur, prior)
        self.assertEqual(s["forecast_loop"]["landed"],
                         [{"type": "issue", "id": 7, "tier": "likely"}])
        self.assertEqual(s["forecast_loop"]["not_yet"],
                         [{"type": "pr", "id": 8, "tier": "possible"}])

    def test_empty_prior_forecast(self):
        prior = series.installment_snapshot(_bundle(shipped=[("pr", 1)]))
        s = series.compute_series(_bundle(shipped=[("pr", 1)]), prior)
        self.assertEqual(s["forecast_loop"],
                         {"landed": [], "not_yet": [], "dropped": []})


class TestSeriesWiring(unittest.TestCase):
    """link.py --series: round-trips + appends; no `series` key without it."""

    def _write(self, bundle):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as fh:
            json.dump(bundle, fh)
        return path

    def test_no_series_flag_adds_no_series_key(self):
        # A raw fixture-free minimal bundle that enrich tolerates.
        b = _min_enrichable()
        path = self._write(b)
        link.main([path])
        with open(path) as fh:
            out = json.load(fh)
        self.assertNotIn("series", out)
        os.unlink(path)

    def test_series_first_then_appends(self):
        series_fd, series_path = tempfile.mkstemp(suffix=".json")
        os.close(series_fd)
        os.unlink(series_path)  # absent ⇒ first installment

        b1 = _min_enrichable()
        p1 = self._write(b1)
        link.main([p1, "--series", series_path])
        with open(p1) as fh:
            out1 = json.load(fh)
        self.assertTrue(out1["series"]["first_installment"])
        self.assertEqual(len(_installments(series_path)), 1)

        # The NEXT window appends a second installment and is no longer
        # "first". (Before #86 this test re-ran the SAME window and expected a
        # second entry; a same-window re-run now replaces its entry, see
        # TestSeriesIdempotency.)
        b2 = _min_enrichable("2026-02-01", "2026-02-28")
        p2 = self._write(b2)
        link.main([p2, "--series", series_path])
        with open(p2) as fh:
            out2 = json.load(fh)
        self.assertFalse(out2["series"]["first_installment"])
        self.assertEqual(len(_installments(series_path)), 2)

        for p in (p1, p2, series_path):
            os.unlink(p)

    def test_empty_or_whitespace_index_is_first_installment(self):
        # A zero-byte / whitespace-only file (touched-but-unwritten or truncated)
        # must be treated as the first installment, not crash on JSONDecodeError.
        for contents in ("", "   \n"):
            series_fd, series_path = tempfile.mkstemp(suffix=".json")
            os.close(series_fd)
            with open(series_path, "w") as fh:
                fh.write(contents)
            p = self._write(_min_enrichable())
            link.main([p, "--series", series_path])
            with open(p) as fh:
                out = json.load(fh)
            self.assertTrue(out["series"]["first_installment"])
            self.assertEqual(len(_installments(series_path)), 1)
            os.unlink(p)
            os.unlink(series_path)


def _min_enrichable(frm="2026-01-01", to="2026-01-31", owner="o", repo="r"):
    """A minimal raw bundle that link.enrich accepts without KeyErrors."""
    return {
        "meta": {"owner": owner, "repo": repo, "from": frm,
                 "to": to, "ref_date": to,
                 "period": {"from": frm, "to": to}},
        "commits": [],
        "prs": [],
        "issues": [],
        "milestones": [],
    }


def _installments(series_path):
    """The installment list of a written index (version 2 document)."""
    with open(series_path) as fh:
        doc = json.load(fh)
    assert isinstance(doc, dict) and doc.get("version") == 2, doc
    return doc["installments"]


def _refs(*items):
    return [{"type": t, "id": i} for t, i in items]


class TestIssue86Scenario(unittest.TestCase):
    """The confirmed scenario of #86. Prior in_flight: PR#2, issue#3; prior
    forecast: issues #4, #5. Now PR#2 is rejected, #3 is a next candidate, #4
    shipped, #5 rejected (closed not planned)."""

    def setUp(self):
        self.prior = series.installment_snapshot(_bundle(
            in_flight=[("pr", 2), ("issue", 3)],
            next_candidates=[("issue", 4), ("issue", 5)],
            forecast=[("issue", 4, "likely"), ("issue", 5, "possible")],
            prs=[{"number": 2, "board_status": "In review"}]))
        cur = _bundle(meta={"from": "2026-02-01", "to": "2026-02-28"},
                      shipped=[("issue", 4)],
                      rejected=[("pr", 2), ("issue", 5)],
                      next_candidates=[("issue", 3)])
        self.s = series.compute_series(cur, self.prior)

    def test_carried_work_that_left_shipped_or_in_flight_is_reported(self):
        self.assertEqual(
            [(c["type"], c["id"], c["bucket"], c["prior_status"])
             for c in self.s["carried_over"]],
            [("issue", 3, "next_candidates", "in_flight"),
             ("pr", 2, "rejected", "In review")])

    def test_prior_forecast_candidate_is_not_new(self):
        self.assertEqual(self.s["new"], [])

    def test_forecast_loop_landed_and_dropped(self):
        loop = self.s["forecast_loop"]
        self.assertEqual(loop["landed"],
                         [{"type": "issue", "id": 4, "tier": "likely"}])
        self.assertEqual(loop["dropped"],
                         [{"type": "issue", "id": 5, "tier": "possible"}])
        self.assertEqual(loop["not_yet"], [])


class TestNewExcludesEverythingThePriorKnew(unittest.TestCase):
    def test_prior_next_candidate_rejected_and_forecast_are_not_new(self):
        prior = {"from": "2026-01-01", "to": "2026-01-31",
                 "shipped": [], "in_flight": [],
                 "next_candidates": _refs(("issue", 7)),
                 "rejected": _refs(("pr", 8)),
                 "forecast": [{"type": "issue", "id": 9, "tier": "longshot"}]}
        cur = _bundle(meta={"from": "2026-02-01", "to": "2026-02-28"},
                      shipped=[("issue", 7), ("issue", 9)],
                      in_flight=[("pr", 8), ("pr", 10)])
        s = series.compute_series(cur, prior)
        self.assertEqual([(r["type"], r["id"]) for r in s["new"]], [("pr", 10)])

    def test_old_snapshot_without_new_lists_still_compares(self):
        # A pre-#86 snapshot has no next_candidates / rejected / captured.
        prior = {"from": "2026-01-01", "to": "2026-01-31", "ref_date": "2026-01-31",
                 "shipped": [], "in_flight": _refs(("pr", 1)), "forecast": []}
        cur = _bundle(meta={"from": "2026-02-01", "to": "2026-02-28"},
                      rejected=[("pr", 1)], shipped=[("pr", 2)])
        s = series.compute_series(cur, prior)
        self.assertEqual([(c["id"], c["bucket"]) for c in s["carried_over"]],
                         [(1, "rejected")])
        self.assertEqual([r["id"] for r in s["new"]], [2])
        self.assertEqual(s["prior"], {"from": "2026-01-01", "to": "2026-01-31",
                                      "captured": None})


class TestSnapshotV2Fields(unittest.TestCase):
    def test_snapshot_records_rejected_next_candidates_and_capture(self):
        b = _bundle(rejected=[("pr", 4)], next_candidates=[("issue", 2), ("issue", 1)])
        snap = series.installment_snapshot(b, captured="backfill")
        self.assertEqual(snap["rejected"], [{"type": "pr", "id": 4}])
        self.assertEqual(snap["next_candidates"],
                         [{"type": "issue", "id": 1}, {"type": "issue", "id": 2}])
        self.assertEqual(snap["captured"], "backfill")
        self.assertEqual(series.installment_snapshot(b)["captured"], "in_order")

    def test_window_comes_from_meta_period(self):
        b = _bundle(meta={"from": "x", "to": "y", "owner": "o", "repo": "r",
                          "period": {"from": "2026-03-01", "to": "2026-03-31"}})
        snap = series.installment_snapshot(b)
        self.assertEqual((snap["from"], snap["to"]), ("2026-03-01", "2026-03-31"))
        self.assertEqual((snap["owner"], snap["repo"]), ("o", "r"))


class TestPlaceInstallment(unittest.TestCase):
    def _w(self, frm, to, **kw):
        return dict({"from": frm, "to": to, "shipped": []}, **kw)

    def test_same_window_replaces_and_order_is_by_window(self):
        idx = [self._w("2026-03-01", "2026-03-31"), self._w("2026-01-01", "2026-01-31")]
        out = series.place_installment(idx, self._w("2026-02-01", "2026-02-28"))
        self.assertEqual([i["from"] for i in out],
                         ["2026-01-01", "2026-02-01", "2026-03-01"])
        again = series.place_installment(out, self._w("2026-02-01", "2026-02-28", tag=1))
        self.assertEqual([i["from"] for i in again],
                         ["2026-01-01", "2026-02-01", "2026-03-01"])
        self.assertEqual(again[1]["tag"], 1)

    def test_legacy_duplicates_collapse_to_the_last_recorded(self):
        idx = [self._w("2026-01-01", "2026-01-31", tag="a"),
               self._w("2026-01-01", "2026-01-31", tag="b")]
        self.assertEqual(series.normalize_installments(idx),
                         [self._w("2026-01-01", "2026-01-31", tag="b")])

    def test_predecessor_and_later(self):
        idx = [self._w("2026-01-01", "2026-01-31"), self._w("2026-03-01", "2026-03-31")]
        feb = ("2026-02-01", "2026-02-28")
        self.assertEqual(series.predecessor(idx, feb)["from"], "2026-01-01")
        self.assertEqual([i["from"] for i in series.later_installments(idx, feb)],
                         ["2026-03-01"])
        self.assertIsNone(series.predecessor(idx, ("2026-01-01", "2026-01-31")))
        # A windowless (hand-made / legacy) entry sorts before every window.
        idx2 = [{"shipped": [], "tag": "old"}] + idx
        self.assertEqual(series.predecessor(idx2, ("2026-01-01", "2026-01-31"))["tag"],
                         "old")


class _SeriesDisk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.spath = os.path.join(self.dir, "series.json")

    def run_link(self, bundle, name="b.json"):
        """link.py BUNDLE --series; returns (bundle['series'], stderr)."""
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            json.dump(bundle, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            link.main([p, "--series", self.spath])
        with open(p) as fh:
            return json.load(fh)["series"], err.getvalue()

    def sample(self, frm, to):
        with open(os.path.join(os.path.dirname(__file__), "fixtures",
                               "bundle_sample.json")) as fh:
            b = json.load(fh)
        b["meta"].update({"from": frm, "to": to, "ref_date": to,
                          "period": {"from": frm, "to": to}})
        return b


class TestSeriesIdempotency(_SeriesDisk):
    def test_same_window_twice_leaves_one_entry_and_the_same_block(self):
        first, _ = self.run_link(self.sample("2026-05-01", "2026-05-31"))
        with open(self.spath) as fh:
            index_once = fh.read()
        second, _ = self.run_link(self.sample("2026-05-01", "2026-05-31"))
        self.assertEqual(first, second)
        self.assertTrue(second["first_installment"])
        with open(self.spath) as fh:
            self.assertEqual(fh.read(), index_once)
        self.assertEqual(len(_installments(self.spath)), 1)

    def test_same_window_twice_after_a_prior_month(self):
        self.run_link(_min_enrichable("2026-04-01", "2026-04-30"))
        first, _ = self.run_link(self.sample("2026-05-01", "2026-05-31"))
        second, _ = self.run_link(self.sample("2026-05-01", "2026-05-31"))
        self.assertEqual(first, second)
        self.assertEqual(second["prior"]["from"], "2026-04-01")
        self.assertEqual([(r["type"], r["id"]) for r in second["new"]],
                         [("issue", 17), ("pr", 42)])
        self.assertEqual([i["from"] for i in _installments(self.spath)],
                         ["2026-04-01", "2026-05-01"])

    def test_out_of_order_compares_against_the_window_predecessor(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.run_link(_min_enrichable("2026-03-01", "2026-03-31"))
        feb, err = self.run_link(_min_enrichable("2026-02-01", "2026-02-28"))
        self.assertFalse(feb["first_installment"])
        self.assertEqual(feb["prior"], {"from": "2026-01-01", "to": "2026-01-31",
                                        "captured": "in_order"})
        inst = _installments(self.spath)
        self.assertEqual([i["from"] for i in inst],
                         ["2026-01-01", "2026-02-01", "2026-03-01"])
        self.assertEqual([i["captured"] for i in inst],
                         ["in_order", "backfill", "in_order"])
        self.assertIn("2026-03-01", err)      # later installment named

    def test_rerun_of_an_earlier_window_replaces_it(self):
        self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.run_link(_min_enrichable("2026-02-01", "2026-02-28"))
        jan, err = self.run_link(_min_enrichable("2026-01-01", "2026-01-31"))
        self.assertTrue(jan["first_installment"])
        inst = _installments(self.spath)
        self.assertEqual([i["from"] for i in inst], ["2026-01-01", "2026-02-01"])
        self.assertEqual(inst[0]["captured"], "backfill")
        self.assertIn("2026-02-01", err)

    def test_old_list_format_is_read_and_rewritten_as_version_2(self):
        legacy = [{"from": "2026-04-01", "to": "2026-04-30", "ref_date": "2026-04-30",
                   "shipped": [], "in_flight": [{"type": "pr", "id": 42}],
                   "forecast": []},
                  # a pre-#86 double run left the month twice
                  {"from": "2026-04-01", "to": "2026-04-30", "ref_date": "2026-04-30",
                   "shipped": [], "in_flight": [{"type": "pr", "id": 42,
                                                 "board_status": "Review"}],
                   "forecast": []}]
        with open(self.spath, "w") as fh:
            json.dump(legacy, fh)
        s, _ = self.run_link(self.sample("2026-05-01", "2026-05-31"))
        self.assertEqual([(c["type"], c["id"], c["bucket"], c["prior_status"])
                          for c in s["carried_over"]],
                         [("pr", 42, "shipped", "Review")])
        inst = _installments(self.spath)
        self.assertEqual([i["from"] for i in inst], ["2026-04-01", "2026-05-01"])
        self.assertEqual(inst[0], legacy[1])     # kept verbatim

    def test_version_2_document_is_read(self):
        with open(self.spath, "w") as fh:
            json.dump({"version": 2, "installments": [
                {"from": "2026-04-01", "to": "2026-04-30", "shipped": [],
                 "in_flight": [], "forecast": [], "captured": "in_order"}]}, fh)
        s, _ = self.run_link(_min_enrichable("2026-05-01", "2026-05-31"))
        self.assertEqual(s["prior"]["from"], "2026-04-01")


class TestSeriesRefusals(_SeriesDisk):
    def _refused(self, bundle, needle):
        p = os.path.join(self.dir, "b.json")
        with open(p, "w") as fh:
            json.dump(bundle, fh)
        with open(p) as fh:
            before = fh.read()
        index_before = None
        if os.path.exists(self.spath):
            with open(self.spath) as fh:
                index_before = fh.read()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            link.main([p, "--series", self.spath])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(needle, err.getvalue())
        with open(p) as fh:
            self.assertEqual(fh.read(), before)
        if index_before is not None:
            with open(self.spath) as fh:
                self.assertEqual(fh.read(), index_before)

    def test_unknown_index_version_exits_2(self):
        with open(self.spath, "w") as fh:
            json.dump({"version": 3, "installments": []}, fh)
        self._refused(_min_enrichable(), "version")

    def test_bundle_without_a_window_exits_2(self):
        b = _min_enrichable()
        for k in ("from", "to", "period"):
            del b["meta"][k]
        self._refused(b, "window")

    def test_index_of_another_repo_exits_2(self):
        self.run_link(_min_enrichable(owner="o", repo="other"))
        self._refused(_min_enrichable("2026-02-01", "2026-02-28"), "o/other")

    def test_non_string_window_in_index_exits_2(self):
        with open(self.spath, "w") as fh:
            json.dump([{"from": 20260101, "to": "2026-01-31", "shipped": []}], fh)
        self._refused(_min_enrichable(), "from")


if __name__ == "__main__":
    unittest.main()
