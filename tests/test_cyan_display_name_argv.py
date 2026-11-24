"""The display name reaches cyan as one argv token, `-n=<name>`.

cyan parses its argv with argparse (`cyan/__main__.py`: `-n`, metavar `name`,
then `parse_args()`). Given `-n <name>` as two tokens, argparse takes a name
that starts with `-` and has no space (`-Notes`, `-u`, `--force`) for an
option, and every build of that app fails with "argument -n: expected one
argument". Search adds an app under the store's own name, so a listing can put
one there. `-n=<name>` is split at the first `=` and is never read as an
option, on the bundled Python 3.13 as on 3.12 and 3.14.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from graft_ipa.cyan_runner import build_command
from tests.test_cyan_runner import make_app, make_cfg


def _cyan_parser() -> argparse.ArgumentParser:
    """cyan 1.4.4's options, as its `__main__.py` declares them."""
    parser = argparse.ArgumentParser(exit_on_error=False)
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output")
    parser.add_argument("-z", "--cyan", nargs="+")
    parser.add_argument("-f", nargs="+")
    for flag in "nvbmklx":
        parser.add_argument(f"-{flag}")
    for flag in ("-u", "-w", "-d", "-s", "-q", "-e", "-g", "--ignore-encrypted", "--overwrite"):
        parser.add_argument(flag, action="store_true")
    parser.add_argument("-c", "--compress", type=int, default=6, choices=range(0, 10))
    return parser


@pytest.mark.parametrize("name", ["-Notes", "-u", "--force", "-ELEMENT-", "=x", "a=b", "Plain Name"])
def test_any_display_name_reaches_cyan_as_the_name(tmp_path: Path, name: str) -> None:
    app = make_app(keep_bundle_id=True)
    app.cyan.display_name = name
    cmd = build_command(make_cfg(tmp_path), app, tmp_path / "in.ipa", tmp_path / "out.ipa")

    assert f"-n={name}" in cmd
    parsed = _cyan_parser().parse_args(cmd[1:])
    assert parsed.n == name
    assert parsed.u is True, "the next flag is still a flag"
