"""The two story-check rules that refuse a statement of not-knowing
(`checks_said_unknown`, `resolved_said_unknown`), pinned in one truth
table (issue #97): what the sentence names x what the slice covers ->
refuse or accept. The train's own PR is #1; #2 is a linked item outside
the outcome items, of which the slice holds no coverage.

Rule: a sentence naming items is refused only when EVERY item it names is
covered; a sentence naming none is refused only when every PR of the
slice is covered; an item the slice has no coverage for is unknown."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_pr_activity import _fact, _story_bundle  # noqa: E402
from test_story import train_of  # noqa: E402
from test_story_substance import enriched  # noqa: E402

REFUSE, ACCEPT = True, False


def _slice(fact=None, no_activity=False, mutate=None):
    b = _story_bundle(fact if fact is not None else _fact(), state="open")
    if no_activity:
        del b["pr_activity"]
    b = enriched(b)
    s = link.slice_train(b, train_of(b)["id"])
    if mutate:
        mutate(s)
    return s


def _after_instant(f):
    for c in f["prs"][0]["commits"]:
        c["committed_at"] = "2026-06-10T00:00:00Z"
    return f


def _incomplete(index):
    """Commit `index` with checks omitted and no visible failure (a visible
    failure would be a complete result: failing)."""
    f = _fact()
    f["prs"][0]["commits"][index]["checks_omitted"] = 4
    f["prs"][0]["commits"][index]["checks"][0]["conclusion"] = "success"
    return f


# coverage of #1 -> (slice builder, head result known, every commit complete)
CHECK_COVERAGE = {
    "complete": (lambda: _slice(), True, True),
    "head only": (lambda: _slice(_fact(commits_total=14, commits=_fact()["prs"][0]
                                       ["commits"][1:])), True, False),
    "partial commits": (lambda: _slice(_fact(commits_total=14)), True, False),
    "incomplete older commit": (lambda: _slice(_incomplete(0)), True, False),
    "incomplete head": (lambda: _slice(_incomplete(1)), False, False),
    "capped": (lambda: _slice(_fact(checks_status="capped", commits=[],
                                    threads=[])), False, False),
    "failed": (lambda: _slice(_fact(checks_status="failed", commits=[],
                                    threads=[])), False, False),
    "no ledger entries": (lambda: _slice(no_activity=True), False, False),
    "as-of unavailable": (lambda: _slice(_after_instant(_fact(commits_total=14))),
                          False, False),
}

# what the sentence names -> expected verdict from (head known, history known)
NAMES = {
    "no item": lambda known: known,
    "own PR": lambda known: known,
    "linked item": lambda known: False,
    "mixed": lambda known: False,
}
HEAD = {"no item": "The CI result is not in the record.",
        "own PR": "The CI result of #1 is not in the record.",
        "linked item": "The CI result of #2 is not in the record.",
        "mixed": "The CI results of #1 and #2 are not in the record."}
HISTORY = {"no item": "The CI results of the earlier pushes are not in the record.",
           "own PR": "The CI results of the earlier pushes of #1 are not in the record.",
           "linked item": "The CI results of the earlier pushes of #2 are not "
                          "in the record.",
           "mixed": "The CI results of the earlier pushes of #1 and #2 are not "
                    "in the record."}


class TestChecksSaidUnknown(unittest.TestCase):

    def test_truth_table(self):
        for cov, (build, head_known, history_known) in CHECK_COVERAGE.items():
            s = build()
            for name, verdict in NAMES.items():
                for claim, known, sentences in (("head", head_known, HEAD),
                                                ("history", history_known,
                                                 HISTORY)):
                    with self.subTest(coverage=cov, names=name, claim=claim):
                        got = bool(link.check_story_record_claims(
                            s, sentences[name])["checks_said_unknown"])
                        self.assertEqual(got, verdict(known))


def _cap_hid(s):
    s["conversation"]["threads_omitted"] = 2


# coverage of #1's review-thread resolutions -> (slice builder, complete)
RESOLUTION_COVERAGE = {
    "complete": (lambda: _slice(), True),
    "threads omitted by gather": (lambda: _slice(_fact(threads_omitted=3)), False),
    "ledger cap hid threads": (lambda: _slice(mutate=_cap_hid), False),
    "capped (no flags)": (lambda: _slice(_fact(checks_status="capped", commits=[],
                                               threads=[])), False),
    "no ledger entries": (lambda: _slice(no_activity=True), False),
}
RESOLVED = {"no item": "Whether the review threads were resolved is not recorded.",
            "own PR": "Whether the review threads of #1 were resolved is not "
                      "recorded.",
            "linked item": "Whether the review threads of #2 were resolved is "
                           "not recorded.",
            "mixed": "Whether the review threads of #1 and #2 were resolved is "
                     "not recorded."}


class TestResolvedSaidUnknown(unittest.TestCase):

    def test_truth_table(self):
        for cov, (build, complete) in RESOLUTION_COVERAGE.items():
            s = build()
            for name, verdict in NAMES.items():
                with self.subTest(coverage=cov, names=name):
                    got = bool(link.check_story_record_claims(
                        s, RESOLVED[name])["resolved_said_unknown"])
                    self.assertEqual(got, verdict(complete))


class TestLinkedItemFinding(unittest.TestCase):
    """The review finding itself: a linked #2 outside the outcome items is
    judged on #2 (no coverage: unknown), never on #1's coverage."""

    def test_linked_item_claims_pass(self):
        s = _slice()
        r = link.check_story_record_claims(
            s, "The checks of #2 are unknown. Whether the threads of #2 were "
               "resolved is not recorded.")
        self.assertEqual(r, {"checks_said_unknown": [], "resolved_said_unknown": []})


if __name__ == "__main__":
    unittest.main()
