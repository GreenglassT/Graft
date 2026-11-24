"""`--set-portal` / `--clear-portal`: the App Store Connect key, without prompts.

The Settings pane's "App Store Connect" section runs these. `--set-portal` is
the `--setup-portal` wizard's own steps — refuse a free config, need an
existing one, sanity-check the key by listing devices, copy the .p8 into the
config directory 0600 as `AuthKey_<key_id>.p8`, and write the section with
`device_udids` — with the answers given as flags instead of typed at prompts.

Nothing here touches the network: App Store Connect is the fake `Client`
below, installed where the wizard's own test installs it
(`setup_portal.apple_portal.ASCClient`). A test that expects a refusal before
the check installs one that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import apple_portal, setup_portal
from graft_ipa import cli as cli_mod
from graft_ipa.config import remove_signing_section

KEY_ID = "KEY1234567"
ISSUER = "00000000-0000-0000-0000-000000000000"


def _device(udid: str, status: str = "ENABLED") -> apple_portal.Device:
    return apple_portal.Device(id=udid, udid=udid, name="iPhone", platform="IOS", status=status)


class _Recorder:
    """A fake ASCClient factory: records the credentials it was built with."""

    def __init__(self, devices=(), error: apple_portal.PortalError | None = None) -> None:
        self.devices = list(devices)
        self.error = error
        self.creds: list[apple_portal.PortalCredentials] = []

    def __call__(self, creds):
        self.creds.append(creds)
        recorder = self

        class Client:
            def list_devices(self, **_):
                if recorder.error:
                    raise recorder.error
                return recorder.devices

        return Client()


@pytest.fixture
def asc(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder(devices=[
        _device("00008030-000000000000000A"),
        _device("00008030-000000000000000B", status="DISABLED"),
    ])
    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", recorder)
    return recorder


@pytest.fixture
def no_asc(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(creds):
        raise AssertionError("App Store Connect must not be asked")

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", refuse)


@pytest.fixture
def p8(tmp_path: Path) -> Path:
    path = tmp_path / "Downloads" / f"AuthKey_{KEY_ID}.p8"
    path.parent.mkdir(parents=True)
    path.write_text("-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n")
    return path


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def _set(p8: Path, *extra: str) -> list[str]:
    return [
        "--json", "--set-portal", "--asc-key-id", KEY_ID, "--asc-issuer-id", ISSUER,
        "--asc-key-file", str(p8), *extra,
    ]


def _section(config: Path):
    return yaml.safe_load(config.read_text()).get("app_store_connect")


# --- --set-portal -------------------------------------------------------------


def test_set_portal_does_what_the_wizard_does(temp_config_env: Path, asc: _Recorder, p8: Path) -> None:
    result = _run(_set(p8))

    assert result.exit_code == 0, result.output
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"
    assert dest.read_text() == p8.read_text()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert _section(temp_config_env) == {
        "key_id": KEY_ID,
        "issuer_id": ISSUER,
        "key_file": str(dest),
        "device_udids": ["00008030-000000000000000A"],
    }
    (event,) = [e for e in _events(result) if e["type"] == "portal-set"]
    assert event == {
        "type": "portal-set", "key_id": KEY_ID, "issuer_id": ISSUER,
        "key_file": str(dest), "devices": 2,
    }


def test_the_check_uses_the_chosen_file_before_anything_is_copied(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _run(_set(p8))

    (creds,) = asc.creds
    assert (creds.key_id, creds.issuer_id, creds.key_path) == (KEY_ID, ISSUER, p8)


def test_a_failed_check_writes_nothing_and_says_why(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    asc.error = apple_portal.PortalError(
        "ASC API 401 on GET /devices: NOT_AUTHORIZED", hint="check the key id and issuer id"
    )
    before = temp_config_env.read_text()

    result = _run(_set(p8))

    assert result.exit_code == apple_portal.PortalError.exit_code
    assert "NOT_AUTHORIZED" in result.stderr
    assert "check the key id and issuer id" in result.stderr
    assert temp_config_env.read_text() == before
    assert not (temp_config_env.parent / f"AuthKey_{KEY_ID}.p8").exists()
    assert not any(e["type"] == "portal-set" for e in _events(result))


def test_a_key_that_cannot_sign_is_reported_not_crashed_on(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, p8: Path
) -> None:
    """A file that is not an EC private key fails inside the JWT library, not
    as a PortalError. It still gets a sentence, and still writes nothing."""
    def client(creds):
        class Client:
            def list_devices(self, **_):
                raise ValueError("Could not deserialize key data")

        return Client()

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", client)

    result = _run(_set(p8))

    assert result.exit_code == apple_portal.PortalError.exit_code, result.output
    said = " ".join(result.stderr.split())
    assert "Could not deserialize key data" in said
    assert "isn't a failure graft knows how to explain" not in said
    assert _section(temp_config_env) is None


def test_a_free_config_is_refused_before_app_store_connect_is_asked(
    temp_config_env: Path, no_asc: None, p8: Path
) -> None:
    remove_signing_section(temp_config_env)
    before = temp_config_env.read_text()

    result = _run(_set(p8))

    assert result.exit_code == 2, result.output
    assert "needs a paid Apple Developer Program membership" in result.stderr
    assert temp_config_env.read_text() == before


def test_it_needs_an_existing_config(tmp_path: Path, no_asc: None, p8: Path) -> None:
    missing = tmp_path / "nowhere" / "config.yaml"

    result = _run(["--config", str(missing), *_set(p8)])

    assert result.exit_code == 2, result.output
    assert "config file not found at" in result.stderr
    assert not missing.exists()


def test_a_missing_key_file_is_refused_before_the_check(
    temp_config_env: Path, no_asc: None, tmp_path: Path
) -> None:
    result = _run(_set(tmp_path / "nope.p8"))

    assert result.exit_code == 2, result.output
    assert "nope.p8" in result.stderr
    assert _section(temp_config_env) is None


@pytest.mark.parametrize("missing", ["--asc-key-id", "--asc-issuer-id", "--asc-key-file"])
def test_every_field_is_required(
    temp_config_env: Path, no_asc: None, p8: Path, missing: str
) -> None:
    args = _set(p8)
    at = args.index(missing)
    del args[at:at + 2]

    result = _run(args)

    assert result.exit_code == 2, result.output
    assert missing in result.stderr


def test_a_blank_field_is_refused(temp_config_env: Path, no_asc: None, p8: Path) -> None:
    result = _run([
        "--json", "--set-portal", "--asc-key-id", "  ", "--asc-issuer-id", ISSUER,
        "--asc-key-file", str(p8),
    ])

    assert result.exit_code == 2, result.output
    assert "--asc-key-id" in result.stderr


def test_a_key_already_in_the_config_dir_is_used_in_place(
    temp_config_env: Path, asc: _Recorder
) -> None:
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"
    dest.write_text("key")
    dest.chmod(0o644)

    result = _run(_set(dest))

    assert result.exit_code == 0, result.output
    assert dest.read_text() == "key"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert _section(temp_config_env)["key_file"] == str(dest)


# --- the key file and the ids, handled safely ----------------------------------


@pytest.mark.parametrize("key_id", ["../x", "../../evil", "KEY/123456", "key1234567", "KEY12345", "KEY1234567X"])
def test_a_key_id_that_is_not_ten_capitals_and_digits_is_refused(
    temp_config_env: Path, no_asc: None, p8: Path, tmp_path: Path, key_id: str
) -> None:
    """It names a file: `AuthKey_<key_id>.p8` beside config.yaml. `../x` used to
    name one outside it."""
    before = temp_config_env.read_text()
    outside_before = sorted(q for q in tmp_path.rglob("*") if q.is_file())

    result = _run([
        "--json", "--set-portal", "--asc-key-id", key_id, "--asc-issuer-id", ISSUER,
        "--asc-key-file", str(p8),
    ])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "10-character id shown next to the key in App Store Connect" in said
    assert temp_config_env.read_text() == before
    assert sorted(q for q in tmp_path.rglob("*") if q.is_file()) == outside_before


@pytest.mark.parametrize("issuer", ["ISS", "not-a-uuid", "00000000-0000-0000-0000-00000000000", "../x"])
def test_an_issuer_id_that_is_not_a_uuid_is_refused(
    temp_config_env: Path, no_asc: None, p8: Path, issuer: str
) -> None:
    before = temp_config_env.read_text()

    result = _run([
        "--json", "--set-portal", "--asc-key-id", KEY_ID, "--asc-issuer-id", issuer,
        "--asc-key-file", str(p8),
    ])

    assert result.exit_code == 2, result.output
    assert "Issuer ID" in " ".join(result.stderr.split())
    assert temp_config_env.read_text() == before


def test_the_copy_is_0600_from_a_0644_source(temp_config_env: Path, asc: _Recorder, p8: Path) -> None:
    p8.chmod(0o644)

    assert _run(_set(p8)).exit_code == 0

    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(p8.stat().st_mode) == 0o644, "the user's own file is left as it was"


def test_the_key_is_never_under_its_name_wider_than_0600(
    temp_config_env: Path, asc: _Recorder, p8: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written into a 0600 file and renamed into place, so there is no moment
    where `AuthKey_<id>.p8` holds the key at the umask's mode — which a copy
    followed by a chmod has."""
    import os

    p8.chmod(0o644)
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"
    arrived: list[int] = []
    real_replace = os.replace

    def replace(src, dst, *a, **k):
        if Path(dst) == dest:
            arrived.append(stat.S_IMODE(os.lstat(src).st_mode))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)

    assert _run(_set(p8)).exit_code == 0
    assert arrived == [0o600], "the key reached its name some other way, or not owner-only"
    assert dest.read_text() == p8.read_text()


def test_a_symlink_at_the_destination_is_replaced_not_followed(
    temp_config_env: Path, asc: _Recorder, p8: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "elsewhere" / "victim.txt"
    victim.parent.mkdir()
    victim.write_text("do not touch")
    victim.chmod(0o644)
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"
    dest.symlink_to(victim)

    assert _run(_set(p8)).exit_code == 0

    assert victim.read_text() == "do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert not dest.is_symlink()
    assert dest.read_text() == p8.read_text()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_no_temporary_file_is_left_beside_the_config(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    assert _run(_set(p8)).exit_code == 0

    assert sorted(q.name for q in temp_config_env.parent.iterdir() if q.name.startswith(".")) == []


def test_the_settings_echo_carries_the_new_key(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    result = _run(_set(p8))

    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["app_store_connect"]["key_id"] == KEY_ID
    assert settings["app_store_connect"]["key_file_exists"] is True


def _hand_set(config: Path, **section) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["app_store_connect"] = section
    config.write_text(yaml.safe_dump(raw))


def test_a_new_key_keeps_a_hand_set_profile_cache_dir(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """`--set-portal` replaced the whole section: a hand-set
    `profile_cache_dir` vanished. Only the key, and the devices it sees, are
    the setting's to replace."""
    _hand_set(
        temp_config_env, key_id="OLDKEY0000", issuer_id=ISSUER,
        key_file="~/old.p8", device_udids=["00008030-00000000000000CC"],
        profile_cache_dir="~/profiles",
    )

    result = _run(_set(p8))

    assert result.exit_code == 0, result.output
    assert _section(temp_config_env) == {
        "key_id": KEY_ID,
        "issuer_id": ISSUER,
        "key_file": str(temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"),
        "device_udids": ["00008030-000000000000000A"],
        "profile_cache_dir": "~/profiles",
    }


def test_a_re_run_picks_up_a_newly_registered_device(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """The list is the one `--set-portal` wrote itself. Kept whenever it was
    non-empty, a second run never saw the phone registered since the first."""
    assert _run(_set(p8)).exit_code == 0
    assert _section(temp_config_env)["device_udids"] == ["00008030-000000000000000A"]
    asc.devices.append(_device("00008030-000000000000000C"))

    assert _run(_set(p8)).exit_code == 0

    assert _section(temp_config_env)["device_udids"] == [
        "00008030-000000000000000A", "00008030-000000000000000C",
    ]


def test_a_key_from_another_team_replaces_the_old_teams_devices(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """The old team's UDIDs are not this key's to provision for."""
    _hand_set(
        temp_config_env, key_id="OLDKEY0000", issuer_id="11111111-1111-1111-1111-111111111111",
        key_file="~/old.p8", device_udids=["00008030-0000000000000OLD"],
    )

    assert _run(_set(p8)).exit_code == 0

    section = _section(temp_config_env)
    assert section["issuer_id"] == ISSUER
    assert section["device_udids"] == ["00008030-000000000000000A"]


@pytest.mark.parametrize("udids", [["00008030-00000000000000CC", "00008030-00000000000000DD"], []])
def test_a_pinned_device_list_survives(
    temp_config_env: Path, asc: _Recorder, p8: Path, udids: list[str]
) -> None:
    """`device_udids_pinned: true` is how a hand-curated list says so; it and
    the list are left exactly as they are, even by a key from another team."""
    _hand_set(
        temp_config_env, key_id="OLDKEY0000", issuer_id="11111111-1111-1111-1111-111111111111",
        key_file="~/old.p8", device_udids=udids, device_udids_pinned=True,
    )

    assert _run(_set(p8)).exit_code == 0

    section = _section(temp_config_env)
    assert section["key_id"] == KEY_ID
    assert section["device_udids"] == udids
    assert section["device_udids_pinned"] is True


def test_a_pin_that_is_not_true_does_not_pin(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _hand_set(
        temp_config_env, key_id="OLDKEY0000", issuer_id=ISSUER, key_file="~/old.p8",
        device_udids=["00008030-00000000000000CC"], device_udids_pinned=False,
    )

    assert _run(_set(p8)).exit_code == 0

    assert _section(temp_config_env)["device_udids"] == ["00008030-000000000000000A"]


def test_the_wizard_refreshes_the_list_too(
    temp_config_env: Path, asc: _Recorder, p8: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hand_set(temp_config_env, key_id="OLDKEY0000", issuer_id=ISSUER,
              key_file="~/old.p8", device_udids=["00008030-00000000000000CC"])
    answers = iter([KEY_ID, ISSUER, str(p8)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))

    result = _run(["--setup-portal"])

    assert result.exit_code == 0, result.output
    assert _section(temp_config_env)["device_udids"] == ["00008030-000000000000000A"]


def test_the_loader_reads_the_pin(temp_config_env: Path) -> None:
    from graft_ipa.config import load_config

    _hand_set(temp_config_env, key_id=KEY_ID, issuer_id=ISSUER, key_file="~/k.p8",
              device_udids=["U1"], device_udids_pinned=True)
    assert load_config(temp_config_env).app_store_connect.device_udids_pinned is True

    _hand_set(temp_config_env, key_id=KEY_ID, issuer_id=ISSUER, key_file="~/k.p8",
              device_udids=["U1"])
    assert load_config(temp_config_env).app_store_connect.device_udids_pinned is False


@pytest.mark.parametrize("devices", [None, []])
def test_an_empty_device_list_is_still_filled_in(
    temp_config_env: Path, asc: _Recorder, p8: Path, devices
) -> None:
    """Nothing curated to keep: the list comes from App Store Connect, as it
    does on a first set."""
    _hand_set(temp_config_env, key_id="OLDKEY0000", issuer_id=ISSUER,
              key_file="~/old.p8", device_udids=devices)

    assert _run(_set(p8)).exit_code == 0
    assert _section(temp_config_env)["device_udids"] == ["00008030-000000000000000A"]
    assert "profile_cache_dir" not in _section(temp_config_env)


def test_the_flags_belong_to_set_portal(temp_config_env: Path, no_asc: None) -> None:
    result = _run(["--json", "--list", "--asc-key-id", KEY_ID])
    assert result.exit_code == 0, "above the gate --list ignores verb-only flags, as ever"

    result = _run(["--json", "--set-settings", "--archive-keep", "3", "--asc-key-id", KEY_ID])
    assert result.exit_code == 2
    assert "--asc-key-id is not valid with --set-settings" in " ".join(result.stderr.split())


# --- --clear-portal -----------------------------------------------------------


def test_clear_portal_removes_the_section_and_keeps_the_file(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _run(_set(p8))
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"

    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    assert _section(temp_config_env) is None
    assert dest.exists(), "the key is the user's; removing the setting keeps the file"
    (event,) = [e for e in _events(result) if e["type"] == "portal-cleared"]
    assert event == {"type": "portal-cleared", "removed": True, "key_file": str(dest)}
    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["app_store_connect"] is None


def test_clearing_nothing_is_not_an_error(temp_config_env: Path) -> None:
    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    (event,) = [e for e in _events(result) if e["type"] == "portal-cleared"]
    assert event == {"type": "portal-cleared", "removed": False, "key_file": None}


def test_clearing_warns_about_apps_that_still_provision_automatically(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _run(_set(p8))
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["example"]["auto_profile"] = True
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))

    result = _run(["--json", "--clear-portal"])

    warnings = [e["line"] for e in _events(result) if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "example" in warnings[0], warnings



def _auto_profile_apps(cfg_path: Path, **deliveries: str | None) -> None:
    """Set `auto_profile: true` on each named app, adding any not there yet;
    a None delivery leaves the key out, so the account's default applies."""
    raw = yaml.safe_load(cfg_path.read_text())
    for name, delivery in deliveries.items():
        entry = raw["apps"].setdefault(name, {"bundle_id": f"com.example.{name}"})
        entry["auto_profile"] = True
        if delivery is None:
            entry.pop("delivery", None)
        else:
            entry["delivery"] = delivery
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _warning_lines(result) -> list[str]:
    return [e["line"] for e in _events(result) if e["type"] == "log" and e["source"] == "warning"]


def test_clearing_does_not_warn_about_a_livecontainer_app(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """A LiveContainer build never signs, so the key was never its to lose."""
    _run(_set(p8))
    _auto_profile_apps(temp_config_env, example="livecontainer")

    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    assert _warning_lines(result) == []


def test_clearing_names_only_the_apps_that_sign(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """`bravo` has no `delivery:`, which on a paid config means sideload."""
    _run(_set(p8))
    _auto_profile_apps(temp_config_env, example="sideload", bravo=None, other="livecontainer")

    result = _run(["--json", "--clear-portal"])

    (warning,) = _warning_lines(result)
    assert warning.startswith("bravo, example still create"), warning
    assert "other" not in warning, warning



def test_on_a_free_config_an_app_with_no_delivery_is_livecontainer(temp_config_env: Path) -> None:
    """The default follows the account, as the loader's does: a key left over
    from a paid config names nothing on a free one."""
    remove_signing_section(temp_config_env)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["app_store_connect"] = {"key_id": KEY_ID, "issuer_id": "I", "key_file": "/k.p8"}
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    _auto_profile_apps(temp_config_env, example=None)

    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    (event,) = [e for e in _events(result) if e["type"] == "portal-cleared"]
    assert event["removed"] is True
    assert _warning_lines(result) == []


def test_clearing_a_config_with_signing_keys_at_the_top_level_still_succeeds(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """The section was removed, then working out which apps still sign read the
    account, which refuses signing keys stranded outside `signing:` — so a
    clear that had already landed exited 2. It is reported as done, with the
    reason it could not name those apps as a warning."""
    assert _run(_set(p8)).exit_code == 0
    raw = yaml.safe_load(temp_config_env.read_text())
    raw.update(raw.pop("signing"))
    raw["signing"] = None
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))

    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    assert _section(temp_config_env) is None
    (event,) = [e for e in _events(result) if e["type"] == "portal-cleared"]
    assert event["removed"] is True
    (warning,) = _warning_lines(result)
    assert "at the top level of config.yaml, but belong under signing:" in warning, warning
    assert "couldn't tell which apps" in warning, warning


# --- --settings reports it -----------------------------------------------------


def test_settings_reports_no_key_as_null(temp_config_env: Path) -> None:
    result = _run(["--json", "--settings"])

    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert "app_store_connect" in settings
    assert settings["app_store_connect"] is None


def test_settings_reports_the_key_but_never_its_contents(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _run(_set(p8))
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"

    result = _run(["--json", "--settings"])

    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["app_store_connect"] == {
        "key_id": KEY_ID, "issuer_id": ISSUER, "key_file": str(dest), "key_file_exists": True,
    }
    assert "PRIVATE KEY" not in result.stdout

    dest.unlink()
    result = _run(["--json", "--settings"])
    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["app_store_connect"]["key_file_exists"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a 0o000 directory anyway")
def test_a_key_file_in_a_directory_that_cannot_be_searched_is_reported_missing(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`Path.is_file()` raises PermissionError there on Python 3.11-3.13, and
    `--settings` caught only ConfigError: the Settings pane got a traceback
    instead of its settings. The key is as good as missing; say so."""
    locked = tmp_path / "locked"
    locked.mkdir()
    _hand_set(temp_config_env, key_id=KEY_ID, issuer_id=ISSUER,
              key_file=str(locked / f"AuthKey_{KEY_ID}.p8"))
    locked.chmod(0o000)
    try:
        result = _run(["--json", "--settings"])
    finally:
        locked.chmod(0o700)

    assert result.exit_code == 0, result.output
    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["app_store_connect"]["key_file_exists"] is False
    assert settings["app_store_connect"]["key_id"] == KEY_ID


@pytest.mark.parametrize("text", [
    "apps: [unclosed\n",
    "apps: [unclosed\napp_store_connect: {key_id: KEY1234567}\n",
    "- not\n- a mapping\n",
])
def test_an_unparseable_config_leaves_the_key_out(temp_config_env: Path, text: str) -> None:
    """Not `null`: the app reads a present null as "no key set", which a config
    it cannot read does not say. Absent is "this CLI doesn't report it" — the
    section hides, and the parse-error banner explains."""
    temp_config_env.write_text(text)

    result = _run(["--json", "--settings"])

    assert result.exit_code == 0, result.output
    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["parse_error"]
    assert "app_store_connect" not in settings


# --- the CLI's version rides on --settings -------------------------------------


def test_settings_carries_the_cli_version(temp_config_env: Path) -> None:
    """The Settings pane shows "graft <version>" from the read it already
    makes, rather than spawning `--version` for it."""
    from graft_ipa import __version__

    result = _run(["--json", "--settings"])

    (settings,) = [e for e in _events(result) if e["type"] == "settings"]
    assert settings["graft_version"] == __version__


def test_every_settings_echo_carries_it_too(temp_config_env: Path, asc: _Recorder, p8: Path) -> None:
    from graft_ipa import __version__

    for args in (_set(p8), ["--json", "--clear-portal"],
                 ["--json", "--set-settings", "--archive-keep", "3"]):
        result = _run(args)
        (settings,) = [e for e in _events(result) if e["type"] == "settings"]
        assert settings["graft_version"] == __version__, args


def test_an_unparseable_config_still_reports_the_version(temp_config_env: Path) -> None:
    from graft_ipa import __version__

    temp_config_env.write_text("apps: [unclosed\n")

    (settings,) = [e for e in _events(_run(["--json", "--settings"])) if e["type"] == "settings"]
    assert settings["graft_version"] == __version__


# --- which devices the section records (review #3) ----------------------------

MY_PHONE = "00008110-0000000000000FF0"
OLD_PHONES = [f"00008030-00000000000000{n}" for n in ("01", "02", "03")]


def _of_class(udid: str, device_class: str, status: str = "ENABLED") -> apple_portal.Device:
    return apple_portal.Device(
        id=udid, udid=udid, name="device", platform="IOS", status=status,
        device_class=device_class,
    )


def test_every_enabled_iphone_and_ipad_is_recorded_not_the_first_three(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, p8: Path
) -> None:
    """Three older phones still enabled, listed first, and the user's own
    phone (`default_device_udid`) last: keeping the first three wrote a list
    without it, and every auto-provisioned profile left the phone out."""
    from graft_ipa.config import load_config
    from graft_ipa.signer import auto_profile_device_udids

    raw = yaml.safe_load(temp_config_env.read_text())
    raw["default_device_udid"] = MY_PHONE
    temp_config_env.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", _Recorder(devices=[
        *(_of_class(u, "IPHONE") for u in OLD_PHONES),
        _of_class("00008027-00000000000000AA", "IPAD"),
        _of_class("00008006-00000000000000BB", "APPLE_WATCH"),
        _of_class("00008030-00000000000000CC", "IPHONE", status="DISABLED"),
        _of_class(MY_PHONE, "IPHONE"),
    ]))

    result = _run(_set(p8))

    assert result.exit_code == 0, result.output
    recorded = _section(temp_config_env)["device_udids"]
    assert recorded == sorted([*OLD_PHONES, "00008027-00000000000000AA", MY_PHONE])
    assert MY_PHONE in auto_profile_device_udids(load_config(temp_config_env))


def test_the_list_does_not_depend_on_the_order_app_store_connect_returns(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, p8: Path
) -> None:
    devices = [_of_class(u, "IPHONE") for u in (MY_PHONE, *OLD_PHONES)]
    lists = []
    for order in (devices, devices[::-1]):
        monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", _Recorder(devices=order))
        assert _run(_set(p8)).exit_code == 0
        lists.append(_section(temp_config_env)["device_udids"])

    assert lists[0] == lists[1]


def test_a_device_whose_class_app_store_connect_omits_is_kept() -> None:
    """Dropping the user's phone is the failure; an unknown class is not
    reason enough to risk it."""
    assert setup_portal._enabled_udids([_of_class(MY_PHONE, "")]) == [MY_PHONE]


def test_the_device_class_is_read_from_app_store_connect() -> None:
    device = apple_portal._device_from_resource({
        "id": "D1",
        "attributes": {
            "udid": MY_PHONE, "name": "phone", "platform": "IOS",
            "status": "ENABLED", "deviceClass": "IPHONE",
        },
    })
    assert device.device_class == "IPHONE"


# --- a writer beside --settings (review #9) -----------------------------------


def test_set_portal_beside_settings_still_checks_and_writes(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    """--settings came first in _VERBS, so this reported and exited 0 having
    checked and written nothing."""
    result = _run(["--settings", *_set(p8)])

    assert result.exit_code == 0, result.output
    assert any(e["type"] == "portal-set" for e in _events(result))
    assert _section(temp_config_env)["key_id"] == KEY_ID


def test_a_missing_key_file_beside_settings_is_still_refused(
    temp_config_env: Path, no_asc: None, tmp_path: Path
) -> None:
    result = _run(["--settings", *_set(tmp_path / "nonexistent.p8")])

    assert result.exit_code == 2, result.output
    assert "no .p8 file" in result.stderr


def test_clear_portal_beside_settings_still_clears(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    _run(_set(p8))

    result = _run(["--json", "--settings", "--clear-portal"])

    assert result.exit_code == 0, result.output
    assert _section(temp_config_env) is None
    assert any(e["type"] == "portal-cleared" for e in _events(result))


# --- a key file graft may not look at (review #12) ----------------------------


@pytest.fixture
def locked_p8(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    key = locked / f"AuthKey_{KEY_ID}.p8"
    key.write_text("not a real key")
    locked.chmod(0o000)
    yield key
    locked.chmod(0o700)


def test_a_key_file_in_a_folder_graft_cannot_search_is_a_config_error(
    temp_config_env: Path, no_asc: None, locked_p8: Path
) -> None:
    """Python 3.11-3.13's `is_file` raises PermissionError there, which came
    out as exit 1 and "isn't a failure graft knows how to explain"."""
    if os.geteuid() == 0:
        pytest.skip("root can search any folder")

    result = _run(_set(locked_p8))

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "isn't a failure graft knows how to explain" not in said
    assert "PermissionError" not in said
    assert _section(temp_config_env) is None


def test_the_permission_error_itself_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch that matters on 3.11-3.13, forced on any interpreter."""
    def deny(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", deny)

    with pytest.raises(setup_portal.ConfigError, match="can't look at") as raised:
        setup_portal._is_key_file(Path("/locked/AuthKey_KEY1234567.p8"))
    assert "Permission denied" in raised.value.message


# --- key copies nothing names (review #23) ------------------------------------


def _warnings(result) -> list[str]:
    return [e["line"] for e in _events(result) if e["type"] == "log" and e["source"] == "warning"]


def test_a_config_write_that_fails_takes_the_key_copy_back_out(
    temp_config_env: Path, asc: _Recorder, p8: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a): another graft held the config lock past its wait. The key was
    installed first and stayed, with config.yaml naming nothing."""
    from graft_ipa.errors import ConfigError

    def locked(*a, **k):
        raise ConfigError("another graft process is editing the config")

    monkeypatch.setattr(setup_portal, "set_app_store_connect", locked)

    result = _run(_set(p8))

    assert result.exit_code == 2, result.output
    assert not (temp_config_env.parent / f"AuthKey_{KEY_ID}.p8").exists()
    assert _section(temp_config_env) is None


def test_a_failed_write_leaves_a_copy_that_was_already_there(
    temp_config_env: Path, asc: _Recorder, p8: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.errors import ConfigError

    assert _run(_set(p8)).exit_code == 0
    dest = temp_config_env.parent / f"AuthKey_{KEY_ID}.p8"

    def locked(*a, **k):
        raise ConfigError("another graft process is editing the config")

    monkeypatch.setattr(setup_portal, "set_app_store_connect", locked)
    _run(_set(p8))

    assert dest.exists(), "the same key set again must not lose the copy the config names"


def _second_key(tmp_path: Path) -> tuple[str, Path]:
    other = "BBBBBBBBBB"
    path = tmp_path / "Downloads" / f"AuthKey_{other}.p8"
    path.write_text("-----BEGIN PRIVATE KEY-----\nanother\n-----END PRIVATE KEY-----\n")
    return other, path


def test_a_new_key_names_the_earlier_copy_it_leaves(
    temp_config_env: Path, asc: _Recorder, p8: Path, tmp_path: Path
) -> None:
    """(b): setting key B left AuthKey_A.p8 beside the config unmentioned."""
    assert _run(_set(p8)).exit_code == 0
    other, path = _second_key(tmp_path)

    result = _run(["--json", "--set-portal", "--asc-key-id", other, "--asc-issuer-id", ISSUER,
                   "--asc-key-file", str(path)])

    assert result.exit_code == 0, result.output
    (warning,) = _warnings(result)
    assert f"AuthKey_{KEY_ID}.p8" in warning and f"AuthKey_{other}.p8" not in warning
    assert (temp_config_env.parent / f"AuthKey_{KEY_ID}.p8").exists(), "named, never deleted"


def test_removing_the_key_names_every_other_copy_too(
    temp_config_env: Path, asc: _Recorder, p8: Path, tmp_path: Path
) -> None:
    assert _run(_set(p8)).exit_code == 0
    other, path = _second_key(tmp_path)
    assert _run(["--set-portal", "--asc-key-id", other, "--asc-issuer-id", ISSUER,
                 "--asc-key-file", str(path)]).exit_code == 0

    result = _run(["--json", "--clear-portal"])

    assert result.exit_code == 0, result.output
    assert any(f"AuthKey_{KEY_ID}.p8" in w for w in _warnings(result)), _warnings(result)
    (event,) = [e for e in _events(result) if e["type"] == "portal-cleared"]
    assert event["key_file"].endswith(f"AuthKey_{other}.p8")


def test_a_single_key_set_and_removed_warns_about_nothing_extra(
    temp_config_env: Path, asc: _Recorder, p8: Path
) -> None:
    assert not _warnings(_run(_set(p8)))
    assert not _warnings(_run(["--json", "--clear-portal"]))
