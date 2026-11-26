"""Issue #54 — the qualified-id grammar with a second `#`.

Symbol/comment artifact ids are `{project}/{repo}#{path}#{lang}:{subkind}:{name}`
and a path or a comment name may itself contain `#`. Every reader must recover
the FULL local id (everything after the first `#`) and tell a symbol id from a
file path containing `#` by the `lang:subkind:name` tail shape.
"""

import unittest

import extract
import gather
import graphstore
import spotlight
import validate

SHA = "a" * 40


def _store(bundle):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, bundle)
    return conn


def _base(**extra):
    b = {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                 "to": "2026-05-31"},
        "commits": [{"sha": SHA, "author": "alice", "date": "2026-05-02",
                     "message": "refactor (#1)",
                     "files": ["a/main.tf", "b/main.tf"]}],
        "prs": [{"number": 1, "author": "alice", "merged": True,
                 "merged_at": "2026-05-03",
                 "url": "https://github.com/o/r/pull/1"}],
        "issues": [],
    }
    b.update(extra)
    return b


def _sym(path, subkind, name, change, lang="terraform"):
    return {"commit": SHA, "author": "alice", "date": "2026-05-02",
            "path": path, "lang": lang, "subkind": subkind, "name": name,
            "change": change, "before": None, "after": None}


def _move_bundle():
    return _base(symbol_events=[
        _sym("a/main.tf", "resource", "azurerm_x.this", "drop"),
        _sym("b/main.tf", "resource", "azurerm_x.this", "add"),
    ])


def _check(report, name):
    for c in report.checks:
        if c["name"] == name:
            return c
    raise AssertionError(name)


class ParseIdGrammar(unittest.TestCase):
    def test_parse_id_splits_scope_on_first_hash(self):
        p = graphstore.parse_id("P/o/r#src/a.py#python:function:f")
        self.assertEqual(p, {"scope": "P/o/r", "local": "src/a.py#python:function:f"})

    def test_parse_id_single_hash_unchanged(self):
        self.assertEqual(graphstore.parse_id("o/r#pr-5"),
                         {"scope": "o/r", "local": "pr-5"})
        self.assertEqual(graphstore.parse_id("avm#person-bob"),
                         {"scope": "avm", "local": "person-bob"})

    def test_local_id_comment_name_with_hashes(self):
        qid = "o/r#main.tf#terraform:comment:#### networking ####"
        self.assertEqual(graphstore.local_id(qid),
                         "main.tf#terraform:comment:#### networking ####")

    def test_local_id_strips_known_prefix(self):
        self.assertEqual(
            graphstore.local_id("P/o/r#docs/C#-guide.md", "P", "o/r"),
            "docs/C#-guide.md")

    def test_split_symbol_local_forms(self):
        s = graphstore.split_symbol_local
        self.assertEqual(s("src/a.py#python:function:f"),
                         ("src/a.py", "python", "function", "f"))
        # `#` in the path and in the name
        self.assertEqual(s("C#/main.tf#terraform:comment:# net: x ##"),
                         ("C#/main.tf", "terraform", "comment", "# net: x ##"))
        # name with `:` kept whole; empty lang/name allowed (fold writes `lang or ""`)
        self.assertEqual(s("m.bicep#bicep:comment:TODO: a:b"),
                         ("m.bicep", "bicep", "comment", "TODO: a:b"))
        self.assertEqual(s("x.tf#:resource:"), ("x.tf", "", "resource", ""))
        # not symbols: file paths with `#`, file artifacts, shas, prefixed ids
        for local in ("docs/C#-guide.md", "art:docs/C#-guide.md", SHA,
                      "pr-5", "review-5-99", "a#b/c.md", "issue#x"):
            self.assertIsNone(s(local), local)


PREFIXED_PATH_SYMBOL = "o/r#issue-templates/main.tf#terraform:resource:azurerm_z.this"


class PrefixLikePathIsStillArtifact(unittest.TestCase):
    """A symbol whose PATH starts with a structural prefix (`issue-`, `release-`)
    is still an artifact — the artifact form is tested before the prefixes."""

    def test_validate_kind(self):
        self.assertEqual(validate._id_kind(None, PREFIXED_PATH_SYMBOL, {}), "artifact")

    def test_classify_id(self):
        self.assertEqual(gather.classify_id(PREFIXED_PATH_SYMBOL)["node_class"], "code")

    def test_spotlight_kind(self):
        self.assertEqual(spotlight._node_kind(PREFIXED_PATH_SYMBOL, {}), "symbol")


class ValidateIdGrammar(unittest.TestCase):
    def test_symbol_move_store_validates_ok(self):
        conn = _store(_move_bundle())
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])

    def test_symbol_under_person_prefixed_path_is_an_artifact_not_a_person(self):
        # A symbol whose PATH starts with `person-` (first-`#` parsing makes its
        # local id start with `person-`) must stay an artifact: no orphan-person
        # report, and its move edges conform.
        conn = _store(_base(symbol_events=[
            _sym("person-data/main.tf", "resource", "azurerm_x.this", "drop"),
            _sym("b/main.tf", "resource", "azurerm_x.this", "add"),
        ]))
        qid = "o/r#person-data/main.tf#terraform:resource:azurerm_x.this"
        self.assertFalse(validate._is_person_id(qid))
        self.assertFalse(validate._is_person_id("o/r#art:person-notes.md"))
        self.assertTrue(validate._is_person_id("o#person-alice"))
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])

    def test_symbol_move_endpoints_classified_artifact(self):
        conn = _store(_move_bundle())
        kind = validate._id_kind(
            conn, "o/r#a/main.tf#terraform:resource:azurerm_x.this", {})
        self.assertEqual(kind, "artifact")

    def test_comment_named_with_hashes_passes_no_fabrication(self):
        conn = _store(_base(symbol_events=[
            _sym("main.tf", "comment", "#### networking ####", "add"),
            _sym("main.tf", "comment", "# ends with #", "add"),
        ]))
        c = validate.check_no_fabrication(conn)
        self.assertTrue(c["ok"], c["details"])
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])

    def test_symbol_artifact_without_history_is_caught(self):
        conn = _store(_move_bundle())
        bad = "o/r#c/main.tf#terraform:resource:ghost"
        graphstore.upsert_node(conn, bad, "o", "r", "code", None,
                               {"kind": "symbol", "lifecycle": []})
        conn.commit()
        c = validate.check_no_fabrication(conn)
        self.assertFalse(c["ok"])
        self.assertTrue(any(d.get("id") == bad and "lifecycle" in d.get("problem", "")
                            for d in c["details"]), c["details"])

    def test_file_artifact_with_hash_without_history_is_caught(self):
        conn = _store(_move_bundle())
        bad = "o/r#art:docs/C#-guide.md"
        graphstore.upsert_node(conn, bad, "o", "r", "code", None, {"lifecycle": []})
        conn.commit()
        c = validate.check_no_fabrication(conn)
        self.assertTrue(any(d.get("id") == bad for d in c["details"]), c["details"])

    def test_path_with_hash_store_validates_ok(self):
        conn = _store(_base(code_events=[
            {"commit": SHA, "author": "alice", "date": "2026-05-02",
             "change": "add", "path": "docs/C#-guide.md"}]))
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])


class ClassifyIdGrammar(unittest.TestCase):
    def _c(self, qid):
        return gather.classify_id(qid)

    def test_review_and_event_are_social(self):
        for qid, local in (("P/o/r#review-5-99", "review-5-99"),
                           ("P/o/r#event-pr-5-1", "event-pr-5-1")):
            info = self._c(qid)
            self.assertEqual(info["node_class"], "social", qid)
            self.assertEqual(info["local"], local)

    def test_file_artifact_is_code(self):
        info = self._c("P/o/r#art:README.md")
        self.assertEqual(info["node_class"], "code")
        self.assertEqual(info["local"], "art:README.md")
        self.assertNotEqual(info["kind"], "code")  # never git-fetched as a sha

    def test_symbol_artifact_is_code_with_full_local(self):
        info = self._c("P/o/r#src/a.py#python:function:f")
        self.assertEqual(info["node_class"], "code")
        self.assertEqual(info["local"], "src/a.py#python:function:f")
        self.assertNotEqual(info["kind"], "code")

    def test_existing_forms_unchanged(self):
        self.assertEqual(self._c("o/r#pr-5")["kind"], "social")
        self.assertEqual(self._c("o/r#issue-5")["kind"], "social")
        self.assertEqual(self._c("o/r#comment-9")["kind"], "social")
        self.assertEqual(self._c("o/r#" + SHA)["kind"], "code")
        self.assertEqual(self._c("o/r#milestone-1")["kind"], "structure")
        self.assertEqual(self._c("o/r#codegraph")["kind"], "structure")
        self.assertEqual(self._c("o#person-bob")["kind"], "structure")

    def test_backfill_never_writes_repo_with_hash(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        seen = []

        def fetch(kind, local, qid):
            seen.append((kind, local))
            return {"node": {"title": "f", "author": "a"}, "edges": []}

        # backfill only writes PR / issue records (#113): an artifact or a
        # review id is never fetched, so it can never land under a bad repo.
        for qid in ("P/o/r#src/a.py#python:function:f", "P/o/r#art:C#.md",
                    "P/o/r#review-5-99"):
            gather.backfill(conn, qid, fetch=fetch)
            self.assertIsNone(graphstore.get_node(conn, qid), qid)
        for qid in ("P/o/r#pr-5", "P/o/r#issue-6"):
            gather.backfill(conn, qid, fetch=fetch)
            node = graphstore.get_node(conn, qid)
            self.assertEqual((node["project"], node["repo"]), ("P", "o/r"), qid)
        self.assertNotIn("#", "".join(
            r[0] for r in conn.execute("SELECT repo FROM nodes")))
        self.assertEqual(seen, [("social", "pr-5"), ("social", "issue-6")])


class ExtractLedgerGrammar(unittest.TestCase):
    def test_path_with_hash_round_trips_code_events(self):
        ev = {"commit": SHA, "author": "alice", "date": "2026-05-02",
              "change": "add", "path": "docs/C#-guide.md"}
        conn = _store(_base(code_events=[ev]))
        out = extract._materialize_code_events(conn, "o", "r")
        self.assertEqual([e["path"] for e in out], ["docs/C#-guide.md"])
        self.assertEqual(extract._materialize_symbol_events(conn, "o", "r"), [])
        view = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual([e["path"] for e in view["code_events"]],
                         ["docs/C#-guide.md"])

    def test_symbol_with_hash_in_path_and_name_round_trips(self):
        sym = _sym("C#/main.tf", "comment", "# net: x ##", "add")
        conn = _store(_base(symbol_events=[sym]))
        out = extract._materialize_symbol_events(conn, "o", "r")
        self.assertEqual(len(out), 1)
        got = out[0]
        self.assertEqual((got["path"], got["lang"], got["subkind"], got["name"]),
                         ("C#/main.tf", "terraform", "comment", "# net: x ##"))
        self.assertEqual(extract._materialize_code_events(conn, "o", "r"), [])


class SpotlightKindGrammar(unittest.TestCase):
    def test_symbol_row_kind(self):
        self.assertEqual(spotlight._node_kind(
            "o/r#a/main.tf#terraform:resource:azurerm_x.this", {}), "symbol")
        self.assertEqual(spotlight._node_kind(
            "o/r#main.tf#terraform:comment:#### networking ####", {}), "symbol")

    def test_file_with_hash_is_file(self):
        self.assertEqual(spotlight._node_kind("o/r#art:docs/C#-guide.md", {}), "file")

    def test_commit_kind(self):
        self.assertEqual(spotlight._node_kind("o/r#" + SHA, {"sha": SHA}), "commit")

    def test_normalize_local_symbol_id_resolves(self):
        conn = _store(_move_bundle())
        local = "b/main.tf#terraform:resource:azurerm_x.this"
        self.assertEqual(spotlight._normalize_artifact_id(conn, "o", local),
                         "o/r#" + local)
        self.assertEqual(spotlight._normalize_artifact_id(conn, "o", "o/r#" + local),
                         "o/r#" + local)

    def test_normalize_local_path_starting_with_project_name(self):
        # project "o" and a local path `o/...`: must not be mistaken for qualified
        conn = _store(_base(symbol_events=[
            _sym("o/main.tf", "resource", "azurerm_y.this", "add")]))
        local = "o/main.tf#terraform:resource:azurerm_y.this"
        self.assertEqual(spotlight._normalize_artifact_id(conn, "o", local),
                         "o/r#" + local)


COLON_PATH = "notes#a:b:c.md"   # a file path with the symbol-tail shape
COLON_MILESTONE = "o/r#milestone-v1#a:b:c"


def _colon_path_bundle():
    b = _base(code_events=[{"commit": SHA, "author": "alice", "date": "2026-05-02",
                            "change": "add", "path": COLON_PATH}])
    b["commits"][0]["files"] = ["a/main.tf", "b/main.tf", COLON_PATH]
    return b


class StoredNodeClassification(unittest.TestCase):
    """#101 item 3: a stored node / ledger row is classified by its class and
    record (a ledger row by its commit's stored file list); the id shape is the
    fallback for dangling ids only."""

    def test_ledger_file_row_named_by_its_commit_is_a_file_row(self):
        conn = _store(_colon_path_bundle())
        self.assertEqual([e["path"] for e in
                          extract._materialize_code_events(conn, "o", "r")],
                         [COLON_PATH])
        self.assertEqual(extract._materialize_symbol_events(conn, "o", "r"), [])
        # the artifact substrate holds the file artifact, not a phantom symbol
        self.assertIsNotNone(graphstore.get_node(conn, "o/r#art:" + COLON_PATH))
        self.assertIsNone(graphstore.get_node(conn, "o/r#" + COLON_PATH))
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])

    def test_ledger_row_without_commit_file_list_falls_back_to_shape(self):
        self.assertTrue(graphstore.ledger_row_is_symbol(COLON_PATH, None))
        self.assertTrue(graphstore.ledger_row_is_symbol(COLON_PATH, frozenset()))
        self.assertFalse(graphstore.ledger_row_is_symbol(
            COLON_PATH, frozenset([COLON_PATH])))
        # a real symbol row: its commit lists the PATH, never the symbol id
        self.assertTrue(graphstore.ledger_row_is_symbol(
            "a/main.tf#terraform:resource:x", frozenset(["a/main.tf"])))

    def test_spotlight_symbols_authored_skips_file_row(self):
        conn = _store(_colon_path_bundle())
        out = spotlight.person_impact(conn, "o", "alice")
        self.assertIn("symbols_authored", out)
        self.assertNotIn("o/r#" + COLON_PATH,
                         [s["id"] for s in out["symbols_authored"]])

    def test_stored_milestone_with_symbol_shaped_title_is_a_milestone(self):
        conn = _store(_move_bundle())
        graphstore.upsert_node(conn, COLON_MILESTONE, "o", "r", "structure", None,
                               {"title": "v1#a:b:c"})
        conn.commit()
        self.assertEqual(validate._id_kind(conn, COLON_MILESTONE, {}), "milestone")
        self.assertEqual(spotlight._node_kind(COLON_MILESTONE, {"title": "v1#a:b:c"},
                                              "structure"), "milestone")
        # dangling: only the id shape is known
        self.assertEqual(validate._id_kind(None, COLON_MILESTONE, {}), "artifact")

    def test_stored_artifact_kinds_come_from_the_record(self):
        conn = _store(_move_bundle())
        sym = graphstore.get_node(conn, "o/r#b/main.tf#terraform:resource:azurerm_x.this")
        self.assertTrue(graphstore.is_symbol_node(sym))
        self.assertEqual(spotlight._node_kind(sym["id"], sym["data"], "code"), "symbol")
        commit = graphstore.get_node(conn, "o/r#" + SHA)
        self.assertFalse(graphstore.is_artifact_node(commit))
        self.assertEqual(validate._id_kind(conn, commit["id"], {}), "commit")


class CodeNodeForms(unittest.TestCase):
    """#101 item 4: every stored code node is a commit (sha id + commit record)
    or an artifact (`art:` id + file record, or symbol-tail id + symbol/comment
    record); no_drift audits every non-commit code node."""

    def _fab(self, conn):
        return validate.check_no_fabrication(conn)

    def test_real_stores_have_only_known_code_node_forms(self):
        for bundle in (_move_bundle(), _colon_path_bundle()):
            c = self._fab(_store(bundle))
            self.assertTrue(c["ok"], c["details"])

    def _assert_form_error(self, nid, data):
        conn = _store(_move_bundle())
        graphstore.upsert_node(conn, nid, "o", "r", "code", "2026-05-02", data)
        conn.commit()
        c = self._fab(conn)
        self.assertFalse(c["ok"])
        self.assertTrue(any(d.get("id") == nid
                            and "code-node form" in d.get("problem", "")
                            for d in c["details"]), c["details"])

    def test_commit_record_under_artifact_id_is_an_error(self):
        self._assert_form_error("o/r#art:x.md", {"sha": SHA, "files": []})

    def test_artifact_record_under_sha_id_is_an_error(self):
        self._assert_form_error("o/r#" + "b" * 40,
                                {"kind": "doc", "path": "x.md", "lifecycle": [
                                    {"event": "add", "commit": SHA}]})

    def test_symbol_kind_under_file_artifact_id_is_an_error(self):
        self._assert_form_error("o/r#art:x.md", {"kind": "symbol", "lifecycle": [
            {"event": "add", "commit": SHA}]})

    def test_file_kind_under_symbol_id_is_an_error(self):
        self._assert_form_error("o/r#x.md#md:h:y", {"kind": "doc", "lifecycle": [
            {"event": "add", "commit": SHA}]})

    def test_no_drift_audits_every_non_commit_code_node(self):
        conn = _store(_move_bundle())
        nid = "o/r#" + "c" * 40
        graphstore.upsert_node(conn, nid, "o", "r", "code", "2026-05-02",
                               {"kind": "doc", "path": "x.md", "lifecycle": []})
        conn.commit()
        c = validate.check_no_drift(conn, "o", "r", {"meta": {"owner": "o",
                                                              "repo": "r"}},
                                    check_people=False)
        self.assertTrue(any(d.get("id") == "c" * 40
                            and d.get("problem") == "stored but not derivable"
                            for d in c["details"]), c["details"])


if __name__ == "__main__":
    unittest.main()
