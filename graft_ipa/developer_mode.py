"""Developer Mode on the device a development-signed build installs on (spec §5).

iOS 16 and later run a development-signed app only with Developer Mode on;
off, the app installs and then won't open. So only a sideload app signed
with a development profile asks (`needed`): an ad hoc or enterprise
profile runs without it, and LiveContainer delivery signs nothing.

One answer for every caller: `needed` says whether an app's install asks,
`status` reads the phone for `--update`'s readiness (quick from the last
reading, or live), `require_on` refuses the build. `None` is "don't know"
and never stops anything.

No pymobiledevice3 at import: the quick answer is a file read, and
`--update --plan --quick` must stay cheap. The live read imports it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import devices_seen, mobileprovision, signer
from .config import AppConfig, Config
from .errors import DeveloperModeOffError, PreflightError, SigningError


def turn_on(name: str | None = None, *, revealed: bool = False) -> str:
    """What the person does about it: the spec's sentence.

    `name` is what the device is ("iPad Air", or "iPad"; never the name it
    gives itself: `DeveloperModeCheck.model_name`), when the read could
    say. `revealed`: Graft has just had the phone show the switch, which iOS
    hides until a development app is installed or Xcode connects
    (`device_info.check_developer_mode`).
    """
    where = (
        f"Turn on Developer Mode on your {name or 'iPhone or iPad'} in "
        "Settings › Privacy & Security › Developer Mode"
    )
    if revealed:
        return (
            f"{where}; Graft has made the option visible. The device restarts; "
            "confirm after it does."
        )
    return f"{where}, then restart it."


#: The hint with nothing known of the device, and no reveal: word for word the
#: app's row (`DeviceDeveloperModeRow.turnOn`), and how `--update`'s readiness
#: reason begins (`update_readiness.DEVELOPER_MODE_OFF`).
TURN_ON = turn_on()


def needed(cfg: Config, app: AppConfig) -> bool | None:
    """Whether installing a build of `app` needs Developer Mode on the phone.

    True for a sideload app signed with a development profile, one whose
    entitlements say `get-task-allow: true` (`mobileprovision.is_development`).
    zsign signs with the profile alone (`-m`, no `-e`), so the app gets what
    the profile says. An auto-provisioned profile is one, on disk yet or not:
    Graft makes only `IOS_APP_DEVELOPMENT` profiles.

    False for an ad hoc or enterprise profile (`get-task-allow: false`), for
    LiveContainer delivery, which signs nothing, and for a free Apple ID,
    which can't sideload (`Config.require_delivery`) and has no profile.

    None when the profile can't be named or read: "don't know", which never
    blocks a build. Pre-flight fails a build over either before it gets here.

    Reads one file and nothing else: the profile `signer.plan_profile_for_app`
    names. Never `resolve_profile_for_app`, which can call App Store Connect
    and read the .p12's password from the keychain.
    """
    if app.delivery != "sideload" or cfg.free_account:
        return False
    try:
        plan = signer.plan_profile_for_app(cfg, app)
    except SigningError:
        return None
    if plan.source == signer.PROFILE_SOURCE_AUTO:
        return True
    try:
        plist = mobileprovision.decode(plan.path)
    except PreflightError:
        return None
    return mobileprovision.is_development(plist)


def status(state_dir: Path, udid: str, *, live: bool) -> bool | None:
    """Developer Mode on `udid`: True, False, or None for "don't know".

    `live=False`: the last reading `devices-seen.json` has (from
    `--device-info`, or an earlier live read), opening no session. `live=True`:
    read the phone now (`device_info.read_developer_mode`) and remember a
    true or false answer; None leaves the last one there.
    """
    if not live:
        value = devices_seen.seen(state_dir, udid).get("developer_mode")
        return value if isinstance(value, bool) else None
    from .device_info import read_developer_mode

    value = read_developer_mode(udid)
    devices_seen.remember(state_dir, udid, developer_mode=value)
    return value


def require_on(
    state_dir: Path, udid: str, *, reveal: bool = False,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Raise `DeveloperModeOffError` when `udid` says Developer Mode is off,
    read now, and remember what it said. On, or unknown: nothing.

    `reveal` is a real build's (`cli._build_one`, never a dry run's): with
    Developer Mode off, the phone is first asked, in the same session, to show
    the switch in Settings (`device_info.check_developer_mode`), and the hint
    then says it's there. A reveal that fails is `warn`ed, and the refusal is
    the same, with the plain hint. Nothing else reveals: not `status`, so not
    readiness or a plan, and not `--device-info`.
    """
    from .device_info import check_developer_mode

    check = check_developer_mode(udid, reveal=reveal)
    devices_seen.remember(state_dir, udid, developer_mode=check.on)
    if check.on is not False:
        return
    # What this session called the device is remembered first, for a device
    # Graft has no name for yet (one it has keeps it: a session may give only
    # the family, "iPad"). Then it's named as Graft last saw it, else by its
    # UDID. The hint says only the model or the family ("your iPad"), never
    # a name the device gives itself, so not the remembered one either; with
    # neither, "your iPhone or iPad".
    if check.name and devices_seen.name_of(state_dir, udid) is None:
        devices_seen.remember(state_dir, udid, name=check.name)
    shown = devices_seen.label(state_dir, udid)
    if check.reveal_error is not None and warn is not None:
        warn(f"couldn't show the Developer Mode switch on {shown}: {check.reveal_error}")
    raise DeveloperModeOffError(
        f"Developer Mode is off on {shown}",
        hint=turn_on(check.model_name, revealed=check.revealed),
    )
