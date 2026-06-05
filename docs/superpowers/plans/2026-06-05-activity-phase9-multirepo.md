# Phase 9 Multi-Repo Project — Implementation Plan (Part 1: producer + cross-repo graph)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `gather` fold several GitHub repos into ONE journey-graph store under one logical project (identity `{project}/{owner/repo}#{local}`), and link decision trains *across* member repos via qualified body refs and repo-aware timeline cross-references.

**Architecture:** Additive `--manifest` path in `gather`. The single-repo `--owner/--repo` path is untouched and byte-stable; multi-repo is gated entirely on a member set passed to `fold_bundle`. Cross-repo spine edges are derived at fold time from data already in the PR blob (title/body) plus the injected member set; member→member becomes a `closes`/`cross_ref` edge, member→non-member becomes an honest `external_refs` record (never a fetchable gap). The store-wide `traverse_spine` already walks edges regardless of repo, so cross-repo trains form with zero traversal change.

**Tech Stack:** Python 3 stdlib only (`argparse`, `json`, `re`, `sqlite3`), `unittest` tests run under `pytest`. All work in `.claude/skills/activity-overview/`.

**Scope of Part 1 (this plan):** spec slices **S1 + S2**. The deliverable is a *trustworthy multi-repo graph* — verifiable at the `graphstore` level (folded nodes, cross-repo edges, `traverse_spine` spanning repos), which is exactly this project's Phase-7 "the store IS the deliverable" philosophy. Reader aggregation (S3), cross-repo Terraform deps (S4), and the real-data trust gate (S5) are **Parts 2–4** — see Roadmap. Each Part produces working, testable software on its own.

**Spec:** `docs/superpowers/specs/2026-06-05-activity-phase9-multirepo.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.claude/skills/activity-overview/manifest.py` | Load + validate the project manifest (JSON → `{project, from, to, repos:[{owner,repo,registry}]}`). | **Create** |
| `.claude/skills/activity-overview/test_manifest.py` | Manifest loader unit tests. | **Create** |
| `.claude/skills/activity-overview/gather.py` | Multi-repo producer: `--manifest` CLI, per-member acquire loop, `fold_bundle` project/repo override + cross-repo edge emission, qualified-ref + repo-aware-timeline parsers. | **Modify** |
| `.claude/skills/activity-overview/test_gather.py` | Tests for the new parsers, fold override, cross-repo edges, single-repo byte-stability, and the two-repo integration. | **Modify** |

**Run convention (all tasks):** tests import `gather`/`graphstore` as top-level modules, so run from the skill directory:

```bash
cd .claude/skills/activity-overview && python3 -m pytest <file> -k <name> -v
```

The full guard for "single-repo is unchanged" is the existing suite:

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```

---

## Task 1: Manifest loader

**Files:**
- Create: `.claude/skills/activity-overview/manifest.py`
- Test: `.claude/skills/activity-overview/test_manifest.py`

- [ ] **Step 1: Write the failing tests**

Create `.claude/skills/activity-overview/test_manifest.py`:

```python
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import manifest  # noqa: E402


def _write(tmp, obj):
    path = os.path.join(tmp, "m.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return path


class TestLoadManifest(unittest.TestCase):
    def test_loads_project_window_and_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {
                "project": "avm-tf-storage",
                "window": {"from": "2026-03-01", "to": "2026-03-31"},
                "repos": [
                    {"owner": "Azure", "repo": "terraform-azurerm-avm-res-storage-storageaccount",
                     "registry": "Azure/avm-res-storage-storageaccount/azurerm"},
                    {"owner": "Azure", "repo": "terraform-azurerm-avm-res-keyvault-vault"},
                ],
            })
            m = manifest.load_manifest(path)
        self.assertEqual(m["project"], "avm-tf-storage")
        self.assertEqual(m["from"], "2026-03-01")
        self.assertEqual(m["to"], "2026-03-31")
        self.assertEqual(len(m["repos"]), 2)
        self.assertEqual(m["repos"][0]["registry"],
                         "Azure/avm-res-storage-storageaccount/azurerm")
        self.assertIsNone(m["repos"][1]["registry"])  # optional, defaults None

    def test_member_slugs(self):
        m = {"repos": [{"owner": "Azure", "repo": "a"},
                       {"owner": "Azure", "repo": "b"}]}
        self.assertEqual(manifest.member_slugs(m), {"Azure/a", "Azure/b"})

    def test_rejects_missing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaises(ValueError):
                manifest.load_manifest(path)

    def test_rejects_missing_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaises(ValueError):
                manifest.load_manifest(path)

    def test_rejects_empty_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p",
                                "window": {"from": "x", "to": "y"}, "repos": []})
            with self.assertRaises(ValueError):
                manifest.load_manifest(path)

    def test_rejects_member_without_owner_or_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o"}]})
            with self.assertRaises(ValueError):
                manifest.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_manifest.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'manifest'`.

- [ ] **Step 3: Write the implementation**

Create `.claude/skills/activity-overview/manifest.py`:

```python
"""Load + validate a Phase 9 multi-repo project manifest.

A manifest declares a LOGICAL project name, one gather window, and an explicit
set of member repos (each `owner/repo`, with an optional Terraform registry path
used for cross-repo dependency resolution in a later slice). JSON, stdlib-only —
consistent with the rest of the skill. This is the contract `gather --manifest`
folds against; how the manifest is authored (hand-written, or generated from the
AVM index) is out of scope.
"""
import json


def load_manifest(path):
    """Read + validate a manifest file. Returns a normalized dict:
    {"project": str, "from": str, "to": str,
     "repos": [{"owner": str, "repo": str, "registry": str|None}, ...]}.
    Raises ValueError on any missing/empty required field."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    project = raw.get("project")
    if not project:
        raise ValueError("manifest: 'project' is required")

    window = raw.get("window") or {}
    frm, to = window.get("from"), window.get("to")
    if not frm or not to:
        raise ValueError("manifest: 'window.from' and 'window.to' are required")

    raw_repos = raw.get("repos") or []
    if not raw_repos:
        raise ValueError("manifest: 'repos' must list at least one member")

    repos = []
    for r in raw_repos:
        owner, repo = r.get("owner"), r.get("repo")
        if not owner or not repo:
            raise ValueError("manifest: each repo needs 'owner' and 'repo'")
        repos.append({"owner": owner, "repo": repo, "registry": r.get("registry")})

    return {"project": project, "from": frm, "to": to, "repos": repos}


def member_slugs(manifest):
    """The set of 'owner/repo' slugs for a (loaded) manifest dict."""
    return {"{}/{}".format(r["owner"], r["repo"]) for r in manifest.get("repos", [])}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_manifest.py -v
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/manifest.py .claude/skills/activity-overview/test_manifest.py
git commit -m "feat(activity): manifest loader for multi-repo projects (S1)"
```

---

## Task 2: `fold_bundle` project/repo override (identity plumbing)

Make `fold_bundle` accept explicit `project`/`repo` (and a `members` param, unused until Task 7), defaulting to today's `meta.owner`/`meta.repo`. This lets the manifest loop fold each member under the LOGICAL project with `repo = "owner/repo"`, while the single-repo path stays byte-identical.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:2026` (`fold_bundle` signature + the `project, repo = …` lines)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py` (after the `TestFoldBundle` class):

```python
class TestFoldBundleOverride(unittest.TestCase):
    def test_override_project_and_slug_repo(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle(),
                           project="avm-tf", repo="Azure/widget")
        # node ids are qualified with the OVERRIDE project + owner/repo slug
        pr = graphstore.get_node(conn, "avm-tf/Azure/widget#pr-10")
        self.assertEqual(pr["node_class"], "social")
        self.assertEqual(pr["project"], "avm-tf")
        self.assertEqual(pr["repo"], "Azure/widget")
        # spine edge dst is qualified the same way (parse_id splits scope on first /)
        out = graphstore.get_edges(conn, "avm-tf/Azure/widget#pr-10", direction="out")
        self.assertIn(("closes", "avm-tf/Azure/widget#issue-3"),
                      {(e["edge_type"], e["dst_id"]) for e in out})
        # window + clone_sha recorded under the override identity
        self.assertIn({"project": "avm-tf", "repo": "Azure/widget",
                       "from": "2026-01-01", "to": "2026-01-31"},
                      graphstore.get_windows(conn))
        self.assertEqual(graphstore.get_clone_sha(conn, "avm-tf", "Azure/widget"),
                         "deadbeef")

    def test_default_identity_unchanged(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle())  # no override
        self.assertIsNotNone(graphstore.get_node(conn, "acme/widget#pr-10"))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldBundleOverride -v
```
Expected: FAIL — `fold_bundle()` got an unexpected keyword argument `project`.

- [ ] **Step 3: Edit the implementation**

In `.claude/skills/activity-overview/gather.py`, change the `fold_bundle` signature and the project/repo derivation (lines ~2026 and ~2041-2044).

Replace:

```python
def fold_bundle(conn, bundle):
```
with:

```python
def fold_bundle(conn, bundle, project=None, repo=None, members=None):
```

Replace:

```python
    meta = bundle.get("meta", {})
    project, repo = meta.get("owner"), meta.get("repo")
    if not project or not repo:
        raise ValueError("bundle meta needs owner and repo to qualify ids")
```
with:

```python
    meta = bundle.get("meta", {})
    # Identity override (Phase 9): a multi-repo run folds each member under one
    # LOGICAL project (`project`) with `repo` = "owner/repo". Single-repo runs pass
    # neither and fall back to meta.owner/meta.repo — byte-identical to before.
    # `members` (a set/dict of "owner/repo" slugs) gates cross-repo edge emission;
    # None (single-repo) skips it entirely (see Task 7).
    if project is None:
        project = meta.get("owner")
    if repo is None:
        repo = meta.get("repo")
    if not project or not repo:
        raise ValueError("bundle meta needs owner and repo to qualify ids")
```

Also update the docstring's first line is optional; leave the body as-is. The trailing `record_window`/`set_clone_sha` already use the `project`/`repo` locals, so they pick up the override with no change.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldBundleOverride -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite (byte-stability guard)**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS — no existing test regresses (the override defaults preserve today's behavior).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): fold_bundle project/repo identity override (S1)"
```

---

## Task 3: `gather --manifest` CLI + per-member acquire loop

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:1346` (`parse_args`), `:2296` (`main`); add a `_member_args` helper.
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
class TestManifestMain(unittest.TestCase):
    def test_member_args_clones_namespace_with_overrides(self):
        base = gather.parse_args([
            "--owner", "x", "--repo", "y", "--from", "a", "--to", "b",
            "--store", "s.db", "--no-clone"])
        member = gather._member_args(
            base, {"owner": "Azure", "repo": "mod-a", "registry": None},
            "2026-03-01", "2026-03-31")
        self.assertEqual(member.owner, "Azure")
        self.assertEqual(member.repo, "mod-a")
        self.assertEqual(getattr(member, "from"), "2026-03-01")
        self.assertEqual(member.to, "2026-03-31")
        self.assertIsNone(member.clone_dir)            # re-derived per member
        self.assertTrue(member.no_clone)               # other flags carried through

    def test_main_folds_each_member_under_logical_project(self):
        import tempfile
        man = {
            "project": "proj",
            "window": {"from": "2026-01-01", "to": "2026-01-31"},
            "repos": [{"owner": "Azure", "repo": "mod-a"},
                      {"owner": "Azure", "repo": "mod-b"}],
        }
        calls = []

        def fake_acquire(args, env):
            calls.append((args.owner, args.repo))
            b = _fold_fixture_bundle()
            b["meta"] = {**b["meta"], "owner": args.owner, "repo": args.repo,
                         "from": getattr(args, "from"), "to": args.to}
            return b

        with tempfile.TemporaryDirectory() as tmp:
            mpath = os.path.join(tmp, "m.json")
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            store = os.path.join(tmp, "j.db")
            orig = gather.acquire
            gather.acquire = fake_acquire
            try:
                gather.main(["--manifest", mpath, "--store", store])
            finally:
                gather.acquire = orig
            conn = graphstore.open_store(store)
        # both members acquired, folded under the logical project + owner/repo slug
        self.assertEqual(set(calls), {("Azure", "mod-a"), ("Azure", "mod-b")})
        self.assertIsNotNone(graphstore.get_node(conn, "proj/Azure/mod-a#pr-10"))
        self.assertIsNotNone(graphstore.get_node(conn, "proj/Azure/mod-b#pr-10"))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestManifestMain -v
```
Expected: FAIL — `_member_args` does not exist / `--manifest` unrecognized.

- [ ] **Step 3: Edit `parse_args`**

In `.claude/skills/activity-overview/gather.py`, add the flag near the other args (after `p.add_argument("--repo")`, line ~1349):

```python
    p.add_argument("--manifest",
                   help="path to a multi-repo project manifest (JSON). Mutually "
                        "exclusive with --owner/--repo; folds every member repo "
                        "into one store under the manifest's logical project name.")
```

- [ ] **Step 4: Add the `_member_args` helper and the import**

Near the top of `gather.py`, ensure `manifest` is importable — add with the other local imports (alongside `import derive` / `import graphstore`):

```python
import manifest as manifest_mod
```

Add `_member_args` just above `def main(` (line ~2296):

```python
def _member_args(base, member, frm, to):
    """Clone the CLI args for one manifest member: same flags, but owner/repo and
    the window come from the manifest, and clone_dir is re-derived per member
    (acquire defaults it to workspace/{repo}-clone)."""
    import argparse
    fields = {**vars(base), "owner": member["owner"], "repo": member["repo"],
              "clone_dir": None}
    ns = argparse.Namespace(**fields)
    setattr(ns, "from", frm)   # 'from' is a Python keyword: set via attribute
    ns.to = to
    return ns
```

- [ ] **Step 5: Edit `main` to branch on `--manifest`**

Replace the body of `main` (lines ~2296-2308) with:

```python
def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    os.makedirs(os.path.dirname(args.store) or ".", exist_ok=True)
    conn = graphstore.open_store(args.store)
    graphstore.init_schema(conn)
    if getattr(args, "manifest", None):
        man = manifest_mod.load_manifest(args.manifest)
        members = manifest_mod.member_slugs(man)
        for m in man["repos"]:
            member_args = _member_args(args, m, man["from"], man["to"])
            bundle = acquire(member_args, os.environ)
            fold_bundle(conn, bundle, project=man["project"],
                        repo="{}/{}".format(m["owner"], m["repo"]),
                        members=members)
        sys.stderr.write(
            "folded {} member repo(s) of project '{}' into store {}\n".format(
                len(man["repos"]), man["project"], args.store))
    else:
        # Store-only single-repo path (Phase 7), unchanged.
        bundle = acquire(args, os.environ)
        fold_bundle(conn, bundle)
        sys.stderr.write("folded bundle into store {}\n".format(args.store))
    conn.close()
    return args.store
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestManifestMain -v
```
Expected: PASS (2 tests).

- [ ] **Step 7: Run the full suite**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): gather --manifest multi-repo acquire loop (S1)"
```

---

## Task 4: `parse_qualified_refs` — cross-repo body references

Pure parser for qualified refs in PR/issue text: closing keywords (`Closes owner/repo#N`) and full GitHub URLs (`https://github.com/owner/repo/(issues|pull)/N`). Bare `#N` stays with the existing `parse_closing_refs` (same-repo).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add regexes + function near `_CLOSING_RE`, line ~219)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
class TestParseQualifiedRefs(unittest.TestCase):
    def test_closing_keyword_owner_repo_hash(self):
        refs = gather.parse_qualified_refs(
            "Closes Azure/Azure-Verified-Modules#1234 and more")
        self.assertEqual(refs, [{"owner": "Azure", "repo": "Azure-Verified-Modules",
                                 "number": 1234, "kind": "closes", "is_pr": False}])

    def test_full_url_issue_and_pull(self):
        refs = gather.parse_qualified_refs(
            "see https://github.com/Azure/mod-b/issues/7 and "
            "https://github.com/Azure/mod-c/pull/9")
        self.assertEqual(refs, [
            {"owner": "Azure", "repo": "mod-b", "number": 7,
             "kind": "cross_ref", "is_pr": False},
            {"owner": "Azure", "repo": "mod-c", "number": 9,
             "kind": "cross_ref", "is_pr": True},
        ])

    def test_dedup_order_preserving(self):
        refs = gather.parse_qualified_refs(
            "Fixes Azure/a#1\nResolves Azure/a#1\nFixes Azure/b#2")
        self.assertEqual([(r["repo"], r["number"]) for r in refs], [("a", 1), ("b", 2)])

    def test_bare_hash_not_captured(self):
        self.assertEqual(gather.parse_qualified_refs("Closes #5"), [])

    def test_empty(self):
        self.assertEqual(gather.parse_qualified_refs(None), [])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestParseQualifiedRefs -v
```
Expected: FAIL — `module 'gather' has no attribute 'parse_qualified_refs'`.

- [ ] **Step 3: Add the implementation**

In `.claude/skills/activity-overview/gather.py`, after the `_CLOSING_RE` block (line ~221), add:

```python
# Cross-repo references (Phase 9). A closing keyword qualified with owner/repo
# (`Closes Azure/repo#12`) -> a `closes` link to that OTHER repo's issue. A bare
# GitHub URL (`https://github.com/owner/repo/(issues|pull)/N`) -> a `cross_ref`
# mention (issues -> issue node, pull -> pr node). Bare `#N` stays same-repo
# (parse_closing_refs). Owners/repos: GitHub names — alnum start, then word/.-.
_QUALIFIED_CLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?P<owner>[A-Za-z0-9][\w.-]*)/(?P<repo>[A-Za-z0-9][\w.-]*)#(?P<num>\d+)",
    re.IGNORECASE,
)
_QUALIFIED_URL_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9][\w.-]*)/(?P<repo>[A-Za-z0-9][\w.-]*)/"
    r"(?P<kind>issues|pull)/(?P<num>\d+)",
)


def parse_qualified_refs(text):
    """Cross-repo refs in PR/issue text, ordered + deduped. Returns a list of
    {owner, repo, number, kind, is_pr}: closing keywords -> kind 'closes'
    (is_pr False; closing targets an issue); github.com URLs -> kind 'cross_ref'
    (is_pr True for /pull/, False for /issues/). Bare `#N` is left to
    parse_closing_refs (same-repo). Pure."""
    out, seen = [], set()

    def add(owner, repo, num, kind, is_pr):
        key = (owner, repo, num)
        if key not in seen:
            seen.add(key)
            out.append({"owner": owner, "repo": repo, "number": num,
                        "kind": kind, "is_pr": is_pr})

    for m in _QUALIFIED_CLOSE_RE.finditer(text or ""):
        add(m.group("owner"), m.group("repo"), int(m.group("num")), "closes", False)
    for m in _QUALIFIED_URL_RE.finditer(text or ""):
        add(m.group("owner"), m.group("repo"), int(m.group("num")), "cross_ref",
            m.group("kind") == "pull")
    return out
```

> Note: `seen` is keyed on `(owner, repo, number)` so a URL duplicating a
> closing-keyword target is dropped (the closing keyword wins because it is
> scanned first — its `closes`/issue semantics are stronger than a bare mention).

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestParseQualifiedRefs -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): parse_qualified_refs for cross-repo body refs (S2)"
```

---

## Task 5: `parse_timeline_xrefs` — repo-aware timeline cross-references

`parse_timeline_crossrefs` (existing) returns bare same-repo issue numbers and **drops the source repo**. Add a sibling that returns the *cross-repo* refs (source repo ≠ current), carrying the source owner/repo/number and whether the source is a PR.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add function after `parse_timeline_crossrefs`, line ~361)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
class TestParseTimelineXrefs(unittest.TestCase):
    def _ev(self, full_name, number, is_pr):
        src = {"number": number,
               "repository": {"full_name": full_name},
               "pull_request": ({} if is_pr else None)}
        return {"event": "cross-referenced", "source": {"issue": src}}

    def test_keeps_cross_repo_drops_same_repo(self):
        tl = [self._ev("Azure/other", 7, False),     # cross-repo issue
              self._ev("Azure/self", 3, False),       # same repo -> dropped
              self._ev("Azure/other2", 9, True)]      # cross-repo PR
        out = gather.parse_timeline_xrefs(tl, "Azure/self")
        self.assertEqual(out, [
            {"owner": "Azure", "repo": "other", "number": 7,
             "kind": "cross_ref", "is_pr": False},
            {"owner": "Azure", "repo": "other2", "number": 9,
             "kind": "cross_ref", "is_pr": True},
        ])

    def test_dedup_and_ignores_non_crossref_events(self):
        tl = [self._ev("Azure/other", 7, False),
              self._ev("Azure/other", 7, False),
              {"event": "labeled"}]
        out = gather.parse_timeline_xrefs(tl, "Azure/self")
        self.assertEqual(len(out), 1)

    def test_empty(self):
        self.assertEqual(gather.parse_timeline_xrefs(None, "o/r"), [])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestParseTimelineXrefs -v
```
Expected: FAIL — `module 'gather' has no attribute 'parse_timeline_xrefs'`.

- [ ] **Step 3: Add the implementation**

In `.claude/skills/activity-overview/gather.py`, after `parse_timeline_crossrefs` (line ~361), add:

```python
def parse_timeline_xrefs(raw_timeline, current_slug):
    """Cross-REPO timeline cross-references (Phase 9): cross-referenced events
    whose source issue/PR lives in a DIFFERENT repo than `current_slug`
    ('owner/repo'). Same-repo refs are left to parse_timeline_crossrefs. Returns
    ordered, deduped [{owner, repo, number, kind='cross_ref', is_pr}]. Pure."""
    out, seen = [], set()
    for ev in raw_timeline or []:
        if ev.get("event") != "cross-referenced":
            continue
        src = (ev.get("source") or {}).get("issue") or {}
        full = ((src.get("repository") or {}).get("full_name")) or ""
        num = src.get("number")
        if not full or full == current_slug or num is None:
            continue
        key = (full, num)
        if key in seen:
            continue
        seen.add(key)
        owner, _, repo = full.partition("/")
        out.append({"owner": owner, "repo": repo, "number": num,
                    "kind": "cross_ref", "is_pr": src.get("pull_request") is not None})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestParseTimelineXrefs -v
```
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): parse_timeline_xrefs (repo-aware timeline) (S2)"
```

---

## Task 6: `acquire` captures cross-repo timeline xrefs (conditional key)

Store the cross-repo timeline refs on the PR blob so `fold_bundle` can resolve them. The source-repo info is only available at acquire time (the timeline API), so it must be captured here. Use the codebase's conditional-key pattern (write the key only when non-empty) so single-repo gathers without cross-repo timeline refs are byte-identical — and the golden/fold fixtures (which never carry `timeline_xrefs`) are unaffected.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py:1712-1714` (the timeline fetch in `acquire`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
class TestAcquireTimelineXrefs(unittest.TestCase):
    def test_sets_timeline_xrefs_only_when_cross_repo_present(self):
        pr_same = {"number": 1, "crossref_issues": []}
        tl_same = [{"event": "cross-referenced",
                    "source": {"issue": {"number": 5,
                                         "repository": {"full_name": "Azure/self"},
                                         "pull_request": None}}}]
        gather._attach_timeline_xrefs(pr_same, tl_same, "Azure/self")
        self.assertNotIn("timeline_xrefs", pr_same)   # same-repo only -> key absent

        pr_cross = {"number": 2, "crossref_issues": []}
        tl_cross = [{"event": "cross-referenced",
                     "source": {"issue": {"number": 8,
                                          "repository": {"full_name": "Azure/other"},
                                          "pull_request": None}}}]
        gather._attach_timeline_xrefs(pr_cross, tl_cross, "Azure/self")
        self.assertEqual(pr_cross["timeline_xrefs"],
                         [{"owner": "Azure", "repo": "other", "number": 8,
                           "kind": "cross_ref", "is_pr": False}])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestAcquireTimelineXrefs -v
```
Expected: FAIL — `module 'gather' has no attribute '_attach_timeline_xrefs'`.

- [ ] **Step 3: Add the helper and call it in `acquire`**

In `.claude/skills/activity-overview/gather.py`, add the helper just above `def acquire(` (line ~1596):

```python
def _attach_timeline_xrefs(pr, timeline, current_slug):
    """Set pr['timeline_xrefs'] to the cross-repo timeline refs, but ONLY when
    there are any (conditional key: a PR with no cross-repo refs keeps its exact
    prior shape, so single-repo gathers stay byte-identical). Mutates pr."""
    xrefs = parse_timeline_xrefs(timeline, current_slug)
    if xrefs:
        pr["timeline_xrefs"] = xrefs
```

In `acquire`, find the timeline block (line ~1712):

```python
        timeline = fetch_all(
            get_page, f"{api}/issues/{pr['number']}/timeline?per_page=100")
        pr["crossref_issues"] = parse_timeline_crossrefs(timeline)
```
and add one line after it:

```python
        timeline = fetch_all(
            get_page, f"{api}/issues/{pr['number']}/timeline?per_page=100")
        pr["crossref_issues"] = parse_timeline_crossrefs(timeline)
        _attach_timeline_xrefs(pr, timeline, f"{owner}/{repo}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestAcquireTimelineXrefs -v
```
Expected: PASS (1 test).

- [ ] **Step 5: Run the full suite (byte-stability guard)**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS — `acquire` is not exercised by the golden/fold fixtures, and the conditional key adds nothing when no cross-repo timeline ref exists.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): acquire captures cross-repo timeline xrefs (S2)"
```

---

## Task 7: `fold_bundle` emits cross-repo edges + records external refs

Wire the `members` param (Task 2) to behavior: for each PR, resolve qualified body refs (Task 4) + captured timeline xrefs (Task 6). A ref to a **member** repo becomes a cross-repo `closes`/`cross_ref` spine edge; a ref to a **non-member** repo is recorded on the node's `data` as an `external_refs` entry (honest, not a fetchable gap). Gated on `members is not None`, so single-repo folds are byte-identical.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add `_cross_repo_pr_edges` helper; extend the PR loop in `fold_bundle`, lines ~2052-2060)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
def _xrepo_fold_bundle():
    """A PR in member 'Azure/mod-a' that closes an issue in member 'Azure/mod-b'
    and mentions a NON-member 'Other/ext' via a qualified body ref."""
    return {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                 "to": "2026-01-31", "clone_sha": "sha-a"},
        "prs": [{
            "number": 10, "url": "u/10", "state": "closed", "merged": True,
            "merged_at": "2026-01-10T00:00:00Z", "created_at": "2026-01-05T00:00:00Z",
            "closed_at": "2026-01-10T00:00:00Z", "closes": [], "crossref_issues": [],
            "title": "feat: cross-module",
            "body": "Closes Azure/mod-b#3\nalso Closes Other/ext#99",
            "timeline_xrefs": [{"owner": "Azure", "repo": "mod-b", "number": 5,
                                "kind": "cross_ref", "is_pr": True}],
        }],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }


class TestFoldCrossRepoEdges(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        self.members = {"Azure/mod-a", "Azure/mod-b"}
        gather.fold_bundle(self.conn, _xrepo_fold_bundle(),
                           project="proj", repo="Azure/mod-a", members=self.members)

    def test_member_close_becomes_cross_repo_edge(self):
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("closes", "proj/Azure/mod-b#issue-3"), types)

    def test_member_timeline_xref_becomes_cross_repo_pr_edge(self):
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        types = {(e["edge_type"], e["dst_id"]) for e in out}
        self.assertIn(("cross_ref", "proj/Azure/mod-b#pr-5"), types)  # is_pr -> pr-

    def test_non_member_ref_is_external_not_edge(self):
        pr = graphstore.get_node(self.conn, "proj/Azure/mod-a#pr-10")
        self.assertEqual(pr["data"]["external_refs"],
                         [{"repo": "Other/ext", "number": 99, "kind": "closes"}])
        # no edge to the non-member
        out = graphstore.get_edges(self.conn, "proj/Azure/mod-a#pr-10",
                                   direction="out")
        dsts = {e["dst_id"] for e in out}
        self.assertNotIn("proj/Other/ext#issue-99", dsts)

    def test_cross_repo_train_traverses_repo_boundary(self):
        # Seed at mod-b's issue; reach mod-a's PR across the repo boundary.
        res = graphstore.traverse_spine(self.conn, ["proj/Azure/mod-b#issue-3"])
        self.assertIn("proj/Azure/mod-a#pr-10", res["reached"])

    def test_single_repo_path_emits_no_external_refs(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _fold_fixture_bundle())   # members=None
        pr = graphstore.get_node(conn, "acme/widget#pr-10")
        self.assertNotIn("external_refs", pr["data"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldCrossRepoEdges -v
```
Expected: FAIL — no `closes` edge to `proj/Azure/mod-b#issue-3` / no `external_refs` key.

- [ ] **Step 3: Add the `_cross_repo_pr_edges` helper**

In `.claude/skills/activity-overview/gather.py`, add just above `def fold_bundle(` (line ~2026):

```python
def _cross_repo_pr_edges(pr, project, current_repo, members):
    """Resolve a PR's cross-repo references (qualified body refs +
    repo-aware timeline xrefs) for a multi-repo project. Returns
    (external_refs, edges):
      - a ref to a MEMBER repo -> an edge tuple (src_pid, dst_id, kind, None, None)
        where kind is 'closes'/'cross_ref' and dst is the target repo's issue/pr;
      - a ref to a NON-member repo -> an external_refs dict {repo, number, kind}.
    Same-repo refs are skipped (the bare-#N path already covers them). Pure."""
    pid = graphstore.qualify_id(project, current_repo, "pr-{}".format(pr["number"]))
    text = (pr.get("title", "") or "") + "\n" + (pr.get("body") or "")
    refs = parse_qualified_refs(text) + list(pr.get("timeline_xrefs") or [])
    edges, external, seen = [], [], set()
    for r in refs:
        slug = "{}/{}".format(r["owner"], r["repo"])
        if slug == current_repo:
            continue
        local = ("pr-" if r.get("is_pr") else "issue-") + str(r["number"])
        key = (slug, local, r["kind"])
        if key in seen:
            continue
        seen.add(key)
        if slug in members:
            edges.append((pid, graphstore.qualify_id(project, slug, local),
                          r["kind"], None, None))
        else:
            external.append({"repo": slug, "number": r["number"], "kind": r["kind"]})
    return external, edges
```

- [ ] **Step 4: Extend the PR loop in `fold_bundle`**

In `fold_bundle`, replace the PR loop (lines ~2052-2060):

```python
    # social: PRs, with closes/cross_ref spine edges to their issues.
    for pr in bundle.get("prs", []):
        pid = qid("pr-{}".format(pr["number"]))
        ts = pr.get("merged_at") or pr.get("closed_at") or pr.get("created_at")
        nodes.append((pid, project, repo, "social", ts, pr, fetched))
        for n in pr.get("closes") or []:
            edges.append((pid, qid("issue-{}".format(n)), "closes", None, None))
        for n in pr.get("crossref_issues") or []:
            edges.append((pid, qid("issue-{}".format(n)), "cross_ref", None, None))
```
with:

```python
    # social: PRs, with closes/cross_ref spine edges to their issues. In a
    # multi-repo project (members given) a PR's qualified refs + repo-aware
    # timeline xrefs also yield CROSS-repo spine edges (to member repos) or
    # honest external_refs (to non-members) — see _cross_repo_pr_edges. Gated on
    # `members is not None` so single-repo folds are byte-identical.
    for pr in bundle.get("prs", []):
        pid = qid("pr-{}".format(pr["number"]))
        ts = pr.get("merged_at") or pr.get("closed_at") or pr.get("created_at")
        if members is not None:
            external, xedges = _cross_repo_pr_edges(pr, project, repo, members)
            if external:
                pr = {**pr, "external_refs": external}
            edges.extend(xedges)
        nodes.append((pid, project, repo, "social", ts, pr, fetched))
        for n in pr.get("closes") or []:
            edges.append((pid, qid("issue-{}".format(n)), "closes", None, None))
        for n in pr.get("crossref_issues") or []:
            edges.append((pid, qid("issue-{}".format(n)), "cross_ref", None, None))
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestFoldCrossRepoEdges -v
```
Expected: PASS (5 tests).

- [ ] **Step 6: Run the full suite (byte-stability guard)**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS — single-repo `fold_bundle` (members=None) is unchanged.

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): cross-repo spine edges + external_refs on fold (S2)"
```

---

## Task 8: Integration — two-repo manifest folds a cross-repo train

End-to-end over `main`: a manifest with two members where member A's PR closes member B's issue produces a single train spanning both repos in one store. Uses a fake `acquire` (no network) returning per-member bundles.

**Files:**
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the test**

Append to `.claude/skills/activity-overview/test_gather.py`:

```python
class TestMultiRepoIntegration(unittest.TestCase):
    def test_two_member_manifest_builds_cross_repo_train(self):
        import tempfile
        man = {
            "project": "avm",
            "window": {"from": "2026-01-01", "to": "2026-01-31"},
            "repos": [{"owner": "Azure", "repo": "mod-a"},
                      {"owner": "Azure", "repo": "mod-b"}],
        }

        def fake_acquire(args, env):
            base = {"meta": {"owner": args.owner, "repo": args.repo,
                             "from": getattr(args, "from"), "to": args.to,
                             "clone_sha": "sha-" + args.repo},
                    "issues": [], "commits": [], "code_events": [],
                    "milestones": [], "releases": [], "code_graph": {"areas": []}}
            if args.repo == "mod-a":
                base["prs"] = [{
                    "number": 10, "url": "u/10", "merged": True,
                    "merged_at": "2026-01-10T00:00:00Z",
                    "created_at": "2026-01-05T00:00:00Z",
                    "closed_at": "2026-01-10T00:00:00Z",
                    "closes": [], "crossref_issues": [],
                    "title": "feat", "body": "Closes Azure/mod-b#3"}]
            else:
                base["prs"] = []
                base["issues"] = [{"number": 3, "url": "u/3", "state": "closed",
                                   "closed_at": "2026-01-08T00:00:00Z",
                                   "updated_at": "2026-01-08T00:00:00Z"}]
            return base

        with tempfile.TemporaryDirectory() as tmp:
            mpath = os.path.join(tmp, "m.json")
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            store = os.path.join(tmp, "j.db")
            orig = gather.acquire
            gather.acquire = fake_acquire
            try:
                gather.main(["--manifest", mpath, "--store", store])
            finally:
                gather.acquire = orig
            conn = graphstore.open_store(store)

        # both windows recorded under the logical project + owner/repo slugs
        windows = graphstore.get_windows(conn)
        self.assertIn({"project": "avm", "repo": "Azure/mod-a",
                       "from": "2026-01-01", "to": "2026-01-31"}, windows)
        self.assertIn({"project": "avm", "repo": "Azure/mod-b",
                       "from": "2026-01-01", "to": "2026-01-31"}, windows)
        # the cross-repo train: B's issue reaches A's PR over the spine
        res = graphstore.traverse_spine(conn, ["avm/Azure/mod-b#issue-3"])
        self.assertIn("avm/Azure/mod-a#pr-10", res["reached"])
```

- [ ] **Step 2: Run test to verify it passes**

```bash
cd .claude/skills/activity-overview && python3 -m pytest test_gather.py -k TestMultiRepoIntegration -v
```
Expected: PASS (1 test) — all the wiring from Tasks 1–7 composes.

> If this fails, the failure localizes the defect: a missing window points at Task 3 (the main loop / `record_window` under override); a missing reach points at Task 7 (cross-repo edge) or Task 2 (override qualification).

- [ ] **Step 3: Run the full suite**

```bash
cd .claude/skills/activity-overview && python3 -m pytest -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/activity-overview/test_gather.py
git commit -m "test(activity): two-repo manifest cross-repo train integration (S2)"
```

---

## Part 1 done — verification checklist

- [ ] `gather --manifest m.json --store j.db` folds every member under one logical project; ids are `{project}/{owner/repo}#{local}`.
- [ ] Member→member qualified body refs and repo-aware timeline xrefs are `closes`/`cross_ref` spine edges; `traverse_spine` walks them across repos.
- [ ] Member→non-member refs are recorded as `external_refs` on the PR node, never as edges/gaps.
- [ ] The full existing suite passes unchanged (single-repo path byte-stable).

---

## Roadmap — Parts 2–4 (separate plans, written after Part 1 lands)

These build on Part 1 and each is a standalone testable increment. They get their own plans because each has an open design point best fixed against the *landed* Part 1 code rather than guessed now.

- **Part 2 — S3: reader + validate aggregation.** Generalize `extract.extract` to a member set (`range_query`/new `graphstore.project_nodes` over `repo IN (…)`; people already aggregate via the `"*"` sentinel) and make `validate.py` self-source over the full multi-repo window. **Open design point:** the materialized `artifacts`/`code_events`/`symbol_events` are keyed by *local* id and **collide across repos** (two repos each have `art:main.tf`). The plan must first choose a cross-repo keying scheme (e.g. repo-qualified projection keys) and trace it through `link`/`render` consumers — a small design pass before the TDD tasks.
- **Part 3 — S4: cross-repo Terraform `depends_on`.** Extend `build_terraform_edges.resolve()` so a registry source resolves to a member repo by manifest `registry` (exact) then HashiCorp naming convention (`namespace/name/provider` ↔ `namespace/terraform-provider-name`), emit cross-repo `depends_on` (area→area) edges, extend `render`'s `module_graph` to span members, and add a `spotlight` reverse-dependency (blast-radius) query.
- **Part 4 — S5: real-data trust gate + docs.** Prove on a small real AVM-TF constellation (cross-repo trains form, cross-repo deps resolve, `validate.py` green on the multi-repo store); update `SKILL.md`/`STORE.md`/`REFERENCE.md`.

---

## Self-Review (against the spec)

- **S1 coverage:** manifest (Task 1), identity override (Task 2), multi-repo gather loop (Task 3) ✓.
- **S2 coverage:** qualified body refs (Task 4), repo-aware timeline (Tasks 5–6), member→member edges + member→non-member `external_refs` (Task 7), cross-repo train traversal (Tasks 7–8) ✓.
- **S3/S4/S5:** explicitly deferred to Parts 2–4 with rationale (not placeholders) ✓.
- **Type consistency:** `parse_qualified_refs` and `parse_timeline_xrefs` both emit `{owner, repo, number, kind, is_pr}`; `_cross_repo_pr_edges` consumes exactly those keys and emits 5-tuple edges `(src, dst, type, None, None)` matching `fold_bundle`'s existing `edges` shape; `external_refs` entries are `{repo, number, kind}` (asserted in Task 7) ✓.
- **Byte-stability:** every `fold_bundle`/`acquire` change is gated (`members is not None`) or conditional-key, with a full-suite guard step in Tasks 2, 3, 6, 7 ✓.
