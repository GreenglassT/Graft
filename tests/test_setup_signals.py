"""The sentence the Mac app's setup screen reads off a missing config.

The app's first call is `graft --json --list`. With no config.yaml it fails
before there is an event stream to report into, so the app tells "create your
config" apart from every other config problem by `ConfigError`'s exit status
and the start of this one message (`CLIContract.Message.configNotFound`).
Rewording it here sends every new user back to a modal alert, so it is pinned
from this side against the Swift literal itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graft_ipa.config import load_config, read_settings
from graft_ipa.errors import ConfigError
from tests.cli_ast import contract_swift_source


def _swift_prefix() -> str:
    match = re.search(r'static let configNotFound = "([^"\\]*)"', contract_swift_source())
    assert match, "CLIContract.Message.configNotFound is gone from CLIContract.swift"
    return match.group(1)


def _swift_config_exit() -> int:
    match = re.search(r"static let configError: Int32 = (\d+)", contract_swift_source())
    assert match, "CLIContract.Exit.configError is gone from CLIContract.swift"
    return int(match.group(1))


@pytest.mark.parametrize("read", [load_config, read_settings])
def test_a_missing_config_starts_with_the_sentence_the_app_matches(tmp_path: Path, read) -> None:
    missing = tmp_path / "config.yaml"
    with pytest.raises(ConfigError) as caught:
        read(missing)
    # `.message`, not `str(e)`: the hint is appended to the latter, and the app
    # matches the `error:` line alone.
    assert caught.value.message == f"{_swift_prefix()}{missing}"
    assert caught.value.exit_code == _swift_config_exit()
