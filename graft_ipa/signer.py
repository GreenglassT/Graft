"""Wrap zsign for re-signing the tweak-injected IPA."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import apple_portal, keychain, mobileprovision
from .config import AppConfig, Config
from .errors import SigningError


def resolve_p12_password(cfg: Config) -> str | None:
    """Read the .p12 password from env first, then macOS Keychain."""
    pw = os.environ.get(cfg.signing.cert_password_env)
    if pw:
        return pw
    if cfg.signing.team_id:
        return keychain.read_p12_password(cfg.signing.team_id)
    return None


@dataclass
class SignResult:
    output_ipa: Path
    team_id: str | None
    cert_subject: str | None
    expiry: str | None
    stdout: str
    stderr: str
    # Everything auto-provisioning did that the user would otherwise have to
    # reverse-engineer: a round trip to Apple that replaced a cached profile,
    # and any configured device App Store Connect does not have. The build
    # turns each into a warning.
    #
    # A tuple rather than the single `profile_note` it replaces, because there
    # is now more than one thing worth saying and they are independent — a
    # refresh can happen with or without a skipped device, and a skipped device
    # is worth saying on a build that refreshed nothing. Two fields would have
    # invited a third; the build loops.
    profile_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedProfile:
    """The profile a real signing run will use, and why it had to be fetched."""

    path: Path
    refresh_reason: str | None = None
    # Configured UDIDs ASC has no enabled device for. The profile does not
    # cover them and no refresh can make it; see `ensure_profile_for_bundle`.
    unregistered_devices: tuple[str, ...] = ()


_TEAM_RE = re.compile(r"TeamId(?:entifier)?[:\s]+([A-Z0-9]+)", re.IGNORECASE)
_SUBJECT_RE = re.compile(r"Subject[:\s]+(.+)")
_EXPIRY_RE = re.compile(r"(?:Expires?|ExpirationDate)[:\s]+([^\n]+)", re.IGNORECASE)


# Where a signing profile comes from. `plan_profile_for_app` is the only place
# that decides; everything downstream — the real portal call, --dry-run's
# rendering, pre-flight — dispatches on these instead of re-deriving the
# precedence, so the profile that gets validated is the one that gets used.
PROFILE_SOURCE_OVERRIDE = "app-override"
PROFILE_SOURCE_AUTO = "auto-profile"
PROFILE_SOURCE_GLOBAL = "global"


@dataclass(frozen=True)
class ProfilePlan:
    """Which .mobileprovision an app will sign with, decided without side effects."""

    path: Path
    source: str
    description: str  # human phrasing for check details and --dry-run output

    @property
    def created_on_demand(self) -> bool:
        """True when Apple writes this file at sign time.

        An auto-provisioned profile that isn't on disk yet is normal, not a
        misconfiguration: `resolve_profile_for_app` creates it during stage 4.
        """
        return self.source == PROFILE_SOURCE_AUTO

    def require_local(self) -> Path:
        """The planned path, checked to be present on disk.

        Auto-provisioned profiles are exempt — see `created_on_demand`.
        """
        if self.created_on_demand or self.path.exists():
            return self.path
        raise SigningError(
            f"{self.description} not found: {self.path}",
            hint="download the provisioning profile from developer.apple.com",
        )


def plan_profile_for_app(cfg: Config, app: AppConfig | None) -> ProfilePlan:
    """Decide which .mobileprovision will sign `app`. Touches nothing.

    Priority:
        1. Per-app explicit override (app.profile_override)
        2. App Store Connect auto-provisioning (app.auto_profile + cfg.app_store_connect)
        3. Global signing.profile (the default wildcard)

    No network calls, no filesystem writes, and no requirement that the file
    already exist — this is what --dry-run and pre-flight use so they can name
    the right profile without registering anything in the user's developer
    account. Raises SigningError only when the config cannot name a profile at
    all; use `require_local()` on the result to enforce that the file is there.
    """
    if app and app.profile_override:
        return ProfilePlan(
            path=app.profile_override,
            source=PROFILE_SOURCE_OVERRIDE,
            description=f"profile override for {app.name}",
        )

    if app and app.auto_profile:
        asc_cfg = cfg.app_store_connect
        if asc_cfg is None:
            raise SigningError(
                f"app '{app.name}' has auto_profile but no app_store_connect "
                "section is configured",
                hint="run `graft --setup-portal` to register an ASC API key",
            )
        if not _auto_profile_device_udids(cfg, asc_cfg):
            raise SigningError(
                "auto_profile needs at least one device UDID",
                hint="set default_device_udid in config.yaml or app_store_connect.device_udids",
            )
        pattern = (
            app.bundle_id
            if app.exact_app_id
            else apple_portal.derive_wildcard_for(app.bundle_id)
        )
        return ProfilePlan(
            # apple_portal's own cache-naming rule rather than a restatement of
            # it: a second copy would let pre-flight validate one file while
            # ensure_profile_for_bundle writes another.
            path=apple_portal.profile_cache_path(asc_cfg.profile_cache_dir, pattern),
            source=PROFILE_SOURCE_AUTO,
            description=f"App Store Connect profile for {pattern}",
        )

    return ProfilePlan(
        path=cfg.signing.profile,
        source=PROFILE_SOURCE_GLOBAL,
        description="signing.profile",
    )


def _auto_profile_device_udids(cfg: Config, asc_cfg) -> list[str]:
    return asc_cfg.device_udids or (
        [cfg.default_device_udid] if cfg.default_device_udid else []
    )


def auto_profile_device_udids(cfg: Config) -> list[str]:
    """The UDIDs an auto-provisioned profile will be created for. Touches nothing.

    Public because pre-flight needs the same answer: it downgrades "connected
    phone isn't in the cached profile" from a failure to a note *only* for a
    phone stage 4 is actually going to register, and this is the one rule
    saying which those are. Empty when the app can't auto-provision at all.
    """
    asc_cfg = cfg.app_store_connect
    if asc_cfg is None:
        return []
    return _auto_profile_device_udids(cfg, asc_cfg)


def resolve_profile_for_app(
    cfg: Config,
    app: AppConfig | None,
    *,
    password: str | None,
) -> ResolvedProfile:
    """Return the .mobileprovision to use when signing `app`.

    MAY WRITE TO THE APPLE DEVELOPER PORTAL: for an `auto_profile` app this
    registers a Bundle ID in the user's account if none matches, creates a
    provisioning profile, and caches the .mobileprovision on disk. Callers that
    must not do any of that — `--dry-run`, pre-flight — call
    `plan_profile_for_app` instead.
    """
    plan = plan_profile_for_app(cfg, app)
    if plan.source == PROFILE_SOURCE_AUTO:
        # Only reachable through `app.auto_profile`, so `app` is not None here.
        return _provision_via_portal(cfg, app, password=password)
    return ResolvedProfile(path=plan.require_local())


def _provision_via_portal(
    cfg: Config,
    app: AppConfig,
    *,
    password: str | None,
) -> ResolvedProfile:
    asc_cfg = cfg.app_store_connect
    if password is None:
        raise SigningError(
            f"need .p12 password to resolve cert serial for {app.name}",
            hint=f"export {cfg.signing.cert_password_env}=<your .p12 password>",
        )
    cert_serial = apple_portal.read_p12_cert_serial(cfg.signing.cert, password)
    creds = apple_portal.PortalCredentials(
        key_id=asc_cfg.key_id,
        issuer_id=asc_cfg.issuer_id,
        key_path=asc_cfg.key_file,
    )
    client = apple_portal.ASCClient(creds)
    try:
        result = apple_portal.ensure_profile_for_bundle(
            client,
            bundle_id=app.bundle_id,
            cert_serial=cert_serial,
            device_udids=_auto_profile_device_udids(cfg, asc_cfg),
            cache_dir=asc_cfg.profile_cache_dir,
            exact=app.exact_app_id,
        )
    except apple_portal.PortalError as e:
        raise SigningError(
            f"auto-provisioning failed for {app.name}: {e.message}",
            hint=e.hint,
        ) from e
    # `force_refresh` is deliberately not passed. Whether a cached profile is
    # still good is decided by `ensure_profile_for_bundle` against the file
    # itself — a second opinion here is a second place for the two to disagree.
    return ResolvedProfile(
        path=result.profile_path,
        refresh_reason=result.refresh_reason,
        unregistered_devices=result.unregistered_devices,
    )


def run_zsign(
    cfg: Config,
    input_ipa: Path,
    output_ipa: Path,
    *,
    app: AppConfig | None = None,
    dry_run: bool = False,
) -> SignResult:
    pw = resolve_p12_password(cfg)
    if not pw:
        raise SigningError(
            f".p12 password not available (env {cfg.signing.cert_password_env} unset and not in Keychain)",
            hint=f"export {cfg.signing.cert_password_env}=<your .p12 password>  OR  graft --set-p12-password",
        )
    if not cfg.signing.cert.exists():
        raise SigningError(f"signing cert not found: {cfg.signing.cert}")

    # The dry-run branch returns BEFORE `resolve_profile_for_app` is reachable,
    # and that placement is the fix, not a detail. Resolving for real registers
    # a permanent Bundle ID in the user's Apple developer account when none
    # matches the app, burns a provisioning-profile slot and writes a
    # .mobileprovision into the cache — done by the one command whose entire
    # purpose is to touch nothing, with nothing in the output saying so.
    #
    # Planning keeps everything checkable without a network call: the same
    # precedence, and the same SigningError for a missing override file, a
    # missing wildcard, an auto_profile app with no app_store_connect section
    # or with no device UDIDs. What it gives up is exactly the round trip that
    # has side effects, and the rendered output names the profile either way.
    if dry_run:
        plan = plan_profile_for_app(cfg, app)
        cmd = _zsign_cmd(cfg, input_ipa, output_ipa, pw, plan.require_local())
        # Redact the password in the rendered command.
        redacted = list(cmd)
        redacted[redacted.index(pw)] = f"<{cfg.signing.cert_password_env}>"
        return SignResult(
            output_ipa=output_ipa,
            team_id=None,
            cert_subject=None,
            expiry=None,
            stdout=" ".join(redacted) + "\n" + _describe_plan(cfg, plan),
            stderr="",
        )

    resolved = resolve_profile_for_app(cfg, app, password=pw)
    cmd = _zsign_cmd(cfg, input_ipa, output_ipa, pw, resolved.path)

    output_ipa.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise SigningError(
            "zsign not found in PATH",
            hint="brew install zsign",
        ) from e

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not output_ipa.exists():
        raise SigningError(
            f"zsign exited with code {proc.returncode}",
            hint=_signing_hint(combined, cfg.signing.cert_password_env),
        )

    return SignResult(
        output_ipa=output_ipa,
        team_id=_first_match(_TEAM_RE, combined),
        cert_subject=_first_match(_SUBJECT_RE, combined),
        expiry=_first_match(_EXPIRY_RE, combined),
        stdout=proc.stdout,
        stderr=proc.stderr,
        profile_notes=_provisioning_notes(resolved),
    )


def _provisioning_notes(resolved: ResolvedProfile) -> tuple[str, ...]:
    """What the build should say out loud about how this profile was obtained.

    Phrased here rather than in `apple_portal` for the same reason
    `refresh_reason` always was: that module reports facts, and this one knows
    they are being read by a person mid-build.
    """
    notes: list[str] = []
    if resolved.refresh_reason:
        notes.append(
            "re-provisioned the signing profile via App Store Connect: "
            f"{resolved.refresh_reason}"
        )
    if resolved.unregistered_devices:
        # Not a failure. The build proceeds for the devices ASC does have, and
        # the profile simply does not cover this one — which is the right
        # outcome for a UDID left behind by a replaced phone, and something the
        # user has to be told if it was not.
        notes.append(
            "App Store Connect has no enabled device for "
            f"{', '.join(resolved.unregistered_devices)} — the signing profile "
            "does not cover it, and an install on that device will be refused. "
            "Add it at developer.apple.com -> Devices, or remove it from "
            "app_store_connect.device_udids in config.yaml."
        )
    return tuple(notes)


def _zsign_cmd(
    cfg: Config, input_ipa: Path, output_ipa: Path, password: str, profile: Path
) -> list[str]:
    return [
        "zsign",
        "-k", str(cfg.signing.cert),
        "-p", password,
        "-m", str(profile),
        "-o", str(output_ipa),
        str(input_ipa),
    ]


def _describe_plan(cfg: Config, plan: ProfilePlan) -> str:
    """The signing-identity line a dry run prints under the rendered command.

    Skipping profile resolution silently would hide a real misconfiguration, so
    a dry run still says which profile it picked and where that came from — and,
    for an auto-provisioned one, whether the file exists yet or Apple would have
    had to make it.

    "Exists" is not "will be reused". The real run replaces a cached profile
    that is expired, unreadable, or missing a device, so this asks the same
    offline question it does — `cache_problem`, against the same UDIDs — and
    says so. It once printed "a real run reuses this file" for a profile that
    did not list a newly connected iPad, which the real run then re-provisioned.
    """
    note = f"# signing profile: {plan.description} -> {plan.path}"
    if not plan.created_on_demand:
        return note
    if plan.path.exists():
        problem = mobileprovision.cache_problem(
            plan.path, device_udids=auto_profile_device_udids(cfg)
        )
        if problem is not None:
            return note + (
                f" (a real run would try to re-provision: {problem} "
                "— --dry-run does not contact Apple)"
            )
        return note + " (already cached; a real run reuses this file)"
    return note + (
        " (not cached yet; a real run would create it via App Store Connect "
        "— --dry-run does not contact Apple)"
    )


def _first_match(rx: re.Pattern[str], s: str) -> str | None:
    m = rx.search(s)
    return m.group(1).strip() if m else None


def _signing_hint(output: str, password_env: str) -> str:
    lo = output.lower()
    if "wrong password" in lo or "incorrect password" in lo or "mac verify failure" in lo:
        return (
            f"the .p12 password is wrong — check the value of ${password_env} "
            "or the one stored with `graft --set-p12-password`"
        )
    if "killed: 9" in lo or "code signature" in lo:
        return (
            "the zsign binary may be quarantined. Try: "
            "xattr -cr \"$(which zsign)\" and re-run."
        )
    if "no such file" in lo:
        return "one of the inputs is missing — verify cert/profile/IPA paths"
    return output.strip() or "re-run with --verbose for full output"
