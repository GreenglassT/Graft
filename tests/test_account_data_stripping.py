"""Nothing Graft stages identifies the Apple ID that bought the app.

An App Store IPA carries `iTunesMetadata.plist` (the buyer's Apple ID email
and account ids) and an `SC_Info/` directory in the app and in every extension
(the FairPlay licence for that account). Only the UnfairPlay path ever dealt
with them, and only by trusting UnfairPlay's `--strip-account-data`: an
already-decrypted `--provide-ipa` drop was staged byte for byte, and a
`--decrypt` dump kept every `SC_Info/`. Now every staging path removes both and
checks they are gone, and a build heals an IPA staged before it did.
"""

from __future__ import annotations

import json
import plistlib
import stat
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import decrypt_runner, ipa_inspect, provide_ipa
from graft_ipa.config import load_config
from tests.appstore_fakes import FakeTools, macho

from .test_free_account_build import _build, free_config  # noqa: F401 — the fixture
from .test_lc_discovery import fake_device

FAKE_EMAIL = "someone@example.com"

#: Everything in the fixture that identifies the account.
ACCOUNT_ENTRIES = {
    "iTunesMetadata.plist",
    "Payload/App.app/SC_Info/",
    "Payload/App.app/SC_Info/App.sinf",
    "Payload/App.app/SC_Info/App.supp",
    "Payload/App.app/PlugIns/Ext.appex/SC_Info/Ext.sinf",
}


def account_ipa(path: Path, *, bundle_id: str = "com.example.app", version: str = "21.46.3",
                cryptid: int | None = 0) -> Path:
    """A decrypted IPA still carrying its buyer's metadata and licences."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("iTunesMetadata.plist", plistlib.dumps({
            "appleId": FAKE_EMAIL,
            "com.apple.iTunesStore.downloadInfo": {
                "accountInfo": {"AppleID": FAKE_EMAIL, "DSPersonID": 1234567890},
            },
            "itemName": "Example",
        }))
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "CFBundleExecutable": "App",
        }))
        exe = zipfile.ZipInfo("Payload/App.app/App")
        exe.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(exe, macho(cryptid) if cryptid is not None else b"\x00\x00")
        zf.writestr(zipfile.ZipInfo("Payload/App.app/SC_Info/"), b"")
        zf.writestr("Payload/App.app/SC_Info/App.sinf", b"licence for " + FAKE_EMAIL.encode())
        zf.writestr("Payload/App.app/SC_Info/App.supp", b"supp")
        zf.writestr("Payload/App.app/PlugIns/Ext.appex/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": f"{bundle_id}.ext",
        }))
        zf.writestr("Payload/App.app/PlugIns/Ext.appex/SC_Info/Ext.sinf", b"ext licence")
        zf.writestr("Payload/App.app/marker.txt", b"keep me")
    return path


def _names(ipa: Path) -> set[str]:
    with zipfile.ZipFile(ipa) as zf:
        return set(zf.namelist())


def assert_identifies_nobody(ipa: Path) -> None:
    names = _names(ipa)
    assert not names & ACCOUNT_ENTRIES, sorted(names & ACCOUNT_ENTRIES)
    assert not [n for n in names if "SC_Info" in n.split("/")]
    assert FAKE_EMAIL.encode() not in ipa.read_bytes()


# ---- the rule ----


def test_account_data_is_found_in_the_app_and_every_extension(tmp_path: Path) -> None:
    ipa = account_ipa(tmp_path / "a.ipa")
    assert set(ipa_inspect.account_data_in(ipa)) == ACCOUNT_ENTRIES


def test_stripping_removes_only_account_data(tmp_path: Path) -> None:
    src = account_ipa(tmp_path / "a.ipa")
    dest = tmp_path / "b.ipa"
    removed = ipa_inspect.strip_account_data(src, dest)

    assert set(removed) == ACCOUNT_ENTRIES
    assert_identifies_nobody(dest)
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dest) as b:
        kept = [i for i in a.infolist() if i.filename not in ACCOUNT_ENTRIES]
        assert [i.filename for i in kept] == b.namelist()
        for info in kept:
            assert b.read(info.filename) == a.read(info.filename), info.filename
            assert b.getinfo(info.filename).external_attr == info.external_attr, info.filename


# ---- every staging path ----


def test_provide_stages_a_copy_that_identifies_nobody(tmp_path: Path) -> None:
    src = account_ipa(tmp_path / "drop.ipa")
    before = src.read_bytes()
    dest, _ = provide_ipa.provide(
        cache_dir=tmp_path / "cache", expected_bundle_id="com.example.app", source_path=src,
    )
    assert_identifies_nobody(dest)
    assert "Payload/App.app/marker.txt" in _names(dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert src.read_bytes() == before, "the user's own file is not theirs to rewrite"


def test_staging_checks_the_result_rather_than_trusting_the_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the stripping ever leaves something behind, nothing is staged."""
    import shutil

    from graft_ipa.errors import IPAValidationError

    def leaky(src, dest, **_):
        shutil.copyfile(src, dest)
        return []

    monkeypatch.setattr(provide_ipa, "strip_account_data", leaky)
    src = account_ipa(tmp_path / "drop.ipa")
    cache = tmp_path / "cache"
    with pytest.raises(IPAValidationError) as exc:
        provide_ipa.provide(cache_dir=cache, expected_bundle_id="com.example.app",
                            source_path=src)
    assert "account data" in str(exc.value)
    assert list(cache.iterdir()) == [], "neither the IPA nor its partial file may remain"


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def config(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


def _staged(config: Path) -> Path:
    (staged,) = sorted(load_config(config).cache_dir.glob("*.ipa"))
    return staged


def test_a_decrypted_provide_ipa_drop_is_staged_without_account_data(
    config: Path, tmp_path: Path
) -> None:
    drop = account_ipa(tmp_path / "Downloads" / "Example.ipa")
    result = CliRunner().invoke(cli_mod.app, ["--json", "--provide-ipa", str(drop),
                                              "--app", "example"])
    assert result.exit_code == 0, result.output
    assert_identifies_nobody(_staged(config))


def test_an_appstore_get_does_not_trust_the_strip_flag(
    config: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """An UnfairPlay that ignored `--strip-account-data` (or a future one that
    strips less) must not decide what reaches the cache."""
    fakes.set(decrypted_ipa=str(account_ipa(tmp_path / "fixtures" / "leaky.ipa")))
    result = CliRunner().invoke(cli_mod.app, ["--json", "--appstore-get", "--app", "example"])
    assert result.exit_code == 0, result.output
    assert_identifies_nobody(_staged(config))


def test_a_decrypt_dump_is_staged_without_its_licences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dump(bundle_id, out_dir, **_k):
        ipa = account_ipa(Path(out_dir) / "dump.ipa", bundle_id=bundle_id)
        return decrypt_runner.DecryptResult(ipa_path=ipa, bundle_id=bundle_id, stdout="")

    monkeypatch.setattr(decrypt_runner, "decrypt_app", dump)
    monkeypatch.setattr(cli_mod, "verify_decrypted", lambda _d: [])
    cache = tmp_path / "cache"
    cfg = SimpleNamespace(default_device_udid=None, cache_dir=cache)
    cli_mod._do_decrypt(cfg, "com.example.app", device="U", mode="all",
                        backend=decrypt_runner.FRIDA)
    (staged,) = cache.glob("*.ipa")
    assert_identifies_nobody(staged)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


# ---- and what was staged before ----


def test_a_build_heals_an_ipa_staged_before_this_rule(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    cfg = free_config
    staged = next(cfg.cache_dir.glob("*.ipa"))
    account_ipa(staged, version="1.0", cryptid=None)
    fake_device(monkeypatch, ["com.kdt.livecontainer.FGHIJ67890"])

    events = _build(cfg, tmp_path)

    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True, events[-1]
    assert_identifies_nobody(staged)
    warnings = [e["line"] for e in events if e["type"] == "log" and e.get("source") == "warning"]
    assert any("Apple ID" in w for w in warnings), warnings


def test_a_dry_run_build_leaves_the_staged_ipa_alone(
    free_config, tmp_path: Path  # noqa: F811
) -> None:
    from graft_ipa.build_log import BuildLog
    from graft_ipa.json_output import JsonEmitter

    cfg = free_config
    staged = next(cfg.cache_dir.glob("*.ipa"))
    account_ipa(staged, version="1.0", cryptid=None)
    before = staged.read_bytes()
    cli_mod._build_one(
        cfg=cfg, log=BuildLog(cfg.state_dir / "log.sqlite"), app_name="example",
        dry_run=True, skip_install=True, skip_decrypt_check=True, verbose=False,
        device=None, emitter=JsonEmitter(stream=StringIO()), trash_dir=tmp_path / "Trash",
    )
    assert staged.read_bytes() == before
