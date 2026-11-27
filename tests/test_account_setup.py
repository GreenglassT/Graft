"""`account_setup`: `graft --init` without questions, which the Mac app runs.

Real .p12 files and profile plists throughout (`signing_fixtures`), so the
checks themselves run; only the Keychain is a fake."""

from __future__ import annotations

import os
import re
import signal
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from graft_ipa import account_setup, config, keychain
from graft_ipa.config import add_app, add_repo, load_config, set_settings, write_default_config
from graft_ipa.errors import ConfigError, GraftError

from .keychain_fakes import FakeSecurity
from .signing_fixtures import (
    COMMON_NAME,
    LIBRESSL,
    OPENSSL3,
    PASSWORD,
    TEAM,
    UDIDS,
    make_identity,
    openssl_p12,
)


@contextmanager
def _must_not_block(seconds: int = 5):
    """Fail the test, rather than hang the suite, if the body blocks."""

    def _hung(signum, frame):
        raise TimeoutError("blocked reading a chosen file")

    previous = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# --- reading and checking the two files ----------------------------------------


def test_a_matching_p12_and_profile_are_read_and_described(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)

    got = account_setup.read_identity(ident.p12, ident.profile, PASSWORD)

    assert got.team_id == TEAM
    assert got.devices == UDIDS
    assert got.certificate == COMMON_NAME
    assert got.profile_name == "Test Wildcard"
    assert got.certificate_listed is True
    assert got.expires is not None and got.expires > datetime.now(UTC)
    # What was checked is what will be installed.
    assert got.p12_bytes == ident.p12.read_bytes()
    assert got.profile_bytes == ident.profile.read_bytes()
    assert "p12_bytes" not in repr(got), "the private key is out of the repr"


def test_the_wrong_password_is_said_in_words(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)

    with pytest.raises(ConfigError, match=r"couldn't open Certificates\.p12 with that password"):
        account_setup.read_identity(ident.p12, ident.profile, "not it")


def test_a_certificate_the_profile_does_not_list_is_refused(tmp_path: Path) -> None:
    """zsign signs with the .p12's certificate whatever the profile says, and
    the phone refuses the result: refused here, before anything is written."""
    ident = make_identity(tmp_path, listed=False)

    with pytest.raises(ConfigError, match=r"\(serial 5EED\) isn't one Test.mobileprovision") as err:
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)
    assert "download a profile that includes this certificate" in (err.value.hint or "")


def test_a_profile_that_lists_no_certificates_is_not_judged_on_them(tmp_path: Path) -> None:
    ident = make_identity(tmp_path, listed=None)

    got = account_setup.read_identity(ident.p12, ident.profile, PASSWORD)

    assert got.certificate_listed is False


def test_an_expired_profile_is_refused(tmp_path: Path) -> None:
    ident = make_identity(tmp_path, expires_in=timedelta(days=-1))

    with pytest.raises(ConfigError, match=r"Test\.mobileprovision expired on") as err:
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)
    assert "developer.apple.com" in (err.value.hint or "")


def test_a_profile_that_names_no_team_is_refused(tmp_path: Path) -> None:
    ident = make_identity(tmp_path, team=None)

    with pytest.raises(ConfigError, match="names no team"):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)


def test_a_file_that_is_not_a_profile_is_refused(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)
    ident.profile.write_bytes(b"not a profile")

    with pytest.raises(ConfigError, match="failed to decode"):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)


def test_a_symlinked_choice_is_read_through_its_real_file(tmp_path: Path) -> None:
    """A .p12 or profile chosen in Downloads or a cloud-synced folder is often
    reached through a link: the link is resolved and its real file read once.
    Only a destination is never written through a link (`private_write`)."""
    ident = make_identity(tmp_path)
    p12_link = tmp_path / "link.p12"
    p12_link.symlink_to(ident.p12)
    profile_link = tmp_path / "link.mobileprovision"
    profile_link.symlink_to(ident.profile)

    got = account_setup.read_identity(p12_link, profile_link, PASSWORD)

    assert got.team_id == TEAM
    assert got.p12_bytes == ident.p12.read_bytes()
    assert got.profile_bytes == ident.profile.read_bytes()


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)
    fifo = tmp_path / "fifo.mobileprovision"
    os.mkfifo(fifo)

    with _must_not_block(), pytest.raises(ConfigError, match="is not a file"):
        account_setup.read_identity(ident.p12, fifo, PASSWORD)


def test_a_link_to_what_is_not_a_file_is_refused(tmp_path: Path) -> None:
    """Resolving a link doesn't skip the check on what it leads to."""
    ident = make_identity(tmp_path)
    folder = tmp_path / "a folder"
    folder.mkdir()
    link = tmp_path / "link.p12"
    link.symlink_to(folder)

    with pytest.raises(ConfigError, match="is not a file"):
        account_setup.read_identity(link, ident.profile, PASSWORD)


def test_a_file_too_large_to_be_either_is_refused(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)
    big = tmp_path / "big.p12"
    with big.open("wb") as f:
        f.truncate(account_setup.MAX_INPUT_BYTES + 1)

    with pytest.raises(ConfigError, match="larger than 1 MiB"):
        account_setup.read_identity(big, ident.profile, PASSWORD)


def test_a_missing_file_says_which_it_was(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)

    with pytest.raises(ConfigError, match="provisioning profile not found"):
        account_setup.read_identity(ident.p12, tmp_path / "gone.mobileprovision", PASSWORD)


# --- the free setup ---------------------------------------------------------------


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where `--init` writes, with every directory under `tmp_path`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    return tmp_path / "cfg" / "graft" / "config.yaml"


def _never_asked() -> str:
    raise AssertionError("asked for a password")


def _steps() -> tuple[list[str], object]:
    seen: list[str] = []
    return seen, lambda step, detail: seen.append(step)


def test_free_on_a_new_machine_writes_the_free_starter(cfg_path: Path) -> None:
    seen, on_step = _steps()

    result = account_setup.setup_account(
        cfg_path, account="free", password=_never_asked, on_step=on_step
    )

    assert (result.account, result.change, result.dry_run) == ("free", account_setup.CREATED, False)
    assert load_config(cfg_path).free_account
    assert "signing" not in yaml.safe_load(cfg_path.read_text())
    assert result.steps == config.FREE_DEVICE_STEPS
    assert seen == ["config"]
    assert stat.S_IMODE(cfg_path.parent.stat().st_mode) == 0o700


def test_free_on_a_free_config_changes_nothing(cfg_path: Path) -> None:
    write_default_config(cfg_path, free_account=True)
    add_app(cfg_path, "example", {"bundle_id": "com.example.app"})
    before = cfg_path.read_text()

    result = account_setup.setup_account(cfg_path, account="free")

    assert result.change == account_setup.UNCHANGED
    assert cfg_path.read_text() == before
    assert result.steps == config.FREE_DEVICE_STEPS


def test_free_on_a_paid_config_switches_it_and_keeps_everything_else(cfg_path: Path) -> None:
    write_default_config(cfg_path, team_id=TEAM)
    add_app(cfg_path, "example", {"bundle_id": "com.example.app"})
    add_app(cfg_path, "side", {"bundle_id": "com.example.side", "delivery": "sideload"})
    add_repo(cfg_path, "https://repo.example.com", "Example repo")
    before = yaml.safe_load(cfg_path.read_text())

    result = account_setup.setup_account(cfg_path, account="free", password=_never_asked)

    after = yaml.safe_load(cfg_path.read_text())
    assert result.change == account_setup.SWITCHED
    assert "signing" not in after
    assert after == {k: v for k, v in before.items() if k != "signing"}
    assert result.sideloads == ("side",)
    assert load_config(cfg_path).free_account


def test_a_dry_run_switch_reports_and_writes_nothing(cfg_path: Path) -> None:
    write_default_config(cfg_path, team_id=TEAM)
    add_app(cfg_path, "side", {"bundle_id": "com.example.side", "delivery": "sideload"})
    before = cfg_path.read_text()

    result = account_setup.setup_account(cfg_path, account="free", dry_run=True)

    assert (result.change, result.dry_run, result.sideloads) == (account_setup.SWITCHED, True, ("side",))
    assert cfg_path.read_text() == before


def test_a_dry_run_on_a_new_machine_creates_nothing(cfg_path: Path) -> None:
    result = account_setup.setup_account(cfg_path, account="free", dry_run=True)

    assert result.change == account_setup.CREATED
    assert not cfg_path.parent.exists()


def test_free_with_signing_files_is_refused(cfg_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--p12 is only for --account paid"):
        account_setup.setup_account(cfg_path, account="free", p12_path=tmp_path / "x.p12")
    assert not cfg_path.exists()


@pytest.mark.parametrize("account", ["free", "paid"])
def test_a_config_that_does_not_parse_is_never_replaced(
    cfg_path: Path, tmp_path: Path, account: str
) -> None:
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("this: is: not: valid: yaml:\n")
    ident = make_identity(tmp_path)

    with pytest.raises(ConfigError, match="can't be read, so the setup won't touch it"):
        account_setup.setup_account(
            cfg_path, account=account,
            p12_path=ident.p12 if account == "paid" else None,
            profile_path=ident.profile if account == "paid" else None,
            password=_never_asked,
        )
    assert cfg_path.read_text() == "this: is: not: valid: yaml:\n"


def test_an_account_that_is_neither_is_refused(cfg_path: Path) -> None:
    with pytest.raises(ConfigError, match="--account must be free or paid, not 'team'"):
        account_setup.setup_account(cfg_path, account="team")


# --- the paid setup ---------------------------------------------------------------


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    fake = FakeSecurity()
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def _paid(cfg_path: Path, ident, *, password: str = PASSWORD, **kw):
    return account_setup.setup_account(
        cfg_path, account="paid", p12_path=ident.p12, profile_path=ident.profile,
        password=lambda: password, **kw,
    )


def _nothing_written(cfg_path: Path, fake: FakeSecurity) -> None:
    for name in ("config.yaml", account_setup.P12_NAME, account_setup.PROFILE_NAME):
        assert not (cfg_path.parent / name).exists(), name
    assert fake.items == {}, "something reached the Keychain"


def test_paid_on_a_new_machine_stores_copies_and_writes(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    ident = make_identity(tmp_path)
    seen, on_step = _steps()

    result = _paid(cfg_path, ident, on_step=on_step)

    cfg_dir = cfg_path.parent
    for name, src in ((account_setup.P12_NAME, ident.p12), (account_setup.PROFILE_NAME, ident.profile)):
        copy = cfg_dir / name
        assert copy.read_bytes() == src.read_bytes(), name
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600, name
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["signing"]["team_id"] == TEAM
    assert Path(raw["signing"]["cert"]) == cfg_dir.absolute() / account_setup.P12_NAME
    assert Path(raw["signing"]["profile"]) == cfg_dir.absolute() / account_setup.PROFILE_NAME
    assert load_config(cfg_path).free_account is False
    assert fake_keychain.items[("graft", f"p12-{TEAM}")] == PASSWORD
    assert PASSWORD not in cfg_path.read_text()
    assert result.change == account_setup.CREATED
    assert result.identity is not None and result.identity.devices == UDIDS
    assert seen == ["profile", "certificate", "keychain", "files", "config"]
    fields = result.fields()
    assert fields["team_id"] == TEAM and fields["device_count"] == 2
    assert fields["expires"].endswith("Z")
    assert PASSWORD not in repr(fields)


def test_paid_on_a_free_config_upgrades_it_and_moves_no_app(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    write_default_config(cfg_path, free_account=True)
    add_app(cfg_path, "plain", {"bundle_id": "com.example.plain"})
    add_repo(cfg_path, "https://repo.example.com", "Example repo")
    ident = make_identity(tmp_path)

    result = _paid(cfg_path, ident)

    after = yaml.safe_load(cfg_path.read_text())
    assert result.change == account_setup.UPGRADED
    assert result.pinned == ("plain",)
    assert after["apps"]["plain"]["delivery"] == "livecontainer"
    assert after["repos"]
    assert load_config(cfg_path).apps["plain"].delivery == "livecontainer"


def test_paid_on_a_paid_config_is_refused_before_a_password_is_asked(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    write_default_config(cfg_path, team_id="FGHIJ67890")
    before = cfg_path.read_text()
    ident = make_identity(tmp_path)

    with pytest.raises(ConfigError, match="already set up for a paid account"):
        account_setup.setup_account(
            cfg_path, account="paid", p12_path=ident.p12, profile_path=ident.profile,
            password=_never_asked,
        )
    assert cfg_path.read_text() == before
    assert fake_keychain.items == {}


def test_paid_without_both_files_is_refused_before_a_password_is_asked(
    cfg_path: Path, tmp_path: Path
) -> None:
    ident = make_identity(tmp_path)

    with pytest.raises(ConfigError, match="--account paid needs --profile"):
        account_setup.setup_account(
            cfg_path, account="paid", p12_path=ident.p12, password=_never_asked
        )


@pytest.mark.skipif(LIBRESSL is None, reason="needs /usr/bin/openssl")
def test_a_blank_password_p12_is_set_up_and_its_builds_find_it(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """Keychain Access can export a .p12 with a blank password. It opens with
    "", the Keychain stores "", and a build reads "" back as a password."""
    from graft_ipa import signer

    ident = make_identity(tmp_path, password="", p12_maker=openssl_p12(LIBRESSL))

    _paid(cfg_path, ident, password="")

    assert fake_keychain.items == {("graft", f"p12-{TEAM}"): ""}
    assert signer.resolve_p12_password(load_config(cfg_path)) == ""


@pytest.mark.skipif(LIBRESSL is None, reason="needs /usr/bin/openssl")
@pytest.mark.parametrize("where", ["--check", "sideload build", "--lc-install"])
def test_a_stored_blank_password_counts_as_found_where_builds_read_it(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Combined fix check #17: the resolver alone was tested. The three
    readers that decide whether a password was found each test `is None`;
    `if not pw` there would take a stored "" for none."""
    from graft_ipa import lc_manage, preflight, signer

    ident = make_identity(tmp_path, password="", p12_maker=openssl_p12(LIBRESSL))
    _paid(cfg_path, ident, password="")
    monkeypatch.delenv(load_config(cfg_path).signing.cert_password_env, raising=False)
    cfg = load_config(cfg_path)

    if where == "--check":
        (row,) = [r for r in preflight.check_signing_files(cfg) if r.name == ".p12 password"]
        assert (row.ok, row.detail) == (True, "keychain entry present")
    elif where == "sideload build":
        config.add_app(cfg_path, "example", {"bundle_id": "com.example.app", "tweaks": [],
                                             "delivery": "sideload"})
        cfg = load_config(cfg_path)
        src = tmp_path / "in.ipa"
        src.write_bytes(b"PK")
        result = signer.run_zsign(cfg, src, tmp_path / "out.ipa", app=cfg.get_app("example"), dry_run=True)
        assert isinstance(result, signer.SignResult)
    else:
        handed = {}
        monkeypatch.setattr(lc_manage, "install", lambda **kw: handed.update(kw) or [])
        monkeypatch.setattr(lc_manage, "_target", lambda cfg, udid: "u1")
        monkeypatch.setattr(lc_manage, "AscProfiles", lambda cfg, password, *, target=None, note=None: None)
        monkeypatch.setattr(lc_manage, "require_app_store_connect", lambda cfg: None)
        lc_manage.run_install(cfg, udid="u1", instances=None, channel=None, force=False,
                              events=lc_manage.LcEvents(None, warn=lambda line: None))
        assert handed["p12_password"] == ""


def test_an_empty_password_for_a_p12_that_has_one_says_so(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    with pytest.raises(ConfigError, match=r"Certificates\.p12 doesn't open without a password") as err:
        _paid(cfg_path, make_identity(tmp_path), password="")
    assert "send it on stdin" not in (err.value.hint or "")
    assert "exported it from Keychain Access" in (err.value.hint or "")
    _nothing_written(cfg_path, fake_keychain)


def test_a_wrong_password_writes_nothing(cfg_path: Path, tmp_path: Path, fake_keychain) -> None:
    with pytest.raises(ConfigError, match="couldn't open"):
        _paid(cfg_path, make_identity(tmp_path), password="not it")
    _nothing_written(cfg_path, fake_keychain)


def test_a_certificate_the_profile_does_not_list_writes_nothing(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    with pytest.raises(ConfigError, match="isn't one"):
        _paid(cfg_path, make_identity(tmp_path, listed=False))
    _nothing_written(cfg_path, fake_keychain)


def test_a_keychain_that_refuses_leaves_nothing_on_disk(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    fake_keychain.fail_writes = True

    with pytest.raises(GraftError, match=r"couldn't store the \.p12 password in the Keychain"):
        _paid(cfg_path, make_identity(tmp_path))
    _nothing_written(cfg_path, fake_keychain)


def test_a_config_that_cannot_be_written_takes_the_new_copies_back(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(account_setup, "write_default_config", full)

    with pytest.raises(OSError, match="No space left"):
        _paid(cfg_path, make_identity(tmp_path))
    assert not (cfg_path.parent / account_setup.P12_NAME).exists()
    assert not (cfg_path.parent / account_setup.PROFILE_NAME).exists()


def test_a_link_planted_where_a_copy_goes_is_replaced_not_written_through(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    cfg_path.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    (cfg_path.parent / account_setup.P12_NAME).symlink_to(victim)

    _paid(cfg_path, make_identity(tmp_path))

    assert victim.read_bytes() == b"untouched"
    assert not (cfg_path.parent / account_setup.P12_NAME).is_symlink()


def test_a_dry_run_checks_everything_and_writes_nothing(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    seen, on_step = _steps()

    result = _paid(cfg_path, make_identity(tmp_path), dry_run=True, on_step=on_step)

    assert result.dry_run is True and result.identity is not None
    assert result.summary == account_setup.DRY_RUN_SUMMARY
    assert seen == ["profile", "certificate", "config"]
    _nothing_written(cfg_path, fake_keychain)
    assert not cfg_path.parent.exists()


# --- a team ID off a profile is checked before it is stored ------------------------

#: Teams a crafted profile could name. The first would end the `security -i`
#: command that stores the password and start another.
_NOT_TEAM_IDS = [
    "ABCDE12345\ndelete-keychain login.keychain",
    'ABCDE"1234',
    "ABCDE1234\\",
    "abcde12345",
    "ABCDE1234",
]


@pytest.mark.parametrize("team", _NOT_TEAM_IDS)
def test_a_team_id_apple_would_never_issue_is_refused_before_the_keychain(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, team: str
) -> None:
    with pytest.raises(ConfigError, match="team ID isn't a valid Apple team ID"):
        _paid(cfg_path, make_identity(tmp_path, team=team))
    assert fake_keychain.calls == [], "the Keychain was reached"
    _nothing_written(cfg_path, fake_keychain)


def test_the_interactive_init_does_not_keep_a_crafted_team_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--init` in Terminal reads the team off the profile into config.yaml,
    which `--set-p12-password` later turns into a Keychain account: a crafted
    one is left out, and the placeholder kept, as for a profile with none."""
    from io import StringIO

    from rich.console import Console

    from graft_ipa import init_flow

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    ident = make_identity(tmp_path, team="ABCDE12345\ndelete-keychain login.keychain")
    answers = {".p12": str(ident.p12), ".mobileprovision": str(ident.profile)}
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(
        lambda question, *a, **k: next(v for key, v in answers.items() if key in question)
    ))
    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(lambda *a, **k: True))
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    out = StringIO()

    signing = init_flow._ask_signing(Console(file=out, width=300), cfg_dir)

    assert signing["team_id"] == config.TEAM_ID_PLACEHOLDER
    assert "isn't a valid Apple team ID" in out.getvalue()


# --- replacing a paid account's certificate and profile ---------------------------

NEW_PASSWORD = "a new password"
OTHER_TEAM = "FGHIJ67890"


def _outside_signing(text: str) -> list[str]:
    """config.yaml's lines, without the `signing:` block."""
    kept, inside = [], False
    for line in text.splitlines():
        if line.startswith("signing:"):
            inside = True
            continue
        if inside and line.startswith((" ", "\t")):
            continue
        inside = False
        kept.append(line)
    return kept


@pytest.fixture
def paid_config(cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity) -> Path:
    """A paid config set up by `setup_account` itself, with an app, a repo and
    a setting of its own, as a year of use leaves it."""
    _paid(cfg_path, make_identity(tmp_path / "old"))
    add_app(cfg_path, "example", {"bundle_id": "com.example.app", "delivery": "livecontainer"})
    add_repo(cfg_path, "https://repo.example.com", "Example repo")
    set_settings(cfg_path, archive_keep=4)
    return cfg_path


def _replace(cfg_path: Path, ident, **kw):
    return account_setup.setup_account(
        cfg_path, account="paid", p12_path=ident.p12, profile_path=ident.profile,
        password=lambda: NEW_PASSWORD, replace=True, **kw,
    )


def _identity_on_disk(cfg_path: Path, fake: FakeSecurity) -> tuple:
    cfg_dir = cfg_path.parent
    return (
        cfg_path.read_text(),
        (cfg_dir / account_setup.P12_NAME).read_bytes(),
        (cfg_dir / account_setup.PROFILE_NAME).read_bytes(),
        dict(fake.items),
        sorted(p.name for p in cfg_dir.iterdir()),
    )


def test_paid_on_a_paid_config_says_how_to_replace_and_changes_nothing(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    before = _identity_on_disk(paid_config, fake_keychain)
    ident = make_identity(tmp_path / "new", password=NEW_PASSWORD)

    with pytest.raises(ConfigError, match=(
        "already set up for a paid account; add --replace to swap in a new certificate and profile"
    )) as err:
        account_setup.setup_account(
            paid_config, account="paid", p12_path=ident.p12, profile_path=ident.profile,
            password=_never_asked,
        )
    assert config.APP_ACCOUNT_SETTINGS in (err.value.hint or "")
    assert _identity_on_disk(paid_config, fake_keychain) == before


def test_replace_swaps_the_files_the_password_and_signing_and_keeps_the_rest(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    before_text = paid_config.read_text()
    ident = make_identity(tmp_path / "new", password=NEW_PASSWORD)
    seen, on_step = _steps()

    result = _replace(paid_config, ident, on_step=on_step)

    cfg_dir = paid_config.parent
    assert (cfg_dir / account_setup.P12_NAME).read_bytes() == ident.p12.read_bytes()
    assert (cfg_dir / account_setup.PROFILE_NAME).read_bytes() == ident.profile.read_bytes()
    assert fake_keychain.items == {("graft", f"p12-{TEAM}"): NEW_PASSWORD}
    raw = yaml.safe_load(paid_config.read_text())
    assert raw["signing"]["team_id"] == TEAM
    assert Path(raw["signing"]["cert"]) == cfg_dir.absolute() / account_setup.P12_NAME
    assert _outside_signing(paid_config.read_text()) == _outside_signing(before_text)
    # Replaced, not left beside: one of each, and no temporary behind.
    assert sorted(p.name for p in cfg_dir.iterdir() if p.suffix in (".p12", ".mobileprovision", ".tmp")) == [
        account_setup.P12_NAME, account_setup.PROFILE_NAME,
    ]
    assert result.change == account_setup.REPLACED
    assert result.fields()["previous_team_id"] == TEAM
    assert seen == ["profile", "certificate", "keychain", "files", "config"]
    assert NEW_PASSWORD not in paid_config.read_text()


def test_replace_with_another_team_keeps_the_old_teams_item_and_says_so(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """`p12-<team>` is keyed by team alone: another config, or the 1.x CLI,
    may read the old team's item, so it stays, and the result says so."""
    ident = make_identity(tmp_path / "new", team=OTHER_TEAM, password=NEW_PASSWORD)

    result = _replace(paid_config, ident)

    assert fake_keychain.items == {
        ("graft", f"p12-{OTHER_TEAM}"): NEW_PASSWORD, ("graft", f"p12-{TEAM}"): PASSWORD,
    }
    assert not any(c[1] == "delete-generic-password" for c in fake_keychain.calls)
    assert yaml.safe_load(paid_config.read_text())["signing"]["team_id"] == OTHER_TEAM
    fields = result.fields()
    assert (fields["team_id"], fields["previous_team_id"]) == (OTHER_TEAM, TEAM)
    assert TEAM in result.summary and OTHER_TEAM in result.summary
    assert any(f"p12-{TEAM}" in w and "stays in the Keychain" in w for w in fields["warnings"])
    # Combined fix check #20: the LiveContainer on the phone is the old team's.
    assert any(f"com.kdt.livecontainer.{TEAM}" in w and "--lc-install" in w
               and f"installs team {OTHER_TEAM}'s" in w for w in fields["warnings"]), fields["warnings"]


def test_a_team_changing_replace_warns_about_the_app_store_connect_key(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """The key stays (it is the user's), but it belongs to the old team."""
    config.set_app_store_connect(paid_config, {
        "key_id": "KEY1234567", "issuer_id": "00000000-0000-0000-0000-000000000000",
        "key_file": str(tmp_path / "AuthKey.p8"),
    })
    ident = make_identity(tmp_path / "new", team=OTHER_TEAM, password=NEW_PASSWORD)

    dry = _replace(paid_config, ident, dry_run=True)
    result = _replace(paid_config, ident)

    said = (f"the App Store Connect key is for team {TEAM}; automatic profiles will be "
            f"refused until you set a key for team {OTHER_TEAM}.")
    assert said in dry.fields()["warnings"], "the review screen says it before"
    assert said in result.fields()["warnings"]
    assert yaml.safe_load(paid_config.read_text())["app_store_connect"]["key_id"] == "KEY1234567"


def test_a_same_team_replace_says_nothing_about_the_key(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    config.set_app_store_connect(paid_config, {
        "key_id": "KEY1234567", "issuer_id": "00000000-0000-0000-0000-000000000000",
        "key_file": str(tmp_path / "AuthKey.p8"),
    })
    result = _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    assert not any("App Store Connect" in w for w in result.warnings)
    assert not any("LiveContainer" in w for w in result.warnings), "same team, same LiveContainer"


def test_a_replace_dry_run_checks_and_writes_nothing(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    before = _identity_on_disk(paid_config, fake_keychain)
    seen, on_step = _steps()

    result = _replace(
        paid_config, make_identity(tmp_path / "new", team=OTHER_TEAM, password=NEW_PASSWORD),
        dry_run=True, on_step=on_step,
    )

    assert (result.change, result.dry_run) == (account_setup.REPLACED, True)
    assert result.fields()["previous_team_id"] == TEAM
    assert seen == ["profile", "certificate", "config"]
    assert _identity_on_disk(paid_config, fake_keychain) == before


def test_a_keychain_item_that_cannot_be_read_back_is_not_overwritten(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """Nothing could put it back, so a replace that would overwrite it stops first."""
    before = _identity_on_disk(paid_config, fake_keychain)
    fake_keychain.find_status["graft"] = 51  # what a denied Keychain prompt returns

    with pytest.raises(GraftError, match="couldn't read"):
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    assert _identity_on_disk(paid_config, fake_keychain) == before


@pytest.mark.parametrize(("account", "free_config", "message"), [
    ("free", False, "--replace is only for --account paid"),
    ("paid", True, "--replace swaps a paid account's certificate and profile"),
    ("paid", False, "--replace swaps a paid account's certificate and profile"),
])
def test_replace_is_refused_where_there_is_nothing_to_replace(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    account: str, free_config: bool, message: str,
) -> None:
    if free_config:
        write_default_config(cfg_path, free_account=True)
    ident = make_identity(tmp_path)
    paid = account == "paid"

    with pytest.raises(ConfigError, match=message):
        account_setup.setup_account(
            cfg_path, account=account, replace=True, password=_never_asked,
            p12_path=ident.p12 if paid else None, profile_path=ident.profile if paid else None,
        )
    assert fake_keychain.items == {}


# --- every paid write is undone at every failure point (2.0 review F1, F2, F5) ------


def _snapshot(cfg_path: Path, fake: FakeSecurity, outside: tuple[Path, ...] = ()) -> tuple:
    """The config dir's files and links (not its folders or the config lock),
    the bytes of `outside` files, and the Keychain."""
    entries: dict[str, tuple] = {}
    if cfg_path.parent.exists():
        for entry in sorted(cfg_path.parent.iterdir()):
            if entry.name.endswith(".lock"):
                continue
            if entry.is_symlink():
                entries[entry.name] = ("link", os.readlink(entry))
            elif entry.is_file():
                entries[entry.name] = ("file", entry.read_bytes(), stat.S_IMODE(entry.stat().st_mode))
    return entries, {str(p): p.read_bytes() for p in outside}, dict(fake.items)


def _free_after_paid(cfg_path: Path, tmp_path: Path) -> None:
    """A paid account switched to free: Switch to a free Apple ID keeps the
    copies and the Keychain item, as its confirmation says."""
    _paid(cfg_path, make_identity(tmp_path / "old"))
    add_app(cfg_path, "example", {"bundle_id": "com.example.app"})
    account_setup.setup_account(cfg_path, account="free")


#: Each kind of paid write: how to set up what is there before, the team the
#: new pair is for, and whether it is a replace.
_KINDS = ["create", "upgrade", "upgrade over what free kept", "replace",
          "replace for another team", "replace symlinked copies", "replace copies kept elsewhere"]


def _arrange(kind: str, cfg_path: Path, tmp_path: Path) -> tuple[tuple[Path, ...], str, bool]:
    """Set up `kind`'s starting point; `(outside files, new team, replace?)`."""
    outside: tuple[Path, ...] = ()
    if kind == "upgrade":
        write_default_config(cfg_path, free_account=True)
        add_app(cfg_path, "example", {"bundle_id": "com.example.app"})
    elif kind == "upgrade over what free kept":
        _free_after_paid(cfg_path, tmp_path)
    elif kind.startswith("replace"):
        _paid(cfg_path, make_identity(tmp_path / "old"))
        add_app(cfg_path, "example", {"bundle_id": "com.example.app", "delivery": "livecontainer"})
        cfg_dir = cfg_path.parent
        if kind == "replace symlinked copies":
            synced = tmp_path / "synced"
            synced.mkdir()
            for name in (account_setup.P12_NAME, account_setup.PROFILE_NAME):
                (synced / name).write_bytes((cfg_dir / name).read_bytes())
                (cfg_dir / name).unlink()
                (cfg_dir / name).symlink_to(synced / name)
            outside = tuple(synced / n for n in (account_setup.P12_NAME, account_setup.PROFILE_NAME))
        elif kind == "replace copies kept elsewhere":
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            for name in (account_setup.P12_NAME, account_setup.PROFILE_NAME):
                (cfg_dir / name).rename(elsewhere / name)
            set_settings(cfg_path, signing_cert=str(elsewhere / account_setup.P12_NAME),
                         signing_profile=str(elsewhere / account_setup.PROFILE_NAME))
            outside = tuple(elsewhere / n for n in (account_setup.P12_NAME, account_setup.PROFILE_NAME))
    team = OTHER_TEAM if kind == "replace for another team" else TEAM
    return outside, team, kind.startswith("replace")


_WRITER = {"create": "write_default_config", "upgrade": "add_signing_section",
           "upgrade over what free kept": "add_signing_section"}


def _fail_at(point: str, kind: str, monkeypatch: pytest.MonkeyPatch, fake: FakeSecurity):
    """Make `point` fail; returns `(on_step, exception class expected)`."""
    real_stage, real_rename = account_setup.private_stage, os.rename
    stages = []

    def stage(data, dest):
        stages.append(dest.name)
        if (point, len(stages)) in (("the .p12 write", 1), ("the profile write", 2)):
            raise OSError(28, "No space left on device")
        return real_stage(data, dest)

    monkeypatch.setattr(account_setup, "private_stage", stage)
    if point == "the profile swap":
        def rename(src, dst, *a, **k):
            if Path(dst).name == account_setup.PROFILE_NAME and ".tmp" in Path(src).name:
                raise OSError(13, "Permission denied")
            return real_rename(src, dst, *a, **k)
        monkeypatch.setattr(account_setup.os, "rename", rename)
    if point == "the Keychain step":
        fake.fail_writes = True
    if point == "a Keychain that wrote and complained":
        fake.write_stderr_on_success = b"add-generic-password: returned -25308\n"
    if point in ("the config write", "a Ctrl-C at the config write"):
        writer = _WRITER.get(kind, "replace_signing")
        real = getattr(account_setup, writer)
        boom = (KeyboardInterrupt() if point.startswith("a Ctrl-C")
                else OSError(28, "No space left on device"))

        def write(*a, **k):
            if k.get("write", True):
                raise boom
            return real(*a, **k)
        monkeypatch.setattr(account_setup, writer, write)
    if point == "the re-open":
        from graft_ipa import signer
        monkeypatch.setattr(signer, "p12_password_for", lambda env, team: "not it")
    expected = {
        "the Keychain step": GraftError, "a Keychain that wrote and complained": GraftError,
        "the re-open": GraftError, "a reader that hung up at the files": BrokenPipeError,
        "a reader that hung up at the last step": BrokenPipeError,
        "a Ctrl-C at the config write": KeyboardInterrupt,
    }.get(point, OSError)

    def on_step(step, detail):
        if point == "a reader that hung up at the files" and step == "files":
            raise BrokenPipeError(32, "Broken pipe")
        if point == "a reader that hung up at the last step" and step == "config":
            raise BrokenPipeError(32, "Broken pipe")
    return on_step, expected


_POINTS = ["the .p12 write", "the profile write", "the Keychain step",
           "a Keychain that wrote and complained", "the profile swap", "the config write",
           "the re-open", "a reader that hung up at the files",
           "a reader that hung up at the last step", "a Ctrl-C at the config write"]


@pytest.mark.parametrize("point", _POINTS)
@pytest.mark.parametrize("kind", _KINDS)
def test_a_paid_write_that_fails_anywhere_leaves_the_account_as_it_was(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    monkeypatch: pytest.MonkeyPatch, kind: str, point: str,
) -> None:
    """The copies, their links, config.yaml and the Keychain item, exactly as
    they were, and nothing left beside them (a staged copy, a kept-aside old
    one), whichever step fails."""
    outside, team, replace = _arrange(kind, cfg_path, tmp_path)
    before = _snapshot(cfg_path, fake_keychain, outside)
    new = make_identity(tmp_path / "new", team=team, password=NEW_PASSWORD)
    on_step, expected = _fail_at(point, kind, monkeypatch, fake_keychain)

    with pytest.raises(expected):
        account_setup.setup_account(
            cfg_path, account="paid", p12_path=new.p12, profile_path=new.profile,
            password=lambda: NEW_PASSWORD, replace=replace, on_step=on_step,
        )

    assert _snapshot(cfg_path, fake_keychain, outside) == before


@pytest.mark.parametrize("kind", _KINDS)
def test_every_kind_of_paid_write_lands_whole_and_leaves_nothing_beside(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, kind: str
) -> None:
    outside, team, replace = _arrange(kind, cfg_path, tmp_path)
    kept_outside = {p: p.read_bytes() for p in outside}
    new = make_identity(tmp_path / "new", team=team, password=NEW_PASSWORD)

    account_setup.setup_account(
        cfg_path, account="paid", p12_path=new.p12, profile_path=new.profile,
        password=lambda: NEW_PASSWORD, replace=replace,
    )

    cfg_dir = cfg_path.parent
    for name, src in ((account_setup.P12_NAME, new.p12), (account_setup.PROFILE_NAME, new.profile)):
        assert not (cfg_dir / name).is_symlink(), name
        assert (cfg_dir / name).read_bytes() == src.read_bytes(), name
    assert fake_keychain.items[("graft", f"p12-{team}")] == NEW_PASSWORD
    loaded = load_config(cfg_path)
    assert loaded.signing is not None and loaded.signing.cert == cfg_dir.absolute() / account_setup.P12_NAME
    assert [p.name for p in cfg_dir.iterdir() if "graft-old" in p.name or p.suffix == ".tmp"] == []
    # A link's target is the user's own file: never written through.
    assert {p: p.read_bytes() for p in outside} == kept_outside


def test_a_second_stop_during_the_undo_waits_for_it(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM is held while the undo runs: one that arrives during an undo
    step is delivered only once every piece is back, so a second Stop can't
    cut the undo short."""
    before = _snapshot(paid_config, fake_keychain)
    order: list[str] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: order.append("signal"))
    real_write = keychain.write_p12_password

    def write(team, pw):
        if pw == PASSWORD:  # the undo putting the old password back
            os.kill(os.getpid(), signal.SIGTERM)
            order.append("restored")
        return real_write(team, pw)

    monkeypatch.setattr(keychain, "write_p12_password", write)
    monkeypatch.setattr(account_setup, "replace_signing",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    try:
        with pytest.raises(OSError):
            _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert order == ["restored", "signal"]
    assert _snapshot(paid_config, fake_keychain) == before


def test_a_stop_mid_setup_rolls_it_back(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """The app's Stop, or its quitting, sends SIGTERM; `--init` turns it into
    `Stopped` (`lc_manage.exit_on_termination`), which the undo catches."""
    from graft_ipa import lc_manage
    from graft_ipa.errors import Stopped

    def on_step(step, detail):
        if step == "files":
            os.kill(os.getpid(), signal.SIGTERM)

    with lc_manage.exit_on_termination(), pytest.raises(Stopped):
        _paid(cfg_path, make_identity(tmp_path), on_step=on_step)
    assert _snapshot(cfg_path, fake_keychain) == ({}, {}, {})


def test_an_undo_that_cannot_finish_says_what_it_left(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never silent (F1): the caller hears which pieces didn't go back."""
    monkeypatch.setattr(account_setup, "replace_signing",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    real_write = keychain.write_p12_password
    calls = []

    def write(team, pw):
        calls.append(pw)
        if len(calls) > 1:  # the restore
            raise OSError(5, "Input/output error")
        return real_write(team, pw)

    monkeypatch.setattr(keychain, "write_p12_password", write)
    with pytest.raises(GraftError, match=rf"the Keychain item p12-{TEAM} .*couldn't be put back"):
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))


# --- GRAFT_P12_PASS, which builds read before the Keychain (2.0 review F3) ----------


@pytest.mark.parametrize("replace", [False, True], ids=["create", "replace"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry run"])
def test_a_stale_graft_p12_pass_is_refused_before_anything_is_written(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    monkeypatch: pytest.MonkeyPatch, replace: bool, dry_run: bool,
) -> None:
    if replace:
        _paid(cfg_path, make_identity(tmp_path / "old"))
    before = _snapshot(cfg_path, fake_keychain)
    monkeypatch.setenv("GRAFT_P12_PASS", PASSWORD)
    new = make_identity(tmp_path / "new", password=NEW_PASSWORD)

    with pytest.raises(ConfigError) as err:
        account_setup.setup_account(
            cfg_path, account="paid", p12_path=new.p12, profile_path=new.profile,
            password=lambda: NEW_PASSWORD, replace=replace, dry_run=dry_run,
        )
    assert err.value.message == (
        "GRAFT_P12_PASS is set in your shell and overrides the Keychain, so builds would use "
        "the old password. Remove it from your shell profile, or set it to the new password."
    )
    assert _snapshot(cfg_path, fake_keychain) == before
    assert NEW_PASSWORD not in str(err.value) and PASSWORD not in str(err.value)


def test_a_graft_p12_pass_that_holds_the_new_password_is_a_warning(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_P12_PASS", PASSWORD)

    result = _paid(cfg_path, make_identity(tmp_path))

    assert any("GRAFT_P12_PASS is set in your shell and overrides the Keychain" in w
               for w in result.fields()["warnings"])


def test_the_config_named_variable_is_the_one_checked(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(paid_config.read_text())
    raw["signing"]["cert_password_env"] = "MY_P12_PASSWORD"
    paid_config.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("MY_P12_PASSWORD", "something old")

    with pytest.raises(ConfigError, match=r"^MY_P12_PASSWORD is set in your shell"):
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))


def test_the_installed_p12_is_reopened_with_the_password_a_build_uses(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After every write, the .p12 config.yaml names is opened the way
    `signer` will open it; a run that a build would fail on is rolled back."""
    from graft_ipa import signer

    asked: list[tuple[str, str | None]] = []
    real = signer.p12_password_for

    def resolve(env, team):
        asked.append((env, team))
        return real(env, team)

    monkeypatch.setattr(signer, "p12_password_for", resolve)
    _paid(cfg_path, make_identity(tmp_path))
    assert asked == [("GRAFT_P12_PASS", TEAM)]


# --- the config race (2.0 review F6, F8) ---------------------------------------------


@pytest.mark.parametrize("account", ["free", "paid"])
def test_a_config_that_appears_while_the_setup_runs_is_not_overwritten(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another window's setup, or a Terminal `--init`, made a config after
    this one looked: it is kept, and this run takes back what it wrote."""
    ident = make_identity(tmp_path)

    def meanwhile() -> None:
        write_default_config(cfg_path, free_account=True)
        add_app(cfg_path, "example", {"bundle_id": "com.example.app"})

    if account == "paid":
        def password() -> str:
            meanwhile()
            return PASSWORD
        kw = {"p12_path": ident.p12, "profile_path": ident.profile, "password": password}
    else:
        real_state = account_setup.config_state

        def state(path):
            answer = real_state(path)
            meanwhile()
            return answer
        monkeypatch.setattr(account_setup, "config_state", state)
        kw = {}

    with pytest.raises(ConfigError, match="appeared while the setup ran"):
        account_setup.setup_account(cfg_path, account=account, **kw)
    assert load_config(cfg_path).apps.keys() == {"example"}
    assert not (cfg_path.parent / account_setup.P12_NAME).exists()
    assert fake_keychain.items == {}


# --- a replace on a partly filled-in signing: section --------------------------------


def test_a_replace_completes_a_signing_section_missing_its_password_variable(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """A 1.x config that relied on the Keychain alone: `load_config` refuses
    it, and the replace is the one repair the app offers."""
    raw = yaml.safe_load(paid_config.read_text())
    del raw["signing"]["cert_password_env"]
    paid_config.write_text(yaml.safe_dump(raw))
    seen, on_step = [], lambda step, detail: seen.append(detail)

    _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD), on_step=on_step)

    assert load_config(paid_config).signing.cert_password_env == "GRAFT_P12_PASS"
    assert any("had no cert_password_env" in d for d in seen)


# --- the long-password cap (2.0 review SEC-1, F2, SEC-3) ------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_password_over_the_cap_is_refused_before_the_p12_is_opened(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity,
    monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    secret = "LONG-PASSWORD-PIECE-" * 52  # 1040 bytes
    opened = []
    monkeypatch.setattr(account_setup, "read_identity", lambda *a: opened.append(a))

    with pytest.raises(ConfigError) as err:
        _paid(cfg_path, make_identity(tmp_path), password=secret, dry_run=dry_run)
    assert err.value.message == (
        "a .p12 password longer than 1024 bytes isn't supported (in UTF-8, so a character "
        "outside ASCII counts 2 to 4, and a \\ or \" counts 2); export the certificate again "
        "with a shorter one."
    )
    assert opened == [] and fake_keychain.calls == []
    assert "LONG-PASS" not in str(err.value)


def test_a_password_at_the_cap_is_set_up(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    secret = "x" * keychain.MAX_SECRET_BYTES
    _paid(cfg_path, make_identity(tmp_path, password=secret), password=secret)
    assert fake_keychain.items[("graft", f"p12-{TEAM}")] == secret


def test_what_security_says_never_carries_the_password(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """Even if `security` echoes a piece of the command (`unknown command
    "<…>"`), the error the app shows withholds it (SEC-3)."""
    secret = "Tr0ub4dor&3-LAST-PART-OF-THE-SECRET"
    fake_keychain.write_stderr_on_success = (
        f'security: unknown command "{secret[-20:]}"\n{secret[-9:]}": returned 1\n'.encode()
    )
    with pytest.raises(GraftError) as err:
        _paid(cfg_path, make_identity(tmp_path, password=secret), password=secret)
    said = f"{err.value.message} {err.value.hint}"
    assert "couldn't store the .p12 password" in said
    pieces = {secret[i:i + 6] for i in range(len(secret) - 5)}
    assert not [p for p in pieces if p in said], said


# --- what read_identity refuses before anything is written ---------------------------


@pytest.mark.parametrize(("valid", "said"), [
    ((timedelta(days=-400), timedelta(days=-1)), r"the certificate in Certificates\.p12 expired on \d{4}-"),
    ((timedelta(days=30), timedelta(days=400)), r"isn't valid until \d{4}-"),
], ids=["expired", "not yet valid"])
def test_a_certificate_outside_its_dates_is_refused(
    tmp_path: Path, valid, said: str
) -> None:
    ident = make_identity(tmp_path, valid=valid)
    with pytest.raises(ConfigError, match=said):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)


@pytest.mark.parametrize(("extra", "said"), [
    ({"ProvisionedDevices": [b"\x00\x01"]}, "ProvisionedDevices isn't a list of device IDs"),
    ({"ProvisionedDevices": [datetime(2026, 1, 1)]}, "ProvisionedDevices isn't a list of device IDs"),
    ({"ProvisionedDevices": 5}, "ProvisionedDevices isn't a list of device IDs"),
    # XML can't hold an ESC at all; a bidirectional override it can.
    ({"ProvisionedDevices": ["00008130-001A2B3C0123456D\u202e"]}, "ProvisionedDevices"),
    ({"ProvisionedDevices": [" 00008130-001A2B3C0123456D"]}, "ProvisionedDevices"),
    ({"TeamIdentifier": 5}, "TeamIdentifier isn't a list of team IDs"),
    ({"TeamIdentifier": [5]}, "TeamIdentifier isn't a list of team IDs"),
    ({"DeveloperCertificates": 5}, "DeveloperCertificates isn't a list of certificates"),
    ({"Name": 5}, "Name isn't text"),
    ({"TeamIdentifier": [f"{TEAM} "]}, "team ID isn't a valid Apple team ID"),
    ({"TeamIdentifier": ["", TEAM]}, "team ID isn't a valid Apple team ID"),
    ({"TeamIdentifier": [f"{TEAM}\n"]}, "team ID isn't a valid Apple team ID"),
    ({"ApplicationIdentifierPrefix": ["bad prefix"]}, "team ID isn't a valid Apple team ID"),
])
def test_a_profile_field_of_the_wrong_type_or_shape_is_refused(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, extra: dict, said: str
) -> None:
    """Refused before anything is written, rather than crashing the
    `init-result` emit after (F7, F5), or storing a value its readers then
    see differently (F4)."""
    with pytest.raises(ConfigError, match=said):
        _paid(cfg_path, make_identity(tmp_path, extra=extra))
    _nothing_written(cfg_path, fake_keychain)


def test_a_certificate_name_cannot_reach_the_terminal_as_escapes(tmp_path: Path) -> None:
    ident = make_identity(tmp_path, common_name="Apple Development: X\x1bc\x1bPtmux;x\x1b\\‮\x07")
    got = account_setup.read_identity(ident.p12, ident.profile, PASSWORD)
    assert got.certificate == "Apple Development: Xc" + "Ptmux;x\\"
    assert not any(ord(c) < 0x20 or c == "‮" for c in got.certificate)


def test_a_link_planted_after_the_path_was_resolved_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`O_NOFOLLOW`: a link swapped in between `realpath` and `open` is
    refused, not followed (F10). Resolving is made a no-op so the link is
    still there when the file is opened."""
    ident = make_identity(tmp_path)
    link = tmp_path / "swapped.p12"
    link.symlink_to(ident.p12)
    monkeypatch.setattr(account_setup.os.path, "realpath", lambda p: str(p))

    with pytest.raises(ConfigError, match="is a symbolic link that doesn't lead to a file"):
        account_setup.read_chosen_file(link, ".p12")


# --- real-world .p12 formats, and what they ask of the key derivation (F3) ----------


def _security_export(key, cert, password: str) -> bytes:
    """`security export -f pkcs12` from a throwaway keychain: Keychain
    Access's own format. Opt-in (`GRAFT_REAL_KEYCHAIN_TESTS=1`), like every
    test that runs the real `security`. Never the login keychain: every call
    names the scratch one, which is unlocked with its known password, set
    never to lock, imported into with `-A` (so the export asks nothing), and
    deleted at the end. It is never added to the search list."""
    import secrets as secrets_mod
    import subprocess
    import tempfile

    from .signing_fixtures import openssl_p12 as maker

    with tempfile.TemporaryDirectory() as work:
        kc = str(Path(work) / "export.keychain")
        legacy = Path(work) / "in.p12"
        legacy.write_bytes(maker(LIBRESSL)(key, cert, "in"))

        def security(*args):
            assert not {"default-keychain", "list-keychains", "login-keychain"} & set(args)
            return subprocess.run(["/usr/bin/security", *args], check=True,
                                  capture_output=True, timeout=60)

        kc_password = secrets_mod.token_hex(16)
        try:
            security("create-keychain", "-p", kc_password, kc)
            security("unlock-keychain", "-p", kc_password, kc)
            security("set-keychain-settings", kc)  # no -l, no -u: it never locks
            security("import", str(legacy), "-k", kc, "-P", "in", "-A")
            out = Path(work) / "out.p12"
            security("export", "-k", kc, "-t", "identities", "-f", "pkcs12",
                     "-P", password, "-o", str(out))
            return out.read_bytes()
        finally:
            subprocess.run(["/usr/bin/security", "delete-keychain", kc],
                           capture_output=True, timeout=60)


_FORMATS = {
    "OpenSSL 3 -legacy (RC2-40, 3DES)": (OPENSSL3, lambda: openssl_p12(OPENSSL3, "-legacy")),
    "OpenSSL 3 default (AES-256, PBKDF2)": (OPENSSL3, lambda: openssl_p12(OPENSSL3)),
    "LibreSSL default (RC2-40, 3DES)": (LIBRESSL, lambda: openssl_p12(LIBRESSL)),
    "security export (Keychain Access)": (
        LIBRESSL if os.access("/usr/bin/security", os.X_OK)
        and os.environ.get("GRAFT_REAL_KEYCHAIN_TESTS") == "1" else None,
        lambda: _security_export,
    ),
}


@pytest.mark.parametrize(
    "fmt",
    # The Keychain Access case runs the real `security` (opt-in): marked, so
    # the program guard lets it, and only it, with GRAFT_REAL_KEYCHAIN_TESTS=1.
    [pytest.param(f, marks=pytest.mark.real_keychain) if "security" in f else f for f in _FORMATS],
)
def test_every_real_world_p12_format_reads(tmp_path: Path, fmt: str) -> None:
    needs, maker = _FORMATS[fmt]
    if needs is None:
        pytest.skip(f"{fmt}: needs a tool this Mac doesn't have, or is opt-in "
                    "(GRAFT_REAL_KEYCHAIN_TESTS=1)")
    ident = make_identity(tmp_path, rsa_key=True, p12_maker=maker())

    got = account_setup.read_identity(ident.p12, ident.profile, PASSWORD)

    assert got.certificate == COMMON_NAME and got.team_id == TEAM


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    size = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes(
        (n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + size + body


def _elements(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """Each element in `data[start:end]`: `(its bytes, content start, content end)`."""
    from graft_ipa import p12 as p12_mod

    out, i = [], start
    while i < end:
        _, cs, ce, after = p12_mod._tlv(data, i, end)
        out.append((data[i:after], cs, ce))
        i = after
    return out


def _with_mac_iterations(data: bytes, count: int) -> bytes:
    """`data` re-encoded with its MacData iteration count set to `count`: the
    review's own crafted file (the MAC itself no longer matches, which
    OpenSSL only finds out after spending `count` rounds on it)."""
    (root,) = _elements(data, 0, len(data))
    version, auth, mac = _elements(data, root[1], root[2])
    digest, salt, _ = _elements(data, mac[1], mac[2])
    body = count.to_bytes((count.bit_length() + 8) // 8, "big")
    return _der(0x30, version[0] + auth[0] + _der(0x30, digest[0] + salt[0] + _der(0x02, body)))


@pytest.mark.parametrize("how", ["openssl -iter", "a patched MAC count"])
def test_a_p12_that_asks_for_absurd_key_derivation_is_refused_at_once(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, how: str
) -> None:
    import time

    from graft_ipa import p12 as p12_mod

    if how == "openssl -iter":
        if OPENSSL3 is None:
            pytest.skip("needs OpenSSL 3 for -iter")
        ident = make_identity(tmp_path, rsa_key=True,
                              p12_maker=openssl_p12(OPENSSL3, "-iter", "1000000"))
    else:
        ident = make_identity(tmp_path)
        ident.p12.write_bytes(_with_mac_iterations(ident.p12.read_bytes(), 0x7FFFFFFF))
        assert p12_mod.kdf_iterations(ident.p12.read_bytes()) > 0x7FFFFFFF
    started = time.monotonic()

    with pytest.raises(ConfigError, match=r"asks for [\d,]+ rounds of key derivation"):
        _paid(cfg_path, ident)

    # In-process, 2^31 MAC rounds took about 100 s (the review's measurement).
    assert time.monotonic() - started < 3, "refused before the key derivation ran"
    _nothing_written(cfg_path, fake_keychain)


@pytest.mark.parametrize(("rounds", "refused"), [(998_000, False), (1_000_000, True)])
def test_the_kdf_cap_counts_the_mac_and_every_encryption(rounds: int, refused: bool) -> None:
    """cryptography's `kdf_rounds` sets the certificate SafeContents' and the
    key bag's counts, and leaves the MAC at 2048: 2 x rounds + 2048 in all,
    either side of the 2,000,000 cap."""
    from cryptography.hazmat.primitives.serialization import PrivateFormat, pkcs12

    from graft_ipa import p12 as p12_mod

    from .signing_fixtures import _certificate

    key, cert = _certificate(1)
    data = pkcs12.serialize_key_and_certificates(
        b"t", key, cert, None,
        PrivateFormat.PKCS12.encryption_builder().kdf_rounds(rounds).build(b"pw"),
    )
    assert p12_mod.kdf_iterations(data) == 2 * rounds + 2048
    if refused:
        with pytest.raises(p12_mod.CostlyP12Error):
            p12_mod.check_kdf_cost(data)
    else:
        p12_mod.check_kdf_cost(data)
    assert _with_mac_iterations(data, 5_000_000) != data
    assert p12_mod.kdf_iterations(_with_mac_iterations(data, 5_000_000)) == 2 * rounds + 5_000_000


@pytest.mark.parametrize("junk", [b"", b"not a p12", b"\x30\x03\x02\x01\x03"])
def test_a_p12_that_isnt_one_is_said_in_words(
    tmp_path: Path, junk: bytes
) -> None:
    ident = make_identity(tmp_path)
    ident.p12.write_bytes(junk)
    with pytest.raises(ConfigError, match=r"Certificates\.p12 isn't a \.p12 Graft can read"):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)


# --- a dry run of the upgrade writes nothing (2.0 review F2 of tests) ----------------


def test_an_upgrade_dry_run_on_a_free_config_writes_nothing(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """Add a paid account, then Continue, in Settings is this dry run (D1)."""
    write_default_config(cfg_path, free_account=True)
    add_app(cfg_path, "example", {"bundle_id": "com.example.app"})
    before = _snapshot(cfg_path, fake_keychain)

    result = _paid(cfg_path, make_identity(tmp_path), dry_run=True)

    assert (result.change, result.pinned) == (account_setup.UPGRADED, ("example",))
    assert _snapshot(cfg_path, fake_keychain) == before


def test_a_dry_run_refuses_a_keychain_item_it_could_not_put_back(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """The review screen refuses what the real run would (F12)."""
    fake_keychain.find_status["graft"] = 51
    with pytest.raises(GraftError, match="couldn't read the Keychain item"):
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD), dry_run=True)


def test_a_keychain_that_does_not_answer_says_how_to_unlock_it(
    paid_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def hangs(argv, **kwargs):
        assert kwargs.get("timeout") == keychain.TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(keychain, "_run", hangs)
    with pytest.raises(GraftError, match=re.escape(keychain.NO_ANSWER)):
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))


@pytest.mark.parametrize("damage", ["not utf-8", "a folder"])
def test_a_config_that_cannot_even_be_opened_is_unreadable_not_a_crash(
    cfg_path: Path, damage: str
) -> None:
    """config_state caught only ConfigError, so a non-UTF-8 file or a folder
    at config.yaml escaped as an unexplained error. Now it is said, with the
    path, and never replaced."""
    cfg_path.parent.mkdir(parents=True)
    if damage == "a folder":
        cfg_path.mkdir()
    else:
        cfg_path.write_bytes(b"apps:\n  caf\xe9: {}\n")
    assert account_setup.config_state(cfg_path) == account_setup.UNREADABLE
    with pytest.raises(ConfigError, match="can't be read, so the setup won't touch it") as err:
        account_setup.setup_account(cfg_path, account="free")
    assert str(cfg_path) in err.value.message
    with pytest.raises(ConfigError, match=str(cfg_path).replace("/", ".")):
        config.load_config(cfg_path)


@pytest.mark.parametrize("kind", ["folder", "fifo", "broken-link", "too-big"])
def test_a_chosen_files_refusal_never_carries_its_names_escape_sequences(tmp_path, kind):
    """Combined fix check #16: the steps went through printable(), but the
    refusals interpolated the raw path, and _fatal's escape() leaves ESC."""
    name = "cert\x1bc\x1b]0;pwned\x1b\\.p12"
    path = tmp_path / name
    if kind == "folder":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "broken-link":
        path.symlink_to(tmp_path / "gone\x1b[31m.p12")
    else:
        path.write_bytes(b"\0" * (account_setup.MAX_INPUT_BYTES + 1))
    with pytest.raises(ConfigError) as err:
        account_setup.read_chosen_file(path, ".p12")
    text = f"{err.value.message} {err.value.hint or ''}"
    assert "\x1b" not in text and "\x07" not in text, repr(text)
    assert "pwned" in text or kind == "broken-link", "the name is still shown, without its controls"


# --- the undo touches only what this run wrote (combined fix check #18) ---------------


def test_the_undo_leaves_a_file_another_writer_put_where_this_run_created_one(tmp_path: Path) -> None:
    """`created` remembers the inode: a second setup that moved this run's
    .p12 aside and installed its own keeps it when this run undoes."""
    dest = tmp_path / "AppleDevelopment.p12"
    dest.write_bytes(b"ours")
    work = account_setup._Change()
    work.created(dest)
    dest.unlink()
    dest.write_bytes(b"the other setup's")  # a new inode at the same name
    assert work.undo() == []
    assert dest.read_bytes() == b"the other setup's"


def test_the_undo_removes_the_file_this_run_created(tmp_path: Path) -> None:
    dest = tmp_path / "AppleDevelopment.p12"
    dest.write_bytes(b"ours")
    work = account_setup._Change()
    work.created(dest)
    assert work.undo() == [] and not dest.exists()


def test_without_hard_links_the_undo_puts_config_yaml_back_from_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some network and FAT volumes have no hard links: keep_config keeps
    the text instead, and a failure after the write still restores it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("apps: {}\n# the user's own config\n")

    def no_links(src, dst, *a, **k):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(account_setup.os, "link", no_links)
    work = account_setup._Change()
    work.keep_config(cfg)
    cfg.write_text("signing: {}\n")
    assert work.undo() == []
    assert cfg.read_text() == "apps: {}\n# the user's own config\n"
    # config.yaml.lock is the writers' lock (cli-3), not a kept copy.
    assert [p.name for p in tmp_path.iterdir() if p.suffix != ".lock"] == ["config.yaml"], (
        "no kept copy left behind")


@pytest.mark.parametrize("before", [None, "the old password"], ids=["no-item", "an-item"])
def test_the_keychain_undo_leaves_a_password_another_setup_stored_since(
    fake_keychain: FakeSecurity, before: str | None
) -> None:
    """Two setups for one team: B finished between A's write and A's
    failure. A deleting (or rewinding) the item left B with no password."""
    put_back = account_setup._put_keychain_back(TEAM, "A's password", before)
    keychain.write_p12_password(TEAM, "B's password")
    put_back()
    assert keychain.read_p12_password(TEAM) == "B's password"


@pytest.mark.parametrize("before", [None, "the old password"], ids=["no-item", "an-item"])
def test_the_keychain_undo_puts_back_what_was_there_while_it_holds_this_runs(
    fake_keychain: FakeSecurity, before: str | None
) -> None:
    if before is not None:
        keychain.write_p12_password(TEAM, before)
    put_back = account_setup._put_keychain_back(TEAM, "A's password", before)
    keychain.write_p12_password(TEAM, "A's password")
    put_back()
    assert keychain.read_p12_password(TEAM) == before


# --- app-2: a file macOS won't let Graft read says how to let it ----------------

import errno as _errno  # noqa: E402

PRIVACY = "allow Graft (or Terminal, if you ran graft there) in System Settings › Privacy & Security › Files and Folders"
#: The start of the hint for plain permissions (EACCES), which no privacy
#: setting changes (the try14 delta audit's d14-cli-5).
OWNER = "your user can't read"


def _refused_open(monkeypatch, target: Path, code: int) -> None:
    """`os.open` of `target` refused as TCC refuses a file in Downloads
    (EPERM), or plain permissions do (EACCES); any other path opens."""
    real = os.open

    def fake(path, *args, **kwargs):
        if os.path.realpath(path) == os.path.realpath(target):
            raise PermissionError(code, os.strerror(code), str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake)


def test_a_chosen_file_macos_wont_let_graft_read_names_the_setting(tmp_path, monkeypatch) -> None:
    """The app hands the CLI the path of a .p12 or profile the user picked;
    one in Downloads, Desktop or iCloud raises a privacy prompt, and Deny
    left an EPERM with no next step (the whole-diff review's app-2). Run
    from Terminal, macOS asks for Terminal, so that is named too."""
    chosen = tmp_path / "Downloads" / "dist.p12"
    chosen.parent.mkdir()
    chosen.write_bytes(b"p12")
    _refused_open(monkeypatch, chosen, _errno.EPERM)
    with pytest.raises(ConfigError) as e:
        account_setup.read_chosen_file(chosen, ".p12")
    assert e.value.hint == (
        f"macOS didn't let Graft read {chosen}: {PRIVACY}, or move the file out of "
        "Downloads/Desktop/Documents and choose it again"
    )


def test_a_chosen_file_your_user_cant_read_says_to_check_its_owner(tmp_path, monkeypatch) -> None:
    """The try14 delta audit's d14-cli-5: a .p12 copied with sudo is root's,
    mode 0600, and EACCES. The privacy setting and a move out of Downloads
    change neither."""
    chosen = tmp_path / "dist.p12"
    chosen.write_bytes(b"p12")
    _refused_open(monkeypatch, chosen, _errno.EACCES)
    with pytest.raises(ConfigError) as e:
        account_setup.read_chosen_file(chosen, ".p12")
    assert e.value.hint.startswith(f"{OWNER} {chosen}: it may belong to another user, root for one")
    assert "Get Info" in e.value.hint
    assert "Privacy & Security" not in e.value.hint


def test_a_chosen_file_with_no_read_permission_gets_the_hint_for_real(tmp_path) -> None:
    chosen = tmp_path / "profile.mobileprovision"
    chosen.write_bytes(b"profile")
    chosen.chmod(0)
    try:
        with pytest.raises(ConfigError) as e:
            account_setup.read_chosen_file(chosen, "provisioning profile")
    finally:
        chosen.chmod(0o600)
    assert (e.value.hint or "").startswith(OWNER), "mode 000 is EACCES: permissions, not privacy"


def test_another_open_failure_gets_no_privacy_hint(tmp_path, monkeypatch) -> None:
    chosen = tmp_path / "dist.p12"
    chosen.write_bytes(b"p12")
    _refused_open(monkeypatch, chosen, _errno.EIO)
    with pytest.raises(ConfigError) as e:
        account_setup.read_chosen_file(chosen, ".p12")
    assert PRIVACY not in (e.value.hint or "")


# --- cli-2: the Keychain undo can't read the item -------------------------------


def test_a_keychain_undo_that_cant_read_the_item_says_it_couldnt_put_it_back(
    fake_keychain: FakeSecurity,
) -> None:
    """`read_p12_password` answered None for a denied or locked read, which
    the undo took for "another writer's" and skipped, silently: the new
    password stayed while the old .p12 came back."""
    keychain.write_p12_password(TEAM, "A's password")
    work = account_setup._Change()
    work.on_undo(f"the Keychain item p12-{TEAM}", account_setup._put_keychain_back(TEAM, "A's password", "old"))
    fake_keychain.find_item_status[("graft", f"p12-{TEAM}")] = 51  # a denied Keychain prompt
    (failed,) = work.undo()
    assert failed.startswith(f"the Keychain item p12-{TEAM} (") and "couldn't read" in failed
    del fake_keychain.find_item_status[("graft", f"p12-{TEAM}")]
    assert keychain.find_p12_password(TEAM) == (False, "A's password"), "left as it was, and said"


def test_a_keychain_undo_never_copies_the_legacy_item(fake_keychain: FakeSecurity) -> None:
    """`read_p12_password` falls back to the pre-rename `tweak-build` item
    and copies it into a new `graft` one: an undo that only looks must
    not."""
    fake_keychain.items[("tweak-build", f"p12-{TEAM}")] = "1.x password"
    account_setup._put_keychain_back(TEAM, "A's password", None)()
    assert ("graft", f"p12-{TEAM}") not in fake_keychain.items
    assert [c for c in fake_keychain.calls if "tweak-build" in c] == []



# --- cli-3: the config undo writes under the writers' lock --------------------------


def _held_while(monkeypatch, owner, name: str, cfg: Path, when=lambda *a: True) -> list[bool]:
    """Patch `owner.name` to record, each time it is called for `cfg`,
    whether the config lock is held; it still does its work."""
    real = getattr(owner, name)
    seen: list[bool] = []

    def recording(*args, **kwargs):
        if when(*args):
            seen.append(config._config_lock_held(cfg))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, recording)
    return seen


def test_the_config_undo_puts_config_yaml_back_under_the_lock(tmp_path: Path, monkeypatch) -> None:
    """It ran after `_config_lock` was released, so its rename could clobber
    a writer that took the lock in between (the whole-diff review's cli-3)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("apps: {}\n")
    work = account_setup._Change()
    work.keep_config(cfg)
    config._atomic_write_text(cfg, "signing: {}\n")
    seen = _held_while(monkeypatch, account_setup.os, "replace", cfg,
                       when=lambda src, dst, *a: os.path.realpath(dst) == os.path.realpath(cfg))
    assert not config._config_lock_held(cfg), "premise: the undo runs after the write's lock"
    assert work.undo() == []
    assert seen == [True]
    assert cfg.read_text() == "apps: {}\n"


def test_without_hard_links_the_config_undo_writes_under_the_lock(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("apps: {}\n")
    monkeypatch.setattr(account_setup.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(45, "no")))
    work = account_setup._Change()
    work.keep_config(cfg)
    cfg.write_text("signing: {}\n")
    seen = _held_while(monkeypatch, config, "_atomic_write_text", cfg)
    assert work.undo() == []
    assert seen == [True]


def test_a_config_this_run_created_is_removed_under_the_lock(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch
) -> None:
    seen = _held_while(monkeypatch, account_setup, "_same_file", cfg_path,
                       when=lambda path, *a: os.path.realpath(path) == os.path.realpath(cfg_path))
    monkeypatch.setattr(account_setup, "_reopen", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    with pytest.raises(OSError):
        _paid(cfg_path, make_identity(tmp_path))
    assert seen == [True]
    assert not cfg_path.exists()


# --- cli-4 (#5 re-described): a Stop held through the undo keeps its report ----------


def test_a_stop_held_through_an_undo_that_couldnt_finish_keeps_the_report(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The undo holds SIGTERM, and the Stop raised as it unmasked, inside
    `undo()`: its list of what couldn't be put back went with it, and
    the process exited as stopped with nothing said. Now the undo finishes,
    the report rides on the Stop (`undo_incomplete`), and the Stop is
    raised."""
    from graft_ipa import lc_manage
    from graft_ipa.errors import Stopped

    monkeypatch.setattr(account_setup, "replace_signing",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    real_write = keychain.write_p12_password

    def write(team, pw):
        if pw == PASSWORD:  # the undo putting the old password back: a Stop, and a failure
            os.kill(os.getpid(), signal.SIGTERM)
            raise OSError(5, "Input/output error")
        return real_write(team, pw)

    monkeypatch.setattr(keychain, "write_p12_password", write)
    with lc_manage.exit_on_termination(), pytest.raises(Stopped) as e:
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    report = e.value.undo_incomplete
    assert isinstance(report, GraftError)
    assert re.search(rf"the Keychain item p12-{TEAM} .*couldn't be put back", report.message)
    assert report.hint


def test_a_stop_held_through_an_undo_that_finished_keeps_why_the_setup_failed(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything was put back, so there is no undo report. But the setup
    had failed for a reason of its own before the Stop, and the bare stop
    dropped it (the try14 delta audit's d14-cli-6): it rides on the Stop
    too (`setup_failed`)."""
    from graft_ipa import lc_manage
    from graft_ipa.errors import Stopped

    before = _snapshot(paid_config, fake_keychain)
    monkeypatch.setattr(account_setup, "replace_signing",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    real_write = keychain.write_p12_password

    def write(team, pw):
        if pw == PASSWORD:
            os.kill(os.getpid(), signal.SIGTERM)
        return real_write(team, pw)

    monkeypatch.setattr(keychain, "write_p12_password", write)
    with lc_manage.exit_on_termination(), pytest.raises(Stopped) as e:
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    assert getattr(e.value, "undo_incomplete", None) is None
    assert _snapshot(paid_config, fake_keychain) == before
    report = e.value.setup_failed
    assert isinstance(report, GraftError)
    assert report.message == "the setup failed ([Errno 28] full); everything it changed was put back"


def test_a_setup_that_was_stopped_and_then_stopped_again_says_no_failure(
    paid_config: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop is no failure (C-STOP): a second one held through the undo
    carries nothing to say."""
    from graft_ipa import lc_manage
    from graft_ipa.errors import Stopped

    monkeypatch.setattr(account_setup, "replace_signing",
                        lambda *a, **k: (_ for _ in ()).throw(Stopped(signal.SIGTERM)))
    real_write = keychain.write_p12_password

    def write(team, pw):
        if pw == PASSWORD:
            os.kill(os.getpid(), signal.SIGTERM)
        return real_write(team, pw)

    monkeypatch.setattr(keychain, "write_p12_password", write)
    with lc_manage.exit_on_termination(), pytest.raises(Stopped) as e:
        _replace(paid_config, make_identity(tmp_path / "new", password=NEW_PASSWORD))
    assert getattr(e.value, "setup_failed", None) is None
    assert getattr(e.value, "undo_incomplete", None) is None


@pytest.mark.parametrize("attribute", ["setup_failed", "undo_incomplete"])
def test_the_cli_says_a_held_stop_s_report_before_it_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], attribute: str,
) -> None:
    from graft_ipa import cli
    from graft_ipa.errors import Stopped

    def stopped(*_a, **_k):
        stop = Stopped(signal.SIGTERM)
        setattr(stop, attribute, GraftError("the setup failed (the .p12 doesn't open); everything it "
                                            "changed was put back", hint="check the password"))
        raise stop

    monkeypatch.setattr(account_setup, "setup_account", stopped)
    with pytest.raises(Stopped):
        cli._do_init_account(tmp_path / "config.yaml", account="paid", p12=None, profile=None,
                             dry_run=False, emitter=None)
    said = " ".join(capsys.readouterr().err.split())
    assert "the setup failed (the .p12 doesn't open); everything it changed was put back: check the password" in said


def test_security_run_while_signals_are_held_gets_them_unblocked(tmp_path, monkeypatch) -> None:
    """`security` children inherited the undo's blocked SIGTERM, SIGHUP and
    SIGINT, so a Stop couldn't end one. `keychain._run_security` itself,
    with `security` swapped by full path for a script that prints its
    blocked mask, and the suite's switch off for this call only."""
    import subprocess as sp
    import sys

    tool = tmp_path / "mask-reader"
    script = "import signal; print(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])))"
    tool.write_text(f"#!/bin/sh\nexec '{sys.executable}' -c '{script}'\n")
    tool.chmod(0o755)
    real = sp.run
    monkeypatch.delenv("GRAFT_NO_KEYCHAIN")
    monkeypatch.setattr(keychain.subprocess, "run", lambda argv, **kw: real([str(tool)], **kw))
    with account_setup._signals_held():
        proc = keychain._run_security(["security", "find-generic-password"], capture_output=True,
                                      text=True, timeout=30)
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []), "premise: held here"
    assert proc.stdout.strip() == "[]"
