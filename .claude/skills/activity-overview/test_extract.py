"""Equivalence-gate tests for extract.py — the store-backed bundle reader.

The proof that `extract` faithfully reconstructs a rev-13 RAW bundle out of the
journey-graph store is the *enrich-equivalence gate*:

    seed store (fold the golden) -> extract(window) -> link.enrich(extracted)
    must equal link.enrich(golden)

Why `enrich(extract) == enrich(golden)` rather than the literal `extract -> enrich
== golden`? The checked-in golden bundles (fixtures/bundle_*.json) were generated
at Phase 3a and are STALE relative to the current link.py (which since then grew
people/modules/forecast/symbol_moves and richer trains/buckets). `fold_bundle`
only stores — and `enrich` only consumes — the RAW keys, so the meaningful,
achievable equivalence is: feeding extract's reconstruction through the *current*
enrich yields the same result as feeding the golden's own raw keys through it.
That isolates exactly what extract is responsible for (the raw substrate) from
churn in the derived layer. See gather.fold_bundle (gather.py) and
link.enrich (link.py).

Two normalizations make the comparison robust (reused by the next agent):
  (a) meta: drop keys not reconstructible from P6-stored facts / volatile —
      `ref_date`, `period`, `generated_at`. enrich falls back to meta["to"] and
      {from,to} for these, so dropping them does not change any other output.
  (b) empty containers: drop keys whose value is [] or {} from BOTH sides, so an
      absent raw array (bundle_sample has no `releases`) and a present-but-empty
      one (bundle_p3 has `releases: []`) compare equal.
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
import link  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# meta keys that P6 does not store (so extract cannot reconstruct them) or that
# are volatile; enrich falls back to deterministic equivalents.
_VOLATILE_META = ("ref_date", "period", "generated_at")


def _load_golden(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def _normalize(bundle):
    """Canonicalize an enriched bundle for equivalence comparison: drop volatile
    meta keys and any key whose value is an empty list/dict (see module docstring
    (a) and (b))."""
    b = copy.deepcopy(bundle)
    meta = b.get("meta")
    if isinstance(meta, dict):
        for k in _VOLATILE_META:
            meta.pop(k, None)
    return {k: v for k, v in b.items() if v not in ([], {})}


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, indent=2)


class ExtractEquivalenceGate(unittest.TestCase):
    """For each target golden: fold it, extract its window, and assert the
    enriched extraction matches the enriched golden (the equivalence gate)."""

    def _gate(self, golden_name):
        golden = _load_golden(golden_name)
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(golden))

        meta = golden["meta"]
        extracted = extract.extract(
            conn, meta["owner"], meta["repo"], meta["from"], meta["to"]
        )

        # extract emits ONLY raw keys; enrich adds the derived ones.
        self.assertNotIn("trains", extracted,
                         "extract must emit raw keys only; enrich adds derived")
        self.assertNotIn("buckets", extracted)

        got = _normalize(link.enrich(copy.deepcopy(extracted)))
        want = _normalize(link.enrich(copy.deepcopy(golden)))
        if got != want:
            # readable diff: report which top-level keys differ.
            diffs = []
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    diffs.append(k)
            self.fail(
                "enrich(extract) != enrich({}) — differing keys: {}\n"
                "--- got[{}] ---\n{}\n--- want[{}] ---\n{}".format(
                    golden_name, diffs,
                    diffs[0] if diffs else "", _canonical(got.get(diffs[0]) if diffs else None),
                    diffs[0] if diffs else "", _canonical(want.get(diffs[0]) if diffs else None),
                )
            )

    def test_bundle_sample_gate(self):
        # commits/issues/prs -> trains/buckets (the first green).
        self._gate("bundle_sample.json")

    def test_bundle_p3_gate(self):
        # adds code_events -> artifacts/timeline/feature_deltas, plus milestones.
        self._gate("bundle_p3.json")


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

    def test_emits_only_raw_keys(self):
        b = extract.extract(self.conn, "o", "r", "2026-05-01", "2026-05-31")
        derived = {"trains", "buckets", "artifacts", "timeline",
                   "feature_deltas", "forecast", "modules", "people",
                   "symbol_moves"}
        self.assertEqual(set(b) & derived, set(),
                         "extract must not emit derived keys")

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
