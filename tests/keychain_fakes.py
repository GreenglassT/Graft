"""A stand-in for macOS `security`, so no test ever reads or writes a real keychain."""

from __future__ import annotations

import shlex
import subprocess


class FakeSecurity:
    """Enough of `security` to run keychain.py against.

    `find-generic-password` as argv, and `add-generic-password` read from
    stdin by `security -i` — parsed with POSIX double-quote rules, which is
    what `security`'s own interactive parser was measured to follow for the
    quoting keychain.py does.
    """

    def __init__(self, items: dict[tuple[str, str], str] | None = None) -> None:
        self.items = dict(items or {})
        self.calls: list[list[str]] = []
        self.stdin: list[str] = []
        self.fail_writes = False
        #: Exit 0 but complain on stderr — `security -i` can do that.
        self.write_stderr_on_success = b""
        #: service -> exit status for a lookup, overriding found/not-found
        #: (51 is what a denied Keychain prompt returns).
        self.find_status: dict[str, int] = {}
        #: service -> a `password:` line to print instead of the real one,
        #: for shapes `security` has not been seen to produce.
        self.password_lines: dict[str, str] = {}

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[1:] == ["-i"]:
            data = kwargs.get("input") or b""
            text = data.decode() if isinstance(data, bytes) else str(data)
            self.stdin.append(text)
            return self._interactive(text)
        if argv[1] == "find-generic-password":
            return self._find(argv[2:], text=bool(kwargs.get("text")))
        raise AssertionError(f"unexpected security call: {argv}")

    @staticmethod
    def _options(tokens: list[str]) -> dict[str, str]:
        return dict(zip(tokens[0::2], tokens[1::2], strict=False))

    @staticmethod
    def _printable(secret: str) -> bool:
        # What `security` renders as text rather than hex, as measured: ASCII
        # 0x20-0x7e, except the backslash (which it hex-encodes).
        return all(" " <= c <= "~" and c != "\\" for c in secret)

    @classmethod
    def password_line(cls, secret: str) -> str:
        """`find-generic-password -g`'s stderr line for `secret`, in the three
        four shapes measured on a real keychain."""
        if secret == "":
            return "password: "
        if cls._printable(secret):
            return f'password: "{secret}"'
        data = secret.encode()
        if not any(0x20 <= b <= 0x7E and b != 0x5C for b in data):
            # Nothing printable at all: the hex alone, and one trailing space.
            return f"password: 0x{data.hex().upper()} "
        octal = "".join(
            chr(b) if 0x20 <= b <= 0x7E and b != 0x5C else f"\\{b:03o}" for b in data
        )
        return f'password: 0x{data.hex().upper()}  "{octal}"'

    def _find(self, tokens: list[str], *, text: bool) -> subprocess.CompletedProcess:
        opts = self._options(tokens)
        service, account = opts.get("-s", ""), opts.get("-a", "")

        def out(s: str):
            return s if text else s.encode()

        if service in self.find_status:
            return subprocess.CompletedProcess([], self.find_status[service], out(""), out(""))
        if (service, account) not in self.items:
            return subprocess.CompletedProcess(
                [], 44, out(""),
                out("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n"),
            )
        secret = self.items[(service, account)]
        if "-g" in tokens:
            line = self.password_lines.get(service) or self.password_line(secret)
            attrs = f'keychain: "/fake.keychain-db"\nclass: "genp"\n    "svce"<blob>="{service}"\n'
            return subprocess.CompletedProcess([], 0, out(attrs), out(line + "\n"))
        # `-w`: printable secrets as text, anything else as bare hex.
        shown = secret if self._printable(secret) else secret.encode().hex()
        return subprocess.CompletedProcess([], 0, out(shown + "\n"), out(""))

    def _interactive(self, text: str) -> subprocess.CompletedProcess:
        lines = text.splitlines()
        assert len(lines) == 1, f"one command per call expected, got {lines!r}"
        tokens = shlex.split(lines[0], posix=True)
        assert tokens[0] == "add-generic-password", tokens
        if self.fail_writes:
            return subprocess.CompletedProcess(
                [], 45, b"", b"security: SecKeychainItemCreateFromContent: write refused\n"
            )
        opts = self._options([t for t in tokens[1:] if t not in ("-U", "-A")])
        self.items[(opts["-s"], opts["-a"])] = opts["-w"]
        return subprocess.CompletedProcess([], 0, b"", self.write_stderr_on_success)
