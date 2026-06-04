import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import graphstore  # noqa: E402


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


class TestSchema(unittest.TestCase):
    def test_init_creates_core_tables(self):
        conn = _store()
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"nodes", "edges", "code_events", "meta"} <= names)

    def test_schema_version_recorded(self):
        conn = _store()
        self.assertEqual(
            graphstore.get_meta(conn, "schema_version"),
            str(graphstore.SCHEMA_VERSION),
        )


class TestIdentity(unittest.TestCase):
    def test_qualify_id_repo_scoped(self):
        self.assertEqual(
            graphstore.qualify_id("avm", "bicep-registry-modules", "pr-4821"),
            "avm/bicep-registry-modules#pr-4821",
        )

    def test_qualify_person_project_scoped(self):
        self.assertEqual(
            graphstore.qualify_person("avm", "octocat"),
            "avm#person-octocat",
        )

    def test_parse_id_splits_on_last_hash(self):
        parsed = graphstore.parse_id("avm/bicep-registry-modules#path/main.bicep#x")
        self.assertEqual(parsed["scope"], "avm/bicep-registry-modules#path/main.bicep")
        self.assertEqual(parsed["local"], "x")

    def test_qualified_ids_do_not_collide_across_repos(self):
        a = graphstore.qualify_id("avm", "repo-a", "issue-1")
        b = graphstore.qualify_id("avm", "repo-b", "issue-1")
        self.assertNotEqual(a, b)


class TestNodes(unittest.TestCase):
    def test_upsert_then_get_round_trips_data(self):
        conn = _store()
        nid = graphstore.qualify_id("p", "r", "pr-1")
        graphstore.upsert_node(
            conn, id=nid, project="p", repo="r", node_class="social",
            ts="2026-04-01T00:00:00Z", data={"number": 1, "title": "x"},
        )
        node = graphstore.get_node(conn, nid)
        self.assertEqual(node["id"], nid)
        self.assertEqual(node["node_class"], "social")
        self.assertEqual(node["data"], {"number": 1, "title": "x"})

    def test_get_missing_returns_none(self):
        conn = _store()
        self.assertIsNone(graphstore.get_node(conn, "nope#x"))

    def test_reupsert_updates_in_place_no_duplicate(self):
        conn = _store()
        nid = graphstore.qualify_id("p", "r", "issue-1")
        graphstore.upsert_node(
            conn, id=nid, project="p", repo="r", node_class="social",
            ts="2026-04-01T00:00:00Z", data={"state": "open"},
            fetched_at="2026-04-01T00:00:00Z",
        )
        graphstore.upsert_node(
            conn, id=nid, project="p", repo="r", node_class="social",
            ts="2026-04-02T00:00:00Z", data={"state": "closed"},
            fetched_at="2026-04-02T00:00:00Z",
        )
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        self.assertEqual(count, 1)
        node = graphstore.get_node(conn, nid)
        self.assertEqual(node["data"], {"state": "closed"})
        self.assertEqual(node["ts"], "2026-04-02T00:00:00Z")
        self.assertEqual(node["fetched_at"], "2026-04-02T00:00:00Z")


class TestEdges(unittest.TestCase):
    def test_upsert_edge_then_get_both_directions(self):
        conn = _store()
        graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#issue-1", "closes")
        out = graphstore.get_edges(conn, "p/r#pr-1", direction="out")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["dst_id"], "p/r#issue-1")
        self.assertEqual(out[0]["edge_type"], "closes")
        inb = graphstore.get_edges(conn, "p/r#issue-1", direction="in")
        self.assertEqual(len(inb), 1)
        self.assertEqual(inb[0]["src_id"], "p/r#pr-1")

    def test_reupsert_edge_unions_no_duplicate(self):
        conn = _store()
        graphstore.upsert_edge(conn, "a", "b", "closes")
        graphstore.upsert_edge(conn, "a", "b", "closes", data={"via": "trailer"})
        count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        self.assertEqual(count, 1)
        edge = graphstore.get_edges(conn, "a", direction="out")[0]
        self.assertEqual(edge["data"], {"via": "trailer"})

    def test_get_edges_filters_by_type(self):
        conn = _store()
        graphstore.upsert_edge(conn, "a", "b", "closes")
        graphstore.upsert_edge(conn, "a", "c", "cross_ref")
        only = graphstore.get_edges(conn, "a", direction="out", edge_types=["closes"])
        self.assertEqual([e["dst_id"] for e in only], ["b"])

    def test_get_edges_rejects_unknown_direction(self):
        conn = _store()
        with self.assertRaises(ValueError):
            graphstore.get_edges(conn, "a", direction="sideways")


class TestCodeEvents(unittest.TestCase):
    def test_add_and_get_ordered_by_date(self):
        conn = _store()
        aid = "p/r#main.bicep#bicep:param:location"
        graphstore.add_code_event(
            conn, aid, "change", "sha2", author="bob",
            date="2026-04-05T00:00:00Z", before="x", after="y", detail="bicep param location",
        )
        graphstore.add_code_event(
            conn, aid, "add", "sha1", author="ann",
            date="2026-04-01T00:00:00Z", detail="bicep param location",
        )
        events = graphstore.get_code_events(conn, aid)
        self.assertEqual([e["event"] for e in events], ["add", "change"])
        self.assertEqual(events[0]["author"], "ann")
        self.assertEqual(events[1]["after"], "y")

    def test_re_add_same_event_is_noop(self):
        conn = _store()
        aid = "p/r#main.bicep#bicep:param:x"
        for _ in range(2):
            graphstore.add_code_event(
                conn, aid, "add", "sha1", date="2026-04-01T00:00:00Z"
            )
        count = conn.execute("SELECT COUNT(*) FROM code_events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_ref_round_trips_as_dict(self):
        conn = _store()
        aid = "p/r#main.bicep#bicep:param:x"
        graphstore.add_code_event(
            conn, aid, "add", "sha1", date="2026-04-01T00:00:00Z",
            ref={"type": "commit", "id": "sha1", "url": "https://x/sha1"},
        )
        events = graphstore.get_code_events(conn, aid)
        self.assertEqual(events[0]["ref"]["type"], "commit")


def _seed_window_nodes(conn):
    rows = [
        ("pr-1", "r1", "social", "2026-04-02T00:00:00Z"),
        ("pr-2", "r1", "social", "2026-04-20T00:00:00Z"),
        ("pr-3", "r1", "social", "2026-05-10T00:00:00Z"),   # out of window
        ("pr-9", "r2", "social", "2026-04-15T00:00:00Z"),   # other repo
        ("c-1", "r1", "code", "2026-04-12T00:00:00Z"),
        ("area-1", "r1", "structure", None),                # structure: no ts
    ]
    for local, repo, klass, ts in rows:
        nid = graphstore.qualify_id("p", repo, local)
        graphstore.upsert_node(
            conn, id=nid, project="p", repo=repo, node_class=klass,
            ts=ts, data={"local": local},
        )


class TestRangeQuery(unittest.TestCase):
    def test_window_bounds_and_repo_filter(self):
        conn = _store()
        _seed_window_nodes(conn)
        got = graphstore.range_query(
            conn, "p", ["r1"], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"
        )
        locals_ = sorted(n["data"]["local"] for n in got)
        self.assertEqual(locals_, ["c-1", "pr-1", "pr-2"])  # pr-3 out, r2 excluded, area-1 null ts

    def test_node_class_filter(self):
        conn = _store()
        _seed_window_nodes(conn)
        got = graphstore.range_query(
            conn, "p", ["r1"], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z",
            node_class="social",
        )
        self.assertEqual(sorted(n["data"]["local"] for n in got), ["pr-1", "pr-2"])

    def test_multi_repo_union(self):
        conn = _store()
        _seed_window_nodes(conn)
        got = graphstore.range_query(
            conn, "p", ["r1", "r2"], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z",
            node_class="social",
        )
        self.assertIn("pr-9", [n["data"]["local"] for n in got])

    def test_empty_repos_returns_empty(self):
        conn = _store()
        _seed_window_nodes(conn)
        got = graphstore.range_query(
            conn, "p", [], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"
        )
        self.assertEqual(got, [])


class TestTraversal(unittest.TestCase):
    def _train(self, conn):
        # issue-1 <-closes- pr-1 <-part_of- commit-1 ; pr-1 -cross_ref-> issue-2
        for nid, klass in [
            ("p/r#issue-1", "social"), ("p/r#pr-1", "social"),
            ("p/r#commit-1", "code"), ("p/r#issue-2", "social"),
        ]:
            graphstore.upsert_node(
                conn, id=nid, project="p", repo="r", node_class=klass,
                ts="2026-04-01T00:00:00Z", data={},
            )
        graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#issue-1", "closes")
        graphstore.upsert_edge(conn, "p/r#commit-1", "p/r#pr-1", "part_of")
        graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#issue-2", "cross_ref")

    def test_reaches_connected_component_both_directions(self):
        conn = _store()
        self._train(conn)
        res = graphstore.traverse_spine(conn, ["p/r#issue-1"], max_depth=6)
        self.assertEqual(
            set(res["reached"]),
            {"p/r#issue-1", "p/r#pr-1", "p/r#commit-1", "p/r#issue-2"},
        )
        self.assertEqual(res["reached"]["p/r#issue-1"], 0)
        self.assertEqual(res["reached"]["p/r#pr-1"], 1)

    def test_depth_cap_limits_reach(self):
        conn = _store()
        self._train(conn)
        res = graphstore.traverse_spine(conn, ["p/r#issue-1"], max_depth=1)
        self.assertIn("p/r#pr-1", res["reached"])
        self.assertNotIn("p/r#commit-1", res["reached"])  # depth 2, beyond cap

    def test_non_spine_edges_are_not_followed(self):
        conn = _store()
        self._train(conn)
        graphstore.upsert_node(
            conn, id="p/r#area-9", project="p", repo="r",
            node_class="structure", ts=None, data={},
        )
        graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#area-9", "touches")
        res = graphstore.traverse_spine(conn, ["p/r#issue-1"], max_depth=6)
        self.assertNotIn("p/r#area-9", res["reached"])  # 'touches' not a spine type

    def test_missing_node_reported(self):
        conn = _store()
        self._train(conn)
        # an out-of-window issue referenced but never stored
        graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#issue-99", "closes")
        res = graphstore.traverse_spine(conn, ["p/r#issue-1"], max_depth=6)
        self.assertIn("p/r#issue-99", res["reached"])
        self.assertIn("p/r#issue-99", res["missing"])
        self.assertNotIn("p/r#issue-1", res["missing"])

    def test_empty_seeds_returns_empty(self):
        conn = _store()
        res = graphstore.traverse_spine(conn, [], max_depth=6)
        self.assertEqual(res, {"reached": {}, "missing": []})

    def test_empty_edge_types_returns_just_seeds(self):
        conn = _store()
        self._train(conn)
        # no allowed edge types -> no traversal; degrades to just the seeds
        # (SQLite permits `IN ()`, matching nothing — it does not raise).
        res = graphstore.traverse_spine(
            conn, ["p/r#issue-1"], max_depth=6, edge_types=[]
        )
        self.assertEqual(res, {"reached": {"p/r#issue-1": 0}, "missing": []})


def _needs_fts5():
    conn = _store()
    return graphstore.fts5_available(conn)


@unittest.skipUnless(_needs_fts5(), "SQLite build lacks FTS5")
class TestFts(unittest.TestCase):
    def test_index_and_search_matches(self):
        conn = _store()
        graphstore.index_text(conn, "p/r#comment-1", "this is a breaking change to the API")
        graphstore.index_text(conn, "p/r#comment-2", "minor docs tweak")
        hits = graphstore.fts_search(conn, "breaking change")
        self.assertEqual(hits, ["p/r#comment-1"])

    def test_reindex_same_node_no_duplicate(self):
        conn = _store()
        graphstore.index_text(conn, "p/r#comment-1", "alpha")
        graphstore.index_text(conn, "p/r#comment-1", "alpha beta")
        hits_alpha = graphstore.fts_search(conn, "alpha")
        self.assertEqual(hits_alpha, ["p/r#comment-1"])
        rows = conn.execute(
            "SELECT COUNT(*) FROM fts_text WHERE node_id=?", ("p/r#comment-1",)
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_search_no_match_returns_empty(self):
        conn = _store()
        graphstore.index_text(conn, "p/r#c", "nothing relevant")
        self.assertEqual(graphstore.fts_search(conn, "zzz"), [])


class TestMetaProvenance(unittest.TestCase):
    def test_record_window_dedups_exact_repeat(self):
        conn = _store()
        graphstore.record_window(conn, "p", "r", "2026-04-01", "2026-04-30")
        graphstore.record_window(conn, "p", "r", "2026-04-01", "2026-04-30")
        graphstore.record_window(conn, "p", "r", "2026-05-01", "2026-05-31")
        windows = graphstore.get_windows(conn)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["from"], "2026-04-01")

    def test_clone_sha_round_trips_per_repo(self):
        conn = _store()
        graphstore.set_clone_sha(conn, "p", "r1", "abc123")
        graphstore.set_clone_sha(conn, "p", "r2", "def456")
        self.assertEqual(graphstore.get_clone_sha(conn, "p", "r1"), "abc123")
        self.assertEqual(graphstore.get_clone_sha(conn, "p", "r2"), "def456")
        self.assertIsNone(graphstore.get_clone_sha(conn, "p", "missing"))


def _fold(conn):
    """Simulate one gather fold: a small train + a code event + edges."""
    for nid, klass, ts in [
        ("p/r#issue-1", "social", "2026-04-01T00:00:00Z"),
        ("p/r#pr-1", "social", "2026-04-03T00:00:00Z"),
        ("p/r#commit-1", "code", "2026-04-03T00:00:00Z"),
    ]:
        graphstore.upsert_node(
            conn, id=nid, project="p", repo="r", node_class=klass,
            ts=ts, data={"id": nid}, fetched_at="2026-04-04T00:00:00Z",
        )
    graphstore.upsert_edge(conn, "p/r#pr-1", "p/r#issue-1", "closes")
    graphstore.upsert_edge(conn, "p/r#commit-1", "p/r#pr-1", "part_of")
    graphstore.add_code_event(
        conn, "p/r#main.bicep#bicep:param:x", "add", "commit-1",
        date="2026-04-03T00:00:00Z",
    )
    graphstore.record_window(conn, "p", "r", "2026-04-01", "2026-04-30")


def _counts(conn):
    return {
        t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
        for t in ("nodes", "edges", "code_events")
    }


class TestIdempotentAccumulation(unittest.TestCase):
    def test_folding_same_window_twice_is_noop(self):
        conn = _store()
        _fold(conn)
        first = _counts(conn)
        _fold(conn)  # overlapping re-gather
        second = _counts(conn)
        self.assertEqual(first, second)
        self.assertEqual(len(graphstore.get_windows(conn)), 1)

    def test_overlapping_window_unions_new_node_only(self):
        conn = _store()
        _fold(conn)
        # an overlapping window that adds exactly one new node
        graphstore.upsert_node(
            conn, id="p/r#pr-2", project="p", repo="r", node_class="social",
            ts="2026-04-29T00:00:00Z", data={"id": "p/r#pr-2"},
        )
        self.assertEqual(_counts(conn)["nodes"], 4)
        _fold(conn)  # re-fold the original window; pr-2 must survive, nothing duplicates
        self.assertEqual(_counts(conn)["nodes"], 4)


class TestDeterminism(unittest.TestCase):
    def test_data_blob_is_key_sorted(self):
        conn = _store()
        graphstore.upsert_node(
            conn, id="p/r#x", project="p", repo="r", node_class="social",
            ts="2026-04-01T00:00:00Z", data={"b": 2, "a": 1},
            fetched_at="2026-04-01T00:00:00Z",
        )
        raw = conn.execute("SELECT data FROM nodes WHERE id='p/r#x'").fetchone()[0]
        self.assertEqual(raw, '{"a": 1, "b": 2}')

    def test_range_query_order_is_stable(self):
        conn = _store()
        for local, ts in [
            ("c", "2026-04-03T00:00:00Z"),
            ("a", "2026-04-01T00:00:00Z"),
            ("b", "2026-04-01T00:00:00Z"),
        ]:
            graphstore.upsert_node(
                conn, id=graphstore.qualify_id("p", "r", local),
                project="p", repo="r", node_class="social", ts=ts,
                data={}, fetched_at="2026-04-04T00:00:00Z",
            )
        got = graphstore.range_query(
            conn, "p", ["r"], "2026-04-01T00:00:00Z", "2026-04-30T00:00:00Z"
        )
        # tie on ts -> ordered by id; a before b before later c
        self.assertEqual([n["id"] for n in got],
                         ["p/r#a", "p/r#b", "p/r#c"])


class TestScaleSmoke(unittest.TestCase):
    def test_window_query_uses_index_over_50k_nodes(self):
        conn = _store()
        conn.execute("BEGIN")
        for i in range(50000):
            # spread across ~3 months; ~1/3 land inside the April window
            month = 4 + (i % 3)
            day = 1 + (i % 27)
            ts = "2026-{:02d}-{:02d}T00:00:00Z".format(month, day)
            conn.execute(
                "INSERT INTO nodes (id, project, repo, node_class, ts, data, fetched_at) "
                "VALUES (?, 'p', 'r', 'social', ?, '{}', '2026-04-04T00:00:00Z')",
                ("p/r#n{}".format(i), ts),
            )
        conn.commit()
        # correctness at scale: the window returns the in-range rows
        got = graphstore.range_query(
            conn, "p", ["r"], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"
        )
        self.assertGreater(len(got), 10000)
        # guard the real intent — the window scan must use idx_nodes_window, not
        # a full table scan. A query-plan assertion is deterministic across CI
        # runners/Python versions, unlike a wall-clock threshold. This EXPLAIN
        # mirrors the query range_query runs for a single repo.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM nodes WHERE project=? AND repo IN (?) "
            "AND ts IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ts, id",
            ["p", "r", "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"],
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn("idx_nodes_window", detail)


class TestBatchWrite(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)

    def test_upsert_nodes_batch_and_idempotent(self):
        rows = [
            ("p/r#pr-1", "p", "r", "social", "2026-01-01T00:00:00Z", {"n": 1}, None),
            ("p/r#issue-1", "p", "r", "social", "2026-01-02T00:00:00Z", {"n": 2}, None),
        ]
        graphstore.upsert_nodes(self.conn, rows)
        graphstore.upsert_nodes(self.conn, rows)  # re-fold: no duplication
        self.assertEqual(graphstore.get_node(self.conn, "p/r#pr-1")["data"], {"n": 1})
        count = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        self.assertEqual(count, 2)

    def test_upsert_nodes_rejects_bad_class(self):
        with self.assertRaises(ValueError):
            graphstore.upsert_nodes(
                self.conn, [("x", "p", "r", "bogus", None, {}, None)])

    def test_upsert_edges_batch_and_union(self):
        rows = [("p/r#pr-1", "p/r#issue-1", "closes", None, None),
                ("p/r#c1", "p/r#pr-1", "part_of", None, {"k": "v"})]
        graphstore.upsert_edges(self.conn, rows)
        graphstore.upsert_edges(self.conn, rows)  # re-fold: union, not append
        edges = graphstore.get_edges(self.conn, "p/r#pr-1")
        self.assertEqual(len(edges), 2)

    def test_upsert_nodes_refreshes_on_conflict(self):
        graphstore.upsert_nodes(self.conn, [
            ("p/r#pr-1", "p", "r", "social", "2026-01-01T00:00:00Z", {"v": 1}, None)])
        graphstore.upsert_nodes(self.conn, [
            ("p/r#pr-1", "p", "r", "social", "2026-02-02T00:00:00Z", {"v": 2}, None)])
        node = graphstore.get_node(self.conn, "p/r#pr-1")
        self.assertEqual(node["data"], {"v": 2})
        self.assertEqual(node["ts"], "2026-02-02T00:00:00Z")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM nodes").fetchone()[0], 1)

    def test_add_code_events_batch_set_semantics(self):
        rows = [("p/r#a.py", "add", "c1", "alice", "2026-01-01", None, None,
                 None, None, None)]
        graphstore.add_code_events(self.conn, rows)
        graphstore.add_code_events(self.conn, rows)  # set semantics: no dup
        evs = graphstore.get_code_events(self.conn, "p/r#a.py")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["event"], "add")

    def test_add_code_events_ref_round_trips(self):
        graphstore.add_code_events(self.conn, [
            ("p/r#a.py", "modify", "c2", "alice", "2026-01-01", None,
             {"old": "x.py"}, None, None, None)])
        ev = graphstore.get_code_events(self.conn, "p/r#a.py")[0]
        self.assertEqual(ev["ref"], {"old": "x.py"})

    def test_empty_batches_are_noops(self):
        graphstore.upsert_nodes(self.conn, [])
        graphstore.upsert_edges(self.conn, [])
        graphstore.add_code_events(self.conn, [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class TestDeadRefs(unittest.TestCase):
    def setUp(self):
        self.conn = _store()

    def test_record_then_is_dead(self):
        qid = graphstore.qualify_id("acme", "widget", "issue-123")
        self.assertFalse(graphstore.is_dead_ref(self.conn, qid))
        graphstore.record_dead_ref(self.conn, qid)
        self.assertTrue(graphstore.is_dead_ref(self.conn, qid))

    def test_record_is_idempotent(self):
        qid = graphstore.qualify_id("acme", "widget", "issue-123")
        graphstore.record_dead_ref(self.conn, qid)
        graphstore.record_dead_ref(self.conn, qid)  # second call must not raise
        self.assertEqual(graphstore.get_dead_refs(self.conn), [qid])

    def test_get_dead_refs_sorted(self):
        for local in ("issue-9", "issue-1", "issue-5"):
            graphstore.record_dead_ref(
                self.conn, graphstore.qualify_id("acme", "widget", local))
        got = graphstore.get_dead_refs(self.conn)
        self.assertEqual(got, sorted(got))

    def test_dead_refs_table_created_by_init(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dead_refs'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_traverse_spine_skip_dead_omits_tombstoned_missing(self):
        # PR #10 closes issue #7 (absent). Seed PR #10's train.
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        issue = graphstore.qualify_id("acme", "widget", "issue-7")
        graphstore.upsert_node(self.conn, pr, "acme", "widget", "social",
                               "2026-03-15T00:00:00Z", {"number": 10})
        graphstore.upsert_edge(self.conn, pr, issue, "closes")

        # Default: issue #7 is reported missing.
        m = graphstore.traverse_spine(self.conn, [pr])["missing"]
        self.assertIn(issue, m)

        # Tombstoned + skip_dead=True: it is omitted from missing.
        graphstore.record_dead_ref(self.conn, issue)
        m2 = graphstore.traverse_spine(self.conn, [pr], skip_dead=True)["missing"]
        self.assertNotIn(issue, m2)
        # Without skip_dead it is still reported (default unchanged).
        self.assertIn(issue, graphstore.traverse_spine(self.conn, [pr])["missing"])
