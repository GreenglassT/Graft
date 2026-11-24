from __future__ import annotations

import re
from pathlib import Path

import pytest
from wikigen import WikiError
from wikigen.links import REPO_BLOB, REPO_TREE, build_index, make_resolver
from wikigen.manifest import Page, load_manifest

from .helpers import write


def setup(repo: Path, source: str = "docs/wiki/first.md"):
    manifest = load_manifest(repo / "docs/wiki")
    index = build_index(manifest)
    src = (repo / source).resolve()
    page = next((p for p in manifest.pages() if p.source == src), None)
    if page is None:  # a source outside the wiki, e.g. docs/upgrading.md
        page = Page("x", "X", "X", "G", src, "/wiki/x/")
    return make_resolver(page, index, repo)


def test_external_unchanged(repo):
    r = setup(repo)
    for url in ("https://example.com/a#b", "http://example.com", "mailto:a@example.com"):
        assert r(url) == url


def test_same_page_anchor_ok_and_missing_is_error(repo):
    write(repo / "docs/wiki/first.md", "# First Page\n\n## Part One\n")
    r = setup(repo)
    assert r("#part-one") == "#part-one"
    with pytest.raises(WikiError, match=r"first\.md: broken link #nope"):
        r("#nope")


def test_page_link_with_anchor(repo):
    write(repo / "docs/wiki/second.md", "# Second Page\n\n## Part\n")
    r = setup(repo)
    assert r("second.md#part") == "/wiki/second/#part"
    assert r("second.md") == "/wiki/second/"
    assert r("README.md") == "/wiki/"  # the wiki home, not the repo README


def test_percent_encoded_href_is_unquoted(repo):
    write(repo / "docs/wiki/second.md", "# Second Page\n\n## Café au lait\n")
    r = setup(repo)
    assert r("second.md#caf%C3%A9-au-lait") == "/wiki/second/#caf%C3%A9-au-lait"
    write(repo / "docs/my file.txt", "x")
    assert r("../my%20file.txt") == REPO_BLOB + "docs/my%20file.txt"


def test_page_link_bad_anchor_is_error(repo):
    r = setup(repo)
    with pytest.raises(WikiError, match=re.escape("broken link second.md#nope")):
        r("second.md#nope")


def test_link_from_outside_source_resolves(repo):
    write(repo / "docs/wiki/second.md", "# Second Page\n\n## Part\n")
    r = setup(repo, "docs/upgrading.md")
    assert r("../README.md#part") == "/wiki/second/#part"
    assert r("../README.md") == "/wiki/"


def test_link_from_repo_root_source_resolves_from_the_root(repo):
    """CHANGELOG.md's links are written from the repo root, as GitHub shows them."""
    write(repo / "CHANGELOG.md", "# Changelog\n\nEach release.\n")
    r = setup(repo, "CHANGELOG.md")
    assert r("docs/wiki/second.md") == "/wiki/second/"
    assert r("docs/upgrading.md#going-back") == REPO_BLOB + "docs/upgrading.md#going-back"
    assert r("LICENSE") == REPO_BLOB + "LICENSE"


def test_readme_anchor_ambiguous_is_error(repo):
    write(repo / "docs/wiki/first.md", "# First Page\n\n## Dup\n")
    write(repo / "docs/wiki/second.md", "# Second Page\n\n## Dup\n")
    r = setup(repo)
    with pytest.raises(WikiError, match=r"#dup.*first.*second"):
        r("../../README.md#dup")


def test_readme_section_that_is_a_whole_page_resolves_to_the_page(repo):
    """README's "## Removing Graft" is the wiki page titled "Removing Graft", not an H2 in one."""
    write(repo / "docs/wiki/second.md", "# Second Page\n\n## Part\n")
    r = setup(repo, "CHANGELOG.md")
    assert r("README.md#second-page") == "/wiki/second/"
    assert r("README.md#part") == "/wiki/second/#part"  # a section in a page still wins


def test_readme_anchor_two_page_titles_is_error(repo):
    write(repo / "docs/wiki/first.md", "# Same\n")
    write(repo / "docs/wiki/second.md", "# Same\n")
    r = setup(repo)
    with pytest.raises(WikiError, match=r"#same.*first.*second"):
        r("../../README.md#same")


def test_readme_anchor_unknown_is_error(repo):
    r = setup(repo)
    with pytest.raises(WikiError, match=r"#nothing"):
        r("../../README.md#nothing")


def test_repo_file_becomes_github_blob(repo):
    r = setup(repo)
    assert r("../../LICENSE") == REPO_BLOB + "LICENSE"
    assert r("../upgrading.md#x") == REPO_BLOB + "docs/upgrading.md#x"


def test_repo_dir_becomes_github_tree(repo):
    r = setup(repo)
    assert r("../") == REPO_TREE + "docs"
    assert r("../../docs/") == REPO_TREE + "docs"


def test_missing_file_and_outside_repo_are_errors(repo):
    r = setup(repo)
    with pytest.raises(WikiError, match=re.escape("broken link nope.md")):
        r("nope.md")
    with pytest.raises(WikiError, match=re.escape("broken link ../../../outside.txt")):
        r("../../../outside.txt")
    (repo.parent / "outside.txt").write_text("x")
    with pytest.raises(WikiError, match=re.escape("broken link ../../../repo/../outside.txt")):
        r("../../../repo/../outside.txt")


def test_link_to_image_is_error(repo):
    r = setup(repo)
    with pytest.raises(WikiError, match=re.escape("broken link img/shot-light.png")):
        r("img/shot-light.png")
    with pytest.raises(WikiError, match=re.escape("broken link wiki.toml")):
        r("wiki.toml")


def test_other_readme_is_a_repo_file_not_the_root_readme(repo):
    write(repo / "docs/README.md", "# Docs\n")
    r = setup(repo)
    assert r("../README.md") == REPO_BLOB + "docs/README.md"
