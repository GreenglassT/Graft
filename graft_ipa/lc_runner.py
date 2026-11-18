"""LiveContainer delivery: push a tweaked .ipa into LiveContainer's sandbox.

Replaces the zsign + ideviceinstaller install steps when an app has
`delivery: livecontainer` set. The host LiveContainer.app is signed once with
the user's dev cert; inner apps run unsigned inside LC's dyld-managed
environment, so they don't need their own provisioning profile and they don't
hit the OAuth/anti-tamper failures that re-signing causes.

The pipeline becomes:
    download IPA -> verify cryptid 0 -> cyan inject -> push into LC -> done

The user then opens LiveContainer and taps the new app to register it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .errors import GraftError


class LiveContainerError(GraftError):
    exit_code = 11


# Default upstream bundle id. AltStore-installed LC has team-id suffix
# appended (com.kdt.livecontainer.<TEAM>), so the actual bundle id can be
# overridden per-config or auto-detected.
LC_BUNDLE_ID = "com.kdt.livecontainer"


@dataclass
class LCDeliveryResult:
    udid: str
    bundle_id: str
    documents_path: str  # path inside LC's sandbox, e.g. "Documents/example.ipa"
    size_bytes: int
    others: list[str] = field(default_factory=list)  # other LiveContainers also installed


def _choose_lc_bundle_id(installed: list[str], preferred: str) -> tuple[str, list[str]]:
    """Pick which LiveContainer to push into, and report any others.

    Multiple LiveContainers can be installed side by side (the app shipped as
    `com.kdt.livecontainer`, then `com.kdt.LiveContainer2`, `com.kdt.LiveContainer3`,
    each with a team-id suffix). Pushing into the wrong one means the IPA silently
    never appears where the user imports — so we surface every LiveContainer found
    and refuse to guess when the preferred id matches none of them.

    Returns (chosen_bundle_id, other_livecontainer_bundle_ids).
    """
    family = sorted({b for b in installed if b.lower().startswith("com.kdt.livecontainer")})
    if preferred in installed:
        chosen = preferred
    elif not family:
        raise LiveContainerError(
            f"no installed app matches LiveContainer (looked for '{preferred}')",
            hint="install LiveContainer first (via SideStore/AltStore).",
        )
    else:
        # The preferred prefix (default: the original lowercase id) uniquely names one.
        pref_matches = sorted(
            {b for b in family if b == preferred or b.startswith(preferred + ".")}
        )
        if len(pref_matches) == 1:
            chosen = pref_matches[0]
        elif len(family) == 1:
            chosen = family[0]
        else:
            raise LiveContainerError(
                f"multiple LiveContainers installed and none matches '{preferred}': "
                f"{', '.join(family)}",
                hint="set `lc_bundle_id: <exact id>` on this app in config.yaml.",
            )
    return chosen, [b for b in family if b != chosen]


def push_ipa_to_livecontainer(
    ipa: Path,
    udid: str,
    *,
    lc_bundle_id: str = LC_BUNDLE_ID,
    inner_filename: str | None = None,
    dry_run: bool = False,
) -> LCDeliveryResult:
    """Push `ipa` into LiveContainer's Documents folder over usbmux.

    "over usbmux", not "over USB": `create_using_usbmux` reaches a device
    enumerated by usbmuxd, and that includes one paired over Wi-Fi. Verified
    pushing 117 MB to a phone with no cable attached.

    The phone must be connected, LiveContainer must be installed (com.kdt.livecontainer
    by default), and the .ipa needs to be cryptid 0 (LC won't run encrypted binaries).
    """
    if dry_run:
        return LCDeliveryResult(
            udid=udid,
            bundle_id=lc_bundle_id,
            documents_path=f"Documents/{inner_filename or ipa.name}",
            size_bytes=ipa.stat().st_size if ipa.exists() else 0,
        )

    if not ipa.exists():
        raise LiveContainerError(f"IPA to push not found: {ipa}")

    target_name = inner_filename or ipa.name
    size = ipa.stat().st_size

    try:
        target_bid, others = asyncio.run(_push_async(ipa, target_name, udid, lc_bundle_id))
    except LiveContainerError:
        raise
    except Exception as e:
        raise LiveContainerError(
            f"failed to push {ipa.name} into LiveContainer: {e}",
            hint=(
                "is LiveContainer installed (com.kdt.livecontainer) and is the phone "
                "unlocked + plugged in? run `pymobiledevice3 apps list` to confirm"
            ),
        ) from e

    return LCDeliveryResult(
        udid=udid,
        bundle_id=target_bid,
        documents_path=f"Documents/{target_name}",
        size_bytes=size,
        others=others,
    )


def list_livecontainer_documents(*, udid: str, lc_bundle_id: str) -> list[str]:
    """Filenames in a LiveContainer's Documents/ folder.

    The same HouseArrest service the push uses, asked the mirror question:
    push writes `Documents/<name>`, this reports which names are there now.

    Raises on an unreachable device, a LiveContainer that isn't installed, or a
    sandbox that won't open. Callers must read that as "couldn't tell", never
    as "the file is gone" — a demote has to rest on evidence, and a device that
    didn't answer is not evidence.

    Note what a present filename does and does not prove: that the file is in
    Documents/, and nothing more. Whether LiveContainer ever imported it lives
    in that app's private container, which this tool deliberately does not read
    (spec §3).
    """
    return asyncio.run(_listdir_async(udid, lc_bundle_id))


async def _listdir_async(udid: str, lc_bundle_id: str) -> list[str]:
    # Imported lazily for the same reason as `_push_async`: a test run without
    # pymobiledevice3 must not blow up at collection.
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    lock = await create_using_usbmux(serial=udid)
    try:
        ha = await HouseArrestService.create(lockdown=lock, bundle_id=lc_bundle_id)
        try:
            # `listdir` returns bare filenames on the installed version (it
            # strips '.' and '..' itself). `.name` keeps this honest if a
            # future one returns full paths, since the caller compares against
            # the bare `remote_name` the push recorded.
            return [Path(p).name for p in await ha.listdir("Documents")]
        finally:
            await ha.close()
    finally:
        await lock.close()


async def _push_async(
    ipa: Path, target_name: str, udid: str, lc_bundle_id: str
) -> tuple[str, list[str]]:
    # Import lazily so test runs without pymobiledevice3 don't blow up at collection.
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    lock = await create_using_usbmux(serial=udid)
    try:
        target_bid, others = await _resolve_lc_bundle_id(lock, lc_bundle_id)
        try:
            ha = await HouseArrestService.create(
                lockdown=lock, bundle_id=target_bid
            )
        except Exception as e:
            raise LiveContainerError(
                f"could not open sandbox for {target_bid} on {udid}: {e}",
                hint=(
                    "install LiveContainer first: `graft --lc-install --device <udid>` "
                    "(or through AltStore/SideStore)"
                ),
            ) from e
        try:
            data = ipa.read_bytes()
            await ha.set_file_contents(f"Documents/{target_name}", data)
        finally:
            await ha.close()
    finally:
        await lock.close()
    return target_bid, others


async def _resolve_lc_bundle_id(lock, preferred: str) -> tuple[str, list[str]]:
    """Find the installed LiveContainer to push into, plus any others present.

    SideStore/AltStore append a team-id suffix (`com.kdt.livecontainer.<TEAM>`), and
    multiple LiveContainer generations can be installed at once. See
    `_choose_lc_bundle_id` for the selection rules. Returns (chosen, others).
    """
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    ipx = InstallationProxyService(lockdown=lock)
    apps = await ipx.get_apps()
    return _choose_lc_bundle_id(list(apps), preferred)
