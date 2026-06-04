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


if __name__ == "__main__":
    unittest.main()
