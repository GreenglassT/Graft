"""A stand-in for macOS `security`, so no test ever reads or writes a real keychain."""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Who made an item nobody said made it: the 1.x ipatool, an ad hoc build.
FOREIGN = "another-ipatool"
#: The first creation date `FakeSecurity` hands out; each creation adds a
#: step, so two items made apart never share one (a real `cdat` has
#: one-second resolution, and two sign-ins are tens of seconds apart).
_CDAT_BASE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
_CDAT_STEP = timedelta(seconds=7)


def cdat_for(tick: int) -> str:
    """The creation date of the `tick`th item made, as `security` prints it."""
    return (_CDAT_BASE + _CDAT_STEP * tick).strftime("%Y%m%d%H%M%SZ")


def attributes_block(service: str, account: str, cdat: str, mdat: str) -> str:
    """`find-generic-password`'s stdout for one item, without `-g`/`-w`: the
    shape macOS's `security` prints (SecurityTool's
    `print_keychain_item_attributes`): the dates are 16-byte blobs ending in a
    NUL, so as hex and then quoted with the NUL escaped."""

    def date(value: str) -> str:
        raw = value.encode() + b"\0"
        return f'0x{raw.hex().upper()}  "{value}\\000"'

    return (
        'keychain: "/Users/fake/Library/Keychains/login.keychain-db"\n'
        "version: 512\n"
        'class: "genp"\n'
        "attributes:\n"
        f'    0x00000007 <blob>="{service}"\n'
        "    0x00000008 <blob>=<NULL>\n"
        f'    "acct"<blob>="{account}"\n'
        f'    "cdat"<timedate>={date(cdat)}\n'
        '    "crtr"<uint32>=<NULL>\n'
        '    "cusi"<sint32>=<NULL>\n'
        '    "desc"<blob>=<NULL>\n'
        '    "gena"<blob>=<NULL>\n'
        '    "icmt"<blob>=<NULL>\n'
        '    "invi"<sint32>=<NULL>\n'
        f'    "mdat"<timedate>={date(mdat)}\n'
        '    "nega"<sint32>=<NULL>\n'
        '    "prot"<blob>=<NULL>\n'
        '    "scrp"<sint32>=<NULL>\n'
        f'    "svce"<blob>="{service}"\n'
        '    "type"<uint32>=<NULL>\n'
    )


class FakeSecurity:
    """Enough of `security` to run keychain.py against.

    `find-generic-password` as argv, and `add-generic-password` read from
    stdin by `security -i` — parsed with POSIX double-quote rules, which is
    what `security`'s own interactive parser was measured to follow for the
    quoting keychain.py does.

    Every item has a creation date (`cdat`), which `find-generic-password`
    prints among its attributes, and an owner: the program that made it, which
    is the only one macOS lets read it without a dialog. `security` itself
    never shows an owner; the fake ipatool (`appstore_fakes`) reads it to
    decide whether reading an item would have asked.

    With `store`, the items live in that JSON file rather than in memory, so a
    fake ipatool in another process can make, update and remove them as the
    real one does in the login keychain. Each call reads the file first and
    writes it after; `load()` refreshes `items` for a test's own look.
    """

    def __init__(
        self,
        items: dict[tuple[str, str], str] | None = None,
        *,
        store: Path | None = None,
        owner: str = FOREIGN,
    ) -> None:
        self.items = dict(items or {})
        #: (service, account) -> cdat, and -> who made it.
        self.created: dict[tuple[str, str], str] = {}
        self.modified: dict[tuple[str, str], str] = {}
        self.owners: dict[tuple[str, str], str] = {}
        self.tick = 0
        self.store = store
        self.calls: list[list[str]] = []
        self.stdin: list[str] = []
        self.fail_writes = False
        self.fail_deletes = False
        #: (service, account) -> exit status for a delete, overriding the rest.
        self.delete_status: dict[tuple[str, str], int] = {}
        #: How many writes would have asked the user to confirm an access-list
        #: change (`-U -A` on an item that exists): always 0 for Graft.
        self.access_prompts = 0
        #: Exit 0 but complain on stderr — `security -i` can do that.
        self.write_stderr_on_success = b""
        #: service -> exit status for a lookup, overriding found/not-found
        #: (51 is what a denied Keychain prompt returns).
        self.find_status: dict[str, int] = {}
        #: (service, account) -> exit status for a lookup of that one item.
        self.find_item_status: dict[tuple[str, str], int] = {}
        #: Services, or (service, account) keys, whose calls never answer:
        #: `security` waiting on a SecurityAgent dialog nobody answers, which
        #: `subprocess.run` ends as a timeout.
        self.hang: set[object] = set()
        #: service -> a `password:` line to print instead of the real one,
        #: for shapes `security` has not been seen to produce.
        self.password_lines: dict[str, str] = {}
        #: Print no `"cdat"` line, as a `security` of another shape might.
        self.hide_cdat = False
        #: Print the `"cdat"` line in another shape, as a later `security`
        #: might: "null" (`<NULL>`, no date at all) or "iso" (the item's own
        #: date, as a quoted ISO string rather than the 14-digit blob).
        self.cdat_shape: str | None = None
        if store is not None and store.exists() and not items:
            self.load()
        else:
            for key in self.items:
                self._stamp(key, owner)
            self._save()

    # --- the shared store ------------------------------------------------------

    def load(self) -> FakeSecurity:
        if self.store is None or not self.store.exists():
            return self
        data = json.loads(self.store.read_text())
        self.tick = data["tick"]
        self.items, self.created, self.modified, self.owners = {}, {}, {}, {}
        for row in data["items"]:
            key = (row["service"], row["account"])
            self.items[key] = row["secret"]
            self.created[key] = row["cdat"]
            self.modified[key] = row.get("mdat", row["cdat"])
            self.owners[key] = row["owner"]
        return self

    def _save(self) -> None:
        if self.store is None:
            return
        rows = [
            {"service": s, "account": a, "secret": secret, "cdat": self.created[(s, a)],
             "mdat": self.modified.get((s, a), self.created[(s, a)]), "owner": self.owners[(s, a)]}
            for (s, a), secret in self.items.items()
        ]
        self.store.write_text(json.dumps({"tick": self.tick, "items": rows}))

    def _stamp(self, key: tuple[str, str], owner: str) -> None:
        self.tick += 1
        self.created[key] = self.modified[key] = cdat_for(self.tick)
        self.owners[key] = owner

    def add(self, service: str, account: str, secret: str = "x", *, owner: str = FOREIGN) -> str:
        """Make an item as another program would; returns its cdat."""
        self.load()
        self.items[(service, account)] = secret
        self._stamp((service, account), owner)
        self._save()
        return self.created[(service, account)]

    def remove(self, service: str, account: str) -> None:
        self.load()
        self._forget((service, account))
        self._save()

    def _forget(self, key: tuple[str, str]) -> None:
        self.items.pop(key, None)
        self.created.pop(key, None)
        self.modified.pop(key, None)
        self.owners.pop(key, None)

    # --- `security` ------------------------------------------------------------

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.load()
        try:
            if argv[1:] == ["-i"]:
                data = kwargs.get("input") or b""
                text = data.decode() if isinstance(data, bytes) else str(data)
                self.stdin.append(text)
                return self._interactive(text)
            if argv[1] == "find-generic-password":
                return self._find(argv[2:], text=bool(kwargs.get("text")), timeout=kwargs.get("timeout"))
            if argv[1] == "delete-generic-password":
                return self._delete(argv[2:], timeout=kwargs.get("timeout"))
            raise AssertionError(f"unexpected security call: {argv}")
        finally:
            self._save()

    @staticmethod
    def _options(tokens: list[str]) -> dict[str, str]:
        return dict(zip(tokens[0::2], tokens[1::2], strict=False))

    def _hangs(self, service: str, account: str, timeout: object) -> None:
        if service in self.hang or (service, account) in self.hang:
            raise subprocess.TimeoutExpired(["security"], timeout or 0)

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

    def _find(self, tokens: list[str], *, text: bool, timeout: object = None) -> subprocess.CompletedProcess:
        opts = self._options(tokens)
        service, account = opts.get("-s", ""), opts.get("-a", "")
        if "-a" not in opts:
            # No account: the first item under the service, as `security`
            # answers `find-generic-password -s <service>`.
            account = next((a for (s, a) in self.items if s == service), "")
        key = (service, account)

        def out(s: str):
            return s if text else s.encode()

        self._hangs(service, account, timeout)
        if service in self.find_status:
            return subprocess.CompletedProcess([], self.find_status[service], out(""), out(""))
        if key in self.find_item_status:
            return subprocess.CompletedProcess([], self.find_item_status[key], out(""), out(""))
        if key not in self.items:
            return subprocess.CompletedProcess(
                [], 44, out(""),
                out("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n"),
            )
        if key not in self.created:
            # Put in `items` by hand after construction: it was made now.
            self._stamp(key, FOREIGN)
        secret = self.items[key]
        attrs = attributes_block(service, account, self.created[key], self.modified[key])
        if self.hide_cdat:
            attrs = "".join(line for line in attrs.splitlines(True) if '"cdat"' not in line)
        elif self.cdat_shape is not None:
            made = datetime.strptime(self.created[key], "%Y%m%d%H%M%SZ")
            value = {"null": "<NULL>", "iso": f'"{made:%Y-%m-%d %H:%M:%S} +0000"'}[self.cdat_shape]
            attrs = "".join(f'    "cdat"<timedate>={value}\n' if '"cdat"' in line else line
                            for line in attrs.splitlines(True))
        if "-g" in tokens:
            line = self.password_lines.get(service) or self.password_line(secret)
            return subprocess.CompletedProcess([], 0, out(attrs), out(line + "\n"))
        if "-w" in tokens:
            # Printable secrets as text, anything else as bare hex.
            shown = secret if self._printable(secret) else secret.encode().hex()
            return subprocess.CompletedProcess([], 0, out(shown + "\n"), out(""))
        # Neither: the attributes only, never the secret.
        return subprocess.CompletedProcess([], 0, out(attrs), out(""))

    def _delete(self, tokens: list[str], *, timeout: object = None) -> subprocess.CompletedProcess:
        opts = self._options(tokens)
        key = (opts.get("-s", ""), opts.get("-a", ""))
        self._hangs(*key, timeout)
        if key in self.delete_status:
            return subprocess.CompletedProcess(
                [], self.delete_status[key], b"", b"security: SecKeychainItemDelete: delete refused\n"
            )
        if self.fail_deletes:
            return subprocess.CompletedProcess(
                [], 45, b"", b"security: SecKeychainItemDelete: delete refused\n"
            )
        if key not in self.items:
            return subprocess.CompletedProcess(
                [], 44, b"",
                b"security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n",
            )
        self._forget(key)
        return subprocess.CompletedProcess([], 0, b"", b"")

    def _interactive(self, text: str) -> subprocess.CompletedProcess:
        lines = text.splitlines()
        assert len(lines) == 1, f"one command per call expected, got {lines!r}"
        tokens = shlex.split(lines[0], posix=True)
        assert tokens[0] == "add-generic-password", tokens
        if self.fail_writes:
            return subprocess.CompletedProcess(
                [], 45, b"", b"security: SecKeychainItemCreateFromContent: write refused\n"
            )
        flags = {t for t in tokens[1:] if t in ("-U", "-A")}
        options = [t for t in tokens[1:] if t not in ("-U", "-A")]
        if len(options) % 2:  # a keychain named last (`_write(keychain_path=)`)
            options = options[:-1]
        opts = self._options(options)
        key = (opts["-s"], opts["-a"])
        if key in self.items and "-U" not in flags:
            return subprocess.CompletedProcess([], 45, b"", (
                b"security: SecKeychainItemCreateFromContent: The specified item already "
                b"exists in the keychain.\n"))
        if key in self.items and "-A" in flags:
            # Measured on a throwaway keychain: the password is replaced, then
            # changing the access list needs the keychain password, which a
            # run with nobody to answer is refused, and the command fails.
            self.items[key] = opts["-w"]
            self.access_prompts += 1
            return subprocess.CompletedProcess([], 45, b"", (
                b"security: SecKeychainItemSetAccess: User canceled the operation.\n"
                b"security: SecKeychainItemCreateFromContent: The specified item already "
                b"exists in the keychain.\n"))
        if key in self.items:
            self.items[key] = opts["-w"]
        else:
            self.items[key] = opts["-w"]
            self._stamp(key, "security")
        return subprocess.CompletedProcess([], 0, b"", self.write_stderr_on_success)
