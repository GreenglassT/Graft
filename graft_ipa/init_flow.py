"""Interactive `graft --init` setup."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from .config import (
    default_cache_dir,
    default_config_path,
    default_tweaks_dir,
    write_default_config,
)
from .preflight import list_connected_devices, parse_mobileprovision


def run_init(console: Console | None = None) -> Path:
    console = console or Console()
    console.rule("[bold]graft setup[/bold]")

    cfg_path = default_config_path()
    cfg_dir = cfg_path.parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tweaks_dir = default_tweaks_dir()
    tweaks_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"config dir: [cyan]{cfg_dir}[/cyan]")
    console.print(f"cache dir:  [cyan]{cache_dir}[/cyan]")
    console.print(f"tweaks dir: [cyan]{tweaks_dir}[/cyan]")

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
            shutil.copy2(cert_src_path, cert_dest)
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
        console.print(f"Set ${env_name} in your shell config, e.g.:")
        console.print(f"  [dim]echo 'export {env_name}=<password>' >> ~/.zshrc[/dim]")

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
            shutil.copy2(profile_src_path, profile_dest)
            console.print(f"[green]✓[/green] copied profile to {profile_dest}")
    elif profile_src_path == profile_dest and profile_dest.exists():
        console.print(f"[green]✓[/green] using existing {profile_dest}")

    team_id = "REPLACEME"
    if profile_dest.exists():
        try:
            plist = parse_mobileprovision(profile_dest)
            team_ids = plist.get("TeamIdentifier") or []
            if team_ids:
                team_id = team_ids[0]
                console.print(f"[green]✓[/green] team id from profile: [bold]{team_id}[/bold]")
        except Exception as e:
            console.print(f"[yellow]![/yellow] could not parse profile: {e}")

    # 4. devices
    console.rule("Connected devices")
    default_udid: str | None = None
    devices = list_connected_devices()
    if devices:
        console.print(f"detected: {', '.join(devices)}")
        default_udid = devices[0]
    else:
        console.print("[yellow]![/yellow] no devices detected — connect your iPhone via USB and trust this Mac")

    # 5. write config
    console.rule("Writing config")
    overrides = {
        "cert": cert_dest,
        "profile": profile_dest,
        "team_id": team_id,
    }
    if default_udid:
        overrides["default_device_udid"] = default_udid
    if cfg_path.exists():
        if not Confirm.ask(f"{cfg_path} already exists — overwrite?", default=False):
            console.print("kept existing config — edit it manually if needed")
            return cfg_path
    write_default_config(cfg_path, **overrides)
    console.print(f"[green]✓[/green] wrote {cfg_path}")
    console.rule("[bold green]Setup complete[/bold green]")
    console.print(
        "Add an app with `graft --add-app <App Store URL>` (or edit "
        f"[cyan]{cfg_path}[/cyan] — it has an example), then run `graft --check`."
    )
    return cfg_path

