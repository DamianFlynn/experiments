"""Offline diagram render: enriched bundle -> Mermaid .mmd files, validated by mmdc.

Pure emitters build the diagram text from existing bundle fields; `mmdc` (mermaid-cli)
compiles every file so a diagram that would not render fails the run. No network.

Size limits. Mermaid refuses large diagrams in two ways (verified with mmdc 12.2.0,
default config): a source longer than `MERMAID_MAX_TEXT` characters renders as an
*error SVG* ("Maximum text size in diagram exceeded") with exit 0, and a flowchart
with more than `MERMAID_MAX_EDGES` edges aborts with exit 1 ("Edge limit exceeded").
Every emitter therefore applies a deterministic cap (a documented top-N key) with an
explicit "+K more" marker in the diagram, and shrinks the cap further if the text
would still exceed `DIAGRAM_TEXT_BUDGET`. What was cut is reported through the
emitter's `notes` dict and recorded in `bundle.diagrams.truncated`."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

INSTALL_HINT = (
    "Install mermaid-cli: `npm install -g @mermaid-js/mermaid-cli`."
)

# Adaptive layout thresholds for emit_train_flowchart:
#   mode C (full, with area nodes) when prs <= MAX_PRS and areas <= MAX_AREAS;
#   mode A (bare issue→PR→outcome only) otherwise.
TRAIN_FLOW_MAX_PRS = 4
TRAIN_FLOW_MAX_AREAS = 5

# --- Mermaid's own limits (mermaid defaults `maxTextSize`, `maxEdges`) ---------------
# Text length is measured as JavaScript string length (UTF-16 code units).
MERMAID_MAX_TEXT = 50000   # > this: error SVG, exit 0 (caught after mmdc too)
MERMAID_MAX_EDGES = 500    # > this: "Edge limit exceeded", exit 1

# --- Our caps (deterministic; each emitter documents its ranking key) ---------------
# Target text size, with headroom under MERMAID_MAX_TEXT. If a capped diagram is still
# longer (e.g. many emoji, which count double), the emitter shrinks its cap by 1/4
# per step until it fits.
DIAGRAM_TEXT_BUDGET = 40000
# Edges drawn by the contributor / module / project-module graphs. Sized so that the
# worst case (2 nodes per edge, 40-char labels, version-labelled edges) is ~35k chars
# and far below MERMAID_MAX_EDGES; a 120-edge flowchart is also the practical limit
# of what a reader can follow.
GRAPH_MAX_EDGES = 120
# Gantt rows: PR tasks (~100 chars each -> ~20k chars) and release milestones.
GANTT_MAX_PRS = 200
GANTT_MAX_RELEASES = 30
# Content timeline: dated periods, and events listed per period.
TIMELINE_MAX_DAYS = 40
TIMELINE_MAX_EVENTS_PER_DAY = 8
# Train flowchart PR nodes (mode A draws 2 edges per PR -> <= 122 edges).
TRAIN_FLOW_MAX_PR_NODES = 60
# Issue-kind pie slices, including the "+K more kinds" slice.
PIE_MAX_SLICES = 10
# Blocker graph edges (unchanged from the original hard-coded default).
BLOCKER_MAX_EDGES = 40


def _mermaid_len(text):
    """Length as Mermaid measures it (JS string length = UTF-16 code units)."""
    return len((text or "").encode("utf-16-le")) // 2


def _note(notes, shown, total, unit):
    """Record a truncation in the caller's `notes` dict (no-op when nothing cut)."""
    if notes is not None and shown < total:
        notes.update({"shown": shown, "total": total, "unit": unit})


def _fit(build, notes):
    """Run `build(scale, notes)` with scale 1.0, then shrink (x0.75 per step) until
    the text fits DIAGRAM_TEXT_BUDGET. Deterministic: same input -> same scale.
    Each emitter turns `scale` into caps via `_cap(CONST, scale)`."""
    scale = 1.0
    for _ in range(40):
        local = {}
        text = build(scale, local)
        if _mermaid_len(text) <= DIAGRAM_TEXT_BUDGET:
            break
        scale *= 0.75
    if notes is not None:
        notes.update(local)
    return text


def _cap(limit, scale):
    return max(1, int(limit * scale))


_LINE_BREAKS = "\u0085\u2028\u2029"   # NEL and the Unicode line/paragraph separators


def _one_line(text):
    """Replace CR, LF, CRLF, TAB, other control characters and the Unicode line
    separators with one space each. Mermaid ends a statement at a line break,
    and a bare CR breaks gantt parsing."""
    text = "" if text is None else str(text)
    return "".join(" " if (ord(ch) < 32 or ord(ch) == 127 or ch in _LINE_BREAKS)
                   else ch for ch in text.replace("\r\n", "\n"))


# Characters an untrusted label may not carry into ANY diagram: a `"` ends a
# quoted flowchart / pie label; a backtick right after the opening quote turns
# a label into a Markdown string; `<` and `>` would be read as HTML by
# flowchart labels (DOMPurify strips scripts, but not every tag).
_LABEL_CHARS = str.maketrans({'"': "'", "`": "'", "<": "\u2039", ">": "\u203a"})
# Mermaid entity codes (`#quot;`, `#60;`) are decoded inside labels; the `#` of
# such a sequence is replaced with a fullwidth `＃` so the text shows as typed.
_ENTITY_RE = re.compile(r"#(?=[A-Za-z0-9]+;)")


def _mermaid_text(text):
    """THE sanitiser for untrusted label text (#110 item 3): every emitter and
    every label kind (node labels, subgraph titles, edge labels, pie slices and
    titles, gantt task and title text, timeline events) goes through it, and
    the per-kind helpers below only add what their diagram type needs on top
    (gantt / timeline replace `:`, caps).

    - one line (_one_line);
    - no `%%`: any run of `%` collapses to one, so neither a `%%{...}%%`
      directive (which Mermaid extracts from ANYWHERE in the source and which
      changes the diagram's configuration, or fails the render) nor a `%%`
      comment can be formed; a single `%` (`50% done`) stays;
    - `"` and backticks become `'`, `<` / `>` become `‹` / `›`;
    - an entity-code `#name;` keeps its text but cannot be decoded.
    Words, digits and non-ASCII text are kept, so a label stays readable.
    Surrounding whitespace is stripped; the result may be empty."""
    clean = _one_line(text)
    clean = re.sub(r"%{2,}", "%", clean)
    clean = _ENTITY_RE.sub("\uff03", clean.translate(_LABEL_CHARS))
    return clean.strip()


_PIE_ROWS = [
    ("Shipped", "shipped"),
    ("In flight", "in_flight"),
    ("Rejected", "rejected"),
    ("Next candidates", "next_candidates"),
]


def emit_buckets_pie(bundle):
    """A Mermaid `pie` of bucket counts. Zero-count slices are dropped."""
    meta = bundle.get("meta", {})
    buckets = bundle.get("buckets", {})
    lines = ["pie showData",
             "    title Work by status ({} → {})".format(
                 _mermaid_text(meta.get("from")), _mermaid_text(meta.get("to")))]
    any_slice = False
    for label, key in _PIE_ROWS:
        count = len(buckets.get(key, []))
        if count:
            lines.append(f'    "{label}" : {count}')
            any_slice = True
    if not any_slice:
        lines.append('    "No activity" : 1')
    return "\n".join(lines) + "\n"


def _day(ts, default):
    return (ts or default or "")[:10]


def _gantt_label(text):
    """A gantt task name: the shared sanitiser (_mermaid_text) plus ':' (the
    field separator) replaced. Capped at 60 characters.

    A task line must not START with free text: the gantt lexer reads a line
    beginning with a keyword (`section`, `title`, `dateFormat`, `excludes`,
    `todayMarker`, `click`, `accTitle`, ... — case-insensitive) as that
    statement, and one beginning with `%` or `x%` as a comment. Every row
    therefore starts with a fixed prefix: `#<n> ` for a PR, `Release ` for a
    release (#110 item 4), and the fixed texts of the marker rows."""
    return _mermaid_text(text).replace(":", " -").strip()[:60] or "item"


def _pr_created(p):
    return p.get("created_at") or p.get("merged_at") or ""


def _num_key(value):
    """Sort key for a PR/issue number that tolerates missing/non-int values."""
    return (0, value, "") if isinstance(value, int) else (1, 0, str(value))


def emit_timeline_gantt(bundle, notes=None):
    """A Mermaid `gantt` of PR lifespans + releases across the window.

    Capped at GANTT_MAX_PRS PR rows — the most recently created (created_at desc,
    then number desc) — plus a "+K more PRs not shown" row; and at
    GANTT_MAX_RELEASES releases (most recently published). Rows are then drawn in
    chronological order as before."""
    return _fit(lambda scale, n: _gantt(bundle, scale, n), notes)


def _gantt(bundle, scale, notes):
    meta = bundle.get("meta", {})
    frm = meta.get("from") or "1970-01-01"
    to = meta.get("to") or frm
    lines = [
        "gantt",
        "    title Timeline ({} → {})".format(_mermaid_text(frm), _mermaid_text(to)),
        "    dateFormat YYYY-MM-DD",
        "    axisFormat %m-%d",
    ]
    all_prs = bundle.get("prs", [])
    pr_cap = _cap(GANTT_MAX_PRS, scale)
    keep = sorted(all_prs, key=lambda p: (_pr_created(p), _num_key(p.get("number"))),
                  reverse=True)[:pr_cap]
    prs = sorted(keep, key=lambda p: (_pr_created(p), _num_key(p.get("number"))))
    pr_omitted = len(all_prs) - len(prs)
    all_releases = bundle.get("releases", [])
    rel_cap = _cap(GANTT_MAX_RELEASES, scale)
    rel_key = lambda r: (r.get("published_at") or "", r.get("tag_name") or "",  # noqa: E731
                         r.get("name") or "")
    releases = sorted(sorted(all_releases, key=rel_key, reverse=True)[:rel_cap],
                      key=rel_key) if len(all_releases) > rel_cap else all_releases
    rel_omitted = len(all_releases) - len(releases)
    if pr_omitted and rel_omitted:
        _note(notes, len(prs) + len(releases), len(all_prs) + len(all_releases),
              "PRs+releases")
    elif pr_omitted:
        _note(notes, len(prs), len(all_prs), "PRs")
    elif rel_omitted:
        _note(notes, len(releases), len(all_releases), "releases")
    if prs:
        lines.append("    section Pull requests")
        for p in prs:
            start = _day(p.get("created_at"), frm)
            end = _day(p.get("merged_at") or p.get("closed_at"), to)
            if end < start:
                end = start
            # merged / closed unmerged / open; a PR whose state is unknown
            # gets no status tag rather than falling through to "active".
            if p.get("merged"):
                status = "done, "
            elif p.get("state") == "closed":
                status = "crit, "
            elif p.get("state") == "open":
                status = "active, "
            else:
                status = ""
            label = _gantt_label(f"#{p.get('number', '?')} {p.get('title') or ''}")
            lines.append(f"    {label} :{status}{start}, {end}")
        if pr_omitted:
            lines.append(f"    +{pr_omitted} more PRs not shown :active, "
                         f"{_day(frm, '1970-01-01')}, {_day(to, frm)}")
    if releases:
        lines.append("    section Releases")
        for r in releases:
            day = _day(r.get("published_at"), to)
            # `Release ` prefix: a release name is free text and must never be
            # read as a gantt keyword (#110 item 4).
            label = _gantt_label("Release {}".format(
                r.get("name") or r.get("tag_name") or "(unnamed)"))
            lines.append(f"    {label} :milestone, {day}, 0d")
        if rel_omitted:
            lines.append(f"    +{rel_omitted} more releases not shown :milestone, "
                         f"{_day(to, frm)}, 0d")
    if not prs and not releases:
        lines.append("    section Activity")
        lines.append(f"    No dated items :active, {_day(frm, '2026-01-01')}, {_day(to, frm)}")
    return "\n".join(lines) + "\n"


def _timeline_text(text):
    """A `timeline` event: the shared sanitiser (_mermaid_text) plus ':' (the
    period / event separator) replaced. Capped at 60 characters. An event
    follows `: ` on its period's line, so it never starts a statement."""
    return _mermaid_text(text).replace(":", " -").strip()[:60] or "event"


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _timeline_period(date):
    """A timeline period label: the event's `YYYY-MM-DD` day, else `undated`.
    A period starts its line, where the timeline lexer reads `#` and `%` as
    comments, so free text is never used here."""
    day = (date or "")[:10] if isinstance(date, str) else ""
    return day if _DAY_RE.fullmatch(day) else "undated"


def emit_content_timeline(bundle, notes=None):
    """A Mermaid `timeline` of artifact lifecycle events (built/changed/dropped),
    one period per date with its events. Derived from `artifacts`.

    Capped at the TIMELINE_MAX_DAYS latest periods (sorted date order; an
    `earlier : +K more events on M earlier dates` period replaces the rest) and at
    TIMELINE_MAX_EVENTS_PER_DAY events per period (the first ones by artifact
    id, then lifecycle position — never the key order of `artifacts`, #100 —
    then a `+K more events` event)."""
    return _fit(lambda scale, n: _content_timeline(bundle, scale, n), notes)


def _content_timeline(bundle, scale, notes):
    meta = bundle.get("meta", {})
    lines = ["timeline",
             "    title Content lifecycle ({} - {})".format(
                 _mermaid_text(meta.get("from")), _mermaid_text(meta.get("to")))]
    by_date = {}
    verb = {"add": "built", "change": "changed", "remove": "dropped"}
    arts = bundle.get("artifacts", {})
    for aid in sorted(arts):
        art = arts[aid]
        for ev in art.get("lifecycle", []):
            day = _timeline_period(ev.get("date"))
            evt = ev.get("event", "")
            label = _timeline_text(
                f"{verb.get(evt, evt)} {art.get('name', '?')} "
                f"({art.get('kind', '?')}) by {ev.get('author') or '?'}")
            by_date.setdefault(day, []).append(label)
    if not by_date:
        lines.append("    {} : no content events".format(
            _timeline_period(meta.get("from")) if meta.get("from") else "—"))
        return "\n".join(lines) + "\n"
    total = sum(len(v) for v in by_date.values())
    days = sorted(by_date)
    day_cap = _cap(TIMELINE_MAX_DAYS, scale)
    per_day = _cap(TIMELINE_MAX_EVENTS_PER_DAY, scale)
    dropped_days = days[:-day_cap] if len(days) > day_cap else []
    shown = 0
    if dropped_days:
        k = sum(len(by_date[d]) for d in dropped_days)
        lines.append(f"    earlier : +{k} more events on {len(dropped_days)} "
                     f"earlier dates")
    for day in days[len(dropped_days):]:
        events = by_date[day][:per_day]
        shown += len(events)
        extra = len(by_date[day]) - len(events)
        if extra:
            events = events + [f"+{extra} more events"]
        lines.append(f"    {day} : {' : '.join(events)}")
    _note(notes, shown, total, "events")
    return "\n".join(lines) + "\n"


_DELTA_KINDS = ["add", "drop", "change"]


def emit_deltas_bar(bundle):
    """A Mermaid `xychart-beta` bar of feature_delta counts by kind.

    Mermaid has no standalone bar diagram; `xychart-beta` is its native bar chart
    (category x-axis + numeric bar series), which renders counts-by-category
    correctly — unlike `pie`, which would read as proportions and lose ordering."""
    counts = {k: 0 for k in _DELTA_KINDS}
    for d in bundle.get("feature_deltas", []):
        if d.get("kind") in counts:
            counts[d["kind"]] += 1
    values = [counts[k] for k in _DELTA_KINDS]
    top = max(values) or 1
    lines = [
        "xychart-beta",
        '    title "Feature changes by kind"',
        '    x-axis [add, drop, change]',
        f'    y-axis "Count" 0 --> {top}',
        f"    bar [{', '.join(str(v) for v in values)}]",
    ]
    return "\n".join(lines) + "\n"


_ID_PLAIN_MAX = 32   # plain ASCII-alnum keys up to this length are used verbatim
_ID_HEAD_MAX = 24    # readable head kept in front of the hash for all other keys


def _stable_node_id(prefix, text):
    """A deterministic, collision-resistant Mermaid node id for arbitrary text.

    Two disjoint shapes (the prefix is always ASCII alnum):
      * `<prefix>_<text>` when `text` is 1..32 ASCII letters/digits (e.g. `p_alice`,
        `pr_42`) — readable, and injective because nothing was changed;
      * `<prefix>_<sanitised head, ≤24>_<sha1(text)[:8]>` otherwise. Sanitising
        (`a-b`, `a/b`, `a_b` -> `a_b`) and truncation (long AVM paths sharing a
        prefix) can no longer merge nodes because the hash covers the FULL text.
    The first shape has no `_` after the prefix and the second always has one, so
    they cannot coincide. Within a diagram, `_NodeIds` additionally guarantees
    uniqueness even against a hash collision."""
    text = "" if text is None else str(text)
    if 0 < len(text) <= _ID_PLAIN_MAX and all(
            ch.isascii() and ch.isalnum() for ch in text):
        return f"{prefix}_{text}"
    safe = "".join(ch if (ch.isascii() and ch.isalnum()) else "_" for ch in text)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{safe[:_ID_HEAD_MAX]}_{digest}"


class _NodeIds:
    """Per-diagram id allocator: the same (prefix, text) always gets the same id,
    and two different texts never share one: every id handed out is remembered, and
    a clash (only possible on an 8-hex hash collision) is resolved with a `_2`,
    `_3`, ... suffix until the id is unused. Deterministic given the call order."""

    def __init__(self):
        self._by_key = {}
        self._used = set()

    def __call__(self, prefix, text):
        key = (prefix, "" if text is None else str(text))
        nid = self._by_key.get(key)
        if nid is None:
            base = nid = _stable_node_id(prefix, text)
            n = 1
            while nid in self._used:
                n += 1
                nid = f"{base}_{n}"
            self._by_key[key] = nid
            self._used.add(nid)
        return nid


def _area_tail(area):
    return (area or "").rstrip("/").split("/")[-1] or area


def _flow_label(text):
    """A flowchart node or edge label (drawn inside `"..."`): the shared
    sanitiser (_mermaid_text), capped at 40 characters."""
    return _mermaid_text(text)[:40] or "?"


def _subgraph_label(text):
    """A subgraph TITLE: the shared sanitiser (_mermaid_text). Unlike
    _flow_label this is UNCAPPED by design: a subgraph title is a repo identity
    (e.g. 'Azure/terraform-azurerm-avm-res-keyvault-vault', 46 chars) and
    truncating it would hide which member the blast radius points at."""
    return _mermaid_text(text) or "?"


def _more_node(omitted, what="edges"):
    """The explicit overflow marker node for a capped flowchart. Its id `more`
    cannot clash with an allocated id (those always carry a `<prefix>_`)."""
    return f'    more["+{omitted} more {what} not shown"]'


def emit_contributor_graph(bundle, notes=None):
    """A Mermaid `flowchart` of people <-> code-area edges.

    Each person links to the areas they authored/reviewed in (from `people.modules`,
    falling back to `people.areas`). Emits a `No contributor data` placeholder when
    no edges can be built. Derived from existing bundle fields.

    Capped at GRAPH_MAX_EDGES edges, chosen breadth-first so every contributor
    keeps an edge before anyone gets a second: key = (position of the area in the
    person's list, login, area). A `+K more edges not shown` node marks the cut."""
    return _fit(lambda scale, n: _contributor_graph(bundle, scale, n), notes)


def _contributor_graph(bundle, scale, notes):
    people = bundle.get("people", {})
    lines = ["flowchart LR"]
    ranked = []
    for login in sorted(people):
        p = people[login] or {}
        mods = p.get("modules") or p.get("areas") or []
        seen = set()
        for area in mods:
            if area in seen:
                continue
            seen.add(area)
            ranked.append((len(seen) - 1, login, area))
    if not ranked:
        lines.append("    none[No contributor data]")
        return "\n".join(lines) + "\n"
    ranked.sort()
    cap = _cap(GRAPH_MAX_EDGES, scale)
    kept = ranked[:cap]
    _note(notes, len(kept), len(ranked), "edges")
    ids = _NodeIds()
    logins = sorted({login for _, login, _ in kept})
    areas = sorted({area for _, _, area in kept})
    person_nodes = {ids("p", login): login for login in logins}
    area_nodes = {ids("a", area): _area_tail(area) for area in areas}
    for pid, login in sorted(person_nodes.items()):
        lines.append(f'    {pid}["{_flow_label(login)}"]')
    for aid, label in sorted(area_nodes.items()):
        lines.append(f'    {aid}("{_flow_label(label)}")')
    for pid, aid in sorted((ids("p", login), ids("a", area)) for _, login, area in kept):
        lines.append(f"    {pid} --> {aid}")
    if len(ranked) > len(kept):
        lines.append(_more_node(len(ranked) - len(kept)))
    return "\n".join(lines) + "\n"


def _pie_label(text):
    """A pie slice label (inside `"..."`; a `"` or a line break in it makes mmdc
    emit a 'Syntax error' SVG with exit 0): the shared sanitiser
    (_mermaid_text), capped at 60 characters."""
    return _mermaid_text(text)[:60] or "?"


def emit_kind_breakdown(bundle, notes=None):
    """A Mermaid `pie` of issues by `kind` (feature/bug/idea/...). The at-a-glance
    kind mix; proportions are the point, so `pie` (the spec palette entry).

    Capped at PIE_MAX_SLICES slices: the largest kinds (count desc, then name) and
    one `+K more kinds` slice carrying the remaining issues, so totals still add up."""
    counts = {}
    for issue in bundle.get("issues", []):
        kind = issue.get("kind") or "other"
        counts[kind] = counts.get(kind, 0) + 1
    lines = ["pie showData", "    title Issues by kind"]
    if not counts:
        lines.append('    "No issues" : 1')
        return "\n".join(lines) + "\n"
    ordered = sorted(counts, key=lambda k: (-counts[k], str(k)))
    shown = ordered if len(ordered) <= PIE_MAX_SLICES else ordered[:PIE_MAX_SLICES - 1]
    for kind in shown:
        lines.append(f'    "{_pie_label(kind)}" : {counts[kind]}')
    rest = ordered[len(shown):]
    if rest:
        lines.append(f'    "+{len(rest)} more kinds" : '
                     f'{sum(counts[k] for k in rest)}')
        _note(notes, len(shown), len(ordered), "kinds")
    return "\n".join(lines) + "\n"


# The PR outcome (link `train.pr_outcomes`) that decides each train outcome.
_TRAIN_DECIDING_PR_OUTCOME = {"shipped": "merged", "in_flight": "open",
                              "rejected": "rejected"}

# The outcome node's label for each train outcome link.build_trains sets
# ("shipped" adds its milestone). Any other value is drawn as itself, never
# as one of these (#110 item 2).
_TRAIN_OUTCOME_LABEL = {"shipped": "Shipped", "in_flight": "In flight",
                        "rejected": "Rejected",
                        "abandoned": "Abandoned (not planned)"}


def emit_train_flowchart(bundle, train, notes=None):
    """A Mermaid `flowchart` telling the life story of one train.

    Outcome mapping (#110 item 2): 'shipped' -> 'Shipped [-> <milestone>]';
    'in_flight' -> 'In flight'; 'rejected' -> 'Rejected'; 'abandoned' ->
    'Abandoned (not planned)'; any other value is drawn as its own text
    ('Unknown outcome' when empty), never as one of these. When no PR node leads
    to the outcome (an abandoned train has no PRs), the issue node does.
    Every PR of the train is drawn (issue #93); one whose own outcome
    (`train.pr_outcomes`) is not the one that decided the train's is labelled
    `<outcome>: <title>` (e.g. `rejected: ...` in a shipped train) and has no
    edge to the outcome node.

    Adaptive layout:
      mode C (default): PR nodes annotated with distinct code_area nodes when
        len(prs) <= TRAIN_FLOW_MAX_PRS and len(distinct code_areas) <= TRAIN_FLOW_MAX_AREAS.
      mode A: bare issue -> PR -> outcome chain when either threshold is exceeded.

    Capped at TRAIN_FLOW_MAX_PR_NODES PR nodes (the first ones in the train's own
    `prs` order); the rest collapse into one `+K more PRs not shown` node wired into
    the same issue -> ... -> outcome chain.
    """
    return _fit(lambda scale, n: _train_flowchart(bundle, train, scale, n), notes)


def _train_flowchart(bundle, train, scale, notes):
    issues_by_num = {i["number"]: i for i in bundle.get("issues", [])}
    prs_by_num = {p["number"]: p for p in bundle.get("prs", [])}

    train_prs = train.get("prs", [])
    code_areas = train.get("code_areas", [])
    outcome = train.get("outcome", "")

    # Determine mode using DISTINCT non-empty areas so duplicates don't
    # wrongly push over the threshold.
    distinct_areas = set(a for a in code_areas if a)
    mode_c = (len(train_prs) <= TRAIN_FLOW_MAX_PRS and
              len(distinct_areas) <= TRAIN_FLOW_MAX_AREAS)

    lines = ["flowchart LR"]
    ids = _NodeIds()

    # --- issue node (when root_issue present) ---
    root_issue_num = train.get("root_issue")
    issue_node_id = None
    if root_issue_num is not None:
        issue = issues_by_num.get(root_issue_num, {})
        issue_title = issue.get("title") or f"Issue #{root_issue_num}"
        issue_node_id = ids("iss", str(root_issue_num))
        lines.append(f'    {issue_node_id}["{_flow_label(issue_title)}"]')

    # --- PR nodes (capped; the rest collapse into one marker node) ---
    pr_cap = _cap(TRAIN_FLOW_MAX_PR_NODES, scale)
    shown_prs = train_prs[:pr_cap]
    omitted = len(train_prs) - len(shown_prs)
    _note(notes, len(shown_prs), len(train_prs), "PRs")
    # A train holds every attempt (issue #93). An attempt whose own outcome is
    # not the one that decided the train's (e.g. a rejected PR in a shipped
    # train) is labelled with it and draws no edge to the outcome node.
    pr_outcomes = train.get("pr_outcomes") or {}
    deciding = _TRAIN_DECIDING_PR_OUTCOME.get(outcome)
    pr_node_ids = []      # every shown PR node (issue -> PR edges)
    to_outcome = []       # the PR nodes that lead to the outcome node
    for pr_num in shown_prs:
        pr = prs_by_num.get(pr_num, {})
        pr_title = pr.get("title") or f"PR #{pr_num}"
        pr_id = ids("pr", str(pr_num))
        pr_node_ids.append(pr_id)
        own = pr_outcomes.get(str(pr_num))
        if own and deciding and own != deciding:
            pr_title = f"{own}: {pr_title}"
        else:
            to_outcome.append(pr_id)
        lines.append(f'    {pr_id}["{_flow_label(pr_title)}"]')
    if omitted:
        lines.append(_more_node(omitted, "PRs"))
        pr_node_ids.append("more")
        to_outcome.append("more")

    # --- outcome node ---
    if outcome == "shipped":
        # best-effort: take first non-empty PR milestone
        milestone = None
        for pr_num in train_prs:
            ms = (prs_by_num.get(pr_num) or {}).get("milestone")
            if ms:
                milestone = ms
                break
        if milestone:
            # The outer _flow_label on outcome_label (below) sanitises/truncates the
            # whole string, so don't pre-truncate the milestone here (double-capping
            # would silently shrink the visible name).
            outcome_label = f"Shipped → {milestone}"
        else:
            outcome_label = "Shipped"
    elif outcome in _TRAIN_OUTCOME_LABEL:
        outcome_label = _TRAIN_OUTCOME_LABEL[outcome]
    else:
        outcome_label = str(outcome) if outcome else "Unknown outcome"

    outcome_node_id = ids("out", train.get("id", "train"))
    lines.append(f'    {outcome_node_id}(["{_flow_label(outcome_label)}"])')

    # --- edges: issue -> PRs ---
    if issue_node_id:
        for pr_id in pr_node_ids:
            lines.append(f"    {issue_node_id} --> {pr_id}")
    # If PR-only train and no issue node, PRs are the start nodes (no extra edge needed)

    # --- edges: PRs -> outcome (only the attempts that decided it); with no
    # such PR (an abandoned train has none) the issue leads to the outcome ---
    for pr_id in to_outcome:
        lines.append(f"    {pr_id} --> {outcome_node_id}")
    if not to_outcome and issue_node_id:
        lines.append(f"    {issue_node_id} --> {outcome_node_id}")

    # --- mode C: area annotation nodes and edges ---
    if mode_c and distinct_areas:
        area_nodes = {}
        for area in sorted(distinct_areas):
            aid = ids("area", area)
            area_nodes[aid] = _area_tail(area)
        for aid, label in sorted(area_nodes.items()):
            lines.append(f'    {aid}("{_flow_label(label)}")')
        # code_areas is per-train (not per-PR in the bundle), so every PR links to
        # every train area — the finest granularity the schema supports.
        for pr_id in pr_node_ids:
            for aid in sorted(area_nodes):
                lines.append(f"    {pr_id} --> {aid}")

    return "\n".join(lines) + "\n"


def _rank_by_target_indegree(edges, cap):
    """Top-`cap` of distinct (src, dst, label) edges, most depended-upon target
    first: key = (-in-degree of dst over distinct src, src, dst, label). The
    blast-radius hubs survive the cut; ties break on the full texts."""
    indeg = {}
    for src, dst in {(s, d) for s, d, _ in edges}:
        indeg[dst] = indeg.get(dst, 0) + 1
    ranked = sorted(edges, key=lambda e: (-indeg[e[1]], e[0], e[1], e[2]))
    return ranked[:cap]


def _edge_line(src_id, dst_id, label, indent="    "):
    if label:
        # quote the edge label so markers with Mermaid-special chars (e.g. the
        # `child[]` multi-instance marker) don't break the flowchart parser.
        return f'{indent}{src_id} -->|"{_flow_label(label)}"| {dst_id}'
    return f"{indent}{src_id} --> {dst_id}"


def emit_module_graph(bundle, notes=None):
    """A Mermaid `flowchart` of resolved area->area dependency edges (blast radius).

    Reads `code_graph.areas[].edges` (Phase 3c). Each resolved edge draws
    source-area --> target-area, labelled with the version when present. Emits a
    `No module dependencies` placeholder when no resolved edges exist. Derived
    solely from existing bundle fields.

    Capped at GRAPH_MAX_EDGES edges ranked by target in-degree (see
    `_rank_by_target_indegree`), with a `+K more edges not shown` node."""
    return _fit(lambda scale, n: _module_graph(bundle, scale, n), notes)


def _module_graph(bundle, scale, notes):
    areas = (bundle.get("code_graph") or {}).get("areas", [])
    lines = ["flowchart LR"]
    drawn = set()
    for area in areas:
        for edge in area.get("edges", []):
            if not edge.get("resolved") or edge.get("to") is None:
                continue
            if edge.get("local"):
                # private child submodule: mark multi-instance arrays as child[].
                label = "child[]" if edge.get("instances") == "many" else "child"
            else:
                label = edge.get("version") or ("transitive" if edge.get("transitive") else "")
            drawn.add((str(area["id"]), str(edge["to"]), str(label)))
    if not drawn:
        lines.append("    none[No module dependencies]")
        return "\n".join(lines) + "\n"
    kept = _rank_by_target_indegree(drawn, _cap(GRAPH_MAX_EDGES, scale))
    _note(notes, len(kept), len(drawn), "edges")
    ids = _NodeIds()
    nodes = {ids("m", t): _area_tail(t)
             for t in sorted({s for s, _, _ in kept} | {d for _, d, _ in kept})}
    for nid, label in sorted(nodes.items()):
        lines.append(f'    {nid}("{_flow_label(label)}")')
    for src_id, dst_id, label in sorted((ids("m", s), ids("m", d), lab)
                                        for s, d, lab in kept):
        lines.append(_edge_line(src_id, dst_id, label))
    if len(drawn) > len(kept):
        lines.append(_more_node(len(drawn) - len(kept)))
    return "\n".join(lines) + "\n"


def emit_blocker_graph(bundle, max_edges=BLOCKER_MAX_EDGES, notes=None):
    """A Mermaid `flowchart` over the issues' surfaced `blocks` edges (Phase 11).

    Each issue that participates in a dependency becomes a node labelled `#<n>`;
    every `blocks` relation draws blocker --> blocked (fold normalized the edge
    direction, and extract surfaces it as `issue["blocking"]` = the issues this one
    blocks).

    Open only, the flow rule (#60, #101): a relation is drawn only when the
    blocker AND the blocked issue are bundle issues with `state == "open"` —
    exactly the relation `blockers[]` counts. A closed (resolved) blocker no
    longer blocks and is omitted, as are closed dependants and issues absent from
    the bundle (state unknown). Emits a `No blocked issues` placeholder when no
    such relation exists. Bounded: at most `max_edges` (BLOCKER_MAX_EDGES) edges
    are drawn, the first in (blocker, blocked) number order, with a `+K more
    blocked` node when more exist. Derived solely from existing bundle fields."""
    issues = bundle.get("issues") or []
    open_issues = {iss.get("number") for iss in issues
                   if iss.get("state") == "open"}
    # Collect open blocker->open blocked pairs from the resolved `blocking` lists;
    # sorted + deduped so the diagram is deterministic regardless of order.
    edges = sorted({
        (iss["number"], blocked)
        for iss in issues
        if iss.get("number") is not None and iss["number"] in open_issues
        for blocked in (iss.get("blocking") or [])
        if blocked in open_issues
    })
    lines = ["flowchart LR"]
    if not edges:
        lines.append("    none[No blocked issues]")
        return "\n".join(lines) + "\n"
    overflow = len(edges) - max_edges
    drawn = edges[:max_edges]
    _note(notes, len(drawn), len(edges), "edges")
    ids = _NodeIds()
    nodes = {}
    for blocker, blocked in drawn:
        for n in (blocker, blocked):
            nodes[ids("i", str(n))] = "#{}".format(n)
    for nid, label in sorted(nodes.items()):
        lines.append(f'    {nid}("{_flow_label(label)}")')
    for blocker, blocked in drawn:
        lines.append(f"    {ids('i', str(blocker))} --> {ids('i', str(blocked))}")
    if overflow > 0:
        lines.append(f"    more[+{overflow} more blocked]")
    return "\n".join(lines) + "\n"


def emit_project_module_graph(module_edges, notes=None):
    """A Mermaid flowchart of project module dependencies (Slice S4 blast radius).
    Nodes are grouped into a subgraph per member repo; each edge draws
    src-area --> dst-area labelled with its version (or 'transitive'). Cross-repo
    edges are the point of the diagram; intra-repo edges are included for context.

    A PROJECT-level emitter: the digest report flow calls it with
    `digest.project_depends_on(...)` rows (NOT the single-repo `render()` dict,
    which is bundle-scoped). Pure; deterministic — it sorts repos, nodes, and
    edges internally, so input order does not matter.

    Capped at GRAPH_MAX_EDGES edges ranked by target in-degree (see
    `_rank_by_target_indegree`), with a `+K more edges not shown` node; pass a
    `notes` dict to learn what was cut."""
    if not module_edges:
        return "flowchart LR\n    none[No cross-repo module dependencies]\n"
    return _fit(lambda scale, n: _project_module_graph(module_edges, scale, n), notes)


def _project_module_graph(module_edges, scale, notes):
    drawn = set()
    for e in module_edges:
        # cross_repo intentionally unused here: all edges are drawn uniformly
        # (visual styling of cross- vs intra-repo edges is future work).
        label = e.get("version") or ("transitive" if e.get("transitive") else "")
        drawn.add(((str(e["src_repo"]), str(e["src_area"])),
                   (str(e["dst_repo"]), str(e["dst_area"])), str(label)))
    kept = _rank_by_target_indegree(drawn, _cap(GRAPH_MAX_EDGES, scale))
    _note(notes, len(kept), len(drawn), "edges")
    ids = _NodeIds()

    def nid(key):
        # long AVM {repo}::{area} keys share a long prefix; the hashed id keeps
        # them distinct.
        return ids("m", "{}::{}".format(*key))

    by_repo = {}
    for key in sorted({s for s, _, _ in kept} | {d for _, d, _ in kept}):
        # Label with the full area path (not _area_tail): two modules in one repo
        # can both end in `main.tf`; the path disambiguates them in the subgraph.
        by_repo.setdefault(key[0], {})[nid(key)] = key[1]
    lines = ["flowchart LR"]
    for repo in sorted(by_repo):
        # Subgraph title uses the UNCAPPED _subgraph_label (repo identity must not
        # be truncated); node area labels keep _flow_label's 40-char cap.
        lines.append('    subgraph {}["{}"]'.format(ids("r", repo),
                                                     _subgraph_label(repo)))
        for node_id, area in sorted(by_repo[repo].items()):
            lines.append('        {}("{}")'.format(node_id, _flow_label(area)))
        lines.append("    end")
    for src, dst, label in sorted((nid(s), nid(d), lab) for s, d, lab in kept):
        lines.append(_edge_line(src, dst, label))
    if len(drawn) > len(kept):
        lines.append(_more_node(len(drawn) - len(kept)))
    return "\n".join(lines) + "\n"


def _render_all(bundle):
    """(name -> Mermaid text, name -> truncation note) for every flat diagram."""
    # buckets_pie (<= 4 slices) and deltas_bar (3 bars) are fixed-size; every
    # other emitter is capped and reports cuts through `notes`.
    emitters = [
        ("buckets_pie", lambda b, notes: emit_buckets_pie(b)),
        ("timeline_gantt", emit_timeline_gantt),
        ("content_timeline", emit_content_timeline),
        ("deltas_bar", lambda b, notes: emit_deltas_bar(b)),
        ("contributor_graph", emit_contributor_graph),
        ("kind_breakdown", emit_kind_breakdown),
        ("module_graph", emit_module_graph),
        ("blocker_graph", emit_blocker_graph),
    ]
    texts, truncated = {}, {}
    for name, emit in emitters:
        notes = {}
        texts[name] = emit(bundle, notes=notes)
        if notes:
            truncated[name] = notes
    return texts, truncated


def render(bundle):
    """Name -> Mermaid text for every diagram this phase emits."""
    return _render_all(bundle)[0]


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_diagrams(bundle, outdir="workspace/diagrams", train_id=None):
    """Write each diagram to <outdir>/<name>.mmd.

    Records a **workspace-relative** manifest on `bundle["diagrams"]` (e.g.
    `diagrams/<name>.mmd` — paths relative to the parent of `outdir`) so the
    persisted bundle is portable for downstream embedding, per the design spec.
    RETURNS the real on-disk paths (name -> path as written) so the caller can
    hand them straight to `validate_with_mmdc`.

    When `train_id` is given, renders ONLY that one train's flowchart (spotlight
    mode); the flat single-instance diagrams are skipped. Otherwise, all flat
    diagrams are written plus train flowcharts for every deep-tier train.
    `bundle["diagrams"]["train_flowcharts"]` is always a nested map
    {<train-id>: <workspace-relative-path>}.

    `bundle["diagrams"]["truncated"]` maps each diagram that hit a cap (flat name
    or train id) to `{"shown": n, "total": m, "unit": "PRs"|"edges"|...}`; it is
    `{}` when nothing was cut, so a report can say exactly what a diagram omits."""
    if train_id is not None:
        # Spotlight: render exactly one train (any tier). Resolve BEFORE touching
        # the filesystem so an unknown id leaves nothing behind.
        trains_by_id = {t["id"]: t for t in bundle.get("trains", [])}
        if train_id not in trains_by_id:
            raise KeyError(f"train_id {train_id!r} not found in bundle")
    os.makedirs(outdir, exist_ok=True)
    root = os.path.dirname(os.path.normpath(outdir)) or "."
    real_paths = {}

    if train_id is not None:
        notes = {}
        text = emit_train_flowchart(bundle, trains_by_id[train_id], notes=notes)
        path = os.path.join(outdir, f"{train_id}.mmd")
        _write(path, text)
        real_paths[train_id] = path
        # Merge into the bundle diagrams (preserve existing flat keys if any)
        diags = bundle.setdefault("diagrams", {})
        diags.setdefault("train_flowcharts", {})[train_id] = os.path.relpath(path, root)
        truncated = diags.setdefault("truncated", {})
        if notes:
            truncated[train_id] = notes
        else:
            truncated.pop(train_id, None)
        return real_paths

    # Default: write flat single-instance diagrams
    relative = {}
    texts, truncated = _render_all(bundle)
    for name, text in texts.items():
        path = os.path.join(outdir, f"{name}.mmd")
        _write(path, text)
        real_paths[name] = path
        relative[name] = os.path.relpath(path, root)

    # Write train flowcharts for every deep train
    train_rel_map = {}
    for train in bundle.get("trains", []):
        if train.get("tier") != "deep":
            continue
        tid = train["id"]
        notes = {}
        text = emit_train_flowchart(bundle, train, notes=notes)
        path = os.path.join(outdir, f"{tid}.mmd")
        _write(path, text)
        real_paths[tid] = path
        train_rel_map[tid] = os.path.relpath(path, root)
        if notes:
            truncated[tid] = notes

    relative["train_flowcharts"] = train_rel_map
    relative["truncated"] = truncated
    bundle["diagrams"] = relative
    return real_paths


def ensure_mmdc(which=shutil.which):
    """Return the mmdc path or fail fast with install guidance."""
    path = which("mmdc")
    if not path:
        sys.stderr.write("error: `mmdc` not found on PATH. " + INSTALL_HINT + "\n")
        raise SystemExit(3)
    return path


# Markers of an SVG that mmdc wrote with exit 0 although the diagram did NOT render:
# Mermaid draws its error diagram instead (role "error" for parse errors; the text
# limit draws a message box inside an otherwise normal-looking SVG). Checked after
# every compile.
_ERROR_SVG_ROLE = 'aria-roledescription="error"'       # structural: a parse error
# The text-limit failure has NO structural marker: Mermaid draws a normal-looking
# diagram whose only label is this message. It is therefore only treated as an
# error when the diagram SOURCE does not itself contain the phrase (a PR titled
# "Fix Maximum text size in diagram exceeded" is a valid label, not a failure).
_ERROR_SVG_TEXTS = (
    "Maximum text size in diagram exceeded",
    "Syntax error in text",
)

PUPPETEER_CONFIG_ENV = "ACTIVITY_MMDC_PUPPETEER_CONFIG"
_NO_SANDBOX = {"args": ["--no-sandbox", "--disable-setuid-sandbox"]}


def _svg_error(svg_path, source=""):
    """None when `svg_path` holds a real diagram, else a reason string.

    Mermaid's error role is decisive. Its error MESSAGES are only decisive when
    they cannot have come from the diagram's own text (`source`), so a valid
    diagram that merely mentions them in a label is not rejected."""
    try:
        with open(svg_path, encoding="utf-8", errors="replace") as fh:
            svg = fh.read()
    except OSError:
        return "mmdc reported success but wrote no output"
    for text in _ERROR_SVG_TEXTS:
        if text in svg and text not in (source or ""):
            return f"mmdc rendered an error diagram ({text})"
    if _ERROR_SVG_ROLE in svg:
        return f"mmdc rendered an error diagram ({_ERROR_SVG_ROLE})"
    return None


def _puppeteer_args(env, euid, tmpdir):
    """`-p <config>` for mmdc: the file named by $ACTIVITY_MMDC_PUPPETEER_CONFIG
    when set; otherwise, when running as root (CI containers), a generated
    no-sandbox config, because Chrome refuses to start sandboxed as root."""
    cfg = env.get(PUPPETEER_CONFIG_ENV)
    if cfg:
        return ["-p", cfg]
    if euid == 0:
        path = os.path.join(tmpdir, "puppeteer.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_NO_SANDBOX, fh)
        return ["-p", path]
    return []


def _source_too_long(path):
    try:
        with open(path, encoding="utf-8") as fh:
            size = _mermaid_len(fh.read())
    except (OSError, UnicodeDecodeError):
        return None     # let mmdc report the unreadable input
    if size > MERMAID_MAX_TEXT:
        return (f"{size} characters exceeds Mermaid's text limit "
                f"({MERMAID_MAX_TEXT}); mmdc would render an error diagram")
    return None


def validate_with_mmdc(paths, export=None, runner=subprocess.run, which=shutil.which,
                       env=None, euid=None):
    """Compile each .mmd with mmdc; raise RuntimeError on the first that fails to
    render. A failure is a non-zero exit, a missing output, OR an exit-0 *error
    SVG* (Mermaid's syntax-error / maximum-text-size diagrams) — mmdc exits 0 for
    those, so the SVG itself is inspected. Sources over MERMAID_MAX_TEXT are
    rejected before mmdc runs.

    Every diagram is first compiled to a throwaway temp SVG, so a validation-only
    run never overwrites or deletes a user's own exported images next to the
    `.mmd`. When `export` is 'svg' that checked SVG is copied beside the .mmd;
    when 'png' a second mmdc call writes the PNG (a PNG cannot be inspected for
    the error text, hence the SVG check first). Nothing is exported for a diagram
    that fails.

    `env`/`euid` default to the process environment / effective uid and select
    the puppeteer config (see `_puppeteer_args`)."""
    mmdc = ensure_mmdc(which)
    env = os.environ if env is None else env
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else None
    with tempfile.TemporaryDirectory() as tmp:
        extra = _puppeteer_args(env, euid, tmp)
        out = os.path.join(tmp, "validate.svg")
        for path in paths:
            too_long = _source_too_long(path)
            if too_long:
                raise RuntimeError(f"mmdc failed to render {path}: {too_long}")
            if os.path.exists(out):
                os.remove(out)
            result = runner([mmdc, "-i", path, "-o", out, "-q"] + extra,
                            capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"mmdc failed to render {path}:\n{result.stderr}")
            try:
                with open(path, encoding="utf-8", errors="replace") as _fh:
                    source = _fh.read()
            except OSError:
                source = ""
            reason = _svg_error(out, source)
            if reason:
                raise RuntimeError(f"mmdc failed to render {path}: {reason}")
            if export == "svg":
                shutil.copyfile(out, os.path.splitext(path)[0] + ".svg")
            elif export == "png":
                png = os.path.splitext(path)[0] + ".png"
                result = runner([mmdc, "-i", path, "-o", png, "-q"] + extra,
                                capture_output=True, text=True)
                if result.returncode != 0 or not os.path.exists(png):
                    raise RuntimeError(
                        f"mmdc failed to export {png}:\n{result.stderr}")
            os.remove(out)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Render + validate activity-overview diagrams.")
    p.add_argument("bundle", help="Path to the enriched bundle JSON.")
    p.add_argument("--diagrams-dir", default="workspace/diagrams")
    p.add_argument("--export", choices=["svg", "png"], default=None,
                   help="Also export images beside each .mmd (needs mmdc, so it "
                        "cannot be combined with --skip-validate).")
    p.add_argument("--skip-validate", action="store_true",
                   help="Skip the mmdc compile check (not recommended).")
    p.add_argument("--train", default=None, metavar="TRAIN_ID",
                   help="Spotlight: render only this one train's flowchart (any tier).")
    args = p.parse_args(argv)
    if args.export and args.skip_validate:
        # Exporting runs mmdc, which IS the validation; silently writing no images
        # (the old behaviour) misreported the run, so reject the combination.
        p.error("--export needs mmdc and always validates; drop --skip-validate")
    return args


def _unknown_train_message(train_id, bundle_path, known):
    listing = ", ".join(known[:10]) + (", ..." if len(known) > 10 else "")
    return (f"render.py --train: train {train_id!r} not found in {bundle_path} "
            f"({len(known)} trains{': ' + listing if known else ''})\n")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    with open(args.bundle) as fh:
        bundle = json.load(fh)
    if args.train is not None:
        known = sorted(str(t.get("id")) for t in bundle.get("trains", []))
        if args.train not in known:
            # Clean usage error like `link.py --slice`; nothing is written.
            sys.stderr.write(_unknown_train_message(args.train, args.bundle, known))
            raise SystemExit(2)
    manifest = write_diagrams(bundle, args.diagrams_dir, train_id=args.train)
    if not args.skip_validate:
        try:
            validate_with_mmdc(list(manifest.values()), export=args.export)
        except RuntimeError as exc:
            sys.stderr.write(f"error: {exc}\n")
            raise SystemExit(1)
    tmp = args.bundle + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    os.replace(tmp, args.bundle)
    exported = f", exported {args.export}" if args.export else ""
    sys.stderr.write(
        f"rendered {len(manifest)} diagrams into {args.diagrams_dir} "
        f"({'validated' if not args.skip_validate else 'unvalidated'}{exported})\n")
    truncated = (bundle.get("diagrams") or {}).get("truncated") or {}
    for name in sorted(n for n in truncated if n in manifest):
        t = truncated[name]
        sys.stderr.write(f"note: {name} capped: {t['shown']} of {t['total']} "
                         f"{t['unit']} shown\n")
    return manifest


if __name__ == "__main__":
    main()
