"""SQLite property-graph store for the activity-overview journey substrate.

Stdlib only (sqlite3). Holds the accumulated, identity-keyed graph that
gather writes and extract/spotlight read. All SQL lives here; callers use
the function API below. See STORE.md for the schema and identity rules.
"""

import datetime
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse

# Store schema version (meta `schema_version`). History:
#   1 - initial schema.
#   2 - issue #55: `fts_map` (node_id -> fts_text rowid) so re-indexing deletes
#       by rowid instead of scanning fts_text; `idx_edges_type_src` so
#       `edges_by_type` is an index range scan. `init_schema` migrates a v1 store
#       in place (additive: new index + new table back-filled from fts_text).
SCHEMA_VERSION = 2

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
CREATE INDEX IF NOT EXISTS idx_edges_type_src
    ON edges (edge_type, src_id, dst_id);

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


def open_store_readonly(path):
    """Open an EXISTING store strictly read-only (issue #79), for auditors.

    Unlike open_store this never creates the file (a missing path raises
    FileNotFoundError), sets no journal mode and runs no schema step; the
    connection is opened with SQLite's `mode=ro` URI, so any write through it
    raises sqlite3.OperationalError instead of touching the store."""
    if not os.path.isfile(path):
        raise FileNotFoundError("store not found: {}".format(path))
    uri = "file:{}?mode=ro".format(
        urllib.parse.quote(os.path.abspath(path), safe="/"))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def set_meta(conn, key, value, commit=True):
    """Upsert a key/value pair in the meta table (value is stored as str).
    `commit=False` leaves the write in the caller's open transaction."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    if commit:
        conn.commit()


def get_meta(conn, key, default=None):
    """Return the meta value for key, or default if absent."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def fts5_available(conn):
    """True if this SQLite build supports FTS5. Probed once, then cached
    (availability is a build property, so it can't change between calls).

    The probe runs on a scratch in-memory connection, never on `conn`: on a
    read-only reader connection (open_store_readonly) the probe's CREATE
    would fail and FTS5 would wrongly read as absent (issue #107)."""
    global _FTS5_CACHE
    if _FTS5_CACHE is not None:
        return _FTS5_CACHE
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        _FTS5_CACHE = True
    except sqlite3.OperationalError:
        _FTS5_CACHE = False
    finally:
        probe.close()
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
    """Split a qualified id into {scope, local} on the FIRST '#'.

    The scope (`{project}/{repo}` or `{project}`) never contains '#' (GitHub
    owner/repo names and logins cannot), while a local id may: a symbol/comment
    artifact is `<path>#<lang>:<subkind>:<name>`, and the path or the name can
    carry further '#'. So everything after the first '#' is the local id."""
    scope, _, local = qid.partition("#")
    return {"scope": scope, "local": local}


def local_id(qid, project=None, repo=None):
    """The FULL local id of a qualified id (every internal '#' preserved).

    When `project`/`repo` are known and `qid` starts with `{project}/{repo}#`,
    exactly that prefix is stripped; otherwise this is `parse_id(qid)["local"]`
    (split on the first '#'). The one helper every reader uses to recover a
    local id — never rpartition on '#'."""
    if project is not None and repo is not None:
        prefix = qualify_id(project, repo, "")
        if qid.startswith(prefix):
            return qid[len(prefix):]
    return parse_id(qid)["local"]


# A symbol/comment artifact local id ends in `#<lang>:<subkind>:<name>`. `lang`
# is an identifier-like token (possibly empty: fold writes `lang or ""`),
# `subkind` a non-empty one; neither contains '#', ':', '/', '.' or whitespace.
# `name` is free text (may contain '#', ':' and spaces, or be empty).
_SYMBOL_TAIL_RE = re.compile(r"([A-Za-z0-9_+-]*):([A-Za-z0-9_-]+):(.*)\Z", re.S)


def split_symbol_local(local):
    """Split a symbol/comment artifact LOCAL id into (path, lang, subkind, name),
    or return None when `local` is not one (a bare file path — even one that
    contains '#' — an `art:<path>` file artifact, a sha, `pr-N`, ...).

    Rule: the separator is the FIRST '#' that is preceded by a non-empty path
    and followed by the `<lang>:<subkind>:` tail shape (see _SYMBOL_TAIL_RE).
    A '#' inside a path is followed by more path text (`C#-guide.md`,
    `C#/main.tf#...`), which never has that shape, so it is skipped; '#'s inside
    the name come after the separator and stay in the name. Ids starting with
    `art:` are file artifacts and never symbols."""
    if not local or local.startswith("art:"):
        return None
    start = 0
    while True:
        i = local.find("#", start)
        if i < 0:
            return None
        if i > 0:
            m = _SYMBOL_TAIL_RE.match(local, i + 1)
            if m:
                return (local[:i], m.group(1), m.group(2), m.group(3))
        start = i + 1


def is_symbol_local(local):
    """True when `local` is a symbol/comment artifact local id."""
    return split_symbol_local(local) is not None


def is_artifact_local(local):
    """True for any artifact local id: `art:<path>` (file) or a symbol id."""
    return bool(local) and (local.startswith("art:") or is_symbol_local(local))


# Classifying a STORED node or ledger row (#101). The id-shape helpers above are
# the fallback for a dangling id only: a stored node is classified by its class
# and record, so a structure node whose title holds `#<token>:<token>:` (e.g.
# `milestone-v1#a:b:c`) is never an artifact.
SYMBOL_ARTIFACT_KINDS = ("symbol", "comment")


def is_artifact_node(node):
    """True for a stored artifact node: a `code` node whose record is not a
    commit's. Every commit record carries `sha` (gather writes it); an artifact
    record (derive.build_artifacts) never does."""
    data = node.get("data")
    return node.get("node_class") == "code" and not (
        isinstance(data, dict) and "sha" in data)


def is_symbol_node(node):
    """True for a stored symbol/comment artifact node (record `kind` symbol or
    comment); a file artifact has a path kind (readme/doc/example)."""
    return is_artifact_node(node) and isinstance(node.get("data"), dict) \
        and node["data"].get("kind") in SYMBOL_ARTIFACT_KINDS


def ledger_row_is_symbol(local, commit_files=None):
    """True when a `code_events` ledger row keyed by `local` is a SYMBOL row.

    File rows are keyed by the bare `<path>` and have no node of their own, so
    the stored node that decides is the row's commit: when `commit_files` (that
    commit's stored file list, repo_commit_files) names `local` itself, the row
    is that file's row whatever its shape (`notes#a:b:c.md`). Otherwise (the
    commit is not stored or has no file list) the id shape decides
    (is_symbol_local)."""
    if commit_files and local in commit_files:
        return False
    return is_symbol_local(local)


def repo_commit_files(conn, project, repo):
    """{sha: frozenset(files)} for the repo's stored commit nodes that carry a
    non-empty file list (input to ledger_row_is_symbol). Only `code` rows whose
    local id has a sha's length are decoded; a row counts when its `data.sha`
    equals that local id."""
    prefix = qualify_id(project, repo, "")
    lo, hi = _prefix_range(prefix)
    out = {}
    for nid, raw in conn.execute(
            "SELECT id, data FROM nodes WHERE id >= ? AND id < ? "
            "AND node_class='code' AND length(id) IN (?, ?)",
            (lo, hi, len(prefix) + 40, len(prefix) + 64)):
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            continue
        sha = nid[len(prefix):]
        if isinstance(data, dict) and data.get("sha") == sha and data.get("files"):
            out[sha] = frozenset(f for f in data["files"] if isinstance(f, str))
    return out


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


def _prefix_range(prefix):
    """(lo, hi) such that `col >= lo AND col < hi` selects exactly the TEXT
    values starting with `prefix`, compared case-sensitively (BINARY collation).

    Replaces `LIKE 'prefix%'`, which is case-insensitive by default (so
    `Azure/` also matched `azure/`) and therefore cannot use an index. `hi` is
    `prefix` with its last character incremented; BINARY compares UTF-8 bytes,
    whose order equals code-point order, so every string with the prefix sorts
    in [lo, hi) and nothing else does. Callers always pass a prefix ending in a
    fixed ASCII separator ('/' or '#'), so the increment cannot overflow."""
    if not prefix:
        raise ValueError("empty prefix")
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _edges_by_type_sql(edge_type, project):
    lo, hi = _prefix_range(project + "/")
    return ("SELECT * FROM edges WHERE edge_type=? AND src_id >= ? AND src_id < ? "
            "ORDER BY src_id, dst_id", (edge_type, lo, hi))


def edges_by_type(conn, edge_type, project):
    """All edges of `edge_type` whose src node belongs to `project`, sorted by
    (src_id, dst_id). src_id is the qualified '{project}/{repo}#local', so the
    filter is the literal, case-sensitive prefix '{project}/' expressed as an
    index range (`idx_edges_type_src`); see _prefix_range."""
    sql, params = _edges_by_type_sql(edge_type, project)
    return [_row_to_edge(r) for r in conn.execute(sql, params)]


def add_code_event(conn, artifact_id, event, commit_sha, author=None, date=None,
                   hunk=None, ref=None, before=None, after=None, detail=None):
    """Record one artifact lifecycle event. Keyed by (artifact, commit, event):
    re-seeing the same event never duplicates a lifecycle entry; its other
    columns take the new values (issue #111, see add_code_events)."""
    conn.execute(
        _CODE_EVENT_UPSERT,
        (
            artifact_id, event, commit_sha, author, date, hunk,
            json.dumps(ref, sort_keys=True) if ref is not None else None,
            before, after, detail,
        ),
    )
    conn.commit()


def upsert_nodes(conn, rows, commit=True):
    """Batch upsert nodes in one transaction. rows: iterable of
    (id, project, repo, node_class, ts, data, fetched_at) where `data` is a
    Python object (JSON-serialized here) and `fetched_at` None -> now. Same
    immutable-identity / ts-data-fetched_at-refresh semantics as upsert_node.
    `commit=False` leaves the writes in the caller's open transaction (a fold
    commits everything once, at the end)."""
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
        if commit:
            conn.commit()


def upsert_edges(conn, rows, commit=True):
    """Batch upsert edges in one transaction. rows: iterable of
    (src_id, dst_id, edge_type, ts, data). Re-upsert unions (refreshes ts/data),
    never appends — same semantics as upsert_edge. `commit=False`: see
    upsert_nodes."""
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
        if commit:
            conn.commit()


# Keyed by (artifact, commit, event); the other columns are what the latest
# fold of that commit says (issue #111): a commit whose author resolution
# changed (#78) re-credits its rows, like the commit record it belongs to.
_CODE_EVENT_UPSERT = (
    "INSERT INTO code_events "
    "(artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(artifact_id, commit_sha, event) DO UPDATE SET "
    "author=excluded.author, date=excluded.date, hunk=excluded.hunk, "
    "ref=excluded.ref, before=excluded.before, after=excluded.after, "
    "detail=excluded.detail")


def add_code_events(conn, rows, commit=True):
    """Batch-upsert code events. rows match add_code_event's positional args:
    (artifact_id, event, commit_sha, author, date, hunk, ref, before, after,
    detail). `ref` is JSON-serialized. Keyed by (artifact, commit, event): a
    key already stored takes this batch's other columns (latest fold wins,
    issue #111); within one batch the FIRST row of a key wins, as it did
    with INSERT OR IGNORE, so a fold into an empty store writes what it
    always wrote. `commit=False`: see upsert_nodes."""
    payload, seen = [], set()
    for (artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) in rows:
        key = (artifact_id, commit_sha, event)
        if key in seen:
            continue
        seen.add(key)
        payload.append((
            artifact_id, event, commit_sha, author, date, hunk,
            json.dumps(ref, sort_keys=True) if ref is not None else None,
            before, after, detail,
        ))
    if payload:
        conn.executemany(_CODE_EVENT_UPSERT, payload)
        if commit:
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


def _is_date_only(value):
    """A `ts`/bound with no time part (`YYYY-MM-DD`, no `T`) covers its whole day."""
    return "T" not in value


def ts_in_window(ts, ts_from, ts_to):
    """THE window predicate (issue #52): is `ts` within [ts_from, ts_to]?

    `ts` and the bounds may each be date-only (`YYYY-MM-DD`) or an ISO datetime
    (`YYYY-MM-DDTHH:MM:SSZ`). Each bound is compared at the COARSER granularity
    of the pair: when either side is date-only the comparison is on the date
    prefix (`[:10]`), so a date-only value counts as its whole day; only when
    both are datetimes is the full ISO string compared. Consequences:

    - date-only bounds keep ISO-ts nodes on the first and last day
      (`2026-05-31T15:00:00Z` is inside `--to 2026-05-31`);
    - ISO bounds keep date-only-ts nodes (commits) on the first and last day
      (`2026-03-01` is inside `from=2026-03-01T00:00:00Z`).

    Two datetimes are compared at SECOND granularity, on their first 19
    characters (`YYYY-MM-DDTHH:MM:SS`), so a stored ts with fractional seconds
    (`...:00.5Z`; GitHub sends none) is not misordered by the `.` sorting
    before `Z`: like a date-only value, it counts as its whole second. Both
    sides are taken to be UTC: stored GitHub timestamps end in `Z`, and
    readers pass bounds through manifest.check_reader_window, which converts
    an offset to UTC `...Z` (issue #111).

    Inclusive at both ends; either bound may be None (one-sided / unbounded).
    An empty/None `ts` cannot be proven in-window -> False. `range_query`'s SQL
    implements exactly this rule; every other window check delegates here."""
    if not ts:
        return False
    if ts_from:
        if _is_date_only(ts_from) or _is_date_only(ts):
            if ts[:10] < ts_from[:10]:
                return False
        elif ts[:19] < ts_from[:19]:
            return False
    if ts_to:
        if _is_date_only(ts_to) or _is_date_only(ts):
            if ts[:10] > ts_to[:10]:
                return False
        elif ts[:19] > ts_to[:19]:
            return False
    return True


def _next_day(day):
    """`YYYY-MM-DD` + 1 day, or None if `day` is not a parseable date."""
    try:
        d = datetime.date.fromisoformat(day)
    except (TypeError, ValueError):
        return None
    try:
        return (d + datetime.timedelta(days=1)).isoformat()
    except OverflowError:
        return None


def _window_sql(ts_from, ts_to):
    """(sql_fragment, params) implementing `ts_in_window` over `nodes.ts`.

    A plain `ts >= day` / `ts < next_day` range prefilter keeps the window scan
    on the `ts` index; the extra terms add the granularity rule (a date-only ts
    — no `T` — is compared on its day against an ISO bound)."""
    clauses, params = [], []
    if ts_from:
        day = ts_from[:10]
        # ts >= 'YYYY-MM-DD' is exactly "date prefix >= day" for any ts.
        clauses.append("ts >= ?")
        params.append(day)
        if not _is_date_only(ts_from):
            # ISO bound: an ISO ts compares to the second; a date-only ts
            # on/after the bound's day already passed the prefix test above.
            clauses.append("(substr(ts, 1, 19) >= ? OR instr(ts, 'T') = 0)")
            params.append(ts_from[:19])
    if ts_to:
        day = ts_to[:10]
        nxt = _next_day(day)
        if nxt is not None:
            clauses.append("ts < ?")             # index-friendly prefilter
            params.append(nxt)
        clauses.append("substr(ts, 1, 10) <= ?")
        params.append(day)
        if not _is_date_only(ts_to):
            clauses.append("(substr(ts, 1, 19) <= ? OR instr(ts, 'T') = 0)")
            params.append(ts_to[:19])
    return " AND ".join(clauses), params


def _range_query_sql(project, repos, ts_from, ts_to, node_class):
    repo_ph = ",".join("?" for _ in repos)
    sql = (
        "SELECT * FROM nodes "
        "WHERE project=? AND repo IN ({}) "
        "AND ts IS NOT NULL AND ts != ''".format(repo_ph)
    )
    params = [project] + list(repos)
    wsql, wparams = _window_sql(ts_from, ts_to)
    if wsql:
        sql += " AND " + wsql
        params += wparams
    if node_class is not None:
        sql += " AND node_class=?"
        params.append(node_class)
    sql += " ORDER BY ts, id"
    return sql, params


def range_query(conn, project, repos, ts_from, ts_to, node_class=None):
    """In-window nodes: project match, repo in `repos`, ts in [ts_from, ts_to]
    under the granularity-safe rule of `ts_in_window` (bounds and ts may each
    be date-only or ISO; a date-only value covers its whole day; inclusive).
    Either bound may be None. Nodes with NULL ts (structure) are excluded —
    they are not activity. Ordered by (ts, id) for deterministic output."""
    if not repos:
        return []
    sql, params = _range_query_sql(project, repos, ts_from, ts_to, node_class)
    return [_row_to_node(r) for r in conn.execute(sql, params)]


# `data.synthesized` of the minimal area node a fold creates for a depends_on
# target it has not stored (gather.fold_bundle). It is an edge anchor, not data.
STANDIN_MARK = "depends_on_target"


def _is_standin(data):
    """True for a stored stand-in's `data` JSON text."""
    if "synthesized" not in data:
        return False
    try:
        return json.loads(data).get("synthesized") == STANDIN_MARK
    except (ValueError, AttributeError):
        return False


def _only_standins(conn, project, repo):
    """True when every node of (project, repo) is a depends_on stand-in."""
    if conn.execute("SELECT 1 FROM nodes WHERE project=? AND repo=? "
                    "AND node_class != 'structure' LIMIT 1",
                    (project, repo)).fetchone():
        return False
    return all(_is_standin(r[0]) for r in conn.execute(
        "SELECT data FROM nodes WHERE project=? AND repo=?", (project, repo)))


_UNKNOWN_WINDOWS = object()


def project_repos(conn, project):
    """The project's distinct MEMBER repos (`owner/repo`), sorted. Excludes the
    `"*"` person sentinel so people (project-scoped) never count as a repo. This
    is store-only member discovery: a reader needs just the store + project name.

    A repo whose ONLY nodes are depends_on stand-ins (`data.synthesized ==
    STANDIN_MARK`) is listed only when a window was gathered for it (a quiet
    member). Otherwise it is a phantom that another member's fold created, e.g.
    for a member whose own gather failed (issue #77), and is left out."""
    repos = [r[0] for r in conn.execute(
        "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
        (project,))]
    gathered = None
    out = []
    for repo in repos:
        if _only_standins(conn, project, repo):
            if gathered is None:
                try:
                    gathered = {w.get("repo") for w in get_windows(conn)
                                if isinstance(w, dict)
                                and w.get("project") == project}
                except ValueError:
                    # Unreadable window ledger: cannot tell a quiet member from a
                    # phantom. Keep the repo listed so an audit examines (and
                    # fails) it, rather than raising during discovery.
                    gathered = _UNKNOWN_WINDOWS
            if gathered is not _UNKNOWN_WINDOWS and repo not in gathered:
                continue
        out.append(repo)
    return sorted(out)


def resolve_repo(conn, project, repo):
    """The stored spelling of `repo` in `project` (issue #70). GitHub names are
    case-insensitive, so a reader may name a member in any case; ids keep the
    spelling it was folded under. An exact match wins; otherwise the single
    stored repo equal to `repo` ignoring case. With no match (or, defensively,
    several case-variants and no exact one) `repo` is returned unchanged so the
    caller reports it as unknown. The project itself stays case-sensitive."""
    if repo is None:
        return None
    repos = project_repos(conn, project)
    if repo in repos:
        return repo
    hits = [r for r in repos if r.lower() == repo.lower()]
    return hits[0] if len(hits) == 1 else repo


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


def _repo_code_events_sql(project, repo):
    lo, hi = _prefix_range(qualify_id(project, repo, ""))
    return ("SELECT * FROM code_events WHERE artifact_id >= ? AND artifact_id < ? "
            "ORDER BY rowid", (lo, hi))


def repo_code_events(conn, project, repo):
    """Code-event ledger rows for one project/repo, in insertion (rowid) order.

    rowid order == the order fold_bundle inserted them == the original bundle's
    `code_events` source order, so a reader can reconstruct that array exactly
    (the per-artifact get_code_events orders by date/event and cannot recover
    cross-artifact source order). The `{project}/{repo}#` filter is a
    case-sensitive index range on artifact_id (see _prefix_range)."""
    sql, params = _repo_code_events_sql(project, repo)
    rows = conn.execute(sql, params)
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

    # When skip_dead, exclude tombstoned phantom ids from the recursive
    # expansion itself — not just the returned `missing`. Filtering only the
    # final `missing` list would still let a repeated dead ref (e.g. two PRs
    # both `Fixes #123`) BRIDGE otherwise-unrelated trains, because the CTE
    # reaches the dead id from both sides before the filter runs.
    next_id = "CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END"
    dead_clause = ""
    if skip_dead and _has_dead_refs(conn):
        dead_clause = " AND ({}) NOT IN (SELECT id FROM dead_refs)".format(next_id)

    seed_values = ",".join("(?)" for _ in seed_ids)
    etype_ph = ",".join("?" for _ in edge_types)
    sql = (
        "WITH RECURSIVE seeds(id) AS (VALUES {seeds}), "
        "reach(id, depth) AS ( "
        "  SELECT id, 0 FROM seeds "
        "  UNION "
        "  SELECT {next}, "
        "         r.depth + 1 "
        "  FROM reach r "
        "  JOIN edges e ON (e.src_id = r.id OR e.dst_id = r.id) "
        "  WHERE e.edge_type IN ({etypes}) AND r.depth < ?{dead} "
        ") "
        "SELECT id, MIN(depth) AS depth FROM reach GROUP BY id"
    ).format(seeds=seed_values, etypes=etype_ph, next=next_id, dead=dead_clause)
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


_FTS_TABLE_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts_text "
    "USING fts5(node_id UNINDEXED, text)"
)

# node_id -> the fts_text rowid holding its text (schema v2). `node_id` is an
# UNINDEXED FTS5 column, so `DELETE FROM fts_text WHERE node_id=?` scans the
# whole virtual table — once per indexed node, which made a fold quadratic in
# store size. With this map a re-index is a primary-key lookup plus a rowid
# delete/insert on fts_text.
_FTS_MAP_DDL = (
    "CREATE TABLE IF NOT EXISTS fts_map ("
    "node_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL)"
)


def _ensure_fts_map(conn):
    """Create and back-fill `fts_map` when it is absent (an FTS-bearing store
    from schema v1). Runs inside the caller's transaction, if any.

    Back-fill: a v1 store keeps one fts_text row per node (index_text was
    delete-then-insert), but any stray duplicates are collapsed first, keeping
    the newest (highest rowid) row, so the map is one-to-one. Used by
    init_schema (the migration) and lazily by index_text(s), because a reader
    such as validate's idempotency probe may fold into a copy of a store that
    was never passed through init_schema."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='fts_map'").fetchone():
        return
    conn.execute(_FTS_TABLE_DDL)
    conn.execute(_FTS_MAP_DDL)
    conn.execute(
        "DELETE FROM fts_text WHERE rowid NOT IN "
        "(SELECT MAX(rowid) FROM fts_text GROUP BY node_id)")
    conn.execute(
        "INSERT INTO fts_map (node_id, fts_rowid) "
        "SELECT node_id, rowid FROM fts_text")


def _index_one(conn, node_id, text):
    row = conn.execute("SELECT fts_rowid FROM fts_map WHERE node_id=?",
                       (node_id,)).fetchone()
    if row is not None:
        # Re-index in place: same rowid, so the map row never changes.
        conn.execute("DELETE FROM fts_text WHERE rowid=?", (row[0],))
        conn.execute("INSERT INTO fts_text (rowid, node_id, text) VALUES (?, ?, ?)",
                     (row[0], node_id, text))
    else:
        cur = conn.execute("INSERT INTO fts_text (node_id, text) VALUES (?, ?)",
                           (node_id, text))
        conn.execute("INSERT INTO fts_map (node_id, fts_rowid) VALUES (?, ?)",
                     (node_id, cur.lastrowid))


def index_text(conn, node_id, text, commit=True):
    """Index a node's searchable text. Replaces any text already indexed for
    `node_id` (FTS5 has no UPSERT), so it is idempotent. Raises if the SQLite
    build lacks FTS5. The old row is found through `fts_map` and deleted by
    rowid — no fts_text scan.

    `commit=False` lets a caller batch many inserts into a single transaction
    (e.g. folding a whole window) and commit once at the end."""
    index_texts(conn, [(node_id, text)], commit=commit)


def index_texts(conn, items, commit=True):
    """Batch form of index_text: items is an iterable of (node_id, text).
    Checks the fts_map migration once per call rather than once per node."""
    if not fts5_available(conn):
        raise RuntimeError("FTS5 not available in this SQLite build")
    _ensure_fts_map(conn)
    for node_id, text in items:
        _index_one(conn, node_id, text)
    if commit:
        conn.commit()


# FTS coverage (issue #111): whether fts_text holds every indexable stored
# node. A fold on a host without FTS5 cannot index its records, so it sets
# "partial"; the next fold WITH FTS5 that finds the key not "complete" (also
# on a store that predates the key) indexes every stored node fts_map lacks
# -- and, after a "partial" fold, re-indexes the mapped ones too, since that
# fold may have changed their text -- (gather._backfill_fts, one pass) and
# sets "complete".
FTS_COVERAGE_KEY = "fts_coverage"
FTS_COMPLETE = "complete"
FTS_PARTIAL = "partial"


def fts_unindexed_nodes(conn, include_indexed=False):
    """[(id, node_class, data)] of the stored social / code nodes that have no
    fts_text row (not in fts_map), sorted by id; with `include_indexed`, every
    stored social / code node (a record edited by a fold without FTS5 keeps
    its OLD indexed text, so recovery re-indexes those too). Creates fts_map
    if absent (writer only; needs FTS5)."""
    _ensure_fts_map(conn)
    missing = "" if include_indexed else \
        " AND id NOT IN (SELECT node_id FROM fts_map)"
    return [(r[0], r[1], json.loads(r[2]) if r[2] else None) for r in conn.execute(
        "SELECT id, node_class, data FROM nodes WHERE node_class IN "
        "('social', 'code')" + missing + " ORDER BY id")]


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


def record_window(conn, project, repo, frm, to, commit=True):
    """Append a gathered window to the meta ledger, deduped on the exact tuple.
    `commit=False` leaves the write in the caller's open transaction."""
    raw = get_meta(conn, "gathered_windows")
    windows = json.loads(raw) if raw else []
    entry = {"project": project, "repo": repo, "from": frm, "to": to}
    if entry not in windows:
        windows.append(entry)
        set_meta(conn, "gathered_windows", json.dumps(windows, sort_keys=True),
                 commit=commit)


def get_windows(conn):
    """Return the list of gathered windows recorded in the meta ledger."""
    raw = get_meta(conn, "gathered_windows")
    return json.loads(raw) if raw else []


# Per-member run status of a manifest gather (issue #77), meta `member_status`:
# a JSON list of {project, repo, from, to, status[, error]}, one entry per
# (project, repo, from, to), sorted on that key. `status` is the outcome of the
# LATEST attempt to gather that member for that window.
MEMBER_OK = "ok"
MEMBER_FAILED = "failed"
MEMBER_NOT_ATTEMPTED = "not_attempted"
MEMBER_STATUSES = (MEMBER_OK, MEMBER_FAILED, MEMBER_NOT_ATTEMPTED)


def _status_key(entry):
    return (entry["project"], entry["repo"], entry["from"], entry["to"])


def set_member_status(conn, project, repos, frm, to, status, error=None,
                      commit=True):
    """Record `status` (one of MEMBER_STATUSES) for each repo in `repos` (an
    iterable of `owner/repo`) for the window [frm, to], replacing that member's
    earlier entry for the same window. `error` (a one-line reason) is stored
    only with MEMBER_FAILED. `commit=False`: see upsert_nodes."""
    if status not in MEMBER_STATUSES:
        raise ValueError("unknown member status: {}".format(status))
    entries = {_status_key(e): e for e in get_member_status(conn)}
    for repo in repos:
        entry = {"project": project, "repo": repo, "from": frm, "to": to,
                 "status": status}
        if status == MEMBER_FAILED:
            entry["error"] = error or "unknown error"
        entries[_status_key(entry)] = entry
    set_meta(conn, "member_status",
             json.dumps([entries[k] for k in sorted(entries)], sort_keys=True),
             commit=commit)


def get_member_status(conn, project=None):
    """The recorded member run statuses (optionally of one project), sorted on
    (project, repo, from, to). Empty for a store no manifest gather wrote."""
    raw = get_meta(conn, "member_status")
    entries = json.loads(raw) if raw else []
    return [e for e in entries if project is None or e["project"] == project]


def incomplete_members(conn, project, ts_from=None, ts_to=None):
    """The member status entries of `project` that are not MEMBER_OK, limited
    to windows that overlap [ts_from, ts_to] (compared on the YYYY-MM-DD day;
    either bound may be None)."""
    lo = ts_from[:10] if ts_from else None
    hi = ts_to[:10] if ts_to else None
    return [e for e in get_member_status(conn, project)
            if e["status"] != MEMBER_OK
            and (hi is None or e["from"] <= hi)
            and (lo is None or e["to"] >= lo)]


def describe_member_status(entry):
    """One line for an incomplete member: `repo [from..to] status[: error]`."""
    line = "{} [{}..{}] {}".format(entry["repo"], entry["from"], entry["to"],
                                   entry["status"])
    if entry.get("error"):
        line += ": " + entry["error"]
    return line


def incomplete_warning(conn, project, ts_from=None, ts_to=None):
    """The reader warning (digest.py, view.py) for a project whose members'
    last gather of a window overlapping [ts_from, ts_to] failed or was not
    attempted (issue #77), or None when there is nothing to warn about."""
    try:
        missing = incomplete_members(conn, project, ts_from, ts_to)
    except (ValueError, TypeError, KeyError, AttributeError):
        return "the member run status of project {!r} is unreadable".format(
            project)
    if not missing:
        return None
    return ("project {!r} is incomplete: the last gather of these members did "
            "not finish (re-run gather.py --manifest ... --resume):\n{}".format(
                project, "\n".join("  " + describe_member_status(e)
                                   for e in missing)))


def _clone_sha_key(project, repo):
    return "clone_sha:{}/{}".format(project, repo)


def set_clone_sha(conn, project, repo, sha, commit=True):
    """Record the tree SHA a repo was last gathered against (per project/repo).
    `commit=False` leaves the write in the caller's open transaction."""
    set_meta(conn, _clone_sha_key(project, repo), sha, commit=commit)


def get_clone_sha(conn, project, repo):
    """Return the recorded clone SHA for project/repo, or None if absent."""
    return get_meta(conn, _clone_sha_key(project, repo))


def _ref_date_key(project, repo, frm, to):
    return "ref_date:{}/{}:{}:{}".format(project, repo, frm, to)


def set_ref_date(conn, project, repo, frm, to, ref_date, commit=True):
    """Record the reference date a gathered window [frm, to] was folded with
    (issue #75: gather `--ref-date`, else the window's `to`). One value per
    (project, repo, from, to); a re-fold of the same window replaces it.
    `commit=False` leaves the write in the caller's open transaction."""
    set_meta(conn, _ref_date_key(project, repo, frm, to), ref_date,
             commit=commit)


def get_ref_date(conn, project, repo, frm, to):
    """Return the reference date stored for exactly this gathered window, or
    None when that window was not folded (or was folded before issue #75)."""
    return get_meta(conn, _ref_date_key(project, repo, frm, to))


def _branches_key(project, repo, frm, to):
    return "branches:{}/{}:{}:{}".format(project, repo, frm, to)


def set_branches(conn, project, repo, frm, to, base_branch, branches,
                 commit=True):
    """Record the branches a gathered window [frm, to] was analyzed against
    (issue #84): `base_branch` (the mainline; None when gather could not
    determine it) and `branches` (the list it was given or resolved). One value
    per (project, repo, from, to); a re-fold of the same window replaces it.
    `commit=False` leaves the write in the caller's open transaction."""
    set_meta(conn, _branches_key(project, repo, frm, to),
             json.dumps({"base_branch": base_branch,
                         "branches": list(branches or [])}, sort_keys=True),
             commit=commit)


def get_branches(conn, project, repo, frm, to):
    """Return {"base_branch", "branches"} stored for exactly this gathered
    window, or None when that window was not folded with them (not gathered,
    or folded before issue #84)."""
    raw = get_meta(conn, _branches_key(project, repo, frm, to))
    return json.loads(raw) if raw else None


def _clone_info_key(project, repo, frm, to):
    return "clone:{}/{}:{}:{}".format(project, repo, frm, to)


def set_clone_info(conn, project, repo, frm, to, clone_mode,
                   boundary_dropped_commits, structure_as_of=None, commit=True):
    """Record how a gathered window [frm, to] was cloned (issue #107):
    `clone_mode` (gather.prepare_clone: fresh / fetched / depth1-fallback /
    none; None when the bundle did not say) and `boundary_dropped_commits`
    (the in-window commit shas whose diffs the shallow boundary dropped,
    stored sorted). One value per (project, repo, from, to); a re-fold of the
    same window replaces it. `commit=False` leaves the write in the caller's
    open transaction."""
    info = {"clone_mode": clone_mode,
            "boundary_dropped_commits": sorted(
                set(boundary_dropped_commits or []))}
    # Which tree the window's structure was read at: "window_end" (its last
    # mainline commit) or "head" (the bounded clone did not reach it).
    if structure_as_of is not None:
        info["structure_as_of"] = structure_as_of
    set_meta(conn, _clone_info_key(project, repo, frm, to),
             json.dumps(info, sort_keys=True), commit=commit)


def get_clone_info(conn, project, repo, frm, to):
    """Return {"clone_mode", "boundary_dropped_commits"} stored for exactly
    this gathered window, or None when that window was not folded with them
    (not gathered, folded before issue #107, or an unreadable entry)."""
    raw = get_meta(conn, _clone_info_key(project, repo, frm, to))
    try:
        info = json.loads(raw) if raw else None
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


# Per-window facts (issue #94): the bundle keys a gather computes for ITS
# window (CI runs created in it, the areas of the files it saw, the CODEOWNERS
# and label taxonomy of its checkout, and the first-time contributors of its
# PRs / issues). Kept per (project, repo, from, to) so a view of an earlier
# window does not read a later gather's values. `newcomers` is per-window only
# (no latest-wins node), and stored only by a gather that computed it; so is
# `module_notes` (the changelog entries, version-file movement and version
# tags the window's commits added, read from the clone: gather
# collect_module_notes), and `pr_history` (each window PR's own commits from
# refs/pull/<N>/head and the comment / parameter changes between them: gather
# collect_pr_history), and `affiliations` (each window person's
# maintainer_org / community / unknown label with the deciding signal:
# gather collect_affiliations), and `pr_activity` (each window PR's check
# results, review-thread resolved flags and lifecycle events: gather
# collect_pr_activity).
WINDOW_FACT_KEYS = ("workflow_stats", "code_graph", "code_owners",
                    "label_taxonomy", "newcomers", "module_notes",
                    "pr_history", "affiliations", "pr_activity",
                    "module_ownership")


def _window_facts_key(project, repo, frm, to):
    return "facts:{}/{}:{}:{}".format(project, repo, frm, to)


def _fact_blob_prefix(project, repo):
    return "fact:{}/{}:".format(project, repo)


def set_window_facts(conn, project, repo, frm, to, bundle, clone_sha=None,
                     commit=True):
    """Record the facts of a gathered window [frm, to] (issue #94).

    `facts:{project}/{repo}:{from}:{to}` holds {"clone_sha", "facts": {key:
    sha256}} for each WINDOW_FACT_KEYS value present and non-empty in `bundle`
    (the same rule fold applies to the latest-wins nodes). Each value is
    stored once per repo under `fact:{project}/{repo}:{sha256}` (its JSON
    text, encoded as node data is), so windows with identical facts share it.
    A re-fold of the same window replaces its entry; a value no window of the
    repo references any more is deleted. `commit=False` leaves the writes in
    the caller's open transaction."""
    refs = {}
    for key in WINDOW_FACT_KEYS:
        value = bundle.get(key)
        if not value:
            continue
        text = json.dumps(value, sort_keys=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        set_meta(conn, _fact_blob_prefix(project, repo) + digest, text,
                 commit=False)
        refs[key] = digest
    # Whether CI runs were COLLECTED for this window, separately from what
    # they were: a gather run with --no-workflows has no CI facts at all, while
    # a collected window with zero runs has an (empty) result. gather records
    # it in meta.workflows_collected; a bundle without the flag (hand-built, or
    # gathered before the flag existed) counts as collected when it carries a
    # workflow_stats key.
    meta = bundle.get("meta") or {}
    collected = meta.get("workflows_collected")
    if collected is None:
        collected = "workflow_stats" in bundle
    set_meta(conn, _window_facts_key(project, repo, frm, to),
             json.dumps({"clone_sha": clone_sha, "facts": refs,
                         "ci_collected": bool(collected)},
                        sort_keys=True), commit=False)
    _prune_fact_blobs(conn, project, repo)
    if commit:
        conn.commit()


def _meta_with_prefix(conn, prefix):
    lo, hi = _prefix_range(prefix)
    return conn.execute("SELECT key, value FROM meta WHERE key >= ? AND key < ? "
                        "ORDER BY key", (lo, hi)).fetchall()


def _prune_fact_blobs(conn, project, repo):
    """Delete the repo's fact values no window entry references."""
    used = set()
    for _key, value in _meta_with_prefix(
            conn, "facts:{}/{}:".format(project, repo)):
        try:
            used |= set((json.loads(value).get("facts") or {}).values())
        except (ValueError, AttributeError):
            return   # an unreadable entry: keep everything (validate reports it)
    prefix = _fact_blob_prefix(project, repo)
    for key, _value in _meta_with_prefix(conn, prefix):
        if key[len(prefix):] not in used:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))


def get_window_facts(conn, project, repo, frm, to, keys=WINDOW_FACT_KEYS):
    """The facts recorded for exactly this gathered window, or None when that
    window was not folded (or was folded before issue #94):
    {"clone_sha", "facts": {key: value}, "missing": [keys whose stored value
    is gone], "ci_collected": bool} — `missing` is non-empty only on a damaged
    store; `ci_collected` is False for a window gathered with --no-workflows. Only the
    values of `keys` are read (`()` reads none: an existence probe)."""
    raw = get_meta(conn, _window_facts_key(project, repo, frm, to))
    if not raw:
        return None
    entry = json.loads(raw)
    facts, missing = {}, []
    for key in keys:
        digest = (entry.get("facts") or {}).get(key)
        if digest is None:
            continue
        text = get_meta(conn, _fact_blob_prefix(project, repo) + digest)
        if text is None:
            missing.append(key)
        else:
            facts[key] = json.loads(text)
    # ci_collected: an entry written before the flag existed counts as collected
    return {"clone_sha": entry.get("clone_sha"), "facts": facts,
            "missing": missing,
            "ci_collected": bool(entry.get("ci_collected", True))}


def split_window_key(key, prefix):
    """(scope, window) of a per-window meta key `{prefix}{project}/{repo}:
    {from}:{to}` (`facts:`, `ref_date:`, `branches:`, `clone:`), or None when
    the key does not have that shape. The project may contain `:` and the
    bounds may be timestamps, so the key is not split on its first `:`: the
    project ends at the first `/` (a project name has none, check_project)
    and the repo (`name` or `owner/name`, GitHub's alphabet: no `:`) at the
    next `:` (issue #111)."""
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix):]
    slash = rest.find("/")
    if slash < 0:
        return None
    colon = rest.find(":", slash)
    if colon < 0:
        return None
    return rest[:colon], rest[colon + 1:]


def window_facts_problems(conn):
    """[(meta key, problem)] for every per-window facts entry of the store
    that is unreadable or names a value that is not stored (issue #94)."""
    out = []
    for key, value in _meta_with_prefix(conn, "facts:"):
        try:
            entry = json.loads(value)
            refs = entry.get("facts") or {}
            scope = split_window_key(key, "facts:")[0]
        except (ValueError, AttributeError, TypeError):
            out.append((key, "unreadable per-window facts entry"))
            continue
        for fact in sorted(refs):
            if get_meta(conn, "fact:{}:{}".format(scope, refs[fact])) is None:
                out.append((key, "{} value {} is not stored".format(
                    fact, refs[fact])))
    return out


# Current-state edges (issue #95). A fold keeps the repo's current-state edges
# (blocks, in_milestone, in_iteration, depends_on, owns) equal to what the
# repo's stored inputs imply, retracting the ones they no longer imply (see
# gather.fold_bundle and STORE.md "Current-state edges").
#
# `current_state:{project}/{repo}` records how the LAST fold of the repo kept
# them: {"items": true, "structure": <bool>, "records": true, "from", "to"}.
# `records` (issue #111) is true once a fold kept the record-derived edges of
# every stored record equal to its content (gather._maintain_record_edges;
# the first such fold of a repo heals all its records). `items` is true
# once a fold rebuilt the item relations (blocks / in_milestone /
# in_iteration) from the stored records; `structure` is true when that fold
# could replay depends_on / owns over every gathered window's facts (false
# when a window of the repo has no per-window facts: those edges were then
# only added, as before #95). validate reads it to decide which checks are
# exact. `retractions:{project}/{repo}` is the ledger of what folds retracted:
# a JSON list (oldest first, at most RETRACTION_LEDGER_MAX entries) of
# {"from", "to", "counts": {type: n}, "edges": [[src, dst, type], ...] (at
# most RETRACTION_LEDGER_EDGES, sorted), "standins_removed": [id, ...],
# "people_removed": [id, ...] (#111; absent from entries written before)}. A
# fold that retracts nothing appends nothing, so a re-fold stays a no-op.
RETRACTION_LEDGER_MAX = 100
RETRACTION_LEDGER_EDGES = 200


def _current_state_key(project, repo):
    return "current_state:{}/{}".format(project, repo)


def _retractions_key(project, repo):
    return "retractions:{}/{}".format(project, repo)


def set_current_state(conn, project, repo, entry, commit=True):
    """Record how the last fold of (project, repo) kept its current-state
    edges (see above). `commit=False`: see upsert_nodes."""
    set_meta(conn, _current_state_key(project, repo),
             json.dumps(entry, sort_keys=True), commit=commit)


def get_current_state(conn, project, repo):
    """The current-state record of (project, repo), or None for a repo no
    fold since issue #95 has written (or an unreadable record)."""
    raw = get_meta(conn, _current_state_key(project, repo))
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


# The manifest context of a repo's folds (issue #113), meta
# `fold_context:{project}/{repo}`: {"members": [slug, ...] (sorted),
# "registry": {slug: Terraform registry address or null} or null,
# "bicep_registry_prefix": {slug: prefix or null} or null,
# "members_changed": bool}, written by every manifest fold of the repo (the
# member set and registries it derived the cross-repo edges with).
# `members_changed` is sticky: true once a fold of the repo used another
# member set than the one recorded, ran without one (a single-repo fold), or
# found records stored by a fold that recorded none — the stored records'
# cross-repo closes / cross_ref were then not all derived with this member
# set. validate resolves cross-repo edges exactly from it. A store whose
# folds predate it has none (validate then checks as before, with a WARN).
def _fold_context_key(project, repo):
    return "fold_context:{}/{}".format(project, repo)


def set_fold_context(conn, project, repo, entry, commit=True):
    """Record the manifest context of the latest fold of (project, repo)
    (see above). `commit=False`: see upsert_nodes."""
    set_meta(conn, _fold_context_key(project, repo),
             json.dumps(entry, sort_keys=True), commit=commit)


def get_fold_context(conn, project, repo):
    """The recorded fold context of (project, repo) (see above), or None when
    no manifest fold recorded one or it is unreadable."""
    raw = get_meta(conn, _fold_context_key(project, repo))
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def record_retractions(conn, project, repo, entry, commit=True):
    """Append one fold's retractions to the repo's ledger (see above), keeping
    the newest RETRACTION_LEDGER_MAX entries. `commit=False`: see
    upsert_nodes."""
    entries = get_retractions(conn, project, repo)
    entries.append(entry)
    set_meta(conn, _retractions_key(project, repo),
             json.dumps(entries[-RETRACTION_LEDGER_MAX:], sort_keys=True),
             commit=commit)


def get_retractions(conn, project, repo):
    """The repo's retraction ledger (oldest first); [] when none."""
    raw = get_meta(conn, _retractions_key(project, repo))
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def edges_from_prefix(conn, prefix, edge_type):
    """{(src, dst, type): data} of the `edge_type` edges whose src id starts
    with `prefix` (a repo's '{project}/{repo}#'), as an index range scan."""
    lo, hi = _prefix_range(prefix)
    rows = conn.execute(
        "SELECT * FROM edges WHERE edge_type=? AND src_id >= ? AND src_id < ?",
        (edge_type, lo, hi))
    return {(e["src_id"], e["dst_id"], e["edge_type"]): e["data"]
            for e in (_row_to_edge(r) for r in rows)}


def edges_into_prefix(conn, prefix, edge_type):
    """{(src, dst, type): data} of the `edge_type` edges whose dst id starts
    with `prefix`."""
    lo, hi = _prefix_range(prefix)
    rows = conn.execute(
        "SELECT * FROM edges WHERE dst_id >= ? AND dst_id < ? AND edge_type=?",
        (lo, hi, edge_type))
    return {(e["src_id"], e["dst_id"], e["edge_type"]): e["data"]
            for e in (_row_to_edge(r) for r in rows)}


def delete_edges(conn, keys, commit=True):
    """Delete the edges keyed (src, dst, type) in `keys` (absent keys are a
    no-op). `commit=False`: see upsert_nodes."""
    rows = sorted(set(keys))
    if rows:
        conn.executemany(
            "DELETE FROM edges WHERE src_id=? AND dst_id=? AND edge_type=?",
            rows)
        if commit:
            conn.commit()


def node_ids_with_prefix(conn, prefix):
    """The ids of the nodes whose id starts with `prefix`, sorted."""
    lo, hi = _prefix_range(prefix)
    return [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE id >= ? AND id < ? ORDER BY id", (lo, hi))]


def nodes_with_prefix(conn, prefix):
    """[(id, data)] of the nodes whose id starts with `prefix`, sorted by id."""
    lo, hi = _prefix_range(prefix)
    return [(r[0], json.loads(r[1])) for r in conn.execute(
        "SELECT id, data FROM nodes WHERE id >= ? AND id < ? ORDER BY id",
        (lo, hi))]


def _occurs_more(key):
    """SQL: the JSON text `data` holds `"<key>": ` more often than
    `"<key>": null`, i.e. some `<key>` member has a non-null value. String
    contents never match (a `"` inside a JSON string is escaped)."""
    k, n = '"{}": '.format(key), '"{}": null'.format(key)
    return ("(length(data) - length(replace(data, '{k}', ''))) / {lk} > "
            "(length(data) - length(replace(data, '{n}', ''))) / {ln}").format(
                k=k, n=n, lk=len(k), ln=len(n))


def item_relation_rows(conn, prefix):
    """[(id, data)] of the PR/issue nodes under `prefix` ('{project}/{repo}#')
    whose record may name a milestone, an iteration or a `blocks` ref, sorted
    by id. A cheap SQL prefilter on the stored JSON text (it only ever lets
    too many rows through; the caller reads the record itself), so a fold
    does not decode every item of a large repo."""
    where = "({} OR {} OR instr(data, '\"blocks\": [{{') > 0)".format(
        _occurs_more("milestone"), _occurs_more("iteration"))
    out = []
    for local in ("issue-", "pr-"):
        lo, hi = _prefix_range(prefix + local)
        out.extend((r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT id, data FROM nodes WHERE id >= ? AND id < ? AND "
            "node_class = 'social' AND " + where + " ORDER BY id", (lo, hi)))
    return sorted(out, key=lambda r: r[0])


def delete_orphan_standins(conn, ids, commit=True):
    """Delete each id in `ids` that is a depends_on stand-in node
    (`data.synthesized == STANDIN_MARK`) no edge references any more. Returns
    the deleted ids, sorted. A real node is never deleted. `commit=False`: see
    upsert_nodes."""
    removed = []
    for nid in sorted(set(ids)):
        row = conn.execute("SELECT data FROM nodes WHERE id=?", (nid,)).fetchone()
        if row is None or not _is_standin(row[0]):
            continue
        if conn.execute("SELECT 1 FROM edges WHERE src_id=? OR dst_id=? LIMIT 1",
                        (nid, nid)).fetchone():
            continue
        conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
        removed.append(nid)
    if removed and commit:
        conn.commit()
    return removed


_DEAD_REFS_DDL = (
    "CREATE TABLE IF NOT EXISTS dead_refs ("
    "id TEXT PRIMARY KEY, project TEXT, reason TEXT, first_seen TEXT)"
)


def _has_dead_refs(conn):
    """True if the store has a dead_refs table. Readers check this instead of
    creating the table, so a read never writes schema (issue #79): a store
    without the table simply has no tombstones."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dead_refs'"
    ).fetchone() is not None


def _ensure_dead_refs(conn):
    """Create the dead_refs table if absent. Lets record_dead_ref work on a
    store opened directly (not via init_schema) or one that predates 8d. Only
    the writer calls it; readers use _has_dead_refs (issue #79)."""
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
    if not _has_dead_refs(conn):
        return False
    row = conn.execute("SELECT 1 FROM dead_refs WHERE id=?", (id,)).fetchone()
    return row is not None


def get_dead_refs(conn):
    """All tombstoned ids, sorted (deterministic)."""
    if not _has_dead_refs(conn):
        return []
    return [r[0] for r in conn.execute("SELECT id FROM dead_refs ORDER BY id")]


def init_schema(conn):
    """Create all tables (FTS5 ones when the build supports it) and migrate an
    older store in place to SCHEMA_VERSION.

    Every schema step is additive and idempotent (`IF NOT EXISTS`, back-fill
    only when the table is new), so running init_schema on a v1 store, a
    current store or an empty file all converge on the same schema. v1 -> v2
    adds `idx_edges_type_src` and `fts_map` (back-filled from fts_text). The
    stored `schema_version` is raised to SCHEMA_VERSION, never lowered."""
    conn.executescript(_CORE_SCHEMA)
    with conn:
        if fts5_available(conn):
            conn.execute(_FTS_TABLE_DDL)
            _ensure_fts_map(conn)
        stored = get_meta(conn, "schema_version")
        try:
            stored_n = int(stored) if stored is not None else None
        except ValueError:
            stored_n = None
        if stored_n is None or stored_n < SCHEMA_VERSION:
            set_meta(conn, "schema_version", SCHEMA_VERSION, commit=False)
