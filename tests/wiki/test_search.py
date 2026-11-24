from pathlib import Path

from wikigen.manifest import Page
from wikigen.render import Heading, Rendered, Section
from wikigen.search import search_entries


def _page(slug: str = "first") -> Page:
    return Page(slug, "First Page", "First", "User Guide", Path("/x.md"), f"/wiki/{slug}/")


def _rendered(sections: tuple[Section, ...]) -> Rendered:
    return Rendered("", (), sections, "p", (), ())


def test_search_entries_one_per_h2_h3_plus_intro():
    body = _rendered(
        (
            Section(None, "intro words"),
            Section(Heading(2, "Setup", "setup"), "set up text"),
            Section(Heading(3, "Deep", "deep"), "deeper text"),
        )
    )
    assert search_entries(_page(), body) == [
        {
            "page": "first/",
            "pageTitle": "First",
            "section": "",
            "sectionId": "",
            "text": "intro words",
        },
        {
            "page": "first/",
            "pageTitle": "First",
            "section": "Setup",
            "sectionId": "setup",
            "text": "set up text",
        },
        {
            "page": "first/",
            "pageTitle": "First",
            "section": "Deep",
            "sectionId": "deep",
            "text": "deeper text",
        },
    ]


def test_search_entries_skip_empty_text_and_home_is_empty():
    body = _rendered((Section(None, "intro"), Section(Heading(2, "Empty", "empty"), "")))
    assert [e["section"] for e in search_entries(_page(), body)] == [""]
    assert search_entries(_page(""), body) == []
