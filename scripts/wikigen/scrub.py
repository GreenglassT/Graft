"""Scan rendered wiki HTML for leaks before the site is written.

Two layers: built-in patterns that need no private knowledge (a home-directory
path, a new-style UDID), and a private word list that lives outside the repo.
Old-style 40-hex UDIDs look like git SHAs, so they belong in the word list.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_TEXT_LIMIT = 120

# `<you>` is HTML-escaped to `&lt;you&gt;` before the scan sees it.
BUILTIN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("/Users/ path", re.compile(r"/Users/(?!<|&lt;)[^/\s<&]+")),
    ("UDID", re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b")),
)


@dataclass(frozen=True)
class Hit:
    where: str
    line: int  # 1-based line where the hit starts
    term: str  # a BUILTIN name, or the denylist term
    text: str  # that line, stripped, cut to 120 characters


def load_denylist(path: Path) -> tuple[str, ...]:
    """One term per line. A `#` at the start of a line (after whitespace) comments it out."""
    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return tuple(terms)


def _term_pattern(term: str) -> re.Pattern[str]:
    if not term.strip():
        raise ValueError("empty scrub term would match everywhere")
    body = r"\s+".join(re.escape(word) for word in term.split())
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def scan(where: str, text: str, terms: Sequence[str]) -> list[Hit]:
    """Every hit in `text`, in position order. Built-ins and terms are checked together.

    It matches the text it is given, literally. It does not decode HTML entities or strip
    tags, so callers scanning HTML must also pass a text view of the page with tags removed
    and entities unescaped.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = list(BUILTIN)
    patterns += [(term, _term_pattern(term)) for term in terms]

    found: list[tuple[int, int, str]] = []  # (start, pattern index, term name)
    for index, (name, pattern) in enumerate(patterns):
        for match in pattern.finditer(text):
            found.append((match.start(), index, name))
    found.sort()

    lines = text.split("\n")
    hits: list[Hit] = []
    for start, _index, name in found:
        line_no = text.count("\n", 0, start) + 1
        hits.append(
            Hit(
                where=where,
                line=line_no,
                term=name,
                text=lines[line_no - 1].strip()[:_TEXT_LIMIT],
            )
        )
    return hits
