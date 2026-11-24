"""Load docs/wiki/wiki.toml into an ordered page list."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import WikiError

_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


@dataclass(frozen=True)
class Page:
    slug: str  # "" for home
    title: str  # the source's H1 text
    nav: str  # manifest nav, or the title
    group: str | None  # None for home
    source: Path  # absolute, resolved
    url_path: str  # /wiki/ or /wiki/<slug>/


@dataclass(frozen=True)
class Manifest:
    home: Page
    groups: tuple[tuple[str, tuple[Page, ...]], ...]

    def pages(self) -> tuple[Page, ...]:
        return (self.home, *(p for _, pages in self.groups for p in pages))


def _title(source: Path) -> str:
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
        break
    raise WikiError(f'{source}: the first non-blank line must be a "# " heading')


def _page(wiki_dir: Path, entry: dict, slug: str, group: str | None, manifest: Path) -> Page:
    src = entry.get("source")
    if not isinstance(src, str) or not src:
        raise WikiError(f"{manifest}: page {slug or 'home'!r} has no source")
    source = (wiki_dir / src).resolve()
    if not source.is_file():
        raise WikiError(f"{source}: source file not found (listed in wiki.toml)")
    title = _title(source)
    url = "/wiki/" if not slug else f"/wiki/{slug}/"
    return Page(slug, title, entry.get("nav") or title, group, source, url)


def load_manifest(wiki_dir: Path) -> Manifest:
    wiki_dir = Path(wiki_dir)
    path = wiki_dir / "wiki.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WikiError(f"{path}: manifest not found") from None
    except tomllib.TOMLDecodeError as e:
        raise WikiError(f"{path}: {e}") from None

    if "home" not in data:
        raise WikiError(f"{path}: missing [home] table")
    home = _page(wiki_dir, data["home"], "", None, path)

    seen: set[str] = set()
    groups: list[tuple[str, tuple[Page, ...]]] = []
    for g in data.get("group", []):
        name = g.get("name")
        if not name:
            raise WikiError(f"{path}: a [[group]] has no name")
        pages = []
        for entry in g.get("page", []):
            slug = entry.get("slug", "")
            if not _SLUG.fullmatch(slug):
                raise WikiError(f"{path}: bad slug {slug!r} (must match [a-z0-9]+(-[a-z0-9]+)*)")
            if slug in seen:
                raise WikiError(f"{path}: duplicate slug {slug!r}")
            seen.add(slug)
            pages.append(_page(wiki_dir, entry, slug, name, path))
        groups.append((name, tuple(pages)))

    manifest = Manifest(home, tuple(groups))
    listed = {p.source for p in manifest.pages()}
    for md in sorted(wiki_dir.glob("*.md")):
        if md.resolve() not in listed:
            raise WikiError(f"{md}: markdown file is not in wiki.toml")
    return manifest
