"""A config with no `signing:` section is a free Apple ID, and it loads.

Free mode: LiveContainer is on the device already, installed and refreshed by
SideStore, AltStore, iloader or Impactor with the user's free Apple ID. Graft
pushes unsigned IPAs into it and signs nothing, so the config has no signing
identity to describe. What follows from that is pinned here: the config loads,
it says which mode it is, an app with no `delivery:` key is a LiveContainer
app, and nothing writes half a signing section into it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa.config import (
    ACCOUNT_FREE,
    ACCOUNT_PAID,
    SETTINGS,
    account_of,
    add_app,
    load_config,
    set_settings,
    write_default_config,
)
from graft_ipa.errors import ConfigError, PaidAccountRequiredError


def _free(tmp_path: Path, apps: dict | None = None) -> Path:
    path = tmp_path / "config.yaml"
    raw: dict = {"cache_dir": str(tmp_path / "ipas")}
    if apps is not None:
        raw["apps"] = apps
    path.write_text(yaml.safe_dump(raw))
    return path


def _paid(tmp_path: Path, apps: dict | None = None) -> Path:
    path = tmp_path / "config.yaml"
    raw: dict = {
        "signing": {
            "cert": str(tmp_path / "c.p12"),
            "cert_password_env": "X",
            "profile": str(tmp_path / "p.mobileprovision"),
            "team_id": "ABCDE12345",
        },
    }
    if apps is not None:
        raw["apps"] = apps
    path.write_text(yaml.safe_dump(raw))
    return path


def test_a_config_without_signing_loads_as_a_free_account(tmp_path: Path) -> None:
    cfg = load_config(_free(tmp_path))

    assert cfg.signing is None
    assert cfg.free_account is True
    assert cfg.account == ACCOUNT_FREE == "free"
    assert cfg.team_id is None


def test_a_config_with_signing_is_paid(tmp_path: Path) -> None:
    cfg = load_config(_paid(tmp_path))

    assert cfg.free_account is False
    assert cfg.account == ACCOUNT_PAID == "paid"
    assert cfg.team_id == "ABCDE12345"


@pytest.mark.parametrize("body", ["signing:\n", "signing: {}\n", "signing: null\n"])
def test_an_empty_signing_heading_is_free_too(tmp_path: Path, body: str) -> None:
    """Commenting out every key under `signing:` leaves the heading behind; that
    says "no signing identity" just as deleting it does."""
    path = tmp_path / "config.yaml"
    path.write_text(body)

    assert load_config(path).free_account is True
    assert account_of(path) == ACCOUNT_FREE


def test_a_signing_section_that_is_not_a_mapping_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("signing: ABCDE12345\n")

    with pytest.raises(ConfigError, match="'signing' must be a mapping"):
        load_config(path)


def test_a_partial_signing_section_is_still_an_error(tmp_path: Path) -> None:
    """Free mode is *no* section. Half of one is a broken paid config, and
    reading it as free would hide the half the user did write."""
    path = tmp_path / "config.yaml"
    path.write_text("signing:\n  team_id: ABCDE12345\n")

    with pytest.raises(ConfigError, match="signing section missing keys"):
        load_config(path)


@pytest.mark.parametrize(
    ("body", "stray"),
    [
        # The whole section dedented: `signing:` parses as null.
        ("signing:\ncert: c.p12\ncert_password_env: X\nprofile: p.mobileprovision\n"
         "team_id: ABCDE12345\n", "cert, cert_password_env, profile, team_id"),
        # No heading left at all.
        ("cert: c.p12\nteam_id: ABCDE12345\n", "cert, team_id"),
        ("signing: {}\nprofile: p.mobileprovision\n", "profile"),
        # Half indented: the section is short of exactly the keys outside it.
        ("signing:\n  cert: c.p12\n  cert_password_env: X\nprofile: p.mobileprovision\n"
         "team_id: ABCDE12345\n", "profile, team_id"),
    ],
)
def test_signing_keys_at_the_top_level_are_an_indentation_error(
    tmp_path: Path, body: str, stray: str
) -> None:
    """A paid config whose keys lost their indent must not quietly load as a
    free one: that flips every app without `delivery:` to LiveContainer and
    tells the user they have no signing identity."""
    path = tmp_path / "config.yaml"
    path.write_text(body + "apps:\n  example:\n    bundle_id: com.example.app\n")

    for read in (load_config, account_of):
        with pytest.raises(ConfigError) as exc:
            read(path)
        assert exc.value.message == (
            f"{stray} {'is' if ',' not in stray else 'are'} at the top level of "
            "config.yaml, but belong under signing:"
        )
        assert "indent" in exc.value.hint and "signing:" in exc.value.hint


def test_set_settings_does_not_write_over_misplaced_signing_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("signing:\ncert: c.p12\ncert_password_env: X\nprofile: p\nteam_id: T\n")
    before = path.read_text()

    with pytest.raises(ConfigError, match="belong under signing:"):
        set_settings(path, signing_team_id="ABCDE12345")

    assert path.read_text() == before


def test_a_complete_section_with_a_stray_copy_beside_it_still_loads(tmp_path: Path) -> None:
    """Only keys the section is missing are an indentation mistake; a stray
    duplicate of one it has is noise, and refusing it would break a config
    that has always loaded."""
    path = _paid(tmp_path)
    path.write_text(path.read_text() + "team_id: FGHIJ67890\n")

    assert load_config(path).team_id == "ABCDE12345"


@pytest.mark.parametrize("team", ["", "REPLACEME", "  "])
def test_an_unset_team_id_reads_as_none(tmp_path: Path, team: str) -> None:
    path = _paid(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["signing"]["team_id"] = team
    path.write_text(yaml.safe_dump(raw))

    assert load_config(path).team_id is None


def test_in_free_mode_an_app_without_delivery_is_livecontainer(tmp_path: Path) -> None:
    cfg = load_config(_free(tmp_path, {"example": {"bundle_id": "com.example.app"}}))

    assert cfg.default_delivery == "livecontainer"
    assert cfg.apps["example"].delivery == "livecontainer"


def test_in_paid_mode_an_app_without_delivery_is_still_sideload(tmp_path: Path) -> None:
    cfg = load_config(_paid(tmp_path, {"example": {"bundle_id": "com.example.app"}}))

    assert cfg.default_delivery == "sideload"
    assert cfg.apps["example"].delivery == "sideload"


def test_an_explicit_delivery_wins_in_either_mode(tmp_path: Path) -> None:
    """A free config may hold an explicit sideload app (carried over from a
    paid one). It loads — refusing the whole config would take `--list` and
    every other verb down with it — and building it is what refuses."""
    apps = {
        "example": {"bundle_id": "com.example.app", "delivery": "sideload"},
        "other": {"bundle_id": "com.example.other", "delivery": "livecontainer"},
    }
    cfg = load_config(_free(tmp_path, apps))

    assert cfg.apps["example"].delivery == "sideload"
    assert cfg.apps["other"].delivery == "livecontainer"


def test_require_paid_account_names_the_path_and_the_way_out(tmp_path: Path) -> None:
    cfg = load_config(_free(tmp_path))

    with pytest.raises(PaidAccountRequiredError) as exc:
        cfg.require_paid_account("--lc-install")

    assert exc.value.message.startswith("--lc-install needs a paid Apple Developer Program")
    assert "free Apple ID" in exc.value.message
    # Not the default config, so the command names it: `--init` edits the
    # config it is pointed at.
    assert f"run `graft --config {cfg.path} --init` and answer yes" in exc.value.hint
    # Named once, in the command: the path twice ran the refusal past the
    # app's banner limit (K11).
    assert exc.value.hint.count(str(cfg.path)) == 1
    assert isinstance(exc.value, ConfigError)


def test_the_way_out_for_the_default_config_is_plain_init(tmp_path: Path) -> None:
    from graft_ipa.config import default_config_path

    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("apps: {}\n")

    with pytest.raises(PaidAccountRequiredError) as exc:
        load_config(path).require_paid_account("--lc-install")

    assert "run `graft --init` and answer yes" in exc.value.hint
    assert "keeps your apps" in exc.value.hint
    assert "delivery: livecontainer" in exc.value.hint


def test_require_paid_account_returns_the_section_when_paid(tmp_path: Path) -> None:
    cfg = load_config(_paid(tmp_path))

    assert cfg.require_paid_account("--lc-install") is cfg.signing


# --- the free starter ---------------------------------------------------------


def test_the_free_starter_has_no_signing_section_and_loads_free(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_default_config(path, free_account=True, default_device_udid="UDID-PLACEHOLDER")

    raw = yaml.safe_load(path.read_text())
    cfg = load_config(path)

    assert "signing" not in raw
    assert cfg.free_account is True
    assert cfg.default_device_udid == "UDID-PLACEHOLDER"
    assert cfg.apps == {}


def test_the_free_starter_says_what_it_is_and_how_to_go_paid(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_default_config(path, free_account=True)
    text = path.read_text()

    assert "Free Apple ID" in text
    assert "--init" in text and "answer yes" in text
    assert "delivery: livecontainer" in text
    assert "delivery: sideload" not in text


def test_an_app_added_to_the_free_starter_without_delivery_is_livecontainer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    write_default_config(path, free_account=True)
    add_app(path, "example", {"bundle_id": "com.example.app", "tweaks": []})

    assert load_config(path).apps["example"].delivery == "livecontainer"


# --- --set-settings ------------------------------------------------------------

SIGNING_SETTINGS = sorted(k for k, s in SETTINGS.items() if s.path[0] == "signing")


def test_the_signing_settings_are_the_three_flags() -> None:
    """If a fourth `signing.*` setting is registered, it is refused in free
    mode by the same rule — this keeps the list below honest."""
    assert SIGNING_SETTINGS == ["signing_cert", "signing_profile", "signing_team_id"]


@pytest.mark.parametrize("key", SIGNING_SETTINGS)
def test_set_settings_refuses_signing_fields_on_a_free_config(tmp_path: Path, key: str) -> None:
    path = _free(tmp_path)
    before = path.read_text()

    with pytest.raises(PaidAccountRequiredError) as exc:
        set_settings(path, **{key: "ABCDE12345"})

    assert exc.value.message.startswith("--" + key.replace("_", "-"))
    assert "paid Apple Developer Program" in exc.value.message
    # Nothing written: half a signing section would make the config unloadable.
    assert path.read_text() == before
    assert load_config(path).free_account is True


def test_set_settings_still_writes_the_other_settings_on_a_free_config(tmp_path: Path) -> None:
    path = _free(tmp_path)

    assert set_settings(path, archive_keep=3, default_device_udid="UDID-PLACEHOLDER") == [
        "archive_keep", "default_device_udid",
    ]
    assert load_config(path).archive_keep == 3


def test_set_settings_writes_signing_fields_on_a_paid_config(tmp_path: Path) -> None:
    path = _paid(tmp_path)

    assert set_settings(path, signing_team_id="FGHIJ67890") == ["signing_team_id"]
    assert load_config(path).team_id == "FGHIJ67890"
