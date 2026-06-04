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


_RENDERERS = {"person": _render_person_md}


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
    conn.close()

    if args.md:
        print(render_md(res))
    else:
        print(json.dumps(res, sort_keys=True, indent=2))
    return 0  # needs_gather / fts_unavailable are valid answers (exit 0)


if __name__ == "__main__":
    sys.exit(main())
