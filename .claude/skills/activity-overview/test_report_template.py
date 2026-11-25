"""Docs contract for `report-template.md` and `reference/report-sections.md`
(issue #83): every field the two documents tell the agent to read must exist in
the real pipeline output, not just in a document.

The corpus is built offline from an inline fixture exactly the way SKILL.md says
a report is made: gather.fold_bundle -> store -> `view.py` -> `link.py --series`
-> `render.py --skip-validate`, plus `link.py --slice` (train slice),
`spotlight.slice_module` (module biography slice) and `digest.py` (the merged
project view over a two-member store).

What is checked:

* every `bundle[...]` / `view[...]` subscript chain in either document;
* every `{placeholder}` in either document: each dotted path inside it must be
  rooted at a declared row variable (see the "Placeholder rules" block of the
  template and `ROW_VARS` below) or a top-level key, and must resolve; a
  placeholder that names no field must be one of `AGENT_DERIVED`, each of which
  the template's rules block documents. `{a, b, c}` shape literals (copies of
  the BUNDLE.md shapes or a narrator's output contract) are not placeholders;
* every back-ticked dotted path (`effort.elapsed_days`, `series.new`, ...):
  rooted paths must resolve from their root, other paths must resolve from some
  object in the output, and `module.function` names must exist in the module;
* one canonical section per diagram, the per-train narrative slot, and that
  stall/blocker attribution reads login lists, never the integer counts.

`PENDING` lists the few fields a document may name before the code emits them,
each with its fallback rule written in the documents.
"""

import contextlib
import copy
import importlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import render  # noqa: E402
import spotlight  # noqa: E402
import view  # noqa: E402

TEMPLATE = os.path.join(HERE, "report-template.md")
SECTIONS = os.path.join(HERE, "reference", "report-sections.md")

FROM, TO = "2026-05-01", "2026-05-31"
AREA = "avm/res/net/fw"

# Row variables: the name a placeholder uses for "the current row", and where
# that row comes from. A path rooted at a row variable resolves if it resolves
# from ANY of its sources. Keep in step with the template's rules block.
ROW_VARS = {
    "meta": ["bundle.meta", "view.meta", "view.members[].bundle.meta"],
    "ref": ["bundle.buckets.shipped[]", "bundle.buckets.in_flight[]",
            "bundle.buckets.rejected[]", "bundle.buckets.next_candidates[]",
            "view.shipped[]", "bundle.series.new[]",
            "bundle.series.carried_over[]", "bundle.forecast.candidates[].ref"],
    "loop": ["bundle.series.forecast_loop.landed[]",
             "bundle.series.forecast_loop.not_yet[]"],
    "item": ["bundle.prs[]", "bundle.issues[]"],
    "pr": ["bundle.prs[]"],
    "issue": ["bundle.issues[]"],
    "commit": ["bundle.commits[]"],
    "train": ["bundle.trains[]"],
    "ptrain": ["view.trains[]"],
    "member": ["view.members[]"],
    "related": ["view.related_work[]"],
    "release": ["bundle.releases[]"],
    "stats": ["bundle.workflow_stats[]"],
    "sprint": ["sprints_selected[]"],
    "flow_entry": ["bundle.flow[]"],
    "blocker": ["bundle.blockers[]"],
    "cand": ["bundle.forecast.candidates[]"],
    "delta": ["bundle.feature_deltas[]"],
    "move": ["bundle.symbol_moves.links[]"],
    "artifact": ["bundle.artifacts[]"],
    "ev": ["bundle.artifacts[].lifecycle[]"],
    "area": ["bundle.code_graph.areas[]"],
    "edge": ["view.module_edges[]"],
    "module": ["bundle.modules[]", "view.modules[]"],
    "rule": ["bundle.code_owners[]"],
    "fame": ["bundle.halls.fame[]"],
    "person": ["bundle.people[]", "view.people[]"],
    "mod": ["module"],
    "sym": ["module.symbols[]", "module.files[]"],
    "sev": ["module.symbols[].lifecycle[]", "module.files[].lifecycle[]"],
    "mtl": ["module.trains[].timeline[]"],
}

# Whole documents, addressable as roots.
DOC_ROOTS = ("bundle", "view", "slice", "module")

# Placeholders that name no field: the agent writes them, by a rule the
# template's "Placeholder rules" block documents.
AGENT_DERIVED = {
    "short title",
    "what was discussed",
    "what was decided",
    "verbatim quote",
    "requests raised",
    "action items / who owns what",
    "workflow",
    "owners",
    "top contributors",
    "one-line what-this-module-is",
    "change",
    "effort line",
}

# Fields a document may name before the code emits them; each document states
# the fallback next to the name.
PENDING = {
    "meta.ref_date": "views carry no ref_date yet (issue #75); fall back to meta.to",
}

_SEG = r"(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]\s]*\])"
PATH_RE = re.compile(r"(?<![\w.\"'/<-])([A-Za-z_][A-Za-z0-9_]*)((?:" + _SEG + r")+)")
FULL_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:" + _SEG + r")+$")
CHAIN_RE = re.compile(r"\b(bundle|view)((?:\[[^\[\]]*\])+)")
PLACEHOLDER_RE = re.compile(r"\{([^{}\n]+)\}")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
SHAPE_RE = re.compile(r"^[\w?\"]+(?::[^,]*)?(?:\s*,\s*[\w?\"]+(?::[^,]*)?)+$")
FILE_RE = re.compile(r"\.(?:md|py|json|mmd|db|txt|vtt)$")
NARRATIVE_SLOT = "<!-- narrative: <train-id> -->"


# ---------------------------------------------------------------- fixture ----

def _rich_bundle(owner, name, base, area=AREA, edge_to=None, ticket=None):
    """One member's raw bundle with every family of fact the report reads:
    shipped / in-flight / rejected / next-candidate items, reviews, a reopen,
    a blocker, board status + sprints, artifacts (example removed, docs added,
    symbols, a TODO comment, a symbol move), releases, CI stats, CODEOWNERS,
    milestones and (optionally) a module edge and a ticket reference."""
    url = "https://github.com/{}/{}".format(owner, name)
    c1, c2, c3 = "a" * 40, "b" * 40, "c" * 40
    pr_ship, pr_open, pr_rej = base + 1, base + 2, base + 3
    iss_ship, iss_bug, iss_old, iss_np, iss_next = (
        base + 10, base + 11, base + 12, base + 13, base + 14)
    docs = ["{}/docs/page{}.md".format(area, i) for i in range(9)]
    edges = []
    if edge_to:
        edges.append({"to": edge_to, "kind": "module",
                      "ref": "br/public:{}:1.0.0".format(edge_to),
                      "version": "1.0.0", "transitive": False,
                      "provider": "bicep", "resolved": True})

    def ev(commit, author, date, change, path):
        return {"commit": commit, "author": author, "date": date,
                "change": change, "path": path}

    def sym(commit, author, date, path, subkind, nm, change, before, after):
        return {"path": path, "lang": "bicep", "subkind": subkind, "name": nm,
                "change": change, "commit": commit, "author": author,
                "date": date, "before": before, "after": after}

    d1, d2, d3 = "2026-05-03T00:00:00Z", "2026-05-10T00:00:00Z", "2026-05-20T00:00:00Z"
    body = "Closes #{}".format(iss_ship) + (" ({})".format(ticket) if ticket else "")
    return {
        "meta": {"owner": owner, "repo": name, "from": FROM, "to": TO,
                 "base_branch": "main"},
        "commits": [
            {"sha": c1, "parents": [], "author": "alice", "date": d1,
             "message": "feat: add fw (#{})".format(pr_ship),
             "files": [area + "/main.bicep", area + "/examples/basic/main.bicep"]},
            {"sha": c2, "parents": [c1], "author": "bob", "date": d2,
             "message": "docs: pages (#{})".format(pr_ship),
             "files": [area + "/README.md"] + docs},
            {"sha": c3, "parents": [c2], "author": "alice", "date": d3,
             "message": "chore: drop example",
             "files": [area + "/examples/basic/main.bicep", area + "/sub.bicep"]},
        ],
        "code_events": (
            [ev(c1, "alice", d1, "add", area + "/main.bicep"),
             ev(c1, "alice", d1, "add", area + "/examples/basic/main.bicep"),
             ev(c2, "bob", d2, "add", area + "/README.md")]
            + [ev(c2, "bob", d2, "add", p) for p in docs]
            + [ev(c3, "alice", d3, "delete", area + "/examples/basic/main.bicep"),
               ev(c3, "alice", d3, "add", area + "/sub.bicep")]),
        "symbol_events": [
            sym(c1, "alice", d1, area + "/main.bicep", "param", "location",
                "add", None, "param location string"),
            sym(c1, "alice", d1, area + "/main.bicep", "todo", "// TODO: zones",
                "add", None, "// TODO: zones"),
            # a move: dropped from one file, added to another (no other add)
            sym(c3, "alice", d3, area + "/main.bicep", "resource", "uniqueThing",
                "drop", "resource uniqueThing ...", None),
            sym(c3, "alice", d3, area + "/sub.bicep", "resource", "uniqueThing",
                "add", None, "resource uniqueThing ..."),
        ],
        "prs": [
            {"number": pr_ship, "title": "feat: add firewall", "body": body,
             "author": "alice", "author_association": "MEMBER",
             "labels": ["Type: Feature"], "merged": True, "merged_by": "carol",
             "merged_at": "2026-05-12T00:00:00Z", "closed_at": "2026-05-12T00:00:00Z",
             "created_at": "2026-05-02T00:00:00Z", "updated_at": "2026-05-12T00:00:00Z",
             "state": "closed", "base": "main", "head": "feat-fw",
             "closes": [iss_ship], "crossref_issues": [],
             "url": "{}/pull/{}".format(url, pr_ship), "milestone": "v1.0",
             "reviewers": ["carol"], "review_decision": "approved",
             "review_comments_count": 1, "comments": 1,
             "comments_list": [{"author": "dave", "author_association": "NONE",
                                "body": "nice", "id": 1,
                                "url": "{}/pull/{}#c1".format(url, pr_ship),
                                "created_at": "2026-05-05T00:00:00Z"}],
             "review_comments": [{"author": "carol", "author_association": "MEMBER",
                                  "body": "nit", "id": 2,
                                  "url": "{}/pull/{}#r2".format(url, pr_ship),
                                  "created_at": "2026-05-06T00:00:00Z"}],
             "reviews": [
                 {"id": 100, "author": "carol", "state": "changes_requested",
                  "submitted_at": "2026-05-06T00:00:00Z", "body": "fix",
                  "url": "{}/pull/{}#rv100".format(url, pr_ship)},
                 {"id": 101, "author": "carol", "state": "approved",
                  "submitted_at": "2026-05-08T00:00:00Z", "body": None,
                  "url": "{}/pull/{}#rv101".format(url, pr_ship)}],
             "lifecycle": [{"id": 900, "actor": "alice", "event": "reopened",
                            "created_at": "2026-05-04T00:00:00Z", "label": None,
                            "url": None}],
             "board_status": "Done", "iteration": "IT1"},
            {"number": pr_open, "title": "fix: open work", "body": "",
             "author": "bob", "labels": [], "merged": False, "merged_at": None,
             "closed_at": None, "created_at": "2026-05-15T00:00:00Z",
             "updated_at": "2026-05-25T00:00:00Z", "state": "open",
             "base": "main", "head": "fix-x", "closes": [iss_bug],
             "crossref_issues": [], "url": "{}/pull/{}".format(url, pr_open),
             "milestone": None, "reviewers": [],
             "board_status": "In Progress", "iteration": "IT2"},
            {"number": pr_rej, "title": "experiment", "body": "",
             "author": "erin", "labels": [], "merged": False, "merged_at": None,
             "closed_at": "2026-05-18T00:00:00Z", "created_at": "2026-05-14T00:00:00Z",
             "updated_at": "2026-05-18T00:00:00Z", "state": "closed",
             "base": "main", "head": "exp", "closes": [], "crossref_issues": [],
             "url": "{}/pull/{}".format(url, pr_rej), "milestone": None,
             "reviewers": []},
        ],
        "issues": [
            {"number": iss_ship, "title": "Add firewall module", "body": "",
             "kind": "feature", "author": "frank", "author_association": "NONE",
             "labels": ["Type: Feature"], "assignees": ["alice"],
             "state": "closed", "state_reason": "completed",
             "closed_at": "2026-05-12T00:00:00Z", "created_at": "2026-03-01T00:00:00Z",
             "updated_at": "2026-05-12T00:00:00Z",
             "url": "{}/issues/{}".format(url, iss_ship), "milestone": "v1.0",
             "comments_list": [],
             "reactions": {"+1": 0, "-1": 0, "heart": 0, "hooray": 0, "total": 0}},
            {"number": iss_bug, "title": "Bug in rules", "body": "", "kind": "bug",
             "author": "gina", "labels": ["bug"], "assignees": ["bob"],
             "state": "open", "state_reason": None, "closed_at": None,
             "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-05-20T00:00:00Z",
             "url": "{}/issues/{}".format(url, iss_bug), "milestone": "v1.1",
             "board_status": "Todo", "iteration": "IT2",
             "facets": {"area": "area: net", "priority": None, "status": None,
                        "lifecycle": None},
             "blocks": [{"number": iss_old, "direction": "out"}],
             "reactions": {"+1": 6, "-1": 0, "heart": 0, "hooray": 0, "total": 6}},
            {"number": iss_old, "title": "Old request", "body": "",
             "kind": "feature", "author": "hank", "labels": [], "assignees": [],
             "state": "open", "state_reason": None, "closed_at": None,
             "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z",
             "url": "{}/issues/{}".format(url, iss_old), "milestone": None,
             "reactions": {"+1": 7, "-1": 0, "heart": 0, "hooray": 0, "total": 7}},
            {"number": iss_np, "title": "Nope", "body": "", "kind": "idea",
             "author": "hank", "labels": [], "assignees": [], "state": "closed",
             "state_reason": "not_planned", "closed_at": "2026-05-09T00:00:00Z",
             "created_at": "2026-04-01T00:00:00Z", "updated_at": "2026-05-09T00:00:00Z",
             "url": "{}/issues/{}".format(url, iss_np)},
            {"number": iss_next, "title": "Next thing", "body": "",
             "kind": "feature", "author": "gina", "labels": [], "assignees": [],
             "state": "open", "state_reason": None, "closed_at": None,
             "created_at": "2026-05-02T00:00:00Z", "updated_at": "2026-05-22T00:00:00Z",
             "url": "{}/issues/{}".format(url, iss_next), "milestone": "v1.2",
             "lifecycle": [{"id": 901, "actor": "gina", "event": "reopened",
                            "created_at": "2026-05-21T00:00:00Z", "label": None,
                            "url": None}]},
        ],
        "milestones": [
            {"title": "v1.0", "number": 1, "state": "closed",
             "due_on": "2026-05-15T00:00:00Z"},
            {"title": "v1.1", "number": 2, "state": "open",
             "due_on": "2026-06-15T00:00:00Z"},
            {"title": "v1.2", "number": 3, "state": "open",
             "due_on": "2026-07-15T00:00:00Z"}],
        "releases": [{"tag_name": "v1.0.0", "name": "1.0.0",
                      "published_at": "2026-05-15T00:00:00Z", "prerelease": False,
                      "url": "{}/releases/tag/v1.0.0".format(url)}],
        "workflow_stats": {"CI": {"total": 3, "success": 2, "failure": 1,
                                  "cancelled": 0, "other": 0}},
        "workflows": [],
        "code_graph": {"provider": "directory", "areas": [
            {"id": area, "label": area.rsplit("/", 1)[-1],
             "paths": [area + "/main.bicep", area + "/README.md"],
             "edges": edges, "edges_status": "resolved"}]},
        "code_owners": [{"pattern": "/avm/res/net/", "owners": ["alice"]}],
        "sprints": {
            "IT1": {"title": "Sprint 1", "start": "2026-05-01", "end": "2026-05-14"},
            "IT2": {"title": "Sprint 2", "start": "2026-05-15", "end": "2026-05-28"}},
    }


@contextlib.contextmanager
def _quiet():
    with contextlib.redirect_stderr(io.StringIO()):
        yield


def _fold(db, folds):
    conn = graphstore.open_store(db)
    graphstore.init_schema(conn)
    for bundle, kw in folds:
        gather.fold_bundle(conn, copy.deepcopy(bundle), **kw)
    conn.commit()
    conn.close()


def build_corpus(tmp):
    """Run the documented pipeline on the fixture; return the documents."""
    # Single repo: store -> view.py -> link.py --series -> render.py.
    db = os.path.join(tmp, "journey.db")
    _fold(db, [(_rich_bundle("o", "r", 40), {})])
    bpath = os.path.join(tmp, "view", "bundle.json")
    spath = os.path.join(tmp, "series.json")
    # A prior installment, so `series` is not a first installment and every
    # series list (new, carried_over, forecast_loop.landed/not_yet) is filled.
    with open(spath, "w", encoding="utf-8") as fh:
        json.dump([{"from": "2026-04-01", "to": "2026-04-30",
                    "ref_date": "2026-04-30", "shipped": [],
                    "in_flight": [{"type": "pr", "id": 41,
                                   "board_status": "In Progress"}],
                    "forecast": [{"type": "pr", "id": 41, "tier": "likely"},
                                 {"type": "issue", "id": 51, "tier": "possible"}]}],
                  fh)
    with _quiet():
        assert view.main(["--store", db, "--from", FROM, "--to", TO,
                          "--out", bpath]) == 0
        link.main([bpath, "--series", spath])
        render.main([bpath, "--diagrams-dir", os.path.join(tmp, "diagrams"),
                     "--skip-validate"])
    with open(bpath, encoding="utf-8") as fh:
        bundle = json.load(fh)
    deep = [t["id"] for t in bundle["trains"] if t.get("tier") == "deep"]
    train_slice = link.slice_train(bundle, deep[0])
    conn = graphstore.open_store(db)
    try:
        module = spotlight.slice_module(conn, "o", AREA)
    finally:
        conn.close()
    meta = bundle["meta"]
    sprints_selected = [s for s in link.select_sprints(
        bundle.get("sprints", {}), meta.get("ref_date") or meta["to"]) if s]

    # Multi-repo: two members -> digest.py (ticket clustering switched on).
    pdb = os.path.join(tmp, "project.db")
    members = {"Azure/mod-a", "Azure/mod-b"}
    _fold(pdb, [
        (_rich_bundle("Azure", "mod-a", 40, edge_to="avm/res/net/pip",
                      ticket="ABC-1"),
         {"project": "proj", "repo": "Azure/mod-a", "members": members}),
        (_rich_bundle("Azure", "mod-b", 60, area="avm/res/net/pip",
                      ticket="ABC-1"),
         {"project": "proj", "repo": "Azure/mod-b", "members": members}),
    ])
    out = io.StringIO()
    with _quiet(), contextlib.redirect_stdout(out):
        assert digest.main(["--store", pdb, "--project", "proj", "--from", FROM,
                            "--to", TO, "--ticket-pattern", r"ABC-\d+"]) == 0
    project_view = json.loads(out.getvalue())
    return {"bundle": bundle, "view": project_view, "slice": train_slice,
            "module": module, "sprints_selected": sprints_selected}


# --------------------------------------------------------------- resolver ----

def parse_path(text):
    """'a.b[]["c"][x].d' -> ['a', 'b', '[]', 'c', '[]', 'd'].
    A quoted subscript is a literal key; any other subscript is a wildcard
    (every element of a list / every value of a map)."""
    segs = []
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_-]*)|\[([^\]]*)\]", text):
        if m.group(1) is not None:
            segs.append(m.group(1))
        else:
            inner = m.group(2).strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                segs.append(inner[1:-1])
            else:
                segs.append("[]")
    return segs


def _resolve(obj, segs):
    """True when `segs` leads to an existing key from `obj` on some branch."""
    if not segs:
        return True
    head, rest = segs[0], segs[1:]
    if head == "[]":
        if isinstance(obj, list):
            vals = obj
        elif isinstance(obj, dict):
            vals = [v for k, v in obj.items() if not str(k).startswith("_")]
        else:
            return False
        return any(_resolve(v, rest) for v in vals)
    return isinstance(obj, dict) and head in obj and _resolve(obj[head], rest)


def _all_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _all_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_dicts(v)


class Corpus:
    def __init__(self, docs):
        self.docs = docs
        self.top_keys = set()
        for name in ("bundle", "view", "slice", "module"):
            self.top_keys |= set(docs[name])

    def _root(self, base):
        segs = parse_path(base)
        return segs[0], segs[1:]

    def resolve_rooted(self, text):
        """Resolve a path whose first segment is a row variable, a document
        name or a top-level key. Returns None when the root is unknown."""
        segs = parse_path(text)
        head, rest = segs[0], segs[1:]
        if head in ROW_VARS:
            for base in ROW_VARS[head]:
                doc, base_segs = self._root(base)
                if _resolve(self.docs[doc], base_segs + rest):
                    return True
            return False
        if head in DOC_ROOTS:
            return _resolve(self.docs[head], rest)
        if head in self.top_keys:
            return any(_resolve(self.docs[d], segs) for d in DOC_ROOTS)
        return None

    def resolve_anywhere(self, text):
        segs = parse_path(text)
        return any(_resolve(d, segs) for doc in DOC_ROOTS
                   for d in _all_dicts(self.docs[doc]))


def _module_function(text):
    """`link.select_sprints` style references to code: True when the module
    is one of the skill's and has that attribute; None when not code."""
    segs = text.split(".")
    if len(segs) != 2 or not os.path.isfile(os.path.join(HERE, segs[0] + ".py")):
        return None
    if segs[0] in ("series", "view") and not segs[1].islower():
        return None
    mod = importlib.import_module(segs[0])
    if hasattr(mod, segs[1]):
        return True
    return None


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _sections(text):
    """[(heading, body)] split on `## ` headings (the preamble is '')."""
    out, head, buf = [], "", []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append((head, "\n".join(buf)))
            head, buf = line[3:].strip(), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf)))
    return out


def _rules_block(template):
    m = re.search(r"placeholder-rules:begin(.*?)placeholder-rules:end", template, re.S)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ tests ----

class _CorpusCase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.corpus = Corpus(build_corpus(cls._tmp.name))
        cls.docs = {"report-template.md": _read(TEMPLATE),
                    "reference/report-sections.md": _read(SECTIONS)}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class TestCorpusIsRich(_CorpusCase):
    """The fixture must actually exercise the optional families, or a missing
    key would pass as 'omit-when-empty' rather than be checked."""

    def test_optional_families_are_populated(self):
        b = self.corpus.docs["bundle"]
        v = self.corpus.docs["view"]
        for key in ("flow", "blockers", "releases", "workflow_stats", "sprints",
                    "artifacts", "feature_deltas", "timeline", "people"):
            self.assertTrue(b.get(key), key)
        self.assertTrue(b["forecast"]["candidates"])
        self.assertTrue(b["symbol_moves"]["links"])
        self.assertTrue(b["diagrams"]["truncated"])
        self.assertFalse(b["series"]["first_installment"])
        for key in ("new", "carried_over"):
            self.assertTrue(b["series"][key], key)
        for key in ("landed", "not_yet"):
            self.assertTrue(b["series"]["forecast_loop"][key], key)
        for key in ("buckets", "halls"):
            for sub, val in b[key].items():
                self.assertTrue(val, "{}.{}".format(key, sub))
        self.assertTrue(any(p.get("reopen_count") for p in b["prs"]))
        self.assertTrue(any(p.get("review_rounds") for p in b["prs"]))
        self.assertTrue(v["related_work"])
        self.assertTrue(v["module_edges"])
        self.assertTrue(self.corpus.docs["sprints_selected"])
        self.assertTrue(self.corpus.docs["module"]["symbols"])


class TestSubscriptChains(_CorpusCase):
    def test_every_bundle_and_view_chain_resolves(self):
        bad = []
        for name, text in self.docs.items():
            for m in CHAIN_RE.finditer(text):
                path = m.group(1) + m.group(2)
                if not self.corpus.resolve_rooted(path):
                    bad.append("{}: {}".format(name, path))
        self.assertEqual(bad, [], "subscript chains that are not in the output")


class TestPlaceholders(_CorpusCase):
    def _placeholders(self, text):
        for m in PLACEHOLDER_RE.finditer(text):
            inner = m.group(1).strip()
            if SHAPE_RE.match(inner.replace("[ref]", "").replace("[]", "")):
                continue    # a `{a, b, c}` shape literal, not a placeholder
            yield inner

    def test_every_placeholder_path_resolves_or_is_agent_derived(self):
        bad = []
        for name, text in self.docs.items():
            for inner in self._placeholders(text):
                paths = [m.group(0) for m in PATH_RE.finditer(inner)]
                checked = 0
                for path in paths:
                    if path in PENDING:
                        checked += 1
                        continue
                    ok = self.corpus.resolve_rooted(path)
                    if ok is None:
                        if _module_function(path):
                            checked += 1
                            continue
                        bad.append("{}: {{{}}} -> unknown root in {!r}".format(
                            name, inner, path))
                    elif not ok:
                        bad.append("{}: {{{}}} -> {!r} not in output".format(
                            name, inner, path))
                    else:
                        checked += 1
                if not paths and inner not in AGENT_DERIVED:
                    bad.append("{}: {{{}}} names no field and is not a "
                               "documented agent-derived value".format(name, inner))
        self.assertEqual(bad, [])

    def test_agent_derived_values_are_documented_in_the_rules_block(self):
        rules = _rules_block(self.docs["report-template.md"])
        self.assertTrue(rules, "template has no placeholder-rules block")
        missing = [v for v in sorted(AGENT_DERIVED) if "{" + v + "}" not in rules]
        self.assertEqual(missing, [])

    def test_row_variables_are_documented_in_the_rules_block(self):
        rules = _rules_block(self.docs["report-template.md"])
        used = set()
        for text in self.docs.values():
            for inner in PLACEHOLDER_RE.findall(text):
                for m in PATH_RE.finditer(inner):
                    if m.group(1) in ROW_VARS:
                        used.add(m.group(1))
        missing = [v for v in sorted(used) if "`{}`".format(v) not in rules]
        self.assertEqual(missing, [], "row variables used but not defined")

    def test_pending_fields_state_their_fallback(self):
        for name, text in self.docs.items():
            for field in PENDING:
                for m in re.finditer(re.escape(field), text):
                    window = text[m.start():m.start() + 200]
                    self.assertIn("meta.to", window,
                                  "{}: {} named without its fallback".format(
                                      name, field))


class TestBacktickPaths(_CorpusCase):
    def test_every_backticked_field_path_resolves(self):
        bad = []
        for name, text in self.docs.items():
            for span in BACKTICK_RE.findall(text):
                span = span.strip()
                if not FULL_PATH_RE.match(span) or span in PENDING \
                        or FILE_RE.search(span):
                    continue
                if _module_function(span):
                    continue
                ok = self.corpus.resolve_rooted(span)
                if ok is None:
                    ok = self.corpus.resolve_anywhere(span)
                if not ok:
                    bad.append("{}: `{}`".format(name, span))
        self.assertEqual(bad, [], "back-ticked paths not in the output")


class TestDiagramPlacement(_CorpusCase):
    """Issue #83 problem 6: one canonical section per diagram, the same in
    both documents."""

    FLAT = ("buckets_pie", "timeline_gantt", "content_timeline", "deltas_bar",
            "contributor_graph", "kind_breakdown", "module_graph", "blocker_graph")

    def _embeds(self):
        """diagram -> set of template `##` headings that embed it in a mermaid
        fence."""
        out = {}
        for head, body in _sections(self.docs["report-template.md"]):
            for fence in re.findall(r"```mermaid\n(.*?)```", body, re.S):
                for name in re.findall(r"diagrams\.([a-z_]+)", fence):
                    out.setdefault(name, set()).add(head)
        return out

    def test_every_flat_diagram_is_a_real_key(self):
        self.assertEqual(
            sorted(set(self.FLAT) - set(self.corpus.docs["bundle"]["diagrams"])), [])

    def test_each_flat_diagram_is_embedded_in_exactly_one_template_section(self):
        embeds = self._embeds()
        for name in self.FLAT:
            self.assertEqual(len(embeds.get(name, ())), 1,
                             "{} embedded in {}".format(name, embeds.get(name)))

    def test_sections_guide_names_each_diagram_under_the_same_section(self):
        embeds = self._embeds()
        for head, body in _sections(self.docs["reference/report-sections.md"]):
            for name in set(re.findall(r"diagrams\.([a-z_]+)", body)):
                if name not in self.FLAT:
                    continue
                (canonical,) = embeds[name]
                self.assertIn(canonical.lower(), head.lower(),
                              "report-sections.md names diagrams.{} under '{}', "
                              "the template embeds it under '{}'".format(
                                  name, head, canonical))


class TestNarrativeSlotAndStaleNotes(_CorpusCase):
    def test_template_has_the_per_train_narrative_slot(self):
        self.assertIn(NARRATIVE_SLOT, self.docs["report-template.md"])
        self.assertIn(NARRATIVE_SLOT, self.docs["reference/report-sections.md"])

    def test_no_stale_later_slice_note(self):
        text = self.docs["report-template.md"]
        self.assertNotIn("later slice", text)
        self.assertNotIn("lands with graphify", text)


class TestAttributionReadsLogins(_CorpusCase):
    """Issue #83 problem 3: effort.participants / effort.reviewers are counts,
    so the internal appendix must attribute from login fields."""

    LOGIN_FIELDS = ("pr.author", "pr.reviewers", "issue.author", "issue.assignees")

    def _appendix(self, name):
        for head, body in _sections(self.docs[name]):
            if head.lower().startswith("internal appendix"):
                return body
        self.fail("{} has no internal appendix section".format(name))

    def test_effort_counts_are_integers(self):
        for t in self.corpus.docs["bundle"]["trains"]:
            self.assertIsInstance(t["effort"]["participants"], int)
            self.assertIsInstance(t["effort"]["reviewers"], int)

    def test_appendix_never_attributes_from_counts(self):
        for name in self.docs:
            body = self._appendix(name)
            self.assertNotRegex(body, r"effort\.(participants|reviewers)")

    def test_appendix_names_real_login_fields(self):
        b = self.corpus.docs["bundle"]
        for name in self.docs:
            body = self._appendix(name)
            for field in self.LOGIN_FIELDS:
                self.assertIn("`{}`".format(field), body, name)
        for field in self.LOGIN_FIELDS:
            var, key = field.split(".")
            rows = b["prs"] if var == "pr" else b["issues"]
            vals = [r[key] for r in rows if key in r]
            self.assertTrue(vals, field)
            for val in vals:
                if isinstance(val, list):
                    self.assertTrue(all(isinstance(x, str) for x in val), field)
                else:
                    self.assertIsInstance(val, (str, type(None)), field)


if __name__ == "__main__":
    unittest.main()
