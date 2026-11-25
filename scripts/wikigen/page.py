"""Page chrome (read from the site's index.html) and the wiki page skeleton."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from . import WikiError
from .manifest import Manifest, Page
from .render import Rendered

SITE = "https://graftipa.com"
# The link-preview card at the site root, which the homepage's tags name too. Feeds show
# 1.91:1 without cropping it; build() checks the site's file is this size.
OG_IMAGE = "og.png"
OG_WIDTH, OG_HEIGHT = 1200, 630
OG_ALT = (
    "Graft's icon and name beside “Pick an app. Pick your tweaks. Graft does the rest.”, "
    "above the Graft window with example apps and its Build & Install button."
)
_HEADER = re.compile(r'<header class="bar">.*?</header>', re.DOTALL)
_FOOTER = re.compile(r"<footer>.*?</footer>", re.DOTALL)
_ATTR = re.compile(r"""(\b(?:href|src)=)(["'])(.*?)\2""", re.DOTALL)
_ANCHOR = re.compile(r"<a\b[^>]*>")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_DOCS_HREF = re.compile(r"""\shref=(["'])/wiki/\1""")
_DESCRIPTION_MAX = 160


@dataclass(frozen=True)
class Chrome:
    header: str
    footer: str
    #: The site's analytics snippet (`build.ANALYTICS`), just before `</body>`
    #: on every page; empty without one.
    body_end: str = ""


def _absolute(value: str) -> str:
    if value.startswith("/") or _SCHEME.match(value):
        return value
    if value.startswith("./"):
        value = value[2:]
    return "/" + value


def _rewrite(fragment: str) -> str:
    return _ATTR.sub(lambda m: f"{m[1]}{m[2]}{_absolute(m[3])}{m[2]}", fragment)


def _mark_docs(header: str) -> str:
    def mark(m: re.Match[str]) -> str:
        tag = m[0]
        if _DOCS_HREF.search(tag) and "aria-current" not in tag:
            return f'{tag[:-1]} aria-current="page">'
        return tag

    return _ANCHOR.sub(mark, header)


def read_chrome(index_html: str) -> Chrome:
    headers = _HEADER.findall(index_html)
    if len(headers) != 1 or index_html.count('<header class="bar">') != 1:
        raise WikiError('index.html: expected exactly one <header class="bar">')
    footers = _FOOTER.findall(index_html)
    if len(footers) != 1 or index_html.count("<footer") != 1:
        raise WikiError("index.html: expected exactly one <footer>")
    header = _rewrite(headers[0])
    if not any(_DOCS_HREF.search(a) for a in _ANCHOR.findall(header)):
        raise WikiError("index.html: the header has no Docs link to wiki/")
    return Chrome(header, _rewrite(footers[0]))


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def description(first_paragraph: str) -> str:
    text = " ".join(first_paragraph.split())
    if len(text) <= _DESCRIPTION_MAX:
        return text
    cut = text[:_DESCRIPTION_MAX].rfind(" ")
    return text[: cut if cut > 0 else _DESCRIPTION_MAX] + "…"


def _sidebar(page: Page, manifest: Manifest) -> str:
    def link(p: Page) -> str:
        current = ' aria-current="page"' if p == page else ""
        return f'<a href="{_esc(p.url_path)}"{current}>{_esc(p.nav)}</a>'

    parts = [link(manifest.home)]
    for name, pages in manifest.groups:
        parts.append(f'<p class="sidebar-label">{_esc(name)}</p>')
        parts.extend(link(p) for p in pages)
    return "\n".join(parts)


def _pager(page: Page, manifest: Manifest) -> str:
    pages = manifest.pages()
    i = pages.index(page)
    links = []
    if i > 0:
        prev = pages[i - 1]
        links.append(f'<a rel="prev" href="{_esc(prev.url_path)}">← {_esc(prev.nav)}</a>')
    if i < len(pages) - 1:
        nxt = pages[i + 1]
        links.append(f'<a rel="next" href="{_esc(nxt.url_path)}">{_esc(nxt.nav)} →</a>')
    return f'<nav class="pager">{"".join(links)}</nav>'


def render_page(page: Page, body: Rendered, manifest: Manifest, chrome: Chrome) -> str:
    title = "Graft Docs" if not page.slug else f"{page.title} | Graft Docs"
    url = f"{SITE}{page.url_path}"
    desc = _esc(description(body.first_paragraph))
    crumb = ""
    if page.group is not None:
        crumb = (
            '<nav class="breadcrumb" aria-label="Breadcrumb">'
            f'<a href="/wiki/">Docs</a> \u203a {_esc(page.group)}</nav>\n'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{_esc(url)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{_esc(url)}">
<meta property="og:site_name" content="Graft">
<meta property="og:image" content="{SITE}/{OG_IMAGE}">
<meta property="og:image:width" content="{OG_WIDTH}">
<meta property="og:image:height" content="{OG_HEIGHT}">
<meta property="og:image:alt" content="{_esc(OG_ALT)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{SITE}/{OG_IMAGE}">
<link rel="icon" href="/favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:ital,wght@0,400..900;1,400..900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css">
<link rel="stylesheet" href="/wiki/wiki.css">
<script src="/wiki/wiki.js" defer></script>
<script>document.documentElement.classList.add("js")</script>
</head>
<body>
<div class="wrap">
{_mark_docs(chrome.header)}
<div class="wiki-layout">
<button class="wiki-nav-toggle" aria-expanded="false" aria-controls="wiki-sidebar">Contents</button>
<aside id="wiki-sidebar" class="wiki-sidebar">
<input type="search" class="search-input" placeholder="Search docs" aria-label="Search docs" autocomplete="off">
<div class="search-results" role="listbox" aria-label="Search results"></div>
{_sidebar(page, manifest)}
</aside>
<main class="wiki-content">
{crumb}<h1>{_esc(page.title)}</h1>
{body.html}{_pager(page, manifest)}
</main>
</div>
{chrome.footer}
</div>
{chrome.body_end}</body>
</html>
"""
