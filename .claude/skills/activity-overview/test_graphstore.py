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


if __name__ == "__main__":
    unittest.main()
