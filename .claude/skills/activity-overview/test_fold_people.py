"""Slice 7b-1 step 3: person `structure` nodes + the contribution / owns /
touches / depends_on / in_milestone edges persisted on the write path.

Proven by writer round-trip tests (no link/extract changes). A no-leak
regression confirms these additions never appear in extract's raw output.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _people_bundle():
    """fixtures/bundle_p3b.json drives people/areas/owners/milestones. We augment
    it minimally to also exercise `merged` (a merged_by login), `commented`
    (comment objects with authors) and `depends_on` (a code_graph area edge),
    none of which the raw fixture carries."""
    with open(os.path.join(FIX, "bundle_p3b.json")) as fh:
        b = json.load(fh)
    # merged_by + a conversation comment on PR 42 (author + a commenter login).
    for pr in b["prs"]:
        if pr["number"] == 42:
            pr["merged_by"] = "mallory"
            pr["comments_list"] = [{"author": "erin", "body": "lgtm"}]
            pr["review_comments"] = [{"author": "carol", "body": "nit"}]
    # a code_graph area dependency edge (firewall-policy depends on docs).
    b["code_graph"]["edges"] = [
        {"from": "avm/res/network/firewall-policy", "to": "docs",
         "version": "1.0.0", "transitive": False}]
    return b


def _qp(login):
    return graphstore.qualify_person("o", login)


def _qid(local):
    return graphstore.qualify_id("o", "r", local)


class TestFoldPeople(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _people_bundle())

    def test_person_nodes_persisted(self):
        # alice authors a firewall-policy commit; derive.attribute_people_areas
        # enumerates her (and dave) as people with modules/areas.
        alice = graphstore.get_node(self.conn, _qp("alice"))
        self.assertIsNotNone(alice)
        self.assertEqual(alice["node_class"], "structure")
        self.assertIsNone(alice["ts"])  # people are point-in-time / NULL ts
        self.assertEqual(alice["data"]["login"], "alice")
        self.assertIn("avm/res/network/firewall-policy", alice["data"]["areas"])
        self.assertIsNotNone(graphstore.get_node(self.conn, _qp("dave")))

    def test_authored_edges(self):
        # person -> pr and person -> commit.
        out = graphstore.get_edges(self.conn, _qp("alice"), direction="out",
                                   edge_types=["authored"])
        dsts = {e["dst_id"] for e in out}
        self.assertIn(_qid("pr-42"), dsts)
        self.assertIn(
            _qid("c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"), dsts)

    def test_reviewed_and_merged_edges(self):
        rev = graphstore.get_edges(self.conn, _qp("carol"), direction="out",
                                   edge_types=["reviewed"])
        self.assertIn(_qid("pr-42"), {e["dst_id"] for e in rev})
        mer = graphstore.get_edges(self.conn, _qp("mallory"), direction="out",
                                   edge_types=["merged"])
        self.assertIn(_qid("pr-42"), {e["dst_id"] for e in mer})

    def test_commented_edges(self):
        # conversation + review comment authors -> the PR.
        erin = graphstore.get_edges(self.conn, _qp("erin"), direction="out",
                                    edge_types=["commented"])
        self.assertIn(_qid("pr-42"), {e["dst_id"] for e in erin})
        carol = graphstore.get_edges(self.conn, _qp("carol"), direction="out",
                                     edge_types=["commented"])
        self.assertIn(_qid("pr-42"), {e["dst_id"] for e in carol})

    def test_owns_edges(self):
        # code_owners {"avm/res/network/": [alice, bob]} -> the firewall-policy area.
        owns = graphstore.get_edges(self.conn, _qp("alice"), direction="out",
                                    edge_types=["owns"])
        self.assertIn(
            _qid("area-avm/res/network/firewall-policy"),
            {e["dst_id"] for e in owns})
        bob = graphstore.get_edges(self.conn, _qp("bob"), direction="out",
                                   edge_types=["owns"])
        self.assertIn(
            _qid("area-avm/res/network/firewall-policy"),
            {e["dst_id"] for e in bob})

    def test_touches_edges(self):
        # commit c1 touches files in firewall-policy -> commit -> area.
        commit = _qid("c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1")
        t = graphstore.get_edges(self.conn, commit, direction="out",
                                 edge_types=["touches"])
        self.assertIn(
            _qid("area-avm/res/network/firewall-policy"),
            {e["dst_id"] for e in t})

    def test_depends_on_edge_carries_data(self):
        src = _qid("area-avm/res/network/firewall-policy")
        dep = graphstore.get_edges(self.conn, src, direction="out",
                                   edge_types=["depends_on"])
        self.assertEqual(len(dep), 1)
        self.assertEqual(dep[0]["dst_id"], _qid("area-docs"))
        self.assertEqual(dep[0]["data"]["version"], "1.0.0")
        self.assertEqual(dep[0]["data"]["transitive"], False)

    def test_in_milestone_edge(self):
        # PR 42 / issue 17 carry milestone title "v1.2.0" -> milestone-4 node.
        pr = graphstore.get_edges(self.conn, _qid("pr-42"), direction="out",
                                  edge_types=["in_milestone"])
        self.assertIn(_qid("milestone-4"), {e["dst_id"] for e in pr})
        iss = graphstore.get_edges(self.conn, _qid("issue-17"), direction="out",
                                   edge_types=["in_milestone"])
        self.assertIn(_qid("milestone-4"), {e["dst_id"] for e in iss})

    def test_project_scoped_person_idempotent_across_repos(self):
        # Folding the same login from ANOTHER repo in the same project must not
        # create a duplicate person node (id is project-scoped).
        b2 = _people_bundle()
        b2["meta"]["repo"] = "other"
        gather.fold_bundle(self.conn, b2)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE id=?", (_qp("alice"),)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_idempotent_refold(self):
        n_nodes = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        gather.fold_bundle(self.conn, _people_bundle())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], n_nodes)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], n_edges)


class TestPeopleNoLeak(unittest.TestCase):
    """extract's raw output must be byte-identical with vs without the people
    nodes + non-spine edges (they live outside the spine and the prefix-keyed
    structure reconstruction, so they are naturally ignored)."""

    # The contribution/owns/touches/depends_on/in_milestone edge types this
    # slice adds; deleting them + the person nodes must not change extract's
    # output (it reads neither — arrays come from prefix-keyed structure/code
    # nodes and the spine, none of which these touch).
    _NEW_EDGE_TYPES = ("authored", "reviewed", "merged", "reported",
                       "commented", "reacted", "owns", "touches",
                       "depends_on", "in_milestone")

    def test_extract_byte_identical_with_and_without_people(self):
        # Fold once; extract WITH the people nodes + non-spine edges present.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _people_bundle())
        out_with = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")

        # Sanity: the additions really are present before we remove them.
        n_people = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE id LIKE '%#person-%'").fetchone()[0]
        self.assertGreater(n_people, 0)
        ph = ",".join("?" for _ in self._NEW_EDGE_TYPES)
        n_edges = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE edge_type IN ({})".format(ph),
            list(self._NEW_EDGE_TYPES)).fetchone()[0]
        self.assertGreater(n_edges, 0)

        # Now physically delete every person node + new edge type and re-extract.
        conn.execute("DELETE FROM nodes WHERE id LIKE '%#person-%'")
        conn.execute("DELETE FROM edges WHERE edge_type IN ({})".format(ph),
                     list(self._NEW_EDGE_TYPES))
        conn.commit()
        out_without = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31")

        # Byte-identical: the additions never leaked into extract's raw bundle.
        self.assertEqual(
            json.dumps(out_with, sort_keys=True),
            json.dumps(out_without, sort_keys=True))
        # And no person node ever masqueraded as a commit (code-node filter).
        self.assertFalse(any("person-" in str(c.get("sha"))
                             for c in out_with["commits"]))


if __name__ == "__main__":
    unittest.main()
