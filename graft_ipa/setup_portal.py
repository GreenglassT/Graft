"""Interactive `graft --setup-portal` wizard, and its non-interactive twin.

Walks the user through:
  1. Where to create an App Store Connect API key (link + role guidance).
  2. Move the .p8 file into ~/.config/graft/.
  3. Record key_id, issuer_id, and the path into config.yaml.
  4. Sanity-check by listing devices via the API to confirm credentials work.

`set_portal` does the same with the answers handed in (`--set-portal`, which
the app's Settings pane runs), and checks the key *before* it copies or writes
anything, so a key that fails leaves no trace. `clear_portal` removes the
setting (`--clear-portal`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt

from . import apple_portal
from .config import (
    ACCOUNT_FREE,
    APP_ACCOUNT_SETTINGS,
    AppStoreConnectConfig,
    _default_delivery,
    _load_raw,
    _signing_section,
    account_of,
    clear_app_store_connect,
    default_config_path,
    load_config,
    paid_account_required,
    set_app_store_connect,
)
from .data_dirs import private_write
from .errors import ConfigError, GraftError

#: Where App Store Connect keys are made — the wizard's first sentence, and the
#: Settings pane's link.
KEYS_URL = "https://appstoreconnect.apple.com/access/integrations/api"

#: The device classes an auto-provisioned profile is made for: the ones an iOS
#: app installs on. App Store Connect's IOS platform also lists a registered
#: Apple Watch or Apple TV, which can't take the app and would only use a slot.
_APP_DEVICE_CLASSES = frozenset({"IPHONE", "IPAD", "IPOD"})


#: App Store Connect's Key ID: ten capital letters and digits. Checked before it
#: is used, because it names a file — `AuthKey_<key_id>.p8` beside config.yaml
#: — and `../x` would name one somewhere else.
_KEY_ID = re.compile(r"[A-Z0-9]{10}")
#: The Issuer ID is a UUID.
_ISSUER_ID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


def _require_ids(key_id: str, issuer_id: str) -> None:
    """Refuse a Key ID or Issuer ID that is not shaped like App Store Connect's.

    Both paths call this before the key id goes into a filename or either id
    goes into config.yaml.
    """
    if not _KEY_ID.fullmatch(key_id):
        raise ConfigError(
            f"{key_id!r} is not an App Store Connect Key ID",
            hint="the Key ID is the 10-character id shown next to the key in App Store Connect",
        )
    if not _ISSUER_ID.fullmatch(issuer_id):
        raise ConfigError(
            f"{issuer_id!r} is not an App Store Connect Issuer ID",
            hint="the Issuer ID is the long id shown above your keys in App Store Connect",
        )


def _key_dest(cfg_path: Path, key_id: str) -> Path:
    """Where the key is installed: `AuthKey_<key_id>.p8` beside config.yaml,
    as an absolute path, because it is written into config.yaml. A relative
    `--config` made it relative, and then it named the key only from the
    directory the setup ran in. `absolute()`, not `resolve()`: a symlink at
    the destination is replaced, never followed (`data_dirs.private_write`)."""
    return cfg_path.parent.absolute() / f"AuthKey_{key_id}.p8"


def _install_then_record(cfg_path: Path, key_bytes: bytes, dest: Path, section: dict[str, Any]) -> None:
    """Install the key at `dest`, then write `section`; undo the install if
    the write fails.

    The key goes in first so config.yaml never names a file that isn't
    there. But when the write then failed (another graft holding the config
    lock past its wait, a full disk), the private key stayed beside the
    config with nothing naming it, against this module's promise that a
    failed set-up leaves no trace. A copy that was already there (the same
    key set again) is left alone.
    """
    existed = dest.exists()
    private_write(key_bytes, dest)
    try:
        set_app_store_connect(cfg_path, section)
    except BaseException:
        if not existed:
            dest.unlink(missing_ok=True)
        raise


#: The name `_key_dest` gives a key's copy.
_KEY_COPY = re.compile(r"AuthKey_[A-Z0-9]{10}\.p8")


def other_key_copies(cfg_path: Path, *, keep: Path | None) -> list[Path]:
    """Every `AuthKey_<id>.p8` beside config.yaml except `keep`.

    A new key never removed the copy of the one before it, and removing a
    key names only its own file, so an earlier private key could sit in the
    config folder unmentioned. They are named rather than deleted: App Store
    Connect lets a key be downloaded once, so this copy may be the only one.
    """
    folder = cfg_path.parent.absolute()
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    kept = keep.absolute() if keep is not None else None
    return [
        p for p in entries
        if _KEY_COPY.fullmatch(p.name) and p.absolute() != kept and p.is_file()
    ]


def key_copies_warning(copies: list[Path]) -> str | None:
    """What to say about `copies` (`other_key_copies`), or None for none:
    the one wording for --set-portal, --clear-portal and the wizard."""
    if not copies:
        return None
    names = ", ".join(p.name for p in copies)
    return (
        f"{names} {'is' if len(copies) == 1 else 'are'} still in {copies[0].parent}: "
        "a private key no setting uses; delete it once you no longer need that key"
    )


def _is_key_file(path: Path) -> bool:
    """`path.is_file()`, with a path graft may not look into refused as a
    `ConfigError` rather than escaping as a raw `PermissionError`.

    On Python 3.11-3.13 `is_file` re-raises EACCES and EPERM from the stat: a
    .p8 inside a folder the user cannot search made `--set-portal` exit 1
    with "that isn't a failure graft knows how to explain".
    """
    try:
        return path.is_file()
    except OSError as e:
        raise ConfigError(
            f"can't look at {path}: {e.strerror or e}",
            hint="move the .p8 somewhere you can open, such as Downloads, and choose it there",
        ) from e


def _enabled_udids(devices: list[apple_portal.Device]) -> list[str]:
    """Every enabled iPhone, iPad and iPod touch, sorted by UDID.

    `device_udids`, which every auto-provisioned profile is created for. It
    used to be the first three enabled devices in whatever order App Store
    Connect returned them, so a team with three older phones still enabled
    wrote a list without the user's own phone, and every profile left it
    out. There is no cap: a development profile takes every registered
    device, and the only cost of a longer list is a longer profile.

    Sorted, so the same devices write the same list whatever order App Store
    Connect lists them in. A device whose class App Store Connect didn't
    report is kept: leaving out a phone is the failure this exists to stop.
    """
    return sorted(
        {
            d.udid for d in devices
            if d.status == "ENABLED" and d.udid
            and (not d.device_class or d.device_class in _APP_DEVICE_CLASSES)
        },
        key=str.lower,
    )


def refresh_registered_devices(cfg_path: Path) -> list[str]:
    """Re-read the registered devices from App Store Connect and save them as
    `app_store_connect.device_udids`: the listing Save and test takes
    (`ASCClient.list_devices`, `_enabled_udids`), written the same way
    (`set_app_store_connect`, under the config lock). Returns the enabled
    UDIDs.

    For an install to a device the saved list predates (spec §5b). With
    `device_udids_pinned: true` the list is the person's: nothing is asked,
    and the pinned list is returned. Registers nothing: a device is the
    person's to add at developer.apple.com.
    """
    raw = _load_raw(cfg_path)
    asc = AppStoreConnectConfig.from_dict(
        raw.get("app_store_connect"), default_cache_dir=cfg_path.parent / "profiles"
    )
    if asc is None:
        raise ConfigError(
            "no App Store Connect key is set, so the registered devices can't be read",
            hint="add one in Settings › App Store Connect, or run `graft --setup-portal`",
        )
    if asc.device_udids_pinned:
        return list(asc.device_udids)
    client = apple_portal.ASCClient(apple_portal.PortalCredentials(
        key_id=asc.key_id, issuer_id=asc.issuer_id, key_path=asc.key_file,
    ))
    udids = _enabled_udids(client.list_devices())
    set_app_store_connect(cfg_path, {"device_udids": udids})
    return udids


def _check_key(
    key_id: str, issuer_id: str, key_path: Path, key_bytes: bytes
) -> list[apple_portal.Device]:
    """List devices with this key: the sanity check both paths run.

    Signed with `key_bytes`, the key as already read; `key_path` only names it.

    A key file that is not an EC private key fails inside the JWT library, as
    whatever that raises, before any request is made. It is still this key
    failing to sign in, so it is reported as a `PortalError` with the
    library's words rather than falling through to "a failure graft doesn't
    know how to explain".
    """
    creds = apple_portal.PortalCredentials(
        key_id=key_id, issuer_id=issuer_id, key_path=key_path, key_bytes=key_bytes
    )
    try:
        return apple_portal.ASCClient(creds).list_devices()
    except apple_portal.PortalError:
        raise
    except Exception as e:
        raise apple_portal.PortalError(
            f"couldn't sign in to App Store Connect with {key_path.name}: {e}",
            hint="choose the .p8 App Store Connect gave you when you made the key",
        ) from e


def set_portal(
    cfg_path: Path, *, key_id: str | None, issuer_id: str | None, key_file: str | None
) -> dict[str, Any]:
    """`--set-portal`: what the wizard does, with the answers given.

    Refuses a free config and a missing one, as the wizard does. Then checks
    the key against App Store Connect from the file the user chose, and only
    when that worked copies it beside the config as `AuthKey_<key_id>.p8`
    (0600) and writes the key into the section, merged with what is there
    (`set_app_store_connect`: a hand-set `profile_cache_dir` survives, and
    `device_udids` is refreshed unless `device_udids_pinned: true`). A failed
    check raises its `PortalError` —
    message and hint — with nothing written.

    Returns what the `portal-set` event reports: the key's id, issuer, where
    its .p8 now is, and how many devices App Store Connect listed.
    """
    # The account first, which also says "config file not found" for a
    # missing config: no flag is worth checking against a config that isn't
    # there or can't use one.
    if account_of(cfg_path) == ACCOUNT_FREE:
        raise paid_account_required("--set-portal", cfg_path)
    given = {
        "--asc-key-id": (key_id or "").strip(),
        "--asc-issuer-id": (issuer_id or "").strip(),
        "--asc-key-file": (key_file or "").strip(),
    }
    missing = [flag for flag, value in given.items() if not value]
    if missing:
        raise ConfigError(
            f"--set-portal requires {', '.join(missing)}",
            hint=f"make a key at {KEYS_URL}, then pass its Key ID, the Issuer ID, "
            "and the .p8 you downloaded",
        )
    key_id, issuer_id = given["--asc-key-id"], given["--asc-issuer-id"]
    _require_ids(key_id, issuer_id)
    source = Path(given["--asc-key-file"]).expanduser()
    if not _is_key_file(source):
        raise ConfigError(
            f"no .p8 file at {source}",
            hint="choose the AuthKey_….p8 you downloaded from App Store Connect",
        )

    # Read once, capped: these bytes are what App Store Connect is asked
    # about and what is installed, so the two cannot differ.
    key_bytes = apple_portal.read_key_file(source)
    devices = _check_key(key_id, issuer_id, source, key_bytes)

    dest = _key_dest(cfg_path, key_id)
    _install_then_record(cfg_path, key_bytes, dest, {
        "key_id": key_id,
        "issuer_id": issuer_id,
        "key_file": str(dest),
        "device_udids": _enabled_udids(devices),
    })
    return {"key_id": key_id, "issuer_id": issuer_id, "key_file": str(dest), "devices": len(devices)}


def clear_portal(cfg_path: Path) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """`--clear-portal`: remove the App Store Connect setting.

    Returns the section that was removed (None when there was none), the
    sideload apps still set to `auto_profile: true`, whose builds now fail
    until a key is set again or the option is turned off — the caller warns
    about them — and why those apps could not be named, or None. A
    LiveContainer app is left out whatever its `auto_profile`: its builds
    never sign, so the key was never theirs to lose. So is an app with its
    own `profile:`, which signs with that file instead. An app with no
    `delivery:` takes the account's default, as the loader reads it.
    The .p8 file is left where it is.

    The account is read after the section is gone, and a config whose
    signing keys are stranded at the top level refuses to say what it is
    (`_signing_section`). That used to raise, so a clear that had landed
    exited 2; the removal stands, and the refusal comes back as the third
    value for the caller to warn with.
    """
    removed = clear_app_store_connect(cfg_path)
    raw = _load_raw(cfg_path)
    try:
        default = _default_delivery(free_account=_signing_section(raw) is None)
    except ConfigError as e:
        return removed, [], e.message
    apps = raw.get("apps") or {}
    # An app with its own `profile:` signs with that file whatever
    # auto_profile says (`signer._plan_profile`), so it never needed the key.
    auto = sorted(
        name for name, entry in apps.items()
        if isinstance(entry, dict) and entry.get("auto_profile")
        and not entry.get("profile")
        and str(entry.get("delivery", default)).lower() == "sideload"
    ) if isinstance(apps, dict) else []
    return removed, auto, None


def run_setup_portal(console: Console | None = None, cfg_path: Path | None = None) -> None:
    """`cfg_path` is `--config`'s, the default config without one."""
    console = console or Console()
    console.rule("App Store Connect API key setup")

    cfg_path = cfg_path or default_config_path()
    if not cfg_path.exists():
        raise GraftError(
            f"config not found at {cfg_path}",
            hint="run `graft --init` first, or open Graft.app, whose first run sets up "
            "your account",
        )
    # Before a single prompt: an App Store Connect API key exists only for a
    # paid Apple Developer Program team, and what it is for here — creating
    # profiles that sign builds — is a paid-only path.
    if account_of(cfg_path) == ACCOUNT_FREE:
        raise paid_account_required("--setup-portal", cfg_path)

    console.print(
        f"Open [bold]{KEYS_URL}[/bold] "
        "and create a new key with role [bold]Developer[/bold] (or higher). "
        "Download the .p8 file. You will only get the chance to download it once."
    )
    console.print(
        "From the same page, copy the [bold]Key ID[/bold] (10 chars) and the "
        "[bold]Issuer ID[/bold] (UUID at the top of the page)."
    )

    key_id = Prompt.ask("Key ID").strip()
    issuer_id = Prompt.ask("Issuer ID").strip()
    _require_ids(key_id, issuer_id)

    p8_src = Prompt.ask(
        "Path to the downloaded .p8 file",
        default=str(Path.home() / "Downloads" / f"AuthKey_{key_id}.p8"),
    )
    # Both absolute, so a key typed relatively that is already in place
    # compares equal to where it would go.
    p8_src_path = Path(p8_src).expanduser().absolute()
    p8_dest = _key_dest(cfg_path, key_id)

    # Which file is the key, and whether it is to be installed as
    # `AuthKey_<id>.p8`: the one chosen, if the copy is confirmed; the one
    # already there otherwise (the copy declined, or nothing at the path typed
    # but a key in place), checked where it is. `is_file()`, as `set_portal`
    # gates, not `exists()`: a FIFO exists, and reading one blocked the wizard
    # forever.
    if _is_key_file(p8_src_path) and p8_src_path != p8_dest:
        install = Confirm.ask(f"Copy {p8_src_path} -> {p8_dest}?", default=True)
        source = p8_src_path if install else p8_dest
    elif p8_src_path == p8_dest and _is_key_file(p8_dest):
        # Rewritten rather than chmod'ed: a chmod follows a symlink at `dest`.
        source, install = p8_dest, True
    elif not _is_key_file(p8_dest):
        raise GraftError(
            f".p8 file not found at {p8_src_path}",
            hint="re-download from App Store Connect and re-run --setup-portal",
        )
    else:
        source, install = p8_dest, False

    # `set_portal`'s path: read once (`read_key_file`, which checks the
    # descriptor too and caps the read), check those bytes, install those
    # bytes — only after the check passed. A key App Store Connect refuses, or
    # one that is not an EC private key at all, raises its `PortalError`
    # (`_check_key`) with nothing installed and nothing written, and the
    # wizard exits non-zero.
    key_bytes = apple_portal.read_key_file(source)
    console.print("[dim]sanity-check: listing devices via ASC API...[/dim]")
    devices = _check_key(key_id, issuer_id, source, key_bytes)
    console.print(
        f"[green]ok[/green] auth works. ASC sees [bold]{len(devices)}[/bold] iOS device(s):"
    )
    for d in devices[:5]:
        console.print(f"  {d.udid}  {d.name}  ({d.status})")

    # Merge into config.yaml.
    # Through config's own writer: atomic, and under the config lock, so it
    # cannot erase a change the GUI makes at the same moment. Installed just
    # before, and taken back out if the write fails (`_install_then_record`).
    section = {
        "key_id": key_id,
        "issuer_id": issuer_id,
        "key_file": str(p8_dest),
        "device_udids": _enabled_udids(devices),
    }
    if install:
        _install_then_record(cfg_path, key_bytes, p8_dest, section)
        if source != p8_dest:
            console.print(f"[green]copied[/green] .p8 to {p8_dest}")
    else:
        set_app_store_connect(cfg_path, section)
    console.print(f"[green]wrote[/green] app_store_connect to {cfg_path}")
    # The earlier keys' copies, as --set-portal names them (combined fix check #6).
    leftover = key_copies_warning(other_key_copies(cfg_path, keep=p8_dest))
    if leftover:
        console.print(f"[yellow]warning:[/yellow] {escape(leftover)}")

    console.print(
        "\nTo turn on auto-provisioning for an app, run "
        "[bold]graft --set-app-options --app NAME --auto-profile[/bold], or turn on "
        "\"Create the provisioning profile automatically\" in the app's Edit sheet. "
        "The first build will register a wildcard App ID + profile in your portal "
        "and cache it; subsequent builds reuse the cache."
    )

    # Verify it parses.
    try:
        load_config(cfg_path)
    except GraftError as e:
        raise GraftError(
            f"config no longer parses after edit: {e.message}",
            hint="restore it from a backup, or set the account up again with `graft "
            f"--init` or {APP_ACCOUNT_SETTINGS}",
        ) from e
