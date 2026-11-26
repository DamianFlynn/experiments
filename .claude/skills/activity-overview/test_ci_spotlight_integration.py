"""Offline tests for ci_spotlight_integration.py (issue #90).

The live `spotlight` CI job gathers a real AVM week and runs the gate against
it; it cannot run on a pull request. These tests build small stores in a temp
directory from fixture bundles and run the gate's `main` in-process, so a
regression in the gate (a false failure on a correct store, a check that reads
another project's ids, a check that no longer bites) is caught by the unit
suite. No network.
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

sys.path.insert(0, os.path.dirname(__file__))
import ci_spotlight_integration as gate  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
BUNDLES = sorted(n for n in os.listdir(FIX)
                 if n.startswith("bundle_") and n.endswith(".json"))


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _sym(path, change, sha, author, date, before, after):
    return {"path": path, "lang": "bicep", "subkind": "resource", "name": "fw",
            "change": change, "commit": sha, "author": author, "date": date,
            "before": before, "after": after}


def _commit(sha, date, msg, author="alice"):
    return {"sha": sha, "message": msg, "author": author, "date": date,
            "url": "https://github.com/acme/r1/commit/{}".format(sha)}


def _move_windows():
    """Three windows of one repo: `fw` is created in old.bicep (add, change),
    moved to new.bicep (drop + add, linked as a move), then changed three times
    in new.bicep. The destination of the move has the most ledger events (4
    against the source's 3), so it is the artifact the gate picks for `symbol`
    — and its identity chain starts at the SOURCE (old.bicep), not at the
    artifact itself."""
    def window(frm, to, commits, events):
        return {"meta": {"owner": "acme", "repo": "r1", "from": frm, "to": to},
                "prs": [], "issues": [], "commits": commits,
                "symbol_events": events}
    old, new = "old.bicep", "new.bicep"
    return [
        window("2026-01-01", "2026-01-15",
               [_commit("aaa1111", "2026-01-05T00:00:00Z", "add fw"),
                _commit("bbb2222", "2026-01-10T00:00:00Z", "tweak fw")],
               [_sym(old, "add", "aaa1111", "alice", "2026-01-05T00:00:00Z",
                     None, "v1"),
                _sym(old, "change", "bbb2222", "alice", "2026-01-10T00:00:00Z",
                     "v1", "v2")]),
        window("2026-01-16", "2026-01-31",
               [_commit("ccc3333", "2026-01-20T00:00:00Z", "move fw", "bob")],
               [_sym(old, "drop", "ccc3333", "bob", "2026-01-20T00:00:00Z",
                     "v2", None),
                _sym(new, "add", "ccc3333", "bob", "2026-01-20T00:00:00Z",
                     None, "v2")]),
        window("2026-02-01", "2026-02-28",
               [_commit("ddd4444", "2026-02-05T00:00:00Z", "fw v3"),
                _commit("eee5555", "2026-02-10T00:00:00Z", "fw v4"),
                _commit("fff6666", "2026-02-15T00:00:00Z", "fw v5")],
               [_sym(new, "change", "ddd4444", "alice", "2026-02-05T00:00:00Z",
                     "v2", "v3"),
                _sym(new, "change", "eee5555", "alice", "2026-02-10T00:00:00Z",
                     "v3", "v4"),
                _sym(new, "change", "fff6666", "alice", "2026-02-15T00:00:00Z",
                     "v4", "v5")]),
    ]


def _decoy(bundle, login="zed"):
    """A copy of `bundle` that is RICHER than the original on every discovery
    axis, with every login renamed to `login`: three extra commits by `login`
    touching the firewall-policy area, each with a ledger event on the same
    file. Folded under another project it out-ranks the real project's person,
    area and artifact, so an unscoped discovery query picks the decoy's ids."""
    b = json.loads(json.dumps(bundle).replace('"alice"', json.dumps(login))
                   .replace('"bob"', json.dumps(login))
                   .replace('"carol"', json.dumps(login))
                   .replace('"dave"', json.dumps(login)))
    path = "avm/res/network/firewall-policy/main.bicep"
    for i in range(3):
        sha = "{}".format(i + 5) * 40
        b["commits"].append({"sha": sha, "message": "tune policy module",
                             "pr": None, "author": login,
                             "files": [path]})
        b["code_events"].append({"commit": sha, "author": login,
                                 "date": "2026-05-1{}".format(i + 1),
                                 "change": "modify", "path": path})
    return b


class _GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "store.db")

    def build(self, folds):
        """folds: [(bundle, project, repo)]; project/repo None -> meta."""
        conn = graphstore.open_store(self.db)
        graphstore.init_schema(conn)
        for bundle, project, repo in folds:
            gather.fold_bundle(conn, copy.deepcopy(bundle),
                               project=project, repo=repo)
        conn.commit()
        conn.close()

    def run_gate(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = gate.main(["--store", self.db] + list(extra))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def line(self, out, name):
        """The gate's result line for check `name` ('' when absent)."""
        for ln in out.splitlines():
            if ln.strip()[2:].startswith(name):
                return ln.strip()
        return ""


class TestMoveDestination(_GateCase):
    """Problem 1: the richest artifact is the destination of a move."""

    def setUp(self):
        super().setUp()
        self.build([(b, None, None) for b in _move_windows()])
        self.dst = graphstore.qualify_id("acme", "r1",
                                         "new.bicep#bicep:resource:fw")
        self.src = graphstore.qualify_id("acme", "r1",
                                         "old.bicep#bicep:resource:fw")

    def test_store_shape_is_the_issue_case(self):
        conn = graphstore.open_store(self.db)
        try:
            self.assertEqual(gate._richest_artifact(conn, "acme"), self.dst)
            res = spotlight.pattern_evolution(conn, "acme", self.dst)
        finally:
            conn.close()
        ids = [c["id"] for c in res["identity_chain"]]
        self.assertEqual(ids, [self.src, self.dst])  # head is NOT the artifact

    def test_symbol_check_passes(self):
        code, out, _ = self.run_gate()
        sym = self.line(out, "symbol")
        self.assertTrue(sym.startswith("✓"), out)
        self.assertIn("new.bicep#bicep:resource:fw", sym)
        self.assertIn("4 lifecycle events", sym)
        self.assertNotIn("identity_chain", out)
        self.assertEqual(code, 0, out)


class TestProjectScoping(_GateCase):
    """Problem 2: on a multi-project store, --project inspects only that
    project. The decoys out-rank the target on every discovery axis and their
    names collide with the target under LIKE (`_` wildcard, case-insensitive)."""

    TARGET = "a_c"
    DECOYS = ("abc", "A_C")

    def setUp(self):
        super().setUp()
        base = _load("bundle_p3b.json")
        folds = [(base, self.TARGET, "o/r")]
        folds += [(_decoy(base), p, "o/r") for p in self.DECOYS]
        self.build(folds)

    def test_discovery_reads_only_the_project(self):
        conn = graphstore.open_store(self.db)
        try:
            self.assertEqual(gate._most_active_person(conn, "abc"), "zed")
            login = gate._most_active_person(conn, self.TARGET)
            self.assertNotEqual(login, "zed")
            self.assertIsNotNone(graphstore.get_node(
                conn, graphstore.qualify_person(self.TARGET, login)))
            repo, area, n = gate._most_touched_area(conn, self.TARGET)
            self.assertEqual(repo, "o/r")
            self.assertEqual(area, "avm/res/network/firewall-policy")
            self.assertEqual(n, 1)  # the decoy's area has 4 touches
            aid = gate._richest_artifact(conn, self.TARGET)
            self.assertTrue(aid.startswith(self.TARGET + "/o/r#"), aid)
            self.assertEqual(aid, "a_c/o/r#docs/firewall.md")
        finally:
            conn.close()

    def test_gate_passes_on_the_target_only(self):
        code, out, _ = self.run_gate("--project", self.TARGET, "--require-all")
        self.assertEqual(code, 0, out)
        self.assertNotIn("zed", out)
        self.assertIn("a_c/o/r#docs/firewall.md", self.line(out, "symbol"))

    def test_each_decoy_also_passes_on_its_own(self):
        for p in self.DECOYS:
            code, out, _ = self.run_gate("--project", p, "--require-all")
            self.assertEqual(code, 0, out)
            self.assertIn("person `zed`", out)

    def test_grep_term_is_found_in_the_project(self):
        # A term indexed only in another project must not be chosen.
        conn = graphstore.open_store(self.db)
        try:
            if not graphstore.fts5_available(conn):
                self.skipTest("FTS5 unavailable")
            graphstore.index_text(conn, "abc/o/r#pr-42", "module avm bicep")
            for nid in [r[0] for r in conn.execute(
                    "SELECT node_id FROM fts_map WHERE "
                    "(node_id >= 'a_c/' AND node_id < 'a_c0') OR "
                    "(node_id >= 'a_c#' AND node_id < 'a_c$')")]:
                graphstore.index_text(conn, nid, "nothing generic here")
            term = gate._grep_term(conn, self.TARGET)
            self.assertNotIn(term, gate._GREP_CANDIDATES)
            hits = gate._project_hits(conn, self.TARGET, term)
            self.assertTrue(hits)
        finally:
            conn.close()

    def test_ambiguous_store_without_project_is_usage_error(self):
        code, _, err = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("multiple projects", err)


class TestFixtureStores(_GateCase):
    """Acceptance: the gate exits 0 on a store folded from each fixture
    bundle. A check whose input the store does not hold is reported as skipped
    (named, with the reason); --require-all turns any skip into a failure, which
    is how the live job runs."""

    def test_fixture_bundles_present(self):
        self.assertGreaterEqual(len(BUNDLES), 5, BUNDLES)

    def test_each_fixture_store_exits_zero(self):
        for name in BUNDLES:
            with self.subTest(bundle=name):
                if os.path.exists(self.db):
                    os.remove(self.db)
                self.build([(_load(name), None, None)])
                code, out, _ = self.run_gate()
                self.assertEqual(code, 0, out)
                self.assertNotIn("✗", out)
                for check in ("person", "subsystem", "symbol", "grep"):
                    ln = self.line(out, check)
                    self.assertTrue(ln.startswith(("✓", "-")), (check, out))

    def test_full_fixture_verifies_all_four(self):
        self.build([(_load("bundle_p3b.json"), None, None)])
        code, out, _ = self.run_gate("--require-all")
        self.assertEqual(code, 0, out)
        for check in ("person", "subsystem", "symbol", "grep"):
            self.assertTrue(self.line(out, check).startswith("✓"), out)

    def test_require_all_fails_on_skips(self):
        self.build([(_load("bundle_p3c.json"), None, None)])
        code, out, _ = self.run_gate()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)
        code, out, _ = self.run_gate("--require-all")
        self.assertEqual(code, 1, out)

    def test_deleted_file_reads_removed(self):
        # p3b's docs/firewall.md is added then deleted: the gate checks the
        # outcome against the last ledger event.
        self.build([(_load("bundle_p3b.json"), None, None)])
        _, out, _ = self.run_gate()
        self.assertIn("removed", self.line(out, "symbol"))


class TestChecksBite(_GateCase):
    """The checks fail when spotlight's answer is wrong — not only when the
    store is empty. Each case corrupts one spotlight result."""

    def setUp(self):
        super().setUp()
        self.build([(_load("bundle_p3b.json"), None, None)])

    def _with(self, name, fn):
        real = getattr(spotlight, name)

        def wrapped(*a, **k):
            return fn(real(*a, **k))
        with mock.patch.object(spotlight, name, wrapped):
            return self.run_gate()

    def test_dropped_lifecycle_event_fails(self):
        def drop(res):
            res["delivered"][0]["timeline"].pop()
            return res
        code, out, _ = self._with("pattern_evolution", drop)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "symbol").startswith("✗"), out)

    def test_wrong_symbol_outcome_fails(self):
        def alive(res):
            res["delivered"][0]["outcome"] = "alive"
            return res
        code, out, _ = self._with("pattern_evolution", alive)
        self.assertEqual(code, 1)
        self.assertIn("outcome", self.line(out, "symbol"))

    def test_unknown_train_outcome_fails(self):
        def merged(res):
            for t in res["delivered"]:
                t["outcome"] = "merged"
            return res
        code, out, _ = self._with("person_impact", merged)
        self.assertEqual(code, 1)
        self.assertIn("outcome", self.line(out, "person"))

    def test_foreign_repo_on_train_fails(self):
        def foreign(res):
            res["delivered"][0]["repos"] = ["other/repo"]
            return res
        code, out, _ = self._with("subsystem_split", foreign)
        self.assertEqual(code, 1)
        self.assertIn("repo", self.line(out, "subsystem"))

    def test_grep_leaking_another_project_fails(self):
        def leak(res):
            for t in res["delivered"][:1]:
                t["touchpoints"].append(
                    {"id": "zz/o/r#pr-1", "role": "mention", "number": 1})
            return res
        code, out, _ = self._with("text_mining", leak)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "grep").startswith("✗"), out)

    def test_grep_missing_a_match_fails(self):
        def lose(res):
            for t in res["delivered"]:
                t["touchpoints"] = []
            return res
        code, out, _ = self._with("text_mining", lose)
        self.assertEqual(code, 1)
        self.assertTrue(self.line(out, "grep").startswith("✗"), out)


def _rich_bundle():
    """One window where the most-active person (alice) touches trains of
    several outcomes in several roles, an area is touched, a file has a
    lifecycle ending in a delete, and the text is searchable."""
    url = "https://github.com/acme/r1/{}/{}"
    path = "avm/res/net/fw/main.bicep"
    doc = "avm/res/net/fw/README.md"
    commits = [
        {"sha": "a" * 40, "author": "alice", "date": "2026-03-05",
         "message": "Add the firewall module (#10)", "files": [path, doc]},
        {"sha": "b" * 40, "author": "alice", "date": "2026-03-06",
         "message": "Tune the firewall module directly", "files": [path, doc]},
        {"sha": "c" * 40, "author": "bob", "date": "2026-03-07",
         "message": "Drop the firewall readme (#11)", "files": [doc]},
    ]
    return {
        "meta": {"owner": "acme", "repo": "r1", "from": "2026-03-01",
                 "to": "2026-03-31"},
        "prs": [
            {"number": 10, "title": "Firewall module", "author": "alice",
             "merged": True, "state": "closed", "merged_by": "bob",
             "merged_at": "2026-03-05T00:00:00Z", "closes": [7],
             "reviewers": ["carol"], "url": url.format("pull", 10),
             "body": "Adds the firewall module.",
             "comments_list": [{"author": "carol", "body": "firewall looks fine"}]},
            {"number": 11, "title": "Readme cleanup", "author": "bob",
             "state": "open", "created_at": "2026-03-07T00:00:00Z",
             "url": url.format("pull", 11), "body": "firewall readme",
             "comments_list": [{"author": "alice",
                                "body": "please keep the firewall notes"}]},
            {"number": 12, "title": "Rejected firewall idea", "author": "alice",
             "state": "closed", "merged": False,
             "closed_at": "2026-03-08T00:00:00Z", "url": url.format("pull", 12),
             "body": "an idea"},
        ],
        "issues": [
            {"number": 7, "title": "Need a firewall module", "author": "dave",
             "state": "closed", "state_reason": "completed",
             "closed_at": "2026-03-05T00:00:00Z", "url": url.format("issues", 7),
             "body": "Please add one.",
             "comments_list": [{"author": "alice", "body": "on the firewall now"}]},
            {"number": 8, "title": "Firewall docs?", "author": "alice",
             "state": "closed", "state_reason": "not_planned",
             "closed_at": "2026-03-09T00:00:00Z", "url": url.format("issues", 8),
             "body": "docs for the firewall"},
        ],
        "commits": commits,
        "code_events": [
            {"path": doc, "change": "add", "commit": "a" * 40, "author": "alice",
             "date": "2026-03-05"},
            {"path": doc, "change": "modify", "commit": "b" * 40,
             "author": "alice", "date": "2026-03-06"},
            {"path": doc, "change": "delete", "commit": "c" * 40, "author": "bob",
             "date": "2026-03-07"},
        ],
        "code_graph": {"provider": "directory", "areas": [
            {"id": "avm/res/net/fw", "paths": [path, doc]}]},
        "milestones": [], "releases": [],
    }


class TestMutantsFailTheGate(_GateCase):
    """Issue #113 M8: the gate compares spotlight's answers with the STORE, so
    a spotlight that is wrong — even one internally consistent — fails it.
    The first four are the reviewer's mutants (each passed before, with
    --require-all); the rest guard the other comparisons."""

    def setUp(self):
        super().setUp()
        self.build([(_rich_bundle(), None, None)])

    def _with(self, name, fn):
        real = getattr(spotlight, name)

        def wrapped(*a, **k):
            return fn(real(*a, **k))
        with mock.patch.object(spotlight, name, wrapped):
            return self.run_gate("--require-all")

    def _fails(self, name, fn, check, needle=None):
        code, out, _ = self._with(name, fn)
        self.assertEqual(code, 1, out)
        ln = self.line(out, check)
        self.assertTrue(ln.startswith("✗"), out)
        if needle:
            self.assertIn(needle, ln)

    def test_correct_store_passes_with_require_all(self):
        code, out, _ = self.run_gate("--require-all")
        self.assertEqual(code, 0, out)
        self.assertIn("person `alice`", out)
        conn = graphstore.open_store_readonly(self.db)
        try:
            res = spotlight.person_impact(conn, "acme", "alice")
        finally:
            conn.close()
        outcomes = sorted(t["outcome"] for t in res["delivered"])
        # several trains of several outcomes: the mutants below have teeth
        self.assertEqual(outcomes, ["abandoned", "direct_push", "in_flight",
                                    "rejected", "shipped"])

    # --- the reviewer's four mutants ---------------------------------------
    def test_person_drops_a_train(self):
        def drop(res):
            gone = res["delivered"].pop()
            res["summary"]["trains_touched"] -= 1
            res["summary"]["shipped"] -= gone["outcome"] == "shipped"
            for tp in gone["touchpoints"]:
                etype = {"author": "authored", "reviewer": "reviewed",
                         "merger": "merged", "reporter": "reported",
                         "commenter": "commented"}[tp["role"]]
                res["summary"][etype] -= 1
            return res
        self._fails("person_impact", drop, "person", "touchpoints differ")

    def test_person_relabels_shipped_as_rejected(self):
        def relabel(res):
            for t in res["delivered"]:
                if t["outcome"] == "shipped":
                    t["outcome"] = "rejected"
                    res["summary"]["shipped"] -= 1
            return res
        self._fails("person_impact", relabel, "person", "stored records give")

    def test_symbol_rewrites_every_event_and_author(self):
        def rewrite(res):
            for r in res["delivered"][0]["timeline"]:
                r["event"] = "add"
                r["author"] = "mallory"
            res["delivered"][0]["outcome"] = "alive"
            res["summary"]["outcome"] = "alive"
            return res
        self._fails("pattern_evolution", rewrite, "symbol", "ledger row")

    def test_grep_fabricates_titles_and_excerpts(self):
        def fabricate(res):
            for t in res["delivered"]:
                for tp in t["touchpoints"]:
                    tp["title"] = "Totally unrelated"
                    tp["excerpt"] = "words nobody wrote"
            return res
        self._fails("text_mining", fabricate, "grep")

    # --- more mutants --------------------------------------------------------
    def test_grep_fabricates_only_the_excerpt(self):
        def fabricate(res):
            for t in res["delivered"]:
                for tp in t["touchpoints"]:
                    tp["excerpt"] = "words nobody wrote"
            return res
        self._fails("text_mining", fabricate, "grep", "excerpt")

    def test_person_swaps_a_role(self):
        def swap(res):
            for t in res["delivered"]:
                for tp in t["touchpoints"]:
                    if tp["role"] == "author":
                        tp["role"] = "reviewer"
                        return res
            return res
        self._fails("person_impact", swap, "person", "touchpoints differ")

    def test_person_lists_a_target_in_two_trains(self):
        def dup(res):
            res["delivered"][1]["touchpoints"].append(
                dict(res["delivered"][0]["touchpoints"][0]))
            return res
        self._fails("person_impact", dup, "person", "exactly one")

    def test_person_invents_a_train(self):
        def invent(res):
            fake = copy.deepcopy(res["delivered"][0])
            fake["anchor"] = fake["anchor"] + "-copy"
            fake["touchpoints"] = []
            res["delivered"].append(fake)
            res["summary"]["trains_touched"] += 1
            res["summary"]["shipped"] += fake["outcome"] == "shipped"
            return res
        self._fails("person_impact", invent, "person", "no touchpoint")

    def test_person_miscounts_a_role(self):
        def miscount(res):
            res["summary"]["commented"] = res["summary"].get("commented", 0) + 1
            return res
        self._fails("person_impact", miscount, "person", "summary.commented")

    def test_person_fabricates_a_comment_excerpt(self):
        def fabricate(res):
            for t in res["delivered"]:
                for tp in t["touchpoints"]:
                    if tp["role"] == "commenter":
                        tp["excerpt"] = "I never said this"
            return res
        self._fails("person_impact", fabricate, "person", "comment excerpt")

    def test_timeline_title_fabricated(self):
        def fabricate(res):
            for t in res["delivered"]:
                for r in t["timeline"]:
                    if r.get("title"):
                        r["title"] = "Something else"
            return res
        self._fails("subsystem_split", fabricate, "subsystem", "title")

    def test_timeline_url_rewritten(self):
        def rewrite(res):
            for t in res["delivered"]:
                for r in t["timeline"]:
                    if r.get("url"):
                        r["url"] = "https://elsewhere.invalid/x"
            return res
        self._fails("person_impact", rewrite, "person", "url")

    def test_timeline_dates_shifted_in_order(self):
        # every row moved by the same amount keeps the order; the gate must
        # still compare each date with the stored node's (review finding)
        def shift(res):
            for t in res["delivered"]:
                for r in t["timeline"] + t["touchpoints"]:
                    if r.get("ts"):
                        r["ts"] = "1999" + r["ts"][4:]
            return res
        self._fails("person_impact", shift, "person", "is dated")

    def test_subsystem_outcome_flipped(self):
        def flip(res):
            for t in res["delivered"]:
                t["outcome"] = "in_flight" if t["outcome"] == "shipped" \
                    else "shipped"
            res["summary"]["shipped"] = sum(
                t["outcome"] == "shipped" for t in res["delivered"])
            return res
        self._fails("subsystem_split", flip, "subsystem", "stored records give")

    def test_symbol_shifts_a_date(self):
        def shift(res):
            res["delivered"][0]["timeline"][0]["ts"] = "2020-01-01"
            return res
        self._fails("pattern_evolution", shift, "symbol")

    def test_symbol_outcome_ignores_last_ledger_row(self):
        def alive(res):
            res["delivered"][0]["timeline"][-1]["event"] = "modify"
            res["delivered"][0]["outcome"] = "alive"
            return res
        self._fails("pattern_evolution", alive, "symbol")


class TestStoreOutcomeRule(unittest.TestCase):
    """The gate's own outcome rule (LLD §9) on the precedence cases, and its
    agreement with spotlight's on each: the live gate must not fail a correct
    store because the two rules read a mixed train differently."""

    @staticmethod
    def _n(local, cls="social", **data):
        return {"id": "o/r#" + local, "node_class": cls, "data": data}

    CASES = [
        ("merged PR beats an open one", ["pr-1:merged", "pr-2:open"], "shipped"),
        ("open PR beats a rejected one", ["pr-1:open", "pr-2:rejected"],
         "in_flight"),
        ("all PRs closed unmerged", ["pr-1:rejected", "pr-2:rejected"],
         "rejected"),
        ("PR outcome beats issues", ["pr-1:rejected", "issue-1:open"], "rejected"),
        ("no PR: an open issue beats a completed one",
         ["issue-1:open", "issue-2:completed"], "in_flight"),
        ("no PR: completed issue", ["issue-1:completed"], "shipped"),
        ("no PR: closed issue without a reason reads completed",
         ["issue-1:closed"], "shipped"),
        ("dropped issue plus commits", ["issue-1:not_planned", "commit"],
         "direct_push"),
        ("only dropped issues", ["issue-1:duplicate", "issue-2:not_planned"],
         "abandoned"),
        ("commits only", ["commit"], "direct_push"),
        ("an issue id holding a merged PR is a PR",
         ["issue-9:pullmerged", "issue-1:open"], "shipped"),
        ("release", ["release-v1", "pr-1:open"], "shipped"),
        ("leaves only", ["review-1-2"], "in_flight"),
    ]

    def _nodes(self, specs):
        out = []
        for spec in specs:
            local, _, how = spec.partition(":")
            if spec == "commit":
                out.append(self._n("f" * 40, "code", sha="f" * 40))
            elif local.startswith("release-"):
                out.append(self._n(local, "structure", tag_name="v1"))
            elif local.startswith("pr-"):
                out.append(self._n(local, merged=how == "merged",
                                   state="open" if how == "open" else "closed"))
            elif how == "pullmerged":
                out.append(self._n(local, merged=True, state="closed",
                                   url="https://github.com/o/r/pull/9"))
            elif local.startswith("issue-"):
                data = {"state": "open" if how == "open" else "closed"}
                if how in ("completed", "not_planned", "duplicate"):
                    data["state_reason"] = how
                out.append(self._n(local, **data))
            else:
                out.append(self._n(local))
        return out

    def test_precedence_cases(self):
        for name, specs, want in self.CASES:
            nodes = self._nodes(specs)
            self.assertEqual(gate._store_outcome(nodes), want, name)
            self.assertEqual(spotlight._outcome(nodes), want, name)


class TestDeepTrainIsNotAFalseFailure(_GateCase):
    """A causal chain deeper than the traversal cap (eight PRs each closing
    the previous PR's issue) entered at both ends and the middle: spotlight
    may then list one record in two trains. That is the reader's documented
    depth cap, not a wrong answer, so the gate's 'exactly one train' rule is
    relaxed there (and only there) to 'at least one'."""

    def setUp(self):
        super().setUp()
        url = "https://github.com/acme/r1/{}/{}"
        prs, issues = [], []
        for k in range(1, 9):
            prs.append({"number": 100 + k, "title": "step {}".format(k),
                        "author": "alice" if k in (1, 4, 5, 8) else "bob",
                        "merged": True, "state": "closed",
                        "merged_at": "2026-03-{:02d}T00:00:00Z".format(k),
                        "closes": [k - 1, k] if k > 1 else [k],
                        "url": url.format("pull", 100 + k)})
            issues.append({"number": k, "title": "issue {}".format(k),
                           "author": "alice", "state": "closed",
                           "closed_at": "2026-03-{:02d}T00:00:00Z".format(k),
                           "url": url.format("issues", k)})
        self.build([({"meta": {"owner": "acme", "repo": "r1",
                               "from": "2026-03-01", "to": "2026-03-31"},
                      "prs": prs, "issues": issues, "commits": [],
                      "code_events": [], "milestones": [], "releases": []},
                     None, None)])

    def test_gate_passes(self):
        conn = graphstore.open_store_readonly(self.db)
        try:
            res = spotlight.person_impact(conn, "acme", "alice")
        finally:
            conn.close()
        seen = [tp["id"] for t in res["delivered"] for tp in t["touchpoints"]]
        self.assertGreater(len(seen), len(set(seen)))   # the relaxed case occurs
        code, out, _ = self.run_gate()
        self.assertTrue(self.line(out, "person").startswith("✓ person `alice`"),
                        out)
        self.assertEqual(code, 0, out)


class TestCli(_GateCase):
    def test_unknown_project_is_usage_error(self):
        self.build([(_load("bundle_p3b.json"), None, None)])
        code, out, err = self.run_gate("--project", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        self.assertEqual(out, "")

    def test_missing_store_is_usage_error_and_not_created(self):
        code, out, err = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
