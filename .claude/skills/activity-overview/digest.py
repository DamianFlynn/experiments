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
import os
import re
import sqlite3
import sys

import derive
import extract as extract_mod
import graphstore
import link as link_mod
import manifest as manifest_mod


# Internal-ticket clustering is OPT-IN (issue #74). There is no default pattern:
# a generic "2+ capitals, hyphen, digits" rule matched UTF-8, SHA-256, HTTP-200,
# ISO-8601 and CVE-2024 and glued unrelated trains together in `related_work`.
# A pattern comes from `--ticket-pattern RE`, or from the manifest's declared
# `ticket_prefixes` via ticket_pattern_from_prefixes.


def ticket_pattern_from_prefixes(prefixes):
    """Compiled `\\b(?:P1|P2)-\\d+\\b` for declared ticket prefixes (e.g. the
    manifest's `ticket_prefixes`), or None when there are none (clustering off).
    Prefixes keep their declared order; matching is case-sensitive."""
    if not prefixes:
        return None
    return re.compile(r"\b(?:{})-\d+\b".format(
        "|".join(re.escape(p) for p in prefixes)))


def parse_ticket_refs(text, pattern=None):
    """Ordered, deduped internal-ticket ids in `text`. Pure. `pattern` None means
    ticket parsing is off (returns []). Uses capture group 1
    when the pattern has one AND it participated in the match, else the whole
    match — so an optional group that didn't fire never yields a `None` token."""
    if pattern is None:
        return []
    out, seen = [], set()
    for m in pattern.finditer(text or ""):
        tok = m.group(1) if (m.re.groups and m.group(1) is not None) else m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def group_related_work(trains):
    """Cluster project trains that share a ticket but are otherwise unlinked.
    Returns [{"ticket": str, "train_ids": [...]}, ...] for tickets spanning >=2
    trains, ordered by ticket. Each train carries a `tickets` list (set by
    build_project_view)."""
    by_ticket = {}
    for tr in trains:
        for tk in tr.get("tickets", []):
            by_ticket.setdefault(tk, [])
            if tr["id"] not in by_ticket[tk]:
                by_ticket[tk].append(tr["id"])
    return [{"ticket": tk, "train_ids": sorted(ids)}
            for tk, ids in sorted(by_ticket.items()) if len(ids) >= 2]


def member_bundles(conn, project, repos, ts_from, ts_to, *, backfill=None,
                   backfill_budget=50, warn=None, ref_date=None):
    """Materialize + enrich one bundle per member repo, in `repos` order.
    Each is the single-repo enriched bundle for that member, produced via
    the unmodified extract -> link.enrich path, so each carries the window
    (`meta.period`, buckets limited to it) and `meta.ref_date` (issue #75).
    `ref_date` overrides every member's reference date; None lets extract use
    the value stored for the window, else `ts_to`.

    `warn` is an optional callable(str) forwarded to extract for spine
    backfill-budget / missing-context diagnostics; the default (None) lets
    extract emit them to stderr, so an incomplete project digest is visible
    without corrupting the JSON on stdout.
    Each member's `meta` is extended (append-only) by `_member_identity`.
    Returns [{"repo": "owner/repo", "bundle": <enriched dict>}, ...]."""
    out = []
    for repo in repos:
        bundle = extract_mod.extract(
            conn, project, repo, ts_from, ts_to,
            backfill=backfill, backfill_budget=backfill_budget,
            warn=warn, ref_date=ref_date)
        bundle = link_mod.enrich(bundle)
        bundle["meta"].update(_member_identity(project, repo))
        out.append({"repo": repo, "bundle": bundle})
    return out


def _member_identity(project, repo):
    """Append-only identity keys for a member bundle's `meta` (issue #74).

    extract's `meta.owner` is the store's PROJECT scope and `meta.repo` the
    store's repo key — for a manifest member that is `owner=<project>`,
    `repo="Azure/name"`, so `{owner}/{repo}` is not a GitHub slug. Those two keys
    are left as they are; these are added:
      project     the project scope (== meta.owner)
      slug        the GitHub `owner/name`
      repo_owner  the GitHub owner (user/org)
      repo_name   the bare GitHub repository name
    A manifest member's repo key is already `owner/name`. A single-repo store is
    keyed project=<owner>, repo=<name> (gather.py --owner/--repo), so its slug is
    `{project}/{repo}`."""
    if "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)
    else:
        repo_owner, repo_name = project, repo
    return {"project": project, "slug": "{}/{}".format(repo_owner, repo_name),
            "repo_owner": repo_owner, "repo_name": repo_name}


def spine_components(conn, project, repos, ts_from, ts_to, max_depth=6):
    """Connected components of the project-wide spine, seeded by in-window social
    nodes across `repos`. Each returned frozenset of qualified node ids is one
    decision train (possibly spanning members — the store's cross-repo
    closes/cross_ref edges join them). Deterministic: components are ordered by
    their lexicographically smallest member id.

    Three behaviours to know:
    - Reachability is capped at `max_depth` hops (matching extract), so a single
      real train spanning more than `max_depth` spine hops can split into two
      components.
    - traverse_spine has no window filter, so a component MAY include out-of-window
      social anchors reachable from an in-window seed (cross-window train context).
    - Each frozenset holds only PR/issue SOCIAL anchors; commits/areas traversed
      bridge the component but are dropped from the returned set (trains are keyed
      off social anchors)."""
    in_window = graphstore.range_query(conn, project, repos, ts_from, ts_to)
    # Only pr/issue ANCHORS seed components. Phase 10's review/event social leaves
    # are train members reached from their parent anchor, not seeds — seeding from
    # them would leak a review/event id into a component (or orphan one whose
    # parent is out of window), which build_project_trains can't key off.
    socials = [n["id"] for n in in_window if n["node_class"] == "social"
               and graphstore.parse_id(n["id"])["local"].startswith(("pr-", "issue-"))]
    seen, comps = set(), []
    for sid in socials:
        if sid in seen:
            continue
        reached = graphstore.traverse_spine(
            conn, [sid], max_depth=max_depth, skip_dead=True)["reached"]
        # keep reached SOCIAL anchors (issues/prs); commits/areas reached are train
        # members but trains are keyed off social anchors.
        comp = {nid for nid in reached
                if graphstore.parse_id(nid)["local"].startswith(("pr-", "issue-"))}
        comp.add(sid)
        # only social anchors can be seeds (the loop iterates `socials`), so
        # tracking anchors in `seen` is enough to keep components disjoint.
        seen |= comp
        comps.append(frozenset(comp))
    comps.sort(key=lambda c: min(c))
    return comps


def window_components(conn, project, members, components):
    """`components` without the stored pr/issue anchors of a member repo that
    are NOT in that member's view (issue #85).

    traverse_spine has no window filter, so on a store holding several
    windows a component can reach another window's PRs/issues, and
    build_project_trains would fold them into this window's project trains as
    orphan refs. A member's view (extract) already decides which of its
    stored records belong to the window; this applies the same decision to
    the components. Ids of repos that are not members, and ids the store does
    not hold (referenced, never gathered), are kept as before. On a store
    holding only the viewed window every stored anchor is in its member's
    view, so nothing is removed."""
    out_of_view = set()
    for m in members:
        repo, bundle = m["repo"], m["bundle"]
        in_view = {graphstore.qualify_id(project, repo, "pr-{}".format(p["number"]))
                   for p in bundle.get("prs", [])}
        in_view |= {graphstore.qualify_id(project, repo,
                                          "issue-{}".format(i["number"]))
                    for i in bundle.get("issues", [])}
        for n in graphstore.repo_nodes(conn, project, repo, node_class="social"):
            local = graphstore.parse_id(n["id"])["local"]
            if local.startswith(("pr-", "issue-")) and n["id"] not in in_view:
                out_of_view.add(n["id"])
    if not out_of_view:
        return components
    return [frozenset(c - out_of_view) for c in components]


# outcome precedence when member trains merge into one project train (mirrors
# link.build_trains' shipped > in_flight > rejected > abandoned).
_OUTCOME_RANK = {"shipped": 3, "in_flight": 2, "rejected": 1, "abandoned": 0}


def _member_train_anchor_qid(project, repo, train):
    """The qualified spine anchor of a member train: its root issue, else its
    first PR — exactly link.build_trains' anchor, qualified to `repo`."""
    if train.get("root_issue") is not None:
        local = "issue-{}".format(train["root_issue"])
    else:
        local = "pr-{}".format(train["prs"][0])
    return graphstore.qualify_id(project, repo, local)


def build_project_trains(members, components, project):
    """Stitch the per-member trains into project trains along the spine.

    `members` is member_bundles' output; `components` is spine_components' output.
    Each member train maps to the component containing its anchor; member trains
    sharing a component merge into ONE project train with qualified, repo-spanning
    references. Trains whose anchor isn't in any component form their own singleton
    project train. Deterministic order by project-train id.

    Components may include social nodes (issues/PRs) that have no corresponding
    member train (e.g. a cross-repo issue that was closed without a local PR).
    These nodes are folded into the project train that owns the component so that
    cross-repo references and repos are fully represented. A component all of whose
    social nodes are orphans (no member train maps to it) produces no project train —
    mirroring single-repo behaviour, where a closed issue with no PR and no
    `not_planned` reason yields no train.

    Two consequences of the orphan fold worth knowing:
    - Because `spine_components` caps reachability at `max_depth`, a single real
      train spanning more than that many spine hops can split into two components;
      an intermediate anchor shared by both may then surface in two project trains
      (once as a member-train ref, once orphan-folded). Rare (needs a >max_depth
      chain) and inherited from `extract`'s single-repo cap.
    - A project train's `repos`/refs reflect its whole component, so when the caller
      restricts `members`/`repos` to a subset, a spine edge reaching a member
      OUTSIDE the requested subset still pulls that member's node in (the edge is
      real). The emitted `repos` can thus exceed the requested set."""
    comp_of = {}
    for i, comp in enumerate(components):
        for nid in comp:
            comp_of[nid] = i

    groups = {}
    for m in members:
        repo, bundle = m["repo"], m["bundle"]
        for tr in bundle.get("trains", []):
            anchor = _member_train_anchor_qid(project, repo, tr)
            key = ("comp", comp_of[anchor]) if anchor in comp_of else ("solo", anchor)
            groups.setdefault(key, []).append((repo, tr))

    out = []
    for key, items in groups.items():
        prs, issues, commits, evidence, repos = [], [], [], [], []
        outcome = "abandoned"
        best_rank = -1
        for repo, tr in items:
            if repo not in repos:
                repos.append(repo)
            for n in tr["prs"]:
                prs.append(graphstore.qualify_id(project, repo, "pr-{}".format(n)))
            if tr.get("root_issue") is not None:
                issues.append(graphstore.qualify_id(
                    project, repo, "issue-{}".format(tr["root_issue"])))
            for sha in tr.get("commits", []):
                commits.append(graphstore.qualify_id(project, repo, sha))
            for ev in tr.get("evidence", []):
                evidence.append({**ev, "repo": repo})
            rank = _OUTCOME_RANK.get(tr["outcome"], 0)
            if rank > best_rank:
                best_rank, outcome = rank, tr["outcome"]
        # kind: prefer a typed root-issue train; among candidates pick the one with
        # the min anchor — consistent with the train-id derivation and the outcome
        # ordering (vs. first-by-repo-order, which depended on member iteration).
        root_items = [(r, tr) for r, tr in items if tr.get("root_issue") is not None]
        _, kpick = min(root_items or items,
                       key=lambda rt: _member_train_anchor_qid(project, rt[0], rt[1]))
        kind = kpick["kind"]

        # Fold in any component nodes that have no member train (e.g. a cross-repo
        # issue closed without a local PR).  These contribute repos + refs but no
        # outcome/kind signal — they are already accounted for by the spine edge that
        # linked them to a train-bearing anchor.
        if key[0] == "comp":
            comp_idx = key[1]
            train_anchors = set(prs) | set(issues)
            for nid in components[comp_idx]:
                if nid in train_anchors:
                    continue
                parsed = graphstore.parse_id(nid)
                scope = parsed["scope"]   # "{project}/{repo}"
                local = parsed["local"]   # "pr-N" or "issue-N"
                # scope is "{project}/{owner}/{repo}"; strip the exact "{project}/"
                # prefix (we hold `project`) so the owner/repo slug is recovered
                # correctly even if a project name ever contained a slash —
                # independent of the manifest slash-free guard.
                repo_part = scope[len(project) + 1:]
                if local.startswith("issue-"):
                    issues.append(nid)
                    if repo_part not in repos:
                        repos.append(repo_part)
                elif local.startswith("pr-"):
                    prs.append(nid)
                    if repo_part not in repos:
                        repos.append(repo_part)

        # the project-train id is derived from the merged, fully-qualified
        # reference set -> deterministic and globally unique across repos.
        if not (prs or issues):
            raise ValueError("project train must reference at least one pr/issue")
        tid = "ptrain-" + min(prs + issues)
        out.append({
            "id": tid,
            "kind": kind,
            "outcome": outcome,
            "repos": sorted(repos),
            "prs": sorted(set(prs)),
            "issues": sorted(set(issues)),
            "commits": sorted(set(commits)),
            "evidence": evidence,
            "code_areas": [],
        })
    out.sort(key=lambda t: t["id"])
    return out


def _attach_tickets(members, project_trains, project, ticket_pattern):
    """Set each project train's `tickets` from the titles + bodies of its member
    PRs/issues. Builds a qualified-id -> record index from the member bundles so
    a train's qualified refs resolve to their text."""
    rec = {}
    for m in members:
        repo, b = m["repo"], m["bundle"]
        for p in b.get("prs", []):
            rec[graphstore.qualify_id(project, repo, "pr-{}".format(p["number"]))] = p
        for i in b.get("issues", []):
            rec[graphstore.qualify_id(project, repo, "issue-{}".format(i["number"]))] = i
    for tr in project_trains:
        seen, tickets = set(), []
        for qid in tr["prs"] + tr["issues"]:
            r = rec.get(qid) or {}
            text = "{}\n{}".format(r.get("title") or "", r.get("body") or "")
            for tk in parse_ticket_refs(text, ticket_pattern):
                if tk not in seen:
                    seen.add(tk)
                    tickets.append(tk)
        tr["tickets"] = tickets


def _merge_shipped(members):
    """Project-wide Shipped: each member's buckets.shipped rows, tagged by repo."""
    out = []
    for m in members:
        for row in (m["bundle"].get("buckets", {}) or {}).get("shipped", []):
            out.append({**row, "repo": m["repo"]})
    return out


def _merge_people(members):
    """Project-wide people (issue #88): the union of the member views' people,
    keyed by login, with each figure combined across members.

    A member view holds only the people with a contribution in that member
    (extract._view_people) and its profile counts only that member's records,
    so a project figure is a combination of disjoint parts — nobody is counted
    twice:
      counts (derive.PROFILE_COUNT_FIELDS)  summed over members
      merge_rate            prs_merged / prs_authored of the sums (3 dp)
      review_latency_days   median of every member's latency samples
                            (derive.review_latency_samples over the member's
                            PRs), not a median of medians
      first_seen / last_active   earliest / latest over members
      modules / areas       "{repo}::{area}", like the project `modules`
                            keys, so two members' same-named areas stay apart
      is_bot                OR; login_resolved false when any member says so
    Any other field is kept from the first member's record. Member records are
    not mutated. Order: first appearance over members in `members` order."""
    people, samples = {}, {}
    for m in members:
        repo, bundle = m["repo"], m["bundle"]
        latencies = derive.review_latency_samples(bundle.get("prs") or [])
        for login, rec in (bundle.get("people", {}) or {}).items():
            if login not in people:
                cur = dict(rec)  # copy: preserve all fields, don't alias
                cur["modules"], cur["areas"], cur["is_bot"] = [], [], False
                for field in derive.PROFILE_COUNT_FIELDS:
                    if field in cur:
                        cur[field] = 0
                for field in ("first_seen", "last_active"):
                    if field in cur:
                        cur[field] = None
                people[login] = cur
            cur = people[login]
            for field in ("modules", "areas"):
                cur[field] = sorted(set(cur[field]) | {
                    "{}::{}".format(repo, a) for a in rec.get(field) or []})
            for field in derive.PROFILE_COUNT_FIELDS:
                if isinstance(rec.get(field), int):
                    cur[field] = (cur.get(field) or 0) + rec[field]
            for field, pick in (("first_seen", min), ("last_active", max)):
                dates = [d for d in (cur.get(field), rec.get(field)) if d]
                if dates:
                    cur[field] = pick(dates)
            cur["is_bot"] = cur["is_bot"] or bool(rec.get("is_bot"))
            if rec.get("login_resolved") is False:
                cur["login_resolved"] = False
            samples.setdefault(login, []).extend(latencies.get(login, []))
    for login, cur in people.items():
        if "prs_authored" in cur:
            authored = cur["prs_authored"]
            cur["merge_rate"] = (round(cur.get("prs_merged", 0) / authored, 3)
                                 if authored else None)
        if "review_latency_days" in cur:
            cur["review_latency_days"] = derive.median_latency(samples[login])
    return people


def _merge_modules(members):
    """Project-wide modules, area ids repo-qualified ("{repo}::{area}") so two
    members' same-named areas never collide."""
    mods = {}
    for m in members:
        for area, stats in (m["bundle"].get("modules", {}) or {}).items():
            mods["{}::{}".format(m["repo"], area)] = stats
    return mods


def build_project_view(conn, project, repos, ts_from, ts_to, *, backfill=None,
                       backfill_budget=50, ticket_pattern=None,
                       warn=None, ref_date=None):
    """The merged project view consumed by report-template.md's narrative step.
    Keys: meta{project,repos,from,to}, members[{repo,bundle}] (per-member raw +
    enriched, for collision-prone per-file sections), trains (project-wide, each
    with a `tickets` list), related_work (ticket clusters), shipped (repo-tagged),
    people (merged by login, figures combined across members: _merge_people),
    halls (`fame` ranked on the merged people), modules (repo-qualified).

    `ticket_pattern` (a compiled regex) opts into ticket clustering; None (the
    default) leaves every train's `tickets` and `related_work` empty.

    `warn` is forwarded to member_bundles (extract diagnostics); default None
    lets extract surface incompleteness on stderr. `ref_date` is forwarded to
    member_bundles (override of each member's `meta.ref_date`)."""
    members = member_bundles(conn, project, repos, ts_from, ts_to,
                             backfill=backfill, backfill_budget=backfill_budget,
                             warn=warn, ref_date=ref_date)
    comps = window_components(
        conn, project, members,
        spine_components(conn, project, repos, ts_from, ts_to))
    trains = build_project_trains(members, comps, project)
    _attach_tickets(members, trains, project, ticket_pattern)
    related = group_related_work(trains)
    people = _merge_people(members)
    return {
        "meta": {"project": project, "repos": list(repos),
                 "from": ts_from, "to": ts_to,
                 "schema_version": graphstore.SCHEMA_VERSION},
        "members": members,
        "trains": trains,
        "related_work": related,
        "shipped": _merge_shipped(members),
        "people": people,
        "halls": derive.build_halls({"people": people})["halls"],
        "modules": _merge_modules(members),
        "module_edges": project_depends_on(conn, project, repos),
    }


def _area_of(qid):
    """('owner/repo', 'area-tail') from a qualified area id
    '{project}/{owner}/{repo}#area-<tail>'. Precondition: a repo-qualified id
    (raises on a project-scoped/person id with no '/' in its scope)."""
    parsed = graphstore.parse_id(qid)
    parts = parsed["scope"].split("/", 1)              # strip leading project/
    if len(parts) < 2:
        raise ValueError("_area_of: expected a repo-qualified id, got {!r}".format(qid))
    repo = parts[1]
    local = parsed["local"]
    area = local[len("area-"):] if local.startswith("area-") else local
    return repo, area


def project_depends_on(conn, project, repos):
    """The project's module-dependency edges as render-ready rows:
    [{src_repo, src_area, dst_repo, dst_area, version, transitive, cross_repo}, ...]
    sorted by (src_id, dst_id). Reads resolved depends_on edges from the store."""
    repo_set = set(repos)
    out = []
    for e in graphstore.edges_by_type(conn, "depends_on", project):
        src_repo, src_area = _area_of(e["src_id"])
        dst_repo, dst_area = _area_of(e["dst_id"])
        if src_repo not in repo_set:
            continue
        d = e["data"] or {}
        out.append({
            "src_repo": src_repo, "src_area": src_area,
            "dst_repo": dst_repo, "dst_area": dst_area,
            "version": d.get("version"), "transitive": d.get("transitive"),
            "cross_repo": bool(d.get("cross_repo")) or src_repo != dst_repo,
        })
    return out


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Emit a multi-repo project digest view (JSON) from a store. "
                    "Output embeds full per-member bundles and can be large for many repos.")
    p.add_argument("--store", required=True, help="path to the journey-graph store")
    p.add_argument("--project", default=None,
                   help="logical project name (as keyed in the store). Defaults "
                        "to the --manifest project; one of the two is required.")
    p.add_argument("--repo", action="append", dest="repos", default=None,
                   help="member repo 'owner/repo'; repeatable. Default: all "
                        "members discovered in the store for the project.")
    p.add_argument("--from", dest="ts_from", required=True,
                   help="window start (YYYY-MM-DD, or a full ISO timestamp)")
    p.add_argument("--to", dest="ts_to", required=True,
                   help="window end (YYYY-MM-DD, or a full ISO timestamp)")
    p.add_argument("--manifest", default=None,
                   help="project manifest; its optional 'ticket_prefixes' "
                        "(e.g. [\"ABC\", \"PLAT\"]) turn on ticket clustering "
                        "with the pattern \\b(?:ABC|PLAT)-\\d+\\b.")
    p.add_argument("--ticket-pattern", default=None,
                   help="regex for internal-ticket refs (wins over the manifest's "
                        "ticket_prefixes); the first capture group is used if the "
                        "pattern has one, else the whole match. Without this or "
                        "manifest ticket_prefixes, ticket clustering is off and "
                        "related_work is [].")
    p.add_argument("--ref-date", dest="ref_date", default=None,
                   help="reference date for every member's meta.ref_date "
                        "(YYYY-MM-DD or a full ISO timestamp). Default: the "
                        "--ref-date gather stored for exactly this window, "
                        "else --to.")
    return p.parse_args(argv)


def _fail(msg):
    sys.stderr.write("digest.py: error: {}\n".format(msg))
    return 2


def _store_projects(conn):
    """Distinct project scopes in the store, sorted (same query as view.py)."""
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM nodes WHERE project IS NOT NULL"))


def resolve_members(conn, project, repos):
    """The member repos to digest, or raise ValueError naming what the store
    holds: an unknown project lists the store's projects; an unknown `--repo`
    lists the project's repos (same wording as view.py). A `--repo` in any casing
    selects the stored member (#70) and is returned in the stored spelling."""
    projects = _store_projects(conn)
    if project not in projects:
        raise ValueError("project {!r} is not in the store (projects: {})".format(
            project, projects))
    known = graphstore.project_repos(conn, project)
    if not known:
        raise ValueError("project {!r} has no member repos in the store".format(
            project))
    if not repos:
        return known
    resolved = []
    for repo in repos:
        stored = graphstore.resolve_repo(conn, project, repo)
        if stored not in known:
            raise ValueError(
                "repo {!r} has no nodes in project {!r} (repos: {})".format(
                    repo, project, known))
        if stored not in resolved:      # two spellings of one member: once
            resolved.append(stored)
    return resolved


def main(argv=None):
    """Exit codes: 0 view written to stdout; 2 usage error — bad window or
    --ref-date, bad --ticket-pattern or --manifest, missing/unreadable store,
    unknown project or repo. Nothing is written to stdout on exit 2."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest_mod.check_reader_window(args.ts_from, args.ts_to, "--from", "--to")
        if args.ref_date is not None:
            manifest_mod.check_ref_date(args.ref_date, "--ref-date")
    except ValueError as exc:
        return _fail(str(exc))

    man = None
    if args.manifest:
        try:
            man = manifest_mod.load_manifest(args.manifest)
        except (OSError, ValueError) as exc:
            return _fail("cannot use --manifest {!r}: {}".format(args.manifest, exc))
    project = args.project
    if man is not None:
        if project is None:
            project = man["project"]
        elif project != man["project"]:
            return _fail("--project {!r} does not match --manifest project "
                         "{!r}".format(project, man["project"]))
    if project is None:
        return _fail("--project is required (or pass --manifest)")

    if args.ticket_pattern:
        try:
            pattern = re.compile(args.ticket_pattern)
        except re.error as e:
            return _fail("invalid --ticket-pattern: {}".format(e))
    else:
        pattern = ticket_pattern_from_prefixes(
            man["ticket_prefixes"] if man is not None else None)

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
            repos = resolve_members(conn, project, args.repos)
        except ValueError as exc:
            return _fail(str(exc))
        warning = graphstore.incomplete_warning(conn, project, args.ts_from,
                                                args.ts_to)
        if warning:
            sys.stderr.write("digest.py: warning: {}\n".format(warning))
        view = build_project_view(conn, project, repos,
                                  args.ts_from, args.ts_to, ticket_pattern=pattern,
                                  ref_date=args.ref_date)
    except sqlite3.DatabaseError as exc:
        return _fail("cannot read store {!r}: {}".format(args.store, exc))
    finally:
        conn.close()
    sys.stdout.write(json.dumps(view, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
