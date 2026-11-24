"""Search-index entries for one wiki page."""

from __future__ import annotations

from .manifest import Page
from .render import Rendered


def search_entries(page: Page, body: Rendered) -> list[dict[str, str]]:
    if not page.slug:
        return []
    entries = []
    for section in body.sections:
        if not section.text:
            continue
        heading = section.heading
        entries.append(
            {
                "page": f"{page.slug}/",
                "pageTitle": page.nav,
                "section": heading.text if heading else "",
                "sectionId": heading.id if heading else "",
                "text": section.text,
            }
        )
    return entries
