"""`ipatool.py`: running the user's ipatool, and saying what its failures mean.

Against the fake in `appstore_fakes` — never the real binary, which would talk
to Apple with a real account.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa import ipatool, tools
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FAKE_EMAIL, FAKE_NAME, FakeTools


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


def test_the_version_is_read_off_its_own_banner(fakes: FakeTools) -> None:
    assert ipatool.version(fakes.ipatool) == "2.8.0"


def test_a_missing_binary_has_no_version(tmp_path: Path) -> None:
    assert ipatool.version(tmp_path / "nope") is None


def test_a_present_binary_is_present(fakes: FakeTools, tmp_path: Path) -> None:
    assert ipatool.present(fakes.ipatool) is True
    assert ipatool.present(tmp_path / "nope") is False
    not_executable = tmp_path / "plain"
    not_executable.write_text("x")
    assert ipatool.present(not_executable) is False


def test_the_account_carries_the_email_and_never_the_name(fakes: FakeTools) -> None:
    """Since 2.0 Settings shows who is signed in. The name is still never kept."""
    answer = ipatool.account(fakes.ipatool)

    assert answer == ipatool.Account(signed_in=True, email=FAKE_EMAIL)
    assert FAKE_NAME not in repr(answer)
    assert ipatool.signed_in(fakes.ipatool) is True


def test_a_signed_out_account_has_no_email(fakes: FakeTools) -> None:
    fakes.set(signed_in=False)
    assert ipatool.account(fakes.ipatool) == ipatool.Account(signed_in=False)


def _item_there(monkeypatch: pytest.MonkeyPatch) -> None:
    from graft_ipa import keychain
    from tests.keychain_fakes import FakeSecurity

    monkeypatch.setattr(
        keychain, "_run", FakeSecurity({("ipatool-auth.service", "account"): "someone else's"})
    )


#: K1: what ipatool said after Deny was pressed on the Keychain dialog.
DENIED = "failed to get account: failed to get item: The specified item could not be found in the keyring"


def test_not_found_with_the_item_there_is_a_refusal_not_signed_out(
    fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 10: a denied Keychain dialog is never "not signed in"."""
    from graft_ipa import appstore_account, tools

    _item_there(monkeypatch)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    appstore_account.mark_signed_in("bundled")
    fakes.set(signed_in=False, auth_raw='{"level":"error","success":false,"error":"' + DENIED + '"}')
    assert ipatool.account(fakes.ipatool) == ipatool.Account(signed_in=None, refused=True)
    assert not appstore_account.marker_path().exists(), "the next sign-in clears the items first"


def test_a_download_refused_by_the_keychain_never_says_not_found(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import tools

    _item_there(monkeypatch)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    err = _download_error(fakes, tmp_path, DENIED)
    assert err.message == ipatool.KEYCHAIN_REFUSED
    assert "could not be found" not in err.message
    assert "dialog was denied" in err.hint
    assert ipatool.LOGIN_COMMAND in err.hint


def test_a_refusal_of_any_other_ipatool_names_it_and_says_signing_in_saves_over(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """signin-B (the try14 delta audit): a configured or PATH ipatool refused
    an item isn't "Graft's ipatool", and its sign-in deletes nothing first
    (`appstore_account.needs_clearing`), so it saves over that item and macOS
    may ask. As status says (`appstore.status`, its `elif legacy:` branch)."""
    _item_there(monkeypatch)
    err = _download_error(fakes, tmp_path, DENIED)
    assert err.message == (
        f"macOS's Keychain didn't let the ipatool at {fakes.ipatool} read the App Store "
        "sign-in in your login keychain"
    )
    assert "Graft's ipatool" not in err.message + err.hint
    assert "Graft 1.x" not in err.hint
    assert "replaces it" not in err.hint
    assert "dialog was denied" in err.hint
    assert "saves over it" in err.hint
    assert "may ask for your login keychain password" in err.hint
    assert ipatool.LOGIN_COMMAND in err.hint
    assert isinstance(err, ipatool.KeychainRefusal) and err.refused


GRAFT_APP_SETTINGS = "Graft (Settings › App Store)"


def test_a_pipx_graft_s_ipatool_refused_is_told_to_sign_in_with_it_not_in_graft_app(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The try15 delta audit's d15-py-3: from a pipx `graft`, the ipatool
    on PATH or in ~/.local/bin isn't the one Graft.app signs in with.
    Graft.app's own deletes the item instead of saving over it, and this one
    still can't read the new sign-in. Only signing in again with it helps,
    as status says."""
    _item_there(monkeypatch)
    err = _download_error(fakes, tmp_path, DENIED)
    assert GRAFT_APP_SETTINGS not in err.hint
    assert f"Sign in again with it, in Terminal: {ipatool.LOGIN_COMMAND}; that saves over it" in err.hint
    assert "may ask for your login keychain password" in err.hint


@pytest.mark.parametrize(("source", "inside", "offers_graft_app"), [
    (tools.SOURCE_CONFIG, False, True),
    (tools.SOURCE_PATH, False, False),
    (tools.SOURCE_DEFAULT, False, False),
    (None, False, False),
    (None, True, True),
    (tools.SOURCE_CONFIG, True, True),
])
def test_graft_app_is_offered_only_where_it_signs_in_with_the_same_ipatool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source, inside: bool, offers_graft_app: bool,
) -> None:
    """config.yaml's ipatool is Graft.app's too; inside Graft.app, the only
    ipatool that isn't its own is config.yaml's."""
    helpers = tmp_path / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", (lambda: helpers) if inside else (lambda: None))
    _, hint = ipatool.keychain_refused_text(tmp_path / "elsewhere" / "ipatool", source)
    assert (GRAFT_APP_SETTINGS in hint) is offers_graft_app
    assert ipatool.LOGIN_COMMAND in hint and "saves over it" in hint


def test_signed_out_is_false(fakes: FakeTools) -> None:
    fakes.set(signed_in=False)

    assert ipatool.signed_in(fakes.ipatool) is False


def test_a_missing_binary_is_not_signed_in(tmp_path: Path) -> None:
    assert ipatool.signed_in(tmp_path / "nope") is False


def test_download_runs_the_spec_command_and_returns_the_file(
    fakes: FakeTools, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    got = ipatool.download(fakes.ipatool, "com.example.app", out)

    assert got == out / "com.example.app_100000002_1.0.ipa"
    assert got.exists()
    (call,) = fakes.calls("ipatool")
    assert call["argv"] == [
        "download", "-b", "com.example.app", "-o", str(out),
        "--format", "json", "--non-interactive",
    ]


def test_never_asks_ipatool_to_purchase(fakes: FakeTools, tmp_path: Path) -> None:
    """`download` never takes `--purchase`: getting a license is
    `--appstore-claim`'s job, which checks the price first. Never a side
    effect of a download."""
    out = tmp_path / "out"
    out.mkdir()
    ipatool.download(fakes.ipatool, "com.x", out)

    assert not any("purchase" in a for c in fakes.calls() for a in c["argv"])


def _download_error(fakes: FakeTools, tmp_path: Path, error: str) -> AppStoreError:
    fakes.set(download_error=error)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.example.app", out)
    return e.value


def test_not_signed_in_hints_the_login_command(fakes: FakeTools, tmp_path: Path) -> None:
    err = _download_error(
        fakes, tmp_path,
        "failed to get account: item could not be found in the keyring",
    )

    assert err.message.startswith("ipatool is not signed in")
    assert ipatool.LOGIN_COMMAND in err.hint
    assert "Settings › App Store" in err.hint


def test_an_expired_token_hints_the_login_command_too(fakes: FakeTools, tmp_path: Path) -> None:
    err = _download_error(fakes, tmp_path, "password token is expired")

    assert err.message.startswith("ipatool is not signed in")
    assert ipatool.LOGIN_COMMAND in err.hint


def test_not_owned_says_to_tap_get_on_a_device(fakes: FakeTools, tmp_path: Path) -> None:
    err = _download_error(fakes, tmp_path, "license is required")

    assert err.message == (
        "this Apple ID doesn't own com.example.app yet "
        "(ipatool: license is required)"
    )
    assert "tap Get" in err.hint
    assert ipatool.LOGIN_COMMAND not in err.hint


def test_2_8_1_is_told_to_pin_2_8_0(fakes: FakeTools, tmp_path: Path) -> None:
    """Whatever it failed with. The error here deliberately does not say
    "storefront", so this holds the version half of the rule on its own —
    with the storefront wording it passed with the version check deleted."""
    fakes.set(ipatool_version="2.8.1")
    err = _download_error(fakes, tmp_path, "request failed with status 404")

    assert "Install ipatool 2.8.0" in err.hint
    assert "#672" in err.hint


GENERIC_HINT = "that is ipatool's own error — running the same download in Terminal may say more"


@pytest.mark.parametrize("version", ["2.8.0", "9.9.9-dev"])
def test_a_storefront_failure_from_a_good_ipatool_gets_the_generic_hint(
    fakes: FakeTools, tmp_path: Path, monkeypatch, version: str
) -> None:
    """get-2: "storefront" alone used to mean 2.8.1's bug, and got "install
    ipatool 2.8.0 to <path>": with Graft.app's own ipatool, a path inside
    Graft.app, which breaks its seal, and for a 2.8.0 nothing to fix. Only a
    known-bad version gets install advice."""
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    fakes.set(ipatool_version=version)
    err = _download_error(fakes, tmp_path, "available storefronts request failed")

    assert err.hint == GENERIC_HINT
    assert "install" not in err.hint.lower() and "Graft.app" not in err.hint
    assert "available storefronts request failed" in err.message


def test_a_known_bad_bundled_ipatool_s_storefront_failure_isn_t_told_to_install_into_graft_app(
    fakes: FakeTools, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    fakes.set(ipatool_version="2.8.1")
    err = _download_error(fakes, tmp_path, "available storefronts request failed")

    assert err.hint == ipatool.bad_version_hint("2.8.1", path=fakes.ipatool, source=tools.SOURCE_BUNDLED)
    assert ipatool.OVERRIDE_PATH in err.hint
    assert str(fakes.root) not in err.hint


def test_an_unrecognised_error_is_passed_through_verbatim(
    fakes: FakeTools, tmp_path: Path
) -> None:
    err = _download_error(fakes, tmp_path, "something new and strange")

    assert err.message == "ipatool couldn't download com.example.app: something new and strange"
    assert "tap Get" not in err.hint
    assert ipatool.LOGIN_COMMAND not in err.hint


def test_an_output_outside_the_directory_it_was_given_is_refused(
    fakes: FakeTools, tmp_path: Path
) -> None:
    """Everything downstream deletes the temp dir it handed over and trusts
    that this removes the download with it. A file ipatool put somewhere else
    would outlive the run, carrying the account's license."""
    elsewhere = tmp_path / "elsewhere.ipa"
    fakes.set(download_output=str(elsewhere))
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.x", out)

    assert e.value.message.startswith("ipatool wrote the IPA outside")


def test_a_missing_binary_is_an_install_hint(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(AppStoreError) as e:
        ipatool.download(tmp_path / "nope", "com.x", out)

    assert e.value.message == f"ipatool not found at {tmp_path / 'nope'}"
    assert "2.8.0" in e.value.hint


# --- Unicorn: downloaded by ipatool on first use (the combined fix check's medium #2) ---

#: What ipatool 2.8.0 says when its first Unicorn download fails offline,
#: built from its own source's words: pkg/appstore wraps the Store
#: framework's error (internal/store/backend.go:221, "open Store framework"),
#: which wraps the engine's (internal/sap/unicorn/engine.go:96, "load
#: Unicorn"), the library's (library_unix.go:19, "load Unicorn library") and
#: the download's (cache.go:132, "download Unicorn 2.1.4"), around Go's own
#: dial error.
OFFLINE_UNICORN = (
    "failed to send http request: open Store framework: load Unicorn: load Unicorn library: "
    'download Unicorn 2.1.4: Get "https://files.pythonhosted.org/packages/6c/ae/'
    '4943c6f8524d729ec7d5e69df6407ea05d710fe77471d91cecf3fc64eb57/'
    'unicorn-2.1.4-cp37-abi3-macosx_11_0_arm64.whl": dial tcp: lookup files.pythonhosted.org: '
    "no such host"
)


def test_the_unicorn_words_are_ipatools_own() -> None:
    """The phrases matched are in the pinned source's error strings."""
    assert all(words in OFFLINE_UNICORN for words in ipatool.UNICORN_FAILURES)


def test_an_offline_first_get_says_ipatool_must_download_unicorn_first(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_error=OFFLINE_UNICORN)
    (tmp_path / "out").mkdir()
    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.example.app", tmp_path / "out")
    assert e.value.message == (
        "ipatool needs to download its App Store emulator (Unicorn, from PyPI) the first time; "
        "connect to the internet and try again."
    )
    assert e.value.hint == f"ipatool said: {OFFLINE_UNICORN}"


def test_an_offline_first_claim_says_the_same(fakes: FakeTools) -> None:
    fakes.set(purchase_error=OFFLINE_UNICORN.replace("failed to send http request", "failed to purchase item"))
    with pytest.raises(AppStoreError) as e:
        ipatool.purchase(fakes.ipatool, "com.example.app")
    assert e.value.message == ipatool.UNICORN_FIRST_USE


def test_a_failure_that_isnt_unicorns_keeps_its_own_words(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_error="failed to send http request: dial tcp: lookup p25-buy.itunes.apple.com: no such host")
    (tmp_path / "out").mkdir()
    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.example.app", tmp_path / "out")
    assert "Unicorn" not in e.value.message


#: How ipatool 2.8.0 words a restore image that is gone or changed: its
#: Store backend's wrapping (internal/store/backend.go:163) around the asset
#: loader's (internal/sap/assets/remote.go:37), around each failure below.
_LOAD = "failed to send http request: load Store runtime: resolve macOS 27.0.1 (26A434) frameworks: "
RESTORE_IMAGE_GONE = [
    # What Apple's S3 origin answered for a missing file on 2026-11-24.
    _LOAD + "open remote image: server returned 403 Forbidden without byte-range support",
    _LOAD + "open remote image: server returned 404 Not Found without byte-range support",
    _LOAD + "read remote image range: server returned 410 Gone",
    _LOAD + "open encrypted macOS filesystem: download Apple FCS key: server returned 404 Not Found",
    "failed to send http request: load Store runtime: macOS 27.0.1 (26A434) restore image does not "
    "contain 043-70867-640.dmg.aea",
    "failed to send http request: load Store runtime: macOS 27.0.1 (26A434) restore member "
    "kernelcache.release.mac13j does not match the pinned image",
    "failed to send http request: load Store runtime: apple SAP asset CoreFP.icxs failed integrity verification",
]
#: What trying again can fix: offline, a server error, a rate limit.
RESTORE_IMAGE_NOT_GONE = [
    _LOAD + 'open remote image: Get "https://updates.cdn-apple.com/x.ipsw": dial tcp: lookup '
            "updates.cdn-apple.com: no such host",
    _LOAD + "open remote image: server returned 503 Service Unavailable without byte-range support",
    _LOAD + "read remote image range: server returned 429 Too Many Requests",
    "failed to send http request: server returned 404 Not Found",
]


@pytest.mark.parametrize("error", RESTORE_IMAGE_GONE)
def test_a_gone_restore_image_says_a_newer_graft_fixes_it(fakes: FakeTools, tmp_path: Path, error: str) -> None:
    fakes.set(download_error=error)
    (tmp_path / "out").mkdir()
    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.example.app", tmp_path / "out")
    assert e.value.message == ipatool.RESTORE_IMAGE_GONE
    assert e.value.hint == f"ipatool said: {error}"
    message = " ".join(e.value.message.split())
    assert "A newer Graft will fix this" in message and "update notice" in message
    assert "Help › Check for Updates…" in message and "graft --check-update" in message


@pytest.mark.parametrize("error", RESTORE_IMAGE_GONE[:2])
def test_a_first_claim_with_the_restore_image_gone_says_the_same(fakes: FakeTools, error: str) -> None:
    fakes.set(purchase_error=error)
    with pytest.raises(AppStoreError) as e:
        ipatool.purchase(fakes.ipatool, "com.example.app")
    assert e.value.message == ipatool.RESTORE_IMAGE_GONE


@pytest.mark.parametrize("error", RESTORE_IMAGE_NOT_GONE)
def test_a_failure_trying_again_can_fix_is_not_called_a_gone_image(error: str) -> None:
    assert ipatool.restore_image_failure(error) is None
    assert ipatool.explain(error, bundle_id="com.example.app", path=Path("/x/ipatool"),
                           reported_version="2.8.0").message != ipatool.RESTORE_IMAGE_GONE


def test_graft_names_no_unicorn_to_any_ipatool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graft.app bundles no Unicorn and its ipatool is unpatched: nothing
    sets IPATOOL_UNICORN_LIBRARY, in the app's ipatool or another."""
    monkeypatch.delenv("IPATOOL_UNICORN_LIBRARY", raising=False)
    contents = tmp_path / "Graft.app" / "Contents"
    fakes = FakeTools(contents / "Helpers" / "bin")
    assert ipatool.version(fakes.ipatool) == "2.8.0"
    assert ipatool.signed_in(fakes.ipatool) is True
    assert [c["unicorn"] for c in fakes.calls("ipatool")] == [None, None]


def test_the_terminal_sign_in_line_is_graft_s_and_names_no_account() -> None:
    """Since 2.0 Graft signs in (`appstore_login`), and asks for the Apple ID:
    nothing of the account is ever on a command line (decision 3)."""
    assert ipatool.LOGIN_COMMAND == "graft --appstore-login"
    assert "@" not in ipatool.LOGIN_HINT and "EMAIL" not in ipatool.LOGIN_HINT
