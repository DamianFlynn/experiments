"""validate.py — the graph-trustworthiness audit (Phase 7 trust gate).

A CRITICAL CI gate. The activity report narrates who/what/when/why entirely
from the journey-graph store, so if the graph is wrong the report lies
confidently. This harness PROVES a populated store is trustworthy: it re-checks
the invariants the write path (`gather.fold_bundle`) is supposed to uphold and
fails loudly — per invariant, per entity — when one is broken.

Read-only over the store: `validate` never mutates it. stdlib only.

API
    validate(conn, project=None, repo=None, bundle=None) -> Report
        Report.ok      : bool  (False iff any ERROR-severity check failed)
        Report.checks  : [{name, ok, severity, details:[...]}]

CLI
    python3 validate.py STORE.db [--project P --repo R] [--bundle bundle.json]
    Prints a readable report; exits non-zero iff any ERROR-severity check fails
    (so CI can gate on it). WARN/INFO never fail the gate.

The invariants (each a named check; see the per-function docstrings):
    referential_integrity   ERROR  every non-spine edge endpoint resolves to a
                                    stored node (dangling spine edge -> INFO).
    participant_completeness ERROR  every login in any raw record has a person
                                    node + its contribution edge ("carol" class).
    provenance              ERROR  every social/code node cites a source ref.
    no_fabrication          ERROR  no orphan person, every artifact has history,
                                    no empty/non-round-tripping id.
    schema_conformance      ERROR  each edge type's endpoints match STORE.md.
    no_drift                ERROR  stored people/artifacts == freshly re-derived
                                    from the bundle (only with --bundle).
    idempotency             ERROR  re-folding the bundle changes no counts
                                    (only with --bundle).
"""

import argparse
import copy
import json
import sqlite3
import sys

import graphstore
from graphstore import SPINE_EDGE_TYPES
import derive

try:
    import gather
except Exception:  # pragma: no cover - gather is always importable in practice
    gather = None


ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"


class Report:
    """The structured trust report. `ok` is False iff any ERROR check failed."""

    def __init__(self, checks):
        self.checks = checks
        self.ok = all(c["ok"] for c in checks if c["severity"] == ERROR)

    def to_dict(self):
        return {"ok": self.ok, "checks": self.checks}


def _check(name, severity, details):
    """A check result. `ok` is True when no detail carries an ERROR.

    `details` is a list of dicts; an entry with `severity == ERROR` marks the
    check failed. INFO/WARN entries are informational and never fail the gate.
    """
    failed = any(d.get("severity", severity) == ERROR for d in details)
    return {"name": name, "ok": not failed, "severity": severity,
            "details": details}


# --- store introspection helpers ---------------------------------------------

def _all_node_ids(conn):
    return {r[0] for r in conn.execute("SELECT id FROM nodes")}


def _all_nodes(conn):
    return [graphstore._row_to_node(r) for r in conn.execute("SELECT * FROM nodes")]


def _all_edges(conn):
    return [graphstore._row_to_edge(r) for r in conn.execute("SELECT * FROM edges")]


def _detect_project_repo(conn, project, repo):
    """Derive (project, repo) from the store when not passed.

    project: the single non-sentinel project in `nodes` (people use repo "*").
    repo:    the single non-sentinel repo for that project. Works over a real
    gathered store; raises if the store holds >1 project/repo and the caller
    did not disambiguate.
    """
    if project is None:
        projects = sorted({
            r[0] for r in conn.execute(
                "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL")
        })
        if len(projects) == 1:
            project = projects[0]
        elif not projects:
            raise ValueError("empty store: no project to validate")
        else:
            raise ValueError(
                "store holds multiple projects {} — pass --project".format(projects))
    if repo is None:
        repos = sorted({
            r[0] for r in conn.execute(
                "SELECT DISTINCT repo FROM nodes WHERE project=? AND repo != '*'",
                (project,))
        })
        if len(repos) == 1:
            repo = repos[0]
        elif not repos:
            raise ValueError("project {} has no repo to validate".format(project))
        else:
            raise ValueError(
                "project {} holds multiple repos {} — pass --repo".format(project, repos))
    return project, repo


def _local(qid):
    return graphstore.parse_id(qid)["local"]


def _is_person_id(qid):
    return "#person-" in qid


# --- check 1: referential integrity ------------------------------------------

def check_referential_integrity(conn):
    """Every edge's src_id and dst_id resolves to a stored node.

    EXCEPT spine edges (SPINE_EDGE_TYPES) may legitimately point at a
    not-yet-gathered node — that is the backfill case (a windowed PR closing an
    out-of-window issue). Those are reported as INFO ("missing thread"), not
    ERROR. The `owns` (person->area) edge's person endpoint may be a CODEOWNER
    who never contributed (no person node) — that is a documented clean-store
    reality, reported INFO. A dangling NON-spine edge (e.g. an `authored` whose
    person or target is absent) is an ERROR. Counts are reported per edge type.
    """
    ids = _all_node_ids(conn)
    details = []
    per_type = {}
    for e in _all_edges(conn):
        et = e["edge_type"]
        per_type.setdefault(et, {"edges": 0, "dangling": 0})
        per_type[et]["edges"] += 1
        for side in ("src_id", "dst_id"):
            target = e[side]
            if target in ids:
                continue
            spine = et in SPINE_EDGE_TYPES
            # owns' person src may be a CODEOWNER who never contributed.
            owns_codeowner = (et == "owns" and side == "src_id"
                              and _is_person_id(target))
            sev = INFO if (spine or owns_codeowner) else ERROR
            per_type[et]["dangling"] += 1
            details.append({
                "severity": sev,
                "edge_type": et,
                "side": side,
                "src": e["src_id"], "dst": e["dst_id"],
                "missing": target,
                "note": ("missing thread (backfill)" if spine else
                         "codeowner not a contributor" if owns_codeowner else
                         "dangling non-spine edge"),
            })
    # summary line (INFO) so the report always shows per-type coverage
    details.append({"severity": INFO, "summary": "per_type_counts",
                    "counts": per_type})
    return _check("referential_integrity", ERROR, details)


# --- check 2: participant completeness (the carol class) ---------------------

# Which raw fields contribute which login under which edge type. dst is the
# local id of the target node the contribution edge points at.
def _expected_contributions(bundle):
    """Enumerate (login, edge_type, dst_local, source) the write path must have
    written, straight from the RAW records — pr.author/merged_by/reviewers,
    issue.author, commit.author, and comment/review-comment authors."""
    out = []
    for pr in bundle.get("prs", []):
        num = pr.get("number")
        if num is None:
            continue  # malformed record (caught by provenance, not here)
        dst = "pr-{}".format(num)
        src = "pr #{}".format(num)
        if pr.get("author"):
            out.append((pr["author"], "authored", dst, src + " author"))
        if pr.get("merged_by"):
            out.append((pr["merged_by"], "merged", dst, src + " merged_by"))
        for rv in pr.get("reviewers") or []:
            if rv:
                out.append((rv, "reviewed", dst, src + " reviewer"))
        for c in (pr.get("comments_list") or []) + (pr.get("review_comments") or []):
            if c.get("author"):
                out.append((c["author"], "commented", dst, src + " comment"))
    for iss in bundle.get("issues", []):
        num = iss.get("number")
        if num is None:
            continue
        dst = "issue-{}".format(num)
        src = "issue #{}".format(num)
        if iss.get("author"):
            out.append((iss["author"], "reported", dst, src + " author"))
        for c in iss.get("comments_list") or []:
            if c.get("author"):
                out.append((c["author"], "commented", dst, src + " comment"))
    for c in bundle.get("commits", []):
        sha = c.get("sha")
        if c.get("author") and sha:
            out.append((c["author"], "authored", sha,
                        "commit {}".format(sha[:8])))
    return out


def _edge_set(conn):
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT src_id, dst_id, edge_type FROM edges")}


def check_participant_completeness(conn, project, repo, bundle):
    """For every login in any raw record assert its person node EXISTS and its
    contribution edge exists. Missing person or missing edge = ERROR, naming the
    login and the record.

    This is the gate's reason to exist — the `carol` bug: a reviewer of a PR
    whose commits only message-resolve to it was silently dropped. The write
    path is fixed, so a reviewer of a message-resolved PR MUST be present.

    Requires the raw bundle to know the expected participants; when no bundle is
    passed the check degrades to an INFO note (it cannot enumerate raw records
    from the store alone, since comments/reviews live embedded in node data).
    """
    if bundle is None:
        # Fall back to scanning the embedded raw records in stored node data.
        bundle = _reconstruct_raw_from_store(conn, project, repo)
    ids = _all_node_ids(conn)
    edges = _edge_set(conn)
    details = []
    for login, etype, dst_local, source in _expected_contributions(bundle):
        person = graphstore.qualify_person(project, login)
        dst = graphstore.qualify_id(project, repo, dst_local)
        if person not in ids:
            details.append({
                "severity": ERROR, "login": login, "source": source,
                "missing": "person node {}".format(person),
                "edge_type": etype})
            continue
        if (person, dst, etype) not in edges:
            details.append({
                "severity": ERROR, "login": login, "source": source,
                "missing": "{} edge {} -> {}".format(etype, person, dst),
                "edge_type": etype})
    if not details:
        details.append({"severity": INFO, "note": "all participants present"})
    return _check("participant_completeness", ERROR, details)


def _reconstruct_raw_from_store(conn, project, repo):
    """Best-effort raw-bundle reconstruction from stored node `data` blobs, so
    participant-completeness can run WITHOUT an external bundle (e.g. live CI
    over a real store). PRs/issues keep their comments embedded in `data`."""
    prs, issues, commits = [], [], []
    for n in graphstore.repo_nodes(conn, project, repo):
        local = _local(n["id"])
        if n["node_class"] == "social" and local.startswith("pr-"):
            prs.append(n["data"])
        elif n["node_class"] == "social" and local.startswith("issue-"):
            issues.append(n["data"])
        elif n["node_class"] == "code" and not local.startswith("art:") \
                and "#" not in local and "sha" in (n["data"] or {}):
            # bare-sha commit node (local id is the sha; artifacts have art:/#)
            commits.append(n["data"])
    return {"prs": prs, "issues": issues, "commits": commits}


# --- check 3: provenance / no unsourced facts --------------------------------

def _has_source_ref(node):
    """A node carries a citable source: a url and/or number/sha (directly, or —
    for artifacts — via a lifecycle event's commit ref)."""
    data = node.get("data") or {}
    if not isinstance(data, dict):
        return False
    for key in ("url", "html_url", "number", "sha"):
        if data.get(key):
            return True
    # artifacts cite their commit history via lifecycle refs
    for lc in data.get("lifecycle") or []:
        if isinstance(lc, dict) and (lc.get("commit") or
                                     (lc.get("ref") or {}).get("url")):
            return True
    return False


def check_provenance(conn):
    """Every social/code node's `data` carries a source identifier (a url and/or
    number/sha — artifacts may cite via a lifecycle commit ref). A node lacking
    any source ref = ERROR: the report would cite nothing for it.

    Structure singletons (people, areas, milestones, code-graph/owners/label
    singletons) are exempt — the schema gives them no url, they are not
    narrated as sourced facts.
    """
    details = []
    for n in _all_nodes(conn):
        if n["node_class"] == "structure":
            continue
        if not _has_source_ref(n):
            details.append({
                "severity": ERROR, "id": n["id"],
                "node_class": n["node_class"],
                "missing": "no url/number/sha/lifecycle ref in data"})
    if not details:
        details.append({"severity": INFO, "note": "all social/code nodes sourced"})
    return _check("provenance", ERROR, details)


# --- check 4: no fabrication / no orphans ------------------------------------

_CONTRIB_EDGE_TYPES = ("authored", "merged", "reviewed", "reported",
                       "commented", "reacted")


def check_no_fabrication(conn):
    """No invented entities, no historyless artifacts, no malformed ids.

    - Every person node is referenced by >=1 contribution edge (a person with
      NO contribution edge is a contributor we invented = ERROR). `owns` alone
      does NOT count — a CODEOWNER who never contributed has no person node (the
      write path skips it), so any EXISTING person node must have earned it.
    - Every artifact `code` node (local id `art:<path>` / `<path>#…`) has >=1
      lifecycle event (artifact with no history = ERROR).
    - No node has an empty/None id; ids round-trip through parse_id.
    """
    details = []
    # contribution edges by person src
    contrib_src = set()
    for r in conn.execute(
            "SELECT src_id FROM edges WHERE edge_type IN ({})".format(
                ",".join("?" for _ in _CONTRIB_EDGE_TYPES)),
            _CONTRIB_EDGE_TYPES):
        contrib_src.add(r[0])

    for n in _all_nodes(conn):
        nid = n["id"]
        # id well-formedness
        if not nid:
            details.append({"severity": ERROR, "id": repr(nid),
                            "problem": "empty/None node id"})
            continue
        parsed = graphstore.parse_id(nid)
        if not parsed["local"]:
            details.append({"severity": ERROR, "id": nid,
                            "problem": "id does not round-trip through parse_id "
                                       "(empty local)"})
        # orphan person
        if _is_person_id(nid):
            if nid not in contrib_src:
                login = parsed["local"].replace("person-", "", 1)
                details.append({
                    "severity": ERROR, "id": nid, "login": login,
                    "problem": "orphan person: no contribution edge "
                               "(invented contributor)"})
        # artifact with no history
        elif n["node_class"] == "code":
            local = parsed["local"]
            is_artifact = local.startswith("art:") or "#" in local
            if is_artifact:
                lifecycle = (n["data"] or {}).get("lifecycle") or []
                if not lifecycle:
                    details.append({
                        "severity": ERROR, "id": nid,
                        "problem": "artifact code node with no lifecycle history"})
    if not details:
        details.append({"severity": INFO, "note": "no orphans/fabrications"})
    return _check("no_fabrication", ERROR, details)


# --- check 5: schema conformance of edges ------------------------------------

def _id_kind(conn, qid, node_cache):
    """Classify a node id by its stored node_class + local-id form, for edge
    endpoint conformance. Returns one of: person, area, milestone, pr, issue,
    commit, artifact, social, code, structure, or '<missing>' when not stored.
    """
    local = _local(qid)
    if _is_person_id(qid):
        return "person"
    if local.startswith("area-"):
        return "area"
    if local.startswith("milestone-"):
        return "milestone"
    if local.startswith("release-"):
        return "release"
    if local.startswith("pr-"):
        return "pr"
    if local.startswith("issue-"):
        return "issue"
    if local.startswith("art:") or "#" in local:
        return "artifact"
    node = node_cache.get(qid)
    if node is None and qid not in node_cache:
        node = graphstore.get_node(conn, qid)
        node_cache[qid] = node
    if node is None:
        return "<missing>"
    if node["node_class"] == "code":
        return "commit"
    return node["node_class"]


# (edge_type) -> (allowed src kinds, allowed dst kinds). A missing endpoint is
# not a schema violation here (referential_integrity owns that); we only flag a
# STORED endpoint whose kind contradicts STORE.md.
_EDGE_SCHEMA = {
    "authored":     ({"person"}, {"pr", "commit"}),
    "merged":       ({"person"}, {"pr"}),
    "reviewed":     ({"person"}, {"pr"}),
    "reported":     ({"person"}, {"issue"}),
    "commented":    ({"person"}, {"pr", "issue"}),
    "reacted":      ({"person"}, {"pr", "issue", "commit"}),
    "owns":         ({"person"}, {"area"}),
    "touches":      ({"commit", "pr"}, {"area"}),
    "depends_on":   ({"area"}, {"area"}),
    "in_milestone": ({"pr", "issue"}, {"milestone"}),
    "in_iteration": ({"pr", "issue"}, {"sprint"}),
    "closes":       ({"pr"}, {"issue"}),
    "part_of":      ({"commit"}, {"pr"}),
    "blocks":       ({"issue"}, {"issue"}),
    "replaced_by":  ({"artifact"}, {"artifact"}),
    "identity_from": ({"artifact"}, {"artifact"}),
}


def check_schema_conformance(conn):
    """Each edge type's endpoints match STORE.md semantics: `authored` src is a
    person and dst a pr/commit; `touches` dst is an area; `owns` person->area;
    `in_milestone` dst is a milestone; etc. A STORED endpoint whose kind
    contradicts the schema = ERROR (a missing endpoint is referential_integrity's
    job, not flagged here). `cross_ref` / `spun_off` / `duplicate_of` are
    polymorphic (issue<->pr<->commit) and are not endpoint-constrained.
    """
    cache = {}
    details = []
    for e in _all_edges(conn):
        et = e["edge_type"]
        rule = _EDGE_SCHEMA.get(et)
        if rule is None:
            continue  # polymorphic / unconstrained spine edge
        src_ok, dst_ok = rule
        sk = _id_kind(conn, e["src_id"], cache)
        dk = _id_kind(conn, e["dst_id"], cache)
        if sk != "<missing>" and sk not in src_ok:
            details.append({
                "severity": ERROR, "edge_type": et, "endpoint": "src",
                "id": e["src_id"], "kind": sk, "expected": sorted(src_ok)})
        if dk != "<missing>" and dk not in dst_ok:
            details.append({
                "severity": ERROR, "edge_type": et, "endpoint": "dst",
                "id": e["dst_id"], "kind": dk, "expected": sorted(dst_ok)})
    if not details:
        details.append({"severity": INFO, "note": "all edge endpoints conform"})
    return _check("schema_conformance", ERROR, details)


# --- check 6: store-derived == link-derived (no drift) -----------------------

def _derive_people(bundle):
    """Re-derive the people projection exactly as the write path does, via the
    SAME shared enumerator (derive.enumerate_participants) — so writer and
    auditor can never diverge. Run attach_commit_prs on a COPY first (so
    reviewers of message-resolved PRs are attributed), then enumerate the FULL
    participant set (every login with a contribution edge, bot-tagged)."""
    area_idx = derive.area_index(bundle.get("code_graph", {}) or {})
    resolved = copy.deepcopy(bundle.get("commits") or [])
    gather.attach_commit_prs(resolved)
    view = {**bundle, "commits": resolved, "people": dict(bundle.get("people") or {})}
    return derive.enumerate_participants(view, area_idx)


def _derive_artifacts(bundle):
    """Re-derive the artifact projection exactly as the write path does:
    build_artifacts then link_symbol_identity (on a copy carrying them)."""
    artifacts = derive.build_artifacts(bundle)
    derive.link_symbol_identity({**bundle, "artifacts": artifacts})
    return artifacts


def check_no_drift(conn, project, repo, bundle):
    """Store-derived == link-derived (the carol-check generalized to ANY data).

    Re-derive people and artifacts from the bundle (running attach_commit_prs
    first, exactly as the write path does) and assert the STORED person/artifact
    nodes equal the freshly derived set — same ids, same key fields. Any
    difference = ERROR with the diff. This is the strongest real-data guarantee:
    it catches a stored projection that has silently drifted from what the data
    says it should be.
    """
    details = []

    # --- people ---
    want_people = _derive_people(bundle)
    stored_people = {}
    for n in graphstore.repo_nodes(conn, project, "*", "structure"):
        local = _local(n["id"])
        if local.startswith("person-"):
            login = local[len("person-"):]
            d = n["data"] or {}
            stored_people[login] = {
                "modules": sorted(d.get("modules") or []),
                "areas": sorted(d.get("areas") or []),
                "is_bot": bool(d.get("is_bot")),
            }
    want_norm = {
        login: {"modules": sorted(rec.get("modules") or []),
                "areas": sorted(rec.get("areas") or []),
                "is_bot": bool(rec.get("is_bot"))}
        for login, rec in want_people.items()
    }
    for login in sorted(set(stored_people) | set(want_norm)):
        if login not in stored_people:
            details.append({"severity": ERROR, "kind": "person", "login": login,
                            "problem": "derived but not stored", "expected": want_norm[login]})
        elif login not in want_norm:
            details.append({"severity": ERROR, "kind": "person", "login": login,
                            "problem": "stored but not derivable", "stored": stored_people[login]})
        elif stored_people[login] != want_norm[login]:
            details.append({"severity": ERROR, "kind": "person", "login": login,
                            "problem": "person drift",
                            "stored": stored_people[login],
                            "expected": want_norm[login]})

    # --- artifacts ---
    # The stored artifact id is `{project}/{repo}#{local}` where the SYMBOL-artifact
    # local is itself a double-`#` form `{path}#{lang}:{subkind}:{name}`. `_local`
    # (parse_id) rpartitions on the LAST `#`, which truncates a symbol id to
    # `{lang}:{subkind}:{name}` — so it would never match the derived `{path}#…`
    # key and 86 real artifacts were FALSELY reported "derived but not stored". Strip
    # ONLY the repo prefix to recover the true local id (mirroring how fold_bundle /
    # extract reconstruct it).
    want_arts = _derive_artifacts(bundle)
    stored_arts = {}
    repo_prefix = "{}/{}#".format(project, repo)
    for n in graphstore.repo_nodes(conn, project, repo, "code"):
        nid = n["id"]
        local = nid[len(repo_prefix):] if nid.startswith(repo_prefix) else _local(nid)
        if local.startswith("art:") or "#" in local:
            stored_arts[local] = n["data"] or {}
    art_fields = ("kind", "path", "name", "status", "replaced_by", "code_area")
    for aid in sorted(set(stored_arts) | set(want_arts)):
        if aid not in stored_arts:
            details.append({"severity": ERROR, "kind": "artifact", "id": aid,
                            "problem": "derived but not stored"})
        elif aid not in want_arts:
            details.append({"severity": ERROR, "kind": "artifact", "id": aid,
                            "problem": "stored but not derivable"})
        else:
            s, w = stored_arts[aid], want_arts[aid]
            for f in art_fields:
                if s.get(f) != w.get(f):
                    details.append({
                        "severity": ERROR, "kind": "artifact", "id": aid,
                        "field": f, "problem": "artifact field drift",
                        "stored": s.get(f), "expected": w.get(f)})
            if len(s.get("lifecycle") or []) != len(w.get("lifecycle") or []):
                details.append({
                    "severity": ERROR, "kind": "artifact", "id": aid,
                    "field": "lifecycle", "problem": "lifecycle length drift",
                    "stored": len(s.get("lifecycle") or []),
                    "expected": len(w.get("lifecycle") or [])})

    if not details:
        details.append({"severity": INFO, "note": "store matches re-derived people+artifacts"})
    return _check("no_drift", ERROR, details)


# --- check 7: idempotency probe ----------------------------------------------

def _counts(conn):
    return {
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "code_events": conn.execute("SELECT COUNT(*) FROM code_events").fetchone()[0],
    }


def check_idempotency(conn, bundle):
    """Re-fold the bundle into a SECONDARY in-memory copy of the store and assert
    node/edge/code_event counts are unchanged (durable dedup holds).

    Read-only over the audited store: we copy it into memory, re-fold there, and
    compare counts. A count that grows means a fold path is non-idempotent (it
    would silently inflate the graph on every overlapping window).
    """
    before = _counts(conn)
    copy_conn = graphstore.open_store(":memory:")
    conn.backup(copy_conn)
    gather.fold_bundle(copy_conn, bundle)
    after = _counts(copy_conn)
    copy_conn.close()
    details = []
    for k in ("nodes", "edges", "code_events"):
        if after[k] != before[k]:
            details.append({
                "severity": ERROR, "table": k,
                "problem": "re-fold changed count (non-idempotent)",
                "before": before[k], "after": after[k]})
    if not details:
        details.append({"severity": INFO, "note": "re-fold is a no-op", "counts": before})
    return _check("idempotency", ERROR, details)


# --- top-level driver ---------------------------------------------------------

def validate(conn, project=None, repo=None, bundle=None):
    """Audit a populated store; return a Report.

    project/repo are derived from the store (`nodes`) when not passed, so this
    works over a real gathered store. When `bundle` is provided the two
    real-data checks (no_drift, idempotency) additionally run.
    """
    project, repo = _detect_project_repo(conn, project, repo)
    checks = [
        check_referential_integrity(conn),
        check_participant_completeness(conn, project, repo, bundle),
        check_provenance(conn),
        check_no_fabrication(conn),
        check_schema_conformance(conn),
    ]
    if bundle is not None:
        checks.append(check_no_drift(conn, project, repo, bundle))
        checks.append(check_idempotency(conn, bundle))
    return Report(checks)


# --- CLI ----------------------------------------------------------------------

def _format_report(report, project, repo):
    lines = []
    status = "OK" if report.ok else "FAIL"
    lines.append("trust audit: {}  (project={} repo={})".format(status, project, repo))
    lines.append("=" * 60)
    for c in report.checks:
        mark = "ok  " if c["ok"] else "FAIL"
        lines.append("[{}] {}  ({})".format(mark, c["name"], c["severity"]))
        for d in c["details"]:
            sev = d.get("severity", c["severity"])
            if c["ok"] and sev == INFO:
                # show only a brief note for passing checks
                note = d.get("note") or d.get("summary")
                if note:
                    lines.append("      - {}".format(note))
                continue
            lines.append("      - [{}] {}".format(
                sev, json.dumps({k: v for k, v in d.items()
                                 if k != "severity"}, sort_keys=True)))
    lines.append("=" * 60)
    lines.append("RESULT: {}".format(status))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit a journey-graph store for trustworthiness.")
    parser.add_argument("store", help="path to the STORE.db SQLite store")
    parser.add_argument("--project", default=None,
                        help="project (auto-detected from the store if omitted)")
    parser.add_argument("--repo", default=None,
                        help="repo (auto-detected from the store if omitted)")
    parser.add_argument("--bundle", default=None,
                        help="raw bundle JSON to enable no_drift + idempotency")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    conn = graphstore.open_store(args.store)
    bundle = None
    if args.bundle:
        with open(args.bundle) as fh:
            bundle = json.load(fh)
    report = validate(conn, project=args.project, repo=args.repo, bundle=bundle)
    project, repo = _detect_project_repo(conn, args.project, args.repo)
    conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    else:
        print(_format_report(report, project, repo))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
