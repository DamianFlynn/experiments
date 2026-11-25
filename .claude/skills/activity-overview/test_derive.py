"""Phase 7b-1: the pure derivations are usable directly from `derive` (a leaf
module that imports neither link nor gather). These tests exercise derive's
public surface WITHOUT importing link, proving the write-path can derive the
same facts without a link dependency. Behavior parity with link is already
covered by test_link.py (which calls the same objects via re-export)."""

import unittest

import derive


class DeriveIsALeafModule(unittest.TestCase):
    def test_imports_no_forbidden_modules(self):
        # derive must be a leaf: its source must not import link or gather.
        # (A direct AST/source check avoids mutating global sys.modules, which
        # other test files in the run rely on.)
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(derive))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("link", imported)
        self.assertNotIn("gather", imported)


class ArtifactId(unittest.TestCase):
    def test_stable_prefix(self):
        self.assertEqual(derive.artifact_id("docs/x.md"), "art:docs/x.md")

    def test_none_path(self):
        self.assertEqual(derive.artifact_id(None), "art:")


class BuildArtifacts(unittest.TestCase):
    def test_classifies_and_folds_lifecycle(self):
        bundle = {
            "code_events": [
                {"path": "README.md", "change": "add", "commit": "c1",
                 "author": "a", "date": "2026-01-01"},
                {"path": "src/app.py", "change": "add", "commit": "c2",
                 "author": "a", "date": "2026-01-02"},
            ]
        }
        arts = derive.build_artifacts(bundle)
        self.assertIn("art:README.md", arts)
        self.assertEqual(arts["art:README.md"]["kind"], "readme")
        # untracked code path is ignored at file granularity
        self.assertNotIn("art:src/app.py", arts)
        self.assertEqual(arts["art:README.md"]["status"], "live")

    def test_empty(self):
        self.assertEqual(derive.build_artifacts({"code_events": []}), {})

    def test_file_lifecycle_carries_hunk_when_present(self):
        # Phase 10 slice-diffs: a code_event's bounded `hunk` rides onto the file
        # artifact's lifecycle entry (omit-when-empty otherwise).
        hunk = "@@ +1 @@\n-old\n+new"
        bundle = {"code_events": [
            {"path": "docs/x.md", "change": "modify", "commit": "c1",
             "author": "a", "date": "2026-01-01", "hunk": hunk},
            {"path": "README.md", "change": "add", "commit": "c2",
             "author": "a", "date": "2026-01-02"},  # no hunk -> key absent
        ]}
        arts = derive.build_artifacts(bundle)
        self.assertEqual(arts["art:docs/x.md"]["lifecycle"][0]["hunk"], hunk)
        self.assertNotIn("hunk", arts["art:README.md"]["lifecycle"][0])

    def test_rename_hunk_only_on_new_path_not_old(self):
        # A rename reuses one code_event for both sides; its `hunk` is the NEW file's
        # diff, so it must land on the new-path `add` ONLY, never the old-path remove.
        hunk = "@@ +1 @@\n+new content"
        bundle = {"code_events": [
            {"path": "docs/new.md", "old_path": "docs/old.md", "change": "rename",
             "commit": "c1", "author": "a", "date": "2026-01-01", "hunk": hunk},
        ]}
        arts = derive.build_artifacts(bundle)
        self.assertEqual(arts["art:docs/new.md"]["lifecycle"][0]["hunk"], hunk)
        self.assertNotIn("hunk", arts["art:docs/old.md"]["lifecycle"][0])


def _ev(change, path, n, old_path=None, author="a"):
    ev = {"path": path, "change": change, "commit": "c%d" % n, "author": author,
          "date": "2026-01-%02d" % n}
    if old_path:
        ev["old_path"] = old_path
    return ev


def _state(arts, path):
    a = arts[derive.artifact_id(path)]
    return (a["status"], a["replaced_by"], [e["event"] for e in a["lifecycle"]])


class ArtifactStatusFromLastEvent(unittest.TestCase):
    """#64: status is derived from the artifact's LAST lifecycle event. `replaced`
    is only true when that last event is a rename away to a TRACKED successor."""

    def test_rename_then_readd_old_path_is_live(self):
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "docs/b.md", 1, old_path="docs/a.md"),
            _ev("add", "docs/a.md", 2),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"), ("live", None, ["remove", "add"]))
        self.assertEqual(_state(arts, "docs/b.md"), ("live", None, ["add"]))

    def test_rename_to_untracked_target_is_removed_not_replaced(self):
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "src/x.txt", 1, old_path="docs/z.md"),
        ]})
        self.assertNotIn(derive.artifact_id("src/x.txt"), arts)
        self.assertEqual(_state(arts, "docs/z.md"), ("removed", None, ["remove"]))

    def test_rename_chain_each_points_to_direct_successor(self):
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "docs/b.md", 1, old_path="docs/a.md"),
            _ev("rename", "docs/c.md", 2, old_path="docs/b.md"),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"),
                         ("replaced", "art:docs/b.md", ["remove"]))
        self.assertEqual(_state(arts, "docs/b.md"),
                         ("replaced", "art:docs/c.md", ["add", "remove"]))
        self.assertEqual(_state(arts, "docs/c.md"), ("live", None, ["add"]))

    def test_rename_then_delete_target(self):
        # a stays `replaced` by its direct successor b (consumers walk the chain);
        # b's last event is a plain delete -> `removed`, no successor.
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "docs/b.md", 1, old_path="docs/a.md"),
            _ev("delete", "docs/b.md", 2),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"),
                         ("replaced", "art:docs/b.md", ["remove"]))
        self.assertEqual(_state(arts, "docs/b.md"), ("removed", None, ["add", "remove"]))

    def test_readd_after_delete_is_live(self):
        arts = derive.build_artifacts({"code_events": [
            _ev("add", "docs/a.md", 1),
            _ev("delete", "docs/a.md", 2),
            _ev("add", "docs/a.md", 3),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"),
                         ("live", None, ["add", "remove", "add"]))

    def test_readd_after_rename_then_renamed_away_again(self):
        # the LAST rename decides the successor, not the first.
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "docs/b.md", 1, old_path="docs/a.md"),
            _ev("add", "docs/a.md", 2),
            _ev("rename", "docs/c.md", 3, old_path="docs/a.md"),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"),
                         ("replaced", "art:docs/c.md", ["remove", "add", "remove"]))

    def test_rename_then_delete_of_readded_old_path_is_removed(self):
        arts = derive.build_artifacts({"code_events": [
            _ev("rename", "docs/b.md", 1, old_path="docs/a.md"),
            _ev("add", "docs/a.md", 2),
            _ev("delete", "docs/a.md", 3),
        ]})
        self.assertEqual(_state(arts, "docs/a.md"),
                         ("removed", None, ["remove", "add", "remove"]))

    def test_symbol_readded_in_source_file_is_not_replaced(self):
        # Same rule for symbol moves: a source symbol re-added in its own file
        # after moving is live again. match_symbol_moves treats the name as
        # ambiguous (added in two files), so no move link / no `replaced`.
        def sym(change, path, n):
            return {"lang": "bicep", "subkind": "resource", "name": "foo",
                    "change": change, "path": path, "commit": "c%d" % n,
                    "author": "a", "date": "2026-01-%02d" % n,
                    "before": None, "after": None}
        bundle = {"code_events": [], "symbol_events": [
            sym("drop", "a.bicep", 1), sym("add", "b.bicep", 1),
            sym("add", "a.bicep", 2)]}
        bundle["artifacts"] = derive.build_artifacts(bundle)
        derive.link_symbol_identity(bundle)
        src = bundle["artifacts"]["a.bicep#bicep:resource:foo"]
        self.assertEqual((src["status"], src["replaced_by"]), ("live", None))
        self.assertEqual(bundle["symbol_moves"]["links"], [])

    def test_readded_file_does_not_count_as_authored_then_removed(self):
        bundle = {"code_events": [
            _ev("add", "docs/a.md", 1, author="ann"),
            _ev("rename", "docs/b.md", 2, old_path="docs/a.md", author="ann"),
            _ev("add", "docs/a.md", 3, author="ann"),
        ]}
        bundle["artifacts"] = derive.build_artifacts(bundle)
        bundle["people"] = {"ann": {}}
        derive.annotate_people_profile(bundle)
        # a.md is live again; b.md is live -> nothing authored was removed.
        self.assertEqual(bundle["people"]["ann"]["authored_then_removed"], 0)


class AreaIndexAndAttribution(unittest.TestCase):
    def test_area_index_and_lookup(self):
        cg = {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]}
        idx = derive.area_index(cg)
        self.assertEqual(idx, {"docs/x.md": "area:a"})
        self.assertEqual(derive._area_for_path("docs/x.md", idx), "area:a")
        self.assertIsNone(derive._area_for_path("nope", idx))

    def test_attribute_code_areas_fills_nulls(self):
        bundle = {
            "code_graph": {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]},
            "artifacts": {"art:docs/x.md": {"path": "docs/x.md", "code_area": None}},
            "feature_deltas": [{"artifact": "art:docs/x.md", "name": "x.md",
                                "area": None}],
        }
        idx = derive.attribute_code_areas(bundle)
        self.assertEqual(bundle["artifacts"]["art:docs/x.md"]["code_area"], "area:a")
        self.assertEqual(bundle["feature_deltas"][0]["area"], "area:a")
        self.assertEqual(idx, {"docs/x.md": "area:a"})


class Modules(unittest.TestCase):
    def test_build_modules_counts(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {"commits": [
            {"sha": "c1", "pr": 1, "files": ["docs/x.md"]},
            {"sha": "c2", "pr": 1, "files": ["docs/x.md", "other"]},
        ]}
        derive.build_modules(bundle, idx)
        self.assertEqual(bundle["modules"],
                         {"area:a": {"commits": 2, "prs": 1, "files_changed": 1}})


class People(unittest.TestCase):
    def test_attribute_people_areas(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {
            "commits": [{"sha": "c1", "pr": 1, "author": "alice",
                         "files": ["docs/x.md"]}],
            "prs": [{"number": 1, "reviewers": ["bob"]}],
        }
        derive.attribute_people_areas(bundle, idx)
        self.assertEqual(bundle["people"]["alice"]["areas"], ["area:a"])
        self.assertEqual(bundle["people"]["bob"]["areas"], ["area:a"])


class EnumerateParticipants(unittest.TestCase):
    """The shared enumerator returns the FULL participant set (anti-drift)."""

    def test_includes_pure_participants_and_contributors(self):
        idx = {"docs/x.md": "area:a"}
        bundle = {
            "code_graph": {"areas": [{"id": "area:a", "paths": ["docs/x.md"]}]},
            "commits": [{"sha": "c1", "pr": 1, "author": "alice",
                         "files": ["docs/x.md"]}],
            "prs": [{"number": 1, "author": "alice", "merged_by": "mallory",
                     "reviewers": ["bob"],
                     "comments_list": [{"author": "erin"}],
                     "review_comments": [{"author": "frank"}]}],
            "issues": [{"number": 9, "author": "grace",
                        "comments_list": [{"author": "heidi"}]}],
        }
        ppl = derive.enumerate_participants(bundle, idx)
        # everyone with any contribution edge appears.
        self.assertEqual(
            set(ppl),
            {"alice", "mallory", "bob", "erin", "frank", "grace", "heidi"})
        # contributors carry modules/areas; pure participants are empty.
        self.assertEqual(ppl["alice"]["areas"], ["area:a"])
        self.assertEqual(ppl["bob"]["areas"], ["area:a"])  # reviewer inherits PR areas
        self.assertEqual(ppl["erin"]["areas"], [])
        self.assertEqual(ppl["grace"]["modules"], [])

    def test_bots_tagged_not_dropped(self):
        bundle = {"commits": [], "prs": [
            {"number": 1, "author": "dependabot[bot]",
             "comments_list": [{"author": "github-actions"}]}],
            "issues": [{"number": 2, "author": "real-human"}]}
        ppl = derive.enumerate_participants(bundle)
        self.assertIn("dependabot[bot]", ppl)
        self.assertTrue(ppl["dependabot[bot]"]["is_bot"])
        self.assertTrue(ppl["github-actions"]["is_bot"])
        self.assertFalse(ppl["real-human"]["is_bot"])

    def test_is_bot_login_patterns(self):
        for b in ("foo[bot]", "copilot-swe-agent[bot]", "github-actions",
                  "microsoft-github-policy-service", "release-organizer",
                  "copilot-anything"):
            self.assertTrue(derive.is_bot_login(b), b)
        for h in ("alice", "bob", "robot-cat", "", None):
            self.assertFalse(derive.is_bot_login(h), h)


class SymbolMoves(unittest.TestCase):
    def test_unique_name_move_medium(self):
        events = [
            {"lang": "bicep", "subkind": "resource", "name": "foo",
             "change": "drop", "path": "a.bicep"},
            {"lang": "bicep", "subkind": "resource", "name": "foo",
             "change": "add", "path": "b.bicep"},
        ]
        moves = derive.match_symbol_moves(events)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["confidence"], "medium")
        self.assertEqual(moves[0]["basis"], "unique_name")

    def test_link_symbol_identity_records_summary(self):
        bundle = {
            "symbol_events": [
                {"lang": "bicep", "subkind": "resource", "name": "foo",
                 "change": "drop", "path": "a.bicep"},
                {"lang": "bicep", "subkind": "resource", "name": "foo",
                 "change": "add", "path": "b.bicep"},
            ],
            "code_events": [],
            "artifacts": {
                "a.bicep#bicep:resource:foo": {"status": "live"},
                "b.bicep#bicep:resource:foo": {"status": "live"},
            },
        }
        derive.link_symbol_identity(bundle)
        self.assertEqual(bundle["symbol_moves"]["by_confidence"]["medium"], 1)
        self.assertEqual(
            bundle["artifacts"]["a.bicep#bicep:resource:foo"]["status"], "replaced")


class ReviewRoundsAndReopenCount(unittest.TestCase):
    """Phase 10 slice 1: derived read-side counts from the review/lifecycle
    submissions persisted on the bundle (pure, like build_modules)."""

    def test_review_rounds_counts_and_state_sequence(self):
        bundle = {"prs": [
            {"number": 7, "reviews": [
                {"state": "changes_requested", "submitted_at": "2026-01-01"},
                {"state": "approved", "submitted_at": "2026-01-02"},
            ]},
            {"number": 8},  # no reviews -> absent from the map
        ]}
        derive.annotate_review_rounds(bundle)
        self.assertEqual(bundle["prs"][0]["review_rounds"],
                         {"count": 2, "states": ["changes_requested", "approved"]})
        self.assertNotIn("review_rounds", bundle["prs"][1])

    def test_review_rounds_orders_by_submitted_at(self):
        bundle = {"prs": [{"number": 1, "reviews": [
            {"state": "approved", "submitted_at": "2026-01-05"},
            {"state": "commented", "submitted_at": "2026-01-02"},
        ]}]}
        derive.annotate_review_rounds(bundle)
        self.assertEqual(bundle["prs"][0]["review_rounds"]["states"],
                         ["commented", "approved"])

    # --- issue #63: precise review-round definition ------------------------
    @staticmethod
    def _rounds(reviews, author="alice"):
        bundle = {"prs": [{"number": 1, "author": author, "reviews": reviews}]}
        derive.annotate_review_rounds(bundle)
        return bundle["prs"][0].get("review_rounds")

    def test_review_rounds_exclude_pending_and_author_replies(self):
        # observed before the fix: count 3, ['pending','approved','commented']
        got = self._rounds([
            {"id": 3, "author": "bob", "state": "pending", "submitted_at": None},
            {"id": 1, "author": "carol", "state": "approved",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 2, "author": "alice", "state": "commented",
             "submitted_at": "2026-01-03T00:00:00Z"},
        ])
        self.assertEqual(got, {"count": 1, "states": ["approved"]})

    def test_review_rounds_only_author_or_pending_omits_key(self):
        self.assertIsNone(self._rounds([
            {"id": 1, "author": "alice", "state": "commented",
             "submitted_at": "2026-01-03T00:00:00Z"},
            {"id": 2, "author": "bob", "state": "pending", "submitted_at": None},
        ]))

    def test_review_rounds_changes_requested_then_rereview_opens_round(self):
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "changes_requested",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "alice", "state": "commented",
             "submitted_at": "2026-01-01T12:00:00Z"},  # author reply: ignored
            {"id": 3, "author": "bob", "state": "changes_requested",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 4, "author": "bob", "state": "approved",
             "submitted_at": "2026-01-03T00:00:00Z"},
        ])
        # round 1: bob requests changes; round 2: bob re-reviews and requests
        # changes again (a repeat reviewer starts a new block); round 3: bob
        # approves.
        self.assertEqual(got, {"count": 3, "states": [
            "changes_requested", "changes_requested", "approved"]})

    def test_review_rounds_each_rereview_after_changes_opens_round(self):
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "changes_requested",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "bob", "state": "commented",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 3, "author": "bob", "state": "changes_requested",
             "submitted_at": "2026-01-03T00:00:00Z"},
            {"id": 4, "author": "bob", "state": "approved",
             "submitted_at": "2026-01-04T00:00:00Z"},
        ])
        self.assertEqual(got["count"], 3)

    def test_review_rounds_concurrent_changes_requests_are_one_round(self):
        # bob and carol both request changes before any re-review -> one block;
        # the first later review that is not a change request opens round 2.
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "changes_requested",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "carol", "state": "changes_requested",
             "submitted_at": "2026-01-01T01:00:00Z"},
            {"id": 3, "author": "bob", "state": "commented",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 4, "author": "carol", "state": "approved",
             "submitted_at": "2026-01-03T00:00:00Z"},
        ])
        self.assertEqual(got["count"], 2)

    def test_review_rounds_no_change_request_is_one_round(self):
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "commented",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "carol", "state": "approved",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 3, "author": "bob", "state": "approved",
             "submitted_at": "2026-01-03T00:00:00Z"},
        ])
        self.assertEqual(got, {"count": 1,
                               "states": ["commented", "approved", "approved"]})

    def test_review_rounds_trailing_change_request_does_not_open_round(self):
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "approved",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "carol", "state": "changes_requested",
             "submitted_at": "2026-01-02T00:00:00Z"},
        ])
        self.assertEqual(got["count"], 1)

    def test_review_rounds_state_case_insensitive(self):
        got = self._rounds([
            {"id": 1, "author": "bob", "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "bob", "state": "PENDING", "submitted_at": None},
            {"id": 3, "author": "bob", "state": "APPROVED",
             "submitted_at": "2026-01-02T00:00:00Z"},
        ])
        self.assertEqual(got, {"count": 2,
                               "states": ["changes_requested", "approved"]})

    def test_review_rounds_unknown_pr_author_keeps_all_submitted(self):
        got = self._rounds([
            {"id": 1, "author": "alice", "state": "commented",
             "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "author": "bob", "state": "pending", "submitted_at": None},
        ], author=None)
        self.assertEqual(got, {"count": 1, "states": ["commented"]})

    def test_reopen_count_on_pr_and_issue(self):
        bundle = {
            "prs": [{"number": 7, "lifecycle": [
                {"event": "reopened"}, {"event": "ready_for_review"},
                {"event": "reopened"}]}],
            "issues": [
                {"number": 3, "lifecycle": [{"event": "reopened"}]},
                {"number": 4, "lifecycle": [{"event": "closed"}]},
                {"number": 5},  # no lifecycle
            ],
        }
        derive.annotate_reopen_count(bundle)
        self.assertEqual(bundle["prs"][0]["reopen_count"], 2)
        self.assertEqual(bundle["issues"][0]["reopen_count"], 1)
        # zero reopens -> key omitted (no fabricated zero).
        self.assertNotIn("reopen_count", bundle["issues"][1])
        self.assertNotIn("reopen_count", bundle["issues"][2])


class TestPeopleProfile(unittest.TestCase):
    def _people(self):
        return {
            "alice": {"modules": [], "areas": ["avm/res/network"], "is_bot": False},
            "bob": {"modules": [], "areas": ["avm/res/storage"], "is_bot": False},
            "ci[bot]": {"modules": [], "areas": [], "is_bot": True},
        }

    def test_counts_and_merge_rate(self):
        bundle = {
            "people": self._people(),
            "prs": [
                {"number": 1, "author": "alice", "merged": True,
                 "created_at": "2026-01-01", "merged_at": "2026-01-02",
                 "merged_by": "bob", "reviewers": ["bob"], "reviews": []},
                {"number": 2, "author": "alice", "merged": False,
                 "created_at": "2026-01-03", "reviewers": []},
            ],
            "commits": [
                {"sha": "c1", "author": "alice", "date": "2026-01-01"},
                {"sha": "c2", "author": "alice", "date": "2026-01-02"},
            ],
            "issues": [{"number": 9, "author": "alice", "created_at": "2026-01-04"}],
        }
        derive.annotate_people_profile(bundle)
        a = bundle["people"]["alice"]
        self.assertEqual(a["prs_authored"], 2)
        self.assertEqual(a["prs_merged"], 1)
        self.assertEqual(a["merge_rate"], 0.5)
        self.assertEqual(a["commits_authored"], 2)
        self.assertEqual(a["issues_opened"], 1)
        # bob reviewed PR 1, merged PR 1
        b = bundle["people"]["bob"]
        self.assertEqual(b["prs_reviewed"], 1)
        self.assertEqual(b["prs_authored"], 0)
        # existing keys preserved
        self.assertEqual(a["areas"], ["avm/res/network"])
        self.assertFalse(a["is_bot"])

    def test_merge_rate_none_when_no_authored(self):
        bundle = {"people": self._people(), "prs": [], "commits": [], "issues": []}
        derive.annotate_people_profile(bundle)
        self.assertIsNone(bundle["people"]["alice"]["merge_rate"])
        self.assertEqual(bundle["people"]["alice"]["prs_authored"], 0)

    def test_review_latency_median(self):
        # alice reviews two PRs: 2-day and 4-day latency -> median 3.0
        bundle = {
            "people": self._people(),
            "prs": [
                {"number": 1, "author": "bob", "created_at": "2026-01-01",
                 "reviewers": ["alice"],
                 "reviews": [{"author": "alice", "state": "approved",
                              "submitted_at": "2026-01-03"}]},
                {"number": 2, "author": "bob", "created_at": "2026-01-01",
                 "reviewers": ["alice"],
                 "reviews": [{"author": "alice", "state": "approved",
                              "submitted_at": "2026-01-05"}]},
            ],
            "commits": [], "issues": [],
        }
        derive.annotate_people_profile(bundle)
        self.assertEqual(bundle["people"]["alice"]["review_latency_days"], 3.0)

    def test_review_latency_none_when_no_samples(self):
        bundle = {
            "people": self._people(),
            "prs": [{"number": 1, "author": "bob", "created_at": "2026-01-01",
                     "reviewers": ["alice"], "reviews": []}],
            "commits": [], "issues": [],
        }
        derive.annotate_people_profile(bundle)
        self.assertIsNone(bundle["people"]["alice"]["review_latency_days"])
        self.assertEqual(bundle["people"]["alice"]["prs_reviewed"], 1)

    def test_first_seen_last_active(self):
        bundle = {
            "people": self._people(),
            "prs": [{"number": 1, "author": "alice", "merged": False,
                     "created_at": "2026-03-15", "reviewers": []}],
            "commits": [{"sha": "c1", "author": "alice", "date": "2026-01-05"}],
            "issues": [{"number": 9, "author": "alice",
                        "created_at": "2026-06-01T12:00:00Z"}],
        }
        derive.annotate_people_profile(bundle)
        a = bundle["people"]["alice"]
        self.assertEqual(a["first_seen"], "2026-01-05")
        self.assertEqual(a["last_active"], "2026-06-01")

    def test_first_seen_none_when_no_dates(self):
        bundle = {"people": self._people(), "prs": [], "commits": [], "issues": []}
        derive.annotate_people_profile(bundle)
        self.assertIsNone(bundle["people"]["alice"]["first_seen"])
        self.assertIsNone(bundle["people"]["alice"]["last_active"])

    def test_comment_timestamps_feed_first_last_active(self):
        # A login who ONLY commented (PR conversation, PR review comment, or issue
        # comment) still gets first_seen/last_active from those timestamps.
        bundle = {
            "people": self._people(),
            "prs": [{"number": 1, "author": "alice", "merged": False,
                     "created_at": "2026-02-01", "reviewers": [],
                     "comments_list": [{"author": "bob", "created_at": "2026-02-02"}],
                     "review_comments": [{"author": "bob", "created_at": "2026-02-05"}]}],
            "issues": [{"number": 9, "author": "alice", "created_at": "2026-02-01",
                        "comments_list": [{"author": "bob",
                                           "created_at": "2026-01-20"}]}],
            "commits": [],
        }
        derive.annotate_people_profile(bundle)
        bob = bundle["people"]["bob"]
        self.assertEqual(bob["prs_authored"], 0)        # bob only commented
        self.assertEqual(bob["first_seen"], "2026-01-20")
        self.assertEqual(bob["last_active"], "2026-02-05")

    def test_authored_by_kind(self):
        bundle = {
            "people": self._people(),
            "prs": [], "commits": [], "issues": [],
            "artifacts": {
                "art:examples/e.bicep": {"kind": "example", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                "art:docs/d.md": {"kind": "doc", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                "art:README.md": {"kind": "readme", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                "sym1": {"kind": "symbol", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                # bob added, not alice -> not counted for alice
                "art:examples/f.bicep": {"kind": "example", "status": "live",
                    "lifecycle": [{"author": "bob", "event": "add"}]},
                # alice only CHANGED, not added -> not counted
                "sym2": {"kind": "symbol", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "change"}]},
            },
        }
        derive.annotate_people_profile(bundle)
        a = bundle["people"]["alice"]
        self.assertEqual(a["examples_authored"], 1)
        self.assertEqual(a["docs_authored"], 2)  # doc + readme
        self.assertEqual(a["symbols_authored"], 1)
        self.assertEqual(bundle["people"]["bob"]["examples_authored"], 1)

    def test_authored_then_removed(self):
        bundle = {
            "people": self._people(),
            "prs": [], "commits": [], "issues": [],
            "artifacts": {
                "art:examples/e.bicep": {"kind": "example", "status": "replaced",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                "art:docs/d.md": {"kind": "doc", "status": "removed",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
                "art:docs/keep.md": {"kind": "doc", "status": "live",
                    "lifecycle": [{"author": "alice", "event": "add"}]},
            },
        }
        derive.annotate_people_profile(bundle)
        self.assertEqual(bundle["people"]["alice"]["authored_then_removed"], 2)

    def test_stale_owned(self):
        bundle = {
            "people": self._people(),
            "prs": [], "commits": [], "issues": [],
            "code_owners": {
                "avm/res/network/": ["alice", "bob"],
                "avm/res/storage/": ["bob"],
            },
            "trains": [
                {"id": "t1", "code_areas": ["avm/res/network/foo"],
                 "effort": {"stalled": True}},
                {"id": "t2", "code_areas": ["avm/res/storage/bar"],
                 "effort": {"stalled": False}},
            ],
        }
        derive.annotate_people_profile(bundle)
        # alice owns network prefix which has a stalled train
        self.assertEqual(bundle["people"]["alice"]["stale_owned"], 1)
        # bob owns network (stalled) + storage (not stalled) -> 1
        self.assertEqual(bundle["people"]["bob"]["stale_owned"], 1)

    def test_build_halls_ranking_and_bot_exclusion(self):
        bundle = {"people": {
            "alice": {"areas": ["avm/res/network"], "is_bot": False,
                      "prs_merged": 3, "prs_reviewed": 2, "commits_authored": 5},
            "bob": {"areas": ["avm/res/storage"], "is_bot": False,
                    "prs_merged": 1, "prs_reviewed": 0, "commits_authored": 1},
            "ci[bot]": {"areas": [], "is_bot": True,
                        "prs_merged": 10, "prs_reviewed": 10,
                        "commits_authored": 10},
        }}
        derive.build_halls(bundle)
        fame = bundle["halls"]["fame"]
        self.assertEqual([e["login"] for e in fame], ["alice", "bob"])
        # alice: 3*2 + 2 + 5 = 13
        self.assertEqual(fame[0]["score"], 13)
        self.assertEqual(fame[0]["areas"], ["avm/res/network"])
        # bob: 1*2 + 0 + 1 = 3
        self.assertEqual(fame[1]["score"], 3)
        self.assertEqual(fame[1]["prs_merged"], 1)
        # bot excluded despite top score
        self.assertNotIn("ci[bot]", [e["login"] for e in fame])
        # halls.internal/shame/blame intentionally omitted
        self.assertEqual(set(bundle["halls"]), {"fame"})

    def test_build_halls_empty_when_no_score(self):
        bundle = {"people": {
            "alice": {"areas": [], "is_bot": False, "prs_merged": 0,
                      "prs_reviewed": 0, "commits_authored": 0},
        }}
        derive.build_halls(bundle)
        self.assertEqual(bundle["halls"]["fame"], [])


def _issue(number, **kw):
    """Build an open issue with sensible defaults; override via kwargs."""
    base = {
        "number": number,
        "url": f"https://github.com/o/r/issues/{number}",
        "state": "open",
        "created_at": None,
        "updated_at": None,
        "reactions": {},
        "comments_list": [],
    }
    base.update(kw)
    return base


# A reference date; staleness/age computed relative to this.
_META = {"ref_date": "2026-06-06"}


def _one(flow):
    """Single flow entry helper."""
    assert len(flow) == 1, flow
    return flow[0]


class TestFlow(unittest.TestCase):
    def test_blocked_fires(self):
        # #60: only OPEN blockers block, so the blockers must be in the bundle and open.
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[2, 3]), _issue(2), _issue(3)]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "blocked")
        self.assertEqual(e["blocked_by"], [2, 3])
        self.assertEqual(e["blocked_by_unknown"], [])
        self.assertIn("blocked by 2 issue(s)", e["signals"])

    def test_upvoted_but_ignored_fires(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-05-01", updated_at="2026-04-01",
                   reactions={"+1": 7, "total": 9})]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "upvoted_but_ignored")
        self.assertEqual(e["reactions"], 9)
        self.assertIn("9 reactions", e["signals"])

    def test_traction_then_abandoned_fires(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-05-01", updated_at="2026-04-01",
                   comments_list=[{}] * 6)]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "traction_then_abandoned")
        self.assertIn("6 comments", e["signals"])

    def test_hung_fires(self):
        # old (>=60d) AND stale (>=30d since update, #60), not upvoted / traction.
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-01-01", updated_at="2026-05-01")]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "hung")
        self.assertGreaterEqual(e["age_days"], 60)
        self.assertIn(f"open {e['age_days']}d", e["signals"])

    def test_precedence_blocked_beats_upvoted(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[9], created_at="2026-05-01",
                   updated_at="2026-04-01", reactions={"+1": 7, "total": 7}),
            _issue(9)]}  # the open blocker (healthy itself -> not emitted)
        derive.build_flow(bundle)
        self.assertEqual(_one(bundle["flow"])["state"], "blocked")

    def test_open_linked_pr_exempts_from_starved_pathologies(self):
        # would be upvoted_but_ignored, but has an OPEN linked PR -> healthy/omitted.
        bundle = {"meta": _META,
                  "prs": [{"number": 50, "state": "open", "merged": False,
                           "closes": [1], "crossref_issues": []}],
                  "issues": [
                      _issue(1, created_at="2026-05-01", updated_at="2026-04-01",
                             reactions={"+1": 7, "total": 7},
                             comments_list=[{}] * 6)]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_open_linked_pr_via_crossref_exempts_hung(self):
        bundle = {"meta": _META,
                  "prs": [{"number": 50, "state": "open", "merged": False,
                           "closes": [], "crossref_issues": [1]}],
                  "issues": [
                      _issue(1, created_at="2026-01-01", updated_at="2026-05-01")]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_merged_or_closed_pr_does_not_exempt(self):
        # a merged PR is not "in flight" -> issue still classified.
        bundle = {"meta": _META,
                  "prs": [{"number": 50, "state": "closed", "merged": True,
                           "closes": [1], "crossref_issues": []}],
                  "issues": [
                      _issue(1, created_at="2026-05-01", updated_at="2026-04-01",
                             reactions={"+1": 7, "total": 7})]}
        derive.build_flow(bundle)
        self.assertEqual(_one(bundle["flow"])["state"], "upvoted_but_ignored")

    def test_healthy_is_omitted(self):
        # recent, no blockers, low reactions/comments, young.
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-06-01", updated_at="2026-06-05")]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_closed_issues_ignored(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, state="closed", blocked_by=[2])]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_age_days_none_when_no_created_at(self):
        # blocked (so it emits) but no created_at -> age_days None, no "open Nd".
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[2], created_at=None), _issue(2)]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertIsNone(e["age_days"])
        self.assertFalse(any(s.startswith("open ") for s in e["signals"]))

    def test_meta_to_fallback(self):
        bundle = {"meta": {"to": "2026-06-06"}, "issues": [
            _issue(1, created_at="2026-01-01", updated_at="2026-05-01")]}
        derive.build_flow(bundle)
        self.assertEqual(_one(bundle["flow"])["state"], "hung")

    def test_sorted_by_number(self):
        bundle = {"meta": _META, "issues": [
            _issue(3, blocked_by=[9]),
            _issue(1, blocked_by=[9]),
            _issue(2, blocked_by=[9]),
            _issue(9)]}  # the open blocker (healthy itself -> not emitted)
        derive.build_flow(bundle)
        self.assertEqual([e["number"] for e in bundle["flow"]], [1, 2, 3])

    def test_not_stale_upvoted_is_healthy(self):
        # high upvotes but recently updated -> not stale -> not a pathology.
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-06-01", updated_at="2026-06-05",
                   reactions={"+1": 9, "total": 9})]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    # -- #60: blocker state + hung recency ------------------------------------

    def test_closed_blocker_does_not_block_falls_through(self):
        # #7 "blocked by #8", #8 closed completed -> not blocked; classified by its
        # own signals (here upvoted_but_ignored, which `blocked` used to mask).
        bundle = {"meta": _META, "issues": [
            _issue(7, blocked_by=[8], created_at="2026-05-01",
                   updated_at="2026-04-01", reactions={"+1": 7, "total": 7}),
            _issue(8, state="closed")]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "upvoted_but_ignored")
        self.assertEqual(e["blocked_by"], [])
        self.assertEqual(e["blocked_by_unknown"], [])
        self.assertFalse(any(s.startswith("blocked by") for s in e["signals"]))

    def test_closed_blocker_only_and_otherwise_healthy_is_omitted(self):
        bundle = {"meta": _META, "issues": [
            _issue(7, blocked_by=[8], created_at="2026-06-01",
                   updated_at="2026-06-05"),
            _issue(8, state="closed")]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_closed_blocker_no_longer_masks_hung(self):
        bundle = {"meta": _META, "issues": [
            _issue(7, blocked_by=[8], created_at="2026-01-01",
                   updated_at="2026-04-01"),
            _issue(8, state="closed")]}
        derive.build_flow(bundle)
        self.assertEqual(_one(bundle["flow"])["state"], "hung")

    def test_mixed_open_and_closed_blockers_counts_only_open(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[2, 3]), _issue(2), _issue(3, state="closed")]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "blocked")
        self.assertEqual(e["blocked_by"], [2])
        self.assertIn("blocked by 1 issue(s)", e["signals"])

    def test_blocker_absent_from_bundle_is_unknown_not_blocking(self):
        # #9 is not a gathered issue: its state is unknown. It does not make #1
        # `blocked`, but it is reported (never silently dropped) on an emitted entry.
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[9], created_at="2026-01-01",
                   updated_at="2026-04-01")]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "hung")
        self.assertEqual(e["blocked_by"], [])
        self.assertEqual(e["blocked_by_unknown"], [9])
        self.assertIn("1 blocker(s) of unknown state", e["signals"])

    def test_unknown_blocker_alongside_open_blocker(self):
        bundle = {"meta": _META, "issues": [
            _issue(1, blocked_by=[2, 9]), _issue(2)]}
        derive.build_flow(bundle)
        e = _one(bundle["flow"])
        self.assertEqual(e["state"], "blocked")
        self.assertEqual(e["blocked_by"], [2])
        self.assertEqual(e["blocked_by_unknown"], [9])
        self.assertIn("blocked by 1 issue(s)", e["signals"])
        self.assertIn("1 blocker(s) of unknown state", e["signals"])

    def test_hung_requires_staleness(self):
        # open ~150 days, updated (commented) yesterday -> not hung (#60).
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-01-07", updated_at="2026-06-05",
                   comments_list=[{}])]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_hung_needs_known_recency(self):
        # no updated_at -> recency unknown -> not stale -> not hung.
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-01-01", updated_at=None)]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])

    def test_hung_staleness_boundary(self):
        # updated exactly FLOW_STALE_DAYS (30) before ref -> stale -> hung; 29 -> not.
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-01-01", updated_at="2026-05-07")]}
        derive.build_flow(bundle)
        self.assertEqual(_one(bundle["flow"])["state"], "hung")
        bundle = {"meta": _META, "issues": [
            _issue(1, created_at="2026-01-01", updated_at="2026-05-08")]}
        derive.build_flow(bundle)
        self.assertEqual(bundle["flow"], [])


class TestBlockers(unittest.TestCase):
    def test_in_degree_ranking_and_sort(self):
        # #60: only open blockers, counting only open blocked issues -> every issue
        # here (blockers and dependants) is open and present in the bundle.
        dependants = [{"number": n, "url": "u%d" % n, "state": "open"}
                      for n in (10, 11, 20, 21, 22, 30, 40, 41)]
        bundle = {"issues": [
            {"number": 1, "url": "u1", "state": "open", "blocking": [10, 11]},
            {"number": 2, "url": "u2", "state": "open", "blocking": [20, 21, 22]},
            {"number": 3, "url": "u3", "state": "open", "blocking": [30]},
            {"number": 4, "url": "u4", "state": "open", "blocking": [40, 41]},
            {"number": 5, "url": "u5", "state": "open"},
        ] + dependants}
        derive.build_blockers(bundle)
        out = bundle["blockers"]
        # sort: blocks_count desc, then number asc -> 2(3), 1(2), 4(2), 3(1)
        self.assertEqual([(e["number"], e["blocks_count"]) for e in out],
                         [(2, 3), (1, 2), (4, 2), (3, 1)])
        self.assertEqual(out[0]["blocks"], [20, 21, 22])

    def test_dedup_blocks(self):
        bundle = {"issues": [
            {"number": 1, "url": "u1", "state": "open", "blocking": [10, 10, 11]},
            {"number": 10, "url": "u10", "state": "open"},
            {"number": 11, "url": "u11", "state": "open"}]}
        derive.build_blockers(bundle)
        e = bundle["blockers"][0]
        self.assertEqual(e["blocks"], [10, 11])
        self.assertEqual(e["blocks_count"], 2)

    def test_empty_when_no_blocking(self):
        bundle = {"issues": [{"number": 1, "url": "u1"}]}
        derive.build_blockers(bundle)
        self.assertEqual(bundle["blockers"], [])

    # -- #60 -----------------------------------------------------------------

    def test_closed_blocker_excluded(self):
        bundle = {"issues": [
            {"number": 8, "url": "u8", "state": "closed", "blocking": [7]},
            {"number": 7, "url": "u7", "state": "open"}]}
        derive.build_blockers(bundle)
        self.assertEqual(bundle["blockers"], [])

    def test_closed_and_unknown_dependants_not_counted(self):
        bundle = {"issues": [
            {"number": 1, "url": "u1", "state": "open", "blocking": [2, 3, 4]},
            {"number": 2, "url": "u2", "state": "open"},
            {"number": 3, "url": "u3", "state": "closed"}]}  # #4 not in bundle
        derive.build_blockers(bundle)
        e = bundle["blockers"][0]
        self.assertEqual(e["blocks"], [2])
        self.assertEqual(e["blocks_count"], 1)

    def test_blocker_with_no_open_dependants_omitted(self):
        bundle = {"issues": [
            {"number": 1, "url": "u1", "state": "open", "blocking": [3, 4]},
            {"number": 3, "url": "u3", "state": "closed"}]}
        derive.build_blockers(bundle)
        self.assertEqual(bundle["blockers"], [])


if __name__ == "__main__":
    unittest.main()
