"""The scheduled gather runner and its reader (schedule.py).

Unit tests stub the subprocess seam (`run=`), the clocks (`now=`, `clock=`)
and the code version; nothing here touches the network. The end-to-end test
at the bottom runs the runner against a tiny local git repository through the
REAL gather and validate CLIs (subprocesses), with gather's GitHub API layer
stubbed empty by a small wrapper script, to prove the wiring.
"""
import contextlib
import datetime
import io
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graphstore  # noqa: E402
import schedule  # noqa: E402

UTC = datetime.timezone.utc
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2


def _no_network(url, token, timeout=20):
    """The runner's one API call, replaced for the whole module: no test
    reaches the network (a test that needs answers passes its own seam)."""
    return None, {}, "no network in tests"


schedule.default_http_get = _no_network


def D(text):
    return datetime.date.fromisoformat(text)


def T(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def streams_file(tmp, streams=None, **top):
    data = {"version": 1, "workspace": "ws",
            "streams": streams or [{"id": "demo", "kind": "repo", "owner": "o",
                                    "repo": "r"}]}
    data.update(top)
    path = os.path.join(tmp, "streams.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def make_store(path, windows=(), prs=0, people=0):
    """A minimal store file: the meta ledger and some nodes (enough for the
    runner's own reads; the real fold is exercised end to end below)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    for frm, to in windows:
        graphstore.record_window(conn, "o", "r", frm, to)
    for n in range(prs):
        graphstore.upsert_node(conn, "o/r#pr-{}".format(n + 1), "o", "r", "social",
                               "2026-11-02T00:00:00Z", {"n": n})
    for n in range(people):
        graphstore.upsert_node(conn, "o#person-p{}".format(n), "o", "*", "structure",
                               None, {})
    conn.commit()
    conn.close()


class FakeClock:
    def __init__(self, start=T("2026-11-20T02:00:00Z"), step=0.0):
        self.t = start
        self.mono = 0.0
        self.step = step

    def now(self):
        return self.t

    def clock(self):
        self.mono += self.step
        return self.mono


class Runner:
    """A scripted subprocess seam: gather / validate results per call, with
    the side effect a real gather has (it writes the store)."""

    def __init__(self, gather=None, validate=None):
        self.calls = []
        self.gather = gather or (lambda argv: (0, "", "folded bundle into store\n"))
        self.validate = validate or (lambda argv: (0, "RESULT: OK\n", ""))

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        if argv[1].endswith("validate.py"):
            return self.validate(argv)
        store = argv[argv.index("--store") + 1]
        code, out, err = self.gather(argv)
        if code == 0:
            frm = argv[argv.index("--from") + 1] if "--from" in argv else "2026-11-01"
            to = argv[argv.index("--to") + 1] if "--to" in argv else "2026-11-30"
            conn = graphstore.open_store(store)
            graphstore.init_schema(conn)
            graphstore.record_window(conn, "o", "r", frm, to)
            n = len(self.calls)
            graphstore.upsert_node(conn, "o/r#pr-{}".format(1000 + n), "o", "r", "social",
                                   frm + "T00:00:00Z", {"call": n})
            conn.commit()
            conn.close()
        return code, out, err

    def gathers(self):
        return [a for a, _ in self.calls if a[1].endswith("gather.py")]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-sched-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = os.path.join(self.tmp, "ws")

    def cfg(self, streams=None, **top):
        return schedule.load_streams(streams_file(self.tmp, streams, **top))

    def run_cli(self, argv, runner=None, clock=None, env=None, **seams):
        clock = clock or FakeClock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(argv, run=runner or Runner(), now=clock.now,
                                 clock=clock.clock, version_of=lambda: {
                                     "sha": "abc123", "dirty": False},
                                 base_env=env if env is not None
                                 else {"GITHUB_TOKEN": TOKEN, "PATH": "/bin"},
                                 isdir=lambda p: False, **seams)
        return code, out.getvalue(), err.getvalue()

    def status(self, stream="demo"):
        with open(os.path.join(self.ws, stream, "status.json"), encoding="utf-8") as fh:
            return json.load(fh)


# --- the streams file -----------------------------------------------------------

class TestStreamsFile(Case):
    def test_paths_are_resolved_against_the_file_and_the_workspace(self):
        cfg = self.cfg([{"id": "tf", "kind": "manifest", "manifest": "tf/m.json"}],
                       people="shared/people.json")
        self.assertEqual(cfg["workspace"], self.ws)
        self.assertEqual(cfg["people"], os.path.join(self.ws, "shared", "people.json"))
        self.assertEqual(cfg["streams"][0]["manifest"], os.path.join(self.ws, "tf", "m.json"))
        self.assertEqual(cfg["regather_days"], 7)

    def test_defaults_follow_the_cadence(self):
        cfg = self.cfg([{"id": "w", "kind": "repo", "owner": "o", "repo": "r",
                         "cadence": ["weekly", "monthly"]},
                        {"id": "m", "kind": "repo", "owner": "o", "repo": "s"}])
        self.assertEqual([s["carry_previous_days"] for s in cfg["streams"]], [6, 0])
        self.assertEqual(cfg["streams"][1]["cadence"], ["monthly"])

    def test_example_file_loads(self):
        cfg = schedule.load_streams(os.path.join(HERE, "examples", "streams.json"))
        ids = [s["id"] for s in cfg["streams"]]
        self.assertIn("avm-bicep", ids)
        self.assertIn("avm-tf", ids)
        self.assertEqual(cfg["people"], os.path.join(cfg["workspace"], "people.json"))

    def test_bad_entries_are_refused_with_the_reason(self):
        bad = [
            ([{"id": "Bad Id", "kind": "repo", "owner": "o", "repo": "r"}], "'id'"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r"},
              {"id": "a", "kind": "repo", "owner": "o", "repo": "s"}], "duplicate"),
            ([{"id": "a", "kind": "svn"}], "'kind'"),
            ([{"id": "a", "kind": "repo", "owner": "o/x", "repo": "r"}], "owner"),
            ([{"id": "a", "kind": "manifest"}], "'manifest'"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
               "gather_flags": ["--store=/x"]}], "--store"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
               "gather_flags": ["--branches"]}], "--branches"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
               "env": {"GITHUB_TOKEN": "x"}}], "GITHUB_TOKEN"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
               "env": {"ACTIVITY_TEST_TOKEN": "x"}}], "ACTIVITY_TEST_TOKEN"),
            ([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
               "cadence": ["daily"]}], "cadence"),
        ]
        for streams, needle in bad:
            with self.subTest(needle=needle):
                with self.assertRaises(schedule.ConfigError) as cm:
                    self.cfg(streams)
                self.assertIn(needle, str(cm.exception))

    def test_allowed_flags_reach_the_gather_command(self):
        cfg = self.cfg([{"id": "a", "kind": "repo", "owner": "o", "repo": "r",
                         "home": {"orgs": ["Azure"], "names": ["Microsoft"],
                                  "email_domains": ["microsoft.com"]},
                         "gather_flags": ["--structural-iac", "--branches=main"]}])
        argv = schedule.gather_argv(cfg, cfg["streams"][0], "2026-11-01", "2026-11-30",
                                    "2026-11-20", "/s.db")
        self.assertEqual(argv[2:], [
            "--owner", "o", "--repo", "r", "--from", "2026-11-01", "--to", "2026-11-30",
            "--ref-date", "2026-11-20", "--store", "/s.db",
            "--clone-dir", os.path.join(self.ws, "a", "clone"),
            "--people", os.path.join(self.ws, "people.json"),
            "--home-org", "Azure", "--home-name", "Microsoft",
            "--home-email-domain", "microsoft.com", "--structural-iac",
            "--branches", "main"])


# --- planning ---------------------------------------------------------------------

class TestPlanning(Case):
    def plan(self, today, carry=0, regather=7, backfill=None, have=()):
        cfg = self.cfg([{"id": "s", "kind": "repo", "owner": "o", "repo": "r",
                         "carry_previous_days": carry}],
                       regather_previous_month_days=regather)
        today = D(today)
        months = schedule.parse_backfill(backfill, today) if backfill else None
        return schedule.plan_stream(cfg, cfg["streams"][0], today, months,
                                    windows_of=lambda path: list(have))

    def test_mid_month_gathers_the_current_month_only(self):
        plan = self.plan("2026-11-20")
        self.assertEqual([(w["month"], w["from"], w["to"], w["ref_date"], w["reason"])
                          for w in plan],
                         [("2026-11", "2026-11-01", "2026-11-30", "2026-11-20", "current")])
        self.assertTrue(plan[0]["store"].endswith(os.path.join("s", "stores", "2026-11.db")))

    def test_first_days_also_regather_the_closed_previous_month(self):
        for day in ("2026-12-01", "2026-12-07"):
            with self.subTest(day=day):
                plan = self.plan(day)
                self.assertEqual([(w["month"], w["reason"], w["ref_date"]) for w in plan],
                                 [("2026-11", "previous", None),
                                  ("2026-12", "current", day)])
                self.assertEqual(plan[0]["to"], "2026-11-30")
        self.assertEqual([w["month"] for w in self.plan("2026-12-08")], ["2026-12"])
        self.assertEqual([w["month"] for w in self.plan("2026-12-02", regather=0)],
                         ["2026-12"])

    def test_year_boundary(self):
        plan = self.plan("2027-01-03")
        self.assertEqual([(w["month"], w["from"], w["to"]) for w in plan],
                         [("2026-12", "2026-12-01", "2026-12-31"),
                          ("2027-01", "2027-01-01", "2027-01-31")])

    def test_leap_years(self):
        plan = self.plan("2024-03-02")
        self.assertEqual(plan[0]["to"], "2024-02-29")
        self.assertEqual(self.plan("2025-03-02")[0]["to"], "2025-02-28")
        self.assertEqual(self.plan("2024-02-29")[0]["ref_date"], None)  # last day
        tail = self.plan("2024-03-05", carry=6)[1]["tail"]
        self.assertEqual(tail, {"from": "2024-02-24", "to": "2024-02-29"})

    def test_last_day_of_a_month_needs_no_ref_date(self):
        self.assertIsNone(self.plan("2026-11-30")[0]["ref_date"])

    def test_backfill_ranges_are_whole_months_oldest_first(self):
        plan = self.plan("2026-11-20", backfill="2026-08..2026-10")
        self.assertEqual([(w["month"], w["reason"], w["ref_date"]) for w in plan],
                         [("2026-08", "backfill", None), ("2026-09", "backfill", None),
                          ("2026-10", "backfill", None)])
        plan = self.plan("2026-11-20", backfill="2025-12..2026-01")
        self.assertEqual([w["month"] for w in plan], ["2025-12", "2026-01"])
        plan = self.plan("2026-11-20", backfill="2026-11")
        self.assertEqual(plan[0]["reason"], "current")
        self.assertEqual(plan[0]["ref_date"], "2026-11-20")

    def test_backfill_refuses_bad_ranges(self):
        for text in ("2026-12", "2026-10..2026-09", "2026-13", "26-10", "2026-10.."):
            with self.subTest(text=text), self.assertRaises(schedule.ConfigError):
                schedule.parse_backfill(text, D("2026-11-20"))

    def test_carried_days_are_gathered_once_then_kept(self):
        plan = self.plan("2026-11-20", carry=6)
        self.assertEqual(plan[0]["tail"], {"from": "2026-10-26", "to": "2026-10-31"})
        plan = self.plan("2026-11-20", carry=6, have=[("2026-10-26", "2026-10-31")])
        self.assertIsNone(plan[0]["tail"])
        # refreshed while the previous month is still being re-gathered
        plan = self.plan("2026-12-03", carry=6,
                         have=[("2026-11-25", "2026-11-30"), ("2026-10-26", "2026-10-31")])
        self.assertEqual([w["tail"] for w in plan],
                         [None, {"from": "2026-11-25", "to": "2026-11-30"}])


# --- dry run -------------------------------------------------------------------------

class TestDryRun(Case):
    def test_prints_the_plan_and_exact_commands_and_changes_nothing(self):
        path = streams_file(self.tmp, [{"id": "demo", "kind": "repo", "owner": "o",
                                        "repo": "r", "gather_flags": ["--structural-iac"]}])
        runner = Runner()
        code, out, _err = self.run_cli(["run", "--streams", path, "--dry-run",
                                        "--today", "2026-12-02"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(self.ws))
        self.assertIn("2026-11 [2026-11-01..2026-11-30] previous", out)
        self.assertIn("2026-12 [2026-12-01..2026-12-31] current ref-date 2026-12-02", out)
        self.assertIn("--owner o --repo r --from 2026-12-01 --to 2026-12-31 "
                      "--ref-date 2026-12-02 --store", out)
        self.assertIn("validate.py", out)
        self.assertIn("--structural-iac", out)
        self.assertNotIn(TOKEN, out)

    def test_dry_run_needs_no_token(self):
        path = streams_file(self.tmp)
        code, _out, _err = self.run_cli(["run", "--streams", path, "--dry-run"], env={})
        self.assertEqual(code, 0)


# --- the swap ---------------------------------------------------------------------------

class TestSwap(Case):
    def setUp(self):
        super().setUp()
        self.path = streams_file(self.tmp)
        self.store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        make_store(self.store, [("2026-11-01", "2026-11-30")], prs=3)
        with open(self.store, "rb") as fh:
            self.good = fh.read()

    def assertStoreUntouched(self):
        with open(self.store, "rb") as fh:
            self.assertEqual(fh.read(), self.good)
        self.assertFalse(os.path.exists(schedule.tmp_store_path(self.store)))
        self.assertFalse(os.path.exists(self.store + ".prev"))

    def test_success_folds_into_a_copy_and_keeps_the_previous_file(self):
        runner = Runner()
        code, _out, _err = self.run_cli(["run", "--streams", self.path], runner=runner)
        self.assertEqual(code, 0)
        gather_store = runner.gathers()[0][runner.gathers()[0].index("--store") + 1]
        self.assertEqual(gather_store, schedule.tmp_store_path(self.store))
        self.assertEqual(schedule.store_counts(self.store)["prs"], 4)
        with open(self.store + ".prev", "rb") as fh:
            self.assertEqual(fh.read(), self.good)
        self.assertFalse(os.path.exists(gather_store))
        # one generation only
        self.run_cli(["run", "--streams", self.path], runner=Runner())
        self.assertEqual(schedule.store_counts(self.store + ".prev")["prs"], 4)

    def test_gather_failure_never_replaces_the_store(self):
        runner = Runner(gather=lambda argv: (1, "", "error: boom\n"))
        code, _out, _err = self.run_cli(["run", "--streams", self.path], runner=runner)
        self.assertEqual(code, 1)
        self.assertStoreUntouched()
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertEqual(attempt["result"], "gather_failed")
        self.assertIn("error: boom", attempt["notes"]["lines"])
        self.assertEqual(len([a for a, _ in runner.calls if a[1].endswith("validate.py")]), 0)

    def test_validate_failure_never_replaces_the_store(self):
        runner = Runner(validate=lambda argv: (1, "[FAIL] edge_set\nRESULT: FAIL\n", ""))
        code, _out, _err = self.run_cli(["run", "--streams", self.path], runner=runner)
        self.assertEqual(code, 1)
        self.assertStoreUntouched()
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertEqual(attempt["result"], "validate_failed")
        self.assertEqual(attempt["validate"], {"exit": 1, "result": "FAIL"})
        self.assertNotIn("last_success", self.status()["windows"]["2026-11"])

    def test_validate_runs_on_the_copy_without_the_token(self):
        runner = Runner()
        self.run_cli(["run", "--streams", self.path], runner=runner)
        argv, env = [c for c in runner.calls if c[0][1].endswith("validate.py")][0]
        self.assertEqual(argv[2], schedule.tmp_store_path(self.store))
        self.assertEqual(argv[3:], ["--project", "o", "--repo", "r"])
        self.assertNotIn("GITHUB_TOKEN", env)
        gather_env = runner.calls[0][1]
        self.assertEqual(gather_env["GITHUB_TOKEN"], TOKEN)

    def test_an_open_store_is_not_swapped(self):
        reader = sqlite3.connect(self.store)
        self.addCleanup(reader.close)
        reader.execute("SELECT count(*) FROM nodes").fetchall()  # holds -wal/-shm
        code, _out, _err = self.run_cli(["run", "--streams", self.path], runner=Runner())
        self.assertEqual(code, 1)
        self.assertEqual(self.status()["windows"]["2026-11"]["last_attempt"]["result"],
                         "swap_failed")
        self.assertEqual(schedule.store_counts(self.store)["prs"], 3)

    def test_a_first_gather_creates_the_store(self):
        os.remove(self.store)
        code, _out, _err = self.run_cli(["run", "--streams", self.path], runner=Runner())
        self.assertEqual(code, 0)
        self.assertEqual(schedule.store_counts(self.store)["prs"], 1)
        self.assertFalse(os.path.exists(self.store + ".prev"))


# --- lock --------------------------------------------------------------------------------

class TestLock(Case):
    def test_a_held_lock_stops_the_run(self):
        path = streams_file(self.tmp)
        os.makedirs(self.ws)
        lock = os.path.join(self.ws, schedule.LOCK_NAME)
        with open(lock, "w") as fh:
            json.dump({"pid": 1, "host": "h", "started_at": "2026-11-20T01:00:00Z"}, fh)
        runner = Runner()
        code, _out, err = self.run_cli(["run", "--streams", path], runner=runner)
        self.assertEqual(code, schedule.EXIT_LOCKED)
        self.assertIn("another run holds", err)
        self.assertEqual(runner.calls, [])
        self.assertTrue(os.path.exists(lock))

    def test_a_stale_lock_is_reported_not_broken(self):
        path = streams_file(self.tmp)
        os.makedirs(self.ws)
        lock = os.path.join(self.ws, schedule.LOCK_NAME)
        with open(lock, "w") as fh:
            json.dump({"pid": 1, "host": "h", "started_at": "2026-11-19T01:00:00Z"}, fh)
        code, _out, err = self.run_cli(["run", "--streams", path])
        self.assertEqual(code, schedule.EXIT_LOCKED)
        self.assertIn("STALE lock", err)
        self.assertIn("25.0 h ago", err)
        self.assertTrue(os.path.exists(lock))
        code, _out, _err = self.run_cli(["run", "--streams", path, "--break-stale-lock"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(lock))

    def test_break_stale_lock_never_breaks_a_fresh_one(self):
        os.makedirs(self.ws)
        lock = os.path.join(self.ws, schedule.LOCK_NAME)
        with open(lock, "w") as fh:
            json.dump({"started_at": "2026-11-20T01:30:00Z"}, fh)
        with self.assertRaises(schedule.Locked):
            schedule.acquire_lock(self.ws, T("2026-11-20T02:00:00Z"), 12, break_stale=True)

    def test_the_lock_is_released_after_a_failing_run(self):
        path = streams_file(self.tmp)
        self.run_cli(["run", "--streams", path],
                     runner=Runner(gather=lambda argv: (2, "", "x")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, schedule.LOCK_NAME)))


# --- status, notes, redaction ------------------------------------------------------------

class TestStatus(Case):
    def test_stream_and_rollup_status(self):
        path = streams_file(self.tmp)
        err = ("pr activity: o/r: checks gathered for 5 PR(s), 2 capped, 0 failed, "
               "0 not found, 0 not requested (3 request(s), 17 point(s)); 9 "
               "lifecycle event(s) in 2s\n"
               "affiliation: o/r: 9 person(s), 4 API call(s) (cached answers reused): "
               "2 maintainer_org, 3 community, 4 unknown (3 not checked)\n"
               "warning: GitHub primary rate limit hit on https://api.github.com/x; "
               "waiting 60s (attempt 1 of 5)\n"
               "folded bundle into store\n")
        clock = FakeClock(step=1.5)
        code, _out, _err = self.run_cli(["run", "--streams", path],
                                        runner=Runner(gather=lambda a: (0, "", err)),
                                        clock=clock)
        self.assertEqual(code, 0)
        st = self.status()
        self.assertEqual(st["stream"], "demo")
        self.assertEqual(st["last_run"]["result"], "ok")
        self.assertTrue(st["last_run"]["log"].startswith("logs" + os.sep + "run-"))
        win = st["windows"]["2026-11"]
        self.assertEqual(win["store"], os.path.join("demo", "stores", "2026-11.db"))
        ok = win["last_success"]
        self.assertEqual(ok, win["last_attempt"])
        self.assertEqual(ok["at"], "2026-11-20T02:00:00Z")
        self.assertEqual(ok["code_version"], {"sha": "abc123", "dirty": False})
        self.assertEqual(ok["validate"], {"exit": 0, "result": "OK"})
        self.assertEqual(ok["counts"]["prs"], 1)
        self.assertGreater(ok["duration_s"], 0)
        self.assertEqual(ok["gathers"][0]["exit"], 0)
        notes = ok["notes"]
        self.assertEqual(notes["api_points"], 17)
        self.assertEqual(notes["capped"], 2)
        self.assertEqual(notes["not_checked"], 3)
        self.assertEqual(notes["rate_limit_waits"], 1)
        self.assertEqual(notes["warnings"], 1)
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            top = json.load(fh)
        self.assertEqual(top["last_run"]["exit_code"], 0)
        self.assertEqual(top["streams"]["demo"]["windows"], {"2026-11": "ok"})
        self.assertEqual(top["streams"]["demo"]["last_success_at"], "2026-11-20T02:00:00Z")
        # a later failed attempt keeps the last success
        self.run_cli(["run", "--streams", path],
                     runner=Runner(gather=lambda a: (1, "", "error: x\n")))
        win = self.status()["windows"]["2026-11"]
        self.assertEqual(win["last_attempt"]["result"], "gather_failed")
        self.assertEqual(win["last_success"]["at"], "2026-11-20T02:00:00Z")

    def test_gather_notes_without_points(self):
        notes = schedule.gather_notes("note: o/r: base branch 'main'\nfolded\n")
        self.assertIsNone(notes["api_points"])
        self.assertEqual(notes["lines"], ["note: o/r: base branch 'main'"])

    def test_status_command(self):
        path = streams_file(self.tmp)
        self.run_cli(["run", "--streams", path])
        code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertEqual(code, 0)
        self.assertIn("demo: ok", out)
        self.assertIn("2026-11: last success 2026-11-20T02:00:00Z", out)


class TestToken(Case):
    def test_missing_token_stops_before_anything_runs(self):
        path = streams_file(self.tmp)
        runner = Runner()
        code, _out, err = self.run_cli(["run", "--streams", path], runner=runner,
                                       env={"PATH": "/bin"})
        self.assertEqual(code, 2)
        self.assertIn("GITHUB_TOKEN", err)
        self.assertEqual(runner.calls, [])

    def test_token_is_redacted_from_log_status_and_stderr(self):
        path = streams_file(self.tmp)
        leak = "debug: Authorization: token {} and a stray ghp_{}\n".format(
            TOKEN, "Z" * 36)
        code, out, err = self.run_cli(
            ["run", "--streams", path],
            runner=Runner(gather=lambda a: (1, leak, "error: " + leak)))
        self.assertEqual(code, 1)
        files = []
        for root, _dirs, names in os.walk(self.ws):
            files += [os.path.join(root, n) for n in names]
        self.assertTrue(any("logs" in f for f in files))
        for f in files:
            if f.endswith((".json", ".log")):
                with open(f, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn(TOKEN, text, f)
                self.assertNotIn("ghp_ZZZZ", text, f)
        self.assertNotIn(TOKEN, out + err)
        self.assertIn(schedule.REDACTED, err)

    def test_redact(self):
        self.assertEqual(schedule.redact("a {} b".format(TOKEN), [TOKEN]),
                         "a [REDACTED] b")
        self.assertEqual(schedule.redact("x github_pat_" + "a" * 30, []), "x [REDACTED]")
        self.assertEqual(schedule.secrets_of({"GH_TOKEN": "short", "MY_PAT": "p" * 9,
                                              "HOME": "/home/someone"}), ["p" * 9])


class TestSyncedFolder(Case):
    def test_reasons(self):
        self.assertTrue(schedule.synced_folder_reasons(
            "/Users/me/Library/Mobile Documents/iCloud~md~obsidian/Documents/x",
            isdir=lambda p: False))
        self.assertTrue(schedule.synced_folder_reasons("/Users/me/Dropbox/data",
                                                       isdir=lambda p: False))
        self.assertTrue(schedule.synced_folder_reasons("/home/me/OneDrive - Org/a",
                                                       isdir=lambda p: False))
        vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(vault, ".obsidian"))
        self.assertIn(".obsidian", " ".join(
            schedule.synced_folder_reasons(os.path.join(vault, "data", "ws"))))
        self.assertEqual(schedule.synced_folder_reasons(
            "/home/me/activity-data", isdir=lambda p: False), [])

    def test_run_warns_but_does_not_fail(self):
        vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(vault, ".obsidian"))
        path = streams_file(self.tmp, workspace="vault/ws")
        out, err = io.StringIO(), io.StringIO()
        clock = FakeClock()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["run", "--streams", path], run=Runner(),
                                 now=clock.now, clock=clock.clock,
                                 version_of=lambda: None,
                                 base_env={"GITHUB_TOKEN": TOKEN})
        self.assertEqual(code, 0)
        self.assertIn("looks synced", err.getvalue())
        with open(os.path.join(vault, "ws", "status.json"), encoding="utf-8") as fh:
            self.assertIn(".obsidian", json.load(fh)["workspace_warnings"][0])


class TestStreamsIndependent(Case):
    def test_one_failing_stream_does_not_stop_the_next(self):
        path = streams_file(self.tmp, [
            {"id": "bad", "kind": "repo", "owner": "o", "repo": "broken"},
            {"id": "good", "kind": "repo", "owner": "o", "repo": "r"},
            {"id": "off", "kind": "repo", "owner": "o", "repo": "x", "enabled": False}])

        def gather(argv):
            if argv[argv.index("--repo") + 1] == "broken":
                raise RuntimeError("seam exploded")
            return 0, "", ""
        code, _out, _err = self.run_cli(["run", "--streams", path],
                                        runner=Runner(gather=gather))
        self.assertEqual(code, 1)
        self.assertEqual(self.status("bad")["windows"]["2026-11"]["last_attempt"]["result"],
                         "error")
        self.assertEqual(self.status("good")["last_run"]["result"], "ok")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "off")))
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            top = json.load(fh)
        self.assertEqual({k: v["result"] for k, v in top["streams"].items()},
                         {"bad": "failed", "good": "ok"})

    def test_stream_selects_one_even_when_disabled(self):
        path = streams_file(self.tmp, [
            {"id": "a", "kind": "repo", "owner": "o", "repo": "r"},
            {"id": "off", "kind": "repo", "owner": "o", "repo": "x", "enabled": False}])
        runner = Runner()
        code, _out, _err = self.run_cli(["run", "--streams", path, "--stream", "off"],
                                        runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual([a[a.index("--repo") + 1] for a in runner.gathers()], ["x"])
        code, _out, err = self.run_cli(["run", "--streams", path, "--stream", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown stream", err)

    def test_time_budget_skips_and_records(self):
        path = streams_file(self.tmp, [
            {"id": "a", "kind": "repo", "owner": "o", "repo": "r"},
            {"id": "b", "kind": "repo", "owner": "o", "repo": "s"}])
        clock = FakeClock(step=0.0)

        def slow(argv):
            clock.mono += 61 * 60
            return 0, "", ""
        code, _out, _err = self.run_cli(["run", "--streams", path, "--max-minutes", "60",
                                         "--today", "2026-12-02"],
                                        runner=Runner(gather=slow), clock=clock)
        self.assertEqual(code, 1)
        a, b = self.status("a")["windows"], self.status("b")["windows"]
        self.assertEqual(a["2026-11"]["last_attempt"]["result"], "ok")
        self.assertEqual(a["2026-12"]["last_attempt"]["result"], "skipped_time_budget")
        self.assertEqual(b["2026-11"]["last_attempt"]["result"], "skipped_time_budget")
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            top = json.load(fh)
        self.assertEqual(top["last_run"]["skipped_time_budget"],
                         ["a:2026-12", "b:2026-11", "b:2026-12"])
        self.assertEqual(top["streams"]["b"]["result"], "skipped")


class TestManifestStream(Case):
    def test_window_and_registry_are_written_into_a_run_manifest(self):
        os.makedirs(os.path.join(self.ws, "tf"))
        with open(os.path.join(self.ws, "tf", "manifest.json"), "w") as fh:
            json.dump({"project": "avm-tf", "window": {"from": "2026-01-01",
                                                       "to": "2026-01-31"},
                       "repos": [{"owner": "o", "repo": "a"}],
                       "people": "../elsewhere.json"}, fh)
        path = streams_file(self.tmp, [{"id": "tf", "kind": "manifest",
                                        "manifest": "tf/manifest.json"}])
        runner = Runner()
        code, _out, _err = self.run_cli(["run", "--streams", path], runner=runner)
        self.assertEqual(code, 0)
        argv = runner.gathers()[0]
        mpath = argv[argv.index("--manifest") + 1]
        with open(mpath, encoding="utf-8") as fh:
            run_manifest = json.load(fh)
        self.assertEqual(run_manifest["window"], {"from": "2026-11-01", "to": "2026-11-30"})
        self.assertEqual(run_manifest["people"], os.path.join(self.ws, "people.json"))
        self.assertIn("--ref-date", argv)
        self.assertNotIn("--owner", argv)
        vargv = [a for a, _ in runner.calls if a[1].endswith("validate.py")][0]
        self.assertEqual(vargv[3:], ["--project", "avm-tf"])

    def test_missing_manifest_fails_that_stream_only(self):
        path = streams_file(self.tmp, [{"id": "tf", "kind": "manifest",
                                        "manifest": "tf/manifest.json"}])
        code, _out, _err = self.run_cli(["run", "--streams", path])
        self.assertEqual(code, 1)
        self.assertEqual(self.status("tf")["windows"]["2026-11"]["last_attempt"]["result"],
                         "config_error")


class TestTemplates(unittest.TestCase):
    def test_launch_agent_and_wrappers(self):
        import plistlib
        ex = os.path.join(HERE, "examples")
        with open(os.path.join(ex, "launchd", "local.activity-overview.gather.plist"),
                  "rb") as fh:
            plist = plistlib.load(fh)
        self.assertIs(plist["RunAtLoad"], False)
        self.assertEqual(sorted(plist["StartCalendarInterval"]), ["Hour", "Minute"])
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertTrue(plist["StandardErrorPath"].startswith("__WORKSPACE__/logs/"))
        for rel_path in ("launchd/activity-gather.sh", "cron/activity-gather.sh"):
            with open(os.path.join(ex, rel_path), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('schedule.py" run --streams', text)
            self.assertNotRegex(text, r"gh[pousr]_[A-Za-z0-9]{20,}")
            self.assertTrue(os.access(os.path.join(ex, rel_path), os.X_OK), rel_path)


class TestEnv(Case):
    def test_caps_reach_the_gather_and_the_plugin_cache_is_in_the_workspace(self):
        path = streams_file(self.tmp, [{"id": "demo", "kind": "repo", "owner": "o",
                                        "repo": "r", "env": {"ACTIVITY_NEWCOMERS_MAX": 5}}],
                            env={"TF_PLUGIN_CACHE_DIR": ".tfcache",
                                 "ACTIVITY_IAC_MAX_WORKERS": "2"})
        runner = Runner()
        code, _out, _err = self.run_cli(["run", "--streams", path], runner=runner)
        self.assertEqual(code, 0)
        env = runner.calls[0][1]
        self.assertEqual(env["ACTIVITY_NEWCOMERS_MAX"], "5")
        self.assertEqual(env["ACTIVITY_IAC_MAX_WORKERS"], "2")
        self.assertEqual(env["TF_PLUGIN_CACHE_DIR"], os.path.join(self.ws, ".tfcache"))
        self.assertTrue(os.path.isdir(os.path.join(self.ws, ".tfcache")))


# --- the reader ------------------------------------------------------------------------------

class TestLatest(Case):
    def setUp(self):
        super().setUp()
        self.path = streams_file(self.tmp, [{"id": "demo", "kind": "repo", "owner": "o",
                                             "repo": "r", "cadence": ["weekly", "monthly"]}])

    def write_status(self, windows):
        os.makedirs(os.path.join(self.ws, "demo"), exist_ok=True)
        with open(os.path.join(self.ws, "demo", "status.json"), "w") as fh:
            json.dump({"windows": windows}, fh)

    @staticmethod
    def entry(month, frm, to, at, result="ok", attempt_at=None, attempt_result="ok",
              verdict="OK"):
        ok = {"at": at, "result": result, "validate": {"exit": 0, "result": verdict}}
        return {"month": month, "from": frm, "to": to, "last_success": ok,
                "last_attempt": {"at": attempt_at or at, "result": attempt_result,
                                 "error": None if attempt_result == "ok" else "boom"}}

    def latest(self, *args, now="2026-11-20T08:00:00Z"):
        clock = FakeClock(start=T(now))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["latest", "--streams", self.path, "--stream", "demo",
                                  *args], now=clock.now)
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def test_month_is_fresh(self):
        store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        make_store(store, [("2026-11-01", "2026-11-30")])
        self.write_status({"2026-11": self.entry("2026-11", "2026-11-01", "2026-11-30",
                                                 "2026-11-20T02:00:00Z")})
        code, out, _err = self.latest("--month", "2026-11")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"])
        self.assertTrue(out["single_store"])
        s = out["stores"][0]
        self.assertEqual(s["store"], store)
        self.assertEqual(s["age_hours"], 6.0)
        self.assertEqual(s["validate"], "OK")
        self.assertFalse(s["final"])
        self.assertEqual(out["read"][0]["view"][2:], [
            "--store", store, "--project", "o", "--repo", "r", "--from", "2026-11-01",
            "--to", "2026-11-30", "--out", "VIEW.json"])

    def test_stale_open_month_is_refused_unless_accepted(self):
        store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        make_store(store, [("2026-11-01", "2026-11-30")])
        self.write_status({"2026-11": self.entry("2026-11", "2026-11-01", "2026-11-30",
                                                 "2026-11-18T02:00:00Z")})
        code, out, err = self.latest("--month", "2026-11")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("54.0 h ago", out["refusal"])
        self.assertIn("REFUSED", err)
        code, out, _err = self.latest("--month", "2026-11", "--max-age-hours", "72")
        self.assertEqual(code, 0)
        code, out, _err = self.latest("--month", "2026-11", "--accept-stale")
        self.assertEqual(code, 0)
        self.assertTrue(out["overridden"])

    def test_closed_month_gathered_after_its_end_is_never_stale(self):
        store = os.path.join(self.ws, "demo", "stores", "2026-10.db")
        make_store(store, [("2026-10-01", "2026-10-31")])
        self.write_status({"2026-10": self.entry("2026-10", "2026-10-01", "2026-10-31",
                                                 "2026-11-07T02:00:00Z")})
        code, out, _err = self.latest("--month", "2026-10")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["stores"][0]["final"])

    def test_a_failed_last_run_is_refused(self):
        store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        make_store(store, [("2026-11-01", "2026-11-30")])
        self.write_status({"2026-11": self.entry(
            "2026-11", "2026-11-01", "2026-11-30", "2026-11-20T01:00:00Z",
            attempt_at="2026-11-20T02:00:00Z", attempt_result="validate_failed")})
        code, out, _err = self.latest("--month", "2026-11")
        self.assertEqual(code, 1)
        self.assertIn("failed", out["refusal"])

    def test_nothing_gathered_is_refused(self):
        code, out, _err = self.latest("--month", "2026-11")
        self.assertEqual(code, 1)
        self.assertIn("no successful scheduled run", out["refusal"])

    def test_a_week_spanning_two_months_reads_one_store_with_the_carried_days(self):
        dec = os.path.join(self.ws, "demo", "stores", "2026-12.db")
        make_store(dec, [("2026-11-25", "2026-11-30"), ("2026-12-01", "2026-12-31")])
        self.write_status({"2026-12": self.entry("2026-12", "2026-12-01", "2026-12-31",
                                                 "2026-12-04T02:00:00Z")})
        code, out, _err = self.latest("--last-days", "7", now="2026-12-04T09:00:00Z")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["period"], {"from": "2026-11-28", "to": "2026-12-04"})
        self.assertTrue(out["single_store"])
        self.assertEqual([r["store"] for r in out["read"]], [dec])
        self.assertEqual(out["read"][0]["from"], "2026-11-28")

    def test_a_span_no_store_holds_lists_each_month(self):
        for month, frm, to in (("2026-11", "2026-11-01", "2026-11-30"),
                               ("2026-12", "2026-12-01", "2026-12-31")):
            make_store(os.path.join(self.ws, "demo", "stores", month + ".db"), [(frm, to)])
        self.write_status({
            "2026-11": self.entry("2026-11", "2026-11-01", "2026-11-30",
                                  "2026-12-04T02:00:00Z"),
            "2026-12": self.entry("2026-12", "2026-12-01", "2026-12-31",
                                  "2026-12-04T02:00:00Z")})
        code, out, _err = self.latest("--from", "2026-11-20", "--to", "2026-12-04",
                                      now="2026-12-04T09:00:00Z")
        self.assertEqual(code, 0, out)
        self.assertFalse(out["single_store"])
        self.assertEqual([(r["from"], r["to"]) for r in out["read"]],
                         [("2026-11-20", "2026-11-30"), ("2026-12-01", "2026-12-04")])
        self.assertIn("roll-up", out["split_note"])

    def test_period_arguments(self):
        for args in ([], ["--month", "2026-11", "--last-days", "3"], ["--from", "2026-11-01"],
                     ["--month", "2026-13"], ["--from", "2026-11-05", "--to", "2026-11-01"]):
            with self.subTest(args=args):
                code, _out, _err = self.latest(*args)
                self.assertEqual(code, 2)


class TestRetention(Case):
    def test_old_logs_and_opt_in_old_stores_go(self):
        cfg = self.cfg(retention={"log_days": 1, "store_months": 2})
        logs = os.path.join(self.ws, "logs")
        os.makedirs(logs)
        old = os.path.join(logs, "run-old.log")
        new = os.path.join(logs, "run-new.log")
        for p in (old, new):
            with open(p, "w") as fh:
                fh.write("x")
        os.utime(old, (0, 0))
        stores = os.path.join(self.ws, "demo", "stores")
        for month in ("2026-08", "2026-10", "2026-11"):
            make_store(os.path.join(stores, month + ".db"))
        schedule.apply_retention(cfg, D("2026-11-20"), lambda *a, **k: None)
        self.assertEqual(sorted(os.listdir(logs)), ["run-new.log"])
        self.assertEqual(sorted(os.listdir(stores)), ["2026-10.db", "2026-11.db"])

    def test_prev_is_kept_only_while_the_month_is_regathered(self):
        cfg = self.cfg()
        stores = os.path.join(self.ws, "demo", "stores")
        for name in ("2026-09.db", "2026-09.db.prev", "2026-10.db", "2026-10.db.prev",
                     "2026-11.db", "2026-11.db.prev"):
            make_store(os.path.join(stores, name))
        schedule.apply_retention(cfg, D("2026-11-05"), lambda *a, **k: None)
        self.assertEqual(sorted(os.listdir(stores)), [
            "2026-09.db", "2026-10.db", "2026-10.db.prev", "2026-11.db",
            "2026-11.db.prev"])
        schedule.apply_retention(cfg, D("2026-11-08"), lambda *a, **k: None)
        self.assertEqual(sorted(os.listdir(stores)), [
            "2026-09.db", "2026-10.db", "2026-11.db", "2026-11.db.prev"])


# --- end to end: the real gather and validate CLIs -------------------------------------------

GIT_TEST_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}

# gather with its GitHub API layer stubbed empty and no IaC toolchain; it also
# prints the token it was given, which the runner must redact.
SHIM = """\
import os, sys
sys.path.insert(0, {skill!r})
import gather
url = os.environ["AO_TEST_REMOTE"]
sys.stderr.write("debug: token " + os.environ.get("GITHUB_TOKEN", "") + "\\n")
gather._github_remote_url = lambda o, r: url
gather._paginated = lambda token: (lambda u: ([], None))
gather._graphql_client = lambda token: None
_p = gather.select_code_area_provider
gather.select_code_area_provider = lambda paths, d, **kw: _p(
    paths, d, which=lambda n: None, **kw)
_i = gather.extract_iac_edges
gather.extract_iac_edges = lambda g, d, **kw: _i(g, d, which=lambda n: None, **kw)
gather.main(sys.argv[1:])
"""


def git(*args, cwd=None, date=None):
    env = dict(os.environ)
    env.update(GIT_TEST_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date + "T12:00:00Z"
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=T",
                           "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class TestEndToEnd(Case):
    def test_runner_drives_the_real_gather_and_validate(self):
        src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        for day in ("2026-10-28", "2026-11-03", "2026-11-12"):
            os.makedirs(os.path.join(src, "modules", "net"), exist_ok=True)
            with open(os.path.join(src, "modules", "net", "main.tf"), "w") as fh:
                fh.write('variable "v" {{ default = "{}" }}\n'.format(day))
            git("add", "-A", cwd=src)
            git("commit", "-q", "-m", "change " + day, cwd=src, date=day)
        shim = os.path.join(self.tmp, "shim", "gather_shim.py")
        os.makedirs(os.path.dirname(shim))
        with open(shim, "w") as fh:
            fh.write(SHIM.format(skill=HERE))
        path = streams_file(self.tmp, [{
            "id": "demo", "kind": "repo", "owner": "o", "repo": "r",
            "cadence": ["weekly", "monthly"],
            "gather_flags": ["--no-workflows", "--no-releases", "--no-project-board",
                             "--no-newcomers", "--no-affiliation", "--no-pr-checks"]}])
        before = sorted(n for n in os.listdir(HERE) if n != "__pycache__")
        env = dict(os.environ, GITHUB_TOKEN=TOKEN, AO_TEST_REMOTE="file://" + src,
                   **GIT_TEST_ENV)
        env.pop("GH_TOKEN", None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["run", "--streams", path, "--today", "2026-11-20"],
                                 base_env=env, gather_script=shim)
            code2 = schedule.main(["run", "--streams", path, "--today", "2026-11-20"],
                                  base_env=env, gather_script=shim)
        self.assertEqual((code, code2), (0, 0), err.getvalue()[-3000:])
        store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        self.assertTrue(os.path.isfile(store))
        self.assertTrue(os.path.isfile(store + ".prev"))
        self.assertFalse(os.path.exists(schedule.tmp_store_path(store)))
        self.assertEqual(schedule.store_windows(store),
                         [("2026-10-26", "2026-10-31"), ("2026-11-01", "2026-11-30")])
        st = self.status()
        ok = st["windows"]["2026-11"]["last_success"]
        self.assertEqual(ok["validate"], {"exit": 0, "result": "OK"})
        self.assertEqual(ok["counts"]["commits"], 3)
        self.assertEqual([g["part"] for g in ok["gathers"]], ["month"])  # carried once
        first_run = st["windows"]["2026-11"]["carried"]
        self.assertEqual((first_run["from"], first_run["to"]), ("2026-10-26", "2026-10-31"))
        # the token never reaches a file, and the gather's own echo was redacted
        for root, _dirs, names in os.walk(self.ws):
            for n in names:
                if n.endswith((".json", ".log")):
                    with open(os.path.join(root, n), encoding="utf-8") as fh:
                        self.assertNotIn(TOKEN, fh.read(), n)
        self.assertIn("debug: token " + schedule.REDACTED, err.getvalue())
        # nothing was written into the skill directory
        self.assertEqual(sorted(n for n in os.listdir(HERE) if n != "__pycache__"), before)
        # the reader: the month is fresh; a week across the boundary is one store
        clock = FakeClock(start=T("2026-11-20T09:00:00Z"))
        rout = io.StringIO()
        with contextlib.redirect_stdout(rout), contextlib.redirect_stderr(io.StringIO()):
            rc = schedule.main(["latest", "--streams", path, "--stream", "demo",
                                "--from", "2026-10-28", "--to", "2026-11-03"],
                               now=lambda: T(ok["finished_at"]) + datetime.timedelta(hours=1))
        self.assertEqual(rc, 0, rout.getvalue())
        answer = json.loads(rout.getvalue())
        self.assertTrue(answer["single_store"])
        del clock
        # view.py over the week reads both sides of the boundary from that store
        view_out = os.path.join(self.tmp, "week.json")
        argv = [a if a != "VIEW.json" else view_out for a in answer["read"][0]["view"]]
        argv[0] = sys.executable
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(view_out, encoding="utf-8") as fh:
            commits = sorted(c["date"][:10] for c in json.load(fh)["commits"])
        self.assertEqual(commits, ["2026-10-28", "2026-11-03"])
        if os.environ.get("AO_PRINT_STATUS"):
            sys.stderr.write(json.dumps(st, indent=2) + "\n")


# --- offline recompute -----------------------------------------------------------------------

class TestRecompute(Case):
    def setUp(self):
        super().setUp()
        self.path = streams_file(self.tmp)
        self.store = os.path.join(self.ws, "demo", "stores", "2026-11.db")
        make_store(self.store, [("2026-10-26", "2026-10-31"),
                                ("2026-11-01", "2026-11-30")], prs=2)
        with open(self.store, "rb") as fh:
            self.good = fh.read()

    def test_recomputes_every_window_on_a_copy_then_validates_and_swaps(self):
        runner = Runner()
        code, _out, _err = self.run_cli(["recompute", "--streams", self.path,
                                         "--stream", "demo", "--month", "2026-11"],
                                        runner=runner)
        self.assertEqual(code, 0)
        tmp = schedule.tmp_store_path(self.store)
        gathers = runner.gathers()
        self.assertEqual([(a[a.index("--from") + 1], a[a.index("--to") + 1])
                          for a in gathers],
                         [("2026-10-26", "2026-10-31"), ("2026-11-01", "2026-11-30")])
        for argv in gathers:
            self.assertEqual(argv[-1], "--recompute-ownership")
            self.assertEqual(argv[argv.index("--store") + 1], tmp)
            self.assertEqual(argv[argv.index("--clone-dir") + 1],
                             os.path.join(self.ws, "demo", "clone"))
            self.assertNotIn("--people", argv)
        for _argv, env in runner.calls:
            self.assertNotIn("GITHUB_TOKEN", env)        # offline: no token needed
        vargv = [a for a, _ in runner.calls if a[1].endswith("validate.py")][0]
        self.assertEqual(vargv[2], tmp)
        self.assertEqual(schedule.store_counts(self.store)["prs"], 4)
        with open(self.store + ".prev", "rb") as fh:
            self.assertEqual(fh.read(), self.good)
        rec = self.status()["windows"]["2026-11"]["last_recompute"]
        self.assertEqual((rec["result"], rec["what"]), ("ok", "ownership"))
        self.assertEqual(rec["validate"], {"exit": 0, "result": "OK"})
        self.assertFalse(os.path.exists(os.path.join(self.ws, schedule.LOCK_NAME)))

    def test_a_failed_validate_keeps_the_store(self):
        runner = Runner(validate=lambda argv: (1, "RESULT: FAIL\n", ""))
        code, _out, _err = self.run_cli(["recompute", "--streams", self.path,
                                         "--stream", "demo", "--month", "2026-11"],
                                        runner=runner)
        self.assertEqual(code, 1)
        with open(self.store, "rb") as fh:
            self.assertEqual(fh.read(), self.good)
        self.assertFalse(os.path.exists(schedule.tmp_store_path(self.store)))
        rec = self.status()["windows"]["2026-11"]["last_recompute"]
        self.assertEqual(rec["result"], "validate_failed")

    def test_a_failed_recompute_keeps_the_store(self):
        runner = Runner(gather=lambda argv: (1, "", "error: clone missing\n"))
        code, _out, _err = self.run_cli(["recompute", "--streams", self.path,
                                         "--stream", "demo", "--month", "2026-11"],
                                        runner=runner)
        self.assertEqual(code, 1)
        with open(self.store, "rb") as fh:
            self.assertEqual(fh.read(), self.good)
        self.assertEqual(len(runner.gathers()), 1)      # stops at the first failure

    def test_refusals(self):
        code, _out, err = self.run_cli(["recompute", "--streams", self.path,
                                        "--stream", "demo", "--month", "2026-09"])
        self.assertEqual(code, 2)
        self.assertIn("holds no gathered window", err)
        mpath = streams_file(self.tmp, [{"id": "tf", "kind": "manifest",
                                         "manifest": "tf/m.json"}])
        make_store(os.path.join(self.ws, "tf", "stores", "2026-11.db"),
                   [("2026-11-01", "2026-11-30")])
        code, _out, err = self.run_cli(["recompute", "--streams", mpath,
                                        "--stream", "tf", "--month", "2026-11"])
        self.assertEqual(code, 2)
        self.assertIn("single-repository", err)

    def test_dry_run(self):
        runner = Runner()
        code, out, _err = self.run_cli(["recompute", "--streams", self.path, "--stream",
                                        "demo", "--month", "2026-11", "--dry-run"],
                                       runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertEqual(out.count("--recompute-ownership"), 2)
        self.assertIn("then: swap", out)

    def test_every_recompute_flag_exists_in_gather(self):
        proc = subprocess.run([sys.executable, schedule.GATHER_SCRIPT, "--help"],
                              capture_output=True, text=True)
        for flag in schedule.RECOMPUTE_FLAGS.values():
            self.assertIn(flag, proc.stdout)


# --- an expired / refused token ----------------------------------------------------------------

EXPIRED_401 = ("error: GitHub API 401 for https://api.github.com/repos/o/r\n"
               "  message: Bad credentials\n")
LIFETIME_403 = ("error: GitHub API 403 for https://api.github.com/repos/o/r\n"
                "  message: The 'Org' enterprise forbids access via a personal access "
                "token whose lifetime is greater than 8 days.\n"
                "  hint: 403 with rate-limit headers = rate limited; ...\n")


class TestAuthFailure(Case):
    def test_detection(self):
        self.assertIn("401", schedule.auth_failure(EXPIRED_401))
        self.assertIn("lifetime", schedule.auth_failure(LIFETIME_403))
        self.assertIn("SSO", schedule.auth_failure(
            "error: GitHub API 403 for u\n  x-github-sso: required; url=x\n"))
        self.assertEqual(schedule.auth_failure(
            "error: set GITHUB_TOKEN (or GH_TOKEN) with `repo` scope\n"),
            "no token in the environment")
        self.assertIsNone(schedule.auth_failure(
            "error: GitHub API 403 for u\n  x-ratelimit-remaining: 0\n"))
        self.assertIsNone(schedule.auth_failure("error: GitHub API 404 for u\n"))
        self.assertIsNone(schedule.auth_failure(""))

    def test_status_names_an_expired_token(self):
        path = streams_file(self.tmp)
        self.run_cli(["run", "--streams", path])
        code, _out, _err = self.run_cli(["run", "--streams", path],
                                        runner=Runner(gather=lambda a: (1, "", LIFETIME_403)))
        self.assertEqual(code, 1)
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertIn("lifetime", attempt["auth_error"])
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            self.assertIn("lifetime", json.load(fh)["streams"]["demo"]["auth_error"])
        code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertEqual(code, 1)
        self.assertIn("TOKEN: GitHub refused the token (403", out)
        self.assertIn("Keeping the token valid", out)

    def test_other_failures_are_not_called_token_problems(self):
        path = streams_file(self.tmp)
        self.run_cli(["run", "--streams", path],
                     runner=Runner(gather=lambda a: (1, "", "error: clone failed\n")))
        self.assertIsNone(self.status()["windows"]["2026-11"]["last_attempt"]["auth_error"])
        _code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertNotIn("TOKEN:", out)


# --- the commands after latest, per cadence ---------------------------------------------------

class TestNextCommands(Case):
    def setUp(self):
        super().setUp()
        self.path = streams_file(self.tmp, [
            {"id": "wk", "kind": "repo", "owner": "o", "repo": "r",
             "cadence": ["weekly", "monthly"]},
            {"id": "mo", "kind": "repo", "owner": "o", "repo": "r"}])
        for sid in ("wk", "mo"):
            make_store(os.path.join(self.ws, sid, "stores", "2026-11.db"),
                       [("2026-10-26", "2026-10-31"), ("2026-11-01", "2026-11-30")])
            os.makedirs(os.path.join(self.ws, sid), exist_ok=True)
            with open(os.path.join(self.ws, sid, "status.json"), "w") as fh:
                json.dump({"windows": {"2026-11": TestLatest.entry(
                    "2026-11", "2026-11-01", "2026-11-30", "2026-11-20T02:00:00Z")}}, fh)

    def latest(self, stream, *args):
        clock = FakeClock(start=T("2026-11-20T08:00:00Z"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["latest", "--streams", self.path, "--stream", stream,
                                  *args], now=clock.now)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_weekly_stream_gets_the_weekly_layout(self):
        code, out, err = self.latest("wk", "--last-days", "7")
        self.assertEqual(code, 0, out)
        nxt = out["next"]
        self.assertEqual(nxt["cadence"], "weekly")
        cmds = nxt["commands"]
        self.assertEqual(len(cmds), 4)
        self.assertIn("view.py --store", cmds[0])
        self.assertIn("--from 2026-11-14 --to 2026-11-20", cmds[0])
        self.assertIn("link.py", cmds[1])
        self.assertIn("--people " + os.path.join(self.ws, "people.json"), cmds[1])
        self.assertIn(os.path.join(self.ws, "wk", "series-weekly.json"), cmds[1])
        self.assertIn("--outline --weekly > ", cmds[2])
        self.assertIn("--work-package", cmds[3])
        self.assertTrue(cmds[3].endswith("--weekly"))
        self.assertIn("next (weekly issue):", err)

    def test_a_month_gets_the_monthly_layout(self):
        _code, out, _err = self.latest("wk", "--month", "2026-11")
        cmds = out["next"]["commands"]
        self.assertEqual(out["next"]["cadence"], "monthly")
        self.assertNotIn("--weekly", " ".join(cmds))
        self.assertIn(os.path.join(self.ws, "wk", "series.json"), cmds[1])

    def test_a_monthly_only_stream_and_the_override(self):
        _code, out, _err = self.latest("mo", "--from", "2026-11-01", "--to", "2026-11-14")
        self.assertEqual(out["next"]["cadence"], "monthly")
        _code, out, _err = self.latest("mo", "--last-days", "14", "--cadence", "weekly")
        self.assertEqual(out["next"]["cadence"], "weekly")
        self.assertIn("--weekly", out["next"]["commands"][2])

    def test_a_manifest_stream_views_each_member(self):
        os.makedirs(os.path.join(self.ws, "tf"), exist_ok=True)
        with open(os.path.join(self.ws, "tf", "m.json"), "w") as fh:
            json.dump({"project": "proj", "window": {"from": "2026-11-01",
                                                     "to": "2026-11-30"},
                       "repos": [{"owner": "o", "repo": "a"}, {"owner": "o", "repo": "b"}]},
                      fh)
        path = streams_file(self.tmp, [{"id": "tf", "kind": "manifest",
                                        "manifest": "tf/m.json"}])
        store = os.path.join(self.ws, "tf", "stores", "2026-11.db")
        os.makedirs(os.path.dirname(store))
        conn = graphstore.open_store(store)
        graphstore.init_schema(conn)
        graphstore.record_window(conn, "proj", "o/a", "2026-11-01", "2026-11-30")
        for repo in ("o/a", "o/b"):
            graphstore.upsert_node(conn, "proj/{}#pr-1".format(repo), "proj", repo,
                                   "social", "2026-11-02T00:00:00Z", {})
        conn.commit()
        conn.close()
        cfg = schedule.load_streams(path)
        nxt = schedule.next_commands(cfg, cfg["streams"][0], store, D("2026-11-01"),
                                     D("2026-11-30"), "monthly")
        self.assertEqual([os.path.basename(v) for v in nxt["views"]],
                         ["o__a.json", "o__b.json"])
        self.assertIn("--project proj --repo o/a", nxt["commands"][0])
        self.assertIn(os.path.join("series", "o__b.json"), nxt["commands"][3])
        self.assertIn("o__a.json", nxt["commands"][4])
        self.assertIn("o__b.json", nxt["commands"][4])


# --- the token: renew, status, expiry, reminder -------------------------------------------------

NEW_TOKEN = "github_pat_" + "N3w7oKen" * 8
OLD_TOKEN = "ghp_" + "0ld7oKen" * 5


class FakeAPI:
    """The API seam: answers per path, records every call (and the token
    it carried, to prove which token was checked)."""

    def __init__(self, user=200, probe=200, expiry="2026-12-05 10:00:00 UTC",
                 probe_message="ok"):
        self.calls = []
        self.user, self.probe, self.expiry = user, probe, expiry
        self.probe_message = probe_message

    def __call__(self, url, token, timeout=20):
        self.calls.append((url, token))
        if url.endswith("/user"):
            headers = {"github-authentication-token-expiration": self.expiry} \
                if self.expiry else {}
            if self.user != 200:
                return self.user, {}, json.dumps({"message": "Bad credentials"})
            return 200, headers, json.dumps({"login": "someone"})
        if self.probe != 200:
            return self.probe, {}, json.dumps({"message": self.probe_message})
        return 200, {}, json.dumps({"full_name": "o/r"})


class FakeSecurity:
    """The `security` seam: a one-item keychain. Records argv and stdin."""

    def __init__(self, stored=None, corrupt=False):
        self.calls = []
        self.items = {}
        if stored:
            self.items["activity-gh-token"] = stored
        self.corrupt = corrupt

    def __call__(self, argv, stdin_text=None):
        self.calls.append((list(argv), stdin_text))
        if argv == ["security", "-i"]:
            words = stdin_text.split()
            assert words[0] == "add-generic-password" and words[-2] == "-w", words
            svc = words[words.index("-s") + 1]
            self.items[svc] = "garbled" if self.corrupt else words[-1]
            return 0, ""
        if argv[1] == "find-generic-password":
            svc = argv[argv.index("-s") + 1]
            return (0, self.items[svc] + "\n") if svc in self.items else (44, "")
        return 1, ""


class TokenCliCase(Case):
    """A streams file with an env-file token, and `token …` run with every
    seam replaced."""

    def setUp(self):
        super().setUp()
        self.env_file = os.path.join(self.tmp, "cfg", "token.env")
        self.path = streams_file(self.tmp, token={"store": "env-file",
                                                  "env_file": self.env_file})
        self.clock = FakeClock(start=T("2026-11-28T10:00:00Z"))

    def token_cli(self, *argv, api=None, prompt=NEW_TOKEN, security=None, browser=True,
                  spawn=None, launch_code=0):
        self.opened, self.prompts, self.spawned, self.launched = [], [], [], []

        def launch(a):
            self.launched.append(a)
            return launch_code
        out, err = io.StringIO(), io.StringIO()

        def ask(text):
            self.prompts.append(text)
            return prompt

        def open_browser(url):
            self.opened.append(url)
            return browser
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", *argv, "--streams", self.path],
                                 http_get=api or FakeAPI(), prompt=ask,
                                 open_browser=open_browser,
                                 security=security or FakeSecurity(),
                                 spawn=spawn or (lambda a, e: self.spawned.append((a, e))),
                                 launch=launch, now=self.clock.now)
        self.assertNotIn(NEW_TOKEN, out.getvalue() + err.getvalue())
        self.assertNotIn(OLD_TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def write_old(self):
        schedule.write_env_file(self.env_file, OLD_TOKEN)

    def stored(self):
        with open(self.env_file, encoding="utf-8") as fh:
            return fh.read()


class TestTokenRenew(TokenCliCase):
    def test_page_url_uses_only_documented_parameters(self):
        url = schedule.token_page_url()
        base, _sep, query = url.partition("?")
        self.assertEqual(base, "https://github.com/settings/personal-access-tokens/new")
        params = urllib_parse_qs(query)
        self.assertEqual(sorted(params), ["description", "expires_in", "name"])
        self.assertEqual(params["expires_in"], ["7"])
        self.assertLessEqual(len(params["name"][0]), 40)
        self.assertEqual(schedule.token_page_url(classic=True),
                         "https://github.com/settings/tokens/new")

    def test_renew_prompts_checks_then_stores_atomically(self):
        self.write_old()
        api = FakeAPI()
        code, _out, err = self.token_cli("renew", api=api)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), "GITHUB_TOKEN={}\n".format(NEW_TOKEN))
        self.assertEqual(os.stat(self.env_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.env_file)).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(os.path.dirname(self.env_file)), ["token.env"])
        self.assertEqual(self.prompts, ["New token (input hidden): "])
        self.assertEqual(len(self.opened), 1)
        self.assertIn("Opened the new-token page", err)
        self.assertEqual([u.rsplit("/", 2)[-2:] for u, _t in api.calls],
                         [["api.github.com", "user"], ["o", "r"]])   # identity, then a read
        self.assertTrue(all(t == NEW_TOKEN for _u, t in api.calls))
        self.assertIn("someone - expires 2026-12-05T10:00:00Z (7.0 day(s) left)", err)

    def test_a_refused_token_keeps_the_old_one(self):
        self.write_old()
        lifetime = ("The 'Org' enterprise forbids access via a personal access token "
                    "whose lifetime is greater than 8 days.")
        for api in (FakeAPI(user=401), FakeAPI(probe=403, probe_message=lifetime)):
            with self.subTest(api=(api.user, api.probe)):
                code, _out, err = self.token_cli("renew", api=api)
                self.assertEqual(code, 1)
                self.assertIn("NOT stored", err)
                self.assertEqual(self.stored(), "GITHUB_TOKEN={}\n".format(OLD_TOKEN))
        self.assertIn("lifetime is greater than 8 days", err)

    def test_a_malformed_paste_is_refused_before_any_call(self):
        self.write_old()
        api = FakeAPI()
        code, _out, err = self.token_cli("renew", api=api, prompt="not a token!")
        self.assertEqual(code, 1)
        self.assertEqual(api.calls, [])
        self.assertIn("nothing changed", err)
        self.assertEqual(self.stored(), "GITHUB_TOKEN={}\n".format(OLD_TOKEN))

    def test_no_browser_prints_the_address(self):
        code, _out, err = self.token_cli("renew", "--no-browser")
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [])
        self.assertIn("Open the new-token page:\n  https://github.com/settings/"
                      "personal-access-tokens/new?", err)
        _code, _out, err = self.token_cli("renew", browser=False)
        self.assertIn("Open the new-token page", err)

    def test_keychain_store_keeps_the_token_off_every_argv(self):
        path = streams_file(self.tmp, token={"store": "keychain"})
        self.path = path
        sec = FakeSecurity(stored=OLD_TOKEN)
        code, _out, err = self.token_cli("renew", security=sec)
        self.assertEqual(code, 0, err)
        self.assertEqual(sec.items["activity-gh-token"], NEW_TOKEN)
        for argv, _stdin in sec.calls:
            self.assertNotIn(NEW_TOKEN, " ".join(argv))
        add = [c for c in sec.calls if c[0] == ["security", "-i"]]
        self.assertEqual(len(add), 1)
        self.assertIn("-U -a ", add[0][1])
        self.assertIn("-T /usr/bin/security -w " + NEW_TOKEN, add[0][1])
        # the item is read back and compared
        self.assertEqual(sec.calls[-1][0][1], "find-generic-password")

    def test_keychain_read_back_mismatch_fails(self):
        self.path = streams_file(self.tmp, token={"store": "keychain"})
        code, _out, err = self.token_cli("renew", security=FakeSecurity(corrupt=True))
        self.assertEqual(code, 1)
        self.assertIn("did not read back", err)

    def test_kick_starts_a_run_with_the_token_in_the_environment_only(self):
        with mock.patch.object(schedule.sys, "platform", "linux"):
            code, _out, _err = self.token_cli("renew", "--kick")
        self.assertEqual(code, 0)
        (argv, env), = self.spawned
        self.assertEqual(argv[2:], ["run", "--streams", self.path])
        self.assertNotIn(NEW_TOKEN, " ".join(argv))
        self.assertEqual(env["GITHUB_TOKEN"], NEW_TOKEN)
        with mock.patch.object(schedule.sys, "platform", "darwin"):
            code, _out, err = self.token_cli("renew", "--kick")
        self.assertEqual(code, 0)
        self.assertEqual(self.spawned, [])
        argv, = self.launched
        self.assertEqual(argv[:2], ["launchctl", "kickstart"])
        self.assertTrue(argv[2].endswith("/local.activity-overview.gather"))
        self.assertIn("started the scheduled job", err)

    def test_a_failed_kickstart_is_not_reported_as_started(self):
        with mock.patch.object(schedule.sys, "platform", "darwin"):
            code, _out, err = self.token_cli("renew", "--kick", launch_code=113)
        self.assertEqual(code, 1)
        self.assertIn("kickstart exited 113", err)
        self.assertIn("was NOT started", err)
        self.assertNotIn("started the scheduled job (", err)

    def test_status_shows_expiry_and_warns_near_the_end(self):
        self.write_old()
        code, out, _err = self.token_cli("status", api=FakeAPI())
        self.assertEqual(code, 0)
        self.assertIn("expires 2026-12-05T10:00:00Z (7.0 day(s) left)", out)
        self.assertNotIn("WARNING", out)
        code, out, _err = self.token_cli(
            "status", api=FakeAPI(expiry="2026-11-30 04:00:00 +0000"))
        self.assertEqual(code, 0)
        self.assertIn("WARNING: the token expires 2026-11-30T04:00:00Z (1.8 day(s) left)",
                      out)
        code, out, _err = self.token_cli("status", api=FakeAPI(user=401))
        self.assertEqual(code, 1)
        self.assertIn("refused", out)

    def test_status_without_a_stored_token(self):
        code, out, _err = self.token_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("no stored token", out)

    def test_parse_expiry(self):
        want = T("2026-12-05T10:00:00Z")
        self.assertEqual(schedule.parse_expiry("2026-12-05 10:00:00 UTC"), want)
        self.assertEqual(schedule.parse_expiry("2026-12-05 10:00:00 +0000"), want)
        self.assertEqual(schedule.parse_expiry("2026-12-05 11:00:00 +0100"), want)
        self.assertEqual(schedule.parse_expiry("2026-12-05 02:00:00 -0800"), want)
        self.assertEqual(schedule.parse_expiry("2026-12-05T10:00:00Z"), want)
        for bad in ("", None, "soon", "2026-13-05 10:00:00 UTC"):
            self.assertIsNone(schedule.parse_expiry(bad))

    def test_unknown_token_settings_are_refused(self):
        with self.assertRaises(schedule.ConfigError) as cm:
            self.cfg(token={"value": "x"})
        self.assertIn("never goes in this file", str(cm.exception))
        with self.assertRaises(schedule.ConfigError):
            self.cfg(token={"store": "vault"})


class TestTokenInRuns(Case):
    def run_with(self, api):
        path = streams_file(self.tmp)
        code, _out, _err = self.run_cli(["run", "--streams", path], http_get=api)
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            return code, path, json.load(fh)

    def test_the_run_records_the_expiry(self):
        code, _path, top = self.run_with(FakeAPI(expiry="2026-11-25 02:00:00 UTC"))
        self.assertEqual(code, 0)
        self.assertEqual(top["token_expires_at"], "2026-11-25T02:00:00Z")
        self.assertEqual(top["token_days_left"], 5.0)
        self.assertEqual(top["token"]["login"], "someone")
        self.assertNotIn(TOKEN, json.dumps(top))

    def test_status_latest_and_remind_warn_within_two_days(self):
        _code, path, _top = self.run_with(FakeAPI(expiry="2026-11-22 02:00:00 UTC"))
        code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertIn("TOKEN: the token expires 2026-11-22T02:00:00Z (2.0 day(s) left)", out)
        clock = FakeClock(start=T("2026-11-20T08:00:00Z"))
        lout, lerr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(lout), contextlib.redirect_stderr(lerr):
            schedule.main(["latest", "--streams", path, "--stream", "demo",
                           "--month", "2026-11"], now=clock.now)
        self.assertIn("token expires", json.loads(lout.getvalue())["warnings"][0])
        self.assertIn("latest: WARNING: the token expires", lerr.getvalue())
        code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertEqual(code, 0)
        self.assertIn("schedule.py token renew", out)
        self.assertNotIn('"', out)

    def test_no_reminder_with_days_to_spare(self):
        _code, path, _top = self.run_with(FakeAPI(expiry="2026-11-27 02:00:00 UTC"))
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertEqual(out, "")
        _code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertNotIn("TOKEN:", out)

    def test_a_refused_token_is_reminded(self):
        _code, path, top = self.run_with(FakeAPI(user=401))
        self.assertIn("refused", top["token"]["error"])
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertIn("refused", out)

    def test_an_unreachable_api_does_not_stop_the_run(self):
        code, _path, top = self.run_with(None)          # the module's no-network seam
        self.assertEqual(code, 0)
        self.assertIn("could not be reached", top["token"]["error"])
        self.assertIsNone(top["token_days_left"])


# --- the app route --------------------------------------------------------------------------------

APP_TOKEN_PREFIX = "ghs_" + "AppInst" * 5
KEY_TEXT = b"-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n"


class FakeAppAPI:
    """The app endpoints and the probe reads, scripted; records every call
    with the credential it carried and any body."""

    def __init__(self, installs=None, token_status=201, jwt_status=200, rest=200,
                 graphql=None, search=200, inst_status=200):
        self.calls = []
        self.installs = [{"id": 42, "app_id": 7, "account": {"login": "owner"},
                          "permissions": {"metadata": "read", "contents": "read",
                                          "pull_requests": "write"}}] \
            if installs is None else installs
        self.token_status, self.jwt_status, self.inst_status = (
            token_status, jwt_status, inst_status)
        self.rest, self.search = rest, search
        self.graphql = graphql if graphql is not None else {
            "data": {"repository": {"nameWithOwner": "Azure/bicep-registry-modules",
                                    "pullRequests": {"totalCount": 5000}},
                     "rateLimit": {"limit": 5000, "remaining": 4999}}}
        self.issued = []

    def __call__(self, method, url, bearer, body=None, timeout=30):
        self.calls.append((method, url, bearer, body))
        path = url[len(schedule.API):]
        if path.startswith("/app/"):
            if self.jwt_status != 200:
                return self.jwt_status, {}, json.dumps({"message": "'Expiration time' "
                                                        "claim ('exp') is too far"})
            if path == "/app/installations?per_page=100":
                return 200, {}, json.dumps(self.installs)
            if path.endswith("/access_tokens"):
                if self.token_status != 201:
                    return self.token_status, {}, json.dumps({"message": "denied"})
                token = "{}{:04d}".format(APP_TOKEN_PREFIX, len(self.issued))
                self.issued.append(token)
                return 201, {}, json.dumps({"token": token,
                                            "expires_at": "2026-11-20T03:00:00Z",
                                            "permissions": body["permissions"]})
            if self.inst_status != 200:
                return self.inst_status, {}, json.dumps({"message": "Not Found"})
            return 200, {}, json.dumps(self.installs[0])
        if path == "/rate_limit":
            return 200, {}, json.dumps({"resources": {
                "core": {"limit": 5000, "remaining": 4990},
                "graphql": {"limit": 5000, "remaining": 5000},
                "search": {"limit": 30, "remaining": 30}}})
        if path.startswith("/repos/"):
            if self.rest != 200:
                return self.rest, {}, json.dumps({"message": "Resource protected by "
                                                  "organization policy"})
            return 200, {}, json.dumps({"full_name": "Azure/bicep-registry-modules",
                                        "visibility": "public", "default_branch": "main"})
        if path == "/graphql":
            return 200, {}, json.dumps(self.graphql)
        if path.startswith("/search/"):
            return (200, {}, json.dumps({"total_count": 5000})) if self.search == 200 \
                else (self.search, {}, json.dumps({"message": "nope"}))
        return 404, {}, "{}"


def fake_sign(key, data):
    return b"signature-of-" + data[:8]


def make_key(folder):
    """A throwaway RSA key via openssl, mode 600; None when there is no openssl."""
    if not shutil.which("openssl"):
        return None
    path = os.path.join(folder, "app.pem")
    subprocess.run(["openssl", "genrsa", "-out", path, "2048"], check=True,
                   capture_output=True)
    os.chmod(path, 0o600)
    return path


class AppCase(Case):
    def setUp(self):
        super().setUp()
        self.keydir = os.path.join(self.tmp, "keys")
        os.makedirs(self.keydir, mode=0o700)
        self.key = os.path.join(self.keydir, "app.pem")
        with open(self.key, "w") as fh:
            fh.write(KEY_TEXT.decode())
        os.chmod(self.key, 0o600)

    def app_cfg(self, streams=None, **token):
        block = {"store": "app", "client_id": "Iv23testclient",
                 "private_key_file": self.key}
        block.update(token)
        return streams_file(self.tmp, streams, token=block)

    def settings(self, path, **extra):
        cfg = schedule.load_streams(path)
        s = schedule.token_settings(cfg)
        s.update(extra)
        return s


@unittest.skipUnless(shutil.which("openssl"), "openssl is not on PATH")
class TestAppJWT(Case):
    def verify(self, key, jwt):
        header, payload, sig = jwt.split(".")
        pub = os.path.join(self.tmp, "pub.pem")
        subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub],
                       check=True, capture_output=True)
        import base64
        data = os.path.join(self.tmp, "input")
        sigf = os.path.join(self.tmp, "sig")
        with open(data, "w") as fh:
            fh.write(header + "." + payload)
        with open(sigf, "wb") as fh:
            fh.write(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        proc = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub,
                               "-signature", sigf, data], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Verified OK", proc.stdout)

        def part(p):
            return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        return part(header), part(payload)

    def test_signed_with_a_real_key_and_verifiable(self):
        key = make_key(self.tmp)
        now = 1_800_000_000
        jwt = schedule.make_jwt("Iv23testclient", ("file", key), now)
        header, claims = self.verify(key, jwt)
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(claims, {"iat": now - 60, "exp": now + 540, "iss": "Iv23testclient"})
        self.assertLessEqual(claims["exp"] - now, 600)      # GitHub: at most 10 minutes

    def test_the_pkcs1_form_github_downloads(self):
        key = make_key(self.tmp)
        pkcs1 = os.path.join(self.tmp, "pkcs1.pem")
        proc = subprocess.run(["openssl", "rsa", "-in", key, "-traditional", "-out", pkcs1],
                              capture_output=True)
        if proc.returncode != 0:          # an openssl whose genrsa already writes PKCS#1
            pkcs1 = key
        os.chmod(pkcs1, 0o600)
        with open(pkcs1, "rb") as fh:
            head = fh.read(40)
        if b"BEGIN RSA PRIVATE KEY" not in head:
            self.skipTest("this openssl cannot write a PKCS#1 key")
        with open(pkcs1, "rb") as fh:
            content = fh.read()
        # the key is read once through the verified handle, then signed with
        # through the pipe (openssl never reopens the path)
        loaded = schedule.load_app_key({"private_key_file": pkcs1}, isdir=lambda p: False)
        self.assertEqual(loaded, ("pem", content))
        _header, claims = self.verify(pkcs1, schedule.make_jwt(
            "Iv23x", loaded, 1_800_000_000))
        self.assertEqual(claims["iss"], "Iv23x")
        _header, claims = self.verify(pkcs1, schedule.make_jwt(
            "Iv23x", ("file", pkcs1), 1_800_000_000))
        self.assertEqual(claims["iss"], "Iv23x")

    def test_key_through_a_pipe(self):
        key = make_key(self.tmp)
        with open(key, "rb") as fh:
            pem = fh.read()
        jwt = schedule.make_jwt(12345, ("pem", pem), 1_800_000_000)
        _header, claims = self.verify(key, jwt)
        self.assertEqual(claims["iss"], "12345")

    def test_a_bad_key_is_a_plain_error(self):
        bad = os.path.join(self.tmp, "bad.pem")
        with open(bad, "w") as fh:
            fh.write("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.make_jwt("x", ("file", bad), 1_800_000_000)
        self.assertIn("openssl could not sign", str(cm.exception))
        self.assertNotIn("AAAA", str(cm.exception))


class TestAppKey(AppCase):
    def test_file_checks(self):
        s = {"private_key_file": self.key, "workspace": self.ws}
        self.assertEqual(schedule.load_app_key(s, isdir=lambda p: False), ("pem", KEY_TEXT))
        os.chmod(self.key, 0o644)
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key(s, isdir=lambda p: False)
        self.assertIn("mode 644", str(cm.exception))
        os.chmod(self.key, 0o600)
        inside = os.path.join(self.ws, "app.pem")
        os.makedirs(self.ws)
        shutil.copy(self.key, inside)
        os.chmod(inside, 0o600)
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key({"private_key_file": inside, "workspace": self.ws},
                                  isdir=lambda p: False)
        self.assertIn("inside the workspace", str(cm.exception))
        synced = os.path.join(self.tmp, "Dropbox")
        os.makedirs(synced)
        shutil.copy(self.key, os.path.join(synced, "k.pem"))
        os.chmod(os.path.join(synced, "k.pem"), 0o600)
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key({"private_key_file": os.path.join(synced, "k.pem")},
                                  isdir=lambda p: False)
        self.assertIn("synced", str(cm.exception))
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key({"private_key_file": self.key + ".gone"})
        self.assertIn("does not exist", str(cm.exception))

    def test_keychain_item(self):
        import base64
        pem = b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
        sec = FakeSecurity()
        sec.items["app-key"] = base64.b64encode(pem).decode()
        s = {"private_key_keychain_service": "app-key", "account": "me"}
        self.assertEqual(schedule.load_app_key(s, sec), ("pem", pem))
        sec.items["app-key"] = "not base64 !"
        with self.assertRaises(schedule.AuthError):
            schedule.load_app_key(s, sec)
        with self.assertRaises(schedule.AuthError):
            schedule.load_app_key({"private_key_keychain_service": "missing",
                                   "account": "me"}, sec)

    def test_import_keeps_the_key_off_argv(self):
        path = self.app_cfg(private_key_keychain_service="app-key")
        sec = FakeSecurity()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "app-key", "--streams", path,
                                  "--import", self.key], security=sec)
        self.assertEqual(code, 0, err.getvalue())
        import base64
        with open(self.key, "rb") as fh:
            self.assertEqual(base64.b64decode(sec.items["app-key"]), fh.read())
        for argv, _stdin in sec.calls:
            self.assertNotIn(sec.items["app-key"], " ".join(argv))
        self.assertNotIn("not a real key", out.getvalue() + err.getvalue())

    def test_streams_file_rules(self):
        with self.assertRaises(schedule.ConfigError) as cm:
            self.cfg(token={"store": "app", "private_key_file": self.key})
        self.assertIn("client_id", str(cm.exception))
        with self.assertRaises(schedule.ConfigError) as cm:
            self.cfg(token={"store": "app", "client_id": "x"})
        self.assertIn("private_key", str(cm.exception))
        with self.assertRaises(schedule.ConfigError) as cm:
            self.cfg(token={"store": "app", "client_id": "x",
                            "private_key_file": "-----BEGIN PRIVATE KEY-----"})
        self.assertIn("never goes in this file", str(cm.exception))
        with self.assertRaises(schedule.ConfigError):
            self.cfg(token={"store": "app", "app_id": "7", "private_key_file": self.key})


class TestAppMint(AppCase):
    def tokens(self, api, **extra):
        return schedule.AppTokens(self.settings(self.app_cfg(), **extra), api=api,
                                  sign=fake_sign, isdir=lambda p: False,
                                  now=FakeClock().now)

    def test_mint_finds_the_installation_and_asks_for_read_only(self):
        import base64
        api = FakeAppAPI()
        info = self.tokens(api).mint()
        self.assertEqual(info["token"], APP_TOKEN_PREFIX + "0000")
        self.assertEqual((info["installation_id"], info["account"], info["app_id"]),
                         (42, "owner", 7))
        post = [c for c in api.calls if c[0] == "POST"][0]
        self.assertEqual(post[3], {"permissions": {"contents": "read", "metadata": "read",
                                                   "pull_requests": "read"}})
        jwt = post[2]
        self.assertEqual(jwt.count("."), 2)
        part = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        self.assertEqual(claims["iss"], "Iv23testclient")
        self.assertEqual(claims["iat"], int(T("2026-11-20T02:00:00Z").timestamp()) - 60)
        self.assertNotIn("token", schedule.AppTokens.public(info))

    def test_configured_installation(self):
        api = FakeAppAPI()
        self.tokens(api, installation_id=42).mint()
        self.assertEqual(api.calls[0][1], schedule.API + "/app/installations/42")
        with self.assertRaises(schedule.AuthError) as cm:
            self.tokens(FakeAppAPI(inst_status=404), installation_id=9).mint()
        self.assertIn("installation 9 was not found", str(cm.exception))

    def test_plain_errors(self):
        cases = [(FakeAppAPI(installs=[]), "not installed anywhere"),
                 (FakeAppAPI(installs=[{"id": 1, "account": {"login": "a"}},
                                       {"id": 2, "account": {"login": "b"}}]),
                  "2 installations (1 on a, 2 on b); set token.installation_id"),
                 (FakeAppAPI(jwt_status=401), "refused the app's JWT (401"),
                 (FakeAppAPI(token_status=403), "refused the installation token (403")]
        for api, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(schedule.AuthError) as cm:
                    self.tokens(api).mint()
                self.assertIn(needle, str(cm.exception))
                if "JWT" in needle:
                    self.assertIn("the key and the clock", str(cm.exception))


class TestAppRuns(AppCase):
    def run_app(self, runner, api=None, streams=None, clock=None, **token):
        path = self.app_cfg(streams, **token)
        code, out, err = self.run_cli(["run", "--streams", path], runner=runner,
                                      api=api or FakeAppAPI(), sign=fake_sign,
                                      clock=clock, env={"PATH": "/bin"})
        return code, path

    def all_text(self):
        text = ""
        for root, _dirs, names in os.walk(self.ws):
            for n in names:
                if n.endswith((".json", ".log")):
                    with open(os.path.join(root, n), encoding="utf-8") as fh:
                        text += fh.read()
        return text

    def test_a_fresh_token_per_gather_and_no_secret_written(self):
        runner = Runner()
        api = FakeAppAPI()
        code, path = self.run_app(runner, api, streams=[
            {"id": "demo", "kind": "repo", "owner": "o", "repo": "r",
             "cadence": ["weekly", "monthly"]}])
        self.assertEqual(code, 0)
        gather_tokens = [env["GITHUB_TOKEN"] for argv, env in runner.calls
                         if argv[1].endswith("gather.py")]
        self.assertEqual(len(gather_tokens), 2)                 # carried days + month
        self.assertEqual(len(set(gather_tokens)), 2)            # one token each
        self.assertTrue(all(t.startswith(APP_TOKEN_PREFIX) for t in gather_tokens))
        for argv, env in runner.calls:
            self.assertNotIn("GH_TOKEN", env)
            if argv[1].endswith("validate.py"):
                self.assertNotIn("GITHUB_TOKEN", env)
        text = self.all_text()
        for token in api.issued:
            self.assertNotIn(token, text)
        self.assertNotIn("not a real key", text)
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            top = json.load(fh)
        self.assertEqual(top["token_kind"], "app")
        self.assertEqual((top["token"]["installation_id"], top["token"]["account"]),
                         (42, "owner"))
        self.assertEqual(top["token_expires_at"], "2026-11-20T03:00:00Z")
        self.assertIsNone(top["token_days_left"])
        attempt = self.status()["windows"]["2026-11"]["last_success"]
        self.assertEqual(attempt["token"]["minted"], 2)
        code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertNotIn("TOKEN:", out)                         # no expiry warning
        self.assertIn("installation token minted", out)
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertEqual(out, "")

    def test_no_key_is_a_plain_auth_error(self):
        os.remove(self.key)
        runner = Runner()
        code, path = self.run_app(runner)
        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [])
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertEqual(attempt["result"], "token_failed")
        self.assertIn("private key file", attempt["auth_error"])
        _code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertIn("TOKEN: the app could not mint a token", out)
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertIn("app-check", out)

    def test_refused_installation_token(self):
        code, _path = self.run_app(Runner(), FakeAppAPI(token_status=403))
        self.assertEqual(code, 1)
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertIn("refused the installation token (403", attempt["auth_error"])

    def manifest_streams(self):
        os.makedirs(os.path.join(self.ws, "tf"), exist_ok=True)
        with open(os.path.join(self.ws, "tf", "manifest.json"), "w") as fh:
            json.dump({"project": "p", "window": {"from": "2026-11-01", "to": "2026-11-30"},
                       "repos": [{"owner": "o", "repo": n} for n in ("a", "b", "c")]}, fh)
        return [{"id": "tf", "kind": "manifest", "manifest": "tf/manifest.json"}]

    def scripted(self, *results):
        results = list(results)

        def gather(argv):
            failed = results.pop(0)
            if failed:
                return 1, "", "member o/a failed: x\ngather: {} of 3 member(s) failed " \
                              "for project 'p' (2026-11-01..2026-11-30):\n".format(failed)
            return 0, "", ""
        return gather

    def test_a_manifest_is_resumed_with_fresh_tokens_while_it_progresses(self):
        runner = Runner(gather=self.scripted(2, 1, 0))
        code, _path = self.run_app(runner, streams=self.manifest_streams())
        self.assertEqual(code, 0)
        gathers = [(a, e) for a, e in runner.calls if a[1].endswith("gather.py")]
        self.assertEqual(["--resume" in a for a, _e in gathers], [False, True, True])
        self.assertEqual(len({e["GITHUB_TOKEN"] for _a, e in gathers}), 3)
        parts = [g["part"] for g in
                 self.status("tf")["windows"]["2026-11"]["last_success"]["gathers"]]
        self.assertEqual(parts, ["month", "month (resume 1)", "month (resume 2)"])

    def test_no_progress_stops(self):
        runner = Runner(gather=self.scripted(2, 2))
        code, _path = self.run_app(runner, streams=self.manifest_streams())
        self.assertEqual(code, 1)
        self.assertEqual(len([a for a, _e in runner.calls if a[1].endswith("gather.py")]), 2)

    def test_the_personal_token_path_does_not_resume(self):
        runner = Runner(gather=self.scripted(2, 1, 0))
        path = streams_file(self.tmp, self.manifest_streams())
        code, _out, _err = self.run_cli(["run", "--streams", path], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(len([a for a, _e in runner.calls if a[1].endswith("gather.py")]), 1)

    def test_a_gather_that_outlives_the_hour_says_so(self):
        clock = FakeClock()

        def slow(argv):
            clock.mono += 61 * 60
            return 1, "", ("error: GitHub API 401 for https://api.github.com/x\n"
                           "  message: Bad credentials\n")
        self.run_app(Runner(gather=slow), clock=clock)
        attempt = self.status()["windows"]["2026-11"]["last_attempt"]
        self.assertIn("401", attempt["auth_error"])
        self.assertIn("longer than the installation token's hour", attempt["auth_error"])


class TestAppCheck(AppCase):
    def check(self, api):
        path = self.app_cfg(probe_repo="Azure/bicep-registry-modules")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "app-check", "--streams", path], api=api,
                                 sign=fake_sign, now=FakeClock().now)
        text = out.getvalue() + err.getvalue()
        for token in api.issued:
            self.assertNotIn(token, text)
        return code, out.getvalue()

    def test_usable(self):
        code, out = self.check(FakeAppAPI())
        self.assertEqual(code, 0, out)
        self.assertIn("installation token: minted for app Iv23testclient installation 42 "
                      "on owner", out)
        self.assertIn("rate limit (REST GET /rate_limit): OK - HTTP 200; core 4990/5000", out)
        self.assertIn("REST read (GET /repos/Azure/bicep-registry-modules): OK", out)
        self.assertIn("GraphQL read (repository Azure/bicep-registry-modules): OK", out)
        self.assertIn("5000 merged PRs", out)
        self.assertIn("search (GET /search/issues, newcomers check only): OK", out)
        self.assertIn("RESULT: USABLE", out)

    def test_refused_by_rest_or_graphql(self):
        code, out = self.check(FakeAppAPI(rest=403))
        self.assertEqual(code, 1)
        self.assertIn("FAILED - HTTP 403; Resource protected by organization policy", out)
        self.assertIn("RESULT: NOT USABLE", out)
        code, out = self.check(FakeAppAPI(graphql={"errors": [
            {"message": "Although you appear to have the correct authorization "
                        "credentials, the organization has enabled OAuth App access "
                        "restrictions"}]}))
        self.assertEqual(code, 1)
        self.assertIn("OAuth App access restrictions", out)

    def test_search_alone_does_not_decide(self):
        code, out = self.check(FakeAppAPI(search=403))
        self.assertEqual(code, 0)
        self.assertIn("search (GET /search/issues, newcomers check only): FAILED", out)

    def test_no_token(self):
        code, out = self.check(FakeAppAPI(installs=[]))
        self.assertEqual(code, 1)
        self.assertIn("installation token: FAILED - the app is not installed anywhere", out)

    def test_kind_and_renew_with_the_app_route(self):
        path = self.app_cfg()
        code, out, _err = self.run_cli(["token", "kind", "--streams", path])
        self.assertEqual(out, "app\n")
        code, _out, err = self.run_cli(["token", "renew", "--streams", path],
                                       prompt=lambda t: NEW_TOKEN)
        self.assertEqual(code, 2)
        self.assertIn("needs no weekly renewal", err)


class TestReviewFindings(AppCase):
    """PR review: the advice names the route in use, readers recompute the
    days left from the expiry, and a bare env-file name works."""

    def test_a_later_app_mint_failure_points_to_app_check(self):
        mints = {"n": 0}
        api = FakeAppAPI()
        real = api.__call__

        def flaky(method, url, bearer, body=None, timeout=30):
            if url.endswith("/access_tokens"):
                mints["n"] += 1
                if mints["n"] > 1:              # the run's check succeeds, the gather's fails
                    return 403, {}, json.dumps({"message": "denied"})
            return real(method, url, bearer, body, timeout)
        path = self.app_cfg()
        code, _out, _err = self.run_cli(["run", "--streams", path], api=flaky,
                                        sign=fake_sign, env={"PATH": "/bin"})
        self.assertEqual(code, 1)
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            top = json.load(fh)
        self.assertIsNone(top["token"]["error"])
        self.assertIn("installation token", top["streams"]["demo"]["auth_error"])
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertIn("token app-check", out)
        self.assertNotIn("token renew", out)
        _code, out, _err = self.run_cli(["status", "--streams", path])
        self.assertIn("token app-check", out)
        self.assertNotIn("Keeping the token valid", out)

    def test_the_personal_route_still_points_to_renew(self):
        path = streams_file(self.tmp)
        self.run_cli(["run", "--streams", path],
                     runner=Runner(gather=lambda a: (1, "", LIFETIME_403)))
        _code, out, _err = self.run_cli(["token", "remind", "--streams", path])
        self.assertIn("token renew", out)

    def test_readers_recompute_days_left_from_the_expiry(self):
        path = streams_file(self.tmp)
        # the last run, on 2026-11-20, saw 5 days left ...
        self.run_cli(["run", "--streams", path],
                     http_get=FakeAPI(expiry="2026-11-25 02:00:00 UTC"))
        with open(os.path.join(self.ws, "status.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["token_days_left"], 5.0)
        # ... then runs stopped; on 2026-11-24 a day is left, on 2026-11-26 none
        for when, needle in (("2026-11-24T02:00:00Z", "(1.0 day(s) left)"),
                             ("2026-11-26T02:00:00Z", "expired 2026-11-25T02:00:00Z")):
            clock = FakeClock(start=T(when))
            with self.subTest(when=when):
                _code, out, _err = self.run_cli(["status", "--streams", path], clock=clock)
                self.assertIn(needle, out)
                _code, out, _err = self.run_cli(["token", "remind", "--streams", path],
                                                clock=clock)
                self.assertIn(needle, out)
                lout = io.StringIO()
                with contextlib.redirect_stdout(lout), \
                        contextlib.redirect_stderr(io.StringIO()):
                    schedule.main(["latest", "--streams", path, "--stream", "demo",
                                   "--month", "2026-11", "--accept-stale"], now=clock.now)
                self.assertIn(needle, json.loads(lout.getvalue())["warnings"][0])
        _code, out, _err = self.run_cli(["status", "--streams", path])   # 2026-11-20
        self.assertNotIn("TOKEN:", out)

    def test_a_bare_env_file_name(self):
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        schedule.write_env_file("token.env", NEW_TOKEN)
        with open(os.path.join(self.tmp, "token.env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "GITHUB_TOKEN={}\n".format(NEW_TOKEN))
        self.assertEqual(os.stat("token.env").st_mode & 0o777, 0o600)
        path = streams_file(self.tmp)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "renew", "--streams", path, "--store", "env-file",
                                  "--env-file", "renewed.env", "--no-browser"],
                                 http_get=FakeAPI(), prompt=lambda t: NEW_TOKEN,
                                 now=FakeClock().now)
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "renewed.env")))


class FlakySecurity(FakeSecurity):
    """A keychain whose writes go wrong: the first `bad_writes` puts store a
    garbled value (or, with `fail_exit`, exit non-zero and store nothing)."""

    def __init__(self, stored=None, bad_writes=1, fail_exit=False):
        super().__init__(stored=stored)
        self.bad_writes, self.fail_exit = bad_writes, fail_exit

    def __call__(self, argv, stdin_text=None):
        if argv == ["security", "-i"] and self.bad_writes:
            self.bad_writes -= 1
            self.calls.append((list(argv), stdin_text))
            if self.fail_exit:
                return 1, ""
            self.items[stdin_text.split()[stdin_text.split().index("-s") + 1]] = "garbled"
            return 0, ""
        return super().__call__(argv, stdin_text)


class TestKeychainWriteFailures(TokenCliCase):
    """Review: a keychain read-back failure must not claim the old token is
    still there — `-U` has already replaced it."""

    def setUp(self):
        super().setUp()
        self.path = streams_file(self.tmp, token={"store": "keychain"})

    def test_the_previous_token_is_written_back(self):
        sec = FlakySecurity(stored=OLD_TOKEN)
        code, _out, err = self.token_cli("renew", security=sec)
        self.assertEqual(code, 1)
        self.assertEqual(sec.items["activity-gh-token"], OLD_TOKEN)
        self.assertIn("the previous value was written back and reads back correctly", err)
        self.assertNotIn("stays in place", err.split("storing failed")[-1])
        for argv, _stdin in sec.calls:
            self.assertNotIn(OLD_TOKEN, " ".join(argv))
            self.assertNotIn(NEW_TOKEN, " ".join(argv))

    def test_when_restoring_fails_too_it_says_to_check(self):
        sec = FlakySecurity(stored=OLD_TOKEN, bad_writes=2)
        code, _out, err = self.token_cli("renew", security=sec)
        self.assertEqual(code, 1)
        self.assertIn("may now hold a different or partial value: check it", err)
        self.assertNotIn("stays in place", err.split("storing failed")[-1])

    def test_a_first_store_that_fails(self):
        code, _out, err = self.token_cli("renew", security=FlakySecurity())
        self.assertEqual(code, 1)
        self.assertIn("check it", err)
        self.assertNotIn("stays in place", err.split("storing failed")[-1])

    def test_a_refused_write_that_changed_nothing(self):
        sec = FlakySecurity(stored=OLD_TOKEN, fail_exit=True)
        code, _out, err = self.token_cli("renew", security=sec)
        self.assertEqual(code, 1)
        self.assertEqual(sec.items["activity-gh-token"], OLD_TOKEN)
        self.assertIn("security could not store it (exit 1); the item still holds its "
                      "previous value", err)

    def test_app_key_import_restores_the_previous_key_too(self):
        import base64
        key = os.path.join(self.tmp, "k.pem")
        with open(key, "w") as fh:
            fh.write("-----BEGIN RSA PRIVATE KEY-----\nnew\n-----END RSA PRIVATE KEY-----\n")
        old = base64.b64encode(b"-----BEGIN RSA PRIVATE KEY-----\nold\n").decode()
        sec = FlakySecurity()
        sec.items["app-key"] = old
        self.path = streams_file(self.tmp, token={
            "store": "app", "client_id": "x", "private_key_keychain_service": "app-key"})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "app-key", "--streams", self.path,
                                  "--import", key], security=sec)
        self.assertEqual(code, 1)
        self.assertEqual(sec.items["app-key"], old)
        self.assertIn("previous value was written back", err.getvalue())


class TestTokenWhere(Case):
    def where(self, path, field):
        code, out, _err = self.run_cli(["token", "where", "--streams", path,
                                        "--field", field])
        self.assertEqual(code, 0)
        return out.strip()

    def test_the_settings_renewal_uses(self):
        custom = os.path.join(self.tmp, "secrets", "gh.env")
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": custom,
                                             "keychain_service": "my-gh"})
        self.assertEqual(self.where(path, "store"), "env-file")
        self.assertEqual(self.where(path, "env-file"), custom)
        self.assertEqual(self.where(path, "keychain-service"), "my-gh")
        cfg = schedule.load_streams(path)
        self.assertEqual(self.where(path, "account"),
                         schedule.token_settings(cfg)["account"])

    def test_relative_paths_are_resolved_once(self):
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": "token.env",
                                             "private_key_file": "../keys/app.pem"})
        cfg = schedule.load_streams(path)
        s = schedule.token_settings(cfg)
        self.assertEqual(s["env_file"], os.path.join(self.ws, "token.env"))
        self.assertEqual(s["private_key_file"], os.path.join(self.tmp, "keys", "app.pem"))
        self.assertEqual(self.where(path, "env-file"), os.path.join(self.ws, "token.env"))
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)
        args = mock.Mock(store=None, env_file="cli.env")
        self.assertEqual(schedule.token_settings(cfg, args)["env_file"],
                         os.path.join(self.tmp, "cli.env"))

    def test_a_token_block_inside_a_stream_is_refused(self):
        """Review (low): `token` is read only at the top level; inside a stream
        it was silently ignored and the default store used."""
        path = streams_file(self.tmp, [{"id": "demo", "kind": "repo", "owner": "o",
                                        "repo": "r", "token": {"store": "env-file"}}])
        code, _out, err = self.run_cli(["check", "--streams", path])
        self.assertEqual(code, 2)
        self.assertIn("'token' is a top-level setting", err)
        code, _out, err = self.run_cli(["run", "--streams", path, "--dry-run"])
        self.assertEqual(code, 2)

    def test_renewing_somewhere_the_wrappers_do_not_read_is_flagged(self):
        env_file = os.path.join(self.tmp, "cfg", "token.env")
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": env_file})
        for extra, flagged in (([], False),
                               (["--env-file", os.path.join(self.tmp, "other.env")], True),
                               (["--store", "keychain"], True)):
            with self.subTest(extra=extra):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = schedule.main(["token", "renew", "--streams", path,
                                          "--no-browser", *extra],
                                         http_get=FakeAPI(), prompt=lambda t: NEW_TOKEN,
                                         security=FakeSecurity(), now=FakeClock().now)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual("the wrappers" in err.getvalue(), flagged, err.getvalue())


WRAPPER_SHIM = """\
import os, sys
sys.path.insert(0, {skill!r})
if sys.argv[1] == "token":
    import schedule
    sys.exit(schedule.main(sys.argv[1:]))
if sys.argv[1] == "run":
    with open(os.environ["AO_SEEN"], "w") as fh:
        fh.write(os.environ.get("GITHUB_TOKEN", "<none>"))
"""

FAKE_SECURITY = """#!/bin/sh
# find-generic-password -a ACCT -s SVC -w: the value for exactly that item
[ "$1" = find-generic-password ] || exit 1
[ "$3" = "{acct}" ] && [ "$5" = "{svc}" ] && {{ echo "{value}"; exit 0; }}
exit 44
"""


@unittest.skipUnless(shutil.which("sh") and shutil.which("git"), "needs sh and git")
class TestWrappersFollowTheSettings(Case):
    """Review: both wrappers find the token where the streams file says (the
    settings `token renew` writes to), not at a fixed path or item name."""

    VALUE = "fakevalue123456789"

    def wrapper(self, which, token_block):
        skill = os.path.join(self.tmp, "skill")
        os.makedirs(skill)
        with open(os.path.join(skill, "schedule.py"), "w") as fh:
            fh.write(WRAPPER_SHIM.format(skill=HERE))
        os.makedirs(self.ws)
        path = streams_file(self.tmp, token=token_block)
        with open(os.path.join(HERE, "examples", which, "activity-gather.sh")) as fh:
            text = fh.read().replace("__SKILL_DIR__", skill)
        text = text.replace("__WORKSPACE__/streams.json", path)
        script = os.path.join(self.tmp, "w.sh")
        with open(script, "w") as fh:
            fh.write(text)
        return script

    def run_wrapper(self, script, extra_path=None, first_path=None, cwd=None):
        seen = os.path.join(self.tmp, "seen")
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        env.update(HOME=self.tmp, AO_SEEN=seen, NOTIFY="0",
                   USER=env.get("USER") or "tester")
        if extra_path:
            env["PATH"] = env.get("PATH", "") + os.pathsep + extra_path
        if first_path:
            env["PATH"] = first_path + os.pathsep + env.get("PATH", "")
        if os.path.exists(seen):
            os.remove(seen)
        proc = subprocess.run(["sh", script], env=env, capture_output=True, text=True,
                              cwd=cwd or self.tmp)
        got = None
        if os.path.exists(seen):
            with open(seen) as fh:
                got = fh.read()
        return proc, got

    def test_env_file_from_the_streams_file(self):
        custom = os.path.join(self.tmp, "secrets", "gh.env")
        schedule.write_env_file(custom, self.VALUE)
        for which in ("cron", "launchd"):
            with self.subTest(wrapper=which):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(os.path.join(self.tmp, "skill"), ignore_errors=True)
                script = self.wrapper(which, {"store": "env-file", "env_file": custom})
                proc, got = self.run_wrapper(script)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(got, self.VALUE)

    @unittest.skipIf(os.path.exists("/usr/bin/security"),
                     "a real keychain tool is on this machine")
    def test_keychain_item_from_the_streams_file(self):
        acct = os.environ.get("USER") or "tester"
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "security")
        with open(fake, "w") as fh:
            fh.write(FAKE_SECURITY.format(acct=acct, svc="my-gh", value=self.VALUE))
        os.chmod(fake, 0o755)
        for which in ("cron", "launchd"):
            with self.subTest(wrapper=which):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(os.path.join(self.tmp, "skill"), ignore_errors=True)
                script = self.wrapper(which, {"store": "keychain",
                                              "keychain_service": "my-gh"})
                proc, got = self.run_wrapper(script, extra_path=bindir)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(got, self.VALUE)

    def test_the_app_route_reads_nothing(self):
        for which in ("cron", "launchd"):
            with self.subTest(wrapper=which):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(os.path.join(self.tmp, "skill"), ignore_errors=True)
                script = self.wrapper(which, {"store": "app", "client_id": "x",
                                              "private_key_file": "~/k.pem"})
                proc, got = self.run_wrapper(script)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(got, "<none>")

    def test_a_bare_env_file_name_is_the_checked_file_not_one_on_path(self):
        """Review (medium): `token.env` passed the mode check in one place
        while POSIX `.` searched PATH for it. The streams file's relative
        path now means the workspace's file, and the wrapper reads exactly
        the file it checked, wherever it runs and whatever is on PATH."""
        decoy_dir = os.path.join(self.tmp, "decoy")
        os.makedirs(decoy_dir)
        with open(os.path.join(decoy_dir, "token.env"), "w") as fh:
            fh.write("GITHUB_TOKEN=decoyvalue000000000\n")
        os.chmod(os.path.join(decoy_dir, "token.env"), 0o600)
        for which in ("cron", "launchd"):
            for cwd in ("tmp", "workspace"):
                with self.subTest(wrapper=which, cwd=cwd):
                    shutil.rmtree(self.ws, ignore_errors=True)
                    shutil.rmtree(os.path.join(self.tmp, "skill"), ignore_errors=True)
                    script = self.wrapper(which, {"store": "env-file",
                                                  "env_file": "token.env"})
                    schedule.write_env_file(os.path.join(self.ws, "token.env"), self.VALUE)
                    proc, got = self.run_wrapper(
                        script, first_path=decoy_dir,
                        cwd=self.tmp if cwd == "tmp" else self.ws)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(got, self.VALUE)

    def test_a_writable_token_folder_stops_both_wrappers(self):
        folder = os.path.join(self.tmp, "loose")
        target = os.path.join(folder, "gh.env")
        schedule.write_env_file(target, self.VALUE)
        os.chmod(folder, 0o777)
        self.addCleanup(os.chmod, folder, 0o700)
        for which in ("cron", "launchd"):
            with self.subTest(wrapper=which):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(os.path.join(self.tmp, "skill"), ignore_errors=True)
                script = self.wrapper(which, {"store": "env-file", "env_file": target})
                proc, got = self.run_wrapper(script)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("chmod 700 " + folder, proc.stderr)
                self.assertIsNone(got)                   # nothing sourced, nothing run

    def test_a_missing_env_file_stops_with_its_name(self):
        missing = os.path.join(self.tmp, "nowhere", "gh.env")
        script = self.wrapper("cron", {"store": "env-file", "env_file": missing})
        proc, got = self.run_wrapper(script)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(missing, proc.stderr)
        self.assertIsNone(got)


class SecretFolderCase(AppCase):
    """The folder holding the token file or the app key: tightened on write
    when it is the user's own private folder, refused on every path when
    group/other-writable, warned about when only group/other-readable, and
    never chmod-ed when shared or not the user's."""

    def folder(self, name, mode):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, mode)
        return d

    def mode(self, d):
        return os.stat(d).st_mode & 0o777

    def renew(self, env_file, **token):
        path = streams_file(self.tmp, token=dict({"store": "env-file",
                                                  "env_file": env_file}, **token))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "renew", "--streams", path, "--no-browser"],
                                 http_get=FakeAPI(), prompt=lambda t: NEW_TOKEN,
                                 now=FakeClock().now)
        return code, err.getvalue()


class TestSecretFolders(SecretFolderCase):
    # --- write paths --------------------------------------------------------

    def test_a_new_folder_is_created_private(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        target = os.path.join(self.tmp, "new", "token.env")
        self.assertEqual(schedule.write_env_file(target, NEW_TOKEN), [])
        self.assertEqual(self.mode(os.path.dirname(target)), 0o700)

    def test_an_own_loose_folder_is_tightened_and_said(self):
        for start in (0o755, 0o750, 0o775, 0o777):
            with self.subTest(mode=oct(start)):
                d = self.folder("cfg", start)
                code, err = self.renew(os.path.join(d, "token.env"))
                self.assertEqual(code, 0, err)
                self.assertEqual(self.mode(d), 0o700)
                self.assertIn("tightened {} from mode {:o} to 700".format(d, start), err)

    def test_a_private_folder_is_left_alone_quietly(self):
        d = self.folder("cfg", 0o700)
        code, err = self.renew(os.path.join(d, "token.env"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("tightened", err)

    def test_a_shared_folder_is_never_chmodded(self):
        # the workspace: readable -> warned and written; writable -> refused
        os.makedirs(self.ws)
        os.chmod(self.ws, 0o755)
        code, err = self.renew("token.env")                 # relative: the workspace
        self.assertEqual(code, 0, err)
        self.assertEqual(self.mode(self.ws), 0o755)
        self.assertIn("is a shared location, so its mode is not changed", err)
        os.chmod(self.ws, 0o777)
        self.addCleanup(os.chmod, self.ws, 0o755)
        with open(os.path.join(self.ws, "token.env"), encoding="utf-8") as fh:
            before = fh.read()
        code, err = self.renew("token.env")
        self.assertEqual(code, 1)
        self.assertEqual(self.mode(self.ws), 0o777)
        self.assertIn("writable by group or others (mode 777)", err)
        self.assertIn("old token stays in place", err)
        with open(os.path.join(self.ws, "token.env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_the_home_folder_and_temp_dirs_count_as_shared(self):
        home = self.folder("home", 0o755)
        with mock.patch.dict(os.environ, {"HOME": home}):
            notes = schedule.secure_secret_dir(home)
            self.assertEqual(self.mode(home), 0o755)
            self.assertIn("shared location", notes[0])
            import tempfile
            shared = schedule._shared_locations()
            self.assertIn(os.path.realpath(tempfile.gettempdir()), shared)
            self.assertIn(os.path.realpath("/tmp"), shared)

    def test_a_folder_that_is_not_ours_is_never_chmodded(self):
        readable = self.folder("theirs", 0o755)
        writable = self.folder("theirs-w", 0o777)
        with mock.patch.object(schedule.os, "getuid", lambda: os.stat(readable).st_uid + 1):
            notes = schedule.secure_secret_dir(readable)
            self.assertIn("not your own folder", notes[0])
            self.assertEqual(self.mode(readable), 0o755)
            with self.assertRaises(schedule.SecretDirError) as cm:
                schedule.secure_secret_dir(writable)
            self.assertIn("put the file in a private folder", str(cm.exception))
            self.assertEqual(self.mode(writable), 0o777)

    def test_app_key_import_tightens_the_key_folder(self):
        d = self.folder("keys2", 0o755)
        key = os.path.join(d, "app.pem")
        shutil.copy(self.key, key)
        os.chmod(key, 0o600)
        path = self.app_cfg(private_key_keychain_service="app-key")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = schedule.main(["token", "app-key", "--streams", path, "--import", key],
                                 security=FakeSecurity())
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.mode(d), 0o700)
        self.assertIn("tightened", err.getvalue())

    # --- read paths -----------------------------------------------------------

    def test_check_secret_dir(self):
        self.assertIsNone(schedule.check_secret_dir(self.folder("a", 0o700)))
        self.assertIn("readable by group or others",
                      schedule.check_secret_dir(self.folder("b", 0o750)))
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                d = self.folder("c", mode)
                with self.assertRaises(schedule.SecretDirError) as cm:
                    schedule.check_secret_dir(d)
                self.assertIn("chmod 700 {}".format(d), str(cm.exception))
                self.assertEqual(self.mode(d), mode)        # a read never chmods

    def test_the_app_key_folder(self):
        os.chmod(self.keydir, 0o777)
        s = {"private_key_file": self.key, "workspace": self.ws}
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key(s, isdir=lambda p: False)
        self.assertIn("writable by group or others", str(cm.exception))
        self.assertIn("chmod 700 " + self.keydir, str(cm.exception))
        os.chmod(self.keydir, 0o755)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(schedule.load_app_key(s, isdir=lambda p: False),
                             ("pem", KEY_TEXT))
        self.assertIn("readable by group or others", err.getvalue())
        self.assertEqual(self.mode(self.keydir), 0o755)
        # app-check stops before any call with the folder writable
        os.chmod(self.keydir, 0o777)
        api = FakeAppAPI()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = schedule.main(["token", "app-check", "--streams", self.app_cfg()],
                                 api=api, sign=fake_sign, now=FakeClock().now)
        self.assertEqual(code, 1)
        self.assertEqual(api.calls, [])
        self.assertIn("writable by group or others", out.getvalue())

    def test_token_status_and_where_on_the_env_file(self):
        d = self.folder("cfg", 0o700)
        target = os.path.join(d, "token.env")
        schedule.write_env_file(target, NEW_TOKEN)
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": target})
        os.chmod(d, 0o777)
        code, out, _err = self.run_cli(["token", "status", "--streams", path],
                                       http_get=FakeAPI())
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        code, out, err = self.run_cli(["token", "where", "--streams", path,
                                       "--field", "env-file"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("chmod 700 " + d, err)
        os.chmod(d, 0o755)
        code, out, _err = self.run_cli(["token", "status", "--streams", path],
                                       http_get=FakeAPI())
        self.assertEqual(code, 0)
        self.assertIn("WARNING: the token's folder {} is readable".format(d), out)
        code, out, err = self.run_cli(["token", "where", "--streams", path,
                                       "--field", "env-file"])
        self.assertEqual((code, out.strip()), (0, target))
        self.assertIn("readable by group or others", err)
        self.assertEqual(self.mode(d), 0o755)               # reads never chmod


FOREIGN_UID = 54321
AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


class TestSecretChain(SecretFolderCase):
    """Review (HIGH): the whole path to a secret is checked, not only its
    folder. A mode-700 /shared/private under a world-writable, non-sticky
    /shared could be renamed away and replaced with someone else's folder
    and token. Real temporary directories; foreign ownership by chown
    (root only), sticky directories, symlinks."""

    def setUp(self):
        super().setUp()
        self.shared = self.folder("shared", 0o777)       # world-writable, NOT sticky
        self.private = os.path.join(self.shared, "private")
        os.makedirs(self.private)
        os.chmod(self.private, 0o700)
        self.addCleanup(os.chmod, self.shared, 0o700)

    def put_token(self, folder):
        target = os.path.join(folder, "token.env")
        with open(target, "w") as fh:
            fh.write("GITHUB_TOKEN={}\n".format(NEW_TOKEN))
        os.chmod(target, 0o600)
        return target

    def test_the_finding_a_private_folder_under_a_writable_parent(self):
        target = self.put_token(self.private)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.check_secret_dir(self.private)
        msg = str(cm.exception)
        self.assertIn("{} is writable by group or others (mode 777) without the sticky "
                      "bit".format(self.shared), msg)
        self.assertIn("chmod go-w " + self.shared, msg)
        # every path refuses: where (the wrappers), status, renew (a write)
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": target})
        code, out, err = self.run_cli(["token", "where", "--streams", path,
                                       "--field", "env-file"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("without the sticky bit", err)
        code, out, _err = self.run_cli(["token", "status", "--streams", path],
                                       http_get=FakeAPI())
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        with open(target) as fh:
            before = fh.read()
        code, err = self.renew(target)
        self.assertEqual(code, 1)
        self.assertIn("without the sticky bit", err)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.mode(self.shared), 0o777)       # ancestors never chmod-ed
        # the app key the same way
        key = os.path.join(self.private, "app.pem")
        with open(key, "wb") as fh:
            fh.write(KEY_TEXT)
        os.chmod(key, 0o600)
        with self.assertRaises(schedule.AuthError) as cm:
            schedule.load_app_key({"private_key_file": key}, isdir=lambda p: False)
        self.assertIn("without the sticky bit", str(cm.exception))

    def test_a_missing_folder_under_a_writable_parent_is_not_created(self):
        target = os.path.join(self.shared, "new", "token.env")
        code, err = self.renew(target)
        self.assertEqual(code, 1)
        self.assertIn("without the sticky bit", err)
        self.assertFalse(os.path.exists(os.path.join(self.shared, "new")))

    def test_the_sticky_exception(self):
        os.chmod(self.shared, 0o1777)                         # like /tmp
        target = self.put_token(self.private)
        self.assertIsNone(schedule.check_secret_dir(self.private))
        real, st = schedule.verify_secret_chain(target)
        self.assertEqual(real, target)
        data, _st = schedule.open_secret_file(target)
        self.assertIn(NEW_TOKEN.encode(), data)

    def test_the_real_temp_directory_passes_through_its_sticky_bit(self):
        # every test path runs through the real temp directory; when it is the
        # usual sticky, world-writable one, the walk accepts it because the
        # next component (this test's folder) is ours
        parent = os.path.dirname(os.path.realpath(self.tmp))
        st = os.stat(parent)
        if not (st.st_mode & 0o022 and st.st_mode & 0o1000):
            self.skipTest("the temp directory is not sticky and world-writable here")
        safe = self.folder("safe", 0o700)
        self.assertIsNone(schedule.check_secret_dir(safe))

    @unittest.skipUnless(AS_ROOT, "chown needs root")
    def test_the_sticky_exception_needs_the_entry_to_be_ours(self):
        os.chmod(self.shared, 0o1777)
        os.chown(self.private, FOREIGN_UID, -1)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.check_secret_dir(self.private)
        self.assertIn("protects only entries owned by you or root, but {} is owned by "
                      "uid {}".format(self.private, FOREIGN_UID), str(cm.exception))

    @unittest.skipUnless(AS_ROOT, "chown needs root")
    def test_a_foreign_ancestor(self):
        theirs = self.folder("theirs", 0o755)
        mine = os.path.join(theirs, "mine")
        os.makedirs(mine)
        os.chmod(mine, 0o700)
        os.chown(theirs, FOREIGN_UID, -1)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.check_secret_dir(mine)
        self.assertIn("{} is owned by uid {} (neither you nor root)".format(
            theirs, FOREIGN_UID), str(cm.exception))
        target = self.put_token(mine)
        with self.assertRaises(schedule.SecretDirError):
            schedule.open_secret_file(target)

    @unittest.skipUnless(AS_ROOT, "chown needs root")
    def test_a_foreign_secret_file(self):
        safe = self.folder("safe", 0o700)
        target = self.put_token(safe)
        os.chown(target, FOREIGN_UID, -1)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.open_secret_file(target)
        self.assertIn("owned by uid {}, not you".format(FOREIGN_UID), str(cm.exception))
        with self.assertRaises(schedule.SecretDirError):
            schedule.env_file_for_wrapper(target)

    def test_a_symlinked_path_is_walked_to_its_real_place(self):
        safe = self.folder("safe", 0o700)
        target = self.put_token(safe)
        links = self.folder("links", 0o700)
        link_dir = os.path.join(links, "cfg")
        os.symlink("../safe", link_dir)                       # relative, with `..`
        via_link = os.path.join(link_dir, "token.env")
        self.assertEqual(schedule.verify_secret_chain(via_link)[0], target)
        self.assertIsNone(schedule.check_secret_dir(link_dir))
        path = streams_file(self.tmp, token={"store": "env-file", "env_file": via_link})
        code, out, _err = self.run_cli(["token", "where", "--streams", path,
                                        "--field", "env-file"])
        self.assertEqual((code, out.strip()), (0, target))   # the real path, no link left
        # a link whose target lies under the writable /shared is refused
        bad = os.path.join(links, "bad")
        os.symlink(self.private, bad)
        self.put_token(self.private)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.open_secret_file(os.path.join(bad, "token.env"))
        self.assertIn("without the sticky bit", str(cm.exception))

    def test_a_symlink_in_a_writable_folder_is_a_substitution_point(self):
        safe = self.folder("safe", 0o700)
        self.put_token(safe)
        link = os.path.join(self.shared, "cfg")               # the link's folder is 777
        os.symlink(safe, link)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.open_secret_file(os.path.join(link, "token.env"))
        self.assertIn("another user can rename {}".format(link), str(cm.exception))

    @unittest.skipUnless(AS_ROOT, "lchown needs root")
    def test_a_foreign_symlink(self):
        safe = self.folder("safe", 0o700)
        self.put_token(safe)
        links = self.folder("links", 0o700)
        link = os.path.join(links, "cfg")
        os.symlink(safe, link)
        os.lchown(link, FOREIGN_UID, -1)
        with self.assertRaises(schedule.SecretDirError) as cm:
            schedule.open_secret_file(os.path.join(link, "token.env"))
        self.assertIn("{} is a symlink owned by uid {}".format(link, FOREIGN_UID),
                      str(cm.exception))

    def test_a_final_symlink_to_the_file_reads_the_real_file(self):
        safe = self.folder("safe", 0o700)
        target = self.put_token(safe)
        alias = os.path.join(safe, "alias.env")
        os.symlink("token.env", alias)
        self.assertEqual(schedule.env_file_for_wrapper(alias), target)
        data, _st = schedule.open_secret_file(alias)
        self.assertIn(NEW_TOKEN.encode(), data)

    def test_a_swap_between_check_and_open_is_refused(self):
        safe = self.folder("safe", 0o700)
        target = self.put_token(safe)
        other = os.path.join(safe, "other.env")
        with open(other, "w") as fh:
            fh.write("x")
        real_verify = schedule.verify_secret_chain

        def stale(path, what="the token", allow_missing=False):
            real, _st = real_verify(path, what, allow_missing)
            return real, os.lstat(other)                      # what the walk saw before
        with mock.patch.object(schedule, "verify_secret_chain", stale):
            with self.assertRaises(schedule.SecretDirError) as cm:
                schedule.open_secret_file(target)
        self.assertIn("changed while it was being checked", str(cm.exception))

    def test_the_write_goes_to_the_real_folder(self):
        safe = self.folder("safe", 0o700)
        links = self.folder("links", 0o700)
        os.symlink(safe, os.path.join(links, "cfg"))
        schedule.write_env_file(os.path.join(links, "cfg", "token.env"), NEW_TOKEN)
        self.assertTrue(os.path.isfile(os.path.join(safe, "token.env")))
        self.assertTrue(os.path.islink(os.path.join(links, "cfg")))


class TestOwnersStreams(unittest.TestCase):
    """examples/streams.json is the owner's stream list; the group streams'
    manifests ship in examples/manifests/ and load as gather loads them."""

    def setUp(self):
        self.ex = os.path.join(HERE, "examples")
        self.cfg = schedule.load_streams(os.path.join(self.ex, "streams.json"))

    def test_the_list(self):
        got = {s["id"]: (s["kind"], s["target"] if s["kind"] == "repo" else None,
                         s["enabled"]) for s in self.cfg["streams"]}
        self.assertEqual(got, {
            "avm-bicep": ("repo", "Azure/bicep-registry-modules", True),
            "avm-tf": ("manifest", None, True),
            "bicep": ("repo", "Azure/bicep", True),
            "deployment-stacks": ("repo", "Azure/deployment-stacks", True),
            "enterprise-lz": ("manifest", None, True),
            "ai-lz": ("manifest", None, True)})
        ai = [s for s in self.cfg["streams"] if s["id"] == "ai-lz"][0]
        self.assertIn("Azure-Samples", ai["home"]["orgs"])
        self.assertEqual(schedule.probe_repo(self.cfg), "Azure/bicep-registry-modules")
        # the owner chose the app route; the key is a path outside the workspace
        tok = self.cfg["token"]
        self.assertEqual(tok["store"], "app")
        self.assertTrue(tok["private_key_file"].startswith("~/.config/"))
        self.assertTrue(tok["client_id"].startswith("REPLACE-WITH"))

    def test_group_manifests(self):
        import manifest
        want = {"enterprise-lz": ["Azure/Enterprise-Scale", "Azure/alz-bicep-accelerator"],
                "ai-lz": ["Azure/AI-Landing-Zones", "Azure/agent-landing-zone",
                          "Azure-Samples/ai-hub-gateway-solution-accelerator"]}
        for sid, slugs in want.items():
            with self.subTest(stream=sid):
                man = manifest.load_manifest(os.path.join(self.ex, "manifests",
                                                          sid + ".json"))
                self.assertEqual(man["project"], sid)
                self.assertEqual(["{}/{}".format(r["owner"], r["repo"])
                                  for r in man["repos"]], slugs)
                self.assertTrue(all(r["branch"] == "main" for r in man["repos"]))
                stream = [s for s in self.cfg["streams"] if s["id"] == sid][0]
                self.assertTrue(stream["manifest"].endswith(
                    os.path.join(sid, "manifest.json")))
        ai = manifest.load_manifest(os.path.join(self.ex, "manifests", "ai-lz.json"))
        self.assertIn("Azure-Samples", ai["affiliation"]["home_orgs"])


def urllib_parse_qs(query):
    import urllib.parse
    return urllib.parse.parse_qs(query)


# --- no token on a command line in the docs and examples ---------------------------------------

# A token typed into a command lands in shell history, whatever the file mode
# of where it is written. Documented commands must prompt for it instead.
_TOKEN_ON_COMMAND_LINE = (
    # an assignment of a literal (not $(...), $VAR, %s, or an ellipsis)
    re.compile(r"\b(?:GITHUB_TOKEN|GH_TOKEN|ACTIVITY_TEST_TOKEN)=(?![\"']?(?:\$|%s|…|\.\.\.))"
               r"[\"']?[^\s\"'`]"),
    # a placeholder or a real-looking token anywhere in a command
    re.compile(r"<paste>|<your[ _-]?token>|\bgh[pousr]_[A-Za-z0-9]{4,}"
               r"|\bgithub_pat_[A-Za-z0-9_]{4,}", re.I),
    # the keychain item given its value instead of prompting (-w last prompts)
    re.compile(r"security\s+add-generic-password\b.*\s-w\s+[^\s#]"),
)


def token_on_command_line(text):
    """[(line number, line)] that put a token value on a command line."""
    return [(n, ln) for n, ln in enumerate(text.splitlines(), 1)
            if any(p.search(ln) for p in _TOKEN_ON_COMMAND_LINE)]


class TestNoTokenOnCommandLine(unittest.TestCase):
    def _files(self):
        out = [os.path.join(HERE, n) for n in os.listdir(HERE) if n.endswith(".md")]
        for sub in ("docs", "examples", "reference", "commands"):
            for root, dirs, names in os.walk(os.path.join(HERE, sub)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                out += [os.path.join(root, n) for n in names
                        if n.endswith((".md", ".sh", ".txt", ".plist", ".json"))]
        return sorted(out)

    def test_docs_and_examples(self):
        files = self._files()
        self.assertIn(os.path.join(HERE, "docs", "SCHEDULING.md"), files)
        self.assertIn(os.path.join(HERE, "examples", "cron", "activity-gather.sh"), files)
        hits = []
        for path in files:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hits += ["{}:{}: {}".format(os.path.relpath(path, HERE), n, ln.strip())
                         for n, ln in token_on_command_line(fh.read())]
        self.assertEqual(hits, [], "\n".join(hits))

    def test_the_checker_catches_what_it_claims_to(self):
        bad = ["printf 'GITHUB_TOKEN=%s\\n' '<paste>' > token.env",
               "export GITHUB_TOKEN=abc123",
               "echo GH_TOKEN=\"s3cret\" >> ~/.env",
               "echo ghp_abcdefABCDEF0123 > f",
               "security add-generic-password -a me -s svc -w hunter2",
               "security add-generic-password -a me -s svc -U -w 'x'"]
        for line in bad:
            with self.subTest(line=line):
                self.assertTrue(token_on_command_line(line))
        good = ['export GITHUB_TOKEN="$(security find-generic-password -a "$USER" -s s -w)"',
                "GITHUB_TOKEN=$(cat file)",
                "printf 'GITHUB_TOKEN=%s\\n' \"$t\" > token.env",
                "security add-generic-password -a \"$USER\" -s svc -U -w",
                "security add-generic-password -a me -s svc -w   # prompts",
                "holding `GITHUB_TOKEN=…`, mode 600",
                "gather reads GITHUB_TOKEN, else GH_TOKEN"]
        for line in good:
            with self.subTest(line=line):
                self.assertEqual(token_on_command_line(line), [])


if __name__ == "__main__":
    unittest.main()
