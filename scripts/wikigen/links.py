"""Resolve the links in wiki pages: pages, README anchors and repo files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from . import WikiError
from .manifest import Manifest, Page
from .render import anchors, slugify

REPO_BLOB = "https://github.com/GreenglassT/Graft/blob/main/"
REPO_TREE = "https://github.com/GreenglassT/Graft/tree/main/"

_EXTERNAL = ("http://", "https://", "mailto:")


@dataclass(frozen=True)
class LinkIndex:
    pages: dict[Path, Page]  # by resolved source
    anchors: dict[Path, frozenset[str]]  # by resolved source


def build_index(manifest: Manifest) -> LinkIndex:
    pages = {p.source: p for p in manifest.pages()}
    found = {src: anchors(src.read_text(encoding="utf-8")) for src in pages}
    return LinkIndex(pages, found)


def _frag(fragment: str) -> str:
    return f"#{quote(fragment)}" if fragment else ""


def make_resolver(page: Page, index: LinkIndex, repo_root: Path) -> Callable[[str], str]:
    repo_root = Path(repo_root).resolve()
    wiki_dir = repo_root / "docs" / "wiki"
    root_readme = repo_root / "README.md"

    def broken(href: str) -> WikiError:
        return WikiError(f"{page.source}: broken link {href}")

    def resolve(href: str) -> str:
        if href.startswith(_EXTERNAL):
            return href
        path_part, has_frag, frag_part = href.partition("#")
        path_part = unquote(path_part)
        fragment = unquote(frag_part)

        if not path_part:
            if has_frag and fragment in index.anchors[page.source]:
                return _frag(fragment)
            raise broken(href)

        target = (page.source.parent / path_part).resolve()

        hit = index.pages.get(target)
        if hit is not None:
            if fragment and fragment not in index.anchors[hit.source]:
                raise broken(href)
            return hit.url_path + _frag(fragment)

        if target == root_readme:
            if not fragment:
                return "/wiki/"
            owners = sorted(
                (p for p in index.pages.values() if fragment in index.anchors[p.source]),
                key=lambda p: str(p.source),
            )
            if len(owners) == 1:
                return owners[0].url_path + _frag(fragment)
            if not owners:
                # A README section the wiki gives a page of its own ("## Removing Graft"):
                # the page whose title has that id, at its top.
                owners = sorted(
                    (p for p in index.pages.values() if slugify(p.title, {}) == fragment),
                    key=lambda p: str(p.source),
                )
                if len(owners) == 1:
                    return owners[0].url_path
            names = ", ".join(p.slug or "home" for p in owners)
            why = f"matches {names}" if owners else "matches no wiki page"
            raise WikiError(f"{page.source}: broken link {href}: README anchor #{fragment} {why}")

        if target.exists() and target != repo_root and target.is_relative_to(repo_root):
            if not target.is_relative_to(wiki_dir):
                base = REPO_TREE if target.is_dir() else REPO_BLOB
                rel = target.relative_to(repo_root).as_posix()
                return base + quote(rel) + _frag(fragment)

        raise broken(href)

    return resolve
