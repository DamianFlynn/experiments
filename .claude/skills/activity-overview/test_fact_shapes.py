"""The reader-field type table for every fact this round added or extended
(issue #97, review round 5): `linked_items`, the `pr_activity` additions
(description edit time, event actor types), the `pr_history` additions
(net change per file, added paths, replaced commits). A fact validate
accepts never crashes a reader: every field, at every depth, replaced by
values of other types; each mutant validate lets through is fed to the
readers."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
from test_pr_activity import _fact, _folded  # noqa: E402
from test_story import pr  # noqa: E402
from test_story_groups import talk, view  # noqa: E402
from test_story_substance import commit  # noqa: E402

VALUES = (None, 0, 1.5, True, "", "x", "2026-05-05T00:00:00Z", [], ["a"], [1], [{}],
          {}, {"a": 1})


def paths(obj, prefix=()):
    """Every field path of `obj` (into the first element of each list)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield prefix + (k,)
            yield from paths(v, prefix + (k,))
    elif isinstance(obj, list) and obj:
        yield prefix + (0,)
        yield from paths(obj[0], prefix + (0,))


def mutants(base):
    for path in paths(base):
        for val in VALUES:
            g = json.loads(json.dumps(base))
            cur = g
            for k in path[:-1]:
                cur = cur[k]
            cur[path[-1]] = val
            yield path, val, g


def store(key, fact):
    conn = _folded(None)
    for frm, to in (("2026-05-01", "2026-05-31"), ("2026-06-01", "2026-06-30")):
        gather.fold_bundle(conn, {
            "meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                     "period": {"from": frm, "to": to}, "ref_date": to,
                     "base_branch": "main", "branches": ["main"], "clone_sha": None},
            "prs": [], "issues": [], "commits": [], key: fact})
    return extract.extract(conn, "o", "r", "2026-05-01", "2026-06-15")


LINKED = {"collected": True, "gathered_at": "2026-06-02T00:00:00Z", "requests": 1,
          "items": [{"number": 9, "type": "issue", "title": "Bug", "state": "closed",
                     "created_at": "2026-04-01T00:00:00Z",
                     "closed_at": "2026-06-03T00:00:00Z", "state_reason": "completed",
                     "url": "u9", "author": "r"},
                    {"number": 12, "type": "pr", "title": "Fix", "state": "merged",
                     "created_at": "2026-04-02T00:00:00Z", "merged": True,
                     "merged_at": "2026-05-03T00:00:00Z", "url": "u12"}],
          "missing": [14]}


def history():
    c = commit("a" * 40, "2026-05-10T00:00:00Z", "one", ["avm/res/x/y/main.bicep"])
    c["added"] = ["avm/res/x/y/main.bicep"]
    return {"collected": True, "base": "main", "prs": [{
        "number": 1, "status": "fetched", "commits": [c],
        "net_files": {"head": "a" * 40, "base": "m" * 40,
                      "files": [{"path": "avm/res/x/y/main.bicep", "added": 3,
                                 "removed": 1}]},
        "replaced": [{"at": "2026-05-09T00:00:00Z", "status": "read", "actor": "alice",
                      "commits": [{"sha": "b" * 40, "subject": "first", "files": 1}]}]}]}


def activity():
    f = _fact(body_edited_at="2026-06-20T00:00:00Z")
    f["prs"][0]["events"].append({"id": 10, "event": "review_requested",
                                  "at": "2026-05-04T00:00:00Z", "actor": "agent",
                                  "actor_type": "Bot", "requested": "bob"})
    return f


def run_story(b):
    b["meta"]["records_as_of"] = {"instant": "2026-05-31T23:59:59Z"}
    b = link.enrich(b)
    for t in b.get("trains") or []:
        link.slice_train(b, t["id"])


class TestLinkedItemsShape(unittest.TestCase):

    def test_missing_is_a_list_of_numbers(self):
        for bad in (42, [{}], ["14"], [True]):
            self.assertIsNotNone(validate._fact_shape_problem(
                "linked_items", dict(LINKED, missing=bad)), bad)
        self.assertIsNone(validate._fact_shape_problem("linked_items", LINKED))

    def test_accepted_mutants_never_raise(self):
        accepted = 0
        for path, val, g in mutants(LINKED):
            if validate._fact_shape_problem("linked_items", g):
                continue
            accepted += 1
            try:
                v = store("linked_items", g)
                for it in (v.get("linked_items") or {}).get("items") or []:
                    link.linked_item_as_of(it, "2026-05-31T23:59:59Z")
                run_story(view([pr(1, body="See #9, #12 and #14", comments_list=talk(1))],
                               linked_items=g))
            except Exception as exc:          # noqa: BLE001
                self.fail("{} = {!r}: {!r}".format(path, val, exc))
        self.assertGreater(accepted, 10)


class TestPrHistoryAdditions(unittest.TestCase):

    def test_accepted_mutants_never_raise(self):
        accepted = 0
        base = history()
        for path, val, g in mutants(base):
            if path[:3] != ("prs", 0, "net_files") and path[:3] != ("prs", 0, "replaced") \
                    and path[-1] not in ("added", "paths", "parents", "author",
                                         "committed_at"):
                continue
            if validate._fact_shape_problem("pr_history", g):
                continue
            accepted += 1
            try:
                run_story(view([pr(1, comments_list=talk(1))], pr_history=g))
            except Exception as exc:          # noqa: BLE001
                self.fail("{} = {!r}: {!r}".format(path, val, exc))
        self.assertGreater(accepted, 10)


class TestPrActivityAdditions(unittest.TestCase):

    def test_accepted_mutants_never_raise(self):
        accepted = 0
        base = activity()
        for path, val, g in mutants(base):
            if path[-1] not in ("body_edited_at", "actor_type", "actor", "at"):
                continue
            if validate._fact_shape_problem("pr_activity", g):
                continue
            accepted += 1
            try:
                run_story(view([pr(1, comments_list=talk(1))], pr_activity=g,
                               pr_history={"collected": True, "base": "main", "prs": []}))
            except Exception as exc:          # noqa: BLE001
                self.fail("{} = {!r}: {!r}".format(path, val, exc))
        self.assertGreater(accepted, 5)


class TestLabelEventsReader(unittest.TestCase):
    """`label_events` live on PR records (not a fact): the reader itself
    takes any shape without raising."""

    def test_any_shape(self):
        base = {"label_events": [{"event": "labeled", "label": "x",
                                  "at": "2026-06-02T00:00:00Z"}]}
        for path, val, g in mutants(base):
            b = view([pr(1, labels=["x"], comments_list=talk(1), **g)])
            try:
                run_story(b)
            except Exception as exc:          # noqa: BLE001
                self.fail("{} = {!r}: {!r}".format(path, val, exc))


if __name__ == "__main__":
    unittest.main()
