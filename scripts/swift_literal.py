#!/usr/bin/env python3
"""Whether a folder's Swift code has a string literal that a Release build
compiles: not in a comment, nor inside another string, nor in an `#if`
branch that a Release build leaves out.

    .venv/bin/python scripts/swift_literal.py <folder> <text>

Exits 0 when a `*.swift` file under <folder> has the literal "<text>" in
code a Release build compiles, 1 when none has, and 2 for a usage error.
graft-release asks it, before an Intel release, whether the app's own code
(Graft/Graft) reads Info.plist's GraftBundleArch: the Intel bundle's
executable is universal, so an app that doesn't would show Intel Macs the
old "needs Apple silicon" screen, from a bundle that verify-bundle passes.

A tripwire against building from a commit without the app's Intel support,
not a compiler. It reads Swift's tokens as far as the question needs
(`scan`):

- comments: `//` to the end of its line, and `/* */`, which nest;
- string literals: `"..."`, multi-line `\"\"\"...\"\"\"`, and raw ones with
  any number of `#` (`#"..."#`, `##"..."##`), whose escapes and
  interpolations take that many `#` after the backslash. An interpolation,
  `\\(...)`, is code again, with strings and comments of its own;
- extended regex literals (`#/.../#`), so a quote in one opens no string;
- `#if`, `#elseif`, `#else` and `#endif`, where they are code.

A branch is compiled when its condition is true and every earlier one's
false, in a block that is compiled itself. A condition is read as a Release
build of the app reads it: `true`, `false` and `DEBUG` (false: only the
Debug configuration sets it), with `!`, `&&`, `||` and parentheses.
Anything else (`os(macOS)`, `arch(x86_64)`, `canImport(AppKit)`,
`compiler(>=6)`, any other flag) may be either, and so is a condition that
needs it, unless the rest decides (`false && os(macOS)` is false). A
literal counts only in code that is certainly compiled, never in code that
might not be.

The literal is a one-line string, plain or raw, that is exactly <text>
with no escape or interpolation, as the app writes the key:
`"GraftBundleArch"`. A multi-line string that mentions it doesn't count,
nor the name as an identifier. One in a compile-time directive does
(`#warning("GraftBundleArch")`, `#sourceLocation(file: ...)`): a limit
accepted for a tripwire.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

#: A compiler directive, once a `#` in code opens no raw string or regex.
_DIRECTIVE = re.compile(r"#(elseif|else|endif|if)\b")
#: A condition's tokens: the operators, parentheses, a name, or any one
#: other character (what `os(...)`'s arguments are made of).
_TOKEN = re.compile(r"\s*(&&|\|\||[!()]|[A-Za-z_][A-Za-z0-9_]*|\S)")
#: What a Release build of the app says each of these is. Every other name
#: is unknown: may be either.
_KNOWN = {"true": True, "false": False, "DEBUG": False}

#: True, False, or None for "may be either".
Maybe = bool | None


def _not(a: Maybe) -> Maybe:
    return None if a is None else not a


def _and(a: Maybe, b: Maybe) -> Maybe:
    if a is False or b is False:
        return False
    return True if a is True and b is True else None


def _or(a: Maybe, b: Maybe) -> Maybe:
    if a is True or b is True:
        return True
    return False if a is False and b is False else None


class _Unreadable(Exception):
    """A condition that isn't one this reads: it may be either."""


def evaluate(condition: str) -> Maybe:
    """What a Release build makes of an `#if` or `#elseif` condition: True,
    False, or None when it may be either (anything but `_KNOWN`'s names,
    `!`, `&&`, `||` and parentheses decides it, or it can't be read)."""
    tokens = _TOKEN.findall(condition)
    at = 0

    def peek() -> str | None:
        return tokens[at] if at < len(tokens) else None

    def take() -> str:
        nonlocal at
        if at >= len(tokens):
            raise _Unreadable
        at += 1
        return tokens[at - 1]

    def either() -> Maybe:
        value = both()
        while peek() == "||":
            take()
            value = _or(value, both())
        return value

    def both() -> Maybe:
        value = unary()
        while peek() == "&&":
            take()
            value = _and(value, unary())
        return value

    def unary() -> Maybe:
        if peek() == "!":
            take()
            return _not(unary())
        token = take()
        if token == "(":
            value = either()
            if take() != ")":
                raise _Unreadable
            return value
        if not (token[0].isalpha() or token[0] == "_"):
            raise _Unreadable
        if peek() == "(":
            # `os(macOS)`, `canImport(AppKit)`, `compiler(>=6)`: skipped to
            # its closing parenthesis, and unknown.
            depth = 0
            while True:
                part = take()
                depth += {"(": 1, ")": -1}.get(part, 0)
                if depth == 0:
                    return None
        return _KNOWN.get(token)

    try:
        value = either()
    except _Unreadable:
        return None
    return value if at == len(tokens) else None


class Literal(NamedTuple):
    """A one-line string literal: its text between the quotes, or None when
    it has an escape or an interpolation; and whether a Release build
    certainly compiles it."""

    text: str | None
    compiled: bool


class _Block:
    """An open `#if` block: whether some branch before the open one is
    taken, and whether the open one is. Not a dataclass: graft-release's
    tests load this file without a module of its own, which a dataclass
    needs to read its annotations."""

    def __init__(self, value: Maybe) -> None:
        self.taken: Maybe = value
        self.active: Maybe = value


class _Scanner:
    def __init__(self, swift: str) -> None:
        self.s = swift
        self.n = len(swift)
        self.i = 0
        self.blocks: list[_Block] = []
        self.literals: list[Literal] = []

    def compiled(self) -> bool:
        return all(block.active is True for block in self.blocks)

    def at(self, text: str) -> bool:
        return self.s.startswith(text, self.i)

    def hashes(self) -> int:
        """How many `#` start here."""
        j = self.i
        while j < self.n and self.s[j] == "#":
            j += 1
        return j - self.i

    def code(self, interpolation: bool = False) -> None:
        """Code from here to the end, or for an `interpolation` to the `)`
        that closes it (past which it leaves `i`)."""
        depth = 0
        while self.i < self.n:
            c = self.s[self.i]
            if self.at("//"):
                self.line_comment()
            elif self.at("/*"):
                self.block_comment()
            elif c == '"':
                self.string(0)
            elif c == "#":
                k = self.hashes()
                after = self.s[self.i + k:self.i + k + 1]
                if after == '"':
                    self.string(k)
                elif after == "/":
                    self.regex(k)
                elif m := _DIRECTIVE.match(self.s, self.i):
                    # In an interpolation too: a closure there can hold a
                    # whole #if block, which swiftc accepts.
                    self.i = m.end()
                    self.directive(m.group(1))
                else:
                    self.i += k
            elif c == "(":
                depth += 1
                self.i += 1
            elif c == ")":
                self.i += 1
                if interpolation and depth == 0:
                    return
                depth -= 1
            else:
                self.i += 1

    def line_comment(self) -> None:
        end = self.s.find("\n", self.i)
        self.i = self.n if end < 0 else end

    def block_comment(self) -> None:
        """A `/* */` comment, those nested in it included. Nothing inside is
        a string: the first `*/` that matches closes it."""
        depth = 0
        while self.i < self.n:
            if self.at("/*"):
                depth += 1
                self.i += 2
            elif self.at("*/"):
                depth -= 1
                self.i += 2
                if depth == 0:
                    return
            else:
                self.i += 1

    def string(self, k: int, *, record: bool = True) -> None:
        """The string literal starting here, after `k` `#`: one-line or
        multi-line, its escapes and interpolations taking `k` `#` after the
        backslash. A one-line one is recorded (`literals`) when `record`."""
        self.i += k
        multi = self.at('"""') and not self.one_line_raw(k)
        self.i += 3 if multi else 1
        close = ('"""' if multi else '"') + "#" * k
        escape = "\\" + "#" * k
        start, plain = self.i, True
        while self.i < self.n:
            if self.at(close):
                if record and not multi:
                    self.literals.append(
                        Literal(self.s[start:self.i] if plain else None, self.compiled()))
                self.i += len(close)
                return
            if self.at(escape):
                self.i += len(escape)
                plain = False
                if self.at("("):
                    self.i += 1
                    self.code(interpolation=True)
                elif self.i < self.n and (multi or self.s[self.i] != "\n"):
                    self.i += 1
                continue
            if self.s[self.i] == "\n" and not multi:
                # Left open, which Swift refuses: it ends with its line.
                return
            self.i += 1

    def one_line_raw(self, k: int) -> bool:
        """Whether the three quotes here, after `k` `#`, open a one-line raw
        string rather than a multi-line one: as Swift's lexer has it, when the
        rest of their line, from the third quote, has a `"` with `k` `#` after
        it. So `#` + three quotes + `#` is a one-line string holding a quote,
        as swiftc compiles it, and `#` + three quotes + `a"#` one holding
        two quotes and an `a`."""
        if not k:
            return False
        end = self.s.find("\n", self.i)
        line = self.s[self.i + 2:self.n if end < 0 else end]
        return '"' + "#" * k in line

    def regex(self, k: int) -> None:
        """An extended regex literal, `#/.../#`, with `k` `#`. One that
        starts with a line break may span lines; any other ends with its
        line."""
        self.i += k + 1
        close = "/" + "#" * k
        multi = self.at("\n")
        while self.i < self.n:
            if self.at(close):
                self.i += len(close)
                return
            if self.s[self.i] == "\n" and not multi:
                return
            self.i += 1

    def condition(self) -> Maybe:
        """The condition after `#if` or `#elseif`, read on from here: to the
        end of its line, without its comments, and past a line break only
        inside an open parenthesis. swiftc refuses an `&&` or `||` that ends
        a line, or starts the next one, outside parentheses."""
        parts: list[str] = []
        while self.i < self.n:
            c = self.s[self.i]
            if self.at("//"):
                self.line_comment()
            elif self.at("/*"):
                self.block_comment()
                parts.append(" ")
            elif c == '"':
                # Only `canImport`'s version has one, which is unknown
                # anyway; a string here is no literal in code.
                self.string(0, record=False)
                parts.append("_")
            elif c == "\n":
                text = "".join(parts)
                if text.count("(") > text.count(")"):
                    parts.append(" ")
                    self.i += 1
                else:
                    break
            else:
                parts.append(c)
                self.i += 1
        return evaluate("".join(parts))

    def directive(self, word: str) -> None:
        if word == "if":
            value = self.condition()
            self.blocks.append(_Block(value))
        elif not self.blocks:
            # An `#elseif`, `#else` or `#endif` with no `#if`: Swift refuses
            # it, and it changes nothing here.
            if word == "elseif":
                self.condition()
        elif word == "elseif":
            block = self.blocks[-1]
            value = self.condition()
            block.active = _and(_not(block.taken), value)
            block.taken = _or(block.taken, value)
        elif word == "else":
            block = self.blocks[-1]
            block.active = _not(block.taken)
            block.taken = True
        else:
            self.blocks.pop()


def scan(swift: str) -> list[Literal]:
    """Every one-line string literal in Swift source `swift`, in order."""
    scanner = _Scanner(swift)
    scanner.code()
    return scanner.literals


def names_literal(swift: str, text: str) -> bool:
    """Whether Swift source `swift` has the string literal "`text`" in code
    a Release build certainly compiles (`scan`)."""
    return any(lit.compiled and lit.text == text for lit in scan(swift))


def files_naming(folder: Path, text: str) -> list[Path]:
    """Each `*.swift` file under `folder`, at any depth, that names `text`
    as a literal (`names_literal`), in order."""
    return [p for p in sorted(folder.rglob("*.swift"))
            if p.is_file() and names_literal(p.read_text(encoding="utf-8", errors="replace"), text)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: swift_literal.py <folder> <text>", file=sys.stderr)
        return 2
    folder = Path(argv[0])
    if not folder.is_dir():
        print(f"swift_literal: no folder {folder}", file=sys.stderr)
        return 2
    return 0 if files_naming(folder, argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
