# Scheduled gathering

> **Audience:** the operator who wants the data gathered every night on an always-on
> machine, so that writing a newsletter or digest only **reads** stores that are already
> there. Tool: [`../schedule.py`](../schedule.py). Example inputs:
> [`../examples/streams.json`](../examples/streams.json),
> [`../examples/launchd/`](../examples/launchd/), [`../examples/cron/`](../examples/cron/).

## 1. Two concerns, kept apart

| Concern | Who runs it | What it does | Network |
|---|---|---|---|
| **Gathering** | a nightly job: `schedule.py run` | for every followed stream, folds the current month into `<stream>/stores/YYYY-MM.db` with the existing `gather.py` CLI, audits it with `validate.py`, swaps it in only when both pass, writes `status.json` | only the gather it starts |
| **Reading / writing** | the skill, when you ask for a newsletter or digest | asks `schedule.py latest` which store holds the period and whether it is fresh, then `view.py` → `link.py` → `newsletter.py --outline` → write → checks | none |

The runner has no model and makes no network call of its own: it builds the same gather
command you would type (USAGE §4), runs it as a subprocess, and runs `validate.py` on the
result. A scheduled store is therefore exactly the store a manual gather of the same
window would give. When there is no scheduled workspace, the skill falls back to gathering
on demand (SKILL.md step 1).

## 2. The workspace on disk

Everything the runner writes lives under the **workspace** named in the streams file. The
skill directory is only read (it can be read-only); the workspace can be anywhere.

```
~/activity-data/                    the workspace (local disk, NOT a synced folder: §8)
  streams.json                      the streams file (§3); may live elsewhere
  people.json                       the ONE people registry every stream shares (USAGE 2.1, 5.7)
  people-activity.json              generated beside it by gather
  status.json                       roll-up of the last run (§6)
  .schedule.lock                    present only while a run is going (§5)
  logs/run-20261127T024000Z.log     one log per run (kept retention.log_days)
  logs/launchd.out.log  launchd.err.log   (macOS: what the wrapper itself prints)
  bin/activity-gather.sh            the wrapper the schedule calls (§9)
  .tfcache/                         shared TF_PLUGIN_CACHE_DIR (if the streams file sets it)
  avm-bicep/
    status.json                     this stream's per-month results (§6)
    stores/2026-11.db               the month store the skill reads
    stores/2026-11.db.prev          the store before the last successful run (one generation)
    clone/                          gather's reused clone (manifest streams: clone/<owner>/<repo>)
  avm-tf/
    manifest.json                   your manifest (members; its window is ignored)
    run/manifest.2026-11-01_2026-11-30.json   the manifest with the window filled in
    stores/  clone/  status.json
```

Reports and views are not part of the schedule: write them wherever you like (the notes
vault is fine for finished Markdown).

## 3. The streams file

JSON, like a manifest. Relative paths: `workspace` is relative to the streams file's own
directory; every other path is relative to the workspace. `~` is expanded.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | `1` | `1` | schema version |
| `workspace` | path | **required** | the data directory (§2) |
| `people` | path | `people.json` | the shared people registry, passed to every gather as `--people` |
| `regather_previous_month_days` | int ≥ 0 | `7` | during the first N days of a month the previous month is gathered again, so a closed month ends in its final state (0: never) |
| `stale_after_hours` | int ≥ 1 | `36` | `latest` refuses an open month whose last success is older |
| `lock_stale_hours` | int ≥ 1 | `12` | a lock older than this is reported as stale (never silently broken) |
| `retention.log_days` | int ≥ 1 | `60` | run logs older than this are deleted |
| `retention.store_months` | int ≥ 1 or `null` | `null` | keep only the newest N month stores (`null`: keep all) |
| `env` | object | `{}` | env for every gather: `ACTIVITY_*` knobs (REFERENCE.md) and `TF_PLUGIN_CACHE_DIR` (made workspace-relative, created); never a credential |
| `streams[]` | list | **required** | one entry per newsletter stream |

Per stream:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | `[a-z0-9][a-z0-9_-]*` | **required** | names the stream's directory |
| `title` | string | the id | for status and the reader |
| `kind` | `repo` / `manifest` | **required** | one repository, or a multi-repo manifest |
| `owner`, `repo` | strings | (kind `repo`) | the repository |
| `manifest` | path | (kind `manifest`) | the manifest (its `window` and `people` are replaced per run) |
| `home.orgs` / `home.names` / `home.email_domains` | lists | `[]` | `--home-org` / `--home-name` / `--home-email-domain` |
| `cadence` | `["weekly"]`, `["monthly"]` or both | `["monthly"]` | a hint for the writer; `weekly` turns on `carry_previous_days` |
| `carry_previous_days` | int 0–27 | `6` when weekly, else `0` | also fold the last N days of the previous month into each month's store (§4) |
| `gather_flags` | list | `[]` | extra gather flags: `--structural-iac`, `--no-iac-build`, `--no-pr-history`, `--no-pr-checks`, `--no-project-board`, `--no-newcomers`, `--no-affiliation`, `--no-workflows`, `--no-releases`, `--people-offline`, `--no-clone`, or `--branches=X`, `--people-refresh-days=N`, `--affiliation=LOGIN=VALUE`. Target, window, store, clone dir and registry are the runner's and are refused |
| `env` | object | `{}` | per-stream env overrides (caps), same rules as the global `env` |
| `stale_after_hours` | int | global | per-stream staleness threshold |
| `enabled` | bool | `true` | a disabled stream is skipped unless named with `--stream` |

[`examples/streams.json`](../examples/streams.json) follows the owner's streams: AVM Bicep
(`Azure/bicep-registry-modules`, weekly + monthly, `--structural-iac`), AVM Terraform (a
manifest), `Azure/bicep`, the AI hub gateway landing zone
(`Azure-Samples/ai-hub-gateway-solution-accelerator`), and an enterprise-scale landing zone
(disabled until its repository is confirmed). `python3 schedule.py check --streams FILE`
prints how the file resolves.

## 4. Which windows a run gathers

"Today" is the **UTC** date of the run.

- **Every gather is a whole calendar month**, `--from YYYY-MM-01 --to YYYY-MM-<last>`, into
  `stores/YYYY-MM.db`. While the month is open the gather also passes `--ref-date <today>`:
  records created after today cannot exist yet, so the store holds the month **to date**,
  with forecast / flow framed as of today. Why not `--to <today>`? Each night would add a
  new window to the store (a new per-window fact set every night, and a month view that is
  not exactly any gathered window); with the whole-month window every night re-folds the
  same window, replacing its per-window facts, and the store always holds exactly what a
  manual monthly gather holds.
- **Closing a month:** during the first `regather_previous_month_days` days (default 7) the
  previous month is gathered again (without `--ref-date`), so its store ends with the
  records as they stood a week after it closed, gathered after its end. `latest` calls such
  a month **final**.
- **Carried days** (`carry_previous_days`, default 6 for a weekly stream): before the month
  window, the last N days of the previous month are folded into the month's store as a
  second, older window (folded first: USAGE 2.1 "fold months oldest first"). A 7-day week
  that crosses the month boundary is then one `view.py` over one store. They are gathered
  when the store does not hold them yet and refreshed during the re-gather days; after
  that they stay as they are.
- **Backfill:** `--backfill 2026-08..2026-10` gathers those whole months (oldest first)
  instead of today's windows; a month after the current one is refused.
- `--stream ID` (repeatable) runs only those streams; `--dry-run` prints the plan and the
  exact gather / validate commands and changes nothing; `--today YYYY-MM-DD` plans as of
  another date.

## 5. What a run does, and what happens on failure

For each enabled stream, one after another (streams are independent: one failing never
stops the next), for each planned window:

1. **Copy** the month store to `stores/.YYYY-MM.db.next` with SQLite's online backup (a
   consistent copy even if the store was last closed uncleanly). No store yet: start empty.
2. **Fold**: run gather on the copy (the carried days first, then the month). The fold is
   incremental and idempotent, so folding into a copy of last night's store is the same
   as folding into the store itself.
3. **Validate** the copy: `validate.py <copy> --project … [--repo …]`, which must exit 0
   (`RESULT: OK`). It runs without the token in its environment.
4. **Swap**: hard-link the current store to `YYYY-MM.db.prev` (replacing the older one),
   then rename the copy over `YYYY-MM.db` (an atomic rename on the same disk: a reader
   always finds a complete store). The store is not swapped while someone holds it open
   (its `-wal` / `-shm` files exist): that attempt is `swap_failed` and retried next night.

A gather that exits non-zero, a validate that is not OK, or any error deletes the copy and
**leaves the store as it was**. A manifest run in which one member fails exits 1 and so is
not swapped (validate's `member_status` check would fail it anyway): the whole stream keeps
yesterday's store until the member gathers again or is removed from the manifest. The
people registry is the exception: gather updates it as it goes, also on a run that is then
discarded (its updates are lookups, idempotent, and the next run repeats them).

`.prev` is kept while the month is still gathered (the current month, and the previous
one during its re-gather days); after that it is deleted, as the copy would only double the
month's size. To roll back by hand: `mv stores/2026-11.db.prev stores/2026-11.db`.

**The lock.** `<workspace>/.schedule.lock` (pid, host, start time) is created exclusively
at the start and removed at the end. A second run while it exists stops with exit 3. A lock
older than `lock_stale_hours` (default 12) is reported as **STALE** with its pid, host and
age, and still not removed: check that no run is active, then remove it or pass
`--break-stale-lock`.

**The time budget.** `--max-minutes N` starts no new window after N minutes; the windows not
started are recorded `skipped_time_budget` (in each stream's status and the roll-up's
`last_run.skipped_time_budget`) and the run exits 1. Streams run one at a time, so they
share the token's API budget instead of competing for it.

Exit codes of `run`: 0 every window ok, 1 a window failed or was skipped, 2 usage or
streams-file error (or no token), 3 locked.

## 6. Status files

`<stream>/status.json` keeps, per month, the last attempt and the last success (a failed
attempt never erases the last success). This one is from the end-to-end test (a tiny local
repository, the real gather and validate CLIs, the API stubbed empty; trimmed):

```json
{
  "schema": 1, "stream": "demo", "title": "demo", "kind": "repo", "target": "o/r",
  "cadence": ["weekly", "monthly"], "carry_previous_days": 6,
  "updated_at": "2026-11-27T12:21:12Z",
  "last_run": {"run_id": "20261127T122112Z", "result": "ok",
               "started_at": "2026-11-27T12:21:12Z", "finished_at": "2026-11-27T12:21:12Z",
               "log": "logs/run-20261127T122112Z.log"},
  "windows": {
    "2026-11": {
      "month": "2026-11", "from": "2026-11-01", "to": "2026-11-30",
      "store": "demo/stores/2026-11.db", "store_prev": "demo/stores/2026-11.db.prev",
      "carried": {"from": "2026-10-26", "to": "2026-10-31", "at": "2026-11-27T12:21:11Z"},
      "last_attempt": { "...": "the same object as last_success here" },
      "last_success": {
        "at": "2026-11-27T12:21:12Z", "finished_at": "2026-11-27T12:21:12Z",
        "result": "ok", "reason": "current", "run_id": "20261127T122112Z",
        "from": "2026-11-01", "to": "2026-11-30", "ref_date": "2026-11-20",
        "duration_s": 0.3,
        "gathers": [{"part": "month", "from": "2026-11-01", "to": "2026-11-30",
                     "ref_date": "2026-11-20", "exit": 0, "duration_s": 0.1}],
        "validate": {"exit": 0, "result": "OK"},
        "counts": {"prs": 0, "issues": 0, "commits": 3, "people": 1, "windows": 2,
                   "bytes": 98304},
        "notes": {"lines": ["note: o/r: base branch 'main' (the repo default branch, from the clone)",
                            "closing issues: GraphQL unavailable (no GraphQL client), using closing keywords only"],
                  "lines_omitted": 0, "warnings": 0, "rate_limit_waits": 0,
                  "api_points": null, "not_checked": 0, "capped": 0, "member_failures": []},
        "code_version": {"sha": "0451f3dd9822514498fcdaed23c558a237d2be2a", "dirty": true},
        "error": null
      }
    }
  }
}
```

- `result`: `ok`, `gather_failed`, `validate_failed`, `swap_failed`, `config_error`
  (e.g. the manifest is missing), `error` (anything else, with the message), or
  `skipped_time_budget`.
- `counts` are store totals after the fold (all windows in it), `bytes` its size.
- `notes` is what gather said about itself: its `warning:` / `note:` lines and summaries
  (capped at 60), rate-limit waits, the GraphQL points it reported (`api_points`; gather
  reports them for the check-results pass only, so this is a lower bound, `null` when it
  reported none), people not checked for affiliation, PRs whose checks were capped, failed
  manifest members.
- `code_version` is the git commit of the skill directory that ran (`dirty`: it had local
  changes), or `null` outside a git checkout.

`<workspace>/status.json` is the roll-up: `last_run` (`run_id`, `today`, `result`,
`exit_code`, `log`, `max_minutes`, `skipped_time_budget`, `backfill`), `code_version`,
`workspace_warnings`, and per stream `{result, windows: {month: result}, last_success_at,
status}`. `python3 schedule.py status --streams FILE` prints the last three months of each
stream; `--json` prints the roll-up.

## 7. The token

gather reads **`GITHUB_TOKEN`**, else **`GH_TOKEN`**, from its environment (USAGE §1.1 for
the scopes). The runner passes its own environment through to gather and nothing else: it
never writes a token to the streams file, a log, a status file or a manifest, strips it from
validate's environment, and replaces any token value (and anything shaped like a GitHub
token) in captured output with `[REDACTED]`. The streams file refuses `env` names that
look like credentials. Without a token `run` stops before doing anything (exit 2).

- **macOS:** keep it in the login keychain; the wrapper reads it for the one process:
  ```bash
  security add-generic-password -a "$USER" -s activity-gh-token -T /usr/bin/security -U -w
  # (paste when prompted; -U updates an existing item; -T lets `security` read it without a dialog)
  ```
  A LaunchAgent runs in your login session, so the login keychain is available once you
  have logged in after a reboot (enable automatic login, or log in once, on a headless Mac
  mini).
- **Plain env file** (Linux, or macOS without the keychain): `~/.config/activity-overview/token.env`
  holding `GITHUB_TOKEN=…`, mode **600**, outside the workspace, any synced folder and any
  git checkout. [`examples/cron/activity-gather.sh`](../examples/cron/activity-gather.sh)
  refuses a file that is not 600.
- **Lifetime.** The enterprise that owns `Azure/*` refuses personal tokens that live longer
  than 8 days (USAGE §1.1). A nightly job with such a token must have it replaced every week
  or so: when it expires, every run fails with gather's 403 message in the log and
  `gather_failed` in status, and `latest` refuses stale months. See §12 for CI, whose
  built-in token is not subject to that cap.

## 8. Where the workspace lives

Keep the workspace on a **local, non-synced disk** (e.g. `~/activity-data`). SQLite stores
under iCloud Drive, Dropbox, OneDrive or a synced notes vault can be uploaded mid-write
(a torn copy on the other machines) or forked into conflict copies. The runner warns, and
carries on, when the workspace path contains `Mobile Documents`, `iCloud`, `Dropbox` or
`OneDrive`, or an ancestor folder holds `.obsidian`; the warning is also in
`status.json` `workspace_warnings`. Write only the finished Markdown into the vault.

## 9. First-time setup on the Mac mini, step by step

Prerequisites: Python **3.11+** (the skill's minimum; `python3 --version`; Homebrew's
`python` or a python.org build — the Xcode command line tools' `python3` may be older),
`git` (Xcode command line tools: `xcode-select --install`, or Homebrew), the skill installed
(USAGE §2; `$AO` below is the skill directory). Full Disk Access is **not** needed for a
workspace in your home directory (it is for `~/Desktop`, `~/Documents`, `~/Downloads`).

```bash
# 1 · the workspace and the streams file
W=~/activity-data
mkdir -p $W/bin $W/logs
cp $AO/examples/streams.json $W/streams.json
#   edit $W/streams.json: "workspace": "." (the file sits in the workspace), the streams you follow
#   AVM Terraform: write its manifest ($W/avm-tf/manifest.json, e.g. with manifest_from_index.py)
python3 $AO/schedule.py check --streams $W/streams.json

# 2 · the token (§7)
security add-generic-password -a "$USER" -s activity-gh-token -T /usr/bin/security -U -w

# 3 · the wrapper
sed -e "s#__SKILL_DIR__#$AO#" -e "s#__WORKSPACE__#$W#" \
  $AO/examples/launchd/activity-gather.sh > $W/bin/activity-gather.sh
chmod 700 $W/bin/activity-gather.sh
$W/bin/activity-gather.sh --dry-run             # the plan and the exact commands

# 4 · backfill the last three closed months (and the current month to date), one stream
#     at a time if you want to watch it; a busy monorepo month takes minutes, a manifest longer
$W/bin/activity-gather.sh --backfill 2026-08..2026-11

# 5 · install the schedule (§10) and check the first result the next morning
python3 $AO/schedule.py status --streams $W/streams.json
```

## 10. Install on macOS (launchd)

```bash
W=~/activity-data
sed -e "s#__WORKSPACE__#$W#g" $AO/examples/launchd/local.activity-overview.gather.plist \
  > ~/Library/LaunchAgents/local.activity-overview.gather.plist
plutil -lint ~/Library/LaunchAgents/local.activity-overview.gather.plist

# install (load)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.activity-overview.gather.plist
launchctl print gui/$(id -u)/local.activity-overview.gather | head -30    # loaded? last exit code

# run now (does not wait for the schedule)
launchctl kickstart gui/$(id -u)/local.activity-overview.gather

# see the last result
python3 $AO/schedule.py status --streams $W/streams.json
tail -50 $W/logs/launchd.err.log; ls -t $W/logs/run-*.log | head -1 | xargs tail -50

# change the time: edit the plist, then bootout + bootstrap again
# uninstall (unload)
launchctl bootout gui/$(id -u)/local.activity-overview.gather
rm ~/Library/LaunchAgents/local.activity-overview.gather.plist
```

macOS specifics:

- **The Mac must be awake.** A `StartCalendarInterval` job missed while the Mac slept runs
  at the next wake (one run, not one per missed night); a Mac that is shut down runs
  nothing. On a Mac mini either set *Energy → Prevent automatic sleeping* or schedule a
  wake a few minutes before the job: `sudo pmset repeat wakeorpoweron MTWRFSU 02:35:00`
  (`pmset -g sched` shows it). Power Nap does not run user LaunchAgents: do not rely on it.
- The job runs as you, in your login session: after a reboot it starts once you log in
  (automatic login on a dedicated Mac mini).
- `ProcessType Background`, `LowPriorityIO` and `nice 10` keep it out of the way; the plist
  sets `RunAtLoad` false, so loading it does not start a run.
- launchd gives jobs a minimal `PATH`: the wrapper adds Homebrew's paths and checks for
  `python3` (3.11+) and `git` before it starts.

## 11. Linux (cron)

Copy [`examples/cron/activity-gather.sh`](../examples/cron/activity-gather.sh) to
`$W/bin/` (fill `__SKILL_DIR__` / `__WORKSPACE__` as above), create the mode-600 env file
(§7), and add the line from [`examples/cron/crontab.txt`](../examples/cron/crontab.txt)
with `crontab -e` (absolute paths). The runner is the same file on both systems; cron's
time is local, the plan is by UTC date: keep the run well clear of midnight UTC.

## 12. In a CI schedule

The same `schedule.py run` works in a scheduled CI job, with two differences: the
workspace must outlive the job (restore it from, and save it back to, durable storage that
is not the repository's git history — an artifact store, a cache keyed by stream and month,
or a mounted volume — because stores are tens of MB a month and hold contributors' activity),
and the token is the job's built-in token (no personal-token lifetime cap; public
repositories only). Run one job at a time per workspace (the lock only protects one
machine's disk). This task changes no workflow.

## 13. The reader: what the skill does now

```bash
python3 $AO/schedule.py latest --streams $W/streams.json --stream avm-bicep --last-days 7
python3 $AO/schedule.py latest --streams $W/streams.json --stream avm-bicep --month 2026-11
python3 $AO/schedule.py latest --streams $W/streams.json --stream avm-bicep --from 2026-11-24 --to 2026-11-30
```

It prints JSON: the `period`, the `stores` to read with their `last_success_at`,
`age_hours`, `final` (gathered after the month closed), `validate` result and last attempt,
and a `read` list with the `view.py` command for each (`--out VIEW.json` is yours to
replace). Exit 0 when the data may be read; **exit 1 with `refusal`** when a store is
missing, its last run failed, it did not validate OK, or an open month's last success is
older than `stale_after_hours` (`--max-age-hours H` changes the threshold for one call;
`--accept-stale` answers anyway and lists the problems under `overridden`, for a report
that then says how old its data is).

**A week across two months** (e.g. 2026-11-28..2026-12-04): the December store also holds
the carried days 2026-11-25..2026-11-30 (§4), so `latest` answers with ONE store and one
`view.py` over the whole week (an accumulated store, USAGE 2.1: views are window-scoped;
a view straddling two months carries no CI statistics, as for any manual store). A period
no single store holds (longer than the carried days, or a stream without them) comes back
as one `read` entry per month with `split_note`: read each month on its own, or gather
the whole period into its own store (USAGE §5.3 *Roll-ups*). Nothing here merges stores.

The skill's flow (SKILL.md): **scheduled job gathers → `schedule.py latest` → `view.py`
→ `link.py --people` → `newsletter.py --outline` → write → `--check-story` /
`--check-newsletter`**. Without a scheduled workspace (no streams file, or `latest`
refuses and you choose not to override), it gathers on demand as before.

## 14. When a run fails

1. `python3 $AO/schedule.py status --streams $W/streams.json` names the stream, month,
   step and error; the run log (`last_run.log`) has gather's and validate's full output.
2. By step: `gather_failed` — read gather's message in the log (an expired token is a 403
   naming the lifetime policy: replace it, §7; a rate limit is waited out by gather itself);
   `validate_failed` — USAGE §5.5 reads the report; `swap_failed` — something held the
   store open during the run (close it; the next run retries); `config_error` — fix the
   streams file or manifest (`schedule.py check`).
3. Re-run just that stream now: `$W/bin/activity-gather.sh --stream avm-bicep` (or
   `launchctl kickstart …`). The store you had is untouched until a run passes.
4. A month that stayed broken past its re-gather days can be gathered again any time:
   `--backfill 2026-11 --stream avm-bicep`.

## 15. Adding a stream

Add an entry to `streams[]` (for a manifest stream, write the manifest first), run
`schedule.py check`, `--dry-run --stream ID`, then backfill its history with
`--backfill YYYY-MM..YYYY-MM --stream ID`. From the next night it is gathered with the
others. To pause one, set `"enabled": false` (its stores and status stay).

## 16. Disk growth

Measured: a month store of `Azure/bicep-registry-modules` is about **14–16 MB**; the people
registry is about **100 KB** plus about **30 KB per month** for `people-activity.json`.
Per stream and month that means the month store, plus while the month is gathered a `.prev`
of the same size and the `.next` copy during a run, plus for a weekly stream the carried
days (about a fifth of a month's records and one more set of per-window facts). The clone
grows once and is reused (that repository's `.git` was 13.5 MB, 23.1 MB after the first
PR-history fetch; USAGE §5.6). For the monorepo: about 15 MB × 12 ≈ **180–200 MB a year**,
plus ~30–45 MB of `.prev` / `.next` working space; the other streams are smaller
repositories. `counts.bytes` in each status entry gives the real figure per store; set
`retention.store_months` to cap it.

## 17. Uninstall

```bash
launchctl bootout gui/$(id -u)/local.activity-overview.gather          # macOS
rm ~/Library/LaunchAgents/local.activity-overview.gather.plist
crontab -e                                                             # Linux: delete the line
security delete-generic-password -a "$USER" -s activity-gh-token       # the stored token
sudo pmset repeat cancel                                               # if you scheduled a wake
# the data: the workspace is self-contained; keep it (stores stay readable) or delete it
rm -r ~/activity-data
```
