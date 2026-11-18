"""Every string the app decodes from an `app` event is a string (K3).

YAML types what it finds: `display_name: 2048` is an int, `tweaks: [1.0]` a
float. `AppConfig.from_dict` passed both through, the `--list` event carried a
number where the app's `AppRecord` expects a string, the decode threw, and the
app's `try?` dropped the whole app from the list without a word. Text fields
are now made text when the config is read, and a value that cannot be what its
key needs (a compression level of `high`, an app named by a bare number) is a
ConfigError naming the key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa.cli import app as cli_app
from graft_ipa.config import load_config
from graft_ipa.errors import ConfigError


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "xdg-config" / "graft" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


NUMERIC = """\
default_device_udid: 1234567890
apps:
  example:
    bundle_id: com.example.app
    preferred_version: 19.0
    tweaks: [1.0, a.deb, 7]
    lc_bundle_id: com.kdt.livecontainer
    cyan:
      display_name: 2048
      new_bundle_id: 42
      extra_args: [--x, 3]
"""


def test_numbers_in_text_fields_load_as_text(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, NUMERIC))
    app = cfg.apps["example"]

    assert app.cyan.display_name == "2048"
    assert app.tweaks == ["1.0", "a.deb", "7"]
    assert app.preferred_version == "19.0"
    assert app.cyan.new_bundle_id == "42"
    assert app.cyan.extra_args == ["--x", "3"]
    assert cfg.default_device_udid == "1234567890"


def test_a_bare_tweak_name_is_a_list_of_one(tmp_path: Path) -> None:
    """`tweaks: a.deb` is a string, and `list()` of it is one tweak per letter."""
    cfg = load_config(_write(tmp_path, "apps:\n  example:\n    bundle_id: c\n    tweaks: a.deb\n"))

    assert cfg.apps["example"].tweaks == ["a.deb"]


def test_the_list_event_carries_strings_where_the_app_decodes_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, NUMERIC)
    result = CliRunner().invoke(cli_app, ["--json", "--config", str(path), "--list"])

    assert result.exit_code == 0, result.output
    (event,) = [json.loads(line) for line in result.stdout.splitlines()
                if line.startswith("{") and json.loads(line)["type"] == "app"]
    for key in ("name", "bundle_id", "preferred_version", "display_name", "delivery",
                "lc_bundle_id"):
        assert isinstance(event[key], str), (key, event[key])
    assert all(isinstance(t, str) for t in event["tweaks"])
    assert isinstance(event["compression_level"], int)


#: Configs `load_config` refuses, each with the key its message must name.
UNLOADABLE = [
    ("apps:\n  2048:\n    bundle_id: c\n", "2048"),
    ("apps:\n  example:\n    bundle_id: c\n    cyan:\n      compression_level: high\n",
     "compression_level"),
    ("apps:\n  example:\n    bundle_id: c\n    tweaks: {a: b}\n", "tweaks"),
    ("apps:\n  broken: com.example.app\n", "broken"),
    ("apps:\n  broken:\n", "broken"),
]


@pytest.mark.parametrize(("text", "named"), UNLOADABLE)
def test_a_value_that_cannot_be_what_its_key_needs_names_the_key(
    tmp_path: Path, text: str, named: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, text))

    assert named in exc.value.message


# --- a hint the user can follow (P3) ---------------------------------------------
#
# An app entry that is not a mapping said "remove it with `graft --remove-app
# broken`", and --remove-app loads the config first: it failed with the same
# error, the hint pointing back at itself. A config that will not load can
# only be fixed by editing it.


def test_an_app_entry_that_is_not_a_mapping_says_which_file_and_key_to_edit(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "apps:\n  broken: com.example.app\n")

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    hint = exc.value.hint or ""
    assert hint.startswith(f"edit {path}"), hint
    assert "`broken:`" in hint and "`apps:`" in hint and "bundle_id" in hint


@pytest.mark.parametrize("text", [
    *(text for text, _ in UNLOADABLE),
    "signing: just-a-string\n",
    "cert: /somewhere/AppleDevelopment.p12\n",  # a signing key outside signing:
    "archive_keep: -1\n",
    None,  # no config.yaml at all: the hint names `graft --init`, which needs none
])
def test_no_load_error_hint_names_a_command_that_needs_the_config_to_load(
    tmp_path: Path, text: str | None
) -> None:
    from graft_ipa.cli import _VERBS, _Needs, _Verb

    path = _write(tmp_path, text) if text is not None else tmp_path / "absent.yaml"
    with pytest.raises(ConfigError) as exc:
        load_config(path)

    needs = {v.flag: v.needs for v in _VERBS if isinstance(v, _Verb)}
    for flag in re.findall(r"`graft (?:--config \S+ )?(--[a-z0-9-]+)", exc.value.hint or ""):
        assert needs[flag] is not _Needs.LOADED, (
            f"the hint sends the user to {flag}, which loads this config first "
            f"and fails the same way: {exc.value.hint}"
        )
