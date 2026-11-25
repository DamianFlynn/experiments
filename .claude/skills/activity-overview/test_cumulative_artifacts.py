"""Cumulative artifact folding (issue #76).

A store grows window by window. The `code_events` ledger keeps every window's
rows, so each artifact node (`lifecycle`, `status`, `replaced_by`, `ts`) and the
symbol-move edges must be rebuilt from the WHOLE stored ledger, not from the
incoming bundle alone. Then the store equals its re-derivation by construction:
`validate` passes on a multi-window store, and folding windows in any order, or
re-folding one, converges to the same store.

Scenario (from the issue): `fixtures/bundle_p3.json` is May. June is the same
history shifted a month with new commit shas, minus the delete — so June
re-adds `docs/firewall.md` and leaves it live.
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
_RAW = ("meta", "prs", "issues", "commits", "code_events", "milestones", "releases")


def _may():
    with open(os.path.join(FIX, "bundle_p3.json")) as fh:
        base = json.load(fh)
    return {k: copy.deepcopy(base[k]) for k in _RAW if k in base}


def _june():
    """May shifted to June (new shas and PR/issue numbers), minus the delete."""
    text = json.dumps(_may())
    text = text.replace("2026-05-31", "2026-06-30").replace("2026-05-", "2026-06-")
    for ch in "1234":
        text = text.replace(("c" + ch) * 20, ("d" + ch) * 20)
    june = json.loads(text)
    for item in june.get("prs", []) + june.get("issues", []):
        item["number"] += 100
    june["code_events"] = [e for e in june["code_events"] if e["change"] != "delete"]
    return june


def _store(*bundles):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b))
    return conn


def _artifact_nodes(conn, project="o", repo="r"):
    """{local id: (ts, data)} for every artifact node of one repo."""
    out = {}
    for n in graphstore.repo_nodes(conn, project, repo, "code"):
        local = graphstore.local_id(n["id"], project, repo)
        if graphstore.is_artifact_local(local):
            out[local] = (n["ts"], n["data"])
    return out


def _move_edges(conn):
    return sorted(
        (r[0], r[1], r[2], r[3]) for r in conn.execute(
            "SELECT src_id, dst_id, edge_type, data FROM edges "
            "WHERE edge_type IN ('replaced_by', 'identity_from')"))


def _ok(testcase, conn, project="o", repo="r"):
    report = validate.validate(conn, project=project, repo=repo)
    failing = [c for c in report.checks if not c["ok"]]
    testcase.assertEqual(failing, [], json.dumps(failing, indent=1, default=str))


class TestMayJuneStore(unittest.TestCase):

    def test_two_windows_pass_validate(self):
        _ok(self, _store(_may(), _june()))

    def test_firewall_has_three_event_lifecycle_and_is_live(self):
        arts = _artifact_nodes(_store(_may(), _june()))
        ts, fw = arts["art:docs/firewall.md"]
        self.assertEqual([(e["event"], e["date"]) for e in fw["lifecycle"]],
                         [("add", "2026-05-03"), ("remove", "2026-05-25"),
                          ("add", "2026-06-03")])
        self.assertEqual(fw["status"], "live")
        self.assertIsNone(fw["replaced_by"])
        self.assertEqual(ts, "2026-06-03")

    def test_readme_lifecycle_spans_both_windows(self):
        arts = _artifact_nodes(_store(_may(), _june()))
        ts, readme = arts["art:README.md"]
        self.assertEqual([e["commit"][:2] for e in readme["lifecycle"]], ["c2", "d2"])
        self.assertEqual(ts, "2026-06-10")

    def test_renamed_away_twice_stays_replaced(self):
        arts = _artifact_nodes(_store(_may(), _june()))
        _, old = arts["art:examples/basic/main.bicep"]
        self.assertEqual([e["event"] for e in old["lifecycle"]],
                         ["add", "remove", "add", "remove"])
        self.assertEqual(old["status"], "replaced")
        self.assertEqual(old["replaced_by"], "art:examples/advanced/main.bicep")
        _, new = arts["art:examples/advanced/main.bicep"]
        self.assertEqual(len(new["lifecycle"]), 2)
        self.assertEqual(new["status"], "live")

    def test_fold_order_does_not_matter(self):
        self.assertEqual(_artifact_nodes(_store(_june(), _may())),
                         _artifact_nodes(_store(_may(), _june())))
        _ok(self, _store(_june(), _may()))

    def test_refolding_may_changes_nothing(self):
        once = _store(_may(), _june())
        again = _store(_may(), _june(), _may())
        self.assertEqual(_artifact_nodes(again), _artifact_nodes(once))
        _ok(self, again)
        report = validate.validate(again, project="o", repo="r")
        idem = [c for c in report.checks if c["name"] == "idempotency"]
        self.assertEqual(len(idem), 1)
        self.assertTrue(idem[0]["ok"], idem[0]["details"])

    def test_two_windows_equal_one_union_window(self):
        may, june = _may(), _june()
        union = copy.deepcopy(may)
        for key in ("prs", "issues", "commits", "code_events"):
            union[key] = may.get(key, []) + june.get(key, [])
        union["meta"]["to"] = "2026-06-30"
        self.assertEqual(_artifact_nodes(_store(may, june)),
                         _artifact_nodes(_store(union)))

    def test_overlapping_windows_converge(self):
        may, june = _may(), _june()
        # A second window that overlaps May's last fortnight and covers June.
        overlap = copy.deepcopy(june)
        overlap["meta"]["from"] = "2026-05-15"
        late_may = [e for e in may["code_events"] if e["date"] >= "2026-05-15"]
        overlap["code_events"] = late_may + june["code_events"]
        a = _store(may, overlap)
        b = _store(overlap, may)
        self.assertEqual(_artifact_nodes(a), _artifact_nodes(b))
        self.assertEqual(_artifact_nodes(a), _artifact_nodes(_store(may, june)))
        _ok(self, a)
        _ok(self, b)

    def test_extract_artifacts_independent_of_fold_order(self):
        """extract reads the stored nodes back in build_artifacts' insertion order
        (the timeline tie-break depends on it); that order must not depend on
        which window was folded first."""
        def keys(conn):
            return list(extract.extract(conn, "o", "r", "2026-05-01", "2026-06-30",
                                        warn=lambda _m: None)["artifacts"])
        june = _june()
        # June's first event names a file May never touches, so ledger (fold)
        # order and date order disagree on which artifact comes first.
        june["code_events"].insert(0, {
            "commit": "d0" * 20, "author": "Eve", "date": "2026-06-01",
            "change": "add", "path": "docs/june.md"})
        may_first = keys(_store(_may(), june))
        self.assertEqual(may_first[-1], "art:docs/june.md")
        self.assertEqual(keys(_store(june, _may())), may_first)

    def test_external_bundle_cross_check_on_multi_window_store(self):
        """`validate --bundle <one window>` on a store that holds more windows:
        the store's artifacts are the cumulative projection, which the bundle's
        events are already part of — no drift."""
        conn = _store(_may(), _june())
        for b in (_may(), _june()):
            report = validate.validate(conn, project="o", repo="r", bundle=b)
            nd = [c for c in report.checks if c["name"] == "no_drift"][0]
            arts = [d for d in nd["details"] if d.get("kind") == "artifact"]
            self.assertEqual(arts, [])

    def test_external_bundle_not_in_store_is_drift(self):
        """A bundle whose events were never folded still shows as drift."""
        conn = _store(_may())
        report = validate.validate(conn, project="o", repo="r", bundle=_june())
        nd = [c for c in report.checks if c["name"] == "no_drift"][0]
        ids = {d.get("id") for d in nd["details"] if d.get("kind") == "artifact"}
        self.assertIn("art:docs/firewall.md", ids)

    def test_window_only_node_is_caught(self):
        """no_drift keeps its teeth: a node holding only the last window's
        lifecycle (the pre-fix behaviour) is reported."""
        conn = _store(_may(), _june())
        aid = "o/r#art:docs/firewall.md"
        node = graphstore.get_node(conn, aid)
        data = dict(node["data"], lifecycle=node["data"]["lifecycle"][-1:])
        graphstore.upsert_node(conn, aid, "o", "r", "code", node["ts"], data)
        report = validate.validate(conn, project="o", repo="r")
        nd = [c for c in report.checks if c["name"] == "no_drift"][0]
        self.assertFalse(nd["ok"])
        self.assertIn("lifecycle length drift",
                      {d.get("problem") for d in nd["details"]})


class TestSingleWindowUnchanged(unittest.TestCase):

    def test_single_window_nodes_equal_bundle_derivation(self):
        """A one-window store holds exactly what derive builds from that bundle."""
        for name in ("bundle_p3.json", "bundle_p3b.json", "bundle_p3c.json",
                     "bundle_sample.json", "bundle_p2.json"):
            with open(os.path.join(FIX, name)) as fh:
                bundle = json.load(fh)
            want = derive.build_artifacts(bundle)
            derive.link_symbol_identity({**bundle, "artifacts": want})
            got = _artifact_nodes(_store(bundle), bundle["meta"]["owner"],
                                  bundle["meta"]["repo"])
            self.assertEqual({k: v[1] for k, v in got.items()}, want, name)

    def test_single_window_symbol_moves_equal_link_symbol_identity(self):
        """Recorded moves applied to the store's nodes give exactly what
        link_symbol_identity gives on the one bundle (both confidences)."""
        rename = {"commit": "r1" * 20, "author": "a", "date": "2026-05-04",
                  "change": "rename", "old_path": "x/old.bicep",
                  "path": "x/new.bicep"}
        bundle = _sym_bundle("2026-05-01", "2026-05-31", [
            _sym("a.bicep", "change", "fw", "a1" * 20, "2026-05-02"),
            _sym("a.bicep", "drop", "fw", "a2" * 20, "2026-05-03"),
            _sym("b.bicep", "add", "fw", "a2" * 20, "2026-05-03"),
            _sym("x/old.bicep", "drop", "vnet", "r1" * 20, "2026-05-04"),
            _sym("x/new.bicep", "add", "vnet", "r1" * 20, "2026-05-04"),
            _sym("a.bicep", "add", "note", "a1" * 20, "2026-05-02",
                 subkind="todo"),
        ], [rename])
        want = derive.build_artifacts(bundle)
        derive.link_symbol_identity({**bundle, "artifacts": want})
        self.assertEqual(
            {k for k, a in want.items() if a.get("status") == "replaced"},
            {"a.bicep#bicep:resource:fw", "x/old.bicep#bicep:resource:vnet"})
        got = {k: v[1] for k, v in _artifact_nodes(_store(bundle)).items()}
        self.assertEqual(got, want)


def _sym(path, change, name, commit, date, lang="bicep", subkind="resource"):
    return {"path": path, "lang": lang, "subkind": subkind, "name": name,
            "change": change, "commit": commit, "author": "a", "date": date,
            "before": None, "after": None}


def _sym_bundle(frm, to, symbol_events, code_events=()):
    return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
            "commits": [], "code_events": list(code_events),
            "symbol_events": symbol_events}


class TestSymbolMovesAcrossWindows(unittest.TestCase):
    """Symbol moves are matched per fold window (as before #76) and recorded as
    edges; every node applies the recorded moves on top of its cumulative
    lifecycle. Matching over the whole ledger instead would make a symbol's
    creation `add` (one window) and its move `add` (a later one) ambiguous."""

    CREATE = _sym_bundle("2026-05-01", "2026-05-31",
                         [_sym("a.bicep", "add", "fw", "a1" * 20, "2026-05-02")])
    MOVE = _sym_bundle("2026-06-01", "2026-06-30",
                       [_sym("a.bicep", "drop", "fw", "a2" * 20, "2026-06-10"),
                        _sym("b.bicep", "add", "fw", "a2" * 20, "2026-06-10")])
    MOVE_ON = _sym_bundle("2026-07-01", "2026-07-31",
                          [_sym("b.bicep", "drop", "fw", "b2" * 20, "2026-07-10"),
                           _sym("c.bicep", "add", "fw", "b2" * 20, "2026-07-10")])
    REVIVE = _sym_bundle("2026-07-01", "2026-07-31",
                         [_sym("a.bicep", "add", "fw", "a3" * 20, "2026-07-03")])

    A = "a.bicep#bicep:resource:fw"
    B = "b.bicep#bicep:resource:fw"
    C = "c.bicep#bicep:resource:fw"
    # the move edge also records the source's drop commit (`move_commit`)
    EDGE = ('{"move_basis": "unique_name", "move_commit": "%s", '
            '"move_confidence": "medium"}' % ("a2" * 20))

    def _orders(self, *bundles):
        return (bundles, tuple(reversed(bundles)))

    def test_move_in_later_window_links_cumulative_nodes(self):
        stores = [_store(*o) for o in self._orders(self.CREATE, self.MOVE)]
        for conn in stores:
            arts = _artifact_nodes(conn)
            a, b = arts[self.A][1], arts[self.B][1]
            self.assertEqual([e["event"] for e in a["lifecycle"]], ["add", "remove"])
            self.assertEqual((a["status"], a["replaced_by"]), ("replaced", self.B))
            self.assertEqual(b["identity_from"], self.A)
            self.assertEqual(
                _move_edges(conn),
                [("o/r#" + self.A, "o/r#" + self.B, "replaced_by", self.EDGE),
                 ("o/r#" + self.B, "o/r#" + self.A, "identity_from", self.EDGE)])
            _ok(self, conn)
        self.assertEqual(_artifact_nodes(stores[0]), _artifact_nodes(stores[1]))

    def test_readding_an_old_destination_does_not_revive_an_old_move(self):
        # A -> B (June), A revived (July), A -> C (August), B re-added (September):
        # A is replaced by C, the move that IS its final removal, whatever
        # happens to B afterwards.
        august = _sym_bundle("2026-08-01", "2026-08-31", [
            _sym("a.bicep", "drop", "fw", "a4" * 20, "2026-08-10"),
            _sym("c.bicep", "add", "fw", "a4" * 20, "2026-08-10")])
        september = _sym_bundle("2026-09-01", "2026-09-30", [
            _sym("b.bicep", "add", "fw", "b5" * 20, "2026-09-05")])
        for order in ((self.CREATE, self.MOVE, self.REVIVE, august, september),
                      (september, august, self.REVIVE, self.MOVE, self.CREATE)):
            a = _artifact_nodes(_store(*order))[self.A][1]
            self.assertEqual((a["status"], a["replaced_by"]),
                             ("replaced", self.C))

    def test_revived_then_deleted_without_a_move_is_removed(self):
        # A -> B (June), A revived and later deleted with no new move (July):
        # the June move does not explain the July deletion.
        july = _sym_bundle("2026-07-01", "2026-07-31", [
            _sym("a.bicep", "add", "fw", "a3" * 20, "2026-07-03"),
            _sym("a.bicep", "drop", "fw", "a4" * 20, "2026-07-10")])
        for order in ((self.CREATE, self.MOVE, july),
                      (july, self.MOVE, self.CREATE)):
            arts = _artifact_nodes(_store(*order))
            a = arts[self.A][1]
            self.assertEqual((a["status"], a.get("replaced_by")),
                             ("removed", None))
            # B still knows where it came from
            self.assertEqual(arts[self.B][1]["identity_from"], self.A)

    def test_drop_and_add_in_different_windows_is_not_a_move(self):
        may = _sym_bundle("2026-05-01", "2026-05-31",
                          [_sym("a.bicep", "drop", "fw", "a2" * 20, "2026-05-20")])
        june = _sym_bundle("2026-06-01", "2026-06-30",
                           [_sym("b.bicep", "add", "fw", "b1" * 20, "2026-06-05")])
        conn = _store(may, june)
        self.assertEqual(_artifact_nodes(conn)[self.A][1]["status"], "removed")
        self.assertEqual(_move_edges(conn), [])
        # no_drift only: validate's idempotency probe re-folds the WHOLE ledger
        # as one window, which matches this pair as a move and adds two edges —
        # a limitation of the probe on multi-window stores (documented in LLD
        # §14.4), not of the store.
        report = validate.validate(conn, project="o", repo="r")
        nd = [c for c in report.checks if c["name"] == "no_drift"][0]
        self.assertTrue(nd["ok"], nd["details"])

    def test_revived_after_move_is_live_again(self):
        stores = [_store(*o) for o in self._orders(self.CREATE, self.MOVE, self.REVIVE)]
        for conn in stores:
            arts = _artifact_nodes(conn)
            a = arts[self.A][1]
            self.assertEqual([e["event"] for e in a["lifecycle"]],
                             ["add", "remove", "add"])
            self.assertEqual((a["status"], a["replaced_by"]), ("live", None))
            self.assertNotIn("move_confidence", a)
            self.assertEqual(arts[self.B][1]["identity_from"], self.A)
            _ok(self, conn)
        self.assertEqual(_artifact_nodes(stores[0]), _artifact_nodes(stores[1]))

    def test_moved_twice_carries_both_links(self):
        stores = [_store(*o) for o in self._orders(self.CREATE, self.MOVE, self.MOVE_ON)]
        for conn in stores:
            arts = _artifact_nodes(conn)
            b = arts[self.B][1]
            self.assertEqual(b["identity_from"], self.A)
            self.assertEqual((b["status"], b["replaced_by"]), ("replaced", self.C))
            self.assertEqual(arts[self.C][1]["identity_from"], self.B)
            self.assertEqual(arts[self.A][1]["replaced_by"], self.B)
            _ok(self, conn)
        self.assertEqual(_artifact_nodes(stores[0]), _artifact_nodes(stores[1]))

    def test_refold_of_move_window_changes_nothing(self):
        once = _store(self.CREATE, self.MOVE)
        again = _store(self.CREATE, self.MOVE, self.CREATE, self.MOVE)
        self.assertEqual(_artifact_nodes(again), _artifact_nodes(once))
        self.assertEqual(_move_edges(again), _move_edges(once))

    def test_symbol_lifecycle_spans_windows(self):
        june = _sym_bundle("2026-06-01", "2026-06-30",
                           [_sym("a.bicep", "change", "fw", "a3" * 20, "2026-06-03")])
        for order in self._orders(self.CREATE, june):
            conn = _store(*order)
            _, a = _artifact_nodes(conn)[self.A]
            self.assertEqual([e["event"] for e in a["lifecycle"]], ["add", "change"])
            self.assertEqual(a["status"], "live")
            _ok(self, conn)


class TestChronologicalLifecycle(unittest.TestCase):
    """build_artifacts orders each lifecycle by event date (ties keep source
    order), so status comes from the newest event whatever order the events
    arrive in — the property that makes the fold order-independent."""

    def test_out_of_order_events_sorted_by_date(self):
        ev = lambda c, d, ch: {"commit": c, "author": "a", "date": d,  # noqa: E731
                               "change": ch, "path": "docs/x.md"}
        bundle = {"meta": {"owner": "o", "repo": "r"},
                  "code_events": [ev("j1", "2026-06-02", "add"),
                                  ev("m1", "2026-05-02", "add"),
                                  ev("m2", "2026-05-09", "delete")]}
        a = derive.build_artifacts(bundle)["art:docs/x.md"]
        self.assertEqual([e["commit"] for e in a["lifecycle"]], ["m1", "m2", "j1"])
        self.assertEqual(a["status"], "live")

    def test_same_date_keeps_source_order(self):
        ev = lambda c, ch: {"commit": c, "author": "a", "date": "2026-05-02",  # noqa: E731
                            "change": ch, "path": "docs/x.md"}
        bundle = {"meta": {"owner": "o", "repo": "r"},
                  "code_events": [ev("k1", "add"), ev("k2", "delete")]}
        a = derive.build_artifacts(bundle)["art:docs/x.md"]
        self.assertEqual([e["commit"] for e in a["lifecycle"]], ["k1", "k2"])
        self.assertEqual(a["status"], "removed")


if __name__ == "__main__":
    unittest.main()


class TestLifecycleOrderIsCommitTime(unittest.TestCase):
    """`date` is the committer's LOCAL day; lifecycles follow the UTC commit
    time when the commits carry it, so a later commit made in a timezone that is
    still on the previous day is not replayed first."""

    def _bundle(self, with_times):
        add, delete = "a" * 40, "b" * 40
        commits = [
            # 2026-05-02T00:30+02:00 == 2026-05-01T22:30Z (local day: the 2nd)
            {"sha": add, "date": "2026-05-02"},
            # 2026-05-01T23:30Z (local day: the 1st) -- the LATER commit
            {"sha": delete, "date": "2026-05-01"}]
        if with_times:
            commits[0]["committed_at"] = "2026-05-01T22:30:00Z"
            commits[1]["committed_at"] = "2026-05-01T23:30:00Z"
        ev = lambda change, sha, day: {  # noqa: E731
            "path": "docs/a.md", "change": change, "commit": sha,
            "author": "x", "date": day}
        return {"meta": {"owner": "o", "repo": "r"}, "commits": commits,
                "code_events": [ev("add", add, "2026-05-02"),
                                ev("delete", delete, "2026-05-01")]}

    def test_utc_commit_time_orders_the_lifecycle(self):
        art = derive.build_artifacts(self._bundle(True))["art:docs/a.md"]
        self.assertEqual([e["event"] for e in art["lifecycle"]], ["add", "remove"])
        self.assertEqual(art["status"], "removed")

    def test_store_round_trip_keeps_commit_time_order(self):
        bundle = self._bundle(True)
        bundle["meta"].update({"from": "2026-05-01", "to": "2026-05-31"})
        conn = _store(bundle)
        art = _artifact_nodes(conn)["art:docs/a.md"][1]
        self.assertEqual([e["event"] for e in art["lifecycle"]], ["add", "remove"])
        _ok(self, conn)


class TestReviewFollowUps(unittest.TestCase):
    def test_project_with_no_member_nodes_fails_even_if_status_says_ok(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.set_member_status(conn, "p", ["o/r"], "2026-01-01", "2026-01-31",
                                     graphstore.MEMBER_OK)
        report = validate.validate_project(conn, "p", [])
        self.assertFalse(report["ok"])
        names = {c["name"]: c["ok"] for c in report["project_checks"]}
        self.assertIs(names.get("target"), False)

    def test_unreadable_window_ledger_does_not_break_member_discovery(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("p/o/b#area-main.tf", "p", "o/b", "structure", None,
             {"id": "main.tf", "synthesized": graphstore.STANDIN_MARK}, None)])
        graphstore.set_meta(conn, "gathered_windows", "{not json")
        self.assertEqual(graphstore.project_repos(conn, "p"), ["o/b"])

    def test_series_index_with_bad_nested_fields_exits_2(self):
        import io
        import tempfile
        import contextlib
        import link
        for doc in ('[{"shipped": null}]', '[{"forecast": [null]}]',
                    '[{"in_flight": {"a": 1}}]'):
            with tempfile.TemporaryDirectory() as tmp:
                idx = os.path.join(tmp, "series.json")
                with open(idx, "w") as fh:
                    fh.write(doc)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit) as cm:
                    link._load_series_index(idx)
                self.assertEqual(cm.exception.code, 2, doc)
                self.assertIn("not a list of reference objects", err.getvalue())
        # a well-formed index still loads
        with tempfile.TemporaryDirectory() as tmp:
            idx = os.path.join(tmp, "series.json")
            with open(idx, "w") as fh:
                fh.write('[{"shipped": [{"type": "pr", "id": 1}], "forecast": []}]')
            self.assertEqual(len(link._load_series_index(idx)), 1)
