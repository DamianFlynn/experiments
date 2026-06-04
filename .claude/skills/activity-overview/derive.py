"""Pure derivations from a raw bundle.

Leaf module: imports stdlib only — never `link` or `gather` — so the store
write-path can derive the same artifact/area/module/people/symbol facts the
link layer uses WITHOUT taking a dependency on `link`. `link.py` re-imports
these names so its public API (and `link.enrich`) is unchanged.

Everything here is a deterministic function of the bundle it's handed; no
network, no clock, no global state.
"""


def ref(type_, id_, url):
    """A provenance reference: every narrative-bearing fact resolves to one."""
    return {"type": type_, "id": id_, "url": url}


def artifact_id(path):
    """Stable artifact id from a path. Deterministic so the same file keeps the
    same id across periods (the spec's series-continuity rule)."""
    return "art:" + (path or "")


def classify_artifact_path(path):
    """Classify a changed file path into a tracked artifact kind, or None.

    File granularity only (Phase 3a). Precedence: readme > example > doc.
      - readme : basename matches README* (any/no extension)
      - example: under an `examples/` directory, or a `*.example*` filename
      - doc    : a `*.md` file, or any file under a `docs/` directory
      - else   : None (ignored at file granularity)
    Canonical single source: `gather` re-exports this (gather→derive is acyclic
    since derive is a leaf). `symbol`/`comment` artifacts come from the Phase 3d
    hunk walk (gather.parse_symbol_events), not this path classifier."""
    if not path:
        return None
    parts = path.split("/")
    base = parts[-1]
    low = base.lower()
    if base.upper().startswith("README"):
        return "readme"
    # `.example` only when it is a dot-segment (config.example.json, foo.example),
    # not an incidental substring (counter-example.md must stay a doc).
    if "examples" in parts[:-1] or ".example." in low or low.endswith(".example"):
        return "example"
    if low.endswith(".md") or "docs" in parts[:-1]:
        return "doc"
    return None


def _commit_url(bundle, sha):
    meta = bundle.get("meta", {})
    owner, repo = meta.get("owner"), meta.get("repo")
    if owner and repo:
        return f"https://github.com/{owner}/{repo}/commit/{sha}"
    return f"https://github.com/commit/{sha}"


# git change -> artifact lifecycle event. add/copy introduce; modify changes;
# delete removes; rename is handled specially (remove old + add new).
_CHANGE_TO_EVENT = {"add": "add", "copy": "add", "modify": "change", "delete": "remove"}
_SYMBOL_CHANGE_TO_EVENT = {"add": "add", "drop": "remove", "change": "change"}


def build_artifacts(bundle):
    """Fold raw `code_events` into the per-artifact lifecycle ledger (file-level).

    Each tracked path (readme/doc/example) gets one entry with an ordered
    lifecycle. Renames link the old artifact (status `replaced`, `replaced_by`)
    to the new one. `code_area` is null in Phase 3a (graphify is Phase 3b). Pure.
    """
    artifacts = {}

    def ensure(path):
        kind = classify_artifact_path(path)
        if kind is None:
            return None
        aid = artifact_id(path)
        if aid not in artifacts:
            artifacts[aid] = {
                "kind": kind, "path": path, "name": path.split("/")[-1],
                "status": "live", "replaced_by": None, "code_area": None,
                "lifecycle": [],
            }
        return aid

    def append_event(aid, event, ev):
        artifacts[aid]["lifecycle"].append({
            "event": event, "commit": ev["commit"], "author": ev["author"],
            "date": ev["date"],
            "ref": {"type": "commit", "id": ev["commit"],
                    "url": _commit_url(bundle, ev["commit"])},
        })

    for ev in bundle.get("code_events", []):
        change = ev["change"]
        if change in ("rename", "copy") and ev.get("old_path"):
            new_aid = ensure(ev["path"])
            if new_aid is not None:
                append_event(new_aid, "add", ev)
            if change == "rename":
                old_aid = ensure(ev["old_path"])
                if old_aid is not None:
                    append_event(old_aid, "remove", ev)
                    artifacts[old_aid]["status"] = "replaced"
                    # replaced_by is the direct successor; consumers walk the chain for terminal paths (A->B->C).
                    artifacts[old_aid]["replaced_by"] = new_aid
            continue
        aid = ensure(ev["path"])
        if aid is None:
            continue
        append_event(aid, _CHANGE_TO_EVENT.get(change, "change"), ev)

    # Phase 3d: fold symbol-granular events into kind:symbol/comment artifacts.
    # Each carries a bounded before/after on its lifecycle entry (file-level entries
    # leave those absent). id = "<path>#<lang>:<subkind>:<name>" (stable per symbol).
    for ev in bundle.get("symbol_events", []):
        kind = "comment" if ev["subkind"] in ("comment", "todo") else "symbol"
        aid = f'{ev["path"]}#{ev["lang"]}:{ev["subkind"]}:{ev["name"] or ""}'
        if aid not in artifacts:
            artifacts[aid] = {
                "kind": kind, "path": ev["path"], "name": ev["name"] or "comment",
                "subkind": ev["subkind"], "lang": ev["lang"], "status": "live",
                "replaced_by": None, "code_area": None, "lifecycle": [],
            }
        artifacts[aid]["lifecycle"].append({
            "event": _SYMBOL_CHANGE_TO_EVENT.get(ev["change"], "change"),
            "commit": ev["commit"], "author": ev["author"], "date": ev["date"],
            "before": ev["before"], "after": ev["after"],
            "ref": {"type": "commit", "id": ev["commit"],
                    "url": _commit_url(bundle, ev["commit"])},
        })

    # Final status from the last lifecycle event (unless already `replaced`).
    for a in artifacts.values():
        if a["status"] == "replaced":
            continue
        last = a["lifecycle"][-1]["event"] if a["lifecycle"] else None
        a["status"] = "removed" if last == "remove" else "live"
    return artifacts


def area_index(code_graph):
    """Build a path -> area id index from a code_graph's areas. Pure."""
    idx = {}
    for area in (code_graph or {}).get("areas", []):
        for path in area.get("paths", []):
            idx[path] = area["id"]
    return idx


def _area_for_path(path, idx):
    """Direct lookup; None when the path is not covered by any area (no guessing)."""
    return idx.get(path)


def attribute_code_areas(bundle):
    """Fill `code_area` on artifacts and `area` on feature_deltas from code_graph.

    Replaces the Phase 3a nulls where the path is covered by an area; leaves null
    otherwise (degrades cleanly on an empty/absent code_graph). Mutates in place,
    returns the index for reuse by trains/people attribution. Pure-ish (in-place)."""
    idx = area_index(bundle.get("code_graph", {}))
    for art in bundle.get("artifacts", {}).values():
        area = _area_for_path(art.get("path"), idx)
        if area is not None:
            art["code_area"] = area
    for delta in bundle.get("feature_deltas", []):
        art = bundle.get("artifacts", {}).get(delta.get("artifact"), {})
        area = art.get("code_area") or _area_for_path(delta.get("name"), idx)
        if area is not None:
            delta["area"] = area
    return idx


def _commit_areas(commit, idx):
    """Distinct area ids touched by a commit's files."""
    areas = set()
    for f in commit.get("files", []):
        area = idx.get(f)
        if area is not None:
            areas.add(area)
    return areas


def build_modules(bundle, idx):
    """Populate bundle['modules'] = {<area>: {commits, prs, files_changed}}.

    Counts per area: distinct commits, distinct PRs, and distinct files changed
    across the window's commits. Pure (in place)."""
    mods = {}
    for c in bundle.get("commits", []):
        pr = c.get("pr")
        for f in c.get("files", []):
            area = idx.get(f)
            if area is None:
                continue
            m = mods.setdefault(
                area, {"_commits": set(), "_prs": set(), "_files": set()})
            m["_commits"].add(c["sha"])
            if pr is not None:
                m["_prs"].add(pr)
            m["_files"].add(f)
    bundle["modules"] = {
        area: {"commits": len(m["_commits"]), "prs": len(m["_prs"]),
               "files_changed": len(m["_files"])}
        for area, m in mods.items()}
    return bundle


def attribute_people_areas(bundle, idx):
    """Give each authoring/reviewing person their modules + areas.

    A person's modules = the areas of files in commits they authored; areas mirror
    modules (the directory-provider id doubles as the area). Reviewers inherit the
    areas of the PRs they reviewed. Creates minimal people entries as needed. Pure
    (in place)."""
    people = bundle.setdefault("people", {})

    def touch(login, area):
        if not login or area is None:
            return
        p = people.setdefault(login, {"modules": [], "areas": []})
        if area not in p.setdefault("modules", []):
            p["modules"].append(area)
        if area not in p.setdefault("areas", []):
            p["areas"].append(area)

    by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    for c in bundle.get("commits", []):
        for area in _commit_areas(c, idx):
            touch(c.get("author"), area)
    # reviewers inherit their PR's commit areas via the trains map.
    pr_commits = {}
    for c in bundle.get("commits", []):
        if c.get("pr") is not None:
            pr_commits.setdefault(c["pr"], []).append(c["sha"])
    for pr in bundle.get("prs", []):
        areas = set()
        for sha in pr_commits.get(pr.get("number"), []):
            c = by_sha.get(sha)
            if c:
                areas |= _commit_areas(c, idx)
        for reviewer in pr.get("reviewers", []):
            for area in areas:
                touch(reviewer, area)
    # normalize lists deterministically
    for p in people.values():
        if "modules" in p:
            p["modules"] = sorted(p["modules"])
        if "areas" in p:
            p["areas"] = sorted(p["areas"])
    return bundle


# Bots are TAGGED (is_bot=True), never dropped — dropping = losing data. A login is
# a bot when it ends with the GitHub App suffix `[bot]` or matches one of the known
# automation accounts the AVM org runs. Patterns are matched case-insensitively.
import re as _re

_BOT_EXACT = {"github-actions", "microsoft-github-policy-service"}
_BOT_RE = _re.compile(
    r"""(?ix)
    ^ (?:
        .*\[bot\]            # any GitHub App login: foo[bot], copilot-swe-agent[bot]
      | .*-organizer         # *-organizer automation accounts
      | copilot-.*           # copilot-* family
    ) $
    """
)


def is_bot_login(login):
    """True when `login` is an automation account (tagged, never dropped). Pure."""
    if not login:
        return False
    low = login.lower()
    return low in _BOT_EXACT or bool(_BOT_RE.match(login))


def enumerate_participants(bundle, idx=None):
    """The FULL participant set — ONE source of truth for "who" (the anti-drift
    enumerator).

    Returns {login: {modules, areas, is_bot}} for EVERY login that carries any
    contribution edge on the write path: `pr.author`, `pr.merged_by`, each
    `pr.reviewers`, `issue.author`, commit `author`, and comment / review-comment
    authors (PR conversation + review comments, issue comments).

    Contributors (commit authors + reviewers of PRs with mapped commits) keep the
    `{modules, areas}` `attribute_people_areas` derives; pure participants get
    empty `modules`/`areas`. Every login is bot-tagged via `is_bot_login` — tagged,
    not dropped, so no "who" is ever lost.

    The CALLER must have run `attach_commit_prs` on the bundle's commits first (so
    commit->PR mapping is present, mirroring enrich / the carol fix); both
    `gather.fold_bundle` and `validate.no_drift` do exactly that on a copy. Pure.
    """
    if idx is None:
        idx = area_index(bundle.get("code_graph", {}) or {})
    # contributor modules/areas come from the existing attribution (a fresh copy of
    # the people map so we never mutate the caller's bundle).
    attributed = attribute_people_areas(
        {**bundle, "people": dict(bundle.get("people") or {})}, idx
    )["people"]

    out = {}

    def add(login):
        if not login:
            return
        if login not in out:
            rec = attributed.get(login) or {}
            out[login] = {
                "modules": sorted(rec.get("modules") or []),
                "areas": sorted(rec.get("areas") or []),
                "is_bot": is_bot_login(login),
            }

    for pr in bundle.get("prs", []):
        add(pr.get("author"))
        add(pr.get("merged_by"))
        for rv in pr.get("reviewers") or []:
            add(rv)
        for c in (pr.get("comments_list") or []) + (pr.get("review_comments") or []):
            add(c.get("author"))
    for iss in bundle.get("issues", []):
        add(iss.get("author"))
        for c in iss.get("comments_list") or []:
            add(c.get("author"))
    for c in bundle.get("commits", []):
        add(c.get("author"))
    return out


# Phase 3e: symbol-identity (window-wide moves). Comments are excluded — their text
# identity already captures evolution and would match noisily.
_COMMENT_SUBKINDS = {"comment", "todo"}


def match_symbol_moves(symbol_events, rename_pairs=()):
    """Detect window-wide symbol MOVES — the same symbol dropped in one file and added
    in another. Pure; precision over recall. Only UNIQUE `(lang, subkind, name)` pairings (one
    source file, one dest file, different) are linked; ambiguous names (boilerplate
    dropped/added in >1 file) are SKIPPED — the key false-positive guard. `confidence`
    is `high` when `(src, dst)` is also a git rename/copy pair, else `medium`. Comments
    are excluded. -> [{lang, subkind, name, from_path, to_path, confidence, basis}] (sorted).
    Keyed by `lang` too, so a Bicep symbol can't link to a same-named Terraform one."""
    drops, adds = {}, {}
    for e in symbol_events:
        if e.get("subkind") in _COMMENT_SUBKINDS:
            continue
        key = (e.get("lang"), e.get("subkind"), e.get("name"))
        bucket = drops if e.get("change") == "drop" else adds if e.get("change") == "add" else None
        if bucket is not None and e.get("path"):
            bucket.setdefault(key, set()).add(e["path"])
    renames = {(a, b) for a, b in rename_pairs}
    moves = []
    for key, src in drops.items():
        dst = adds.get(key)
        if not dst or len(src) != 1 or len(dst) != 1:
            continue                       # absent or ambiguous -> not a confident move
        a, b = next(iter(src)), next(iter(dst))
        if a == b:
            continue                       # re-added in the same file -> not a move
        lang, subkind, name = key
        basis = "file_rename" if (a, b) in renames else "unique_name"
        moves.append({"lang": lang, "subkind": subkind, "name": name, "from_path": a,
                      "to_path": b, "confidence": "high" if basis == "file_rename" else "medium",
                      "basis": basis})
    return sorted(moves, key=lambda m: (m["from_path"], str(m["subkind"]), str(m["name"])))


def link_symbol_identity(bundle):
    """Apply window-wide symbol moves onto the artifact ledger (Phase 3e). For each move
    the source symbol artifact is `status:"replaced"` + `replaced_by` the dest, the dest
    gets `identity_from`, and both carry `move_confidence`/`move_basis`. Records a
    `symbol_moves` summary on the bundle. Mutates; safe when there are no symbol_events."""
    arts = bundle.get("artifacts", {})
    renames = [(e.get("old_path"), e["path"]) for e in bundle.get("code_events", [])
               if e.get("change") in ("rename", "copy") and e.get("old_path")]
    moves = match_symbol_moves(bundle.get("symbol_events", []), renames)
    summary = {"high": 0, "medium": 0}
    linked = []
    for m in moves:
        lang = m["lang"] or ""             # both endpoints share lang (move key includes it)
        src = f'{m["from_path"]}#{lang}:{m["subkind"]}:{m["name"]}'
        dst = f'{m["to_path"]}#{lang}:{m["subkind"]}:{m["name"]}'
        if src in arts and dst in arts:
            arts[src]["status"] = "replaced"
            arts[src]["replaced_by"] = dst
            arts[dst]["identity_from"] = src
            for aid in (src, dst):
                arts[aid]["move_confidence"] = m["confidence"]
                arts[aid]["move_basis"] = m["basis"]
            summary[m["confidence"]] += 1
            linked.append({**m, "from": src, "to": dst})
    bundle["symbol_moves"] = {"links": linked, "by_confidence": summary}
    return bundle
