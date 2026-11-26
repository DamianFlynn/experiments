"""Trust-check strictness (issue #89).

Each mutation of a clean store that the trust check used to pass is now caught
on the check that owns the invariant it breaks:

- edge_set: the stored edges of a repo equal the edges its stored records imply
  (re-derived by folding the store's own self-sourced bundle into an empty
  scratch store), in BOTH directions; an extra edge is an ERROR.
- schema_conformance: every edge type has an explicit endpoint rule; an unknown
  type, or an endpoint outside the rule, is an ERROR.
- provenance: a PR/issue/review/event url points at github.com/{owner}/{repo}
  and the node's own number; a commit sha is 40 (or 64) hex.
- no_fabrication: ids follow the grammar, agree with the row's project/repo
  columns, and no repo is stored under two case spellings.
- no_drift: people are re-derived from the records alone (not seeded from the
  stored person nodes), compare `login_resolved`, and with --bundle the stored
  records must equal the bundle's.

What the store alone cannot show (a record deleted with all its edges, a blob
rewritten consistently) is caught when the original bundle is passed.
Plus the four known gaps: idempotency on multi-window stores, single-member
audits of a multi-member project, pre-#78 people and what no_drift proves.
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
C1 = "c1" * 20
C4 = "c4" * 20


def _bundle(name="bundle_p3b.json"):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def _store(*bundles, **kw):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b), **kw)
    return conn


def _p3b():
    return _store(_bundle())


def _set_data(conn, qid, **changes):
    n = graphstore.get_node(conn, qid)
    graphstore.upsert_node(conn, qid, n["project"], n["repo"], n["node_class"],
                           n["ts"], dict(n["data"], **changes))


def _check(report, name):
    for c in report.checks:
        if c["name"] == name:
            return c
    raise AssertionError("no check {} in {}".format(
        name, [c["name"] for c in report.checks]))


def _failing(report):
    return sorted(c["name"] for c in report.checks if not c["ok"])


def _report(conn, **kw):
    return validate.validate(conn, project="o", repo="r", **kw)


class _Case(unittest.TestCase):
    def assertFailsOn(self, conn, name, needle, **kw):
        report = _report(conn, **kw)
        c = _check(report, name)
        self.assertIs(c["ok"], False, json.dumps(c, indent=1, default=str))
        self.assertIn(needle, json.dumps(c["details"], default=str))
        self.assertIs(report.ok, False)
        return c

    def assertClean(self, conn, project="o", repo="r", **kw):
        report = validate.validate(conn, project=project, repo=repo, **kw)
        bad = [c for c in report.checks if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))
        return report


# --- clean stores still pass ---------------------------------------------------

def _sample_as_gathered():
    """fixtures/bundle_sample.json is a minimal link/render fixture: its commits
    are `a`/`b`/`c` with no date and its PR/issue carry no timestamp, which no
    gather produces. The same content with real-shaped shas and dates."""
    b = _bundle("bundle_sample.json")
    shas = {c["sha"]: c["sha"] * 40 for c in b["commits"]}
    for c in b["commits"]:
        c["sha"] = shas[c["sha"]]
        c["date"] = "2026-05-10"
    b["prs"][0]["merged_at"] = "2026-05-10T12:00:00Z"
    b["issues"][0]["closed_at"] = "2026-05-10T12:00:00Z"
    return b


_STORE_FIXTURES = ("bundle_p2.json", "bundle_p3.json", "bundle_p3b.json",
                   "bundle_p3c.json")


class TestCleanFixturesPass(_Case):
    def test_every_fixture_bundle_validates_clean(self):
        for name in _STORE_FIXTURES:
            with self.subTest(name):
                self.assertClean(_store(_bundle(name)))
        self.assertClean(_store(_sample_as_gathered()))

    def test_every_fixture_bundle_validates_clean_with_bundle(self):
        for name in _STORE_FIXTURES:
            with self.subTest(name):
                self.assertClean(_store(_bundle(name)), bundle=_bundle(name))
        b = _sample_as_gathered()
        self.assertClean(_store(b), bundle=b)

    def test_raw_sample_fixture_is_not_store_shaped(self):
        # One-letter shas fail the sha rule, and with no timestamp at all the
        # store cannot be self-sourced (issue #79): the fixture, not the
        # check, is unlike real data.
        report = _report(_store(_bundle("bundle_sample.json")))
        self.assertEqual(_failing(report), [
            "edge_set", "idempotency", "no_drift", "no_fabrication",
            "provenance"])

    def test_edge_set_is_listed_after_schema_conformance(self):
        names = [c["name"] for c in _report(_p3b()).checks]
        self.assertEqual(names, [
            "target", "referential_integrity", "participant_completeness",
            "provenance", "no_fabrication", "schema_conformance", "edge_set",
            "no_drift", "idempotency"])

    def test_edge_set_reports_counts(self):
        c = _check(_report(_p3b()), "edge_set")
        self.assertTrue(c["ok"], c)
        self.assertIn("edge(s)", json.dumps(c["details"]))


# --- confirmed mutations from the issue -------------------------------------------

class TestFabricatedNodes(_Case):
    def test_inserted_pr_with_foreign_url_fails_provenance(self):
        conn = _p3b()
        graphstore.upsert_node(conn, "o/r#pr-999", "o", "r", "social",
                               "2026-05-20T00:00:00Z",
                               {"number": 999, "merged": True, "title": "x",
                                "url": "https://evil.example/made-up"})
        self.assertFailsOn(conn, "provenance", "pr-999")

    def test_pr_url_rewritten_to_another_number_fails_provenance(self):
        conn = _p3b()
        _set_data(conn, "o/r#pr-42", url="https://github.com/o/r/pull/4242")
        self.assertFailsOn(conn, "provenance", "pr-42")

    def test_pr_url_of_another_repo_fails_provenance(self):
        conn = _p3b()
        _set_data(conn, "o/r#pr-42", url="https://github.com/o/other/pull/42")
        self.assertFailsOn(conn, "provenance", "pr-42")

    def test_pr_url_pointing_at_an_issue_fails_provenance(self):
        conn = _p3b()
        _set_data(conn, "o/r#pr-42", url="https://github.com/o/r/issues/42")
        self.assertFailsOn(conn, "provenance", "pr-42")

    def test_pr_without_url_fails_provenance(self):
        conn = _p3b()
        n = graphstore.get_node(conn, "o/r#pr-42")
        data = {k: v for k, v in n["data"].items() if k != "url"}
        graphstore.upsert_node(conn, n["id"], "o", "r", "social", n["ts"], data)
        self.assertFailsOn(conn, "provenance", "pr-42")

    def test_number_disagreeing_with_id_fails_provenance(self):
        conn = _p3b()
        _set_data(conn, "o/r#issue-17", number=71)
        self.assertFailsOn(conn, "provenance", "issue-17")

    def test_url_owner_repo_case_is_insensitive(self):
        conn = _p3b()
        _set_data(conn, "o/r#pr-42", url="https://github.com/O/R/pull/42")
        self.assertEqual(_check(_report(conn), "provenance")["ok"], True)

    def test_commit_sha_not_40_hex_fails_provenance(self):
        conn = _p3b()
        n = graphstore.get_node(conn, "o/r#" + C1)
        graphstore.upsert_node(conn, "o/r#zz-not-a-sha", "o", "r", "code",
                               "2026-05-02", dict(n["data"], sha="zz-not-a-sha"))
        self.assertFailsOn(conn, "provenance", "zz-not-a-sha")

    def test_commit_sha_disagreeing_with_id_fails_provenance(self):
        conn = _p3b()
        _set_data(conn, "o/r#" + C1, sha=C4)
        self.assertFailsOn(conn, "provenance", C1)

    def test_garbage_id_fails_no_fabrication(self):
        conn = _p3b()
        graphstore.upsert_node(conn, "garbage-no-hash", "o", "r", "social",
                               "2026-05-20T00:00:00Z",
                               {"number": 1, "url": "https://github.com/o/r/pull/1"})
        self.assertFailsOn(conn, "no_fabrication", "garbage-no-hash")

    def test_unknown_local_form_fails_no_fabrication(self):
        conn = _p3b()
        graphstore.upsert_node(conn, "o/r#thing-1", "o", "r", "social",
                               "2026-05-20T00:00:00Z",
                               {"number": 1, "url": "https://github.com/o/r/pull/1"})
        self.assertFailsOn(conn, "no_fabrication", "thing-1")

    def test_id_scope_disagreeing_with_columns_fails_no_fabrication(self):
        conn = _p3b()
        n = graphstore.get_node(conn, "o/r#pr-42")
        graphstore.upsert_node(conn, "o/x#pr-42", "o", "r", "social", n["ts"],
                               n["data"])
        self.assertFailsOn(conn, "no_fabrication", "o/x#pr-42")

    def test_case_variant_duplicate_fails_no_fabrication(self):
        conn = _p3b()
        n = graphstore.get_node(conn, "o/r#pr-42")
        graphstore.upsert_node(conn, "O/r#pr-42", "O", "r", "social", n["ts"],
                               n["data"])
        self.assertFailsOn(conn, "no_fabrication", "O/r#pr-42")

    def test_case_variant_scope_in_wrong_columns_fails_no_fabrication(self):
        conn = _p3b()
        n = graphstore.get_node(conn, "o/r#pr-42")
        graphstore.upsert_node(conn, "O/r#pr-42", "o", "r", "social", n["ts"],
                               n["data"])
        self.assertFailsOn(conn, "no_fabrication", "O/r#pr-42")

    def test_person_id_under_a_repo_scope_fails_no_fabrication(self):
        conn = _p3b()
        graphstore.upsert_node(conn, "o/r#person-alice", "o", "r", "structure",
                               None, {"login": "alice", "areas": [], "modules": []})
        self.assertFailsOn(conn, "no_fabrication", "o/r#person-alice")


class TestExtraEdges(_Case):
    def test_extra_merged_edge_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o#person-dave", "o/r#pr-42", "merged")
        c = self.assertFailsOn(conn, "edge_set", "person-dave")
        self.assertIn("stored but not derivable", json.dumps(c["details"]))

    def test_extra_authored_edge_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o#person-carol", "o/r#pr-42", "authored")
        self.assertFailsOn(conn, "edge_set", "person-carol")

    def test_invented_edge_type_fails_schema_and_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#issue-17", "o#person-alice",
                               "approved_by")
        self.assertFailsOn(conn, "schema_conformance", "approved_by")
        self.assertFailsOn(conn, "edge_set", "approved_by")

    def test_cross_ref_person_to_area_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o#person-alice", "o/r#area-docs",
                               "cross_ref")
        self.assertFailsOn(conn, "schema_conformance", "cross_ref")

    def test_fake_closes_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#issue-18", "closes")
        self.assertFailsOn(conn, "edge_set", "issue-18")

    def test_fake_depends_on_pr_to_area_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#area-docs", "depends_on")
        self.assertFailsOn(conn, "schema_conformance", "depends_on")

    def test_fake_depends_on_area_to_area_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#area-docs",
                               "o/r#area-avm/res/network/firewall-policy",
                               "depends_on")
        self.assertFailsOn(conn, "edge_set", "depends_on")

    def test_fake_owns_pr_to_area_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#area-docs", "owns")
        self.assertFailsOn(conn, "schema_conformance", "owns")

    def test_fake_owns_person_to_area_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o#person-carol", "o/r#area-docs", "owns")
        self.assertFailsOn(conn, "edge_set", "owns")

    def test_fake_touches_pr_to_area_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#pr-42", "o/r#area-docs", "touches")
        self.assertFailsOn(conn, "schema_conformance", "touches")

    def test_fake_touches_commit_to_area_fails_edge_set(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#" + C1, "o/r#area-docs", "touches")
        self.assertFailsOn(conn, "edge_set", "touches")

    def test_edge_data_rewritten_fails_edge_set(self):
        conn = _store(_bundle("bundle_p3c.json"))
        row = conn.execute("SELECT src_id, dst_id FROM edges "
                           "WHERE edge_type='depends_on' LIMIT 1").fetchone()
        graphstore.upsert_edge(conn, row[0], row[1], "depends_on",
                               data={"version": "9.9.9"})
        c = self.assertFailsOn(conn, "edge_set", "edge data drift")
        self.assertIn("9.9.9", json.dumps(c["details"]))

    def test_cross_project_edge_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "zz#person-alice", "o/r#pr-42", "authored")
        self.assertFailsOn(conn, "schema_conformance", "zz#person-alice")

    def test_cross_repo_touches_fails_schema(self):
        conn = _p3b()
        graphstore.upsert_edge(conn, "o/r#" + C1, "o/other#area-docs", "touches")
        self.assertFailsOn(conn, "schema_conformance", "o/other#area-docs")

    def test_missing_closes_edge_fails_edge_set(self):
        conn = _p3b()
        conn.execute("DELETE FROM edges WHERE edge_type='closes'")
        conn.commit()
        c = self.assertFailsOn(conn, "edge_set", "closes")
        self.assertIn("derived from the stored records but not stored",
                      json.dumps(c["details"]))


class TestPeopleNotSeeded(_Case):
    def test_areas_tampering_fails_no_drift(self):
        # carol only reviewed PR 42 (firewall-policy); give her docs too.
        conn = _p3b()
        _set_data(conn, "o#person-carol",
                  areas=["avm/res/network/firewall-policy", "docs"],
                  modules=["avm/res/network/firewall-policy", "docs"])
        c = self.assertFailsOn(conn, "no_drift", "carol")
        self.assertIn("docs", json.dumps(c["details"]))

    def test_areas_tampering_fails_no_drift_with_bundle(self):
        # a one-window store: the --bundle is the whole store, people exact
        conn = _p3b()
        _set_data(conn, "o#person-carol",
                  areas=["avm/res/network/firewall-policy", "docs"],
                  modules=["avm/res/network/firewall-policy", "docs"])
        self.assertFailsOn(conn, "no_drift", "carol", bundle=_bundle())

    def test_pure_participant_given_areas_fails_no_drift(self):
        conn = _store(_bundle("bundle_p3.json"))   # bob/carol/dave comment only
        login = sorted(validate._stored_project_people(conn, "o"))[0]
        _set_data(conn, graphstore.qualify_person("o", login),
                  areas=["mod/net", "docs"], modules=["mod/net", "docs"])
        self.assertFailsOn(conn, "no_drift", login)

    def test_derive_people_ignores_bundle_people(self):
        b = _bundle()
        b["people"] = {"carol": {"areas": ["docs"], "modules": ["docs"]}}
        self.assertEqual(validate._derive_people(b)["carol"]["areas"],
                         ["avm/res/network/firewall-policy"])


# --- what the store alone cannot show: caught with --bundle --------------------

class TestBundleCrossCheck(_Case):
    def test_title_and_merged_rewrite_passes_store_only_but_fails_with_bundle(self):
        conn = _p3b()
        _set_data(conn, "o/r#pr-42", title="rewritten", merged=False)
        self.assertEqual(_report(conn).ok, True)   # documented limit
        c = self.assertFailsOn(conn, "no_drift", "pr-42", bundle=_bundle())
        self.assertIn("title", json.dumps(c["details"]))

    def test_comment_body_rewrite_fails_with_bundle(self):
        b = _bundle("bundle_p3.json")
        conn = _store(b)
        pr = graphstore.get_node(conn, "o/r#pr-42")
        cl = copy.deepcopy(pr["data"]["comments_list"])
        cl[0]["body"] = "rewritten"
        _set_data(conn, "o/r#pr-42", comments_list=cl)
        self.assertFailsOn(conn, "no_drift", "comments_list", bundle=b)

    def test_record_deleted_with_its_edges_fails_with_bundle(self):
        conn = _p3b()
        conn.execute("DELETE FROM nodes WHERE id='o/r#issue-18'")
        conn.execute("DELETE FROM edges WHERE src_id='o/r#issue-18' "
                     "OR dst_id='o/r#issue-18'")
        conn.commit()
        self.assertEqual(_report(conn).ok, True)   # documented limit
        self.assertFailsOn(conn, "no_drift", "issue-18", bundle=_bundle())

    def test_reviewer_dropped_from_blob_and_edge_fails_with_bundle(self):
        # alice also reviewed her own PR: dropping that review leaves no trace
        # in the store (she keeps her person node, areas and other edges).
        b = _bundle()
        b["prs"][0]["reviewers"] = ["carol", "alice"]
        conn = _store(b)
        _set_data(conn, "o/r#pr-42", reviewers=["carol"])
        conn.execute("DELETE FROM edges WHERE edge_type='reviewed' "
                     "AND src_id='o#person-alice'")
        conn.commit()
        self.assertEqual(_report(conn).ok, True)   # documented limit
        self.assertFailsOn(conn, "no_drift", "reviewers", bundle=b)

    def test_a_view_saved_at_gather_time_works_as_the_bundle(self):
        # gather writes no bundle file: a view.py/extract JSON of the fresh
        # store is the cross-check, also for PRs/issues whose reviews and
        # lifecycle events the view re-attaches from their own nodes.
        import extract
        b = _bundle()
        b["prs"][0]["reviews"] = [{"id": 5, "author": "carol",
                                   "state": "approved", "body": None,
                                   "submitted_at": "2026-05-09T00:00:00Z",
                                   "url": None}]
        b["issues"][0]["lifecycle"] = [{"id": 6, "actor": "alice",
                                        "event": "closed", "label": None,
                                        "created_at": "2026-05-10T12:00:00Z",
                                        "url": None}]
        conn = _store(b)
        view = json.loads(json.dumps(extract.extract(
            conn, "o", "r", "2026-05-01", "2026-05-31", warn=lambda _m: None)))
        self.assertClean(conn, bundle=view)
        _set_data(conn, "o/r#pr-42", title="rewritten")
        self.assertFailsOn(conn, "no_drift", "title", bundle=view)

    def test_reviewer_who_inherited_areas_is_caught_store_only(self):
        # dave reviewed PR 42 and so inherited its area; dropping the review
        # leaves an area the records no longer imply (people are not seeded).
        b = _bundle()
        b["prs"][0]["reviewers"] = ["carol", "dave"]
        conn = _store(b)
        _set_data(conn, "o/r#pr-42", reviewers=["carol"])
        conn.execute("DELETE FROM edges WHERE edge_type='reviewed' "
                     "AND src_id='o#person-dave'")
        conn.commit()
        self.assertFailsOn(conn, "no_drift", "dave")


# --- known gap 1: idempotency on a multi-window store ---------------------------

def _sym(path, change, name, commit, date):
    return {"path": path, "lang": "bicep", "subkind": "resource", "name": name,
            "change": change, "commit": commit, "author": "a", "date": date,
            "before": None, "after": None}


def _sym_bundle(frm, to, events):
    return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
            "commits": [], "code_events": [], "symbol_events": events}


class TestMultiWindowIdempotency(_Case):
    def test_cross_window_drop_and_add_is_not_an_idempotency_failure(self):
        may = _sym_bundle("2026-05-01", "2026-05-31",
                          [_sym("a.bicep", "drop", "fw", "a2" * 20, "2026-05-20")])
        june = _sym_bundle("2026-06-01", "2026-06-30",
                           [_sym("b.bicep", "add", "fw", "b1" * 20, "2026-06-05")])
        conn = _store(may, june)
        report = self.assertClean(conn)
        idem = _check(report, "idempotency")
        self.assertIn("cross-window", json.dumps(idem["details"]))

    def test_single_window_store_still_catches_a_non_idempotent_fold(self):
        # A move the single window implies but the store lacks: the re-fold
        # adds it, and on a one-window store that is a failure.
        b = _sym_bundle("2026-05-01", "2026-05-31",
                        [_sym("a.bicep", "drop", "fw", "a2" * 20, "2026-05-20"),
                         _sym("b.bicep", "add", "fw", "a2" * 20, "2026-05-20")])
        conn = _store(b)
        conn.execute("DELETE FROM edges WHERE edge_type IN "
                     "('replaced_by', 'identity_from')")
        conn.commit()
        self.assertFailsOn(conn, "idempotency", "non-idempotent")

    def test_multi_window_unsupported_move_edge_fails_edge_set(self):
        may = _sym_bundle("2026-05-01", "2026-05-31",
                          [_sym("a.bicep", "add", "fw", "a1" * 20, "2026-05-02")])
        june = _sym_bundle("2026-06-01", "2026-06-30",
                           [_sym("b.bicep", "add", "gw", "b1" * 20, "2026-06-05")])
        conn = _store(may, june)
        data = {"move_confidence": "medium", "move_basis": "unique_name"}
        graphstore.upsert_edge(conn, "o/r#a.bicep#bicep:resource:fw",
                               "o/r#b.bicep#bicep:resource:gw", "replaced_by",
                               data=data)
        graphstore.upsert_edge(conn, "o/r#b.bicep#bicep:resource:gw",
                               "o/r#a.bicep#bicep:resource:fw", "identity_from",
                               data=data)
        self.assertFailsOn(conn, "edge_set", "replaced_by")

    def test_multi_window_recorded_move_is_supported(self):
        create = _sym_bundle("2026-05-01", "2026-05-31",
                             [_sym("a.bicep", "add", "fw", "a1" * 20, "2026-05-02")])
        move = _sym_bundle("2026-06-01", "2026-06-30",
                           [_sym("a.bicep", "drop", "fw", "a2" * 20, "2026-06-05"),
                            _sym("b.bicep", "add", "fw", "a2" * 20, "2026-06-05")])
        conn = _store(create, move)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM edges WHERE edge_type='replaced_by'"
        ).fetchone()[0], 1)
        self.assertClean(conn)


# --- known gap 2: one member of a multi-member project ---------------------------

def _two_members():
    members = {"Azure/mod-a", "Azure/mod-b"}
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for repo, author, num, area in (("Azure/mod-a", "alice", 1, "a"),
                                    ("Azure/mod-b", "bob", 2, "b")):
        sha = ("a" if num == 1 else "b") * 40
        b = {"meta": {"owner": "Azure", "repo": repo.split("/")[1],
                      "from": "2026-01-01", "to": "2026-01-31"},
             "prs": [{"number": num, "author": author, "merged": True,
                      "merged_at": "2026-01-10T00:00:00Z", "closes": [],
                      "crossref_issues": [], "title": "t", "body": "",
                      "url": "https://github.com/{}/pull/{}".format(repo, num)}],
             "issues": [],
             "commits": [{"sha": sha, "author": author, "date": "2026-01-09",
                          "message": "feat (#{})".format(num),
                          "files": ["{}/main.tf".format(area)]}],
             "code_events": [], "milestones": [], "releases": [],
             "code_graph": {"areas": [{"id": area,
                                       "paths": ["{}/main.tf".format(area)]}]}}
        gather.fold_bundle(conn, b, project="proj", repo=repo, members=members)
    return conn


class TestSingleMemberAudit(_Case):
    def test_one_healthy_member_passes(self):
        conn = _two_members()
        report = self.assertClean(conn, project="proj", repo="Azure/mod-b")
        nd = _check(report, "no_drift")
        self.assertIn("project-wide", json.dumps(nd["details"]))

    def test_member_derived_area_missing_still_fails(self):
        conn = _two_members()
        _set_data(conn, "proj#person-bob", areas=[], modules=[])
        report = validate.validate(conn, project="proj", repo="Azure/mod-b")
        nd = _check(report, "no_drift")
        self.assertIs(nd["ok"], False, nd)
        self.assertIn("bob", json.dumps(nd["details"]))

    def test_project_mode_people_stay_exact(self):
        conn = _two_members()
        _set_data(conn, "proj#person-bob", areas=["a", "b"], modules=["a", "b"])
        agg = validate.validate_project(conn, "proj",
                                        ["Azure/mod-a", "Azure/mod-b"])
        people = [c for c in agg["project_checks"]
                  if c["name"] == "no_drift_people"][0]
        self.assertIs(people["ok"], False, people)


# --- known gap 3: login_resolved and pre-#78 people -----------------------------

class TestPeopleIdentity(_Case):
    def _unresolved_store(self):
        b = _bundle()
        for c in b["commits"]:
            if c["author"] == "dave":
                c["author"] = "Dave D"
                c["author_email"] = "dave@corp.example"
        gather.resolve_commit_authors(b["commits"], {})
        return _store(b), b

    def test_login_resolved_is_compared(self):
        conn, _ = self._unresolved_store()
        qid = graphstore.qualify_person("o", "Dave D")
        n = graphstore.get_node(conn, qid)
        self.assertIs(n["data"]["login_resolved"], False)
        self.assertClean(conn)
        data = {k: v for k, v in n["data"].items() if k != "login_resolved"}
        graphstore.upsert_node(conn, qid, "o", "*", "structure", None, data)
        self.assertFailsOn(conn, "no_drift", "login_resolved")

    def test_pre_78_git_name_person_gets_an_actionable_message(self):
        # A pre-#78 store: dave's commit was keyed by his git name.
        old = _bundle()
        for c in old["commits"]:
            if c["author"] == "dave":
                c["author"] = "Dave D"
        conn = _store(old)
        # Re-gathered after #78: the same commit now names the login.
        _set_data(conn, "o/r#" + C4, author="dave")
        gather.fold_bundle(conn, _bundle())
        report = _report(conn)
        nd = _check(report, "no_drift")
        self.assertIs(nd["ok"], False)
        text = json.dumps(nd["details"])
        self.assertIn("Dave D", text)
        self.assertIn("#78", text)
        self.assertIn("fresh store", text)
        es = _check(report, "edge_set")
        self.assertIs(es["ok"], False)
        self.assertIn("#78", json.dumps(es["details"]))


# --- multi-window and multi-repo stores stay green -------------------------------

class TestRealShapedStoresPass(_Case):
    def test_cross_repo_closes_and_registry_depends_on_pass(self):
        members = {"o/app", "o/terraform-azurerm-mod"}
        app = {"meta": {"owner": "o", "repo": "app", "from": "2026-01-01",
                        "to": "2026-01-31"},
               "prs": [{"number": 5, "author": "alice", "merged": True,
                        "merged_at": "2026-01-10T00:00:00Z", "closes": [],
                        "crossref_issues": [], "title": "t",
                        "body": "Fixes o/terraform-azurerm-mod#9",
                        "url": "https://github.com/o/app/pull/5"}],
               "issues": [], "commits": [], "code_events": [],
               "code_graph": {"areas": [{
                   "id": "main.tf", "paths": ["main.tf"],
                   "edges": [{"to": None, "resolved": False, "version": "1.0.0",
                              "ref": "o/mod/azurerm", "transitive": False}]}]}}
        mod = {"meta": {"owner": "o", "repo": "terraform-azurerm-mod",
                        "from": "2026-01-01", "to": "2026-01-31"},
               "prs": [], "issues": [{"number": 9, "author": "bob",
                                      "closed_at": "2026-01-10T00:00:00Z",
                                      "url": "https://github.com/o/terraform-"
                                             "azurerm-mod/issues/9"}],
               "commits": [], "code_events": []}
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        reg = {m: None for m in members}
        for b, repo in ((app, "o/app"), (mod, "o/terraform-azurerm-mod")):
            gather.fold_bundle(conn, copy.deepcopy(b), project="p", repo=repo,
                               members=members, registry_by_slug=reg)
        xedges = conn.execute(
            "SELECT edge_type FROM edges WHERE src_id LIKE 'p/o/app#%' "
            "AND dst_id LIKE 'p/o/terraform-azurerm-mod#%'").fetchall()
        self.assertEqual(sorted(r[0] for r in xedges), ["closes", "depends_on"])
        agg = validate.validate_project(conn, "p", sorted(members))
        bad = [(m["repo"], c) for m in agg["members"] for c in m["checks"]
               if not c["ok"]] + [c for c in agg["project_checks"] if not c["ok"]]
        self.assertEqual(bad, [], json.dumps(bad, indent=1, default=str))

    def test_unsupported_cross_repo_edge_fails_edge_set(self):
        members = {"o/a", "o/b"}
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for repo, num in (("o/a", 1), ("o/b", 2)):
            gather.fold_bundle(conn, {
                "meta": {"owner": "o", "repo": repo[2:], "from": "2026-01-01",
                         "to": "2026-01-31"},
                "prs": [{"number": num, "author": "alice", "merged": True,
                         "merged_at": "2026-01-10T00:00:00Z", "closes": [],
                         "crossref_issues": [], "title": "t", "body": "",
                         "url": "https://github.com/{}/pull/{}".format(repo, num)}],
                "issues": [], "commits": [], "code_events": []},
                project="p", repo=repo, members=members)
        graphstore.upsert_edge(conn, "p/o/a#pr-1", "p/o/b#pr-2", "cross_ref")
        report = validate.validate(conn, project="p", repo="o/a")
        es = _check(report, "edge_set")
        self.assertIs(es["ok"], False, es)
        self.assertIn("p/o/b#pr-2", json.dumps(es["details"]))

    def test_multi_window_stale_current_state_edge_is_a_warning(self):
        # May's code graph maps the file to `old`; June re-maps it to `new`.
        def window(frm, to, sha, area):
            return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to},
                    "prs": [], "issues": [],
                    "commits": [{"sha": sha, "author": "alice", "date": frm,
                                 "message": "x", "files": ["m/main.tf"]}],
                    "code_events": [],
                    "code_graph": {"areas": [{"id": area,
                                              "paths": ["m/main.tf"]}]}}
        conn = _store(window("2026-05-01", "2026-05-31", "a" * 40, "old"),
                      window("2026-06-01", "2026-06-30", "b" * 40, "new"))
        report = self.assertClean(conn)
        es = _check(report, "edge_set")
        warns = [d for d in es["details"] if d.get("severity") == "WARN"]
        self.assertTrue(warns, es)
        self.assertIn("area-old", json.dumps(warns))


if __name__ == "__main__":
    unittest.main()
