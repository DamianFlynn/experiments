"""Offline diagram render: enriched bundle -> Mermaid .mmd files, validated by mmdc.

Pure emitters build the diagram text from existing bundle fields; `mmdc` (mermaid-cli)
compiles every file so a diagram that would not render fails the run. No network."""

import argparse
import json
import os
import shutil
import subprocess
import sys

INSTALL_HINT = (
    "Install mermaid-cli so `mmdc` is on PATH: `npm install -g @mermaid-js/mermaid-cli`."
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
    newlines. Collapse them and trim."""
    clean = (text or "").replace(":", " -").replace("\n", " ").strip()
    return clean[:60] or "item"


def emit_timeline_gantt(bundle):
    """A Mermaid `gantt` of PR lifespans + releases across the window."""
    meta = bundle.get("meta", {})
    frm, to = meta.get("from", ""), meta.get("to", "")
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
            label = _gantt_label(f"#{p['number']} {p.get('title', '')}")
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


def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return {
        "buckets_pie": emit_buckets_pie(bundle),
        "timeline_gantt": emit_timeline_gantt(bundle),
    }


def write_diagrams(bundle, outdir="workspace/diagrams"):
    """Write each diagram to <outdir>/<name>.mmd and record the manifest on the
    bundle. Returns the name->path manifest."""
    os.makedirs(outdir, exist_ok=True)
    manifest = {}
    for name, text in render(bundle).items():
        path = os.path.join(outdir, f"{name}.mmd")
        with open(path, "w") as fh:
            fh.write(text)
        manifest[name] = path
    bundle["diagrams"] = manifest
    return manifest
