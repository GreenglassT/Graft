"""Exit codes have one table, and usbmuxd being down is said plainly (K13, K14).

- K13: `DecryptError` and `LiveContainerError` both exited 11, while
  errors.py claimed to be the one list of the codes that are taken. The app
  keys on 2, 16 and 17 and on neither of these, so LiveContainer gets a code of
  its own, and every code is in `errors.EXIT_CODES`.
- K14: with usbmuxd down, `--devices` and `--device-apps` exited 1 with
  "ConnectionFailedToUsbmuxdError … that isn't a failure graft knows how to
  explain", where `--lc-clean` said what to check. It is a failure graft
  knows now, said once, at the error boundary, for every verb.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import graft_ipa
from graft_ipa import cli as cli_mod
from graft_ipa import errors
from graft_ipa.decrypt_runner import DecryptError
from graft_ipa.lc_runner import LiveContainerError


def _all_error_classes() -> list[type]:
    for info in pkgutil.iter_modules(graft_ipa.__path__):
        importlib.import_module(f"graft_ipa.{info.name}")
    found, todo = [], [errors.GraftError]
    while todo:
        cls = todo.pop()
        found.append(cls)
        todo.extend(cls.__subclasses__())
    return [c for c in found if c.__module__.startswith("graft_ipa.")]


def test_every_exit_code_a_class_sets_is_in_the_table() -> None:
    own = [(c, c.__dict__["exit_code"]) for c in _all_error_classes() if "exit_code" in c.__dict__]

    assert own, "the walk found no error classes"
    for cls, code in own:
        assert code in errors.EXIT_CODES, f"{cls.__qualname__} exits {code}, not in EXIT_CODES"


def test_no_two_error_families_share_a_code() -> None:
    by_code: dict[int, list[str]] = {}
    for cls in _all_error_classes():
        if "exit_code" in cls.__dict__:
            by_code.setdefault(cls.__dict__["exit_code"], []).append(cls.__qualname__)

    shared = {code: names for code, names in by_code.items() if len(names) > 1}
    assert shared == {}


def test_livecontainer_and_decrypt_failures_are_told_apart() -> None:
    assert LiveContainerError("x").exit_code != DecryptError("x").exit_code
    assert LiveContainerError("x").exit_code == errors.EXIT_LIVECONTAINER


def test_the_codes_the_app_keys_on_have_not_moved() -> None:
    from graft_ipa.errors import ConfigError, StagedIPAInUseError, TweakInUseError

    assert (ConfigError("x").exit_code, TweakInUseError("x").exit_code,
            StagedIPAInUseError("x").exit_code) == (2, 16, 17)


def test_the_codes_outside_the_classes_are_in_the_table_too() -> None:
    for code in (3, 64, errors.EXIT_BROKEN_PIPE, 128 + 15):
        assert code in errors.EXIT_CODES


# --- K14 ---------------------------------------------------------------------------


def _usbmuxd_down(*_a, **_k):
    from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError

    raise ConnectionFailedToUsbmuxdError()


@pytest.mark.parametrize(("args", "target"), [
    (["--devices"], "list_devices"),
    (["--devices", "--brief"], "list_devices_brief"),
])
def test_usbmuxd_down_is_said_plainly(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], target: str
) -> None:
    monkeypatch.setattr(cli_mod, target, _usbmuxd_down)

    result = CliRunner().invoke(cli_mod.app, args)

    assert result.exit_code == 1
    assert "usbmuxd" in result.stderr
    assert "isn't a failure graft knows how to explain" not in result.stderr
    assert "ConnectionFailedToUsbmuxdError" not in result.stderr


def test_device_apps_with_usbmuxd_down_too(temp_config_env: Path, monkeypatch) -> None:
    from graft_ipa import device_apps

    async def down(*_a, **_k):
        _usbmuxd_down()

    monkeypatch.setattr("pymobiledevice3.usbmux.list_devices", down)
    monkeypatch.setattr(device_apps, "list_user_apps", _usbmuxd_down, raising=False)

    result = CliRunner().invoke(cli_mod.app, ["--device-apps", "--device", "UDID-X"])

    assert result.exit_code == 1, result.output
    assert "usbmuxd" in result.stderr
    assert "isn't a failure graft knows how to explain" not in result.stderr


def test_other_unexpected_errors_keep_the_raw_report() -> None:
    reported = cli_mod._unexpected(RuntimeError("something new"))

    assert reported.message == "RuntimeError: something new"
    assert "isn't a failure graft knows how to explain" in reported.hint
