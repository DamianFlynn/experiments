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


if __name__ == "__main__":
    unittest.main()
