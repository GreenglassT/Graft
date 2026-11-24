"""Build the repo's own docs/wiki: no broken links, no scrub hits."""

import html
import os
import re
from pathlib import Path

import pytest
from wikigen.build import build
from wikigen.manifest import load_manifest

from .helpers import make_site

REPO = Path(__file__).resolve().parents[2]


def test_real_docs_build_cleanly(tmp_path):
    site = make_site(tmp_path)
    report = build(repo_root=REPO, out=site, lastmod="2026-12-01", denylist=None)
    assert report.pages >= 1
    for page in load_manifest(REPO / "docs" / "wiki").pages():
        assert (site / page.url_path.lstrip("/") / "index.html").is_file(), page.slug


def test_changelog_page_is_the_whole_changelog(tmp_path):
    """/wiki/changelog/ renders the repo's CHANGELOG.md, every release of it (1.x included),
    and every page's sidebar links it."""
    site = make_site(tmp_path)
    build(repo_root=REPO, out=site, lastmod="2026-12-01", denylist=None)
    pages = {p.slug: p for p in load_manifest(REPO / "docs" / "wiki").pages()}
    assert pages["changelog"].source == (REPO / "CHANGELOG.md").resolve()
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = re.findall(r"^## (.+)$", changelog, re.MULTILINE)
    assert releases
    out = (site / "wiki/changelog/index.html").read_text(encoding="utf-8")
    for release in releases:
        assert f">{html.escape(release)}</h2>" in out, release
    for index in sorted((site / "wiki").rglob("index.html")):
        assert '<a href="/wiki/changelog/"' in index.read_text(encoding="utf-8"), index


@pytest.mark.skipif(not os.environ.get("GRAFT_SCRUB_DENYLIST"), reason="private scrub list not set")
def test_real_docs_pass_private_scrub(tmp_path):
    report = build(
        repo_root=REPO,
        out=make_site(tmp_path),
        lastmod="2026-12-01",
        denylist=Path(os.environ["GRAFT_SCRUB_DENYLIST"]),
    )
    assert report.private_layer
