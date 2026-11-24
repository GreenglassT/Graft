"""The pymobiledevice3 range is enforced, not only declared (re-check #41).

`>=10.4.0,<12` is the range the suite has run against (10.4.0 and 11.x);
12 is a major version nobody has. Nothing read the specifier: dropping the
cap, or a re-lock that pinned 12.x into the bundle, stayed green, and the
bundle installs its lock with --no-deps, so pip never compares the two.
pytest needs `packaging`, so it is always here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent


def _declared() -> Requirement:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    (req,) = [Requirement(d) for d in deps if Requirement(d).name == "pymobiledevice3"]
    return req


def test_the_declared_range_has_its_floor_and_its_cap() -> None:
    spec = _declared().specifier
    assert Version("10.4.0") in spec and Version("11.99.99") in spec
    assert Version("10.3.9") not in spec
    assert Version("12.0.0") not in spec, "the <12 cap is gone"


def test_the_bundle_pins_a_version_inside_it() -> None:
    lock = (REPO / "bundle" / "requirements.lock").read_text()
    (pinned,) = re.findall(r"^pymobiledevice3==(\S+)", lock, re.MULTILINE)
    assert Version(pinned) in _declared().specifier, f"the bundle pins {pinned}"


#: Every public method of `HouseArrestService` (and so of `AfcService` and
#: `LockdownService`, which it extends) in pymobiledevice3 10.4.0, the
#: declared floor, read off the 10.4.0 sources. No `fseek` or `ftell`: those
#: came later (11.58.0 has both), so a call to one failed with AttributeError
#: on a floor install (combined fix check #5).
FLOOR_AFC_API = frozenset({
    "aclose", "close", "connect", "create", "dirlist", "exists", "fclose", "fopen", "fread", "fwrite",
    "get_device_info", "get_file_contents", "isdir", "link", "listdir", "lock", "makedirs", "os_stat",
    "pull", "push", "rename", "resolve_path", "rm", "rm_single", "send_command", "service",
    "set_file_contents", "shell", "stat", "wait_exists", "walk",
})
#: Methods graft uses only behind `getattr(ha, name, None)`, with a fallback
#: for a floor install.
OPTIONAL_AFC_API = frozenset({"fseek"})
#: What graft calls its AFC and HouseArrest services.
_AFC_NAMES = frozenset({"ha", "afc"})


def _afc_uses() -> tuple[set[str], set[str]]:
    """(methods called on an AFC service, names looked up with getattr) in graft_ipa."""
    import ast

    called, looked_up = set(), set()
    for py in (REPO / "graft_ipa").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in _AFC_NAMES:
                called.add(f.attr)
            if (isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in _AFC_NAMES
                    and isinstance(node.args[1], ast.Constant)):
                assert len(node.args) == 3, "an optional method needs a default, for the floor"
                looked_up.add(node.args[1].value)
    return called, looked_up


def test_every_afc_call_graft_makes_exists_at_the_declared_floor() -> None:
    """The floor is 10.4.0 because the AFC calls installs make exist there:
    a call to a method 10.4.0 lacks has to sit behind a getattr fallback."""
    assert Version("10.4.0") in _declared().specifier and Version("10.3.9") not in _declared().specifier
    called, looked_up = _afc_uses()
    assert "fopen" in called and "fread" in called, "premise: the scan sees the tail read"
    assert called - FLOOR_AFC_API == set(), "not in pymobiledevice3 10.4.0: raise the floor, or fall back"
    assert looked_up <= OPTIONAL_AFC_API | FLOOR_AFC_API, looked_up


def test_the_recorded_floor_api_is_still_there_in_the_installed_release() -> None:
    """The list above is not invented: every method on it exists in the
    pymobiledevice3 this suite runs against (10.4.0 or 11.x)."""
    from pymobiledevice3.services.house_arrest import HouseArrestService

    assert {m for m in FLOOR_AFC_API if not hasattr(HouseArrestService, m)} == set()
