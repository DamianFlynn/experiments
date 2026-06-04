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
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

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


def _symbol_bundle():
    """A small bundle WITH symbol_events: a couple of symbols + a comment, with
    before/after, across 2 commits. No golden carries symbol_events, so this
    fixture is crafted inline to exercise the ledger round-trip."""
    c1 = "a" * 40
    c2 = "b" * 40
    return {
        "meta": {"owner": "o", "repo": "r",
                 "from": "2026-05-01", "to": "2026-05-31"},
        "commits": [
            {"sha": c1, "message": "Add module", "pr": null_safe(),
             "author": "Alice", "date": "2026-05-03"},
            {"sha": c2, "message": "Refine module", "pr": null_safe(),
             "author": "Bob", "date": "2026-05-10"},
        ],
        "code_events": [
            {"commit": c1, "author": "Alice", "date": "2026-05-03",
             "change": "add", "path": "avm/res/x/main.bicep"},
            {"commit": c2, "author": "Bob", "date": "2026-05-10",
             "change": "modify", "path": "avm/res/x/main.bicep"},
        ],
        "symbol_events": [
            # an added param symbol
            {"path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "param", "name": "location", "change": "add",
             "commit": c1, "author": "Alice", "date": "2026-05-03",
             "before": None, "after": "param location string"},
            # the same param changed in c2 (before/after carried)
            {"path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "param", "name": "location", "change": "change",
             "commit": c2, "author": "Bob", "date": "2026-05-10",
             "before": "param location string",
             "after": "param location string = resourceGroup().location"},
            # a resource symbol whose name carries colons/parens
            {"path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "resource", "name": "rg (Microsoft.Resources/rg:2024)",
             "change": "add", "commit": c1, "author": "Alice",
             "date": "2026-05-03", "before": None, "after": "resource rg ..."},
            # a comment artifact (subkind comment -> kind comment)
            {"path": "avm/res/x/main.bicep", "lang": "bicep",
             "subkind": "comment", "name": "// Parameters //",
             "change": "add", "commit": c1, "author": "Alice",
             "date": "2026-05-03", "before": None, "after": "// Parameters //"},
        ],
        "prs": [], "issues": [], "milestones": [], "releases": [],
    }


def null_safe():
    return None


def _symbol_arts(artifacts):
    """Restrict a build_artifacts result to its symbol/comment artifacts
    (those whose id carries a `#`, i.e. NOT the `art:<path>` file artifacts)."""
    return {aid: a for aid, a in artifacts.items() if "#" in aid}


class ExtractSymbolEventsRoundTrip(unittest.TestCase):
    """extract reconstructs symbol_events from the ledger so the self-sourced raw
    bundle is COMPLETE and build_artifacts re-derives the symbol/comment artifacts
    exactly. RED before the fix (extract drops symbol_events -> build_artifacts
    can't re-derive them), GREEN after."""

    def setUp(self):
        self.bundle = _symbol_bundle()
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, copy.deepcopy(self.bundle))
        m = self.bundle["meta"]
        self.extracted = extract.extract(
            self.conn, m["owner"], m["repo"], m["from"], m["to"],
            warn=lambda _m: None)

    def test_extract_emits_symbol_events(self):
        self.assertIn("symbol_events", self.extracted)
        self.assertEqual(len(self.extracted["symbol_events"]),
                         len(self.bundle["symbol_events"]),
                         "extract must round-trip every symbol_event row")

    def test_build_artifacts_reproduces_symbol_artifacts(self):
        # The acceptance criterion: build_artifacts on the reconstructed bundle
        # reproduces the STORED symbol artifacts exactly (same ids + lifecycle).
        want = _symbol_arts(derive.build_artifacts(self.bundle))
        got = _symbol_arts(derive.build_artifacts(self.extracted))
        self.assertTrue(want, "fixture precondition: symbol artifacts exist")
        self.assertEqual(set(got), set(want), "same symbol artifact ids")
        for aid in want:
            self.assertEqual(got[aid]["kind"], want[aid]["kind"], aid)
            self.assertEqual(got[aid]["status"], want[aid]["status"], aid)
            # lifecycle: same events in the same order (the lifecycle the live
            # audit compares against the stored projection).
            self.assertEqual(
                [e["event"] for e in got[aid]["lifecycle"]],
                [e["event"] for e in want[aid]["lifecycle"]], aid)
            self.assertEqual(
                [(e["commit"], e["before"], e["after"])
                 for e in got[aid]["lifecycle"]],
                [(e["commit"], e["before"], e["after"])
                 for e in want[aid]["lifecycle"]], aid)

    def test_symbol_rows_not_double_counted_into_code_events(self):
        # file-level code_events array stays file-only (no `#` paths).
        for ev in self.extracted["code_events"]:
            self.assertNotIn("#", ev["path"])
        self.assertEqual(len(self.extracted["code_events"]),
                         len(self.bundle["code_events"]))

    def test_self_sourced_validate_no_drift_passes(self):
        report = validate.validate(self.conn)
        nd = [c for c in report.checks if c["name"] == "no_drift"][0]
        self.assertTrue(
            nd["ok"],
            "self-sourced no_drift must pass; details: {}".format(nd["details"]))


class ExtractArtifactsKeyNoCollision(unittest.TestCase):
    """Regression (Copilot review on #13): extract keyed bundle["artifacts"] via
    `_local` (parse_id rpartitions the LAST `#`), truncating a symbol artifact's
    `<path>#<lang>:<subkind>:<name>` id down to `<lang>:<subkind>:<name>`. Two
    files sharing a symbol of the same lang:subkind:name then collided to one key,
    silently dropping artifacts from the materialized view. No golden carries
    symbol artifacts, so no gate caught it — on the real AVM store 27 of 94
    artifacts were lost. extract must key by the FULL local id."""

    def test_same_symbol_in_two_files_does_not_collide(self):
        c1 = "c" * 40
        bundle = {
            "meta": {"owner": "o", "repo": "r",
                     "from": "2026-05-01", "to": "2026-05-31"},
            "commits": [{"sha": c1, "message": "Add", "pr": None,
                         "author": "Alice", "date": "2026-05-03"}],
            "code_events": [
                {"commit": c1, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "avm/res/a/main.bicep"},
                {"commit": c1, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "avm/res/b/main.bicep"},
            ],
            "symbol_events": [
                {"path": "avm/res/a/main.bicep", "lang": "bicep",
                 "subkind": "param", "name": "location", "change": "add",
                 "commit": c1, "author": "Alice", "date": "2026-05-03",
                 "before": None, "after": "param location string"},
                {"path": "avm/res/b/main.bicep", "lang": "bicep",
                 "subkind": "param", "name": "location", "change": "add",
                 "commit": c1, "author": "Alice", "date": "2026-05-03",
                 "before": None, "after": "param location string"},
            ],
            "prs": [], "issues": [], "milestones": [], "releases": [],
        }
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(bundle))
        m = bundle["meta"]
        ex = extract.extract(conn, m["owner"], m["repo"], m["from"], m["to"],
                             warn=lambda _m: None)
        arts = ex["artifacts"]
        # Both same-named symbols survive under DISTINCT, full-local keys.
        self.assertIn("avm/res/a/main.bicep#bicep:param:location", arts)
        self.assertIn("avm/res/b/main.bicep#bicep:param:location", arts)
        # And extract's keying matches build_artifacts (the source of truth).
        self.assertEqual(set(arts), set(derive.build_artifacts(bundle)),
                         "extract artifacts map must key exactly as build_artifacts")


class ExtractSymbolMovesRoundTrip(unittest.TestCase):
    """Bonus: with symbol_events round-tripped, symbol_moves is no longer FORCED
    empty by missing symbol_events. A `drop` in one file + `add` in another (the
    move shape) survives extract — exercising the `remove`->`drop` reverse map —
    and link_symbol_identity links it on the self-sourced bundle."""

    def _move_bundle(self):
        c1, c2 = "c" * 40, "d" * 40
        return {
            "meta": {"owner": "o", "repo": "r",
                     "from": "2026-05-01", "to": "2026-05-31"},
            "commits": [
                {"sha": c1, "message": "m1", "pr": None,
                 "author": "Alice", "date": "2026-05-03"},
                {"sha": c2, "message": "m2", "pr": None,
                 "author": "Bob", "date": "2026-05-10"},
            ],
            "code_events": [
                {"commit": c1, "author": "Alice", "date": "2026-05-03",
                 "change": "modify", "path": "avm/res/a/main.bicep"},
                {"commit": c2, "author": "Bob", "date": "2026-05-10",
                 "change": "modify", "path": "avm/res/b/main.bicep"},
            ],
            "symbol_events": [
                {"path": "avm/res/a/main.bicep", "lang": "bicep",
                 "subkind": "resource", "name": "uniqueThing", "change": "drop",
                 "commit": c1, "author": "Alice", "date": "2026-05-03",
                 "before": "resource uniqueThing ...", "after": None},
                {"path": "avm/res/b/main.bicep", "lang": "bicep",
                 "subkind": "resource", "name": "uniqueThing", "change": "add",
                 "commit": c2, "author": "Bob", "date": "2026-05-10",
                 "before": None, "after": "resource uniqueThing ..."},
            ],
            "prs": [], "issues": [], "milestones": [], "releases": [],
        }

    def test_drop_roundtrips_and_move_links(self):
        bundle = self._move_bundle()
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(bundle))
        m = bundle["meta"]
        ex = extract.extract(conn, m["owner"], m["repo"], m["from"], m["to"],
                             warn=lambda _m: None)
        # the `drop` change reverse-maps faithfully through the ledger
        changes = sorted(e["change"] for e in ex["symbol_events"])
        self.assertEqual(changes, ["add", "drop"])
        # link_symbol_identity now finds a confident move (no longer forced empty)
        view = {**ex, "artifacts": derive.build_artifacts(ex)}
        derive.link_symbol_identity(view)
        self.assertEqual(len(view["symbol_moves"]["links"]), 1,
                         "a unique cross-file drop+add must link as a move")


if __name__ == "__main__":
    unittest.main()
