"""Offline link layer: enrich a bundle with trains and buckets. No network."""

import json
import re
import sys

_PR_RE = re.compile(r"Merge pull request #(\d+)|\(#(\d+)\)")


def resolve_commit_pr(message):
    """Best-effort PR number from a commit subject (merge or squash style)."""
    m = _PR_RE.search(message or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def attach_commit_prs(commits):
    """Set each commit's `pr` from its message in place."""
    for c in commits:
        c["pr"] = resolve_commit_pr(c.get("message", ""))
    return commits


def ref(type_, id_, url):
    """A provenance reference: every narrative-bearing fact resolves to one."""
    return {"type": type_, "id": id_, "url": url}


def build_trains(bundle):
    """Group merged PRs (+ their commits + closing issue) into decision trains.

    Train id is deterministic from its anchor: the root issue number when the PR
    closes one (`train-issue-<n>`), else the PR number (`train-pr-<n>`).
    """
    commits_by_pr = {}
    for c in bundle["commits"]:
        commits_by_pr.setdefault(c.get("pr"), []).append(c["sha"])
    issues_by_num = {i["number"]: i for i in bundle["issues"]}

    # Group merged PRs by anchor so multiple PRs on one issue share a train.
    groups = {}
    for pr in bundle["prs"]:
        if not pr.get("merged"):
            continue
        links = list(pr.get("closes") or [])
        for n in pr.get("crossref_issues") or []:
            if n not in links:
                links.append(n)
        root = links[0] if links else None
        anchor = ("issue", root) if root is not None else ("pr", pr["number"])
        groups.setdefault(anchor, []).append(pr)

    trains = []
    for (kind, key), prs in groups.items():
        prs = sorted(prs, key=lambda p: p["number"])
        pr_numbers = [p["number"] for p in prs]
        shas = []
        evidence = []
        for p in prs:
            shas.extend(commits_by_pr.get(p["number"], []))
            evidence.append(ref("pr", p["number"], p["url"]))
        root_issue = key if kind == "issue" else None
        train_kind = "other"
        if root_issue is not None and root_issue in issues_by_num:
            issue = issues_by_num[root_issue]
            train_kind = issue.get("kind", "other")
            evidence.insert(0, ref("issue", root_issue, issue["url"]))
        trains.append({
            "id": f"train-issue-{root_issue}" if root_issue is not None
            else f"train-pr-{pr_numbers[0]}",
            "kind": train_kind,
            "root_issue": root_issue,
            "prs": pr_numbers,
            "commits": sorted(shas),
            "outcome": "shipped",
            "evidence": evidence,
        })
    return sorted(trains, key=lambda t: t["id"])


def compute_buckets(bundle):
    """Coarse Phase-1 buckets: shipped = merged PRs + completed issues."""
    shipped = []
    for pr in bundle["prs"]:
        if pr.get("merged"):
            shipped.append(ref("pr", pr["number"], pr["url"]))
    for issue in bundle["issues"]:
        if issue.get("state") == "closed" and issue.get("state_reason") == "completed":
            shipped.append(ref("issue", issue["number"], issue["url"]))
    return {"shipped": shipped, "in_flight": [], "rejected": [], "next_candidates": []}


def enrich(bundle):
    """Deterministically enrich a bundle in place: commit->PR, trains, buckets."""
    attach_commit_prs(bundle["commits"])
    bundle["trains"] = build_trains(bundle)
    bundle["buckets"] = compute_buckets(bundle)
    return bundle


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write("usage: link.py BUNDLE.json\n")
        raise SystemExit(2)
    path = argv[0]
    with open(path) as fh:
        bundle = json.load(fh)
    enrich(bundle)
    with open(path, "w") as fh:
        json.dump(bundle, fh, indent=2)
    sys.stderr.write(
        f"linked {len(bundle['trains'])} trains, "
        f"{len(bundle['buckets']['shipped'])} shipped into {path}\n"
    )
    return path


if __name__ == "__main__":
    main()
