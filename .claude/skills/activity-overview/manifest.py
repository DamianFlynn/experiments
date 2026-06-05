"""Load + validate a Phase 9 multi-repo project manifest.

A manifest declares a LOGICAL project name, one gather window, and an explicit
set of member repos (each `owner/repo`, with an optional Terraform registry path
used for cross-repo dependency resolution in a later slice). JSON, stdlib-only —
consistent with the rest of the skill. This is the contract `gather --manifest`
folds against; how the manifest is authored (hand-written, or generated from the
AVM index) is out of scope.
"""
import json


def load_manifest(path):
    """Read + validate a manifest file. Returns a normalized dict:
    {"project": str, "from": str, "to": str,
     "repos": [{"owner": str, "repo": str, "registry": str|None}, ...]}.
    Raises ValueError on any missing/empty required field."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("manifest: top-level value must be a JSON object")

    project = raw.get("project")
    if not project:
        raise ValueError("manifest: 'project' is required")
    if "/" in project:
        raise ValueError("manifest: 'project' must not contain '/'")

    window = raw.get("window")
    if not isinstance(window, dict):
        window = {}
    date_from, date_to = window.get("from"), window.get("to")
    if not date_from or not date_to:
        raise ValueError("manifest: 'window.from' and 'window.to' are required")

    raw_repos = raw.get("repos") or []
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ValueError("manifest: 'repos' must list at least one member")

    repos = []
    for r in raw_repos:
        if not isinstance(r, dict):
            raise ValueError("manifest: each repo must be an object with "
                             "'owner' and 'repo'")
        owner, repo = r.get("owner"), r.get("repo")
        if not owner or not repo:
            raise ValueError("manifest: each repo needs 'owner' and 'repo'")
        # owner/repo flow into '{project}/{owner}/{repo}#...' node ids, which the
        # backfill path splits back on '/'; a '/' inside either would desync that.
        if "/" in owner or "/" in repo:
            raise ValueError("manifest: 'owner' and 'repo' must not contain '/'")
        # registry is optional (Terraform registry path), but if present it must
        # be a string so downstream dep-resolution can rely on the type.
        registry = r.get("registry")
        if registry is not None and not isinstance(registry, str):
            raise ValueError("manifest: 'registry' must be a string when present")
        repos.append({"owner": owner, "repo": repo, "registry": registry})

    return {"project": project, "from": date_from, "to": date_to, "repos": repos}


def member_slugs(manifest):
    """The set of 'owner/repo' slugs for a (loaded) manifest dict."""
    return {"{}/{}".format(r["owner"], r["repo"]) for r in manifest.get("repos", [])}
