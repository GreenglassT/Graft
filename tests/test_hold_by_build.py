"""Holds by build (spec §6): `preferred_build` {version, version_id} beside
`preferred_version`, a get by id staged under its own name, and the one rule
(`ipa_cache.admits`) that picks the file a build uses."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config, set_app_options, set_app_tweaks, swap_preferred_version


def _entry(config: Path) -> dict:
    return yaml.safe_load(config.read_text())["apps"]["example"]


def _write(config: Path, **entry: object) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["apps"]["example"].update(entry)
    config.write_text(yaml.safe_dump(raw, sort_keys=False))


def _hold(config: Path) -> tuple[str, str | None]:
    app = load_config(config).get_app("example")
    return app.preferred_version, app.preferred_version_id


# --- config.yaml -----------------------------------------------------------------


def _build(version: object = "1.2.0", version_id: object = "800200") -> dict:
    return {"version": version, "version_id": version_id}


def test_an_old_config_has_no_id(temp_config_env: Path) -> None:
    assert _hold(temp_config_env) == ("latest", None)


def test_an_id_reads_quoted_or_not(temp_config_env: Path) -> None:
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=_build())
    assert "'800200'" in temp_config_env.read_text(), "premise: safe_dump quotes a digit string"
    assert _hold(temp_config_env) == ("1.2.0", "800200")

    temp_config_env.write_text(temp_config_env.read_text().replace("'800200'", "800200"))
    assert _hold(temp_config_env) == ("1.2.0", "800200"), "YAML reads it as an int"


@pytest.mark.parametrize("bad", ["abc", "-5", "1" * 21, "", True, 1.5, ["800200"], "１２３", None])
def test_an_id_that_isnt_one_is_no_id(temp_config_env: Path, bad: object) -> None:
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=_build(version_id=bad))
    assert _hold(temp_config_env) == ("1.2.0", None)


@pytest.mark.parametrize("bad", ["800200", 800200, ["1.2.0", "800200"], {"version_id": "800200"}, {}])
def test_a_build_that_isnt_a_version_and_its_id_is_no_build(temp_config_env: Path, bad: object) -> None:
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=bad)
    assert _hold(temp_config_env) == ("1.2.0", None)


def test_latest_has_no_id_whatever_the_file_says(temp_config_env: Path) -> None:
    _write(temp_config_env, preferred_version="latest", preferred_build=_build(version="latest"))
    assert _hold(temp_config_env) == ("latest", None)


def test_a_build_of_another_version_is_no_build(temp_config_env: Path) -> None:
    """A hand edit of `preferred_version`, or Edit App on 2.2.1 (which knows
    nothing of `preferred_build` and leaves it), moves the version and not the
    build. The id names a build of 1.2.0, so honoring it at 1.3.0 would hold
    the app to a file that can't exist, and the hint to get that build again
    would move the hold back to 1.2.0."""
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=_build())
    assert _hold(temp_config_env) == ("1.2.0", "800200"), "premise"

    _write(temp_config_env, preferred_version="1.3.0")

    assert _hold(temp_config_env) == ("1.3.0", None)


def test_a_version_spelled_as_yaml_reads_it_still_matches(temp_config_env: Path) -> None:
    """Unquoted, `1.2` is a float on both keys, and they still agree."""
    _write(temp_config_env, preferred_version="1.2", preferred_build=_build(version="1.2"))
    temp_config_env.write_text(temp_config_env.read_text().replace("'1.2'", "1.2"))
    assert "'1.2'" not in temp_config_env.read_text(), "premise: both unquoted"

    assert _hold(temp_config_env) == ("1.2", "800200")


def test_a_2_3_beta_s_bare_id_is_no_build(temp_config_env: Path) -> None:
    """Before `preferred_build`, a 2.3 beta wrote the id alone, with nothing
    to say which version it was the build of: read as a hold by version."""
    _write(temp_config_env, preferred_version="1.2.0", preferred_version_id="800200")
    assert _hold(temp_config_env) == ("1.2.0", None)


@pytest.mark.parametrize("stale", [
    {"preferred_version": "1.3.0", "preferred_build": _build()},
    {"preferred_version": "latest", "preferred_build": _build()},
    {"preferred_version": "1.2.0", "preferred_build": _build(version_id="abc")},
    {"preferred_version": "1.2.0", "preferred_version_id": "800200"},
])
def test_a_build_that_isnt_honored_is_cleared_on_the_next_write(
    temp_config_env: Path, stale: dict,
) -> None:
    _write(temp_config_env, **stale)

    set_app_tweaks(temp_config_env, "example", ["some.deb"])

    entry = _entry(temp_config_env)
    assert "preferred_build" not in entry and "preferred_version_id" not in entry
    assert entry["preferred_version"] == stale["preferred_version"], "the version is the person's"


def test_a_build_that_is_honored_survives_other_writes(temp_config_env: Path) -> None:
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=_build())

    set_app_tweaks(temp_config_env, "example", ["some.deb"])

    assert _hold(temp_config_env) == ("1.2.0", "800200")


def test_another_app_s_stale_build_is_cleared_too(temp_config_env: Path) -> None:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["other"] = {
        "bundle_id": "com.example.other", "preferred_version": "2.0", "preferred_build": _build(),
    }
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))

    set_app_tweaks(temp_config_env, "example", ["some.deb"])

    assert "preferred_build" not in yaml.safe_load(temp_config_env.read_text())["apps"]["other"]


def test_a_stale_build_is_no_hold_by_build_for_a_build(temp_config_env: Path) -> None:
    """What the stale id did: a strict hold on a file that can't exist, and
    a hint whose `--version-id` moved the hold back to 1.2.0."""
    _write(temp_config_env, preferred_version="1.2.0", preferred_build=_build())
    _write(temp_config_env, preferred_version="1.3.0")
    cfg = load_config(temp_config_env)
    app = cfg.get_app("example")

    with pytest.raises(IPANotFoundError) as caught:
        acquire_ipa(cfg, app)
    assert "--version-id 800200" not in caught.value.hint
    assert "version 1.3.0" in caught.value.message

    make_ipa(cfg.cache_dir / "com.example.app_1.3.0.ipa", version="1.3.0")
    found = find_in_cache(cfg, app)
    assert found is not None and found.path.name == "com.example.app_1.3.0.ipa"


def test_a_hold_by_build_is_swapped_in_and_out_together(temp_config_env: Path) -> None:
    found = swap_preferred_version(
        temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"),
    )
    assert found == ("latest", None)
    assert _hold(temp_config_env) == ("1.2.0", "800200")
    assert _entry(temp_config_env)["preferred_build"] == {"version": "1.2.0", "version_id": "800200"}

    found = swap_preferred_version(
        temp_config_env, "example", expected=("1.2.0", "800200"), new=("latest", None),
    )
    assert found == ("1.2.0", "800200")
    assert _entry(temp_config_env)["preferred_version"] == "latest"
    assert "preferred_build" not in _entry(temp_config_env)


def test_another_build_of_the_same_version_is_a_change(temp_config_env: Path) -> None:
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    found = swap_preferred_version(
        temp_config_env, "example", expected=("1.2.0", "800200"), new=("1.2.0", "800250"),
    )

    assert found == ("1.2.0", "800200")
    assert _hold(temp_config_env) == ("1.2.0", "800250")


def test_a_hold_changed_since_is_left_alone(temp_config_env: Path) -> None:
    set_app_options(temp_config_env, "example", preferred_version="1.3.0")

    found = swap_preferred_version(
        temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"),
    )

    assert found == ("1.3.0", None)
    assert _hold(temp_config_env) == ("1.3.0", None)


def test_a_hold_moved_to_the_other_build_since_is_left_alone(temp_config_env: Path) -> None:
    """Another get, of the other build of the same number, ran meanwhile: the
    version is what it was, the build isn't, and that later choice stays."""
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800250"))

    found = swap_preferred_version(
        temp_config_env, "example", expected=("1.2.0", "800200"), new=("latest", None),
    )

    assert found == ("1.2.0", "800250")
    assert _hold(temp_config_env) == ("1.2.0", "800250")


def test_a_swap_sees_a_stale_build_as_no_build(temp_config_env: Path) -> None:
    """`expected` is what `AppConfig` read, which ignores a stale build: the
    swap reads the file the same way, or every get after a hand edit would
    warn that the hold had changed while it ran."""
    _write(temp_config_env, preferred_version="1.3.0", preferred_build=_build())

    found = swap_preferred_version(
        temp_config_env, "example", expected=("1.3.0", None), new=("latest", None),
    )

    assert found == ("1.3.0", None)
    assert _entry(temp_config_env)["preferred_version"] == "latest"
    assert "preferred_build" not in _entry(temp_config_env)


def test_a_preferred_version_set_by_hand_lets_go_of_the_build(temp_config_env: Path) -> None:
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    set_app_options(temp_config_env, "example", preferred_version="1.2.0")

    assert _hold(temp_config_env) == ("1.2.0", None)
    assert "preferred_build" not in _entry(temp_config_env)


def test_editing_anything_else_keeps_the_build(temp_config_env: Path) -> None:
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    set_app_options(temp_config_env, "example", cyan={"display_name": "Edited"}, delivery="livecontainer")

    assert _hold(temp_config_env) == ("1.2.0", "800200")


def test_the_edit_sheet_s_command_lets_go_too(temp_config_env: Path) -> None:
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    result = CliRunner().invoke(cli_mod.app, [
        "--json", "--set-app-options", "--app", "example", "--preferred-version", "1.3.0",
    ])

    assert result.exit_code == 0, result.output
    assert _hold(temp_config_env) == ("1.3.0", None)


# --- The staged names --------------------------------------------------------------

from graft_ipa.ipa_cache import staged_name  # noqa: E402
from graft_ipa.provide_ipa import provide  # noqa: E402
from tests.appstore_fakes import make_ipa  # noqa: E402


def _marker(ipa: Path) -> str:
    with zipfile.ZipFile(ipa) as z:
        return z.read("Payload/App.app/marker.txt").decode()


def test_the_staged_names() -> None:
    assert staged_name("com.example.app", "1.2.0") == "com.example.app_1.2.0.ipa"
    assert staged_name("com.example.app", "1.2.0", "800200") == "com.example.app_1.2.0_800200.ipa"


@pytest.mark.parametrize("bad", ["", "abc", "../x", "１２", "8 0"])
def test_a_version_id_in_a_name_is_ascii_digits(bad: str) -> None:
    with pytest.raises(ValueError):
        staged_name("com.example.app", "1.2.0", bad)


def test_a_build_staged_by_id_sits_beside_the_plain_one(tmp_path: Path) -> None:
    one = make_ipa(tmp_path / "in" / "one.ipa", version="1.2.0", encrypted=False, marker="build 1")
    two = make_ipa(tmp_path / "in" / "two.ipa", version="1.2.0", encrypted=False, marker="build 2")
    cache = tmp_path / "cache"

    plain, _ = provide(cache_dir=cache, expected_bundle_id="com.example.app", source_path=two)
    by_id, _ = provide(
        cache_dir=cache, expected_bundle_id="com.example.app", source_path=one,
        external_version_id="800200",
    )

    assert plain.name == "com.example.app_1.2.0.ipa"
    assert by_id.name == "com.example.app_1.2.0_800200.ipa"
    assert (_marker(plain), _marker(by_id)) == ("build 2", "build 1")


# --- Which file a build uses -------------------------------------------------------

from graft_ipa.config import AppConfig, CyanOptions  # noqa: E402
from graft_ipa.errors import IPANotFoundError, StagedIPAInUseError  # noqa: E402
from graft_ipa.ipa_cache import acquire_ipa, admits, find_in_cache  # noqa: E402

BUNDLE = "com.example.app"


class _Cfg:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir


def _app(version: str = "latest", version_id: str | None = None) -> AppConfig:
    return AppConfig(
        name="example", bundle_id=BUNDLE, preferred_version=version, tweaks=[],
        cyan=CyanOptions(), preferred_version_id=version_id,
    )


def _two_builds_and_a_plain_one(cache: Path) -> None:
    make_ipa(cache / "com.example.app_1.2.0.ipa", version="1.2.0", marker="unknown build")
    make_ipa(cache / "com.example.app_1.2.0_800200.ipa", version="1.2.0", marker="build 1")
    make_ipa(cache / "com.example.app_1.2.0_800250.ipa", version="1.2.0", marker="build 2")


def test_a_hold_by_build_builds_that_build(tmp_path: Path) -> None:
    _two_builds_and_a_plain_one(tmp_path)

    one = find_in_cache(_Cfg(tmp_path), _app("1.2.0", "800200"))
    two = find_in_cache(_Cfg(tmp_path), _app("1.2.0", "800250"))

    assert one is not None and one.path.name == "com.example.app_1.2.0_800200.ipa"
    assert one.external_version_id == "800200"
    assert _marker(one.path) == "build 1"
    assert two is not None and _marker(two.path) == "build 2"


def test_a_hold_by_build_whose_file_is_gone_uses_no_other(tmp_path: Path) -> None:
    make_ipa(tmp_path / "com.example.app_1.2.0.ipa", version="1.2.0")
    make_ipa(tmp_path / "com.example.app_1.2.0_800250.ipa", version="1.2.0")

    assert find_in_cache(_Cfg(tmp_path), _app("1.2.0", "800200")) is None


def test_a_hold_by_build_reads_the_file_inside(tmp_path: Path) -> None:
    make_ipa(tmp_path / "com.example.app_1.2.0_800200.ipa", version="1.3.0")

    assert find_in_cache(_Cfg(tmp_path), _app("1.2.0", "800200")) is None


def test_a_hold_by_build_reads_the_bundle_id_inside(tmp_path: Path) -> None:
    make_ipa(tmp_path / "com.example.app_1.2.0_800200.ipa", bundle_id="com.example.other", version="1.2.0")

    assert find_in_cache(_Cfg(tmp_path), _app("1.2.0", "800200")) is None


def test_a_hold_without_an_id_works_as_before(tmp_path: Path) -> None:
    _two_builds_and_a_plain_one(tmp_path)

    found = find_in_cache(_Cfg(tmp_path), _app("1.2.0"))

    assert found is not None and found.path.name == "com.example.app_1.2.0.ipa"
    assert found.external_version_id is None


def test_a_hold_without_an_id_still_finds_a_file_staged_by_id(tmp_path: Path) -> None:
    """A hold set by hand after a get by id: the version is what counts."""
    make_ipa(tmp_path / "com.example.app_1.2.0_800200.ipa", version="1.2.0")

    found = find_in_cache(_Cfg(tmp_path), _app("1.2.0"))

    assert found is not None and found.path.name == "com.example.app_1.2.0_800200.ipa"


def test_latest_prefers_the_plain_file_on_a_tie(tmp_path: Path) -> None:
    _two_builds_and_a_plain_one(tmp_path)

    found = find_in_cache(_Cfg(tmp_path), _app())

    assert found is not None and found.path.name == "com.example.app_1.2.0.ipa"


def test_the_rule_itself() -> None:
    held = _app("1.2.0", "800200")
    plain = Path("com.example.app_1.2.0.ipa")
    own = Path("com.example.app_1.2.0_800200.ipa")
    assert admits(held, own, "1.2.0")
    assert not admits(held, plain, "1.2.0")
    assert not admits(held, own, "1.3.0")
    assert admits(_app("1.2.0"), plain, "1.2.0") and admits(_app("1.2.0"), own, "1.2.0")
    assert admits(_app(), plain, "9.9.9")


def test_the_missing_held_build_says_how_to_get_it_back(tmp_path: Path) -> None:
    with pytest.raises(IPANotFoundError) as caught:
        acquire_ipa(_Cfg(tmp_path), _app("1.2.0", "800200"))

    assert "800200" in caught.value.message
    assert "--appstore-get --app example --version-id 800200" in caught.value.hint
    assert "--provide-ipa" not in caught.value.hint, "a provided file isn't that build"
    # Edit App lets go only when its field changes, and it says 1.2.0
    # already: the command that holds by version is named outright.
    assert "--set-app-options --app example --preferred-version 1.2.0" in caught.value.hint


# --- --delete-staged asks first about the held build ------------------------------


def _held_twins(config: Path) -> Path:
    cache = load_config(config).cache_dir
    make_ipa(cache / "com.example.app_1.2.0.ipa", version="1.2.0")
    make_ipa(cache / "com.example.app_1.2.0_800200.ipa", version="1.2.0")
    swap_preferred_version(config, "example", expected=("latest", None), new=("1.2.0", "800200"))
    return cache


def test_deleting_the_held_build_asks_first_even_with_its_twin_staged(temp_config_env: Path) -> None:
    cache = _held_twins(temp_config_env)

    with pytest.raises(StagedIPAInUseError) as caught:
        cli_mod._do_delete_staged(
            load_config(temp_config_env), filename="com.example.app_1.2.0_800200.ipa", force=False,
        )

    assert (cache / "com.example.app_1.2.0_800200.ipa").is_file()
    # Not "the only staged IPA": its twin is staged too, and is no use to the hold.
    assert caught.value.message == (
        "com.example.app_1.2.0_800200.ipa is the only staged IPA that example can build from"
    )
    assert "example is held at this build" in caught.value.hint
    assert "--appstore-get --app example --version-id 800200" in caught.value.hint


def test_the_twin_goes_without_asking(temp_config_env: Path) -> None:
    cache = _held_twins(temp_config_env)

    cli_mod._do_delete_staged(
        load_config(temp_config_env), filename="com.example.app_1.2.0.ipa", force=False,
    )

    assert not (cache / "com.example.app_1.2.0.ipa").exists()


def test_a_file_the_held_build_never_uses_goes_without_asking(temp_config_env: Path) -> None:
    """The held build's own file is gone: the plain 1.2.0 was never one its
    builds could use (`admits`), so deleting it changes nothing for them."""
    cache = load_config(temp_config_env).cache_dir
    make_ipa(cache / "com.example.app_1.2.0.ipa", version="1.2.0")
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    cli_mod._do_delete_staged(
        load_config(temp_config_env), filename="com.example.app_1.2.0.ipa", force=False,
    )

    assert not (cache / "com.example.app_1.2.0.ipa").exists()


# --- The delivery digest names the held build -------------------------------------

from graft_ipa.build_log import BuildLog  # noqa: E402
from graft_ipa.delivery import compute_inputs_digest  # noqa: E402


def test_a_delivery_goes_stale_when_the_hold_moves_to_the_other_build(temp_config_env: Path) -> None:
    """Through the push's own record and the listing's own read: the row
    keeps the build it was built from, and moving the hold between two builds
    of one number is a change to deliver."""
    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    for name in cfg.get_app("example").tweaks:
        (cfg.tweaks_dir / name).write_bytes(b"payload-" + name.encode())
    make_ipa(cfg.cache_dir / "com.example.app_1.2.0_800200.ipa", version="1.2.0", marker="build 1")
    make_ipa(cfg.cache_dir / "com.example.app_1.2.0_800250.ipa", version="1.2.0", marker="build 2")
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))
    log = BuildLog(cfg.state_dir / "log.sqlite")
    cfg = load_config(temp_config_env)
    app = cfg.get_app("example")
    cli_mod._record_delivery_if_pushed(
        log, app_name="example", inputs=compute_inputs_digest(cfg, app, source_version="1.2.0"),
        ipa_path=None, lc_bundle_id=None, remote_name="example.ipa", udid="UDID1", dry_run=False,
    )
    assert log.latest_delivery("example")["held_version_id"] == "800200"
    assert cli_mod._delivery_state_for(cfg, "example", app, log)["kind"] == "current"

    swap_preferred_version(temp_config_env, "example", expected=("1.2.0", "800200"), new=("1.2.0", "800250"))
    cfg = load_config(temp_config_env)
    state = cli_mod._delivery_state_for(cfg, "example", cfg.get_app("example"), log)

    assert (state["kind"], state["reasons"]) == ("stale", ["the held build changed"])

    # And to a hold by version, from the plain file: no id now, the row's
    # still names one, so the reason comes from what the row stored.
    make_ipa(cfg.cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", marker="plain")
    set_app_options(temp_config_env, "example", preferred_version="1.2.0")
    cfg = load_config(temp_config_env)
    state = cli_mod._delivery_state_for(cfg, "example", cfg.get_app("example"), log)

    assert (state["kind"], state["reasons"]) == ("stale", ["the held build changed"])


def test_a_recorded_reinstall_keeps_the_build_it_shipped(temp_config_env: Path, tmp_path: Path) -> None:
    """A re-install of bytes a recorded push shipped copies that push's
    inputs, the held build among them, or it would read stale against the
    very hold it was built under."""
    from graft_ipa.build_log import sha256_of

    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    for name in cfg.get_app("example").tweaks:
        (cfg.tweaks_dir / name).write_bytes(b"payload-" + name.encode())
    make_ipa(cfg.cache_dir / "com.example.app_1.2.0_800200.ipa", version="1.2.0")
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))
    cfg = load_config(temp_config_env)
    app = cfg.get_app("example")
    log = BuildLog(cfg.state_dir / "log.sqlite")
    archive = tmp_path / "example.ipa"
    archive.write_bytes(b"the archived IPA")
    cli_mod._record_delivery_if_pushed(
        log, app_name="example", inputs=compute_inputs_digest(cfg, app, source_version="1.2.0"),
        ipa_path=archive, lc_bundle_id=None, remote_name="example.ipa", udid="UDID1", dry_run=False,
    )
    assert log.latest_delivery("example")["ipa_sha256"] == sha256_of(archive), "premise"

    cli_mod._record_reinstall_delivery(
        cfg, app_name="example", bundle_id=app.bundle_id, version="1.2.0", ipa_path=archive,
        delivery=app.delivery, lc_bundle_id=None, remote_name="example.ipa", udid="UDID1",
    )

    row = log.latest_delivery("example")
    assert (row["origin"], row["held_version_id"]) == ("reinstall-recorded", "800200")
    assert cli_mod._delivery_state_for(cfg, "example", app, log)["kind"] == "current"


# --- A provide or a dump of the held version -------------------------------------

import io  # noqa: E402
import json  # noqa: E402

from graft_ipa import decrypt_runner  # noqa: E402
from graft_ipa.json_output import JsonEmitter  # noqa: E402


def _events_of(text: str, kind: str | None = None) -> list[dict]:
    rows = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [e for e in rows if kind is None or e["type"] == kind]


def _warnings(events: list[dict]) -> list[str]:
    return [e["line"] for e in events if e["type"] == "log" and e.get("source") == "warning"]


def _held_by_build(config: Path) -> Path:
    """Held at build 800200 of 1.2.0, with that build's own file staged."""
    cache = load_config(config).cache_dir
    make_ipa(cache / "com.example.app_1.2.0_800200.ipa", version="1.2.0", encrypted=False, marker="build 1")
    swap_preferred_version(config, "example", expected=("latest", None), new=("1.2.0", "800200"))
    return cache


def _drop(tmp_path: Path, version: str = "1.2.0") -> Path:
    return make_ipa(tmp_path / "Downloads" / f"Example-{version}.ipa", version=version,
                    encrypted=False, marker="dropped")


def test_a_provide_of_the_held_version_holds_it_by_version(temp_config_env: Path, tmp_path: Path) -> None:
    """A drop of 1.2.0 stages the plain file, which a hold by build never
    uses: it was ignored without a word. The person gave Graft this IPA for
    that version, so the hold becomes one by version, and builds use it."""
    _held_by_build(temp_config_env)

    result = CliRunner().invoke(cli_mod.app, [
        "--json", "--provide-ipa", str(_drop(tmp_path)), "--app", "example",
    ])

    assert result.exit_code == 0, result.output
    events = _events_of(result.stdout)
    assert [e["type"] for e in events if e["type"] in ("ipa-provided", "appstore-hold")] == [
        "ipa-provided", "appstore-hold",
    ]
    assert _events_of(result.stdout, "appstore-hold") == [{
        "type": "appstore-hold", "app": "example", "bundle_id": "com.example.app",
        "version": "1.2.0", "version_id": None, "previous": "1.2.0",
    }]
    (warning,) = _warnings(events)
    assert "may not be the build of 1.2.0 it was held at (App Store version id 800200)" in warning
    assert "Graft now holds it at 1.2.0 by version" in warning
    assert _hold(temp_config_env) == ("1.2.0", None)
    assert "preferred_build" not in _entry(temp_config_env)
    cfg = load_config(temp_config_env)
    used = find_in_cache(cfg, cfg.get_app("example"))
    assert used is not None and _marker(used.path) == "dropped", "builds use the IPA given"


def test_a_provide_of_the_held_version_says_so_in_terminal(temp_config_env: Path, tmp_path: Path) -> None:
    _held_by_build(temp_config_env)

    result = CliRunner().invoke(cli_mod.app, ["--provide-ipa", str(_drop(tmp_path)), "--app", "example"])

    assert result.exit_code == 0, result.output
    assert "Graft now holds it at 1.2.0 by version" in " ".join(result.output.split())
    assert _hold(temp_config_env) == ("1.2.0", None)


@pytest.mark.parametrize(("hold", "version"), [
    (("1.2.0", "800200"), "1.3.0"),   # held by build, another version dropped
    (("1.2.0", None), "1.2.0"),       # held by version: the drop is used already
    (("latest", None), "1.2.0"),
])
def test_any_other_provide_leaves_the_hold_alone(
    temp_config_env: Path, tmp_path: Path, hold: tuple[str, str | None], version: str,
) -> None:
    make_ipa(load_config(temp_config_env).cache_dir / "com.example.app_1.2.0_800200.ipa", version="1.2.0")
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=hold)

    result = CliRunner().invoke(cli_mod.app, [
        "--json", "--provide-ipa", str(_drop(tmp_path, version)), "--app", "example",
    ])

    assert result.exit_code == 0, result.output
    assert _events_of(result.stdout, "appstore-hold") == []
    assert _warnings(_events_of(result.stdout)) == []
    assert _hold(temp_config_env) == hold


def test_a_hold_changed_during_the_provide_is_left_alone(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another get moved the hold to the other build while this IPA was
    being staged: that later choice stays, and nothing is said of it."""
    _held_by_build(temp_config_env)
    real = cli_mod._stage_ipa

    def stage(cfg, **kwargs):
        swap_preferred_version(
            temp_config_env, "example", expected=("1.2.0", "800200"), new=("1.2.0", "800250"),
        )
        return real(cfg, **kwargs)

    monkeypatch.setattr(cli_mod, "_stage_ipa", stage)

    result = CliRunner().invoke(cli_mod.app, [
        "--json", "--provide-ipa", str(_drop(tmp_path)), "--app", "example",
    ])

    assert result.exit_code == 0, result.output
    assert _events_of(result.stdout, "appstore-hold") == []
    assert _hold(temp_config_env) == ("1.2.0", "800250")


def test_a_dump_of_the_held_version_holds_it_by_version(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--decrypt` names a bundle id, not an app: every app held by build at
    the version it dumped lets go of the build."""
    _held_by_build(temp_config_env)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["other"] = {"bundle_id": "com.example.other", "preferred_version": "1.2.0",
                            "preferred_build": {"version": "1.2.0", "version_id": "900100"}}
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))

    def dump(bundle_id, out_dir, **_k):
        ipa = make_ipa(Path(out_dir) / "dump.ipa", bundle_id=bundle_id, version="1.2.0",
                       encrypted=False, marker="dumped")
        return decrypt_runner.DecryptResult(ipa_path=ipa, bundle_id=bundle_id, stdout="")

    monkeypatch.setattr(decrypt_runner, "decrypt_app", dump)
    monkeypatch.setattr(cli_mod, "verify_decrypted", lambda _d: [])
    out = io.StringIO()

    cli_mod._do_decrypt(
        load_config(temp_config_env), "com.example.app", device="U", backend=decrypt_runner.FRIDA,
        emitter=JsonEmitter(stream=out),
    )

    events = _events_of(out.getvalue())
    assert [(e["app"], e["version"], e["version_id"]) for e in _events_of(out.getvalue(), "appstore-hold")] == [
        ("example", "1.2.0", None),
    ]
    assert len(_warnings(events)) == 1
    assert _hold(temp_config_env) == ("1.2.0", None)
    other = load_config(temp_config_env).get_app("other")
    assert other.preferred_version_id == "900100", "another bundle id's hold is its own"


# --- The Cache pane's "BUILD USES THIS" (`--staged-ipas`' `preferred`) -----------


def _preferred(config: Path) -> list[str]:
    out = io.StringIO()
    cli_mod._do_staged_ipas(load_config(config), emitter=JsonEmitter(stream=out))
    return [e["filename"] for e in _events_of(out.getvalue(), "staged-ipa") if e["preferred"]]


def _four_staged(config: Path) -> None:
    cache = load_config(config).cache_dir
    make_ipa(cache / "com.example.app_1.2.0.ipa", version="1.2.0", marker="plain")
    make_ipa(cache / "com.example.app_1.2.0_800200.ipa", version="1.2.0", marker="build 1")
    make_ipa(cache / "com.example.app_1.2.0_800250.ipa", version="1.2.0", marker="build 2")
    make_ipa(cache / "com.example.app_1.3.0.ipa", version="1.3.0")


@pytest.mark.parametrize(("hold", "uses"), [
    (("1.2.0", "800200"), "com.example.app_1.2.0_800200.ipa"),
    (("1.2.0", "800250"), "com.example.app_1.2.0_800250.ipa"),
    (("1.2.0", None), "com.example.app_1.2.0.ipa"),
    (("latest", None), "com.example.app_1.3.0.ipa"),
])
def test_the_file_marked_is_the_one_the_app_s_build_uses(
    temp_config_env: Path, hold: tuple[str, str | None], uses: str,
) -> None:
    """It was `latest`'s rule whatever the app was held at, so a hold by
    build wore the chip on the plain file (the tie's winner), which that
    build never uses (`admits`)."""
    _four_staged(temp_config_env)
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=hold)

    assert _preferred(temp_config_env) == [uses]
    cfg = load_config(temp_config_env)
    assert find_in_cache(cfg, cfg.get_app("example")).path.name == uses, "the build's own answer"


def test_nothing_is_marked_when_the_held_build_is_gone(temp_config_env: Path) -> None:
    cache = load_config(temp_config_env).cache_dir
    make_ipa(cache / "com.example.app_1.2.0.ipa", version="1.2.0")
    make_ipa(cache / "com.example.app_1.3.0.ipa", version="1.3.0")
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    assert _preferred(temp_config_env) == [], "a build would stop: no file is the one it uses"


def test_apps_that_disagree_keep_the_newest_marked(temp_config_env: Path) -> None:
    """Two apps of one bundle id held differently have no one answer, so the
    mark stays where it was before holds counted: the newest."""
    _four_staged(temp_config_env)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["copy"] = {"bundle_id": "com.example.app", "preferred_version": "1.2.0"}
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))

    assert _preferred(temp_config_env) == ["com.example.app_1.3.0.ipa"]


# --- A build starts from the held build ------------------------------------------


def test_a_dry_run_build_starts_from_the_held_build(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the build itself (`_build_one`, stage acquire), with the plain
    1.2.0 and the other build staged beside it."""
    from types import SimpleNamespace

    from graft_ipa import tools
    from tests.tool_fakes import fake_path

    _four_staged(temp_config_env)
    swap_preferred_version(temp_config_env, "example", expected=("latest", None), new=("1.2.0", "800200"))
    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    for name in cfg.get_app("example").tweaks:
        (cfg.tweaks_dir / name).write_bytes(b"payload")
    monkeypatch.setattr(tools, "which", fake_path(temp_config_env.parent / "path-bin"))
    # Signing isn't what this is about, and the config's identity isn't on disk.
    monkeypatch.setattr(cli_mod, "run_all_checks", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_mod, "run_zsign", lambda *_a, **_k: SimpleNamespace(
        team_id="ABCDE12345", cert_subject="subject", expiry="2027-01-01", stdout="", stderr="",
        profile_note=None, profile_notes=[],
    ))
    started_from: list[Path] = []

    def cyan(_cfg, _app, src, dst, *, dry_run=False):
        started_from.append(Path(src))
        return SimpleNamespace(output_ipa=Path(dst), stdout="", stderr="")

    monkeypatch.setattr(cli_mod, "run_cyan", cyan)
    out = io.StringIO()

    cli_mod._build_one(
        cfg=cfg, app_name="example", dry_run=True, skip_install=True, skip_decrypt_check=True,
        verbose=False, device=None, emitter=JsonEmitter(stream=out),
    )

    (acquired,) = [e for e in _events_of(out.getvalue(), "stage")
                   if e["name"] == "acquire" and e["status"] == "end"]
    assert Path(acquired["path"]).name == "com.example.app_1.2.0_800200.ipa"
    assert acquired["version"] == "1.2.0"
    assert [p.name for p in started_from] == ["com.example.app_1.2.0_800200.ipa"]
