"""Module ownership (issue #97: "maintainer needed / added / removed"): the
per-window fact `module_ownership` from CODEOWNERS and per-module owner
files, orphan markers and ownership labels."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

S1, S2, S3 = "1" * 40, "2" * 40, "3" * 40


class Blobs:
    def __init__(self, files):
        self.files = files            # {(rev, path): text | Exception}

    def __call__(self, rev, path):
        got = self.files.get((rev, path))
        if isinstance(got, Exception):
            raise got
        return got


def meta(owners):
    return json.dumps({"moduleDisplayName": "X", "owners": owners})


def implied_start_state(events, blobs, end_files=(), start_files=None):
    """The COMPLETE start state a test's history implies (what gather reads
    from the tree before the window): the orphan markers tracked then
    (`start_files`, else the end files' markers minus those the window
    added, plus those it removed) and every owner file the window changes,
    with its owners before its first change. Nothing else existed."""
    def folder(p):
        return p.rsplit("/", 1)[0] if "/" in p else ""
    if start_files is not None:
        markers = {folder(f) for f in start_files if f.endswith("/ORPHANED.md")}
    else:
        markers = {folder(f) for f in end_files if f.endswith("/ORPHANED.md")}
        for e in events:
            if e.get("path", "").endswith("/ORPHANED.md"):
                (markers.discard if e["change"] == "add" else markers.add)(
                    folder(e["path"]))
    state = {m: {"owners": [], "codeowners": [], "orphan_marker": True} for m in markers}
    for e in sorted(events, key=lambda e: (e.get("date") or "", e.get("commit") or "")):
        p = e.get("path", "")
        if not p.endswith("/metadata.json") or folder(p) in state \
                and state[folder(p)].get("owner_file"):
            continue
        if e.get("change") == "add":
            continue
        try:
            before = json.loads(blobs.get((e["commit"] + "^", p)) or "null")
        except (TypeError, ValueError):
            continue                  # unreadable / not JSON: not known
        if isinstance(before, dict) and isinstance(before.get("owners"), list):
            r = state.setdefault(folder(p), {"owners": [], "codeowners": [],
                                             "orphan_marker": False})
            r.update(owner_file="metadata.json", owners=list(before["owners"]))
    return state


def collect(events, blobs, prs=(), issues=(), end_files=(), **kw):
    if "start_state" not in kw:
        kw["start_state"] = implied_start_state(events, blobs, end_files,
                                                kw.pop("start_files", None))
    return gather.collect_module_ownership(
        events, [{"sha": S1, "pr": 7471}], list(prs), list(issues), Blobs(blobs),
        list(end_files), "2026-10-01", "2026-10-31", web="https://github.com/o/r", **kw)


class TestOwnerFiles(unittest.TestCase):

    def test_owner_file_added_removed_emptied(self):
        nsg = "avm/res/network/network-security-group/metadata.json"
        kv = "avm/res/key-vault/vault/metadata.json"
        evs = [{"commit": S1, "path": nsg, "change": "modify", "date": "2026-10-06",
                "author": "arjenhuitema"},
               {"commit": S2, "path": kv, "change": "modify", "date": "2026-10-09",
                "author": "x"}]
        got = collect(evs, {(S1 + "^", nsg): meta([]), (S1, nsg): meta(["arjenhuitema"]),
                            (S2 + "^", kv): meta(["a", "b"]), (S2, kv): meta([])})
        self.assertEqual([(e["kind"], e["module"], e["people"]) for e in got["events"]], [
            ("owner_added", "avm/res/network/network-security-group", ["arjenhuitema"]),
            ("owner_needed", "avm/res/key-vault/vault", []),
            ("owner_removed", "avm/res/key-vault/vault", ["a", "b"])])
        self.assertEqual(got["events"][0]["source"], {
            "type": "commit", "sha": S1, "url": "https://github.com/o/r/commit/" + S1,
            "pr": 7471})
        self.assertEqual(got["events"][0]["via"], "metadata.json")

    def test_codeowners_patterns(self):
        before = "/avm/res/a/ @t-a\n/avm/res/b/** @t-b @t-c\n"
        after = "/avm/res/a/ @t-a @t-new\n/avm/res/b/** @t-c\n"
        evs = [{"commit": S1, "path": ".github/CODEOWNERS", "change": "modify",
                "date": "2026-10-02"}]
        got = collect(evs, {(S1 + "^", ".github/CODEOWNERS"): before,
                            (S1, ".github/CODEOWNERS"): after})
        self.assertEqual([(e["kind"], e["module"], e["people"]) for e in got["events"]], [
            ("owner_added", "avm/res/a", ["t-new"]),
            ("owner_removed", "avm/res/b", ["t-b"])])

    def test_unreadable_version_is_not_no_owners(self):
        p = "avm/res/x/y/metadata.json"
        got = collect([{"commit": S1, "path": p, "change": "modify", "date": "2026-10-02"}],
                      {(S1 + "^", p): gather.BlobUnreadable("boom")})
        self.assertEqual(got["events"], [])
        self.assertEqual(got["unread"], [{"path": p, "commit": S1}])


class TestMarkersAndLabels(unittest.TestCase):

    def test_orphan_markers_labels_and_standing_list(self):
        evs = [{"commit": S1, "path": "avm/res/a/b/ORPHANED.md", "change": "add",
                "date": "2026-10-03", "author": "core"},
               {"commit": S2, "path": "avm/res/c/d/ORPHANED.md", "change": "delete",
                "date": "2026-10-04", "author": "newowner"}]
        issue = {"number": 7600, "title": "[AVM Module Issue]: avm/res/e/f has no owner",
                 "labels": ["Status: Owner Needed"], "state": "open",
                 "author": "someone", "created_at": "2026-10-10T00:00:00Z",
                 "url": "https://github.com/o/r/issues/7600"}
        old = dict(issue, number=7000, title="avm/res/g/h orphaned",
                   labels=["Status: Module Orphaned :yellow_circle:"],
                   created_at="2026-01-01T00:00:00Z")
        got = collect(evs, {}, issues=[issue, old],
                      end_files=["avm/res/a/b/ORPHANED.md", "avm/res/a/b/main.bicep"])
        kinds = [(e["kind"], e["module"]) for e in got["events"]]
        self.assertEqual(kinds, [("orphaned", "avm/res/a/b"), ("adopted", "avm/res/c/d"),
                                 ("owner_needed", "avm/res/e/f")])
        label_ev = got["events"][2]
        self.assertEqual((label_ev["quote"], label_ev["via"], label_ev["source"]["number"]),
                         (issue["title"], "Status: Owner Needed", 7600))
        self.assertEqual([(w["module"], w["marker"]) for w in got["without_owner"]], [
            ("avm/res/a/b", "ORPHANED.md"),
            ("avm/res/e/f", "Status: Owner Needed"),
            ("avm/res/g/h", "Status: Module Orphaned :yellow_circle:")])
        self.assertEqual(got["without_owner"][0]["since"], "2026-10-03")

    def test_a_label_asking_the_owner_to_respond_is_not_owner_needed(self):
        pr = {"number": 7502, "title": "feat(avm/res/x/y): add z", "state": "open",
              "labels": ["Needs: Module Owner :mega:"], "author": "a",
              "created_at": "2026-10-09T00:00:00Z", "url": "u"}
        got = collect([], {}, prs=[pr])
        self.assertEqual((got["events"], got["without_owner"]), ([], []))

    def test_a_stale_label_after_an_owner_was_added(self):
        p = "avm/res/sql/server/metadata.json"
        pr = {"number": 7448, "title": "feat(avm/res/sql/server): x", "state": "open",
              "labels": ["Status: Module Orphaned :yellow_circle:"], "author": "a",
              "created_at": "2026-10-02T00:00:00Z", "url": "u"}
        got = collect([{"commit": S1, "path": p, "change": "modify",
                        "date": "2026-10-06"}],
                      {(S1 + "^", p): meta([]), (S1, p): meta(["arjenhuitema"])},
                      prs=[pr])
        self.assertEqual([e["kind"] for e in got["events"]], ["orphaned", "owner_added"])
        self.assertEqual(got["without_owner"], [])

    def test_label_patterns_are_configurable(self):
        pats = gather.ownership_label_patterns(
            {"ACTIVITY_OWNERSHIP_LABELS": "owner_needed=maintainer wanted"})
        self.assertEqual(pats, {"owner_needed": ("maintainer wanted",)})
        with self.assertRaises(SystemExit):
            gather.ownership_label_patterns({"ACTIVITY_OWNERSHIP_LABELS": "nonsense"})


class TestStoredAndViewed(unittest.TestCase):

    def fact(self):
        return {"collected": True, "events": [
            {"kind": "owner_added", "module": "avm/res/a", "people": ["p"],
             "date": "2026-05-06", "source": {"type": "commit", "sha": S1},
             "via": "metadata.json"}], "without_owner": [],
            "label_patterns": {"orphaned": ["orphan"]}, "owner_files": ["metadata.json"]}

    def test_fold_validate_extract(self):
        from test_pr_activity import _folded
        conn = graphstore.open_store(":memory:")
        conn = _folded(None)
        b = {"meta": {"owner": "o", "repo": "r", "from": "2026-05-01", "to": "2026-05-31",
                      "period": {"from": "2026-05-01", "to": "2026-05-31"},
                      "ref_date": "2026-05-31", "base_branch": "main",
                      "branches": ["main"], "clone_sha": None},
             "prs": [], "issues": [], "commits": [], "module_ownership": self.fact()}
        gather.fold_bundle(conn, b)
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c for c in report.checks if not c["ok"]], [])
        v = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(v["module_ownership"], self.fact())

    def test_shape(self):
        self.assertIsNone(validate._fact_shape_problem("module_ownership", self.fact()))
        bad = self.fact()
        bad["events"][0]["kind"] = "guessed"
        self.assertIsNotNone(validate._fact_shape_problem("module_ownership", bad))
        self.assertIsNotNone(validate._fact_shape_problem(
            "module_ownership", dict(self.fact(), events=1)))


if __name__ == "__main__":
    unittest.main()
