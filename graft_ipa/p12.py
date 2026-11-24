"""The signing identity, handed to a tool without its real password.

Two tools need the `.p12` and would take its password on the command line:
`security import -P` (LiveContainer's throwaway keychain, `lc_sign`) and
`zsign -p` (sideload signing, `signer`). A command line is readable by every
user on the Mac (`ps -axww`) for as long as the tool runs, so neither ever gets
the user's password: the identity is re-encrypted in-process under a one-time
password, written owner-only into a private directory, and that copy and that
password are what the tool sees.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .child_env import child_env
from .errors import SigningError


class UnreadableP12Error(SigningError):
    """The .p12 would not open with the password given: wrong, most often."""


#: cryptography's notice that a .p12 is BER rather than DER, as a `warnings`
#: message pattern (matched from the message's start, hence the `.*`).
_BER_FALLBACK_WARNING = r".*could not be parsed as DER"


def load_key_and_certificates(data: bytes, password: bytes | None) -> tuple[Any, Any, Any]:
    """`pkcs12.load_key_and_certificates`, without its BER-fallback warning.

    A Keychain Access export is BER- rather than DER-encoded, so cryptography
    warns "PKCS#12 bundle could not be parsed as DER, falling back to parsing
    as BER" on every load of the account's real .p12. That UserWarning went to
    stderr in every paid build, where the Mac app reads error lines. Only that
    warning is silenced; every other one still shows. Every .p12 load in
    graft goes through here.
    """
    from cryptography.hazmat.primitives.serialization import pkcs12

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=_BER_FALLBACK_WARNING, category=UserWarning)
        return pkcs12.load_key_and_certificates(data, password)


def write_reencrypted(src: Path, password: str, dest: Path, new_password: str) -> None:
    """Re-encrypt the identity in `src` under `new_password` into `dest` (0600).

    The legacy PBES1 / 3DES / SHA-1 MAC profile, not cryptography's default
    AES one: macOS `security import` refuses the modern encryption with an
    unhelpful "MAC verification failed", and the OpenSSL zsign links reads the
    legacy one in every version. The weak cipher protects a file that lives for
    one subprocess call under a random password, inside a private directory.

    The account's real .p12 is BER- rather than DER-encoded; see
    `load_key_and_certificates` for the warning that brings.

    `dest` must not exist: it is created exclusively, so nothing already at
    that path — a link planted there, say — is written through.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import PrivateFormat, pkcs12

    try:
        key, cert, extra = load_key_and_certificates(src.read_bytes(), password.encode())
    except ValueError as e:
        raise UnreadableP12Error(
            f"could not read {src}: {e}",
            hint="wrong .p12 password, or an unsupported export format",
        ) from e
    if key is None or cert is None:
        raise SigningError(f"{src} does not hold both a private key and a certificate")
    encryption = (
        PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())  # the MAC macOS's importer accepts
        .build(new_password.encode())
    )
    data = pkcs12.serialize_key_and_certificates(b"graft", key, cert, extra, encryption)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


@contextmanager
def private_identity(src: Path, password: str, *, prefix: str = "graft-p12-") -> Iterator[tuple[Path, str]]:
    """`(copy, one_time_password)`: `src`'s identity, re-encrypted, for one tool.

    The copy is 0600 inside a fresh 0700 directory (`mkdtemp`), and the
    directory is removed when the block exits — on success, on an exception,
    and on `SystemExit`. A caller whose process can be stopped by SIGTERM (the
    GUI's Stop) has to turn that signal into `SystemExit` around this block
    (`lc_manage.exit_on_termination`); the default disposition kills the
    process without running any `finally`.
    """
    work = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        one_time = secrets.token_hex(16)
        copy = work / "identity.p12"
        write_reencrypted(src, password, copy, one_time)
        yield copy, one_time
    finally:
        # SIGTERM and SIGHUP held for the removal: a second Stop arriving
        # mid-`rmtree` would raise out of it and leave the identity behind;
        # held, it is raised the moment the directory is gone (as
        # `appstore.private_workdir` does). SIGKILL cannot be held, which is
        # what `sweep_stale_copies` is for.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP})
        try:
            shutil.rmtree(work, ignore_errors=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)


#: The directories `private_identity` makes: zsign's (`signer`) and the default.
COPY_PREFIXES = ("graft-zsign-", "graft-p12-")
#: How old a copy must be before a sweep removes it: far past any signing
#: another Graft could still be doing with it.
STALE_COPY_SECONDS = 60 * 60


def sweep_stale_copies(
    tmpdir: Path | None = None, *, older_than: float = STALE_COPY_SECONDS
) -> list[Path]:
    """Remove identity copies a killed run left in the temp directory.

    `private_identity` removes its directory on every way out but SIGKILL —
    and the GUI sends one two seconds after a Stop that has not finished. What
    is left is a 0700 directory holding the identity under a password that
    died with the process, still the private key. Only directories older than
    `older_than`, named as ours, owned by this user. Never raises.
    """
    root = Path(tmpdir) if tmpdir is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - older_than
    removed: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        try:
            if not entry.name.startswith(COPY_PREFIXES) or not entry.is_dir(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_mtime >= cutoff:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            removed.append(Path(entry.path))
        except OSError:
            continue
    return removed


# --- how much key derivation a .p12 asks for, read before any is done -------------

#: The most PBKDF / PKCS#12-KDF rounds a chosen .p12 may ask for, summed over
#: its MAC and every encryption whose parameters can be read before anything
#: is decrypted. Keychain Access, `security export` and OpenSSL ask for a few
#: thousand in all; a crafted file can ask for 2^31 per field and stall the
#: setup for minutes (the 2.0 review's F3).
MAX_KDF_ITERATIONS = 2_000_000


class CostlyP12Error(ValueError):
    """The .p12 asks for more key derivation than `MAX_KDF_ITERATIONS`."""

    def __init__(self, iterations: int) -> None:
        super().__init__(f"{iterations} key-derivation rounds")
        self.iterations = iterations


class P12StructureError(ValueError):
    """The .p12 isn't laid out as PKCS#12 that `kdf_iterations` can read."""


_OID_DATA = "1.2.840.113549.1.7.1"
_OID_ENCRYPTED_DATA = "1.2.840.113549.1.7.6"
_OID_SHROUDED_KEY_BAG = "1.2.840.113549.1.12.10.1.2"
_OID_SAFE_CONTENTS_BAG = "1.2.840.113549.1.12.10.1.6"
_OID_PBES2 = "1.2.840.113549.1.5.13"
_OID_PBKDF2 = "1.2.840.113549.1.5.12"
#: pbeWithSHAAnd*: the PKCS#12 KDF, params `{salt, iterations}`.
_PKCS12_PBE = "1.2.840.113549.1.12.1."
#: PBES1 (PKCS#5 v1.5), the same params.
_PBES1 = frozenset(f"1.2.840.113549.1.5.{n}" for n in (1, 3, 4, 6, 10, 11))
#: How deep `safeContentsBag`s may nest. OpenSSL recurses into them; nothing
#: real nests at all.
_MAX_DEPTH = 4


def _tlv(data: bytes, i: int, end: int, depth: int = 0) -> tuple[int, int, int, int]:
    """`(tag, content_start, content_end, next)` of the BER element at `i`."""
    if depth > 32 or i + 2 > end:
        raise P12StructureError("truncated")
    tag = data[i]
    if tag & 0x1F == 0x1F:
        raise P12StructureError("multi-byte tag")
    first = data[i + 1]
    i += 2
    if first == 0x80:  # indefinite: constructed, ends at 00 00
        if not tag & 0x20:
            raise P12StructureError("indefinite primitive")
        j = i
        while True:
            if j + 2 > end:
                raise P12StructureError("no end-of-contents")
            if data[j] == 0 and data[j + 1] == 0:
                return tag, i, j, j + 2
            j = _tlv(data, j, end, depth + 1)[3]
    if first & 0x80:
        count = first & 0x7F
        if not 0 < count <= 4 or i + count > end:
            raise P12StructureError("length out of range")
        length = int.from_bytes(data[i:i + count], "big")
        i += count
    else:
        length = first
    if i + length > end:
        raise P12StructureError("element runs past its parent")
    return tag, i, i + length, i + length


def _kids(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    i = start
    while i < end:
        tag, cs, ce, i = _tlv(data, i, end)
        out.append((tag, cs, ce))
    return out


def _want(node: tuple[int, int, int], tag: int) -> tuple[int, int, int]:
    if node[0] != tag:
        raise P12StructureError(f"expected tag {tag:#x}, found {node[0]:#x}")
    return node


def _seq(data: bytes, node: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    _, cs, ce = _want(node, 0x30)
    return _kids(data, cs, ce)


def _explicit(data: bytes, node: tuple[int, int, int]) -> tuple[int, int, int]:
    """The one element inside a `[0] EXPLICIT`."""
    _, cs, ce = _want(node, 0xA0)
    kids = _kids(data, cs, ce)
    if len(kids) != 1:
        raise P12StructureError("[0] holds more than one element")
    return kids[0]


def _octets(data: bytes, node: tuple[int, int, int]) -> bytes:
    tag, cs, ce = node
    if tag == 0x04:
        return data[cs:ce]
    if tag == 0x24:  # constructed (BER): the chunks, joined
        return b"".join(_octets(data, kid) for kid in _kids(data, cs, ce))
    raise P12StructureError(f"not an octet string: {tag:#x}")


def _oid(data: bytes, node: tuple[int, int, int]) -> str:
    _, cs, ce = _want(node, 0x06)
    body = data[cs:ce]
    if not body:
        raise P12StructureError("empty OID")
    arcs = [body[0] // 40, body[0] % 40] if body[0] < 80 else [2, body[0] - 80]
    value = 0
    for b in body[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(a) for a in arcs)


def _count(data: bytes, node: tuple[int, int, int]) -> int:
    _, cs, ce = _want(node, 0x02)
    value = int.from_bytes(data[cs:ce], "big", signed=True)
    if value < 1:
        raise P12StructureError("an iteration count below 1")
    return value


def _algorithm_cost(data: bytes, node: tuple[int, int, int]) -> int:
    """The rounds one AlgorithmIdentifier asks for; unknown schemes are refused."""
    kids = _seq(data, node)
    if len(kids) < 2:
        raise P12StructureError("an encryption with no parameters")
    oid = _oid(data, kids[0])
    if oid.startswith(_PKCS12_PBE) or oid in _PBES1:
        params = _seq(data, kids[1])
        return _count(data, params[1]) if len(params) > 1 else 1
    if oid == _OID_PBES2:
        kdf, _enc = _seq(data, kids[1])[:2]
        kdf_kids = _seq(data, kdf)
        if _oid(data, kdf_kids[0]) != _OID_PBKDF2 or len(kdf_kids) < 2:
            raise P12StructureError("a PBES2 key derivation other than PBKDF2")
        return _count(data, _seq(data, kdf_kids[1])[1])
    raise P12StructureError(f"an unknown encryption ({oid})")


def _bags_cost(data: bytes, node: tuple[int, int, int], depth: int) -> int:
    """The rounds the shrouded key bags in one plaintext SafeContents (`node`)
    ask for, nested `safeContentsBag`s included."""
    if depth > _MAX_DEPTH:
        raise P12StructureError("SafeContents nested too deep")
    total = 0
    for bag in _seq(data, node):
        bag_kids = _seq(data, bag)
        kind = _oid(data, bag_kids[0])
        if kind == _OID_SHROUDED_KEY_BAG:
            total += _algorithm_cost(data, _seq(data, _explicit(data, bag_kids[1]))[0])
        elif kind == _OID_SAFE_CONTENTS_BAG:
            total += _bags_cost(data, _explicit(data, bag_kids[1]), depth + 1)
    return total


def _root(data: bytes) -> tuple[int, int, int]:
    """The one element `data` holds, which must fill it."""
    tag, cs, ce, nxt = _tlv(data, 0, len(data))
    if nxt != len(data):
        raise P12StructureError("bytes after the end")
    return tag, cs, ce


def kdf_iterations(data: bytes) -> int:
    """The key-derivation rounds `data`, a PKCS#12 file, asks for before any
    of it is decrypted: the MAC's, each encrypted SafeContents', and each
    shrouded key bag's in a plaintext SafeContents.

    A minimal BER read of the structure OpenSSL walks, not a decryption.
    Every exporter measured (OpenSSL 3 and its `-legacy`, LibreSSL, `security
    export`) puts the private key in a plaintext SafeContents, where its
    count can be read. A key bag hidden inside an *encrypted* SafeContents
    can't be read before decrypting, so its count isn't in this sum.

    Raises `P12StructureError` for anything that isn't laid out that way.
    """
    try:
        pfx = _seq(data, _root(data))
        if len(pfx) < 2:
            raise P12StructureError("not a PFX")
        total = 0
        if len(pfx) > 2:
            mac = _seq(data, pfx[2])
            total += _count(data, mac[2]) if len(mac) > 2 else 1
        auth = _seq(data, pfx[1])
        if _oid(data, auth[0]) != _OID_DATA:
            raise P12StructureError("an authSafe that isn't password-integrity data")
        safe = _octets(data, _explicit(data, auth[1]))
        for info in _seq(safe, _root(safe)):
            info_kids = _seq(safe, info)
            kind = _oid(safe, info_kids[0])
            if kind == _OID_DATA:
                bags = _octets(safe, _explicit(safe, info_kids[1]))
                total += _bags_cost(bags, _root(bags), 0)
            elif kind == _OID_ENCRYPTED_DATA:
                encrypted = _seq(safe, _explicit(safe, info_kids[1]))
                content_info = _seq(safe, encrypted[1])
                total += _algorithm_cost(safe, content_info[1])
            else:
                raise P12StructureError(f"a SafeContents of an unknown kind ({kind})")
        return total
    except (IndexError, ValueError, RecursionError) as e:
        if isinstance(e, P12StructureError):
            raise
        raise P12StructureError(str(e)) from None


def check_kdf_cost(data: bytes) -> None:
    """Refuse a .p12 that asks for more than `MAX_KDF_ITERATIONS` before
    OpenSSL spends them (`CostlyP12Error`), or one `kdf_iterations` can't read
    (`P12StructureError`)."""
    total = kdf_iterations(data)
    if total > MAX_KDF_ITERATIONS:
        raise CostlyP12Error(total)


# --- the backstop: opening it in a process that can be stopped --------------------

#: How long a chosen .p12 may take to open. Keychain Access's exports open in
#: milliseconds; a key bag hidden inside an encrypted SafeContents can ask for
#: 2^31 rounds that `kdf_iterations` can't see, and in-process nothing could
#: stop OpenSSL spending them.
PROBE_SECONDS = 10.0


class SlowP12Error(ValueError):
    """The .p12 didn't open within `PROBE_SECONDS`; the child was killed."""


class P12ProbeError(ValueError):
    """The child that opens the .p12 didn't run or didn't answer."""


def _probe_argv() -> list[str]:
    """The child: this interpreter, isolated (`-I`: no PYTHON* variables, no
    user site, no current directory on the path), running `p12_probe`. A
    seam for the test of the timeout path."""
    return [sys.executable, "-I", "-m", "graft_ipa.p12_probe"]


def probe(data: bytes, password: bytes, *, seconds: float | None = None) -> str:
    """Open `data` with `password` in a child process (`graft_ipa.p12_probe`),
    for at most `seconds` (`PROBE_SECONDS`); its answer, `ok`, `incomplete`
    or `wrong`.

    The password and the file go on the child's stdin only, never its argv
    or environment. When the time is up the child is killed and reaped
    (`subprocess.run` does both before it raises) and `SlowP12Error` is
    raised: there is no fall-back to opening it here. A caller that needs the
    key opens the file itself afterward, knowing it takes no longer than
    the child did.
    """
    payload = len(password).to_bytes(4, "big") + password + data
    try:
        proc = subprocess.run(
            _probe_argv(), input=payload, capture_output=True, check=False,
            timeout=PROBE_SECONDS if seconds is None else seconds, env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise SlowP12Error() from None
    except OSError as e:
        raise P12ProbeError(f"couldn't start the check ({e.strerror or e})") from None
    answer = proc.stdout.decode("ascii", "replace").strip()
    if proc.returncode != 0 or answer not in ("ok", "incomplete", "wrong"):
        raise P12ProbeError(f"the check stopped without an answer (exit {proc.returncode})")
    return answer
