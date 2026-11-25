"""Tests for view.py (store -> per-repo bundle view CLI) and the SKILL.md /
commands/activity.md docs contract (issue #51).

Offline: stores are built from the committed fixtures via gather.fold_bundle.
"""

import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
import view  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _build_store(path, folds):
    """folds: [(fixture_name, project, repo)]; project/repo None -> meta."""
    conn = graphstore.open_store(path)
    graphstore.init_schema(conn)
    for name, project, repo in folds:
        gather.fold_bundle(conn, copy.deepcopy(_load(name)),
                           project=project, repo=repo)
    conn.commit()
    conn.close()


def _run_main(argv):
    """Run view.main in-process; return (exit_code, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            code = view.main(argv)
        except SystemExit as exc:  # argparse errors
            code = exc.code
    return code, err.getvalue()


def _cli(script, *args, cwd=None):
    return subprocess.run([sys.executable, os.path.join(HERE, script)] + list(args),
                          capture_output=True, text=True, cwd=cwd or HERE)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class TestViewSingleRepo(_TmpCase):
    def setUp(self):
        super().setUp()
        self.db = self.path("journey.db")
        _build_store(self.db, [("bundle_sample.json", None, None)])

    def test_autodetect_writes_exactly_extract_output(self):
        out = self.path("views", "bundle.json")   # parent dir does not exist yet
        code, err = _run_main(["--store", self.db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", out])
        self.assertEqual(code, 0, err)
        with open(out, encoding="utf-8") as fh:
            got = json.load(fh)
        conn = graphstore.open_store(self.db)
        want = extract.extract(conn, "o", "r", "2026-05-01", "2026-05-31",
                               warn=lambda _m: None)
        conn.close()
        self.assertEqual(got, json.loads(json.dumps(want)))
        # Raw only: link adds the derived keys.
        self.assertNotIn("trains", got)
        self.assertNotIn("buckets", got)
        self.assertEqual(got["meta"]["owner"], "o")
        self.assertEqual(got["meta"]["repo"], "r")
        self.assertFalse(os.path.exists(out + ".tmp"))

    def test_explicit_scope_matches_autodetect_and_is_byte_stable(self):
        a, b = self.path("a.json"), self.path("b.json")
        self.assertEqual(_run_main(["--store", self.db, "--from", "2026-05-01",
                                    "--to", "2026-05-31", "--out", a])[0], 0)
        self.assertEqual(_run_main(["--store", self.db, "--project", "o",
                                    "--repo", "r", "--from", "2026-05-01",
                                    "--to", "2026-05-31", "--out", b])[0], 0)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_unknown_project_exits_2(self):
        code, err = _run_main(["--store", self.db, "--project", "nope",
                               "--from", "2026-05-01", "--to", "2026-05-31",
                               "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        self.assertFalse(os.path.exists(self.path("x.json")))

    def test_unknown_repo_exits_2_and_lists_repos(self):
        code, err = _run_main(["--store", self.db, "--repo", "nope",
                               "--from", "2026-05-01", "--to", "2026-05-31",
                               "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("'r'", err)
        self.assertFalse(os.path.exists(self.path("x.json")))

    def test_bad_dates_exit_2(self):
        for frm, to in (("May", "2026-05-31"), ("2026-05-31", "2026-05-01")):
            code, _ = _run_main(["--store", self.db, "--from", frm, "--to", to,
                                 "--out", self.path("x.json")])
            self.assertEqual(code, 2, (frm, to))


class TestViewStoreErrors(_TmpCase):
    def test_missing_store_exits_2_and_creates_nothing(self):
        db = self.path("absent.db")
        code, err = _run_main(["--store", db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("gather.py", err)
        self.assertFalse(os.path.exists(db))

    def test_empty_store_exits_2(self):
        db = self.path("empty.db")
        _build_store(db, [])
        code, err = _run_main(["--store", db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("empty store", err)

    def test_not_a_store_exits_2(self):
        db = self.path("junk.db")
        with open(db, "w") as fh:
            fh.write("this is not sqlite\n" * 20)
        code, err = _run_main(["--store", db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read store", err)

    def test_multiple_projects_need_project(self):
        db = self.path("two.db")
        _build_store(db, [("bundle_sample.json", "p1", "r"),
                          ("bundle_sample.json", "p2", "r")])
        code, err = _run_main(["--store", db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("--project", err)
        code, err = _run_main(["--store", db, "--project", "p2", "--from",
                               "2026-05-01", "--to", "2026-05-31",
                               "--out", self.path("x.json")])
        self.assertEqual(code, 0, err)


class TestViewMultiRepo(_TmpCase):
    def setUp(self):
        super().setUp()
        self.db = self.path("proj.db")
        _build_store(self.db, [("bundle_sample.json", "proj", "o/one"),
                               ("bundle_p2.json", "proj", "o/two")])

    def test_ambiguous_repo_exits_2_and_points_at_digest(self):
        code, err = _run_main(["--store", self.db, "--from", "2026-05-01",
                               "--to", "2026-05-31", "--out", self.path("x.json")])
        self.assertEqual(code, 2)
        self.assertIn("--repo", err)
        self.assertIn("digest.py", err)
        self.assertIn("o/one", err)

    def test_repo_selects_one_member(self):
        out = self.path("two.json")
        code, err = _run_main(["--store", self.db, "--repo", "o/two", "--from",
                               "2026-05-01", "--to", "2026-05-31", "--out", out])
        self.assertEqual(code, 0, err)
        with open(out, encoding="utf-8") as fh:
            got = json.load(fh)
        self.assertEqual(got["meta"]["owner"], "proj")
        self.assertEqual(got["meta"]["repo"], "o/two")
        conn = graphstore.open_store(self.db)
        want = extract.extract(conn, "proj", "o/two", "2026-05-01", "2026-05-31",
                               warn=lambda _m: None)
        conn.close()
        self.assertEqual(got, json.loads(json.dumps(want)))


class TestViewKeyOrderPreserved(_TmpCase):
    """Regression: the view file must keep extract's key order. bundle_p3 has
    same-timestamp artifact events whose order breaks timeline ties, so
    `json.dump(..., sort_keys=True)` would change link's timeline and
    feature_deltas versus enriching extract's in-memory result."""

    def test_link_on_view_file_equals_in_memory_enrich(self):
        db = self.path("p3.db")
        _build_store(db, [("bundle_p3.json", None, None)])
        out = self.path("p3.json")
        self.assertEqual(_run_main(["--store", db, "--from", "2026-05-01",
                                    "--to", "2026-05-31", "--out", out])[0], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            link.main([out])
        with open(out, encoding="utf-8") as fh:
            via_file = json.load(fh)
        conn = graphstore.open_store(db)
        mem = link.enrich(extract.extract(conn, "o", "r", "2026-05-01",
                                          "2026-05-31", warn=lambda _m: None))
        conn.close()
        self.assertEqual(json.dumps(via_file, sort_keys=True),
                         json.dumps(mem, sort_keys=True))


class TestEndToEnd(_TmpCase):
    """Store -> view.py -> link.py --series -> link.py --slice ->
    render.py --skip-validate, as SKILL.md documents, all as real processes."""

    def test_pipeline_from_fixture_store(self):
        db = self.path("workspace", "journey.db")
        os.makedirs(os.path.dirname(db))
        _build_store(db, [("bundle_sample.json", None, None)])
        bundle = self.path("workspace", "view", "bundle.json")
        series = self.path("workspace", "series.json")
        diagrams = self.path("workspace", "diagrams")

        r = _cli("view.py", "--store", db, "--from", "2026-05-01",
                 "--to", "2026-05-31", "--out", bundle)
        self.assertEqual(r.returncode, 0, r.stderr)

        r = _cli("link.py", bundle, "--series", series)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(bundle, encoding="utf-8") as fh:
            linked = json.load(fh)
        self.assertIn("series", linked)
        self.assertTrue(linked["trains"], "fixture should yield trains")
        train_id = linked["trains"][0]["id"]

        r = _cli("render.py", bundle, "--diagrams-dir", diagrams, "--skip-validate")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(bundle, encoding="utf-8") as fh:
            rendered = json.load(fh)
        # diagram paths are relative to the PARENT of --diagrams-dir.
        base = os.path.dirname(diagrams)
        self.assertTrue(rendered["diagrams"])
        for name, rel in rendered["diagrams"].items():
            if name == "train_flowcharts":
                continue
            self.assertTrue(rel.startswith("diagrams/"), rel)
            self.assertTrue(os.path.isfile(os.path.join(base, rel)), rel)

        r = _cli("link.py", bundle, "--slice", train_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["train"]["id"], train_id)


# --- docs contract -------------------------------------------------------------

_DOCS = ("SKILL.md", os.path.join("commands", "activity.md"))
# Scripts whose argv is hand-rolled (no argparse --help to read flags from):
# the exact flags each accepts. link.py: `BUNDLE [--slice ID | --series PATH]`.
_HAND_ROLLED = {"link.py": {"--slice", "--series"}, "transcript.py": set()}
_PY_CMD = re.compile(r"python3\s+(\S+\.py)\b([^\n]*)")
# Where the command's own argv ends (pipe / redirect / chain / comment). `<x>`
# is a placeholder in these docs, never an input redirect.
_SHELL_END = re.compile(r"\s(?:\||>|;|&&|#)\s|\s>\S")


def _command_lines(text):
    """Every `python3 <script>.py ...` invocation in a Markdown doc: lines of
    fenced code blocks (with `\\` continuations joined) and inline code spans
    (which may wrap across lines)."""
    cmds = []
    fence = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.S | re.M)
    for block in fence.findall(text):
        for line in block.replace("\\\n", " ").splitlines():
            cmds.extend(m.group(0) for m in _PY_CMD.finditer(line))
    prose = fence.sub("", text)
    for span in re.findall(r"`([^`]+)`", prose):
        span = " ".join(span.split())
        cmds.extend(m.group(0) for m in _PY_CMD.finditer(span))
    return cmds


def _script_and_flags(cmd):
    m = _PY_CMD.match(cmd)
    script, rest = m.group(1), m.group(2)
    end = _SHELL_END.search(" " + rest)
    if end:
        rest = (" " + rest)[:end.start()]
    script = re.sub(r"^(\$\{?CLAUDE_SKILL_DIR\}?|\$AO)/", "", script)
    tokens = rest.replace("[", " ").replace("]", " ").replace("|", " ").split()
    flags = [t.split("=", 1)[0] for t in tokens if t.startswith("-") and t != "-"]
    return script, flags, tokens


_HELP_CACHE = {}


def _argparse_flags(script):
    if script not in _HELP_CACHE:
        r = _cli(script, "--help")
        assert r.returncode == 0, (script, r.stderr)
        _HELP_CACHE[script] = set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)",
                                             r.stdout))
    return _HELP_CACHE[script]


class TestDocsContract(unittest.TestCase):
    def _cmds(self, doc):
        with open(os.path.join(HERE, doc), encoding="utf-8") as fh:
            return _command_lines(fh.read())

    def test_extractor_sees_fenced_inline_and_continued_commands(self):
        text = ("x `python3 a.py <p> --p\n  q --r s` y\n   ```bash\n"
                "   python3 b.py --u \\\n  --v w > out.json --no\n   ```\n")
        got = [_script_and_flags(c)[:2] for c in _command_lines(text)]
        self.assertEqual(sorted(got), [("a.py", ["--p", "--r"]),
                                       ("b.py", ["--u", "--v"])])

    def test_docs_document_the_view_step(self):
        for doc in _DOCS:
            scripts = {_script_and_flags(c)[0] for c in self._cmds(doc)}
            for needed in ("gather.py", "validate.py", "view.py", "link.py",
                           "render.py"):
                self.assertIn(needed, scripts, "{} lacks {}".format(doc, needed))

    def test_every_command_names_a_real_script_and_accepted_flags(self):
        for doc in _DOCS:
            cmds = self._cmds(doc)
            self.assertTrue(cmds, doc)
            for cmd in cmds:
                script, flags, tokens = _script_and_flags(cmd)
                where = "{}: {!r}".format(doc, cmd)
                self.assertTrue(os.path.isfile(os.path.join(HERE, script)), where)
                if script in _HAND_ROLLED:
                    allowed = _HAND_ROLLED[script]
                    self.assertLessEqual(len(flags), 1, where)
                    if flags:
                        # link.py recognizes the flag only as the 2nd token.
                        self.assertEqual(tokens.index(flags[0]), 1, where)
                else:
                    allowed = _argparse_flags(script)
                for flag in flags:
                    self.assertIn(flag, allowed, where)


if __name__ == "__main__":
    unittest.main()
