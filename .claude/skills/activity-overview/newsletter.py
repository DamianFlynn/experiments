#!/usr/bin/env python3
"""The newsletter layout: a release-notes style issue written from a linked
view (reference/newsletter.md).

Two read-only commands, neither of which writes the view:

  newsletter.py VIEW.json [VIEW.json ...] --outline [--weekly]
      The ordered, pre-selected material per section, as JSON on stdout:
      which modules lead, the new modules, breaking changes declared by the
      maintainers kept apart from the candidates the module layer detected,
      the stories to write in full and the ones to point at, the people rows
      with what each contributed, the open work that is ready, failing or
      waiting (the derived `ball` state, never a name), the plumbing share
      and the numbers with their coverage notes. Selection, ordering and
      classification only: no prose. Several views (the members of a
      multi-repo stream) are merged and every row names its `repo`.

  newsletter.py VIEW.json [VIEW.json ...] --check-newsletter ISSUE.md
          [--slices DIR] [--people PEOPLE.json] [--weekly]
      The mechanical check of a written issue against the same views (exit
      0 when it passes, 1 with the lists, 2 on a usage error). Each full
      story in the issue sits between `<!-- story: <train-id> -->` and
      `<!-- /story -->` and is checked with link.py's story check against
      `DIR/<train-id>.slice.json`.

Everything it reads is already in the view (module_changes, contributors,
affiliations, newcomers, people, people_context, stories, trains, prs,
issues, pr_activity through the train slices); keys a view lacks make the
matching section an omission with its reason. Stdlib only.
"""
import datetime
import json
import os
import re
import sys

import link

# Section ids in the order the issue presents them (reference/newsletter.md).
SECTION_ORDER = ("intro", "breaking", "new_modules", "module_changes", "stories",
                 "people", "coming", "plumbing", "numbers")

# The heading each section is written under; a heading starting with one of the
# aliases (case-insensitive) is that section. The intro has no heading: it is
# the text between the `# ` title and the first `## ` heading.
HEADINGS = {
    "breaking":       ("Breaking changes and deprecations", "Breaking changes",
                       "Deprecations"),
    "new_modules":    ("New modules",),
    "module_changes": ("What changed in existing modules", "Changes to existing modules",
                       "Noteworthy changes"),
    "stories":        ("Stories", "Behind the changes"),
    "people":         ("People", "Contributors"),
    "coming":         ("Coming up and stuck", "Coming up", "What is coming"),
    "plumbing":       ("Plumbing",),
    "numbers":        ("By the numbers", "Numbers"),
}

# Caps per cadence. A monthly issue is the full layout; a weekly one keeps
# every section that carries a user-facing change (breaking, new modules) at
# full size and shortens the rest.
CADENCE = {
    "month": {"modules": 12, "also": 20, "changelog_items": 4, "stories_full": 2,
              "story_pointers": 8, "newcomers": 20, "maintainers": 8, "top": 5,
              "ready": 10, "failing": 8, "first_review": 8, "awaiting_review": 5,
              "plumbing_prs": 8},
    "week":  {"modules": 6, "also": 10, "changelog_items": 2, "stories_full": 1,
              "story_pointers": 4, "newcomers": 10, "maintainers": 4, "top": 3,
              "ready": 5, "failing": 5, "first_review": 5, "awaiting_review": 3,
              "plumbing_prs": 4},
}

# What a person's work in the window is, best first: the module layer's PR
# headline, `docs` split out of plumbing, then open work, then an issue.
CONTRIBUTION_ORDER = ("new_module", "module_change", "release", "docs", "plumbing",
                      "open_pr", "issue")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# An open PR's files are not in the store, so a proposed module is known only
# from its title: a signal, never a fact (reference/newsletter.md).
_NEW_MODULE_TITLE_RE = re.compile(r"\bnew module\b|\binitial\b.{0,40}\bmodule\b", re.I)

_NO_MODULE_LAYER = ("the view has no module layer (module_notes were not gathered, "
                    "so module_changes is absent)")


# --- small helpers ------------------------------------------------------------

def _slug(view):
    """`owner/repo` of a view. A manifest member's view already carries the
    qualified `owner/repo` as `meta.repo` (its `meta.owner` is the project):
    keep it; prepend the owner only to a bare repository name."""
    m = view.get("meta") or {}
    repo = m.get("repo") or ""
    if "/" in repo:
        return repo
    return "{}/{}".format(m.get("owner"), repo)


def story_key(view, train_id, multi):
    """The identity of a story in an issue: the plain train id for a single
    repository; `owner/repo:train-id` in a multi-repo issue, where train ids
    (repository-local) can collide."""
    return "{}:{}".format(_slug(view), train_id) if multi else train_id


def slice_filename(key):
    """The slice file of a story key: `<train-id>.slice.json`, or
    `<owner>__<repo>__<train-id>.slice.json` for a qualified key."""
    return key.replace("/", "__").replace(":", "__") + ".slice.json"


def _period(view):
    m = view.get("meta") or {}
    p = m.get("period") or {}
    return p.get("from") or m.get("from"), p.get("to") or m.get("to")


def _in_period(ts, period):
    if not ts or not period[0] or not period[1]:
        return False
    return period[0] <= ts[:10] <= period[1]


def period_label(frm, to):
    """"October 2026" for a whole calendar month, else "1-7 October 2026" /
    "28 September 2026 - 4 October 2026"."""
    if not frm or not to:
        return None
    fy, fm, fd = (int(x) for x in frm.split("-"))
    ty, tm, td = (int(x) for x in to.split("-"))
    nxt = (ty + (tm == 12), tm % 12 + 1)
    last = (datetime.date(*nxt, 1) - datetime.timedelta(days=1)).day
    if (fy, fm) == (ty, tm) and fd == 1 and td == last:
        return "{} {}".format(_MONTHS[fm - 1], fy)
    if (fy, fm) == (ty, tm):
        return "{}-{} {} {}".format(fd, td, _MONTHS[fm - 1], fy)
    return "{} {} {} - {} {} {}".format(fd, _MONTHS[fm - 1], fy, td,
                                       _MONTHS[tm - 1], ty)


def _bare(v):
    return str(v).lstrip("vV") if v is not None else None


def _vkey(v):
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-]", _bare(v) or ""))


def _items_text(items):
    return [it.get("text") if isinstance(it, dict) else it for it in items or []]


def _person(view, login):
    """{login, name?, affiliation, signal?, strength?, maintainer?} from the
    view's affiliations fact (else its people rows); a do_not_name person is
    {login, do_not_name: True} and no name."""
    aff = ((view.get("affiliations") or {}).get("people") or {}).get(login) or {}
    row = (view.get("people") or {}).get(login) or {}
    if aff.get("signal") == "do_not_name":
        return {"login": login, "affiliation": "unknown", "do_not_name": True}
    out = {"login": login,
           "affiliation": aff.get("affiliation") or row.get("affiliation") or "unknown"}
    for k_out, k_in in (("name", "name"), ("signal", "signal"),
                        ("strength", "strength"), ("maintainer", "maintainer")):
        val = aff.get(k_in, row.get(k_in))
        if val is not None:
            out[k_out] = val
    return out


def _do_not_name(view):
    return {login for login, a in ((view.get("affiliations") or {})
                                   .get("people") or {}).items()
            if isinstance(a, dict) and a.get("signal") == "do_not_name"}


def _whole_days(ts, day):
    """Whole days from a timestamp's date to a YYYY-MM-DD day."""
    if not ts or not day:
        return None
    a = datetime.date.fromisoformat(ts[:10])
    return (datetime.date.fromisoformat(day[:10]) - a).days


def _with_repo(row, repo, multi):
    if multi:
        row = dict(row, repo=repo)
    return row


def _story_trains(view, multi=False):
    """The story keys of a view (story_key), selected then more."""
    st = view.get("stories") or {}
    return [story_key(view, s["id"], multi)
            for s in (st.get("selected") or []) + (st.get("more") or [])]


def _train_title(view, train):
    issues = {i["number"]: i for i in view.get("issues") or []}
    prs = {p["number"]: p for p in view.get("prs") or []}
    if train.get("root_issue") in issues:
        it = issues[train["root_issue"]]
        return it.get("title"), it.get("url")
    for n in train.get("prs") or []:
        if n in prs:
            return prs[n].get("title"), prs[n].get("url")
    for n in train.get("issues") or []:
        if n in issues:
            return issues[n].get("title"), issues[n].get("url")
    return None, None


def _module_story(view, numbers, multi=False):
    """The story (selected or more) holding one of these PR numbers, as its
    story key."""
    trains = {t["id"]: t for t in view.get("trains") or []}
    st = view.get("stories") or {}
    for s in (st.get("selected") or []) + (st.get("more") or []):
        if set(trains.get(s["id"], {}).get("prs") or []) & set(numbers):
            return story_key(view, s["id"], multi)
    return None


def _merged_in_window(view):
    """The numbers of the PRs merged into the mainline in the window: the
    module layer's list when the view has one, else by merged_at."""
    mc = view.get("module_changes")
    if isinstance(mc, dict) and mc.get("prs") is not None:
        return [r["number"] for r in mc["prs"]]
    period = _period(view)
    return [p["number"] for p in view.get("prs") or []
            if p.get("merged") and _in_period(p.get("merged_at"), period)]


# --- sections -----------------------------------------------------------------

def _own_published(m):
    return [p.get("version") for p in (m.get("versions") or {}).get("published") or []
            if not p.get("module") or p.get("module") == m.get("area")]


def section_breaking(view, multi=False):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"declared": [], "candidates": [], "deprecations": [], "notices": [],
                "omit": _NO_MODULE_LAYER}
    repo = _slug(view)
    declared, candidates, deps, notices = [], [], [], []
    for m in mc.get("modules") or []:
        brk = m.get("breaking") or {}
        published = {(p.get("module") or m["area"], _bare(p.get("version")))
                     for p in (m.get("versions") or {}).get("published") or []}
        texts = " ".join(d.get("text") or "" for d in brk.get("declared") or [])
        if brk.get("declared"):
            items = [{"version": d.get("version"), "text": d.get("text"),
                      "module": d.get("module"),
                      "published": (d.get("module") or m["area"],
                                    _bare(d.get("version"))) in published,
                      "url": ((d.get("sources") or [{}])[0]).get("url")}
                     for d in brk["declared"]]
            declared.append(_with_repo({
                "area": m["area"], "kind": m.get("kind"),
                "versions": sorted({i["version"] for i in items if i["version"]},
                                   key=_vkey),
                "items": items,
                "story": _module_story(view, [p.get("number") for p in m.get("prs") or []],
                                       multi),
            }, repo, multi))
        if brk.get("candidates"):
            candidates.append(_with_repo({
                "area": m["area"], "kind": m.get("kind"),
                "declared_too": bool(brk.get("declared")),
                "wording": "check before upgrading",
                "rules": [{"rule": c.get("rule"), "subject": c.get("subject"),
                           "covered_by_declared": bool(c.get("subject")) and
                           str(c.get("subject")).split(" ->")[0].split("/")[-1] in texts,
                           "url": ((c.get("sources") or [{}])[0]).get("url")}
                          for c in brk["candidates"]],
            }, repo, multi))
        seen = {}
        for d in m.get("deprecations") or []:
            key = d.get("target")
            row = seen.get(key)
            if row is None:
                row = seen[key] = _with_repo({
                    "area": m["area"], "target": key, "basis": [],
                    "version": d.get("version"), "text": d.get("text"),
                    "url": ((d.get("sources") or [{}])[0]).get("url")}, repo, multi)
                deps.append(row)
            row["basis"].append(d.get("basis"))
            row["version"] = row["version"] or d.get("version")
            row["text"] = row["text"] or d.get("text")
        if m.get("status") in ("removed", "orphaned"):
            notices.append(_with_repo({"area": m["area"], "status": m["status"]},
                                      repo, multi))
    declared.sort(key=lambda r: (-len(r["items"]), r["area"]))
    out = {"declared": declared, "candidates": candidates, "deprecations": deps,
           "notices": notices, "omit": None}
    if not (declared or candidates or deps or notices):
        out["omit"] = ("no declared breaking change, breaking candidate, deprecation, "
                       "removal or orphaned module in the window")
    return out


def _delivered_by(view, numbers):
    prs = {p["number"]: p for p in view.get("prs") or []}
    out, seen = [], set()
    for n in numbers:
        a = (prs.get(n) or {}).get("author")
        if a and a not in seen:
            seen.add(a)
            out.append(_person(view, a))
    return out


def _changelog_items(m, version=None, kinds_out=("breaking",), cap=None):
    """[{version, items}] of a module's changelog in the window, without the
    sections of `kinds_out` and placeholders; items verbatim."""
    rows = []
    for c in m.get("changelog") or []:
        if c.get("module") and c.get("module") != m.get("area"):
            continue
        if version is not None and c.get("version") != version:
            continue
        items = [t for s in c.get("sections") or [] if s.get("kind") not in kinds_out
                 for t in _items_text(s.get("items"))]
        if items:
            if cap is not None and len(items) > cap:
                rows.append({"version": c.get("version"), "items": items[:cap],
                             "items_omitted": len(items) - cap})
            else:
                rows.append({"version": c.get("version"), "items": items})
    return rows


def section_new_modules(view, caps, multi=False):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"modules": [], "child_modules": [], "omit": _NO_MODULE_LAYER}
    repo = _slug(view)
    by_area = {m["area"]: m for m in mc.get("modules") or []}
    summ = mc.get("summary") or {}
    rows = []
    for n in summ.get("new_modules") or []:
        m = by_area.get(n["area"], {})
        prs = n.get("introduced_by") or n.get("prs") or []
        items = [t for r in _changelog_items(m, kinds_out=()) for t in r["items"]]
        rows.append(_with_repo({
            "area": n["area"], "kind": n.get("kind"),
            "first_version": n.get("first_version"),
            "published": n.get("first_version") in _own_published(m),
            "changelog": items[:caps["changelog_items"] + 2],
            "prs": [{"number": p, "url": (next((x for x in view.get("prs") or []
                                                if x["number"] == p), {})).get("url")}
                    for p in prs],
            "delivered_by": _delivered_by(view, prs),
            "story": _module_story(view, prs, multi),
        }, repo, multi))
    child = [_with_repo(dict(c), repo, multi) for c in summ.get("new_child_modules") or []]
    out = {"modules": rows, "child_modules": child, "omit": None}
    if not rows and not child:
        out["omit"] = "no module is new in the window (module_changes.summary.new_modules is empty)"
    return out


def _version_row(m):
    v = m.get("versions") or {}
    vf = v.get("version_file") or {}
    cl = sorted(v.get("changelog") or [], key=_vkey)
    own = sorted(set(_own_published(m)), key=_vkey)
    row = {"from": vf.get("from"), "to": (cl[-1] if cl else None) or
           (own[-1] if own else None) or vf.get("to"),
           "changelog": cl, "published": own}
    child = sorted({(p.get("module"), p.get("version"))
                    for p in v.get("published") or []
                    if p.get("module") and p.get("module") != m.get("area")})
    if child:
        row["child_published"] = [{"module": a, "version": b} for a, b in child]
    for k in ("declared_not_published", "published_not_declared", "releases"):
        if v.get(k):
            row[k] = v[k]
    return row


def _interface_names(m):
    """{group: {change: [name]}} of a module's interface delta (at most 10
    names per change), for the writer to see what moved."""
    out = {}
    for group, changes in (m.get("interface") or {}).items():
        if not isinstance(changes, dict):
            continue
        for change, rows in changes.items():
            if not isinstance(rows, list) or not rows:
                continue
            names = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                after = r.get("after") if isinstance(r.get("after"), dict) else {}
                n = r.get("name") or after.get("name") or r.get("from_path")
                if n:
                    names.append(n)
            if names:
                out.setdefault(group, {})[change] = names[:10]
    return out


def section_module_changes(view, caps, multi=False):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"modules": [], "also": [], "plumbing_only": [], "omit": _NO_MODULE_LAYER}
    repo = _slug(view)
    rows, plumbing_only = [], []
    for m in mc.get("modules") or []:
        if m.get("status") == "new":
            continue
        if m.get("plumbing_only"):
            plumbing_only.append(m["area"])
            continue
        brk = m.get("breaking") or {}
        prs = [{"number": p.get("number"), "url": p.get("url"), "title": p.get("title"),
                "author": p.get("author")} for p in m.get("prs") or []]
        row = {"area": m["area"], "kind": m.get("kind"), "status": m.get("status"),
               "score": m.get("product_score") or 0, "version": _version_row(m),
               "changelog": _changelog_items(m, cap=caps["changelog_items"]),
               "prs": prs,
               "breaking": "declared" if brk.get("declared") else
               ("candidate" if brk.get("candidates") else None),
               "story": _module_story(view, [p["number"] for p in prs], multi)}
        names = _interface_names(m)
        if names:
            row["interface"] = names
        rows.append(_with_repo(row, repo, multi))
    rows.sort(key=lambda r: (-r["score"], r["area"]))
    return {"modules": rows, "also": [], "plumbing_only": sorted(plumbing_only),
            "omit": None if rows else "no module had a reader-facing change in the window"}


def _cut_modules(sec, caps):
    rows = sec["modules"]
    sec["modules"] = rows[:caps["modules"]]
    sec["also"] = [{k: r[k] for k in ("area", "score", "repo") if k in r}
                   for r in rows[caps["modules"]:]]
    return sec


def section_stories(views, caps, multi=False):
    rows = []
    for v in views:
        trains = {t["id"]: t for t in v.get("trains") or []}
        st = v.get("stories") or {}
        for s in (st.get("selected") or []):
            t = trains.get(s["id"], {})
            title, url = _train_title(v, t)
            key = story_key(v, s["id"], multi)
            sfile = slice_filename(key)
            view_file = "VIEW" if not multi else \
                "workspace/view/{}.json".format(_slug(v).replace("/", "__"))
            rows.append(_with_repo({
                "id": s["id"], "key": key, "rank": s.get("rank"), "score": s.get("score"),
                "outcome": t.get("outcome"), "title": title, "url": url,
                "prs": t.get("prs") or [], "issues": t.get("issues") or [],
                "modules": [a for a in t.get("code_areas") or []
                            if link.module_kind(a) is not None][:3],
                "slice": "python3 link.py {} --slice {} > {}".format(
                    view_file, s["id"], sfile),
                "check": "python3 link.py {} --check-story {}".format(
                    sfile, sfile.replace(".slice.json", ".story.md")),
            }, _slug(v), multi))
        for s in st.get("more") or []:
            t = trains.get(s["id"], {})
            title, url = _train_title(v, t)
            rows.append(_with_repo({"id": s["id"], "key": story_key(v, s["id"], multi),
                                    "rank": None, "score": s.get("score"),
                                    "outcome": t.get("outcome"), "title": title,
                                    "url": url, "more": True}, _slug(v), multi))
    selected = sorted([r for r in rows if not r.get("more")],
                      key=lambda r: (-(r["score"] or 0), r["rank"] or 0, r["key"]))
    more = sorted([r for r in rows if r.get("more")],
                  key=lambda r: (-(r["score"] or 0), r["key"]))
    n = caps["stories_full"]
    full = selected[:n]

    def cls(r):
        return "shipped" if r["outcome"] == "shipped" else "open"

    if len(full) >= 2 and len({cls(r) for r in full}) == 1:
        other = [r for r in selected[n:] if cls(r) != cls(full[0])]
        if other:
            full = full[:-1] + [other[0]]
    full_keys = {r["key"] for r in full}
    pointers = [r for r in selected if r["key"] not in full_keys] + more
    pointers = [{k: r[k] for k in ("id", "key", "title", "url", "outcome", "repo")
                 if k in r}
                for r in pointers]
    out = {"full": full, "pointers": pointers[:caps["story_pointers"]],
           "pointers_omitted": max(0, len(pointers) - caps["story_pointers"]),
           "omit": None}
    if not full:
        out["omit"] = "no train qualified as a story in the window (stories is absent)"
    return out


def contribution_class(row):
    """What a contributors row (newcomers / hall) contributed, best first
    (CONTRIBUTION_ORDER): from the module layer's PR headline of their merged
    PRs, `docs` for a docs-only plumbing PR; `open_pr` with only open PRs;
    `issue` with neither."""
    best = None
    for p in (row.get("contributed") or {}).get("prs") or []:
        if p.get("state") == "merged":
            h = p.get("headline")
            if h == "plumbing" and p.get("class") == "docs-only":
                h = "docs"
            if h not in CONTRIBUTION_ORDER:
                h = "plumbing" if h == "unknown" else h
            c = h if h in CONTRIBUTION_ORDER else None
        else:
            c = "open_pr"
        if c and (best is None or CONTRIBUTION_ORDER.index(c) < CONTRIBUTION_ORDER.index(best)):
            best = c
    return best or "issue"


def _merged_by_class(view, login):
    """{contribution class: count} of the PRs a person authored that were
    merged into the mainline in the window, by the module layer's PR row:
    new_module (it introduced a new module), release, docs (docs-only),
    plumbing, module_change (any other source change)."""
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {}
    prs = {p["number"]: p for p in view.get("prs") or []}
    intro = {n for m in (mc.get("summary") or {}).get("new_modules") or []
             for n in m.get("introduced_by") or []}
    out = {}
    for r in mc.get("prs") or []:
        if (prs.get(r["number"]) or {}).get("author") != login:
            continue
        if r["number"] in intro:
            k = "new_module"
        elif r.get("class") == "release":
            k = "release"
        elif r.get("plumbing"):
            k = "docs" if r.get("class") == "docs-only" else "plumbing"
        elif r.get("class") == "unknown":
            k = "unknown"
        else:
            k = "module_change"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _pr_brief(p):
    out = {"number": p.get("number"), "url": p.get("url"), "title": p.get("title")}
    mods = [m.get("area") for m in p.get("modules") or [] if m.get("area")]
    if mods:
        out["modules"] = mods
    if p.get("headline"):
        out["headline"] = p["headline"]
    if p.get("changelog"):
        out["changelog"] = p["changelog"]
    return out


def _registry(view, login):
    """Registry context of one person (people_context): where it reaches
    before the window (an `active_since` inside the window says nothing a
    newcomer row does not) and the other repositories."""
    pc = ((view.get("people_context") or {}).get("people") or {}).get(login)
    if not pc:
        return None
    here = pc.get("here") or {}
    out = {}
    if here.get("active_since") and not here.get("active_since_is_first_ever") \
            and here["active_since"] < (_period(view)[0] or ""):
        out["active_here_since"] = here["active_since"]
    for k in ("also_maintains", "also_contributes_to"):
        if pc.get(k):
            out[k] = pc[k]
    return out or None


def section_people(views, caps, multi=False):
    newcomers, maint, top, not_named = [], [], [], 0
    gaps = {}
    affs = []
    community = []
    for v in views:
        repo = _slug(v)
        c = v.get("contributors") or {}
        hidden = _do_not_name(v)
        for r in c.get("newcomers") or []:
            if r.get("login") in hidden or r.get("affiliation_signal") == "do_not_name":
                not_named += 1
                continue
            prs = (r.get("contributed") or {}).get("prs") or []
            row = {"login": r["login"], "class": r.get("class"),
                   "contribution": contribution_class(r),
                   "first": {"type": (r.get("first") or {}).get("first"),
                             "number": (r.get("first") or {}).get("number"),
                             "url": (r.get("first") or {}).get("url"),
                             "created_at": (r.get("first") or {}).get("created_at")},
                   "merged": [_pr_brief(p) for p in prs if p.get("state") == "merged"],
                   "open": [_pr_brief(p) for p in prs if p.get("state") != "merged"]}
            row.update({k: v2 for k, v2 in _person(v, r["login"]).items() if k != "login"})
            for k in ("gap_days", "gap_bucket"):
                if r.get(k) is not None:
                    row[k] = r[k]
            if r.get("class") == "returning" and r.get("gap_bucket"):
                gaps[r["gap_bucket"]] = gaps.get(r["gap_bucket"], 0) + 1
            reg = _registry(v, r["login"])
            if reg:
                row["registry"] = reg
            newcomers.append(_with_repo(row, repo, multi))
        aff = c.get("affiliation")
        if aff:
            affs.append(_with_repo({k: aff[k] for k in ("people", "merged_prs", "reviews")
                                    if k in aff}, repo, multi))
        if c.get("community"):
            community.append(_with_repo(dict(c["community"]), repo, multi))
        prs = {p["number"]: p for p in v.get("prs") or []}
        merged = _merged_in_window(v)
        logins = ((aff or {}).get("maintainers") or {}).get("logins") or []
        for login in logins:
            if login in hidden:
                continue
            others = sum(1 for n in merged if (prs.get(n) or {}).get("merged_by") == login
                         and prs[n].get("author") != login)
            own = sum(1 for n in merged if (prs.get(n) or {}).get("merged_by") == login
                      and prs[n].get("author") == login)
            row = {"login": login,
                   "reviews": ((v.get("people") or {}).get(login) or {}).get("prs_reviewed", 0),
                   "merged_others": others, "merged_own": own,
                   "authored_by_class": _merged_by_class(v, login)}
            row.update({k: v2 for k, v2 in _person(v, login).items() if k != "login"})
            reg = _registry(v, login)
            if reg:
                row["registry"] = reg
            maint.append(_with_repo(row, repo, multi))
        for h in c.get("hall") or []:
            if h.get("login") in hidden or ((v.get("people") or {}).get(h.get("login"))
                                            or {}).get("is_bot"):
                continue
            row = {"login": h["login"], "score": h.get("score"),
                   "merged_by_class": _merged_by_class(v, h["login"]),
                   "reviewed": (h.get("reviewed") or {}).get("prs", 0)}
            row.update({k: v2 for k, v2 in _person(v, h["login"]).items() if k != "login"})
            top.append(_with_repo(row, repo, multi))
    newcomers.sort(key=lambda r: (CONTRIBUTION_ORDER.index(r["contribution"]),
                                  r["login"].lower(), r.get("repo") or ""))
    maint.sort(key=lambda r: (-(r["reviews"] + r["merged_others"]), r["login"].lower()))
    top.sort(key=lambda r: (-(r["score"] or 0), r["login"].lower()))
    out = {"newcomers": newcomers[:caps["newcomers"]],
           "newcomers_omitted": max(0, len(newcomers) - caps["newcomers"]),
           "not_named": not_named, "returning_by_gap": gaps,
           "gap_months": [3, 6, 9],
           "affiliation": affs[0] if len(affs) == 1 else affs,
           "community": community[0] if len(community) == 1 else community,
           "maintainers": maint[:caps["maintainers"]], "top": top[:caps["top"]],
           "omit": None}
    if not newcomers and not affs and not top:
        out["omit"] = "the view has no people layer (contributors is absent)"
    return out


def section_coming(view, caps, multi=False):
    """Open PRs with a recorded event in the window, grouped by the derived
    ball state of their train slice's outcome block (link.ball_with): ready
    (approved, nothing outstanding, checks not failing), failing checks,
    waiting for a first review, waiting for a reviewer, and the rest counted.
    The newsletter never names whom the next move is with."""
    repo = _slug(view)
    period = _period(view)
    as_of = (view.get("meta") or {}).get("ref_date") or period[1]
    groups = {"ready": [], "failing_checks": [], "needs_first_review": [],
              "awaiting_review": [], "other": {}}
    for t in view.get("trains") or []:
        # every train with PRs: a shipped train can hold an open follow-up;
        # the per-item open state and activity below select
        if not t.get("prs"):
            continue
        try:
            oc = link.slice_train(view, t["id"])["conversation"]["outcome"]
        except (KeyError, TypeError):
            continue
        for it in oc.get("items") or []:
            if it.get("type") != "pr" or it.get("state") != "open" or not it.get("ball"):
                continue
            last = (it.get("last_event") or {}).get("at")
            if not _in_period(last, period):
                continue
            ball = it["ball"]
            checks = it.get("checks") if isinstance(it.get("checks"), dict) else {}
            row = {"number": it.get("number"), "url": it.get("url"),
                   "title": it.get("title"), "train": t["id"],
                   "opened_at": it.get("created_at"), "last_event_at": last,
                   "days_since_last_event": _whole_days(last, as_of),
                   "ball": {"state": ball.get("state"), "rule": ball.get("rule"),
                            "derived": True},
                   "outstanding_change_requests":
                       len((it.get("reviews") or {}).get("outstanding_change_requests") or []),
                   "checks": {"rollup": checks.get("rollup"),
                              "failing": checks.get("failing") or []}
                   if checks.get("status") == "gathered" else
                   {"status": checks.get("status") or "not gathered"},
                   "modules": [a for a in t.get("code_areas") or []
                               if link.module_kind(a) is not None][:3]}
            if t.get("story"):
                row["story"] = story_key(view, t["id"], multi)
            if _NEW_MODULE_TITLE_RE.search(it.get("title") or ""):
                row["title_signal"] = "new_module"
            row = _with_repo(row, repo, multi)
            verdicts = (it.get("reviews") or {}).get("verdicts") or []
            approved = any(x.get("state") == "approved" for x in verdicts) and \
                not row["outstanding_change_requests"]
            row["approved"] = approved
            st = ball.get("state")
            # the derived next move decides; a retained approval does not
            if st == "awaiting merge":
                groups["ready"].append(row)
            elif ball.get("rule") == "R5 failing checks":
                groups["failing_checks"].append(row)
            elif st == "awaiting a first review":
                groups["needs_first_review"].append(row)
            elif st == "awaiting reviewer":
                groups["awaiting_review"].append(row)
            else:
                groups["other"][st] = groups["other"].get(st, 0) + 1
    fc = view.get("forecast") or {}
    groups["forecast"] = [_with_repo({"number": (c.get("ref") or {}).get("id"),
                                      "url": (c.get("ref") or {}).get("url"),
                                      "tier": c.get("tier"), "signals": c.get("signals")},
                                     repo, multi)
                          for c in fc.get("candidates") or [] if c.get("tier") == "likely"]
    groups["quiet"] = len((view.get("stories") or {}).get("quiet") or [])
    return groups


def _finish_coming(groups, caps):
    def wait(r):
        return (-(r.get("days_since_last_event") or 0), r["number"])
    groups["counts"] = {k: len(groups[k]) for k in ("ready", "failing_checks",
                                                    "needs_first_review",
                                                    "awaiting_review")}
    groups["counts"]["other"] = sum(groups["other"].values())
    groups["counts"]["open_prs_active"] = sum(groups["counts"].values())
    for key, cap in (("ready", "ready"), ("failing_checks", "failing"),
                     ("needs_first_review", "first_review"),
                     ("awaiting_review", "awaiting_review")):
        rows = sorted(groups[key], key=wait)
        groups[key] = rows[:caps[cap]]
        if len(rows) > caps[cap]:
            groups[key + "_omitted"] = len(rows) - caps[cap]
    groups["omit"] = None
    if not (groups["ready"] or groups["failing_checks"] or groups["needs_first_review"]
            or groups["awaiting_review"] or groups["other"]):
        groups["omit"] = "no open pull request had recorded activity in the window"
    return groups


def section_plumbing(view, caps, multi=False):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"prs": [], "omit": _NO_MODULE_LAYER}
    pl = (mc.get("summary") or {}).get("plumbing") or {}
    rows = [r for r in mc.get("prs") or [] if r.get("plumbing")]
    rows.sort(key=lambda r: (-len(r.get("areas") or []), r["number"]))
    out = {"merged_prs": pl.get("merged_prs"), "plumbing_prs": pl.get("plumbing_prs"),
           "classified": pl.get("classified"), "share": pl.get("share"),
           "by_class": pl.get("by_class") or {},
           "prs": [_with_repo({"number": r["number"], "url": r.get("url"),
                               "title": r.get("title"), "class": r.get("class"),
                               "areas": len(r.get("areas") or []),
                               "sweep": bool(r.get("sweep"))}, _slug(view), multi)
                   for r in rows],
           "omit": None}
    if not rows:
        out["omit"] = "no merged pull request was plumbing in the window"
    return out


def _coverage(view):
    notes = []
    m = view.get("meta") or {}
    if m.get("state_as_of"):
        notes.append("records are as last gathered on {} (meta.state_as_of)"
                     .format(m["state_as_of"]))
    elif m.get("records_as_of"):
        notes.append("records are shown as of {}; titles and labels as gathered on {}"
                     .format((m["records_as_of"] or {}).get("instant"),
                             (m.get("gathered_at") or "")[:10]))
    aff = (view.get("contributors") or {}).get("affiliation")
    if aff:
        ppl = aff.get("people") or {}
        tot = sum(ppl.values()) or 0
        if tot:
            notes.append("affiliation unknown for {} of {} people ({}%); unknown is "
                         "never counted as community".format(
                             ppl.get("unknown", 0), tot,
                             round(100.0 * ppl.get("unknown", 0) / tot)))
    elif "affiliations" not in view:
        notes.append("affiliations were not gathered: no Microsoft / community split")
    nc = view.get("newcomers")
    if nc is None:
        notes.append("newcomers were not gathered: no first-time contributors")
    elif nc.get("collected") is False:
        notes.append("newcomers were not looked up (--no-newcomers)")
    else:
        if nc.get("unknown"):
            notes.append("newcomer check failed for {} people".format(len(nc["unknown"])))
        if nc.get("not_checked"):
            notes.append("{} people over the newcomer cap were not checked"
                         .format(len(nc["not_checked"])))
    pc = view.get("people_context")
    if not pc:
        notes.append("no people registry context (link.py --people not used): no "
                     "'also maintains' or tenure lines")
    else:
        cov = pc.get("coverage") or {}
        notes.append("the people registry covers {} gathered month(s) ({}); tenure, "
                     "trend and 'also maintains' need more".format(
                         len(cov.get("months") or []), ", ".join(cov.get("months") or [])))
    pa = view.get("pr_activity")
    if not pa:
        notes.append("check results and review-thread resolutions were not gathered")
    else:
        bad = [r.get("number") for r in pa.get("prs") or []
               if r.get("checks_status") != "gathered"]
        if bad:
            notes.append("checks not read for {} PR(s)".format(len(bad)))
        partial = [r.get("number") for r in pa.get("prs") or []
                   if r.get("checks_status") == "gathered" and _partly_read(r)]
        if partial:
            notes.append("checks, review threads or lifecycle events partly read for "
                         "{} PR(s) (earlier commits, check contexts, threads or events "
                         "beyond the read limits)".format(len(partial)))
    mc = view.get("module_changes")
    if isinstance(mc, dict):
        inp = mc.get("inputs") or {}
        if not inp.get("tags_collected"):
            notes.append("version tags were not read: published / not published "
                         "is not known")
        if inp.get("unread"):
            notes.append("{} changelog / version file(s) could not be read"
                         .format(len(inp["unread"])))
    else:
        notes.append(_NO_MODULE_LAYER)
    return notes


def _partly_read(row):
    """True when a gathered pr_activity row left something out: earlier
    commits (commits_total above the commits read), check contexts of a
    commit, review threads or lifecycle events."""
    commits = row.get("commits") or []
    total = row.get("commits_total")
    return bool((isinstance(total, int) and total > len(commits))
                or any(c.get("checks_omitted") for c in commits if isinstance(c, dict))
                or row.get("threads_omitted") or row.get("events_omitted"))


def section_numbers(views, multi=False):
    per = []
    for v in views:
        period = _period(v)
        mc = v.get("module_changes") if isinstance(v.get("module_changes"), dict) else None
        f = {}
        if mc:
            pl = (mc.get("summary") or {}).get("plumbing") or {}
            f.update(merged_prs=pl.get("merged_prs"), classified_prs=pl.get("classified"),
                     plumbing_prs=pl.get("plumbing_prs"),
                     product_prs=(pl.get("classified") or 0) - (pl.get("plumbing_prs") or 0),
                     plumbing_share_pct=round(100 * (pl.get("share") or 0)),
                     modules_touched=(mc.get("summary") or {}).get("modules"),
                     modules_reader_facing=sum(1 for m in mc.get("modules") or []
                                               if not m.get("plumbing_only")),
                     modules_new=len((mc.get("summary") or {}).get("new_modules") or []),
                     modules_declared_breaking=len((mc.get("summary") or {})
                                                   .get("declared_breaking") or []))
            tags = ((v.get("module_notes") or {}).get("tags") or {})
            if tags.get("collected"):
                f["versions_published"] = len(tags.get("items") or [])
        else:
            f["merged_prs"] = len(_merged_in_window(v))
        f["prs_opened"] = sum(1 for p in v.get("prs") or []
                              if _in_period(p.get("created_at"), period))
        f["issues_opened"] = sum(1 for i in v.get("issues") or []
                                 if _in_period(i.get("created_at"), period))
        people = v.get("people") or {}
        f["people"] = sum(1 for p in people.values() if not p.get("is_bot"))
        nc = v.get("newcomers") or {}
        if nc.get("collected"):
            f["first_time_contributors"] = len(nc.get("new") or [])
            if "returning" in nc:
                f["returning_contributors"] = len(nc.get("returning") or [])
            if nc.get("candidates") is not None:
                f["newcomer_candidates"] = nc["candidates"]
        aff = (v.get("contributors") or {}).get("affiliation") or {}
        for label, n in (aff.get("people") or {}).items():
            f["people_" + label] = n
        labelled = sum((aff.get("people") or {}).values())
        if labelled:
            f["people_labelled"] = labelled
            for label, n in (aff.get("people") or {}).items():
                f["people_" + label + "_pct"] = round(100.0 * n / labelled)
        for label in ("maintainer_org", "community", "unknown"):
            row = (aff.get("merged_prs") or {}).get(label)
            if row:
                f["merged_prs_" + label] = row.get("count")
                f["merged_prs_" + label + "_pct"] = round(100 * (row.get("share") or 0))
            row = (aff.get("reviews") or {}).get(label)
            if row:
                f["reviews_" + label] = row.get("count")
                f["reviews_" + label + "_pct"] = round(100 * (row.get("share") or 0))
        if aff.get("merged_prs"):
            f["merged_prs_human"] = aff["merged_prs"].get("total")
        if aff.get("reviews"):
            f["reviews_total"] = aff["reviews"].get("total")
        st = v.get("stories") or {}
        f["stories_selected"] = len(st.get("selected") or [])
        f["trains_in_flight"] = sum(1 for t in v.get("trains") or []
                                    if t.get("outcome") == "in_flight")
        per.append({"repo": _slug(v), "facts": {k: x for k, x in f.items() if x is not None},
                    "coverage": _coverage(v)})
    if len(per) == 1:
        return {"facts": per[0]["facts"], "coverage": per[0]["coverage"], "omit": None}
    total = {}
    additive = ("merged_prs", "classified_prs", "plumbing_prs", "product_prs",
                "modules_touched", "modules_reader_facing", "modules_new",
                "modules_declared_breaking", "versions_published", "prs_opened",
                "issues_opened", "stories_selected", "trains_in_flight")
    for p in per:
        for k in additive:
            if isinstance(p["facts"].get(k), int):
                total[k] = total.get(k, 0) + p["facts"][k]
    if total.get("classified_prs"):
        total["plumbing_share_pct"] = round(100.0 * total.get("plumbing_prs", 0)
                                            / total["classified_prs"])
    return {"facts": total, "by_repo": per,
            "coverage": sorted({c for p in per for c in p["coverage"]}),
            "note": "people counts are per repository (a person can work in several)",
            "omit": None}


def section_intro(out):
    """What the intro leads with, in this order: declared breaking changes,
    new modules, the lead modules, newcomers who landed merged work, the top
    story, the plumbing share."""
    sec = {s["id"]: s for s in out["sections"]}
    lead = []
    b = sec["breaking"]
    if b.get("declared"):
        lead.append({"fact": "declared_breaking",
                     "areas": [d["area"] for d in b["declared"]]})
    if sec["new_modules"].get("modules"):
        lead.append({"fact": "new_modules",
                     "areas": [m["area"] for m in sec["new_modules"]["modules"]]})
    mods = sec["module_changes"].get("modules") or []
    if mods:
        lead.append({"fact": "lead_modules", "areas": [m["area"] for m in mods[:3]]})
    landed = [r["login"] for r in sec["people"].get("newcomers") or []
              if r["contribution"] in ("new_module", "module_change", "release", "docs",
                                       "plumbing")]
    if landed:
        lead.append({"fact": "newcomers_landed_work", "logins": landed})
    if sec["stories"].get("full"):
        lead.append({"fact": "top_story", "id": sec["stories"]["full"][0]["id"]})
    if sec["plumbing"].get("share") is not None:
        lead.append({"fact": "plumbing_share", "share": sec["plumbing"]["share"]})
    return {"lead_with": lead, "sentences": [3, 5], "omit": None}


def outline(views, cadence="month"):
    """The ordered, pre-selected material of one issue over one or more linked
    views (the members of a stream). Pure: reads the views, never modifies
    them. Raises ValueError for an unknown cadence."""
    if cadence not in CADENCE:
        raise ValueError("cadence must be one of {}".format(sorted(CADENCE)))
    caps = CADENCE[cadence]
    views = list(views)
    multi = len(views) > 1
    froms = sorted(f for f, _ in (_period(v) for v in views) if f)
    tos = sorted(t for _, t in (_period(v) for v in views) if t)
    frm, to = (froms[0] if froms else None), (tos[-1] if tos else None)

    def merged(builder, keys, *args):
        parts = [builder(v, *args, multi=multi) for v in views]
        out = {k: [x for p in parts for x in p.get(k) or []] for k in keys}
        omits = [p.get("omit") for p in parts if p.get("omit")]
        out["omit"] = omits[0] if len(omits) == len(parts) else None
        return out, parts

    breaking, _ = merged(section_breaking, ("declared", "candidates", "deprecations",
                                            "notices"))
    breaking["declared"].sort(key=lambda r: (-len(r["items"]), r["area"], r.get("repo", "")))
    new_mods, _ = merged(section_new_modules, ("modules", "child_modules"), caps)
    changes, parts = merged(section_module_changes, ("modules", "plumbing_only"), caps)
    changes["modules"].sort(key=lambda r: (-r["score"], r["area"], r.get("repo", "")))
    changes = _cut_modules(changes, caps)
    if changes["omit"] is None and not changes["modules"]:
        changes["omit"] = "no module had a reader-facing change in the window"
    coming = {"ready": [], "failing_checks": [], "needs_first_review": [],
              "awaiting_review": [], "other": {}, "forecast": [], "quiet": 0}
    for v in views:
        g = section_coming(v, caps, multi=multi)
        for k in ("ready", "failing_checks", "needs_first_review", "awaiting_review",
                  "forecast"):
            coming[k] += g[k]
        for k, n in g["other"].items():
            coming["other"][k] = coming["other"].get(k, 0) + n
        coming["quiet"] += g["quiet"]
    coming = _finish_coming(coming, caps)
    plumbing, pparts = merged(section_plumbing, ("prs",), caps)
    if len(pparts) == 1:
        plumbing.update({k: pparts[0].get(k) for k in ("merged_prs", "plumbing_prs",
                                                       "classified", "share", "by_class")})
    else:
        tot = sum(p.get("classified") or 0 for p in pparts)
        plum = sum(p.get("plumbing_prs") or 0 for p in pparts)
        plumbing.update(merged_prs=sum(p.get("merged_prs") or 0 for p in pparts),
                        plumbing_prs=plum, classified=tot,
                        share=round(plum / tot, 3) if tot else None)
    plumbing["prs"].sort(key=lambda r: (-r["areas"], r["number"]))
    plumbing["prs_omitted"] = max(0, len(plumbing["prs"]) - caps["plumbing_prs"])
    plumbing["prs"] = plumbing["prs"][:caps["plumbing_prs"]]
    sections = [
        {"id": "breaking", **breaking},
        {"id": "new_modules", **new_mods},
        {"id": "module_changes", **changes},
        {"id": "stories", **section_stories(views, caps, multi)},
        {"id": "people", **section_people(views, caps, multi)},
        {"id": "coming", **coming},
        {"id": "plumbing", **plumbing},
        {"id": "numbers", **section_numbers(views, multi)},
    ]
    for s in sections:
        s["heading"] = HEADINGS[s["id"]][0]
    out = {"kind": "newsletter_outline", "version": 1,
           "repos": [_slug(v) for v in views], "period": {"from": frm, "to": to},
           "period_label": period_label(frm, to), "cadence": cadence,
           "as_of": max(((v.get("meta") or {}).get("records_as_of") or {}).get("instant")
                        or (v.get("meta") or {}).get("ref_date") or "" for v in views),
           "sections": sections}
    out["sections"].insert(0, {"id": "intro", "heading": None, **section_intro(out)})
    if not multi:
        out["repo"] = out["repos"][0]
    return out


# --- the check ----------------------------------------------------------------

_STORY_BLOCK_RE = re.compile(r"<!--\s*story:\s*(\S+)\s*-->(.*?)<!--\s*/story\s*-->", re.S)
_OMITTED_RE = re.compile(r"<!--\s*omitted:\s*([a-z_]+)\s*(?:—|-|:)\s*(\S.*?)\s*-->")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_FENCE_RE = re.compile(r"```.*?```", re.S)
_URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
_AVM_PATH_RE = re.compile(r"\bavm/(?:res|ptn|utl)/[a-z0-9-]+/[a-z0-9-]+(?:/[a-z0-9-]+)*")
_PATHY_RE = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?![\w.]|-\w)")
_ITEM_RE = re.compile(r"(?<![\w/.&])#(\d+)\b")
_LOGIN_RE = re.compile(r"(?<![\w/.@`])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)\b")
_NUMBER_RE = re.compile(r"(?<![\w.#/-])(\d[\d,]*(?:\.\d+)?)(%?)(?![\w.]\d|\w)")
_BREAK_WORD_RE = re.compile(r"\bbreak(?:s|ing)?\b", re.I)
_HEDGE_RE = re.compile(r"check before upgrading|\bcandidate|\bmay\b|\bmight\b|"
                       r"\bdetected\b|\bnot declared\b|\bnot listed\b", re.I)
_YEAR_RE = re.compile(r"^(19|20)\d\d$")

# Words that characterise people, effort or mood (story brief rule 7,
# reference/newsletter.md "What not to say"). Not exhaustive.
CHARACTERISING_WORDS = ("finally", "frustrated", "frustrating", "sloppy", "patiently",
                        "tireless", "tirelessly", "heroic", "heroically", "lazy",
                        "careless", "unfortunately", "sadly", "pushed back hard",
                        "ignored", "dragged", "struggled", "annoyed", "impatient",
                        "rockstar", "amazing work", "stalled by")


def _strip(text, *, code=True):
    """Blank URLs, HTML comments, fences and (optionally) code spans, keeping
    offsets so lines and sentences stay where they were."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _COMMENT_RE.sub(blank, text)
    text = _FENCE_RE.sub(blank, text)
    text = _URL_RE.sub(blank, text)
    if code:
        text = _CODE_SPAN_RE.sub(blank, text)
    return text


def _paragraphs(text):
    """Paragraphs and list items: split on blank lines and on list markers."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        cur = []
        for line in block.split("\n"):
            if re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line) and cur:
                out.append("\n".join(cur))
                cur = []
            cur.append(line)
        if cur:
            out.append("\n".join(cur))
    return [p for p in out if p.strip()]


def _sentences(par):
    return [s for s in re.split(r"(?<=[.!?;])\s+|\n", par) if s.strip()]


def _view_strings(views):
    urls, texts = set(), []
    for v in views:
        link._slice_strings(v, urls, texts)
    return urls, texts


def _numbers_in(obj, out):
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        out.add(str(obj))
    elif isinstance(obj, float):
        out.add(str(obj))
        if 0 <= obj <= 1:
            out.add(str(round(obj * 100)))
            out.add(str(round(obj * 100, 1)))
    elif isinstance(obj, str):
        if re.fullmatch(r"\d+(?:\.\d+)*", obj):
            out.add(obj)
    elif isinstance(obj, dict):
        for x in obj.values():
            _numbers_in(x, out)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            _numbers_in(x, out)


def _module_index(views):
    """{area: set of versions it knows} over every module of the views
    (changelog versions, tags, version files, children's), and the set of
    every known module path (areas, children, code areas)."""
    versions, paths, text = {}, set(), []
    for v in views:
        for a in (v.get("code_graph") or {}).get("areas") or []:
            if a.get("id"):
                paths.add(a["id"])
        mc = v.get("module_changes")
        if not isinstance(mc, dict):
            continue
        for m in mc.get("modules") or []:
            area = m["area"]
            paths.add(area)
            vs = versions.setdefault(area, set())
            ver = m.get("versions") or {}
            for k in ("changelog", "declared_not_published", "published_not_declared"):
                vs.update(_bare(x) for x in ver.get(k) or [])
            for p in ver.get("published") or []:
                vs.add(_bare(p.get("version")))
                if p.get("module"):
                    paths.add(p["module"])
            vf = ver.get("version_file") or {}
            vs.update(_bare(x) for x in (vf.get("from"), vf.get("to")) if x)
            if m.get("first_version"):
                vs.add(_bare(m["first_version"]))
            for c in m.get("changelog") or []:
                vs.add(_bare(c.get("version")))
                if c.get("module"):
                    paths.add(c["module"])
                for s in c.get("sections") or []:
                    for t in _items_text(s.get("items")):
                        text.append(t or "")
                        vs.update(_VERSION_RE.findall(t or ""))
            for ch in (m.get("structure") or {}).get("children") or []:
                if ch.get("path"):
                    paths.add(ch["path"])
                if ch.get("from"):
                    paths.add(ch["from"])
            for c in (m.get("breaking") or {}).get("candidates") or []:
                for part in str(c.get("subject") or "").split(" -> "):
                    if "/" in part:
                        paths.add(part.strip())
            vs.discard(None)
        text.extend(p.get("title") or "" for p in v.get("prs") or [])
    return versions, paths, "\n".join(text)


def _area_of(path, areas):
    best = None
    for a in areas:
        if path == a or path.startswith(a + "/"):
            if best is None or len(a) > len(best):
                best = a
    return best


def check_newsletter(views, text, slices=None, registry_names=None, cadence="month"):
    """The mechanical check of an issue against its views
    (reference/newsletter.md "The check"). `slices` maps a story train id to
    its slice (link.py --slice output); a story block without one is listed
    in `stories_not_checked`. `registry_names` maps a do_not_name login to a
    name the registry holds for it (that name must not appear). Pure."""
    views = list(views)
    out_l = outline(views, cadence=cadence)
    sec = {s["id"]: s for s in out_l["sections"]}
    slices = slices or {}
    res = {"ok": True}

    # 1. story blocks: checked against their own slices, then blanked
    stories, not_checked, not_in_outline, duplicate, seen = {}, [], [], [], set()
    multi = len(views) > 1
    story_ids = {s["key"] for s in sec["stories"].get("full") or []} | \
        {s["key"] for s in sec["stories"].get("pointers") or []} | \
        {key for v in views for key in _story_trains(v, multi)}
    for m in _STORY_BLOCK_RE.finditer(text):
        tid, body = m.group(1), m.group(2).strip()
        if tid in seen and tid not in duplicate:
            duplicate.append(tid)
        seen.add(tid)
        if tid not in story_ids:
            not_in_outline.append(tid)
        sl = slices.get(tid)
        if sl is None:
            if tid not in not_checked:
                not_checked.append(tid)
        else:
            r = link.check_story_evidence(sl, body)
            r = {k: x for k, x in r.items() if x or k == "ok"}
            prev = stories.get(tid)
            if prev is not None and not prev.get("ok"):
                r = prev            # a failing block is never hidden by a later one
            stories[tid] = r
    body = _STORY_BLOCK_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    res.update(stories=stories, stories_not_checked=not_checked,
               stories_not_in_outline=not_in_outline, stories_duplicate=duplicate)

    # 2. sections: present (heading) or omitted with a reason, in order
    headings = []
    for m in re.finditer(r"^##\s+(.+?)\s*$", body, re.M):
        h = m.group(1).strip().strip("*_").lower()
        sid = next((k for k, al in HEADINGS.items()
                    if any(h.startswith(a.lower()) for a in al)), None)
        headings.append((sid, m.group(1).strip()))
    present = [sid for sid, _ in headings if sid]
    omitted = {m.group(1): m.group(2) for m in _OMITTED_RE.finditer(body)}
    missing, padded = [], []
    for sid in SECTION_ORDER[1:]:
        o = sec[sid].get("omit")
        if o:
            if sid in present:
                padded.append(sid)
            elif sid not in omitted:
                missing.append(sid)
        elif sid not in present and sid not in omitted:
            missing.append(sid)
    order = [SECTION_ORDER.index(s) for s in present]
    res.update(sections_missing=missing, omitted_section_present=padded,
               sections_out_of_order=order != sorted(order),
               unknown_headings=[h for sid, h in headings if sid is None])
    first_h2 = re.search(r"^##\s", body, re.M)
    intro = body[:first_h2.start()] if first_h2 else body
    intro_text = re.sub(r"^#\s.*$", "", _COMMENT_RE.sub("", intro), flags=re.M).strip()
    res["title_missing"] = not re.search(r"^#\s+\S", body, re.M)
    res["intro_missing"] = not intro_text

    # 3. what the prose may name
    urls, texts = _view_strings(views)
    norm_texts = [link._norm_ws(t) for t in texts]
    versions, paths, module_text = _module_index(views)
    areas = sorted(versions)
    global_versions = set().union(*versions.values()) if versions else set()
    items = {(n) for v in views for n in [p.get("number") for p in v.get("prs") or []]
             + [i.get("number") for i in v.get("issues") or []]}
    people = {}
    hidden = set(registry_names or {})
    for v in views:
        hidden |= _do_not_name(v)
        for login in set((v.get("people") or {})) | set(((v.get("affiliations") or {})
                                                           .get("people") or {})):
            people.setdefault(login, _person(v, login))
        for p in v.get("prs") or []:
            if p.get("author"):
                people.setdefault(p["author"], _person(v, p["author"]))
    home_words = set()
    for v in views:
        owner = ((v.get("meta") or {}).get("owner") or "").lower()
        a = v.get("affiliations") or {}
        home_words |= {n.lower() for n in a.get("home_names") or [] if n.lower() != owner}
        home_words |= {n.lower() for n in a.get("home_orgs") or [] if n.lower() != owner}

    # URLs, quotes
    cited = []
    for u in _URL_RE.findall(_COMMENT_RE.sub(" ", body)):
        u = u.rstrip(".,;:")
        if u not in cited:
            cited.append(u)
    res["urls_not_in_view"] = [u for u in cited if u not in urls]
    quotes = []
    for m in link._QUOTE_RE.finditer(_COMMENT_RE.sub(" ", body)):
        q = m.group(1) or m.group(2)
        if not q.startswith(("http://", "https://")):
            quotes.append(q)
    res["quotes_not_in_view"] = [q for q in quotes
                                 if not any(link._norm_ws(q) in t for t in norm_texts)]
    # strip quotes for the remaining prose checks (they are verified above)
    no_quotes = link._QUOTE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                                   _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ",
                                                                    m.group(0)), body))
    with_code = _strip(no_quotes, code=False)
    prose = _strip(no_quotes, code=True)
    # suppression reads the text with its quotations and code spans kept: a
    # supported quote may still name a do_not_name person
    quoted_prose = _strip(body, code=False)

    # modules: an AVM module path (or a Terraform `modules/<name>` submodule)
    # must be a module, child, code area or path the view's changelogs name
    named_mods = []
    for m in _AVM_PATH_RE.finditer(with_code):
        tok = m.group(0).rstrip("/.-")
        if tok not in named_mods:
            named_mods.append(tok)
    for m in _CODE_SPAN_RE.finditer(no_quotes):
        tok = m.group(0).strip("`")
        if tok.startswith("modules/") and _PATHY_RE.match(tok) and tok not in named_mods:
            named_mods.append(tok)
    res["modules_not_in_view"] = sorted({p for p in named_mods
                                         if p not in paths and p not in module_text})

    # versions, bound to the one module a paragraph names
    bad_versions = []
    for par in _paragraphs(no_quotes):
        par_c = _strip(par, code=False)
        mods = {_area_of(m.group(0), areas) for m in _AVM_PATH_RE.finditer(par_c)}
        mods |= {_area_of(m.group(0).strip("`"), areas)
                 for m in _CODE_SPAN_RE.finditer(par)}
        mods.discard(None)
        for ver in _VERSION_RE.findall(_strip(par, code=False)):
            ok_set = versions[next(iter(mods))] if len(mods) == 1 else global_versions
            if ver not in ok_set:
                row = {"module": next(iter(mods)) if len(mods) == 1 else None,
                       "version": ver}
                if row not in bad_versions:
                    bad_versions.append(row)
    res["versions_not_in_view"] = bad_versions

    # items
    res["items_not_in_view"] = sorted({int(n) for n in _ITEM_RE.findall(prose)
                                       if int(n) not in items})

    # people: known logins, names tied to their login, do_not_name, affiliation words
    mentioned = _LOGIN_RE.findall(prose)
    res["people_not_in_view"] = sorted({m for m in mentioned if m not in people})
    dnn = sorted({m for m in _LOGIN_RE.findall(quoted_prose) if m in hidden})
    for login, name in (registry_names or {}).items():
        if name and re.search(r"\b" + re.escape(name) + r"\b", quoted_prose):
            dnn.append(login)
    for login in hidden:
        if re.search(r"(?<![\w/@-])" + re.escape(login) + r"(?![\w-])", quoted_prose) \
                and login not in dnn:
            dnn.append(login)
    res["do_not_name_named"] = sorted(set(dnn))
    mismatch = []
    names = {p["name"]: login for login, p in people.items() if p.get("name")}
    for m in re.finditer(r"\(@([A-Za-z0-9-]+)[,)]", prose):
        login = m.group(1)
        before = prose[max(0, m.start() - 80):m.start()].split("\n")[-1]
        before = re.sub(r"[*_\[\]]", "", before).rstrip()
        want = (people.get(login) or {}).get("name")
        if want:
            if not before.endswith(want):
                mismatch.append({"login": login, "name": want,
                                 "written": before[-len(want) - 10:].strip()})
        else:
            other = next((n for n, lg in names.items() if lg != login
                          and before.endswith(n)), None)
            if other:
                mismatch.append({"login": login, "name": None, "written": other})
    res["name_mismatch"] = mismatch
    aff_bad = []
    for par in _paragraphs(prose):
        for s in _sentences(par):
            logins = [x for x in _LOGIN_RE.findall(s) if x in people]
            if not logins:
                continue
            says = []
            if re.search(r"\bcommunity\b", s, re.I):
                says.append("community")
            if any(re.search(r"\b" + re.escape(w) + r"\b", s, re.I) for w in home_words):
                says.append("maintainer_org")
            for said in says:
                for lg in logins:
                    if people[lg].get("affiliation") != said:
                        aff_bad.append({"login": lg, "said": "community"
                                        if said == "community" else "home organisation",
                                        "label": people[lg].get("affiliation"),
                                        "sentence": s.strip()[:200]})
    res["affiliation_mismatch"] = aff_bad

    # breaking candidates worded as declared
    declared_areas = {d["area"] for d in sec["breaking"].get("declared") or []}
    cand = {c["area"]: c for c in sec["breaking"].get("candidates") or []}
    worded = []
    for par in _paragraphs(no_quotes):
        par_c = _strip(par, code=False)
        if not _BREAK_WORD_RE.search(_strip(par, code=True)) or \
                _HEDGE_RE.search(par_c):
            continue
        mods = {_area_of(m.group(0), areas) for m in _AVM_PATH_RE.finditer(par_c)}
        mods |= {_area_of(m.group(0).strip("`"), areas)
                 for m in _CODE_SPAN_RE.finditer(par)}
        for a in sorted(x for x in mods if x in cand):
            uncovered = [r["subject"] for r in cand[a]["rules"]
                         if not r["covered_by_declared"]]
            subj_named = [s_ for s_ in uncovered if s_ and s_.split(" ->")[0]
                          .split("/")[-1] in par]
            if a not in declared_areas or subj_named:
                worded.append({"module": a, "subjects": subj_named or uncovered,
                               "paragraph": " ".join(par.split())[:200]})
    res["candidates_worded_as_declared"] = worded

    # dates carry the year
    no_year = []
    for m in link._DATE_IN_PROSE_RE.finditer(prose):
        if not (m.group(1) or m.group(2) or m.group(3)):
            no_year.append(m.group(0))
    res["dates_without_year"] = no_year

    # numbers the view does not hold
    allowed = set()
    _numbers_in(out_l, allowed)
    for v in views:
        _numbers_in((v.get("contributors") or {}).get("affiliation"), allowed)
    num_prose = link._DATE_IN_PROSE_RE.sub(lambda m: " " * len(m.group(0)), prose)
    num_prose = _VERSION_RE.sub(lambda m: " " * len(m.group(0)), num_prose)
    num_prose = re.sub(r"\b\d+\.\d+(?:\.\d+)*\b", lambda m: m.group(0)
                       if m.group(0).replace(".", "", 1).isdigit() else " ", num_prose)
    num_prose = _ITEM_RE.sub(lambda m: " " * len(m.group(0)), num_prose)
    num_prose = re.sub(r"^\s*\d+\.\s", lambda m: " " * len(m.group(0)), num_prose,
                       flags=re.M)
    bad_nums = []
    for m in _NUMBER_RE.finditer(num_prose):
        tok = m.group(1).replace(",", "")
        if _YEAR_RE.match(tok) or tok in allowed:
            continue
        if tok not in bad_nums:
            bad_nums.append(tok)
    res["numbers_not_in_view"] = bad_nums

    # characterising words
    low = prose.lower()
    res["characterising_words"] = [w for w in CHARACTERISING_WORDS
                                   if re.search(r"\b" + re.escape(w) + r"\b", low)]

    fail_keys = ("stories_not_checked", "stories_not_in_outline", "stories_duplicate",
                 "sections_missing",
                 "omitted_section_present", "sections_out_of_order", "title_missing",
                 "intro_missing", "urls_not_in_view", "quotes_not_in_view",
                 "modules_not_in_view", "versions_not_in_view", "items_not_in_view",
                 "people_not_in_view", "do_not_name_named", "name_mismatch",
                 "affiliation_mismatch", "candidates_worded_as_declared",
                 "dates_without_year", "numbers_not_in_view", "characterising_words")
    res["ok"] = not any(res[k] for k in fail_keys) and \
        all(r.get("ok") for r in stories.values())
    res["checked"] = {"urls": len(cited), "quotes": len(quotes),
                      "modules": len(named_mods), "people": len(set(mentioned)),
                      "stories": len(stories) + len(not_checked),
                      "sections_present": present, "sections_omitted": sorted(omitted)}
    res["cannot_check"] = list(CANNOT_CHECK)
    return res


CANNOT_CHECK = (
    "that a paraphrase says what the changelog or the record says (only quoted text is "
    "compared verbatim)",
    "that a 'why it matters' line is supported by the record",
    "that a number is attached to the right fact (a number is only required to be one "
    "the outline or view holds)",
    "that a person's contribution is described by the right class",
    "tone and fairness beyond the listed characterising words",
    "that the selection rules were followed for which modules and stories appear",
    "anything inside a story block beyond link.py's story check",
)


# --- CLI ----------------------------------------------------------------------

_USAGE = ("usage: newsletter.py VIEW.json [VIEW.json ...] --outline [--weekly]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --check-newsletter ISSUE.md "
          "[--slices DIR] [--people PEOPLE.json] [--weekly]\n")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("newsletter.py: error: cannot read {} ({})\n".format(path, exc))
        raise SystemExit(2)


def _registry_hidden(path):
    """{login: name or None} of the registry's do_not_name entries."""
    reg = _load(path)
    out = {}
    for login, e in ((reg or {}).get("people") or {}).items():
        if isinstance(e, dict) and e.get("do_not_name"):
            out[login] = e.get("name")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(_USAGE)
        raise SystemExit(0)
    cadence = "month"
    if "--weekly" in argv:
        argv.remove("--weekly")
        cadence = "week"
    opts = {}
    for flag in ("--check-newsletter", "--slices", "--people"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                sys.stderr.write(_USAGE)
                raise SystemExit(2)
            opts[flag] = argv[i + 1]
            del argv[i:i + 2]
    do_outline = "--outline" in argv
    if do_outline:
        argv.remove("--outline")
    paths = argv
    if not paths or any(p.startswith("-") for p in paths) or \
            do_outline == ("--check-newsletter" in opts) or \
            (do_outline and ("--slices" in opts or "--people" in opts)):
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    views = [_load(p) for p in paths]
    if do_outline:
        json.dump(outline(views, cadence=cadence), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    try:
        with open(opts["--check-newsletter"], encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write("newsletter.py: error: cannot read {} ({})\n".format(
            opts["--check-newsletter"], exc))
        raise SystemExit(2)
    slices = {}
    if "--slices" in opts:
        for m in _STORY_BLOCK_RE.finditer(text):
            p = os.path.join(opts["--slices"], slice_filename(m.group(1)))
            if os.path.exists(p):
                slices[m.group(1)] = _load(p)
    names = _registry_hidden(opts["--people"]) if "--people" in opts else None
    res = check_newsletter(views, text, slices=slices, registry_names=names,
                           cadence=cadence)
    json.dump(res, sys.stdout, indent=2)
    sys.stdout.write("\n")
    raise SystemExit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
