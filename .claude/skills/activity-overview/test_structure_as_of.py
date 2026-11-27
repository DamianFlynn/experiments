"""Structure reads describe the mainline as of the window's end (issue #107,
item 4).

The code graph's whole-tree structural scan, the IaC entrypoints/builds and
CODEOWNERS read the checked-out tree. Before #107 that was always the branch
head, so a past window re-gathered later described TODAY's tree. Now acquire()
reads structure at the last commit of the walked branch (first-parent chain)
whose committer date is on or before `--to`, when the bounded clone has it,
from a temporary `git worktree` of that commit, so the clone itself never
leaves its branch head. When that commit is not in the clone the head is
used. `meta.clone_sha` names the commit the
structure was read at and `meta.structure_as_of` says which rule applied
("window_end" | "head").

Gathering the current month (window end on or after the head's date) reads
the head, exactly as before: TestCurrentMonthUnchanged compares acquire()'s
bundle with one captured from the code before #107
(fixtures/acquire_current_month_golden.json).
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
from test_walked_branch import GitRepoCase, git, owners  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "fixtures", "acquire_current_month_golden.json")

HUB_V1 = ("module vnet 'br/public:avm/res/net/vnet:0.1.0' = {\n"
          "  name: 'vnet'\n}\n")
HUB_V2 = ("module spoke './spoke/main.bicep' = {\n"
          "  name: 'spoke'\n}\n")


# --- the current-month repository (shared with the golden capture) -----------

CURRENT_FROM, CURRENT_TO = "2026-05-01", "2026-05-31"


def build_current_month_repo(case):
    """A `main`-default repo whose layouts are all ones item 9 leaves alone
    (AVM modules, Terraform modules/<name>, a two-segment Bicep module dir
    with its README, docs, root files), with two in-window commits and nothing
    after the window."""
    case.commit({
        "README.md": "repo\n",
        ".github/CODEOWNERS": "* @core\n/platform/ @platform-team\n",
        "avm/res/net/vnet/main.bicep": "param name string\n",
        "avm/res/net/vnet/README.md": "vnet\n",
        "modules/storage/main.tf": 'module "x" {\n  source = "../net"\n}\n',
        "modules/storage/README.md": "storage\n",
        "modules/net/main.tf": 'variable "n" {}\n',
        "platform/hub/main.bicep": HUB_V1,
        "platform/hub/README.md": "hub\n",
        "docs/guide.md": "guide\n",
    }, "2026-03-01")
    case.commit({
        "platform/hub/README.md": "hub v2\n",
        "avm/res/net/vnet/main.bicep": "param name string\nparam loc string\n",
        "modules/storage/main.tf": 'module "x" {\n  source = "../net"\n}\n# v2\n',
        "docs/guide.md": "guide v2\n",
    }, "2026-05-05")
    case.commit({
        ".github/CODEOWNERS": "* @core\n/platform/ @hub-team\n",
        "README.md": "repo v2\n",
        "platform/hub/main.bicep": HUB_V1 + "// v2\n",
    }, "2026-05-20")


def normalized(bundle):
    """The bundle without the per-run clone path (a temp dir)."""
    b = json.loads(json.dumps(bundle, sort_keys=True))
    b["meta"]["clone_dir"] = "<clone>"
    return b


class TestCurrentMonthUnchanged(GitRepoCase):
    """A gather of the current month on a `main`-default repo is the bundle
    the code before #107 produced, plus `meta.structure_as_of`."""

    def _golden(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            return json.load(fh)

    def _check(self, key, *extra):
        build_current_month_repo(self)
        b, err = self.acquire(CURRENT_FROM, CURRENT_TO, *extra)
        got = normalized(b)
        self.assertEqual(got["meta"].pop("structure_as_of"), "window_end")
        # Added by the closing-issues read (#105) after the golden was captured:
        # where `closes` came from. Not part of what this gate pins.
        self.assertIn(got["meta"].pop("closing_refs_source"),
                      ("graphql", "keywords"))
        # Added by the first-time-contributor check after the golden was
        # captured: this repo has no PR / issue, so no candidate and no call.
        self.assertEqual(got.pop("newcomers"), {
            "collected": True, "before": CURRENT_FROM, "candidates": 0,
            "new": [], "unknown": [], "not_checked": []})
        # Added by the module change notes after the golden was captured:
        # this repo has no changelog, version file or tag.
        self.assertEqual(got.pop("module_notes"), {
            "collected": True, "changelogs": [], "versions": [],
            "tags": {"collected": False, "reason": "the clone holds no tags"}})
        # Added by the contributor affiliation check after the golden was
        # captured: no PR / issue and no commit resolved to a login, so
        # nobody to label and no call.
        self.assertEqual(got.pop("affiliations"), {
            "collected": True, "home_orgs": ["o"], "home_names": ["o"],
            "home_email_domains": [], "people": {}, "unknown": [],
            "not_checked": []})
        # Added by the module ownership fact after the golden was captured:
        # this repo's CODEOWNERS moved `platform` from one team to another
        # in the window.
        own = got.pop("module_ownership")
        self.assertEqual([(e["kind"], e["module"], e["people"], e["via"])
                          for e in own["events"]],
                         [("owner_added", "platform", ["hub-team"], "codeowners"),
                          ("owner_removed", "platform", ["platform-team"],
                           "codeowners")])
        self.assertEqual(own["without_owner"], [])
        # The view contract version is now stamped on gather's bundle too
        # (one version everywhere); the golden was captured when it was 1.
        golden = json.loads(json.dumps(self._golden()[key]))
        self.assertEqual(golden["meta"]["schema_version"], 1)
        self.assertEqual(got["meta"].pop("schema_version"),
                         extract.VIEW_CONTRACT_VERSION)
        golden["meta"].pop("schema_version")
        self.assertEqual(got["meta"]["clone_sha"], self.rev("HEAD"))
        self.assertEqual(got, golden)
        self.assertEqual(self.checked_out(), ("main", self.rev("HEAD")))
        self.assertNotIn("remote default", err)
        self.assertNotIn("structure", err)
        return b

    def test_window_scoped_bundle_is_unchanged(self):
        self._check("window")

    def test_structural_bundle_is_unchanged(self):
        self._check("structural", "--structural-iac")

    def test_no_checkout_moves_head(self):
        build_current_month_repo(self)
        calls = []
        real = gather.run_git

        def run(args, cwd=None, timeout=None):
            calls.append(list(args))
            return real(args, cwd=cwd, timeout=timeout)
        with mock.patch.object(gather, "run_git", run):
            self.acquire(CURRENT_FROM, CURRENT_TO, "--structural-iac")
        self.assertEqual([c for c in calls if "worktree" in c or "checkout" in c
                          and "--detach" in c], [])
        # one tracked-file listing, shared by the area rule and the scan
        self.assertEqual(sum(1 for c in calls if "ls-files" in c), 1)


# --- a past window re-gathered later ------------------------------------------

class _PastWindowRepo(GitRepoCase):
    """April: the hub module references the public vnet module and CODEOWNERS
    gives /platform/ to @april-team. May (after the April window): the hub
    references a local spoke module instead and /platform/ moves to
    @may-team."""

    APR_FROM, APR_TO = "2026-04-01", "2026-04-30"

    def setUp(self):
        super().setUp()
        self.base = self.commit({
            "README.md": "repo\n",
            ".github/CODEOWNERS": "/platform/ @april-team\n",
            "avm/res/net/vnet/main.bicep": "param name string\n",
            "platform/hub/main.bicep": HUB_V1,
        }, "2026-03-25")
        self.april = self.commit({"platform/hub/README.md": "hub\n"},
                                 "2026-04-15")
        self.may = self.commit({
            ".github/CODEOWNERS": "/platform/ @may-team\n",
            "platform/hub/main.bicep": HUB_V2,
            "platform/hub/spoke/main.bicep": "param p string\n",
        }, "2026-05-20")


def worktrees(repo):
    """The worktree paths git has registered for `repo` (the clone itself
    first)."""
    out = git("-C", repo, "worktree", "list", "--porcelain")
    return [ln[len("worktree "):] for ln in out.splitlines()
            if ln.startswith("worktree ")]


def _edges(bundle, area):
    return sorted((e["to"], e["ref"]) for a in bundle["code_graph"]["areas"]
                  if a["id"] == area for e in a.get("edges") or [])


class TestPastWindowReadsItsOwnTree(_PastWindowRepo):
    def test_codeowners_and_module_source_as_of_window_end(self):
        b, err = self.acquire(self.APR_FROM, self.APR_TO, "--structural-iac")
        self.assertEqual(b["meta"]["structure_as_of"], "window_end")
        self.assertEqual(b["meta"]["clone_sha"], self.april)
        self.assertEqual(owners(b), {"/platform/": ["april-team"]})
        self.assertEqual(_edges(b, "platform/hub"),
                         [("avm/res/net/vnet", "br/public:avm/res/net/vnet:0.1.0")])
        # the May-only module is not in the April structure
        self.assertNotIn("platform/hub/spoke",
                         [a["id"] for a in b["code_graph"]["areas"]])
        self.assertIn(self.april[:12], err)
        # the clone never left its branch head; the temporary worktree is gone
        self.assertEqual(self.checked_out(), ("main", self.may))
        self.assertEqual(len(worktrees(self.clone)), 1)
        self.assertEqual(git("-C", self.clone, "status", "--porcelain"), "")

    def test_current_window_reads_the_head(self):
        b, _ = self.acquire("2026-05-01", "2026-05-31", "--structural-iac")
        self.assertEqual(b["meta"]["structure_as_of"], "window_end")
        self.assertEqual(b["meta"]["clone_sha"], self.may)
        self.assertEqual(owners(b), {"/platform/": ["may-team"]})
        self.assertEqual(_edges(b, "platform/hub"),
                         [("platform/hub/spoke", "./spoke/main.bicep")])

    def test_regather_of_april_after_may_on_a_reused_clone(self):
        may, _ = self.acquire("2026-05-01", "2026-05-31", "--structural-iac")
        apr, _ = self.acquire(self.APR_FROM, self.APR_TO, "--structural-iac")
        self.assertEqual(apr["meta"]["clone_mode"], "fetched")
        self.assertEqual(owners(apr), {"/platform/": ["april-team"]})
        self.assertEqual(apr["meta"]["clone_sha"], self.april)
        self.assertEqual(self.checked_out(), ("main", self.may))
        # both windows in one store: the trust check stays green
        self.assertValidates(may, apr)

    def test_trust_check_green_on_the_past_window(self):
        b, _ = self.acquire(self.APR_FROM, self.APR_TO, "--structural-iac")
        self.assertValidates(b)

    def test_failed_structure_read_leaves_no_worktree(self):
        seen = []

        def boom(code_graph, tree, **kw):
            seen.append(tree)
            self.assertEqual(self.checked_out(tree)[1], self.april)
            raise RuntimeError("scan failed (test)")
        with mock.patch.object(gather, "apply_structural_scan", boom), \
                self.assertRaises(RuntimeError):
            self.acquire(self.APR_FROM, self.APR_TO, "--structural-iac")
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))
        self.assertEqual(len(worktrees(self.clone)), 1)
        self.assertEqual(self.checked_out(), ("main", self.may))


class TestWindowEndNotInClone(GitRepoCase):
    def test_quiet_window_with_later_commits_keeps_the_head(self):
        # nothing between Jan and May; the March window's bound (Feb 15) only
        # reaches the May commit, so the mainline as of March is not on disk
        self.commit({".github/CODEOWNERS": "* @old\n"}, "2026-01-10")
        head = self.commit({".github/CODEOWNERS": "* @new\n"}, "2026-05-20")
        b, err = self.acquire("2026-03-01", "2026-03-31")
        self.assertEqual(b["meta"]["structure_as_of"], "head")
        self.assertEqual(b["meta"]["clone_sha"], head)
        self.assertEqual(owners(b), {"*": ["new"]})
        notes = [ln for ln in err.splitlines() if "structure" in ln]
        self.assertEqual(len(notes), 1, err)
        self.assertIn("head", notes[0])
        self.assertEqual(self.checked_out(), ("main", head))

    def test_depth1_fallback_tip_is_the_window_end(self):
        # a quiet repo: its only commit predates the window, so the depth-1
        # tip IS the last commit on or before --to
        tip = self.commit({"a.txt": "a\n"}, "2025-06-01")
        b, _ = self.acquire("2026-05-01", "2026-05-31")
        self.assertEqual(b["meta"]["clone_mode"], "depth1-fallback")
        self.assertEqual(b["meta"]["structure_as_of"], "window_end")
        self.assertEqual(b["meta"]["clone_sha"], tip)

    def test_no_clone_has_no_structure_commit(self):
        b, _ = self.acquire("2026-05-01", "2026-05-31", "--no-clone")
        self.assertIsNone(b["meta"]["clone_sha"])
        self.assertIsNone(b["meta"]["structure_as_of"])


class TestWindowEndCommitHelper(GitRepoCase):
    def test_first_parent_chain_last_commit_on_or_before_to(self):
        a = self.commit({"f": "1\n"}, "2026-04-01")
        git("checkout", "-q", "-b", "topic", cwd=self.src)
        topic = self.commit({"g": "1\n"}, "2026-04-20")   # merged in May
        git("checkout", "-q", "main", cwd=self.src)
        b = self.commit({"f": "2\n"}, "2026-04-10")
        env = {"GIT_AUTHOR_DATE": "2026-05-02T12:00:00Z",
               "GIT_COMMITTER_DATE": "2026-05-02T12:00:00Z"}
        with mock.patch.dict(os.environ, env):
            git("merge", "-q", "--no-ff", "-m", "merge topic", "topic",
                cwd=self.src)
        # as of 2026-04-30 the mainline was `b`; `topic` (dated 04-20) was
        # not on it yet
        self.assertEqual(gather.window_end_commit(self.src, "2026-04-30"), b)
        self.assertEqual(gather.window_end_commit(self.src, "2026-04-05"), a)
        self.assertIsNone(gather.window_end_commit(self.src, "2026-03-31"))
        self.assertEqual(gather.window_end_commit(self.src, "2026-05-31"),
                         self.rev("HEAD"))
        self.assertNotEqual(topic, b)


if __name__ == "__main__":
    unittest.main()
