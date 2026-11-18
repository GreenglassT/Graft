"""`ipatool.py`: running the user's ipatool, and saying what its failures mean.

Against the fake in `appstore_fakes` — never the real binary, which would talk
to Apple with a real account.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa import ipatool
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


def test_signed_in_is_a_bool_and_nothing_about_the_account(fakes: FakeTools) -> None:
    """The spec's rule: `signed_in: true/false` only. The fake's `auth info`
    prints an email and a name exactly as the real one does, and neither may
    come back out of this module in any form."""
    answer = ipatool.signed_in(fakes.ipatool)

    assert answer is True
    assert FAKE_EMAIL not in repr(answer) and FAKE_NAME not in repr(answer)


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
    """`--purchase` would spend the account's "Get" on the user's behalf, and
    fails for US accounts anyway (majd/ipatool#666). Owning the app is the
    user's step."""
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
    assert ipatool.login_command(fakes.ipatool) in err.hint


def test_an_expired_token_hints_the_login_command_too(fakes: FakeTools, tmp_path: Path) -> None:
    err = _download_error(fakes, tmp_path, "password token is expired")

    assert err.message.startswith("ipatool is not signed in")
    assert ipatool.login_command(fakes.ipatool) in err.hint


def test_not_owned_says_to_tap_get_on_a_device(fakes: FakeTools, tmp_path: Path) -> None:
    err = _download_error(fakes, tmp_path, "license is required")

    assert err.message == (
        "this Apple ID doesn't own com.example.app yet "
        "(ipatool: license is required)"
    )
    assert "tap Get" in err.hint
    assert "ipatool auth login" not in err.hint


def test_2_8_1_is_told_to_pin_2_8_0(fakes: FakeTools, tmp_path: Path) -> None:
    """Whatever it failed with. The error here deliberately does not say
    "storefront", so this holds the version half of the rule on its own —
    with the storefront wording it passed with the version check deleted."""
    fakes.set(ipatool_version="2.8.1")
    err = _download_error(fakes, tmp_path, "request failed with status 404")

    assert "Install ipatool 2.8.0" in err.hint
    assert "#672" in err.hint


def test_the_storefront_failure_is_recognised_on_its_own(
    fakes: FakeTools, tmp_path: Path
) -> None:
    """A version the banner reports oddly (a dev build, a fork) still gets the
    one hint that fixes this error."""
    fakes.set(ipatool_version="9.9.9-dev")
    err = _download_error(fakes, tmp_path, "available storefronts request failed")

    assert "install ipatool 2.8.0" in err.hint


def test_an_unrecognised_error_is_passed_through_verbatim(
    fakes: FakeTools, tmp_path: Path
) -> None:
    err = _download_error(fakes, tmp_path, "something new and strange")

    assert err.message == "ipatool couldn't download com.example.app: something new and strange"
    assert "tap Get" not in err.hint
    assert ipatool.login_command(fakes.ipatool) not in err.hint


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
