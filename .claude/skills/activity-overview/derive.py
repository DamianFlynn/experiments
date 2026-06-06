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

    def append_event(aid, event, ev, hunk=None):
        entry = {
            "event": event, "commit": ev["commit"], "author": ev["author"],
            "date": ev["date"],
            "ref": {"type": "commit", "id": ev["commit"],
                    "url": _commit_url(bundle, ev["commit"])},
        }
        # Phase 10 slice-diffs: carry the bounded file diff onto the file artifact's
        # lifecycle entry so it lives on the STORED artifact (durable in the graph).
        # The hunk is passed EXPLICITLY (not read off `ev`): a rename reuses one `ev`
        # for both sides and its diff is the NEW file's content, so it attaches to the
        # new-path `add` only — never the old-path `remove`. Omit-when-empty: no-patch
        # bundles carry no hunk, so the key is absent and the goldens stay identical.
        if hunk:
            entry["hunk"] = hunk
        artifacts[aid]["lifecycle"].append(entry)

    for ev in bundle.get("code_events", []):
        change = ev["change"]
        if change in ("rename", "copy") and ev.get("old_path"):
            new_aid = ensure(ev["path"])
            if new_aid is not None:
                append_event(new_aid, "add", ev, hunk=ev.get("hunk"))
            if change == "rename":
                old_aid = ensure(ev["old_path"])
                if old_aid is not None:
                    # No hunk on the old-path removal — `ev["hunk"]` is the NEW file's
                    # diff (the patch keys by the rename target), not the old file's.
                    append_event(old_aid, "remove", ev)
                    artifacts[old_aid]["status"] = "replaced"
                    # replaced_by is the direct successor; consumers walk the chain for terminal paths (A->B->C).
                    artifacts[old_aid]["replaced_by"] = new_aid
            continue
        aid = ensure(ev["path"])
        if aid is None:
            continue
        append_event(aid, _CHANGE_TO_EVENT.get(change, "change"), ev, hunk=ev.get("hunk"))

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


def annotate_review_rounds(bundle):
    """Set `review_rounds` on each PR that has review submissions (Phase 10).

    `review_rounds = {count, states}` where `states` is the per-submission state
    ordered by `submitted_at` (the approve->changes-requested->re-review
    texture). PRs with no `reviews` are left untouched (no fabricated key). Pure
    (in place); returns bundle for convenience."""
    for pr in bundle.get("prs", []):
        reviews = pr.get("reviews")
        if not reviews:
            continue
        ordered = sorted(reviews, key=lambda r: (r.get("submitted_at") or "",
                                                 r.get("id") or 0))
        pr["review_rounds"] = {
            "count": len(ordered),
            "states": [r.get("state") for r in ordered],
        }
    return bundle


def annotate_reopen_count(bundle):
    """Set `reopen_count` on each PR/issue with >=1 `reopened` lifecycle event
    (Phase 10). The key is omitted when the count is zero (no fabricated zero).
    Pure (in place); returns bundle for convenience."""
    for item in list(bundle.get("prs", [])) + list(bundle.get("issues", [])):
        reopens = sum(1 for ev in item.get("lifecycle") or []
                      if ev.get("event") == "reopened")
        if reopens:
            item["reopen_count"] = reopens
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


# ---------------------------------------------------------------------------
# Phase 11 slice 1: people profile + recognition halls
# ---------------------------------------------------------------------------

import datetime as _dt
import statistics as _stats

_REMOVED_STATUSES = {"removed", "dropped", "replaced"}


def _iso_date(ts):
    """Parse the leading "YYYY-MM-DD" of an ISO timestamp to a date, or None.
    Guards both unparseable strings and non-string inputs. Pure."""
    try:
        return _dt.date.fromisoformat((ts or "")[:10])
    except (ValueError, TypeError):
        return None


def annotate_people_profile(bundle):
    """Enrich each `bundle["people"][login]` in place with window-derived metrics.

    Keeps the existing `modules`/`areas`/`is_bot` keys and MERGES the profile
    fields (counts always present as ints; merge_rate/review_latency_days/
    first_seen/last_active present as null when N/A). Deterministic; reads only
    bundle facts (prs/commits/issues/artifacts/code_owners/trains). Pure (in
    place); returns bundle for convenience."""
    people = bundle.get("people") or {}
    prs = bundle.get("prs", [])
    commits = bundle.get("commits", [])
    issues = bundle.get("issues", [])
    artifacts = bundle.get("artifacts", {})
    code_owners = bundle.get("code_owners", {})
    trains = bundle.get("trains", [])

    # Pre-compute the set of area-prefixes that contain a stalled train: a prefix
    # "contains a stalled train" when any stalled train has a code_areas entry that
    # startswith the raw prefix string.
    stalled_areas = []
    for t in trains:
        if (t.get("effort") or {}).get("stalled"):
            stalled_areas.extend(t.get("code_areas") or [])

    def _prefix_has_stalled(prefix):
        return any(area.startswith(prefix) for area in stalled_areas)

    for login, profile in people.items():
        prs_authored = 0
        prs_merged = 0
        prs_reviewed = 0
        review_latencies = []
        dates = []

        for pr in prs:
            if pr.get("author") == login:
                prs_authored += 1
                d = _iso_date(pr.get("created_at"))
                if d:
                    dates.append(d)
                if pr.get("merged"):
                    prs_merged += 1
            if pr.get("merged_by") == login and pr.get("merged"):
                d = _iso_date(pr.get("merged_at"))
                if d:
                    dates.append(d)
            if login in (pr.get("reviewers") or []):
                prs_reviewed += 1
                # this login's earliest timestamped review on this PR
                own = [r for r in (pr.get("reviews") or [])
                       if r.get("author") == login and r.get("submitted_at")]
                own.sort(key=lambda r: r["submitted_at"])
                if own:
                    sub = _iso_date(own[0].get("submitted_at"))
                    created = _iso_date(pr.get("created_at"))
                    if sub:
                        dates.append(sub)
                    if sub and created:
                        latency = (sub - created).days
                        if latency >= 0:
                            review_latencies.append(latency)

        commits_authored = 0
        for c in commits:
            if c.get("author") == login:
                commits_authored += 1
                d = _iso_date(c.get("date"))
                if d:
                    dates.append(d)

        issues_opened = 0
        for iss in issues:
            if iss.get("author") == login:
                issues_opened += 1
                d = _iso_date(iss.get("created_at"))
                if d:
                    dates.append(d)

        # artifacts authored (distinct, by an "add" lifecycle event by this login),
        # bucketed by kind; plus authored-then-removed.
        examples_authored = 0
        docs_authored = 0
        symbols_authored = 0
        authored_then_removed = 0
        for art in artifacts.values():
            added = any(ev.get("author") == login and ev.get("event") == "add"
                        for ev in art.get("lifecycle") or [])
            if not added:
                continue
            kind = art.get("kind")
            if kind == "example":
                examples_authored += 1
            elif kind in ("doc", "readme"):
                docs_authored += 1
            elif kind == "symbol":
                symbols_authored += 1
            if art.get("status") in _REMOVED_STATUSES:
                authored_then_removed += 1

        # stale_owned: distinct owned prefixes (code_owners key whose owner list
        # includes login) that contain a stalled train.
        stale_owned = 0
        for prefix, owners in code_owners.items():
            if login in (owners or []) and _prefix_has_stalled(prefix):
                stale_owned += 1

        merge_rate = round(prs_merged / prs_authored, 3) if prs_authored else None
        review_latency_days = (round(_stats.median(review_latencies), 1)
                               if review_latencies else None)
        first_seen = min(dates).isoformat() if dates else None
        last_active = max(dates).isoformat() if dates else None

        profile.update({
            "prs_authored": prs_authored,
            "prs_merged": prs_merged,
            "merge_rate": merge_rate,
            "prs_reviewed": prs_reviewed,
            "commits_authored": commits_authored,
            "issues_opened": issues_opened,
            "review_latency_days": review_latency_days,
            "first_seen": first_seen,
            "last_active": last_active,
            "examples_authored": examples_authored,
            "docs_authored": docs_authored,
            "symbols_authored": symbols_authored,
            "authored_then_removed": authored_then_removed,
            "stale_owned": stale_owned,
        })

    return bundle


def build_halls(bundle):
    """Build `bundle["halls"] = {"fame": [...]}` — recognition only.

    Run AFTER annotate_people_profile (reads its counts). For each NON-bot login,
    score = prs_merged*2 + prs_reviewed + commits_authored; keep score>0, sort by
    (-score, login), take top 10. `halls.internal`/`shame`/`blame` are
    intentionally NOT built (recognition, not blame). Pure (sets halls); returns
    bundle."""
    people = bundle.get("people") or {}
    fame = []
    for login, profile in people.items():
        if profile.get("is_bot"):
            continue
        prs_merged = profile.get("prs_merged", 0)
        prs_reviewed = profile.get("prs_reviewed", 0)
        commits_authored = profile.get("commits_authored", 0)
        score = prs_merged * 2 + prs_reviewed + commits_authored
        if score <= 0:
            continue
        fame.append({
            "login": login,
            "score": score,
            "prs_merged": prs_merged,
            "prs_reviewed": prs_reviewed,
            "commits_authored": commits_authored,
            "areas": profile.get("areas", []),
        })
    fame.sort(key=lambda e: (-e["score"], e["login"]))
    bundle["halls"] = {"fame": fame[:10]}
    return bundle
