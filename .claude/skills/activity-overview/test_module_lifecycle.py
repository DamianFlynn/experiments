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


class TestMarkerTextsAndProposed(unittest.TestCase):

    def test_orphan_marker_text_is_kept(self):
        st = start_state()
        st[ORPH]["marker_text"] = "THIS MODULE IS CURRENTLY ORPHANED.\n- Become its owner: see the list."
        f = gather.collect_module_ownership([], [], [], [], Blobs({}), [], "2026-10-01",
                                            "2026-10-31", start_state=st)
        row = next(r for r in f["without_owner"] if r["module"] == ORPH)
        self.assertIn("Become its owner", row["marker_text"])

    def test_gather_reads_the_orphan_marker_text(self):
        files = [ORPH + "/main.bicep", ORPH + "/metadata.json", ORPH + "/ORPHANED.md"]

        def run(argv):
            return "\n".join(files) if argv[3] == "ls-tree" else ""
        blobs = Blobs({(START, ORPH + "/metadata.json"): meta([]),
                       (START, ORPH + "/ORPHANED.md"): "ORPHANED.\n\n- Take it over."})
        with mock.patch.object(gather, "window_start_commit", lambda *a, **k: START):
            state, _u = gather.ownership_start_state("/c", "2026-10-01", run=run,
                                                     read_blob=blobs)
        self.assertEqual(state[ORPH]["marker_text"], "ORPHANED.\n- Take it over.")

    def test_shared_boilerplate_is_said_once(self):
        st = start_state()
        boiler = "THIS MODULE IS DEPRECATED.\n- It will no longer receive any updates."
        for m in (DEP, "avm/ptn/b/dep2"):
            st.setdefault(m, {"owners": [], "codeowners": [], "orphan_marker": False})
            st[m].update(deprecated=True, deprecated_text=boiler, entry=True)
        f = gather.collect_module_ownership([], [], [], [], Blobs({}), [], "2026-10-01",
                                            "2026-10-31", start_state=st)
        rows = {r["module"]: r for r in f["deprecated_modules"]}
        self.assertTrue(all(r["marker_text_shared"] for r in rows.values()))
        self.assertTrue(all("deprecated_text" not in r for r in rows.values()))
        self.assertEqual(f["marker_texts"]["deprecated"], {"text": boiler, "modules": 2})

    def test_a_replacement_only_from_explicit_text(self):
        self.assertIsNone(gather.replacement_named([
            ("DEPRECATED marker", "It is recommended to migrate to a replacement/"
                                  "alternative version of the module, if available.")]))
        got = gather.replacement_named([("README.md", "> This module is replaced by "
                                                      "`avm/ptn/b/new`.")])
        self.assertEqual(got, {"module": "avm/ptn/b/new", "file": "README.md",
                               "quote": "> This module is replaced by `avm/ptn/b/new`."})

    def test_a_proposed_module_is_described_and_proposed(self):
        hist_ = {"prs": [{"number": 7560, "commits": [{
            "sha": "p" * 40, "parents": 1, "committed_at": "2026-10-23T10:00:00Z",
            "added": [STUB + "/main.bicep", STUB + "/metadata.json",
                      "avm/res/n/gallery-image/main.bicep",
                      "avm/res/n/gallery-image/metadata.json",
                      "utilities/t/x/metadata.json"]}]}]}
        blobs = Blobs({("p" * 40, "avm/res/n/gallery-image/metadata.json"):
                       meta([], "Gallery Image", "AVM Resource Module for Gallery Image",
                            "Microsoft.AzureStackHCI/galleryImages"),
                       ("p" * 40, STUB + "/metadata.json"): meta([], "Stub One")})
        prop = gather.proposed_module_info(hist_, blobs, "2026-10-31T23:59:59Z")
        self.assertEqual(sorted(prop), sorted(["avm/res/n/gallery-image", STUB]))
        f = gather.collect_module_ownership([], [], [], [], Blobs({}), [], "2026-10-01",
                                            "2026-10-31", start_state=start_state(),
                                            proposed=prop)
        mods = f["modules_at_end"]
        self.assertEqual((mods["avm/res/n/gallery-image"]["lifecycle"],
                          mods["avm/res/n/gallery-image"]["display_name"]),
                         ("proposed", "Gallery Image"))
        self.assertEqual(mods[STUB]["lifecycle"], "proposed")    # a stub its PR fills
        self.assertNotIn(STUB, [r["module"] for r in f["stub_modules"]])
        early = extract.ownership_state(f, "2026-10-20")
        self.assertEqual(early["modules"][STUB]["lifecycle"], "stub")
        self.assertIsNone(validate._fact_shape_problem("module_ownership", f))

    def test_a_stub_whose_entry_file_a_pr_adds_is_proposed(self):
        # the real #7485 shape: the folder holds only its metadata on the
        # base; the PR's own commits add main.bicep, not the metadata file
        hist_ = {"prs": [{"number": 7485, "commits": [{
            "sha": "q" * 40, "parents": 1, "committed_at": "2026-10-07T10:00:00Z",
            "added": [STUB + "/main.bicep", STUB + "/README.md"]}]}]}
        prop = gather.proposed_module_info(hist_, Blobs({}), "2026-10-31T23:59:59Z")
        self.assertEqual(prop, {STUB: {"prs": [7485], "commit": "q" * 40,
                                       "committed_at": "2026-10-07T10:00:00Z"}})
        f = gather.collect_module_ownership([], [], [], [], Blobs({}), [], "2026-10-01",
                                            "2026-10-31", start_state=start_state(),
                                            proposed=prop)
        self.assertEqual((f["modules_at_end"][STUB]["lifecycle"],
                          f["modules_at_end"][STUB]["display_name"]),
                         ("proposed", "Stub One"))


class TestTrainsAndAutomation(unittest.TestCase):

    def test_open_pr_trains_carry_their_modules(self):
        b = view([pr(1, title="feat: z", comments_list=talk(1))],
                 pr_history=hist({"number": 1, "status": "fetched", "commits": [
                     commit("a" * 40, "2026-05-10T00:00:00Z", "z",
                            ["avm/res/e/new/main.bicep", ".github/workflows/x.yml"])]}))
        b = enriched(b)
        t = next(t for t in b["trains"] if t["id"] == "train-pr-1")
        self.assertEqual((t["modules"], t["modules_source"]), (["avm/res/e/new"], "pr_commits"))
        self.assertEqual(t["pr_code_areas"], [".github/workflows", "avm/res/e/new"])
        self.assertEqual(rows(b)["train-pr-1"]["modules"], t["modules"])

    def test_on_behalf_of_from_the_record_per_pr(self):
        f = {"collected": True, "prs": [
            {"number": 1, "events": [{"event": "assigned", "at": "2026-05-01T00:00:00Z",
                                      "actor": "agent", "assignee": "segraef"}]},
            {"number": 2, "events": []}]}
        i = {"number": 9, "title": "t", "body": "", "author": "reporter", "state": "open",
             "created_at": "2026-04-01T00:00:00Z", "url": "u9", "labels": [],
             "comments_list": []}
        b = view([pr(1, author="agent", author_type="Bot", comments_list=talk(1)),
                  pr(2, author="agent", closes=[9], comments_list=talk(2))], [i],
                 pr_activity=f)
        b = enriched(b)
        a = b["people"]["agent"]["automation"]
        self.assertEqual(a["on_behalf_of_by_pr"], {
            "1": {"login": "segraef", "basis": "assignee", "pr": 1},
            "2": {"login": "reporter", "basis": "closes the issue of", "pr": 2, "issue": 9}})


class TestBallAndReviewState(unittest.TestCase):

    def test_a_stale_approval_puts_the_ball_with_the_approver(self):
        from test_story import review
        h = hist({"number": 1, "status": "fetched", "commits": [
            commit("a" * 40, "2026-05-20T00:00:00Z", "rework", [LIVE + "/main.bicep"])]})
        p = pr(1, reviews=[review("bob", "2026-05-05T00:00:00Z", "approved", 6)])
        b = enriched(view([p], pr_history=h))
        o = link.slice_train(b, "train-pr-1")["conversation"]["outcome"]
        self.assertEqual(o["items"][0]["review_state"]["state"], "approved_stale")
        self.assertEqual((o["ball"]["state"], o["ball"]["with"], o["ball"]["rule"]),
                         ("awaiting reviewer", ["bob"],
                          "R8 approval made stale by a later push"))


class TestUnavailableEndToEnd(unittest.TestCase):
    """A clone whose history does not reach the window's start: through
    gather (the offline recompute) -> store -> view, the ownership state is
    unavailable, with the reason; the events are still there."""

    def test_shallow_clone(self):
        import shutil
        import subprocess
        import tempfile
        import graphstore
        from test_pr_activity import _folded
        from test_story import git
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "src")
            os.mkdir(src)
            git("init", "-q", "-b", "main", cwd=src)
            os.makedirs(os.path.join(src, LIVE))
            with open(os.path.join(src, LIVE, "metadata.json"), "w") as fh:
                fh.write(meta(["alice"]))
            with open(os.path.join(src, LIVE, "main.bicep"), "w") as fh:
                fh.write("param a string\n")
            git("add", ".", cwd=src)
            git("commit", "-q", "-m", "module", cwd=src, date="2026-09-10")
            with open(os.path.join(src, LIVE, "ORPHANED.md"), "w") as fh:
                fh.write("ORPHANED.\n")
            git("add", ".", cwd=src)
            git("commit", "-q", "-m", "orphan it", cwd=src, date="2026-10-05")
            clone = os.path.join(tmp, "clone")
            git("clone", "-q", "--shallow-since=2026-10-01", "file://" + src, clone)
            self.assertEqual(gather.window_start_commit(clone, "2026-10-01"), None)
            conn = _folded(None)
            head = git("rev-parse", "HEAD", cwd=clone).strip()
            gather.fold_bundle(conn, {
                "meta": {"owner": "o", "repo": "r", "from": "2026-10-01",
                         "to": "2026-10-31", "period": {"from": "2026-10-01",
                                                        "to": "2026-10-31"},
                         "ref_date": "2026-10-31", "base_branch": "main",
                         "branches": ["main"], "clone_sha": head},
                "prs": [], "issues": [], "commits": [],
                "code_events": [{"commit": head, "path": LIVE + "/ORPHANED.md",
                                 "change": "add", "date": "2026-10-05", "author": "x"}]})
            fact = gather.recompute_module_ownership(conn, "o", "r", "2026-10-01",
                                                     "2026-10-31", clone)
            self.assertEqual(fact["start_state_source"], "markers_only")
            self.assertEqual(fact["without_owner"], [])
            self.assertIn("never read", fact["without_owner_unavailable"])
            v = extract.extract(conn, "o", "r", "2026-10-01", "2026-10-15")
            mo = v["module_ownership"]
            self.assertEqual(mo["without_owner"], [])
            self.assertIn("never read", mo["without_owner_unavailable"])
            self.assertEqual([e["kind"] for e in mo["events"]], ["orphaned"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
