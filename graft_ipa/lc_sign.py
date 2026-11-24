"""Sign LiveContainer the way AltStore does, so guest apps can keep a login.

**Why this is not zsign.** zsign copies a wildcard profile's entitlements into
the signature verbatim: `keychain-access-groups = ["TEAM.*"]` and
`application-identifier = "TEAM.com.kdt.*"`. In a provisioning profile those
are *patterns* — what the profile permits. In a signature they are *literal
strings* — what the binary claims. LiveContainer's keychain redirection
(`LiveContainer/Tweaks/SecItem.m`) probes `TEAM.com.kdt.livecontainer.shared`
(and `.1` … `.127`), gets errSecMissingEntitlement because no literal group of
that name was signed in, logs `[LC] failed to access keychain access group`,
and switches itself off. From then on a guest app's own keychain writes fail,
and sign-in is "not remembered" on the next launch. Root-caused with
device logs on 2026-11-17; LiveContainer's JIT-Less Diagnose shows it as
"keychain-access-groups correct: No".

**What this does instead** (`build_entitlements`): start from the entitlements
LiveContainer's own binaries *request* — embedded at build time with the team
placeholder `AAAAA11111` — put the real team in, and keep only what the profile
grants, matching each requested value against the profile's patterns with
`fnmatch`. The profile's `TEAM.*` admits the 128 explicit
`TEAM.com.kdt.livecontainer.shared[.N]` groups, so those are what get signed.
App groups are mapped to the team-suffixed names AltStore/SideStore registered
them under. Then each bundle is signed inside-out with Apple's `codesign`, so
every extension carries its own profile and its own entitlements.

The signing identity is the config's `.p12`, imported into a throwaway
keychain (`TempKeychain`) so `codesign` never raises a prompt, and whose
addition to the user's keychain search list is reverted exactly afterward —
on failure too. Every subprocess goes through `Runner`, which is the seam the
tests replace: nothing under test touches a real keychain.

Ported from `scripts/lc_resign.py`, which was hand-run and verified on a real
iPad on iPadOS 27.0 before this module existed.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import lc_instance, mobileprovision
from . import p12 as p12_mod
from .child_env import child_env
from .errors import SigningError

#: The team id LiveContainer's build embeds in its requested entitlements, to
#: be replaced by the signer's own. AltStore and SideStore do the same swap.
PLACEHOLDER_TEAM = "AAAAA11111"

#: App groups LiveContainer asks for by their bare name, and which AltStore /
#: SideStore register in the account with `.<TEAM>` appended. The SideStore one
#: is the first LiveContainer's `appGroupID` tries, which is why all three
#: instances end up sharing it.
APP_GROUP_BASES = ("group.com.SideStore.SideStore", "group.com.rileytestut.AltStore")

APP_GROUPS_KEY = "com.apple.security.application-groups"
KEYCHAIN_GROUPS_KEY = "keychain-access-groups"


def replace_team(value: Any, team: str) -> Any:
    """`value` with every `AAAAA11111` replaced by `team`, at any depth."""
    if isinstance(value, str):
        return value.replace(PLACEHOLDER_TEAM, team)
    if isinstance(value, list):
        return [replace_team(v, team) for v in value]
    if isinstance(value, dict):
        return {k: replace_team(v, team) for k, v in value.items()}
    return value


def build_entitlements(
    requested: dict[str, Any],
    profile_ents: dict[str, Any],
    *,
    team: str,
    bundle_id: str,
    verbatim_app_id: bool = False,
    app_id_prefix: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """The entitlements to sign `bundle_id` with, and what was left out.

    Every value returned is one the profile permits; every value requested and
    not returned is named in the notes, so a dropped capability is visible
    rather than discovered on the device.

    `app_id_prefix` is the profile's App ID prefix
    (`mobileprovision.app_id_prefix`); the application-identifier is
    `<it>.<bundle_id>`. None means the team id, which is the prefix on every
    account made since 2011 — but not on older ones, whose profiles would
    otherwise never cover the identifier built here.

    `verbatim_app_id` keeps the profile's literal `application-identifier`
    instead of the exact one. Its only use is upgrading, in place, a copy that
    zsign signed earlier: iOS refuses an upgrade whose application-identifier
    differs from the installed one (`MismatchedApplicationIdentifierEntitlement`),
    and zsign wrote the profile's wildcard there.
    """
    notes: list[str] = []
    prefix = app_id_prefix or team
    req = replace_team(requested, team)
    # Keychain access groups are scoped by App ID prefix, not by team: the
    # profile grants `<prefix>.*`. Filled with the team on an account whose
    # prefix differs, all 128 fall outside that grant and are dropped — and
    # with them LiveContainer's keychain hook, which is the login failure this
    # module exists to fix. App groups and the team-identifier stay the team's.
    if KEYCHAIN_GROUPS_KEY in requested:
        req[KEYCHAIN_GROUPS_KEY] = replace_team(requested[KEYCHAIN_GROUPS_KEY], prefix)
    ents: dict[str, Any] = {}

    app_id = f"{prefix}.{bundle_id}"
    allowed_app_id = profile_ents.get("application-identifier", "")
    if not fnmatch.fnmatchcase(app_id, allowed_app_id):
        raise SigningError(
            f"profile application-identifier {allowed_app_id!r} does not cover {app_id!r}",
            hint="the profile was made for a different App ID; re-provision it",
        )
    ents["application-identifier"] = allowed_app_id if verbatim_app_id else app_id
    ents["com.apple.developer.team-identifier"] = team
    if "get-task-allow" in profile_ents:
        ents["get-task-allow"] = profile_ents["get-task-allow"]

    for key, value in req.items():
        if key in ents:
            continue
        if key not in profile_ents:
            notes.append(f"dropped {key} (profile does not grant it)")
            continue
        allowed = profile_ents[key]
        if key == APP_GROUPS_KEY:
            mapped = [f"{g}.{team}" if g in APP_GROUP_BASES else g for g in value]
            kept = [g for g in mapped if g in allowed]
            if not kept:
                notes.append(f"dropped {key} (none of {mapped} in profile {allowed})")
                continue
            ents[key] = kept
        elif isinstance(value, list) and isinstance(allowed, list):
            # The profile lists patterns; the signature must list literals.
            # Each requested literal is kept iff some pattern admits it.
            kept = [v for v in value if any(fnmatch.fnmatchcase(v, pat) for pat in allowed)]
            if len(kept) != len(value):
                notes.append(f"{key}: kept {len(kept)}/{len(value)}")
            if kept:
                ents[key] = kept
        elif value == allowed or allowed is True:
            ents[key] = value
        else:
            notes.append(f"dropped {key}={value!r} (profile allows {allowed!r})")
    return ents, notes


# ---------- the subprocess seam ----------


class Runner:
    """Every external command this feature runs, in one replaceable place.

    `codesign` and `security` both act on things a test must never touch — the
    user's keychain search list, a real signing identity — so nothing in
    `lc_sign` or `lc_manage` calls `subprocess` directly. (Installing on the
    phone is in-process now, through `device_install`.)
    Tests pass a fake that records argv and answers from fixtures.
    """

    def run(
        self, cmd: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=child_env())
        except FileNotFoundError as e:
            raise SigningError(f"{cmd[0]} not found in PATH") from e
        if check and proc.returncode != 0:
            raise SigningError(
                f"{' '.join(cmd[:2])} exited with code {proc.returncode}",
                hint=(proc.stderr or proc.stdout).strip() or None,
            )
        return proc


# ---------- identity and keychain ----------


def p12_identity(p12: Path, password: str) -> str:
    """The SHA-1 of the certificate in `p12`, which is how `codesign -s` names it.

    By hash rather than by common name: an AltStore-made certificate on the
    same account can share the name "Apple Development: …", and `codesign`
    refuses an ambiguous name.
    """
    from cryptography.hazmat.primitives import hashes

    try:
        _, cert, _ = p12_mod.load_key_and_certificates(p12.read_bytes(), password.encode())
    except ValueError as e:
        raise SigningError(
            f"could not read {p12}: {e}",
            hint="wrong .p12 password, or an unsupported export format",
        ) from e
    if cert is None:
        raise SigningError(f"{p12} contains no certificate")
    return cert.fingerprint(hashes.SHA1()).hex().upper()  # an identifier, not a security boundary


def _search_list(runner: Runner) -> list[str]:
    out = runner.run(["security", "list-keychains", "-d", "user"]).stdout
    return [line.strip().strip('"') for line in out.splitlines() if line.strip()]


#: The file name every `temp_keychain` uses: `lcsign-<pid>-<hex>.keychain-db`.
#: The PID is what tells a leftover from a keychain another run is using
#: right now (see `_is_stale`). Group 2 is the PID; absent in the old format.
_OURS = re.compile(r"(^|/)lcsign-(?:(\d+)-)?[^/]*\.keychain-db$")


def _is_stale(entry: str) -> bool:
    """A search-list entry of ours that no running process owns.

    Stale when its file is gone, when the PID in its name is dead, or when the
    name predates the PID (nothing can be owning one of those any more). A
    *live* PID with its file present is another run mid-signing — a terminal
    `--lc-install`, or `scripts/lc_resign.py` overlapping the GUI — and
    sweeping it would pull the identity out from under its `codesign`.
    """
    match = _OURS.search(entry)
    if match is None:
        return False
    if not Path(entry).exists() or match.group(2) is None:
        return True
    try:
        os.kill(int(match.group(2)), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists, owned by someone else: alive
    return False


def _same_path(a: str, b: str) -> bool:
    # `security` prints the resolved path (/private/var/… for /var/…), so
    # compare resolved forms, not strings.
    return os.path.realpath(a) == os.path.realpath(b)


def _set_search_list(runner: Runner, paths: list[str]) -> None:
    runner.run(["security", "list-keychains", "-d", "user", "-s", *paths])


@contextlib.contextmanager
def temp_keychain(runner: Runner, p12: Path, password: str, workdir: Path) -> Iterator[Path]:
    """A throwaway keychain holding the `.p12`'s identity, for `codesign`.

    Why not the login keychain: importing there is permanent, and `codesign`
    using a key from it raises an "allow access" prompt per binary — a GUI-run
    install would hang on a dialog nobody is looking at. A fresh keychain with
    a random password and `set-key-partition-list` for `codesign:` never asks.

    `codesign` only builds the certificate chain from keychains on the user's
    search list, so this one is appended to it for the duration, behind the
    user's own. Three rules keep that from outliving the run:

    * **On the way out, remove only ours.** The list is re-read and written
      back without this keychain — never restored from a snapshot taken on the
      way in. A snapshot would undo anything else that changed the list
      meanwhile, including a concurrent run removing *its* keychain, which the
      snapshot would then put back.
    * **On the way in, heal what a killed run left.** A SIGKILL, or a SIGTERM
      before the CLI's handlers were installed, skips every `finally`, leaving
      an unlocked `lcsign-<pid>-*.keychain-db` on the list. An entry whose
      process is dead or whose file is gone is dropped (its file deleted if it
      still exists) before ours goes on. One whose process is still running is
      another run's live keychain and is left exactly where it is.
    * The `lc` verbs turn SIGTERM/SIGHUP into `SystemExit` (see
      `lc_manage.exit_on_termination`), so a GUI Stop runs this `finally`.
    """
    current = _search_list(runner)
    stale = [p for p in current if _is_stale(p)]
    if stale:
        for old in stale:
            if Path(old).exists():
                runner.run(["security", "delete-keychain", old], check=False)
        _set_search_list(runner, [p for p in current if p not in stale])
    path = workdir / f"lcsign-{os.getpid()}-{uuid.uuid4().hex[:8]}.keychain-db"
    secret = uuid.uuid4().hex
    created = False
    try:
        runner.run(["security", "create-keychain", "-p", secret, str(path)])
        created = True
        runner.run(["security", "set-keychain-settings", "-lut", "3600", str(path)])
        runner.run(["security", "unlock-keychain", "-p", secret, str(path)])
        # Never `-P <the user's password>`: argv is readable by every user on
        # the machine via `ps` for as long as the import runs. The identity is
        # re-encrypted in-process under a throwaway password, written 0600,
        # and removed the moment the import returns — on failure too.
        throwaway = secrets.token_hex(16)
        copy = workdir / f"lcsign-{uuid.uuid4().hex[:8]}.p12"
        try:
            p12_mod.write_reencrypted(p12, password, copy, throwaway)
            runner.run([
                "security", "import", str(copy), "-k", str(path), "-P", throwaway,
                "-T", "/usr/bin/codesign",
            ])
        finally:
            copy.unlink(missing_ok=True)
        runner.run([
            "security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
            "-s", "-k", secret, str(path),
        ])
        _set_search_list(runner, [*_search_list(runner), str(path)])
        yield path
    finally:
        try:
            now = _search_list(runner)
            if any(_same_path(p, str(path)) for p in now):
                _set_search_list(runner, [p for p in now if not _same_path(p, str(path))])
        finally:
            if created:
                runner.run(["security", "delete-keychain", str(path)], check=False)


# ---------- signing ----------


@dataclass(frozen=True)
class BundleReport:
    """What one bundle was signed with — the line a person checks first."""

    name: str
    bundle_id: str
    profile_name: str
    application_identifier: str
    keychain_groups: int
    app_groups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"{self.name}: {self.application_identifier} profile={self.profile_name!r} "
            f"keychain-groups={self.keychain_groups} app-groups={self.app_groups}"
        )
        return line + (f" | {'; '.join(self.notes)}" if self.notes else "")


def requested_entitlements(binary: Path, runner: Runner) -> dict[str, Any]:
    """The entitlements `binary` was built to request. Empty if it has none."""
    out = runner.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(binary)], check=False
    )
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    return plistlib.loads(out.stdout.encode())


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def _codesign(runner: Runner, path: Path, identity: str, keychain: Path,
              entitlements: Path | None = None) -> None:
    # `--generate-entitlement-der`: iOS 15+ reads the DER form, and a signature
    # without it launches with no entitlements at all. `--timestamp=none`: a
    # development signature needs no timestamp, and asking Apple's server for
    # one per binary is a network dependency for nothing.
    cmd = ["codesign", "-f", "-s", identity, "--keychain", str(keychain),
           "--timestamp=none", "--generate-entitlement-der"]
    if entitlements is not None:
        cmd += ["--entitlements", str(entitlements)]
    cmd.append(str(path))
    runner.run(cmd)


def _executable(bundle: Path) -> Path:
    # Through `bundle_file`, never `bundle / info[...]`: the plist came out of
    # a downloaded IPA, and this path is handed to `codesign -f`, which
    # rewrites whatever it names.
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    return lc_instance.bundle_file(bundle, info)


def _refuse_untrusted_layout(app: Path, bundles: list[Path]) -> None:
    """Stop before the first signature if the tree could aim one outside it.

    `codesign -f` follows a symlink and re-signs its target in place, so a
    link anywhere in the app is a write outside the extraction directory. iOS
    bundles have none and `zipfile` never creates one, so any found here is
    refused rather than skipped. Every bundle's executable name is validated
    up front for the same reason: half-signed is the worst place to stop.
    """
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            raise lc_instance.LiveContainerError(
                f"{app.name} contains a symlink: {path.relative_to(app)}"
            )
    for bundle in bundles:
        _executable(bundle)


def _sign_loose(runner: Runner, bundle: Path, identity: str, keychain: Path) -> None:
    """Every Mach-O in `bundle` that is not its main executable, e.g. a dylib."""
    exe = _executable(bundle)
    for f in sorted(bundle.rglob("*")):
        if f.is_file() and f != exe and "_CodeSignature" not in f.parts and _is_macho(f):
            _codesign(runner, f, identity, keychain)


def _sign_with_profile(
    runner: Runner, bundle: Path, *, profile: Path, team: str, identity: str,
    keychain: Path, workdir: Path, load_profile: Callable[[Path], dict],
    verbatim_app_id: bool,
) -> BundleReport:
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    bundle_id = info["CFBundleIdentifier"]
    shutil.copyfile(profile, bundle / "embedded.mobileprovision")
    prof = load_profile(profile)
    ents, notes = build_entitlements(
        requested_entitlements(_executable(bundle), runner), prof["Entitlements"],
        team=team, bundle_id=bundle_id, verbatim_app_id=verbatim_app_id,
        app_id_prefix=mobileprovision.app_id_prefix(prof, fallback=team),
    )
    ent_file = workdir / f"{bundle.name}.entitlements.plist"
    ent_file.write_bytes(plistlib.dumps(ents))
    _codesign(runner, bundle, identity, keychain, ent_file)
    return BundleReport(
        name=bundle.name,
        bundle_id=bundle_id,
        profile_name=str(prof.get("Name", profile.name)),
        application_identifier=ents["application-identifier"],
        keychain_groups=len(ents.get("keychain-access-groups", [])),
        app_groups=list(ents.get(APP_GROUPS_KEY, [])),
        notes=notes,
    )


def sign_app(
    app: Path,
    *,
    profile_for: Callable[[str], Path],
    identity: str,
    keychain: Path,
    runner: Runner,
    workdir: Path,
    load_profile: Callable[[Path], dict] = mobileprovision.decode,
    verbatim_app_id: bool = False,
) -> list[BundleReport]:
    """Sign an extracted `.app` inside-out, each bundle with its own profile.

    Order is what makes the outer seals valid: frameworks and loose dylibs
    first (no profile, no entitlements — they inherit the process's), then each
    extension with the profile `profile_for` returns for *its* bundle id, then
    the app itself. `codesign --deep` is deliberately not used to sign: it
    would give every nested bundle the app's entitlements. It is used to
    *verify*, as the last step.
    """
    frameworks = sorted(app.glob("Frameworks/*.framework")) + sorted(
        app.glob("PlugIns/*.appex/Frameworks/*.framework")
    )
    appexes = sorted(app.glob("PlugIns/*.appex"))
    _refuse_untrusted_layout(app, [*frameworks, *appexes, app])

    main_id = plistlib.loads((app / "Info.plist").read_bytes())["CFBundleIdentifier"]
    main_profile = profile_for(main_id)
    team = load_profile(main_profile)["TeamIdentifier"][0]
    reports: list[BundleReport] = []

    for fw in frameworks:
        _sign_loose(runner, fw, identity, keychain)
        _codesign(runner, fw, identity, keychain)
    for dylib in sorted(app.glob("Frameworks/*.dylib")):
        _codesign(runner, dylib, identity, keychain)
    for appex in appexes:
        _sign_loose(runner, appex, identity, keychain)
        appex_id = plistlib.loads((appex / "Info.plist").read_bytes())["CFBundleIdentifier"]
        reports.append(_sign_with_profile(
            runner, appex, profile=profile_for(appex_id), team=team, identity=identity,
            keychain=keychain, workdir=workdir, load_profile=load_profile,
            verbatim_app_id=verbatim_app_id,
        ))
    reports.append(_sign_with_profile(
        runner, app, profile=main_profile, team=team, identity=identity,
        keychain=keychain, workdir=workdir, load_profile=load_profile,
        verbatim_app_id=verbatim_app_id,
    ))
    runner.run(["codesign", "--verify", "--deep", "--strict", str(app)])
    return reports


def sign_ipa(
    ipa_in: Path,
    ipa_out: Path,
    *,
    profile_for: Callable[[str], Path],
    p12: Path,
    p12_password: str,
    runner: Runner | None = None,
    load_profile: Callable[[Path], dict] = mobileprovision.decode,
    verbatim_app_id: bool = False,
) -> list[BundleReport]:
    """`sign_app` for a whole `.ipa`: extract, sign, re-zip into `ipa_out`."""
    runner = runner or Runner()
    identity = p12_identity(p12, p12_password)
    with tempfile.TemporaryDirectory(prefix="lcsign-") as td:
        work = Path(td)
        app = lc_instance.extract_app(ipa_in, work / "x")
        with temp_keychain(runner, p12, p12_password, work) as keychain:
            reports = sign_app(
                app, profile_for=profile_for, identity=identity, keychain=keychain,
                runner=runner, workdir=work, load_profile=load_profile,
                verbatim_app_id=verbatim_app_id,
            )
        lc_instance.archive_payload(work / "x", ipa_out)
    return reports
