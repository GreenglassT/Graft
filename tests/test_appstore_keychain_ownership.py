"""Whose ipatool items are in the login keychain, across ipatools.

Graft.app's bundled ipatool may read without a dialog only the items it made
itself. Graft's marker records those by their creation dates
(`appstore_account`), and these are the ways that record was once wrong (the
2.0 review's #3, #4, #12, #13 and #15), played through end to end:

- a sign-in through the user's own ipatool (pipx's `~/.local/bin`, or one set
  in config.yaml and later removed), then Graft.app's status;
- the user's own ipatool revoking and signing in again;
- the state dir copied onto a Mac whose keychain holds another sign-in;
- Sign Out, then Sign In;
- the user's own ipatool denied Graft's item.

One fake login keychain (`FakeSecurity(store=)`) is shared by Graft's
`security` calls and by two fake ipatools in other processes: the bundled one,
and the user's own. Each fake makes, updates and removes items as ipatool
2.8.0 does (updates in place keep the creation date), and writes down every
Keychain dialog it would have shown (`FakeTools.dialogs`). Decision 10 is then
checked directly: Graft's own ipatool is never put in front of a dialog.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from graft_ipa import appstore, appstore_account, appstore_login, ipatool, keychain, tools
from graft_ipa.appstore_account import IPATOOL_SERVICE
from graft_ipa.config import load_config
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FAKE_EMAIL, FakeTools
from tests.keychain_fakes import FakeSecurity
from tests.test_appstore_login import EMAIL, PASSWORD, Codes, Stdin, _run, secret


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    fake = FakeSecurity(store=tmp_path / "login-keychain.json")
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def _fake(root: Path, store: FakeSecurity) -> FakeTools:
    return FakeTools(root).set(keychain=str(store.store), login_two_factor=False)


@pytest.fixture
def bundled(tmp_path: Path, store: FakeSecurity) -> FakeTools:
    """Graft.app's own ipatool (in `Contents/Helpers/bin`)."""
    return _fake(tmp_path / "Graft.app-Helpers-bin", store)


@pytest.fixture
def own(store: FakeSecurity) -> FakeTools:
    """The user's own ipatool at ~/.local/bin, where pipx's `graft` finds it."""
    return _fake(Path.home() / ".local" / "bin", store)


@pytest.fixture
def cfg(temp_config_env: Path):
    """No appstore: section, so the ipatool that runs is Graft.app's when
    there is one (`app(True)`) and ~/.local/bin's when not."""
    return load_config(temp_config_env)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, bundled: FakeTools):
    """`app(True)`: this CLI is Graft.app's; `app(False)`: a pipx install's."""

    def switch(inside: bool) -> None:
        monkeypatch.setattr(tools, "bundled_tools_dir", (lambda: bundled.root) if inside else (lambda: None))

    return switch


def _status(cfg) -> dict:
    return appstore.status(cfg.appstore)["ipatool"]


def _auth_infos(fakes: FakeTools) -> int:
    return len([c for c in fakes.calls("ipatool") if c["argv"][:2] == ["auth", "info"]])


def _deletes(store: FakeSecurity) -> list[str]:
    return [c[-1] for c in store.calls if c[1] == "delete-generic-password"]


def _sign_in_graft(cfg) -> object:
    outcome, _ = _run(cfg, Stdin(EMAIL, PASSWORD))
    return outcome


def _sign_in_directly(fakes: FakeTools) -> str:
    """`ipatool auth login` run by hand, with no Graft around it: no marker."""
    return appstore_login.sign_in(
        fakes.ipatool, secret(EMAIL), secret(PASSWORD), Codes(), emitter=None, say=lambda _: None,
    )


def _revoke_directly(fakes: FakeTools) -> None:
    subprocess.run([str(fakes.ipatool), "auth", "revoke", "--format", "json", "--non-interactive"],
                   check=True, capture_output=True, timeout=30)


def _ours(store: FakeSecurity, fakes: FakeTools) -> set[str]:
    store.load()
    return {a for (s, a), owner in store.owners.items() if s == IPATOOL_SERVICE and owner == str(fakes.root)}


def test_the_premise_graft_s_own_sign_in_is_silent(cfg, app, bundled: FakeTools, store) -> None:
    """K3, in the fakes: after Graft's own sign-in, status reads it silently."""
    app(True)
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert _ours(store, bundled) == {"account", "anisette-v4"}
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["email"], ipa["legacy_signin"]) == (True, FAKE_EMAIL, False)
    assert bundled.dialogs() == []


def test_the_premise_the_fake_does_show_a_dialog_for_another_program_s_item(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """Positive control: reading the user's own ipatool's item is a dialog."""
    app(False)
    _sign_in_directly(own)
    from graft_ipa import ipatool

    assert ipatool.account(bundled.ipatool).signed_in is None
    assert bundled.dialogs() == ["read account"]


def test_a_sign_in_through_the_users_own_ipatool_vouches_for_nothing(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """#3 and #4: a pipx `graft --appstore-login` signs in with
    ~/.local/bin/ipatool (source default). Graft.app's status then must not
    ask its own ipatool about those items, and its Sign In replaces them
    without the K2 dialogs."""
    app(False)
    assert _status(cfg)["source"] == tools.SOURCE_DEFAULT
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert _ours(store, own) == {"account", "anisette-v4"}

    app(True)
    ipa = _status(cfg)
    assert ipa["source"] == tools.SOURCE_BUNDLED
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None)
    assert _auth_infos(bundled) == 0, "Graft's ipatool was never asked: asking is the dialog"

    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert sorted(_deletes(store)) == ["account", "anisette-v4"]
    assert _ours(store, bundled) == {"account", "anisette-v4"}
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"]) == (True, False)
    assert bundled.dialogs() == []


def test_a_config_ipatool_that_is_later_removed_vouches_for_nothing(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path,
) -> None:
    """#4: appstore.ipatool names another ipatool (say a good one beside a
    bad bundled copy); Graft.app signs in with it; then the setting goes."""
    override = _fake(tmp_path / "override", store)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = {"ipatool": str(override.ipatool)}
    with_override = tmp_path / "with-override.yaml"
    with_override.write_text(yaml.safe_dump(raw, sort_keys=False))
    app(True)
    configured = load_config(with_override)
    assert _status(configured)["source"] == tools.SOURCE_CONFIG
    assert _sign_in_graft(configured) == FAKE_EMAIL
    assert _ours(store, override) == {"account", "anisette-v4"}

    ipa = _status(cfg)  # the setting removed
    assert (ipa["source"], ipa["legacy_signin"], ipa["signed_in"]) == (tools.SOURCE_BUNDLED, True, None)
    assert _auth_infos(bundled) == 0
    assert bundled.dialogs() == []


def test_a_pipx_sign_in_over_graft_s_items_replaces_them_without_a_dialog(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """The whole-diff review's signin-1 (upgrade-1): for any ipatool but the
    bundled one, an item Graft's record vouches for is another program's
    (Graft.app's), so a sign-in through it deletes those first, as the
    bundled ipatool deletes 1.x items, and signs in fresh. It used to sign
    in over them: K2's dialogs. The record then vouches for nothing."""
    app(True)
    _sign_in_graft(cfg)
    app(False)
    store.calls.clear()
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert own.dialogs() == [], "no K1 read and no K2 update: Graft's items were gone first"
    assert sorted(_deletes(store)) == ["account", "anisette-v4"]
    assert _ours(store, own) == {"account", "anisette-v4"}
    assert appstore_account.vouched() == {}

    app(True)
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"]) == (None, True)
    assert _auth_infos(bundled) == 0, "status asked nobody"
    assert bundled.dialogs() == []


def test_a_revoke_and_a_login_by_another_ipatool_is_not_graft_s(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """#13: after Graft signs in, the user does what the sheet says their own
    ipatool needs, `ipatool auth revoke` and `auth login`, by hand. The new
    `account` is theirs; `anisette-v4`, which revoke leaves, is still Graft's."""
    app(True)
    _sign_in_graft(cfg)
    _revoke_directly(own)
    own.set(keychain_answer="allow")
    _sign_in_directly(own)
    assert _ours(store, own) == {"account"}
    assert _ours(store, bundled) == {"anisette-v4"}

    ipa = _status(cfg)
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None)
    assert _auth_infos(bundled) == 0

    store.calls.clear()
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert _deletes(store) == ["account"], "Graft's own device item stays"
    assert bundled.dialogs() == []


def test_a_state_dir_copied_onto_another_keychain_vouches_for_nothing(
    cfg, app, bundled: FakeTools, own: FakeTools, store, tmp_path: Path, monkeypatch,
) -> None:
    """#13: ~/.local/state/graft restored onto a Mac (or a reset login
    keychain) whose items another ipatool made."""
    app(True)
    _sign_in_graft(cfg)
    other = FakeSecurity(store=tmp_path / "other-mac.json")
    for name in ("account", "anisette-v4", "kbsync-v1"):
        other.add(IPATOOL_SERVICE, name, owner=str(own.root))
    monkeypatch.setattr(keychain, "_run", other)
    bundled.set(keychain=str(other.store))

    ipa = _status(cfg)
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None)
    assert bundled.dialogs() == []


def test_sign_out_then_sign_in_keeps_graft_s_own_device_item(
    cfg, app, bundled: FakeTools, store,
) -> None:
    """#15: revoke leaves `anisette-v4`. The next sign-in deletes nothing:
    every item left is Graft's own."""
    app(True)
    _sign_in_graft(cfg)
    store.load()
    device = store.created[(IPATOOL_SERVICE, "anisette-v4")]
    appstore_login.sign_out(cfg, emitter=None, say=lambda _: None)
    store.load()
    assert set(store.items) == {(IPATOOL_SERVICE, "anisette-v4")}
    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == []

    store.calls.clear()
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert _deletes(store) == []
    store.load()
    assert store.created[(IPATOOL_SERVICE, "anisette-v4")] == device
    assert bundled.dialogs() == []


def test_a_pipx_status_over_graft_s_sign_in_asks_nobody(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """signin-1, the pipx (default) case: a pipx `graft --appstore-status`
    runs the user's own ipatool, which can't read Graft.app's `account`
    without a dialog. It used to run `auth info` on it anyway (K1, again on
    every status after a Deny). Now it isn't asked: "couldn't tell", saying
    whose sign-in it is, and Graft.app's record is left as it was."""
    app(True)
    _sign_in_graft(cfg)
    app(False)
    ipa = _status(cfg)
    assert (ipa["source"], ipa["signed_in"], ipa["legacy_signin"]) == (tools.SOURCE_DEFAULT, None, False)
    assert ipa["unknown_reason"] == ipatool.UNKNOWN_GRAFT_SIGNIN == "graft-ipatool-signin"
    assert str(own.ipatool) in ipa["unknown_message"]
    assert "Graft.app's own ipatool" in ipa["unknown_message"]
    assert "Keychain dialogs" in ipa["unknown_message"]
    assert "Sign in again" in ipa["unknown_message"]
    assert (_auth_infos(own), own.dialogs()) == (0, [])
    assert set(appstore_account.vouched()) == {"account", "anisette-v4"}
    commands = appstore.status(cfg.appstore)["setup_commands"]
    assert f"# {ipa['unknown_message']}" in commands
    assert ipatool.LOGIN_COMMAND in commands
    said: list[str] = []
    appstore.report_status(cfg, emitter=None, say=said.append)
    assert "couldn't tell whether it is signed in (Graft.app's own ipatool holds the sign-in)" in "\n".join(said)

    app(True)
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["email"], ipa["legacy_signin"]) == (True, FAKE_EMAIL, False)
    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == []
    appstore._require_signed_in(cfg.appstore, bundled.ipatool)
    assert bundled.dialogs() == []


def _configured(temp_config_env: Path, tmp_path: Path, fakes: FakeTools):
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = {"ipatool": str(fakes.ipatool), "unfairplay_models": str(fakes.models)}
    path = tmp_path / "configured.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(path)


def test_a_configured_ipatool_over_graft_s_sign_in_is_not_asked_and_its_sign_in_replaces_it(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path,
) -> None:
    """signin-1, the verifier's scenario: Graft.app signed in with its own
    ipatool, then appstore.ipatool names another (as Graft's own hints and
    upgrading.md suggest). Status ran that one's `auth info` on Graft's items
    (K1's dialog on every read), and its sign-in cleared nothing and saved
    over them (K2's)."""
    app(True)
    _sign_in_graft(cfg)
    override = _fake(tmp_path / "override", store)
    configured = _configured(temp_config_env, tmp_path, override)

    ipa = _status(configured)
    assert (ipa["source"], ipa["signed_in"], ipa["legacy_signin"]) == (tools.SOURCE_CONFIG, None, False)
    assert ipa["unknown_reason"] == "graft-ipatool-signin"
    message = ipa["unknown_message"]
    assert f"the ipatool at {override.ipatool}, set in config.yaml," in message
    assert message.startswith("Graft.app's own ipatool holds this App Store sign-in")
    assert "remove appstore.ipatool from config.yaml to use Graft.app's" in message
    assert (_auth_infos(override), override.dialogs()) == (0, [])

    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(configured.appstore, override.ipatool)
    assert "Graft.app's own ipatool" in e.value.message and str(override.ipatool) in e.value.message
    assert "config.yaml" in (e.value.hint or "")
    assert (_auth_infos(override), override.dialogs()) == (0, []), "a Get asks nobody either"

    store.calls.clear()
    assert _sign_in_graft(configured) == FAKE_EMAIL
    assert sorted(_deletes(store)) == ["account", "anisette-v4"]
    assert override.dialogs() == []
    assert _ours(store, override) == {"account", "anisette-v4"}
    assert appstore_account.vouched() == {}, "a sign-in through another ipatool vouches for nothing"
    ipa = _status(configured)
    assert (ipa["signed_in"], ipa["unknown_reason"]) == (True, None)

    ipa = _status(cfg)  # the setting removed: Graft's own ipatool now meets another's sign-in
    assert (ipa["source"], ipa["legacy_signin"], ipa["signed_in"]) == (tools.SOURCE_BUNDLED, True, None)
    assert bundled.dialogs() == []


def _naming(temp_config_env: Path, tmp_path: Path, ipatool_path: Path, models: Path):
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = {"ipatool": str(ipatool_path), "unfairplay_models": str(models)}
    path = tmp_path / "naming.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(path)


@pytest.mark.parametrize("how", ["its own path", "a link to it"])
def test_graft_s_own_ipatool_set_in_config_is_graft_s_own(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path, how: str,
) -> None:
    """The delta audit's signin-A: appstore.ipatool set to the path
    --appstore-status prints for Graft.app's own ipatool, or to a link to it,
    counted as another ipatool, so the items that binary made were another
    program's to it: status and Get refused them, and Sign In... deleted
    them and emptied the record. It is the bundled ipatool, run by its own
    path, and nothing is refused, asked or deleted."""
    app(True)
    _sign_in_graft(cfg)
    vouched = appstore_account.vouched()
    named = bundled.ipatool
    if how == "a link to it":
        named = tmp_path / "bin" / "ipatool"
        named.parent.mkdir()
        named.symlink_to(bundled.ipatool)
    configured = _naming(temp_config_env, tmp_path, named, bundled.models)
    tools_cfg = configured.appstore
    assert "ipatool" in tools_cfg.explicit

    explicit = "ipatool" in tools_cfg.explicit
    assert ipatool.source_of(tools_cfg.ipatool, explicit=explicit) == tools.SOURCE_BUNDLED
    assert ipatool.locate(tools_cfg.ipatool, explicit=explicit) == bundled.ipatool
    ipa = _status(configured)
    assert (ipa["source"], ipa["path"]) == (tools.SOURCE_BUNDLED, str(bundled.ipatool))
    assert (ipa["signed_in"], ipa["email"], ipa["legacy_signin"], ipa["unknown_reason"]) == \
        (True, FAKE_EMAIL, False, None)
    assert ipa["note"] is None, "it is there, so no 'isn't there any more' note"
    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == []
    appstore._require_signed_in(tools_cfg, ipatool.locate(tools_cfg.ipatool, explicit=explicit))

    store.calls.clear()
    assert _sign_in_graft(configured) == FAKE_EMAIL
    assert _deletes(store) == [], "Graft's own items are not another program's"
    assert set(appstore_account.vouched()) == set(vouched), "and the record still vouches for them"
    assert bundled.dialogs() == []


def test_another_ipatool_set_in_config_is_still_another(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path,
) -> None:
    """The control: a copy of the same file elsewhere is another program to
    the Keychain, and stays the configured ipatool."""
    import shutil

    app(True)
    copy = tmp_path / "copy" / "ipatool"
    copy.parent.mkdir()
    shutil.copy2(bundled.ipatool, copy)
    configured = _naming(temp_config_env, tmp_path, copy, bundled.models)
    assert ipatool.source_of(configured.appstore.ipatool, explicit=True) == tools.SOURCE_CONFIG
    assert ipatool.locate(configured.appstore.ipatool, explicit=True) == copy


def test_a_path_ipatool_over_graft_s_sign_in_says_where_it_was_found(
    cfg, app, bundled: FakeTools, store, tmp_path: Path, monkeypatch,
) -> None:
    app(True)
    _sign_in_graft(cfg)
    on_path = _fake(tmp_path / "on-path", store)
    app(False)
    monkeypatch.setattr(tools, "which", lambda name: str(on_path.ipatool) if name == "ipatool" else None)
    ipa = _status(cfg)
    assert (ipa["source"], ipa["unknown_reason"]) == (tools.SOURCE_PATH, "graft-ipatool-signin")
    assert f"the ipatool at {on_path.ipatool}, found on PATH," in ipa["unknown_message"]
    assert (_auth_infos(on_path), on_path.dialogs()) == (0, [])


def test_a_pipx_graft_with_a_configured_ipatool_is_told_graft_app_needs_the_setting_removed(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path,
) -> None:
    """Removing appstore.ipatool brings Graft.app's ipatool back only inside
    Graft.app: a pipx `graft` would run ~/.local/bin's or PATH's then. And
    "use Graft.app" alone didn't help (the delta audit's signin-C): Graft.app
    reads the same config.yaml, and runs the ipatool it names."""
    app(True)
    _sign_in_graft(cfg)
    override = _fake(tmp_path / "override", store)
    app(False)
    ipa = _status(_configured(temp_config_env, tmp_path, override))
    assert (ipa["source"], ipa["unknown_reason"]) == (tools.SOURCE_CONFIG, "graft-ipatool-signin")
    assert "to use Graft.app's" not in ipa["unknown_message"], "not the in-app advice"
    assert ipa["unknown_message"].endswith(
        "or use Graft.app with appstore.ipatool removed from config.yaml: Graft.app runs its own "
        "ipatool, which reads it, unless that setting names another."
    )


def test_a_pipx_graft_with_a_path_ipatool_is_told_to_use_graft_app(
    cfg, app, bundled: FakeTools, store, tmp_path: Path, monkeypatch,
) -> None:
    """With nothing in config.yaml, Graft.app runs its own: that advice stands."""
    app(True)
    _sign_in_graft(cfg)
    on_path = _fake(tmp_path / "on-path", store)
    app(False)
    monkeypatch.setattr(tools, "which", lambda name: str(on_path.ipatool) if name == "ipatool" else None)
    ipa = _status(cfg)
    assert (ipa["source"], ipa["unknown_reason"]) == (tools.SOURCE_PATH, "graft-ipatool-signin")
    assert ipa["unknown_message"].endswith("or use Graft.app, whose own ipatool reads it.")


@pytest.mark.parametrize("how", ["exit 36", "no answer"])
def test_another_ipatool_with_a_keychain_that_cant_say_asks_nobody(
    cfg, app, bundled: FakeTools, own: FakeTools, store, how: str,
) -> None:
    """With a record that vouches for `account`, whether it is still Graft's
    is asked of `security`; no answer is "couldn't tell", never `auth info`."""
    app(True)
    _sign_in_graft(cfg)
    app(False)
    if how == "no answer":
        store.hang.add(IPATOOL_SERVICE)
    else:
        store.find_status[IPATOOL_SERVICE] = 36
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"], ipa["unknown_reason"]) == (None, False, "keychain-no-answer")
    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(cfg.appstore, own.ipatool)
    assert "couldn't tell" in e.value.message
    assert (_auth_infos(own), own.dialogs()) == (0, [])


def test_another_ipatool_refused_an_item_graft_doesnt_know_isnt_sent_to_graft_s(
    cfg, app, own: FakeTools, store,
) -> None:
    """signin-1's text: a pipx status whose ipatool is refused an `account`
    nobody's record knows (another ipatool's) says that ipatool's path, not
    "sign in once with Graft's ... which replaces it": its sign-in clears
    only what Graft's record vouches for."""
    app(False)
    store.add(IPATOOL_SERVICE, "account")
    payload = appstore.status(cfg.appstore)
    assert (payload["ipatool"]["legacy_signin"], own.dialogs()) == (True, ["read account"])
    text = "\n".join(payload["setup_commands"])
    assert "sign in once with Graft's" not in text
    assert f"the ipatool at {own.ipatool} can't read the App Store sign-in" in text
    assert ipatool.LOGIN_COMMAND in payload["setup_commands"]
    said: list[str] = []
    appstore.report_status(cfg, emitter=None, say=said.append)
    assert "its sign-in is one this ipatool can't read" in "\n".join(said)


def test_another_ipatool_s_device_or_cache_item_that_is_graft_s_is_seen_to(
    cfg, app, bundled: FakeTools, own: FakeTools, store, tmp_path: Path, monkeypatch,
) -> None:
    """The rest of a Get through another ipatool: its own `account`, but
    Graft's `anisette-v4` (left after a revoke and a login by hand) is a
    refusal, and Graft's `kbsync-v1` is a cache deleted first."""
    app(True)
    _sign_in_graft(cfg)
    _revoke_directly(own)
    own.set(keychain_answer="allow")
    _sign_in_directly(own)
    own.set(keychain_answer="deny")
    asked = len(own.dialogs())
    assert _ours(store, bundled) == {"anisette-v4"}
    app(False)
    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(cfg.appstore, own.ipatool)
    assert "anisette-v4" in e.value.message and "Graft.app's own ipatool" in e.value.message
    assert str(own.ipatool) in e.value.message

    store.remove(IPATOOL_SERVICE, "anisette-v4")
    store.add(IPATOOL_SERVICE, "anisette-v4", owner=str(own.root))
    made = store.add(IPATOOL_SERVICE, "kbsync-v1", owner=str(bundled.root))
    data = json.loads(appstore_account.marker_path().read_text())
    data["bundled_items"]["kbsync-v1"] = made
    appstore_account.marker_path().write_text(json.dumps(data))
    store.calls.clear()
    appstore._require_signed_in(cfg.appstore, own.ipatool)
    assert _deletes(store) == ["kbsync-v1"]
    assert len(own.dialogs()) == asked, "the user's ipatool never met Graft's items"


def test_graft_s_own_ipatool_denied_forgets_the_record(
    cfg, app, bundled: FakeTools, store,
) -> None:
    """The other half of #12: when the refused ipatool is Graft's own, its
    record is wrong (a keychain restored, an access list changed), and is
    forgotten, so the next status doesn't ask and the next sign-in clears."""
    app(True)
    _sign_in_graft(cfg)
    store.load()
    store.owners[(IPATOOL_SERVICE, "account")] = "someone-else"  # an access list changed by hand
    store._save()
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"]) == (None, True)
    assert not appstore_account.marker_path().exists()
    assert _status(cfg)["legacy_signin"] is True
    assert bundled.dialogs() == ["read account"], "one dialog, and no more after it"


def test_a_marker_from_before_the_record_vouches_for_nothing(
    cfg, app, bundled: FakeTools, store,
) -> None:
    """A try build's marker had no creation dates. It reads as another
    ipatool's sign-in and heals on the next sign-in, as a Deny already did."""
    app(True)
    _sign_in_graft(cfg)
    appstore_account.marker_path().write_text(
        json.dumps({"ipatool_source": "bundled", "signed_in_at": "2026-11-21T00:00:00+00:00"})
    )
    ipa = _status(cfg)
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None)
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    assert _status(cfg)["legacy_signin"] is False
    assert bundled.dialogs() == []


# --- #14: "couldn't tell" is never "no" ---------------------------------------

from tests.test_appstore_login import DENIED_K1 as DENIED  # noqa: E402


def _lookups(store: FakeSecurity) -> int:
    return len([c for c in store.calls if c[1] == "find-generic-password"])


@pytest.mark.parametrize("how", ["exit 36", "no answer"])
def test_a_keychain_that_cant_say_is_couldnt_tell_and_ipatool_isnt_asked(
    cfg, app, bundled: FakeTools, own: FakeTools, store, how: str,
) -> None:
    """A locked keychain, a dialog canceled or nobody answering: status
    neither runs `auth info` (which could be the very dialog) nor says
    "signed out". One lookup, and no second wait after it."""
    app(False)
    _sign_in_directly(own)
    app(True)
    if how == "no answer":
        store.hang.add(IPATOOL_SERVICE)
    else:
        store.find_status[IPATOOL_SERVICE] = 36
    store.calls.clear()
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"]) == (None, False)
    assert ipa["unknown_reason"] == ipatool.UNKNOWN_KEYCHAIN
    assert ipa["unknown_message"] == ipatool.UNKNOWN_MESSAGES[ipatool.UNKNOWN_KEYCHAIN]
    assert _auth_infos(bundled) == 0
    assert bundled.dialogs() == []
    assert _lookups(store) == 1
    commands = appstore.status(cfg.appstore)["setup_commands"]
    assert any("Keychain Access" in c for c in commands)
    assert ipatool.LOGIN_COMMAND not in commands, "nothing says it is signed out"


def test_a_get_with_a_keychain_that_cant_say_is_refused_without_asking_ipatool(
    cfg, app, bundled: FakeTools, store,
) -> None:
    app(True)
    store.add(IPATOOL_SERVICE, "account")
    store.find_status[IPATOOL_SERVICE] = 51
    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(cfg.appstore, bundled.ipatool)
    assert "couldn't tell" in e.value.message
    assert "not signed in" not in e.value.message
    assert _auth_infos(bundled) == 0


def test_not_found_when_the_keychain_cant_say_is_not_signed_out(
    cfg, app, bundled: FakeTools, store, tmp_path: Path,
) -> None:
    """ipatool's "not found" with `security` unable to say whether the item
    is there: neither a refusal nor "signed out"."""
    app(True)
    bundled.set(keychain=None, auth_raw=json.dumps({"level": "error", "success": False, "error": DENIED}))
    store.find_status[IPATOOL_SERVICE] = 51
    found = ipatool.account(bundled.ipatool)
    assert found == ipatool.Account(None, unknown=ipatool.UNKNOWN_KEYCHAIN)
    assert ipatool.explain(DENIED, bundle_id="com.example.app", path=bundled.ipatool,
                           reported_version="2.8.0").message == ipatool.KEYCHAIN_UNKNOWN
    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(cfg.appstore, bundled.ipatool)
    assert "couldn't tell" in e.value.message
    assert _auth_infos(bundled) == 1, "the one `account` above; the preflight asked nobody"


def test_a_status_whose_second_lookup_gets_no_answer_waits_once(
    cfg, app, bundled: FakeTools, store,
) -> None:
    """The first lookup answered (Graft's own item); `auth info` then says
    "not found" and the lookup that would tell a refusal from a sign-out gets
    no answer. One wait, then "couldn't tell"."""
    app(True)
    _sign_in_graft(cfg)
    bundled.set(auth_raw=json.dumps({"level": "error", "success": False, "error": DENIED}))
    real = store.__call__

    def second_hangs(argv, **kw):
        if argv[1] == "find-generic-password" and _lookups(store) >= 1:
            store.hang.add(IPATOOL_SERVICE)
        return real(argv, **kw)

    store.calls.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(keychain, "_run", second_hangs)
        ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["unknown_reason"]) == (None, ipatool.UNKNOWN_KEYCHAIN)
    assert _lookups(store) == 2


def test_a_delete_that_fails_stops_the_sign_in_before_ipatool_runs(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """K2's two password dialogs are what signing in over another ipatool's
    items costs, so a delete that didn't happen (not "there was none") stops
    the sign-in, at the first one, with words."""
    app(False)
    _sign_in_directly(own)
    app(True)
    store.fail_deletes = True
    store.calls.clear()
    outcome, events = _run(cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, appstore_login.LoginFailed)
    assert outcome.reason == appstore_login.REASON_KEYCHAIN_CLEAR == "keychain-clear"
    assert events[-1]["reason"] == "keychain-clear"
    assert "login keychain" in events[-1]["message"]
    assert bundled.login_runs() == ([], []), "ipatool never started"
    assert len(_deletes(store)) == 1, "stopped at the first"
    store.load()
    assert (IPATOOL_SERVICE, "account") in store.items


def test_a_lookup_with_no_answer_stops_the_sign_in_after_one_wait(
    cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    app(False)
    _sign_in_directly(own)
    app(True)
    store.hang.add(IPATOOL_SERVICE)
    store.calls.clear()
    outcome, events = _run(cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, appstore_login.LoginFailed)
    assert events[-1]["reason"] == "keychain-clear"
    assert len(store.calls) == 1, "no second 60-second wait"
    assert bundled.login_runs() == ([], [])


# --- An item `security` lists without a usable date (the fix re-check's problem 5)
#
# Real macOS `security` always prints `cdat` for a generic password, but an
# item that is there and can't be dated must never count as Graft's: it
# couldn't be told from another program's remade one.


def test_a_creation_date_that_says_nothing_is_none_and_another_shape_is_kept() -> None:
    assert keychain._created('    "acct"<blob>="account"\n') is None, "no cdat line"
    assert keychain._created('    "cdat"<timedate>=<NULL>\n') is None
    assert keychain._created('    "cdat"<timedate>=\n') is None
    other = '"2026-11-22 14:20:00 +0000"'
    assert keychain._created(f'    "cdat"<timedate>={other}\n') == other, (
        "still the same for the same item, so a comparison stays sound"
    )


@pytest.mark.parametrize("shape", ["missing", "null"])
def test_a_sign_in_whose_items_cant_be_dated_is_not_recorded_as_graft_s(
    cfg, app, bundled: FakeTools, store, shape: str,
) -> None:
    app(True)
    if shape == "missing":
        store.hide_cdat = True
    else:
        store.cdat_shape = "null"

    outcome, events = _run(cfg, Stdin(EMAIL, PASSWORD))

    assert isinstance(outcome, appstore_login.LoginFailed)
    assert outcome.reason == events[-1]["reason"] == appstore_login.REASON_NOT_RECORDED
    assert appstore_account.vouched() == {}
    ipa = _status(cfg)
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None), "never taken for Graft's"
    assert _auth_infos(bundled) == 0
    assert bundled.dialogs() == []


@pytest.mark.parametrize("shape", ["missing", "null"])
def test_another_ipatool_s_item_that_cant_be_dated_is_never_graft_s(
    cfg, app, bundled: FakeTools, own: FakeTools, store, shape: str,
) -> None:
    """A 1.x sign-in, no marker, and no date: no record entry and no date
    are not a match. Status asks nobody, and a sign-in clears it."""
    app(False)
    _sign_in_directly(own)
    if shape == "missing":
        store.hide_cdat = True
    else:
        store.cdat_shape = "null"
    app(True)

    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == ["account", "anisette-v4"]
    ipa = _status(cfg)
    assert (ipa["legacy_signin"], ipa["signed_in"]) == (True, None)
    assert _auth_infos(bundled) == 0
    assert bundled.dialogs() == []


def test_a_date_in_another_shape_still_tells_graft_s_own_items(cfg, app, bundled: FakeTools, store) -> None:
    app(True)
    store.cdat_shape = "iso"

    assert _sign_in_graft(cfg) == FAKE_EMAIL

    recorded = appstore_account.vouched()
    assert set(recorded) == {"account", "anisette-v4"}
    assert all(value.startswith('"2026-') for value in recorded.values()), recorded
    ipa = _status(cfg)
    assert (ipa["signed_in"], ipa["legacy_signin"]) == (True, False)
    assert bundled.dialogs() == []


# --- A Get meeting another ipatool's cache or device item (the fix re-check's
# problem 6). ipatool 2.8.0 reads and saves `kbsync-v1` (cachedKBSync,
# storeKBSync) and `anisette-v4` (storeAMDProof) on every download, and a
# login makes no kbsync-v1, so the first download after one does.


@pytest.fixture
def get_cfg(temp_config_env: Path, bundled: FakeTools, tmp_path: Path):
    """Graft.app's ipatool, with the bundled fake's model, so a Get runs."""
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = {"unfairplay_models": str(bundled.models)}
    path = tmp_path / "get.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(path)


def _get(cfg) -> None:
    with appstore.fetched(cfg, "example", emitter=None) as ipa:
        assert ipa.exists()


def _downloads(fakes: FakeTools) -> int:
    return len([c for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]])


def _download_directly(fakes: FakeTools, tmp_path: Path) -> None:
    """`ipatool download` in Terminal, with no Graft around it."""
    out = tmp_path / f"by-hand-{fakes.root.name}"
    out.mkdir(exist_ok=True)
    subprocess.run([str(fakes.ipatool), "download", "-b", "com.example.app", "-o", str(out), "--format", "json"],
                   check=True, capture_output=True, timeout=30)


def test_a_get_after_the_users_own_ipatool_made_the_cache_raises_no_dialog(
    get_cfg, cfg, app, bundled: FakeTools, own: FakeTools, store, tmp_path: Path,
) -> None:
    """The reviewer's scenario: Graft.app signs in, the user's own ipatool
    downloads in Terminal (allowing its dialogs) and so makes kbsync-v1, and
    then Graft.app gets an app. The cache is deleted first (no dialog, K2b),
    Graft's ipatool makes its own, and the next Get keeps that one."""
    app(True)
    assert _sign_in_graft(cfg) == FAKE_EMAIL
    own.set(keychain_answer="allow")
    _download_directly(own, tmp_path)
    assert "kbsync-v1" in _ours(store, own)

    store.calls.clear()
    _get(get_cfg)

    assert bundled.dialogs() == [], "Graft's ipatool never met another program's item"
    assert _deletes(store) == ["kbsync-v1"]
    assert "kbsync-v1" in _ours(store, bundled)
    assert appstore_account.vouched()["kbsync-v1"] == store.created[(IPATOOL_SERVICE, "kbsync-v1")]

    store.calls.clear()
    _get(get_cfg)
    assert _deletes(store) == [], "Graft's own cache, recorded after its Get, is kept"
    assert bundled.dialogs() == []
    assert _downloads(bundled) == 2


def test_a_get_with_another_ipatool_s_device_item_is_refused_before_ipatool_runs(
    get_cfg, cfg, app, bundled: FakeTools, own: FakeTools, store,
) -> None:
    """A foreign anisette-v4 is read on every download, and without one ipatool
    can't download at all (only a login provisions it), so it is neither
    deleted nor met: the Get is refused with words, and a sign-in replaces it."""
    app(True)
    _sign_in_graft(cfg)
    store.remove(IPATOOL_SERVICE, "anisette-v4")
    store.add(IPATOOL_SERVICE, "anisette-v4", owner=str(own.root))

    with pytest.raises(appstore.AppStoreError) as e:
        _get(get_cfg)

    assert "anisette-v4" in e.value.message and "another ipatool" in e.value.message
    assert "signing in replaces it" in (e.value.hint or "")
    assert _downloads(bundled) == 0
    assert bundled.dialogs() == []
    assert _deletes(store) == [], "not deleted: that would only make every download fail"
    # Delta audit #8: the sign-in is Graft's, so Settings shows Sign In
    # Again… and Sign Out, not Sign In…: the hint names the button there.
    assert _buttons(appstore.status(get_cfg.appstore)) == {SIGN_IN_AGAIN, "Sign Out"}
    assert e.value.hint == f"{ipatool.RENEW_HINT}; signing in replaces it"
    assert _named_buttons(e.value.hint) == {SIGN_IN_AGAIN}


def test_a_cache_that_can_t_be_deleted_stops_the_get_before_ipatool_runs(
    get_cfg, cfg, app, bundled: FakeTools, own: FakeTools, store, tmp_path: Path,
) -> None:
    app(True)
    _sign_in_graft(cfg)
    own.set(keychain_answer="allow")
    _download_directly(own, tmp_path)
    store.fail_deletes = True

    with pytest.raises(appstore.AppStoreError) as e:
        _get(get_cfg)

    assert "kbsync-v1" in e.value.message
    assert _downloads(bundled) == 0
    assert bundled.dialogs() == []
    # Delta audit #8: a sign-in isn't what clears it (it meets the same
    # delete), so the hint says to try the Get again, not to sign in.
    assert e.value.hint == appstore_account.CLEAR_CACHE_HINT
    assert e.value.hint.startswith("try again (no need to sign in again)")
    assert "then sign in again" not in e.value.hint
    assert _named_buttons(e.value.hint) == set()
    store.fail_deletes = False
    _get(get_cfg)
    assert _downloads(bundled) == 1, "the Get alone, tried again, goes through"


def test_the_first_get_after_a_sign_in_records_graft_s_own_cache(get_cfg, cfg, app, bundled: FakeTools, store) -> None:
    """As the owner's real run on try12 saw: a sign-in records `account` and
    `anisette-v4` only (a login makes no kbsync-v1), and the first Get makes
    kbsync-v1 with Graft's ipatool. It goes on the record, so the next Get
    doesn't delete Graft's own cache."""
    app(True)
    _sign_in_graft(cfg)
    assert set(appstore_account.vouched()) == {"account", "anisette-v4"}

    _get(get_cfg)

    store.load()
    assert appstore_account.vouched() == {
        name: store.created[(IPATOOL_SERVICE, name)] for name in ("account", "anisette-v4", "kbsync-v1")
    }
    store.calls.clear()
    _get(get_cfg)
    assert _deletes(store) == []
    assert bundled.dialogs() == []


def test_another_program_s_cache_met_by_a_get_is_never_adopted(
    get_cfg, cfg, app, bundled: FakeTools, own: FakeTools, store, tmp_path: Path, monkeypatch,
) -> None:
    """A kbsync-v1 another program made, there before the download and still
    there after it with its own date (here, one that appeared just after the
    clear: the clear is skipped), stays off the record."""
    app(True)
    _sign_in_graft(cfg)
    own.set(keychain_answer="allow")
    _download_directly(own, tmp_path)
    monkeypatch.setattr(appstore_account, "clear_foreign_cache", lambda source: False)

    _get(get_cfg)

    assert "kbsync-v1" in _ours(store, own), "Graft's ipatool was refused it, so it is unchanged"
    assert "kbsync-v1" not in appstore_account.vouched()


def _lookup(exists, created=None):
    return keychain.Lookup(exists, created)


@pytest.mark.parametrize(
    ("before", "adopted"),
    [
        (_lookup(False), True),  # made by this run
        (_lookup(True, "20261101120000Z"), True),  # remade by it (invalidateKBSync): a new date
        (None, False),  # not the bundled ipatool
        (_lookup(None), False),  # security couldn't say then
    ],
)
def test_a_cache_is_adopted_only_when_the_run_made_it(cfg, app, bundled: FakeTools, store, before, adopted) -> None:
    app(True)
    _sign_in_graft(cfg)
    made = store.add(IPATOOL_SERVICE, "kbsync-v1", owner=str(bundled.root))

    assert appstore_account.adopt_cache(tools.SOURCE_BUNDLED, before) is adopted
    assert ("kbsync-v1" in appstore_account.vouched()) is adopted
    if adopted:
        assert appstore_account.vouched()["kbsync-v1"] == made


def test_a_cache_there_before_with_the_same_date_is_never_adopted(cfg, app, bundled: FakeTools, store) -> None:
    app(True)
    _sign_in_graft(cfg)
    made = store.add(IPATOOL_SERVICE, "kbsync-v1")  # another program's

    assert appstore_account.adopt_cache(tools.SOURCE_BUNDLED, _lookup(True, made)) is False
    assert "kbsync-v1" not in appstore_account.vouched()
    assert appstore_account.adopt_cache(tools.SOURCE_DEFAULT, _lookup(False)) is False


def test_a_claim_makes_no_cache_and_adopts_none(cfg, app, bundled: FakeTools, own: FakeTools, store,
                                                tmp_path: Path, monkeypatch) -> None:
    """ipatool's purchase touches no kbsync-v1: a claim leaves the record as
    it was, and still deletes another program's cache first."""
    from tests.test_appstore_claim import _listing

    _listing(monkeypatch, 0.0, "Free")
    app(True)
    _sign_in_graft(cfg)
    own.set(keychain_answer="allow")
    _download_directly(own, tmp_path)
    recorded = appstore_account.vouched()

    appstore.claim(cfg, "example", emitter=None, say=lambda _: None)

    assert _deletes(store)[-1:] == ["kbsync-v1"]
    assert appstore_account.vouched() == recorded
    assert bundled.dialogs() == []


def test_no_cache_is_adopted_onto_a_record_that_vouches_for_no_account(cfg, app, own: FakeTools, store) -> None:
    """After a sign-in through the user's own ipatool the record vouches for
    nothing, and a cache isn't the first thing it starts vouching for."""
    app(False)
    _sign_in_graft(cfg)
    assert appstore_account.vouched() == {}
    store.add(IPATOOL_SERVICE, "kbsync-v1")

    assert appstore_account.adopt_cache(tools.SOURCE_BUNDLED, _lookup(False)) is False
    assert appstore_account.vouched() == {}


@pytest.mark.parametrize("verb", ["get", "claim"])
def test_a_keychain_that_locks_during_the_download_is_not_asked_after_it(
    get_cfg, cfg, app, bundled: FakeTools, store, monkeypatch, verb: str,
) -> None:
    """The try13 delta audit's #16: the lock was read only before the
    download, which can take many minutes, and the cache's adoption then
    asked the keychain whatever it had become since: on a locked one that
    lookup can wait, or ask for its password. It is read again first, and a
    locked keychain is not asked: the Get or claim still finishes, and the
    cache just isn't recorded (it is deleted before the next Get)."""
    from tests.test_appstore_claim import _listing

    _listing(monkeypatch, 0.0, "Free")
    app(True)
    _sign_in_graft(cfg)
    name = "download" if verb == "get" else "purchase"
    real = getattr(ipatool, name)
    locked_at: list[int] = []

    def then_lock(*args, **kwargs):
        result = real(*args, **kwargs)
        monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
        locked_at.append(len(store.calls))
        return result

    monkeypatch.setattr(ipatool, name, then_lock)
    if verb == "get":
        _get(get_cfg)
    else:
        appstore.claim(cfg, "example", emitter=None, say=lambda _: None)

    assert locked_at, f"the {name} ran"
    assert store.calls[locked_at[0]:] == [], "nothing asked the locked keychain"
    assert "kbsync-v1" not in appstore_account.vouched()
    assert bundled.dialogs() == []


def test_a_locked_keychain_adopts_nothing_and_is_not_asked(cfg, app, bundled: FakeTools, store, monkeypatch) -> None:
    app(True)
    _sign_in_graft(cfg)
    store.add(IPATOOL_SERVICE, "kbsync-v1", owner=str(bundled.root))
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    store.calls.clear()

    assert appstore_account.adopt_cache(tools.SOURCE_BUNDLED, _lookup(False)) is False
    assert store.calls == []
    assert "kbsync-v1" not in appstore_account.vouched()


# --- A locked login keychain (the owner's try12 round) ------------------------
#
# `security find-generic-password` without -g still answered on a locked
# login keychain, so status ran the bundled `auth info`: an unlock dialog for
# ipatool, then five more for Graft's own `security`. The lock is now read up
# front, with no UI (`keychain.login_keychain_locked`), and nothing asks the
# keychain while it is locked.


@pytest.fixture
def locked(monkeypatch: pytest.MonkeyPatch, store: FakeSecurity):
    """The lock check says locked (the seam conftest fakes as unlocked)."""
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    return store


@pytest.mark.parametrize("inside", [True, False], ids=["graft-app", "pipx"])
def test_status_with_a_locked_keychain_asks_nothing(cfg, app, bundled: FakeTools, own: FakeTools, store,
                                                    inside: bool, monkeypatch) -> None:
    app(inside)
    _sign_in_graft(cfg)
    who = bundled if inside else own
    infos = _auth_infos(who)
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    store.calls.clear()

    ipa = _status(cfg)

    assert (ipa["signed_in"], ipa["legacy_signin"]) == (None, False)
    assert ipa["unknown_reason"] == "keychain-locked"
    assert ipa["unknown_message"] == ipatool.UNKNOWN_MESSAGES["keychain-locked"]
    assert "login keychain is locked" in ipa["unknown_message"] and "Unlock it" in ipa["unknown_message"]
    assert _auth_infos(who) == infos, "no ipatool asked: that was the first dialog"
    assert store.calls == [], "no `security` either: those were the other five"
    assert who.dialogs() == []


def test_status_with_the_keychain_locked_says_so_in_terminal(cfg, app, bundled: FakeTools, locked) -> None:
    app(True)
    said: list[str] = []
    appstore.report_status(cfg, emitter=None, say=said.append)
    text = "\n".join(said)
    assert "couldn't tell whether it is signed in (the login keychain is locked)" in text
    assert "unlock the login keychain" in text


def test_a_get_with_a_locked_keychain_is_refused_before_anything_asks(
    get_cfg, cfg, app, bundled: FakeTools, store, monkeypatch,
) -> None:
    app(True)
    _sign_in_graft(cfg)
    infos = _auth_infos(bundled)
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    store.calls.clear()

    with pytest.raises(appstore.AppStoreError) as e:
        _get(get_cfg)

    assert e.value.message == "couldn't tell whether ipatool is signed in: your login keychain is locked"
    assert "unlock the login keychain" in (e.value.hint or "")
    assert (_auth_infos(bundled), _downloads(bundled)) == (infos, 0)
    assert store.calls == []


def test_a_claim_with_a_locked_keychain_is_refused_before_anything_asks(cfg, app, bundled: FakeTools, store,
                                                                         monkeypatch) -> None:
    from tests.test_appstore_claim import _listing

    _listing(monkeypatch, 0.0, "Free")
    app(True)
    _sign_in_graft(cfg)
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    store.calls.clear()

    with pytest.raises(appstore.AppStoreError) as e:
        appstore.claim(cfg, "example", emitter=None, say=lambda _: None)

    assert "login keychain is locked" in e.value.message
    assert [c for c in bundled.calls("ipatool") if c["argv"][:1] == ["purchase"]] == []
    assert store.calls == []


def test_a_sign_in_with_a_locked_keychain_reads_nothing_and_deletes_nothing(
    cfg, app, bundled: FakeTools, own: FakeTools, store, locked,
) -> None:
    app(False)
    _sign_in_directly(own)  # another ipatool's items, which a sign-in would delete
    app(True)
    store.calls.clear()
    stdin = Stdin(EMAIL, PASSWORD)

    outcome, events = _run(cfg, stdin)

    assert isinstance(outcome, appstore_login.LoginFailed)
    assert outcome.reason == appstore_login.REASON_KEYCHAIN_LOCKED == "keychain-locked"
    assert events == [{"type": "appstore-login", "state": "failed", "reason": "keychain-locked",
                       "message": outcome.message, "hint": ipatool.KEYCHAIN_LOCKED_HINT}]
    assert "login keychain is locked" in outcome.message
    assert stdin.asked == [], "no Apple ID or password read"
    assert store.calls == [], "nothing looked up, nothing deleted"
    assert bundled.login_runs() == ([], [])


def test_a_sign_out_with_a_locked_keychain_runs_no_revoke(cfg, app, bundled: FakeTools, store, monkeypatch) -> None:
    app(True)
    _sign_in_graft(cfg)
    monkeypatch.setattr(keychain, "_lock_probe", lambda: True)
    store.calls.clear()

    with pytest.raises(appstore.AppStoreError) as e:
        appstore_login.sign_out(cfg, emitter=None, say=lambda _: None)

    assert "login keychain is locked" in e.value.message
    assert [c for c in bundled.calls("ipatool") if c["argv"][:2] == ["auth", "revoke"]] == []
    assert store.calls == []


# --- Delta audit #8: a hint names only a button that is on screen ----------------

from tests.test_appstore_contract_pins import _swift_enum  # noqa: E402

SIGN_IN = _swift_enum("SettingsLabel")["signIn"]
SIGN_IN_AGAIN = _swift_enum("SettingsLabel")["signInAgain"]


def _buttons(status: dict) -> set[str]:
    """The account buttons Settings > App Store shows beside the ipatool row
    for `status`, as AppStoreSettingsSection.accountButtons decides them:
    none without an ipatool or a verdict, Sign In… for a sign-in Graft's
    ipatool can't read or none at all, and Sign In Again… with Sign Out
    while signed in."""
    ipa = status["ipatool"]
    if not ipa["present"]:
        return set()
    if ipa["legacy_signin"]:
        return {SIGN_IN}
    return {True: {SIGN_IN_AGAIN, "Sign Out"}, False: {SIGN_IN}}.get(ipa["signed_in"], set())


def _named_buttons(hint: str | None) -> set[str]:
    return {label for label in (SIGN_IN, SIGN_IN_AGAIN) if label in (hint or "")}


def test_a_get_whose_renewal_needs_a_code_names_the_button_settings_shows(
    get_cfg, cfg, app, bundled: FakeTools, store,
) -> None:
    """RENEW_HINT names Sign In Again…, which Settings shows only while
    status reads signed in: so it is right only where that holds, as it does
    for an expired sign-in (`auth info` reads only the keychain)."""
    app(True)
    _sign_in_graft(cfg)
    bundled.set(download_error="auth code is required")
    with pytest.raises(appstore.AppStoreError) as e:
        _get(get_cfg)
    assert e.value.hint == ipatool.RENEW_HINT
    assert _named_buttons(e.value.hint) == {SIGN_IN_AGAIN}
    assert _named_buttons(e.value.hint) <= _buttons(appstore.status(get_cfg.appstore))


def test_the_renew_hint_is_used_only_where_status_reads_signed_in() -> None:
    """Each place RENEW_HINT (Sign In Again…) is given, and why status reads
    signed in there: a new one has to be looked at, and added here."""
    import ast
    import inspect
    from collections import Counter

    from graft_ipa import appstore as appstore_mod

    uses: Counter[str] = Counter()
    for module in (ipatool, appstore_mod, appstore_login, appstore_account):
        tree = ast.parse(inspect.getsource(module))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Name) and node.id == "RENEW_HINT") or (
                            isinstance(node, ast.Attribute) and node.attr == "RENEW_HINT"):
                        uses[f"{module.__name__.rsplit('.', 1)[-1]}.{fn.name}"] += 1
    assert uses == {
        # Each once: ipatool asked for a code to renew a sign-in `auth info`
        # still reads (AUTH_CODE_REQUIRED).
        "ipatool.explain": 1, "ipatool.explain_purchase": 1,
        # After `account` read signed in (and not another ipatool's).
        "appstore._require_signed_in": 1,
    }


def _doc(name: str) -> str:
    return " ".join((Path(__file__).resolve().parent.parent / name).read_text().split())


def test_the_docs_say_another_ipatool_s_sign_in_is_saved_over_not_deleted() -> None:
    """The try15 delta audit's d15-app-2: network.md said Graft deletes every
    item the running ipatool didn't make. Any ipatool but Graft.app's own
    deletes only the items Graft's record vouches for
    (`appstore_account.needs_clearing`), and its sign-in saves over a third
    ipatool's, where macOS may ask for the login keychain password. That is
    what the sign-in sheet says (`otherLegacyNote`), so the page says it too."""
    text = _doc("docs/network.md")
    assert "deletes the ones the ipatool that runs didn't make" not in text
    assert "saved over" in text and "may ask for your login keychain password" in text


def test_the_upgrade_guide_says_your_own_ipatool_s_sign_in_deletes_graft_s() -> None:
    """d15-app-1's docs half: upgrading.md tells you how to keep your own
    ipatool, so it says what signing in with it does to Graft.app's own
    sign-in, which `needs_clearing` deletes first (the audit ran it with a
    config source: all three vouched items)."""
    text = _doc("docs/upgrading.md")
    assert "deletes the sign-in Graft.app's own ipatool made" in text
    assert "saved over" in text


# --- The try15 delta audit's d15-py-1 and d15-py-2: the same file, however named ---------


def _graft_app(root: Path, store: FakeSecurity, bundle_id: str = ipatool.GRAFT_APP_BUNDLE_ID) -> FakeTools:
    """An app bundle at `root` whose Contents/Helpers/bin holds a fake ipatool."""
    import plistlib

    fakes = _fake(root / "Contents" / "Helpers" / "bin", store)
    (root / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    return fakes


def _counts_as_graft_s_own(configured, graft: FakeTools, store: FakeSecurity, vouched: dict) -> None:
    """signin-A's outcome: the bundled rung, run by its own path; signed
    in; and a sign-in that deletes nothing and keeps the record."""
    tools_cfg = configured.appstore
    assert ipatool.source_of(tools_cfg.ipatool, explicit=True) == tools.SOURCE_BUNDLED
    assert ipatool.locate(tools_cfg.ipatool, explicit=True) == graft.ipatool
    ipa = _status(configured)
    assert (ipa["source"], ipa["signed_in"], ipa["legacy_signin"], ipa["unknown_reason"]) == \
        (tools.SOURCE_BUNDLED, True, False, None)
    assert appstore_account.needs_clearing(tools.SOURCE_BUNDLED) == []
    appstore._require_signed_in(tools_cfg, ipatool.locate(tools_cfg.ipatool, explicit=True))
    store.calls.clear()
    assert _sign_in_graft(configured) == FAKE_EMAIL
    assert _deletes(store) == [], "Graft's own items are not another program's"
    assert set(appstore_account.vouched()) == set(vouched)
    assert graft.dialogs() == []


@pytest.mark.parametrize("how", ["its own path", "a link to it"])
def test_a_pipx_graft_set_to_graft_app_s_own_ipatool_counts_it_as_graft_s_own(
    cfg, store, temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    """d15-py-1: signin-A held inside Graft.app only. A pipx `graft` has no
    app of its own, so the same setting was "another ipatool" to it: its
    sign-in deleted Graft's items and emptied the record, and Graft.app's
    next one deleted that sign-in's. Both now count the file in Graft.app
    as Graft.app's own."""
    graft = _graft_app(tmp_path / "GraftA.app", store)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: graft.root)
    _sign_in_graft(cfg)
    vouched = appstore_account.vouched()
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    named = graft.ipatool
    if how == "a link to it":
        named = tmp_path / "bin" / "ipatool"
        named.parent.mkdir()
        named.symlink_to(graft.ipatool)
    _counts_as_graft_s_own(_naming(temp_config_env, tmp_path, named, graft.models), graft, store, vouched)


def test_a_pipx_graft_set_to_another_app_s_ipatool_still_counts_it_as_configured(
    store, temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the app it lies in has to be Graft.app."""
    other = _graft_app(tmp_path / "Other.app", store, bundle_id="com.example.other")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    configured = _naming(temp_config_env, tmp_path, other.ipatool, other.models)
    assert ipatool.source_of(configured.appstore.ipatool, explicit=True) == tools.SOURCE_CONFIG
    assert not ipatool.is_bundled(other.ipatool)


@pytest.mark.parametrize("how", ["a hard link", "another case", "the Data volume's spelling"])
def test_graft_s_own_ipatool_named_another_way_is_graft_s_own(
    cfg, app, bundled: FakeTools, store, temp_config_env: Path, tmp_path: Path, how: str,
) -> None:
    """d15-py-2: realpath doesn't fold case, map the /System/Volumes/Data
    firmlink or see a hard link, so each of these, the very same file,
    churned the sign-in as signin-A did. Compared by device and inode now."""
    import os

    app(True)
    _sign_in_graft(cfg)
    vouched = appstore_account.vouched()
    if how == "a hard link":
        named = tmp_path / "hard" / "ipatool"
        named.parent.mkdir()
        os.link(bundled.ipatool, named)
    elif how == "another case":
        named = bundled.ipatool.parent.with_name(bundled.ipatool.parent.name.upper()) / "IPATOOL"
    else:
        named = Path("/System/Volumes/Data" + os.path.realpath(bundled.ipatool))
    assert named != bundled.ipatool and os.path.samefile(named, bundled.ipatool), "premise"
    assert os.path.realpath(named) != os.path.realpath(bundled.ipatool) or how == "a hard link"
    _counts_as_graft_s_own(_naming(temp_config_env, tmp_path, named, bundled.models), bundled, store, vouched)
