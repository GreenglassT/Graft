"""`graft --init` without its questions: the setup the Mac app runs.

`--init --account free` and `--init --account paid --p12 P --profile R` (the
.p12 password on stdin, one line) do what the interactive `--init`
(`init_flow`) does for the same answers, with every answer given up front:

* free, no config: the free starter, with no `signing:` section;
* free on a paid config: the `signing:` section removed, and its apps, repos
  and settings kept (`remove_signing_section`, as `init_flow._switch_to_free`);
* paid, no config: the paid starter;
* paid on a free config: a `signing:` section added, apps kept, and each app
  with no `delivery:` key pinned to LiveContainer (`add_signing_section`, as
  `init_flow._upgrade_to_paid`).
* paid on a paid config, with `replace`: the certificate, the profile, the
  Keychain password and the `signing:` section swapped for the new pair, and
  everything else kept. A development certificate and profile expire every
  year, and this is how the app renews them.

Two things it does that the interactive flow leaves to the user: it stores
the password in the Keychain (`--set-p12-password`'s entry, `p12-<team>`),
and it checks the .p12 against the profile before anything is written, so a
certificate the profile can't sign with is refused here rather than by the
phone after a build.

What it never does is overwrite a config. A paid config asked to become paid
without `replace`, or a config that doesn't parse, is refused with nothing
changed: the interactive flow asks "overwrite?" there, and nobody is here to
ask.

Nothing is written until both files have been read and checked. A paid
setup's writes are then one undoable change (`_Change`): the new copies are
staged beside the old ones, the Keychain item is stored, the copies are
swapped in with the old ones kept aside, config.yaml is written, and the
installed .p12 is opened again with the password a build will use. A failure
at any of those, a Ctrl-C, a Stop (SIGTERM) or a reader that hung up (a
BrokenPipe) puts every piece back, by rename, so the undo needs no free
space. `dry_run` stops before the first write.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import signal
import stat
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import keychain, mobileprovision
from . import p12 as p12_mod
from .config import (
    ACCOUNT_FREE,
    ACCOUNT_PAID,
    APP_ACCOUNT_SETTINGS,
    FREE_DEVICE_STEPS,
    TEAM_ID_PLACEHOLDER,
    account_of,
    add_signing_section,
    default_cache_dir,
    default_tweaks_dir,
    read_settings,
    remove_signing_section,
    replace_signing,
    write_default_config,
)
from .data_dirs import private_stage
from .errors import (
    NOT_LET_READ,
    ConfigError,
    GraftError,
    PreflightError,
    Stopped,
    not_let_read_hint,
)

#: A .p12 is a few KiB and a development profile tens of KiB. Anything larger
#: is the wrong file (an IPA, a disk image), refused before it is read whole.
MAX_INPUT_BYTES = 1 << 20


def read_chosen_file(path: Path, what: str) -> bytes:
    """The bytes of a file the user chose, read once.

    A file picked in Downloads or a cloud-synced folder is often reached
    through a symbolic link, so the path is resolved and the real file read.
    That one is opened with `O_NOFOLLOW` (a link there now is one planted
    since it was resolved, or a loop: refused) and `O_NONBLOCK`, so a FIFO is
    refused rather than waited on forever (`apple_portal.read_key_file`'s
    lesson). It is checked with `fstat` on the descriptor it is then read
    from, so the check can't be raced, and read with a size cap. Nothing
    reads the path again: what is checked is what is installed.

    Only a source may be reached through a link. Every write into the config
    directory replaces a link at its destination rather than following it
    (`data_dirs.private_write`).
    """
    real = os.path.realpath(path)
    # Every refusal names the path without its control characters: a file's
    # name is whoever made it's, and Terminal runs an ESC in it (combined fix
    # check #16).
    shown = printable(str(path))
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {shown}") from None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ConfigError(
                f"{shown} is a symbolic link that doesn't lead to a file",
                hint=f"choose the {what} itself",
            ) from None
        raise ConfigError(
            f"can't open {shown}: {e.strerror or e}",
            hint=not_let_read_hint(shown) if e.errno in NOT_LET_READ else None,
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigError(f"{shown} is not a file", hint=f"choose the {what} itself")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_INPUT_BYTES:
            chunk = os.read(fd, MAX_INPUT_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_INPUT_BYTES:
        raise ConfigError(
            f"{shown} is larger than 1 MiB, so it isn't a {what}", hint=f"choose the {what} itself"
        )
    return b"".join(chunks)


@dataclass(frozen=True)
class SigningIdentity:
    """A .p12 and a profile that belong together, read and checked."""

    team_id: str
    #: The profile's `Name`, as developer.apple.com shows it.
    profile_name: str | None
    #: The UDIDs the profile lists.
    devices: tuple[str, ...]
    expires: datetime | None
    #: The .p12 certificate's common name ("Apple Development: ...").
    certificate: str | None
    #: Whether the profile lists that certificate. False when it lists none,
    #: which is not judged (`mobileprovision.developer_certificate_serials`).
    certificate_listed: bool
    # The bytes checked, which are the bytes installed. Out of the repr: a
    # traceback's locals or a log line must never carry the private key.
    p12_bytes: bytes = field(repr=False)
    profile_bytes: bytes = field(repr=False)


def printable(text: str) -> str:
    """`text` without control or format characters: an ESC, a BEL, a
    bidirectional override. A certificate's name and a file's name come from
    whoever made the file, and the setup prints them to a terminal."""
    return "".join(c for c in text if c.isprintable())


def _common_name(cert: Any) -> str | None:
    from cryptography.x509.oid import NameOID

    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return printable(str(names[0].value)) if names else None


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable() and value.strip() == value


def _check_profile_fields(plist: dict, name: str) -> None:
    """Refuse a profile whose fields aren't the types Apple writes, before
    anything is read out of them: each reaches config.yaml, the Keychain
    account, the `init-result` event or a check, and a wrong type there
    crashed the run, once after everything had been written (F7, F5)."""

    def bad(key: str, what: str) -> ConfigError:
        return ConfigError(
            f"{name}'s {key} isn't {what}",
            hint="download the profile again from developer.apple.com",
        )

    for key in ("TeamIdentifier", "ApplicationIdentifierPrefix"):
        value = plist.get(key)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise bad(key, "a list of team IDs")
    devices = plist.get("ProvisionedDevices")
    if devices is not None and not (isinstance(devices, list) and all(map(_is_text, devices))):
        raise bad("ProvisionedDevices", "a list of device IDs")
    certificates = plist.get("DeveloperCertificates")
    if certificates is not None and not (
        isinstance(certificates, list) and all(isinstance(c, bytes) for c in certificates)
    ):
        raise bad("DeveloperCertificates", "a list of certificates")
    if plist.get("Name") is not None and not isinstance(plist["Name"], str):
        raise bad("Name", "text")
    if plist.get("ExpirationDate") is not None and not isinstance(plist["ExpirationDate"], datetime):
        raise bad("ExpirationDate", "a date")


def _open_p12(
    p12_bytes: bytes, password: str, name: str, *, time_boxed: bool = True
) -> tuple[Any, Any]:
    """The key and certificate, after their cost has been bounded twice, so a
    crafted file can't stall the run:

    * the key-derivation counts that can be read before decrypting
      (`p12.check_kdf_cost`), a fast refusal of an absurd one;
    * and, for any layout, the open itself, in a child process killed after
      `p12.PROBE_SECONDS` (`p12.probe`). A key bag inside an encrypted
      SafeContents hides its count until it is decrypted, so only a clock
      can bound it. This process opens the file only once the child has,
      within the time.

    `time_boxed=False` is for bytes already opened that way (`_reopen`)."""
    try:
        p12_mod.check_kdf_cost(p12_bytes)
    except p12_mod.CostlyP12Error as e:
        raise ConfigError(
            f"{name} asks for {e.iterations:,} rounds of key derivation to open, more than "
            f"the {p12_mod.MAX_KDF_ITERATIONS:,} Graft allows",
            hint="an export from Keychain Access asks for a few thousand; export the "
            "certificate again from Keychain Access",
        ) from None
    except p12_mod.P12StructureError:
        raise ConfigError(
            f"{name} isn't a .p12 Graft can read",
            hint="in Keychain Access, export the certificate from My Certificates, with "
            "its key, as a .p12",
        ) from None
    wrong = ConfigError(
        f"couldn't open {name} with that password" if password
        else f"{name} doesn't open without a password",
        hint="use the password you set when you exported it from Keychain Access",
    )
    if time_boxed:
        try:
            answer = p12_mod.probe(p12_bytes, password.encode())
        except p12_mod.SlowP12Error:
            raise ConfigError(
                f"{name}: this .p12 asks for an unreasonable amount of work to open; export "
                "it again",
                hint="a .p12 exported from Keychain Access opens in well under a second",
            ) from None
        except p12_mod.P12ProbeError as e:
            raise ConfigError(f"couldn't check {name}: {e}",
                              hint="try again; nothing was changed") from None
        if answer == "wrong":
            raise wrong
    try:
        key, cert, _ = p12_mod.load_key_and_certificates(p12_bytes, password.encode())
    except ValueError as e:
        raise wrong from e
    return key, cert


def read_identity(p12_path: Path, profile_path: Path, password: str) -> SigningIdentity:
    """Read both files once and check they make a signing identity.

    Refused, each with its reason: a file that isn't there, isn't a regular
    file (after following any link to it) or is too large; a profile that
    doesn't decode, has fields of the wrong type, names no team, or names one
    that isn't shaped like an Apple team ID; a .p12 that asks for too much key
    derivation, that the password doesn't open, or that lacks its key; a
    certificate that has expired or isn't valid yet, or that the profile
    doesn't list; a profile that has expired.

    An empty password is a password: Keychain Access can export a .p12 with
    a blank one.
    """
    profile_bytes = read_chosen_file(profile_path, "provisioning profile")
    p12_bytes = read_chosen_file(p12_path, ".p12")
    profile_name, p12_name = printable(profile_path.name), printable(p12_path.name)
    try:
        plist = mobileprovision.decode_bytes(profile_bytes, source=profile_name)
    except PreflightError as e:
        raise ConfigError(
            e.message, hint="choose the .mobileprovision downloaded from developer.apple.com"
        ) from e
    _check_profile_fields(plist, profile_name)
    teams = plist.get("TeamIdentifier") or []
    if not teams:
        raise ConfigError(
            f"{profile_name} names no team",
            hint="download a development profile from developer.apple.com",
        )
    # Before the .p12 is opened and long before anything is written: the team
    # becomes a Keychain account written through `security -i`, so a crafted
    # one (a line break, a quote) is refused here, never quoted later. As
    # written, not stripped: the bytes installed are what every later reader
    # (preflight, lc_sign) sees, so the value checked must be that one.
    prefixes = plist.get("ApplicationIdentifierPrefix") or []
    if not mobileprovision.is_team_id(teams[0]) or (
        prefixes and not mobileprovision.is_team_id(prefixes[0])
    ):
        raise ConfigError(
            f"{profile_name}'s team ID isn't a valid Apple team ID",
            hint="an Apple team ID is 10 capital letters and digits; download the "
            "profile again from developer.apple.com",
        )
    key, cert = _open_p12(p12_bytes, password, p12_name)
    if key is None or cert is None:
        raise ConfigError(
            f"{p12_name} doesn't hold both a certificate and its private key",
            hint="in Keychain Access, export the certificate from My Certificates, with its key",
        )
    now = datetime.now(UTC)
    if cert.not_valid_after_utc <= now:
        raise ConfigError(
            f"the certificate in {p12_name} expired on {cert.not_valid_after_utc.date().isoformat()}",
            hint="export the current Apple Development certificate from Keychain Access; "
            "an expired one is often still there beside the new one",
        )
    if cert.not_valid_before_utc > now:
        raise ConfigError(
            f"the certificate in {p12_name} isn't valid until "
            f"{cert.not_valid_before_utc.date().isoformat()}",
            hint="check that this Mac's date and time are right",
        )
    listed = mobileprovision.developer_certificate_serials(plist)
    if listed is not None and cert.serial_number not in listed:
        raise ConfigError(
            f"the certificate in {p12_name} (serial {cert.serial_number:X}) isn't one "
            f"{profile_name} lets sign apps",
            hint="download a profile that includes this certificate from developer.apple.com, "
            "or export the .p12 of the certificate the profile was made with",
        )
    expires = mobileprovision.expiration(plist)
    if expires is not None and expires <= now:
        raise ConfigError(
            f"{profile_name} expired on {expires.date().isoformat()}",
            hint="download a new one from developer.apple.com",
        )
    name = plist.get("Name")
    return SigningIdentity(
        team_id=teams[0],
        profile_name=printable(name) if name else None,
        devices=tuple(mobileprovision.provisioned_devices(plist)),
        expires=expires,
        certificate=_common_name(cert),
        certificate_listed=listed is not None,
        p12_bytes=p12_bytes,
        profile_bytes=profile_bytes,
    )


ACCOUNTS = (ACCOUNT_FREE, ACCOUNT_PAID)

#: `config_state` for no config.yaml, and for one that is there and doesn't parse.
MISSING = "missing"
UNREADABLE = "unreadable"

# What `InitResult.change` says happened to config.yaml.
CREATED = "created"
UPGRADED = "upgraded"
SWITCHED = "switched"
REPLACED = "replaced"
UNCHANGED = "unchanged"

#: `on_step(step, detail)`: one thing done, as a sentence.
Step = Callable[[str, str], None]


def _no_step(step: str, detail: str) -> None:
    pass


def config_state(cfg_path: Path) -> str:
    """Which config is at `cfg_path`: `free`, `paid`, `MISSING` or `UNREADABLE`.

    One reading for both setups: the interactive one asks it before offering
    to switch or upgrade (`init_flow`), and this one before refusing to
    overwrite. A config that doesn't parse is neither account.
    """
    if not cfg_path.exists():
        return MISSING
    try:
        return account_of(cfg_path)
    except ConfigError:
        return UNREADABLE


def prepare_dirs(cfg_path: Path) -> Path:
    """Make the directories `--init` writes into, and return the config's.

    The config's own is 0700 when this makes it: it is about to hold the
    .p12 and the profile. The tweaks folder and the IPA cache are made too.
    """
    cfg_dir = cfg_path.parent
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    default_tweaks_dir().mkdir(parents=True, exist_ok=True)
    default_cache_dir().mkdir(parents=True, exist_ok=True)
    return cfg_dir


_SUMMARIES = {
    (ACCOUNT_FREE, CREATED): "Set up for a free Apple ID: Graft builds your apps and "
    "puts them into LiveContainer.",
    (ACCOUNT_FREE, UNCHANGED): "Already set up for a free Apple ID.",
    (ACCOUNT_FREE, SWITCHED): "Switched to a free Apple ID. Your apps, repos and settings are kept.",
    (ACCOUNT_PAID, CREATED): "Set up for team {team}: Graft signs and installs your apps itself.",
    (ACCOUNT_PAID, UPGRADED): "Added a paid account for team {team}. Your apps, repos and "
    "settings are kept.",
    (ACCOUNT_PAID, REPLACED): "Replaced the certificate and profile for team {team}. Your "
    "apps, repos and settings are kept.",
}

#: `REPLACED`'s summary when the new pair belongs to another team.
_MOVED_SUMMARY = (
    "Replaced the certificate and profile: team {previous} is now team {team}. Your apps, "
    "repos and settings are kept."
)

DRY_RUN_SUMMARY = "Everything checks out; nothing was written (--dry-run)."


@dataclass(frozen=True)
class InitResult:
    """What `setup_account` came to: the `init-result` event (`fields`)."""

    account: str
    config: Path
    change: str
    dry_run: bool
    identity: SigningIdentity | None = None
    #: Apps given `delivery: livecontainer` on the way to paid, so none moves.
    pinned: tuple[str, ...] = ()
    #: Apps still `delivery: sideload` on a free config: refused until switched.
    sideloads: tuple[str, ...] = ()
    #: The team `signing:` named before a replace; None for every other change.
    previous_team_id: str | None = None
    #: What the user should know although the setup worked (or, in a dry run,
    #: would work): sentences, shown on the app's review and done screens.
    warnings: tuple[str, ...] = ()

    @property
    def steps(self) -> tuple[str, ...]:
        """What a free Apple ID still sets up on the device (`FREE_DEVICE_STEPS`)."""
        return FREE_DEVICE_STEPS if self.account == ACCOUNT_FREE else ()

    @property
    def summary(self) -> str:
        if self.dry_run:
            return DRY_RUN_SUMMARY
        team = self.identity.team_id if self.identity else ""
        previous = self.previous_team_id
        if self.change == REPLACED and previous and previous != team:
            return _MOVED_SUMMARY.format(team=team, previous=previous)
        return _SUMMARIES[(self.account, self.change)].format(team=team)

    def fields(self) -> dict[str, Any]:
        """The `init-result` event's fields. Never the password, which this
        object has never held."""
        ident = self.identity
        expires = (
            ident.expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ident and ident.expires else None
        )
        return {
            "account": self.account,
            "config": str(self.config),
            "change": self.change,
            "dry_run": self.dry_run,
            "summary": self.summary,
            "team_id": ident.team_id if ident else None,
            "profile_name": ident.profile_name if ident else None,
            "certificate": ident.certificate if ident else None,
            "device_count": len(ident.devices) if ident else None,
            "devices": list(ident.devices) if ident else [],
            "expires": expires,
            "pinned": list(self.pinned),
            "sideloads": list(self.sideloads),
            "steps": list(self.steps),
            "previous_team_id": self.previous_team_id,
            "warnings": list(self.warnings),
        }


def setup_account(
    cfg_path: Path,
    *,
    account: str,
    p12_path: Path | None = None,
    profile_path: Path | None = None,
    password: Callable[[], str] = lambda: "",
    dry_run: bool = False,
    replace: bool = False,
    on_step: Step = _no_step,
) -> InitResult:
    """`--init --account free|paid`: set the account up at `cfg_path`, asking nothing.

    `replace` (`--replace`) is for a paid config only: it swaps in a new
    certificate and profile (`_replace_paid`).

    `password` is called once, for a paid account, only after the arguments
    and the config at `cfg_path` have been checked, so nobody is asked for a
    password for a run that is refused anyway. `on_step(step, detail)` hears
    each thing done, in order. Under `dry_run` each says what would be done,
    and nothing is.
    """
    account = account.strip().lower()
    if account not in ACCOUNTS:
        raise ConfigError(f"--account must be free or paid, not {account!r}")
    files = (("--p12", p12_path), ("--profile", profile_path))
    if account == ACCOUNT_FREE:
        given = [flag for flag, path in files if path is not None]
        if given:
            raise ConfigError(
                f"{' and '.join(given)} {'is' if len(given) == 1 else 'are'} only for "
                "--account paid",
                hint="a free Apple ID has no certificate or profile",
            )
    else:
        missing = [flag for flag, path in files if path is None]
        if missing:
            raise ConfigError(
                f"--account paid needs {' and '.join(missing)}",
                hint="the .p12 exported from Keychain Access, and the development "
                "profile from developer.apple.com",
            )
    if replace and account == ACCOUNT_FREE:
        raise ConfigError(
            "--replace is only for --account paid",
            hint="it swaps a paid account's certificate and profile for new ones",
        )
    state = config_state(cfg_path)
    if state == UNREADABLE:
        raise ConfigError(
            f"{cfg_path} is there but can't be read, so the setup won't touch it",
            hint="fix it or move it aside, then run the setup again; nothing was changed",
        )
    if account == ACCOUNT_FREE:
        return _setup_free(cfg_path, state, dry_run=dry_run, on_step=on_step)
    if state == ACCOUNT_PAID and not replace:
        raise ConfigError(
            f"{cfg_path} is already set up for a paid account; add --replace to swap in a "
            "new certificate and profile",
            hint=f"{APP_ACCOUNT_SETTINGS} has Replace certificate and profile, which does "
            "the same; nothing was changed",
        )
    if replace and state != ACCOUNT_PAID:
        now = "is set up for a free Apple ID" if state == ACCOUNT_FREE else "doesn't exist yet"
        raise ConfigError(
            f"--replace swaps a paid account's certificate and profile, and {cfg_path} {now}",
            hint="leave out --replace to set up the paid account; nothing was changed",
        )
    secret = password()
    # Before the .p12 is opened and before anything is written: `security -i`
    # takes one line of 4095 bytes, and a password chosen by whoever handed
    # out the .p12 could otherwise spill into a command of their choosing.
    if not keychain.secret_fits(secret):
        raise ConfigError(keychain.P12_PASSWORD_TOO_LONG)
    assert p12_path is not None and profile_path is not None  # checked above
    identity = read_identity(p12_path, profile_path, secret)
    expiry = (
        f"expires {identity.expires.date().isoformat()}" if identity.expires else "no expiry date"
    )
    on_step("profile", f"{printable(profile_path.name)}: team {identity.team_id}, "
            f"{_count(len(identity.devices), 'device')}, {expiry}")
    cert = identity.certificate or "the certificate"
    checked = (
        "and the profile lists it" if identity.certificate_listed
        else "(the profile lists no certificates to check it against)"
    )
    on_step("certificate", f"{cert} in {printable(p12_path.name)} opens with that password, "
            f"{checked}")
    return _setup_paid(cfg_path, state, identity, secret, dry_run=dry_run, on_step=on_step)


def _setup_free(cfg_path: Path, state: str, *, dry_run: bool, on_step: Step) -> InitResult:
    if state == ACCOUNT_FREE:
        on_step("config", f"{cfg_path} is already set up for a free Apple ID")
        return InitResult(account=ACCOUNT_FREE, config=cfg_path, change=UNCHANGED, dry_run=dry_run)
    if state == ACCOUNT_PAID:
        sideloads = remove_signing_section(cfg_path, write=not dry_run)
        on_step("config", (
            f"would remove the signing: section from {cfg_path}; its apps, repos and "
            "settings would stay"
        ) if dry_run else (
            f"removed the signing: section from {cfg_path}; its apps, repos and settings "
            f"stay, and so do the signing files in {cfg_path.parent}"
        ))
        return InitResult(
            account=ACCOUNT_FREE, config=cfg_path, change=SWITCHED, dry_run=dry_run,
            sideloads=tuple(sideloads),
        )
    if not dry_run:
        prepare_dirs(cfg_path)
        write_default_config(cfg_path, free_account=True, only_if_missing=True)
    on_step("config", f"would write {cfg_path}" if dry_run else f"wrote {cfg_path}")
    return InitResult(account=ACCOUNT_FREE, config=cfg_path, change=CREATED, dry_run=dry_run)


#: Where the copies go, beside config.yaml: the names `--init` has always used.
P12_NAME = "AppleDevelopment.p12"
PROFILE_NAME = "wildcard.mobileprovision"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _store_password(team_id: str, password: str) -> None:
    """`--set-p12-password`'s entry (`p12-<team>`), from the same writer.
    What `security` said is in the error, never the password
    (`keychain._redacted`)."""
    try:
        keychain.write_p12_password(team_id, password)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise GraftError(
            f"couldn't store the .p12 password in the Keychain: {_keychain_detail(e)}",
            hint="unlock the login keychain, or allow access when macOS asks, and try again",
        ) from e


def _keychain_detail(e: BaseException) -> str:
    said = getattr(e, "stderr", None)
    return (said.decode(errors="replace") if isinstance(said, bytes) else str(said or e)).strip()


#: The variable a paid config's `signing.cert_password_env` names unless it
#: says otherwise (`config._signing_template`).
DEFAULT_PASSWORD_ENV = "GRAFT_P12_PASS"


def _password_env_name(cfg_path: Path, state: str) -> str:
    """The variable builds will read the .p12 password from first: the paid
    config's own `cert_password_env`, or the one a new section gets."""
    if state == ACCOUNT_PAID:
        section = config_mod._signing_section(config_mod._load_raw(cfg_path)) or {}
        named = section.get("cert_password_env")
        if isinstance(named, str) and named.strip():
            return named.strip()
    return DEFAULT_PASSWORD_ENV


def _check_password_env(env_name: str, password: str) -> str | None:
    """Refuse when `$<env_name>` would make builds use another password than
    the one being stored; the warning to give when it holds this one.

    The signer reads the variable before the Keychain (`signer.p12_password_for`),
    and the app hands every CLI run its login shell's environment, so a 1.x
    user's `export GRAFT_P12_PASS=<old>` would win over the renewed password.
    """
    value = os.environ.get(env_name)
    if not value:
        return None
    if value != password:
        raise ConfigError(
            f"{env_name} is set in your shell and overrides the Keychain, so builds would "
            f"use the old password. Remove it from your shell profile, or set it to the "
            "new password."
        )
    return (f"{env_name} is set in your shell and overrides the Keychain. It holds this "
            "password, so builds will work; if you change the password, remove it from "
            "your shell profile.")


def _kept_secret(team: str) -> tuple[bool, str | None]:
    """`(missing, secret)` for the team's Keychain item, which every paid
    write keeps to put back. One that is there but can't be read, or can't be
    written back, is refused before anything is changed."""
    missing, kept = keychain.find_p12_password(team)
    if not missing and kept is None:
        raise GraftError(
            f"couldn't read the Keychain item for team {team} (account p12-{team}), so it "
            "can't be put back if the setup fails",
            hint="unlock the login keychain, or allow access when macOS asks, and try "
            "again; nothing was changed",
        )
    if kept is not None and not keychain.secret_fits(kept):
        raise GraftError(
            f"the Keychain's password for team {team} (account p12-{team}) is longer than "
            f"{keychain.MAX_SECRET_BYTES} bytes, so it couldn't be put back if the "
            "setup fails",
            hint="remove that item in Keychain Access, then run the setup again; nothing "
            "was changed",
        )
    return missing, kept


@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    """SIGTERM, SIGHUP and SIGINT held for an undo, so a second Stop or
    Ctrl-C can't cut it short; delivered once it is done."""
    try:
        held = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}
        )
    except (OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


def _same_file(path: Path, ident: tuple[int, int] | None) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return ident is not None and (info.st_dev, info.st_ino) == ident


def _put_keychain_back(team: str, wrote: str, kept: str | None) -> Callable[[], None]:
    """The undo of storing `wrote` as team `team`'s password, where `kept`
    was before (None: no item): only while the item still holds `wrote`.

    The file undo is checked by inode, and this is its Keychain half
    (combined fix check #18): two setups for the same team, one that
    finished between this one's write and its failure, left their password
    in the item; deleting it then left that setup with a working file and no
    password. An item that holds something else now is another writer's.

    Asked of the `graft` item alone (`find_p12_password`), as `_kept_secret`
    asked: `read_p12_password` falls back to the pre-rename `tweak-build`
    item and copies it into a new `graft` one, and answers None for an item
    it couldn't read (a denied prompt, a locked keychain), which this took
    for another writer's and skipped without a word (the whole-diff review's
    cli-2). An item there that can't be read raises, so the undo lists it
    as not put back.
    """
    def put_back() -> None:
        missing, now = keychain.find_p12_password(team)
        if not missing and now is None:
            raise OSError("couldn't read it to check it still holds this setup's password")
        if now != wrote:
            return
        if kept is not None:
            keychain.write_p12_password(team, kept)
        else:
            keychain.delete_p12_password(team)

    return put_back


class _Change:
    """One paid setup's writes, every piece of which can be put back.

    Restoring is by rename, so it needs no free space and can't half-fail:
    each new copy is written beside its destination first (`stage`), the
    entry already there, a link included, is moved aside under a name of
    Graft's own, and the new one renamed in (`swap`). config.yaml keeps its
    old inode under a hard link while the new one is written (`keep_config`).
    `undo` renames every kept entry back; `commit` removes them.

    The undo only ever unlinks entries this run made, checked by inode, and
    never resolves a destination: a symlinked copy the user made comes back
    as the same link.
    """

    def __init__(self) -> None:
        self._undo: list[tuple[str, Callable[[], None]]] = []
        self._commit: list[Callable[[], None]] = []

    def _token(self) -> str:
        return secrets.token_hex(6)

    def stage(self, data: bytes, dest: Path) -> Path:
        staged = private_stage(data, dest)
        self._undo.append((f"the new {dest.name}", lambda: staged.unlink(missing_ok=True)))
        return staged

    def created(self, path: Path, *, locked_by: Path | None = None) -> None:
        """`path` is a file this run made where nothing was: the undo removes
        it, and only it (by inode), so what another writer put there since is
        never touched. With `locked_by` (config.yaml), the look and the
        removal are under that config's writers' lock (`keep_config`)."""
        info = os.lstat(path)
        ours = (info.st_dev, info.st_ino)

        def remove() -> None:
            with config_mod._config_lock(locked_by) if locked_by else contextlib.nullcontext():
                if _same_file(path, ours):
                    path.unlink()

        self._undo.append((str(path), remove))

    def swap(self, staged: Path, dest: Path) -> None:
        if dest.is_dir() and not dest.is_symlink():
            raise ConfigError(f"{dest} is a folder, so the setup won't put a file there",
                              hint="move it aside and run the setup again")
        if os.path.lexists(dest):
            kept = dest.with_name(f".{dest.name}.graft-old-{self._token()}")
            os.rename(dest, kept)
            try:
                os.rename(staged, dest)
            except BaseException:
                os.rename(kept, dest)
                raise
            self._undo.append((f"the old {dest.name}", lambda: os.replace(kept, dest)))
            self._commit.append(lambda: kept.unlink(missing_ok=True))
        else:
            os.rename(staged, dest)
            self.created(dest)

    def keep_config(self, cfg_path: Path) -> None:
        """Before config.yaml is written: keep what is there so `undo` can put
        it back. A config this run creates is registered once it is written
        (`created`). config.yaml is resolved, as its writer resolves it
        (`config._atomic_write_text`): a symlinked config is edited where it
        really lives, and put back there.

        The undo runs after the write's `_config_lock` is released, so each
        put-back takes it again: a writer that took it in between is never
        clobbered mid-write (the whole-diff review's cli-3). A lock that
        can't be had is a step that failed, said with the rest."""
        real = Path(os.path.realpath(cfg_path))
        if not real.exists():
            return
        kept = real.with_name(f".{real.name}.graft-old-{self._token()}")
        try:
            os.link(real, kept)
        except OSError:
            # No hard links here (some network and FAT volumes): keep the text.
            text = real.read_text(encoding="utf-8")

            def put_text_back() -> None:
                with config_mod._config_lock(cfg_path):
                    config_mod._atomic_write_text(real, text)

            self._undo.append((str(cfg_path), put_text_back))
            return

        def put_back() -> None:
            with config_mod._config_lock(cfg_path):
                # rename(2) does nothing when both names are links to one
                # inode, which they still are if config.yaml was never
                # rewritten.
                if real.exists() and os.path.samefile(kept, real):
                    kept.unlink()
                else:
                    os.replace(kept, real)

        self._undo.append((str(cfg_path), put_back))
        self._commit.append(lambda: kept.unlink(missing_ok=True))

    def on_undo(self, what: str, action: Callable[[], None]) -> None:
        self._undo.append((what, action))

    def undo(self) -> list[str]:
        """Put everything back, newest first; what couldn't be.

        A Stop or Ctrl-C held through it (`_signals_held`) is delivered as
        the hold ends, and so raised from here, after every step has run.
        It carries what couldn't be put back (`undo_failed`): raised bare,
        it took that list with it, and the process exited as stopped with
        nothing said (the whole-diff review's cli-4)."""
        failed: list[str] = []
        try:
            with _signals_held():
                for what, action in reversed(self._undo):
                    try:
                        action()
                    except Exception as e:
                        failed.append(f"{what} ({_keychain_detail(e)})")
        except (Stopped, KeyboardInterrupt) as stop:
            stop.undo_failed = failed  # type: ignore[union-attr]
            raise
        return failed

    def commit(self) -> None:
        with _signals_held():
            for action in self._commit:
                with contextlib.suppress(OSError):
                    action()


def _reopen(cfg_path: Path, identity: SigningIdentity) -> None:
    """Open the installed .p12 the way a build will: the cert path and team
    in config.yaml as written, the password by the signer's own order
    (`signer.p12_password_for`). A setup that a build would fail on is not
    a setup that worked."""
    from . import signer

    section = config_mod._signing_section(config_mod._load_raw(cfg_path)) or {}
    team = str(section.get("team_id") or "").strip()
    team_id = team if team and team != TEAM_ID_PLACEHOLDER else None
    password = signer.p12_password_for(str(section.get("cert_password_env") or ""), team_id)
    cert_path = config_mod._expand(str(section.get("cert") or ""))
    hint = "nothing was changed; check the password and try again"
    if password is None:
        raise GraftError("a build would find no .p12 password for this account", hint=hint)
    try:
        installed = cert_path.read_bytes()
    except OSError as e:
        raise GraftError(f"couldn't read the installed {cert_path}: {e.strerror or e}",
                         hint=hint) from None
    if installed != identity.p12_bytes:
        raise GraftError(f"{cert_path}, which config.yaml names, isn't the .p12 that was "
                         "checked", hint=hint)
    try:
        # The bytes `read_identity` opened in a child within its time, so
        # their cost is known; another password costs no more (its MAC check
        # fails first).
        _open_p12(installed, password, cert_path.name, time_boxed=False)
    except ConfigError as e:
        raise GraftError(
            f"the installed .p12 doesn't open with the password a build would use "
            f"({e.message})",
            hint=hint,
        ) from None


def _warnings(
    identity: SigningIdentity, env_warning: str | None, *, previous: str | None,
    asc: bool,
) -> tuple[str, ...]:
    said: list[str] = []
    if not identity.certificate_listed:
        said.append("The profile lists no certificates, so the certificate wasn't checked "
                    "against it.")
    if env_warning:
        said.append(env_warning)
    team = identity.team_id
    if previous and previous not in (team, TEAM_ID_PLACEHOLDER):
        if asc:
            said.append(f"the App Store Connect key is for team {previous}; automatic "
                        f"profiles will be refused until you set a key for team {team}.")
        said.append(f"Team {previous}'s .p12 password stays in the Keychain (account "
                    f"p12-{previous}): another config, or Graft 1.x, may still use it. "
                    "Remove it in Keychain Access if nothing does.")
        # Combined fix check #20: "your apps are kept" said nothing of the
        # LiveContainer on the phone, which is the old team's by bundle id.
        said.append(f"A LiveContainer installed for team {previous} "
                    f"(com.kdt.livecontainer.{previous}) stays on your devices with the apps "
                    "already in it, and deliveries keep going to it until you install "
                    f"LiveContainer again (--lc-install), which installs team {team}'s; "
                    "new deliveries then go there.")
    return tuple(said)


def _setup_paid(
    cfg_path: Path, state: str, identity: SigningIdentity, password: str,
    *, dry_run: bool, on_step: Step,
) -> InitResult:
    """A paid account on no config (created), on a free one (upgraded) or on
    a paid one (replaced, `--replace`), as one `_Change`.

    The order: the two copies staged beside their destinations, the Keychain
    item stored, the copies swapped in, config.yaml written, and the installed
    .p12 opened again with the password a build will use (`_reopen`). Before
    any of it, what is there now is kept: the team's Keychain item (refused
    if it can't be read, since nothing could put it back) and every entry
    the copies replace, so a failure at any step, or a Stop, a Ctrl-C or a
    reader that hung up, leaves the account as it was.

    A replace keeps the apps, repos and settings, and the old team's
    Keychain item too: other configs, and the 1.x CLI, may read it.
    """
    change = {ACCOUNT_PAID: REPLACED, ACCOUNT_FREE: UPGRADED}.get(state, CREATED)
    team = identity.team_id
    cfg_dir = cfg_path.parent.absolute()
    cert_dest, profile_dest = cfg_dir / P12_NAME, cfg_dir / PROFILE_NAME
    previous = None
    raw: dict[str, Any] = {}
    if change == REPLACED:
        raw = config_mod._load_raw(cfg_path)
        previous = (read_settings(cfg_path).get("signing_team_id") or "").strip() or None
    env_warning = _check_password_env(_password_env_name(cfg_path, state), password)
    # A read writes nothing, so the dry run makes it too: the review then
    # refuses what the real run would (F12).
    _, kept_secret = _kept_secret(team)
    warnings = _warnings(identity, env_warning, previous=previous,
                         asc=bool(raw.get("app_store_connect")))
    result = InitResult(
        account=ACCOUNT_PAID, config=cfg_path, change=change, dry_run=dry_run,
        identity=identity, previous_team_id=previous, warnings=warnings,
    )
    signing = {"cert": cert_dest, "profile": profile_dest, "team_id": team}
    where = {
        CREATED: f"write {cfg_path} for team {team}",
        UPGRADED: f"add a signing: section for team {team} to {cfg_path}",
        REPLACED: f"replace the certificate, profile and team ({team}) in {cfg_path}'s "
        "signing: section; its apps, repos and settings stay",
    }[change]
    if dry_run:
        pinned = add_signing_section(cfg_path, write=False, **signing) if change == UPGRADED else []
        on_step("config", f"would {where}")
        return replace(result, pinned=tuple(pinned))

    prepare_dirs(cfg_path)
    work = _Change()
    pinned: list[str] = []
    try:
        staged_p12 = work.stage(identity.p12_bytes, cert_dest)
        staged_profile = work.stage(identity.profile_bytes, profile_dest)
        # Registered before the write: a `security` that wrote and then
        # complained (exit 0, words on stderr) is put back too (F5).
        work.on_undo(f"the Keychain item p12-{team}", _put_keychain_back(team, password, kept_secret))
        _store_password(team, password)
        on_step("keychain", f"stored the .p12 password in the Keychain (service "
                f"{keychain.SERVICE}, account p12-{team})")
        work.swap(staged_p12, cert_dest)
        work.swap(staged_profile, profile_dest)
        on_step("files", f"copied the .p12 and the profile into {cfg_dir}, readable only by you")
        with config_mod._config_lock(cfg_path):
            work.keep_config(cfg_path)
            if change == CREATED:
                write_default_config(cfg_path, free_account=False, only_if_missing=True, **signing)
                work.created(Path(os.path.realpath(cfg_path)), locked_by=cfg_path)
            elif change == UPGRADED:
                pinned = add_signing_section(cfg_path, **signing)
            else:
                added = replace_signing(cfg_path, **signing)
                if added:
                    on_step("config", f"signing: had no {', '.join(added)}; set it to "
                            f"{DEFAULT_PASSWORD_ENV}, as a new setup does")
            _reopen(cfg_path, identity)
            on_step("config", _done(where))
    except BaseException as e:
        try:
            failed = work.undo()
        except (Stopped, KeyboardInterrupt) as stop:
            # A Stop held through the undo: it still stops the run, and
            # carries the report for the CLI to say first (`undo_incomplete`).
            failed = getattr(stop, "undo_failed", [])
            if failed:
                stop.undo_incomplete = _undo_incomplete(e, failed)  # type: ignore[union-attr]
            raise
        if failed:
            raise _undo_incomplete(e, failed) from e
        raise
    work.commit()
    return replace(result, pinned=tuple(pinned))


def _undo_incomplete(cause: BaseException, failed: list[str]) -> GraftError:
    """What a setup whose undo couldn't put everything back says."""
    return GraftError(
        f"the setup failed ({_keychain_detail(cause) or type(cause).__name__}), and "
        f"{', '.join(failed)} couldn't be put back",
        hint="put those back by hand, or run the setup again",
    )


def _done(where: str) -> str:
    """`where` said as done: "write X" as "wrote X"."""
    first, rest = where.split(" ", 1)
    return f"{ {'write': 'wrote', 'add': 'added', 'replace': 'replaced'}[first]} {rest}"
