"""Render one wiki Markdown file to HTML, collecting headings, search sections and images."""

from __future__ import annotations

import html
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import WikiError

_DROP = re.compile(r"[^\w\s-]")
_URL = re.compile(r"https?://")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    id: str


@dataclass(frozen=True)
class Section:
    heading: Heading | None  # None for the text before the first H2
    text: str


@dataclass(frozen=True)
class Rendered:
    html: str
    headings: tuple[Heading, ...]
    sections: tuple[Section, ...]
    first_paragraph: str
    images: tuple[Path, ...]
    links: tuple[str, ...]


def slugify(text: str, seen: dict[str, int]) -> str:
    """GitHub's heading id rule, with -N suffixes for repeats."""
    slug = _DROP.sub("", text.lower()).replace(" ", "-")
    if slug in seen:
        seen[slug] += 1
        return f"{slug}-{seen[slug] - 1}"
    seen[slug] = 1
    return slug


def _md() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def _inline_text(token: Token) -> str:
    parts = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _headings(tokens: list[Token]) -> list[tuple[int, Heading]]:
    """(index of heading_open, Heading) for every H2 to H4, with GitHub's ids.

    Shared by anchors() and render_markdown() so the two can never disagree. The H1 takes part
    in the repeat count, as it does on GitHub, but is not returned.
    """
    seen: dict[str, int] = {}
    found = []
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        level = int(tok.tag[1])
        text = _collapse(_inline_text(tokens[i + 1]))
        slug = slugify(text, seen)
        if 2 <= level <= 4:
            found.append((i, Heading(level, text, slug)))
    return found


def anchors(md_text: str) -> frozenset[str]:
    return frozenset(h.id for _, h in _headings(_md().parse(md_text)))


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise WikiError(f"{path}: not a PNG")
    return struct.unpack(">II", head[16:24])


def _figure(img: Token, source: Path, image_dir: Path) -> tuple[str, Path]:
    src = img.attrGet("src") or ""
    alt = img.content.strip()
    if not alt:
        raise WikiError(f"{source}: image {src} has no alt text")
    light = (source.parent / src).resolve()
    if light.parent != image_dir.resolve() or light.suffix != ".png":
        raise WikiError(f"{source}: image {src} must be a .png directly in {image_dir}")
    stem = light.name.removesuffix(".png")
    if stem.endswith("-dark"):
        raise WikiError(
            f"{source}: image {src} is a dark twin; use {stem.removesuffix('-dark')}.png "
            f"for a single-theme image or {stem.removesuffix('-dark')}-light.png for a pair"
        )
    if not light.is_file():
        raise WikiError(f"{source}: image {src} does not exist")
    width, height = _png_size(light)
    title = img.attrGet("title")
    caption = f"<figcaption>{html.escape(str(title))}</figcaption>" if title else ""
    tag = (
        f'<img src="/wiki/img/{html.escape(light.name)}" alt="{html.escape(alt)}" '
        f'width="{width}" height="{height}" loading="lazy">'
    )
    if not stem.endswith("-light"):
        return f'<figure class="wiki-shot">{tag}{caption}</figure>\n', light
    dark = light.with_name(stem.removesuffix("-light") + "-dark.png")
    if not dark.is_file():
        raise WikiError(f"{source}: image {src} has no dark twin {dark.name}")
    dark_width, dark_height = _png_size(dark)
    if (dark_width, dark_height) != (width, height):
        raise WikiError(
            f"{source}: {dark.name} is {dark_width}x{dark_height}, "
            f"its light twin is {width}x{height}"
        )
    out = (
        '<figure class="wiki-shot"><picture>'
        f'<source srcset="/wiki/img/{html.escape(dark.name)}" '
        'media="(prefers-color-scheme: dark)">'
        f"{tag}</picture>{caption}</figure>\n"
    )
    return out, light


def _html_block(content: str) -> Token:
    tok = Token("html_block", "", 0)
    tok.content = content
    return tok


def render_markdown(
    md_text: str,
    *,
    source: Path,
    resolve_link: Callable[[str], str],
    image_dir: Path,
) -> Rendered:
    md = _md()
    tokens = md.parse(md_text)

    ids = dict(_headings(tokens))
    h1_seen = False
    first_paragraph = ""
    links: list[str] = []
    images: list[Path] = []
    sections: list[Section] = []
    out: list[Token] = []
    current: Heading | None = None
    buf: list[str] = []
    before_h2 = True

    def flush() -> None:
        text = _collapse(" ".join(buf))
        if current is not None or text:
            sections.append(Section(current, text))
        buf.clear()

    def process_inline(tok: Token) -> None:
        """Resolve links and reject bare URLs and unmanaged images in one inline token."""
        link_depth = 0
        for child in tok.children or []:
            if child.type == "image":
                raise WikiError(f"{source}: an image must be alone in its paragraph")
            if child.type == "link_close":
                link_depth -= 1
            elif child.type == "text" and link_depth == 0 and _URL.search(child.content):
                raise WikiError(f"{source}: bare URL in text: {child.content.strip()!r}")
            if child.type == "link_open":
                link_depth += 1
                href = child.attrGet("href") or ""
                links.append(href)
                child.attrSet("href", resolve_link(href))

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            inline = tokens[i + 1]
            if tok.tag == "h1" and not h1_seen:
                h1_seen = True
                i += 3
                continue
            process_inline(inline)
            heading = ids.get(i)
            if heading is not None:
                tok.attrSet("id", heading.id)
                if heading.level <= 3:
                    flush()
                    current = heading
                    before_h2 = False
                else:
                    buf.append(heading.text)
            else:
                buf.append(_inline_text(inline))
            out.extend(tokens[i : i + 3])
            i += 3
            continue
        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            kids = inline.children or []
            if before_h2 and not first_paragraph and h1_seen:
                first_paragraph = _collapse(_inline_text(inline))
            images_here = [c for c in kids if c.type == "image"]
            if images_here:
                if len(kids) != 1:
                    raise WikiError(f"{source}: an image must be alone in its paragraph")
                block, light = _figure(kids[0], source, image_dir)
                images.append(light)
                out.append(_html_block(block))
                i += 3
                continue
        if tok.type == "inline":
            process_inline(tok)
            buf.append(_inline_text(tok))
        elif tok.type in ("fence", "code_block"):
            buf.append(tok.content)
        if tok.type == "table_open":
            out.append(_html_block('<div class="table-wrap">'))
        out.append(tok)
        if tok.type == "table_close":
            out.append(_html_block("</div>\n"))
        i += 1
    flush()

    if not first_paragraph:
        raise WikiError(f"{source}: no paragraph between the H1 and the first H2")

    body = md.renderer.render(out, md.options, {})
    return Rendered(
        html=body,
        headings=tuple(h for _, h in sorted(ids.items())),
        sections=tuple(sections),
        first_paragraph=first_paragraph,
        images=tuple(images),
        links=tuple(links),
    )
