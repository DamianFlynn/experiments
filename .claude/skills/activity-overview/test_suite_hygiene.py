"""Guard: every test in this skill dir must be collectable by the project's test
command, ``python3 -m unittest discover -s .claude/skills/activity-overview
-p 'test_*.py'``.

``unittest`` collects only ``TestCase`` methods, so a module-level ``def
test_*`` (pytest style) is silently never run, and pytest is not a dependency
of this stdlib-only skill. Issue #99 found 27 such tests in test_validate.py;
this guard makes the pattern fail loudly instead of passing unnoticed.
"""

import ast
import glob
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def hygiene_violations(source, filename="<string>"):
    """Return a sorted list of human-readable violations in ``source``:
    module-level ``test_*`` functions (sync or async) and any import of
    ``pytest``."""
    tree = ast.parse(source, filename=filename)
    out = []
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")):
            out.append("{}:{}: module-level test function {!r} is never "
                       "collected by unittest".format(
                           filename, node.lineno, node.name))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name == "pytest" or name.startswith("pytest.") \
                    or name == "_pytest" or name.startswith("_pytest."):
                out.append("{}:{}: imports {!r} (skill is stdlib-only; "
                           "pytest is not installed)".format(
                               filename, node.lineno, name))
    return sorted(out)


def _test_files():
    return sorted(glob.glob(os.path.join(HERE, "test_*.py")))


class TestSuiteHygiene(unittest.TestCase):
    def test_test_files_found(self):
        # Sanity: the glob must actually see this suite (incl. this file),
        # otherwise the guard below would pass vacuously.
        names = {os.path.basename(p) for p in _test_files()}
        self.assertIn("test_suite_hygiene.py", names)
        self.assertIn("test_validate.py", names)

    def test_no_module_level_tests_or_pytest_in_any_test_file(self):
        problems = []
        for path in _test_files():
            with open(path, encoding="utf-8") as fh:
                problems.extend(hygiene_violations(
                    fh.read(), os.path.basename(path)))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    # --- the checker itself must catch what it claims to ------------------

    def test_checker_flags_module_level_test_function(self):
        src = "def test_foo(tmp_path):\n    assert True\n"
        v = hygiene_violations(src, "x.py")
        self.assertEqual(len(v), 1, v)
        self.assertIn("test_foo", v[0])

    def test_checker_flags_async_module_level_test_function(self):
        v = hygiene_violations("async def test_bar():\n    pass\n", "x.py")
        self.assertEqual(len(v), 1, v)

    def test_checker_flags_pytest_imports(self):
        for src in ("import pytest\n", "from pytest import raises\n",
                    "import pytest as pt\n",
                    "def helper():\n    import pytest\n"):
            with self.subTest(src=src):
                self.assertEqual(len(hygiene_violations(src, "x.py")), 1, src)

    def test_checker_allows_testcase_methods_and_helpers(self):
        src = ("import unittest\n"
               "def _helper():\n    return 1\n"
               "class TestX(unittest.TestCase):\n"
               "    def test_ok(self):\n        self.assertTrue(True)\n")
        self.assertEqual(hygiene_violations(src, "x.py"), [])


if __name__ == "__main__":
    unittest.main()


class NoMergeConflictMarkers(unittest.TestCase):
    """A conflict resolved by hand can leave `<<<<<<<` / `>>>>>>>` blocks in a
    file that no test reads (docs). This reached the default branch once; fail
    the suite if any tracked text file in the skill still carries them."""

    _MARKERS = ("<<<<<<< ", ">>>>>>> ")
    _SUFFIXES = (".py", ".md", ".json", ".yml", ".yaml", ".txt", ".csv", ".mmd")

    def test_no_conflict_markers_in_skill_files(self):
        root = os.path.dirname(os.path.abspath(__file__))
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("workspace", "__pycache__", ".git")]
            for name in filenames:
                if not name.endswith(self._SUFFIXES):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) == os.path.abspath(__file__):
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if line.startswith(self._MARKERS):
                            hits.append("%s:%d" % (os.path.relpath(path, root), n))
        self.assertEqual(hits, [], "merge-conflict markers left in: %s" % hits)
