"""Phase 9 project digest aggregator (S3).

One level above `extract`: given a project and its member repos, materialize each
member's enriched bundle via the EXISTING single-repo pipeline
(`extract.extract` + `link.enrich`), then merge the per-member results into one
project view. Repo is an explicit dimension throughout — single-repo
`extract`/`link`/`render` are untouched and byte-stable. See
docs/superpowers/specs/2026-06-05-activity-phase9-multirepo.md (S3).
"""
import argparse
import json
import sys

import extract as extract_mod
import graphstore
import link as link_mod


def member_bundles(conn, project, repos, ts_from, ts_to, *, backfill=None,
                   backfill_budget=50):
    """Materialize + enrich one bundle per member repo, in `repos` order.
    Each is the single-repo enriched bundle for that member, produced via
    the unmodified extract -> link.enrich path.
    Returns [{"repo": "owner/repo", "bundle": <enriched dict>}, ...]."""
    out = []
    for repo in repos:
        bundle = extract_mod.extract(
            conn, project, repo, ts_from, ts_to,
            backfill=backfill, backfill_budget=backfill_budget,
            warn=lambda _m: None)
        bundle = link_mod.enrich(bundle)
        out.append({"repo": repo, "bundle": bundle})
    return out


def spine_components(conn, project, repos, ts_from, ts_to, max_depth=6):
    """Connected components of the project-wide spine, seeded by in-window social
    nodes across `repos`. Each returned frozenset of qualified node ids is one
    decision train (possibly spanning members — the store's cross-repo
    closes/cross_ref edges join them). Deterministic: components are ordered by
    their lexicographically smallest member id.

    Three behaviours to know:
    - Reachability is capped at `max_depth` hops (matching extract), so a single
      real train spanning more than `max_depth` spine hops can split into two
      components.
    - traverse_spine has no window filter, so a component MAY include out-of-window
      social anchors reachable from an in-window seed (cross-window train context).
    - Each frozenset holds only PR/issue SOCIAL anchors; commits/areas traversed
      bridge the component but are dropped from the returned set (trains are keyed
      off social anchors)."""
    in_window = graphstore.range_query(conn, project, repos, ts_from, ts_to)
    socials = [n["id"] for n in in_window if n["node_class"] == "social"]
    seen, comps = set(), []
    for sid in socials:
        if sid in seen:
            continue
        reached = graphstore.traverse_spine(
            conn, [sid], max_depth=max_depth, skip_dead=True)["reached"]
        # keep reached SOCIAL anchors (issues/prs); commits/areas reached are train
        # members but trains are keyed off social anchors.
        comp = {nid for nid in reached
                if graphstore.parse_id(nid)["local"].startswith(("pr-", "issue-"))}
        comp.add(sid)
        # only social anchors can be seeds (the loop iterates `socials`), so
        # tracking anchors in `seen` is enough to keep components disjoint.
        seen |= comp
        comps.append(frozenset(comp))
    comps.sort(key=lambda c: min(c))
    return comps
