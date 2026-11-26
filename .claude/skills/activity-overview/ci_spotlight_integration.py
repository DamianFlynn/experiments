#!/usr/bin/env python3
"""CI integration gate: run the four train/lifecycle `spotlight` queries
(person / subsystem / symbol / grep) against a REAL gathered journey-graph store
and assert each returns a correct, cited, project-scoped answer.

This is the "build a db and query it, exactly as real execution does" check — not
a mock and not a crafted fixture. The CI workflow gathers a fixed historical AVM
window (early March 2026 — historical, so the data is stable) into the store; this
script then exercises the queries over it. Offline, the same checks run against
stores folded from the fixture bundles (test_ci_spotlight_integration.py).

Query arguments are *discovered* from the store, scoped to the project (the
most-active person, the most-touched area, the artifact with the richest
lifecycle, a search term with hits), so the gate verifies real behaviour without
hard-coding brittle ids. Each answer is then checked against what the store
itself holds, never against spotlight's own answer (issue #113): a person's trains
are exactly the trains of their contribution edges, each touchpoint carrying the
role its edge implies; every train's outcome equals the one the gate computes
from the stored member records (_store_outcome, LLD §9's rule written out here,
not imported); every timeline row and touchpoint cites what its stored node
holds; every symbol lifecycle row equals its ledger row and the outcome follows
the last ledger row; every grep match is delivered, nothing outside the project
is, and each mention's title and excerpt are the stored node's own text.

A check whose input the store does not hold (no contributions, no area, no code
events, no indexed text) is reported as SKIPPED with the reason. Without
--require-all skips do not fail the run (a fixture store holds only some
inputs); with --require-all — how the live job runs — any skip fails it.

Exit codes: 0 every check verified or skipped (no skips with --require-all);
1 a check failed, or a skip under --require-all; 2 usage error (missing store,
unknown project, several projects and no --project).

Usage: python3 ci_spotlight_integration.py --store PATH [--project NAME]
                                           [--require-all]
"""

import argparse
import json
import os
import re
import sqlite3
import sys

import graphstore
import spotlight

_CONTRIB = ("authored", "reviewed", "merged", "reported", "commented")
# search terms common to AVM discussion/code; the first with hits IN THE PROJECT
# is used. A store without any (a small fixture) falls back to a word taken from
# the project's own indexed text (_grep_term).
_GREP_CANDIDATES = ("module", "avm", "bicep", "resource", "version", "fix")
# how many of the project's indexed nodes the fallback term search reads
_GREP_FALLBACK_NODES = 50
_WORD_RE = re.compile(r"[A-Za-z]{4,}")

# spotlight's outcome vocabularies: a decision train (link.build_trains plus
# direct_push) and an artifact lifecycle.
_TRAIN_OUTCOMES = ("shipped", "in_flight", "rejected", "direct_push", "abandoned")
_LIFECYCLE_OUTCOMES = ("alive", "removed")
# ledger events that end an artifact: a symbol's `remove`, a file's `delete`
_REMOVAL_EVENTS = ("remove", "delete")
# A contribution edge's type -> the role its touchpoint carries (LLD §9).
_ROLE_OF = {"authored": "author", "reviewed": "reviewer", "merged": "merger",
            "reported": "reporter", "commented": "commenter"}
# Spotlight groups trains over the CAUSAL spine: every spine edge type but
# `cross_ref` (a mention is not causality).
_CAUSAL = tuple(t for t in graphstore.SPINE_EDGE_TYPES if t != "cross_ref")
# Issue close reasons that mean "not done" (GitHub `state_reason`).
_DROPPED = ("not_planned", "duplicate")
# Citation fields a timeline row / touchpoint copies from its stored node.
_CITE_FIELDS = ("number", "sha", "url", "title")


class CheckError(AssertionError):
    """A failed integration expectation (printed, then exit 1)."""


class CheckSkipped(Exception):
    """The store holds no input for this check (printed; fails only under
    --require-all)."""


def _require(cond, msg):
    if not cond:
        raise CheckError(msg)


def _is_cited(row):
    """A row/touchpoint carries a source citation."""
    return any(row.get(k) is not None
               for k in ("url", "number", "sha", "ref_sha"))


def _train_has_cited_timeline(train):
    return any(_is_cited(r) for r in train.get("timeline", []))


def _in_project(node_id, project):
    """True for a repo-scoped (`{project}/…`) or project-scoped (`{project}#…`)
    id of `project` — case-sensitive, so `abc` or `A_C` never pass for `a_c`."""
    return node_id.startswith((project + "/", project + "#"))


def _range(prefix):
    """(lo, hi) selecting exactly the ids that start with `prefix`,
    case-sensitively and with no LIKE wildcards (`_`/`%` in a project name are
    literal)."""
    return graphstore._prefix_range(prefix)


def _norm(text):
    """Whitespace-collapsed text (how spotlight bounds an excerpt)."""
    return " ".join(str(text or "").split())


def _member_kind(node):
    """A stored train member's kind for the outcome rule: "release", "pr",
    "issue", "commit" or None (review / lifecycle leaves, artifacts, areas).
    A `pr-<n>` record is a PR; so is an `issue-<n>` record carrying a /pull/
    url (a `Closes #N` whose #N is a PR, backfilled under the issue id)."""
    local = graphstore.local_id(node["id"])
    data = node.get("data") or {}
    if local.startswith("release-"):
        return "release"
    if node.get("node_class") == "social":
        if local.startswith("pr-"):
            return "pr"
        if local.startswith("issue-"):
            return "pr" if "/pull/" in (data.get("url") or "") else "issue"
        return None
    if node.get("node_class") == "code" and "sha" in data \
            and not graphstore.is_artifact_local(local):
        return "commit"
    return None


def _store_outcome(nodes):
    """A decision train's outcome from its stored member records — the gate's
    own statement of LLD §9's rule, so a wrong spotlight label cannot pass:
      1. any release -> shipped;
      2. with PRs: any merged -> shipped; else any not closed -> in_flight;
         else (all closed unmerged) -> rejected;
      3. no PR: any issue not closed -> in_flight; else any closed issue
         whose state_reason is not not_planned / duplicate (none counts as
         completed) -> shipped;
      4. else commits -> direct_push; else issues (all dropped) -> abandoned;
         else in_flight."""
    prs, issues, commits = [], [], False
    for node in nodes:
        kind = _member_kind(node)
        data = node.get("data") or {}
        if kind == "release":
            return "shipped"
        if kind == "pr":
            prs.append(data)
        elif kind == "issue":
            issues.append(data)
        elif kind == "commit":
            commits = True
    if prs:
        if any(p.get("merged") for p in prs):
            return "shipped"
        if any(p.get("state") != "closed" for p in prs):
            return "in_flight"
        return "rejected"
    if any(i.get("state") != "closed" for i in issues):
        return "in_flight"
    if any(i.get("state_reason") not in _DROPPED for i in issues):
        return "shipped"
    if commits:
        return "direct_push"
    return "abandoned" if issues else "in_flight"


def _fts_text(conn, node_id):
    """The indexed text of `node_id` ('' when none / no index)."""
    try:
        row = conn.execute(
            "SELECT t.text FROM fts_map m JOIN fts_text t ON t.rowid = m.fts_rowid "
            "WHERE m.node_id = ?", (node_id,)).fetchone()
    except sqlite3.DatabaseError:
        return ""
    return (row[0] if row else "") or ""


# --------------------------------------------------------------------------
# argument discovery (deterministic, project-scoped) over the real store
# --------------------------------------------------------------------------

def _most_active_person(conn, project):
    """The login with the most contribution edges in `project`, ties by id."""
    prefix = graphstore.qualify_person(project, "")
    lo, hi = _range(prefix)
    row = conn.execute(
        "SELECT src_id, COUNT(*) c FROM edges WHERE edge_type IN ({}) "
        "AND src_id >= ? AND src_id < ? GROUP BY src_id "
        "ORDER BY c DESC, src_id LIMIT 1".format(",".join("?" * len(_CONTRIB))),
        _CONTRIB + (lo, hi)).fetchone()
    return row[0][len(prefix):] if row else None


def _most_touched_area(conn, project):
    """(repo, area, touches) for the project's area with the most `touches`
    in-edges (ties by node id), else its first area node with touches=0, else
    None. Areas are repo-scoped, so the repo is part of the answer."""
    best = None  # (-touches, area node id, repo)
    for repo in graphstore.project_repos(conn, project):
        prefix = graphstore.qualify_id(project, repo, "area-")
        lo, hi = _range(prefix)
        row = conn.execute(
            "SELECT dst_id, COUNT(*) c FROM edges WHERE edge_type='touches' "
            "AND dst_id >= ? AND dst_id < ? GROUP BY dst_id "
            "ORDER BY c DESC, dst_id LIMIT 1", (lo, hi)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, 0 FROM nodes WHERE id >= ? AND id < ? "
                "ORDER BY id LIMIT 1", (lo, hi)).fetchone()
        if row is not None:
            cand = (-row[1], row[0], repo)
            if best is None or cand < best:
                best = cand
    if best is None:
        return None
    touches, area_id, repo = -best[0], best[1], best[2]
    prefix = graphstore.qualify_id(project, repo, "area-")
    return repo, area_id[len(prefix):], touches


def _richest_artifact(conn, project):
    """The project's `code_events` key with the most ledger rows (ties by id)."""
    best = None  # (-count, artifact_id)
    for repo in graphstore.project_repos(conn, project):
        lo, hi = _range(graphstore.qualify_id(project, repo, ""))
        row = conn.execute(
            "SELECT artifact_id, COUNT(*) c FROM code_events "
            "WHERE artifact_id >= ? AND artifact_id < ? GROUP BY artifact_id "
            "ORDER BY c DESC, artifact_id LIMIT 1", (lo, hi)).fetchone()
        if row is not None and (best is None or (-row[1], row[0]) < best):
            best = (-row[1], row[0])
    return best[1] if best else None


def _project_hits(conn, project, term):
    """Sorted ids of `project`'s nodes whose indexed text matches `term` as a
    literal phrase — the set spotlight grep must deliver (fts_text is
    store-wide, so other projects' hits are dropped, as spotlight does)."""
    query = '"{}"'.format(term.replace('"', '""'))
    return sorted({m for m in graphstore.fts_search(conn, query)
                   if _in_project(m, project)})


def _grep_term(conn, project):
    """A term with at least one hit in `project`: the first candidate that has
    one, else the first 4+-letter word of the project's indexed text (nodes in
    id order) that has one. None without FTS5 or indexed project text."""
    if not graphstore.fts5_available(conn):
        return None
    for term in _GREP_CANDIDATES:
        if _project_hits(conn, project, term):
            return term
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='fts_map'").fetchone():
        return None
    rows = []
    for prefix in (project + "#", project + "/"):
        lo, hi = _range(prefix)
        rows += [tuple(r) for r in conn.execute(
            "SELECT m.node_id, t.text FROM fts_map m "
            "JOIN fts_text t ON t.rowid = m.fts_rowid "
            "WHERE m.node_id >= ? AND m.node_id < ? ORDER BY m.node_id LIMIT ?",
            (lo, hi, _GREP_FALLBACK_NODES))]
    for _, text in sorted(rows)[:_GREP_FALLBACK_NODES]:
        for word in _WORD_RE.findall(text or ""):
            if _project_hits(conn, project, word):
                return word
    return None


# --------------------------------------------------------------------------
# shared train assertions
# --------------------------------------------------------------------------

def _check_trains(conn, project, label, delivered):
    """Every delivered train: an outcome from spotlight's vocabulary; `repos`
    non-empty and all project members; a (ts, id)-ordered timeline whose ids
    are all in the project and stored; touchpoints all in the project; every
    row and touchpoint citing what its stored node holds (_check_citation);
    and the outcome the stored member records give (_store_outcome)."""
    members = set(graphstore.project_repos(conn, project))
    for t in delivered:
        a = t.get("anchor")
        _require(t.get("outcome") in _TRAIN_OUTCOMES,
                 "{}: train {} has outcome {!r}, not one of {}".format(
                     label, a, t.get("outcome"), "/".join(_TRAIN_OUTCOMES)))
        repos = t.get("repos")
        _require(isinstance(repos, list) and repos and set(repos) <= members,
                 "{}: train {} names repos {!r}, not members of {}".format(
                     label, a, repos, sorted(members)))
        tl = t.get("timeline")
        _require(isinstance(tl, list), "{}: train {} has no timeline".format(
            label, a))
        _require([(r.get("ts") or "", r["id"]) for r in tl]
                 == sorted((r.get("ts") or "", r["id"]) for r in tl),
                 "{}: train {} timeline is not chronological".format(label, a))
        stray = sorted(r["id"] for r in tl + t.get("touchpoints", [])
                       if not _in_project(r["id"], project))
        _require(not stray, "{}: train {} carries ids outside project {}: "
                 "{}".format(label, a, project, stray[:3]))
        nodes = []
        for r in tl:
            node = graphstore.get_node(conn, r["id"])
            _require(node is not None, "{}: train {} timeline cites {}, which "
                     "the store does not hold".format(label, a, r["id"]))
            nodes.append(node)
            _check_citation(label, a, r, node)
        for tp in t.get("touchpoints", []):
            _check_citation(label, a, tp, graphstore.get_node(conn, tp["id"]))
        want = _store_outcome(nodes)
        _require(t.get("outcome") == want,
                 "{}: train {} has outcome {!r} but its stored records give "
                 "{!r}".format(label, a, t.get("outcome"), want))


def _check_citation(label, anchor, row, node):
    """A timeline row / touchpoint cites what its stored node holds: each of
    number / sha / url / title present exactly when the node's data has it,
    with the same value — except that a commit whose record stores no url may
    be cited by its GitHub `/commit/<sha>` link; a comment row's excerpt is
    the node's body."""
    data = (node or {}).get("data") or {}
    for key in _CITE_FIELDS:
        if (key == "url" and data.get("url") is None and data.get("sha")
                and (row.get("url") or "").endswith("/commit/" + data["sha"])):
            continue
        _require(row.get(key) == data.get(key),
                 "{}: train {} row {} cites {}={!r} but the stored node holds "
                 "{!r}".format(label, anchor, row["id"], key, row.get(key),
                               data.get(key)))
    if row.get("kind") == "comment" and "excerpt" in row:
        _require(_norm(row["excerpt"]) in _norm(data.get("body")),
                 "{}: train {} comment {} excerpt is not its stored body".format(
                     label, anchor, row["id"]))


def _check_shipped_count(label, res, total_key):
    shipped = sum(1 for t in res["delivered"] if t["outcome"] == "shipped")
    _require(res["summary"].get(total_key) == len(res["delivered"]),
             "{}: summary.{}={} but {} trains delivered".format(
                 label, total_key, res["summary"].get(total_key),
                 len(res["delivered"])))
    _require(res["summary"].get("shipped") == shipped,
             "{}: summary.shipped={} but {} shipped trains".format(
                 label, res["summary"].get("shipped"), shipped))


# --------------------------------------------------------------------------
# per-query checks
# --------------------------------------------------------------------------

def _shallow(conn, nid):
    """True when the causal-spine train of `nid` is one piece from wherever it
    is entered: each of its members reaches the whole train within
    graphstore.traverse_spine's default depth. A deeper train can be entered
    at two ends whose depth-capped reaches differ, so a reader may then list
    one record in two trains."""
    def reach(x, **kw):
        return set(graphstore.traverse_spine(conn, [x], edge_types=_CAUSAL,
                                             skip_dead=True, **kw)["reached"])
    whole = reach(nid, max_depth=64)
    return all(reach(m) == whole for m in sorted(whole))


def _check_person_touchpoints(conn, project, login, label, res):
    """The person's trains are exactly the trains of their contribution edges
    in the store: every (target, role) pair a contribution edge implies is a
    touchpoint of a delivered train and every touchpoint is such a pair; a
    target sits in exactly one train (in at least one when its train is
    deeper than the traversal cap, see _shallow); every delivered train holds
    a touchpoint; each summary role count equals the touchpoints of that role
    (an all-history query delivers every train)."""
    person = graphstore.qualify_person(project, login)
    edges = graphstore.get_edges(conn, person, direction="out",
                                 edge_types=list(_CONTRIB))
    want = {(e["dst_id"], _ROLE_OF[e["edge_type"]]) for e in edges}
    got, trains_of = set(), {}
    for t in res["delivered"]:
        tps = t.get("touchpoints") or []
        _require(tps, "{}: train {} holds no touchpoint of {} — no contribution "
                 "edge leads to it".format(label, t.get("anchor"), login))
        for tp in tps:
            got.add((tp["id"], tp.get("role")))
            trains_of.setdefault(tp["id"], set()).add(t.get("anchor"))
            if tp.get("role") == "commenter" and tp.get("excerpt"):
                data = (graphstore.get_node(conn, tp["id"]) or {}).get("data") or {}
                bodies = [_norm(c.get("body")) for c in
                          (data.get("comments_list") or [])
                          + (data.get("review_comments") or [])
                          if c.get("author") == login]
                _require(any(_norm(tp["excerpt"]) in b for b in bodies),
                         "{}: comment excerpt on {} is not one of {}'s stored "
                         "comments".format(label, tp["id"], login))
    _require(got == want,
             "{}: touchpoints differ from the contribution edges in the store: "
             "missing {} extra {}".format(label, sorted(want - got)[:3],
                                          sorted(got - want)[:3]))
    for dst in sorted({d for d, _ in want}):
        n = len(trains_of.get(dst, ()))
        if n > 1 and not _shallow(conn, dst):
            continue
        _require(n == 1, "{}: contribution target {} is in {} delivered trains, "
                 "not exactly one".format(label, dst, n))
    # each role count is the touchpoints of that role over the delivered
    # trains: the store's edges of that type once each, as the target of an
    # edge is in exactly one train (bar the deep-train case above)
    for etype in _CONTRIB:
        count = sum(1 for t in res["delivered"] for tp in t["touchpoints"]
                    if tp.get("role") == _ROLE_OF[etype])
        _require(res["summary"].get(etype, 0) == count,
                 "{}: summary.{}={} but the delivered trains hold {} {} "
                 "touchpoints".format(label, etype, res["summary"].get(etype, 0),
                                      count, _ROLE_OF[etype]))


def check_person(conn, project):
    login = _most_active_person(conn, project)
    if login is None:
        raise CheckSkipped("no person in project {} has a contribution "
                           "edge".format(project))
    label = "person({})".format(login)
    res = spotlight.person_impact(conn, project, login)
    _require(res["status"] == "ok", "{}: status={}".format(label, res["status"]))
    _require(len(res["delivered"]) > 0, "{}: no delivered trains".format(label))
    _check_trains(conn, project, label, res["delivered"])
    _check_shipped_count(label, res, "trains_touched")
    _check_person_touchpoints(conn, project, login, label, res)
    _require(any(_train_has_cited_timeline(t) for t in res["delivered"]),
             "{}: no train carries a cited timeline".format(label))
    _require(any(_is_cited(tp) for t in res["delivered"] for tp in t["touchpoints"]),
             "{}: no touchpoint carries a citation".format(label))
    # determinism: identical JSON across two runs
    a = json.dumps(spotlight.person_impact(conn, project, login), sort_keys=True)
    b = json.dumps(spotlight.person_impact(conn, project, login), sort_keys=True)
    _require(a == b, "{}: non-deterministic output".format(label))
    return "person `{}`: {} trains, cited ✓ (deterministic)".format(
        login, len(res["delivered"]))


def check_subsystem(conn, project):
    found = _most_touched_area(conn, project)
    if found is None:
        raise CheckSkipped("no area nodes in project {}".format(project))
    repo, area, touches = found
    label = "subsystem({} in {})".format(area, repo)
    res = spotlight.subsystem_split(conn, project, area, repo=repo)
    _require(res["status"] == "ok", "{}: status={}".format(label, res["status"]))
    # narrowed to one member, every repo-qualified row names that member
    _require(res.get("repos") == [repo],
             "{}: repos={!r}, expected [{!r}]".format(label, res.get("repos"), repo))
    for c in res["contributors"]:
        _require(c.get("repos") == [repo],
                 "{}: contributor {} names repos {!r}".format(
                     label, c.get("login"), c.get("repos")))
    deps = res.get("depends_on") or {}
    for d in deps.get("out", []) + deps.get("in", []):
        _require(d.get("repo") == repo,
                 "{}: depends_on row {} names repo {!r}".format(
                     label, d.get("id"), d.get("repo")))
    _check_trains(conn, project, label, res["delivered"])
    _check_shipped_count(label, res, "trains")
    if touches == 0:
        _require(res["delivered"] == [],
                 "{}: trains delivered for an area nothing touches".format(label))
        raise CheckSkipped("no area in project {} is touched by a commit; area "
                           "{} in {}: envelope verified, trains not "
                           "checked".format(project, area, repo))
    _require(len(res["delivered"]) > 0,
             "{}: no delivered trains for an area with {} touches".format(
                 label, touches))
    # every commit/PR that touches the area is a touchpoint of a delivered train
    area_id = graphstore.qualify_id(project, repo, "area-" + area)
    touchers = {e["src_id"] for e in graphstore.get_edges(
        conn, area_id, direction="in", edge_types=["touches"])}
    delivered_touch = {tp["id"] for t in res["delivered"] for tp in t["touchpoints"]}
    _require(touchers == delivered_touch,
             "{}: touching nodes {} but touchpoints {}".format(
                 label, sorted(touchers)[:3], sorted(delivered_touch)[:3]))
    _require(any(_train_has_cited_timeline(t) for t in res["delivered"]),
             "{}: no train carries a cited timeline".format(label))
    return "subsystem `{}` in {}: {} trains, {} contributors, cited ✓".format(
        area, repo, len(res["delivered"]), len(res["contributors"]))


def check_symbol(conn, project):
    aid = _richest_artifact(conn, project)
    if aid is None:
        raise CheckSkipped("no code_events in project {}".format(project))
    label = "symbol({})".format(aid)
    ledger = graphstore.get_code_events(conn, aid)
    res = spotlight.pattern_evolution(conn, project, aid)
    _require(res["status"] == "ok", "{}: status={}".format(label, res["status"]))
    _require(res.get("focus") == aid, "{}: focus is {!r}".format(
        label, res.get("focus")))
    _require(len(res["delivered"]) == 1,
             "{}: expected exactly one lifecycle train".format(label))
    train = res["delivered"][0]
    tl = train["timeline"]
    # all-history query: the whole ledger, in (date, commit) order
    _require(len(tl) == len(ledger),
             "{}: {} lifecycle rows for {} ledger events".format(
                 label, len(tl), len(ledger)))
    _require([(r["ts"], r["sha"]) for r in tl]
             == sorted((r["ts"], r["sha"]) for r in tl),
             "{}: lifecycle is not in (date, commit) order".format(label))
    # row for row, the ledger's own event / date / commit / author (the
    # ledger in (date, commit) order, stable over graphstore's read order)
    rows = sorted(ledger, key=lambda e: (e["date"] or "", e["commit_sha"]))
    for i, (r, e) in enumerate(zip(tl, rows)):
        got = (r.get("event"), r.get("ts"), r.get("sha"), r.get("author"))
        exp = (e["event"], e["date"] or "", e["commit_sha"], e["author"])
        _require(got == exp, "{}: lifecycle row {} is {} but the ledger row is "
                 "{}".format(label, i, got, exp))
    _require(all(r.get("id") == aid for r in tl),
             "{}: a lifecycle row names another artifact".format(label))
    _require(all("kind" in r for r in tl), "{}: a lifecycle row lacks 'kind'".format(
        label))
    _require(all(_is_cited(r) for r in tl),
             "{}: a lifecycle row lacks a commit citation".format(label))
    last = rows[-1]["event"] if rows else None
    want = "removed" if last in _REMOVAL_EVENTS else "alive"
    _require(train["outcome"] in _LIFECYCLE_OUTCOMES and train["outcome"] == want,
             "{}: outcome {!r} but the last ledger event is {!r} (expected "
             "{!r})".format(label, train["outcome"], last, want))
    # the artifact sits somewhere in its identity chain — at the head only when
    # it is not the destination of a move
    chain = [c["id"] for c in res.get("identity_chain") or []]
    _require(aid in chain, "{}: artifact not in its identity_chain {}".format(
        label, chain))
    _require(len(chain) == len(set(chain)),
             "{}: identity_chain repeats an id".format(label))
    _require(all(_in_project(c, project) for c in chain),
             "{}: identity_chain leaves project {}".format(label, project))
    return "symbol `{}`: {} lifecycle events, {}, identity chain of {}, " \
        "cited ✓".format(aid, len(tl), train["outcome"], len(chain))


def check_grep(conn, project):
    if not graphstore.fts5_available(conn):
        res = spotlight.text_mining(conn, project, "module")
        _require(res["status"] == "fts_unavailable",
                 "grep without FTS5: status={}".format(res["status"]))
        raise CheckSkipped("FTS5 unavailable in this SQLite build")
    term = _grep_term(conn, project)
    if term is None:
        raise CheckSkipped("no indexed text in project {}".format(project))
    label = "grep({})".format(term)
    hits = _project_hits(conn, project, term)
    res = spotlight.text_mining(conn, project, term)
    _require(res["status"] == "ok", "{}: status={}".format(label, res["status"]))
    _require(len(res["delivered"]) > 0,
             "{}: {} matches but no delivered trains".format(label, len(hits)))
    _check_trains(conn, project, label, res["delivered"])
    # every match in the project is delivered as a mention, and nothing else
    mentions = {tp["id"] for t in res["delivered"] for tp in t["touchpoints"]
                if tp.get("role") == "mention"}
    _require(mentions == set(hits),
             "{}: delivered mentions {} but the project's matches are {}".format(
                 label, sorted(mentions)[:3], hits[:3]))
    for t in res["delivered"]:
        for tp in t["touchpoints"]:
            if tp.get("role") != "mention":
                continue
            node = graphstore.get_node(conn, tp["id"]) or {}
            data = node.get("data") or {}
            _require(tp.get("title") == data.get("title"),
                     "{}: mention {} has title {!r} but the stored node's is "
                     "{!r}".format(label, tp["id"], tp.get("title"),
                                   data.get("title")))
            excerpt = _norm(tp.get("excerpt"))
            sources = [data.get(k) for k in ("body", "message", "title")]
            sources.append(_fts_text(conn, tp["id"]))
            _require(any(excerpt in _norm(src) for src in sources if src)
                     or not (excerpt or any(sources)),
                     "{}: mention {} excerpt {!r} is not in the stored "
                     "text".format(label, tp["id"], excerpt[:60]))
    _require(res["summary"]["matches"] == len(hits),
             "{}: summary.matches={} for {} matches".format(
                 label, res["summary"]["matches"], len(hits)))
    _require(all(_train_has_cited_timeline(t) for t in res["delivered"]),
             "{}: a delivered train has no cited timeline".format(label))
    # input hardening on real data: an operator/quote-bearing phrase must not raise
    hardened = spotlight.text_mining(conn, project, 'a AND "b" :c -d')
    _require(hardened["status"] == "ok",
             "grep input-hardening: operator phrase did not return ok")
    return "grep `{}`: {} matches across {} trains, cited ✓ (input-hardened)".format(
        term, len(hits), len(res["delivered"]))


CHECKS = (("person", check_person), ("subsystem", check_subsystem),
          ("symbol", check_symbol), ("grep", check_grep))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="spotlight CI integration gate over a real store")
    ap.add_argument("--store", required=True)
    ap.add_argument("--project", default=None,
                    help="project to check (required when the store holds several)")
    ap.add_argument("--require-all", action="store_true",
                    help="fail when a check is skipped for lack of input "
                         "(the live job sets this)")
    args = ap.parse_args(argv)

    # sqlite3.connect would silently CREATE a missing file; refuse instead.
    if not os.path.isfile(args.store):
        ap.error("store {!r} does not exist".format(args.store))
    # Read-only (#107): the gate only queries the store.
    try:
        conn = graphstore.open_store_readonly(args.store)
    except FileNotFoundError:
        ap.error("store {!r} does not exist".format(args.store))
    try:
        project = spotlight._detect_project(conn, args.project)
    except (ValueError, sqlite3.DatabaseError) as exc:
        conn.close()
        ap.error(str(exc))
    print("spotlight integration gate — project '{}', store {}".format(
        project, args.store))

    failed, skipped = 0, 0
    for name, fn in CHECKS:
        try:
            print("  ✓ {}".format(fn(conn, project)))
        except CheckSkipped as e:
            skipped += 1
            print("  - {} skipped: {}".format(name, e))
        except CheckError as e:
            failed += 1
            print("  ✗ {}: {}".format(name, e))
    conn.close()

    verified = len(CHECKS) - failed - skipped
    if failed:
        print("\nFAILED: {}/{} spotlight queries did not verify".format(
            failed, len(CHECKS)))
        return 1
    if skipped and args.require_all:
        print("\nFAILED: {} of {} checks skipped for lack of input and "
              "--require-all is set".format(skipped, len(CHECKS)))
        return 1
    print("\nOK: {} verified, {} skipped (no input in the store)".format(
        verified, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
