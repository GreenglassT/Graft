"""`graft --remove-data`: what Graft keeps outside Graft.app, and removing it.

This feature deletes things, so every test here runs where nothing real can
be reached: the suite's scratch HOME and XDG roots (conftest), a scratch temp
folder (`_no_real_temp_folder`), and a fake `security` seeded per test
(`keychain_fakes.FakeSecurity`). `test_every_path_is_under_the_scratch_folders`
holds the first of those to account for this module's own paths.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore_account, data_dirs, keychain, remove_data
from graft_ipa import cli as cli_mod
from graft_ipa.errors import ConfigError

from .keychain_fakes import FakeSecurity

TEAM = "ABCDE12345"
UDID = "00008110-000A1B2C3D4E5F60"
OLD = time.time() - 2 * remove_data.STALE_SECONDS


# --- Helpers -------------------------------------------------------------------------


@pytest.fixture
def security(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    fake = FakeSecurity()
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def _write(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _config_path() -> Path:
    return data_dirs.config_dir() / "config.yaml"


def _write_config(body: dict) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body))
    return path


def _survey() -> remove_data.Survey:
    return remove_data.survey(_config_path())


def _item(found: remove_data.Survey, item_id: str) -> remove_data.Item:
    item = found.item(item_id)
    assert item is not None, f"no {item_id} in {[i.id for i in found.items]}"
    return item


def _old(path: Path) -> Path:
    os.utime(path, (OLD, OLD))
    return path


def _invoke(*args: str):
    return CliRunner().invoke(cli_mod.app, list(args))


def _events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def _security_calls(fake: FakeSecurity, verb: str) -> list[list[str]]:
    return [c for c in fake.calls if c[1] == verb]


# --- The keychain helper ------------------------------------------------------------------


def test_first_account_reads_the_account_and_never_the_secret(security: FakeSecurity) -> None:
    security.add(keychain.SERVICE, f"p12-{TEAM}", "a-secret")

    look = keychain.first_account(keychain.SERVICE)

    assert look.exists is True and look.account == f"p12-{TEAM}" and look.created
    assert security.calls == [["security", "find-generic-password", "-s", keychain.SERVICE]]
    assert keychain.first_account("com.example.nothing").exists is False


def test_first_account_says_when_security_didnt_answer(security: FakeSecurity) -> None:
    security.hang.add(keychain.SERVICE)

    look = keychain.first_account(keychain.SERVICE)

    assert look.exists is None and look.no_answer and look.account is None


# --- The listing -----------------------------------------------------------------------


def test_every_item_is_listed_on_an_empty_home(security: FakeSecurity) -> None:
    found = _survey()

    ids = [i.id for i in found.items]
    assert ids == [
        "config", "cache", "state", "data", "legacy",
        "appstore-signin", "ipatool-cache", "unfairplay-model",
        "passwords", "graft-link", "app-settings", "update-log", "temp",
    ]
    assert not any(i.present for i in found.items)
    assert found.keychain == remove_data.KEYCHAIN_CHECKED
    assert all(i.group in remove_data.GROUP_TITLES for i in found.items)


def test_the_listing_creates_nothing(security: FakeSecurity, tmp_path: Path) -> None:
    """Unlike every other verb, no folder is made (`secure_app_dirs`): the
    listing would otherwise find the four folders it had just made."""
    before = sorted(p for p in tmp_path.rglob("*"))
    result = _invoke("--remove-data", "--json")

    assert result.exit_code == 0, result.output
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert not data_dirs.config_dir().exists()


def test_graft_folders_are_listed_with_their_sizes(security: FakeSecurity) -> None:
    _write(data_dirs.config_dir() / "config.yaml", 100)
    _write(data_dirs.cache_dir() / "ipas" / "com.example.app_1.0.ipa", 5000)
    _write(data_dirs.state_dir() / "log.sqlite", 300)
    _write(data_dirs.data_dir() / "frida-ios-dump" / "dump.py", 40)

    found = _survey()

    assert _item(found, "config").paths == [data_dirs.config_dir()]
    assert _item(found, "config").size_bytes >= 100
    assert _item(found, "cache").size_bytes == 5000
    assert _item(found, "state").size_bytes == 300
    assert _item(found, "data").size_bytes == 40
    for item_id in ("config", "cache", "state", "data"):
        item = _item(found, item_id)
        assert item.present and item.default and item.removable


def test_the_state_item_names_what_updates_and_playcover_remember(security: FakeSecurity) -> None:
    """Final review 3 #6: the refusal memory, the dropped devices and the
    PlayCover settings memory live in the state folder too
    (`update_refusals`, `update_drops`, `playcover_handoff`)."""
    detail = _item(_survey(), "state").detail

    assert detail.endswith(
        "the names of the iPhones and iPads Graft has seen, the PlayCover settings it keeps for "
        "your apps, which devices you stopped updating an app on, and which App Store versions "
        "your Apple ID was refused.")


def test_the_rename_links_go_with_their_folders(security: FakeSecurity) -> None:
    folder = _write(data_dirs.cache_dir() / "f", 1).parent
    link = data_dirs.xdg_cache_home() / data_dirs.LEGACY_DIRNAME
    link.symlink_to(data_dirs.APP_DIRNAME)

    assert _item(_survey(), "cache").paths == [folder, link]


def test_a_folder_that_is_the_users_link_is_kept(security: FakeSecurity, tmp_path: Path) -> None:
    theirs = tmp_path / "someone-else" / "graft-cache"
    _write(theirs / "a.ipa", 10)
    data_dirs.xdg_cache_home().mkdir(parents=True)
    data_dirs.cache_dir().symlink_to(theirs)

    found = _survey()

    assert not _item(found, "cache").present
    kept = _item(found, "kept-cache")
    assert kept.paths == [data_dirs.cache_dir()] and not kept.removable
    assert "link" in kept.title


def test_a_pre_rename_folder_beside_graft_is_its_own_item_and_off(security: FakeSecurity) -> None:
    _write(data_dirs.config_dir() / "config.yaml")
    old = _write(data_dirs.xdg_config_home() / data_dirs.LEGACY_DIRNAME / "config.yaml").parent

    found = _survey()

    legacy = _item(found, "legacy")
    assert legacy.paths == [old] and legacy.present and not legacy.default
    assert _item(found, "config").paths == [data_dirs.config_dir()]


def test_an_unmigrated_pre_rename_folder_is_the_folder_in_use(security: FakeSecurity) -> None:
    old = _write(data_dirs.xdg_state_home() / data_dirs.LEGACY_DIRNAME / "log.sqlite").parent

    found = _survey()

    assert _item(found, "state").paths == [old]
    assert not _item(found, "legacy").present


def test_the_app_store_sign_in_graft_made_is_on(security: FakeSecurity) -> None:
    service = appstore_account.IPATOOL_SERVICE
    made = {name: security.add(service, name) for name in ("account", "anisette-v4")}
    marker = appstore_account.marker_path()
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"format": 2, "bundled_items": made}))
    cookies = _write(Path.home() / ".ipatool" / "cookies")

    item = _item(_survey(), "appstore-signin")

    assert item.keychain == [(service, "account"), (service, "anisette-v4")]
    assert item.paths == [cookies.parent]
    assert item.present and item.default
    assert "Terminal" in (item.note or "")
    # Attributes only: never -g or -w, which read the secret.
    finds = _security_calls(security, "find-generic-password")
    assert finds and not any("-g" in c or "-w" in c for c in finds)


def test_another_ipatools_sign_in_is_listed_but_off(security: FakeSecurity) -> None:
    security.add(appstore_account.IPATOOL_SERVICE, "account")

    item = _item(_survey(), "appstore-signin")

    assert item.present and not item.default
    assert "another ipatool" in (item.note or "")


def test_ipatool_keeps_its_cookies_under_xdg_state_home_when_it_is_set(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    moved = _write(tmp_path / "state-home" / "ipatool" / "cookies").parent

    assert remove_data.ipatool_state_dirs() == [Path.home() / ".ipatool", moved]
    assert _item(_survey(), "appstore-signin").paths == [moved]


def test_ipatools_cache_and_only_its_stale_workspaces(security: FakeSecurity) -> None:
    cache = _write(remove_data.ipatool_cache_dir() / "unicorn" / "lib", 2000).parents[1]
    tmp = remove_data.temp_root()
    stale = tmp / "ipatool-macos-27-123"
    stale.mkdir()
    _old(stale)
    (tmp / "ipatool-macos-27-456").mkdir()  # in use: written just now

    item = _item(_survey(), "ipatool-cache")

    assert item.paths == [cache, stale]
    assert item.size_bytes == 2000


def test_the_model_in_unfairplays_own_folder(security: FakeSecurity) -> None:
    model = _write(remove_data.unfairplay_default_models() / "m.bin", 64)

    item = _item(_survey(), "unfairplay-model")

    assert item.paths == [model.parents[1]]
    assert item.paths[0].name == "unfairplay"
    assert item.size_bytes == 64


def test_a_model_folder_set_in_config_is_kept(security: FakeSecurity, tmp_path: Path) -> None:
    models = _write(tmp_path / "my-models" / "m.bin").parent
    _write_config({"appstore": {"unfairplay_models": str(models)}})

    found = _survey()

    assert not _item(found, "unfairplay-model").present
    kept = _item(found, "kept-unfairplay-model")
    assert kept.paths == [models] and not kept.removable


def test_passwords_are_found_by_service_and_account_and_are_off(security: FakeSecurity) -> None:
    security.add(keychain.SERVICE, f"p12-{TEAM}")
    security.add(keychain.SERVICE, f"ssh-{UDID}")
    security.add(keychain.LEGACY_SERVICE, f"p12-{TEAM}")
    _write_config({"signing": {"team_id": TEAM}, "default_device_udid": UDID})

    item = _item(_survey(), "passwords")

    assert set(item.keychain) == {
        (keychain.SERVICE, f"p12-{TEAM}"), (keychain.SERVICE, f"ssh-{UDID}"),
        (keychain.LEGACY_SERVICE, f"p12-{TEAM}"),
    }
    assert item.present and not item.default and item.removable
    finds = _security_calls(security, "find-generic-password")
    assert not any("-g" in c or "-w" in c for c in finds)


def test_another_programs_item_under_the_same_service_is_not_listed(
    security: FakeSecurity,
) -> None:
    security.add(keychain.SERVICE, "someone-else")

    item = _item(_survey(), "passwords")

    assert item.keychain == [] and item.present is False


def test_the_build_logs_devices_are_asked_about(security: FakeSecurity) -> None:
    import sqlite3

    db = data_dirs.state_dir() / "log.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE builds (signing_team TEXT, udid TEXT)")
        conn.execute("CREATE TABLE deliveries (udid TEXT)")
        conn.execute("INSERT INTO builds VALUES (?, ?)", (TEAM, None))
        conn.execute("INSERT INTO deliveries VALUES (?)", (UDID,))
    conn.close()
    security.add(keychain.SERVICE, "someone-else")  # first, so not found that way
    security.add(keychain.SERVICE, f"ssh-{UDID}")

    item = _item(_survey(), "passwords")

    assert item.keychain == [(keychain.SERVICE, f"ssh-{UDID}")]


def test_a_locked_keychain_is_asked_nothing(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    security.add(appstore_account.IPATOOL_SERVICE, "account")
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)

    found = _survey()

    assert security.calls == []
    assert found.keychain == remove_data.KEYCHAIN_LOCKED
    assert _item(found, "appstore-signin").present is None
    assert _item(found, "passwords").present is None


def test_the_first_unanswered_lookup_is_the_last(security: FakeSecurity) -> None:
    security.hang.add(appstore_account.IPATOOL_SERVICE)

    found = _survey()

    assert found.keychain == remove_data.KEYCHAIN_NO_ANSWER
    assert len(security.calls) == 1
    assert _item(found, "passwords").present is None


def test_the_graft_link_only_when_it_points_into_a_graft_app(security: FakeSecurity) -> None:
    link = remove_data.graft_link()
    link.parent.mkdir(parents=True)
    link.symlink_to("/Applications/Graft.app/Contents/Helpers/graft")

    found = _survey()

    assert _item(found, "graft-link").paths == [link]
    assert found.item("kept-graft-link") is None


@pytest.mark.parametrize("target", [
    "/Users/example/.local/pipx/venvs/graft-ipa/bin/graft",
    "/Applications/Graft.app/Contents/MacOS/Graft",
    "../share/graft",
])
def test_any_other_graft_in_local_bin_is_kept(security: FakeSecurity, target: str) -> None:
    link = remove_data.graft_link()
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    found = _survey()

    assert not _item(found, "graft-link").present
    assert not _item(found, "kept-graft-link").removable


def test_graft_apps_own_settings_are_the_apps_to_remove(security: FakeSecurity) -> None:
    _write(remove_data.preferences_plist(), 500)

    item = _item(_survey(), "app-settings")

    assert item.present and not item.removable and item.default
    assert "defaults delete com.greenglasst.graft" in (item.how or "")


def test_the_update_log_folder_is_on_and_goes_whole(security: FakeSecurity) -> None:
    """`~/Library/Logs/Graft`, where the self-update writes update.log (and
    a from-source install its rebuild log): on by default, so `--items all`
    takes it, and ~/Library/Logs stays."""
    log = _write(remove_data.logs_dir() / "update.log", 300)
    _write(remove_data.logs_dir() / "rebuild.log", 200)

    found = _survey()
    item = _item(found, "update-log")

    assert (item.group, item.title) == (remove_data.GROUP_APP, "Update log")
    assert item.paths == [log.parent] and item.present and item.default and item.removable
    assert item.size_bytes == 500
    assert "update-log" in [i.id for i in remove_data.choose(found, "all")]

    [outcome] = remove_data.remove(remove_data.choose(found, "update-log"), dry_run=False)

    assert outcome.ok and outcome.freed_bytes == 500
    assert not log.parent.exists()
    assert log.parent.parent.is_dir(), "~/Library/Logs stays"


def test_the_update_log_folder_is_where_the_updater_writes(security: FakeSecurity) -> None:
    from graft_ipa import app_update

    assert app_update.log_path().parent == remove_data.logs_dir()


def test_a_logs_folder_that_is_a_link_is_not_listed(security: FakeSecurity, tmp_path: Path) -> None:
    """Never removed through a link, nor the folder it points to."""
    theirs = _write(tmp_path / "someone-else" / "logs" / "update.log").parent
    remove_data.logs_dir().parent.mkdir(parents=True)
    remove_data.logs_dir().symlink_to(theirs)

    item = _item(_survey(), "update-log")

    assert not item.present and item.paths == []
    assert theirs.is_dir()


def test_only_grafts_own_stale_temporary_folders(security: FakeSecurity) -> None:
    tmp = remove_data.temp_root()
    ours = []
    for name in ("graft-example-ab12cd_3", "graft-zsign-0a1b2c3d", "lcsign-zz99zz99",
                 "lcinstall-a1b2c3d4"):
        (tmp / name).mkdir()
        ours.append(_old(tmp / name))
    _old(_write(tmp / "graft-enroll-cancel.0123456789abcdef", 0))
    ours.append(tmp / "graft-enroll-cancel.0123456789abcdef")
    for name in ("graft-swift-test.lock", "graft-contract-1B2C3D4E-AAAA-BBBB-CCCC-0123456789AB",
                 "graftless-ab12cd34", "other-ab12cd34"):
        (tmp / name).mkdir()
        _old(tmp / name)
    (tmp / "graft-example-fresh000").mkdir()  # a run still going
    marker = _write(remove_data.rebuild_marker())

    item = _item(_survey(), "temp")

    assert sorted(item.paths) == sorted([*ours, marker])


def test_folders_config_yaml_chose_outside_grafts_are_kept(
    security: FakeSecurity, tmp_path: Path
) -> None:
    outside = _write(tmp_path / "Documents" / "IPAs" / "a.ipa").parent
    inside = data_dirs.cache_dir() / "ipas"
    inside.mkdir(parents=True)
    _write_config({"cache_dir": str(outside), "tweaks_dir": str(inside.parent / "tweaks"),
                   "built_dir": str(inside)})

    found = _survey()

    kept = [i for i in found.items if i.group == remove_data.GROUP_KEPT]
    assert [(i.id, i.paths) for i in kept] == [("kept-cache_dir", [outside])]
    assert not kept[0].removable and not kept[0].default


def test_a_broken_config_still_lists_everything_else(security: FakeSecurity) -> None:
    path = _config_path()
    _write(path).write_text("this: is: not: yaml:\n")

    found = remove_data.survey(path)

    assert _item(found, "config").present


def test_every_path_is_under_the_scratch_folders(security: FakeSecurity, tmp_path: Path) -> None:
    """What every other test here relies on: nothing this module names can be
    the developer's own, under the suite's fixtures."""
    real_home = Path(os.path.expanduser(f"~{os.environ.get('USER', '')}"))
    paths = [
        remove_data.ipatool_cache_dir(), *remove_data.ipatool_state_dirs(),
        remove_data.unfairplay_default_models(), remove_data.graft_link(),
        remove_data.rebuild_marker(), remove_data.preferences_plist(), remove_data.logs_dir(),
        remove_data.temp_root(), data_dirs.config_dir(), data_dirs.cache_dir(),
        data_dirs.state_dir(), data_dirs.data_dir(),
    ]
    for path in paths:
        assert not path.resolve().is_relative_to(real_home.resolve()), path
    assert remove_data.temp_root().resolve() != Path(os.environ.get("TMPDIR", "/tmp")).resolve()


# --- Choosing ----------------------------------------------------------------------------


def _with_everything(security: FakeSecurity) -> None:
    _write(data_dirs.config_dir() / "config.yaml")
    _write(data_dirs.cache_dir() / "a.ipa")
    _write(remove_data.ipatool_cache_dir() / "x")
    security.add(keychain.SERVICE, f"p12-{TEAM}")
    _write(remove_data.preferences_plist())


def test_all_is_every_default_item_that_is_there(security: FakeSecurity) -> None:
    _with_everything(security)
    found = _survey()

    assert [i.id for i in remove_data.choose(found, "all")] == ["config", "cache", "ipatool-cache"]
    assert [i.id for i in remove_data.choose(found, "all,passwords")] == [
        "config", "cache", "ipatool-cache", "passwords",
    ]
    assert [i.id for i in remove_data.choose(found, " cache , passwords ")] == ["cache", "passwords"]


@pytest.mark.parametrize("spec, says", [
    ("bogus", "no item called 'bogus'"),
    ("app-settings", "isn't something --remove-data removes"),
    (",", "needs at least one item"),
])
def test_what_cant_be_chosen_is_refused_before_anything_goes(
    security: FakeSecurity, spec: str, says: str
) -> None:
    _with_everything(security)
    with pytest.raises(ConfigError, match=says):
        remove_data.choose(_survey(), spec)


# --- Removing ----------------------------------------------------------------------------


def test_removing_a_folder_never_follows_a_link_out_of_it(
    security: FakeSecurity, tmp_path: Path
) -> None:
    precious = _write(tmp_path / "precious" / "keep.txt")
    cache = data_dirs.cache_dir()
    _write(cache / "ipas" / "a.ipa", 100)
    (cache / "outside").symlink_to(precious.parent)
    (cache / "outside-file").symlink_to(precious)
    sealed = cache / "sealed"
    _write(sealed / "inner" / "f")
    os.chmod(sealed / "inner", 0o500)
    os.chmod(sealed, 0o500)

    outcomes = remove_data.remove(remove_data.choose(_survey(), "cache"), dry_run=False)

    assert outcomes[0].ok, outcomes[0].failed
    assert outcomes[0].removed == [str(cache)]
    assert outcomes[0].freed_bytes == 110
    assert not os.path.lexists(cache)
    assert precious.read_bytes() == b"x" * 10


def test_removing_the_config_takes_the_rename_link_too(security: FakeSecurity) -> None:
    _write(data_dirs.config_dir() / "config.yaml")
    link = data_dirs.xdg_config_home() / data_dirs.LEGACY_DIRNAME
    link.symlink_to(data_dirs.APP_DIRNAME)

    [outcome] = remove_data.remove(remove_data.choose(_survey(), "config"), dry_run=False)

    assert outcome.ok and not os.path.lexists(link) and not data_dirs.config_dir().exists()
    assert data_dirs.xdg_config_home().is_dir(), "the XDG root itself stays"


def test_a_dry_run_touches_nothing(security: FakeSecurity) -> None:
    _with_everything(security)
    found = _survey()

    outcomes = remove_data.remove(remove_data.choose(found, "all,passwords"), dry_run=True)

    assert all(o.ok and o.removed for o in outcomes)
    assert data_dirs.config_dir().exists() and remove_data.ipatool_cache_dir().exists()
    assert (keychain.SERVICE, f"p12-{TEAM}") in security.items
    assert _security_calls(security, "delete-generic-password") == []


def test_removing_the_sign_in_deletes_each_item_by_name_and_service(
    security: FakeSecurity,
) -> None:
    service = appstore_account.IPATOOL_SERVICE
    for name in appstore_account.IPATOOL_ITEMS:
        security.add(service, name)
    security.add("unrelated.service", "account")
    marker = _write(appstore_account.marker_path())
    cookies = _write(Path.home() / ".ipatool" / "cookies").parent

    [outcome] = remove_data.remove(
        remove_data.choose(_survey(), "appstore-signin"), dry_run=False
    )

    assert outcome.ok
    deletes = _security_calls(security, "delete-generic-password")
    assert deletes == [
        ["security", "delete-generic-password", "-s", service, "-a", name]
        for name in appstore_account.IPATOOL_ITEMS
    ]
    assert list(security.items) == [("unrelated.service", "account")]
    assert not marker.exists() and not cookies.exists()


def test_a_locked_keychain_stops_the_sign_in_before_anything_goes(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    security.add(appstore_account.IPATOOL_SERVICE, "account")
    cookies = _write(Path.home() / ".ipatool" / "cookies").parent
    found = _survey()
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)

    [outcome] = remove_data.remove(remove_data.choose(found, "appstore-signin"), dry_run=False)

    assert not outcome.ok and "locked" in outcome.failed[0][1]
    assert cookies.exists()
    assert _security_calls(security, "delete-generic-password") == []


def test_removing_passwords_takes_grafts_and_leaves_anyone_elses(security: FakeSecurity) -> None:
    security.add(keychain.SERVICE, f"p12-{TEAM}")
    security.add(keychain.SERVICE, "ssh-UNKNOWNDEVICE01")  # no record of it anywhere
    security.add(keychain.LEGACY_SERVICE, f"p12-{TEAM}")
    security.add(keychain.SERVICE, "someone-else")
    security.add("ipatool-auth.service", "account")

    [outcome] = remove_data.remove(remove_data.choose(_survey(), "passwords"), dry_run=False)

    assert outcome.ok, outcome.failed
    assert sorted(outcome.removed) == sorted([
        f"graft p12-{TEAM}", "graft ssh-UNKNOWNDEVICE01", f"tweak-build p12-{TEAM}",
    ])
    assert sorted(security.items) == [
        ("graft", "someone-else"), ("ipatool-auth.service", "account"),
    ]
    for call in _security_calls(security, "delete-generic-password"):
        assert call[2] == "-s" and call[4] == "-a" and len(call) == 6


def test_a_password_the_keychain_wont_delete_is_reported_once(security: FakeSecurity) -> None:
    security.add(keychain.SERVICE, f"p12-{TEAM}")
    security.delete_status[(keychain.SERVICE, f"p12-{TEAM}")] = 51

    [outcome] = remove_data.remove(remove_data.choose(_survey(), "passwords"), dry_run=False)

    assert outcome.failed == [(f"graft p12-{TEAM}", remove_data.NO_ANSWER_REASON)]


def test_removing_leftovers_takes_the_marker_folder_only_when_empty(
    security: FakeSecurity,
) -> None:
    marker = _write(remove_data.rebuild_marker())

    [outcome] = remove_data.remove(remove_data.choose(_survey(), "temp"), dry_run=False)

    assert outcome.ok and not marker.parent.exists()
    assert marker.parent.parent.is_dir(), "~/Library/Caches stays"


@pytest.mark.parametrize("path", [
    Path("/"), Path("/Users"), Path("relative/graft"),
])
def test_the_guard_refuses_a_root_by_itself(security: FakeSecurity, path: Path) -> None:
    """Asked of `_guard` alone, which raises and removes nothing: a real
    root never goes near `remove_path` in this suite, so a guard that
    regresses fails this test instead of removing it."""
    with pytest.raises(remove_data.RefusedPath):
        remove_data._guard(path)


@pytest.mark.parametrize("where", ["home", "xdg", "tmp", "library", "logs"])
def test_no_root_is_ever_removed(security: FakeSecurity, where: str) -> None:
    """End to end, but only ever on the suite's scratch folders (asserted
    first), so even a broken guard costs nothing but this test."""
    path = {
        "home": Path.home(), "xdg": data_dirs.xdg_cache_home(),
        "tmp": remove_data.temp_root(), "library": Path.home() / "Library",
        "logs": Path.home() / "Library" / "Logs",
    }[where]
    real_home = Path(os.path.expanduser(f"~{os.environ.get('USER', '')}")).resolve()
    assert not path.resolve().is_relative_to(real_home), path
    assert "pytest-of-" in str(path.resolve()), path
    path.mkdir(parents=True, exist_ok=True)
    _write(path / "keep.txt")
    with pytest.raises(remove_data.RefusedPath):
        remove_data.remove_path(path)
    assert (path / "keep.txt").exists()


def test_an_xdg_root_set_to_the_home_folder_is_never_removed(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(Path.home().parent))
    home = Path.home()
    assert "pytest-of-" in str(home.resolve()), "only ever the suite's scratch HOME"
    _write(home / "keep.txt")

    outcomes = remove_data.remove([remove_data.Item(
        id="cache", group=remove_data.GROUP_FILES, title="t", detail="d", paths=[home],
    )], dry_run=False)

    assert not outcomes[0].ok and (home / "keep.txt").exists()


# --- The verb ------------------------------------------------------------------------------


def test_the_json_listing(security: FakeSecurity) -> None:
    _with_everything(security)

    result = _invoke("--json", "--remove-data")

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    items = [e for e in events if e["type"] == "data-item"]
    assert [e["id"] for e in items][:4] == ["config", "cache", "state", "data"]
    end = events[-1]
    assert end["type"] == "data-items-end" and end["count"] == len(items)
    assert end["keychain"] == "checked"
    config = next(e for e in items if e["id"] == "config")
    assert set(config) >= {
        "type", "id", "group", "group_title", "title", "detail", "note", "how", "paths",
        "keychain", "size_bytes", "present", "default", "removable",
    }
    passwords = next(e for e in items if e["id"] == "passwords")
    assert passwords["keychain"] == [{"service": "graft", "account": f"p12-{TEAM}"}]
    assert passwords["default"] is False and passwords["size_bytes"] is None


def test_the_terminal_listing_removes_nothing_and_says_how(security: FakeSecurity) -> None:
    _with_everything(security)

    result = _invoke("--remove-data")

    assert result.exit_code == 0, result.output
    assert "Nothing was removed" in result.output
    assert "--items all,passwords" in result.output
    assert data_dirs.config_dir().exists()


def test_the_verb_removes_what_items_names(security: FakeSecurity) -> None:
    _with_everything(security)

    result = _invoke("--json", "--remove-data", "--items", "cache,ipatool-cache")

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert [e["type"] for e in events] == ["data-removed", "data-removed", "data-removal-end"]
    assert events[-1]["removed"] == ["cache", "ipatool-cache"] and events[-1]["ok"]
    assert not data_dirs.cache_dir().exists() and not remove_data.ipatool_cache_dir().exists()
    assert data_dirs.config_dir().exists()
    assert (keychain.SERVICE, f"p12-{TEAM}") in security.items


def test_the_verb_with_dry_run_removes_nothing(security: FakeSecurity) -> None:
    _with_everything(security)

    result = _invoke("--json", "--remove-data", "--items", "all", "--dry-run")

    assert result.exit_code == 0, result.output
    end = _events(result.stdout)[-1]
    assert end["dry_run"] is True and end["removed"] == ["config", "cache", "ipatool-cache"]
    assert data_dirs.cache_dir().exists() and data_dirs.config_dir().exists()


def test_an_unknown_item_is_a_usage_error_and_removes_nothing(security: FakeSecurity) -> None:
    _with_everything(security)

    result = _invoke("--remove-data", "--items", "cache,bogus")

    assert result.exit_code == 2
    assert data_dirs.cache_dir().exists()


def test_a_failure_is_exit_1_after_its_events(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_everything(security)
    monkeypatch.setattr(remove_data, "remove_path", lambda path: "Operation not permitted")

    result = _invoke("--json", "--remove-data", "--items", "cache")

    assert result.exit_code == 1
    removed = _events(result.stdout)[0]
    assert removed["ok"] is False
    assert removed["failed"] == [{"what": str(data_dirs.cache_dir()), "reason": "Operation not permitted"}]


def test_items_without_the_verb_is_a_stray_flag(temp_config_env: Path) -> None:
    result = _invoke("--list", "--items", "cache")

    assert result.exit_code == 0  # above the gate, --list ignores it, as it ignores --app
    result = _invoke("--builds", "--items", "cache")
    assert result.exit_code == 2 and "--items" in result.output
