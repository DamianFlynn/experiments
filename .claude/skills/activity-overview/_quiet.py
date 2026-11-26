"""Test helper: keep the diagnostics a passing test expects off the terminal.

Many CLI paths say what they did on stderr ("folded ...", "note: ...",
"warning: ..."); a test that exercises them in-process would otherwise spray
that text between unittest's progress dots. ``quiet(self)`` in ``setUp`` (or
at the top of one test) sends ``sys.stderr`` to an in-memory buffer until the
test ends and returns the buffer, so a test can still assert on what was said.
Nothing is deleted: the code under test writes exactly as before.
"""

import contextlib
import io


def quiet(test):
    """Redirect sys.stderr for the rest of ``test``; return the buffer."""
    err = io.StringIO()
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stderr(err))
    test.addCleanup(stack.close)
    return err
