# Newsletter brief — Azure/bicep-registry-modules, August–October 2026

Source material for a monthly newsletter, built only from the `activity-overview` store and
bundle views in this folder (`views/2026-0{8,9,10}/bundle.json`, roll-up
`views/2026-08..10/bundle.json`) and the reports in `reports/`. Every item links to its
GitHub source. All three months were **gathered late, on 2026-11-26**: open/merged state,
comments and reviews are as of each month's end, but titles, labels and milestones are as
of 2026-11-26 (USAGE §5.3).

PR links: `https://github.com/Azure/bicep-registry-modules/pull/<n>`; issue links:
`…/issues/<n>`.

---

## 1. What happened — the shipped work per month

The skill's own "trains" do not group this work: on this repository every train holds one or
two PRs (VERIFICATION.md, "Largest train per month"), so the themes below are **groupings I
made from PR titles and the deep-train narratives**. Each PR is cited; the grouping is
judgment.

**August** — a quiet month: 23 PRs merged into `main`, 13 issues completed
([report](2026-08.md)).
- Child modules stop colliding on deployment names: [#7249](https://github.com/Azure/bicep-registry-modules/pull/7249) (closes [#7194](https://github.com/Azure/bicep-registry-modules/issues/7194)) and [#7250](https://github.com/Azure/bicep-registry-modules/pull/7250) (seven modules).
- Module features: Cognitive Services account `deployments` child module ([#7235](https://github.com/Azure/bicep-registry-modules/pull/7235)) and `modelProviderData` ([#7254](https://github.com/Azure/bicep-registry-modules/pull/7254), closes [#7197](https://github.com/Azure/bicep-registry-modules/issues/7197)); SQL Dynamic Data Masking ([#7252](https://github.com/Azure/bicep-registry-modules/pull/7252), [#7262](https://github.com/Azure/bicep-registry-modules/pull/7262)); VNet IPAM pool prefix allocations ([#7271](https://github.com/Azure/bicep-registry-modules/pull/7271), closes [#6695](https://github.com/Azure/bicep-registry-modules/issues/6695)); ACR network-rule bypass for ACR Tasks ([#7280](https://github.com/Azure/bicep-registry-modules/pull/7280), closes [#6990](https://github.com/Azure/bicep-registry-modules/issues/6990)); Cosmos DB SecuredByPerimeter ([#7257](https://github.com/Azure/bicep-registry-modules/pull/7257)).
- Five AI solution-accelerator pattern-module upgrades: [#7301](https://github.com/Azure/bicep-registry-modules/pull/7301), [#7302](https://github.com/Azure/bicep-registry-modules/pull/7302), [#7303](https://github.com/Azure/bicep-registry-modules/pull/7303), [#7305](https://github.com/Azure/bicep-registry-modules/pull/7305), [#7307](https://github.com/Azure/bicep-registry-modules/pull/7307).

**September** — 69 PRs into `main`, 14 issues completed ([report](2026-09.md)).
- **Metadata-driven module platform** (the month's main event, nearly all by one contributor): per-module `metadata.json` backfilled across the repo with a release guard ([#7349](https://github.com/Azure/bicep-registry-modules/pull/7349) — the narrative counts 572 files, 222 root modules + 350 children), display-name and description drift fixed ([#7371](https://github.com/Azure/bicep-registry-modules/pull/7371), [#7373](https://github.com/Azure/bicep-registry-modules/pull/7373)), metadata made mandatory ([#7379](https://github.com/Azure/bicep-registry-modules/pull/7379)), telemetry prefixes sourced from it ([#7399](https://github.com/Azure/bicep-registry-modules/pull/7399), [#7403](https://github.com/Azure/bicep-registry-modules/pull/7403)), reviewer routing from metadata instead of generated CODEOWNERS ([#7343](https://github.com/Azure/bicep-registry-modules/pull/7343), [#7378](https://github.com/Azure/bicep-registry-modules/pull/7378), [#7383](https://github.com/Azure/bicep-registry-modules/pull/7383)), scaffolding for 49 planned modules ([#7366](https://github.com/Azure/bicep-registry-modules/pull/7366)), and the legacy `modules/` directory deprecated ([#7406](https://github.com/Azure/bicep-registry-modules/pull/7406)).
- CI and supply chain: a generic module workflow ([#7322](https://github.com/Azure/bicep-registry-modules/pull/7322)), tests spread across subscriptions ([#7336](https://github.com/Azure/bicep-registry-modules/pull/7336), [#7339](https://github.com/Azure/bicep-registry-modules/pull/7339)), Actions pinned to SHAs ([#7306](https://github.com/Azure/bicep-registry-modules/pull/7306), [#7320](https://github.com/Azure/bicep-registry-modules/pull/7320)).
- Cross-module: CMK user-assigned identity added to 17 modules in one batch ([#7279](https://github.com/Azure/bicep-registry-modules/pull/7279)).
- Azure Stack HCI / hybrid-compute API refresh: [#7384](https://github.com/Azure/bicep-registry-modules/pull/7384), [#7421](https://github.com/Azure/bicep-registry-modules/pull/7421), [#7427](https://github.com/Azure/bicep-registry-modules/pull/7427), [#7429](https://github.com/Azure/bicep-registry-modules/pull/7429) and three telemetry-version fixes.

**October** — 65 PRs into `main`, 27 issues completed ([report](2026-10.md)).
- **CI/test reliability campaign** (the largest group): retries and cleanup ([#7455](https://github.com/Azure/bicep-registry-modules/pull/7455), [#7464](https://github.com/Azure/bicep-registry-modules/pull/7464), [#7486](https://github.com/Azure/bicep-registry-modules/pull/7486), [#7517](https://github.com/Azure/bicep-registry-modules/pull/7517)), provider/feature registration before tests ([#7456](https://github.com/Azure/bicep-registry-modules/pull/7456), [#7518](https://github.com/Azure/bicep-registry-modules/pull/7518)), Azure Local (HCI) test fixtures and image builds ([#7491](https://github.com/Azure/bicep-registry-modules/pull/7491), [#7525](https://github.com/Azure/bicep-registry-modules/pull/7525), [#7544](https://github.com/Azure/bicep-registry-modules/pull/7544)), experimental registry workflows split and pinned to a verified tools release ([#7559](https://github.com/Azure/bicep-registry-modules/pull/7559), [#7563](https://github.com/Azure/bicep-registry-modules/pull/7563), [#7569](https://github.com/Azure/bicep-registry-modules/pull/7569)). Fourteen of the 27 completed issues are `[Failed pipeline]` reports closed in the month.
- README-generator fixes ([#7442](https://github.com/Azure/bicep-registry-modules/pull/7442), [#7454](https://github.com/Azure/bicep-registry-modules/pull/7454)); #7454 also moved or retired child modules (see question 6).
- Module fixes: SQL Managed Instance patch release ([#7444](https://github.com/Azure/bicep-registry-modules/pull/7444), closes [#7344](https://github.com/Azure/bicep-registry-modules/issues/7344)), Data Factory Git configuration ([#7360](https://github.com/Azure/bicep-registry-modules/pull/7360), closes [#7342](https://github.com/Azure/bicep-registry-modules/issues/7342)), IoT Hub private endpoint ([#7469](https://github.com/Azure/bicep-registry-modules/pull/7469), closes [#7459](https://github.com/Azure/bicep-registry-modules/issues/7459)), Event Hub capture ([#7499](https://github.com/Azure/bicep-registry-modules/pull/7499)), AVD host-pool `MultiplePersistent` ([#7502](https://github.com/Azure/bicep-registry-modules/pull/7502), closes [#7489](https://github.com/Azure/bicep-registry-modules/issues/7489)), Redis private DNS zone ([#7558](https://github.com/Azure/bicep-registry-modules/pull/7558)).

No GitHub releases were published in any of the three months (`releases` is empty), so
"shipped" here means merged into `main`, not published to the registry.

**Confidence: high for what merged** — each month's shipped PR set equals GitHub's
`merged:<month> base:main` exactly and the commits equal `git log` (VERIFICATION.md).
**Medium for the themes** — they are my grouping of titles; the skill offers no grouping
above one PR at this repository's shape.

## 2. What is stalled, and what was slow to land

**What the skill's facts mean.** A *stalled train* is an OPEN train with no recorded activity
(updates, comments, reviews, timeline events, commits) on any of its PRs or issues for more
than 21 days at the month's last day. *Flow* classifies every open issue of the repo, not
just this month's: `traction_then_abandoned` = 5+ comments and no update for 30 days,
`hung` = open 60+ days and no update for 30 days, both only when no open PR is linked.
*Slow to land* = a shipped train that took more than 21 days from first opened to merge.
Activity GitHub records nowhere else (a push, a label change) is not counted.

| Month end | Stalled trains | Flow: traction-then-abandoned | Flow: hung | Blockers |
|---|---|---|---|---|
| 2026-08-31 | 33 | 109 | 41 | 0 |
| 2026-09-30 | 33 | 104 | 45 | 0 |
| 2026-10-31 | **55** | 108 | 52 | 0 |

- The longest-stalled work is the same CI/tooling PRs every month: [#4863](https://github.com/Azure/bicep-registry-modules/pull/4863) "add gh issue label logic" (idle 512 → 542 → 573 days), [#3810](https://github.com/Azure/bicep-registry-modules/pull/3810) PSRule suppressions (448 → 509), and the pattern-module test-folder validation pair [#5315](https://github.com/Azure/bicep-registry-modules/pull/5315) / [#5316](https://github.com/Azure/bicep-registry-modules/pull/5316) (441 → 472 in Sep/Oct). Full lists: each report's "Stalled, blocked & pile-ups".
- The jump to 55 stalled trains in October is the main backlog signal of the period.
- Stalled work does move: `train-issue-6167` ([PR #7146](https://github.com/Azure/bicep-registry-modules/pull/7146)) was stalled 41 days at the end of August and had reviews on 2026-09-05 and 2026-09-18 (verified, item 7). None of August's 33 stalled trains had shipped by the end of October.
- **Slow to land** — Aug: [#6695](https://github.com/Azure/bicep-registry-modules/issues/6695) (163 days), [#6736](https://github.com/Azure/bicep-registry-modules/issues/6736) (154), [#6927](https://github.com/Azure/bicep-registry-modules/issues/6927) (119), [#6990](https://github.com/Azure/bicep-registry-modules/issues/6990) (108); Sep: [#7270](https://github.com/Azure/bicep-registry-modules/issues/7270) (34), the Function Apps pattern [#7299](https://github.com/Azure/bicep-registry-modules/pull/7299) (33), the CMK batch [#7279](https://github.com/Azure/bicep-registry-modules/pull/7279) (32); Oct: [#7288](https://github.com/Azure/bicep-registry-modules/pull/7288) (62), [#7345](https://github.com/Azure/bicep-registry-modules/issues/7345) (37). **Do not use** August's top entry, `train-issue-2422` "993 days": that issue is still open, and the train counts as shipped only through an unrelated rejected PR (VERIFICATION item 4, FINDINGS #13).
- Closed without merging: 19 PRs in Aug (several long-open feature PRs, e.g. [#4967](https://github.com/Azure/bicep-registry-modules/pull/4967), [#5163](https://github.com/Azure/bicep-registry-modules/pull/5163)), 11 in Sep, 18 in Oct.

**Confidence: medium.** Idle-day figures checked exactly against GitHub for one train
(item 7). The stalled count depends on what GitHub records as activity, and the flow
classification covers every open issue in the repo (150–160 each month, many years old), so
read it as "the repo's open backlog at month end", not "this month's stalls".

## 3. Who the heroes are

**Measure used:** the skill's recognition hall `halls.fame`, score
`prs_merged × 2 + prs_reviewed + commits_authored`, bots excluded. On this squash-merge
repository a merged PR also produces one commit, so authorship weighs three times a
review (FINDINGS #14). I therefore also list reviewers and mergers separately.

| Month | Top by footprint (score) | Top reviewers (PRs reviewed) | Merged the most PRs |
|---|---|---|---|
| Aug | AlexanderSehr (35), eriqua (16), PadhiAjit-Microsoft (9), krbar (9) | AlexanderSehr 23, eriqua 10, aniaroramsft 6 | AlexanderSehr 6, ReneHezser 5, aniaroramsft 5 |
| Sep | jaredfholgate (120), jtracey93 (41), AlexanderSehr (34), abhisheksharma-ops (21) | **jtracey93 35**, AlexanderSehr 22, eriqua 9 | jaredfholgate 40, AlexanderSehr 9, chirag1603 8 |
| Oct | jaredfholgate (135), AlexanderSehr (27), abhisheksharma-ops (21), jtracey93 (16) | AlexanderSehr 21, jtracey93 16, eriqua 9 | jaredfholgate 46, AlexanderSehr 7, chirag1603 7 |

- **jaredfholgate** authored 38 of September's 69 merged PRs and 44 of October's 65 — the
  metadata platform and the CI reliability campaign
  (e.g. [#7349](https://github.com/Azure/bicep-registry-modules/pull/7349), [#7525](https://github.com/Azure/bicep-registry-modules/pull/7525)). Counts verified against GitHub (item 9).
- **AlexanderSehr** is the steady reviewer across all three months (23 / 22 / 21 PRs reviewed)
  and also shipped module features ([#7280](https://github.com/Azure/bicep-registry-modules/pull/7280), [#7341](https://github.com/Azure/bicep-registry-modules/pull/7341)).
- **jtracey93** reviewed 35 PRs in September, the month's heaviest review load.
- **abhisheksharma-ops** carried the Azure Stack HCI / hybrid API refresh (7 merged PRs in each of Sep and Oct; Oct count verified, item 11).

Full tables with evidence links: each report's "Contributors & community". No shame lists:
the skill builds none, and this brief names no one in connection with stalled work.

**Confidence: high for the counts** (merged counts match GitHub for three people), **medium
for the ranking**, which is a formula choice (see the weighting note above).

## 4. Who is new

**The skill cannot answer this.** It has no first-time-contributor measure
(FINDINGS #12): `people.first_seen` is the earliest activity *inside the view*, and each
PR's `author_association` is GitHub's value **today**, not at the time. No merged PR in
the period has `FIRST_TIME_CONTRIBUTOR` (Aug: 8 merged-PR authors, all `CONTRIBUTOR`; Sep:
14 `CONTRIBUTOR`, 1 `MEMBER`; Oct: 11 `CONTRIBUTOR`, 1 `MEMBER`), and once a newcomer's PR
merges GitHub shows them as `CONTRIBUTOR` anyway.

The closest the data gets is **"merged a PR this month and had no recorded activity earlier
in this three-month period"**, which only means "new to *this period's data*":
- September: abhisheksharma-ops ([#7384](https://github.com/Azure/bicep-registry-modules/pull/7384)), arnoldna ([#7348](https://github.com/Azure/bicep-registry-modules/pull/7348)), balliere ([#7363](https://github.com/Azure/bicep-registry-modules/pull/7363)), donk-msft ([#7318](https://github.com/Azure/bicep-registry-modules/pull/7318)).
- October: NPetersenDK ([#7558](https://github.com/Azure/bicep-registry-modules/pull/7558)), Theuno ([#7499](https://github.com/Azure/bicep-registry-modules/pull/7499)), arjenhuitema ([#7471](https://github.com/Azure/bicep-registry-modules/pull/7471)), reikor0x ([#7502](https://github.com/Azure/bicep-registry-modules/pull/7502)).

**Confidence: low.** None of these is confirmed as a first-time contributor; check each
author's history on GitHub before calling anyone "new" in print.

## 5. What is new — modules and capabilities

- **New modules (September):** the pattern module `avm/ptn/app/function-app` ([#7299](https://github.com/Azure/bicep-registry-modules/pull/7299)) and the resource module `avm/res/code-signing/code-signing-account` ([#7363](https://github.com/Azure/bicep-registry-modules/pull/7363)) — both are module-root `README.md` *add* deltas in the store. September also scaffolded metadata-only folders for 49 planned modules from `module-list.md` ([#7366](https://github.com/Azure/bicep-registry-modules/pull/7366)) — placeholders, not shipped modules.
- No new module root was added in August or October by the same test.
- **Notable new capabilities:** Cognitive Services `deployments` child module and `modelProviderData` (Aug), SQL Dynamic Data Masking (Aug), VNet IPAM pool prefix allocations (Aug), ACR Tasks network bypass (Aug), CMK user-assigned identity across 17 modules (Sep), SQL Server time-based immutability for long-term backups ([#7341](https://github.com/Azure/bicep-registry-modules/pull/7341), Sep), communication-service `disableLocalAuth` / `publicNetworkAccess` ([#7318](https://github.com/Azure/bicep-registry-modules/pull/7318), Sep), AVD host-pool `MultiplePersistent` (Oct). Links in question 1.

**Confidence: medium.** "New module" is my reading of README-add deltas at a module root;
the skill has no "new module" fact. Nothing was published as a GitHub release, so I can't
say which of these reached the public registry in the window.

## 6. What changed — busiest modules, dependencies, removals and moves

- **Busiest areas** (PRs touching the area, from `modules`): Aug — `.github/workflows` (5), `avm/res/document-db/database-account` (3); Sep — `utilities/pipelines` (20), `utilities/tests` (20), `.github/workflows` (17), `avm/res/azure-stack-hci/cluster` (13); Oct — `utilities/tests` (40), `utilities/pipelines` (28), `.github/workflows` (19), `utilities/e2e-template-assets` (15). The repo's tooling, not its modules, absorbed most change in Sep and Oct. Several September PRs each touched 200+ module folders at once ([#7349](https://github.com/Azure/bicep-registry-modules/pull/7349) 226, [#7373](https://github.com/Azure/bicep-registry-modules/pull/7373) 267, [#7403](https://github.com/Azure/bicep-registry-modules/pull/7403) 269 areas).
- **Dependency changes** (direct module edges, compared month to month from each month's code graph, read at its last `main` commit): none between August and September; between September and October, all from one commit ([574f7d7](https://github.com/Azure/bicep-registry-modules/commit/574f7d7184a2541e5eb6e7ea3c3e5b80ab19488a), PR [#7454](https://github.com/Azure/bicep-registry-modules/pull/7454)):
  - `avm/res/devices/iot-hub`: consumer groups moved from `consumergroup` to `event-hub-endpoint/consumergroup` (verified, item 12);
  - `avm/res/db-for-my-sql/flexible-server`: child renamed `advanced-threat-protection` → `advanced-threat-protection-setting` (verified, item 13);
  - `avm/res/dev-test-lab/lab`: the `secrets` parameter and `lab_secrets` module were **dropped**, and `lab/secret` gained a `DEPRECATED.md`.
- **Removals / retirements:** the legacy `modules/` directory was deprecated ([#7406](https://github.com/Azure/bicep-registry-modules/pull/7406), Sep); `ORPHANED.md` markers were retired in favour of metadata ownership ([#7383](https://github.com/Azure/bicep-registry-modules/pull/7383), Sep); `dev-test-lab/lab/secret` was deprecated (Oct, above).
- **Feature-delta volume:** 2,489 (Aug), 2,063 (Sep), 745 (Oct) add/change/drop deltas; the tables are in each report.

**Confidence: high for the dependency changes** (both checked line by line in the clone),
**medium for "busiest"** (a PR count per area; one sweeping PR counts once per area it
touches).

## 7. What is next — the forecast and how it held up

**The skill produced no forecast for this repository.** In all three months
`forecast.next_milestone` is null and `forecast.candidates` is empty, because
`buckets.next_candidates` is empty (FINDINGS #11), even though the repo has 8 milestones.
The forecast loop in the series is therefore empty too: September and October each compared
0 predictions with what landed (`series.forecast_loop` landed / not_yet / dropped all 0).
So the brief cannot say how a month's forecast compared with the next month's deliveries.

What the data does show about the next step is the in-flight work at the end of October: 64
items in flight, in the [October report](2026-10.md) "In flight" section. Of the 25 items carried over
from September, October's series block shows where each went (shipped / still in
flight / rejected). Read those as current work, not predictions.

**Confidence: none for a forecast** — there isn't one. Even with candidates, §5.3 says
caught-up months should not be used to calibrate the forecast, and all three of these were
caught up.

---

### Caveats that apply to every answer

- All three months were gathered on 2026-11-26. State, comments and reviews are projected
  to each month's end; titles, labels and milestones are as of 2026-11-26.
- The diagrams in the reports are **unvalidated** (`render.py --skip-validate`: mmdc's
  headless Chrome is missing on the host).
- Trust check: each store printed `RESULT: OK`; the §5.5 cross-check against the view saved
  at gather time fails on every store, a skill defect (FINDINGS #7), not a data error.
