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

# FTS5 availability is a property of the compiled SQLite library — identical for
# every connection in the process — so it is probed once and cached here.
_FTS5_CACHE = None

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

CREATE TABLE IF NOT EXISTS dead_refs (
    id         TEXT PRIMARY KEY,
    project    TEXT,
    reason     TEXT,
    first_seen TEXT
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
    """True if this SQLite build supports FTS5. Probed once, then cached
    (availability is a build property, so it can't change between calls)."""
    global _FTS5_CACHE
    if _FTS5_CACHE is not None:
        return _FTS5_CACHE
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        _FTS5_CACHE = True
    except sqlite3.OperationalError:
        _FTS5_CACHE = False
    return _FTS5_CACHE


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


def upsert_edge(conn, src_id, dst_id, edge_type, ts=None, data=None):
    """Insert or update an edge keyed by (src, dst, type). Re-upsert unions
    (refreshes ts/data); never appends a duplicate."""
    conn.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, ts, data) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(src_id, dst_id, edge_type) DO UPDATE SET "
        "ts=excluded.ts, data=excluded.data",
        (src_id, dst_id, edge_type, ts,
         json.dumps(data, sort_keys=True) if data is not None else None),
    )
    conn.commit()


def _row_to_edge(row):
    return {
        "src_id": row["src_id"],
        "dst_id": row["dst_id"],
        "edge_type": row["edge_type"],
        "ts": row["ts"],
        "data": json.loads(row["data"]) if row["data"] is not None else None,
    }


def get_edges(conn, node_id, direction="both", edge_types=None):
    """Edges touching node_id. direction: 'out' (src=node), 'in' (dst=node),
    or 'both'. Optional edge_types allowlist."""
    if direction not in ("out", "in", "both"):
        raise ValueError("unknown direction: {}".format(direction))
    clauses = []
    params = []
    if direction == "out":
        clauses.append("src_id=?")
        params.append(node_id)
    elif direction == "in":
        clauses.append("dst_id=?")
        params.append(node_id)
    else:
        clauses.append("(src_id=? OR dst_id=?)")
        params.extend([node_id, node_id])
    if edge_types:
        ph = ",".join("?" for _ in edge_types)
        clauses.append("edge_type IN ({})".format(ph))
        params.extend(edge_types)
    sql = "SELECT * FROM edges WHERE {} ORDER BY edge_type, dst_id, src_id".format(
        " AND ".join(clauses)
    )
    return [_row_to_edge(r) for r in conn.execute(sql, params)]


def add_code_event(conn, artifact_id, event, commit_sha, author=None, date=None,
                   hunk=None, ref=None, before=None, after=None, detail=None):
    """Append one artifact lifecycle event. Keyed by (artifact, commit, event):
    re-seeing the same event is a no-op (set semantics), so re-folding a window
    never duplicates a lifecycle entry."""
    conn.execute(
        "INSERT OR IGNORE INTO code_events "
        "(artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id, event, commit_sha, author, date, hunk,
            json.dumps(ref, sort_keys=True) if ref is not None else None,
            before, after, detail,
        ),
    )
    conn.commit()


def upsert_nodes(conn, rows):
    """Batch upsert nodes in one transaction. rows: iterable of
    (id, project, repo, node_class, ts, data, fetched_at) where `data` is a
    Python object (JSON-serialized here) and `fetched_at` None -> now. Same
    immutable-identity / ts-data-fetched_at-refresh semantics as upsert_node."""
    payload = []
    for (id, project, repo, node_class, ts, data, fetched_at) in rows:
        if node_class not in NODE_CLASSES:
            raise ValueError("unknown node_class: {}".format(node_class))
        payload.append((
            id, project, repo, node_class, ts,
            json.dumps(data, sort_keys=True),
            now_iso() if fetched_at is None else fetched_at,
        ))
    if payload:
        conn.executemany(
            "INSERT INTO nodes (id, project, repo, node_class, ts, data, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "ts=excluded.ts, data=excluded.data, fetched_at=excluded.fetched_at",
            payload,
        )
        conn.commit()


def upsert_edges(conn, rows):
    """Batch upsert edges in one transaction. rows: iterable of
    (src_id, dst_id, edge_type, ts, data). Re-upsert unions (refreshes ts/data),
    never appends — same semantics as upsert_edge."""
    payload = [
        (src, dst, etype, ts,
         json.dumps(data, sort_keys=True) if data is not None else None)
        for (src, dst, etype, ts, data) in rows
    ]
    if payload:
        conn.executemany(
            "INSERT INTO edges (src_id, dst_id, edge_type, ts, data) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(src_id, dst_id, edge_type) DO UPDATE SET "
            "ts=excluded.ts, data=excluded.data",
            payload,
        )
        conn.commit()


def add_code_events(conn, rows):
    """Batch-append code events (set semantics, INSERT OR IGNORE). rows match
    add_code_event's positional args: (artifact_id, event, commit_sha, author,
    date, hunk, ref, before, after, detail). `ref` is JSON-serialized."""
    payload = []
    for (artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) in rows:
        payload.append((
            artifact_id, event, commit_sha, author, date, hunk,
            json.dumps(ref, sort_keys=True) if ref is not None else None,
            before, after, detail,
        ))
    if payload:
        conn.executemany(
            "INSERT OR IGNORE INTO code_events "
            "(artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()


def get_code_events(conn, artifact_id):
    """Lifecycle events for an artifact, ordered by date then event."""
    rows = conn.execute(
        "SELECT * FROM code_events WHERE artifact_id=? ORDER BY date, event",
        (artifact_id,),
    )
    out = []
    for r in rows:
        out.append({
            "artifact_id": r["artifact_id"],
            "event": r["event"],
            "commit_sha": r["commit_sha"],
            "author": r["author"],
            "date": r["date"],
            "hunk": r["hunk"],
            "ref": json.loads(r["ref"]) if r["ref"] is not None else None,
            "before": r["before"],
            "after": r["after"],
            "detail": r["detail"],
        })
    return out


def range_query(conn, project, repos, ts_from, ts_to, node_class=None):
    """In-window nodes: project match, repo in `repos`, ts in [ts_from, ts_to].
    Nodes with NULL ts (structure) are excluded — they are not activity.
    Ordered by (ts, id) for deterministic output."""
    if not repos:
        return []
    repo_ph = ",".join("?" for _ in repos)
    sql = (
        "SELECT * FROM nodes "
        "WHERE project=? AND repo IN ({}) "
        "AND ts IS NOT NULL AND ts BETWEEN ? AND ?".format(repo_ph)
    )
    params = [project] + list(repos) + [ts_from, ts_to]
    if node_class is not None:
        sql += " AND node_class=?"
        params.append(node_class)
    sql += " ORDER BY ts, id"
    return [_row_to_node(r) for r in conn.execute(sql, params)]


def repo_nodes(conn, project, repo, node_class=None):
    """All nodes for one project/repo (optionally one node_class), ordered by
    (ts NULLs last, id). Unlike range_query this is *not* window-filtered: it is
    the materialization source a reader uses to reconstruct the full raw bundle
    arrays (which include NULL-ts nodes the window scan excludes). The window
    still governs the in_window flag a reader computes via range_query."""
    sql = "SELECT * FROM nodes WHERE project=? AND repo=?"
    params = [project, repo]
    if node_class is not None:
        sql += " AND node_class=?"
        params.append(node_class)
    sql += " ORDER BY ts IS NULL, ts, id"
    return [_row_to_node(r) for r in conn.execute(sql, params)]


def repo_code_events(conn, project, repo):
    """Code-event ledger rows for one project/repo, in insertion (rowid) order.

    rowid order == the order fold_bundle inserted them == the original bundle's
    `code_events` source order, so a reader can reconstruct that array exactly
    (the per-artifact get_code_events orders by date/event and cannot recover
    cross-artifact source order)."""
    pref = "{}/{}#".format(project, repo)
    rows = conn.execute(
        "SELECT * FROM code_events WHERE artifact_id LIKE ? ESCAPE '\\' ORDER BY rowid",
        (pref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
    )
    out = []
    for r in rows:
        out.append({
            "artifact_id": r["artifact_id"],
            "event": r["event"],
            "commit_sha": r["commit_sha"],
            "author": r["author"],
            "date": r["date"],
            "hunk": r["hunk"],
            "ref": json.loads(r["ref"]) if r["ref"] is not None else None,
            "before": r["before"],
            "after": r["after"],
            "detail": r["detail"],
        })
    return out


def traverse_spine(conn, seed_ids, max_depth=6, edge_types=SPINE_EDGE_TYPES,
                   skip_dead=False):
    """Undirected reachability over the spine edge allowlist, depth-capped.

    Walks edges in both directions (a train links issue<->pr<->commit
    regardless of edge direction), following only `edge_types`, stopping at
    `max_depth` hops. Returns {"reached": {id: min_depth}, "missing": [ids
    reached but absent from nodes]} — `missing` is what a reader backfills.
    """
    seed_ids = list(dict.fromkeys(seed_ids))  # dedup, preserve order
    if not seed_ids:
        return {"reached": {}, "missing": []}

    seed_values = ",".join("(?)" for _ in seed_ids)
    etype_ph = ",".join("?" for _ in edge_types)
    sql = (
        "WITH RECURSIVE seeds(id) AS (VALUES {seeds}), "
        "reach(id, depth) AS ( "
        "  SELECT id, 0 FROM seeds "
        "  UNION "
        "  SELECT CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END, "
        "         r.depth + 1 "
        "  FROM reach r "
        "  JOIN edges e ON (e.src_id = r.id OR e.dst_id = r.id) "
        "  WHERE e.edge_type IN ({etypes}) AND r.depth < ? "
        ") "
        "SELECT id, MIN(depth) AS depth FROM reach GROUP BY id"
    ).format(seeds=seed_values, etypes=etype_ph)
    params = list(seed_ids) + list(edge_types) + [max_depth]

    reached = {row["id"]: row["depth"] for row in conn.execute(sql, params)}

    present = set()
    ids = list(reached)
    if ids:
        ph = ",".join("?" for _ in ids)
        present = {
            r["id"] for r in conn.execute(
                "SELECT id FROM nodes WHERE id IN ({})".format(ph), ids
            )
        }
    missing = [i for i in reached if i not in present]
    if skip_dead and missing:
        missing = [i for i in missing if not is_dead_ref(conn, i)]
    return {"reached": reached, "missing": missing}


def index_text(conn, node_id, text, commit=True):
    """Index a node's searchable text. Delete-then-insert keeps it idempotent
    (FTS5 has no UPSERT). Raises if the SQLite build lacks FTS5.

    `commit=False` lets a caller batch many inserts into a single transaction
    (e.g. folding a whole window) and commit once at the end."""
    if not fts5_available(conn):
        raise RuntimeError("FTS5 not available in this SQLite build")
    conn.execute("DELETE FROM fts_text WHERE node_id=?", (node_id,))
    conn.execute(
        "INSERT INTO fts_text (node_id, text) VALUES (?, ?)", (node_id, text)
    )
    if commit:
        conn.commit()


def fts_search(conn, query):
    """Node ids whose indexed text matches the FTS5 query, ranked by relevance.

    Orders by FTS5's built-in `rank` (bm25 score), with `node_id` as a
    deterministic tie-breaker so equal-scoring matches order stably."""
    if not fts5_available(conn):
        raise RuntimeError("FTS5 not available in this SQLite build")
    rows = conn.execute(
        "SELECT node_id FROM fts_text WHERE fts_text MATCH ? ORDER BY rank, node_id",
        (query,),
    )
    return [r[0] for r in rows]


def record_window(conn, project, repo, frm, to):
    """Append a gathered window to the meta ledger, deduped on the exact tuple."""
    raw = get_meta(conn, "gathered_windows")
    windows = json.loads(raw) if raw else []
    entry = {"project": project, "repo": repo, "from": frm, "to": to}
    if entry not in windows:
        windows.append(entry)
        set_meta(conn, "gathered_windows", json.dumps(windows, sort_keys=True))


def get_windows(conn):
    """Return the list of gathered windows recorded in the meta ledger."""
    raw = get_meta(conn, "gathered_windows")
    return json.loads(raw) if raw else []


def _clone_sha_key(project, repo):
    return "clone_sha:{}/{}".format(project, repo)


def set_clone_sha(conn, project, repo, sha):
    """Record the tree SHA a repo was last gathered against (per project/repo)."""
    set_meta(conn, _clone_sha_key(project, repo), sha)


def get_clone_sha(conn, project, repo):
    """Return the recorded clone SHA for project/repo, or None if absent."""
    return get_meta(conn, _clone_sha_key(project, repo))


_DEAD_REFS_DDL = (
    "CREATE TABLE IF NOT EXISTS dead_refs ("
    "id TEXT PRIMARY KEY, project TEXT, reason TEXT, first_seen TEXT)"
)


def _ensure_dead_refs(conn):
    """Create the dead_refs table if absent. Lets the tombstone helpers work on
    a store opened directly (not via init_schema) or one that predates 8d."""
    conn.execute(_DEAD_REFS_DDL)


def record_dead_ref(conn, id, reason="absent"):
    """Tombstone a qualified id known not to exist (a 404 phantom). Idempotent:
    re-recording the same id is a no-op (first_seen is preserved). We never
    destructively delete the dangling edge — this just stops traversal from
    re-surfacing it. `project` is recovered from the id's scope when present."""
    _ensure_dead_refs(conn)
    parsed = parse_id(id)
    project = parsed["scope"].split("/", 1)[0] if parsed["scope"] else None
    conn.execute(
        "INSERT INTO dead_refs (id, project, reason, first_seen) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (id, project, reason, now_iso()),
    )
    conn.commit()


def is_dead_ref(conn, id):
    """True if `id` has been tombstoned as a known-absent phantom."""
    _ensure_dead_refs(conn)
    row = conn.execute("SELECT 1 FROM dead_refs WHERE id=?", (id,)).fetchone()
    return row is not None


def get_dead_refs(conn):
    """All tombstoned ids, sorted (deterministic)."""
    _ensure_dead_refs(conn)
    return [r[0] for r in conn.execute("SELECT id FROM dead_refs ORDER BY id")]


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
