"""One bundle/view contract version, stamped in every view (issue #107 item 8).

`extract.VIEW_CONTRACT_VERSION` versions the bundle/view shape (BUNDLE.md):
gather's in-memory bundle, every extract / view.py view, each digest member
bundle and the digest project view carry it as `meta.schema_version`. The
STORE schema version (`graphstore.SCHEMA_VERSION`) is a different contract and
lives only in the store's meta. Before: gather stamped 1, extract views had
none and digest stamped the store's 2.
"""

import contextlib
import copy
import io
import json
import os
import subprocess
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
import view  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
BUILD_REPORT = os.path.join(HERE, "samples", "build_report.py")


def _bundle():
    with open(os.path.join(FIX, "bundle_p3b.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _store(path=":memory:"):
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(_bundle()))
    conn.commit()
    return conn


class TestViewContractVersion(unittest.TestCase):
    def test_constant_is_its_own_contract(self):
        self.assertIsInstance(extract.VIEW_CONTRACT_VERSION, int)
        # every value an earlier view carried (gather 1, digest's store 2) is
        # retired, so an old view never reads as the current contract
        self.assertGreater(extract.VIEW_CONTRACT_VERSION, 2)

    def test_extract_view_stamps_it(self):
        v = extract.extract(_store(), "o", "r", "2026-05-01", "2026-05-31")
        self.assertEqual(v["meta"]["schema_version"],
                         extract.VIEW_CONTRACT_VERSION)
        enriched = link.enrich(copy.deepcopy(v))
        self.assertEqual(enriched["meta"]["schema_version"],
                         extract.VIEW_CONTRACT_VERSION)

    def test_gather_bundle_stamps_it(self):
        b = gather.build_bundle({"owner": "o", "repo": "r"}, [], [], [])
        self.assertEqual(b["meta"]["schema_version"],
                         extract.VIEW_CONTRACT_VERSION)

    def test_view_cli_writes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "s.db")
            _store(store).close()
            out = os.path.join(tmp, "v.json")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(view.main(["--store", store, "--from",
                                            "2026-05-01", "--to", "2026-05-31",
                                            "--out", out]), 0)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["meta"]["schema_version"],
                                 extract.VIEW_CONTRACT_VERSION)

    def test_digest_project_view_and_members_stamp_it(self):
        pv = digest.build_project_view(_store(), "o", ["r"], "2026-05-01",
                                       "2026-05-31", warn=lambda _m: None)
        self.assertEqual(pv["meta"]["schema_version"],
                         extract.VIEW_CONTRACT_VERSION)
        for m in pv["members"]:
            self.assertEqual(m["bundle"]["meta"]["schema_version"],
                             extract.VIEW_CONTRACT_VERSION, m["repo"])

    def test_store_schema_version_stays_in_the_store(self):
        conn = _store()
        self.assertEqual(graphstore.get_meta(conn, "schema_version"),
                         str(graphstore.SCHEMA_VERSION))


class TestBuildReportChecksTheVersion(unittest.TestCase):
    def _run(self, version):
        with open(os.path.join(HERE, "samples", "digest_view.json"),
                  encoding="utf-8") as fh:
            v = json.load(fh)
        if version is not None:
            v["meta"]["schema_version"] = version
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(v, fh)
            return subprocess.run(
                [sys.executable, BUILD_REPORT, "--view", path, "--out",
                 os.path.join(tmp, "r.md"), "--skip-validate"],
                capture_output=True, text=True, cwd=tmp)

    def test_newer_contract_refused(self):
        r = self._run(extract.VIEW_CONTRACT_VERSION + 1)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("schema_version", r.stderr)

    def test_current_and_unversioned_views_render(self):
        for version in (extract.VIEW_CONTRACT_VERSION, None):
            r = self._run(version)
            self.assertEqual(r.returncode, 0, (version, r.stderr))


if __name__ == "__main__":
    unittest.main()
