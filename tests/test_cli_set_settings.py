"""Writing global settings from the GUI.

Everything the Settings pane could show, it could not change: the default
device, retention, and the signing paths all meant hand-editing config.yaml.
The contract these tests pin is the one the GUI reconciles against — the
returned `changed` list must name exactly the keys that reached disk, because
the pane refuses to report success for a field it asked for and did not get
back.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa.config import (
    DEFAULT_ARCHIVE_KEEP,
    SETTINGS,
    load_config,
    read_settings,
    set_settings,
)
from graft_ipa.errors import ConfigError
from graft_ipa.json_output import JsonEmitter


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_writes_a_top_level_setting(temp_config_env: Path):
    assert set_settings(temp_config_env, archive_keep=5) == ["archive_keep"]
    assert _raw(temp_config_env)["archive_keep"] == 5
    assert load_config(temp_config_env).archive_keep == 5


def test_writes_a_nested_signing_setting(temp_config_env: Path):
    assert set_settings(temp_config_env, signing_team_id="ABCDE12345") == ["signing_team_id"]
    assert _raw(temp_config_env)["signing"]["team_id"] == "ABCDE12345"


def test_is_a_patch_not_a_replace(temp_config_env: Path):
    """The whole point: writing one field must not disturb the apps, the repos,
    or the rest of the signing block."""
    before = _raw(temp_config_env)
    set_settings(temp_config_env, archive_keep=4)
    after = _raw(temp_config_env)

    assert after["apps"] == before["apps"]
    assert after.get("repos") == before.get("repos")
    assert after["signing"]["cert"] == before["signing"]["cert"]
    assert after["signing"]["team_id"] == before["signing"]["team_id"]


def test_nothing_to_do_writes_no_file(temp_config_env: Path):
    """A call with every option unset must not rewrite config.yaml — this runs
    behind a Save button, and `yaml.safe_dump` does not preserve comments."""
    before = temp_config_env.read_bytes()
    assert set_settings(temp_config_env) == []
    assert temp_config_env.read_bytes() == before


def test_changed_names_only_what_was_asked_for(temp_config_env: Path):
    changed = set_settings(temp_config_env, archive_keep=3, signing_team_id="ZZZZZ99999")
    assert changed == ["archive_keep", "signing_team_id"]


def test_an_empty_default_device_unsets_it(temp_config_env: Path):
    """"No default, let the CLI pick" is a real choice, made from a picker with
    a blank row — and it must remove the key rather than store "", which
    `select_device` would treat as a udid."""
    set_settings(temp_config_env, default_device_udid="ABC")
    assert _raw(temp_config_env)["default_device_udid"] == "ABC"

    assert set_settings(temp_config_env, default_device_udid="") == ["default_device_udid"]
    assert "default_device_udid" not in _raw(temp_config_env)
    assert load_config(temp_config_env).default_device_udid is None


@pytest.mark.parametrize("key", ["signing_cert", "signing_profile", "signing_team_id"])
def test_a_signing_field_cannot_be_blanked(temp_config_env: Path, key: str):
    """Unlike the device, these have no meaningful empty value: a build cannot
    sign without them. Blanking one from a text field that lost focus would
    turn a working config into a broken one."""
    with pytest.raises(ConfigError):
        set_settings(temp_config_env, **{key: "   "})


def test_archive_keep_accepts_zero(temp_config_env: Path):
    """0 is a documented value everywhere else — `_archive_keep` validates
    non-negative and its hint says "use 0 to keep only the archive the current
    build just produced", and `--prune-builds --keep 0` is accepted. Refusing
    it here meant a user already running 0 could nudge the stepper once and
    never get back."""
    assert set_settings(temp_config_env, archive_keep=0) == ["archive_keep"]
    assert load_config(temp_config_env).archive_keep == 0


def test_archive_keep_refuses_negative(temp_config_env: Path):
    with pytest.raises(ConfigError):
        set_settings(temp_config_env, archive_keep=-1)


def test_archive_keep_rejects_nonsense(temp_config_env: Path):
    with pytest.raises(ConfigError):
        set_settings(temp_config_env, archive_keep="lots")


def test_an_unknown_setting_is_refused_by_name(temp_config_env: Path):
    with pytest.raises(ConfigError) as excinfo:
        set_settings(temp_config_env, cache_dir="/tmp/x")
    assert "cache_dir" in str(excinfo.value)


def test_a_rejected_write_leaves_the_file_untouched(temp_config_env: Path):
    """Validation happens before the write, so a bad field in a multi-field
    save does not half-apply the good ones."""
    before = temp_config_env.read_bytes()
    with pytest.raises(ConfigError):
        set_settings(temp_config_env, archive_keep=2, signing_team_id="")
    assert temp_config_env.read_bytes() == before


def test_paths_are_expanded_but_not_required_to_exist(temp_config_env: Path):
    """A cert the user is about to create, or one on an unmounted volume, is
    not an error at write time — `--check` is what reports it."""
    set_settings(temp_config_env, signing_cert="~/nowhere/absent.p12")
    written = _raw(temp_config_env)["signing"]["cert"]
    assert written.startswith("/")
    assert "~" not in written


def test_signing_block_is_not_created_when_absent(temp_config_env: Path):
    """No `signing:` section is a free Apple ID now. This used to write
    `signing: {team_id: NEW}` — one key of four, which `load_config` rejects,
    so every other verb then failed on a config that had just worked. Becoming
    paid is `--init` or a whole section added on purpose; see
    test_free_account_config.py for the refusal's wording."""
    from graft_ipa.errors import PaidAccountRequiredError

    raw = _raw(temp_config_env)
    del raw["signing"]
    temp_config_env.write_text(yaml.safe_dump(raw))

    with pytest.raises(PaidAccountRequiredError):
        set_settings(temp_config_env, signing_team_id="NEW")
    assert "signing" not in _raw(temp_config_env)


def test_the_verb_emits_what_changed_and_the_state_that_produced(temp_config_env: Path):
    """Both events, in this order — the GUI's save is one process because of it.

    The pane used to spawn `--settings` after every write to learn what it had
    just written: a second interpreter start doing no work above startup, for a
    value this process already holds. Reading it here also closes the window in
    which something else could edit the file in between.
    """
    from graft_ipa import cli as cli_mod

    out = StringIO()
    cli_mod._do_set_settings(
        temp_config_env, emitter=JsonEmitter(stream=out), archive_keep=6
    )

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["settings-updated", "settings"]
    assert events[0]["changed"] == ["archive_keep"]
    assert events[1]["archive_keep"] == 6, "the reported state predates the write"
    # The same shape `--settings` emits, less `paths`: a write only lands in a
    # config that parsed (C-SETTINGS). `app_store_connect` included, or the
    # pane would show the key as not set up after every save.
    assert set(events[1]) == {
        "type", "parse_error", "paths_are_defaults", "app_store_connect", "graft_version", *SETTINGS,
    }
    assert (events[1]["parse_error"], events[1]["paths_are_defaults"]) == (None, False)


# --- the mapping and the sibling preservation the mutation runs found bare ---


def test_each_cli_option_reaches_its_own_config_key(temp_config_env: Path):
    """The parameter-to-key mapping in `_verb_set_settings` had no test:
    exchanging `signing_cert` and `signing_profile` in the call left the whole
    suite green while the GUI wrote the profile path into `signing.cert`, and
    the next build failed to sign with a confusing error about a file type.

    Driven through the verb, not `set_settings`, because the verb is where the
    mapping lives.
    """
    from graft_ipa import cli as cli_mod

    cli_mod._verb_set_settings(
        config_path=temp_config_env,
        default_device="UDID-X",
        archive_keep=9,
        signing_cert="/certs/mine.p12",
        signing_profile="/profiles/mine.mobileprovision",
        signing_team_id="TEAMX12345",
        json_mode=False,
    )

    raw = _raw(temp_config_env)
    assert raw["default_device_udid"] == "UDID-X"
    assert raw["archive_keep"] == 9
    assert raw["signing"]["cert"] == "/certs/mine.p12"
    assert raw["signing"]["profile"] == "/profiles/mine.mobileprovision"
    assert raw["signing"]["team_id"] == "TEAMX12345"


def test_writing_one_signing_field_preserves_its_siblings(temp_config_env: Path):
    """The `isinstance(existing, dict)` reuse could be replaced with `{}` and
    every test still passed — which would wipe cert, cert_password_env and
    profile whenever team_id was written. That is the whole signing block
    destroyed by editing one field, in a file with no backup."""
    before = _raw(temp_config_env)["signing"]
    assert len(before) >= 3, "premise: there are siblings to lose"

    set_settings(temp_config_env, signing_team_id="ONLYTHIS12")

    after = _raw(temp_config_env)["signing"]
    assert after["team_id"] == "ONLYTHIS12"
    for key, value in before.items():
        if key != "team_id":
            assert after[key] == value, f"writing team_id lost signing.{key}"


def test_reading_settings_does_not_require_a_loadable_config(temp_config_env: Path):
    """The pane exists to repair a broken `signing:` block, so the read has to
    answer for one."""
    raw = _raw(temp_config_env)
    del raw["signing"]["team_id"]
    temp_config_env.write_text(yaml.safe_dump(raw))

    values = read_settings(temp_config_env)
    assert values["signing_team_id"] is None
    assert values["signing_cert"] == raw["signing"]["cert"]

    with pytest.raises(ConfigError):
        load_config(temp_config_env)


@pytest.mark.parametrize("stored,expected", [
    (False, "False"),
    (12345678, "12345678"),
])
def test_a_non_string_device_udid_is_coerced(temp_config_env, stored, expected):
    """YAML types what it finds: `default_device_udid: no` is a bool under YAML
    1.1 and an all-digit legacy UDID is an int. Both reached the GUI as
    non-strings, failed to decode, and were dropped by a `try?` that replaced
    the entire editor with "Couldn't read your settings." and said nothing."""
    raw = _raw(temp_config_env)
    raw["default_device_udid"] = stored
    temp_config_env.write_text(yaml.safe_dump(raw))

    assert read_settings(temp_config_env)["default_device_udid"] == expected


def test_a_missing_archive_keep_reports_the_default(temp_config_env: Path):
    raw = _raw(temp_config_env)
    raw.pop("archive_keep", None)
    temp_config_env.write_text(yaml.safe_dump(raw))
    assert read_settings(temp_config_env)["archive_keep"] == DEFAULT_ARCHIVE_KEEP


@pytest.mark.parametrize("stored", ["ten", True, [1], -3])
def test_an_unreadable_archive_keep_reports_nothing_not_the_default(
    temp_config_env: Path, stored
):
    """The lenient reader and the strict loader must not disagree about a value
    that drives a destructive prune.

    `_archive_keep` raises on all of these, on purpose — so substituting
    `DEFAULT_ARCHIVE_KEEP` here showed the pane a confident `2` for a config
    that every build refuses to run. Displaying a value that is not on disk is
    the exact failure the `changed` reconciliation exists to prevent, so this
    reports "can't read it" and lets the pane say so.
    """
    raw = _raw(temp_config_env)
    raw["archive_keep"] = stored
    temp_config_env.write_text(yaml.safe_dump(raw))

    assert read_settings(temp_config_env)["archive_keep"] is None
    with pytest.raises(ConfigError):
        load_config(temp_config_env)


@pytest.mark.parametrize("signing", ["just a string", ["a", "list"], 7])
def test_a_signing_block_that_is_not_a_mapping_reads_as_unset(temp_config_env: Path, signing):
    """`_dig` walks two levels, and the second level is whatever YAML found.

    The reader this replaced coerced a non-mapping `signing:` to `{}` before
    touching it. Walking the path without that check turns the same document
    into an AttributeError inside a listing that promises never to raise — and
    a config whose `signing:` is a bare string is precisely one somebody needs
    this editor to fix.
    """
    raw = _raw(temp_config_env)
    raw["signing"] = signing
    temp_config_env.write_text(yaml.safe_dump(raw))

    values = read_settings(temp_config_env)
    assert values["signing_cert"] is None
    assert values["signing_team_id"] is None


@pytest.mark.parametrize("signing", ["TODO", ["a", "list"], 7])
def test_a_signing_block_that_is_not_a_mapping_can_be_repaired(temp_config_env: Path, signing):
    """The reader above shows it as unset; the writer must then be able to set
    it. A `signing:` that is present but broken is a paid config being
    repaired, not a free one, so the free-account refusal does not apply: the
    non-mapping is replaced with a mapping holding what was written."""
    raw = _raw(temp_config_env)
    raw["signing"] = signing
    temp_config_env.write_text(yaml.safe_dump(raw))

    assert set_settings(temp_config_env, signing_team_id="ABCDE12345") == ["signing_team_id"]

    after = _raw(temp_config_env)
    assert after["signing"] == {"team_id": "ABCDE12345"}
    assert after["apps"] == raw["apps"]


def test_a_setting_with_no_flag_to_write_it_is_refused_at_the_verb(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--describe` publishes `setting_fields` straight from `SETTINGS`, so a
    key registered there and never wired to a flag has the CLI advertising a
    capability it does not have — the one thing `--describe`'s docstring
    promises cannot happen. The Swift contract test compares what the app sends
    against what the CLI declares, which is a subset check and stays green.

    Mirrors the `APP_OPTION_FIELDS` guard in `_do_set_app_options`.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import _Setting

    monkeypatch.setitem(
        SETTINGS, "cert_password_env", _Setting(("signing", "cert_password_env"), "text", "reject")
    )

    with pytest.raises(ValueError) as excinfo:
        cli_mod._verb_set_settings(
            config_path=temp_config_env,
            default_device=None, archive_keep=None, signing_cert=None,
            signing_profile=None, signing_team_id=None, json_mode=False,
        )
    assert "cert_password_env" in str(excinfo.value)


def test_a_text_setting_is_not_path_expanded(temp_config_env: Path):
    """`kind` decides this, and it is the answer an if-chain used to guess.

    The replaced code expanded everything that was not `signing_team_id` by
    name, so the next setting registered inherited "expand me" by falling off
    the end of the chain — and the next candidate is `signing.cert_password_env`,
    an environment variable *name*, which would have been silently turned into
    a path under $HOME.
    """
    set_settings(temp_config_env, default_device_udid="~UDID")
    assert _raw(temp_config_env)["default_device_udid"] == "~UDID"


def test_options_not_given_are_skipped_not_written_as_none(temp_config_env: Path):
    """`_verb_set_settings` passes all five keywords every time, with `None`
    for the ones absent from argv — so the `value is None` skip is what stops
    `str(None).strip()` writing the literal string "None" into signing.cert.

    Driven through the verb, because that is the caller that always passes the
    full set; calling `set_settings` with two kwargs cannot reach the bug.
    """
    from graft_ipa import cli as cli_mod

    before = _raw(temp_config_env)
    cli_mod._verb_set_settings(
        config_path=temp_config_env,
        default_device=None,
        archive_keep=None,
        signing_cert=None,
        signing_profile=None,
        signing_team_id="ONLYTEAM12",
        json_mode=False,
    )

    after = _raw(temp_config_env)
    assert after["signing"]["team_id"] == "ONLYTEAM12"
    assert after["signing"]["cert"] == before["signing"]["cert"]
    assert after["signing"]["profile"] == before["signing"]["profile"]
    assert "None" not in str(after), "a skipped option was written as the string None"
    assert after.get("default_device_udid") == before.get("default_device_udid")


def test_a_call_with_every_option_none_writes_nothing(temp_config_env: Path):
    """The no-op path the GUI's disabled Save button relies on."""
    from graft_ipa import cli as cli_mod

    before = temp_config_env.read_bytes()
    cli_mod._verb_set_settings(
        config_path=temp_config_env,
        default_device=None, archive_keep=None, signing_cert=None,
        signing_profile=None, signing_team_id=None, json_mode=False,
    )
    assert temp_config_env.read_bytes() == before


# --- the team ID is checked as one read off a profile is (2.0 review SEC-4) ---


@pytest.mark.parametrize("team", [
    "ABCDE12345\ndelete-keychain login.keychain",
    'ABCDE"1234',
    "abcde12345",
    "ABCDE1234",
    "A" * 4100 + " show-keychain-info /nowhere.keychain",
])
def test_a_team_id_apple_would_never_issue_is_refused(temp_config_env: Path, team: str):
    """Settings' Team ID and `--set-settings --signing-team-id` reach the
    same `security -i` line as a team off a profile (`--set-p12-password`
    turns it into the account), so they get the same check."""
    before = temp_config_env.read_bytes()
    with pytest.raises(ConfigError, match="isn't an Apple team ID") as err:
        set_settings(temp_config_env, signing_team_id=team)
    assert temp_config_env.read_bytes() == before
    assert "show-keychain-info" not in err.value.message, "a long value is not repeated back"
