"""SQLite property-graph store for the activity-overview journey substrate.

Stdlib only (sqlite3). Holds the accumulated, identity-keyed graph that
gather writes and extract/spotlight read. All SQL lives here; callers use
the function API below. See STORE.md for the schema and identity rules.
"""

import json
import sqlite3
import time

SCHEMA_VERSION = 1

NODE_CLASSES = ("social", "code", "structure")

# Spine edge types: the allowlist a decision-train traversal may follow.
SPINE_EDGE_TYPES = ("closes", "part_of", "cross_ref", "spun_off", "duplicate_of")

_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    node_class  TEXT NOT NULL,
    ts          TEXT,
    data        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_window
    ON nodes (project, repo, node_class, ts);
CREATE INDEX IF NOT EXISTS idx_nodes_ts ON nodes (ts);

CREATE TABLE IF NOT EXISTS edges (
    src_id     TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    edge_type  TEXT NOT NULL,
    ts         TEXT,
    data       TEXT,
    PRIMARY KEY (src_id, dst_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_id, edge_type);

CREATE TABLE IF NOT EXISTS code_events (
    artifact_id TEXT NOT NULL,
    event       TEXT NOT NULL,
    commit_sha  TEXT NOT NULL,
    author      TEXT,
    date        TEXT,
    hunk        TEXT,
    ref         TEXT,
    before      TEXT,
    after       TEXT,
    detail      TEXT,
    PRIMARY KEY (artifact_id, commit_sha, event)
);
CREATE INDEX IF NOT EXISTS idx_code_events_artifact
    ON code_events (artifact_id, date);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso():
    """UTC timestamp, second precision, ISO-8601 with trailing Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def open_store(path=":memory:"):
    """Open (creating if needed) a store. Rows come back as sqlite3.Row."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def set_meta(conn, key, value):
    """Upsert a key/value pair in the meta table (value is stored as str)."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn, key, default=None):
    """Return the meta value for key, or default if absent."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def fts5_available(conn):
    """True if this SQLite build supports FTS5."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def qualify_id(project, repo, local):
    """Repo-scoped node id: '{project}/{repo}#{local}'."""
    return "{}/{}#{}".format(project, repo, local)


def qualify_person(project, login):
    """Project-scoped person id: '{project}#person-{login}'.

    People aggregate across all repos in a project, so they are not
    repo-qualified (design decision 2).
    """
    return "{}#person-{}".format(project, login)


def parse_id(qid):
    """Split a qualified id into {scope, local} on the last '#'."""
    scope, _, local = qid.rpartition("#")
    return {"scope": scope, "local": local}


def _row_to_node(row):
    return {
        "id": row["id"],
        "project": row["project"],
        "repo": row["repo"],
        "node_class": row["node_class"],
        "ts": row["ts"],
        "data": json.loads(row["data"]),
        "fetched_at": row["fetched_at"],
    }


def upsert_node(conn, id, project, repo, node_class, ts, data, fetched_at=None):
    """Insert or update a node by id. Identity columns (project/repo/
    node_class) are immutable; ts/data/fetched_at refresh on conflict."""
    if node_class not in NODE_CLASSES:
        raise ValueError("unknown node_class: {}".format(node_class))
    conn.execute(
        "INSERT INTO nodes (id, project, repo, node_class, ts, data, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "ts=excluded.ts, data=excluded.data, fetched_at=excluded.fetched_at",
        (
            id, project, repo, node_class, ts,
            json.dumps(data, sort_keys=True),
            now_iso() if fetched_at is None else fetched_at,
        ),
    )
    conn.commit()


def get_node(conn, id):
    """Return the node dict for id, or None if absent."""
    row = conn.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
    return _row_to_node(row) if row else None


def init_schema(conn):
    """Create all tables. FTS5 table is created when the build supports it."""
    conn.executescript(_CORE_SCHEMA)
    if fts5_available(conn):
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_text "
            "USING fts5(node_id UNINDEXED, text)"
        )
    conn.commit()
    if get_meta(conn, "schema_version") is None:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
