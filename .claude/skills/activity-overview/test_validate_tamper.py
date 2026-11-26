"""Trust-check tamper cases (issue #113, trust-check items).

Each case tampers with a correct store one edit at a time and names the check
that now fails; the untampered store (and the correct stores a normal gather
produces: re-folds, several windows, legacy stores, manifest stores) still
passes.

- C2  invented records: a PR / issue nobody contributed to, node kinds fold
      never writes (`comment-N`, m5), and — with --bundle — any stored record
      of the bundle's window that the bundle lacks.
- M1  the store's own markers: an unreadable or missing `current_state:`, a
      deleted `facts:` entry, a `gathered_windows` entry no fold wrote.
- M2  per-window meta: fact values re-hashed against their key, fact refs
      checked by shape, `branches:` / `ref_date:` / `clone:` / `clone_sha:`
      shapes, orphan per-window keys; with --bundle their values.
- M3  artifact lifecycle entries compared, not only counted.
- M4  a cross-repo depends_on resolved against the manifest's registries
      recorded at fold time (meta `fold_context:`), both directions.
- M7  symbol-move support checked against an index, not a scan per move.
- m2  a missing cross-repo closes / cross_ref is detected.
- m6  project mode: no full-table scan per member, one self-source each.
- m7  project names are case-sensitive: two projects differing only in case
      are not case-variants of each other.
"""

import copy
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
D = "d" * 40


def _bundle(name="bundle_p3b.json"):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def _gathered_p3b():
    """bundle_p3b as a real gather carries it: branches, clone provenance,
    CI statistics (bundle_p2's), so every per-window meta key is written."""
    b = _bundle()
    b["meta"].update(base_branch="main", branches=["main"],
                     clone_sha="ab" * 20, clone_mode="fresh",
                     boundary_dropped_commits=[], structure_as_of="window_end",
                     ref_date="2026-05-31")
    b["workflow_stats"] = _bundle("bundle_p2.json")["workflow_stats"]
    return b


def _store(*bundles, **kw):
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for b in bundles:
        gather.fold_bundle(conn, copy.deepcopy(b), **kw)
    return conn


def _window(frm, to, n, sha, area="m"):
    """A small one-window bundle: one merged PR, one commit, one area."""
    return {"meta": {"owner": "o", "repo": "r", "from": frm, "to": to,
                     "base_branch": "main", "branches": ["main"]},
            "prs": [{"number": n, "author": "alice", "merged": True,
                     "merged_at": frm + "T12:00:00Z", "closes": [],
                     "crossref_issues": [], "title": "t", "body": "",
                     "url": "https://github.com/o/r/pull/{}".format(n)}],
            "issues": [],
            "commits": [{"sha": sha, "author": "alice", "date": frm,
                         "message": "x (#{})".format(n), "files": ["m/main.tf"]}],
            "code_events": [],
            "code_graph": {"areas": [{"id": area, "paths": ["m/main.tf"]}]}}


MAY = ("2026-05-01", "2026-05-31")
JUNE = ("2026-06-01", "2026-06-30")


def _two_windows():
    may = _window(MAY[0], MAY[1], 1, "a" * 40)
    june = _window(JUNE[0], JUNE[1], 2, "b" * 40)
    return _store(may, june), may, june


def _check(report, name):
    for c in report.checks:
        if c["name"] == name:
            return c
    raise AssertionError("no check {} in {}".format(
        name, [c["name"] for c in report.checks]))


def _report(conn, **kw):
    return validate.validate(conn, project="o", repo="r", **kw)


def _set_meta(conn, key, value):
    graphstore.set_meta(conn, key, value if isinstance(value, str)
                        else json.dumps(value, sort_keys=True))


def _del_meta(conn, key):
    conn.execute("DELETE FROM meta WHERE key=?", (key,))
    conn.commit()


def _meta(conn, key):
    return json.loads(graphstore.get_meta(conn, key))


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


# --- C2 / m5: invented records -------------------------------------------------

def _fake_pr(conn, number=999, author=None, ts="2026-05-20T00:00:00Z"):
    data = {"number": number, "merged": True, "merged_at": ts, "title": "x",
            "state": "closed", "closes": [], "crossref_issues": [],
            "url": "https://github.com/o/r/pull/{}".format(number)}
    if author:
        data["author"] = author
    graphstore.upsert_node(conn, "o/r#pr-{}".format(number), "o", "r",
                           "social", ts, data)
    if author:
        graphstore.upsert_edge(conn, "o#person-" + author,
                               "o/r#pr-{}".format(number), "authored")


class TestInventedRecords(_Case):
    def test_clean_store_passes_with_and_without_bundle(self):
        b = _gathered_p3b()
        self.assertClean(_store(b))
        self.assertClean(_store(b), bundle=b)

    def test_pr_nobody_contributed_to_fails_no_fabrication(self):
        conn = _store(_bundle())
        _fake_pr(conn)
        self.assertFailsOn(conn, "no_fabrication", "no contribution edge")

    def test_issue_nobody_contributed_to_fails_no_fabrication(self):
        b = _bundle()
        for iss in b["issues"]:
            iss["author"] = "dave"
        conn = _store(b)
        self.assertClean(conn)
        graphstore.upsert_node(conn, "o/r#issue-998", "o", "r", "social",
                               "2026-05-20T00:00:00Z", {
                                   "number": 998, "state": "closed",
                                   "closed_at": "2026-05-20T00:00:00Z",
                                   "url": "https://github.com/o/r/issues/998"})
        self.assertFailsOn(conn, "no_fabrication", "o/r#issue-998")

    def test_hand_built_records_without_authors_only_warn(self):
        # The fixtures' issues carry no author (no gather writes such a
        # record): with no authored issue in the repo to compare with, an
        # issue nobody contributed to is a WARN, not a failure.
        report = _report(_store(_bundle()))
        c = _check(report, "no_fabrication")
        self.assertTrue(c["ok"], c)
        self.assertIn("o/r#issue-17", json.dumps(
            [d for d in c["details"] if d.get("severity") == "WARN"]))

    def test_comment_node_fails_no_fabrication(self):
        # m5: fold never writes a `comment-N` node; with a url it used to pass
        # provenance and no_fabrication alike.
        conn = _store(_bundle())
        graphstore.upsert_node(conn, "o/r#comment-5", "o", "r", "social",
                               "2026-05-20T00:00:00Z", {
                                   "url": "https://github.com/o/r/pull/42"})
        self.assertFailsOn(conn, "no_fabrication", "o/r#comment-5")

    def test_consistent_fake_pr_is_invisible_store_only_but_fails_with_bundle(self):
        # A fake PR plus its `authored` edge is internally consistent: no
        # store-only audit can tell it from a real one (STORE.md). The bundle
        # saved at gather time can.
        conn = _store(_bundle())
        _fake_pr(conn, author="alice")
        self.assertClean(conn)
        c = self.assertFailsOn(conn, "no_drift", "o/r#pr-999",
                               bundle=_bundle())
        self.assertIn("not in --bundle", json.dumps(c["details"]))

    def test_stray_commit_release_milestone_area_fail_with_bundle(self):
        for local, cls, ts, data in (
                (D, "code", "2026-05-12",
                 {"sha": D, "author": "alice", "date": "2026-05-12",
                  "message": "x", "files": []}),
                ("release-v9", "structure", "2026-05-15T00:00:00Z",
                 {"tag_name": "v9", "published_at": "2026-05-15T00:00:00Z"}),
                ("milestone-77", "structure", None,
                 {"number": 77, "title": "fake"}),
                ("area-fake", "structure", None,
                 {"id": "fake", "paths": ["fake/x.tf"]})):
            with self.subTest(local):
                b = _bundle()
                conn = _store(b)
                graphstore.upsert_node(conn, "o/r#" + local, "o", "r", cls,
                                       ts, data)
                if cls == "code":
                    graphstore.upsert_edge(conn, "o#person-alice", "o/r#" + local,
                                           "authored")
                self.assertFailsOn(conn, "no_drift", "o/r#" + local, bundle=b)

    def test_fake_review_of_a_real_pr_fails_with_bundle(self):
        b = _bundle()
        conn = _store(b)
        graphstore.upsert_node(conn, "o/r#review-42-777", "o", "r", "social",
                               "2026-05-06T00:00:00Z", {
                                   "id": 777, "author": "alice", "state": "APPROVED",
                                   "submitted_at": "2026-05-06T00:00:00Z",
                                   "url": "https://github.com/o/r/pull/42"
                                          "#pullrequestreview-777"})
        graphstore.upsert_edge(conn, "o/r#review-42-777", "o/r#pr-42", "part_of")
        self.assertFailsOn(conn, "no_drift", "o/r#review-42-777", bundle=b)

    def test_window_bundle_of_a_two_window_store_passes(self):
        conn, may, june = _two_windows()
        self.assertClean(conn, bundle=may)
        self.assertClean(conn, bundle=june)

    def test_bundle_of_a_store_with_a_depends_on_stand_in_passes(self):
        # the stand-in for a sub-module the window's graph does not list is
        # an edge anchor, not a stored area the bundle lacks
        b = _window(MAY[0], MAY[1], 1, "a" * 40)
        b["code_graph"]["areas"][0]["edges"] = [
            {"to": "sub", "resolved": True, "kind": "module", "transitive": False}]
        conn = _store(b)
        self.assertEqual(graphstore.get_node(conn, "o/r#area-sub")["data"]
                         ["synthesized"], graphstore.STANDIN_MARK)
        self.assertClean(conn, bundle=b)

    def test_saved_view_as_bundle_passes(self):
        b = _gathered_p3b()
        conn = _store(b)
        view = extract.extract(conn, "o", "r", MAY[0], MAY[1],
                               warn=lambda _m: None)
        self.assertClean(conn, bundle=json.loads(json.dumps(view)))


# --- M1: the store's markers ---------------------------------------------------

def _fake_depends_on(conn):
    """A depends_on no window's facts imply, to a stand-in target (so it
    dangles nowhere): on a multi-window store whose markers were damaged it
    used to be only a WARN."""
    graphstore.upsert_node(conn, "o/r#area-x", "o", "r", "structure", None,
                           {"id": "x", "synthesized": graphstore.STANDIN_MARK})
    graphstore.upsert_edge(conn, "o/r#area-m", "o/r#area-x", "depends_on",
                           data={"kind": "module", "resolved": True})


class TestStoreMarkers(_Case):
    def test_two_window_store_is_clean(self):
        conn, _may, _june = _two_windows()
        self.assertClean(conn)

    def test_corrupt_current_state_is_an_error(self):
        conn, _m, _j = _two_windows()
        _set_meta(conn, "current_state:o/r", "{not json")
        self.assertFailsOn(conn, "meta_integrity", "current_state:o/r")

    def test_current_state_without_items_is_an_error(self):
        conn, _m, _j = _two_windows()
        state = _meta(conn, "current_state:o/r")
        del state["items"]
        _set_meta(conn, "current_state:o/r", state)
        self.assertFailsOn(conn, "meta_integrity", "current_state:o/r")

    def test_deleted_current_state_is_an_error(self):
        # It used to downgrade a fake `blocks` / depends_on to a WARN.
        conn, _m, _j = _two_windows()
        _del_meta(conn, "current_state:o/r")
        _fake_depends_on(conn)
        self.assertFailsOn(conn, "meta_integrity", "current_state")

    def test_deleted_facts_entry_is_an_error(self):
        conn, _m, _j = _two_windows()
        _del_meta(conn, "facts:o/r:{}:{}".format(*JUNE))
        _fake_depends_on(conn)
        self.assertFailsOn(conn, "meta_integrity", "2026-06-01:2026-06-30")

    def test_structure_flag_contradicting_the_facts_is_an_error(self):
        conn, _m, _j = _two_windows()
        state = _meta(conn, "current_state:o/r")
        state["structure"] = False
        _set_meta(conn, "current_state:o/r", state)
        self.assertFailsOn(conn, "meta_integrity", "structure")

    def test_fake_gathered_window_is_an_error_and_downgrades_nothing(self):
        conn = _store(_bundle())
        wins = graphstore.get_windows(conn)
        wins.append({"project": "o", "repo": "r", "from": "2026-07-01",
                     "to": "2026-07-31"})
        _set_meta(conn, "gathered_windows", wins)
        self.assertFailsOn(conn, "meta_integrity", "2026-07-01")
        graphstore.upsert_edge(conn, "o/r#area-docs",
                               "o/r#area-avm/res/network/firewall-policy",
                               "depends_on", data={"kind": "module"})
        # still exact: the evidence-less window does not make it multi-window
        self.assertFailsOn(conn, "edge_set", "depends_on")

    def test_unreadable_gathered_windows_is_an_error(self):
        conn = _store(_bundle())
        _set_meta(conn, "gathered_windows", "[{")
        self.assertFailsOn(conn, "meta_integrity", "gathered_windows")

    def test_legacy_store_without_markers_still_passes(self):
        # A store folded before #75/#94/#95 holds only gathered_windows.
        conn, _m, _j = _two_windows()
        for (key,) in conn.execute("SELECT key FROM meta").fetchall():
            if key.split(":")[0] in ("facts", "fact", "current_state",
                                     "ref_date", "branches", "clone",
                                     "retractions", "clone_sha"):
                _del_meta(conn, key)
        report = _report(conn)
        self.assertTrue(_check(report, "meta_integrity")["ok"],
                        _check(report, "meta_integrity"))

    def test_store_with_a_pre_facts_window_still_passes(self):
        # May folded before #94 (no facts), June since: the last fold marks
        # structure false, which is consistent and exact.
        may = _window(MAY[0], MAY[1], 1, "a" * 40)
        june = _window(JUNE[0], JUNE[1], 2, "b" * 40)
        conn = _store(may)
        for (key,) in conn.execute("SELECT key FROM meta").fetchall():
            if key.split(":")[0] in ("facts", "fact", "current_state"):
                _del_meta(conn, key)
        gather.fold_bundle(conn, copy.deepcopy(june))
        self.assertIs(_meta(conn, "current_state:o/r")["structure"], False)
        report = _report(conn)
        self.assertTrue(_check(report, "meta_integrity")["ok"],
                        _check(report, "meta_integrity"))


# --- M2: per-window meta -------------------------------------------------------

def _fact_keys(conn):
    return [k for (k,) in conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'fact:o/r:%'")]


class TestPerWindowMeta(_Case):
    FACTS = "facts:o/r:2026-05-01:2026-05-31"

    def _ci_key(self, conn):
        ref = _meta(conn, self.FACTS)["facts"]["workflow_stats"]
        return "fact:o/r:" + ref

    def test_changed_fact_value_fails_its_hash(self):
        conn = _store(_gathered_p3b())
        key = self._ci_key(conn)
        stats = _meta(conn, key)
        stats["CI"]["total"] = 30
        _set_meta(conn, key, stats)
        self.assertFailsOn(conn, "meta_integrity", key)

    def test_swapped_fact_refs_fail_their_shape(self):
        conn = _store(_gathered_p3b())
        entry = _meta(conn, self.FACTS)
        refs = entry["facts"]
        refs["workflow_stats"], refs["label_taxonomy"] = (
            refs["label_taxonomy"], refs["workflow_stats"])
        _set_meta(conn, self.FACTS, entry)
        self.assertFailsOn(conn, "meta_integrity", "workflow_stats")

    def test_malformed_facts_ref_fails(self):
        conn = _store(_gathered_p3b())
        entry = _meta(conn, self.FACTS)
        entry["facts"]["ci"] = entry["facts"].pop("workflow_stats")
        _set_meta(conn, self.FACTS, entry)
        self.assertFailsOn(conn, "meta_integrity", "ci")

    def test_rehashed_fact_passes_store_only_but_fails_with_bundle(self):
        b = _gathered_p3b()
        conn = _store(b)
        old = self._ci_key(conn)
        stats = _meta(conn, old)
        stats["CI"]["total"] = 30
        text = json.dumps(stats, sort_keys=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _del_meta(conn, old)
        _set_meta(conn, "fact:o/r:" + digest, text)
        entry = _meta(conn, self.FACTS)
        entry["facts"]["workflow_stats"] = digest
        _set_meta(conn, self.FACTS, entry)
        self.assertClean(conn)
        self.assertFailsOn(conn, "no_drift", "workflow_stats", bundle=b)

    def test_malformed_branches_ref_date_clone_fail(self):
        for key, value in (
                ("branches:o/r:2026-05-01:2026-05-31",
                 {"base_branch": 5, "branches": "main"}),
                ("ref_date:o/r:2026-05-01:2026-05-31", "yesterday"),
                ("clone:o/r:2026-05-01:2026-05-31",
                 {"clone_mode": "teleport", "boundary_dropped_commits": "x"}),
                ("clone_sha:o/r", "not-a-sha")):
            with self.subTest(key):
                conn = _store(_gathered_p3b())
                _set_meta(conn, key, value)
                self.assertFailsOn(conn, "meta_integrity", key)

    def test_orphan_per_window_key_fails(self):
        conn = _store(_gathered_p3b())
        _set_meta(conn, "ref_date:o/r:2026-09-01:2026-09-30", "2026-09-30")
        self.assertFailsOn(conn, "meta_integrity", "2026-09-01")

    def test_plausible_branch_and_ref_date_changes_fail_with_bundle(self):
        for key, value, field in (
                ("branches:o/r:2026-05-01:2026-05-31",
                 {"base_branch": "dev", "branches": ["dev"]}, "base_branch"),
                ("ref_date:o/r:2026-05-01:2026-05-31", "2026-05-20", "ref_date"),
                ("clone:o/r:2026-05-01:2026-05-31",
                 {"clone_mode": "fetched", "boundary_dropped_commits": [],
                  "structure_as_of": "window_end"}, "clone_mode")):
            with self.subTest(key):
                b = _gathered_p3b()
                conn = _store(b)
                _set_meta(conn, key, value)
                self.assertClean(conn)
                self.assertFailsOn(conn, "no_drift", field, bundle=b)


# --- M3: artifact lifecycle content --------------------------------------------

class TestLifecycleContent(_Case):
    def test_rewritten_lifecycle_author_fails_no_drift(self):
        conn = _store(_bundle())
        qid = "o/r#art:docs/firewall.md"
        n = graphstore.get_node(conn, qid)
        data = copy.deepcopy(n["data"])
        data["lifecycle"][0]["author"] = "mallory"
        graphstore.upsert_node(conn, qid, "o", "r", "code", n["ts"], data)
        self.assertFailsOn(conn, "no_drift", "mallory")


# --- M4 / m2: cross-repo edges -------------------------------------------------

APP, MOD, OTHER = "o/app", "o/terraform-azurerm-mod", "o/other"


def _tf_member(slug, prs=(), issues=(), areas=None):
    owner, repo = slug.split("/")
    b = {"meta": {"owner": owner, "repo": repo, "from": "2026-01-01",
                  "to": "2026-01-31"},
         "prs": list(prs), "issues": list(issues), "commits": [],
         "code_events": []}
    if areas is not None:
        b["code_graph"] = {"areas": areas}
    return b


def _tf_project(drop_context=False):
    members = {APP, MOD, OTHER}
    app = _tf_member(APP, prs=[{
        "number": 5, "author": "alice", "merged": True,
        "merged_at": "2026-01-10T00:00:00Z", "closes": [], "crossref_issues": [],
        "title": "t", "body": "Fixes o/terraform-azurerm-mod#9",
        "url": "https://github.com/o/app/pull/5"}], areas=[{
            "id": "main.tf", "paths": ["main.tf"],
            "edges": [{"to": None, "resolved": False, "version": "1.0.0",
                       "ref": "o/mod/azurerm", "transitive": False}]}])
    mod = _tf_member(MOD, issues=[{
        "number": 9, "author": "bob", "closed_at": "2026-01-10T00:00:00Z",
        "url": "https://github.com/o/terraform-azurerm-mod/issues/9"}],
        areas=[{"id": "main.tf", "paths": ["main.tf"], "edges": []}])
    other = _tf_member(OTHER, issues=[{
        "number": 3, "author": "bob", "closed_at": "2026-01-10T00:00:00Z",
        "url": "https://github.com/o/other/issues/3"}],
        areas=[{"id": "main.tf", "paths": ["main.tf"], "edges": []}])
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    reg = {APP: None, MOD: None, OTHER: "o/other/azurerm"}
    for slug, b in ((APP, app), (MOD, mod), (OTHER, other)):
        gather.fold_bundle(conn, b, project="p", repo=slug, members=members,
                           registry_by_slug=reg, bicep_prefix_by_slug={})
    if drop_context:
        for slug in members:
            _del_meta(conn, "fold_context:p/" + slug)
    return conn


def _project_failures(conn):
    agg = validate.validate_project(conn, "p", graphstore.project_repos(conn, "p"))
    return agg, sorted(
        ["{}:{}".format(m["repo"], c["name"]) for m in agg["members"]
         for c in m["checks"] if not c["ok"]]
        + ["project:" + c["name"] for c in agg["project_checks"] if not c["ok"]])


def _member_check(agg, repo, name):
    for m in agg["members"]:
        if m["repo"] == repo:
            for c in m["checks"]:
                if c["name"] == name:
                    return c
    raise AssertionError((repo, name))


ROOT = "p/{}#area-main.tf"


class TestCrossRepoEdges(_Case):
    def test_manifest_store_is_clean_and_records_its_fold_context(self):
        conn = _tf_project()
        self.assertEqual(_project_failures(conn)[1], [])
        ctx = graphstore.get_fold_context(conn, "p", APP)
        self.assertEqual(ctx["members"], sorted([APP, MOD, OTHER]))
        self.assertEqual(ctx["registry"][OTHER], "o/other/azurerm")
        self.assertIs(ctx["members_changed"], False)

    def test_terraform_depends_on_repointed_to_another_root_fails(self):
        conn = _tf_project()
        key = (ROOT.format(APP), ROOT.format(MOD), "depends_on")
        data = conn.execute("SELECT data FROM edges WHERE src_id=? AND dst_id=? "
                            "AND edge_type=?", key).fetchone()[0]
        graphstore.delete_edges(conn, [key])
        graphstore.upsert_edge(conn, ROOT.format(APP), ROOT.format(OTHER),
                               "depends_on", data=json.loads(data))
        agg, bad = _project_failures(conn)
        self.assertIn(APP + ":edge_set", bad)
        self.assertIn(ROOT.format(OTHER),
                      json.dumps(_member_check(agg, APP, "edge_set")))

    def test_missing_cross_repo_depends_on_fails(self):
        conn = _tf_project()
        graphstore.delete_edges(conn, [(ROOT.format(APP), ROOT.format(MOD),
                                        "depends_on")])
        agg, bad = _project_failures(conn)
        self.assertIn(APP + ":edge_set", bad)

    def test_missing_cross_repo_closes_fails(self):
        conn = _tf_project()
        graphstore.delete_edges(conn, [("p/o/app#pr-5",
                                        "p/o/terraform-azurerm-mod#issue-9",
                                        "closes")])
        agg, bad = _project_failures(conn)
        self.assertIn(APP + ":edge_set", bad)
        self.assertIn("issue-9", json.dumps(_member_check(agg, APP, "edge_set")))

    def test_store_without_fold_context_warns_on_a_missing_cross_repo_edge(self):
        # A manifest store folded before the fold context was recorded: the
        # member set at fold time is unknown, so a missing cross-repo edge is
        # only a WARN (re-fold to make it exact).
        conn = _tf_project(drop_context=True)
        self.assertEqual(_project_failures(conn)[1], [])
        graphstore.delete_edges(conn, [("p/o/app#pr-5",
                                        "p/o/terraform-azurerm-mod#issue-9",
                                        "closes")])
        agg, bad = _project_failures(conn)
        self.assertEqual(bad, [])
        es = _member_check(agg, APP, "edge_set")
        self.assertIn("WARN", json.dumps(es))

    def test_member_set_change_is_recorded_and_relaxes_cross_repo_checks(self):
        conn = _tf_project()
        b = _tf_member(APP, areas=[{"id": "main.tf", "paths": ["main.tf"],
                                    "edges": []}])
        b["meta"].update({"from": "2026-02-01", "to": "2026-02-28"})
        gather.fold_bundle(conn, b, project="p", repo=APP, members={APP, MOD},
                           registry_by_slug={}, bicep_prefix_by_slug={})
        self.assertIs(graphstore.get_fold_context(conn, "p", APP)
                      ["members_changed"], True)
        self.assertEqual(_project_failures(conn)[1], [])


BRM = "Azure/bicep-registry-modules"
LZ = "Contoso/landing-zone"
NOT_PUB = "Contoso/not-a-publisher"
KV = "avm/res/key-vault/vault"


def _bicep_project():
    members = {BRM, LZ, NOT_PUB}

    def member(slug, areas):
        owner, repo = slug.split("/")
        return {"meta": {"owner": owner, "repo": repo, "from": "2026-01-01",
                         "to": "2026-01-31"},
                "prs": [], "issues": [], "commits": [], "code_events": [],
                "code_graph": {"areas": areas}}
    lz = member(LZ, [{"id": "main.bicep", "paths": ["main.bicep"], "edges": [{
        "to": None, "resolved": False, "kind": "module",
        "ref": "br/public:{}:0.9.0".format(KV), "version": "0.9.0",
        "transitive": False, "provider": "bicep"}]}])
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for slug, b in ((LZ, lz),
                    (BRM, member(BRM, [{"id": KV, "paths": [KV + "/main.bicep"],
                                        "edges": []}])),
                    (NOT_PUB, member(NOT_PUB, [{"id": KV,
                                                "paths": [KV + "/main.bicep"],
                                                "edges": []}]))):
        gather.fold_bundle(conn, b, project="proj", repo=slug, members=members,
                           registry_by_slug={m: None for m in members},
                           bicep_prefix_by_slug={BRM: "avm", LZ: None,
                                                 NOT_PUB: None})
    return conn


class TestCrossRepoBicep(_Case):
    def test_bicep_depends_on_repointed_to_a_non_publisher_fails(self):
        conn = _bicep_project()
        agg = validate.validate_project(conn, "proj",
                                        graphstore.project_repos(conn, "proj"))
        self.assertTrue(agg["ok"], json.dumps(agg, indent=1)[:3000])
        # the consumer was folded first and left a stand-in in the
        # publisher's id space: that is not a record folded without context
        for slug in (LZ, BRM, NOT_PUB):
            self.assertIs(graphstore.get_fold_context(conn, "proj", slug)
                          ["members_changed"], False, slug)
        src ="proj/{}#area-main.bicep".format(LZ)
        good = "proj/{}#area-{}".format(BRM, KV)
        bad = "proj/{}#area-{}".format(NOT_PUB, KV)
        data = conn.execute("SELECT data FROM edges WHERE src_id=? AND dst_id=?",
                            (src, good)).fetchone()[0]
        graphstore.delete_edges(conn, [(src, good, "depends_on")])
        graphstore.upsert_edge(conn, src, bad, "depends_on", data=json.loads(data))
        agg = validate.validate_project(conn, "proj",
                                        graphstore.project_repos(conn, "proj"))
        self.assertFalse(agg["ok"])
        self.assertIn(bad, json.dumps(_member_check(agg, LZ, "edge_set")))


# --- M7: symbol moves are checked against an index ------------------------------

class _CountingList(list):
    iterations = 0

    def __iter__(self):
        type(self).iterations += 1
        return super().__iter__()


def _sym(path, change, name, commit, date):
    return {"path": path, "change": change, "lang": "python",
            "subkind": "function", "name": name, "commit": commit,
            "author": "alice", "date": date,
            "before": "def {}(): pass".format(name) if change == "drop" else None,
            "after": None if change == "drop" else "def {}(): pass".format(name)}


class TestMoveIndex(_Case):
    K = 120

    def _store(self):
        c1, c2 = "1" * 40, "2" * 40
        may = {"meta": {"owner": "o", "repo": "r", "from": MAY[0], "to": MAY[1]},
               "prs": [], "issues": [], "code_events": [],
               "commits": [{"sha": c1, "author": "alice", "date": "2026-05-02",
                            "message": "m", "files": ["a.py", "b.py"]}],
               "symbol_events": [
                   e for i in range(self.K) for e in (
                       _sym("a.py", "drop", "f{}".format(i), c1, "2026-05-02"),
                       _sym("b.py", "add", "f{}".format(i), c1, "2026-05-02"))]}
        june = {"meta": {"owner": "o", "repo": "r", "from": JUNE[0], "to": JUNE[1]},
                "prs": [], "issues": [], "code_events": [],
                "commits": [{"sha": c2, "author": "alice", "date": "2026-06-02",
                             "message": "m", "files": ["c.py"]}],
                "symbol_events": [_sym("c.py", "add", "f{}".format(i), c2,
                                       "2026-06-02") for i in range(self.K)]}
        return _store(may, june)

    def test_moves_are_supported_and_checked_without_a_scan_per_move(self):
        conn = self._store()
        self.assertClean(conn)
        bundle, why = validate._self_source_bundle(conn, "o", "r")
        self.assertIsNone(why)
        bundle["symbol_events"] = _CountingList(bundle["symbol_events"])
        _CountingList.iterations = 0
        c = validate.check_edge_set(conn, "o", "r", bundle)
        self.assertTrue(c["ok"], c)
        # every stored move (2K) is a cross-window one checked by
        # _move_problem; a scan per move iterates the events 2K+ times.
        self.assertLess(_CountingList.iterations, 40)


# --- m6: project mode is linear in the store ------------------------------------

def _n_member_store(n):
    members = {"o/m{}".format(i) for i in range(n)}
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    for i in range(n):
        slug = "o/m{}".format(i)
        b = _window(MAY[0], MAY[1], i + 1, "{:x}".format(i + 1) * 40)
        b["meta"]["repo"] = "m{}".format(i)
        b["prs"][0]["url"] = "https://github.com/{}/pull/{}".format(slug, i + 1)
        gather.fold_bundle(conn, b, project="p", repo=slug, members=members,
                           registry_by_slug={}, bicep_prefix_by_slug={})
    return conn


_FULL_SCANS = ("SELECT * FROM nodes", "SELECT id FROM nodes",
               "SELECT * FROM edges", "SELECT src_id, dst_id, edge_type FROM edges")


class TestProjectModeCost(unittest.TestCase):
    def _profile(self, n):
        conn = _n_member_store(n)
        stmts = []
        conn.set_trace_callback(stmts.append)
        calls = []
        real = extract.extract

        def counting(*a, **kw):
            calls.append(a[2])
            return real(*a, **kw)
        extract.extract = counting
        try:
            agg = validate.validate_project(conn, "p",
                                            graphstore.project_repos(conn, "p"))
        finally:
            extract.extract = real
            conn.set_trace_callback(None)
        self.assertTrue(agg["ok"], json.dumps(agg, indent=1)[:3000])
        full = [s for s in stmts if s.strip() in _FULL_SCANS]
        return len(full), calls

    def test_full_table_scans_do_not_grow_with_members(self):
        two, _ = self._profile(2)
        four, _ = self._profile(4)
        self.assertEqual(two, four)

    def test_each_member_is_self_sourced_once(self):
        _, calls = self._profile(3)
        self.assertEqual(sorted(calls), ["o/m0", "o/m1", "o/m2"])


# --- m7: project names are case-sensitive ---------------------------------------

class TestProjectCase(_Case):
    def test_projects_differing_only_in_case_do_not_collide(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        for project in ("Acme", "acme"):
            gather.fold_bundle(conn, _bundle(), project=project, repo="o/r")
        for project in ("Acme", "acme"):
            with self.subTest(project):
                self.assertClean(conn, project=project, repo="o/r")

    def test_repo_case_variants_inside_one_project_still_fail(self):
        conn = _store(_bundle())
        graphstore.upsert_node(conn, "o/R#milestone-9", "o", "R", "structure",
                               None, {"number": 9, "title": "x"})
        c = self.assertFailsOn(conn, "no_fabrication", "case-variant")
        self.assertIn("o/R#milestone-9", json.dumps(c["details"]))


if __name__ == "__main__":
    unittest.main()
