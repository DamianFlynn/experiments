import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
import series  # noqa: E402


def _bundle(meta=None, shipped=(), in_flight=(), next_candidates=(),
            forecast=(), prs=(), issues=()):
    """A minimal already-enriched bundle: just the keys series.py reads."""
    def _refs(items):
        return [{"type": t, "id": i, "url": f"http://x/{t}/{i}"} for t, i in items]
    return {
        "meta": meta or {"from": "2026-01-01", "to": "2026-01-31",
                         "ref_date": "2026-01-31"},
        "buckets": {
            "shipped": _refs(shipped),
            "rejected": [],
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
        self.assertEqual(s["forecast_loop"], {"landed": [], "not_yet": []})


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
        self.assertEqual(s["forecast_loop"], {"landed": [], "not_yet": []})


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
        with open(series_path) as fh:
            idx = json.load(fh)
        self.assertEqual(len(idx), 1)

        # Second run appends a second installment and is no longer "first".
        b2 = _min_enrichable()
        p2 = self._write(b2)
        link.main([p2, "--series", series_path])
        with open(p2) as fh:
            out2 = json.load(fh)
        self.assertFalse(out2["series"]["first_installment"])
        with open(series_path) as fh:
            idx2 = json.load(fh)
        self.assertEqual(len(idx2), 2)

        for p in (p1, p2, series_path):
            os.unlink(p)


def _min_enrichable():
    """A minimal raw bundle that link.enrich accepts without KeyErrors."""
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-01-01",
                 "to": "2026-01-31", "ref_date": "2026-01-31",
                 "period": {"from": "2026-01-01", "to": "2026-01-31"}},
        "commits": [],
        "prs": [],
        "issues": [],
        "milestones": [],
    }


if __name__ == "__main__":
    unittest.main()
