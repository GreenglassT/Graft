"""Pre-flight checks: CLI tools, certs, profile, env vars, device.

Each check returns a structured CheckResult so the CLI can print a tidy table.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import install_hints, mobileprovision
from . import tools as resolver
from .config import DELIVERIES, AppConfig, Config, paid_upgrade
from .cyan_runner import CYAN_INSTALL_COMMAND
from .errors import GraftError, PaidAccountRequiredError, PreflightError, SigningError
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
    # A row that reports a fact rather than passing a test: the account
    # mode, or no device connected where none is needed (`check_devices`).
    # Always `ok`: nothing about it can fail, and counting it as a failure
    # would turn a free Apple ID, or a phone left in a pocket, into a broken
    # setup.
    info: bool = False
    # The exact command that installs what this row found missing, or None.
    # The same command the hint says after "install with: ", without the
    # prose around it, so the GUI can offer it to copy.
    install: str | None = None
    # For a tool row: which rung of `tools.resolve` found it ("bundled",
    # "PATH", ...). None on every other row.
    source: str | None = None

    @property
    def status(self) -> str:
        if self.info:
            return "INFO"
        return "OK" if self.ok else "FAIL"


REQUIRED_TOOLS = {
    "cyan": CYAN_INSTALL_COMMAND,
    "zsign": "brew install zsign",
    "security": "(builtin) /usr/bin/security",
}


def _install_command(tool: str) -> str | None:
    """`REQUIRED_TOOLS[tool]` when it is a command to run; None for a builtin."""
    command = REQUIRED_TOOLS[tool]
    return None if command.startswith("(") else command


def _install_hint(tool: str) -> str:
    return "install with: " + install_hints.with_prerequisites(REQUIRED_TOOLS[tool])


def check_tools(tools: Iterable[str] | None = None) -> list[CheckResult]:
    names = list(tools) if tools is not None else list(REQUIRED_TOOLS)
    results = []
    for tool in names:
        r = resolver.resolve(tool)
        if r.found:
            detail = f"{r.path} (bundled)" if r.source == resolver.SOURCE_BUNDLED else str(r.path)
            results.append(CheckResult(name=tool, ok=True, detail=detail, source=r.source))
        elif (bundled := resolver.bundled_tools_dir()) is not None and tool in resolver.BUNDLED_TOOLS:
            # Graft.app carries it, so missing means a damaged app: the pipx or
            # Homebrew command would be the wrong advice (review #12).
            results.append(CheckResult(name=tool, ok=False, install=None,
                                       hint=f"Graft.app is damaged: {bundled / tool} is missing. "
                                            "Download Graft again."))
        else:
            results.append(CheckResult(name=tool, ok=False, hint=_install_hint(tool),
                                       install=_install_command(tool)))
    return results


def required_tools_for(delivery: str) -> list[str]:
    """The external tools a build actually invokes for this delivery mode.

    livecontainer never signs (no zsign), and pushes into LiveContainer over
    pymobiledevice3, which comes with the CLI; sideload always signs, and
    installs over pymobiledevice3 too (`device_install`), so installing on the
    device adds no tool either way. Neither needs `otool`: the cryptid
    (decryption) check reads the load commands itself (`macho`), so no mode
    needs the Xcode Command Line Tools. Finding the device is pymobiledevice3's
    in both (`list_connected_devices`), so no mode needs libimobiledevice.
    """
    if delivery == "livecontainer":
        return ["cyan"]
    return ["cyan", "zsign", "security"]


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
    A free Apple ID has no profile either, and its own row in that function.
    """
    if cfg.free_account:
        return None
    try:
        return plan_profile_for_app(cfg, app)
    except SigningError:
        return None


TEAM_ID_CHECK_NAME = "signing.team_id"


def _delivery_refusal(
    cfg: Config, delivery: str, app: str | None
) -> PaidAccountRequiredError | None:
    """`Config.require_delivery`'s refusal, as a value for a row to carry: the
    same sentence a build of that app would fail with."""
    try:
        cfg.require_delivery(delivery, app=app)
    except PaidAccountRequiredError as e:
        return e
    return None


def check_signing_files(cfg: Config, *, app: AppConfig | None = None) -> list[CheckResult]:
    if cfg.free_account:
        # One failing row rather than four about files a free Apple ID never
        # has. Only reachable for a sideload app on a free config, which the
        # build refuses before pre-flight; this keeps `--check`-shaped callers
        # from reaching `cfg.signing` when it is None.
        err = _delivery_refusal(cfg, "sideload", app.name if app else None)
        assert err is not None  # a free account refuses sideload, always
        return [CheckResult(name=ACCOUNT_CHECK_NAME, ok=False, detail=err.message,
                            hint=err.hint or "")]
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
    # `cfg.team_id`, the one definition of "configured": None for blank,
    # whitespace and `TEAM_ID_PLACEHOLDER` alike. The detail still shows what
    # is written, so a placeholder reads as itself rather than as missing.
    team = cfg.team_id
    results.append(
        CheckResult(
            name=TEAM_ID_CHECK_NAME,
            ok=team is not None,
            detail=cfg.signing.team_id.strip() or "(missing)",
            hint="run `graft --set-settings --signing-team-id <TEAM>`",
        )
    )
    from . import keychain
    env_var = cfg.signing.cert_password_env
    has_env = bool(os.environ.get(env_var))
    # Keyed as `--set-p12-password` writes it: by `cfg.team_id`, never by a
    # placeholder it refuses to store under.
    # `is not None`: "" is a blank-password .p12's password, stored.
    try:
        in_keychain = bool(team) and keychain.read_p12_password(team) is not None
        no_answer = False
    except keychain.KeychainNoAnswer:
        in_keychain, no_answer = False, True
    if has_env:
        detail, ok = f"env {env_var} set", True
    elif in_keychain:
        detail, ok = "keychain entry present", True
    elif no_answer:
        detail, ok = keychain.NO_ANSWER, False
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
    the one branch the generalization skipped.

    None for a profile the user manages: nothing in the product will replace it,
    so a failure there is theirs to act on and saying otherwise would be a lie
    with a build-shaped consequence.
    """
    if not plan.created_on_demand:
        return None
    # The team, as stage 4 asks it (`signer.resolve_profile_for_app`). The
    # certificate stage 4 also asks about needs the .p12's password, so it
    # is left to stage 4.
    return mobileprovision.cache_problem(
        plan.path, device_udids=auto_profile_device_udids(cfg), team_id=cfg.team_id
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
    expected = cfg.team_id
    team_ok = expected is not None and expected in team_ids
    team_detail = f"profile={team_ids} config={expected}"
    # Which way to point depends on who owns the file. A profile the user
    # chose may be the wrong one or signing.team_id may be: say both. It used
    # to say only "update signing.team_id to match the profile", which after
    # a move to another team pointed back at the team just left.
    team_hint = (
        f"the profile was made by team {', '.join(team_ids) or '(none)'}; use a "
        f"profile made by {expected} (signing.team_id), or correct "
        "signing.team_id in config.yaml if the profile is the right one"
    )
    if not team_ok and cached_profile_refresh_note(cfg, plan) is not None:
        # An auto-provisioned profile from another team is a cache miss:
        # stage 4 makes a new one with this team's key (`cache_problem`'s
        # team rule), so failing here aborted the build before the only code
        # that can fix it, as an expired one did.
        team_ok, team_hint = True, ""
        team_detail += f" — cached copy; {_REFRESH_NOTE}"
    results.append(
        CheckResult(
            name="profile team id matches config",
            ok=team_ok,
            detail=team_detail,
            hint=team_hint,
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

    Asked of usbmuxd through pymobiledevice3 (`devices.list_devices_brief`), as
    the Devices tab and the LiveContainer push ask it — no lockdown session, so
    a phone that is on the bus is listed even if describing it would fail. It
    used to shell out to libimobiledevice's `idevice_id -l -n`, which a free
    Apple ID had to install for this one check and which a CLI started with a
    bare PATH could never find, so it never saw a device.

    A phone on both transports (a cable and Wi-Fi sync) is listed once, as
    USB. usbmuxd unreachable is no devices here, not an error: a run that
    needs a device fails on none, and `--check`, which doesn't, asks again
    (`usbmux_problem`) and says it couldn't look.
    """
    from .devices import list_devices_brief

    try:
        listed = list_devices_brief()
    except Exception:
        return []
    return [
        (d.udid, "network" if d.connection_type == "Network" else "usb") for d in listed
    ]


DEVICES_CHECK_NAME = "connected ios device(s)"
#: The device row's detail when none is connected and none is needed. The
#: row is information then, which shows no hint, so the detail says what to
#: do.
NOT_CONNECTED_DETAIL = (
    "not connected: plug in an iPhone or iPad with a USB cable and trust this Mac "
    "before you install on it"
)


def usbmux_problem() -> str | None:
    """Why macOS's usbmuxd couldn't say which devices are connected, or None
    when it answered. `list_connected_devices` reads no answer as no devices,
    which a run that needs one fails on either way; where none is needed
    (`check_devices`), "not connected" would hide it. Asked only then, after
    an empty listing: a second listing, without a lockdown session, is about
    0.05s."""
    from .devices import list_devices_brief

    try:
        list_devices_brief()
    except Exception as e:
        text = str(e).strip()
        return f"{type(e).__name__}: {text}" if text else type(e).__name__
    return None


def check_devices(
    cfg: Config,
    *,
    check_profile: bool = True,
    app: AppConfig | None = None,
    udids: list[str] | None = None,
    required: bool = True,
    target: str | None = None,
) -> list[CheckResult]:
    """`udids` is the connected devices when the caller has already listed
    them; otherwise they are listed here.

    `required` is whether this run needs a device: a build that installs
    does, so none connected fails. `--check` doesn't: there, none connected
    is information (`NOT_CONNECTED_DETAIL`), which Settings › Preflight draws
    as such and counts as neither OK nor Fail. Graft not being able to ask
    (`usbmux_problem`) is a failure either way."""
    results: list[CheckResult] = []
    udids = list_connected_devices() if udids is None else udids
    problem = usbmux_problem() if not udids and not required else None
    if udids or required:
        results.append(
            CheckResult(
                name=DEVICES_CHECK_NAME,
                ok=bool(udids),
                detail=", ".join(udids) if udids else "none",
                hint="plug an iPhone in via USB and trust this Mac",
            )
        )
    elif problem is None:
        results.append(CheckResult(name=DEVICES_CHECK_NAME, ok=True, info=True,
                                   detail=NOT_CONNECTED_DETAIL))
    else:
        results.append(
            CheckResult(
                name=DEVICES_CHECK_NAME,
                ok=False,
                detail=f"couldn't ask macOS which devices are connected: {problem}",
                hint="plug the iPhone or iPad in again; if Graft still can't see it, "
                     "restart this Mac",
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
            # `configured`/`renewing` pair below. ASC normalizes UDIDs to lower
            # case in places and config.yaml does not, and an exact match here
            # against case-insensitive rules three lines away made pre-flight
            # promise a refresh that never happens — or hard-fail a build whose
            # profile does list the phone.
            listed = {u.lower() for u in provisioned}
            # The build's own device when it has one (`installer.resolve_target`):
            # a profile listing another connected device is no use to an
            # install on this one. With none (`--check`), any connected
            # device in the profile is enough.
            judged = [target] if target else udids
            matches = [u for u in judged if u.lower() in listed]
            ok = bool(matches)
            detail = (
                ", ".join(matches)
                if matches
                else f"this build's device {target} isn't in it; provisioned={provisioned}"
                if target
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
                # The build's own device is one stage 4 provisions for too
                # (`signer.target_profile_udids`), after re-reading the
                # registered devices if it has to.
                configured = {c.lower() for c in auto_profile_device_udids(cfg, target=target)}
                renewing = [u for u in judged if u.lower() in configured]
                if renewing:
                    ok, hint = True, ""
                    detail = (
                        f"{', '.join(renewing)} not in the cached profile — App "
                        "Store Connect re-provisions it at sign time"
                    )
                else:
                    # Not the portal: Xcode may well have registered the
                    # phone there already. The list auto-provisioning covers
                    # is the snapshot Save and test took.
                    hint = (
                        "auto-provisioning covers app_store_connect.device_udids "
                        "and the default device, and this phone is neither. Once "
                        "it is registered at developer.apple.com -> Devices, press "
                        "Save and test in Settings › App Store Connect (or run "
                        "`graft --set-portal` again) to refresh the list, or make "
                        "it the default device"
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


def deliveries_in_use(cfg: Config) -> list[str]:
    """The delivery modes a build of this config would use, in `DELIVERIES`
    order: every configured app's, or with no apps yet the account's default
    (sideload for paid), which is what the first app added will get."""
    used = {a.delivery for a in cfg.apps.values()} or {cfg.default_delivery}
    return [d for d in DELIVERIES if d in used]


def run_all_checks(
    cfg: Config,
    *,
    delivery: str = "sideload",
    will_install: bool = True,
    app: AppConfig | None = None,
    deliveries: Iterable[str] | None = None,
    device_required: bool = True,
    target: str | None = None,
) -> list[CheckResult]:
    """Pre-flight checks scoped to what this build actually needs.

    A `livecontainer` build skips the entire signing identity (zsign, .p12,
    profile) since it runs the IPA unsigned; device checks are skipped unless
    the build will push to a device (`will_install`).

    `app` makes the signing checks app-aware as well as delivery-aware: without
    it every check validates the global `signing.profile`, even for an app that
    signs with a per-app `profile:` override or with `auto_profile`. Leave it
    None for the `--check` command, which has no app in hand.

    `deliveries` is `--check`'s, for a paid config: every mode its apps use
    (`deliveries_in_use`), in place of `delivery`. The tools are the ones
    those modes run — zsign only when some app sideloads, so a paid user
    whose apps all go into LiveContainer is not told a tool is missing on
    every launch — and the profile's device
    list only matters then too. The signing rows stay: a paid config's
    identity is what `--lc-install` and `--lc-push-cert` sign with, and
    those report the tools they need when they run.

    `device_required` is False for `--check`, which installs nothing: no
    device connected is then information, not a failure (`check_devices`).

    `target` is the device a build will install on (`installer.resolve_target`):
    the profile check counts it as one stage 4 provisions for.
    """
    modes = [delivery] if deliveries is None else list(deliveries)
    sideloads = "sideload" in modes
    tools: list[str] = []
    for mode in modes:
        tools.extend(t for t in required_tools_for(mode) if t not in tools)
    results: list[CheckResult] = []
    results.extend(check_tools(tools))
    if sideloads or deliveries is not None:
        results.extend(check_signing_files(cfg, app=app))
        results.extend(check_profile_contents(cfg, app=app))
    if will_install:
        results.extend(check_devices(cfg, check_profile=sideloads, app=app,
                                     required=device_required, target=target))
    return results


def run_export_checks() -> list[CheckResult]:
    """Pre-flight for `--export-ipa`: cyan, and nothing else.

    An export stops once cyan has injected the tweaks, so it needs no signing
    identity, no device and no LiveContainer, whatever the app's delivery mode
    and whichever account this is. Checking them would refuse an export that
    can't fail for want of them.
    """
    return check_tools(["cyan"])


ACCOUNT_CHECK_NAME = "account"
FREE_ACCOUNT_DETAIL = "free Apple ID: LiveContainer delivery only"
LIVECONTAINER_CHECK_NAME = "LiveContainer on device"


def check_account(cfg: Config) -> CheckResult:
    """The account mode, as information. Only `--check` on a free Apple ID
    shows it: a paid config's signing rows already say what it is."""
    return CheckResult(
        name=ACCOUNT_CHECK_NAME,
        ok=True,
        info=True,
        detail=FREE_ACCOUNT_DETAIL if cfg.free_account else "paid Apple Developer account",
    )


def _installed_bundle_ids(udid: str) -> list[str]:
    """The bundle ids on `udid`, System apps included and no other attribute
    asked for — one listing, which `check_livecontainer` reuses for every pin."""
    from .lc_manage import UsbmuxDevice

    return UsbmuxDevice().installed_bundle_ids(udid)


def check_livecontainer(
    cfg: Config,
    udids: list[str],
    *,
    installed_apps: Callable[[str], Iterable[str]] | None = None,
) -> list[CheckResult]:
    """Would a LiveContainer push find its container on the device?

    Asked the way the push asks it (`lc_discovery.choose`), once per pin the
    LiveContainer apps carry — an unpinned app and one pinned to LiveContainer2
    look for different things — and once unpinned when there are no such apps
    yet. The device checked is the default one when it is connected, else
    every connected one, each listed once. No device, no row: the device row
    already says there's none.

    `installed_apps` defaults to `_installed_bundle_ids`, looked up when the
    check runs rather than bound at import, so a test that replaces the module
    attribute replaces what `--check` calls.
    """
    from . import lc_discovery
    from .lc_runner import LiveContainerError

    if not udids:
        return []
    if installed_apps is None:
        installed_apps = _installed_bundle_ids
    targets = [cfg.default_device_udid] if cfg.default_device_udid in udids else udids
    pins = {a.lc_bundle_id for a in cfg.apps.values() if a.delivery == "livecontainer"}
    results = []
    for udid in targets:
        try:
            installed = list(installed_apps(udid))
        except GraftError as e:
            results.append(CheckResult(
                name=LIVECONTAINER_CHECK_NAME, ok=False,
                detail=f"{udid}: could not list its apps: {e.message}", hint=e.hint or "",
            ))
            continue
        if not installed:
            # A locked phone lists nothing at all, not even system apps: that
            # is no answer, not "no LiveContainer here" (the build reads the
            # same evidence the same way, `lc_runner._ListedNoApps`).
            results.append(CheckResult(
                name=LIVECONTAINER_CHECK_NAME, ok=False,
                detail=f"{udid}: the device listed no apps at all, which is how a locked phone answers",
                hint="unlock the device, keep it on the home screen, and run `graft --check` again",
            ))
            continue
        chosen: set[str] = set()
        problem: LiveContainerError | None = None
        for pin in sorted(pins or {None}, key=lambda p: p or ""):
            try:
                chosen.add(lc_discovery.choose(
                    installed, pinned=pin, team=cfg.team_id, free_account=cfg.free_account,
                )[0])
            except LiveContainerError as e:
                problem = problem or e
        if problem is not None:
            results.append(CheckResult(
                name=LIVECONTAINER_CHECK_NAME, ok=False,
                detail=f"{udid}: {problem.message}", hint=problem.hint or "",
            ))
            continue
        others = [b for b in lc_discovery.livecontainers(installed, cfg.team_id)
                  if b not in chosen]
        detail = f"{udid}: {', '.join(sorted(chosen))}"
        if others:
            detail += f" (also installed: {', '.join(others)})"
        results.append(CheckResult(name=LIVECONTAINER_CHECK_NAME, ok=True, detail=detail))
    return results


def run_free_account_checks(
    cfg: Config,
    *,
    installed_apps: Callable[[str], Iterable[str]] | None = None,
) -> list[CheckResult]:
    """`graft --check` for a free Apple ID.

    What a LiveContainer build needs and nothing it does not: the account as
    information, cyan, a device (none connected is information too), and a
    LiveContainer on it. No zsign, no .p12, profile or password — a free
    Apple ID has none of them, and a red row for each would read as a broken
    setup rather than a different one.
    `installed_apps` is `check_livecontainer`'s, None for the real device.

    But an app the config does say to sideload is a failure: every build of
    it is refused (`check_app_deliveries`).
    """
    udids = list_connected_devices()
    return [
        check_account(cfg),
        *check_app_deliveries(cfg),
        *check_tools(required_tools_for("livecontainer")),
        *check_devices(cfg, check_profile=False, udids=udids, required=False),
        *check_livecontainer(cfg, udids, installed_apps=installed_apps),
    ]


def check_app_deliveries(cfg: Config) -> list[CheckResult]:
    """One failing row per app whose delivery this account cannot do.

    On a free Apple ID that is an app with an explicit `delivery: sideload`,
    carried over from a paid config: it loads, so `--list` and every other
    verb keep working, and each build of it is refused. The row says so with
    the build's own sentence (`Config.require_delivery`) and the one-line fix.
    """
    rows = []
    for name, app in sorted(cfg.apps.items()):
        err = _delivery_refusal(cfg, app.delivery, name)
        if err is None:
            continue
        rows.append(CheckResult(
            name=f"app '{name}'", ok=False, detail=err.message,
            hint=f"graft --set-app-options --app {name} --delivery livecontainer; or, "
            f"with a paid membership, {paid_upgrade(cfg.path, short=True)}",
        ))
    return rows


def assert_all_passed(results: Iterable[CheckResult]) -> None:
    failed = [r for r in results if not r.ok]
    if failed:
        names = ", ".join(r.name for r in failed)
        raise PreflightError(
            f"{len(failed)} pre-flight check(s) failed: {names}",
            hint="run `graft --check` for details",
        )
