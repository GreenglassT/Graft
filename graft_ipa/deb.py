"""Read a .deb's own control metadata.

Why this exists: the Repos browser shows a package's name, version and section
— straight off the repo's Packages index — and the moment you download it the
Tweaks library shows a filename and a byte count. The information was on screen
one click earlier and then thrown away, so two builds of `d-tweak.deb` a
month apart are indistinguishable in the library.

Reading the archive rather than recording what the repo said at fetch time is
deliberate. Tweaks arrive here three ways: a repo download, a Canister fetch,
and a file copied into `tweaks_dir` by hand — which is how anything built from
source gets in. Only the .deb itself is present for all three, so only the .deb
can answer for all three.

Every function here is best-effort by contract: `--tweaks` must keep listing a
library that contains a truncated download, a renamed zip, or a .deb using a
compression this Python was not built with. Failure means "no metadata", never
an exception.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

from .repo import _parse_stanza, _to_repo_package

_AR_MAGIC = b"!<arch>\n"
# ar headers are fixed-width ASCII: 16 name, 12 mtime, 6 uid, 6 gid, 8 mode,
# 10 size, 2 trailer.
_AR_HEADER = 60

# A Debian control stanza is a few hundred bytes; the largest real one is well
# under a kilobyte. These caps exist because every byte here came off a
# third-party repo: `tarfile` decompresses lazily, so a `control` entry can
# declare gigabytes and still compress to a few KB on disk. Reading it
# unbounded turns a library listing into an OOM.
_MAX_CONTROL_TAR = 4 * 1024 * 1024
_MAX_CONTROL_TEXT = 256 * 1024


def _ar_members(handle):
    """Yield `(name, payload_reader)` for each member of an ar archive.

    Takes a file object and seeks past member bodies rather than taking the
    whole file as bytes. A .deb's `data.tar` is the whole tweak — hundreds of
    megabytes for a big one — and the caller only ever wants `control.tar`,
    which by the format's own ordering comes first. Reading the file into
    memory to find it meant the size of the archive decided the memory cost of
    listing the library.

    Hand-rolled because Python ships no ar reader and the format is 60 bytes of
    fixed-width ASCII per member. Stops at the first malformed header rather
    than raising: a file that is not really a .deb should read as "no
    metadata", which is what an empty generator gives the caller.
    """
    if handle.read(len(_AR_MAGIC)) != _AR_MAGIC:
        return
    while True:
        header = handle.read(_AR_HEADER)
        if len(header) < _AR_HEADER or header[58:60] != b"`\n":
            return
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii", "replace").strip())
        except ValueError:
            return
        # `size < 0` is the one that mattered. A negative size passed the old
        # bounds check and made `pos` advance by `60 + size`, which for -60 is
        # zero — the generator looped forever on a single malformed header and
        # `--tweaks` never returned. A corrupt download was enough to make the
        # whole tweak library unreachable until the file was deleted by hand.
        if size < 0:
            return
        start = handle.tell()
        if name.startswith("control.tar") and size <= _MAX_CONTROL_TAR:
            payload = handle.read(size)
            if len(payload) < size:
                return  # truncated: the member claims more than the file holds
            yield name, payload
        # Members are padded to an even offset. Seeking rather than reading is
        # what keeps `data.tar` from ever entering memory.
        handle.seek(start + size + (size % 2))


def _strip_leading_dot_slash(name: str) -> str:
    """`./control` and `control` are both seen in the wild; nothing else is.

    Deliberately not `lstrip("./")`, which strips a character *set* — it maps
    `.../..//control` and `././control` to `control` too, so a crafted archive
    could put its stanza somewhere that is not the control member and have it
    read as authoritative.
    """
    return name[2:] if name.startswith("./") else name


def _control_text(payload: bytes, member: str) -> str | None:
    """The `control` file's text out of a control.tar.<compression> member.

    `tarfile` picks the decompressor from the stream itself, so gz/xz/bz2 need
    no branching. zstd is the exception: it only became a `tarfile` mode in
    Python 3.14, and a .deb built with it on an older interpreter is simply one
    this function cannot read — which is a missing name in a list, not an error.
    """
    mode = "r:zst" if member.endswith(".zst") else "r:*"
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as tar:
            for entry in tar:
                if _strip_leading_dot_slash(entry.name) != "control" or not entry.isfile():
                    continue
                # `entry.size` is the load-bearing check — `tarfile` bounds an
                # extracted member to it, so this is what a decompression bomb
                # actually runs into. The `read(n)` below is belt-and-braces
                # against that bound changing; no test can reach it today, and
                # it is cheap enough to keep anyway.
                if entry.size > _MAX_CONTROL_TEXT:
                    return None
                extracted = tar.extractfile(entry)
                if extracted is None:
                    return None
                return extracted.read(_MAX_CONTROL_TEXT).decode("utf-8", "replace")
    except Exception:
        return None
    return None


def control_fields(path: Path) -> dict[str, Any]:
    """The subset of `path`'s control stanza that the Tweaks library shows.

    Returns `{}` for anything unreadable — a directory, a truncated download, a
    .deb whose control member uses a compression this interpreter lacks. The
    caller adds these keys to a record it would have emitted anyway, so an empty
    dict degrades to exactly the old behaviour.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return {}

    with handle:
        # `next`, not a `for`: the old loop returned unconditionally on its
        # first iteration, so it never looped — and saying so explicitly also
        # closes the generator, which is what stops it seeking past `data.tar`
        # for an answer nobody wants.
        member = next(_ar_members(handle), None)
        if member is None:
            return {}
        name, payload = member
        text = _control_text(payload, name)
    if text is None:
        return {}

    # Through `repo._to_repo_package`, the same mapping the Repos browser's
    # rows come from. Re-deriving it here had already lost a fallback: that
    # function reads `Author or Maintainer`, and a .deb declaring only
    # `Maintainer:` showed an author in the browser and a blank one in the
    # library — reopening, one field over, the exact gap this module closed.
    package = _to_repo_package(_parse_stanza(text))
    if package is None:
        return {}
    fields = {
        # The `Package:` identifier, not the display name. Everything that can
        # answer "is there a newer version of this" — a repo's Packages index,
        # Canister — is keyed by it, and it was the one field this function
        # parsed and then threw away, which made the library's own metadata
        # unable to be compared against anything.
        "package_id": package.package_id,
        "name": package.name,
        "version": package.version,
        "section": package.section,
        "author": package.author,
    }
    return {key: value for key, value in fields.items() if value}
