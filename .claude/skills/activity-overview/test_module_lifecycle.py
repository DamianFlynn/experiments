"""Module lifecycle from the tree (issue #97, checked against the October
2026 clone): a module folder is live, deprecated, orphaned or a stub;
"without an owner" is about live (and orphaned) modules; lifecycle events
and what a module IS (its metadata file) reach the module rows and the
stories; proposed new modules come from the PR's own added entry file.
Each test reproduces its gap before the fix."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
from test_module_ownership import Blobs  # noqa: E402
from test_story import comment, pr  # noqa: E402
from test_story_groups import merged_pr, rows, talk, view  # noqa: E402
from test_story_rows_modules import hist  # noqa: E402
from test_story_substance import commit, enriched  # noqa: E402

LIVE, DEP, STUB, ORPH = ("avm/res/a/live", "avm/ptn/b/dep", "avm/ptn/c/stub",
                         "avm/res/d/orph")
START = "s" * 40


def meta(owners, name="X", desc="AVM Resource Module for X", typ="Microsoft.X/xs"):
    return json.dumps({"moduleDisplayName": name, "moduleDescription": desc,
                       "canonicalType": typ, "owners": owners})


def start_state():
    files = [LIVE + "/main.bicep", LIVE + "/metadata.json",
             DEP + "/main.bicep", DEP + "/metadata.json", DEP + "/DEPRECATED.md",
             STUB + "/metadata.json",
             ORPH + "/main.bicep", ORPH + "/metadata.json", ORPH + "/ORPHANED.md"]

    def run(argv):
        if argv[3] == "ls-tree":
            return "\n".join(files)
        if argv[3] == "log":
            return "\x1e2026-05-04T00:00:00Z\n\n" + DEP + "/DEPRECATED.md\n"
        raise AssertionError(argv)
    blobs = Blobs({(START, LIVE + "/metadata.json"): meta([], "Live One"),
                   (START, DEP + "/metadata.json"): meta([], "Dep One"),
                   (START, DEP + "/DEPRECATED.md"):
                       "⚠️THIS MODULE IS DEPRECATED.⚠️\n\n"
                       "- Use avm/ptn/b/new instead.\n",
                   (START, STUB + "/metadata.json"): meta([], "Stub One"),
                   (START, ORPH + "/metadata.json"): meta(["o"], "Orph One")})
    with mock.patch.object(gather, "window_start_commit", lambda *a, **k: START):
        state, _unread = gather.ownership_start_state("/c", "2026-10-01", run=run,
                                                      read_blob=blobs)
    return state


class TestLifecycleFromTheTree(unittest.TestCase):

    def fact(self, events=(), commits=(), blobs=None):
        return gather.collect_module_ownership(
            list(events), list(commits), [], [], Blobs(blobs or {}), [],
            "2026-10-01", "2026-10-31", start_state=start_state())

    def test_start_state_carries_lifecycle_and_what_a_module_is(self):
        st = start_state()
        self.assertEqual((st[LIVE]["entry"], st[STUB]["entry"]), (True, False))
        self.assertTrue(st[DEP]["deprecated"])
        self.assertIn("Use avm/ptn/b/new instead.", st[DEP]["deprecated_text"])
        self.assertEqual(st[LIVE]["info"], {"display_name": "Live One",
                                            "description": "AVM Resource Module for X",
                                            "resource_type": "Microsoft.X/xs"})

    def test_without_owner_is_live_and_orphaned_only(self):
        f = self.fact()
        self.assertEqual([(r["module"], r["lifecycle"]) for r in f["without_owner"]],
                         [(LIVE, "live"), (ORPH, "orphaned")])
        self.assertEqual(f["without_owner"][0]["display_name"], "Live One")
        self.assertEqual([r["module"] for r in f["deprecated_modules"]], [DEP])
        self.assertIn("instead", f["deprecated_modules"][0]["deprecated_text"])
        self.assertEqual([r["module"] for r in f["stub_modules"]], [STUB])
        self.assertEqual(f["modules_at_end"][LIVE]["lifecycle"], "live")
        self.assertIsNone(validate._fact_shape_problem("module_ownership", f))

    def test_lifecycle_events_in_the_window(self):
        s1, s2, s3, s4 = "1" * 40, "2" * 40, "3" * 40, "4" * 40
        new = "avm/res/e/new"
        evs = [{"commit": s1, "path": LIVE + "/DEPRECATED.md", "change": "add",
                "date": "2026-10-03", "author": "m"},
               {"commit": s2, "path": new + "/main.bicep", "change": "add",
                "date": "2026-10-05", "author": "n"},
               {"commit": s3, "path": STUB + "/metadata.json", "change": "modify",
                "date": "2026-10-07", "author": "m"},
               {"commit": s4, "path": "utilities/x/y/main.bicep", "change": "add",
                "date": "2026-10-08", "author": "n"}]
        commits = [{"sha": x, "committed_at": "2026-10-0{}T10:00:00Z".format(i + 3)}
                   for i, x in enumerate((s1, s2, s3, s4))]
        blobs = {(s1, LIVE + "/DEPRECATED.md"): "Deprecated: use avm/res/a/next.\n",
                 (s3 + "^", STUB + "/metadata.json"): meta([], "Stub One"),
                 (s3, STUB + "/metadata.json"): meta([], "Stub Renamed")}
        f = self.fact(evs, commits, blobs)
        kinds = [(e["kind"], e["module"]) for e in f["events"]]
        self.assertIn(("deprecated", LIVE), kinds)
        self.assertIn(("module_added", new), kinds)
        self.assertIn(("module_info_changed", STUB), kinds)
        self.assertNotIn("utilities/x/y", [m for _k, m in kinds])
        dep = next(e for e in f["events"] if e["kind"] == "deprecated")
        self.assertEqual(dep["marker_text"], "Deprecated: use avm/res/a/next.")
        before = extract.ownership_state(f, "2026-10-02")
        after = extract.ownership_state(f, "2026-10-31")
        self.assertEqual(before["modules"][LIVE]["lifecycle"], "live")
        self.assertEqual(after["modules"][LIVE]["lifecycle"], "deprecated")
        self.assertEqual(after["modules"][new]["lifecycle"], "live")
        self.assertEqual(after["modules"][STUB]["display_name"], "Stub Renamed")
        self.assertNotIn(LIVE, [r["module"] for r in after["without_owner"]])

    def test_shapes_are_validated(self):
        f = self.fact()
        for key, bad in (("start_state", {LIVE: {"entry": "yes"}}),
                         ("start_state", {LIVE: {"info": {"display_name": 3}}}),
                         ("deprecated_modules", [{"module": DEP}]),
                         ("modules_at_end", {LIVE: {"lifecycle": 1}})):
            self.assertIsNotNone(validate._fact_shape_problem(
                "module_ownership", dict(f, **{key: bad})), (key, bad))

    def test_offline_flag_name(self):
        args = gather.parse_args(["--owner", "o", "--repo", "r", "--from", "2026-10-01",
                                  "--to", "2026-10-31", "--store", "s.db",
                                  "--recompute-modules"])
        self.assertTrue(args.recompute_ownership)


class TestReaders(unittest.TestCase):

    def view(self):
        f = TestLifecycleFromTheTree.fact(None)
        p = merged_pr(1, title="fix(avm/res/a/live): x", labels=["Needs: Module Owner :mega:"],
                      comments_list=talk(1))
        b = view([p, pr(2, title="feat: new", comments_list=talk(2))],
                 pr_history=hist({"number": 2, "status": "fetched", "commits": [dict(
                     commit("a" * 40, "2026-05-10T00:00:00Z", "new",
                            ["avm/res/e/new/main.bicep", "utilities/t/x/main.bicep"]),
                     added=["avm/res/e/new/main.bicep", "utilities/t/x/main.bicep"])]}))
        f = dict(f, owners_at_end={LIVE: ["olga"]})
        b["module_ownership"] = f
        b["commits"] = [{"sha": "c" * 40, "message": "fix: x (#1)", "pr": 1,
                         "committed_at": "2026-05-20T10:00:00Z",
                         "files": [LIVE + "/main.bicep"]}]
        b["module_notes"] = {"collected": True, "changelogs": [], "versions": [],
                             "tags": {"collected": True, "items": []}}
        return enriched(b)

    def test_what_a_module_is_reaches_rows_and_stories(self):
        b = self.view()
        r = rows(b)["train-pr-1"]
        self.assertEqual(r["module_info"][LIVE]["display_name"], "Live One")
        mc = {m["area"]: m for m in b["module_changes"]["modules"]}
        self.assertEqual((mc[LIVE]["display_name"], mc[LIVE]["module_lifecycle"]),
                         ("Live One", "live"))

    def test_lifecycle_check_against_the_module_layer(self):
        b = {"module_ownership": {"events": [
                {"kind": "module_added", "module": "avm/res/e/new", "date": "2026-05-02"},
                {"kind": "deprecated", "module": DEP, "date": "2026-05-03"}],
                "modules_at_end": {}},
             "module_changes": {"modules": [{"area": "avm/res/e/new", "status": "new"},
                                            {"area": "avm/res/f/other", "status": "new"}]}}
        link.annotate_module_lifecycle(b)
        self.assertEqual(b["module_changes"]["lifecycle_check"], {
            "new": {"agree": ["avm/res/e/new"], "only_module_layer": ["avm/res/f/other"],
                    "only_tree": []},
            "deprecated": {"agree": [], "only_module_layer": [], "only_tree": [DEP]}})
        self.assertEqual(b["module_changes"]["modules"][0]["lifecycle_events"][0]["kind"],
                         "module_added")

    def test_a_closed_pr_waited_on_the_named_owner(self):
        b = self.view()
        o = link.slice_train(b, "train-pr-1")["conversation"]["outcome"]
        w = o["owner_response_requested"][0]
        self.assertEqual((w["waited_on"], w["owners"], w["owners_source"]),
                         ("the module owner", ["olga"], "owner_file"))
        self.assertEqual(o["items"][0]["module_owners"], {LIVE: ["olga"]})

    def test_a_proposed_module_is_a_module_folder(self):
        b = self.view()
        p2 = next(p for p in b["prs"] if p["number"] == 2)
        self.assertEqual(p2["proposes_new_module"], ["avm/res/e/new"])
        o = link.slice_train(b, "train-pr-2")["conversation"]["outcome"]
        self.assertEqual(o["items"][0]["proposes_new_module"], ["avm/res/e/new"])


if __name__ == "__main__":
    unittest.main()
