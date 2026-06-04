# activity-overview Phase 5 — graphstore foundation (journey-graph substrate) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `graphstore.py` — a stdlib-only SQLite property-graph store (node classes + typed edges + code-event ledger + FTS5 text index + meta) with identity-keyed idempotent upserts, windowed range queries, and bounded spine traversal — as the durable substrate the later gather/extract/spotlight phases read and write.

**Architecture:** A single embedded SQLite database modeled as a property graph: three node classes (`social`/`code`/`structure`) in one `nodes` table keyed by a qualified id, a typed `edges` table, a `code_events` lifecycle ledger, an FTS5 `fts_text` index, and a `meta` key/value table. All persistence is identity-keyed so re-folding overlapping windows is a durable no-op. `graphstore.py` owns *all* SQL; later readers/writers call its function API and never touch SQL directly. This phase is self-contained — no network, no dependency on `gather.py`/`link.py`.

**Tech Stack:** Python 3.11 stdlib only (`sqlite3`, `json`, `time`); `unittest` tests run under `pytest`. No new external dependencies. FTS5 is used when the bundled SQLite provides it (skip-guarded otherwise, mirroring the existing `mmdc` skip pattern).

**Spec:** `docs/superpowers/specs/2026-06-01-activity-overview-design.md` rev 14 — *Architecture (rev 14) — persistent graph substrate*, Sections 1 and 5, decisions 1–2; this is **Phase 5** of the unified P1–P14 ledger.

---

## File Structure

- **Create `.claude/skills/activity-overview/graphstore.py`** — the entire store. One module, one responsibility (persistence + query primitives). Public API only; no bundle/domain logic leaks in (that lives in gather/extract in later phases).
- **Create `.claude/skills/activity-overview/test_graphstore.py`** — unit + idempotency + determinism + traversal + FTS + scale tests, `unittest.TestCase` style matching `test_link.py`.
- **Create `.claude/skills/activity-overview/STORE.md`** — human-readable schema reference (node classes, edge types, ancillary tables, identity rules), the contract later phases and downstream renderers code against.

All three live beside the existing `gather.py`/`link.py`/`render.py` in the skill directory. Tests import with the established shim:

```python
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import graphstore  # noqa: E402
```

Run a single test file from the skill dir with:
`cd .claude/skills/activity-overview && python -m pytest test_graphstore.py -v`
Run the whole suite with: `python -m pytest -q` (must stay green throughout).

---

## API surface (locked here; later phases depend on these exact names)

```
open_store(path=":memory:") -> sqlite3.Connection
init_schema(conn) -> None
fts5_available(conn) -> bool
now_iso() -> str

qualify_id(project, repo, local) -> str          # "{project}/{repo}#{local}"
qualify_person(project, login) -> str            # "{project}#person-{login}" (project-scoped)
parse_id(qid) -> {"scope": str, "local": str}    # split on the last "#"

upsert_node(conn, id, project, repo, node_class, ts, data, fetched_at=None) -> None
get_node(conn, id) -> dict | None                # data parsed back to a dict

upsert_edge(conn, src_id, dst_id, edge_type, ts=None, data=None) -> None
get_edges(conn, node_id, direction="both", edge_types=None) -> list[dict]

add_code_event(conn, artifact_id, event, commit_sha, author=None, date=None,
               hunk=None, ref=None, before=None, after=None, detail=None) -> None
get_code_events(conn, artifact_id) -> list[dict] # ordered by date, then event

range_query(conn, project, repos, ts_from, ts_to, node_class=None) -> list[dict]
traverse_spine(conn, seed_ids, max_depth=6, edge_types=SPINE_EDGE_TYPES)
    -> {"reached": {id: depth}, "missing": [id]}

index_text(conn, node_id, text) -> None          # delete+insert (idempotent); needs fts5
fts_search(conn, query) -> list[str]             # node_ids matching, ordered

set_meta(conn, key, value) -> None
get_meta(conn, key, default=None) -> str | None
record_window(conn, project, repo, frm, to) -> None
get_windows(conn) -> list[dict]
set_clone_sha(conn, project, repo, sha) -> None
get_clone_sha(conn, project, repo) -> str | None
```

Module-level constants: `SCHEMA_VERSION = 1`, `NODE_CLASSES = ("social", "code", "structure")`, `SPINE_EDGE_TYPES = ("closes", "part_of", "cross_ref", "spun_off", "duplicate_of")`.

---

## Task 1: Module skeleton — connection + schema init

**Files:**
- Create: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Create `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphstore'` (or `AttributeError` once the file exists but functions don't).

- [ ] **Step 3: Write minimal implementation**

Create `graphstore.py`:

```python
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
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn, key, default=None):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore schema + connection (journey P5)"
```

---

## Task 2: Identity helpers — qualify / parse

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestIdentity -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'qualify_id'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py` (after `init_schema`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestIdentity -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore identity helpers (journey P5)"
```

---

## Task 3: Nodes — upsert + get, idempotent update

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
        )
        graphstore.upsert_node(
            conn, id=nid, project="p", repo="r", node_class="social",
            ts="2026-04-02T00:00:00Z", data={"state": "closed"},
        )
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        self.assertEqual(count, 1)
        node = graphstore.get_node(conn, nid)
        self.assertEqual(node["data"], {"state": "closed"})
        self.assertEqual(node["ts"], "2026-04-02T00:00:00Z")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestNodes -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'upsert_node'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
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
            fetched_at or now_iso(),
        ),
    )
    conn.commit()


def get_node(conn, id):
    row = conn.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
    return _row_to_node(row) if row else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestNodes -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore node upsert/get with idempotent update (journey P5)"
```

---

## Task 4: Edges — upsert (union, no dup) + get directional

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestEdges -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'upsert_edge'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestEdges -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore edge upsert/get with set semantics (journey P5)"
```

---

## Task 5: Code events — set-semantics ledger

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestCodeEvents -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'add_code_event'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestCodeEvents -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore code-event ledger with set semantics (journey P5)"
```

---

## Task 6: Range query — the window scan

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestRangeQuery -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'range_query'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
def range_query(conn, project, repos, ts_from, ts_to, node_class=None):
    """In-window nodes: project match, repo in `repos`, ts in [ts_from, ts_to].
    Nodes with NULL ts (structure) are excluded — they are not activity.
    Ordered by (ts, id) for deterministic output."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestRangeQuery -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore windowed range query (journey P5)"
```

---

## Task 7: Spine traversal — bounded, allowlisted, with missing detection

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestTraversal -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'traverse_spine'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
def traverse_spine(conn, seed_ids, max_depth=6, edge_types=SPINE_EDGE_TYPES):
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
            r[0] for r in conn.execute(
                "SELECT id FROM nodes WHERE id IN ({})".format(ph), ids
            )
        }
    missing = [i for i in reached if i not in present]
    return {"reached": reached, "missing": missing}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestTraversal -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore bounded spine traversal + missing detection (journey P5)"
```

---

## Task 8: FTS5 text index — index + search (skip-guarded)

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestFts -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'index_text'` (or SKIPPED if this build lacks FTS5 — in which case implement and confirm the suite still passes with the skip).

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
def index_text(conn, node_id, text):
    """Index a node's searchable text. Delete-then-insert keeps it idempotent
    (FTS5 has no UPSERT). Raises if the SQLite build lacks FTS5."""
    if not fts5_available(conn):
        raise RuntimeError("FTS5 not available in this SQLite build")
    conn.execute("DELETE FROM fts_text WHERE node_id=?", (node_id,))
    conn.execute(
        "INSERT INTO fts_text (node_id, text) VALUES (?, ?)", (node_id, text)
    )
    conn.commit()


def fts_search(conn, query):
    """Node ids whose indexed text matches the FTS5 query, ranked by relevance."""
    if not fts5_available(conn):
        raise RuntimeError("FTS5 not available in this SQLite build")
    rows = conn.execute(
        "SELECT node_id FROM fts_text WHERE fts_text MATCH ? ORDER BY rank",
        (query,),
    )
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestFts -v`
Expected: PASS (3 tests) where FTS5 is available; SKIPPED otherwise. Either way the full suite stays green.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore FTS5 text index + search (journey P5)"
```

---

## Task 9: Meta — windows ledger + clone_sha provenance

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py`
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestMetaProvenance -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'record_window'`.

- [ ] **Step 3: Write minimal implementation**

Add to `graphstore.py`:

```python
def record_window(conn, project, repo, frm, to):
    """Append a gathered window to the meta ledger, deduped on the exact tuple."""
    raw = get_meta(conn, "gathered_windows")
    windows = json.loads(raw) if raw else []
    entry = {"project": project, "repo": repo, "from": frm, "to": to}
    if entry not in windows:
        windows.append(entry)
        set_meta(conn, "gathered_windows", json.dumps(windows, sort_keys=True))


def get_windows(conn):
    raw = get_meta(conn, "gathered_windows")
    return json.loads(raw) if raw else []


def _clone_sha_key(project, repo):
    return "clone_sha:{}/{}".format(project, repo)


def set_clone_sha(conn, project, repo, sha):
    set_meta(conn, _clone_sha_key(project, repo), sha)


def get_clone_sha(conn, project, repo):
    return get_meta(conn, _clone_sha_key(project, repo))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestMetaProvenance -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore window ledger + clone_sha provenance (journey P5)"
```

---

## Task 10: Idempotency / accumulation — fold-twice integration test

**Files:**
- Modify: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestIdempotentAccumulation -v`
Expected: PASS immediately — this test exercises already-implemented primitives and is the durability guarantee made explicit. (If it FAILS, a prior task's upsert is appending rather than unioning; fix that task, do not weaken this test.)

- [ ] **Step 3: (No new implementation expected)**

This is a guard test over Tasks 3–9. If green, proceed. If red, the defect is in an upsert primitive — return to that task.

- [ ] **Step 4: Run the full suite**

Run: `cd .claude/skills/activity-overview && python -m pytest -q`
Expected: PASS (all prior tests + these; 2 pre-existing `mmdc` skips remain).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/test_graphstore.py
git commit -m "test(activity): graphstore durable idempotent accumulation (journey P5)"
```

---

## Task 11: Determinism — stable serialization + ordering

**Files:**
- Modify: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestDeterminism -v`
Expected: PASS (the `sort_keys=True` in `upsert_node` and the `ORDER BY ts, id` in `range_query` already guarantee this). If red, restore those two guarantees in their respective tasks.

- [ ] **Step 3: (No new implementation expected)**

Guard test over Tasks 3 and 6.

- [ ] **Step 4: Run the full suite**

Run: `cd .claude/skills/activity-overview && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/test_graphstore.py
git commit -m "test(activity): graphstore deterministic serialization + ordering (journey P5)"
```

---

## Task 12: Scale smoke — windowed query stays fast at volume

**Files:**
- Modify: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `test_graphstore.py`:

```python
import time as _time


class TestScaleSmoke(unittest.TestCase):
    def test_window_query_fast_over_50k_nodes(self):
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
        start = _time.perf_counter()
        got = graphstore.range_query(
            conn, "p", ["r"], "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z"
        )
        elapsed = _time.perf_counter() - start
        self.assertGreater(len(got), 10000)
        self.assertLess(elapsed, 2.0)  # generous ceiling; guards a missing index
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd .claude/skills/activity-overview && python -m pytest test_graphstore.py::TestScaleSmoke -v`
Expected: PASS. The `idx_nodes_window` index keeps the windowed scan well under the 2s ceiling. (If it FAILS on time, confirm `idx_nodes_window` exists from Task 1 — a full table scan is the likely cause.)

- [ ] **Step 3: (No new implementation expected)**

Guard test proving the substrate's scale rationale (the reason flat JSON was abandoned). If slow, the fix is the index from Task 1, not weakening the ceiling.

- [ ] **Step 4: Run the full suite**

Run: `cd .claude/skills/activity-overview && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/test_graphstore.py
git commit -m "test(activity): graphstore scale smoke for windowed query (journey P5)"
```

---

## Task 13: STORE.md — schema reference doc

**Files:**
- Create: `.claude/skills/activity-overview/STORE.md`

- [ ] **Step 1: Write the doc**

Create `STORE.md`:

```markdown
# STORE.md — journey-graph schema

`graphstore.py` is a stdlib-only SQLite property graph: the durable,
identity-keyed substrate that `gather` writes and `extract`/`spotlight` read.
This file is the contract those phases (and downstream renderer authors) code
against. All SQL lives in `graphstore.py`; callers use its function API.

## Identity (qualified ids)

Every node id is namespaced so multi-repo data cannot collide:

- Repo-scoped: `{project}/{repo}#{local}` — e.g.
  `avm/bicep-registry-modules#pr-4821`, `…#issue-17`, `…#<sha>`,
  `…#<path>#<lang>:<subkind>:<name>` (artifacts).
- Project-scoped (people only): `{project}#person-{login}` — a person
  aggregates across all repos in a project.

Helpers: `qualify_id`, `qualify_person`, `parse_id`.

## Node classes (`nodes` table)

One row per entity. Columns: `id` (PK), `project`, `repo`, `node_class`,
`ts`, `data` (JSON blob of the full record), `fetched_at`.

| node_class | holds | `ts` is |
|---|---|---|
| `social` | PRs, issues, comments, reviews | merged/closed/created date |
| `code`   | commits, artifacts (incl. symbols/comments) | author/event date |
| `structure` | code areas, milestones, releases, sprints, people | point-in-time / NULL |

Identity columns (`project`/`repo`/`node_class`) are immutable; `ts`/`data`/
`fetched_at` refresh on re-upsert. Re-folding an overlapping window mutates
nothing already correct — the dedup guarantee is durable, not recomputed.

`structure` nodes typically carry NULL `ts` and are excluded from window
range scans (they are not activity).

## Edges (`edges` table)

`(src_id, dst_id, edge_type, ts, data)`, PK `(src,dst,type)`. Re-upsert
unions, never appends. Edge types:

`closes` (pr→issue), `part_of` (commit→pr), `cross_ref` (issue↔pr↔commit),
`duplicate_of`/`spun_off` (issue→issue), `touches` (commit/pr→area),
`authored`/`reviewed`/`merged`/`reported`/`commented`/`reacted` (person→node),
`owns` (person→area), `depends_on` (area→area, carries version/transitive in
`data`), `replaced_by`/`identity_from` (artifact→artifact), `blocks`
(issue→issue), `in_milestone`/`in_iteration` (social→structure).

### Spine edges and decision trains

A decision train is **not stored** — it is the connected component reachable
from a root issue over the *spine* edge allowlist:
`SPINE_EDGE_TYPES = (closes, part_of, cross_ref, spun_off, duplicate_of)`.
`traverse_spine(seed_ids, max_depth, edge_types)` walks these **undirected**
(both directions), depth-capped, and returns `{reached: {id: depth}, missing:
[id]}`. `missing` ids are spine nodes referenced but not yet stored (e.g. an
out-of-window issue a PR closes) — what a reader backfills.

## Code-event ledger (`code_events` table)

One row per artifact lifecycle event: `(artifact_id, event, commit_sha,
author, date, hunk, ref, before, after, detail)`, PK `(artifact, commit,
event)` → set semantics (re-seeing a commit is a no-op). An artifact's
`lifecycle[]` is `get_code_events(artifact_id)` ordered by date.

## Text index (`fts_text`, FTS5)

`index_text(node_id, text)` (delete-then-insert; idempotent) feeds an FTS5
index; `fts_search(query)` returns matching node ids ranked by relevance.
Created only when the SQLite build supports FTS5 (`fts5_available`); the
text-mining spotlight query depends on it.

## Meta (`meta` table)

Key/value provenance: `schema_version`; `gathered_windows` (JSON list of
folded `{project,repo,from,to}`, deduped — `record_window`/`get_windows`);
`clone_sha:{project}/{repo}` (the tree a repo was last gathered against, for
deterministic resume/roll-up — `set_clone_sha`/`get_clone_sha`).

## Determinism

`data` blobs serialize with `sort_keys=True`; `range_query` orders by
`(ts, id)`. Given fixed inputs the store is byte-stable (modulo `fetched_at`).
```

- [ ] **Step 2: Verify the doc matches the code**

Run: `cd .claude/skills/activity-overview && python -m pytest -q`
Expected: PASS (doc-only change; suite stays green). Manually confirm every function name and edge type in `STORE.md` exists in `graphstore.py`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/STORE.md
git commit -m "docs(activity): STORE.md graphstore schema reference (journey P5)"
```

---

## Task 14: Final verification — full suite green

**Files:** none (verification only)

- [ ] **Step 1: Run the complete skill test suite**

Run: `cd .claude/skills/activity-overview && python -m pytest -q`
Expected: PASS — all pre-existing `test_gather.py`/`test_link.py`/`test_render.py` tests **plus** the new `test_graphstore.py`, with only the 2 pre-existing `mmdc` skips (and the FTS class skipped only if this build lacks FTS5).

- [ ] **Step 2: Confirm no SQL leaked outside graphstore**

Run: `cd .claude/skills/activity-overview && grep -L "graphstore" $(grep -rl "INSERT INTO\|SELECT .* FROM" --include="*.py" . | grep -v graphstore.py | grep -v test_) 2>/dev/null; echo "checked"`
Expected: prints only `checked` (no other module embeds store SQL). If a file is listed, move its SQL behind a `graphstore` function.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin claude/journey-graph-design
```

---

## Self-Review (completed by plan author)

**Spec coverage (Section 1 + Section 5 + decisions 1–2):**
- 3 node classes + qualified ids → Tasks 1–3 (`nodes`, `qualify_id`/`qualify_person`). ✓
- Typed edge table + all edge types → Task 4 (+ documented in Task 13). ✓
- Train = traversal seed over spine allowlist (not a stored row) → Task 7. ✓
- `code_events` lifecycle ledger, set semantics → Task 5. ✓
- FTS5 text index → Task 8. ✓
- `meta` (gathered_windows, clone_sha, schema_version) → Tasks 1, 9. ✓
- Durable idempotent accumulation (the dedup guarantee) → Task 10. ✓
- Determinism → Task 11. ✓ Scale (the JSON-breaks rationale) → Task 12. ✓
- `graphstore.py` owns all SQL → enforced by Task 14 Step 2. ✓
- STORE.md contract → Task 13. ✓
- **Out of scope for P5 (correctly deferred):** `backfill` network fetch (P6 — `traverse_spine` only *reports* `missing`), the bundle-view materialization + golden-bundle equivalence test (P7), spotlight queries (P8), multi-repo end-to-end (P9). The design's Section 5 "golden-bundle equivalence" test lands in P7 where `extract` exists; P5 ships only the store primitives it stands on.

**Placeholder scan:** no TBD/"handle edge cases"/"similar to Task N" — every code step shows complete code. ✓

**Type/name consistency:** `open_store`/`init_schema`/`upsert_node`/`get_node`/`upsert_edge`/`get_edges`/`add_code_event`/`get_code_events`/`range_query`/`traverse_spine`/`index_text`/`fts_search`/`record_window`/`get_windows`/`set_clone_sha`/`get_clone_sha`/`qualify_id`/`qualify_person`/`parse_id` — names identical across every task and the API-surface section. `SPINE_EDGE_TYPES`/`NODE_CLASSES`/`SCHEMA_VERSION` constants consistent. `traverse_spine` return shape `{"reached": {id: depth}, "missing": [id]}` used identically in Task 7 tests and Task 13 doc. ✓

---

## Note for later phases (do not lose)

Phase 4b (sub-agent per-train narration + the gather review/lifecycle slice) was deferred under the **flat-bundle** architecture. It should be **revisited after P5–P7 on this substrate**, not retrofitted onto JSON: narration reads from a per-train slice over `traverse_spine`, and the review/lifecycle data becomes `social` nodes + `reviewed`/`in_milestone`/lifecycle edges. Sequence it after `extract` (P7) proves the bundle-view equivalence, so 4b is built once, on the graph.
