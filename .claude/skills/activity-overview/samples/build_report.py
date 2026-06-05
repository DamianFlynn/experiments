"""Structural renderer for a digest.py project view (illustration only).

**The JSON bundle is the product.** `gather → store → digest.py` emits the
verifiable project view (`samples/digest_view.json`): the trustworthy, fully
sourced interface a downstream content agent consumes to author a narrative.

This script does NOT author narrative. It deterministically renders the bundle
into a Markdown *skeleton* — metadata, summary counts, tables, and the validated
`.mmd` diagrams — so a reader can eyeball the bundle's shape. Every value resolves
to the bundle or a GitHub URL; no interpretation, theses, or editorial prose. The
emitted `.md` is an illustration of what an agent would flesh out, not the
pipeline's product. Re-running over the same view is byte-stable.
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
    """First resolvable ref title for a project train (a label, not narrative)."""
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


def esc(s):
    return str(s or "").replace("|", "\\|")


lines = []
W = lines.append


# ============================ header / provenance ============================
W("# {} — Project Activity Digest ({} → {})".format(PROJECT, FROM, TO))
W("")
W("> **Illustration, not the product.** The product is the JSON bundle "
  "(`samples/digest_view.json`) emitted by `digest.py` — the verifiable interface a "
  "downstream content agent consumes to write the narrative. This Markdown is a "
  "deterministic *structural* rendering of that bundle (tables + validated diagrams "
  "+ sourced facts, no authored prose). Every value resolves to the bundle or a "
  "GitHub URL.")
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
# Completeness status, read straight from each member's meta.boundary_dropped_commits
# (a data field, not an opinion): non-empty = code ledgers incomplete for that member.
_dropped = {m["repo"]: (m["bundle"].get("meta") or {}).get("boundary_dropped_commits", [])
            for m in view["members"]}
_with_gap = {r: d for r, d in _dropped.items() if d}
if _with_gap:
    detail = "; ".join("`{}` ({})".format(short(r), ", ".join(c[:9] for c in d))
                       for r, d in _with_gap.items())
    W("> ⚠️ **`meta.boundary_dropped_commits` non-empty** ({}): {}. Feature-change / "
      "content-lifecycle ledgers are incomplete for those members. Re-gather with a "
      "wider `ACTIVITY_CLONE_MARGIN_DAYS`.".format(
          sum(len(d) for d in _with_gap.values()), detail))
else:
    W("> ✅ **`meta.boundary_dropped_commits` empty for every member** — "
      "feature-change / content-lifecycle ledgers are complete.")
W("")


# ================================= summary ===================================
n_ship = len(view["shipped"])
n_trains = len(view["trains"])
n_cross = sum(1 for t in view["trains"] if len(t["repos"]) > 1)
n_edges = len(view["module_edges"])
n_cross_edges = sum(1 for e in view["module_edges"] if e["cross_repo"])
ship_pr = sum(1 for r in view["shipped"] if r["type"] == "pr")
ship_iss = n_ship - ship_pr
n_people = len(view["people"])
n_bots = sum(1 for r in view["people"].values() if r.get("is_bot"))
W("## Summary")
W("")
W("| Metric | Value |")
W("|---|---|")
W("| Member repos | {} |".format(len(view["members"])))
W("| Shipped (merged PRs) | {} |".format(ship_pr))
W("| Shipped (issues closed) | {} |".format(ship_iss))
W("| Decision trains | {} |".format(n_trains))
W("| Cross-repo trains | {} |".format(n_cross))
W("| `related_work` clusters | {} |".format(len(view["related_work"])))
W("| Module-dependency edges | {} |".format(n_edges))
W("| — cross-repo / intra-repo | {} / {} |".format(n_cross_edges, n_edges - n_cross_edges))
W("| People (of which bots) | {} ({}) |".format(n_people, n_bots))
W("| Modules (repo-qualified areas) | {} |".format(len(view["modules"])))
W("")


# ====================== cross-repo module dependency graph ===================
W("## Cross-repo module dependency graph")
W("")
W("`view[\"module_edges\"]` — resolved Terraform `depends_on` (consumer area → the "
  "member area it sources). Cross-repo = a member sourcing another member's module.")
W("")
W(mmd(os.path.join(DIAG, "project_module_graph.mmd")))
W("")
W("| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo | Transitive |")
W("|---|---|---|---|---|")
for e in view["module_edges"]:
    W("| `{}` · `{}` | `{}` · `{}` | {} | {} | {} |".format(
        short(e["src_repo"]), e["src_area"], short(e["dst_repo"]), e["dst_area"],
        ("`" + e["version"] + "`") if e["version"] else "—",
        "yes" if e["cross_repo"] else "no",
        "yes" if e["transitive"] else "no"))
W("")


# ============================ decision trains ================================
trains = sorted(view["trains"],
                key=lambda t: (-(len(t["prs"]) + len(t["commits"])),
                               -_RANK.get(t["outcome"], 0), t["id"]))
W("## Decision trains")
W("")
W("`view[\"trains\"]`, ordered by PR+commit count then outcome. Anchor = qualified "
  "spine id; refs link to GitHub.")
W("")
W("| Train (anchor) | Repo | Kind | Outcome | Refs | Commits |")
W("|---|---|---|---|---|---|")
for t in trains:
    anchor = graphstore.parse_id(t["id"][len("ptrain-"):])["local"]
    W("| `{}` — {} | {} | {} | {} | {} | {} |".format(
        anchor, esc(train_title(t)[:46]),
        ", ".join(short(r) for r in t["repos"]),
        t["kind"], t["outcome"],
        " ".join(train_refs(t)), len(t["commits"])))
W("")
W("`related_work` (ticket-linked cross-repo clusters): {}".format(
    len(view["related_work"]) or "none"))
W("")


# ============================ shipped this period ============================
W("## Shipped this period")
W("")
W("`view[\"shipped\"]` (repo-tagged), grouped by member.")
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
        train = "`{}`".format(r["train"]) if r.get("train") else "—"
        W("| [{}#{}]({}) | {} | {} |".format(
            r["type"], r["id"], r["url"], esc(rec.get("title"))[:80], train))
    W("")


# ============================ module ownership ===============================
W("## Module ownership")
W("")
W("`view[\"modules\"]` (per-area commits/PRs/files) + CODEOWNERS + `view[\"people\"]`. "
  "Top 8 areas per repo by files touched.")
W("")
people = view["people"]
attributed = {login: rec for login, rec in people.items() if rec.get("modules")}
W("- People in window: {} logins ({} bots); {} with module-level attribution: {}.".format(
    len(people), n_bots, len(attributed),
    ", ".join("`{}`{}".format(k, " (bot)" if v["is_bot"] else "")
              for k, v in sorted(attributed.items())) or "—"))
W("")
mods = view["modules"]
W("| Repo | Module (area) | CODEOWNERS | Commits | PRs | Files |")
W("|---|---|---|---|---|---|")
for m in view["members"]:
    repo = m["repo"]
    co = (m["bundle"].get("code_owners") or {})
    owner = "; ".join(sorted({o for owners in co.values() for o in owners})) or "—"
    repo_mods = [(k.split("::", 1)[1], s) for k, s in mods.items()
                 if k.startswith(repo + "::")]
    repo_mods.sort(key=lambda kv: (-kv[1].get("files_changed", 0),
                                   -kv[1].get("commits", 0), kv[0]))
    for area, s in repo_mods[:8]:
        W("| `{}` | `{}` | `{}` | {} | {} | {} |".format(
            short(repo), area, owner, s.get("commits", 0),
            s.get("prs", 0), s.get("files_changed", 0)))
W("")
cg = os.path.join(DIAG, slug("Azure/terraform-azurerm-avm-ptn-aiml-landing-zone"),
                  "contributor_graph.mmd")
if os.path.exists(cg):
    W("People ↔ code-area edges:")
    W("")
    W(mmd(cg))
    W("")


# ============================ per-member appendices ==========================
W("---")
W("")
W("## Per-member detail")
W("")
W("Per-member sections from `view[\"members\"][i][\"bundle\"]`.")
W("")

for m in view["members"]:
    repo, b = m["repo"], m["bundle"]
    sl = slug(repo)
    bk = b["buckets"]
    W("### `{}`".format(repo))
    W("")
    W("#### Activity at a glance")
    W("")
    W(mmd(os.path.join(DIAG, sl, "buckets_pie.mmd")))
    W("")
    W(mmd(os.path.join(DIAG, sl, "timeline_gantt.mmd")))
    W("")
    rels = b.get("releases", [])
    if rels:
        W("#### Releases")
        W("")
        for r in rels:
            W("- `{}` ({}) — {} · [release]({})".format(
                r.get("tag_name"), r.get("name"), r.get("published_at"),
                r.get("url", "")))
        W("")
    ws = b.get("workflow_stats", {})
    if ws:
        W("#### CI/CD health")
        W("")
        W("| Workflow | Success | Failure | Cancelled | Total |")
        W("|---|---|---|---|---|")
        for name, s in sorted(ws.items()):
            W("| {} | {} | {} | {} | {} |".format(
                esc(name), s.get("success", 0), s.get("failure", 0),
                s.get("cancelled", 0), s.get("total", 0)))
        W("")
    W("#### Issue kinds")
    W("")
    W(mmd(os.path.join(DIAG, sl, "kind_breakdown.mmd")))
    W("")
    inflt = bk.get("in_flight", [])
    if inflt:
        by = member_index(repo)
        prs = [r for r in inflt if r["type"] == "pr"]
        iss = [r for r in inflt if r["type"] == "issue"]
        W("#### In flight — {} ({} open PRs, {} open issues)".format(
            len(inflt), len(prs), len(iss)))
        W("")
        for r in sorted(prs, key=lambda r: r["id"]):
            rec = by.get((r["type"], r["id"]), {})
            train = " — train `{}`".format(r["train"]) if r.get("train") else ""
            W("- [{}#{}]({}) {}{}".format(
                r["type"], r["id"], r["url"], esc(rec.get("title"))[:90], train))
        W("")
    rej = bk.get("rejected", [])
    if rej:
        by = member_index(repo)
        W("#### Rejected / abandoned")
        W("")
        for r in rej:
            rec = by.get((r["type"], r["id"]), {})
            W("- [{}#{}]({}) {}".format(
                r["type"], r["id"], r["url"], esc(rec.get("title"))[:90]))
        W("")
    fc = b.get("forecast", {})
    W("#### Next-release forecast")
    W("")
    W("- Next milestone: {}".format(fc.get("next_milestone") or "none identified"))
    W("- Candidates: {}".format(len(fc.get("candidates", []))))
    W("")
    W("#### Module dependency graph")
    W("")
    W(mmd(os.path.join(DIAG, sl, "module_graph.mmd")))
    W("")
    fd = b.get("feature_deltas", [])
    if fd:
        W("#### Feature changes (add / drop / change)")
        W("")
        W(mmd(os.path.join(DIAG, sl, "deltas_bar.mmd")))
        W("")
        W("| Kind | Subject | Detail / Name | Author | PR |")
        W("|---|---|---|---|---|")
        for d in fd:
            pr = d.get("pr")
            W("| {} | {} | {} | {} | {} |".format(
                d.get("kind"), d.get("subject"),
                esc(d.get("detail") or d.get("name"))[:60],
                esc(d.get("author")) or "—", "#{}".format(pr) if pr else "—"))
        W("")
        sym = [d for d in fd if d.get("subject") in ("symbol", "comment")]
        if sym:
            W("##### Symbol-level changes")
            W("")
            W("| Detail | Kind | Before | After |")
            W("|---|---|---|---|")
            for d in sym:
                W("| {} | {} | `{}` | `{}` |".format(
                    esc(d.get("detail"))[:42], d.get("kind"),
                    esc(d.get("before") or "—")[:48], esc(d.get("after") or "—")[:48]))
            W("")
        arts = b.get("artifacts", {})
        if arts:
            W("#### Content lifecycle (built / changed / dropped)")
            W("")
            W(mmd(os.path.join(DIAG, sl, "content_timeline.mmd")))
            W("")
            removed = sorted((a for a in arts.values()
                              if a.get("status") in ("removed", "replaced")),
                             key=lambda a: a.get("name") or "")
            if removed:
                for a in removed:
                    W("- `{}` ({}) — {}".format(
                        a.get("name"), a.get("kind"), a.get("status")))
            else:
                W("- {} artifacts, all `live` (none removed/replaced).".format(len(arts)))
            W("")
    W("")

W("---")
W("")
W("*Structural rendering of `digest.py` over the project store; {} diagrams "
  "validated with `mmdc`. The JSON bundle (`digest_view.json`) is the source of "
  "truth. Re-run: `python3 samples/build_report.py`.*".format(
      sum(len(os.listdir(os.path.join(DIAG, slug(m["repo"])))) for m in view["members"]) + 1))

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("wrote", OUT, "({} lines)".format(len(lines)))
