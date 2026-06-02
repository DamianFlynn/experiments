# Activity Overview — Phase 3a Implementation Plan (narrative substrate)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thicken the activity-overview skill into **Phase 3a** — the *narrative substrate* slice. Acquire the **actual discussion text** (PR review-comment bodies, PR/issue conversation comment bodies, issue reactions) and a **full-window file-level code-event walk** (`git log --name-status -M -C`), then fold those into three new offline-pure Link products — an **artifacts** lifecycle ledger (file granularity), a unified **timeline** event stream, and a **feature_deltas** projection — plus two new `render.py` diagrams (`content_timeline`, `deltas_bar`). The report, `SKILL.md`, `BUNDLE.md`, and the live integration gate grow the matching sections.

**Architecture:** Unchanged from Phase 2 — three offline-pure layers feed one network layer. `gather.py` grows pure `normalize_comment` / `normalize_review_comment` / `summarize_reactions` / `parse_code_events` / `classify_artifact_path` helpers (unit-tested from recorded fixtures) plus thin network/git wiring in `acquire()`. `link.py` gains pure `build_artifacts` / `build_timeline` / `compute_feature_deltas` folds over the new raw events + existing bundle fields. `render.py` gains pure `emit_content_timeline` / `emit_deltas_bar` emitters, registered in `render()` / `write_diagrams` so the manifest gains two keys. Markdown report + SKILL + BUNDLE docs + the integration workflow grow the new sections.

**Tech Stack:** Python 3.11 stdlib only (`json`, `argparse`, `urllib`, `subprocess`, `shutil`, `unittest`); `git` for cloning + the code-event walk; `mmdc` (mermaid-cli, via Node) as a preflight-checked external binary used only to validate/export diagrams. No third-party Python packages.

**Spec:** `docs/superpowers/specs/2026-06-01-activity-overview-design.md` — "Authored-content provenance — the timeline as an event stream" (~line 212), the REST fetches / code-event walk (~lines 362-410), the bundle schema (~lines 420-492, esp. `timeline`/`artifacts`/`feature_deltas` ~lines 436, 456-464), report-template §4a/4b (~lines 646-652), and the Phase 3 phasing bullet (~line 833).

**Working directory:** `.claude/skills/activity-overview/`. Run all `python3`/`pytest` commands from that directory (it is how the existing suite is laid out — tests `sys.path.insert` the skill dir and read `fixtures/`).

**Branch:** continue on the existing `claude/activity-overview-phase2`-style branch. All commits are local; the only push is the final task.

**Backward-compatibility rule (applies to every task):** Phase 1 **and Phase 2** tests must stay green. Do **not** mutate `fixtures/rest_sample.json`, `fixtures/rest_p2_sample.json`, `fixtures/bundle_sample.json`, or `fixtures/bundle_p2.json`, and do **not** change any existing test assertion. **Exception:** Task 10 Step 4a legitimately relaxes the pre-existing render manifest-equality assertions (the manifest legitimately grows by two diagram keys — `content_timeline` and `deltas_bar`), so those specific assertions are updated to per-key/superset checks rather than strict-equality comparisons. Add **new** fixture files for Phase 3a (`fixtures/git_log_p3_sample.txt`, `fixtures/rest_p3_sample.json`, `fixtures/bundle_p3.json`). Phase 2 keeps its `comments` / `review_comments_count` integer counts on PRs/issues — Phase 3a **adds** the body arrays alongside them, it does not remove the counts. All new windowed/folding logic must degrade permissively when fields are absent (so the Phase 1/2 dateless and bodiless fixtures still process as before — an empty code-walk yields empty `artifacts`/`feature_deltas` and a social-only `timeline`).

---

## LOCKED SCOPE & explicit deferrals

Phase 3a is the **narrative substrate** only. It does **NOT** use graphify (that is Phase 3b). Explicit deferrals — the schema reserves their place; Phase 3a leaves them empty/null and documents why:

- **`symbol` and inline `comment` artifacts** — these need `git log -p` hunk extraction + tree-sitter symbol/comment parsing. Phase 3a walks at **file granularity only** (`--name-status`), so `classify_artifact_path` returns only `readme` / `doc` / `example` / `None`. Symbol- and comment-kind artifacts are **deferred to a later slice (Phase 3b+)**.
- **`code_area`** on every artifact and `area` on every feature_delta — these come from graphify communities (Phase 3b). Phase 3a sets `code_area: null` and `area: null`.
- **`hunk`** evidence on lifecycle events and feature_deltas — requires `-p` diffs. Phase 3a omits `hunk` (the schema marks it optional with `?`); file-level events carry `commit`/`author`/`date`/`ref` but no inline hunk.
- **`prs[].files`** — derivable from the clone's per-commit file lists, but reliably attributing files to a *PR* needs the merge-structure resolution that lives in Link/commits. Phase 3a **defers `prs[].files`** (the schema reserves it); the code-event walk attributes events to **commits/authors**, not PRs, and feature_deltas resolve their owning train/pr **best-effort** via the commit→PR map Link already builds (`attach_commit_prs`). Where a delta's commit has no resolvable PR, `pr`/`train` are `null`.

Everything else in the locked scope (discussion bodies, reactions + `open_high_activity`, file-level code-event walk, artifacts ledger, unified timeline, feature_deltas projection, the two diagrams, the report/docs sections, and the integration gate) **is** built here.

---

## File Structure

All paths are under `.claude/skills/activity-overview/`.

- **Modify `gather.py`** — add pure `normalize_comment`, `normalize_review_comment`, `summarize_reactions`, `derive_open_high_activity`, `parse_code_events`, `classify_artifact_path`; widen `acquire()` to fetch per-PR review comments + per-PR/issue conversation comments + per-issue reactions and to run the full-window `git log --name-status -M -C` walk; record the new PR/issue body arrays + `reactions`/`open_high_activity`.
- **Modify `link.py`** — add pure `artifact_id`, `build_artifacts`, `build_timeline`, `compute_feature_deltas`; call them from `enrich()` so `bundle["artifacts"]`, `bundle["timeline"]`, `bundle["feature_deltas"]` are populated.
- **Modify `render.py`** — add pure `emit_content_timeline`, `emit_deltas_bar`; register both in `render()` so `write_diagrams`/manifest gain `content_timeline` + `deltas_bar`.
- **Modify `test_gather.py`, `test_link.py`, `test_render.py`** — add Phase 3a test classes only; touch no existing assertion. **Exception:** Task 10 Step 4a legitimately relaxes the two pre-existing render manifest-equality assertions in `test_render.py` (the manifest legitimately grows by two diagram keys — `content_timeline` and `deltas_bar`), so those specific assertions are updated to per-key/superset checks rather than strict-equality comparisons.
- **Create fixtures:** `fixtures/git_log_p3_sample.txt` (recorded `--name-status -M -C` walk), `fixtures/rest_p3_sample.json` (recorded comment/review-comment/reaction arrays), `fixtures/bundle_p3.json` (a bundle carrying raw code-events + comments for the link/render folds).
- **Modify docs:** `report-template.md` (Content lifecycle + Feature changes sections), `SKILL.md` (mention the new sections), `BUNDLE.md` (document `timeline`, `artifacts`, `feature_deltas`, the new comment/reaction fields; note the deferrals).
- **Modify `.github/workflows/activity-overview-integration.yml`** — extend the assertion block (Phase 3a contract) and run it green on real data before the phase is done.

---

## Task 1: Pure comment + review-comment normalizers

The Phase-4 train narratives mine the **actual text** of the discussion. Two pure normalizers map raw GitHub comment objects to the bundle's comment shape `{author, author_association, body, url, id, created_at}`, tested offline.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add after `normalize_milestone`, before `fetch_all`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py` (after `TestWorkflowsReleasesMilestones`):

```python
class TestCommentsAndReactions(unittest.TestCase):
    def test_normalize_comment_maps_body_author_url_id(self):
        raw = {
            "id": 9001, "body": "Could you split this into two functions?",
            "user": {"login": "bob"}, "author_association": "MEMBER",
            "html_url": "https://github.com/o/r/issues/17#issuecomment-9001",
        }
        c = gather.normalize_comment(raw)
        self.assertEqual(c["id"], 9001)
        self.assertEqual(c["author"], "bob")
        self.assertEqual(c["author_association"], "MEMBER")
        self.assertEqual(c["body"], "Could you split this into two functions?")
        self.assertEqual(c["url"],
                         "https://github.com/o/r/issues/17#issuecomment-9001")

    def test_normalize_comment_permissive_on_missing_fields(self):
        c = gather.normalize_comment({"id": 1})
        self.assertEqual(c["id"], 1)
        self.assertIsNone(c["author"])
        self.assertEqual(c["body"], "")
        self.assertIsNone(c["url"])
        self.assertIsNone(c["author_association"])

    def test_normalize_review_comment_maps_same_shape(self):
        raw = {
            "id": 7002, "body": "nit: rename `x`.",
            "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
            "html_url": "https://github.com/o/r/pull/42#discussion_r7002",
        }
        rc = gather.normalize_review_comment(raw)
        self.assertEqual(rc, {"id": 7002, "author": "carol",
                              "author_association": "CONTRIBUTOR",
                              "body": "nit: rename `x`.",
                              "url": "https://github.com/o/r/pull/42#discussion_r7002"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "CommentsAndReactions" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'normalize_comment'`.

- [ ] **Step 3: Implement the normalizers**

Add to `gather.py` after `normalize_milestone` (and before `fetch_all`):

```python
def _normalize_comment_obj(raw):
    """Shared mapping for conversation + review comments: the bundle's comment
    shape {id, author, author_association, body, url, created_at}. Pure, permissive."""
    return {
        "id": raw.get("id"),
        "author": (raw.get("user") or {}).get("login"),
        "author_association": raw.get("author_association"),
        "body": raw.get("body") or "",
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
    }


def normalize_comment(raw):
    """Map a GitHub issue/PR conversation comment to the bundle's comment shape."""
    return _normalize_comment_obj(raw)


def normalize_review_comment(raw):
    """Map a GitHub PR review comment (inline diff comment) to the same shape."""
    return _normalize_comment_obj(raw)
```

(Both share one mapping because the bundle persists the same six fields for either source — the distinction is which array they land in: `prs[].review_comments` vs `prs[].comments` / `issues[].comments`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "CommentsAndReactions" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): add pure comment + review-comment normalizers

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 2: Pure reactions summary + high-activity signal

Issue reactions drive the spec's "upvoted-but-ignored" flow signal (later phase) and the Phase 3a `open_high_activity` boolean. Both are pure functions over the raw reactions object.

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add after `normalize_review_comment`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add to `test_gather.py` inside `class TestCommentsAndReactions`:

```python
    def test_summarize_reactions_picks_the_tracked_keys(self):
        raw = {"+1": 12, "-1": 1, "laugh": 0, "hooray": 3, "confused": 0,
               "heart": 4, "rocket": 2, "eyes": 1, "total_count": 23}
        r = gather.summarize_reactions(raw)
        self.assertEqual(r, {"+1": 12, "-1": 1, "heart": 4, "hooray": 3, "total": 23})

    def test_summarize_reactions_permissive_on_missing(self):
        self.assertEqual(gather.summarize_reactions(None),
                         {"+1": 0, "-1": 0, "heart": 0, "hooray": 0, "total": 0})
        self.assertEqual(gather.summarize_reactions({})["total"], 0)

    def test_summarize_reactions_falls_back_to_summing_when_total_absent(self):
        # GitHub usually sends total_count; if it's missing we sum the tracked keys.
        r = gather.summarize_reactions({"+1": 5, "heart": 2})
        self.assertEqual(r["total"], 7)

    def test_derive_open_high_activity_true_when_open_and_engaged(self):
        issue = {"state": "open", "comments": 7,
                 "reactions": {"+1": 9, "-1": 0, "heart": 0, "hooray": 0, "total": 9}}
        self.assertTrue(gather.derive_open_high_activity(issue))

    def test_derive_open_high_activity_false_when_closed_or_quiet(self):
        self.assertFalse(gather.derive_open_high_activity(
            {"state": "closed", "comments": 50,
             "reactions": {"+1": 99, "total": 99}}))
        self.assertFalse(gather.derive_open_high_activity(
            {"state": "open", "comments": 1,
             "reactions": {"+1": 1, "total": 1}}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "reactions or high_activity" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'summarize_reactions'`.

- [ ] **Step 3: Implement the helpers**

Add to `gather.py` after `normalize_review_comment`:

```python
# The four reaction kinds the bundle tracks (the upvote/downvote/affinity signal
# the flow analysis keys on), plus a derived total.
_TRACKED_REACTIONS = ("+1", "-1", "heart", "hooray")

# Thresholds for the Phase-3a `open_high_activity` signal: an OPEN issue with
# meaningful discussion or upvotes. Deliberately permissive — it is a hint for
# the "open risks" report section, not a hard classification.
_HIGH_ACTIVITY_COMMENTS = 5
_HIGH_ACTIVITY_UPVOTES = 5


def summarize_reactions(raw):
    """Reduce a GitHub reactions object to {'+1','-1','heart','hooray','total'}.
    Pure, permissive: missing keys count as 0; total prefers `total_count`, else
    sums the tracked keys."""
    raw = raw or {}
    out = {k: int(raw.get(k) or 0) for k in _TRACKED_REACTIONS}
    if raw.get("total_count") is not None:
        out["total"] = int(raw["total_count"])
    else:
        out["total"] = sum(out[k] for k in _TRACKED_REACTIONS)
    return out


def derive_open_high_activity(issue):
    """True for an OPEN issue with notable engagement (many comments or upvotes).
    A cheap surface for the report's open-risks section. Pure."""
    if issue.get("state") != "open":
        return False
    comments = issue.get("comments", 0) or 0
    upvotes = (issue.get("reactions") or {}).get("+1", 0) or 0
    return comments >= _HIGH_ACTIVITY_COMMENTS or upvotes >= _HIGH_ACTIVITY_UPVOTES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "reactions or high_activity" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): summarize issue reactions + open_high_activity signal

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 3: Pure artifact-path classifier

The file-level code-event walk only tracks paths that map to a recognized artifact `kind`. `classify_artifact_path(path)` is the pure gate: `readme` / `doc` / `example` / `None`. (`symbol`/`comment` are deferred — see LOCKED SCOPE.)

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (add after `derive_open_high_activity`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestArtifactPathClassifier(unittest.TestCase):
    def test_readme_basename_wins(self):
        self.assertEqual(gather.classify_artifact_path("README.md"), "readme")
        self.assertEqual(gather.classify_artifact_path("modules/x/README"), "readme")
        self.assertEqual(
            gather.classify_artifact_path("docs/README.markdown"), "readme")

    def test_example_dir_or_suffix(self):
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/main.bicep"), "example")
        self.assertEqual(
            gather.classify_artifact_path("modules/x/examples/full/main.tf"), "example")
        self.assertEqual(
            gather.classify_artifact_path("config.example.json"), "example")

    def test_doc_md_or_under_docs(self):
        self.assertEqual(gather.classify_artifact_path("docs/design.md"), "doc")
        self.assertEqual(gather.classify_artifact_path("notes/CHANGELOG.md"), "doc")
        self.assertEqual(gather.classify_artifact_path("docs/spec.txt"), "doc")

    def test_unrecognized_paths_are_none(self):
        self.assertIsNone(gather.classify_artifact_path("modules/x/main.bicep"))
        self.assertIsNone(gather.classify_artifact_path("src/app.py"))
        self.assertIsNone(gather.classify_artifact_path(""))
        self.assertIsNone(gather.classify_artifact_path(None))

    def test_precedence_readme_over_example_over_doc(self):
        # a README inside an examples dir is still a README (basename wins)
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/README.md"), "readme")
        # an example markdown under examples/ is an example, not a doc
        self.assertEqual(
            gather.classify_artifact_path("examples/basic/notes.md"), "example")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "ArtifactPathClassifier" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'classify_artifact_path'`.

- [ ] **Step 3: Implement the classifier**

Add to `gather.py` after `derive_open_high_activity`:

```python
def classify_artifact_path(path):
    """Classify a changed file path into a tracked artifact kind, or None.

    File granularity only (Phase 3a). Precedence: readme > example > doc.
      - readme : basename matches README* (any/no extension)
      - example: under an `examples/` directory, or a `*.example*` filename
      - doc    : a `*.md` file, or any file under a `docs/` directory
      - else   : None (ignored at file granularity)
    `symbol` and `comment` kinds need hunk/AST parsing and are deferred."""
    if not path:
        return None
    parts = path.split("/")
    base = parts[-1]
    if base.upper().startswith("README"):
        return "readme"
    if "examples" in parts[:-1] or ".example" in base.lower():
        return "example"
    if base.lower().endswith(".md") or "docs" in parts[:-1]:
        return "doc"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "ArtifactPathClassifier" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): classify changed paths into readme/doc/example artifact kinds

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 4: Phase 3a git-log fixture + pure code-event parser

The full-window code-event walk runs `git log --name-status -M -C --since --until`. Parse its output into raw code-events `{commit, author, date, change, path, old_path?}` over a recorded fixture. Rename/copy detection (`-M -C`) yields `R###`/`C###` status codes with two tab-separated paths.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/git_log_p3_sample.txt`
- Modify: `.claude/skills/activity-overview/gather.py` (add `parse_code_events` + a log-format constant after `parse_git_log`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Create the git-log fixture**

The walk uses the same RECORD_SEP/FIELD_SEP header convention as Phase 1's `parse_git_log` (so commit metadata parses identically), but with `--name-status` body lines instead of `--name-only`. Each status line is `STATUS\tpath` (add/modify/delete) or `STATUS\toldpath\tnewpath` (rename/copy). Tabs are written as literal `\t` here — **write real tab characters** in the file.

Write `.claude/skills/activity-overview/fixtures/git_log_p3_sample.txt` with these four commit records (header fields separated by `\x1f`, records by `\x1e`; body lines use real TAB):

```
<RS>c1c1...c1<FS><FS>Alice<FS>2026-05-03<FS>Add basic example
A	examples/basic/main.bicep
A	docs/firewall.md
<RS>c2c2...c2<FS>parent2a parent2b<FS>Bob<FS>2026-05-10<FS>Revise README and example
M	README.md
M	examples/basic/main.bicep
<RS>c3c3...c3<FS><FS>Carol<FS>2026-05-18<FS>Rename example to advanced
R096	examples/basic/main.bicep	examples/advanced/main.bicep
<RS>c4c4...c4<FS><FS>Dave<FS>2026-05-25<FS>Drop stale doc and a source file
D	docs/firewall.md
M	src/app.py
```

Concretely (replace `<RS>`=`\x1e`, `<FS>`=`\x1f`, real tabs; full 40-char SHAs):

```python
python3 - <<'PY'
import os
RS, FS = "\x1e", "\x1f"
records = [
    ("c1"*20, "", "Alice", "2026-05-03", "Add basic example",
     ["A\texamples/basic/main.bicep", "A\tdocs/firewall.md"]),
    ("c2"*20, "p2a p2b", "Bob", "2026-05-10", "Revise README and example",
     ["M\tREADME.md", "M\texamples/basic/main.bicep"]),
    ("c3"*20, "", "Carol", "2026-05-18", "Rename example to advanced",
     ["R096\texamples/basic/main.bicep\texamples/advanced/main.bicep"]),
    ("c4"*20, "", "Dave", "2026-05-25", "Drop stale doc and a source file",
     ["D\tdocs/firewall.md", "M\tsrc/app.py"]),
]
chunks = []
for sha, parents, author, date, subject, files in records:
    header = FS.join([sha, parents, author, date, subject])
    chunks.append(RS + header + "\n" + "\n".join(files))
path = "fixtures/git_log_p3_sample.txt"
os.makedirs("fixtures", exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(chunks) + "\n")
print("wrote", path)
PY
```

Run that snippet from the skill dir to materialize the fixture exactly. Verify:

Run: `python3 -c "d=open('fixtures/git_log_p3_sample.txt').read(); print(d.count(chr(0x1e)), d.count('\t'))"`
Expected: `4 8` (4 records, 8 TAB-separated status lines worth of tabs: A/A, M/M, R(2 tabs), D/M = 2+2+2+2).

- [ ] **Step 2: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestParseCodeEvents(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            self.raw = fh.read()

    def test_parses_adds_modifies_deletes(self):
        events = gather.parse_code_events(self.raw)
        adds = [e for e in events if e["change"] == "add"]
        self.assertIn(("examples/basic/main.bicep", "Alice", "2026-05-03"),
                      [(e["path"], e["author"], e["date"]) for e in adds])
        deletes = [e for e in events if e["change"] == "delete"]
        self.assertEqual([e["path"] for e in deletes], ["docs/firewall.md"])
        modifies = [e for e in events if e["change"] == "modify"]
        self.assertIn("README.md", [e["path"] for e in modifies])

    def test_rename_carries_old_and_new_path(self):
        events = gather.parse_code_events(self.raw)
        renames = [e for e in events if e["change"] == "rename"]
        self.assertEqual(len(renames), 1)
        r = renames[0]
        self.assertEqual(r["old_path"], "examples/basic/main.bicep")
        self.assertEqual(r["path"], "examples/advanced/main.bicep")
        self.assertEqual(r["author"], "Carol")

    def test_every_event_carries_commit_author_date(self):
        for e in gather.parse_code_events(self.raw):
            self.assertEqual(len(e["commit"]), 40)
            self.assertTrue(e["author"])
            self.assertEqual(len(e["date"]), 10)
            self.assertIn(e["change"], {"add", "modify", "delete", "rename", "copy"})

    def test_non_rename_events_have_no_old_path_key(self):
        events = gather.parse_code_events(self.raw)
        add = next(e for e in events if e["change"] == "add")
        self.assertNotIn("old_path", add)

    def test_empty_input_yields_no_events(self):
        self.assertEqual(gather.parse_code_events(""), [])
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "ParseCodeEvents" -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'parse_code_events'`.

- [ ] **Step 4: Implement the parser + the log-format constant**

Add to `gather.py` immediately after `parse_git_log` (the constant documents the exact `git log` invocation the walk uses; `acquire()` in Task 6 reuses it):

```python
# git log format for the full-window code-event walk (Phase 3a). Same RECORD_SEP/
# FIELD_SEP header as parse_git_log, but the BODY is `--name-status` lines so each
# changed path carries its change type (and rename/copy detection via -M -C gives
# `R###`/`C###` with old+new paths).
CODE_LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ad%x1f%s"

_STATUS_TO_CHANGE = {"A": "add", "M": "modify", "D": "delete",
                     "R": "rename", "C": "copy", "T": "modify"}


def parse_code_events(raw):
    """Parse `git log --name-status -M -C` output into raw code-events.

    Each event: {commit, author, date, change, path[, old_path]} where change is
    add|modify|delete|rename|copy. Rename/copy lines (`R###`/`C###`) carry the old
    path in `old_path` and the new path in `path`. Pure; permissive on junk lines.
    """
    events = []
    for chunk in raw.split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, _parents, author, date, _subject = (f.strip() for f in fields[:5])
        for ln in lines[1:]:
            if not ln.strip():
                continue
            cols = ln.split("\t")
            status = cols[0].strip()
            change = _STATUS_TO_CHANGE.get(status[:1])
            if change is None or len(cols) < 2:
                continue
            ev = {"commit": sha, "author": author, "date": date, "change": change}
            if change in ("rename", "copy") and len(cols) >= 3:
                ev["old_path"] = cols[1].strip()
                ev["path"] = cols[2].strip()
            else:
                ev["path"] = cols[1].strip()
            events.append(ev)
    return events
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest test_gather.py -k "ParseCodeEvents" -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py .claude/skills/activity-overview/fixtures/git_log_p3_sample.txt
git commit -m "$(cat <<'EOF'
feat(activity): parse git --name-status -M -C into raw code-events

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 5: Phase 3a REST fixture (comments + review comments + reactions)

A single recorded-response fixture drives the offline acquire-assembly test (Task 6). Kept separate from `rest_p2_sample.json` so Phase 1/2 tests are untouched.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/rest_p3_sample.json`

- [ ] **Step 1: Create the fixture**

Write `.claude/skills/activity-overview/fixtures/rest_p3_sample.json`:

```json
{
  "window": {"from": "2026-05-01", "to": "2026-05-31"},
  "pr_review_comments": {
    "42": [
      {"id": 7001, "body": "Inline: extract this branch.",
       "user": {"login": "bob"}, "author_association": "MEMBER",
       "html_url": "https://github.com/o/r/pull/42#discussion_r7001"}
    ],
    "44": []
  },
  "pr_comments": {
    "42": [
      {"id": 8001, "body": "LGTM once the example is added.",
       "user": {"login": "carol"}, "author_association": "CONTRIBUTOR",
       "html_url": "https://github.com/o/r/pull/42#issuecomment-8001"}
    ],
    "44": [
      {"id": 8002, "body": "Holding for the next milestone.",
       "user": {"login": "alice"}, "author_association": "MEMBER",
       "html_url": "https://github.com/o/r/pull/44#issuecomment-8002"}
    ]
  },
  "issue_comments": {
    "18": [
      {"id": 9001, "body": "+1, we need this for the firewall module.",
       "user": {"login": "dave"}, "author_association": "CONTRIBUTOR",
       "html_url": "https://github.com/o/r/issues/18#issuecomment-9001"},
      {"id": 9002, "body": "Agreed — biggest gap right now.",
       "user": {"login": "erin"}, "author_association": "NONE",
       "html_url": "https://github.com/o/r/issues/18#issuecomment-9002"}
    ]
  },
  "issue_reactions": {
    "18": {"+1": 9, "-1": 0, "laugh": 0, "hooray": 2, "confused": 0,
           "heart": 1, "rocket": 0, "eyes": 3, "total_count": 12},
    "21": {"+1": 1, "-1": 0, "hooray": 0, "heart": 0, "total_count": 1}
  }
}
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import json; d=json.load(open('fixtures/rest_p3_sample.json')); print(len(d['issue_comments']['18']), d['issue_reactions']['18']['total_count'])"`
Expected: `2 12`

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/activity-overview/fixtures/rest_p3_sample.json
git commit -m "$(cat <<'EOF'
test(activity): add Phase 3a REST sample (comments + reactions)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 6: Widen `acquire()` for bodies, reactions, and the code-event walk

Wire the pure helpers (Tasks 1-4) into `acquire()`: per kept-PR review comments + conversation comments, per-issue conversation comments + reactions (+ `open_high_activity`), and the full-window `git log --name-status -M -C` walk recorded under a new bundle field `code_events`. Network/git wiring is verified **offline** by composing the helpers over the Task 5 + Task 4 fixtures (mirrors the existing `TestAcquireAssemblyP2` pattern — no network).

**Files:**
- Modify: `.claude/skills/activity-overview/gather.py` (`acquire()` — comment/reaction fetches per item; the code-walk `run_git` call; `build_bundle` post-assembly to add `code_events`)
- Test: `.claude/skills/activity-overview/test_gather.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_gather.py`:

```python
class TestAcquireAssemblyP3(unittest.TestCase):
    """Compose the Phase 3a helpers over recorded REST + git-log, offline."""

    def _bundle(self):
        with open(os.path.join(FIX, "rest_p2_sample.json")) as fh:
            p2 = json.load(fh)
        with open(os.path.join(FIX, "rest_p3_sample.json")) as fh:
            p3 = json.load(fh)
        with open(os.path.join(FIX, "git_log_p3_sample.txt")) as fh:
            code_events = gather.parse_code_events(fh.read())

        frm, to = p2["window"]["from"], p2["window"]["to"]
        prs = [gather.normalize_pr(p) for p in p2["pulls"]]
        for pr in prs:
            n = str(pr["number"])
            pr["review_comments"] = [gather.normalize_review_comment(c)
                                     for c in p3["pr_review_comments"].get(n, [])]
            pr["comments_list"] = [gather.normalize_comment(c)
                                   for c in p3["pr_comments"].get(n, [])]
        issues = [gather.normalize_issue(i) for i in p2["issues"].values()]
        for issue in issues:
            n = str(issue["number"])
            issue["comments_list"] = [gather.normalize_comment(c)
                                      for c in p3["issue_comments"].get(n, [])]
            issue["reactions"] = gather.summarize_reactions(
                p3["issue_reactions"].get(n))
            issue["open_high_activity"] = gather.derive_open_high_activity(issue)
        meta = {"owner": "o", "repo": "r", "from": frm, "to": to,
                "period": {"from": frm, "to": to}, "ref_date": to}
        bundle = gather.build_bundle(meta, [], prs, issues)
        bundle["code_events"] = code_events
        return bundle

    def test_pr_carries_review_comment_bodies(self):
        b = self._bundle()
        pr42 = next(p for p in b["prs"] if p["number"] == 42)
        self.assertEqual(pr42["review_comments"][0]["body"],
                         "Inline: extract this branch.")
        self.assertEqual(pr42["review_comments"][0]["author"], "bob")
        # Phase 2's integer counts are preserved alongside the new arrays.
        self.assertEqual(pr42["review_comments_count"], 1)
        self.assertEqual(pr42["comments_list"][0]["body"],
                         "LGTM once the example is added.")

    def test_issue_carries_comments_reactions_and_signal(self):
        b = self._bundle()
        issue18 = next(i for i in b["issues"] if i["number"] == 18)
        self.assertEqual(len(issue18["comments_list"]), 2)
        self.assertEqual(issue18["reactions"]["+1"], 9)
        self.assertEqual(issue18["reactions"]["total"], 12)
        self.assertTrue(issue18["open_high_activity"])  # open + 9 upvotes
        issue21 = next(i for i in b["issues"] if i["number"] == 21)
        self.assertFalse(issue21["open_high_activity"])  # open but quiet

    def test_code_events_present_on_bundle(self):
        b = self._bundle()
        kinds = {e["change"] for e in b["code_events"]}
        self.assertEqual(kinds, {"add", "modify", "delete", "rename"})
```

> **Naming note:** the test stores conversation comments under `comments_list` (not `comments`) because Phase 2 already uses `prs[].comments` / `issues[].comments` for the **integer count**. To avoid clobbering that count while honoring the spec's `comments` body array, Step 3 normalizes acquire() to write bodies under `comments` **on a fresh key and keep the count under `comments_count`** — see the rename decision below. The test above uses `comments_list` as the body-array key; Step 3 fixes the key to the final name and this test is updated to match in the same step.

- [ ] **Step 2: Resolve the `comments` field-name collision (decision, applied in Step 3)**

Phase 2 set `prs[].comments` / `issues[].comments` to an **integer**. The spec's Phase 3a schema uses `comments: [ {author,...} ]` (an array of bodies). Two fields cannot share one name. **Decision:** keep the integer count under the new key `comments_count` (added alongside, non-breaking — Phase 2 tests assert `comments == <int>`, so we must NOT remove `comments` as an int on the existing fixtures… therefore the inverse): **keep `comments` as the Phase 2 integer count unchanged, and store the body arrays under `comments_list`.** This preserves every existing assertion (`test_normalize_pr_captures_phase2_fields` asserts `pr["comments"] == 4`) and adds the bodies non-destructively. `BUNDLE.md` (Task 11) documents that the body array lives in `comments_list` and the integer in `comments`, noting the spec's `comments` body-array name was taken by the Phase 2 count.

(Net: the Task 1 test and the Task 6 test both use `comments_list`; no existing assertion changes. This is the single naming deviation from the spec's literal field name, recorded explicitly here and in BUNDLE.md.)

- [ ] **Step 3: Run the assembly test to verify it fails**

Run: `python3 -m pytest test_gather.py -k "AcquireAssemblyP3" -v`
Expected: PASS already for the pure-composition path **if** Tasks 1-4 are complete (this test only composes pure helpers + `build_bundle`, like `TestAcquireAssemblyP2`). If it passes, that confirms the helpers compose; proceed to wire `acquire()` (Step 4), which the live integration gate (Task 12) exercises against real data. If run before Tasks 1-4, it fails with `AttributeError`.

- [ ] **Step 4: Wire `acquire()`**

In `acquire()` (gather.py), inside the kept-PR loop (after `pr["crossref_issues"] = parse_timeline_crossrefs(timeline)`, before `prs.append(pr)`), add the body fetches:

```python
        review_comments = fetch_all(
            get_page, f"{api}/pulls/{pr['number']}/comments?per_page=100")
        pr["review_comments"] = [normalize_review_comment(c) for c in review_comments]
        conv_comments = fetch_all(
            get_page, f"{api}/issues/{pr['number']}/comments?per_page=100")
        pr["comments_list"] = [normalize_comment(c) for c in conv_comments]
```

After issues are assembled (after the `for n in sorted(wanted - seen):` loop completes), add the per-issue comment + reaction enrichment:

```python
    for issue in issues:
        n = issue["number"]
        conv = fetch_all(get_page, f"{api}/issues/{n}/comments?per_page=100")
        issue["comments_list"] = [normalize_comment(c) for c in conv]
        raw_reactions, _ = http_get_json(
            f"{api}/issues/{n}/reactions?per_page=1", token)
        # The reactions LIST endpoint returns items, not a summary; the summary
        # counts live on the issue object's `reactions` field. acquire() already
        # has each raw issue in scope only via the issues endpoint, which omits the
        # reactions summary, so fetch the issue object's reactions summary here.
        issue_obj, _ = http_get_json(f"{api}/issues/{n}", token)
        issue["reactions"] = summarize_reactions(issue_obj.get("reactions"))
        issue["open_high_activity"] = derive_open_high_activity(issue)
```

> Implementation note for the implementer: the GitHub issue object includes a `reactions` summary object when fetched via `GET /repos/{o}/{r}/issues/{n}` (with the default `Accept` header). The walk above re-fetches the issue once per issue to read that summary; if a future optimization keeps the raw issue object from the earlier `fetch_until`, thread it through instead. Either way `summarize_reactions` is the single pure reducer.

Then add the full-window code-event walk after the existing Phase 1 `git log` block (after `commits = parse_git_log(raw)`), guarded so `--no-clone`/missing clone degrades to empty:

```python
    code_events = []
    if not args.no_clone or os.path.isdir(clone_dir):
        raw_walk = run_git([
            "git", "-C", clone_dir, "log",
            f"--since={frm}", f"--until={to}",
            f"--pretty=format:{CODE_LOG_FORMAT}", "--date=short",
            "--name-status", "-M", "-C",
        ])
        code_events = parse_code_events(raw_walk)
```

Finally, after `bundle = build_bundle(meta, commits, prs, issues)` and the existing Phase 2 assignments, add:

```python
    bundle["code_events"] = code_events
```

And reserve `code_events` in `build_bundle` so the schema is stable: in `build_bundle`'s reserved-fields block, add `"code_events": [],` next to the other reserved lists.

- [ ] **Step 5: Update the failing-test placeholder key**

The Step 1 test already uses `comments_list` (the resolved key). No change needed beyond confirming it matches `acquire()`.

- [ ] **Step 6: Run the full gather suite**

Run: `python3 -m pytest test_gather.py -v`
Expected: PASS — all Phase 1/2 tests (including `test_skeleton_has_all_top_level_keys_and_reserved_empties`, which must now also see `code_events` reserved empty — **update that reserved-empty list check is forbidden** since it iterates an explicit allow-list; `code_events` is simply an additional key it does not assert on, so it stays green) + the new `TestAcquireAssemblyP3`.

> Verify the skeleton test still passes: it asserts specific keys are empty lists/dicts and does NOT assert the bundle has *only* those keys, so adding `code_events: []` is safe. Confirm by reading the assertion (it loops a fixed allow-list).

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/activity-overview/gather.py .claude/skills/activity-overview/test_gather.py
git commit -m "$(cat <<'EOF'
feat(activity): acquire comment/review bodies, reactions, full-window code events

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 7: `link.py` — artifacts ledger (file-level)

Fold raw `code_events` into the `artifacts` map: one entry per stable artifact id (derived from path), with `kind`, `path`, `name`, `status`, `replaced_by`, `code_area: null`, and an ordered `lifecycle` of `{event: add|change|remove, commit, author, date, ref}`. Renames link `replaced`/`replaced_by`. Pure.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add `artifact_id` + `build_artifacts` after `train_index`)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestBuildArtifacts(unittest.TestCase):
    def _events(self):
        return [
            {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
             "change": "add", "path": "examples/basic/main.bicep"},
            {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
             "change": "add", "path": "docs/firewall.md"},
            {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
             "change": "modify", "path": "README.md"},
            {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
             "change": "modify", "path": "examples/basic/main.bicep"},
            {"commit": "c3"*20, "author": "Carol", "date": "2026-05-18",
             "change": "rename", "old_path": "examples/basic/main.bicep",
             "path": "examples/advanced/main.bicep"},
            {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
             "change": "delete", "path": "docs/firewall.md"},
            {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
             "change": "modify", "path": "src/app.py"},
        ]

    def _bundle(self):
        return {"meta": {"owner": "o", "repo": "r"}, "code_events": self._events(),
                "commits": [], "prs": [], "issues": []}

    def test_unrecognized_paths_are_ignored(self):
        arts = link.build_artifacts(self._bundle())
        paths = {a["path"] for a in arts.values()}
        self.assertNotIn("src/app.py", paths)  # not a tracked artifact kind

    def test_add_then_change_builds_ordered_lifecycle(self):
        arts = link.build_artifacts(self._bundle())
        readme = next(a for a in arts.values() if a["path"] == "README.md")
        self.assertEqual(readme["kind"], "readme")
        self.assertEqual([e["event"] for e in readme["lifecycle"]], ["change"])
        self.assertEqual(readme["status"], "live")
        self.assertIsNone(readme["code_area"])  # graphify deferred to Phase 3b

    def test_delete_sets_status_removed(self):
        arts = link.build_artifacts(self._bundle())
        doc = next(a for a in arts.values() if a["path"] == "docs/firewall.md")
        self.assertEqual([e["event"] for e in doc["lifecycle"]],
                         ["add", "remove"])
        self.assertEqual(doc["status"], "removed")

    def test_rename_links_replaced_and_replaced_by(self):
        arts = link.build_artifacts(self._bundle())
        old_id = link.artifact_id("examples/basic/main.bicep")
        new_id = link.artifact_id("examples/advanced/main.bicep")
        self.assertEqual(arts[old_id]["status"], "replaced")
        self.assertEqual(arts[old_id]["replaced_by"], new_id)
        # the new artifact records an `add` event from the rename commit
        self.assertEqual(arts[new_id]["lifecycle"][0]["event"], "add")
        self.assertEqual(arts[new_id]["status"], "live")

    def test_lifecycle_refs_are_well_formed_commit_refs(self):
        arts = link.build_artifacts(self._bundle())
        for a in arts.values():
            for ev in a["lifecycle"]:
                self.assertEqual(ev["ref"]["type"], "commit")
                self.assertEqual(len(ev["ref"]["id"]), 40)
                self.assertTrue(ev["ref"]["url"].startswith("https://"))

    def test_empty_code_events_yields_empty_map(self):
        self.assertEqual(link.build_artifacts({"code_events": []}), {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "BuildArtifacts" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'build_artifacts'`.

- [ ] **Step 3: Implement `artifact_id` + `build_artifacts`**

Add to `link.py` after `train_index`. (`classify_artifact_path` lives in `gather.py`; import it so the kind gate is shared rather than duplicated.) Add `import gather` to the top of `link.py` next to the existing imports.

```python
import gather  # for classify_artifact_path (shared artifact-kind gate)


def artifact_id(path):
    """Stable artifact id from a path. Deterministic so the same file keeps the
    same id across periods (the spec's series-continuity rule)."""
    return "art:" + (path or "")


def _commit_url(bundle, sha):
    meta = bundle.get("meta", {})
    owner, repo = meta.get("owner"), meta.get("repo")
    if owner and repo:
        return f"https://github.com/{owner}/{repo}/commit/{sha}"
    return f"https://github.com/commit/{sha}"


# git change -> artifact lifecycle event. add/copy introduce; modify changes;
# delete removes; rename is handled specially (remove old + add new).
_CHANGE_TO_EVENT = {"add": "add", "copy": "add", "modify": "change", "delete": "remove"}


def build_artifacts(bundle):
    """Fold raw `code_events` into the per-artifact lifecycle ledger (file-level).

    Each tracked path (readme/doc/example) gets one entry with an ordered
    lifecycle. Renames link the old artifact (status `replaced`, `replaced_by`)
    to the new one. `code_area` is null in Phase 3a (graphify is Phase 3b). Pure.
    """
    artifacts = {}

    def ensure(path):
        kind = gather.classify_artifact_path(path)
        if kind is None:
            return None
        aid = artifact_id(path)
        if aid not in artifacts:
            artifacts[aid] = {
                "kind": kind, "path": path, "name": path.split("/")[-1],
                "status": "live", "replaced_by": None, "code_area": None,
                "lifecycle": [],
            }
        return aid

    def append_event(aid, event, ev):
        artifacts[aid]["lifecycle"].append({
            "event": event, "commit": ev["commit"], "author": ev["author"],
            "date": ev["date"],
            "ref": {"type": "commit", "id": ev["commit"],
                    "url": _commit_url(bundle, ev["commit"])},
        })

    for ev in bundle.get("code_events", []):
        change = ev["change"]
        if change in ("rename", "copy") and ev.get("old_path"):
            old_aid = ensure(ev["old_path"])
            new_aid = ensure(ev["path"])
            if new_aid is not None:
                append_event(new_aid, "add", ev)
            if old_aid is not None:
                append_event(old_aid, "remove", ev)
                artifacts[old_aid]["status"] = "replaced"
                artifacts[old_aid]["replaced_by"] = new_aid
            continue
        aid = ensure(ev["path"])
        if aid is None:
            continue
        append_event(aid, _CHANGE_TO_EVENT.get(change, "change"), ev)

    # Final status from the last lifecycle event (unless already `replaced`).
    for a in artifacts.values():
        if a["status"] == "replaced":
            continue
        last = a["lifecycle"][-1]["event"] if a["lifecycle"] else None
        a["status"] = "removed" if last == "remove" else "live"
    return artifacts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -k "BuildArtifacts" -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
feat(activity): build file-level artifacts lifecycle ledger from code events

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 8: `link.py` — unified timeline

Merge social events (PR/issue conversation comments, PR review comments, reactions-bearing issues, PR↔issue cross-refs) and code events (artifact add/change/remove) into one chronological `timeline` of `{ts, actor, layer, event, ref, subject}`, sorted by `ts`. Pure, derived from existing bundle fields + the new artifacts.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add `build_timeline` after `build_artifacts`)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestBuildTimeline(unittest.TestCase):
    def _bundle(self):
        return {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "docs/firewall.md"},
            ],
            "commits": [], "prs": [
                {"number": 42, "url": "https://github.com/o/r/pull/42",
                 "review_comments": [
                     {"id": 7001, "author": "bob", "body": "x",
                      "url": "https://github.com/o/r/pull/42#discussion_r7001"}],
                 "comments_list": [
                     {"id": 8001, "author": "carol", "body": "y",
                      "url": "https://github.com/o/r/pull/42#issuecomment-8001"}]},
            ],
            "issues": [
                {"number": 18, "url": "https://github.com/o/r/issues/18",
                 "comments_list": [
                     {"id": 9001, "author": "dave", "body": "z",
                      "url": "https://github.com/o/r/issues/18#issuecomment-9001"}],
                 "reactions": {"+1": 9, "total": 12}, "open_high_activity": True},
            ],
        }

    def test_timeline_merges_social_and_code_layers(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        tl = link.build_timeline(b)
        layers = {e["layer"] for e in tl}
        self.assertEqual(layers, {"social", "code"})

    def test_every_event_has_required_shape(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        for e in link.build_timeline(b):
            self.assertIn(e["layer"], {"social", "code"})
            self.assertTrue(e["ts"])
            self.assertIn("actor", e)
            self.assertIn("event", e)
            self.assertIn("type", e["ref"])
            self.assertTrue(str(e["ref"]["url"]).startswith("https://"))
            self.assertIn("kind", e["subject"])

    def test_timeline_sorted_by_ts(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        tl = link.build_timeline(b)
        self.assertEqual([e["ts"] for e in tl], sorted(e["ts"] for e in tl))

    def test_code_event_subject_carries_path_and_kind(self):
        b = self._bundle()
        b["artifacts"] = link.build_artifacts(b)
        code = [e for e in link.build_timeline(b) if e["layer"] == "code"][0]
        self.assertEqual(code["subject"]["path"], "docs/firewall.md")
        self.assertEqual(code["subject"]["kind"], "doc")

    def test_empty_bundle_yields_empty_timeline(self):
        self.assertEqual(link.build_timeline(
            {"prs": [], "issues": [], "artifacts": {}}), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "BuildTimeline" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'build_timeline'`.

- [ ] **Step 3: Implement `build_timeline`**

Add to `link.py` after `build_artifacts`:

```python
def build_timeline(bundle):
    """Merge social + code events into one chronological event stream.

    Event shape: {ts, actor, layer:'social'|'code', event, ref:{type,...,url},
    subject:{kind,name,path}}. Social events come from PR/issue comments + review
    comments; code events from artifact lifecycle entries. Sorted by ts. Comments
    in early phases may lack a precise per-comment timestamp, so ts falls back to
    the comment url ordering via a stable secondary key. Pure.
    """
    events = []

    def social(actor, event, ref_type, number, url, subject, ts):
        events.append({
            "ts": ts or "", "actor": actor, "layer": "social", "event": event,
            "ref": {"type": ref_type, "number": number, "url": url},
            "subject": subject,
        })

    for pr in bundle.get("prs", []):
        url = pr.get("url")
        for c in pr.get("review_comments", []):
            social(c.get("author"), "review_comment", "pr", pr["number"],
                   c.get("url") or url,
                   {"kind": "review_comment", "name": None, "path": None},
                   c.get("created_at"))
        for c in pr.get("comments_list", []):
            social(c.get("author"), "comment", "pr", pr["number"],
                   c.get("url") or url,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at"))

    for issue in bundle.get("issues", []):
        url = issue.get("url")
        for c in issue.get("comments_list", []):
            social(c.get("author"), "comment", "issue", issue["number"],
                   c.get("url") or url,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at"))

    for art in bundle.get("artifacts", {}).values():
        for ev in art.get("lifecycle", []):
            events.append({
                "ts": ev.get("date") or "", "actor": ev.get("author"),
                "layer": "code", "event": ev["event"], "ref": ev["ref"],
                "subject": {"kind": art["kind"], "name": art["name"],
                            "path": art["path"]},
            })

    # Stable sort by ts (then by url so equal-ts events are deterministic).
    events.sort(key=lambda e: (e["ts"], str(e["ref"].get("url") or "")))
    return events
```

> Note: GitHub comment objects carry `created_at`; the Phase 3a normalizers (Task 1) persist it in the comment shape `{author, author_association, body, url, id, created_at}`, so social `ts` is populated when the field is present in the bundle. Code events (which carry `date`) sort by their date string. The `subject.path`/`name` are `None` for social events (a comment has no file subject) and populated for code events.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -k "BuildTimeline" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
feat(activity): build unified social+code event timeline

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 9: `link.py` — feature_deltas projection over artifacts

Project the `artifacts` ledger into `feature_deltas`: one delta per lifecycle event, `add→add`, `remove→drop`, `change→change`, attributing author/commit and (best-effort) the owning train/pr via the commit→PR map. `area: null`, `hunk` omitted (deferred). Pure.

**Files:**
- Modify: `.claude/skills/activity-overview/link.py` (add `compute_feature_deltas` after `build_timeline`)
- Test: `.claude/skills/activity-overview/test_link.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `test_link.py`:

```python
class TestComputeFeatureDeltas(unittest.TestCase):
    def _bundle(self):
        b = {
            "meta": {"owner": "o", "repo": "r"},
            "code_events": [
                {"commit": "c1"*20, "author": "Alice", "date": "2026-05-03",
                 "change": "add", "path": "examples/basic/main.bicep"},
                {"commit": "c4"*20, "author": "Dave", "date": "2026-05-25",
                 "change": "delete", "path": "docs/firewall.md"},
                {"commit": "c2"*20, "author": "Bob", "date": "2026-05-10",
                 "change": "modify", "path": "README.md"},
            ],
            # commit c1 resolves to PR 42 via its message; others do not.
            "commits": [
                {"sha": "c1"*20, "message": "Add basic example (#42)", "pr": None},
            ],
            "prs": [{"number": 42, "url": "https://github.com/o/r/pull/42"}],
            "issues": [], "trains": [
                {"id": "train-pr-42", "prs": [42], "root_issue": None}],
        }
        link.attach_commit_prs(b["commits"])
        b["artifacts"] = link.build_artifacts(b)
        return b

    def test_add_remove_change_map_to_delta_kinds(self):
        deltas = link.compute_feature_deltas(self._bundle())
        kinds = {(d["subject"], d["kind"]) for d in deltas}
        self.assertIn(("example", "add"), kinds)
        self.assertIn(("readme", "change"), kinds)
        self.assertIn(("doc", "drop"), kinds)

    def test_delta_attributes_author_commit_and_artifact(self):
        deltas = link.compute_feature_deltas(self._bundle())
        add = next(d for d in deltas if d["kind"] == "add")
        self.assertEqual(add["author"], "Alice")
        self.assertEqual(add["commit"], "c1"*20)
        self.assertEqual(add["artifact"], link.artifact_id("examples/basic/main.bicep"))
        self.assertTrue(add["url"].startswith("https://"))
        self.assertIsNone(add["area"])  # graphify deferred

    def test_delta_resolves_owning_pr_and_train_when_known(self):
        deltas = link.compute_feature_deltas(self._bundle())
        add = next(d for d in deltas if d["kind"] == "add")
        self.assertEqual(add["pr"], 42)          # c1 -> (#42)
        self.assertEqual(add["train"], "train-pr-42")
        drop = next(d for d in deltas if d["kind"] == "drop")
        self.assertIsNone(drop["pr"])            # c4 has no resolvable PR
        self.assertIsNone(drop["train"])

    def test_empty_artifacts_yield_no_deltas(self):
        self.assertEqual(link.compute_feature_deltas(
            {"artifacts": {}, "commits": [], "trains": []}), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_link.py -k "ComputeFeatureDeltas" -v`
Expected: FAIL with `AttributeError: module 'link' has no attribute 'compute_feature_deltas'`.

- [ ] **Step 3: Implement `compute_feature_deltas`**

Add to `link.py` after `build_timeline`:

```python
_EVENT_TO_DELTA = {"add": "add", "remove": "drop", "change": "change"}


def compute_feature_deltas(bundle):
    """Project the artifacts ledger into the feature_deltas view.

    One delta per lifecycle event: add->add, remove->drop, change->change. Each
    attributes author/commit/url + (best-effort) the owning pr/train via the
    commit->PR map Link already builds. `area`/`before`/`after`/`detail` are null
    in Phase 3a (graphify + hunk parsing are later slices). Pure.
    """
    commit_to_pr = {c["sha"]: c.get("pr") for c in bundle.get("commits", [])}
    pr_to_train = {}
    for t in bundle.get("trains", []):
        for n in t.get("prs", []):
            pr_to_train[n] = t["id"]

    deltas = []
    for aid, art in bundle.get("artifacts", {}).items():
        for ev in art.get("lifecycle", []):
            kind = _EVENT_TO_DELTA.get(ev["event"])
            if kind is None:
                continue
            pr = commit_to_pr.get(ev["commit"])
            deltas.append({
                "area": None,
                "kind": kind,
                "subject": art["kind"],
                "name": art["name"],
                "before": None,
                "after": None,
                "detail": None,
                "artifact": aid,
                "author": ev["author"],
                "train": pr_to_train.get(pr) if pr is not None else None,
                "pr": pr,
                "commit": ev["commit"],
                "url": ev["ref"]["url"],
            })
    return deltas
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_link.py -k "ComputeFeatureDeltas" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire all three folds into `enrich()`**

In `link.py`, extend `enrich()` to populate the three new products (order matters: artifacts → timeline/deltas depend on it):

```python
def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets,
    and the Phase 3a narrative substrate (artifacts, timeline, feature_deltas)."""
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    bundle["artifacts"] = build_artifacts(bundle)
    bundle["timeline"] = build_timeline(bundle)
    bundle["feature_deltas"] = compute_feature_deltas(bundle)
    return bundle
```

> `enrich()` must stay idempotent (an existing Phase 2 test asserts re-running yields identical trains). `build_artifacts`/`build_timeline`/`compute_feature_deltas` are pure functions of `code_events` + comments + artifacts, so re-running recomputes the same maps/lists — idempotent. `attach_commit_prs` runs before `build_artifacts` so `compute_feature_deltas` sees resolved `commit.pr`.

- [ ] **Step 6: Run the full link suite (+ verify idempotency unaffected)**

Run: `python3 -m pytest test_link.py -v`
Expected: PASS — all Phase 1/2 link tests (incl. `test_enrich_is_idempotent_and_populates_both`, whose fixtures have no `code_events`, so artifacts/timeline/deltas are empty and unchanged) + the three new Phase 3a classes.

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/activity-overview/link.py .claude/skills/activity-overview/test_link.py
git commit -m "$(cat <<'EOF'
feat(activity): project feature_deltas over artifacts and wire enrich() folds

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 10: `render.py` — content_timeline + deltas_bar diagrams

Two new pure emitters, registered in `render()` so the manifest gains `content_timeline` + `deltas_bar`. `emit_content_timeline` uses Mermaid `timeline` (artifact lifecycle by date). `emit_deltas_bar` uses **`xychart-beta`** (Mermaid's native bar chart) of feature_delta counts by kind — justified below.

**Diagram-type choice (justification, recorded per the spec's "pick one and justify"):** Mermaid has no standalone "bar" diagram, but `xychart-beta` provides a real bar series (`bar [a, b, c]`) with category x-axis — it renders counts-by-category natively and matches the spec's own palette entry (`deltas_bar — xychart-beta bar`). A `pie` would lose the add/drop/change ordering and read as proportions, not counts; `xychart-beta` is the correct fit. `content_timeline` uses `timeline` (the spec's palette entry for it) because artifact lifecycles are date-stamped sections of events, which is exactly what `timeline` renders.

**Files:**
- Modify: `.claude/skills/activity-overview/render.py` (add both emitters + register in `render()`)
- Test: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Write the failing test**

Add new classes to `test_render.py`. Add a small helper bundle with artifacts + feature_deltas:

```python
def _p3_bundle():
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31"},
        "artifacts": {
            "art:docs/firewall.md": {
                "kind": "doc", "path": "docs/firewall.md", "name": "firewall.md",
                "status": "removed", "replaced_by": None, "code_area": None,
                "lifecycle": [
                    {"event": "add", "commit": "c1"*20, "author": "Alice",
                     "date": "2026-05-03", "ref": {"type": "commit", "id": "c1"*20,
                     "url": "https://github.com/o/r/commit/" + "c1"*20}},
                    {"event": "remove", "commit": "c4"*20, "author": "Dave",
                     "date": "2026-05-25", "ref": {"type": "commit", "id": "c4"*20,
                     "url": "https://github.com/o/r/commit/" + "c4"*20}},
                ]},
            "art:examples/basic/main.bicep": {
                "kind": "example", "path": "examples/basic/main.bicep",
                "name": "main.bicep", "status": "live", "replaced_by": None,
                "code_area": None,
                "lifecycle": [
                    {"event": "add", "commit": "c1"*20, "author": "Alice",
                     "date": "2026-05-03", "ref": {"type": "commit", "id": "c1"*20,
                     "url": "https://github.com/o/r/commit/" + "c1"*20}}]},
        },
        "feature_deltas": [
            {"kind": "add", "subject": "example", "name": "main.bicep",
             "artifact": "art:examples/basic/main.bicep", "author": "Alice",
             "url": "u", "area": None, "pr": 42, "train": "train-pr-42",
             "commit": "c1"*20},
            {"kind": "add", "subject": "doc", "name": "firewall.md",
             "artifact": "art:docs/firewall.md", "author": "Alice", "url": "u",
             "area": None, "pr": None, "train": None, "commit": "c1"*20},
            {"kind": "drop", "subject": "doc", "name": "firewall.md",
             "artifact": "art:docs/firewall.md", "author": "Dave", "url": "u",
             "area": None, "pr": None, "train": None, "commit": "c4"*20},
        ],
    }


class TestContentTimeline(unittest.TestCase):
    def test_timeline_header_and_dated_events(self):
        mmd = render.emit_content_timeline(_p3_bundle())
        self.assertTrue(mmd.startswith("timeline"))
        self.assertIn("2026-05-03", mmd)
        self.assertIn("2026-05-25", mmd)
        # artifact names surface in the event text
        self.assertIn("firewall.md", mmd)

    def test_timeline_placeholder_when_no_artifacts(self):
        mmd = render.emit_content_timeline(
            {"meta": {}, "artifacts": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("timeline"))
        self.assertIn("No content events", mmd)


class TestDeltasBar(unittest.TestCase):
    def test_bar_uses_xychart_and_counts_by_kind(self):
        mmd = render.emit_deltas_bar(_p3_bundle())
        self.assertTrue(mmd.startswith("xychart-beta"))
        # 2 add, 1 drop, 0 change -> bar series [2, 1, 0]
        self.assertIn("bar [2, 1, 0]", mmd)
        self.assertIn("add", mmd)
        self.assertIn("drop", mmd)
        self.assertIn("change", mmd)

    def test_bar_placeholder_when_no_deltas(self):
        mmd = render.emit_deltas_bar({"meta": {}, "feature_deltas": []})
        self.assertTrue(mmd.startswith("xychart-beta"))
        self.assertIn("bar [0, 0, 0]", mmd)


class TestRenderManifestP3(unittest.TestCase):
    def test_render_includes_phase3a_diagrams(self):
        out = render.render(_p3_bundle())
        self.assertTrue(out["content_timeline"].startswith("timeline"))
        self.assertTrue(out["deltas_bar"].startswith("xychart-beta"))
        # Phase 2 diagrams still present
        self.assertIn("buckets_pie", out)
        self.assertIn("timeline_gantt", out)

    def test_write_diagrams_manifest_gains_two_keys(self):
        b = _p3_bundle()
        b["buckets"] = {"shipped": [], "in_flight": [], "rejected": [],
                        "next_candidates": []}
        b["prs"] = []
        b["releases"] = []
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(b, os.path.join(d, "diagrams"))
            self.assertEqual(
                set(real),
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"})
            self.assertIn("content_timeline", b["diagrams"])
            self.assertIn("deltas_bar", b["diagrams"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_render.py -k "ContentTimeline or DeltasBar or RenderManifestP3" -v`
Expected: FAIL with `AttributeError: module 'render' has no attribute 'emit_content_timeline'`.

- [ ] **Step 3: Implement both emitters + register them**

Add to `render.py` after `emit_timeline_gantt`:

```python
def _timeline_text(text):
    """Mermaid `timeline` event text cannot contain ':' (section/event separator)
    or newlines. Sanitise like the gantt labels."""
    clean = (text or "").replace(":", " -").replace("\n", " ")
    while "%%" in clean:
        clean = clean.replace("%%", "%")
    return clean.strip()[:60] or "event"


def emit_content_timeline(bundle):
    """A Mermaid `timeline` of artifact lifecycle events (built/changed/dropped),
    grouped by date. Derived from `artifacts`."""
    meta = bundle.get("meta", {})
    lines = ["timeline",
             f"    title Content lifecycle ({meta.get('from','')} - {meta.get('to','')})"]
    # Collect (date, text) for every lifecycle event, grouped under its date.
    by_date = {}
    verb = {"add": "built", "change": "changed", "remove": "dropped"}
    for art in bundle.get("artifacts", {}).values():
        for ev in art.get("lifecycle", []):
            day = (ev.get("date") or "")[:10] or "undated"
            label = _timeline_text(
                f"{verb.get(ev['event'], ev['event'])} {art['name']} "
                f"({art['kind']}) by {ev.get('author') or '?'}")
            by_date.setdefault(day, []).append(label)
    if not by_date:
        lines.append("    section Activity")
        lines.append("        No content events : none")
        return "\n".join(lines) + "\n"
    for day in sorted(by_date):
        lines.append(f"    section {day}")
        for label in by_date[day]:
            lines.append(f"        {label} : {day}")
    return "\n".join(lines) + "\n"


_DELTA_KINDS = ["add", "drop", "change"]


def emit_deltas_bar(bundle):
    """A Mermaid `xychart-beta` bar of feature_delta counts by kind.

    Mermaid has no standalone bar diagram; `xychart-beta` is its native bar chart
    (category x-axis + numeric bar series), which renders counts-by-category
    correctly — unlike `pie`, which would read as proportions and lose ordering."""
    counts = {k: 0 for k in _DELTA_KINDS}
    for d in bundle.get("feature_deltas", []):
        if d.get("kind") in counts:
            counts[d["kind"]] += 1
    values = [counts[k] for k in _DELTA_KINDS]
    top = max(values) or 1
    lines = [
        "xychart-beta",
        '    title "Feature changes by kind"',
        '    x-axis [add, drop, change]',
        f'    y-axis "Count" 0 --> {top}',
        f"    bar [{', '.join(str(v) for v in values)}]",
    ]
    return "\n".join(lines) + "\n"
```

Then register both in `render()` (replace the existing `render()` return dict):

```python
def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
        "content_timeline": emit_content_timeline(bundle),
        "deltas_bar": emit_deltas_bar(bundle),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_render.py -k "ContentTimeline or DeltasBar or RenderManifestP3" -v`
Expected: PASS.

> Note: the existing Phase 2 `TestWriteDiagrams.test_writes_files_and_manifest` asserts `b["diagrams"] == {only the two Phase 2 keys}` for the **Phase 2 `bundle_p2.json`** (which has no `feature_deltas`/`artifacts` keys → emitters still emit placeholder diagrams, so the manifest now has FOUR keys). **This breaks that existing assertion.** Per the no-mutation rule we cannot edit the assertion — so `emit_content_timeline`/`emit_deltas_bar` must still register (the manifest legitimately grows). Resolve by **updating that one Phase 2 test is disallowed**; instead, the Phase 2 test reads `bundle_p2.json` which lacks `artifacts`/`feature_deltas` — the emitters degrade to placeholders but STILL appear in the manifest, growing it to 4 keys and failing the strict-equality assertion.

- [ ] **Step 4a: Reconcile the Phase 2 manifest assertion (required)**

The Phase 2 `test_writes_files_and_manifest` uses `assertEqual(b["diagrams"], {two keys})`. Registering two more diagrams makes the manifest four keys, so that strict-equality test would fail. The backward-compat rule forbids editing existing assertions — but a manifest that legitimately grows is a real contract change, and the spec's bundle schema already lists `content_timeline`/`deltas_bar` as manifest members. **Resolution:** this is the one existing assertion that MUST be relaxed because the contract it pins is exactly what Phase 3a changes. Change only its comparison from equality to a superset check, leaving every other Phase 2 assertion intact:

In `test_render.py`, `TestWriteDiagrams.test_writes_files_and_manifest`, replace:

```python
            self.assertEqual(b["diagrams"],
                             {"buckets_pie": os.path.join("diagrams", "buckets_pie.mmd"),
                              "timeline_gantt": os.path.join("diagrams", "timeline_gantt.mmd")})
```

with:

```python
            # Phase 3a grows the manifest; assert the Phase 2 entries are still
            # present and correct rather than pinning the full set.
            self.assertEqual(b["diagrams"]["buckets_pie"],
                             os.path.join("diagrams", "buckets_pie.mmd"))
            self.assertEqual(b["diagrams"]["timeline_gantt"],
                             os.path.join("diagrams", "timeline_gantt.mmd"))
```

This is the **only** permitted edit to a pre-existing assertion in Phase 3a, justified because the manifest size is the contract Phase 3a deliberately extends. (`TestMmdcValidation.test_main_skip_validate_writes_diagrams_and_bundle` asserts `set(...) == {two keys}` on `bundle_p2.json` too — apply the same superset relaxation there: change `assertEqual(set(written["diagrams"]), {two})` to assert the two Phase 2 keys are a subset with `self.assertLessEqual({"buckets_pie","timeline_gantt"}, set(written["diagrams"]))`.) Note both relaxations in the commit body.

- [ ] **Step 5: Run the full render suite**

Run: `python3 -m pytest test_render.py -v`
Expected: PASS — Phase 2 render tests (with the two relaxed manifest assertions) + the new Phase 3a emitters; the real-mmdc test stays skipped (no working mmdc).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/activity-overview/render.py .claude/skills/activity-overview/test_render.py
git commit -m "$(cat <<'EOF'
feat(activity): emit content_timeline + deltas_bar diagrams

Relaxes two Phase 2 manifest-equality assertions to superset checks, since
the diagrams manifest legitimately grows by content_timeline + deltas_bar.

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 11: Report template + SKILL + BUNDLE docs

Surface the new data: the **Content lifecycle** and **Feature changes** sections (embedding the two new diagrams and citing artifact/feature_delta refs), and document `timeline`/`artifacts`/`feature_deltas` + the new comment/reaction fields and the deferrals.

**Files:**
- Modify: `.claude/skills/activity-overview/report-template.md`
- Modify: `.claude/skills/activity-overview/SKILL.md`
- Modify: `.claude/skills/activity-overview/BUNDLE.md`

- [ ] **Step 1: Extend `report-template.md`**

Append these two sections to the end of `report-template.md` (placed per spec §4a/4b — Feature changes then Content lifecycle, embedding the new diagrams):

```markdown

## Feature changes (add / drop / change)

The `feature_deltas` ledger as a table grouped by kind. Each row cites its
artifact and the commit/PR that changed it (`area` is null until graphify lands).

```mermaid
{contents of diagrams.deltas_bar}
```

| Kind | Subject | Name | Author | PR | Commit |
|------|---------|------|--------|----|--------|
| {kind} | {subject} | {name} | {author} | {pr or "—"} | [{commit:7}]({url}) |

## Content lifecycle (built / changed / dropped)

From `artifacts`: examples, docs, and READMEs introduced, revised, or
removed/replaced within the window — *who* authored and *who* removed each, with
dates. Surfaces "we shipped an example in March and dropped it in May", which a
tip-only diff hides. (Inline code-symbols and comments are a later slice;
`code_area` lands with graphify.)

```mermaid
{contents of diagrams.content_timeline}
```

For each artifact in `artifacts` (status `removed`/`replaced` first):

- **{name}** ({kind}) — {status}. Lifecycle: {for each event} {event} by
  {author} on {date} ([{commit:7}]({ref.url})){end}.{ if replaced_by } Replaced by
  `{replaced_by}`.{end}
```

- [ ] **Step 2: Update `SKILL.md`**

In `SKILL.md`, in the step-4 "Write the report" prose, extend the embed list, and in the `## Rules` "Phase 2 reports cover…" bullet add the new sections. Replace the Phase 2 rules bullet with:

```markdown
- Phase 3a reports additionally cover: **Feature changes (add/drop/change)** and
  **Content lifecycle (built/changed/dropped)**, embedding `diagrams.deltas_bar`
  and `diagrams.content_timeline` and citing `feature_deltas`/`artifacts` refs.
  PR/issue **comment and review-comment bodies** and issue **reactions** are now
  in the bundle for narrative grounding. Sections with no backing data are omitted.
```

And add a one-line note to step 3 (Render diagrams) that the manifest now includes `content_timeline` and `deltas_bar`:

```markdown
   The manifest now also includes `content_timeline` and `deltas_bar`.
```

- [ ] **Step 3: Update `BUNDLE.md`**

Append a Phase 3a section to `BUNDLE.md`:

```markdown

## Phase 3a fields (narrative substrate)

- **prs[]** gain `review_comments: [{author, author_association, body, url, id, created_at}]`
  (inline diff comments) and `comments_list: [{...same shape}]` (conversation
  comments). The Phase 2 integer count stays under `comments` /
  `review_comments_count` — the spec's `comments` *body-array* name was already
  taken by the Phase 2 count, so bodies live under `comments_list`.
- **issues[]** gain `comments_list: [{...}]`, `reactions: {"+1","-1","heart",
  "hooray","total"}`, and `open_high_activity: bool` (open issue with notable
  comments/upvotes).
- **code_events** (gather) — raw file-level events from the full-window
  `git log --name-status -M -C` walk: `[{commit, author, date, change:
  add|modify|delete|rename|copy, path, old_path?}]`. The raw material Link folds.
- **artifacts** `{ "<id>": { kind:"example|doc|readme", path, name,
  status:"live|removed|replaced", replaced_by:id|null, code_area:null,
  lifecycle:[{event:"add|change|remove", commit, author, date, ref}] } }`.
  File granularity only in Phase 3a. **Deferred:** `symbol`/`comment` kinds (need
  `-p` hunk + tree-sitter), `code_area` (graphify, Phase 3b), and per-event `hunk`.
- **timeline** `[{ ts, actor, layer:"social|code", event, ref:{type, number|sha,
  url}, subject:{kind, name, path} }]` — sorted social (comments/reviews) + code
  (artifact lifecycle) events. Social events have no file `subject.path` and (in
  Phase 3a) no precise `ts`; code events carry both.
- **feature_deltas** `[{ area:null, kind:"add|drop|change", subject, name, before,
  after, detail, artifact:id, author, train:id|null, pr:num|null, commit:sha, url
  }]` — a projection over `artifacts` (add→add, remove→drop, change→change).
  `area`/`before`/`after`/`detail`/`hunk` are null/absent until graphify + hunk
  parsing (later slice). `pr`/`train` resolve best-effort via the commit→PR map.
- **diagrams{}** now also maps `content_timeline` (Mermaid `timeline`) and
  `deltas_bar` (Mermaid `xychart-beta` bar).
```

- [ ] **Step 4: Verify the docs mention the new pieces**

Run:
```bash
grep -c "Content lifecycle\|Feature changes\|deltas_bar\|content_timeline" report-template.md
grep -c "Content lifecycle\|Feature changes\|reactions" SKILL.md
grep -c "artifacts\|feature_deltas\|timeline\|code_events\|open_high_activity" BUNDLE.md
```
Expected: each `grep -c` prints a non-zero count (≥3, ≥1, ≥4 respectively).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/report-template.md .claude/skills/activity-overview/SKILL.md .claude/skills/activity-overview/BUNDLE.md
git commit -m "$(cat <<'EOF'
docs(activity): document Phase 3a artifacts/timeline/feature_deltas + sections

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 12: Phase 3a fixture + end-to-end offline integration test

A single enriched-input fixture `bundle_p3.json` (carrying `code_events` + comment bodies + reactions) drives an end-to-end `link.enrich → render` test asserting the full Phase 3a slice: artifacts ledger, timeline, feature_deltas, and the four-diagram manifest.

**Files:**
- Create: `.claude/skills/activity-overview/fixtures/bundle_p3.json`
- Modify: `.claude/skills/activity-overview/test_render.py`

- [ ] **Step 1: Create the fixture**

Write `.claude/skills/activity-overview/fixtures/bundle_p3.json` (a pre-link bundle with the new raw inputs; trains/buckets/artifacts/timeline/feature_deltas empty, filled by `enrich`):

```json
{
  "meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31",
           "ref_date": "2026-05-31", "period": {"from": "2026-05-01", "to": "2026-05-31"}},
  "commits": [
    {"sha": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1",
     "message": "Add basic example (#42)", "pr": null},
    {"sha": "c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2",
     "message": "Revise README and example", "pr": null}
  ],
  "code_events": [
    {"commit": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1", "author": "Alice",
     "date": "2026-05-03", "change": "add", "path": "examples/basic/main.bicep"},
    {"commit": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1", "author": "Alice",
     "date": "2026-05-03", "change": "add", "path": "docs/firewall.md"},
    {"commit": "c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2", "author": "Bob",
     "date": "2026-05-10", "change": "modify", "path": "README.md"},
    {"commit": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3", "author": "Carol",
     "date": "2026-05-18", "change": "rename",
     "old_path": "examples/basic/main.bicep",
     "path": "examples/advanced/main.bicep"},
    {"commit": "c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4", "author": "Dave",
     "date": "2026-05-25", "change": "delete", "path": "docs/firewall.md"}
  ],
  "prs": [
    {"number": 42, "title": "Add policy param", "merged": true, "state": "closed",
     "merged_at": "2026-05-10T12:00:00Z", "closed_at": "2026-05-10T12:00:00Z",
     "milestone": "v1.2.0", "labels": ["enhancement"], "closes": [17],
     "crossref_issues": [], "url": "https://github.com/o/r/pull/42",
     "review_comments": [
       {"id": 7001, "author": "bob", "author_association": "MEMBER",
        "body": "Inline: extract this branch.",
        "url": "https://github.com/o/r/pull/42#discussion_r7001"}],
     "comments_list": [
       {"id": 8001, "author": "carol", "author_association": "CONTRIBUTOR",
        "body": "LGTM once the example is added.",
        "url": "https://github.com/o/r/pull/42#issuecomment-8001"}]}
  ],
  "issues": [
    {"number": 17, "title": "Support policy param", "kind": "feature",
     "state": "closed", "state_reason": "completed", "milestone": "v1.2.0",
     "closed_at": "2026-05-10T12:00:00Z", "updated_at": "2026-05-10T12:00:00Z",
     "labels": ["enhancement"], "url": "https://github.com/o/r/issues/17",
     "comments_list": [], "reactions": {"+1": 0, "-1": 0, "heart": 0, "hooray": 0, "total": 0},
     "open_high_activity": false},
    {"number": 18, "title": "Open feature for next release", "kind": "other",
     "state": "open", "state_reason": null, "milestone": "v1.3.0",
     "closed_at": null, "updated_at": "2026-05-22T00:00:00Z",
     "labels": [], "url": "https://github.com/o/r/issues/18",
     "comments_list": [
       {"id": 9001, "author": "dave", "author_association": "CONTRIBUTOR",
        "body": "+1, we need this for the firewall module.",
        "url": "https://github.com/o/r/issues/18#issuecomment-9001"}],
     "reactions": {"+1": 9, "-1": 0, "heart": 1, "hooray": 2, "total": 12},
     "open_high_activity": true}
  ],
  "milestones": [
    {"title": "v1.2.0", "number": 4, "state": "open", "due_on": "2026-05-31T00:00:00Z"},
    {"title": "v1.3.0", "number": 5, "state": "open", "due_on": "2026-06-30T00:00:00Z"}
  ],
  "releases": [],
  "trains": [],
  "artifacts": {},
  "timeline": [],
  "feature_deltas": [],
  "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []}
}
```

Verify it parses:

Run: `python3 -c "import json; d=json.load(open('fixtures/bundle_p3.json')); print(len(d['code_events']), len(d['issues']))"`
Expected: `5 2`

- [ ] **Step 2: Write the end-to-end test**

Add to `test_render.py`:

```python
class TestEndToEndOfflineP3(unittest.TestCase):
    def test_link_then_render_builds_full_substrate(self):
        with open(os.path.join(FIX, "bundle_p3.json")) as fh:
            bundle = link.enrich(json.load(fh))

        # artifacts: README change (live), doc add+remove (removed),
        # example renamed (old replaced -> new live)
        arts = bundle["artifacts"]
        doc = next(a for a in arts.values() if a["path"] == "docs/firewall.md")
        self.assertEqual(doc["status"], "removed")
        old_ex = arts[link.artifact_id("examples/basic/main.bicep")]
        self.assertEqual(old_ex["status"], "replaced")
        self.assertEqual(old_ex["replaced_by"],
                         link.artifact_id("examples/advanced/main.bicep"))

        # timeline: both layers, sorted, well-formed refs
        tl = bundle["timeline"]
        self.assertTrue(tl)
        self.assertEqual({e["layer"] for e in tl}, {"social", "code"})
        self.assertEqual([e["ts"] for e in tl], sorted(e["ts"] for e in tl))

        # feature_deltas: add/drop/change present; c1 -> PR 42
        kinds = {d["kind"] for d in bundle["feature_deltas"]}
        self.assertEqual(kinds, {"add", "drop", "change"})
        add42 = next(d for d in bundle["feature_deltas"]
                     if d["commit"].startswith("c1") and d["kind"] == "add")
        self.assertEqual(add42["pr"], 42)

        # render: four-diagram manifest, validation stubbed (mmdc absent here)
        with tempfile.TemporaryDirectory() as d:
            real = render.write_diagrams(bundle, os.path.join(d, "diagrams"))
            self.assertEqual(
                set(real),
                {"buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar"})

            class Ok:
                returncode = 0
                stderr = ""
            render.validate_with_mmdc(list(real.values()),
                                      runner=lambda cmd, **kw: Ok(),
                                      which=lambda _n: "/usr/bin/mmdc")
```

- [ ] **Step 3: Run the test**

Run: `python3 -m pytest test_render.py -k "EndToEndOfflineP3" -v`
Expected: PASS.

> Note: `link.enrich` builds trains from PR 42 (merged, closes #17) → `train-issue-17`. `commits[0]` message `"Add basic example (#42)"` resolves to PR 42 via `attach_commit_prs`, so the `add` delta on `examples/basic/main.bicep` (commit c1) attributes `pr: 42`, `train: train-issue-17`.

- [ ] **Step 4: Run the entire suite**

Run: `python3 -m pytest -v` (from the skill dir)
Expected: PASS — every Phase 1, Phase 2, and Phase 3a test green; the one real-mmdc test skipped.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/activity-overview/fixtures/bundle_p3.json .claude/skills/activity-overview/test_render.py
git commit -m "$(cat <<'EOF'
test(activity): end-to-end offline link+render across the Phase 3a substrate

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

---

## Task 13: Extend the live integration smoke test (per-phase gate — REQUIRED)

Extend `.github/workflows/activity-overview-integration.yml`'s assertion block to the **Phase 3a** contract, and **run it green on real data** before the phase is done. The offline unit tests prove the units; this proves the whole vertical slice still works end-to-end against a real repository.

**Files:**
- Modify: `.github/workflows/activity-overview-integration.yml`

- [ ] **Step 1: Extend the assertion block**

In the `Assert ...` step's inline Python, add Phase 3a assertions **after** the Phase 2 block (before the final `print`). Append:

```python
          # 6. Phase 3a: discussion bodies on PRs/issues.
          for p in prs:
              assert isinstance(p.get("review_comments", []), list), p["number"]
              assert isinstance(p.get("comments_list", []), list), p["number"]
              for c in p.get("review_comments", []):
                  assert "author" in c and "body" in c \
                      and str(c.get("url", "")).startswith("https://"), c
          for i in issues:
              assert isinstance(i.get("comments_list", []), list), i["number"]
              r = i.get("reactions", {})
              assert set(r) >= {"+1", "-1", "heart", "hooray", "total"}, \
                  f"issue {i['number']} reactions missing keys: {r}"
              assert isinstance(i.get("open_high_activity", False), bool), i["number"]

          # 7. Phase 3a: artifacts ledger — non-empty ordered lifecycle, valid status.
          arts = b.get("artifacts", {})
          assert isinstance(arts, dict), "artifacts must be a dict"
          for aid, a in arts.items():
              assert a["kind"] in {"example", "doc", "readme"}, \
                  f"{aid} unexpected kind {a['kind']} (symbol/comment deferred)"
              assert a["status"] in {"live", "removed", "replaced"}, aid
              assert a["lifecycle"], f"{aid} has empty lifecycle"
              for ev in a["lifecycle"]:
                  assert ev["event"] in {"add", "change", "remove"}, aid
                  assert well_formed(ev["ref"]) and ev["ref"]["type"] == "commit", aid
              if a["status"] == "replaced":
                  assert a["replaced_by"] in arts, f"{aid} replaced_by dangles"
              assert a["code_area"] is None, "code_area is deferred to Phase 3b"

          # 8. Phase 3a: timeline — well-formed events, layer in {social,code}.
          tl = b.get("timeline", [])
          assert isinstance(tl, list), "timeline must be a list"
          for e in tl:
              assert e["layer"] in {"social", "code"}, e
              assert "actor" in e and "event" in e and "ts" in e, e
              assert isinstance(e.get("ref"), dict) \
                  and str(e["ref"].get("url", "")).startswith("https://"), e
              assert set(e.get("subject", {})) >= {"kind", "name", "path"}, e
          assert [e["ts"] for e in tl] == sorted(e["ts"] for e in tl), \
              "timeline must be sorted by ts"

          # 9. Phase 3a: feature_deltas reference real artifacts; kinds valid.
          for d in b.get("feature_deltas", []):
              assert d["kind"] in {"add", "drop", "change"}, d
              assert d["artifact"] in arts, f"delta references unknown artifact {d['artifact']}"
              assert d["area"] is None, "feature_delta area is deferred to Phase 3b"
              assert str(d.get("url", "")).startswith("https://"), d
              if d.get("pr") is not None:
                  assert d["pr"] in pr_by, f"delta pr {d['pr']} unknown"

          # 10. Phase 3a: diagrams manifest now includes the two new diagrams.
          assert set(dg) >= {"buckets_pie", "timeline_gantt",
                             "content_timeline", "deltas_bar"}, \
              "diagrams manifest missing Phase 3a keys"
```

Also extend the final `print(...)` to surface the new counts (append to the f-string body):

```python
          print(f"  phase3a: artifacts={len(arts)} timeline={len(tl)} "
                f"feature_deltas={len(b.get('feature_deltas', []))} "
                f"pr_review_comments={sum(len(p.get('review_comments', [])) for p in prs)}")
```

And update the step name / maintenance header to say **Phase 3a**:

- Change the assert step `name:` from `... (Phase 2)` to `... (Phase 3a)`.
- Update the `MAINTENANCE` comment's "assertions currently cover Phase 2 …" line to mention the Phase 3a additions (discussion bodies, reactions, artifacts ledger, unified timeline, feature_deltas, content_timeline + deltas_bar).

- [ ] **Step 2: Validate the workflow YAML + embedded Python parse locally**

Run (from the repo root):
```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/activity-overview-integration.yml'))" 2>/dev/null \
  || python3 -c "print('PyYAML absent; skip YAML lint (CI validates on push)')"
```
Expected: no error (or the skip notice if PyYAML is absent — the embedded Python is exercised for real only by the live run in Step 4).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/activity-overview-integration.yml
git commit -m "$(cat <<'EOF'
ci(activity): assert Phase 3a contract (bodies, artifacts, timeline, deltas)

https://claude.ai/code/session_01NUzaWbTrTnYbxCUEJ36Byb
EOF
)"
```

- [ ] **Step 4: RUN THE GATE ON REAL DATA (required before the phase is "done")**

This workflow **MUST** be run manually and be **green on real data** before Phase 3a is considered complete. After pushing (Task 14), trigger it:

```bash
gh workflow run "activity-overview integration (live smoke test)" \
  -f owner=Azure -f repo=bicep-registry-modules
# then watch it:
gh run watch "$(gh run list --workflow='activity-overview integration (live smoke test)' \
  --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Confirm the run is green and the uploaded `activity-bundle` artifact contains `workspace/diagrams/content_timeline.mmd` and `deltas_bar.mmd`. If it goes red on real-repo data (e.g. an artifact with an empty lifecycle, an unexpected status, or a non-sorted timeline), fix `gather.py`/`link.py` and re-run until green — do NOT mark the phase done on a red gate. (If the `ACTIVITY_TEST_TOKEN` secret is expired, rotate it first; a red run caused solely by an expired PAT is a token issue, not a code regression, but the gate still must be made green before sign-off.)

---

## Task 14: Push the branch

- [ ] **Step 1: Confirm clean tree and full suite**

Run:
```bash
git status --short
cd .claude/skills/activity-overview && python3 -m pytest -q && cd -
```
Expected: no uncommitted changes; all tests pass (one skip — the real-mmdc test).

- [ ] **Step 2: Push**

```bash
git push -u origin "$(git rev-parse --abbrev-ref HEAD)"
```
(Retry on network error with exponential backoff: 2s, 4s, 8s, 16s.)

- [ ] **Step 3: Run the live gate (Task 13 Step 4) and confirm green.**

Phase 3a is done only when the full offline suite is green AND the live integration workflow is green on real data with the Phase 3a assertions.

---

## Self-Review

### 1. Locked-scope coverage (every item → task)

| # | Locked-scope item | Task(s) |
|---|---|---|
| 1 | Acquire PR **review comment** bodies → `prs[].review_comments` | Task 1 (`normalize_review_comment`) + Task 6 wiring |
| 1 | Acquire PR/issue **conversation comment** bodies → `prs[]`/`issues[]` comments | Task 1 (`normalize_comment`) + Task 6 wiring (stored as `comments_list`; see naming note) |
| 1 | Issue **reactions** summary + `open_high_activity` | Task 2 (`summarize_reactions`, `derive_open_high_activity`) + Task 6 |
| 1 | Keep Phase 2 `comments`/`review_comments_count` counts | Task 6 Step 2 decision (counts untouched; bodies added under `comments_list`) |
| 2 | File-level code-event walk + pure parser | Task 4 (`parse_code_events` + `git_log_p3_sample.txt`) + Task 6 wiring (`code_events`) |
| 2 | `classify_artifact_path` → readme/doc/example/None | Task 3 |
| 2 | DEFER `symbol`/inline `comment` artifacts | LOCKED SCOPE section + Task 3 docstring + BUNDLE.md (Task 11) + integration assert (Task 13 #7) |
| 2 | `prs[].files` from clone (note if deferred) | **DEFERRED** — stated in LOCKED SCOPE; events attribute to commits, deltas resolve pr via commit→PR map |
| 3 | Artifacts ledger (file-level) with lifecycle/status/replaced_by, `code_area: null` | Task 7 (`build_artifacts`, `artifact_id`) |
| 4 | Unified timeline (social + code), sorted | Task 8 (`build_timeline`) |
| 5 | `feature_deltas` projection over artifacts | Task 9 (`compute_feature_deltas`) + `enrich()` wiring |
| 6 | `emit_content_timeline` (timeline) + `emit_deltas_bar` (xychart-beta, justified) + manifest registration | Task 10 |
| 7 | Report + SKILL + BUNDLE doc sections | Task 11 |
| 8 | Integration smoke test (per-phase gate, run-green-required) | Task 13 |
| 9 | Push the branch | Task 14 |

All nine locked-scope items map to a task. Diagram-type choice (`xychart-beta` for `deltas_bar`, `timeline` for `content_timeline`) is justified in Task 10 and matches the spec palette (~lines 547-548). The live-mmdc test stays skip-guarded (Task 10 Step 5; unchanged `_mmdc_works()` guard).

### 2. Placeholder scan

No "TBD/TODO/handle the edge cases" left as work: every implementation step shows complete function bodies; every test step shows full assertions; every run step gives the exact `pytest`/`grep`/`gh` command and expected output. The two places that look like open questions are resolved decisions, not placeholders: (a) Task 6 Step 2 fixes the `comments` field-name collision to `comments_list` with the count preserved; (b) Task 10 Step 4a relaxes exactly two Phase 2 manifest-equality assertions to superset checks (the single justified edit to pre-existing tests, because the manifest size is the contract Phase 3a extends). The fixture-generation snippet in Task 4 is runnable Python that materializes the exact bytes (real tabs), not a sketch.

### 3. Type / name consistency across tasks

- `normalize_comment` / `normalize_review_comment` → `{id, author, author_association, body, url, created_at}` — produced in Task 1, consumed in Task 6 (PR/issue arrays) and Task 8 (`build_timeline` reads `author`/`url`/`created_at` for social `ts`). Consistent.
- `summarize_reactions` → `{"+1","-1","heart","hooray","total"}` — Task 2; read by `derive_open_high_activity` (`reactions["+1"]`), the Task 6 issue enrichment, the Task 12 fixture, and integration assert #6. Keys identical everywhere.
- `parse_code_events` → `{commit(40), author, date, change∈{add,modify,delete,rename,copy}, path, old_path?}` — Task 4; consumed by `build_artifacts` (Task 7), which dispatches on `change` via `_CHANGE_TO_EVENT` and the rename/copy special-case. `old_path` only on rename/copy — asserted in Task 4 and relied on in Task 7.
- `classify_artifact_path` lives in `gather.py` (Task 3) and is imported by `link.py` (`import gather`) in Task 7 — single shared gate, returns exactly `readme|doc|example|None`. `build_artifacts` and the integration assert (#7) both restrict `kind ∈ {example,doc,readme}`.
- `artifact_id(path)` → `"art:"+path` — Task 7; used as the artifacts map key, as `replaced_by`, as `feature_deltas[].artifact` (Task 9), and in test/fixture lookups (Tasks 7/9/12). Deterministic and consistent.
- `build_artifacts` lifecycle event shape `{event∈{add,change,remove}, commit, author, date, ref:{type:"commit", id, url}}` — Task 7; `build_timeline` (Task 8) reads `event`/`date`/`author`/`ref`; `compute_feature_deltas` (Task 9) reads `event`/`commit`/`author`/`ref.url`; `emit_content_timeline` (Task 10) reads `event`/`date`/`author`; integration assert (#7/#8) checks the same. `_EVENT_TO_DELTA` maps the same three events.
- timeline event shape `{ts, actor, layer∈{social,code}, event, ref:{type,number|sha,url}, subject:{kind,name,path}}` — Task 8; asserted in Task 8 tests, Task 12 e2e, and integration #8. Sorted-by-ts invariant asserted in all three.
- `feature_deltas` entry keys (`area,kind,subject,name,before,after,detail,artifact,author,train,pr,commit,url`) — Task 9; read by `emit_deltas_bar` (counts by `kind`), the report table (Task 11), and integration #9. `kind∈{add,drop,change}` everywhere; `area`/`pr`/`train` null-able consistently.
- `render()` keys grow to `{buckets_pie, timeline_gantt, content_timeline, deltas_bar}` — Task 10; matched by `write_diagrams` manifest, the relaxed Phase 2 tests (Task 10 Step 4a), the Task 10/12 manifest tests, and integration #10.
- `enrich()` order (attach_commit_prs → trains → buckets → artifacts → timeline → feature_deltas) ensures `compute_feature_deltas` sees resolved `commit.pr` and `build_timeline` sees built `artifacts` — Task 9 Step 5.

### 4. Backward-compatibility check

- No existing fixture is mutated; three new fixtures are added (`git_log_p3_sample.txt`, `rest_p3_sample.json`, `bundle_p3.json`).
- The Phase 2 `comments`/`review_comments_count` integer counts are untouched; bodies are additive under `comments_list`/`review_comments` (Task 6 Step 2). The `test_normalize_pr_captures_phase2_fields` assertion `pr["comments"] == 4` still holds.
- `build_bundle` gains a reserved `code_events: []`; the skeleton test loops a fixed allow-list and does not assert the absence of extra keys, so it stays green (Task 6 Step 6 verifies).
- All new Link folds degrade to empty on bundles without `code_events`/comments (the Phase 1/2 fixtures), so `test_enrich_is_idempotent_and_populates_both` and every Phase 2 link/render test stay green.
- Exactly two pre-existing assertions change — both manifest-size equality checks relaxed to superset checks (Task 10 Step 4a) — justified because the diagrams manifest is the contract Phase 3a deliberately extends, and the spec schema already lists the two new manifest members.

### 5. Explicit deferrals (flagged)

- **`symbol` + inline `comment` artifacts** — need `-p` hunk + tree-sitter; Phase 3a is file-granularity only. Deferred to Phase 3b+.
- **`code_area` (artifacts) / `area` (feature_deltas)** — graphify communities; null in Phase 3a, populated in Phase 3b.
- **`hunk` evidence** on lifecycle events + feature_deltas — needs `-p` diffs; omitted in Phase 3a.
- **`prs[].files`** — needs PR↔file attribution via merge structure; deferred (events attribute to commits, deltas to pr via commit→PR map best-effort).
- **`before`/`after`/`detail`** on feature_deltas — language-aware subject extraction; null in Phase 3a.
- **Precise social `ts`** on timeline comment events — `created_at` is persisted by the Phase 3a comment shape (`normalize_comment`/`normalize_review_comment` include it), so social events that carry a real timestamp will sort correctly; events where `created_at` is absent or null fall back to the comment URL as a stable secondary key.

All deferrals are stated in the LOCKED SCOPE section, repeated at their point of use, documented in BUNDLE.md (Task 11), and enforced as `is None` assertions in the integration gate (Task 13 #7/#9) so a future slice that populates them will deliberately flip those asserts.
