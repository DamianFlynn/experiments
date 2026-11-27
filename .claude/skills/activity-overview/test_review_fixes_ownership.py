"""Review fixes (issue #97): ownership as of any day, unread is never
empty, replaced history verified at both ends, check pages that come back
incomplete. Each reproduces its finding before the fix."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
from test_module_ownership import S1, S2, S3, Blobs, collect, meta  # noqa: E402
from test_pr_activity import _folded, check_run, gpr, pr_node, commit_node, clock  # noqa: E402
from test_pr_whole_record import HistoryGraphQL, sha  # noqa: E402
from test_story import GIT_TEST_ENV, git  # noqa: E402

P = "avm/res/x/y/metadata.json"


def label_item(n, created, closed=None, labels=("Status: Module Orphaned",)):
    out = {"number": n, "title": "avm/res/x/y needs an owner", "labels": list(labels),
           "state": "closed" if closed else "open", "author": "a",
           "created_at": created, "url": "u{}".format(n), "comments_list": []}
    if closed:
        out["closed_at"] = closed
    return out


class TestOwnerFileParse(unittest.TestCase):

    def test_invalid_json_is_unread_not_removed(self):
        got = collect([{"commit": S1, "path": P, "change": "modify",
                        "date": "2026-10-03"}],
                      {(S1 + "^", P): meta(["a"]), (S1, P): "{ not json"})
        self.assertEqual(got["events"], [])
        self.assertEqual(got["unread"], [{"path": P, "commit": S1,
                                          "reason": "not valid JSON"}])


class TestRecordsAsOfTheWindowEnd(unittest.TestCase):

    def test_closed_in_november_is_open_in_october(self):
        got = collect([], {}, issues=[label_item(1, "2026-10-05T00:00:00Z",
                                                 closed="2026-11-04T00:00:00Z")])
        self.assertEqual([w["module"] for w in got["without_owner"]], ["avm/res/x/y"])

    def test_created_in_november_does_not_exist(self):
        got = collect([], {}, issues=[label_item(1, "2026-11-04T00:00:00Z")])
        self.assertEqual((got["events"], got["without_owner"]), ([], []))


class TestLatestStateWins(unittest.TestCase):

    def test_added_then_emptied_is_without_owner(self):
        evs = [{"commit": S1, "path": P, "change": "modify", "date": "2026-10-06"},
               {"commit": S2, "path": P, "change": "modify", "date": "2026-10-09"}]
        got = collect(evs, {(S1 + "^", P): meta([]), (S1, P): meta(["o"]),
                            (S2 + "^", P): meta(["o"]), (S2, P): meta([])},
                      issues=[label_item(1, "2026-10-02T00:00:00Z")])
        self.assertEqual([w["module"] for w in got["without_owner"]], ["avm/res/x/y"])


class TestIdentities(unittest.TestCase):
    """A marker commit's author is the store's identity: acquire collects
    ownership after author resolution (the event keeps the remapped login)."""

    def test_marker_event_uses_the_resolved_author(self):
        src = open(os.path.join(os.path.dirname(__file__), "gather.py")).read()
        resolved = src.index("remap_event_authors(symbol_events, by_sha)")
        collected = src.index("module_ownership = collect_module_ownership(")
        self.assertLess(resolved, collected)
        got = collect([{"commit": S1, "path": "avm/res/a/b/ORPHANED.md", "change": "add",
                        "date": "2026-10-03", "author": "resolved-login"}], {})
        self.assertEqual(got["events"][0]["people"], ["resolved-login"])


def _store(*facts):
    conn = _folded(None)
    for frm, to, fact in facts:
        gather.fold_bundle(conn, {
            "meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                     "period": {"from": frm, "to": to}, "ref_date": to,
                     "base_branch": "main", "branches": ["main"], "clone_sha": None},
            "prs": [], "issues": [], "commits": [], "module_ownership": fact})
    return conn


class TestMergedViews(unittest.TestCase):

    def oct_fact(self):
        return collect([{"commit": S1, "path": "avm/res/a/b/ORPHANED.md",
                         "change": "add", "date": "2026-10-20", "author": "x"},
                        {"commit": S2, "path": "avm/res/c/d/ORPHANED.md",
                         "change": "delete", "date": "2026-10-10", "author": "y"}],
                       {}, issues=[label_item(1, "2026-10-02T00:00:00Z",
                                              closed="2026-11-04T00:00:00Z")],
                       end_files=["avm/res/a/b/ORPHANED.md"])

    def test_a_week_inside_the_month_is_reconstructed(self):
        conn = _store(("2026-10-01", "2026-10-31", self.oct_fact()))
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-10-07")
        mods = {w["module"]: w for w in v["module_ownership"]["without_owner"]}
        self.assertEqual(sorted(mods), ["avm/res/c/d", "avm/res/x/y"])  # not a/b yet
        self.assertEqual([e["kind"] for e in v["module_ownership"]["events"]],
                         ["orphaned"])          # the label event of 2 Oct only

    def test_a_collected_empty_window_clears_the_list(self):
        nov = collect([], {}, issues=[label_item(1, "2026-10-02T00:00:00Z",
                                                 closed="2026-11-04T00:00:00Z")],
                      end_files=["avm/res/a/b/ORPHANED.md"])
        nov["labelled_items"] = []
        self.assertEqual(
            [w["module"] for w in gather.collect_module_ownership(
                [], [], [], [label_item(1, "2026-10-02T00:00:00Z",
                                        closed="2026-11-04T00:00:00Z")],
                Blobs({}), [], "2026-11-01", "2026-11-30",
                start_files=[])["without_owner"]], [])
        empty = gather.collect_module_ownership([], [], [], [], Blobs({}), [],
                                                "2026-11-01", "2026-11-30",
                                                start_files=[])
        conn = _store(("2026-10-01", "2026-10-31", self.oct_fact()),
                      ("2026-11-01", "2026-11-30", empty))
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-11-30")
        self.assertEqual(v["module_ownership"]["without_owner"], [])
        self.assertEqual(len(v["module_ownership"]["events"]), 3)   # October's kept

    def test_an_old_fact_says_a_day_inside_is_unavailable(self):
        old = self.oct_fact()
        # a fact gathered before the complete start state (the shape now
        # marked by `start_state`; before, by `start_without_owner`)
        for k in ("start_state", "start_state_source", "window", "labelled_items"):
            old.pop(k)
        conn = _store(("2026-10-01", "2026-10-31", old))
        v = extract.extract(conn, "o", "r", "2026-10-01", "2026-10-07")
        self.assertEqual(v["module_ownership"]["without_owner"], [])
        self.assertIn("reconstructed", v["module_ownership"]["without_owner_unavailable"])


class TestRecompute(unittest.TestCase):

    def test_recompute_replaces_only_the_ownership_fact(self):
        conn = _store(("2026-10-01", "2026-10-31", {"collected": True, "events": [],
                                                    "without_owner": []}))
        before = graphstore.get_window_facts(conn, "o", "r", "2026-10-01", "2026-10-31")
        fact = gather.recompute_module_ownership(
            conn, "o", "r", "2026-10-01", "2026-10-31", "/nonexistent",
            read_blob=Blobs({}), end_files=["avm/res/a/b/ORPHANED.md"])
        after = graphstore.get_window_facts(conn, "o", "r", "2026-10-01", "2026-10-31")
        self.assertEqual(after["facts"]["module_ownership"], fact)
        self.assertEqual([w["module"] for w in fact["without_owner"]], ["avm/res/a/b"])
        self.assertEqual({k: v for k, v in after["facts"].items() if k != "module_ownership"},
                         {k: v for k, v in before["facts"].items() if k != "module_ownership"})


class TestReplacedNeedsBothEnds(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-rep-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        src = self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
        open(os.path.join(src, "a"), "w").write("1")
        git("add", "-A", cwd=src)
        git("commit", "-q", "-m", "base", cwd=src, date="2026-04-01")
        self.base = git("rev-parse", "HEAD", cwd=src).strip()
        open(os.path.join(src, "a"), "w").write("2")
        git("commit", "-q", "-am", "old", cwd=src, date="2026-05-01")
        self.old = git("rev-parse", "HEAD", cwd=src).strip()
        git("reset", "-q", "--hard", self.base, cwd=src)
        self.clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "file://" + src, self.clone)

    def test_missing_after_is_unavailable(self):
        row = {}
        gather.collect_replaced_commits(
            row, self.clone, [{"before": self.old, "after": "e" * 40, "at": "t"}],
            "HEAD", [], gather.run_git)
        self.assertEqual(row["replaced"][0]["status"], "unavailable")
        self.assertIn("after commit", row["replaced"][0]["reason"])


class TestCheckPages(unittest.TestCase):

    def test_a_missing_object_keeps_known_evidence(self):
        hist = [(sha(i), "2026-09-01T00:00:00Z", "FAILURE") for i in range(3)]
        node = pr_node(1, [commit_node(sha(100), "2026-09-20T00:00:00Z",
                                       [check_run("t")])], total=4)

        class Missing(HistoryGraphQL):
            def __call__(self, query, variables=None, errors=None):
                out = super().__call__(query, variables, errors)
                if "object(oid:" in query:
                    out["repository"] = {k: None for k in out["repository"]}
                return out
        row = gather.collect_pr_activity([gpr(1)], Missing({1: node}, history={1: hist}),
                                         "o", "r", clock=clock)["prs"][0]
        older = row["commits"][0]
        self.assertEqual((older["rollup"], older.get("state_only")), ("failing", True))

    def test_a_page_without_commits_fails_the_history(self):
        node = pr_node(1, [commit_node(sha(100), "2026-09-20T00:00:00Z",
                                       [check_run("t")])], total=4)

        class NoCommits(HistoryGraphQL):
            def __call__(self, query, variables=None, errors=None):
                out = super().__call__(query, variables, errors)
                if "commits(first:" in query:
                    out["repository"]["pullRequest"] = {}
                return out
        row = gather.collect_pr_activity([gpr(1)], NoCommits({1: node}, history={1: []}),
                                         "o", "r", clock=clock)["prs"][0]
        self.assertEqual(row["history_status"], "failed")


if __name__ == "__main__":
    unittest.main()
