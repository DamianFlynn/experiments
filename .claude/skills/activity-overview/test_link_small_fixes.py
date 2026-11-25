"""Issue #82: smaller correctness fixes in link/extract.

1. Timeline order: code events sort by the commit's real committer timestamp
   (`committed_at`, from `%cI`), not by its day, so a same-day code event no
   longer always precedes the day's social events.
2. Slice diff cap: the combined `diff` chars kept in one slice never exceed
   SLICE_DIFF_CAP; every omitted diff is counted.
3. None-typed fields: `link.enrich` is total over JSON nulls in list/dict fields
   and reaction counts, including what the backfill normalisers produce.
4. Slice contract: the slice shape documented in BUNDLE.md lists exactly the
   keys `slice_train` returns (a docs-contract test).
5. `link.main`: the bundle and the series index are written atomically
   (temp file + os.replace), and a malformed series index exits 2 with a clear
   message instead of a traceback.

No network: git runs only against a throwaway local repo, and the backfill
seam's HTTP call is replaced by a fake.
"""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import derive  # noqa: E402
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
SHA = "c1" * 20


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. Timeline order
# ---------------------------------------------------------------------------

def _timeline_bundle(committed_at):
    """One doc-file commit on 2026-05-10 plus one PR comment at 08:00Z that day."""
    commit = {"sha": SHA, "parents": [], "author": "Alice", "date": "2026-05-10",
              "message": "docs", "files": ["docs/a.md"], "pr": None}
    if committed_at is not None:
        commit["committed_at"] = committed_at
    b = {
        "meta": {"owner": "o", "repo": "r"},
        "commits": [commit],
        "code_events": [{"commit": SHA, "author": "Alice", "date": "2026-05-10",
                         "change": "add", "path": "docs/a.md"}],
        "prs": [{"number": 1, "url": "https://github.com/o/r/pull/1",
                 "comments_list": [{
                     "id": 1, "author": "bob", "body": "x",
                     "created_at": "2026-05-10T08:00:00Z",
                     "url": "https://github.com/o/r/pull/1#issuecomment-1"}]}],
        "issues": [],
    }
    b["artifacts"] = derive.build_artifacts(b)
    return b


class TestCommitTimestamp(unittest.TestCase):
    def _record(self, *fields):
        return gather.RECORD_SEP + gather.FIELD_SEP.join(fields) + "\ndocs/a.md\n"

    def test_commit_walk_requests_committer_timestamp(self):
        self.assertIn("%cI", " ".join(gather.COMMIT_WALK_ARGS))

    def test_committed_at_is_normalised_to_utc(self):
        raw = self._record(SHA, "", "Alice", "2026-05-10", "docs",
                           "a@example.invalid", "2026-05-10T10:30:00+02:00")
        (c,) = gather.parse_git_log(raw)
        self.assertEqual(c["date"], "2026-05-10")          # windowing day kept
        self.assertEqual(c["committed_at"], "2026-05-10T08:30:00Z")

    def test_unparseable_or_absent_timestamp_adds_no_key(self):
        for fields in ((SHA, "", "Alice", "2026-05-10", "docs"),
                       (SHA, "", "Alice", "2026-05-10", "docs", "a@x", "garbage"),
                       (SHA, "", "Alice", "2026-05-10", "docs", "a@x", "")):
            (c,) = gather.parse_git_log(self._record(*fields))
            self.assertNotIn("committed_at", c, fields)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_git_commit_walk_carries_committed_at(self):
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "a@example.invalid",
               "GIT_COMMITTER_NAME": "Alice",
               "GIT_COMMITTER_EMAIL": "a@example.invalid",
               "GIT_AUTHOR_DATE": "2026-05-10T09:15:00+02:00",
               "GIT_COMMITTER_DATE": "2026-05-10T09:15:00+02:00"}
        with mock.patch.dict(os.environ, env), \
                tempfile.TemporaryDirectory() as repo:
            def git(*args):
                return subprocess.run(
                    ["git", "-c", "commit.gpgsign=false", "-C", repo, *args],
                    check=True, capture_output=True, text=True).stdout
            git("init", "-q")
            with open(os.path.join(repo, "a.md"), "w") as fh:
                fh.write("a\n")
            git("add", "-A")
            git("commit", "-q", "-m", "first")
            raw = gather.run_git(["git", "-C", repo, "log",
                                  *gather.COMMIT_WALK_ARGS])
        (c,) = gather.parse_git_log(raw)
        self.assertEqual(c["date"], "2026-05-10")
        self.assertEqual(c["committed_at"], "2026-05-10T07:15:00Z")
        self.assertIn("author_email", c)   # the transient #78 field still parses

    def test_committed_at_round_trips_through_the_store(self):
        golden = _load("bundle_p3.json")
        for c in golden["commits"]:
            c["date"] = "2026-05-10"
            c["committed_at"] = "2026-05-10T12:34:56Z"
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, copy.deepcopy(golden))
        meta = golden["meta"]
        view = extract.extract(conn, meta["owner"], meta["repo"],
                               meta["from"], meta["to"])
        self.assertTrue(view["commits"])
        for c in view["commits"]:
            self.assertEqual(c["committed_at"], "2026-05-10T12:34:56Z")


class TestTimelineRealTimeOrder(unittest.TestCase):
    def _layers(self, committed_at):
        return [e["layer"] for e in link.build_timeline(_timeline_bundle(committed_at))]

    def test_same_day_code_after_earlier_social(self):
        # Before #82 the code event's ts was the bare day '2026-05-10', which
        # sorts before every '2026-05-10T..' social ts regardless of real time.
        self.assertEqual(self._layers("2026-05-10T09:00:00Z"), ["social", "code"])

    def test_same_day_code_before_later_social(self):
        self.assertEqual(self._layers("2026-05-10T07:00:00Z"), ["code", "social"])

    def test_code_event_ts_is_the_commit_timestamp(self):
        tl = link.build_timeline(_timeline_bundle("2026-05-10T09:00:00Z"))
        code = [e for e in tl if e["layer"] == "code"]
        self.assertEqual([e["ts"] for e in code], ["2026-05-10T09:00:00Z"])

    def test_without_committed_at_falls_back_to_the_day(self):
        # Stores gathered before #82 carry no committed_at: the day is still used.
        tl = link.build_timeline(_timeline_bundle(None))
        code = [e for e in tl if e["layer"] == "code"]
        self.assertEqual([e["ts"] for e in code], ["2026-05-10"])


# ---------------------------------------------------------------------------
# 2. Slice diff cap
# ---------------------------------------------------------------------------

class TestSliceDiffCap(unittest.TestCase):
    def _total(self, out):
        return sum(len(d.get("diff") or "") for d in out)

    def test_issue_repro_5999_plus_6000(self):
        cap = link.SLICE_DIFF_CAP
        deltas = [{"name": "a", "diff": "x" * (cap - 1)},
                  {"name": "b", "diff": "y" * cap}]
        out, dropped = link._cap_train_diffs(deltas)
        self.assertLessEqual(self._total(out), cap)
        self.assertEqual(dropped, 1)
        self.assertIn("diff", out[0])
        self.assertNotIn("diff", out[1])

    def test_exactly_at_cap_is_kept(self):
        cap = link.SLICE_DIFF_CAP
        deltas = [{"name": "a", "diff": "x" * (cap - 10)},
                  {"name": "b", "diff": "y" * 10}]
        out, dropped = link._cap_train_diffs(deltas)
        self.assertEqual(self._total(out), cap)
        self.assertEqual(dropped, 0)

    def test_a_smaller_later_diff_that_fits_is_kept(self):
        cap = link.SLICE_DIFF_CAP
        deltas = [{"name": "a", "diff": "x" * (cap - 100)},
                  {"name": "b", "diff": "y" * 500},     # does not fit -> omitted
                  {"name": "c", "diff": "z" * 100}]     # fits exactly -> kept
        out, dropped = link._cap_train_diffs(deltas)
        self.assertEqual(dropped, 1)
        self.assertEqual([("diff" in d) for d in out], [True, False, True])
        self.assertEqual(self._total(out), cap)

    def test_slice_total_within_cap_through_slice_train(self):
        cap = link.SLICE_DIFF_CAP
        deltas = [{"train": "t", "artifact": "a{}".format(i), "diff": "d" * 799}
                  for i in range(20)]
        bundle = {"trains": [{"id": "t", "prs": [], "commits": []}],
                  "issues": [], "prs": [], "commits": [],
                  "feature_deltas": deltas, "symbol_moves": {"links": []}}
        s = link.slice_train(bundle, "t")
        self.assertLessEqual(self._total(s["feature_deltas"]), cap)
        kept = sum(1 for d in s["feature_deltas"] if "diff" in d)
        self.assertEqual(kept, cap // 799)
        self.assertEqual(s["feature_deltas_diff_overflow"], 20 - kept)
        self.assertEqual(len(s["feature_deltas"]), 20)   # deltas still listed


# ---------------------------------------------------------------------------
# 3. None-typed fields
# ---------------------------------------------------------------------------

class TestEnrichNoneTypedFields(unittest.TestCase):
    def test_issue_repro_fields(self):
        b = _load("bundle_p3b.json")
        for pr in b["prs"]:
            pr["reviewers"] = None
            pr["labels"] = None
        for issue in b["issues"]:
            issue["labels"] = None
            issue["reactions"] = {"+1": None, "total": None}
        link.enrich(b)   # must not raise
        for pr in b["prs"]:
            self.assertEqual(pr["reviewers"], [])
            self.assertEqual(pr["labels"], [])

    def test_every_list_or_dict_field_set_to_null(self):
        for name in ("bundle_p2.json", "bundle_p3.json", "bundle_p3b.json"):
            base = _load(name)
            for coll in ("prs", "issues", "commits"):
                keys = sorted({k for item in base.get(coll, []) for k, v in item.items()
                               if isinstance(v, (list, dict))})
                for key in keys:
                    with self.subTest(fixture=name, field="{}.{}".format(coll, key)):
                        b = copy.deepcopy(base)
                        for item in b[coll]:
                            if key in item:
                                item[key] = None
                        link.enrich(b)

    def test_flow_reads_null_reaction_counts_as_zero(self):
        b = _load("bundle_p3.json")
        for issue in b["issues"]:
            issue["reactions"] = {"+1": None, "total": None}
            # old and untouched, so the open issue is classified (`hung`)
            issue["created_at"] = issue["updated_at"] = "2025-01-01T00:00:00Z"
        link.enrich(b)
        self.assertEqual([(e["number"], e["reactions"]) for e in b["flow"]],
                         [(18, 0)])

    def test_null_free_bundle_is_unchanged(self):
        # The guard adds no keys and changes nothing on a well-formed bundle.
        for name in ("bundle_p2.json", "bundle_p3b.json"):
            b = _load(name)
            before = copy.deepcopy(b)
            link._null_collections_to_empty(b)
            self.assertEqual(b, before)

    def test_backfilled_pr_and_issue_with_nulls_enrich(self):
        # Through make_backfill_fetcher's normalisation (normalize_pr /
        # normalize_issue): REST objects with null labels/assignees/user.
        fetch = gather.make_backfill_fetcher("tok")

        def fake(url, token, allow_404=False):
            if "/pulls/" in url:
                return ({"number": 5, "title": "PR 5", "user": None, "labels": None,
                         "body": None, "merged_at": "2026-05-10T10:00:00Z",
                         "html_url": "https://github.com/acme/widget/pull/5"}, None)
            if "/issues/" in url:
                return ({"number": 6, "title": "Issue 6", "user": None,
                         "labels": None, "assignees": None, "body": None,
                         "state": "open",
                         "html_url": "https://github.com/acme/widget/issues/6"}, None)
            return (None, 404)

        with mock.patch.object(gather, "http_get_json", fake):
            pr = fetch("social", "pr-5", "acme/widget#pr-5")["node"]
            issue = fetch("social", "issue-6", "acme/widget#issue-6")["node"]
        self.assertEqual(pr["labels"], [])
        self.assertEqual(issue["labels"], [])
        self.assertEqual(issue["assignees"], [])
        b = {"meta": {"owner": "acme", "repo": "widget"},
             "prs": [pr], "issues": [issue], "commits": []}
        link.enrich(b)   # must not raise
        self.assertIn("timeline", b)


# ---------------------------------------------------------------------------
# 4. Slice contract (BUNDLE.md vs slice_train)
# ---------------------------------------------------------------------------

def _split_top(body):
    """Split `body` on commas at bracket depth 0."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _braced(text):
    """The text inside the first balanced {...} of `text`, or None."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return None


def _doc_keys(body):
    """{key: nested keys dict or None} for one documented `{ ... }` body."""
    out = {}
    for part in _split_top(body):
        m = re.match(r"([a-z_]+)", part)
        if not m:
            continue
        rest = part[m.end():]
        inner = _braced(rest) if rest.lstrip("*").startswith(":") else None
        out[m.group(1)] = _doc_keys(inner) if inner is not None else None
    return out


def _documented_slice():
    with open(os.path.join(HERE, "BUNDLE.md"), encoding="utf-8") as fh:
        text = fh.read()
    anchor = text.index("**`slice_train(bundle, train_id)`**")
    block = text[anchor:].split("```", 2)[1]
    tops = list(re.finditer(r'^\s*"(\w+)":', block, re.M))
    doc = {}
    for i, m in enumerate(tops):
        end = tops[i + 1].start() if i + 1 < len(tops) else len(block)
        value = block[m.end():end]
        inner = _braced(value)
        doc[m.group(1)] = _doc_keys(inner) if inner is not None else None
    return doc


def _contract_bundle():
    review = {"author": "rev", "state": "APPROVED",
              "submitted_at": "2026-05-09T00:00:00Z", "body": "ok"}
    lc = {"event": "reopened", "actor": "a", "created_at": "2026-05-08T00:00:00Z",
          "label": None, "id": 1, "url": "u"}
    return {
        "trains": [{"id": "t", "root_issue": 1, "prs": [2], "commits": [SHA]}],
        "issues": [{"number": 1, "title": "i", "body": "b", "url": "u",
                    "labels": [], "kind": "bug", "comments_list": [],
                    "lifecycle": [lc], "reopen_count": 1}],
        "prs": [{"number": 2, "title": "p", "body": "b", "state": "closed",
                 "merged": True, "url": "u", "reviewers": ["rev"],
                 "review_decision": "approved", "review_comments": [],
                 "comments_list": [], "reviews": [review], "lifecycle": [lc],
                 "review_rounds": {"count": 1, "states": ["approved"]},
                 "reopen_count": 1}],
        "commits": [{"sha": SHA, "message": "m", "author": "a", "date": "2026-05-09"}],
        "feature_deltas": [], "symbol_moves": {"links": []},
    }


class TestSliceContractDocumented(unittest.TestCase):
    def setUp(self):
        self.doc = _documented_slice()
        self.s = link.slice_train(_contract_bundle(), "t")

    def test_top_level_keys(self):
        self.assertEqual(set(self.doc), set(self.s))

    def test_train_issue_commit_keys(self):
        self.assertEqual(set(self.doc["train"]), set(self.s["train"]))
        self.assertEqual(set(self.doc["issue"]), set(self.s["issue"]))
        self.assertEqual(set(self.doc["commits"]), set(self.s["commits"][0]))
        self.assertEqual(set(self.doc["issue"]["lifecycle"]),
                         set(self.s["issue"]["lifecycle"][0]))

    def test_pr_keys_and_nested_shapes(self):
        doc_pr, pr = self.doc["prs"], self.s["prs"][0]
        self.assertEqual(set(doc_pr), set(pr))
        self.assertEqual(set(doc_pr["reviews"]), set(pr["reviews"][0]))
        self.assertEqual(set(doc_pr["lifecycle"]), set(pr["lifecycle"][0]))
        self.assertEqual(set(doc_pr["review_rounds"]), set(pr["review_rounds"]))


# ---------------------------------------------------------------------------
# 5. link.main: atomic writes, malformed series index
# ---------------------------------------------------------------------------

def _min_enrichable():
    return {"meta": {"owner": "o", "repo": "r", "from": "2026-01-01",
                     "to": "2026-01-31"},
            "prs": [], "issues": [], "commits": []}


class _Disk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.bundle_path = os.path.join(self.dir, "bundle.json")
        self.series_path = os.path.join(self.dir, "series.json")
        self._put(self.bundle_path, json.dumps(_min_enrichable()))

    def _put(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _get(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _main(self, *extra):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            link.main([self.bundle_path, *extra])
        return err.getvalue()


def _failing_dump(obj, fh, **kw):
    fh.write("{\"partial")
    raise OSError("disk full")


class TestAtomicWrites(_Disk):
    def test_interrupted_bundle_write_keeps_previous_bundle(self):
        before = self._get(self.bundle_path)
        with mock.patch.object(link.json, "dump", _failing_dump):
            with self.assertRaises(OSError):
                self._main()
        self.assertEqual(self._get(self.bundle_path), before)
        self.assertEqual(os.listdir(self.dir), ["bundle.json"])  # no temp left

    def test_interrupted_series_write_keeps_previous_index(self):
        self._main("--series", self.series_path)
        before = self._get(self.series_path)
        self.assertEqual(len(json.loads(before)["installments"]), 1)
        real_dump = json.dump

        def dump(obj, fh, **kw):
            if "installments" in obj:        # the series index (version 2, #86)
                return _failing_dump(obj, fh, **kw)
            return real_dump(obj, fh, **kw)

        # The next month, so a completed write WOULD change the index (a
        # same-window re-run rewrites identical bytes since #86).
        b = _min_enrichable()
        b["meta"].update({"from": "2026-02-01", "to": "2026-02-28"})
        self._put(self.bundle_path, json.dumps(b))
        with mock.patch.object(link.json, "dump", dump):
            with self.assertRaises(OSError):
                self._main("--series", self.series_path)
        self.assertEqual(self._get(self.series_path), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ["bundle.json", "series.json"])

    def test_failed_bundle_write_does_not_append_an_installment(self):
        # The bundle is written first, so a run that cannot write its bundle
        # leaves the series index untouched (no installment without a bundle).
        self._main("--series", self.series_path)
        before = self._get(self.series_path)
        b = _min_enrichable()                 # the next month (see above)
        b["meta"].update({"from": "2026-02-01", "to": "2026-02-28"})
        self._put(self.bundle_path, json.dumps(b))
        with mock.patch.object(link.json, "dump", _failing_dump):
            with self.assertRaises(OSError):
                self._main("--series", self.series_path)
        self.assertEqual(self._get(self.series_path), before)

    def test_output_bytes_unchanged(self):
        self._main()
        b = _min_enrichable()
        link.enrich(b)
        self.assertEqual(self._get(self.bundle_path), json.dumps(b, indent=2))


class TestMalformedSeriesIndex(_Disk):
    def test_malformed_index_exits_2_with_message(self):
        for text in ('{"not": "a list"}', "[1, 2]", "[{}, null]", "{oops", '"s"'):
            with self.subTest(index=text):
                self._put(self.series_path, text)
                before = self._get(self.bundle_path)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit) as cm:
                    link.main([self.bundle_path, "--series", self.series_path])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("series index", err.getvalue())
                self.assertIn(self.series_path, err.getvalue())
                # nothing written: bundle and index untouched
                self.assertEqual(self._get(self.bundle_path), before)
                self.assertEqual(self._get(self.series_path), text)

    def test_valid_index_still_appends(self):
        self._put(self.series_path, "[]")
        self._main("--series", self.series_path)
        self.assertEqual(
            len(json.loads(self._get(self.series_path))["installments"]), 1)


if __name__ == "__main__":
    unittest.main()
