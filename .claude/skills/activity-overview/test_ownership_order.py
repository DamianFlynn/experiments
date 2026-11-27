"""Ownership review findings (issue #97) and the account type gather keeps:
A) events of one day replay in commit order, not by kind; B) the commit-
derived ownership state of earlier windows carries into a later window's
reconstruction; C) the reconstruction inputs are validated. Each test
reproduces its finding before the fix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import validate  # noqa: E402
from test_module_ownership import S1, S2, Blobs, meta  # noqa: E402
from test_review_fixes_ownership import _store  # noqa: E402

P = "avm/res/x/y/metadata.json"


def ownership(code_events, commits, blobs, frm="2026-10-01", to="2026-10-31"):
    return gather.collect_module_ownership(
        code_events, commits, [], [], Blobs(blobs), [], frm, to,
        web="https://github.com/o/r", start_files=[])


class TestA_SameDayOrder(unittest.TestCase):

    def fact(self):
        # S2 empties the owner list at 09:00, S1 gives it an owner at 15:00:
        # the module ends the day owned (sha order and kind order say otherwise)
        commits = [{"sha": S1, "committed_at": "2026-10-06T15:00:00Z"},
                   {"sha": S2, "committed_at": "2026-10-06T09:00:00Z"}]
        evs = [{"commit": S1, "path": P, "change": "modify", "date": "2026-10-06"},
               {"commit": S2, "path": P, "change": "modify", "date": "2026-10-06"}]
        return ownership(evs, commits, {(S2 + "^", P): meta(["o"]), (S2, P): meta([]),
                                        (S1 + "^", P): meta([]), (S1, P): meta(["n"])})

    def test_replayed_in_commit_order(self):
        f = self.fact()
        self.assertEqual([(e["kind"], e["source"]["sha"]) for e in f["events"]], [
            ("owner_needed", S2), ("owner_removed", S2), ("owner_added", S1)])
        self.assertEqual([e["at"] for e in f["events"]], [
            "2026-10-06T09:00:00Z", "2026-10-06T09:00:00Z", "2026-10-06T15:00:00Z"])
        self.assertEqual([e["seq"] for e in f["events"]], [0, 1, 2])
        self.assertEqual(f["without_owner"], [])

    def test_reconstruction_replays_the_stored_order(self):
        f = self.fact()
        f["events"].reverse()
        self.assertEqual(extract.standing_without_owner(f, "2026-10-06"), [])

    def test_the_stored_shape_validates(self):
        self.assertIsNone(validate._fact_shape_problem("module_ownership", self.fact()))


class TestB_CarriedAcrossWindows(unittest.TestCase):
    """Since the self-contained design: the state a window starts with is in
    its own fact (`start_state`, read from the tree before it), never carried
    from another window."""

    def test_an_owner_list_emptied_in_september_stands_in_october(self):
        sep = ownership([{"commit": S1, "path": P, "change": "modify",
                          "date": "2026-09-10"}],
                        [{"sha": S1, "committed_at": "2026-09-10T10:00:00Z"}],
                        {(S1 + "^", P): meta(["o"]), (S1, P): meta([])},
                        "2026-09-01", "2026-09-30")
        self.assertEqual([w["module"] for w in sep["without_owner"]], ["avm/res/x/y"])
        octo = gather.collect_module_ownership(
            [], [], [], [], Blobs({}), [], "2026-10-01", "2026-10-31",
            start_state={"avm/res/x/y": {"owner_file": "metadata.json", "owners": [],
                                         "codeowners": [], "orphan_marker": False,
                                         "owners_since": "2026-09-10"}})
        conn = _store(("2026-09-01", "2026-09-30", sep), ("2026-10-01", "2026-10-31", octo))
        for frm in ("2026-09-01", "2026-10-01"):
            for to in ("2026-10-07", "2026-10-31"):
                v = extract.extract(conn, "o", "r", frm, to)
                self.assertEqual([(w["module"], w.get("since")) for w in
                                  v["module_ownership"]["without_owner"]],
                                 [("avm/res/x/y", "2026-09-10")], (frm, to))

    def test_an_owner_added_later_clears_it(self):
        sep = ownership([{"commit": S1, "path": P, "change": "modify",
                          "date": "2026-09-10"}],
                        [{"sha": S1, "committed_at": "2026-09-10T10:00:00Z"}],
                        {(S1 + "^", P): meta(["o"]), (S1, P): meta([])},
                        "2026-09-01", "2026-09-30")
        octo = gather.collect_module_ownership(
            [{"commit": S2, "path": P, "change": "modify", "date": "2026-10-03"}],
            [{"sha": S2, "committed_at": "2026-10-03T10:00:00Z"}], [], [],
            Blobs({(S2 + "^", P): meta([]), (S2, P): meta(["n"])}), [],
            "2026-10-01", "2026-10-31",
            start_state={"avm/res/x/y": {"owner_file": "metadata.json", "owners": [],
                                         "codeowners": [], "orphan_marker": False,
                                         "owners_since": "2026-09-10"}})
        conn = _store(("2026-09-01", "2026-09-30", sep), ("2026-10-01", "2026-10-31", octo))
        v = extract.extract(conn, "o", "r", "2026-09-01", "2026-10-31")
        self.assertEqual(v["module_ownership"]["without_owner"], [])


class TestC_ReconstructionInputsValidated(unittest.TestCase):

    def test_lists_of_objects(self):
        f = ownership([], [], {})
        self.assertIsNone(validate._fact_shape_problem("module_ownership", f))
        for key, bad in (("start_without_owner", "avm/res/x/y"),
                         ("start_without_owner", ["avm/res/x/y"]),
                         ("labelled_items", {"module": "m"}),
                         ("labelled_items", [1])):
            self.assertIsNotNone(validate._fact_shape_problem(
                "module_ownership", dict(f, **{key: bad})), (key, bad))


class TestAccountType(unittest.TestCase):

    def test_kept_when_not_a_user(self):
        bot = {"login": "agent-x", "type": "Bot"}
        user = {"login": "alice", "type": "User"}
        for fn in (gather.normalize_pr, gather.normalize_issue, gather.normalize_review,
                   gather.normalize_comment):
            self.assertEqual(fn({"number": 1, "id": 1, "user": bot})["author_type"],
                             "Bot", fn.__name__)
            self.assertNotIn("author_type", fn({"number": 1, "id": 1, "user": user}))


if __name__ == "__main__":
    unittest.main()
