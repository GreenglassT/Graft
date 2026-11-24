"""Loaded by every Python process the test suite starts (conftest puts this
folder on its PYTHONPATH): blocks the network as `tests/network_guard.py`
blocks it in the suite's own process, and records what it refused in the
file $GRAFT_TEST_NETWORK_LOG names, which the test's teardown reads. And,
always, refuses to start `security`, `osascript`, `sudo` and the rest
(`tests/program_guard.py`), recording that in $GRAFT_TEST_PROGRAM_LOG.

It shadows any other sitecustomize, so it runs the next one on sys.path
after this folder: Homebrew's Python sets sys.executable in its own (see
tests/test_no_external_tools.py), and that one's spy chains on the same way.
"""

import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))

# First, and whatever else is set: no child of the suite starts a program
# that can show a dialog or act with privilege.
_pspec = importlib.util.spec_from_file_location(
    "_graft_test_program_guard", os.path.join(os.path.dirname(_here), "program_guard.py"))
_programs = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(_programs)
_programs.install_in_child(os.environ.get("GRAFT_TEST_PROGRAM_LOG"))

_log = os.environ.get("GRAFT_TEST_NETWORK_LOG")
if _log:
    _spec = importlib.util.spec_from_file_location(
        "_graft_test_network_guard", os.path.join(os.path.dirname(_here), "network_guard.py"))
    _guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_guard)
    _guard.install_in_child(_log)

_after = False
for _entry in sys.path:
    _dir = os.path.abspath(_entry or ".")
    if _dir == _here:
        _after = True
        continue
    _next = os.path.join(_dir, "sitecustomize.py")
    if _after and os.path.isfile(_next):
        _spec = importlib.util.spec_from_file_location("_chained_sitecustomize", _next)
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
        break
