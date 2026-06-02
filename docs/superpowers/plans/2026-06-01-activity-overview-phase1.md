# Activity-Overview — Phase 1 (Walking Skeleton) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first complete vertical slice of the `activity-overview` skill: acquire a bounded clone + minimal GitHub API into a self-describing JSON bundle, link it offline into basic decision-trains and a coarse `shipped` bucket, and ship the agent-facing docs that render a verifiable digest.

**Architecture:** Two deterministic Python CLIs over a single on-disk bundle. `gather.py` (the only network-touching layer) shells out to `git` and calls the GitHub REST API, normalizes results into a schema-complete bundle (later-phase fields reserved empty). `link.py` is pure offline transforms: resolve commit↔PR, group into trains with deterministic ids, compute the `shipped` bucket, write the enriched bundle back. Claude renders the digest from the bundle via `SKILL.md` + `report-template.md`. Every network/`git`/IO boundary is isolated behind a seam (an injectable callable) so all logic is unit-tested offline against recorded fixtures.

**Tech Stack:** Python 3 standard library only (`argparse`, `json`, `urllib`, `subprocess`, `datetime`, `re`). Tests use stdlib `unittest` (pytest is intentionally avoided — the skill must run with zero pip installs, and pytest is not present in the environment). External binaries `git` and `graphify` are runtime deps of the real CLI but are **not** exercised by the test suite.

**Test command (used throughout):**
```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

**Scope note:** Phase 1 only. Reserved-but-empty bundle fields (`timeline`, `artifacts`, `feature_deltas`, `people`, `halls`, `flow`, `blockers`, `diagrams`, `code_graph`, `project`, `sprints`, etc.) are populated in later phases. Do not implement them here — just reserve their place so the schema is stable from day one.

---

## File Structure

All paths under `.claude/skills/activity-overview/`:

- `gather.py` — Acquire. Pure helpers (`build_bundle`, `parse_git_log`, `build_clone_cmd`, `in_window`, `parse_closing_refs`, `normalize_pr`, `normalize_issue`, `select_merged_prs`, `fetch_all`, `parse_args`, `resolve_token`) + thin IO wrappers (`run_git`, `http_get_json`) + `main`.
- `link.py` — offline transforms (`ref`, `resolve_commit_pr`, `attach_commit_prs`, `build_trains`, `compute_buckets`, `enrich`, `main`).
- `test_gather.py` — unit tests for every pure `gather.py` helper.
- `test_link.py` — unit tests for every pure `link.py` transform + a provenance-lint test + an end-to-end offline assembly test.
- `fixtures/git_log_sample.txt` — recorded `git log` output (records separated by `\x1e`, fields by `\x1f`).
- `fixtures/rest_sample.json` — recorded REST responses: `{ "pulls": [...], "issues": { "<n>": {...} } }`.
- `fixtures/bundle_sample.json` — a Phase-1 pre-link bundle, input to `link.py` transform tests.
- `SKILL.md` — procedure + render instructions.
- `report-template.md` — fixed Phase-1 report shape.
- `commands/activity.md` — `/activity` slash-command wrapper.
- `projects.example.json` — config template (placeholders).
- `BUNDLE.md` — bundle schema + ref convention for downstream renderer authors.
- `REFERENCE.md` — install + usage + troubleshooting.

---

## Task 1: Scaffold + bundle skeleton builder

**Files:**
- Create: `.claude/skills/activity-overview/gather.py`
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Create `.claude/skills/activity-overview/test_gather.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestBuildBundle(unittest.TestCase):
    def test_skeleton_has_all_top_level_keys_and_reserved_empties(self):
        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, commits=[{"sha": "abc"}],
                                     prs=[{"number": 42}], issues=[{"number": 17}])

        # supplied data is carried through
        self.assertEqual(bundle["meta"]["owner"], "o")
        self.assertEqual(bundle["commits"], [{"sha": "abc"}])
        self.assertEqual(bundle["prs"], [{"number": 42}])
        self.assertEqual(bundle["issues"], [{"number": 17}])

        # later-phase fields are reserved but empty
        for key in ["timeline", "feature_deltas", "trains", "blockers",
                    "releases", "milestones", "docsRefs"]:
            self.assertEqual(bundle[key], [], f"{key} should be reserved empty list")
        for key in ["artifacts", "people", "modules", "code_owners", "flow",
                    "label_taxonomy", "diagrams", "workflow_stats"]:
            self.assertEqual(bundle[key], {}, f"{key} should be reserved empty dict")
        self.assertEqual(
            bundle["buckets"],
            {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        )
        self.assertIn("schema_version", bundle["meta"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gather'` (file does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `.claude/skills/activity-overview/gather.py`:

```python
"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""

SCHEMA_VERSION = 1


def build_bundle(meta, commits, prs, issues):
    """Assemble the on-disk bundle skeleton.

    Phase 1 fills meta/commits/prs/issues; every other top-level field is
    reserved with an empty value so the schema is stable across phases.
    """
    meta = dict(meta)
    meta.setdefault("schema_version", SCHEMA_VERSION)
    return {
        "meta": meta,
        "commits": commits,
        "prs": prs,
        "issues": issues,
        # --- reserved for later phases (empty, schema-stable) ---
        "timeline": [],
        "artifacts": {},
        "feature_deltas": [],
        "trains": [],
        "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        "people": {},
        "halls": {},
        "flow": {},
        "blockers": [],
        "code_owners": {},
        "code_graph": {},
        "label_taxonomy": {},
        "modules": {},
        "workflow_stats": {},
        "workflows": [],
        "releases": [],
        "milestones": [],
        "docsRefs": [],
        "release_train": {},
        "sprints": {},
        "project": {},
        "diagrams": {},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): bundle skeleton builder with reserved schema fields"
```

---

## Task 2: git log parser

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`
- Create: `.claude/skills/activity-overview/fixtures/git_log_sample.txt`
- Test: `.claude/skills/activity-overview/test_gather.py`

The real CLI runs:
`git log <range> --pretty=format:'%x1e%H%x1f%P%x1f%an%x1f%ad%x1f%s' --date=short --name-only`
Each commit record begins with the `\x1e` record separator; fields are `\x1f`-separated; file paths follow on their own lines (merge commits list no files).

- [ ] **Step 1: Create the fixture (explicit control bytes)**

Run this exact command to write `fixtures/git_log_sample.txt` with real `\x1e`/`\x1f` bytes:

```bash
mkdir -p .claude/skills/activity-overview/fixtures
python3 - <<'PY'
RS, FS = "\x1e", "\x1f"
records = [
    # sha, parents, author, date, subject, [files]
    ("a1"*20, "p1"*20, "Alice", "2026-05-10", "Add policy param (#42)",
     ["modules/firewall/main.bicep", "modules/firewall/README.md"]),
    ("b2"*20, "p2"*20 + " " + "p3"*20, "Bob", "2026-05-10",
     "Merge pull request #42 from feature/policy", []),
    ("c3"*20, "p4"*20, "Carol", "2026-05-12", "Tidy outputs",
     ["modules/firewall/outputs.bicep"]),
]
chunks = []
for sha, parents, author, date, subject, files in records:
    line = RS + FS.join([sha, parents, author, date, subject])
    body = "".join("\n" + f for f in files)
    chunks.append(line + body)
out = "".join(chunks) + "\n"
with open(".claude/skills/activity-overview/fixtures/git_log_sample.txt", "w") as fh:
    fh.write(out)
print("wrote", len(out), "bytes")
PY
```

- [ ] **Step 2: Write the failing test**

Append to `test_gather.py` (inside the file, before `if __name__`):

```python
class TestParseGitLog(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "git_log_sample.txt")) as fh:
            self.raw = fh.read()

    def test_parses_three_commits_in_order(self):
        commits = gather.parse_git_log(self.raw)
        self.assertEqual(len(commits), 3)
        self.assertEqual(commits[0]["sha"], "a1" * 20)
        self.assertEqual(commits[0]["author"], "Alice")
        self.assertEqual(commits[0]["date"], "2026-05-10")
        self.assertEqual(commits[0]["message"], "Add policy param (#42)")
        self.assertEqual(
            commits[0]["files"],
            ["modules/firewall/main.bicep", "modules/firewall/README.md"],
        )

    def test_merge_commit_has_two_parents_and_no_files(self):
        commits = gather.parse_git_log(self.raw)
        merge = commits[1]
        self.assertEqual(len(merge["parents"]), 2)
        self.assertEqual(merge["files"], [])

    def test_empty_input_yields_no_commits(self):
        self.assertEqual(gather.parse_git_log(""), [])
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'parse_git_log'`.

- [ ] **Step 4: Write minimal implementation**

Add to `gather.py`:

```python
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


def parse_git_log(raw):
    """Parse `git log` output formatted with RECORD_SEP/FIELD_SEP separators.

    Each record: <sha>\x1f<parents>\x1f<author>\x1f<date>\x1f<subject> followed by
    newline-separated file paths. Returns a list of commit dicts.
    """
    commits = []
    for chunk in raw.split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, parents, author, date, subject = fields[:5]
        files = [ln for ln in lines[1:] if ln.strip()]
        commits.append({
            "sha": sha,
            "parents": parents.split() if parents.strip() else [],
            "author": author,
            "date": date,
            "message": subject,
            "files": files,
            "pr": None,  # resolved in link.py
        })
    return commits
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py .claude/skills/activity-overview/fixtures/git_log_sample.txt
git commit -m "feat(activity): parse git log records into commit dicts"
```

---

## Task 3: clone command + window helper

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestCloneAndWindow(unittest.TestCase):
    def test_build_clone_cmd_is_bounded_and_partial(self):
        cmd = gather.build_clone_cmd("https://github.com/o/r.git",
                                     "2026-05-01", "/tmp/clone")
        self.assertEqual(cmd[0], "git")
        self.assertIn("clone", cmd)
        self.assertIn("--filter=blob:none", cmd)
        self.assertIn("--shallow-since=2026-05-01", cmd)
        self.assertIn("--no-single-branch", cmd)
        self.assertEqual(cmd[-2:], ["https://github.com/o/r.git", "/tmp/clone"])

    def test_in_window_inclusive_bounds(self):
        self.assertTrue(gather.in_window("2026-05-01", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-31", "2026-05-01", "2026-05-31"))
        self.assertTrue(gather.in_window("2026-05-15T08:00:00Z", "2026-05-01", "2026-05-31"))

    def test_in_window_rejects_outside_and_none(self):
        self.assertFalse(gather.in_window("2026-04-30", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window("2026-06-01", "2026-05-01", "2026-05-31"))
        self.assertFalse(gather.in_window(None, "2026-05-01", "2026-05-31"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'build_clone_cmd'`.

- [ ] **Step 3: Write minimal implementation**

Add to `gather.py`:

```python
def build_clone_cmd(repo_url, from_date, clone_dir):
    """Construct the bounded, partial clone command (network-free to build)."""
    return [
        "git", "clone",
        "--filter=blob:none",
        f"--shallow-since={from_date}",
        "--no-single-branch",
        repo_url, clone_dir,
    ]


def in_window(ts, from_date, to_date):
    """True if ISO date/datetime string `ts` falls within [from_date, to_date]
    inclusive, comparing on the date prefix. None/empty is never in window."""
    if not ts:
        return False
    day = ts[:10]
    return from_date <= day <= to_date
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): clone command builder + window predicate"
```

---

## Task 4: closing-ref parser + PR normalization + merged selection

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`
- Create: `.claude/skills/activity-overview/fixtures/rest_sample.json`
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Create the fixture**

Create `.claude/skills/activity-overview/fixtures/rest_sample.json`:

```json
{
  "pulls": [
    {
      "number": 42,
      "title": "Add AzureFirewall policy param",
      "body": "Implements the policy parameter. Fixes #17.",
      "state": "closed",
      "merged_at": "2026-05-10T12:00:00Z",
      "closed_at": "2026-05-10T12:00:00Z",
      "user": {"login": "alice"},
      "author_association": "MEMBER",
      "labels": [{"name": "enhancement"}],
      "merged_by": {"login": "bob"},
      "html_url": "https://github.com/o/r/pull/42"
    },
    {
      "number": 43,
      "title": "WIP experiment",
      "body": "no references here",
      "state": "closed",
      "merged_at": null,
      "closed_at": "2026-05-11T09:00:00Z",
      "user": {"login": "carol"},
      "author_association": "CONTRIBUTOR",
      "labels": [],
      "merged_by": null,
      "html_url": "https://github.com/o/r/pull/43"
    },
    {
      "number": 44,
      "title": "Still open",
      "body": "Resolves #18 and closes #19",
      "state": "open",
      "merged_at": null,
      "closed_at": null,
      "user": {"login": "alice"},
      "author_association": "MEMBER",
      "labels": [],
      "merged_by": null,
      "html_url": "https://github.com/o/r/pull/44"
    },
    {
      "number": 41,
      "title": "Merged before the window",
      "body": "Fixes #10",
      "state": "closed",
      "merged_at": "2026-04-20T12:00:00Z",
      "closed_at": "2026-04-20T12:00:00Z",
      "user": {"login": "alice"},
      "author_association": "MEMBER",
      "labels": [],
      "merged_by": {"login": "bob"},
      "html_url": "https://github.com/o/r/pull/41"
    }
  ],
  "issues": {
    "17": {
      "number": 17,
      "title": "Support firewall policy parameter",
      "body": "We need a policy param.",
      "state": "closed",
      "state_reason": "completed",
      "user": {"login": "dave"},
      "author_association": "CONTRIBUTOR",
      "labels": [{"name": "enhancement"}],
      "assignees": [{"login": "alice"}],
      "closed_at": "2026-05-10T12:00:00Z",
      "html_url": "https://github.com/o/r/issues/17"
    }
  }
}
```

- [ ] **Step 2: Write the failing test**

Append to `test_gather.py`:

```python
import json


class TestPrNormalization(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            self.data = json.load(fh)

    def test_parse_closing_refs_all_keywords(self):
        self.assertEqual(gather.parse_closing_refs("Fixes #17"), [17])
        self.assertEqual(
            gather.parse_closing_refs("Resolves #18 and closes #19"), [18, 19]
        )
        self.assertEqual(gather.parse_closing_refs("no references here"), [])
        # de-duplicates while preserving order
        self.assertEqual(gather.parse_closing_refs("fix #5 fixed #5"), [5])

    def test_normalize_pr_maps_fields_and_parses_closes(self):
        pr = gather.normalize_pr(self.data["pulls"][0])
        self.assertEqual(pr["number"], 42)
        self.assertEqual(pr["author"], "alice")
        self.assertEqual(pr["author_association"], "MEMBER")
        self.assertTrue(pr["merged"])
        self.assertEqual(pr["merged_by"], "bob")
        self.assertEqual(pr["labels"], ["enhancement"])
        self.assertEqual(pr["closes"], [17])
        self.assertEqual(pr["url"], "https://github.com/o/r/pull/42")

    def test_normalize_pr_unmerged_has_merged_false(self):
        pr = gather.normalize_pr(self.data["pulls"][1])
        self.assertFalse(pr["merged"])
        self.assertIsNone(pr["merged_by"])

    def test_select_merged_prs_only_in_window(self):
        prs = [gather.normalize_pr(p) for p in self.data["pulls"]]
        merged = gather.select_merged_prs(prs, "2026-05-01", "2026-05-31")
        self.assertEqual([p["number"] for p in merged], [42])
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'parse_closing_refs'`.

- [ ] **Step 4: Write minimal implementation**

Add to `gather.py` (add `import re` at the top of the file):

```python
import re

_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)


def parse_closing_refs(text):
    """Extract issue numbers from GitHub closing keywords, de-duplicated,
    order-preserving."""
    out = []
    for m in _CLOSING_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


def normalize_pr(raw):
    """Map a GitHub REST PR object to the bundle's PR shape."""
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "merged": bool(raw.get("merged_at")),
        "merged_by": (raw.get("merged_by") or {}).get("login")
        if raw.get("merged_by") else None,
        "merged_at": raw.get("merged_at"),
        "closed_at": raw.get("closed_at"),
        "state": raw.get("state"),
        "closes": parse_closing_refs(
            (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")
        ),
        "url": raw.get("html_url"),
    }


def select_merged_prs(prs, from_date, to_date):
    """Return normalized PRs merged within [from_date, to_date]."""
    return [p for p in prs if p["merged"] and in_window(p["merged_at"], from_date, to_date)]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py .claude/skills/activity-overview/fixtures/rest_sample.json
git commit -m "feat(activity): closing-ref parser, PR normalization, merged-in-window selection"
```

---

## Task 5: issue normalization + paginated fetch seam

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`
- Test: `.claude/skills/activity-overview/test_gather.py`

The real `main` passes a live `http_get_json` into `fetch_all`. Tests pass a fake to prove pagination + assembly without any network.

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestIssueAndFetch(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            self.data = json.load(fh)

    def test_normalize_issue_maps_kind_and_state_reason(self):
        issue = gather.normalize_issue(self.data["issues"]["17"])
        self.assertEqual(issue["number"], 17)
        self.assertEqual(issue["author"], "dave")
        self.assertEqual(issue["state_reason"], "completed")
        self.assertEqual(issue["labels"], ["enhancement"])
        self.assertEqual(issue["assignees"], ["alice"])
        self.assertEqual(issue["url"], "https://github.com/o/r/issues/17")

    def test_fetch_all_follows_pages_until_short_page(self):
        pages = {
            "u?page=1": (["a", "b"], "u?page=2"),
            "u?page=2": (["c"], None),
        }
        calls = []

        def fake_get(url):
            calls.append(url)
            body, nxt = pages[url]
            return body, nxt

        items = gather.fetch_all(fake_get, "u?page=1")
        self.assertEqual(items, ["a", "b", "c"])
        self.assertEqual(calls, ["u?page=1", "u?page=2"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'normalize_issue'`.

- [ ] **Step 3: Write minimal implementation**

Add to `gather.py`:

```python
def normalize_issue(raw):
    """Map a GitHub REST issue object to the bundle's issue shape."""
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "kind": "other",  # refined in later phases (issue types/labels/template)
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "assignees": [a["login"] for a in raw.get("assignees", [])],
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "closed_at": raw.get("closed_at"),
        "url": raw.get("html_url"),
    }


def fetch_all(get_page, first_url):
    """Walk a paginated endpoint. `get_page(url)` returns (items, next_url|None).
    Network/parse details live in the caller's closure, so this is testable with
    a fake."""
    items = []
    url = first_url
    while url:
        page_items, url = get_page(url)
        items.extend(page_items)
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): issue normalization + paginated fetch seam"
```

---

## Task 6: CLI parsing, auth resolution, and IO wiring

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Append to `test_gather.py`:

```python
class TestCliAndAuth(unittest.TestCase):
    def test_parse_args_required_and_defaults(self):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r",
            "--from", "2026-05-01", "--to", "2026-05-31",
        ])
        self.assertEqual(args.owner, "o")
        self.assertEqual(args.repo, "r")
        self.assertEqual(getattr(args, "from"), "2026-05-01")
        self.assertEqual(args.to, "2026-05-31")
        self.assertEqual(args.branches, "main")
        self.assertFalse(args.no_clone)

    def test_resolve_token_prefers_github_token(self):
        self.assertEqual(
            gather.resolve_token({"GITHUB_TOKEN": "gh", "GH_TOKEN": "alt"}), "gh"
        )

    def test_resolve_token_falls_back_to_gh_token(self):
        self.assertEqual(gather.resolve_token({"GH_TOKEN": "alt"}), "alt")

    def test_resolve_token_missing_raises(self):
        with self.assertRaises(SystemExit):
            gather.resolve_token({})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'gather' has no attribute 'parse_args'`.

- [ ] **Step 3: Write minimal implementation**

Add to `gather.py` (add `import argparse`, `import json`, `import os`, `import subprocess`, `import sys`, `import urllib.request`, `import urllib.parse` at top alongside `import re`):

```python
import argparse
import json
import os
import subprocess
import sys
import urllib.request


def parse_args(argv):
    p = argparse.ArgumentParser(description="Acquire an activity-overview bundle.")
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--from", dest="from", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--branches", default="main")
    p.add_argument("--clone-dir", default=None)
    p.add_argument("--no-clone", action="store_true")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def resolve_token(env):
    """Return a GitHub token from env, preferring GITHUB_TOKEN. Exit if absent."""
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        sys.stderr.write(
            "error: set GITHUB_TOKEN (or GH_TOKEN) with repo + read:project scope\n"
        )
        raise SystemExit(2)
    return token


def run_git(args, cwd=None):
    """Thin wrapper around git (not unit-tested)."""
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def http_get_json(url, token):
    """GET a GitHub API URL → (parsed_json, next_url). Not unit-tested."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "activity-overview",
    })
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode())
        link = resp.headers.get("Link", "")
        nxt = _next_link(link)
    return body, nxt


def _next_link(link_header):
    """Parse a GitHub Link header, returning the rel="next" url or None."""
    for part in (link_header or "").split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if 'rel="next"' in section[1]:
            return url
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): CLI parsing, token resolution, git/http IO wrappers"
```

---

## Task 7: `main` wiring for gather (manual end-to-end, no unit test)

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py`

This task wires the seams into a runnable `main`. It is not unit-tested (it is pure IO orchestration over already-tested helpers); it is exercised by the manual smoke check in Task 12.

- [ ] **Step 1: Add `main` to `gather.py`**

```python
def _paginated(token):
    """Adapter: turn http_get_json into the (items, next) shape fetch_all wants."""
    def get_page(url):
        return http_get_json(url, token)
    return get_page


def gather(args, env):
    token = resolve_token(env)
    owner, repo = args.owner, args.repo
    frm, to = getattr(args, "from"), args.to
    clone_dir = args.clone_dir or f"workspace/{repo}-clone"
    repo_url = f"https://github.com/{owner}/{repo}.git"

    if not args.no_clone:
        run_git(build_clone_cmd(repo_url, frm, clone_dir))

    log_fmt = "%x1e%H%x1f%P%x1f%an%x1f%ad%x1f%s"
    raw = run_git([
        "git", "-C", clone_dir, "log",
        f"--since={frm}", f"--until={to}",
        f"--pretty=format:{log_fmt}", "--date=short", "--name-only",
    ])
    commits = parse_git_log(raw)

    get_page = _paginated(token)
    api = f"https://api.github.com/repos/{owner}/{repo}"
    raw_pulls = fetch_all(
        get_page, f"{api}/pulls?state=all&sort=updated&direction=desc&per_page=100"
    )
    prs = select_merged_prs([normalize_pr(p) for p in raw_pulls], frm, to)

    issues = []
    seen = set()
    for pr in prs:
        for n in pr["closes"]:
            if n in seen:
                continue
            seen.add(n)
            raw_issue, _ = http_get_json(f"{api}/issues/{n}", token)
            issues.append(normalize_issue(raw_issue))

    meta = {
        "owner": owner, "repo": repo, "from": frm, "to": to,
        "branches": args.branches.split(","), "clone_dir": clone_dir,
        "period": {"from": frm, "to": to}, "prev_bundle": None,
    }
    return build_bundle(meta, commits, prs, issues)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    bundle = gather(args, os.environ)
    out = args.out or f"workspace/activity-{getattr(args, 'from')}-{args.to}.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(f"wrote {out}\n")
    return out


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the module still imports and tests pass**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (17 tests — `main` adds no unit tests but must not break imports).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/gather.py
git commit -m "feat(activity): wire gather main (clone -> log -> REST -> bundle)"
```

---

## Task 8: link.py — commit↔PR resolution

**Files:**
- Create: `.claude/skills/activity-overview/link.py`
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Create `.claude/skills/activity-overview/test_link.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402


class TestCommitPrResolution(unittest.TestCase):
    def test_resolve_commit_pr_from_squash_subject(self):
        self.assertEqual(link.resolve_commit_pr("Add policy param (#42)"), 42)

    def test_resolve_commit_pr_from_merge_subject(self):
        self.assertEqual(
            link.resolve_commit_pr("Merge pull request #42 from feature/policy"), 42
        )

    def test_resolve_commit_pr_none_when_absent(self):
        self.assertIsNone(link.resolve_commit_pr("Tidy outputs"))

    def test_attach_commit_prs_sets_pr_field(self):
        commits = [
            {"sha": "a", "message": "Add policy param (#42)", "pr": None},
            {"sha": "b", "message": "Tidy outputs", "pr": None},
        ]
        link.attach_commit_prs(commits)
        self.assertEqual(commits[0]["pr"], 42)
        self.assertIsNone(commits[1]["pr"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'link'`.

- [ ] **Step 3: Write minimal implementation**

Create `.claude/skills/activity-overview/link.py`:

```python
"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import re
import sys

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

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (4 new tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "feat(activity): resolve commit->PR from subjects"
```

---

## Task 9: link.py — train builder

**Files:**
- Modify: `.claude/skills/activity-overview/link.py`
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Append to `test_link.py` (before `if __name__`):

```python
def _sample_bundle():
    return {
        "commits": [
            {"sha": "a", "message": "Add policy param (#42)", "pr": None},
            {"sha": "b", "message": "Merge pull request #42 from x", "pr": None},
            {"sha": "c", "message": "Tidy outputs", "pr": None},
        ],
        "prs": [
            {"number": 42, "title": "Add policy param", "merged": True,
             "closes": [17], "url": "https://github.com/o/r/pull/42"},
        ],
        "issues": [
            {"number": 17, "title": "Support policy param", "kind": "feature",
             "state": "closed", "state_reason": "completed",
             "url": "https://github.com/o/r/issues/17"},
        ],
    }


class TestBuildTrains(unittest.TestCase):
    def test_train_id_uses_root_issue(self):
        bundle = _sample_bundle()
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(len(trains), 1)
        t = trains[0]
        self.assertEqual(t["id"], "train-issue-17")
        self.assertEqual(t["root_issue"], 17)
        self.assertEqual(t["prs"], [42])
        self.assertEqual(sorted(t["commits"]), ["a", "b"])
        self.assertEqual(t["outcome"], "shipped")
        self.assertEqual(t["kind"], "feature")

    def test_train_id_falls_back_to_pr_when_issueless(self):
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["id"], "train-pr-42")
        self.assertIsNone(trains[0]["root_issue"])

    def test_train_evidence_refs_are_well_formed(self):
        bundle = _sample_bundle()
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        for ev in trains[0]["evidence"]:
            self.assertIn("type", ev)
            self.assertIn("id", ev)
            self.assertTrue(ev["url"].startswith("https://"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'link' has no attribute 'build_trains'`.

- [ ] **Step 3: Write minimal implementation**

Add to `link.py`:

```python
def ref(type_, id_, url):
    """A provenance reference: every narrative-bearing fact resolves to one."""
    return {"type": type_, "id": id_, "url": url}


def build_trains(bundle):
    """Group merged PRs (+ their commits + closing issue) into decision trains.

    Train id is deterministic from its anchor: the root issue number when the PR
    closes one (`train-issue-<n>`), else the PR number (`train-pr-<n>`).
    """
    commits_by_pr = {}
    for c in bundle["commits"]:
        commits_by_pr.setdefault(c.get("pr"), []).append(c["sha"])
    issues_by_num = {i["number"]: i for i in bundle["issues"]}

    # Group merged PRs by anchor so multiple PRs on one issue share a train.
    groups = {}
    for pr in bundle["prs"]:
        if not pr.get("merged"):
            continue
        root = pr["closes"][0] if pr.get("closes") else None
        anchor = ("issue", root) if root is not None else ("pr", pr["number"])
        groups.setdefault(anchor, []).append(pr)

    trains = []
    for (kind, key), prs in groups.items():
        prs = sorted(prs, key=lambda p: p["number"])
        pr_numbers = [p["number"] for p in prs]
        shas = []
        evidence = []
        for p in prs:
            shas.extend(commits_by_pr.get(p["number"], []))
            evidence.append(ref("pr", p["number"], p["url"]))
        root_issue = key if kind == "issue" else None
        train_kind = "other"
        if root_issue is not None and root_issue in issues_by_num:
            issue = issues_by_num[root_issue]
            train_kind = issue.get("kind", "other")
            evidence.insert(0, ref("issue", root_issue, issue["url"]))
        trains.append({
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": train_kind,
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": sorted(shas),
            "outcome": "shipped",
            "evidence": evidence,
        })
    return sorted(trains, key=lambda t: t["id"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (3 new tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "feat(activity): build decision trains with deterministic ids"
```

---

## Task 10: link.py — shipped bucket, enrich, main

**Files:**
- Modify: `.claude/skills/activity-overview/link.py`
- Create: `.claude/skills/activity-overview/fixtures/bundle_sample.json`
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Create the fixture**

Create `.claude/skills/activity-overview/fixtures/bundle_sample.json` (a Phase-1 pre-link bundle):

```json
{
  "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
  "commits": [
    {"sha": "a", "message": "Add policy param (#42)", "pr": null},
    {"sha": "b", "message": "Merge pull request #42 from x", "pr": null},
    {"sha": "c", "message": "Tidy outputs", "pr": null}
  ],
  "prs": [
    {"number": 42, "title": "Add policy param", "merged": true, "closes": [17],
     "url": "https://github.com/o/r/pull/42"}
  ],
  "issues": [
    {"number": 17, "title": "Support policy param", "kind": "feature",
     "state": "closed", "state_reason": "completed",
     "url": "https://github.com/o/r/issues/17"}
  ],
  "trains": [],
  "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
}
```

- [ ] **Step 2: Write the failing test**

Append to `test_link.py`:

```python
import json

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestBucketsAndEnrich(unittest.TestCase):
    def test_shipped_bucket_has_merged_prs_and_completed_issues(self):
        bundle = _sample_bundle()
        bundle.setdefault("buckets", {"shipped": [], "in_flight": [],
                                      "rejected": [], "next_candidates": []})
        link.attach_commit_prs(bundle["commits"])
        buckets = link.compute_buckets(bundle)
        kinds = {(r["type"], r["id"]) for r in buckets["shipped"]}
        self.assertIn(("pr", 42), kinds)
        self.assertIn(("issue", 17), kinds)

    def test_enrich_is_idempotent_and_populates_both(self):
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = json.load(fh)
        once = link.enrich(bundle)
        self.assertEqual(once["trains"][0]["id"], "train-issue-17")
        self.assertTrue(once["buckets"]["shipped"])
        # running again yields the same trains (deterministic, no duplication)
        twice = link.enrich(once)
        self.assertEqual(
            [t["id"] for t in once["trains"]],
            [t["id"] for t in twice["trains"]],
        )
        self.assertEqual(len(once["trains"]), len(twice["trains"]))


if __name__ == "__main__":
    unittest.main()
```

Note: remove the now-duplicate trailing `if __name__ == "__main__"` block left over from Task 8 so the file has exactly one at the very end.

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: FAIL — `AttributeError: module 'link' has no attribute 'compute_buckets'`.

- [ ] **Step 4: Write minimal implementation**

Add to `link.py`:

```python
def compute_buckets(bundle):
    """Coarse Phase-1 buckets: shipped = merged PRs + completed issues."""
    shipped = []
    for pr in bundle["prs"]:
        if pr.get("merged"):
            shipped.append(ref("pr", pr["number"], pr["url"]))
    for issue in bundle["issues"]:
        if issue.get("state") == "closed" and issue.get("state_reason") == "completed":
            shipped.append(ref("issue", issue["number"], issue["url"]))
    return {"shipped": shipped, "in_flight": [], "rejected": [], "next_candidates": []}


def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets."""
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    return bundle


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write("usage: link.py BUNDLE.json\n")
        raise SystemExit(2)
    path = argv[0]
    with open(path) as fh:
        bundle = json.load(fh)
    enrich(bundle)
    with open(path, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(
        f"linked {len(bundle['trains'])} trains, "
        f"{len(bundle['buckets']['shipped'])} shipped into {path}\n"
    )
    return path


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (2 new tests; full suite green).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py .claude/skills/activity-overview/fixtures/bundle_sample.json
git commit -m "feat(activity): shipped bucket + enrich + link main"
```

---

## Task 11: Provenance lint + end-to-end offline assembly test

**Files:**
- Test: `.claude/skills/activity-overview/test_link.py`

This task adds the spec's **provenance lint** (every narrative-bearing fact carries a well-formed ref) and a no-network end-to-end test that runs the gather *assembly* helpers (not the IO) straight into link.

- [ ] **Step 1: Write the failing test**

Append to `test_link.py` (before the final `if __name__`):

```python
import gather  # noqa: E402


def _well_formed(r):
    return (
        isinstance(r, dict)
        and isinstance(r.get("type"), str)
        and r.get("id") is not None
        and isinstance(r.get("url"), str)
        and r["url"].startswith("https://")
    )


class TestProvenanceAndEndToEnd(unittest.TestCase):
    def test_every_train_and_bucket_ref_is_well_formed(self):
        with open(os.path.join(FIX, "bundle_sample.json")) as fh:
            bundle = link.enrich(json.load(fh))
        for t in bundle["trains"]:
            self.assertTrue(t["evidence"], "train must carry evidence")
            for ev in t["evidence"]:
                self.assertTrue(_well_formed(ev), f"bad ref {ev}")
        for r in bundle["buckets"]["shipped"]:
            self.assertTrue(_well_formed(r), f"bad ref {r}")

    def test_gather_assembly_into_link_offline(self):
        # Build a bundle purely from fixtures — no git, no network.
        with open(os.path.join(FIX, "git_log_sample.txt")) as fh:
            commits = gather.parse_git_log(fh.read())
        with open(os.path.join(FIX, "rest_sample.json")) as fh:
            data = json.load(fh)
        prs = gather.select_merged_prs(
            [gather.normalize_pr(p) for p in data["pulls"]],
            "2026-05-01", "2026-05-31",
        )
        issues = [gather.normalize_issue(data["issues"][str(n)])
                  for p in prs for n in p["closes"] if str(n) in data["issues"]]
        meta = {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"}
        bundle = gather.build_bundle(meta, commits, prs, issues)

        link.enrich(bundle)

        self.assertEqual([p["number"] for p in bundle["prs"]], [42])
        self.assertEqual(bundle["trains"][0]["id"], "train-issue-17")
        shipped = {(r["type"], r["id"]) for r in bundle["buckets"]["shipped"]}
        self.assertEqual(shipped, {("pr", 42), ("issue", 17)})
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: These two tests should **pass immediately** (they exercise already-implemented behavior end-to-end). If either fails, fix the underlying helper — do not weaken the test. This task is the integration safety net.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/test_link.py
git commit -m "test(activity): provenance lint + offline gather->link end-to-end"
```

---

## Task 12: Agent-facing docs (SKILL.md, template, command, configs)

**Files:**
- Create: `.claude/skills/activity-overview/SKILL.md`
- Create: `.claude/skills/activity-overview/report-template.md`
- Create: `.claude/skills/activity-overview/commands/activity.md`
- Create: `.claude/skills/activity-overview/projects.example.json`
- Create: `.claude/skills/activity-overview/BUNDLE.md`
- Create: `.claude/skills/activity-overview/REFERENCE.md`

These are docs, not code — no unit tests. Keep Phase-1 content honest: only describe sections the Phase-1 bundle can actually back.

- [ ] **Step 1: Create `SKILL.md`**

```markdown
---
name: activity-overview
description: Generate a verifiable repository activity digest for a date window — clones the repo (bounded), pulls merged PRs and their closing issues, links them into decision trains, and renders a sourced Markdown report. Use when asked to summarize what shipped in a repo over a period.
---

# Activity Overview

Produce a **fact-based activity digest** for `OWNER/REPO` over `[FROM, TO]`. Every
claim in the report resolves to a source ref in the bundle — never invent facts.

## Procedure

1. **Acquire.** Run the gather CLI (requires `GITHUB_TOKEN` with repo + read:project
   scope, and `git` on PATH):
   ```bash
   python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --out workspace/bundle.json
   ```
   This writes a schema-complete bundle (see `BUNDLE.md`).
2. **Link.** Enrich it offline (no network):
   ```bash
   python3 link.py workspace/bundle.json
   ```
   This adds `trains` and `buckets.shipped`.
3. **Render.** Read `workspace/bundle.json` and fill `report-template.md`. Cite each
   fact with its `url`. Do not state anything the bundle does not contain.

## Rules

- The bundle is the only source of truth. If a fact is not in the bundle, omit it.
- Quote PR/issue numbers and link their `url`.
- Phase 1 reports cover: executive summary, shipped this period, and decision trains.
  Other sections arrive in later phases — leave them out rather than padding.
```

- [ ] **Step 2: Create `report-template.md`**

```markdown
# {repo} — Activity Digest ({from} → {to})

## Executive summary

{N} merged PRs across {M} decision trains. {shipped_count} items shipped.

## Shipped this period

For each ref in `buckets.shipped`, one line: the PR/issue title, number, and link.

- [{title}]({url}) (#{number})

## Decision trains

For each train in `trains`:

### {train.id} — {issue or PR title}

- **Root issue:** #{root_issue} (or "none — PR-anchored")
- **PRs:** {prs}
- **Commits:** {commit count}
- **Outcome:** {outcome}
- **Evidence:** {evidence urls}
```

- [ ] **Step 3: Create `commands/activity.md`**

```markdown
---
description: Generate a repository activity digest for a date window
---

Run the activity-overview skill for the repository and window the user names.

Steps:
1. Resolve OWNER, REPO, FROM, TO from the user's request (ask if missing).
2. Follow `.claude/skills/activity-overview/SKILL.md`: gather → link → render.
3. Return the filled report and the path to the bundle used.

Arguments: $ARGUMENTS
```

- [ ] **Step 4: Create `projects.example.json`**

```json
{
  "projects": {
    "example": {
      "owner": "OWNER",
      "repo": "REPO",
      "branches": ["main"],
      "clone_dir": "workspace/example-clone"
    }
  }
}
```

- [ ] **Step 5: Create `BUNDLE.md`**

```markdown
# Bundle Schema (Phase 1)

The bundle is a single JSON object. Phase 1 populates `meta`, `commits`, `prs`,
`issues`, `trains`, and `buckets`; all other top-level keys are reserved and empty
until later phases.

## Ref convention

Every provenance reference is `{ "type": "pr|issue|commit", "id": <number|sha>,
"url": "https://..." }`. Every narrative-bearing fact (a train, a bucket entry)
resolves to at least one such ref. To fact-check any claim in a report, follow its
ref `url` to GitHub.

## Top-level keys (Phase 1)

- `meta` — `owner, repo, from, to, branches, clone_dir, period, prev_bundle, schema_version`.
- `commits` — `[{ sha, parents, author, date, message, files, pr }]` (`pr` set by link).
- `prs` — merged-in-window PRs: `{ number, title, body, author, author_association,
  labels, merged, merged_by, merged_at, closed_at, state, closes:[issue#], url }`.
- `issues` — closing issues: `{ number, title, body, kind, author, author_association,
  labels, assignees, state, state_reason, closed_at, url }`.
- `trains` — `[{ id, kind, root_issue, prs:[#], commits:[sha], outcome, evidence:[ref] }]`.
- `buckets` — `{ shipped:[ref], in_flight:[], rejected:[], next_candidates:[] }`.

## Reserved (empty in Phase 1)

`timeline, artifacts, feature_deltas, people, halls, flow, blockers, code_owners,
code_graph, label_taxonomy, modules, workflow_stats, workflows, releases, milestones,
docsRefs, release_train, sprints, project, diagrams`.
```

- [ ] **Step 6: Create `REFERENCE.md`**

```markdown
# activity-overview — Reference

## Install

Copy `.claude/skills/activity-overview/` into your repo (or `~/.claude/skills/`).
Requirements: Python 3 (stdlib only), `git` on PATH, and a `GITHUB_TOKEN`
(or `GH_TOKEN`) with `repo` + `read:project` scope.

## Usage

```bash
python3 gather.py --owner OWNER --repo REPO --from 2026-05-01 --to 2026-05-31 \
    --out workspace/bundle.json
python3 link.py workspace/bundle.json
```

Then render with the skill (see `SKILL.md`).

## Tests

```bash
python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v
```

## Troubleshooting

- **`error: set GITHUB_TOKEN`** — export a token before running gather.
- **Empty `shipped`** — no PRs were *merged* inside the window; widen `--from/--to`.
- **`git` errors on clone** — ensure `git` ≥ 2.x is on PATH; the clone is bounded by
  `--shallow-since`, so very old history is intentionally absent.
```

- [ ] **Step 7: Verify tests still pass (docs add no code)**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS (full suite green).

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/activity-overview/SKILL.md .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/commands/activity.md .claude/skills/activity-overview/projects.example.json .claude/skills/activity-overview/BUNDLE.md .claude/skills/activity-overview/REFERENCE.md
git commit -m "docs(activity): SKILL, template, command, BUNDLE schema, reference"
```

---

## Task 13: Final verification + push

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite one last time**

Run: `python3 -m unittest discover -s .claude/skills/activity-overview -p 'test_*.py' -v`
Expected: PASS — all tests across `test_gather.py` and `test_link.py` green, zero network access.

- [ ] **Step 2: Confirm no stray syntax errors in the CLIs**

Run: `python3 -c "import sys; sys.path.insert(0, '.claude/skills/activity-overview'); import gather, link; print('import ok')"`
Expected: prints `import ok`.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin claude/install-superpowers-skill-1Dibe
```

---

## Self-Review (completed by plan author)

**Spec coverage (Phase 1 scope only):**
- *Acquire — bounded clone* → Task 3 (`build_clone_cmd`) + Task 7 (`gather` main).
- *Acquire — git log → commits* → Task 2 (`parse_git_log`).
- *Acquire — merged PRs in window* → Task 4 (`normalize_pr`, `select_merged_prs`).
- *Acquire — their closing issues* → Task 4/5 (`parse_closing_refs`, `normalize_issue`) + Task 7 wiring.
- *Acquire — auth, CLI, pagination, bundle write* → Tasks 5–7.
- *Link — PR→commits* → Task 8 (`resolve_commit_pr`, `attach_commit_prs`).
- *Link — PR→issue, basic trains, deterministic ids* → Task 9 (`build_trains`).
- *Link — coarse shipped bucket* → Task 10 (`compute_buckets`).
- *Report — verifiable digest sections* → Task 12 (`SKILL.md`, `report-template.md`).
- *Bundle is the product + schema doc + ref convention* → Task 1 (skeleton), Task 12 (`BUNDLE.md`).
- *Tests offline, provenance lint* → Task 11.
- *Layout (committed, portable)* → all files under `.claude/skills/activity-overview/`.

**Deferred by design (NOT in this plan, reserved empty):** social layer, graphify/code_graph, feature_deltas, artifacts/timeline, people/halls, flow/blockers, Projects v2, releases/milestones, diagrams, series/continuity, transcript. These map to spec Phases 2–8.

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step ships complete code; every doc step ships full content.

**Type/name consistency:** `build_bundle(meta, commits, prs, issues)`, `parse_git_log → {sha,parents,author,date,message,files,pr}`, `normalize_pr → {…,merged,closes,url}`, `resolve_commit_pr`/`attach_commit_prs`, `build_trains → {id,kind,root_issue,prs,commits,outcome,evidence}`, `compute_buckets → {shipped,…}`, `enrich`, `ref(type,id,url)` — names and shapes are consistent across gather/link/tests/BUNDLE.md.
```
