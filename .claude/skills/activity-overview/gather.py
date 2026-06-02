"""Acquire layer for the activity-overview skill.

The only component that touches the network. Produces a schema-complete bundle;
later-phase fields are reserved empty here and filled by later phases.
"""

SCHEMA_VERSION = 1


def build_bundle(meta, commits, prs, issues):
    """Assemble the on-disk bundle skeleton.

    Phase 1 fills meta/commits/prs/issues; every other top-level field is
    reserved with an empty value so the schema is stable across phases.
    """
    meta = dict(meta)
    meta.setdefault("schema_version", SCHEMA_VERSION)
    return {
        "meta": meta,
        "commits": commits,
        "prs": prs,
        "issues": issues,
        # --- reserved for later phases (empty, schema-stable) ---
        "timeline": [],
        "artifacts": {},
        "feature_deltas": [],
        "trains": [],
        "buckets": {"shipped": [], "in_flight": [], "rejected": [], "next_candidates": []},
        "people": {},
        "halls": {},
        "flow": {},
        "blockers": [],
        "code_owners": {},
        "code_graph": {},
        "label_taxonomy": {},
        "modules": {},
        "workflow_stats": {},
        "workflows": [],
        "releases": [],
        "milestones": [],
        "docsRefs": [],
        "release_train": {},
        "sprints": {},
        "project": {},
        "diagrams": {},
    }


RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


def parse_git_log(raw):
    """Parse `git log` output formatted with RECORD_SEP/FIELD_SEP separators.

    Each record: <sha>\x1f<parents>\x1f<author>\x1f<date>\x1f<subject> followed by
    newline-separated file paths. Returns a list of commit dicts.
    """
    commits = []
    for chunk in raw.split(RECORD_SEP):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split(FIELD_SEP)
        if len(fields) < 5:
            continue
        sha, parents, author, date, subject = fields[:5]
        files = [ln for ln in lines[1:] if ln.strip()]
        commits.append({
            "sha": sha,
            "parents": parents.split() if parents.strip() else [],
            "author": author,
            "date": date,
            "message": subject,
            "files": files,
            "pr": None,  # resolved in link.py
        })
    return commits
