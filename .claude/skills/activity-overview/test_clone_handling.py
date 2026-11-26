"""Clone handling (#53): quiet repos, re-using an existing clone dir, --no-clone.

Every test drives REAL git against a throwaway local `file://` repository in a
temp dir — no network. `protocol.file.allow=always` is injected through
GIT_CONFIG_COUNT so the production code's own git subprocesses may use the
file:// transport (a shallow clone needs a URL, not a plain path).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
from _quiet import quiet  # noqa: E402

GIT_TEST_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def _git(*args, cwd=None, date=None):
    env = dict(os.environ)
    env.update(GIT_TEST_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{date}T12:00:00Z"
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "commit.gpgsign=false",
         "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


class _RepoCase(unittest.TestCase):
    """A source repo with commits on the given dates, served at a file:// URL."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-clone-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "src")
        _git("init", "-q", "-b", "main", self.src)
        self.url = "file://" + self.src
        self.clone = os.path.join(self.tmp, "ws", "r-clone")
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)

    def commit(self, msg, date):
        with open(os.path.join(self.src, "f.txt"), "w") as fh:
            fh.write(msg + "\n")
        _git("add", "-A", cwd=self.src)
        _git("commit", "-q", "-m", msg, cwd=self.src, date=date)
        return _git("rev-parse", "HEAD", cwd=self.src).strip()

    def head(self, d):
        return _git("-C", d, "rev-parse", "HEAD").strip()

    def log(self, d):
        return _git("-C", d, "log", "--format=%H").split()


class TestPrepareCloneFresh(_RepoCase):
    def test_active_repo_is_a_fresh_shallow_clone(self):
        self.commit("old", "2026-03-01")
        new = self.commit("new", "2026-05-20")
        mode = gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertEqual(mode, "fresh")
        self.assertEqual(self.head(self.clone), new)
        self.assertEqual(self.log(self.clone), [new])   # bounded by --shallow-since

    def test_quiet_repo_falls_back_to_depth1(self):
        # Last commit 2026-03-10; --shallow-since=2026-05-04 selects nothing, which
        # made `git clone` exit 128 and abort the run before #53.
        self.commit("a", "2026-03-01")
        last = self.commit("b", "2026-03-10")
        mode = gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertEqual(mode, "depth1-fallback")
        self.assertEqual(self.head(self.clone), last)   # HEAD available for IaC/CODEOWNERS
        self.assertEqual(self.log(self.clone), [last])  # depth 1

    def test_other_clone_failures_still_raise(self):
        self.commit("a", "2026-05-20")
        with self.assertRaises(RuntimeError):
            gather.prepare_clone("file://" + os.path.join(self.tmp, "nope"),
                                 "2026-05-18", self.clone)
        self.assertFalse(os.path.exists(self.clone))

    def test_build_clone_cmd_depth1(self):
        cmd = gather.build_clone_cmd("https://github.com/o/r.git", "2026-05-01",
                                     "/tmp/c", depth1=True)
        self.assertIn("--depth=1", cmd)
        self.assertFalse(any(a.startswith("--shallow-since") for a in cmd))
        self.assertIn("--filter=blob:none", cmd)
        self.assertEqual(cmd[-2:], ["https://github.com/o/r.git", "/tmp/c"])


class TestPrepareCloneExistingDir(_RepoCase):
    def test_second_run_into_same_dir_fetches_new_commit(self):
        self.commit("a", "2026-05-02")
        mode1 = gather.prepare_clone(self.url, "2026-05-01", self.clone)
        self.assertEqual(mode1, "fresh")
        new = self.commit("b", "2026-05-25")
        # Before #53: `fatal: destination path ... already exists`.
        mode2 = gather.prepare_clone(self.url, "2026-05-01", self.clone)
        self.assertEqual(mode2, "fetched")
        self.assertEqual(self.head(self.clone), new)
        self.assertIn(new, self.log(self.clone))

    def test_existing_clone_deepens_for_an_earlier_window(self):
        a = self.commit("a", "2026-03-02")
        b = self.commit("b", "2026-05-20")
        gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertEqual(self.log(self.clone), [b])
        mode = gather.prepare_clone(self.url, "2026-03-10", self.clone)
        self.assertEqual(mode, "fetched")
        self.assertEqual(self.log(self.clone), [b, a])

    def test_existing_clone_of_quiet_repo_falls_back_to_depth1(self):
        self.commit("a", "2026-03-01")
        gather.prepare_clone(self.url, "2026-03-01", self.clone)
        last = self.commit("b", "2026-03-10")
        mode = gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertEqual(mode, "depth1-fallback")
        self.assertEqual(self.head(self.clone), last)

    def test_existing_dir_of_another_remote_is_refused_untouched(self):
        self.commit("a", "2026-05-20")
        other = os.path.join(self.tmp, "other")
        _git("init", "-q", "-b", "main", other)
        _git("commit", "-q", "--allow-empty", "-m", "x", cwd=other, date="2026-05-20")
        gather.prepare_clone("file://" + other, "2026-05-18", self.clone)
        before = self.head(self.clone)
        with self.assertRaises(RuntimeError) as cm:
            gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertIn("different remote", str(cm.exception))
        self.assertEqual(self.head(self.clone), before)

    def test_existing_non_git_dir_is_refused(self):
        os.makedirs(self.clone)
        with open(os.path.join(self.clone, "keep.txt"), "w") as fh:
            fh.write("mine\n")
        with self.assertRaises(RuntimeError) as cm:
            gather.prepare_clone(self.url, "2026-05-18", self.clone)
        self.assertIn("not a git clone", str(cm.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.clone, "keep.txt")))

    def test_existing_empty_dir_gets_a_fresh_clone(self):
        new = self.commit("a", "2026-05-20")
        os.makedirs(self.clone)
        self.assertEqual(gather.prepare_clone(self.url, "2026-05-18", self.clone), "fresh")
        self.assertEqual(self.head(self.clone), new)


class TestPrepareCloneNoClone(_RepoCase):
    def test_no_clone_missing_dir_is_none_and_creates_nothing(self):
        self.commit("a", "2026-05-20")
        mode = gather.prepare_clone(self.url, "2026-05-18", self.clone, no_clone=True)
        self.assertEqual(mode, "none")
        self.assertFalse(os.path.exists(self.clone))

    def test_no_clone_existing_dir_is_fetched(self):
        self.commit("a", "2026-05-20")
        gather.prepare_clone(self.url, "2026-05-18", self.clone)
        new = self.commit("b", "2026-05-22")
        mode = gather.prepare_clone(self.url, "2026-05-18", self.clone, no_clone=True)
        self.assertEqual(mode, "fetched")
        self.assertEqual(self.head(self.clone), new)


class TestRemoteNormalisation(unittest.TestCase):
    def test_equivalent_github_urls_match(self):
        n = gather._normalize_remote
        want = n("https://github.com/Owner/Repo.git")
        for url in ("https://github.com/owner/repo", "https://github.com/owner/repo/",
                    "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git"):
            self.assertEqual(n(url), want, url)
        self.assertNotEqual(n("https://github.com/owner/other.git"), want)


class TestAcquireCloneStep(_RepoCase):
    """acquire() end to end with the GitHub API stubbed to empty and the remote
    URL pointed at the local file:// repo."""

    def setUp(self):
        super().setUp()
        quiet(self)

    def _acquire(self, *extra):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", "2026-05-18", "--to", "2026-05-31",
            "--clone-dir", self.clone, "--store", os.path.join(self.tmp, "s.db"),
            "--no-workflows", "--no-releases", "--no-project-board", *extra])
        real_provider = gather.select_code_area_provider
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated",
                                  lambda token: (lambda url: ([], None))), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d: real_provider(
                                      paths, d, which=lambda _n: None)), \
                mock.patch.object(gather, "extract_iac_edges", lambda g, d: g):
            return gather.acquire(args, {"GITHUB_TOKEN": "dummy"})

    def test_quiet_repo_does_not_abort_and_yields_no_commits(self):
        self.commit("a", "2026-03-01")
        last = self.commit("b", "2026-03-10")
        b = self._acquire()
        self.assertEqual(b["meta"]["clone_mode"], "depth1-fallback")
        self.assertEqual(b["commits"], [])
        self.assertEqual(b["code_events"], [])
        self.assertEqual(b["symbol_events"], [])
        self.assertEqual(b["meta"]["clone_sha"], last)

    def test_second_acquire_into_same_dir_sees_new_commit(self):
        self.commit("a", "2026-05-19")
        b1 = self._acquire()
        self.assertEqual(b1["meta"]["clone_mode"], "fresh")
        new = self.commit("b", "2026-05-25")
        b2 = self._acquire()
        self.assertEqual(b2["meta"]["clone_mode"], "fetched")
        self.assertIn(new, [c["sha"] for c in b2["commits"]])
        self.assertEqual(b2["meta"]["clone_sha"], new)

    def test_no_clone_missing_dir_degrades_to_empty_walks(self):
        self.commit("a", "2026-05-20")
        b = self._acquire("--no-clone")
        self.assertEqual(b["meta"]["clone_mode"], "none")
        self.assertEqual(b["commits"], [])
        self.assertEqual(b["code_events"], [])
        self.assertEqual(b["symbol_events"], [])
        self.assertIsNone(b["meta"]["clone_sha"])
        self.assertFalse(os.path.exists(self.clone))

    def test_ref_date_survives_gather_fold_view(self):
        # Issue #75: `gather --ref-date X` -> fold -> view meta.ref_date == X;
        # without the flag the view's ref_date is the window's `to`.
        import extract
        import graphstore
        self.commit("a", "2026-05-20")
        for extra, want in ((("--ref-date", "2026-05-25"), "2026-05-25"),
                            ((), "2026-05-31")):
            b = self._acquire(*extra)
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, b)
            meta = extract.extract(conn, "o", "r", "2026-05-18", "2026-05-31",
                                   warn=lambda _m: None)["meta"]
            conn.close()
            self.assertEqual(meta["ref_date"], want)
            self.assertEqual(meta["period"],
                             {"from": "2026-05-18", "to": "2026-05-31"})


if __name__ == "__main__":
    unittest.main()
