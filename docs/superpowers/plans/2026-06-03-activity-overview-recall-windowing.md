# Issue-Link Recall & Cross-Window Train Continuity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover PR→issue links that a single windowed slice currently loses, so trains anchor on their real originating issue instead of falling back to the PR.

**Architecture:** Two recall fixes in the network layer (`gather.py`), both flowing through machinery that already exists. (1) Read GitHub's authoritative `closingIssuesReferences` (GraphQL) — window-independent UI links — and merge into each PR's `closes`; the existing out-of-window issue hydration (`gather.py:1865-1870`, which fetches any issue in `closes ∪ crossref_issues` that the window didn't load) then pulls the originating issue automatically. (2) Capture bare `#N`/issue-URL mentions as a *separate, non-anchoring* `related_issues` field so context threads are recorded without corrupting the anchor. The durable cross-period roll-up is scoped OUT of this plan (see "Deferred").

**Tech Stack:** Python 3 stdlib (`urllib`, `re`, `json`), existing `gather.py` HTTP helpers, `unittest`/`pytest`.

---

## Why this plan (empirical grounding)

Measured on the real AVM bundle (`Azure/bicep-registry-modules`, window 2026-05-25..06-01): of **46 PR-anchored trains** (no issue link, anchored `train-pr-<n>`):

- **40** have **no textual issue signal** in title/body → the link, if any, lives in GitHub's UI "linked issue" dropdown = the GraphQL `closingIssuesReferences` field, which `gather.py` never reads (it only regexes the body). **→ Phase A.**
- **~6** cite an issue number **far below the in-window range** (`#2248`, `#39`, `#744`, `/issues/5903`) — the thread points back *before the window*. These are bare/contextual refs, not closing keywords. **→ Phase B (record as related), and Phase A's hydration already steps back for true closing refs.**
- **0** were colon-variant (`Closes: #N`) misses — tightening the regex is **not** the fix.

Key discovery: **out-of-window hydration already works** (`gather.py:1865-1870`) — it fetches any issue in `wanted = closes ∪ crossref_issues` that the window didn't load. So the missing piece is the *signal* (`closes`), not the plumbing. Phase A supplies the authoritative signal; hydration carries it the rest of the way.

---

## File Structure

- **`gather.py`** (modify) — add a GraphQL POST helper + a batched closing-refs fetch + two pure parsers; refactor `normalize_pr` to also emit `related_issues`; merge GraphQL closes into `pr["closes"]` in the PR loop.
- **`test_gather.py`** (modify) — unit tests for the four new **pure** functions (`parse_closing_issue_refs`, `_merge_refs`, `parse_related_refs`, `_closing_refs_query`). Network glue (`graphql_post`, the fetch loop) is **not** unit-tested, matching the existing convention (`http_get_json` is documented "Not unit-tested") — it is validated by a real re-gather (Task 7).
- **`BUNDLE.md`** (modify) — document the new `related_issues` field and that `closes` now includes GraphQL UI links.

No `link.py` change is required: `_pr_anchor` already prefers `closes[0]`, so richer `closes` automatically converts `train-pr-<n>` into `train-issue-<n>`.

---

## Phase A — Authoritative linked issues (GraphQL `closingIssuesReferences`)

### Task 1: Pure parser for the GraphQL payload

**Files:**
- Modify: `gather.py` (add near the other parsers, after `parse_closing_refs` ~line 228)
- Test: `test_gather.py`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_closing_issue_refs_maps_pr_to_issue_numbers(self):
    payload = {"repository": {
        "p7116": {"closingIssuesReferences": {"nodes": [{"number": 7062}]}},
        "p7109": {"closingIssuesReferences": {"nodes": [{"number": 7073}, {"number": 12}]}},
        "p9999": {"closingIssuesReferences": {"nodes": []}},   # none -> omitted
        "p_bad": None,                                          # null PR -> skipped
    }}
    self.assertEqual(
        gather.parse_closing_issue_refs(payload),
        {7116: [7062], 7109: [7073, 12]},
    )

def test_parse_closing_issue_refs_tolerates_empty(self):
    self.assertEqual(gather.parse_closing_issue_refs({}), {})
    self.assertEqual(gather.parse_closing_issue_refs(None), {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_gather.py -k parse_closing_issue_refs -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'parse_closing_issue_refs'`

- [ ] **Step 3: Write minimal implementation**

```python
def parse_closing_issue_refs(data):
    """Map {pr_number: [issue numbers]} from a `closingIssuesReferences` GraphQL
    payload whose PR fields are aliased `p<number>`. Pure; tolerant of nulls."""
    repo = (data or {}).get("repository") or {}
    out = {}
    for alias, node in repo.items():
        if not alias.startswith("p") or not node:
            continue
        try:
            pr_num = int(alias[1:])
        except ValueError:
            continue
        nodes = ((node.get("closingIssuesReferences") or {}).get("nodes")) or []
        refs = [n["number"] for n in nodes if isinstance(n, dict) and "number" in n]
        if refs:
            out[pr_num] = refs
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_gather.py -k parse_closing_issue_refs -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): parse closingIssuesReferences GraphQL payload"
```

### Task 2: Order-preserving ref merge

**Files:**
- Modify: `gather.py` (add beside `parse_closing_issue_refs`)
- Test: `test_gather.py`

- [ ] **Step 1: Write the failing test**

```python
def test_merge_refs_unions_order_preserving_primary_first(self):
    # body-parsed closes stay first; GraphQL-only refs appended; dups dropped
    self.assertEqual(gather._merge_refs([7062], [7062, 99]), [7062, 99])
    self.assertEqual(gather._merge_refs([], [5, 5, 6]), [5, 6])
    self.assertEqual(gather._merge_refs([1, 2], []), [1, 2])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_gather.py -k merge_refs -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute '_merge_refs'`

- [ ] **Step 3: Write minimal implementation**

```python
def _merge_refs(primary, extra):
    """Order-preserving union of two issue-number lists; `primary` first. Pure."""
    out = list(primary or [])
    for n in extra or []:
        if n not in out:
            out.append(n)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_gather.py -k merge_refs -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): order-preserving issue-ref merge helper"
```

### Task 3: Batched GraphQL query builder

**Files:**
- Modify: `gather.py`
- Test: `test_gather.py`

- [ ] **Step 1: Write the failing test**

```python
def test_closing_refs_query_aliases_each_pr(self):
    q = gather._closing_refs_query([7116, 7109])
    self.assertIn("p7116: pullRequest(number:7116)", q)
    self.assertIn("p7109: pullRequest(number:7109)", q)
    self.assertIn("closingIssuesReferences(first:20)", q)
    self.assertIn("$owner:String!", q)
    self.assertIn("$name:String!", q)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_gather.py -k closing_refs_query -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute '_closing_refs_query'`

- [ ] **Step 3: Write minimal implementation**

```python
def _closing_refs_query(pr_numbers):
    """Build a batched GraphQL query: one aliased `p<n>` pullRequest field per PR,
    each selecting its closingIssuesReferences node numbers. Pure."""
    aliases = "\n".join(
        f"    p{n}: pullRequest(number:{n})"
        f"{{ closingIssuesReferences(first:20){{ nodes{{ number }} }} }}"
        for n in pr_numbers
    )
    return (
        "query($owner:String!,$name:String!){\n"
        "  repository(owner:$owner,name:$name){\n"
        f"{aliases}\n"
        "  }\n"
        "}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_gather.py -k closing_refs_query -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): batched closingIssuesReferences query builder"
```

### Task 4: GraphQL POST transport + wire into the PR loop

**Files:**
- Modify: `gather.py` — add `graphql_post` after `_format_http_error` (~line 1635); add the fetch+merge block in the PR-collection function after the `for raw in raw_closed + raw_open:` loop ends (~line 1834, immediately before `wanted = set()` at line 1838).

> Network glue — no unit test (matches `http_get_json` "Not unit-tested"). Validated by Task 7's real re-gather.

- [ ] **Step 1: Add the transport helper**

```python
GITHUB_GRAPHQL = "https://api.github.com/graphql"


def graphql_post(query, variables, token):
    """POST a GraphQL query to GitHub → the `data` object. Not unit-tested.
    Reuses _format_http_error so 403/SSO/scope diagnostics match REST calls."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GITHUB_GRAPHQL, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "activity-overview",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        raise SystemExit(_format_http_error(GITHUB_GRAPHQL, err)) from err
    if payload.get("errors"):
        raise SystemExit(f"error: GitHub GraphQL errors: {payload['errors']}")
    return payload.get("data") or {}
```

- [ ] **Step 2: Wire the batched fetch + merge after the PR loop (gather.py ~line 1834)**

Insert immediately after `prs.append(pr)` closes its `for` loop and before `wanted = set()`:

```python
    # Authoritative GitHub UI-linked issues (window-independent), batched via
    # GraphQL and merged into each PR's body-parsed closes. This converts
    # PR-anchored trains into issue-anchored ones and feeds out-of-window issue
    # hydration below (wanted -> per-issue fetch at lines ~1865-1870).
    pr_numbers = [p["number"] for p in prs]
    closing_by_pr = {}
    for i in range(0, len(pr_numbers), 50):
        data = graphql_post(
            _closing_refs_query(pr_numbers[i:i + 50]),
            {"owner": owner, "name": repo}, token,
        )
        closing_by_pr.update(parse_closing_issue_refs(data))
    for p in prs:
        p["closes"] = _merge_refs(p["closes"], closing_by_pr.get(p["number"], []))
```

- [ ] **Step 3: Verify the module imports and the full suite is still green**

Run: `python -c "import gather"` then `python -m pytest -q`
Expected: import OK; all existing tests pass (no behavior change offline — the new code only runs against the network).

- [ ] **Step 4: Commit**

```bash
git add gather.py
git commit -m "feat(activity): fetch authoritative linked issues via GraphQL"
```

---

## Phase B — Related (non-closing) thread links

Captures bare `#N` / issue-URL mentions as a **separate** `related_issues` field. These do **not** change the anchor (anchoring stays on `closes`), avoiding false trains; they record the thread so the report can show "related: #N" and a later roll-up can use them as soft edges.

### Task 5: Pure `related_issues` parser

**Files:**
- Modify: `gather.py` (add the two regexes beside `_CLOSING_RE` ~line 217, and the function beside `parse_closing_refs`)
- Test: `test_gather.py`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_related_refs_collects_bare_and_url_minus_excluded(self):
    text = ("Implements the thing. See #2248 and "
            "https://github.com/Azure/bicep-registry-modules/issues/39 . "
            "Closes #7062.")
    # 7062 is a closing ref (excluded); self (#100) excluded; order preserved
    self.assertEqual(
        gather.parse_related_refs(text, exclude={7062, 100}),
        [2248, 39],
    )

def test_parse_related_refs_dedupes_and_tolerates_empty(self):
    self.assertEqual(gather.parse_related_refs("#5 #5 #6", exclude=set()), [5, 6])
    self.assertEqual(gather.parse_related_refs("", exclude=set()), [])
    self.assertEqual(gather.parse_related_refs(None, exclude=set()), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_gather.py -k parse_related_refs -v`
Expected: FAIL with `AttributeError: module 'gather' has no attribute 'parse_related_refs'`

- [ ] **Step 3: Write minimal implementation**

```python
_BARE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
_ISSUE_URL_RE = re.compile(r"github\.com/[\w.-]+/[\w.-]+/issues/(\d+)")


def parse_related_refs(text, exclude=()):
    """Issue numbers mentioned (bare `#N` or an issues URL) but NOT as closing
    keywords, minus `exclude` (closing refs + the PR's own number). De-duplicated,
    order-preserving (bare refs in text order, then URL refs). Pure."""
    exclude = set(exclude)
    out = []
    for m in list(_BARE_REF_RE.finditer(text or "")) + list(_ISSUE_URL_RE.finditer(text or "")):
        n = int(m.group(1))
        if n not in out and n not in exclude:
            out.append(n)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_gather.py -k parse_related_refs -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add gather.py test_gather.py
git commit -m "feat(activity): parse non-closing related issue references"
```

### Task 6: Emit `related_issues` from `normalize_pr`

**Files:**
- Modify: `gather.py:231-262` (`normalize_pr`) and `test_gather.py`; `BUNDLE.md`

- [ ] **Step 1: Write the failing test**

```python
def test_normalize_pr_emits_related_issues_excluding_closes_and_self(self):
    raw = {
        "number": 100, "title": "feat: x (see #2248)",
        "body": "Closes #7062. Related to #2248 and #100 itself.",
        "user": {"login": "a"}, "labels": [], "state": "open",
    }
    pr = gather.normalize_pr(raw)
    self.assertEqual(pr["closes"], [7062])
    self.assertEqual(pr["related_issues"], [2248])   # 7062 closing, 100 self -> out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_gather.py -k normalize_pr_emits_related -v`
Expected: FAIL with `KeyError: 'related_issues'`

- [ ] **Step 3: Refactor `normalize_pr` to compute text once and emit both fields**

Replace the inline `"closes": parse_closing_refs(...)` (gather.py:260-262) so the function computes:

```python
    _text = (raw.get("title", "") or "") + "\n" + (raw.get("body") or "")
    _closes = parse_closing_refs(_text)
    _related = parse_related_refs(_text, exclude=set(_closes) | {raw.get("number")})
```

and in the returned dict set:

```python
        "closes": _closes,
        "related_issues": _related,
```

(Keep `"crossref_issues": []` as-is; it is filled later from the timeline.)

- [ ] **Step 4: Run test + full suite**

Run: `python -m pytest test_gather.py -k normalize_pr -v && python -m pytest -q`
Expected: PASS; full suite green.

- [ ] **Step 5: Document the field in BUNDLE.md**

In the `prs` schema bullet (BUNDLE.md ~line 37-39), add `related_issues` after `base, head`:

```markdown
  `related_issues` are non-closing `#N`/issue-URL mentions (context threads, not anchors);
  `closes` now also includes GitHub's authoritative UI-linked issues (GraphQL
  `closingIssuesReferences`), so a body that links via the UI dropdown still anchors its train.
```

- [ ] **Step 6: Commit**

```bash
git add gather.py test_gather.py BUNDLE.md
git commit -m "feat(activity): emit related_issues on PRs; document recall fields"
```

---

## Task 7: Validate recall on the real AVM bundle

**Files:** none (validation only). Requires network + `GITHUB_TOKEN`.

- [ ] **Step 1: Re-gather the same AVM window**

Run (adjust token/paths to the environment):
```bash
python gather.py --owner Azure --repo bicep-registry-modules \
  --from 2026-05-25 --to 2026-06-01 --out /tmp/avm/bundle_recall.json
```
Expected: exits 0; bundle written.

- [ ] **Step 2: Measure the anchoring shift**

Run:
```bash
python3 -c "
import json,sys; sys.path.insert(0,'.'); import link
b=json.load(open('/tmp/avm/bundle_recall.json')); link.enrich(b)
from collections import Counter
print('anchoring:', dict(Counter('issue' if t['root_issue'] is not None else 'pr' for t in b['trains'])))
print('prs with related_issues:', sum(1 for p in b['prs'] if p.get('related_issues')))
"
```
Expected: the `issue`-anchored count is **materially higher** than the pre-change baseline (21 issue / 45 PR). Record the new split in the Phase-4a plan's validation-findings section.

- [ ] **Step 3: Confirm out-of-window heads were hydrated**

Run:
```bash
python3 -c "
import json; b=json.load(open('/tmp/avm/bundle_recall.json'))
nums={i['number'] for i in b['issues']}
frm=b['meta']['from']
oow=[i['number'] for i in b['issues'] if (i.get('created_at') or '')[:10] < frm]
print('issues hydrated from before the window:', len(oow), sorted(oow)[:10])
"
```
Expected: a non-empty list — the GraphQL links pulled originating issues created before the window (proving the train's head reconnected).

- [ ] **Step 4: Re-render + compile diagrams (regression guard)**

Run:
```bash
python3 -c "
import json,sys,glob,subprocess,os; sys.path.insert(0,'.'); import link,render
b=json.load(open('/tmp/avm/bundle_recall.json')); link.enrich(b)
out='/tmp/avm/d_recall'; os.makedirs(out,exist_ok=True); render.write_diagrams(b,out)
pp='/tmp/pp.json'; open(pp,'w').write(json.dumps({'args':['--no-sandbox']}))
bad=[f for f in glob.glob(out+'/*.mmd') if subprocess.run(['mmdc','-p',pp,'-i',f,'-o',f[:-4]+'.svg','-q'],capture_output=True).returncode]
print('FAILED:', bad or 'none')
"
```
Expected: `FAILED: none`.

- [ ] **Step 5: Commit the recorded findings**

```bash
git add docs/superpowers/plans/2026-06-03-activity-overview-phase4a.md
git commit -m "docs(activity): record recall re-validation (issue-anchor lift)"
```

---

## Deferred — cross-period roll-up graph (separate plan needed)

The durable answer to "a single random slice can't see a train that spans time" is the spec's **roll-up** (`docs/superpowers/specs/2026-06-01-activity-overview-design.md` lines 250-279): merge per-period bundles into a persistent graph keyed by the **stable `train-issue-<n>` / `train-pr-<n>` ids** (already deterministic), so a train that appears across months keeps one identity and accumulates its commits/PRs/outcomes, while structure (code graph) is taken from the latest installment.

This is a distinct subsystem (bundle persistence, cross-bundle merge, lifecycle fields `first_seen`/`last_seen`/`carried`, conflict resolution when an outcome changes shipped↔reopened across periods). Per the writing-plans scope check it should get its **own brainstorm + plan**, not be wedged here. Phases A–B above maximize recall *within* a window and reconnect immediate out-of-window heads; the roll-up maximizes recall *across* windows. Recommend scheduling it as the next phase once A–B land and the issue-anchor lift is measured.

**Not pursued (deliberately):** tightening `_CLOSING_RE` for `Closes: #N` (0 occurrences measured); scanning commit trailers/PR comments for closing refs (lower-yield than the authoritative GraphQL field, and noisier).

---

## Self-Review

- **Spec coverage:** Recall loss from UI links → Task 1-4. Out-of-window head → existing hydration (1865-1870) fed by Task 4; verified in Task 7 Step 3. Context threads → Task 5-6. Cross-period continuity → explicitly Deferred with rationale. ✔
- **Placeholder scan:** no TBD/"handle edge cases"/"similar to" — every code step shows full code and every run step an expected result. ✔
- **Type consistency:** `parse_closing_issue_refs` → `dict[int, list[int]]` consumed by `closing_by_pr.update(...)` then `_merge_refs(p["closes"], closing_by_pr.get(...))`; `_closing_refs_query([int])` aliases `p<n>` parsed back by `int(alias[1:])`; `parse_related_refs(text, exclude:set)` → `list[int]`, called in `normalize_pr` with `exclude=set(_closes) | {number}`. Names match across tasks. ✔
