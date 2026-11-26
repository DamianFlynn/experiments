"""Guard: every test in this skill dir must be collectable by the project's test
command, ``python3 -m unittest discover -s .claude/skills/activity-overview
-p 'test_*.py'``.

``unittest`` collects only ``TestCase`` methods, so a module-level ``def
test_*`` (pytest style) is silently never run, and pytest is not a dependency
of this stdlib-only skill. Issue #99 found 27 such tests in test_validate.py;
this guard makes the pattern fail loudly instead of passing unnoticed.
"""

import ast
import contextlib
import glob
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


# --- stdout guard -----------------------------------------------------------
# unittest writes its summary ("Ran N tests" / "OK") to stderr, so anything a
# test lets through to stdout lands AFTER the result and `... 2>&1 | tail -1`
# no longer shows it (issue #97, finding 3). Running the suite a second time
# under capture would double its cost, and an AST scan for print()/main()
# calls is guesswork, so the check is a tripwire instead: discovery imports
# every test module (this one included) before any test runs, and importing
# this module wraps sys.stdout once. A write that reaches the wrapper while a
# TestCase is on the call stack registers a failing cleanup on that test, so
# the offending test itself fails, named exactly, whatever order the modules
# run in. It costs nothing on a quiet suite (no writes, no work), passes the
# text through unchanged, and never raises into the code under test. A test
# that captures its output (contextlib.redirect_stdout, or unittest -b)
# replaces sys.stdout and so never reaches the wrapper. Output a subprocess
# writes to file descriptor 1 directly is out of its reach; such tests capture
# the child's output with subprocess.run(capture_output=True) anyway.

def _test_on_stack(frame):
    while frame is not None:
        obj = frame.f_locals.get("self")
        if isinstance(obj, unittest.TestCase):
            return obj
        frame = frame.f_back
    return None


class StdoutGuard:
    """Pass-through stdout wrapper that fails the test that writes to it."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        if text:
            test = _test_on_stack(sys._getframe(1))
            if test is not None and not getattr(test, "_stdout_guard_hit", False):
                test._stdout_guard_hit = True
                test.addCleanup(test.fail, (
                    "test wrote to stdout during the run: {!r}; capture it "
                    "with contextlib.redirect_stdout and assert on it (stdout "
                    "lands after unittest's summary)".format(text[:80])))
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


if not isinstance(sys.stdout, StdoutGuard):
    sys.stdout = StdoutGuard(sys.stdout)


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


class StdoutGuardWorks(unittest.TestCase):
    """The tripwire at the top of this file must fail exactly the test that
    writes to stdout, and nothing else."""

    @staticmethod
    def _run(body):
        class Dummy(unittest.TestCase):
            def test_it(self):
                body()
        result = unittest.TestResult()
        Dummy("test_it").run(result)
        return result

    def test_guard_is_installed(self):
        # Under `unittest -b` the runner swaps in its own buffer per test.
        self.assertIsInstance(sys.stdout, (StdoutGuard, io.StringIO))

    def test_write_inside_a_test_fails_that_test_and_passes_through(self):
        sink = io.StringIO()

        def body():
            with contextlib.redirect_stdout(StdoutGuard(sink)):
                print('{"leaked": true}')
        result = self._run(body)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertIn("wrote to stdout", result.failures[0][1])
        self.assertEqual(sink.getvalue(), '{"leaked": true}\n')

    def test_captured_output_does_not_fail(self):
        def body():
            with contextlib.redirect_stdout(StdoutGuard(io.StringIO())):
                with contextlib.redirect_stdout(io.StringIO()):
                    print("captured")
        result = self._run(body)
        self.assertTrue(result.wasSuccessful(), result.failures)

    def test_write_with_no_test_on_the_stack_passes_through(self):
        import threading
        sink = io.StringIO()
        guard = StdoutGuard(sink)
        t = threading.Thread(target=guard.write, args=("x",))
        t.start()
        t.join()
        self.assertEqual(sink.getvalue(), "x")
