"""Historical check coverage is per commit in the story check (issue #97):
every word form of an earlier-history sentence is recognised, and an older
commit whose checks were only partly read keeps its result unknown. Each
reproduces the finding before its fix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_pr_activity import _fact, _story_bundle  # noqa: E402
from test_story import train_of  # noqa: E402
from test_story_substance import enriched  # noqa: E402


def _slice(fact):
    b = enriched(_story_bundle(fact, state="open"))
    return link.slice_train(b, train_of(b)["id"])


class TestEarlierHistoryWordForms(unittest.TestCase):

    def test_every_form_with_partial_commit_coverage(self):
        s = _slice(_fact(commits_total=14))
        for text in ("The CI results of the first push of #1 are unknown.",
                     "The CI results of the earlier pushes of #1 are unknown.",
                     "The CI result of the initial commit of #1 is unknown.",
                     "The check runs of the earliest commits of #1 are unknown.",
                     "Checks of the previous run of #1 are unknown.",
                     "Checks of the earlier runs of #1 are unknown."):
            self.assertEqual(link.check_story_record_claims(s, text)
                             ["checks_said_unknown"], [], text)

    def test_the_head_claim_is_still_refused(self):
        s = _slice(_fact(commits_total=14))
        self.assertEqual(len(link.check_story_record_claims(
            s, "The CI result of #1 is unknown.")["checks_said_unknown"]), 1)


class TestPerCommitCompleteness(unittest.TestCase):

    def test_an_older_incomplete_commit_keeps_history_unknown(self):
        f = _fact()                                  # 2 of 2 commits read
        f["prs"][0]["commits"][0]["checks_omitted"] = 4
        f["prs"][0]["commits"][0]["checks"][0]["conclusion"] = "success"
        s = _slice(f)
        ch = s["conversation"]["outcome"]["items"][0]["checks"]
        self.assertEqual((ch["rollup"], ch["commits_incomplete"]), ("passing", 1))
        text = "The CI result of the earlier commit of #1 is unknown."
        self.assertEqual(link.check_story_record_claims(s, text)
                         ["checks_said_unknown"], [])

    def test_complete_history_still_refuses(self):
        s = _slice(_fact())
        self.assertNotIn("commits_incomplete",
                         s["conversation"]["outcome"]["items"][0]["checks"])
        text = "The CI result of the earlier commit of #1 is unknown."
        self.assertEqual(len(link.check_story_record_claims(s, text)
                             ["checks_said_unknown"]), 1)


if __name__ == "__main__":
    unittest.main()
