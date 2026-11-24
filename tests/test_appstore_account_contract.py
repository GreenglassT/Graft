"""The App Store account events are on the contract from both sides before
any verb emits one, so every later commit keeps test_cli_event_contract.py
and CLIContractTests green."""

from __future__ import annotations

from graft_ipa.json_output import EVENT_TYPES, GUI_EVENTS

ACCOUNT_EVENTS = {
    "appstore-login",
    "appstore-logout",
    "appstore-enroll-candidate",
    "appstore-enroll-candidates-end",
    "appstore-enroll-command",
    "appstore-enrolled",
    "appstore-claimed",
}


def test_the_account_events_are_registered_for_the_gui() -> None:
    assert ACCOUNT_EVENTS <= EVENT_TYPES
    assert ACCOUNT_EVENTS <= GUI_EVENTS


def test_each_couldn_t_tell_reason_has_its_own_sentence_for_the_app() -> None:
    """`appstore-status.ipatool.unknown_message`: the CLI's own sentence for
    each `unknown_reason`, which the app shows on the account row (Settings
    hides `setup_commands`' comments). Null whenever the reason is."""
    from graft_ipa import ipatool

    reasons = {ipatool.UNKNOWN_NO_VERDICT, ipatool.UNKNOWN_KEYCHAIN, ipatool.UNKNOWN_LOCKED}
    assert reasons == {"no-verdict", "keychain-no-answer", "keychain-locked"}
    assert set(ipatool.UNKNOWN_MESSAGES) == reasons
    for reason, sentence in ipatool.UNKNOWN_MESSAGES.items():
        assert sentence.startswith("Couldn't tell whether ipatool is signed in: "), reason
        assert sentence.endswith("."), reason
    assert "keychain is locked" in ipatool.UNKNOWN_MESSAGES["keychain-locked"]
    assert "didn't answer" in ipatool.UNKNOWN_MESSAGES["keychain-no-answer"]


def test_unknown_message_is_documented_on_the_status_event() -> None:
    from pathlib import Path

    import graft_ipa.json_output as json_output

    text = Path(json_output.__file__).read_text()
    assert "`ipatool.unknown_message`" in text and '"keychain-locked"' in text
