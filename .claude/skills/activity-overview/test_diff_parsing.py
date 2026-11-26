"""Regression tests for #69: unified-diff hunk boundaries and git path quoting.

1. `parse_unified_diff` must not treat a removed line starting `-- ` or an added
   line starting `++ ` (rendered `--- ` / `+++ ` inside a hunk) as file headers.
2. Paths git C-quotes (non-ASCII without core.quotepath=false; `"`/`\\`/tab/
   newline always) must reach commits[].files, code_events and the patch walk
   as the same plain path so the (commit, path) diff join hits.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402

SHA = "a" * 40

# The issue's confirmed case: a removed SQL-style comment and an added `++ ` line
# inside a terraform hunk, followed by a second file to prove the next header is
# still parsed.
SQL_HUNK_DIFF = (
    "diff --git a/main.tf b/main.tf\n"
    "index 1111111..2222222 100644\n"
    "--- a/main.tf\n"
    "+++ b/main.tf\n"
    "@@ -1,4 +1,4 @@\n"
    ' resource "azurerm_resource_group" "this" {\n'
    "--- old sql-style note\n"
    "+++ counter\n"
    '   name = "rg"\n'
    " }\n"
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _record(diff, sha=SHA):
    header = gather.FIELD_SEP.join([sha, "p", "Alice", "a@x",
                                    "2026-05-03T12:00:00Z", "subj"])
    return gather.RECORD_SEP + header + "\n" + diff


class TestHunkBoundaries(unittest.TestCase):
    def test_sql_comment_lines_stay_hunk_content(self):
        files = gather.parse_unified_diff(SQL_HUNK_DIFF)
        self.assertEqual([f["path"] for f in files], ["main.tf", "README.md"])
        tf = files[0]
        self.assertEqual(tf["old_path"], "main.tf")
        self.assertEqual(len(tf["hunks"]), 1)
        self.assertIn(("-", "-- old sql-style note"), tf["hunks"][0]["lines"])
        self.assertIn(("+", "++ counter"), tf["hunks"][0]["lines"])
        self.assertEqual(len(tf["hunks"][0]["lines"]), 5)

    def test_sql_comment_hunk_yields_symbol_events_under_main_tf(self):
        events = gather.parse_symbol_events(_record(SQL_HUNK_DIFF))
        self.assertTrue(events)
        self.assertEqual({e["path"] for e in events}, {"main.tf"})
        self.assertIn(("resource", "azurerm_resource_group.this", "change"),
                      {(e["subkind"], e["name"], e["change"]) for e in events})

    def test_sql_comment_hunk_diff_filed_under_main_tf(self):
        diffs = {d["path"]: d["hunk"]
                 for d in gather.parse_file_diffs(_record(SQL_HUNK_DIFF))}
        self.assertEqual(set(diffs), {"main.tf", "README.md"})
        self.assertIn("--- old sql-style note", diffs["main.tf"])
        self.assertIn("+++ counter", diffs["main.tf"])

    def test_header_lookalikes_at_hunk_start_and_end(self):
        # `-- `/`++ ` lines as the FIRST and LAST lines of a hunk, then a second
        # hunk in the same file — counts from the @@ header delimit each hunk.
        diff = ("diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
                "@@ -1,2 +1,2 @@\n"
                "--- first\n"
                "+++ last\n"
                " keep\n"
                "@@ -10 +10,2 @@\n"
                " ctx\n"
                "+++ tail\n")
        f = gather.parse_unified_diff(diff)[0]
        self.assertEqual((f["path"], f["old_path"]), ("q.sql", "q.sql"))
        self.assertEqual([h["new_start"] for h in f["hunks"]], [1, 10])
        self.assertEqual(f["hunks"][0]["lines"],
                         [("-", "-- first"), ("+", "++ last"), (" ", "keep")])
        self.assertEqual(f["hunks"][1]["lines"], [(" ", "ctx"), ("+", "++ tail")])

    def test_no_newline_marker_not_counted_or_kept(self):
        diff = ("diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "\\ No newline at end of file\n"
                "+new\n"
                "\\ No newline at end of file\n")
        f = gather.parse_unified_diff(diff)[0]
        self.assertEqual(f["hunks"][0]["lines"], [("-", "old"), ("+", "new")])

    def test_lines_after_exhausted_hunk_are_not_headers(self):
        # Once a hunk's declared counts are consumed, a `+++ ` line is neither a
        # header nor (silently) hunk content: the path must not be rewritten.
        diff = ("diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
                "@@ -1 +1 @@\n-a\n+b\n+++ bogus\n")
        f = gather.parse_unified_diff(diff)[0]
        self.assertEqual(f["path"], "x.txt")
        self.assertEqual(f["hunks"][0]["lines"], [("-", "a"), ("+", "b")])

    def test_empty_context_line_inside_hunk(self):
        # Some transports strip the single-space prefix of a blank context line.
        diff = ("diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
                "@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n")
        f = gather.parse_unified_diff(diff)[0]
        self.assertEqual(f["hunks"][0]["lines"],
                         [(" ", "a"), (" ", ""), ("-", "b"), ("+", "c")])


class TestUnquoteGitPath(unittest.TestCase):
    def test_plain_path_unchanged(self):
        self.assertEqual(gather.unquote_git_path("docs/with space.md"),
                         "docs/with space.md")

    def test_octal_utf8(self):
        self.assertEqual(gather.unquote_git_path('"docs/caf\\303\\251.md"'),
                         "docs/café.md")

    def test_c_escapes(self):
        self.assertEqual(gather.unquote_git_path('"a\\tb\\n\\"q\\"\\\\z"'),
                         'a\tb\n"q"\\z')


class TestQuotedPathsInParsers(unittest.TestCase):
    def test_patch_headers_quoted_and_space(self):
        diff = ('diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\n'
                '--- "a/docs/caf\\303\\251.md"\n'
                '+++ "b/docs/caf\\303\\251.md"\n'
                "@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/docs/with space.md b/docs/with space.md\n"
                "--- a/docs/with space.md\t\n"
                "+++ b/docs/with space.md\t\n"
                "@@ -1 +1 @@\n-x\n+y\n")
        files = gather.parse_unified_diff(diff)
        self.assertEqual([(f["old_path"], f["path"]) for f in files],
                         [("docs/café.md", "docs/café.md"),
                          ("docs/with space.md", "docs/with space.md")])

    def test_pure_rename_takes_paths_from_rename_lines(self):
        diff = ('diff --git "a/docs/caf\\303\\251.md" "b/docs/na\\303\\257ve.md"\n'
                "similarity index 100%\n"
                'rename from "docs/caf\\303\\251.md"\n'
                'rename to "docs/na\\303\\257ve.md"\n')
        f = gather.parse_unified_diff(diff)[0]
        self.assertEqual((f["old_path"], f["path"]), ("docs/café.md", "docs/naïve.md"))
        self.assertEqual(f["hunks"], [])

    def test_name_status_quoted(self):
        raw = ("\x1e" + SHA + "\x1f\x1fEve\x1fe@x\x1f2026-05-20T12:00:00Z\x1fs\n"
               'M\t"docs/caf\\303\\251.md"\n'
               "A\tdocs/with space.md\n"
               'R097\t"docs/old-na\\303\\257ve.md"\t"docs/na\\303\\257ve.md"\n')
        ev = gather.parse_code_events(raw)
        self.assertEqual([(e["change"], e.get("old_path"), e["path"]) for e in ev],
                         [("modify", None, "docs/café.md"),
                          ("add", None, "docs/with space.md"),
                          ("rename", "docs/old-naïve.md", "docs/naïve.md")])

    def test_name_only_quoted(self):
        raw = ("\x1e" + SHA + "\x1f\x1fEve\x1fe@x\x1f2026-05-20T12:00:00Z\x1fs\n"
               '"docs/caf\\303\\251.md"\ndocs/with space.md\n')
        self.assertEqual(gather.parse_git_log(raw)[0]["files"],
                         ["docs/café.md", "docs/with space.md"])


class TestRunGitQuotepath(unittest.TestCase):
    def _capture(self, args):
        seen = {}

        def fake_run(a, **kw):
            seen["args"] = a
            return subprocess.CompletedProcess(a, 0, stdout="", stderr="")

        with mock.patch.object(gather.subprocess, "run", fake_run):
            gather.run_git(args)
        return seen["args"]

    def test_git_gets_quotepath_false(self):
        self.assertEqual(self._capture(["git", "-C", "d", "log"]),
                         ["git", "-c", "core.quotepath=false", "-C", "d", "log"])

    def test_non_git_commands_untouched(self):
        for cmd in (["bicep", "build", "x.bicep"], ["terraform", "graph"],
                    ["graphify", "update", "d"]):
            self.assertEqual(self._capture(list(cmd)), cmd)


class TestEndToEndRealRepo(unittest.TestCase):
    """A throwaway local repo walked with the exact commands acquire() runs."""

    CAFE = "docs/café.md"
    SPACE = "docs/with space.md"
    QUOTE = 'docs/say "hi".md'
    OLD = "docs/old-naïve.md"
    NEW = "docs/naïve-renamed.md"
    TF = "main.tf"

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "a@example.invalid",
            "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "a@example.invalid",
            "GIT_AUTHOR_DATE": "2026-05-03T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-05-03T12:00:00Z"})
        self._env.start()
        self.addCleanup(self._env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self._git("init", "-q")
        os.makedirs(os.path.join(self.repo, "docs"))
        long_body = "".join("line {}\n".format(i) for i in range(20))
        self._write(self.CAFE, "bonjour\n")
        self._write(self.SPACE, "one\n")
        self._write(self.QUOTE, "q\n")
        self._write(self.OLD, long_body)
        self._write(self.TF, 'resource "azurerm_resource_group" "this" {\n'
                             "-- old sql-style note\n"
                             '  name = "rg"\n'
                             "}\n")
        self._commit("initial")
        self._write(self.CAFE, "bonjour\nau revoir\n")
        self._write(self.SPACE, "two\n")
        self._write(self.QUOTE, "q2\n")
        self._git("mv", self.OLD, self.NEW)
        self._write(self.NEW, long_body + "line 20\n")
        self._write(self.TF, 'resource "azurerm_resource_group" "this" {\n'
                             "++ counter\n"
                             '  name = "rg"\n'
                             "}\n")
        self._commit("second")
        self.head = self._git("rev-parse", "HEAD").strip()

    def _git(self, *args):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", self.repo] + list(args),
            check=True, capture_output=True, text=True).stdout

    def _write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg)

    def _log(self, walk_args):
        return gather.run_git(["git", "-C", self.repo, "log"] + list(walk_args))

    def test_commit_file_lists_unquoted(self):
        commits = gather.parse_git_log(self._log(gather.COMMIT_WALK_ARGS))
        head = next(c for c in commits if c["sha"] == self.head)
        for p in (self.CAFE, self.SPACE, self.QUOTE, self.NEW, self.TF):
            self.assertIn(p, head["files"])
        self.assertFalse([f for f in head["files"] if f.startswith('"')])

    def test_code_events_unquoted_and_rename_detected(self):
        events = [e for e in gather.parse_code_events(self._log(gather.CODE_WALK_ARGS))
                  if e["commit"] == self.head]
        by_path = {e["path"]: e for e in events}
        for p in (self.CAFE, self.SPACE, self.QUOTE, self.TF):
            self.assertEqual(by_path[p]["change"], "modify")
        self.assertEqual(by_path[self.NEW]["change"], "rename")
        self.assertEqual(by_path[self.NEW]["old_path"], self.OLD)

    def test_patch_walk_paths_join_and_symbols(self):
        code_events = [e for e in gather.parse_code_events(
            self._log(gather.CODE_WALK_ARGS)) if e["commit"] == self.head]
        sym, diffs = gather.parse_patch_events(self._log(gather.PATCH_WALK_ARGS))
        head_diffs = {d["path"]: d["hunk"] for d in diffs if d["commit"] == self.head}
        self.assertEqual(set(head_diffs),
                         {self.CAFE, self.SPACE, self.QUOTE, self.NEW, self.TF})
        self.assertIn("--- old sql-style note", head_diffs[self.TF])
        self.assertIn("+++ counter", head_diffs[self.TF])

        gather.attach_file_diffs(code_events, diffs)
        for ev in code_events:
            self.assertTrue(ev.get("hunk"), ev["path"])

        head_sym = [e for e in sym if e["commit"] == self.head]
        self.assertEqual({e["path"] for e in head_sym}, {self.TF})
        self.assertIn(("resource", "azurerm_resource_group.this", "change"),
                      {(e["subkind"], e["name"], e["change"]) for e in head_sym})

    def test_ls_files_unquoted(self):
        self.assertEqual(sorted(gather.git_ls_files(self.repo)),
                         sorted([self.CAFE, self.SPACE, self.QUOTE, self.NEW, self.TF]))


if __name__ == "__main__":
    unittest.main()
