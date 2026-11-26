"""Issue #109 (collection review), items 2, 3, 4, 5, 9, 10, 12 and 14.

  2  git walks independent of the user's global/system git config.
  3  Bicep `module` refs only from code (never from comments or strings).
  4  a reused --clone-dir survives an earlier run's in-place IaC build output.
  5  an undecodable / dangling tracked .tf / .bicep / CODEOWNERS never fails the
     member, and an unread area is not authoritatively scanned (#95 replay).
  9  no repo file is read through a symlink that leaves the checkout.
 10  terraform never uses a repo-shipped plugin mirror; --no-iac-build.
 12  manifest clone dirs cannot collide; the former layout is still reused.
 14  child processes never see the GitHub token.

Real git runs against throwaway local repos (no network); git, terraform and
bicep are otherwise faked through the injected seams or a mocked
subprocess.run.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
from test_clone_handling import GIT_TEST_ENV, _git  # noqa: E402

HAVE_GIT = bool(shutil.which("git"))

POLLUTED_CONFIG = """\
[color]
    ui = always
    diff = always
[diff]
    noprefix = true
    mnemonicPrefix = true
    renames = false
[log]
    showSignature = true
[core]
    pager = less
[clone]
    defaultRemoteName = upstream
"""


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return path


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-i109-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        # Each test reads the user's git config afresh.
        cache = mock.patch.object(gather, "_git_passthrough_cache", None)
        cache.start()
        self.addCleanup(cache.stop)


# --- item 2: git config isolation --------------------------------------------

@unittest.skipUnless(HAVE_GIT, "git not installed")
class TestWalksIgnoreUserGitConfig(_TmpCase):
    """A repo with files under top-level `a/` and `b/` (what `diff.noprefix`
    would mangle), a rename (what `diff.renames=false` would split) and code
    lines (what colour escapes would hide from the symbol parser)."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        _git("init", "-q", "-b", "main", self.repo)
        _write(self.repo, "a/main.bicep", "param one string\n")
        _write(self.repo, "b/old.txt", "".join("line %d\n" % i for i in range(20)))
        _write(self.repo, "main.tf", 'variable "x" {}\n')
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "first", cwd=self.repo, date="2026-05-02")
        _write(self.repo, "a/main.bicep", "param one string\nparam two int\n")
        _git("mv", "b/old.txt", "b/new.txt", cwd=self.repo)
        _write(self.repo, "main.tf", 'variable "x" {}\noutput "y" { value = 1 }\n')
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-q", "-m", "second", cwd=self.repo, date="2026-05-03")
        self.polluted = _write(self.tmp, "gitconfig", POLLUTED_CONFIG)

    def _walks(self, global_config):
        out = {}
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": global_config}), \
                mock.patch.object(gather, "_git_passthrough_cache", None):
            for name in ("COMMIT_WALK_ARGS", "CODE_WALK_ARGS", "PATCH_WALK_ARGS"):
                out[name] = gather.run_git(
                    ["git", "-C", self.repo, "log", *getattr(gather, name)])
        return out

    def test_walk_output_is_byte_identical_under_a_polluted_global_config(self):
        clean = self._walks(os.devnull)
        self.assertEqual(self._walks(self.polluted), clean)
        # ...and it is the right output, not merely the same.
        events, diffs = gather.parse_patch_events(clean["PATCH_WALK_ARGS"])
        self.assertIn("a/main.bicep", {e["path"] for e in events})
        self.assertNotIn("\x1b", clean["PATCH_WALK_ARGS"])
        commits = gather.parse_git_log(clean["COMMIT_WALK_ARGS"])
        self.assertEqual(commits[0]["files"], ["a/main.bicep", "b/new.txt", "main.tf"])

    def test_walk_flags_alone_pin_the_output(self):
        # Belt and braces: even if the isolation were bypassed, the walk
        # arguments themselves give the clean-config output.
        def raw(cfg):
            env = dict(os.environ, GIT_CONFIG_GLOBAL=cfg, GIT_CONFIG_NOSYSTEM="1")
            return {name: subprocess.run(
                ["git", "-c", "core.quotepath=false", "-C", self.repo, "log",
                 *getattr(gather, name)], env=env, capture_output=True,
                check=True).stdout
                for name in ("COMMIT_WALK_ARGS", "CODE_WALK_ARGS", "PATCH_WALK_ARGS")}
        self.assertEqual(raw(self.polluted), raw(os.devnull))

    def test_reused_clone_works_with_clone_default_remote_name_set(self):
        # clone.defaultRemoteName=upstream used to name the remote `upstream`,
        # so the next run's `remote.origin.url` check refused the clone.
        url = "file://" + self.repo
        clone = os.path.join(self.tmp, "ws", "clone")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": self.polluted}):
            self.assertEqual(gather.prepare_clone(url, "2026-05-01", clone), "fresh")
            self.assertEqual(gather.prepare_clone(url, "2026-05-01", clone), "fetched")


class TestGitChildEnvironment(_TmpCase):
    def test_isolation_variables(self):
        with mock.patch.dict(os.environ, {"LANGUAGE": "de", "LANG": "de_DE.UTF-8",
                                          "GIT_CONFIG_GLOBAL": "/home/u/.gitconfig"}), \
                mock.patch.object(gather, "_git_passthrough_cache", []):
            env = gather._child_env(["git", "log"])
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_PAGER"], "cat")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertNotIn("LANGUAGE", env)
        # the caller's own GIT_CONFIG_COUNT entries are kept
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "protocol.file.allow")

    def test_non_git_tools_are_not_isolated(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8"}), \
                mock.patch.object(gather, "_git_passthrough_cache", []):
            env = gather._child_env(["bicep", "build"])
        self.assertNotIn("GIT_CONFIG_NOSYSTEM", env)
        self.assertEqual(env["LC_ALL"], "de_DE.UTF-8")

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_network_settings_are_carried_over_and_nothing_else(self):
        cfg = _write(self.tmp, "gitconfig", """\
[http]
    proxy = http://proxy.invalid:3128
    sslCAInfo = /etc/corp/ca.pem
    extraHeader = Authorization: bearer s3cret
    sslKey = /home/u/client.key
[http "https://ghe.example/"]
    sslVerify = false
[url "https://mirror.example/"]
    insteadOf = https://github.com/
[credential]
    helper = osxkeychain
[safe]
    directory = /srv/clones/x
[color]
    ui = always
[user]
    name = Bob
""")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": cfg,
                                          "GIT_CONFIG_NOSYSTEM": "1"}):
            pairs = gather._git_passthrough_config()
            env = gather._child_env(["git", "status"])
            seen = gather.run_git(["git", "config", "--get", "http.proxy"]).strip()
            with self.assertRaises(RuntimeError):      # not carried over
                gather.run_git(["git", "config", "--get", "color.ui"])
            with self.assertRaises(RuntimeError):
                gather.run_git(["git", "config", "--get", "http.extraheader"])
        self.assertEqual(pairs, [
            ("http.proxy", "http://proxy.invalid:3128"),
            ("http.sslcainfo", "/etc/corp/ca.pem"),
            ("http.https://ghe.example/.sslverify", "false"),
            ("url.https://mirror.example/.insteadof", "https://github.com/"),
            ("credential.helper", "osxkeychain"),
            ("safe.directory", "/srv/clones/x")])
        self.assertEqual(seen, "http://proxy.invalid:3128")
        # carried over first, the caller's own GIT_CONFIG_COUNT entries after
        self.assertEqual(env["GIT_CONFIG_COUNT"], "7")
        self.assertEqual(env["GIT_CONFIG_KEY_6"], "protocol.file.allow")
        # values travel in the environment, never in argv / error text
        try:
            gather.run_git(["git", "-C", self.tmp, "rev-parse", "HEAD"])
        except RuntimeError as exc:
            self.assertNotIn("proxy.invalid", str(exc))

    def test_passthrough_key_filter(self):
        keep = ["http.proxy", "http.https://x.example/.proxy", "http.sslcainfo",
                "http.sslbackend", "url.https://m/.insteadof", "credential.helper",
                "credential.https://github.com.helper", "safe.directory",
                "protocol.file.allow", "core.sshcommand"]
        drop = ["http.extraheader", "http.https://x/.extraheader", "http.sslcert",
                "http.sslkey", "http.cookiefile", "url.https://m/.pushinsteadof",
                "color.ui", "diff.noprefix", "core.pager", "include.path"]
        for key in keep:
            self.assertTrue(gather._git_passthrough_key(key), key)
        for key in drop:
            self.assertFalse(gather._git_passthrough_key(key), key)


# --- item 14: no token in any child environment --------------------------------

class TestChildProcessesNeverSeeTheToken(_TmpCase):
    ENV = {"GITHUB_TOKEN": "ghp_x", "GH_TOKEN": "ghp_y", "GITHUB_PAT": "ghp_z",
           "ACTIVITY_TEST_TOKEN": "t", "NPM_TOKEN": "n", "GH_ENTERPRISE_TOKEN": "e",
           "TF_TOKEN_app_terraform_io": "tfc", "HTTPS_PROXY": "http://proxy:3128",
           "SSL_CERT_FILE": "/etc/ca.pem", "TF_PLUGIN_CACHE_DIR": "/cache",
           "PATH": "/usr/bin:/bin"}
    SECRETS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "ACTIVITY_TEST_TOKEN",
               "NPM_TOKEN", "GH_ENTERPRISE_TOKEN")

    def _envs(self, commands):
        seen = []

        def fake(args, **kw):
            seen.append((args[0], kw["env"]))
            return subprocess.CompletedProcess(args, 0, "", "")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with mock.patch.dict(os.environ, dict(self.ENV, HOME=home)), \
                mock.patch.object(gather, "_git_passthrough_cache", []), \
                mock.patch.object(gather, "_terraform_cli_config_cache", None), \
                mock.patch.object(gather.subprocess, "run", fake):
            for cmd in commands:
                gather.run_git(cmd)
        return seen

    def test_git_bicep_terraform_graphify(self):
        seen = self._envs([["git", "log"], ["bicep", "build", "x"],
                           ["terraform", "-chdir=x", "init"], ["graphify", "update"]])
        self.assertEqual([t for t, _ in seen], ["git", "bicep", "terraform", "graphify"])
        for tool, env in seen:
            for name in self.SECRETS:
                self.assertNotIn(name, env, (tool, name))
            self.assertEqual(env["PATH"], "/usr/bin:/bin")
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy:3128")
            self.assertEqual(env["SSL_CERT_FILE"], "/etc/ca.pem")
        tf = dict(seen)["terraform"]
        self.assertEqual(tf["TF_PLUGIN_CACHE_DIR"], "/cache")
        self.assertEqual(tf["TF_TOKEN_app_terraform_io"], "tfc")   # its own registry auth
        self.assertIn("TF_CLI_CONFIG_FILE", tf)

    def test_git_keeps_the_token_for_a_configured_credential_helper(self):
        # `gh auth git-credential` reads GH_TOKEN; without it a private clone
        # that works today would fail. Only git, only with a helper configured.
        seen = []

        def fake(args, **kw):
            seen.append((args[0], kw["env"]))
            return subprocess.CompletedProcess(args, 0, "", "")
        helper = [("credential.https://github.com.helper", "!gh auth git-credential")]
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(gather, "_git_passthrough_cache", helper), \
                mock.patch.object(gather, "_terraform_cli_config_cache", "/x.tfrc"), \
                mock.patch.object(gather.subprocess, "run", fake):
            gather.run_git(["git", "fetch"])
            gather.run_git(["terraform", "init"])
        git_env, tf_env = seen[0][1], seen[1][1]
        self.assertEqual(git_env["GH_TOKEN"], "ghp_y")
        self.assertEqual(git_env["GITHUB_TOKEN"], "ghp_x")
        self.assertNotIn("NPM_TOKEN", git_env)
        self.assertNotIn("GITHUB_PAT", git_env)
        for name in self.SECRETS:
            self.assertNotIn(name, tf_env)

    def test_extract_iac_edges_build_goes_through_the_same_environment(self):
        # The build path calls run_git (its default `run`) for terraform.
        root = os.path.join(self.tmp, "clone")
        _write(root, "infra/main.tf", 'module "a" { source = "./a" }\n')
        seen = []

        def fake(args, **kw):
            seen.append((list(args), kw["env"]))
            return subprocess.CompletedProcess(args, 0, "digraph {}", "")
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(gather, "_terraform_cli_config_cache", None), \
                mock.patch.object(gather.subprocess, "run", fake):
            gather.extract_iac_edges(
                {"areas": [{"id": "infra", "paths": ["infra/main.tf"], "edges": []}]},
                root, which=lambda n: "/bin/" + n if n == "terraform" else None,
                max_workers=1)
        self.assertTrue(seen)
        for args, env in seen:
            self.assertEqual(os.path.basename(args[0]), "terraform")
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertIn("TF_CLI_CONFIG_FILE", env)


# --- item 10: terraform plugin mirror, --no-iac-build ---------------------------

class TestTerraformCliConfig(_TmpCase):
    def _config(self, env):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, dict(env, HOME=home)), \
                mock.patch.object(gather, "_terraform_cli_config_cache", None):
            if "TF_CLI_CONFIG_FILE" not in env:
                os.environ.pop("TF_CLI_CONFIG_FILE", None)   # restored on exit
            path = gather._child_env(["terraform", "init"])["TF_CLI_CONFIG_FILE"]
            with open(path, encoding="utf-8") as fh:
                return path, fh.read(), home

    def test_explicit_direct_installation_disables_the_implied_mirror(self):
        path, text, _home = self._config({})
        self.assertRegex(text, r"provider_installation\s*\{")
        self.assertIn("direct {}", text)
        self.assertNotIn("terraform.d", text)       # no mirror at all
        self.assertTrue(path.startswith(tempfile.gettempdir()))

    def test_user_plugin_dir_is_kept_as_an_explicit_mirror(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".terraform.d", "plugins"))
        _path, text, _ = self._config({})
        self.assertIn('path = "%s"' % os.path.join(home, ".terraform.d", "plugins"),
                      text)

    def test_user_config_is_kept(self):
        home = os.path.join(self.tmp, "home")
        _write(home, ".terraformrc", 'plugin_cache_dir = "/c"\n')
        _path, text, _ = self._config({})
        self.assertTrue(text.startswith('plugin_cache_dir = "/c"\n'))
        self.assertIn("direct {}", text)

    def test_user_config_with_its_own_provider_installation_is_used_as_is(self):
        own = _write(self.tmp, "own.tfrc",
                     'provider_installation {\n  network_mirror { url = "https://m/" }\n}\n')
        path, _text, _ = self._config({"TF_CLI_CONFIG_FILE": own})
        self.assertEqual(path, own)

    def test_terraform_commands_unchanged(self):
        calls = []
        gather._terraform_graph_dot(
            lambda cmd, **kw: calls.append(cmd) or "", "/c", "mod", timeout=5)
        self.assertEqual(calls, [
            ["terraform", "-chdir=/c/mod", "init", "-backend=false", "-input=false"],
            ["terraform", "-chdir=/c/mod", "graph"]])


class TestNoIacBuildFlag(unittest.TestCase):
    BASE = ["--owner", "o", "--repo", "r", "--from", "2026-05-01",
            "--to", "2026-05-31", "--store", "s.db"]

    def test_default_builds(self):
        self.assertFalse(gather.parse_args(self.BASE).no_iac_build)
        self.assertTrue(gather.parse_args(self.BASE + ["--no-iac-build"]).no_iac_build)


# --- acquire() end to end: items 5, 9, 10 ---------------------------------------

@unittest.skipUnless(HAVE_GIT, "git not installed")
class TestAcquireUntrustedFiles(_TmpCase):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        _git("init", "-q", "-b", "main", self.src)
        self.outside = _write(self.tmp, "outside/secret.txt", "* @stolen\n")
        _write(self.src, "modules/latin/main.tf",
               b'# caf\xe9\nmodule "net" {\n  source = "../net"\n}\n')
        _write(self.src, "modules/net/main.tf", 'variable "x" {}\n')
        _write(self.src, "modules/utf16/main.bicep",
               "module s '../shared/main.bicep' = {}\n".encode("utf-16"))
        _write(self.src, "modules/shared/main.bicep", "param p string\n")
        os.makedirs(os.path.join(self.src, "modules/gone"))
        os.symlink("does-not-exist.tf", os.path.join(self.src, "modules/gone/main.tf"))
        os.makedirs(os.path.join(self.src, ".github"))
        os.symlink(self.outside, os.path.join(self.src, ".github/CODEOWNERS"))
        _git("add", "-A", cwd=self.src)
        _git("commit", "-q", "-m", "c", cwd=self.src, date="2026-05-20")

    def _acquire(self, *extra):
        clone = os.path.join(self.tmp, "ws", "r")
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", "2026-05-18",
            "--to", "2026-05-31", "--clone-dir", clone, "--structural-iac",
            "--store", os.path.join(self.tmp, "s.db"), "--no-workflows",
            "--no-releases", "--no-project-board", *extra])
        real_provider = gather.select_code_area_provider
        real_extract = gather.extract_iac_edges
        self.extract_kw = []

        def extract(g, d, **kw):
            self.extract_kw.append(dict(kw))
            kw.setdefault("which", lambda _n: None)   # no real toolchain here
            return real_extract(g, d, **kw)
        err = io.StringIO()
        with mock.patch.object(gather, "_github_remote_url",
                               lambda o, r: "file://" + self.src), \
                mock.patch.object(gather, "_paginated",
                                  lambda token: (lambda url: ([], None))), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d, **kw: real_provider(
                                      paths, d, which=lambda _n: None, **kw)), \
                mock.patch.object(gather, "extract_iac_edges", extract), \
                redirect_stderr(err):
            bundle = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        return bundle, err.getvalue()

    def test_member_survives_and_unread_files_are_recorded(self):
        b, err = self._acquire()
        g = b["code_graph"]
        # item 5: latin-1 .tf and UTF-16 .bicep are read; their edges are there
        edges = {(a["id"], e["to"]) for a in g["areas"] for e in a.get("edges", [])}
        self.assertIn(("modules/latin", "modules/net"), edges)
        self.assertIn(("modules/utf16", "modules/shared"), edges)
        # the dangling tracked symlink is skipped with one warning, not scanned
        self.assertEqual(g["structural_scan_unread"], ["modules/gone"])
        self.assertEqual(err.count("modules/gone/main.tf"), 1)
        # item 9: the CODEOWNERS symlink out of the clone is refused, never read
        self.assertEqual(b["code_owners"], {})
        self.assertNotIn("stolen", repr(b))
        self.assertEqual(g["codeowners_unread"], ".github/CODEOWNERS")
        self.assertIn(".github/CODEOWNERS", err)

    def test_no_iac_build_skips_builds_but_still_scans(self):
        b, err = self._acquire("--no-iac-build")
        g = b["code_graph"]
        self.assertIsNone(self.extract_kw[0]["which"]("terraform"))
        self.assertIsNone(self.extract_kw[0]["which"]("bicep"))
        self.assertTrue(g["edge_extraction"]["disabled"])
        self.assertEqual(g["edge_extraction"]["resolved"], 0)
        self.assertTrue(g["structural_scan"])
        self.assertIn("--no-iac-build", err)

    def test_default_still_builds(self):
        self._acquire()
        self.assertNotIn("which", self.extract_kw[0])


# --- item 5: structural scan and replay ------------------------------------------

class TestStructuralScanUnread(_TmpCase):
    def test_raising_reader_marks_the_area_unread_not_fatal(self):
        files = {"modules/a/main.tf": 'module "b" { source = "../b" }\n',
                 "modules/a/extra.tf": None,
                 "modules/b/main.bicep": None}

        def read(rel):
            if files[rel] is None:
                raise OSError(2, "No such file or directory")
            return files[rel]
        cg = {"areas": []}
        with redirect_stderr(io.StringIO()):
            gather.apply_structural_scan(cg, "/clone", list_files=lambda: list(files),
                                         read_text=read)
        self.assertEqual(cg["structural_scan_unread"], ["modules/a", "modules/b"])
        a = next(x for x in cg["areas"] if x["id"] == "modules/a")
        self.assertEqual([e["to"] for e in a["edges"]], ["modules/b"])  # partial, kept
        self.assertNotIn("unread", a)

    def test_clean_scan_has_no_unread_key(self):
        cg = {"areas": []}
        gather.apply_structural_scan(cg, "/clone", list_files=lambda: ["x/main.tf"],
                                     read_text=lambda rel: 'variable "v" {}\n')
        self.assertNotIn("structural_scan_unread", cg)

    def _replay(self, second):
        area = {"id": "modules/a", "paths": ["modules/a/main.tf"],
                "edges": [{"to": "modules/b", "kind": "module", "ref": "../b",
                           "version": None, "transitive": False,
                           "provider": "terraform", "resolved": True}]}
        first = {"areas": [area, {"id": "modules/b", "paths": [], "edges": []}],
                 "structural_scan": True}
        windows = [("2026-04-01", "2026-04-30", first, [], "sha1"),
                   ("2026-05-01", "2026-05-31", second, [], "sha2")]
        replay = gather.StructureReplay(windows, "p", "o/r")
        src = graphstore.qualify_id("p", "o/r", "area-modules/a")
        dst = graphstore.qualify_id("p", "o/r", "area-modules/b")
        return (src, dst, "depends_on") in replay.desired()

    def test_unread_area_keeps_its_edges(self):
        second = {"areas": [{"id": "modules/a", "paths": [], "edges": []}],
                  "structural_scan": True, "structural_scan_unread": ["modules/a"]}
        self.assertTrue(self._replay(second))     # the April edge stays

    def test_read_area_with_no_edge_is_still_retracted(self):
        second = {"areas": [{"id": "modules/a", "paths": [], "edges": []}],
                  "structural_scan": True}
        self.assertFalse(self._replay(second))    # May's scan says it is gone

    def test_unreadable_codeowners_is_not_owner_authority(self):
        cg = {"areas": [{"id": "x", "paths": []}], "codeowners_unread": "CODEOWNERS"}
        self.assertEqual(gather._owns_authority(cg, [], "sha", "p", "o/r"), set())
        cg.pop("codeowners_unread")
        self.assertEqual(len(gather._owns_authority(cg, [], "sha", "p", "o/r")), 1)


class TestReadTextFile(_TmpCase):
    def test_decoding_never_fails(self):
        root = self.tmp
        _write(root, "l.tf", b"# caf\xe9\r\nx\r\n")
        _write(root, "u.bicep", "param é string\n".encode("utf-16"))
        _write(root, "b.bicep", b"\xef\xbb\xbfmodule m 'x' = {}\n")
        self.assertEqual(gather._read_text_file(os.path.join(root, "l.tf")),
                         "# caf\ufffd\nx\n")
        self.assertEqual(gather._read_text_file(os.path.join(root, "u.bicep")),
                         "param é string\n")
        self.assertEqual(gather._read_text_file(os.path.join(root, "b.bicep")),
                         "module m 'x' = {}\n")

    def test_symlink_out_of_the_root_is_refused(self):
        root = os.path.join(self.tmp, "clone")
        outside = _write(self.tmp, "secret", "s3cret\n")
        _write(root, "inner/real.tf", "ok\n")
        os.symlink(outside, os.path.join(root, "out.tf"))
        os.symlink(os.path.join(root, "inner", "real.tf"), os.path.join(root, "in.tf"))
        with self.assertRaises(gather.RepoFileRefused):
            gather._read_text_file(os.path.join(root, "out.tf"), root=root)
        self.assertEqual(gather._read_text_file(os.path.join(root, "in.tf"),
                                                root=root), "ok\n")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(gather._read_repo_text(root, "out.tf"))
        self.assertIn("out.tf", err.getvalue())
        self.assertNotIn("s3cret", err.getvalue())

    def test_relative_dotdot_symlink_is_refused(self):
        root = os.path.join(self.tmp, "clone")
        _write(self.tmp, "secret", "s3cret\n")
        os.makedirs(root)
        os.symlink("../secret", os.path.join(root, "CODEOWNERS"))
        with self.assertRaises(gather.RepoFileRefused):
            gather._read_text_file(os.path.join(root, "CODEOWNERS"), root=root)

    def test_structural_scan_default_reader_refuses_outside_symlink(self):
        root = os.path.join(self.tmp, "clone")
        outside = _write(self.tmp, "evil.tf", 'module "x" { source = "../x" }\n')
        os.makedirs(os.path.join(root, "modules", "a"))
        os.symlink(outside, os.path.join(root, "modules", "a", "main.tf"))
        with redirect_stderr(io.StringIO()):
            out = gather.scan_structural_terraform_areas(
                root, list_files=lambda: ["modules/a/main.tf"])
        self.assertEqual(out["modules/a"]["edges"], [])
        self.assertTrue(out["modules/a"]["unread"])

    def test_iac_build_reader_refuses_outside_symlink(self):
        root = os.path.join(self.tmp, "clone")
        outside = _write(self.tmp, "evil.bicep", "module x '../x/main.bicep' = {}\n")
        os.makedirs(os.path.join(root, "mod"))
        os.symlink(outside, os.path.join(root, "mod", "main.bicep"))
        cg = {"areas": [{"id": "mod", "paths": ["mod/main.bicep"], "edges": []}]}
        gather.extract_iac_edges(cg, root, which=lambda n: n == "bicep",
                                 run=lambda cmd, **kw: "{}", max_workers=1,
                                 retries=0)
        self.assertEqual(cg["areas"][0]["edges_status"], "failed")
        self.assertEqual(cg["areas"][0]["edges"], [])


# --- item 3: Bicep comments and strings ------------------------------------------

class TestBicepModuleRefsMasked(unittest.TestCase):
    def refs(self, src):
        return [r["ref"] for r in gather.parse_bicep_module_refs(src)]

    def test_comments_and_strings_yield_nothing(self):
        for src in ("// module x 'br/public:avm/res/a/b:1.0.0' = {}\n",
                    "/* module x 'br/public:avm/res/a/b:1.0.0' = {} */\n",
                    "/*\nmodule x 'br/public:avm/res/a/b:1.0.0' = {}\n*/\n",
                    "var s = 'module x \\'br/public:avm/res/a/b:1.0.0\\''\n",
                    "var s = '''\nmodule x 'br/public:avm/res/a/b:1.0.0' = {}\n'''\n",
                    "var s = 'a ${'module x'} b'\nvar t = 'x'\n",
                    "output o string = 'x' // module y 'z/main.bicep'\n"):
            self.assertEqual(self.refs(src), [], src)

    def test_real_modules_are_still_found(self):
        src = ("/* header */ module a 'a/main.bicep' = {\n  name: 'a'\n}\n"
               "@batchSize(1)\n@description('module q \\'no/main.bicep\\'')\n"
               "module b 'br/public:avm/res/b/c:0.1.0' = [for i in r: {}]\n"
               "module c 'c/main.bicep' = if (deploy) {\n}\n"
               "module d\n  'd/main.bicep' =\n  {\n}\n"
               "  @sys.description('x') module e 'e/main.bicep' = {}\n"
               "var s = '${x}' // trailing\nmodule f 'f/main.bicep' = {}\n")
        got = gather.parse_bicep_module_refs(src)
        self.assertEqual([r["ref"] for r in got],
                         ["a/main.bicep", "br/public:avm/res/b/c:0.1.0",
                          "c/main.bicep", "d/main.bicep", "e/main.bicep",
                          "f/main.bicep"])
        by = {r["ref"]: r for r in got}
        self.assertEqual(by["a/main.bicep"]["instances"], "one")
        self.assertEqual(by["br/public:avm/res/b/c:0.1.0"]["instances"], "many")
        self.assertEqual(by["br/public:avm/res/b/c:0.1.0"]["version"], "0.1.0")
        self.assertIsNone(by["c/main.bicep"]["instances"])          # as before
        self.assertEqual(by["d/main.bicep"]["instances"], "one")

    def test_commented_cross_repo_module_gives_no_edge(self):
        src = "// module x 'br/public:avm/res/other/thing:1.0.0' = {}\n"
        self.assertEqual(gather.build_bicep_edges(src, None, "m/main.bicep", set()), [])

    def test_mask_keeps_offsets(self):
        src = "a // c\n'x${'y'}z' /* q\n */ '''m\n'''"
        self.assertEqual(len(gather._bicep_mask(src)), len(src))
        self.assertEqual(gather._bicep_mask(src).count("\n"), src.count("\n"))


# --- item 4: reused clone after an in-place IaC build ------------------------------

@unittest.skipUnless(HAVE_GIT, "git not installed")
class TestReusedCloneAfterInPlaceBuild(_TmpCase):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        _git("init", "-q", "-b", "main", self.src)
        _write(self.src, "mod/main.tf", 'variable "x" {}\n')
        _write(self.src, "lock/main.tf", 'variable "y" {}\n')
        _write(self.src, "lock/.terraform.lock.hcl", "# v1\n")
        self.commit("2026-05-02")
        self.url = "file://" + self.src
        self.clone = os.path.join(self.tmp, "ws", "r")
        gather.prepare_clone(self.url, "2026-05-01", self.clone)
        # what `terraform init` left in the clone during a current-month gather
        _write(self.clone, "mod/.terraform/modules/modules.json", "{}")
        _write(self.clone, "mod/.terraform.lock.hcl", "# local\n")
        _write(self.clone, "lock/.terraform.lock.hcl", "# rewritten\n")
        _write(self.clone, "notes.txt", "my own work\n")      # the user's

    def commit(self, date):
        _git("add", "-A", cwd=self.src)
        _git("commit", "-q", "-m", "c " + date, cwd=self.src, date=date)
        return _git("rev-parse", "HEAD", cwd=self.src).strip()

    def head(self):
        return _git("-C", self.clone, "rev-parse", "HEAD").strip()

    def test_upstream_adds_a_file_where_the_build_left_one(self):
        _write(self.src, "mod/.terraform.lock.hcl", "# upstream\n")
        _write(self.src, "lock/.terraform.lock.hcl", "# v2\n")
        new = self.commit("2026-05-20")
        self.assertEqual(gather.prepare_clone(self.url, "2026-05-01", self.clone),
                         "fetched")
        self.assertEqual(self.head(), new)
        with open(os.path.join(self.clone, "mod/.terraform.lock.hcl")) as fh:
            self.assertEqual(fh.read(), "# upstream\n")
        with open(os.path.join(self.clone, "lock/.terraform.lock.hcl")) as fh:
            self.assertEqual(fh.read(), "# v2\n")
        self.assertFalse(os.path.exists(os.path.join(self.clone, "mod/.terraform")))
        with open(os.path.join(self.clone, "notes.txt")) as fh:   # never deleted
            self.assertEqual(fh.read(), "my own work\n")

    def test_base_branch_checkout_is_not_blocked(self):
        _git("checkout", "-q", "-b", "release", cwd=self.src)
        _write(self.src, "mod/.terraform.lock.hcl", "# release\n")
        rel = self.commit("2026-05-21")
        _git("checkout", "-q", "main", cwd=self.src)
        gather.checkout_base_branch(self.clone, "release", "2026-05-01")
        self.assertEqual(self.head(), rel)
        self.assertTrue(os.path.exists(os.path.join(self.clone, "notes.txt")))

    def test_users_untracked_file_in_the_way_still_blocks(self):
        # Only gather's by-products are cleaned: a user's own untracked file at
        # a path upstream now tracks still stops the checkout (never deleted).
        _write(self.src, "notes.txt", "upstream\n")
        self.commit("2026-05-20")
        with self.assertRaises(RuntimeError):
            gather.prepare_clone(self.url, "2026-05-01", self.clone)
        with open(os.path.join(self.clone, "notes.txt")) as fh:
            self.assertEqual(fh.read(), "my own work\n")


# --- item 12: manifest clone dirs ---------------------------------------------------

class TestMemberCloneDirs(_TmpCase):
    def _args(self, base):
        return gather.parse_args(["--manifest", "m.json", "--store", "s.db",
                                  "--clone-dir", base])

    def test_hyphenated_names_do_not_collide(self):
        base = os.path.join(self.tmp, "clones")
        a = gather._member_args(self._args(base), {"owner": "a-b", "repo": "c"}, "x", "y")
        b = gather._member_args(self._args(base), {"owner": "a", "repo": "b-c"}, "x", "y")
        self.assertNotEqual(a.clone_dir, b.clone_dir)
        self.assertEqual(a.clone_dir, base + "/a-b/c")
        self.assertEqual(b.clone_dir, base + "/a/b-c")

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_former_layout_clone_of_the_same_remote_is_reused(self):
        base = os.path.join(self.tmp, "clones")
        old = os.path.join(base, "Azure-mod-clone")
        _git("init", "-q", old)
        _git("-C", old, "remote", "add", "origin", "https://github.com/Azure/mod.git")
        m = gather._member_args(self._args(base), {"owner": "Azure", "repo": "mod"},
                                "x", "y")
        self.assertEqual(m.clone_dir, old)

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_former_layout_clone_of_another_remote_is_not_used(self):
        # `a-b`/`c` and `a`/`b-c` shared `a-b-c-clone`: only its own remote's
        # member gets it.
        base = os.path.join(self.tmp, "clones")
        old = os.path.join(base, "a-b-c-clone")
        _git("init", "-q", old)
        _git("-C", old, "remote", "add", "origin", "https://github.com/a-b/c.git")
        mine = gather._member_args(self._args(base), {"owner": "a-b", "repo": "c"},
                                   "x", "y")
        other = gather._member_args(self._args(base), {"owner": "a", "repo": "b-c"},
                                    "x", "y")
        self.assertEqual(mine.clone_dir, old)
        self.assertEqual(other.clone_dir, base + "/a/b-c")

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_new_layout_wins_once_it_exists(self):
        base = os.path.join(self.tmp, "clones")
        old = os.path.join(base, "o-r-clone")
        _git("init", "-q", old)
        _git("-C", old, "remote", "add", "origin", "https://github.com/o/r.git")
        os.makedirs(os.path.join(base, "o", "r"))
        m = gather._member_args(self._args(base), {"owner": "o", "repo": "r"}, "x", "y")
        self.assertEqual(m.clone_dir, base + "/o/r")


if __name__ == "__main__":
    unittest.main()
