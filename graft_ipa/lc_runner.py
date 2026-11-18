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
import hashlib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import EXIT_LIVECONTAINER, GraftError


class LiveContainerError(GraftError):
    exit_code = EXIT_LIVECONTAINER


#: What a dry run reports as its target when the device did not answer and
#: no pin names one: the push finds it there, and guessing an id would claim
#: a container the device may not have.
DRY_RUN_TARGET = "the LiveContainer found on the device"


class _ListedNoApps(Exception):
    """The device listed no apps at all, which is how a locked phone answers
    (`lc_manage.install` refuses on the same evidence).

    Deliberately not a LiveContainerError: it is no answer, not the answer
    "no LiveContainer here", and every caller already has a way of reading a
    phone that did not answer. The early check defers to the push, a dry run
    leaves the target unknown, and the push fails asking whether the phone is
    unlocked. Read as an answer, it failed a build before cyan with the steps
    for installing a LiveContainer the phone already had.
    """


@dataclass
class LCDeliveryResult:
    udid: str
    bundle_id: str
    documents_path: str  # path inside LC's sandbox, e.g. "Documents/example.ipa"
    # None only on a dry run whose IPA was never made (a dry run skips cyan).
    size_bytes: int | None
    others: list[str] = field(default_factory=list)  # other LiveContainers also installed


def push_ipa_to_livecontainer(
    ipa: Path,
    udid: str,
    *,
    lc_bundle_id: str | None = None,
    team: str | None = None,
    free_account: bool = False,
    inner_filename: str | None = None,
    ours: Mapping[str, Collection[str]] | None = None,
    archives: Iterable[Path] = (),
    dry_run: bool = False,
) -> LCDeliveryResult:
    """Push `ipa` into LiveContainer's Documents folder over usbmux.

    "over usbmux", not "over USB": `create_using_usbmux` reaches a device
    enumerated by usbmuxd, and that includes one paired over Wi-Fi. Verified
    pushing 117 MB to a phone with no cable attached.

    `lc_bundle_id` is the app's pin, None for none; `team` the configured team
    id, None for a free Apple ID. Which LiveContainer that means on this device
    is `lc_discovery.choose`'s answer, asked of the apps actually installed.
    The .ipa needs to be cryptid 0 (LC won't run encrypted binaries).

    `ours` is what Graft's deliveries recorded pushing on this device, per
    LiveContainer (`BuildLog.pushed_by_instance(udid)`). A file already at the
    target name is overwritten only when it is one of those: re-pushing our
    own earlier delivery is the normal case, but a same-named file the user
    put there is theirs, and is refused rather than replaced. None means no
    records, so any existing file is refused.

    `archives` are this app's archived builds. A same-named file with no
    record whose bytes are one of them is ours too: Graft pushed it, and the
    record is gone (a state reset, a fresh Mac with the same phone). Read only
    when an archive of its size exists, since reading it means moving the
    whole file off the phone.

    A dry run asks the device the same question and writes nothing. It names
    the container the push would choose, and raises what the push would raise
    (none installed, or several and no way to pick). A device that does not
    answer leaves it unknown: the pin if there is one, else `DRY_RUN_TARGET`,
    never an id the device may not have.
    """
    if dry_run:
        try:
            target_bid, others = asyncio.run(_choose_async(
                udid, pinned=lc_bundle_id, team=team, free_account=free_account,
            ))
        except LiveContainerError:
            raise
        except Exception:
            target_bid, others = lc_bundle_id or DRY_RUN_TARGET, []
        return LCDeliveryResult(
            udid=udid,
            bundle_id=target_bid,
            documents_path=f"Documents/{inner_filename or ipa.name}",
            size_bytes=ipa.stat().st_size if ipa.exists() else None,
            others=others,
        )

    if not ipa.exists():
        raise LiveContainerError(f"IPA to push not found: {ipa}")

    target_name = inner_filename or ipa.name
    size = ipa.stat().st_size

    try:
        target_bid, others = asyncio.run(_push_async(
            ipa, target_name, udid,
            pinned=lc_bundle_id, team=team, free_account=free_account, ours=ours or {},
            archives=list(archives),
        ))
    except LiveContainerError:
        raise
    except Exception as e:
        raise LiveContainerError(
            f"failed to push {ipa.name} into LiveContainer: {e}",
            hint=(
                "is the phone unlocked, trusted and connected (USB or Wi-Fi)? "
                f"`graft --lc-status --device {udid}` lists the LiveContainers Graft can see"
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


def check_push_target(
    udid: str,
    *,
    inner_filename: str,
    lc_bundle_id: str | None,
    team: str | None,
    free_account: bool,
    ours: Mapping[str, Collection[str]],
    archives: Iterable[Path] = (),
) -> str | None:
    """Before the build does its work, what the push would refuse at the end.

    The LiveContainer the push would choose, after checking that a file
    already at `Documents/<inner_filename>` there is one the push may
    overwrite (`_is_foreign`). Raises the push's own LiveContainerError
    (none installed, several and no pin, a stranger's file), so a build fails
    before cyan rather than after it. A phone that does not answer is None:
    it may answer by stage 5, and the push asks again. So is one that lists
    no apps (`_ListedNoApps`): a locked phone, which may be unlocked by then.
    """
    try:
        return asyncio.run(_check_async(
            udid, inner_filename, pinned=lc_bundle_id, team=team, free_account=free_account,
            ours=ours, archives=list(archives),
        ))
    except LiveContainerError:
        raise
    except Exception:
        return None


async def _check_async(
    udid: str, target_name: str, *, pinned: str | None, team: str | None,
    free_account: bool, ours: Mapping[str, Collection[str]], archives: list[Path],
) -> str:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    from .device_session import closing_best_effort

    lock = await create_using_usbmux(serial=udid)
    async with closing_best_effort(lock):
        target_bid, _ = await _resolve_lc_bundle_id(
            lock, pinned=pinned, team=team, free_account=free_account
        )
        try:
            ha = await HouseArrestService.create(lockdown=lock, bundle_id=target_bid)
        except Exception:
            return target_bid  # the push says why the sandbox would not open
        async with closing_best_effort(ha):
            if await _is_foreign(ha, target_name, target_bid, ours, archives):
                raise _foreign_file(target_bid, udid, target_name)
    return target_bid


async def _is_foreign(
    ha, target_name: str, target_bid: str, ours: Mapping[str, Collection[str]],
    archives: list[Path],
) -> bool:
    """A file is at `Documents/<target_name>` and nothing says Graft put it there.

    The evidence is a delivery record for that name in that container, or
    the file's bytes being one of this app's archived builds.
    """
    path = f"Documents/{target_name}"
    if target_name in ours.get(target_bid, ()) or not await ha.exists(path):
        return False
    return not await _is_an_archive(ha, path, archives)


async def _is_an_archive(ha, path: str, archives: list[Path]) -> bool:
    try:
        size = int((await ha.stat(path))["st_size"])
    except Exception:
        return False
    same_size = [a for a in archives if a.is_file() and a.stat().st_size == size]
    if not same_size:
        return False
    remote = hashlib.sha256(await ha.get_file_contents(path)).hexdigest()
    from .build_log import sha256_of

    return any(sha256_of(a) == remote for a in same_size)


def _foreign_file(target_bid: str, udid: str, target_name: str) -> LiveContainerError:
    path = f"Documents/{target_name}"
    return LiveContainerError(
        f"{target_bid} on {udid} already has a {path} that Graft did not "
        "push; not overwriting it",
        hint=(
            f"rename or remove {target_name} in LiveContainer's Documents "
            "(Files app, On My iPhone > LiveContainer), then try again"
        ),
    )


async def _push_async(
    ipa: Path, target_name: str, udid: str, *,
    pinned: str | None, team: str | None, free_account: bool,
    ours: Mapping[str, Collection[str]], archives: list[Path],
) -> tuple[str, list[str]]:
    # Import lazily so test runs without pymobiledevice3 don't blow up at collection.
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    lock = await create_using_usbmux(serial=udid)
    try:
        target_bid, others = await _resolve_lc_bundle_id(
            lock, pinned=pinned, team=team, free_account=free_account
        )
        try:
            ha = await HouseArrestService.create(
                lockdown=lock, bundle_id=target_bid
            )
        except Exception as e:
            # Not "set LiveContainer up": discovery just found it installed.
            raise LiveContainerError(
                f"could not open sandbox for {target_bid} on {udid}: {e}",
                hint=(
                    f"{target_bid} is installed, but iOS would not open its Documents: "
                    "unlock the phone, open LiveContainer once, and try again"
                ),
            ) from e
        try:
            path = f"Documents/{target_name}"
            # Before a single byte: set_file_contents replaces whatever is
            # there, and a same-named file nothing says Graft pushed is the
            # user's (their own copy of the app, kept in SideStore's
            # LiveContainer), not a stale push of ours.
            if await _is_foreign(ha, target_name, target_bid, ours, archives):
                raise _foreign_file(target_bid, udid, target_name)
            data = ipa.read_bytes()
            await ha.set_file_contents(path, data)
        finally:
            await ha.close()
    finally:
        await lock.close()
    return target_bid, others


async def _choose_async(
    udid: str, *, pinned: str | None, team: str | None, free_account: bool
) -> tuple[str, list[str]]:
    """`_resolve_lc_bundle_id` over a session of its own, for the dry run."""
    from pymobiledevice3.lockdown import create_using_usbmux

    from .device_session import closing_best_effort

    lock = await create_using_usbmux(serial=udid)
    async with closing_best_effort(lock):
        return await _resolve_lc_bundle_id(
            lock, pinned=pinned, team=team, free_account=free_account
        )


async def _resolve_lc_bundle_id(
    lock, *, pinned: str | None, team: str | None, free_account: bool
) -> tuple[str, list[str]]:
    """Find the installed LiveContainer to push into, plus any others present.

    Installers append a team-id suffix (`com.kdt.livecontainer.<TEAM>`), and up
    to three instances can be installed at once. `lc_discovery.choose` holds the
    selection rules, for this and for `--lc-status`; it is asked of every app
    on the device, System ones included. Returns (chosen, others).

    A listing with no apps in it raises `_ListedNoApps` rather than asking
    `choose`, whose answer to nothing is "no LiveContainer found".
    """
    from . import lc_discovery

    installed = await lc_discovery.installed_bundle_ids(lock)
    if not installed:
        raise _ListedNoApps("the device listed no apps at all, which is how a locked phone answers")
    return lc_discovery.choose(installed, pinned=pinned, team=team, free_account=free_account)
