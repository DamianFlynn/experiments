"""CODEOWNERS parsing + ownership matching (issue #65).

One matcher (derive.compile_codeowners / codeowners_owners / area_owners) backs
both the write path (gather.fold_bundle `owns` edges) and the read path
(derive.annotate_people_profile `stale_owned`). These tests pin GitHub's
CODEOWNERS semantics: gitignore-style patterns, last matching rule wins, inline
comments and GitLab section headers ignored, owner-less rules un-assign, and the
rule ORDER surviving fold -> store -> extract.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import validate  # noqa: E402

ISSUE_SCENARIO_1 = """\
*              @carol
/modules/xy/   @dave
*.tf           @erin
modules/x      @bob
"""


def _owners(rules_value, path, is_dir=False):
    return derive.codeowners_owners(
        derive.compile_codeowners(rules_value), path, is_dir)


def _rule(pattern, *owners):
    return {"pattern": pattern, "owners": list(owners)}


class TestParseCodeownersSyntax(unittest.TestCase):
    def test_inline_comment_is_not_an_owner(self):
        rules = gather.parse_codeowners("docs/  @alice @bob   # docs team\n")
        self.assertEqual(rules, [_rule("docs/", "alice", "bob")])

    def test_inline_comment_only_leaves_ownerless_rule(self):
        rules = gather.parse_codeowners("docs/  # nobody\n")
        self.assertEqual(rules, [_rule("docs/")])

    def test_section_headers_ignored(self):
        text = ("[Docs]\n"
                "docs/ @alice\n"
                "^[Optional][2] @zed\n"
                "[Backend] @team-lead\n"
                "src/ @bob\n")
        self.assertEqual(gather.parse_codeowners(text),
                         [_rule("docs/", "alice"), _rule("src/", "bob")])

    def test_order_and_duplicates_preserved(self):
        text = "b/ @x\na/ @y\nb/ @z\n"
        self.assertEqual([r["pattern"] for r in gather.parse_codeowners(text)],
                         ["b/", "a/", "b/"])

    def test_email_and_team_owners(self):
        rules = gather.parse_codeowners("* @org/team dev@example.com\n")
        self.assertEqual(rules, [_rule("*", "org/team", "dev@example.com")])

    def test_escaped_space_in_pattern(self):
        rules = gather.parse_codeowners(r"my\ dir/ @a" + "\n")
        self.assertEqual(rules, [_rule(r"my\ dir/", "a")])
        self.assertEqual(_owners(rules, "my dir/file.txt"), ["a"])


class TestMatcher(unittest.TestCase):
    def test_leading_slash_anchors_at_root(self):
        rules = [_rule("/avm/res/network/", "alice")]
        self.assertEqual(_owners(rules, "avm/res/network/vnet/main.bicep"), ["alice"])
        self.assertEqual(_owners(rules, "x/avm/res/network/vnet/main.bicep"), [])

    def test_middle_slash_anchors_too(self):
        rules = [_rule("docs/api", "a")]
        self.assertEqual(_owners(rules, "docs/api/x.md"), ["a"])
        self.assertEqual(_owners(rules, "src/docs/api/x.md"), [])

    def test_bare_name_matches_any_depth(self):
        rules = [_rule("apps/", "a"), _rule("*.tf", "t")]
        self.assertEqual(_owners(rules, "x/y/apps/z.txt"), ["a"])
        self.assertEqual(_owners(rules, "deep/er/main.tf"), ["t"])

    def test_segment_boundaries_respected(self):
        rules = [_rule("modules/x", "bob")]
        self.assertEqual(_owners(rules, "modules/x/main.tf"), ["bob"])
        self.assertEqual(_owners(rules, "modules/xy/main.tf"), [])

    def test_trailing_slash_is_directory_only(self):
        rules = [_rule("build/", "a")]
        self.assertEqual(_owners(rules, "build"), [])          # a FILE named build
        self.assertEqual(_owners(rules, "build", is_dir=True), ["a"])
        self.assertEqual(_owners(rules, "build/out.o"), ["a"])

    def test_star_within_segment_doublestar_across(self):
        rules = [_rule("/docs/*", "one"), _rule("/src/**/test", "t")]
        self.assertEqual(_owners(rules, "docs/a.md"), ["one"])
        # GitHub: docs/* owns files directly in docs, not nested ones.
        self.assertEqual(_owners(rules, "docs/x/a.md"), [])
        self.assertEqual(_owners(rules, "src/test/a.py"), ["t"])
        self.assertEqual(_owners(rules, "src/a/b/test/a.py"), ["t"])
        self.assertEqual(_owners(rules, "src/a/b/nottest/a.py"), [])

    def test_last_match_wins_no_union(self):
        rules = [_rule("*", "carol"), _rule("docs/", "dave"), _rule("*.md", "erin")]
        self.assertEqual(_owners(rules, "docs/a.md"), ["erin"])
        self.assertEqual(_owners(rules, "docs/a.txt"), ["dave"])
        self.assertEqual(_owners(rules, "src/a.txt"), ["carol"])

    def test_ownerless_rule_unassigns(self):
        rules = [_rule("*", "carol"), _rule("vendor/")]
        self.assertEqual(_owners(rules, "vendor/lib.go"), [])
        self.assertEqual(_owners(rules, "main.go"), ["carol"])

    def test_negation_never_matches(self):
        self.assertEqual(_owners([_rule("*", "a"), _rule("!x", "b")], "x"), ["a"])

    def test_legacy_dict_form_accepted(self):
        self.assertEqual(_owners({"*": ["a"], "src/": ["b"]}, "src/x"), ["b"])

    def test_area_is_matched_as_a_directory(self):
        rules = derive.compile_codeowners(gather.parse_codeowners(ISSUE_SCENARIO_1))
        self.assertEqual(derive.area_owners(rules, "modules/x"), ["bob"])
        self.assertEqual(derive.area_owners(rules, "modules/xy"), ["dave"])
        self.assertEqual(derive.area_owners(rules, "other"), ["carol"])
        # files inside an area can still have other owners (last match per file)
        self.assertEqual(derive.codeowners_owners(rules, "modules/xy/main.tf"),
                         ["erin"])

    def test_community_area_unions_file_owners(self):
        rules = derive.compile_codeowners([_rule("a/", "x"), _rule("b/", "y")])
        self.assertEqual(
            derive.area_owners(rules, "community:3", ["b/f.py", "a/f.py"]),
            ["x", "y"])


def _scenario_bundle(code_owners):
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                 "to": "2026-05-31"},
        "prs": [], "issues": [], "commits": [],
        "code_graph": {"provider": "directory", "areas": [
            {"id": "modules/x", "label": "x", "paths": ["modules/x/main.tf"],
             "edges": []},
            {"id": "modules/xy", "label": "xy", "paths": ["modules/xy/main.tf"],
             "edges": []},
        ]},
        "code_owners": code_owners,
    }


def _owns(conn):
    out = {}
    for row in conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE edge_type='owns'"):
        login = row[0].split("#person-", 1)[-1]
        out.setdefault(login, set()).add(row[1].split("#area-", 1)[-1])
    return out


class TestFoldOwnsEdges(unittest.TestCase):
    def _fold(self, code_owners):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _scenario_bundle(code_owners))
        return conn

    def test_issue_scenario_1_last_match_wins(self):
        conn = self._fold(gather.parse_codeowners(ISSUE_SCENARIO_1))
        # modules/x: `modules/x` (last) -> bob. modules/xy: `/modules/xy/` is the
        # last rule matching the directory (`modules/x` stops at the segment
        # boundary; `*.tf` matches files, not the directory) -> dave. carol's `*`
        # is overridden for both; erin owns no area directory.
        self.assertEqual(_owns(conn), {"bob": {"modules/x"},
                                       "dave": {"modules/xy"}})

    def test_ownerless_last_rule_emits_no_edge(self):
        conn = self._fold([_rule("*", "carol"), _rule("/modules/xy/")])
        self.assertEqual(_owns(conn), {"carol": {"modules/x"}})

    def test_order_roundtrips_through_store_and_extract(self):
        # sorted-key order would differ: `*` < `*.tf` < `/modules/xy/` < `modules/x`.
        rules = gather.parse_codeowners(
            "modules/x @bob\n/modules/xy/ @dave\n* @carol\n")
        conn = self._fold(rules)
        ex = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                             warn=lambda _m: None)
        self.assertEqual(ex["code_owners"], rules)
        compiled = derive.compile_codeowners(ex["code_owners"])
        self.assertEqual(derive.area_owners(compiled, "modules/x"), ["carol"])
        self.assertEqual(_owns(conn), {"carol": {"modules/x", "modules/xy"}})
        # re-folding the extracted bundle is idempotent (same owns edges)
        gather.fold_bundle(conn, copy.deepcopy(ex))
        self.assertEqual(_owns(conn), {"carol": {"modules/x", "modules/xy"}})

    def test_store_with_rule_list_validates_green(self):
        conn = self._fold(gather.parse_codeowners(ISSUE_SCENARIO_1))
        report = validate.validate(conn, project="o", repo="r")
        self.assertTrue(report.ok, [c for c in report.checks if not c["ok"]])


class TestStaleOwned(unittest.TestCase):
    def _stale(self, code_owners, area="avm/res/network/vnet"):
        bundle = {
            "people": {"alice": {}, "bob": {}},
            "prs": [], "commits": [], "issues": [],
            "code_owners": code_owners,
            "trains": [{"id": "t1", "code_areas": [area],
                        "effort": {"stalled": True}}],
        }
        derive.annotate_people_profile(bundle)
        return {k: v["stale_owned"] for k, v in bundle["people"].items()}

    def test_issue_scenario_2(self):
        # last matching rule is `avm/res/network/` -> bob; alice's two rules are
        # overridden. (The old startswith code got the same numbers only because
        # `/avm/...` and `*` never matched at all.)
        self.assertEqual(self._stale({"/avm/res/network/": ["alice"],
                                      "*": ["alice"],
                                      "avm/res/network/": ["bob"]}),
                         {"alice": 0, "bob": 1})

    def test_leading_slash_rule_matches(self):
        self.assertEqual(self._stale([_rule("/avm/res/network/", "alice")]),
                         {"alice": 1, "bob": 0})

    def test_last_match_wins_over_earlier_rule(self):
        self.assertEqual(self._stale([_rule("avm/res/network/", "bob"),
                                      _rule("/avm/res/network/", "alice")]),
                         {"alice": 1, "bob": 0})

    def test_star_owns_everything(self):
        self.assertEqual(self._stale([_rule("*", "alice")]),
                         {"alice": 1, "bob": 0})

    def test_counts_distinct_owned_stalled_areas(self):
        bundle = {
            "people": {"alice": {}},
            "prs": [], "commits": [], "issues": [],
            "code_owners": [_rule("/avm/res/network/", "alice")],
            "trains": [
                {"id": "t1", "code_areas": ["avm/res/network/a",
                                            "avm/res/network/b"],
                 "effort": {"stalled": True}},
                {"id": "t2", "code_areas": ["avm/res/network/a"],
                 "effort": {"stalled": True}},
                {"id": "t3", "code_areas": ["avm/res/network/c"],
                 "effort": {"stalled": False}},
            ],
        }
        derive.annotate_people_profile(bundle)
        self.assertEqual(bundle["people"]["alice"]["stale_owned"], 2)


if __name__ == "__main__":
    unittest.main()
