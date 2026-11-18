"""Pre-flight checks: CLI tools, certs, profile, env vars, device.

Each check returns a structured CheckResult so the CLI can print a tidy table.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import mobileprovision
from .config import AppConfig, Config
from .errors import PreflightError, SigningError
from .signer import ProfilePlan, auto_profile_device_udids, plan_profile_for_app


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # Stable identifier the GUI can match on to surface inline actions
    # (e.g. "set-p12-password" → "Set Password…" button). Independent of
    # the human-readable `name`, which is free to change.
    action_key: str | None = None

    @property
    def status(self) -> str:
        return "OK" if self.ok else "FAIL"


REQUIRED_TOOLS = {
    "cyan": "pipx install --force https://github.com/asdfzxcvbn/pyzule-rw/archive/main.zip",
    "zsign": "brew install zsign",
    "ideviceinstaller": "brew install ideviceinstaller",
    "idevice_id": "brew install libimobiledevice",
    "otool": "xcode-select --install",
    "security": "(builtin) /usr/bin/security",
}


def check_tools(tools: Iterable[str] | None = None) -> list[CheckResult]:
    names = list(tools) if tools is not None else list(REQUIRED_TOOLS)
    results = []
    for tool in names:
        install = REQUIRED_TOOLS[tool]
        path = shutil.which(tool)
        if path:
            results.append(CheckResult(name=tool, ok=True, detail=path))
        else:
            results.append(
                CheckResult(name=tool, ok=False, hint=f"install with: {install}")
            )
    return results


def required_tools_for(delivery: str, will_install: bool) -> list[str]:
    """The external tools a build actually invokes for this delivery mode.

    livecontainer never signs (no zsign) and only talks to a device when it
    pushes the IPA into LiveContainer; sideload always signs and installs.
    `otool` is needed in both modes for the cryptid (decryption) check.
    """
    if delivery == "livecontainer":
        tools = ["cyan", "otool"]
        if will_install:
            tools.append("idevice_id")
        return tools
    tools = ["cyan", "zsign", "otool", "security"]
    if will_install:
        tools += ["ideviceinstaller", "idevice_id"]
    return tools


def planned_profile(cfg: Config, app: AppConfig | None) -> ProfilePlan | None:
    """The profile that will actually sign `app`, or None if none can be named.

    Delegates to `signer.plan_profile_for_app` rather than reading
    `cfg.signing.profile` directly, so a per-app `profile:` override or an
    `auto_profile` app is checked against the file that will sign it. Validating
    the global wildcard instead both failed builds that were configured
    correctly and passed a device check against a profile that was never used.

    Planning is deliberate: `resolve_profile_for_app` would auto-provision, and
    pre-flight (including a bare `--check`) must not write to the portal.

    None means the config cannot name a profile at all — `check_signing_files`
    reports that as a failed check, so the other checks stay quiet about it.
    """
    try:
        return plan_profile_for_app(cfg, app)
    except SigningError:
        return None


def check_signing_files(cfg: Config, *, app: AppConfig | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(
        CheckResult(
            name="signing.cert (.p12)",
            ok=cfg.signing.cert.exists(),
            detail=str(cfg.signing.cert),
            hint="copy the .p12 exported from Keychain to this path",
        )
    )
    results.append(_check_profile_present(cfg, app))
    from . import keychain
    env_var = cfg.signing.cert_password_env
    has_env = bool(os.environ.get(env_var))
    in_keychain = bool(cfg.signing.team_id and keychain.read_p12_password(cfg.signing.team_id))
    if has_env:
        detail, ok = f"env {env_var} set", True
    elif in_keychain:
        detail, ok = "keychain entry present", True
    else:
        detail, ok = "(missing)", False
    results.append(
        CheckResult(
            name=".p12 password",
            ok=ok,
            detail=detail,
            hint=f"run `graft --set-p12-password`  OR  export {env_var}=<your .p12 password>",
            action_key="set-p12-password",
        )
    )
    return results


PROFILE_CHECK_NAME = "signing profile (.mobileprovision)"


def _check_profile_present(cfg: Config, app: AppConfig | None) -> CheckResult:
    try:
        plan = plan_profile_for_app(cfg, app)
    except SigningError as e:
        # e.g. auto_profile with no app_store_connect section — there is no
        # profile to point at, so the message is the whole detail.
        return CheckResult(name=PROFILE_CHECK_NAME, ok=False, detail=e.message, hint=e.hint or "")
    detail = f"{plan.description}: {plan.path}"
    if plan.created_on_demand and not plan.path.exists():
        # Not a failure: stage 4 generates it. Saying so beats a red row on a
        # config that builds fine.
        return CheckResult(
            name=PROFILE_CHECK_NAME,
            ok=True,
            detail=detail + " (not cached yet; App Store Connect provisions it at sign time)",
        )
    return CheckResult(
        name=PROFILE_CHECK_NAME,
        ok=plan.path.exists(),
        detail=detail,
        hint="download the provisioning profile from developer.apple.com",
    )


def parse_mobileprovision(profile: Path) -> dict:
    """Decode a .mobileprovision and return the embedded plist as a dict.

    Kept as a name here — it is what this module's callers and their tests
    reach for — but the decoder itself lives in `mobileprovision`, below
    `signer` in the import graph, so `apple_portal` can use the same one when
    it decides whether its cached profile is still good.
    """
    return mobileprovision.decode(profile)


PROFILE_PARSE_CHECK_NAME = "profile parse"
PROFILE_DEVICES_CHECK_NAME = "profile devices"

# What a passing row says about a cache stage 4 is about to replace.
_REFRESH_NOTE = "App Store Connect re-provisions it at sign time"


def cached_profile_refresh_note(cfg: Config, plan: ProfilePlan) -> str | None:
    """Why stage 4 will replace this profile, or None if it needs no help.

    One function answers "is this cached profile good?" — `cache_problem` — and
    both callers consult it. `ensure_profile_for_bundle` reads a non-None answer
    as "re-provision"; pre-flight reads the same answer as "do not fail the
    build for this", because the repair happens at stage 4 and a failed check
    aborts before stage 2.

    Deciding it in one place is what stops the two from disagreeing, and they
    did — on the one condition pre-flight had never implemented. `cache_problem`
    has three: expired, missing a device, and *undecodable*. Pre-flight
    re-derived the first two by hand and hard-failed on the third, so an
    auto-provisioned cache file truncated by a full volume or left half-written
    by an interrupted download stopped every build of that app before the only
    code that could heal it, with no remedy but deleting a file under ~/.config
    by hand. That is the dead end this whole rule exists to remove, surviving in
    the one branch the generalisation skipped.

    None for a profile the user manages: nothing in the product will replace it,
    so a failure there is theirs to act on and saying otherwise would be a lie
    with a build-shaped consequence.
    """
    if not plan.created_on_demand:
        return None
    return mobileprovision.cache_problem(
        plan.path, device_udids=auto_profile_device_udids(cfg)
    )


def check_profile_contents(cfg: Config, *, app: AppConfig | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    plan = planned_profile(cfg, app)
    if plan is None or not plan.path.exists():
        # Missing, or an auto profile that doesn't exist yet: nothing to parse.
        # Both are already accounted for by check_signing_files.
        return results
    try:
        plist = parse_mobileprovision(plan.path)
    except PreflightError as e:
        note = cached_profile_refresh_note(cfg, plan)
        if note is not None:
            # A cache we cannot read is a cache miss, not a broken install.
            # Nothing further is checkable — there is no plist to read a team id
            # or an expiry out of — so this row is the whole report.
            return [
                CheckResult(
                    name=PROFILE_PARSE_CHECK_NAME,
                    ok=True,
                    detail=f"{note} — {_REFRESH_NOTE}",
                )
            ]
        results.append(CheckResult(name=PROFILE_PARSE_CHECK_NAME, ok=False, hint=str(e)))
        return results

    team_ids: list[str] = plist.get("TeamIdentifier") or []
    expected = cfg.signing.team_id
    team_ok = expected in team_ids
    results.append(
        CheckResult(
            name="profile team id matches config",
            ok=team_ok,
            detail=f"profile={team_ids} config={expected}",
            hint="update signing.team_id in config.yaml to match the profile",
        )
    )

    expiry = mobileprovision.expiration(plist)
    if expiry is not None:
        remaining = expiry - datetime.now(UTC)
        # Compare instants, not whole days. `timedelta(hours=9).days` is 0, so
        # truncating to days called a profile with nine hours of life left
        # expired — and since a failed check aborts before stage 2, every
        # sideload build was refused for the profile's entire last day, on a
        # profile zsign would have signed with happily.
        ok = remaining > timedelta(0)
        detail = f"{expiry.isoformat()} ({_format_remaining(remaining)})"
        hint = "regenerate the profile in the Apple Developer portal" if not ok else ""
        if not ok and cached_profile_refresh_note(cfg, plan) is not None:
            # An auto-provisioned profile is a cache, not a file the user
            # manages, and `ensure_profile_for_bundle` replaces an expired one
            # during stage 4. Failing here aborted the build BEFORE the only
            # code that can fix it. Asked of `cache_problem` rather than
            # re-derived, so this row and stage 4 cannot reach opposite verdicts
            # about the same file.
            ok, hint = True, ""
            detail += f" — cached copy; {_REFRESH_NOTE}"
        results.append(
            CheckResult(name="profile expiry", ok=ok, detail=detail, hint=hint)
        )
    return results


def _format_remaining(remaining: timedelta) -> str:
    """Phrase how much life a profile has left, with sub-day resolution.

    "expires in 9 hours" is a warning the user can act on today; the whole-day
    arithmetic it replaces rendered the same profile as "0 days left" and then
    failed the check outright. CheckResult is a two-state type and the
    `preflight-check` JSON event carries a bool `ok`, so the urgency lives in
    this string rather than in a third status the GUI has no way to decode.
    """
    seconds = remaining.total_seconds()
    if seconds <= 0:
        return f"expired {_approx_duration(-seconds)} ago"
    return f"expires in {_approx_duration(seconds)}"


def _approx_duration(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        count = int(seconds // size)
        if count:
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return "under a minute"


def list_connected_devices() -> list[str]:
    """Return UDIDs of all paired iOS devices (USB and Wi-Fi)."""
    return [udid for udid, _conn in list_connected_devices_with_transport()]


def list_connected_devices_with_transport() -> list[tuple[str, str]]:
    """Return (udid, transport) pairs. transport ∈ {"usb", "network"}.

    `idevice_id -l -n` emits lines like:
        00008030-0000000000000001
        00008030-0000000000000001 (Network)

    If the same UDID appears via both transports (USB cable + Wi-Fi pairing),
    keep the first occurrence position but upgrade transport to "usb" — USB
    is more reliable for large IPAs.
    """
    if shutil.which("idevice_id") is None:
        return []
    try:
        out = subprocess.run(
            ["idevice_id", "-l", "-n"], check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError:
        return []
    order: list[str] = []
    transport_by_udid: dict[str, str] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("(Network)"):
            udid, transport = line.removesuffix("(Network)").strip(), "network"
        elif line.endswith("(USB)"):
            # Newer libimobiledevice tags USB devices explicitly; older builds
            # printed the bare UDID. Strip the suffix either way.
            udid, transport = line.removesuffix("(USB)").strip(), "usb"
        else:
            udid, transport = line, "usb"
        if udid not in transport_by_udid:
            order.append(udid)
            transport_by_udid[udid] = transport
        elif transport == "usb":
            transport_by_udid[udid] = "usb"
    return [(udid, transport_by_udid[udid]) for udid in order]


def check_devices(
    cfg: Config, *, check_profile: bool = True, app: AppConfig | None = None
) -> list[CheckResult]:
    results: list[CheckResult] = []
    udids = list_connected_devices()
    results.append(
        CheckResult(
            name="connected ios device(s)",
            ok=bool(udids),
            detail=", ".join(udids) if udids else "none",
            hint="plug an iPhone in via USB and trust this Mac",
        )
    )
    # The provisioning-profile device match only matters when we sign; a
    # livecontainer build runs unsigned, so its profile is irrelevant here.
    #
    # `plan` is the profile that will sign THIS app. Reading the global wildcard
    # instead reported "device in provisioning profile: OK" off a file the build
    # would never use — green while the profile that actually signs may not list
    # the connected phone. When there is no profile on disk to read (an
    # auto_profile app whose profile Apple has yet to generate), there is
    # nothing to assert, and no row is better than a green one.
    plan = planned_profile(cfg, app) if check_profile and udids else None
    if plan is not None and plan.path.exists():
        try:
            # `mobileprovision.provisioned_devices`, not a second copy of it:
            # "an absent key means a distribution profile, not zero devices" is
            # a rule `cache_problem` also depends on, and it was written out
            # twice — three lines apart, which is how they stay in step until
            # the day they don't.
            provisioned = mobileprovision.provisioned_devices(
                parse_mobileprovision(plan.path)
            )
        except PreflightError as e:
            note = cached_profile_refresh_note(cfg, plan)
            if note is not None:
                # The self-heal `check_profile_contents` applies to the
                # identical branch, which this one did not get. Without it a
                # truncated cache produced `profile parse: OK` and `profile
                # devices: FAIL` about the same file — two rows contradicting
                # each other — and the failing one aborted the build before
                # stage 2, so `ensure_profile_for_bundle`, the only code that
                # recreates the cache, was never reached. Every build of that
                # app failed identically and the only remedy was `rm` under
                # ~/.config: the dead end this rule exists to remove, surviving
                # in the branch the fix skipped.
                results.append(
                    CheckResult(
                        name=PROFILE_DEVICES_CHECK_NAME,
                        ok=True,
                        detail=f"{note} — {_REFRESH_NOTE}",
                    )
                )
                return results
            results.append(
                CheckResult(name=PROFILE_DEVICES_CHECK_NAME, ok=False, hint=str(e))
            )
            return results
        # Development profiles list devices; distribution profiles do not.
        if provisioned:
            # Case-insensitively, like `cache_problem` and like the
            # `configured`/`renewing` pair below. ASC normalises UDIDs to lower
            # case in places and config.yaml does not, and an exact match here
            # against case-insensitive rules three lines away made pre-flight
            # promise a refresh that never happens — or hard-fail a build whose
            # profile does list the phone.
            listed = {u.lower() for u in provisioned}
            matches = [u for u in udids if u.lower() in listed]
            ok = bool(matches)
            detail = (
                ", ".join(matches)
                if matches
                else f"connected={udids} provisioned={provisioned}"
            )
            hint = (
                "add this device's UDID in the Apple Developer portal and "
                "regenerate the profile"
            )
            if not ok and plan.created_on_demand:
                # Same self-healing rule as the expiry row, with one extra
                # condition: stage 4 re-provisions for the UDIDs the config
                # names, so a connected phone is only "about to be added" if it
                # is one of them. A phone nobody configured would fail at zsign
                # instead, which is a worse place to learn it.
                configured = {c.lower() for c in auto_profile_device_udids(cfg)}
                renewing = [u for u in udids if u.lower() in configured]
                if renewing:
                    ok, hint = True, ""
                    detail = (
                        f"{', '.join(renewing)} not in the cached profile — App "
                        "Store Connect re-provisions it at sign time"
                    )
            results.append(
                CheckResult(
                    name="device in provisioning profile",
                    ok=ok,
                    detail=detail,
                    hint=hint,
                )
            )
    return results


def run_all_checks(
    cfg: Config,
    *,
    delivery: str = "sideload",
    will_install: bool = True,
    app: AppConfig | None = None,
) -> list[CheckResult]:
    """Pre-flight checks scoped to what this build actually needs.

    Defaults reproduce the original mode-agnostic, full check set so the global
    `--check` command and other callers are unaffected. A `livecontainer` build
    skips the entire signing identity (zsign, .p12, profile) since it runs the
    IPA unsigned; device checks are skipped unless the build will push to a
    device (`will_install`).

    `app` makes the signing checks app-aware as well as delivery-aware: without
    it every check validates the global `signing.profile`, even for an app that
    signs with a per-app `profile:` override or with `auto_profile`. Leave it
    None for the mode-agnostic `--check` command, which has no app in hand.
    """
    is_livecontainer = delivery == "livecontainer"
    results: list[CheckResult] = []
    results.extend(check_tools(required_tools_for(delivery, will_install)))
    if not is_livecontainer:
        results.extend(check_signing_files(cfg, app=app))
        results.extend(check_profile_contents(cfg, app=app))
    if will_install:
        results.extend(check_devices(cfg, check_profile=not is_livecontainer, app=app))
    return results


def assert_all_passed(results: Iterable[CheckResult]) -> None:
    failed = [r for r in results if not r.ok]
    if failed:
        names = ", ".join(r.name for r in failed)
        raise PreflightError(
            f"{len(failed)} pre-flight check(s) failed: {names}",
            hint="run `graft --check` for details",
        )
