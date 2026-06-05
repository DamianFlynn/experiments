"""Generate a Phase 9 project manifest from the AVM module index.

The Azure Verified Modules project publishes its module list as CSV under
`docs/static/module-indexes/` in `Azure/Azure-Verified-Modules`
(`Terraform{Resource,Pattern,Utility}Modules.csv`). Each row carries a `RepoURL`
and a `PublicRegistryReference`; this tool turns a filtered slice of that index
into the `{project, window, repos:[{owner, repo, registry}]}` manifest that
`gather.py --manifest` folds — so a constellation isn't hand-authored.

The parse/build core is pure and stdlib-only (offline-testable). The CLI reads the
index from a file, stdin, or an http(s) URL (incl. the canonical AVM CSVs via
`--avm res|ptn|utl`) and writes the manifest JSON. The output is exactly the
contract `manifest.load_manifest` validates.
"""
import argparse
import csv
import io
import json
import sys
import urllib.request

# Canonical AVM-Terraform module-index CSVs (main branch). Bump deliberately.
_AVM_BASE = ("https://raw.githubusercontent.com/Azure/Azure-Verified-Modules/"
             "main/docs/static/module-indexes/")
AVM_INDEX_URLS = {
    "res": _AVM_BASE + "TerraformResourceModules.csv",
    "ptn": _AVM_BASE + "TerraformPatternModules.csv",
    "utl": _AVM_BASE + "TerraformUtilityModules.csv",
}
_KINDS = ("res", "ptn", "utl")


def _owner_repo_from_url(url):
    """('Azure', 'terraform-azurerm-avm-res-x') from a github RepoURL, or None."""
    if not url:
        return None
    s = url.strip().rstrip("/")
    i = s.find("github.com/")
    if i < 0:
        return None
    parts = [p for p in s[i + len("github.com/"):].split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _registry_from_reference(ref):
    """'Azure/avm-res-x/azurerm' (namespace/name/provider) from a
    PublicRegistryReference. Accepts the registry URL form
    (`https://registry.terraform.io/modules/<ns>/<name>/<provider>[/latest]`) or a
    bare `<ns>/<name>/<provider>` triple. Returns None when not resolvable."""
    if not ref:
        return None
    s = ref.strip()
    i = s.find("/modules/")
    if i >= 0:
        parts = [p for p in s[i + len("/modules/"):].split("/") if p]
    elif "://" not in s:
        parts = [p for p in s.split("/") if p]
    else:
        return None
    return "/".join(parts[:3]) if len(parts) >= 3 else None


def _kind_of(module_name):
    """'res'|'ptn'|'utl' from an `avm-<kind>-...` module name, else None."""
    for k in _KINDS:
        if (module_name or "").startswith("avm-{}-".format(k)):
            return k
    return None


def parse_index(text):
    """Parse an AVM module-index CSV (text) into normalized rows. Column access is
    case-insensitive and tolerant of the documented AVM headers; a row without a
    usable github `RepoURL` is dropped (can't name a member). Pure.

    Each row: {module, kind, status, owner, repo, registry, display, namespace,
    resource_type}."""
    rows = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for raw in reader:
        g = {(k or "").strip().lower(): (v or "").strip()
             for k, v in raw.items() if k}
        owner_repo = _owner_repo_from_url(g.get("repourl"))
        if not owner_repo:
            continue
        owner, repo = owner_repo
        module = g.get("modulename")
        rows.append({
            "module": module,
            "kind": _kind_of(module),
            "status": g.get("modulestatus"),
            "owner": owner,
            "repo": repo,
            "registry": _registry_from_reference(g.get("publicregistryreference")),
            "display": g.get("moduledisplayname"),
            "namespace": g.get("providernamespace"),
            "resource_type": g.get("resourcetype"),
        })
    return rows


def build_manifest(rows, project, frm, to, *, kinds=None, statuses=None,
                   name_contains=None, include=None, exclude=None, limit=None):
    """Filter index `rows` and assemble a manifest dict
    `{project, window:{from,to}, repos:[{owner,repo,registry}]}`. Filters (all
    optional, AND-combined across categories): `kinds` (res/ptn/utl), `statuses`
    (case-insensitive substring, any-match), `name_contains` (substring on the
    module name, any-match), `exclude` ('owner/repo' slugs to drop). `include`
    ('owner/repo' slugs) bypasses the kind/status/name filters for rows present in
    the index. Members are deduped by slug and sorted; `limit` caps the count after
    sorting. Deterministic and pure."""
    inc, exc = set(include or []), set(exclude or [])
    kset = set(kinds) if kinds else None
    sset = [s.lower() for s in statuses] if statuses else None
    nset = [n.lower() for n in name_contains] if name_contains else None
    seen, members = set(), []
    for r in rows:
        slug = "{}/{}".format(r["owner"], r["repo"])
        if slug in exc or slug in seen:
            continue
        if slug not in inc:
            if kset is not None and r["kind"] not in kset:
                continue
            if sset is not None and not any(s in (r["status"] or "").lower() for s in sset):
                continue
            if nset is not None and not any(n in (r["module"] or "").lower() for n in nset):
                continue
        seen.add(slug)
        members.append({"owner": r["owner"], "repo": r["repo"],
                        "registry": r["registry"]})
    members.sort(key=lambda m: (m["owner"], m["repo"]))
    if limit is not None:
        members = members[:limit]
    return {"project": project, "window": {"from": frm, "to": to}, "repos": members}


def _read_index(src, opener=urllib.request.urlopen):
    """Read one index source: an http(s) URL, '-' for stdin, or a file path."""
    if src == "-":
        return sys.stdin.read()
    if src.startswith(("http://", "https://")):
        with opener(src) as resp:                     # nosec - operator-supplied URL
            return resp.read().decode("utf-8-sig")
    with open(src, encoding="utf-8-sig") as fh:
        return fh.read()


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Generate a multi-repo project manifest from the AVM module index.")
    p.add_argument("--index", action="append", default=[], metavar="FILE|URL|-",
                   help="index CSV source; repeatable. '-' = stdin.")
    p.add_argument("--avm", action="append", default=[], choices=_KINDS,
                   help="fetch a canonical AVM index by kind (res/ptn/utl); repeatable.")
    p.add_argument("--project", required=True, help="logical project name (no '/').")
    p.add_argument("--from", dest="frm", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--kind", action="append", dest="kinds", choices=_KINDS,
                   help="keep only these module kinds; repeatable.")
    p.add_argument("--status", action="append", dest="statuses",
                   help="keep rows whose ModuleStatus contains this (e.g. Available); repeatable.")
    p.add_argument("--name-contains", action="append", dest="name_contains",
                   help="keep rows whose module name contains this; repeatable.")
    p.add_argument("--include", action="append", default=[],
                   help="'owner/repo' to keep regardless of other filters; repeatable.")
    p.add_argument("--exclude", action="append", default=[],
                   help="'owner/repo' to drop; repeatable.")
    p.add_argument("--limit", type=int, default=None, help="cap member count.")
    p.add_argument("--out", default=None, help="write manifest here (default: stdout).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # Enforce the manifest contract up front (mirrors manifest.load_manifest) so we
    # never emit a manifest that the loader would reject.
    if not args.project or "/" in args.project:
        sys.stderr.write("manifest-from-index: --project must be non-empty and "
                         "must not contain '/'\n")
        return 2
    sources = list(args.index) + [AVM_INDEX_URLS[k] for k in args.avm]
    if not sources:
        sys.stderr.write("manifest-from-index: need at least one --index or --avm\n")
        return 2
    rows = []
    for src in sources:
        rows.extend(parse_index(_read_index(src)))
    manifest = build_manifest(
        rows, args.project, args.frm, args.to,
        kinds=args.kinds, statuses=args.statuses, name_contains=args.name_contains,
        include=args.include, exclude=args.exclude, limit=args.limit)
    if not manifest["repos"]:
        sys.stderr.write("manifest-from-index: no members matched the filters "
                         "(check --kind/--status/--name-contains/--include)\n")
        return 1
    text = json.dumps(manifest, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        sys.stderr.write("wrote {} members to {}\n".format(
            len(manifest["repos"]), args.out))
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
