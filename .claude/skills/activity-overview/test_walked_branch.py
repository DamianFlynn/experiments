"""The walked branch is the mainline (issue #107, item 1).

`meta.base_branch` comes from `--branches` / a manifest member's `branch` /
the clone's default branch / the API (resolve_branches, #84). Before #107 the
clone still checked out the remote DEFAULT branch, so with a non-default
mainline the commits, code events, symbol events, code graph and CODEOWNERS
described a branch the report did not claim to describe. Now acquire()
fetches `origin/<base_branch>` under the same shallow bound and checks it out
(fresh and reused clone alike), and a base branch the remote does not have is
a clear error naming it.

Every test drives REAL git against a throwaway local `file://` repository in
a temp dir (no network, no global/system git config); the GitHub API is
stubbed through acquire's `_paginated` seam.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

GIT_TEST_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def git(*args, cwd=None, date=None):
    env = dict(os.environ)
    env.update(GIT_TEST_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{date}T12:00:00Z"
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "commit.gpgsign=false",
         "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def no_tool(_name):
    return None


class GitRepoCase(unittest.TestCase):
    """A source repo (default branch `main`) served at a file:// URL, and an
    acquire() driver with the GitHub API stubbed and no IaC toolchain."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-i107-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", self.src)
        self.url = "file://" + self.src
        self.clone = os.path.join(self.tmp, "ws", "r-clone")
        self.urls = []

    def commit(self, files, date, msg=None, delete=()):
        """Write `files` ({path: text}), delete `delete`, commit at `date`."""
        for rel, text in files.items():
            path = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        for rel in delete:
            os.remove(os.path.join(self.src, rel))
        git("add", "-A", cwd=self.src)
        git("commit", "-q", "--allow-empty", "-m", msg or ("c " + date),
            cwd=self.src, date=date)
        return self.rev("HEAD")

    def rev(self, ref, repo=None):
        return git("-C", repo or self.src, "rev-parse", ref).strip()

    def checked_out(self, repo=None):
        """(branch name or None when detached, HEAD sha) of a repo."""
        repo = repo or self.clone
        try:
            name = git("-C", repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
        except subprocess.CalledProcessError:
            name = None
        return name, self.rev("HEAD", repo)

    def get_page(self, url):
        self.urls.append(url)
        return [], None

    def acquire(self, frm, to, *extra, clone=None):
        """acquire() against the local repo -> (bundle, stderr text)."""
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", frm, "--to", to,
            "--clone-dir", clone or self.clone,
            "--store", os.path.join(self.tmp, "s.db"), "--no-workflows",
            "--no-releases", "--no-project-board", *extra])
        real_provider = gather.select_code_area_provider
        real_iac = gather.extract_iac_edges
        err = io.StringIO()
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated", lambda token: self.get_page), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d, **kw: real_provider(
                                      paths, d, which=no_tool, **kw)), \
                mock.patch.object(gather, "extract_iac_edges",
                                  lambda g, d, **kw: real_iac(
                                      g, d, which=no_tool, **kw)), \
                contextlib.redirect_stderr(err):
            bundle = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        return bundle, err.getvalue()

    def assertValidates(self, *bundles):
        """Fold `bundles` into one fresh store; the trust check has no
        failing check."""
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for b in bundles:
            gather.fold_bundle(conn, b)
        report = validate.validate(conn, project="o", repo="r")
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        return report


def walked_shas(bundle):
    return sorted(c["sha"] for c in bundle["commits"])


def owners(bundle):
    """The bundle's CODEOWNERS rules as {pattern: owners}."""
    return {r["pattern"]: r["owners"] for r in bundle["code_owners"]}


class _TwoBranchRepo(GitRepoCase):
    """`main` (the remote default) and `develop` (the named mainline) diverge
    after a shared root: each has its own in-window commit, CODEOWNERS and a
    file only it has."""

    FROM, TO = "2026-05-01", "2026-05-31"

    def setUp(self):
        super().setUp()
        self.root = self.commit({"README.md": "r\n",
                                 ".github/CODEOWNERS": "* @main-team\n"},
                                "2026-04-25")
        git("branch", "develop", cwd=self.src)
        self.main_c = self.commit({"main_only.txt": "m\n"}, "2026-05-10", "on main")
        git("checkout", "-q", "develop", cwd=self.src)
        self.dev_c = self.commit({"dev_only.txt": "d\n",
                                  ".github/CODEOWNERS": "* @dev-team\n"},
                                 "2026-05-12", "on develop")
        git("checkout", "-q", "main", cwd=self.src)


class TestNonDefaultMainline(_TwoBranchRepo):
    def test_fresh_clone_walks_the_named_mainline(self):
        b, err = self.acquire(self.FROM, self.TO, "--branches", "develop")
        self.assertEqual(b["meta"]["base_branch"], "develop")
        self.assertEqual(walked_shas(b), [self.dev_c])
        self.assertEqual({e["path"] for e in b["code_events"]},
                         {"dev_only.txt", ".github/CODEOWNERS"})
        self.assertEqual(owners(b), {"*": ["dev-team"]})
        self.assertEqual(sorted(p for a in b["code_graph"]["areas"]
                                for p in a["paths"]),
                         [".github/CODEOWNERS", "dev_only.txt"])
        self.assertEqual(b["meta"]["clone_sha"], self.dev_c)
        self.assertEqual(b["meta"]["clone_mode"], "fresh")
        # the clone is left ON the walked branch, at its head
        self.assertEqual(self.checked_out(), ("develop", self.dev_c))
        # one note says the walked branch is not the remote default
        notes = [ln for ln in err.splitlines() if "remote default" in ln]
        self.assertEqual(len(notes), 1, err)
        self.assertIn("'develop'", notes[0])
        self.assertIn("'main'", notes[0])

    def test_commit_authors_listing_starts_at_the_walked_head(self):
        self.acquire(self.FROM, self.TO, "--branches", "develop")
        listings = [u for u in self.urls if "/commits?" in u]
        self.assertEqual(len(listings), 1, self.urls)
        self.assertIn("sha=" + self.dev_c, listings[0])

    def test_reused_clone_switches_to_the_mainline_and_back(self):
        b1, _ = self.acquire(self.FROM, self.TO)
        self.assertEqual(walked_shas(b1), [self.main_c])
        b2, _ = self.acquire(self.FROM, self.TO, "--branches", "develop,main")
        self.assertEqual(b2["meta"]["clone_mode"], "fetched")
        self.assertEqual(b2["meta"]["base_branch"], "develop")
        self.assertEqual(b2["meta"]["branches"], ["develop", "main"])
        self.assertEqual(walked_shas(b2), [self.dev_c])
        self.assertEqual(owners(b2), {"*": ["dev-team"]})
        self.assertEqual(self.checked_out(), ("develop", self.dev_c))
        # a later default-branch run on the same clone walks main again
        b3, err3 = self.acquire(self.FROM, self.TO)
        self.assertEqual(walked_shas(b3), [self.main_c])
        self.assertEqual(owners(b3), {"*": ["main-team"]})
        self.assertEqual(self.checked_out(), ("main", self.main_c))
        self.assertNotIn("remote default", err3)

    def test_reused_clone_sees_new_mainline_commits(self):
        self.acquire(self.FROM, self.TO, "--branches", "develop")
        git("checkout", "-q", "develop", cwd=self.src)
        dev2 = self.commit({"dev_only.txt": "d2\n"}, "2026-05-20")
        git("checkout", "-q", "main", cwd=self.src)
        b, _ = self.acquire(self.FROM, self.TO, "--branches", "develop")
        self.assertEqual(walked_shas(b), sorted([self.dev_c, dev2]))
        self.assertEqual(self.checked_out(), ("develop", dev2))

    def test_naming_the_default_branch_changes_nothing(self):
        plain, err_plain = self.acquire(self.FROM, self.TO,
                                        clone=os.path.join(self.tmp, "a"))
        named, err_named = self.acquire(self.FROM, self.TO, "--branches", "main",
                                        clone=os.path.join(self.tmp, "b"))
        for b in (plain, named):
            b["meta"].pop("clone_dir")
        self.assertEqual(named, plain)
        self.assertNotIn("remote default", err_plain + err_named)

    def test_trust_check_green_on_the_mainline_store(self):
        b, _ = self.acquire(self.FROM, self.TO, "--branches", "develop")
        self.assertValidates(b)


class TestMissingMainline(_TwoBranchRepo):
    def test_single_repo_stops_with_the_branch_named(self):
        with self.assertRaises(SystemExit) as cm:
            self.acquire(self.FROM, self.TO, "--branches", "nope")
        self.assertIsInstance(cm.exception.code, str)   # exit status 1 + message
        self.assertIn("'nope'", cm.exception.code)
        self.assertIn("o/r", cm.exception.code)

    def test_main_exits_non_zero(self):
        argv = ["--owner", "o", "--repo", "r", "--from", self.FROM, "--to", self.TO,
                "--clone-dir", self.clone, "--store", os.path.join(self.tmp, "s.db"),
                "--no-workflows", "--no-releases", "--no-project-board",
                "--branches", "nope"]
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated", lambda token: self.get_page), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "dummy"}), \
                self.assertRaises(SystemExit) as cm:
            gather.main(argv)
        self.assertNotIn(cm.exception.code, (0, None))
        self.assertIn("'nope'", str(cm.exception.code))

    def test_manifest_member_is_recorded_failed(self):
        man_path = os.path.join(self.tmp, "m.json")
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump({"project": "p",
                       "window": {"from": self.FROM, "to": self.TO},
                       "repos": [{"owner": "o", "repo": "r", "branch": "nope"},
                                 {"owner": "o", "repo": "s", "branch": "develop"}]},
                      fh)
        store = os.path.join(self.tmp, "p.db")
        argv = ["--manifest", man_path, "--store", store,
                "--clone-dir", os.path.join(self.tmp, "ws"), "--no-workflows",
                "--no-releases", "--no-project-board"]
        err = io.StringIO()
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated", lambda token: self.get_page), \
                mock.patch.object(gather, "extract_iac_edges",
                                  lambda g, d, **kw: g), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "dummy"}), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            gather.main(argv)
        self.assertEqual(cm.exception.code, 1)
        conn = graphstore.open_store(store)
        try:
            status = {e["repo"]: e for e in graphstore.get_member_status(conn, "p")}
        finally:
            conn.close()
        self.assertEqual(status["o/r"]["status"], graphstore.MEMBER_FAILED)
        self.assertIn("'nope'", status["o/r"]["error"])
        self.assertEqual(status["o/s"]["status"], graphstore.MEMBER_OK)


class TestQuietMainline(GitRepoCase):
    """The named mainline has nothing since the clone bound while the remote
    default is busy: the walked branch falls back to depth 1 and the walks
    are empty, exactly as a quiet repo's default branch does."""

    def test_quiet_named_branch_is_a_depth1_fallback(self):
        self.commit({"a.txt": "a\n"}, "2025-01-10")
        git("branch", "release", cwd=self.src)
        rel = self.rev("release")
        self.commit({"a.txt": "b\n"}, "2026-05-10")
        b, err = self.acquire("2026-05-01", "2026-05-31", "--branches", "release")
        self.assertEqual(b["meta"]["clone_mode"], "depth1-fallback")
        self.assertEqual(b["commits"], [])
        self.assertEqual(b["code_events"], [])
        self.assertEqual(b["meta"]["clone_sha"], rel)
        self.assertEqual(self.checked_out(), ("release", rel))
        self.assertIn("depth1-fallback", err)


class TestCheckoutBaseBranchHelper(_TwoBranchRepo):
    def test_missing_branch_raises_the_named_error(self):
        gather.prepare_clone(self.url, self.FROM, self.clone)
        with self.assertRaises(gather.MissingBranchError) as cm:
            gather.checkout_base_branch(self.clone, "nope", self.FROM)
        self.assertIn("'nope'", str(cm.exception))
        # the clone is untouched: still on the remote default
        self.assertEqual(self.checked_out(), ("main", self.main_c))

    def test_fetch_uses_the_window_shallow_bound(self):
        gather.prepare_clone(self.url, self.FROM, self.clone)
        calls = []
        real = gather.run_git

        def run(args, cwd=None, timeout=None):
            calls.append(list(args))
            return real(args, cwd=cwd, timeout=timeout)
        self.assertFalse(gather.checkout_base_branch(self.clone, "develop",
                                                     self.FROM, run=run))
        fetch = [c for c in calls if "fetch" in c]
        self.assertEqual(len(fetch), 1)
        self.assertIn("--shallow-since=2026-04-17", fetch[0])
        self.assertIn("+refs/heads/develop:refs/remotes/origin/develop", fetch[0])
        self.assertEqual(self.checked_out(), ("develop", self.dev_c))


if __name__ == "__main__":
    unittest.main()
