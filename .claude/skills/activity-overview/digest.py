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
    """Materialize + enrich one bundle per member repo, in `repos` order. Each is
    the byte-identical single-repo digest of that member (extract -> link.enrich).
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
