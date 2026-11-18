"""The cyan install hint is pinned to a commit, in code and in the README.

A tag archive (`archive/refs/tags/v1.4.4.zip`) follows the tag, and a tag can
be moved: a fresh install could fetch code nobody tested under a name that
says it was. The archive of a commit sha cannot change. The sha is the one tag
v1.4.4 points at (`git ls-remote … refs/tags/v1.4.4`, a lightweight tag), the
release Graft is tested with.
"""

from __future__ import annotations

import re
from pathlib import Path

from graft_ipa import cyan_runner, preflight

ROOT = Path(__file__).resolve().parent.parent
V1_4_4 = "04a40f2aa65ec9cb36127f74d52ae4ef71377c50"
PINNED = f"https://github.com/asdfzxcvbn/pyzule-rw/archive/{V1_4_4}.zip"


def test_the_install_command_is_pinned_to_the_v1_4_4_commit() -> None:
    assert cyan_runner.CYAN_INSTALL_COMMAND == f"pipx install --force {PINNED}"


def test_the_pin_is_a_full_commit_sha_and_not_a_ref() -> None:
    url = cyan_runner.CYAN_INSTALL_COMMAND.split()[-1]
    assert re.fullmatch(r"https://github\.com/asdfzxcvbn/pyzule-rw/archive/[0-9a-f]{40}\.zip", url)


def test_preflight_uses_the_pinned_command() -> None:
    assert preflight.REQUIRED_TOOLS["cyan"] == cyan_runner.CYAN_INSTALL_COMMAND


def test_nothing_in_the_package_or_readme_installs_cyan_from_a_moveable_ref() -> None:
    sources = [*(ROOT / "graft_ipa").glob("*.py"), ROOT / "README.md"]
    for path in sources:
        text = path.read_text()
        assert "pyzule-rw/archive/main" not in text, path
        assert "pyzule-rw/archive/refs/" not in text, path


def test_the_readme_install_line_uses_the_pinned_archive() -> None:
    assert PINNED in (ROOT / "README.md").read_text()
