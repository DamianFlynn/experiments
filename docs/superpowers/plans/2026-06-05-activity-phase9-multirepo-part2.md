# Phase 9 Multi-Repo Project — Implementation Plan (Part 2: reader aggregation, S3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the digest pipeline answer about a **project** spanning several member repos: one project view whose Decision-trains span repo boundaries (including work glued only by a shared internal ticket), whose Shipped/Ownership sections aggregate the member set, and over which `validate.py` is green — without touching the byte-stable single-repo `extract`/`link`/`render` path.

**Architecture:** An **additive aggregation layer** (`digest.py`), one level above `extract`. It calls the *existing* single-repo `extract` + `link.enrich` once per member (each call is one repo, so the bundle arrays never collide), then **merges** the per-member results into one project view with `repo` as an explicit dimension: cross-repo Decision-trains are formed by **stitching** the per-member trains along the project-wide spine (the store's `closes`/`cross_ref` edges already cross repos — `traverse_spine` walks them), Shipped/Ownership are merged (people dedupe by login via the `"*"` sentinel; module ids are repo-qualified to avoid `main.tf`-vs-`main.tf` collisions), and trains that share an **internal-ticket reference** (e.g. `ABC-1234`) but no spine edge are clustered into `related_work`. No Markdown generator is added — the existing `report-template.md` + narrative step consumes the merged view (a small template update adds the project sections).

**Tech Stack:** Python 3 stdlib only (`argparse`, `json`, `re`, `sqlite3`), `unittest` under `pytest`. All work in `.claude/skills/activity-overview/`.

**Spec:** `docs/superpowers/specs/2026-06-05-activity-phase9-multirepo.md` (slice **S3**). Part 1 (S1+S2 — the multi-repo producer + cross-repo graph) is landed on `master`.

**Scope of Part 2 (this plan):** spec slice **S3** (readers + validate aggregate the member set) **plus the internal-ticket grouping** the user flagged as how real cross-repo work links (distinct repos, one fix, glued by a ticket reference in comments rather than a GitHub link). Cross-repo Terraform `depends_on` (S4) and the real-data trust gate + docs (S5) remain **Parts 3–4**.

---

## Key design decisions (locked before tasks)

1. **Aggregate, don't generalize.** `extract(conn, project, repo, …)` stays single-repo and byte-identical. `link.enrich`, `render`, and the per-repo `validate` checks are untouched. All multi-repo behaviour lives in the new `digest.py`. This is the option chosen over "generalize `extract` to a `repos` list + repo-qualified projection keys" precisely to keep every golden/characterization gate byte-stable.
2. **Trains are stitched, not rebuilt.** Each member's `link.enrich` already produces classified, single-repo `trains` (anchor, kind, outcome, prs, commits, evidence — link.py:212). A project train is a set of member trains whose **anchor nodes fall in one connected component of the project-wide spine** (`traverse_spine` over the union of members' in-window social nodes). Merging member trains reuses *all* existing classification; the only new logic is component-grouping + a precedence merge. Because `extract` materializes each member's arrays from `repo_nodes` (single-repo-scoped), a member train lives entirely within one repo — so a member train maps to exactly one component, and cross-repo links appear only as edges *between* member trains. (graphstore.py: `range_query` :337, `traverse_spine` :401, `get_edges` :204.)
3. **Repo is an explicit dimension.** The merged view keys trains by a project-scoped id, qualifies every pr/issue/commit reference as `{project}/{repo}#{local}`, tags every Shipped row with its `repo`, and repo-qualifies module ids (`{repo}::{area}`). Sections where flattening would collide and dedup is *not* meaningful (per-file `artifacts`, `feature_deltas`, `timeline`) are kept **per-member** under `view["members"]`, so the template renders them as per-repo subsections.
4. **People already aggregate.** Person nodes are project-scoped with the `repo="*"` sentinel (gather.py folds them once per project; `extract._materialize_people` reads them project-wide). Merging member `people` dicts by login is therefore a union, not a reconciliation.
5. **Internal-ticket grouping is a soft cluster, not a merge.** Spine-linked work becomes one train (decision 2). Work in different repos sharing a ticket id but no spine edge stays as separate trains, grouped under `related_work: [{ticket, train_ids:[…]}]` — matching the user's model (the ticket is the glue, the repos are distinct deliverables).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.claude/skills/activity-overview/graphstore.py` | Add `project_repos(conn, project)` — distinct non-sentinel member repos for a project (store-only member discovery). | **Modify** |
| `.claude/skills/activity-overview/spotlight.py` | Refactor `_project_repos` to call `graphstore.project_repos` (DRY; behaviour byte-identical). | **Modify** |
| `.claude/skills/activity-overview/digest.py` | The project aggregation layer: per-member extract+enrich, spine components, project-train stitching, ticket grouping, view assembly, CLI. | **Create** |
| `.claude/skills/activity-overview/test_digest.py` | Unit + integration tests for the aggregator. | **Create** |
| `.claude/skills/activity-overview/validate.py` | Add `validate_project(conn, project, repos)` + a `--project`-only multi-repo path in `main`. | **Modify** |
| `.claude/skills/activity-overview/test_validate.py` | Tests for the multi-repo validate path. | **Modify** |
| `.claude/skills/activity-overview/report-template.md` | Add the project sections (repo column on Shipped/Ownership; Related-work cluster; project-wide trains). | **Modify** |

**Run convention (all tasks):** tests import modules top-level, so run from the skill directory:

```bash
cd .claude/skills/activity-overview && python3 -m pytest <file> -k <name> -v
```

Full byte-stability guard (single-repo path unchanged):

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```

---

## Task 1: `graphstore.project_repos` — store-only member discovery

A reader needs the member set from the store + project name alone. `spotlight._project_repos` (spotlight.py:49) already computes exactly this; promote it to `graphstore` so `digest` and `spotlight` share one source of truth.

**Files:**
- Modify: `.claude/skills/activity-overview/graphstore.py` (add `project_repos` near `range_query`, ~:355)
- Modify: `.claude/skills/activity-overview/spotlight.py:49` (`_project_repos` delegates)
- Test: `.claude/skills/activity-overview/test_graphstore.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_graphstore.py`:

```python
class TestProjectRepos(unittest.TestCase):
    def test_distinct_members_excluding_person_sentinel(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("p/Azure/a#pr-1", "p", "Azure/a", "social", "2026-01-01", {}, None),
            ("p/Azure/b#pr-1", "p", "Azure/b", "social", "2026-01-02", {}, None),
            ("p/Azure/a#pr-2", "p", "Azure/a", "social", "2026-01-03", {}, None),
            ("p#person-x", "p", "*", "structure", None, {"login": "x"}, None),
        ])
        self.assertEqual(graphstore.project_repos(conn, "p"),
                         ["Azure/a", "Azure/b"])

    def test_empty_project(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        self.assertEqual(graphstore.project_repos(conn, "nope"), [])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_graphstore.py -k TestProjectRepos -v
```
Expected: FAIL — `module 'graphstore' has no attribute 'project_repos'`.

- [ ] **Step 3: Add the implementation**

In `.claude/skills/activity-overview/graphstore.py`, after `range_query` (~:355):

```python
def project_repos(conn, project):
    """The project's distinct MEMBER repos (`owner/repo`), sorted. Excludes the
    `"*"` person sentinel so people (project-scoped) never count as a repo. This
    is store-only member discovery: a reader needs just the store + project name."""
    return sorted(
        r[0] for r in conn.execute(
            "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
            (project,))
    )
```

- [ ] **Step 4: Refactor `spotlight._project_repos` to delegate (DRY)**

In `.claude/skills/activity-overview/spotlight.py`, replace the body of `_project_repos` (:49-55) with:

```python
def _project_repos(conn, project):
    """The non-sentinel repos for a project (people aggregate across them)."""
    return graphstore.project_repos(conn, project)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_graphstore.py -k TestProjectRepos test_spotlight.py -q
```
Expected: PASS — new tests green, and every existing `spotlight` test still passes (behaviour identical).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/graphstore.py .claude/skills/activity-overview/spotlight.py .claude/skills/activity-overview/test_graphstore.py
git commit -m "feat(activity): graphstore.project_repos member discovery (S3)"
```

---

## Task 2: `digest.member_bundles` — per-member extract + enrich

The aggregator's foundation: materialize each member's enriched bundle via the *existing* single-repo pipeline. Each call is one repo, so the bundles are byte-identical to a standalone single-repo digest of that member.

**Files:**
- Create: `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Create `.claude/skills/activity-overview/test_digest.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import graphstore  # noqa: E402
import gather  # noqa: E402
import digest  # noqa: E402


def _seed_two_member_store(conn):
    """Member A (Azure/mod-a): PR #10 merged, closes A-local issue is NOT set;
    instead it closes mod-b#3 (cross-repo, via Part 1 fold). Member B
    (Azure/mod-b): issue #3 closed. Both in window 2026-01."""
    graphstore.init_schema(conn)
    bundle_a = {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [{"number": 10, "url": "uA/10", "state": "closed", "merged": True,
                 "base": "main", "head": "f10",
                 "merged_at": "2026-01-10T00:00:00Z",
                 "created_at": "2026-01-05T00:00:00Z",
                 "closed_at": "2026-01-10T00:00:00Z",
                 "closes": [], "crossref_issues": [],
                 "title": "feat: thing", "body": "Closes Azure/mod-b#3"}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    bundle_b = {
        "meta": {"owner": "Azure", "repo": "mod-b", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [], "issues": [{"number": 3, "url": "uB/3", "state": "closed",
                               "closed_at": "2026-01-08T00:00:00Z",
                               "updated_at": "2026-01-08T00:00:00Z"}],
        "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    members = {"Azure/mod-a", "Azure/mod-b"}
    gather.fold_bundle(conn, bundle_a, project="proj", repo="Azure/mod-a",
                       members=members)
    gather.fold_bundle(conn, bundle_b, project="proj", repo="Azure/mod-b",
                       members=members)


class TestMemberBundles(unittest.TestCase):
    def test_one_enriched_bundle_per_member(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        members = digest.member_bundles(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual([m["repo"] for m in members],
                         ["Azure/mod-a", "Azure/mod-b"])
        # each member bundle is the single-repo enriched shape (has trains/buckets)
        self.assertIn("trains", members[0]["bundle"])
        self.assertIn("buckets", members[0]["bundle"])
        # A's PR #10 is in A's bundle only
        self.assertEqual([p["number"] for p in members[0]["bundle"]["prs"]], [10])
        self.assertEqual(members[1]["bundle"]["prs"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestMemberBundles -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'digest'`.

- [ ] **Step 3: Write the implementation**

Create `.claude/skills/activity-overview/digest.py`:

```python
"""Phase 9 project digest aggregator (S3).

One level above `extract`: given a project and its member repos, materialize each
member's enriched bundle via the EXISTING single-repo pipeline
(`extract.extract` + `link.enrich`), then merge the per-member results into one
project view. Repo is an explicit dimension throughout — single-repo
`extract`/`link`/`render` are untouched and byte-stable. See
docs/superpowers/specs/2026-06-05-activity-phase9-multirepo.md (S3).
"""
import argparse
import json
import sys

import extract as extract_mod
import graphstore
import link as link_mod


def member_bundles(conn, project, repos, ts_from, ts_to, *, backfill=None,
                   backfill_budget=50):
    """Materialize + enrich one bundle per member repo, in `repos` order. Each is
    the byte-identical single-repo digest of that member (extract -> link.enrich).
    Returns [{"repo": "owner/repo", "bundle": <enriched dict>}, ...]."""
    out = []
    for repo in repos:
        bundle = extract_mod.extract(
            conn, project, repo, ts_from, ts_to,
            backfill=backfill, backfill_budget=backfill_budget,
            warn=lambda _m: None)
        link_mod.enrich(bundle)
        out.append({"repo": repo, "bundle": bundle})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestMemberBundles -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): digest.member_bundles per-member extract+enrich (S3)"
```

---

## Task 3: `digest.spine_components` — project-wide connected components

Group the project's in-window social nodes into connected components of the spine. Each component is one (possibly cross-repo) decision train. Built directly on the store, so cross-repo `closes`/`cross_ref` edges (Part 1) connect members with no extra logic.

**Files:**
- Modify: `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `test_digest.py`:

```python
class TestSpineComponents(unittest.TestCase):
    def test_cross_repo_edge_unifies_two_members(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        # exactly one component, containing A's PR and B's issue
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0],
                         frozenset({"proj/Azure/mod-a#pr-10",
                                    "proj/Azure/mod-b#issue-3"}))

    def test_unconnected_socials_are_separate_components(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("proj/Azure/a#pr-1", "proj", "Azure/a", "social", "2026-01-01", {}, None),
            ("proj/Azure/b#pr-9", "proj", "Azure/b", "social", "2026-01-02", {}, None),
        ])
        comps = digest.spine_components(
            conn, "proj", ["Azure/a", "Azure/b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual({frozenset(c) for c in comps},
                         {frozenset({"proj/Azure/a#pr-1"}),
                          frozenset({"proj/Azure/b#pr-9"})})
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestSpineComponents -v
```
Expected: FAIL — `module 'digest' has no attribute 'spine_components'`.

- [ ] **Step 3: Add the implementation**

In `digest.py`, after `member_bundles`:

```python
def spine_components(conn, project, repos, ts_from, ts_to, max_depth=6):
    """Connected components of the project-wide spine, seeded by in-window social
    nodes across `repos`. Each returned frozenset of qualified node ids is one
    decision train (possibly spanning members — the store's cross-repo
    closes/cross_ref edges join them). Deterministic: components are ordered by
    their lexicographically smallest member id."""
    in_window = graphstore.range_query(conn, project, repos, ts_from, ts_to)
    socials = [n["id"] for n in in_window if n["node_class"] == "social"]
    seen, comps = set(), []
    for sid in socials:
        if sid in seen:
            continue
        reached = graphstore.traverse_spine(
            conn, [sid], max_depth=max_depth, skip_dead=True)["reached"]
        # the component is the reachable set restricted to nodes that exist as
        # social anchors (issues/prs); commits/areas reached are train members but
        # we key trains off social anchors. Keep all reached SOCIAL ids.
        comp = {nid for nid in reached
                if graphstore.parse_id(nid)["local"].startswith(("pr-", "issue-"))}
        comp.add(sid)
        seen |= comp
        comps.append(frozenset(comp))
    comps.sort(key=lambda c: min(c))
    return comps
```

> Note: seeding from each unseen social and unioning the reachable socials yields
> disjoint components (a social already pulled into an earlier component is
> skipped). `skip_dead=True` matches `extract`'s traversal so a tombstoned
> phantom ref can't bridge unrelated trains.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestSpineComponents -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): digest.spine_components project-wide trains (S3)"
```

---

## Task 4: `digest.build_project_trains` — stitch member trains across repos

Map each member's (single-repo, already-classified) train to its spine component, then merge member trains that share a component into one project train: union the qualified prs/issues/commits/evidence, merge the outcome by precedence, and record the contributing `repos`.

**Files:**
- Modify: `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `test_digest.py`:

```python
class TestBuildProjectTrains(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        _seed_two_member_store(self.conn)
        self.frm, self.to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        self.members = digest.member_bundles(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)
        self.comps = digest.spine_components(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)

    def test_cross_repo_train_is_single_and_spans_repos(self):
        trains = digest.build_project_trains(self.members, self.comps, "proj")
        self.assertEqual(len(trains), 1)
        t = trains[0]
        self.assertEqual(set(t["repos"]), {"Azure/mod-a", "Azure/mod-b"})
        # prs/issues are QUALIFIED, spanning both repos
        self.assertIn("proj/Azure/mod-a#pr-10", t["prs"])
        self.assertIn("proj/Azure/mod-b#issue-3", t["issues"])
        self.assertEqual(t["outcome"], "shipped")

    def test_single_repo_train_preserved_with_qualified_ids(self):
        # B alone: an unconnected shipped PR in one repo -> one single-repo train.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = {"meta": {"owner": "Azure", "repo": "solo", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [{"number": 7, "url": "u/7", "state": "closed", "merged": True,
                      "base": "main", "head": "h7",
                      "merged_at": "2026-01-09T00:00:00Z",
                      "created_at": "2026-01-02T00:00:00Z",
                      "closed_at": "2026-01-09T00:00:00Z",
                      "closes": [], "crossref_issues": [], "title": "fix: x",
                      "body": ""}],
             "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        gather.fold_bundle(conn, b, project="proj", repo="Azure/solo",
                           members={"Azure/solo"})
        frm, to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        members = digest.member_bundles(conn, "proj", ["Azure/solo"], frm, to)
        comps = digest.spine_components(conn, "proj", ["Azure/solo"], frm, to)
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["repos"], ["Azure/solo"])
        self.assertEqual(trains[0]["prs"], ["proj/Azure/solo#pr-7"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestBuildProjectTrains -v
```
Expected: FAIL — `module 'digest' has no attribute 'build_project_trains'`.

- [ ] **Step 3: Add the implementation**

In `digest.py`, after `spine_components`:

```python
# outcome precedence when member trains merge into one project train (mirrors
# link.build_trains' shipped > in_flight > rejected > abandoned).
_OUTCOME_RANK = {"shipped": 3, "in_flight": 2, "rejected": 1, "abandoned": 0}


def _member_train_anchor_qid(project, repo, train):
    """The qualified spine anchor of a member train: its root issue, else its
    first PR — exactly link.build_trains' anchor, qualified to `repo`."""
    if train.get("root_issue") is not None:
        local = "issue-{}".format(train["root_issue"])
    else:
        local = "pr-{}".format(train["prs"][0])
    return graphstore.qualify_id(project, repo, local)


def build_project_trains(members, components, project):
    """Stitch the per-member trains into project trains along the spine.

    `members` is member_bundles' output; `components` is spine_components' output.
    Each member train maps to the component containing its anchor; member trains
    sharing a component merge into ONE project train with qualified, repo-spanning
    references. Trains whose anchor isn't in any component (e.g. an abandoned issue
    with no in-window social seed) form their own singleton project train.
    Deterministic order by project-train id."""
    # index: qualified anchor id -> component index
    comp_of = {}
    for i, comp in enumerate(components):
        for nid in comp:
            comp_of[nid] = i

    # bucket member trains by component (singletons keyed by their own anchor)
    groups = {}
    for m in members:
        repo, bundle = m["repo"], m["bundle"]
        for tr in bundle.get("trains", []):
            anchor = _member_train_anchor_qid(project, repo, tr)
            key = ("comp", comp_of[anchor]) if anchor in comp_of else ("solo", anchor)
            groups.setdefault(key, []).append((repo, tr))

    out = []
    for key, items in groups.items():
        prs, issues, commits, evidence, repos = [], [], [], [], []
        kind, outcome, root = "other", "abandoned", None
        best_rank = -1
        for repo, tr in items:
            if repo not in repos:
                repos.append(repo)
            for n in tr["prs"]:
                prs.append(graphstore.qualify_id(project, repo, "pr-{}".format(n)))
            if tr.get("root_issue") is not None:
                issues.append(graphstore.qualify_id(
                    project, repo, "issue-{}".format(tr["root_issue"])))
            for sha in tr.get("commits", []):
                commits.append(graphstore.qualify_id(project, repo, sha))
            for ev in tr.get("evidence", []):
                evidence.append({**ev, "repo": repo})
            rank = _OUTCOME_RANK.get(tr["outcome"], 0)
            if rank > best_rank:
                best_rank, outcome = rank, tr["outcome"]
            # kind/root: prefer a typed root-issue train (lowest anchor wins ties)
            if tr.get("root_issue") is not None and (root is None):
                kind, root = tr["kind"], _member_train_anchor_qid(project, repo, tr)
        if root is None:  # no root issue anywhere: take the kind of the min anchor
            repo0, tr0 = min(items, key=lambda rt: _member_train_anchor_qid(
                project, rt[0], rt[1]))
            kind = tr0["kind"]
        # the project-train id is derived from the merged, fully-qualified
        # reference set -> deterministic and globally unique across repos.
        tid = "ptrain-" + min(prs + issues)
        out.append({
            "id": tid,
            "kind": kind,
            "outcome": outcome,
            "repos": sorted(repos),
            "prs": sorted(set(prs)),
            "issues": sorted(set(issues)),
            "commits": sorted(set(commits)),
            "evidence": evidence,
            "code_areas": [],
        })
    out.sort(key=lambda t: t["id"])
    return out
```

> The project-train id is `"ptrain-" + min(prs + issues)` — deterministic and
> globally unique because every reference is fully qualified (`{project}/{repo}#…`).

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestBuildProjectTrains -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): digest.build_project_trains cross-repo stitch (S3)"
```

---

## Task 5: `digest.parse_ticket_refs` + `related_work` clustering

Group trains that share an internal-ticket reference (e.g. `ABC-1234`) but no spine edge — the real-world glue when distinct repos serve one fix linked only by a ticket mention in bodies/comments. A soft cluster (trains stay separate), not a merge.

**Files:**
- Modify: `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `test_digest.py`:

```python
class TestTicketGrouping(unittest.TestCase):
    def test_parse_ticket_refs_default_pattern(self):
        self.assertEqual(
            digest.parse_ticket_refs("see ABC-1234 and ABC-1234 and XY-7, not A1"),
            ["ABC-1234", "XY-7"])  # ordered, deduped; needs >=2 letters then -digits

    def test_parse_ticket_refs_empty(self):
        self.assertEqual(digest.parse_ticket_refs(None), [])

    def test_related_work_clusters_trains_sharing_a_ticket(self):
        trains = [
            {"id": "ptrain-a", "tickets": ["ABC-1"]},
            {"id": "ptrain-b", "tickets": ["ABC-1", "ZZ-9"]},
            {"id": "ptrain-c", "tickets": ["QQ-2"]},
        ]
        groups = digest.group_related_work(trains)
        self.assertEqual(groups, [{"ticket": "ABC-1",
                                   "train_ids": ["ptrain-a", "ptrain-b"]}])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestTicketGrouping -v
```
Expected: FAIL — `module 'digest' has no attribute 'parse_ticket_refs'`.

- [ ] **Step 3: Add the implementation**

In `digest.py`, add near the top (after imports):

```python
import re

# Internal-ticket reference (Jira/ADO-style): 2+ uppercase letters, hyphen,
# digits. Conservative so it doesn't swallow enum-like tokens (A1, v2). Override
# via build_project_view(ticket_pattern=...).
_DEFAULT_TICKET_RE = re.compile(r"\b([A-Z]{2,}-\d+)\b")


def parse_ticket_refs(text, pattern=_DEFAULT_TICKET_RE):
    """Ordered, deduped internal-ticket ids in `text`. Pure."""
    out, seen = [], set()
    for m in pattern.finditer(text or ""):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def group_related_work(trains):
    """Cluster project trains that share a ticket but are otherwise unlinked.
    Returns [{"ticket": str, "train_ids": [...]}, ...] for tickets spanning >=2
    trains, ordered by ticket. Each train carries a `tickets` list (see
    build_project_view)."""
    by_ticket = {}
    for tr in trains:
        for tk in tr.get("tickets", []):
            by_ticket.setdefault(tk, [])
            if tr["id"] not in by_ticket[tk]:
                by_ticket[tk].append(tr["id"])
    return [{"ticket": tk, "train_ids": sorted(ids)}
            for tk, ids in sorted(by_ticket.items()) if len(ids) >= 2]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestTicketGrouping -v
```
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): internal-ticket related_work clustering (S3)"
```

---

## Task 6: `digest.build_project_view` — assemble the merged view

Tie it together: per-member bundles + project trains (with tickets attached) + related-work clusters + merged Shipped/people/modules. Keep collision-prone per-file sections per-member under `view["members"]`.

**Files:**
- Modify: `.claude/skills/activity-overview/digest.py`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `test_digest.py`:

```python
class TestBuildProjectView(unittest.TestCase):
    def test_view_spans_members_with_merged_sections(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        view = digest.build_project_view(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual(view["meta"]["project"], "proj")
        self.assertEqual(view["meta"]["repos"], ["Azure/mod-a", "Azure/mod-b"])
        # one cross-repo train, spanning both repos
        self.assertEqual(len(view["trains"]), 1)
        self.assertEqual(set(view["trains"][0]["repos"]),
                         {"Azure/mod-a", "Azure/mod-b"})
        # Shipped row is repo-tagged and present for A's PR
        shipped_repos = {s["repo"] for s in view["shipped"]}
        self.assertIn("Azure/mod-a", shipped_repos)
        # per-member raw sections retained for collision-prone keys
        self.assertEqual([m["repo"] for m in view["members"]],
                         ["Azure/mod-a", "Azure/mod-b"])

    def test_contributor_in_both_members_appears_once(self):
        # Person nodes are project-scoped ("*"), so people merge to one login.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo in ("Azure/x", "Azure/y"):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31",
                          "base_branch": "main"},
                 "prs": [{"number": 1, "url": "u", "state": "closed",
                          "merged": True, "base": "main", "head": "h",
                          "merged_at": "2026-01-05T00:00:00Z",
                          "created_at": "2026-01-02T00:00:00Z",
                          "closed_at": "2026-01-05T00:00:00Z",
                          "closes": [], "crossref_issues": [],
                          "title": "feat: a", "body": "", "author": "alice"}],
                 "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo,
                               members={"Azure/x", "Azure/y"})
        view = digest.build_project_view(
            conn, "proj", ["Azure/x", "Azure/y"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertIn("alice", view["people"])
        self.assertEqual(len(view["people"]), 1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestBuildProjectView -v
```
Expected: FAIL — `module 'digest' has no attribute 'build_project_view'`.

- [ ] **Step 3: Add the implementation**

In `digest.py`, after `group_related_work`:

```python
def _attach_tickets(members, project_trains, project, ticket_pattern):
    """Set each project train's `tickets` from the bodies/titles of its member
    PRs/issues (+ any external_refs). Builds a qualified-id -> record index from
    the member bundles so a train's qualified refs resolve to their text."""
    rec = {}
    for m in members:
        repo, b = m["repo"], m["bundle"]
        for p in b.get("prs", []):
            rec[graphstore.qualify_id(project, repo, "pr-{}".format(p["number"]))] = p
        for i in b.get("issues", []):
            rec[graphstore.qualify_id(project, repo, "issue-{}".format(i["number"]))] = i
    for tr in project_trains:
        seen, tickets = set(), []
        for qid in tr["prs"] + tr["issues"]:
            r = rec.get(qid) or {}
            text = "{}\n{}".format(r.get("title", "") or "", r.get("body") or "")
            for ext in r.get("external_refs") or []:
                text += "\n{} {}".format(ext.get("repo", ""), ext.get("number", ""))
            for tk in parse_ticket_refs(text, ticket_pattern):
                if tk not in seen:
                    seen.add(tk)
                    tickets.append(tk)
        tr["tickets"] = tickets


def _merge_shipped(members):
    """Project-wide Shipped: each member's buckets.shipped rows, tagged by repo."""
    out = []
    for m in members:
        for row in (m["bundle"].get("buckets", {}) or {}).get("shipped", []):
            out.append({**row, "repo": m["repo"]})
    return out


def _merge_people(members):
    """Union member `people` dicts by login (person nodes are project-scoped, so
    the records are identical across members; union modules/areas, OR is_bot)."""
    people = {}
    for m in members:
        for login, rec in (m["bundle"].get("people", {}) or {}).items():
            cur = people.setdefault(login, {"modules": [], "areas": [],
                                            "is_bot": False})
            cur["modules"] = sorted(set(cur["modules"]) | set(rec.get("modules", [])))
            cur["areas"] = sorted(set(cur["areas"]) | set(rec.get("areas", [])))
            cur["is_bot"] = cur["is_bot"] or bool(rec.get("is_bot"))
    return people


def _merge_modules(members):
    """Project-wide modules, area ids repo-qualified ("{repo}::{area}") so two
    members' same-named areas never collide."""
    mods = {}
    for m in members:
        for area, stats in (m["bundle"].get("modules", {}) or {}).items():
            mods["{}::{}".format(m["repo"], area)] = stats
    return mods


def build_project_view(conn, project, repos, ts_from, ts_to, *, backfill=None,
                       backfill_budget=50, ticket_pattern=_DEFAULT_TICKET_RE):
    """The merged project view consumed by report-template.md's narrative step.
    Keys: meta{project,repos,from,to}, members[{repo,bundle}] (per-member raw +
    enriched, for collision-prone per-file sections), trains (cross-repo, with
    tickets), related_work (ticket clusters), shipped (repo-tagged), people
    (merged by login), modules (repo-qualified)."""
    members = member_bundles(conn, project, repos, ts_from, ts_to,
                             backfill=backfill, backfill_budget=backfill_budget)
    comps = spine_components(conn, project, repos, ts_from, ts_to)
    trains = build_project_trains(members, comps, project)
    _attach_tickets(members, trains, project, ticket_pattern)
    related = group_related_work(trains)
    return {
        "meta": {"project": project, "repos": list(repos),
                 "from": ts_from, "to": ts_to},
        "members": members,
        "trains": trains,
        "related_work": related,
        "shipped": _merge_shipped(members),
        "people": _merge_people(members),
        "modules": _merge_modules(members),
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestBuildProjectView -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite (byte-stability guard)**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS — `digest` is additive; nothing in `extract`/`link`/`render` changed.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): digest.build_project_view merged project view (S3)"
```

---

## Task 7: `validate.validate_project` — self-source over the member set

`validate.py` self-sources per repo via `extract` (validate.py:653) and checks drift/idempotency. Add a project mode that runs the existing per-member checks over the full member set and aggregates the verdict — so a multi-repo store is verifiable as one project.

**Files:**
- Modify: `.claude/skills/activity-overview/validate.py` (add `validate_project`; multi-repo branch in `main`)
- Test: `.claude/skills/activity-overview/test_validate.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_validate.py`:

```python
class TestValidateProject(unittest.TestCase):
    def test_two_member_store_validates_green(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo in ("Azure/mod-a", "Azure/mod-b"):
            b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                          "from": "2026-01-01", "to": "2026-01-31",
                          "base_branch": "main"},
                 "prs": [], "issues": [], "commits": [], "code_events": [],
                 "milestones": [], "releases": [], "code_graph": {"areas": []}}
            gather.fold_bundle(conn, b, project="proj", repo=repo,
                               members={"Azure/mod-a", "Azure/mod-b"})
        report = validate.validate_project(conn, "proj",
                                           ["Azure/mod-a", "Azure/mod-b"])
        self.assertTrue(report["ok"])
        self.assertEqual({r["repo"] for r in report["members"]},
                         {"Azure/mod-a", "Azure/mod-b"})
        self.assertTrue(all(r["ok"] for r in report["members"]))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_validate.py -k TestValidateProject -v
```
Expected: FAIL — `module 'validate' has no attribute 'validate_project'`.

- [ ] **Step 3: Add the implementation**

First inspect the existing single-repo entry point. `validate.py`'s `main` calls `_detect_project_repo` then runs the per-repo checks and builds a report dict (validate.py:727-759). Factor the per-repo body into a reusable `validate_repo(conn, project, repo)` returning `{"ok": bool, ...}` if it isn't already one function, then add:

```python
def validate_project(conn, project, repos):
    """Run the per-member validation over a project's full member set and
    aggregate. Returns {"ok": all-green, "project": project,
    "members": [{"repo": r, "ok": bool, ...per-repo report...}, ...]}."""
    member_reports = []
    for repo in repos:
        rep = validate_repo(conn, project, repo)
        member_reports.append({"repo": repo, **rep})
    return {"ok": all(r["ok"] for r in member_reports),
            "project": project, "members": member_reports}
```

> If `main`'s per-repo logic is inline rather than a `validate_repo` function,
> Step 3 first extracts it into `validate_repo(conn, project, repo)` with NO
> behaviour change (run the full `test_validate.py` suite to confirm byte-stable),
> then `main` and `validate_project` both call it.

- [ ] **Step 4: Wire `main` to the project path**

In `validate.py` `main`, when `--repo` is omitted but the store holds multiple repos for the detected project, run `validate_project(conn, project, graphstore.project_repos(conn, project))` and print/emit its aggregate (preserve the single-repo path exactly when one repo or `--repo` is given). Add `--project`-only handling:

```python
    project, repo = _detect_project_repo(conn, args.project, args.repo)
    repos = graphstore.project_repos(conn, project)
    if repo is None and len(repos) > 1:
        report = validate_project(conn, project, repos)
        _emit(report, as_json=args.json)        # existing emit helper
        return 0 if report["ok"] else 1
    # ... existing single-repo path unchanged ...
```

> `_detect_project_repo` must tolerate a `None` repo when several exist (today it
> may error/ambiguate). Adjust it to return `repo=None` in that case rather than
> raising, so the project path above triggers. Keep the single-repo behaviour
> identical when exactly one repo exists.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_validate.py -q
```
Expected: PASS — new project test green; every existing single-repo validate test unchanged.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/validate.py .claude/skills/activity-overview/test_validate.py
git commit -m "feat(activity): validate_project aggregates the member set (S3)"
```

---

## Task 8: `digest` CLI + `report-template.md` project sections

A one-shot reader command emitting the merged view as JSON for the narrative step, and the template additions so the report renders the project (repo columns + Related-work) — no Markdown generator, the template stays human/subagent-filled.

**Files:**
- Modify: `.claude/skills/activity-overview/digest.py` (add `main`)
- Modify: `.claude/skills/activity-overview/report-template.md`
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `test_digest.py`:

```python
class TestDigestCli(unittest.TestCase):
    def test_main_emits_project_view_json(self):
        import io
        import tempfile
        import contextlib
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            graphstore.dump_to(conn, store) if hasattr(graphstore, "dump_to") \
                else _persist(conn, store)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                digest.main(["--store", store, "--project", "proj",
                             "--from", "2026-01-01T00:00:00Z",
                             "--to", "2026-01-31T23:59:59Z"])
        view = json.loads(buf.getvalue())
        self.assertEqual(view["meta"]["project"], "proj")
        self.assertEqual(len(view["trains"]), 1)


def _persist(mem_conn, path):
    """Helper: copy an in-memory store to a file store for CLI tests."""
    disk = graphstore.open_store(path)
    mem_conn.backup(disk)
    disk.close()
```

> If `graphstore` already exposes a memory→file helper, use it instead of
> `_persist`. The CLI test exercises `digest.main` reading a real store path.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestDigestCli -v
```
Expected: FAIL — `module 'digest' has no attribute 'main'`.

- [ ] **Step 3: Add the CLI**

In `digest.py`, at the bottom:

```python
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Emit a multi-repo project digest view (JSON) from a store.")
    p.add_argument("--store", required=True, help="path to the journey-graph store")
    p.add_argument("--project", required=True, help="logical project name")
    p.add_argument("--repo", action="append", dest="repos", default=None,
                   help="member repo 'owner/repo'; repeatable. Default: all "
                        "members discovered in the store for the project.")
    p.add_argument("--from", dest="ts_from", required=True)
    p.add_argument("--to", dest="ts_to", required=True)
    p.add_argument("--ticket-pattern", default=None,
                   help="regex (one capture group) for internal-ticket refs; "
                        "default matches Jira/ADO-style ABC-1234.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    conn = graphstore.open_store(args.store)
    repos = args.repos or graphstore.project_repos(conn, args.project)
    pattern = (re.compile(args.ticket_pattern) if args.ticket_pattern
               else _DEFAULT_TICKET_RE)
    view = build_project_view(conn, args.project, repos, args.ts_from, args.ts_to,
                              ticket_pattern=pattern)
    # members carry full per-repo bundles; the narrative step reads them, but the
    # default JSON keeps them (the template references view["members"][*].bundle).
    sys.stdout.write(json.dumps(view, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Update `report-template.md`**

Add/adjust these sections (keep all existing single-repo sections; they read `view["members"][i]["bundle"]` per member):

- **Shipped this period** — add a leading `Repo` column sourced from `view["shipped"][].repo`.
- **Decision trains** — source from `view["trains"]`: each train shows its `repos` (a "spans: A, B" note when `len(repos) > 1`), `kind`, `outcome`, and qualified `prs`/`issues`.
- **Related work (cross-repo, ticket-linked)** — NEW section from `view["related_work"]`: for each `{ticket, train_ids}`, list the ticket and the trains it glues (the case where distinct repos serve one fix linked only by a ticket reference).
- **Module ownership** — add a `Repo` column; iterate `view["modules"]` (ids are `{repo}::{area}`) and `view["people"]` (merged logins).
- **Per-repo detail** — note that per-file sections (Content lifecycle, Feature changes) render once per member from `view["members"][i]["bundle"]`.

```markdown
## Related work (cross-repo, ticket-linked)

Trains in different repos that share an internal ticket but no GitHub link
(`view["related_work"]`). Each is one deliverable; the ticket is the glue.

| Ticket | Trains (repos) |
|--------|----------------|
| `{ticket}` | {for id in train_ids: train id + its repos} |
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestDigestCli -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/digest.py .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/test_digest.py
git commit -m "feat(activity): digest CLI + multi-repo report-template sections (S3)"
```

---

## Task 9: Integration — two-member project digest end-to-end

One test that proves the S3 gate: a two-member store renders one project view whose Shipped/Trains span both repos, whose cross-repo train is single, whose ticket-glued work clusters into `related_work`, and over which `validate_project` is green.

**Files:**
- Test: `.claude/skills/activity-overview/test_digest.py`

- [ ] **Step 1: Write the test**

Append to `test_digest.py`:

```python
class TestProjectDigestIntegration(unittest.TestCase):
    def test_gate_cross_repo_train_ticket_cluster_and_validate(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"Azure/mod-a", "Azure/mod-b", "Azure/mod-c"}
        # A closes B#3 (spine cross-repo train). C mentions the same ticket as A
        # but has NO spine link -> related_work cluster, not a merged train.
        a = {"meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [{"number": 10, "url": "uA/10", "state": "closed",
                      "merged": True, "base": "main", "head": "hA",
                      "merged_at": "2026-01-10T00:00:00Z",
                      "created_at": "2026-01-05T00:00:00Z",
                      "closed_at": "2026-01-10T00:00:00Z",
                      "closes": [], "crossref_issues": [],
                      "title": "feat: x", "body": "Closes Azure/mod-b#3\nADO-555"}],
             "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        b = {"meta": {"owner": "Azure", "repo": "mod-b", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [], "issues": [{"number": 3, "url": "uB/3", "state": "closed",
                                    "closed_at": "2026-01-08T00:00:00Z",
                                    "updated_at": "2026-01-08T00:00:00Z"}],
             "commits": [], "code_events": [], "milestones": [], "releases": [],
             "code_graph": {"areas": []}}
        c = {"meta": {"owner": "Azure", "repo": "mod-c", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [{"number": 20, "url": "uC/20", "state": "closed",
                      "merged": True, "base": "main", "head": "hC",
                      "merged_at": "2026-01-12T00:00:00Z",
                      "created_at": "2026-01-06T00:00:00Z",
                      "closed_at": "2026-01-12T00:00:00Z",
                      "closes": [], "crossref_issues": [],
                      "title": "feat: y", "body": "part of ADO-555"}],
             "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        for bundle, repo in ((a, "Azure/mod-a"), (b, "Azure/mod-b"),
                             (c, "Azure/mod-c")):
            gather.fold_bundle(conn, bundle, project="proj", repo=repo,
                               members=members)

        frm, to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        repos = graphstore.project_repos(conn, "proj")
        view = digest.build_project_view(conn, "proj", repos, frm, to)

        # one cross-repo train (A+B) and one standalone train (C)
        spanning = [t for t in view["trains"] if len(t["repos"]) > 1]
        self.assertEqual(len(spanning), 1)
        self.assertEqual(set(spanning[0]["repos"]), {"Azure/mod-a", "Azure/mod-b"})
        # ADO-555 glues A's train and C's train (different repos, no spine link)
        tickets = {g["ticket"] for g in view["related_work"]}
        self.assertIn("ADO-555", tickets)
        glued = next(g for g in view["related_work"] if g["ticket"] == "ADO-555")
        self.assertEqual(len(glued["train_ids"]), 2)
        # Shipped spans the project
        self.assertEqual({s["repo"] for s in view["shipped"]},
                         {"Azure/mod-a", "Azure/mod-c"})
        # validate is green across the member set
        self.assertTrue(validate.validate_project(conn, "proj", repos)["ok"])
```

(Add `import validate` to the test file's imports.)

- [ ] **Step 2: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_digest.py -k TestProjectDigestIntegration -v
```
Expected: PASS — Tasks 1–8 compose into the S3 gate.

> If it fails, the failure localizes: no spanning train → Task 3/4 (components or
> stitch); missing `related_work` → Task 5/6 (ticket parse/attach); Shipped gap →
> Task 6 (`_merge_shipped`); validate red → Task 7.

- [ ] **Step 3: Run the full suite**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/activity-overview/test_digest.py
git commit -m "test(activity): two-member project digest S3 gate integration"
```

---

## Part 2 done — verification checklist

- [ ] `digest.py --store j.db --project P --from F --to T` emits one project view spanning all members discovered in the store.
- [ ] Cross-repo decision trains (spine-linked across members) form **one** train with qualified, repo-spanning refs; single-repo trains are preserved with qualified ids.
- [ ] Work in different repos sharing an internal ticket but no spine edge clusters into `related_work` (soft group, trains stay separate).
- [ ] Shipped + Ownership aggregate the member set (repo-tagged; people deduped by login; module ids repo-qualified).
- [ ] `validate_project` is green over a two-member store; `validate.py` single-repo path is byte-stable.
- [ ] The full existing suite passes unchanged (single-repo `extract`/`link`/`render`/`validate` untouched).

---

## Roadmap — Parts 3–4 (separate plans)

- **Part 3 — S4: cross-repo Terraform `depends_on`.** Extend `build_terraform_edges.resolve()` so a registry source resolves to a member repo by manifest `registry` (exact) then HashiCorp naming convention, emit cross-repo `depends_on` (area→area) edges, extend `render`'s `module_graph` to span members, add a `spotlight` reverse-dependency (blast-radius) query. The merged `view["modules"]` (repo-qualified ids) from this plan is the render seam it builds on.
- **Part 4 — S5: real-data trust gate + docs.** Prove on a small real AVM-TF constellation (cross-repo trains form, deps resolve, `validate_project` green); update `SKILL.md` (a `--manifest` gather + `digest.py` reader procedure, multi-repo report sections), `STORE.md`, `REFERENCE.md`.

---

## Self-Review (against the spec, S3)

- **S3 coverage:** `extract`→member set via the aggregator (Tasks 2, 6); store-only member discovery (Task 1); cross-repo trains via spine stitching (Tasks 3–4); Shipped/people/modules aggregation, contributor-merges-to-one (Task 6); `validate` over the full member set (Task 7); project digest CLI + template sections (Task 8); end-to-end gate (Task 9) ✓.
- **User extension (ticket glue):** internal-ticket parsing + `related_work` clustering for repos linked only by a ticket (Tasks 5, 6, 9) ✓.
- **Byte-stability:** `extract`/`link`/`render` untouched; `validate` single-repo path preserved (Task 7 refactors to a shared `validate_repo` with a full-suite guard); `spotlight` refactor is behaviour-identical (Task 1). Full-suite guard steps in Tasks 6, 7 ✓.
- **Type consistency:** project trains use `{id, kind, outcome, repos, prs, issues, commits, evidence, code_areas, tickets}` — `build_project_trains` (Task 4) sets all but `tickets`, which `_attach_tickets` (Task 6) adds before `group_related_work` (Task 5) reads it; `view` keys (`meta, members, trains, related_work, shipped, people, modules`) are produced in Task 6 and consumed by the CLI/template (Task 8) and the integration gate (Task 9) ✓.
- **Deferred:** S4/S5 explicitly to Parts 3–4 with the render/module seam called out ✓.
