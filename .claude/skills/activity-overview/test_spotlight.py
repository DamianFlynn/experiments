"""Phase 8 tests: the fts_text indexing prerequisite on the fold path, and the
spotlight reader realigned to the chronological delivery-train contract.

TDD-first. A crafted small bundle is folded into an in-memory store; the
query envelopes are asserted against the unified delivery-train shape
(query/focus/focus_kind/project/status/scope/summary/<context>/delivered).
A real-data smoke runs against workspace/journey.db when present.
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402

HERE = os.path.dirname(__file__)
REAL_STORE = os.path.join(HERE, "workspace", "journey.db")


def _crafted_bundle(repo="r1"):
    """A small bundle exercising every person field for login 'alice':
    she authors PR 1 (which closes issue 9 -> a train), reviews PR 2, comments
    on PR 2, reports issue 9, and authors a symbol in main.bicep that is later
    removed (by anyone). Searchable text lives in titles/bodies/comments/commit
    messages so the FTS prerequisite has content."""
    return {
        "meta": {"owner": "acme", "repo": repo,
                 "from": "2026-01-01", "to": "2026-01-31"},
        "prs": [
            {"number": 1, "title": "Add firewall policy",
             "body": "Implements the firewall policy. Closes #9",
             "author": "alice", "merged_by": "bob", "merged_at": "2026-01-10T00:00:00Z",
             "created_at": "2026-01-05T00:00:00Z", "state": "closed", "merged": True,
             "closes": [9], "reviewers": ["carol"],
             "url": "https://github.com/acme/{}/pull/1".format(repo),
             "comments_list": [{"author": "dave", "body": "nice work on the policy"}]},
            {"number": 2, "title": "Refactor storage module",
             "body": "Cleans up the storage module internals.",
             "author": "bob", "merged_by": "bob", "merged_at": "2026-01-20T00:00:00Z",
             "created_at": "2026-01-15T00:00:00Z", "state": "closed", "merged": True,
             "reviewers": ["alice"],
             "url": "https://github.com/acme/{}/pull/2".format(repo),
             "comments_list": [{"author": "alice", "body": "looks good to me"}]},
        ],
        "issues": [
            {"number": 9, "title": "Firewall policy missing",
             "body": "We need a firewall policy module.",
             "author": "alice", "state": "closed",
             "closed_at": "2026-01-10T00:00:00Z", "updated_at": "2026-01-10T00:00:00Z",
             "url": "https://github.com/acme/{}/issues/9".format(repo),
             "comments_list": [{"author": "bob", "body": "agreed, this is needed"}]},
        ],
        "commits": [
            {"sha": "c0ffee1", "message": "Add firewall policy (#1)",
             "author": "alice", "date": "2026-01-08T00:00:00Z"},
        ],
        "symbol_events": [
            {"path": "main.bicep", "lang": "bicep", "subkind": "resource",
             "name": "fw", "change": "add", "commit": "c0ffee1",
             "author": "alice", "date": "2026-01-08T00:00:00Z",
             "before": None, "after": "resource fw ..."},
            {"path": "main.bicep", "lang": "bicep", "subkind": "resource",
             "name": "fw", "change": "drop", "commit": "deadbee",
             "author": "bob", "date": "2026-01-25T00:00:00Z",
             "before": "resource fw ...", "after": None},
        ],
    }


class TestFtsPrerequisite(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)

    def test_fold_populates_fts_text(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        gather.fold_bundle(self.conn, _crafted_bundle())
        rows = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        self.assertGreater(rows, 0)

    def test_fts_search_finds_owning_node(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        gather.fold_bundle(self.conn, _crafted_bundle())
        # phrase from PR 1 body -> the PR social node id
        hits = graphstore.fts_search(self.conn, "firewall")
        self.assertIn(graphstore.qualify_id("acme", "r1", "pr-1"), hits)
        # comment body -> still the owning PR node
        hits2 = graphstore.fts_search(self.conn, '"nice work"')
        self.assertIn(graphstore.qualify_id("acme", "r1", "pr-1"), hits2)
        # commit message -> the commit code node
        hits3 = graphstore.fts_search(self.conn, "policy")
        self.assertIn(graphstore.qualify_id("acme", "r1", "c0ffee1"), hits3)

    def test_indexing_idempotent(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        b = _crafted_bundle()
        gather.fold_bundle(self.conn, b)
        n1 = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        gather.fold_bundle(self.conn, b)  # re-fold
        n2 = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        self.assertEqual(n1, n2)


class TestPersonImpact(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_envelope(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "person")
        self.assertEqual(res["focus"], "alice")
        self.assertEqual(res["focus_kind"], "person")
        self.assertEqual(res["project"], "acme")
        # scope omitted -> all-history
        self.assertEqual(res["scope"], "all-history")
        # context blocks present
        self.assertEqual(res["is_bot"], False)
        self.assertEqual(res["areas"], [])
        self.assertEqual(res["modules"], [])
        self.assertIn("symbols_authored", res)
        self.assertIn("authored_then_removed", res)

    def test_summary_counts(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        s = res["summary"]
        # per-role counts (authored = PR1 + commit)
        self.assertEqual(s["authored"], 2)
        self.assertEqual(s["reviewed"], 1)
        self.assertEqual(s["commented"], 1)
        self.assertEqual(s["reported"], 1)
        self.assertNotIn("merged", s)  # alice merged nothing
        self.assertIn("trains_touched", s)
        self.assertIn("shipped", s)
        # the PR1/issue9/commit train + the PR2 train she reviewed; both PRs
        # merged, so both trains shipped
        self.assertEqual(s["trains_touched"], 2)
        self.assertEqual(s["shipped"], 2)

    def test_delivered_train(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        delivered = res["delivered"]
        pr1 = graphstore.qualify_id("acme", "r1", "pr-1")
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        issue9 = graphstore.qualify_id("acme", "r1", "issue-9")
        commit = graphstore.qualify_id("acme", "r1", "c0ffee1")
        # the firewall train reaches issue9 <- pr1 <- commit (part_of/closes);
        # its anchor is the train's ORIGIN — the issue (issue beats pr beats
        # commit), so the headline reads as the originating issue, not a commit.
        fw_anchor = issue9
        anchors = {t["anchor"] for t in delivered}
        self.assertIn(fw_anchor, anchors)
        self.assertIn(pr2, anchors)

        fw = [t for t in delivered if t["anchor"] == fw_anchor][0]
        self.assertEqual(fw["outcome"], "shipped")
        # the headline is the issue's title, not the commit message
        self.assertEqual(fw["title"], "Firewall policy missing")
        self.assertIn("title", fw)
        self.assertIn("areas", fw)
        self.assertIn("roles", fw)
        # alice authored PR1 + reported issue9 -> both roles appear
        self.assertIn("author", fw["roles"])
        self.assertIn("reporter", fw["roles"])
        # touchpoints are her own cited events in the train, chronological
        tp_ids = [tp["id"] for tp in fw["touchpoints"]]
        self.assertIn(pr1, tp_ids)
        self.assertIn(issue9, tp_ids)
        for tp in fw["touchpoints"]:
            self.assertIn("role", tp)
            self.assertIn("ts", tp)
        # the PR1 touchpoint cites number/url/title
        pr1_tp = [tp for tp in fw["touchpoints"] if tp["id"] == pr1][0]
        self.assertEqual(pr1_tp["number"], 1)
        self.assertEqual(pr1_tp["url"], "https://github.com/acme/r1/pull/1")
        # timeline is the full cited spine, chronological by (ts, id)
        keys = [(r.get("ts") or "", r["id"]) for r in fw["timeline"]]
        self.assertEqual(keys, sorted(keys))
        for r in fw["timeline"]:
            self.assertIn("id", r)
            self.assertIn("kind", r)

    def test_comment_touchpoint_carries_excerpt(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        train = [t for t in res["delivered"] if t["anchor"] == pr2][0]
        # alice commented + reviewed on PR2; the comment touchpoint (role
        # 'commenter') carries the bounded excerpt of her own comment body
        comment_tp = [tp for tp in train["touchpoints"]
                      if tp["role"] == "commenter"]
        self.assertTrue(comment_tp)
        self.assertIn("excerpt", comment_tp[0])
        self.assertIn("looks good", comment_tp[0]["excerpt"])

    def test_delivered_chronological_shipped_emphasized(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        delivered = res["delivered"]
        # ordered by (key_date, anchor); each carries its outcome
        for t in delivered:
            self.assertIn(t["outcome"],
                          ("shipped", "rejected", "in_flight"))
        keys = [(t.get("key_date") or "", t["anchor"]) for t in delivered]
        self.assertEqual(keys, sorted(keys))

    def test_scope_echoed_when_bounded(self):
        res = spotlight.person_impact(self.conn, "acme", "alice",
                                      ts_from="2026-01-01", ts_to="2026-01-31")
        self.assertEqual(res["scope"], {"from": "2026-01-01", "to": "2026-01-31"})

    def test_from_to_filters_trains(self):
        # the PR2 train (key date 2026-01-20) is excluded by an early --to;
        # the firewall train (PR1 merged 2026-01-10) survives.
        res = spotlight.person_impact(self.conn, "acme", "alice",
                                      ts_from="2026-01-01", ts_to="2026-01-12")
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        anchors = {t["anchor"] for t in res["delivered"]}
        self.assertNotIn(pr2, anchors)
        self.assertGreaterEqual(len(res["delivered"]), 1)

    def test_symbols_context(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        self.assertEqual(len(res["symbols_authored"]), 1)
        self.assertTrue(res["symbols_authored"][0]["id"].endswith(
            "main.bicep#bicep:resource:fw"))
        self.assertEqual(len(res["authored_then_removed"]), 1)

    def test_needs_gather_unknown_login(self):
        res = spotlight.person_impact(self.conn, "acme", "nobody")
        self.assertEqual(res["status"], "needs_gather")
        self.assertIn("guidance", res)
        self.assertIn("nobody", res["guidance"])

    def test_determinism(self):
        a = json.dumps(spotlight.person_impact(self.conn, "acme", "alice"),
                       sort_keys=True)
        b = json.dumps(spotlight.person_impact(self.conn, "acme", "alice"),
                       sort_keys=True)
        self.assertEqual(a, b)

    def test_cross_repo_aggregation(self):
        # fold a second repo's window for the same login -> aggregates
        gather.fold_bundle(self.conn, _crafted_bundle(repo="r2"))
        res = spotlight.person_impact(self.conn, "acme", "alice")
        # delivered now spans trains in both repos
        anchors = [t["anchor"] for t in res["delivered"]]
        self.assertTrue(any("/r1#" in a for a in anchors))
        self.assertTrue(any("/r2#" in a for a in anchors))


class TestCausalOnlyTrains(unittest.TestCase):
    """A `cross_ref` (a casual "related to #N" mention) must NOT merge spotlight
    decision trains, even though the graph-level spine traversal — the one the
    report uses — intentionally follows it. Spotlight groups by causal links only
    (closes/part_of/spun_off/duplicate_of), so an unrelated PR that merely mentions
    an issue does not get filed under (and mis-headlined by) that issue's train."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())
        self.pr1 = graphstore.qualify_id("acme", "r1", "pr-1")    # closes #9 (causal)
        self.pr2 = graphstore.qualify_id("acme", "r1", "pr-2")    # storage refactor
        self.issue9 = graphstore.qualify_id("acme", "r1", "issue-9")
        # pr-2 merely *mentions* the firewall issue -> a casual cross-reference.
        graphstore.upsert_edge(self.conn, self.pr2, self.issue9, "cross_ref")

    def test_graph_spine_bridges_via_cross_ref(self):
        # Sanity: the default (report) traversal DOES bridge pr-2 -> issue-9.
        reached = graphstore.traverse_spine(self.conn, [self.pr2])["reached"]
        self.assertIn(self.issue9, reached)

    def test_spotlight_keeps_cross_referenced_trains_separate(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        # The train carrying pr-2 must NOT pull in the firewall train (issue-9/pr-1).
        carriers = [t for t in res["delivered"]
                    if any(row["id"] == self.pr2 for row in t["timeline"])]
        self.assertEqual(len(carriers), 1, "pr-2 should sit in exactly one train")
        ids = {row["id"] for row in carriers[0]["timeline"]}
        self.assertNotIn(self.issue9, ids)
        self.assertNotIn(self.pr1, ids)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.store = os.path.join(HERE, "workspace", "_spotlight_test.db")
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        if os.path.exists(self.store):
            os.remove(self.store)
        conn = graphstore.open_store(self.store)
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _crafted_bundle())
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.store + suffix
            if os.path.exists(p):
                os.remove(p)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "spotlight.py")] + list(args),
            capture_output=True, text=True)

    def test_default_json(self):
        r = self._run("person", "alice", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["focus"], "alice")
        self.assertIn("delivered", out)

    def test_person_from_to_flags(self):
        r = self._run("person", "alice", "--store", self.store,
                      "--from", "2026-01-01", "--to", "2026-01-12")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["scope"], {"from": "2026-01-01", "to": "2026-01-12"})

    def test_needs_gather_exits_zero(self):
        r = self._run("person", "nobody", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "needs_gather")

    def test_md_renders(self):
        r = self._run("person", "alice", "--store", self.store, "--md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("alice", r.stdout)

    def test_bad_query_exits_nonzero(self):
        r = self._run("bogus", "x", "--store", self.store)
        self.assertNotEqual(r.returncode, 0)

    def test_project_autodetected(self):
        r = self._run("person", "alice", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _run_offline(self, *args):
        # --complete with NO token must run offline (honest-only), never reach
        # the network. Force GITHUB_TOKEN out of the child env.
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "spotlight.py")] + list(args),
            capture_output=True, text=True, env=env)

    def test_complete_flag_accepted_offline(self):
        r = self._run_offline("person", "alice", "--store", self.store,
                              "--complete", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["delivered"])
        self.assertIn("complete", out["delivered"][0])
        self.assertIn("honest-only", r.stderr)  # offline notice emitted

    def test_complete_budget_flag_parses(self):
        r = self._run_offline("person", "alice", "--store", self.store,
                              "--complete", "--complete-budget", "5", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)


_SYM = lambda path, name, change, commit, author, date, before, after: {
    "path": path, "lang": "bicep", "subkind": "resource", "name": name,
    "change": change, "commit": commit, "author": author, "date": date,
    "before": before, "after": after}


def _evolution_create_bundle(repo="r1"):
    """Window 1: the bicep resource symbol `fw` is added in old.bicep and then
    changed there. (Folded separately from the move window so each fold's
    drop/add sets stay UNIQUE — match_symbol_moves is window-wide and would
    otherwise see the creation `add` and the move `add` as an ambiguous pair.)"""
    return {
        "meta": {"owner": "acme", "repo": repo,
                 "from": "2026-01-01", "to": "2026-01-15"},
        "prs": [], "issues": [],
        "commits": [
            {"sha": "aaa1111", "message": "add fw", "author": "alice",
             "date": "2026-01-05T00:00:00Z",
             "url": "https://github.com/acme/{}/commit/aaa1111".format(repo)},
            {"sha": "bbb2222", "message": "tweak fw", "author": "alice",
             "date": "2026-01-10T00:00:00Z",
             "url": "https://github.com/acme/{}/commit/bbb2222".format(repo)},
        ],
        "symbol_events": [
            _SYM("old.bicep", "fw", "add", "aaa1111", "alice",
                 "2026-01-05T00:00:00Z", None, "resource fw v1"),
            _SYM("old.bicep", "fw", "change", "bbb2222", "alice",
                 "2026-01-10T00:00:00Z", "resource fw v1", "resource fw v2"),
        ],
    }


def _evolution_move_bundle(repo="r1"):
    """Window 2: `fw` is dropped from old.bicep and added in new.bicep — a
    UNIQUE-name move within this window, so match_symbol_moves links old->new
    (replaced_by old->new, identity_from new->old). Combined with window 1 the
    source artifact's lifecycle spans add+change+remove in date order."""
    return {
        "meta": {"owner": "acme", "repo": repo,
                 "from": "2026-01-16", "to": "2026-01-31"},
        "prs": [], "issues": [],
        "commits": [
            {"sha": "ccc3333", "message": "move fw to new.bicep", "author": "bob",
             "date": "2026-01-20T00:00:00Z",
             "url": "https://github.com/acme/{}/commit/ccc3333".format(repo)},
        ],
        "symbol_events": [
            _SYM("old.bicep", "fw", "drop", "ccc3333", "bob",
                 "2026-01-20T00:00:00Z", "resource fw v2", None),
            _SYM("new.bicep", "fw", "add", "ccc3333", "bob",
                 "2026-01-20T00:00:00Z", None, "resource fw v2"),
        ],
    }


class TestPatternEvolution(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _evolution_create_bundle())
        gather.fold_bundle(self.conn, _evolution_move_bundle())
        self.src = graphstore.qualify_id(
            "acme", "r1", "old.bicep#bicep:resource:fw")
        self.dst = graphstore.qualify_id(
            "acme", "r1", "new.bicep#bicep:resource:fw")

    def test_envelope(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "symbol")
        self.assertEqual(res["focus"], self.src)
        self.assertEqual(res["focus_kind"], "symbol")
        self.assertEqual(res["project"], "acme")
        self.assertEqual(res["scope"], "all-history")
        self.assertIn("identity_chain", res)  # context block
        self.assertIn("summary", res)

    def test_delivered_single_lifecycle_train(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        delivered = res["delivered"]
        # a single train = the artifact's lifecycle
        self.assertEqual(len(delivered), 1)
        train = delivered[0]
        self.assertEqual(train["anchor"], self.src)
        # outcome removed (last event is a remove)
        self.assertEqual(train["outcome"], "removed")
        # timeline rows in (date/ts, commit/id) order, each cited
        tl = train["timeline"]
        self.assertEqual([r["event"] for r in tl], ["add", "change", "remove"])
        self.assertEqual([r.get("sha") for r in tl],
                         ["aaa1111", "bbb2222", "ccc3333"])
        for r in tl:
            self.assertIn("kind", r)
            self.assertIn("ts", r)
        # before/after preserved as context on symbol rows
        self.assertEqual(tl[0]["before"], None)
        self.assertEqual(tl[0]["after"], "resource fw v1")
        self.assertEqual(tl[1]["before"], "resource fw v1")

    def test_summary(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        s = res["summary"]
        self.assertEqual(s["events"], 3)
        self.assertEqual(s["outcome"], "removed")

    def test_identity_chain_context(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        chain = res["identity_chain"]
        self.assertEqual([c["id"] for c in chain], [self.src, self.dst])
        link = [c for c in chain if c.get("confidence")]
        self.assertTrue(link)
        self.assertIn(link[0]["confidence"], ("high", "medium"))
        self.assertIn("basis", link[0])

    def test_chain_from_dst_same_chain(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.dst)
        self.assertEqual([c["id"] for c in res["identity_chain"]],
                         [self.src, self.dst])

    def test_from_to_bounds_lifecycle(self):
        # bound to window 1 only -> add + change, no remove; outcome alive
        res = spotlight.pattern_evolution(
            self.conn, "acme", self.src,
            ts_from="2026-01-01", ts_to="2026-01-15")
        self.assertEqual(res["scope"], {"from": "2026-01-01", "to": "2026-01-15"})
        tl = res["delivered"][0]["timeline"]
        self.assertEqual([r["event"] for r in tl], ["add", "change"])
        self.assertEqual(res["delivered"][0]["outcome"], "alive")

    def test_accepts_local_id(self):
        res = spotlight.pattern_evolution(
            self.conn, "acme", "old.bicep#bicep:resource:fw")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["focus"], self.src)

    def test_needs_gather_unknown(self):
        res = spotlight.pattern_evolution(
            self.conn, "acme", "nope.bicep#bicep:resource:ghost")
        self.assertEqual(res["status"], "needs_gather")
        self.assertIn("guidance", res)

    def test_determinism(self):
        a = json.dumps(spotlight.pattern_evolution(self.conn, "acme", self.src),
                       sort_keys=True)
        b = json.dumps(spotlight.pattern_evolution(self.conn, "acme", self.src),
                       sort_keys=True)
        self.assertEqual(a, b)


def _subsystem_store(conn):
    """Seed a store directly (areas/owns/depends_on are sparse via fold, so we
    craft the graph) with one area that has: a codeowner (owns), two PRs that
    touch it via their commits (one merged=shipped, one open=stalled, and the
    merged PR closes an issue so its train has a spine), and a depends_on edge
    in each direction."""
    P, R = "acme", "r1"
    qid = lambda local: graphstore.qualify_id(P, R, local)
    area = "area-avm/res/net/fw"
    dep_up = "area-avm/res/net/base"   # fw depends_on base (forward / out)
    dep_dn = "area-avm/res/net/app"    # app depends_on fw (reverse / in)
    fetched = "2026-01-01T00:00:00Z"
    nodes = [
        (qid(area), P, R, "structure", None,
         {"id": "avm/res/net/fw", "paths": ["avm/res/net/fw"]}, fetched),
        (qid(dep_up), P, R, "structure", None, {"id": "avm/res/net/base"}, fetched),
        (qid(dep_dn), P, R, "structure", None, {"id": "avm/res/net/app"}, fetched),
        (graphstore.qualify_person(P, "owner1"), P, "*", "structure", None,
         {"login": "owner1"}, fetched),
        # PR 1 merged (shipped), closes issue 9; PR 2 open (stalled)
        (qid("pr-1"), P, R, "social", "2026-01-10T00:00:00Z",
         {"number": 1, "title": "ship fw", "state": "closed", "merged": True,
          "url": "https://github.com/acme/r1/pull/1"}, fetched),
        (qid("pr-2"), P, R, "social", "2026-01-15T00:00:00Z",
         {"number": 2, "title": "wip fw", "state": "open", "merged": False,
          "url": "https://github.com/acme/r1/pull/2"}, fetched),
        (qid("issue-9"), P, R, "social", "2026-01-08T00:00:00Z",
         {"number": 9, "title": "need fw", "state": "closed",
          "url": "https://github.com/acme/r1/issues/9"}, fetched),
        # commits that touch the area, each part_of a PR
        (qid("sha1"), P, R, "code", "2026-01-09T00:00:00Z",
         {"sha": "sha1", "author": "carol"}, fetched),
        (qid("sha2"), P, R, "code", "2026-01-14T00:00:00Z",
         {"sha": "sha2", "author": "dave"}, fetched),
    ]
    graphstore.upsert_nodes(conn, nodes)
    edges = [
        (graphstore.qualify_person(P, "owner1"), qid(area), "owns", None, None),
        (qid("sha1"), qid(area), "touches", None, None),
        (qid("sha2"), qid(area), "touches", None, None),
        (qid("sha1"), qid("pr-1"), "part_of", None, None),
        (qid("sha2"), qid("pr-2"), "part_of", None, None),
        (qid("pr-1"), qid("issue-9"), "closes", None, None),
        (qid(area), qid(dep_up), "depends_on", None,
         {"version": "1.0.0", "transitive": False}),
        (qid(dep_dn), qid(area), "depends_on", None,
         {"version": "2.0.0", "transitive": True}),
    ]
    graphstore.upsert_edges(conn, edges)


class TestSubsystemSplit(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        _subsystem_store(self.conn)

    def test_envelope(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "subsystem")
        self.assertEqual(res["focus"], "avm/res/net/fw")
        self.assertEqual(res["focus_kind"], "subsystem")
        self.assertEqual(res["project"], "acme")
        self.assertEqual(res["scope"], "all-history")
        self.assertIn("contributors", res)   # context block
        self.assertIn("depends_on", res)     # context block

    def test_contributors_context(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        relations = {c["login"]: c["relation"] for c in res["contributors"]}
        self.assertEqual(relations.get("owner1"), "owns")
        self.assertIn("carol", relations)
        self.assertEqual(relations["carol"], "touches")
        logins = [c["login"] for c in res["contributors"]]
        self.assertEqual(logins, sorted(logins))

    def test_depends_on_context(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        deps_out = res["depends_on"]["out"]
        deps_in = res["depends_on"]["in"]
        self.assertEqual([d["area"] for d in deps_out], ["avm/res/net/base"])
        self.assertEqual(deps_out[0]["version"], "1.0.0")
        self.assertEqual([d["area"] for d in deps_in], ["avm/res/net/app"])
        self.assertEqual(deps_in[0]["transitive"], True)

    def test_summary(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        s = res["summary"]
        # two trains touched the area (PR1 train + PR2 train), one shipped
        self.assertEqual(s["trains"], 2)
        self.assertEqual(s["shipped"], 1)
        self.assertEqual(s["contributors"], len(res["contributors"]))

    def test_delivered_trains(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        delivered = res["delivered"]
        outcomes = {t["anchor"]: t["outcome"] for t in delivered}
        pr1 = graphstore.qualify_id("acme", "r1", "pr-1")
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        issue9 = graphstore.qualify_id("acme", "r1", "issue-9")
        # PR1 train (anchored on its ORIGIN issue) shipped; PR2 in_flight
        pr1_anchor = issue9
        self.assertEqual(outcomes[pr1_anchor], "shipped")
        self.assertEqual(outcomes[pr2], "in_flight")
        # touchpoints = the area-touching nodes in each train (the commits)
        pr1_train = [t for t in delivered if t["anchor"] == pr1_anchor][0]
        tp_ids = [tp["id"] for tp in pr1_train["touchpoints"]]
        self.assertIn(graphstore.qualify_id("acme", "r1", "sha1"), tp_ids)
        # chronological by (key_date, anchor)
        keys = [(t.get("key_date") or "", t["anchor"]) for t in delivered]
        self.assertEqual(keys, sorted(keys))
        # full spine timeline cited
        for t in delivered:
            for r in t["timeline"]:
                self.assertIn("kind", r)

    def test_time_range_filter(self):
        # PR2 train (key date 2026-01-15) excluded by an early --to
        res = spotlight.subsystem_split(
            self.conn, "acme", "avm/res/net/fw",
            ts_from="2026-01-01", ts_to="2026-01-12")
        self.assertEqual(res["scope"], {"from": "2026-01-01", "to": "2026-01-12"})
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        anchors = {t["anchor"] for t in res["delivered"]}
        self.assertNotIn(pr2, anchors)

    def test_needs_gather_unknown_area(self):
        res = spotlight.subsystem_split(self.conn, "acme", "no/such/area")
        self.assertEqual(res["status"], "needs_gather")
        self.assertIn("guidance", res)

    def test_determinism(self):
        a = json.dumps(spotlight.subsystem_split(self.conn, "acme",
                       "avm/res/net/fw"), sort_keys=True)
        b = json.dumps(spotlight.subsystem_split(self.conn, "acme",
                       "avm/res/net/fw"), sort_keys=True)
        self.assertEqual(a, b)


class TestTextMining(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_fts_unavailable_status(self):
        if graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 available — degrade path not exercised here")
        res = spotlight.text_mining(self.conn, "acme", "firewall")
        self.assertEqual(res["status"], "fts_unavailable")
        self.assertEqual(res["query"], "grep")
        self.assertIn("guidance", res)

    def test_grep_golden(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        res = spotlight.text_mining(self.conn, "acme", "firewall")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "grep")
        self.assertEqual(res["focus"], "firewall")
        self.assertEqual(res["focus_kind"], "grep")
        self.assertEqual(res["project"], "acme")
        self.assertEqual(res["scope"], "all-history")
        # "firewall" appears in PR1 body/title and issue9 title/body — both in
        # the same firewall train, anchored on the origin issue.
        issue9 = graphstore.qualify_id("acme", "r1", "issue-9")
        pr1 = graphstore.qualify_id("acme", "r1", "pr-1")
        anchors = [t["anchor"] for t in res["delivered"]]
        self.assertEqual(anchors, [issue9])  # single train, origin-anchored
        train = res["delivered"][0]
        # the matched nodes are the focus touchpoints with role "mention"
        mention_ids = {tp["id"] for tp in train["touchpoints"]}
        self.assertIn(pr1, mention_ids)
        self.assertIn(issue9, mention_ids)
        for tp in train["touchpoints"]:
            self.assertEqual(tp["role"], "mention")
            self.assertIn("excerpt", tp)
        # summary carries match + train counts
        self.assertEqual(res["summary"]["trains"], 1)
        self.assertGreaterEqual(res["summary"]["matches"], 2)
        # full cited spine timeline present and chronological
        keys = [(r.get("ts") or "", r["id"]) for r in train["timeline"]]
        self.assertEqual(keys, sorted(keys))

    def test_grep_no_matches_is_ok_empty(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        res = spotlight.text_mining(self.conn, "acme", "zzz_no_such_phrase_zzz")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["delivered"], [])
        self.assertEqual(res["summary"]["matches"], 0)
        self.assertEqual(res["summary"]["trains"], 0)

    def test_grep_input_sanitization_never_raises(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        for phrase in ("fix AND bug", 'a "b" c', "foo:bar", "-x",
                       "OR NOT *", 'unbalanced " quote'):
            res = spotlight.text_mining(self.conn, "acme", phrase)
            self.assertEqual(res["status"], "ok", phrase)
            self.assertEqual(res["query"], "grep")

    def test_grep_o_matches_only_hydrates_matches(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        # count every node in the store, then assert grep hydrates only the
        # matches + their trains, never the full history.
        total = self.conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        calls = {"n": 0}
        real_get = graphstore.get_node

        def counting_get(conn, nid):
            calls["n"] += 1
            return real_get(conn, nid)

        graphstore.get_node = counting_get
        try:
            res = spotlight.text_mining(self.conn, "acme", "firewall")
        finally:
            graphstore.get_node = real_get
        self.assertEqual(res["status"], "ok")
        # the firewall train is small (issue9/pr1/commit); hydration stays well
        # under a full-history scan even with re-fetches inside _train.
        self.assertLess(calls["n"], total * 3)

    def test_grep_from_to_filters_trains(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        # the firewall train's key date is the issue creation (2026-01-08…);
        # an early --to before it drops the train.
        res = spotlight.text_mining(self.conn, "acme", "firewall",
                                    ts_from="2026-01-01", ts_to="2026-01-02")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["delivered"], [])

    def test_grep_determinism(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        a = json.dumps(spotlight.text_mining(self.conn, "acme", "firewall"),
                       sort_keys=True)
        b = json.dumps(spotlight.text_mining(self.conn, "acme", "firewall"),
                       sort_keys=True)
        self.assertEqual(a, b)


class TestGapRender(unittest.TestCase):
    def test_complete_train_renders_no_gap_line(self):
        t = {"anchor": "a", "title": "x", "outcome": "shipped",
             "key_date": "2026-03-01", "areas": [], "roles": [],
             "touchpoints": [], "timeline": [], "complete": True, "gaps": []}
        md = spotlight._render_train_md(t)
        self.assertNotIn("gaps", md.lower())

    def test_gappy_train_renders_compact_summary(self):
        t = {"anchor": "a", "title": "x", "outcome": "shipped",
             "key_date": "2026-03-01", "areas": [], "roles": [],
             "touchpoints": [], "timeline": [], "complete": False,
             "gaps": [{"id": "acme/w#issue-5", "reason": "outside_window"},
                      {"id": "acme/w#issue-9", "reason": "not_gathered"}]}
        md = spotlight._render_train_md(t)
        self.assertIn("2 gaps", md)
        self.assertIn("outside-window", md)
        self.assertIn("not-gathered", md)


class TestGrepRender(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_grep_md_render_golden(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        res = spotlight.text_mining(self.conn, "acme", "firewall")
        md = spotlight.render_md(res)
        self.assertIn("spotlight: grep `firewall`", md)
        self.assertIn("[shipped]", md)
        self.assertIn("mention", md)
        # cited timeline + excerpt present
        self.assertIn("- timeline:", md)

    def test_grep_md_fts_unavailable(self):
        if graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 available")
        res = spotlight.text_mining(self.conn, "acme", "firewall")
        md = spotlight.render_md(res)
        self.assertIn("FTS unavailable", md)

    def test_symbol_md_has_mermaid_chain(self):
        # a two-link identity chain should render a graph LR mermaid block
        gather.fold_bundle(self.conn, _evolution_create_bundle(repo="r2"))
        gather.fold_bundle(self.conn, _evolution_move_bundle(repo="r2"))
        src = graphstore.qualify_id("acme", "r2", "old.bicep#bicep:resource:fw")
        res = spotlight.pattern_evolution(self.conn, "acme", src)
        md = spotlight.render_md(res)
        self.assertIn("```mermaid", md)
        self.assertIn("graph LR", md)


@unittest.skipUnless(os.path.exists(REAL_STORE), "real store absent")
class TestRealDataSmoke(unittest.TestCase):
    def test_person_impact_real(self):
        conn = graphstore.open_store(REAL_STORE)
        res = spotlight.person_impact(conn, "Azure", "AlexanderSehr")
        conn.close()
        self.assertEqual(res["status"], "ok")
        # non-empty delivered trains
        self.assertGreater(len(res["delivered"]), 0)
        # at least one touchpoint carries a url/number/sha citation
        any_cite = any(
            ("url" in tp or "number" in tp or "sha" in tp)
            for t in res["delivered"] for tp in t["touchpoints"])
        self.assertTrue(any_cite)

    def test_pattern_evolution_real(self):
        conn = graphstore.open_store(REAL_STORE)
        aid = ("Azure/bicep-registry-modules#"
               "avm/res/cache/redis/main.bicep")
        res = spotlight.pattern_evolution(conn, "Azure", aid)
        conn.close()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["delivered"]), 1)
        tl = res["delivered"][0]["timeline"]
        self.assertGreaterEqual(len(tl), 1)
        for r in tl:
            self.assertIn("kind", r)
        self.assertEqual(res["identity_chain"][0]["id"], aid)

    def test_subsystem_split_real_sparse(self):
        conn = graphstore.open_store(REAL_STORE)
        res = spotlight.subsystem_split(conn, "Azure", "avm/res/cache/redis")
        conn.close()
        self.assertIn(res["status"], ("ok", "needs_gather"))

    def test_text_mining_real(self):
        conn = graphstore.open_store(REAL_STORE)
        if not graphstore.fts5_available(conn):
            conn.close()
            self.skipTest("FTS5 unavailable")
        # a common substantive word; whatever it matches, the envelope is a
        # valid ok answer and every delivered train is cited.
        res = spotlight.text_mining(conn, "Azure", "redis")
        conn.close()
        self.assertEqual(res["query"], "grep")
        self.assertIn(res["status"], ("ok", "fts_unavailable"))
        if res["status"] == "ok":
            for t in res["delivered"]:
                for r in t["timeline"]:
                    self.assertIn("kind", r)


class TestReviewFixes(unittest.TestCase):
    """Regressions for the PR #14 review (Copilot): commit-only trains, date-only
    range bounds, FTS phrase sanitization, dependency render, empty-text FTS."""

    def _store(self, bundle):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, bundle)
        return conn

    # #1 — seed trains from every touched dst, not just PR/issue ids
    def test_commit_only_contributor_has_a_train(self):
        b = {"meta": {"owner": "acme", "repo": "r1",
                      "from": "2026-02-01", "to": "2026-02-28"},
             "prs": [], "issues": [],
             "commits": [{"sha": "facade1", "message": "Direct push to the pipeline",
                          "author": "zoe", "date": "2026-02-10T00:00:00Z"}]}
        conn = self._store(b)
        res = spotlight.person_impact(conn, "acme", "zoe")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["summary"].get("authored"), 1)
        self.assertEqual(len(res["delivered"]), 1,
                         "commit-only contributor's train must appear in delivered")
        ids = {r["id"] for r in res["delivered"][0]["timeline"]}
        self.assertIn(graphstore.qualify_id("acme", "r1", "facade1"), ids)

    # #2 — date-only bounds normalized so inclusive --to matches same-day datetimes
    def test_date_only_to_includes_same_day_datetime(self):
        self.assertTrue(spotlight._date_in_range("2026-01-12T14:00:00Z", None, "2026-01-12"))

    def test_date_only_from_includes_same_day_datetime(self):
        self.assertTrue(spotlight._date_in_range("2026-01-12T00:00:00Z", "2026-01-12", None))

    def test_date_range_excludes_outside(self):
        self.assertFalse(spotlight._date_in_range("2026-01-13T00:00:00Z", None, "2026-01-12"))
        self.assertFalse(spotlight._date_in_range("2026-01-11T23:00:00Z", "2026-01-12", None))

    def test_date_range_empty_ts(self):
        # an empty timestamp is excluded once filtering is in play
        self.assertFalse(spotlight._date_in_range("", None, "2026-01-12"))
        self.assertFalse(spotlight._date_in_range("", "2026-01-12", None))

    # #3 — whitespace/empty FTS phrase -> match-nothing literal, never raises
    def test_fts_query_whitespace_is_empty(self):
        self.assertEqual(spotlight._fts_query("   "), '""')
        self.assertEqual(spotlight._fts_query(""), '""')
        self.assertEqual(spotlight._fts_query(None), '""')
        self.assertEqual(spotlight._fts_query('a "b"'), '"a ""b"""')

    # #5 — subsystem render omits a missing version (no "vNone")
    def test_subsystem_render_omits_missing_version(self):
        res = {"query": "subsystem", "focus": "x", "focus_kind": "subsystem",
               "project": "acme", "status": "ok", "scope": "all-history",
               "summary": {"trains": 0, "shipped": 0, "contributors": 0},
               "contributors": [],
               "depends_on": {
                   "out": [{"area": "y", "id": "i", "version": None, "transitive": False}],
                   "in": [{"area": "z", "id": "j", "version": "1.2", "transitive": True}]},
               "delivered": []}
        md = spotlight.render_md(res)
        self.assertNotIn("vNone", md)
        self.assertIn("- depends on y", md)
        self.assertIn("- depended on by z (v1.2, transitive)", md)

    # #6 — fold does not index empty searchable text
    def test_fold_skips_empty_searchable_text(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        if not graphstore.fts5_available(conn):
            self.skipTest("FTS5 unavailable")
        b = {"meta": {"owner": "acme", "repo": "r1",
                      "from": "2026-02-01", "to": "2026-02-28"},
             "prs": [], "issues": [],
             "commits": [
                 {"sha": "aaa1111", "message": "Real change to the gateway",
                  "author": "zoe", "date": "2026-02-10T00:00:00Z"},
                 {"sha": "bbb2222", "message": "",
                  "author": "zoe", "date": "2026-02-11T00:00:00Z"}]}
        gather.fold_bundle(conn, b)
        rows = {r[0] for r in conn.execute("SELECT node_id FROM fts_text")}
        self.assertIn(graphstore.qualify_id("acme", "r1", "aaa1111"), rows)
        self.assertNotIn(graphstore.qualify_id("acme", "r1", "bbb2222"), rows)


    # #1 (2nd pass) — windowed summary counts stay consistent with delivered
    def test_summary_counts_are_scope_consistent(self):
        conn = self._store(_crafted_bundle())
        full = spotlight.person_impact(conn, "acme", "alice")
        # full history: alice reviewed + commented PR-2 (the storage train)
        self.assertEqual(full["summary"].get("reviewed"), 1)
        self.assertEqual(full["summary"].get("commented"), 1)
        # scope to the firewall train's window — excludes the later PR-2 train
        scoped = spotlight.person_impact(conn, "acme", "alice", ts_to="2026-01-10")
        pr2 = graphstore.qualify_id("acme", "r1", "pr-2")
        self.assertTrue(all(pr2 not in {r["id"] for r in t["timeline"]}
                            for t in scoped["delivered"]),
                        "PR-2 train should be filtered out by ts_to")
        # the excluded review/comment on PR-2 must NOT inflate the scoped summary
        self.assertNotIn("reviewed", scoped["summary"])
        self.assertNotIn("commented", scoped["summary"])
        self.assertEqual(scoped["summary"]["trains_touched"], len(scoped["delivered"]))


if __name__ == "__main__":
    unittest.main()


class TestHonestContract(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_every_train_carries_complete_and_gaps(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        self.assertTrue(res.get("delivered"))
        for t in res["delivered"]:
            self.assertIn("complete", t)
            self.assertIn("gaps", t)
            self.assertIsInstance(t["gaps"], list)

    def test_offline_default_self_contained_trains_are_complete(self):
        # No fetcher: trains complete-or-not from the store alone. The crafted
        # fixture's trains are self-contained, so they are complete:true offline.
        res = spotlight.person_impact(self.conn, "acme", "alice")
        for t in res["delivered"]:
            self.assertTrue(t["complete"])
            self.assertEqual(t["gaps"], [])
