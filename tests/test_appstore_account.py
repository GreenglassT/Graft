"""`appstore_account`: Graft's own sign-in marker, and telling a 1.x sign-in
(the user's own ipatool's keychain items) from one Graft's ipatool made."""

from __future__ import annotations

import json
import stat

import pytest

from graft_ipa import appstore_account, keychain, tools
from graft_ipa.appstore_account import IPATOOL_ITEMS, IPATOOL_SERVICE
from tests.keychain_fakes import FakeSecurity


def _security(monkeypatch: pytest.MonkeyPatch, *keys: str) -> FakeSecurity:
    fake = FakeSecurity({(IPATOOL_SERVICE, k): "old" for k in keys})
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def test_the_marker_says_which_ipatool_when_and_its_items_dates_and_nothing_about_the_account(
    monkeypatch,
) -> None:
    fake = _security(monkeypatch, "account", "anisette-v4")
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    path = appstore_account.marker_path()
    body = json.loads(path.read_text())
    assert set(body) == {"format", "ipatool_source", "signed_in_at", "bundled_items"}
    assert body["bundled_items"] == {
        "account": fake.created[(IPATOOL_SERVICE, "account")],
        "anisette-v4": fake.created[(IPATOOL_SERVICE, "anisette-v4")],
    }
    assert "old" not in path.read_text(), "no secret, nothing of the account"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    appstore_account.forget_signed_in()
    assert not path.exists()


def test_forgetting_twice_is_fine() -> None:
    appstore_account.forget_signed_in()
    appstore_account.forget_signed_in()


def test_items_with_no_marker_under_the_bundled_ipatool_are_a_1x_sign_in(monkeypatch) -> None:
    _security(monkeypatch, "account")
    assert appstore_account.legacy_signin(tools.SOURCE_BUNDLED) is True


def test_graft_s_own_sign_in_is_not_a_1x_one(monkeypatch) -> None:
    _security(monkeypatch, "account")
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    assert appstore_account.legacy_signin(tools.SOURCE_BUNDLED) is False


NOT_BUNDLED = (tools.SOURCE_CONFIG, tools.SOURCE_DEFAULT, tools.SOURCE_PATH)


def test_the_users_own_ipatool_reads_its_own_items(monkeypatch) -> None:
    """A pipx install, or an ipatool set in config.yaml, is the program that
    made items Graft's record doesn't vouch for, so nothing is wrong, nothing
    is cleared, and with no record `security` isn't even asked."""
    fake = _security(monkeypatch, "account")
    for source in NOT_BUNDLED:
        assert appstore_account.legacy_signin(source) is False
        assert appstore_account.graft_signin(source) is False
        assert appstore_account.needs_clearing(source) == []
        assert appstore_account.foreign_device(source) is False
    assert fake.calls == []


def test_for_another_ipatool_what_graft_s_record_vouches_for_is_another_program_s(monkeypatch) -> None:
    """signin-1: the bundled ipatool made what the record vouches for, and
    any other ipatool can read it only behind a Keychain dialog, so for that
    one it is what a 1.x item is for the bundled one: not read, and cleared
    before it signs in."""
    _security(monkeypatch, *IPATOOL_ITEMS)
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    for source in NOT_BUNDLED:
        assert appstore_account.graft_signin(source) is True
        assert appstore_account.legacy_signin(source) is False, "not a sign-in Graft's own can't read"
        assert appstore_account.foreign_device(source) is True
        assert appstore_account.needs_clearing(source) == list(IPATOOL_ITEMS)
    assert appstore_account.graft_signin(tools.SOURCE_BUNDLED) is False
    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == []


def test_for_another_ipatool_only_items_still_graft_s_are_cleared(monkeypatch) -> None:
    """An item remade since (a new creation date) isn't Graft's any more."""
    fake = _security(monkeypatch, "account", "anisette-v4")
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    fake.remove(IPATOOL_SERVICE, "account")
    fake.add(IPATOOL_SERVICE, "account")
    assert appstore_account.graft_signin(tools.SOURCE_CONFIG) is False
    assert appstore_account.needs_clearing(tools.SOURCE_CONFIG) == ["anisette-v4"]


def test_a_sign_in_through_another_ipatool_vouches_for_nothing(monkeypatch) -> None:
    """Its sign-in deleted Graft's items first (`needs_clearing`), so the
    record drops them rather than carry dates of items that are gone."""
    _security(monkeypatch, *IPATOOL_ITEMS)
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    assert set(appstore_account.vouched()) == set(IPATOOL_ITEMS)
    appstore_account.mark_signed_in(tools.SOURCE_CONFIG)
    assert appstore_account.vouched() == {}
    body = json.loads(appstore_account.marker_path().read_text())
    assert (body["ipatool_source"], body["bundled_items"]) == (tools.SOURCE_CONFIG, {})


def test_for_another_ipatool_a_keychain_that_cant_say_is_couldnt_tell(monkeypatch) -> None:
    fake = _security(monkeypatch, "account")
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    fake.find_status[IPATOOL_SERVICE] = 36
    assert appstore_account.graft_signin(tools.SOURCE_DEFAULT) is None
    with pytest.raises(appstore_account.KeychainUnanswered):
        appstore_account.needs_clearing(tools.SOURCE_DEFAULT)


def test_no_items_is_not_a_1x_sign_in(monkeypatch) -> None:
    _security(monkeypatch)
    assert appstore_account.legacy_signin(tools.SOURCE_BUNDLED) is False


def test_asking_never_reads_a_secret(monkeypatch) -> None:
    fake = _security(monkeypatch, "account")
    appstore_account.legacy_signin(tools.SOURCE_BUNDLED)
    (call,) = fake.calls
    assert call == ["security", "find-generic-password", "-s", IPATOOL_SERVICE, "-a", "account"]


def test_clearing_removes_all_three_items_and_says_which(monkeypatch) -> None:
    fake = _security(monkeypatch, *IPATOOL_ITEMS)
    assert appstore_account.clear_legacy_items() == list(IPATOOL_ITEMS)
    assert not [k for k in fake.items if k[0] == IPATOOL_SERVICE]


def test_clearing_names_only_ipatools_items(monkeypatch) -> None:
    """Decision 2: only ipatool's own items, by their exact service and
    account. Graft's own secrets beside them stay."""
    fake = FakeSecurity({
        **{(IPATOOL_SERVICE, k): "old" for k in IPATOOL_ITEMS},
        ("graft", "p12-TESTTEAM01"): "keep me",
        ("ipatool-auth.service-other", "account"): "not ours to touch",
    })
    monkeypatch.setattr(keychain, "_run", fake)
    appstore_account.clear_legacy_items()
    deletes = [c for c in fake.calls if c[1] == "delete-generic-password"]
    assert deletes == [
        ["security", "delete-generic-password", "-s", IPATOOL_SERVICE, "-a", k] for k in IPATOOL_ITEMS
    ]
    assert set(fake.items) == {("graft", "p12-TESTTEAM01"), ("ipatool-auth.service-other", "account")}


def test_clearing_what_isnt_there_removes_nothing(monkeypatch) -> None:
    _security(monkeypatch, "account")
    assert appstore_account.clear_legacy_items() == ["account"]


def test_not_found_with_the_item_there_is_a_keychain_refusal(monkeypatch) -> None:
    """K1: after a denied Keychain dialog ipatool says "not found"."""
    said = "failed to get account: failed to get item: The specified item could not be found in the keyring"
    _security(monkeypatch, "account")
    assert appstore_account.keychain_refused(said) is True


def test_not_found_with_no_item_is_simply_signed_out(monkeypatch) -> None:
    _security(monkeypatch)
    assert appstore_account.keychain_refused(
        "failed to get account: failed to get item: The specified item could not be found in the keyring"
    ) is False
