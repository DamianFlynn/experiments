#!/usr/bin/env python3
"""The retained people registry: one human-editable record per GitHub login,
shared by every gather of a workspace (all months, all repositories of a
newsletter stream), so a person is looked up once and refreshed rarely.

Where and what (docs/USAGE.md section 5.7, BUNDLE.md "The people registry"):

* one JSON file per workspace, by default `<stream>/people.json` next to
  `stores/`, passed to gather with `--people PATH` or a manifest's `people`;
* {"format": FORMAT, "version": VERSION, "people": {login: entry}}, written
  with sorted keys, two-space indent and a trailing newline, atomically
  (temporary file + rename) under an advisory lock, and only when the text
  changes, so version control shows one line per real change;
* an entry holds only: login, name (the public profile's display name),
  affiliation / signal / strength, maintainer_of {repo: evidence}, tags,
  first_seen / last_seen, looked_up_at, source, locked, do_not_name, note.
  Never a company, bio, email or any other profile text; an unknown key is
  an error naming the entry.

Operator edits win. An entry whose `source` is "operator", or whose `locked`
is true, keeps every decided field (name, affiliation, maintainer_of) as the
operator wrote it; `locked: ["name", ...]` pins only the fields listed; a
`maintainer_of` value written as plain true / false is the operator's and is
never changed. `tags`, `do_not_name` and `note` are the operator's alone: the
skill never writes them. Observations (first_seen / last_seen) are always
updated.

This module is pure apart from load / write / update_registry and the CLI
(`python3 people.py seed ...` / `summary ...`), which never calls the network.
"""
import argparse
import contextlib
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
              "looked_up_at", "source", "locked", "do_not_name", "note")
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
                         "true | false | {...}}")
    for repo, v in mo.items():
        where = "`maintainer_of[{!r}]`".format(repo)
        if not _SLUG_RE.fullmatch(repo or ""):
            raise ValueError("{}: the key must be owner/name".format(where))
        if isinstance(v, bool):
            continue
        if not isinstance(v, dict) or set(v) - {"maintainer", "signal",
                                                "strength", "last_evidence"}:
            raise ValueError("{} must be true, false or {{\"maintainer\", "
                             "\"signal\", \"strength\", \"last_evidence\"}}"
                             .format(where))
        if not isinstance(v.get("maintainer"), bool):
            raise ValueError("{}.maintainer must be true or false".format(where))
        if "signal" in v and v["signal"] not in MAINTAINER_SIGNALS:
            raise ValueError("{}.signal must be one of {}, got {!r}".format(
                where, ", ".join(MAINTAINER_SIGNALS), v["signal"]))
        if "strength" in v and v["strength"] not in STRENGTHS:
            raise ValueError("{}.strength must be one of {}".format(
                where, ", ".join(STRENGTHS)))
        if "last_evidence" in v:
            _check_date(v["last_evidence"], where + ".last_evidence")
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
    nothing is written when the text would not change. Returns True when the
    file changed."""
    text = dumps(registry)
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


def update_registry(path, patches):
    """Re-read the file under the lock, apply `patches` ({login: patch}, see
    apply_patch) over what is on disk now (so an edit made while a gather
    ran is kept) and write it back. Returns (registry, changed)."""
    with _locked(path):
        reg = load_registry(path)
        reg = apply_patches(reg, patches)
        changed = write_registry(path, reg)
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
                 unknown_retry_days=UNKNOWN_RETRY_DAYS):
    """"never" (no lookup or no label yet), "fresh" (reuse: no call) or
    "stale" (due for a refresh): a decided label is fresh for
    `refresh_days`, an `unknown` one for `unknown_retry_days`. Pure."""
    if not entry or not entry.get("looked_up_at") or not entry.get("affiliation"):
        return "never"
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
    plain true / false (signal operator), the stored evidence, or (False,
    "none") when the registry holds no evidence of write access there."""
    v = ((entry or {}).get("maintainer_of") or {}).get(repo)
    if isinstance(v, bool):
        return v, "operator"
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
      label       {affiliation, signal, strength?}   unless affiliation locked
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
            if isinstance(cur, bool):
                continue                  # the operator's own setting
            new = {"maintainer": True, "signal": ev["signal"],
                   "strength": MAINTAINER_STRENGTH.get(ev["signal"], "medium")}
            if ev.get("last_evidence"):
                new["last_evidence"] = ev["last_evidence"]
            if isinstance(cur, dict) and cur.get("maintainer") is True:
                if _signal_rank(cur.get("signal")) <= _signal_rank(ev["signal"]):
                    new = dict(cur)
                last = _later(cur.get("last_evidence"), ev.get("last_evidence"))
                if last:
                    new["last_evidence"] = last
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


def maintainer_evidence(prs=(), issues=(), approvals_min=APPROVALS_MIN):
    """{login: {"signal", "last_evidence"}}: who the records show holding
    write access to the repository, by the strongest signal:
      merged_others_pr   merged a PR somebody else authored        strong
      OWNER              author_association OWNER on their records strong
      merged_own_pr      merged their own PR                       medium
      COLLABORATOR       author_association COLLABORATOR           medium
      approving_reviewer approved at least `approvals_min` PRs of
                         others that were then merged              medium
    MEMBER (of the organisation that owns the repo) is an affiliation, not a
    role: it is not a maintainer signal. Pure."""
    found = {}

    def add(login, signal, ts):
        if not _human(login):
            return
        cur = found.get(login)
        if cur is None or _signal_rank(signal) < _signal_rank(cur["signal"]):
            cur = {"signal": signal, "last_evidence": (cur or {}).get("last_evidence")}
        cur["last_evidence"] = _later(cur.get("last_evidence"), _day(ts))
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
                    and r.get("author") and r.get("author") != a:
                approvers[r["author"]] = _later(approvers.get(r["author"]),
                                                _day(r.get("submitted_at")))
        for login, day in approvers.items():
            n, last = approvals.get(login, (0, None))
            approvals[login] = (n + 1, _later(last, day))
    for login, (n, last) in approvals.items():
        if n >= approvals_min:
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
        if not found[login].get("last_evidence"):
            found[login].pop("last_evidence", None)
    return {k: found[k] for k in sorted(found)}


# --- one run of gather -------------------------------------------------------

class Run:
    """One gather run's view of the registry: the file as loaded, this run's
    patches (applied in memory so a manifest's later members see them) and
    the counters / budgets of the run. `save()` merges the patches into the
    file as it is on disk at that moment."""

    def __init__(self, path, registry=None, now=None, offline=False,
                 refresh_days=REFRESH_DAYS, unknown_retry_days=UNKNOWN_RETRY_DAYS,
                 refresh_max=REFRESH_MAX, name_max=NAME_MAX):
        self.path = path
        self.registry = registry if registry is not None else load_registry(path)
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

    def state(self, login):
        return lookup_state(self.entry(login), self.now, self.refresh_days,
                            self.unknown_retry_days)

    def save(self):
        """Merge this run's patches into the file; returns True when it
        changed. The in-memory registry becomes the file's new content."""
        if not self.patches:
            return False
        self.registry, changed = update_registry(self.path, self.patches)
        self.patches = {}
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
                                     "project": project, "repo": repo})
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
                                          keys=("affiliations",))
        fact = ((got or {}).get("facts") or {}).get("affiliations")
        if isinstance(fact, dict) and fact.get("collected"):
            for login, e in (fact.get("people") or {}).items():
                if isinstance(e, dict):
                    slot(project, repo)["affiliations"][login] = e
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
    for login, ev in maintainer_evidence(prs, issues).items():
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


def summarize(registry, repo=None, now=None):
    """Counts of a registry: people, by affiliation, named, tagged,
    do_not_name, due for a refresh, and the maintainers by repository and
    signal (with their logins)."""
    now = now or today()
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
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(
        description="The retained people registry: `seed` adds / updates people "
                    "from stored records (logins, first / last seen, maintainer "
                    "evidence, author_association labels; no API call); "
                    "`summary` prints the counts and the maintainers found.")
    p.add_argument("command", choices=("seed", "summary"))
    p.add_argument("stores", nargs="*", metavar="STORE",
                   help="seed: store files to read (read only)")
    p.add_argument("--people", required=True, metavar="PATH",
                   help="the registry file (created by seed when missing)")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                   help="only these repositories (repeatable; summary: the first)")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = p.parse_intermixed_args(argv)
    if args.command == "seed" and not args.stores:
        p.error("seed needs at least one STORE")
    import graphstore
    try:
        if args.command == "seed":
            load_registry(args.people)            # fail before reading stores
            patches = {}
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
            reg, changed = update_registry(args.people, patches)
            sys.stderr.write("people registry {}: {} person(s) from {} store(s), "
                             "{}\n".format(args.people, len(patches),
                                           len(args.stores),
                                           "updated" if changed else "unchanged"))
            summary = summarize(reg, args.repo[0] if len(args.repo) == 1 else None)
        else:
            summary = summarize(load_registry(args.people),
                                args.repo[0] if args.repo else None)
    except RegistryError as exc:
        raise SystemExit("error: {}".format(exc)) from None
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n"
                     if args.json else _summary_text(summary))
    return 0


if __name__ == "__main__":
    main()
