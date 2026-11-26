"""Shallow-boundary commits (#68).

When nothing in the clone margin precedes `--from` (typical for per-module
AVM-Terraform repos), the first in-window commit lands on the shallow boundary:
its parent is grafted away and git reports the WHOLE tree as changed. Before
#68 only code/symbol events were dropped; `commits[].files` kept the whole-tree
list, so every area/module looked touched.

The fix deepens the clone past such a commit (bounded `git fetch --deepen`) so
its parent is present and its diff is real; only if that fails does it fall back
to dropping the commit's diffs AND clearing its `files`, recorded in
`meta.boundary_dropped_commits`.

Real git against throwaway local `file://` repos (no network); the harness is
shared with test_clone_handling.py.
"""
import os
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


def _refusing_deepen(calls=None):
    """A run_git that fails every `fetch --deepen` (e.g. a remote that cannot
    deepen) and passes everything else to real git."""
    real = gather.run_git

    def run(args, cwd=None, timeout=None):
        if any(str(a).startswith("--deepen") for a in args):
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
            if any(str(a).startswith("--deepen") for a in args):
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


if __name__ == "__main__":
    unittest.main()
