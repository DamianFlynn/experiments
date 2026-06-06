#!/usr/bin/env python3
"""Example downstream formatter — a SECOND consumer of the structured digest view.

The activity-overview pipeline's product is the structured, fully-sourced view
(`digest.py` for a multi-repo project, or the enriched single-repo bundle). The
narrative Markdown report is ONE renderer over it; this tiny script is another —
a compact "shipped" changelog grouped by repo — to demonstrate that the view is a
stable, documented contract any formatter can consume (see BUNDLE.md → "Output
contract for downstream formatters"). stdlib only; no network; deterministic.

Usage:
    python3 examples/formatters/shipped_changelog.py samples/digest_view.json
"""
import json
import sys
from collections import defaultdict

# The view must carry `meta` and shipped refs — either a project view's top-level
# `shipped` or a single-repo bundle's `buckets.shipped`. Checked up front so a
# non-view (or a future breaking change) surfaces loudly rather than mis-rendering.
def _is_view(view):
    return isinstance(view, dict) and "meta" in view and (
        "shipped" in view or "shipped" in (view.get("buckets") or {}))


def _shipped(view):
    """Shipped refs from either entry point: a multi-repo project view's top-level
    `shipped`, or a single-repo enriched bundle's `buckets.shipped`."""
    if "shipped" in view:
        return view["shipped"]
    return (view.get("buckets") or {}).get("shipped", [])


def render(view):
    meta = view.get("meta", {})
    title = meta.get("project") or f'{meta.get("owner","")}/{meta.get("repo","")}'.strip("/")
    lines = [f"# {title} — shipped {meta.get('from','?')} → {meta.get('to','?')}", ""]

    by_repo = defaultdict(list)
    for item in _shipped(view):
        # single-repo views may omit `repo`; fall back to meta.
        repo = item.get("repo") or f'{meta.get("owner","")}/{meta.get("repo","")}'.strip("/")
        by_repo[repo].append(item)

    if not by_repo:
        lines.append("_Nothing shipped in this window._")
        return "\n".join(lines) + "\n"

    for repo in sorted(by_repo):
        lines.append(f"## {repo}")
        for item in sorted(by_repo[repo], key=lambda i: (i.get("type", ""), i.get("id", 0))):
            kind = item.get("type", "item")
            num = item.get("id")
            url = item.get("url", "")
            train = f" · train `{item['train']}`" if item.get("train") else ""
            lines.append(f"- {kind} [#{num}]({url}){train}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: shipped_changelog.py DIGEST_VIEW.json\n")
        raise SystemExit(2)
    with open(argv[0], encoding="utf-8") as fh:
        view = json.load(fh)
    if not _is_view(view):
        sys.stderr.write(
            f"error: {argv[0]} is not a digest view (need `meta` + `shipped` or "
            "`buckets.shipped`); see BUNDLE.md for the contract\n")
        raise SystemExit(2)
    sys.stdout.write(render(view))


if __name__ == "__main__":
    main()
