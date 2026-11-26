"""Issue #84: the analyzed mainline branch (`meta.base_branch`).

Two defects, fixed together:

1. The store path dropped `meta.base_branch` (fold never stored it, extract
   never re-emitted it), so `link` saw None, treated every merged PR as merged
   into the mainline and never ran the stacked-PR (`contributing_prs`) logic.
2. gather hard-wired `--branches main`, so a repo whose default branch is
   `master`/`develop` (or a manifest mixing defaults) would report nothing
   shipped once (1) is fixed.

Every test is offline: real git only against throwaway local `file://` repos,
the GitHub API stubbed through `gather._paginated`.
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
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import manifest  # noqa: E402

FROM, TO = "2026-05-01", "2026-05-31"


def _pr(number, base, head, merged=True, state="closed", closed="2026-05-12"):
    ts = closed + "T00:00:00Z"
    return {"number": number, "title": "change {}".format(number), "body": "",
            "author": "alice", "labels": [], "merged": merged,
            "merged_at": ts if merged else None, "closed_at": ts,
            "created_at": "2026-05-02T00:00:00Z", "updated_at": ts,
            "state": state, "base": base, "head": head, "closes": [],
            "crossref_issues": [], "milestone": None, "reviewers": [],
            "url": "https://github.com/o/r/pull/{}".format(number)}


def _bundle(prs, base_branch="main", owner="o", repo="r", frm=FROM, to=TO,
            branches=None):
    meta = {"owner": owner, "repo": repo, "from": frm, "to": to}
    if base_branch is not _ABSENT:
        meta["base_branch"] = base_branch
        meta["branches"] = (branches if branches is not None
                            else ([base_branch] if base_branch else []))
    return {"meta": meta, "prs": prs, "issues": [], "commits": [],
            "code_events": [], "symbol_events": []}


_ABSENT = object()


def _store(*bundles, **fold_kwargs):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, b, **fold_kwargs)
    return conn


def _view(conn, project="o", repo="r", frm=FROM, to=TO):
    warnings = []
    bundle = extract.extract(conn, project, repo, frm, to,
                             warn=warnings.append)
    return link.enrich(bundle), warnings


def _shipped(bundle):
    return sorted(r["id"] for r in bundle["buckets"]["shipped"]
                  if r["type"] == "pr")


def _trains(bundle):
    return [(t["id"], t["outcome"], t["contributing_prs"])
            for t in bundle["trains"]]


def _branch_warnings(warnings):
    return [w for w in warnings if "base branch" in w]


STACKED = [_pr(1, "main", "feat"), _pr(2, "feat", "feat-part")]


class TestStorePathKeepsBaseBranch(unittest.TestCase):
    """Problem 1: fold -> extract -> enrich must match the raw bundle."""

    def test_raw_bundle_reference(self):
        raw = link.enrich(_bundle([dict(p) for p in STACKED]))
        self.assertEqual(_trains(raw), [("train-pr-1", "shipped", [2])])
        self.assertEqual(_shipped(raw), [1])

    def test_stacked_pr_through_the_store(self):
        view, warnings = _view(_store(_bundle(STACKED)))
        self.assertEqual(view["meta"]["base_branch"], "main")
        self.assertEqual(view["meta"]["branches"], ["main"])
        self.assertEqual(_trains(view), [("train-pr-1", "shipped", [2])])
        self.assertEqual(_shipped(view), [1])
        self.assertEqual(_branch_warnings(warnings), [])

    def test_master_default_repo_reports_its_merges(self):
        prs = [_pr(1, "master", "a"), _pr(2, "master", "b")]
        view, _ = _view(_store(_bundle(prs, base_branch="master")))
        self.assertEqual(view["meta"]["base_branch"], "master")
        self.assertEqual(_shipped(view), [1, 2])

    def test_release_branch_merge_is_not_shipped(self):
        prs = [_pr(1, "main", "a"), _pr(2, "release/1.0", "b")]
        view, _ = _view(_store(_bundle(prs)))
        self.assertEqual(_shipped(view), [1])
        self.assertEqual([t["id"] for t in view["trains"]], ["train-pr-1"])

    def test_refold_of_same_window_replaces_value(self):
        conn = _store(_bundle(STACKED, base_branch="main"),
                      _bundle(STACKED, base_branch="develop"))
        view, _ = _view(conn)
        self.assertEqual(view["meta"]["base_branch"], "develop")

    def test_view_meta_is_deterministic(self):
        a, _ = _view(_store(_bundle(STACKED)))
        b, _ = _view(_store(_bundle(STACKED)))
        self.assertEqual(json.dumps(a["meta"]), json.dumps(b["meta"]))


class TestUnknownBaseBranch(unittest.TestCase):
    """Stores with no usable stored value: unknown, warned, never assumed."""

    def test_store_without_the_key_warns_and_keeps_old_behaviour(self):
        # A store folded before #84 (the bundle meta carries no base_branch).
        view, warnings = _view(_store(_bundle(STACKED, base_branch=_ABSENT)))
        self.assertIsNone(view["meta"]["base_branch"])
        self.assertEqual(view["meta"]["branches"], [])
        # Unknown -> every merged PR counts as a mainline merge (pre-#84
        # behaviour), and the reader says so instead of silently assuming main.
        self.assertEqual(_shipped(view), [1, 2])
        msgs = _branch_warnings(warnings)
        self.assertEqual(len(msgs), 1, warnings)
        self.assertIn("re-gather", msgs[0])
        self.assertNotIn("'main'", msgs[0])

    def test_gather_could_not_determine_it(self):
        view, warnings = _view(_store(_bundle(STACKED, base_branch=None)))
        self.assertIsNone(view["meta"]["base_branch"])
        msgs = _branch_warnings(warnings)
        self.assertEqual(len(msgs), 1, warnings)
        self.assertIn("could not determine", msgs[0])

    def test_no_warning_without_merged_prs(self):
        prs = [_pr(1, "main", "a", merged=False, state="open")]
        _, warnings = _view(_store(_bundle(prs, base_branch=_ABSENT)))
        self.assertEqual(_branch_warnings(warnings), [])


class TestWiderViewWindows(unittest.TestCase):
    """A view whose window is not exactly one gathered window (a roll-up)."""

    def _two_windows(self, first, second):
        may = _bundle([_pr(1, "main", "a")], base_branch=first,
                      frm="2026-05-01", to="2026-05-31")
        jun = _bundle([_pr(2, "main", "b", closed="2026-06-10")],
                      base_branch=second, frm="2026-06-01", to="2026-06-30")
        return _store(may, jun)

    def test_agreeing_windows_give_the_value(self):
        view, warnings = _view(self._two_windows("main", "main"),
                               frm="2026-05-01", to="2026-06-30")
        self.assertEqual(view["meta"]["base_branch"], "main")
        self.assertEqual(_branch_warnings(warnings), [])

    def test_conflicting_windows_are_unknown_and_warned(self):
        view, warnings = _view(self._two_windows("master", "main"),
                               frm="2026-05-01", to="2026-06-30")
        self.assertIsNone(view["meta"]["base_branch"])
        msgs = _branch_warnings(warnings)
        self.assertEqual(len(msgs), 1, warnings)
        self.assertIn("main", msgs[0])
        self.assertIn("master", msgs[0])

    def test_exact_window_wins(self):
        view, _ = _view(self._two_windows("master", "main"),
                        frm="2026-06-01", to="2026-06-30")
        self.assertEqual(view["meta"]["base_branch"], "main")

    def test_sub_window_uses_the_overlapping_gather(self):
        view, _ = _view(self._two_windows("master", "main"),
                        frm="2026-05-10", to="2026-05-20")
        self.assertEqual(view["meta"]["base_branch"], "master")


class TestGraphstoreBranches(unittest.TestCase):
    def test_round_trip_per_window(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.set_branches(conn, "p", "r", "2026-05-01", "2026-05-31",
                                "main", ["main", "dev"])
        self.assertEqual(
            graphstore.get_branches(conn, "p", "r", "2026-05-01", "2026-05-31"),
            {"base_branch": "main", "branches": ["main", "dev"]})
        self.assertIsNone(
            graphstore.get_branches(conn, "p", "r", "2026-06-01", "2026-06-30"))
        self.assertIsNone(
            graphstore.get_branches(conn, "p", "other", "2026-05-01", "2026-05-31"))


# --- gather: where the base branch comes from ---------------------------------

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
    default = "master"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-base-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "src")
        _git("init", "-q", "-b", self.default, self.src)
        with open(os.path.join(self.src, "f.txt"), "w") as fh:
            fh.write("x\n")
        _git("add", "-A", cwd=self.src)
        _git("commit", "-q", "-m", "a", cwd=self.src, date="2026-05-20")
        self.url = "file://" + self.src
        self.clone = os.path.join(self.tmp, "ws", "r-clone")
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)


class TestResolveBranches(_RepoCase):
    def test_explicit_flag_wins(self):
        got = gather.resolve_branches("main,release", self.clone, "none")
        self.assertEqual(got, (["main", "release"], "main", "--branches"))

    def test_clone_default_branch(self):
        gather.prepare_clone(self.url, "2026-05-18", self.clone)
        got = gather.resolve_branches(None, self.clone, "fresh",
                                      get_repo=lambda: self.fail("no API call"))
        self.assertEqual(got[:2], (["master"], "master"))

    def test_checked_out_branch_when_origin_head_is_missing(self):
        gather.prepare_clone(self.url, "2026-05-18", self.clone)
        _git("-C", self.clone, "remote", "set-head", "origin", "-d")
        got = gather.resolve_branches(None, self.clone, "fresh",
                                      get_repo=lambda: self.fail("no API call"))
        self.assertEqual(got, (["master"], "master", "clone"))

    def test_api_when_there_is_no_clone(self):
        got = gather.resolve_branches(
            None, self.clone, "none", get_repo=lambda: {"default_branch": "develop"})
        self.assertEqual(got[:2], (["develop"], "develop"))

    def test_unknown_when_nothing_answers(self):
        for answer in ([], None, {"default_branch": ""}, {}):
            got = gather.resolve_branches(None, self.clone, "none",
                                          get_repo=lambda a=answer: a)
            self.assertEqual(got, ([], None, None), answer)

    def test_branches_flag_no_longer_defaults_to_main(self):
        args = gather.parse_args(["--owner", "o", "--repo", "r", "--from", FROM,
                                  "--to", TO, "--store", "s.db"])
        self.assertIsNone(args.branches)


class TestAcquireBaseBranch(_RepoCase):
    """acquire() end to end: a `master`-default repo, no `--branches`."""

    RAW_PR = {"number": 7, "title": "fix: x", "body": "", "user": {"login": "a"},
              "state": "closed", "merged_at": "2026-05-20T00:00:00Z",
              "closed_at": "2026-05-20T00:00:00Z",
              "created_at": "2026-05-19T00:00:00Z",
              "updated_at": "2026-05-20T00:00:00Z",
              "base": {"ref": "master"}, "head": {"ref": "fix-x"},
              "html_url": "https://github.com/o/r/pull/7"}

    def _acquire(self, *extra, repo_json=None):
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", "2026-05-18", "--to", "2026-05-31",
            "--clone-dir", self.clone, "--store", os.path.join(self.tmp, "s.db"),
            "--no-workflows", "--no-releases", "--no-project-board", *extra])
        real_provider = gather.select_code_area_provider

        def get_page(url):
            if url == "https://api.github.com/repos/o/r":
                return repo_json, None
            if "/pulls?state=closed" in url:
                return [dict(self.RAW_PR)], None
            return [], None

        err = io.StringIO()
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated", lambda token: get_page), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d: real_provider(
                                      paths, d, which=lambda _n: None)), \
                mock.patch.object(gather, "extract_iac_edges", lambda g, d: g), \
                mock.patch.object(gather, "_graphql_client", lambda token: None), \
                contextlib.redirect_stderr(err):
            # no GraphQL client: the closing-issue read (#105) stays offline
            bundle = gather.acquire(args, {"GITHUB_TOKEN": "dummy"})
        return bundle, err.getvalue()

    def test_master_default_detected_and_reported_as_shipped(self):
        b, _ = self._acquire()
        self.assertEqual(b["meta"]["base_branch"], "master")
        self.assertEqual(b["meta"]["branches"], ["master"])
        conn = _store(b)
        view, warnings = _view(conn, frm="2026-05-18", to="2026-05-31")
        self.assertEqual(view["meta"]["base_branch"], "master")
        self.assertEqual(_shipped(view), [7])
        self.assertEqual(_branch_warnings(warnings), [])

    def test_explicit_branches_still_override(self):
        b, _ = self._acquire("--branches", "main")
        self.assertEqual(b["meta"]["base_branch"], "main")
        view, _ = _view(_store(b), frm="2026-05-18", to="2026-05-31")
        self.assertEqual(_shipped(view), [])   # the user said main; PR went to master

    def test_no_clone_uses_the_api(self):
        b, _ = self._acquire("--no-clone", repo_json={"default_branch": "trunk"})
        self.assertEqual(b["meta"]["base_branch"], "trunk")

    def test_unresolved_is_warned_not_assumed(self):
        b, err = self._acquire("--no-clone", repo_json=[])
        self.assertIsNone(b["meta"]["base_branch"])
        self.assertEqual(b["meta"]["branches"], [])
        self.assertIn("base branch", err)


# --- manifest: per-member branch, mixed defaults ------------------------------

class TestManifestBranch(unittest.TestCase):
    def _load(self, repos):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"project": "p",
                           "window": {"from": FROM, "to": TO},
                           "repos": repos}, fh)
            return manifest.load_manifest(path)

    def test_optional_member_branch(self):
        m = self._load([{"owner": "o", "repo": "a", "branch": "develop"},
                        {"owner": "o", "repo": "b"}])
        self.assertEqual(m["repos"][0]["branch"], "develop")
        self.assertIsNone(m["repos"][1]["branch"])

    def test_bad_member_branch_rejected(self):
        for bad in (123, "", " ", "has space", "-x", ["main"]):
            with self.assertRaisesRegex(ValueError, "branch"):
                self._load([{"owner": "o", "repo": "a", "branch": bad}])

    def test_member_args_take_the_member_branch(self):
        base = gather.parse_args(["--manifest", "m.json", "--store", "s.db"])
        a = gather._member_args(base, {"owner": "o", "repo": "a",
                                       "registry": None, "branch": "develop"},
                                FROM, TO)
        b = gather._member_args(base, {"owner": "o", "repo": "b",
                                       "registry": None, "branch": None},
                                FROM, TO)
        self.assertEqual(a.branches, "develop")
        self.assertIsNone(b.branches)          # resolved per member by acquire

    def test_member_branch_beats_global_flag(self):
        base = gather.parse_args(["--manifest", "m.json", "--store", "s.db",
                                  "--branches", "main"])
        a = gather._member_args(base, {"owner": "o", "repo": "a",
                                       "registry": None, "branch": "develop"},
                                FROM, TO)
        b = gather._member_args(base, {"owner": "o", "repo": "b",
                                       "registry": None}, FROM, TO)
        self.assertEqual(a.branches, "develop")
        self.assertEqual(b.branches, "main")

    def test_mixed_defaults_in_one_project_store(self):
        # Member a defaults to main, member b to master; each also merged one
        # PR into a non-mainline branch. One store, each member its own value.
        a = _bundle([_pr(1, "main", "x"), _pr(2, "release/1", "y")],
                    base_branch="main", owner="p", repo="o/a")
        b = _bundle([_pr(3, "master", "x"), _pr(4, "develop", "y")],
                    base_branch="master", owner="p", repo="o/b")
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        members = {"o/a", "o/b"}
        gather.fold_bundle(conn, a, project="p", repo="o/a", members=members)
        gather.fold_bundle(conn, b, project="p", repo="o/b", members=members)
        out = digest.member_bundles(conn, "p", ["o/a", "o/b"], FROM, TO,
                                    warn=lambda _m: None)
        got = {m["repo"]: (m["bundle"]["meta"]["base_branch"],
                           _shipped(m["bundle"])) for m in out}
        self.assertEqual(got, {"o/a": ("main", [1]), "o/b": ("master", [3])})


if __name__ == "__main__":
    unittest.main()
