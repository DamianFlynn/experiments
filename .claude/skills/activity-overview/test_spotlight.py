"""Phase 8a tests: the fts_text indexing prerequisite on the fold path, and the
spotlight reader scaffold + person-impact query.

TDD-first. A crafted small bundle is folded into an in-memory store; the
person-impact JSON is asserted against an exact ordered, cited structure.
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
    """A small bundle exercising every person-impact field for login 'alice':
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

    def test_golden(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "person")
        self.assertEqual(res["login"], "alice")
        self.assertEqual(res["is_bot"], False)
        # areas/modules come from code_graph area attribution (none in this
        # bundle), so they are empty here — the field is present and a list.
        self.assertEqual(res["areas"], [])
        self.assertEqual(res["modules"], [])

        contrib = res["contributions"]
        # grouped by type, each with count + cited rows
        self.assertEqual(contrib["authored"]["count"], 2)  # PR 1 + commit
        self.assertEqual(contrib["reviewed"]["count"], 1)  # PR 2
        self.assertEqual(contrib["commented"]["count"], 1)  # PR 2 comment
        self.assertEqual(contrib["reported"]["count"], 1)  # issue 9
        self.assertNotIn("merged", contrib)  # alice merged nothing

        # citations present on rows
        authored_pr = [r for r in contrib["authored"]["items"]
                       if r["id"].endswith("#pr-1")][0]
        self.assertEqual(authored_pr["number"], 1)
        self.assertEqual(authored_pr["url"],
                         "https://github.com/acme/r1/pull/1")
        self.assertEqual(authored_pr["title"], "Add firewall policy")
        authored_commit = [r for r in contrib["authored"]["items"]
                           if r["id"].endswith("#c0ffee1")][0]
        self.assertEqual(authored_commit["sha"], "c0ffee1")

        # symbols authored + authored_then_removed
        self.assertEqual(len(res["symbols_authored"]), 1)
        self.assertTrue(res["symbols_authored"][0]["id"].endswith(
            "main.bicep#bicep:resource:fw"))
        self.assertEqual(len(res["authored_then_removed"]), 1)

        # trains anchored: the PR1/issue9 closes train
        self.assertEqual(len(res["trains_anchored"]), 1)
        train = res["trains_anchored"][0]
        self.assertIn("anchor", train)
        self.assertGreaterEqual(train["reached"], 2)

    def test_contributions_ordered(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        # authored items ordered by (ts, dst_id)
        items = res["contributions"]["authored"]["items"]
        keys = [(r.get("ts") or "", r["id"]) for r in items]
        self.assertEqual(keys, sorted(keys))

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
        # authored now spans both repos' PR-1 + commit (2 per repo)
        ids = [r["id"] for r in res["contributions"]["authored"]["items"]]
        self.assertTrue(any("/r1#" in i for i in ids))
        self.assertTrue(any("/r2#" in i for i in ids))


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
        self.assertEqual(out["login"], "alice")

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

    def test_golden_lifecycle(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "symbol")
        self.assertEqual(res["artifact_id"], self.src)
        lc = res["lifecycle"]
        # add -> change -> remove, in (date, commit) order
        self.assertEqual([e["event"] for e in lc], ["add", "change", "remove"])
        self.assertEqual([e["commit"] for e in lc],
                         ["aaa1111", "bbb2222", "ccc3333"])
        # symbols carry bounded before/after
        self.assertEqual(lc[0]["before"], None)
        self.assertEqual(lc[0]["after"], "resource fw v1")
        self.assertEqual(lc[1]["before"], "resource fw v1")
        # each cited by commit ref/commit
        for e in lc:
            self.assertIn("commit", e)
            self.assertIn("author", e)
            self.assertIn("date", e)

    def test_golden_identity_chain(self):
        res = spotlight.pattern_evolution(self.conn, "acme", self.src)
        chain = res["identity_chain"]
        # ordered A -> B across the move
        self.assertEqual([c["id"] for c in chain], [self.src, self.dst])
        # the move link carries confidence + basis
        link = [c for c in chain if c.get("confidence")]
        self.assertTrue(link)
        self.assertIn(link[0]["confidence"], ("high", "medium"))
        self.assertIn("basis", link[0])

    def test_chain_from_dst_same_chain(self):
        # walking from the destination recovers the same ordered chain
        res = spotlight.pattern_evolution(self.conn, "acme", self.dst)
        self.assertEqual([c["id"] for c in res["identity_chain"]],
                         [self.src, self.dst])

    def test_accepts_local_id(self):
        res = spotlight.pattern_evolution(
            self.conn, "acme", "old.bicep#bicep:resource:fw")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["artifact_id"], self.src)

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
    touch it via their commits (one merged=shipped, one open=stalled), and a
    depends_on edge in each direction."""
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
        # PR 1 merged (shipped), PR 2 open (stalled)
        (qid("pr-1"), P, R, "social", "2026-01-10T00:00:00Z",
         {"number": 1, "title": "ship fw", "state": "closed", "merged": True,
          "url": "https://github.com/acme/r1/pull/1"}, fetched),
        (qid("pr-2"), P, R, "social", "2026-01-15T00:00:00Z",
         {"number": 2, "title": "wip fw", "state": "open", "merged": False,
          "url": "https://github.com/acme/r1/pull/2"}, fetched),
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

    def test_golden(self):
        res = spotlight.subsystem_split(self.conn, "acme", "avm/res/net/fw")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "subsystem")
        self.assertEqual(res["area"], "avm/res/net/fw")

        # contributors: owner1 (owns) + carol/dave via touching commits
        relations = {c["login"]: c["relation"] for c in res["contributors"]}
        self.assertEqual(relations.get("owner1"), "owns")
        # touching-commit authors surface as touches contributors
        self.assertIn("carol", relations)
        self.assertEqual(relations["carol"], "touches")
        # ordered by login
        logins = [c["login"] for c in res["contributors"]]
        self.assertEqual(logins, sorted(logins))

        # shipped (PR1 merged) vs stalled (PR2 open)
        shipped = [i["number"] for i in res["shipped"]]
        stalled = [i["number"] for i in res["stalled"]]
        self.assertEqual(shipped, [1])
        self.assertEqual(stalled, [2])
        self.assertEqual(res["shipped"][0]["url"],
                         "https://github.com/acme/r1/pull/1")

        # blast radius both directions, carrying version/transitive
        deps_out = res["depends_on"]["out"]
        deps_in = res["depends_on"]["in"]
        self.assertEqual([d["area"] for d in deps_out], ["avm/res/net/base"])
        self.assertEqual(deps_out[0]["version"], "1.0.0")
        self.assertEqual([d["area"] for d in deps_in], ["avm/res/net/app"])
        self.assertEqual(deps_in[0]["transitive"], True)

    def test_time_range_filter(self):
        # PR2 (ts 2026-01-15) excluded by an early --to
        res = spotlight.subsystem_split(
            self.conn, "acme", "avm/res/net/fw",
            ts_from="2026-01-01", ts_to="2026-01-12")
        nums = [i["number"] for i in res["shipped"] + res["stalled"]]
        self.assertEqual(nums, [1])

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


@unittest.skipUnless(os.path.exists(REAL_STORE), "real store absent")
class TestRealDataSmoke(unittest.TestCase):
    def test_person_impact_real(self):
        conn = graphstore.open_store(REAL_STORE)
        res = spotlight.person_impact(conn, "Azure", "AlexanderSehr")
        conn.close()
        self.assertEqual(res["status"], "ok")
        # non-empty contributions with citations
        total = sum(g["count"] for g in res["contributions"].values())
        self.assertGreater(total, 0)
        # at least one cited row carries a url/number/sha
        any_cite = any(
            ("url" in r or "number" in r or "sha" in r)
            for g in res["contributions"].values() for r in g["items"])
        self.assertTrue(any_cite)

    def test_pattern_evolution_real(self):
        # a real file artifact with a code_event lifecycle (109 events in store)
        conn = graphstore.open_store(REAL_STORE)
        aid = ("Azure/bicep-registry-modules#"
               "avm/res/cache/redis/main.bicep")
        res = spotlight.pattern_evolution(conn, "Azure", aid)
        conn.close()
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(len(res["lifecycle"]), 1)
        # each lifecycle row carries its commit citation
        for e in res["lifecycle"]:
            self.assertIn("commit", e)
        # identity_chain is at least the artifact itself (no moves in store)
        self.assertEqual(res["identity_chain"][0]["id"], aid)

    def test_subsystem_split_real_sparse(self):
        # owns/depends_on are empty in the short real window; touches has 4.
        # The query must run cleanly and return ok or needs_gather.
        conn = graphstore.open_store(REAL_STORE)
        res = spotlight.subsystem_split(conn, "Azure", "avm/res/cache/redis")
        conn.close()
        self.assertIn(res["status"], ("ok", "needs_gather"))


if __name__ == "__main__":
    unittest.main()
