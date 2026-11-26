"""Whether each of an app's update targets is ready, worked out before anything downloads.

An app's targets are the devices it was delivered to (spec §11.3, option B;
`cli._delivery_targets`), and each is asked about on its own. Connected first,
asked of usbmux alone through the CLI's one rule (`installer.resolve_target`,
with that device named, §11.6): not connected, the target is **waiting**
("iPad mini isn't connected: it'll update when it is"), never blocked, and
the app's other targets still run. No fallback to another device, and no
"pick one".

Then, for LiveContainer delivery, a LiveContainer on that device; for a
development-signed install (`developer_mode.needed`), Developer Mode on
(Plan B's `developer_mode.status`; unknown never blocks); and for a sideload
app, that device in the profile that signs it. A "no" blocks that target
alone.

Two probes, for the plan's two forms (spec 3.1's table):

- `QuickProbe` (`--plan --quick`, Graft.app's daily check): usbmux's list and
  nothing else. A device's name, LiveContainer and Developer Mode are the
  last answers in `devices-seen.json`, so it opens no session to the device
  and uses no AFC or house_arrest. Readiness is "likely": what it remembers
  only shows, and never blocks; a remembered "Developer Mode off" is a note.
- `FullProbe` (`--plan`, and every run): a lockdown session per device, as
  `--devices` opens; installation_proxy for the LiveContainer, as the push
  asks; Developer Mode read now; the signing profile read. A "no" blocks.
  What it learns is remembered for the next quick plan.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from . import developer_mode, devices, devices_seen, installer
from .config import AppConfig, Config
from .errors import DeviceNotConnectedError, GraftError, NoDeviceConnectedError

CHECKED = "checked"
LIKELY = "likely"
LIVECONTAINER = "livecontainer"
SIDELOAD = "sideload"

#: A target whose device isn't on usbmux (§11.3, §11.6): waiting, not blocked.
WAITING = "{device} isn't connected: it'll update when it is"
#: What a quick plan shows, without blocking, of a remembered reading.
DEVELOPER_MODE_WAS_OFF = "Developer Mode was off on {device} when Graft last checked"
#: A sideload target the signing profile doesn't list (§11.3: registered).
NOT_IN_PROFILE = "{device} isn't in the provisioning profile that signs {app}"
#: A target blocked for Developer Mode off: the build's hint, then how to make
#: the switch appear, which iOS hides until a development app is installed.
#: Only a real build's refusal has the phone show it (`developer_mode.require_on`);
#: an update never does.
DEVELOPER_MODE_OFF = (
    f"{developer_mode.TURN_ON} If the switch isn't there, Build & Install the app once: "
    "Graft makes it appear."
)


@dataclass(frozen=True)
class Target:
    """Where one install goes: an `update-plan` target's device and delivery."""

    delivery: str
    udid: str | None = None
    device_name: str | None = None
    lc_bundle_id: str | None = None


@dataclass(frozen=True)
class Readiness:
    """One target, and whether it can install now: `waiting` (the sentence,
    when its device isn't connected), `blocked_reason` (any other "no"), and
    what a quick plan shows without blocking (`note`). None when there's
    nothing."""

    target: Target
    waiting: str | None = None
    blocked_reason: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Phone:
    udid: str
    name: str | None = None
    livecontainer_bundle_id: str | None = None


def connection(cfg: Config, app: AppConfig, udid: str) -> tuple[str | None, str | None]:
    """`udid`'s name, and its waiting sentence when it isn't connected (None
    when it is). Asked of usbmux alone, with that device named."""
    try:
        target = installer.resolve_target(cfg, app, explicit_udid=udid)
    except DeviceNotConnectedError as e:  # that device isn't on usbmux (spec §11.6)
        return devices_seen.name_of(cfg.state_dir, udid), WAITING.format(device=e.label)
    except NoDeviceConnectedError:  # nothing is: the device is still the one to connect
        return (devices_seen.name_of(cfg.state_dir, udid),
                WAITING.format(device=devices_seen.label(cfg.state_dir, udid)))
    return target.name, None


class QuickProbe:
    """`devices-seen.json`, and nothing asked of the device."""

    readiness = LIKELY
    blocks = False

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def describe(self, udid: str) -> Phone | str:
        seen = devices_seen.seen(self.state_dir, udid)
        return Phone(udid, seen.get("name"), seen.get("livecontainer_bundle_id"))

    def livecontainer(self, cfg: Config, phone: Phone, app: AppConfig) -> tuple[str | None, str | None]:
        return app.lc_bundle_id or phone.livecontainer_bundle_id, None

    def developer_mode(self, phone: Phone) -> bool | None:
        return developer_mode.status(self.state_dir, phone.udid, live=False)

    def registered(self, cfg: Config, phone: Phone, app: AppConfig) -> str | None:
        return None


class FullProbe:
    """A lockdown session per device, installation_proxy, Developer Mode now."""

    readiness = CHECKED
    blocks = True

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._listing: devices.DeviceListing | None = None
        self._installed: dict[str, list[str] | GraftError] = {}

    def describe(self, udid: str) -> Phone | str:
        """The device as lockdown describes it, or why it couldn't."""
        if self._listing is None:
            try:
                self._listing = devices.list_devices()
            except Exception:  # usbmuxd gone between the choice and here
                self._listing = devices.DeviceListing(devices=[], failures=[])
            for d in self._listing.devices:
                devices_seen.remember(self.state_dir, d.udid, **devices_seen.listing_facts(d))
        for d in self._listing.devices:
            if d.udid == udid:
                return Phone(udid, d.name, d.livecontainer_bundle_id)
        for f in self._listing.failures:
            if f.udid == udid:
                return f"{udid} is connected but couldn't be read: {f.error}"
        return f"{udid} is connected but couldn't be read: it didn't answer"

    def livecontainer(self, cfg: Config, phone: Phone, app: AppConfig) -> tuple[str | None, str | None]:
        """The LiveContainer the push would use, or why there's none: asked as
        `preflight.check_livecontainer` asks it."""
        from . import lc_discovery, lc_manage
        from .lc_runner import LiveContainerError

        label = phone.name or phone.udid
        if phone.udid not in self._installed:
            try:
                self._installed[phone.udid] = list(lc_manage.UsbmuxDevice().installed_bundle_ids(phone.udid))
            except GraftError as e:
                self._installed[phone.udid] = e
        installed = self._installed[phone.udid]
        if isinstance(installed, GraftError):
            return None, f"couldn't list the apps on {label}: {installed.message}"
        if not installed:
            return None, f"{label} listed no apps: unlock it and keep it on the home screen"
        try:
            chosen, _others = lc_discovery.choose(
                installed, pinned=app.lc_bundle_id, team=cfg.team_id, free_account=cfg.free_account,
            )
        except LiveContainerError as e:
            return None, e.message
        return chosen, None

    def developer_mode(self, phone: Phone) -> bool | None:
        return developer_mode.status(self.state_dir, phone.udid, live=True)

    def registered(self, cfg: Config, phone: Phone, app: AppConfig) -> str | None:
        """Why the profile that signs `app` won't install on this device, or
        None: pre-flight's own check (`preflight.check_devices`), for this
        device alone. Reads the profile file and nothing else."""
        from . import preflight

        rows = preflight.check_devices(cfg, check_profile=True, app=app, udids=[phone.udid],
                                       target=phone.udid)
        failed = [r for r in rows if not r.ok and r.name != preflight.DEVICES_CHECK_NAME]
        if not failed:
            return None
        return NOT_IN_PROFILE.format(device=phone.name or phone.udid,
                                     app=app.cyan.display_name or app.name)


def readiness_for(
    cfg: Config, targets: list[tuple[AppConfig, str]], probe,
) -> dict[tuple[str, str], Readiness]:
    """Each (app, device) target's readiness, keyed by (app name, UDID). Each
    device is asked whether it's connected, and described, once; nothing is
    asked of any device when `targets` is empty."""
    out: dict[tuple[str, str], Readiness] = {}
    connected: dict[str, tuple[str | None, str | None]] = {}
    seen: dict[str, Phone | str] = {}
    for app, udid in targets:
        target = Target(delivery=app.delivery, udid=udid,
                        lc_bundle_id=app.lc_bundle_id if app.delivery == LIVECONTAINER else None)
        if udid not in connected:
            connected[udid] = connection(cfg, app, udid)
        name, waiting = connected[udid]
        target = replace(target, device_name=name)
        if waiting is not None:
            out[(app.name, udid)] = Readiness(target, waiting=waiting)
            continue
        if udid not in seen:
            seen[udid] = probe.describe(udid)
        described = seen[udid]
        if isinstance(described, str):
            out[(app.name, udid)] = Readiness(target, blocked_reason=described)
            continue
        label = described.name or name or described.udid
        target = replace(target, device_name=described.name or name)
        blocked: str | None = None
        note: str | None = None
        if app.delivery == LIVECONTAINER:
            found, why = probe.livecontainer(cfg, described, app)
            target = replace(target, lc_bundle_id=found)
            if why is not None and probe.blocks:
                blocked = why
        if blocked is None and developer_mode.needed(cfg, app) is True:
            if probe.developer_mode(described) is False:
                if probe.blocks:
                    blocked = DEVELOPER_MODE_OFF
                else:
                    note = DEVELOPER_MODE_WAS_OFF.format(device=label)
        if blocked is None and app.delivery == SIDELOAD:
            blocked = probe.registered(cfg, described, app)
        out[(app.name, udid)] = Readiness(target, blocked_reason=blocked, note=note)
    return out
