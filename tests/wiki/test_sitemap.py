import pytest
from wikigen import WikiError
from wikigen.sitemap import merge_sitemap

from .helpers import SITEMAP_XML

URLS = ["https://graftipa.com/wiki/", "https://graftipa.com/wiki/first/"]


def test_merge_sitemap_keeps_home_and_replaces_wiki():
    old = SITEMAP_XML.replace(
        "</urlset>",
        "  <url>\n    <loc>https://graftipa.com/wiki/stale/</loc>\n"
        "    <lastmod>2020-01-01</lastmod>\n  </url>\n</urlset>",
    )
    out = merge_sitemap(old, URLS, "2026-12-01")
    assert out == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n    <loc>https://graftipa.com/</loc>\n    <lastmod>2026-11-23</lastmod>\n  </url>\n"
        "  <url>\n    <loc>https://graftipa.com/wiki/</loc>\n    <lastmod>2026-12-01</lastmod>\n  </url>\n"
        "  <url>\n    <loc>https://graftipa.com/wiki/first/</loc>\n"
        "    <lastmod>2026-12-01</lastmod>\n  </url>\n"
        "</urlset>\n"
    )


def test_merge_sitemap_is_idempotent():
    once = merge_sitemap(SITEMAP_XML, URLS, "2026-12-01")
    assert merge_sitemap(once, URLS, "2026-12-01") == once


def test_merge_sitemap_without_wiki_urls_round_trips_the_real_layout():
    assert merge_sitemap(SITEMAP_XML, [], "2026-12-01") == SITEMAP_XML


def test_merge_sitemap_requires_urlset():
    with pytest.raises(WikiError, match=r"^sitemap\.xml: "):
        merge_sitemap("<html></html>", URLS, "2026-12-01")
