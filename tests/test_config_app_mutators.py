from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa.config import (
    ConfigError,
    load_config,
    remove_app,
    set_app_options,
)


def test_remove_app_drops_only_the_named_entry(temp_config_env: Path) -> None:
    from graft_ipa.config import add_app

    # The default config ships only `example`; add a sibling so the test can
    # prove removal is surgical rather than vacuously true.
    add_app(temp_config_env, "delta", {"bundle_id": "com.delta.app"})
    before = sorted(load_config(temp_config_env).apps)
    assert before == ["delta", "example"]

    remove_app(temp_config_env, "example")

    after = sorted(load_config(temp_config_env).apps)
    assert after == ["delta"]


def test_remove_app_unknown_name_raises(temp_config_env: Path) -> None:
    before = temp_config_env.read_text()
    with pytest.raises(ConfigError, match="not found"):
        remove_app(temp_config_env, "nosuchapp")
    assert temp_config_env.read_text() == before


def test_set_app_options_patches_one_field_only(temp_config_env: Path) -> None:
    original = load_config(temp_config_env).get_app("example")

    set_app_options(temp_config_env, "example", preferred_version="21.16.2")

    updated = load_config(temp_config_env).get_app("example")
    assert updated.preferred_version == "21.16.2"
    # Everything else survives untouched.
    assert updated.tweaks == original.tweaks
    assert updated.bundle_id == original.bundle_id
    assert updated.delivery == original.delivery
    assert updated.cyan.display_name == original.cyan.display_name
    assert updated.cyan.compression_level == original.cyan.compression_level


def test_set_app_options_merges_cyan_keys_rather_than_replacing(
    temp_config_env: Path,
) -> None:
    original = load_config(temp_config_env).get_app("example")
    assert original.cyan.keep_bundle_id is True

    set_app_options(temp_config_env, "example", cyan={"compression_level": 9})

    updated = load_config(temp_config_env).get_app("example")
    assert updated.cyan.compression_level == 9
    assert updated.cyan.keep_bundle_id is True, "unnamed cyan keys must survive"
    assert updated.cyan.display_name == original.cyan.display_name


def test_set_app_options_creates_cyan_block_when_absent(
    temp_config_env: Path,
) -> None:
    from graft_ipa.config import add_app

    # add_app with a bare {"bundle_id": ...} lands with no `cyan:` key at all —
    # the realistic shape for an app the GUI's "add app" flow just created,
    # which the edit sheet will then patch. entry.get("cyan") is None here,
    # exercising the `merged = {}` branch rather than merging into an
    # existing mapping.
    add_app(temp_config_env, "delta", {"bundle_id": "com.delta.app"})

    set_app_options(temp_config_env, "delta", cyan={"compression_level": 9})

    updated = load_config(temp_config_env).get_app("delta")
    assert updated.cyan.compression_level == 9
    # Defaults for everything else in the freshly-created cyan block.
    assert updated.cyan.keep_bundle_id is True


def test_set_app_options_sets_a_real_display_name(temp_config_env: Path) -> None:
    set_app_options(temp_config_env, "example", cyan={"display_name": "Example Deluxe"})

    updated = load_config(temp_config_env).get_app("example")
    assert updated.cyan.display_name == "Example Deluxe"


def test_set_app_options_creates_display_name_when_absent(
    temp_config_env: Path,
) -> None:
    from graft_ipa.config import add_app

    add_app(temp_config_env, "delta", {"bundle_id": "com.delta.app"})
    assert load_config(temp_config_env).get_app("delta").cyan.display_name is None

    set_app_options(temp_config_env, "delta", cyan={"display_name": "Delta+"})

    updated = load_config(temp_config_env).get_app("delta")
    assert updated.cyan.display_name == "Delta+"


@pytest.mark.parametrize("empty_value", ["", "   "])
def test_set_app_options_empty_display_name_deletes_the_key(
    temp_config_env: Path, empty_value: str
) -> None:
    # Start from a config with a real display name so removal is provable.
    original = load_config(temp_config_env).get_app("example")
    assert original.cyan.display_name

    set_app_options(temp_config_env, "example", cyan={"display_name": empty_value})

    raw = yaml.safe_load(temp_config_env.read_text())
    cyan_raw = raw["apps"]["example"]["cyan"]
    assert "display_name" not in cyan_raw, (
        "an emptied display_name must be absent from the raw YAML, not just "
        "falsy — the GUI's `app.displayName ?? app.name` coalesce only falls "
        "back to the app's name when the key is missing, not when it's ''"
    )
    # And the typed loader agrees: falls back to None (AppConfig has no name
    # fallback itself — that coalesce lives GUI-side).
    updated = load_config(temp_config_env).get_app("example")
    assert updated.cyan.display_name is None


def test_set_app_options_empty_display_name_on_app_with_no_cyan_block(
    temp_config_env: Path,
) -> None:
    from graft_ipa.config import add_app

    # No `cyan:` key at all — the realistic shape right after --add-app-manual
    # with no display name. Clearing an already-absent display_name must not
    # raise and must leave the app cyan-block-having-no-display_name.
    add_app(temp_config_env, "delta", {"bundle_id": "com.delta.app"})

    set_app_options(temp_config_env, "delta", cyan={"display_name": ""})

    updated = load_config(temp_config_env).get_app("delta")
    assert updated.cyan.display_name is None
    raw = yaml.safe_load(temp_config_env.read_text())
    cyan_raw = raw["apps"]["delta"].get("cyan") or {}
    assert "display_name" not in cyan_raw


def test_set_app_options_can_change_delivery(temp_config_env: Path) -> None:
    set_app_options(temp_config_env, "example", delivery="livecontainer")
    assert load_config(temp_config_env).get_app("example").delivery == "livecontainer"


def test_set_app_options_rejects_an_invalid_delivery(temp_config_env: Path) -> None:
    before = temp_config_env.read_text()
    with pytest.raises(ConfigError, match="invalid delivery"):
        set_app_options(temp_config_env, "example", delivery="carrier-pigeon")
    assert temp_config_env.read_text() == before, "must fail before writing"


def test_set_app_options_unknown_app_raises(temp_config_env: Path) -> None:
    before = temp_config_env.read_text()
    with pytest.raises(ConfigError, match="not found"):
        set_app_options(temp_config_env, "nosuchapp", preferred_version="1.0")
    assert temp_config_env.read_text() == before, "must fail before writing"


@pytest.mark.parametrize("bad_version", ["", "   "])
def test_set_app_options_rejects_empty_preferred_version(
    temp_config_env: Path, bad_version: str
) -> None:
    before = temp_config_env.read_text()
    with pytest.raises(ConfigError, match="preferred_version can't be empty"):
        set_app_options(temp_config_env, "example", preferred_version=bad_version)
    assert temp_config_env.read_text() == before, "must fail before writing"


def test_set_app_options_with_nothing_to_change_is_a_noop(
    temp_config_env: Path,
) -> None:
    before = temp_config_env.read_text()
    set_app_options(temp_config_env, "example")
    assert temp_config_env.read_text() == before


def test_set_app_options_noop_does_not_rewrite_the_file_at_all(
    temp_config_env: Path,
) -> None:
    """A no-op must not touch the file, not merely produce equal bytes.

    Byte equality alone would still pass if a no-op re-serialized a config that
    happens to already be in safe_dump's canonical form — and then a
    hand-written config with comments WOULD be silently reformatted, because
    _atomic_write_yaml round-trips through yaml.safe_dump. st_ino is the sharp
    check: the write commits via os.replace() onto a fresh temp file, so any
    write at all necessarily changes the inode.

    This is the property that makes the mutator envelope in config.py a
    _load_apps/_commit_apps pair instead of a context manager that writes on
    __exit__; it exists to fail if anyone converts it to the latter.
    """
    # Comments and non-canonical formatting so a stray rewrite is visible.
    temp_config_env.write_text(
        "# signing setup — hand-written, keep the comments\n"
        "signing:\n"
        "  cert: /tmp/dev.p12\n"
        "  cert_password_env: TWEAK_BUILD_P12_PASS\n"
        "  profile: /tmp/wildcard.mobileprovision\n"
        "  team_id: TESTTEAM01   # from the portal\n"
        "apps:\n"
        "  example:\n"
        "    bundle_id: com.example.app   # keep this exact\n"
        "    cyan: {display_name: 'Example+', compression_level: 6}\n"
    )
    before_bytes = temp_config_env.read_bytes()
    before_stat = temp_config_env.stat()

    set_app_options(temp_config_env, "example")
    set_app_options(temp_config_env, "example", cyan={})

    after_stat = temp_config_env.stat()
    assert temp_config_env.read_bytes() == before_bytes
    assert after_stat.st_ino == before_stat.st_ino, "a no-op replaced the file"
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns, "a no-op touched the file"
    assert "# keep this exact" in temp_config_env.read_text()

    # Control: a real change does move both, so the assertions above have teeth.
    set_app_options(temp_config_env, "example", preferred_version="21.16.2")
    changed_stat = temp_config_env.stat()
    assert changed_stat.st_ino != before_stat.st_ino
    assert temp_config_env.read_bytes() != before_bytes


def test_set_app_tweaks_unknown_app_lists_the_available_apps(
    temp_config_env: Path,
) -> None:
    """Parity with remove_app / set_app_options, which have always had the hint.

    All three raise the same "not found" error from the same helper now; this
    pins that set_app_tweaks didn't get left behind again.
    """
    from graft_ipa.config import set_app_tweaks

    with pytest.raises(ConfigError) as excinfo:
        set_app_tweaks(temp_config_env, "nosuchapp", ["foo.deb"])
    assert excinfo.value.hint == "available apps: example"


def test_set_app_options_empty_cyan_is_a_noop_even_for_unknown_app(
    temp_config_env: Path,
) -> None:
    # Documents a deliberate consequence of the zero-I/O no-op check running
    # before the app-existence check: a caller passing cyan={} (nothing to
    # merge) gets a silent success, even for a name that doesn't exist. This
    # pins that behavior as a decision on record, not an accident of `not cyan`.
    before = temp_config_env.read_text()
    set_app_options(temp_config_env, "nosuchapp", cyan={})
    assert temp_config_env.read_text() == before
