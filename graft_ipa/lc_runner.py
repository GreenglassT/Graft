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

import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import device_session
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
    when an archive of its size exists, and then only its end
    (`_is_an_archive`).

    A dry run asks the device the same question and writes nothing. It names
    the container the push would choose, and raises what the push would raise
    (none installed, or several and no way to pick). A device that does not
    answer leaves it unknown: the pin if there is one, else `DRY_RUN_TARGET`,
    never an id the device may not have.
    """
    if dry_run:
        try:
            target_bid, others = device_session.run(_choose_async(
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
        target_bid, others = device_session.run(_push_async(
            ipa, target_name, udid,
            pinned=lc_bundle_id, team=team, free_account=free_account, ours=ours or {},
            archives=list(archives),
        ))
    except LiveContainerError:
        raise
    except Exception as e:
        from .installer import connection_hint

        raise LiveContainerError(
            f"failed to push {ipa.name} into LiveContainer: {e}",
            hint=connection_hint(f"{type(e).__name__}: {e}") or (
                "is the device unlocked, trusted and connected (USB or Wi-Fi)? "
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
    return device_session.run(_listdir_async(udid, lc_bundle_id))


async def _listdir_async(udid: str, lc_bundle_id: str) -> list[str]:
    # Imported lazily for the same reason as `_push_async`: a test run without
    # pymobiledevice3 must not blow up at collection.
    from pymobiledevice3.services.house_arrest import HouseArrestService

    from .device_install import _answered
    from .device_session import open_lockdown

    lock = await open_lockdown(udid)
    try:
        ha = await _answered(HouseArrestService.create(lockdown=lock, bundle_id=lc_bundle_id),
                             "when Graft opened LiveContainer's Documents")
        try:
            # `listdir` returns bare filenames on the installed version (it
            # strips '.' and '..' itself). `.name` keeps this honest if a
            # future one returns full paths, since the caller compares against
            # the bare `remote_name` the push recorded.
            return [Path(p).name for p in await _answered(ha.listdir("Documents"),
                                                          "listing LiveContainer's Documents")]
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
        return device_session.run(_check_async(
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
    from pymobiledevice3.services.house_arrest import HouseArrestService

    from .device_install import _answered
    from .device_session import closing_best_effort, open_lockdown

    # A build's first session with the phone, before cyan: where a phone that
    # has never trusted this Mac is asked to (`open_lockdown`).
    lock = await open_lockdown(udid)
    async with closing_best_effort(lock):
        target_bid, _ = await _resolve_lc_bundle_id(
            lock, pinned=pinned, team=team, free_account=free_account
        )
        try:
            ha = await _answered(HouseArrestService.create(lockdown=lock, bundle_id=target_bid),
                                 "when Graft opened LiveContainer's Documents")
        except Exception:
            return target_bid  # the push says why the sandbox would not open
        async with closing_best_effort(ha):
            if await _is_foreign(ha, target_name, target_bid, ours, archives):
                raise _foreign_file(target_bid, udid, target_name, lock)
    return target_bid


async def _is_foreign(
    ha, target_name: str, target_bid: str, ours: Mapping[str, Collection[str]],
    archives: list[Path],
) -> bool:
    """A file is at `Documents/<target_name>` and nothing says Graft put it there.

    The evidence is a delivery record for that name in that container, or
    the file's bytes being one of this app's archived builds.
    """
    from .device_install import _answered

    path = f"Documents/{target_name}"
    if target_name in ours.get(target_bid, ()) or not await _answered(
        ha.exists(path), "when Graft looked for a file of the same name"
    ):
        return False
    return not await _is_an_archive(ha, path, archives)


#: The most `_is_an_archive` reads of a file on the phone: its end, where a
#: ZIP keeps its central directory. About 6,000 members' entries; an app with
#: more still has its last few thousand, and every byte after them, compared.
ARCHIVE_TAIL_BYTES = 1024 * 1024
#: Without `AfcService.fseek`, how far `_read_end` reads forward, and throws
#: away, to reach the end. fseek came after 10.4.0, the oldest pymobiledevice3
#: pyproject.toml allows (combined fix check #5): 10.4.0 has only the
#: FILE_SEEK opcode, and 11.58.0, 11.59.0 and 11.61.x have the method. Beyond
#: this the file can't be told from a stranger's, which is refused, the safe
#: way to be wrong; at USB speed this is seconds.
ARCHIVE_SKIP_LIMIT = 256 * 1024 * 1024
#: One read while skipping: AFC's own largest (`MAXIMUM_READ_SIZE`).
ARCHIVE_SKIP_CHUNK = 4 * 1024 * 1024


async def _is_an_archive(ha, path: str, archives: list[Path]) -> bool:
    """Whether the file at `path` is one of `archives`, without reading it all.

    Its size first, from `stat`, then its end: the last `ARCHIVE_TAIL_BYTES`
    (all of a smaller file), through one open, a seek, one read of that
    many bytes and a close, compared with the same bytes of each archive of
    that size. Where pymobiledevice3 has no seek, the bytes before the end
    are read and thrown away, up to `ARCHIVE_SKIP_LIMIT` (`_read_end`). An IPA is a ZIP, and a ZIP ends with its central directory
    and the end-of-central-directory record: every member's name, sizes,
    CRC-32 and offset. Two IPAs of the same size whose ends match hold the
    same members with the same CRC-32s.

    It used to hash the whole file, through `get_file_contents`: on the
    early check and again on the push, a 2 GB game read in full off the
    phone and held in graft's memory, which pymobiledevice3's `data +=
    chunk` reads made several times that (re-check #13).

    A read that fails is raised, as a phone that stopped answering, not
    taken for a stranger's file. So is a `stat` that fails for any reason
    but "there is no such file" (the whole-diff review's cli-6): read as
    "not ours", it refused Graft's own earlier push as a stranger's. Each
    read has the silence limit (`device_install._answered`, cli-7).
    """
    from .device_install import _answered

    try:
        info = await _answered(ha.stat(path), "when Graft read the size of a file of the same name")
    except Exception as e:
        if _not_found(e):
            return False
        raise
    try:
        size = int(info["st_size"])
    except (KeyError, TypeError, ValueError):
        return False  # an answer with no size: can't be one of ours
    same_size = [a for a in archives if a.is_file() and a.stat().st_size == size]
    if not same_size:
        return False
    length = min(size, ARCHIVE_TAIL_BYTES)
    end = await _read_end(ha, path, size - length, length)
    return end is not None and any(_end_of(a, length) == end for a in same_size)


async def _read_end(ha, path: str, offset: int, length: int) -> bytes | None:
    """`length` bytes of `path` from `offset`, or None when they can't be
    reached without fseek inside `ARCHIVE_SKIP_LIMIT`, or the file ends
    before them."""
    from .device_install import _answered
    from .device_session import Teardown

    reading = "while Graft read a file of the same name"
    handle = await _answered(ha.fopen(path, "r"), reading)
    try:
        seek = getattr(ha, "fseek", None)
        if seek is not None:
            await _answered(seek(handle, offset), reading)
        else:
            if offset > ARCHIVE_SKIP_LIMIT:
                return None
            left = offset
            while left > 0:
                got = await _answered(ha.fread(handle, min(ARCHIVE_SKIP_CHUNK, left)), reading)
                if not got:
                    return None
                left -= len(got)
        return await _answered(ha.fread(handle, length), reading)
    finally:
        await Teardown()(ha.fclose(handle))


def _not_found(e: BaseException) -> bool:
    """Whether AFC's `e` says the file isn't there: `AfcFileNotFoundError`
    (pymobiledevice3 11.x), or the status codes it means it by (10.4.0
    raised a bare `AfcException`; `stat` answers READ_ERROR)."""
    from pymobiledevice3 import exceptions
    from pymobiledevice3.services.afc import AfcError

    missing = getattr(exceptions, "AfcFileNotFoundError", None)
    if missing is not None and isinstance(e, missing):
        return True
    return isinstance(e, exceptions.AfcException) and getattr(e, "status", None) in (
        AfcError.OBJECT_NOT_FOUND, AfcError.READ_ERROR,
    )


def _end_of(archive: Path, length: int) -> bytes:
    with archive.open("rb") as f:
        f.seek(-length, os.SEEK_END)
        return f.read(length)


def _foreign_file(target_bid: str, udid: str, target_name: str, lock=None) -> LiveContainerError:
    """The refusal of a file at `Documents/<target_name>` Graft didn't put
    there, with the Files app's folder for it on that kind of device ("On My
    iPad"), read from `lock`, the session the push has open; without one,
    "On My iPhone or iPad"."""
    from .devices import family_from_lockdown

    path = f"Documents/{target_name}"
    kind = family_from_lockdown(lock) or "iPhone or iPad"
    return LiveContainerError(
        f"{target_bid} on {udid} already has a {path} that Graft did not "
        "push; not overwriting it",
        hint=(
            f"rename or remove {target_name} in LiveContainer's Documents "
            f"(Files app, On My {kind} > LiveContainer), then try again"
        ),
    )


async def _push_async(
    ipa: Path, target_name: str, udid: str, *,
    pinned: str | None, team: str | None, free_account: bool,
    ours: Mapping[str, Collection[str]], archives: list[Path],
) -> tuple[str, list[str]]:
    # Import lazily so test runs without pymobiledevice3 don't blow up at collection.
    from pymobiledevice3.services.house_arrest import HouseArrestService

    from .device_install import _answered
    from .device_session import Teardown, closing_best_effort, open_lockdown

    # Every clean-up wait below on one budget, the partial's removal and the
    # two closes, as in the sideload install (`device_install._install`): a
    # phone that has stopped answering must not hold a Stop past the app's
    # grace, and a second Stop ends all of it.
    teardown = Teardown()
    lock = await open_lockdown(udid)
    async with closing_best_effort(lock, within=teardown):
        target_bid, others = await _resolve_lc_bundle_id(
            lock, pinned=pinned, team=team, free_account=free_account
        )
        try:
            ha = await _answered(HouseArrestService.create(lockdown=lock, bundle_id=target_bid),
                                 "when Graft opened LiveContainer's Documents")
        except Exception as e:
            from .installer import connection_hint

            # Not "set LiveContainer up": discovery just found it installed. A
            # connection that dropped here (a cable pulled as the vend runs)
            # gets the hint every other step gives for that, not this one.
            raise LiveContainerError(
                f"could not open sandbox for {target_bid} on {udid}: {e}",
                hint=connection_hint(f"{type(e).__name__}: {e}") or (
                    f"{target_bid} is installed, but iOS would not open its Documents: "
                    "unlock the device, open LiveContainer once, and try again"
                ),
            ) from e
        async with closing_best_effort(ha, within=teardown):
            await _sweep_partials(ha)
            path = f"Documents/{target_name}"
            # Before a single byte: set_file_contents replaces whatever is
            # there, and a same-named file nothing says Graft pushed is the
            # user's (their own copy of the app, kept in SideStore's
            # LiveContainer), not a stale push of ours.
            if await _is_foreign(ha, target_name, target_bid, ours, archives):
                raise _foreign_file(target_bid, udid, target_name, lock)
            await _write_into_place(ha, ipa, path, teardown)
    return target_bid, others


#: What `partial_path` names a push's file until it is whole: the name it is
#: for, this Mac (`device_install.this_mac`) and this process.
_PARTIAL_NAME = re.compile(r"\..+\.([0-9a-f]{12})-(\d+)\.graft-partial")


def partial_path(path: str) -> str:
    """Where a push to `path` writes until the file is whole: hidden, and not
    an `.ipa`, so neither LiveContainer nor the Files app lists it. Named
    for this Mac and this process, so a later push or `--lc-clean` can tell
    one nothing is writing any more (`is_abandoned_partial`)."""
    from .device_install import this_mac

    folder, _, name = path.rpartition("/")
    return f"{folder}/.{name}.{this_mac()}-{os.getpid()}.graft-partial"


def is_abandoned_partial(name: str) -> bool:
    """Whether `name`, in a LiveContainer's Documents, is a push's partial
    file that nothing is writing any more (re-check D4).

    A push whose connection drops cannot remove its partial, and the Files
    app does not show a hidden name, so the user cannot either: up to a
    whole IPA of the phone's storage. The next push sweeps such files
    (`_sweep_partials`), and so does `--lc-clean` (`lc_manage.clean`).

    Only a partial named by a push from this Mac whose graft process is no
    longer running (`device_install.left_behind`, the rule for staged IPAs
    too). A pid says whether a write can still be going on, and only on the
    Mac that wrote it: another Mac's partial is that Mac's to sweep. A
    timestamp would compare the phone's clock with this Mac's, and a live
    push may leave its file untouched for `SILENCE_SECONDS` while it waits
    on the phone, so any age short enough to help would sometimes remove a
    push in flight. A partial named before names carried the Mac
    (`.<app>.ipa.graft-partial`) is anyone's, and is left.
    """
    from .device_install import left_behind

    return left_behind(name, _PARTIAL_NAME)


async def _sweep_partials(ha) -> None:
    """Remove the abandoned partials (`is_abandoned_partial`) in Documents.

    Best effort, before a push writes anything: one left there is clutter,
    and must not stop the push. A Stop still ends it, and so does a phone
    gone quiet (`device_install._answered`, cli-7): that is no clutter, and
    the push would only wait on it again.
    """
    from .device_install import PhoneWentSilentError, _answered

    sweeping = "while Graft swept what earlier pushes left"
    try:
        for name in await _answered(ha.listdir("Documents"), sweeping):
            if is_abandoned_partial(name):
                await _answered(ha.rm_single(f"Documents/{name}", force=True), sweeping)
    except PhoneWentSilentError:
        raise
    except Exception:
        pass


async def _write_into_place(ha, ipa: Path, path: str, teardown=None) -> None:
    """Put `ipa` at `path` in the sandbox, whole or not at all.

    Streamed, a chunk at a time (`device_install.upload`), not
    `set_file_contents(ipa.read_bytes())`: that held the whole IPA in graft's
    memory, about 4x its size on pymobiledevice3 10.x, and a game is
    gigabytes (review #13).

    Written under `partial_path` and renamed into place once complete: a
    push stopped or dropped half-way used to leave a truncated `<app>.ipa` at
    the real name, which no delivery record covers and no archive matches, so
    every later push refused it as the user's (`_is_foreign`) until they
    deleted it by hand (review #14). Now a failed push removes its partial
    file (within `Teardown`'s budget), and one that could not, its
    connection gone, leaves only a hidden name that the next push from this
    Mac, or `--lc-clean`, removes (`is_abandoned_partial`).

    The old file, once the upload is whole, is moved aside rather than
    removed (AFC may or may not let a rename replace a file), the new one
    renamed in, and only then the old one removed. A rename that fails puts
    the old one back and keeps the whole upload, hidden, for the next push
    or `--lc-clean` to sweep: removing the old one first, then the partial
    on the failure, left neither (the whole-diff review's cli-5). The aside
    is named as a partial is, so one that couldn't be put back is swept
    too. Only reached once `_is_foreign` has said the push may overwrite
    the old file. Each of these calls has the silence limit
    (`device_install._answered`, cli-7).

    `teardown` is the push's clean-up budget (`Teardown`), a new one if none
    is given.
    """
    from .device_install import _answered, upload
    from .device_session import Teardown

    teardown = teardown or Teardown()
    partial = partial_path(path)
    try:
        await upload(ha, ipa, partial, teardown)
    except BaseException:
        await teardown(ha.rm_single(partial, force=True))
        raise
    finishing = "as the upload was put in place"
    aside = partial_path(f"{path}.previous")
    there = await _answered(ha.exists(path), finishing)
    try:
        # The move-aside inside the try too: the phone can apply it and its
        # answer never arrive (it goes silent, or a Stop lands while Graft
        # waits). Nothing was then at `path`, the old file was hidden under
        # a name the next push's sweep removes, and nothing put it back (the
        # try14 delta audit's d14-cli-2). Putting it back when it was never
        # moved fails harmlessly: there is nothing at `aside`.
        if there:
            await _answered(ha.rename(path, aside), finishing)
        await _answered(ha.rename(partial, path), finishing)
    except BaseException:
        if there:
            await teardown(ha.rename(aside, path))
        raise
    if there:
        await teardown(ha.rm_single(aside, force=True))


async def _choose_async(
    udid: str, *, pinned: str | None, team: str | None, free_account: bool
) -> tuple[str, list[str]]:
    """`_resolve_lc_bundle_id` over a session of its own, for the dry run."""
    from .device_session import closing_best_effort, open_lockdown

    lock = await open_lockdown(udid)
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
    from .device_install import _answered

    installed = await _answered(lc_discovery.installed_bundle_ids(lock),
                                "when Graft looked for LiveContainer")
    if not installed:
        raise _ListedNoApps("the device listed no apps at all, which is how a locked device answers")
    return lc_discovery.choose(installed, pinned=pinned, team=team, free_account=free_account)
