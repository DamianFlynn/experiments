# Findings — independent validation of `activity-overview` on Azure/bicep-registry-modules

Run 2026-11-26 on master `0d965bc`, working only from SKILL.md, docs/USAGE.md (§1, §2, §4,
§5, §7.2), report-template.md, reference/report-sections.md and BUNDLE.md. Evidence paths
are in this folder; `logs/` means the run logs quoted in RUNLOG.md.

Tags: **defect** (the skill does something wrong), **wrong-doc** (the docs say something
untrue), **doc-gap** (the docs leave out something an operator needs), **suspicious-data**
(output that is technically produced but misleading), **usability**.

## Defects and wrong output

**1. [defect] A transient network error during gather crashes with a traceback and leaves an empty store.**
August run 1 (15:19:30 → 15:22:13Z) died in the patch walk:
`RuntimeError: command failed (exit 128): git … log --reverse … -p …` /
`LibreSSL SSL_connect: SSL_ERROR_SYSCALL in connection to github.com:443` /
`fatal: could not fetch e67b4305… from promisor remote`. The gather clone is shallow and
blobless (`remote.origin.partialclonefilter blob:none`), so `git log -p` fetches blobs from
GitHub on demand for its whole length; one dropped connection ends the run. No retry, no
clean error message. The store was left with 0 nodes (`validate`: `target` "store holds no
nodes", `RESULT: FAIL`). Re-running the same window into the same store (USAGE §5.1 "fix
cause, re-gather") worked: `RESULT: OK` after 19 m 40 s.

**7. [defect] The §5.5 anti-tamper cross-check fails on every fresh, untouched store.**
Following §5.5 exactly — `view.py --as-stored` straight after the gather, then
`validate.py <store> --bundle at-gather.json` — gives `RESULT: FAIL` on all three stores
(`no_drift`: 53 / 22 / 10 × `{"kind": "person", "problem": "stored but not derivable"}` for
Aug / Sep / Oct), while plain `validate.py` on the same stores prints `RESULT: OK`.
Cause, from the store: gather stores every PR open at gather time (§7.2), including PRs
created **after** the window; the window-scoped `--as-stored` view leaves them out, so their
authors can't be re-derived from it. Example: `Azure#person-paulyuk`'s only edge is
`authored` → `pr-7576`, created 2026-11-02T16:16:55Z (GitHub) — after September. §5.5 says
a failure on a fresh single-window store "is a producer defect … don't publish". In
practice the documented tamper check can never pass for a past month of an active repo, so
an operator following the docs either can't publish or learns to ignore the check.

**8. [defect] A month gathered late into its own store loses its "state as of" caveat.**
The template's STATE AS OF rule writes "Records are as last gathered on … titles, labels,
milestones and board status may be newer" only when `meta.state_as_of` is present. A late
gather into a fresh per-month store (the layout USAGE §2.1 recommends) carries no
`state_as_of` (Sep meta: `records_as_of {instant 2026-09-30T23:59:59Z, prs 42, issues 40}`,
nothing else), yet USAGE §5.3 says exactly those fields are today's. The gather time
isn't anywhere in the view either, so the report can't state it from the data. I wrote
the caveat by hand from the run log (each report's second line).

**13. [suspicious-data / defect] A rejected PR's closing references pull an open issue into a shipped train and give it a false 993-day "slow to land".**
`train-issue-2422` (Aug) is `shipped`, `slow_to_land`, `elapsed_days: 993`, root issue
[#2422](https://github.com/Azure/bicep-registry-modules/issues/2422). On GitHub #2422 is
still **open**. The merged PR [#7264](https://github.com/Azure/bicep-registry-modules/pull/7264)
closes only #3813; #2422 joined the train because the **rejected** PR
[#5163](https://github.com/Azure/bicep-registry-modules/pull/5163) listed both. Since the root
is the lowest issue number, the report titles a shipped train with an unshipped issue
("[Bug Report]: virtual_machine enableAcceleratedNetworking…") and dates its effort from
2023-11-21. It is the top "slow to land" item of August, so a newsletter would repeat it.
(VERIFICATION item 4 — the one mismatch.)

## Data that is produced but misleading

**11. [suspicious-data] The forecast is empty for every month, so the forecast loop never runs.**
`forecast.next_milestone: null`, `forecast.candidates: []`, `buckets.next_candidates: []` in
Aug, Sep and Oct, although `milestones` holds 8 entries (e.g. "Committed Backlog",
`v0.16`, `v0.17`). The series' `forecast_loop` is therefore 0/0/0 in Sep and Oct.
Newsletter question 7 ("how did each month's forecast compare with what shipped") can't
be answered. The docs I may read don't say what a repo needs for the forecast to produce
candidates (§6 is outside the allowed sections; nothing in §5 or the template points there
when the forecast is empty).

**14. [suspicious-data] The recognition score triple-counts authorship on squash-merge repos.**
`halls.fame` score = `prs_merged*2 + prs_reviewed + commits_authored`. On this repo every
merged PR is also one squash commit, so each authored-and-merged PR scores 3 and each review
scores 1: jaredfholgate 120 (Sep) = 38×2 + 6 + 38. Reviewers who carried the month
(jtracey93: 35 PRs reviewed in Sep) rank below. I used the hall and also gave reviewers and
mergers separately.

**15. [usability] On a monorepo of independent PRs, "decision trains" are single PRs.**
The largest train in each month holds 2 PRs (VERIFICATION). Month-long campaigns (Sep:
~38 metadata-platform PRs; Oct: ~44 CI-reliability PRs) are dozens of separate mention or
deep trains with no closing reference between them. The template then renders 94 / 140 /
157 train lines per month, and the deep tier goes mostly to mechanical sweeps (e.g.
[#7349](https://github.com/Azure/bicep-registry-modules/pull/7349) touching 226 areas). The
"trains, not PR lists" framing had to be done by hand from titles.

**16. [usability] `prs_authored` is "in-scope PRs by this person", not "PRs opened this month".**
AlexanderSehr, Aug: `prs_authored` 10, but he opened 4 PRs in August (GitHub); the rest are
older PRs of his still open or closed in the window. The template's column header
"Authored" invites the wrong reading. Merged counts match GitHub exactly (VERIFICATION
items 9–11).

**18. [usability] "New this installment" is "new to the series", not "newly opened".**
Sep `series.new` holds 135 items, including issues open for years that were merely
touched in September (e.g. [#2308](https://github.com/Azure/bicep-registry-modules/issues/2308)).
The template's gloss "first seen this window" is accurate for the series but will be read
as "new work" in a newsletter.

**21. [usability] Flow covers the whole repo's open backlog, not the month.**
`flow` classifies every open issue of the repo (150 / 149 / 160 entries; e.g.
[#1580](https://github.com/Azure/bicep-registry-modules/issues/1580), open 907 days). That
is as documented (BUNDLE "flow"), but reports present it under the month with no line
saying so; I added the explanation in the brief.

## Documentation

**2. [wrong-doc] Test-suite expected result.** USAGE §2: "ends `OK (skipped=6)`". Observed
`Ran 2474 tests in 85.660s` / `OK (skipped=17)` with mmdc installed and FTS5 available.

**3. [usability] The suite's last lines aren't its result.** The brief's own check
(`… 2>&1 | tail -2`) prints `  ]` and `}`: a test writes JSON to stdout, which lands after
unittest's stderr summary. The result has to be grepped for (`^OK|^FAILED|^Ran`).

**4. [wrong-doc] Token scope disagrees between SKILL.md and USAGE.** SKILL.md step 1:
"requires `GITHUB_TOKEN` with `repo` scope"; USAGE §1.1: `public_repo` for public targets.

**5. [doc-gap] Nothing says the gather needs the network for its whole run.** The bounded
clone is blobless; the git patch walk downloads file contents as it goes (finding 1). §1,
§4, §5 and §7.2 describe the clone as "bounded" only.

**6. [usability] `view.py` on an empty store says "error: empty store: no project to validate".**
view.py does not validate; the message is copied from the validator.

**9. [doc-gap] The mmdc preflight passes while rendering fails.** §1 checks `mmdc --version`
(11.15.0 here). `render.py` then fails on the first diagram: `Error: Could not find Chrome
(ver. 148.0.7778.97)` (puppeteer's headless Chrome isn't installed). The documented
fallback (`--skip-validate`, say so in the report) worked; I installed nothing. A
render-one-diagram preflight would catch it.

**10. [doc-gap] Roll-ups with one store per month.** §2.1 recommends one store per installment;
§5.3 then says a quarter needs either "the whole quarter window into its own store … and
digest that" or an accumulated store. With the recommended layout, a roll-up therefore means
a **fourth full gather** (here 2026-08-01..10-31; see RUNLOG for its time). "digest that"
names `digest.py`, the multi-repo tool; for a single repo the equivalent is `view.py` over
the wider window — I used view.py. §5.3 also doesn't say whether a roll-up report should use
the same template or how to relate it to the monthly series (I kept it out of `series.json`,
as §5.2 requires, and linked it without a series).

**12. [doc-gap] No first-time-contributor fact.** Nothing in the bundle identifies
newcomers: `people.first_seen` is the earliest activity *in the view*; `author_association`
is GitHub's value at gather time, and BUNDLE.md's "Not projected" list doesn't mention it,
so a reader may think it is as of the window's end. Newsletter question 4 is answered only
as "new to this period's data", low confidence.

**17. [wrong-doc] §7.2 module count.** "a monorepo with 500+ resource modules … and ~50
pattern modules". At October's last commit there are 224 `avm/{res,ptn,utl}/<group>/<module>/main.bicep`
roots (`edge_extraction.resolved` 224 in Sep; PR #7349's narrative counts 222 root
modules). The 500+ figure is probably modules including children (#7349: 572 metadata files).

**19. [usability] The template has no length control except for diagrams.** Filled
faithfully, the monthly reports run to ~900–1,600 lines (Feature changes: 2,489 / 2,063 /
745 deltas; Content lifecycle: thousands of artifacts; 94–157 trains; "Shipped by code area"
repeats a 226-area PR under every area). I capped long lists at 40 rows with a "Showing N of M"
line and grouped trains touching more than 10 areas under "Cross-cutting".

**20. [usability] One narrator sub-agent per deep train is heavy at this scale.** 8 / 18 / 8
deep trains. I ran one narrator per month that took each slice separately under the
verbatim contract; every evidence URL was then checked against its slice (162 of 162 kept).
The narrator contract worked well.

## Worked well, and surprising enough to note

**22.** The as-of projection held exactly on every boundary case checked: PR #7306 (open at
Aug end, merged 2026-09-01T08:27Z), PR #7430 (opened 2026-09-30T17:02Z, merged
2026-10-01T04:37Z), issue #7344 (closed on 09-30, reopened, closed twice more on 10-02 —
September shows it closed with no reason and in no bucket, as BUNDLE.md says), and
train-issue-6167's idle days (41 at Aug end, last activity 2026-09-18T13:20:10Z in Sep, both
equal to GitHub's review timestamps).

**23.** The shipped sets are exact: each month's shipped PRs equal GitHub's
`merged:<month> base:main` (23 / 69 / 65), commits equal `git log --first-parent`, and the PR
merged into a feature branch (#7291) is correctly not shipped and appears as
`contributing_prs` of the train that took the branch to main.

**24.** `--structural-iac` caught real child-module renames between months (iot-hub consumer
groups, MySQL advanced-threat-protection, dev-test-lab secrets dropped) and the store's
feature deltas pin all of them to one commit, `574f7d7` (#7454) — a "README generation" PR
that changed module structure, which a title-only review would miss.

## Count by tag

| Tag | Findings |
|---|---|
| defect | 1, 7, 8, 13 (13 also suspicious-data) |
| suspicious-data | 11, 13, 14 |
| wrong-doc | 2, 4, 17 |
| doc-gap | 5, 9, 10, 12 |
| usability | 3, 6, 15, 16, 18, 19, 20, 21 |
| worked well | 22, 23, 24 |

Totals (each finding counted once, by its first tag): defect 4, suspicious-data 2,
wrong-doc 3, doc-gap 4, usability 8 — 21 findings, plus 3 "worked well" notes.
