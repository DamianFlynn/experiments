#!/usr/bin/env python3
"""The retained people registry: one human-editable record per GitHub login,
shared by every gather of the owner's workspace (all months, all
repositories, all newsletter streams), so a person is looked up once and
refreshed rarely, and the per-repository activity profile kept beside it.

Where and what (docs/USAGE.md section 5.7, BUNDLE.md "The people registry"):

* two JSON files, by default `<workspace>/people.json` (identity, roles, the
  owner's edits) and the generated `<workspace>/people-activity.json` beside
  it (activity per repository per gathered window; activity_path), passed to
  gather with `--people PATH` or a manifest's `people`;
* people.json: {"format": FORMAT, "version": VERSION, "people": {login:
  entry}}; an entry holds only: login, name (the public profile's display
  name), affiliation / signal / strength, maintainer_of {repo: evidence},
  tags, first_seen / last_seen (the derived global first / last activity),
  looked_up_at, source, locked, do_not_name, note, home (the home
  settings the label was decided against). Never a company, bio, email or
  any other profile text; an unknown key is an error naming the entry.
  The activity profile added only optional fields (`first_evidence`, the
  owner's "former"), so a file written before it is read as it is;
* people-activity.json: {"format": ACTIVITY_FORMAT, "version":
  ACTIVITY_VERSION, "months_kept", "repos": {repo: coverage}, "people":
  {login: {repo: {window: contribution}}}}: per repository and gathered
  window one small contribution record per person (counts by month, first /
  last event with its reference); every total, rate, trend and status is
  derived from the set of windows (repo_profile), so re-gathering a window
  replaces its record and never counts twice;
* both written with sorted keys, two-space indent and a trailing newline,
  atomically (temporary file + rename) under one advisory lock, and only
  when the text changes, so version control shows one line per real change.

Operator edits win. An entry whose `source` is "operator", or whose `locked`
is true, keeps every decided field (name, affiliation, maintainer_of) as the
operator wrote it; `locked: ["name", ...]` pins only the fields listed; a
`maintainer_of` value written as plain true / false / "former" is the
operator's and is never changed. `tags`, `do_not_name` and `note` are the
operator's alone: the skill never writes them. Observations (first_seen /
last_seen, the activity file) are always updated.

This module is pure apart from the load / write / update functions and the
CLI (`python3 people.py seed | update | summary | show | check ...`), which
never calls the network.
"""
import argparse
import contextlib
import copy
import datetime
import json
import os
import re
import sys
import tempfile

try:                      # advisory lock where the platform has one
    import fcntl
except ImportError:       # pragma: no cover - non-POSIX
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import derive  # noqa: E402
import manifest as manifest_mod  # noqa: E402

FORMAT = "activity-overview/people"
VERSION = 1
ACTIVITY_FORMAT = "activity-overview/people-activity"
ACTIVITY_VERSION = 1

# Lookup once, refresh rarely. A decided entry (maintainer_org / community)
# is reused for REFRESH_DAYS after its lookup; an `unknown` one only for
# UNKNOWN_RETRY_DAYS (about one monthly cycle: a profile filled in or a
# membership made public shows up by the next installment). At most
# REFRESH_MAX stale entries are refreshed per run, oldest lookup first; the
# rest are reused as they are until a later run. At most NAME_MAX profile
# calls per run are made only for a display name (people decided without a
# lookup), newcomers first, then the most active.
REFRESH_DAYS = 120             # ACTIVITY_PEOPLE_REFRESH_DAYS / --people-refresh-days
UNKNOWN_RETRY_DAYS = 30        # ACTIVITY_PEOPLE_UNKNOWN_RETRY_DAYS
REFRESH_MAX = 50               # ACTIVITY_PEOPLE_REFRESH_MAX
NAME_MAX = 30                  # ACTIVITY_PEOPLE_NAME_MAX
NAME_MAX_CHARS = 100

# The maintainer property: write access to ONE repository, from stored facts
# only (no API call). Strongest first; the operator's own setting beats all.
APPROVALS_MIN = 3              # approving reviews of others' PRs that merged
MAINTAINER_SIGNALS = ("operator", "merged_others_pr", "OWNER",
                      "merged_own_pr", "COLLABORATOR", "approving_reviewer",
                      "none")
MAINTAINER_STRENGTH = {"operator": "operator", "merged_others_pr": "strong",
                       "OWNER": "strong", "merged_own_pr": "medium",
                       "COLLABORATOR": "medium", "approving_reviewer": "medium"}
_MAINTAINER_ASSOCIATIONS = ("OWNER", "COLLABORATOR")

AFFILIATION_VALUES = manifest_mod.AFFILIATION_VALUES
# signals the registry may hold for an affiliation (a per-window-only signal
# such as commit_email_domain or lookup_failed is never stored here)
REGISTRY_SIGNALS = ("override", "author_association", "public_membership",
                    "profile_company", "none")
STRENGTHS = ("operator", "strong", "medium", "weak")
SOURCES = ("lookup", "operator")
LOCKABLE = ("name", "affiliation", "maintainer_of")
ENTRY_KEYS = ("login", "name", "affiliation", "signal", "strength",
              "maintainer_of", "tags", "first_seen", "last_seen",
              "looked_up_at", "source", "locked", "do_not_name", "note",
              "home")
_MAINTAINER_KEYS = ("maintainer", "signal", "strength", "first_evidence",
                    "last_evidence")
OPERATOR_FORMER = "former"     # the owner's plain "former" in maintainer_of
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")
_GHOST = "ghost"


class RegistryError(ValueError):
    """A people registry that is not one this module reads: the message names
    the file and the entry."""


def today():
    """Today's date (YYYY-MM-DD); patched in tests."""
    return datetime.date.today().isoformat()


def empty_registry():
    return {"format": FORMAT, "version": VERSION, "people": {}}


# --- reading and checking ----------------------------------------------------

def _check_date(value, where):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("{} must be a date YYYY-MM-DD, got {!r}".format(where, value))


def _check_seen(value, where):
    if not isinstance(value, dict) or set(value) - {"date", "repo"} \
            or "date" not in value:
        raise ValueError("{} must be {{\"date\": YYYY-MM-DD, \"repo\": "
                         "\"owner/name\"}}".format(where))
    _check_date(value["date"], where + ".date")
    if "repo" in value and not (isinstance(value["repo"], str)
                                and _SLUG_RE.fullmatch(value["repo"])):
        raise ValueError("{}.repo must be owner/name, got {!r}".format(
            where, value["repo"]))


def check_name(value, where):
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or \
            len(value) > NAME_MAX_CHARS or any(ord(ch) < 32 for ch in value):
        raise ValueError("{} must be a one-line display name of at most {} "
                         "characters".format(where, NAME_MAX_CHARS))


def check_entry(login, entry):
    """Raise ValueError (naming the field) when `entry` is not a registry
    entry for `login`. Returns the entry."""
    if not isinstance(entry, dict):
        raise ValueError("must be an object")
    extra = sorted(set(entry) - set(ENTRY_KEYS))
    if extra:
        raise ValueError("unknown key(s) {} (an entry holds only {}; free text "
                         "goes in `note`)".format(", ".join(extra),
                                                  ", ".join(ENTRY_KEYS)))
    if entry.get("login") != login:
        raise ValueError("`login` must repeat the key {!r}, got {!r}".format(
            login, entry.get("login")))
    check_name(entry.get("name"), "`name`")
    if "affiliation" in entry and entry["affiliation"] not in AFFILIATION_VALUES:
        raise ValueError("`affiliation` must be one of {}, got {!r}".format(
            ", ".join(AFFILIATION_VALUES), entry["affiliation"]))
    if "signal" in entry and entry["signal"] not in REGISTRY_SIGNALS:
        raise ValueError("`signal` must be one of {}, got {!r}".format(
            ", ".join(REGISTRY_SIGNALS), entry["signal"]))
    if "strength" in entry and entry["strength"] not in STRENGTHS:
        raise ValueError("`strength` must be one of {}, got {!r}".format(
            ", ".join(STRENGTHS), entry["strength"]))
    mo = entry.get("maintainer_of", {})
    if not isinstance(mo, dict):
        raise ValueError("`maintainer_of` must be an object {\"owner/name\": "
                         "true | false | \"former\" | {...}}")
    for repo, v in mo.items():
        where = "`maintainer_of[{!r}]`".format(repo)
        if not _SLUG_RE.fullmatch(repo or ""):
            raise ValueError("{}: the key must be owner/name".format(where))
        if isinstance(v, bool) or v == OPERATOR_FORMER:
            continue
        if not isinstance(v, dict) or set(v) - set(_MAINTAINER_KEYS):
            raise ValueError("{} must be true, false, \"former\" or {{{}}}".format(
                where, ", ".join('"{}"'.format(k) for k in _MAINTAINER_KEYS)))
        if not isinstance(v.get("maintainer"), bool):
            raise ValueError("{}.maintainer must be true or false".format(where))
        if "signal" in v and v["signal"] not in MAINTAINER_SIGNALS:
            raise ValueError("{}.signal must be one of {}, got {!r}".format(
                where, ", ".join(MAINTAINER_SIGNALS), v["signal"]))
        if "strength" in v and v["strength"] not in STRENGTHS:
            raise ValueError("{}.strength must be one of {}".format(
                where, ", ".join(STRENGTHS)))
        for key in ("first_evidence", "last_evidence"):
            if key in v:
                _check_date(v[key], "{}.{}".format(where, key))
        if v.get("first_evidence") and v.get("last_evidence") and \
                v["first_evidence"] > v["last_evidence"]:
            raise ValueError("{}.first_evidence is after last_evidence".format(where))
    tags = entry.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip()
                                             for t in tags):
        raise ValueError("`tags` must be a list of non-empty strings")
    for key in ("first_seen", "last_seen"):
        if key in entry:
            _check_seen(entry[key], "`{}`".format(key))
    if "looked_up_at" in entry:
        _check_date(entry["looked_up_at"], "`looked_up_at`")
    if "source" in entry and entry["source"] not in SOURCES:
        raise ValueError("`source` must be one of {}, got {!r}".format(
            ", ".join(SOURCES), entry["source"]))
    locked = entry.get("locked", False)
    if not (isinstance(locked, bool) or (isinstance(locked, list) and all(
            f in LOCKABLE for f in locked))):
        raise ValueError("`locked` must be true, false or a list of {}".format(
            ", ".join(LOCKABLE)))
    if "do_not_name" in entry and not isinstance(entry["do_not_name"], bool):
        raise ValueError("`do_not_name` must be true or false")
    if "note" in entry and not isinstance(entry["note"], str):
        raise ValueError("`note` must be a string")
    if "home" in entry and not isinstance(entry["home"], str):
        raise ValueError("`home` must be a string")
    return entry


def parse_registry(text, path="people registry"):
    """The registry held in `text` (the file's content), checked. Raises
    RegistryError naming `path` and the entry; never resets silently."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise RegistryError("{}: not valid JSON ({})".format(path, exc)) from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise RegistryError("{}: not a people registry (expected an object with "
                            "\"format\": {!r})".format(path, FORMAT))
    if raw.get("version") != VERSION:
        raise RegistryError("{}: version {!r} is not one this skill reads "
                            "(expected {})".format(path, raw.get("version"), VERSION))
    extra = sorted(set(raw) - {"format", "version", "people"})
    if extra:
        raise RegistryError("{}: unknown top-level key(s) {}".format(
            path, ", ".join(extra)))
    people = raw.get("people", {})
    if not isinstance(people, dict):
        raise RegistryError("{}: \"people\" must be an object {{login: entry}}"
                            .format(path))
    seen = {}
    for login in sorted(people):
        low = login.lower()
        if low in seen:
            raise RegistryError("{}: entries {!r} and {!r} are the same GitHub "
                                "login (logins ignore case): keep one".format(
                                    path, seen[low], login))
        seen[low] = login
        try:
            check_entry(login, people[login])
        except ValueError as exc:
            raise RegistryError("{}: entry {!r}: {}".format(path, login, exc)) \
                from None
    return {"format": FORMAT, "version": VERSION, "people": dict(people)}


def load_registry(path):
    """The registry at `path`, or an empty one when the file does not exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return empty_registry()
    return parse_registry(text, path)


def dumps(registry):
    """The registry as stable text: sorted keys, two-space indent, non-ASCII
    names kept readable, a trailing newline."""
    return json.dumps(registry, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_registry(path, registry):
    """Write atomically (temporary file in the same directory, then rename);
    nothing is written when the text would not change. Returns True when
    the file changed."""
    return _write_text(path, dumps(registry))


def _write_text(path, text):
    """Write `text` to `path` atomically, only when it changes; True when the
    file changed."""
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    except FileNotFoundError:
        pass
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".people-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


@contextlib.contextmanager
def _locked(path):
    """An exclusive advisory lock on `<path>.lock` (POSIX), so two gathers of
    one workspace update the registry one after the other."""
    if fcntl is None:
        yield
        return
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.abspath(path) + ".lock", "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def update_registry(path, patches, windows=None):
    """Re-read the file under the lock, apply `patches` ({login: patch}, see
    apply_patch) over what is on disk now (so an edit made while a gather
    ran is kept) and write it back; with `windows` (fold_window arguments,
    see Run.record_window) also fold them into the activity file beside it
    (activity_path), re-read under the same lock. Returns (registry,
    changed) - changed when either file changed."""
    with _locked(path):
        reg = load_registry(path)
        reg = apply_patches(reg, patches)
        changed = write_registry(path, reg)
        if windows:
            apath = activity_path(path)
            act = load_activity(apath)
            for w in windows:
                act = fold_window(act, **w)
            changed = write_activity(apath, act) or changed
    return reg, changed


# --- entry semantics ----------------------------------------------------------

def find(registry, login):
    """(key, entry) of `login` in the registry, matched ignoring case, or
    (None, None)."""
    people = (registry or {}).get("people") or {}
    if login in people:
        return login, people[login]
    low = (login or "").lower()
    for key, entry in people.items():
        if key.lower() == low:
            return key, entry
    return None, None


def is_locked(entry, field):
    """True when the operator owns `field` of `entry` (source operator,
    locked true, or the field in the locked list)."""
    if not entry:
        return False
    if entry.get("source") == "operator" or entry.get("locked") is True:
        return True
    locked = entry.get("locked")
    return isinstance(locked, list) and field in locked


def lookup_state(entry, now, refresh_days=REFRESH_DAYS,
                 unknown_retry_days=UNKNOWN_RETRY_DAYS, home=None):
    """"never" (no lookup or no label yet), "fresh" (reuse: no call) or
    "stale" (due for a refresh): a decided label is fresh for
    `refresh_days`, an `unknown` one for `unknown_retry_days`. With `home`
    (the current home settings, gather.affiliation_home) a label decided
    against other or unrecorded home settings is stale, unless the operator
    owns it. Pure."""
    if not entry or not entry.get("looked_up_at") or not entry.get("affiliation"):
        return "never"
    if home is not None and entry.get("home") != home \
            and not is_locked(entry, "affiliation"):
        return "stale"
    try:
        age = (datetime.date.fromisoformat(now)
               - datetime.date.fromisoformat(entry["looked_up_at"])).days
    except ValueError:
        return "stale"
    limit = unknown_retry_days if entry["affiliation"] == "unknown" else refresh_days
    return "fresh" if age < limit else "stale"


def display_name(entry):
    """The name an output may show, or None (no name, or do_not_name)."""
    if not entry or entry.get("do_not_name"):
        return None
    name = entry.get("name")
    return name if isinstance(name, str) and name.strip() else None


def maintainer_for(entry, repo):
    """(maintainer, signal) of `entry` for ONE repository: the operator's
    plain true / false (signal operator; "former" is false), the stored
    evidence, or (False, "none") when the registry holds no evidence of
    write access there. Recency is maintainer_status's: this is whether the
    registry holds evidence at all."""
    v = ((entry or {}).get("maintainer_of") or {}).get(repo)
    if isinstance(v, bool):
        return v, "operator"
    if v == OPERATOR_FORMER:
        return False, "operator"
    if isinstance(v, dict):
        if is_locked(entry, "maintainer_of"):
            return v.get("maintainer") is True, "operator"
        return v.get("maintainer") is True, v.get("signal") or "none"
    return False, "none"


def _signal_rank(signal):
    return MAINTAINER_SIGNALS.index(signal) if signal in MAINTAINER_SIGNALS \
        else len(MAINTAINER_SIGNALS)


def apply_patch(entry, patch, login):
    """`entry` (or a new one for `login`) with one run's `patch` applied;
    operator-owned fields are left alone. Patch keys (all optional):
      seen        [(date, repo)]   first_seen / last_seen: earliest / latest
      label       {affiliation, signal, strength?, home?}   unless
                  affiliation locked
      name        str ("" : the profile has none)     unless name locked
      looked_up_at  date
      maintainer  {repo: {signal, strength, last_evidence}}  unless locked or
                  the operator's plain true / false; the stronger signal stays
    Pure (returns a new dict)."""
    e = dict(entry) if entry else {"login": login, "source": "lookup"}
    e.setdefault("login", login)
    for date, repo in patch.get("seen") or []:
        if not date:
            continue
        seen = {"date": date, "repo": repo} if repo else {"date": date}
        if "first_seen" not in e or date < e["first_seen"]["date"]:
            e["first_seen"] = seen
        if "last_seen" not in e or date > e["last_seen"]["date"]:
            e["last_seen"] = seen
    label = patch.get("label")
    if label and not is_locked(e, "affiliation"):
        e["affiliation"] = label["affiliation"]
        e["signal"] = label["signal"]
        if label.get("strength"):
            e["strength"] = label["strength"]
        else:
            e.pop("strength", None)
        if label.get("home"):
            e["home"] = label["home"]
        else:
            e.pop("home", None)
    if "name" in patch and not is_locked(e, "name") and not e.get("do_not_name"):
        name = (patch["name"] or "").strip()[:NAME_MAX_CHARS]
        name = "".join(ch for ch in name if ord(ch) >= 32)
        if name:
            e["name"] = name
        else:
            e.pop("name", None)
    if patch.get("looked_up_at"):
        e["looked_up_at"] = patch["looked_up_at"]
    if patch.get("maintainer") and not is_locked(e, "maintainer_of"):
        mo = dict(e.get("maintainer_of") or {})
        for repo, ev in sorted(patch["maintainer"].items()):
            cur = mo.get(repo)
            if isinstance(cur, bool) or cur == OPERATOR_FORMER:
                continue                  # the operator's own setting
            new = {"maintainer": True, "signal": ev["signal"],
                   "strength": MAINTAINER_STRENGTH.get(ev["signal"], "medium")}
            for key in ("first_evidence", "last_evidence"):
                if ev.get(key):
                    new[key] = ev[key]
            if isinstance(cur, dict) and cur.get("maintainer") is True:
                if _signal_rank(cur.get("signal")) <= _signal_rank(ev["signal"]):
                    new = dict(cur)
                last = _later(cur.get("last_evidence"), ev.get("last_evidence"))
                if last:
                    new["last_evidence"] = last
                first = _earlier(cur.get("first_evidence"), ev.get("first_evidence"))
                if first:
                    new["first_evidence"] = first
            mo[repo] = new
        e["maintainer_of"] = mo
    if not e.get("source"):
        e["source"] = "lookup"
    return e


def merge_patch(a, b):
    """Two patches of one login as one (b after a). Pure."""
    out = dict(a or {})
    for k, v in (b or {}).items():
        if k == "seen":
            out["seen"] = list(out.get("seen") or []) + list(v)
        elif k == "maintainer":
            m = dict(out.get("maintainer") or {})
            for repo, ev in v.items():
                cur = m.get(repo)
                if cur is None or _signal_rank(ev["signal"]) < _signal_rank(cur["signal"]):
                    keep = dict(ev)
                else:
                    keep = dict(cur)
                last = _later((cur or {}).get("last_evidence"), ev.get("last_evidence"))
                if last:
                    keep["last_evidence"] = last
                first = _earlier((cur or {}).get("first_evidence"),
                                 ev.get("first_evidence"))
                if first:
                    keep["first_evidence"] = first
                m[repo] = keep
            out["maintainer"] = m
        else:
            out[k] = v
    return out


def apply_patches(registry, patches):
    """The registry with every login's patch applied (a login matched to an
    existing key ignoring case keeps that key's spelling). Pure."""
    people = dict((registry or {}).get("people") or {})
    for login in sorted(patches or {}):
        key, entry = find({"people": people}, login)
        key = key or login
        people[key] = apply_patch(entry, patches[login], key)
    return {"format": FORMAT, "version": VERSION, "people": people}


# --- facts from records (no API call) ----------------------------------------

def _human(login):
    return bool(login) and login != _GHOST and not derive.is_bot_login(login)


def _day(ts):
    return str(ts)[:10] if ts else None


def _later(a, b):
    """The later of two optional days."""
    return max(d for d in (a, b) if d) if (a or b) else None


def _earlier(a, b):
    """The earlier of two optional days."""
    return min(d for d in (a, b) if d) if (a or b) else None


def activity_dates(prs=(), issues=(), commits=()):
    """{login: (first day, last day)} of the activity the records show: PRs
    and issues opened, merges, reviews, comments and resolved commits. Bots,
    ghost and unresolved commit authors are left out. Pure."""
    out = {}

    def add(login, ts):
        day = _day(ts)
        if not _human(login) or not day:
            return
        lo, hi = out.get(login, (day, day))
        out[login] = (min(lo, day), max(hi, day))
    for it in list(prs or []) + list(issues or []):
        add(it.get("author"), it.get("created_at"))
        if it.get("merged_by"):
            add(it.get("merged_by"), it.get("merged_at"))
        for r in it.get("reviews") or []:
            if isinstance(r, dict):
                add(r.get("author"), r.get("submitted_at"))
        for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
            if isinstance(c, dict):
                add(c.get("author"), c.get("created_at"))
    for c in commits or []:
        if c.get("login_resolved") is not False:
            add(c.get("author"), c.get("committed_at") or c.get("date"))
    return out


def maintainer_evidence(prs=(), issues=(), approvals_min=APPROVALS_MIN, until=None):
    """{login: {"signal", "first_evidence", "last_evidence", "strength"}}:
    who the records show holding write access to the repository, by the
    strongest signal:
      merged_others_pr   merged a PR somebody else authored        strong
      OWNER              author_association OWNER on their records strong
      merged_own_pr      merged their own PR                       medium
      COLLABORATOR       author_association COLLABORATOR           medium
      approving_reviewer approved at least `approvals_min` PRs of
                         others that were then merged              medium
    MEMBER (of the organisation that owns the repo) is an affiliation, not a
    role: it is not a maintainer signal. `until` (a day: the window's end)
    leaves out evidence dated after it, so a store read later does not date
    a role beyond what was gathered. Pure."""
    found = {}

    def add(login, signal, ts):
        if not _human(login):
            return
        if until is not None and _day(ts) and _day(ts) > until:
            return
        cur = found.get(login)
        if cur is None or _signal_rank(signal) < _signal_rank(cur["signal"]):
            cur = {"signal": signal,
                   "first_evidence": (cur or {}).get("first_evidence"),
                   "last_evidence": (cur or {}).get("last_evidence")}
        cur["last_evidence"] = _later(cur.get("last_evidence"), _day(ts))
        cur["first_evidence"] = _earlier(cur.get("first_evidence"), _day(ts))
        found[login] = cur
    approvals = {}
    for pr in prs or []:
        merged = pr.get("merged") or bool(pr.get("merged_at"))
        if not merged:
            continue
        m, a = pr.get("merged_by"), pr.get("author")
        if m:
            add(m, "merged_others_pr" if m != a else "merged_own_pr",
                pr.get("merged_at"))
        approvers = {}
        for r in pr.get("reviews") or []:
            if isinstance(r, dict) and (r.get("state") or "").lower() == "approved" \
                    and r.get("author") and r.get("author") != a and not (
                        until is not None and (_day(r.get("submitted_at")) or "") > until):
                approvers[r["author"]] = _later(approvers.get(r["author"]),
                                                _day(r.get("submitted_at")))
        for login, day in approvers.items():
            n, first, last = approvals.get(login, (0, None, None))
            approvals[login] = (n + 1, _earlier(first, day), _later(last, day))
    for login, (n, first, last) in approvals.items():
        if n >= approvals_min:
            add(login, "approving_reviewer", first)
            add(login, "approving_reviewer", last)
    for it in list(prs or []) + list(issues or []):
        if it.get("author_association") in _MAINTAINER_ASSOCIATIONS:
            add(it.get("author"), it["author_association"], it.get("created_at"))
        for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
            if isinstance(c, dict) and c.get("author_association") in \
                    _MAINTAINER_ASSOCIATIONS:
                add(c.get("author"), c["author_association"], c.get("created_at"))
    for login in found:
        found[login]["strength"] = MAINTAINER_STRENGTH[found[login]["signal"]]
        for key in ("first_evidence", "last_evidence"):
            if not found[login].get(key):
                found[login].pop(key, None)
    return {k: found[k] for k in sorted(found)}


# --- the activity profile (people-activity.json) ------------------------------
#
# What the registry knows of each person's activity, per repository, from the
# windows the skill gathered - never more. The file holds, per repository and
# gathered window, one contribution record per person; every figure a reader
# sees (totals, firsts / lasts, the monthly series, rates, trend, status) is
# derived from the set of windows (repo_profile), so:
#   * re-gathering or re-seeding a window REPLACES that window's records
#     (fold_window): nothing is counted twice, whatever the order of runs;
#   * windows that overlap inside a month are never summed: per month the
#     windows covering most of it are used, disjoint ones only (_month_choice);
#   * every first / total is "as observed since <coverage from>", unless the
#     gather-time newcomer search established a true first (`first_ever`).
# The owner never edits this file; `people.py check` explains a bad one.

MONTHS_KEPT = 36               # monthly detail kept per repository; older
                               # windows fold into one `before` record
ACTIVE_DAYS = 183              # ACTIVITY_PEOPLE_ACTIVE_DAYS / --active-days
FORMER_DAYS = 548              # ACTIVITY_PEOPLE_FORMER_DAYS / --former-days
GATHERED_SINCE_MIN_DAYS = 90   # gathered days after the last evidence needed
                               # to call anyone lapsed (else `unknown`)
RATE_MONTHS = (3, 6, 12)
TREND_RISING = 1.25            # recent / prior activity at or above: rising
TREND_FALLING = 0.75           # at or below: falling
# per-month counts of a contribution record
COUNT_KEYS = ("commits", "prs_opened", "prs_merged", "reviews", "approvals",
              "approvals_merged", "merges", "merges_others", "issues",
              "comments")
# the units of rate and trend (comments are left out: a thread is not work)
ACTIVITY_UNITS = ("commits", "prs_opened", "reviews", "merges", "issues")
# first / last events: [day, reference] (a PR / issue number, a commit sha);
# `comment` is the day only
EVENT_KEYS = ("commit", "pr_opened", "pr_merged", "review", "approval",
              "merge", "issue", "comment")
WINDOW_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
BEFORE = "before"
CONTRIBUTOR_STATUSES = ("active", "returned", "lapsed", "unknown")
MAINTAINER_STATUSES = ("active", "lapsed", "former", "unknown")


def thresholds(env=None, active_days=None, former_days=None):
    """{active_days, former_days} from the arguments, else the environment
    (ACTIVITY_PEOPLE_ACTIVE_DAYS / ACTIVITY_PEOPLE_FORMER_DAYS), else the
    constants. A value that is not a positive integer is an error."""
    env = os.environ if env is None else env
    out = {}
    for key, given, name, default in (
            ("active_days", active_days, "ACTIVITY_PEOPLE_ACTIVE_DAYS", ACTIVE_DAYS),
            ("former_days", former_days, "ACTIVITY_PEOPLE_FORMER_DAYS", FORMER_DAYS)):
        value = given if given is not None else env.get(name, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("{} must be a positive integer, got {!r}".format(
                name, value)) from None
        if value < 1:
            raise ValueError("{} must be a positive integer, got {!r}".format(
                name, value))
        out[key] = value
    if out["former_days"] < out["active_days"]:
        raise ValueError("ACTIVITY_PEOPLE_FORMER_DAYS ({}) must not be below "
                         "ACTIVITY_PEOPLE_ACTIVE_DAYS ({})".format(
                             out["former_days"], out["active_days"]))
    return out


def activity_path(people_path):
    """The activity file beside a registry: `people.json` ->
    `people-activity.json` (any `<stem>.json` -> `<stem>-activity.json`)."""
    root, ext = os.path.splitext(people_path)
    return "{}-activity{}".format(root, ext or ".json")


def empty_activity():
    return {"format": ACTIVITY_FORMAT, "version": ACTIVITY_VERSION,
            "months_kept": MONTHS_KEPT, "repos": {}, "people": {}}


def window_key(frm, to):
    return "{}..{}".format(frm, to)


def parse_window_key(key):
    m = WINDOW_KEY_RE.fullmatch(key or "")
    return (m.group(1), m.group(2)) if m else None


def _date(day):
    return datetime.date.fromisoformat(day)


def _days(a, b):
    """Days from day `a` to day `b`."""
    return (_date(b) - _date(a)).days


def _next_day(day):
    return (_date(day) + datetime.timedelta(days=1)).isoformat()


def _months(frm, to):
    """The months "YYYY-MM" from day `frm` to day `to`, both included."""
    y, m = int(frm[:4]), int(frm[5:7])
    out = []
    while "{:04d}-{:02d}".format(y, m) <= to[:7]:
        out.append("{:04d}-{:02d}".format(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_bounds(month):
    y, m = int(month[:4]), int(month[5:7])
    nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    return "{}-01".format(month), (nxt - datetime.timedelta(days=1)).isoformat()


def _month_shift(month, delta):
    n = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return "{:04d}-{:02d}".format(n // 12, n % 12 + 1)


def _overlap_days(a, b):
    """Days two (from, to) day ranges share (0 when disjoint)."""
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return _days(lo, hi) + 1 if lo <= hi else 0


def _ref_key(value):
    """Sort key of a first / last event value ([day, ref] or day)."""
    if isinstance(value, list):
        ref = value[1]
        return (value[0], (0, ref, "") if isinstance(ref, int) else (1, 0, str(ref)))
    return (value, (0, 0, ""))


def _event_day(value):
    return value[0] if isinstance(value, list) else value


def window_contributions(prs=(), issues=(), commits=(), frm=None, to=None,
                         newcomers=None):
    """({login: contribution}, stats) of ONE gathered window: only events
    dated inside [frm, to] count (a store holds records that reach before
    and after its window; those events belong to other windows). A
    contribution is {"months": {"YYYY-MM": {count: n}}, "first": {event:
    value}, "last": {event: value}, "first_ever"?: day} with only the keys
    that occur:
      commits / commit         a resolved commit of the mainline the store
                               walked (an unresolved git author has no
                               login: counted in stats only)
      prs_opened / pr_opened   a PR the person opened
      prs_merged / pr_merged   a PR of theirs merged
      merges / merge           a merge they performed (merges_others: of a
                               PR somebody else authored)
      reviews / review         a review submission on somebody else's PR
                               (approvals / approval: those approving;
                               approvals_merged: approved PRs that were
                               merged by the time of the gather, once per PR)
      issues / issue           an issue they opened
      comments / comment       a comment or review comment (day only)
    `first_ever` (the window's `newcomers.before`) marks a person the
    newcomer search found with no earlier PR or issue in the repository.
    `seen_before` is the earliest day of the person's events dated before
    `frm` in these records (a store keeps older records: an open issue's
    early comments); it is not counted anywhere, it only says the person was
    seen before the coverage (fold_window keeps it per person and repository
    as the earliest day, outside the window records). stats: {"people": n
    (with an event in the window), "unresolved_commit_authors": n}. Bots and
    ghost are left out. Pure."""
    out = {}
    earlier = {}

    def inwin(day):
        return bool(day) and (frm is None or day >= frm) and (to is None or day <= to)

    def add(login, count, event, ts, ref=None):
        day = _day(ts)
        if not _human(login) or not day:
            return
        if frm is not None and day < frm:
            earlier[login] = _earlier(earlier.get(login), day)
            return
        if not inwin(day):
            return
        r = out.setdefault(login, {"months": {}, "first": {}, "last": {}})
        if count:
            m = r["months"].setdefault(day[:7], {})
            m[count] = m.get(count, 0) + 1
        if event:
            value = day if ref is None else [day, ref]
            cur = r["first"].get(event)
            if cur is None or _ref_key(value) < _ref_key(cur):
                r["first"][event] = value
            cur = r["last"].get(event)
            if cur is None or _ref_key(value) > _ref_key(cur):
                r["last"][event] = value
    unresolved = set()
    for c in commits or []:
        ts = c.get("committed_at") or c.get("date")
        if c.get("login_resolved") is False:
            if inwin(_day(ts)) and c.get("author"):
                unresolved.add(c["author"])
            continue
        add(c.get("author"), "commits", "commit", ts, c.get("sha"))
    for pr in prs or []:
        n, author = pr.get("number"), pr.get("author")
        add(author, "prs_opened", "pr_opened", pr.get("created_at"), n)
        merged = bool(pr.get("merged") or pr.get("merged_at"))
        if merged and pr.get("merged_at"):
            add(author, "prs_merged", "pr_merged", pr.get("merged_at"), n)
            m = pr.get("merged_by")
            if m:
                add(m, "merges", "merge", pr.get("merged_at"), n)
                if m != author:
                    add(m, "merges_others", None, pr.get("merged_at"))
        approved = set()
        for r in pr.get("reviews") or []:
            if not isinstance(r, dict) or not r.get("author") or r.get("author") == author:
                continue
            add(r["author"], "reviews", "review", r.get("submitted_at"), n)
            if (r.get("state") or "").lower() == "approved":
                add(r["author"], "approvals", "approval", r.get("submitted_at"), n)
                if merged and r["author"] not in approved:
                    approved.add(r["author"])
                    add(r["author"], "approvals_merged", None, r.get("submitted_at"))
    for it in issues or []:
        add(it.get("author"), "issues", "issue", it.get("created_at"), it.get("number"))
    for it in list(prs or []) + list(issues or []):
        for c in (it.get("comments_list") or []) + (it.get("review_comments") or []):
            if isinstance(c, dict):
                add(c.get("author"), "comments", "comment", c.get("created_at"))
    if isinstance(newcomers, dict) and newcomers.get("collected"):
        for e in newcomers.get("new") or []:
            login = e.get("login") if isinstance(e, dict) else None
            if login in out and newcomers.get("before"):
                out[login]["first_ever"] = newcomers["before"]
    for r in out.values():
        for k in ("first", "last"):
            if not r[k]:
                del r[k]
    stats = {"people": len(out), "unresolved_commit_authors": len(unresolved)}
    for login, day in earlier.items():
        out.setdefault(login, {"months": {}})["seen_before"] = day
    return {k: out[k] for k in sorted(out)}, stats


def _merge_contribution(a, b):
    """Two contribution records as one (counts summed, firsts earliest,
    lasts latest); used only to fold old windows into `before`. Pure."""
    out = {"months": {}}
    for rec in (a or {}, b or {}):
        for month, counts in (rec.get("months") or {}).items():
            m = out["months"].setdefault(month, {})
            for k, v in counts.items():
                m[k] = m.get(k, 0) + v
        for side, better in (("first", lambda x, y: _ref_key(x) < _ref_key(y)),
                             ("last", lambda x, y: _ref_key(x) > _ref_key(y))):
            for ev, value in (rec.get(side) or {}).items():
                cur = out.setdefault(side, {}).get(ev)
                if cur is None or better(value, cur):
                    out[side][ev] = value
        if rec.get("first_ever"):
            out["first_ever"] = _earlier(out.get("first_ever"), rec["first_ever"])
    return out


def window_is_folded(activity, slug, to):
    """True when a window ending on `to` lies inside the repository's folded
    `before` range (its records were folded away: folding it again would
    count it twice, so it is skipped)."""
    before = (((activity or {}).get("repos") or {}).get(slug) or {}).get(BEFORE)
    return bool(before) and to <= before["to"]


def fold_window(activity, slug, frm, to, contributions, stats=None, stream=None,
                source="gather"):
    """The activity with ONE gathered window of repository `slug` recorded:
    every person's record of that window is replaced by `contributions`
    (window_contributions), the window is listed in the repository's
    coverage with its `source` (gather | seed) and `stats`, and windows that
    end more than MONTHS_KEPT months before the repository's latest one are
    folded into one `before` record per person. A window inside the folded
    range is skipped (window_is_folded). Idempotent: folding the same window
    twice gives the same activity. Pure (returns a new dict)."""
    act = copy.deepcopy(activity) if activity else empty_activity()
    if window_is_folded(act, slug, to):
        return act
    key = window_key(frm, to)
    repo = act["repos"].setdefault(slug, {"windows": {}})
    if stream:
        repo["stream"] = stream
    meta = {"source": source}
    meta.update(stats or {})
    repo["windows"][key] = meta
    for login in list(act["people"]):
        repos = act["people"][login]
        if slug in repos:
            repos[slug].pop(key, None)
            if not repos[slug]:
                del repos[slug]
        if not repos:
            del act["people"][login]
    for login, rec in (contributions or {}).items():
        rec = dict(rec)
        day = rec.pop("seen_before", None)
        if day:
            # kept per person and repository as the earliest day (min is
            # idempotent and order-free; one line instead of one per window)
            by = act.setdefault("seen_before", {}).setdefault(login, {})
            by[slug] = _earlier(by.get(slug), day)
        if any(rec.get("months", {}).values()) or set(rec) - {"months"}:
            act["people"].setdefault(login, {}).setdefault(slug, {})[key] = rec
    return _fold_old_windows(act, slug)


def _fold_old_windows(act, slug):
    """Fold the windows of `slug` that end before the MONTHS_KEPT cutoff into
    `before` (in place; returns act)."""
    repo = act["repos"][slug]
    keys = sorted(repo["windows"])
    if not keys:
        return act
    latest = max(parse_window_key(k)[1] for k in keys)[:7]
    cutoff = _month_shift(latest, -(act.get("months_kept") or MONTHS_KEPT) + 1)
    old = [k for k in keys if parse_window_key(k)[1][:7] < cutoff]
    if not old:
        return act
    before = dict(repo.get(BEFORE) or {})
    for k in old:
        frm, to = parse_window_key(k)
        before["from"] = _earlier(before.get("from"), frm)
        before["to"] = _later(before.get("to"), to)
        before["windows"] = before.get("windows", 0) + 1
        del repo["windows"][k]
    repo[BEFORE] = before
    for login, repos in act["people"].items():
        recs = repos.get(slug)
        if not recs:
            continue
        acc = recs.get(BEFORE)
        for k in old:
            if k in recs:
                rec = recs.pop(k)
                folded = {"months": {BEFORE: {}}}
                for counts in (rec.get("months") or {}).values():
                    for c, v in counts.items():
                        folded["months"][BEFORE][c] = folded["months"][BEFORE].get(c, 0) + v
                for side in ("first", "last", "first_ever"):
                    if side in rec:
                        folded[side] = rec[side]
                acc = _merge_contribution(acc, folded)
        if acc is not None:
            recs[BEFORE] = acc
    return act


# --- reading and checking the activity file -----------------------------------

def _check_contribution(rec, where, window):
    if not isinstance(rec, dict) or set(rec) - {"months", "first", "last",
                                                 "first_ever"}:
        raise ValueError("{} must be {{months, first?, last?, first_ever?}}".format(where))
    months = rec.get("months")
    if not isinstance(months, dict):
        raise ValueError("{}.months must be an object".format(where))
    for month, counts in months.items():
        if window is None:
            if month != BEFORE:
                raise ValueError("{}: the folded record holds only months "
                                 "\"before\"".format(where))
        elif not _MONTH_RE.fullmatch(month) or not (
                window[0][:7] <= month <= window[1][:7]):
            raise ValueError("{}.months: {!r} is not a month of the window".format(
                where, month))
        if not isinstance(counts, dict) or set(counts) - set(COUNT_KEYS) or not all(
                isinstance(v, int) and not isinstance(v, bool) and v > 0
                for v in counts.values()):
            raise ValueError("{}.months[{!r}] must map {} to positive whole "
                             "numbers".format(where, month, ", ".join(COUNT_KEYS)))
    for side in ("first", "last"):
        events = rec.get(side, {})
        if not isinstance(events, dict) or set(events) - set(EVENT_KEYS):
            raise ValueError("{}.{} must map {} to an event".format(
                where, side, ", ".join(EVENT_KEYS)))
        for ev, value in events.items():
            day = value if ev == "comment" else (
                value[0] if isinstance(value, list) and len(value) == 2 else None)
            if not isinstance(day, str) or not _DATE_RE.fullmatch(day):
                raise ValueError("{}.{}.{} must be {}".format(
                    where, side, ev, "a day" if ev == "comment"
                    else "[day, reference]"))
            if window is not None and not (window[0] <= day <= window[1]):
                raise ValueError("{}.{}.{}: {} is outside the window".format(
                    where, side, ev, day))
    if "first_ever" in rec:
        _check_date(rec["first_ever"], where + ".first_ever")


def check_activity(raw, path="people activity"):
    """The activity object, checked: raises RegistryError naming the file,
    the repository / person and the window. Returns it."""
    if not isinstance(raw, dict) or raw.get("format") != ACTIVITY_FORMAT:
        raise RegistryError("{}: not a people activity file (expected an object "
                            "with \"format\": {!r})".format(path, ACTIVITY_FORMAT))
    if raw.get("version") != ACTIVITY_VERSION:
        raise RegistryError("{}: version {!r} is not one this skill reads "
                            "(expected {})".format(path, raw.get("version"),
                                                   ACTIVITY_VERSION))
    extra = sorted(set(raw) - {"format", "version", "months_kept", "repos", "people",
                               "seen_before"})
    if extra:
        raise RegistryError("{}: unknown top-level key(s) {}".format(
            path, ", ".join(extra)))
    if not isinstance(raw.get("months_kept", MONTHS_KEPT), int) or \
            raw.get("months_kept", MONTHS_KEPT) < 1:
        raise RegistryError("{}: months_kept must be a positive whole number".format(path))
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise RegistryError("{}: \"repos\" must be an object".format(path))
    for slug, repo in repos.items():
        where = "{}: repos[{!r}]".format(path, slug)
        if not _SLUG_RE.fullmatch(slug or "") or not isinstance(repo, dict) or \
                set(repo) - {"windows", "stream", BEFORE} or \
                not isinstance(repo.get("windows"), dict):
            raise RegistryError("{} must be {{windows, stream?, before?}} under an "
                                "owner/name key".format(where))
        for key, meta in repo["windows"].items():
            if not parse_window_key(key) or parse_window_key(key)[0] > \
                    parse_window_key(key)[1]:
                raise RegistryError("{}: window {!r} is not FROM..TO".format(where, key))
            if not isinstance(meta, dict) or meta.get("source") not in ("gather", "seed") \
                    or set(meta) - {"source", "people", "unresolved_commit_authors"}:
                raise RegistryError("{}: window {!r} must be {{source: gather | seed, "
                                    "people?, unresolved_commit_authors?}}".format(
                                        where, key))
        if BEFORE in repo:
            b = repo[BEFORE]
            if not isinstance(b, dict) or set(b) != {"from", "to", "windows"}:
                raise RegistryError("{}: before must be {{from, to, windows}}".format(where))
        if "stream" in repo and not (isinstance(repo["stream"], str)
                                     and repo["stream"].strip()):
            raise RegistryError("{}: stream must be a non-empty string".format(where))
    people = raw.get("people", {})
    if not isinstance(people, dict):
        raise RegistryError("{}: \"people\" must be an object".format(path))
    for login, by_repo in people.items():
        if not isinstance(by_repo, dict):
            raise RegistryError("{}: people[{!r}] must be {{repo: {{window: "
                                "record}}}}".format(path, login))
        for slug, recs in by_repo.items():
            where = "{}: people[{!r}][{!r}]".format(path, login, slug)
            if slug not in repos:
                raise RegistryError("{}: the repository has no coverage in "
                                    "\"repos\"".format(where))
            if not isinstance(recs, dict):
                raise RegistryError("{} must be {{window: record}}".format(where))
            for key, rec in recs.items():
                if key == BEFORE:
                    if BEFORE not in repos[slug]:
                        raise RegistryError("{}: a folded record without a "
                                            "folded range".format(where))
                    window = None
                elif key not in repos[slug]["windows"]:
                    raise RegistryError("{}: window {!r} is not in the repository's "
                                        "coverage".format(where, key))
                else:
                    window = parse_window_key(key)
                try:
                    _check_contribution(rec, "[{!r}]".format(key), window)
                except ValueError as exc:
                    raise RegistryError("{}{}".format(where, exc)) from None
    seen = raw.get("seen_before", {})
    if not isinstance(seen, dict):
        raise RegistryError("{}: \"seen_before\" must be {{login: {{repo: day}}}}".format(path))
    for login, by_repo in seen.items():
        where = "{}: seen_before[{!r}]".format(path, login)
        if not isinstance(by_repo, dict):
            raise RegistryError("{} must be {{repo: day}}".format(where))
        for slug, day in by_repo.items():
            if slug not in repos:
                raise RegistryError("{}[{!r}]: the repository has no coverage in "
                                    "\"repos\"".format(where, slug))
            try:
                _check_date(day, "[{!r}]".format(slug))
            except ValueError as exc:
                raise RegistryError("{}{}".format(where, exc)) from None
    return raw


def parse_activity(text, path="people activity"):
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise RegistryError("{}: not valid JSON ({})".format(path, exc)) from None
    return check_activity(raw, path)


def load_activity(path):
    """The activity file at `path`, or an empty one when it does not exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return empty_activity()
    return parse_activity(text, path)


def _scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def _compact(obj, indent):
    """JSON text of `obj`: objects one key per line (sorted), but an object
    or list whose values are all scalars on ONE line - a month's counts, a
    first / last event, a window's coverage entry - so the file stays
    readable and a diff shows one line per changed month."""
    if isinstance(obj, dict) and obj and not all(_scalar(v) for v in obj.values()):
        pad = " " * (indent + 2)
        body = ",\n".join("{}{}: {}".format(pad, json.dumps(k, ensure_ascii=False),
                                            _compact(obj[k], indent + 2))
                          for k in sorted(obj))
        return "{\n" + body + "\n" + " " * indent + "}"
    if isinstance(obj, list) and obj and not all(_scalar(v) for v in obj):
        pad = " " * (indent + 2)
        return "[\n" + ",\n".join(pad + _compact(v, indent + 2) for v in obj) + \
            "\n" + " " * indent + "]"
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


def dumps_activity(activity):
    """The activity file as stable text (sorted keys, leaf objects on one
    line, a trailing newline)."""
    return _compact(activity, 0) + "\n"


def write_activity(path, activity):
    """Write the activity file atomically, only when it changes."""
    return _write_text(path, dumps_activity(activity))


# --- deriving the profile -----------------------------------------------------

def _repo_windows(activity, slug, as_of=None):
    """[(key, from, to)] of the repository's gathered windows (ending on or
    before `as_of` when given), oldest first."""
    repo = ((activity or {}).get("repos") or {}).get(slug) or {}
    out = []
    for key in sorted(repo.get("windows") or {}):
        frm, to = parse_window_key(key)
        if as_of is None or to <= as_of:
            out.append((key, frm, to))
    return out


def coverage(activity, slug, as_of=None):
    """What the skill gathered of one repository: {"from", "to", "windows",
    "gaps": [[from, to]], "gathered_days", "months": [YYYY-MM], "before"?:
    {from, to, windows} (folded)} or None when nothing was gathered. A
    month counts as gathered when any window covers any of its days."""
    repo = ((activity or {}).get("repos") or {}).get(slug) or {}
    wins = _repo_windows(activity, slug, as_of)
    before = repo.get(BEFORE)
    if not wins and not before:
        return None
    merged = []
    for _k, frm, to in wins:
        if merged and frm <= _next_day(merged[-1][1]):
            merged[-1][1] = max(merged[-1][1], to)
        else:
            merged.append([frm, to])
    gaps = [[_next_day(a[1]), (_date(b[0]) - datetime.timedelta(days=1)).isoformat()]
            for a, b in zip(merged, merged[1:])]
    months = sorted({m for _k, frm, to in wins for m in _months(frm, to)})
    out = {"windows": len(wins), "gaps": gaps,
           "gathered_days": sum(_days(a, b) + 1 for a, b in merged),
           "months": months, "intervals": merged}
    if wins:
        out["from"], out["to"] = merged[0][0], merged[-1][1]
    if before:
        out[BEFORE] = dict(before)
        out["from"] = before["from"]
        out.setdefault("to", before["to"])
    return out


def _gathered_after(cov, day):
    """Gathered days of `cov` strictly after `day`."""
    total = 0
    for a, b in (cov or {}).get("intervals") or []:
        lo = max(a, _next_day(day))
        if lo <= b:
            total += _days(lo, b) + 1
    return total


def _month_choice(activity, slug, as_of=None):
    """{month: [window key]}: per month, the windows whose records count:
    the window covering most of the month first, then any other that does
    not overlap a chosen one inside the month (two overlapping windows are
    never summed; ties: the later window). Pure."""
    out = {}
    wins = _repo_windows(activity, slug, as_of)
    for month in sorted({m for _k, f, t in wins for m in _months(f, t)}):
        span = _month_bounds(month)
        cands = []
        for key, frm, to in wins:
            part = (max(frm, span[0]), min(to, span[1]))
            if part[0] <= part[1]:
                cands.append((-(_days(*part) + 1), [-ord(ch) for ch in key], key, part))
        chosen = []
        for _n, _o, key, part in sorted(cands):
            if all(_overlap_days(part, p) == 0 for _k, p in chosen):
                chosen.append((key, part))
        out[month] = [k for k, _p in chosen]
    return out


def _person_records(activity, login, slug, as_of=None):
    """{window key: record} of one person in one repository (as of)."""
    key, _ = find({"people": (activity or {}).get("people") or {}}, login)
    recs = (((activity or {}).get("people") or {}).get(key or login) or {}).get(slug) or {}
    keep = {k for k, _f, _t in _repo_windows(activity, slug, as_of)}
    return {k: v for k, v in recs.items() if k in keep or k == BEFORE}


def _seen_before(activity, login, slug, cov, as_of=None):
    """The earliest day `login` shows in stored records of `slug` dated
    before a gathered window, when that day is outside every gathered window
    (a day a window covers is already counted, or known to be quiet) and on
    or before `as_of`; else None."""
    seen = (activity or {}).get("seen_before") or {}
    key, _ = find({"people": seen}, login)
    day = (seen.get(key or login) or {}).get(slug)
    if not day or (as_of is not None and day > as_of):
        return None
    if any(a <= day <= b for a, b in (cov or {}).get("intervals") or []):
        return None
    return day


def _url(slug, event, ref):
    if event == "commit":
        return "https://github.com/{}/commit/{}".format(slug, ref)
    if event == "issue":
        return "https://github.com/{}/issues/{}".format(slug, ref)
    return "https://github.com/{}/pull/{}".format(slug, ref)


def _event_out(slug, event, value):
    if not isinstance(value, list):
        return {"date": value}
    out = {"date": value[0], "url": _url(slug, event, value[1])}
    out["sha" if event == "commit" else "number"] = value[1]
    return out


def _lifecycle(last_day, cov, active_days, former_days=None):
    """(status, basis) of somebody whose latest qualifying action in a
    repository was on `last_day`, against its coverage:
      active   last_day within active_days of the latest gathered day
      unknown  not, but fewer than GATHERED_SINCE_MIN_DAYS were gathered
               after last_day (coverage gaps: absence is not known)
      former   (with former_days) more than former_days before it
      lapsed   otherwise."""
    if not cov or not cov.get("to"):
        return "unknown", "no gathered window of the repository"
    gap = _days(last_day, cov["to"])
    if gap < 0:
        return "active", "last on {}, after the latest gathered day {} (a stored " \
            "record dated later)".format(last_day, cov["to"])
    if gap <= active_days:
        return "active", "last on {}, {} day(s) before the latest gathered day {} " \
            "(active within {})".format(last_day, gap, cov["to"], active_days)
    after = _gathered_after(cov, last_day)
    if after < GATHERED_SINCE_MIN_DAYS:
        return "unknown", "last on {}; only {} day(s) gathered since (needs {})".format(
            last_day, after, GATHERED_SINCE_MIN_DAYS)
    if former_days is not None and gap > former_days:
        return "former", "last on {}, {} days before {} (former beyond {})".format(
            last_day, gap, cov["to"], former_days)
    return "lapsed", "last on {}, {} days before {}, {} day(s) gathered since " \
        "(lapsed beyond {})".format(last_day, gap, cov["to"], after, active_days)


def _round(x):
    return round(x, 2)


def repo_profile(activity, login, slug, as_of=None, entry=None, limits=None):
    """One person's derived activity profile in one repository, or None
    when the activity file holds nothing of them there:
      observed_since   the repository's coverage start: every first and
                       total is as observed since then
      coverage         see coverage()
      first / last     {event: {date, number | sha, url}} (comment: date)
      seen_before      the earliest day the person shows in stored records
                       dated before a window (not counted anywhere)
      first_pr_ever / first_issue_ever   true only when the gather-time
                       newcomer search found no earlier PR or issue and the
                       first observed one is in that window
      totals           sums over by_month (incl. a folded `before`)
      by_month         {YYYY-MM | before: {count: n}} (_month_choice)
      rates            {"3" | "6" | "12": {months, from, to, unit: average}}
                       over the repository's last N gathered months
                       (absent months count as zero)
      trend            {value: rising | steady | falling | inactive |
                       insufficient, basis}
      status / status_basis   contributor lifecycle (active | returned |
                       lapsed | unknown, _lifecycle)
      maintainer       maintainer_status() when the registry entry holds
                       maintainer evidence for the repository
    `as_of` (a day) keeps only the windows ending by then (link: the view's
    end, so an old report's context stays what it was). Pure."""
    limits = limits or thresholds({})
    recs = _person_records(activity, login, slug, as_of)
    cov = coverage(activity, slug, as_of)
    seen_before = _seen_before(activity, login, slug, cov, as_of)
    if (not recs and not seen_before) or not cov:
        maint = maintainer_status(entry, slug, activity, as_of, limits)
        return {"maintainer": maint, "coverage": cov} if maint else None
    choice = _month_choice(activity, slug, as_of)
    by_month = {}
    for month, keys in choice.items():
        for key in keys:
            for c, v in ((recs.get(key) or {}).get("months") or {}).get(month, {}).items():
                by_month.setdefault(month, {})[c] = by_month.get(month, {}).get(c, 0) + v
    if BEFORE in recs:
        by_month[BEFORE] = dict(recs[BEFORE]["months"].get(BEFORE) or {})
    totals = {}
    for counts in by_month.values():
        for c, v in counts.items():
            totals[c] = totals.get(c, 0) + v
    first, last, ever = {}, {}, None
    first_raw = {}
    for key, rec in recs.items():
        for ev, value in (rec.get("first") or {}).items():
            if ev not in first_raw or _ref_key(value) < _ref_key(first_raw[ev][0]):
                first_raw[ev] = (value, key)
        for ev, value in (rec.get("last") or {}).items():
            if ev not in last or _ref_key(value) > _ref_key(last[ev]):
                last[ev] = value
        if rec.get("first_ever"):
            ever = (rec["first_ever"], key)
    out = {"observed_since": cov.get("from"),
           "coverage": {k: v for k, v in cov.items() if k != "intervals"}}
    if seen_before:
        out["seen_before"] = seen_before
    out["first"] = {ev: _event_out(slug, ev, v) for ev, (v, _k) in sorted(first_raw.items())}
    out["last"] = {ev: _event_out(slug, ev, v) for ev, v in sorted(last.items())}
    if ever:
        for ev, flag in (("pr_opened", "first_pr_ever"), ("issue", "first_issue_ever")):
            if ev in first_raw and first_raw[ev][1] == ever[1]:
                out[flag] = True
    out["totals"] = {c: totals[c] for c in COUNT_KEYS if totals.get(c)}
    keep = sorted(m for m in by_month if m != BEFORE)
    out["by_month"] = {m: {c: by_month[m][c] for c in COUNT_KEYS if by_month[m].get(c)}
                       for m in keep + ([BEFORE] if BEFORE in by_month else [])
                       if any(by_month[m].values())}
    months = cov.get("months") or []

    def units(month):
        return sum((by_month.get(month) or {}).get(u, 0) for u in ACTIVITY_UNITS)
    rates = {}
    for n in RATE_MONTHS:
        last_n = months[-n:]
        if not last_n:
            continue
        row = {"months": len(last_n), "from": last_n[0], "to": last_n[-1]}
        for c in ("commits", "prs_opened", "prs_merged", "reviews", "merges", "issues"):
            row[c] = _round(sum((by_month.get(m) or {}).get(c, 0) for m in last_n)
                            / len(last_n))
        row["activity"] = _round(sum(units(m) for m in last_n) / len(last_n))
        rates[str(n)] = row
    out["rates"] = rates
    k = min(3, len(months) // 2)
    if k < 1:
        out["trend"] = {"value": "insufficient",
                        "basis": "fewer than 2 gathered months"}
    else:
        recent = sum(units(m) for m in months[-k:]) / k
        prior = sum(units(m) for m in months[-2 * k:-k]) / k
        if recent == 0:
            value = "inactive"
        elif prior == 0:
            value = "rising"
        elif recent >= TREND_RISING * prior:
            value = "rising"
        elif recent <= TREND_FALLING * prior:
            value = "falling"
        else:
            value = "steady"
        out["trend"] = {"value": value, "basis": "last {k} vs previous {k} gathered "
                        "month(s): {r} vs {p} activity per month ({u})".format(
                            k=k, r=_round(recent), p=_round(prior),
                            u=" + ".join(ACTIVITY_UNITS))}
    last_day = max(_event_day(v) for v in last.values()) if last else seen_before
    if last_day:
        status, basis = _lifecycle(last_day, cov, limits["active_days"])
        if status == "active":
            active = [m for m in keep if any((by_month.get(m) or {}).values())]
            need = max(1, limits["active_days"] // 30)
            gathered = set(months)
            for a, b in zip(active, active[1:]):
                between = [m for m in gathered if a < m < b]
                if len(between) >= need:
                    status = "returned"
                    basis += "; back after {} gathered month(s) without activity " \
                        "({} to {})".format(len(between), a, b)
        out["status"], out["status_basis"] = status, basis
    maint = maintainer_status(entry, slug, activity, as_of, limits)
    if maint:
        out["maintainer"] = maint
    return out


def _maintainer_last_action(activity, login, slug, as_of):
    """(first, last) day of the maintainer-type actions the activity file
    holds (merges performed, approvals) as of `as_of`."""
    first = last = None
    for rec in _person_records(activity, login, slug, as_of).values():
        for ev in ("merge", "approval"):
            if ev in (rec.get("first") or {}):
                first = _earlier(first, _event_day(rec["first"][ev]))
            if ev in (rec.get("last") or {}):
                last = _later(last, _event_day(rec["last"][ev]))
    return first, last


def maintainer_status(entry, slug, activity=None, as_of=None, limits=None):
    """The maintainer role of `entry` in ONE repository with its lifecycle,
    or None when the registry holds no maintainer evidence there:
      {maintainer, signal, status, basis, first_evidence?, last_evidence?,
       merges_others?, approvals_merged?}
    The owner's plain true is status active, "former" former (maintainer
    false); a plain false is not a role (None). Stored evidence is never
    dropped, but its status reflects recency (_lifecycle with former_days):
    `last_evidence` is the latest maintainer-type action (registry evidence,
    or merges / approvals in the activity file) on or before `as_of`; a
    `former` status makes `maintainer` false. Pure."""
    limits = limits or thresholds({})
    if not entry:
        return None
    v = (entry.get("maintainer_of") or {}).get(slug)
    if v is True:
        return {"maintainer": True, "signal": "operator", "status": "active",
                "basis": "set by the owner"}
    if v == OPERATOR_FORMER:
        return {"maintainer": False, "signal": "operator", "status": "former",
                "basis": "set by the owner"}
    if not isinstance(v, dict) or v.get("maintainer") is not True:
        return None
    signal = "operator" if is_locked(entry, "maintainer_of") else (v.get("signal") or "none")
    a_first, a_last = _maintainer_last_action(activity, entry.get("login"), slug, as_of)
    first = v.get("first_evidence")
    last = v.get("last_evidence")
    if as_of is not None:
        first = first if first and first <= as_of else None
        last = last if last and last <= as_of else None
    first = _earlier(first, a_first)
    last = _later(last, a_last) or first
    if last is None:
        if as_of is not None and (v.get("first_evidence") or v.get("last_evidence")):
            return None              # not yet a maintainer at `as_of`
        out = {"maintainer": True, "signal": signal, "status": "unknown",
               "basis": "evidence without a date"}
    else:
        cov = coverage(activity, slug, as_of)
        status, basis = _lifecycle(last, cov, limits["active_days"],
                                   limits["former_days"])
        out = {"maintainer": status != "former", "signal": signal,
               "status": status, "basis": basis, "last_evidence": last}
        if first:
            out["first_evidence"] = first
    recs = _person_records(activity, entry.get("login"), slug, as_of)
    if recs:
        for c in ("merges_others", "approvals_merged"):
            n = sum(counts.get(c, 0) for rec in recs.values()
                    for counts in (rec.get("months") or {}).values())
            if n:
                out[c] = n
    return out


# --- cross-project context ----------------------------------------------------

def person_repos(activity, registry, login):
    """The repositories the registry knows `login` in: their activity, or
    maintainer evidence in people.json. Sorted."""
    repos = set()
    for part in ("people", "seen_before"):
        m = (activity or {}).get(part) or {}
        key, _ = find({"people": m}, login)
        repos |= set(m.get(key or login) or {})
    _k, entry = find(registry, login)
    repos |= set((entry or {}).get("maintainer_of") or {})
    return sorted(repos)


def cross_project(registry, activity, login, as_of=None, limits=None):
    """One person across every repository of the registry: {"repos":
    [{repo, stream?, role: maintainer | former_maintainer | contributor,
    status?, maintainer_status?, observed_since?, first?, last?, totals?}],
    "maintains": [repo] (maintainer status active / lapsed / unknown),
    "first_anywhere"?, "last_anywhere"? {date, repo}, "streams": {stream:
    {repos, maintains, totals, first?, last?}} (streams of more than one
    repository only)}. Registry context, not a fact of any window. Pure."""
    limits = limits or thresholds({})
    _k, entry = find(registry, login)
    rows, maintains, firsts, lasts = [], [], [], []
    streams = {}
    for slug in person_repos(activity, registry, login):
        prof = repo_profile(activity, login, slug, as_of, entry, limits)
        if not prof:
            continue
        stream = (((activity or {}).get("repos") or {}).get(slug) or {}).get("stream")
        m = prof.get("maintainer")
        role = "contributor"
        if m and m["status"] == "former":
            role = "former_maintainer"
        elif m and m.get("maintainer"):
            role = "maintainer"
            maintains.append(slug)
        row = {"repo": slug, "role": role}
        if stream:
            row["stream"] = stream
        if prof.get("status"):
            row["status"] = prof["status"]
        if m:
            row["maintainer_status"] = m["status"]
        if prof.get("observed_since"):
            row["observed_since"] = prof["observed_since"]
        days = [e["date"] for e in (prof.get("first") or {}).values()] + \
            ([prof["seen_before"]] if prof.get("seen_before") else [])
        ldays = [e["date"] for e in (prof.get("last") or {}).values()] or \
            ([prof["seen_before"]] if prof.get("seen_before") else [])
        if days:
            row["first"] = min(days)
            firsts.append((min(days), slug))
        if ldays:
            row["last"] = max(ldays)
            lasts.append((max(ldays), slug))
        if prof.get("totals"):
            row["totals"] = prof["totals"]
        rows.append(row)
        if stream:
            s = streams.setdefault(stream, {"repos": 0, "maintains": 0, "totals": {}})
            s["repos"] += 1
            s["maintains"] += role == "maintainer"
            for c, n in (prof.get("totals") or {}).items():
                s["totals"][c] = s["totals"].get(c, 0) + n
            if days:
                s["first"] = _earlier(s.get("first"), min(days))
            if ldays:
                s["last"] = _later(s.get("last"), max(ldays))
    stream_size = {}
    for slug, repo in ((activity or {}).get("repos") or {}).items():
        if repo.get("stream"):
            stream_size[repo["stream"]] = stream_size.get(repo["stream"], 0) + 1
    out = {"repos": rows, "maintains": maintains}
    if firsts:
        d, r = min(firsts)
        out["first_anywhere"] = {"date": d, "repo": r}
    if lasts:
        d, r = max(lasts)
        out["last_anywhere"] = {"date": d, "repo": r}
    out["streams"] = {k: streams[k] for k in sorted(streams)
                      if stream_size.get(k, 0) > 1}
    return out


def writer_context(registry, activity, login, slug, as_of=None, limits=None):
    """What a writer may say of `login` beside a report of repository
    `slug`, from the registry (never a fact of the window), or None for a
    do_not_name person or one the registry does not know:
      here       {active_since, active_since_is_first_ever, observed_since,
                  prs_merged_last_6_months, months_counted, status,
                  maintainer_status?, trend?} - this repository, as observed
      also_maintains / also_contributes_to   [{repo, status}] elsewhere
      first_anywhere?   {date, repo}
      streams?   multi-repository streams the person works in
    Pure."""
    limits = limits or thresholds({})
    _k, entry = find(registry, login)
    if (entry or {}).get("do_not_name"):
        return None
    cp = cross_project(registry, activity, login, as_of, limits)
    if not cp["repos"]:
        return None
    out = {}
    prof = repo_profile(activity, login, slug, as_of, entry, limits)
    if prof and (prof.get("first") or prof.get("seen_before")):
        months = (prof.get("coverage") or {}).get("months") or []
        last6 = months[-6:]
        days = [e["date"] for e in (prof.get("first") or {}).values()]
        since = min(days + ([prof["seen_before"]] if prof.get("seen_before") else []))
        here = {"active_since": since,
                "active_since_is_first_ever": bool(prof.get("first_pr_ever")
                                                   or prof.get("first_issue_ever")),
                "observed_since": prof["observed_since"],
                "prs_merged_last_6_months": sum(
                    (prof["by_month"].get(m) or {}).get("prs_merged", 0) for m in last6),
                "months_counted": len(last6)}
        if prof.get("seen_before") and prof["seen_before"] == since:
            here["active_since_basis"] = "seen in stored records dated before " \
                "the coverage (not counted)"
        if prof.get("status"):
            here["status"] = prof["status"]
        if prof.get("trend"):
            here["trend"] = prof["trend"]["value"]
        if prof.get("maintainer"):
            here["maintainer_status"] = prof["maintainer"]["status"]
        out["here"] = here
    elif prof and prof.get("maintainer"):
        out["here"] = {"maintainer_status": prof["maintainer"]["status"]}
    others = [r for r in cp["repos"] if r["repo"] != slug]
    maint = [{"repo": r["repo"], "status": r["maintainer_status"]} for r in others
             if r["role"] == "maintainer"]
    contrib = [{"repo": r["repo"], "status": r.get("status", "unknown")}
               for r in others if r["role"] != "maintainer" and r.get("status")]
    if maint:
        out["also_maintains"] = maint
    if contrib:
        out["also_contributes_to"] = contrib
    if cp.get("first_anywhere"):
        out["first_anywhere"] = cp["first_anywhere"]
    if cp.get("streams"):
        out["streams"] = cp["streams"]
    return out or None


def load_view_source(people_path, env=None):
    """What link --people reads: {"registry", "activity", "people_text",
    "activity_text", "limits"}. The registry file must exist (an explicit
    request); a missing activity file is an empty one. Raises OSError or
    RegistryError (a ValueError)."""
    with open(people_path, encoding="utf-8") as fh:
        people_text = fh.read()
    apath = activity_path(people_path)
    try:
        with open(apath, encoding="utf-8") as fh:
            activity_text = fh.read()
        activity = parse_activity(activity_text, apath)
    except FileNotFoundError:
        activity_text, activity = "", empty_activity()
    return {"registry": parse_registry(people_text, people_path),
            "activity": activity, "people_text": people_text,
            "activity_text": activity_text, "limits": thresholds(env)}


def registry_stamp(people_text, activity_text, activity, as_of=None):
    """The state of the registry a context was read from: the two files'
    sha256 and the latest gathered day (as of) over every repository."""
    import hashlib
    latest = None
    for slug in sorted(((activity or {}).get("repos") or {})):
        cov = coverage(activity, slug, as_of)
        if cov and cov.get("to"):
            latest = _later(latest, cov["to"])
    out = {"people_sha256": hashlib.sha256((people_text or "").encode("utf-8")).hexdigest(),
           "activity_sha256": hashlib.sha256((activity_text or "").encode("utf-8")).hexdigest()}
    if latest:
        out["latest_gathered"] = latest
    return out


# --- one run of gather -------------------------------------------------------

class Run:
    """One gather run's view of the registry: the file as loaded, this run's
    patches (applied in memory so a manifest's later members see them) and
    the counters / budgets of the run. `save()` merges the patches into the
    file as it is on disk at that moment."""

    def __init__(self, path, registry=None, now=None, offline=False,
                 refresh_days=REFRESH_DAYS, unknown_retry_days=UNKNOWN_RETRY_DAYS,
                 refresh_max=REFRESH_MAX, name_max=NAME_MAX, limits=None,
                 activity=None):
        self.path = path
        self.registry = registry if registry is not None else load_registry(path)
        self.activity = activity if activity is not None else \
            load_activity(activity_path(path))
        self.limits = limits or thresholds()
        self.windows = []
        self.now = now or today()
        self.offline = offline
        self.refresh_days = refresh_days
        self.unknown_retry_days = unknown_retry_days
        self.refresh_left = refresh_max
        self.names_left = name_max
        self.patches = {}
        self.totals = {"looked_up": 0, "refreshed": 0, "reused": 0,
                       "names_fetched": 0}

    def entry(self, login):
        """The entry as this run sees it (file + this run's patches)."""
        key, e = find(self.registry, login)
        p = self.patches.get(key or login)
        return apply_patch(e, p, key or login) if p else e

    def patch(self, login, **fields):
        key, _e = find(self.registry, login)
        key = key or login
        self.patches[key] = merge_patch(self.patches.get(key), fields)

    def state(self, login, home=None):
        return lookup_state(self.entry(login), self.now, self.refresh_days,
                            self.unknown_retry_days, home)

    def record_window(self, slug, frm, to, prs=(), issues=(), commits=(),
                      newcomers=None, stream=None, source="gather"):
        """Record one gathered window of repository `slug` in the activity
        profile (window_contributions + fold_window): applied in memory now
        (so maintainer_status sees it) and folded into the file by save()."""
        contributions, stats = window_contributions(prs, issues, commits, frm, to,
                                                    newcomers)
        w = {"slug": slug, "frm": frm, "to": to, "contributions": contributions,
             "stats": stats, "stream": stream, "source": source}
        self.activity = fold_window(self.activity, **w)
        self.windows.append(w)

    def maintainer_status(self, login, slug):
        return maintainer_status(self.entry(login), slug, self.activity,
                                 limits=self.limits)

    def save(self):
        """Merge this run's patches and windows into the files; returns True
        when one changed. The in-memory registry becomes the file's new
        content."""
        if not self.patches and not self.windows:
            return False
        self.registry, changed = update_registry(self.path, self.patches,
                                                 self.windows)
        if self.windows:
            self.activity = load_activity(activity_path(self.path))
        self.patches, self.windows = {}, []
        return changed


# --- seeding from stores (no API call) ----------------------------------------

def store_records(conn):
    """{repo slug: {"prs", "issues", "commits", "logins", "affiliations"}}
    from one store's stored records: PR / issue records with their review
    submissions attached, resolved commits, the people of each repository and
    the labels of any stored `affiliations` window fact (latest window
    last). Read only."""
    import graphstore
    out = {}

    def slot(project, repo):
        slug = repo if "/" in repo else "{}/{}".format(project, repo)
        return out.setdefault(slug, {"prs": {}, "issues": [], "commits": [],
                                     "reviews": {}, "affiliations": {},
                                     "windows": [], "project": project,
                                     "repo": repo})
    rows = conn.execute("SELECT id, project, repo, node_class, data FROM nodes "
                        "WHERE node_class IN ('social', 'code') ORDER BY id")
    for nid, project, repo, cls, data in rows:
        if repo == "*":
            continue
        local = nid.split("#", 1)[1] if "#" in nid else nid
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        s = slot(project, repo)
        if cls == "social" and local.startswith("pr-"):
            s["prs"][d.get("number")] = d
        elif cls == "social" and local.startswith("issue-"):
            s["issues"].append(d)
        elif cls == "social" and local.startswith("review-"):
            try:
                n = int(local.split("-")[1])
            except (IndexError, ValueError):
                continue
            s["reviews"].setdefault(n, []).append(d)
        elif cls == "code" and re.fullmatch(r"[0-9a-f]{40}", local) and d.get("author"):
            s["commits"].append(d)
    try:
        ledger = graphstore.get_windows(conn)
    except ValueError:
        ledger = []
    for w in sorted(ledger, key=lambda w: (w.get("to") or "", w.get("from") or "")):
        project, repo = w.get("project"), w.get("repo")
        if not project or not repo:
            continue
        got = graphstore.get_window_facts(conn, project, repo, w["from"], w["to"],
                                          keys=("affiliations", "newcomers"))
        facts = (got or {}).get("facts") or {}
        fact = facts.get("affiliations")
        if isinstance(fact, dict) and fact.get("collected"):
            for login, e in (fact.get("people") or {}).items():
                if isinstance(e, dict):
                    slot(project, repo)["affiliations"][login] = e
        slot(project, repo)["windows"].append(
            {"from": w["from"][:10], "to": w["to"][:10],
             "newcomers": facts.get("newcomers")})
    for s in out.values():
        prs = []
        for n in sorted(s["prs"], key=lambda n: (n is None, n)):
            pr = dict(s["prs"][n])
            if s["reviews"].get(n) and not pr.get("reviews"):
                pr["reviews"] = sorted(s["reviews"][n],
                                       key=lambda r: str(r.get("submitted_at")))
            prs.append(pr)
        s["prs"] = prs
        del s["reviews"]
    return out


def seed_patches(records, slug):
    """{login: patch} for one repository's stored records: seen dates,
    maintainer evidence, and the label of an author_association OWNER /
    MEMBER / COLLABORATOR or of a stored `affiliations` fact entry decided
    by GitHub data (never a lookup date: the next gather looks them up)."""
    import gather
    prs, issues, commits = records["prs"], records["issues"], records["commits"]
    patches = {}
    for login, (first, last) in activity_dates(prs, issues, commits).items():
        patches[login] = {"seen": [(first, slug), (last, slug)]}
    until = max((w["to"] for w in records.get("windows") or []), default=None)
    for login, ev in maintainer_evidence(prs, issues, until=until).items():
        patches.setdefault(login, {})["maintainer"] = {slug: ev}
    _order, assoc = gather.affiliation_people(prs, issues, commits)
    for login, a in assoc.items():
        if login in patches and a in gather._HOME_ASSOCIATIONS:
            patches[login]["label"] = {"affiliation": "maintainer_org",
                                       "signal": "author_association",
                                       "strength": gather._HOME_ASSOCIATIONS[a]}
    for login, e in (records.get("affiliations") or {}).items():
        if login in patches and "label" not in patches[login] and \
                e.get("signal") in ("public_membership", "profile_company",
                                    "author_association"):
            label = {"affiliation": e["affiliation"], "signal": e["signal"]}
            if e.get("strength"):
                label["strength"] = e["strength"]
            patches[login]["label"] = label
    return patches


def seed_windows(records, slug, stream=None):
    """fold_window arguments for every gathered window of one repository's
    stored records (store_records): each window's contributions are the
    records' events dated inside it, with the window's `newcomers` fact."""
    out = []
    seen = set()
    for w in records.get("windows") or []:
        key = (w["from"], w["to"])
        if key in seen:
            continue
        seen.add(key)
        contributions, stats = window_contributions(
            records["prs"], records["issues"], records["commits"], w["from"],
            w["to"], w.get("newcomers"))
        out.append({"slug": slug, "frm": w["from"], "to": w["to"],
                    "contributions": contributions, "stats": stats,
                    "stream": stream, "source": "seed"})
    return out


def _activity_units(prof, n="3"):
    return ((prof or {}).get("rates") or {}).get(n, {}).get("activity", 0)


def summarize(registry, repo=None, now=None, activity=None, limits=None,
              top=5):
    """Counts of a registry: people, by affiliation, named, tagged,
    do_not_name, due for a refresh, the maintainers by repository and
    signal (with their logins) and, from the activity file: the coverage of
    each repository, the maintainers by status, newcomers by month (first
    ever from the newcomer search; first observed after the coverage's
    first month), the `top` busiest people of each repository (activity per
    month over the last 3 gathered months) and the people known in more
    than one repository."""
    now = now or today()
    limits = limits or thresholds({})
    act = activity or empty_activity()
    people = (registry or {}).get("people") or {}
    out = {"people": len(people),
           "affiliation": {k: 0 for k in AFFILIATION_VALUES + ("not_set",)},
           "named": 0, "tagged": 0, "do_not_name": 0, "operator_entries": 0,
           "lookup": {"never": 0, "fresh": 0, "stale": 0},
           "maintainers": {}}
    for login in sorted(people, key=str.lower):
        e = people[login]
        out["affiliation"][e.get("affiliation") or "not_set"] += 1
        out["named"] += bool(e.get("name"))
        out["tagged"] += bool(e.get("tags"))
        out["do_not_name"] += bool(e.get("do_not_name"))
        out["operator_entries"] += e.get("source") == "operator"
        out["lookup"][lookup_state(e, now)] += 1
        for slug in sorted(e.get("maintainer_of") or {}):
            if repo and slug != repo:
                continue
            is_m, signal = maintainer_for(e, slug)
            if not is_m:
                continue
            by = out["maintainers"].setdefault(slug, {})
            by.setdefault(signal, []).append(login)
    repos = sorted(set(act.get("repos") or {}) | set(out["maintainers"]))
    if repo:
        repos = [r for r in repos if r == repo]
    if not act.get("repos"):
        return out
    out["coverage"] = {}
    out["maintainer_status"] = {}
    out["newcomers_by_month"] = {}
    out["busiest"] = {}
    logins = sorted(set(people) | set(act.get("people") or {}), key=str.lower)
    multi = []
    for slug in repos:
        cov = coverage(act, slug)
        if cov:
            out["coverage"][slug] = {k: cov[k] for k in ("from", "to", "windows", "gaps")
                                     if k in cov}
        status, newc, busy = {}, {}, []
        first_month = (cov or {}).get("months", [None])[0] if cov else None
        for login in logins:
            _k, entry = find(registry, login)
            prof = repo_profile(act, login, slug, entry=entry, limits=limits)
            if not prof:
                continue
            m = prof.get("maintainer")
            if m:
                status.setdefault(m["status"], []).append(login)
            if prof.get("first") and not prof.get("seen_before"):
                day = min(e["date"] for e in prof["first"].values())
                month = day[:7]
                row = newc.setdefault(month, {"first_ever": [], "first_observed": 0})
                if prof.get("first_pr_ever") or prof.get("first_issue_ever"):
                    row["first_ever"].append(login)
                if month != first_month:
                    row["first_observed"] += 1
            if prof.get("first"):
                busy.append((-_activity_units(prof), login.lower(), login, prof))
        out["maintainer_status"][slug] = {k: status[k] for k in MAINTAINER_STATUSES
                                          if k in status}
        out["newcomers_by_month"][slug] = {
            k: newc[k] for k in sorted(newc)
            if k != first_month or newc[k]["first_ever"]}
        rows = []
        for _n, _l, login, prof in sorted(busy)[:top]:
            rate = (prof.get("rates") or {}).get("3") or {}
            rows.append({"login": login, "activity_per_month": rate.get("activity", 0),
                         "months": rate.get("months", 0),
                         "rates_3": {k: rate.get(k) for k in ("commits", "prs_opened",
                                                               "prs_merged", "reviews",
                                                               "merges")},
                         "trend": (prof.get("trend") or {}).get("value"),
                         "status": prof.get("status")})
        out["busiest"][slug] = rows
    for login in logins:
        if len(person_repos(act, registry, login)) > 1:
            cp = cross_project(registry, act, login, limits=limits)
            multi.append({"login": login,
                          "repos": [{"repo": r["repo"], "role": r["role"],
                                     "status": r.get("status")} for r in cp["repos"]]})
    out["multi_repo"] = multi
    return out


def _summary_text(s):
    lines = ["people: {}".format(s["people"]),
             "affiliation: " + ", ".join("{} {}".format(v, k)
                                         for k, v in s["affiliation"].items()),
             "named: {}, tagged: {}, do_not_name: {}, operator entries: {}".format(
                 s["named"], s["tagged"], s["do_not_name"], s["operator_entries"]),
             "lookups: {never} never, {fresh} fresh, {stale} due for refresh".format(
                 **s["lookup"])]
    for slug in sorted(s["maintainers"]):
        by = s["maintainers"][slug]
        total = sum(len(v) for v in by.values())
        lines.append("maintainers of {}: {}".format(slug, total))
        for signal in MAINTAINER_SIGNALS:
            if signal in by:
                lines.append("  {} ({}): {}".format(signal, len(by[signal]),
                                                    ", ".join(by[signal])))
    for slug, cov in sorted((s.get("coverage") or {}).items()):
        lines.append("coverage of {}: {} to {}, {} window(s){}".format(
            slug, cov.get("from"), cov.get("to"), cov.get("windows"),
            "; gaps: " + ", ".join("{}..{}".format(*g) for g in cov["gaps"])
            if cov.get("gaps") else ", no gap"))
        st = (s.get("maintainer_status") or {}).get(slug) or {}
        if st:
            lines.append("  maintainers by status: " + "; ".join(
                "{} ({}): {}".format(k, len(v), ", ".join(v)) for k, v in st.items()))
        nm = (s.get("newcomers_by_month") or {}).get(slug) or {}
        for month, row in nm.items():
            lines.append("  {}: {} first observed{}".format(
                month, row["first_observed"],
                ", first ever (newcomer search): " + ", ".join(row["first_ever"])
                if row["first_ever"] else ""))
        busy = (s.get("busiest") or {}).get(slug) or []
        if busy:
            lines.append("  busiest (activity per month, last {} gathered "
                         "month(s)):".format(busy[0]["months"]))
            for r in busy:
                lines.append("    {:<24} {:>6}  merged PRs {} reviews {} merges {} "
                             "commits {}  trend {}, {}".format(
                                 r["login"], r["activity_per_month"],
                                 r["rates_3"]["prs_merged"], r["rates_3"]["reviews"],
                                 r["rates_3"]["merges"], r["rates_3"]["commits"],
                                 r["trend"], r["status"]))
    if s.get("multi_repo") is not None:
        lines.append("people in more than one repository (activity, an earlier sighting or a role): {}".format(
            len(s["multi_repo"])))
        for row in s["multi_repo"]:
            lines.append("  {}: {}".format(row["login"], "; ".join(
                "{} ({}{})".format(r["repo"], r["role"],
                                   ", " + r["status"] if r.get("status") else "")
                for r in row["repos"])))
    return "\n".join(lines) + "\n"


def show(registry, activity, login, limits=None):
    """The full picture of one person across repositories: the registry
    entry, then per repository the derived profile, then the cross-project
    summary. None when the registry and the activity file hold nothing."""
    limits = limits or thresholds({})
    key, entry = find(registry, login)
    repos = person_repos(activity, registry, login)
    if entry is None and not repos:
        return None
    return {"login": key or login, "entry": entry,
            "repos": {slug: repo_profile(activity, key or login, slug, entry=entry,
                                         limits=limits) for slug in repos},
            "cross_project": cross_project(registry, activity, key or login,
                                           limits=limits)}


def _show_text(s):
    e = s.get("entry") or {}
    lines = ["{}{}".format(s["login"], " ({})".format(e["name"]) if e.get("name") else "")]
    if e.get("do_not_name"):
        lines.append("  do_not_name: every output shows the login only and no "
                     "activity profile")
    if e.get("affiliation"):
        lines.append("  affiliation: {} (signal {}{})".format(
            e["affiliation"], e.get("signal"),
            ", " + e["strength"] if e.get("strength") else ""))
    for k in ("tags", "note", "source", "locked", "looked_up_at"):
        if e.get(k) not in (None, [], False, ""):
            lines.append("  {}: {}".format(k, e[k]))
    for k in ("first_seen", "last_seen"):
        if e.get(k):
            lines.append("  {} (anywhere, as gathered): {} in {}".format(
                k, e[k]["date"], e[k].get("repo", "?")))
    for slug, prof in sorted((s.get("repos") or {}).items()):
        lines.append("")
        lines.append("== {} ==".format(slug))
        if not prof:
            lines.append("  nothing gathered")
            continue
        cov = prof.get("coverage") or {}
        lines.append("  coverage: {} to {} ({} window(s){}); every first and total "
                     "below is as observed since {}".format(
                         cov.get("from"), cov.get("to"), cov.get("windows"),
                         "; gaps " + ", ".join("{}..{}".format(*g) for g in cov["gaps"])
                         if cov.get("gaps") else "", prof.get("observed_since") or cov.get("from")))
        m = prof.get("maintainer")
        if m:
            lines.append("  maintainer: {} (signal {}; {}{})".format(
                m["status"], m["signal"], m["basis"],
                "; first evidence {}".format(m["first_evidence"])
                if m.get("first_evidence") else ""))
            extra = ["{} {}".format(m[c], c) for c in ("merges_others", "approvals_merged")
                     if m.get(c)]
            if extra:
                lines.append("    observed: " + ", ".join(extra))
        if prof.get("status"):
            lines.append("  contributor status: {} ({})".format(
                prof["status"], prof["status_basis"]))
        if prof.get("seen_before"):
            lines.append("  seen before the coverage: {} (in stored records; not "
                         "counted)".format(prof["seen_before"]))
        for side in ("first", "last"):
            for ev, v in (prof.get(side) or {}).items():
                ref = "#{}".format(v["number"]) if "number" in v else (
                    v["sha"][:12] if "sha" in v else "")
                flag = ""
                if side == "first" and ev == "pr_opened":
                    flag = " (first ever: newcomer search)" if prof.get("first_pr_ever") \
                        else " (as observed)"
                if side == "first" and ev == "issue":
                    flag = " (first ever: newcomer search)" if prof.get("first_issue_ever") \
                        else " (as observed)"
                lines.append("  {:<5} {:<9} {} {}{}".format(side, ev, v["date"], ref, flag))
        if prof.get("totals"):
            lines.append("  totals (observed): " + ", ".join(
                "{} {}".format(v, k) for k, v in prof["totals"].items()))
        for month, counts in (prof.get("by_month") or {}).items():
            lines.append("    {:<7} ".format(month) + ", ".join(
                "{} {}".format(v, k) for k, v in counts.items()))
        shown = set()
        for n, r in sorted((prof.get("rates") or {}).items(), key=lambda t: int(t[0])):
            if r["months"] in shown:
                continue                 # fewer gathered months than asked
            shown.add(r["months"])
            lines.append("  rate over last {} gathered month(s) ({}..{}): {} activity/"
                         "month; merged PRs {}, reviews {}, merges {}, commits {}".format(
                             r["months"], r["from"], r["to"], r["activity"],
                             r["prs_merged"], r["reviews"], r["merges"], r["commits"]))
        if prof.get("trend"):
            lines.append("  trend: {} ({})".format(prof["trend"]["value"],
                                                   prof["trend"]["basis"]))
    cp = s.get("cross_project") or {}
    lines.append("")
    lines.append("== across repositories (registry context) ==")
    for r in cp.get("repos") or []:
        lines.append("  {}: {}{}{}".format(
            r["repo"], r["role"],
            ", " + r["status"] if r.get("status") else "",
            ", maintainer " + r["maintainer_status"] if r.get("maintainer_status") else ""))
    if cp.get("maintains"):
        lines.append("  maintains: " + ", ".join(cp["maintains"]))
    for k in ("first_anywhere", "last_anywhere"):
        if cp.get(k):
            lines.append("  {}: {} in {} (as observed)".format(
                k, cp[k]["date"], cp[k]["repo"]))
    for stream, row in (cp.get("streams") or {}).items():
        lines.append("  stream {}: {} repo(s), maintains {}, {}".format(
            stream, row["repos"], row["maintains"], ", ".join(
                "{} {}".format(v, k) for k, v in row["totals"].items())))
    return "\n".join(lines) + "\n"


def check_files(people_path):
    """[(level, message)] for the registry and its activity file: errors
    (a file this skill would refuse) and warnings (logins in the activity
    file without a registry entry, entries with neither activity nor a
    role, repositories with coverage gaps). Empty: all is well."""
    problems = []
    reg = act = None
    try:
        reg = load_registry(people_path)
    except RegistryError as exc:
        problems.append(("error", str(exc)))
    apath = activity_path(people_path)
    try:
        act = load_activity(apath)
    except RegistryError as exc:
        problems.append(("error", str(exc)))
    if reg is None or act is None:
        return problems
    known = {k.lower() for k in reg["people"]}
    for login in sorted(act.get("people") or {}, key=str.lower):
        if login.lower() not in known:
            problems.append(("warning", "{}: {!r} has activity but no entry in {} "
                             "(a seed or gather adds it)".format(apath, login,
                                                                people_path)))
    for slug in sorted(act.get("repos") or {}):
        cov = coverage(act, slug)
        if cov and cov.get("gaps"):
            problems.append(("warning", "{}: coverage of {} has gap(s) {}: activity "
                             "there is not known".format(apath, slug, ", ".join(
                                 "{}..{}".format(*g) for g in cov["gaps"]))))
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(
        description="The retained people registry and its activity profile. "
                    "`seed` / `update` (the same) add / update people and their "
                    "per-repository activity from stored records (logins, "
                    "maintainer evidence, author_association labels, one "
                    "contribution record per gathered window: re-reading a "
                    "store replaces its windows, never counts twice; no API "
                    "call); `summary` prints the counts, the coverage, the "
                    "maintainers by status, newcomers by month, the busiest "
                    "people and the people in more than one repository; "
                    "`show LOGIN` prints one person across repositories; "
                    "`check` validates both files and explains any problem "
                    "(exit 1 on an error).")
    p.add_argument("command", choices=("seed", "update", "summary", "show", "check"))
    p.add_argument("stores", nargs="*", metavar="STORE|LOGIN",
                   help="seed / update: store files to read (read only); show: "
                        "the login")
    p.add_argument("--people", required=True, metavar="PATH",
                   help="the registry file (created by seed when missing); the "
                        "activity file is beside it (people-activity.json)")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                   help="only these repositories (repeatable; summary: the first)")
    p.add_argument("--stream", metavar="NAME",
                   help="seed / update: the stream (newsletter) the repositories "
                        "belong to, for the per-stream roll-up (default: a "
                        "manifest store's project; none for a single-repo store)")
    p.add_argument("--active-days", type=int, metavar="N",
                   help="lifecycle: active within N days of the repository's "
                        "latest gathered day (default {}, "
                        "ACTIVITY_PEOPLE_ACTIVE_DAYS)".format(ACTIVE_DAYS))
    p.add_argument("--former-days", type=int, metavar="N",
                   help="lifecycle: a maintainer without evidence for more than N "
                        "days is former (default {}, ACTIVITY_PEOPLE_FORMER_DAYS)"
                   .format(FORMER_DAYS))
    p.add_argument("--json", action="store_true", help="print JSON")
    args = p.parse_intermixed_args(argv)
    if args.command in ("seed", "update") and not args.stores:
        p.error("{} needs at least one STORE".format(args.command))
    if args.command == "show" and len(args.stores) != 1:
        p.error("show needs exactly one LOGIN")
    try:
        limits = thresholds(None, args.active_days, args.former_days)
    except ValueError as exc:
        p.error(str(exc))
    import graphstore
    try:
        if args.command == "check":
            problems = check_files(args.people)
            for level, msg in problems:
                sys.stdout.write("{}: {}\n".format(level, msg))
            errors = sum(1 for level, _m in problems if level == "error")
            sys.stdout.write("{}: {} error(s), {} warning(s)\n".format(
                "FAILED" if errors else "OK", errors, len(problems) - errors))
            return 1 if errors else 0
        if args.command in ("seed", "update"):
            load_registry(args.people)            # fail before reading stores
            load_activity(activity_path(args.people))
            patches, windows = {}, []
            for path in args.stores:
                conn = graphstore.open_store_readonly(path)
                try:
                    recs = store_records(conn)
                finally:
                    conn.close()
                for slug in sorted(recs):
                    if args.repo and slug not in args.repo:
                        continue
                    for login, pt in seed_patches(recs[slug], slug).items():
                        key, _ = find({"people": patches}, login)
                        patches[key or login] = merge_patch(
                            patches.get(key or login), pt)
                    project = recs[slug].get("project")
                    stream = args.stream or (
                        project if project and project != slug.split("/")[0] else None)
                    windows += seed_windows(recs[slug], slug, stream)
            reg, changed = update_registry(args.people, patches, windows)
            sys.stderr.write("people registry {}: {} person(s), {} window(s) from {} "
                             "store(s), {}\n".format(
                                 args.people, len(patches), len(windows),
                                 len(args.stores), "updated" if changed else "unchanged"))
            summary = summarize(reg, args.repo[0] if len(args.repo) == 1 else None,
                                activity=load_activity(activity_path(args.people)),
                                limits=limits)
        elif args.command == "show":
            got = show(load_registry(args.people),
                       load_activity(activity_path(args.people)), args.stores[0],
                       limits)
            if got is None:
                raise SystemExit("error: {!r} is not in {} or its activity file".format(
                    args.stores[0], args.people))
            sys.stdout.write(json.dumps(got, indent=2, sort_keys=True,
                                        ensure_ascii=False) + "\n"
                             if args.json else _show_text(got))
            return 0
        else:
            summary = summarize(load_registry(args.people),
                                args.repo[0] if args.repo else None,
                                activity=load_activity(activity_path(args.people)),
                                limits=limits)
    except RegistryError as exc:
        raise SystemExit("error: {}".format(exc)) from None
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n"
                     if args.json else _summary_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
