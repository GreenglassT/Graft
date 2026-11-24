"""Wrap zsign for re-signing the tweak-injected IPA."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import apple_portal, install_hints, keychain, mobileprovision, p12, signing_cert, tools
from .child_env import BUNDLED_OPENSSL_CONF, child_env
from .config import AppConfig, Config
from .errors import SigningError


def _signing_for(cfg: Config, app: AppConfig | None) -> None:
    """Refuse to sign anything on a free Apple ID, naming what asked.

    The signing boundary's guard. The delivery checks
    (`Config.require_delivery`) refuse sideload and the paid-only verbs before
    they get here; this is the floor under them, so a path nobody thought of
    reads as the same refusal rather than an AttributeError on a `signing`
    that is None.

    Once per entry point: each public function below calls it, then does its
    work through the unguarded `_p12_password`, `_plan_profile` and
    `_resolve_profile`, so `run_zsign` asks once, not once per helper.
    """
    if app is not None and app.auto_profile:
        what = f"auto_profile: true (app '{app.name}')"
    elif app is not None:
        what = f"signing app '{app.name}'"
    else:
        what = "signing"
    cfg.require_paid_account(what)


def resolve_p12_password(cfg: Config) -> str | None:
    """Read the .p12 password from env first, then macOS Keychain."""
    _signing_for(cfg, None)
    return _p12_password(cfg)


def _p12_password(cfg: Config) -> str | None:
    """`resolve_p12_password` past its guard."""
    # By `cfg.team_id`, the key `--set-p12-password` writes under: never a
    # placeholder or a padded spelling of it.
    return p12_password_for(cfg.signing.cert_password_env, cfg.team_id)


def p12_password_for(env_name: str, team_id: str | None) -> str | None:
    """The .p12 password a build uses: `$<env_name>` if it is set and not
    empty, else the Keychain's `p12-<team_id>`, else None.

    "" is a password: a .p12 exported with a blank one is stored that way.
    The setup re-opens the .p12 it installed through this, so what it checks
    is what every build will use (`account_setup`).
    """
    pw = os.environ.get(env_name) if env_name else None
    if pw:
        return pw
    if team_id:
        return keychain.read_p12_password(team_id)
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


# zsign's team line, for a certificate with no OU: the subject and expiry
# are read from the certificate itself (`signing_cert`).
_TEAM_RE = re.compile(r"TeamId(?:entifier)?[:\s]+([A-Z0-9]+)", re.IGNORECASE)


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
    _signing_for(cfg, app)
    return _plan_profile(cfg, app)


def _plan_profile(cfg: Config, app: AppConfig | None) -> ProfilePlan:
    """`plan_profile_for_app` past its guard."""
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
    """`device_udids`, plus the default phone when it isn't one of them.

    `device_udids` is a snapshot, refreshed only by Save and test (or
    `--set-portal` / `--setup-portal`). The default phone counted only when
    the list was empty, so a phone bought and made the default after the last
    save was left out of every profile and failed pre-flight on every build.
    It is the phone builds go to, so it is always in.
    """
    udids = list(asc_cfg.device_udids)
    default = cfg.default_device_udid
    if default and default.lower() not in {u.lower() for u in udids}:
        udids.append(default)
    return udids


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
    _signing_for(cfg, app)
    return _resolve_profile(cfg, app, password=password)


def _resolve_profile(
    cfg: Config, app: AppConfig | None, *, password: str | None
) -> ResolvedProfile:
    """`resolve_profile_for_app` past its guard."""
    plan = _plan_profile(cfg, app)
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
            team_id=cfg.team_id,
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
    before_signing: Callable[[Path], None] | None = None,
) -> SignResult:
    """`before_signing` is handed the profile zsign will sign with, once it
    is known and before zsign runs: the planned one on a dry run (which may
    not exist yet, for an auto_profile app), the resolved one otherwise."""
    _signing_for(cfg, app)
    pw = _p12_password(cfg)
    if pw is None:
        raise SigningError(
            f".p12 password not available (env {cfg.signing.cert_password_env} unset and not in Keychain)",
            hint=f"export {cfg.signing.cert_password_env}=<your .p12 password>  OR  graft --set-p12-password",
        )
    if not cfg.signing.cert.exists():
        raise SigningError(f"signing cert not found: {cfg.signing.cert}")
    z = tools.resolve("zsign")

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
        plan = _plan_profile(cfg, app)
        # Rendered, not made: a dry run writes no copy of the identity.
        cmd = _zsign_cmd(
            str(z.path or "zsign"), input_ipa, output_ipa,
            f"<a private copy of {cfg.signing.cert}>", "<its one-time password>",
            plan.require_local(),
        )
        if before_signing is not None:
            before_signing(plan.path)
        return SignResult(
            output_ipa=output_ipa,
            team_id=None,
            cert_subject=None,
            expiry=None,
            stdout=" ".join(cmd) + "\n" + _describe_plan(cfg, plan),
            stderr="",
        )

    # Before the profile is resolved: that can register a Bundle ID and spend
    # a profile slot in the user's developer account, all for a build that
    # could never have been signed.
    if not z.found:
        raise SigningError("zsign not found in PATH", hint=_zsign_install_hint())
    zsign = str(z.path)

    resolved = _resolve_profile(cfg, app, password=pw)
    if before_signing is not None:
        before_signing(resolved.path)
    output_ipa.parent.mkdir(parents=True, exist_ok=True)

    # Never `-p <the user's password>`: a command line is readable by every
    # user on the Mac (`ps -axww`) for as long as zsign runs. zsign gets a
    # 0600 copy of the identity, re-encrypted under a one-time password, in a
    # 0700 directory that is gone however this block is left. SIGTERM becomes
    # SystemExit while it exists: the GUI's Stop is a SIGTERM, and its default
    # disposition would kill the process past the `finally` that removes it.
    from .lc_manage import exit_on_termination

    with exit_on_termination(), _private_identity(cfg, pw) as (identity, one_time):
        cmd = _zsign_cmd(zsign, input_ipa, output_ipa, str(identity), one_time, resolved.path)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=child_env(zsign_env(z)))
        except FileNotFoundError as e:  # gone since it was resolved
            raise SigningError("zsign not found in PATH", hint=_zsign_install_hint()) from e

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not output_ipa.exists():
        raise SigningError(
            f"zsign exited with code {proc.returncode}",
            hint=_signing_hint(
                combined, cfg.signing.cert_password_env, proc.returncode,
                # The binary `subprocess` ran: the one `tools.resolve` found.
                zsign=zsign,
            ),
        )

    cert = signing_cert.read(cfg.signing.cert, pw)
    return SignResult(
        output_ipa=output_ipa,
        team_id=cert.team_id or _first_match(_TEAM_RE, combined),
        cert_subject=cert.subject,
        expiry=cert.expiry,
        stdout=proc.stdout,
        stderr=proc.stderr,
        profile_notes=_provisioning_notes(resolved),
    )


def zsign_env(resolved: tools.Resolved) -> dict[str, str]:
    """What zsign's process needs on top of `child_env()`.

    Graft.app's zsign is repointed at the OpenSSL bundled in
    `Contents/Frameworks`, whose loadable providers (`legacy.dylib`) sit in
    `Contents/Frameworks/ossl-modules`. OpenSSL looks for them where it was
    configured to install them, a directory on the release machine, unless
    `OPENSSL_MODULES` says otherwise.

    That OpenSSL would also read the openssl.cnf in the directory it was
    configured with, so it gets none (`child_env.BUNDLED_OPENSSL_CONF`).

    Only for the bundled zsign, and the modules only when that folder is
    there: a zsign from PATH or config.yaml brings its own OpenSSL and keeps
    whatever the user's environment says.
    """
    if resolved.source != tools.SOURCE_BUNDLED or resolved.path is None:
        return {}
    # `<Contents>/Helpers/bin/zsign`: the bundled rung builds it that way.
    modules = resolved.path.parent.parent.parent / "Frameworks" / "ossl-modules"
    return {**BUNDLED_OPENSSL_CONF, **({"OPENSSL_MODULES": str(modules)} if modules.is_dir() else {})}


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


@contextlib.contextmanager
def _private_identity(cfg: Config, password: str) -> Iterator[tuple[Path, str]]:
    """`p12.private_identity` for the configured cert, with a wrong password
    named the way every other signing hint names it.

    Reading the .p12 in-process is also where a wrong password now shows up —
    before zsign runs rather than as zsign's "mac verify failure".
    """
    try:
        with p12.private_identity(cfg.signing.cert, password, prefix="graft-zsign-") as made:
            yield made
    except p12.UnreadableP12Error as e:
        raise SigningError(
            f"could not open {cfg.signing.cert} with the .p12 password",
            hint=_wrong_password_hint(cfg.signing.cert_password_env)
            + " (or the .p12 was exported in a format that cannot be read)",
        ) from e


def _zsign_cmd(
    zsign: str, input_ipa: Path, output_ipa: Path, identity: str, password: str, profile: Path
) -> list[str]:
    """`zsign` is the binary to run (`tools.resolve`). `identity` and
    `password` are the private copy's, never the user's."""
    return [
        zsign,
        "-k", identity,
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
    that is expired, unreadable, missing a device or from another team, so
    this asks the same offline question pre-flight does — `cache_problem`,
    against the same UDIDs and signing.team_id — and says so. It once printed
    "a real run reuses this file" for a profile that did not list a newly
    connected iPad, and later for one from the team the user had just left,
    both of which the real run re-provisioned.

    The certificate is the one question stage 4 asks that this can't: it
    needs the .p12 opened, which a dry run does not do. So "reuses" comes with
    that condition said out loud.
    """
    note = f"# signing profile: {plan.description} -> {plan.path}"
    if not plan.created_on_demand:
        return note
    if plan.path.exists():
        problem = mobileprovision.cache_problem(
            plan.path, device_udids=auto_profile_device_udids(cfg), team_id=cfg.team_id
        )
        if problem is not None:
            return note + (
                f" (a real run would try to re-provision: {problem} "
                "— --dry-run does not contact Apple)"
            )
        return note + (
            " (already cached; a real run reuses this file if it lists the .p12's "
            "certificate, which --dry-run does not open)"
        )
    return note + (
        " (not cached yet; a real run would create it via App Store Connect "
        "— --dry-run does not contact Apple)"
    )


def _first_match(rx: re.Pattern[str], s: str) -> str | None:
    m = rx.search(s)
    return m.group(1).strip() if m else None


def _wrong_password_hint(password_env: str) -> str:
    return (
        f"the .p12 password is wrong — check the value of ${password_env} "
        "or the one stored with `graft --set-p12-password`"
    )


def _quarantined(path: str | None) -> bool:
    """Whether `path` carries macOS's `com.apple.quarantine` attribute.

    `xattr -p` exits 0 only when the attribute is there. Anything that stops
    the question being answered — no such file, no `xattr` — is "no": the hint
    this decides is advice to strip that attribute, and it is only worth giving
    when there is one.
    """
    if not path:
        return False
    try:
        proc = subprocess.run(
            ["/usr/bin/xattr", "-p", "com.apple.quarantine", path],
            capture_output=True, text=True, timeout=10, env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _zsign_install_hint() -> str:
    return install_hints.with_prerequisites("brew install zsign")


def _quarantine_hint(zsign: str) -> str:
    return (
        f"{zsign} is quarantined, so macOS kills it before it runs. Clear it with: "
        f"xattr -d com.apple.quarantine {shlex.quote(zsign)} and re-run."
    )


def _signing_hint(
    output: str, password_env: str, returncode: int = 0, *, zsign: str | None = None
) -> str:
    lo = output.lower()
    killed = returncode == -9 or "killed: 9" in lo or "code signature" in lo
    # A quarantined binary is killed by the OS before it can print anything: the
    # subprocess reports -9 (SIGKILL) with empty output, so the text matches
    # below alone never fire for it. Said only when the binary is asked and
    # says so: a SIGKILL is also memory pressure, a crash, or a `kill -9`.
    if killed and _quarantined(zsign):
        return _quarantine_hint(zsign)
    if returncode == -9:
        return (
            "zsign was killed (SIGKILL) before it finished. It is not quarantined, so "
            "this is not Gatekeeper: re-run, and if it happens again check Console "
            "for a crash report or memory pressure."
        )
    if "wrong password" in lo or "incorrect password" in lo or "mac verify failure" in lo:
        return _wrong_password_hint(password_env)
    if "no such file" in lo:
        return "one of the inputs is missing — verify cert/profile/IPA paths"
    return output.strip() or "re-run with --verbose for full output"
