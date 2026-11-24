"""F7 — the auto-provisioned profile cache must not become a dead end.

Making pre-flight app-aware meant it started validating the *auto-provisioning
cache* under `app_store_connect.profile_cache_dir`, enforcing expiry and device
membership on it. Nothing refreshed that cache:
`apple_portal.ensure_profile_for_bundle` returned the cached file whenever it
existed, on the strength of a comment saying "signer.py validates expiry
upstream" — which signer.py has never done.

The two halves met badly. A failed pre-flight check aborts the build before
stage 2, and stage 4 is the only code that can talk to Apple. So once that cache
expired — or once a newly-connected iPhone wasn't in it — every build of that
app hard-failed pre-flight, and the only remedy was deleting a file under
~/.config by hand.

THE FIX, and why this one: the cache heals itself. `ensure_profile_for_bundle`
treats an expired file, a file that omits a needed device, and a file it cannot
decode as a miss and re-provisions. Pre-flight stops failing for the conditions
stage 4 is about to repair.

Both halves are required and neither is sufficient. Self-healing alone never
runs, because pre-flight still aborts first. Downgrading pre-flight alone would
turn a clear stop into a confusing failure inside zsign, further from the cause.

A `--refresh-profile` flag was the alternative, and it is worse: it is a remedy
the user has to know exists, for a condition they have no way to diagnose ("the
build stopped working" is all they see). The rule the tool can check, it should
check. What a flag would add over this is the ability to force a refresh when
the *cert* changes, which neither the expiry nor the device list can detect —
worth having, but it is a new capability rather than an escape from a dead end,
and shipping it as the fix would leave the dead end in place for anyone who
never finds the flag.

Never makes a real Apple Developer Portal call: the client is a MagicMock and
the decoder is patched, so nothing here shells out to `security` either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import apple_portal, mobileprovision, preflight, tools
from graft_ipa.apple_portal import (
    BundleId,
    Certificate,
    Device,
    Profile,
    ensure_profile_for_bundle,
)
from graft_ipa.config import (
    AppConfig,
    AppStoreConnectConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import PreflightError

UDID = "00008130-001A2B3C0123456D"


# ---------------------------------------------------------------------------
# mobileprovision.cache_problem — the validity rule itself
# ---------------------------------------------------------------------------


def _plist(*, expires: datetime | None, devices: list[str] | None) -> dict:
    out: dict = {}
    if expires is not None:
        out["ExpirationDate"] = expires
    if devices is not None:
        out["ProvisionedDevices"] = devices
    return out


def _problem(tmp_path: Path, plist: dict, *, device_udids=(UDID,)) -> str | None:
    profile = tmp_path / "cached.mobileprovision"
    profile.write_bytes(b"x")
    with patch.object(mobileprovision, "decode", return_value=plist):
        return mobileprovision.cache_problem(profile, device_udids=device_udids)


def test_a_healthy_cached_profile_is_reusable(tmp_path: Path) -> None:
    plist = _plist(expires=datetime.now(UTC) + timedelta(days=200), devices=[UDID])
    assert _problem(tmp_path, plist) is None


def test_an_expired_cached_profile_is_not(tmp_path: Path) -> None:
    plist = _plist(expires=datetime.now(UTC) - timedelta(days=1), devices=[UDID])
    problem = _problem(tmp_path, plist)
    assert problem is not None and "expired" in problem


def test_a_naive_expiry_is_read_as_utc(tmp_path: Path) -> None:
    """plistlib hands back naive datetimes; comparing one to an aware `now`
    raises TypeError, which would read as "unreadable" and re-provision on
    every single build."""
    naive = (datetime.now(UTC) + timedelta(days=200)).replace(tzinfo=None)
    assert _problem(tmp_path, _plist(expires=naive, devices=[UDID])) is None


def test_a_profile_missing_the_device_is_not_reusable(tmp_path: Path) -> None:
    plist = _plist(expires=datetime.now(UTC) + timedelta(days=200), devices=["00008130-OTHER"])
    problem = _problem(tmp_path, plist)
    assert problem is not None and UDID in problem


def test_device_matching_ignores_case(tmp_path: Path) -> None:
    """ASC normalizes UDIDs to lower case in places; config.yaml does not.

    A case-sensitive comparison would re-provision on every build — a round
    trip to Apple per build, and a new profile each time.
    """
    plist = _plist(expires=datetime.now(UTC) + timedelta(days=200), devices=[UDID.lower()])
    assert _problem(tmp_path, plist) is None


def test_a_distribution_profile_covers_every_device(tmp_path: Path) -> None:
    """No ProvisionedDevices key means "not a development profile", not
    "provisioned for nobody". Reading absence as absence of the device would
    re-provision forever."""
    plist = _plist(expires=datetime.now(UTC) + timedelta(days=200), devices=None)
    assert _problem(tmp_path, plist) is None


def test_a_profile_that_will_not_decode_is_not_reusable(tmp_path: Path) -> None:
    """The safe direction. Re-provisioning costs a round trip; trusting a file
    we cannot read costs a signed IPA the phone refuses to install."""
    profile = tmp_path / "cached.mobileprovision"
    profile.write_bytes(b"not a CMS blob")
    with patch.object(mobileprovision, "decode", side_effect=PreflightError("garbage")):
        problem = mobileprovision.cache_problem(profile, device_udids=[UDID])
    assert problem is not None and "could not be read" in problem


# ---------------------------------------------------------------------------
# ensure_profile_for_bundle — the cache that heals itself
# ---------------------------------------------------------------------------


def _portal_client() -> MagicMock:
    client = MagicMock()
    client.list_bundle_ids.return_value = [
        BundleId(id="bid1", identifier="com.bravo.*", name="x", platform="IOS")
    ]
    client.list_certificates.return_value = [
        Certificate(
            id="cert1",
            certificate_type="DEVELOPMENT",
            display_name="Apple Development: tester",
            serial_number="DEADBEEF",
            expiration=None,
        )
    ]
    client.list_devices.return_value = [
        Device(id="dev1", udid=UDID, name="iPhone", platform="IOS", status="ENABLED")
    ]
    client.list_profiles.return_value = [
        Profile(
            id="old1",
            name="graft com.bravo.* dev",
            profile_type="IOS_APP_DEVELOPMENT",
            uuid="uuid",
            expiration=None,
            profile_content_b64=None,
        )
    ]
    import base64

    client.create_profile.return_value = Profile(
        id="new1",
        name="graft com.bravo.* dev 2",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="uuid2",
        expiration=None,
        profile_content_b64=base64.b64encode(b"FRESH_PROFILE").decode(),
    )
    return client


def _stale_only(reason: str):
    """`cache_problem` answering "stale" about the old bytes and "fine" about
    the new ones.

    Keyed on content rather than pinned to one answer because the rule is now
    asked twice — once about the file on disk and once about what App Store
    Connect hands back — and a patch that says "stale" to both would be
    describing a refresh that cannot succeed, which is a different test.
    """
    return patch.object(
        apple_portal.mobileprovision,
        "cache_problem",
        side_effect=lambda path, **_: reason if path.read_bytes() == b"STALE" else None,
    )


def test_an_expired_cache_is_replaced_rather_than_returned(tmp_path: Path) -> None:
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"STALE")
    client = _portal_client()
    _remote(client, b"STALE")

    with _stale_only("the cached profile expired"):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    assert result.refreshed is True
    assert result.profile_path.read_bytes() == b"FRESH_PROFILE"
    assert result.refresh_reason == "the cached profile expired"


def test_a_stale_cache_deletes_the_remote_profile_it_came_from(tmp_path: Path) -> None:
    """Re-downloading is not refreshing.

    The remote profile of the same name is usually the one this stale file was
    downloaded from, so reusing it would fetch the same expired bytes back and
    the next build would find the cache stale all over again. It is read and
    judged first (review #16: never deleted unread), refused for the same
    reason, and only then deleted and recreated.
    """
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"STALE")
    client = _portal_client()
    _remote(client, b"STALE")

    with _stale_only("expired"):
        ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    client.get_profile.assert_called_once_with("old1", with_content=True)
    client.create_profile.assert_called_once()
    client.delete_profile.assert_called_once_with("old1")


def test_a_stale_cache_keeps_a_remote_profile_that_is_fine(tmp_path: Path) -> None:
    """Review #16: the local file being stale says nothing certain about the
    remote one. Another Mac may already have refreshed it; deleting it unread
    replaced a profile that Mac was using."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"STALE")
    client = _portal_client()
    _remote(client, b"REFRESHED_ELSEWHERE")

    with _stale_only("expired"):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    assert result.profile_path.read_bytes() == b"REFRESHED_ELSEWHERE"
    assert result.refresh_reason == "expired"
    client.create_profile.assert_not_called()
    client.delete_profile.assert_not_called()


def test_a_usable_cache_still_makes_no_network_call(tmp_path: Path) -> None:
    """The self-heal must not become a refresh-every-build."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"GOOD")
    client = _portal_client()

    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    assert result.refreshed is False
    client.list_bundle_ids.assert_not_called()
    client.delete_profile.assert_not_called()


def test_the_cache_is_judged_against_the_devices_the_build_needs(tmp_path: Path) -> None:
    """`device_udids` is passed through, not re-derived.

    The whole point of the device half is that the profile is checked against
    the phones this build will register — checking it against nothing would
    make "connect a new iPhone" an unfixable failure again.
    """
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"GOOD")

    with patch.object(
        apple_portal.mobileprovision, "cache_problem", return_value=None
    ) as problem:
        ensure_profile_for_bundle(
            _portal_client(),
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID, "00008130-SECOND"],
            cache_dir=cache,
        )

    assert problem.call_args.kwargs["device_udids"] == [UDID, "00008130-SECOND"]


# ---------------------------------------------------------------------------
# …and the two answers that were never asked the question
# ---------------------------------------------------------------------------


def _remote(client: MagicMock, content: bytes) -> None:
    """What `get_profile` hands back for the pre-existing remote profile."""
    import base64

    client.get_profile.return_value = Profile(
        id="old1",
        name="graft com.bravo.* dev",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="uuid",
        expiration=None,
        profile_content_b64=base64.b64encode(content).decode(),
    )


def _judged(bad: bytes, reason: str):
    return patch.object(
        apple_portal.mobileprovision,
        "cache_problem",
        side_effect=lambda path, **_: reason if path.read_bytes() == bad else None,
    )


def test_the_remote_profile_reused_on_a_cache_miss_is_judged_too(tmp_path: Path) -> None:
    """The cache-miss path had no validity check at all.

    With no file on disk — a fresh machine, or the moment after the user
    deletes it, which is the remedy the old dead end trained them into — this
    is the *only* path. It reused the pre-existing remote profile unchecked, so
    "when is a cached profile good?" still had a second, unanswered answer:
    every pre-flight row green, stage 4 downloading a profile created before
    the phone existed, and the phone refusing the IPA. Only the next build,
    which judges the file this one wrote, healed it.
    """
    cache = tmp_path / "profiles"  # deliberately absent
    client = _portal_client()
    _remote(client, b"REMOTE_MISSING_THE_PHONE")

    with _judged(b"REMOTE_MISSING_THE_PHONE", f"does not list {UDID}"):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    assert result.profile_path.read_bytes() == b"FRESH_PROFILE"
    assert result.refresh_reason == f"does not list {UDID}"
    client.delete_profile.assert_called_once_with("old1")


def test_a_good_remote_profile_is_still_reused_without_recreating_it(
    tmp_path: Path,
) -> None:
    """The direction that must not move: judging it must not mean replacing it.

    A check that always fails is a refresh on every build wearing a different
    hat, which is the cost this whole rule exists to avoid.
    """
    cache = tmp_path / "profiles"
    client = _portal_client()
    _remote(client, b"REMOTE_IS_FINE")

    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    assert result.profile_path.read_bytes() == b"REMOTE_IS_FINE"
    client.create_profile.assert_not_called()
    client.delete_profile.assert_not_called()


STRANGER = "00008130-NOTREGISTERED"


def _decoder(plists: dict[bytes, dict]):
    """`mobileprovision.decode` answering from the file's own bytes.

    Only `security cms -D` is displaced: `cache_problem` itself runs for real,
    which is the point — the question these tests are about is *which devices
    it is asked about*, and patching it out would answer that question in the
    test instead of in the product.
    """
    return patch.object(
        mobileprovision, "decode", side_effect=lambda path: plists[path.read_bytes()]
    )


def test_a_device_ASC_cannot_provide_is_not_a_reason_to_refresh(tmp_path: Path) -> None:
    """The cause of the every-build re-provisioning, removed at the cause.

    An unregistered UDID can never appear in a profile ASC issues, so judging
    the cache against it asks a question no refresh can answer: the new file
    came back still missing it, the next build asked again, and the account's
    profile was deleted and recreated on every single build — with a network
    failure between the two calls leaving it with no profile at all.

    Once the impossible device is out of the question the cached profile is
    fine, and it is returned untouched. The user is told (see
    `test_the_skipped_device_is_said_out_loud`), not blocked: this is the
    ordinary state of a config.yaml after a phone is replaced.
    """
    cache = tmp_path / "profiles"
    cache.mkdir()
    cached = cache / "com_bravo_x.mobileprovision"
    cached.write_bytes(b"CACHED")
    client = _portal_client()  # ASC has UDID and nothing else

    with _decoder({b"CACHED": {"ProvisionedDevices": [UDID]}}):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID, STRANGER],
            cache_dir=cache,
        )

    assert result.refreshed is False
    assert result.profile_path.read_bytes() == b"CACHED"
    assert result.unregistered_devices == (STRANGER,)
    client.delete_profile.assert_not_called()
    client.create_profile.assert_not_called()


def test_a_registered_device_the_profile_lacks_still_refreshes(tmp_path: Path) -> None:
    """The half that must survive: narrowing the question must not silence it.

    A phone ASC *does* have, missing from the cached profile, is exactly what
    the self-heal exists for — connect a new iPhone and stage 4 adds it. If the
    narrowing swallowed this too, the dead end would be back with the refresh
    simply never happening.
    """
    cache = tmp_path / "profiles"
    cache.mkdir()
    cached = cache / "com_bravo_x.mobileprovision"
    cached.write_bytes(b"CACHED")
    client = _portal_client()
    _remote(client, b"CACHED")  # the remote copy lacks the phone too

    with _decoder(
        {
            b"CACHED": {"ProvisionedDevices": ["00008130-SOMEOTHERPHONE"]},
            b"FRESH_PROFILE": {"ProvisionedDevices": [UDID]},
        }
    ):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID, STRANGER],
            cache_dir=cache,
        )

    assert result.refreshed is True
    assert result.profile_path.read_bytes() == b"FRESH_PROFILE"
    assert result.unregistered_devices == (STRANGER,)
    # The reason names the device the refresh was actually for, not the one it
    # was never going to add.
    assert UDID in (result.refresh_reason or "")
    assert STRANGER not in (result.refresh_reason or "")
    client.create_profile.assert_called_once()
    assert client.create_profile.call_args.kwargs["device_ids"] == ["dev1"]


def test_the_skipped_device_is_said_out_loud(tmp_path: Path) -> None:
    """Carrying on without a configured device is only safe if it is announced.

    The build proceeds and the profile does not cover that phone, so an install
    on it will be refused — which is a mystery unless this sentence exists. It
    goes out through `_warn`, so both channels get it.
    """
    from graft_ipa.signer import ResolvedProfile, _provisioning_notes

    notes = _provisioning_notes(
        ResolvedProfile(path=tmp_path / "p", unregistered_devices=(STRANGER,))
    )

    assert len(notes) == 1
    assert STRANGER in notes[0]
    assert "developer.apple.com" in notes[0]

    # …and independently of a refresh, which is the reason these are a tuple.
    both = _provisioning_notes(
        ResolvedProfile(
            path=tmp_path / "p",
            refresh_reason="the cached profile expired",
            unregistered_devices=(STRANGER,),
        )
    )
    assert len(both) == 2
    assert "re-provisioned" in both[0]


def test_a_refresh_that_resolves_nothing_stops_rather_than_repeating(
    tmp_path: Path,
) -> None:
    """`force_refresh = True` on any problem, with nothing asking about the result.

    A condition the refresh cannot fix meant `delete_profile` +
    `create_profile` on every single build — for as long as the condition
    lasted, which for an unregistered device is forever — with a network
    failure between the two calls leaving the account with no profile at all.
    Step 3 catches the device case before anything is touched; this is the
    backstop for whatever else ASC can hand back.
    """
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(b"STALE")
    client = _portal_client()
    _remote(client, b"STALE")

    with patch.object(
        apple_portal.mobileprovision, "cache_problem", return_value="expired on arrival"
    ), pytest.raises(apple_portal.PortalError, match="still unusable"):
        ensure_profile_for_bundle(
            client,
            bundle_id="com.bravo.app",
            cert_serial="DEADBEEF",
            device_udids=[UDID],
            cache_dir=cache,
        )

    # Created before the old one is deleted, so a stop here leaves the account
    # with a profile rather than with none. The refused new one does not stay:
    # left there, each build would add another (review #1).
    client.delete_profile.assert_called_once_with("new1")
    assert (cache / "com_bravo_x.mobileprovision").read_bytes() == b"STALE", (
        "a profile that failed its own check became the cache"
    )


# ---------------------------------------------------------------------------
# The build says so
# ---------------------------------------------------------------------------


def test_a_refresh_is_reported_through_the_one_warning_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Self-healing must not mean silent.

    A round trip to Apple that nothing mentions is one the user has to
    reverse-engineer from a network log later. It goes out as a warning, which
    means both channels — `log` for the build panel, stderr for everyone else.
    """
    import json

    from graft_ipa import cli as cli_mod
    from graft_ipa.json_output import JsonEmitter
    from graft_ipa.signer import SignResult

    emitter = JsonEmitter()
    sg = SignResult(
        output_ipa=tmp_path / "out.ipa",
        team_id="TEAM",
        cert_subject=None,
        expiry=None,
        stdout="",
        stderr="",
        profile_notes=(
            "re-provisioned the signing profile via App Store Connect: expired",
        ),
    )
    for note in sg.profile_notes:
        cli_mod._warn(emitter, note)

    captured = capsys.readouterr()
    event = json.loads(captured.out.splitlines()[0])
    assert event["source"] == "warning"
    assert "re-provisioned" in event["line"]
    assert "re-provisioned" in captured.err


def test_a_signing_run_with_no_refresh_carries_no_note() -> None:
    """`profile_notes` is iterated by the build, so a placeholder here would
    warn on every sideload build in the product."""
    from graft_ipa.signer import SignResult

    sg = SignResult(
        output_ipa=Path("out.ipa"),
        team_id=None,
        cert_subject=None,
        expiry=None,
        stdout="",
        stderr="",
    )
    assert sg.profile_notes == ()


# ---------------------------------------------------------------------------
# …and pre-flight reads the same rule, on all three of its conditions
# ---------------------------------------------------------------------------
#
# The dead end above survived in the one condition the generalization skipped.
# `cache_problem` has three — undecodable, expired, missing a device — and
# pre-flight re-derived two of them by hand while `check_profile_contents`
# hard-failed on the third: a `CheckResult(name="profile parse", ok=False)` with
# no `created_on_demand` exemption anywhere near it. So an auto-provisioned
# cache file that will not decode (truncated by a full volume, half-written by
# an interrupted download) aborted pre-flight before stage 2, while
# `cache_problem` classified the very same file as a refreshable miss that stage
# 4 would heal. Same dead end, same remedy: `rm` under ~/.config.
#
# The fix is at the layer, not at the branch: pre-flight asks `cache_problem`
# and renders whatever sentence comes back as a *passing* note.


def _auto_cfg(tmp_path: Path) -> Config:
    """A config whose bravo app signs with an auto-provisioned profile."""
    cfg = Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "wildcard.mobileprovision",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )
    cfg.app_store_connect = AppStoreConnectConfig(
        key_id="KEYID12345",
        issuer_id="1234-issuer",
        key_file=tmp_path / "AuthKey.p8",
        device_udids=[UDID],
        profile_cache_dir=tmp_path / "profiles",
    )
    return cfg


def _auto_app(**kwargs) -> AppConfig:
    return AppConfig(
        name="bravo",
        bundle_id="com.bravo.app",
        preferred_version="latest",
        tweaks=[],
        cyan=CyanOptions(),
        **kwargs,
    )


def _write_cache(tmp_path: Path) -> Path:
    cached = tmp_path / "profiles" / "com_bravo_x.mobileprovision"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x30\x82 truncated")
    return cached


def _undecodable():
    """`security cms -D` refusing the file, without running `security`."""
    return patch.object(
        mobileprovision,
        "decode",
        side_effect=PreflightError("failed to decode: unable to read CMS"),
    )


def test_a_cache_file_that_will_not_decode_does_not_fail_preflight(tmp_path: Path) -> None:
    """The condition pre-flight never implemented.

    `cache_problem` calls an unreadable cache a reason to re-provision. This
    called it a reason to stop, three stages before the code that re-provisions.
    """
    cfg = _auto_cfg(tmp_path)
    _write_cache(tmp_path)

    with _undecodable():
        results = preflight.check_profile_contents(cfg, app=_auto_app(auto_profile=True))

    assert results, "no row at all — the check silently reported nothing"
    row = next(r for r in results if r.name == preflight.PROFILE_PARSE_CHECK_NAME)
    assert row.ok is True, row.hint
    assert "could not be read" in row.detail
    assert "re-provisions it at sign time" in row.detail


def _preflight_with_an_unreadable_cache(cfg: Config, app: AppConfig) -> list:
    """`run_all_checks` as `_build_one` calls it, with everything else satisfied.

    Nothing here touches anything real: `idevice_id` is never run, the keychain
    is answered in-process, and `security cms -D` is never reached because the
    decoder itself is the thing under test.
    """
    from graft_ipa import keychain

    with patch.object(tools, "which", return_value="/usr/bin/true"), \
         patch.object(keychain, "read_p12_password", return_value="hunter2"), \
         patch.object(preflight, "list_connected_devices", return_value=[UDID]), \
         _undecodable():
        return preflight.run_all_checks(
            cfg, delivery="sideload", will_install=True, app=app
        )


def test_the_unreadable_cache_no_longer_aborts_the_build(tmp_path: Path) -> None:
    """The user-visible half: a failed check raises before stage 2.

    Which is the whole defect — the build never reaches the one place that can
    replace the file, and no flag, retry or reinstall gets it there.

    Asserted against `run_all_checks`, which is what `_build_one` runs, because
    the version of this that called `check_profile_contents` alone was a strict
    subset of it and passed while the build it claims to describe still died:
    `check_devices` had the identical decode-failure branch and never got the
    downgrade, so the same file produced `profile parse: OK` next to `profile
    devices: FAIL`. A test named after the build has to run what the build
    runs — this one asserted the fix over exactly the half that had it.
    """
    cfg = _auto_cfg(tmp_path)
    cfg.signing.cert.write_bytes(b"p12")
    _write_cache(tmp_path)
    app = _auto_app(auto_profile=True)

    results = _preflight_with_an_unreadable_cache(cfg, app)

    assert [r.name for r in results if not r.ok] == [], (
        "pre-flight still aborts the build over a cache stage 4 replaces"
    )
    preflight.assert_all_passed(results)  # raised PreflightError before this fix


def test_both_rows_about_the_unreadable_cache_say_the_same_thing(
    tmp_path: Path,
) -> None:
    """Two checks read this one file, and they used to disagree in print.

    `profile parse: OK — the cached profile could not be read …` immediately
    above `profile devices: FAIL` is not just an unnecessary stop; it is two
    contradictory claims about one path, in a table whose whole job is to say
    what is wrong.
    """
    cfg = _auto_cfg(tmp_path)
    cfg.signing.cert.write_bytes(b"p12")
    _write_cache(tmp_path)

    results = _preflight_with_an_unreadable_cache(cfg, _auto_app(auto_profile=True))
    rows = {
        r.name: r
        for r in results
        if r.name in (preflight.PROFILE_PARSE_CHECK_NAME, preflight.PROFILE_DEVICES_CHECK_NAME)
    }

    assert set(rows) == {
        preflight.PROFILE_PARSE_CHECK_NAME,
        preflight.PROFILE_DEVICES_CHECK_NAME,
    }, f"one of the two rows about the cache is missing: {sorted(rows)}"
    for name, row in rows.items():
        assert row.ok is True, f"{name}: {row.hint}"
        assert "could not be read" in row.detail, name
        assert "re-provisions it at sign time" in row.detail, name


def test_a_profile_the_user_manages_still_fails_the_device_row_too(
    tmp_path: Path,
) -> None:
    """The direction that must not move, on the sibling branch as well.

    Nothing in the product replaces a profile the user pointed at, so a file
    that will not decode is theirs to act on — and the downgrade has to stay
    tied to `created_on_demand`, not to "the decoder raised".
    """
    cfg = _auto_cfg(tmp_path)
    cfg.signing.cert.write_bytes(b"p12")
    cfg.signing.profile.write_bytes(b"\x30\x82 truncated")

    results = _preflight_with_an_unreadable_cache(cfg, _auto_app())
    row = next(r for r in results if r.name == preflight.PROFILE_DEVICES_CHECK_NAME)

    assert row.ok is False
    assert "unable to read CMS" in row.hint


def test_a_profile_the_user_manages_still_fails_when_it_will_not_decode(
    tmp_path: Path,
) -> None:
    """The direction that must not move.

    Nothing in the product replaces a profile the user pointed at, so passing
    the row would be a claim with a build-shaped consequence: zsign fails
    instead, further from the file that explains it.
    """
    cfg = _auto_cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"\x30\x82 truncated")

    with _undecodable():
        results = preflight.check_profile_contents(cfg, app=_auto_app())

    row = next(r for r in results if r.name == preflight.PROFILE_PARSE_CHECK_NAME)
    assert row.ok is False
    assert "unable to read CMS" in row.hint


def test_preflight_and_stage_four_answer_from_the_same_function(tmp_path: Path) -> None:
    """The point of the fix, asserted as the thing it actually is.

    Two callers, one rule. If `check_profile_contents` ever re-derives the
    answer again — the shape this round found — patching `cache_problem` out
    from under it stops changing what pre-flight says, and this fails.
    """
    cfg = _auto_cfg(tmp_path)
    _write_cache(tmp_path)

    with patch.object(
        mobileprovision, "cache_problem", return_value="the cached profile is a teapot"
    ), _undecodable():
        results = preflight.check_profile_contents(cfg, app=_auto_app(auto_profile=True))

    row = next(r for r in results if r.name == preflight.PROFILE_PARSE_CHECK_NAME)
    assert row.ok is True
    assert "is a teapot" in row.detail, (
        "pre-flight reached its own verdict instead of `cache_problem`'s"
    )


# ---------------------------------------------------------------------------
# Review re-check #5: a phone that arrived after the last Save and test
# ---------------------------------------------------------------------------

NEW_PHONE = "00008140-000A1B2C3D4E5F60"


def test_the_default_phone_is_provisioned_though_device_udids_predates_it(
    tmp_path: Path,
) -> None:
    """`device_udids` is a snapshot from the last Save and test. A phone made
    the default after it was left out of every profile, because the default
    counted only when the list was empty."""
    from graft_ipa import signer

    cfg = _auto_cfg(tmp_path)
    cfg.default_device_udid = NEW_PHONE

    assert signer.auto_profile_device_udids(cfg) == [UDID, NEW_PHONE]


def test_a_default_phone_already_listed_is_not_listed_twice(tmp_path: Path) -> None:
    from graft_ipa import signer

    cfg = _auto_cfg(tmp_path)
    cfg.default_device_udid = UDID.lower()

    assert signer.auto_profile_device_udids(cfg) == [UDID]


def _device_row(cfg: Config, connected: str) -> preflight.CheckResult:
    cached = cfg.app_store_connect.profile_cache_dir / "com_bravo_x.mobileprovision"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"CACHED")
    plist = {"ProvisionedDevices": [UDID],
             "ExpirationDate": datetime.now(UTC) + timedelta(days=200)}
    with patch.object(mobileprovision, "decode", return_value=plist):
        rows = preflight.check_devices(cfg, app=_auto_app(auto_profile=True), udids=[connected])
    return next(r for r in rows if r.name == "device in provisioning profile")


def test_a_new_default_phone_is_renewed_at_sign_time_not_refused(tmp_path: Path) -> None:
    cfg = _auto_cfg(tmp_path)
    cfg.default_device_udid = NEW_PHONE

    row = _device_row(cfg, NEW_PHONE)

    assert row.ok is True, row.hint
    assert "re-provisions it at sign time" in row.detail


def test_a_phone_auto_provisioning_does_not_cover_names_save_and_test(tmp_path: Path) -> None:
    """The phone may already be in the portal (Xcode registers it), so "add
    it in the portal and regenerate the profile" sent the user to a step
    that was done. What refreshes the list is Save and test."""
    cfg = _auto_cfg(tmp_path)

    row = _device_row(cfg, NEW_PHONE)

    assert row.ok is False
    assert "Save and test" in row.hint
    assert "graft --set-portal" in row.hint
    assert "default device" in row.hint
