"""view.py — materialize ONE repo's raw bundle view from the journey-graph store.

The CLI face of `extract.extract` (the reader stage). gather writes only the
store; this writes the per-repo **bundle view** file that `link.py` enriches in
place and `render.py` draws from:

    python3 view.py --store DB [--project P] [--repo R] --from F --to T --out bundle.json

Store scoping (how gather keys what it folds):
  * single repo (`gather.py --owner O --repo N`): project = O, repo = N
  * manifest   (`gather.py --manifest M`):       project = manifest.project,
                                                  repo = "owner/name" per member

`--project` / `--repo` are auto-detected when the store holds exactly one
project / the project holds exactly one repo (same rule as `validate.py`).
For a multi-repo project, `--repo` selects ONE member; the merged project view
(cross-repo trains, merged shipped/people/modules, related_work, module_edges)
comes from `digest.py`, not from this tool.

Output: exactly what `extract.extract` returns (the RAW keys only — no trains /
buckets / diagrams; run `link.py` next), as indented JSON. Arrays are already
sorted deterministically by extract. Object keys are written in extract's
order, NOT re-sorted: the order of `artifacts` breaks same-timestamp ties in
`link.build_timeline`, so re-sorting the keys would change the enriched
timeline / feature_deltas. The output is byte-identical run to run.

No network: missing out-of-window spine context is only warned about on stderr
(extract's warn-only mode); nothing is backfilled.

Exit codes: 0 written; 2 usage error, missing/unreadable store, or an
ambiguous / unknown / empty project or repo.

Stdlib only. Python 3.
"""

import argparse
import json
import os
import sqlite3
import sys

import extract
import graphstore
import manifest
import validate


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Materialize one repo's raw bundle view (extract) from a "
                    "journey-graph store, for link.py / render.py.")
    p.add_argument("--store", required=True,
                   help="path to the journey-graph store written by gather.py")
    p.add_argument("--project", default=None,
                   help="project (single repo: the owner; manifest: its "
                        "project name). Auto-detected when the store holds one.")
    p.add_argument("--repo", default=None,
                   help="repo (single repo: the bare name; manifest member: "
                        "'owner/name'). Auto-detected when the project holds one.")
    p.add_argument("--from", dest="ts_from", required=True,
                   help="window start (YYYY-MM-DD, or a full ISO timestamp)")
    p.add_argument("--to", dest="ts_to", required=True,
                   help="window end (YYYY-MM-DD, or a full ISO timestamp)")
    p.add_argument("--out", required=True,
                   help="bundle view JSON to write (parent dirs are created)")
    args = p.parse_args(argv)
    # Shared reader-window rules (manifest.check_reader_window): each bound is a
    # real YYYY-MM-DD date or a full ISO timestamp, validated in FULL (no junk
    # after the date), and from <= to as instants.
    try:
        manifest.check_reader_window(args.ts_from, args.ts_to, "--from", "--to")
    except ValueError as exc:
        p.error("{} (given --from {!r}, --to {!r})".format(
            exc, args.ts_from, args.ts_to))
    return args


def _store_projects(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL"))


def resolve_scope(conn, project, repo):
    """Return (project, repo) for the view, or raise ValueError with a message
    that tells the operator what to pass. Auto-detection reuses
    `validate._detect_project_repo`; explicitly passed values are checked
    against the store so a typo never yields a silently empty view."""
    if project is not None:
        projects = _store_projects(conn)
        if project not in projects:
            raise ValueError(
                "project {!r} is not in the store (projects: {})".format(
                    project, projects))
    try:
        project, detected = validate._detect_project_repo(conn, project, repo)
    except ValueError as exc:
        msg = str(exc)
        if "multiple repos" in msg:
            msg += (" (a bundle view is one member; for the merged project "
                    "view use digest.py)")
        raise ValueError(msg)
    if repo is not None:
        repos = graphstore.project_repos(conn, project)
        if repo not in repos:
            raise ValueError(
                "repo {!r} has no nodes in project {!r} (repos: {})".format(
                    repo, project, repos))
    return project, detected


def write_view(bundle, out):
    """Write the bundle atomically (tmp + replace) as indented JSON, keeping
    extract's key order (see the module docstring for why it is not sorted)."""
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, out)


def _fail(msg):
    sys.stderr.write("view.py: error: {}\n".format(msg))
    return 2


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # sqlite3.connect would silently CREATE a missing file; refuse instead.
    if not os.path.isfile(args.store):
        return _fail("store {!r} does not exist (run gather.py first)".format(
            args.store))
    try:
        conn = graphstore.open_store(args.store)
    except sqlite3.DatabaseError as exc:
        return _fail("cannot read store {!r}: {}".format(args.store, exc))
    try:
        try:
            project, repo = resolve_scope(conn, args.project, args.repo)
        except ValueError as exc:
            return _fail(str(exc))
        bundle = extract.extract(conn, project, repo, args.ts_from, args.ts_to)
    except sqlite3.DatabaseError as exc:
        return _fail("cannot read store {!r}: {}".format(args.store, exc))
    finally:
        conn.close()
    write_view(bundle, args.out)
    sys.stderr.write(
        "view: {} prs, {} issues, {} commits for {}/{} [{} .. {}] -> {}\n".format(
            len(bundle["prs"]), len(bundle["issues"]), len(bundle["commits"]),
            project, repo, args.ts_from, args.ts_to, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
