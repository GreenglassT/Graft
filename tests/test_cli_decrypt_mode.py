"""`--decrypt-mode` takes `all` and nothing else on the verbs that stage a dump.

`--decrypt` and `--decrypt-frontmost` dump an app, verify it is cryptid 0 and
stage it for a build, and only bagbak's `all` mode dumps an app. `main` writes
bare binaries with no `Payload`, so the dump died at reading its Info.plist
(exit 5, naming a temp path already deleted); under Automatic a bagbak failure
fell back to frida-ios-dump, which has no mode and staged the whole app the
user had asked not to dump; and `ext` was never shown to produce anything a
build could use. Each mode but `all` is refused before the device is touched.

What is accepted is not passed on: `all` is the only mode there is past the
flag, so it is fixed where the bagbak argv is built (`decrypt_runner`), and
nothing between the flag and there carries a mode at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import decrypt_runner


def _invoke(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


@pytest.fixture
def dumps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Whether a dump ran — and a frontmost app to find. A dump takes no mode:
    the whole app is the only thing it makes."""
    seen: list[str] = []

    def dump(*a, **k):
        assert "mode" not in k and "decrypt_mode" not in k, k
        seen.append("dumped")

    monkeypatch.setattr(cli_mod, "_decrypt_escalating", dump)
    def frontmost(udid):
        seen.append("asked the device")
        return "com.example.app"

    monkeypatch.setattr(decrypt_runner, "frontmost_app", frontmost)
    return seen


@pytest.mark.parametrize("verb", [
    ["--decrypt", "com.example.app"],
    ["--decrypt-frontmost"],
])
@pytest.mark.parametrize("mode", ["main", "ext", "ALL"])
def test_a_mode_that_cannot_make_an_app_is_refused_first(
    temp_config_env: Path, dumps: list[str], verb: list[str], mode: str
) -> None:
    result = _invoke(["--json", *verb, "--device", "U1", "--decrypt-mode", mode])

    assert result.exit_code == 2, result.output
    assert dumps == [], "the device was asked or a dump ran"
    said = " ".join(result.output.split())
    assert f"--decrypt-mode {mode} is not supported" in said, said
    assert "only all dumps a whole app" in said, said


@pytest.mark.parametrize(("verb", "expect"), [
    (["--decrypt", "com.example.app"], ["dumped"]),
    (["--decrypt-frontmost"], ["asked the device", "dumped"]),
])
@pytest.mark.parametrize("mode", [["--decrypt-mode", "all"], []])
def test_all_and_no_mode_still_dump_the_whole_app(
    temp_config_env: Path, dumps: list[str], verb: list[str], expect: list[str],
    mode: list[str],
) -> None:
    result = _invoke(["--json", *verb, "--device", "U1", *mode])

    assert result.exit_code == 0, result.output
    assert dumps == expect


def test_the_dump_is_always_of_the_whole_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fixed at the `decrypt_runner` boundary: a bagbak with a mode positional
    is always told `all`, and a call cannot ask for another."""
    import inspect

    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/usr/local/bin/bagbak")
    monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 5)

    result = decrypt_runner.decrypt_app("com.example.app", tmp_path, udid="U", dry_run=True)

    assert result.stdout.split()[-2:] == ["com.example.app", "all"], result.stdout
    for fn in (decrypt_runner.decrypt_app, decrypt_runner.build_command):
        assert "mode" not in inspect.signature(fn).parameters, fn.__name__
