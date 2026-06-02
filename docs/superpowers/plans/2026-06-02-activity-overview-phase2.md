# Activity Overview — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thicken the activity-overview skill into Phase 2 — a social-layer acquire (reviews, comments, timeline cross-refs, workflow runs, releases, milestones), full four-way bucketing, and a new deterministic `render.py` that emits two Mermaid diagrams validated by `mmdc`.

**Architecture:** Three offline-pure layers feed one network layer, mirroring Phase 1. `gather.py` grows pure `normalize_*` / `aggregate_*` / `summarize_*` / `parse_*` helpers (unit-tested from recorded fixtures) plus thin network wiring in `acquire()`. `link.py` gains windowed milestone-aware `compute_buckets` with single-bucket precedence and train cross-linking. A new `render.py` turns the enriched bundle into `workspace/diagrams/*.mmd` (a `pie` and a `gantt`), records a name→path manifest in `bundle.diagrams`, and compiles every diagram with `mmdc` so a broken diagram fails the run. The Markdown report template and SKILL procedure grow the new sections.

**Tech Stack:** Python 3.11 stdlib only (`json`, `argparse`, `urllib`, `subprocess`, `shutil`, `unittest`); `git` for cloning; `mmdc` (mermaid-cli, via Node) as a preflight-checked external binary used only to validate/export diagrams. No third-party Python packages.

**Spec:** `docs/superpowers/specs/2026-06-01-activity-overview-design.md` (Phase 2 bullet + Components + Buckets + Diagrams sections).

**Working directory:** `.claude/skills/activity-overview/`. Run all `python3`/`pytest` commands from that directory (it is how the existing suite is laid out — tests `sys.path.insert` the skill dir and read `fixtures/`).

**Backward-compatibility rule (applies to every task):** Phase 1 tests must stay green. Do **not** mutate the existing arrays in `fixtures/rest_sample.json` or `fixtures/bundle_sample.json`; add **new** fixture files for Phase 2. All new windowed logic must degrade permissively when dates/period are absent (so the dateless Phase 1 fixtures still bucket as before).

---

## File Structure

All paths are under `.claude/skills/activity-overview/`.

- **Modify `gather.py`** — extend `normalize_pr` / `normalize_issue`; add `summarize_reviews`, `parse_timeline_crossrefs`, `normalize_workflow`, `aggregate_workflow_stats`, `normalize_release`, `normalize_milestone`; widen `acquire()` (open + closed-unmerged PRs, broader issue set, workflows/releases/milestones, new CLI flags).
- **Modify `link.py`** — crossref-aware `build_trains`; add `train_index`, `select_milestones`, `_in_window`, `_high_priority`; rewrite `compute_buckets` to full four-way windowed bucketing with precedence and train tagging.
- **Create `render.py`** — pure `emit_buckets_pie`, `emit_timeline_gantt`, `render`, `write_diagrams`; `ensure_mmdc`, `validate_with_mmdc`; `main` CLI.
- **Create `test_render.py`** — pure-emitter + manifest tests (always run); mmdc tests guarded by `skipUnless`.
- **Modify `test_gather.py`, `test_link.py`** — add Phase 2 test classes.
- **Create fixtures:** `fixtures/rest_p2_sample.json` (recorded REST for the social layer) and `fixtures/bundle_p2.json` (enriched bundle for link/render).
- **Modify docs:** `report-template.md` (new sections + embedded diagrams), `SKILL.md` (render step + mmdc preflight), `BUNDLE.md` (new fields).

---

## Task 1: Extend `normalize_pr` with Phase 2 PR fields

Phase 1 dropped open and closed-unmerged PRs and never recorded `created_at`/`milestone`/`updated_at`/comment counts. Buckets and the gantt need them.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (`normalize_pr`, lines 123-142)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add to `test_gather.py` inside `class TestPrNormalization` (after `test_normalize_pr_unmerged_has_merged_false`):

```python
    def test_normalize_pr_captures_phase2_fields(self):
        raw = {
            "number": 44, "title": "Still open", "body": "Resolves #18",
            "state": "open", "merged_at": None, "closed_at": None,
            "created_at": "2026-05-03T09:00:00Z", "updated_at": "2026-05-20T09:00:00Z",
            "user": {"login": "alice"}, "author_association": "MEMBER",
            "labels": [{"name": "priority/high"}],
            "milestone": {"title": "v1.3.0"},
            "comments": 4, "review_comments": 2,
            "html_url": "https://github.com/o/r/pull/44",
        }
        pr = gather.normalize_pr(raw)
        self.assertEqual(pr["created_at"], "2026-05-03T09:00:00Z")
        self.assertEqual(pr["updated_at"], "2026-05-20T09:00:00Z")
        self.assertEqual(pr["milestone"], "v1.3.0")
        self.assertEqual(pr["comments"], 4)
        self.assertEqual(pr["review_comments_count"], 2)
        self.assertEqual(pr["state"], "open")
        self.assertFalse(pr["merged"])

    def test_normalize_pr_milestone_none_when_absent(self):
        pr = gather.normalize_pr({"number": 1, "html_url": "u"})
        self.assertIsNone(pr["milestone"])
        self.assertEqual(pr["comments"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k phase2_fields -v` (from the skill dir)
Expected: FAIL with `KeyError: 'created_at'` (field not yet produced).

- [ ] **Step 3: Modify `normalize_pr`**

Replace the body of `normalize_pr` (gather.py:123-142) with:

```python
def normalize_pr(raw):
    """Map a GitHub REST PR object to the bundle's PR shape."""
    milestone = raw.get("milestone")
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "milestone": milestone.get("title") if milestone else None,
        "merged": bool(raw.get("merged_at")),
        "merged_by": (raw.get("merged_by") or {}).get("login")
        if raw.get("merged_by") else None,
        "merged_at": raw.get("merged_at"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "state": raw.get("state"),
        "comments": raw.get("comments", 0) or 0,
        "review_comments_count": raw.get("review_comments", 0) or 0,
        "reviewers": [],
        "review_decision": "none",
        "crossref_issues": [],
        "closes": parse_closing_refs(
            (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")
        ),
        "url": raw.get("html_url"),
    }
```

(`reviewers`/`review_decision`/`crossref_issues` are filled later in `acquire()`; they are initialised here so the PR shape is schema-stable even when those fetches are skipped.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "Pr" -v`
Expected: PASS (new tests + the existing `normalize_pr` tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): capture created_at/milestone/comment fields on PRs"
```

---

## Task 2: Extend `normalize_issue` with Phase 2 issue fields

Buckets need `milestone`, `updated_at`, and comment count on issues (open issues now participate).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (`normalize_issue`, lines 150-165)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add to `test_gather.py` inside `class TestIssueAndFetch` (after `test_normalize_issue_maps_kind_and_state_reason`):

```python
    def test_normalize_issue_captures_phase2_fields(self):
        raw = {
            "number": 18, "title": "Open feature", "body": "",
            "state": "open", "state_reason": None,
            "updated_at": "2026-05-22T00:00:00Z",
            "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
            "labels": [{"name": "priority/high"}],
            "assignees": [{"login": "alice"}],
            "milestone": {"title": "v1.3.0"},
            "comments": 7,
            "html_url": "https://github.com/o/r/issues/18",
        }
        issue = gather.normalize_issue(raw)
        self.assertEqual(issue["milestone"], "v1.3.0")
        self.assertEqual(issue["updated_at"], "2026-05-22T00:00:00Z")
        self.assertEqual(issue["comments"], 7)
        self.assertEqual(issue["state"], "open")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k phase2_fields -v`
Expected: FAIL with `KeyError: 'milestone'`.

- [ ] **Step 3: Modify `normalize_issue`**

Replace the body of `normalize_issue` (gather.py:150-165) with:

```python
def normalize_issue(raw):
    """Map a GitHub REST issue object to the bundle's issue shape."""
    milestone = raw.get("milestone")
    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "kind": "other",  # refined in later phases (issue types/labels/template)
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "labels": [lbl["name"] for lbl in raw.get("labels", [])],
        "assignees": [a["login"] for a in raw.get("assignees", [])],
        "milestone": milestone.get("title") if milestone else None,
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "comments": raw.get("comments", 0) or 0,
        "url": raw.get("html_url"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "Issue" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): capture milestone/updated_at/comments on issues"
```

---

## Task 3: Pure reviews + timeline cross-ref helpers

Reviews give a PR's decision; timeline events surface issue links that closing-keyword parsing misses. Both are pure functions over recorded arrays.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add two functions after `normalize_issue`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestReviewsAndTimeline(unittest.TestCase):
    def test_summarize_reviews_picks_latest_decision_per_reviewer(self):
        raw = [
            {"user": {"login": "bob"}, "state": "COMMENTED",
             "submitted_at": "2026-05-08T10:00:00Z"},
            {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-05-09T10:00:00Z"},
            {"user": {"login": "carol"}, "state": "APPROVED",
             "submitted_at": "2026-05-10T10:00:00Z"},
        ]
        out = gather.summarize_reviews(raw)
        self.assertEqual(sorted(out["reviewers"]), ["bob", "carol"])
        # changes_requested outranks approved when any reviewer still blocks
        self.assertEqual(out["decision"], "changes_requested")

    def test_summarize_reviews_approved_when_all_clear(self):
        raw = [{"user": {"login": "carol"}, "state": "APPROVED",
                "submitted_at": "2026-05-10T10:00:00Z"}]
        self.assertEqual(gather.summarize_reviews(raw)["decision"], "approved")

    def test_summarize_reviews_empty_is_none(self):
        self.assertEqual(gather.summarize_reviews([])["decision"], "none")

    def test_parse_timeline_crossrefs_collects_issue_numbers(self):
        raw = [
            {"event": "cross-referenced",
             "source": {"issue": {"number": 18, "pull_request": None}}},
            {"event": "connected", "subject": {"number": 19}},
            {"event": "labeled"},
            {"event": "cross-referenced",
             "source": {"issue": {"number": 18, "pull_request": None}}},
        ]
        self.assertEqual(gather.parse_timeline_crossrefs(raw), [18, 19])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "ReviewsAndTimeline" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'summarize_reviews'`.

- [ ] **Step 3: Implement the helpers**

Add to `gather.py` immediately after `normalize_issue` (before `fetch_all`):

```python
# Review states ranked by how strongly they gate a merge. A PR's decision is
# the strongest *latest-per-reviewer* signal: any outstanding changes-requested
# dominates; otherwise an approval; otherwise a bare comment.
_REVIEW_RANK = {"changes_requested": 3, "approved": 2, "commented": 1, "none": 0}


def summarize_reviews(raw_reviews):
    """Reduce raw PR reviews to {reviewers, decision}. Pure."""
    latest = {}  # login -> (submitted_at, state)
    for r in raw_reviews or []:
        login = (r.get("user") or {}).get("login")
        if not login:
            continue
        state = (r.get("state") or "").lower()
        if state not in _REVIEW_RANK:
            continue
        ts = r.get("submitted_at") or ""
        if login not in latest or ts >= latest[login][0]:
            latest[login] = (ts, state)
    reviewers = sorted(latest)
    decision = "none"
    for _, state in latest.values():
        if _REVIEW_RANK[state] > _REVIEW_RANK[decision]:
            decision = state
    return {"reviewers": reviewers, "decision": decision}


def parse_timeline_crossrefs(raw_timeline):
    """Issue numbers cross-referenced/connected to a PR via timeline events.

    De-duplicated, order-preserving. Skips cross-refs whose source is itself a
    pull request (we only want issue links). Pure."""
    out = []
    for ev in raw_timeline or []:
        kind = ev.get("event")
        num = None
        if kind == "cross-referenced":
            issue = (ev.get("source") or {}).get("issue") or {}
            if issue.get("pull_request") is None:
                num = issue.get("number")
        elif kind in ("connected", "disconnected"):
            num = (ev.get("subject") or {}).get("number")
        if num is not None and num not in out:
            out.append(num)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "ReviewsAndTimeline" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): add pure reviews-decision and timeline cross-ref parsers"
```

---

## Task 4: Workflow / release / milestone normalizers

CI/CD health, releases, and milestones are new bundle sections. Each is a pure normalizer plus one aggregator for workflow stats.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add after `parse_timeline_crossrefs`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestWorkflowsReleasesMilestones(unittest.TestCase):
    def test_normalize_workflow_maps_fields(self):
        raw = {"name": "CI", "conclusion": "success", "status": "completed",
               "event": "push", "head_branch": "main",
               "created_at": "2026-05-10T00:00:00Z",
               "html_url": "https://github.com/o/r/actions/runs/1"}
        wf = gather.normalize_workflow(raw)
        self.assertEqual(wf["name"], "CI")
        self.assertEqual(wf["conclusion"], "success")
        self.assertEqual(wf["url"], "https://github.com/o/r/actions/runs/1")

    def test_aggregate_workflow_stats_counts_by_conclusion(self):
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CI", "conclusion": "failure"},
            {"name": "CI", "conclusion": "success"},
            {"name": "CI", "conclusion": "cancelled"},
            {"name": "Release", "conclusion": "success"},
            {"name": "CI", "conclusion": "neutral"},
        ]
        stats = gather.aggregate_workflow_stats(runs)
        self.assertEqual(stats["CI"],
                         {"total": 5, "success": 2, "failure": 1,
                          "cancelled": 1, "other": 1})
        self.assertEqual(stats["Release"]["success"], 1)

    def test_normalize_release_and_milestone(self):
        rel = gather.normalize_release({
            "tag_name": "v1.2.0", "name": "1.2.0",
            "published_at": "2026-05-15T00:00:00Z", "prerelease": False,
            "html_url": "https://github.com/o/r/releases/tag/v1.2.0"})
        self.assertEqual(rel["tag_name"], "v1.2.0")
        self.assertFalse(rel["prerelease"])
        ms = gather.normalize_milestone({
            "title": "v1.3.0", "number": 5, "state": "open",
            "due_on": "2026-06-30T00:00:00Z", "open_issues": 3,
            "closed_issues": 7,
            "html_url": "https://github.com/o/r/milestone/5"})
        self.assertEqual(ms["title"], "v1.3.0")
        self.assertEqual(ms["open_issues"], 3)
        self.assertEqual(ms["state"], "open")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "WorkflowsReleasesMilestones" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'normalize_workflow'`.

- [ ] **Step 3: Implement the normalizers**

Add to `gather.py` after `parse_timeline_crossrefs`:

```python
def normalize_workflow(raw):
    """Map a GitHub Actions run object to the bundle's workflow shape."""
    return {
        "name": raw.get("name"),
        "conclusion": raw.get("conclusion"),
        "status": raw.get("status"),
        "event": raw.get("event"),
        "head_branch": raw.get("head_branch"),
        "created_at": raw.get("created_at"),
        "url": raw.get("html_url"),
    }


def aggregate_workflow_stats(workflows):
    """Count runs per workflow name by conclusion. Pure.

    Buckets conclusions into success/failure/cancelled/other so the report can
    show a CI health line per workflow."""
    stats = {}
    for wf in workflows or []:
        name = wf.get("name") or "(unnamed)"
        s = stats.setdefault(
            name, {"total": 0, "success": 0, "failure": 0, "cancelled": 0, "other": 0})
        s["total"] += 1
        conclusion = wf.get("conclusion")
        if conclusion in ("success", "failure", "cancelled"):
            s[conclusion] += 1
        else:
            s["other"] += 1
    return stats


def normalize_release(raw):
    """Map a GitHub release object to the bundle's release shape."""
    return {
        "tag_name": raw.get("tag_name"),
        "name": raw.get("name"),
        "published_at": raw.get("published_at"),
        "prerelease": bool(raw.get("prerelease")),
        "url": raw.get("html_url"),
    }


def normalize_milestone(raw):
    """Map a GitHub milestone object to the bundle's milestone shape."""
    return {
        "title": raw.get("title"),
        "number": raw.get("number"),
        "state": raw.get("state"),
        "due_on": raw.get("due_on"),
        "open_issues": raw.get("open_issues", 0) or 0,
        "closed_issues": raw.get("closed_issues", 0) or 0,
        "url": raw.get("html_url"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "WorkflowsReleasesMilestones" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): add workflow/release/milestone normalizers"
```

---

## Task 5: Phase 2 REST fixture

A single recorded-response fixture drives the offline acquire-assembly test (Task 6) and is reused by the link/render fixtures. Kept separate from `rest_sample.json` so Phase 1 tests are untouched.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/rest_p2_sample.json`

- [ ] **Step 1: Create the fixture**

Write `.claude/skills/activity-overview/fixtures/rest_p2_sample.json`:

```json
{
  "window": {"from": "2026-05-01", "to": "2026-05-31"},
  "pulls": [
    {"number": 42, "title": "Add policy param", "body": "Fixes #17.",
     "state": "closed", "merged_at": "2026-05-10T12:00:00Z",
     "closed_at": "2026-05-10T12:00:00Z", "created_at": "2026-05-02T08:00:00Z",
     "updated_at": "2026-05-10T12:00:00Z", "user": {"login": "alice"},
     "author_association": "MEMBER", "labels": [{"name": "enhancement"}],
     "milestone": {"title": "v1.2.0"}, "merged_by": {"login": "bob"},
     "comments": 3, "review_comments": 1,
     "html_url": "https://github.com/o/r/pull/42"},
    {"number": 43, "title": "WIP experiment", "body": "no refs",
     "state": "closed", "merged_at": null, "closed_at": "2026-05-11T09:00:00Z",
     "created_at": "2026-05-05T08:00:00Z", "updated_at": "2026-05-11T09:00:00Z",
     "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
     "labels": [], "milestone": null, "merged_by": null,
     "comments": 0, "review_comments": 0,
     "html_url": "https://github.com/o/r/pull/43"},
    {"number": 44, "title": "Still open", "body": "Resolves #18",
     "state": "open", "merged_at": null, "closed_at": null,
     "created_at": "2026-05-20T08:00:00Z", "updated_at": "2026-05-25T08:00:00Z",
     "user": {"login": "alice"}, "author_association": "MEMBER",
     "labels": [{"name": "priority/high"}], "milestone": {"title": "v1.3.0"},
     "comments": 2, "review_comments": 0,
     "html_url": "https://github.com/o/r/pull/44"}
  ],
  "reviews": {
    "42": [{"user": {"login": "bob"}, "state": "APPROVED",
            "submitted_at": "2026-05-09T10:00:00Z"}],
    "43": [{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-05-10T10:00:00Z"}],
    "44": []
  },
  "timeline": {
    "42": [],
    "43": [],
    "44": [{"event": "cross-referenced",
            "source": {"issue": {"number": 18, "pull_request": null}}}]
  },
  "issues": {
    "17": {"number": 17, "title": "Support policy param", "body": "",
           "state": "closed", "state_reason": "completed",
           "updated_at": "2026-05-10T12:00:00Z", "user": {"login": "dave"},
           "author_association": "CONTRIBUTOR", "labels": [{"name": "enhancement"}],
           "assignees": [{"login": "alice"}], "milestone": {"title": "v1.2.0"},
           "closed_at": "2026-05-10T12:00:00Z", "comments": 4,
           "html_url": "https://github.com/o/r/issues/17"},
    "18": {"number": 18, "title": "Open feature for next release", "body": "",
           "state": "open", "state_reason": null,
           "updated_at": "2026-05-22T00:00:00Z", "user": {"login": "carol"},
           "author_association": "CONTRIBUTOR", "labels": [],
           "assignees": [], "milestone": {"title": "v1.3.0"},
           "closed_at": null, "comments": 1,
           "html_url": "https://github.com/o/r/issues/18"},
    "20": {"number": 20, "title": "Abandoned idea", "body": "",
           "state": "closed", "state_reason": "not_planned",
           "updated_at": "2026-05-15T00:00:00Z", "user": {"login": "carol"},
           "author_association": "CONTRIBUTOR", "labels": [],
           "assignees": [], "milestone": null,
           "closed_at": "2026-05-15T00:00:00Z", "comments": 0,
           "html_url": "https://github.com/o/r/issues/20"},
    "21": {"number": 21, "title": "Active in current release", "body": "",
           "state": "open", "state_reason": null,
           "updated_at": "2026-05-18T00:00:00Z", "user": {"login": "dave"},
           "author_association": "MEMBER", "labels": [],
           "assignees": [], "milestone": {"title": "v1.2.0"},
           "closed_at": null, "comments": 0,
           "html_url": "https://github.com/o/r/issues/21"}
  },
  "workflows": [
    {"name": "CI", "conclusion": "success", "status": "completed",
     "event": "push", "head_branch": "main", "created_at": "2026-05-10T00:00:00Z",
     "html_url": "https://github.com/o/r/actions/runs/1"},
    {"name": "CI", "conclusion": "failure", "status": "completed",
     "event": "pull_request", "head_branch": "feat", "created_at": "2026-05-11T00:00:00Z",
     "html_url": "https://github.com/o/r/actions/runs/2"},
    {"name": "CI", "conclusion": "success", "status": "completed",
     "event": "push", "head_branch": "main", "created_at": "2026-05-12T00:00:00Z",
     "html_url": "https://github.com/o/r/actions/runs/3"}
  ],
  "releases": [
    {"tag_name": "v1.2.0", "name": "1.2.0", "published_at": "2026-05-15T00:00:00Z",
     "prerelease": false, "html_url": "https://github.com/o/r/releases/tag/v1.2.0"}
  ],
  "milestones": [
    {"title": "v1.1.0", "number": 3, "state": "closed",
     "due_on": "2026-04-30T00:00:00Z", "open_issues": 0, "closed_issues": 10,
     "html_url": "https://github.com/o/r/milestone/3"},
    {"title": "v1.2.0", "number": 4, "state": "open",
     "due_on": "2026-05-31T00:00:00Z", "open_issues": 1, "closed_issues": 8,
     "html_url": "https://github.com/o/r/milestone/4"},
    {"title": "v1.3.0", "number": 5, "state": "open",
     "due_on": "2026-06-30T00:00:00Z", "open_issues": 2, "closed_issues": 0,
     "html_url": "https://github.com/o/r/milestone/5"}
  ]
}
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import json; d=json.load(open('fixtures/rest_p2_sample.json')); print(len(d['pulls']), len(d['issues']), len(d['milestones']))"`
Expected: `3 4 3`

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/fixtures/rest_p2_sample.json
git commit -m "test(activity): add Phase 2 REST sample fixture"
```

---

## Task 6: Widen `acquire()` and `main()` for the social layer

`acquire()` currently keeps only merged-in-window PRs and their closing issues. Phase 2 must also keep open + closed-unmerged PRs, pull a broader issue set, attach reviews/cross-refs per PR, and fetch workflows/releases/milestones. The pure helpers (Tasks 1-4) do the parsing; this task wires them and adds CLI flags. Network wiring is verified offline by composing the helpers over the Task 5 fixture (mirrors the existing `test_gather_assembly_into_link_offline` pattern — no network).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (`parse_args`, `acquire`, `build_bundle` call)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestAcquireAssemblyP2(unittest.TestCase):
    """Compose the pure helpers over recorded REST, as acquire() does, offline."""

    def _bundle_from_fixture(self):
        with open(os.path.join(FIX, "rest_p2_sample.json")) as fh:
            data = json.load(fh)
        frm, to = data["window"]["from"], data["window"]["to"]
        prs = [gather.normalize_pr(p) for p in data["pulls"]]
        for pr in prs:
            rv = gather.summarize_reviews(data["reviews"].get(str(pr["number"]), []))
            pr["reviewers"] = rv["reviewers"]
            pr["review_decision"] = rv["decision"]
            pr["crossref_issues"] = gather.parse_timeline_crossrefs(
                data["timeline"].get(str(pr["number"]), []))
        issues = [gather.normalize_issue(i) for i in data["issues"].values()]
        workflows = [gather.normalize_workflow(w) for w in data["workflows"]]
        releases = [gather.normalize_release(r) for r in data["releases"]]
        milestones = [gather.normalize_milestone(m) for m in data["milestones"]]
        meta = {"owner": "o", "repo": "r", "from": frm, "to": to,
                "period": {"from": frm, "to": to}, "ref_date": to}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["workflows"] = workflows
        bundle["workflow_stats"] = gather.aggregate_workflow_stats(workflows)
        bundle["releases"] = releases
        bundle["milestones"] = milestones
        return bundle

    def test_bundle_has_social_layer(self):
        b = self._bundle_from_fixture()
        self.assertEqual({p["number"] for p in b["prs"]}, {42, 43, 44})
        pr44 = next(p for p in b["prs"] if p["number"] == 44)
        self.assertEqual(pr44["crossref_issues"], [18])
        pr43 = next(p for p in b["prs"] if p["number"] == 43)
        self.assertEqual(pr43["review_decision"], "changes_requested")
        self.assertEqual(b["workflow_stats"]["CI"]["total"], 3)
        self.assertEqual(b["releases"][0]["tag_name"], "v1.2.0")
        self.assertEqual(len(b["milestones"]), 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "AcquireAssemblyP2" -v`
Expected: FAIL — the fixture composition works only once the helpers exist; if Tasks 1-4 are complete this test actually **passes already** for the pure-composition path. If it passes, that confirms the helpers compose correctly; proceed to wire `acquire()` (Steps 3-4) which the test in Step 5 exercises. (If run before Tasks 1-4, it fails with `AttributeError`.)

- [ ] **Step 3: Add CLI flags**

In `parse_args` (gather.py:195-205), add after the `--no-clone` argument:

```python
    p.add_argument("--ref-date", default=None,
                   help="Reference date for milestone framing (default: --to).")
    p.add_argument("--include-workflows", dest="include_workflows",
                   action="store_true", default=True)
    p.add_argument("--no-workflows", dest="include_workflows", action="store_false")
    p.add_argument("--include-releases", dest="include_releases",
                   action="store_true", default=True)
    p.add_argument("--no-releases", dest="include_releases", action="store_false")
```

- [ ] **Step 4: Widen `acquire()`**

Replace the PR/issue selection block and the `meta`/return in `acquire()` (gather.py:327-355) with:

```python
    get_page = _paginated(token)
    api = f"https://api.github.com/repos/{owner}/{repo}"
    # Closed PRs (merged + rejected) bounded by update time, as in Phase 1...
    raw_closed = fetch_until(
        get_page,
        f"{api}/pulls?state=closed&sort=updated&direction=desc&per_page=100",
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm,
    )
    # ...plus all currently-open PRs (these feed in_flight / next_candidates).
    raw_open = fetch_all(
        get_page, f"{api}/pulls?state=open&sort=updated&direction=desc&per_page=100")

    prs = []
    for raw in raw_closed + raw_open:
        pr = normalize_pr(raw)
        # Keep: merged-in-window, closed-unmerged-in-window, or any open PR.
        keep = (
            (pr["merged"] and in_window(pr["merged_at"], frm, to))
            or (pr["state"] == "closed" and not pr["merged"]
                and in_window(pr["closed_at"], frm, to))
            or pr["state"] == "open"
        )
        if not keep:
            continue
        reviews, _ = http_get_json(f"{api}/pulls/{pr['number']}/reviews?per_page=100", token)
        summary = summarize_reviews(reviews)
        pr["reviewers"] = summary["reviewers"]
        pr["review_decision"] = summary["decision"]
        timeline, _ = http_get_json(
            f"{api}/issues/{pr['number']}/timeline?per_page=100", token)
        pr["crossref_issues"] = parse_timeline_crossrefs(timeline)
        prs.append(pr)

    # Issue set: every issue a kept PR closes or cross-references, plus open and
    # recently-closed issues (for in_flight / next_candidates / rejected buckets).
    wanted = set()
    for pr in prs:
        wanted.update(pr["closes"])
        wanted.update(pr["crossref_issues"])
    raw_repo_issues = fetch_until(
        get_page,
        f"{api}/issues?state=all&sort=updated&direction=desc&per_page=100",
        lambda page: bool(page) and page[-1].get("updated_at", "")[:10] >= frm,
    )
    issues = []
    seen = set()
    for raw in raw_repo_issues:
        if raw.get("pull_request"):  # the issues endpoint also lists PRs; skip them
            continue
        issue = normalize_issue(raw)
        seen.add(issue["number"])
        issues.append(issue)
    for n in sorted(wanted - seen):
        raw_issue, _ = http_get_json(f"{api}/issues/{n}", token)
        if raw_issue.get("pull_request"):
            continue
        issues.append(normalize_issue(raw_issue))

    workflows = []
    workflow_stats = {}
    if getattr(args, "include_workflows", True):
        raw_runs = _runs_page(get_page, api, frm, to)
        workflows = [normalize_workflow(r) for r in raw_runs]
        workflow_stats = aggregate_workflow_stats(workflows)

    releases = []
    if getattr(args, "include_releases", True):
        raw_releases = fetch_all(get_page, f"{api}/releases?per_page=100")
        releases = [normalize_release(r) for r in raw_releases
                    if in_window(r.get("published_at"), frm, to)]

    raw_milestones = fetch_all(get_page, f"{api}/milestones?state=all&per_page=100")
    milestones = [normalize_milestone(m) for m in raw_milestones]

    ref_date = getattr(args, "ref_date", None) or to
    meta = {
        "owner": owner, "repo": repo, "from": frm, "to": to,
        "branches": args.branches.split(","), "clone_dir": clone_dir,
        "ref_date": ref_date,
        "period": {"from": frm, "to": to}, "prev_bundle": None,
    }
    bundle = build_bundle(meta, commits, prs, issues)
    bundle["workflows"] = workflows
    bundle["workflow_stats"] = workflow_stats
    bundle["releases"] = releases
    bundle["milestones"] = milestones
    return bundle
```

Then add this small helper near `_paginated` (the `actions/runs` payload wraps its list under a `workflow_runs` key, unlike other endpoints, so it cannot reuse `fetch_until` directly):

```python
def _runs_page(get_page, api, frm, to):
    """Walk actions/runs, whose pages wrap the list under `workflow_runs`."""
    runs = []
    url = f"{api}/actions/runs?created={frm}..{to}&per_page=100"
    while url:
        payload, url = get_page(url)
        page = payload.get("workflow_runs", []) if isinstance(payload, dict) else payload
        runs.extend(page)
        if page and page[-1].get("created_at", "")[:10] < frm:
            break
    return runs
```

- [ ] **Step 5: Run the full gather suite**

Run: `python3 -m pytest test_gather.py -v`
Expected: PASS (all Phase 1 tests + the new `TestAcquireAssemblyP2`). The Phase 1 `test_parse_args_required_and_defaults` still passes because the new flags have defaults.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "feat(activity): acquire reviews, crossrefs, open PRs, workflows, releases, milestones"
```

---

## Task 7: Crossref-aware trains

Fold timeline cross-references into PR→issue anchoring so a PR that links its issue only via a GitHub "connected" event (not a closing keyword) still joins the right train.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (`build_trains`, lines 30-75)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add to `test_link.py` inside `class TestBuildTrains`:

```python
    def test_train_anchors_on_crossref_when_no_closing_keyword(self):
        bundle = _sample_bundle()
        bundle["prs"][0]["closes"] = []
        bundle["prs"][0]["crossref_issues"] = [17]
        link.attach_commit_prs(bundle["commits"])
        trains = link.build_trains(bundle)
        self.assertEqual(trains[0]["id"], "train-issue-17")
        self.assertEqual(trains[0]["root_issue"], 17)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k crossref -v`
Expected: FAIL — train anchors on the PR (`train-pr-42`) because `closes` is empty and crossrefs are ignored.

- [ ] **Step 3: Modify `build_trains`**

In `link.py`, change the anchor line inside `build_trains` (line 46) from:

```python
        root = pr["closes"][0] if pr.get("closes") else None
```

to:

```python
        links = list(pr.get("closes") or [])
        for n in pr.get("crossref_issues") or []:
            if n not in links:
                links.append(n)
        root = links[0] if links else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -k "Train or crossref" -v`
Expected: PASS (new test + existing train tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "feat(activity): anchor trains on timeline cross-refs, not just closing keywords"
```

---

## Task 8: Milestone selection + full four-way buckets

Rewrite `compute_buckets` into the full windowed, milestone-aware, single-bucket-per-item classifier with precedence `shipped > rejected > next_candidates > in_flight`, and tag each ref with its train id. Mapping (per spec, confirmed): `in_flight` = open items active-in-window ∪ on the current (earliest-open) milestone; `next_candidates` = open items on the next milestone ∪ high-priority-labelled.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add helpers; replace `compute_buckets`, lines 78-87)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestSelectMilestonesAndBuckets(unittest.TestCase):
    def _p2_bundle(self):
        with open(os.path.join(FIX, "bundle_p2.json")) as fh:
            return link.enrich(json.load(fh))

    def test_select_milestones_current_and_next(self):
        ms = [
            {"title": "v1.1.0", "state": "closed", "due_on": "2026-04-30T00:00:00Z", "number": 3},
            {"title": "v1.2.0", "state": "open", "due_on": "2026-05-31T00:00:00Z", "number": 4},
            {"title": "v1.3.0", "state": "open", "due_on": "2026-06-30T00:00:00Z", "number": 5},
        ]
        current, nxt = link.select_milestones(ms, "2026-05-20")
        self.assertEqual(current["title"], "v1.2.0")
        self.assertEqual(nxt["title"], "v1.3.0")

    def test_buckets_classify_each_item_once(self):
        b = self._p2_bundle()["buckets"]
        def nums(key):
            return {(r["type"], r["id"]) for r in b[key]}
        self.assertIn(("pr", 42), nums("shipped"))
        self.assertIn(("issue", 17), nums("shipped"))
        self.assertIn(("pr", 43), nums("rejected"))
        self.assertIn(("issue", 20), nums("rejected"))
        # open #44 + #18 are on the NEXT milestone (v1.3.0) -> next_candidates
        self.assertIn(("pr", 44), nums("next_candidates"))
        self.assertIn(("issue", 18), nums("next_candidates"))
        # open #21 is on the CURRENT milestone (v1.2.0) -> in_flight
        self.assertIn(("issue", 21), nums("in_flight"))
        # no item appears in two buckets
        all_refs = [(r["type"], r["id"]) for k in b for r in b[k]]
        self.assertEqual(len(all_refs), len(set(all_refs)))

    def test_bucket_refs_carry_train_id_when_known(self):
        b = self._p2_bundle()["buckets"]
        pr42 = next(r for r in b["shipped"] if (r["type"], r["id"]) == ("pr", 42))
        self.assertEqual(pr42["train"], "train-issue-17")
```

- [ ] **Step 2: Create the link fixture**

Write `.claude/skills/activity-overview/fixtures/bundle_p2.json` (an un-enriched bundle the test enriches; commits omitted for brevity, trains/buckets empty):

```json
{
  "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31",
           "ref_date": "2026-05-31", "period": {"from": "2026-05-01", "to": "2026-05-31"}},
  "commits": [],
  "prs": [
    {"number": 42, "title": "Add policy param", "merged": true, "state": "closed",
     "merged_at": "2026-05-10T12:00:00Z", "closed_at": "2026-05-10T12:00:00Z",
     "created_at": "2026-05-02T08:00:00Z", "updated_at": "2026-05-10T12:00:00Z",
     "milestone": "v1.2.0", "labels": ["enhancement"], "closes": [17],
     "crossref_issues": [], "url": "https://github.com/o/r/pull/42"},
    {"number": 43, "title": "WIP experiment", "merged": false, "state": "closed",
     "merged_at": null, "closed_at": "2026-05-11T09:00:00Z",
     "created_at": "2026-05-05T08:00:00Z", "updated_at": "2026-05-11T09:00:00Z",
     "milestone": null, "labels": [], "closes": [], "crossref_issues": [],
     "url": "https://github.com/o/r/pull/43"},
    {"number": 44, "title": "Still open", "merged": false, "state": "open",
     "merged_at": null, "closed_at": null,
     "created_at": "2026-05-20T08:00:00Z", "updated_at": "2026-05-25T08:00:00Z",
     "milestone": "v1.3.0", "labels": ["priority/high"], "closes": [],
     "crossref_issues": [18], "url": "https://github.com/o/r/pull/44"}
  ],
  "issues": [
    {"number": 17, "title": "Support policy param", "kind": "feature",
     "state": "closed", "state_reason": "completed", "milestone": "v1.2.0",
     "closed_at": "2026-05-10T12:00:00Z", "updated_at": "2026-05-10T12:00:00Z",
     "labels": ["enhancement"], "url": "https://github.com/o/r/issues/17"},
    {"number": 18, "title": "Open feature for next release", "kind": "other",
     "state": "open", "state_reason": null, "milestone": "v1.3.0",
     "closed_at": null, "updated_at": "2026-05-22T00:00:00Z",
     "labels": [], "url": "https://github.com/o/r/issues/18"},
    {"number": 20, "title": "Abandoned idea", "kind": "other",
     "state": "closed", "state_reason": "not_planned", "milestone": null,
     "closed_at": "2026-05-15T00:00:00Z", "updated_at": "2026-05-15T00:00:00Z",
     "labels": [], "url": "https://github.com/o/r/issues/20"},
    {"number": 21, "title": "Active in current release", "kind": "other",
     "state": "open", "state_reason": null, "milestone": "v1.2.0",
     "closed_at": null, "updated_at": "2026-05-18T00:00:00Z",
     "labels": [], "url": "https://github.com/o/r/issues/21"}
  ],
  "milestones": [
    {"title": "v1.1.0", "number": 3, "state": "closed", "due_on": "2026-04-30T00:00:00Z"},
    {"title": "v1.2.0", "number": 4, "state": "open", "due_on": "2026-05-31T00:00:00Z"},
    {"title": "v1.3.0", "number": 5, "state": "open", "due_on": "2026-06-30T00:00:00Z"}
  ],
  "releases": [
    {"tag_name": "v1.2.0", "name": "1.2.0", "published_at": "2026-05-15T00:00:00Z",
     "prerelease": false, "url": "https://github.com/o/r/releases/tag/v1.2.0"}
  ],
  "workflow_stats": {"CI": {"total": 3, "success": 2, "failure": 1, "cancelled": 0, "other": 0}},
  "trains": [],
  "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "SelectMilestonesAndBuckets" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'select_milestones'`.

- [ ] **Step 4: Implement helpers + rewrite `compute_buckets`**

In `link.py`, add after `ref()` (line 27):

```python
HIGH_PRIORITY_LABELS = {
    "priority/high", "priority/critical", "p0", "p1", "high-priority", "critical",
}


def _in_window(ts, period):
    """True if `ts` (ISO) falls in `period`. Permissive when either is missing,
    so dateless fixtures (and pre-window-free bundles) classify as in-window."""
    if not period or not ts:
        return True
    frm, to = period.get("from"), period.get("to")
    day = ts[:10]
    return (not frm or day >= frm) and (not to or day <= to)


def _high_priority(item):
    return any((lbl or "").lower() in HIGH_PRIORITY_LABELS
               for lbl in item.get("labels", []))


def _ms_sort_key(m):
    return ((m.get("due_on") or "9999-12-31")[:10], m.get("number") or 0)


def select_milestones(milestones, ref_date):
    """(current, next) open milestones by due date. current = earliest open whose
    due date is on/after ref_date (else the earliest open); next = the one after."""
    open_ms = sorted((m for m in milestones if m.get("state") == "open"),
                     key=_ms_sort_key)
    if not open_ms:
        return None, None
    current = next(
        (m for m in open_ms if (m.get("due_on") or "9999-12-31")[:10] >= ref_date),
        open_ms[0])
    idx = open_ms.index(current)
    nxt = open_ms[idx + 1] if idx + 1 < len(open_ms) else None
    return current, nxt


def train_index(trains):
    """Map ('pr'|'issue', number) -> train id, for cross-linking bucket refs."""
    idx = {}
    for t in trains:
        if t.get("root_issue") is not None:
            idx[("issue", t["root_issue"])] = t["id"]
        for n in t.get("prs", []):
            idx[("pr", n)] = t["id"]
    return idx
```

Then replace `compute_buckets` (the old lines 78-87) with:

```python
def compute_buckets(bundle):
    """Full four-way bucketing: one bucket per item, precedence
    shipped > rejected > next_candidates > in_flight. Refs carry their train id."""
    meta = bundle.get("meta", {})
    period = meta.get("period")
    ref_date = meta.get("ref_date") or meta.get("to") or ""
    current_ms, next_ms = select_milestones(bundle.get("milestones", []), ref_date)
    current_title = current_ms["title"] if current_ms else None
    next_title = next_ms["title"] if next_ms else None
    tindex = train_index(bundle.get("trains", []))

    out = {"shipped": [], "rejected": [], "next_candidates": [], "in_flight": []}

    def add(bucket, type_, num, url):
        r = ref(type_, num, url)
        tid = tindex.get((type_, num))
        if tid:
            r["train"] = tid
        out[bucket].append(r)

    def classify(item, type_):
        num, url = item["number"], item.get("url")
        state = item.get("state")
        if type_ == "pr" and item.get("merged") and _in_window(item.get("merged_at"), period):
            add("shipped", type_, num, url)
        elif type_ == "issue" and state == "closed" \
                and item.get("state_reason") == "completed" \
                and _in_window(item.get("closed_at"), period):
            add("shipped", type_, num, url)
        elif state == "closed" and type_ == "pr" and not item.get("merged") \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "closed" and type_ == "issue" \
                and item.get("state_reason") == "not_planned" \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "open":
            on_next = next_title is not None and item.get("milestone") == next_title
            on_current = current_title is not None and item.get("milestone") == current_title
            if on_next or _high_priority(item):
                add("next_candidates", type_, num, url)
            elif on_current or _in_window(item.get("updated_at"), period):
                add("in_flight", type_, num, url)

    for pr in bundle.get("prs", []):
        classify(pr, "pr")
    for issue in bundle.get("issues", []):
        classify(issue, "issue")
    return out
```

Note: PR #44 in the fixture is `priority/high` **and** on the next milestone — both routes lead to `next_candidates`, so precedence is unambiguous there. Issue #21 is on the current milestone and not high-priority → `in_flight`.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -v`
Expected: PASS — all Phase 1 link tests (dateless `_sample_bundle` / `bundle_sample.json` still bucket pr42+issue17 into `shipped` via permissive `_in_window`) plus the new bucket tests.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py .claude/skills/activity-overview/fixtures/bundle_p2.json
git commit -m "feat(activity): full four-way milestone-aware buckets with train cross-links"
```

---

## Task 9: `render.py` — buckets pie emitter

Start `render.py` with the simplest diagram: a Mermaid `pie` of bucket counts. Pure string generation, unit-tested offline (no mmdc).

**Files:**
- Create: `.claude/skills/activity-overview/render.py`
- Create: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Create `.claude/skills/activity-overview/test_render.py`:

```python
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import render  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _bundle():
    with open(os.path.join(FIX, "bundle_p2.json")) as fh:
        return json.load(fh)


class TestBucketsPie(unittest.TestCase):
    def test_pie_header_and_counts(self):
        b = _bundle()
        b["buckets"] = {"shipped": [{"type": "pr", "id": 42, "url": "u"},
                                    {"type": "issue", "id": 17, "url": "u"}],
                        "in_flight": [{"type": "issue", "id": 21, "url": "u"}],
                        "rejected": [{"type": "pr", "id": 43, "url": "u"}],
                        "next_candidates": []}
        mmd = render.emit_buckets_pie(b)
        self.assertTrue(mmd.startswith("pie"))
        self.assertIn('"Shipped" : 2', mmd)
        self.assertIn('"In flight" : 1', mmd)
        self.assertIn('"Rejected" : 1', mmd)
        # zero-count slices are omitted so mmdc never sees an empty slice
        self.assertNotIn("Next candidates", mmd)

    def test_pie_all_empty_has_placeholder(self):
        b = _bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
        mmd = render.emit_buckets_pie(b)
        self.assertIn('"No activity" : 1', mmd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "Pie" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'render'`.

- [ ] **Step 3: Create `render.py` with the pie emitter**

Create `.claude/skills/activity-overview/render.py`:

```python
"""Offline diagram render: enriched bundle -> Mermaid .mmd files, validated by mmdc.

Pure emitters build the diagram text from existing bundle fields; `mmdc` (mermaid-cli)
compiles every file so a diagram that would not render fails the run. No network."""

import argparse
import json
import os
import shutil
import subprocess
import sys

INSTALL_HINT = (
    "Install mermaid-cli so `mmdc` is on PATH: `npm install -g @mermaid-js/mermaid-cli`."
)

_PIE_ROWS = [
    ("Shipped", "shipped"),
    ("In flight", "in_flight"),
    ("Rejected", "rejected"),
    ("Next candidates", "next_candidates"),
]


def emit_buckets_pie(bundle):
    """A Mermaid `pie` of bucket counts. Zero-count slices are dropped."""
    meta = bundle.get("meta", {})
    buckets = bundle.get("buckets", {})
    lines = ["pie showData", f"    title Work by status ({meta.get('from','')} → {meta.get('to','')})"]
    any_slice = False
    for label, key in _PIE_ROWS:
        count = len(buckets.get(key, []))
        if count:
            lines.append(f'    "{label}" : {count}')
            any_slice = True
    if not any_slice:
        lines.append('    "No activity" : 1')
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -k "Pie" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "feat(activity): render buckets pie diagram from bundle"
```

---

## Task 10: `render.py` — timeline gantt emitter

Add the `gantt` of PR lifespans + releases over the window.

**Files:**
- Modify: `.claude/skills/activity-overview/render.py`
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Add to `test_render.py`:

```python
class TestTimelineGantt(unittest.TestCase):
    def test_gantt_header_and_tasks(self):
        mmd = render.emit_timeline_gantt(_bundle())
        self.assertTrue(mmd.startswith("gantt"))
        self.assertIn("dateFormat YYYY-MM-DD", mmd)
        self.assertIn("section Pull requests", mmd)
        # merged PR -> done; open PR -> active; closed-unmerged -> crit
        self.assertIn(":done,", mmd)
        self.assertIn(":active,", mmd)
        self.assertIn(":crit,", mmd)
        self.assertIn("section Releases", mmd)
        self.assertIn(":milestone,", mmd)

    def test_gantt_labels_have_no_colons(self):
        b = _bundle()
        b["prs"][0]["title"] = "Fix: thing: with colons"
        mmd = render.emit_timeline_gantt(b)
        for line in mmd.splitlines():
            if line.strip().startswith("#42"):
                # only the status-separator colon may remain
                self.assertEqual(line.count(":"), 1)

    def test_gantt_clamps_end_after_start(self):
        b = _bundle()
        b["prs"] = [{"number": 9, "title": "weird", "state": "open",
                     "created_at": "2026-05-20T00:00:00Z",
                     "merged_at": None, "closed_at": None, "merged": False}]
        b["releases"] = []
        mmd = render.emit_timeline_gantt(b)
        # open PR with no end uses `to`; start <= end always
        self.assertIn("2026-05-20", mmd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "Gantt" -v`
Expected: FAIL with `AttributeError: module 'render' has no attribute 'emit_timeline_gantt'`.

- [ ] **Step 3: Add the gantt emitter**

Add to `render.py` after `emit_buckets_pie`:

```python
def _day(ts, default):
    return (ts or default or "")[:10]


def _gantt_label(text):
    """Mermaid gantt task names cannot contain ':' (the field separator) or
    newlines. Collapse them and trim."""
    clean = (text or "").replace(":", " -").replace("\n", " ").strip()
    return clean[:60] or "item"


def emit_timeline_gantt(bundle):
    """A Mermaid `gantt` of PR lifespans + releases across the window."""
    meta = bundle.get("meta", {})
    frm, to = meta.get("from", ""), meta.get("to", "")
    lines = [
        "gantt",
        f"    title Timeline ({frm} → {to})",
        "    dateFormat YYYY-MM-DD",
        "    axisFormat %m-%d",
    ]
    prs = sorted(bundle.get("prs", []),
                 key=lambda p: (p.get("created_at") or p.get("merged_at") or ""))
    if prs:
        lines.append("    section Pull requests")
        for p in prs:
            start = _day(p.get("created_at"), frm)
            end = _day(p.get("merged_at") or p.get("closed_at"), to)
            if end < start:
                end = start
            if p.get("merged"):
                status = "done"
            elif p.get("state") == "closed":
                status = "crit"
            else:
                status = "active"
            label = _gantt_label(f"#{p['number']} {p.get('title', '')}")
            lines.append(f"    {label} :{status}, {start}, {end}")
    releases = bundle.get("releases", [])
    if releases:
        lines.append("    section Releases")
        for r in releases:
            day = _day(r.get("published_at"), to)
            label = _gantt_label(r.get("name") or r.get("tag_name") or "release")
            lines.append(f"    {label} :milestone, {day}, 0d")
    if not prs and not releases:
        lines.append("    section Activity")
        lines.append(f"    No dated items :active, {_day(frm, '2026-01-01')}, {_day(to, frm)}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -k "Gantt" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "feat(activity): render timeline gantt diagram from bundle"
```

---

## Task 11: `render.py` — manifest writer + render()

Write each diagram to `workspace/diagrams/<name>.mmd` and record the name→path manifest in `bundle["diagrams"]`.

**Files:**
- Modify: `.claude/skills/activity-overview/render.py`
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Add to `test_render.py`:

```python
import tempfile  # add to the imports at the top of the file


class TestWriteDiagrams(unittest.TestCase):
    def test_writes_files_and_manifest(self):
        b = _bundle()
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(b, d)
            self.assertEqual(set(manifest), {"buckets_pie", "timeline_gantt"})
            for name, path in manifest.items():
                self.assertTrue(os.path.exists(path))
                self.assertTrue(path.endswith(f"{name}.mmd"))
            # manifest is recorded back onto the bundle for downstream stages
            self.assertEqual(b["diagrams"], manifest)

    def test_render_returns_mmd_text_per_diagram(self):
        out = render.render(_bundle())
        self.assertTrue(out["buckets_pie"].startswith("pie"))
        self.assertTrue(out["timeline_gantt"].startswith("gantt"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "WriteDiagrams or render_returns" -v`
Expected: FAIL with `AttributeError: module 'render' has no attribute 'write_diagrams'`.

- [ ] **Step 3: Add `render()` and `write_diagrams()`**

Add to `render.py` after `emit_timeline_gantt`:

```python
def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
    }


def write_diagrams(bundle, outdir="workspace/diagrams"):
    """Write each diagram to <outdir>/<name>.mmd and record the manifest on the
    bundle. Returns the name->path manifest."""
    os.makedirs(outdir, exist_ok=True)
    manifest = {}
    for name, text in render(bundle).items():
        path = os.path.join(outdir, f"{name}.mmd")
        with open(path, "w") as fh:
            fh.write(text)
        manifest[name] = path
    bundle["diagrams"] = manifest
    return manifest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -k "WriteDiagrams or render_returns" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "feat(activity): write diagram files and record bundle.diagrams manifest"
```

---

## Task 12: `render.py` — mmdc validation + CLI

Add the preflight (`ensure_mmdc`), the compile-validation (`validate_with_mmdc`), and `main()`. The pure tests run everywhere; the real-compile test is `skipUnless(shutil.which("mmdc"))` so the suite stays green where mmdc is absent (it is not installed in this environment — Node is, so installing mermaid-cli is possible but not required to pass tests).

**Files:**
- Modify: `.claude/skills/activity-overview/render.py`
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Add to `test_render.py`:

```python
import shutil  # add to the imports at the top of the file


class TestMmdcValidation(unittest.TestCase):
    def test_ensure_mmdc_raises_with_hint_when_absent(self):
        with self.assertRaises(SystemExit) as ctx:
            render.ensure_mmdc(which=lambda _name: None)
        self.assertEqual(ctx.exception.code, 3)

    def test_validate_raises_on_nonzero_mmdc(self):
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            class R:
                returncode = 1
                stderr = "Parse error on line 2"
            return R()

        with self.assertRaises(RuntimeError) as ctx:
            render.validate_with_mmdc(["a.mmd"], runner=fake_run,
                                      which=lambda _n: "/usr/bin/mmdc")
        self.assertIn("Parse error", str(ctx.exception))

    @unittest.skipUnless(shutil.which("mmdc"), "mmdc not installed")
    def test_real_mmdc_compiles_emitted_diagrams(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(_bundle(), d)
            render.validate_with_mmdc(list(manifest.values()))  # raises on failure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "Mmdc" -v`
Expected: FAIL with `AttributeError: module 'render' has no attribute 'ensure_mmdc'` (the real-compile test reports as skipped).

- [ ] **Step 3: Add validation + CLI to `render.py`**

Add to `render.py` after `write_diagrams`:

```python
def ensure_mmdc(which=shutil.which):
    """Return the mmdc path or fail fast with install guidance."""
    path = which("mmdc")
    if not path:
        sys.stderr.write("error: `mmdc` not found on PATH. " + INSTALL_HINT + "\n")
        raise SystemExit(3)
    return path


def validate_with_mmdc(paths, export=None, runner=subprocess.run, which=shutil.which):
    """Compile each .mmd with mmdc; raise RuntimeError on the first that fails to
    render. When `export` is 'svg'/'png' the image is kept beside the .mmd;
    otherwise it is rendered to a temp .svg purely to validate, then removed."""
    mmdc = ensure_mmdc(which)
    for path in paths:
        out = os.path.splitext(path)[0] + "." + (export or "svg")
        result = runner([mmdc, "-i", path, "-o", out, "-q"],
                        capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"mmdc failed to render {path}:\n{result.stderr}")
        if export is None:
            try:
                os.remove(out)
            except OSError:
                pass


def parse_args(argv):
    p = argparse.ArgumentParser(description="Render + validate activity-overview diagrams.")
    p.add_argument("bundle", help="Path to the enriched bundle JSON.")
    p.add_argument("--diagrams-dir", default="workspace/diagrams")
    p.add_argument("--export", choices=["svg", "png"], default=None,
                   help="Also export images beside each .mmd.")
    p.add_argument("--skip-validate", action="store_true",
                   help="Skip the mmdc compile check (not recommended).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    with open(args.bundle) as fh:
        bundle = json.load(fh)
    manifest = write_diagrams(bundle, args.diagrams_dir)
    if not args.skip_validate:
        validate_with_mmdc(list(manifest.values()), export=args.export)
    with open(args.bundle, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(
        f"rendered {len(manifest)} diagrams into {args.diagrams_dir} "
        f"({'validated' if not args.skip_validate else 'unvalidated'})\n")
    return manifest


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -v`
Expected: PASS — `test_real_mmdc_compiles_emitted_diagrams` shows as skipped (mmdc absent); all others pass.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "feat(activity): validate diagrams with mmdc preflight + render CLI"
```

---

## Task 13: Report template + SKILL procedure + BUNDLE docs

Surface the new data: CI/CD health, releases, in-flight, rejected/abandoned, forecast candidates, and the two embedded diagrams. Update the SKILL procedure to run `render.py` (with the mmdc preflight) and document the new bundle fields.

**Files:**
- Modify: `.claude/skills/activity-overview/report-template.md`
- Modify: `.claude/skills/activity-overview/SKILL.md`
- Modify: `.claude/skills/activity-overview/BUNDLE.md`

- [ ] **Step 1: Extend `report-template.md`**

Append these sections to the end of `report-template.md`:

```markdown

## Activity at a glance

Embed the rendered diagrams from `bundle.diagrams` (the `.mmd` file contents) as
fenced ```mermaid blocks:

```mermaid
{contents of diagrams.buckets_pie}
```

```mermaid
{contents of diagrams.timeline_gantt}
```

## Releases

For each release in `releases` (newest first): tag, name, date, and link.

- **{name}** (`{tag_name}`) — published {published_at}. [release]({url})

## CI/CD health

For each workflow in `workflow_stats`: total runs and success/failure split.

- **{workflow}** — {success}/{total} succeeded ({failure} failed, {cancelled} cancelled).

## In flight

For each ref in `buckets.in_flight`: title, number, link, and train id if present.

- [{title}]({url}) (#{number}){ — train `{train}`}

## Rejected / abandoned

For each ref in `buckets.rejected`: PRs closed without merge + issues closed as
not planned.

- [{title}]({url}) (#{number})

## Next up (forecast candidates)

For each ref in `buckets.next_candidates`: open items on the next milestone or
flagged high-priority — the basis for the next-release forecast.

- [{title}]({url}) (#{number}){ — train `{train}`}
```

- [ ] **Step 2: Update `SKILL.md` procedure**

In `SKILL.md`, replace the `3. **Render.** ...` step with a render-diagrams step plus the existing prose render, and add the mmdc preflight. Change the procedure's step 3 to:

```markdown
3. **Render diagrams.** Preflight: `git`, `graphify`, and `mmdc` must be on PATH
   (install mermaid-cli with `npm install -g @mermaid-js/mermaid-cli`). Then:
   ```bash
   python3 render.py workspace/bundle.json
   ```
   This writes `workspace/diagrams/*.mmd`, records `bundle.diagrams`, and **fails
   if any diagram does not compile** under `mmdc`.
4. **Write the report.** Read `workspace/bundle.json` and fill `report-template.md`,
   embedding each `bundle.diagrams` file as a ```mermaid block. Cite each fact with
   its `url`. Do not state anything the bundle does not contain.
```

And in the `## Rules` list, replace the "Phase 1 reports cover…" bullet with:

```markdown
- Phase 2 reports cover: executive summary, shipped, decision trains, **activity-at-a-glance
  diagrams, releases, CI/CD health, in-flight, rejected/abandoned, and next-up candidates**.
  Sections with no backing data are omitted rather than padded.
```

- [ ] **Step 3: Update `BUNDLE.md`**

Add a Phase 2 note to `BUNDLE.md` documenting the new/extended fields. Append:

```markdown

## Phase 2 fields

- **prs[]** gain `created_at`, `updated_at`, `milestone`, `comments`,
  `review_comments_count`, `reviewers`, `review_decision`
  (`approved|changes_requested|commented|none`), and `crossref_issues`.
- **issues[]** gain `milestone`, `updated_at`, `comments`. Open and
  not-planned-closed issues are now included, not just PR-closing issues.
- **workflows[]**, **workflow_stats{}**, **releases[]**, **milestones[]** are
  populated (see the schema block in the design spec).
- **buckets** are fully classified: `shipped`, `rejected`, `in_flight`,
  `next_candidates` (one bucket per item; precedence shipped > rejected >
  next_candidates > in_flight). Each ref may carry a `train` id.
- **diagrams{}** maps `buckets_pie` / `timeline_gantt` to their `.mmd` paths,
  written and mmdc-validated by `render.py`.
```

- [ ] **Step 4: Verify the docs mention the new pieces**

Run:
```bash
grep -c "mermaid\|In flight\|CI/CD health\|Next up" report-template.md
grep -c "render.py\|mmdc" SKILL.md
grep -c "crossref_issues\|workflow_stats\|diagrams" BUNDLE.md
```
Expected: each `grep -c` prints a non-zero count (≥3, ≥2, ≥3 respectively).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/SKILL.md .claude/skills/activity-overview/BUNDLE.md
git commit -m "docs(activity): add Phase 2 report sections, render step, bundle fields"
```

---

## Task 14: End-to-end offline integration test

One test that runs gather-assembly → link.enrich → render across the Phase 2 fixtures, asserting the bundle is fully populated and the diagrams are emitted (validation mocked, since mmdc is absent here). This is the "eyeball the whole slice" gate in code form.

**Files:**
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the test**

Add to `test_render.py`:

```python
import link  # noqa: E402  (add near the render import at the top)


class TestEndToEndOffline(unittest.TestCase):
    def test_link_then_render_produces_validated_diagrams(self):
        with open(os.path.join(FIX, "bundle_p2.json")) as fh:
            bundle = link.enrich(json.load(fh))
        # buckets populated by link
        self.assertTrue(bundle["buckets"]["shipped"])
        self.assertTrue(bundle["buckets"]["next_candidates"])
        with tempfile.TemporaryDirectory() as d:
            manifest = render.write_diagrams(bundle, d)
            # validation path runs with a stubbed mmdc (returncode 0)
            class Ok:
                returncode = 0
                stderr = ""
            render.validate_with_mmdc(list(manifest.values()),
                                      runner=lambda cmd, **kw: Ok(),
                                      which=lambda _n: "/usr/bin/mmdc")
            self.assertEqual(set(bundle["diagrams"]), {"buckets_pie", "timeline_gantt"})
            self.assertTrue(os.path.exists(manifest["buckets_pie"]))
```

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest test_render.py -k "EndToEnd" -v`
Expected: PASS.

- [ ] **Step 3: Run the entire suite**

Run: `python3 -m pytest -v` (from the skill dir)
Expected: PASS — every Phase 1 and Phase 2 test green; the one real-mmdc test skipped.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/activity-overview/test_render.py
git commit -m "test(activity): end-to-end offline link+render integration"
```

---

## Task 15: Push the branch

- [ ] **Step 1: Confirm clean tree and full suite**

Run:
```bash
git status --short
cd .claude/skills/activity-overview && python3 -m pytest -q && cd -
```
Expected: no uncommitted changes; all tests pass (one skip).

- [ ] **Step 2: Push**

```bash
git push -u origin claude/activity-overview-phase2
```
(Retry on network error with exponential backoff: 2s, 4s, 8s, 16s.)

---

## Self-Review

**1. Spec coverage (Phase 2 bullet → task):**
- Acquire: comments (counts) → Tasks 1-2; reviews (decision) → Task 3 (`summarize_reviews`) + Task 6 wiring; timeline cross-refs → Task 3 (`parse_timeline_crossrefs`) + Task 6 + Task 7 (fold into linking); workflow runs → `workflow_stats` → Task 4 + Task 6; releases, milestones → Task 4 + Task 6; PRs gain `created_at` → Task 1. ✓
- Link: fold timeline cross-refs into PR↔issue linking → Task 7; full four-way buckets with precedence + the confirmed milestone mapping → Task 8; bucket items carry train id → Task 8. ✓
- Render: new `render.py` emits `buckets_pie` (`pie`) + `timeline_gantt` (`gantt`) from existing fields → Tasks 9-11; **validated by mmdc**, preflight-checked → Task 12. ✓
- Report: CI/CD, releases, rejected/abandoned, in-flight (+ next-up) sections embedding the two `.mmd` files → Task 13. ✓
- Diagram-type mapping (pie/gantt) and "appropriate type per visual" → Tasks 9-10 emit the spec's `pie`/`gantt`. ✓

**2. Placeholder scan:** No "TBD/TODO/handle edge cases" — every code step shows complete code; every test step shows assertions; every run step gives the exact command and expected result. The one deliberate "delete the `if False else` scaffold" note in Task 6 is an explicit instruction with the corrected three lines shown, not a placeholder. ✓

**3. Type/name consistency across tasks:**
- `summarize_reviews` returns `{"reviewers", "decision"}` — consumed identically in Task 6 wiring and asserted in Task 3. ✓
- `parse_timeline_crossrefs` → list of ints; PR field `crossref_issues` set in Task 6, read in Task 7 (`build_trains`) and Task 8 fixture. ✓
- `select_milestones(milestones, ref_date)` → `(current, next)` dicts; `compute_buckets` reads `["title"]`. ✓
- `train_index` keys `("pr"|"issue", number)` match `add()`'s lookup in `compute_buckets`. ✓
- `render()` keys `buckets_pie`/`timeline_gantt` match `write_diagrams` manifest and every test's expected set. ✓
- `ensure_mmdc(which=...)` / `validate_with_mmdc(paths, export, runner, which)` signatures match all call sites (Task 12 main, Task 14 stubbed run). ✓
- `_in_window(ts, period)` permissive-on-missing is the single reason Phase 1 dateless fixtures keep classifying into `shipped`; consistently used for merged/closed/updated checks. ✓

**Backward-compat:** Phase 1 fixtures are never mutated; new logic is permissive on missing dates/period/milestones; new CLI flags have defaults so `test_parse_args_required_and_defaults` is unaffected. The only behavioral change to an existing function is `compute_buckets`, whose Phase 1 assertions (pr42+issue17 in `shipped`) still hold under permissive windowing. ✓
