# Step 4 — output checked against GitHub and the clone

Checked 2026-11-26 with `gh pr view`, `gh issue view`, `gh pr list --search`, the REST
timeline / reviews / comments endpoints, and `git show <window-end sha>:<path>` in the
gather clone. "Report says" is what the monthly view and report state (as of that month's end).

| # | Item | What the report / view says | What GitHub / the clone says | Result |
|---|---|---|---|---|
| 1 | [PR #7280](https://github.com/Azure/bicep-registry-modules/pull/7280) → closes [#6990](https://github.com/Azure/bicep-registry-modules/issues/6990) (Aug) | shipped, merged 2026-08-18T07:37:20Z, train `train-issue-6990`; #6990 shipped, closed 2026-08-18T07:37:21Z | merged 2026-08-18T07:37:20Z into main, `closingIssuesReferences` = [6990]; #6990 CLOSED COMPLETED 2026-08-18T07:37:21Z | match |
| 2 | [PR #7341](https://github.com/Azure/bicep-registry-modules/pull/7341) → closes [#7284](https://github.com/Azure/bicep-registry-modules/issues/7284) (Sep) | shipped, closed 2026-09-11T14:31:39Z, train `train-issue-7284`; #7284 shipped | merged 2026-09-11T14:31:39Z, closes [7284]; #7284 CLOSED COMPLETED 2026-09-11T14:31:40Z | match |
| 3 | [PR #7502](https://github.com/Azure/bicep-registry-modules/pull/7502) → closes [#7489](https://github.com/Azure/bicep-registry-modules/issues/7489) (Oct) | shipped, merged 2026-10-18T12:23:11Z; #7489 shipped, closed 2026-10-09T16:50:03Z (before the PR merged) | merged 2026-10-18T12:23:11Z, closes [7489]; #7489 CLOSED COMPLETED 2026-10-09T16:50:03Z | match (the issue really was closed 9 days before the fix merged) |
| 4 | [PR #7264](https://github.com/Azure/bicep-registry-modules/pull/7264) and its train `train-issue-2422` (Aug) | train `train-issue-2422` **shipped**, "slow to land (993 days)", titled from root issue [#2422](https://github.com/Azure/bicep-registry-modules/issues/2422); members PRs #5163 (rejected), #7264 (merged), issues #2422, #3813 | #7264 merged 2026-08-11 and closes **only [#3813]**; #2422 is still **OPEN**. #2422 joined the train only because the *rejected* [PR #5163](https://github.com/Azure/bicep-registry-modules/pull/5163) listed it (closes [2422, 3813]) | **mismatch** — a shipped train is titled and dated by an issue that never shipped (FINDINGS #13) |
| 5 | Rejected [PR #4967](https://github.com/Azure/bicep-registry-modules/pull/4967) (Aug) | rejected bucket, closed unmerged 2026-08-11T11:50:54Z, train `train-issue-4965` rejected | CLOSED, not merged, 2026-08-11T11:50:54Z, closes [4965] | match |
| 6 | [PR #7306](https://github.com/Azure/bicep-registry-modules/pull/7306) — open at Aug end, since moved | Aug: in_flight, open (projected: `as_of.fields` include merged/state); Sep: shipped, closed 2026-09-01T08:27:43Z | created 2026-08-29T00:34:18Z, merged 2026-09-01T08:27:42Z | match |
| 7 | Stalled train `train-issue-6167` ([PR #7146](https://github.com/Azure/bicep-registry-modules/pull/7146)) — stalled at Aug end, since moved | Aug: stalled, idle 41 days; Sep: not stalled, idle 12, last activity 2026-09-18T13:20:10Z | PR reviews 2026-07-21T05:56:20Z (41 days before 2026-08-31), then 2026-09-05T16:13:03Z and 2026-09-18T13:20:10Z; still OPEN | match |
| 8 | Issue [#7344](https://github.com/Azure/bicep-registry-modules/issues/7344) — closed, reopened, closed across the Sep/Oct boundary | Sep: closed 2026-09-30T15:10:17Z with no reason, in no bucket (an undone close, BUNDLE.md); Oct: shipped, closed 2026-10-02T13:13:48Z | closed 2026-09-30T15:10:17Z → reopened 10-01T06:15:49Z → closed 10-02T07:58:17Z → reopened 10-02T11:28:58Z → closed 10-02T13:13:49Z COMPLETED | match |
| 9 | `jaredfholgate` counts, Sep | merged 38, authored 43, reviewed 6, commits 38 | `author:jaredfholgate merged:2026-09-01..2026-09-30 base:main` = 38; PRs created in Sep (any state) = 43 | match (merged, authored) |
| 10 | `AlexanderSehr` counts, Aug | merged 4, authored 10, reviewed 23 | merged into main in Aug = 4; PRs **created** in Aug = 4 | match for merged; "authored 10" counts every in-scope PR he wrote, including older ones still open or closed in Aug — not "opened this month" (FINDINGS #16) |
| 11 | `abhisheksharma-ops` counts, Oct | merged 7, authored 9 | merged into main in Oct = 7; created in Oct = 8 | match for merged; authored differs by definition as in #10 |
| 12 | Dependency edge `avm/res/devices/iot-hub` → consumer groups | Sep: → `avm/res/devices/iot-hub/consumergroup`; Oct: → `avm/res/devices/iot-hub/event-hub-endpoint/consumergroup` (edge diff Sep→Oct) | `git show 5c123604fa1d:avm/res/devices/iot-hub/main.bicep` line 211 `module consumerGroupsModule 'consumergroup/main.bicep'`; at `82950838e7ce`: `'event-hub-endpoint/consumergroup/main.bicep'`; changed by [574f7d7](https://github.com/Azure/bicep-registry-modules/commit/574f7d7184a2541e5eb6e7ea3c3e5b80ab19488a) (#7454) | match |
| 13 | Dependency edge `avm/res/db-for-my-sql/flexible-server` → ATP child | Sep: → `…/advanced-threat-protection`; Oct: → `…/advanced-threat-protection-setting` | line 476 at the two shas: `'advanced-threat-protection/main.bicep'` → `'advanced-threat-protection-setting/main.bicep'`, same commit 574f7d7 | match |
| 14 | Boundary: [PR #7430](https://github.com/Azure/bicep-registry-modules/pull/7430) (created 2026-09-30T17:02Z, merged 2026-10-01T04:37Z) | Sep: in_flight, open; Oct: shipped | created 2026-09-30T17:02:14Z, merged 2026-10-01T04:37:57Z | match |
| 15 | Boundary: [PR #7422](https://github.com/Azure/bicep-registry-modules/pull/7422) (created 2026-09-30T06:11Z) | Sep: in_flight; Oct: in_flight | OPEN, created 2026-09-30T06:11:19Z | match |

**Match rate: 14 of 15** (item 4 is a mismatch). Items 10 and 11 match on the
recognition measure used (merged); their "authored" column needs a definition note.

Also checked while choosing items: the PRs in each month's shipped bucket are exactly the
set GitHub returns for `merged:<month> base:main` (Aug 23, Sep 69, Oct 65; no PR missing or
extra), and the commits in each view equal `git log --first-parent origin/main` for the
month (Aug 23, Sep 69, Oct 65). The September view holds one more merged PR,
[#7291](https://github.com/Azure/bicep-registry-modules/pull/7291), merged into the
feature branch `users/alsehr/peterbud_7284`: correctly not shipped, and listed as
`contributing_prs` of `train-issue-7284` whose PR #7341 took that branch into main.

## Largest train per month

| Month | Largest train(s) | PRs | Issues |
|---|---|---|---|
| 2026-08 | `train-issue-2422` (#5163 rejected, #7264 merged); `train-issue-6990` (#7158, #7280) | 2 | 2 / 1 |
| 2026-09 | `train-issue-7295` (#7334, #7347) | 2 | 1 |
| 2026-10 | `train-issue-7342` (#7360, #7501), `train-issue-7344` (#7442, #7444), `train-issue-7503` | 2 | 2 / 1 / 6 |

No train holds more than 10 PRs. On this repository a train is effectively one PR or one
issue + its fix: the month-scale campaigns (Sep's metadata migration, Oct's CI
reliability work) are dozens of separate single-PR trains with no closing reference
between them, so the "trains, not PR lists" framing of the newsletter had to be grouped
by hand from titles (FINDINGS #15).
