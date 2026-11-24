"""The App Store words and numbers the app reads, pinned against the CLI's.

Each literal below is spelled once in Python (the CLI emits it) and once in
`Graft/Graft/Models/CLIContract.swift` (the app compares against it). A typo
on either side compiles, passes each side's own tests, and silently breaks
the feature: an `unverified-helper` refusal the app doesn't recognize falls
back to a generic failure, an exit status read wrong turns a Stop into
"failed". So each pair is read out of the Swift source and compared here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graft_ipa import appstore, enrollment, tools
from tests.cli_ast import contract_swift_source


def _swift_enum(name: str) -> dict[str, str]:
    """`static let NAME = VALUE` inside `enum <name> { … }`, values unquoted."""
    source = contract_swift_source()
    start = re.search(rf"\benum {re.escape(name)}\s*\{{", source)
    assert start, f"enum {name} is gone from CLIContract.swift"
    depth, i = 1, start.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    body = source[start.end():i]
    return {
        m.group(1): m.group(2).strip('"')
        for m in re.finditer(r"static let (\w+)\s*=\s*(\"[^\"\\]*\"|-?\d+)", body)
    }


@pytest.mark.parametrize(
    ("swift", "python"),
    [
        ("unverified", appstore.REFUSED_UNVERIFIED),
        ("needsLicense", appstore.REFUSED_NEEDS_LICENSE),
        ("paid", appstore.REFUSED_PAID),
        ("unverifiedHelper", enrollment.REFUSED_UNVERIFIED_HELPER),
    ],
)
def test_each_refusal_reason_is_the_word_the_app_reads(swift: str, python: str) -> None:
    assert _swift_enum("AppStoreRefusal")[swift] == python


def test_the_tool_sources_are_the_words_the_app_reads() -> None:
    assert _swift_enum("ToolSource") == {
        "bundled": tools.SOURCE_BUNDLED,
        "config": tools.SOURCE_CONFIG,
        "defaultPath": tools.SOURCE_DEFAULT,
        "path": tools.SOURCE_PATH,
    }


def test_the_unverified_helper_kinds_are_the_words_the_app_reads() -> None:
    assert _swift_enum("UnverifiedHelperKind") == {
        "adHoc": enrollment.UNVERIFIED_AD_HOC,
        "notBundled": enrollment.UNVERIFIED_NOT_BUNDLED,
    }


def test_the_enrolment_exit_statuses_are_the_numbers_the_app_reads() -> None:
    assert _swift_enum("EnrolExit") == {
        "notInApplications": str(enrollment.EXIT_NOT_IN_APPLICATIONS),
        "notRoots": str(enrollment.EXIT_NOT_ROOTS),
        "writable": str(enrollment.EXIT_WRITABLE),
        "stopped": str(enrollment.EXIT_CANCELLED),
        "timedOut": str(enrollment.EXIT_TIME_LIMIT),
        "noFairPlayState": str(enrollment.EXIT_NO_FAIRPLAY_STATE),
    }


# --- The delta audit's critic: the new status fields and failed reasons ----------
#
# The app decodes `appstore-status` and `appstore-login` with Swift structs
# (Graft/Graft/Models/AppStoreRecords.swift). A field spelled differently is
# never filled; a non-optional one the CLI leaves out or sends null fails the
# whole line, which buildEvents then drops without a word. So the structs'
# keys are read out of the Swift source and held against what the CLI emits.

from tests.cli_ast import REPO_ROOT, strip_swift_comments  # noqa: E402

RECORDS_SWIFT = REPO_ROOT / "Graft" / "Graft" / "Models" / "AppStoreRecords.swift"


def _swift_struct(name: str, path: Path = RECORDS_SWIFT) -> str:
    source = strip_swift_comments(path.read_text())
    start = re.search(rf"\bstruct {re.escape(name)}\b[^{{]*\{{", source)
    assert start, f"struct {name} is gone from {path.name}"
    depth, i = 1, start.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[start.end():i - 1]


def _decoded(name: str, path: Path = RECORDS_SWIFT) -> dict[str, bool]:
    """The JSON keys struct `name` decodes, each with whether the Swift type
    is optional (or has a default): its own stored properties, through its
    CodingKeys when it has them, not those of a struct nested in it."""
    body = _swift_struct(name, path)
    for nested in re.finditer(r"\bstruct (\w+)\b", body):
        body = body.replace(_swift_struct(nested[1], path), "")
    props = {m[2]: m[3].strip().endswith("?") or m[4] is not None
             for m in re.finditer(r"^\s*(let|var) (\w+)\s*:\s*([^=\n]+?)\s*(=.*)?$", body, re.M)}
    keys = {p: p for p in props}
    coding = re.search(r"enum CodingKeys\b[^{]*\{(.*?)\}", body, re.S)
    if coding:
        keys = {}
        for case in re.findall(r"case ([^\n]+)", coding[1]):
            for part in case.split(","):
                prop, _, raw = part.partition("=")
                keys[prop.strip()] = raw.strip().strip('"') or prop.strip()
    return {keys[p]: optional for p, optional in props.items() if p in keys}


@pytest.fixture
def no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ipatool or UnfairPlay anywhere: status reads its fields without
    running either."""
    monkeypatch.setattr(tools, "which", lambda _name: None)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)


#: Fields the CLI sends that this app doesn't read: UnfairPlay's `source`,
#: which no Settings row shows. Anything else unread is a spelling to fix.
NOT_DECODED_YET = {"ipatool": set(), "unfairplay": {"source"}}


@pytest.mark.parametrize(("tool", "struct"), [("ipatool", "IPATool"), ("unfairplay", "UnfairPlay")])
def test_each_status_field_the_app_decodes_is_one_the_cli_sends(no_tools, tool: str, struct: str) -> None:
    from graft_ipa.config import AppStoreToolsConfig

    payload = appstore.status(AppStoreToolsConfig.from_dict(None))[tool]
    swift = _decoded(struct)
    assert set(swift) <= set(payload), f"the app reads keys the CLI never sends: {set(swift) - set(payload)}"
    assert set(payload) - set(swift) <= NOT_DECODED_YET[tool], set(payload) - set(swift)
    for key, optional in swift.items():
        if not optional:
            assert payload[key] is not None, f"{tool}.{key} is non-optional in Swift but null here"
    if tool == "ipatool":
        assert {"unknown_reason", "unknown_message", "legacy_signin", "source", "email"} <= set(swift)


def test_the_status_record_s_own_keys_are_the_cli_s(no_tools) -> None:
    from graft_ipa.config import AppStoreToolsConfig

    payload = appstore.status(AppStoreToolsConfig.from_dict(None))
    assert set(_decoded("AppStoreStatusRecord")) == set(payload)


def test_each_couldn_t_tell_reason_is_worded_by_the_app_or_by_the_cli() -> None:
    """`unknown_reason`: the app words "no-verdict" and "keychain-no-answer"
    itself when no `unknown_message` comes (CLIContract.UnknownSignIn), and
    shows the CLI's sentence for any other, "keychain-locked" among them,
    which has no Swift counterpart by design. So each reason the CLI can send
    has the app's word or the CLI's sentence, and the app's words are the
    CLI's spellings."""
    from graft_ipa import ipatool

    app_words = _swift_enum("UnknownSignIn")
    assert app_words.items() >= {
        "noVerdict": ipatool.UNKNOWN_NO_VERDICT, "keychainNoAnswer": ipatool.UNKNOWN_KEYCHAIN,
    }.items()
    reasons = {ipatool.UNKNOWN_NO_VERDICT, ipatool.UNKNOWN_KEYCHAIN, ipatool.UNKNOWN_LOCKED,
               ipatool.UNKNOWN_GRAFT_SIGNIN}
    assert set(app_words.values()) <= reasons, "every word the app has is one the CLI sends"
    assert reasons - set(app_words.values()) <= {"keychain-locked", "graft-ipatool-signin"}
    # "graft-ipatool-signin" names both ipatools, so its sentence is built
    # (`ipatool.graft_signin_message`); every other is a fixed one.
    assert set(ipatool.UNKNOWN_MESSAGES) | {ipatool.UNKNOWN_GRAFT_SIGNIN} == reasons
    assert ipatool.graft_signin_message(Path("/x/ipatool"), tools.SOURCE_DEFAULT)


def test_every_failed_sign_in_reason_reaches_the_sheet_as_the_cli_s_sentence() -> None:
    """`appstore-login {state: "failed"}`: the sheet shows `message` for any
    `reason` (AppStoreAccountStore.outcome), so a reason needs no Swift
    counterpart, but the record must decode: its keys are the CLI's, all
    optional but `state`, and every reason has a sentence. "unknown-prompt",
    "ipatool-changed" and "keychain-locked" have no Swift word of their own."""
    from graft_ipa import appstore_login

    swift = _decoded("AppStoreLoginRecord")
    assert swift == {"state": False, "email": True, "reason": True, "message": True, "hint": True, "elapsed": True}
    reasons = {v for k, v in vars(appstore_login).items() if k.startswith("REASON_")}
    assert {"unknown-prompt", "ipatool-changed", "keychain-locked"} <= reasons
    for reason in reasons - {appstore_login.REASON_OTHER}:
        assert appstore_login.MESSAGES.get(reason), reason
    assert appstore_login.STATE_FAILED == _swift_enum("AppStoreLogin")["failed"]
    contract = contract_swift_source()
    for reason in ("unknown-prompt", "ipatool-changed", "keychain-locked"):
        assert f'"{reason}"' not in contract, f"{reason} has a Swift word now: pin it here"


# --- The update check ---------------------------------------------------------
#
# `update-check` decodes into `UpdateCheckRecord` (BuildEvent.swift). The app
# reads `latest` and `update_available` for the banner and the alert (and
# builds the release page from `latest`: it decodes `url` and never opens it),
# `current` to know the answer is about this copy, and `message` for a failed
# Check for Updates…. A key spelled differently there is never filled, and a
# non-optional one the CLI sends as null drops the whole line, so the check
# would look like it never answered.

EVENTS_SWIFT = REPO_ROOT / "Graft" / "Graft" / "Models" / "BuildEvent.swift"


@pytest.mark.parametrize("worked", [True, False])
def test_the_update_check_record_decodes_what_the_cli_sends(worked: bool) -> None:
    from graft_ipa import release_check

    if worked:
        payload = release_check.event(
            "2.0", release=release_check.Release(version="2.0.1", url=release_check.RELEASES_PAGE + "/latest")
        )
    else:
        payload = release_check.event(
            "2.0", error=release_check.UpdateCheckError("offline", reason=release_check.OFFLINE)
        )
    swift = _decoded("UpdateCheckRecord", EVENTS_SWIFT)
    assert set(swift) == set(payload)
    for key, optional in swift.items():
        if not optional:
            assert payload[key] is not None, f"update-check.{key} is non-optional in Swift but null here"


def test_the_already_staged_record_decodes_every_key_the_cli_sends(tmp_path: Path) -> None:
    """`appstore-already-staged`, a get that downloaded nothing: the app's
    record (`AppStoreAlreadyStagedRecord`) reads exactly the keys the CLI
    sends, and the CLI sends each one it can't do without, never null. A
    line that failed to decode would leave the app with a get that "finished"
    and said nothing."""
    import json
    from io import StringIO

    from graft_ipa.ipa_cache import CachedIPA
    from graft_ipa.json_output import EVENT_TYPES, GUI_EVENTS, JsonEmitter

    assert "appstore-already-staged" in EVENT_TYPES and "appstore-already-staged" in GUI_EVENTS
    buf = StringIO()
    staged = CachedIPA(path=tmp_path / "com.example.app_1.0.ipa", bundle_id="com.example.app", version="1.0")
    appstore.report_already_staged("example", staged, emitter=JsonEmitter(stream=buf), say=print)
    (line,) = buf.getvalue().splitlines()
    payload = json.loads(line)
    assert payload.pop("type") == "appstore-already-staged"

    swift = _decoded("AppStoreAlreadyStagedRecord")
    assert set(swift) == set(payload)
    for key, optional in swift.items():
        if not optional:
            assert payload[key] is not None, key
