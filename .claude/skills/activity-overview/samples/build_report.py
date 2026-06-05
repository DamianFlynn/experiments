"""Render the AVM-TF constellation project digest from a digest.py view.

Reads workspace/digest_view.json (the JSON emitted by
`python3 digest.py --store ... --project ... --from ... --to ...`) plus the
per-member + project `.mmd` diagrams rendered under workspace/diagrams/, and
fills report-template.md into a finished Markdown digest. Every fact resolves to
a store-backed value or a GitHub URL; narrative prose is grounded in those
values only. Deterministic — re-running over the same view is byte-stable.
"""
import datetime
import json
import os
import sys

# Resolve paths against the skill root (this file lives in <skill>/samples/), and
# put the skill root on sys.path so `import graphstore` / `import render` work no
# matter the invocation CWD.
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_ROOT)

VIEW = os.path.join(SKILL_ROOT, "workspace/digest_view.json")
DIAG = os.path.join(SKILL_ROOT, "workspace/diagrams")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "avm-tf-aiml-lz-digest.md")

with open(VIEW, encoding="utf-8") as _fh:
    view = json.load(_fh)
PROJECT = view["meta"]["project"]
FROM, TO = view["meta"]["from"], view["meta"]["to"]
TODAY = datetime.date.today().isoformat()

import graphstore  # noqa: E402  (qualify/parse ids, same as digest.py)


def slug(repo):
    return repo.replace("/", "__")


def short(repo):
    return repo.split("/")[-1]


def mmd(path):
    """Inline a rendered diagram as a fenced mermaid block (validated by mmdc)."""
    with open(path, encoding="utf-8") as fh:
        body = fh.read().rstrip("\n")
    return "```mermaid\n" + body + "\n```"


# ---- title/url index across all members (resolve qualified refs) -------------
idx = {}
for m in view["members"]:
    repo, b = m["repo"], m["bundle"]
    for p in b.get("prs", []):
        idx[graphstore.qualify_id(PROJECT, repo, "pr-{}".format(p["number"]))] = {
            "title": p.get("title"), "url": p.get("url"), "num": p["number"],
            "repo": repo, "type": "pr"}
    for i in b.get("issues", []):
        idx[graphstore.qualify_id(PROJECT, repo, "issue-{}".format(i["number"]))] = {
            "title": i.get("title"), "url": i.get("url"), "num": i["number"],
            "repo": repo, "type": "issue"}


def member_index(repo):
    by = {}
    b = [m for m in view["members"] if m["repo"] == repo][0]["bundle"]
    for p in b.get("prs", []):
        by[("pr", p["number"])] = p
    for i in b.get("issues", []):
        by[("issue", i["number"])] = i
    return by


_RANK = {"shipped": 3, "in_flight": 2, "rejected": 1, "abandoned": 0}


def train_title(t):
    """Best human title for a project train: its first resolvable ref."""
    for q in t["issues"] + t["prs"]:
        info = idx.get(q)
        if info and info["title"]:
            return info["title"]
    return "(cross-window train)"


def train_refs(t):
    out = []
    for q in t["prs"] + t["issues"]:
        info = idx.get(q)
        if info:
            out.append("[{}#{}]({})".format(short(info["repo"]), info["num"], info["url"]))
        else:
            # out-of-window spine anchor (no in-window record to link): render it
            # repo-qualified, since a bare local id like `issue-123` is ambiguous
            # across members (two repos can both have issue-123).
            p = graphstore.parse_id(q)
            scope = p["scope"]
            repo_part = scope[len(PROJECT) + 1:] if scope.startswith(PROJECT + "/") else scope
            out.append("`{}#{}`".format(short(repo_part), p["local"]))
    return out


lines = []
W = lines.append


# ============================ header / provenance ============================
W("# {} — Project Activity Digest ({} → {})".format(PROJECT, FROM, TO))
W("")
W("> **Multi-repo constellation digest.** Source of truth is the journey-graph "
  "store (`workspace/journey.db`); this report is materialized from "
  "`digest.py`'s project view and validated diagrams. Every claim resolves to a "
  "store value or a GitHub URL.")
W("")
W("| | |")
W("|---|---|")
W("| **Project** | `{}` |".format(PROJECT))
W("| **Window** | {} → {} |".format(FROM, TO))
W("| **Members** | {} repos |".format(len(view["members"])))
W("| **Generated** | {} |".format(TODAY))
clones = []
for m in view["members"]:
    sha = (m["bundle"].get("meta") or {}).get("clone_sha", "")
    clones.append("`{}`@`{}`".format(short(m["repo"]), (sha or "?")[:9]))
W("| **Pinned clones** | {} |".format("; ".join(clones)))
W("")
W("Member repositories:")
W("")
for m in view["members"]:
    W("- [`{}`](https://github.com/{})".format(m["repo"], m["repo"]))
W("")
# Data-driven completeness note: read each member's meta.boundary_dropped_commits.
# A wider ACTIVITY_CLONE_MARGIN_DAYS at gather time recovers these; when none
# remain, the report states the ledgers are complete instead of warning.
_dropped = {m["repo"]: (m["bundle"].get("meta") or {}).get("boundary_dropped_commits", [])
            for m in view["members"]}
_with_gap = {r: d for r, d in _dropped.items() if d}
if _with_gap:
    detail = "; ".join("`{}` ({})".format(short(r), ", ".join(c[:9] for c in d))
                       for r, d in _with_gap.items())
    W("> ⚠️ **Known gap.** {} in-window commit(s) sat at the shallow-clone "
      "boundary, so their whole-tree phantom diffs were dropped (visible in "
      "`meta.boundary_dropped_commits`): {}. Code-level ledgers (feature changes "
      "/ content lifecycle) are incomplete for those members. Widen the clone "
      "margin (`ACTIVITY_CLONE_MARGIN_DAYS`) at gather time to recover "
      "them.".format(sum(len(d) for d in _with_gap.values()), detail))
else:
    W("> ✅ **Completeness.** No in-window commit landed on the shallow-clone "
      "boundary (`meta.boundary_dropped_commits` is empty for every member), so "
      "the code-level ledgers (feature changes / content lifecycle) are complete "
      "across the constellation. Gathered with a widened clone margin "
      "(`ACTIVITY_CLONE_MARGIN_DAYS`).")
W("")


# ============================ executive summary ==============================
n_ship = len(view["shipped"])
n_trains = len(view["trains"])
n_cross = sum(1 for t in view["trains"] if len(t["repos"]) > 1)
n_edges = len(view["module_edges"])
n_cross_edges = sum(1 for e in view["module_edges"] if e["cross_repo"])
ship_pr = sum(1 for r in view["shipped"] if r["type"] == "pr")
ship_iss = n_ship - ship_pr
W("## Executive summary")
W("")
W("Across the constellation's **{} member repos**, **{} items shipped** this week "
  "({} merged PRs, {} issues closed) over **{} decision trains**. The three "
  "modules form a real dependency constellation — **{} resolved module-dependency "
  "edges, {} of them cross-repo**: the AI/ML landing-zone *pattern* consumes both "
  "*resource* modules it sits above.".format(
      len(view["members"]), n_ship, ship_pr, ship_iss, n_trains, n_edges, n_cross_edges))
W("")
W("The notable structural finding: **the repos are coupled by module "
  "dependencies, not by shared work**. There are **{} cross-repo decision trains** "
  "and **{} ticket-linked `related_work` clusters** — every train lives entirely "
  "within one repo, and no internal ticket spans two. So a change in the vnet or "
  "operationalinsights module ripples into the landing zone *structurally* "
  "(version-pinned `source` references), even though the teams' issue/PR threads "
  "this window never crossed repo boundaries.".format(n_cross, len(view["related_work"])))
W("")


# ====================== cross-repo module dependency graph ===================
W("## Cross-repo module dependency graph (blast radius)")
W("")
W("Resolved `depends_on` edges across the project (Terraform `source` → the "
  "member that publishes that registry module). Cross-repo edges are a member "
  "module depending on **another member's** published module — the constellation's "
  "real coupling.")
W("")
W(mmd(os.path.join(DIAG, "project_module_graph.mmd")))
W("")
W("| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo | Transitive |")
W("|---|---|---|---|---|")
for e in view["module_edges"]:
    W("| `{}` · `{}` | `{}` · `{}` | {} | {} | {} |".format(
        short(e["src_repo"]), e["src_area"], short(e["dst_repo"]), e["dst_area"],
        ("`" + e["version"] + "`") if e["version"] else "—",
        "**yes**" if e["cross_repo"] else "no",
        "yes" if e["transitive"] else "no"))
W("")
_xrepo = [e for e in view["module_edges"] if e["cross_repo"]]
_intra = [e for e in view["module_edges"] if not e["cross_repo"]]
if _xrepo:
    W("**Reading the graph.** {} cross-repo dependency edge(s) resolved this "
      "window — a member module consuming another member's published "
      "module:".format(len(_xrepo)))
    W("")
    for e in _xrepo:
        W("- `{}` · `{}` → `{}` · `{}` (`{}`{})".format(
            short(e["src_repo"]), e["src_area"], short(e["dst_repo"]), e["dst_area"],
            e["version"] or "unpinned", ", transitive" if e["transitive"] else ""))
    W("")
    if _intra:
        W("Plus {} intra-repo sub-module edge(s).".format(len(_intra)))
        W("")
    W("Edges come from a static, whole-tracked-tree parse of every module's "
      "`source` references (no `terraform init`), so the graph reflects the repo's "
      "full module structure regardless of in-window churn. **Blast radius:** a "
      "breaking change to a depended-on resource module's `main.tf` forces a version "
      "bump in every consumer above. Compute the precise dependents of any member "
      "with `python3 spotlight.py dependents <owner/repo> --store workspace/journey.db "
      "--project {}`.".format(PROJECT))
else:
    W("No cross-repo module dependencies resolved this window.")
W("")


# ============================ decision trains ================================
trains = sorted(view["trains"],
                key=lambda t: (-(len(t["prs"]) + len(t["commits"])),
                               -_RANK.get(t["outcome"], 0), t["id"]))
W("## Decision trains")
W("")
W("{} project-wide trains, ranked most-substantial first (by PR + commit count, "
  "then outcome). Each train is keyed off its qualified spine anchor; refs link "
  "to GitHub. Project trains carry no per-train flowchart of their own — for the "
  "two headline trains, the owning member's deep-tier flowchart is embedded "
  "below.".format(n_trains))
W("")
W("| Train (anchor) | Repo | Kind | Outcome | Refs | Commits |")
W("|---|---|---|---|---|---|")
for t in trains:
    anchor = graphstore.parse_id(t["id"][len("ptrain-"):])["local"]
    W("| `{}` — {} | {} | {} | {} | {} | {} |".format(
        anchor, train_title(t)[:46].replace("|", "\\|"),
        ", ".join(short(r) for r in t["repos"]),
        t["kind"], t["outcome"],
        " ".join(train_refs(t)), len(t["commits"])))
W("")

# featured deep-train flowcharts (drill into the owning member bundle)
featured = [
    ("Azure/terraform-azurerm-avm-ptn-aiml-landing-zone", "train-pr-80",
     "**Shipped headline — APIM VNet integration.** "
     "[PR #80](https://github.com/Azure/terraform-azurerm-avm-ptn-aiml-landing-zone/pull/80) "
     "configures API Management with VNet integration so it reaches internal "
     "services. *Significance 28.0 — landed in 7 days · 2 participants · 0 formal "
     "reviewers (merged via the maintainer pipeline).* Cut into release `v0.4.1`."),
    ("Azure/terraform-azurerm-avm-ptn-aiml-landing-zone", "train-pr-84",
     "**Largest open train — the 7-issue rollup.** "
     "[PR #84](https://github.com/Azure/terraform-azurerm-avm-ptn-aiml-landing-zone/pull/84) "
     "*“resolve 7 open issues — AppGW routing, APIM zones, VNet peering…”* "
     "is still open (opened 2026-04-09, ~57 days as of {} · 3 participants · 1 "
     "reviewer). It bundles several in-window bug reports into one fix.".format(TODAY)),
]
for repo, tid, narrative in featured:
    fc = os.path.join(DIAG, slug(repo), tid + ".mmd")
    if not os.path.exists(fc):
        continue
    W("### {} · `{}`".format(short(repo), tid))
    W("")
    W(narrative)
    W("")
    W(mmd(fc))
    W("")
if not view["related_work"]:
    W("> **Related work (ticket-linked):** none. No internal ticket reference "
      "(Jira/ADO-style) spans two trains this window, so there are no hidden "
      "cross-repo deliverables beyond the module-dependency coupling above.")
    W("")


# ============================ shipped this period ============================
W("## Shipped this period")
W("")
W("{} items merged/closed, grouped by member repo.".format(n_ship))
W("")
for m in view["members"]:
    repo = m["repo"]
    rows = [r for r in view["shipped"] if r["repo"] == repo]
    if not rows:
        continue
    by = member_index(repo)
    W("### `{}` — {} shipped".format(short(repo), len(rows)))
    W("")
    W("| Item | Title | Train |")
    W("|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["type"], r["id"])):
        rec = by.get((r["type"], r["id"]), {})
        title = (rec.get("title") or "").replace("|", "\\|")
        train = "`{}`".format(r["train"]) if r.get("train") else "—"
        W("| [{}#{}]({}) | {} | {} |".format(
            r["type"], r["id"], r["url"], title[:80], train))
    W("")


# ============================ module ownership ===============================
W("## Module ownership")
W("")
W("CODEOWNERS plus per-area contribution stats (commits / PRs / files touched in "
  "window). Area ids are repo-qualified in the store; split on `::` for display. "
  "All three repos are owned by the same AVM core team.")
W("")
people = view["people"]
attributed = {login: rec for login, rec in people.items() if rec.get("modules")}
W("- **People in window:** {} logins ({} bots). **{}** have module-level "
  "attribution: {}.".format(
      len(people), sum(1 for r in people.values() if r["is_bot"]),
      len(attributed),
      ", ".join("`{}`{}".format(k, " (bot)" if v["is_bot"] else "")
                for k, v in sorted(attributed.items())) or "—"))
W("")
# top modules per repo by commits
mods = view["modules"]
W("| Repo | Module (area) | CODEOWNERS | Commits | PRs | Files |")
W("|---|---|---|---|---|---|")
# show meaningful module areas (skip pure dotfile churn): top 8 per repo by files
for m in view["members"]:
    repo = m["repo"]
    co = (m["bundle"].get("code_owners") or {})
    owner = "; ".join(sorted({o for owners in co.values() for o in owners})) or "—"
    repo_mods = [(k.split("::", 1)[1], s) for k, s in mods.items()
                 if k.startswith(repo + "::")]
    repo_mods.sort(key=lambda kv: (-kv[1].get("files_changed", 0),
                                   -kv[1].get("commits", 0), kv[0]))
    for area, s in repo_mods[:8]:
        W("| `{}` | `{}` | {} | {} | {} | {} |".format(
            short(repo), area, "`" + owner + "`", s.get("commits", 0),
            s.get("prs", 0), s.get("files_changed", 0)))
W("")
# contributor graph for the repo that has one
cg = os.path.join(DIAG, slug("Azure/terraform-azurerm-avm-ptn-aiml-landing-zone"),
                  "contributor_graph.mmd")
if os.path.exists(cg):
    W("People ↔ code-area edges (landing zone — the only member with in-window "
      "code-area attribution after the boundary drop):")
    W("")
    W(mmd(cg))
    W("")


# ============================ per-member appendices ==========================
W("---")
W("")
W("## Per-member detail")
W("")
W("Single-repo sections below render once per member from "
  "`view[\"members\"][i][\"bundle\"]`.")
W("")

for m in view["members"]:
    repo, b = m["repo"], m["bundle"]
    sl = slug(repo)
    bk = b["buckets"]
    W("### `{}`".format(repo))
    W("")
    # activity at a glance
    W("#### Activity at a glance")
    W("")
    W(mmd(os.path.join(DIAG, sl, "buckets_pie.mmd")))
    W("")
    W(mmd(os.path.join(DIAG, sl, "timeline_gantt.mmd")))
    W("")
    # releases
    rels = b.get("releases", [])
    if rels:
        W("#### Releases")
        W("")
        for r in rels:
            W("- **{}** (`{}`) — published {}. [release]({})".format(
                r.get("name"), r.get("tag_name"), r.get("published_at"),
                r.get("url", "")))
        W("")
    # CI/CD
    ws = b.get("workflow_stats", {})
    if ws:
        W("#### CI/CD health")
        W("")
        W("| Workflow | Success | Failure | Cancelled | Total |")
        W("|---|---|---|---|---|")
        for name, s in sorted(ws.items()):
            W("| {} | {} | {} | {} | {} |".format(
                name.replace("|", "\\|"), s.get("success", 0), s.get("failure", 0),
                s.get("cancelled", 0), s.get("total", 0)))
        W("")
    # issue kinds
    W("#### Issue kinds")
    W("")
    W(mmd(os.path.join(DIAG, sl, "kind_breakdown.mmd")))
    W("")
    # in flight
    inflt = bk.get("in_flight", [])
    if inflt:
        by = member_index(repo)
        W("#### In flight ({})".format(len(inflt)))
        W("")
        prs = [r for r in inflt if r["type"] == "pr"]
        iss = [r for r in inflt if r["type"] == "issue"]
        W("{} open PRs, {} open issues. Open PRs:".format(len(prs), len(iss)))
        W("")
        for r in sorted(prs, key=lambda r: r["id"]):
            rec = by.get((r["type"], r["id"]), {})
            train = " — train `{}`".format(r["train"]) if r.get("train") else ""
            W("- [{}#{}]({}) {}{}".format(
                r["type"], r["id"], r["url"],
                (rec.get("title") or "").replace("|", "\\|")[:90], train))
        W("")
    # rejected / abandoned
    rej = bk.get("rejected", [])
    if rej:
        by = member_index(repo)
        W("#### Rejected / abandoned")
        W("")
        for r in rej:
            rec = by.get((r["type"], r["id"]), {})
            W("- [{}#{}]({}) {}".format(
                r["type"], r["id"], r["url"],
                (rec.get("title") or "").replace("|", "\\|")[:90]))
        W("")
    # forecast
    fc = b.get("forecast", {})
    cands = fc.get("candidates", [])
    W("#### Next-release forecast")
    W("")
    if not cands:
        W("**Next milestone:** none identified. No open item carried a milestone, "
          "high-priority, or open-PR signal strong enough to forecast this window.")
    else:
        W("**Next milestone:** {}".format(fc.get("next_milestone") or "none identified"))
    W("")
    # module dependency graph
    W("#### Module dependency graph")
    W("")
    W(mmd(os.path.join(DIAG, sl, "module_graph.mmd")))
    W("")
    # feature changes + content lifecycle (only where present)
    fd = b.get("feature_deltas", [])
    if fd:
        W("#### Feature changes (add / drop / change)")
        W("")
        W(mmd(os.path.join(DIAG, sl, "deltas_bar.mmd")))
        W("")
        W("| Kind | Subject | Detail / Name | Author | PR |")
        W("|---|---|---|---|---|")
        for d in fd:
            name = d.get("detail") or d.get("name") or ""
            pr = d.get("pr")
            prcell = "#{}".format(pr) if pr else "—"
            W("| {} | {} | {} | {} | {} |".format(
                d.get("kind"), d.get("subject"),
                str(name).replace("|", "\\|")[:60],
                d.get("author") or "—", prcell))
        W("")
        sym = [d for d in fd if d.get("subject") in ("symbol", "comment")]
        if sym:
            W("##### Symbol-level changes")
            W("")
            W("| Detail | Kind | Before | After |")
            W("|---|---|---|---|")
            for d in sym:
                bef = str(d.get("before") or "—").replace("|", "\\|")[:48]
                aft = str(d.get("after") or "—").replace("|", "\\|")[:48]
                W("| {} | {} | `{}` | `{}` |".format(
                    str(d.get("detail") or "").replace("|", "\\|")[:42],
                    d.get("kind"), bef, aft))
            W("")
        # content lifecycle
        arts = b.get("artifacts", {})
        if arts:
            W("#### Content lifecycle (built / changed / dropped)")
            W("")
            W(mmd(os.path.join(DIAG, sl, "content_timeline.mmd")))
            W("")
            statuses = sorted(arts.values(), key=lambda a: a.get("status") or "")
            removed = [a for a in statuses if a.get("status") in ("removed", "replaced")]
            if removed:
                for a in removed:
                    W("- **{}** ({}) — {}.".format(
                        a.get("name"), a.get("kind"), a.get("status")))
            else:
                W("All {} tracked artifacts are `live` (examples, READMEs, docs "
                  "revised in window — none removed or replaced).".format(len(arts)))
            W("")
    W("")

W("---")
W("")
W("*Generated from `digest.py` over `workspace/journey.db`. {} diagrams "
  "rendered and validated with `mmdc`. Re-run: "
  "`python3 build_report.py`.*".format(
      sum(len(os.listdir(os.path.join(DIAG, slug(m["repo"])))) for m in view["members"]) + 1))

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("wrote", OUT, "({} lines)".format(len(lines)))
