"""extract.py — materialize a rev-13 RAW bundle view out of the journey-graph store.

This is the reader counterpart of `gather.fold_bundle`: where fold *writes* a raw
bundle into the SQLite property graph by identity, `extract` *reads* a window back
out and reconstructs the rev-13 raw bundle (`meta`, `prs`, `issues`, `commits`,
`code_events`, `milestones`, `releases`). It emits ONLY raw keys — `link.enrich`
adds every derived key (trains/buckets/artifacts/timeline/feature_deltas/people/
modules/forecast) downstream, so render/report and their tests stay untouched.

Materialization approach (slice 7a part 1)
------------------------------------------
1. Range-query the in-window social + code nodes (graphstore.range_query) — these
   are conceptually `in_window: true` and are what activity counts reflect.
2. Seed a bounded spine traversal (graphstore.traverse_spine) from every in-window
   social node's train anchor to pull out-of-window spine *context* nodes
   (`in_window: false`). `missing` spine ids (referenced but not yet stored — e.g.
   an out-of-window issue a PR closes) are only WARNED about here; wiring backfill
   is slice 7c. Activity counts never include context or missing nodes.
3. Materialize the raw arrays from the node `data` blobs. The materialization
   *source* for the arrays is `graphstore.repo_nodes` (all social/code/structure
   nodes for the repo, NOT window-filtered): the raw arrays must include NULL-ts
   nodes the window scan legitimately drops (un-dated commits, degenerate
   fixtures), while the window/spine sets above still drive the in_window tagging.
4. Reconstruct `code_events` from the ledger in insertion (rowid) order via
   `graphstore.repo_code_events`, which equals the original source order.
5. Reconstruct `meta` (owner/repo/from/to, + clone_sha if recorded) so it
   round-trips. `ref_date`/`period` are NOT stored by P6 and are intentionally not
   reconstructed — enrich falls back to meta["to"] and {from,to} deterministically.

Deterministic ordering matches the golden bundles: prs/issues by number, commits
by sha, milestones by number, releases by tag/name, code_events by source order.

NOT YET MATERIALIZED (next agent — slice 7a part 2, additive here):
  - workflow_stats   (needs a workflow ledger; bundle_p2)
  - code_graph       (areas/edges/edge_extraction; bundle_p2/p3b)
  - code_owners      (CODEOWNERS projection; bundle_p3b)
  - label_taxonomy   (label facet round-trip; bundle_p2/p3b/p3c)
  - symbol_events    (symbol-granular ledger; richer artifacts)
Each slots into `_materialize_*` helpers and `extract`'s assembled dict below;
extend `_RAW_KEYS` and add a `_materialize_<key>` reader. The equivalence-gate
harness in test_extract.py is reusable as-is for the new goldens.
"""

import sys

import graphstore

# Raw keys this slice materializes. (See module docstring for the not-yet list.)
_RAW_KEYS = ("meta", "prs", "issues", "commits", "code_events",
             "milestones", "releases")


def _local(node_id):
    return graphstore.parse_id(node_id)["local"]


def _train_anchor(node):
    """Train-anchor id for an in-window social node — the spine seed. A PR
    anchors on itself; an issue anchors on itself. (The anchor is just the seed
    we hand traverse_spine; the train is the component reachable from it.)"""
    return node["id"]


def extract(conn, project, repo, ts_from, ts_to, max_depth=6, warn=None):
    """Materialize a rev-13 RAW bundle view for project/repo over [ts_from, ts_to].

    Returns a bundle dict carrying ONLY raw keys; run `link.enrich` on it to add
    the derived projections. `warn` is an optional callable(str) for the spine
    `missing` notice (defaults to stderr).
    """
    if warn is None:
        warn = lambda msg: sys.stderr.write(msg + "\n")  # noqa: E731

    # 1. In-window social + code nodes (conceptually in_window: true). These drive
    #    activity counts and seed the spine.
    in_window = graphstore.range_query(conn, project, [repo], ts_from, ts_to)
    in_window_ids = {n["id"] for n in in_window}

    # 2. Seed trains from in-window social nodes, then bounded spine traversal to
    #    surface out-of-window context (in_window: false). Missing => warn only.
    seeds = [_train_anchor(n) for n in in_window if n["node_class"] == "social"]
    spine = graphstore.traverse_spine(conn, seeds, max_depth=max_depth)
    if spine["missing"]:
        warn("extract: {} spine context node(s) referenced but not stored "
             "(backfill is slice 7c): {}".format(
                 len(spine["missing"]), ", ".join(sorted(spine["missing"]))))
    # context_ids: reached-but-out-of-window nodes (in_window: false). Recorded for
    # completeness / future per-node tagging; the raw arrays below are sourced from
    # repo_nodes so they already include these.
    _context_ids = set(spine["reached"]) - in_window_ids  # noqa: F841

    # 3+4. Materialize raw arrays from the full repo node set + the ledger.
    bundle = {"meta": _materialize_meta(conn, project, repo, ts_from, ts_to)}

    socials = graphstore.repo_nodes(conn, project, repo, node_class="social")
    bundle["prs"] = _by(
        [n["data"] for n in socials if _local(n["id"]).startswith("pr-")],
        key=lambda d: d.get("number"))
    bundle["issues"] = _by(
        [n["data"] for n in socials if _local(n["id"]).startswith("issue-")],
        key=lambda d: d.get("number"))

    codes = graphstore.repo_nodes(conn, project, repo, node_class="code")
    bundle["commits"] = _by([n["data"] for n in codes],
                            key=lambda d: str(d.get("sha")))

    structure = graphstore.repo_nodes(conn, project, repo, node_class="structure")
    bundle["milestones"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("milestone-")],
        key=lambda d: (d.get("number") is None, d.get("number"),
                       d.get("title") or ""))
    bundle["releases"] = _by(
        [n["data"] for n in structure
         if _local(n["id"]).startswith("release-")],
        key=lambda d: d.get("tag_name") or d.get("name") or "")

    bundle["code_events"] = _materialize_code_events(conn, project, repo)

    return bundle


def _by(records, key):
    """Sort records deterministically by `key`; stable and idempotent."""
    return sorted(records, key=key)


def _materialize_meta(conn, project, repo, ts_from, ts_to):
    """Reconstruct raw `meta` so it round-trips: owner/repo/window (+clone_sha if
    recorded). ref_date/period are not P6-stored — enrich derives equivalents."""
    meta = {"owner": project, "repo": repo, "from": ts_from, "to": ts_to}
    clone_sha = graphstore.get_clone_sha(conn, project, repo)
    if clone_sha:
        meta["clone_sha"] = clone_sha
    return meta


def _materialize_code_events(conn, project, repo):
    """Reconstruct the raw `code_events` array from the file-level ledger, in
    original source order (ledger rowid order). rename/copy recover `old_path`
    from the event's `detail` (fold_bundle stored old_path there)."""
    out = []
    for ev in graphstore.repo_code_events(conn, project, repo):
        rec = {
            "commit": ev["commit_sha"],
            "author": ev["author"],
            "date": ev["date"],
            "change": ev["event"],
            "path": _local(ev["artifact_id"]),
        }
        if ev["event"] in ("rename", "copy") and ev["detail"]:
            rec["old_path"] = ev["detail"]
        out.append(rec)
    return out
