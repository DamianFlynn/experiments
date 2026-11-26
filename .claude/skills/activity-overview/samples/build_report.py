"""Structural renderer for a digest.py project view (reference formatter).

**The JSON bundle is the product.** `gather → store → digest.py` emits the
verifiable project view — the trustworthy, fully sourced interface a downstream
content agent consumes to author a narrative. The committed snapshot of that
product is `samples/digest_view.json`.

This script does NOT author narrative. It renders the view's diagrams with
`render.py` (one set per member bundle, plus the project module graph), checks
them with `mmdc` unless `--skip-validate`, and writes a Markdown *skeleton* —
metadata, summary counts, tables and the diagrams — so a reader can eyeball the
bundle's shape. Every value resolves to the view or a GitHub URL; no
interpretation, theses or editorial prose. The output depends only on the view
(no wall-clock date, no paths), so re-running over the same view is byte-stable.

Usage:
    python3 samples/build_report.py --view VIEW.json --out REPORT.md \
        [--diagrams-dir DIR] [--skip-validate]

`--diagrams-dir` defaults to `diagrams/` next to `--out`; each member's diagrams go
to `<DIR>/<owner>__<name>/`, the project module graph to
`<DIR>/project_module_graph.mmd`. Exit codes: 0 report written; 1 a diagram failed
mmdc validation (no report written); 2 usage error or not a project view; 3 `mmdc`
not on PATH (pass `--skip-validate` to render without it).
"""
import argparse
import copy
import json
import os
import sys

# Put the skill root (this file lives in <skill>/samples/) on sys.path so the
# pipeline modules import no matter the invocation CWD.
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_ROOT)

import derive  # noqa: E402  (CODEOWNERS rules + area matching)
import extract  # noqa: E402  (VIEW_CONTRACT_VERSION)
import graphstore  # noqa: E402  (qualify/parse ids, same as digest.py)
import render  # noqa: E402  (diagram emitters + mmdc validation)

PROJECT_GRAPH = "project_module_graph"

_RANK = {"shipped": 3, "in_flight": 2, "rejected": 1, "abandoned": 0}


def slug_dir(repo):
    return repo.replace("/", "__")


def short(repo):
    return repo.split("/")[-1]


def esc(s):
    return str(s or "").replace("|", "\\|")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Render a digest.py project view into a structural Markdown "
                    "skeleton (tables + render.py diagrams, no narrative).")
    p.add_argument("--view", required=True,
                   help="project view JSON written by digest.py")
    p.add_argument("--out", required=True, help="Markdown report to write")
    p.add_argument("--diagrams-dir", default=None,
                   help="where the .mmd diagrams are written (default: "
                        "diagrams/ next to --out)")
    p.add_argument("--skip-validate", action="store_true",
                   help="skip the mmdc compile check (no mmdc needed)")
    return p.parse_args(argv)


def is_project_view(view):
    return (isinstance(view, dict) and isinstance(view.get("meta"), dict)
            and "project" in view["meta"] and isinstance(view.get("members"), list)
            and all(k in view for k in ("trains", "shipped", "people", "modules",
                                        "module_edges", "related_work")))


def write_diagrams(view, diagrams_dir):
    """Render the project module graph and every member's render.py diagrams
    (the report embeds all but the blocker graph and train flowcharts); return
    (texts, captions, paths).

    texts:    {(slug_dir|None, name): Mermaid text} (None = project-level);
    captions: {(slug_dir|None, name): "Showing n of m unit."} for capped diagrams;
    paths:    every .mmd written, in write order (for mmdc)."""
    texts, captions, paths = {}, {}, []
    os.makedirs(diagrams_dir, exist_ok=True)

    notes = {}
    text = render.emit_project_module_graph(view["module_edges"], notes=notes)
    path = os.path.join(diagrams_dir, PROJECT_GRAPH + ".mmd")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    paths.append(path)
    texts[(None, PROJECT_GRAPH)] = text
    if notes:
        captions[(None, PROJECT_GRAPH)] = notes

    for m in view["members"]:
        sd = slug_dir(m["repo"])
        bundle = copy.deepcopy(m["bundle"])   # write_diagrams sets bundle["diagrams"]
        written = render.write_diagrams(bundle, os.path.join(diagrams_dir, sd))
        truncated = (bundle.get("diagrams") or {}).get("truncated") or {}
        for name in sorted(written):
            paths.append(written[name])
            with open(written[name], encoding="utf-8") as fh:
                texts[(sd, name)] = fh.read()
            if name in truncated:
                captions[(sd, name)] = truncated[name]
    captions = {k: "Showing {} of {} {}.".format(n["shown"], n["total"], n["unit"])
                for k, n in captions.items()}
    return texts, captions, paths


def _span(window):
    return "{} → {}".format(window.get("from"), window.get("to"))


def ci_caveat(meta):
    """The CI/CD-health caveat line for a member view whose meta carries
    `facts_windows` (#94): its CI counts are a sum over gathered windows,
    partial when `workflow_stats_complete` is false (the windows left out are
    `workflow_stats_excluded`); `structure` names the window the code graph
    and owners come from. None when the key is absent."""
    fw = meta.get("facts_windows")
    if not isinstance(fw, dict):
        return None
    summed = ", ".join(_span(w) for w in fw.get("workflow_stats") or []) or "none"
    line = "> CI counts are the sum of the gathered windows {}".format(summed)
    if not fw.get("workflow_stats_complete"):
        line += "; **partial**: not every day of the window is covered"
        excluded = fw.get("workflow_stats_excluded") or []
        if excluded:
            line += " (not counted: {})".format(
                ", ".join(_span(w) for w in excluded))
    line += "."
    if isinstance(fw.get("structure"), dict):
        line += " Code graph and owners from the {} gather.".format(
            _span(fw["structure"]))
    return line


def build_report(view, texts, captions):
    """The Markdown report (a str) for `view`, embedding the rendered diagrams."""
    meta = view["meta"]
    project = meta["project"]
    frm, to = meta["from"], meta["to"]
    members = view["members"]

    def member_meta(m):
        return m["bundle"].get("meta") or {}

    def mmd(sd, name):
        out = ["```mermaid", texts[(sd, name)].rstrip("\n"), "```"]
        if (sd, name) in captions:
            out += ["", "_{}_".format(captions[(sd, name)])]
        return "\n".join(out)

    # ---- title/url index across all members (resolve qualified refs) ---------
    idx = {}
    for m in members:
        repo, b = m["repo"], m["bundle"]
        for p in b.get("prs", []):
            idx[graphstore.qualify_id(project, repo, "pr-{}".format(p["number"]))] = {
                "title": p.get("title"), "url": p.get("url"), "num": p["number"],
                "repo": repo}
        for i in b.get("issues", []):
            idx[graphstore.qualify_id(project, repo, "issue-{}".format(i["number"]))] = {
                "title": i.get("title"), "url": i.get("url"), "num": i["number"],
                "repo": repo}

    by_member = {}
    for m in members:
        by = {}
        for p in m["bundle"].get("prs", []):
            by[("pr", p["number"])] = p
        for i in m["bundle"].get("issues", []):
            by[("issue", i["number"])] = i
        by_member[m["repo"]] = by

    def train_title(t):
        """The anchor item's title, else the first resolvable ref's (a label,
        not narrative) — report-template's {short title} rule."""
        anchor = t["id"][len("ptrain-"):]
        for q in [anchor] + t["issues"] + t["prs"]:
            info = idx.get(q)
            if info and info["title"]:
                return info["title"]
        return "(cross-window train)"

    def train_refs(t):
        out = []
        for q in t["prs"] + t["issues"]:
            info = idx.get(q)
            if info:
                out.append("[{}#{}]({})".format(short(info["repo"]), info["num"],
                                                info["url"]))
            else:
                # out-of-window spine anchor (no in-window record to link): render
                # it repo-qualified, since a bare local id like `issue-123` is
                # ambiguous across members (two repos can both have issue-123).
                p = graphstore.parse_id(q)
                scope = p["scope"]
                repo_part = (scope[len(project) + 1:]
                             if scope.startswith(project + "/") else scope)
                out.append("`{}#{}`".format(short(repo_part), p["local"]))
        return out

    # Reference date: every current view's member meta carries ref_date (BUNDLE.md
    # issue #75); one shared value is shown, else the window end (an older view).
    ref_dates = sorted({member_meta(m).get("ref_date") for m in members} - {None})
    ref_date = ", ".join(ref_dates) if ref_dates else to

    lines = []
    W = lines.append

    # ============================ header / provenance =========================
    W("# {} — Project Activity Digest ({} → {})".format(project, frm, to))
    W("")
    W("> **Illustration, not the product.** The product is the JSON bundle "
      "(`samples/digest_view.json`) emitted by `digest.py` — the verifiable interface a "
      "downstream content agent consumes to write the narrative. This Markdown is a "
      "deterministic *structural* rendering of that bundle (tables + `render.py` "
      "diagrams + sourced facts, no authored prose). Every value resolves to the "
      "bundle or a GitHub URL.")
    W("")
    W("| | |")
    W("|---|---|")
    W("| **Project** | `{}` |".format(project))
    W("| **Window** | {} → {} |".format(frm, to))
    W("| **Reference date** | {} |".format(ref_date))
    W("| **Members** | {} repos |".format(len(members)))
    clones = []
    for m in members:
        sha = member_meta(m).get("clone_sha", "")
        clones.append("`{}`@`{}`".format(short(m["repo"]), (sha or "?")[:9]))
    W("| **Pinned clones** | {} |".format("; ".join(clones)))
    W("")
    # meta.state_as_of (#85, omit-when-absent): the member's records are as its
    # latest gather saw them, after this window's end. Stated per member.
    for m in members:
        as_of = member_meta(m).get("state_as_of")
        if as_of:
            W("> **Records as of {}** (`{}`): as last gathered on that date, after "
              "this window's end ({}); open/closed state, labels, milestones and "
              "comments may be newer than the window.".format(
                  as_of, short(m["repo"]), to))
            W("")
    W("Member repositories:")
    W("")
    for m in members:
        # digest.py's meta.slug is the GitHub owner/name (BUNDLE.md); the repo
        # key is the fallback for a view older than that key.
        slug = member_meta(m).get("slug") or m["repo"]
        W("- [`{}`](https://github.com/{})".format(slug, slug))
    W("")
    # meta.boundary_dropped_commits: views carry it since #107 (omitted when
    # empty, and absent from views of older stores). Warn when a member has
    # one; never claim completeness from its absence.
    with_gap = {m["repo"]: member_meta(m)["boundary_dropped_commits"]
                for m in members if member_meta(m).get("boundary_dropped_commits")}
    if with_gap:
        detail = "; ".join("`{}` ({})".format(short(r), ", ".join(c[:9] for c in d))
                           for r, d in with_gap.items())
        W("> ⚠️ **`meta.boundary_dropped_commits` non-empty** ({}): {}. Feature-change / "
          "content-lifecycle ledgers are incomplete for those members. Re-gather with a "
          "wider `ACTIVITY_CLONE_MARGIN_DAYS`.".format(
              sum(len(d) for d in with_gap.values()), detail))
        W("")

    # ================================= summary ================================
    n_ship = len(view["shipped"])
    n_edges = len(view["module_edges"])
    n_cross_edges = sum(1 for e in view["module_edges"] if e["cross_repo"])
    ship_pr = sum(1 for r in view["shipped"] if r["type"] == "pr")
    people = view["people"]
    n_bots = sum(1 for r in people.values() if r.get("is_bot"))
    W("## Summary")
    W("")
    W("| Metric | Value |")
    W("|---|---|")
    W("| Member repos | {} |".format(len(members)))
    W("| Shipped (merged PRs) | {} |".format(ship_pr))
    W("| Shipped (issues closed) | {} |".format(n_ship - ship_pr))
    W("| Decision trains | {} |".format(len(view["trains"])))
    W("| Cross-repo trains | {} |".format(
        sum(1 for t in view["trains"] if len(t["repos"]) > 1)))
    W("| `related_work` clusters | {} |".format(len(view["related_work"])))
    W("| Module-dependency edges | {} |".format(n_edges))
    W("| — cross-repo / intra-repo | {} / {} |".format(n_cross_edges,
                                                       n_edges - n_cross_edges))
    W("| People (of which bots) | {} ({}) |".format(len(people), n_bots))
    W("| Modules (repo-qualified areas) | {} |".format(len(view["modules"])))
    W("")

    # ====================== cross-repo module dependency graph ================
    W("## Cross-repo module dependency graph")
    W("")
    W("`view[\"module_edges\"]` — resolved Terraform `depends_on` (consumer area → the "
      "member area it sources). Cross-repo = a member sourcing another member's module.")
    W("")
    W(mmd(None, PROJECT_GRAPH))
    W("")
    W("| Consumer (repo · area) | Depends on (repo · area) | Version | Cross-repo | Transitive |")
    W("|---|---|---|---|---|")
    for e in view["module_edges"]:
        W("| `{}` · `{}` | `{}` · `{}` | {} | {} | {} |".format(
            short(e["src_repo"]), e["src_area"], short(e["dst_repo"]), e["dst_area"],
            ("`" + e["version"] + "`") if e.get("version") else "—",
            "yes" if e["cross_repo"] else "no",
            {True: "yes", False: "no"}.get(e.get("transitive"), "—")))
    W("")

    # ============================ decision trains =============================
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

    # ============================ shipped this period =========================
    W("## Shipped this period")
    W("")
    W("`view[\"shipped\"]` (repo-tagged), grouped by member.")
    W("")
    for m in members:
        repo = m["repo"]
        rows = [r for r in view["shipped"] if r["repo"] == repo]
        if not rows:
            continue
        by = by_member[repo]
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

    # ============================ module ownership ============================
    W("## Module ownership")
    W("")
    W("`view[\"modules\"]` (per-area commits/PRs/files) + CODEOWNERS (last matching "
      "rule per area) + `view[\"people\"]`. Top 8 areas per repo by files touched.")
    W("")
    attributed = {login: rec for login, rec in people.items() if rec.get("modules")}
    W("- People in window: {} logins ({} bots); {} with module-level attribution: {}.".format(
        len(people), n_bots, len(attributed),
        ", ".join("`{}`{}".format(k, " (bot)" if v.get("is_bot") else "")
                  for k, v in sorted(attributed.items())) or "—"))
    W("")
    mods = view["modules"]
    W("| Repo | Module (area) | CODEOWNERS | Commits | PRs | Files |")
    W("|---|---|---|---|---|---|")
    for m in members:
        repo, b = m["repo"], m["bundle"]
        rules = derive.compile_codeowners(b.get("code_owners"))
        area_paths = {a.get("id"): a.get("paths") or []
                      for a in (b.get("code_graph") or {}).get("areas", [])}
        repo_mods = [(k.split("::", 1)[1], s) for k, s in mods.items()
                     if k.startswith(repo + "::")]
        repo_mods.sort(key=lambda kv: (-kv[1].get("files_changed", 0),
                                       -kv[1].get("commits", 0), kv[0]))
        for area, s in repo_mods[:8]:
            owners = derive.area_owners(rules, area, area_paths.get(area, ()))
            W("| `{}` | `{}` | {} | {} | {} | {} |".format(
                short(repo), area,
                "`{}`".format("; ".join(owners)) if owners else "—",
                s.get("commits", 0),
                s.get("prs", 0), s.get("files_changed", 0)))
    W("")
    # People ↔ code-area graph per member (render.py draws a "No contributor
    # data" placeholder when a member has no edges).
    for m in members:
        W("People ↔ code-area edges — `{}`:".format(short(m["repo"])))
        W("")
        W(mmd(slug_dir(m["repo"]), "contributor_graph"))
        W("")

    # ============================ per-member appendices =======================
    W("---")
    W("")
    W("## Per-member detail")
    W("")
    W("Per-member sections from `view[\"members\"][i][\"bundle\"]`.")
    W("")

    for m in members:
        repo, b = m["repo"], m["bundle"]
        sd = slug_dir(repo)
        bk = b.get("buckets") or {}
        by = by_member[repo]
        W("### `{}`".format(repo))
        W("")
        W("#### Activity at a glance")
        W("")
        W(mmd(sd, "buckets_pie"))
        W("")
        W(mmd(sd, "timeline_gantt"))
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
        ws = {k: v for k, v in (b.get("workflow_stats") or {}).items()
              if not k.startswith("_")}       # `_truncated` etc. are metadata
        caveat = ci_caveat(b.get("meta") or {})
        if ws or caveat:
            W("#### CI/CD health")
            W("")
        if caveat:
            # shown even with no CI rows: a sub-window, a window gathered
            # without workflows or with zero runs still has a coverage caveat
            W(caveat)
            W("")
        if not ws and caveat:
            W("No CI runs are recorded for this view.")
            W("")
        if ws:
            W("| Workflow | Success | Failure | Cancelled | Total |")
            W("|---|---|---|---|---|")
            for name, s in sorted(ws.items()):
                W("| {} | {} | {} | {} | {} |".format(
                    esc(name), s.get("success", 0), s.get("failure", 0),
                    s.get("cancelled", 0), s.get("total", 0)))
            W("")
        W("#### Issue kinds")
        W("")
        W(mmd(sd, "kind_breakdown"))
        W("")
        inflt = bk.get("in_flight", [])
        if inflt:
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
            W("#### Rejected / abandoned")
            W("")
            for r in rej:
                rec = by.get((r["type"], r["id"]), {})
                W("- [{}#{}]({}) {}".format(
                    r["type"], r["id"], r["url"], esc(rec.get("title"))[:90]))
            W("")
        fc = b.get("forecast") or {}
        W("#### Next-release forecast")
        W("")
        W("- Next milestone: {}".format(fc.get("next_milestone") or "none identified"))
        W("- Candidates: {}".format(len(fc.get("candidates", []))))
        W("")
        W("#### Module dependency graph")
        W("")
        W(mmd(sd, "module_graph"))
        W("")
        fd = b.get("feature_deltas", [])
        if fd:
            W("#### Feature changes (add / drop / change)")
            W("")
            W(mmd(sd, "deltas_bar"))
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
                        esc(d.get("before") or "—")[:48],
                        esc(d.get("after") or "—")[:48]))
                W("")
            arts = b.get("artifacts", {})
            if arts:
                W("#### Content lifecycle (built / changed / dropped)")
                W("")
                W(mmd(sd, "content_timeline"))
                W("")
                removed = sorted((a for a in arts.values()
                                  if a.get("status") in ("removed", "replaced")),
                                 key=lambda a: a.get("name") or "")
                if removed:
                    for a in removed:
                        W("- `{}` ({}) — {}".format(
                            a.get("name"), a.get("kind"), a.get("status")))
                else:
                    W("- {} artifacts, all `live` (none removed/replaced).".format(
                        len(arts)))
                W("")
        W("")

    W("---")
    W("")
    W("*Structural rendering of a `digest.py` project view; {} diagrams rendered by "
      "`render.py` (checked with `mmdc` unless `--skip-validate`). The JSON bundle "
      "(`digest_view.json`) is the source of truth. Re-run: `python3 "
      "samples/build_report.py --view VIEW.json --out REPORT.md`.*".format(
          len(texts)))
    return "\n".join(lines) + "\n"


def _fail(msg):
    sys.stderr.write("build_report.py: error: {}\n".format(msg))
    return 2


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with open(args.view, encoding="utf-8") as fh:
            view = json.load(fh)
    except (OSError, ValueError) as exc:
        return _fail("cannot read --view {!r}: {}".format(args.view, exc))
    if not is_project_view(view):
        return _fail("{} is not a digest.py project view (need meta.project, "
                     "members, trains, shipped, people, modules, module_edges, "
                     "related_work); see BUNDLE.md".format(args.view))
    version = view["meta"].get("schema_version")
    if isinstance(version, int) and version > extract.VIEW_CONTRACT_VERSION:
        # A newer view contract than this formatter knows (BUNDLE.md). An
        # unversioned view is an older gather and still renders.
        return _fail("{} has meta.schema_version {}; this formatter reads "
                     "views up to {}".format(args.view, version,
                                             extract.VIEW_CONTRACT_VERSION))
    diagrams_dir = args.diagrams_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.out)), "diagrams")
    texts, captions, paths = write_diagrams(view, diagrams_dir)
    if not args.skip_validate:
        try:
            render.validate_with_mmdc(paths)
        except RuntimeError as exc:
            sys.stderr.write("build_report.py: error: {}\n".format(exc))
            return 1
    report = build_report(view, texts, captions)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report)
    sys.stderr.write("wrote {} ({} lines); {} diagrams in {} ({})\n".format(
        args.out, report.count("\n"), len(paths), diagrams_dir,
        "unvalidated" if args.skip_validate else "validated"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
