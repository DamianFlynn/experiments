# Phase 4a — train significance + slices + forecast scaffold (implementation plan)

**Status:** planned. Branch `claude/activity-phase4a`. Builds on the merged 3a–3e `trains`,
`feature_deltas`, `symbol_moves`, `buckets`, and `label_taxonomy`. Splits the original Phase 4
into a **deterministic scaffold (4a, this plan)** and **sub-agent narration (4b, next)**. 4a is
pure Python under TDD + the live gate; it produces everything a narrator needs but writes no prose.

## Why split 4

The original Phase 4 bundled three different risks: (1) deterministic ranking/slicing/forecast
structure, (2) Mermaid train flowcharts, and (3) non-deterministic sub-agent prose. Prose can't be
unit-tested or gated the way (1)/(2) can. 4a lands and locks the testable scaffold so 4b's
sub-agents consume a stable, bounded contract — and so the same slice serves both the **ranked
report** and an **on-demand single-train spotlight** (a long-form "how this train got landed"
deep-dive). 4a makes the spotlight's data ready; 4b writes its narrative.

## What it adds

Five deterministic pieces, all keyed off the existing `trains`/`buckets`:

### 1. `score_train_significance(bundle)` (link.py, after trains are built)

Annotates each train with a **composite significance** and a treatment **tier**, so the report
ranks rather than dumps. Per train:

- `significance` — `footprint × kind_weight + breadth`, where
  - **footprint** = `len(prs) + len(commits) + len(code_areas)` (the train's raw size),
  - **kind_weight** = a tunable map over `trains[].kind` (feature/module-request heavy; bug medium;
    docs/chore/other light),
  - **breadth** = count of distinct `code_areas` (cross-cutting work scores higher).
- `tier` — `"deep"` for the **top-N** trains by significance **or** any train at/above a
  **floor** score; `"mention"` otherwise. `N` and the floor are tunable module constants
  (start `N=8`, floor chosen so a single multi-PR feature always clears it). Deep trains get a
  flowchart + (in 4b) a full narrative; mention trains get a one-liner.

Outcome is a factor only via kind/footprint, not a separate weight — a rejected-but-large train is
still significant (the "why it didn't land" story matters).

### 2. Per-train `effort` block (link.py)

The "time & effort to land it" signal, computed **only from data already in the bundle** (PR
`created_at`/`merged_at`/`updated_at`, `reviewers`, `review_comments_count`, `comments_list`,
issue `comments_list`, commits) — feeds both the forecast and the spotlight. On each train:

```
effort: { opened_at, merged_at, elapsed_days, reviewers, review_comments,
          commits, participants, stalled }
```

- `opened_at` = earliest of root-issue `created_at` / first PR `created_at`; `merged_at` = latest
  PR `merged_at` (null if none merged).
- `elapsed_days` = `merged_at − opened_at` (null when not yet merged → reported as "open N days").
- `reviewers` = distinct reviewer logins across the train's PRs (raw review *submissions* aren't
  persisted, so distinct reviewers is the honest review-effort proxy); `review_comments` = summed
  `review_comments_count`; `participants` = distinct PR/issue authors + reviewers + comment authors
  (from `comments_list`); `commits` = `len(train.commits)`.
- `stalled` = true when `elapsed_days` exceeds a tunable threshold (start 21 days) while the train
  merged — a long-running train. (Per-PR reopen events aren't in the bundle, so no `reopen_count`.)

All fields degrade to null/0 when the source data is thin (e.g. PR-only train with no issue) —
never invented.

### 3. `slice_train(bundle, train_id)` (link.py) — the narrator contract

A **self-contained, bounded** dict that is the single unit both the report ranker and a 4b
sub-agent (standard or spotlight) consume. It carries everything about one train so the consumer
never re-reads the full bundle:

```
{ train:  { id, kind, outcome, significance, tier, effort, code_areas, evidence },
  issue:  { number, title, body*, url, labels, kind } | null,
  prs:    [ { number, title, body*, state, merged, created_at, merged_at,
              reviews:[ {author, state, body*} ], comments:[ body* ]+overflow, url } ],
  commits:[ { sha, message*, author, date } ],
  feature_deltas:[ … the train's deltas … ],
  symbol_moves:  [ … moves whose endpoints are this train's artifacts … ] }
```

`*` = **bounded** text: bodies/messages/review text truncated to a tunable cap (start ~1500
chars) with a truncation marker; long comment lists keep first/last K and record an
`overflow` count. Bounding keeps the slice token-predictable regardless of a train's real size,
which is what makes a 400-line spotlight safe to dispatch. The report **summarizes** the slice;
the spotlight **expands** it — one contract, depth chosen by the consumer.

### 4. `build_forecast(bundle)` → `bundle["forecast"]` (link.py)

Forward-only forecast over `buckets.next_candidates` (the predicted-vs-landed **loop** stays in
Phase 7). Weighted signals → likelihood tiers, mirroring the significance approach so the codebase
stays consistent:

```
forecast: { next_milestone,
            candidates:[ { ref, train|null, score,
                           tier: "likely"|"possible"|"longshot",
                           signals:[ "on <next milestone>", "high-priority",
                                     "open PR #200", "active <7d" ] } ] }
```

- **score** = weighted sum of: on the next milestone (heavy), high-`priority` facet, has an open
  PR / active linked train (work in motion), recent activity (updated within window tail), and age
  (mild). Weights are tunable constants.
- **tier** from score bands (`likely`/`possible`/`longshot`); each candidate carries the `signals`
  that placed it so 4b can narrate "X is likely because …".
- `candidates` ⊆ `next_candidates`; sorted by score desc.

### 5. `train_flowcharts` (render.py) — adaptive Mermaid, one per deep train

Registered as the **map** the schema already reserves: `diagrams.train_flowcharts[id] =
"diagrams/train-<id>.mmd"`. Emitted for every **deep** train (bounds the count); also renderable
on demand for an arbitrary train to back a spotlight (`render.py --train <id>`).

**Adaptive shape** ("which layout fits best", deterministic):
- Default **mode C** — journey with area annotations: `root issue → PR(s) → outcome`, each PR
  annotated with its `code_areas`.
- Auto-degrade to **mode A** (drop area-annotation nodes, keep `issue → PR → outcome`) when
  `len(prs) > 4` **or** distinct `code_areas > 5` — past which C is unreadable. Thresholds tunable.
- Life-story shapes handled deterministically: PR-only train starts at the PR node (no issue);
  outcome node = **Shipped → `<release>`** / **Rejected** / **In flight** from `outcome`;
  reopened and multi-PR trains render as real fan-out, not a straight line.

## Report + docs wiring

- **report-template / SKILL.md:** deepen **"Decision trains"** — deep trains embed
  `diagrams.train_flowcharts[id]` + an **effort line** (e.g. "landed in 9 days · 2 reviewers ·
  3 contributors") with a placeholder for the 4b narrative; mention trains collapse to one line.
  Add **"Next-release forecast"** rendering the forecast tiers + per-candidate signals.
- **BUNDLE.md:** document `trains[].{significance,tier,effort}`, the `slice_train` contract +
  caps, `forecast`, and `diagrams.train_flowcharts` going live.

## Validation

- **Offline unit tests:**
  - `score_train_significance` — ranking order by footprint/kind/breadth; deep = top-N ∪
    ≥floor; large rejected train still deep; tiny docs train → mention.
  - `effort` — elapsed/reviewers/review_comments/participants/stalled computed from fixtures; null
    degradation when issue/merge absent.
  - `slice_train` — self-contained (no dangling refs); bodies truncated at cap with marker;
    comment overflow counted; symbol_moves filtered to the train's endpoints.
  - `build_forecast` — candidates ⊆ next_candidates; signals/tier match seeded inputs; ordering.
  - `train_flowcharts` — C vs A selected at the threshold; PR-only / rejected / in-flight /
    reopened shapes; outcome node text.
- **Live gate:** against a real target window — `significance`+`tier` on every train; `effort`
  well-formed; `slice_train(t)` self-contained + bounded for every `t`; `forecast.candidates ⊆
  next_candidates` with valid tiers/signals; `train_flowcharts` **compile under `mmdc`** and cover
  exactly the deep trains.
- **Spot-check** the bundle's top-ranked train + forecast against the real window (the established
  discipline) — does the ranking and the "likely next" read true?
- **Docs:** BUNDLE.md, SKILL.md, report-template, design spec → **rev 13 (4a shipped)**.

## Explicitly deferred to 4b

Sub-agent dispatch and all narrative prose — both the standard per-train narrative and the
long-form single-train **spotlight**. 4a ships the slice + effort + forecast scaffold +
flowcharts; 4b consumes them. The predicted-vs-landed **forecast loop** remains Phase 7.

## Backlog — forecast tuning (minor)

`FORECAST_OVERDUE_DAYS` ships at 200. Code review noted a quarter (90 days) is a more
natural domain default for a "long-open" signal. Lowering it cleanly also means decoupling
the `TestBuildForecast` fixture's default `created_at` from the threshold (so signal-isolation
tests don't start firing `overdue`) and re-pinning the ±1-day boundary tests. Deferred as a
mild (weight 1.0) tunable — revisit when the forecast is validated against a real window.

## Backlog — gather enrichment (prerequisite slice for 4b)

`gather.py` already **fetches and then discards** two signals: the raw PR review submissions
(`/pulls/{n}/reviews` at `gather.py:1762` — only `reviewers`/`review_decision` are kept) and the
PR timeline lifecycle events (`/issues/{n}/timeline` at `:1766` — only `crossref_issues` is kept).
A small dedicated gather slice — **sequenced before Phase 4b** — should persist `pr["reviews"]`
(`{author, state, submitted_at, body}`) and selected PR timeline lifecycle events
(`reopened` / `closed` / `ready_for_review` / `converted_to_draft`). No new API surface (data is
already in hand). This upgrades the `effort` block to **true `review_rounds` + `reopen_count` +
real stall detection** and gives the 4b spotlight review-round texture (changes-requested rounds,
review bodies). 4a deliberately ships on the current available-data `effort` block and does **not**
depend on this slice.
