"""Issue #77: per-member failure handling in a multi-repo (manifest) gather.

One failing member used to stop the run (later members never attempted), left
the connection open, recorded nothing about the failure, and left the failed
member in the store as a PHANTOM: its only node a `depends_on` stand-in created
by another member's fold, which `graphstore.project_repos` still listed as a
member with zero activity.

Every test injects `gather.acquire` (no network, no clone).
"""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import digest
import gather
import graphstore
import validate
import view

PROJECT = "p"
FROM, TO = "2026-01-01", "2026-01-31"
A = "o/terraform-azurerm-a"
B = "o/terraform-azurerm-b"
C = "o/terraform-azurerm-c"
HTTP_403 = "GitHub API error 403 for /repos/o/terraform-azurerm-b/pulls"


def _member_bundle(args):
    """A small, valid member bundle. Member a's root module references b's
    registry module, so a's fold creates the cross-repo stand-in for b."""
    n = {"terraform-azurerm-a": 1, "terraform-azurerm-b": 2,
         "terraform-azurerm-c": 3}[args.repo]
    edges = []
    if args.repo == "terraform-azurerm-a":
        edges = [{"to": None, "kind": "module", "ref": "o/b/azurerm",
                  "version": "1.0.0", "transitive": False,
                  "provider": "terraform", "resolved": False}]
    return {
        "meta": {"owner": args.owner, "repo": args.repo,
                 "from": getattr(args, "from"), "to": args.to,
                 "base_branch": "main"},
        "prs": [{"number": n, "url": "https://github.com/{}/{}/pull/{}".format(
                     args.owner, args.repo, n), "state": "closed",
                 "merged": True, "author": "dev{}".format(n),
                 "created_at": "2026-01-05T00:00:00Z",
                 "merged_at": "2026-01-10T00:00:00Z",
                 "closed_at": "2026-01-10T00:00:00Z",
                 "closes": [], "crossref_issues": [], "title": "t", "body": ""}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [],
        "code_graph": {"provider": "directory", "areas": [
            {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
             "edges": edges}]},
    }


class _Run:
    """Run gather.main over a manifest with an injected acquire. `fail` maps a
    member repo name to the exception its acquire raises."""

    def __init__(self, tmp, repos=(A, B, C)):
        self.tmp = tmp
        self.store = os.path.join(tmp, "j.db")
        self.mpath = os.path.join(tmp, "m.json")
        with open(self.mpath, "w", encoding="utf-8") as fh:
            json.dump({"project": PROJECT, "window": {"from": FROM, "to": TO},
                       "repos": [{"owner": r.split("/")[0],
                                  "repo": r.split("/")[1]} for r in repos]}, fh)

    def __call__(self, fail=None, extra=()):
        fail = fail or {}
        calls = []

        def fake_acquire(args, env):
            calls.append("{}/{}".format(args.owner, args.repo))
            exc = fail.get(args.repo)
            if exc is not None:
                raise exc
            return _member_bundle(args)

        orig = gather.acquire
        gather.acquire = fake_acquire
        err = io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stderr(err):
                gather.main(["--manifest", self.mpath, "--store", self.store,
                             *extra])
        except SystemExit as exc:
            code = exc.code
        finally:
            gather.acquire = orig
        return code, calls, err.getvalue()

    def conn(self):
        return graphstore.open_store(self.store)

    def validate_cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = validate.main([self.store, *extra])
        return code, out.getvalue()


class TestFailingMemberDoesNotStopTheRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run = _Run(self._tmp.name)
        self.code, self.calls, self.err = self.run(
            fail={"terraform-azurerm-b": SystemExit(HTTP_403)})
        self.addCleanup(self._tmp.cleanup)

    def test_remaining_members_are_attempted_and_folded(self):
        self.assertEqual(self.calls, [A, B, C])
        conn = self.run.conn()
        self.assertIsNotNone(graphstore.get_node(conn, "p/{}#pr-1".format(A)))
        self.assertIsNotNone(graphstore.get_node(conn, "p/{}#pr-3".format(C)))
        repos = {w["repo"] for w in graphstore.get_windows(conn)}
        self.assertEqual(repos, {A, C})

    def test_exit_code_is_non_zero_with_a_summary(self):
        self.assertEqual(self.code, 1)
        self.assertIn("1 of 3 member(s) failed", self.err)
        self.assertIn("{}: {}".format(B, HTTP_403), self.err)
        self.assertIn("--resume", self.err)

    def test_run_status_is_recorded_per_member_and_window(self):
        status = graphstore.get_member_status(self.run.conn(), PROJECT)
        self.assertEqual(
            [(s["repo"], s["from"], s["to"], s["status"]) for s in status],
            [(A, FROM, TO, "ok"), (B, FROM, TO, "failed"), (C, FROM, TO, "ok")])
        self.assertEqual(status[1]["error"], HTTP_403)
        self.assertNotIn("error", status[0])

    def test_phantom_member_is_not_a_project_repo(self):
        conn = self.run.conn()
        # the stand-in a's depends_on created for b is still there ...
        standin = graphstore.get_node(conn, "p/{}#area-main.tf".format(B))
        self.assertEqual(standin["data"].get("synthesized"), "depends_on_target")
        # ... but b is not reported as a member with zero activity
        self.assertEqual(graphstore.project_repos(conn, PROJECT), [A, C])
        self.assertEqual(
            [(s["repo"], s["status"])
             for s in graphstore.incomplete_members(conn, PROJECT)],
            [(B, "failed")])

    def test_validate_fails_the_project_naming_the_failed_member(self):
        code, out = self.run.validate_cli()
        self.assertEqual(code, 1, out)
        self.assertIn("repo: {}  [OK]".format(A), out)
        self.assertIn("repo: {}  [OK]".format(C), out)
        self.assertNotIn("repo: {}".format(B), out)
        self.assertIn("[FAIL] member_status  (ERROR)", out)
        self.assertIn(HTTP_403, out)
        agg = validate.validate_project(self.run.conn(), PROJECT, [A, C])
        self.assertFalse(agg["ok"])
        self.assertTrue(all(m["ok"] for m in agg["members"]), agg)
        failed = [c["name"] for c in agg["project_checks"] if not c["ok"]]
        self.assertEqual(failed, ["member_status"])

    def test_validate_on_the_failed_member_fails_target(self):
        code, out = self.run.validate_cli("--project", PROJECT, "--repo", B)
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] target", out)
        self.assertIn(HTTP_403, out)

    def test_validate_on_a_healthy_member_is_not_failed_by_the_other(self):
        # Per-member audit as validate --project runs it (people are audited
        # project-wide there; a single --repo audit of one member of a
        # multi-member store has its own people issue, #88).
        rep = validate.validate_repo(self.run.conn(), PROJECT, A,
                                     check_people=False)
        self.assertIs(rep.ok, True, [c for c in rep.checks if not c["ok"]])

    def test_digest_warns_about_the_incomplete_member(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = digest.main(["--store", self.run.store, "--project", PROJECT,
                                "--from", FROM, "--to", TO])
        self.assertEqual(code, 0)
        self.assertIn("warning: project 'p' is incomplete", err.getvalue())
        self.assertIn("{} [{}..{}] failed: {}".format(B, FROM, TO, HTTP_403),
                      err.getvalue())
        members = [m["repo"] for m in json.loads(out.getvalue())["members"]]
        self.assertEqual(members, [A, C])

    def test_digest_does_not_warn_outside_the_failed_window(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            digest.main(["--store", self.run.store, "--project", PROJECT,
                         "--from", "2026-02-01", "--to", "2026-02-28"])
        self.assertNotIn("incomplete", err.getvalue())

    def test_view_warns_about_the_incomplete_member(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = view.main(["--store", self.run.store, "--project", PROJECT,
                              "--repo", A, "--from", FROM, "--to", TO,
                              "--out", os.path.join(self._tmp.name, "v.json")])
        self.assertEqual(code, 0)
        self.assertIn("warning: project 'p' is incomplete", err.getvalue())
        self.assertIn(B, err.getvalue())

    def test_resume_completes_only_the_failed_member(self):
        code, calls, err = self.run(extra=["--resume"])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [B])
        conn = self.run.conn()
        self.assertEqual(graphstore.project_repos(conn, PROJECT), [A, B, C])
        self.assertEqual(graphstore.incomplete_members(conn, PROJECT), [])
        code, out = self.run.validate_cli()
        self.assertEqual(code, 0, out)
        self.assertIn("[ok  ] member_status  (ERROR)", out)

    def test_plain_rerun_reattempts_every_member(self):
        code, calls, _ = self.run()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [A, B, C])
        vcode, out = self.run.validate_cli()
        self.assertEqual(vcode, 0, out)


class TestTwoMemberProject(unittest.TestCase):
    """With one of two members failed only one repo holds data; validate must
    still take the project path, not audit the lone healthy repo and pass."""

    def test_validate_without_repo_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, repos=(A, B))
            code, _, _ = run(fail={"terraform-azurerm-b": SystemExit(HTTP_403)})
            self.assertEqual(code, 1)
            self.assertEqual(graphstore.project_repos(run.conn(), PROJECT), [A])
            vcode, out = run.validate_cli()
        self.assertEqual(vcode, 1, out)
        self.assertIn("trust audit (project): FAIL", out)
        self.assertIn("[FAIL] member_status", out)


class TestFailureKinds(unittest.TestCase):
    def test_exception_in_acquire_and_fold_failure_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp)
            orig = gather.fold_bundle

            def fold(conn, bundle, **kw):
                if kw.get("repo") == C:
                    raise ValueError("bad bundle")
                return orig(conn, bundle, **kw)

            gather.fold_bundle = fold
            try:
                code, calls, err = run(fail={
                    "terraform-azurerm-a": RuntimeError("clone failed\nline 2")})
            finally:
                gather.fold_bundle = orig
            status = {s["repo"]: s for s in
                      graphstore.get_member_status(run.conn(), PROJECT)}
            nodes_c = run.conn().execute(
                "SELECT COUNT(*) FROM nodes WHERE repo=?", (C,)).fetchone()[0]
        self.assertEqual(code, 1)
        self.assertEqual(calls, [A, B, C])
        self.assertEqual(status[A]["status"], "failed")
        self.assertEqual(status[A]["error"], "RuntimeError: clone failed line 2")
        self.assertEqual(status[B]["status"], "ok")
        self.assertEqual(status[C]["error"], "ValueError: bad bundle")
        self.assertEqual(nodes_c, 0)
        self.assertIn("2 of 3 member(s) failed", err)

    def test_interrupt_leaves_later_members_not_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp)
            with self.assertRaises(KeyboardInterrupt):
                run(fail={"terraform-azurerm-b": KeyboardInterrupt()})
            conn = run.conn()
            status = [(s["repo"], s["status"])
                      for s in graphstore.get_member_status(conn, PROJECT)]
            vcode, out = run.validate_cli()
        self.assertEqual(status, [(A, "ok"), (B, "not_attempted"),
                                  (C, "not_attempted")])
        self.assertEqual(vcode, 1, out)
        self.assertIn("not_attempted", out)

    def test_connection_is_closed_on_failure(self):
        opened = []
        orig_open = graphstore.open_store

        def spy(path=":memory:"):
            conn = orig_open(path)
            opened.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp)
            graphstore.open_store = spy
            try:
                with self.assertRaises(KeyboardInterrupt):
                    run(fail={"terraform-azurerm-a": KeyboardInterrupt()})
            finally:
                graphstore.open_store = orig_open
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_every_member_failed_is_reported_by_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, repos=(A, B))
            code, _, _ = run(fail={"terraform-azurerm-a": SystemExit("boom a"),
                                   "terraform-azurerm-b": SystemExit(HTTP_403)})
            vcode, out = run.validate_cli("--project", PROJECT)
        self.assertEqual(code, 1)
        self.assertEqual(vcode, 1, out)
        self.assertIn("[FAIL] member_status", out)
        self.assertIn("boom a", out)
        self.assertIn(HTTP_403, out)

    def test_all_members_ok_returns_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp)
            code, calls, err = run()
            self.assertEqual(code, 0, err)
            self.assertIn("folded 3 of 3 member repo(s)", err)
            vcode, out = run.validate_cli()
        self.assertEqual(vcode, 0, out)

    def test_resume_requires_manifest(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                gather.parse_args(["--owner", "o", "--repo", "r", "--from", FROM,
                                   "--to", TO, "--store", "s.db", "--resume"])
        self.assertEqual(cm.exception.code, 2)

    def test_resume_with_everything_ok_attempts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp)
            run()
            code, calls, err = run(extra=["--resume"])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        self.assertIn("nothing to resume", err)


class TestProjectReposExcludesStandIns(unittest.TestCase):
    def _store(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        return conn

    def _standin(self, conn, repo):
        graphstore.upsert_node(
            conn, "p/{}#area-main.tf".format(repo), "p", repo, "structure", None,
            {"id": "main.tf", "synthesized": "depends_on_target"})

    def test_standin_only_repo_is_not_a_member(self):
        conn = self._store()
        self._standin(conn, B)
        graphstore.upsert_node(conn, "p/{}#pr-1".format(A), "p", A, "social",
                               "2026-01-10T00:00:00Z", {"number": 1})
        self.assertEqual(graphstore.project_repos(conn, "p"), [A])

    def test_gathered_quiet_member_with_only_a_standin_stays_a_member(self):
        conn = self._store()
        self._standin(conn, B)
        graphstore.record_window(conn, "p", B, FROM, TO)
        self.assertEqual(graphstore.project_repos(conn, "p"), [B])

    def test_standin_beside_real_nodes_keeps_the_repo(self):
        conn = self._store()
        self._standin(conn, B)
        graphstore.upsert_node(conn, "p/{}#codegraph".format(B), "p", B,
                               "structure", None, {"areas": []})
        self.assertEqual(graphstore.project_repos(conn, "p"), [B])


if __name__ == "__main__":
    unittest.main()
