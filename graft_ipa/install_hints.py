"""What to say before an install command a new Mac cannot run yet.

Most of Graft's install hints are `brew install …`, and a Mac without
Homebrew answers that with "command not found" and nothing else. So a hint
built here says what has to come first: Homebrew (or MacPorts, for the
libimobiledevice tools it carries), pipx for cyan. The command itself stays
exactly what it was — the GUI copies it from the `install` field — and the
note goes after it.

Homebrew is looked for where it installs as well as on PATH: a CLI the Mac app
starts may have a PATH without `/opt/homebrew/bin`, and telling someone with
Homebrew to install it would be wrong.
"""

from __future__ import annotations

from pathlib import Path

from .child_env import which

HOMEBREW_URL = "https://brew.sh"

#: Where Homebrew puts `brew`: Apple silicon, then Intel.
HOMEBREW_LOCATIONS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")

#: Where the Go toolchain lands: Homebrew's two prefixes, then go.dev's installer.
GO_LOCATIONS = ("/opt/homebrew/bin/go", "/usr/local/bin/go", "/usr/local/go/bin/go")


def _found(name: str, locations: tuple[str, ...]) -> bool:
    return which(name) is not None or any(Path(p).is_file() for p in locations)


def homebrew_present() -> bool:
    return _found("brew", HOMEBREW_LOCATIONS)


def pipx_present() -> bool:
    return _found("pipx", ("/opt/homebrew/bin/pipx", "/usr/local/bin/pipx"))


def go_present() -> bool:
    return _found("go", GO_LOCATIONS)


def homebrew_note(*, macports: str | None = None) -> str | None:
    """"Homebrew is needed first: …", or None when it is installed."""
    if homebrew_present():
        return None
    note = f"Homebrew is needed first: {HOMEBREW_URL}"
    if macports:
        note += f" (or, with MacPorts: {macports})"
    return note


def with_prerequisites(command: str, *, macports: str | None = None) -> str:
    """`command`, followed by what must be installed before it can run."""
    notes: list[str] = []
    if command.startswith("pipx ") and not pipx_present():
        notes.append("pipx is needed first: brew install pipx")
    if command.startswith("brew ") or notes:
        note = homebrew_note(macports=macports)
        if note:
            notes.append(note)
    return command if not notes else f"{command} — {'; '.join(notes)}"
