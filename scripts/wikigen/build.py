"""Build docs/wiki into <out>/wiki/ and merge its URLs into <out>/sitemap.xml."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import WikiError
from .links import build_index, make_resolver
from .manifest import load_manifest
from .page import SITE, read_chrome, render_page
from .render import render_markdown
from .scrub import Hit, load_denylist, scan
from .search import search_entries
from .sitemap import merge_sitemap

ASSETS = Path(__file__).resolve().parent / "assets"
REPO_ROOT = Path(__file__).resolve().parents[2]
_TAG = re.compile(r"<[^>]*>")
_HREF = re.compile(r'href="([^"]*)"')
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
OCR_SCRIPT = Path(__file__).resolve().parent / "ocr.swift"
_OCR_TIMEOUT = 180  # the first `swift` run compiles the script


@dataclass(frozen=True)
class Report:
    pages: int
    images: int
    private_layer: bool
    ocr: bool


def _text_view(markup: str, sep: str = " ") -> str:
    return html.unescape(_TAG.sub(sep, markup))


def _check_cname(out: Path) -> None:
    cname = out / "CNAME"
    try:
        value = cname.read_text(encoding="utf-8").strip()
    except OSError:
        raise WikiError(f"{cname}: not found; {out} is not the graftipa.com site") from None
    if value != "graftipa.com":
        raise WikiError(f"{cname}: reads {value!r}, expected 'graftipa.com'")


def _check_home_links(home_html: str, pages) -> None:
    hrefs = {m.split("#", 1)[0] for m in _HREF.findall(home_html)}
    missing = [p.slug for p in pages if p.slug and p.url_path not in hrefs]
    if missing:
        raise WikiError(f"home page does not link every page; missing: {', '.join(missing)}")


def _check_images(img_dir: Path, used: set[Path]) -> list[Path]:
    """Raise on an unused file; return the used files (singles, lights and darks) in name order."""
    if not img_dir.is_dir():
        return []
    keep = set()
    for light in used:
        keep.add(light.resolve())
        if light.name.endswith("-light.png"):
            keep.add(light.with_name(light.name.removesuffix("-light.png") + "-dark.png").resolve())
    unused = []
    result = []
    for f in sorted(img_dir.iterdir()):
        if f.name.startswith(".") or not f.is_file():
            continue
        if f.resolve() in keep:
            result.append(f)
        else:
            unused.append(f)
    if unused:
        names = ", ".join(f.name for f in unused)
        raise WikiError(f"{img_dir}: unused image: {names}")
    return result


def ocr_texts(paths: Sequence[Path]) -> dict[Path, str]:
    """Recognized text of each image, from one run of ocr.swift (Apple Vision)."""
    if not paths:
        return {}
    cmd = ["xcrun", "swift", str(OCR_SCRIPT), *(str(p) for p in paths)]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_OCR_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise WikiError("OCR failed: timed out") from None
    except OSError as e:
        raise WikiError(f"OCR failed: {e}") from None
    if done.returncode != 0:
        raise WikiError(f"OCR failed: {done.stderr.strip() or f'exit {done.returncode}'}")
    try:
        by_name = json.loads(done.stdout)
        return {p: by_name[str(p)] for p in paths}
    except (ValueError, KeyError, TypeError):
        raise WikiError("OCR failed: unreadable output") from None


def _png_chunk_text(kind: bytes, data: bytes) -> str | None:
    """The text in one tEXt, zTXt, iTXt or eXIf chunk; None for any other chunk."""
    if kind in (b"tEXt", b"eXIf"):
        return data.decode("latin-1")
    if kind == b"zTXt":
        keyword, _, rest = data.partition(b"\0")
        return f"{keyword.decode('latin-1')} {zlib.decompress(rest[1:]).decode('latin-1')}"
    if kind == b"iTXt":
        keyword, _, rest = data.partition(b"\0")
        compressed, rest = rest[:1] == b"\x01", rest[2:]
        language, _, rest = rest.partition(b"\0")
        translated, _, text = rest.partition(b"\0")
        if compressed:
            text = zlib.decompress(text)
        parts = [keyword.decode("latin-1"), language.decode("latin-1")]
        parts += [translated.decode("utf-8", "replace"), text.decode("utf-8", "replace")]
        return " ".join(parts)
    return None


def _png_text(path: Path) -> str:
    """The text in a PNG's tEXt, zTXt, iTXt and eXIf chunks, chunks separated by newlines.

    render.py has checked the signature. A truncated chunk, bad compressed text or bytes after
    IEND are errors, so nothing in the file goes out unscanned.
    """
    data = path.read_bytes()
    texts = []
    pos = 8
    while pos < len(data):
        if pos + 12 > len(data):
            raise WikiError(f"{path}: truncated PNG")
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        end = pos + 8 + length
        if end + 4 > len(data):
            raise WikiError(f"{path}: truncated PNG")
        try:
            text = _png_chunk_text(kind, data[pos + 8 : end])
        except zlib.error:
            raise WikiError(f"{path}: unreadable {kind.decode('latin-1')} chunk") from None
        if text is not None:
            texts.append(text.replace("\0", " "))
        pos = end + 4
        if kind == b"IEND" and pos < len(data):
            raise WikiError(f"{path}: data after the IEND chunk")
    return "\n".join(texts)


def _format(hit: Hit) -> str:
    return f"{hit.where}:{hit.line}: {hit.term}: {hit.text}"


def build(
    *,
    repo_root: Path,
    out: Path,
    lastmod: str,
    denylist: Path | None,
    check_images: bool = False,
) -> Report:
    repo_root, out = Path(repo_root), Path(out)
    if not _DATE.fullmatch(lastmod):
        raise WikiError(f"lastmod {lastmod!r} is not YYYY-MM-DD")
    _check_cname(out)
    terms: tuple[str, ...] = ()
    if denylist is not None:
        denylist = Path(denylist)
        if not denylist.is_file():
            raise WikiError(f"{denylist}: scrub list not found")
        terms = load_denylist(denylist)

    wiki_dir = repo_root / "docs" / "wiki"
    img_dir = wiki_dir / "img"
    manifest = load_manifest(wiki_dir)
    try:
        chrome = read_chrome((out / "index.html").read_text(encoding="utf-8"))
    except OSError as e:
        raise WikiError(f"{out / 'index.html'}: {e.strerror}") from None
    index = build_index(manifest)
    pages = manifest.pages()

    outputs: dict[str, str] = {}
    entries: list[dict[str, str]] = []
    used: set[Path] = set()
    for page in pages:
        text = page.source.read_text(encoding="utf-8")
        rendered = render_markdown(
            text,
            source=page.source,
            resolve_link=make_resolver(page, index, repo_root),
            image_dir=img_dir,
        )
        if not page.slug:
            _check_home_links(rendered.html, pages)
        used.update(rendered.images)
        rel = "index.html" if not page.slug else f"{page.slug}/index.html"
        outputs[rel] = render_page(page, rendered, manifest, chrome)
        entries.extend(search_entries(page, rendered))
    image_files = _check_images(img_dir, used)

    outputs["search-index.json"] = (
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    for name in ("wiki.css", "wiki.js"):
        outputs[name] = (ASSETS / name).read_text(encoding="utf-8")
    try:
        old_sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    except OSError as e:
        raise WikiError(f"{out / 'sitemap.xml'}: {e.strerror}") from None
    sitemap = merge_sitemap(old_sitemap, [SITE + p.url_path for p in pages], lastmod)

    hits: list[Hit] = []
    if check_images and image_files:
        for image, text in ocr_texts(image_files).items():
            hits += scan(f"wiki/img/{image.name} (OCR)", text, terms)
    for image in image_files:
        hits += scan(f"wiki/img/{image.name} (png text)", _png_text(image), terms)
    for rel, content in outputs.items():
        where = f"wiki/{rel}"
        hits += scan(where, content, terms)
        if rel.endswith(".html"):
            hits += scan(f"{where} (text)", _text_view(content), terms)
            hits += scan(f"{where} (joined)", _text_view(content, ""), terms)
            # Tags kept, so attribute values (alt text) are seen with `&` and `'` decoded.
            hits += scan(f"{where} (unescaped)", html.unescape(content), terms)
    hits += scan("sitemap.xml", sitemap, terms)
    if hits:
        raise WikiError("\n".join(_format(h) for h in hits))

    staging = out / ".wiki-build"
    if staging.exists():
        shutil.rmtree(staging)
    for rel, content in outputs.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if image_files:
        (staging / "img").mkdir(parents=True, exist_ok=True)
        for f in image_files:
            shutil.copyfile(f, staging / "img" / f.name)
    live = out / "wiki"
    old = out / ".wiki-old"
    if old.exists():
        shutil.rmtree(old)
    had_live = live.exists()
    if had_live:
        live.rename(old)
    try:
        staging.rename(live)
    except OSError:
        if had_live:
            old.rename(live)
        raise
    if had_live:
        shutil.rmtree(old)
    tmp = out / ".sitemap.xml.tmp"
    tmp.write_text(sitemap, encoding="utf-8")
    os.replace(tmp, out / "sitemap.xml")
    return Report(len(pages), len(image_files), denylist is not None, check_images)


def _git_lastmod(repo_root: Path) -> str:
    """The date of the last commit to docs/wiki, or to a page it renders from elsewhere in the
    repo (docs/network.md, CHANGELOG.md), as wiki.toml lists them."""
    repo_root = Path(repo_root).resolve()
    wiki_dir = repo_root / "docs" / "wiki"
    outside = sorted(
        p.source.relative_to(repo_root).as_posix()
        for p in load_manifest(wiki_dir).pages()
        if p.source.is_relative_to(repo_root) and not p.source.is_relative_to(wiki_dir)
    )
    cmd = ["git", "-C", str(repo_root), "log", "-1", "--format=%cs", "--", "docs/wiki", *outside]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        value = done.stdout.strip() if done.returncode == 0 else ""
    except OSError:
        value = ""
    if not value:
        raise WikiError("no --lastmod and no git history for docs/wiki; pass --lastmod YYYY-MM-DD")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-wiki", description="Build graftipa.com/wiki/.")
    parser.add_argument("--out", required=True, type=Path, help="the site checkout")
    parser.add_argument("--lastmod", help="YYYY-MM-DD for the sitemap entries")
    parser.add_argument(
        "--denylist", type=Path, help="private scrub list (or GRAFT_SCRUB_DENYLIST)"
    )
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="don't OCR the screenshots for the scrub (don't publish such a build)",
    )
    # OCR is the default now; the old opt-in flag stays accepted for existing instructions.
    parser.add_argument("--check-images", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    repo_root = REPO_ROOT
    denylist = args.denylist
    if denylist is None and os.environ.get("GRAFT_SCRUB_DENYLIST"):
        denylist = Path(os.environ["GRAFT_SCRUB_DENYLIST"])
    try:
        lastmod = args.lastmod or _git_lastmod(repo_root)
        report = build(
            repo_root=repo_root,
            out=args.out,
            lastmod=lastmod,
            denylist=denylist,
            check_images=not args.skip_image_check,
        )
    except (WikiError, OSError) as e:
        for line in str(e).splitlines() or [""]:
            print(f"error: {line}")
        return 1
    print(f"built {report.pages} pages, {report.images} images into {args.out}/wiki")
    if not report.private_layer:
        print(
            "warning: no private scrub list (--denylist or GRAFT_SCRUB_DENYLIST); only /Users/ "
            "paths and UDIDs were checked. Don't publish this build.",
            file=sys.stderr,
        )
    if report.images and not report.ocr:
        print(
            "warning: images were not OCR-checked (--skip-image-check); don't publish this build.",
            file=sys.stderr,
        )
    return 0
