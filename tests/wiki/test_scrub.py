from __future__ import annotations

from pathlib import Path

import pytest
from wikigen.scrub import BUILTIN, Hit, load_denylist, scan

from .helpers import write


def test_users_path_hit_but_placeholder_ok():
    assert scan("p", "see /Users/alex/x", ())
    assert not scan("p", "under /Users/<you>/", ())


def test_users_path_escaped_placeholder_ok():
    # The scrub runs on rendered HTML, where `<you>` is `&lt;you&gt;`.
    assert not scan("p", "under /Users/&lt;you&gt;/", ())


def test_new_style_udid_hit_and_sha_not():
    assert scan("p", "00008030-001A2B3C4D5E6F70", ())
    assert not scan("p", "04a40f2aa65ec9cb36127f74d52ae4ef71377c50", ())


def test_builtin_names_are_stable():
    assert [name for name, _ in BUILTIN] == ["/Users/ path", "UDID"]


def test_denylist_terms_case_insensitive_word_bounded(tmp_path: Path):
    terms = load_denylist(write(tmp_path / "d.txt", "# names\nExampleGame\n\n"))
    assert terms == ("ExampleGame",)
    assert scan("p", "the examplegame app", terms)
    assert not scan("p", "examplegames2", terms)  # a letter right after the term: no hit
    assert not scan("p", "2examplegame", terms)  # a digit right before the term: no hit


def test_load_denylist_strips_and_comments_only_at_line_start(tmp_path: Path):
    path = write(
        tmp_path / "d.txt",
        "# whole-line comment\n   # indented comment\n\n  SomeTerm  \n"
        "someone@example.com\nSome#Term\n",
    )
    assert load_denylist(path) == ("SomeTerm", "someone@example.com", "Some#Term")


def test_bom_does_not_disable_first_term(tmp_path: Path):
    path = write(tmp_path / "d.txt", "\ufeffSomeTerm\nother\n")
    terms = load_denylist(path)
    assert terms == ("SomeTerm", "other")
    assert scan("p", "the SomeTerm app", terms)


def test_term_is_literal_not_regex(tmp_path: Path):
    terms = load_denylist(write(tmp_path / "d.txt", "Some+Term\nsome.site\n"))
    assert scan("p", "the Some+Term app", terms)
    assert not scan("p", "Some plus", terms)
    assert scan("p", "see some.site now", terms)
    assert not scan("p", "see someXsite now", terms)


def test_multiword_term_matches_across_line_break_and_double_space():
    assert scan("p", "a Two\nWords app", ("Two Words",))
    assert scan("p", "a Two  Words app", ("Two Words",))
    assert scan("p", "a two words app", ("Two Words",))
    assert not scan("p", "a Twowords app", ("Two Words",))


def test_every_hit_reported_with_line_numbers():
    text = "line one ExampleGame\nline two\n  SomeTerm and ExampleGame again  \n"
    hits = scan("README", text, ("ExampleGame", "SomeTerm"))
    assert hits == [
        Hit(where="README", line=1, term="ExampleGame", text="line one ExampleGame"),
        Hit(
            where="README",
            line=3,
            term="SomeTerm",
            text="SomeTerm and ExampleGame again",
        ),
        Hit(
            where="README",
            line=3,
            term="ExampleGame",
            text="SomeTerm and ExampleGame again",
        ),
    ]


def test_hits_interleave_builtins_and_terms_by_position():
    text = "ExampleGame at /Users/alex and SomeTerm\n"
    terms = [hit.term for hit in scan("p", text, ("SomeTerm", "ExampleGame"))]
    assert terms == ["ExampleGame", "/Users/ path", "SomeTerm"]


def test_term_found_twice_gives_two_hits():
    hits = scan("p", "ExampleGame\nExampleGame\n", ("ExampleGame",))
    assert [h.line for h in hits] == [1, 2]


def test_udid_hit_reports_name_and_line():
    hits = scan("p", "ok\n00008030-001A2B3C4D5E6F70\n", ())
    assert hits == [Hit(where="p", line=2, term="UDID", text="00008030-001A2B3C4D5E6F70")]


def test_text_is_trimmed_to_120_characters():
    long_line = "x" * 200 + " SomeTerm"
    (hit,) = scan("p", long_line, ("SomeTerm",))
    assert len(hit.text) == 120
    assert hit.text == long_line.strip()[:120]


def test_no_hits_on_clean_text():
    assert scan("p", "Plain docs with nothing private.\n", ("ExampleGame",)) == []


def test_empty_term_is_rejected():
    with pytest.raises(ValueError):
        scan("p", "anything", ("",))
