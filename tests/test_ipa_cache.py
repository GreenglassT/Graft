"""`ipa_cache`: where a build's source IPA comes from, now that it is only ever
the cache.

The lookup itself (`find_in_cache`, its ordering and its plist check) is pinned
by `test_cache_latest.py`, `test_ipa_memo.py` and `test_cli_staged_ipas.py`.
These pin `acquire_ipa`: a hit is returned, and a miss names every way left to
put an IPA in the cache — there is no network fallback any more.
"""

from __future__ import annotations

import inspect
import plistlib
import zipfile
from pathlib import Path

import pytest

from graft_ipa import ipa_cache
from graft_ipa.config import AppConfig, CyanOptions
from graft_ipa.errors import IPANotFoundError

BUNDLE = "com.example.app"


def _cfg(tmp_path: Path):
    class _Cfg:
        cache_dir = tmp_path / "ipas"
    return _Cfg()


def _app() -> AppConfig:
    return AppConfig(
        name="example", bundle_id=BUNDLE, preferred_version="latest",
        tweaks=[], cyan=CyanOptions(),
    )


def _fake_ipa(cache: Path, version: str) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{BUNDLE}_{version}.ipa"
    info = {"CFBundleIdentifier": BUNDLE, "CFBundleShortVersionString": version}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", plistlib.dumps(info))
    return path


def test_a_cached_ipa_is_what_acquire_returns(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    path = _fake_ipa(cfg.cache_dir, "1.2.3")

    got = ipa_cache.acquire_ipa(cfg, _app())

    assert (got.path, got.bundle_id, got.version) == (path, BUNDLE, "1.2.3")


def test_a_miss_names_all_three_remaining_sources(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    with pytest.raises(IPANotFoundError) as exc:
        ipa_cache.acquire_ipa(cfg, _app())

    assert exc.value.message == "no IPA found for example"
    hint = exc.value.hint or ""
    assert "current release" not in hint  # only said when a pin makes it matter
    assert "--provide-ipa" in hint
    assert f"--decrypt {BUNDLE}" in hint
    assert "--appstore-get --app example" in hint
    assert str(cfg.cache_dir) in hint


def test_a_pinned_miss_names_the_version_and_what_the_app_store_would_fetch(
    tmp_path: Path,
) -> None:
    """A pinned app can miss with other versions cached; "no IPA found" alone
    would be wrong. And `--appstore-get` fetches the current release, which
    is not the pinned one — the hint has to say so rather than send the user
    round a loop that cannot satisfy the pin."""
    cfg = _cfg(tmp_path)
    _fake_ipa(cfg.cache_dir, "2.0.0")
    app = _app()
    app.preferred_version = "1.2.3"

    with pytest.raises(IPANotFoundError) as exc:
        ipa_cache.acquire_ipa(cfg, app)

    assert exc.value.message == f"no IPA found for example: version 1.2.3 of {BUNDLE}"
    hint = exc.value.hint or ""
    assert "--provide-ipa" in hint
    assert f"--decrypt {BUNDLE}" in hint
    assert "--appstore-get --app example" in hint
    assert "fetches the current release, not 1.2.3" in hint


def test_a_miss_keeps_the_old_exit_code(tmp_path: Path) -> None:
    """4 was the old acquisition error's code; scripts that branch on it keep working."""
    assert IPANotFoundError.exit_code == 4


def test_decrypt_without_a_bundle_id_is_a_usage_error(temp_config_env: Path) -> None:
    """A missing argument is the caller's mistake (exit 2, like every other
    "requires" here), not "no IPA was found" (exit 4) — the GUI tells the two
    apart by code."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--decrypt", ""])

    assert result.exit_code == 2, result.output
    assert "--decrypt requires a bundle id" in result.output


def test_there_is_no_redownload_any_more() -> None:
    """`force_redownload` only ever meant "skip the cache and download it
    again". With nothing left that downloads, a build that skips the cache
    can only fail."""
    assert list(inspect.signature(ipa_cache.acquire_ipa).parameters) == ["cfg", "app"]
