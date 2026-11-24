from __future__ import annotations

import re
from pathlib import Path

import pytest
from wikigen import WikiError
from wikigen.render import anchors, render_markdown, slugify

from .helpers import _png


def render(md: str, tmp_path: Path, resolve=lambda h: h):
    (tmp_path / "img").mkdir(exist_ok=True)
    return render_markdown(
        md, source=tmp_path / "page.md", resolve_link=resolve, image_dir=tmp_path / "img"
    )


def shot(tmp_path: Path, name="shot", dark=True, size=(4, 2)):
    img = tmp_path / "img"
    img.mkdir(exist_ok=True)
    (img / f"{name}-light.png").write_bytes(_png(*size, (255, 255, 255)))
    if dark:
        (img / f"{name}-dark.png").write_bytes(_png(*size, (0, 0, 0)))


def test_slugify_matches_github():
    seen = {}
    assert slugify("1. Set up your Apple account", seen) == "1-set-up-your-apple-account"
    assert slugify("The CLI without the app (pipx)", seen) == "the-cli-without-the-app-pipx"
    assert slugify("What Graft downloads, and when", seen) == "what-graft-downloads-and-when"
    assert slugify("Settings › App Store", seen) == "settings--app-store"  # noqa: RUF001
    assert slugify("`--dry-run` and you", seen) == "--dry-run-and-you"
    assert slugify("Troubleshooting", seen) == "troubleshooting"
    assert slugify("Troubleshooting", seen) == "troubleshooting-1"
    assert slugify("Troubleshooting", seen) == "troubleshooting-2"


MD = "# Title\n\nIntro.\n\n## Setup `x`\n\ntext\n\n### Sub\n\n#### Deep\n\n## Setup `x`\n"


def test_h1_removed_and_headings_get_ids(tmp_path):
    r = render(MD, tmp_path)
    assert "<h1" not in r.html and "Title" not in r.html
    assert '<h2 id="setup-x">' in r.html
    assert '<h3 id="sub">' in r.html
    assert '<h4 id="deep">' in r.html
    assert '<h2 id="setup-x-1">' in r.html
    assert [(h.level, h.text, h.id) for h in r.headings] == [
        (2, "Setup x", "setup-x"),
        (3, "Sub", "sub"),
        (4, "Deep", "deep"),
        (2, "Setup x", "setup-x-1"),
    ]


def test_anchors_lists_h2_to_h4_ids(tmp_path):
    assert anchors(MD) == {"setup-x", "sub", "deep", "setup-x-1"}
    assert anchors(MD) == frozenset(h.id for h in render(MD, tmp_path).headings)


def test_render_is_independent_between_calls(tmp_path):
    assert render(MD, tmp_path).html == render(MD, tmp_path).html


def test_links_go_through_resolver(tmp_path):
    r = render("# T\n\nSee [x](other.md#a).\n", tmp_path, lambda h: "/wiki/other/#a")
    assert 'href="/wiki/other/#a"' in r.html
    assert r.links == ("other.md#a",)


def test_image_becomes_picture_with_dark_source(tmp_path):
    shot(tmp_path, size=(7, 3))
    r = render("# T\n\nIntro.\n\n![A shot](img/shot-light.png)\n", tmp_path)
    assert '<figure class="wiki-shot"><picture>' in r.html
    assert (
        '<source srcset="/wiki/img/shot-dark.png" media="(prefers-color-scheme: dark)">' in r.html
    )
    assert (
        '<img src="/wiki/img/shot-light.png" alt="A shot" width="7" height="3" loading="lazy">'
        in r.html
    )
    assert "<figcaption>" not in r.html
    assert r.images == ((tmp_path / "img/shot-light.png").resolve(),)


def test_image_title_becomes_figcaption(tmp_path):
    shot(tmp_path)
    r = render('# T\n\nIntro.\n\n![A](img/shot-light.png "The <caption>")\n', tmp_path)
    assert "<figcaption>The &lt;caption&gt;</figcaption>" in r.html


def test_image_without_dark_twin_is_error(tmp_path):
    shot(tmp_path, dark=False)
    with pytest.raises(WikiError, match="dark twin"):
        render("# T\n\nIntro.\n\n![A](img/shot-light.png)\n", tmp_path)


@pytest.mark.parametrize("dark_size", [(5, 2), (4, 3)])
def test_dark_twin_of_another_size_is_error(tmp_path, dark_size):
    shot(tmp_path, size=(4, 2))
    (tmp_path / "img/shot-dark.png").write_bytes(_png(*dark_size, (0, 0, 0)))
    with pytest.raises(WikiError) as e:
        render("# T\n\nIntro.\n\n![A](img/shot-light.png)\n", tmp_path)
    w, h = dark_size
    want = f"{tmp_path / 'page.md'}: shot-dark.png is {w}x{h}, its light twin is 4x2"
    assert str(e.value) == want


def test_dark_twin_that_is_not_a_png_is_error(tmp_path):
    shot(tmp_path)
    (tmp_path / "img/shot-dark.png").write_bytes(b"placeholder, not an image")
    with pytest.raises(WikiError, match=r"shot-dark\.png: not a PNG"):
        render("# T\n\nIntro.\n\n![A](img/shot-light.png)\n", tmp_path)


def test_image_without_alt_is_error(tmp_path):
    shot(tmp_path)
    with pytest.raises(WikiError, match="alt"):
        render("# T\n\nIntro.\n\n![](img/shot-light.png)\n", tmp_path)


def test_missing_light_image_is_error(tmp_path):
    with pytest.raises(WikiError, match="does not exist"):
        render("# T\n\nIntro.\n\n![A](img/nope-light.png)\n", tmp_path)


def test_image_outside_image_dir_or_not_light_png_is_error(tmp_path):
    shot(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other/x-light.png").write_bytes(_png(1, 1, (0, 0, 0)))
    (tmp_path / "img/sub").mkdir()
    (tmp_path / "img/sub/y-light.png").write_bytes(_png(1, 1, (0, 0, 0)))
    (tmp_path / "img/plain.jpg").write_bytes(b"x")
    for src in ("other/x-light.png", "img/sub/y-light.png", "img/plain.jpg"):
        with pytest.raises(WikiError, match=r"\.png directly in"):
            render(f"# T\n\nIntro.\n\n![A]({src})\n", tmp_path)


def test_single_image_has_no_picture_and_takes_its_png_size(tmp_path):
    (tmp_path / "img").mkdir()
    (tmp_path / "img/solo.png").write_bytes(_png(9, 5, (0, 0, 0)))
    r = render('# T\n\nIntro.\n\n![A solo](img/solo.png "Cap")\n', tmp_path)
    assert (
        '<figure class="wiki-shot"><img src="/wiki/img/solo.png" alt="A solo" '
        'width="9" height="5" loading="lazy"><figcaption>Cap</figcaption></figure>'
    ) in r.html
    assert "<picture>" not in r.html and "<source" not in r.html
    assert r.images == ((tmp_path / "img/solo.png").resolve(),)


def test_missing_single_image_is_error(tmp_path):
    with pytest.raises(WikiError, match="does not exist"):
        render("# T\n\nIntro.\n\n![A](img/nope.png)\n", tmp_path)


def test_direct_dark_image_is_error_naming_the_alternatives(tmp_path):
    shot(tmp_path)
    with pytest.raises(WikiError, match=r"shot\.png for a single-theme image.*shot-light\.png"):
        render("# T\n\nIntro.\n\n![A](img/shot-dark.png)\n", tmp_path)


def test_alt_and_caption_are_escaped(tmp_path):
    shot(tmp_path)
    r = render('# T\n\nIntro.\n\n![a "b" <c>](img/shot-light.png "t \\"q\\" <u>")\n', tmp_path)
    assert 'alt="a &quot;b&quot; &lt;c&gt;"' in r.html
    assert "<figcaption>t &quot;q&quot; &lt;u&gt;</figcaption>" in r.html
    assert "<c>" not in r.html


def test_table_is_wrapped():
    r = render_markdown(
        "# T\n\nIntro.\n\n| a |\n|---|\n| b |\n",
        source=Path("p.md"),
        resolve_link=lambda h: h,
        image_dir=Path("img"),
    )
    assert '<div class="table-wrap"><table>' in r.html
    assert "</table>\n</div>" in r.html


def test_raw_html_is_escaped(tmp_path):
    r = render("# T\n\nUse <script>alert(1)</script> here.\n\n<div>x</div>\n", tmp_path)
    assert "<script>" not in r.html and "<div>" not in r.html
    assert "&lt;script&gt;" in r.html


def test_bare_url_in_text_is_error(tmp_path):
    with pytest.raises(WikiError, match="bare URL"):
        render("# T\n\nsee https://x.y now\n", tmp_path)
    with pytest.raises(WikiError, match="bare URL"):
        render("# T\n\nIntro.\n\n- see http://x.y\n", tmp_path)


def test_urls_in_links_and_code_are_not_bare(tmp_path):
    r = render(
        "# T\n\n[https://x.y](https://x.y) and `https://a.b`\n\n```\nhttps://c.d\n```\n", tmp_path
    )
    assert r.links == ("https://x.y",)


def test_sections_split_at_h2_and_h3_with_plain_text(tmp_path):
    md = (
        "# T\n\nFirst *para*.\n\nMore.\n\n## One\n\nUses `code` here.\n\n```\nfenced  text\n```\n\n"
        "### Two\n\nBody\ntext.\n\n#### Four\n\nDeep.\n\n## Three\n"
    )
    r = render(md, tmp_path)
    got = [(s.heading.text if s.heading else None, s.text) for s in r.sections]
    assert got == [
        (None, "First para. More."),
        ("One", "Uses code here. fenced text"),
        ("Two", "Body text. Four Deep."),
        ("Three", ""),
    ]
    assert [s.heading.level for s in r.sections[1:]] == [2, 3, 2]


def test_first_paragraph_plain_text(tmp_path):
    r = render("# T\n\nHello **bold** `c`\nworld.\n\nSecond.\n", tmp_path)
    assert r.first_paragraph == "Hello bold c world."


def test_missing_first_paragraph_is_error(tmp_path):
    with pytest.raises(WikiError, match="no paragraph"):
        render("# T\n\n## Next\n\nText.\n", tmp_path)


def test_html_has_no_stray_trailing_blank_structure(tmp_path):
    r = render("# T\n\nIntro.\n", tmp_path)
    assert re.fullmatch(r"<p>Intro\.</p>\n", r.html)


def test_link_in_heading_is_resolved_and_recorded(tmp_path):
    r = render("# T\n\nIntro.\n\n## See [x](o.md#a)\n", tmp_path, lambda h: "/wiki/o/#a")
    assert 'href="/wiki/o/#a"' in r.html
    assert r.links == ("o.md#a",)


def test_bare_url_in_heading_is_error(tmp_path):
    with pytest.raises(WikiError, match="bare URL"):
        render("# T\n\nIntro.\n\n## See https://x.y\n", tmp_path)


def test_image_outside_a_lone_paragraph_is_error(tmp_path):
    shot(tmp_path)
    for md in (
        "| a |\n|---|\n| ![z](img/shot-light.png) |\n",
        "## H ![z](img/shot-light.png)\n",
        "- item ![z](img/shot-light.png)\n",
        "text ![z](img/shot-light.png) more\n",
    ):
        with pytest.raises(WikiError, match="image must be alone"):
            render(f"# T\n\nIntro.\n\n{md}", tmp_path)
