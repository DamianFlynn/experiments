"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import re
import sys

import gather  # for classify_artifact_path (shared artifact-kind gate)

_PR_RE = re.compile(r"Merge pull request #(\d+)|\(#(\d+)\)")


def resolve_commit_pr(message):
    """Best-effort PR number from a commit subject (merge or squash style)."""
    m = _PR_RE.search(message or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def attach_commit_prs(commits):
    """Set each commit's `pr` from its message in place."""
    for c in commits:
        c["pr"] = resolve_commit_pr(c.get("message", ""))
    return commits


def ref(type_, id_, url):
    """A provenance reference: every narrative-bearing fact resolves to one."""
    return {"type": type_, "id": id_, "url": url}


HIGH_PRIORITY_LABELS = {
    "priority/high", "priority/critical", "p0", "p1", "high-priority", "critical",
}


def _in_window(ts, period):
    """True if `ts` (ISO) falls in `period`. Permissive when either is missing,
    so dateless fixtures (and pre-window-free bundles) classify as in-window."""
    if not period or not ts:
        return True
    frm, to = period.get("from"), period.get("to")
    day = ts[:10]
    return (not frm or day >= frm) and (not to or day <= to)


def _high_priority(item):
    return any((lbl or "").lower() in HIGH_PRIORITY_LABELS
               for lbl in item.get("labels", []))


def _ms_sort_key(m):
    # `or 0` (not a default arg) guarantees an int secondary key even when
    # `number` is present-but-null; real GitHub milestone numbers are >= 1, so
    # collapsing a hypothetical 0 to 0 is harmless.
    return ((m.get("due_on") or "9999-12-31")[:10], m.get("number") or 0)


def select_milestones(milestones, ref_date):
    """(current, next) open milestones by due date. current = earliest open whose
    due date is on/after ref_date (else the earliest open); next = the one after."""
    open_ms = sorted((m for m in milestones if m.get("state") == "open"),
                     key=_ms_sort_key)
    if not open_ms:
        return None, None
    current = next(
        (m for m in open_ms if (m.get("due_on") or "9999-12-31")[:10] >= ref_date),
        open_ms[0])
    idx = open_ms.index(current)
    nxt = open_ms[idx + 1] if idx + 1 < len(open_ms) else None
    return current, nxt


def train_index(trains):
    """Map ('pr'|'issue', number) -> train id, for cross-linking bucket refs."""
    idx = {}
    for t in trains:
        if t.get("root_issue") is not None:
            idx[("issue", t["root_issue"])] = t["id"]
        for n in t.get("prs", []):
            idx[("pr", n)] = t["id"]
    return idx


def artifact_id(path):
    """Stable artifact id from a path. Deterministic so the same file keeps the
    same id across periods (the spec's series-continuity rule)."""
    return "art:" + (path or "")


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
        kind = gather.classify_artifact_path(path)
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


def build_timeline(bundle):
    """Merge social + code events into one chronological event stream.

    Event shape: {ts, actor, layer:'social'|'code', event, ref:{type,...,url},
    subject:{kind,name,path}}. Social events come from PR/issue comments + review
    comments; code events from artifact lifecycle entries. Sorted by ts.
    Malformed records that lack created_at fall back to URL ordering as a last
    resort (not a normal case — well-formed comment objects carry created_at). Pure.
    """
    events = []

    def social(actor, event, ref_type, number, url, subject, ts):
        events.append({
            "ts": ts or "", "actor": actor, "layer": "social", "event": event,
            # `id` (not `number`) to match the bundle-wide ref convention
            # {type, id, url} used everywhere else (and the gate's well_formed).
            "ref": {"type": ref_type, "id": number, "url": url},
            "subject": subject,
        })

    for pr in bundle.get("prs", []):
        url = pr.get("url")
        for c in pr.get("review_comments", []):
            curl = c.get("url") or url
            social(c.get("author"), "review_comment", "pr", pr["number"],
                   curl,
                   {"kind": "review_comment", "name": None, "path": None},
                   c.get("created_at") or curl)
        for c in pr.get("comments_list", []):
            curl = c.get("url") or url
            social(c.get("author"), "comment", "pr", pr["number"],
                   curl,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at") or curl)

    for issue in bundle.get("issues", []):
        url = issue.get("url")
        for c in issue.get("comments_list", []):
            curl = c.get("url") or url
            social(c.get("author"), "comment", "issue", issue["number"],
                   curl,
                   {"kind": "comment", "name": None, "path": None},
                   c.get("created_at") or curl)

    for art in bundle.get("artifacts", {}).values():
        for ev in art.get("lifecycle", []):
            events.append({
                "ts": ev.get("date") or "", "actor": ev.get("author"),
                "layer": "code", "event": ev["event"], "ref": ev["ref"],
                "subject": {"kind": art["kind"], "name": art["name"],
                            "path": art["path"]},
            })

    # Stable sort by ts (then by url so equal-ts events are deterministic).
    events.sort(key=lambda e: (e["ts"], str(e["ref"].get("url") or "")))
    return events


_EVENT_TO_DELTA = {"add": "add", "remove": "drop", "change": "change"}


def compute_feature_deltas(bundle):
    """Project the artifacts ledger into the feature_deltas view.

    One delta per lifecycle event: add->add, remove->drop, change->change. Each
    attributes author/commit/url + (best-effort) the owning pr/train via the
    commit->PR map Link already builds. For SYMBOL/COMMENT artifacts (Phase 3d)
    `before`/`after` carry the bounded hunk snippet and `detail` is
    "<lang> <subkind> <name>"; file-level deltas leave them null. `area` is filled
    later by attribute_code_areas. Pure.
    """
    commit_to_pr = {c["sha"]: c.get("pr") for c in bundle.get("commits", [])}
    pr_to_train = {}
    for t in bundle.get("trains", []):
        for n in t.get("prs", []):
            pr_to_train[n] = t["id"]

    deltas = []
    for aid, art in bundle.get("artifacts", {}).items():
        for ev in art.get("lifecycle", []):
            kind = _EVENT_TO_DELTA.get(ev["event"])
            if kind is None:
                continue
            pr = commit_to_pr.get(ev["commit"])
            is_symbol = art["kind"] in ("symbol", "comment")
            detail = (f'{art.get("lang", "")} {art.get("subkind", "")} '
                      f'{art["name"]}').strip() if is_symbol else None
            deltas.append({
                "area": None,
                "kind": kind,
                "subject": art["kind"],
                "name": art["name"],
                "before": ev.get("before"),
                "after": ev.get("after"),
                "detail": detail,
                "artifact": aid,
                "author": ev["author"],
                "train": pr_to_train.get(pr) if pr is not None else None,
                "pr": pr,
                "commit": ev["commit"],
                "url": ev["ref"]["url"],
            })
    return deltas


def build_trains(bundle):
    """Group merged PRs (+ their commits + closing issue) into decision trains.

    Train id is deterministic from its anchor: the root issue number when the PR
    closes one (`train-issue-<n>`), else the PR number (`train-pr-<n>`).
    """
    commits_by_pr = {}
    for c in bundle["commits"]:
        commits_by_pr.setdefault(c.get("pr"), []).append(c["sha"])
    issues_by_num = {i["number"]: i for i in bundle["issues"]}

    # Group merged PRs by anchor so multiple PRs on one issue share a train.
    groups = {}
    for pr in bundle["prs"]:
        if not pr.get("merged"):
            continue
        links = list(pr.get("closes") or [])
        for n in pr.get("crossref_issues") or []:
            if n not in links:
                links.append(n)
        root = links[0] if links else None
        anchor = ("issue", root) if root is not None else ("pr", pr["number"])
        groups.setdefault(anchor, []).append(pr)

    trains = []
    for (kind, key), prs in groups.items():
        prs = sorted(prs, key=lambda p: p["number"])
        pr_numbers = [p["number"] for p in prs]
        shas = []
        evidence = []
        for p in prs:
            shas.extend(commits_by_pr.get(p["number"], []))
            evidence.append(ref("pr", p["number"], p["url"]))
        root_issue = key if kind == "issue" else None
        train_kind = "other"
        if root_issue is not None and root_issue in issues_by_num:
            issue = issues_by_num[root_issue]
            train_kind = issue.get("kind", "other")
            evidence.insert(0, ref("issue", root_issue, issue["url"]))
        trains.append({
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": train_kind,
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": sorted(shas),
            "code_areas": [],
            "outcome": "shipped",
            "evidence": evidence,
        })
    return sorted(trains, key=lambda t: t["id"])


def compute_buckets(bundle):
    """Full four-way bucketing: one bucket per item, precedence
    shipped > rejected > next_candidates > in_flight. Refs carry their train id."""
    meta = bundle.get("meta", {})
    period = meta.get("period")
    ref_date = meta.get("ref_date") or meta.get("to") or ""
    current_ms, next_ms = select_milestones(bundle.get("milestones", []), ref_date)
    current_title = current_ms["title"] if current_ms else None
    next_title = next_ms["title"] if next_ms else None
    tindex = train_index(bundle.get("trains", []))

    out = {"shipped": [], "rejected": [], "next_candidates": [], "in_flight": []}

    def add(bucket, type_, num, url):
        r = ref(type_, num, url)
        tid = tindex.get((type_, num))
        if tid:
            r["train"] = tid
        out[bucket].append(r)

    def classify(item, type_):
        num, url = item["number"], item.get("url")
        state = item.get("state")
        if type_ == "pr" and item.get("merged") and _in_window(item.get("merged_at"), period):
            add("shipped", type_, num, url)
        elif type_ == "issue" and state == "closed" \
                and item.get("state_reason") == "completed" \
                and _in_window(item.get("closed_at"), period):
            add("shipped", type_, num, url)
        elif state == "closed" and type_ == "pr" and not item.get("merged") \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "closed" and type_ == "issue" \
                and item.get("state_reason") == "not_planned" \
                and _in_window(item.get("closed_at"), period):
            add("rejected", type_, num, url)
        elif state == "open":
            on_next = next_title is not None and item.get("milestone") == next_title
            on_current = current_title is not None and item.get("milestone") == current_title
            if on_next or _high_priority(item):
                add("next_candidates", type_, num, url)
            elif on_current or _in_window(item.get("updated_at"), period):
                add("in_flight", type_, num, url)
        # Anything else (stale open items off any milestone; closed items with an
        # unrecognised state_reason) is intentionally left in no bucket.

    for pr in bundle.get("prs", []):
        classify(pr, "pr")
    for issue in bundle.get("issues", []):
        classify(issue, "issue")
    return out


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


def attribute_train_areas(bundle, idx):
    """Set each train's `code_areas` from its commits' files. In place. Pure."""
    by_sha = {c["sha"]: c for c in bundle.get("commits", [])}
    for t in bundle.get("trains", []):
        areas = set()
        for sha in t.get("commits", []):
            c = by_sha.get(sha)
            if c:
                areas |= _commit_areas(c, idx)
        t["code_areas"] = sorted(areas)
    return bundle


# ---------------------------------------------------------------------------
# Phase 4a: train significance scoring + treatment tier
# ---------------------------------------------------------------------------

# Per-kind multiplier on the raw footprint.  feature/module-request represent
# the heaviest intentional work; bug is medium; idea captures light exploration;
# docs/chore/other are lightweight.  Tune these values without touching the
# scoring formula — that's why they're a named constant.
TRAIN_KIND_WEIGHTS = {
    "feature": 3.0,
    "module-request": 3.0,
    "bug": 2.0,
    "idea": 1.5,
    "docs": 1.0,
    "chore": 1.0,
    "other": 1.0,
}

# Top-N trains (by significance desc, then id) that always receive the "deep"
# treatment tier regardless of absolute score.
TRAIN_SIGNIFICANCE_TOP_N = 8

# Absolute floor: any train at or above this value is "deep" even when ranked
# outside the top-N.  Reasoning: a single multi-PR feature train with 3 PRs
# + 3 commits + 2 areas has footprint=8 and weight=3.0, so significance=8*3+2
# = 26.  Setting the floor at 20 ensures any such train always clears it while
# a minimal 1-PR docs train (footprint=2, weight=1.0, breadth=0 → sig=2.0)
# stays well below it.
TRAIN_SIGNIFICANCE_FLOOR = 20.0


def score_train_significance(bundle):
    """Annotate each train with `significance` (float) and `tier` ('deep'|'mention').

    significance = footprint * kind_weight + breadth, where:
      footprint = len(prs) + len(commits) + len(code_areas)  # note: code_areas also feeds the breadth term
      kind_weight from TRAIN_KIND_WEIGHTS (unknown kinds → 'other' weight)
      breadth = len(code_areas)
    tier = 'deep' for the top-TRAIN_SIGNIFICANCE_TOP_N trains OR any train
    whose significance >= TRAIN_SIGNIFICANCE_FLOOR; 'mention' otherwise.
    Outcome is intentionally ignored — a rejected train's story still matters.
    Mutates trains in place; returns bundle for convenience."""
    trains = bundle.get("trains", [])
    other_weight = TRAIN_KIND_WEIGHTS["other"]

    for t in trains:
        footprint = len(t.get("prs", [])) + len(t.get("commits", [])) + len(t.get("code_areas", []))
        kind_weight = TRAIN_KIND_WEIGHTS.get(t.get("kind", "other"), other_weight)
        breadth = len(t.get("code_areas", []))
        t["significance"] = float(footprint * kind_weight + breadth)

    # Stable sort: significance desc, then id asc for deterministic tiebreaking.
    ranked = sorted(trains, key=lambda t: (-t["significance"], t["id"]))
    top_ids = {t["id"] for t in ranked[:TRAIN_SIGNIFICANCE_TOP_N]}

    for t in trains:
        if t["id"] in top_ids or t["significance"] >= TRAIN_SIGNIFICANCE_FLOOR:
            t["tier"] = "deep"
        else:
            t["tier"] = "mention"

    return bundle


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


# Phase 3e: symbol-identity (window-wide moves). Comments are excluded — their text
# identity already captures evolution and would match noisily.
_COMMENT_SUBKINDS = {"comment", "todo"}


def match_symbol_moves(symbol_events, rename_pairs=()):
    """Detect window-wide symbol MOVES — the same symbol dropped in one file and added
    in another. Pure; precision over recall. Only UNIQUE `(subkind, name)` pairings (one
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


def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets,
    the Phase 3a narrative substrate (artifacts/timeline/feature_deltas), and the
    Phase 3b code-area attribution (code_area/area/modules/people). Label facets
    and `kind` are stamped in gather.py's acquire(), not here."""
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    bundle["artifacts"] = build_artifacts(bundle)
    link_symbol_identity(bundle)   # Phase 3e: window-wide symbol moves over the ledger
    # build_timeline depends on build_artifacts having run (reads bundle["artifacts"]).
    bundle["timeline"] = build_timeline(bundle)
    # compute_feature_deltas depends on build_trains having run (resolves trains).
    bundle["feature_deltas"] = compute_feature_deltas(bundle)
    # Phase 3b: attribute code areas everywhere the schema reserved a null.
    idx = attribute_code_areas(bundle)
    attribute_train_areas(bundle, idx)
    score_train_significance(bundle)   # Phase 4a: reads code_areas populated above
    build_modules(bundle, idx)
    attribute_people_areas(bundle, idx)
    return bundle


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write("usage: link.py BUNDLE.json\n")
        raise SystemExit(2)
    path = argv[0]
    with open(path) as fh:
        bundle = json.load(fh)
    enrich(bundle)
    with open(path, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(
        f"linked {len(bundle['trains'])} trains, "
        f"{len(bundle['buckets']['shipped'])} shipped into {path}\n"
    )
    return path


if __name__ == "__main__":
    main()
