#!/usr/bin/env python3
"""The newsletter layout: a release-notes style issue written from a linked
view (reference/newsletter.md).

Two read-only commands, neither of which writes the view:

  newsletter.py VIEW.json [VIEW.json ...] --outline [--weekly|--monthly]
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
          [--slices DIR] [--people PEOPLE.json] [--weekly|--monthly]
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

# Section ids in the order the issue presents them (reference/newsletter.md):
# new, then changed, then breaking (the owner's order; the intro flags a
# breaking change and links down to it).
SECTION_ORDER = ("intro", "new_modules", "module_changes", "breaking", "stories",
                 "ownership", "people", "coming", "plumbing", "numbers")

# The heading each section is written under; a heading starting with one of the
# aliases (case-insensitive) is that section. The intro has no heading: it is
# the text between the `# ` title and the first `## ` heading.
HEADINGS = {
    "new_modules":    ("New modules",),
    "module_changes": ("What changed in existing modules", "Changes to existing modules",
                       "Noteworthy changes"),
    "breaking":       ("Breaking changes and deprecations", "Breaking changes",
                       "Deprecations"),
    "stories":        ("Stories", "Behind the changes"),
    "ownership":      ("Module ownership",),
    "people":         ("People", "Contributors"),
    "coming":         ("Coming up and stuck", "Coming up", "What is coming"),
    "plumbing":       ("Plumbing: what was engineered", "Plumbing"),
    "numbers":        ("By the numbers", "Numbers"),
}

# Story groups, in order, with the `###` heading each is written under (the
# plumbing group sits in the plumbing section, under its theme). The state is
# what the story's first line says.
STORY_GROUPS = ("landed", "in_flight", "stalled", "closed", "plumbing")
GROUP_HEADINGS = {"landed": "Landed", "in_flight": "In flight", "stalled": "Stalled",
                  "closed": "Closed without landing"}
STATE_LABELS = {"landed": "Landed", "in_flight": "In flight", "stalled": "Stalled",
                "closed": "Closed"}

# No length target: every story is written in full and every changed module is
# listed. The one limit below only stops absurd output (a runaway view) and is
# never a selection rule; a cut list says so (`<key>_omitted`).
SAFETY_LIMIT = 500
_CAP_KEYS = ("modules", "changelog_items", "newcomers", "maintainers", "top", "ready",
             "failing", "first_review", "awaiting_review", "plumbing_prs")
CADENCE = {"month": {k: SAFETY_LIMIT for k in _CAP_KEYS},
           "week": {k: SAFETY_LIMIT for k in _CAP_KEYS}}
# A window of at most this many days is a weekly issue unless the caller says
# --monthly (a 14-day window is two weeks, not a month); longer is monthly.
WEEKLY_MAX_DAYS = 16


def auto_cadence(views):
    """("week" | "month", window days) from the views' combined period:
    weekly when the window is at most WEEKLY_MAX_DAYS days."""
    froms = sorted(f for f, _ in (_period(v) for v in views) if f)
    tos = sorted(t for _, t in (_period(v) for v in views) if t)
    if not froms or not tos:
        return "month", None
    days = (datetime.date.fromisoformat(tos[-1][:10]) -
            datetime.date.fromisoformat(froms[0][:10])).days + 1
    return ("week" if days <= WEEKLY_MAX_DAYS else "month"), days

# What a person's work in the window is, best first: the module layer's PR
# headline, `docs` split out of plumbing, then open work, then an issue.
CONTRIBUTION_ORDER = ("new_module", "module_change", "release", "docs", "plumbing",
                      "unknown",
                      "open_pr", "issue")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# An open PR's files are not in the store, so a proposed module is known only
# from its title: a signal, never a fact (reference/newsletter.md).
_NEW_MODULE_TITLE_RE = re.compile(r"\bnew module\b|\binitial\b.{0,40}\bmodule\b", re.I)

_NO_MODULE_LAYER = ("the view has no module layer (module_notes were not gathered, "
                    "so module_changes is absent)")


# --- small helpers ------------------------------------------------------------

def login_key(login):
    """Logins compare case-insensitively (as GitHub's do): the one key every
    person lookup uses."""
    return (login or "").lower()


def slug_key(slug):
    """Repository slugs compare case-insensitively."""
    return (slug or "").lower()


def version_key(v):
    """Versions compare without a leading `v` (link._bare_version's rule)."""
    return _bare(v)


def story_key_norm(key):
    """A story key for comparison: the repository part of a qualified key
    (`owner/repo:train-id`) case-insensitive, the train id exact."""
    key = key or ""
    if ":" in key:
        repo, tid = key.split(":", 1)
        return slug_key(repo) + ":" + tid
    return key


# Module paths and train ids compare exactly: repository paths are
# case-sensitive and train ids are generated in one form.


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
    return story_key_norm(key).replace("/", "__").replace(":", "__") + ".slice.json"


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

def _tags_complete(view):
    """True when the view's module layer read version tags for its whole
    period (link `tags_partial`: windows_without_tags), so an absent tag
    means not published; False when that is not established."""
    inp = (view.get("module_changes") or {}).get("inputs") or {}
    return bool(inp.get("tags_collected")) and not inp.get("windows_without_tags")


def _publication(view, published, module, version):
    """True when a tag of (module, version) is recorded; False only when the
    tags were read for the whole period; else None (unknown)."""
    if (module, version_key(version)) in published:
        return True
    return False if _tags_complete(view) else None


def _published_set(m):
    return {(p.get("module") or m["area"], version_key(p.get("version")))
            for p in (m.get("versions") or {}).get("published") or []}


def _own_published(m):
    return [p.get("version") for p in (m.get("versions") or {}).get("published") or []
            if not p.get("module") or p.get("module") == m.get("area")]


def _base_branch(view):
    return (view.get("meta") or {}).get("base_branch") or "main"


def module_url(view, path):
    """(url, source) of a module folder: the view's own `url` when the module
    layer records one, else derived from the repository, base branch and
    path (`derived`). A repository-root module (`main.tf`) is the
    repository's tree."""
    for m in (view.get("module_changes") or {}).get("modules") or []:
        if m.get("area") == path and m.get("url"):
            return m["url"], "view"
    if path == "main.tf":
        return "https://github.com/{}/tree/{}".format(_slug(view), _base_branch(view)), \
            "derived"
    return "https://github.com/{}/tree/{}/{}".format(
        _slug(view), _base_branch(view), path), "derived"


def tag_url(view, tag, recorded=None):
    """(url, source) of a version tag: the recorded one, else the tag's tree."""
    if recorded:
        return recorded, "view"
    return "https://github.com/{}/tree/{}".format(_slug(view), tag), "derived"


def module_release(view, m, module=None):
    """What a module's changes mean for a reader, per version: `published`
    (with tag, date and link), `declared_not_published` (declared in the
    changelog, no tag in the window, tags read for the whole period),
    `unknown` (tags not read for the whole period), or one row
    `no_version_change` when the window declared and tagged no version.
    `module` selects a child module (default: the module itself)."""
    module = module or m["area"]
    v = m.get("versions") or {}
    pub = {}
    for p in v.get("published") or []:
        if (p.get("module") or m["area"]) == module:
            pub[version_key(p.get("version"))] = p
    declared = set()
    if module == m["area"]:
        declared |= {version_key(x) for x in v.get("changelog") or []}
        vf = v.get("version_file") or {}
        # a version file holds major.minor: a version of its own only when no
        # changelog entry or tag of the module states the full version
        if vf.get("to") and vf.get("to") != vf.get("from") and not declared and not pub:
            declared.add(version_key(vf["to"]))
    for c in m.get("changelog") or []:
        if (c.get("module") or m["area"]) == module and c.get("version"):
            declared.add(version_key(c["version"]))
    rows = []
    for ver in sorted(set(pub) | {d for d in declared if d and d[0].isdigit()},
                      key=_vkey):
        if ver in pub:
            p = pub[ver]
            url, src = tag_url(view, p.get("tag"), p.get("url"))
            row = {"module": module, "version": ver, "status": "published",
                   "tag": p.get("tag"), "date": (p.get("date") or "")[:10] or None,
                   "url": url, "url_source": src}
            for k in ("registry", "contents", "note"):
                if p.get(k):
                    row[k] = p[k]
            rows.append(row)
        else:
            rows.append({"module": module, "version": ver,
                         "status": "declared_not_published" if _tags_complete(view)
                         else "unknown"})
    if not rows:
        rows.append({"module": module, "status": "no_version_change"})
    return rows


def pr_release(view, number):
    """What a merged PR means for a reader: the release rows of the versions
    its changelog entries declared (module_release), `no_version_change` for
    plumbing / docs / tests or a source change that declared no version, or
    `unknown` when the module layer does not classify the PR."""
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return [{"status": "unknown"}]
    row = next((r for r in mc.get("prs") or [] if r.get("number") == number), None)
    if row is None or row.get("class") == "unknown":
        return [{"status": "unknown"}]
    out = []
    for m in mc.get("modules") or []:
        if not any(x.get("number") == number for x in m.get("prs") or []):
            continue
        hit = False
        for c in m.get("changelog") or []:
            if any(src.get("pr") == number for src in c.get("sources") or []):
                module = c.get("module") or m["area"]
                for r in module_release(view, m, module):
                    if r.get("version") == version_key(c.get("version")) and r not in out:
                        out.append(r)
                        hit = True
        if not hit and not row.get("plumbing"):
            if (m.get("versions") or {}).get("published_not_declared"):
                # a version was tagged in the window with no changelog entry:
                # whether this PR is in it is not established
                out.append({"module": m["area"], "status": "unknown",
                            "note": "tagged {} in the window without a changelog entry"
                                    .format(", ".join(m["versions"]["published_not_declared"]))})
            else:
                out.append({"module": m["area"], "status": "no_version_change"})
    if not out:
        out.append({"status": "no_version_change"})
    return out


PR_DESCRIPTION_CAP = 800   # outline rows only; the work package gives the full text


def _pr_description(pr, full=False):
    """The PR's own description (its `## Description` part when the template
    has one), HTML comments removed, whitespace collapsed — as last
    gathered, not as of the window. The outline keeps PR_DESCRIPTION_CAP
    characters; `full` (the work package's row and theme files) keeps all."""
    b = (pr.get("body") or "").replace("\r\n", "\n")
    b = re.sub(r"<!--.*?-->", "", b, flags=re.S)
    k = b.find("## Description")
    if k >= 0:
        b = b[k + len("## Description"):]
        nxt = b.find("\n## ")
        if nxt >= 0:
            b = b[:nxt]
    b = " ".join(b.split())
    return (b if full else b[:PR_DESCRIPTION_CAP]) or None


def is_automation(view, login, extra=()):
    """An automation account: the view marks it a bot, the login rule says
    so (`[bot]`), or the operator declared it (`--automation LOGIN`)."""
    if not login:
        return False
    if login_key(login) in {login_key(x) for x in extra}:
        return True
    row = (view.get("people") or {}).get(login) or {}
    if "automation" in row:
        # the view's own marking (account type, the registry's operator tag);
        # `null` is the operator's "not automation" and wins
        return bool(row["automation"])
    return bool(row.get("is_bot")) or link.derive.is_bot_login(login)


def fork_status(view, pr):
    """`fork`, `deleted_fork`, `not_fork` or `unknown`: the view's own
    `fork_status`, else derived from `head_repo` (unknown without one)."""
    if pr.get("fork_status"):
        return pr["fork_status"]
    if not pr.get("head_repo"):
        return "unknown"
    return "fork" if slug_key(pr["head_repo"]) != slug_key(_slug(view)) else "not_fork"


def review_state(view, number):
    """The PR's ONE review state (the view's `review_state.state`): none,
    requested, automated_only, commented, changes_requested, approved,
    approved_stale; None when the view does not derive it."""
    pr = next((x for x in view.get("prs") or [] if x.get("number") == number), None) or {}
    rs = pr.get("review_state")
    return rs.get("state") if isinstance(rs, dict) else None


def module_info(view, area):
    """What a module is: display name, description, resource type and
    lifecycle, from the module layer's row or the ownership fact's
    `modules_at_end` (the module's metadata file)."""
    out = {}
    for m in (view.get("module_changes") or {}).get("modules") or []:
        if m.get("area") == area:
            for k in ("display_name", "description", "resource_type"):
                if m.get(k):
                    out[k] = m[k]
            if m.get("module_lifecycle"):
                out["lifecycle"] = m["module_lifecycle"]
    end = ((view.get("module_ownership") or {}).get("modules_at_end") or {}).get(area) or {}
    for k in ("display_name", "description", "resource_type", "lifecycle"):
        if end.get(k) and k not in out:
            out[k] = end[k]
    return out


def _pr_net_files(view, number):
    """What a PR changed as a whole (`net_files` on the view's theme PR rows,
    else its `pr_history` row), or None."""
    for th in view.get("plumbing_themes") or []:
        for p in th.get("prs") or []:
            if isinstance(p, dict) and p.get("number") == number and p.get("net_files"):
                return p["net_files"]
    h = view.get("pr_history")
    rows = h.get("prs") if isinstance(h, dict) else h
    for r in rows or []:
        if isinstance(r, dict) and r.get("number") == number and r.get("net_files"):
            return r["net_files"]
    return None


def on_behalf_of(view, login, number):
    """The person an automation account worked for on one PR (the view's
    `automation.on_behalf_of_by_pr`), else None."""
    auto = ((view.get("people") or {}).get(login) or {}).get("automation")
    if not isinstance(auto, dict):
        return None
    row = (auto.get("on_behalf_of_by_pr") or {}).get(str(number))
    ob = auto.get("on_behalf_of")
    if row is None and isinstance(ob, dict) and ob.get("pr") == number:
        row = ob
    return row


def _pr_row(view, number, automation=(), full=False):
    """A PR for a reader: number, title, link, author (name, affiliation or
    automation), merged date, fork, description (as last gathered)."""
    pr = next((x for x in view.get("prs") or [] if x.get("number") == number), None) or {}
    out = {"number": number, "url": pr.get("url"), "title": pr.get("title"),
           "author": pr.get("author"), "state": pr.get("state"),
           "merged_at": pr.get("merged_at")}
    if pr.get("author"):
        if is_automation(view, pr["author"], automation):
            out["author_automation"] = True
            obo = on_behalf_of(view, pr["author"], number)
            if obo:
                out["author_on_behalf_of"] = obo
        else:
            person = _person(view, pr["author"])
            for k in ("name", "affiliation", "do_not_name"):
                if person.get(k) is not None:
                    out["author_" + k] = person[k]
    if pr.get("head_repo"):
        out["head_repo"] = pr["head_repo"]
    out["fork_status"] = fork_status(view, pr)
    out["is_fork"] = out["fork_status"] in ("fork", "deleted_fork")
    if pr.get("review_state"):
        out["review_state"] = pr["review_state"]
    if pr.get("closed_unmerged"):
        out["closed_unmerged"] = pr["closed_unmerged"]
    if pr.get("body_as_of"):
        out["body_as_of"] = pr["body_as_of"]
    net = _pr_net_files(view, number)
    if net:
        out["net_files"] = net
    desc = _pr_description(pr, full=full)
    if desc:
        out["description"] = desc
        out["description_as"] = "last gathered"
        if not full and len(desc) == PR_DESCRIPTION_CAP:
            out["description_cut"] = True
    if full and pr.get("body"):
        # the whole body as last gathered (template sections included)
        out["body"] = re.sub(r"<!--.*?-->", "", pr["body"].replace("\r\n", "\n"), flags=re.S)
    return out


def prompted_by(view, numbers):
    """The issues the given PRs close, with title, who asked (name /
    affiliation) and what they asked (the issue form's Description,
    verbatim, at most 600 characters)."""
    prs = {x.get("number"): x for x in view.get("prs") or []}
    issues = {x.get("number"): x for x in view.get("issues") or []}
    out, seen = [], set()
    for n in numbers:
        for i in (prs.get(n) or {}).get("closes") or []:
            if i in seen or i not in issues:
                continue
            seen.add(i)
            iss = issues[i]
            row = {"number": i, "title": iss.get("title"), "url": iss.get("url"),
                   "state": iss.get("state"), "author": iss.get("author"),
                   "created_at": iss.get("created_at")}
            person = _person(view, iss.get("author")) if iss.get("author") else {}
            for k in ("name", "affiliation"):
                if person.get(k) is not None:
                    row["author_" + k] = person[k]
            ask = link._issue_ask(iss.get("body"))[0]
            if ask:
                row["asked"] = ask[:600]
            if isinstance(iss.get("closing"), dict):
                row["closing"] = iss["closing"]
            out.append(row)
    return out


def _interface_detail(m):
    """The module's interface delta without its sources: parameters, outputs,
    resources and dependencies added / removed / modified (before / after)."""
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k != "sources"}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    return clean(m.get("interface") or {})


def _tests_added(view, numbers, area):
    """The test folders the given PRs' commits touched under a module
    (e.g. `tests/e2e/personal-multiple-desktops`), from the PR history."""
    out = set()
    for row in (view.get("pr_history") or {}).get("prs") or []:
        if row.get("number") not in numbers:
            continue
        for c in row.get("commits") or []:
            for path in c.get("paths") or []:
                if path.startswith(area + "/tests/"):
                    parts = path[len(area) + 1:].split("/")
                    out.add("/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts))
    return sorted(out)


def section_breaking(view, multi=False):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"declared": [], "candidates": [], "deprecations": [], "notices": [],
                "omit": _NO_MODULE_LAYER}
    repo = _slug(view)
    declared, candidates, deps, notices = [], [], [], []
    for m in mc.get("modules") or []:
        brk = m.get("breaking") or {}
        published = _published_set(m)
        texts = " ".join(d.get("text") or "" for d in brk.get("declared") or [])
        if brk.get("declared"):
            items = [{"version": d.get("version"), "text": d.get("text"),
                      "module": d.get("module"),
                      "published": _publication(view, published,
                                                d.get("module") or m["area"],
                                                d.get("version")),
                      "url": ((d.get("sources") or [{}])[0]).get("url")}
                     for d in brk["declared"]]
            declared.append(_with_repo({
                "area": m["area"], "kind": m.get("kind"), "url": module_url(view, m["area"])[0],
                "info": module_info(view, m["area"]),
                "release": module_release(view, m),
                "versions": sorted({i["version"] for i in items if i["version"]},
                                   key=_vkey),
                "items": items,
                "story": _module_story(view, [p.get("number") for p in m.get("prs") or []],
                                       multi),
            }, repo, multi))
        if brk.get("candidates"):
            candidates.append(_with_repo({
                "area": m["area"], "kind": m.get("kind"), "url": module_url(view, m["area"])[0],
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
    for e in ((view.get("module_ownership") or {}).get("events") or []):
        if not isinstance(e, dict) or e.get("kind") != "deprecated":
            continue
        target = e.get("module")
        row = next((d for d in deps if d.get("target") == target), None)
        if row is None:
            area = next((m["area"] for m in mc.get("modules") or []
                         if target and (target == m["area"] or target.startswith(m["area"] + "/"))),
                        target)
            row = _with_repo({"area": area, "target": target, "basis": [], "version": None,
                              "text": None, "url": (e.get("source") or {}).get("url")},
                             repo, multi)
            deps.append(row)
        if "lifecycle" not in row["basis"]:
            row["basis"].append("lifecycle")
        row["date"] = e.get("date") or (e.get("at") or "")[:10] or None
        if e.get("marker_text"):
            row["marker_text"] = e["marker_text"]
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
            "area": n["area"], "kind": n.get("kind"), "url": module_url(view, n["area"])[0],
            "release": module_release(view, m) if m else [{"status": "unknown"}],
            "first_version": n.get("first_version"),
            "published": _publication(view, _published_set(m), n["area"],
                                      n.get("first_version")),
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


def _version_row(m, view=None):
    v = m.get("versions") or {}
    vf = v.get("version_file") or {}
    cl = sorted(v.get("changelog") or [], key=_vkey)
    own = sorted(set(_own_published(m)), key=_vkey)
    row = {"from": vf.get("from"), "to": (cl[-1] if cl else None) or
           (own[-1] if own else None) or vf.get("to"),
           "changelog": cl, "published": own,
           # False: tags were not read for the whole period, so a version
           # missing from `published` may still have been published
           "publication_known": _tags_complete(view or {})}
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


def section_module_changes(view, caps, multi=False, automation=()):
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"modules": [], "also": [], "plumbing_only": [], "omit": _NO_MODULE_LAYER}
    repo = _slug(view)
    rows, plumbing_only = [], []
    for m in mc.get("modules") or []:
        if m.get("status") == "new":
            continue
        if m.get("plumbing_only"):
            plumbing_only.append(_qualified(repo, m["area"], multi))
            continue
        brk = m.get("breaking") or {}
        prs = [{"number": p.get("number"), "url": p.get("url"), "title": p.get("title"),
                "author": p.get("author")} for p in m.get("prs") or []]
        numbers = [p["number"] for p in prs]
        url, url_source = module_url(view, m["area"])
        row = {"area": m["area"], "kind": m.get("kind"), "status": m.get("status"),
               "url": url, "url_source": url_source, "info": module_info(view, m["area"]),
               # what the change means for a reader, why it came up, who did it
               "release": module_release(view, m),
               "prompted_by": prompted_by(view, numbers),
               "pr_details": [_pr_row(view, n, automation) for n in numbers],
               "tests_added": _tests_added(view, numbers, m["area"]),
               "interface_detail": _interface_detail(m),
               "score": m.get("product_score") or 0, "version": _version_row(m, view),
               "changelog": _changelog_items(m, cap=caps["changelog_items"]),
               "prs": prs,
               "breaking": "declared" if brk.get("declared") else
               ("candidate" if brk.get("candidates") else None),
               "story": _module_story(view, [p["number"] for p in prs], multi)}
        names = _interface_names(m)
        if names:
            row["interface"] = names
        # child versions published in the window, each with what it holds:
        # its own change (`contents`) or, when no change touched its folder,
        # republished with its parent
        children = sorted({p.get("module") for p in (m.get("versions") or {}).get("published") or []
                           if p.get("module") and p.get("module") != m["area"]})
        if children:
            row["child_releases"] = []
            for child in children:
                for r in module_release(view, m, child):
                    if r.get("version"):
                        c = r.get("contents") or {}
                        r = dict(r, own_change=bool(c.get("items") or c.get("commits")) if c
                                 else None, info=module_info(view, child))
                        if c and not r["own_change"]:
                            r["note"] = r.get("note") or "republished with its parent"
                        row["child_releases"].append(r)
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


def story_links(row):
    """The module folder and tag URLs a story may link beyond its slice (its
    outline row's `modules[].url` and `released[].url`). The newsletter check
    adds them to the slice as `newsletter_links` before the story check, and a
    work package writes them into the slice files it hands out, so the story
    check accepts exactly these links and nothing else new."""
    out = []
    for m in row.get("modules") or []:
        if m.get("url") and m["url"] not in out:
            out.append(m["url"])
    for r in row.get("released") or []:
        if r.get("url") and r["url"] not in out:
            out.append(r["url"])
    return out


def _provisional_group(view, train):
    """(group, state) of a train when the view has no `stories.all`: the
    plumbing group when every merged PR of it is plumbing; landed when it
    shipped; stalled when it is in flight with `effort.stalled`; in flight;
    closed (rejected / abandoned) otherwise."""
    outcome = train.get("outcome")
    if outcome == "shipped":
        state = "landed"
    elif outcome == "in_flight":
        state = "stalled" if (train.get("effort") or {}).get("stalled") else "in_flight"
    else:
        state = "closed"
    plumbing = bool(view.get("module_changes")) and \
        link._train_plumbing(train, view.get("module_changes"))
    return ("plumbing" if plumbing else state), state


_ISSUE_MODULE_RE = re.compile(r"###\s*Module Name\s*\n+\s*`?((?:avm/)?[\w./-]+)`?")


def _train_areas(view, train):
    """The module areas a train touches: its code areas that are modules, else
    the module an issue form of its root issue names (`### Module Name`) — the
    only module evidence for a train whose PRs are open."""
    areas = [a for a in train.get("code_areas") or [] if link.module_kind(a) is not None]
    if areas:
        return areas
    issues = {i.get("number"): i for i in view.get("issues") or []}
    for n in [train.get("root_issue")] + list(train.get("issues") or []):
        m = _ISSUE_MODULE_RE.search((issues.get(n) or {}).get("body") or "")
        if m and m.group(1).startswith("avm/"):
            return [m.group(1).rstrip("/.")]
    return []


def _module_refs(view, areas):
    return [{"area": a, "url": module_url(view, a)[0]} for a in areas]


def _discussed(train):
    """A real discussion in the window: a selected story with a turn in the
    window, or at least STORY_MIN_EXCHANGES human turns in the window
    between at least two people (a discussion carried over is not one)."""
    d = train.get("discussion") or {}
    in_window = d.get("in_window") or 0
    if train.get("story") and in_window >= link.STORY_MIN_IN_WINDOW:
        return True
    return in_window >= link.STORY_MIN_EXCHANGES and (d.get("participants") or 0) >= 2


def story_plan(group, worthy, train, provisional=False):
    """(plan, reason) of one story by the owner's rule: `full` for every
    train landed, in flight or closed, and every stalled train with a human
    turn or an event in the window; `line` for a stalled train merely carried
    over; plumbing `full` under its theme when it had a real discussion, else
    `in_theme` (named in the theme's narrative)."""
    if group in ("landed", "in_flight", "closed"):
        return "full", "{}: every story of the group in full".format(group)
    if group == "stalled":
        if provisional or (worthy and worthy != "carried over"):
            return "full", "stalled, with {} in the window".format(worthy or "activity")
        return "line", "stalled and carried over: no turn in the window"
    if group == "plumbing":
        if _discussed(train):
            return "full", "plumbing with a real discussion: in full under its theme"
        return "in_theme", "plumbing without a real discussion: named in its theme"
    return "full", "ungrouped"


def automation_only(view, train, automation=()):
    """True when every PR of a train is authored by an automation account and
    the train has no human conversation turn (it is told in a plumbing theme,
    whatever group the view gives it)."""
    prs = {p.get("number"): p for p in view.get("prs") or []}
    numbers = train.get("prs") or []
    if not numbers or not all(is_automation(view, (prs.get(n) or {}).get("author"), automation)
                              for n in numbers):
        return False
    d = train.get("discussion") or {}
    turns = (d.get("comments") or 0) + (d.get("review_comments") or 0) + \
        sum((d.get("reviews") or {}).values())
    return turns == 0


def _theme_by_author(view, train):
    """The plumbing theme an automation-only train belongs with: the view's
    theme whose PRs share its PRs' author, if exactly one does."""
    prs = {p.get("number"): p for p in view.get("prs") or []}
    authors = {(prs.get(n) or {}).get("author") for n in train.get("prs") or []}
    hits = [th.get("id") for th in view.get("plumbing_themes") or []
            if authors & {(prs.get(x.get("number")) or {}).get("author")
                          for x in th.get("prs") or []}]
    return hits[0] if len(hits) == 1 else None


def _theme_index(view, multi):
    """{train id: theme id} from the view's plumbing_themes (else the
    provisional themes by PR class)."""
    out = {}
    if isinstance(view.get("plumbing_themes"), list):
        for th in view["plumbing_themes"]:
            for tid in th.get("stories") or []:
                out.setdefault(tid, th.get("id"))
        return out
    mc = view.get("module_changes") or {}
    rows = {r.get("number"): r for r in mc.get("prs") or []}
    for t in view.get("trains") or []:
        for n in t.get("prs") or []:
            if n in rows and rows[n].get("plumbing"):
                out.setdefault(t["id"], _provisional_theme(rows[n]))
    return out


STORY_KINDS = ("new", "changed", "breaking")

# The relations a story can state as a dependency (from its own side), and
# their inverses; `mentions` is never one. `waiting` relations can be "still
# waiting" while both ends are open.
DEPENDENCY_RELATIONS = ("blocked_by", "depends_on", "stacked_on", "follow_up_of", "caused_by",
                        "content_landed_in")
INVERSE_RELATIONS = {"blocks": "blocked_by", "needed_by": "depends_on",
                     "stacked_under": "stacked_on", "followed_by": "follow_up_of",
                     "causes": "caused_by", "content_from": "content_landed_in"}
WAITING_RELATIONS = ("blocked_by", "depends_on", "stacked_on")
PLUMBING_KINDS = ("plumbing", "ci_workflows", "pipeline_utilities", "test_environment",
                  "docs_generation", "dependency_updates", "repository_policy",
                  "other_plumbing")


def _story_ref(key, rows_by_key, multi, trains=None):
    """{key, title, group, state, url, in_issue} of a story key of the
    relation layer (`owner/repo:train-id`), in the outline's own key form; a
    train the issue does not tell (carried over past the idle limit) is
    named by its root item, `in_issue: false`."""
    local = key if multi or not isinstance(key, str) else key.split(":", 1)[-1]
    row = rows_by_key.get(local)
    if row:
        return {"key": local, "title": row.get("title"), "group": row.get("group"),
                "state": row.get("state"), "url": row.get("url"), "in_issue": True}
    t = (trains or {}).get(key) or (trains or {}).get(local) or {}
    return {"key": local, "title": t.get("title"), "group": None, "state": t.get("state"),
            "url": t.get("url"), "in_issue": False}


def connect_stories(views, stories_sec, plumbing_sec, multi=False):
    """How the work connects, from the view's relation layer (`story_graph`,
    `stories.all[].relations`): every story row gains `relations` (its
    non-mention relations, each with the quote, the item, and the other
    story's title, group, state and link, or the external item), a module
    story `waited_on_plumbing`, every plumbing theme `needed_by` / `caused`
    (the module stories that waited on it or that it caused), and the
    stories section a `connections` part (chains, the items most stories
    wait on, external targets, counts per relation)."""
    rows_by_key = {r["key"]: r for r in stories_sec.get("all") or []}
    trains = {}
    for v in views:
        items = {("pr", p.get("number")): p for p in v.get("prs") or []}
        items.update({("issue", i.get("number")): i for i in v.get("issues") or []})
        for t in v.get("trains") or []:
            root = items.get(("issue", t.get("root_issue"))) if t.get("root_issue") else None
            root = root or items.get(("pr", (t.get("prs") or [None])[0])) or \
                items.get(("issue", (t.get("issues") or [None])[0])) or {}
            trains["{}:{}".format(_slug(v), t["id"])] = {
                "title": root.get("title"), "url": root.get("url"),
                "state": "merged" if root.get("merged") else root.get("state")}
    theme_of = {}
    for th in plumbing_sec.get("themes") or []:
        for k in th.get("stories") or []:
            theme_of[k] = th.get("id")
    theme_ids = {th.get("id") for v in views for th in v.get("plumbing_themes") or []} - {None}
    view_rows = {}
    for v in views:
        for e in (v.get("stories") or {}).get("all") or []:
            view_rows[story_key(v, e["id"], multi)] = e
    for key, row in rows_by_key.items():
        rels = []
        for rel in (view_rows.get(key) or {}).get("relations") or []:
            kind = rel.get("relation")
            if kind in ("mentions", "mentioned_by"):
                continue
            item = {"relation": kind, "basis": {k: (rel.get("basis") or {}).get(k)
                                                for k in ("quote", "item", "author", "at", "url",
                                                          "kind", "inverse_of", "subject",
                                                          "task_done", "text_current")
                                                if (rel.get("basis") or {}).get(k) is not None},
                    "other_kind": rel.get("other_kind"), "other_item": rel.get("other_item"),
                    "other_state": rel.get("other_state"), "waiting": rel.get("waiting"),
                    "as_of": rel.get("as_of")}
            if rel.get("external") or isinstance(rel.get("other"), dict):
                item["external"] = rel.get("other") if isinstance(rel.get("other"), dict) else True
                item["other_state"] = None      # another repository: no state claimed
            else:
                item["other"] = _story_ref(rel.get("other"), rows_by_key, multi, trains)
                # another member's story: link it within the issue
                own = key.split(":", 1)[0] if multi else None
                if rel.get("cross_member") or (own and isinstance(rel.get("other"), str)
                                               and rel["other"].split(":", 1)[0] != own):
                    item["cross_member"] = True
            rels.append(item)
        inner_rows = [x for x in (view_rows.get(key) or {}).get("inner_relations") or []
                      if x.get("relation") != "mentions"]
        if inner_rows:
            row["inner_relations"] = inner_rows
        if rels:
            row["relations"] = rels
            # a plumbing target: a plumbing kind or theme id, or a plumbing story
            waited = [x for x in rels if x["relation"] in WAITING_RELATIONS
                      and (x.get("other_kind") in PLUMBING_KINDS or x.get("other_kind") in theme_ids
                           or (x.get("other") or {}).get("group") == "plumbing")]
            if waited and row.get("group") != "plumbing":
                row["waited_on_plumbing"] = waited
    # plumbing themes: the module stories they were needed by, or that they caused
    for th in plumbing_sec.get("themes") or []:
        mine = set(th.get("stories") or [])
        needed, caused = [], []
        for key, row in rows_by_key.items():
            if row.get("group") == "plumbing" or key in mine:
                continue
            for x in row.get("relations") or []:
                other = (x.get("other") or {}).get("key")
                if other in mine and x["relation"] in WAITING_RELATIONS:
                    needed.append({"story": key, "title": row.get("title"), "url": row.get("url"),
                                   "relation": x["relation"], "via": other,
                                   "quote": x["basis"].get("quote")})
                if other in mine and x["relation"] == "caused_by":
                    caused.append({"story": key, "title": row.get("title"), "url": row.get("url"),
                                   "via": other, "quote": x["basis"].get("quote")})
        if needed:
            th["needed_by"] = needed
        if caused:
            th["caused"] = caused
    # the connections part
    # one graph: a single view's own, or the stream's (link.stream_story_graph)
    # so a chain or a most-blocking count across members is computed over the
    # whole stream, not member by member
    chains, most, external, counts, capped = [], [], [], {}, False
    if len(views) > 1:
        graphs = [link.stream_story_graph(
            [{"repo": _slug(v), "bundle": v} for v in join_members(views)])]
    else:
        graphs = [v.get("story_graph") or {} for v in views]
    for g in graphs:
        capped = capped or bool(g.get("chains_capped"))
        for ch in g.get("chains") or []:
            chains.append([_story_ref(k, rows_by_key, multi, trains) for k in ch])
        for m in g.get("most_blocking") or []:
            most.append(dict(_story_ref(m.get("story"), rows_by_key, multi, trains),
                             waiting_stories=m.get("waiting_stories"),
                             stories=[_story_ref(k, rows_by_key, multi, trains)
                                      for k in m.get("stories") or []]))
        for e in g.get("edges") or []:
            counts[e.get("relation")] = counts.get(e.get("relation"), 0) + 1
            if e.get("external") and e.get("relation") != "mentions":
                b_ = e.get("basis") or {}
                external.append({"from": _story_ref(e.get("from"), rows_by_key, multi, trains),
                                 "relation": e.get("relation"), "target": e.get("to"),
                                 "quote": b_.get("quote"),
                                 **{k: b_[k] for k in ("task_done", "text_current") if k in b_}})
    stories_sec["connections"] = {
        "chains": chains, "most_blocking": [m for m in most if (m.get("waiting_stories") or 0) > 1],
        "external": external, "counts": counts,
        # the data side's bounded walk stopped: say "chains were capped"
        "chains_capped": capped,
        # omitted only when the record holds no relation but bare mentions
        "omit": None if any(k != "mentions" for k in counts) else
        "no relation between stories"}


def section_stories(views, caps, multi=False):
    """Every story of the window, grouped by state (STORY_GROUPS), with the
    story plan (story_plan): `full` (written in full), `line` (one line:
    module, state, days idle, what it waits on), `in_theme` (plumbing named
    in its theme). `stories.all` when the view has it, else the selected and
    further stories grouped provisionally (`group_source: provisional`) and
    `stories.quiet` as lines."""
    rows, source = [], "view"
    excluded = []
    for v in views:
        trains = {t["id"]: t for t in v.get("trains") or []}
        st = v.get("stories") or {}
        alls = st.get("all")
        themes = _theme_index(v, multi)
        if not isinstance(alls, list):
            source = "provisional"
            entries = [dict(x) for x in (st.get("selected") or []) + (st.get("more") or [])]
            entries += [dict(x, _quiet=True) for x in st.get("quiet") or []
                        if x.get("id") not in {e.get("id") for e in entries}]
        else:
            entries = [dict(x) for x in alls]
            excluded += [_with_repo(dict(x), _slug(v), multi) for x in st.get("excluded") or []]
        for e in entries:
            t = trains.get(e.get("id"), {})
            if isinstance(alls, list):
                group, state = e.get("group"), e.get("state")
                rule = e.get("group_rule")
            else:
                group, state = _provisional_group(v, t)
                rule = "provisional: train outcome, effort.stalled, plumbing PRs"
            if e.get("_quiet"):
                plan, reason = "line", "no conversation turn in the window"
                if group != "plumbing":
                    group, state = "stalled", "stalled"
            else:
                plan, reason = story_plan(group, e.get("worthy"), t,
                                          provisional=not isinstance(alls, list))
                if group != "plumbing" and automation_only(v, t):
                    plan, reason = "in_theme", ("automation only (every PR by an automation "
                                                "account, no human turn): told in its "
                                                "plumbing theme")
            title, url = _train_title(v, t)
            key = story_key(v, e["id"], multi)
            sfile = slice_filename(key)
            view_file = "VIEW" if not multi else \
                "workspace/view/{}.json".format(_slug(v).replace("/", "__"))
            # only module areas are modules (a view's story row can list CI
            # folders); none left: the train's own module evidence
            areas = [a for a in e.get("modules") or [] if link.module_kind(a) is not None] \
                or _train_areas(v, t)
            released = e.get("released")
            if released is None:
                released = []
                for n in t.get("prs") or []:
                    pr = next((x for x in v.get("prs") or [] if x.get("number") == n), {})
                    if pr.get("merged"):
                        released += [r for r in pr_release(v, n) if r not in released]
                if any(r.get("module") for r in released):
                    released = [r for r in released if r.get("module")]
            row = {
                "id": e["id"], "key": key, "group": group, "state": state,
                "group_rule": rule, "score": e.get("score"), "worthy": e.get("worthy"),
                "plan": plan, "plan_reason": reason,
                "title": e.get("title") if e.get("title") and e.get("title") != title
                and len(e.get("title")) > len(title or "") else title or e.get("title"),
                "url": url, "prs": t.get("prs") or [], "issues": t.get("issues") or [],
                "modules": _module_refs(v, areas), "released": released,
                "idle_days": (t.get("effort") or {}).get("idle_days", e.get("idle_days")),
                "slice": "python3 link.py {} --slice {} > {}".format(view_file, e["id"], sfile),
                "slice_file": sfile,
                "check": "python3 link.py {} --check-story {}".format(
                    sfile, sfile.replace(".slice.json", ".story.md")),
            }
            # what the view says beyond the group: what each module is, why the
            # story is in its state, the proposed new module, related stories
            for k in ("module_info", "modules_source", "status_reason", "substance",
                      "new_module_proposed", "related_stories"):
                if e.get(k):
                    row[k] = e[k]
            if t.get("joined_by"):
                row["joined_by"] = t["joined_by"]
            row["review_states"] = {str(n): review_state(v, n) for n in t.get("prs") or []
                                    if review_state(v, n)}
            for m in row["modules"]:
                info = (e.get("module_info") or {}).get(m["area"]) or module_info(v, m["area"])
                if info:
                    m["info"] = info
            if group == "plumbing" or plan == "in_theme":
                row["theme"] = themes.get(e["id"]) or _theme_by_author(v, t)
            if plan == "line":
                try:
                    oc = link.slice_train(v, e["id"])["conversation"]["outcome"]
                    ball = oc.get("ball") or {}
                    row["waits_on"] = {"state": ball.get("state"), "derived": True}
                    if oc.get("waiting_on"):
                        row["waits_on"]["items"] = [
                            {k: w.get(k) for k in ("number", "type", "state", "title")}
                            for w in oc["waiting_on"]]
                except (KeyError, TypeError):
                    row["waits_on"] = None
            rows.append(_with_repo(row, _slug(v), multi))
    # within a group, the owner's order: new modules, then changes, then
    # breaking changes (a story about a module with a declared breaking change)
    new_areas, breaking_areas = set(), set()
    for v in views:
        summ = (v.get("module_changes") or {}).get("summary") or {}
        new_areas |= {(_slug(v), x.get("area")) for x in summ.get("new_modules") or []}
        breaking_areas |= {(_slug(v), x.get("area")) for x in summ.get("declared_breaking") or []}
    for r in rows:
        sl = r.get("repo") or (_slug(views[0]) if views else None)
        areas = {(sl, m.get("area")) for m in r.get("modules") or []}
        r["story_kind"] = "new" if r.get("new_module_proposed") or areas & new_areas else \
            ("breaking" if areas & breaking_areas else "changed")
    order = {g: i for i, g in enumerate(STORY_GROUPS)}
    korder = {k: i for i, k in enumerate(STORY_KINDS)}
    rows.sort(key=lambda r: (order.get(r["group"], len(order)), korder[r["story_kind"]],
                             -(r["score"] or 0), r["key"]))
    groups = {g: [r for r in rows if r["group"] == g] for g in STORY_GROUPS}
    plan_counts = {}
    for r in rows:
        plan_counts.setdefault(r["group"], {}).setdefault(r["plan"], 0)
        plan_counts[r["group"]][r["plan"]] += 1
    out = {"full": [r for r in rows if r["plan"] == "full"],
           "lines": [r for r in rows if r["plan"] == "line"],
           "in_theme": [r for r in rows if r["plan"] == "in_theme"],
           "all": rows, "groups": groups, "group_source": source,
           "story_plan": [{k: r.get(k) for k in ("key", "group", "state", "plan",
                                                 "plan_reason", "theme", "repo") if k in r}
                          for r in rows],
           "plan_counts": plan_counts, "excluded": excluded, "omit": None}
    if not rows:
        out["omit"] = "no train qualified as a story in the window (stories is absent)"
    return out


def contribution_class(row):
    """What a contributors row (newcomers / hall) contributed, best first
    (CONTRIBUTION_ORDER): from the module layer's PR headline of their merged
    PRs, `docs` for a docs-only plumbing PR, `unknown` for a merged PR whose
    commits the view does not hold (no evidence of what it changed: never
    read as plumbing); `open_pr` with only open PRs; `issue` with neither."""
    best = None
    for p in (row.get("contributed") or {}).get("prs") or []:
        if p.get("state") == "merged":
            h = p.get("headline")
            if h == "plumbing" and p.get("class") == "docs-only":
                h = "docs"
            c = h if h in CONTRIBUTION_ORDER else "unknown"
        else:
            c = "open_pr"
        if best is None or CONTRIBUTION_ORDER.index(c) < CONTRIBUTION_ORDER.index(best):
            best = c
    if best:
        return best
    # no merged or open PR listed: an issue reporter, or a PR closed unmerged /
    # not in the view — the latter is not established, so unknown
    first = (row.get("first") or {}).get("first")
    return "issue" if first == "issue" else "unknown"


def _merged_by_class(view, login):
    """{contribution class: count} of the PRs a person authored that were
    merged into the mainline in the window, by the module layer's PR row:
    new_module (it introduced a new module), release, docs (docs-only),
    plumbing, module_change (any other source change)."""
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return None             # no module layer: not known, not "nothing"
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


def _pr_brief(p, view=None):
    out = {"number": p.get("number"), "url": p.get("url"), "title": p.get("title")}
    mods = [m.get("area") for m in p.get("modules") or [] if m.get("area")]
    if mods:
        out["modules"] = mods
        if view is not None:
            out["module_urls"] = {a: module_url(view, a)[0] for a in mods}
    if view is not None and p.get("state") == "merged":
        out["release"] = pr_release(view, p.get("number"))
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


def _automation_rows(view, automation=(), multi=False):
    """The automation accounts active in the view, for what they are: each
    with its counts and the PRs it authored (title, state, link)."""
    out = []
    for login, row in sorted((view.get("people") or {}).items()):
        if not is_automation(view, login, automation):
            continue
        counts = {k: row.get(k) for k in ("prs_authored", "prs_merged", "prs_reviewed",
                                          "commits_authored", "issues_opened")
                  if isinstance(row.get(k), int) and row.get(k)}
        prs = [_pr_row(view, x["number"], automation) for x in view.get("prs") or []
               if x.get("author") == login]
        if not counts and not prs:
            continue
        auto = row.get("automation") if isinstance(row.get("automation"), dict) else {}
        entry = {"login": login, "automation": True, "counts": counts,
                 "prs": [{k: r.get(k) for k in ("number", "title", "url", "state", "merged_at",
                                                "review_state", "closed_unmerged")
                          if r.get(k) is not None or k in ("number", "state", "merged_at")}
                         for r in prs]}
        # what kind of automation, and for whom it acted (the view's marking)
        if auto.get("kind"):
            entry["kind"] = auto["kind"]
        if auto.get("on_behalf_of"):
            entry["on_behalf_of"] = auto["on_behalf_of"]
        if auto.get("on_behalf_of_by_pr"):
            entry["on_behalf_of_by_pr"] = auto["on_behalf_of_by_pr"]
        if auto.get("same_identity_as"):
            # one automated reviewer seen under two accounts: say it once
            entry["same_identity_as"] = auto["same_identity_as"]
        out.append(_with_repo(entry, _slug(view), multi))
    return out


def section_people(views, caps, multi=False, automation=()):
    newcomers, maint, top, not_named = [], [], [], 0
    bots = []
    gaps = {}
    affs = []
    community = []
    for v in views:
        repo = _slug(v)
        c = v.get("contributors") or {}
        hidden = _do_not_name(v)
        bots += _automation_rows(v, automation, multi)
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
                   "merged": [_pr_brief(p, v) for p in prs if p.get("state") == "merged"],
                   "open": [_pr_brief(p, v) for p in prs if p.get("state") != "merged"]}
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
                   "reviews": ((v.get("people") or {}).get(login) or {}).get("prs_reviewed"),
                   "merged_others": others, "merged_own": own,
                   "authored_by_class": _merged_by_class(v, login)}
            row.update({k: v2 for k, v2 in _person(v, login).items() if k != "login"})
            reg = _registry(v, login)
            if reg:
                row["registry"] = reg
            maint.append(_with_repo(row, repo, multi))
        for h in c.get("hall") or []:
            if h.get("login") in hidden or is_automation(v, h.get("login"), automation):
                continue
            row = {"login": h["login"], "score": h.get("score"),
                   "merged_by_class": _merged_by_class(v, h["login"]),
                   "reviewed": (h.get("reviewed") or {}).get("prs", 0)}
            row.update({k: v2 for k, v2 in _person(v, h["login"]).items() if k != "login"})
            top.append(_with_repo(row, repo, multi))
    newcomers.sort(key=lambda r: (CONTRIBUTION_ORDER.index(r["contribution"]),
                                  r["login"].lower(), r.get("repo") or ""))
    maint.sort(key=lambda r: (-((r["reviews"] or 0) + r["merged_others"]),
                              r["login"].lower()))
    top.sort(key=lambda r: (-(r["score"] or 0), r["login"].lower()))
    out = {"newcomers": newcomers[:caps["newcomers"]],
           "newcomers_omitted": max(0, len(newcomers) - caps["newcomers"]),
           "not_named": not_named, "returning_by_gap": gaps,
           "gap_months": [3, 6, 9],
           "affiliation": affs[0] if len(affs) == 1 else affs,
           "community": community[0] if len(community) == 1 else community,
           "maintainers": maint[:caps["maintainers"]], "top": top[:caps["top"]],
           "automation": bots, "omit": None}
    if not newcomers and not affs and not top and not bots:
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
    groups = {"ready": [], "approval_stale": [], "failing_checks": [],
              "needs_first_review": [], "awaiting_review": [], "other": {}}
    # the view's story rows name a train's modules (from its commits) when the
    # train itself records no code areas
    story_modules = {r.get("id"): r.get("modules") for r in
                     (view.get("stories") or {}).get("all") or [] if r.get("modules")}
    story_groups = {r.get("id"): r.get("group") for r in
                    (view.get("stories") or {}).get("all") or [] if r.get("group") != "plumbing"}
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
                   "modules": [a for a in (story_modules.get(t["id"]) or t.get("code_areas") or [])
                               if link.module_kind(a) is not None][:3]}
            row["module_urls"] = {a: module_url(view, a)[0] for a in row["modules"]}
            if t.get("story") or t["id"] in story_groups:
                row["story"] = story_key(view, t["id"], multi)
                if story_groups.get(t["id"]):
                    row["story_group"] = story_groups[t["id"]]
            row["review_state"] = review_state(view, it.get("number"))
            row = _with_repo(row, repo, multi)
            verdicts = (it.get("reviews") or {}).get("verdicts") or []
            rs = row["review_state"]
            # the review state is the one source for review wording; the
            # verdicts only where the view derives none
            row["approved"] = rs == "approved" if rs else (
                any(x.get("state") == "approved" for x in verdicts) and
                not row["outstanding_change_requests"])
            st = ball.get("state")
            # the derived next move decides, and only a current approval is
            # ready: an approval followed by the author's change is not
            if st == "awaiting merge" and rs in ("approved", None):
                groups["ready"].append(row)
            elif st == "awaiting merge" and rs == "approved_stale":
                groups["approval_stale"].append(row)
            elif ball.get("rule") == "R5 failing checks":
                groups["failing_checks"].append(row)
            elif st in ("awaiting a first review", "awaiting reviewer"):
                first = rs in _NOT_REVIEWED if rs else st == "awaiting a first review"
                groups["needs_first_review" if first else "awaiting_review"].append(row)
            else:
                groups["other"][st] = groups["other"].get(st, 0) + 1
    fc = view.get("forecast") or {}
    groups["forecast"] = [_with_repo({"number": (c.get("ref") or {}).get("id"),
                                      "url": (c.get("ref") or {}).get("url"),
                                      "tier": c.get("tier"), "signals": c.get("signals")},
                                     repo, multi)
                          for c in fc.get("candidates") or [] if c.get("tier") == "likely"]
    groups["quiet"] = len((view.get("stories") or {}).get("quiet") or [])
    groups["proposed_modules"] = proposed_modules(view, multi)
    return groups


def proposed_modules(view, multi=False):
    """The new modules open PRs propose: the module areas whose entry file
    the train's PRs add (`stories.all[].new_module_proposed`), each with the
    train's open PRs, their review state, the story's group and what the
    module is when its metadata says."""
    prs = {p.get("number"): p for p in view.get("prs") or []}
    trains = {t["id"]: t for t in view.get("trains") or []}
    tree = ((view.get("module_ownership") or {}).get("proposed_modules") or {})
    out = []
    seen = set()
    for r in (view.get("stories") or {}).get("all") or []:
        areas = r.get("new_module_proposed") or r.get("proposes_new_module") or []
        for area in areas:
            t = trains.get(r["id"], {})
            open_prs = [n for n in t.get("prs") or [] if (prs.get(n) or {}).get("state") == "open"]
            row = {"module": area, "story": story_key(view, r["id"], multi),
                   "group": r.get("group"), "state": r.get("state"),
                   "prs": [{"number": n, "url": (prs.get(n) or {}).get("url"),
                            "title": (prs.get(n) or {}).get("title"),
                            "review_state": review_state(view, n),
                            "fork_status": fork_status(view, prs.get(n) or {})}
                           for n in open_prs]}
            info = dict((r.get("module_info") or {}).get(area) or {}) or \
                dict((tree.get(area) or {}).get("info") or {}) or module_info(view, area)
            if info:
                row["info"] = info
            seen.add(area)
            out.append(_with_repo(row, _slug(view), multi))
    # the tree's `proposed` lifecycle: an open PR's own commits add the module's
    # entry file (a train the stories carry over may hold it)
    for area, t_row in sorted(tree.items()):
        if area in seen or not isinstance(t_row, dict):
            continue
        open_prs = [n for n in t_row.get("prs") or [] if (prs.get(n) or {}).get("state") == "open"]
        story = next((r for r in (view.get("stories") or {}).get("all") or []
                      if set(open_prs) & set((trains.get(r["id"]) or {}).get("prs") or [])), None)
        row = {"module": area, "story": story_key(view, story["id"], multi) if story else None,
               "group": (story or {}).get("group"), "state": (story or {}).get("state"),
               "plan_source": "module_ownership.proposed_modules",
               "prs": [{"number": n, "url": (prs.get(n) or {}).get("url"),
                        "title": (prs.get(n) or {}).get("title"),
                        "review_state": review_state(view, n),
                        "fork_status": fork_status(view, prs.get(n) or {})} for n in open_prs]}
        if t_row.get("info"):
            row["info"] = t_row["info"]
        out.append(_with_repo(row, _slug(view), multi))
    return out


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


_PROVISIONAL_THEMES = (
    ("sweeps", "Repository-wide sweeps"),
    ("ci", "CI workflows and pipeline utilities"),
    ("tests", "Tests and test fixtures"),
    ("docs", "Documentation"),
    ("mixed", "CI changes made together with module files"),
)


def _provisional_theme(row):
    if row.get("sweep"):
        return "sweeps"
    return {"ci": "ci", "tests-only": "tests", "docs-only": "docs"}.get(
        row.get("class"), "mixed")


def section_plumbing(view, caps, multi=False, automation=()):
    """Plumbing as what was engineered: the view's `plumbing_themes` when it
    has them, else themes by the module layer's PR class (`theme_source:
    provisional`). Per theme: its PRs (title, author, merge date, link,
    description as last gathered), the plumbing stories among its trains,
    the people; then the counts."""
    mc = view.get("module_changes")
    if not isinstance(mc, dict):
        return {"themes": [], "prs": [], "omit": _NO_MODULE_LAYER}
    pl = (mc.get("summary") or {}).get("plumbing") or {}
    rows = [r for r in mc.get("prs") or [] if r.get("plumbing")]
    rows.sort(key=lambda r: (-len(r.get("areas") or []), r["number"]))
    trains = {t["id"]: t for t in view.get("trains") or []}
    plumbing_stories = {}
    for t in trains.values():
        if t.get("story") and _provisional_group(view, t)[0] == "plumbing":
            for n in t.get("prs") or []:
                plumbing_stories.setdefault(n, []).append(story_key(view, t["id"], multi))

    def people_of(numbers):
        out = []
        for n in numbers:
            pr = next((x for x in view.get("prs") or [] if x.get("number") == n), {})
            a = pr.get("author")
            if a and a not in [x["login"] for x in out]:
                if is_automation(view, a, automation):
                    out.append({"login": a, "automation": True})
                else:
                    out.append(dict(_person(view, a)))
        return out

    themes = []
    if isinstance(view.get("plumbing_themes"), list):
        for th in view["plumbing_themes"]:
            numbers = [x.get("number") for x in th.get("prs") or []]
            themes.append(_with_repo({
                "id": th.get("id"), "title_hint": th.get("title_hint"),
                "theme_source": "view", "paths": th.get("paths") or [],
                "prs": [_pr_row(view, n, automation) for n in numbers],
                "stories": [story_key(view, x, multi) for x in th.get("stories") or []],
                "people": people_of(numbers),
                "counts": _theme_counts(view, numbers),
                "subjects": th.get("subjects"), "issues": th.get("issues"),
                "paths_more": th.get("paths_more"),
                "evidence_counts": th.get("evidence_counts")}, _slug(view), multi))
    else:
        by = {}
        for r in rows:
            by.setdefault(_provisional_theme(r), []).append(r["number"])
        for tid, hint in _PROVISIONAL_THEMES:
            numbers = by.get(tid)
            if not numbers:
                continue
            keys = []
            for n in numbers:
                keys += [k for k in plumbing_stories.get(n, []) if k not in keys]
            themes.append(_with_repo({
                "id": tid, "title_hint": hint, "theme_source": "provisional",
                "prs": [_pr_row(view, n, automation) for n in numbers][:caps["plumbing_prs"]],
                "stories": keys, "people": people_of(numbers)}, _slug(view), multi))
    out = {"merged_prs": pl.get("merged_prs"), "plumbing_prs": pl.get("plumbing_prs"),
           "classified": pl.get("classified"), "share": pl.get("share"),
           "by_class": pl.get("by_class") or {}, "themes": themes,
           "prs": [_with_repo({"number": r["number"], "url": r.get("url"),
                               "title": r.get("title"), "class": r.get("class"),
                               "areas": len(r.get("areas") or []),
                               "sweep": bool(r.get("sweep"))}, _slug(view), multi)
                   for r in rows],
           "omit": None}
    if not rows and not themes:
        out["omit"] = "no merged pull request was plumbing in the window"
    return out


def _theme_counts(view, numbers):
    """How a theme's PRs ended as of the window's end: merged, closed without
    merging, open; how many each author opened and how many of each author's
    were merged."""
    prs = {p.get("number"): p for p in view.get("prs") or []}
    out = {"prs": len(numbers), "merged": 0, "closed_unmerged": 0, "open": 0, "by_author": {},
           "merged_by_author": {}}
    for n in numbers:
        p = prs.get(n) or {}
        if p.get("merged"):
            out["merged"] += 1
            if p.get("author"):
                # so a person's merged total reconciles with the themes
                out["merged_by_author"][p["author"]] = \
                    out["merged_by_author"].get(p["author"], 0) + 1
        elif p.get("state") == "closed":
            out["closed_unmerged"] += 1
        elif p.get("state") == "open":
            out["open"] += 1
        if p.get("author"):
            out["by_author"][p["author"]] = out["by_author"].get(p["author"], 0) + 1
    return out


OWNERSHIP_KINDS = ("owner_needed", "owner_response_requested", "owner_added",
                   "owner_removed", "orphaned", "adopted")


LIFECYCLE_EVENT_KINDS = ("deprecated", "undeprecated", "module_added", "module_removed",
                         "module_info_changed")


def _start_without_owner(mo):
    """The modules without an owner at the window's start, from
    `start_state` (a live or orphaned module whose owner file lists nobody or
    that carries an orphan marker); a legacy fact's `start_without_owner`."""
    if isinstance(mo.get("start_state"), dict):
        out = []
        for module, row in sorted(mo["start_state"].items()):
            if not isinstance(row, dict) or row.get("deprecated"):
                continue
            if row.get("orphan_marker") or (row.get("owner_file") and not row.get("owners")
                                             and row.get("entry")):
                out.append({"module": module,
                            "marker": row.get("orphan_marker") or row.get("owner_file")})
        return out
    return mo.get("start_without_owner") or []


def _without_owner_by_kind(rows):
    """{empty_owner_list | orphan_marker: [module (repo-qualified in a stream)]}."""
    by_kind = {}
    for w in rows:
        k = "orphan_marker" if w.get("lifecycle") == "orphaned" or \
            (w.get("marker") or "").upper().startswith("ORPHAN") else "empty_owner_list"
        by_kind.setdefault(k, []).append(
            "{}:{}".format(w["repo"], w["module"]) if w.get("repo") else w["module"])
    return by_kind


def ownership_modules(mo):
    """The modules the ownership fact says something about: its events, the
    modules without an owner (now and at the start), the deprecated modules,
    the stub folders and the labelled items."""
    if not isinstance(mo, dict):
        return set()
    out = {e.get("module") for k in ("events", "without_owner", "labelled_items",
                                     "deprecated_modules", "stub_modules")
           for e in mo.get(k) or [] if isinstance(e, dict) and e.get("module")}
    out |= {e["module"] for e in _start_without_owner(mo) if isinstance(e, dict)
            and e.get("module")}
    return out


def section_ownership(view, multi=False):
    """Module ownership from the view's `module_ownership`: events (owner
    needed / added / removed, orphaned, adopted, owner response requested)
    with module link, people, date and source; the module lifecycle events
    (deprecated, added, removed, ...); the standing list of LIVE and
    orphaned modules without an owner at the window's end (with what each
    module is), the deprecated modules and the count of stub folders
    apart. When the state cannot be known (`without_owner_unavailable`) the
    section says so and never says "no module without an owner"."""
    mo = view.get("module_ownership")
    if not isinstance(mo, dict) or not mo.get("collected"):
        return {"events": [], "without_owner": [],
                "omit": "needs a store gathered with module ownership "
                        "(module_ownership is absent)"}
    events, unattributed, lifecycle = [], [], []
    for e in mo.get("events") or []:
        if not isinstance(e, dict):
            continue
        row = dict(e)
        if e.get("module"):
            row["module_url"] = module_url(view, e["module"])[0]
        if e.get("kind") in LIFECYCLE_EVENT_KINDS:
            lifecycle.append(_with_repo(row, _slug(view), multi))
        elif not e.get("module"):
            # a label on an item that names no single module (a repository-wide
            # PR): kept apart, worded as such or left out with the reason
            unattributed.append(_with_repo(row, _slug(view), multi))
        else:
            events.append(_with_repo(row, _slug(view), multi))
    order = {k: i for i, k in enumerate(OWNERSHIP_KINDS)}
    events.sort(key=lambda r: (order.get(r.get("kind"), len(order)), r.get("date") or "",
                               r.get("module") or ""))

    def with_urls(rows):
        return [_with_repo(dict(w, module_url=module_url(view, w["module"])[0]),
                           _slug(view), multi)
                for w in rows or [] if isinstance(w, dict) and w.get("module")]
    unavailable = mo.get("without_owner_unavailable")
    without = [] if unavailable else with_urls(mo.get("without_owner"))
    by_kind = _without_owner_by_kind(without)
    out = {"events": events, "unattributed": unattributed, "lifecycle_events": lifecycle,
           "without_owner": without, "without_owner_by_kind": by_kind,
           "deprecated_modules": with_urls(mo.get("deprecated_modules")),
           "stub_modules_count": len(mo.get("stub_modules") or []),
           "start_without_owner": with_urls(_start_without_owner(mo)),
           "labelled_items": with_urls(mo.get("labelled_items")),
           "owner_files": mo.get("owner_files"),
           "label_patterns": mo.get("label_patterns"), "omit": None}
    if unavailable:
        out["without_owner_unavailable"] = unavailable
    if mo.get("unread"):
        out["unread"] = mo["unread"]
    # a notice many modules share is said once (`marker_texts`), with how many
    # carry it and whether it names a replacement (only explicit text does)
    if mo.get("marker_texts"):
        out["marker_texts"] = mo["marker_texts"]
    out["deprecated_replacements"] = [
        {"module": d["module"], "replacement": d["replacement"]}
        for d in out["deprecated_modules"] if d.get("replacement")]
    out["proposed_modules"] = [
        _with_repo(dict({"module": m, "module_url": None}, **{k: r.get(k) for k in
                                                                ("prs", "info", "committed_at")
                                                                if r.get(k)}), _slug(view), multi)
        for m, r in sorted((mo.get("proposed_modules") or {}).items()) if isinstance(r, dict)]
    if not events and not without and not lifecycle and not out["deprecated_modules"]:
        out["omit"] = ("module ownership state not known: {}".format(unavailable)
                       if unavailable else
                       "no ownership event and no module without an owner in the window")
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
        elif inp.get("windows_without_tags"):
            notes.append("version tags were not listed for window(s) {}: published / not "
                         "published is not known there".format(
                             ", ".join(str(w) for w in inp["windows_without_tags"])))
        if inp.get("unread"):
            notes.append("{} changelog / version file(s) could not be read"
                         .format(len(inp["unread"])))
    else:
        notes.append(_NO_MODULE_LAYER)
    mo = view.get("module_ownership")
    if isinstance(mo, dict) and mo.get("collected"):
        if mo.get("without_owner_unavailable"):
            notes.append("which modules have no owner is not known ({}): the issue does "
                         "not say that every module has one".format(
                             mo["without_owner_unavailable"]))
        if mo.get("unread"):
            notes.append("{} owner / marker file(s) could not be read".format(
                len(mo["unread"])))
        if mo.get("start_state_source") and mo["start_state_source"] != "complete":
            notes.append("module ownership at the window's start is {} "
                         "(module_ownership.start_state_source)".format(
                             mo["start_state_source"]))
    elif "module_ownership" not in view:
        notes.append("module ownership was not gathered: no ownership section")
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
                     plumbing_share_pct=round(100 * pl["share"])
                     if isinstance(pl.get("share"), (int, float)) else None,
                     modules_touched=(mc.get("summary") or {}).get("modules"),
                     modules_reader_facing=sum(1 for m in mc.get("modules") or []
                                               if not m.get("plumbing_only")),
                     modules_new=len((mc.get("summary") or {}).get("new_modules") or []),
                     modules_declared_breaking=len((mc.get("summary") or {})
                                                   .get("declared_breaking") or []))
            tags = ((v.get("module_notes") or {}).get("tags") or {})
            if tags.get("collected"):
                f["versions_published"] = len(tags.get("items") or [])
                if not _tags_complete(v):
                    f["versions_published_partial"] = True
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
    counts = [p["facts"].get("versions_published") for p in per]
    if any(isinstance(c, int) for c in counts) and (
            any(not isinstance(c, int) for c in counts)
            or any(p["facts"].get("versions_published_partial") for p in per)):
        # a member's count is partial or missing: the sum is a lower bound
        total["versions_published_partial"] = True
    if total.get("classified_prs"):
        total["plumbing_share_pct"] = round(100.0 * total.get("plumbing_prs", 0)
                                            / total["classified_prs"])
    return {"facts": total, "by_repo": per,
            "coverage": sorted({c for p in per for c in p["coverage"]}),
            "note": "people counts are per repository (a person can work in several)",
            "omit": None}


def _qualified(repo, name, multi):
    """A repository-local name (module area, login in a repo role) as
    `<owner/repo>::<name>` in a multi-repo outline; plain otherwise."""
    return "{}::{}".format(repo, name) if multi and repo else name


def section_intro(out):
    """What the intro leads with, in the issue's order: new modules, the lead
    changed modules, that a declared breaking change exists (link down to
    it), newcomers who landed merged work, the story groups, the plumbing
    themes."""
    sec = {s["id"]: s for s in out["sections"]}
    multi = len(out.get("repos") or []) > 1

    def q(r, key):
        return _qualified(r.get("repo"), r[key], multi)
    lead = []
    if sec["new_modules"].get("modules"):
        lead.append({"fact": "new_modules",
                     "areas": [q(m, "area") for m in sec["new_modules"]["modules"]]})
    mods = sec["module_changes"].get("modules") or []
    if mods:
        lead.append({"fact": "lead_modules", "areas": [q(m, "area") for m in mods[:3]]})
    b = sec["breaking"]
    if b.get("declared"):
        lead.append({"fact": "declared_breaking", "link_down": True,
                     "areas": [q(d, "area") for d in b["declared"]]})
    # landed means a merged PR is listed, whatever the class (an `unknown`
    # class can be a first PR closed unmerged)
    landed = [q(r, "login") for r in sec["people"].get("newcomers") or []
              if r.get("merged")]
    if landed:
        lead.append({"fact": "newcomers_landed_work", "logins": landed})
    groups = sec["stories"].get("groups") or {}
    counts = {g: len(groups.get(g) or []) for g in STORY_GROUPS if groups.get(g)}
    if counts:
        full = sec["stories"].get("full") or sec["stories"].get("all") or []
        lead.append({"fact": "stories", "by_group": counts,
                     "plan": sec["stories"].get("plan_counts"),
                     # the highest-scoring story, whatever its place in its group
                     "top_story": max(full, key=lambda r: r.get("score") or 0)["key"]
                     if full else None})
    if sec["plumbing"].get("themes"):
        lead.append({"fact": "plumbing_themes",
                     "themes": [t.get("title_hint") for t in sec["plumbing"]["themes"]],
                     "share": sec["plumbing"].get("share")})
    elif sec["plumbing"].get("share") is not None:
        lead.append({"fact": "plumbing_share", "share": sec["plumbing"]["share"]})
    return {"lead_with": lead, "omit": None}


def outline(views, cadence=None, automation=()):
    """The ordered, pre-selected material of one issue over one or more linked
    views (the members of a stream). Pure: reads the views, never modifies
    them. `automation` names accounts to treat as automation beyond the
    view's own marking. `cadence` None picks it from the window
    (auto_cadence). Raises ValueError for an unknown cadence."""
    views = join_members(views)
    auto, days = auto_cadence(views)
    cadence_source = "caller" if cadence else "window"
    cadence = cadence or auto
    if cadence not in CADENCE:
        raise ValueError("cadence must be one of {}".format(sorted(CADENCE)))
    caps = CADENCE[cadence]
    multi = len(views) > 1
    froms = sorted(f for f, _ in (_period(v) for v in views) if f)
    tos = sorted(t for _, t in (_period(v) for v in views) if t)
    frm, to = (froms[0] if froms else None), (tos[-1] if tos else None)

    def merged(builder, keys, *args, **kw):
        parts = [builder(v, *args, multi=multi, **kw) for v in views]
        out = {k: [x for p in parts for x in p.get(k) or []] for k in keys}
        omits = [p.get("omit") for p in parts if p.get("omit")]
        out["omit"] = omits[0] if len(omits) == len(parts) else None
        return out, parts

    breaking, _ = merged(section_breaking, ("declared", "candidates", "deprecations",
                                            "notices"))
    breaking["declared"].sort(key=lambda r: (-len(r["items"]), r["area"], r.get("repo", "")))
    new_mods, _ = merged(section_new_modules, ("modules", "child_modules"), caps)
    changes, parts = merged(section_module_changes, ("modules", "plumbing_only"), caps,
                            automation=automation)
    changes["modules"].sort(key=lambda r: (-r["score"], r["area"], r.get("repo", "")))
    changes = _cut_modules(changes, caps)
    if changes["omit"] is None and not changes["modules"]:
        changes["omit"] = "no module had a reader-facing change in the window"
    coming = {"ready": [], "failing_checks": [], "needs_first_review": [],
              "awaiting_review": [], "other": {}, "forecast": [], "quiet": 0,
              "proposed_modules": [], "approval_stale": []}
    for v in views:
        g = section_coming(v, caps, multi=multi)
        for k in ("ready", "approval_stale", "failing_checks", "needs_first_review",
                  "awaiting_review", "forecast", "proposed_modules"):
            coming[k] += g[k]
        for k, n in g["other"].items():
            coming["other"][k] = coming["other"].get(k, 0) + n
        coming["quiet"] += g["quiet"]
    coming = _finish_coming(coming, caps)
    plumbing, pparts = merged(section_plumbing, ("prs", "themes"), caps,
                              automation=automation)
    ownership, oparts = merged(section_ownership, ("events", "unattributed", "without_owner",
                                                   "start_without_owner", "labelled_items",
                                                   "lifecycle_events", "deprecated_modules",
                                                   "deprecated_replacements",
                                                   "proposed_modules"))
    mts = [x.get("marker_texts") for x in oparts if x.get("marker_texts")]
    if mts:
        ownership["marker_texts"] = mts[0] if not multi else [
            _with_repo({"marker_texts": x.get("marker_texts")}, _slug(v_), multi)
            for x, v_ in zip(oparts, views) if x.get("marker_texts")]
    ownership["without_owner_by_kind"] = _without_owner_by_kind(ownership["without_owner"])
    ownership["stub_modules_count"] = sum(x.get("stub_modules_count") or 0 for x in oparts)
    unav = [_with_repo({"reason": x["without_owner_unavailable"]}, r, multi)
            for x, r in zip(oparts, [_slug(v) for v in views])
            if x.get("without_owner_unavailable")]
    if unav:
        # never "no module without an owner" for a repository whose state is unknown
        ownership["without_owner_unavailable"] = unav if multi else unav[0]["reason"]
        if ownership.get("omit") and "not known" not in ownership["omit"]:
            ownership["omit"] = "module ownership state not known: {}".format(
                unav[0]["reason"])
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
    stories_sec = section_stories(views, caps, multi)
    connect_stories(views, stories_sec, plumbing, multi)
    sections = [
        {"id": "new_modules", **new_mods},
        {"id": "module_changes", **changes},
        {"id": "breaking", **breaking},
        {"id": "stories", **stories_sec},
        {"id": "ownership", **ownership},
        {"id": "people", **section_people(views, caps, multi, automation)},
        {"id": "coming", **coming},
        {"id": "plumbing", **plumbing},
        {"id": "numbers", **section_numbers(views, multi)},
    ]
    for s in sections:
        s["heading"] = HEADINGS[s["id"]][0]
    out = {"kind": "newsletter_outline", "version": 1,
           "repos": [_slug(v) for v in views], "period": {"from": frm, "to": to},
           "period_label": period_label(frm, to), "cadence": cadence,
           "cadence_source": cadence_source, "window_days": days,
           "as_of": max(((v.get("meta") or {}).get("records_as_of") or {}).get("instant")
                        or (v.get("meta") or {}).get("ref_date") or "" for v in views),
           "sections": sections}
    out["sections"].insert(0, {"id": "intro", "heading": None, **section_intro(out)})
    if not multi:
        out["repo"] = out["repos"][0]
    return out


# --- the check ----------------------------------------------------------------

_STORY_OPEN_RE = re.compile(r"<!--\s*story:\s*(\S*)\s*-->")
_STORY_CLOSE_RE = re.compile(r"<!--\s*/story\s*-->")


def marker_problems(text):
    """Malformed markers of an issue: story markers must pair up in order,
    without nesting, with a train key on the opening one; an `omitted`
    marker needs a section id and a reason; every `<!--` must close."""
    out = []
    tokens = sorted([(m.start(), "open", m.group(1)) for m in _STORY_OPEN_RE.finditer(text)]
                    + [(m.start(), "close", None) for m in _STORY_CLOSE_RE.finditer(text)])
    depth = 0
    for _, kind, key in tokens:
        if kind == "open":
            if not key:
                out.append("story marker without a train key")
            if depth:
                out.append("story block opened inside another story block")
            depth += 1
        else:
            if not depth:
                out.append("story closing marker without an opening one")
            else:
                depth -= 1
    if depth:
        out.append("story block not closed")
    for m in re.finditer(r"<!--\s*omitted\b(.*?)(-->|$)", text, re.S):
        if not m.group(2) or not _OMITTED_RE.match(m.group(0)):
            out.append("omitted marker without a section id and reason: {}"
                       .format(" ".join(m.group(0).split())[:80]))
    if text.count("<!--") > len(re.findall(r"<!--.*?-->", text, re.S)):
        out.append("an HTML comment is not closed")
    return out


# A story the issue does not (yet) carry in full is acknowledged with its key
# and the reason: `<!-- story pending: <key> — <reason> -->`. It is listed in
# `stories_pending` and is not missing; a story neither written nor pending is.
_PENDING_RE = re.compile(r"<!--\s*story pending:\s*(\S+)\s*(?:—|-|:)\s*(\S.*?)\s*-->")
# A plumbing theme whose narrative is not (yet) written:
# `<!-- theme pending: <theme id> — <reason> -->`.
_THEME_PENDING_RE = re.compile(r"<!--\s*theme pending:\s*(\S+)\s*(?:—|-|:)\s*(\S.*?)\s*-->")
_STORY_BLOCK_RE = re.compile(r"<!--\s*story:\s*(\S+)\s*-->(.*?)<!--\s*/story\s*-->", re.S)
_OMITTED_RE = re.compile(r"<!--\s*omitted:\s*([a-z_]+)\s*(?:—|-|:)\s*(\S.*?)\s*-->")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_FENCE_RE = re.compile(r"```.*?```", re.S)
_URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
_AVM_PATH_RE = re.compile(r"\bavm/(?:res|ptn|utl)/[a-z0-9-]+/[a-z0-9-]+(?:/[a-z0-9-]+)*")
_PATHY_RE = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")
# a version may end a sentence ("... 0.7.4."), but not continue as a longer
# dotted number ("0.47.16.16243")
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?!\w|\.\d|-\w)")
_LOGIN_RE = re.compile(
    r"(?<![\w/.@`])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?)(?![\w-])")
_LINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\s]*)\)")
# A release status a sentence about landed work must carry (or the next one).
_RELEASE_STATUS_RE = re.compile(
    r"released as|\bpublished\b|not yet published|declared in the changelog|"
    r"no version change|no new version|release status (?:is )?(?:not known|unknown)|"
    r"not released", re.I)
_LANDED_WORD_RE = re.compile(r"\b(landed|merged|shipped)\b", re.I)
# The marker that presents an automation account for what it is.
AUTOMATION_MARKER = "automation"
# Sections whose module mentions must be links (and published versions tag links),
# and those whose landed work must say its release status.
LINKED_SECTIONS = ("new_modules", "module_changes", "breaking", "ownership", "people",
                   "coming")
RELEASE_SECTIONS = ("new_modules", "module_changes", "people")


def _delink(text):
    """Markdown links and images replaced by their text (same lines)."""
    return _LINK_RE.sub(lambda m: m.group(2), text)


def _delink_items(text, repo):
    """`_delink`, but a link to an issue / PR (link.item_refs; not a comment or
    commit link) keeps the item it names after its text: "[the bump](.../pull/12)"
    reads "the bump (#12)", so a sentence naming an item by a link with other
    text still names it."""
    out, pos = [], 0
    for r in link.item_refs(text, repo):
        if r["form"] != "markdown" or r["evidence"] or r["number"] is None:
            continue
        a, _b = r["span"]
        m = _LINK_RE.match(text, a)
        if not m or a < pos:
            continue
        ref = "{}#{}".format(r["slug"], r["number"]) if r["slug"] else "#{}".format(r["number"])
        label = m.group(2)
        out.append(text[pos:a])
        out.append(label if ref in label else "{} ({})".format(label, ref))
        pos = m.end()
    out.append(text[pos:])
    return _delink("".join(out))


def _urls_in(obj, out):
    if isinstance(obj, dict):
        for x in obj.values():
            _urls_in(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _urls_in(x, out)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        out.add(obj)


def _section_chunks(text):
    """[(section id or None, text)] of the issue split at its `## ` headings;
    the part before the first is the intro."""
    out, last, sid = [], 0, "intro"
    for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.M):
        out.append((sid, text[last:m.start()]))
        h = m.group(1).strip().strip("*_").lower()
        sid = next((k for k, al in HEADINGS.items()
                    if any(h.startswith(a.lower()) for a in al)), None)
        last = m.start()
    out.append((sid, text[last:]))
    return out


def _story_place(text, pos):
    """(section id, `###` heading) a position of the issue sits under."""
    before = text[:pos]
    sid, h2_at = None, 0
    for m in re.finditer(r"^##\s+(.+?)\s*$", before, re.M):
        h = m.group(1).strip().strip("*_").lower()
        sid = next((k for k, al in HEADINGS.items()
                    if any(h.startswith(a.lower()) for a in al)), None)
        h2_at = m.end()
    h3 = None
    for m in re.finditer(r"^###\s+(.+?)\s*$", before[h2_at:], re.M):
        h3 = m.group(1).strip().strip("*_")
    return sid, h3
_NUMBER_RE = re.compile(r"(?<![\w.#/-])(\d[\d,]*(?:\.\d+)?)(%?)(?![\w.]\d|\w)")
_BREAK_WORD_RE = re.compile(r"\bbreak(?:s|ing)?\b", re.I)
_HEDGE_RE = re.compile(r"check before upgrading|\bcandidate|\bmay\b|\bmight\b|"
                       r"\bdetected\b|\bnot declared\b|\bnot listed\b", re.I)
_YEAR_RE = re.compile(r"^(19|20)\d\d$")

# Words that characterise people, effort or mood (story brief rule 7,
# reference/newsletter.md "What not to say"). Not exhaustive.
CHARACTERISING_WORDS = ("finally", "frustrated", "frustrating", "sloppy", "patiently",
                        "was ignored", "were ignored", "went ignored", "ignored by",
                        "tireless", "tirelessly", "heroic", "heroically", "lazy",
                        "careless", "unfortunately", "sadly", "pushed back hard",
                        "dragged", "struggled", "annoyed", "impatient",
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
    """Per repository (_slug): {area: set of versions it knows} over every
    module (changelog versions, tags, version files, child version files,
    release tags, children's), the set of every known module path (areas,
    children, child version files, code areas) and the text a module path
    may be named in (changelog items, PR titles). Module areas are
    repository-local, so nothing is merged across repositories."""
    versions, paths, text = {}, {}, {}
    own = {}       # {repo: {module path: versions of that exact module}}
    for v in views:
        repo = _slug(v)
        ro = own.setdefault(repo, {})

        def put(module, ver, ro=ro):
            if ver:
                ro.setdefault(module, set()).add(version_key(ver))
        rv = versions.setdefault(repo, {})
        rp = paths.setdefault(repo, set())
        rt = text.setdefault(repo, [])
        for a in (v.get("code_graph") or {}).get("areas") or []:
            if a.get("id"):
                rp.add(a["id"])
        mc = v.get("module_changes")
        if isinstance(mc, dict):
            for m in mc.get("modules") or []:
                area = m["area"]
                rp.add(area)
                vs = rv.setdefault(area, set())
                ver = m.get("versions") or {}
                for k in ("changelog", "declared_not_published", "published_not_declared"):
                    vs.update(version_key(x) for x in ver.get(k) or [])
                    for x in ver.get(k) or []:
                        put(area, x)
                for pub in ver.get("published") or []:
                    vs.add(version_key(pub.get("version")))
                    put(pub.get("module") or area, pub.get("version"))
                    if pub.get("module"):
                        rp.add(pub["module"])
                vf = ver.get("version_file") or {}
                vs.update(version_key(x) for x in (vf.get("from"), vf.get("to")) if x)
                put(area, vf.get("from"))
                put(area, vf.get("to"))
                for cv in ver.get("child_version_files") or []:
                    if cv.get("module"):
                        rp.add(cv["module"])
                    vs.update(version_key(x) for x in (cv.get("from"), cv.get("to")) if x)
                    put(cv.get("module") or area, cv.get("from"))
                    put(cv.get("module") or area, cv.get("to"))
                for rel in ver.get("releases") or []:
                    tag = str(rel.get("tag") or "")
                    if tag:
                        vs.add(version_key(tag.rsplit("/", 1)[-1]))
                        put(area, tag.rsplit("/", 1)[-1])
                if m.get("first_version"):
                    vs.add(version_key(m["first_version"]))
                    put(area, m["first_version"])
                for c in m.get("changelog") or []:
                    vs.add(version_key(c.get("version")))
                    put(c.get("module") or area, c.get("version"))
                    if c.get("module"):
                        rp.add(c["module"])
                    for sec in c.get("sections") or []:
                        for t in _items_text(sec.get("items")):
                            rt.append(t or "")
                            vs.update(_VERSION_RE.findall(t or ""))
                            # a version a changelog item names (a referenced
                            # module's) is evidence for the module whose
                            # changelog it is
                            for x in _VERSION_RE.findall(t or ""):
                                put(c.get("module") or area, x)
                for ch in (m.get("structure") or {}).get("children") or []:
                    if ch.get("path"):
                        rp.add(ch["path"])
                    if ch.get("from"):
                        rp.add(ch["from"])
                for c in (m.get("breaking") or {}).get("candidates") or []:
                    for part in str(c.get("subject") or "").split(" -> "):
                        if "/" in part:
                            rp.add(part.strip())
                vs.discard(None)
        rt.extend(pr.get("title") or "" for pr in v.get("prs") or [])
        mo = v.get("module_ownership") or {}
        # the modules the ownership fact names are modules of the repository,
        # and so is every module its end-of-window list holds
        rp.update(ownership_modules(mo))
        rp.update(k for k in (mo.get("modules_at_end") or {}) if isinstance(k, str))
        # a module an open PR proposes is a module path of the repository too
        rp.update(k for k in (mo.get("proposed_modules") or {}) if isinstance(k, str))
        for iss in v.get("issues") or []:
            # the module an issue form names (the thin-story module evidence)
            mm = _ISSUE_MODULE_RE.search(iss.get("body") or "")
            if mm and mm.group(1).startswith("avm/"):
                rp.add(mm.group(1).rstrip("/."))
    return versions, paths, {r: "\n".join(t) for r, t in text.items()}, own


_REPO_LINK_RE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/")


def _scoped_paragraphs(text, slugs):
    """[(paragraph, repo or None)] of the issue text: a paragraph's repository
    is the one its github.com links name (when they name exactly one of the
    views' repositories), else the one the nearest heading above it names
    (`owner/repo` or the bare repository name); with a single repository,
    always that one."""
    if len(slugs) == 1:
        return [(par, slugs[0]) for par in _paragraphs(text)]
    names = [slug_key(sl).split("/", 1)[-1] for sl in slugs]
    bare = {n: sl for n, sl in zip(names, slugs) if names.count(n) == 1}
    low = {slug_key(sl): sl for sl in slugs}
    out, heading = [], None
    for par in _paragraphs(text):
        for line in par.split("\n"):
            if re.match(r"^\s*#{1,6}\s", line):
                heading = None
                for tok in re.findall(r"[\w.-]+(?:/[\w.-]+)?", line):
                    t = tok.lower()
                    if t in low:
                        heading = low[t]
                    elif t in bare and heading is None:
                        heading = bare[t]
        linked = {low[x.lower()] for x in _REPO_LINK_RE.findall(par) if x.lower() in low}
        out.append((par, next(iter(linked)) if len(linked) == 1 else heading))
    return out


def _suppression_hits(text, hidden, registry_names=None):
    """The do_not_name logins the COMPLETE issue text names: `@login` (in
    prose, quotes, inline or fenced code, story blocks, comments, headings,
    tables, link or image text), the bare login as a word or a URL path
    segment (a profile or fork link such as github.com/<login>), and a name
    the registry holds for them. Case-insensitive, as logins are. A PR or
    issue link does not contain its author's login and is not a hit."""
    hits = set()
    for login in hidden:
        pat = r"(?<![A-Za-z0-9-])@?" + re.escape(login) + r"(?![A-Za-z0-9-])"
        if re.search(pat, text, re.I):
            hits.add(login)
    for login, name in (registry_names or {}).items():
        pat = r"\s+".join(re.escape(part) for part in str(name or "").split())
        if pat and re.search(r"(?<!\w)" + pat + r"(?!\w)", text, re.I):
            hits.add(login)
    return sorted(hits)


_ATTACH_GAP_RE = re.compile(
    r"^[\s`*_:]*(?:v?\d+\.\d+(?:\.\d+)?\s*(?:→|->|to)\s*)?v?$")


def _attached_module(par, pos, toks):
    """The module path a version at `pos` is written right after ("`x`
    0.13.0", "`x` 0.12 → 0.13.0"), else None."""
    best = None
    for tok in toks:
        for m in re.finditer(re.escape(tok) + r"(?![\w/-])", par):
            if m.end() <= pos and _ATTACH_GAP_RE.match(par[m.end():pos]):
                if best is None or m.end() > best[0]:
                    best = (m.end(), tok)
    return best[1] if best else None


def _area_of(path, areas):
    best = None
    for a in areas:
        if path == a or path.startswith(a + "/"):
            if best is None or len(a) > len(best):
                best = a
    return best


def check_newsletter(views, text, slices=None, registry_names=None, cadence=None,
                     automation=()):
    """The mechanical check of an issue against its views
    (reference/newsletter.md "The check"). `slices` maps a story train id to
    its slice (link.py --slice output); a story block without one is listed
    in `stories_not_checked`. `registry_names` maps a do_not_name login to a
    name the registry holds for it (that name must not appear). Pure."""
    views = join_members(views)
    text = (text or "").replace("\r\n", "\n")
    out_l = outline(views, cadence=cadence, automation=automation)
    sec = {s["id"]: s for s in out_l["sections"]}
    slices = slices or {}
    res = {"ok": True}

    # 1. story blocks: checked against their own slices, then blanked
    stories, not_checked, not_in_outline, duplicate, seen = {}, [], [], [], set()
    multi = len(views) > 1
    story_ids = {story_key_norm(s["key"]) for s in sec["stories"].get("all") or []} | \
        {story_key_norm(s["key"]) for s in sec["stories"].get("full") or []} | \
        {story_key_norm(s["key"]) for s in sec["stories"].get("pointers") or []} | \
        {story_key_norm(key) for v in views for key in _story_trains(v, multi)}
    norm_slices = {story_key_norm(k): x for k, x in slices.items()}
    info = {story_key_norm(r["key"]): r for r in
            (sec["stories"].get("all") or sec["stories"].get("full") or [])}
    theme_titles = {t.get("id"): t.get("title_hint")
                    for t in sec["plumbing"].get("themes") or []}
    misplaced, state_missing = [], []
    module_links = set()
    for v_ in views:
        _, paths_, _, _ = _module_index([v_])
        for pth in paths_.get(_slug(v_), set()):
            if _AVM_PATH_RE.fullmatch(pth) or pth.startswith("modules/"):
                module_links.add(module_url(v_, pth)[0])
    for m in _STORY_BLOCK_RE.finditer(text):
        tid, body = m.group(1), m.group(2).strip()
        nk = story_key_norm(tid)
        row = info.get(nk)
        if row is not None:
            # a story sits under its group and says its state in its first line
            sid, h3 = _story_place(text, m.start())
            group = row.get("group")
            if group == "plumbing":
                hint = theme_titles.get(row.get("theme"))
                ok_place = sid == "plumbing" and (
                    not hint or (h3 or "").lower().startswith(hint.lower()))
                where = "the plumbing section" + (", under ### " + hint if hint else "")
            else:
                label = GROUP_HEADINGS.get(group, "")
                ok_place = sid == "stories" and (h3 or "").lower().startswith(label.lower())
                where = "### " + label
            if not ok_place:
                misplaced.append({"story": tid, "expected": where,
                                  "found": "{} / {}".format(sid, h3)})
            first = next((ln for ln in body.split("\n") if ln.strip()), "")
            first = re.sub(r"^[\s#*_>]+", "", first)
            want = STATE_LABELS.get(row.get("state"), "")
            if not want or not first.lower().startswith(want.lower()):
                state_missing.append({"story": tid, "state": row.get("state"),
                                      "first_line": first[:120]})
        if nk in seen and tid not in duplicate:
            duplicate.append(tid)
        seen.add(nk)
        if nk not in story_ids:
            not_in_outline.append(tid)
        sl = norm_slices.get(nk)
        if sl is None:
            if tid not in not_checked:
                not_checked.append(tid)
        else:
            if row is not None:
                # the row's module and tag links, and the folder of any module
                # the view knows (a story may name a module its row lacks)
                sl = dict(sl, newsletter_links=sorted(
                    set(sl.get("newsletter_links") or []) | set(story_links(row)) |
                    module_links))
            r = link.check_story_evidence(sl, body)
            r = {k: x for k, x in r.items() if x or k == "ok"}
            prev = stories.get(tid)
            if prev is not None and not prev.get("ok"):
                r = prev            # a failing block is never hidden by a later one
            stories[tid] = r
    body = _STORY_BLOCK_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    pending = {story_key_norm(m.group(1)): m.group(2) for m in _PENDING_RE.finditer(text)}
    written = {story_key_norm(m.group(1)) for m in _STORY_BLOCK_RE.finditer(text)}
    res["stories_missing"] = [r["key"] for r in sec["stories"].get("full") or []
                              if story_key_norm(r["key"]) not in written
                              and story_key_norm(r["key"]) not in pending]
    res["stories_pending"] = sorted(pending)
    # a stalled train carried over is one line: its link appears in the issue
    res["story_lines_missing"] = [
        r["key"] for r in sec["stories"].get("lines") or []
        if r.get("url") and r["url"] not in text and story_key_norm(r["key"]) not in pending]
    # every plumbing theme has its narrative (a `###` heading in the plumbing
    # section) or a pending marker; a written theme names each of its trains
    plumbing_chunk = "".join(c for sid_, c in _section_chunks(text) if sid_ == "plumbing")
    h3s = [h.strip().strip("*_").lower()
           for h in re.findall(r"^###\s+(.+?)\s*$", plumbing_chunk, re.M)]
    theme_pending = {m.group(1) for m in _THEME_PENDING_RE.finditer(text)}
    written_themes, themes_missing = set(), []
    for th in sec["plumbing"].get("themes") or []:
        hint = (th.get("title_hint") or "").lower()
        if hint and any(h.startswith(hint) for h in h3s):
            written_themes.add(th.get("id"))
        elif th.get("id") not in theme_pending:
            themes_missing.append(th.get("id"))
    res["themes_missing"] = themes_missing
    res["themes_pending"] = sorted(theme_pending)
    # a train is named under ITS theme's heading, not anywhere in the section
    theme_text = {}
    hint_of = {th.get("id"): (th.get("title_hint") or "").lower()
               for th in sec["plumbing"].get("themes") or []}
    for part in re.split(r"(?m)^(?=###\s)", plumbing_chunk):
        h = re.match(r"^###\s+(.+?)\s*$", part, re.M)
        if not h:
            continue
        hh = h.group(1).strip().strip("*_").lower()
        for tid, hint in hint_of.items():
            if hint and hh.startswith(hint):
                theme_text[tid] = theme_text.get(tid, "") + part
    res["theme_trains_missing"] = []
    for r in sec["stories"].get("in_theme") or []:
        if r.get("theme") not in written_themes:
            continue
        chunk_ = theme_text.get(r.get("theme"), plumbing_chunk)
        if (r.get("url") and r["url"] in chunk_) or any(
                re.search(r"(?<![\w/.&])#{}\b".format(n), chunk_) or
                "/pull/{})".format(n) in chunk_ for n in r.get("prs") or []):
            continue
        res["theme_trains_missing"].append({"key": r["key"], "theme": r.get("theme")})
    res.update(stories=stories, stories_not_checked=not_checked,
               stories_not_in_outline=not_in_outline, stories_duplicate=duplicate,
               stories_misplaced=misplaced, stories_state_missing=state_missing,
               markers_malformed=marker_problems(text))

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
    slugs = [_slug(v) for v in views]
    versions, paths, module_text, own_versions = _module_index(views)
    items = {sl: set() for sl in slugs}
    people_by_repo = {sl: {} for sl in slugs}
    hidden = set(registry_names or {})
    for v in views:
        sl = _slug(v)
        items[sl] |= {p.get("number") for p in v.get("prs") or []} | \
            {i.get("number") for i in v.get("issues") or []}
        hidden |= _do_not_name(v)
        for login in set((v.get("people") or {})) | set(((v.get("affiliations") or {})
                                                           .get("people") or {})):
            people_by_repo[sl].setdefault(login_key(login), _person(v, login))
        for p in v.get("prs") or []:
            if p.get("author"):
                people_by_repo[sl].setdefault(login_key(p["author"]),
                                              _person(v, p["author"]))
    people = {}
    for sl in slugs:
        for login, row in people_by_repo[sl].items():
            people.setdefault(login, row)
    home_words = set()
    for v in views:
        owner = ((v.get("meta") or {}).get("owner") or "").lower()
        a = v.get("affiliations") or {}
        home_words |= {n.lower() for n in a.get("home_names") or [] if n.lower() != owner}
        home_words |= {n.lower() for n in a.get("home_orgs") or [] if n.lower() != owner}
    unresolved = []
    item_text = {}
    for v in views:
        for x in (v.get("prs") or []) + (v.get("issues") or []):
            if x.get("number") is not None:
                item_text[(slug_key(_slug(v)), x["number"])] = "{}\n{}".format(
                    x.get("title") or "", x.get("body") or "")

    def linked_text(par, repo=None):
        """The titles and descriptions of the view's items a paragraph names
        (link.item_refs: links, comment links to them, owner/repo#N, and a
        bare `#N` in the paragraph's repository, or the only one)."""
        own = repo or (slugs[0] if not multi else None)
        out = []
        for r in link.item_refs(par, own):
            if r["number"] is not None and r["repo"]:
                out.append(item_text.get((slug_key(r["repo"]), r["number"]), ""))
        return "\n".join(out)

    def resolve(repo, what, **row):
        """The repositories a claim is checked against: the paragraph's own,
        else the only one that knows it (`what` is a predicate on a repo);
        ambiguous claims are listed in repository_unresolved."""
        if repo is not None:
            return [repo] if what(repo) else []
        knowing = [sl for sl in slugs if what(sl)]
        if len(knowing) > 1:
            entry = dict(row)
            if entry not in unresolved:
                unresolved.append(entry)
        return knowing

    def tag(row, repo):
        return dict(row, repo=repo) if multi else row

    # URLs, quotes
    cited = []
    for u in _URL_RE.findall(_COMMENT_RE.sub(" ", body)):
        u = u.rstrip(".,;:")
        if u not in cited:
            cited.append(u)
    _urls_in(out_l, urls)
    for v in views:
        for m in (v.get("module_changes") or {}).get("modules") or []:
            urls.add(module_url(v, m["area"])[0])
            for pub in (m.get("versions") or {}).get("published") or []:
                if pub.get("tag"):
                    urls.add(tag_url(v, pub["tag"], pub.get("url"))[0])
                if pub.get("module"):
                    urls.add(module_url(v, pub["module"])[0])
        for a in (v.get("code_graph") or {}).get("areas") or []:
            if a.get("id"):
                urls.add(module_url(v, a["id"])[0])
    res["urls_not_in_view"] = [u for u in cited if u not in urls]
    quotes = []
    for m in link._QUOTE_RE.finditer(_COMMENT_RE.sub(" ", body)):
        q = m.group(1) or m.group(2)
        if not q.startswith(("http://", "https://")):
            quotes.append(q)
    res["quotes_not_in_view"] = [q for q in quotes
                                 if not any(link._norm_ws(q) in t for t in norm_texts)]
    if multi:
        # a quote in a paragraph about one repository must be that repository's
        per_repo_texts = {}
        for v in views:
            u_, t_ = set(), []
            link._slice_strings(v, u_, t_)
            per_repo_texts[_slug(v)] = [link._norm_ws(x) for x in t_]
        body_nc = _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)
        for par, repo in _scoped_paragraphs(body_nc, slugs):
            if repo is None:
                continue
            for m in link._QUOTE_RE.finditer(par):
                q = m.group(1) or m.group(2)
                if q.startswith(("http://", "https://")) or q in res["quotes_not_in_view"]:
                    continue
                if not any(link._norm_ws(q) in t for t in per_repo_texts[repo]):
                    res["quotes_not_in_view"].append(q)
    # strip quotes for the remaining prose checks (they are verified above)
    no_quotes = link._QUOTE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                                   _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ",
                                                                    m.group(0)), body))
    prose = _strip(no_quotes, code=True)
    scoped = _scoped_paragraphs(no_quotes, slugs)

    def areas_named(par, repo_areas):
        par_c = _strip(par, code=False)
        toks = [m.group(0).rstrip("/.-") for m in _AVM_PATH_RE.finditer(par_c)]
        toks += [m.group(0).strip("`") for m in _CODE_SPAN_RE.finditer(par)
                 if m.group(0).strip("`").startswith("modules/")]
        return toks

    # modules: an AVM module path (or a Terraform `modules/<name>` submodule)
    # must be a module, child, code area or path the repository's changelogs name
    named_mods, bad_mods = [], []
    bad_versions, bad_items, worded = [], [], []
    roots = {}
    for v in views:
        for m in (v.get("module_changes") or {}).get("modules") or []:
            if m.get("area") == "main.tf":
                roots.setdefault(_slug(v), []).append(m["area"])
    cand = {(c.get("repo") or slugs[0], c["area"]): c
            for c in sec["breaking"].get("candidates") or []}
    declared = {(d.get("repo") or slugs[0], d["area"])
                for d in sec["breaking"].get("declared") or []}
    # the intro summarises across modules: it is never a root-module claim
    intro_pars = set(_paragraphs(_section_chunks(no_quotes)[0][1]))
    for par, repo in scoped:
        par_c = _strip(_delink(par), code=False)
        toks = areas_named(_delink(par), None)
        cited_text = linked_text(par, repo)
        par_mods = {}
        for tok in toks:
            if tok not in named_mods:
                named_mods.append(tok)
            known = resolve(repo, lambda sl, t=tok: t in paths[sl] or t in module_text[sl]
                            or _area_of(t, versions[sl]) is not None, module=tok)
            ok_repos = [sl for sl in known if tok in paths[sl] or tok in module_text[sl]]
            if not ok_repos and tok in cited_text:
                ok_repos = ["cited"]
            if not ok_repos:
                row = tag({"module": tok}, repo) if multi else tok
                if row not in bad_mods:
                    bad_mods.append(row)
            for sl in known:
                a = _area_of(tok, versions[sl])
                if a is not None:
                    par_mods.setdefault((sl, a), None)
        # versions belong to their module: one written right after a module
        # path is that module's; any other must belong to a module the
        # paragraph names (parent, a named child, or a child of a named parent)
        named_here = [(sl, tok) for tok in toks for sl in
                      ([repo] if repo is not None else slugs)
                      if tok in own_versions.get(sl, {}) or
                      _area_of(tok, versions[sl]) is not None]
        for vm in _VERSION_RE.finditer(par_c):
            ver = vm.group(1)
            attached = _attached_module(par_c, vm.start(), toks)
            if attached is not None and not any(attached in own_versions.get(sl, {})
                                                 for sl in slugs):
                attached = None    # a module the window holds no versions of
            if re.search(r"(?<![\w.])v?" + re.escape(ver) + r"(?![\w]|\.\d)", cited_text):
                continue           # the version as a linked item states it
            if attached is not None:
                pool = [repo] if repo is not None else \
                    [sl for sl in slugs if attached in own_versions.get(sl, {})
                     or _area_of(attached, versions[sl]) is not None] or slugs
                ok = any(ver in own_versions.get(sl, {}).get(attached, set()) for sl in pool)
                row = tag({"module": attached, "version": ver},
                          pool[0] if len(pool) == 1 else repo)
            elif named_here:
                ok = False
                for sl, tok in named_here:
                    for mod, vs in own_versions.get(sl, {}).items():
                        if (mod == tok or mod.startswith(tok + "/")) and ver in vs:
                            ok = True
                row = tag({"module": None, "version": ver}, repo)
            else:
                pool = [repo] if repo is not None else slugs
                ok = any(ver in vs for sl in pool for vs in versions[sl].values())
                row = tag({"module": None, "version": ver}, repo)
            if not ok and row not in bad_versions:
                bad_versions.append(row)
        # items, by the shared resolver (link.item_refs): a link's or an
        # owner/repo#N's own repository, else (#N) the paragraph's. An item of
        # a repository outside the views is not checked here (its link is
        # checked as a URL); a link to a comment, review or commit is evidence
        refs = [r for r in link.item_refs(par, repo) if not r["evidence"]
                and r["number"] is not None]
        linked_nums = []
        for r in refs:
            if r["form"] == "local":
                continue
            sl = next((x for x in slugs if slug_key(x) == slug_key(r["repo"] or "")), None)
            if sl is None:
                continue
            n = r["number"]
            linked_nums.append(n)
            if n not in items.get(sl, set()):
                row = tag({"number": n}, sl) if multi else n
                if row not in bad_items:
                    bad_items.append(row)
        for r in refs:
            if r["form"] != "local":
                continue
            n = r["number"]
            if n in linked_nums:
                continue
            if re.search(r"#{}\b|/(?:pull|issues)/{}\b".format(n, n), cited_text):
                continue           # an item a linked item itself refers to
            known = resolve(repo, lambda sl, n=n: n in items[sl], number=n)
            if not known:
                row = tag({"number": n}, repo) if multi else n
                if row not in bad_items:
                    bad_items.append(row)
        # breaking candidates worded as declared (per repository and area)
        heading_only = all(re.match(r"^\s*#{1,6}\s", ln) or not ln.strip()
                           for ln in par.split("\n"))
        claim_mods = dict(par_mods)
        if not claim_mods and repo is not None and not heading_only \
                and par not in intro_pars:
            # a paragraph of a repository naming no module path speaks of its
            # root module (a Terraform repository's `main.tf` area), if any
            for a in roots.get(repo, []):
                claim_mods[(repo, a)] = None
        if not heading_only and _BREAK_WORD_RE.search(_strip(par, code=True)) \
                and not _HEDGE_RE.search(par_c):
            for (sl, a) in sorted(claim_mods):
                c = cand.get((sl, a))
                if c is None:
                    continue
                uncovered = [r["subject"] for r in c["rules"] if not r["covered_by_declared"]]
                subj_named = [s_ for s_ in uncovered if s_ and s_.split(" ->")[0]
                              .split("/")[-1] in par]
                if (sl, a) not in declared or subj_named:
                    worded.append(tag({"module": a, "subjects": subj_named or uncovered,
                                       "paragraph": " ".join(par.split())[:200]}, sl))
    res["modules_not_in_view"] = sorted(bad_mods, key=json.dumps) if multi else \
        sorted(set(bad_mods))
    res["versions_not_in_view"] = bad_versions
    res["items_not_in_view"] = bad_items if multi else sorted(set(bad_items))
    res["candidates_worded_as_declared"] = worded

    # people: known logins, names tied to their login, affiliation words
    mentioned = _LOGIN_RE.findall(prose)
    res["people_not_in_view"] = sorted({m for m in mentioned
                                        if login_key(m) not in people})
    # suppression reads the COMPLETE issue (stories, quotes, code, comments)
    res["do_not_name_named"] = _suppression_hits(text, hidden, registry_names)
    mismatch = []
    names = {p["name"]: key for key, p in people.items() if p.get("name")}
    for m in re.finditer(r"\(@([A-Za-z0-9-]+)[,)]", prose):
        key = login_key(m.group(1))
        login = (people.get(key) or {}).get("login") or m.group(1)
        before = prose[max(0, m.start() - 80):m.start()].split("\n")[-1]
        before = re.sub(r"[*_\[\]]", "", before).rstrip()
        want = (people.get(key) or {}).get("name")
        if want:
            if not before.endswith(want):
                mismatch.append({"login": login, "name": want,
                                 "written": before[-len(want) - 10:].strip()})
        else:
            other = next((n for n, lg in names.items() if lg != key
                          and before.endswith(n)), None)
            if other:
                mismatch.append({"login": login, "name": None, "written": other})
    res["name_mismatch"] = mismatch
    aff_bad = []
    for par, repo in scoped:
        for s in _sentences(_strip(par, code=True)):
            logins = [login_key(x) for x in _LOGIN_RE.findall(s)
                      if login_key(x) in people]
            if not logins:
                continue
            says = []
            if re.search(r"\bcommunity\b", s, re.I):
                says.append("community")
            if any(re.search(r"\b" + re.escape(w) + r"\b", s, re.I) for w in home_words):
                says.append("maintainer_org")
            for said in says:
                for lg in logins:
                    pool = [repo] if repo is not None else slugs
                    labels = {people_by_repo[sl][lg].get("affiliation")
                              for sl in pool if lg in people_by_repo[sl]} or \
                        {people[lg].get("affiliation")}
                    if labels != {said}:
                        aff_bad.append(tag({"login": people[lg].get("login") or lg,
                                            "said": "community"
                                            if said == "community" else "home organisation",
                                            "label": sorted(x or "unknown" for x in labels)[0]
                                            if len(labels) == 1 else sorted(
                                                x or "unknown" for x in labels),
                                            "sentence": s.strip()[:200]}, repo))
    res["affiliation_mismatch"] = aff_bad
    res["repository_unresolved"] = unresolved

    # every module mention in the reader-facing sections is a link, and a
    # published version stated with it links its tag
    published = {}
    for v in views:
        for m in (v.get("module_changes") or {}).get("modules") or []:
            for pub in (m.get("versions") or {}).get("published") or []:
                published.setdefault(_slug(v), {}).setdefault(
                    pub.get("module") or m["area"], set()).add(version_key(pub.get("version")))
    owned = {}
    for v in views:
        mo = v.get("module_ownership") or {}
        owned[_slug(v)] = ownership_modules(mo)
    not_linked, ver_not_linked, no_status, not_owned = [], [], [], []
    for sid, chunk in _section_chunks(no_quotes):
        if sid not in LINKED_SECTIONS and sid not in RELEASE_SECTIONS:
            continue
        for par, repo in _scoped_paragraphs(chunk, slugs):
            if _is_heading(par):
                continue
            plain = _delink(par)
            toks = areas_named(plain, None)
            pool = [repo] if repo is not None else slugs
            if sid in LINKED_SECTIONS:
                for tok in toks:
                    linkable = any(tok in own_versions.get(sl, {}) or tok in versions[sl]
                                   for sl in pool)
                    if linkable and not re.search(r"\[[^\]\n]*" + re.escape(tok) +
                                                  r"[^\]\n]*\]\(", par):
                        row = tag({"section": sid, "module": tok}, repo)
                        if row not in not_linked:
                            not_linked.append(row)
                plain_c = _strip(plain, code=False)
                for vm in _VERSION_RE.finditer(plain_c):
                    mod = _attached_module(plain_c, vm.start(), toks)
                    ver = vm.group(1)
                    if mod and any(ver in published.get(sl, {}).get(mod, set())
                                   for sl in pool) and \
                            not re.search(r"\[v?" + re.escape(ver) + r"\]\(", par):
                        row = tag({"section": sid, "module": mod, "version": ver}, repo)
                        if row not in ver_not_linked:
                            ver_not_linked.append(row)
            if sid == "ownership":
                for tok in toks:
                    if not any(tok in owned.get(sl, set()) for sl in pool):
                        row = tag({"module": tok}, repo)
                        if row not in not_owned:
                            not_owned.append(row)
            if sid in RELEASE_SECTIONS:
                sents = _sentences(_strip(plain, code=False))
                for i, snt in enumerate(sents):
                    # a sentence about a particular PR or module (it names an
                    # item or a module path) that says it landed
                    about = any(r["number"] is not None for r in link.item_refs(snt)) or \
                        _AVM_PATH_RE.search(snt)
                    if about and _LANDED_WORD_RE.search(snt) and not any(
                            _RELEASE_STATUS_RE.search(x) for x in sents[i:i + 2]):
                        no_status.append({"section": sid, "sentence": " ".join(snt.split())[:200]})
    res["modules_not_linked"] = not_linked
    res["versions_not_linked"] = ver_not_linked
    res["release_status_missing"] = no_status
    res["ownership_not_in_view"] = not_owned

    # automation accounts are presented as automation, never as people
    bots = set()
    for v in views:
        for login in set(v.get("people") or {}) | {x.get("author") for x in v.get("prs") or []
                                                    if x.get("author")}:
            if is_automation(v, login, automation):
                bots.add(login_key(login))
    as_person = []
    for par in _paragraphs(prose):
        for snt in _sentences(par):
            for lg in _LOGIN_RE.findall(snt):
                if login_key(lg) not in bots:
                    continue
                if re.search(r"\(@" + re.escape(lg) + r"\s*[,)]", snt) or \
                        AUTOMATION_MARKER not in snt.lower():
                    as_person.append({"login": lg, "sentence": " ".join(snt.split())[:200]})
    res["automation_as_person"] = as_person

    # one state per fact: a PR's review state, its open/merged/closed state and
    # a version's release status read the same in every section
    res.update(state_claims(views, text))
    res.update(relation_claims(views, text))

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
                       if m.group(0).replace(".", "", 1).isdigit() else " " * len(m.group(0)),
                       num_prose)
    # an item reference (#N, owner/repo#N) is not a count
    for r in link.item_refs(num_prose):
        if r["form"] in ("local", "short"):
            a_, b_ = r["span"]
            num_prose = num_prose[:a_] + " " * (b_ - a_) + num_prose[b_:]
    num_prose = re.sub(r"^\s*\d+\.\s", lambda m: " " * len(m.group(0)), num_prose,
                       flags=re.M)
    bad_nums = []
    # a paragraph may also state what the items it links say (their title and
    # description), and how many items it links
    spans, last = [], 0
    for m in re.finditer(r"\n\s*\n", no_quotes):
        spans.append((last, m.start()))
        last = m.end()
    spans.append((last, len(no_quotes)))
    local = []
    for a, b in spans:
        cited_par = linked_text(no_quotes[a:b])
        toks_ = set(re.findall(r"\d[\d,]*(?:\.\d+)?", cited_par))
        toks_ = {x.replace(",", "") for x in toks_}
        n_items = len({(r["repo"], r["number"]) for r in link.item_refs(no_quotes[a:b])
                       if r.get("url") and r["number"] is not None})
        if n_items:
            toks_.add(str(n_items))
        local.append((a, b, toks_))
    for m in _NUMBER_RE.finditer(num_prose):
        tok = m.group(1).replace(",", "")
        if _YEAR_RE.match(tok) or tok in allowed:
            continue
        if any(a <= m.start() < b and tok in t_ for a, b, t_ in local):
            continue
        if tok not in bad_nums:
            bad_nums.append(tok)
    res["numbers_not_in_view"] = bad_nums

    # characterising words
    low = prose.lower()
    res["characterising_words"] = [w for w in CHARACTERISING_WORDS
                                   if re.search(r"\b" + re.escape(w) + r"\b", low)]

    fail_keys = ("stories_not_checked", "stories_not_in_outline", "stories_duplicate",
                 "stories_missing", "story_lines_missing", "themes_missing",
                 "theme_trains_missing",
                 "stories_misplaced", "stories_state_missing", "modules_not_linked",
                 "versions_not_linked", "release_status_missing", "ownership_not_in_view",
                 "automation_as_person",
                 "review_state_mismatch", "pr_state_mismatch", "release_status_mismatch",
                 "superseded_not_in_view", "relations_not_in_view",
                 "relation_denials_contradicted",
                 "markers_malformed",
                 "sections_missing",
                 "omitted_section_present", "sections_out_of_order", "title_missing",
                 "intro_missing", "urls_not_in_view", "quotes_not_in_view",
                 "modules_not_in_view", "versions_not_in_view", "items_not_in_view",
                 "people_not_in_view", "do_not_name_named", "name_mismatch",
                 "affiliation_mismatch", "candidates_worded_as_declared",
                 "repository_unresolved",
                 "dates_without_year", "numbers_not_in_view", "characterising_words")
    res["ok"] = not any(res[k] for k in fail_keys) and \
        all(r.get("ok") for r in stories.values())
    res["checked"] = {"urls": len(cited), "quotes": len(quotes),
                      "modules": len(named_mods), "people": len(set(mentioned)),
                      "stories": len(stories) + len(not_checked),
                      "sections_present": present, "sections_omitted": sorted(omitted)}
    res["cannot_check"] = list(CANNOT_CHECK)
    return res


# --- one state per fact across sections -------------------------------------

REVIEW_STATES = ("none", "requested", "automated_only", "commented", "changes_requested",
                 "approved", "approved_stale")
_NOT_REVIEWED = {"none", "requested", "automated_only"}
# (claim, pattern, the review states the claim is true of); the first match
# per pattern group wins, stale before plain approval
_REVIEW_CLAIMS = (
    ("approved_stale", re.compile(
        r"\b(?:stale|earlier|older|previous|prior)\s+approval|\bapprov\w*\b[^.;]*?\b(?:before|"
        r"predates?|ahead of|since then|then|but|later)\b[^.;]*?\b(?:push\w*|commit\w*|chang\w*)"
        r"|\bpushed\b[^.;]*\bafter\b[^.;]*\bapprov", re.I), {"approved_stale"}),
    ("not_approved", re.compile(
        r"\b(?:not\s+(?:yet\s+)?(?:been\s+)?approved|no\s+approval|without\s+(?:an?\s+)?approval|"
        r"awaiting\s+(?:an?\s+)?approval|waiting\s+for\s+(?:an?\s+)?approval)\b", re.I),
     set(REVIEW_STATES) - {"approved"}),
    # a review verdict, not the adjective ("an approved test subscription")
    ("approved", re.compile(
        r"(?:@[\w\[\]-]+\s+|\b(?:was|is|been|got|were|has|have|had|and|then|it|first|"
        r"maintainers?|reviewers?)\s+)approved\b|\bapproved\s+(?:it|the\s+(?:pr|pull\s+request|"
        r"change)|on|by)\b|\b(?:an?|the|his|her|their|its|one|current)\s+approv(?:al|ing\s+review)\b",
        re.I), {"approved"}),
    ("changes_requested", re.compile(
        r"\bchanges?\s+(?:(?:were|was|have\s+been|has\s+been)\s+)?requested\b|"
        r"\brequested\s+changes\b", re.I), {"changes_requested"}),
    ("automated_only", re.compile(
        r"\bonly\s+(?:an?\s+)?automat\w*\s+review|\breview\w*\s+only\s+(?:by|from)\s+"
        r"(?:an?\s+)?automat", re.I), {"automated_only"}),
    ("not_reviewed", re.compile(
        r"\b(?:awaiting|awaits|waiting\s+for|waits\s+for|needs|has\s+not\s+had|hasn't\s+had|without|"
        r"no)\s+(?:a\s+|its\s+)?first\s+(?:human\s+)?review|\bnot\s+(?:yet\s+)?(?:been\s+)?"
        r"reviewed\b|\bno\s+(?:human\s+)?review\s+(?:yet|so\s+far)\b", re.I), _NOT_REVIEWED),
    # "not ready to merge" says nothing about the approval
    ("ready", re.compile(r"(?<!not\s)(?<!never\s)(?<!n't\s)\bready\s+(?:to|for)\s+merg\w*",
                         re.I), {"approved"}),
)
_PR_STATE_CLAIMS = (
    ("closed_unmerged", re.compile(
        r"\bclosed\s+(?:without\s+(?:being\s+)?merg\w*|unmerged)|\bwas\s+not\s+merged\b|"
        r"\bnever\s+merged\b", re.I)),
    ("merged", re.compile(r"\b(?:is|was|been|got|has)\s+merged\b|\bmerged\s+on\b", re.I)),
    ("open", re.compile(r"\b(?:is|remains|still)\s+open\b|\bstill\s+in\s+review\b", re.I)),
)
_SUPERSEDE_RE = re.compile(r"\bsupersed\w*|\breplaced\s+by\b", re.I)
_COUNT_SUBJECT_RE = re.compile(r"\b\d+\s+(?:were|are|of\s+them|others)\b", re.I)
_CONDITIONAL_RE = re.compile(r"\b(?:once|if|until|unless|whether|would|could|should)\b", re.I)
_PUB_CLAIMS = (
    ("not_published", re.compile(
        r"\bnot\s+(?:yet\s+)?(?:been\s+)?(?:published|released|tagged)\b|\bunpublished\b|"
        r"\bunreleased\b|\bawait\w*\s+(?:its\s+)?(?:publication|tag)\b|\bno\s+tag\b", re.I)),
    ("published", re.compile(r"\b(?:published|released|tagged)\b", re.I)),
)


def _review_claim(snt):
    out = []
    for name, rx, states in _REVIEW_CLAIMS:
        if rx.search(snt):
            if name == "approved" and out and out[-1][0] in ("approved_stale", "not_approved"):
                continue
            out.append((name, states))
    return out


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")


def _is_heading(par):
    """A markdown heading: one to six `#` then whitespace ("#10 was merged."
    is a sentence about item 10, not a heading)."""
    return bool(_HEADING_RE.match(par))


# --- the record as of a day ------------------------------------------------------

def absence_provable(view, day, tags=False):
    """Whether the record can support a NEGATIVE for `day` ("not published",
    "no owner was added", "nothing happened since"): the view's coverage
    (its period) reaches the day, and, for a claim about tags, the tags were
    read for the whole period (`_tags_complete`). Absence anywhere else is
    not proof; the caller keeps the row pending or says "not known"."""
    period = _period(view)
    if not period[0] or not period[1] or day > period[1]:
        return False
    return _tags_complete(view) if tags else True


def _instant(day):
    return "{}T23:59:59Z".format(day)


# a PR's state in the words the issue's state claims use (`closed_unmerged`
# is the record's own name for a PR closed without merging)
_PR_STATE_NAMES = {"not_created": "not_opened", "closed": "closed_unmerged"}


def pr_state_on(pr, day=None):
    """A PR's state at the end of `day` (None: as recorded) by THE lifecycle
    replay, `link.item_state`, renamed for PR claims: not_opened, open,
    merged, closed_unmerged; None without the record."""
    st = link.item_state(pr, day) if pr else None
    return _PR_STATE_NAMES.get(st, st)


def release_on(view, module, version, day):
    """A module version's release status at the end of `day`: `published`
    when a tag of it is dated on or before the day; `declared_not_published`
    when it is not, and the view's tag coverage reaches the day; `unknown`
    when the tags were not read for the window or the day is past the view's
    coverage (its period end)."""
    mc = view.get("module_changes") or {}
    period = _period(view)
    if not period[1]:
        return "unknown"
    vk = version_key(version)
    # every date the record gives the version's tag (the module layer's
    # published rows; the story rows' released versions carry it too)
    dates, undated = [], False
    for row in (view.get("stories") or {}).get("all") or []:
        for x in row.get("released") or []:
            if isinstance(x, dict) and x.get("status") == "published" and \
                    x.get("module") == module and version_key(x.get("version")) == vk:
                (dates.append(_day(x["date"])) if x.get("date") else None)
    for m in mc.get("modules") or []:
        for pub in (m.get("versions") or {}).get("published") or []:
            if (pub.get("module") or m["area"]) == module and version_key(pub.get("version")) == vk:
                if pub.get("date"):
                    dates.append(_day(pub["date"]))
                else:
                    undated = True
    # a tag dated on or before the day proves publication on its own
    if any(d <= day for d in dates) and day <= period[1]:
        return "published"
    if undated and day == period[1]:
        return "published"   # a tag without a date: published by the window's end
    # every negative needs complete tag coverage reaching the day
    if not absence_provable(view, day, tags=True):
        return "unknown"
    if undated and not dates:
        return "unknown"     # tagged at an unknown time: the day cannot be told
    return "declared_not_published"


# --- relation claims -----------------------------------------------------------
#
# No second parser and no second "does it hold" rule: the claims are read by
# link.parse_relation_claims and judged by link.relation_holds, the functions
# the view itself is built with. What stays here is prose scope: which
# repository a paragraph speaks for, which story a sentence sits in, the day
# judged, and how a failure is reported.

# the relations a present-tense claim asserts are still in force
_PRESENT_KINDS = ("blocked_by", "depends_on", "stacked_on", "blocks", "needed_by",
                  "stacked_under") + tuple(link.GENERIC_RELATIONS)
_REASON_TEXT = {"no_evidence": "no such relation in the record",
                "after_day": "stated in the record only after the day",
                "task_done": "the blocker is no longer open",
                "text_not_current": "the blocker is no longer open",
                "source_not_open": "the blocker is no longer open",
                "target_not_open": "the blocker is no longer open",
                "state_unknown": "an end the record does not hold"}


def join_members(views):
    """The stream's member views with their relations joined
    (`link.join_member_relations`): a reference from one member to another
    member's issue / PR becomes a cross-member edge to that member's story.
    The scheduled path links each member on its own, so every reader joins
    them here. Pure: the views given are not changed (the joined parts are
    copies); a single view, or members already joined, come back as they
    are."""
    views = list(views)
    if len(views) < 2 or all(v.get("_members_joined") for v in views):
        return views
    slugs = {_slug(v).lower() for v in views}
    need = any(isinstance(e.get("to"), dict) and (e["to"].get("repo") or "").lower() in slugs
               for v in views for e in (v.get("story_graph") or {}).get("edges") or [])
    if not need:
        return views
    import copy
    out = []
    for v in views:
        c = dict(v)
        for k in ("story_graph", "stories"):
            if k in v:
                c[k] = copy.deepcopy(v[k])
        c["_members_joined"] = True
        out.append(c)
    link.join_member_relations([{"bundle": c, "repo": _slug(c)} for c in out])
    return out


def _root_item(view, train_id):
    """The item a story is about (its root issue, else its first PR / issue):
    the default subject of a sentence inside its block or post."""
    t = next((x for x in view.get("trains") or [] if x.get("id") == train_id), None)
    if not t:
        return None
    n = t.get("root_issue") or (t.get("prs") or [None])[0] or (t.get("issues") or [None])[0]
    return {"slug": _slug(view), "number": n} if n is not None else None


_EMPHASIS_RE = re.compile(r"\*\*|__")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def _claim_scope(views, text, subject_story=None):
    """The prose scope every claim check reads with the shared parser:
    [(section id, paragraph, the paragraph as written, repository, default
    subject)] — a paragraph's repository (its links or the nearest
    repository heading; the only one for one view), its default subject (the
    root item of the story block it sits in, else a post's own story
    `subject_story`). Comments (but story markers), fences and quoted text
    are blanked in `paragraph`; `the paragraph as written` keeps the quotes."""
    slugs = [_slug(v) for v in views]
    low = {x.lower(): x for x in slugs}
    by_slug = {_slug(v): v for v in views}
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))   # noqa: E731
    t = _COMMENT_RE.sub(lambda m: m.group(0) if m.group(0).lstrip("<!- ").startswith(
        ("story:", "/story")) else blank(m), text)
    t = _FENCE_RE.sub(blank, t)
    quoted = link._QUOTE_RE.sub(blank, t)        # the record's words are not our claims
    blocks = [(m.start(), m.end(), m.group(1)) for m in _STORY_BLOCK_RE.finditer(t)]

    def default_subject(par_pos, repo):
        for a, b, key in blocks:
            if a <= par_pos < b:
                sl, _, tid = key.rpartition(":") if ":" in key else (repo, "", key)
                v = by_slug.get(low.get((sl or repo or "").lower(), sl)) or \
                    (views[0] if len(views) == 1 else None)
                return _root_item(v, tid) if v else None
        if subject_story:
            sl, _, tid = subject_story.rpartition(":")
            v = by_slug.get(low.get(sl.lower(), sl))
            return _root_item(v, tid) if v else None
        return None

    out, pos = [], 0
    for sid, chunk in _section_chunks(quoted):
        start = quoted.index(chunk, pos) if chunk else pos
        pos = start + len(chunk)
        # a heading is prose too ("### #30 depends on #13" claims a relation)
        pars, cur = [], start
        for par, repo in _scoped_paragraphs(chunk, slugs):
            at = quoted.find(par, cur)
            if at >= 0:
                cur = at + len(par)
            pars.append([par, repo, at])
        # a line ending in a colon hands the list right below it (no blank
        # line between) to the parser with it: "#30 depended on:" + "- #12"
        merged = []
        for par, repo, at in pars:
            prev = merged[-1] if merged else None
            if prev and prev[2] >= 0 and at >= 0 and prev[3] and \
                    _LIST_ITEM_RE.match(par) and \
                    quoted[prev[2] + len(prev[0]):at].strip(" \t") == "\n":
                prev[0] = quoted[prev[2]:at + len(par)]
                continue
            merged.append([par, repo, at, par.rstrip().endswith(":")])
        for par, repo, par_pos, _colon in merged:
            repo = repo if repo is not None else (slugs[0] if len(slugs) == 1 else None)
            if repo is None:
                continue
            orig_par = t[par_pos:par_pos + len(par)] if par_pos >= 0 else par
            # emphasis marks are layout, not words: "#30 **depended on** #13"
            # is read as "#30 depended on #13" (offsets kept)
            par = _EMPHASIS_RE.sub(lambda m: " " * len(m.group(0)), par)
            out.append((sid, par, orig_par, repo, default_subject(par_pos, repo)))
    return out


def _claim_item(ref, repo, slugs):
    """A parser reference as {"repo", "number"} in the views' spelling."""
    low = {x.lower(): x for x in slugs}
    sl = ref.get("slug") or repo
    return {"repo": low.get((sl or "").lower(), sl), "number": ref["number"]}


def relation_claims(views, text, as_of=None, subject_story=None):
    """Every relation a sentence asserts, read by `link.parse_relation_claims`
    and judged by `link.relation_holds` with the claim's `query_kind` (the
    generic "waits_on" for waits on / waiting for) — evidence per (subject,
    target) pair, of the kind claimed (an inverse cue from the waiting
    side), dated on or before the day judged (`as_of`, else the window's
    end); a present-tense waiting claim also needs the relation still in
    force that day (both ends open by `link.item_state`, not a ticked task,
    the text current). In-story, cross-story and cross-member evidence
    alike. Scope is ours (`_claim_scope`).

    A negated claim ("is not blocked by", "no longer waits on") asserts
    nothing to check, but one that denies a relation the record holds is a
    contradiction (`relation_denials_contradicted`), judged like the claim it
    denies: a present-tense waiting denial against the relation in force on
    the day, any other ("never depended on", "is not a follow-up to")
    against the record as stated by then.
    For a view without the in-story layer (no `inner_relations` anywhere), a
    pair inside one story passes on a quote in the same sentence that the
    shared parser reads as the same relation to the same target.

    Returns {"relations_not_in_view": [...], "relation_denials_contradicted": [...]}."""
    views = join_members(views)
    slugs = [_slug(v) for v in views]
    target = views[0] if len(views) == 1 else [{"repo": _slug(v), "bundle": v} for v in views]
    by_slug = {_slug(v): v for v in views}
    inner_layer = {_slug(v) for v in views
                   if any("inner_relations" in r for r in (v.get("stories") or {}).get("all") or [])}
    story_of = {}
    for v in views:
        for t in v.get("trains") or []:
            for n in list(t.get("prs") or []) + list(t.get("issues") or []):
                story_of.setdefault((_slug(v), n), set()).add(t["id"])
    bad, denied = [], []

    def quoted_says(orig, kind, tgt, repo):
        for qm in link._QUOTE_RE.finditer(orig):
            for c in link.parse_relation_claims(qm.group(1) or qm.group(2), repo):
                if c["query_kind"] == kind and not c["negated"] and any(
                        x["number"] == tgt["number"] for x in c["targets"]):
                    return True
        return False

    for _sid, par, orig_par, repo, dflt in _claim_scope(views, text, subject_story):
        day = as_of or _period(by_slug.get(repo) or views[0])[1]
        for c in link.parse_relation_claims(par, repo, dflt):
            if c["relation"] in ("mentions", "mentioned_by") or not c["subject"]:
                continue
            kind = c["query_kind"]
            subj = _claim_item(c["subject"], repo, slugs)
            present = c["tense"] == "present" and kind in _PRESENT_KINDS
            snt = " ".join(par[c["span"][0]:c["span"][1]].split())[:200]
            orig = orig_par[c["span"][0]:c["span"][1]]
            for tgt in c["targets"]:
                obj = _claim_item(tgt, repo, slugs)
                # a denial is judged like the claim it denies: "no longer waits
                # on" against the relation in force on the day; "never depended
                # on" or "is not a follow-up to" against the record as stated
                res = link.relation_holds(target, subj, obj, kind, day=day, present=present)
                row = {"claim": kind, "subject": "{}#{}".format(*subj.values()),
                       "target": "{}#{}".format(*obj.values()), "sentence": snt}
                if c["negated"]:
                    if res["holds"]:
                        denied.append(dict(row, problem="denies a relation the record "
                                                        "holds"))
                    continue
                if res["holds"]:
                    continue
                same = story_of.get((subj["repo"], subj["number"]), set()) & \
                    story_of.get((obj["repo"], obj["number"]), set())
                if same and res["reason"] == "no_evidence":
                    if subj["repo"] not in inner_layer:
                        if quoted_says(orig, kind, tgt, repo):
                            continue
                        bad.append(dict(row, reason="no_evidence",
                                        problem="inside one story without a quote"))
                    else:
                        bad.append(dict(row, reason="no_evidence",
                                        problem="inside one story, not in its inner "
                                                "relations"))
                    continue
                bad.append(dict(row, reason=res["reason"],
                                problem=_REASON_TEXT.get(res["reason"], res["reason"])))
    return {"relations_not_in_view": bad, "relation_denials_contradicted": denied}


class RecordAsOf:
    """ONE projection of the record as of a day, for every state, review and
    publication claim of a post: PR state (replayed close / reopen / merge
    events), review state (link.review_state at the end of the day), release
    status (tags dated on or before the day, unknown past the coverage)."""

    def __init__(self, views, day):
        self.day = day
        self.views = {_slug(v): v for v in views}
        self.prs = {(_slug(v), p.get("number")): p for v in views for p in v.get("prs") or []}

    def pr_state(self, repo, number):
        v = self.views.get(repo)
        # "still open" or "not merged" past the coverage is not provable
        if v is not None and not absence_provable(v, self.day):
            return "unknown"
        return pr_state_on(self.prs.get((repo, number)), self.day)

    def review(self, repo, number):
        pr = self.prs.get((repo, number))
        v = self.views.get(repo)
        if v is not None and not absence_provable(v, self.day):
            return "unknown"
        if not pr or v is None or pr_state_on(pr, self.day) in (None, "not_opened"):
            return None
        return (link.review_state(v, pr, _instant(self.day)) or {}).get("state")

    def release(self, repo, module, version):
        v = self.views.get(repo)
        return release_on(v, module, version, self.day) if v is not None else "unknown"


def state_claims(views, text, as_of=None, subject_story=None):
    """One state per fact: every sentence of the issue (stories, quotes and
    comments blanked of quoted text only) that names exactly ONE pull
    request and states its review state, or its open/merged/closed state, or
    that names one module version and says whether it was published, is
    compared with the view's single source (`review_state`, the PR's state,
    the release status) and with what the other sections say of the same
    PR or version. Returns {review_state_mismatch, pr_state_mismatch,
    release_status_mismatch}: each row names the claim, the view's state and
    the sections that stated it. `as_of` (YYYY-MM-DD) judges every claim
    against the record as of that day (`RecordAsOf`) instead of the window's
    end: a post is true on the day it goes out."""
    slugs = [_slug(v) for v in views]
    proj = RecordAsOf(views, as_of) if as_of else None
    prs = {}
    rel = {}
    closed = {}
    for v in views:
        sl = _slug(v)
        for pr in v.get("prs") or []:
            n = pr.get("number")
            if n is None:
                continue
            st = pr_state_on(pr)
            st = st if st != "not_opened" else None
            rs = pr.get("review_state")
            prs[(sl, n)] = {"review": rs.get("state") if isinstance(rs, dict) else None,
                            "state": st}
            if proj is not None:
                prs[(sl, n)] = {"review": proj.review(sl, n), "state": proj.pr_state(sl, n)}
            if st == "closed_unmerged":
                closed[(sl, n)] = pr.get("closed_unmerged") or {}
        for m in (v.get("module_changes") or {}).get("modules") or []:
            mods = {m["area"]} | {x.get("module") for x in
                                  (m.get("versions") or {}).get("published") or [] if x.get("module")}
            for mod in mods:
                for r in module_release(v, m, mod):
                    if r.get("version"):
                        rel[(sl, mod, version_key(r["version"]))] = r.get("status") \
                            if proj is None else proj.release(sl, mod, r["version"])
    known_areas = sorted({k[1] for k in rel}, key=len, reverse=True)

    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))   # noqa: E731
    t = _COMMENT_RE.sub(blank, text)
    t = _FENCE_RE.sub(blank, t)
    t = link._QUOTE_RE.sub(blank, t)
    t = re.sub(r"^\s*>.*$", blank, t, flags=re.M)
    review, pstate, pub = {}, {}, {}
    for sid, chunk in _section_chunks(t):
        for par, repo in _scoped_paragraphs(chunk, slugs):
            if _is_heading(par):
                continue
            repo = repo if repo is not None else (slugs[0] if len(slugs) == 1 else None)
            if repo is None:
                continue
            plain = _strip(_delink_items(par, repo), code=False)
            for snt in _sentences(plain):
                snt_c = _CODE_SPAN_RE.sub(lambda m: m.group(0).strip("`"), snt)
                nums = sorted({r["number"] for r in link.item_refs(snt_c, repo)
                               if not r["evidence"] and (repo, r["number"]) in prs
                               and slug_key(r["repo"] or "") == slug_key(repo)})
                # a conditional, or a count of many PRs ("10 were closed without
                # merging and one, #N, is open"), states nothing about one PR
                cond = bool(_CONDITIONAL_RE.search(snt_c) or _COUNT_SUBJECT_RE.search(snt_c))
                if len(nums) == 1 and not cond:
                    key = (repo, nums[0])
                    for name, states in _review_claim(snt_c):
                        review.setdefault(key, []).append((sid, name, states, snt))
                    for name, rx in _PR_STATE_CLAIMS:
                        if rx.search(snt_c):
                            pstate.setdefault(key, []).append((sid, name, {name}, snt))
                            break
                vers = []
                for vm in _VERSION_RE.finditer(snt_c):
                    toks = [a for a in known_areas if a in snt_c]
                    mod = _attached_module(snt_c, vm.start(), toks) if toks else None
                    if mod is None and len({x for x in toks if not any(
                            y != x and y.startswith(x + "/") for y in toks)}) == 1:
                        # "the module ... was released as 0.13.0": the one module
                        # the sentence names, when that module has the version
                        only = max(toks, key=len)
                        if (repo, only, version_key(vm.group(1))) in rel:
                            mod = only
                    if mod:
                        vers.append((mod, version_key(vm.group(1))))
                vers = sorted(set(vers))
                if len(vers) == 1 and not cond and (repo,) + vers[0] in rel:
                    for name, rx in _PUB_CLAIMS:
                        if rx.search(snt_c):
                            pub.setdefault((repo,) + vers[0], []).append(
                                (sid, name, {"published"} if name == "published" else
                                 {"declared_not_published"}, snt))
                            break

    def verdicts(claims, actual_of, label):
        out = []
        for key, rows in claims.items():
            actual = actual_of(key)
            ok = set.intersection(*[set(r[2]) for r in rows])
            wrong = [r for r in rows if actual is not None and actual not in r[2]]
            if wrong or not ok:
                row = {"repo": key[0], label: key[1] if len(key) == 2 else "/".join(key[1:]),
                       "view": actual,
                       "stated": [{"section": r[0], "claim": r[1],
                                   "sentence": " ".join(r[3].split())[:160]}
                                  for r in (wrong or rows)]}
                if len(slugs) == 1:
                    row.pop("repo")
                out.append(row)
        return out
    # "superseded" / "replaced by" only where the record says so: each such
    # claim read by the shared parser (subject, the item it names) and
    # answered by link.record_superseded; work that landed elsewhere
    # (`content_landed_in`) is said with its basis instead
    supersede = []
    members = views[0] if len(views) == 1 else [{"repo": sl, "bundle": v}
                                                for sl, v in zip(slugs, views)]
    for sid, par, _orig, repo, dflt in _claim_scope(views, text, subject_story):
        for c in link.parse_relation_claims(par, repo, dflt):
            # the parser reads superseded / replaced / consolidated as one
            # relation; only the first two words claim a supersession
            if not _SUPERSEDE_RE.search(c["quote"]) or c["negated"]:
                continue
            snt = " ".join(par[c["span"][0]:c["span"][1]].split())[:160]
            if c["relation"] == "content_landed_in" and c["subject"]:
                pairs = [(_claim_item(c["subject"], repo, slugs), _claim_item(x, repo, slugs))
                         for x in c["targets"]]
            elif c["relation"] == "mentions":
                # "#22 was superseded": an item said superseded, by nothing named
                pairs = [(_claim_item(x, repo, slugs), None) for x in c["targets"]]
            else:
                continue
            for it, by in pairs:
                if (it["repo"], it["number"]) not in closed:
                    continue        # not a PR closed without merging: no such claim
                res = link.record_superseded(members, it, by or it, day=as_of)
                if res["holds"] or (by is None and res["reason"] == "other_item"):
                    continue
                row = {"pr": it["number"], "section": sid, "sentence": snt,
                       "reason": res["reason"]}
                if by is not None:
                    row["by"] = by["number"]
                if len(slugs) > 1:
                    row["repo"] = it["repo"]
                landed = (closed[(it["repo"], it["number"])] or {}).get("content_landed_in")
                if landed:
                    row["content_landed_in"] = [x.get("pr") for x in landed]
                if row not in supersede:
                    supersede.append(row)
    return {
        "superseded_not_in_view": supersede,
        "review_state_mismatch": verdicts(review, lambda k: prs[k]["review"], "pr"),
        "pr_state_mismatch": verdicts(pstate, lambda k: prs[k]["state"], "pr"),
        "release_status_mismatch": verdicts(
            pub, lambda k: rel.get(k) if rel.get(k) in ("published", "declared_not_published")
            else ("unknown" if rel.get(k) == "unknown" else None), "version"),
    }


THEME_CHECK_KEYS = (
    "heading_missing", "theme_trains_missing", "urls_not_in_view", "quotes_not_in_view",
    "items_not_in_view", "modules_not_in_view", "versions_not_in_view", "people_not_in_view",
    "name_mismatch", "do_not_name_named", "affiliation_mismatch", "automation_as_person",
    "review_state_mismatch", "pr_state_mismatch", "release_status_mismatch",
    "superseded_not_in_view", "relations_not_in_view", "relation_denials_contradicted",
    "candidates_worded_as_declared",
    "dates_without_year", "numbers_not_in_view",
    "characterising_words", "modules_not_linked", "versions_not_linked", "markers_malformed")


def check_theme(views, text, theme_id=None, registry_names=None, automation=()):
    """A plumbing theme narrative checked as rigorously as a story: its
    `### <title_hint>` heading and every `in_theme` train named (a link to
    one of its PRs or its root item), and, as the issue check does for the
    plumbing section, every quote verbatim in the record, every URL, item,
    module, version, person and number in the view (or in the PRs the
    paragraph links), a PR's review state and open / merged / closed state
    and a version's release status as the view has them, dates with their
    year and no characterising word. `theme_id` defaults to the theme whose
    title_hint the heading starts with. Pure."""
    views = list(views)
    text = (text or "").replace("\r\n", "\n").strip() + "\n"
    out_l = outline(views, automation=automation)
    sec = {x["id"]: x for x in out_l["sections"]}
    themes = sec["plumbing"].get("themes") or []
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    head = re.match(r"^###\s+(.+?)\s*$", first)
    th = None
    if theme_id:
        th = next((t for t in themes if t.get("id") == theme_id), None)
    elif head:
        h = head.group(1).strip("*_ ").lower()
        th = next((t for t in themes if (t.get("title_hint") or "").lower()
                   and h.startswith((t.get("title_hint") or "").lower())), None)
    res = {"theme": (th or {}).get("id")}
    hint = (th or {}).get("title_hint") or ""
    res["heading_missing"] = [] if head and hint and \
        head.group(1).strip("*_ ").lower().startswith(hint.lower()) else \
        [{"expected": "### " + hint if hint else "### <a theme's title_hint>", "found": first[:80]}]
    # the narrative stands where the issue puts it, so the issue check runs on
    # it unchanged; only the keys about a theme's own text are kept
    title = "# " + (out_l.get("period_label") or "issue")
    issue = "{}\n\nIntro.\n\n## {}\n\n{}".format(
        title, HEADINGS["plumbing"][0], text)
    full = check_newsletter(views, issue, registry_names=registry_names,
                            automation=automation)
    for k in THEME_CHECK_KEYS:
        if k in full:
            res[k] = full[k]
    res["theme_trains_missing"] = [x for x in full.get("theme_trains_missing") or []
                                   if th and x.get("theme") == th.get("id")]
    if th and not head:
        res["theme_trains_missing"] = [
            {"key": r["key"], "theme": th["id"]}
            for r in sec["stories"].get("in_theme") or [] if r.get("theme") == th["id"]]
    res["ok"] = th is not None and not any(res.get(k) for k in THEME_CHECK_KEYS)
    res["cannot_check"] = ["that a theme paragraph describes its pull requests (only that "
                           "what it states is in the record)"]
    return res


CANNOT_CHECK = (
    "that a paraphrase says what the changelog or the record says (only quoted text is "
    "compared verbatim)",
    "that a 'why it matters' or 'for whom' line is supported by the record",
    "that a number is attached to the right fact (a number is only required to be one "
    "the outline or view holds)",
    "that a contribution class or a release status is attached to the right pull request "
    "(only that one is stated)",
    "that a link points to the folder of the module its text names (only that the URL is "
    "known)",
    "that a plumbing theme paragraph describes its pull requests",
    "tone and fairness beyond the listed characterising words",
    "anything inside a story block beyond link.py's story check",
)




# --- daily social posts (drafts for the owner to approve) ----------------------
#
# One post per story, at the first event a reader can act on; plumbing rolls
# into ONE weekly post per repository, covering every theme with activity. reference/social.md has the
# rules, the ledger and what the posting step needs. Nothing here schedules or
# publishes anything: the outline lists what became postable on a day.

SOCIAL_HOLD_DAYS = 3        # a merged story waits this many days for its release
SOCIAL_CHANNELS = ("x", "linkedin")
X_LIMIT = 280               # X's weighted length, a link counted as X_LINK_WEIGHT
X_LINK_WEIGHT = 23          # every URL is shortened to a fixed-length link on X
LINKEDIN_LIMIT = 1200       # our cap for a short LinkedIn post (plain text)
LINKEDIN_HASHTAGS = 3
SOCIAL_TRIGGERS = ("published", "merged_not_published", "merged_release_unknown",
                   "merged_no_release",
                   "new_module_proposed", "breaking_declared_open", "owner_added",
                   "orphaned", "deprecated", "closed_not_planned", "closed_after_post")
ORPHAN_LABEL_HOLD_DAYS = 7  # a label-only orphan signal waits this long for an owner
CLOSED_QUICK_HOURS = 24     # closed by its own author within this of opening: not posted


def _day(ts):
    return (ts or "")[:10] or None


def _add_days(day, n):
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=n)).isoformat()


def _open_pr_breaking(view, number):
    """Breaking changes an open PR's own commits declare in its CHANGELOG
    files (a commit another PR owns, `belongs_to_pr`, is not its own). Within
    one patch, the items under an added `### Breaking Changes` heading. Across
    commits (patches carry no line positions): a later patch that removes the
    "- None" placeholder of a Breaking heading an earlier commit added, and
    adds items under no heading of its own, declares those items — only when
    the file's open placeholders are all under Breaking headings, so the
    removed line cannot be another section's. [{path, items, commit, at}]."""
    h = view.get("pr_history")
    rows = h.get("prs") if isinstance(h, dict) else h
    row = next((r for r in rows or [] if isinstance(r, dict) and r.get("number") == number), None)
    if not row:
        return []
    commits = {c.get("sha"): c for c in row.get("commits") or [] if isinstance(c, dict)}
    order = {c.get("sha"): i for i, c in enumerate(row.get("commits") or [])
             if isinstance(c, dict)}
    patches = [q for q in row.get("patches") or [] if "CHANGELOG" in (q.get("path") or "")
               and (commits.get(q.get("commit")) or {}).get("belongs_to_pr") in (None, number)]
    patches.sort(key=lambda q: ((commits.get(q.get("commit")) or {}).get("committed_at") or "",
                                order.get(q.get("commit"), 0)))
    placeholders = {}            # path -> {"breaking": n, "other": n} open "- None" lines
    out = []

    def found(path, items, sha):
        c = commits.get(sha) or {}
        out.append({"path": path, "items": items, "commit": sha,
                    "at": c.get("committed_at") or c.get("authored_at")})
    for q in patches:
        path, added = q["path"], q.get("added") or []
        ph = placeholders.setdefault(path, {"breaking": 0, "other": 0})
        removed_none = sum(1 for x in q.get("removed") or [] if x.strip().lower() == "- none")
        section, items, lead = None, [], []
        for line in added:
            if line.startswith("#"):
                if section == "breaking" and items:
                    found(path, items, q.get("commit"))
                section = "breaking" if line.strip().lower().startswith("### breaking") else \
                    ("other" if line.startswith("###") else None)
                items = []
                continue
            if not line.startswith("- "):
                continue
            if line.strip().lower() == "- none":
                if section in ("breaking", "other"):
                    ph[section] += 1
                continue
            if section == "breaking":
                items.append(line[2:].strip())
            elif section is None:
                lead.append(line[2:].strip())
        if section == "breaking" and items:
            found(path, items, q.get("commit"))
        if lead and removed_none and ph["breaking"] and not ph["other"]:
            found(path, lead, q.get("commit"))
            ph["breaking"] -= min(removed_none, ph["breaking"])
    return out


def _declared_breaking_prs(view):
    """{module area: {PR numbers whose changelog declares a breaking item}}."""
    out = {}
    for m in (view.get("module_changes") or {}).get("modules") or []:
        for d in (m.get("breaking") or {}).get("declared") or []:
            for src in d.get("sources") or []:
                if isinstance(src, dict) and src.get("pr") is not None:
                    out.setdefault(m["area"], set()).add(src["pr"])
    return out


def _unmerged(pr):
    """A PR that did not merge: open, or closed without merging."""
    return bool(pr) and not pr.get("merged") and pr.get("state") in ("open", "closed")


def _closed_state(kind, n, item, c):
    """What a closing post must state: the item, that it closed without
    landing, the reason as stated or None ("no reason is stated"), what
    superseded it when the record says so, where its content landed."""
    out = {"item": {"type": kind, "number": n, "url": item.get("url"), "title": item.get("title")},
           "closed_on": _day(c["at"]), "reason": c.get("reason"),
           "state": "closed without merging" if kind == "pr" else "closed"}
    for k in ("superseded_by", "quote", "landed_in"):
        if c.get(k):
            out[k] = c[k]
    return out


def _story_people(view, numbers, automation=()):
    out = []
    prs = {p.get("number"): p for p in view.get("prs") or []}
    for n in numbers:
        a = (prs.get(n) or {}).get("author")
        if not a or a in [x["login"] for x in out]:
            continue
        if is_automation(view, a, automation):
            row = {"login": a, "automation": True}
            obo = on_behalf_of(view, a, n)
            if obo:
                row["on_behalf_of"] = obo
        else:
            p = _person(view, a)
            row = {k: p.get(k) for k in ("login", "name", "affiliation", "do_not_name")
                   if p.get(k) is not None}
        out.append(row)
    return out


def _closing(view, kind, item, automation=()):
    """How an item closed without landing, as the record states it:
    {at, by, reason, superseded_by?, landed_in?} or None when it is open or
    merged. `reason` is the stated one (an issue's state_reason, a PR's
    `superseded_by` with its quote), else None ("no reason is stated")."""
    if kind == "issue":
        c = item.get("closing") or {}
        if item.get("state") != "closed" or not c.get("at") or c.get("state_reason") == "completed":
            return None
        return {"at": c["at"], "by": c.get("by"), "reason": c.get("state_reason")}
    if item.get("state") != "closed" or item.get("merged") or not item.get("closed_at"):
        return None
    closed_events = sorted((e for e in item.get("lifecycle") or []
                            if isinstance(e, dict) and e.get("event") == "closed"),
                           key=lambda e: e.get("created_at") or "")
    # the final close (closed, reopened, closed again by someone else)
    by = closed_events[-1].get("actor") if closed_events else None
    cu = item.get("closed_unmerged") or {}
    out = {"at": item["closed_at"], "by": by, "reason": None}
    if cu.get("superseded_by"):
        out["reason"] = "superseded"
        out["superseded_by"] = cu["superseded_by"]
        out["quote"] = cu.get("quote")
    if cu.get("content_landed_in"):
        out["landed_in"] = [{"pr": x.get("pr"), "basis": x.get("basis"),
                             "url": ((next((p for p in view.get("prs") or []
                                            if p.get("number") == x.get("pr")), {}) or {})
                                     .get("url"))} for x in cu["content_landed_in"]]
    return out


def _closure_exclusion(view, kind, item, closing, automation=()):
    """Why a closure is not posted (it folds into the weekly post as a
    count), read from the record: `automation_pr` (the PR's author is an
    automation account), `duplicate_by_automation` (an issue closed as a
    duplicate by an automation account), `quick_own_close` (closed by its own
    author within CLOSED_QUICK_HOURS of opening); else None."""
    author = item.get("author")
    if kind == "pr" and author and is_automation(view, author, automation):
        return "automation_pr"
    if kind == "issue" and closing.get("reason") == "duplicate" and closing.get("by") and \
            is_automation(view, closing["by"], automation):
        return "duplicate_by_automation"
    if closing.get("by") and author and login_key(closing["by"]) == login_key(author) and \
            item.get("created_at"):
        a = datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(closing["at"].replace("Z", "+00:00"))
        if (b - a).total_seconds() <= CLOSED_QUICK_HOURS * 3600:
            return "quick_own_close"
    return None


def social_candidates(views, hold_days=SOCIAL_HOLD_DAYS, automation=()):
    """Every post the window's records allow, each with the day it becomes
    postable (`day`), why (`trigger`), what it must state (`must_state`) and
    a stable `post_id`. Stories: one post each (the earliest trigger wins),
    plus one closing post (`closed_after_post`) when a posted story is
    withdrawn; a user-facing closure posts as `closed_not_planned` unless an
    exclusion folds it into the weekly count (`excluded`). Ownership and
    lifecycle events: one post per module and kind. Plumbing: ONE weekly post
    per week covering every theme with activity, biggest first (`weekly`)."""
    views = join_members(views)
    multi = len(views) > 1
    out_l = outline(views, automation=automation)
    sec = {x["id"]: x for x in out_l["sections"]}
    posts, weekly, loops, excluded = [], [], [], []
    for v in views:
        sl = _slug(v)
        period = _period(v)
        prs = {p.get("number"): p for p in v.get("prs") or []}
        iss = {i.get("number"): i for i in v.get("issues") or []}
        trains = {t["id"]: t for t in v.get("trains") or []}
        declared = _declared_breaking_prs(v)
        for r in sec["stories"].get("all") or []:
            if multi and r.get("repo") != sl:
                continue
            if r.get("group") == "plumbing" or r.get("plan") == "in_theme":
                continue
            t = trains.get(r["id"]) or {}
            numbers = t.get("prs") or r.get("prs") or []
            mods = [{"area": m["area"], "url": m.get("url"),
                     "info": m.get("info") or module_info(v, m["area"])}
                    for m in r.get("modules") or []]
            base = {"post_id": "story:{}:{}".format(sl, r["id"]), "kind": "story",
                    "story": r["key"], "group": r.get("group"), "title": r.get("title"),
                    "url": r.get("url"), "repo": sl if multi else None}
            options = []
            merged = sorted(_day(prs[n].get("merged_at")) for n in numbers
                            if (prs.get(n) or {}).get("merged") and prs[n].get("merged_at"))
            if r.get("group") == "landed" and merged:
                mday = merged[-1]
                rel = [x for x in r.get("released") or [] if isinstance(x, dict)]
                pub = sorted((x for x in rel if x.get("status") == "published" and x.get("date")
                              and _day(x["date"]) >= mday), key=lambda x: _day(x["date"]))
                in_hold = [x for x in pub if _day(x["date"]) <= _add_days(mday, hold_days)]
                # breaking only when a declaration's source PR is in THIS train
                state = {"merged_on": mday, "breaking": "declared" if any(
                    declared.get(m["area"], set()) & set(numbers) for m in mods) else None}
                if in_hold:
                    pday = _day(in_hold[0]["date"])
                    options.append((pday, "published", dict(state, released=[
                        {k: x.get(k) for k in ("module", "version", "url", "registry", "date")}
                        for x in pub if _day(x["date"]) == pday])))
                elif rel and all(x.get("status") == "no_version_change" for x in rel):
                    options.append((mday, "merged_no_release", dict(state, release="no version change")))
                else:
                    hold_end = _add_days(mday, hold_days)
                    # what is declared and not published as of the post's day: a
                    # version declared but untagged, or one published only later
                    later = [x for x in rel if x.get("status") == "declared_not_published"
                             or (x.get("status") == "published" and _day(x.get("date"))
                                 and _day(x["date"]) > hold_end)]
                    unknown = [x for x in rel if x.get("status") == "unknown"]
                    if not unknown and later and not absence_provable(v, hold_end, tags=True) \
                            and hold_end <= (period[1] or hold_end):
                        # tags not read for the whole period: "not yet published"
                        # cannot be told even inside the coverage
                        unknown = [dict(x, reason="tags not read for the whole period")
                                   for x in later]
                    if unknown:
                        # incomplete tag coverage proves nothing: the post keeps the
                        # uncertainty ("release status not known")
                        options.append((hold_end, "merged_release_unknown", dict(
                            state, release="release status not known",
                            unknown=[{k: x.get(k) for k in ("module", "version", "reason")}
                                     for x in unknown], held_from=mday)))
                    else:
                        st_ = dict(state, release="merged, not yet published",
                                   declared=[{k: x.get(k) for k in ("module", "version", "status")}
                                             for x in later], held_from=mday)
                        if not absence_provable(v, hold_end, tags=True):
                            # the view's tags do not reach the due day: "not yet
                            # published" cannot be told; pending until a view covers it
                            st_["pending"] = "the view's coverage ends {}; due {}".format(
                                period[1], hold_end)
                        options.append((hold_end, "merged_not_published", st_))
            for area in r.get("new_module_proposed") or []:
                tree = ((v.get("module_ownership") or {}).get("proposed_modules") or {}).get(area) or {}
                # the PR that adds the entry file (the tree's `prs`), not any open
                # PR of the train
                proposing = [n for n in tree.get("prs") or [] if n in numbers] or \
                    [n for n in numbers if _unmerged(prs.get(n))]
                # as of the event's day: a proposing PR closed unmerged later still
                # opened the proposal (its closing gets the `:closed` post)
                opened = [_day(prs[n].get("created_at")) for n in proposing
                          if _unmerged(prs.get(n)) and prs[n].get("created_at")]
                # not before the pull request exists: the later of the commit
                # adding the entry file and the PR's opening
                days = [x for x in (_day(tree.get("committed_at")),
                                    min(opened) if opened else None) if x]
                day = max(days) if days else None
                if day and not any(pr_state_on(prs.get(n), day) == "open" for n in proposing):
                    day = None          # every proposing PR was closed by then
                if day:
                    options.append((day, "new_module_proposed", {
                        "module": area, "info": tree.get("info") or (r.get("module_info") or {}).get(area),
                        "state": "proposed in an open pull request, not published"}))
            for n in numbers:
                if not _unmerged(prs.get(n)):
                    continue          # a merged PR is posted by its release
                for b in _open_pr_breaking(v, n):
                    if b.get("at"):
                        bday = max(_day(b["at"]), _day(prs[n].get("created_at")) or _day(b["at"]))
                        if pr_state_on(prs[n], bday) != "open":
                            continue  # closed before it declared
                        options.append((bday, "breaking_declared_open", {
                            "pr": n, "changelog": b["path"], "items": b["items"],
                            "state": "declared breaking in an open pull request, not merged"}))
            options = [o for o in options if o[0] and _in_period(o[0], period)
                       or (o[1] in ("merged_not_published", "merged_release_unknown") and o[0])]
            # a user-facing request or change closed without landing (closed group)
            closes = []
            items = [("issue", n, iss.get(n)) for n in t.get("issues") or r.get("issues") or []] + \
                [("pr", n, prs.get(n)) for n in numbers]
            for kind, n, it in items:
                c = _closing(v, kind, it or {}, automation) if it else None
                if c and _in_period(c["at"], period):
                    closes.append((kind, n, it, c))
            if r.get("group") == "closed" and closes and not options:
                kind, n, it, c = sorted(closes, key=lambda x: x[3]["at"])[-1]
                why = _closure_exclusion(v, kind, it, c, automation)
                if why:
                    excluded.append({"story": r["key"], "item": n, "type": kind,
                                     "day": _day(c["at"]), "exclusion": why, "repo": sl})
                    continue
                options.append((_day(c["at"]), "closed_not_planned", dict(
                    _closed_state(kind, n, it, c), modules=[])))
            if not options:
                continue
            day, trig, state = sorted(options, key=lambda o: (o[0], SOCIAL_TRIGGERS.index(o[1])))[0]
            proj = RecordAsOf([v], day)
            # closing the loop: a story that already had a post gets ONE closing
            # post when it is closed without merging or withdrawn
            later = [x for x in closes if _day(x[3]["at"]) > day and trig != "closed_not_planned"]
            final = sorted(later, key=lambda x: x[3]["at"])[-1] if later else None
            # eligible only when, on the day of the FINAL closure, every PR of the
            # train is closed unmerged (replayed, not the window's end)
            still = [n for n in numbers if final and
                     pr_state_on(prs.get(n), _day(final[3]["at"])) in ("open", "merged")
                     or (prs.get(n) or {}).get("merged")]
            if final and not still:
                kind, n, it, c = final
                loop = dict(base, post_id=base["post_id"] + ":closed", first_post_id=base["post_id"],
                            day=_day(c["at"]), trigger="closed_after_post",
                            must_state=dict(_closed_state(kind, n, it, c), first_trigger=trig))
                loops.append(loop)
            posts.append(dict(base, day=day, trigger=trig, must_state=dict(
                state, modules=mods,
                # each PR as it stood on the post's day (the projection)
                prs=[{"number": n, "url": (prs.get(n) or {}).get("url"),
                      "state": pr_state_on(prs.get(n), day),
                      "review_state": proj.review(sl, n)} for n in numbers],
                people=_story_people(v, numbers, automation))))
        # ownership and lifecycle events a reader can act on
        mo = v.get("module_ownership") or {}
        seen = set()
        added = {}
        for e in mo.get("events") or []:
            if isinstance(e, dict) and e.get("kind") == "owner_added" and e.get("module"):
                added.setdefault(e["module"], []).append(_day(e.get("date") or e.get("at")))
        replacements = {d.get("module"): d.get("replacement")
                        for d in mo.get("deprecated_modules") or [] if isinstance(d, dict)}
        for e in mo.get("events") or []:
            if not isinstance(e, dict) or e.get("kind") not in ("owner_added", "orphaned",
                                                               "deprecated"):
                continue
            if not e.get("module") or not _in_period(e.get("date") or e.get("at"), period):
                continue
            pid = "{}:{}:{}:{}".format(e["kind"], sl, e["module"], e.get("date"))
            key = (e["kind"], e["module"])
            if key in seen:
                continue
            eday = _day(e.get("date") or e.get("at"))
            post_day = eday
            orphan_pending = None
            if e["kind"] == "orphaned" and (e.get("source") or {}).get("type") == "pr":
                # only a label on a PR (no marker file in the module folder): held
                # ORPHAN_LABEL_HOLD_DAYS for an owner; an owner in that time
                # suppresses it, nothing is left for a reader to act on
                if any(eday <= a <= _add_days(eday, ORPHAN_LABEL_HOLD_DAYS)
                       for a in added.get(e["module"], [])):
                    excluded.append({"post_id": pid, "exclusion": "orphan_label_then_owner",
                                     "day": eday, "repo": sl})
                    continue
                post_day = _add_days(eday, ORPHAN_LABEL_HOLD_DAYS)
                if not absence_provable(v, post_day):
                    # "no owner since" past the coverage is not proof: pending
                    orphan_pending = "the view's coverage ends {}; due {}".format(
                        period[1], post_day)
            seen.add(key)
            posts.append({"post_id": pid, "kind": "module_event", "day": post_day,
                          "trigger": e["kind"], "repo": sl if multi else None,
                          "must_state": {"module": e["module"],
                                         "module_url": module_url(v, e["module"])[0],
                                         "info": module_info(v, e["module"]),
                                         # named by display name only; a nameless or
                                         # do_not_name owner is counted, not named
                                         "people": [_person(v, x)["name"]
                                                    for x in e.get("people") or []
                                                    if _person(v, x).get("name")
                                                    and not _person(v, x).get("do_not_name")],
                                         "people_unnamed": sum(
                                             1 for x in e.get("people") or []
                                             if not _person(v, x).get("name")
                                             or _person(v, x).get("do_not_name")),
                                         "source": e.get("source"), "via": e.get("via"),
                                         "marker_text": e.get("marker_text"),
                                         **({"pending": orphan_pending} if orphan_pending
                                            else {}),
                                         **({"replacement": (dict(
                                             replacements[e["module"]],
                                             module_url=module_url(
                                                 v, replacements[e["module"]].get("module"))[0])
                                             if replacements.get(e["module"]) else None)}
                                            if e["kind"] == "deprecated" else {})}})
        # plumbing: ONE weekly post per week, due on the week's Sunday (pending
        # until the coverage reaches it), covering every theme with activity
        # that week, biggest first
        weeks = {}
        for th in sec["plumbing"].get("themes") or []:
            if multi and th.get("repo") != sl:
                continue
            for p in th.get("prs") or []:
                pr = prs.get(p.get("number")) or {}
                for ts, what in ((pr.get("merged_at"), "merged"), (pr.get("created_at"), "opened")):
                    if ts and _in_period(ts, period):
                        y, w, _ = datetime.date.fromisoformat(_day(ts)).isocalendar()
                        row = weeks.setdefault((y, w), {}).setdefault(th["id"], {
                            "theme": th["id"], "title_hint": th.get("title_hint"),
                            "merged": set(), "opened": set()})
                        row[what].add(p["number"])
        mine = [x for x in excluded if x.get("repo") == sl]
        for x in mine:
            if x.get("day") and x.get("exclusion") != "orphan_label_then_owner":
                y, w, _ = datetime.date.fromisoformat(x["day"]).isocalendar()
                weeks.setdefault((y, w), {})
        for (y, w), themes in sorted(weeks.items()):
            sunday = datetime.date.fromisocalendar(y, w, 7).isoformat()
            # the week's counts are complete only when the coverage reaches its
            # Sunday; until then the post is pending (due on the Sunday)
            day = sunday
            week_pending = None if absence_provable(v, sunday) else \
                "the view's coverage ends {}; the week ends {}".format(period[1], sunday)
            rows = sorted(themes.values(), key=lambda t: (-len(t["merged"]), -len(t["opened"]),
                                                         t["theme"]))
            folded = [x for x in mine if x.get("day") and x.get("exclusion") !=
                      "orphan_label_then_owner" and
                      datetime.date.fromisoformat(x["day"]).isocalendar()[:2] == (y, w)]
            if not rows and not folded:
                continue
            weekly.append({"post_id": "weekly:{}:{}-W{:02d}".format(sl, y, w),
                           "kind": "weekly", "trigger": "weekly_engineered", "day": day,
                           "repo": sl if multi else None,
                           "must_state": dict({"week": "{}-W{:02d}".format(y, w),
                                               # the span the counts cover: a week that
                                               # starts before the view covers only part
                                               "covers_from": max(datetime.date.fromisocalendar(
                                                   y, w, 1).isoformat(), period[0]),
                                               "covers_to": min(sunday, period[1]),
                                               "themes": [dict(t, merged=sorted(t["merged"]),
                                                               opened=sorted(t["opened"]))
                                                          for t in rows],
                                               "closures_not_posted": folded},
                                              **({"pending": week_pending} if week_pending
                                                 else {}))})
    return {"posts": posts + loops, "weekly": weekly, "excluded": excluded}


def read_ledger(path_or_doc):
    """The ledger of what is already out: {"kind": "social_ledger", "posts":
    [{post_id, day, channels: [..], urls?: {channel: url}}]} -> set of post_ids."""
    doc = path_or_doc
    if isinstance(path_or_doc, str):
        with open(path_or_doc, encoding="utf-8") as fh:
            doc = json.load(fh)
    if not isinstance(doc, dict) or not isinstance(doc.get("posts"), list):
        raise ValueError("not a social ledger: needs {\"posts\": [...]}")
    out = set()
    for p in doc["posts"]:
        if not isinstance(p, dict) or not p.get("post_id"):
            raise ValueError("a ledger row needs post_id")
        out.add(p["post_id"])
    return out


def daily_outline(views, day, posted=(), hold_days=SOCIAL_HOLD_DAYS, automation=(),
                  candidates=None):
    """What became postable on `day`: story and module-event posts whose
    trigger day is `day`, the weekly plumbing items due that day, the stories
    held for their release (`held`, with the day they post), minus what the
    ledger says is out (`already_posted`)."""
    cand = candidates or social_candidates(views, hold_days=hold_days, automation=automation)
    posted = set(posted or ())
    pending = [p for p in cand["posts"] if p["day"] == day and p["must_state"].get("pending")]
    rows = [p for p in cand["posts"] if p["day"] == day and not p["must_state"].get("pending")]
    pending += [w for w in cand["weekly"] if w["day"] == day and w["must_state"].get("pending")]
    weekly = [w for w in cand["weekly"] if w["day"] == day and not w["must_state"].get("pending")]
    held = [{"post_id": p["post_id"], "story": p.get("story"), "merged_on":
             p["must_state"].get("held_from"), "posts_on": p["day"]}
            for p in cand["posts"] if p["trigger"] in ("merged_not_published",
                                                       "merged_release_unknown")
            and p["must_state"].get("held_from") and p["must_state"]["held_from"] <= day < p["day"]]
    return {"kind": "daily_outline", "day": day, "hold_days": hold_days,
            "channels": list(SOCIAL_CHANNELS),
            "limits": {"x": X_LIMIT, "x_link_weight": X_LINK_WEIGHT,
                       "linkedin": LINKEDIN_LIMIT, "linkedin_hashtags": LINKEDIN_HASHTAGS},
            "posts": [p for p in rows if p["post_id"] not in posted],
            "weekly": [w for w in weekly if w["post_id"] not in posted],
            "held": held,
            "pending": [{"post_id": p["post_id"], "story": p.get("story"),
                         "reason": p["must_state"]["pending"]} for p in pending],
            "already_posted": sorted(p["post_id"] for p in rows + weekly
                                     if p["post_id"] in posted)}


# --- the post check -------------------------------------------------------------

_POST_RE = re.compile(r"<!--\s*post:\s*(\S+)\s+channel:\s*(\w+)\s+day:\s*(\d{4}-\d\d-\d\d)\s*-->"
                      r"(.*?)<!--\s*/post\s*-->", re.S)
_HANDLE_RE = re.compile(r"(?<![\w/.@])@([A-Za-z0-9_][A-Za-z0-9_.-]*)")
_HASHTAG_RE = re.compile(r"(?<![\w&/])#([A-Za-z][\w]*)")
_MARKDOWN_RE = re.compile(r"\[[^\]\n]*\]\([^)]*\)|\*\*|__|`|^\s{0,3}#{1,6}\s", re.M)
POST_CHECK_KEYS = (  # plus release_claimed_while_unknown, set per post
    "urls_not_in_view", "quotes_not_in_view", "items_not_in_view", "modules_not_in_view",
    "versions_not_in_view", "people_not_in_view", "do_not_name_named", "affiliation_mismatch",
    "automation_as_person", "review_state_mismatch", "pr_state_mismatch",
    "release_status_mismatch", "superseded_not_in_view", "relations_not_in_view",
    "relation_denials_contradicted",
    "candidates_worded_as_declared",
    "dates_without_year", "numbers_not_in_view", "characterising_words")


def _post_urls(text):
    """The URLs of a plain-text post, trailing sentence punctuation removed
    (as `post_facts` and the newsletter check read them)."""
    return [u.rstrip(".,;:!?)") for u in _URL_RE.findall(text or "")]


_POST_MARKER_RE = re.compile(r"<!--\s*/?\s*post\b[^>]*?-->|<!--\s*/?\s*post\b", re.I)


def post_file_problems(text):
    """What the check cannot account for in a drafts file: a post marker
    outside a well-formed block (malformed, unterminated, stray closing),
    a duplicate (post_id, channel), and any other non-blank text outside
    the blocks. [{problem, at, text}]."""
    out, spans, seen = [], [], set()
    for m in _POST_RE.finditer(text or ""):
        spans.append((m.start(), m.end()))
        key = (m.group(1), m.group(2).lower())
        if key in seen:
            out.append({"problem": "duplicate_post", "at": m.start(),
                        "text": "{} {}".format(*key)})
        seen.add(key)
        if _POST_MARKER_RE.search(m.group(4)):
            out.append({"problem": "marker_inside_post", "at": m.start(),
                        "text": m.group(1)})
    rest, last = [], 0
    for a, b in spans:
        rest.append((last, text[last:a]))
        last = b
    rest.append((last, text[last:]))
    for off, chunk in rest:
        for m in _POST_MARKER_RE.finditer(chunk):
            out.append({"problem": "malformed_or_unterminated_marker", "at": off + m.start(),
                        "text": m.group(0)[:80]})
        stray = _POST_MARKER_RE.sub("", chunk).strip()
        if stray:
            out.append({"problem": "text_outside_posts", "at": off,
                        "text": " ".join(stray.split())[:80]})
    return out


def x_length(text):
    """X's weighted length: every URL counts X_LINK_WEIGHT; a code point in the
    Latin / general-punctuation ranges counts 1, any other (most emoji, CJK) 2."""
    n = 0
    # a URL counts X_LINK_WEIGHT; punctuation after it is text
    rest = _URL_RE.sub(lambda m: "\0" + m.group(0)[len(m.group(0).rstrip(".,;:!?)")):], text)
    n += rest.count("\0") * X_LINK_WEIGHT
    for ch in rest.replace("\0", ""):
        o = ord(ch)
        n += 1 if (o <= 4351 or 8192 <= o <= 8205 or 8208 <= o <= 8223 or 8242 <= o <= 8247) \
            else 2
    return n


def post_facts(text, names=None):
    """The facts a post states, which a voice pass may not change: URLs,
    versions, item numbers, other numbers, quotes, @handles, dates, and the
    people it names (`names`: {display name: login}, as canonical logins)."""
    t = text
    urls = sorted(set(_post_urls(t)))
    t2 = _URL_RE.sub(" ", t)
    quotes = sorted({m.group(1) or m.group(2) for m in link._QUOTE_RE.finditer(t2)})
    dates = sorted({m.group(0) for m in link._DATE_IN_PROSE_RE.finditer(t2)})
    t3 = link._DATE_IN_PROSE_RE.sub(" ", t2)
    versions = sorted(set(_VERSION_RE.findall(t3)))
    # item references by the shared resolver (#N; owner/repo#N kept whole)
    refs = [r for r in link.item_refs(t3) if r["form"] in ("local", "short")]
    items = sorted({("{}#{}".format(r["slug"], r["number"]) if r["slug"] else str(r["number"]))
                    for r in refs})
    for r in refs:
        a_, b_ = r["span"]
        t3 = t3[:a_] + " " * (b_ - a_) + t3[b_:]
    t4 = _VERSION_RE.sub(" ", t3)
    numbers = sorted(set(x.replace(",", "") for x, _ in _NUMBER_RE.findall(t4)))
    handles = sorted(set(_HANDLE_RE.findall(t2)))
    modules = sorted({m.group(0).rstrip("/.-") for m in _AVM_PATH_RE.finditer(t2)})
    people = sorted({login_key(lg) for nm, lg in (names or {}).items()
                     if nm and re.search(r"(?<!\w)" + re.escape(nm) + r"(?!\w)", t2)})
    return {"urls": urls, "quotes": quotes, "dates": dates, "versions": versions,
            "items": items, "numbers": numbers, "handles": handles, "people": people,
            "modules": modules}


def _social_numbers(obj, out):
    """The numbers a row states: its ints and the size of each of its lists."""
    if isinstance(obj, dict):
        for x in obj.values():
            _social_numbers(x, out)
    elif isinstance(obj, list):
        out.add(str(len(obj)))
        for x in obj:
            _social_numbers(x, out)
    elif isinstance(obj, int) and not isinstance(obj, bool):
        out.add(str(obj))


def _confirmed_handles(registry, channel, hidden=()):
    """{handle_lower} the people registry confirms for a channel:
    people[login].social[channel] = {"handle": "...", "confirmed": true}. A
    do_not_name person's handle is never allowed (`hidden` logins and the
    registry's own do_not_name flag, logins compared case-insensitively)."""
    out = set()
    hidden = {login_key(x) for x in hidden}
    for login, row in ((registry or {}).get("people") or {}).items():
        if not isinstance(row, dict) or row.get("do_not_name") or login_key(login) in hidden:
            continue
        s = (row.get("social") or {}).get(channel)
        if isinstance(s, dict) and s.get("confirmed") and s.get("handle"):
            out.add(s["handle"].lstrip("@").lower())
    return out


def check_posts(views, text, registry=None, against=None, hold_days=SOCIAL_HOLD_DAYS,
                automation=(), candidates=None):
    """Check every post block of a drafts file: its limits (X weighted length,
    LinkedIn length and hashtags, plain text), what its row says it must state
    (module link, version and release status, breaking or not), the issue's
    record checks on its text (quotes, items, versions, numbers, states,
    names, do_not_name, characterising words), and @handles only where the
    registry confirms them for the channel. `against` (an earlier version of
    the same file) adds the voice-pass rule: the facts of each post must be
    unchanged (`facts_changed`). Pure."""
    views = join_members(views)
    cand = candidates or social_candidates(views, hold_days=hold_days, automation=automation)
    by_id = {p["post_id"]: p for p in cand["posts"] + cand["weekly"]}
    registry_names = None
    if registry:
        registry_names = {lg: (e or {}).get("name") for lg, e in (registry.get("people") or {}).items()
                          if isinstance(e, dict) and e.get("do_not_name")}
    names, people_rows, hidden_logins = {}, {}, set()
    prs_url = {(_slug(v), x.get("number")): x.get("url") for v in views for x in v.get("prs") or []}
    for v in views:
        hidden_logins |= _do_not_name(v)
        for login in set(v.get("people") or {}) | set(((v.get("affiliations") or {})
                                                        .get("people") or {})):
            pr_ = _person(v, login)
            if pr_.get("name") and not pr_.get("do_not_name"):
                names[pr_["name"]] = login
                people_rows[pr_["name"]] = pr_
    for lg, e in ((registry or {}).get("people") or {}).items():
        if isinstance(e, dict) and e.get("do_not_name"):
            hidden_logins.add(lg)
    before = {}
    if against:
        before = {(m.group(1), m.group(2)): post_facts(m.group(4).strip(), names)
                  for m in _POST_RE.finditer(against)}
    results, ok = [], True
    code_free = None
    blocks = list(_POST_RE.finditer(text or ""))
    if not blocks:
        return {"ok": False, "posts": [], "error": "no <!-- post: ID channel: C day: D --> block"}
    file_errors = post_file_problems(text)
    for m in blocks:
        pid, ch, day, body = m.group(1), m.group(2).lower(), m.group(3), m.group(4).strip()
        r = {"post_id": pid, "channel": ch, "day": day}
        row = by_id.get(pid)
        if ch not in SOCIAL_CHANNELS:
            r["channel_unknown"] = ch
        if row is None:
            r["post_unknown"] = pid
        elif (row.get("must_state") or {}).get("pending"):
            r["post_pending"] = row["must_state"]["pending"]
        elif row["day"] != day:
            r["day_mismatch"] = {"outline": row["day"], "written": day}
        # limits and form
        if ch == "x":
            r["length"] = x_length(body)
            if r["length"] > X_LIMIT:
                r["too_long"] = {"limit": X_LIMIT, "length": r["length"]}
        else:
            r["length"] = len(body)
            if r["length"] > LINKEDIN_LIMIT:
                r["too_long"] = {"limit": LINKEDIN_LIMIT, "length": r["length"]}
            tags = _HASHTAG_RE.findall(_URL_RE.sub(" ", body))
            if len(tags) > LINKEDIN_HASHTAGS:
                r["too_many_hashtags"] = tags
        md = _MARKDOWN_RE.findall(_URL_RE.sub(" ", body))
        if md:
            r["markdown"] = md[:5]
        # handles only where confirmed for this channel
        ok_handles = _confirmed_handles(registry, ch, hidden_logins)
        bad = [h for h in _HANDLE_RE.findall(_URL_RE.sub(" ", body)) if h.lower() not in ok_handles]
        if bad:
            r["handles_not_confirmed"] = bad
        # what the row says the post must state
        missing = []
        if row is not None:
            ms = row.get("must_state") or {}
            low = body.lower()
            urls = set(_post_urls(body))
            mods = ms.get("modules") or ([{"area": ms["module"], "url": ms.get("module_url")}]
                                         if ms.get("module") else [])
            if mods and not any(m.get("url") in urls or m["area"] in body for m in mods):
                missing.append("the module (path or link)")
            trig = row["trigger"]
            event_words = {"owner_added": r"\bowner", "orphaned": r"\borphan",
                           "deprecated": r"\bdeprecat"}
            if trig in event_words and not re.search(event_words[trig], low):
                missing.append("the event ({})".format(trig.replace("_", " ")))
            if trig == "owner_added":
                for who in ms.get("people") or []:
                    if who and who not in body:
                        missing.append("the new owner {}".format(who))
            if trig == "published" and ms.get("released") and not any(
                    x.get("url") in urls for x in ms.get("released") or []):
                missing.append("the published version's tag link")
            if row["kind"] == "module_event" and not (
                    (ms.get("source") or {}).get("url") in urls or ms.get("module_url") in urls):
                missing.append("a link to the event's source or the module")
            if trig == "published":
                for rel in ms.get("released") or []:
                    if rel.get("version") and version_key(rel["version"]) not in \
                            {version_key(x) for x in _VERSION_RE.findall(_URL_RE.sub(" ", body))}:
                        missing.append("published version {}".format(rel["version"]))
                if not re.search(r"\b(published|released)\b", low):
                    missing.append("the word published / released")
            elif trig == "merged_not_published":
                if "not yet published" not in low and "not published yet" not in low:
                    missing.append("'not yet published'")
                found_v = {version_key(x) for x in _VERSION_RE.findall(_URL_RE.sub(" ", body))}
                for d in ms.get("declared") or []:
                    if d.get("version") and version_key(d["version"]) not in found_v:
                        missing.append("declared version {}".format(d["version"]))
            elif trig == "merged_release_unknown":
                if not re.search(r"release status (is )?not known|not known whether", low):
                    missing.append("'release status not known'")
                if re.search(r"\b(not yet published|published|released)\b",
                             low.replace("release status", "")):
                    r["release_claimed_while_unknown"] = True
            elif trig == "merged_no_release":
                if not re.search(r"no (new )?version|no version change|no release", low):
                    missing.append("'no version change'")
            elif trig in ("new_module_proposed", "breaking_declared_open"):
                if not re.search(r"\b(open|proposed|not (yet )?merged)\b", low):
                    missing.append("that the pull request is open / proposed")
                if trig == "new_module_proposed" and not re.search(
                        r"not (yet )?published|not published yet", low):
                    missing.append("'not published'")
            elif trig == "deprecated":
                rpl = ms.get("replacement")
                if rpl:
                    if rpl.get("module") not in body:
                        missing.append("the replacement {}".format(rpl.get("module")))
                    if rpl.get("module_url") and rpl["module_url"] not in urls:
                        missing.append("the replacement's link {}".format(rpl["module_url"]))
                elif not re.search(r"no replacement", low):
                    missing.append("that no replacement is named")
            elif trig in ("closed_not_planned", "closed_after_post"):
                if "closed" not in low and "withdrawn" not in low:
                    missing.append("that it was closed")
                reason = ms.get("reason")
                if reason is None and not re.search(r"no reason (is |was )?stated", low):
                    missing.append("'no reason is stated'")
                elif reason == "not_planned" and "not planned" not in low:
                    missing.append("the stated reason 'not planned'")
                elif reason == "superseded" and not re.search(
                        r"#{}\b|/pull/{}\b".format(ms.get("superseded_by"), ms.get("superseded_by")),
                        body):
                    missing.append("what superseded it (#{})".format(ms.get("superseded_by")))
                elif reason == "duplicate" and "duplicate" not in low:
                    missing.append("the stated reason 'duplicate'")
                for x in ms.get("landed_in") or []:
                    if not re.search(r"#{}\b|/pull/{}\b".format(x["pr"], x["pr"]), body):
                        missing.append("where its content landed (#{})".format(x["pr"]))
                if reason != "superseded" and _SUPERSEDE_RE.search(body):
                    # the post is about this one item: "superseded" needs the
                    # record's own words (`superseded_by`)
                    r["superseded_not_in_view"] = [{"pr": (ms.get("item") or {}).get("number"),
                                                    "content_landed_in": [
                                                        x["pr"] for x in ms.get("landed_in") or []]}]
            if trig == "breaking_declared_open" or ms.get("breaking") == "declared":
                if "breaking" not in low:
                    missing.append("'breaking'")
            elif re.search(r"\bbreak(s|ing)?\b", low):
                r["breaking_not_declared"] = True
            if row["kind"] == "weekly":
                # the repository or one of the week's pull requests, in both forms
                wrepo = row.get("repo") or pid.split(":")[1]
                pr_urls = {(prs_url.get((wrepo, n))) for t in ms.get("themes") or []
                           for n in (t.get("merged") or []) + (t.get("opened") or [])}
                repo_url = "https://github.com/{}".format(wrepo)
                if not (urls & (pr_urls - {None}) or any(
                        u.rstrip("/") == repo_url for u in urls)):
                    missing.append("a link to the repository or one of the week's pull requests")
            if row["kind"] != "weekly" and row.get("url") and row["url"] not in urls and \
                    not any((p or {}).get("url") in urls for p in ms.get("prs") or []) and \
                    not (ms.get("source") or {}).get("url") in urls and not \
                    any(m.get("url") in urls for m in mods) and not \
                    any(x.get("url") in urls for x in ms.get("released") or []) and \
                    (ms.get("item") or {}).get("url") not in urls:
                missing.append("a link to the story, a pull request or the module")
        if missing:
            r["must_state_missing"] = missing
        # the record checks of the issue, on the post's text
        plain = _HANDLE_RE.sub("", body)
        # the candidate's repository in a heading, so every paragraph of the
        # post (linked or not) is scoped to it in a stream
        prepo = (row or {}).get("repo") or (pid.split(":")[1] if ":" in pid else None) \
            or _slug(views[0])
        issue = "# post\n\nIntro.\n\n## {}\n\n### {}\n\n{}\n".format(
            HEADINGS["plumbing"][0], prepo, plain)
        full = check_newsletter(views, issue, registry_names=registry_names,
                                automation=automation)
        if full.get("quotes_not_in_view"):
            # plain text has no code spans: a quote is verbatim when it matches
            # the record with the record's backticks dropped
            if code_free is None:
                u_, t_ = set(), []
                for v_ in views:
                    link._slice_strings(v_, u_, t_)
                code_free = [link._norm_ws(x.replace("`", "")) for x in t_]
            full["quotes_not_in_view"] = [q for q in full["quotes_not_in_view"]
                                          if not any(link._norm_ws(q) in t for t in code_free)]
        allowed = set()
        _social_numbers(row.get("must_state") if row else None, allowed)
        if full.get("numbers_not_in_view"):
            # a count the row itself gives (a weekly post's PRs) is a fact too
            full["numbers_not_in_view"] = [x for x in full["numbers_not_in_view"]
                                           if x not in allowed]
        if full.get("urls_not_in_view"):
            # a repository's own page is a link every post may give
            roots = {"https://github.com/{}".format(_slug(v_)).lower() for v_ in views}
            full["urls_not_in_view"] = [u for u in full["urls_not_in_view"]
                                        if u.rstrip("/").lower() not in roots]
        # every state, review and publication claim against the record as of
        # the post's day (one projection, not the window's end)
        own_story = ("{}:{}".format(pid.split(":")[1], pid.split(":")[2])
                     if pid.startswith("story:") and pid.count(":") >= 2 else None)
        full.update(state_claims(views, issue, as_of=day, subject_story=own_story))
        full.update(relation_claims(views, issue, as_of=day, subject_story=own_story))
        for snt in _sentences(_URL_RE.sub(" ", plain)):
            if not re.search(r"\bcommunity\b", snt, re.I):
                continue
            for nm, prow in people_rows.items():
                if re.search(r"(?<!\w)" + re.escape(nm) + r"(?!\w)", snt) and \
                        prow.get("affiliation") != "community":
                    full.setdefault("affiliation_mismatch", []).append(
                        {"name": nm, "affiliation": prow.get("affiliation"),
                         "sentence": " ".join(snt.split())[:160]})
        for k in POST_CHECK_KEYS:
            if full.get(k):
                r[k] = full[k]
        if against is not None:
            b = before.get((pid, ch))
            if b is None:
                r["facts_changed"] = "no earlier version of this post"
            else:
                now = post_facts(body, names)
                diff = {k: {"added": sorted(set(now[k]) - set(b[k])),
                            "removed": sorted(set(b[k]) - set(now[k]))}
                        for k in now if set(now[k]) != set(b[k])}
                if diff:
                    r["facts_changed"] = diff
        fails = [k for k in r if k not in ("post_id", "channel", "day", "length")]
        r["ok"] = not fails
        ok = ok and r["ok"]
        results.append(r)
    if against is not None:
        now_keys = {(m.group(1), m.group(2).lower()) for m in blocks}
        for (pid, ch) in sorted(before):
            if (pid, ch.lower()) not in now_keys:
                results.append({"post_id": pid, "channel": ch, "facts_changed":
                                "the post is missing from the new version", "ok": False})
                ok = False
    if file_errors:
        ok = False
    return {"ok": ok, "posts": results, "file_errors": file_errors,
            "voice_pass_may_change": VOICE_PASS_MAY_CHANGE}


VOICE_PASS_MAY_CHANGE = (
    "wording and sentence order",
    "person (first person plural, 'we')",
    "greeting and closing lines that state no fact",
    "hashtags, within the channel's limit",
    "emoji, within the length limit")


# --- the work package (many stories written in parallel) ---------------------

WORK_PACKAGE_RULES = """
## Newsletter rules on top of the story brief (reference/newsletter.md)

1. **First line states the state.** The bold headline starts with the story's state label:
   `**Landed — …**`, `**In flight — …**`, `**Stalled — …**` or `**Closed — …**` (the `state` in
   the story's row file). A plumbing story uses its own state the same way.
2. **Closing part.** "What landed" for a landed story; "Where it stands" for every other one,
   a closed one included (what was decided, by whom, and what is left).
3. **Markers.** Write the story between `<!-- story: <key> -->` and `<!-- /story -->` on their
   own lines, nothing else in the file. `<key>` is the `key` in the row file.
4. **Links.** Name each module the story affects as a link to its folder (the row file's
   `modules[].url`, or the folder of a module the record names). A published version stated
   with a module links its tag (`released[].url`). No other URL that is not in the slice.
5. **Release status.** Where the story says a PR landed / merged / shipped, say in the same or
   the next sentence what that means for a reader, from `released[]`: "released as [X](tag)",
   "declared in the changelog, not yet published", "no version change", or "release status
   not known".
6. **Who / why / state of play.** Answer, from the slice: which module or pattern (linked), why
   it came up (`prompted_by` in the row file), what was decided or changed, the state of play,
   where it is heading or what it waits on, how a reader can help (only actions the record
   points to). Fork contributions are said as such (`pr_details[].fork_status`: `fork`,
   `deleted_fork` — "from a fork since deleted" —, `not_fork`, `unknown`).
7. **Automation accounts** (`[bot]` logins, `automation` set in the row's people or the
   view's people) are named as automation, never as a person; one acting for a person
   (`on_behalf_of`) is "an automation account (a coding agent) working for Name (@login)".
8. **No tool, model or vendor names** that appear in the record (agent footers, assistant
   account names): say "an automation account (a coding agent)".
9. **One review state.** Say an open PR's review state only as `review_states` in the row
   file (and `pr_details[].review_state`) gives it: `approved` "approved"; `approved_stale`
   "approved before the author's latest changes" (never "ready to merge"); `none` / `requested`
   "waiting for a first review"; `automated_only` "reviewed only by automation, not yet by a
   person"; `commented` "reviewed with comments, no verdict"; `changes_requested` "changes
   requested". The issue's check fails a PR whose review state reads differently anywhere.
10. **What a module is.** On a module's first mention say what it is, from
   `modules[].info` / `module_info` (display name, resource type), in a few words.
11. **Why it is in its state.** `status_reason` in the row file says why the story is in its
   group; a closed story ends "Where it stands" with the close reason (`closing.state_reason`,
   who closed it), what superseded it, or "the record states no reason".
12. **Where closed work went.** A PR closed without merging whose commits a merged PR holds
   (`closed_unmerged.content_landed_in`) is said as that, with its basis ("its commit ... is
   part of #N, which merged"); "superseded" or "replaced by" only with `superseded_by` (the
   record's own words). The issue check fails "superseded" without it.
13. **Why a member is in a train** (`joined_by` in the row file: the line of the PR
   description that names the issue, or "a closing reference GitHub records") and **what a PR
   changed as a whole** (`net_files`: files, lines added / removed, own commits) may be said;
   an automation account works for the person `author_on_behalf_of` names on that PR.
14. **How the work connects.** `relations` in the row file are the story's links to other
   stories from the record (never a bare mention): say what it waits on (`blocked_by`,
   `depends_on`, `stacked_on` with `waiting`) in "Where it stands", and what it unblocked,
   followed up (`follow_up_of`), was caused by (`caused_by`) or where its content landed
   (`content_landed_in`) where the change is told — with the quote's substance and a link to
   the other item. A target with `waiting: false` is no longer waited on: say it was waited
   on, not that it still is. An external target (another repository) is named with its link
   and no state. A relation between two items of the same story is not in `relations`; it is in
   `inner_relations` in the row file (from the waiting item): state those as facts, with a link,
   no quote needed. Only when the row has no `inner_relations` key at all (a view without that
   layer) is an in-story dependency stated with the slice's own quote. The issue check fails a
   dependency the record does not hold (`relations_not_in_view`) and a denial ("no longer waits
   on") of one it holds in force (`relation_denials_contradicted`). A relation whose basis is a ticked task (`task_done`) or text edited after the window
   (`text_current: false`) is not waited on any more: say the task is ticked or the text changed,
   never that it still waits. When `connections.chains_capped` is set, say the chains were
   capped (the record's walk stopped before their end).
"""


def work_package(views, out_dir, skill_dir=None, cadence=None, automation=(), view_paths=None):
    """Write the work package of one issue into `out_dir`: every `full`
    story's slice (with `newsletter_links`) and outline row, one theme file
    per plumbing theme, `manifest.json` (every story in issue order with its
    plan, slice size and batch), and batches for parallel writers (stories in
    batches of at most WORK_PACKAGE_BATCH stories and WORK_PACKAGE_BYTES of
    slices, of roughly equal slice size; one batch per plumbing theme), each
    with `BATCH.md` and `STORY_BRIEF.md`. Returns the manifest."""
    views = join_members(views)
    multi = len(views) > 1
    skill_dir = skill_dir or os.path.dirname(os.path.abspath(__file__))
    out = outline(views, cadence=cadence, automation=automation)
    sec = {x["id"]: x for x in out["sections"]}
    st = sec["stories"]
    by_repo = {_slug(v): v for v in views}
    for sub in ("slices", "rows", "out", "tmp", "batches"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
    guide = open(os.path.join(skill_dir, "reference", "report-sections.md"),
                 encoding="utf-8").read()
    i = guide.find("### Stories (the story brief)")
    j = guide.find("## Next-release forecast", i)
    brief = guide[i:j].strip() if i >= 0 else ""

    def view_of(row):
        return by_repo.get(row.get("repo")) if multi else views[0]

    module_rows = {(m.get("repo"), m["area"]): m
                   for m in sec["module_changes"].get("modules") or []}

    def write_story(row):
        v = view_of(row)
        sl = link.slice_train(v, row["id"])
        sl["newsletter_links"] = story_links(row)
        path = os.path.join(out_dir, "slices", slice_filename(row["key"]))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sl, fh, indent=1)
        full = dict(row, prompted_by=prompted_by(v, row.get("prs") or []),
                    pr_details=[_pr_row(v, n, automation, full=True)
                                for n in row.get("prs") or []])
        # what each version of the story's modules holds: the release rows
        # and the child versions published with them (`child_releases`)
        mrel = {}
        for m in row.get("modules") or []:
            mr = module_rows.get((row.get("repo"), m.get("area")))
            if mr:
                mrel[m["area"]] = {k: mr.get(k) for k in ("release", "child_releases", "info")
                                   if mr.get(k)}
        if mrel:
            full["module_releases"] = mrel
        people = []
        for login, pp in (sl.get("participants") or {}).items():
            prow = {"login": login}
            prow.update(pp if isinstance(pp, dict) else {})
            if is_automation(v, login, automation):
                prow["automation"] = True
            people.append(prow)
        full["people"] = people
        rpath = os.path.join(out_dir, "rows", slice_filename(row["key"]).replace(
            ".slice.json", ".row.json"))
        with open(rpath, "w", encoding="utf-8") as fh:
            json.dump(full, fh, indent=1)
        return {"key": row["key"], "group": row["group"], "state": row["state"],
                "plan": "full", "plan_reason": row.get("plan_reason"),
                "title": row.get("title"), "theme": row.get("theme"),
                "slice": os.path.relpath(path, out_dir),
                "row": os.path.relpath(rpath, out_dir), "slice_bytes": os.path.getsize(path)}

    manifest = [write_story(r) for r in st.get("full") or [] if r["group"] != "plumbing"]
    total = sum(m["slice_bytes"] for m in manifest)
    n_batches = max(1, -(-len(manifest) // WORK_PACKAGE_BATCH),
                    -(-total // WORK_PACKAGE_BYTES)) if manifest else 0
    bins = [[] for _ in range(n_batches)]
    for m in sorted(manifest, key=lambda m: -m["slice_bytes"]):
        open_bins = [k for k in range(n_batches) if len(bins[k]) < WORK_PACKAGE_BATCH] \
            or list(range(n_batches))
        k = min(open_bins, key=lambda k: (sum(x["slice_bytes"] for x in bins[k]), len(bins[k])))
        bins[k].append(m)
    batches = []
    for k, items in enumerate(bins, 1):
        bid = "stories-{:02d}".format(k)
        items.sort(key=manifest.index)
        for m in items:
            m["batch"] = bid
        batches.append({"id": bid, "kind": "stories", "keys": [m["key"] for m in items],
                        "slice_bytes": sum(m["slice_bytes"] for m in items)})
    rows_all = st.get("all") or []
    for th in sec["plumbing"].get("themes") or []:
        tid = th.get("id")
        members = [r for r in rows_all if r.get("theme") == tid]
        discussed = [r for r in members if r["plan"] == "full"]
        named = [r for r in members if r["plan"] == "in_theme"]
        items = []
        for r in discussed:
            m = write_story(r)
            m["batch"] = "theme-" + tid
            manifest.append(m)
            items.append(m)
        for r in named:
            manifest.append({"key": r["key"], "group": r["group"], "state": r["state"],
                             "plan": "in_theme", "plan_reason": r.get("plan_reason"),
                             "title": r.get("title"), "theme": tid, "url": r.get("url"),
                             "prs": r.get("prs"), "batch": "theme-" + tid})
        v = by_repo.get(th.get("repo")) if multi else views[0]
        tfile = os.path.join(out_dir, "rows", "theme-{}.json".format(tid))
        # the theme's PRs with their full descriptions (the outline cuts them)
        th_full = dict(th, prs=[_pr_row(v, p["number"], automation, full=True)
                                for p in th.get("prs") or [] if p.get("number") is not None])
        with open(tfile, "w", encoding="utf-8") as fh:
            json.dump({"theme": th_full, "discussed": discussed,
                       "named": [dict(r, pr_details=[_pr_row(v, n, automation, full=True)
                                                     for n in r.get("prs") or []])
                                 for r in named]}, fh, indent=1)
        batches.append({"id": "theme-" + tid, "kind": "theme", "theme": tid,
                        "title_hint": th.get("title_hint"), "keys": [m["key"] for m in items],
                        "named": [r["key"] for r in named],
                        "slice_bytes": sum(m["slice_bytes"] for m in items)})
    for r in st.get("lines") or []:
        manifest.append({"key": r["key"], "group": r["group"], "state": r["state"],
                         "plan": "line", "plan_reason": r.get("plan_reason"),
                         "title": r.get("title"), "url": r.get("url"),
                         "idle_days": r.get("idle_days"), "waits_on": r.get("waits_on"),
                         "modules": r.get("modules")})
    doc = {"kind": "newsletter_work_package", "repos": out["repos"], "period": out["period"],
           "plan_counts": st.get("plan_counts"), "stories": manifest, "batches": batches}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    vp = " ".join(os.path.abspath(x) for x in view_paths) if view_paths else "VIEW.json"
    for b in batches:
        _write_batch(out_dir, skill_dir, b, manifest, brief, vp)
    return doc


WORK_PACKAGE_BATCH = 8           # stories per writer batch
WORK_PACKAGE_BYTES = 1_500_000   # slice bytes per writer batch


def _write_batch(out_dir, skill_dir, b, manifest, brief, view_paths="VIEW.json"):
    bdir = os.path.join(out_dir, "batches", b["id"])
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "STORY_BRIEF.md"), "w", encoding="utf-8") as fh:
        fh.write(brief + "\n" + WORK_PACKAGE_RULES)
    by_key = {m["key"]: m for m in manifest}

    def check(key, src):
        m = by_key[key]
        return ("`sed -n '/<!-- story: {k}/,/<!-- \\/story/p' {src} | sed '/^<!-- \\/\\?story/d'"
                " > {d}/tmp/{f}.md && python3 {s}/link.py {d}/{sl} --check-story {d}/tmp/{f}.md`"
                .format(k=key, src=src, d=out_dir, f=slice_filename(key)[:-11], s=skill_dir,
                        sl=m["slice"]))
    lines = ["# Batch {}".format(b["id"]), "",
             "Write from the files listed here ONLY: never outside knowledge, never an invented "
             "fact, name, number or URL. Read `STORY_BRIEF.md` in this directory first.", "",
             "Skill: `{}`. Output directory: `{}/out`.".format(skill_dir, out_dir), ""]
    if b["kind"] == "stories":
        for key in b["keys"]:
            m = by_key[key]
            dst = "{}/out/{}".format(out_dir, slice_filename(key).replace(".slice.json",
                                                                          ".story.md"))
            lines += ["## `{}` — {}: {}".format(key, m["state"], m.get("title")), "",
                      "- slice: `{}/{}` ({} bytes); row: `{}/{}`".format(
                          out_dir, m["slice"], m["slice_bytes"], out_dir, m["row"]),
                      "- write `{}`: `<!-- story: {} -->`, the story (headline starting "
                      "`**{} — `), `<!-- /story -->`".format(
                          dst, key, STATE_LABELS.get(m["state"], m["state"])),
                      "- check: " + check(key, dst), ""]
    else:
        dst = "{}/out/theme-{}.md".format(out_dir, b["theme"])
        lines += ["## Theme `{}`".format(b["title_hint"]), "",
                  "Read `{}/rows/theme-{}.json` (the theme, its PRs with descriptions as last "
                  "gathered, people, the discussed trains and the trains to name).".format(
                      out_dir, b["theme"]),
                  "Write `{}`: first line `### {}`; an intro, then one paragraph per strand of "
                  "work — what problem, what was built or changed, what it means for "
                  "contributors, who did it; name EVERY train under `named` by a link to one of "
                  "its PRs; then each discussed train's story in full between its markers."
                  .format(dst, b["title_hint"]), ""]
        for key in b["keys"]:
            lines += ["- story `{}`: check: {}".format(key, check(key, dst))]
        lines += ["", "Trains to name: " + ", ".join("`{}`".format(k) for k in b["named"]), "",
                  "Check the narrative (heading, every train named, quotes, PR and review "
                  "states, release claims, numbers): `python3 {}/newsletter.py {} --check-theme "
                  "{} --theme {}`".format(skill_dir, view_paths, dst, b["theme"]), ""]
    with open(os.path.join(bdir, "BATCH.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

# --- CLI ----------------------------------------------------------------------

_USAGE = ("usage: newsletter.py VIEW.json [VIEW.json ...] --outline [--weekly|--monthly] "
          "[--automation LOGIN ...]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --check-newsletter ISSUE.md "
          "[--slices DIR] [--people PEOPLE.json] [--weekly|--monthly]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --outline --daily --day YYYY-MM-DD "
          "[--posted LEDGER.json] [--hold-days N]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --check-post POSTS.md "
          "[--people PEOPLE.json] [--against EARLIER.md]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --check-theme THEME.md "
          "[--theme ID] [--people PEOPLE.json]\n"
          "       newsletter.py VIEW.json [VIEW.json ...] --work-package DIR [--weekly|--monthly] "
          "[--automation LOGIN ...]\n")


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
    people = reg.get("people") if isinstance(reg, dict) else None
    if not isinstance(people, dict):
        sys.stderr.write("newsletter.py: error: {} is not a people registry\n".format(path))
        raise SystemExit(2)
    out = {}
    for login, e in people.items():
        if isinstance(e, dict) and e.get("do_not_name"):
            out[login] = e.get("name")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(_USAGE)
        raise SystemExit(0)
    automation = []
    while "--automation" in argv:
        i = argv.index("--automation")
        if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        automation.append(argv[i + 1])
        del argv[i:i + 2]
    cadence = None          # from the window: weekly when it is short
    if "--weekly" in argv and "--monthly" in argv:
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    if "--weekly" in argv:
        argv.remove("--weekly")
        cadence = "week"
    if "--monthly" in argv:
        argv.remove("--monthly")
        cadence = "month"
    opts = {}
    for flag in ("--check-newsletter", "--slices", "--people", "--work-package",
                 "--check-theme", "--theme", "--day", "--posted", "--hold-days",
                 "--check-post", "--against"):
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
    daily = "--daily" in argv
    if daily:
        argv.remove("--daily")
    paths = argv
    try:
        hold = int(opts.get("--hold-days", SOCIAL_HOLD_DAYS))
    except ValueError:
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    if daily:
        if not do_outline or "--day" not in opts or not paths or \
                any(x.startswith("-") for x in paths) or \
                not re.match(r"^\d{4}-\d\d-\d\d$", opts["--day"]):
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        views = [_load(x) for x in paths]
        try:
            posted = read_ledger(opts["--posted"]) if "--posted" in opts else set()
        except (OSError, ValueError) as exc:
            sys.stderr.write("newsletter.py: error: cannot read the ledger ({})\n".format(exc))
            raise SystemExit(2)
        json.dump(daily_outline(views, opts["--day"], posted=posted, hold_days=hold,
                                automation=automation), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if "--check-post" in opts:
        if not paths or any(x.startswith("-") for x in paths) or do_outline:
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        views = [_load(x) for x in paths]
        try:
            with open(opts["--check-post"], encoding="utf-8") as fh:
                ptext = fh.read()
            against = None
            if "--against" in opts:
                with open(opts["--against"], encoding="utf-8") as fh:
                    against = fh.read()
        except OSError as exc:
            sys.stderr.write("newsletter.py: error: {}\n".format(exc))
            raise SystemExit(2)
        registry = _load(opts["--people"]) if "--people" in opts else None
        res = check_posts(views, ptext, registry=registry, against=against, hold_days=hold,
                          automation=automation)
        json.dump(res, sys.stdout, indent=2)
        sys.stdout.write("\n")
        raise SystemExit(0 if res["ok"] else 1)
    if "--check-theme" in opts:
        if not paths or any(x.startswith("-") for x in paths) or do_outline or \
                "--check-newsletter" in opts or "--work-package" in opts:
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        views = [_load(x) for x in paths]
        try:
            with open(opts["--check-theme"], encoding="utf-8") as fh:
                ttext = fh.read()
        except OSError as exc:
            sys.stderr.write("newsletter.py: error: cannot read {} ({})\n".format(
                opts["--check-theme"], exc))
            raise SystemExit(2)
        names = _registry_hidden(opts["--people"]) if "--people" in opts else None
        res = check_theme(views, ttext, theme_id=opts.get("--theme"), registry_names=names,
                          automation=automation)
        json.dump(res, sys.stdout, indent=2)
        sys.stdout.write("\n")
        raise SystemExit(0 if res["ok"] else 1)
    if "--work-package" in opts:
        if not paths or any(x.startswith("-") for x in paths) or do_outline or \
                "--check-newsletter" in opts:
            sys.stderr.write(_USAGE)
            raise SystemExit(2)
        views = [_load(x) for x in paths]
        doc = work_package(views, opts["--work-package"], cadence=cadence,
                           automation=automation, view_paths=paths)
        json.dump({"stories": len(doc["stories"]), "batches": [
            {"id": b["id"], "stories": len(b["keys"]), "named": len(b.get("named") or []),
             "slice_bytes": b["slice_bytes"]} for b in doc["batches"]]}, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    if not paths or any(p.startswith("-") for p in paths) or \
            do_outline == ("--check-newsletter" in opts) or \
            (do_outline and ("--slices" in opts or "--people" in opts)):
        sys.stderr.write(_USAGE)
        raise SystemExit(2)
    views = [_load(p) for p in paths]
    for p, v in zip(paths, views):
        if not isinstance(v, dict) or not isinstance(v.get("meta"), dict):
            sys.stderr.write("newsletter.py: error: {} is not a view (no meta)\n".format(p))
            raise SystemExit(2)
    if do_outline:
        json.dump(outline(views, cadence=cadence, automation=automation), sys.stdout,
                  indent=2)
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
                           cadence=cadence, automation=automation)
    json.dump(res, sys.stdout, indent=2)
    sys.stdout.write("\n")
    raise SystemExit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
