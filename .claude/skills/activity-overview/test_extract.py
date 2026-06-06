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
import link  # noqa: E402
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


def _file_diff_bundle():
    """A small bundle whose FILE-level code_events carry a bounded `hunk` (the
    Phase 10 slice-diff). No golden carries a patch, so this fixture is crafted
    inline to exercise the file-diff ledger round-trip."""
    c1 = "c" * 40
    hunk = "@@ +1 @@\n-old line\n+new line\n context"
    return {
        "meta": {"owner": "o", "repo": "r",
                 "from": "2026-05-01", "to": "2026-05-31"},
        "commits": [
            {"sha": c1, "message": "Edit guide", "pr": None,
             "author": "Alice", "date": "2026-05-03"},
        ],
        "code_events": [
            # a doc file artifact carrying the bounded diff
            {"commit": c1, "author": "Alice", "date": "2026-05-03",
             "change": "modify", "path": "docs/guide.md", "hunk": hunk},
        ],
        "symbol_events": [],
        "prs": [], "issues": [], "milestones": [], "releases": [],
        "_expected_hunk": hunk,
    }


class ExtractFileDiffRoundTrip(unittest.TestCase):
    """The bounded file `hunk` rides code_events -> file artifact lifecycle ->
    ledger -> extract -> re-derived artifact. RED before the fix (hunk dropped on
    fold/extract), GREEN after. Mirrors ExtractSymbolEventsRoundTrip."""

    def setUp(self):
        self.bundle = _file_diff_bundle()
        self.hunk = self.bundle.pop("_expected_hunk")
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, copy.deepcopy(self.bundle))
        m = self.bundle["meta"]
        self.extracted = extract.extract(
            self.conn, m["owner"], m["repo"], m["from"], m["to"],
            warn=lambda _m: None)

    def test_ledger_persists_hunk_on_file_event(self):
        rows = graphstore.repo_code_events(self.conn, "o", "r")
        # file-level rows are `<project>/<repo>#<path>` (one `#`); symbol rows have
        # a second `#`. Restrict to file rows via extract's local-id stripper.
        file_rows = [r for r in rows
                     if "#" not in extract._full_local(r["artifact_id"], "o", "r")]
        self.assertTrue(file_rows)
        self.assertEqual(file_rows[0]["hunk"], self.hunk)

    def test_extract_carries_hunk_back_onto_code_events(self):
        ce = [e for e in self.extracted["code_events"]
              if e["path"] == "docs/guide.md"]
        self.assertEqual(len(ce), 1)
        self.assertEqual(ce[0]["hunk"], self.hunk)

    def test_build_artifacts_reproduces_file_lifecycle_hunk(self):
        want = derive.build_artifacts(self.bundle)
        got = derive.build_artifacts(self.extracted)
        aid = next(a for a in want if want[a]["path"] == "docs/guide.md")
        self.assertEqual(want[aid]["lifecycle"][0].get("hunk"), self.hunk)
        self.assertEqual(got[aid]["lifecycle"][0].get("hunk"), self.hunk)

    def test_feature_deltas_surface_diff(self):
        enriched = link.enrich(copy.deepcopy(self.extracted))
        fd = [d for d in enriched["feature_deltas"]
              if d.get("name") == "guide.md"]
        self.assertTrue(fd)
        self.assertEqual(fd[0].get("diff"), self.hunk)

    def test_self_sourced_no_drift_passes(self):
        report = validate.validate(self.conn)
        nd = [c for c in report.checks if c["name"] == "no_drift"][0]
        self.assertTrue(nd["ok"], nd["details"])


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

    def test_rename_artifact_order_matches_build_artifacts(self):
        # Regression (Copilot re-review on #13): for a rename, build_artifacts
        # ensures the NEW-path artifact before the replaced OLD-path one, so
        # insertion order is [new, old]. _order_artifacts gave both the same
        # event-index rank then tie-broke by id, flipping renames whose old id
        # sorts before the new (e.g. a->z) to [old, new] — which can change
        # build_timeline's same-(ts,url) tie-break. Order must follow insertion.
        c1 = "d" * 40
        bundle = {
            "meta": {"owner": "o", "repo": "r",
                     "from": "2026-05-01", "to": "2026-05-31"},
            "commits": [{"sha": c1, "message": "Rename readme", "pr": None,
                         "author": "Alice", "date": "2026-05-03"}],
            "code_events": [
                {"commit": c1, "author": "Alice", "date": "2026-05-03",
                 "change": "rename", "path": "z/README.md",
                 "old_path": "a/README.md"},
            ],
            "prs": [], "issues": [], "milestones": [], "releases": [],
        }
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(bundle))
        m = bundle["meta"]
        ex = extract.extract(conn, m["owner"], m["repo"], m["from"], m["to"],
                             warn=lambda _m: None)
        # New (z) before replaced old (a) — matching build_artifacts, NOT lexical.
        self.assertEqual(list(ex["artifacts"]),
                         list(derive.build_artifacts(bundle)))
        self.assertEqual(list(ex["artifacts"]),
                         ["art:z/README.md", "art:a/README.md"],
                         "rename must keep [new, old] order, not lexical [old, new]")


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


class ExtractReviewsAndLifecycle(unittest.TestCase):
    """Phase 10 slice 1: extract surfaces the review/lifecycle social nodes back
    onto their parent pr/issue records, and re-attaches the derived counts, so
    the read side (and a train slice) sees the rounds/lifecycle texture."""

    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                     "to": "2026-05-31"},
            "prs": [{
                "number": 7, "url": "https://gh/o/r/pull/7", "state": "closed",
                "merged": True, "merged_at": "2026-05-10T00:00:00Z",
                "created_at": "2026-05-02T00:00:00Z",
                "closed_at": "2026-05-10T00:00:00Z", "closes": [], "crossref_issues": [],
                "reviews": [
                    {"id": 100, "author": "carol", "state": "changes_requested",
                     "submitted_at": "2026-05-03T00:00:00Z", "body": "x",
                     "url": "https://gh/o/r/pull/7#r100"},
                    {"id": 101, "author": "carol", "state": "approved",
                     "submitted_at": "2026-05-04T00:00:00Z", "body": None,
                     "url": "https://gh/o/r/pull/7#r101"},
                ],
                "lifecycle": [
                    {"id": 200, "actor": "dan", "event": "ready_for_review",
                     "created_at": "2026-05-03T06:00:00Z", "label": None,
                     "url": "u200"},
                ],
            }],
            "issues": [{
                "number": 3, "url": "https://gh/o/r/issues/3", "state": "open",
                "updated_at": "2026-05-09T00:00:00Z", "closed_at": None,
                "lifecycle": [
                    {"id": 300, "actor": "alice", "event": "reopened",
                     "created_at": "2026-05-08T00:00:00Z", "label": None,
                     "url": None},
                ],
            }],
            "commits": [], "milestones": [], "releases": [],
        }

    def _extract(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(self._bundle()))
        return extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                               warn=lambda _m: None)

    def test_reviews_resurface_on_pr_in_order(self):
        ex = self._extract()
        pr = ex["prs"][0]
        self.assertEqual([r["id"] for r in pr["reviews"]], [100, 101])
        self.assertEqual([r["state"] for r in pr["reviews"]],
                         ["changes_requested", "approved"])

    def test_lifecycle_resurfaces_on_pr_and_issue(self):
        ex = self._extract()
        self.assertEqual([e["event"] for e in ex["prs"][0]["lifecycle"]],
                         ["ready_for_review"])
        self.assertEqual([e["event"] for e in ex["issues"][0]["lifecycle"]],
                         ["reopened"])
        # synthesized provenance for the url-less issue event round-trips.
        self.assertEqual(ex["issues"][0]["lifecycle"][0]["url"],
                         "https://gh/o/r/issues/3#event-300")

    def test_derived_counts_are_enrich_only(self):
        # review_rounds/reopen_count are enrich-derived (with forecast/modules),
        # so extract emits only the RAW reviews/lifecycle, not the counts.
        ex = self._extract()
        self.assertNotIn("review_rounds", ex["prs"][0])
        self.assertNotIn("reopen_count", ex["issues"][0])
        # enrich (the read-side derive) turns the resurfaced raw data into counts.
        link.enrich(ex)
        self.assertEqual(ex["prs"][0]["review_rounds"],
                         {"count": 2, "states": ["changes_requested", "approved"]})
        self.assertEqual(ex["issues"][0]["reopen_count"], 1)

    def test_keys_absent_when_no_review_or_lifecycle_data(self):
        # a golden with no reviews/events keeps the pr/issue records clean.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(_load_golden("bundle_p3b.json")))
        ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                             warn=lambda _m: None)
        for pr in ex["prs"]:
            self.assertNotIn("reviews", pr)
            self.assertNotIn("lifecycle", pr)
            self.assertNotIn("review_rounds", pr)
        for iss in ex["issues"]:
            self.assertNotIn("lifecycle", iss)
            self.assertNotIn("reopen_count", iss)

    def test_clean_store_validates_green(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(self._bundle()))
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])


class ExtractBlocks(unittest.TestCase):
    """Phase 11 slice 1: extract surfaces the store `blocks` issue->issue edges
    (normalized blocker->blocked by fold) onto the issue records as RESOLVED numbers
    `issue["blocking"]` (numbers this issue blocks, outbound) and
    `issue["blocked_by"]` (numbers blocking this one, inbound). The raw `blocks`
    ref-list is left UNTOUCHED (fold re-reads it). Omit-when-empty for the resolved
    keys so issues with no dependency edges stay byte-identical."""

    def _bundle(self):
        # `blocks` is the parsed directed-ref list normalize_issue produces.
        # #3 blocks #4 (out: 3->4); #5 is blocked by #4 (in: 4->5, so #4 blocks
        # #5). #4 is therefore blocked by #3 and blocks #5. #6 is unrelated.
        def iss(num, blocks):
            return {
                "number": num, "url": "https://gh/o/r/issues/{}".format(num),
                "state": "open", "updated_at": "2026-05-09T00:00:00Z",
                "closed_at": None, "title": "i{}".format(num), "body": "",
                "blocks": blocks,
            }
        return {
            "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                     "to": "2026-05-31"},
            "prs": [],
            "issues": [
                iss(3, [{"number": 4, "direction": "out"}]),
                iss(4, []),
                iss(5, [{"number": 4, "direction": "in"}]),
                iss(6, []),
            ],
            "commits": [], "milestones": [], "releases": [],
        }

    def _extract(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(self._bundle()))
        return extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                               warn=lambda _m: None)

    def _by_num(self, ex):
        return {i["number"]: i for i in ex["issues"]}

    def test_outbound_blocks_surface_as_blocking_numbers(self):
        issues = self._by_num(self._extract())
        self.assertEqual(issues[3]["blocking"], [4])
        self.assertEqual(issues[4]["blocking"], [5])

    def test_inbound_surfaces_as_blocked_by(self):
        issues = self._by_num(self._extract())
        self.assertEqual(issues[4]["blocked_by"], [3])
        self.assertEqual(issues[5]["blocked_by"], [4])

    def test_blocking_numbers_are_sorted_lists(self):
        # craft an issue that blocks several others, out of numeric order.
        bundle = self._bundle()
        bundle["issues"].append({
            "number": 9, "url": "https://gh/o/r/issues/9", "state": "open",
            "updated_at": "2026-05-09T00:00:00Z", "closed_at": None,
            "title": "i9", "body": "",
            "blocks": [{"number": 6, "direction": "out"},
                       {"number": 4, "direction": "out"},
                       {"number": 5, "direction": "out"}],
        })
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(bundle))
        ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                             warn=lambda _m: None)
        nine = next(i for i in ex["issues"] if i["number"] == 9)
        self.assertEqual(nine["blocking"], [4, 5, 6])

    def test_omit_resolved_keys_when_empty(self):
        # the RESOLVED keys are omit-when-empty (the raw `blocks` ref-list stays).
        issues = self._by_num(self._extract())
        self.assertNotIn("blocking", issues[6])     # #6 in no blocks edge
        self.assertNotIn("blocked_by", issues[6])
        self.assertNotIn("blocked_by", issues[3])   # #3 only blocks
        self.assertNotIn("blocking", issues[5])      # #5 only blocked

    def test_raw_blocks_left_untouched_for_refold(self):
        # extract must NOT overwrite the raw `blocks` ref-list — fold re-reads it.
        issues = self._by_num(self._extract())
        self.assertEqual(issues[3]["blocks"], [{"number": 4, "direction": "out"}])

    def test_validate_idempotent_with_block_edge(self):
        # Regression: validate's idempotency gate re-folds the extracted bundle, and
        # gather.fold reads `blocks` as [{number, direction}]. Surfacing resolved ints
        # under the SAME key crashed it ('int' object is not subscriptable); the
        # resolved view lives under `blocking`/`blocked_by` so re-fold stays clean.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(self._bundle()))
        report = validate.validate(conn)
        idem = [c for c in report.checks if c["name"] == "idempotency"][0]
        self.assertTrue(idem["ok"], idem.get("details"))
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])

    def test_goldens_carry_no_blocking(self):
        # the committed goldens have no blocks edges; omit-when-empty must keep
        # every issue free of the resolved keys (the byte-stability contract).
        for name in ("bundle_p3.json", "bundle_p3b.json", "bundle_p3c.json"):
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, copy.deepcopy(_load_golden(name)))
            ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                                 warn=lambda _m: None)
            for iss in ex["issues"]:
                self.assertNotIn("blocking", iss, name)
                self.assertNotIn("blocked_by", iss, name)


class ExtractProjectBoardRoundTrip(unittest.TestCase):
    """Phase 12 slice 1: sprints materialize from sprint-<id> structure nodes;
    each pr/issue surfaces its board_status + iteration. Omit-when-empty."""

    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31"},
            "prs": [{
                "number": 10, "url": "https://gh/o/r/pull/10", "state": "closed",
                "merged": True, "merged_at": "2026-01-10T00:00:00Z",
                "created_at": "2026-01-05T00:00:00Z",
                "closed_at": "2026-01-10T00:00:00Z", "closes": [],
                "crossref_issues": [],
                "board_status": "In Progress", "iteration": "IT_current",
            }],
            "issues": [{
                "number": 3, "url": "https://gh/o/r/issues/3", "state": "open",
                "updated_at": "2026-01-09T00:00:00Z", "closed_at": None,
                "board_status": "Todo",
            }],
            "commits": [], "milestones": [], "releases": [],
            "sprints": {
                "IT_current": {"title": "Sprint 5", "start": "2026-01-12",
                               "end": "2026-01-26"},
            },
        }

    def _extract(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(self._bundle()))
        return extract.extract(conn, "o", "r", "2026-01-01", "2026-01-31",
                               warn=lambda _m: None)

    def test_sprints_materialize(self):
        ex = self._extract()
        self.assertEqual(ex["sprints"], {
            "IT_current": {"title": "Sprint 5", "start": "2026-01-12",
                           "end": "2026-01-26"}})

    def test_board_status_and_iteration_surface(self):
        ex = self._extract()
        pr = ex["prs"][0]
        self.assertEqual(pr.get("board_status"), "In Progress")
        self.assertEqual(pr.get("iteration"), "IT_current")
        self.assertEqual(ex["issues"][0].get("board_status"), "Todo")
        self.assertNotIn("iteration", ex["issues"][0])  # none -> omitted

    def test_omit_when_empty(self):
        # a golden with no project board has no sprints key and clean records.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(_load_golden("bundle_p3b.json")))
        ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                             warn=lambda _m: None)
        self.assertNotIn("sprints", ex)
        for pr in ex["prs"]:
            self.assertNotIn("board_status", pr)
            self.assertNotIn("iteration", pr)
        for iss in ex["issues"]:
            self.assertNotIn("board_status", iss)


if __name__ == "__main__":
    unittest.main()
