"""Where Graft finds ipatool, and which versions it refuses wherever it finds them."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from graft_ipa import appstore, ipatool
from graft_ipa.config import AppStoreToolsConfig, Config, load_config
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FakeTools


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return h


def _install_copy(src: FakeTools, dest_dir: Path) -> Path:
    """A second ipatool, behaving per the same scenario, at `dest_dir`/ipatool."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "ipatool"
    shutil.copy(src.ipatool, dest)
    shutil.copy(src.ipatool.with_suffix(".py"), dest_dir / "ipatool.py")
    shutil.copy(src.root / "scenario.json", dest_dir / "scenario.json")
    dest.chmod(0o755)
    return dest


def test_known_bad_versions_are_a_small_constant() -> None:
    assert ipatool.KNOWN_BAD_VERSIONS == frozenset({"2.8.1"})


def test_configured_path_wins_over_everything(tmp_path: Path, home: Path, monkeypatch) -> None:
    configured = _install_copy(FakeTools(tmp_path / "a"), tmp_path / "configured")
    _install_copy(FakeTools(tmp_path / "b"), home / ".local" / "bin")
    on_path = _install_copy(FakeTools(tmp_path / "c"), tmp_path / "pathdir")
    monkeypatch.setenv("PATH", f"{on_path.parent}:/usr/bin:/bin")

    assert ipatool.locate(configured) == configured


def test_default_location_is_next(tmp_path: Path, home: Path, monkeypatch) -> None:
    default = _install_copy(FakeTools(tmp_path / "b"), home / ".local" / "bin")
    on_path = _install_copy(FakeTools(tmp_path / "c"), tmp_path / "pathdir")
    monkeypatch.setenv("PATH", f"{on_path.parent}:/usr/bin:/bin")

    assert ipatool.locate(tmp_path / "missing" / "ipatool") == default


def test_path_is_the_last_resort(tmp_path: Path, home: Path, monkeypatch) -> None:
    on_path = _install_copy(FakeTools(tmp_path / "c"), tmp_path / "pathdir")
    monkeypatch.setenv("PATH", f"{on_path.parent}:/usr/bin:/bin")

    assert ipatool.locate(tmp_path / "missing" / "ipatool") == on_path


def test_nothing_found_returns_the_configured_path_so_messages_name_it(
    tmp_path: Path, home: Path
) -> None:
    configured = tmp_path / "missing" / "ipatool"
    assert ipatool.locate(configured) == configured


def test_the_bad_version_hint_is_generic_not_pinned_to_one_bad_release() -> None:
    hint = ipatool.bad_version_hint("2.8.1")
    assert "tested with ipatool 2.8.0" in hint
    assert "2.8.1" in hint
    assert "majd/ipatool#672" in hint
    assert ipatool.RELEASE_URL in hint
    assert "~/.local/bin/ipatool" in hint
    assert "appstore.ipatool" in hint


def _cfg_with_only_a_path_copy(
    tmp_path: Path, home: Path, monkeypatch, version: str
) -> tuple[Config, FakeTools]:
    """Config pointing at a nonexistent ipatool; the only real one is on PATH."""
    fakes = FakeTools(tmp_path / "tools")
    fakes.set(ipatool_version=version)
    _install_copy(fakes, tmp_path / "pathdir")
    monkeypatch.setenv("PATH", f"{tmp_path / 'pathdir'}:/usr/bin:/bin")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("signing: {}\n")
    tools = AppStoreToolsConfig(
        ipatool=tmp_path / "missing" / "ipatool",
        unfairplay=fakes.unfairplay,
        unfairplay_models=fakes.models,
    )
    return tools, fakes  # type: ignore[return-value]


def test_status_finds_a_path_install(tmp_path: Path, home: Path, monkeypatch) -> None:
    tools, _ = _cfg_with_only_a_path_copy(tmp_path, home, monkeypatch, "2.8.0")

    payload = appstore.status(tools)  # type: ignore[arg-type]

    assert payload["ipatool"]["present"] is True
    assert payload["ipatool"]["version"] == "2.8.0"
    assert payload["ipatool"]["path"] == str(tmp_path / "pathdir" / "ipatool")
    assert payload["ipatool"]["warning"] is None


def test_status_warns_about_a_bad_version_found_on_path(
    tmp_path: Path, home: Path, monkeypatch
) -> None:
    tools, _ = _cfg_with_only_a_path_copy(tmp_path, home, monkeypatch, "2.8.1")

    payload = appstore.status(tools)  # type: ignore[arg-type]

    assert "#672" in payload["ipatool"]["warning"]
    assert "appstore.ipatool" in payload["ipatool"]["warning"]
    assert any("2.8.0" in c for c in payload["setup_commands"])


def test_fetch_refuses_a_bad_version_wherever_it_was_found(
    tmp_path: Path, home: Path, monkeypatch, temp_config_env: Path
) -> None:
    tools, fakes = _cfg_with_only_a_path_copy(tmp_path, home, monkeypatch, "2.8.1")
    cfg = load_config(temp_config_env)
    cfg.appstore = tools  # type: ignore[assignment]

    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pass

    assert "2.8.1" in e.value.message
    assert "tested with ipatool 2.8.0" in (e.value.hint or "")
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]


def test_fetch_refuses_a_bad_version_at_the_default_location_too(
    tmp_path: Path, home: Path, monkeypatch, temp_config_env: Path
) -> None:
    fakes = FakeTools(tmp_path / "tools")
    fakes.set(ipatool_version="2.8.1")
    _install_copy(fakes, home / ".local" / "bin")
    cfg = load_config(temp_config_env)
    cfg.appstore = AppStoreToolsConfig(
        ipatool=tmp_path / "missing" / "ipatool",
        unfairplay=fakes.unfairplay,
        unfairplay_models=fakes.models,
    )

    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pass

    assert "2.8.1" in e.value.message


def test_fetch_with_nothing_found_names_the_configured_path(
    tmp_path: Path, home: Path, temp_config_env: Path
) -> None:
    cfg = load_config(temp_config_env)
    missing = tmp_path / "missing" / "ipatool"
    cfg.appstore = AppStoreToolsConfig(ipatool=missing)

    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pass

    assert str(missing) in e.value.message

