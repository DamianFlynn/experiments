# activity-overview — Phase 11 completion: people profile + flow/blockers data layer

**Date:** 2026-06-06
**Status:** in progress.
**Why:** the requirements-traceability validation against the master design spec
(`2026-06-01-activity-overview-design.md` §People 360, §Flow 394, ledger P11) found
the **one material gap**: the people/engagement layer was only ever rendered
agent-side from `prs`/`commits`/`effort`/`blocks`. The spec wants people as
**first-class data** — a rich per-person profile, a `flow` pathology classifier, and
a `blockers` in-degree ranking. This closes that gap (decision: *build profile + flow,
gate halls* — recognition only, no shame/blame).

## Design

These are **enrich-time projections** (siblings of `forecast` / `modules`), computed
in `link.enrich` over the materialized bundle — NOT stored person-node data. So the
stored `people` projection (`{modules, areas, is_bot}`) and `validate no_drift` are
unchanged. Pure functions in `derive.py`; `enrich` calls them. Deterministic; every
output is grounded in bundle facts the report can cite. The characterization goldens
(`char_*.json`) are regenerated to lock the new deterministic output (reviewed diff).

### Slice 1 — people profile + `halls.fame`

**`annotate_people_profile(bundle)`** (pure) enriches each `bundle["people"][login]`
in place with window-derived metrics (keeps existing `modules`/`areas`/`is_bot`):
- `prs_authored`, `prs_merged`, `merge_rate` (merged/authored, `null` when 0 authored),
  `prs_reviewed`, `commits_authored`, `issues_opened` — counts over `prs`/`commits`/`issues`.
- `review_latency_days` — median, over PRs this login reviewed, of (their first
  `reviews[].submitted_at` − `pr.created_at`) in days; `null` when no timestamped review.
- `first_seen` / `last_active` — min / max ISO date across the login's activity
  timestamps (`pr.created_at`/`merged_at`, `commit.date`, `issue.created_at`,
  `reviews[].submitted_at`, comment timestamps when present).
- `examples_authored` / `docs_authored` / `symbols_authored` — count of `artifacts`
  whose lifecycle carries an `add`/authored event by this login, grouped by artifact
  `kind` (example; readme+doc; symbol).
- `authored_then_removed` — count of artifacts this login added whose current
  `status` is `removed`/`dropped`/`replaced`.
- `stale_owned` — count of areas this login owns (`code_owners`) that contain a
  **stalled train** (a train with `effort.stalled`, matched by `code_areas` prefix).
  (Open high-activity issues carry no area mapping in the bundle, so they're surfaced
  as *pile-ups* in the flow section — slice 2 — not double-counted here.)

**`build_halls(bundle)`** → `bundle["halls"] = {"fame": [...]}` — recognition only.
`fame` ranks non-bot contributors by a footprint score
(`prs_merged*2 + prs_reviewed + commits_authored`), top N, each
`{login, score, prs_merged, prs_reviewed, commits_authored, areas}`. **`halls.internal`
(shame/blame) is intentionally NOT built** — per the "recognition, not blame" stance;
documented as descoped.

### Slice 2 — `flow` classifier + `blockers` in-degree

**`build_flow(bundle)`** → `bundle["flow"]` = per-OPEN-issue classification, one entry
per non-healthy issue (omit healthy to stay lean), each
`{number, url, state, age_days, reactions, blocked_by, signals:[...]}`. Pathologies, in
precedence order:
- `blocked` — `issue["blocked_by"]` non-empty.
- `upvoted_but_ignored` — reactions ≥ `FLOW_UPVOTE_MIN` AND no recent activity
  (`updated_at` older than `FLOW_STALE_DAYS` before ref_date) AND no open linked PR.
- `traction_then_abandoned` — had real discussion (`len(comments_list) ≥ FLOW_TRACTION_MIN`)
  then went quiet (`updated_at` stale).
- `hung` — open and old (`age_days ≥ FLOW_HUNG_DAYS`), low activity, otherwise unclassified.
- (`healthy` — default; not emitted.)
Tunables are module constants. `age_days`/staleness use `meta.ref_date`. Deterministic
order (by number).

**`build_blockers(bundle)`** → `bundle["blockers"]` = issues ranked by **in-degree** over
the resolved `blocks` relation (`issue["blocking"]`): `[{number, url, blocks_count,
blocks:[numbers]}]`, sorted by `blocks_count` desc then number. Empty when no blocks.

### Report wiring (`SKILL.md` + `report-template.md`)
- **Contributors & community** now reads the deterministic `people` profile (counts,
  merge_rate, review_latency, first_seen/last_active, authored-by-kind) and
  `halls.fame` for the footprint ranking — grounded in data, not agent-counted. Bots
  still split into Automation via `is_bot`.
- **Stalled, blocked & pile-ups** now reads `bundle["flow"]` (the pathology + signals
  per issue) and `bundle["blockers"]` (in-degree ranking), in addition to the existing
  `effort.stalled` trains and `blocker_graph`. Still framed as flow signals, never blame.
- `BUNDLE.md`: move `people` (now profiled), `halls`, `flow`, `blockers` out of
  "Reserved (empty)" and document each field + the descoped `halls.internal`.

## Slices (TDD)
1. **people profile + `halls.fame`** — `annotate_people_profile` + `build_halls`, wired
   into `enrich`; unit-tested (counts, merge_rate, review_latency median, first/last,
   authored-by-kind, authored_then_removed, stale_owned, fame ranking + bot exclusion);
   Contributors report wiring; `BUNDLE.md`. Regenerate + review char goldens.
2. **`flow` + `blockers`** — `build_flow` + `build_blockers`, wired into `enrich`;
   unit-tested (each pathology + precedence, signals, in-degree ranking, empties);
   Stalled/blocked report wiring; `BUNDLE.md`. Regenerate + review char goldens.

## Testing
- Offline unit tests in `test_link.py`/`test_derive.py` over crafted bundles with real
  `author`/`reviewers`/`created_at`/`reviews[].submitted_at`/`reactions`/`blocking`.
- The golden gate (`test_characterization`) is regenerated and the diff reviewed to
  confirm only the intended deterministic additions. `validate` (store-based) is
  unaffected — these are enrich projections, not stored facts; `no_drift` stays green.

## Not in scope
- `halls.internal` (shame/blame) — intentionally omitted (recognition only).
- Storing the profile/flow/blockers as person/issue node facts — they are read-side
  projections (a re-gather is truth), consistent with `forecast`/`modules`.
