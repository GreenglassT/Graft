#!/usr/bin/env python3
"""The release's check for names that must never be public: the apps,
tweaks and bundle ids Graft was used with, its owner, real device UDIDs.

    .venv/bin/python scripts/denylist.py check
    .venv/bin/python scripts/denylist.py tracked <checkout> <commit>
    .venv/bin/python scripts/denylist.py sources <Graft-<version>-sources.tar.gz>
    .venv/bin/python scripts/denylist.py app <Graft.app>
    .venv/bin/python scripts/denylist.py scan <file, archive or folder>...

`check` says whether the list can be read; `tracked` scans the files a
public export of <commit> carries (graft_bundle.SOURCE_EXCLUDES left out, as
from every export); `sources` and `app` scan the source archive and the app
as the release does (below); `scan` scans everything in files, archives
(and the archives inside them) and folders. graft-release runs the first
three, and verify-bundle scans the app.

Every name is scanned, and every file of Graft's own. Others' code and texts
(the pinned downloads, each checked by its SHA-256, and what is built from
them) are scanned by name only: their words are upstream's, popular apps'
names turn up in them as ordinary words (13 of the list's terms in just
three of the source archive's third-party archives), and nothing in them
comes from this machine. In the app, not even their names: they keep
upstream's names there (a Go module's folder, a lexer named for a game),
and so do the license files written about them. See APP_THIRD_PARTY,
app_names_scanned and SOURCES_THIRD_PARTY.

A real iOS app's name sat in public code comments from 1.0 to 2.0, because
the scans then looked only for the release machine's paths and username.

The terms come from the private scrub list the wiki build reads too
(scripts/build-wiki): ~/OpenSource/release-notes/scrub-denylist.txt, or the
file GRAFT_SCRUB_DENYLIST names. Never from this repository: a public list
of what must not be public gives every term away, and hashes of terms
wouldn't hide them, since hashing every App Store app's name takes an
afternoon. Each term is matched as the wiki build matches it: literally,
case-insensitively, its words separated by any run of whitespace, with no
letter or digit just before or after it. Lines starting with `#` are
comments. One kind of comment means something here and nothing to the wiki
build: `#release-allow: <term>` lets the release carry a listed term (a
component the app has to name, say) while the wiki stays scrubbed of it.

A list that can't be read is a failure, never a pass. GRAFT_SKIP_DENYLIST=1
skips the scans, and every one says so loudly.

Nothing this prints holds a term, only where it was found and the list's
line number, so its output can be pasted anywhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import itertools
import os
import re
import struct
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

PATH_ENV = "GRAFT_SCRUB_DENYLIST"
SKIP_ENV = "GRAFT_SKIP_DENYLIST"
#: The comment that lets the release carry a listed term.
_ALLOW = re.compile(r"#\s*release-allow:\s*(.*)")


def default_path() -> Path:
    """The scrub list the wiki build reads: one list, not two."""
    return Path.home() / "OpenSource" / "release-notes" / "scrub-denylist.txt"


def list_path() -> Path:
    value = os.environ.get(PATH_ENV)
    return Path(value).expanduser() if value else default_path()


def skipped() -> bool:
    return os.environ.get(SKIP_ENV) == "1"


def skip_warning() -> str:
    return (f"WARNING: {SKIP_ENV}=1, so the deny-list scan is SKIPPED: nothing checks this for the names of "
            "apps, tweaks, bundle ids or the owner. Don't publish it unchecked.")


class DenylistError(Exception):
    """The list can't be used. The message says why, never with a term in it."""


def _normal(term: str) -> str:
    return " ".join(term.split()).lower()


def _body(term: str) -> bytes:
    """A term as the wiki build's pattern (scripts/wikigen/scrub.py), for
    lowercased bytes: ASCII letters fold, as bytes.lower() folds them."""
    return rb"\s+".join(re.escape(word.encode().lower()) for word in term.split())


def _bounded(body: bytes) -> re.Pattern[bytes]:
    return re.compile(rb"(?<![a-z0-9])" + body + rb"(?![a-z0-9])")


@dataclass(frozen=True)
class Denylist:
    path: Path
    #: (line number in the list, its term's pattern) for each term scanned.
    entries: tuple[tuple[int, re.Pattern[bytes]], ...]
    #: The line numbers of terms a `#release-allow:` comment exempts.
    allowed: tuple[int, ...]
    #: Any of the entries: one pass finds whether a text holds a term at
    #: all, and each entry's own pattern runs only where one does. Hits are
    #: rare, and the app and the source archive are hundreds of MB.
    any_term: re.Pattern[bytes] | None

    def lines_in(self, data: bytes, *, inflate: bool = True) -> list[int]:
        """The line numbers of the terms `data` holds, in its bytes or (with
        `inflate`) inside a zlib stream, as verify-bundle's scan for the
        release machine's paths looks. A Mach-O's CMS signature, which names
        the Developer ID it was signed with, is left out (signature_cms_ranges)."""
        if self.any_term is None:
            return []
        data = without_signatures(data)
        found: set[int] = set()
        for text in itertools.chain((data,), inflated_streams(data) if inflate else ()):
            low = text.lower()
            if self.any_term.search(low):
                found.update(line for line, p in self.entries if line not in found and p.search(low))
        return sorted(found)


def load(path: Path | None = None) -> Denylist:
    """The list at `path` (list_path() unless given). Raises DenylistError
    when it is missing, unreadable or holds no term."""
    path = path or list_path()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise DenylistError(f"no deny-list at {path}") from None
    except UnicodeDecodeError as e:
        raise DenylistError(f"the deny-list at {path} isn't UTF-8 (byte {e.start})") from None
    except OSError as e:
        raise DenylistError(f"can't read the deny-list at {path}: {e.strerror}") from None
    allow, terms = set(), []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if m := _ALLOW.fullmatch(line):
            if m[1].strip():
                allow.add(_normal(m[1]))
        elif line and not line.startswith("#"):
            terms.append((number, line))
    if not terms:
        raise DenylistError(f"the deny-list at {path} holds no term")
    scanned = [(n, t) for n, t in terms if _normal(t) not in allow]
    bodies = sorted({_body(t) for _, t in scanned}, key=len, reverse=True)
    return Denylist(path=path, entries=tuple((n, _bounded(_body(t))) for n, t in scanned),
                    allowed=tuple(n for n, t in terms if _normal(t) in allow),
                    any_term=_bounded(rb"(?:" + rb"|".join(bodies) + rb")") if bodies else None)


# ------------------------------------------------------------ zlib streams

#: A zlib stream's two-byte header: CMF 0x78 (deflate, 32K window) and an FLG
#: that makes the pair a multiple of 31. As verify-bundle has it.
_ZLIB_HEADERS = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
#: How much one candidate stream may inflate to before it's cut off.
_ZLIB_LIMIT = 8 << 20


def inflated_streams(data: bytes) -> Iterator[bytes]:
    """Every zlib stream in `data` that inflates, as its (bounded) plaintext:
    verify-bundle's _inflated_streams. Names leaked only inside compressed
    data (coverage mappings) in 1.0 and 1.1."""
    import zlib
    view = memoryview(data)
    for m in _ZLIB_HEADERS.finditer(data):
        i = m.start()
        if (data[i] * 256 + data[i + 1]) % 31:
            continue
        try:
            out = zlib.decompressobj().decompress(view[i:], _ZLIB_LIMIT)
        except zlib.error:
            continue
        if out:
            yield out


# ------------------------------------------------------------ code signatures

LC_CODE_SIGNATURE = 0x1D
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0


def signature_cms_ranges(data: bytes) -> list[tuple[int, int]]:
    """(start, end) of each Mach-O slice's embedded code signature in `data`:
    the code directory, the requirements, the entitlements and the CMS blob
    with the signing certificates. The certificate's subject is the
    Developer ID's name, and the code directory and requirements carry its
    Team ID: both public by design (`codesign -dv` shows them to anyone),
    both the owner's, which the list holds, so a signed app would always
    fail (2.0.1's try1 did, on the Team ID). None of it is Graft's text: it
    is what codesign writes. The rest of the file is still scanned."""
    if len(data) < 8:
        return []
    magic, nfat = struct.unpack_from(">II", data)
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        if not 0 < nfat <= 16:  # a Java class file starts with cafebabe too
            return []
        entry, fmt = (32, ">iiQQII") if magic == 0xCAFEBABF else (20, ">iiIII")
        if 8 + nfat * entry > len(data):
            return []
        offsets = [struct.unpack_from(fmt, data, 8 + i * entry)[2] for i in range(nfat)]
    else:
        offsets = [0]
    return [r for off in offsets for r in _slice_cms(data, off)]


def _slice_cms(data: bytes, off: int) -> list[tuple[int, int]]:
    if off + 32 > len(data):
        return []
    magic, = struct.unpack_from("<I", data, off)
    if magic not in (0xFEEDFACE, 0xFEEDFACF):
        return []
    ncmds, sizeofcmds = struct.unpack_from("<II", data, off + 16)
    pos = off + (32 if magic == 0xFEEDFACF else 28)
    end = min(pos + sizeofcmds, len(data))
    for _ in range(ncmds):
        if pos + 8 > end:
            break
        cmd, cmdsize = struct.unpack_from("<II", data, pos)
        if cmdsize < 8:
            break
        if cmd == LC_CODE_SIGNATURE and pos + 16 <= end:
            dataoff, datasize = struct.unpack_from("<II", data, pos + 8)
            start, stop = off + dataoff, min(off + dataoff + datasize, len(data))
            if start + 12 <= stop and struct.unpack_from(">I", data, start)[0] == CSMAGIC_EMBEDDED_SIGNATURE:
                return [(start, stop)]
            return []
        pos += cmdsize
    return []


def without_signatures(data: bytes) -> bytes:
    """`data` with each Mach-O CMS signature in it zeroed (signature_cms_ranges)."""
    ranges = signature_cms_ranges(data)
    if not ranges:
        return data
    out = bytearray(data)
    for start, end in ranges:
        out[start:end] = bytes(end - start)
    return bytes(out)


# ------------------------------------------------------------ scanning

@dataclass(frozen=True)
class Hit:
    #: The file, and inside an archive the member (`archive!member`).
    where: str
    lines: tuple[int, ...]

    def __str__(self) -> str:
        return f"{self.where}: the deny-list's line {', '.join(map(str, self.lines))}"


#: How deep archives inside archives are opened (the source archive holds
#: sdists, crates and vendored modules, each an archive).
MAX_DEPTH = 4


#: Compressed data, by its magic, and how to decompress it.
_COMPRESSED = ((b"\x1f\x8b", "gzip"), (b"BZh", "bz2"), (b"\xfd7zXZ\x00", "lzma"))


def _archive(data: bytes) -> tarfile.TarFile | zipfile.ZipFile | None:
    """`data` opened as the tar (plain, gzip, bzip2 or xz) or zip it is, or None."""
    try:
        if any(data.startswith(magic) for magic, _ in _COMPRESSED) or data[257:262] == b"ustar":
            return tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            return zipfile.ZipFile(io.BytesIO(data))
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError):
        return None
    return None


def _decompressed(data: bytes) -> bytes:
    """A compressed file that isn't a tar (a lone .gz), decompressed; anything else as it is."""
    for magic, module in _COMPRESSED:
        if data.startswith(magic):
            try:
                return importlib.import_module(module).decompress(data)
            except (OSError, EOFError, ValueError, ImportError):
                return data
    return data


def _members(archive: tarfile.TarFile | zipfile.ZipFile) -> Iterator[tuple[str, bytes | None]]:
    """(name, contents) of each member; None for one with no contents (a folder, a link)."""
    if isinstance(archive, zipfile.ZipFile):
        for info in archive.infolist():
            yield info.filename, None if info.is_dir() else archive.read(info)
    else:
        for m in archive:
            f = archive.extractfile(m) if m.isfile() else None
            yield m.name, f.read() if f else None


def _scan(where: str, name: str, data: bytes | None, dl: Denylist, hits: list[Hit], depth: int) -> None:
    """One file: its name, then its contents, or each member's if it is an archive."""
    found = set(dl.lines_in(name.encode(errors="surrogateescape"), inflate=False))
    archive = _archive(data) if data is not None and depth < MAX_DEPTH else None
    if archive is not None:
        with archive:
            for member, contents in _members(archive):
                _scan(f"{where}!{member}", member, contents, dl, hits, depth + 1)
    elif data is not None:
        found.update(dl.lines_in(_decompressed(data)))
    if found:
        hits.append(Hit(where, tuple(sorted(found))))


def _everything(rel: str) -> bool:
    return True


def scan_path(path: Path, dl: Denylist, label: str | None = None,
              contents: Callable[[str], bool] = _everything,
              names: Callable[[str], bool] = _everything) -> list[Hit]:
    """Every hit in `path`: a folder's files (their paths under it, and
    contents), or one file and, if it is an archive, its members. A top-level
    tar is read member by member rather than whole. Symlinks are named, not
    followed. `contents` says, by a file's path under `path` (or a member's
    name), whether its contents are scanned too; `names`, whether the path is
    looked at at all (its name, and its contents)."""
    hits: list[Hit] = []
    label = path.name if label is None else label
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            rel = p.relative_to(path).as_posix()
            if not names(rel):
                continue
            data = p.read_bytes() if p.is_file() and not p.is_symlink() and contents(rel) else None
            _scan(f"{label}/{rel}" if label else rel, rel, data, dl, hits, 1)
    elif tarfile.is_tarfile(path):
        found = set(dl.lines_in(path.name.encode(), inflate=False))
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                if not names(m.name):
                    continue
                f = tf.extractfile(m) if m.isfile() and contents(m.name) else None
                _scan(f"{label}!{m.name}" if label else m.name, m.name, f.read() if f else None, dl, hits, 1)
        if found:
            hits.append(Hit(label or path.name, tuple(sorted(found))))
    else:
        _scan(label, path.name, path.read_bytes(), dl, hits, 0)
    return hits


#: Where Graft.app holds others' code and texts, by path under the app:
#: CPython and the locked wheels (cyan, and the helpers built for it, among
#: them), OpenSSL and libplist, the three tools, and the license texts. Each
#: is a pinned download checked by its SHA-256, or built here from one.
APP_THIRD_PARTY = ("Contents/Resources/GraftPython/", "Contents/Frameworks/", "Contents/Resources/Licenses/",
                   "Contents/Helpers/bin/zsign", "Contents/Helpers/bin/ipatool", "Contents/Helpers/bin/unfairplay")
#: Graft's own files among them, scanned whole, names and contents: its
#: Python package (and its dist-info).
APP_OWN = ("Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa",)
#: What the signature writes: CodeResources lists every file's path, so
#: scanning it would only repeat, as one hit, the names skipped above.
APP_DERIVED = ("Contents/_CodeSignature/",)


def app_contents_scanned(rel: str) -> bool:
    """Whether a file's contents are scanned, by its path under Graft.app."""
    return rel.startswith(APP_OWN) or not rel.startswith(APP_THIRD_PARTY + APP_DERIVED)


def app_names_scanned(rel: str) -> bool:
    """Whether a path's name is scanned. Others' files keep upstream's names
    (a Go module's folder, a lexer named for a game), and so do the license
    files and components.json entries written about them, so under the
    third-party roots neither names nor contents are Graft's to scan. The
    try1 build of 2.0.1 failed on exactly those: popular names used as
    ordinary words by others' code."""
    return rel.startswith(APP_OWN) or not rel.startswith(APP_THIRD_PARTY)


#: The source archive's folders (`<top>/<folder>/`) of others' sources: the
#: pinned archives, the vendored Go modules (checked against go.sum) and
#: crates (against their checksums), and the sdists (against the lock). Its
#: graft/ (the tracked files again) and SOURCES.md are Graft's.
SOURCES_THIRD_PARTY = ("pinned", "sdists")


def sources_contents_scanned(rel: str) -> bool:
    """Whether a source archive member's contents are scanned, by its name."""
    parts = rel.split("/")
    return not (len(parts) > 2 and parts[1] in SOURCES_THIRD_PARTY)


def _graft_bundle():
    """scripts/graft_bundle.py, for what a public export of the tree carries."""
    spec = importlib.util.spec_from_file_location("graft_bundle_for_denylist", Path(__file__).with_name("graft_bundle.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scan_tracked(checkout: Path, commit: str, dl: Denylist) -> list[Hit]:
    """Every hit in the files a public export of `commit` carries: the tree
    the source archive's graft/ is (graft_bundle.graft_source_tar), so
    docs/superpowers and tweaks-src, kept out of every export, are left out
    here too. Paths are scanned as well as contents."""
    gb = _graft_bundle()
    with tempfile.TemporaryDirectory(prefix="graft-denylist-") as tmp:
        tar = Path(tmp) / "public.tar"
        gb.graft_source_tar(tar, "", repo=checkout, commit=commit)
        return scan_path(tar, dl, label="")


# ------------------------------------------------------------ the command

def _load_or_report() -> Denylist | None:
    """The list; None, having said why, when it can't be read."""
    try:
        return load()
    except DenylistError as e:
        print(f"error: {e}. The release can't vouch that what it publishes names no app, tweak, bundle id or "
              f"owner without it: put the private scrub list there, or set {PATH_ENV} to it. "
              f"{SKIP_ENV}=1 releases unchecked.", file=sys.stderr)
        return None


def _report(hits: list[Hit], what: str) -> int:
    if hits:
        print(f"error: {what} holds terms from the private deny-list, which must never be public "
              f"({len(hits)} place{'s' if len(hits) != 1 else ''}):", file=sys.stderr)
        for hit in hits[:50]:
            print(f"  {hit}", file=sys.stderr)
        if len(hits) > 50:
            print(f"  ... and {len(hits) - 50} more", file=sys.stderr)
        return 1
    print(f"  no deny-listed name in {what}")
    return 0


def main(argv: list[str]) -> int:
    usage = ("usage: denylist.py check | tracked <checkout> <commit> | sources <tarball> | app <Graft.app> "
             "| scan <path>...")
    if not (argv == ["check"] or (len(argv) == 3 and argv[0] == "tracked")
            or (len(argv) == 2 and argv[0] in ("sources", "app")) or (len(argv) >= 2 and argv[0] == "scan")):
        print(usage, file=sys.stderr)
        return 2
    if skipped():
        print(skip_warning(), file=sys.stderr)
        return 0
    dl = _load_or_report()
    if dl is None:
        return 1
    if argv[0] == "check":
        allowed = f", {len(dl.allowed)} of them allowed in a release (#release-allow)" if dl.allowed else ""
        print(f"  deny-list: {len(dl.entries) + len(dl.allowed)} terms from {dl.path}{allowed}")
        return 0
    if argv[0] == "tracked":
        return _report(scan_tracked(Path(argv[1]), argv[2], dl), f"the tracked files a public export of {argv[2]} carries")
    scope = {"sources": sources_contents_scanned, "app": app_contents_scanned}.get(argv[0], _everything)
    named = app_names_scanned if argv[0] == "app" else _everything
    status = 0
    for arg in argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            status = 1
            continue
        status |= _report(scan_path(path, dl, contents=scope, names=named), str(path))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
