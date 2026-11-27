"""Ownership roll-up across stored windows (issue #97, review findings on
the ownership commits): one change is one event whichever windows stored
it, unread evidence survives a roll-up, the opening state comes from every
contributing window, the start snapshot is validated — and a property
check: the same history stored as one month, a month plus overlapping
carried days, two adjacent months or weekly windows reads back IDENTICALLY
for every day, and equal to a plain replay of the history."""
import json
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import validate  # noqa: E402
from test_module_ownership import Blobs, meta  # noqa: E402
from test_review_fixes_ownership import _store  # noqa: E402


def sha(n):
    return "%040x" % n


def owner_file(module):
    return module + "/metadata.json"


# The history: (sha, commit time, path, change, owners before, owners after)
A, B, C, D, E, F = ("avm/res/a/a", "avm/res/b/b", "avm/res/c/c", "avm/res/d/d",
                    "avm/res/e/e", "avm/res/f/f")
CHANGES = [
    (1, "2026-10-03T08:00:00Z", B + "/ORPHANED.md", "add", None, None),
    (2, "2026-10-06T10:00:00Z", owner_file(A), "modify", ["o1"], []),
    (3, "2026-10-12T10:00:00Z", owner_file(F), "modify", "UNREAD", "UNREAD"),
    (4, "2026-10-20T10:00:00Z", owner_file(A), "modify", [], ["o2"]),
    (5, "2026-10-27T09:00:00Z", owner_file(C), "modify", ["o3"], []),
    (6, "2026-10-27T15:00:00Z", owner_file(C), "modify", [], ["o4"]),
    (7, "2026-10-28T11:00:00Z", B + "/ORPHANED.md", "delete", None, None),
    (8, "2026-10-29T12:00:00Z", owner_file(D), "modify", ["o5"], []),
]
LABEL = "Status: Module Orphaned"
ISSUES = [
    {"number": 10, "title": E + " needs an owner", "labels": [LABEL], "state": "closed",
     "author": "r", "created_at": "2026-10-10T09:00:00Z",
     "closed_at": "2026-10-24T09:00:00Z", "url": "u10", "comments_list": []},
    {"number": 11, "title": A + " has no owner", "labels": [LABEL], "state": "open",
     "author": "r", "created_at": "2026-10-08T09:00:00Z", "url": "u11",
     "comments_list": []},
]


def blobs():
    out = {}
    for n, _at, path, _ch, before, after in CHANGES:
        if before is None:
            continue
        if before == "UNREAD":
            out[(sha(n) + "^", path)] = "{ not json"
            out[(sha(n), path)] = "{ not json"
            continue
        out[(sha(n) + "^", path)] = meta(before)
        out[(sha(n), path)] = meta(after)
    return out


def markers_at(day):
    """The orphan marker files tracked at the start of `day`."""
    files = set()
    for _n, at, path, change, _b, _a in CHANGES:
        if path.endswith("ORPHANED.md") and at[:10] < day:
            (files.add if change == "add" else files.discard)(path)
    return sorted(files)


def fact(frm, to):
    evs = [{"commit": sha(n), "path": path, "change": ch, "date": at[:10], "author": "x"}
           for n, at, path, ch, _b, _a in CHANGES if frm <= at[:10] <= to]
    commits = [{"sha": sha(n), "committed_at": at} for n, at, *_r in CHANGES
               if frm <= at[:10] <= to]
    end_files = markers_at((date.fromisoformat(to) + timedelta(days=1)).isoformat())
    return gather.collect_module_ownership(
        evs, commits, [], list(ISSUES), Blobs(blobs()), end_files, frm, to,
        web="https://github.com/o/r", start_files=markers_at(frm))


STORAGES = {
    "month": [("2026-10-01", "2026-10-31")],
    "month+carried": [("2026-10-01", "2026-10-31"), ("2026-10-26", "2026-11-30")],
    "two months": [("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30")],
    "weekly": [("2026-10-01", "2026-10-07"), ("2026-10-08", "2026-10-14"),
               ("2026-10-15", "2026-10-21"), ("2026-10-22", "2026-10-28"),
               ("2026-10-29", "2026-11-04")],
}


def replay(day):
    """A plain replay of the history: the modules without an owner at the
    end of `day`."""
    markers, emptied, owned_since = set(), set(), {}
    for _n, at, path, change, before, after in sorted(CHANGES, key=lambda c: c[1]):
        if at[:10] > day:
            continue
        module = path.rsplit("/", 1)[0]
        if path.endswith("ORPHANED.md"):
            (markers.add if change == "add" else markers.discard)(module)
            if change == "delete":
                owned_since[module] = at[:10]
        elif before != "UNREAD":
            if before and not after:
                emptied.add(module)
                owned_since.pop(module, None)
            elif after:
                emptied.discard(module)
                owned_since[module] = at[:10]
    out = markers | emptied
    for i in ISSUES:
        opened, closed = i["created_at"][:10], (i.get("closed_at") or "")[:10]
        module = i["title"].split(" ")[0]
        if opened > day or (closed and closed <= day):
            continue
        if owned_since.get(module, "") >= opened:
            continue
        out.add(module)
    return sorted(out)


def days():
    d = date(2026, 10, 1)
    while d <= date(2026, 10, 31):
        yield d.isoformat()
        d += timedelta(days=1)


class TestFindings(unittest.TestCase):

    def test_1_one_change_is_one_event_across_overlapping_windows(self):
        conn = _store(*[(f, t, fact(f, t)) for f, t in STORAGES["month+carried"]])
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        ids = [(e["kind"], e.get("module"), json.dumps(e["source"], sort_keys=True))
               for e in v["module_ownership"]["events"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(fact("2026-10-01", "2026-10-31")["events"]))

    def test_2_unread_survives_a_collected_empty_later_window(self):
        conn = _store(*[(f, t, fact(f, t)) for f, t in STORAGES["two months"]])
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        self.assertEqual(v["module_ownership"]["unread"],
                         [{"path": owner_file(F), "commit": sha(3),
                           "reason": "not valid JSON"}])

    def test_3_opening_state_from_an_overlapping_window(self):
        conn = _store(*[(f, t, fact(f, t)) for f, t in STORAGES["month+carried"]])
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        # D was emptied on 29 October, inside both windows; A's list was
        # emptied on 6 October and refilled: only October's window saw it
        self.assertEqual([w["module"] for w in v["module_ownership"]["without_owner"]],
                         [D])
        self.assertEqual(sorted(extract.carried_ownership(
            [(("2026-10-01", "2026-10-31"), fact("2026-10-01", "2026-10-31"))],
            "2026-10-26")["emptied"]), [])
        self.assertEqual(sorted(extract.carried_ownership(
            [(("2026-10-01", "2026-10-31"), fact("2026-10-01", "2026-10-31"))],
            "2026-10-10")["emptied"]), [A])

    def test_4_start_snapshot_rows_are_validated(self):
        f = fact("2026-10-01", "2026-10-31")
        for key, bad in (("start_without_owner", [{}]),
                         ("start_without_owner", [{"module": 3}]),
                         ("labelled_items", [{}]),
                         ("labelled_items", [{"module": "m", "opened": 5}])):
            self.assertIsNotNone(validate._fact_shape_problem(
                "module_ownership", dict(f, **{key: bad})), (key, bad))
        self.assertIsNone(validate._fact_shape_problem("module_ownership", f))


class TestEveryStorageReadsTheSame(unittest.TestCase):

    def test_identical_for_every_day_and_equal_to_a_replay(self):
        views = {}
        for name, windows in STORAGES.items():
            conn = _store(*[(f, t, fact(f, t)) for f, t in windows])
            views[name] = {d: extract.extract(conn, "o", "r", "2026-10-01", d)
                           ["module_ownership"] for d in days()}
        for d in days():
            want = replay(d)
            first = None
            for name in STORAGES:
                mo = views[name][d]
                got = [w["module"] for w in mo["without_owner"]]
                self.assertEqual(got, want, (name, d))
                shape = (json.dumps(mo["without_owner"], sort_keys=True),
                         json.dumps(mo["events"], sort_keys=True))
                if first is None:
                    first = (name, shape)
                else:
                    self.assertEqual(shape[0], first[1][0], (name, first[0], d, "without"))
                    self.assertEqual(shape[1], first[1][1], (name, first[0], d, "events"))


if __name__ == "__main__":
    unittest.main()
