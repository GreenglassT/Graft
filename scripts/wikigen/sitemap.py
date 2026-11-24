"""Merge the wiki's URLs into the site's sitemap.xml."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from . import WikiError

WIKI_PREFIX = "https://graftipa.com/wiki/"

_URLSET = re.compile(r"<urlset\b[^>]*>")
_URL = re.compile(r"<url>.*?</url>", re.DOTALL)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)


def merge_sitemap(xml: str, wiki_urls: Sequence[str], lastmod: str) -> str:
    opening = _URLSET.search(xml)
    if opening is None:
        raise WikiError("sitemap.xml: no <urlset> element")
    kept = []
    for block in _URL.findall(xml, opening.end()):
        loc = _LOC.search(block)
        if loc is None or not html.unescape(loc.group(1)).startswith(WIKI_PREFIX):
            kept.append(block)
    for url in wiki_urls:
        kept.append(
            "<url>\n"
            f"    <loc>{html.escape(url, quote=False)}</loc>\n"
            f"    <lastmod>{html.escape(lastmod, quote=False)}</lastmod>\n"
            "  </url>"
        )
    body = "".join(f"  {block}\n" for block in kept)
    return f"{xml[: opening.end()]}\n{body}</urlset>\n"
