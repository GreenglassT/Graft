"""Interactive `graft --init` setup.

The first question is whether the user has a paid Apple Developer Program
membership, because everything after it depends on the answer. Yes: the .p12,
password, profile and team, then a config with a `signing:` section. No: a
free Apple ID, which signs nothing through Graft — LiveContainer is installed
and refreshed on the device by SideStore or iloader — so those questions are
skipped, the config has no `signing:` section (which is what makes it free),
and the user gets the few setup steps that happen outside Graft.

Yes on a machine that already has a free config is an upgrade, not a fresh
start: only the signing questions are asked, and the `signing:` section is
added to that config (`config.add_signing_section`), keeping its apps, repos
and settings. That is the path every "needs a paid membership" hint describes
(`config.paid_upgrade`).

No on a machine that already has a paid config is the mirror: a switch to a
free Apple ID, offered rather than assumed, that removes the `signing:`
section and keeps the rest (`config.remove_signing_section`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt

from .account_setup import config_state, prepare_dirs
from .config import (
    ACCOUNT_FREE,
    ACCOUNT_PAID,
    FREE_SETUP_STEPS,
    TEAM_ID_PLACEHOLDER,
    add_signing_section,
    default_cache_dir,
    default_config_path,
    default_tweaks_dir,
    remove_signing_section,
    write_default_config,
)
from .data_dirs import private_copy
from .mobileprovision import is_team_id
from .preflight import list_connected_devices, parse_mobileprovision


def run_init(console: Console | None = None, cfg_path: Path | None = None) -> Path:
    """`cfg_path` is `--config`'s, the default config without one."""
    console = console or Console()
    console.rule("[bold]graft setup[/bold]")

    cfg_path = cfg_path or default_config_path()
    cfg_dir = prepare_dirs(cfg_path)
    tweaks_dir = default_tweaks_dir()
    cache_dir = default_cache_dir()

    console.print(f"config dir: [cyan]{cfg_dir}[/cyan]")
    console.print(f"cache dir:  [cyan]{cache_dir}[/cyan]")
    console.print(f"tweaks dir: [cyan]{tweaks_dir}[/cyan]")

    # 0. which kind of account. No default: the two answers write different
    # configs, and Enter should not pick one for the user.
    console.rule("Apple account")
    console.print(
        "A paid Apple Developer Program membership lets Graft sign and install "
        "apps itself. With a free Apple ID, Graft delivers apps into "
        "LiveContainer, which SideStore or iloader keeps installed for you."
    )
    if not Confirm.ask("Do you have a paid Apple Developer Program membership?"):
        if _is_paid_config(cfg_path):
            return _switch_to_free(console, cfg_path)
        return _run_free_init(console, cfg_path)
    if _is_free_config(cfg_path):
        return _upgrade_to_paid(console, cfg_path)

    overrides = _ask_signing(console, cfg_dir)

    # 4. devices
    default_udid = _pick_device(console)

    # 5. write config
    if default_udid:
        overrides["default_device_udid"] = default_udid
    if not _write(console, cfg_path, free_account=False, overrides=overrides):
        return cfg_path
    console.rule("[bold green]Setup complete[/bold green]")
    console.print(
        "Add an app with `graft --add-app <App Store URL>` (or edit "
        f"[cyan]{cfg_path}[/cyan] — it has an example), then run `graft --check`."
    )
    return cfg_path


def _is_free_config(cfg_path: Path) -> bool:
    """A config is there and it is a free Apple ID's. One that does not parse
    is neither: the fresh-start path asks before replacing it."""
    return config_state(cfg_path) == ACCOUNT_FREE


def _is_paid_config(cfg_path: Path) -> bool:
    """A config is there and it is a paid account's; one that does not parse
    is neither, as for `_is_free_config`."""
    return config_state(cfg_path) == ACCOUNT_PAID


def _switch_to_free(console: Console, cfg_path: Path) -> Path:
    """No on a paid config: offer to switch it to a free Apple ID, keeping
    its apps, repos and settings (`remove_signing_section`).

    It used to fall through to the fresh start: "overwrite?", and on yes an
    empty free starter where the config had been — every app, repo and App
    Store Connect key gone. Declining keeps the paid config as it is.
    """
    console.print(
        f"[cyan]{cfg_path}[/cyan] is set up for a paid account. Switching it to a free "
        "Apple ID removes its signing: section and keeps its apps, repos and settings; "
        "apps with no delivery: key then go into LiveContainer."
    )
    if not Confirm.ask("Switch this config to a free Apple ID?", default=False):
        console.print("kept the paid config as it is")
        return cfg_path
    console.rule("Updating config")
    sideloads = remove_signing_section(cfg_path)
    console.print(f"[green]✓[/green] removed the signing: section from {cfg_path}")
    if sideloads:
        console.print(
            f"  [yellow]![/yellow] still delivery: sideload, which a free Apple ID cannot "
            f"build: {', '.join(sideloads)}. Switch each with `graft --set-app-options "
            "--app <name> --delivery livecontainer`."
        )
    console.rule("[bold green]Setup complete: free Apple ID[/bold green]")
    console.print("To finish setting up:")
    for n, step in enumerate(FREE_SETUP_STEPS, 1):
        console.print(f"  {n}. {step}")
    return cfg_path


def _upgrade_to_paid(console: Console, cfg_path: Path) -> Path:
    """Yes on a free config: the signing questions, then a `signing:` section
    added to it. Nothing else in it changes, except that an app with no
    `delivery:` key is given `delivery: livecontainer`, which is what it was
    (with a paid account, no key means sideload)."""
    console.print(
        f"[cyan]{cfg_path}[/cyan] is set up for a free Apple ID. Graft adds a "
        "signing: section to it and keeps its apps, repos and settings."
    )
    overrides = _ask_signing(console, cfg_path.parent)
    console.rule("Updating config")
    pinned = add_signing_section(cfg_path, **overrides)
    console.print(f"[green]✓[/green] added a signing: section to {cfg_path}")
    if pinned:
        console.print(
            f"  delivery: livecontainer written into {', '.join(pinned)}, so they stay "
            "in LiveContainer (with a paid account, no delivery: key means sideload). "
            "Switch one with `graft --set-app-options --app <name> --delivery sideload`."
        )
    console.rule("[bold green]Setup complete: paid account[/bold green]")
    console.print("Run `graft --check` to see the signing files and the device.")
    return cfg_path


def _ask_signing(console: Console, cfg_dir: Path) -> dict[str, Any]:
    """The signing questions: the .p12, its password, the profile and the
    team id read off it. Returns `write_default_config`'s signing overrides."""
    # 1. .p12 cert
    console.rule("Signing certificate (.p12)")
    cert_src = Prompt.ask(
        "Path to your Apple Developer .p12 (export from Keychain)",
        default=str(Path.home() / "Downloads" / "AppleDevelopment.p12"),
    )
    cert_src_path = Path(os.path.expanduser(cert_src))
    cert_dest = cfg_dir / "AppleDevelopment.p12"
    if cert_src_path.exists() and cert_src_path != cert_dest:
        if Confirm.ask(f"Copy {cert_src_path} -> {cert_dest}?", default=True):
            private_copy(cert_src_path, cert_dest)
            console.print(f"[green]✓[/green] copied .p12 to {cert_dest}")
    elif cert_src_path == cert_dest and cert_dest.exists():
        console.print(f"[green]✓[/green] using existing {cert_dest}")
    else:
        console.print(f"[yellow]![/yellow] {cert_src_path} not found — place the .p12 at {cert_dest} before running a build")

    # 2. password
    console.rule("Signing password")
    env_name = "GRAFT_P12_PASS"
    if os.environ.get(env_name):
        console.print(f"[green]✓[/green] ${env_name} is already set")
    else:
        # The Keychain first: it works everywhere. The Mac app does read the
        # login shell's environment at launch, but only if the shell answers.
        console.print("Once setup finishes, store the .p12 password in the Keychain:")
        console.print("  [dim]graft --set-p12-password[/dim]")
        console.print(
            f"(Or export ${env_name} in your shell profile; the Mac app reads "
            "your login shell's environment when it starts.)"
        )

    # 3. mobileprovision
    console.rule("Provisioning profile (.mobileprovision)")
    profile_src = Prompt.ask(
        "Path to your wildcard .mobileprovision",
        default=str(Path.home() / "Downloads" / "wildcard.mobileprovision"),
    )
    profile_src_path = Path(os.path.expanduser(profile_src))
    profile_dest = cfg_dir / "wildcard.mobileprovision"
    if profile_src_path.exists() and profile_src_path != profile_dest:
        if Confirm.ask(f"Copy {profile_src_path} -> {profile_dest}?", default=True):
            private_copy(profile_src_path, profile_dest)
            console.print(f"[green]✓[/green] copied profile to {profile_dest}")
    elif profile_src_path == profile_dest and profile_dest.exists():
        console.print(f"[green]✓[/green] using existing {profile_dest}")

    team_id = TEAM_ID_PLACEHOLDER
    if profile_dest.exists():
        try:
            plist = parse_mobileprovision(profile_dest)
            team_ids = plist.get("TeamIdentifier") or []
            # Checked before it is stored: config.yaml's team becomes the
            # Keychain account `--set-p12-password` writes through
            # `security -i`, so a crafted one is never kept.
            first = team_ids[0] if isinstance(team_ids, list) and team_ids else None
            if isinstance(first, str) and is_team_id(first):
                team_id = first
                console.print(f"[green]✓[/green] team id from profile: [bold]{team_id}[/bold]")
            elif team_ids:
                console.print(
                    "[yellow]![/yellow] the profile's team ID isn't a valid Apple team ID "
                    "(10 capital letters and digits), so it was not used"
                )
        except Exception as e:
            console.print(f"[yellow]![/yellow] could not parse profile: {e}")
    if team_id == TEAM_ID_PLACEHOLDER:
        console.print(
            "[yellow]![/yellow] no team id read from a profile; set it before "
            "`graft --set-p12-password`: `graft --set-settings --signing-team-id <TEAM>`"
        )
    return {"cert": cert_dest, "profile": profile_dest, "team_id": team_id}


def _run_free_init(console: Console, cfg_path: Path) -> Path:
    """`--init` for a free Apple ID: no signing questions, a config with no
    `signing:` section, and the setup that happens on the device."""
    default_udid = _pick_device(console)
    overrides: dict[str, Any] = {}
    if default_udid:
        overrides["default_device_udid"] = default_udid
    if not _write(console, cfg_path, free_account=True, overrides=overrides):
        return cfg_path
    console.rule("[bold green]Setup complete: free Apple ID[/bold green]")
    console.print(
        "Graft builds unsigned IPAs and pushes them into LiveContainer "
        "(delivery: livecontainer). To finish setting up:"
    )
    for n, step in enumerate(FREE_SETUP_STEPS, 1):
        console.print(f"  {n}. {step}")
    return cfg_path


def _pick_device(console: Console) -> str | None:
    console.rule("Connected devices")
    devices = list_connected_devices()
    if devices:
        console.print(f"detected: {', '.join(devices)}")
        return devices[0]
    console.print("[yellow]![/yellow] no devices detected — connect your iPhone or iPad via USB and trust this Mac")
    return None


def _write(
    console: Console, cfg_path: Path, *, free_account: bool, overrides: dict[str, Any]
) -> bool:
    """Write the starter config, asking before replacing one. False if kept."""
    console.rule("Writing config")
    if cfg_path.exists():
        if not Confirm.ask(f"{cfg_path} already exists — overwrite?", default=False):
            console.print("kept existing config — edit it manually if needed")
            return False
    write_default_config(cfg_path, free_account=free_account, **overrides)
    console.print(f"[green]✓[/green] wrote {cfg_path}")
    return True

