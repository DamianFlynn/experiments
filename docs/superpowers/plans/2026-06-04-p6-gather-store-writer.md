# P6 — Gather as Store Writer (Dual-Write) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `gather` fold its assembled bundle into the SQLite journey-graph store (`graphstore.py`) — nodes, spine edges, and the file-level code-event ledger — alongside the existing JSON bundle, so P7's `extract` has a populated substrate to reconstruct from.

**Architecture:** Dual-write. `gather.acquire()` still returns the raw bundle and `main()` still writes the JSON; when `--store PATH` is passed, `main()` also folds that same bundle into the store via a new `gather.fold_bundle(conn, bundle)`. The fold is a pure mapping over the bundle dict, so it works identically for fresh / `--rollup` / `--resume` runs. Edge derivation reuses gather's existing ref-parsers; the only piece that moves is `resolve_commit_pr` (link → gather), with link re-exporting it so trains and store edges read the same source fields and stay consistent by construction.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `json`, `re`), `unittest`/`pytest`. No new dependencies.

**Scope (locked during brainstorming):**
- **Writes:** all node classes the *raw* bundle contains — `social` (PRs, issues; comments/reviews stay embedded in the parent's data blob), `code` (commits) + the **file-level** `code_events` ledger, `structure` (milestones, releases, code-graph areas).
- **Edges:** **spine edges only** — `closes` (pr→issue), `cross_ref` (pr→issue), `part_of` (commit→pr).
- **Default off:** `--store` is opt-in in P6, so the change is strictly additive — zero behavior change unless the flag is passed.
- **Explicitly deferred to a later phase (P8):** people nodes & artifact nodes (both link-derived), `symbol_events`, and every non-spine edge (`authored`/`reviewed`/`merged`/`commented`/`touches`/`owns`/`depends_on`/`in_milestone`/`replaced_by`/`identity_from`/`blocks`). `label_taxonomy`/`code_owners`/`workflows` are not folded in P6.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.claude/skills/activity-overview/graphstore.py` | SQLite primitives | **Modify** — add batch `upsert_nodes` / `upsert_edges` / `add_code_events` (single-transaction) |
| `.claude/skills/activity-overview/gather.py` | Acquire bundle + (new) fold to store | **Modify** — move `resolve_commit_pr`/`attach_commit_prs` here; add `fold_bundle`; add `--store` flag + `main()` wiring; `import graphstore` |
| `.claude/skills/activity-overview/link.py` | Offline enrichment | **Modify** — drop the two moved defs + `_PR_RE`; re-export them from `gather` |
| `.claude/skills/activity-overview/test_graphstore.py` | Store primitive tests | **Modify** — batch-helper tests |
| `.claude/skills/activity-overview/test_gather.py` | Gather tests | **Modify** — canonical `resolve_commit_pr` tests + `fold_bundle` tests + `--store` wiring test |
| `.claude/skills/activity-overview/STORE.md` | Store contract | **Modify** — note gather as writer + P6 scope |
| `.claude/skills/activity-overview/SKILL.md` | Procedure | **Modify** — document `--store` |

All commands below assume the working directory is the skill folder:

```bash
cd .claude/skills/activity-overview
```

---

## Task 1: Relocate `resolve_commit_pr` / `attach_commit_prs` to gather (shared home), re-export from link

**Why:** The store's `part_of` edges and link's trains must agree on commit→PR resolution. Make `gather` the single home (it already owns `parse_closing_refs`/`parse_timeline_crossrefs`); `link` re-exports so its public API and ~25 existing setup-call sites in `test_link.py` keep working unchanged.

**Files:**
- Modify: `gather.py` (add the two functions + `_PR_RE` near the other parsers)
- Modify: `link.py:10-25` (remove defs + `_PR_RE`, add re-export aliases)
- Test: `test_gather.py` (canonical behavior tests)

- [ ] **Step 1: Write the failing canonical test in `test_gather.py`**

Add to `test_gather.py` (top-level, near other parser tests):

```python
class ResolveCommitPrTests(unittest.TestCase):
    def test_squash_subject(self):
        self.assertEqual(gather.resolve_commit_pr("Add policy param (#42)"), 42)

    def test_merge_subject(self):
        self.assertEqual(
            gather.resolve_commit_pr("Merge pull request #42 from feature/policy"), 42)

    def test_none_when_absent(self):
        self.assertIsNone(gather.resolve_commit_pr("Tidy outputs"))

    def test_attach_sets_pr_field(self):
        commits = [{"sha": "a", "message": "Fix bug (#7)"},
                   {"sha": "b", "message": "Refactor"}]
        gather.attach_commit_prs(commits)
        self.assertEqual(commits[0]["pr"], 7)
        self.assertIsNone(commits[1]["pr"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest test_gather.py::ResolveCommitPrTests -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'resolve_commit_pr'`.

- [ ] **Step 3: Add the functions to `gather.py`**

Add near the other ref-parsers (e.g. just above `def parse_timeline_crossrefs`). Place the regex with the other module-level patterns:

```python
_PR_RE = re.compile(r"Merge pull request #(\d+)|\(#(\d+)\)")


def resolve_commit_pr(message):
    """Best-effort PR number from a commit subject (merge or squash style)."""
    m = _PR_RE.search(message or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def attach_commit_prs(commits):
    """Set each commit's `pr` from its message in place."""
    for c in commits:
        c["pr"] = resolve_commit_pr(c.get("message", ""))
    return commits
```

Confirm `import re` is already present at the top of `gather.py` (it is — used by existing parsers). If not, add it.

- [ ] **Step 4: Replace the defs in `link.py` with re-export aliases**

In `link.py`, delete `_PR_RE` (line 10) and the `resolve_commit_pr` (lines 13-18) and `attach_commit_prs` (lines 21-25) definitions. In their place put:

```python
# commit->PR resolution lives in gather (shared with the store writer, so trains
# and the store's part_of edges read the same signal); re-exported here to keep
# link's public entry points (and its callers/tests) stable.
resolve_commit_pr = gather.resolve_commit_pr
attach_commit_prs = gather.attach_commit_prs
```

`link.py` already has `import gather` at line 8, so the aliases resolve. `enrich()` line 1155 (`attach_commit_prs(bundle["commits"])`) now calls the alias — no change needed there.

- [ ] **Step 5: Run gather + link test suites**

Run: `python3 -m pytest test_gather.py::ResolveCommitPrTests test_link.py -q`
Expected: PASS — the new gather tests pass and **all** existing `test_link.py` tests (which call `link.attach_commit_prs(...)` via the alias) still pass.

- [ ] **Step 6: Commit**

```bash
git add gather.py link.py test_gather.py
git commit -m "refactor(activity): move commit->PR resolution to gather, re-export from link"
```

---

## Task 2: Batch write helpers in `graphstore.py`

**Why:** `upsert_node`/`upsert_edge`/`add_code_event` commit per row; a fold writes hundreds–thousands of rows. Add single-transaction batch variants so `fold_bundle` is one commit per table.

**Files:**
- Modify: `graphstore.py` (add three functions after the existing single-row variants)
- Test: `test_graphstore.py`

- [ ] **Step 1: Write the failing test in `test_graphstore.py`**

```python
class BatchWriteTests(unittest.TestCase):
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

    def test_add_code_events_batch_set_semantics(self):
        rows = [("p/r#a.py", "add", "c1", "alice", "2026-01-01", None, None,
                 None, None, None)]
        graphstore.add_code_events(self.conn, rows)
        graphstore.add_code_events(self.conn, rows)  # set semantics: no dup
        evs = graphstore.get_code_events(self.conn, "p/r#a.py")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["event"], "add")

    def test_empty_batches_are_noops(self):
        graphstore.upsert_nodes(self.conn, [])
        graphstore.upsert_edges(self.conn, [])
        graphstore.add_code_events(self.conn, [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest test_graphstore.py::BatchWriteTests -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'upsert_nodes'`.

- [ ] **Step 3: Add the batch helpers to `graphstore.py`**

Insert after `upsert_node` and `upsert_edge`/`add_code_event` respectively (or grouped together after `add_code_event`):

```python
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
    payload = [
        (a, e, c, au, d, h,
         json.dumps(r, sort_keys=True) if r is not None else None, b, af, de)
        for (a, e, c, au, d, h, r, b, af, de) in rows
    ]
    if payload:
        conn.executemany(
            "INSERT OR IGNORE INTO code_events "
            "(artifact_id, event, commit_sha, author, date, hunk, ref, before, after, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
    conn.commit()
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python3 -m pytest test_graphstore.py::BatchWriteTests -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full store suite (no regressions)**

Run: `python3 -m pytest test_graphstore.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add graphstore.py test_graphstore.py
git commit -m "feat(graphstore): batch upsert_nodes/upsert_edges/add_code_events"
```

---

## Task 3: `fold_bundle(conn, bundle)` in `gather.py`

**Why:** The core of P6 — map an assembled raw bundle to store nodes, spine edges, and the file-level code-event ledger, by stable identity, idempotently.

**Files:**
- Modify: `gather.py` (add `import graphstore` at top; add `fold_bundle`)
- Test: `test_gather.py`

- [ ] **Step 1: Add `import graphstore` to `gather.py`**

At the top of `gather.py`, with the other imports, add:

```python
import graphstore
```

- [ ] **Step 2: Write the failing test in `test_gather.py`**

Add a fixture-bundle helper and tests:

```python
import graphstore  # at top of test_gather.py if not already imported


def _fold_fixture_bundle():
    return {
        "meta": {"owner": "acme", "repo": "widget", "from": "2026-01-01",
                 "to": "2026-01-31", "clone_sha": "deadbeef"},
        "prs": [{
            "number": 10, "url": "u/10", "state": "closed", "merged": True,
            "merged_at": "2026-01-10T00:00:00Z", "created_at": "2026-01-05T00:00:00Z",
            "closed_at": "2026-01-10T00:00:00Z",
            "closes": [3], "crossref_issues": [4],
        }],
        "issues": [
            {"number": 3, "url": "u/3", "state": "closed",
             "closed_at": "2026-01-10T00:00:00Z", "updated_at": "2026-01-10T00:00:00Z"},
            {"number": 4, "url": "u/4", "state": "open",
             "updated_at": "2026-01-09T00:00:00Z", "closed_at": None},
        ],
        "commits": [
            {"sha": "abc123", "message": "Add thing (#10)", "author": "alice",
             "date": "2026-01-09T00:00:00Z", "files": ["a.py"]},
            {"sha": "def456", "message": "WIP no pr ref", "author": "bob",
             "date": "2026-01-08T00:00:00Z", "files": ["b.py"]},
        ],
        "code_events": [
            {"commit": "abc123", "author": "alice", "date": "2026-01-09T00:00:00Z",
             "change": "add", "path": "a.py"},
            {"commit": "abc123", "author": "alice", "date": "2026-01-09T00:00:00Z",
             "change": "rename", "path": "c.py", "old_path": "b.py"},
        ],
        "milestones": [{"number": 1, "title": "v1.0", "state": "open"}],
        "releases": [{"tag_name": "v0.9", "published_at": "2026-01-15T00:00:00Z"}],
        "code_graph": {"areas": [{"name": "core", "path": "src/core"}]},
    }


class FoldBundleTests(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _fold_fixture_bundle())

    def test_nodes_by_class_and_identity(self):
        pr = graphstore.get_node(self.conn, "acme/widget#pr-10")
        self.assertEqual(pr["node_class"], "social")
        self.assertEqual(pr["ts"], "2026-01-10T00:00:00Z")  # merged_at
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#issue-3")["node_class"], "social")
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#abc123")["node_class"], "code")
        ms = graphstore.get_node(self.conn, "acme/widget#milestone-1")
        self.assertEqual(ms["node_class"], "structure")
        self.assertIsNone(ms["ts"])  # structure: NULL ts, excluded from window scans
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#release-v0.9")["node_class"], "structure")
        self.assertEqual(graphstore.get_node(
            self.conn, "acme/widget#area-core")["node_class"], "structure")

    def test_spine_edges(self):
        out = graphstore.get_edges(self.conn, "acme/widget#pr-10", direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("closes", "acme/widget#issue-3"), types)
        self.assertIn(("cross_ref", "acme/widget#issue-4"), types)
        part = graphstore.get_edges(self.conn, "acme/widget#abc123",
                                    direction="out", edge_types=["part_of"])
        self.assertEqual(part[0]["dst_id"], "acme/widget#pr-10")
        # commit without a PR ref produces no part_of edge
        self.assertEqual(graphstore.get_edges(
            self.conn, "acme/widget#def456", edge_types=["part_of"]), [])

    def test_train_reachable_over_spine(self):
        # issue-3 -> pr-10 (closes) -> abc123 (part_of); issue-4 via cross_ref
        res = graphstore.traverse_spine(self.conn, ["acme/widget#issue-3"])
        self.assertIn("acme/widget#pr-10", res["reached"])
        self.assertIn("acme/widget#abc123", res["reached"])
        self.assertIn("acme/widget#issue-4", res["reached"])

    def test_code_event_ledger_file_level(self):
        evs = graphstore.get_code_events(self.conn, "acme/widget#a.py")
        self.assertEqual([e["event"] for e in evs], ["add"])
        ren = graphstore.get_code_events(self.conn, "acme/widget#c.py")
        self.assertEqual(ren[0]["event"], "rename")
        self.assertEqual(ren[0]["detail"], "b.py")  # old_path -> detail

    def test_window_and_clone_sha_recorded(self):
        self.assertIn(
            {"project": "acme", "repo": "widget", "from": "2026-01-01",
             "to": "2026-01-31"},
            graphstore.get_windows(self.conn))
        self.assertEqual(
            graphstore.get_clone_sha(self.conn, "acme", "widget"), "deadbeef")

    def test_idempotent_refold(self):
        gather.fold_bundle(self.conn, _fold_fixture_bundle())  # second fold
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 8)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 3)

    def test_range_query_excludes_structure_with_null_ts(self):
        social_code = graphstore.range_query(
            self.conn, "acme", ["widget"], "2026-01-01", "2026-01-31")
        ids = {n["id"] for n in social_code}
        self.assertIn("acme/widget#pr-10", ids)
        self.assertNotIn("acme/widget#milestone-1", ids)  # NULL ts -> excluded

    def test_requires_owner_and_repo(self):
        with self.assertRaises(ValueError):
            gather.fold_bundle(self.conn, {"meta": {}})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python3 -m pytest test_gather.py::FoldBundleTests -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'fold_bundle'`.

- [ ] **Step 4: Implement `fold_bundle` in `gather.py`**

Add (e.g. just above `def main`):

```python
def fold_bundle(conn, bundle):
    """Fold a raw bundle into the journey-graph store by stable identity:
    upsert nodes, spine edges, and the file-level code-event ledger. Idempotent
    — re-folding an overlapping window mutates nothing already correct. See
    STORE.md for the schema and identity rules.

    P6 scope: social (prs/issues; comments/reviews stay embedded in the parent's
    data blob), code (commits) + the file-level code_events ledger, structure
    (milestones/releases/areas), and the spine edges closes/cross_ref/part_of.
    People & artifact nodes (link-derived), symbol_events, and every non-spine
    edge land in a later phase.
    """
    meta = bundle.get("meta", {})
    project, repo = meta.get("owner"), meta.get("repo")
    if not project or not repo:
        raise ValueError("bundle meta needs owner and repo to qualify ids")

    fetched = graphstore.now_iso()
    nodes, edges, events = [], [], []

    def qid(local):
        return graphstore.qualify_id(project, repo, local)

    # social: PRs, with closes/cross_ref spine edges to their issues.
    for pr in bundle.get("prs", []):
        pid = qid("pr-{}".format(pr["number"]))
        ts = pr.get("merged_at") or pr.get("closed_at") or pr.get("created_at")
        nodes.append((pid, project, repo, "social", ts, pr, fetched))
        for n in pr.get("closes") or []:
            edges.append((pid, qid("issue-{}".format(n)), "closes", None, None))
        for n in pr.get("crossref_issues") or []:
            edges.append((pid, qid("issue-{}".format(n)), "cross_ref", None, None))

    # social: issues.
    for iss in bundle.get("issues", []):
        ts = iss.get("closed_at") or iss.get("updated_at")
        nodes.append((qid("issue-{}".format(iss["number"])), project, repo,
                      "social", ts, iss, fetched))

    # code: commits, with part_of spine edge to the PR named in the subject.
    for c in bundle.get("commits", []):
        cid = qid(c["sha"])
        nodes.append((cid, project, repo, "code", c.get("date"), c, fetched))
        prn = resolve_commit_pr(c.get("message", ""))
        if prn is not None:
            edges.append((cid, qid("pr-{}".format(prn)), "part_of", None, None))

    # code: file-level code-event ledger (rename/copy source -> detail).
    for ev in bundle.get("code_events", []):
        path = ev.get("path")
        if not path:
            continue
        events.append((
            qid(path), ev.get("change"), ev.get("commit"), ev.get("author"),
            ev.get("date"), None, None, None, None, ev.get("old_path"),
        ))

    # structure: milestones & areas (NULL ts -> excluded from window scans),
    # releases (dated point-in-time).
    for m in bundle.get("milestones", []):
        local = "milestone-{}".format(m.get("number") or m.get("title"))
        nodes.append((qid(local), project, repo, "structure", None, m, fetched))
    for r in bundle.get("releases", []):
        local = "release-{}".format(r.get("tag_name"))
        nodes.append((qid(local), project, repo, "structure",
                      r.get("published_at"), r, fetched))
    for area in (bundle.get("code_graph", {}) or {}).get("areas") or []:
        local = "area-{}".format(area.get("name") or area.get("path"))
        nodes.append((qid(local), project, repo, "structure", None, area, fetched))

    graphstore.upsert_nodes(conn, nodes)
    graphstore.upsert_edges(conn, edges)
    graphstore.add_code_events(conn, events)
    graphstore.record_window(conn, project, repo, meta.get("from"), meta.get("to"))
    if meta.get("clone_sha"):
        graphstore.set_clone_sha(conn, project, repo, meta["clone_sha"])
```

- [ ] **Step 5: Run it to verify it passes**

Run: `python3 -m pytest test_gather.py::FoldBundleTests -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): fold_bundle writes nodes, spine edges, code-event ledger to store"
```

---

## Task 4: Wire `--store PATH` into gather's CLI

**Why:** Make the fold reachable from a real run (and from P7's golden-equivalence test) without changing default behavior — off unless `--store` is passed.

**Files:**
- Modify: `gather.py` — `parse_args` (add flag) and `main` (fold when set)
- Test: `test_gather.py`

- [ ] **Step 1: Write the failing test in `test_gather.py`**

```python
class StoreFlagTests(unittest.TestCase):
    def test_main_folds_into_store_when_flag_set(self):
        import tempfile, os, json as _json
        with tempfile.TemporaryDirectory() as d:
            store_path = os.path.join(d, "store.db")
            out_path = os.path.join(d, "bundle.json")
            with open(out_path, "w") as fh:
                _json.dump(_fold_fixture_bundle(), fh)
            # --resume re-emits the given bundle (no network), then --store folds it.
            gather.main(["--resume", out_path, "--out", out_path,
                         "--store", store_path])
            conn = graphstore.open_store(store_path)
            self.assertEqual(graphstore.get_node(
                conn, "acme/widget#pr-10")["node_class"], "social")

    def test_no_store_flag_writes_no_db(self):
        import tempfile, os, json as _json
        with tempfile.TemporaryDirectory() as d:
            out_path = os.path.join(d, "bundle.json")
            with open(out_path, "w") as fh:
                _json.dump(_fold_fixture_bundle(), fh)
            gather.main(["--resume", out_path, "--out", out_path])
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".db")], [])
```

> Note: this reuses `--resume`, which loads a bundle from disk and re-emits it without network access (`resume_acquire`/`resume_bundle` in `gather.py`). If `--resume` mutates `code_graph` in a way that trips the fold, fall back to calling `fold_bundle` directly in the test; the wiring assertion is what matters.

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest test_gather.py::StoreFlagTests -v`
Expected: FAIL — `error: unrecognized arguments: --store` (or no `.db` written).

- [ ] **Step 3: Add the `--store` argument in `parse_args`**

In `gather.py` `parse_args` (alongside `p.add_argument("--out", default=None)` near line 1370):

```python
    p.add_argument("--store", default=None,
                   help="also fold the bundle into this SQLite journey-graph "
                        "store (off by default; additive to the JSON bundle)")
```

- [ ] **Step 4: Fold in `main()` after the JSON is written**

In `gather.py` `main()`, after the `sys.stderr.write(f"wrote {out}\n")` line and before `return out`:

```python
    if getattr(args, "store", None):
        conn = graphstore.open_store(args.store)
        graphstore.init_schema(conn)
        fold_bundle(conn, bundle)
        conn.close()
        sys.stderr.write(f"folded bundle into store {args.store}\n")
```

- [ ] **Step 5: Run it to verify it passes**

Run: `python3 -m pytest test_gather.py::StoreFlagTests -v`
Expected: PASS (2 tests). If the `--resume` path interferes, switch the first test to assert against a direct `gather.fold_bundle` call as noted, then re-run.

- [ ] **Step 6: Run the full gather suite (no regressions)**

Run: `python3 -m pytest test_gather.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): gather --store folds the bundle into the journey-graph store"
```

---

## Task 5: Document the writer in STORE.md and SKILL.md

**Why:** STORE.md is the store contract and currently says "gather writes" aspirationally; make P6's actual scope explicit. SKILL.md documents the CLI.

**Files:**
- Modify: `STORE.md`
- Modify: `SKILL.md`

- [ ] **Step 1: Add a "Writer (P6)" note to `STORE.md`**

Under the `## Edges` / `### Spine edges and decision trains` area (or as a short new subsection after `## Identity`), add:

```markdown
## Writer (gather --store)

`gather --store PATH` folds its assembled bundle into the store via
`gather.fold_bundle(conn, bundle)` — additive to the JSON bundle, idempotent by
identity. P6 writes: `social` (PRs/issues; comments/reviews stay embedded in the
parent's `data` blob), `code` (commits) + the file-level `code_events` ledger,
`structure` (milestones/releases/areas), and the **spine** edges `closes`,
`cross_ref`, `part_of`. People & artifact nodes (link-derived), `symbol_events`,
and all non-spine edges (`authored`/`reviewed`/`touches`/`owns`/…) are written by
a later phase.
```

- [ ] **Step 2: Add the `--store` option to `SKILL.md`**

In `SKILL.md` step 1 (Acquire), after the main gather command block, add:

```markdown
   - **Persist to the journey-graph store (optional).** Add `--store workspace/journey.db`
     to also fold the bundle into the SQLite substrate (`STORE.md`) — additive and
     idempotent; the JSON bundle is still written and the rest of the procedure is
     unchanged.
```

- [ ] **Step 3: Run the full skill suite (docs change shouldn't break anything)**

Run: `python3 -m pytest -q`
Expected: PASS (all suites).

- [ ] **Step 4: Commit**

```bash
git add STORE.md SKILL.md
git commit -m "docs(activity): document gather --store writer and P6 store scope"
```

---

## Final Verification

- [ ] **Run the entire suite:**

Run: `python3 -m pytest -q`
Expected: PASS — `test_graphstore.py`, `test_gather.py`, `test_link.py`, `test_render.py` all green.

- [ ] **Smoke-check determinism of a re-fold (manual):**

```bash
python3 - <<'PY'
import graphstore, gather
from test_gather import _fold_fixture_bundle
c = graphstore.open_store(":memory:"); graphstore.init_schema(c)
gather.fold_bundle(c, _fold_fixture_bundle())
n1 = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
e1 = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
gather.fold_bundle(c, _fold_fixture_bundle())
n2 = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
e2 = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
assert (n1, e1) == (n2, e2), (n1, e1, n2, e2)
print("idempotent:", n1, "nodes", e1, "edges")
PY
```

Expected: `idempotent: 8 nodes 3 edges` (1 PR + 2 issues + 2 commits + 3 structure).

---

## Self-Review Notes (carried from planning)

- **Spec coverage:** dual-write (Tasks 3–4), shared edge derivation via gather (Task 1), full bundle node classes + spine edges (Task 3), batch-commit plumbing (Task 2), `missing`-backfill left to readers (not in scope — `traverse_spine` already returns it). ✅
- **Deferred & documented:** people/artifact nodes, `symbol_events`, non-spine edges, `label_taxonomy`/`code_owners`/`workflows` (STORE.md "Writer" note). These pair naturally with P8 (people + non-spine edges) and the artifact-node identity scheme.
- **P7 hook:** `fold_bundle` + `--store` give P7's `extract` a populated store and the golden-equivalence harness its writer; `traverse_spine` (already in `graphstore.py`) is the train-assembly primitive P7 consumes.
- **Type consistency:** `fold_bundle(conn, bundle)`, `upsert_nodes(conn, rows)`, `upsert_edges(conn, rows)`, `add_code_events(conn, rows)`, `resolve_commit_pr(message)` used identically across tasks; node id scheme `qualify_id(owner, repo, local)` with locals `pr-N`/`issue-N`/`<sha>`/`milestone-…`/`release-…`/`area-…` consistent between Task 3 implementation and tests.
