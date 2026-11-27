"""Second round of review fixes on the story layer (issue #97): unread
evidence carried into the slice, responses matched on the thread's own PR,
per-PR review-thread coverage for the story check, and issue references
that are not code comments. Each reproduces the finding before its fix."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402
from test_pr_activity import _fact, _story_bundle  # noqa: E402
from test_pr_narrative import _bundle  # noqa: E402
from test_story import URL, bundle, pr, review, train_of  # noqa: E402
from test_story_substance import A, B, commit, conversation, enriched, rc  # noqa: E402


class TestUnreadEvidenceInTheSlice(unittest.TestCase):

    def test_a_commit_with_only_skipped_reads_says_so(self):
        b = _bundle()
        c0 = b["pr_history"]["prs"][0]["commits"][0]
        c0["interface_unread"] = ["m/main.bicep"]
        c0["docs_unread"] = ["m/README.md"]
        c = conversation(b)
        it = next(i for i in c["timeline"] if i.get("sha") == A
                  and i["kind"] == "commit")
        self.assertEqual(it["evidence"], {"interface_unread": ["m/main.bicep"],
                                          "docs_unread": ["m/README.md"]})


class TestResponseOnItsOwnPr(unittest.TestCase):

    def test_stacked_prs_sharing_a_commit(self):
        b = _bundle()
        hist = b["pr_history"]
        row2 = copy.deepcopy(hist["prs"][0])
        row2["number"] = 0
        row2.pop("responses")
        hist["prs"].insert(0, row2)          # PR 0 sorts first by url
        p2 = pr(0, title="feat: stacked", created_at="2026-05-01T09:00:00Z")
        b["prs"].insert(0, p2)
        b = enriched(b)
        t = next(t for t in b["trains"] if 1 in (t.get("prs") or []))
        t["prs"] = sorted(set(t["prs"]) | {0})
        c = link.slice_train(b, t["id"])["conversation"]
        th = next(x for x in c["threads"] if x["kind"] == "review_thread")
        ref = th["response"]["commits"][0]
        self.assertEqual(ref["url"], f"{URL}/pull/1/commits/{B}")
        self.assertEqual(c["timeline"][ref["index"]]["on"]["number"], 1)


class TestThreadCoverage(unittest.TestCase):

    def slice(self, **row):
        b = enriched(_story_bundle(_fact(**row), state="open"))
        return link.slice_train(b, train_of(b)["id"])

    def test_omitted_threads_keep_a_qualified_claim_allowed(self):
        s = self.slice(threads_omitted=3)
        self.assertEqual(s["conversation"]["thread_coverage"],
                         {"1": {"flagged": 1, "omitted": 3}})
        text = ("Whether the three remaining review threads of #1 were resolved "
                "is not recorded.")
        self.assertEqual(link.check_story_record_claims(s, text)
                         ["resolved_said_unknown"], [])

    def test_complete_coverage_still_refuses(self):
        s = self.slice()
        text = "Whether the review threads of #1 were resolved is not recorded."
        self.assertEqual(len(link.check_story_record_claims(s, text)
                             ["resolved_said_unknown"]), 1)

    def test_the_slice_cap_hides_threads(self):
        s = self.slice()
        s["conversation"]["threads_omitted"] = 2
        text = "Whether the review threads of #1 were resolved is not recorded."
        self.assertEqual(link.check_story_record_claims(s, text)
                         ["resolved_said_unknown"], [])


class TestIssueReferencesAreNotComments(unittest.TestCase):

    def test_backticked_number_is_not_a_code_comment(self):
        b = enriched(_bundle())
        s = link.slice_train(b, train_of(b)["id"])
        self.assertEqual(link.check_story_code_comments(s, "`#7146` is open."), [])
        self.assertEqual(link.check_story_code_comments(s, "`# not in slice`"),
                         ["# not in slice"])


if __name__ == "__main__":
    unittest.main()
