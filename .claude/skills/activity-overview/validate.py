"""validate.py — the graph-trustworthiness audit (Phase 7 trust gate).

A CRITICAL CI gate. The activity report narrates who/what/when/why entirely
from the journey-graph store, so if the graph is wrong the report lies
confidently. This harness proves a populated store is consistent with its own
records (see "What the gate proves" below): it re-checks the invariants the
write path (`gather.fold_bundle`) is supposed to uphold and fails loudly — per
invariant, per entity — when one is broken.

Read-only over the store: `validate` never mutates it, and the CLI opens the
store with SQLite's read-only URI mode (graphstore.open_store_readonly), so a
missing path is never created. stdlib only.

API
    validate(conn, project=None, repo=None, bundle=None) -> Report
        validate_repo(conn, project, repo, bundle=None) -> Report
        validate_project(conn, project, repos) -> dict
        Report.ok      : bool  (False iff any ERROR-severity check failed)
        Report.checks  : [{name, ok, severity, details:[...]}]

CLI
    python3 validate.py STORE.db [--project P --repo R] [--bundle bundle.json]
    Prints a readable report; exits 1 iff any ERROR-severity check fails (so CI
    can gate on it). WARN/INFO never fail the gate. Exits 2, with no report, on
    a usage error: the store path does not exist or is not a store, the store
    holds several projects (or repos) and none was named, or --bundle is
    unreadable.

Every report lists EVERY check, in a fixed order (issue #79). A check that
cannot run is a FAILED check, never an omitted one: a check that raises (e.g.
on a corrupt row, named by its id) fails with the error, and when edge_set /
no_drift / idempotency cannot self-source a bundle they fail with
`could not self-source: <reason>`. WARN details (e.g. a current-state edge
type a multi-window store cannot prove, with the reason) are listed but never
fail the gate.

The invariants (each a named check; see the per-function docstrings):
    target                  ERROR  the audited (project, repo) has stored nodes
                                    (a mistyped --repo/--project or an empty
                                    store is a failure, not a vacuous pass), and
                                    its last manifest gather of every recorded
                                    window is ok (meta member_status, #77).
    referential_integrity   ERROR  every non-spine edge endpoint resolves to a
                                    stored node (dangling spine edge -> INFO).
    participant_completeness ERROR  every login in any raw record has a person
                                    node + its contribution edge ("carol" class).
    provenance              ERROR  every social/code node cites a source that is
                                    its own: PR/issue/review/event urls on
                                    github.com/{owner}/{repo} with the node's
                                    number, commit ids 40/64-hex shas (#89).
    no_fabrication          ERROR  no orphan person, every artifact has history,
                                    ids follow the grammar and their row's
                                    project/repo, no case-variant scopes (#89).
    schema_conformance      ERROR  every edge type has an endpoint rule (unknown
                                    type -> ERROR) and its endpoints match it.
    edge_set                ERROR  the repo's stored edges == the edges its stored
                                    records imply, both directions (#89).
    no_drift                ERROR  stored people/artifacts == freshly re-derived
                                    from the bundle (self-sourced from the store
                                    via extract); with --bundle the stored records
                                    must also equal the bundle's.
    idempotency             ERROR  re-folding the bundle changes no counts
                                    (bundle self-sourced from the store).

What the gate proves, and what it cannot (issue #89). Self-sourced, it proves
the store is CONSISTENT: every edge, person and artifact is exactly what the
stored records and ledger imply, and every citation points at the node's own
GitHub object. It cannot prove the records themselves are what GitHub holds: a
record deleted together with its edges, a blob rewritten consistently (title,
merged, a comment body) or a wrong ledger leave a self-consistent store. A
`--bundle` saved when the store was gathered (a view.py JSON of the fresh store,
or a raw bundle) catches changed or deleted records; there is no per-fold
content digest in the store.

The row-level checks (referential_integrity, provenance, no_fabrication,
schema_conformance) audit the rows the audited repo owns: a node by its
project/repo columns, an edge by the repo of its src (a person's edge by the
repo of its dst), as edge_set assigns them. Rows no member repo owns (people,
non-member rows) and the store-wide parts (the per-window facts index,
case-variant scopes) are audited with them in a single-repo report, and once,
project-wide, in project mode (issue #107).

Project mode (validate_project) adds the project-wide checks:
    no_drift_people         ERROR  project people == union re-derived over members.
    referential_integrity, provenance, no_fabrication, schema_conformance
                            ERROR  the same checks over the shared rows and the
                                    store-wide parts, once for the project.
    member_status           ERROR  every manifest member's last gather of every
                                    recorded window is ok (#77); a failed member
                                    with no node at all is reported here.
"""

import argparse
import copy
import json
import re
import sqlite3
import sys

import graphstore
from graphstore import SPINE_EDGE_TYPES
import derive
import extract

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

class CorruptRow(Exception):
    """A stored row the auditor cannot decode. `row` names it (a node id, or
    `src -[type]-> dst` for an edge) so the failed check points at the row."""

    def __init__(self, row, problem):
        super().__init__("{}: {}".format(row, problem))
        self.row = row


def _node(row):
    """Decode a node row, raising CorruptRow (naming the node id) when its data
    blob is not valid JSON, or when a social/code/person node's blob is not a
    JSON object (the writer always stores a dict for those). Repo singletons
    may legitimately hold other JSON (`codeowners` is an ordered rule list)."""
    try:
        node = graphstore._row_to_node(row)
    except (TypeError, ValueError) as exc:
        raise CorruptRow(row["id"], "node data is not valid JSON ({})".format(exc))
    needs_object = (node["node_class"] != "structure"
                    or _is_person_id(node["id"] or ""))
    if needs_object and not isinstance(node["data"], dict):
        raise CorruptRow(row["id"], "node data is a JSON {}, not an object".format(
            type(node["data"]).__name__))
    return node


def _edge(row):
    """Decode an edge row, raising CorruptRow (naming the edge) on bad JSON."""
    try:
        return graphstore._row_to_edge(row)
    except (TypeError, ValueError) as exc:
        raise CorruptRow("{} -[{}]-> {}".format(
            row["src_id"], row["edge_type"], row["dst_id"]),
            "edge data is not valid JSON ({})".format(exc))


def _all_node_ids(conn):
    return {r[0] for r in conn.execute("SELECT id FROM nodes")}


def _all_edges(conn):
    return [_edge(r) for r in conn.execute("SELECT * FROM edges")]


def _repo_nodes(conn, project, repo, node_class=None):
    """graphstore.repo_nodes (same rows, same order) decoded via _node, so a
    corrupt row raises CorruptRow naming it instead of a bare JSON error."""
    sql = "SELECT * FROM nodes WHERE project=? AND repo=?"
    params = [project, repo]
    if node_class is not None:
        sql += " AND node_class=?"
        params.append(node_class)
    sql += " ORDER BY ts IS NULL, ts, id"
    return [_node(r) for r in conn.execute(sql, params)]


def _detect_project_repo(conn, project, repo, allow_multi=False):
    """Derive (project, repo) from the store when not passed.

    project: the single non-sentinel project in `nodes` (people use repo "*").
    repo:    the single non-sentinel repo for that project. Works over a real
    gathered store; raises if the store holds >1 project/repo and the caller
    did not disambiguate.

    When allow_multi=True and there are multiple repos, repo is returned as
    None (so the caller can use the project path instead of raising).
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
        repos = graphstore.project_repos(conn, project)
        if len(repos) == 1:
            repo = repos[0]
        elif not repos:
            raise ValueError("project {} has no repo to validate".format(project))
        elif allow_multi:
            repo = None  # caller will use validate_project
        else:
            raise ValueError(
                "project {} holds multiple repos {} — pass --repo".format(project, repos))
    return project, repo


def _resolve_target(conn, project, repo, allow_multi=False):
    """(project, repo) to audit, WITHOUT ever turning a missing target into a
    pass (issue #79). Like _detect_project_repo, but an empty store (no project
    given) or a project with no stored repo (no repo given) comes back with the
    unknown part as None, so check_target reports it as a failed check rather
    than raising. Ambiguity (several projects/repos, none named) still raises
    ValueError: that is a usage error, not a verdict. A named repo is resolved
    to its stored spelling (GitHub names are case-insensitive, issue #70)."""
    if project is None and not _store_projects(conn):
        return None, repo
    if project is not None and repo is None \
            and not graphstore.project_repos(conn, project):
        return project, None
    project, repo = _detect_project_repo(conn, project, repo, allow_multi)
    if repo is not None:
        repo = graphstore.resolve_repo(conn, project, repo)
    return project, repo


def _store_projects(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL"))


def _target_problem(conn, project, repo):
    """Why (project, repo) cannot be audited, or None when it has stored nodes."""
    projects = _store_projects(conn)
    if not projects:
        return {"problem": "store holds no nodes"}
    if project is None or project not in projects:
        return {"problem": "project has no nodes in the store",
                "project": project, "stored_projects": projects}
    repos = graphstore.project_repos(conn, project)
    if repo is None or repo not in repos:
        return {"problem": "repo has no nodes in this project",
                "project": project, "repo": repo, "stored_repos": repos}
    return None


def _incomplete_details(entries):
    """ERROR details for member run-status entries that are not ok (#77)."""
    out = []
    for e in entries:
        d = {"severity": ERROR, "problem": "member gather did not complete",
             "repo": e["repo"], "window": "{}..{}".format(e["from"], e["to"]),
             "status": e["status"]}
        if e.get("error"):
            d["error"] = e["error"]
        out.append(d)
    return out


def check_target(conn, project, repo):
    """The audited (project, repo) exists: the store has nodes, the project has
    nodes and the repo is one of its stored member repos. Without this a
    mistyped --repo/--project, or an empty store, audited nothing and passed
    (issue #79). A manifest member whose last gather of any recorded window
    failed or was not attempted (meta member_status, issue #77) fails too: its
    stored data is incomplete."""
    problem = _target_problem(conn, project, repo)
    incomplete = [e for e in graphstore.incomplete_members(conn, project)
                  if e["repo"] == repo] if project is not None else []
    details = _incomplete_details(incomplete)
    if problem is not None:
        return _check("target", ERROR, [dict(problem, severity=ERROR)] + details)
    if details:
        return _check("target", ERROR, details)
    n = conn.execute("SELECT COUNT(*) FROM nodes WHERE project=? AND repo=?",
                     (project, repo)).fetchone()[0]
    return _check("target", ERROR, [{
        "severity": INFO,
        "note": "{} node(s) stored for {} {}".format(n, project, repo)}])


def check_member_status(conn, project):
    """Project-wide (issue #77): every manifest member's last gather of every
    recorded window is `ok`. A member that failed or was never attempted is an
    ERROR naming the member, the window and the reason, also when it holds no
    node at all (it is then not one of the audited member repos). A store no
    manifest gather wrote records no status and passes."""
    entries = graphstore.get_member_status(conn, project)
    details = _incomplete_details(
        [e for e in entries if e["status"] != graphstore.MEMBER_OK])
    if not details:
        details = [{"severity": INFO, "note": (
            "{} member gather(s) recorded, all ok".format(len(entries))
            if entries else "no member run status recorded")}]
    return _check("member_status", ERROR, details)


def _local(qid):
    """Full local id (everything after the first `#`; see graphstore.local_id)."""
    return graphstore.local_id(qid)


def _is_person_id(qid):
    """Project-scoped person id `{project}#person-{login}`. Classified by the
    LOCAL id, so a symbol whose path/name merely contains `#person-` is not —
    and an artifact whose PATH starts with `person-` (e.g. the symbol
    `person-data/main.tf#terraform:resource:x`, or `art:person-notes.md`) is an
    artifact, never a person."""
    local = _local(qid)
    return local.startswith("person-") and not graphstore.is_artifact_local(local)


# --- audit scope (issue #107) -------------------------------------------------

class _Scope:
    """Which stored rows a row-level check (referential_integrity, provenance,
    no_fabrication, schema_conformance) audits.

    Every row has one owner. A node is owned by the member repo its
    project/repo columns name; an edge by the member repo of its src, a
    person's edge by the member repo of its dst (as check_edge_set assigns
    them, _owned_by_repo). A row no member owns (people, a non-member repo's
    stand-ins, a row whose columns or ids name no member) is SHARED, and so
    are the genuinely store-wide parts (the per-window facts index, scopes
    stored under several case spellings).

    `members`: the member repos of `project`; `unit`: the member repo whose
    rows are audited (None: none); `shared`: also audit the shared rows and
    the store-wide parts. A single-repo audit takes its repo plus the shared
    part (on a one-repo store that is every row); project mode audits each
    member's own rows in its report and the shared part once, project-wide.
    """

    def __init__(self, project, members, unit, shared):
        self.project = project
        self.unit = unit
        self.shared = shared
        self._scopes = {"{}/{}".format(project, r): r for r in members}

    @classmethod
    def single_repo(cls, conn, project, repo):
        """The audit of one repo: its own rows and the shared part, where the
        other stored member repos of the project own theirs."""
        members = set(graphstore.project_repos(conn, project)) \
            if project is not None else set()
        if repo is not None:
            members.add(repo)
        return cls(project, members, repo, True)

    def _owner_of_id(self, qid):
        return self._scopes.get((qid or "").partition("#")[0])

    def node_owner(self, n):
        if n["project"] != self.project:
            return None
        return self._owner_of_id("{}/{}#".format(n["project"], n["repo"]))

    def edge_owner(self, src, dst):
        owner = self._owner_of_id(src)
        if owner is None and _is_person_id(src or "") \
                and graphstore.parse_id(src)["scope"] == self.project:
            owner = self._owner_of_id(dst)
        return owner

    def _covers(self, owner):
        return owner == self.unit if owner is not None else self.shared

    def has_node(self, n):
        return self._covers(self.node_owner(n))

    def has_edge(self, src, dst):
        return self._covers(self.edge_owner(src, dst))


def _scoped_nodes(conn, scope):
    """The decoded nodes `scope` owns (None: all). Rows are filtered on their
    raw columns BEFORE decoding, so a corrupt row fails only the audit that
    owns it."""
    return [_node(r) for r in conn.execute("SELECT * FROM nodes")
            if scope is None or scope.has_node(r)]


def _scoped_edges(conn, scope):
    """The decoded edges `scope` owns (None: all), filtered before decoding."""
    return [_edge(r) for r in conn.execute("SELECT * FROM edges")
            if scope is None or scope.has_edge(r["src_id"], r["dst_id"])]


def _store_wide(scope):
    """True when the audit includes the store-wide parts (no scope: all)."""
    return scope is None or scope.shared


# --- check 1: referential integrity ------------------------------------------

def check_referential_integrity(conn, scope=None):
    """Every edge's src_id and dst_id resolves to a stored node.

    EXCEPT spine edges (SPINE_EDGE_TYPES) may legitimately point at a
    not-yet-gathered node — that is the backfill case (a windowed PR closing an
    out-of-window issue). Those are reported as INFO ("missing thread"), not
    ERROR. The `owns` (person->area) edge's person endpoint may be a CODEOWNER
    who never contributed (no person node) — that is a documented clean-store
    reality, reported INFO. A dangling NON-spine edge (e.g. an `authored` whose
    person or target is absent) is an ERROR. Counts are reported per edge type.
    A per-window facts entry (issue #94) naming a value that is not stored, or
    that cannot be read, is an ERROR too (a store-wide part).

    `scope` (_Scope, issue #107) limits the audit to the edges it owns (and
    the facts index to an audit that includes the store-wide parts); None
    audits the whole store.
    """
    ids = _all_node_ids(conn)
    details = []
    per_type = {}
    for e in _scoped_edges(conn, scope):
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
    # The per-window facts index (issue #94) must name stored values: a view
    # of that window would otherwise silently lose its CI stats / structure.
    for key, problem in (graphstore.window_facts_problems(conn)
                         if _store_wide(scope) else []):
        details.append({"severity": ERROR, "meta_key": key,
                        "problem": problem,
                        "note": "dangling per-window facts reference"})
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
    passed it reconstructs the raw PR/issue/commit records from the store
    (`_reconstruct_raw_from_store`, which reads the comment/review authors embedded
    in node data) and still runs the full check.
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
    for n in _repo_nodes(conn, project, repo):
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


# A git object id: SHA-1 (40 hex) or SHA-256 (64 hex), lower case as git prints.
_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# A GitHub web link to a PR or issue: https://github.com/{owner}/{repo}/
# {pull|issues}/{N}, optionally followed by a path, query or fragment.
_GITHUB_ITEM_URL_RE = re.compile(
    r"\Ahttps://github\.com/([^/?#]+/[^/?#]+)/(pull|issues)/(\d+)(?:[/?#].*)?\Z",
    re.S)
_REVIEW_LOCAL_RE = re.compile(r"\Areview-(\d+)-(.+)\Z", re.S)
_EVENT_LOCAL_RE = re.compile(r"\Aevent-(pr|issue)-(\d+)-(.+)\Z", re.S)
_ITEM_LOCAL_RE = re.compile(r"\A(pr|issue)-(\d+)\Z")


def _repo_slug(project, repo):
    """The GitHub `owner/repo` a node's links must name: the repo column of a
    multi-repo member (`owner/repo`), else `{project}/{repo}` (single-repo
    stores use owner = project)."""
    repo = repo or ""
    return repo if "/" in repo else "{}/{}".format(project, repo)


def _item_url_problem(url, slug, kinds, number):
    """None when `url` is a github.com link to item `number` of repo `slug`
    (compared case-insensitively, as GitHub does) under one of `kinds`
    (`pull`/`issues`); else why not."""
    m = _GITHUB_ITEM_URL_RE.match(url) if isinstance(url, str) else None
    if m is None:
        return "url is not a https://github.com/{owner}/{repo}/pull|issues/{N} link"
    if m.group(1).lower() != slug.lower():
        return "url names repo {}, the node is in {}".format(m.group(1), slug)
    if m.group(2) not in kinds:
        return "url is a /{}/ link, expected /{}/".format(
            m.group(2), "/ or /".join(sorted(kinds)))
    if int(m.group(3)) != number:
        return "url names #{}, the node is #{}".format(m.group(3), number)
    return None


def _social_provenance_problem(node, local):
    """Why a PR / issue / review / event node's citation is not its own, or
    None. Other social local forms fall back to the generic source-ref rule."""
    data = node["data"]
    slug = _repo_slug(node["project"], node["repo"])
    m = _ITEM_LOCAL_RE.match(local)
    if m:
        kind, number = m.group(1), int(m.group(2))
        # issues and PRs share one number space: a backfilled `issue-N` that is
        # really a PR carries its /pull/N link (gather.make_backfill_fetcher).
        kinds = {"pull"} if kind == "pr" else {"issues", "pull"}
        if str(data.get("number")) != str(number):
            return "data.number {!r} does not match the id".format(data.get("number"))
        if not data.get("url"):
            return "no url: a PR/issue must cite its GitHub link"
        return _item_url_problem(data["url"], slug, kinds, number)
    m = _REVIEW_LOCAL_RE.match(local)
    if m:
        number, url = int(m.group(1)), data.get("url")
        # fold synthesizes `<pr url or pr-N>#pullrequestreview-<id>` when absent
        if isinstance(url, str) and url.startswith(
                "pr-{}#pullrequestreview-".format(number)):
            return None
        return _item_url_problem(url, slug, {"pull"}, number)
    m = _EVENT_LOCAL_RE.match(local)
    if m:
        kind, number, url = m.group(1), int(m.group(2)), data.get("url")
        if isinstance(url, str):
            # fold's fallback `<parent local>#event-<id>`, or the API url of a
            # timeline event (https://api.github.com/repos/{slug}/issues/events/N)
            if url.startswith("{}-{}#event-".format(kind, number)):
                return None
            api = "https://api.github.com/repos/{}/".format(slug)
            if url.lower().startswith(api.lower()):
                return None
        kinds = {"pull"} if kind == "pr" else {"issues", "pull"}
        return _item_url_problem(url, slug, kinds, number)
    return None if _has_source_ref(node) else "no url/number/sha in data"


def check_provenance(conn, scope=None):
    """Every social/code node cites a source that is ITS OWN (issue #89):

    - a PR (`pr-N`) / issue (`issue-N`) carries `number == N` and a `url` that
      is https://github.com/{owner}/{repo}/pull|issues/N for the node's repo
      (owner/repo compared case-insensitively); a review / lifecycle event
      cites a link to its parent PR/issue (or fold's synthesized fallback, or a
      timeline event's API url under the same repo);
    - a commit's local id is a 40- (or 64-) hex git sha and `data.sha` equals it;
    - an artifact cites a lifecycle commit ref.

    Any other social/code node needs some url/number/sha. Structure nodes
    (people, areas, milestones, releases, sprints, singletons) are exempt — the
    schema gives them no url and they are not narrated as sourced facts. This
    proves a citation points at the right object, not that the object's
    content is what GitHub says: that needs --bundle (see no_drift).

    `scope` (_Scope, issue #107) limits the audit to the nodes it owns; None
    audits the whole store.
    """
    details = []
    for n in _scoped_nodes(conn, scope):
        if n["node_class"] == "structure" or not n["id"]:
            continue
        local = graphstore.local_id(n["id"], n["project"], n["repo"])
        problem = None
        if n["node_class"] == "social":
            problem = _social_provenance_problem(n, local)
        elif graphstore.is_artifact_node(n):
            if not _has_source_ref(n):
                problem = "no lifecycle commit ref in data"
        elif n["node_class"] == "code":
            sha = (n["data"] or {}).get("sha")
            if not _SHA_RE.match(local):
                problem = "commit id {!r} is not a 40/64-hex git sha".format(local)
            elif sha != local:
                problem = "data.sha {!r} does not match the id".format(sha)
        if problem:
            details.append({"severity": ERROR, "id": n["id"],
                            "node_class": n["node_class"], "problem": problem})
    if not details:
        details.append({"severity": INFO, "note": "all social/code nodes sourced"})
    return _check("provenance", ERROR, details)


# --- check 4: no fabrication / no orphans ------------------------------------

_CONTRIB_EDGE_TYPES = ("authored", "merged", "reviewed", "reported",
                       "commented", "reacted")


# The singleton structure nodes fold writes per repo (gather.fold_bundle).
_SINGLETON_LOCALS = ("workflowstats", "codegraph", "codeowners", "labeltaxonomy")
_SOCIAL_LOCAL_RE = re.compile(
    r"\A(?:pr-\d+|issue-\d+|review-\d+-.+|event-(?:pr|issue)-\d+-.+|comment-.+)\Z",
    re.S)
_STRUCTURE_PREFIXES = ("area-", "milestone-", "release-", "sprint-")


def _id_grammar_problem(node):
    """Why a node id breaks the id grammar (STORE.md "Identity"), or None.

    Person ids are `{project}#person-{login}` with repo column "*"; every other
    id is `{project}/{repo}#{local}` and its scope must equal the row's own
    project/repo columns; the local form must be one the writer produces for
    the node's class (social: pr-N, issue-N, review-…, event-…, comment-…;
    code: a 40/64-hex sha or an artifact; structure: area-/milestone-/release-/
    sprint-… or a singleton)."""
    nid = node["id"]
    if "#" not in nid:
        return "id has no '#': not a qualified {scope}#{local} id"
    scope, _, local = nid.partition("#")
    if not scope or not local:
        return "id has an empty scope or local part"
    if _is_person_id(nid):
        if node["repo"] != "*" or scope != node["project"]:
            return ("person id must be {{project}}#person-{{login}} stored with "
                    "repo '*' (row project={!r} repo={!r})".format(
                        node["project"], node["repo"]))
        if node["node_class"] != "structure":
            return "person node is not a structure node"
        login = local[len("person-"):]
        if not login:
            return "person id names no login"
        stored_login = (node["data"] or {}).get("login")
        if stored_login is not None and stored_login != login:
            return "person data.login {!r} does not match the id".format(stored_login)
        return None
    if node["repo"] == "*":
        return "only person ids may use the project-wide repo '*'"
    want = "{}/{}".format(node["project"], node["repo"])
    if scope != want:
        return "id scope {!r} disagrees with the row's project/repo {!r}".format(
            scope, want)
    cls = node["node_class"]
    if cls == "social":
        ok = bool(_SOCIAL_LOCAL_RE.match(local))
    elif cls == "code":
        ok = graphstore.is_artifact_local(local) or bool(_SHA_RE.match(local))
    else:
        ok = local in _SINGLETON_LOCALS or any(
            local.startswith(p) and len(local) > len(p) for p in _STRUCTURE_PREFIXES)
    return None if ok else "local id {!r} is not a {} id form".format(local, cls)


def _case_variant_details(nodes):
    """ERROR details for id scopes stored under several case spellings (one
    repo forked into two id spaces, issue #70): every id whose scope is not the
    most-used spelling of its case-folded scope is named."""
    by_fold = {}
    for n in nodes:
        if not n["id"] or "#" not in n["id"]:
            continue
        scope = n["id"].partition("#")[0]
        by_fold.setdefault(scope.lower(), {}).setdefault(scope, []).append(n["id"])
    details = []
    for fold in sorted(by_fold):
        spellings = by_fold[fold]
        if len(spellings) < 2:
            continue
        canonical = sorted(spellings, key=lambda s: (-len(spellings[s]), s))[0]
        for scope in sorted(s for s in spellings if s != canonical):
            for nid in sorted(spellings[scope]):
                details.append({
                    "severity": ERROR, "id": nid,
                    "problem": "case-variant duplicate: scope {!r} is also stored "
                               "as {!r}".format(scope, canonical)})
    return details


def _code_form_problem(node, local):
    """Why a stored `code` node is none of the known code-node forms, or None
    (#101). The forms: a commit (a commit record, i.e. `data.sha`, under a
    40/64-hex sha id); a file artifact (an artifact record under `art:<path>`,
    `kind` not symbol/comment); a symbol/comment artifact (an artifact record
    under `<path>#<lang>:<subkind>:<name>`, `kind` symbol or comment). Any
    other combination is an unknown form, which no_drift cannot audit."""
    if not graphstore.is_artifact_node(node):
        if _SHA_RE.match(local):
            return None
        return ("unknown code-node form: commit record under non-sha id "
                "{!r}".format(local))
    symbol_kind = graphstore.is_symbol_node(node)
    if local.startswith("art:"):
        if not symbol_kind:
            return None
        return ("unknown code-node form: symbol/comment record under file "
                "artifact id {!r}".format(local))
    if graphstore.is_symbol_local(local):
        if symbol_kind:
            return None
        return ("unknown code-node form: non-symbol record under symbol id "
                "{!r}".format(local))
    return ("unknown code-node form: artifact record under non-artifact id "
            "{!r}".format(local))


def check_no_fabrication(conn, scope=None):
    """No invented entities, no historyless artifacts, no malformed ids.

    - Every person node is referenced by >=1 contribution edge (a person with
      NO contribution edge is a contributor we invented = ERROR). `owns` alone
      does NOT count — a CODEOWNER who never contributed has no person node (the
      write path skips it), so any EXISTING person node must have earned it.
    - Every artifact `code` node (local id `art:<path>` / `<path>#…`) has >=1
      lifecycle event (artifact with no history = ERROR).
    - Every `code` node is a known form (_code_form_problem, #101): a commit
      record under a sha id, or an artifact record under the matching artifact
      id form; anything else is an ERROR.
    - No node has an empty/None id; ids round-trip through parse_id, follow the
      id grammar and agree with the row's project/repo columns
      (_id_grammar_problem), and no scope is stored under two case spellings
      (issue #89; a store-wide part).

    `scope` (_Scope, issue #107) limits the audit to the nodes it owns (and
    the case-spelling check to an audit that includes the store-wide parts);
    None audits the whole store. A person's contribution edges count in any
    repo.
    """
    details = []
    nodes = _scoped_nodes(conn, scope)
    for n in nodes:
        if n["id"]:
            problem = _id_grammar_problem(n)
            if problem:
                details.append({"severity": ERROR, "id": n["id"],
                                "problem": problem})
    if _store_wide(scope):
        # ids only: the raw rows, so another audit's corrupt row is not read
        details.extend(_case_variant_details(
            conn.execute("SELECT id FROM nodes").fetchall()))
    # contribution edges by person src
    contrib_src = set()
    for r in conn.execute(
            "SELECT src_id FROM edges WHERE edge_type IN ({})".format(
                ",".join("?" for _ in _CONTRIB_EDGE_TYPES)),
            _CONTRIB_EDGE_TYPES):
        contrib_src.add(r[0])

    for n in nodes:
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
        # code node form, and artifact with no history
        elif n["node_class"] == "code":
            local = parsed["local"]
            problem = _code_form_problem(n, local)
            if problem:
                details.append({"severity": ERROR, "id": nid, "problem": problem})
            # `art:<path>` file artifacts and `<path>#<lang>:<subkind>:<name>`
            # symbol/comment artifacts, by record (graphstore.is_artifact_node).
            if graphstore.is_artifact_node(n):
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

    A stored node is classified on its class and record first (#101): a code
    node is an artifact or a commit by its record (graphstore.is_artifact_node),
    and a stored non-code node is never an artifact, whatever its id holds
    (`milestone-v1#a:b:c`). The artifact id shape is tested only for a dangling
    id (or with no `conn`).
    """
    local = _local(qid)
    node = None
    if conn is not None:
        node = node_cache.get(qid)
        if node is None and qid not in node_cache:
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (qid,)).fetchone()
            node = _node(row) if row else None
            node_cache[qid] = node
    if node is not None and node["node_class"] == "code":
        return "artifact" if graphstore.is_artifact_node(node) else "commit"
    if _is_person_id(qid):
        return "person"
    # Artifact form first: a symbol's PATH may itself start with a prefix below
    # (`issue-templates/main.tf#terraform:…`, `release-notes/…`).
    if node is None and graphstore.is_artifact_local(local):
        return "artifact"
    if local.startswith("area-"):
        return "area"
    if local.startswith("milestone-"):
        return "milestone"
    if local.startswith("release-"):
        return "release"
    if local.startswith("sprint-"):
        return "sprint"
    # Phase 10 slice 1: review/event social nodes (checked before pr-/issue- so a
    # review/event id is never misread as its bare-number parent kind).
    if local.startswith("review-"):
        return "review"
    if local.startswith("event-"):
        return "event"
    if local.startswith("pr-"):
        return "pr"
    if local.startswith("issue-"):
        return "issue"
    if node is None:
        return "<missing>"
    return node["node_class"]


# (edge_type) -> (allowed src kinds, allowed dst kinds): an explicit rule for
# EVERY edge type the store may hold (issue #89); any other type is an ERROR. A
# missing endpoint is not a schema violation here (referential_integrity owns
# that); we only flag a STORED endpoint whose kind contradicts STORE.md.
_EDGE_SCHEMA = {
    "authored":     ({"person"}, {"pr", "commit"}),
    "merged":       ({"person"}, {"pr"}),
    "reviewed":     ({"person"}, {"pr"}),
    "reported":     ({"person"}, {"issue"}),
    "commented":    ({"person"}, {"pr", "issue"}),
    "reacted":      ({"person"}, {"pr", "issue", "commit"}),
    "owns":         ({"person"}, {"area"}),
    "touches":      ({"commit"}, {"area"}),
    "depends_on":   ({"area"}, {"area"}),
    "in_milestone": ({"pr", "issue"}, {"milestone"}),
    "in_iteration": ({"pr", "issue"}, {"sprint"}),
    "closes":       ({"pr"}, {"issue"}),
    "cross_ref":    ({"pr"}, {"issue", "pr"}),
    "spun_off":     ({"issue"}, {"issue"}),
    "duplicate_of": ({"issue"}, {"issue"}),
    "part_of":      ({"commit", "review", "event"}, {"pr", "issue"}),
    "blocks":       ({"issue"}, {"issue"}),
    "replaced_by":  ({"artifact"}, {"artifact"}),
    "identity_from": ({"artifact"}, {"artifact"}),
}

# Edge types whose two endpoints are always in the SAME repo: the writer derives
# them inside one repo's bundle. closes / cross_ref / depends_on may cross
# member repos (manifest folds); contribution edges start at a project person.
_SAME_REPO_EDGE_TYPES = frozenset((
    "touches", "part_of", "blocks", "in_milestone", "in_iteration",
    "replaced_by", "identity_from", "spun_off", "duplicate_of"))


def _id_project(qid):
    """The project an id belongs to: a person's scope, or the first segment of
    a `{project}/{repo}` scope (a manifest project name holds no '/')."""
    scope = graphstore.parse_id(qid)["scope"]
    return scope if _is_person_id(qid) else scope.split("/", 1)[0]


def check_schema_conformance(conn, scope=None):
    """Each edge type's endpoints match STORE.md semantics: `authored` src is a
    person and dst a pr/commit; `touches` commit->area; `owns` person->area;
    `cross_ref` pr->issue/pr; `in_milestone` dst is a milestone; etc. A STORED
    endpoint whose kind contradicts the schema = ERROR (a missing endpoint is
    referential_integrity's job, not flagged here). An edge type with no rule
    in _EDGE_SCHEMA is an ERROR (issue #89: unknown types used to be skipped).
    Both endpoints of an edge belong to one project, and the types in
    _SAME_REPO_EDGE_TYPES stay inside one repo.

    `scope` (_Scope, issue #107) limits the audit to the edges it owns; None
    audits the whole store.
    """
    cache = {}
    details = []
    for e in _scoped_edges(conn, scope):
        et = e["edge_type"]
        rule = _EDGE_SCHEMA.get(et)
        if rule is None:
            details.append({
                "severity": ERROR, "edge_type": et,
                "src": e["src_id"], "dst": e["dst_id"],
                "problem": "unknown edge type (no endpoint rule in STORE.md)"})
            continue
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
        if _id_project(e["src_id"]) != _id_project(e["dst_id"]):
            details.append({
                "severity": ERROR, "edge_type": et,
                "src": e["src_id"], "dst": e["dst_id"],
                "problem": "edge crosses projects"})
        elif et in _SAME_REPO_EDGE_TYPES and (
                graphstore.parse_id(e["src_id"])["scope"]
                != graphstore.parse_id(e["dst_id"])["scope"]):
            details.append({
                "severity": ERROR, "edge_type": et,
                "src": e["src_id"], "dst": e["dst_id"],
                "problem": "edge crosses repos (this type stays inside one repo)"})
    if not details:
        details.append({"severity": INFO, "note": "all edge endpoints conform"})
    return _check("schema_conformance", ERROR, details)


# --- check 6: edge set == re-derived from the stored records -----------------

# Re-derived from the stored records alone (record blobs refresh, never go
# stale): compared exactly in both directions on every store.
_RECORD_EDGE_TYPES = frozenset((
    "authored", "merged", "reviewed", "reported", "commented",
    "closes", "cross_ref", "part_of"))
# Current-state edges (issue #95): derived from inputs that change — the
# PR/issue records and milestone / sprint nodes (blocks, in_milestone,
# in_iteration), the per-window code graph and CODEOWNERS facts (depends_on,
# owns) — plus touches, derived from each window's code graph. On a one-window
# store every one is exact. On a multi-window store each is exact when the
# store shows it can be proven (_multi_window_plan): the item relations once a
# fold since #95 rebuilt them, depends_on / owns once such a fold replayed
# them over every window's facts, touches when every window's code graph is
# stored and maps each path to one area. Otherwise a difference is a WARN
# naming the reason.
_CURRENT_STATE_EDGE_TYPES = frozenset((
    "owns", "touches", "depends_on", "in_milestone", "in_iteration", "blocks"))
_ITEM_STATE_EDGE_TYPES = frozenset(("blocks", "in_milestone", "in_iteration"))
_STRUCTURE_STATE_EDGE_TYPES = frozenset(("depends_on", "owns"))
_PRE95_REASON = ("this repo was last folded before current-state edges were "
                 "retracted (#95); fold any window of it once more to make "
                 "this check exact")
# Symbol moves, matched per fold window: exact on a one-window store; on a
# multi-window store each stored move must be supported by the ledger
# (_move_problem) and a cross-window re-match is not required.
_MOVE_EDGE_TYPES = frozenset(("replaced_by", "identity_from"))


def _repo_window_count(conn, project, repo):
    """How many distinct gathered windows the meta ledger records for this
    repo (0 when none; an unreadable ledger counts as one, i.e. strict)."""
    try:
        wins = graphstore.get_windows(conn)
    except ValueError:
        return 1
    return len({(w.get("from"), w.get("to")) for w in wins
                if isinstance(w, dict) and w.get("project") == project
                and w.get("repo") == repo})


def _owned_by_repo(src, dst, project, repo):
    """An edge belongs to the repo whose id space its src is in; a person's
    edge (project-scoped src) belongs to the repo of its dst."""
    prefix = graphstore.qualify_id(project, repo, "")
    if src.startswith(prefix):
        return True
    return (_is_person_id(src) and graphstore.parse_id(src)["scope"] == project
            and dst.startswith(prefix))


def _expected_edges(bundle, project, repo):
    """{(src, dst, type): data} the writer derives from `bundle`: the bundle is
    folded by gather.fold_bundle into an EMPTY in-memory store and its edges
    read back. Single-repo semantics (members=None): cross-repo edges, which
    need the manifest's member set and registries, are checked separately."""
    scratch = graphstore.open_store(":memory:")
    try:
        graphstore.init_schema(scratch)
        gather.fold_bundle(scratch, copy.deepcopy(bundle), project=project,
                           repo=repo)
        return {(e["src_id"], e["dst_id"], e["edge_type"]): e["data"]
                for e in (graphstore._row_to_edge(r) for r in
                          scratch.execute("SELECT * FROM edges"))}
    finally:
        scratch.close()


def _touches_expected(windows, bundle, project, repo):
    """(expected, reason): the touches the stored commits imply against the
    UNION of every window's code-graph path index — each commit was folded
    with the code graph of a window that saw its files, and a directory area
    of a path is the same in every window — or (None, why not): a graphify
    window (community ids are per run) or a path two windows put in different
    areas."""
    merged = {}
    for frm, to, cg, _co, _sha in windows:
        if not cg:
            return None, ("the window {}..{} stored no code graph, so its "
                          "commits' areas cannot be re-derived".format(frm, to))
        if cg.get("provider") == "graphify":
            return None, ("a window's code graph is a graphify one (its "
                          "community ids are per run), so an older commit's "
                          "areas cannot be re-derived")
        for path, aid in derive.area_index(cg or {}).items():
            if merged.setdefault(path, aid) != aid:
                return None, ("the windows' code graphs put {} in different "
                              "areas, so an older commit's areas cannot be "
                              "re-derived".format(path))
    out = {}
    for c in bundle.get("commits") or []:
        for aid in sorted(derive._commit_areas(c, merged)):
            out[(graphstore.qualify_id(project, repo, c["sha"]),
                 graphstore.qualify_id(project, repo, "area-{}".format(aid)),
                 "touches")] = None
    return out, None


def _multi_window_plan(conn, project, repo, bundle):
    """How check_edge_set compares the current-state edges of a multi-window
    store (issue #95): {"expected": {type: {key: data}} for the types whose
    expected set is not the self-sourced bundle's re-fold, "exact": the types
    compared exactly, "reasons": {type: why a difference is only a WARN},
    "replay": the gather.StructureReplay or None}.

    - blocks / in_milestone / in_iteration: exact once a fold since #95
      rebuilt them from the stored records (meta current_state `items`); the
      re-fold of the self-sourced bundle (every stored record and milestone /
      sprint) is then exactly what the store holds.
    - depends_on / owns: exact once a fold since #95 replayed them over every
      window's facts (`structure`) and every gathered window still has its
      facts; expected = the same replay (gather.StructureReplay, without the
      manifest's registries: a cross-repo edge is checked against the code
      graphs of the windows in effect for its source).
    - touches: exact when every window has its facts and _touches_expected
      can merge their path indexes."""
    state = graphstore.get_current_state(conn, project, repo) or {}
    windows = gather.repo_fact_windows(conn, project, repo)
    plan = {"expected": {}, "exact": set(), "reasons": {}, "replay": None}
    if state.get("items") is True:
        plan["exact"] |= _ITEM_STATE_EDGE_TYPES
    else:
        for et in _ITEM_STATE_EDGE_TYPES:
            plan["reasons"][et] = _PRE95_REASON
    no_facts = ("a gathered window of this repo has no per-window facts "
                "(folded before #94); re-gather it to make this check exact")
    if windows is None:
        for et in _STRUCTURE_STATE_EDGE_TYPES | {"touches"}:
            plan["reasons"][et] = no_facts
        return plan
    if state.get("structure") is True:
        replay = gather.StructureReplay(windows, project, repo)
        plan["replay"] = replay
        want = {k: v for k, v in replay.desired().items()
                if _owned_by_repo(k[0], k[1], project, repo)}
        for et in _STRUCTURE_STATE_EDGE_TYPES:
            plan["expected"][et] = {k: v for k, v in want.items() if k[2] == et}
        plan["exact"] |= _STRUCTURE_STATE_EDGE_TYPES
    else:
        for et in _STRUCTURE_STATE_EDGE_TYPES:
            plan["reasons"][et] = _PRE95_REASON
    touches, why = _touches_expected(windows, bundle, project, repo)
    if touches is None:
        plan["reasons"]["touches"] = why
    else:
        plan["expected"]["touches"] = touches
        plan["exact"].add("touches")
    return plan


def _project_members(conn, project):
    """Every member slug the store knows for `project`: stored repos plus the
    repos of recorded member run status (a failed member still was a member)."""
    members = set(graphstore.project_repos(conn, project))
    try:
        members |= {e["repo"] for e in graphstore.get_member_status(conn, project)}
    except (ValueError, TypeError, KeyError):
        pass
    return members


def _cross_repo_dep_target(edge):
    """The local id a manifest fold gives the TARGET of an unresolved registry
    edge once it resolves to another member: a Terraform module source names the
    member's root module (gather._ROOT_AREA_LOCAL); a Bicep public-registry ref
    names the publisher's module area, the directory of `<path>/main.bicep`
    (gather.resolve_bicep_registry_member). None when the ref cannot resolve
    across repos at all (a private registry, template spec or alias)."""
    if edge.get("provider") == "bicep":
        path = (gather._bicep_ref_parts(edge.get("ref"))[0] or "").strip("/")
        if not path:
            return None
        area = gather.classify_code_area(path + "/main.bicep",
                                         gather.DEFAULT_AREA_PATTERNS)
        if not area or gather._is_avm_group_area(area):
            return None
        return "area-" + area
    return gather._ROOT_AREA_LOCAL


def _cross_repo_problem(key, data, bundle, project, repo, members,
                        graphs=None):
    """None when a stored edge from this repo into ANOTHER member repo is one
    the manifest fold derives from the stored records: a `closes`/`cross_ref`
    from a PR's qualified refs / timeline cross-references
    (gather._cross_repo_pr_edges), or a `depends_on` from an unresolved
    registry edge of the source area in the stored code graph to the area that
    edge resolves to in a member (_cross_repo_dep_target: the root module for
    Terraform, the module directory for a Bicep public-registry ref). Else why
    not (or "not cross-repo"). `graphs` (issue #95): the code graphs to look
    in for a depends_on edge (the windows in effect for its source); default
    the bundle's."""
    src, dst, et = key
    dst_scope = graphstore.parse_id(dst)["scope"]
    if not dst_scope.startswith(project + "/") or \
            dst_scope == "{}/{}".format(project, repo):
        return "not cross-repo"
    dst_repo = dst_scope[len(project) + 1:]
    local = graphstore.local_id(src, project, repo)
    if et in ("closes", "cross_ref"):
        m = _ITEM_LOCAL_RE.match(local)
        pr = next((p for p in bundle.get("prs") or []
                   if m and m.group(1) == "pr"
                   and str(p.get("number")) == m.group(2)), None)
        if pr is None:
            return "source is not a stored PR"
        _, edges = gather._cross_repo_pr_edges(pr, project, repo,
                                               set(members) | {dst_repo})
        if any(e[0] == src and e[1] == dst and e[2] == et for e in edges):
            return None
        return "the PR's text and timeline do not reference {}".format(dst)
    if et == "depends_on":
        dst_local = graphstore.local_id(dst)
        aid = local[len("area-"):] if local.startswith("area-") else None
        wrong_target = None
        if graphs is None:
            graphs = [bundle.get("code_graph") or {}]
        areas = [a for g in graphs for a in (g or {}).get("areas") or []]
        for area in areas:
            if area.get("id") != aid:
                continue
            for e in area.get("edges") or []:
                if e.get("resolved") or not e.get("ref"):
                    continue
                want = {k: v for k, v in e.items() if k != "to"}
                want.update(resolved=True, cross_repo=True)
                if want != data:
                    continue
                expected = _cross_repo_dep_target(e)
                if expected is not None and dst_local == expected:
                    return None
                wrong_target = expected
        if wrong_target is not None:
            return ("this registry edge resolves to a member's {!r}, not {!r}"
                    .format(wrong_target, dst_local))
        return "no unresolved registry edge of the source area carries this data"
    return "{} never crosses repos".format(et)


def _move_problem(key, data, stored, bundle, project, repo):
    """None when a stored symbol-move edge is supported by the repo's ledger:
    its twin (`identity_from` <-> `replaced_by`) carries the same data, both
    ends are the same non-comment symbol in two files of this repo, the ledger
    holds a drop of the source (by `move_commit` when recorded) and an add of
    the destination, and the basis/confidence pair is consistent with the
    recorded renames. Else why not."""
    src, dst, et = key
    twin_type = "identity_from" if et == "replaced_by" else "replaced_by"
    if (dst, src, twin_type) not in stored or stored[(dst, src, twin_type)] != data:
        return "no {} twin with the same data".format(twin_type)
    if et == "identity_from":
        src, dst = dst, src
    prefix = graphstore.qualify_id(project, repo, "")
    if not (src.startswith(prefix) and dst.startswith(prefix)):
        return "a move stays inside one repo"
    a = graphstore.split_symbol_local(src[len(prefix):])
    b = graphstore.split_symbol_local(dst[len(prefix):])
    if a is None or b is None or a[1:] != b[1:] or a[0] == b[0]:
        return "the ends are not the same symbol in two files"
    if a[2] in derive._COMMENT_SUBKINDS:
        return "comments are never matched as moves"
    data = data or {}

    def events(change, path):
        return [e for e in bundle.get("symbol_events") or []
                if e.get("change") == change and e.get("path") == path
                and (e.get("lang") or "") == a[1] and e.get("subkind") == a[2]
                and (e.get("name") or "") == a[3]]
    drops = events("drop", a[0])
    if not drops or (data.get("move_commit") and not any(
            e.get("commit") == data["move_commit"] for e in drops)):
        return "the ledger holds no drop of the source"
    if not events("add", b[0]):
        return "the ledger holds no add of the destination"
    renames = {(e.get("old_path"), e.get("path"))
               for e in bundle.get("code_events") or []
               if e.get("change") in ("rename", "copy")}
    basis, conf = data.get("move_basis"), data.get("move_confidence")
    if basis == "file_rename" and conf == "high" and (a[0], b[0]) in renames:
        return None
    if basis == "unique_name" and conf == "medium":
        return None
    return "basis/confidence {}/{} is not supported by the ledger".format(basis, conf)


def check_edge_set(conn, project, repo, bundle):
    """The repo's stored edges EQUAL the edges its stored records imply, in
    both directions (issue #89: completeness used to be expected ⊆ stored for
    contribution edges only, so invented edges passed).

    Expected = what gather.fold_bundle writes for the repo's self-sourced
    bundle, folded into an empty scratch store (_expected_edges): contribution
    edges, closes / cross_ref / part_of, blocks, touches, owns, in_milestone,
    in_iteration, depends_on (from the codegraph singleton) and symbol moves.
    An edge belongs to the repo of its src, a person's edge to the repo of its
    dst (_owned_by_repo).

    - derived but not stored -> ERROR; stored but not derivable -> ERROR, with
      a #78 hint when the source is a pre-#78 git-name person; same key with
      different data -> ERROR.
    - A stored edge into ANOTHER member repo is checked against the manifest
      derivation (_cross_repo_problem); a missing cross-repo edge is not
      detected (it needs the manifest's registries).
    - Multi-window store (more than one gathered window recorded for the repo):
      each _CURRENT_STATE_EDGE_TYPES type is compared exactly when
      _multi_window_plan can prove it (depends_on / owns / touches against
      their replay over the per-window facts, the item relations against the
      re-fold), else a difference is a WARN naming why (issue #95); each
      stored move must be supported by the ledger (_move_problem) and
      cross-window move re-matches are not required.

    This proves the edges are exactly what the stored records imply, not that
    the records are what GitHub holds (see no_drift's --bundle cross-check).
    """
    multi = _repo_window_count(conn, project, repo) > 1
    expected = {k: v for k, v in _expected_edges(bundle, project, repo).items()
                if _owned_by_repo(k[0], k[1], project, repo)}
    plan = _multi_window_plan(conn, project, repo, bundle) if multi else None
    if plan is not None:
        for et, want in plan["expected"].items():
            expected = {k: v for k, v in expected.items() if k[2] != et}
            expected.update(want)
    stored = {(e["src_id"], e["dst_id"], e["edge_type"]): e["data"]
              for e in _all_edges(conn)
              if _owned_by_repo(e["src_id"], e["dst_id"], project, repo)}
    members = None
    hints = {}
    details = []
    per_type = {}

    def lenient_note(et):
        return " ({}; multi-window store)".format(plan["reasons"][et])

    for key in sorted(set(stored) | set(expected)):
        src, dst, et = key
        per_type.setdefault(et, {"stored": 0, "derived": 0})
        if key in stored:
            per_type[et]["stored"] += 1
        if key in expected:
            per_type[et]["derived"] += 1
        base = {"edge_type": et, "src": src, "dst": dst}
        lenient = (multi and et in _CURRENT_STATE_EDGE_TYPES
                   and et not in plan["exact"])
        if key in stored and key in expected:
            if stored[key] != expected[key] and not lenient and not (
                    multi and et in _MOVE_EDGE_TYPES):
                details.append(dict(base, severity=ERROR,
                                    problem="edge data drift",
                                    stored=stored[key], expected=expected[key]))
            continue
        if key in expected:
            if multi and et in _MOVE_EDGE_TYPES:
                continue   # a cross-window re-match: no single window had it
            details.append(dict(
                base, severity=WARN if lenient else ERROR,
                problem="derived from the stored records but not stored"
                        + (lenient_note(et) if lenient else "")))
            continue
        # stored but not derivable from this repo's records
        if members is None:
            members = _project_members(conn, project)
        graphs = None
        replay = plan["replay"] if plan is not None else None
        if et == "depends_on" and replay is not None:
            graphs = [replay.graphs[i] for i in replay.dep_in_effect(
                src, gather._dep_is_transitive(stored[key]))]
        cross = _cross_repo_problem(key, stored[key], bundle, project, repo,
                                    members, graphs=graphs)
        if cross is None:
            continue
        if multi and et in _MOVE_EDGE_TYPES:
            problem = _move_problem(key, stored[key], stored, bundle, project, repo)
            if problem is None:
                continue
            details.append(dict(base, severity=ERROR, data=stored[key],
                                problem="move edge not supported by the "
                                        "ledger: " + problem))
            continue
        # A stored depends_on / owns edge that no window's facts produce is
        # stale only when some authoritative window says it is gone. When a
        # re-gather replaced the facts that carried it with ones that cannot
        # judge (failed build, no scan), fold keeps the edge and so must we:
        # it can be warned about, not proven wrong.
        unproven = (not lenient and replay is not None
                    and et in ("depends_on", "owns")
                    and not replay.retractable(key, stored[key]))
        d = dict(base, severity=WARN if (lenient or unproven) else ERROR,
                 problem="stored but not derivable from the stored records"
                         + (lenient_note(et) if lenient else "")
                         + (" (no gathered window has authoritative input for "
                            "it, so it is kept)" if unproven else ""))
        if cross != "not cross-repo":
            d["cross_repo"] = cross
        if _is_person_id(src):
            if src not in hints:
                hints[src] = _pre78_hint(conn, project, src)
            if hints[src]:
                d["hint"] = hints[src]
        details.append(d)
    note = "{} stored edge(s) of this repo compared with {} re-derived from its " \
           "records".format(len(stored), len(expected))
    if multi:
        note += " (multi-window store; exact: {}; WARN only: {})".format(
            ", ".join(sorted(plan["exact"])) or "none",
            ", ".join(sorted(_CURRENT_STATE_EDGE_TYPES - plan["exact"]))
            or "none")
    details.append({"severity": INFO, "summary": "per_type_counts",
                    "note": note, "counts": per_type})
    return _check("edge_set", ERROR, details)


# --- check 7: store-derived == link-derived (no drift) -----------------------

def _derive_people(bundle):
    """Re-derive the people projection exactly as the write path does, via the
    SAME shared enumerator (derive.enumerate_participants) — so writer and
    auditor can never diverge. Run attach_commit_prs on a COPY first (so
    reviewers of message-resolved PRs are attributed), then enumerate the FULL
    participant set (every login with a contribution edge, bot-tagged)."""
    area_idx = derive.area_index(bundle.get("code_graph", {}) or {})
    resolved = copy.deepcopy(bundle.get("commits") or [])
    gather.attach_commit_prs(resolved)
    # Seeded with NO people (issue #89): a self-sourced bundle's `people` is
    # read from the stored person nodes, and enumerate only ADDS to its seed, so
    # seeding with it made the areas check tautological. A raw gather bundle
    # carries `people: {}`, so this is also exactly what the writer derives.
    view = {**bundle, "commits": resolved, "people": {}}
    return derive.enumerate_participants(view, area_idx)


def _stored_project_people(conn, project):
    """The stored project-scoped people projection
    {login: {modules, areas, is_bot, login_resolved}}. People carry the repo
    sentinel "*", so this reads them once for the project. `login_resolved` is
    False only for an unresolved commit author (#78)."""
    out = {}
    for n in _repo_nodes(conn, project, "*", "structure"):
        local = _local(n["id"])
        if local.startswith("person-"):
            login = local[len("person-"):]
            d = n["data"] or {}
            out[login] = {"modules": sorted(d.get("modules") or []),
                          "areas": sorted(d.get("areas") or []),
                          "is_bot": bool(d.get("is_bot")),
                          "login_resolved": d.get("login_resolved") is not False}
    return out


def _norm_people(people):
    """Normalize a derived people dict to the stored projection's key fields."""
    return {login: {"modules": sorted(rec.get("modules") or []),
                    "areas": sorted(rec.get("areas") or []),
                    "is_bot": bool(rec.get("is_bot")),
                    "login_resolved": rec.get("login_resolved") is not False}
            for login, rec in people.items()}


# How strictly stored people are compared with the re-derivation:
#   exact  - same logins, same fields (one-window, one-repo store; project mode)
#   window - same logins and flags; areas/modules: derived ⊆ stored, an extra
#            stored area is a WARN (a multi-window store: each window attributed
#            areas with ITS code graph, and only the latest one is stored)
#   member - derived ⊆ stored for logins and areas, flags exact (one member of
#            a multi-member project: people are project-wide, the full equality
#            is no_drift_people's; or an external --bundle of one window of a
#            multi-window store)
PEOPLE_EXACT, PEOPLE_WINDOW, PEOPLE_MEMBER = "exact", "window", "member"


def _pre78_hint(conn, project, person_qid):
    """An actionable hint when a stored person is a pre-#78 git-name identity,
    else None: the node has no `login_resolved` flag (written before #78), all
    its contribution edges are `authored` -> commit, and those commits now
    name another author. Folding a post-#78 bundle into a pre-#78 store leaves
    exactly this behind; it is a real mismatch, not a false alarm."""
    row = conn.execute("SELECT data FROM nodes WHERE id=?", (person_qid,)).fetchone()
    try:
        pdata = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return None
    if not isinstance(pdata, dict) or "login_resolved" in pdata:
        return None
    login = graphstore.local_id(person_qid)[len("person-"):]
    authors = set()
    contrib = 0
    for dst, et in conn.execute("SELECT dst_id, edge_type FROM edges "
                                "WHERE src_id=?", (person_qid,)):
        if et not in _CONTRIB_EDGE_TYPES:
            continue
        contrib += 1
        node = conn.execute("SELECT node_class, data FROM nodes WHERE id=?",
                            (dst,)).fetchone()
        if et != "authored" or node is None or node[0] != "code":
            return None
        try:
            author = (json.loads(node[1]) or {}).get("author")
        except (TypeError, ValueError, AttributeError):
            return None
        if author == login:
            return None
        authors.add(str(author))
    if not contrib:
        return None
    return ("pre-#78 identity: {!r} is a commit's git author name from before "
            "people were keyed by GitHub login (#78); the same commit(s) now name "
            "{}. Folding a post-#78 bundle into a pre-#78 store leaves the old "
            "person node and its `authored` edges beside the login-keyed ones. "
            "Re-gather into a fresh store (STORE.md \"Identity (#78)\").".format(
                login, ", ".join(sorted(authors))))


def _people_drift_details(stored_people, want_norm, mode=PEOPLE_EXACT,
                          conn=None, project=None):
    """The details for any divergence between stored and re-derived people,
    under `mode` (PEOPLE_EXACT / PEOPLE_WINDOW / PEOPLE_MEMBER, see above). A
    stored person that is not derivable carries a #78 hint when it is a
    pre-#78 git-name identity (needs `conn`/`project`)."""
    details = []
    for login in sorted(set(stored_people) | set(want_norm)):
        if login not in stored_people:
            details.append({"severity": ERROR, "kind": "person", "login": login,
                            "problem": "derived but not stored", "expected": want_norm[login]})
            continue
        if login not in want_norm:
            if mode == PEOPLE_MEMBER:
                continue   # another member's person: audited project-wide
            d = {"severity": ERROR, "kind": "person", "login": login,
                 "problem": "stored but not derivable", "stored": stored_people[login]}
            if conn is not None:
                hint = _pre78_hint(conn, project,
                                   graphstore.qualify_person(project, login))
                if hint:
                    d["hint"] = hint
            details.append(d)
            continue
        s, w = stored_people[login], want_norm[login]
        if mode == PEOPLE_EXACT:
            if s != w:
                details.append({"severity": ERROR, "kind": "person", "login": login,
                                "problem": "person drift", "stored": s, "expected": w})
            continue
        bad = [f for f in ("is_bot", "login_resolved") if s[f] != w[f]]
        missing = {f: sorted(set(w[f]) - set(s[f])) for f in ("areas", "modules")
                   if set(w[f]) - set(s[f])}
        if bad or missing:
            details.append({"severity": ERROR, "kind": "person", "login": login,
                            "problem": "person drift", "fields": bad,
                            "missing": missing, "stored": s, "expected": w})
        extra = {f: sorted(set(s[f]) - set(w[f])) for f in ("areas", "modules")
                 if set(s[f]) - set(w[f])}
        if extra and mode == PEOPLE_WINDOW:
            details.append({"severity": WARN, "kind": "person", "login": login,
                            "problem": "stored areas not re-derivable from the "
                                       "latest code graph (multi-window store)",
                            "extra": extra})
    return details


def _derive_people_project(conn, project, repos):
    """Re-derive the PROJECT-WIDE people aggregate: per-member enumerate (exactly as
    each fold does), unioned across members (areas/modules union, is_bot OR,
    unresolved if any member derived it unresolved) — the read-side mirror of
    the union-merge fold_bundle writes."""
    agg = {}
    for repo in repos:
        bundle, reason = _self_source_bundle(conn, project, repo)
        if bundle is None:
            raise _NotSourced("{}: {}".format(repo, reason))
        for login, rec in _derive_people(bundle).items():
            cur = agg.setdefault(login, {"areas": set(), "modules": set(),
                                         "is_bot": False, "login_resolved": True})
            cur["areas"] |= set(rec.get("areas") or [])
            cur["modules"] |= set(rec.get("modules") or [])
            cur["is_bot"] = cur["is_bot"] or bool(rec.get("is_bot"))
            if rec.get("login_resolved") is False:
                cur["login_resolved"] = False
    return {login: {"modules": sorted(v["modules"]), "areas": sorted(v["areas"]),
                    "is_bot": v["is_bot"], "login_resolved": v["login_resolved"]}
            for login, v in agg.items()}


def check_no_drift_people(conn, project, repos):
    """PROJECT-WIDE people no-drift: the stored project-scoped person nodes equal
    the people re-derived across ALL members and unioned (mirroring fold_bundle's
    union-merge). People are project-scoped, so this is the correct granularity — a
    per-member check can never reproduce the cross-member union. Exact, except
    that when any member holds several gathered windows the areas compare as
    derived ⊆ stored (PEOPLE_WINDOW)."""
    try:
        want = _norm_people(_derive_people_project(conn, project, repos))
    except _NotSourced as exc:
        return _not_sourced("no_drift_people", str(exc))
    stored = _stored_project_people(conn, project)
    multi = any(_repo_window_count(conn, project, r) > 1 for r in repos)
    details = _people_drift_details(stored, want,
                                    PEOPLE_WINDOW if multi else PEOPLE_EXACT,
                                    conn, project)
    if not any(d["severity"] == ERROR for d in details):
        details.append({"severity": INFO,
                        "note": "store matches re-derived project people"})
    return _check("no_drift_people", ERROR, details)


# Keys fold adds to a stored record that the raw bundle record lacks.
_FOLD_ADDED_RECORD_KEYS = ("external_refs",)
# Keys a view (extract) adds to a record from other stored rows.
_VIEW_ADDED_RECORD_KEYS = ("blocking", "blocked_by")


def _comparable_record(rec):
    """A record in the form both a raw bundle and a saved view (view.py /
    extract output) agree on: the view re-attaches `reviews` / `lifecycle`
    from their own nodes (in timestamp order, with fold's synthesized `url`),
    so those lists compare as sorted entries without `url`."""
    out = {k: v for k, v in rec.items() if k not in _VIEW_ADDED_RECORD_KEYS}
    for key in ("reviews", "lifecycle"):
        if isinstance(out.get(key), list):
            out[key] = sorted(
                json.dumps({k: v for k, v in e.items() if k != "url"}
                           if isinstance(e, dict) else e, sort_keys=True)
                for e in out[key])
    return out


def _record_drift_details(conn, project, repo, bundle):
    """With an EXTERNAL --bundle: each PR / issue / commit record in it must be
    stored, with the same content. This is what the store alone cannot show — a
    record deleted together with its edges, or a blob rewritten consistently
    (title, merged, a comment body, a reviewer dropped with its edge). The
    bundle is a raw bundle (gather.acquire's return value) or a view saved
    from the store when it was gathered (view.py output), so it must hold the
    latest fetch of its records: an older window's bundle on a store that
    re-fetched a record later reports that record as different."""
    details = []
    groups = (("pr", bundle.get("prs") or [], lambda r: "pr-{}".format(r.get("number"))),
              ("issue", bundle.get("issues") or [],
               lambda r: "issue-{}".format(r.get("number"))),
              ("commit", bundle.get("commits") or [], lambda r: r.get("sha")))
    for kind, records, local_of in groups:
        for rec in records:
            if not isinstance(rec, dict):
                continue
            local = local_of(rec)
            qid = graphstore.qualify_id(project, repo, local)
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (qid,)).fetchone()
            if row is None:
                details.append({"severity": ERROR, "kind": "record", "id": qid,
                                "problem": "{} in --bundle but not stored".format(kind)})
                continue
            stored = _comparable_record(_node(row)["data"])
            rec = _comparable_record(rec)
            fields = sorted(
                k for k in set(stored) | set(rec)
                if stored.get(k) != rec.get(k)
                and not (k in _FOLD_ADDED_RECORD_KEYS and k not in rec))
            if fields:
                details.append({"severity": ERROR, "kind": "record", "id": qid,
                                "problem": "stored {} differs from --bundle".format(kind),
                                "fields": fields})
    return details


def _derive_artifacts(conn, project, repo, bundle):
    """Re-derive the artifact projection exactly as the write path does (#76):
    derive.artifact_projection over the repo's WHOLE stored ledger, plus any
    events of `bundle` the ledger does not hold yet (so an external bundle is
    checked against "the store after folding it", and a store holding more
    windows than the bundle is not drift). A self-sourced bundle IS the ledger,
    so for it this is the ledger alone. Symbol moves are the ones fold recorded
    (matched per window; the ledger has no window boundaries to re-match them)."""
    code, symbols = extract.ledger_events(conn, project, repo)
    events = derive.merge_ledger_events(
        {"code_events": code, "symbol_events": symbols}, bundle)
    # Same commit times the writer used (the store's), plus the bundle's own for
    # commits the store does not hold yet.
    commits = list(bundle.get("commits") or []) \
        + extract.commit_times(conn, project, repo)
    return derive.artifact_projection(
        {**events, "meta": bundle.get("meta", {}), "commits": commits},
        extract.recorded_moves(conn, project, repo))


def _people_mode(conn, project, repo, external):
    """The PEOPLE_* comparison mode for a single-repo no_drift (see above). An
    external --bundle is exact on a one-window single-repo store (it is then
    the whole store) and a subset check otherwise (the store holds people the
    bundle never saw)."""
    multi = _repo_window_count(conn, project, repo) > 1
    if len(graphstore.project_repos(conn, project)) > 1 or (external and multi):
        return PEOPLE_MEMBER
    return PEOPLE_WINDOW if multi else PEOPLE_EXACT


def check_no_drift(conn, project, repo, bundle, check_people=True,
                   external=False):
    """Store-derived == link-derived (the carol-check generalized to ANY data).

    Re-derive people and artifacts from the bundle (running attach_commit_prs
    first, exactly as the write path does) and assert the STORED person/artifact
    nodes equal the freshly derived set — same ids, same key fields. Any
    difference = ERROR with the diff.

    People are re-derived from the records alone (never seeded with the stored
    person nodes) and compare modules, areas, is_bot and login_resolved. How
    strictly (_people_mode): exact on a one-window single-repo store; on a
    multi-window store the areas compare as derived ⊆ stored (extra -> WARN);
    when `repo` is one member of a multi-member project, or `bundle` is an
    external --bundle on a multi-window store, only the people this bundle
    derives are checked (derived ⊆ stored) — people are project-wide and
    their full equality is check_no_drift_people's (validate_project). A stored person that is not
    derivable carries a #78 hint when it is a pre-#78 git-name identity.

    Artifacts are re-derived from the store's OWN ledger plus recorded moves
    (#76), so this proves the nodes equal their ledger, not that the ledger is
    right. With an `external` bundle the stored PR / issue / commit records must
    also equal the bundle's (_record_drift_details): the only way to detect a
    deleted record or a rewritten blob.

    `check_people=False` skips the people section (validate_project audits
    them once project-wide)."""
    details = []

    # --- people (skipped per-member in a project: audited once by
    #     check_no_drift_people over the cross-member union) ---
    if check_people:
        want_norm = _norm_people(_derive_people(bundle))
        stored_people = _stored_project_people(conn, project)
        mode = _people_mode(conn, project, repo, external)
        details.extend(_people_drift_details(stored_people, want_norm, mode,
                                             conn, project))
        if mode == PEOPLE_MEMBER:
            details.append({"severity": INFO, "note": (
                "people: the {} derived from this {} are stored with at least "
                "these areas; the full people equality is checked project-wide "
                "(validate.py without --repo)".format(
                    len(want_norm), "--bundle" if external else "member"))})

    if external:
        details.extend(_record_drift_details(conn, project, repo, bundle))

    # --- artifacts ---
    # The stored artifact id is `{project}/{repo}#{local}` where the SYMBOL-artifact
    # local is itself a double-`#` form `{path}#{lang}:{subkind}:{name}`. Strip
    # ONLY the repo prefix (graphstore.local_id) to recover the true local id
    # (mirroring how fold_bundle / extract reconstruct it).
    want_arts = _derive_artifacts(conn, project, repo, bundle)
    stored_arts = {}
    # Every non-commit code node is audited as an artifact (#101: classified by
    # its record, not its id shape; check_no_fabrication rejects any code node
    # whose id form disagrees with its record).
    for n in graphstore.repo_nodes(conn, project, repo, "code"):
        local = graphstore.local_id(n["id"], project, repo)
        if graphstore.is_artifact_node(n):
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

    if not any(d["severity"] == ERROR for d in details):
        details.append({"severity": INFO, "note": "store matches re-derived people+artifacts"})
    return _check("no_drift", ERROR, details)


# --- check 8: idempotency probe ----------------------------------------------

def _counts(conn):
    return {
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "code_events": conn.execute("SELECT COUNT(*) FROM code_events").fetchone()[0],
    }


def _edge_keys(conn):
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT src_id, dst_id, edge_type FROM edges")}


def check_idempotency(conn, bundle, project=None, repo=None):
    """Re-fold the bundle into a SECONDARY in-memory copy of the store and assert
    node/edge/code_event counts are unchanged (durable dedup holds).

    Read-only over the audited store: we copy it into memory, re-fold there, and
    compare counts. A count that grows means a fold path is non-idempotent (it
    would silently inflate the graph on every overlapping window).

    Multi-window store (`project`/`repo` given and more than one gathered
    window recorded for it): the self-sourced bundle holds every window as one,
    so the re-fold can match a symbol move no single window had (a drop in one
    window, a unique add in a later one) and derive current-state edges
    (touches/owns/depends_on/...) for older records against the latest
    window's code graph. Edges are keyed by (src, dst, type), so such new edges are a
    different derivation scope, never a duplicate: they are listed as INFO and
    left out of the edge count (issue #89, LLD §14.5). A re-fold that
    RETRACTS current-state edges (issue #95) of a repo last folded before #95
    is healing stale edges that fold left behind: a WARN, left out of the
    count; on a repo folded since #95 it is a failure (the store was not what
    its inputs imply). On a one-window store every change still fails.
    """
    multi = project is not None and _repo_window_count(conn, project, repo) > 1
    before = _counts(conn)
    keys_before = _edge_keys(conn) if multi else None
    copy_conn = graphstore.open_store(":memory:")
    conn.backup(copy_conn)
    summary = gather.fold_bundle(copy_conn, bundle) or {}
    after = _counts(copy_conn)
    excused, healed = [], []
    if multi:
        keys_after = _edge_keys(copy_conn)
        excused = sorted(
            k for k in keys_after - keys_before
            if k[2] in _MOVE_EDGE_TYPES or k[2] in _CURRENT_STATE_EDGE_TYPES)
        after["edges"] -= len(excused)
        if graphstore.get_current_state(conn, project, repo) is None:
            healed = sorted(k for k in keys_before - keys_after
                            if k[2] in _CURRENT_STATE_EDGE_TYPES)
            after["edges"] += len(healed)
            # the stand-ins those edges alone anchored go with them
            after["nodes"] += len(summary.get("standins_removed") or [])
    copy_conn.close()
    details = []
    if excused:
        details.append({
            "severity": INFO,
            "note": "{} edge(s) a whole-store re-fold derives that no single "
                    "window did (cross-window move re-match / current-state "
                    "input on a multi-window store): not a dedup failure".format(
                        len(excused)),
            "edges": [list(k) for k in excused[:20]]})
    if healed:
        details.append({
            "severity": WARN,
            "note": "{} stale current-state edge(s) a re-fold retracts: {}"
                    .format(len(healed), _PRE95_REASON),
            "edges": [list(k) for k in healed[:20]]})
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

def _store_window(conn, project, repo):
    """The full activity window of a store: (min_ts, max_ts) over project/repo
    nodes that carry a timestamp (structure nodes are NULL-ts and excluded). The
    span is used to self-source the raw bundle via extract so the trust gate is
    fully self-contained on a store (no external bundle file needed)."""
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM nodes "
        "WHERE project=? AND repo=? AND ts IS NOT NULL",
        (project, repo)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _gathered_window(conn, project, repo):
    """(min from, max to) over the gathered windows recorded for project/repo
    in the meta ledger, or (None, None) when none is recorded."""
    try:
        wins = [w for w in graphstore.get_windows(conn)
                if isinstance(w, dict) and w.get("project") == project
                and w.get("repo") == repo and w.get("from") and w.get("to")]
    except ValueError:
        return None, None
    if not wins:
        return None, None
    return min(w["from"] for w in wins), max(w["to"] for w in wins)


class _NotSourced(Exception):
    """A bundle could not be self-sourced; the message is the reason."""


def _not_sourced(name, reason):
    return _check(name, ERROR, [{
        "severity": ERROR, "problem": "could not self-source: {}".format(reason)}])


def _self_source_bundle(conn, project, repo):
    """Reconstruct the raw bundle FROM THE STORE via extract, over the store's
    full activity window. In a store-only world (Phase 7) there is no bundle
    FILE; the bundle is the transient view extract materializes. Running it here
    lets no_drift/idempotency audit the store with no external input.

    Returns (bundle, None), or (None, reason) when no bundle can be sourced;
    the caller then FAILS no_drift/idempotency with that reason (issue #79:
    they used to be silently dropped, so the audit passed on nothing):
      - the (project, repo) has no stored nodes;
      - a stored node cannot be decoded (CorruptRow, naming it);
      - social/code nodes are stored but none carries a timestamp, so the
        activity window cannot be recovered;
      - extract itself raises.
    A member holding only structure nodes (a quiet member: nothing happened in
    its window) is legitimate; it self-sources over its recorded gathered
    window instead (and fails when none is recorded)."""
    problem = _target_problem(conn, project, repo)
    if problem is not None:
        return None, "{} (project={}, repo={})".format(
            problem["problem"], project, repo)
    try:
        nodes = _repo_nodes(conn, project, repo)
    except CorruptRow as exc:
        return None, str(exc)
    ts_from, ts_to = _store_window(conn, project, repo)
    if ts_from is None or ts_to is None:
        activity = sum(1 for n in nodes if n["node_class"] != "structure")
        if activity:
            return None, ("{} social/code node(s) stored but none carries a "
                          "timestamp, so the activity window cannot be "
                          "recovered".format(activity))
        ts_from, ts_to = _gathered_window(conn, project, repo)
        if ts_from is None:
            return None, ("no timestamped node and no recorded gathered "
                          "window for this repo")
    # extract materializes only RAW keys; that is exactly what no_drift /
    # idempotency re-derive against (they run derive.* themselves).
    try:
        return extract.extract(conn, project, repo, ts_from, ts_to,
                               warn=lambda _msg: None), None
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        return None, "extract failed: {}: {}".format(type(exc).__name__, exc)


def validate_repo(conn, project, repo, bundle=None, check_people=True,
                  scope=None):
    """Run all trustworthiness checks for a single (project, repo) and return a
    Report. The two real-data checks (no_drift, idempotency) need a raw bundle
    to re-derive against; when `bundle` is not passed they SELF-SOURCE it from
    the store (via extract over the store's full window) so the gate is
    self-contained on a store-only deliverable. A passed `bundle` is honored as
    an optional cross-check but is never required.

    `check_people=False` (set by validate_project) drops no_drift's people section
    here: project-scoped people are audited once project-wide by
    check_no_drift_people, not per member.

    The row-level checks (referential_integrity, provenance, no_fabrication,
    schema_conformance) audit `scope` (_Scope, issue #107): by default this
    repo's own rows plus the shared rows and store-wide parts, so another
    stored member's errors are not reported here. validate_project passes
    the member's own rows only and audits the shared part once.
    """
    if scope is None:
        scope = _Scope.single_repo(conn, project, repo)
    # Every check is _guarded: the auditor must never crash on a bad store. A
    # check that raises (a corrupt row, a re-derive/re-fold failure) is a failed
    # ERROR check naming the error, and every check is always listed (#79).
    checks = [
        _guarded(check_target, "target", conn, project, repo),
        _guarded(check_referential_integrity, "referential_integrity",
                 conn, scope),
        _guarded(check_participant_completeness, "participant_completeness",
                 conn, project, repo, bundle),
        _guarded(check_provenance, "provenance", conn, scope),
        _guarded(check_no_fabrication, "no_fabrication", conn, scope),
        _guarded(check_schema_conformance, "schema_conformance", conn, scope),
    ]
    # edge_set always audits the store against ITS OWN records (self-sourced),
    # also when an external bundle is passed: the bundle's job is to vouch for
    # the records themselves (no_drift's record cross-check).
    self_bundle, self_reason = _self_source_bundle(conn, project, repo)
    if self_bundle is None:
        checks.append(_not_sourced("edge_set", self_reason))
    else:
        checks.append(_guarded(check_edge_set, "edge_set",
                               conn, project, repo, self_bundle))
    if bundle is not None:
        drift_bundle, reason = bundle, None
    else:
        drift_bundle, reason = self_bundle, self_reason
    if drift_bundle is None:
        # No bundle to re-derive against: these checks FAIL, never vanish.
        checks.append(_not_sourced("no_drift", reason))
        checks.append(_not_sourced("idempotency", reason))
    else:
        checks.append(_guarded(check_no_drift, "no_drift",
                               conn, project, repo, drift_bundle, check_people,
                               bundle is not None))
        checks.append(_guarded(check_idempotency, "idempotency",
                               conn, drift_bundle, project, repo))
    return Report(checks)


def validate(conn, project=None, repo=None, bundle=None):
    """Audit a populated store; return a Report.

    project/repo are derived from the store (`nodes`) when not passed, so this
    works over a real gathered store. The two real-data checks (no_drift,
    idempotency) need a raw bundle to re-derive against; when `bundle` is not
    passed they SELF-SOURCE it from the store (via extract over the store's full
    window) so the gate is self-contained on a store-only deliverable. A passed
    `bundle` is honored as an optional cross-check but is never required.

    An unknown project/repo or an empty store is a failed `target` check (with
    no_drift/idempotency failed as "could not self-source"), not a pass; only
    an ambiguous store (several projects or repos, none named) raises
    ValueError.
    """
    project, repo = _resolve_target(conn, project, repo)
    return validate_repo(conn, project, repo, bundle)


def validate_project(conn, project, repos):
    """Run the per-member validation over a project's full member set and
    aggregate. Returns {"ok": all-green, "project": project,
    "members": [{"repo": r, "ok": bool, ...per-repo report...}, ...]}.

    Raises ValueError on an empty `repos`: validation ATTESTS trustworthiness,
    and `all([])` would vacuously report ok=True for zero repos, masking a
    misconfigured member set. (digest.build_project_view, which only DESCRIBES,
    tolerates an empty project and returns an empty view.) The one exception is
    a project whose members all failed to gather (issue #77): with member run
    status recorded and no repo holding data, the report has no members and
    fails on `member_status`."""
    if not repos and not _has_member_status(conn, project):
        raise ValueError("validate_project: no repos to validate")
    member_reports = []
    for repo in repos:
        # check_people=False: people are project-scoped; audited once below.
        # The row-level checks audit the member's own rows only (#107).
        rep = validate_repo(conn, project, repo, check_people=False,
                            scope=_Scope(project, repos, repo, False))
        member_reports.append({"repo": repo, **rep.to_dict()})
    # PROJECT-WIDE checks (the ones whose unit is the project, not a member repo):
    # people drift over the cross-member union; the row-level checks over the
    # rows no member owns (people, non-member rows) and the store-wide parts,
    # once (#107); the manifest members' run status.
    project_checks = []
    if repos:
        project_checks.append(_guarded(check_no_drift_people, "no_drift_people",
                                       conn, project, repos))
    else:
        # Nothing to audit is never a pass, whatever member_status says (its
        # entries can all read `ok` while the members' nodes are gone).
        project_checks.append(_check("target", ERROR, [{
            "severity": ERROR, "project": project,
            "problem": "project has no member repo with stored nodes"}]))
    shared = _Scope(project, repos, None, True)
    project_checks.extend([
        _guarded(check_referential_integrity, "referential_integrity",
                 conn, shared),
        _guarded(check_provenance, "provenance", conn, shared),
        _guarded(check_no_fabrication, "no_fabrication", conn, shared),
        _guarded(check_schema_conformance, "schema_conformance", conn, shared),
    ])
    project_checks.append(_guarded(check_member_status, "member_status",
                                   conn, project))
    ok = (all(r["ok"] for r in member_reports)
          and all(c["ok"] for c in project_checks))
    return {"ok": ok, "project": project, "members": member_reports,
            "project_checks": project_checks}


def _has_member_status(conn, project):
    """True when a manifest gather recorded member run status for `project`
    (also when that record is unreadable: check_member_status reports it)."""
    try:
        return bool(graphstore.get_member_status(conn, project))
    except (ValueError, TypeError, KeyError, AttributeError):
        return True


def _guarded(fn, name, *args):
    """Run a real-data check, converting an exception (corrupt store breaking the
    re-derive/re-fold) into a failed ERROR check rather than crashing the audit."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - auditor must survive a bad store
        detail = {"severity": ERROR,
                  "problem": "check could not run on this store",
                  "error": "{}: {}".format(type(exc).__name__, exc)}
        if isinstance(exc, CorruptRow):
            detail["row"] = exc.row
        return _check(name, ERROR, [detail])


# --- CLI ----------------------------------------------------------------------

def _format_checks_lines(checks, indent=""):
    """Return a list of lines rendering `checks` (a list of check dicts) using
    the canonical per-check format.  `indent` is prepended to every line so the
    same helper can be used at top-level (empty indent) or nested (e.g. "  ")."""
    lines = []
    for c in checks:
        mark = "ok  " if c["ok"] else "FAIL"
        lines.append("{}[{}] {}  ({})".format(indent, mark, c["name"], c["severity"]))
        for d in c["details"]:
            sev = d.get("severity", c["severity"])
            if c["ok"] and sev == INFO:
                # show only a brief note for passing checks
                note = d.get("note") or d.get("summary")
                if note:
                    lines.append("{}      - {}".format(indent, note))
                continue
            lines.append("{}      - [{}] {}".format(
                indent, sev, json.dumps({k: v for k, v in d.items()
                                         if k != "severity"}, sort_keys=True)))
    return lines


def _format_report(report, project, repo):
    lines = []
    status = "OK" if report.ok else "FAIL"
    lines.append("trust audit: {}  (project={} repo={})".format(status, project, repo))
    lines.append("=" * 60)
    lines.extend(_format_checks_lines(report.checks))
    lines.append("=" * 60)
    lines.append("RESULT: {}".format(status))
    return "\n".join(lines)


def _usage_error(msg):
    sys.stderr.write("validate.py: error: {}\n".format(msg))
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit a journey-graph store for trustworthiness.")
    parser.add_argument("store", help="path to the STORE.db SQLite store")
    parser.add_argument("--project", default=None,
                        help="project (auto-detected from the store if omitted)")
    parser.add_argument("--repo", default=None,
                        help="repo (auto-detected from the store if omitted)")
    parser.add_argument("--bundle", default=None,
                        help="optional raw bundle JSON cross-check for no_drift + "
                             "idempotency (unnecessary: they self-source from the store)")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Read-only (issue #79): never create a missing store, never write to it.
    try:
        conn = graphstore.open_store_readonly(args.store)
    except FileNotFoundError:
        return _usage_error("store {!r} does not exist".format(args.store))
    except sqlite3.Error as exc:
        return _usage_error("cannot open store {!r}: {}".format(args.store, exc))
    try:
        conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        conn.close()
        return _usage_error("{!r} is not a journey-graph store ({})".format(
            args.store, exc))
    bundle = None
    if args.bundle:
        try:
            with open(args.bundle) as fh:
                bundle = json.load(fh)
        except (OSError, ValueError) as exc:
            conn.close()
            return _usage_error("cannot read --bundle {!r}: {}".format(
                args.bundle, exc))

    # Resolve project/repo; allow_multi=True so multi-repo stores don't raise
    # when --repo is omitted (the project path handles them). An unknown or
    # empty target comes back unresolved and FAILS the `target` check.
    try:
        project, repo = _resolve_target(conn, args.project, args.repo,
                                        allow_multi=True)
    except ValueError as exc:
        conn.close()
        return _usage_error(str(exc))

    # A manifest store (member run status recorded) always takes the project
    # path when no --repo is named, even when only one member holds data: the
    # others may have failed (issue #77) and must not drop out of the audit.
    if args.repo is None and project is not None and (
            len(graphstore.project_repos(conn, project)) > 1
            or _has_member_status(conn, project)):
        # Multi-repo project path: run validate_project over all member repos.
        if args.bundle:
            sys.stderr.write("warning: --bundle is ignored in multi-repo "
                             "project validation (it is single-repo by nature)\n")
        repos = graphstore.project_repos(conn, project)
        agg = validate_project(conn, project, repos)
        conn.close()
        if args.json:
            print(json.dumps(agg, sort_keys=True, indent=2))
        else:
            status = "OK" if agg["ok"] else "FAIL"
            print("trust audit (project): {}  (project={} repos={})".format(
                status, project, repos))
            print("=" * 60)
            for mr in agg["members"]:
                mstatus = "OK" if mr["ok"] else "FAIL"
                print("  repo: {}  [{}]".format(mr["repo"], mstatus))
                for line in _format_checks_lines(mr.get("checks", []), indent="    "):
                    print(line)
            if agg.get("project_checks"):
                print("  project-wide:")
                for line in _format_checks_lines(agg["project_checks"], indent="    "):
                    print(line)
            print("=" * 60)
            print("RESULT: {}".format(status))
        return 0 if agg["ok"] else 1
    else:
        # Single-repo path: identical to the original behaviour.
        report = validate_repo(conn, project, repo, bundle)
        conn.close()
        if args.json:
            print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
        else:
            print(_format_report(report, project, repo))
        return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
