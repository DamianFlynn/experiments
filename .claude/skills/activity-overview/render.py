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


def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
        "content_timeline": emit_content_timeline(bundle),
        "deltas_bar": emit_deltas_bar(bundle),
        "contributor_graph": emit_contributor_graph(bundle),
        "kind_breakdown": emit_kind_breakdown(bundle),
    }


def write_diagrams(bundle, outdir="workspace/diagrams"):
    """Write each diagram to <outdir>/<name>.mmd.

    Records a **workspace-relative** manifest on `bundle["diagrams"]` (e.g.
    `diagrams/<name>.mmd` — paths relative to the parent of `outdir`) so the
    persisted bundle is portable for downstream embedding, per the design spec.
    RETURNS the real on-disk paths (name -> path as written) so the caller can
    hand them straight to `validate_with_mmdc`."""
    os.makedirs(outdir, exist_ok=True)
    root = os.path.dirname(os.path.normpath(outdir)) or "."
    real_paths = {}
    relative = {}
    for name, text in render(bundle).items():
        path = os.path.join(outdir, f"{name}.mmd")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        real_paths[name] = path
        relative[name] = os.path.relpath(path, root)
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
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    with open(args.bundle) as fh:
        bundle = json.load(fh)
    manifest = write_diagrams(bundle, args.diagrams_dir)
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
