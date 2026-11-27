"""schedule.py — the scheduled gather runner, and the reader the skill calls.

Two concerns, kept apart (docs/SCHEDULING.md):

  * **Gathering** (this runner, `run`): a nightly job folds each followed
    stream's current month into `<workspace>/<stream>/stores/YYYY-MM.db` with
    the existing `gather.py` CLI (a subprocess per window, so a scheduled run
    and a manual run behave the same), proves the result with `validate.py`,
    and only then swaps it in. No model, and no network of its own: the only
    network use is the gather it starts.
  * **Reading** (`latest`): the skill's report step asks which store(s) hold a
    period, how fresh they are and whether the last run passed the trust
    check, and is refused when the data is stale or the last run failed.

    python3 schedule.py run    --streams FILE [--stream ID]... [--backfill YYYY-MM[..YYYY-MM]]
                               [--dry-run] [--max-minutes N] [--today YYYY-MM-DD]
                               [--break-stale-lock]
    python3 schedule.py latest --streams FILE --stream ID
                               (--month YYYY-MM | --last-days N | --from D --to D)
                               [--max-age-hours H] [--accept-stale] [--today YYYY-MM-DD]
    python3 schedule.py status --streams FILE [--json]
    python3 schedule.py check  --streams FILE

The streams file (JSON; schema in docs/SCHEDULING.md, example in
examples/streams.json) names the workspace root, the ONE people registry every
stream shares, and per stream: id, title, kind (`repo`: owner / repo, or
`manifest`: a manifest path), home settings, cadence, gather flags, env caps,
enabled.

Windows (UTC "today", injectable): every gather is a WHOLE calendar month
(`--from YYYY-MM-01 --to YYYY-MM-<last>`), so a store holds exactly the window
a manual monthly gather would, and each night's re-fold replaces that window's
per-window facts instead of adding a new to-date window. While the month is
open the gather passes `--ref-date <today>` (records created after today cannot
exist yet, so the data is "month to date"). During the first
`regather_previous_month_days` days (default 7) the previous month is gathered
again, so a closed month ends in its final state. `--backfill` gathers history.
A stream with `carry_previous_days` > 0 (default 6 for a weekly cadence) also
folds the last N days of the previous month into the month's store (a second,
older window, folded first): a week that spans the month boundary is then one
view over one store (an accumulated store, USAGE 2.1).

Failure never replaces a good store: the store is copied (SQLite backup API)
to `stores/.YYYY-MM.db.next`, gather folds into the copy, validate audits the
copy, and only then is the copy renamed over the store (the old file kept as
`YYYY-MM.db.prev` for one generation). A per-workspace lock prevents
overlapping runs; a stale lock is reported, never silently broken.

Token: gather reads GITHUB_TOKEN, else GH_TOKEN, from the environment; this
runner only passes the environment through. It never writes a token to any
file, log or status, and redacts one if it appears in captured output.

Exit codes: run — 0 every window ok, 1 a window failed or was skipped by the
time budget, 2 usage / config error, 3 the workspace is locked. latest — 0
usable, 1 refused, 2 usage error.

Stdlib only. Python 3.11+ (the skill's minimum); macOS and Linux.
"""

import argparse
import calendar
import datetime
import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse

import manifest as manifest_mod

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
GATHER_SCRIPT = os.path.join(SKILL_DIR, "gather.py")
VALIDATE_SCRIPT = os.path.join(SKILL_DIR, "validate.py")
VIEW_SCRIPT = os.path.join(SKILL_DIR, "view.py")

STATUS_SCHEMA = 1
DEFAULT_REGATHER_DAYS = 7
DEFAULT_STALE_HOURS = 36
DEFAULT_LOCK_STALE_HOURS = 12
DEFAULT_LOG_DAYS = 60
WEEKLY_CARRY_DAYS = 6
LOCK_NAME = ".schedule.lock"
MAX_NOTE_LINES = 60

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3

_STREAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
CADENCES = ("weekly", "monthly")

# gather flags a stream may add. Everything that names a path, a target or a
# window is the runner's to set, so it is refused here.
BOOL_FLAGS = frozenset((
    "--structural-iac", "--no-iac-build", "--no-pr-history", "--no-pr-checks",
    "--no-project-board", "--no-newcomers", "--no-affiliation",
    "--no-workflows", "--no-releases", "--people-offline", "--no-clone"))
VALUE_FLAGS = frozenset(("--branches", "--people-refresh-days", "--affiliation"))

# Env names a stream may set for its gather: the skill's own knobs and the
# shared Terraform plugin cache. Never a credential.
_ENV_NAME_RE = re.compile(r"^(ACTIVITY_[A-Z0-9_]+|TF_PLUGIN_CACHE_DIR)$")
_SECRET_SUFFIXES = ("_TOKEN", "_PAT", "_SECRET", "_PASSWORD", "_KEY")

# Folders whose contents a sync client copies around: SQLite files there can be
# corrupted mid-write or forked into conflict copies.
SYNCED_MARKERS = ("Mobile Documents", "iCloud", "Dropbox", "OneDrive")


class ConfigError(ValueError):
    """The streams file (or an argument) is unusable."""


# --- dates -------------------------------------------------------------------

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def month_key(day):
    return "{:04d}-{:02d}".format(day.year, day.month)


def parse_month(text, what="month"):
    m = _MONTH_RE.fullmatch(text or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ConfigError("{} must be YYYY-MM, got {!r}".format(what, text))
    return datetime.date(int(m.group(1)), int(m.group(2)), 1)


def month_bounds(first):
    """(first day, last day) of the month that `first` is in."""
    first = first.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def prev_month(first):
    return (first.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


def next_month(first):
    _, last = month_bounds(first)
    return last + datetime.timedelta(days=1)


def months_between(lo, hi):
    """The first days of every month from lo's month to hi's month."""
    out, cur = [], lo.replace(day=1)
    while cur <= hi:
        out.append(cur)
        cur = next_month(cur)
    return out


def parse_backfill(text, today):
    """`YYYY-MM` or `YYYY-MM..YYYY-MM` -> [first days], oldest first. A month
    after the current one is refused (it has nothing to gather)."""
    lo_s, sep, hi_s = (text or "").partition("..")
    lo = parse_month(lo_s, "--backfill")
    hi = parse_month(hi_s, "--backfill") if sep else lo
    if lo > hi:
        raise ConfigError("--backfill {} is after {}".format(lo_s, hi_s))
    if hi > today.replace(day=1):
        raise ConfigError("--backfill {} is after the current month {}".format(
            month_key(hi), month_key(today)))
    return months_between(lo, hi)


# --- the streams file --------------------------------------------------------

def _str_list(value, what):
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value):
        raise ConfigError("{} must be a list of non-empty strings".format(what))
    return list(value)


def _check_env(env, what):
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ConfigError("{} must be an object of NAME: value".format(what))
    out = {}
    for name, value in env.items():
        if not _ENV_NAME_RE.fullmatch(name) or name.upper().endswith(_SECRET_SUFFIXES):
            raise ConfigError(
                "{}: {!r} is not allowed (only ACTIVITY_* knobs and "
                "TF_PLUGIN_CACHE_DIR; never a credential: the token comes from "
                "the environment)".format(what, name))
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ConfigError("{}: {} must be a string or an integer".format(
                what, name))
        out[name] = str(value)
    return out


def _check_flags(flags, what):
    flags = _str_list(flags, what)
    for flag in flags:
        name = flag.split("=", 1)[0]
        if name in BOOL_FLAGS and "=" not in flag:
            continue
        if name in VALUE_FLAGS and "=" in flag and flag.split("=", 1)[1]:
            continue
        raise ConfigError(
            "{}: {!r} is not allowed (boolean flags: {}; with a value, written "
            "FLAG=VALUE: {}; the runner sets the target, window, store, clone "
            "dir and people registry itself)".format(
                what, flag, ", ".join(sorted(BOOL_FLAGS)),
                ", ".join(sorted(VALUE_FLAGS))))
    return flags


def _resolve(base, path):
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


def _int(value, what, minimum=0, default=None, allow_none=False):
    if value is None:
        if allow_none:
            return None
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError("{} must be an integer >= {}".format(what, minimum))
    return value


def load_streams(path):
    """Read + check the streams file. Returns a dict with absolute paths:
    {path, workspace, people, regather_days, stale_hours, lock_stale_hours,
     retention: {log_days, store_months}, env, streams: [stream...]}.
    Raises ConfigError naming the problem."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError("cannot read streams file {!r}: {}".format(path, exc))
    except ValueError as exc:
        raise ConfigError("streams file {!r} is not JSON: {}".format(path, exc))
    if not isinstance(raw, dict):
        raise ConfigError("streams file: the top level must be an object")
    if raw.get("version", 1) != 1:
        raise ConfigError("streams file: version must be 1")
    base = os.path.dirname(os.path.abspath(path))
    if not isinstance(raw.get("workspace"), str) or not raw["workspace"].strip():
        raise ConfigError("streams file: 'workspace' (the data directory) is required")
    workspace = _resolve(base, raw["workspace"])
    people = raw.get("people", "people.json")
    if not isinstance(people, str) or not people.strip():
        raise ConfigError("streams file: 'people' must be a path")
    retention = raw.get("retention") or {}
    if not isinstance(retention, dict):
        raise ConfigError("streams file: 'retention' must be an object")
    cfg = {
        "path": os.path.abspath(path),
        "workspace": workspace,
        "people": _resolve(workspace, people),
        "regather_days": _int(raw.get("regather_previous_month_days"),
                              "regather_previous_month_days", 0,
                              DEFAULT_REGATHER_DAYS),
        "stale_hours": _int(raw.get("stale_after_hours"), "stale_after_hours",
                            1, DEFAULT_STALE_HOURS),
        "lock_stale_hours": _int(raw.get("lock_stale_hours"), "lock_stale_hours",
                                 1, DEFAULT_LOCK_STALE_HOURS),
        "retention": {
            "log_days": _int(retention.get("log_days"), "retention.log_days", 1,
                             DEFAULT_LOG_DAYS),
            "store_months": _int(retention.get("store_months"),
                                 "retention.store_months", 1, allow_none=True),
        },
        "env": _check_env(raw.get("env"), "env"),
        "streams": [],
    }
    streams = raw.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ConfigError("streams file: 'streams' must list at least one stream")
    seen = set()
    for i, s in enumerate(streams):
        what = "streams[{}]".format(i)
        if not isinstance(s, dict):
            raise ConfigError("{} must be an object".format(what))
        sid = s.get("id")
        if not isinstance(sid, str) or not _STREAM_ID_RE.fullmatch(sid):
            raise ConfigError("{}: 'id' must be lower-case letters, digits, '-' "
                              "or '_' (it names the stream's directory), got "
                              "{!r}".format(what, sid))
        if sid in seen:
            raise ConfigError("{}: duplicate id {!r}".format(what, sid))
        seen.add(sid)
        what = "stream {!r}".format(sid)
        kind = s.get("kind")
        st = {"id": sid, "title": s.get("title") or sid, "kind": kind,
              "enabled": s.get("enabled", True)}
        if not isinstance(st["enabled"], bool):
            raise ConfigError("{}: 'enabled' must be true or false".format(what))
        if kind == "repo":
            try:
                st["owner"] = manifest_mod.check_owner(s.get("owner"), "'owner'")
                st["repo"] = manifest_mod.check_repo(s.get("repo"), "'repo'")
            except ValueError as exc:
                raise ConfigError("{}: {}".format(what, exc))
            st["target"] = "{}/{}".format(st["owner"], st["repo"])
        elif kind == "manifest":
            if not isinstance(s.get("manifest"), str) or not s["manifest"].strip():
                raise ConfigError("{}: 'manifest' (a path) is required for kind "
                                  "manifest".format(what))
            st["manifest"] = _resolve(workspace, s["manifest"])
            st["target"] = st["manifest"]
        else:
            raise ConfigError("{}: 'kind' must be 'repo' or 'manifest', got "
                              "{!r}".format(what, kind))
        home = s.get("home") or {}
        if not isinstance(home, dict):
            raise ConfigError("{}: 'home' must be an object".format(what))
        st["home"] = {k: _str_list(home.get(k), "{}: home.{}".format(what, k))
                      for k in ("orgs", "names", "email_domains")}
        try:
            for org in st["home"]["orgs"]:
                manifest_mod.check_owner(org, "home.orgs")
        except ValueError as exc:
            raise ConfigError("{}: {}".format(what, exc))
        cadence = _str_list(s.get("cadence", ["monthly"]), "{}: cadence".format(what))
        bad = [c for c in cadence if c not in CADENCES]
        if bad or not cadence:
            raise ConfigError("{}: cadence must list 'weekly' and/or 'monthly'"
                              .format(what))
        st["cadence"] = cadence
        st["gather_flags"] = _check_flags(s.get("gather_flags"),
                                          "{}: gather_flags".format(what))
        st["env"] = _check_env(s.get("env"), "{}: env".format(what))
        st["carry_previous_days"] = _int(
            s.get("carry_previous_days"), "{}: carry_previous_days".format(what),
            0, WEEKLY_CARRY_DAYS if "weekly" in cadence else 0)
        if st["carry_previous_days"] > 27:
            raise ConfigError("{}: carry_previous_days must be at most 27".format(what))
        st["stale_hours"] = _int(s.get("stale_after_hours"),
                                 "{}: stale_after_hours".format(what), 1,
                                 cfg["stale_hours"])
        cfg["streams"].append(st)
    return cfg


def stream_dir(cfg, stream):
    return os.path.join(cfg["workspace"], stream["id"])


def store_path(cfg, stream, first):
    return os.path.join(stream_dir(cfg, stream), "stores", month_key(first) + ".db")


def rel(cfg, path):
    """A path as written in status files: relative to the workspace."""
    try:
        r = os.path.relpath(path, cfg["workspace"])
    except ValueError:
        return path
    return path if r.startswith("..") else r


# --- warnings about where the workspace lives --------------------------------

def synced_folder_reasons(path, isdir=os.path.isdir):
    """Why `path` looks like it sits in a cloud-synced folder or a notes vault:
    a path component naming a sync client, or an ancestor holding `.obsidian`.
    Empty when it does not."""
    path = os.path.abspath(os.path.expanduser(path))
    reasons = []
    parts = path.split(os.sep)
    for marker in SYNCED_MARKERS:
        if any(marker.lower() in p.lower() for p in parts if p):
            reasons.append("the path contains {!r}".format(marker))
    cur = path
    while True:
        if isdir(os.path.join(cur, ".obsidian")):
            reasons.append("{} holds .obsidian (a notes vault)".format(cur))
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return reasons


def workspace_warnings(cfg, isdir=os.path.isdir):
    reasons = synced_folder_reasons(cfg["workspace"], isdir=isdir)
    if not reasons:
        return []
    return ["workspace {} looks synced ({}): SQLite stores under file sync can be "
            "corrupted or forked into conflict copies. Keep the workspace in a "
            "local, non-synced directory (e.g. ~/activity-data) and write only "
            "the finished Markdown into the vault.".format(
                cfg["workspace"], "; ".join(reasons))]


# --- planning ----------------------------------------------------------------

def store_windows(path):
    """The gathered windows [(from, to)] a store's ledger records; [] when the
    store does not exist or cannot be read. Read-only."""
    if not os.path.isfile(path):
        return []
    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return []
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='gathered_windows'").fetchone()
        windows = json.loads(row[0]) if row and row[0] else []
    except (sqlite3.Error, ValueError):
        return []
    finally:
        conn.close()
    return sorted({(w.get("from"), w.get("to")) for w in windows
                   if isinstance(w, dict) and w.get("from") and w.get("to")})


def plan_stream(cfg, stream, today, backfill=None, windows_of=store_windows):
    """The windows to gather for one stream "today" (a UTC date), oldest
    first. Each: {month, from, to, ref_date, reason, store, tail}, where
    `tail` is the carried window {from, to} folded first, or None."""
    current = today.replace(day=1)
    if backfill:
        months = [(m, "current" if m == current else "backfill") for m in backfill]
    else:
        months = []
        if cfg["regather_days"] and today.day <= cfg["regather_days"]:
            months.append((prev_month(current), "previous"))
        months.append((current, "current"))
    plan = []
    for first, reason in months:
        lo, hi = month_bounds(first)
        path = store_path(cfg, stream, first)
        ref_date = today.isoformat() if (first == current and today < hi) else None
        tail = None
        carry = stream["carry_previous_days"]
        if carry:
            t_to = lo - datetime.timedelta(days=1)
            t_from = t_to - datetime.timedelta(days=carry - 1)
            window = (t_from.isoformat(), t_to.isoformat())
            refresh = reason == "current" and cfg["regather_days"] \
                and today.day <= cfg["regather_days"]
            if refresh or window not in windows_of(path):
                tail = {"from": window[0], "to": window[1]}
        plan.append({"month": month_key(first), "from": lo.isoformat(),
                     "to": hi.isoformat(), "ref_date": ref_date,
                     "reason": reason, "store": path, "tail": tail})
    return plan


def manifest_run_path(cfg, stream, frm, to):
    return os.path.join(stream_dir(cfg, stream), "run",
                        "manifest.{}_{}.json".format(frm, to))


def gather_argv(cfg, stream, frm, to, ref_date, store, manifest_path=None,
                python=None, gather_script=None):
    """The exact gather command for one window."""
    argv = [python or sys.executable, gather_script or GATHER_SCRIPT]
    if stream["kind"] == "repo":
        argv += ["--owner", stream["owner"], "--repo", stream["repo"],
                 "--from", frm, "--to", to]
    else:
        argv += ["--manifest", manifest_path or manifest_run_path(cfg, stream, frm, to)]
    if ref_date:
        argv += ["--ref-date", ref_date]
    argv += ["--store", store,
             "--clone-dir", os.path.join(stream_dir(cfg, stream), "clone"),
             "--people", cfg["people"]]
    for flag, key in (("--home-org", "orgs"), ("--home-name", "names"),
                      ("--home-email-domain", "email_domains")):
        for value in stream["home"][key]:
            argv += [flag, value]
    for flag in stream["gather_flags"]:
        name, sep, value = flag.partition("=")
        argv += [name, value] if sep else [name]
    return argv


def validate_argv(stream, store, project=None, python=None):
    argv = [python or sys.executable, VALIDATE_SCRIPT, store]
    if stream["kind"] == "repo":
        argv += ["--project", stream["owner"], "--repo", stream["repo"]]
    elif project:
        argv += ["--project", project]
    return argv


def tmp_store_path(store):
    return os.path.join(os.path.dirname(store),
                        "." + os.path.basename(store) + ".next")


def stream_env(cfg, stream, base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    env.update(cfg["env"])
    env.update(stream["env"])
    cache = env.get("TF_PLUGIN_CACHE_DIR")
    if cache and ("TF_PLUGIN_CACHE_DIR" in cfg["env"]
                  or "TF_PLUGIN_CACHE_DIR" in stream["env"]):
        env["TF_PLUGIN_CACHE_DIR"] = _resolve(cfg["workspace"], cache)
    return env


def token_free_env(env):
    """`env` without anything that looks like a credential (validate needs none)."""
    return {k: v for k, v in env.items()
            if not (k.upper() in ("GITHUB_TOKEN", "GH_TOKEN")
                    or k.upper().endswith(_SECRET_SUFFIXES))}


# --- redaction ---------------------------------------------------------------

_TOKEN_SHAPES = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
REDACTED = "[REDACTED]"


def secrets_of(env):
    """Values of credential-like variables in `env` (at least 8 characters)."""
    out = set()
    for k, v in env.items():
        if not v or len(v) < 8:
            continue
        up = k.upper()
        if up in ("GITHUB_TOKEN", "GH_TOKEN") or up.endswith(_SECRET_SUFFIXES):
            out.add(v)
    return sorted(out, key=len, reverse=True)


def redact(text, secrets):
    if not text:
        return text
    for s in secrets:
        text = text.replace(s, REDACTED)
    return _TOKEN_SHAPES.sub(REDACTED, text)


# --- reading gather's own report -------------------------------------------

_NOTE_PREFIXES = ("warning:", "note:", "error:", "newcomers:", "affiliation:",
                  "people registry:", "pr history:", "pr activity:",
                  "module notes:", "closing issues:", "member ", "gather:",
                  "retracted ")


def gather_notes(stderr_text):
    """What a gather said about itself: its notes and warnings (capped), rate
    limit waits, GraphQL points it reported, people not checked, capped PRs,
    failed members."""
    lines = [ln.rstrip() for ln in (stderr_text or "").splitlines()]
    notes = [ln for ln in lines if ln.lstrip().lower().startswith(_NOTE_PREFIXES)]
    points = [int(m.group(1)) for ln in lines
              for m in re.finditer(r"(\d+) point\(s\)", ln)]
    not_checked = sum(int(m.group(1)) for ln in lines
                      for m in re.finditer(r"\((\d+) not checked\)", ln))
    capped = sum(int(m.group(1)) for ln in lines if ln.startswith("pr activity:")
                 for m in re.finditer(r"(\d+) capped", ln))
    return {
        "lines": notes[:MAX_NOTE_LINES],
        "lines_omitted": max(0, len(notes) - MAX_NOTE_LINES),
        "warnings": sum(1 for ln in notes if ln.lower().startswith("warning:")),
        "rate_limit_waits": sum(1 for ln in lines if "rate limit hit" in ln),
        "api_points": sum(points) if points else None,
        "not_checked": not_checked,
        "capped": capped,
        "member_failures": [ln.strip() for ln in lines
                            if re.match(r"\s*member \S+ failed:", ln)],
    }


# --- SQLite file handling ------------------------------------------------------

def _connect_ro(path):
    uri = "file:{}?mode=ro".format(urllib.parse.quote(os.path.abspath(path), safe="/"))
    return sqlite3.connect(uri, uri=True)


def _remove(path):
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def copy_store(src, dst):
    """A consistent copy of `src` at `dst` (SQLite's online backup: it reads
    through any WAL, so a store last closed uncleanly still copies whole)."""
    _remove(dst)
    s = sqlite3.connect(src)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def settle(path):
    """Checkpoint a WAL-mode store into its main file. True when the file
    stands alone afterwards (no -wal / -shm left, i.e. nobody has it open)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conn.close()
    return not (os.path.exists(path + "-wal") or os.path.exists(path + "-shm"))


def swap_in(tmp, store):
    """Replace `store` with `tmp` atomically, keeping the old file as
    `store.prev` (one generation). Readers always find a complete store at
    `store`: the old file is hard-linked (copied where links are not
    supported) to `.prev` before the rename."""
    if not settle(tmp):
        raise RuntimeError("the new store {} is still open".format(tmp))
    if os.path.exists(store):
        if not settle(store):
            raise RuntimeError("store {} is in use (a -wal/-shm file is present); "
                               "left as it was".format(store))
        prev = store + ".prev"
        _remove(prev)
        try:
            os.link(store, prev)
        except OSError:
            copy_store(store, prev)
    os.replace(tmp, store)
    try:
        fd = os.open(os.path.dirname(store) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def store_counts(path):
    """Totals in a store: PRs, issues, commits, people, gathered windows."""
    conn = _connect_ro(path)
    try:
        local = "substr(id, instr(id, '#') + 1)"
        prs, issues, commits, people = conn.execute(
            "SELECT"
            " SUM(CASE WHEN node_class='social' AND {l} LIKE 'pr-%' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN node_class='social' AND {l} LIKE 'issue-%' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN node_class='code' AND length({l})=40"
            "      AND {l} NOT GLOB '*[^0-9a-f]*' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN {l} LIKE 'person-%' THEN 1 ELSE 0 END)"
            " FROM nodes".format(l=local)).fetchone()
    finally:
        conn.close()
    return {"prs": prs or 0, "issues": issues or 0, "commits": commits or 0,
            "people": people or 0, "windows": len(store_windows(path)),
            "bytes": os.path.getsize(path)}


# --- seams ---------------------------------------------------------------------

def run_subprocess(argv, env):
    """Run one command; (exit code, stdout, stderr). The seam tests replace."""
    proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                          errors="replace", stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def code_version(skill_dir=SKILL_DIR):
    """{"sha", "dirty"} of the skill's git checkout, or None outside one."""
    def git(*args):
        return subprocess.run(["git", "-C", skill_dir, *args], capture_output=True,
                              text=True, timeout=20, stdin=subprocess.DEVNULL)
    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return None
        dirty = git("status", "--porcelain", "--", ".")
        return {"sha": head.stdout.strip(),
                "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return None


# --- the lock ------------------------------------------------------------------

class Locked(Exception):
    pass


def acquire_lock(workspace, now, stale_hours, break_stale=False):
    """Create `<workspace>/.schedule.lock` (O_EXCL). Raises Locked with the
    reason when another run holds it; a lock older than `stale_hours` is
    reported as stale and only removed with `break_stale`."""
    path = os.path.join(workspace, LOCK_NAME)
    os.makedirs(workspace, exist_ok=True)
    body = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                       "started_at": iso(now)}, sort_keys=True)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as fh:
                    held = json.load(fh)
            except (OSError, ValueError):
                held = {}
            started = parse_iso(held.get("started_at"))
            if started is None:
                try:
                    started = datetime.datetime.fromtimestamp(
                        os.path.getmtime(path), datetime.timezone.utc)
                except OSError:
                    continue
            age = (now - started).total_seconds() / 3600.0
            who = "pid {} on {}".format(held.get("pid", "?"), held.get("host", "?"))
            if age >= stale_hours:
                if break_stale and attempt == 1:
                    os.remove(path)
                    continue
                raise Locked(
                    "STALE lock {} ({}, started {}, {:.1f} h ago, older than {} h): "
                    "no run should take that long. If no run is active, remove "
                    "it or pass --break-stale-lock".format(
                        path, who, iso(started), age, stale_hours))
            raise Locked("another run holds {} ({}, started {}, {:.1f} h ago)".format(
                path, who, iso(started), age))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        return path
    raise Locked("could not create {}".format(path))


def release_lock(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# --- status files --------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def stream_status_path(cfg, stream):
    return os.path.join(stream_dir(cfg, stream), "status.json")


# --- the run ---------------------------------------------------------------------

class RunLog:
    """The per-run log file; each line also goes to stderr. Everything is
    redacted before it is written."""

    def __init__(self, path, secrets, echo=True):
        self.path = path
        self.secrets = secrets
        self.echo = echo
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def __call__(self, text, raw=False):
        text = redact(text, self.secrets)
        lines = text.splitlines() or [""]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out = "".join((ln if raw else "{} {}".format(stamp, ln)) + "\n"
                      for ln in lines)
        if self._fh:
            self._fh.write(out)
            self._fh.flush()
        if self.echo:
            sys.stderr.write(out)

    def close(self):
        if self._fh:
            self._fh.close()


def _write_run_manifest(cfg, stream, frm, to, path):
    """The stream's manifest with this window and the shared registry, checked
    by the same loader gather uses. Returns its project name."""
    raw = read_json(stream["manifest"])
    if not isinstance(raw, dict):
        raise ConfigError("stream {!r}: manifest {} is missing or not a JSON "
                          "object".format(stream["id"], stream["manifest"]))
    raw = dict(raw, window={"from": frm, "to": to}, people=cfg["people"])
    write_json(path, raw)
    try:
        return manifest_mod.load_manifest(path)["project"]
    except ValueError as exc:
        raise ConfigError("stream {!r}: {}".format(stream["id"], exc))


def manifest_project(stream):
    raw = read_json(stream["manifest"]) if stream["kind"] == "manifest" else None
    return raw.get("project") if isinstance(raw, dict) else None


def run_window(cfg, stream, win, log, env, run=run_subprocess, now=utc_now,
               clock=time.monotonic, gather_script=None):
    """Gather one window into a copy of its store, validate the copy, swap it
    in. Returns the attempt record (result 'ok' or the failing step)."""
    store = win["store"]
    tmp = tmp_store_path(store)
    os.makedirs(os.path.dirname(store), exist_ok=True)
    started, t0 = now(), clock()
    attempt = {"at": iso(started), "result": None, "reason": win["reason"],
               "from": win["from"], "to": win["to"], "ref_date": win["ref_date"],
               "gathers": [], "validate": None, "error": None}
    secrets = secrets_of(env)
    all_err = []
    try:
        _remove(tmp)
        if os.path.exists(store):
            copy_store(store, tmp)
        steps = []
        if win["tail"]:
            steps.append((win["tail"]["from"], win["tail"]["to"], None, "tail"))
        steps.append((win["from"], win["to"], win["ref_date"], "month"))
        project = None
        for frm, to, ref_date, part in steps:
            mpath = None
            if stream["kind"] == "manifest":
                mpath = manifest_run_path(cfg, stream, frm, to)
                project = _write_run_manifest(cfg, stream, frm, to, mpath)
            argv = gather_argv(cfg, stream, frm, to, ref_date, tmp, mpath,
                               gather_script=gather_script)
            log("gather {} {} [{}..{}]: {}".format(stream["id"], part, frm, to,
                                                   shlex.join(argv)))
            g0 = clock()
            code, out, err = run(argv, env)
            out, err = redact(out, secrets), redact(err, secrets)
            all_err.append(err)
            for text in (out, err):
                if text:
                    log("\n".join("  | " + ln for ln in text.splitlines()), raw=True)
            attempt["gathers"].append({"part": part, "from": frm, "to": to,
                                       "ref_date": ref_date, "exit": code,
                                       "duration_s": round(clock() - g0, 1)})
            if code != 0:
                attempt["result"] = "gather_failed"
                attempt["error"] = "gather exited {} ({} {}..{})".format(
                    code, part, frm, to)
                return attempt
        vargv = validate_argv(stream, tmp, project)
        log("validate {}: {}".format(stream["id"], shlex.join(vargv)))
        code, out, err = run(vargv, token_free_env(env))
        out, err = redact(out, secrets), redact(err, secrets)
        verdict = None
        for ln in (out or "").splitlines():
            if ln.startswith("RESULT: "):
                verdict = ln[len("RESULT: "):].strip()
        attempt["validate"] = {"exit": code, "result": verdict}
        if code != 0:
            for text in (out, err):
                if text:
                    log("\n".join("  | " + ln for ln in text.splitlines()), raw=True)
            attempt["result"] = "validate_failed"
            attempt["error"] = "validate exited {} (RESULT: {})".format(code, verdict)
            return attempt
        log("validate {}: RESULT: {}".format(stream["id"], verdict))
        attempt["counts"] = store_counts(tmp)
        try:
            swap_in(tmp, store)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            attempt["result"] = "swap_failed"
            attempt["error"] = redact(str(exc), secrets)
            return attempt
        attempt["result"] = "ok"
        return attempt
    except ConfigError as exc:
        attempt["result"] = "config_error"
        attempt["error"] = str(exc)
        return attempt
    except Exception as exc:  # noqa: BLE001 - one window never stops the run
        attempt["result"] = "error"
        attempt["error"] = redact("{}: {}".format(type(exc).__name__, exc), secrets)
        return attempt
    finally:
        if attempt["result"] != "ok":
            _remove(tmp)
        attempt["notes"] = gather_notes("\n".join(all_err))
        attempt["duration_s"] = round(clock() - t0, 1)
        attempt["finished_at"] = iso(now())


def _update_stream_status(cfg, stream, run_id, log_path, run_started, records,
                          version, result, now):
    path = stream_status_path(cfg, stream)
    status = read_json(path, {}) or {}
    windows = status.get("windows") or {}
    for win, attempt in records:
        entry = windows.get(win["month"]) or {}
        entry.update({"month": win["month"], "from": win["from"], "to": win["to"],
                      "store": rel(cfg, win["store"]),
                      "store_prev": rel(cfg, win["store"] + ".prev")})
        attempt = dict(attempt, run_id=run_id, code_version=version)
        entry["last_attempt"] = attempt
        if attempt["result"] == "ok":
            entry["last_success"] = attempt
            if win["tail"]:
                entry["carried"] = {"from": win["tail"]["from"],
                                    "to": win["tail"]["to"], "at": attempt["at"]}
        windows[win["month"]] = entry
    status.update({
        "schema": STATUS_SCHEMA, "stream": stream["id"], "title": stream["title"],
        "kind": stream["kind"],
        "target": rel(cfg, stream["target"]) if stream["kind"] == "manifest"
        else stream["target"],
        "cadence": stream["cadence"],
        "carry_previous_days": stream["carry_previous_days"],
        "updated_at": iso(now()), "windows": dict(sorted(windows.items())),
        "last_run": {"run_id": run_id, "started_at": iso(run_started),
                     "finished_at": iso(now()), "result": result,
                     "log": rel(cfg, log_path) if log_path else None},
    })
    write_json(path, status)
    return status


def apply_retention(cfg, today, log):
    logs = os.path.join(cfg["workspace"], "logs")
    cutoff = time.time() - cfg["retention"]["log_days"] * 86400
    if os.path.isdir(logs):
        for name in sorted(os.listdir(logs)):
            p = os.path.join(logs, name)
            if name.startswith("run-") and os.path.isfile(p) \
                    and os.path.getmtime(p) < cutoff:
                os.remove(p)
    # `.prev` is the rollback copy of a store the schedule still re-gathers:
    # the current month, and the previous one during its re-gather days.
    # Once a month is no longer gathered its `.prev` only doubles its size.
    current = today.replace(day=1)
    active = {month_key(current)}
    if cfg["regather_days"] and today.day <= cfg["regather_days"]:
        active.add(month_key(prev_month(current)))
    keep = cfg["retention"]["store_months"]
    oldest = current
    for _ in range((keep or 1) - 1):
        oldest = prev_month(oldest)
    for stream in cfg["streams"]:
        d = os.path.join(stream_dir(cfg, stream), "stores")
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            m = re.fullmatch(r"(\d{4}-\d{2})\.db(\.prev)?", name)
            if not m:
                continue
            old_store = keep and m.group(1) < month_key(oldest)
            done_prev = m.group(2) and m.group(1) not in active
            if old_store or done_prev:
                log("retention: removing {}".format(os.path.join(d, name)))
                _remove(os.path.join(d, name))


def select_streams(cfg, ids):
    if not ids:
        return [s for s in cfg["streams"] if s["enabled"]]
    known = {s["id"]: s for s in cfg["streams"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ConfigError("unknown stream(s) {} (streams: {})".format(
            ", ".join(unknown), ", ".join(sorted(known))))
    return [known[i] for i in ids]


def cmd_run(cfg, args, run=run_subprocess, now=utc_now, clock=time.monotonic,
            version_of=code_version, gather_script=None, base_env=None,
            isdir=os.path.isdir):
    started = now()
    today = args.today or started.date()
    streams = select_streams(cfg, args.stream)
    backfill = parse_backfill(args.backfill, today) if args.backfill else None
    warnings = workspace_warnings(cfg, isdir=isdir)
    for w in warnings:
        sys.stderr.write("warning: {}\n".format(w))
    plans = [(s, plan_stream(cfg, s, today, backfill)) for s in streams]

    if args.dry_run:
        out = []
        for s, plan in plans:
            out.append("stream {} ({}; {})".format(s["id"], s["title"], s["target"]))
            for win in plan:
                tmp = tmp_store_path(win["store"])
                out.append("  {} [{}..{}] {}{} -> {}".format(
                    win["month"], win["from"], win["to"], win["reason"],
                    " ref-date " + win["ref_date"] if win["ref_date"] else "",
                    win["store"]))
                steps = ([(win["tail"]["from"], win["tail"]["to"], None)]
                         if win["tail"] else [])
                steps.append((win["from"], win["to"], win["ref_date"]))
                for frm, to, ref in steps:
                    out.append("    " + shlex.join(gather_argv(
                        cfg, s, frm, to, ref, tmp, gather_script=gather_script)))
                out.append("    " + shlex.join(validate_argv(
                    s, tmp, manifest_project(s))))
                out.append("    then: swap {} -> {} (previous kept as {}.prev)".format(
                    tmp, win["store"], win["store"]))
        sys.stdout.write("\n".join(out) + "\n")
        return EXIT_OK

    env = dict(os.environ if base_env is None else base_env)
    if not (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")):
        sys.stderr.write("schedule.py: error: set GITHUB_TOKEN (or GH_TOKEN) for the "
                         "gather (docs/SCHEDULING.md \"The token\")\n")
        return EXIT_USAGE
    try:
        lock = acquire_lock(cfg["workspace"], started, cfg["lock_stale_hours"],
                            break_stale=args.break_stale_lock)
    except Locked as exc:
        sys.stderr.write("schedule.py: {}\n".format(exc))
        return EXIT_LOCKED
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(cfg["workspace"], "logs", "run-{}.log".format(run_id))
    log = RunLog(log_path, secrets_of(env))
    t0 = clock()
    budget = args.max_minutes * 60 if args.max_minutes else None
    version = version_of()
    rollup = {"schema": STATUS_SCHEMA, "workspace_warnings": warnings, "streams": {}}
    skipped, failed = [], False
    try:
        log("run {} for {} stream(s), today {} (UTC), code {}".format(
            run_id, len(plans), today, (version or {}).get("sha")))
        for w in warnings:
            log("warning: " + w)
        for s, plan in plans:
            records = []
            senv = stream_env(cfg, s, env)
            if "TF_PLUGIN_CACHE_DIR" in cfg["env"] or "TF_PLUGIN_CACHE_DIR" in s["env"]:
                os.makedirs(senv["TF_PLUGIN_CACHE_DIR"], exist_ok=True)
            for win in plan:
                if budget is not None and clock() - t0 >= budget:
                    attempt = {"at": iso(now()), "result": "skipped_time_budget",
                               "reason": win["reason"], "from": win["from"],
                               "to": win["to"], "error": "--max-minutes {} reached "
                               "before this window started".format(args.max_minutes)}
                    skipped.append("{}:{}".format(s["id"], win["month"]))
                    log("skip {} {}: time budget reached".format(s["id"], win["month"]))
                else:
                    log("stream {} window {} ({})".format(s["id"], win["month"],
                                                          win["reason"]))
                    attempt = run_window(cfg, s, win, log, senv, run=run, now=now,
                                         clock=clock, gather_script=gather_script)
                    log("stream {} window {}: {}{} in {}s".format(
                        s["id"], win["month"], attempt["result"],
                        " - " + attempt["error"] if attempt.get("error") else "",
                        attempt.get("duration_s")))
                records.append((win, attempt))
            results = [a["result"] for _, a in records]
            result = "ok" if all(r == "ok" for r in results) else (
                "skipped" if all(r == "skipped_time_budget" for r in results)
                else "failed")
            if result != "ok":
                failed = True
            status = _update_stream_status(cfg, s, run_id, log_path, started,
                                           records, version, result, now)
            rollup["streams"][s["id"]] = {
                "result": result, "status": rel(cfg, stream_status_path(cfg, s)),
                "windows": {w["month"]: a["result"] for w, a in records},
                "last_success_at": max(
                    (e["last_success"]["at"] for e in status["windows"].values()
                     if e.get("last_success")), default=None)}
        try:
            apply_retention(cfg, today, log)
        except OSError as exc:
            log("warning: retention: {}".format(exc))
    finally:
        finished = now()
        code = EXIT_FAILED if failed else EXIT_OK
        top_path = os.path.join(cfg["workspace"], "status.json")
        previous = read_json(top_path, {}) or {}
        streams_roll = dict(previous.get("streams") or {})
        streams_roll.update(rollup["streams"])
        rollup.update({"updated_at": iso(finished), "streams": streams_roll,
                       "code_version": version,
                       "last_run": {"run_id": run_id, "started_at": iso(started),
                                    "finished_at": iso(finished),
                                    "today": today.isoformat(),
                                    "result": "ok" if code == EXIT_OK else "failed",
                                    "exit_code": code,
                                    "log": rel(cfg, log_path),
                                    "max_minutes": args.max_minutes,
                                    "skipped_time_budget": skipped,
                                    "backfill": args.backfill}})
        write_json(top_path, rollup)
        log("run {} finished: exit {} in {:.0f}s".format(run_id, code, clock() - t0))
        log.close()
        release_lock(lock)
    return code


# --- the reader --------------------------------------------------------------------

def reader_period(args, today):
    """(from, to) dates of the period `latest` is asked about."""
    given = [x for x in (args.month, args.last_days, args.ts_from) if x is not None]
    if len(given) != 1 or (args.ts_from is None) != (args.ts_to is None):
        raise ConfigError("give exactly one of --month YYYY-MM, --last-days N, "
                          "or --from D --to D")
    if args.month:
        return month_bounds(parse_month(args.month, "--month"))
    if args.last_days is not None:
        if args.last_days < 1:
            raise ConfigError("--last-days must be at least 1")
        return today - datetime.timedelta(days=args.last_days - 1), today
    try:
        manifest_mod.check_window(args.ts_from, args.ts_to, "--from", "--to")
    except ValueError as exc:
        raise ConfigError(str(exc))
    return (datetime.date.fromisoformat(args.ts_from),
            datetime.date.fromisoformat(args.ts_to))


def _covers(windows, lo, hi):
    """True when the union of `windows` [(from, to)] covers every day lo..hi."""
    spans = sorted((datetime.date.fromisoformat(a[:10]), datetime.date.fromisoformat(b[:10]))
                   for a, b in windows)
    nxt = lo
    for a, b in spans:
        if a > nxt:
            break
        nxt = max(nxt, b + datetime.timedelta(days=1))
        if nxt > hi:
            return True
    return nxt > hi


def store_freshness(cfg, stream, entry, store, now, max_age_hours):
    """Freshness of one month store from the stream's status entry."""
    out = {"store": store, "exists": os.path.isfile(store), "last_success_at": None,
           "age_hours": None, "final": False, "validate": None,
           "last_attempt": None, "problems": []}
    if not entry or not entry.get("last_success"):
        out["problems"].append("no successful scheduled run recorded for {}".format(
            os.path.basename(store)))
        if not out["exists"]:
            out["problems"].append("store {} does not exist".format(store))
        return out
    success = entry["last_success"]
    at = parse_iso(success.get("at"))
    out["last_success_at"] = success.get("at")
    out["validate"] = (success.get("validate") or {}).get("result")
    if at:
        out["age_hours"] = round((now - at).total_seconds() / 3600.0, 1)
        month_end = datetime.date.fromisoformat(entry["to"])
        out["final"] = at.date() > month_end
    last = entry.get("last_attempt") or {}
    out["last_attempt"] = {"at": last.get("at"), "result": last.get("result"),
                           "error": last.get("error")}
    if not out["exists"]:
        out["problems"].append("store {} does not exist".format(store))
    if last.get("result") not in (None, "ok") and last.get("at", "") > success.get("at", ""):
        out["problems"].append("the last run of {} failed ({}: {})".format(
            entry["month"], last.get("result"), last.get("error")))
    if not out["final"] and out["age_hours"] is not None \
            and out["age_hours"] > max_age_hours:
        out["problems"].append(
            "{} was last gathered {} h ago (more than {} h) and the month is "
            "not closed".format(entry["month"], out["age_hours"], max_age_hours))
    if out["validate"] != "OK":
        out["problems"].append("the last gather of {} did not record RESULT: OK".format(
            entry["month"]))
    return out


def latest(cfg, stream_id, lo, hi, now, max_age_hours=None, accept_stale=False,
           windows_of=store_windows):
    """Which store(s) to read for [lo, hi] and whether they may be read.
    Returns a JSON-able dict; `ok` False carries `refusal`."""
    stream = select_streams(cfg, [stream_id])[0]
    max_age = max_age_hours or stream["stale_hours"]
    status = read_json(stream_status_path(cfg, stream), {}) or {}
    entries = status.get("windows") or {}
    months = months_between(lo, hi)
    out = {"stream": stream["id"], "title": stream["title"], "kind": stream["kind"],
           "target": stream["target"], "period": {"from": lo.isoformat(),
                                                  "to": hi.isoformat()},
           "max_age_hours": max_age, "single_store": False, "read": [],
           "stores": [], "ok": False, "refusal": None, "overridden": []}
    if stream["kind"] == "repo":
        scope = ["--project", stream["owner"], "--repo", stream["repo"]]
    else:
        project = manifest_project(stream)
        scope = ["--project", project] if project else []
    # One store covers the whole period when its gathered windows do (the
    # month's store with the carried days of the previous month).
    last_store = store_path(cfg, stream, hi)
    if _covers(windows_of(last_store), lo, hi):
        used = [(month_key(hi), last_store, lo, hi)]
        out["single_store"] = True
    else:
        used = []
        for first in months:
            m_lo, m_hi = month_bounds(first)
            used.append((month_key(first), store_path(cfg, stream, first),
                         max(lo, m_lo), min(hi, m_hi)))
        out["single_store"] = len(used) == 1
    problems = []
    for month, store, frm, to in used:
        fresh = store_freshness(cfg, stream, entries.get(month), store, now, max_age)
        fresh["month"] = month
        fresh["covers"] = {"from": frm.isoformat(), "to": to.isoformat()}
        out["stores"].append(fresh)
        problems.extend(fresh["problems"])
        out["read"].append({"store": store, "from": frm.isoformat(),
                            "to": to.isoformat(), "view": [
                                "python3", VIEW_SCRIPT, "--store", store, *scope,
                                "--from", frm.isoformat(), "--to", to.isoformat(),
                                "--out", "VIEW.json"]})
    if not out["single_store"]:
        problems_split = ("the period {}..{} spans {} month stores and no one store "
                          "holds all of it: read each month on its own, or gather "
                          "the whole period into its own store (USAGE 5.3 "
                          "roll-ups)".format(lo, hi, len(used)))
        out["split_note"] = problems_split
    if problems and not accept_stale:
        out["refusal"] = "; ".join(problems)
        return out
    out["overridden"] = problems
    out["ok"] = True
    return out


# --- status ------------------------------------------------------------------------

def cmd_status(cfg, args, now=utc_now):
    top = read_json(os.path.join(cfg["workspace"], "status.json"), {}) or {}
    if args.json:
        sys.stdout.write(json.dumps(top, indent=2, sort_keys=True) + "\n")
        return EXIT_OK if (top.get("last_run") or {}).get("exit_code") == 0 else EXIT_FAILED
    lines = []
    last = top.get("last_run") or {}
    lines.append("last run: {} {} (exit {}), log {}".format(
        last.get("run_id", "never"), last.get("result", "-"),
        last.get("exit_code", "-"), last.get("log", "-")))
    t = now()
    for s in cfg["streams"]:
        st = read_json(stream_status_path(cfg, s), {}) or {}
        lines.append("{}{}: {}".format(s["id"], "" if s["enabled"] else " (disabled)",
                                       (st.get("last_run") or {}).get("result", "never run")))
        for month, e in sorted((st.get("windows") or {}).items())[-3:]:
            ok = e.get("last_success") or {}
            at = parse_iso(ok.get("at"))
            age = "{:.1f} h ago".format((t - at).total_seconds() / 3600) if at else "-"
            lines.append("  {}: last success {} ({}), last attempt {}{}".format(
                month, ok.get("at", "never"), age,
                (e.get("last_attempt") or {}).get("result"),
                ", " + str(e["last_attempt"]["error"])
                if (e.get("last_attempt") or {}).get("error") else ""))
    sys.stdout.write("\n".join(lines) + "\n")
    return EXIT_OK if last.get("exit_code") == 0 else EXIT_FAILED


def cmd_check(cfg, isdir=os.path.isdir):
    lines = ["streams file: " + cfg["path"], "workspace: " + cfg["workspace"],
             "people registry: " + cfg["people"]]
    for s in cfg["streams"]:
        lines.append("stream {} ({}): {} {}, cadence {}, carry {} day(s){}".format(
            s["id"], s["title"], s["kind"], s["target"], "+".join(s["cadence"]),
            s["carry_previous_days"], "" if s["enabled"] else ", DISABLED"))
        if s["kind"] == "manifest" and not os.path.isfile(s["manifest"]):
            lines.append("  warning: manifest {} does not exist yet".format(s["manifest"]))
    for w in workspace_warnings(cfg, isdir=isdir):
        lines.append("warning: " + w)
    sys.stdout.write("\n".join(lines) + "\n")
    return EXIT_OK


# --- CLI ---------------------------------------------------------------------------

def _date_arg(text):
    try:
        manifest_mod.check_date(text, "--today")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    return datetime.date.fromisoformat(text)


USAGE_SYNOPSIS = """\
commands:
  run    --streams FILE [--stream ID]... [--backfill YYYY-MM[..YYYY-MM]] [--dry-run]
         [--max-minutes N] [--today YYYY-MM-DD] [--break-stale-lock]
  latest --streams FILE --stream ID (--month YYYY-MM | --last-days N | --from D --to D)
         [--max-age-hours H] [--accept-stale] [--today YYYY-MM-DD]
  status --streams FILE [--json]
  check  --streams FILE
"""


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Scheduled gather runner (run / status / check) and the "
                    "skill's store reader (latest). docs/SCHEDULING.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE_SYNOPSIS)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--streams", required=True, help="the streams file (JSON)")
    r = sub.add_parser("run", parents=[common],
                       help="gather every enabled stream's windows for today")
    r.add_argument("--stream", action="append", default=[], metavar="ID",
                   help="run only this stream (repeatable; a disabled one too)")
    r.add_argument("--backfill", metavar="YYYY-MM[..YYYY-MM]",
                   help="gather these whole months instead of today's windows")
    r.add_argument("--dry-run", action="store_true",
                   help="print the plan and the exact commands; change nothing")
    r.add_argument("--max-minutes", type=int, default=None, metavar="N",
                   help="start no new window after N minutes (recorded as skipped)")
    r.add_argument("--today", type=_date_arg, default=None, metavar="YYYY-MM-DD",
                   help="plan as if today (UTC) were this date (default: today)")
    r.add_argument("--break-stale-lock", action="store_true",
                   help="remove a lock older than lock_stale_hours instead of "
                        "stopping (a lock younger than that is never broken)")
    lt = sub.add_parser("latest", parents=[common],
                        help="the store(s) holding a period, their freshness, "
                             "and whether they may be read")
    lt.add_argument("--stream", required=True, metavar="ID")
    lt.add_argument("--month", default=None, metavar="YYYY-MM")
    lt.add_argument("--last-days", type=int, default=None, metavar="N")
    lt.add_argument("--from", dest="ts_from", default=None, metavar="YYYY-MM-DD")
    lt.add_argument("--to", dest="ts_to", default=None, metavar="YYYY-MM-DD")
    lt.add_argument("--max-age-hours", type=int, default=None, metavar="H",
                    help="refuse an open month gathered longer ago than H hours "
                         "(default: the stream's stale_after_hours)")
    lt.add_argument("--accept-stale", action="store_true",
                    help="answer ok despite stale data or a failed last run "
                         "(the problems are listed under `overridden`)")
    lt.add_argument("--today", type=_date_arg, default=None, metavar="YYYY-MM-DD")
    st = sub.add_parser("status", parents=[common], help="the last results")
    st.add_argument("--json", action="store_true")
    sub.add_parser("check", parents=[common], help="check the streams file")
    args = p.parse_args(argv)
    if args.cmd == "run" and args.max_minutes is not None and args.max_minutes < 1:
        p.error("--max-minutes must be at least 1")
    return args


def main(argv=None, **seams):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg = load_streams(args.streams)
        if args.cmd == "run":
            return cmd_run(cfg, args, **seams)
        if args.cmd == "status":
            return cmd_status(cfg, args, **{k: v for k, v in seams.items()
                                             if k == "now"})
        if args.cmd == "check":
            return cmd_check(cfg)
        now = seams.get("now", utc_now)()
        today = args.today or now.date()
        lo, hi = reader_period(args, today)
        out = latest(cfg, args.stream, lo, hi, now, args.max_age_hours,
                     args.accept_stale)
    except ConfigError as exc:
        sys.stderr.write("schedule.py: error: {}\n".format(exc))
        return EXIT_USAGE
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    if out["ok"]:
        sys.stderr.write("latest: ok, {} store(s) for {}..{}{}\n".format(
            len(out["stores"]), lo, hi,
            " (overridden: {})".format("; ".join(out["overridden"]))
            if out["overridden"] else ""))
        return EXIT_OK
    sys.stderr.write("latest: REFUSED: {}\n".format(out["refusal"]))
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
