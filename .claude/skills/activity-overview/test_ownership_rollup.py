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
A, B, C, D, E, F, G = ("avm/res/a/a", "avm/res/b/b", "avm/res/c/c", "avm/res/d/d",
                       "avm/res/e/e", "avm/res/f/f", "avm/res/g/g")
CHANGES = [
    (1, "2026-10-03T08:00:00Z", B + "/ORPHANED.md", "add", None, None),
    (2, "2026-10-06T10:00:00Z", owner_file(A), "modify", ["o1"], []),
    (3, "2026-10-12T10:00:00Z", owner_file(F), "modify", "UNREAD", "UNREAD"),
    (4, "2026-10-20T10:00:00Z", owner_file(A), "modify", [], ["o2"]),
    (5, "2026-10-27T09:00:00Z", owner_file(C), "modify", ["o3"], []),
    (6, "2026-10-27T15:00:00Z", owner_file(C), "modify", [], ["o4"]),
    (7, "2026-10-28T11:00:00Z", B + "/ORPHANED.md", "delete", None, None),
    (8, "2026-10-29T12:00:00Z", owner_file(D), "modify", ["o5"], []),
    # two commits in the same second: emptied, then refilled (walk order);
    # sha 0x0a sorts after 0x09, kind order would put owner_added first
    (9, "2026-10-09T10:00:00Z", owner_file(G), "modify", ["o6"], []),
    (10, "2026-10-09T10:00:00Z", owner_file(G), "modify", [], ["o7"]),
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


def legacy(f):
    """A fact as a gather before `at` / `seq` stored it."""
    f = json.loads(json.dumps(f))
    for e in f["events"]:
        e.pop("at", None)
        e.pop("seq", None)
    return f


STORAGES = {
    "month": [("2026-10-01", "2026-10-31")],
    "month+carried": [("2026-10-01", "2026-10-31"), ("2026-10-26", "2026-11-30")],
    "two months": [("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30")],
    "weekly": [("2026-10-01", "2026-10-07"), ("2026-10-08", "2026-10-14"),
               ("2026-10-15", "2026-10-21"), ("2026-10-22", "2026-10-28"),
               ("2026-10-29", "2026-11-04")],
    # a fact gathered before `at` / `seq`, and a refreshed overlapping window
    "legacy+refreshed": [("2026-10-01", "2026-10-31", legacy),
                         ("2026-10-26", "2026-11-30")],
}


def store(name):
    rows = []
    for w in STORAGES[name]:
        f = fact(w[0], w[1])
        rows.append((w[0], w[1], w[2](f) if len(w) > 2 else f))
    return _store(*rows)


def strip_time(events):
    return [{k: v for k, v in e.items() if k not in ("at", "seq")} for e in events]


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
        conn = store("month+carried")
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        ids = [(e["kind"], e.get("module"), json.dumps(e["source"], sort_keys=True))
               for e in v["module_ownership"]["events"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(fact("2026-10-01", "2026-10-31")["events"]))

    def test_2_unread_survives_a_collected_empty_later_window(self):
        conn = store("two months")
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        self.assertEqual(v["module_ownership"]["unread"],
                         [{"path": owner_file(F), "commit": sha(3),
                           "reason": "not valid JSON"}])

    def test_3_opening_state_from_an_overlapping_window(self):
        conn = store("month+carried")
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


class TestFindingsRound3(unittest.TestCase):

    def test_1_a_legacy_copy_and_a_refreshed_copy_are_one_event(self):
        conn = store("legacy+refreshed")
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        evs = v["module_ownership"]["events"]
        self.assertEqual(len(evs), len(fact("2026-10-01", "2026-10-31")["events"]))
        late = [e for e in evs if e["date"] >= "2026-10-27"]
        self.assertTrue(all(e.get("at") for e in late))    # the timed copy wins

    def test_2_same_second_keeps_the_recorded_order(self):
        f = fact("2026-10-01", "2026-10-31")
        merged = extract.merge_ownership_events([f, fact("2026-10-08", "2026-10-14")])
        g = [e["kind"] for e in merged if e["module"] == G]
        self.assertEqual(g, ["owner_needed", "owner_removed", "owner_added"])
        carried = extract.carried_ownership([(("2026-10-01", "2026-10-31"), f)],
                                            "2026-10-10")
        self.assertNotIn(G, carried["emptied"])

    def test_3_an_end_day_no_window_covers_is_unavailable(self):
        conn = store("month")
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        mo = v["module_ownership"]
        self.assertEqual(mo["without_owner"], [])
        self.assertIn("2026-11-30", mo["without_owner_unavailable"])
        self.assertEqual(len(mo["events"]), len(fact("2026-10-01", "2026-10-31")["events"]))

    def test_4_field_types_readers_rely_on(self):
        f = fact("2026-10-01", "2026-10-31")
        for path, bad in ((("events", 0, "date"), 20261003),
                          (("events", 0, "module"), ["x"]),
                          (("events", 0, "people"), ["a", 3]),
                          (("events", 0, "seq"), True),
                          (("events", 0, "source"), {"sha": 5}),
                          (("without_owner", 0, "module"), None),
                          (("labelled_items", 0, "opened"), 5),
                          (("label_patterns",), {"orphaned": "orphan"})):
            g = json.loads(json.dumps(f))
            cur = g
            for k in path[:-1]:
                cur = cur[k]
            cur[path[-1]] = bad
            self.assertIsNotNone(validate._fact_shape_problem("module_ownership", g),
                                 (path, bad))


def _readers(g):
    """Every ownership reader over a fact: the reconstruction for every day,
    the merge, the carry, a merged view and link's annotation."""
    import link
    for d in days():
        extract.standing_without_owner(g, d)
    extract.merge_ownership_events([g, g])
    extract.carried_ownership([(("2026-10-01", "2026-10-31"), g)], "2026-10-20")
    conn = _store(("2026-10-01", "2026-10-31", g), ("2026-11-01", "2026-11-30", g))
    v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-15")
    v.setdefault("prs", [])
    v.setdefault("issues", [])
    link.annotate_module_ownership(v)


class TestFuzzReaders(unittest.TestCase):
    """Facts validate accepts never crash a reader: every field of every
    row replaced by values of other types; each mutant validate lets
    through is fed to every reader."""

    VALUES = (None, 0, 1.5, True, "", "x", "2026-10-05", [], ["a"], [1], {}, {"a": 1})

    def test_accepted_mutants_never_raise(self):
        base = fact("2026-10-01", "2026-10-31")
        base["unread"] = base.get("unread") or []
        accepted = 0
        for key in ("events", "without_owner", "start_without_owner",
                    "labelled_items", "unread"):
            rows = base.get(key) or []
            if not rows:
                continue
            fields = sorted({f for r in rows for f in r} | {"module", "date", "people"})
            for field in fields:
                for val in self.VALUES:
                    g = json.loads(json.dumps(base))
                    g[key][0][field] = val
                    if validate._fact_shape_problem("module_ownership", g):
                        continue
                    accepted += 1
                    try:
                        _readers(g)
                    except Exception as exc:        # noqa: BLE001
                        self.fail("{}[0].{} = {!r}: {!r}".format(key, field, val, exc))
        self.assertGreater(accepted, 0)


class TestEveryStorageReadsTheSame(unittest.TestCase):

    def test_identical_for_every_day_and_equal_to_a_replay(self):
        views = {}
        for name in STORAGES:
            conn = store(name)
            views[name] = {d: extract.extract(conn, "o", "r", "2026-10-01", d)
                           ["module_ownership"] for d in days()}
        for d in days():
            want = replay(d)
            first = None
            for name in STORAGES:
                mo = views[name][d]
                ids = [extract.ownership_event_identity(e) for e in mo["events"]]
                self.assertEqual(len(ids), len(set(ids)), (name, d, "duplicates"))
                got = [w["module"] for w in mo["without_owner"]]
                without = mo["without_owner"]
                events = strip_time(mo["events"])     # a legacy fact has no times
                if name == "legacy+refreshed" and d < "2026-10-26":
                    # only the legacy fact covers the day: it never recorded
                    # the order of one day's changes (G: emptied and refilled
                    # in one second), so events compare as a set and G is
                    # aside on every day (its state carries on);
                    # --recompute-ownership rewrites such a fact
                    got = [m for m in got if m != G]
                    without = [w for w in without if w["module"] != G]
                    events = sorted(events, key=lambda e: json.dumps(e, sort_keys=True))
                    self.assertEqual(got, [m for m in want if m != G], (name, d))
                    ref = json.loads(first[1][1])
                    self.assertEqual(json.dumps(events, sort_keys=True), json.dumps(
                        sorted(ref, key=lambda e: json.dumps(e, sort_keys=True)),
                        sort_keys=True), (name, d, "events"))
                    self.assertEqual(json.dumps(without, sort_keys=True), json.dumps(
                        [w for w in json.loads(first[1][0]) if w["module"] != G],
                        sort_keys=True), (name, d, "without"))
                    continue
                if name == "legacy+refreshed":
                    # G's state carries from the legacy fact into later days
                    got = [m for m in got if m != G]
                    want_here = [m for m in want if m != G]
                    self.assertEqual(got, want_here, (name, d))
                    self.assertEqual(
                        json.dumps([w for w in without if w["module"] != G], sort_keys=True),
                        json.dumps([w for w in json.loads(first[1][0])
                                    if w["module"] != G], sort_keys=True), (name, d))
                    ref = json.loads(first[1][1])

                    def split(evs):
                        return ([e for e in evs if e["module"] != G],
                                sorted(json.dumps(e, sort_keys=True) for e in evs
                                       if e["module"] == G))
                    self.assertEqual(split(events), split(ref), (name, d, "events"))
                    continue
                self.assertEqual(got, want, (name, d))
                shape = (json.dumps(without, sort_keys=True),
                         json.dumps(events, sort_keys=True))
                if first is None:
                    first = (name, shape)
                else:
                    self.assertEqual(shape[0], first[1][0], (name, first[0], d, "without"))
                    self.assertEqual(shape[1], first[1][1], (name, first[0], d, "events"))

if __name__ == "__main__":
    unittest.main()
