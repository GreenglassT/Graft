#!/usr/bin/env python3
"""Whether a folder's Swift code has a string literal where it is compiled:
not in a comment, nor in an `#if false` block.

    .venv/bin/python scripts/swift_literal.py <folder> <text>

Exits 0 when a `*.swift` file under <folder> has the literal "<text>" in its
code, and 1 when none has. graft-release asks it, before an Intel release,
whether the app's own code (Graft/Graft) reads Info.plist's GraftBundleArch:
the Intel bundle's executable is universal, so an app that doesn't would
show Intel Macs the old "needs Apple silicon" screen, from a bundle that
verify-bundle passes.

A tripwire against building from a commit without the app's Intel support,
not a Swift parser. It drops comments (`//`, and `/* */`, which nest in
Swift) outside string literals, then every line from `#if false` to its
`#endif` (counting the `#if`s nested in it; its `#else` is compiled), and
looks for the literal in what is left. A raw string (`#"..."#`) is read as
a plain one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: An `#if false` line, once its comments are gone.
_IF_FALSE = re.compile(r"#if\s+false")
_IF = re.compile(r"#if\b")
_ELSE = re.compile(r"#(?:else|elseif)\b")
_ENDIF = re.compile(r"#endif\b")


def strip_comments(swift: str) -> str:
    """`swift` without its comments: each `//` comment to its line's end,
    and each `/* */` (nested ones included) but for its newlines, so the
    lines stay where they were. A `//` or `/*` inside a string literal is
    the string's: a `"` opens one (`\"` inside it doesn't close it), and
    `\"\"\"` a multi-line one."""
    out: list[str] = []
    i, n = 0, len(swift)
    depth = 0  # how many /* */ are open
    quote = ""  # the open string literal's delimiter, if any
    while i < n:
        c = swift[i]
        if depth:
            if swift.startswith("/*", i):
                depth, i = depth + 1, i + 2
            elif swift.startswith("*/", i):
                depth, i = depth - 1, i + 2
            else:
                out.append("\n" if c == "\n" else "")
                i += 1
        elif quote:
            if c == "\\" and i + 1 < n:
                out.append(swift[i:i + 2])
                i += 2
            elif swift.startswith(quote, i):
                out.append(quote)
                i, quote = i + len(quote), ""
            else:
                # A one-line string never spans lines: one left open ends here.
                if c == "\n" and quote == '"':
                    quote = ""
                out.append(c)
                i += 1
        elif swift.startswith("//", i):
            end = swift.find("\n", i)
            i = n if end < 0 else end
        elif swift.startswith("/*", i):
            out.append(" ")
            depth, i = 1, i + 2
        elif swift.startswith('"""', i):
            out.append('"""')
            quote, i = '"""', i + 3
        else:
            if c == '"':
                quote = '"'
            out.append(c)
            i += 1
    return "".join(out)


def drop_if_false(swift: str) -> str:
    """`swift`'s lines without those `#if false` leaves out: from it to its
    `#endif`, counting the `#if`s nested in it, or to its `#else` or
    `#elseif`, which may be compiled. For text whose comments are gone
    (`strip_comments`)."""
    kept: list[str] = []
    depth = 0  # how deep in a `#if false` block, 0 outside one
    for line in swift.splitlines():
        s = line.strip()
        if depth:
            if _IF.match(s):
                depth += 1
            elif _ENDIF.match(s):
                depth -= 1
            elif depth == 1 and _ELSE.match(s):
                depth = 0
        elif _IF_FALSE.fullmatch(s):
            depth = 1
        else:
            kept.append(line)
    return "\n".join(kept)


def names_literal(swift: str, text: str) -> bool:
    """Whether Swift source `swift` has the string literal "`text`" in code
    that is compiled (outside comments and `#if false`)."""
    return f'"{text}"' in drop_if_false(strip_comments(swift))


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
