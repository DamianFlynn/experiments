"""Tests for extract.py — the store-backed bundle reader.

The former symmetric enrich-equivalence gate (`enrich(extract) ==
enrich(golden)`) was RETIRED in slice 7b-2: now that `link.enrich` shrank and
fold materializes artifacts/people into the store for extract to READ back, the
two sides of that comparison legitimately differ (a raw golden fed to enrich
leaves artifacts/people empty; extract's reconstruction carries them). The
authoritative end-to-end gate is now test_characterization.py's golden-master
oracle (`fold -> extract -> enrich` must reproduce the committed char_<name>.json
snapshots byte-for-byte). See that module's docstring.

What remains here guards exactly what extract is responsible for: the per-repo
singleton round-trips (`ExtractSingletonFacts`) and the raw bundle shape /
deterministic ordering / code-event reconstruction (`ExtractRawShape`).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_golden(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


# NOTE (slice 7b-2): the former symmetric enrich-equivalence gate
# (`enrich(extract) == enrich(golden)`) is RETIRED. It is no longer valid now
# that `link.enrich` SHRANK: artifacts/people are materialized into the store by
# fold and READ back by extract — they are no longer derived inside enrich. So
# `enrich(golden)` on a RAW fixture leaves artifacts/people empty (enrich's
# defensive setdefaults), while `enrich(extract)` carries the store-materialized
# values; the two sides legitimately differ and a symmetric comparison would
# wrongly fail. The end-to-end correctness gate is now the ASYMMETRIC
# golden-master oracle in test_characterization.py
# (`fold -> extract -> enrich` must reproduce the committed char_<name>.json
# snapshots byte-for-byte for all five goldens), which proves store-derived ==
# link-derived with no drift. The raw-shape + singleton-roundtrip tests below
# still guard exactly what extract is responsible for.


class ExtractSingletonFacts(unittest.TestCase):
    """Guard against a vacuous gate: each newly round-tripped per-repo singleton
    must be present, non-empty, and equal to the golden's value after extract
    (so _normalize is not silently dropping it and hiding a miss)."""

    def _fold_extract(self, golden_name):
        golden = _load_golden(golden_name)
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(golden))
        meta = golden["meta"]
        extracted = extract.extract(
            conn, meta["owner"], meta["repo"], meta["from"], meta["to"])
        return golden, extracted

    def test_workflow_stats_roundtrips_p2(self):
        golden, ex = self._fold_extract("bundle_p2.json")
        self.assertTrue(golden["workflow_stats"], "fixture precondition")
        self.assertIn("workflow_stats", ex)
        self.assertTrue(ex["workflow_stats"])
        self.assertEqual(ex["workflow_stats"], golden["workflow_stats"])

    def test_code_graph_edges_roundtrip_p3c(self):
        golden, ex = self._fold_extract("bundle_p3c.json")
        self.assertIn("code_graph", ex)
        edges = [e for a in ex["code_graph"]["areas"] for e in a.get("edges", [])]
        self.assertTrue(edges, "p3c code_graph must carry non-empty edges")
        self.assertEqual(ex["code_graph"], golden["code_graph"])

    def test_code_graph_owners_taxonomy_roundtrip_p3b(self):
        golden, ex = self._fold_extract("bundle_p3b.json")
        for key in ("code_graph", "code_owners", "label_taxonomy"):
            self.assertIn(key, ex, key)
            self.assertTrue(ex[key], key + " must be non-empty")
            self.assertEqual(ex[key], golden[key], key)

    def test_absent_singletons_not_fabricated(self):
        # bundle_p3 has none of these; extract must not invent empty keys.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(_load_golden("bundle_p3.json")))
        ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        for key in ("workflow_stats", "code_graph", "code_owners",
                    "label_taxonomy"):
            self.assertNotIn(key, ex, key + " must not be fabricated")


class ExtractRawShape(unittest.TestCase):
    """Properties of the raw bundle extract emits, independent of the goldens."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, copy.deepcopy(_load_golden("bundle_p3.json")))

    def test_meta_roundtrips_owner_repo_window(self):
        b = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(b["meta"]["owner"], "o")
        self.assertEqual(b["meta"]["repo"], "r")
        self.assertEqual(b["meta"]["from"], "2026-05-01")
        self.assertEqual(b["meta"]["to"], "2026-05-31")

    def test_emits_raw_plus_store_materialized_projections(self):
        # extract emits the raw substrate PLUS the two projections fold
        # materializes into nodes and extract READS back (artifacts/people).
        # The window-only projections enrich still computes must NOT appear.
        b = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        enrich_only = {"trains", "buckets", "timeline", "feature_deltas",
                       "forecast", "modules", "symbol_moves"}
        self.assertEqual(set(b) & enrich_only, set(),
                         "extract must not emit enrich-only derived keys")
        # artifacts/people are now materialized from the store by extract.
        self.assertIn("artifacts", b)
        self.assertIn("people", b)

    def test_arrays_are_deterministically_ordered(self):
        b1 = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        b2 = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(b1, b2)
        # commits ordered by sha
        self.assertEqual([c["sha"] for c in b1["commits"]],
                         sorted(c["sha"] for c in b1["commits"]))
        # prs/issues ordered by number
        self.assertEqual([p["number"] for p in b1["prs"]],
                         sorted(p["number"] for p in b1["prs"]))

    def test_code_events_preserve_source_order(self):
        # ledger rowid order == original fold order == source order.
        b = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(
            [(e["commit"][:2], e["path"]) for e in b["code_events"]],
            [("c1", "examples/basic/main.bicep"),
             ("c1", "docs/firewall.md"),
             ("c2", "README.md"),
             ("c3", "examples/advanced/main.bicep"),
             ("c4", "docs/firewall.md")],
        )

    def test_rename_old_path_recovered(self):
        b = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        rename = [e for e in b["code_events"] if e["change"] == "rename"]
        self.assertEqual(len(rename), 1)
        self.assertEqual(rename[0]["old_path"], "examples/basic/main.bicep")


if __name__ == "__main__":
    unittest.main()
