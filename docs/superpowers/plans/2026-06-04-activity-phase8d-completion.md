# Phase 8d — Train-Completion Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote `gather.backfill` (single-node) + `extract`'s inline 7c loop into a shared `complete.py` orchestrator that completes a decision train transitively along the causal spine, bounded by the query window, and stamps every train with an honest `complete`/`gaps` contract while pruning + remembering 404 phantoms.

**Architecture:** A new pure-policy module `complete.py` reads the store via `graphstore`, drives the existing injected `backfill(conn, id)` seam through a window-bounded BFS, and returns `{reached, gaps[{id,reason}], fetched}`. `gather` stays the only writer/network: `backfill` gains an `ABSENT` outcome and records dead refs; `graphstore` gains a tiny `dead_refs` tombstone table. `extract` is refactored onto `complete_train` (byte-identical default); `spotlight`'s four queries thread `complete.annotate` onto every train (offline-by-default, no network).

**Tech Stack:** Python 3 stdlib (`sqlite3`), `unittest.TestCase` tests run via `pytest`. No new dependencies. All tests offline (fixture-backed `FakeFetcher`).

**Working branch:** `claude/phase8-spotlight` (PR #14). All commits land here.

**Run tests from:** `.claude/skills/activity-overview/` (the module dir — tests import `graphstore`, `gather`, etc. as top-level modules).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.claude/skills/activity-overview/graphstore.py` | substrate: schema, nodes/edges, traversal | **Modify** — add `dead_refs` table + `record_dead_ref`/`is_dead_ref`/`get_dead_refs`; `traverse_spine(..., skip_dead=False)` |
| `.claude/skills/activity-overview/gather.py` | the only writer/network; `backfill` single-node primitive | **Modify** — add `ABSENT` sentinel; `backfill` returns `absent`; on `ABSENT` records dead ref |
| `.claude/skills/activity-overview/complete.py` | **NEW** completion policy: transitive, window-bounded BFS + honest gaps; `annotate` | **Create** |
| `.claude/skills/activity-overview/extract.py` | primary reader; window → bundle view | **Modify** — replace inline 7c loop with `complete.complete_train` |
| `.claude/skills/activity-overview/spotlight.py` | second reader; four analytics queries + renders | **Modify** — thread `annotate` onto every `_train`; optional `complete=`/`--complete`; gap line in renders |
| `.claude/skills/activity-overview/test_complete.py` | **NEW** unit tests for `complete.py` | **Create** |
| `.claude/skills/activity-overview/test_graphstore.py` | substrate tests | **Modify** — dead_refs + skip_dead |
| `.claude/skills/activity-overview/test_backfill.py` | gather.backfill + extract wiring | **Modify** — ABSENT outcome; extract parity stays green |
| `.claude/skills/activity-overview/test_spotlight.py` | spotlight queries + renders | **Modify** — goldens gain `complete`/`gaps`; windowed-gap-with-phantom golden |

Slices map 1:1 to the spec's gated slices: **8d-1** = graphstore + gather (Tasks 1–4), **8d-2** = `complete.py` + extract refactor (Tasks 5–11), **8d-3** = spotlight + renders (Tasks 12–15).

---

## Slice 8d-1 — dead-ref memory + seam split

### Task 1: `dead_refs` table + helpers in graphstore

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py` (schema string ~`graphstore.py:64`; new helpers near `record_window` ~`graphstore.py:468`)
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Add to `test_graphstore.py`:

```python
class TestDeadRefs(unittest.TestCase):
    def setUp(self):
        self.conn = _store()  # existing helper: open_store(:memory:) + init_schema

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
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_graphstore.py::TestDeadRefs -v`
Expected: FAIL — `AttributeError: module 'graphstore' has no attribute 'is_dead_ref'` (and the table assertion fails).

- [ ] **Step 3: Add the table to `_CORE_SCHEMA`**

In `graphstore.py`, append to the `_CORE_SCHEMA` string (just before its closing `"""` at `graphstore.py:67`):

```sql
CREATE TABLE IF NOT EXISTS dead_refs (
    id         TEXT PRIMARY KEY,
    project    TEXT,
    reason     TEXT,
    first_seen TEXT
);
```

- [ ] **Step 4: Add the helpers**

In `graphstore.py`, after `record_window` (~`graphstore.py:496`), add:

```python
def record_dead_ref(conn, id, reason="absent"):
    """Tombstone a qualified id known not to exist (a 404 phantom). Idempotent:
    re-recording the same id is a no-op (first_seen is preserved). We never
    destructively delete the dangling edge — this just stops traversal from
    re-surfacing it. `project` is recovered from the id's scope when present."""
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
    row = conn.execute("SELECT 1 FROM dead_refs WHERE id=?", (id,)).fetchone()
    return row is not None


def get_dead_refs(conn):
    """All tombstoned ids, sorted (deterministic)."""
    return [r[0] for r in conn.execute("SELECT id FROM dead_refs ORDER BY id")]
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `python -m pytest test_graphstore.py::TestDeadRefs -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): dead-ref tombstone table + helpers (8d-1)"
```

---

### Task 2: `traverse_spine(skip_dead=)` drops tombstoned ids from `missing`

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py:394` (`traverse_spine`)
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Add to `class TestDeadRefs` in `test_graphstore.py`:

```python
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
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_graphstore.py::TestDeadRefs::test_traverse_spine_skip_dead_omits_tombstoned_missing -v`
Expected: FAIL — `traverse_spine() got an unexpected keyword argument 'skip_dead'`.

- [ ] **Step 3: Add the `skip_dead` parameter**

In `graphstore.py`, change the signature at line 394:

```python
def traverse_spine(conn, seed_ids, max_depth=6, edge_types=SPINE_EDGE_TYPES,
                   skip_dead=False):
```

Then, where `missing` is computed (currently `graphstore.py:434`):

```python
    missing = [i for i in reached if i not in present]
    if skip_dead and missing:
        missing = [i for i in missing if not is_dead_ref(conn, i)]
    return {"reached": reached, "missing": missing}
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python -m pytest test_graphstore.py -v`
Expected: PASS (all `TestDeadRefs` + every existing graphstore test — default behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): traverse_spine(skip_dead=) prunes tombstoned refs (8d-1)"
```

---

### Task 3: `gather.ABSENT` sentinel + `backfill` records dead refs

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:1857` (`backfill`) + a module-level sentinel near `gather.py:1827`
- Test: `.claude/skills/activity-overview/test_backfill.py`

- [ ] **Step 1: Write the failing test**

Add to `test_backfill.py` (the `FakeFetcher` there returns `None` for unknown ids; extend a test to return `ABSENT`):

```python
class BackfillAbsent(unittest.TestCase):
    def test_absent_records_dead_ref_and_reports_absent(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        iid = graphstore.qualify_id("acme", "widget", "issue-123")  # never existed

        # A fetcher that reports this id as a definitive 404.
        def fetch(kind, local, qid):
            return gather.ABSENT

        res = gather.backfill(conn, iid, fetch=fetch)
        self.assertFalse(res["fetched"])
        self.assertTrue(res["absent"])
        self.assertTrue(graphstore.is_dead_ref(conn, iid))  # remembered dead
        self.assertIsNone(graphstore.get_node(conn, iid))    # not upserted

    def test_unreachable_none_is_not_absent(self):
        conn = _store()
        gather.fold_bundle(conn, copy.deepcopy(_cross_window_bundle()))
        iid = graphstore.qualify_id("acme", "widget", "issue-7")

        def fetch(kind, local, qid):
            return None  # transient / unreachable, NOT a 404

        res = gather.backfill(conn, iid, fetch=fetch)
        self.assertFalse(res["fetched"])
        self.assertFalse(res["absent"])
        self.assertFalse(graphstore.is_dead_ref(conn, iid))  # NOT tombstoned
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_backfill.py::BackfillAbsent -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'ABSENT'`.

- [ ] **Step 3: Add the sentinel + handle it in `backfill`**

In `gather.py`, add a module-level sentinel just above `def backfill` (~`gather.py:1856`):

```python
# Returned by a `fetch` seam to mean "this id DEFINITIVELY does not exist" (a
# 404) — distinct from None ("couldn't reach it / transient"). backfill prunes
# an ABSENT id by tombstoning it via graphstore.record_dead_ref.
ABSENT = object()
```

Then in `backfill` (`gather.py:1884`), replace the single fetch+guard block:

```python
    result = fetch(info["kind"], info["local"], id)
    if result is ABSENT:
        graphstore.record_dead_ref(conn, id)  # only gather writes
        return {"fetched": False, "absent": True, "id": id, "edges_added": 0}
    if not result or not result.get("node"):
        return {"fetched": False, "absent": False, "id": id, "edges_added": 0}
```

And update the final success return (`gather.py:1916`) to carry `absent`:

```python
    return {"fetched": True, "absent": False, "id": id, "edges_added": len(edges)}
```

Also update the early "already present" return (`gather.py:1872`) to include the key, so callers can rely on it always being present:

```python
    if graphstore.get_node(conn, id) is not None:
        return {"fetched": False, "absent": False, "id": id, "edges_added": 0}
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python -m pytest test_backfill.py -v`
Expected: PASS — `BackfillAbsent` (2) plus every existing `test_backfill.py` test (the extra `absent` key is additive; existing assertions check `res["fetched"]` only).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_backfill.py
git commit -m "feat(activity): gather.ABSENT sentinel; backfill tombstones 404s (8d-1)"
```

---

### Task 4: Production fetch seam returns `ABSENT` on a real 404

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:1930` (`make_backfill_fetcher`'s inner `fetch`)
- Test: none (this is the live-network edge, not unit-tested — per its existing docstring at `gather.py:1923`). Verified indirectly by the `ABSENT` unit path in Task 3.

> **Reality check (do this first):** `http_get_json(url, token)` returns `(parsed_json, next_url)` and **raises `SystemExit` on ANY `HTTPError`, including 404** (`gather.py:1502-1520`) — it does not surface a status. So a phantom would currently *abort the process*. We add a 404-tolerant path rather than parse the error string.

- [ ] **Step 1: Add a 404-tolerant path to `http_get_json`**

In `gather.py`, change `http_get_json` (`gather.py:1502`) to accept `allow_404=False` and, only when asked, return the status instead of exiting on a 404:

```python
def http_get_json(url, token, allow_404=False):
    """GET a GitHub API URL → (parsed_json, next_url). Not unit-tested.

    With allow_404=True a 404 returns (None, 404) instead of raising, so the
    backfill seam can map a definitively-absent ref to ABSENT. All other HTTP
    errors still raise SystemExit with the diagnostic.
    """
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "activity-overview",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            nxt = _next_link(resp.headers.get("Link", ""))
        return body, nxt
    except urllib.error.HTTPError as err:
        if allow_404 and err.code == 404:
            return None, 404
        raise SystemExit(_format_http_error(url, err)) from err
```

- [ ] **Step 2: Map a 404 to `ABSENT` in the fetch seam**

In `make_backfill_fetcher`'s inner `fetch` (`gather.py:1930`), replace the issue branch (`gather.py:1934`):

```python
        if kind == "social" and local.startswith("issue-"):
            num = local[len("issue-"):]
            raw, nxt = http_get_json(f"{api}/issues/{num}", token, allow_404=True)
            if raw is None and nxt == 404:
                return ABSENT
            if raw.get("pull_request"):
                return None
            return {"node": normalize_issue(raw), "edges": []}
```

And the PR branch (`gather.py:1940`):

```python
        if kind == "social" and local.startswith("pr-"):
            num = local[len("pr-"):]
            raw, nxt = http_get_json(f"{api}/pulls/{num}", token, allow_404=True)
            if raw is None and nxt == 404:
                return ABSENT
            pr = normalize_pr(raw)
            edges = [("issue-{}".format(n), "closes") for n in pr.get("closes") or []]
            return {"node": pr, "edges": edges}
```

> The `code` branch keeps returning `None` on a failed `git fetch` — a missing sha is "unreachable", not a verified-absent social ref; only social 404s are definitive phantoms. Other `http_get_json` callers are unaffected: `allow_404` defaults to `False`, so their behaviour is byte-identical.

- [ ] **Step 3: Verify nothing else broke**

Run: `python -m pytest test_backfill.py test_gather.py -v`
Expected: PASS (no test exercises the live seam; this confirms no import/syntax regressions).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/activity-overview/gather.py
git commit -m "feat(activity): production fetch seam maps REST 404 to ABSENT (8d-1)"
```

---

## Slice 8d-2 — `complete.py` orchestrator + extract refactor

### Task 5: `complete.py` skeleton + offline `not_gathered` path

**Files:**
- Create: `.claude/skills/activity-overview/complete.py`
- Create: `.claude/skills/activity-overview/test_complete.py`

- [ ] **Step 1: Write the failing test**

Create `test_complete.py`:

```python
import unittest

import complete
import gather
import graphstore


def _store():
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    return conn


def _pr_closes_issue(conn, pr_local="pr-10", issue_local="issue-7",
                     pr_ts="2026-03-15T00:00:00Z"):
    """Seed an in-window PR that `closes` an absent (missing) issue. Returns
    (pr_id, issue_id)."""
    pr = graphstore.qualify_id("acme", "widget", pr_local)
    issue = graphstore.qualify_id("acme", "widget", issue_local)
    graphstore.upsert_node(conn, pr, "acme", "widget", "social", pr_ts,
                           {"number": int(pr_local.split("-")[1])})
    graphstore.upsert_edge(conn, pr, issue, "closes")
    return pr, issue


class CompleteOffline(unittest.TestCase):
    def test_no_fetcher_reports_missing_as_not_gathered(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        self.assertEqual(res["fetched"], 0)
        self.assertEqual(res["gaps"], [{"id": issue, "reason": "not_gathered"}])
        # reached is unchanged (nothing fetched) and is a set of ids.
        self.assertIn(pr, res["reached"])

    def test_dead_ref_is_pruned_not_reported(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)
        graphstore.record_dead_ref(conn, issue)  # known phantom
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        self.assertEqual(res["gaps"], [])          # phantom NOT a gap
        self.assertEqual(res["fetched"], 0)

    def test_gaps_sorted_by_id(self):
        conn = _store()
        _pr_closes_issue(conn, "pr-10", "issue-9")
        _pr_closes_issue(conn, "pr-11", "issue-1")
        seeds = [graphstore.qualify_id("acme", "widget", x)
                 for x in ("pr-10", "pr-11")]
        reach = graphstore.traverse_spine(conn, seeds)
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=None)
        ids = [g["id"] for g in res["gaps"]]
        self.assertEqual(ids, sorted(ids))
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_complete.py::CompleteOffline -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'complete'`.

- [ ] **Step 3: Create `complete.py` with the offline path**

Create `complete.py`:

```python
"""Phase 8d — the train-completion orchestrator.

ONE home for completion policy: given a train's reached set and its `missing`
spine refs, decide which to fill, follow the causal spine TRANSITIVELY to the
query-window bound, and return a completed reached set plus an honest gap list.

Pure policy: reads the store via `graphstore`, drives the INJECTED `backfill`
seam (so the offline suite makes zero network calls), never imports a network
library, never writes (the write is gather.backfill's). gather stays the only
writer/network.
"""
import graphstore


def _in_window(node, window):
    """A node is in-window if there is no window, or its ts falls in [from, to].
    A node with no ts is treated as out-of-window (cannot prove it's inside)."""
    if window is None:
        return True
    ts = node.get("ts")
    if ts is None:
        return False
    frm, to = window
    return frm <= ts <= to


def _spine_neighbors(conn, qid, edge_types):
    """Present neighbor nodes of `qid` over the spine allowlist, both directions.
    Used to decide whether a missing id's REFERRER is in-window."""
    nbr_ids = set()
    for e in graphstore.get_edges(conn, qid, direction="out", edge_types=edge_types):
        nbr_ids.add(e["dst_id"])
    for e in graphstore.get_edges(conn, qid, direction="in", edge_types=edge_types):
        nbr_ids.add(e["src_id"])
    out = []
    for nid in nbr_ids:
        n = graphstore.get_node(conn, nid)
        if n is not None:
            out.append(n)
    return out


def complete_train(conn, reached, missing, *, window=None, backfill=None,
                   budget=50, edge_types=graphstore.SPINE_EDGE_TYPES, warn=None):
    """Transitively complete a train. Returns
    {"reached": set, "gaps": [{"id", "reason"}], "fetched": int}.

    - `backfill=None` (offline): every non-dead missing id becomes a
      `not_gathered` gap; phantoms already tombstoned are pruned. No fetch.
    - `window=None`: chase the spine to its connected-component closure.
    - reasons: not_gathered | outside_window | unreachable | budget.
    Determinism: missing processed in sorted id order; gaps sorted by id.
    """
    reached = set(reached)
    gaps = {}            # id -> reason (last write wins; all reasons terminal)
    fetched = 0

    def referrer_in_window(mid):
        if window is None:
            return True
        for nb in _spine_neighbors(conn, mid, edge_types):
            if nb["id"] in reached and _in_window(nb, window):
                return True
        return False

    work = sorted(missing)
    seen = set(work)
    while work:
        progressed = False
        for mid in list(work):
            work.remove(mid)
            if graphstore.is_dead_ref(conn, mid):
                continue                              # phantom: pruned, no gap
            if not referrer_in_window(mid):
                gaps[mid] = "outside_window"
                continue
            if backfill is None:
                gaps[mid] = "not_gathered"
                continue
            if fetched >= budget:
                gaps[mid] = "budget"
                continue
            res = backfill(conn, mid)
            fetched += 1
            if res.get("absent"):
                continue                              # tombstoned by gather: prune
            if not res.get("fetched"):
                gaps[mid] = "unreachable"
                continue
            gaps.pop(mid, None)
            progressed = True
        if progressed:
            # Re-traverse from everything reached so far: a backfilled node may
            # reference further missing spine nodes. skip_dead drops phantoms.
            reach = graphstore.traverse_spine(
                conn, sorted(reached) + sorted(seen), edge_types=edge_types,
                skip_dead=True)
            reached = set(reach["reached"])
            for nm in reach["missing"]:
                if nm not in seen and nm not in gaps:
                    seen.add(nm)
                    work.append(nm)
            work = sorted(work)
        else:
            break

    if warn and gaps:
        warn("complete: {} gap(s): {}".format(
            len(gaps), ", ".join("{}({})".format(i, r)
                                 for i, r in sorted(gaps.items()))))
    return {
        "reached": reached,
        "gaps": [{"id": i, "reason": r} for i, r in sorted(gaps.items())],
        "fetched": fetched,
    }
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python -m pytest test_complete.py::CompleteOffline -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/complete.py .claude/skills/activity-overview/test_complete.py
git commit -m "feat(activity): complete.py orchestrator — offline not_gathered path (8d-2)"
```

---

### Task 6: Transitive reach + window bound (`outside_window`)

**Files:**
- Modify: `.claude/skills/activity-overview/test_complete.py`
- Verify: `.claude/skills/activity-overview/complete.py` (no change expected; this proves the BFS + window logic from Task 5)

- [ ] **Step 1: Write the failing test**

Add to `test_complete.py`:

```python
class CompleteTransitive(unittest.TestCase):
    def _three_deep(self, conn):
        """In-window PR #10 -> closes issue #7 (absent, OUT of window) ->
        spun_off from RFC #5 (absent). A fetcher provides #7 and #5."""
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        issue = graphstore.qualify_id("acme", "widget", "issue-7")
        rfc = graphstore.qualify_id("acme", "widget", "issue-5")
        graphstore.upsert_node(conn, pr, "acme", "widget", "social",
                               "2026-06-15T00:00:00Z", {"number": 10})
        graphstore.upsert_edge(conn, pr, issue, "closes")

        def backfill(c, mid):
            if mid == issue:
                # issue #7 closed BEFORE the window; it cites RFC #5.
                graphstore.upsert_node(c, issue, "acme", "widget", "social",
                                       "2026-01-01T00:00:00Z", {"number": 7})
                graphstore.upsert_edge(c, issue, rfc, "spun_off")
                return {"fetched": True, "absent": False, "id": mid}
            if mid == rfc:
                graphstore.upsert_node(c, rfc, "acme", "widget", "social",
                                       "2025-06-01T00:00:00Z", {"number": 5})
                return {"fetched": True, "absent": False, "id": mid}
            return {"fetched": False, "absent": False, "id": mid}

        return pr, issue, rfc, backfill

    def test_level0_anchor_filled_regardless_of_its_date(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        window = ("2026-06-01", "2026-06-30")
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=window,
            backfill=backfill)
        # issue #7 is a level-0 anchor (referenced by in-window PR #10): filled
        # even though it closed in January.
        self.assertIn(issue, res["reached"])

    def test_transitive_ref_outside_window_is_a_gap_not_chased(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        window = ("2026-06-01", "2026-06-30")
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=window,
            backfill=backfill)
        # RFC #5 is referenced ONLY by issue #7, which landed OUTSIDE the window
        # -> it is a context boundary: #5 is an outside_window gap, never fetched.
        self.assertNotIn(rfc, res["reached"])
        self.assertIn({"id": rfc, "reason": "outside_window"}, res["gaps"])
        self.assertEqual(res["fetched"], 1)  # only issue #7 was fetched

    def test_no_window_chases_to_closure(self):
        conn = _store()
        pr, issue, rfc, backfill = self._three_deep(conn)
        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], window=None,
            backfill=backfill)
        # No window -> no boundary: both #7 and #5 are pulled in, no gaps.
        self.assertIn(issue, res["reached"])
        self.assertIn(rfc, res["reached"])
        self.assertEqual(res["gaps"], [])
        self.assertEqual(res["fetched"], 2)
```

- [ ] **Step 2: Run it**

Run: `python -m pytest test_complete.py::CompleteTransitive -v`
Expected: PASS — the Task 5 implementation already encodes level-0 fill, window-bounded transitive expansion, and closure. If `test_transitive_ref_outside_window_is_a_gap_not_chased` fails, the `referrer_in_window` check is the culprit: confirm a freshly-backfilled out-of-window node (issue #7) is in `reached` (so it is a candidate referrer) but `_in_window` returns False for it, making RFC #5 unfillable.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/test_complete.py
git commit -m "test(activity): transitive reach + window-bound gaps (8d-2)"
```

---

### Task 7: `unreachable` and `budget` gap reasons

**Files:**
- Modify: `.claude/skills/activity-overview/test_complete.py`

- [ ] **Step 1: Write the failing test**

Add to `test_complete.py`:

```python
class CompleteGapReasons(unittest.TestCase):
    def test_unreachable_when_fetch_returns_not_fetched(self):
        conn = _store()
        pr, issue = _pr_closes_issue(conn)

        def backfill(c, mid):
            return {"fetched": False, "absent": False, "id": mid}  # transient

        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill)
        self.assertEqual(res["gaps"], [{"id": issue, "reason": "unreachable"}])
        self.assertEqual(res["fetched"], 1)

    def test_budget_caps_fetches_and_marks_rest_budget(self):
        conn = _store()
        # Three in-window PRs, each closing a distinct missing issue.
        for prl, isl in (("pr-10", "issue-7"), ("pr-11", "issue-8"),
                         ("pr-12", "issue-9")):
            _pr_closes_issue(conn, prl, isl)
        seeds = [graphstore.qualify_id("acme", "widget", x)
                 for x in ("pr-10", "pr-11", "pr-12")]

        def backfill(c, mid):
            graphstore.upsert_node(c, mid, "acme", "widget", "social",
                                   "2026-03-10T00:00:00Z", {"id": mid})
            return {"fetched": True, "absent": False, "id": mid}

        reach = graphstore.traverse_spine(conn, seeds)
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill,
            budget=2)
        self.assertEqual(res["fetched"], 2)               # ceiling respected
        budget_gaps = [g for g in res["gaps"] if g["reason"] == "budget"]
        self.assertEqual(len(budget_gaps), 1)             # the third is a gap
```

- [ ] **Step 2: Run it**

Run: `python -m pytest test_complete.py::CompleteGapReasons -v`
Expected: PASS (the Task 5 implementation already produces both reasons).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/test_complete.py
git commit -m "test(activity): unreachable + budget gap reasons (8d-2)"
```

---

### Task 8: Phantom pruning via `ABSENT` + dead-ref memory (no re-fetch)

**Files:**
- Modify: `.claude/skills/activity-overview/test_complete.py`

- [ ] **Step 1: Write the failing test**

Add to `test_complete.py`:

```python
class CompletePhantomMemory(unittest.TestCase):
    def test_absent_pruned_recorded_and_not_refetched(self):
        conn = _store()
        # PR #10 has a `Fixes #123` template placeholder that never existed.
        pr = graphstore.qualify_id("acme", "widget", "pr-10")
        phantom = graphstore.qualify_id("acme", "widget", "issue-123")
        graphstore.upsert_node(conn, pr, "acme", "widget", "social",
                               "2026-03-15T00:00:00Z", {"number": 10})
        graphstore.upsert_edge(conn, pr, phantom, "closes")

        calls = []

        def backfill(c, mid):
            calls.append(mid)
            # Drive the real gather path so the dead-ref is recorded by gather.
            return gather.backfill(c, mid, fetch=lambda k, l, q: gather.ABSENT)

        reach = graphstore.traverse_spine(conn, [pr])
        res = complete.complete_train(
            conn, reach["reached"], reach["missing"], backfill=backfill)
        # phantom is NOT a gap, but it WAS tombstoned.
        self.assertEqual(res["gaps"], [])
        self.assertTrue(graphstore.is_dead_ref(conn, phantom))
        self.assertEqual(calls, [phantom])  # fetched exactly once

        # A SECOND completion does not re-fetch it (is_dead_ref short-circuits).
        calls.clear()
        reach2 = graphstore.traverse_spine(conn, [pr], skip_dead=True)
        res2 = complete.complete_train(
            conn, reach2["reached"], reach2["missing"], backfill=backfill)
        self.assertEqual(calls, [])         # never re-chased
        self.assertEqual(res2["gaps"], [])
```

- [ ] **Step 2: Run it**

Run: `python -m pytest test_complete.py::CompletePhantomMemory -v`
Expected: PASS — `complete_train` skips `is_dead_ref` ids before fetching, and the first pass tombstones via `gather.backfill`'s `ABSENT` handling (Task 3).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/test_complete.py
git commit -m "test(activity): phantom pruning + dead-ref memory in complete (8d-2)"
```

---

### Task 9: `annotate()` stamps the honest contract onto a train dict

**Files:**
- Modify: `.claude/skills/activity-overview/complete.py`
- Modify: `.claude/skills/activity-overview/test_complete.py`

- [ ] **Step 1: Write the failing test**

Add to `test_complete.py`:

```python
class Annotate(unittest.TestCase):
    def test_complete_true_when_no_gaps(self):
        t = {"anchor": "x"}
        complete.annotate(t, {"reached": {"x"}, "gaps": [], "fetched": 0})
        self.assertEqual(t["complete"], True)
        self.assertEqual(t["gaps"], [])

    def test_complete_false_lists_sorted_gaps(self):
        t = {"anchor": "x"}
        result = {"reached": {"x"},
                  "gaps": [{"id": "b", "reason": "not_gathered"},
                           {"id": "a", "reason": "outside_window"}],
                  "fetched": 0}
        complete.annotate(t, result)
        self.assertEqual(t["complete"], False)
        self.assertEqual([g["id"] for g in t["gaps"]], ["a", "b"])
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_complete.py::Annotate -v`
Expected: FAIL — `AttributeError: module 'complete' has no attribute 'annotate'`.

- [ ] **Step 3: Add `annotate`**

Append to `complete.py`:

```python
def annotate(train, result):
    """Stamp the honest edge contract onto a `_train` dict in place:
    `complete` (no unresolved spine refs) + `gaps` (sorted by id). Returns the
    train for chaining."""
    gaps = sorted(result.get("gaps", []), key=lambda g: g["id"])
    train["complete"] = not gaps
    train["gaps"] = gaps
    return train
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python -m pytest test_complete.py -v`
Expected: PASS (all `complete` tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/complete.py .claude/skills/activity-overview/test_complete.py
git commit -m "feat(activity): complete.annotate stamps complete/gaps (8d-2)"
```

---

### Task 10: Refactor `extract` onto `complete.complete_train` (parity)

**Files:**
- Modify: `.claude/skills/activity-overview/extract.py:133-199`
- Verify: `.claude/skills/activity-overview/test_backfill.py` (the existing 7c suite must stay green — this is the parity gate)

- [ ] **Step 1: Run the existing suite to capture the green baseline**

Run: `python -m pytest test_backfill.py -v`
Expected: PASS — record the passing test names (`ExtractBackfillWiring`, `RollupResumeHardening`, etc.). These must remain green byte-for-byte after the refactor.

- [ ] **Step 2: Replace the inline 7c loop with a `complete_train` call**

In `extract.py`, add `import complete` at the top with the other imports. Then replace the block at `extract.py:164-199` (from `spine = graphstore.traverse_spine(...)` through the budget/missing warnings) with:

```python
    spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth,
                                      skip_dead=True)

    # 2b. Complete the train via the shared orchestrator (slice 7c, now 8d).
    #     window=None preserves 7c semantics: extract fills the whole reachable
    #     spine (level-0 + transitive) within budget, exactly as the inline loop
    #     did. backfill=None (default) is byte-identical warn-only behaviour.
    result = complete.complete_train(
        conn, spine["reached"], spine["missing"], window=None,
        backfill=backfill, budget=backfill_budget, warn=warn)
    if backfill is not None:
        # re-read the spine so the materialization below sees backfilled nodes
        spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth,
                                          skip_dead=True)

    # Honest warnings for anything still unresolved (parity with the old path).
    real_gaps = [g["id"] for g in result["gaps"]]
    budget_gaps = [g["id"] for g in result["gaps"] if g["reason"] == "budget"]
    if budget_gaps:
        warn("extract: backfill budget ({}) exhausted; {} spine context "
             "node(s) left un-fetched: {}".format(
                 backfill_budget, len(budget_gaps), ", ".join(sorted(budget_gaps))))
    if real_gaps:
        warn("extract: {} spine context node(s) referenced but not stored: "
             "{}".format(len(real_gaps), ", ".join(sorted(real_gaps))))
```

> Keep the existing `_context_ids = set(spine["reached"]) - in_window_ids` line below it (`extract.py:203`) unchanged.

- [ ] **Step 3: Run the parity suite**

Run: `python -m pytest test_backfill.py test_extract.py test_characterization.py -v`
Expected: PASS — all green. If `test_default_backfill_none_is_warn_only_unchanged` or the budget test fails, diff the emitted `warn` strings: the messages above are copied verbatim from the old loop (`extract.py:191-199`), and `complete_train(backfill=None)` yields one `not_gathered` gap per missing id, so `real_gaps` equals the old `spine["missing"]`.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/activity-overview/extract.py
git commit -m "refactor(activity): extract uses complete.complete_train (8d-2)"
```

---

### Task 11: Full slice-8d-2 regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the whole skill suite + validator**

Run: `python -m pytest . -q` (from `.claude/skills/activity-overview/`)
Then: `python validate.py` if it is a standalone trust gate (check `--help`), else `python -m pytest test_validate.py -v`.
Expected: PASS — no regressions. `complete.py` is additive; `extract` parity holds; the `dead_refs` table is ignored by the validator.

- [ ] **Step 2: Commit (only if a fixup was needed)**

```bash
git add -A && git commit -m "test(activity): green slice 8d-2 regression gate"
```

---

## Slice 8d-3 — spotlight completion + renders

### Task 12: Thread `complete.annotate` onto every spotlight `_train` (offline default)

**Files:**
- Modify: `.claude/skills/activity-overview/spotlight.py` (the four query builders that call `traverse_spine` at `spotlight.py:407,757,878` and `_train` at `spotlight.py:208`; pass completion params down)
- Modify: `.claude/skills/activity-overview/test_spotlight.py` (goldens gain `complete`/`gaps`)

- [ ] **Step 1: Write the failing test**

Add to `test_spotlight.py` (near `TestPersonImpact`):

```python
class TestHonestContract(unittest.TestCase):
    def setUp(self):
        self.conn = _store()
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_every_train_carries_complete_and_gaps(self):
        res = spotlight.person_impact(self.conn, "r1", "alice")
        self.assertTrue(res.get("delivered"))
        for t in res["delivered"]:
            self.assertIn("complete", t)
            self.assertIn("gaps", t)
            self.assertIsInstance(t["gaps"], list)

    def test_offline_default_makes_no_network_call(self):
        # No fetcher passed -> complete=None path -> trains complete-or-not from
        # the store alone; a fully-stored train is complete:true.
        res = spotlight.person_impact(self.conn, "r1", "alice")
        complete_flags = [t["complete"] for t in res["delivered"]]
        self.assertTrue(all(isinstance(f, bool) for f in complete_flags))
```

> Use the module's existing `_store()`/`_crafted_bundle()` helpers (`test_spotlight.py:25`). `_crafted_bundle` produces self-contained trains, so they should be `complete: true` offline.

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_spotlight.py::TestHonestContract -v`
Expected: FAIL — `KeyError: 'complete'` (trains don't carry the contract yet).

- [ ] **Step 3: Thread `annotate` through `_train`**

In `spotlight.py`, add `import complete` at the top. Give `_train` (`spotlight.py:208`) two optional params and annotate before returning:

```python
def _train(conn, anchor, reached, focus_touch_ids, role_of,
           *, missing=None, window=None, backfill=None, complete_budget=50):
```

At the end of `_train`, just before its `return {...}`, build the result dict and annotate:

```python
    train = { ... existing fields ... }
    comp = complete.complete_train(
        conn, reached, missing or [], window=window, backfill=backfill,
        budget=complete_budget, edge_types=_CAUSAL_SPINE)
    complete.annotate(train, comp)
    return train
```

At each of the four call sites that build trains (`spotlight.py:407`, `:757`, `:878`, and the subsystem builder), capture `missing` from the traversal and pass it plus the query window through. For the person-impact builder (`spotlight.py:407`):

```python
        reach = graphstore.traverse_spine(conn, [seed], edge_types=_CAUSAL_SPINE,
                                          skip_dead=True)
        reached = set(reach["reached"])
        ...
        t = _train(conn, seed, reached, focus_touch_ids, role_of,
                   missing=reach["missing"],
                   window=(ts_from, ts_to) if (ts_from or ts_to) else None,
                   backfill=backfill, complete_budget=complete_budget)
```

Apply the same `missing=`/`window=`/`backfill=` threading at the other three builders. Add `backfill=None, complete_budget=50` to each public query signature (`person_impact` `spotlight.py:329`, and the subsystem/pattern/text builders) so the offline default holds.

- [ ] **Step 4: Run the new test + the goldens**

Run: `python -m pytest test_spotlight.py::TestHonestContract -v`
Expected: PASS.
Run: `python -m pytest test_spotlight.py -v`
Expected: Several golden envelope tests (e.g. `TestPersonImpact::test_delivered_train`, `TestSubsystemSplit::test_delivered_trains`, `TestPatternEvolution::test_delivered_single_lifecycle_train`) FAIL on a dict-equality mismatch — the trains now carry the additive `complete`/`gaps` keys. This is the rev's expected, justified golden churn.

- [ ] **Step 5: Update the affected goldens**

For each failing golden assertion that compares a whole train dict, add `"complete": True, "gaps": []` to the expected train (the `_crafted_bundle`/`_subsystem_store`/`_evolution_*` fixtures are self-contained, so their trains are complete offline). For assertions that check a subset of fields, no change is needed. Re-run after each edit:

Run: `python -m pytest test_spotlight.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/spotlight.py .claude/skills/activity-overview/test_spotlight.py
git commit -m "feat(activity): spotlight trains carry honest complete/gaps (8d-3)"
```

---

### Task 13: `--complete` CLI flag wires the production fetcher

**Files:**
- Modify: `.claude/skills/activity-overview/spotlight.py:1091` (`main`) + the dispatch that calls each query
- Modify: `.claude/skills/activity-overview/test_spotlight.py` (`TestCLI`)

- [ ] **Step 1: Write the failing test**

Add to `class TestCLI` in `test_spotlight.py`:

```python
    def test_complete_flag_accepted_offline(self):
        # --complete without a token still parses and runs offline (no network):
        # an absent GITHUB_TOKEN means no fetcher is built, trains stay honest.
        out = self._run("person", "alice", "--json")
        self.assertIn('"complete"', out)

    def test_complete_flag_present_in_help(self):
        with self.assertRaises(SystemExit):
            spotlight.main(["--help"])
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_spotlight.py::TestCLI::test_complete_flag_accepted_offline -v`
Expected: FAIL — the JSON has no `complete` key only if Task 12 shipped; if Task 12 is in, this passes already for the field, but the `--complete` arg is still unparsed. Add the flag regardless.

- [ ] **Step 3: Add the `--complete` flag + wire the fetcher**

In `main` (`spotlight.py:1091`), after the existing `--to` argument (`spotlight.py:1104`):

```python
    parser.add_argument("--complete", action="store_true",
                        help="fetch missing cross-window spine anchors via the "
                             "GitHub API (needs GITHUB_TOKEN); default is "
                             "offline/honest-only")
    parser.add_argument("--complete-budget", type=int, default=50,
                        help="max nodes to backfill per train when --complete")
```

Then, where the query is dispatched, build the backfill seam only when `--complete` and a token are present:

```python
    backfill = None
    if args.complete:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            fetch = gather.make_backfill_fetcher(token)
            backfill = lambda c, mid: gather.backfill(c, mid, fetch=fetch)  # noqa: E731
        else:
            sys.stderr.write("spotlight: --complete needs GITHUB_TOKEN; "
                             "running offline (honest-only)\n")
    # pass backfill + args.complete_budget into the selected query call
```

Add `import os` and `import gather` to the top of `spotlight.py` if not already imported (check the existing import block).

- [ ] **Step 4: Run the tests**

Run: `python -m pytest test_spotlight.py::TestCLI -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/spotlight.py .claude/skills/activity-overview/test_spotlight.py
git commit -m "feat(activity): spotlight --complete wires production backfill (8d-3)"
```

---

### Task 14: Gap line in the markdown renderers

**Files:**
- Modify: `.claude/skills/activity-overview/spotlight.py:929` (`_render_train_md`)
- Modify: `.claude/skills/activity-overview/test_spotlight.py`

- [ ] **Step 1: Write the failing test**

Add to `test_spotlight.py`:

```python
class TestGapRender(unittest.TestCase):
    def test_complete_train_renders_no_gap_line(self):
        t = {"anchor": "a", "title": "x", "outcome": "shipped",
             "key_date": "2026-03-01", "areas": [], "roles": [],
             "touchpoints": [], "timeline": [], "complete": True, "gaps": []}
        md = spotlight._render_train_md(t)
        self.assertNotIn("gaps", md.lower())

    def test_gappy_train_renders_compact_summary(self):
        t = {"anchor": "a", "title": "x", "outcome": "shipped",
             "key_date": "2026-03-01", "areas": [], "roles": [],
             "touchpoints": [], "timeline": [], "complete": False,
             "gaps": [{"id": "acme/w#issue-5", "reason": "outside_window"},
                      {"id": "acme/w#issue-9", "reason": "not_gathered"}]}
        md = spotlight._render_train_md(t)
        self.assertIn("2 gaps", md)
        self.assertIn("outside-window", md)
        self.assertIn("not-gathered", md)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest test_spotlight.py::TestGapRender -v`
Expected: FAIL — the renderer emits no gap line.

- [ ] **Step 3: Add a deterministic gap line to `_render_train_md`**

In `_render_train_md` (`spotlight.py:929`), before the function returns its joined lines, append:

```python
    gaps = t.get("gaps") or []
    if gaps:
        from collections import Counter
        counts = Counter(g["reason"] for g in gaps)
        parts = ["{} {}".format(counts[r], r.replace("_", "-"))
                 for r in sorted(counts)]
        lines.append("> ⚠ {} gaps: {}".format(len(gaps), ", ".join(parts)))
```

> Match the existing local variable name for the accumulated lines in `_render_train_md` (it builds a `lines` list — confirm at `spotlight.py:929`). Sorting the reasons + `Counter` keeps the line byte-deterministic.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest test_spotlight.py::TestGapRender test_spotlight.py::TestGrepRender -v`
Expected: PASS — new tests pass and existing render goldens are unaffected (complete trains emit no extra line).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/spotlight.py .claude/skills/activity-overview/test_spotlight.py
git commit -m "feat(activity): spotlight renders a compact honest gap line (8d-3)"
```

---

### Task 15: Windowed-gap-with-phantom golden + full regression + real-data smoke

**Files:**
- Modify: `.claude/skills/activity-overview/test_spotlight.py`

- [ ] **Step 1: Write the integrated golden test**

Add to `test_spotlight.py`:

```python
class TestWindowedGapWithPhantom(unittest.TestCase):
    def test_person_query_reports_outside_window_and_prunes_phantom(self):
        conn = _store()
        # alice's in-window PR #10 closes out-of-window issue #7 (a real anchor,
        # provided by the fetcher) AND has a `Fixes #123` phantom (ABSENT).
        pr = graphstore.qualify_id("r1", "r1", "pr-10")
        issue = graphstore.qualify_id("r1", "r1", "issue-7")
        phantom = graphstore.qualify_id("r1", "r1", "issue-123")
        rfc = graphstore.qualify_id("r1", "r1", "issue-5")
        person = graphstore.qualify_person("r1", "alice")
        graphstore.upsert_node(conn, pr, "r1", "r1", "social",
                               "2026-06-10T00:00:00Z", {"number": 10})
        graphstore.upsert_node(conn, person, "r1", "r1", "structure", None,
                               {"login": "alice"})
        graphstore.upsert_edge(conn, person, pr, "authored")
        graphstore.upsert_edge(conn, pr, issue, "closes")
        graphstore.upsert_edge(conn, pr, phantom, "closes")

        def fetch(kind, local, qid):
            if local == "issue-7":
                return {"node": {"number": 7, "closed_at": "2026-01-01T00:00:00Z",
                                 "state": "closed"},
                        "edges": [("issue-5", "spun_off")]}
            if local == "issue-123":
                return gather.ABSENT
            return None
        backfill = lambda c, mid: gather.backfill(c, mid, fetch=fetch)  # noqa: E731

        res = spotlight.person_impact(conn, "r1", "alice",
                                      ts_from="2026-06-01", ts_to="2026-06-30",
                                      backfill=backfill)
        train = res["delivered"][0]
        self.assertFalse(train["complete"])
        gap_ids = {g["id"]: g["reason"] for g in train["gaps"]}
        self.assertNotIn(phantom, gap_ids)             # phantom pruned, not a gap
        self.assertTrue(graphstore.is_dead_ref(conn, phantom))
        self.assertEqual(gap_ids.get(rfc), "outside_window")  # honest pointer
```

> Adjust the `person_impact` signature in Task 12 to accept `backfill=`/`complete_budget=` so this test can inject the seam. Confirm the person-query fixture wiring matches the real `_crafted_bundle` person/authored conventions (`test_spotlight.py:25`); if `person_impact` needs the person node folded a specific way, mirror `_crafted_bundle`.

- [ ] **Step 2: Run it**

Run: `python -m pytest test_spotlight.py::TestWindowedGapWithPhantom -v`
Expected: PASS.

- [ ] **Step 3: Full skill regression + CI integration gate**

Run: `python -m pytest . -q` (from the skill dir)
Run: `python ci_spotlight_integration.py` (the Phase 8 CI gate that builds a real db and runs all four queries — confirm it still exits 0)
Expected: PASS — entire suite green.

- [ ] **Step 4: Real-data `--complete` smoke (manual, gated on a token + store)**

If a real AVM store + `GITHUB_TOKEN` are available locally, run:

```bash
python spotlight.py person <login> --store <path-to.db> --from <from> --to <to> --complete --md
```

Expected: at least one cross-window anchor filled (a train that was gappy offline becomes more complete) and a coherent, small gap set in the render. If no token/store is available, note this step as deferred — it is not part of the offline CI gate.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/test_spotlight.py
git commit -m "test(activity): windowed-gap-with-phantom golden + 8d-3 gate (8d-3)"
```

---

## Self-Review (run against the spec)

**Spec coverage:**
- Honest edge contract (`complete`/`gaps` + four reasons) → Tasks 5–9, 12.
- Phantoms never gaps; pruned + remembered dead → Tasks 1–3, 8, 15.
- Transitive, window-bounded reach (level-0 always; outside_window boundary; closure) → Tasks 5–6.
- `complete.py` as shared orchestrator, injected seam, no network/writes → Tasks 5, 10, 12.
- `gather.backfill` gains `absent`/`ABSENT`, records dead → Tasks 3–4.
- `graphstore.dead_refs` + helpers + `traverse_spine(skip_dead=)` → Tasks 1–2.
- `extract` refactored, byte-identical default (7c suite green) → Task 10.
- `spotlight` four queries gain `complete`/`gaps` offline-by-default + `--complete` + gap render → Tasks 12–14.
- Updated spotlight goldens + windowed-gap-with-phantom golden → Tasks 12, 15.
- Trust gate green; additive table the auditor ignores → Tasks 11, 15.
- Slices 8d-1/8d-2/8d-3 each ship green → Tasks 4, 11, 15.

**Open spec questions (resolved as built):** (a) `budget` is a distinct gap reason (Task 7). (b) spotlight computes `complete`/`gaps` **always**, offline-by-default (Task 12) — the honesty contract holds on every read, `--complete` only shrinks gaps.

**Deferred / needs-confirmation during execution:**
- The exact `lines`-list variable name in `_render_train_md` and the precise `_train` return-dict assembly (Tasks 12, 14) — confirm against the live source when editing; the plan cites the call-site line numbers.
- `http_get_json` return shape **confirmed** `(parsed_json, next_url)` and raises `SystemExit` on 404; Task 4 adds an `allow_404` path rather than assuming a status tuple. No open risk.
- `get_edges(conn, node_id, direction, edge_types)` **confirmed** to support `direction in {"out","in","both"}` + an `edge_types` allowlist (`graphstore.py:197`), as `complete._spine_neighbors` relies on (Task 5).
</content>
</invoke>
