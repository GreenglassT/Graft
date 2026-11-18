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
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import SigningError


class UnreadableP12Error(SigningError):
    """The .p12 would not open with the password given: wrong, most often."""


def write_reencrypted(src: Path, password: str, dest: Path, new_password: str) -> None:
    """Re-encrypt the identity in `src` under `new_password` into `dest` (0600).

    The legacy PBES1 / 3DES / SHA-1 MAC profile, not cryptography's default
    AES one: macOS `security import` refuses the modern encryption with an
    unhelpful "MAC verification failed", and the OpenSSL zsign links reads the
    legacy one in every version. The weak cipher protects a file that lives for
    one subprocess call under a random password, inside a private directory.

    The account's real .p12 is BER- rather than DER-encoded, so cryptography
    warns that it is "falling back to BER" on load. That is expected.

    `dest` must not exist: it is created exclusively, so nothing already at
    that path — a link planted there, say — is written through.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import PrivateFormat, pkcs12

    try:
        key, cert, extra = pkcs12.load_key_and_certificates(src.read_bytes(), password.encode())
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
