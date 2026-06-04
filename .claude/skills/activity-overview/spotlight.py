"""spotlight — the substrate analytics reader (Phase 8).

`extract` answers "what happened in this window?"; `spotlight` answers
cross-cutting questions orthogonal to any one window — "what has this person
done across all repos?" and (in later slices) symbol lifecycle, subsystem
blast radius, and full-text mining. Bounded SQL over the journey-graph store,
deterministically ordered, every row carrying its source citation.

Reader only: imports `graphstore` (+ `derive` where shared shaping helps); it
never writes the store and never touches the network. Primary output is raw,
cited JSON for the AI that builds reports; `--md` is a thin deterministic
render over the same JSON. See docs/.../activity-phase8-spotlight.md.

Stdlib only (sqlite3, argparse, json). Python 3.
"""

import argparse
import json
import sys

import graphstore


def _detect_project(conn, project):
    """Derive the project from the store when not passed (mirrors validate):
    the single non-sentinel project in `nodes`. Raises on an empty store or an
    ambiguous multi-project store so the caller disambiguates with --project."""
    if project is not None:
        return project
    projects = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL")
    })
    if len(projects) == 1:
        return projects[0]
    if not projects:
        raise ValueError("empty store: no project to query")
    raise ValueError(
        "store holds multiple projects {} — pass --project".format(projects))


def _project_repos(conn, project):
    """The non-sentinel repos for a project (people aggregate across them)."""
    return sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
            (project,))
    })


def _like_prefix(s):
    """Escape a LIKE prefix (mirrors graphstore.repo_code_events)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Contribution edge types, in the deterministic group order person-impact emits.
_CONTRIB_TYPES = ("authored", "reviewed", "merged", "reported", "commented")

# Defensive cap on the identity-chain walk (renames/moves should be shallow).
_CHAIN_DEPTH_CAP = 64


def _cite(node_id, data):
    """A citation row for a contribution target: its id plus whichever of
    number / sha / url / title the node data carries (a PR/issue has
    number+url+title; a commit has sha). Always JSON-serializable."""
    row = {"id": node_id}
    if data.get("number") is not None:
        row["number"] = data["number"]
    if data.get("sha") is not None:
        row["sha"] = data["sha"]
    if data.get("url") is not None:
        row["url"] = data["url"]
    if data.get("title") is not None:
        row["title"] = data["title"]
    return row


def person_impact(conn, project, login):
    """Everything `login` did across all repos in `project` (people are
    project-scoped). Returns a deterministically-ordered, citation-bearing
    Result dict. A miss (no person node) returns a needs_gather guidance result.

    Fields:
      modules / areas / is_bot   — from the person structure node.
      contributions              — get_edges(person, "out") grouped by type
                                   (authored/reviewed/merged/reported/commented):
                                   {count, items[ cited dst rows ]}.
      symbols_authored           — code_events with author==login on SYMBOL
                                   artifact ids (local part has a '#').
      authored_then_removed      — those symbols that later have a remove event.
      trains_anchored            — traverse_spine from her authored/reported
                                   PR/issue dsts -> distinct trains (anchor +
                                   reached size).
    Determinism: contributions by (edge_type, ts, dst_id); symbols by id;
    trains by anchor id.
    """
    person_id = graphstore.qualify_person(project, login)
    person = graphstore.get_node(conn, person_id)
    if person is None:
        return {
            "query": "person",
            "login": login,
            "project": project,
            "status": "needs_gather",
            "guidance": (
                "no person node for {}; gather a window where they "
                "contributed".format(login)),
        }

    pdata = person["data"] or {}
    result = {
        "query": "person",
        "login": login,
        "project": project,
        "status": "ok",
        "modules": pdata.get("modules") or [],
        "areas": pdata.get("areas") or [],
        "is_bot": bool(pdata.get("is_bot")),
    }

    # --- contributions: out-edges grouped by type, each row cited ---
    edges = graphstore.get_edges(conn, person_id, direction="out",
                                 edge_types=list(_CONTRIB_TYPES))
    groups = {}
    seed_ids = []  # authored/reported PR/issue dsts for the train traversal
    for e in edges:
        etype = e["edge_type"]
        dst = graphstore.get_node(conn, e["dst_id"])
        ddata = (dst["data"] if dst else {}) or {}
        row = _cite(e["dst_id"], ddata)
        row["ts"] = (dst["ts"] if dst else None) or ""
        groups.setdefault(etype, []).append(row)
        if etype in ("authored", "reported") and ("#pr-" in e["dst_id"]
                                                   or "#issue-" in e["dst_id"]):
            seed_ids.append(e["dst_id"])
    contributions = {}
    for etype in _CONTRIB_TYPES:
        rows = groups.get(etype)
        if not rows:
            continue
        rows.sort(key=lambda r: (r["ts"], r["id"]))
        contributions[etype] = {"count": len(rows), "items": rows}
    result["contributions"] = contributions

    # --- symbols authored (+ authored_then_removed) ---
    # SYMBOL artifact ids carry a '#' in their LOCAL part (path#lang:subkind:name).
    # code_events is keyed by the repo-qualified id; scan the project's repos.
    symbols_authored = []
    removed = []
    for repo in _project_repos(conn, project):
        prefix = "{}/{}#".format(project, repo)
        like = _like_prefix(prefix) + "%"
        rows = conn.execute(
            "SELECT DISTINCT artifact_id FROM code_events "
            "WHERE author=? AND event='add' AND artifact_id LIKE ? ESCAPE '\\'",
            (login, like)).fetchall()
        for (aid,) in rows:
            local = aid[len(prefix):] if aid.startswith(prefix) else aid
            if "#" not in local:
                continue  # file-level artifact, not a symbol
            symbols_authored.append({"id": aid})
            # later remove event (by anyone) on the same artifact?
            rm = conn.execute(
                "SELECT 1 FROM code_events "
                "WHERE artifact_id=? AND event='remove' LIMIT 1",
                (aid,)).fetchone()
            if rm:
                removed.append({"id": aid})
    symbols_authored.sort(key=lambda r: r["id"])
    removed.sort(key=lambda r: r["id"])
    result["symbols_authored"] = symbols_authored
    result["authored_then_removed"] = removed

    # --- trains anchored: spine traversal from her authored/reported PR/issues ---
    trains = []
    if seed_ids:
        seen_anchors = set()
        for seed in sorted(set(seed_ids)):
            reach = graphstore.traverse_spine(conn, [seed])
            reached = reach["reached"]
            if not reached:
                continue
            # the anchor is the deterministically-smallest reached id, so the
            # same train (whatever seed entered it) collapses to one entry.
            anchor = min(reached)
            if anchor in seen_anchors:
                continue
            seen_anchors.add(anchor)
            anchor_node = graphstore.get_node(conn, anchor)
            adata = (anchor_node["data"] if anchor_node else {}) or {}
            train = _cite(anchor, adata)
            train["anchor"] = anchor
            train["reached"] = len(reached)
            trains.append(train)
        trains.sort(key=lambda t: t["anchor"])
    result["trains_anchored"] = trains

    return result


def _normalize_artifact_id(conn, project, artifact_id):
    """Accept an artifact id in qualified (`{project}/{repo}#{local}`) or local
    (`{local}`) form and return the qualified id, or None if it can't be
    resolved. A qualified id is returned as-is. A local id is matched against
    the project's repos: the first repo for which the qualified id has a node OR
    a code_events row wins (deterministic by repo order)."""
    if artifact_id.startswith("{}/".format(project)):
        return artifact_id  # already project-qualified
    for repo in _project_repos(conn, project):
        cand = graphstore.qualify_id(project, repo, artifact_id)
        if graphstore.get_node(conn, cand) is not None:
            return cand
        row = conn.execute(
            "SELECT 1 FROM code_events WHERE artifact_id=? LIMIT 1",
            (cand,)).fetchone()
        if row:
            return cand
    return None


def pattern_evolution(conn, project, artifact_id):
    """A symbol/file artifact's FULL lifecycle across all history (not one
    window). Returns a deterministically-ordered, citation-bearing Result; an
    unknown id (no node and no code_events) yields a needs_gather result.

    Fields:
      lifecycle      — get_code_events(artifact_id) in (date, commit) order;
                       each row {event, commit, author, date, before, after}
                       cited by its commit ref. File artifacts leave
                       before/after null; symbols carry bounded before/after.
      identity_chain — the artifact's true cross-path history assembled by
                       walking `replaced_by` (src->dst) forward and
                       `identity_from` (dst->src) backward from the given id.
                       Ordered A->B->C across renames/moves; each link past the
                       head carries the move `confidence`/`basis` from edge data.
    Determinism: lifecycle by (date, commit); chain in link order (depth-capped).
    """
    qid = _normalize_artifact_id(conn, project, artifact_id)
    events = graphstore.get_code_events(conn, qid) if qid else []
    node = graphstore.get_node(conn, qid) if qid else None
    if not events and node is None:
        return {
            "query": "symbol",
            "artifact_id": artifact_id,
            "project": project,
            "status": "needs_gather",
            "guidance": (
                "no node or code_events for {}; gather a window that touches "
                "this artifact".format(artifact_id)),
        }

    # --- lifecycle: code_events in (date, commit) order, each cited ---
    events.sort(key=lambda e: ((e["date"] or ""), e["commit_sha"]))
    lifecycle = []
    for e in events:
        ref = e.get("ref")
        row = {
            "event": e["event"],
            "commit": e["commit_sha"],
            "author": e["author"],
            "date": e["date"],
            "before": e["before"],
            "after": e["after"],
        }
        # cite by the commit ref when present (url/sha), else the bare sha.
        if isinstance(ref, dict):
            if ref.get("url") is not None:
                row["url"] = ref["url"]
            if ref.get("sha") is not None:
                row["ref_sha"] = ref["sha"]
        lifecycle.append(row)

    # --- identity_chain: walk replaced_by forward + identity_from backward ---
    # Each artifact stores both edges (replaced_by src->dst, identity_from
    # dst->src, both carrying move_confidence/move_basis). Walk forward via
    # replaced_by out-edges, backward via identity_from out-edges, to assemble
    # the ordered chain A->B->C and the move link metadata.
    forward = []  # ids after qid, in move order
    backward = []  # ids before qid, in reverse move order
    link_meta = {}  # id -> {confidence, basis} for the move that PRODUCED it

    if qid:
        seen = {qid}
        # forward: qid replaced_by next
        cur = qid
        depth = 0
        while depth < _CHAIN_DEPTH_CAP:
            edges = graphstore.get_edges(conn, cur, direction="out",
                                         edge_types=["replaced_by"])
            if not edges:
                break
            e = edges[0]  # deterministic (get_edges orders by dst_id)
            nxt = e["dst_id"]
            if nxt in seen:
                break
            d = e["data"] or {}
            link_meta[nxt] = {
                "confidence": d.get("move_confidence"),
                "basis": d.get("move_basis"),
            }
            forward.append(nxt)
            seen.add(nxt)
            cur = nxt
            depth += 1
        # backward: qid identity_from prev (i.e. prev replaced_by qid)
        cur = qid
        depth = 0
        while depth < _CHAIN_DEPTH_CAP:
            edges = graphstore.get_edges(conn, cur, direction="out",
                                         edge_types=["identity_from"])
            if not edges:
                break
            e = edges[0]
            prv = e["dst_id"]
            if prv in seen:
                break
            d = e["data"] or {}
            # this is the move that produced `cur` from `prv`
            link_meta[cur] = {
                "confidence": d.get("move_confidence"),
                "basis": d.get("move_basis"),
            }
            backward.append(prv)
            seen.add(prv)
            cur = prv
            depth += 1

    ordered = list(reversed(backward)) + [qid] + forward
    identity_chain = []
    for i, aid in enumerate(ordered):
        c = {"id": aid}
        meta = link_meta.get(aid)
        if meta and (meta.get("confidence") or meta.get("basis")):
            c["confidence"] = meta.get("confidence")
            c["basis"] = meta.get("basis")
        identity_chain.append(c)

    return {
        "query": "symbol",
        "artifact_id": qid,
        "project": project,
        "status": "ok",
        "lifecycle": lifecycle,
        "identity_chain": identity_chain,
    }


def subsystem_split(conn, project, area, ts_from=None, ts_to=None):
    """An area's activity + blast radius (optional time range). Returns a
    deterministically-ordered, citation-bearing Result; an unknown area (no
    `area-<area>` structure node in any project repo) yields a needs_gather
    result.

    Fields:
      contributors  — codeowners (inverse `owns`, person->area) plus the authors
                      of commits/PRs that `touches` the area, deduped to logins
                      with their relation (`owns` wins over `touches`), cited.
      shipped/stalled — the PRs attributed to the area (a PR is attributed when
                      one of its `part_of` commits `touches` the area, or the PR
                      itself `touches` the area), split by status: merged/closed
                      -> shipped, open -> stalled. Filtered by node `ts` when
                      --from/--to are given. Cited by number/url.
      depends_on    — blast radius: `depends_on` edges OUT of the area (what it
                      depends on) and INTO it (what depends on it), each carrying
                      version/transitive from edge data.
    Determinism: contributors by login; items by number then id; deps by area id.
    """
    # resolve the area node across the project's repos
    area_qid = None
    area_local = "area-{}".format(area)
    for repo in _project_repos(conn, project):
        cand = graphstore.qualify_id(project, repo, area_local)
        if graphstore.get_node(conn, cand) is not None:
            area_qid = cand
            break
    if area_qid is None:
        return {
            "query": "subsystem",
            "area": area,
            "project": project,
            "status": "needs_gather",
            "guidance": (
                "no area node for {}; gather a window that touches it (areas "
                "are sparse in short windows)".format(area)),
        }

    def _in_range(ts):
        if ts is None:
            return False
        if ts_from is not None and ts < ts_from:
            return False
        if ts_to is not None and ts > ts_to:
            return False
        return True

    def _author_login(node):
        return (node["data"] or {}).get("author") if node else None

    # --- in-edges to the area: owns (person) + touches (commit/pr) ---
    in_edges = graphstore.get_edges(conn, area_qid, direction="in",
                                    edge_types=["owns", "touches"])
    relations = {}  # login -> relation ('owns' beats 'touches')
    cite_by_login = {}
    touching_src_ids = []
    for e in in_edges:
        if e["edge_type"] == "owns":
            # src is a person node; recover the login
            person = graphstore.get_node(conn, e["src_id"])
            login = (person["data"] or {}).get("login") if person else None
            if login is None:
                login = graphstore.parse_id(e["src_id"])["local"]
                if login.startswith("person-"):
                    login = login[len("person-"):]
            relations[login] = "owns"
        elif e["edge_type"] == "touches":
            touching_src_ids.append(e["src_id"])

    # touching commits/PRs -> their authors are touches contributors, and the
    # PRs they belong to are the area's attributed items.
    attributed_prs = {}  # pr_id -> pr node
    for src_id in touching_src_ids:
        src = graphstore.get_node(conn, src_id)
        login = _author_login(src)
        if login is not None and login not in relations:
            relations[login] = "touches"
        # if the toucher is itself a PR node, attribute it directly
        if "#pr-" in src_id and src is not None:
            attributed_prs[src_id] = src
        # follow part_of (commit -> pr) to attribute the PR
        for pe in graphstore.get_edges(conn, src_id, direction="out",
                                       edge_types=["part_of"]):
            if "#pr-" in pe["dst_id"]:
                pr = graphstore.get_node(conn, pe["dst_id"])
                if pr is not None:
                    attributed_prs[pe["dst_id"]] = pr

    contributors = [
        {"login": login, "relation": relations[login]}
        for login in sorted(relations)
    ]

    # --- shipped / stalled split over the attributed PRs ---
    shipped, stalled = [], []
    for pr_id in sorted(attributed_prs):
        pr = attributed_prs[pr_id]
        if (ts_from is not None or ts_to is not None) and not _in_range(pr["ts"]):
            continue
        pdata = pr["data"] or {}
        row = _cite(pr_id, pdata)
        merged = bool(pdata.get("merged"))
        state = pdata.get("state")
        if merged or state == "closed":
            shipped.append(row)
        else:
            stalled.append(row)
    _item_key = lambda r: (r.get("number") if r.get("number") is not None
                           else float("inf"), r["id"])
    shipped.sort(key=_item_key)
    stalled.sort(key=_item_key)

    # --- depends_on blast radius (out = depends on; in = depended upon by) ---
    def _dep_row(other_id, data):
        d = data or {}
        local = graphstore.parse_id(other_id)["local"]
        if local.startswith("area-"):
            local = local[len("area-"):]
        return {
            "area": local,
            "id": other_id,
            "version": d.get("version"),
            "transitive": d.get("transitive"),
        }

    deps_out, deps_in = [], []
    for e in graphstore.get_edges(conn, area_qid, direction="out",
                                  edge_types=["depends_on"]):
        deps_out.append(_dep_row(e["dst_id"], e["data"]))
    for e in graphstore.get_edges(conn, area_qid, direction="in",
                                  edge_types=["depends_on"]):
        deps_in.append(_dep_row(e["src_id"], e["data"]))
    deps_out.sort(key=lambda d: d["id"])
    deps_in.sort(key=lambda d: d["id"])

    return {
        "query": "subsystem",
        "area": area,
        "project": project,
        "status": "ok",
        "contributors": contributors,
        "shipped": shipped,
        "stalled": stalled,
        "depends_on": {"out": deps_out, "in": deps_in},
    }


# --------------------------------------------------------------------------
# Markdown render (thin, deterministic formatter over the same JSON).
# --------------------------------------------------------------------------

def _render_person_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: person `{}`\n\n_needs gather:_ {}".format(
            res["login"], res["guidance"])
    if res["status"] == "fts_unavailable":
        return "## spotlight\n\n_FTS unavailable:_ {}".format(
            res.get("guidance", ""))
    lines = ["## spotlight: person `{}`{}".format(
        res["login"], " (bot)" if res["is_bot"] else "")]
    lines.append("")
    lines.append("- modules: {}".format(", ".join(res["modules"]) or "—"))
    lines.append("- areas: {}".format(", ".join(res["areas"]) or "—"))
    lines.append("")
    lines.append("### contributions")
    for etype in _CONTRIB_TYPES:
        g = res["contributions"].get(etype)
        if not g:
            continue
        lines.append("- **{}** ({})".format(etype, g["count"]))
        for row in g["items"]:
            ref = row.get("url") or row.get("sha") or row["id"]
            label = row.get("title") or row.get("id")
            lines.append("  - {} — {}".format(label, ref))
    lines.append("")
    lines.append("### symbols authored ({}; {} later removed)".format(
        len(res["symbols_authored"]), len(res["authored_then_removed"])))
    for s in res["symbols_authored"]:
        lines.append("- {}".format(s["id"]))
    lines.append("")
    lines.append("### trains anchored ({})".format(len(res["trains_anchored"])))
    for t in res["trains_anchored"]:
        ref = t.get("url") or t["anchor"]
        lines.append("- {} (reached {}) — {}".format(
            t["anchor"], t["reached"], ref))
    return "\n".join(lines)


def _render_symbol_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: symbol `{}`\n\n_needs gather:_ {}".format(
            res["artifact_id"], res["guidance"])
    lines = ["## spotlight: symbol `{}`".format(res["artifact_id"]), ""]
    lines.append("### lifecycle ({})".format(len(res["lifecycle"])))
    for e in res["lifecycle"]:
        ref = e.get("url") or e.get("ref_sha") or e["commit"]
        lines.append("- **{}** by {} on {} — {}".format(
            e["event"], e.get("author") or "?", e.get("date") or "?", ref))
    lines.append("")
    lines.append("### identity chain ({})".format(len(res["identity_chain"])))
    for c in res["identity_chain"]:
        if c.get("confidence"):
            lines.append("- {} ({} / {})".format(
                c["id"], c.get("confidence"), c.get("basis")))
        else:
            lines.append("- {}".format(c["id"]))
    return "\n".join(lines)


def _render_subsystem_md(res):
    if res["status"] == "needs_gather":
        return "## spotlight: subsystem `{}`\n\n_needs gather:_ {}".format(
            res["area"], res["guidance"])
    lines = ["## spotlight: subsystem `{}`".format(res["area"]), ""]
    lines.append("### contributors ({})".format(len(res["contributors"])))
    for c in res["contributors"]:
        lines.append("- {} ({})".format(c["login"], c["relation"]))
    lines.append("")
    lines.append("### shipped ({}) / stalled ({})".format(
        len(res["shipped"]), len(res["stalled"])))
    for label, items in (("shipped", res["shipped"]), ("stalled", res["stalled"])):
        for r in items:
            ref = r.get("url") or r["id"]
            label2 = r.get("title") or r.get("id")
            lines.append("- [{}] {} — {}".format(label, label2, ref))
    lines.append("")
    deps = res["depends_on"]
    lines.append("### depends_on blast radius (out {} / in {})".format(
        len(deps["out"]), len(deps["in"])))
    for d in deps["out"]:
        lines.append("- depends on {} (v{}{})".format(
            d["area"], d.get("version"),
            ", transitive" if d.get("transitive") else ""))
    for d in deps["in"]:
        lines.append("- depended on by {} (v{}{})".format(
            d["area"], d.get("version"),
            ", transitive" if d.get("transitive") else ""))
    return "\n".join(lines)


_RENDERERS = {
    "person": _render_person_md,
    "symbol": _render_symbol_md,
    "subsystem": _render_subsystem_md,
}


def render_md(res):
    """Deterministic markdown over a Result dict (a thin formatter, not a
    second data path)."""
    return _RENDERERS[res["query"]](res)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="spotlight — cross-cutting analytics over a journey store.")
    parser.add_argument("query", help="query subcommand (e.g. 'person')")
    parser.add_argument("args", nargs="*", help="query arguments")
    parser.add_argument("--store", required=True, help="path to the store db")
    parser.add_argument("--project", default=None,
                        help="project (auto-detected from the store if omitted)")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="subsystem: filter items at/after this ts")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="subsystem: filter items at/before this ts")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true",
                     help="emit raw cited JSON (default)")
    fmt.add_argument("--md", action="store_true",
                     help="emit a deterministic markdown render")
    args = parser.parse_args(argv)

    if args.query not in _RENDERERS:
        parser.error("unknown query: {}".format(args.query))
    if not args.args:
        parser.error("query '{}' needs an argument".format(args.query))

    conn = graphstore.open_store(args.store)
    project = _detect_project(conn, args.project)

    if args.query == "person":
        res = person_impact(conn, project, args.args[0])
    elif args.query == "symbol":
        res = pattern_evolution(conn, project, args.args[0])
    elif args.query == "subsystem":
        res = subsystem_split(conn, project, args.args[0],
                              ts_from=args.ts_from, ts_to=args.ts_to)
    conn.close()

    if args.md:
        print(render_md(res))
    else:
        print(json.dumps(res, sort_keys=True, indent=2))
    return 0  # needs_gather / fts_unavailable are valid answers (exit 0)


if __name__ == "__main__":
    sys.exit(main())
