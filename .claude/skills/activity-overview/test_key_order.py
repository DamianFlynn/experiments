"""Enrich output must not depend on the key order of the bundle's dicts (#100).

`link.enrich` used to break same-(ts, url) timeline ties, and order the whole of
`feature_deltas`, by the insertion order of `bundle["artifacts"]`, and
`render`'s content timeline listed (and capped) each day's events in that order.
So the same data serialised with `sort_keys=True` (digest.py) and re-enriched
gave a different timeline than enriching extract's in-memory result (view.py).

Every check here compares canonical JSON (`sort_keys=True`): object key order
carries no meaning in a bundle, list order does. The inputs are deliberately
KEY-REVERSED at every depth (not merely sorted), so a consumer that iterates a
dict in its given order cannot pass by luck.

Offline: stores are built from the committed fixtures via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import render  # noqa: E402
import view  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
FIXTURES = sorted(n for n in os.listdir(FIX)
                  if n.startswith("bundle_") and n.endswith(".json"))


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, indent=2)


def reverse_keys(obj, _top=True):
    """`obj` with every dict's keys in REVERSED insertion order, at every depth.
    List order is kept: it is data. The one exception is the legacy dict form of
    top-level `code_owners`, whose insertion order IS the CODEOWNERS rule order
    (the last matching rule wins, see derive.codeowners_rules): reversing it
    would change the data, not just its serialisation."""
    if isinstance(obj, dict):
        out = {}
        for k in reversed(list(obj)):
            if _top and k == "code_owners":
                out[k] = copy.deepcopy(obj[k])
            else:
                out[k] = reverse_keys(obj[k], _top=False)
        return out
    if isinstance(obj, list):
        return [reverse_keys(v, _top=False) for v in obj]
    return obj


def _sorted_roundtrip(obj):
    return json.loads(json.dumps(obj, sort_keys=True))


def _enrich(bundle):
    with contextlib.redirect_stderr(io.StringIO()):
        return link.enrich(copy.deepcopy(bundle))


def _extracted(name):
    golden = _load(name)
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(golden))
    meta = golden["meta"]
    out = extract.extract(conn, meta["owner"], meta["repo"], meta["from"],
                          meta["to"], warn=lambda _m: None)
    conn.close()
    return out


class TestEnrichIndependentOfKeyOrder(unittest.TestCase):
    """Acceptance (#100): for each fixtures/bundle_*.json,
    enrich(extract(...)) == enrich(sort_keys round trip) == enrich(key-reversed)."""

    def test_fixture_list_is_not_empty(self):
        self.assertGreaterEqual(len(FIXTURES), 5)

    def test_extracted_bundles(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                x = _extracted(name)
                want = _canonical(_enrich(x))
                self.assertEqual(_canonical(_enrich(_sorted_roundtrip(x))), want)
                self.assertEqual(_canonical(_enrich(reverse_keys(x))), want)

    def test_raw_fixtures(self):
        # The same holds for a bundle handed to enrich directly (no store).
        for name in FIXTURES:
            with self.subTest(fixture=name):
                raw = _load(name)
                want = _canonical(_enrich(raw))
                self.assertEqual(_canonical(_enrich(reverse_keys(raw))), want)

    def test_reversal_reorders_artifacts(self):
        # Teeth: the fixtures the gate leans on really carry several artifacts,
        # and the reversal really changes their order.
        x = _extracted("bundle_p3.json")
        self.assertGreater(len(x["artifacts"]), 1)
        self.assertNotEqual(list(x["artifacts"]),
                            list(reverse_keys(x)["artifacts"]))


def _tie_bundle():
    """One commit touching two tracked files plus a rename, no committed_at: every
    code event shares one (ts, url). Artifacts inserted out of id order."""
    url = "https://github.com/o/r/commit/c1"

    def ev(event):
        return {"event": event, "commit": "c1", "author": "alice",
                "date": "2026-05-03",
                "ref": {"type": "commit", "id": "c1", "url": url}}

    def art(path, kind, events):
        return {"kind": kind, "path": path, "name": path.split("/")[-1],
                "status": "live", "replaced_by": None, "code_area": None,
                "lifecycle": [ev(e) for e in events]}

    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-05-01",
                 "to": "2026-05-31"},
        "prs": [], "issues": [],
        "commits": [{"sha": "c1", "author": "alice", "date": "2026-05-03",
                     "message": "docs", "files": []}],
        "artifacts": {
            "art:docs/z.md": art("docs/z.md", "doc", ["add"]),
            "art:docs/a.md": art("docs/a.md", "doc", ["remove"]),
            "art:README.md": art("README.md", "readme", ["change"]),
            "art:examples/b.json": art("examples/b.json", "example",
                                       ["add", "change"]),
        },
    }


class TestTimelineAndDeltasTieBreak(unittest.TestCase):
    def test_build_timeline_total_key(self):
        b = _tie_bundle()
        want = link.build_timeline(b)
        self.assertEqual(link.build_timeline(reverse_keys(b)), want)
        # The canonical order: artifact path, then the ledger's order.
        self.assertEqual([(e["subject"]["path"], e["event"]) for e in want],
                         [("README.md", "change"), ("docs/a.md", "remove"),
                          ("docs/z.md", "add"), ("examples/b.json", "add"),
                          ("examples/b.json", "change")])

    def test_compute_feature_deltas_total_key(self):
        b = _tie_bundle()
        want = link.compute_feature_deltas(b)
        self.assertEqual(link.compute_feature_deltas(reverse_keys(b)), want)
        self.assertEqual([(d["artifact"], d["kind"]) for d in want],
                         [("art:README.md", "change"), ("art:docs/a.md", "drop"),
                          ("art:docs/z.md", "add"),
                          ("art:examples/b.json", "add"),
                          ("art:examples/b.json", "change")])

    def test_feature_deltas_follow_time_before_artifact(self):
        # A later commit on an earlier-sorting artifact comes after.
        b = _tie_bundle()
        b["commits"].append({"sha": "c0", "author": "bob", "date": "2026-05-02",
                             "message": "x", "files": []})
        b["artifacts"]["art:docs/z.md"]["lifecycle"].insert(0, {
            "event": "add", "commit": "c0", "author": "bob",
            "date": "2026-05-02",
            "ref": {"type": "commit", "id": "c0",
                    "url": "https://github.com/o/r/commit/c0"}})
        b["artifacts"]["art:docs/z.md"]["lifecycle"][1]["event"] = "change"
        got = [(d["artifact"], d["commit"])
               for d in link.compute_feature_deltas(b)]
        self.assertEqual(got[0], ("art:docs/z.md", "c0"))
        self.assertEqual(link.compute_feature_deltas(reverse_keys(b)),
                         link.compute_feature_deltas(b))

    def test_content_timeline_independent_of_artifact_order(self):
        b = _tie_bundle()
        self.assertEqual(render.emit_content_timeline(reverse_keys(b)),
                         render.emit_content_timeline(b))

    def test_content_timeline_cap_keeps_canonical_events(self):
        # With more events on one day than the per-day cap, WHICH events are
        # shown must not depend on the artifact dict order either.
        b = _tie_bundle()
        for i in range(render.TIMELINE_MAX_EVENTS_PER_DAY + 3):
            path = "docs/n{:02d}.md".format(i)
            b["artifacts"]["art:" + path] = copy.deepcopy(
                b["artifacts"]["art:docs/z.md"])
            b["artifacts"]["art:" + path]["path"] = path
            b["artifacts"]["art:" + path]["name"] = path.split("/")[-1]
        self.assertEqual(render.emit_content_timeline(reverse_keys(b)),
                         render.emit_content_timeline(b))


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def build_store(self, name):
        db = self.path("journey.db")
        conn = graphstore.open_store(db)
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(_load(name)))
        conn.commit()
        conn.close()
        return db


class TestViewWritesSortedKeys(_TmpCase):
    def test_view_output_is_canonical_json(self):
        db = self.build_store("bundle_p3.json")
        out = self.path("p3.json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(view.main(["--store", db, "--from", "2026-05-01",
                                        "--to", "2026-05-31", "--out", out]), 0)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        obj = json.loads(text)
        self.assertEqual(
            text, json.dumps(obj, sort_keys=True, indent=2,
                             ensure_ascii=False) + "\n")


# meta keys digest.member_bundles appends to a member (issue #74); view.py does
# not, so they are set aside when comparing the two routes.
_MEMBER_IDENTITY = ("project", "slug", "repo_owner", "repo_name")


class TestDigestMemberEqualsViewPlusLink(_TmpCase):
    """Acceptance (#100): a member bundle taken from digest.py's (sort_keys)
    output and re-enriched equals the bundle view.py + link.py produce."""

    def _check(self, name):
        golden = _load(name)
        owner, repo = golden["meta"]["owner"], golden["meta"]["repo"]
        frm, to = golden["meta"]["from"], golden["meta"]["to"]
        db = self.build_store(name)

        out = self.path(name)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(view.main(["--store", db, "--from", frm,
                                        "--to", to, "--out", out]), 0)
            link.main([out])
        with open(out, encoding="utf-8") as fh:
            via_view = json.load(fh)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.assertEqual(digest.main(["--store", db, "--project", owner,
                                          "--from", frm, "--to", to]), 0)
        members = json.loads(buf.getvalue())["members"]
        self.assertEqual([m["repo"] for m in members], [repo])
        member = _enrich(members[0]["bundle"])
        for key in _MEMBER_IDENTITY:
            member["meta"].pop(key, None)
        self.assertEqual(_canonical(member), _canonical(via_view))

    def test_each_fixture(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                self._check(name)


if __name__ == "__main__":
    unittest.main()
