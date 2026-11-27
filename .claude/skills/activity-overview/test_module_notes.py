"""Module change notes gathered from the repository (gather side, issue #97
follow-up: the reader-facing fact layer).

The per-window fact `module_notes` holds what the maintainers wrote about
their modules in the window: the changelog entries its commits ADDED (parsed
generically: the AVM `## <version>` / `### Changes` / `### Breaking Changes`
layout and Keep-a-Changelog style files alike), the `version.json` moves and
the version tags dated in the window. Pure parsers are tested on fixtures;
collect_module_notes through its two seams; the git seams and acquire() on a
throwaway local repository (no network).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402
from test_walked_branch import GitRepoCase, git  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return fh.read()


AVM = fixture("changelog_avm_sample.md")
KAC = fixture("changelog_keepachangelog_sample.md")


def texts(section):
    return [i["text"] for i in section["items"]]


class TestParseChangelog(unittest.TestCase):

    def test_avm_layout(self):
        entries = gather.parse_changelog(AVM)
        self.assertEqual([e["version"] for e in entries], ["0.6.0", "0.5.1"])
        top = entries[0]
        self.assertEqual(top["heading"], "## 0.6.0")
        changes, breaking = top["sections"]
        self.assertEqual((changes["heading"], changes["kind"]), ("Changes", "changed"))
        self.assertEqual(changes["items"], [])
        self.assertEqual(changes["placeholder"], "None")
        self.assertEqual((breaking["heading"], breaking["kind"]),
                         ("Breaking Changes", "breaking"))
        self.assertEqual(texts(breaking), [
            "Deprecated `avm/res/dev-test-lab/lab/secret` as no longer supported "
            "by Resource Provider (https://learn.microsoft.com/en-us/azure/"
            "templates/microsoft.devtestlab)"])
        # the title and the preamble paragraph are not an entry
        self.assertNotIn("Changelog", json.dumps(entries))

    def test_keep_a_changelog_layout(self):
        entries = gather.parse_changelog(KAC)
        self.assertEqual([e["version"] for e in entries],
                         ["Unreleased", "1.2.0", "1.1.0"])
        self.assertEqual(entries[1]["date"], "2026-10-14")
        self.assertNotIn("date", entries[0])
        kinds = [(s["heading"], s["kind"]) for s in entries[1]["sections"]]
        self.assertEqual(kinds, [("Added", "added"), ("Deprecated", "deprecated"),
                                 ("Removed", "removed"), ("Notes", "other")])
        # a continuation line belongs to its bullet
        self.assertEqual(texts(entries[1]["sections"][2]),
                         ["Output `primary_key`\n(read it from the resource instead)."])
        # link reference definitions at the end are not text of 1.1.0
        self.assertEqual(texts(entries[2]["sections"][0]),
                         ["Role assignments are no longer recreated on every plan."])

    def test_other_heading_levels_and_unsectioned_items(self):
        text = ("# 2.0.0\n\n- first\n\n#### Breaking\n\n* gone\n\n"
                "# v1.0.0 (2025-01-02)\n\n1. one\n")
        entries = gather.parse_changelog(text)
        self.assertEqual([e["version"] for e in entries], ["2.0.0", "1.0.0"])
        self.assertEqual(entries[0]["sections"][0]["heading"], None)
        self.assertEqual(entries[0]["sections"][0]["kind"], "unsectioned")
        self.assertEqual(texts(entries[0]["sections"][1]), ["gone"])
        self.assertEqual(entries[1]["date"], "2025-01-02")
        self.assertEqual(texts(entries[1]["sections"][0]), ["one"])

    def test_a_heading_without_a_version_ends_the_entry(self):
        text = "## 1.0.0\n\n- kept\n\n## Contributors\n\n- not an item\n"
        entries = gather.parse_changelog(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(texts(entries[0]["sections"][0]), ["kept"])

    def test_nested_bullets_and_a_fenced_block_after_a_blank_stay_with_the_item(self):
        text = ("## 1.0.0\n\n### Breaking Changes\n\n- Before:\n  - nested\n\n"
                "```bicep\nparam a string\n```\n\n- After\n")
        section = gather.parse_changelog(text)[0]["sections"][0]
        self.assertEqual(texts(section), [
            "Before:\n- nested\n```bicep\nparam a string\n```", "After"])

    def test_placeholders(self):
        for text in ("None", "N/A", "none.", "None (interface unchanged)",
                     "N/A - docs only"):
            self.assertTrue(gather.changelog_placeholder(text), text)
        for text in ("None of the outputs changed type", "Nonexistent file",
                     "Removed x"):
            self.assertFalse(gather.changelog_placeholder(text), text)

    def test_long_item_is_capped_and_says_so(self):
        text = "## 1.0.0\n\n- " + "x" * 1000 + "\n"
        item = gather.parse_changelog(text)[0]["sections"][0]["items"][0]
        self.assertEqual(len(item["text"]), gather.MODULE_NOTE_TEXT_CAP)
        self.assertEqual(item["truncated_from"], 1000)

    def test_items_beyond_the_cap_are_counted(self):
        text = "## 1.0.0\n\n" + "".join("- i{}\n".format(i) for i in range(45))
        section = gather.parse_changelog(text)[0]["sections"][0]
        self.assertEqual(len(section["items"]), gather.MODULE_NOTE_ITEMS_CAP)
        self.assertEqual(section["items_omitted"], 45 - gather.MODULE_NOTE_ITEMS_CAP)

    def test_section_kinds(self):
        cases = {"Breaking Changes": "breaking", "BREAKING": "breaking",
                 "Deprecated": "deprecated", "Removed": "removed",
                 "Security": "security", "Fixed": "fixed", "Bug fixes": "fixed",
                 "Added": "added", "New features": "added", "Changes": "changed",
                 "Changed": "changed", "Updates": "changed", "Notes": "other"}
        for heading, kind in cases.items():
            self.assertEqual(gather.changelog_section_kind(heading), kind, heading)

    def test_empty_and_versionless_text(self):
        self.assertEqual(gather.parse_changelog(""), [])
        self.assertEqual(gather.parse_changelog(None), [])
        self.assertEqual(gather.parse_changelog("# Changelog\n\n- a\n"), [])


class TestChangelogAddedEntries(unittest.TestCase):

    def test_a_new_version_is_added_whole(self):
        before = AVM.replace(AVM[AVM.index("## 0.6.0"):AVM.index("## 0.5.1")], "")
        added = gather.changelog_added_entries(before, AVM)
        self.assertEqual([e["version"] for e in added], ["0.6.0"])
        self.assertNotIn("amended", added[0])
        self.assertEqual(added[0]["sections"][1]["kind"], "breaking")

    def test_unchanged_file_adds_nothing(self):
        self.assertEqual(gather.changelog_added_entries(AVM, AVM), [])

    def test_a_new_file_adds_every_entry(self):
        added = gather.changelog_added_entries(None, AVM)
        self.assertEqual([e["version"] for e in added], ["0.6.0", "0.5.1"])

    def test_an_item_appended_to_an_existing_version_is_an_amendment(self):
        after = KAC.replace("- Output `resource_id`.\n",
                            "- Output `resource_id`.\n- Output `name`.\n")
        added = gather.changelog_added_entries(KAC, after)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["version"], "1.2.0")
        self.assertTrue(added[0]["amended"])
        self.assertEqual([texts(s) for s in added[0]["sections"]], [["Output `name`."]])

    def test_a_version_prefix_v_is_the_same_version(self):
        added = gather.changelog_added_entries("## v1.0.0\n\n- a\n", "## 1.0.0\n\n- a\n")
        self.assertEqual(added, [])

    def test_a_changelog_without_versions_reports_its_new_bullets(self):
        added = gather.changelog_added_entries("# Log\n\n- old\n",
                                               "# Log\n\n- new one\n- old\n")
        self.assertEqual(added, [{"version": None, "heading": None, "sections": [
            {"heading": None, "kind": "unsectioned",
             "items": [{"text": "new one"}]}]}])

    def test_removed_entries_are_not_reported(self):
        self.assertEqual(gather.changelog_added_entries(KAC, "# Changelog\n"), [])


class TestVersionFileAndTags(unittest.TestCase):

    def test_version_file(self):
        self.assertEqual(gather.parse_version_file('{"version": "0.6"}'), "0.6")
        self.assertEqual(gather.parse_version_file('{"version": 1}'), "1")
        for text in (None, "", "not json", '{"v": "1"}', '["0.6"]',
                     '{"version": true}'):
            self.assertIsNone(gather.parse_version_file(text), text)

    RAW = "\n".join("\x1f".join(row) for row in (
        ("avm/res/a/b/0.1.0", "commit", "2026-09-02T10:00:00+02:00", "c1", ""),
        ("avm/res/a/b/0.2.0", "commit", "2026-10-02T10:00:00+02:00", "c2", ""),
        ("avm/res/x/y/0.1.0", "tag", "2026-10-31T23:30:00-01:00", "t3", "c3"),
        ("avm/res/n/m/0.1.0", "tag", "2026-10-05T00:00:00Z", "t4", "c4"),
        ("v1.2.0", "commit", "2026-10-20T12:00:00Z", "c5", ""),
        ("release-candidate", "commit", "2026-10-20T12:00:00Z", "c6", ""),
        ("avm/res/q/r/0.9.0", "commit", "", "c7", ""),
    ))

    def test_window_tags_with_module_version_date_and_first(self):
        got = gather.parse_version_tags(self.RAW, "2026-10-01", "2026-10-31")
        rows = {(t["module"], t["version"]): t for t in got["items"]}
        # 23:30 at -01:00 is 00:30 UTC on Nov 1: outside the window
        self.assertEqual(sorted(rows), [("", "v1.2.0"), ("avm/res/a/b", "0.2.0"),
                                        ("avm/res/n/m", "0.1.0")])
        ab = rows[("avm/res/a/b", "0.2.0")]
        self.assertEqual((ab["date"], ab["date_source"], ab["commit"], ab["first"]),
                         ("2026-10-02T08:00:00Z", "commit", "c2", False))
        nm = rows[("avm/res/n/m", "0.1.0")]
        self.assertEqual((nm["date_source"], nm["commit"], nm["first"]),
                         ("tagger", "c4", True))
        self.assertEqual(got["undated"], 1)
        self.assertEqual([t["tag"] for t in got["items"]],
                         ["avm/res/a/b/0.2.0", "avm/res/n/m/0.1.0", "v1.2.0"])


class FakeRepo:
    """read_blob / list_tags seams over a dict {(rev, path): text}."""

    def __init__(self, blobs, tags="", broken=()):
        self.blobs, self.tags, self.broken = blobs, tags, set(broken)
        self.reads = []

    def read_blob(self, rev, path):
        self.reads.append((rev, path))
        if (rev, path) in self.broken:
            raise gather.BlobUnreadable("fatal: unable to read")
        return self.blobs.get((rev, path))

    def list_tags(self):
        if self.tags is None:
            raise RuntimeError("command failed (exit 128): git for-each-ref\nboom")
        return self.tags


def ev(change, path, commit="s1", date="2026-10-03", old=None):
    e = {"commit": commit, "date": date, "change": change, "path": path,
         "author": "a"}
    if old:
        e["old_path"] = old
    return e


class TestCollectModuleNotes(unittest.TestCase):
    CL = "avm/res/dev-test-lab/lab/CHANGELOG.md"
    VF = "avm/res/dev-test-lab/lab/version.json"

    def test_changelog_and_version_of_one_commit(self):
        before = AVM.replace(AVM[AVM.index("## 0.6.0"):AVM.index("## 0.5.1")], "")
        repo = FakeRepo({("s1", self.CL): AVM, ("s1^", self.CL): before,
                         ("s1", self.VF): '{"version": "0.6"}',
                         ("s1^", self.VF): '{"version": "0.5"}'})
        notes = gather.collect_module_notes(
            [ev("modify", self.CL), ev("modify", self.VF),
             ev("modify", "avm/res/dev-test-lab/lab/main.bicep")],
            repo.read_blob, repo.list_tags, "2026-10-01", "2026-10-31")
        self.assertEqual(notes["collected"], True)
        (cl,) = notes["changelogs"]
        self.assertEqual({k: cl[k] for k in ("path", "module", "commit", "date",
                                             "change")},
                         {"path": self.CL, "module": "avm/res/dev-test-lab/lab",
                          "commit": "s1", "date": "2026-10-03", "change": "modify"})
        self.assertNotIn("new_file", cl)
        self.assertEqual([e["version"] for e in cl["entries"]], ["0.6.0"])
        self.assertEqual(notes["versions"], [
            {"path": self.VF, "module": "avm/res/dev-test-lab/lab", "commit": "s1",
             "date": "2026-10-03", "change": "modify", "from": "0.5", "to": "0.6"}])
        self.assertEqual(notes["tags"],
                         {"collected": False, "reason": "the clone holds no tags"})
        self.assertNotIn("unread", notes)
        # only the changelog / version files were read, never main.bicep
        self.assertEqual({p for _r, p in repo.reads}, {self.CL, self.VF})

    def test_a_rename_reads_the_parent_at_the_old_path(self):
        old = "avm/res/x/y/consumergroup/CHANGELOG.md"
        new = "avm/res/x/y/endpoint/consumergroup/CHANGELOG.md"
        repo = FakeRepo({("s1", new): AVM, ("s1^", old): AVM})
        notes = gather.collect_module_notes([ev("rename", new, old=old)],
                                            repo.read_blob, None, "2026-10-01",
                                            "2026-10-31")
        self.assertEqual(notes["changelogs"], [])
        self.assertIn(("s1^", old), repo.reads)

    def test_an_added_changelog_is_a_new_file(self):
        repo = FakeRepo({("s1", self.CL): AVM})
        notes = gather.collect_module_notes([ev("add", self.CL)], repo.read_blob,
                                            repo.list_tags, "2026-10-01", "2026-10-31")
        self.assertTrue(notes["changelogs"][0]["new_file"])
        self.assertEqual(len(notes["changelogs"][0]["entries"]), 2)
        self.assertNotIn(("s1^", self.CL), repo.reads)

    def test_unreadable_and_missing_versions_are_gaps(self):
        repo = FakeRepo({}, broken={("s1", self.CL)})
        notes = gather.collect_module_notes(
            [ev("modify", self.CL), ev("modify", self.VF, commit="s2")],
            repo.read_blob, repo.list_tags, "2026-10-01", "2026-10-31")
        self.assertEqual(notes["changelogs"], [])
        self.assertEqual(notes["unread"], [
            {"path": self.CL, "commit": "s1", "date": "2026-10-03",
             "reason": "fatal: unable to read"},
            {"path": self.VF, "commit": "s2", "date": "2026-10-03",
             "reason": "the file is not in the commit"}])

    def test_deleted_files_and_other_files_are_not_read(self):
        repo = FakeRepo({})
        notes = gather.collect_module_notes(
            [ev("delete", self.CL), ev("modify", "README.md")],
            repo.read_blob, repo.list_tags, "2026-10-01", "2026-10-31")
        self.assertEqual(repo.reads, [])
        self.assertEqual((notes["changelogs"], notes["versions"]), ([], []))

    def test_changelog_names_are_matched_case_insensitively(self):
        for name in ("CHANGELOG.md", "changelog.md", "CHANGES.md", "HISTORY.md"):
            self.assertEqual(gather._note_file_kind("m/" + name), "changelog")
        self.assertEqual(gather._note_file_kind("m/version.json"), "version")
        self.assertIsNone(gather._note_file_kind("m/README.md"))

    def test_entries_beyond_the_cap_are_counted(self):
        text = "".join("## 1.{}.0\n\n- c\n\n".format(i) for i in range(15))
        repo = FakeRepo({("s1", self.CL): text})
        cl = gather.collect_module_notes([ev("add", self.CL)], repo.read_blob, None,
                                         "2026-10-01", "2026-10-31")["changelogs"][0]
        self.assertEqual(len(cl["entries"]), gather.MODULE_NOTE_ENTRIES_CAP)
        self.assertEqual(cl["entries_omitted"], 15 - gather.MODULE_NOTE_ENTRIES_CAP)

    def test_tags(self):
        repo = FakeRepo({}, tags=TestVersionFileAndTags.RAW)
        notes = gather.collect_module_notes([], repo.read_blob, repo.list_tags,
                                            "2026-10-01", "2026-10-31")
        self.assertEqual(notes["tags"]["collected"], True)
        self.assertEqual(notes["tags"]["source"], "clone")
        self.assertEqual(len(notes["tags"]["items"]), 3)
        self.assertEqual(notes["tags"]["undated"], 1)
        failing = FakeRepo({}, tags=None)
        notes = gather.collect_module_notes([], failing.read_blob, failing.list_tags,
                                            "2026-10-01", "2026-10-31")
        self.assertEqual(notes["tags"], {"collected": False,
                                         "reason": "listing tags failed: command "
                                         "failed (exit 128): git for-each-ref"})


class TestGitSeams(GitRepoCase):
    """The production seams and acquire() on a real local repository."""

    def _repo(self):
        self.commit({"m/a/main.bicep": "param a string\n",
                     "m/a/CHANGELOG.md": "# Changelog\n\n## 0.1.0\n\n- Initial\n",
                     "m/a/version.json": '{"version": "0.1"}'}, "2026-03-01")
        git("tag", "m/a/0.1.0", cwd=self.src)
        self.c1 = self.commit({
            "m/a/CHANGELOG.md": "# Changelog\n\n## 0.2.0\n\n### Breaking Changes"
                                "\n\n- Removed `a`\n\n## 0.1.0\n\n- Initial\n",
            "m/a/version.json": '{"version": "0.2"}',
            "m/a/main.bicep": "param b string\n"}, "2026-05-05",
            msg="feat: drop a (#12)")
        git("tag", "-a", "-m", "release", "m/a/0.2.0", cwd=self.src,
            date="2026-05-06")
        git("mv", "m/a", "m/b", cwd=self.src)
        git("commit", "-q", "-m", "move (#13)", cwd=self.src, date="2026-05-07")

    def test_blob_reader_and_tag_lister(self):
        self._repo()
        read = gather.git_blob_reader(self.src)
        self.assertIn("0.2.0", read(self.c1, "m/a/CHANGELOG.md"))
        self.assertNotIn("0.2.0", read(self.c1 + "^", "m/a/CHANGELOG.md"))
        self.assertIsNone(read(self.c1, "m/a/nothing.md"))
        root = self.rev("HEAD~2")
        self.assertIsNone(read(root + "^", "m/a/CHANGELOG.md"))   # root commit
        tags = gather.parse_version_tags(gather.git_tag_lister(self.src)(),
                                         "2026-05-01", "2026-05-31")["items"]
        self.assertEqual([(t["tag"], t["date_source"], t["first"]) for t in tags],
                         [("m/a/0.2.0", "tagger", False)])
        self.assertEqual(tags[0]["commit"], self.c1)

    def test_acquire_records_module_notes_and_fold_stores_them(self):
        self._repo()
        bundle, err = self.acquire("2026-05-01", "2026-05-31")
        notes = bundle["module_notes"]
        self.assertIn("module notes: o/r: 1 changelog update(s), 1 version "
                      "change(s), 1 version tag(s) in the window", err)
        (cl,) = notes["changelogs"]
        self.assertEqual((cl["path"], cl["commit"], cl["date"]),
                         ("m/a/CHANGELOG.md", self.c1, "2026-05-05"))
        self.assertEqual(cl["entries"][0]["version"], "0.2.0")
        self.assertEqual(cl["entries"][0]["sections"][0]["items"],
                         [{"text": "Removed `a`"}])
        self.assertEqual([(v["from"], v["to"]) for v in notes["versions"]],
                         [("0.1", "0.2")])
        # the move (#13) renamed the changelog: nothing was added by it
        self.assertEqual(len(notes["changelogs"]), 1)
        self.assertEqual([t["tag"] for t in notes["tags"]["items"]], ["m/a/0.2.0"])
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, bundle)
        got = graphstore.get_window_facts(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(got["facts"]["module_notes"], notes)
        view = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                               warn=lambda m: None)
        self.assertEqual(view["module_notes"], notes)
        report = validate.validate(conn, project="o", repo="r")
        self.assertEqual([c["name"] for c in report.checks if not c["ok"]], [])

    def test_no_clone_no_module_notes(self):
        self._repo()
        bundle, _err = self.acquire("2026-05-01", "2026-05-31", "--no-clone")
        self.assertNotIn("module_notes", bundle)


def _bundle(frm, to, notes):
    return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                     "period": {"from": frm, "to": to}},
            "prs": [], "issues": [], "commits": [], "module_notes": notes}


def _notes(*items, tags=None):
    return {"collected": True, "changelogs": [dict(i) for i in items],
            "versions": [], "tags": tags or {"collected": False,
                                             "reason": "the clone holds no tags"}}


class TestWindowScope(unittest.TestCase):
    """A view of exactly a gathered window reads its fact verbatim; any other
    view reads the dated items of the overlapping windows that fall in it."""

    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        sep = _notes({"path": "m/CHANGELOG.md", "commit": "a", "date": "2026-09-10",
                      "entries": []},
                     tags={"collected": True, "source": "clone", "items": [
                         {"tag": "m/0.1.0", "date": "2026-09-11T00:00:00Z"}]})
        octo = _notes({"path": "m/CHANGELOG.md", "commit": "b", "date": "2026-10-03",
                       "entries": []},
                      {"path": "n/CHANGELOG.md", "commit": "c", "date": "2026-10-20",
                       "entries": []})
        self.sep, self.oct = sep, octo
        gather.fold_bundle(self.conn, _bundle("2026-09-01", "2026-09-30", sep),
                           project="o", repo="r")
        gather.fold_bundle(self.conn, _bundle("2026-10-01", "2026-10-31", octo),
                           project="o", repo="r")

    def view(self, frm, to):
        return extract.extract(self.conn, "o", "r", frm, to, warn=lambda m: None)

    def test_exact_window_is_verbatim(self):
        self.assertEqual(self.view("2026-10-01", "2026-10-31")["module_notes"],
                         self.oct)

    def test_a_roll_up_unions_the_months(self):
        notes = self.view("2026-09-01", "2026-10-31")["module_notes"]
        self.assertEqual([c["commit"] for c in notes["changelogs"]], ["a", "b", "c"])
        self.assertEqual(notes["tags"]["items"], self.sep["tags"]["items"])
        self.assertEqual(notes["tags"]["windows_without_tags"],
                         [{"from": "2026-10-01", "to": "2026-10-31"}])
        self.assertEqual(notes["windows"], [{"from": "2026-09-01", "to": "2026-09-30"},
                                            {"from": "2026-10-01", "to": "2026-10-31"}])
        self.assertTrue(notes["covers_view"])

    def test_a_sub_window_keeps_its_own_days(self):
        notes = self.view("2026-10-01", "2026-10-10")["module_notes"]
        self.assertEqual([c["commit"] for c in notes["changelogs"]], ["b"])
        self.assertEqual(notes["tags"]["collected"], False)

    def test_a_view_with_no_notes_has_no_key(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = _bundle("2026-10-01", "2026-10-31", None)
        del b["module_notes"]
        gather.fold_bundle(conn, b, project="o", repo="r")
        self.assertNotIn("module_notes", extract.extract(
            conn, "o", "r", "2026-10-01", "2026-10-31", warn=lambda m: None))

    def test_validate_accepts_the_fact_and_flags_a_swapped_value(self):
        # (this store holds no records, so only the meta check is of interest)
        report = validate.validate(self.conn, project="o", repo="r")
        meta = [c for c in report.checks if c["name"] == "meta_integrity"]
        self.assertTrue(meta and meta[0]["ok"], meta)
        self.assertIsNone(validate._fact_shape_problem("module_notes", self.oct))
        self.assertIn("swapped", validate._fact_shape_problem(
            "code_graph", self.oct))


if __name__ == "__main__":
    unittest.main()
