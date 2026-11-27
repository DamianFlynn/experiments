"""The story check never rejects a true statement of a coverage limit
(issue #97): omitted evidence / responses reach the slice, unread earlier
commits may be called unknown, and a PR without ledger entries keeps its
resolution unknown. Each reproduces the finding before its fix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_pr_activity import _fact, _story_bundle  # noqa: E402
from test_pr_narrative import _bundle  # noqa: E402
from test_story import bundle, pr, train_of  # noqa: E402
from test_story_substance import conversation, enriched  # noqa: E402


class TestHistoryOmissionsReachTheSlice(unittest.TestCase):

    def test_narrative_and_response_omissions_are_named(self):
        b = _bundle()
        row = b["pr_history"]["prs"][0]
        row["narrative_omitted"] = 7
        row["responses_omitted"] = 2
        ng = conversation(b)["not_gathered"]
        self.assertIn("7 narrative evidence row(s) of the older commits of #1 "
                      "(beyond the per-PR budget)", ng)
        self.assertIn("what followed 2 review comment(s) of #1 (beyond the per-PR "
                      "budget)", ng)


def _slice(fact):
    b = enriched(_story_bundle(fact, state="open"))
    return link.slice_train(b, train_of(b)["id"])


class TestCommitCoverage(unittest.TestCase):

    def test_unread_earlier_commits_may_be_called_unknown(self):
        s = _slice(_fact(commits_total=14))
        claims = link.check_story_record_claims
        ok = "Check results of the 12 earliest commits of #1 were not gathered."
        self.assertEqual(claims(s, ok)["checks_said_unknown"], [])
        bad = "The CI result of the head commit of #1 is not in the record."
        self.assertEqual(len(claims(s, bad)["checks_said_unknown"]), 1)

    def test_full_coverage_still_refuses_the_earlier_claim(self):
        s = _slice(_fact())
        claims = link.check_story_record_claims
        text = "Check results of the earliest commits of #1 were not gathered."
        self.assertEqual(len(claims(s, text)["checks_said_unknown"]), 1)


class TestUnledgeredPrStaysUnknown(unittest.TestCase):

    def slice_two(self):
        f = _fact()
        f["prs"].append({"number": 2, "checks_status": "capped"})
        p1 = _story_bundle(f, state="open")["prs"][0]
        p2 = pr(2, title="feat: other", created_at="2026-05-03T00:00:00Z")
        b = enriched(bundle([p1, p2], pr_activity=f))
        t = next(t for t in b["trains"] if 1 in (t.get("prs") or []))
        t["prs"] = [1, 2]
        return link.slice_train(b, t["id"])

    def test_named_and_unnamed_claims_about_the_capped_pr(self):
        s = self.slice_two()
        claims = link.check_story_record_claims
        for text in ("Whether the review threads of #2 were resolved is not recorded.",
                     "Whether the review threads were resolved is not recorded.",
                     "Whether the threads of #1 and #2 were resolved is not recorded."):
            self.assertEqual(claims(s, text)["resolved_said_unknown"], [], text)
        self.assertEqual(len(claims(
            s, "Whether the review threads of #1 were resolved is not recorded.")
            ["resolved_said_unknown"]), 1)


class TestRuleTableDocumented(unittest.TestCase):

    def test_report_sections_has_the_table(self):
        with open(os.path.join(os.path.dirname(__file__), "reference",
                               "report-sections.md"), encoding="utf-8") as fh:
            text = fh.read()
        for key in ("| `checks_said_unknown` |", "| `resolved_said_unknown` |"):
            self.assertIn(key, text)


if __name__ == "__main__":
    unittest.main()
