"""Load + validate a Phase 9 multi-repo project manifest.

A manifest declares a LOGICAL project name, one gather window, and an explicit
set of member repos (each `owner/repo`, with an optional Terraform registry path
used for cross-repo dependency resolution in a later slice). JSON, stdlib-only —
consistent with the rest of the skill. This is the contract `gather --manifest`
folds against; how the manifest is authored (hand-written, or generated from the
AVM index) is out of scope.

The field checks (`check_owner`, `check_repo`, `check_date`, `check_window`,
`check_project`) are shared: `gather.py` applies them to its single-repo
`--owner/--repo/--from/--to`, `view.py` to its window, and
`manifest_from_index.py` to what it emits, so every entry point accepts the same
names and dates. This module imports only the standard library, so any script
can import it without a cycle.
"""
import datetime
import json
import re

# GitHub user/org names: alphanumerics and single hyphens, not starting with a
# hyphen, at most 39 characters.
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_OWNER_MAX = 39
# GitHub repo names: alphanumerics, '-', '_', '.'; '.' and '..' are reserved.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REPO_MAX = 100
_REPO_RESERVED = (".", "..")
# The documented window form only. `date.fromisoformat` alone is not enough: on
# Python 3.11+ it also accepts '20260301' and other ISO 8601 variants.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_str(value, what):
    if not isinstance(value, str):
        raise ValueError("{} must be a string, got {} {!r}".format(
            what, type(value).__name__, value))
    if not value:
        raise ValueError("{} must not be empty".format(what))
    return value


def check_owner(value, what="'owner'"):
    """Return `value` if it is a valid GitHub owner (user/org) name, else raise
    ValueError naming `what`."""
    _require_str(value, what)
    if "/" in value:
        raise ValueError("{} must not contain '/', got {!r}".format(what, value))
    if (not _OWNER_RE.match(value) or "--" in value
            or len(value) > _OWNER_MAX):
        raise ValueError(
            "{} must be a GitHub owner name (letters, digits and single "
            "hyphens, not starting with '-', at most {} characters), got "
            "{!r}".format(what, _OWNER_MAX, value))
    return value


def check_repo(value, what="'repo'"):
    """Return `value` if it is a valid GitHub repository name, else raise
    ValueError naming `what`. The name goes unescaped into API URLs, clone URLs
    and clone directories, so anything outside GitHub's own alphabet is refused."""
    _require_str(value, what)
    if "/" in value:
        raise ValueError("{} must not contain '/', got {!r}".format(what, value))
    if value in _REPO_RESERVED:
        raise ValueError("{} {!r} is a reserved name".format(what, value))
    if not _REPO_RE.match(value) or len(value) > _REPO_MAX:
        raise ValueError(
            "{} must be a GitHub repository name (letters, digits, '-', '_', "
            "'.', at most {} characters), got {!r}".format(what, _REPO_MAX, value))
    return value


def check_date(value, what):
    """Return `value` if it is a real calendar date written YYYY-MM-DD, else
    raise ValueError naming `what`."""
    _require_str(value, what)
    try:
        if not _DATE_RE.match(value):
            raise ValueError
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("{} must be a date YYYY-MM-DD, got {!r}".format(
            what, value)) from None
    return value


def check_window(date_from, date_to, what_from="window.from",
                 what_to="window.to"):
    """Validate both window bounds and that from <= to. Returns (from, to)."""
    check_date(date_from, what_from)
    check_date(date_to, what_to)
    if date_from > date_to:     # same fixed-width form, so string order is date order
        raise ValueError("{} {} is after {} {}".format(
            what_from, date_from, what_to, date_to))
    return date_from, date_to


def check_project(value, what="'project'"):
    """Return `value` if usable as a logical project name. It is the scope prefix
    of every node id, so '/' (scope separator) and '#' (scope/local separator,
    STORE.md) are refused, as is surrounding whitespace."""
    _require_str(value, what)
    for ch in ("/", "#"):
        if ch in value:
            raise ValueError("{} must not contain {!r}, got {!r}".format(
                what, ch, value))
    if value != value.strip():
        raise ValueError("{} must not have leading or trailing whitespace, got "
                         "{!r}".format(what, value))
    return value


def load_manifest(path):
    """Read + validate a manifest file. Returns a normalized dict:
    {"project": str, "from": str, "to": str,
     "repos": [{"owner": str, "repo": str, "registry": str|None}, ...]}.
    Raises ValueError (message prefixed 'manifest: ') on any missing, mistyped or
    malformed field, an inverted window, or a duplicate member / registry."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("manifest: top-level value must be a JSON object")

    project = raw.get("project")
    if project is None or project == "":
        raise ValueError("manifest: 'project' is required")
    try:
        check_project(project)
    except ValueError as exc:
        raise ValueError("manifest: {}".format(exc)) from None

    window = raw.get("window")
    if not isinstance(window, dict):
        window = {}
    date_from, date_to = window.get("from"), window.get("to")
    if date_from in (None, "") or date_to in (None, ""):
        raise ValueError("manifest: 'window.from' and 'window.to' are required")
    try:
        check_window(date_from, date_to)
    except ValueError as exc:
        raise ValueError("manifest: {}".format(exc)) from None

    raw_repos = raw.get("repos") or []
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ValueError("manifest: 'repos' must list at least one member")

    repos = []
    seen_slugs, seen_registries = {}, {}
    for i, r in enumerate(raw_repos):
        if not isinstance(r, dict):
            raise ValueError("manifest: each repo must be an object with "
                             "'owner' and 'repo'")
        owner, repo = r.get("owner"), r.get("repo")
        if owner in (None, "") or repo in (None, ""):
            raise ValueError("manifest: each repo needs 'owner' and 'repo'")
        # owner/repo flow into '{project}/{owner}/{repo}#...' node ids (split back
        # on '/'), GitHub API/clone URLs and clone directories: GitHub's own name
        # rules keep all three well-formed.
        try:
            check_owner(owner)
            check_repo(repo)
        except ValueError as exc:
            raise ValueError("manifest: repos[{}]: {}".format(i, exc)) from None
        slug = "{}/{}".format(owner, repo)
        # GitHub names are case-insensitive: 'O/A' is the same repo as 'o/a' and
        # would get the same clone dir and be fetched twice.
        prev = seen_slugs.get(slug.lower())
        if prev is not None:
            raise ValueError("manifest: repos[{}]: duplicate member {!r} (already "
                             "listed as {!r}; names compare case-insensitively)"
                             .format(i, slug, prev))
        seen_slugs[slug.lower()] = slug
        # registry is optional (Terraform registry path), but if present it must
        # be a non-empty string, unique across members: dependency resolution
        # maps a registry source to exactly one member.
        registry = r.get("registry")
        if registry is not None:
            if not isinstance(registry, str) or not registry.strip():
                raise ValueError("manifest: repos[{}]: 'registry' must be a "
                                 "non-empty string when present".format(i))
            prev = seen_registries.get(registry.lower())
            if prev is not None:
                raise ValueError(
                    "manifest: repos[{}]: duplicate registry {!r} on {!r} "
                    "(already declared by {!r})".format(i, registry, slug, prev))
            seen_registries[registry.lower()] = slug
        repos.append({"owner": owner, "repo": repo, "registry": registry})

    return {"project": project, "from": date_from, "to": date_to, "repos": repos}


def member_slugs(manifest):
    """The set of 'owner/repo' slugs for a (loaded) manifest dict."""
    return {"{}/{}".format(r["owner"], r["repo"]) for r in manifest.get("repos", [])}
