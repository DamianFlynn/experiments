"""A cue that ends its line with a colon carries into the list below it
("Depends on:" then "- #12"): the shape PR templates use, read the same by
the record and by the claim checker."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_story import pr  # noqa: E402
from test_story_groups import talk, view  # noqa: E402
from test_story_relations import edges, hist  # noqa: E402
from test_story_substance import enriched  # noqa: E402


def flat(text):
    """[(subject number or None, relation, [targets], task)]"""
    return [((c["subject"] or {}).get("number"), c["relation"],
             [t["number"] for t in c["targets"]], c["task"])
            for c in link.parse_relation_claims(text, "o/r")]


class TestColonLists(unittest.TestCase):

    def test_a_colon_cue_carries_into_its_list(self):
        self.assertEqual(flat("#30 depended on:\n- #12\n- #13"),
                         [(30, "depends_on", [12], None), (30, "depends_on", [13], None)])
        self.assertEqual(flat("Blocked by:\n1. #12\n2) #13"),
                         [(None, "blocked_by", [12], None), (None, "blocked_by", [13], None)])
        self.assertEqual(flat("**Depends on:**\n* #12 and #14\n+ o/x#13 (the API)"),
                         [(None, "depends_on", [12, 14], None),
                          (None, "depends_on", [13], None)])
        self.assertEqual(flat("Waiting for:\n- #12")[0][1], "blocked_by")
        c = link.parse_relation_claims("Waiting for:\n- #12", "o/r")[0]
        self.assertEqual((c["query_kind"], c["quote"]), ("waits_on", "Waiting for:\n- #12"))
        # on one line it already was a list of targets
        self.assertEqual(flat("Depends on: #12, #13"), [(None, "depends_on", [12, 13], None)])

    def test_task_boxes_keep_their_own_state(self):
        self.assertEqual(flat("Blocked by:\n- [ ] #12\n- [x] #13 tests"),
                         [(None, "blocked_by", [12], "open"), (None, "blocked_by", [13], "done")])

    def test_an_item_with_its_own_cue_keeps_it(self):
        self.assertEqual(flat("Depends on:\n- #12 is blocked by #16\n- #13"),
                         [(12, "blocked_by", [16], None), (None, "depends_on", [13], None)])

    def test_where_the_list_ends(self):
        # a colon line that is no cue carries nothing
        self.assertEqual(flat("Changes:\n- #12"), [(None, "mentions", [12], None)])
        # a paragraph or a blank line ends the list
        self.assertEqual(flat("Depends on:\nSome text.\n- #12"), [(None, "mentions", [12], None)])
        self.assertEqual(flat("Depends on:\n\n- #12"), [(None, "mentions", [12], None)])
        self.assertEqual(flat("Depends on:\n- #12\n\n- #13"),
                         [(None, "depends_on", [12], None), (None, "mentions", [13], None)])
        self.assertEqual(flat("Depends on:\n- #12\nAfter that, #15."),
                         [(None, "depends_on", [12], None), (None, "mentions", [15], None)])
        # an item that does not open with a reference is no target
        self.assertEqual(flat("Depends on:\n- see the notes in #12"),
                         [(None, "mentions", [12], None)])
        # a sentence break inside an item does not end the list
        self.assertEqual(flat("Depends on:\n- #12. More words.\n- #13"),
                         [(None, "depends_on", [12], None), (None, "depends_on", [13], None)])

    def test_nested_items_are_details(self):
        self.assertEqual(flat("Depends on:\n- #12\n  - #14 detail\n- #13"),
                         [(None, "depends_on", [12], None), (None, "mentions", [14], None),
                          (None, "depends_on", [13], None)])
        # a colon cue that is itself an item carries into its own sub-items only
        self.assertEqual(flat("- Depends on:\n  - #12\n- #13"),
                         [(None, "depends_on", [12], None), (None, "mentions", [13], None)])

    def test_the_record_reads_a_template(self):
        body = "## Related\n\nDepends on:\n- [ ] #2\n- #3\n\nOther: #4"
        b = enriched(view([pr(1, body=body, comments_list=talk(1)),
                           pr(2, comments_list=talk(2)), pr(3, comments_list=talk(3)),
                           pr(4, comments_list=talk(4))], pr_history=hist()))
        got = {(e["to"], e["relation"], e.get("waiting")) for e in edges(b)}
        self.assertEqual(got, {("o/r:train-pr-2", "depends_on", True),
                               ("o/r:train-pr-3", "depends_on", True),
                               ("o/r:train-pr-4", "mentions", None)})
        e2 = next(e for e in edges(b) if e["to"] == "o/r:train-pr-2")
        self.assertEqual((e2["basis"]["task_done"], e2["basis"]["quote"]),
                         (False, "Depends on:\n- [ ] #2"))


if __name__ == "__main__":
    unittest.main()
