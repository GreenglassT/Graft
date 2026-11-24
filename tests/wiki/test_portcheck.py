from __future__ import annotations

from pathlib import Path

from wikigen.portcheck import main, missing

README = "# G\n\nIntro `intro-only`.\n\n## A\n\nRun `graft --x`.\n"


def test_dropped_code_span_reported():
    assert missing(README, ["# A\n\nRun it.\n"]) == ["code: graft --x"]


def test_renamed_heading_reported():
    assert missing("# G\n\n## A\n\nbody\n", ["## B\n\nbody\n"]) == ["heading: a"]


def test_wiki_h1_counts_as_heading():
    assert missing("# G\n\n## App Store downloads\n", ["# App Store Downloads\n"]) == []


def test_h4_heading_checked():
    assert missing("# G\n\n## A\n\n#### Behind a proxy\n", ["# A\n"]) == ["heading: behind-a-proxy"]


def test_span_moved_to_other_page_and_into_fence_ok():
    assert missing(README, ["# A\n", "# B\n\n```\ngraft --x\n```\n"]) == []


def test_fence_lines_and_external_urls_checked():
    readme = (
        "# G\n\n## A\n\n```sh\n  one --a\n\ntwo --b\n```\n\n"
        "[x](https://e.com/p) [y](#local) [z](http://f.org)\n"
    )
    wiki = ["# A\n\none --a\n[x](https://e.com/p)\n"]
    assert missing(readme, wiki) == ["fence: two --b", "url: http://f.org"]


def test_only_restricts_to_named_h2():
    readme = "# G\n\n## A\n\n`a1`\n\n## B\n\n`b1`\n"
    assert missing(readme, ["x"], only=["B"]) == ["code: b1", "heading: b"]


def test_disclaimer_license_support_ignored():
    readme = "# G\n\n## Disclaimer\n\n`d`\n\n## License\n\n`l`\n\n## Support\n\n`s`\n"
    assert missing(readme, ["x"]) == []


def test_distinct_items_once_in_readme_order():
    readme = "# G\n\n## A\n\n`b` `a` `b`\n"
    assert missing(readme, ["# A"]) == ["code: b", "code: a"]


def _files(tmp_path: Path, readme: str, page: str) -> tuple[Path, Path]:
    r = tmp_path / "README.md"
    r.write_text(readme)
    w = tmp_path / "wiki"
    w.mkdir()
    (w / "p.md").write_text(page)
    return r, w


def test_cli_heading_only_difference_exits_zero(tmp_path, capsys):
    r, w = _files(tmp_path, "# G\n\n## A\n\nbody\n", "## B\n\nbody\n")
    assert main([str(r), str(w)]) == 0
    out = capsys.readouterr().out
    assert "headings not found (informational):" in out
    assert "heading: a" in out


def test_cli_missing_code_exits_one_and_extra_file_counts(tmp_path, capsys):
    r, w = _files(tmp_path, "# G\n\n## A\n\n`x` `y`\n", "# A\n\n`x`\n")
    assert main([str(r), str(w)]) == 1
    assert capsys.readouterr().out.strip() == "code: y"
    extra = tmp_path / "extra.md"
    extra.write_text("`y`")
    assert main([str(r), str(w), str(extra)]) == 0


def test_cli_only(tmp_path, capsys):
    r, w = _files(tmp_path, "# G\n\n## A\n\n`x`\n\n## B\n\n`y`\n", "# A\n\n# B\n")
    assert main([str(r), str(w), "--only", "A"]) == 1
    assert capsys.readouterr().out.strip() == "code: x"


def test_extraction_reaches_nested_blocks():
    readme = (
        "# G\n\n## A\n\n"
        "| h | h2 |\n| - | - |\n| `cell-code` | x |\n\n"
        "- outer\n  - inner `nested-code`\n\n"
        "- item\n\n  ```sh\n  list-fence --q\n  ```\n\n"
        "> quote `quote-code` [q](https://example.com/q)\n\n"
        "<https://example.com/a>\n"
    )
    assert missing(readme, ["# A\n"]) == [
        "code: cell-code",
        "code: nested-code",
        "fence: list-fence --q",
        "code: quote-code",
        "url: https://example.com/q",
        "url: https://example.com/a",
    ]
