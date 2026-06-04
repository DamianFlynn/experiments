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


if __name__ == "__main__":
    unittest.main()
