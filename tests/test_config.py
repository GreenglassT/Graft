from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa.config import load_config
from graft_ipa.errors import ConfigError

from .config_fixtures import write_test_config


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "signing": {
                    "cert": str(tmp_path / "cert.p12"),
                    "cert_password_env": "FOO",
                    "profile": str(tmp_path / "profile.mobileprovision"),
                    "team_id": "ABC1234567",
                },
                "cache_dir": str(tmp_path / "cache"),
                "tweaks_dir": str(tmp_path / "tweaks"),
                "apps": {
                    "example": {
                        "bundle_id": "com.example.app",
                        "tweaks": ["a.deb", "b.dylib"],
                        "cyan": {"display_name": "Example+", "keep_bundle_id": True},
                    }
                },
            }
        )
    )
    cfg = load_config(cfg_path)
    assert cfg.signing.team_id == "ABC1234567"
    app = cfg.get_app("example")
    assert app.bundle_id == "com.example.app"
    assert app.tweaks == ["a.deb", "b.dylib"]
    assert app.cyan.keep_bundle_id is True
    assert cfg.tweak_path("a.deb") == (tmp_path / "tweaks" / "a.deb")


def test_missing_signing(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"apps": {}}))
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_write_and_reload(tmp_path: Path) -> None:
    from graft_ipa.config import write_default_config

    cfg_path = tmp_path / "config.yaml"
    write_default_config(cfg_path, team_id="XYZ9876543")
    cfg = load_config(cfg_path)
    assert cfg.signing.team_id == "XYZ9876543"
    # The starter configures no apps; `test_starter_config.py` pins the rest.
    assert cfg.apps == {}


def test_unknown_app_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path)
    cfg = load_config(cfg_path)
    with pytest.raises(ConfigError):
        cfg.get_app("nope")


def test_load_config_reads_repos(tmp_path) -> None:
    import yaml

    from graft_ipa.config import load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())
    raw["repos"] = [{"url": "https://apt.example.com", "name": "Example"}]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    cfg = load_config(cfg_path)
    assert len(cfg.repos) == 1
    assert cfg.repos[0].url == "https://apt.example.com"
    assert cfg.repos[0].name == "Example"


def test_load_config_repos_defaults_to_empty(tmp_path) -> None:
    from graft_ipa.config import load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    assert load_config(cfg_path).repos == []


def test_load_config_rejects_non_mapping_repo_entry(tmp_path) -> None:
    import yaml

    from graft_ipa.config import ConfigError, load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())
    raw["repos"] = [123]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_load_config_rejects_repo_entry_without_url(tmp_path) -> None:
    import yaml

    from graft_ipa.config import ConfigError, load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())
    raw["repos"] = [{"name": "No URL"}]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    with pytest.raises(ConfigError):
        load_config(cfg_path)


def _with_archive_keep(tmp_path: Path, value) -> Path:
    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())
    raw["archive_keep"] = value
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return cfg_path


def test_archive_keep_defaults_when_absent(tmp_path: Path) -> None:
    from graft_ipa.config import DEFAULT_ARCHIVE_KEEP

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    assert load_config(cfg_path).archive_keep == DEFAULT_ARCHIVE_KEEP


@pytest.mark.parametrize("value", [0, 1, 20])
def test_archive_keep_reads_a_non_negative_integer(tmp_path: Path, value: int) -> None:
    assert load_config(_with_archive_keep(tmp_path, value)).archive_keep == value


@pytest.mark.parametrize("value", ["five", 2.5, -1, True, [5]])
def test_archive_keep_rejects_anything_it_cant_act_on(tmp_path: Path, value) -> None:
    """This one setting drives a destructive sweep.

    Falling back to the default on `archive_keep: "ten"` would trash five
    builds the user meant to keep, so an unreadable value has to be loud.
    `True` is in the list because bool is an int subclass and "keep 1" is not a
    plausible reading of `archive_keep: true`.
    """
    with pytest.raises(ConfigError, match="archive_keep"):
        load_config(_with_archive_keep(tmp_path, value))


def test_add_repo_appends_and_is_idempotent(tmp_path) -> None:
    from graft_ipa.config import add_repo, load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")

    add_repo(cfg_path, "https://apt.example.com", "Example")
    add_repo(cfg_path, "https://apt.example.com", "Example")  # duplicate — ignored

    repos = load_config(cfg_path).repos
    assert len(repos) == 1
    assert repos[0].url == "https://apt.example.com"
    assert repos[0].name == "Example"
    # Existing config content survives the rewrite.
    assert load_config(cfg_path).signing.team_id == "T1"


def test_remove_repo_drops_matching_url(tmp_path) -> None:
    from graft_ipa.config import add_repo, load_config, remove_repo

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    add_repo(cfg_path, "https://a.example.com", "A")
    add_repo(cfg_path, "https://b.example.com", "B")

    remove_repo(cfg_path, "https://a.example.com")

    repos = load_config(cfg_path).repos
    assert [r.url for r in repos] == ["https://b.example.com"]


def test_add_repo_missing_file_raises_configerror(tmp_path) -> None:
    from graft_ipa.config import ConfigError, add_repo

    with pytest.raises(ConfigError):
        add_repo(tmp_path / "does-not-exist.yaml", "https://x.example.com", "X")


def test_slugify_name_lowercases_and_replaces_punctuation() -> None:
    from graft_ipa.config import slugify_name

    assert slugify_name("Bravo") == "bravo"
    assert slugify_name("Example Music") == "example_music"
    assert slugify_name("My App!! v2") == "my_app_v2"
    assert slugify_name("  ") == "app"  # empty after strip → fallback
    assert slugify_name("---") == "app"


def test_add_app_writes_entry_and_round_trips(tmp_path) -> None:
    from graft_ipa.config import add_app, load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    add_app(
        cfg_path,
        name="bravo",
        app_config={
            "bundle_id": "com.bravo.app",
            "preferred_version": "latest",
            "delivery": "sideload",
            "tweaks": [],
            "cyan": {
                "display_name": "Bravo",
                "keep_bundle_id": True,
                "strip_extensions": False,
                "strip_supported_devices": True,
                "compression_level": 6,
            },
        },
    )
    cfg = load_config(cfg_path)
    bravo = cfg.get_app("bravo")
    assert bravo.bundle_id == "com.bravo.app"
    assert bravo.delivery == "sideload"
    assert bravo.cyan.display_name == "Bravo"
    # Existing default config content survives the rewrite.
    assert cfg.signing.team_id == "T1"


def test_add_app_rejects_duplicate_name(tmp_path) -> None:
    from graft_ipa.config import ConfigError, add_app

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    # `example` already exists in the default config.
    with pytest.raises(ConfigError):
        add_app(
            cfg_path,
            name="example",
            app_config={"bundle_id": "com.example.dup"},
        )


def test_add_app_with_dedup_returns_suffixed_name(tmp_path) -> None:
    """`add_app(dedup=True)` performs the read+collision-check+write atomically,
    closing the TOCTOU window the previous in-memory _unique_app_name had."""
    from graft_ipa.config import add_app, load_config

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    actual = add_app(
        cfg_path,
        name="example",  # collides with the seeded entry
        app_config={
            "bundle_id": "com.alt.example",
            "preferred_version": "latest",
            "delivery": "sideload",
            "tweaks": [],
            "cyan": {"display_name": "Alt Example"},
        },
        dedup=True,
    )
    assert actual == "example_2"
    cfg = load_config(cfg_path)
    assert cfg.get_app("example").bundle_id == "com.example.app"
    assert cfg.get_app("example_2").bundle_id == "com.alt.example"
    # Adding another collider should bump to _3.
    third = add_app(
        cfg_path,
        name="example",
        app_config={
            "bundle_id": "com.alt2.example",
            "preferred_version": "latest",
            "delivery": "sideload",
            "tweaks": [],
            "cyan": {"display_name": "Alt2"},
        },
        dedup=True,
    )
    assert third == "example_3"


def test_set_app_tweaks_overwrites_list(tmp_path) -> None:
    from graft_ipa.config import load_config, set_app_tweaks

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    # The seed config has `example` with a populated tweaks list.
    set_app_tweaks(cfg_path, "example", ["foo.deb", "bar.deb"])
    cfg = load_config(cfg_path)
    assert cfg.get_app("example").tweaks == ["foo.deb", "bar.deb"]
    # Unrelated config survives the rewrite.
    assert cfg.signing.team_id == "T1"


def test_set_app_tweaks_can_clear_to_empty_list(tmp_path) -> None:
    from graft_ipa.config import load_config, set_app_tweaks

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    set_app_tweaks(cfg_path, "example", [])
    assert load_config(cfg_path).get_app("example").tweaks == []


def test_set_app_tweaks_unknown_app_raises(tmp_path) -> None:
    from graft_ipa.config import ConfigError, set_app_tweaks

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    with pytest.raises(ConfigError):
        set_app_tweaks(cfg_path, "not-a-real-app", ["foo.deb"])


# --- atomic rewrites -------------------------------------------------------
#
# config.yaml carries the signing/cert block plus every app and repo, it has no
# backup, and (since the GUI) a single Save or Remove click rewrites it. These
# pin that a rewrite is all-or-nothing: an interrupted write must leave the
# previous file byte-for-byte intact, never truncated.


def _mutators():
    """The six app/repo mutators, as (label, callable(path)) pairs."""
    from graft_ipa.config import (
        add_app,
        add_repo,
        remove_app,
        remove_repo,
        set_app_options,
        set_app_tweaks,
    )

    return [
        ("add_app", lambda p: add_app(p, "delta", {"bundle_id": "com.delta.app"})),
        ("set_app_tweaks", lambda p: set_app_tweaks(p, "example", ["only.deb"])),
        ("add_repo", lambda p: add_repo(p, "https://repo.example/", "Example")),
        ("remove_repo", lambda p: remove_repo(p, "https://repo.example/")),
        ("remove_app", lambda p: remove_app(p, "example")),
        ("set_app_options", lambda p: set_app_options(p, "example", preferred_version="9.9.9")),
    ]


def test_interrupted_rewrite_never_truncates_the_config(tmp_path, monkeypatch) -> None:
    """No writer may rewrite config.yaml in place.

    Models the old `path.write_text(...)` failure exactly: opening the target
    for writing empties it, and the process then dies before the new bytes
    land. Any writer that still takes that path leaves an unparseable config
    with the signing block gone. With an atomic replace nobody opens the
    target at all, so each mutator either completes or leaves the old file.
    """
    import contextlib

    real_write_text = Path.write_text
    truncated: list[str] = []

    def truncating_write_text(self, data, *args, **kwargs):
        if self.name != "config.yaml":
            return real_write_text(self, data, *args, **kwargs)
        truncated.append(str(self))
        real_write_text(self, "")  # the truncate half of a non-atomic write
        raise RuntimeError("interrupted mid-write")

    for label, mutate in _mutators():
        cfg_path = tmp_path / label / "config.yaml"
        write_test_config(cfg_path, team_id="T1")

        with monkeypatch.context() as m:
            m.setattr(Path, "write_text", truncating_write_text)
            with contextlib.suppress(RuntimeError):
                mutate(cfg_path)

        assert truncated == [], f"{label}: rewrote config.yaml in place ({truncated})"
        assert cfg_path.read_bytes() != b"", f"{label}: config.yaml was truncated"
        assert load_config(cfg_path).signing.team_id == "T1", f"{label}: config no longer loads"


@pytest.mark.parametrize("failure_point", ["serialize", "replace"])
def test_interrupted_rewrite_leaves_config_intact(tmp_path, monkeypatch, failure_point) -> None:
    """A failure on either side of the temp file leaves the old config alone.

    `serialize` blows up in yaml.safe_dump, before any temp file exists;
    `replace` blows up in os.replace, after a complete temp file was written
    and fsync'd — the exact moment at which the old in-place write would
    already have emptied the target.
    """
    from graft_ipa import config as config_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("interrupted mid-write")

    for label, mutate in _mutators():
        cfg_path = tmp_path / failure_point / label / "config.yaml"
        write_test_config(cfg_path, team_id="T1")
        before = cfg_path.read_bytes()

        with monkeypatch.context() as m:
            if failure_point == "serialize":
                m.setattr(config_mod.yaml, "safe_dump", boom)
            else:
                m.setattr(config_mod.os, "replace", boom)
            with pytest.raises(RuntimeError, match="interrupted mid-write"):
                mutate(cfg_path)

        assert cfg_path.exists(), f"{label}: config.yaml vanished"
        assert cfg_path.read_bytes() == before, f"{label}: config.yaml was modified"
        # It still parses, and the signing block is still there.
        assert load_config(cfg_path).signing.team_id == "T1", f"{label}: config no longer loads"
        # No half-written temp file left behind next to it. (The `.lock`
        # sidecar is the mutators' lock file and persists by design.)
        leftovers = [
            p.name for p in cfg_path.parent.iterdir()
            if p.name not in ("config.yaml", "config.yaml.lock")
        ]
        assert leftovers == [], f"{label}: leftover temp files {leftovers}"


def test_successful_rewrite_round_trips_contents(tmp_path) -> None:
    """A completed atomic write is byte-identical to the same data dumped."""
    from graft_ipa.config import _atomic_write_yaml

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())

    _atomic_write_yaml(cfg_path, raw)

    assert cfg_path.read_text() == yaml.safe_dump(raw, sort_keys=False)
    assert yaml.safe_load(cfg_path.read_text()) == raw
    assert load_config(cfg_path).signing.team_id == "T1"


def test_atomic_write_preserves_target_permissions(tmp_path) -> None:
    """Rewrites keep the config's existing mode — mkstemp's 0600 must not leak in."""
    import os
    import stat

    from graft_ipa.config import _atomic_write_yaml, set_app_tweaks

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    os.chmod(cfg_path, 0o644)

    set_app_tweaks(cfg_path, "example", ["one.deb"])
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o644

    _atomic_write_yaml(cfg_path, yaml.safe_load(cfg_path.read_text()))
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o644


def test_a_symlinked_config_stays_a_symlink_through_a_rewrite(tmp_path) -> None:
    """Dotfile setups link config.yaml into a repo. `os.replace` onto the link
    swapped the link itself for a regular file, silently forking the config
    away from the file the user edits."""
    from graft_ipa.config import load_config, set_app_tweaks

    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    target = real_dir / "config.yaml"
    write_test_config(target, team_id="T1")
    link = tmp_path / "config.yaml"
    link.symlink_to(target)

    set_app_tweaks(link, "example", ["one.deb"])

    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert load_config(target).get_app("example").tweaks == ["one.deb"]


def test_unknown_keys_are_ignored_on_load_and_kept_through_a_rewrite(tmp_path) -> None:
    """A key Graft does not know — a typo, a setting from a newer or older
    version — never fails a load, and a mutator's round trip keeps it rather
    than deleting the user's text."""
    from graft_ipa.config import set_app_tweaks

    cfg_path = tmp_path / "config.yaml"
    write_test_config(cfg_path, team_id="T1")
    raw = yaml.safe_load(cfg_path.read_text())
    raw["retired_feature"] = {"token": "x"}
    raw["apps"]["example"]["retired_option"] = "https://example.com/app"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    cfg = load_config(cfg_path)
    assert cfg.signing.team_id == "T1"
    assert cfg.get_app("example").bundle_id == raw["apps"]["example"]["bundle_id"]
    assert not hasattr(cfg, "retired_feature")

    set_app_tweaks(cfg_path, "example", ["one.deb"])

    after = yaml.safe_load(cfg_path.read_text())
    assert after["retired_feature"] == {"token": "x"}
    assert after["apps"]["example"]["retired_option"] == "https://example.com/app"
    assert after["apps"]["example"]["tweaks"] == ["one.deb"]
