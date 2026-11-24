import re
from pathlib import Path

import pytest
from wikigen import WikiError
from wikigen.manifest import Page, load_manifest
from wikigen.page import Chrome, description, read_chrome, render_page
from wikigen.render import Rendered

HEADER = '<header class="bar"><a href="wiki/">Docs</a></header>'
FOOTER = "<footer><p>x</p></footer>"


def _chrome(site: Path) -> Chrome:
    return read_chrome((site / "index.html").read_text(encoding="utf-8"))


def _rendered(first: str = "The first page.", html: str = "<p>Body.</p>\n") -> Rendered:
    return Rendered(html, (), (), first, (), ())


def _render(repo: Path, site: Path, slug: str, body: Rendered | None = None) -> str:
    manifest = load_manifest(repo / "docs/wiki")
    page = next(p for p in manifest.pages() if p.slug == slug)
    return render_page(page, body or _rendered(), manifest, _chrome(site))


def test_read_chrome_rewrites_relative_urls(site):
    chrome = _chrome(site)
    assert '<a class="brand" href="/"><img src="/icon.png" alt="" width="28" height="28">' in (
        chrome.header
    )
    assert '<a href="/#build">How it works</a>' in chrome.header
    assert '<a href="/wiki/">Docs</a>' in chrome.header
    assert '<a href="https://github.com/GreenglassT/Graft">GitHub</a>' in chrome.header
    assert "Apple Inc." in chrome.footer
    assert 'href="https://m4bapp.com"' in chrome.footer
    assert "aria-current" not in chrome.header


def test_read_chrome_leaves_other_text_alone():
    chrome = read_chrome(
        '<header class="bar"><a href="mailto:a@b.c">m</a> <a href="wiki/">Docs</a>'
        ' <img src="./x.png" alt="src=y"> <a href="/already">r</a></header><footer>href=z</footer>'
    )
    assert 'href="mailto:a@b.c"' in chrome.header
    assert 'href="/already"' in chrome.header
    assert 'src="/x.png" alt="src=y"' in chrome.header
    assert chrome.footer == "<footer>href=z</footer>"


def test_read_chrome_requires_docs_link(site):
    html = (site / "index.html").read_text(encoding="utf-8").replace('href="wiki/"', 'href="x/"')
    with pytest.raises(WikiError, match=r"^index\.html: the header has no Docs link to wiki/$"):
        read_chrome(html)


def test_read_chrome_requires_exactly_one_header_and_footer():
    for html in (
        FOOTER,
        HEADER,
        HEADER + FOOTER + FOOTER,
        HEADER + HEADER + FOOTER,
        HEADER + FOOTER + "<footer>",
    ):
        with pytest.raises(WikiError, match=r"^index\.html: "):
            read_chrome(html)
    assert read_chrome(HEADER + FOOTER)


def test_page_marks_current_in_sidebar_and_header(repo, site):
    html = _render(repo, site, "first")
    assert html.count('aria-current="page"') == 2
    assert '<a href="/wiki/" aria-current="page">Docs</a>' in html
    assert '<a href="/wiki/first/" aria-current="page">First Page</a>' in html
    assert '<a href="/wiki/second/">Second Page</a>' in html
    assert '<p class="sidebar-label">User Guide</p>' in html
    assert 'class="search-input"' in html
    assert '<link rel="stylesheet" href="/wiki/wiki.css">' in html
    assert '<script src="/wiki/wiki.js" defer></script>' in html
    assert "family=Schibsted+Grotesk" in html
    assert '<meta property="og:url" content="https://graftipa.com/wiki/first/">' in html
    assert '<meta property="og:image" content="https://graftipa.com/screenshot.png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert '<meta name="description" content="The first page.">' in html
    assert "<h1>First Page</h1>\n<p>Body.</p>" in html


def test_page_title_and_canonical(repo, site):
    html = _render(repo, site, "first")
    assert "<title>First Page | Graft Docs</title>" in html
    assert '<link rel="canonical" href="https://graftipa.com/wiki/first/">' in html
    assert (
        '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="/wiki/">Docs</a> \u203a User Guide</nav>'
        in html
    )


def test_home_title_is_graft_docs_and_has_no_breadcrumb(repo, site):
    html = _render(repo, site, "")
    assert "<title>Graft Docs</title>" in html
    assert '<link rel="canonical" href="https://graftipa.com/wiki/">' in html
    assert "breadcrumb" not in html
    assert '<a href="/wiki/" aria-current="page">' in html


def test_meta_description_cut_at_word_before_160():
    assert description("  short   text\n here ") == "short text here"
    text = " ".join(["word"] * 60)  # 299 chars
    out = description(text)
    assert out.endswith("…")
    assert len(out) <= 161
    assert out == " ".join(["word"] * 32) + "…"  # 32 words = 159 chars; the 33rd would pass 160
    assert description("a" * 200) == "a" * 160 + "…"
    assert description("x" * 160) == "x" * 160


def test_pager_prev_next_follow_manifest_order(repo, site):
    home = _render(repo, site, "")
    assert 'rel="prev"' not in home
    assert '<a rel="next" href="/wiki/first/">First Page →</a>' in home
    middle = _render(repo, site, "first")
    assert '<a rel="prev" href="/wiki/">← Graft Docs</a>' in middle
    assert '<a rel="next" href="/wiki/second/">Second Page →</a>' in middle
    last = _render(repo, site, "second")
    assert '<a rel="prev" href="/wiki/first/">← First Page</a>' in last
    assert 'rel="next"' not in last
    assert "—" not in middle


def test_inserted_text_is_escaped(repo, site):
    manifest = load_manifest(repo / "docs/wiki")
    first = manifest.groups[0][1][0]
    evil = Page("first", 'A & <b> "q"', "N & <i>", "G <u> &", first.source, first.url_path)
    manifest = type(manifest)(manifest.home, (("G <u> &", (evil, *manifest.groups[0][1][1:])),))
    body = _rendered(first='Desc & <x> "y"')
    html = render_page(evil, body, manifest, _chrome(site))
    assert "<title>A &amp; &lt;b&gt; &quot;q&quot; | Graft Docs</title>" in html
    assert "<h1>A &amp; &lt;b&gt; &quot;q&quot;</h1>" in html
    assert 'content="Desc &amp; &lt;x&gt; &quot;y&quot;"' in html
    assert "\u203a G &lt;u&gt; &amp;</nav>" in html
    assert '<p class="sidebar-label">G &lt;u&gt; &amp;</p>' in html
    assert "N &amp; &lt;i&gt;</a>" in html
    assert not re.search(r"<(b|i|u|x)>", html)
