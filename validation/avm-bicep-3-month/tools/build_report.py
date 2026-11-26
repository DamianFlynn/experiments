#!/usr/bin/env python3
"""Fill report-template.md from one enriched bundle view (single repo).

Every row is read from the bundle; titles come from bundle prs/issues (LOOKUP RULE).
Narratives are read from narratives/<train-id>.json and every evidence URL is checked
against that train's slice (report-sections.md "Verify, then compose"): a URL that is
not in the slice is dropped and counted.

usage: build_report.py <bundle.json> <out.md> --gathered <ISO time> [--cap N]
"""
import argparse, collections, json, os, re, sys

ap = argparse.ArgumentParser()
ap.add_argument("bundle"); ap.add_argument("out")
ap.add_argument("--gathered", required=True)
ap.add_argument("--cap", type=int, default=40)
ap.add_argument("--summary", default=None, help="markdown file with authored exec-summary bullets")
ap.add_argument("--title-suffix", default="")
a = ap.parse_args()

B = json.load(open(a.bundle))
base = os.path.dirname(os.path.abspath(a.bundle))
meta = B["meta"]
PRS = {p["number"]: p for p in B["prs"]}
ISS = {i["number"]: i for i in B["issues"]}
TR = {t["id"]: t for t in B["trains"]}
CAP = a.cap
out = []
w = out.append
stats = collections.Counter()


def item(ref_type, n):
    return (PRS if ref_type == "pr" else ISS).get(n)


def title_of(ref_type, n):
    it = item(ref_type, n)
    return it["title"].replace("|", "\\|").replace("\n", " ") if it else f"(not in bundle: {ref_type} #{n})"


def short_title(t):
    if t.get("root_issue") is not None and t["root_issue"] in ISS:
        return title_of("issue", t["root_issue"])
    if t["prs"]:
        return title_of("pr", t["prs"][0])
    return "(no title)"


def train_url(t):
    if t.get("root_issue") is not None and t["root_issue"] in ISS:
        return ISS[t["root_issue"]]["url"]
    if t["prs"] and t["prs"][0] in PRS:
        return PRS[t["prs"][0]]["url"]
    return None


def mermaid(rel):
    p = os.path.join(base, rel)
    w("```mermaid"); w(open(p).read().rstrip()); w("```")
    tr = B["diagrams"].get("truncated", {})


def trunc_note(key):
    tr = B["diagrams"].get("truncated") or {}
    if key in tr:
        t = tr[key]
        w(f"*Showing {t['shown']} of {t['total']} {t['unit']}.*")


def capped(rows, label):
    if len(rows) > CAP:
        w(f"*Showing {CAP} of {len(rows)} {label}; the full list is in the bundle view.*")
        return rows[:CAP]
    return rows


def ref_line(r, extra=""):
    return f"- [{title_of(r['type'], r['id'])}]({r['url']}) ({r['type']} #{r['id']}){extra}"


# ---------------------------------------------------------------- title + caveats
w(f"# {meta['owner']}/{meta['repo']} — Activity Digest ({meta['from']} → {meta['to']}){a.title_suffix}")
w("")
if meta.get("state_as_of"):
    w(f"> Records are as last gathered on {meta['state_as_of']}, after this report's end ({meta['to']}): titles, labels, milestones and board status may be newer than the window.")
else:
    ra = meta.get("records_as_of")
    w(f"> **Gathered late (validation note, not a template line):** this closed month was gathered on {a.gathered}. "
      f"Open/closed/merged state, comments and reviews are projected to {ra['instant'] if ra else meta['to']} "
      f"({ra['prs'] if ra else 0} PR and {ra['issues'] if ra else 0} issue records projected, `meta.records_as_of`); "
      f"titles, labels, milestones, assignees and board status are as of {a.gathered} (USAGE §5.3). "
      f"The view carries no `meta.state_as_of`, so the template's own caveat line does not fire.")
if meta.get("base_branch") is None:
    w("> The store did not know the mainline, so every merged PR is counted as shipped.")
else:
    w(f"> \"Shipped\" = merged into `{meta['base_branch']}`. Structure (code graph, owners) read at "
      f"`{(meta.get('clone_sha') or '')[:12]}` (`structure_as_of: {meta.get('structure_as_of')}`). Reference date {meta['ref_date']}.")
w("> Diagrams were rendered with `render.py --skip-validate`: mmdc is installed but its headless Chrome is not, so they are **unvalidated**.")
w("")

# ---------------------------------------------------------------- exec summary
sh = B["buckets"]["shipped"]
n_pr = sum(1 for r in sh if r["type"] == "pr"); n_is = sum(1 for r in sh if r["type"] == "issue")
outc = collections.Counter(t["outcome"] for t in B["trains"])
w("## Executive summary"); w("")
w(f"- {n_pr} merged PRs and {n_is} completed issues shipped; {outc.get('shipped',0)} decision trains landed "
  f"({len(B['trains'])} trains in the window: {outc.get('in_flight',0)} in flight, {outc.get('rejected',0)} rejected, {outc.get('abandoned',0)} abandoned).")
if a.summary and os.path.exists(a.summary):
    w(open(a.summary).read().rstrip())
w("")

# ---------------------------------------------------------------- since last installment
S = B.get("series")
if S and not S.get("first_installment"):
    w("## Since last installment"); w("")
    w(f"Compared with the installment for {S['prior']['from']} → {S['prior']['to']} (captured `{S['prior'].get('captured')}`).")
    w("")
    new = S["new"]
    by = collections.Counter(r["bucket"] for r in new)
    w(f"**New this installment:** {len(new)} items ({', '.join(f'{k} {v}' for k,v in sorted(by.items()))}).")
    w("")
    for r in capped(new, "new items"):
        w(ref_line(r, f" — {r['bucket']}"))
    w("")
    co = S["carried_over"]
    by = collections.Counter(r["bucket"] for r in co)
    w(f"**Carried over:** {len(co)} items in flight last installment ({', '.join(f'now {k} {v}' for k,v in sorted(by.items()))}).")
    w("")
    for r in capped(co, "carried-over items"):
        w(ref_line(r, f" — *was {r.get('prior_status')}, now {r['bucket']}*"))
    w("")
    fl = S["forecast_loop"]
    w("**Forecast loop:** " + ("; ".join(f"{k} {len(fl.get(k,[]))}" for k in ("landed","not_yet","dropped"))) +
      " — the previous installment's forecast held no candidates, so there is nothing to compare.")
    for k, lab in (("landed","Landed"),("not_yet","Not yet"),("dropped","Dropped")):
        for l in fl.get(k, []):
            it = item(l["type"], l["id"])
            w(f"- **{lab}:** predicted (tier `{l['tier']}`): [{title_of(l['type'], l['id'])}]({it['url'] if it else ''}) ({l['type']} #{l['id']}).")
    w("")

# ---------------------------------------------------------------- shipped
w("## Shipped this period"); w("")
w("| Title | Ref | Train |"); w("|---|---|---|")
for r in sh:
    w(f"| [{title_of(r['type'], r['id'])}]({r['url']}) | {r['type']} #{r['id']} | `{r.get('train') or '—'}` |")
w("")

# ---------------------------------------------------------------- decision trains
w("## Decision trains"); w("")
order = {"shipped": 0, "in_flight": 1, "rejected": 2, "abandoned": 3}
deep = sorted([t for t in B["trains"] if t["tier"] == "deep"], key=lambda t: (-(len(t["prs"]) + len(t["commits"])), order.get(t["outcome"], 9), t["id"]))
ndir = os.path.join(base, "narratives"); sdir = os.path.join(base, "slices")


def effort_line(t):
    e = t["effort"]; parts = []
    if e.get("merged_at") or t["outcome"] == "shipped":
        parts.append(f"slow to land ({e.get('elapsed_days')} days)" if e.get("slow_to_land") else f"landed in {e.get('elapsed_days')} days")
        parts.append(f"{e.get('reviewers')} reviewers"); parts.append(f"{e.get('participants')} contributors")
    else:
        parts.append(f"open (opened {e.get('opened_at')})")
        if e.get("stalled"):
            parts.append(f"stalled: no activity for {e.get('idle_days')} days")
    rr = sum(((PRS.get(n) or {}).get("review_rounds") or {}).get("count", 0) for n in t["prs"])
    if rr: parts.append(f"{rr} review rounds")
    ro = sum((PRS.get(n) or {}).get("reopen_count", 0) or 0 for n in t["prs"])
    if t.get("root_issue") in ISS: ro += ISS[t["root_issue"]].get("reopen_count", 0) or 0
    if ro: parts.append(f"reopened {ro}×")
    return " · ".join(parts)


def all_urls(o, acc):
    if isinstance(o, dict):
        for v in o.values(): all_urls(v, acc)
    elif isinstance(o, list):
        for v in o: all_urls(v, acc)
    elif isinstance(o, str) and o.startswith("https://"):
        acc.add(o)
    return acc


for t in deep:
    w(f"### Deep train `{t['id']}` — {short_title(t)}"); w("")
    fc = (B["diagrams"].get("train_flowcharts") or {}).get(t["id"])
    if fc: mermaid(fc); trunc_note(t["id"])
    w(""); w(f"*{effort_line(t)}*"); w("")
    w(f"Outcome **{t['outcome']}**; PRs: " + ", ".join(f"[#{n}]({PRS[n]['url']}) ({t['pr_outcomes'].get(str(n))})" for n in t["prs"] if n in PRS) +
      (("; issues: " + ", ".join(f"[#{n}]({ISS[n]['url']})" for n in t["issues"] if n in ISS)) if t["issues"] else "") +
      f"; code areas: {len(t['code_areas'])}" + (f" (`{'`, `'.join(t['code_areas'][:6])}`{' …' if len(t['code_areas'])>6 else ''})" if t['code_areas'] else ""))
    w("")
    npath = os.path.join(ndir, f"{t['id']}.json"); spath = os.path.join(sdir, f"{t['id']}.slice.json")
    if os.path.exists(npath) and os.path.exists(spath):
        N = json.load(open(npath)); allowed = all_urls(json.load(open(spath)), set())
        ev = N.get("evidence") or []
        kept = [e for e in ev if (e.get("url") if isinstance(e, dict) else e) in allowed]
        stats["evidence_total"] += len(ev); stats["evidence_dropped"] += len(ev) - len(kept)
        bylab = collections.defaultdict(list)
        for e in kept:
            if isinstance(e, dict): bylab[e.get("backs") or "summary"].append(e["url"])
        def links(lab):
            u = bylab.get(lab, [])
            return (" — " + ", ".join(f"[{i+1}]({x})" for i, x in enumerate(dict.fromkeys(u)))) if u else ""
        if N.get("summary"): w(f"{N['summary']}{links('summary')}"); w("")
        for lab, key in (("Proposed","proposed"),("Changed","changed"),("Rejected","rejected"),("Shipped","shipped")):
            if N.get(key): w(f"- **{lab}:** {N[key]}{links(key)}")
        w("")
    else:
        stats["narrative_missing"] += 1
        w("*(no narrative: narrator output missing)*"); w("")

w("**Mention trains** — one line each:"); w("")
ment = sorted([t for t in B["trains"] if t["tier"] == "mention"], key=lambda t: (order.get(t["outcome"], 9), t["id"]))
for t in ment:
    u = train_url(t)
    w(f"- `{t['id']}` — [{short_title(t)}]({u}) — {t['outcome']} ({len(t['prs'])} PR(s))")
w("")

# ---------------------------------------------------------------- at a glance
w("## Activity at a glance"); w("")
mermaid(B["diagrams"]["buckets_pie"]); w(""); mermaid(B["diagrams"]["timeline_gantt"]); trunc_note("timeline_gantt"); w("")

# ---------------------------------------------------------------- releases
if B.get("releases"):
    w("## Releases"); w("")
    for r in sorted(B["releases"], key=lambda r: r["published_at"], reverse=True):
        w(f"- **{r.get('name')}** (`{r['tag_name']}`) — published {r['published_at']}{' (prerelease)' if r.get('prerelease') else ''}. [release]({r['url']})")
    w("")

# ---------------------------------------------------------------- CI
ws = {k: v for k, v in B.get("workflow_stats", {}).items() if not k.startswith("_")}
fw = meta.get("facts_windows")
if ws or fw:
    w("## CI/CD health"); w("")
    if fw:
        wsw = fw.get("workflow_stats") or []
        w(f"> CI counts summed over the gathered windows inside this view ({', '.join(x['from']+' → '+x['to'] for x in wsw)}); "
          f"complete: {fw.get('workflow_stats_complete')}. Code graph and owners as of the {fw['structure']['from']} → {fw['structure']['to']} gather.")
    if "_truncated" in B.get("workflow_stats", {}):
        w("> Counts are partial (GitHub's 1,000-run search cap).")
    tot = collections.Counter()
    for k, s in ws.items(): tot.update({x: s.get(x, 0) for x in ("total","success","failure","cancelled")})
    w(f"All workflows: {tot['success']}/{tot['total']} succeeded ({tot['failure']} failed, {tot['cancelled']} cancelled)."); w("")
    for k, s in sorted(ws.items(), key=lambda kv: -kv[1]["total"]):
        w(f"- **{k}** — {s['success']}/{s['total']} succeeded ({s['failure']} failed, {s['cancelled']} cancelled).")
    w("")

# ---------------------------------------------------------------- in flight
inf = B["buckets"]["in_flight"]
if inf:
    w("## In flight"); w("")
    bs = collections.Counter((item(r["type"], r["id"]) or {}).get("board_status") for r in inf)
    for r in capped(inf, "in-flight items"):
        it = item(r["type"], r["id"]) or {}
        w(ref_line(r, (f" — train `{r['train']}`" if r.get("train") else "") + (f" — _{it['board_status']}_" if it.get("board_status") else "")))
    if any(k for k in bs):
        w(""); w("**Board status:** " + ", ".join(f"{k or 'no status'} {v}" for k, v in bs.most_common()))
    w("")

# ---------------------------------------------------------------- rejected
rej = B["buckets"]["rejected"]
if rej:
    w("## Rejected / abandoned"); w("")
    for r in rej: w(ref_line(r))
    w("")

# ---------------------------------------------------------------- stalled
stalled = sorted([t for t in B["trains"] if t["outcome"] == "in_flight" and t["effort"].get("stalled")], key=lambda t: -t["effort"]["idle_days"])
if B["flow"] or B["blockers"] or stalled:
    w("## Stalled, blocked & pile-ups"); w("")
    w("Flow signals at the reference date, not blame.")
    w("")
    if B["flow"]:
        fc = collections.Counter(f["state"] for f in B["flow"])
        w(f"**Flow pathologies** — {len(B['flow'])} open issues: " + ", ".join(f"{k} {v}" for k, v in fc.items()) + ".")
        w("")
        for st in ("blocked","upvoted_but_ignored","traction_then_abandoned","hung"):
            rows = sorted([f for f in B["flow"] if f["state"] == st], key=lambda f: -(f["age_days"] or 0))
            if not rows: continue
            w(f"*{st}* ({len(rows)}):")
            for f in capped(rows, f"{st} issues"):
                w(f"- [{title_of('issue', f['number'])}]({f['url']}) (#{f['number']}) — *{f['state']}* — {' · '.join(f['signals'])}")
            w("")
    if B["blockers"]:
        w("**Top blockers**"); w("")
        for bl in B["blockers"]:
            w(f"- [{title_of('issue', bl['number'])}]({bl['url']}) (#{bl['number']}) — blocks {bl['blocks_count']}: {bl['blocks']}")
        if B["diagrams"].get("blocker_graph"): mermaid(B["diagrams"]["blocker_graph"])
        w("")
    if stalled:
        w(f"**Stalled trains** — {len(stalled)} open trains with no activity for more than 21 days at {meta['ref_date']}:"); w("")
        for t in capped(stalled, "stalled trains"):
            w(f"- `{t['id']}` — {short_title(t)} — no activity for {t['effort']['idle_days']}d (last {t['effort'].get('last_activity_at')}) — [source]({train_url(t)})")
        w("")

# ---------------------------------------------------------------- forecast
fo = B["forecast"]
w("## Next-release forecast"); w("")
w(f"**Next milestone:** {fo.get('next_milestone') or 'none identified'}")
w("")
if not fo.get("candidates"):
    w(f"The forecast has no candidates: `buckets.next_candidates` is empty ({len(B['buckets']['next_candidates'])}).")
else:
    for tier in ("likely","possible","longshot"):
        cs = sorted([c for c in fo["candidates"] if c["tier"] == tier], key=lambda c: -c["score"])
        if not cs: continue
        w(f"### {tier.title()}")
        for c in cs:
            r = c["ref"]
            w(f"- [{title_of(r['type'], r['id'])}]({r['url']}) ({r['type']} #{r['id']})" + (f" — train `{c['train']}`" if c.get("train") else "") +
              f" — *{' · '.join(c['signals'])}*" + (f" — {' · '.join(c['notes'])}" if c.get("notes") else ""))
w("")

# ---------------------------------------------------------------- feature changes
D = B.get("feature_deltas", [])
if D:
    w("## Feature changes (add / drop / change)"); w("")
    mermaid(B["diagrams"]["deltas_bar"]); trunc_note("deltas_bar"); w("")
    kc = collections.Counter((d["kind"], d["subject"]) for d in D)
    w(f"{len(D)} deltas: " + ", ".join(f"{k}/{s} {v}" for (k, s), v in sorted(kc.items())) + ".")
    w("")
    # new / removed module READMEs: the module-level adds and drops
    for kind, lab in (("add","Module READMEs added"),("drop","Module READMEs dropped")):
        rows = [d for d in D if d["kind"] == kind and d["subject"] == "readme" and d.get("area") and d["artifact"] == f"art:{d['area']}/README.md"]
        if rows:
            w(f"**{lab}** ({len(rows)}):")
            for d in rows: w(f"- `{d['area']}` — {d.get('author')} — PR {('#'+str(d['pr'])) if d.get('pr') else '—'} — [{d['commit'][:7]}]({d['url']})")
            w("")
    w("| Kind | Subject | Name | Area | Author | PR | Commit |"); w("|---|---|---|---|---|---|---|")
    rows = sorted([d for d in D if d["subject"] in ("readme","doc") or d["kind"] == "drop"], key=lambda d: (d["kind"], d["subject"], d.get("area") or ""))
    for d in capped(rows, "readme/doc/drop deltas (symbol and comment changes counted above)"):
        w(f"| {d['kind']} | {d['subject']} | {d['name']} | `{d.get('area')}` | {d.get('author')} | {('#'+str(d['pr'])) if d.get('pr') else '—'} | [{d['commit'][:7]}]({d['url']}) |")
    w("")
    mv = (B.get("symbol_moves") or {}).get("links") or []
    if mv:
        w(f"**Moves** ({len(mv)}; `medium` confidence = likely, not certain):")
        for m in capped(mv, "moves"):
            w(f"- `{m['name']}` {m['from_path']} → {m['to_path']} (confidence {m['confidence']})")
        w("")
    sym = [d for d in D if d["subject"] in ("symbol","comment")]
    todo = [d for d in sym if d.get("detail") and " todo " in f" {d['detail']} "]
    cdrop = [d for d in sym if d["subject"] == "comment" and d["kind"] in ("drop","change")]
    w("### Symbol-level changes"); w("")
    w(f"{len(sym)} symbol/comment deltas; {len(todo)} TODO comments, {len(cdrop)} comment drops/changes (listed first).")
    w(""); w("| Detail | Kind | Before | After | PR | Commit |"); w("|---|---|---|---|---|---|")
    def cell(x):
        if not x: return "—"
        x = str(x).replace("|", "\\|").replace("\n", " ").replace("`", "'")
        return "`" + (x[:80] + "…" if len(x) > 80 else x) + "`"
    for d in capped(todo + [d for d in cdrop if d not in todo] + [d for d in sym if d not in todo and d not in cdrop], "symbol/comment deltas"):
        w(f"| {cell(d.get('detail'))} | {d['kind']} | {cell(d.get('before'))} | {cell(d.get('after'))} | {('#'+str(d['pr'])) if d.get('pr') else '—'} | [{d['commit'][:7]}]({d['url']}) |")
    w("")

# ---------------------------------------------------------------- content lifecycle
A = B.get("artifacts") or {}
if A:
    w("## Content lifecycle (built / changed / dropped)"); w("")
    mermaid(B["diagrams"]["content_timeline"]); trunc_note("content_timeline"); w("")
    sc = collections.Counter(x.get("status") for x in A.values())
    w(f"{len(A)} artifacts: " + ", ".join(f"{k} {v}" for k, v in sc.most_common()) + ".")
    w("")
    rows = sorted(A.items(), key=lambda kv: (kv[1].get("status") not in ("removed","replaced"), kv[0]))
    rows = [kv for kv in rows if kv[1].get("status") in ("removed","replaced") or kv[1].get("kind") in ("example","readme")]
    for k, x in capped(rows, "removed/replaced artifacts plus examples and READMEs"):
        evs = ", ".join(f"{e['event']} by {e.get('author')} on {e['date']} ([{e['commit'][:7]}](https://github.com/{meta['owner']}/{meta['repo']}/commit/{e['commit']}))" for e in x.get("lifecycle", [])[:3])
        w(f"- **{x.get('name', k)}** ({x.get('kind')}, `{x.get('path', k)}`) — {x.get('status')}. Lifecycle: {evs}" + (f"; replaced by `{x['replaced_by']}`" if x.get("replaced_by") else ""))
    w("")

# ---------------------------------------------------------------- shipped by area
w("## Shipped by code area"); w("")
grp = collections.defaultdict(list)
w("*Formatter rule: a train touching more than 10 code areas is listed once under \"Cross-cutting\" with its area count, instead of under every area.*"); w("")
cross = []
for r in sh:
    t = TR.get(r.get("train"))
    areas = (t or {}).get("code_areas") or ["Unattributed"]
    if len(areas) > 10: cross.append((r, len(areas))); continue
    for ar in areas: grp[ar].append(r)
if cross:
    w(f"### Cross-cutting (more than 10 areas each)")
    for r, n in cross: w(ref_line(r, f" — train `{r.get('train')}` — {n} areas"))
    w("")
lab = {x["id"]: x.get("label", x["id"]) for x in B["code_graph"]["areas"]}
for ar in sorted(grp, key=lambda k: (k == "Unattributed", -len(grp[k]), k)):
    w(f"### {lab.get(ar, ar)} (`{ar}`)" if ar != "Unattributed" else "### Unattributed")
    for r in grp[ar]: w(ref_line(r, f" — train `{r.get('train')}`"))
    w("")

# ---------------------------------------------------------------- module graph
w("## Module dependency graph / blast radius"); w("")
mermaid(B["diagrams"]["module_graph"]); trunc_note("module_graph")
ex = B["code_graph"].get("edge_extraction")
ne = sum(len(x.get("edges", [])) for x in B["code_graph"]["areas"])
w(""); w(f"{ne} edges over {len(B['code_graph']['areas'])} areas; edge extraction {ex}."); w("")

# ---------------------------------------------------------------- ownership
mods = B.get("modules") or {}
if mods:
    w("## Module ownership"); w("")
    rules = B.get("code_owners") or []
    def owners(area):
        import fnmatch
        res = None; path = area.rstrip("/") + "/"
        for r in rules:
            p = r["pattern"]; pp = p.lstrip("/")
            hit = False
            if p == "*": hit = True
            elif pp.endswith("/") and (path.startswith(pp) if p.startswith("/") else ("/" + path).find("/" + pp) >= 0): hit = True
            elif fnmatch.fnmatch(path.rstrip("/"), pp.rstrip("/")) or fnmatch.fnmatch(path, pp): hit = True
            if hit: res = r["owners"]
        return ", ".join(res) if res else "—"
    w("| Module | Owners (CODEOWNERS) | Top contributors | Commits | PRs | Files |"); w("|---|---|---|---|---|---|")
    ppl = B["people"]
    for k, m in capped(sorted(mods.items(), key=lambda kv: (-kv[1]["commits"], kv[0])), "modules"):
        tc = sorted(l for l, p in ppl.items() if k in (p.get("modules") or []) and not p.get("is_bot"))
        w(f"| `{k}` | {owners(k)} | {', '.join(tc[:4]) + (' …' if len(tc) > 4 else '') or '—'} | {m['commits']} | {m['prs']} | {m['files_changed']} |")
    w(""); w("*Owners resolved by this formatter with a simplified CODEOWNERS matcher (last match wins).*"); w("")
    mermaid(B["diagrams"]["contributor_graph"]); trunc_note("contributor_graph"); w("")

# ---------------------------------------------------------------- issue kinds
w("## Issue kinds"); w("")
kc = collections.Counter(i.get("kind") for i in B["issues"])
w(", ".join(f"{k} {v}" for k, v in kc.most_common()) + f" (of {len(B['issues'])} issues in the view)."); w("")
mermaid(B["diagrams"]["kind_breakdown"]); w("")

# ---------------------------------------------------------------- contributors
P = B["people"]
if P:
    w("## Contributors & community"); w("")
    w("Ranked by `halls.fame` (score = `prs_merged*2 + prs_reviewed + commits_authored`, bots excluded).")
    w("")
    w("| Contributor | Score | Authored | Merged | Reviewed | Commits | Areas | Evidence |"); w("|---|---|---|---|---|---|---|---|")
    for f in B["halls"]["fame"]:
        p = P[f["login"]]
        ex = [pr for pr in B["prs"] if pr["author"] == f["login"] and pr.get("merged")][:2] or [pr for pr in B["prs"] if f["login"] in (pr.get("reviewers") or [])][:2]
        w(f"| `{f['login']}` | {f['score']} | {p['prs_authored']} | {p['prs_merged']} | {p['prs_reviewed']} | {p['commits_authored']} | {len(p.get('areas') or [])} | " +
          ", ".join(f"[#{pr['number']}]({pr['url']})" for pr in ex) + " |")
    w("")
    bots = sorted(l for l, p in P.items() if p.get("is_bot"))
    if bots: w(f"**Automation:** {', '.join('`'+b+'`' for b in bots)}"); w("")

open(a.out, "w").write("\n".join(out) + "\n")
print(f"wrote {a.out}: {len(out)} lines; evidence {stats['evidence_total']} kept {stats['evidence_total']-stats['evidence_dropped']} dropped {stats['evidence_dropped']}; narratives missing {stats['narrative_missing']}", file=sys.stderr)
