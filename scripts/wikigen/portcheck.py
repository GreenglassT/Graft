"""Temporary: prove nothing in the README was dropped when it moved into the wiki.

Deleted in the same commit that shrinks the README.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .render import _inline_text, _md, slugify

_SKIPPED = {"Disclaimer", "License", "Support"}


def _slug(text: str) -> str:
    return slugify(text, {})


def _wiki_slugs(texts: Sequence[str]) -> set[str]:
    found = set()
    for text in texts:
        tokens = _md().parse(text)
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open":
                found.add(_slug(_inline_text(tokens[i + 1])))
    return found


def missing(readme: str, wiki_texts: Sequence[str], only: Sequence[str] = ()) -> list[str]:
    """Items from the README absent from the wiki, in README order, each once."""
    wiki = "\n".join(wiki_texts)
    slugs = _wiki_slugs(wiki_texts)
    items: list[str] = []  # code, fence and url lines, in README order
    heads: list[str] = []
    tokens = _md().parse(readme)
    title: str | None = None  # None until the first H2
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            text = _inline_text(tokens[i + 1])
            if tok.tag == "h2":
                title = text
            if title is None or title in _SKIPPED or (only and title not in only):
                continue
            if tok.tag in ("h2", "h3", "h4") and _slug(text) not in slugs:
                heads.append(f"heading: {_slug(text)}")
            continue
        if title is None or title in _SKIPPED or (only and title not in only):
            continue
        if tok.type == "fence":
            for line in tok.content.splitlines():
                line = line.strip()
                if line and line not in wiki:
                    items.append(f"fence: {line}")
        elif tok.type == "inline":
            for child in tok.children or []:
                if child.type == "code_inline" and child.content not in wiki:
                    items.append(f"code: {child.content}")
                elif child.type == "link_open":
                    href = str(child.attrGet("href") or "")
                    if href.startswith(("http://", "https://")) and href not in wiki:
                        items.append(f"url: {href}")
    out: list[str] = []
    for line in [*items, *heads]:
        if line not in out:
            out.append(line)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wikigen.portcheck")
    ap.add_argument("readme", type=Path)
    ap.add_argument("wiki", type=Path, help="folder of wiki *.md files (not recursive)")
    ap.add_argument("extra", type=Path, nargs="*", help="further files read as wiki text")
    ap.add_argument("--only", nargs="+", default=[], metavar="TITLE")
    args = ap.parse_args(argv)
    texts = [p.read_text(encoding="utf-8") for p in sorted(args.wiki.glob("*.md"))]
    texts += [p.read_text(encoding="utf-8") for p in args.extra]
    lines = missing(args.readme.read_text(encoding="utf-8"), texts, args.only)
    blocking = [x for x in lines if not x.startswith("heading: ")]
    heads = [x for x in lines if x.startswith("heading: ")]
    for line in blocking:
        print(line)
    if heads:
        print("headings not found (informational):")
        for line in heads:
            print(line)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
