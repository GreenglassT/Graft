import pytest
from wikigen import WikiError
from wikigen.manifest import load_manifest

from .helpers import write


def _toml(repo, text):
    write(repo / "docs/wiki/wiki.toml", text)


def test_loads_home_and_groups_in_order(repo):
    m = load_manifest(repo / "docs/wiki")
    assert m.home.slug == "" and m.home.url_path == "/wiki/"
    assert m.home.group is None
    assert [g for g, _ in m.groups] == ["User Guide"]
    assert [p.slug for p in m.pages()] == ["", "first", "second"]
    assert m.pages()[1].title == "First Page"
    assert m.pages()[1].url_path == "/wiki/first/"
    assert m.pages()[1].group == "User Guide"
    assert m.pages()[1].source == (repo / "docs/wiki/first.md").resolve()


def test_nav_defaults_to_title_and_can_override(repo):
    _toml(
        repo,
        '[home]\nsource = "README.md"\n\n[[group]]\nname = "G"\n\n'
        '[[group.page]]\nslug = "first"\nsource = "first.md"\nnav = "1st"\n\n'
        '[[group.page]]\nslug = "second"\nsource = "second.md"\n',
    )
    p = load_manifest(repo / "docs/wiki").pages()
    assert p[1].nav == "1st"
    assert p[2].nav == "Second Page"
    assert p[0].nav == "Graft Docs"


def test_source_outside_wiki_dir_is_allowed(repo):
    _toml(
        repo,
        '[home]\nsource = "README.md"\n\n[[group]]\nname = "G"\n\n'
        '[[group.page]]\nslug = "upgrading"\nsource = "../upgrading.md"\n'
        '\n[[group.page]]\nslug = "first"\nsource = "first.md"\n'
        '\n[[group.page]]\nslug = "second"\nsource = "second.md"\n',
    )
    p = load_manifest(repo / "docs/wiki").pages()[1]
    assert p.title == "Upgrading"
    assert p.source == (repo / "docs/upgrading.md").resolve()


def test_unlisted_markdown_file_is_error(repo):
    write(repo / "docs/wiki/stray.md", "# Stray\n\nText.\n")
    with pytest.raises(WikiError, match=r"stray\.md.*not in wiki.toml"):
        load_manifest(repo / "docs/wiki")


def test_unlisted_markdown_in_subfolder_is_ignored(repo):
    write(repo / "docs/wiki/notes/draft.md", "no heading\n")
    load_manifest(repo / "docs/wiki")


def test_missing_source_is_error(repo):
    (repo / "docs/wiki/second.md").unlink()
    with pytest.raises(WikiError, match=r"second\.md.*(missing|not found)"):
        load_manifest(repo / "docs/wiki")


def test_duplicate_slug_is_error(repo):
    _toml(
        repo,
        '[home]\nsource = "README.md"\n\n[[group]]\nname = "G"\n\n'
        '[[group.page]]\nslug = "first"\nsource = "first.md"\n\n'
        '[[group.page]]\nslug = "first"\nsource = "second.md"\n',
    )
    with pytest.raises(WikiError, match=r"duplicate slug.*first"):
        load_manifest(repo / "docs/wiki")


@pytest.mark.parametrize("slug", ["Getting", "a_b", "-a", "a-", "a--b", ""])
def test_bad_slug_is_error(repo, slug):
    _toml(
        repo,
        '[home]\nsource = "README.md"\n\n[[group]]\nname = "G"\n\n'
        f'[[group.page]]\nslug = "{slug}"\nsource = "first.md"\n\n'
        '[[group.page]]\nslug = "second"\nsource = "second.md"\n',
    )
    with pytest.raises(WikiError, match="slug"):
        load_manifest(repo / "docs/wiki")


def test_source_without_leading_h1_is_error(repo):
    write(repo / "docs/wiki/first.md", "Intro text\n\n# Late Heading\n")
    with pytest.raises(WikiError, match=r"first\.md.*# "):
        load_manifest(repo / "docs/wiki")


def test_leading_blank_lines_before_h1_are_fine(repo):
    write(repo / "docs/wiki/first.md", "\n\n# Spaced Title\n\nBody.\n")
    assert load_manifest(repo / "docs/wiki").pages()[1].title == "Spaced Title"


def test_missing_manifest_is_error(repo):
    (repo / "docs/wiki/wiki.toml").unlink()
    with pytest.raises(WikiError, match=r"wiki\.toml"):
        load_manifest(repo / "docs/wiki")
