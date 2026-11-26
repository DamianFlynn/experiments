"""People identity (#78): one person, one key — GitHub login, not git author name.

PR / issue / review participants are GitHub LOGINS; commit authors come from git
`%an` (a free-text NAME). Before #78 a PR authored by `janedoe` and a commit
authored by `Jane Doe` became two people. gather now resolves each commit's
author to a login (commits-list API `author.login` joined on SHA, then the
`users.noreply.github.com` email), keeps the git name in `author_name`, and flags
what it could not resolve (`login_resolved: false`) under a key that can never
equal a login, so an unresolved name is never merged on a guess.

No network: the GitHub API is stubbed through acquire's `_paginated` seam and the
end-to-end tests drive real git against a throwaway local `file://` repo (the
harness is shared with test_clone_handling.py).
"""
import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402
import test_clone_handling as tch  # noqa: E402
from _quiet import quiet  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
FROM, TO = "2026-05-18", "2026-05-31"


def _commit(sha, name, email=None, files=(), message="msg"):
    c = {"sha": sha, "parents": [], "author": name, "date": "2026-05-20",
         "message": message, "files": list(files), "pr": None}
    if email is not None:
        c["author_email"] = email
    return c


class TestNoreplyEmail(unittest.TestCase):
    def test_id_plus_login_form(self):
        self.assertEqual(gather.login_from_noreply_email(
            "1234567+janedoe@users.noreply.github.com"), "janedoe")

    def test_legacy_login_only_form(self):
        self.assertEqual(gather.login_from_noreply_email(
            "janedoe@users.noreply.github.com"), "janedoe")

    def test_bot_form(self):
        self.assertEqual(gather.login_from_noreply_email(
            "49699333+dependabot[bot]@users.noreply.github.com"), "dependabot[bot]")

    def test_domain_is_case_insensitive(self):
        self.assertEqual(gather.login_from_noreply_email(
            "1+jane-doe@Users.NoReply.GitHub.com"), "jane-doe")

    def test_non_noreply_and_malformed_are_none(self):
        for email in (None, "", "jane@corp.example", "jane@noreply.github.com",
                      "-bad@users.noreply.github.com", "a b@users.noreply.github.com",
                      "x@users.noreply.github.com.evil.example"):
            self.assertIsNone(gather.login_from_noreply_email(email), email)


class TestCommitLoginsFromApi(unittest.TestCase):
    def test_joins_sha_to_author_login_and_skips_unlinked(self):
        items = [
            {"sha": "a" * 40, "author": {"login": "janedoe"}},
            {"sha": "b" * 40, "author": None},           # email not linked
            {"sha": "c" * 40},                            # no author key
            {"sha": "d" * 40, "author": {"login": ""}},
            {"author": {"login": "nosha"}},
        ]
        self.assertEqual(gather.commit_logins_from_api(items), {"a" * 40: "janedoe"})


class TestResolveCommitAuthors(unittest.TestCase):
    def test_api_login_wins_and_keeps_git_name(self):
        c = _commit("a" * 40, "Jane Doe", "jane@corp.example")
        gather.resolve_commit_authors([c], {"a" * 40: "janedoe"})
        self.assertEqual(c["author"], "janedoe")
        self.assertEqual(c["author_name"], "Jane Doe")
        self.assertIs(c["login_resolved"], True)
        self.assertEqual(c["login_source"], "commits_api")

    def test_api_login_beats_noreply_email(self):
        c = _commit("a" * 40, "Jane", "9+other@users.noreply.github.com")
        gather.resolve_commit_authors([c], {"a" * 40: "janedoe"})
        self.assertEqual(c["author"], "janedoe")

    def test_noreply_fallback(self):
        c = _commit("a" * 40, "Jane Doe", "9+janedoe@users.noreply.github.com")
        gather.resolve_commit_authors([c], {})
        self.assertEqual(c["author"], "janedoe")
        self.assertEqual(c["author_name"], "Jane Doe")
        self.assertIs(c["login_resolved"], True)
        self.assertEqual(c["login_source"], "noreply_email")

    def test_noreply_login_takes_known_login_case(self):
        # GitHub logins are case-insensitive; the canonical spelling seen on a
        # PR/issue record wins so one person keeps ONE key.
        c = _commit("a" * 40, "Jane Doe", "9+janedoe@users.noreply.github.com")
        gather.resolve_commit_authors([c], {}, known_logins={"JaneDoe", "bob"})
        self.assertEqual(c["author"], "JaneDoe")

    def test_unresolved_name_is_flagged_and_kept(self):
        c = _commit("a" * 40, "Jane Doe", "jane@corp.example")
        gather.resolve_commit_authors([c], {})
        self.assertEqual(c["author"], "Jane Doe")
        self.assertEqual(c["author_name"], "Jane Doe")
        self.assertIs(c["login_resolved"], False)
        self.assertNotIn("login_source", c)

    def test_unresolved_login_shaped_name_never_collides_with_a_login(self):
        # The git name `janedoe` is NOT proof of the login `janedoe`.
        c = _commit("a" * 40, "janedoe", "jane@corp.example")
        gather.resolve_commit_authors([c], {}, known_logins={"janedoe"})
        self.assertEqual(c["author"], "janedoe" + derive.UNRESOLVED_NAME_SUFFIX)
        self.assertEqual(c["author_name"], "janedoe")
        self.assertIs(c["login_resolved"], False)

    def test_email_is_never_persisted(self):
        cs = [_commit("a" * 40, "Jane", "9+janedoe@users.noreply.github.com"),
              _commit("b" * 40, "Bob", "bob@corp.example"),
              _commit("c" * 40, "Cy")]
        gather.resolve_commit_authors(cs, {"c" * 40: "cy"})
        for c in cs:
            self.assertNotIn("author_email", c)

    def test_empty_name_stays_empty(self):
        c = _commit("a" * 40, "", "x@corp.example")
        gather.resolve_commit_authors([c], {})
        self.assertEqual(c["author"], "")

    def test_returns_sha_map_and_remaps_events(self):
        cs = [_commit("a" * 40, "Jane Doe", "jane@corp.example"),
              _commit("b" * 40, "Bob", "bob@corp.example")]
        by_sha = gather.resolve_commit_authors(cs, {"a" * 40: "janedoe"})
        self.assertEqual(by_sha, {"a" * 40: "janedoe",
                                  "b" * 40: "Bob" + derive.UNRESOLVED_NAME_SUFFIX})
        events = [{"commit": "a" * 40, "author": "Jane Doe", "path": "x"},
                  {"commit": "b" * 40, "author": "Bob", "path": "y"},
                  {"commit": "e" * 40, "author": "Elsewhere", "path": "z"}]
        gather.remap_event_authors(events, by_sha)
        self.assertEqual([e["author"] for e in events],
                         ["janedoe", "Bob" + derive.UNRESOLVED_NAME_SUFFIX, "Elsewhere"])


class TestParseGitLogEmail(unittest.TestCase):
    def test_fourth_field_is_the_transient_author_email(self):
        raw = (gather.RECORD_SEP + gather.FIELD_SEP.join(
            ["a" * 40, "", "Jane Doe", "jane@corp.example",
             "2026-05-20T10:00:00Z", "subj"])
            + "\nf.txt\n")
        (c,) = gather.parse_git_log(raw)
        self.assertEqual(c["author"], "Jane Doe")
        self.assertEqual(c["author_email"], "jane@corp.example")
        self.assertEqual(c["files"], ["f.txt"])

    def test_empty_email_adds_no_key(self):
        raw = (gather.RECORD_SEP + gather.FIELD_SEP.join(
            ["a" * 40, "", "Jane Doe", "", "2026-05-20T10:00:00Z", "subj"])
            + "\nf.txt\n")
        (c,) = gather.parse_git_log(raw)
        self.assertNotIn("author_email", c)

    def test_commit_walk_requests_author_email(self):
        self.assertIn("%ae", " ".join(gather.COMMIT_WALK_ARGS))


class TestBotNameForms(unittest.TestCase):
    def test_name_forms_are_bots(self):
        for name in ("GitHub Actions", "github actions", "github-actions",
                     "dependabot[bot]", "Dependabot", "renovate[bot]", "Renovate Bot",
                     "github-actions" + derive.UNRESOLVED_NAME_SUFFIX,
                     "dependabot[bot]" + derive.UNRESOLVED_NAME_SUFFIX):
            self.assertTrue(derive.is_bot_login(name), name)

    def test_humans_are_not_bots(self):
        for name in ("Jane Doe", "janedoe", "janedoe" + derive.UNRESOLVED_NAME_SUFFIX,
                     "Actions Person", "", None):
            self.assertFalse(derive.is_bot_login(name), name)


def _repro_bundle(commit):
    """The issue's repro: PR #7 by `janedoe` (merged) + one commit in it."""
    return {
        "meta": {"owner": "o", "repo": "r", "from": FROM, "to": TO},
        "prs": [{"number": 7, "title": "Add x", "author": "janedoe", "merged": True,
                 "state": "closed", "merged_at": "2026-05-21T10:00:00Z",
                 "created_at": "2026-05-20T10:00:00Z", "reviewers": [],
                 "closes": [], "crossref_issues": [],
                 "url": "https://github.com/o/r/pull/7"}],
        "issues": [],
        "commits": [commit],
        "code_graph": {"provider": "directory", "areas": [
            {"id": "mod/x", "label": "x", "paths": ["mod/x/main.tf"], "edges": []}]},
    }


def _people(bundle):
    resolved = copy.deepcopy(bundle["commits"])
    gather.attach_commit_prs(resolved)
    view = {**bundle, "commits": resolved}
    people = derive.enumerate_participants(view)
    derive.annotate_people_profile({**view, "people": people})
    return people


class TestIssueRepro(unittest.TestCase):
    def _commit(self, email):
        return _commit("a" * 40, "Jane Doe", email, files=["mod/x/main.tf"],
                       message="Add x (#7)")

    def test_api_resolved_commit_is_one_person(self):
        c = self._commit("jane@corp.example")
        gather.resolve_commit_authors([c], {"a" * 40: "janedoe"})
        people = _people(_repro_bundle(c))
        self.assertEqual(sorted(people), ["janedoe"])
        p = people["janedoe"]
        self.assertEqual((p["prs_merged"], p["commits_authored"]), (1, 1))
        self.assertEqual(p["areas"], ["mod/x"])
        self.assertNotIn("login_resolved", p)

    def test_noreply_resolved_commit_is_one_person(self):
        c = self._commit("77+janedoe@users.noreply.github.com")
        gather.resolve_commit_authors([c], {})
        people = _people(_repro_bundle(c))
        self.assertEqual(sorted(people), ["janedoe"])
        self.assertEqual((people["janedoe"]["prs_merged"],
                          people["janedoe"]["commits_authored"]), (1, 1))

    def test_unresolved_name_stays_separate_and_flagged(self):
        c = self._commit("jane@corp.example")
        gather.resolve_commit_authors([c], {})
        people = _people(_repro_bundle(c))
        self.assertEqual(sorted(people), ["Jane Doe", "janedoe"])
        self.assertIs(people["Jane Doe"]["login_resolved"], False)
        self.assertEqual(people["Jane Doe"]["commits_authored"], 1)
        self.assertNotIn("login_resolved", people["janedoe"])
        self.assertEqual(people["janedoe"]["prs_merged"], 1)

    def test_unresolved_bot_name_is_tagged(self):
        c = _commit("b" * 40, "GitHub Actions", "actions@github.com")
        gather.resolve_commit_authors([c], {})
        people = _people(_repro_bundle(c))
        self.assertTrue(people["GitHub Actions"]["is_bot"])
        self.assertIs(people["GitHub Actions"]["login_resolved"], False)

    def test_legacy_commit_without_flag_is_unchanged(self):
        # A bundle gathered before #78 (no login_resolved key) keeps its keys and
        # gets no flag — golden fixtures stay byte-stable.
        c = {"sha": "a" * 40, "author": "janedoe", "files": ["mod/x/main.tf"],
             "message": "Add x (#7)", "pr": None}
        people = _people(_repro_bundle(c))
        self.assertEqual(sorted(people), ["janedoe"])
        self.assertNotIn("login_resolved", people["janedoe"])


class TestFoldAndValidate(unittest.TestCase):
    """Person nodes are keyed by login; validate (participant_completeness,
    no_drift — which re-derive via the SAME enumerate_participants) passes on a
    store holding resolved and unresolved commit authors."""

    def _bundle(self):
        with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
            b = json.load(fh)
        for c in b["commits"]:
            if c["author"] == "alice":
                c["author"] = "Alice A"
                c["author_email"] = "alice@corp.example"
            else:
                c["author"] = "dave"
                c["author_email"] = "dave@corp.example"
        api = {c["sha"]: "alice" for c in b["commits"] if c["author"] == "Alice A"}
        by_sha = gather.resolve_commit_authors(b["commits"], api)
        gather.remap_event_authors(b["code_events"], by_sha)
        return b

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        self.bundle = self._bundle()
        gather.fold_bundle(self.conn, self.bundle)

    def test_person_nodes(self):
        alice = graphstore.get_node(self.conn, graphstore.qualify_person("o", "alice"))
        self.assertIn("avm/res/network/firewall-policy", alice["data"]["areas"])
        self.assertNotIn("login_resolved", alice["data"])
        self.assertIsNone(graphstore.get_node(
            self.conn, graphstore.qualify_person("o", "Alice A")))
        dave_key = "dave" + derive.UNRESOLVED_NAME_SUFFIX
        dave = graphstore.get_node(self.conn, graphstore.qualify_person("o", dave_key))
        self.assertIs(dave["data"]["login_resolved"], False)
        # the stored commit record keeps both forms
        sha = next(c["sha"] for c in self.bundle["commits"] if c["author"] == "alice")
        node = graphstore.get_node(self.conn, graphstore.qualify_id("o", "r", sha))
        self.assertEqual(node["data"]["author_name"], "Alice A")
        self.assertNotIn("author_email", node["data"])

    def _checks(self, report):
        return {c["name"]: c for c in report.checks}

    def test_validate_with_bundle(self):
        checks = self._checks(validate.validate(self.conn, "o", "r", self.bundle))
        for name in ("participant_completeness", "no_drift"):
            self.assertTrue(checks[name]["ok"], checks[name])

    def test_validate_self_sourced(self):
        checks = self._checks(validate.validate(self.conn, "o", "r"))
        for name in ("participant_completeness", "no_drift"):
            self.assertTrue(checks[name]["ok"], checks[name])

    def test_write_path_and_auditor_share_the_enumerator(self):
        calls = []
        real = derive.enumerate_participants

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)
        with mock.patch.object(derive, "enumerate_participants", spy):
            conn = graphstore.open_store(":memory:")
            graphstore.init_schema(conn)
            gather.fold_bundle(conn, self._bundle())
            n_fold = len(calls)
            validate._derive_people(self._bundle())
        self.assertEqual(n_fold, 1)
        self.assertEqual(len(calls), 2)


class _IdentityRepo(tch._RepoCase):
    def write_commit(self, msg, date, path, name, email):
        p = os.path.join(self.src, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"# {msg}\n")
        tch._git("add", "-A", cwd=self.src)
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": name,
                                          "GIT_AUTHOR_EMAIL": email}):
            tch._git("commit", "-q", "-m", msg, cwd=self.src, date=date)
        return tch._git("rev-parse", "HEAD", cwd=self.src).strip()

    def _acquire(self, api_commits):
        self.urls = []

        def get_page(url):
            self.urls.append(url)
            if "/commits?" in url:
                return list(api_commits), None
            return [], None
        args = gather.parse_args([
            "--owner", "o", "--repo", "r", "--from", FROM, "--to", TO,
            "--clone-dir", self.clone, "--store", os.path.join(self.tmp, "s.db"),
            "--no-workflows", "--no-releases", "--no-project-board"])
        real_provider = gather.select_code_area_provider
        with mock.patch.object(gather, "_github_remote_url", lambda o, r: self.url), \
                mock.patch.object(gather, "_paginated", lambda token: get_page), \
                mock.patch.object(gather, "select_code_area_provider",
                                  lambda paths, d: real_provider(
                                      paths, d, which=lambda _n: None)), \
                mock.patch.object(gather, "extract_iac_edges", lambda g, d: g):
            return gather.acquire(args, {"GITHUB_TOKEN": "dummy"})


class TestAcquireResolvesCommitAuthors(_IdentityRepo):
    def setUp(self):
        super().setUp()
        quiet(self)
        self.write_commit("base", "2026-03-01", "README.md", "Base", "base@corp.example")
        self.jane = self.write_commit("jane", "2026-05-20", "mod/x/main.tf",
                                      "Jane Doe", "jane@corp.example")
        self.bob = self.write_commit("bob", "2026-05-21", "mod/y/main.tf",
                                     "Bob B", "42+bobb@users.noreply.github.com")
        self.carol = self.write_commit("carol", "2026-05-22", "mod/z/main.tf",
                                       "carol", "carol@corp.example")
        self.bot = self.write_commit("bot", "2026-05-23", "mod/z/main.tf",
                                     "GitHub Actions", "actions@github.com")

    def test_commit_records(self):
        b = self._acquire([{"sha": self.jane, "author": {"login": "janedoe"}},
                           {"sha": self.bob, "author": None}])
        by = {c["sha"]: c for c in b["commits"]}
        self.assertEqual(
            {s: (by[s]["author"], by[s]["author_name"], by[s]["login_resolved"])
             for s in (self.jane, self.bob, self.carol, self.bot)},
            {self.jane: ("janedoe", "Jane Doe", True),
             self.bob: ("bobb", "Bob B", True),
             self.carol: ("carol" + derive.UNRESOLVED_NAME_SUFFIX, "carol", False),
             self.bot: ("GitHub Actions", "GitHub Actions", False)})
        self.assertEqual(by[self.jane]["login_source"], "commits_api")
        self.assertEqual(by[self.bob]["login_source"], "noreply_email")
        for c in b["commits"]:
            self.assertNotIn("author_email", c)

    def test_code_and_symbol_events_carry_the_resolved_author(self):
        b = self._acquire([{"sha": self.jane, "author": {"login": "janedoe"}}])
        evs = [e for e in b["code_events"] + b["symbol_events"]
               if e["commit"] == self.jane]
        self.assertTrue(evs)
        self.assertEqual({e["author"] for e in evs}, {"janedoe"})

    def test_commits_api_is_one_bounded_paginated_listing(self):
        self._acquire([])
        urls = [u for u in self.urls if "/commits?" in u]
        self.assertEqual(len(urls), 1)
        u = urls[0]
        self.assertTrue(u.startswith("https://api.github.com/repos/o/r/commits?"), u)
        head = tch._git("-C", self.clone, "rev-parse", "HEAD").strip()
        self.assertIn("sha=" + head, u)
        self.assertIn("since=2026-05-17T00:00:00Z", u)
        self.assertIn("until=2026-06-01T23:59:59Z", u)
        self.assertIn("per_page=100", u)

    def test_deterministic(self):
        api = [{"sha": self.jane, "author": {"login": "janedoe"}}]
        self.assertEqual(json.dumps(self._acquire(api)["commits"], sort_keys=True),
                         json.dumps(self._acquire(api)["commits"], sort_keys=True))


class TestAcquireNoCommitsNoCall(_IdentityRepo):
    def setUp(self):
        super().setUp()
        quiet(self)

    def test_quiet_window_makes_no_commits_call(self):
        self.write_commit("old", "2026-03-01", "README.md", "Old", "o@corp.example")
        b = self._acquire([])
        self.assertEqual(b["commits"], [])
        self.assertFalse([u for u in self.urls if "/commits?" in u])


if __name__ == "__main__":
    unittest.main()
