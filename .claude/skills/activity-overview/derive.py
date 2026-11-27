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


def commit_times(bundle):
    """{sha: committed_at} for the bundle's commits that carry the full UTC
    committer timestamp (gather records it; older stores do not). Pure."""
    return {c.get("sha"): c.get("committed_at")
            for c in bundle.get("commits") or []
            if isinstance(c, dict) and c.get("sha") and c.get("committed_at")}


def chronological(events, times=None):
    """Events in lifecycle order (#76). The key is the commit's UTC committer
    timestamp when `times` ({sha: committed_at}, see commit_times) knows it,
    else the event's `date`. `date` is the committer's LOCAL calendar day, so on
    its own it can put a later commit first when two committers sit in different
    timezones around midnight; the UTC timestamp cannot. Ties (the events of one
    commit, or of one day when no timestamp is known) keep their given order
    (git walk / ledger order). A store's ledger holds its windows in FOLD order,
    so without this a window folded late would supply the "last" event. Stable
    sort; returns a new list."""
    times = times or {}
    return sorted(events,
                  key=lambda e: times.get(e.get("commit")) or e.get("date") or "")


def _code_event_key(ev):
    return ("file", ev.get("path"), ev.get("commit"), ev.get("change"))


def _symbol_event_key(ev):
    return ("symbol", ev.get("path"), ev.get("lang"), ev.get("subkind"),
            ev.get("name") or "", ev.get("commit"),
            _SYMBOL_CHANGE_TO_EVENT.get(ev.get("change"), "change"))


def merge_ledger_events(ledger, bundle):
    """`ledger` (a bundle-shaped dict whose code_events/symbol_events were read
    back from the store) plus the events of `bundle` that the store does not hold
    yet, deduplicated by the ledger's identity (artifact, commit, event) — first
    one wins, as the ledger's INSERT OR IGNORE does. Used by validate so an
    external bundle is checked against "the store after folding it". Pure."""
    out = dict(ledger)
    for key, ident in (("code_events", _code_event_key),
                       ("symbol_events", _symbol_event_key)):
        merged, seen = [], set()
        for ev in list(ledger.get(key) or []) + list(bundle.get(key) or []):
            if key == "code_events" and not ev.get("path"):
                continue                   # fold never writes a path-less event
            k = ident(ev)
            if k not in seen:
                seen.add(k)
                merged.append(ev)
        out[key] = merged
    return out


def artifact_projection(bundle, moves):
    """The artifact nodes a store holds (#76): build_artifacts over the repo's
    whole ledger (`bundle` carries its code_events/symbol_events), then the
    store's recorded symbol `moves` (apply_recorded_moves). fold_bundle writes
    it and validate's no_drift re-derives it, so writer and auditor share one
    rule. Pure."""
    return apply_recorded_moves(build_artifacts(bundle), moves)


def apply_recorded_moves(artifacts, moves):
    """Apply recorded symbol moves onto a cumulative artifact projection (#76).
    Mutates and returns `artifacts`.

    Moves are matched per fold window (match_symbol_moves; recorded as
    `replaced_by` edges), never over the whole ledger: across months a symbol's
    creation `add` and its later move `add` would make every move ambiguous.
    `moves` = [{"from", "to", "confidence", "basis", "commit"}] (local ids;
    `commit` is the source's drop commit in the window that matched the move,
    None on edges recorded before it was kept). A move whose endpoints are not
    both artifacts is ignored. For one window this equals link_symbol_identity.
    Across windows an artifact can gain several moves, and each move is judged
    by ITS OWN evidence, not by whatever happened to its endpoints later:
      - a source is `replaced` / `replaced_by` a move's dest only when that move
        IS the source's final removal: the source's last lifecycle event is a
        remove made by the move's `commit`. A source revived and later deleted
        without a new move is plain `removed`; re-adding an old destination
        does not bring an old move back;
      - a dest takes `identity_from` (and confidence/basis) from its most recent
        incoming move, ranked by the position of the move's drop in the
        source's lifecycle.
    A move without a recorded `commit` falls back to the source's latest remove.
    Ties break on the id, so the order of `moves` does not matter."""
    def drop_index(src, commit):
        lc = artifacts[src]["lifecycle"]
        idx = [i for i, e in enumerate(lc) if e["event"] == "remove"
               and (commit is None or e.get("commit") == commit)]
        return idx[-1] if idx else None

    incoming, outgoing = {}, {}
    for m in moves:
        src, dst = m["from"], m["to"]
        if src not in artifacts or dst not in artifacts:
            continue
        i = drop_index(src, m.get("commit"))
        if i is None:
            continue                      # the move's drop is not in the ledger
        when = artifacts[src]["lifecycle"][i].get("date") or ""
        rank = (when, src)
        if dst not in incoming or rank > incoming[dst][0]:
            incoming[dst] = (rank, m)
        if i == len(artifacts[src]["lifecycle"]) - 1:   # the FINAL removal
            rank = (dst,)
            if src not in outgoing or rank > outgoing[src][0]:
                outgoing[src] = (rank, m)
    for dst, (_rank, m) in incoming.items():
        artifacts[dst]["identity_from"] = m["from"]
        artifacts[dst]["move_confidence"] = m["confidence"]
        artifacts[dst]["move_basis"] = m["basis"]
    for src, (_rank, m) in outgoing.items():
        artifacts[src]["status"] = "replaced"
        artifacts[src]["replaced_by"] = m["to"]
        artifacts[src]["move_confidence"] = m["confidence"]
        artifacts[src]["move_basis"] = m["basis"]
    return artifacts


def build_artifacts(bundle):
    """Fold raw `code_events` into the per-artifact lifecycle ledger (file-level).

    Each tracked path (readme/doc/example) gets one entry with a lifecycle in
    date order (`chronological`; same-day events keep their given order).
    Status comes from the LAST lifecycle event: a rename away to a tracked path
    (that is still the last event) makes the old artifact `replaced`
    with `replaced_by` its direct successor; a later re-add makes it `live` again;
    a delete, or a rename to an untracked path, makes it `removed`. `code_area` is
    null in Phase 3a (graphify is Phase 3b). Pure.
    """
    artifacts = {}
    rename_succ = {}   # old aid -> (lifecycle index of its latest rename-away, new aid|None)

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

    times = commit_times(bundle)
    for ev in chronological(bundle.get("code_events", []), times):
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
                    # Remember WHICH lifecycle entry was the rename-away and its
                    # successor (None when the target is untracked). The final pass
                    # below only honours it if that entry is still the LAST event.
                    rename_succ[old_aid] = (len(artifacts[old_aid]["lifecycle"]) - 1, new_aid)
            continue
        aid = ensure(ev["path"])
        if aid is None:
            continue
        append_event(aid, _CHANGE_TO_EVENT.get(change, "change"), ev, hunk=ev.get("hunk"))

    # Phase 3d: fold symbol-granular events into kind:symbol/comment artifacts.
    # Each carries a bounded before/after on its lifecycle entry (file-level entries
    # leave those absent). id = "<path>#<lang>:<subkind>:<name>" (stable per symbol).
    for ev in chronological(bundle.get("symbol_events", []), times):
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

    # Final status from the LAST lifecycle event (#64):
    #   last event add/change          -> `live`, replaced_by None (a re-add revives it)
    #   last event = rename to a TRACKED path -> `replaced`, replaced_by = that direct
    #                                     successor (consumers walk A->B->C themselves)
    #   any other last `remove` (delete, or rename to an untracked path) -> `removed`
    for aid, a in artifacts.items():
        lc = a["lifecycle"]
        last = lc[-1]["event"] if lc else None
        idx, succ = rename_succ.get(aid, (None, None))
        if last == "remove" and idx == len(lc) - 1 and succ is not None:
            a["status"], a["replaced_by"] = "replaced", succ
        else:
            a["status"] = "removed" if last == "remove" else "live"
            a["replaced_by"] = None
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


_ROUND_REVIEW_STATES = ("approved", "changes_requested", "commented", "dismissed")


def _counted_reviews(pr):
    """The PR's review submissions that take part in review rounds, oldest
    first: SUBMITTED (state approved/changes_requested/commented/dismissed AND a
    `submitted_at`; PENDING drafts never count) and NOT by the PR author (the
    author's own thread replies are recorded by GitHub as COMMENTED reviews).
    When the PR author is unknown nobody is excluded on that ground. Order:
    (submitted_at, integer id, input position) — deterministic."""
    pr_author = pr.get("author")
    kept = []
    for i, r in enumerate(pr.get("reviews") or []):
        state = (r.get("state") or "").lower()
        if state not in _ROUND_REVIEW_STATES or not r.get("submitted_at"):
            continue
        if pr_author and r.get("author") == pr_author:
            continue
        rid = r.get("id")
        rid = rid if isinstance(rid, int) and not isinstance(rid, bool) else -1
        kept.append(((r["submitted_at"], rid, i), state, r.get("author")))
    kept.sort(key=lambda t: t[0])
    return [(state, author) for _, state, author in kept]


def count_review_rounds(counted):
    """Number of review rounds in an ordered [(state, reviewer)] sequence.

    A *change-request block* is a maximal run of consecutive `changes_requested`
    submissions from distinct reviewers (a reviewer repeating a change request
    starts a new block — that is a re-review). Rounds = 0 when there is no
    counted review, else 1 + the number of change-request blocks followed by at
    least one later counted review (the re-review that opens the next round).
    A trailing block with nothing after it does not open a round."""
    if not counted:
        return 0
    rounds = 1
    block = None  # reviewers in the open change-request block, or None
    for state, reviewer in counted:
        if block is not None and (state != "changes_requested" or reviewer in block):
            rounds += 1          # a later review after a change-request block
            block = None
        if state == "changes_requested":
            if block is None:
                block = set()
            block.add(reviewer)
    return rounds


def annotate_review_rounds(bundle):
    """Set `review_rounds` on each PR with counted review submissions (Phase 10,
    defined precisely by issue #63 — see BUNDLE.md "Review decision & rounds").

    `review_rounds = {count, states}`: `states` is the state of every counted
    submission (`_counted_reviews`: submitted, not PENDING, not by the PR
    author), oldest first; `count` is `count_review_rounds` over them. PRs with
    no counted review are left untouched (no fabricated key). Pure (in place);
    returns bundle for convenience."""
    for pr in bundle.get("prs", []):
        counted = _counted_reviews(pr)
        if not counted:
            continue
        pr["review_rounds"] = {
            "count": count_review_rounds(counted),
            "states": [state for state, _ in counted],
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
# Name forms (#78): an UNRESOLVED commit author is keyed by its git `%an`, which for
# automation is often a display name rather than the App login. Compared lower-cased
# with whitespace collapsed.
_BOT_NAMES = {"github actions", "dependabot", "dependabot preview", "renovate",
              "renovate bot"}

# People identity (#78). A commit author gather could NOT resolve to a GitHub login
# keeps its git name as the person key; when that name could itself be a login
# (letters/digits/hyphens, optional `[bot]`) this suffix is appended, so an
# unresolved name can never equal — and be silently merged with — a real login.
# A name that cannot be a login (a space, a dot, ...) is already disjoint.
UNRESOLVED_NAME_SUFFIX = " (git)"
_LOGIN_SHAPED_RE = _re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?$")


def unresolved_person_key(name):
    """The person key for a commit author whose login is unknown: the git name,
    suffixed with UNRESOLVED_NAME_SUFFIX when it is login-shaped. Pure."""
    if name and _LOGIN_SHAPED_RE.match(name):
        return name + UNRESOLVED_NAME_SUFFIX
    return name
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
    """True when `login` is an automation account (tagged, never dropped). Also
    recognises the git-name forms an unresolved commit author is keyed by
    (`GitHub Actions`, `Dependabot`, `github-actions (git)`). Pure."""
    if not login:
        return False
    if login.endswith(UNRESOLVED_NAME_SUFFIX):
        login = login[:-len(UNRESOLVED_NAME_SUFFIX)]
    low = login.lower()
    return (low in _BOT_EXACT or " ".join(low.split()) in _BOT_NAMES
            or bool(_BOT_RE.match(login)))


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

    People identity (#78): a commit's `author` is already the GitHub login when
    gather resolved it. A commit gather could NOT resolve (`login_resolved` is
    False) is keyed by its git name (see `unresolved_person_key`, so the key never
    equals a login) and that person carries `login_resolved: False`; the key is
    omitted otherwise (resolved logins, and commits from bundles gathered before
    #78, which carry no flag).

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

    def add(login, unresolved=False):
        if not login:
            return
        if login not in out:
            rec = attributed.get(login) or {}
            out[login] = {
                "modules": sorted(rec.get("modules") or []),
                "areas": sorted(rec.get("areas") or []),
                "is_bot": is_bot_login(login),
            }
        if unresolved:
            out[login]["login_resolved"] = False

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
        add(c.get("author"), unresolved=c.get("login_resolved") is False)
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
    are excluded. -> [{lang, subkind, name, from_path, to_path, confidence, basis,
    commit}] (sorted); `commit` is the source's (last) drop commit in these events.
    Keyed by `lang` too, so a Bicep symbol can't link to a same-named Terraform one."""
    drops, adds, drop_commit = {}, {}, {}
    for e in symbol_events:
        if e.get("subkind") in _COMMENT_SUBKINDS:
            continue
        key = (e.get("lang"), e.get("subkind"), e.get("name"))
        bucket = drops if e.get("change") == "drop" else adds if e.get("change") == "add" else None
        if bucket is not None and e.get("path"):
            bucket.setdefault(key, set()).add(e["path"])
            if bucket is drops:
                drop_commit[(key, e["path"])] = e.get("commit")   # last drop wins
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
                      "basis": basis, "commit": drop_commit.get((key, a))})
    return sorted(moves, key=lambda m: (m["from_path"], str(m["subkind"]), str(m["name"])))


def link_symbol_identity(bundle):
    """Apply window-wide symbol moves onto the artifact ledger (Phase 3e). For each move
    the source symbol artifact is `status:"replaced"` + `replaced_by` the dest, the dest
    gets `identity_from`, and both carry `move_confidence`/`move_basis`. Records a
    `symbol_moves` summary on the bundle. Mutates; safe when there are no symbol_events.
    Consistent with build_artifacts' last-event rule (#64): a source symbol re-added in
    its own file is "added in >1 file" to match_symbol_moves, so it is never linked
    (and so never marked `replaced`); a linked source has no add after its drop."""
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
            linked.append({**{k: v for k, v in m.items() if k != "commit"},
                           "from": src, "to": dst})
    bundle["symbol_moves"] = {"links": linked, "by_confidence": summary}
    return bundle


# ---------------------------------------------------------------------------
# CODEOWNERS matching (one matcher for the write path — fold_bundle's `owns`
# edges — and the read path — annotate_people_profile's `stale_owned`).
# ---------------------------------------------------------------------------
#
# Semantics (GitHub CODEOWNERS = gitignore patterns, minus negation/char classes):
#   - a leading `/` anchors the pattern at the repo root; a `/` anywhere but the
#     end also anchors it; a pattern with no such slash (`*.tf`, `docs`, `apps/`)
#     matches at any depth;
#   - a trailing `/` matches a directory only (and everything under it);
#   - a pattern that matches a directory also matches everything under it —
#     EXCEPT an anchored pattern whose last segment is a lone `*` (`docs/*`, `/*`),
#     which GitHub documents as owning only the files directly in that directory;
#   - `*` / `?` match within one path segment, `**` across segments;
#   - patterns starting with `!` (negation) are unsupported -> never match;
#   - the LAST matching rule in file order wins (owners are never unioned across
#     rules); a matching rule with no owners un-assigns ownership.


# The reserved truncation key of a workflow_stats map (mirrors
# gather.WORKFLOW_STATS_TRUNCATED_KEY; this leaf module cannot import gather).
WORKFLOW_STATS_TRUNCATED_KEY = "_truncated"


def sum_workflow_stats(stats_list):
    """Sum several windows' `workflow_stats` maps (issue #94): per workflow
    name, every integer counter is added. Truncation markers merge: `fetched`
    is summed, `total_count` is summed (None when any is unknown), `ranges`
    are concatenated in input order, `cap` is the largest. Keys sorted. Pure."""
    out, truncs = {}, []
    for stats in stats_list:
        for name, rec in (stats or {}).items():
            if name == WORKFLOW_STATS_TRUNCATED_KEY:
                truncs.append(rec or {})
                continue
            if not isinstance(rec, dict):
                continue
            cur = out.setdefault(name, {})
            for k, v in rec.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    cur[k] = cur.get(k, 0) + v
                else:
                    cur.setdefault(k, v)
    result = {name: dict(sorted(out[name].items())) for name in out}
    if truncs:
        totals = [t.get("total_count") for t in truncs]
        caps = [t.get("cap") for t in truncs if isinstance(t.get("cap"), int)]
        result[WORKFLOW_STATS_TRUNCATED_KEY] = {
            "truncated": True,
            "cap": max(caps) if caps else None,
            "fetched": sum(t.get("fetched") or 0 for t in truncs),
            "total_count": (None if any(not isinstance(t, int) for t in totals)
                            else sum(totals)),
            "ranges": [r for t in truncs for r in (t.get("ranges") or [])]}
    return dict(sorted(result.items()))


def codeowners_rules(code_owners):
    """Normalize a bundle `code_owners` value to an ordered list of
    `(pattern, [owner, ...])`.

    Accepts the ordered list form parse_codeowners emits
    (`[{"pattern": p, "owners": [...]}, ...]`), `[p, owners]` pairs, and the
    legacy `{pattern: owners}` dict (insertion order; a dict read back from an
    old store is key-SORTED, so its file order is already lost). Pure."""
    if not code_owners:
        return []
    if isinstance(code_owners, dict):
        return [(p, list(o or [])) for p, o in code_owners.items()]
    out = []
    for entry in code_owners:
        if isinstance(entry, dict):
            p, o = entry.get("pattern"), entry.get("owners")
        else:
            p, o = entry[0], entry[1]
        if p:
            out.append((p, list(o or [])))
    return out


def _codeowners_segment_re(seg):
    out = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "\\" and i + 1 < len(seg):
            out.append(_re.escape(seg[i + 1]))
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(_re.escape(ch))
        i += 1
    return "".join(out)


def _compile_codeowners_pattern(pattern):
    """Compile one CODEOWNERS pattern to `(regex, dir_only, files_only, head)`,
    or None for a pattern that can never match (negation, empty, bare `/`).
    `head` is the literal leading text of an anchored pattern (up to its first
    `*`/`?`/`\\`; "" when unanchored) — every candidate the regex can match is a
    prefix of the path, so a path not starting with `head` cannot match. A cheap
    pre-filter that never changes a result. Pure."""
    p = (pattern or "").strip()
    if not p or p.startswith("!"):
        return None
    dir_only = p.endswith("/")
    body = p.rstrip("/")
    anchored = body.startswith("/") or "/" in body.lstrip("/")
    body = body.lstrip("/")
    if not body:
        return None
    segs = body.split("/")
    parts = []
    for i, s in enumerate(segs):
        last = i == len(segs) - 1
        if s == "**":
            parts.append(".+" if last else "(?:[^/]+/)*")
        else:
            parts.append(_codeowners_segment_re(s) + ("" if last else "/"))
    rx = "".join(parts)
    if not anchored:
        rx = "(?:.*/)?" + rx
    files_only = anchored and segs[-1] == "*" and not dir_only
    head = _re.split(r"[*?\\]", body, 1)[0] if anchored else ""
    return _re.compile("^" + rx + "$"), dir_only, files_only, head


def compile_codeowners(code_owners):
    """Compile a `code_owners` value (any form codeowners_rules accepts) into the
    ordered rule list codeowners_owners/area_owners consume. Compile once, match
    many. Pure."""
    rules = []
    for pattern, owners in codeowners_rules(code_owners):
        rules.append((pattern, owners, _compile_codeowners_pattern(pattern)))
    return rules


def _codeowners_rule_matches(compiled, full, ancestors, is_dir):
    rx, dir_only, files_only, head = compiled
    if not full.startswith(head):
        return False
    if rx.match(full):
        if files_only:
            if not is_dir:
                return True
        elif is_dir or not dir_only:
            return True
    if files_only:
        return False
    # a pattern matching an ancestor directory owns everything under it
    return any(rx.match(d) for d in ancestors)


def codeowners_owners(rules, path, is_dir=False):
    """Owners of `path` under compiled `rules` (compile_codeowners): the owners of
    the LAST rule that matches, `[]` when none matches or the last match has no
    owners. `is_dir=True` treats `path` as a directory (a trailing `/` on `path`
    means the same). Pure."""
    if not path:
        return []
    is_dir = is_dir or path.endswith("/")
    parts = [s for s in path.split("/") if s]
    if not parts:
        return []
    full = "/".join(parts)
    ancestors = ["/".join(parts[:k]) for k in range(1, len(parts))]
    owners = []
    for _pattern, rule_owners, compiled in rules:
        if compiled and _codeowners_rule_matches(compiled, full, ancestors, is_dir):
            owners = rule_owners
    return list(owners)


# Area ids the directory provider gives the repo-root module (gather keeps the
# same two literals as _TF_ROOT_ID / _BICEP_ROOT_ID; derive is a leaf, so they are
# repeated here rather than imported).
ROOT_AREA_IDS = ("main.tf", "main.bicep")
_ROOT_PROBE = "__activity_overview_repo_root__"


def area_owners(rules, area_id, paths=()):
    """Owners of a code area. A directory area (the id is its directory path, e.g.
    `avm/res/network/virtual-network`) is owned by the owners of the last rule
    that matches the area directory itself (`<area>/`) — so a rule that only
    matches some files inside it (`*.tf`) does not make its owners own the area;
    `<area>/**` owns the area like `<area>/` (_directory_owners). A graphify
    `community:<n>` area has no directory: it is owned by the union of the
    owners of its `paths`, sorted. The repo-root module (`main.tf` /
    `main.bicep`) is owned by the last catch-all rule only. Pure."""
    if not area_id:
        return []
    if area_id.startswith("community:"):
        return sorted({o for p in paths or () for o in codeowners_owners(rules, p)})
    if area_id in ROOT_AREA_IDS:
        # The repo-root module's area id is file-shaped (`main.tf` / `main.bicep`),
        # but it stands for the ROOT DIRECTORY. Matching the id as a path would let
        # a file rule (`*.tf`, `*.bicep`) claim the whole root module. The root is
        # owned by the last catch-all rule — one that matches an arbitrary
        # top-level directory (`*`, `**`, `/**`) — and by nothing more specific.
        return codeowners_owners(rules, _ROOT_PROBE, is_dir=True)
    return _directory_owners(rules, area_id)


def _directory_owners(rules, directory):
    """Owners of a directory area: the owners of the LAST rule that matches
    the directory itself, where a rule written `<dir>/**` counts as matching
    `<dir>` (issue #110 item 9). GitHub gives `docs/**` and `docs/` the same
    files — everything under `docs` — so both make their owners own area
    `docs`; codeowners_owners matches `docs/**` only below `docs`, which is
    right for a file and too strict for the area. `docs/*` owns only the
    files directly in `docs` (not its subdirectories), so it owns no area."""
    parts = [s for s in directory.split("/") if s]
    if not parts:
        return []
    full = "/".join(parts)
    ancestors = ["/".join(parts[:k]) for k in range(1, len(parts))]
    child = full + "/" + _ROOT_PROBE
    owners = []
    for pattern, rule_owners, compiled in rules:
        if not compiled:
            continue
        if _codeowners_rule_matches(compiled, full, ancestors, True) or (
                (pattern or "").strip().rstrip("/").endswith("/**")
                and compiled[0].match(child)):
            owners = rule_owners
    return list(owners)


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


# The per-person profile counts annotate_people_profile sets. Each counts the
# person's records in ONE bundle (one repo), so a project total is their sum
# over the member bundles (digest._merge_people).
PROFILE_COUNT_FIELDS = ("prs_authored", "prs_merged", "prs_reviewed",
                        "commits_authored", "issues_opened",
                        "examples_authored", "docs_authored",
                        "symbols_authored", "authored_then_removed",
                        "stale_owned", "prs_opened", "own_pr_commits")


def _first_reviews(pr):
    """[(reviewer, first review date or None, latency days or None)] for each
    entry of `pr.reviewers`: the date of the reviewer's earliest submitted
    review on the PR, and its whole days after the PR was created (None when
    either date is missing or the review predates the PR). Pure."""
    created = _iso_date(pr.get("created_at"))
    reviews = pr.get("reviews") or []
    out = []
    for rv in pr.get("reviewers") or []:
        own = sorted((r for r in reviews
                      if r.get("author") == rv and r.get("submitted_at")),
                     key=lambda r: r["submitted_at"])
        sub = _iso_date(own[0].get("submitted_at")) if own else None
        latency = None
        if sub and created and (sub - created).days >= 0:
            latency = (sub - created).days
        out.append((rv, sub, latency))
    return out


def review_latency_samples(prs):
    """{login: [days, ...]}: every review-latency sample annotate_people_profile
    takes the median of, per reviewer, over `prs`. A project's median is taken
    over the samples of all its members (digest._merge_people). Pure."""
    out = {}
    for pr in prs or []:
        for rv, _sub, latency in _first_reviews(pr):
            if latency is not None:
                out.setdefault(rv, []).append(latency)
    return out


def median_latency(samples):
    """The `review_latency_days` value for `samples`: their median rounded to
    one decimal, or None when there are none. Pure."""
    return round(_stats.median(samples), 1) if samples else None


def annotate_people_profile(bundle):
    """Enrich each `bundle["people"][login]` in place with window-derived metrics.

    Keeps the existing `modules`/`areas`/`is_bot` keys and MERGES the profile
    fields (counts always present as ints; merge_rate/review_latency_days/
    first_seen/last_active present as null when N/A). Deterministic; reads only
    bundle facts (prs/commits/issues/artifacts/code_owners/trains). Pure (in
    place); returns bundle for convenience."""
    people = bundle.get("people") or {}
    if not people:
        return bundle
    prs = bundle.get("prs", [])
    commits = bundle.get("commits", [])
    issues = bundle.get("issues", [])
    artifacts = bundle.get("artifacts", {})
    code_owners = bundle.get("code_owners")
    trains = bundle.get("trains", [])

    # Single pass per collection (O(P+N), not O(P*N)): accumulate per-login counters
    # / dates / review-latencies keyed by login, then assign. Only logins already in
    # `people` (the participant enumeration) are tracked — every author/reviewer/
    # commenter/commit-author is in there, so nobody is missed.
    acc = {login: {"prs_authored": 0, "prs_merged": 0, "prs_reviewed": 0,
                   "commits_authored": 0, "issues_opened": 0,
                   "examples_authored": 0, "docs_authored": 0,
                   "symbols_authored": 0, "authored_then_removed": 0,
                   "own_pr_commits": 0, "prs_opened": 0,
                   "latencies": [], "dates": []}
           for login in people}
    # `prs_opened` (#97 final gate): the person's PRs created inside the
    # window, by calendar day. A view also holds PRs created earlier (still
    # open, or closed in the window), which `prs_authored` counts.
    window = (bundle.get("meta") or {}).get("period") or {}
    if not window:
        meta = bundle.get("meta") or {}
        window = {"from": meta.get("from"), "to": meta.get("to")}
    w_from = (window.get("from") or "")[:10] or None
    w_to = (window.get("to") or "")[:10] or None

    def opened_in_window(pr):
        day = (pr.get("created_at") or "")[:10]
        if not day or not (w_from or w_to):
            return None   # unknown: the window or the creation day is missing
        return (w_from is None or day >= w_from) and (w_to is None or day <= w_to)

    opened = [opened_in_window(pr) for pr in prs]
    # emitted only when it differs from `prs_authored` for someone: when the
    # view holds a PR created outside the window
    emit_opened = any(o is False for o in opened)

    def add_date(login, ts):
        if login in acc:
            d = _iso_date(ts)
            if d:
                acc[login]["dates"].append(d)

    for pr, was_opened in zip(prs, opened):
        author = pr.get("author")
        if author in acc:
            acc[author]["prs_authored"] += 1
            if was_opened:
                acc[author]["prs_opened"] += 1
            if pr.get("merged"):
                acc[author]["prs_merged"] += 1
            add_date(author, pr.get("created_at"))
        if pr.get("merged") and pr.get("merged_by") in acc:
            # the merge is the merger's activity (an author's own merge is counted
            # only when they merged it themselves, i.e. merged_by == author).
            add_date(pr.get("merged_by"), pr.get("merged_at"))
        for rv, sub, latency in _first_reviews(pr):
            if rv not in acc:
                continue
            acc[rv]["prs_reviewed"] += 1
            if sub:
                acc[rv]["dates"].append(sub)
            if latency is not None:
                acc[rv]["latencies"].append(latency)
        # comment activity (PR conversation + review comments) counts toward
        # first_seen/last_active so a commenter isn't under-reported.
        for c in (pr.get("comments_list") or []) + (pr.get("review_comments") or []):
            add_date(c.get("author"), c.get("created_at"))

    # the author of each merged PR, so a commit that lands the committer's own
    # merged PR (a squash / landing commit, `commit.pr`) is known (#97 final
    # gate: the hall score does not count that work a third time)
    merged_author = {pr.get("number"): pr.get("author") for pr in prs
                     if pr.get("merged") and pr.get("author")}
    for c in commits:
        au = c.get("author")
        if au in acc:
            acc[au]["commits_authored"] += 1
            add_date(au, c.get("date"))
            if c.get("pr") is not None and merged_author.get(c.get("pr")) == au:
                acc[au]["own_pr_commits"] += 1

    for iss in issues:
        au = iss.get("author")
        if au in acc:
            acc[au]["issues_opened"] += 1
            add_date(au, iss.get("created_at"))
        for c in iss.get("comments_list") or []:
            add_date(c.get("author"), c.get("created_at"))

    # artifacts: each distinct artifact with an "add" event by a login counts toward
    # that login's authored-by-kind tally (+ authored_then_removed when removed).
    for art in artifacts.values():
        kind = art.get("kind")
        removed = art.get("status") in _REMOVED_STATUSES
        adders = {ev.get("author") for ev in (art.get("lifecycle") or [])
                  if ev.get("event") == "add"}
        for login in adders:
            if login not in acc:
                continue
            if kind == "example":
                acc[login]["examples_authored"] += 1
            elif kind in ("doc", "readme"):
                acc[login]["docs_authored"] += 1
            elif kind == "symbol":
                acc[login]["symbols_authored"] += 1
            if removed:
                acc[login]["authored_then_removed"] += 1

    # stale_owned: distinct areas touched by a stalled train that the login owns
    # (area_owners: the last CODEOWNERS rule matching the area directory — the
    # same matcher fold_bundle uses for `owns` edges).
    stalled_areas = set()
    for t in trains:
        if (t.get("effort") or {}).get("stalled"):
            stalled_areas.update(a for a in (t.get("code_areas") or []) if a)
    owner_rules = compile_codeowners(code_owners)
    area_paths = {a.get("id"): a.get("paths") or []
                  for a in (bundle.get("code_graph") or {}).get("areas") or []}
    stalled_owners = {a: set(area_owners(owner_rules, a, area_paths.get(a)))
                      for a in stalled_areas} if owner_rules else {}

    def _stale_owned(login):
        return sum(1 for owners in stalled_owners.values() if login in owners)

    for login, profile in people.items():
        a = acc[login]
        prs_authored = a["prs_authored"]
        dates = a["dates"]
        profile.update({
            "prs_authored": prs_authored,
            "prs_merged": a["prs_merged"],
            "merge_rate": (round(a["prs_merged"] / prs_authored, 3)
                           if prs_authored else None),
            "prs_reviewed": a["prs_reviewed"],
            "commits_authored": a["commits_authored"],
            "issues_opened": a["issues_opened"],
            "review_latency_days": median_latency(a["latencies"]),
            "first_seen": min(dates).isoformat() if dates else None,
            "last_active": max(dates).isoformat() if dates else None,
            "examples_authored": a["examples_authored"],
            "docs_authored": a["docs_authored"],
            "symbols_authored": a["symbols_authored"],
            "authored_then_removed": a["authored_then_removed"],
            "stale_owned": _stale_owned(login),
        })
        # omit-when-not-applicable (older bundles and the golden views keep
        # their shape): see the notes where each is counted
        if emit_opened:
            profile["prs_opened"] = a["prs_opened"]
        if a["own_pr_commits"]:
            profile["own_pr_commits"] = a["own_pr_commits"]

    return bundle


def annotate_people_affiliation(bundle):
    """Set `affiliation` / `affiliation_signal` on each non-bot person from
    the view's `affiliations` fact (gather collect_affiliations), in place.
    Only when the bundle carries a collected fact: every other bundle (and
    every fixture view) is unchanged. A person the fact does not name is
    `unknown` with signal `not_checked` (over the lookup cap / after
    repeated failures) or `not_looked_up` (not a person of a gathered
    window: e.g. an unresolved commit author). An entry gathered with the
    people registry also gives the person's display `name` and the
    `maintainer` property (with `maintainer_signal`) for the view's
    repository; a `do_not_name` entry gives neither (the login only).
    Returns bundle."""
    fact = bundle.get("affiliations")
    if not isinstance(fact, dict) or not fact.get("collected"):
        return bundle
    entries = fact.get("people") or {}
    not_checked = set(fact.get("not_checked") or [])
    for login, profile in (bundle.get("people") or {}).items():
        if profile.get("is_bot"):
            continue
        # the labels are the fact's alone: drop any an earlier enrichment
        # left, so a do_not_name entry (or no entry) never keeps them
        for key in PERSON_LABEL_KEYS:
            profile.pop(key, None)
        e = entries.get(login)
        if isinstance(e, dict) and e.get("affiliation"):
            profile["affiliation"] = e["affiliation"]
            profile["affiliation_signal"] = e.get("signal")
            for key in PERSON_LABEL_KEYS:
                if key in e:
                    profile[key] = e[key]
        else:
            profile["affiliation"] = "unknown"
            profile["affiliation_signal"] = ("not_checked" if login in not_checked
                                             else "not_looked_up")
    return bundle


# How decisive each affiliation signal is, strongest first: when two member
# views of a project disagree about one person, the stronger signal wins
# (digest._merge_people).
AFFILIATION_SIGNAL_ORDER = ("override", "author_association",
                            "public_membership", "profile_company",
                            "commit_email_domain", "lookup_failed", "none",
                            "not_checked", "not_looked_up", "do_not_name")

# What the people registry adds to a person record (and to every row that
# names the person: halls, contributors, story participants): the display
# name and the maintainer property for the repository, with its signal.
PERSON_LABEL_KEYS = ("name", "maintainer", "maintainer_signal")


def person_labels(profile):
    """{name?, maintainer?, maintainer_signal?} of a person record: only the
    keys it carries (omit-when-absent). Pure."""
    return {k: profile[k] for k in PERSON_LABEL_KEYS if k in (profile or {})}


def affiliation_rank(profile):
    """Sort key of a person record's affiliation: lower is more decisive;
    the operator's override first (also an explicit `unknown`), then a
    known label before `unknown`. Pure."""
    sig = profile.get("affiliation_signal")
    order = AFFILIATION_SIGNAL_ORDER.index(sig) \
        if sig in AFFILIATION_SIGNAL_ORDER else len(AFFILIATION_SIGNAL_ORDER)
    return (sig != "override", profile.get("affiliation") == "unknown", order)


def hall_score(profile):
    """The `halls.fame` footprint of one person (build_halls). Pure."""
    return (profile.get("prs_merged", 0) * 2 + profile.get("prs_reviewed", 0)
            + profile.get("commits_authored", 0)
            - (profile.get("own_pr_commits") or 0))


def _hall_entries(people, keep=None):
    entries = []
    for login, profile in people.items():
        if profile.get("is_bot") or (keep is not None and not keep(profile)):
            continue
        score = hall_score(profile)
        if score <= 0:
            continue
        entry = {
            "login": login,
            "score": score,
            "prs_merged": profile.get("prs_merged", 0),
            "prs_reviewed": profile.get("prs_reviewed", 0),
            "commits_authored": profile.get("commits_authored", 0),
            "areas": profile.get("areas", []),
        }
        if profile.get("own_pr_commits"):
            entry["own_pr_commits"] = profile["own_pr_commits"]
        entry.update(person_labels(profile))
        entries.append(entry)
    entries.sort(key=lambda e: (-e["score"], e["login"]))
    return entries[:10]


def build_halls(bundle):
    """Build `bundle["halls"] = {"fame": [...]}` — recognition only.

    Run AFTER annotate_people_profile (reads its counts). For each NON-bot login,
    score = prs_merged*2 + prs_reviewed + (commits_authored - own_pr_commits);
    keep score>0, sort by (-score, login), take top 10. `own_pr_commits` (0
    when absent) are the person's commits that land a merged PR they authored
    (a squash / landing commit): that work already scores through
    `prs_merged`, so it is not counted again (#97 final gate) — only commits
    not attributable to their own merged PRs (direct pushes, commits on
    others' PRs) add to the score. A fame entry carries `own_pr_commits` when
    it is not 0. `halls.internal`/`shame`/`blame` are intentionally NOT built
    (recognition, not blame).

    When the people carry `affiliation` (annotate_people_affiliation), the
    same people are also ranked, the same way, within each known
    affiliation: `halls.community` and `halls.maintainer_org` (top 10
    each), plus `halls.unranked_unknown`, how many people with a positive
    score are `unknown` — never ranked as community. `fame` itself does
    not change. Pure (sets halls); returns bundle."""
    people = bundle.get("people") or {}
    halls = {"fame": _hall_entries(people)}
    if any("affiliation" in p for p in people.values()):
        for aff in ("community", "maintainer_org"):
            halls[aff] = _hall_entries(
                people, keep=lambda p, a=aff: p.get("affiliation") == a)
        halls["unranked_unknown"] = sum(
            1 for p in people.values() if not p.get("is_bot")
            and p.get("affiliation") == "unknown" and hall_score(p) > 0)
    bundle["halls"] = halls
    return bundle


# ---------------------------------------------------------------------------
# Phase 11 slice 2: flow pathology classifier + blockers in-degree
# ---------------------------------------------------------------------------

FLOW_STALE_DAYS = 30
FLOW_UPVOTE_MIN = 5
FLOW_TRACTION_MIN = 5
FLOW_HUNG_DAYS = 60


def _issue_states(bundle):
    """{issue number: state} for the bundle's issues. A number absent from this map
    is an issue that was not gathered: its state is UNKNOWN (see `build_flow`)."""
    return {iss.get("number"): iss.get("state")
            for iss in bundle.get("issues", []) if iss.get("number") is not None}


def build_flow(bundle):
    """Classify each OPEN issue into ONE flow pathology; emit only non-healthy.

    Read-side projection (sibling of forecast/modules). For each `state=="open"`
    issue, classify in precedence order (first match wins): `blocked` >
    `upvoted_but_ignored` > `traction_then_abandoned` > `hung` > healthy
    (healthy is NOT emitted, to stay lean). Staleness/age use `meta.ref_date`
    (fallback `meta.to`). Issues with an OPEN linked PR (via PR `closes`/
    `crossref_issues`) are exempt from the activity-starved pathologies
    (upvoted/traction/hung) — there IS work in flight. Deterministic; sorted by
    number. Pure (sets `bundle["flow"]`); returns bundle.

    Blocker state (#60): `issue["blocked_by"]` is the raw stored relation (extract
    does not filter it). Only blockers that are bundle issues with `state=="open"`
    count: they form the entry's `blocked_by` and drive `blocked`. A closed blocker
    no longer blocks, so the issue falls through to its own pathology. A blocker
    absent from the bundle has UNKNOWN state: it does not make the issue `blocked`
    (gather pulls every open issue of the repo, so an un-gathered blocker was not
    open when gathered), but it is listed in `blocked_by_unknown` and a signal.
    `hung` requires staleness as well as age, so a long-lived issue with recent
    activity is not `hung`."""
    meta = bundle.get("meta") or {}
    ref = _iso_date(meta.get("ref_date") or meta.get("to"))
    states = _issue_states(bundle)

    # open issues with at least one open (unmerged) linked PR -> work in flight.
    open_pr_issues = set()
    for pr in bundle.get("prs", []):
        if pr.get("state") == "open" and not pr.get("merged"):
            for num in (pr.get("closes") or []) + (pr.get("crossref_issues") or []):
                open_pr_issues.add(num)

    entries = []
    for issue in bundle.get("issues", []):
        if issue.get("state") != "open":
            continue
        has_open_pr = issue.get("number") in open_pr_issues
        created = _iso_date(issue.get("created_at"))
        age_days = (ref - created).days if (ref and created) else None
        updated = _iso_date(issue.get("updated_at"))
        stale_days = (ref - updated).days if (ref and updated) else None
        stale = stale_days is not None and stale_days >= FLOW_STALE_DAYS
        reactions = issue.get("reactions") or {}
        upvotes = reactions.get("+1", 0)
        reactions_total = reactions.get("total", 0)
        raw_blockers = sorted(set(issue.get("blocked_by") or []))
        blocked_by = [n for n in raw_blockers if states.get(n) == "open"]
        blocked_by_unknown = [n for n in raw_blockers if n not in states]
        comments = len(issue.get("comments_list") or [])

        if blocked_by:
            pathology = "blocked"
        elif upvotes >= FLOW_UPVOTE_MIN and stale and not has_open_pr:
            pathology = "upvoted_but_ignored"
        elif comments >= FLOW_TRACTION_MIN and stale and not has_open_pr:
            pathology = "traction_then_abandoned"
        elif (age_days is not None and age_days >= FLOW_HUNG_DAYS and stale
              and not has_open_pr):
            pathology = "hung"
        else:
            continue  # healthy -> not emitted

        signals = []
        if blocked_by:
            signals.append(f"blocked by {len(blocked_by)} issue(s)")
        if blocked_by_unknown:
            signals.append(f"{len(blocked_by_unknown)} blocker(s) of unknown state")
        if reactions_total > 0:
            signals.append(f"{reactions_total} reactions")
        if stale:
            signals.append(f"stale {stale_days}d")
        if comments > 0:
            signals.append(f"{comments} comments")
        if age_days is not None:
            signals.append(f"open {age_days}d")

        entries.append({
            "number": issue.get("number"),
            "url": issue.get("url"),
            "state": pathology,
            "age_days": age_days,
            "reactions": reactions_total,
            "blocked_by": blocked_by,
            "blocked_by_unknown": blocked_by_unknown,
            "signals": signals,
        })

    bundle["flow"] = sorted(entries, key=lambda e: e["number"])
    return bundle


def build_blockers(bundle):
    """Rank issues by how many other issues each one blocks (`blocks_count`).

    `issue["blocking"]` lists the issues this one blocks (the blocker→blocked
    direction set by `extract._attach_blocks`), so this ranks by that out-degree —
    the issues to unblock first. For each issue with a non-empty `blocking` list,
    emit `{number, url, blocks_count, blocks}` where `blocks` is the sorted unique
    set it blocks. Sorted by `blocks_count` desc then number. Empty when no
    blocking. Pure (sets `bundle["blockers"]`); returns bundle.

    Open only (#60): `issue["blocking"]` is the raw stored relation. A blocker is
    ranked only when it is itself `state=="open"`, and `blocks` keeps only the
    blocked issues that are bundle issues with `state=="open"` — closed dependants
    and dependants absent from the bundle (state unknown) are not counted. A blocker
    left with no open dependant is omitted."""
    states = _issue_states(bundle)
    entries = []
    for issue in bundle.get("issues", []):
        if issue.get("state") != "open":
            continue
        blocks = sorted(n for n in set(issue.get("blocking") or [])
                        if states.get(n) == "open")
        if not blocks:
            continue
        entries.append({
            "number": issue.get("number"),
            "url": issue.get("url"),
            "blocks_count": len(blocks),
            "blocks": blocks,
        })
    bundle["blockers"] = sorted(entries, key=lambda e: (-e["blocks_count"], e["number"]))
    return bundle
