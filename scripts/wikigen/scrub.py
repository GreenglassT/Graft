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

from . import WikiError

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


def load_allowed(path: Path) -> tuple[str, ...]:
    """The list's `#public-allow: <phrase>` lines: exact phrases a public page may
    carry although they hold a listed term (a project's own invite link, say).
    scan() blanks each wherever it appears before looking for the terms, so the
    same term anywhere else is still a hit. scripts/denylist.py reads them too."""
    phrases: list[str] = []
    terms = load_denylist(path)
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        m = re.fullmatch(r"#\s*public-allow:\s*(.*)", raw.strip())
        if m and m[1].strip():
            if _only_a_term(m[1], terms):
                raise WikiError(
                    f"{path}, line {number}: a #public-allow phrase must be more than a listed "
                    "term, or it would let the bare term through everywhere"
                )
            phrases.append(m[1].strip())
    return tuple(phrases)


def _only_a_term(phrase: str, terms: Sequence[str]) -> bool:
    """scripts/denylist.py's only_a_term: with every listed term taken out
    (case and whitespace aside), no letter or digit is left."""
    rest = " ".join(phrase.split()).lower()
    for term in terms:
        t = " ".join(term.split()).lower()
        if t and t in rest and not any(ch.isalnum() for ch in rest.replace(t, "")):
            return True
    return False


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Unbounded, so a text view that joins words across tags still matches."""
    return re.compile(r"\s+".join(re.escape(word) for word in phrase.split()), re.IGNORECASE)


def _blank(text: str, allowed: Sequence[str]) -> str:
    """`text` with each allowed phrase replaced by spaces (newlines kept), so
    positions and line numbers don't move."""
    for phrase in allowed:
        text = _phrase_pattern(phrase).sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)
    return text


def _term_pattern(term: str) -> re.Pattern[str]:
    if not term.strip():
        raise ValueError("empty scrub term would match everywhere")
    body = r"\s+".join(re.escape(word) for word in term.split())
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def scan(where: str, text: str, terms: Sequence[str], allowed: Sequence[str] = ()) -> list[Hit]:
    """Every hit in `text`, in position order. Built-ins and terms are checked together.
    Terms are looked for with each `allowed` phrase blanked out (load_allowed);
    the built-ins always see the text as it is.

    It matches the text it is given, literally. It does not decode HTML entities or strip
    tags, so callers scanning HTML must also pass a text view of the page with tags removed
    and entities unescaped.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = list(BUILTIN)
    patterns += [(term, _term_pattern(term)) for term in terms]

    masked = _blank(text, allowed)
    found: list[tuple[int, int, str]] = []  # (start, pattern index, term name)
    for index, (name, pattern) in enumerate(patterns):
        for match in pattern.finditer(text if index < len(BUILTIN) else masked):
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
