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
TMP_STORE = "{TMP_STORE}"   # placeholder for the working copy in a recompute command
GATHER_SCRIPT = os.path.join(SKILL_DIR, "gather.py")
VALIDATE_SCRIPT = os.path.join(SKILL_DIR, "validate.py")
VIEW_SCRIPT = os.path.join(SKILL_DIR, "view.py")
LINK_SCRIPT = os.path.join(SKILL_DIR, "link.py")
NEWSLETTER_SCRIPT = os.path.join(SKILL_DIR, "newsletter.py")

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


def _check_token_settings(value):
    """The streams file's optional `token` block: where `token renew` stores
    the token and which repository it reads to prove it (never the token)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("streams file: 'token' must be an object")
    allowed = {"store", "env_file", "keychain_service", "probe_repo", "app_id",
               "client_id", "installation_id", "private_key_file",
               "private_key_keychain_service"}
    extra = sorted(set(value) - allowed)
    if extra:
        raise ConfigError("streams file: token: unknown key(s) {} (allowed: {}; the "
                          "token or key itself never goes in this file)".format(
                              ", ".join(extra), ", ".join(sorted(allowed))))
    if value.get("store", "auto") not in ("auto", "keychain", "env-file", "app"):
        raise ConfigError("streams file: token.store must be auto, keychain, env-file "
                          "or app")
    for key in ("app_id", "installation_id"):
        if key in value and (isinstance(value[key], bool)
                             or not isinstance(value[key], int) or value[key] < 1):
            raise ConfigError("streams file: token.{} must be a positive integer".format(key))
    for key in ("client_id", "private_key_file", "private_key_keychain_service"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ConfigError("streams file: token.{} must be a non-empty string".format(key))
    if "private_key_file" in value and "BEGIN" in value["private_key_file"]:
        raise ConfigError("streams file: token.private_key_file is the PATH of the key "
                          "file; the key itself never goes in this file")
    if value.get("store") == "app":
        if not (value.get("app_id") or value.get("client_id")):
            raise ConfigError("streams file: token.store app needs token.client_id (or "
                              "token.app_id)")
        if not (value.get("private_key_file") or value.get("private_key_keychain_service")):
            raise ConfigError("streams file: token.store app needs token.private_key_file "
                              "or token.private_key_keychain_service")
    for key in ("env_file", "keychain_service", "probe_repo"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ConfigError("streams file: token.{} must be a non-empty string".format(key))
    if "probe_repo" in value:
        owner, _sep, repo = value["probe_repo"].partition("/")
        try:
            manifest_mod.check_owner(owner, "token.probe_repo owner")
            manifest_mod.check_repo(repo, "token.probe_repo repo")
        except ValueError as exc:
            raise ConfigError("streams file: {}".format(exc))
    return dict(value)


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
        "token": _check_token_settings(raw.get("token")),
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
        if "token" in s:
            # read only at the top level: here it would be silently ignored
            # and the default store used instead
            raise ConfigError("{}: 'token' is a top-level setting of the streams file "
                              "(one token for every stream); move it out of the "
                              "stream".format(what))
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


def auth_failure(stderr_text):
    """A one-line reason when a gather failed because GitHub refused the
    token (expired, revoked, over the organisation's lifetime limit, not
    authorized for its SSO, or missing), else None. Read from gather's own
    error lines (`error: GitHub API 401 ...`, its `message:` line)."""
    text = stderr_text or ""
    low = text.lower()
    if "set github_token" in low:
        return "no token in the environment"
    m = re.search(r"error: GitHub API (401|403)\b", text)
    if not m:
        return None
    msg = re.search(r"^\s*message: (.*)$", text, re.M)
    message = msg.group(1).strip() if msg else ""
    if m.group(1) == "401" or "bad credentials" in low:
        return "GitHub refused the token (401{}): expired, revoked or wrong".format(
            ": " + message if message else "")
    if "lifetime" in low or "expir" in low:
        return "GitHub refused the token (403: {}): its lifetime is over the " \
               "organisation's limit".format(message or "lifetime policy")
    if "x-github-sso" in low:
        return "the token is not authorized for the organisation's SAML SSO (403)"
    return None


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


def manifest_failed_members(stderr_text):
    """How many members a manifest gather reported failed (its summary line
    `gather: N of M member(s) failed ...`), else None."""
    m = re.search(r"^gather: (\d+) of \d+ member\(s\) failed", stderr_text or "", re.M)
    return int(m.group(1)) if m else None


def run_window(cfg, stream, win, log, env, run=run_subprocess, now=utc_now,
               clock=time.monotonic, gather_script=None, commands=None,
               token_source=None):
    """Gather one window into a copy of its store, validate the copy, swap it
    in. Returns the attempt record (result 'ok' or the failing step).
    `commands` [(part, from, to, ref_date, argv with TMP_STORE)] replaces the
    gather steps (an offline recompute); the store must then exist already."""
    store = win["store"]
    tmp = tmp_store_path(store)
    os.makedirs(os.path.dirname(store), exist_ok=True)
    started, t0 = now(), clock()
    attempt = {"at": iso(started), "result": None, "reason": win["reason"],
               "from": win["from"], "to": win["to"], "ref_date": win["ref_date"],
               "gathers": [], "validate": None, "error": None, "auth_error": None}
    secrets = secrets_of(env)
    all_err = []
    try:
        _remove(tmp)
        if os.path.exists(store):
            copy_store(store, tmp)
        elif commands is not None:
            raise ConfigError("store {} does not exist: nothing to recompute".format(
                store))
        steps = []
        if commands is not None:
            steps = [(frm, to, ref_date, part, [tmp if a == TMP_STORE else a
                                                for a in argv])
                     for part, frm, to, ref_date, argv in commands]
        else:
            if win["tail"]:
                steps.append((win["tail"]["from"], win["tail"]["to"], None, "tail",
                              None))
            steps.append((win["from"], win["to"], win["ref_date"], "month", None))
        project = None
        for frm, to, ref_date, part, argv in steps:
            mpath = None
            if stream["kind"] == "manifest" and argv is None:
                mpath = manifest_run_path(cfg, stream, frm, to)
                project = _write_run_manifest(cfg, stream, frm, to, mpath)
            if argv is None:
                argv = gather_argv(cfg, stream, frm, to, ref_date, tmp, mpath,
                                   gather_script=gather_script)
            # With the app route, a manifest gather that fails only some members
            # (e.g. ones reached after the installation token's hour) is
            # resumed with a fresh token (`gather --resume` re-gathers just the
            # members not ok for the window, with the whole member list), as
            # long as each pass leaves fewer members failed.
            run_argv, label, previous_failed, resumes = argv, part, None, 0
            while True:
                log("gather {} {} [{}..{}]: {}".format(stream["id"], label, frm, to,
                                                       shlex.join(run_argv)))
                step_env = env
                if token_source is not None and commands is None:
                    try:
                        minted = token_source.mint()
                    except AuthError as exc:
                        attempt["result"] = "token_failed"
                        attempt["error"] = "no installation token for the gather"
                        attempt["auth_error"] = str(exc)
                        return attempt
                    step_env = dict(env)
                    step_env.pop("GH_TOKEN", None)
                    step_env["GITHUB_TOKEN"] = minted["token"]
                    attempt["token"] = {"kind": "app",
                                        "minted": (attempt.get("token") or {}).get(
                                            "minted", 0) + 1,
                                        "expires_at": minted.get("expires_at"),
                                        "installation_id": minted.get("installation_id")}
                    secrets = sorted(set(secrets) | set(secrets_of(step_env)),
                                     key=len, reverse=True)
                g0 = clock()
                code, out, err = run(run_argv, step_env)
                took = clock() - g0
                out, err = redact(out, secrets), redact(err, secrets)
                all_err.append(err)
                for text in (out, err):
                    if text:
                        log("\n".join("  | " + ln for ln in text.splitlines()), raw=True)
                attempt["gathers"].append({"part": label, "from": frm, "to": to,
                                           "ref_date": ref_date, "exit": code,
                                           "duration_s": round(took, 1)})
                failed = manifest_failed_members(err) if code == 1 else None
                if (token_source is not None and commands is None
                        and stream["kind"] == "manifest" and failed
                        and resumes < MAX_MANIFEST_RESUMES
                        and (previous_failed is None or failed < previous_failed)):
                    previous_failed, resumes = failed, resumes + 1
                    run_argv = argv + ["--resume"]
                    label = "{} (resume {})".format(part, resumes)
                    log("gather {}: {} member(s) failed; resuming with a fresh "
                        "installation token".format(stream["id"], failed))
                    continue
                break
            if code != 0:
                attempt["result"] = "gather_failed"
                attempt["error"] = "gather exited {} ({} {}..{})".format(
                    code, label, frm, to)
                auth = auth_failure(err)
                if auth and token_source is not None and took >= 55 * 60:
                    auth += ("; the gather ran {:.0f} minutes, longer than the "
                             "installation token's hour (docs/SCHEDULING.md 7)".format(
                                 took / 60))
                attempt["auth_error"] = auth
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
        if attempt.get("auth_error") is None:
            attempt["auth_error"] = auth_failure("\n".join(all_err)) \
                if attempt["result"] == "gather_failed" else None
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
            isdir=os.path.isdir, http_get=None, api=None, sign=None, security=None,
            **_ignored):
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
    settings = token_settings(cfg)
    token_source = None
    if settings["store"] == "app":
        # a fresh installation token per gather; none is needed in the environment
        token_source = AppTokens(settings, api=api, now=now, sign=sign,
                                 security=security, isdir=isdir)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
    elif not (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")):
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
        # One identity call: the token's expiry (and an early word when GitHub
        # refuses it). A failure here never stops the run: gather decides.
        if token_source is not None:
            token = app_token_status(token_source, now=now)
        else:
            token = check_token(env.get("GITHUB_TOKEN") or env.get("GH_TOKEN"),
                                http_get or default_http_get, now=now)
            token["kind"] = "personal"
        rollup["token"] = token
        rollup["token_kind"] = token["kind"]
        rollup["token_expires_at"] = token.get("expires_at")
        rollup["token_days_left"] = token.get("days_left")
        log("token: " + describe_token(token))
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
                                         clock=clock, gather_script=gather_script,
                                         token_source=token_source)
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
                     if e.get("last_success")), default=None),
                "auth_error": next((a.get("auth_error") for _w, a in records
                                    if a.get("auth_error")), None)}
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


# --- the token: expiry, renewal, reminder -----------------------------------------
#
# A personal access token cannot be created through the API, so `token renew`
# scripts everything around the one manual step: it opens GitHub's
# new-token page prefilled through its DOCUMENTED query parameters (fine
# grained page: name, description, expires_in; no permission is needed, as
# every token reads public repositories; the classic page documents none, so
# none are passed), reads the new token at a hidden prompt, checks it with
# the API, and only then replaces the stored one. The token never appears in
# argv, a log or a status file.

API = "https://api.github.com"
TOKEN_PAGE = "https://github.com/settings/personal-access-tokens/new"
CLASSIC_TOKEN_PAGE = "https://github.com/settings/tokens/new"
TOKEN_NAME = "activity-overview read-only"
TOKEN_DESCRIPTION = ("Nightly activity gather: reads public repositories only. "
                     "Replaced every week (docs/SCHEDULING.md 7.1).")
TOKEN_DAYS = 7
TOKEN_WARN_DAYS = 2
DEFAULT_ENV_FILE = "~/.config/activity-overview/token.env"
DEFAULT_KEYCHAIN_SERVICE = "activity-gh-token"
LAUNCH_LABEL = "local.activity-overview.gather"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")


def token_page_url(days=TOKEN_DAYS, classic=False):
    if classic:
        return CLASSIC_TOKEN_PAGE
    return TOKEN_PAGE + "?" + urllib.parse.urlencode(
        {"name": TOKEN_NAME, "description": TOKEN_DESCRIPTION, "expires_in": days})


def parse_expiry(value):
    """The `github-authentication-token-expiration` header as a UTC datetime,
    or None. Its format is not documented; observed forms are
    `YYYY-MM-DD HH:MM:SS UTC` and `YYYY-MM-DD HH:MM:SS +0000` (any offset), and an
    ISO timestamp is accepted too."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    return parse_iso(text.replace(" ", "T", 1) if "T" not in text else text)


def default_http_get(url, token, timeout=20):
    """One authenticated GET to api.github.com: (status, headers, body text);
    (None, {}, reason) when it could not be sent."""
    import urllib.error
    import urllib.request
    if not url.startswith(API + "/"):
        raise ValueError("the token is only sent to " + API)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "activity-overview-schedule", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, {k.lower(): v for k, v in resp.headers.items()},
                    resp.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        return (err.code, {k.lower(): v for k, v in (err.headers or {}).items()},
                err.read(65536).decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        return None, {}, str(exc)


def _api_message(body):
    try:
        return (json.loads(body) or {}).get("message") or ""
    except (ValueError, AttributeError):
        return (body or "")[:200]


def check_token(token, http_get, probe=None, now=utc_now):
    """Ask GitHub about a token: identity and expiry (one call), and with
    `probe` ('owner/repo') a read of that repository, so an organisation's
    lifetime policy refuses it at once. Never returns the token.
    {ok, login, expires_at, days_left, error, probe, checked_at}."""
    out = {"ok": False, "login": None, "expires_at": None, "days_left": None,
           "error": None, "probe": None, "checked_at": iso(now())}
    if not token:
        out["error"] = "no token"
        return out
    status, headers, body = http_get(API + "/user", token)
    if status is None:
        out["error"] = "GitHub could not be reached ({})".format(body)
        return out
    if status != 200:
        out["error"] = "GitHub refused the token ({}{})".format(
            status, ": " + _api_message(body) if _api_message(body) else "")
        return out
    try:
        out["login"] = (json.loads(body) or {}).get("login")
    except ValueError:
        pass
    expires = parse_expiry(headers.get("github-authentication-token-expiration"))
    if expires:
        out["expires_at"] = iso(expires)
        out["days_left"] = round((expires - now()).total_seconds() / 86400.0, 1)
    if probe:
        status, _h, body = http_get("{}/repos/{}".format(API, probe), token)
        out["probe"] = {"repo": probe, "status": status}
        if status != 200:
            out["error"] = "reading {} failed ({}{})".format(
                probe, status, ": " + _api_message(body) if _api_message(body) else "")
            return out
    out["ok"] = True
    return out


def describe_token(info):
    if info.get("kind") == "app":
        who = "app {} installation {}{}".format(
            info.get("client_id") or info.get("app_id"), info.get("installation_id"),
            " on " + info["account"] if info.get("account") else "")
        if info.get("error"):
            return "{}: {}".format(who, info["error"])
        return "{} - installation token minted, expires {} (one hour; a new one is " \
               "minted for every gather)".format(who, info.get("expires_at"))
    if info.get("error"):
        return info["error"]
    if info.get("expires_at"):
        return "{} - expires {} ({} day(s) left)".format(
            info.get("login"), info["expires_at"], info["days_left"])
    return "{} - no expiry reported".format(info.get("login"))


def token_as_of(info, now):
    """A saved token entry with `days_left` recomputed from its `expires_at`
    at `now`: a reader long after the last run must not trust the days left
    that run saw (a personal token expires whether or not runs happen)."""
    if not info or info.get("kind") == "app":
        return info
    expires = parse_iso(info.get("expires_at"))
    if expires is None:
        return info
    return dict(info, days_left=round((expires - now).total_seconds() / 86400.0, 1))


def token_route(cfg, top=None):
    """'app' or 'personal': the last run's token kind, else the streams file's."""
    return ((top or {}).get("token") or {}).get("kind") or (
        "app" if token_settings(cfg)["store"] == "app" else "personal")


def auth_advice(cfg, top=None):
    """What to run when GitHub refused a token, for the route in use."""
    if token_route(cfg, top) == "app":
        return "run `schedule.py token app-check` (docs/SCHEDULING.md 7.1)"
    return "run `schedule.py token renew` (docs/SCHEDULING.md 7.2)"


def token_warning(info):
    """A one-line warning when the token is refused or expires within
    TOKEN_WARN_DAYS days, else None."""
    if not info:
        return None
    if info.get("kind") == "app":
        # no expiry to warn about: every gather mints its own one-hour token
        if info.get("error"):
            return "the app could not mint a token: {} - run `schedule.py token " \
                   "app-check` (docs/SCHEDULING.md 7)".format(info["error"])
        return None
    if info.get("error") and "refused" in info["error"]:
        return "the token was refused: {} - run `schedule.py token renew`".format(
            info["error"])
    left = info.get("days_left")
    if left is not None and left <= 0:
        return ("the token expired {} - run `schedule.py token renew`".format(
            info.get("expires_at")))
    if left is not None and left <= TOKEN_WARN_DAYS:
        return ("the token expires {} ({} day(s) left) - run `schedule.py token "
                "renew` before then".format(info.get("expires_at"), left))
    return None


def token_settings(cfg, args=None):
    """Where the token is kept: {store: keychain|env-file, env_file,
    keychain_service, account}."""
    t = dict(cfg.get("token") or {})
    store = getattr(args, "store", None) or t.get("store", "auto")
    if store == "auto":
        store = "keychain" if sys.platform == "darwin" else "env-file"
    # Every path comes back absolute, so the runner, `token renew` and the
    # wrappers (`token where`) name the same file whatever their working
    # directory: a streams-file path relative to the workspace, like the
    # file's other paths; a --env-file given on the command line relative to
    # the current directory.
    workspace = cfg.get("workspace") or os.getcwd()
    if getattr(args, "env_file", None):
        env_file = os.path.abspath(os.path.expanduser(args.env_file))
    else:
        env_file = _resolve(workspace, t.get("env_file") or DEFAULT_ENV_FILE)
    return {"store": store,
            "env_file": env_file,
            "keychain_service": t.get("keychain_service") or DEFAULT_KEYCHAIN_SERVICE,
            "account": os.environ.get("USER") or os.environ.get("LOGNAME") or "user",
            # the app route (store "app"): ids and where the key is, never the key
            "app_id": t.get("app_id"), "client_id": t.get("client_id"),
            "installation_id": t.get("installation_id"),
            "private_key_file": _resolve(workspace, t["private_key_file"])
            if t.get("private_key_file") else None,
            "private_key_keychain_service": t.get("private_key_keychain_service"),
            "workspace": cfg.get("workspace")}


def run_security(argv, stdin_text=None):
    """The `security` seam: (exit code, stdout). Its stdout is captured, never
    echoed."""
    proc = subprocess.run(argv, input=stdin_text, capture_output=True, text=True)
    return proc.returncode, proc.stdout


# --- the folder that holds a secret --------------------------------------------------
#
# The rule (token env file, app private key):
#   * group- or other-WRITABLE folder: refused, on every path. Whoever can
#     write the folder can replace or rename the file whatever its own mode
#     (a planted token, or a planted key the runner would sign with).
#   * group- or other-READABLE (not writable) folder: on a write path
#     (`token renew`, `token app-key`) the folder is tightened to 700 when it
#     is the user's own and not a shared location, and the output says so;
#     otherwise, and on every read path, a warning: the file's own mode 600
#     keeps its content private, the folder only exposes its names.
#   * never chmod a folder the user does not own, or a shared location (the
#     home directory, /, the temporary directories, the workspace): a
#     writable one is refused, a readable one warned about, with the fix.


class SecretDirError(ValueError):
    """The folder holding a secret is unsafe; the message names the fix."""


def _shared_locations(workspace=None):
    import tempfile
    places = {os.path.expanduser("~"), os.sep, "/tmp", "/var/tmp", tempfile.gettempdir()}
    if workspace:
        places.add(workspace)
    return {os.path.realpath(p) for p in places}


def _dir_of(path):
    return os.path.dirname(os.path.abspath(path))


# --- the whole path to a secret ------------------------------------------------------
#
# Checking only the folder that holds a secret is not enough: a mode-700
# /shared/private under a world-writable /shared can be renamed away by
# another user and replaced with their own folder and file. So every path to
# a secret is walked component by component from / (verify_secret_chain):
#
#   * every directory on the way, the root included, is owned by the user or
#     by root;
#   * no directory on the way is writable by group or others, except one with
#     the sticky bit (like /tmp) where the next component down is owned by the
#     user or root: the sticky bit lets only an entry's owner (or root) rename
#     or remove it, so nobody else can swap that entry;
#   * a symlink on the way is a substitution point too: its own directory is
#     checked like any other (above), the link must be owned by the user or
#     root, and its target is then walked from / in the same way (relative
#     targets from the link's directory; `..` is followed through the real,
#     already-walked directories, never lexically);
#   * the secret file itself must be a regular file owned by the user.
#
# On top of the chain, the folder that directly holds the secret keeps its
# stricter rule (check_secret_dir / secure_secret_dir below: never writable
# by group or others, even with the sticky bit).
#
# Deliberately NOT covered: access control lists (only the mode bits and
# owners are read); a directory owned by root but administered by someone
# else (root is trusted on the whole path); and the time between the check
# and the use. That window is narrowed where it is cheap: a secret the runner
# reads is opened once, without following a final symlink, and the open
# handle is checked (fstat: the same file the walk saw, regular, the user's);
# the wrappers source the env file by path, but the real path `token where`
# prints contains no symlink left to swap. Changing a directory on a checked
# path needs its owner (the user) or root, which the walk already trusts.

MAX_SYMLINKS = 40


def _uid():
    return os.getuid() if hasattr(os, "getuid") else None


def owner_of(path, st):
    """The owner (uid) of `path` as lstat/fstat reported it in `st`. The one
    seam for ownership: a test makes a single folder look foreign here while
    every other component keeps its real owner."""
    return st.st_uid


def _owner_ok(path, st, uid):
    return owner_of(path, st) in (uid, 0)


def _chain_error(what, path, message):
    return SecretDirError("{} ({}): {}".format(what, path, message))


def _swappable(what, original, folder, mode, entry):
    return _chain_error(
        what, original,
        "{} is writable by group or others (mode {:o}) without the sticky bit, so "
        "another user can rename {} and put their own in its place. Fix: `chmod go-w "
        "{}` if it is yours, or keep the file under folders only you and root can "
        "change (e.g. ~/.config/activity-overview)".format(
            folder, mode & 0o7777, entry, folder))


def verify_secret_chain(path, what="the token", allow_missing=False):
    """Walk `path` from / and apply the rule above. Returns (real_path, st):
    the path with every symlink resolved and the lstat of its last
    component (None when it does not exist and `allow_missing`). Raises
    SecretDirError naming the offending component and the fix."""
    import stat as statmod
    uid = _uid()
    original = os.path.abspath(os.path.expanduser(path))
    pending = [p for p in original.split(os.sep) if p]
    cur = os.sep
    st_cur = os.lstat(cur)
    if not _owner_ok(cur, st_cur, uid):
        raise _chain_error(what, original, "/ is owned by uid {}".format(
            owner_of(cur, st_cur)))
    links = 0
    st = st_cur
    while pending:
        name = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            cur = os.path.dirname(cur) or os.sep
            st_cur = st = os.lstat(cur)
            continue
        nxt = os.path.join(cur, name)
        dmode = st_cur.st_mode
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            if not allow_missing:
                raise _chain_error(what, original, "{} does not exist".format(nxt))
            # what is missing will be created in `cur`: it must be a folder
            # nobody else can swap the new entry in (sticky: the new entry is
            # ours, so it is protected)
            if dmode & 0o022 and not dmode & statmod.S_ISVTX:
                raise _swappable(what, original, cur, dmode, nxt)
            return os.path.join(nxt, *pending), None
        # may anyone but the user or root replace `nxt` inside `cur`?
        if dmode & 0o022:
            sticky = bool(dmode & statmod.S_ISVTX)
            if not sticky:
                raise _swappable(what, original, cur, dmode, nxt)
            if not _owner_ok(nxt, st, uid):
                raise _chain_error(
                    what, original,
                    "{} is writable by others with the sticky bit, which protects only "
                    "entries owned by you or root, but {} is owned by uid {}".format(
                        cur, nxt, owner_of(nxt, st)))
        if statmod.S_ISLNK(st.st_mode):
            links += 1
            if links > MAX_SYMLINKS:
                raise _chain_error(what, original, "too many symlinks")
            if not _owner_ok(nxt, st, uid):
                raise _chain_error(
                    what, original,
                    "{} is a symlink owned by uid {} (neither you nor root): its owner "
                    "can point it anywhere. Fix: use the real path, or a link you own".format(
                        nxt, owner_of(nxt, st)))
            target = os.readlink(nxt)
            if not os.path.isabs(target):
                target = os.path.join(cur, target)
            # the target is walked from / like the original path
            pending = [p for p in target.split(os.sep) if p] + pending
            cur = os.sep
            st_cur = os.lstat(cur)
            continue
        if pending and not statmod.S_ISDIR(st.st_mode):
            raise _chain_error(what, original, "{} is not a directory".format(nxt))
        if statmod.S_ISDIR(st.st_mode) and not _owner_ok(nxt, st, uid):
            raise _chain_error(
                what, original,
                "{} is owned by uid {} (neither you nor root), who could replace "
                "everything below it. Fix: keep the file under folders owned by you or "
                "root".format(nxt, owner_of(nxt, st)))
        cur, st_cur = nxt, st
    return cur, st


def open_secret_file(path, what="the token"):
    """Open a secret once, after verify_secret_chain: no final symlink is
    followed (O_NOFOLLOW), and the open handle must be the file the walk saw
    (same device and inode), a regular file, owned by the user. Returns the
    bytes. Raises SecretDirError."""
    import stat as statmod
    real, seen = verify_secret_chain(path, what)
    if not statmod.S_ISREG(seen.st_mode):
        raise _chain_error(what, path, "{} is not a regular file".format(real))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise _chain_error(what, path, "{} cannot be opened: {}".format(
            real, exc.strerror or exc))
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (seen.st_dev, seen.st_ino) or \
                not statmod.S_ISREG(st.st_mode):
            raise _chain_error(what, path, "{} changed while it was being checked".format(real))
        if owner_of(real, st) != _uid():
            raise _chain_error(what, path, "{} is owned by uid {}, not you".format(
                real, owner_of(real, st)))
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks), st
    finally:
        os.close(fd)


def env_file_for_wrapper(path):
    """The env file's real path, its whole path verified: what a wrapper may
    source. A missing file comes back as its real would-be path (the wrapper
    then reports it missing); an existing one must be a regular file of the
    user's own."""
    import stat as statmod
    real, st = verify_secret_chain(path, "the token", allow_missing=True)
    if st is not None:
        if not statmod.S_ISREG(st.st_mode):
            raise _chain_error("the token", path, "{} is not a regular file".format(real))
        if owner_of(real, st) != _uid():
            raise _chain_error("the token", path, "{} is owned by uid {}, not you".format(
                real, owner_of(real, st)))
    return real


def check_secret_dir(directory, workspace=None, what="the token"):
    """Read paths: verify the whole path to `directory` (verify_secret_chain),
    then raise SecretDirError when `directory` itself is group- or
    other-writable (even with the sticky bit); return a warning (or None)
    when it is only group- or other-readable."""
    try:
        directory, st = verify_secret_chain(directory, what, allow_missing=True)
    except FileNotFoundError:
        return None
    if st is None:
        return None                      # a missing folder: the file check says so
    mode = st.st_mode & 0o777
    if mode & 0o022:
        raise SecretDirError(
            "{} is in {}, which is writable by group or others (mode {:o}): anyone who "
            "can write the folder can replace the file. Fix: `chmod 700 {}` if it is "
            "your own private folder, or move the file to one such as "
            "~/.config/activity-overview (mode 700)".format(what, directory, mode,
                                                             directory))
    if mode & 0o055:
        return ("{}'s folder {} is readable by group or others (mode {:o}); the file "
                "itself is private, but `chmod 700 {}` (or a private folder) hides its "
                "name too".format(what, directory, mode, directory))
    return None


def secure_secret_dir(directory, workspace=None, what="the token"):
    """Write paths: create `directory` (700) if missing; tighten an existing
    group/other-readable folder of the user's own to 700 unless it is a shared
    location. Returns the notes to print; raises SecretDirError for a folder
    that stays group/other-writable, and for any unsafe directory or symlink
    on the way to it (verify_secret_chain; ancestors are never chmod-ed)."""
    directory, st = verify_secret_chain(directory, what, allow_missing=True)
    if st is None:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)       # makedirs' mode is filtered by the umask
        verify_secret_chain(directory, what)
        return []
    import stat as statmod
    if not statmod.S_ISDIR(st.st_mode):
        raise _chain_error(what, directory, "{} is not a directory".format(directory))
    mode = st.st_mode & 0o777
    if not mode & 0o077:
        return []
    shared = directory in _shared_locations(workspace)
    mine = owner_of(directory, st) == _uid()
    if mine and not shared:
        os.chmod(directory, 0o700)
        return ["tightened {} from mode {:o} to 700 (it holds {})".format(
            directory, mode, what)]
    why = ("a shared location" if shared else "not your own folder")
    if mode & 0o022:
        raise SecretDirError(
            "{} would go in {}, which is writable by group or others (mode {:o}) and is "
            "{}, so its mode is not changed: put the file in a private folder such as "
            "~/.config/activity-overview".format(what, directory, mode, why))
    return ["{}'s folder {} is readable by group or others (mode {:o}) and is {}, so "
            "its mode is not changed; a private folder such as ~/.config/activity-overview "
            "would hide the file's name too".format(what, directory, mode, why)]


_warned_dirs = set()


def warn_once(message, key):
    if key not in _warned_dirs:
        _warned_dirs.add(key)
        sys.stderr.write("warning: {}\n".format(message))


def write_env_file(path, token, workspace=None):
    """Replace the env file atomically: a new mode-600 file, fsynced, renamed
    over the old one, in a folder made private first (secure_secret_dir: a
    new folder is created 700; an existing group/other-readable one of the
    user's own is tightened to 700; a group/other-writable one that cannot be
    is refused before anything is written), after the whole path to it is
    verified (verify_secret_chain: no directory or symlink on the way that
    anyone but the user or root could swap). The file is written in the
    folder's real path, so no symlink is followed between check and write.
    Returns the notes to print."""
    notes = secure_secret_dir(_dir_of(path), workspace, "the token")
    real_dir, _st = verify_secret_chain(_dir_of(path), "the token")
    path = os.path.join(real_dir, os.path.basename(path))
    tmp = "{}.tmp-{}".format(path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("GITHUB_TOKEN={}\n".format(token))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    return notes


def store_token(settings, token, security=run_security):
    """Store the (already validated) token. The keychain is written through
    `security -i`, which reads its command from stdin: the token goes through
    a pipe, never into an argv or a process listing; it is then read back and
    compared."""
    if settings["store"] == "env-file":
        return write_env_file(settings["env_file"], token, settings.get("workspace"))
    keychain_write_checked(settings["account"], settings["keychain_service"], token,
                           security)
    return []


class KeychainWriteError(RuntimeError):
    """A keychain write that did not read back. `state` says what the item
    holds now: 'restored' (the previous value, read back), 'unknown' (it may
    hold a partial or different value: check it), or 'unchanged' (security
    refused before writing anything that read back as changed)."""

    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


def _keychain_get(acct, svc, security):
    code, out = security(["security", "find-generic-password", "-a", acct, "-s", svc, "-w"])
    return out.strip() if code == 0 and out.strip() else None


def _keychain_put(acct, svc, value, security):
    """`add-generic-password -U` through `security -i` (stdin): the value is
    never in an argv or the process listing."""
    code, _out = security(["security", "-i"], "add-generic-password -U -a {} -s {} -T "
                          "/usr/bin/security -w {}\n".format(acct, svc, value))
    return code


def keychain_write_checked(acct, svc, value, security=run_security):
    """Write `value` to the keychain item and read it back. `-U` replaces an
    existing item before the read-back can tell whether the write is right,
    so the previous value is read first and, when the read-back differs,
    written back the same way (stdin, never a command line) and read back
    again. Raises KeychainWriteError saying what the item holds then."""
    for name, v in (("account", acct), ("keychain service", svc)):
        if not re.fullmatch(r"[A-Za-z0-9._@-]+", v):
            raise ConfigError("the {} {!r} must be letters, digits, '.', '_', '@' "
                              "or '-' to be stored safely".format(name, v))
    previous = _keychain_get(acct, svc, security)
    code = _keychain_put(acct, svc, value, security)
    now_held = _keychain_get(acct, svc, security)
    if code == 0 and now_held == value:
        return
    what = ("security could not store it (exit {})".format(code) if code != 0 else
            "the keychain item {!r} did not read back as written".format(svc))
    if now_held == previous:
        raise KeychainWriteError(what + "; the item still holds its previous value",
                                 "unchanged")
    if previous is not None and _keychain_put(acct, svc, previous, security) == 0 \
            and _keychain_get(acct, svc, security) == previous:
        raise KeychainWriteError(what + "; the previous value was written back and reads "
                                 "back correctly", "restored")
    raise KeychainWriteError(
        what + "; the item {!r} may now hold a different or partial value: check it "
        "(`schedule.py token status`, or Keychain Access) before the next run".format(svc),
        "unknown")


def read_stored_token(settings, security=run_security):
    """The stored personal token, or None when there is none. The env file is
    opened once after its whole path is verified (open_secret_file); an
    unsafe path raises SecretDirError."""
    if settings["store"] == "env-file":
        if not os.path.lexists(settings["env_file"]):
            return None
        data, _st = open_secret_file(settings["env_file"], "the token")
        for line in data.decode("utf-8", "replace").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() in ("GITHUB_TOKEN", "GH_TOKEN"):
                return value.strip().strip("'\"")
        return None
    code, out = security(["security", "find-generic-password", "-a",
                          settings["account"], "-s", settings["keychain_service"], "-w"])
    return out.strip() if code == 0 and out.strip() else None


def probe_repo(cfg):
    """The repository `token renew` reads to prove the token is accepted:
    the streams file's `token.probe_repo`, else the first enabled single
    repository stream."""
    t = cfg.get("token") or {}
    if t.get("probe_repo"):
        return t["probe_repo"]
    for s in cfg["streams"]:
        if s["enabled"] and s["kind"] == "repo":
            return s["target"]
    return None


def default_open_browser(url):
    import webbrowser
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - no browser is a normal case
        return False


def default_prompt(text):
    import getpass
    return getpass.getpass(text)


def default_launch(argv):
    """Run a short command (launchctl kickstart) and return its exit code."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True).returncode
    except OSError:
        return 127


# `token where --field NAME`: what the wrappers ask; the keys of token_settings
WHERE_FIELDS = {"store": "store", "env-file": "env_file",
                "keychain-service": "keychain_service", "account": "account"}


def wrapper_mismatch(cfg, settings):
    """When `token renew` stored the token somewhere other than what the
    streams file says (a --store / --env-file given on the command line), the
    wrappers, which read the streams file, will not find it: say so."""
    configured = token_settings(cfg)
    if settings["store"] != configured["store"]:
        return ("the streams file's token.store is {!r} but the token was stored with "
                "--store {}: the wrappers follow the streams file; set token.store to "
                "{!r} there".format(configured["store"], settings["store"],
                                    settings["store"]))
    if settings["store"] == "env-file" and \
            os.path.abspath(settings["env_file"]) != os.path.abspath(configured["env_file"]):
        return ("the token was written to {} but the streams file's token.env_file is {}: "
                "the wrappers read the streams file; set token.env_file to match".format(
                    settings["env_file"], configured["env_file"]))
    return None


def default_spawn(argv, env):
    """Start a detached process (the run after `token renew --kick`)."""
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# --- the app route: installation tokens minted per gather ---------------------------
#
# A GitHub App installed on the owner's own account (docs/SCHEDULING.md 7)
# needs no weekly step: for every gather the runner signs a short JWT with
# the app's private key (RS256, through the system `openssl`, so no package
# is needed), finds the installation, and asks for an installation token
# narrowed to read access. The token lasts one hour (GitHub's documented
# lifetime), so one is minted per gather invocation, never per run. The key
# stays in its mode-600 file (or the keychain) and goes to openssl as a file
# path or through a pipe; neither the key, the JWT nor a token reaches an
# argv, a log or a status file.

JWT_BACKDATE_S = 60       # GitHub: set iat 60 seconds in the past (clock drift)
JWT_LIFETIME_S = 540      # GitHub: exp no more than 10 minutes ahead; 9 leaves margin
MAX_MANIFEST_RESUMES = 4  # fresh-token `--resume` passes after a manifest gather


class AuthError(Exception):
    """The app could not produce a token; the message is plain and safe to
    record (it never holds the key, the JWT or a token)."""


def b64url(data):
    import base64
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_claims(issuer, now_epoch):
    return {"iat": int(now_epoch) - JWT_BACKDATE_S,
            "exp": int(now_epoch) + JWT_LIFETIME_S, "iss": str(issuer)}


def openssl_sign(key, data):
    """RS256 signature of `data` with `openssl dgst -sha256 -sign` (documented
    by OpenSSL and by LibreSSL, which macOS ships). `key` is ("file", path),
    or ("pem", bytes) handed to openssl through a pipe (/dev/fd/N), so the key
    is never written to disk or put on a command line."""
    try:
        if key[0] == "file":
            proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key[1]],
                                  input=data, capture_output=True)
        else:
            r, w = os.pipe()
            try:
                os.write(w, key[1])
                os.close(w)
                w = None
                proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign",
                                       "/dev/fd/{}".format(r)], input=data,
                                      capture_output=True, pass_fds=(r,))
            finally:
                os.close(r)
                if w is not None:
                    os.close(w)
    except FileNotFoundError:
        raise AuthError("the app route needs the `openssl` command; none was found")
    except OSError as exc:
        raise AuthError("openssl could not be run: {}".format(exc))
    if proc.returncode != 0 or not proc.stdout:
        first = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AuthError("openssl could not sign with the app's private key (exit {}{})"
                        .format(proc.returncode, ": " + first[0][:200] if first else ""))
    return proc.stdout


def make_jwt(issuer, key, now_epoch, sign=openssl_sign):
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")))
    payload = b64url(json.dumps(jwt_claims(issuer, now_epoch), separators=(",", ":")))
    signing_input = "{}.{}".format(header, payload)
    return "{}.{}".format(signing_input, b64url(sign(key, signing_input.encode("ascii"))))


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.realpath(path),
                                   os.path.realpath(folder)]) == os.path.realpath(folder)
    except ValueError:
        return False


def load_app_key(settings, security=run_security, isdir=os.path.isdir):
    """The app's private key, checked, as ("pem", bytes): from a mode-600 PEM
    file outside the workspace and any synced folder, whose whole path is
    verified and which is opened once (open_secret_file, so openssl never
    reopens it by path), or from the keychain item (base64 of the PEM, see
    `token app-key`)."""
    path = settings.get("private_key_file")
    if path:
        if not os.path.lexists(path):
            raise AuthError("the app's private key file {} does not exist".format(path))
        if settings.get("workspace") and _inside(path, settings["workspace"]):
            raise AuthError("the app's private key file {} is inside the workspace; "
                            "keep it outside (e.g. ~/.config/activity-overview/)".format(path))
        reasons = synced_folder_reasons(os.path.dirname(path), isdir=isdir)
        if reasons:
            raise AuthError("the app's private key file {} looks synced ({}); keep it "
                            "on a local, non-synced disk".format(path, "; ".join(reasons)))
        try:
            warn = check_secret_dir(_dir_of(path), settings.get("workspace"),
                                    "the app's private key")
        except SecretDirError as exc:
            raise AuthError(str(exc))
        if warn:
            warn_once(warn, ("key-dir", _dir_of(path)))
        try:
            pem, st = open_secret_file(path, "the app's private key")
        except SecretDirError as exc:
            raise AuthError(str(exc))
        mode = st.st_mode & 0o777
        if mode & 0o077:
            raise AuthError("the app's private key file {} is mode {:o}; it must be "
                            "600 (chmod 600 it)".format(path, mode))
        if b"PRIVATE KEY" not in pem[:64]:
            raise AuthError("{} is not a PEM private key".format(path))
        return ("pem", pem)
    svc = settings.get("private_key_keychain_service")
    if not svc:
        raise AuthError("no app private key configured (token.private_key_file or "
                        "token.private_key_keychain_service)")
    import base64
    import binascii
    code, out = security(["security", "find-generic-password", "-a",
                          settings["account"], "-s", svc, "-w"])
    if code != 0 or not out.strip():
        raise AuthError("no app private key in the keychain item {!r}".format(svc))
    try:
        pem = base64.b64decode(out.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise AuthError("the keychain item {!r} does not hold a key stored by "
                        "`token app-key`".format(svc))
    if b"PRIVATE KEY" not in pem:
        raise AuthError("the keychain item {!r} does not hold a PEM private key".format(svc))
    return ("pem", pem)


def default_api(method, url, bearer, body=None, timeout=30):
    """One request to api.github.com with a Bearer credential (a JWT or a
    token): (status, headers, body text); (None, {}, reason) when it could
    not be sent."""
    import urllib.error
    import urllib.request
    if not url.startswith(API + "/"):
        raise ValueError("credentials are only sent to " + API)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
        "User-Agent": "activity-overview-schedule", "X-GitHub-Api-Version": "2022-11-28",
        **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, {k.lower(): v for k, v in resp.headers.items()},
                    resp.read(262144).decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        return (err.code, {k.lower(): v for k, v in (err.headers or {}).items()},
                err.read(65536).decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        return None, {}, str(exc)


def _refusal(what, status, body):
    if status is None:
        return "{}: GitHub could not be reached ({})".format(what, body)
    msg = _api_message(body)
    return "{} ({}{})".format(what, status, ": " + msg if msg else "")


class AppTokens:
    """Mints installation tokens for the configured app. `mint()` returns
    {token, expires_at, app_id, client_id, installation_id, account,
    permissions}; `public(info)` is the same without the token."""

    def __init__(self, settings, api=None, now=utc_now, sign=None, security=None,
                 isdir=os.path.isdir):
        self.settings = settings
        self.api = api or default_api
        self.now = now
        self.sign = sign or openssl_sign
        self.security = security or run_security
        self.isdir = isdir
        self.minted = 0

    def jwt(self):
        key = load_app_key(self.settings, self.security, self.isdir)
        issuer = self.settings.get("client_id") or self.settings.get("app_id")
        return make_jwt(issuer, key, self.now().timestamp(), self.sign)

    def installation(self, jwt):
        inst_id = self.settings.get("installation_id")
        if inst_id:
            status, _h, body = self.api("GET", "{}/app/installations/{}".format(
                API, inst_id), jwt)
            if status == 404:
                raise AuthError("installation {} was not found for this app (is it "
                                "installed, and is token.installation_id right?)".format(
                                    inst_id))
            if status != 200:
                raise AuthError(_refusal("GitHub refused the app's JWT", status, body)
                                + "; check token.client_id / app_id, the key and the clock")
            return json.loads(body)
        status, _h, body = self.api("GET", API + "/app/installations?per_page=100", jwt)
        if status != 200:
            raise AuthError(_refusal("GitHub refused the app's JWT", status, body)
                            + "; check token.client_id / app_id, the key and the clock")
        installs = json.loads(body) or []
        if len(installs) != 1:
            raise AuthError(
                "the app is not installed anywhere: install it on your account"
                if not installs else
                "the app has {} installations ({}); set token.installation_id".format(
                    len(installs), ", ".join("{} on {}".format(
                        i.get("id"), (i.get("account") or {}).get("login"))
                        for i in installs)))
        return installs[0]

    def mint(self):
        jwt = self.jwt()
        inst = self.installation(jwt)
        # read access only: every permission the installation holds, at `read`
        perms = {k: "read" for k in sorted(inst.get("permissions") or {})} \
            or {"metadata": "read"}
        status, _h, body = self.api("POST", "{}/app/installations/{}/access_tokens".format(
            API, inst["id"]), jwt, {"permissions": perms})
        if status != 201:
            raise AuthError(_refusal("GitHub refused the installation token", status, body))
        data = json.loads(body)
        if not data.get("token"):
            raise AuthError("GitHub answered without an installation token")
        self.minted += 1
        return {"token": data["token"], "expires_at": data.get("expires_at"),
                "app_id": self.settings.get("app_id") or inst.get("app_id"),
                "client_id": self.settings.get("client_id"),
                "installation_id": inst.get("id"),
                "account": (inst.get("account") or {}).get("login"),
                "permissions": data.get("permissions") or perms}

    @staticmethod
    def public(info):
        return {k: v for k, v in info.items() if k != "token"}


def app_token_status(source, now=utc_now):
    """Mint once, keep only what is not secret: the status `token` entry."""
    out = {"kind": "app", "ok": False, "checked_at": iso(now()), "error": None,
           "app_id": source.settings.get("app_id"),
           "client_id": source.settings.get("client_id"),
           "installation_id": source.settings.get("installation_id"),
           "account": None, "expires_at": None, "days_left": None}
    try:
        info = source.mint()
    except AuthError as exc:
        out["error"] = str(exc)
        return out
    out.update(AppTokens.public(info))
    out["ok"] = True
    return out


def app_check(source, probe, api, now=utc_now):
    """Mint an installation token and prove it against the real target:
    rate limit, a REST read, a GraphQL read and a search of `probe`. Prints
    what GitHub answered for each, never the token. Usable = minted, and the
    REST and GraphQL reads succeed (search only feeds the newcomers check)."""
    out = sys.stdout.write
    try:
        info = source.mint()
    except AuthError as exc:
        out("installation token: FAILED - {}\n".format(exc))
        out("RESULT: NOT USABLE (no installation token; see docs/SCHEDULING.md 7)\n")
        return EXIT_FAILED
    token = info.pop("token")
    out("installation token: minted for app {} installation {} on {}, expires {}, "
        "permissions {}\n".format(info.get("client_id") or info.get("app_id"),
                                  info.get("installation_id"), info.get("account"),
                                  info.get("expires_at"),
                                  json.dumps(info.get("permissions"), sort_keys=True)))
    owner, _sep, name = probe.partition("/")
    results = {}

    def report(label, status, body, ok, detail):
        results[label] = ok
        out("{}: {} - {}\n".format(label, "OK" if ok else "FAILED",
                                   "HTTP {}; {}".format(status, detail)
                                   if status is not None else detail))

    status, _h, body = api("GET", API + "/rate_limit", token)
    try:
        res = (json.loads(body) or {}).get("resources") or {}
        detail = ", ".join("{} {}/{}".format(k, res[k].get("remaining"), res[k].get("limit"))
                           for k in ("core", "graphql", "search") if k in res) \
            or _api_message(body)
    except (ValueError, AttributeError):
        detail = _api_message(body) or str(body)[:200]
    report("rate limit (REST GET /rate_limit)", status, body, status == 200, detail)

    status, _h, body = api("GET", "{}/repos/{}".format(API, probe), token)
    try:
        data = json.loads(body) or {}
    except ValueError:
        data = {}
    ok = status == 200 and data.get("full_name", "").lower() == probe.lower()
    report("REST read (GET /repos/{})".format(probe), status, body, ok,
           "{} ({}, default branch {})".format(data.get("full_name"), data.get("visibility"),
                                               data.get("default_branch"))
           if ok else _api_message(body) or str(body)[:200])

    query = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
             "{nameWithOwner defaultBranchRef{name} pullRequests(states:MERGED){totalCount}}"
             " rateLimit{limit remaining}}")
    status, _h, body = api("POST", API + "/graphql", token,
                           {"query": query, "variables": {"owner": owner, "name": name}})
    try:
        data = json.loads(body) or {}
    except ValueError:
        data = {}
    repo = (data.get("data") or {}).get("repository") or {}
    ok = status == 200 and not data.get("errors") and bool(repo)
    report("GraphQL read (repository {})".format(probe), status, body, ok,
           "{}: {} merged PRs; rate limit {}/{}".format(
               repo.get("nameWithOwner"), (repo.get("pullRequests") or {}).get("totalCount"),
               ((data.get("data") or {}).get("rateLimit") or {}).get("remaining"),
               ((data.get("data") or {}).get("rateLimit") or {}).get("limit"))
           if ok else "; ".join(e.get("message", "") for e in data.get("errors") or [])
           or _api_message(body) or str(body)[:200])

    status, _h, body = api("GET", "{}/search/issues?q=repo:{}+is:pr&per_page=1".format(
        API, probe), token)
    try:
        total = (json.loads(body) or {}).get("total_count")
    except ValueError:
        total = None
    report("search (GET /search/issues, newcomers check only)", status, body,
           status == 200, "{} PRs".format(total) if status == 200
           else _api_message(body) or str(body)[:200])
    usable = all(v for k, v in results.items() if k.startswith(("REST read", "GraphQL")))
    out("RESULT: {}\n".format(
        "USABLE - the nightly run can use the app route" if usable else
        "NOT USABLE - keep the personal token (token renew, docs/SCHEDULING.md 7.2)"))
    return EXIT_OK if usable else EXIT_FAILED


def app_key_import(settings, path, security=run_security):
    """Put the app's PEM private key into the keychain item
    token.private_key_keychain_service, base64-encoded on one line, through
    `security -i` (stdin), then read it back and compare."""
    import base64
    svc = settings.get("private_key_keychain_service")
    if not svc:
        sys.stderr.write("schedule.py: error: set token.private_key_keychain_service in "
                         "the streams file first\n")
        return EXIT_USAGE
    path = os.path.expanduser(path)
    # the key's folder is made private first (a write path, like `token renew`)
    try:
        notes = secure_secret_dir(_dir_of(path), settings.get("workspace"),
                                  "the app's private key")
    except SecretDirError as exc:
        sys.stderr.write("token app-key: {}\n".format(exc))
        return EXIT_FAILED
    for note in notes:
        sys.stderr.write("token app-key: {}\n".format(note))
    try:
        with open(path, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        sys.stderr.write("token app-key: cannot read {}: {}\n".format(path, exc.strerror))
        return EXIT_FAILED
    if b"PRIVATE KEY" not in pem:
        sys.stderr.write("token app-key: {} is not a PEM private key\n".format(path))
        return EXIT_FAILED
    value = base64.b64encode(pem).decode("ascii")
    try:
        keychain_write_checked(settings["account"], svc, value, security)
    except ConfigError as exc:
        sys.stderr.write("token app-key: {}\n".format(exc))
        return EXIT_FAILED
    except KeychainWriteError as exc:
        sys.stderr.write("token app-key: storing failed: {}; keep using "
                         "token.private_key_file\n".format(redact(str(exc), [value])))
        return EXIT_FAILED
    sys.stderr.write("token app-key: stored in the keychain item {!r}. Remove "
                     "token.private_key_file from the streams file to use it, and keep "
                     "or delete {} as you see fit\n".format(svc, path))
    return EXIT_OK


def cmd_token(cfg, args, http_get=None, prompt=None, open_browser=None,
              security=None, spawn=None, now=utc_now, api=None, sign=None,
              launch=None, **_ignored):
    http_get = http_get or default_http_get
    security = security or run_security
    settings = token_settings(cfg, args)
    out = sys.stdout.write
    if args.token_cmd == "kind":
        out(settings["store"] + "\n")
        return EXIT_OK
    if args.token_cmd == "where":
        # the wrappers' one source for where the token is: the same settings
        # `token renew` writes to (never the token itself)
        where = token_settings(cfg)
        value = where[WHERE_FIELDS[args.field]]
        if args.field == "env-file" and where["store"] == "env-file":
            # the wrappers source the file right after this: its whole path is
            # verified, and the real path (no symlink left) is what they get
            try:
                warn = check_secret_dir(_dir_of(value), where["workspace"])
                value = env_file_for_wrapper(value)
            except SecretDirError as exc:
                sys.stderr.write("schedule.py: error: {}\n".format(exc))
                return EXIT_FAILED
            if warn:
                sys.stderr.write("warning: {}\n".format(warn))
        out("{}\n".format(value))
        return EXIT_OK
    if args.token_cmd in ("app-check", "app-key") or (
            settings["store"] == "app" and args.token_cmd == "status"):
        if not (settings.get("client_id") or settings.get("app_id")) and \
                args.token_cmd != "app-key":
            sys.stderr.write("schedule.py: error: the streams file's token block has no "
                             "client_id / app_id (docs/SCHEDULING.md 7)\n")
            return EXIT_USAGE
        source = AppTokens(settings, api=api, now=now, sign=sign, security=security)
        if args.token_cmd == "app-key":
            return app_key_import(settings, args.import_path, security)
        if args.token_cmd == "status":
            info = app_token_status(source, now=now)
            out("token: {}\n".format(describe_token(info)))
            return EXIT_OK if info["ok"] else EXIT_FAILED
        return app_check(source, probe_repo(cfg) or "Azure/bicep-registry-modules",
                         api or default_api, now=now)
    if settings["store"] == "app" and args.token_cmd == "renew" and not args.store:
        sys.stderr.write("token renew: the streams file uses the app route (token.store "
                         "app), which needs no weekly renewal. For the personal-token "
                         "fallback pass --store keychain (or env-file) and set "
                         "token.store to match (docs/SCHEDULING.md 7.2)\n")
        return EXIT_USAGE
    if args.token_cmd == "status":
        if settings["store"] == "env-file":
            try:
                warn = check_secret_dir(_dir_of(settings["env_file"]),
                                        settings["workspace"])
            except SecretDirError as exc:
                out("token: REFUSED: {}\n".format(exc))
                return EXIT_FAILED
            if warn:
                out("WARNING: {}\n".format(warn))
        try:
            token = read_stored_token(settings, security)
        except SecretDirError as exc:
            out("token: REFUSED: {}\n".format(exc))
            return EXIT_FAILED
        if not token:
            out("no stored token ({})\n".format(
                settings["env_file"] if settings["store"] == "env-file"
                else "keychain item " + settings["keychain_service"]))
            return EXIT_FAILED
        info = check_token(token, http_get, now=now)
        out("token: {}\n".format(describe_token(info)))
        warn = token_warning(info)
        if warn:
            out("WARNING: {}\n".format(warn))
        return EXIT_OK if info["ok"] else EXIT_FAILED
    if args.token_cmd == "remind":
        top = read_json(os.path.join(cfg["workspace"], "status.json"), {}) or {}
        info = token_as_of(dict(top.get("token") or {}), now())
        refused = [sid for sid, e in (top.get("streams") or {}).items()
                   if e.get("auth_error")]
        msg = token_warning(info)
        if not msg and refused:
            msg = "GitHub refused the token for {} - {}".format(
                ", ".join(sorted(refused)), auth_advice(cfg, top))
        if msg:
            out(msg.replace('"', "'").replace("\\", "/") + "\n")
        return EXIT_OK
    # renew
    url = token_page_url(args.days, classic=args.classic)
    opened = (open_browser or default_open_browser)(url) if not args.no_browser else False
    sys.stderr.write("{} the new-token page:\n  {}\n".format(
        "Opened" if opened else "Open", url))
    sys.stderr.write(
        "Create the token there ({}): expiry {} days; {}. Then paste it here.\n".format(
            "classic: note '{}', scopes public_repo and read:project".format(TOKEN_NAME)
            if args.classic else "name and expiry are prefilled; resource owner: "
            "your own account; no permission needs adding (every token reads "
            "public repositories)", args.days,
            "the old token stays in place until the new one is checked"))
    token = (prompt or default_prompt)("New token (input hidden): ").strip()
    if not _TOKEN_RE.fullmatch(token):
        sys.stderr.write("token renew: that does not look like a token (letters, "
                         "digits and '_' only); nothing changed\n")
        return EXIT_FAILED
    info = check_token(token, http_get, probe=probe_repo(cfg), now=now)
    sys.stderr.write("token renew: {}\n".format(describe_token(info)))
    if not info["ok"]:
        sys.stderr.write("token renew: the new token was NOT stored; the old one "
                         "stays in place\n")
        return EXIT_FAILED
    if info["days_left"] is not None and info["days_left"] > 8:
        sys.stderr.write("token renew: warning: it lives {} days; organisations "
                         "with an 8-day limit will refuse it\n".format(info["days_left"]))
    try:
        notes = store_token(settings, token, security)
    except KeychainWriteError as exc:
        # say what the item holds now, never more than was checked
        sys.stderr.write("token renew: storing failed: {}\n".format(
            redact(str(exc), [token])))
        return EXIT_FAILED
    except (OSError, RuntimeError, ConfigError, SecretDirError) as exc:
        # the env file is replaced by one rename at the very end, and a
        # ConfigError / SecretDirError stops before any write: the old token
        # is untouched
        sys.stderr.write("token renew: storing failed: {}; the old token stays in "
                         "place\n".format(redact(str(exc), [token])))
        return EXIT_FAILED
    for note in notes or []:
        sys.stderr.write("token renew: {}\n".format(note))
    sys.stderr.write("token renew: stored in {}\n".format(
        settings["env_file"] if settings["store"] == "env-file"
        else "the keychain item " + settings["keychain_service"]))
    mismatch = wrapper_mismatch(cfg, settings)
    if mismatch:
        sys.stderr.write("token renew: WARNING: {}\n".format(mismatch))
    if args.kick:
        if sys.platform == "darwin" and not args.kick_here:
            code = (launch or default_launch)(["launchctl", "kickstart", "gui/{}/{}".format(
                os.getuid(), LAUNCH_LABEL)])
            if code == 0:
                sys.stderr.write("token renew: started the scheduled job ({})\n".format(
                    LAUNCH_LABEL))
            else:
                sys.stderr.write("token renew: launchctl kickstart exited {}: the "
                                 "scheduled job ({}) was NOT started (is it installed? "
                                 "docs/SCHEDULING.md 10)\n".format(code, LAUNCH_LABEL))
                return EXIT_FAILED
        else:
            env = dict(os.environ, GITHUB_TOKEN=token)
            (spawn or default_spawn)([sys.executable, os.path.abspath(__file__), "run",
                                      "--streams", cfg["path"]], env)
            sys.stderr.write("token renew: started a run in the background (its log: "
                             "{})\n".format(os.path.join(cfg["workspace"], "logs")))
    return EXIT_OK


# --- offline recompute ------------------------------------------------------------

# What `recompute --what` can redo, and the gather flag that does it (offline:
# from the stored records and the stream's clone, no API call). Only what
# gather offers: `gather.py --help` lists its `--recompute-*` flags.
RECOMPUTE_FLAGS = {"ownership": "--recompute-ownership"}


def recompute_commands(cfg, stream, what, windows, python=None, gather_script=None):
    """The recompute command for every gathered window of a store, oldest
    first, against the working copy (TMP_STORE)."""
    if stream["kind"] != "repo":
        raise ConfigError("stream {!r}: recompute works on a single-repository "
                          "stream only (gather's {} takes --owner/--repo)".format(
                              stream["id"], RECOMPUTE_FLAGS[what]))
    out = []
    for frm, to in windows:
        out.append(("recompute-" + what, frm, to, None, [
            python or sys.executable, gather_script or GATHER_SCRIPT,
            "--owner", stream["owner"], "--repo", stream["repo"],
            "--from", frm, "--to", to, "--store", TMP_STORE,
            "--clone-dir", os.path.join(stream_dir(cfg, stream), "clone"),
            RECOMPUTE_FLAGS[what]]))
    return out


def cmd_recompute(cfg, args, run=run_subprocess, now=utc_now, clock=time.monotonic,
                  version_of=code_version, gather_script=None, base_env=None,
                  isdir=os.path.isdir, **_ignored):
    """Recompute clone-derived facts of one month store after a code upgrade,
    on a copy, validated, then swapped in (exactly as a gather is)."""
    stream = select_streams(cfg, [args.stream])[0]
    first = parse_month(args.month, "--month")
    store = store_path(cfg, stream, first)
    windows = store_windows(store)
    commands = recompute_commands(cfg, stream, args.what, windows,
                                  gather_script=gather_script)
    if not windows:
        raise ConfigError("store {} holds no gathered window (gather it first)".format(
            store))
    lo, hi = month_bounds(first)
    win = {"month": month_key(first), "from": lo.isoformat(), "to": hi.isoformat(),
           "ref_date": None, "reason": "recompute-" + args.what, "store": store,
           "tail": None}
    tmp = tmp_store_path(store)
    if args.dry_run:
        lines = ["recompute {} {} ({}):".format(stream["id"], win["month"], args.what)]
        lines += ["  " + shlex.join([tmp if a == TMP_STORE else a for a in argv])
                  for _p, _f, _t, _r, argv in commands]
        lines.append("  " + shlex.join(validate_argv(stream, tmp)))
        lines.append("  then: swap {} -> {}".format(tmp, store))
        sys.stdout.write("\n".join(lines) + "\n")
        return EXIT_OK
    started = now()
    env = token_free_env(dict(os.environ if base_env is None else base_env))
    try:
        lock = acquire_lock(cfg["workspace"], started, cfg["lock_stale_hours"])
    except Locked as exc:
        sys.stderr.write("schedule.py: {}\n".format(exc))
        return EXIT_LOCKED
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(cfg["workspace"], "logs", "run-{}.log".format(run_id))
    log = RunLog(log_path, [])
    attempt = {"result": "error"}
    try:
        log("recompute {} for {} {} ({} window(s))".format(
            args.what, stream["id"], win["month"], len(commands)))
        attempt = run_window(cfg, stream, win, log, env, run=run, now=now,
                             clock=clock, commands=commands)
        attempt = dict(attempt, run_id=run_id, code_version=version_of(),
                       what=args.what, log=rel(cfg, log_path))
        path = stream_status_path(cfg, stream)
        status = read_json(path, {}) or {}
        entry = status.setdefault("windows", {}).setdefault(win["month"], {
            "month": win["month"], "from": win["from"], "to": win["to"],
            "store": rel(cfg, store), "store_prev": rel(cfg, store + ".prev")})
        entry["last_recompute"] = attempt
        write_json(path, status)
        log("recompute {} {}: {}{}".format(
            stream["id"], win["month"], attempt["result"],
            " - " + attempt["error"] if attempt.get("error") else ""))
    finally:
        log.close()
        release_lock(lock)
    return EXIT_OK if attempt["result"] == "ok" else EXIT_FAILED


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


def issue_cadence(stream, explicit=None, by_month=False):
    """The newsletter layout for this issue: as asked, else monthly for a
    whole month, else weekly when the stream's cadence includes it."""
    if explicit:
        return explicit
    if by_month or "weekly" not in stream["cadence"]:
        return "monthly"
    return "weekly"


def latest(cfg, stream_id, lo, hi, now, max_age_hours=None, accept_stale=False,
           windows_of=store_windows, cadence=None, by_month=False):
    """Which store(s) to read for [lo, hi] and whether they may be read.
    Returns a JSON-able dict; `ok` False carries `refusal`. With one store,
    `next` holds the commands that follow for the issue's cadence."""
    stream = select_streams(cfg, [stream_id])[0]
    cadence = issue_cadence(stream, cadence, by_month)
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
    if out["single_store"]:
        out["next"] = next_commands(cfg, stream, used[0][1], lo, hi, cadence)
    top = read_json(os.path.join(cfg["workspace"], "status.json"), {}) or {}
    warn = token_warning(token_as_of(top.get("token"), now))
    out["warnings"] = [warn] if warn else []
    if problems and not accept_stale:
        out["refusal"] = "; ".join(problems)
        return out
    out["overridden"] = problems
    out["ok"] = True
    return out


def store_repos(path, project):
    """The repositories a store holds for `project` (read-only)."""
    if not os.path.isfile(path):
        return []
    try:
        conn = _connect_ro(path)
        try:
            return sorted(r[0] for r in conn.execute(
                "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
                (project,)))
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def next_commands(cfg, stream, store, lo, hi, cadence):
    """The commands after `latest` for one newsletter issue: a view per
    repository, linked with the people registry (and the cadence's own
    series index), the outline and the work package — with `--weekly` for a
    weekly issue (the outline's default layout is the monthly one)."""
    py = "python3"
    label = "{}_{}".format(lo.isoformat(), hi.isoformat())
    out_dir = os.path.join(stream_dir(cfg, stream), "views", label)
    suffix = "-weekly" if cadence == "weekly" else ""
    weekly = ["--weekly"] if cadence == "weekly" else []
    if stream["kind"] == "repo":
        members = [(["--project", stream["owner"], "--repo", stream["repo"]],
                    os.path.join(out_dir, "bundle.json"),
                    os.path.join(stream_dir(cfg, stream),
                                 "series{}.json".format(suffix)))]
    else:
        project = manifest_project(stream) or ""
        members = []
        for repo in store_repos(store, project):
            name = repo.replace("/", "__")
            members.append((["--project", project, "--repo", repo],
                            os.path.join(out_dir, name + ".json"),
                            os.path.join(stream_dir(cfg, stream), "series",
                                         "{}{}.json".format(name, suffix))))
    cmds = []
    for scope, view, _series in members:
        cmds.append(shlex.join([py, VIEW_SCRIPT, "--store", store, *scope,
                                "--from", lo.isoformat(), "--to", hi.isoformat(),
                                "--out", view]))
    for _scope, view, series in members:
        cmds.append(shlex.join([py, LINK_SCRIPT, view, "--series", series,
                                "--people", cfg["people"]]))
    views = [v for _s, v, _r in members]
    outline = os.path.join(out_dir, "newsletter.outline.json")
    cmds.append(shlex.join([py, NEWSLETTER_SCRIPT, *views, "--outline", *weekly])
                + " > " + shlex.quote(outline))
    cmds.append(shlex.join([py, NEWSLETTER_SCRIPT, *views, "--work-package",
                            os.path.join(out_dir, "package"), *weekly]))
    return {"cadence": cadence, "views": views, "outline": outline,
            "work_package": os.path.join(out_dir, "package"), "commands": cmds}


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
    app_route = token_route(cfg, top) == "app"
    if top.get("token"):
        info = token_as_of(top["token"], t)
        lines.append("token: {}".format(describe_token(info)))
        warn = token_warning(info)
        if warn:
            lines.append("TOKEN: " + warn)
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
            auth = (e.get("last_attempt") or {}).get("auth_error")
            if auth:
                if app_route:
                    lines.append("  TOKEN: {} - run `schedule.py token app-check` to see "
                                 "what GitHub answers (docs/SCHEDULING.md 7.1), then "
                                 "re-run".format(auth))
                else:
                    lines.append("  TOKEN: {} - replace the token (docs/SCHEDULING.md "
                                 "\"Keeping the token valid\"), then re-run".format(auth))
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
  recompute --streams FILE --stream ID --month YYYY-MM [--what ownership] [--dry-run]
  latest ... [--cadence weekly|monthly]
  status --streams FILE [--json]
  check  --streams FILE
  token renew  --streams FILE [--store keychain|env-file] [--env-file PATH] [--days N]
               [--classic] [--no-browser] [--kick] [--kick-here]
  token status --streams FILE [--store keychain|env-file] [--env-file PATH]
  token remind --streams FILE
  token kind   --streams FILE
  token where  --streams FILE --field store|env-file|keychain-service|account
  token app-check --streams FILE
  token app-key   --streams FILE --import PEM
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
    lt.add_argument("--cadence", choices=CADENCES, default=None,
                    help="the issue's layout (default: monthly for --month, else "
                         "weekly when the stream's cadence includes it)")
    rc = sub.add_parser("recompute", parents=[common],
                        help="recompute clone-derived facts of a month store "
                             "offline (after a code upgrade), validated, swapped")
    rc.add_argument("--stream", required=True, metavar="ID")
    rc.add_argument("--month", required=True, metavar="YYYY-MM")
    rc.add_argument("--what", choices=sorted(RECOMPUTE_FLAGS), default="ownership")
    rc.add_argument("--dry-run", action="store_true")
    st = sub.add_parser("status", parents=[common], help="the last results")
    st.add_argument("--json", action="store_true")
    sub.add_parser("check", parents=[common], help="check the streams file")
    tk = sub.add_parser("token", help="replace the stored token (weekly), show its "
                                      "expiry, or print a reminder")
    tks = tk.add_subparsers(dest="token_cmd", required=True)
    for name, text in (("renew", "open the new-token page, read the token at a hidden "
                                 "prompt, check it, then store it"),
                       ("status", "the stored token's login, expiry and days left"),
                       ("remind", "print a one-line reminder when the token expires "
                                  "within 2 days or was refused (no network)"),
                       ("kind", "print where the token comes from: app, keychain "
                                "or env-file (for the wrappers)"),
                       ("where", "print one setting of where the token is kept, as "
                                 "`token renew` uses it (for the wrappers; never "
                                 "the token)"),
                       ("app-check", "mint an installation token and prove it: rate "
                                     "limit, REST, GraphQL and search reads of "
                                     "token.probe_repo"),
                       ("app-key", "store the app's PEM private key in the keychain "
                                   "item token.private_key_keychain_service")):
        t = tks.add_parser(name, parents=[common], help=text)
        t.add_argument("--store", choices=("keychain", "env-file"), default=None,
                       help="where the token is kept (default: the streams file's "
                            "token.store, else keychain on macOS, env-file elsewhere)")
        t.add_argument("--env-file", default=None, metavar="PATH",
                       help="the env file (default {})".format(DEFAULT_ENV_FILE))
        if name == "renew":
            t.add_argument("--days", type=int, default=TOKEN_DAYS, metavar="N",
                           help="expiry prefilled on the page (default 7)")
            t.add_argument("--classic", action="store_true",
                           help="open the classic token page instead (nothing prefilled)")
            t.add_argument("--no-browser", action="store_true",
                           help="only print the page's address")
            t.add_argument("--kick", action="store_true",
                           help="start a run once the token is stored")
            t.add_argument("--kick-here", action="store_true",
                           help="with --kick on macOS: start the run from this "
                                "process, not through the scheduled job")
        if name == "app-key":
            t.add_argument("--import", dest="import_path", required=True, metavar="PEM",
                           help="the private key file downloaded from the app's page")
        if name == "where":
            t.add_argument("--field", required=True, choices=sorted(WHERE_FIELDS),
                           help="store, env-file, keychain-service or account")
    args = p.parse_args(argv)
    if args.cmd == "token" and args.token_cmd == "renew" and not 1 <= args.days <= 366:
        p.error("--days must be between 1 and 366")
    if args.cmd == "run" and args.max_minutes is not None and args.max_minutes < 1:
        p.error("--max-minutes must be at least 1")
    return args


def main(argv=None, **seams):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg = load_streams(args.streams)
        if args.cmd == "run":
            return cmd_run(cfg, args, **seams)
        if args.cmd == "recompute":
            return cmd_recompute(cfg, args, **seams)
        if args.cmd == "status":
            return cmd_status(cfg, args, **{k: v for k, v in seams.items()
                                             if k == "now"})
        if args.cmd == "check":
            return cmd_check(cfg)
        if args.cmd == "token":
            return cmd_token(cfg, args, **seams)
        now = seams.get("now", utc_now)()
        today = args.today or now.date()
        lo, hi = reader_period(args, today)
        out = latest(cfg, args.stream, lo, hi, now, args.max_age_hours,
                     args.accept_stale, cadence=args.cadence,
                     by_month=args.month is not None)
    except ConfigError as exc:
        sys.stderr.write("schedule.py: error: {}\n".format(exc))
        return EXIT_USAGE
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    for w in out.get("warnings") or []:
        sys.stderr.write("latest: WARNING: {}\n".format(w))
    if out["ok"]:
        sys.stderr.write("latest: ok, {} store(s) for {}..{}{}\n".format(
            len(out["stores"]), lo, hi,
            " (overridden: {})".format("; ".join(out["overridden"]))
            if out["overridden"] else ""))
        if out.get("next"):
            sys.stderr.write("next ({} issue):\n{}\n".format(
                out["next"]["cadence"],
                "\n".join("  " + c for c in out["next"]["commands"])))
        return EXIT_OK
    sys.stderr.write("latest: REFUSED: {}\n".format(out["refusal"]))
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
