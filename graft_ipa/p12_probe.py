"""Open a .p12 in a process of its own, so the setup can time-box it.

`python -I -m graft_ipa.p12_probe`, started by `p12.probe`. stdin is the
password's length (4 bytes, big-endian), the password, then the .p12's
bytes: never argv, never the environment. stdout is one word and nothing
else, never a key: `ok` (the key and a certificate are there), `incomplete`
(the file opens but lacks one of them) or `wrong` (it doesn't open with that
password, or isn't PKCS#12 at all).

How long this takes is decided by the file: its key-derivation counts, some
of which are hidden inside its encrypted parts until they are decrypted
(`p12.kdf_iterations` can only read the rest). The parent kills it when its
time is up and refuses the file; that is the whole reason it is a process.
"""

from __future__ import annotations

import sys


def main() -> int:
    data = sys.stdin.buffer.read()
    size = int.from_bytes(data[:4], "big")
    password, p12 = data[4:4 + size], data[4 + size:]
    from graft_ipa.p12 import load_key_and_certificates

    try:
        key, cert, _ = load_key_and_certificates(p12, password)
    except ValueError:
        print("wrong")
        return 0
    print("ok" if key is not None and cert is not None else "incomplete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
