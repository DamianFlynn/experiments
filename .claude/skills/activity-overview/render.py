"""Offline diagram render: enriched bundle -> Mermaid .mmd files, validated by mmdc.

Pure emitters build the diagram text from existing bundle fields; `mmdc` (mermaid-cli)
compiles every file so a diagram that would not render fails the run. No network."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

INSTALL_HINT = (
    "Install mermaid-cli: `npm install -g @mermaid-js/mermaid-cli`."
)

# Adaptive layout thresholds for emit_train_flowchart:
#   mode C (full, with area nodes) when prs <= MAX_PRS and areas <= MAX_AREAS;
#   mode A (bare issue→PR→outcome only) otherwise.
TRAIN_FLOW_MAX_PRS = 4
TRAIN_FLOW_MAX_AREAS = 5

_PIE_ROWS = [
    ("Shipped", "shipped"),
    ("In flight", "in_flight"),
    ("Rejected", "rejected"),
    ("Next candidates", "next_candidates"),
]


def emit_buckets_pie(bundle):
    """A Mermaid `pie` of bucket counts. Zero-count slices are dropped."""
    meta = bundle.get("meta", {})
    buckets = bundle.get("buckets", {})
    lines = ["pie showData", f"    title Work by status ({meta.get('from','')} → {meta.get('to','')})"]
    any_slice = False
    for label, key in _PIE_ROWS:
        count = len(buckets.get(key, []))
        if count:
            lines.append(f'    "{label}" : {count}')
            any_slice = True
    if not any_slice:
        lines.append('    "No activity" : 1')
    return "\n".join(lines) + "\n"


def _day(ts, default):
    return (ts or default or "")[:10]


def _gantt_label(text):
    """Mermaid gantt task names cannot contain ':' (the field separator) or
    newlines, and `%%` starts a comment. Sanitise and trim."""
    clean = (text or "").replace(":", " -").replace("\n", " ")
    while "%%" in clean:  # collapse any run of '%' down so no comment marker survives
        clean = clean.replace("%%", "%")
    return clean.strip()[:60] or "item"


def emit_timeline_gantt(bundle):
    """A Mermaid `gantt` of PR lifespans + releases across the window."""
    meta = bundle.get("meta", {})
    frm = meta.get("from") or "1970-01-01"
    to = meta.get("to") or frm
    lines = [
        "gantt",
        f"    title Timeline ({frm} → {to})",
        "    dateFormat YYYY-MM-DD",
        "    axisFormat %m-%d",
    ]
    prs = sorted(bundle.get("prs", []),
                 key=lambda p: (p.get("created_at") or p.get("merged_at") or ""))
    if prs:
        lines.append("    section Pull requests")
        for p in prs:
            start = _day(p.get("created_at"), frm)
            end = _day(p.get("merged_at") or p.get("closed_at"), to)
            if end < start:
                end = start
            if p.get("merged"):
                status = "done"
            elif p.get("state") == "closed":
                status = "crit"
            else:
                status = "active"
            label = _gantt_label(f"#{p.get('number', '?')} {p.get('title') or ''}")
            lines.append(f"    {label} :{status}, {start}, {end}")
    releases = bundle.get("releases", [])
    if releases:
        lines.append("    section Releases")
        for r in releases:
            day = _day(r.get("published_at"), to)
            label = _gantt_label(r.get("name") or r.get("tag_name") or "release")
            lines.append(f"    {label} :milestone, {day}, 0d")
    if not prs and not releases:
        lines.append("    section Activity")
        lines.append(f"    No dated items :active, {_day(frm, '2026-01-01')}, {_day(to, frm)}")
    return "\n".join(lines) + "\n"


def _timeline_text(text):
    """Mermaid `timeline` event text cannot contain ':' (section/event separator)
    or newlines. Sanitise like the gantt labels."""
    clean = (text or "").replace(":", " -").replace("\n", " ")
    while "%%" in clean:
        clean = clean.replace("%%", "%")
    return clean.strip()[:60] or "event"


def emit_content_timeline(bundle):
    """A Mermaid `timeline` of artifact lifecycle events (built/changed/dropped),
    one period per date with its events. Derived from `artifacts`."""
    meta = bundle.get("meta", {})
    lines = ["timeline",
             f"    title Content lifecycle ({meta.get('from','')} - {meta.get('to','')})"]
    by_date = {}
    verb = {"add": "built", "change": "changed", "remove": "dropped"}
    for art in bundle.get("artifacts", {}).values():
        for ev in art.get("lifecycle", []):
            day = (ev.get("date") or "")[:10] or "undated"
            evt = ev.get("event", "")
            label = _timeline_text(
                f"{verb.get(evt, evt)} {art.get('name', '?')} "
                f"({art.get('kind', '?')}) by {ev.get('author') or '?'}")
            by_date.setdefault(day, []).append(label)
    if not by_date:
        lines.append(f"    {meta.get('from') or '—'} : no content events")
        return "\n".join(lines) + "\n"
    for day in sorted(by_date):
        events = " : ".join(by_date[day])
        lines.append(f"    {day} : {events}")
    return "\n".join(lines) + "\n"


_DELTA_KINDS = ["add", "drop", "change"]


def emit_deltas_bar(bundle):
    """A Mermaid `xychart-beta` bar of feature_delta counts by kind.

    Mermaid has no standalone bar diagram; `xychart-beta` is its native bar chart
    (category x-axis + numeric bar series), which renders counts-by-category
    correctly — unlike `pie`, which would read as proportions and lose ordering."""
    counts = {k: 0 for k in _DELTA_KINDS}
    for d in bundle.get("feature_deltas", []):
        if d.get("kind") in counts:
            counts[d["kind"]] += 1
    values = [counts[k] for k in _DELTA_KINDS]
    top = max(values) or 1
    lines = [
        "xychart-beta",
        '    title "Feature changes by kind"',
        '    x-axis [add, drop, change]',
        f'    y-axis "Count" 0 --> {top}',
        f"    bar [{', '.join(str(v) for v in values)}]",
    ]
    return "\n".join(lines) + "\n"


def _node_id(prefix, text):
    """A safe Mermaid node id from arbitrary text (alnum + underscore)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in (text or ""))
    return f"{prefix}_{safe}"[:60]


def _area_tail(area):
    return (area or "").rstrip("/").split("/")[-1] or area


def _flow_label(text):
    """Sanitise a flowchart label: drop quotes/newlines that would break the node."""
    clean = (text or "").replace('"', "'").replace("\n", " ")
    return clean.strip()[:40] or "?"


def emit_contributor_graph(bundle):
    """A Mermaid `flowchart` of people <-> code-area edges.

    Each person links to the areas they authored/reviewed in (from `people.modules`,
    falling back to `people.areas`). Emits a `No contributor data` placeholder when
    no edges can be built. Derived from existing bundle fields."""
    people = bundle.get("people", {})
    lines = ["flowchart LR"]
    edges = []
    area_nodes = {}
    person_nodes = {}
    for login, p in sorted(people.items()):
        mods = p.get("modules") or p.get("areas") or []
        if not mods:
            continue
        pid = _node_id("p", login)
        person_nodes[pid] = login
        for area in mods:
            aid = _node_id("a", area)
            area_nodes[aid] = _area_tail(area)
            edges.append((pid, aid))
    if not edges:
        lines.append("    none[No contributor data]")
        return "\n".join(lines) + "\n"
    for pid, login in sorted(person_nodes.items()):
        lines.append(f'    {pid}["{_flow_label(login)}"]')
    for aid, label in sorted(area_nodes.items()):
        lines.append(f'    {aid}("{_flow_label(label)}")')
    for pid, aid in sorted(set(edges)):
        lines.append(f"    {pid} --> {aid}")
    return "\n".join(lines) + "\n"


def emit_kind_breakdown(bundle):
    """A Mermaid `pie` of issues by `kind` (feature/bug/idea/...). The at-a-glance
    kind mix; proportions are the point, so `pie` (the spec palette entry)."""
    counts = {}
    for issue in bundle.get("issues", []):
        kind = issue.get("kind") or "other"
        counts[kind] = counts.get(kind, 0) + 1
    lines = ["pie showData", "    title Issues by kind"]
    if not counts:
        lines.append('    "No issues" : 1')
        return "\n".join(lines) + "\n"
    for kind in sorted(counts, key=lambda k: (-counts[k], k)):
        lines.append(f'    "{kind}" : {counts[kind]}')
    return "\n".join(lines) + "\n"


def emit_train_flowchart(bundle, train):
    """A Mermaid `flowchart` telling the life story of one train.

    Outcome mapping: 'shipped' -> 'Shipped [-> <milestone>]';
    'rejected' -> 'Rejected'; anything else (open, in_flight, ...) -> 'In flight'.

    Adaptive layout:
      mode C (default): PR nodes annotated with distinct code_area nodes when
        len(prs) <= TRAIN_FLOW_MAX_PRS and len(distinct code_areas) <= TRAIN_FLOW_MAX_AREAS.
      mode A: bare issue -> PR -> outcome chain when either threshold is exceeded.
    """
    issues_by_num = {i["number"]: i for i in bundle.get("issues", [])}
    prs_by_num = {p["number"]: p for p in bundle.get("prs", [])}

    train_prs = train.get("prs", [])
    code_areas = train.get("code_areas", [])
    outcome = train.get("outcome", "")

    # Determine mode using DISTINCT non-empty areas so duplicates don't
    # wrongly push over the threshold.
    distinct_areas = set(a for a in code_areas if a)
    mode_c = (len(train_prs) <= TRAIN_FLOW_MAX_PRS and
              len(distinct_areas) <= TRAIN_FLOW_MAX_AREAS)

    lines = ["flowchart LR"]

    # --- issue node (when root_issue present) ---
    root_issue_num = train.get("root_issue")
    issue_node_id = None
    if root_issue_num is not None:
        issue = issues_by_num.get(root_issue_num, {})
        issue_title = issue.get("title") or f"Issue #{root_issue_num}"
        issue_node_id = _node_id("iss", str(root_issue_num))
        lines.append(f'    {issue_node_id}["{_flow_label(issue_title)}"]')

    # --- PR nodes ---
    pr_node_ids = []
    for pr_num in train_prs:
        pr = prs_by_num.get(pr_num, {})
        pr_title = pr.get("title") or f"PR #{pr_num}"
        pr_id = _node_id("pr", str(pr_num))
        pr_node_ids.append(pr_id)
        lines.append(f'    {pr_id}["{_flow_label(pr_title)}"]')

    # --- outcome node ---
    if outcome == "shipped":
        # best-effort: take first non-empty PR milestone
        milestone = None
        for pr_num in train_prs:
            ms = (prs_by_num.get(pr_num) or {}).get("milestone")
            if ms:
                milestone = ms
                break
        if milestone:
            # The outer _flow_label on outcome_label (below) sanitises/truncates the
            # whole string, so don't pre-truncate the milestone here (double-capping
            # would silently shrink the visible name).
            outcome_label = f"Shipped → {milestone}"
        else:
            outcome_label = "Shipped"
    elif outcome == "rejected":
        outcome_label = "Rejected"
    else:
        outcome_label = "In flight"

    outcome_node_id = _node_id("out", train.get("id", "train"))
    lines.append(f'    {outcome_node_id}(["{_flow_label(outcome_label)}"])')

    # --- edges: issue -> PRs ---
    if issue_node_id:
        for pr_id in pr_node_ids:
            lines.append(f"    {issue_node_id} --> {pr_id}")
    # If PR-only train and no issue node, PRs are the start nodes (no extra edge needed)

    # --- edges: PRs -> outcome ---
    for pr_id in pr_node_ids:
        lines.append(f"    {pr_id} --> {outcome_node_id}")

    # --- mode C: area annotation nodes and edges ---
    if mode_c and distinct_areas:
        area_nodes = {}
        for area in distinct_areas:
            aid = _node_id("area", area)
            area_nodes[aid] = _area_tail(area)
        for aid, label in sorted(area_nodes.items()):
            lines.append(f'    {aid}("{_flow_label(label)}")')
        # code_areas is per-train (not per-PR in the bundle), so every PR links to
        # every train area — the finest granularity the schema supports.
        for pr_id in pr_node_ids:
            for aid in sorted(area_nodes):
                lines.append(f"    {pr_id} --> {aid}")

    return "\n".join(lines) + "\n"


def emit_module_graph(bundle):
    """A Mermaid `flowchart` of resolved area->area dependency edges (blast radius).

    Reads `code_graph.areas[].edges` (Phase 3c). Each resolved edge draws
    source-area --> target-area, labelled with the version when present. Emits a
    `No module dependencies` placeholder when no resolved edges exist. Derived
    solely from existing bundle fields."""
    areas = (bundle.get("code_graph") or {}).get("areas", [])
    lines = ["flowchart LR"]
    nodes = {}
    drawn = []
    for area in areas:
        src_id = _node_id("m", area["id"])
        for edge in area.get("edges", []):
            if not edge.get("resolved") or edge.get("to") is None:
                continue
            dst_id = _node_id("m", edge["to"])
            nodes[src_id] = _area_tail(area["id"])
            nodes[dst_id] = _area_tail(edge["to"])
            if edge.get("local"):
                # private child submodule: mark multi-instance arrays as child[].
                label = "child[]" if edge.get("instances") == "many" else "child"
            else:
                label = edge.get("version") or ("transitive" if edge.get("transitive") else "")
            drawn.append((src_id, dst_id, label))
    if not drawn:
        lines.append("    none[No module dependencies]")
        return "\n".join(lines) + "\n"
    for nid, label in sorted(nodes.items()):
        lines.append(f'    {nid}("{_flow_label(label)}")')
    for src_id, dst_id, label in sorted(set(drawn)):
        if label:
            # quote the edge label so markers with Mermaid-special chars (e.g. the
            # `child[]` multi-instance marker) don't break the flowchart parser.
            lines.append(f'    {src_id} -->|"{_flow_label(label)}"| {dst_id}')
        else:
            lines.append(f"    {src_id} --> {dst_id}")
    return "\n".join(lines) + "\n"


def emit_project_module_graph(module_edges):
    """A Mermaid flowchart of project module dependencies (Slice S4 blast radius).
    Nodes are grouped into a subgraph per member repo; each edge draws
    src-area --> dst-area labelled with its version (or 'transitive'). Cross-repo
    edges are the point of the diagram; intra-repo edges are included for context.

    A PROJECT-level emitter: the digest report flow calls it with
    `digest.project_depends_on(...)` rows (NOT the single-repo `render()` dict,
    which is bundle-scoped). Pure; deterministic — it sorts repos, nodes, and
    edges internally, so input order does not matter."""
    if not module_edges:
        return "flowchart LR\n    none[No cross-repo module dependencies]\n"
    by_repo = {}
    drawn = []
    for e in module_edges:
        src = _node_id("m", "{}::{}".format(e["src_repo"], e["src_area"]))
        dst = _node_id("m", "{}::{}".format(e["dst_repo"], e["dst_area"]))
        # Label with the full area path (not _area_tail): two modules in one repo
        # can both end in `main.tf`; the path disambiguates them in the subgraph.
        by_repo.setdefault(e["src_repo"], {})[src] = e["src_area"]
        by_repo.setdefault(e["dst_repo"], {})[dst] = e["dst_area"]
        # cross_repo intentionally unused here: all edges are drawn uniformly
        # (visual styling of cross- vs intra-repo edges is future work).
        label = e.get("version") or ("transitive" if e.get("transitive") else "")
        drawn.append((src, dst, label))
    lines = ["flowchart LR"]
    for repo in sorted(by_repo):
        # Use full repo name in the subgraph label (only escape quotes; do NOT
        # truncate — long repo names like terraform-azurerm-avm-res-keyvault-vault
        # would be silently clipped by _flow_label's 40-char cap).
        repo_label = repo.replace('"', "'")
        lines.append('    subgraph {}["{}"]'.format(_node_id("r", repo),
                                                     repo_label))
        for nid, area in sorted(by_repo[repo].items()):
            lines.append('        {}("{}")'.format(nid, _flow_label(area)))
        lines.append("    end")
    for src, dst, label in sorted(set(drawn)):
        if label:
            lines.append('    {} -->|"{}"| {}'.format(src, _flow_label(label), dst))
        else:
            lines.append("    {} --> {}".format(src, dst))
    return "\n".join(lines) + "\n"


def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
        "content_timeline": emit_content_timeline(bundle),
        "deltas_bar": emit_deltas_bar(bundle),
        "contributor_graph": emit_contributor_graph(bundle),
        "kind_breakdown": emit_kind_breakdown(bundle),
        "module_graph": emit_module_graph(bundle),
    }


def write_diagrams(bundle, outdir="workspace/diagrams", train_id=None):
    """Write each diagram to <outdir>/<name>.mmd.

    Records a **workspace-relative** manifest on `bundle["diagrams"]` (e.g.
    `diagrams/<name>.mmd` — paths relative to the parent of `outdir`) so the
    persisted bundle is portable for downstream embedding, per the design spec.
    RETURNS the real on-disk paths (name -> path as written) so the caller can
    hand them straight to `validate_with_mmdc`.

    When `train_id` is given, renders ONLY that one train's flowchart (spotlight
    mode); the flat single-instance diagrams are skipped. Otherwise, all flat
    diagrams are written plus train flowcharts for every deep-tier train.
    `bundle["diagrams"]["train_flowcharts"]` is always a nested map
    {<train-id>: <workspace-relative-path>}."""
    os.makedirs(outdir, exist_ok=True)
    root = os.path.dirname(os.path.normpath(outdir)) or "."
    real_paths = {}

    if train_id is not None:
        # Spotlight: render exactly one train (any tier)
        trains_by_id = {t["id"]: t for t in bundle.get("trains", [])}
        if train_id not in trains_by_id:
            raise KeyError(f"train_id {train_id!r} not found in bundle")
        train = trains_by_id[train_id]
        text = emit_train_flowchart(bundle, train)
        fname = f"{train_id}.mmd"
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        real_paths[train_id] = path
        rel = os.path.relpath(path, root)
        # Merge into the bundle diagrams (preserve existing flat keys if any)
        diags = bundle.setdefault("diagrams", {})
        tf = diags.setdefault("train_flowcharts", {})
        tf[train_id] = rel
        return real_paths

    # Default: write flat single-instance diagrams
    relative = {}
    for name, text in render(bundle).items():
        path = os.path.join(outdir, f"{name}.mmd")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        real_paths[name] = path
        relative[name] = os.path.relpath(path, root)

    # Write train flowcharts for every deep train
    train_rel_map = {}
    for train in bundle.get("trains", []):
        if train.get("tier") != "deep":
            continue
        tid = train["id"]
        text = emit_train_flowchart(bundle, train)
        fname = f"{tid}.mmd"
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        real_paths[tid] = path
        train_rel_map[tid] = os.path.relpath(path, root)

    relative["train_flowcharts"] = train_rel_map
    bundle["diagrams"] = relative
    return real_paths


def ensure_mmdc(which=shutil.which):
    """Return the mmdc path or fail fast with install guidance."""
    path = which("mmdc")
    if not path:
        sys.stderr.write("error: `mmdc` not found on PATH. " + INSTALL_HINT + "\n")
        raise SystemExit(3)
    return path


def validate_with_mmdc(paths, export=None, runner=subprocess.run, which=shutil.which):
    """Compile each .mmd with mmdc; raise RuntimeError on the first that fails to
    render. When `export` is 'svg'/'png' the image is written beside the .mmd;
    when `export` is None the diagram is compiled to a throwaway temp file purely
    to validate, so a validation-only run never overwrites or deletes a user's
    own exported images next to the `.mmd`."""
    mmdc = ensure_mmdc(which)
    for path in paths:
        if export in ("svg", "png"):
            out = os.path.splitext(path)[0] + "." + export
            result = runner([mmdc, "-i", path, "-o", out, "-q"],
                            capture_output=True, text=True)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "validate.svg")
                result = runner([mmdc, "-i", path, "-o", out, "-q"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"mmdc failed to render {path}:\n{result.stderr}")


def parse_args(argv):
    p = argparse.ArgumentParser(description="Render + validate activity-overview diagrams.")
    p.add_argument("bundle", help="Path to the enriched bundle JSON.")
    p.add_argument("--diagrams-dir", default="workspace/diagrams")
    p.add_argument("--export", choices=["svg", "png"], default=None,
                   help="Also export images beside each .mmd.")
    p.add_argument("--skip-validate", action="store_true",
                   help="Skip the mmdc compile check (not recommended).")
    p.add_argument("--train", default=None, metavar="TRAIN_ID",
                   help="Spotlight: render only this one train's flowchart (any tier).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    with open(args.bundle) as fh:
        bundle = json.load(fh)
    manifest = write_diagrams(bundle, args.diagrams_dir, train_id=args.train)
    if not args.skip_validate:
        try:
            validate_with_mmdc(list(manifest.values()), export=args.export)
        except RuntimeError as exc:
            sys.stderr.write(f"error: {exc}\n")
            raise SystemExit(1)
    tmp = args.bundle + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    os.replace(tmp, args.bundle)
    sys.stderr.write(
        f"rendered {len(manifest)} diagrams into {args.diagrams_dir} "
        f"({'validated' if not args.skip_validate else 'unvalidated'})\n")
    return manifest


if __name__ == "__main__":
    main()
