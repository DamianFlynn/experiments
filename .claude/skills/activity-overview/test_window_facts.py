"""Issue #94 — per-window CI statistics and structure facts.

A gather's `workflow_stats` (CI runs created in its window), `code_graph`
(the areas of the files it saw, with their dependency edges), `code_owners`
and `label_taxonomy` (read from its checkout) describe THAT window. Before the
fix fold kept one latest-wins node per repo for each, so on a store holding
several months every view showed the last folded month's CI statistics and
structure, and people areas in a past month were attributed against a later
code graph.

The rules (extract.py "Per-window facts", STORE.md, BUNDLE.md `meta.facts_windows`):

- fold records each window's facts (and its clone sha) under
  `facts:{project}/{repo}:{from}:{to}`; each value is stored once per repo
  under its content hash (`fact:{project}/{repo}:{sha256}`), so windows with
  identical facts share it;
- a view of exactly a gathered window uses that window's facts;
- any other view sums the CI statistics of the gathered windows inside it
  (never two that overlap: a roll-up is summed instead of its months),
  takes the structure of the latest gathered window overlapping it (else the
  latest before it, else the earliest), and says which in `meta.facts_windows`
  (`workflow_stats_complete` is true only when the summed windows cover every
  day of the view); a window that only partly overlaps the view is not summed
  (its runs cannot be split by day) and is listed under
  `workflow_stats_excluded`; with no gathered window inside the view
  `workflow_stats` is omitted;
- a store folded before #94 (no per-window facts) reads the latest-wins nodes
  as before.

Offline: stores are built in memory via gather.fold_bundle.
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
import test_window_scoped_views as tw  # noqa: E402

MAY, JUNE, SPAN = tw.MAY, tw.JUNE, tw.SPAN
JULY = ("2026-07-01", "2026-07-31")
QUARTER = ("2026-04-01", "2026-06-30")
SHA_MAY, SHA_JUNE, SHA_JULY = "5" * 40, "6" * 40, "7" * 40


def _stats(**per_name):
    return {name: {"total": t, "success": s, "failure": f, "cancelled": c,
                   "other": t - s - f - c}
            for name, (t, s, f, c) in per_name.items()}


MAY_STATS = _stats(CI=(10, 8, 2, 0))
JUNE_STATS = _stats(CI=(4, 1, 3, 0), Release=(2, 2, 0, 0))
JULY_STATS = _stats(CI=(1, 1, 0, 0))

MAY_GRAPH = {"provider": "directory", "areas": [
    {"id": "core", "label": "core", "paths": ["README.md", "src/a.py"],
     "edges": []}]}
JUNE_GRAPH = {"provider": "directory", "areas": [
    {"id": "lib", "label": "lib", "paths": ["lib/x.py"], "edges": []},
    {"id": "next", "label": "next", "paths": ["README.md", "src/b.py"],
     "edges": [{"to": "lib", "kind": "module", "ref": "./lib",
                "version": None, "transitive": False,
                "provider": "terraform", "resolved": True}]}]}
MAY_OWNERS = [{"pattern": "*", "owners": ["alice"]}]
JUNE_OWNERS = [{"pattern": "*", "owners": ["erin"]}]
MAY_TAXONOMY = {"facets": {"kind": ["bug"]}}
JUNE_TAXONOMY = {"facets": {"kind": ["bug", "feature"]}}


def _with_facts(bundle, stats, graph, owners, taxonomy, sha):
    bundle["workflow_stats"] = copy.deepcopy(stats)
    bundle["code_graph"] = copy.deepcopy(graph)
    bundle["code_owners"] = copy.deepcopy(owners)
    bundle["label_taxonomy"] = copy.deepcopy(taxonomy)
    bundle["meta"]["clone_sha"] = sha
    for c in bundle["commits"]:   # people areas come from commit file lists
        c["files"] = sorted(e["path"] for e in bundle["code_events"]
                            if e["commit"] == c["sha"])
    return bundle


def _may():
    return _with_facts(tw._may(), MAY_STATS, MAY_GRAPH, MAY_OWNERS,
                       MAY_TAXONOMY, SHA_MAY)


def _june():
    return _with_facts(tw._june(), JUNE_STATS, JUNE_GRAPH, JUNE_OWNERS,
                       JUNE_TAXONOMY, SHA_JUNE)


def _july():
    return {"meta": {"owner": "o", "repo": "r", "from": JULY[0],
                     "to": JULY[1], "base_branch": "main",
                     "branches": ["main"], "clone_sha": SHA_JULY},
            "prs": [], "issues": [], "commits": [], "code_events": [],
            "symbol_events": [], "milestones": [], "releases": [],
            "workflow_stats": copy.deepcopy(JULY_STATS),
            "code_graph": copy.deepcopy(MAY_GRAPH)}


_store, _raw, _ok = tw._store, tw._raw, tw._ok


def _facts(b):
    return {k: b.get(k) for k in ("workflow_stats", "code_graph",
                                  "code_owners", "label_taxonomy")}


def _w(window):
    return {"from": window[0], "to": window[1]}


class EachMonthItsOwnFacts(unittest.TestCase):
    """Two months with different CI stats / code graph / owners / taxonomy."""

    def setUp(self):
        self.stores = [_store(_may(), _june()), _store(_june(), _may())]

    def tearDown(self):
        for c in self.stores:
            c.close()

    def test_may_view_shows_may_facts(self):
        for conn in self.stores:
            b = _raw(conn, MAY)
            self.assertEqual(b["workflow_stats"], MAY_STATS)
            self.assertEqual(b["code_graph"], MAY_GRAPH)
            self.assertEqual(b["code_owners"], MAY_OWNERS)
            self.assertEqual(b["label_taxonomy"], MAY_TAXONOMY)
            self.assertEqual(b["meta"]["clone_sha"], SHA_MAY)
            self.assertNotIn("facts_windows", b["meta"])

    def test_june_view_shows_june_facts(self):
        for conn in self.stores:
            b = _raw(conn, JUNE)
            self.assertEqual(b["workflow_stats"], JUNE_STATS)
            self.assertEqual(b["code_graph"], JUNE_GRAPH)
            self.assertEqual(b["code_owners"], JUNE_OWNERS)
            self.assertEqual(b["label_taxonomy"], JUNE_TAXONOMY)
            self.assertEqual(b["meta"]["clone_sha"], SHA_JUNE)

    def test_each_month_equals_a_store_holding_only_that_month(self):
        # The whole enriched view, facts included (state_as_of aside).
        for window, alone in ((MAY, _may()), (JUNE, _june())):
            single = _store(alone)
            want = tw._without(link.enrich(_raw(single, window)))
            for conn in self.stores:
                got = tw._without(link.enrich(_raw(conn, window)))
                self.assertEqual(_facts(got), _facts(want), window)
                self.assertEqual(got, want, window)
            single.close()

    def test_people_areas_use_the_months_code_graph(self):
        # bob edits README.md in both months: area `core` in May's graph,
        # `next` in June's.
        for conn in self.stores:
            self.assertEqual(_raw(conn, MAY)["people"]["bob"]["areas"], ["core"])
            self.assertEqual(_raw(conn, JUNE)["people"]["bob"]["areas"], ["next"])

    def test_code_area_attribution_uses_the_months_code_graph(self):
        for conn in self.stores:
            may = link.enrich(_raw(conn, MAY))
            self.assertEqual(may["artifacts"]["art:README.md"].get("code_area"),
                             "core")

    def test_two_month_view_sums_ci_stats(self):
        for conn in self.stores:
            b = _raw(conn, SPAN)
            self.assertEqual(b["workflow_stats"], {
                "CI": {"total": 14, "success": 9, "failure": 5,
                       "cancelled": 0, "other": 0},
                "Release": {"total": 2, "success": 2, "failure": 0,
                            "cancelled": 0, "other": 0}})
            # structure: the latest gathered window in the view
            self.assertEqual(b["code_graph"], JUNE_GRAPH)
            self.assertEqual(b["code_owners"], JUNE_OWNERS)
            self.assertEqual(b["meta"]["clone_sha"], SHA_JUNE)
            self.assertEqual(b["meta"]["facts_windows"], {
                "structure": _w(JUNE),
                "workflow_stats": [_w(MAY), _w(JUNE)],
                "workflow_stats_complete": True})

    def test_quarter_view_is_a_marked_partial_sum(self):
        # April was never gathered: the sum covers May and June only.
        for conn in self.stores:
            b = _raw(conn, QUARTER)
            self.assertEqual(b["workflow_stats"]["CI"]["total"], 14)
            self.assertEqual(b["meta"]["facts_windows"], {
                "structure": _w(JUNE),
                "workflow_stats": [_w(MAY), _w(JUNE)],
                "workflow_stats_complete": False})

    def test_sub_window_view_omits_ci_stats_it_cannot_split(self):
        for conn in self.stores:
            b = _raw(conn, ("2026-05-10", "2026-05-20"))
            self.assertNotIn("workflow_stats", b)
            self.assertEqual(b["code_graph"], MAY_GRAPH)
            self.assertEqual(b["meta"]["clone_sha"], SHA_MAY)
            self.assertEqual(b["meta"]["facts_windows"], {
                "structure": _w(MAY), "workflow_stats": [],
                "workflow_stats_complete": False,
                "workflow_stats_excluded": [_w(MAY)]})

    def test_view_straddling_two_months_sums_neither(self):
        for conn in self.stores:
            b = _raw(conn, ("2026-05-15", "2026-06-15"))
            self.assertNotIn("workflow_stats", b)
            self.assertEqual(b["code_graph"], JUNE_GRAPH)
            self.assertEqual(b["meta"]["facts_windows"]["workflow_stats_excluded"],
                             [_w(MAY), _w(JUNE)])

    def test_validate_green_both_fold_orders(self):
        may, june = _may(), _june()
        may["symbol_events"], june["symbol_events"] = [], []
        for conn in (_store(may, june), _store(june, may)):
            _ok(self, conn)
            conn.close()
        for alone in (_may(), _june()):
            single = _store(alone)
            _ok(self, single)
            single.close()


class GapsBetweenWindows(unittest.TestCase):

    def setUp(self):
        self.stores = [_store(_may(), _july()), _store(_july(), _may())]

    def tearDown(self):
        for c in self.stores:
            c.close()

    def test_view_between_gathered_windows(self):
        # June was never gathered: no CI stats; the structure as last
        # gathered before it (May).
        for conn in self.stores:
            b = _raw(conn, JUNE)
            self.assertNotIn("workflow_stats", b)
            self.assertEqual(b["code_owners"], MAY_OWNERS)
            self.assertEqual(b["meta"]["clone_sha"], SHA_MAY)
            self.assertEqual(b["meta"]["facts_windows"], {
                "structure": _w(MAY), "workflow_stats": [],
                "workflow_stats_complete": False})

    def test_view_before_every_gathered_window(self):
        for conn in self.stores:
            b = _raw(conn, ("2026-03-01", "2026-03-31"))
            self.assertNotIn("workflow_stats", b)
            self.assertEqual(b["meta"]["facts_windows"]["structure"], _w(MAY))

    def test_window_without_a_fact_has_none(self):
        # July stored no owners / taxonomy: its view has none, not May's.
        for conn in self.stores:
            b = _raw(conn, JULY)
            self.assertEqual(b["workflow_stats"], JULY_STATS)
            self.assertNotIn("code_owners", b)
            self.assertNotIn("label_taxonomy", b)
            self.assertEqual(b["meta"]["clone_sha"], SHA_JULY)

    def test_three_month_view_sums_the_two_gathered(self):
        for conn in self.stores:
            b = _raw(conn, ("2026-05-01", "2026-07-31"))
            self.assertEqual(b["workflow_stats"]["CI"]["total"], 11)
            self.assertFalse(b["meta"]["facts_windows"]["workflow_stats_complete"])
            self.assertEqual(b["meta"]["facts_windows"]["structure"], _w(JULY))


class OverlappingGatheredWindows(unittest.TestCase):
    """A store can hold a month and a roll-up window over it: no run may be
    counted twice in a wider view."""

    def _rollup(self):
        b = _june()
        b["meta"]["from"], b["meta"]["to"] = SPAN
        b["workflow_stats"] = _stats(CI=(20, 20, 0, 0))
        return b

    def test_rollup_is_summed_once(self):
        for order in ((_may(), _june(), self._rollup()),
                      (self._rollup(), _june(), _may())):
            conn = _store(*order)
            b = _raw(conn, QUARTER)
            self.assertEqual(b["workflow_stats"], _stats(CI=(20, 20, 0, 0)))
            fw = b["meta"]["facts_windows"]
            self.assertEqual(fw["workflow_stats"], [_w(SPAN)])
            self.assertEqual(fw["workflow_stats_excluded"], [_w(MAY), _w(JUNE)])
            self.assertFalse(fw["workflow_stats_complete"])   # April missing
            # the roll-up window itself is an exact view
            self.assertNotIn("facts_windows", _raw(conn, SPAN)["meta"])
            # each month still shows its own
            self.assertEqual(_raw(conn, MAY)["workflow_stats"], MAY_STATS)
            conn.close()

    def test_partly_overlapping_windows_sum_the_first(self):
        early = _may()
        early["meta"]["from"], early["meta"]["to"] = "2026-05-01", "2026-05-20"
        late = _june()
        late["meta"]["from"], late["meta"]["to"] = "2026-05-10", "2026-05-31"
        conn = _store(early, late)
        b = _raw(conn, MAY)
        self.assertEqual(b["workflow_stats"], MAY_STATS)
        self.assertFalse(b["meta"]["facts_windows"]["workflow_stats_complete"])
        self.assertEqual(b["meta"]["facts_windows"]["structure"],
                         {"from": "2026-05-10", "to": "2026-05-31"})


class SingleWindowStore(unittest.TestCase):

    def test_exact_view_unchanged(self):
        conn = _store(_may())
        b = _raw(conn, MAY)
        self.assertEqual(_facts(b), {"workflow_stats": MAY_STATS,
                                     "code_graph": MAY_GRAPH,
                                     "code_owners": MAY_OWNERS,
                                     "label_taxonomy": MAY_TAXONOMY})
        self.assertNotIn("facts_windows", b["meta"])
        # the latest-wins nodes are still written, with the same values
        for key, local in extract._SINGLETON_FACTS:
            self.assertEqual(extract._materialize_singleton(
                conn, "o", "r", local), b[key])

    def test_other_windows_say_where_their_facts_come_from(self):
        # Deliberate change on a one-window store: a view that is not the
        # gathered window gains meta.facts_windows, and a sub-window view no
        # longer presents the whole month's CI runs as its own.
        conn = _store(_may())
        sub = _raw(conn, ("2026-05-01", "2026-05-15"))
        self.assertNotIn("workflow_stats", sub)
        self.assertEqual(sub["code_graph"], MAY_GRAPH)
        self.assertFalse(sub["meta"]["facts_windows"]["workflow_stats_complete"])
        wide = _raw(conn, ("2026-01-01", "2026-05-31"))
        self.assertEqual(wide["workflow_stats"], MAY_STATS)
        self.assertEqual(wide["meta"]["facts_windows"]["workflow_stats"],
                         [_w(MAY)])

    def test_store_folded_before_the_fix_reads_the_latest_nodes(self):
        conn = _store(_may(), _june())
        conn.execute("DELETE FROM meta WHERE key LIKE 'facts:%' "
                     "OR key LIKE 'fact:%'")
        conn.commit()
        b = _raw(conn, MAY)
        # legacy: the last folded month's facts, no facts_windows
        self.assertEqual(b["workflow_stats"], JUNE_STATS)
        self.assertEqual(b["code_graph"], JUNE_GRAPH)
        self.assertNotIn("facts_windows", b["meta"])


class StoredOncePerContent(unittest.TestCase):

    def _blobs(self, conn):
        return sorted(r[0] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'fact:%'"))

    def test_identical_facts_are_stored_once(self):
        july = _july()
        july["code_graph"] = copy.deepcopy(MAY_GRAPH)
        conn = _store(_may(), july)
        # May: 4 values; July: stats (new) + the same graph as May.
        self.assertEqual(len(self._blobs(conn)), 5)

    def test_refold_replaces_the_window_and_prunes(self):
        conn = _store(_may())
        before = self._blobs(conn)
        again = _may()
        again["workflow_stats"] = _stats(CI=(3, 3, 0, 0))
        gather.fold_bundle(conn, again)
        self.assertEqual(_raw(conn, MAY)["workflow_stats"],
                         _stats(CI=(3, 3, 0, 0)))
        after = self._blobs(conn)
        self.assertEqual(len(after), len(before))
        self.assertNotEqual(after, before)

    def test_fold_is_idempotent_on_the_facts(self):
        conn = _store(_may(), _june())
        meta = sorted(map(tuple, conn.execute("SELECT key, value FROM meta")))
        gather.fold_bundle(conn, _june())
        self.assertEqual(sorted(map(tuple, conn.execute(
            "SELECT key, value FROM meta"))), meta)

    def test_getter(self):
        conn = _store(_may())
        got = graphstore.get_window_facts(conn, "o", "r", *MAY)
        self.assertEqual(got["clone_sha"], SHA_MAY)
        self.assertEqual(got["facts"]["code_owners"], MAY_OWNERS)
        self.assertEqual(got["missing"], [])
        self.assertIsNone(graphstore.get_window_facts(conn, "o", "r", *JUNE))


class SumWorkflowStats(unittest.TestCase):

    def test_truncation_markers_merge(self):
        key = gather.WORKFLOW_STATS_TRUNCATED_KEY
        self.assertEqual(derive.WORKFLOW_STATS_TRUNCATED_KEY, key)
        a = dict(_stats(CI=(1000, 900, 100, 0)), **{key: {
            "truncated": True, "cap": 1000, "fetched": 1000,
            "total_count": 1200, "ranges": ["2026-05-03..2026-05-03"]}})
        b = _stats(CI=(5, 5, 0, 0))
        c = dict(_stats(CI=(1000, 1000, 0, 0)), **{key: {
            "truncated": True, "cap": 1000, "fetched": 1000,
            "total_count": None, "ranges": ["2026-06-09..2026-06-09"]}})
        got = derive.sum_workflow_stats([a, b, c])
        self.assertEqual(got["CI"]["total"], 2005)
        self.assertEqual(got[key], {
            "truncated": True, "cap": 1000, "fetched": 2000,
            "total_count": None,
            "ranges": ["2026-05-03..2026-05-03", "2026-06-09..2026-06-09"]})
        self.assertEqual(list(got), sorted(got))

    def test_empty(self):
        self.assertEqual(derive.sum_workflow_stats([]), {})


class TrustCheckSeesTheFactsIndex(unittest.TestCase):

    def test_dangling_fact_reference_is_an_error(self):
        conn = _store(_may())
        conn.execute("DELETE FROM meta WHERE key LIKE 'fact:%'")
        conn.commit()   # (an open write transaction stalls validate's backup)
        report = validate.validate(conn, project="o", repo="r")
        failing = [c["name"] for c in report.checks if not c["ok"]]
        self.assertIn("referential_integrity", failing)
        details = [d for c in report.checks
                   if c["name"] == "referential_integrity"
                   for d in c["details"] if d.get("severity") == "ERROR"]
        self.assertTrue(any("facts" in json.dumps(d) for d in details), details)


if __name__ == "__main__":
    unittest.main()
