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

    project = raw.get("project")
    if not project:
        raise ValueError("manifest: 'project' is required")

    window = raw.get("window") or {}
    date_from, date_to = window.get("from"), window.get("to")
    if not date_from or not date_to:
        raise ValueError("manifest: 'window.from' and 'window.to' are required")

    raw_repos = raw.get("repos") or []
    if not raw_repos:
        raise ValueError("manifest: 'repos' must list at least one member")

    repos = []
    for r in raw_repos:
        owner, repo = r.get("owner"), r.get("repo")
        if not owner or not repo:
            raise ValueError("manifest: each repo needs 'owner' and 'repo'")
        repos.append({"owner": owner, "repo": repo, "registry": r.get("registry")})

    return {"project": project, "from": date_from, "to": date_to, "repos": repos}


def member_slugs(manifest):
    """The set of 'owner/repo' slugs for a (loaded) manifest dict."""
    return {"{}/{}".format(r["owner"], r["repo"]) for r in manifest.get("repos", [])}
