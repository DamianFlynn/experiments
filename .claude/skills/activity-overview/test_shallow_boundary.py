"""Shallow-boundary commits (#68).

When nothing in the clone margin precedes `--from` (typical for per-module
AVM-Terraform repos), the first in-window commit lands on the shallow boundary:
its parent is grafted away and git reports the WHOLE tree as changed. Before
#68 only code/symbol events were dropped; `commits[].files` kept the whole-tree
list, so every area/module looked touched.

The fix deepens the clone past such a commit (a bounded, earlier `git fetch
--shallow-since`; never `--deepen`, which can cut a merge history, #97) so
its parent is present and its diff is real; only if that fails does it fall back
to dropping the commit's diffs AND clearing its `files`, recorded in
`meta.boundary_dropped_commits`.

Real git against throwaway local `file://` repos (no network); the harness is
shared with test_clone_handling.py.
"""
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import test_clone_handling as tch  # noqa: E402
from _quiet import quiet  # noqa: E402

FROM, TO = "2026-05-18", "2026-05-31"
OLD_FILES = ["modules/a/main.tf", "modules/b/main.tf", "modules/c/main.tf",
             "modules/d/main.tf", "modules/e/main.tf", "docs/café.md"]
CHANGED = ["modules/a/main.tf", "modules/b/main.tf"]


class _BoundaryRepo(tch._RepoCase):
    def write_commit(self, msg, date, paths):
        for rel in paths:
            p = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"# {msg}\n")
        tch._git("add", "-A", cwd=self.src)
        tch._git("commit", "-q", "-m", msg, cwd=self.src, date=date)
        return tch._git("rev-parse", "HEAD", cwd=self.src).strip()

    def issue_repro(self):
        """One old commit adding 6 files (well before from - margin) and one
        in-window commit changing 2 of them."""
        old = self.write_commit("old", "2026-01-10", OLD_FILES)
        new = self.write_commit("new", "2026-05-20", CHANGED)
        return old, new

    def _acquire(self, run_git=None):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", FROM, "--to", TO,
            "--clone-dir", self.clone, "--store", os.path.join(self.tmp, "s.db"),
            "--no-workflows", "--no-releases", "--no-project-board"])
        real_provider = gather.select_code_area_provider
        patches = [
            mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url),
            mock.patch.object(gather, "_paginated",
                              lambda token: (lambda url: ([], None))),
            mock.patch.object(gather, "select_code_area_provider",
                              lambda paths, d: real_provider(
                                  paths, d, which=lambda _n: None)),
            mock.patch.object(gather, "extract_iac_edges", lambda g, d: g),
        ]
        if run_git is not None:
            patches.append(mock.patch.object(gather, "run_git", run_git))
        for p in patches:
            p.start()
        try:
            return gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        finally:
            for p in reversed(patches):
                p.stop()

    @staticmethod
    def area_paths(bundle):
        return sorted({p for a in bundle["code_graph"].get("areas", [])
                       for p in a.get("paths", [])})


def _is_deepen_fetch(args):
    """True for deepen_window_boundary's fetches: a `fetch` past the window's own
    clone bound (an earlier `--shallow-since`, `--unshallow` or `--deepen`)."""
    return "fetch" in args and any(
        str(a).startswith(("--deepen", "--unshallow")) or (
            str(a).startswith("--shallow-since") and a != gather._shallow_selector(FROM))
        for a in args)


def _refusing_deepen(calls=None):
    """A run_git that fails every deepening fetch (e.g. a remote that cannot
    deepen) and passes everything else to real git."""
    real = gather.run_git

    def run(args, cwd=None, timeout=None):
        if _is_deepen_fetch(args):
            if calls is not None:
                calls.append(list(args))
            raise RuntimeError("fatal: deepen refused (test)")
        return real(args, cwd=cwd, timeout=timeout)
    return run


class TestDeepenBoundaryHelper(_BoundaryRepo):
    def test_repro_is_a_boundary_commit_before_deepening(self):
        # Pins the precondition: the clone margin alone does not reach `old`.
        _old, new = self.issue_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        self.assertEqual(gather.shallow_boundary_shas(self.clone), {new})

    def test_deepen_brings_in_the_parent(self):
        old, new = self.issue_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        left = gather.deepen_window_boundary(self.clone, FROM, TO)
        self.assertEqual(left, [])
        self.assertNotIn(new, gather.shallow_boundary_shas(self.clone))
        self.assertEqual(self.log(self.clone), [new, old])

    def test_root_commit_is_not_a_boundary_and_needs_no_fetch(self):
        self.write_commit("root", "2026-05-20", OLD_FILES)
        gather.prepare_clone(self.url, FROM, self.clone)
        calls = []
        real = gather.run_git

        def run(args, cwd=None, timeout=None):
            calls.append(list(args))
            return real(args, cwd=cwd, timeout=timeout)
        self.assertEqual(gather.deepen_window_boundary(self.clone, FROM, TO, run=run), [])
        self.assertFalse(any("fetch" in c for c in calls), calls)

    def test_pre_window_boundary_is_left_alone(self):
        # A margin commit sits on the boundary, the in-window one keeps its parent:
        # nothing to deepen.
        self.write_commit("old", "2026-01-10", OLD_FILES)
        margin = self.write_commit("margin", "2026-05-10", ["modules/c/main.tf"])
        self.write_commit("new", "2026-05-20", CHANGED)
        gather.prepare_clone(self.url, FROM, self.clone)
        self.assertEqual(gather.shallow_boundary_shas(self.clone), {margin})
        calls = []
        self.assertEqual(gather.deepen_window_boundary(
            self.clone, FROM, TO, run=_refusing_deepen(calls)), [])
        self.assertEqual(calls, [])

    def test_deepen_failure_reports_the_commit(self):
        _old, new = self.issue_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        calls = []
        with mock.patch("sys.stderr") as err:
            left = gather.deepen_window_boundary(self.clone, FROM, TO,
                                                 run=_refusing_deepen(calls))
        self.assertEqual(left, [new])
        self.assertIn("could not deepen",
                      "".join(str(a[0][0]) for a in err.write.call_args_list))
        self.assertEqual(len(calls), 1)   # a hard failure is not retried

    def test_deepen_attempts_are_bounded(self):
        _old, new = self.issue_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        calls = []
        real = gather.run_git

        def run(args, cwd=None, timeout=None):
            if _is_deepen_fetch(args):
                calls.append(list(args))
                return ""          # "succeeds" but never moves the boundary
            return real(args, cwd=cwd, timeout=timeout)
        left = gather.deepen_window_boundary(self.clone, FROM, TO, run=run)
        self.assertEqual(left, [new])
        self.assertEqual(len(calls), len(gather.BOUNDARY_DEEPEN_STEPS))


class TestAcquireBoundary(_BoundaryRepo):
    def setUp(self):
        super().setUp()
        quiet(self)

    def test_issue_repro_boundary_commit_is_made_real(self):
        _old, new = self.issue_repro()
        b = self._acquire()
        self.assertEqual(b["meta"]["boundary_dropped_commits"], [])
        [c] = b["commits"]
        self.assertEqual(c["sha"], new)
        self.assertEqual(sorted(c["files"]), CHANGED)
        evs = [e for e in b["code_events"] if e["commit"] == new]
        self.assertEqual(sorted(e["path"] for e in evs), CHANGED)
        self.assertEqual({e["change"] for e in evs}, {"modify"})
        self.assertTrue(all(e["commit"] == new for e in b["symbol_events"]))
        self.assertEqual(self.area_paths(b), CHANGED)

    def test_second_acquire_into_same_dir_also_deepens(self):
        _old, new = self.issue_repro()
        self._acquire()
        b = self._acquire()          # fetch --shallow-since re-shortens; deepen again
        self.assertEqual(b["meta"]["clone_mode"], "fetched")
        self.assertEqual(b["meta"]["boundary_dropped_commits"], [])
        self.assertEqual(sorted(b["commits"][0]["files"]), CHANGED)

    def test_root_commit_keeps_its_full_file_list(self):
        root = self.write_commit("root", "2026-05-20", OLD_FILES)
        b = self._acquire()
        self.assertEqual(b["meta"]["boundary_dropped_commits"], [])
        [c] = b["commits"]
        self.assertEqual(c["sha"], root)
        self.assertEqual(len(c["files"]), len(OLD_FILES))
        self.assertEqual(len([e for e in b["code_events"] if e["commit"] == root]),
                         len(OLD_FILES))

    def test_fallback_clears_files_and_is_consistent(self):
        _old, new = self.issue_repro()
        with mock.patch("sys.stderr") as err:
            b = self._acquire(run_git=_refusing_deepen())
        self.assertEqual(b["meta"]["boundary_dropped_commits"], [new])
        [c] = b["commits"]
        self.assertEqual(c["sha"], new)
        self.assertEqual(c["files"], [])          # no whole-tree phantom list
        self.assertEqual([e for e in b["code_events"] if e["commit"] == new], [])
        self.assertEqual([e for e in b["symbol_events"] if e["commit"] == new], [])
        self.assertEqual(self.area_paths(b), [])  # nothing looks touched
        written = "".join(str(a[0][0]) for a in err.write.call_args_list)
        self.assertIn("shallow clone boundary", written)


def _harmful_deepen(fail_restore=False, calls=None):
    """A run_git that replaces deepen_window_boundary's widening fetch with the
    old relative `fetch --deepen=1` (which cuts the walked branch on a merge
    history); with `fail_restore` the restoring re-fetch is replaced too, so
    the loss cannot be undone. Everything else goes to real git."""
    real = gather.run_git
    seen = []

    def run(args, cwd=None, timeout=None):
        if "fetch" in args and any(str(a).startswith(("--shallow-since", "--unshallow"))
                                   for a in args):
            seen.append(list(args))
            if calls is not None:
                calls.append(list(args))
            if len(seen) == 1 or fail_restore:
                args = [a for a in args if not str(a).startswith(
                    ("--shallow-since", "--unshallow"))]
                args.insert(args.index("fetch") + 1, "--deepen=1")
        return real(args, cwd=cwd, timeout=timeout)
    return run


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestDeepenNeverLosesHistory(_BoundaryRepo):
    """#97: on a merge history `git fetch --deepen=<n>` (relative to EVERY
    shallow boundary commit) could make a mainline commit above the window a
    boundary and cut all in-window history from the walked branch; and a merge
    with one parent older than the clone bound hides its other, in-window parent
    behind the boundary. Real git over file://, the same server logic as a remote."""

    def setUp(self):
        super().setUp()
        quiet(self)

    def merge(self, msg, date, rev):
        tch._git("merge", "-q", "--no-ff", "-m", msg, rev, cwd=self.src, date=date)
        return tch._git("rev-parse", "HEAD", cwd=self.src).strip()

    def checkout(self, *args):
        tch._git("checkout", "-q", *args, cwd=self.src)

    def cut_repro(self):
        """main: R (old) - W - W2 (in-window) - X - X2 - Z(merge of `sync`).
        `sync` is an old branch (Q, from R) that merged X: its merge commit B has
        an old parent, so B is a clone boundary one step above X. W is an
        in-window boundary (R predates the margin), which makes gather deepen."""
        root = self.write_commit("root", "2026-01-10", OLD_FILES)
        w = self.write_commit("w", "2026-05-20", CHANGED)
        w2 = self.write_commit("w2", "2026-05-25", ["modules/c/main.tf"])
        x = self.write_commit("x", "2026-06-10", ["modules/d/main.tf"])
        self.checkout("-b", "sync", root)
        self.write_commit("q", "2026-01-15", ["modules/e/main.tf"])
        self.merge("sync main", "2026-06-15", x)
        self.checkout("main")
        self.write_commit("x2", "2026-06-18", ["modules/a/main.tf"])
        self.merge("merge sync", "2026-06-20", "sync")
        tch._git("branch", "-q", "-D", "sync", cwd=self.src)
        return w, w2

    def hidden_repro(self):
        """main: R (old) - M1 (margin) - W (in-window) - Z(merge of `side`).
        `side` (from M1) holds H (in-window) and then merges an old commit Q, so
        its merge commit B2 (after the window) is a clone boundary with ALL its
        parents grafted away: H is not reachable at all after the clone."""
        root = self.write_commit("root", "2026-01-10", OLD_FILES)
        m1 = self.write_commit("m1", "2026-05-10", ["modules/c/main.tf"])
        w = self.write_commit("w", "2026-05-20", CHANGED)
        self.checkout("-b", "old", root)
        self.write_commit("q", "2026-01-15", ["modules/e/main.tf"])
        self.checkout("-b", "side", m1)
        h = self.write_commit("h", "2026-05-22", ["modules/d/main.tf"])
        self.merge("merge old", "2026-06-02", "old")
        self.checkout("main")
        self.merge("merge side", "2026-06-05", "side")
        tch._git("branch", "-q", "-D", "side", "old", cwd=self.src)
        return w, h

    def window(self):
        raw = gather.run_git(["git", "-C", self.clone, "log",
                              f"--pretty=format:{gather.LOG_FORMAT}"])
        return {c["sha"] for c in gather.window_records(
            gather.parse_git_log(raw), FROM, TO)}

    def test_relative_deepen_cuts_the_walked_branch(self):
        # Pins the git behaviour the fix avoids: `--deepen=1` counts from every
        # boundary commit, so B's parent X becomes a boundary and W, W2 vanish.
        w, w2 = self.cut_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        self.assertEqual(self.window(), {w, w2})
        gather.run_git(["git", "-C", self.clone, "fetch", "--deepen=1", "origin"])
        self.assertEqual(self.window(), set())

    def test_deepening_keeps_every_in_window_commit(self):
        w, w2 = self.cut_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        self.assertEqual(gather.deepen_window_boundary(self.clone, FROM, TO), [])
        self.assertEqual(self.window(), {w, w2})
        self.assertNotIn(w, gather.shallow_boundary_shas(self.clone))

    def test_acquire_stores_the_in_window_commits_of_a_merge_history(self):
        w, w2 = self.cut_repro()
        b = self._acquire()
        self.assertEqual({c["sha"] for c in b["commits"]}, {w, w2})
        self.assertEqual(b["meta"]["boundary_dropped_commits"], [])
        [cw] = [c for c in b["commits"] if c["sha"] == w]
        self.assertEqual(sorted(cw["files"]), CHANGED)

    def test_commit_hidden_behind_a_late_boundary_is_fetched(self):
        w, h = self.hidden_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        self.assertEqual(self.window(), {w})          # H hidden by the clone bound
        gather.deepen_window_boundary(self.clone, FROM, TO)
        self.assertEqual(self.window(), {w, h})

    def test_acquire_stores_a_commit_hidden_behind_a_late_boundary(self):
        w, h = self.hidden_repro()
        b = self._acquire()
        self.assertEqual({c["sha"] for c in b["commits"]}, {w, h})

    def test_guard_restores_a_deepen_step_that_lost_commits(self):
        w, w2 = self.cut_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        calls = []
        with mock.patch("sys.stderr") as err:
            left = gather.deepen_window_boundary(self.clone, FROM, TO,
                                                 run=_harmful_deepen(calls=calls))
        self.assertEqual(self.window(), {w, w2})      # restored
        self.assertEqual(left, [w])                   # the caller's #68 fallback
        self.assertEqual(calls[1][4], gather._shallow_selector(FROM))
        self.assertEqual(len(calls), 2)               # no further deepening
        written = "".join(str(a[0][0]) for a in err.write.call_args_list)
        self.assertIn("restored", written)

    def test_guard_fails_loudly_when_the_loss_cannot_be_restored(self):
        self.cut_repro()
        gather.prepare_clone(self.url, FROM, self.clone)
        with self.assertRaisesRegex(RuntimeError, "in-window commit"):
            gather.deepen_window_boundary(self.clone, FROM, TO,
                                          run=_harmful_deepen(fail_restore=True))


if __name__ == "__main__":
    unittest.main()
