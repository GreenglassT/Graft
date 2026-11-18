"""Interactive `graft --setup-portal` wizard.

Walks the user through:
  1. Where to create an App Store Connect API key (link + role guidance).
  2. Move the .p8 file into ~/.config/graft/.
  3. Record key_id, issuer_id, and the path into config.yaml.
  4. Sanity-check by listing devices via the API to confirm credentials work.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from . import apple_portal
from .config import default_config_path, load_config, set_app_store_connect
from .errors import GraftError


def run_setup_portal(console: Console | None = None) -> None:
    console = console or Console()
    console.rule("App Store Connect API key setup")

    cfg_path = default_config_path()
    if not cfg_path.exists():
        raise GraftError(
            f"config not found at {cfg_path}",
            hint="run `graft --init` first",
        )

    console.print(
        "Open [bold]https://appstoreconnect.apple.com/access/integrations/api[/bold] "
        "and create a new key with role [bold]Developer[/bold] (or higher). "
        "Download the .p8 file. You will only get the chance to download it once."
    )
    console.print(
        "From the same page, copy the [bold]Key ID[/bold] (10 chars) and the "
        "[bold]Issuer ID[/bold] (UUID at the top of the page)."
    )

    key_id = Prompt.ask("Key ID").strip()
    issuer_id = Prompt.ask("Issuer ID").strip()

    p8_src = Prompt.ask(
        "Path to the downloaded .p8 file",
        default=str(Path.home() / "Downloads" / f"AuthKey_{key_id}.p8"),
    )
    p8_src_path = Path(p8_src).expanduser()
    p8_dest = cfg_path.parent / f"AuthKey_{key_id}.p8"

    if p8_src_path.exists() and p8_src_path != p8_dest:
        if Confirm.ask(f"Copy {p8_src_path} -> {p8_dest}?", default=True):
            shutil.copy2(p8_src_path, p8_dest)
            p8_dest.chmod(0o600)
            console.print(f"[green]copied[/green] .p8 to {p8_dest}")
    elif p8_src_path == p8_dest and p8_dest.exists():
        p8_dest.chmod(0o600)
    elif not p8_dest.exists():
        raise GraftError(
            f".p8 file not found at {p8_src_path}",
            hint="re-download from App Store Connect and re-run --setup-portal",
        )

    creds = apple_portal.PortalCredentials(
        key_id=key_id, issuer_id=issuer_id, key_path=p8_dest
    )
    client = apple_portal.ASCClient(creds)

    console.print("[dim]sanity-check: listing devices via ASC API...[/dim]")
    try:
        devices = client.list_devices()
    except apple_portal.PortalError as e:
        console.print(f"[red]auth check failed:[/red] {e.message}")
        if e.hint:
            console.print(f"  hint: {e.hint}")
        return
    console.print(
        f"[green]ok[/green] auth works. ASC sees [bold]{len(devices)}[/bold] iOS device(s):"
    )
    for d in devices[:5]:
        console.print(f"  {d.udid}  {d.name}  ({d.status})")

    # Merge into config.yaml.
    # Through config's own writer: atomic, and under the config lock, so it
    # cannot erase a change the GUI makes at the same moment.
    set_app_store_connect(cfg_path, {
        "key_id": key_id,
        "issuer_id": issuer_id,
        "key_file": str(p8_dest),
        "device_udids": [d.udid for d in devices if d.status == "ENABLED"][:3],
    })
    console.print(f"[green]wrote[/green] app_store_connect to {cfg_path}")

    console.print(
        "\nTo turn on auto-provisioning for an app, add "
        "[bold]auto_profile: true[/bold] under that app in the config. "
        "The first build will register a wildcard App ID + profile in your portal "
        "and cache it; subsequent builds reuse the cache."
    )

    # Verify it parses.
    try:
        load_config(cfg_path)
    except GraftError as e:
        raise GraftError(
            f"config no longer parses after edit: {e.message}",
            hint="restore from backup or run --init",
        ) from e
