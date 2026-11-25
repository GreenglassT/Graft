import json
import shutil
import stat
import subprocess
import zlib
from pathlib import Path

import pytest
from wikigen import WikiError
from wikigen import build as build_mod
from wikigen.build import build, main

from .helpers import WIKI_TOML, _png, make_repo, write

LASTMOD = "2026-12-01"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The standard fake repo, with the first page showing the screenshot."""
    root = make_repo(tmp_path / "repo")
    write(
        root / "docs/wiki/first.md",
        "# First Page\n\nThe first page.\n\n![A shot](img/shot-light.png)\n",
    )
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def run(repo, site, denylist=None, lastmod=LASTMOD):
    return build(repo_root=repo, out=site, lastmod=lastmod, denylist=denylist)


def test_builds_pages_index_assets_and_sitemap(repo, site):
    r = run(repo, site)
    assert (site / "wiki/index.html").is_file() and (site / "wiki/first/index.html").is_file()
    assert json.loads((site / "wiki/search-index.json").read_text())
    assert (site / "wiki/wiki.css").is_file() and (site / "wiki/img/shot-dark.png").is_file()
    assert (site / "wiki/img/shot-light.png").is_file()
    assert "<loc>https://graftipa.com/wiki/first/</loc>" in (site / "sitemap.xml").read_text()
    assert r.pages == 3 and r.private_layer is False and r.images == 2
    assert not (site / ".wiki-build").exists()


BEACON = (
    '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
    "data-cf-beacon='{\"token\": \"0123456789abcdef\"}'></script>"
)


def _pages(site):
    return {p: p.read_text() for p in (site / "wiki").rglob("*.html")}


def test_without_analytics_pages_end_as_before(repo, site):
    run(repo, site)
    for path, page in _pages(site).items():
        assert page.endswith("</div>\n</body>\n</html>\n"), path
        assert "cloudflareinsights" not in page, path


def test_analytics_snippet_goes_just_before_body_end_on_every_page(repo, site):
    write(site / "analytics.html", "\n" + BEACON + "\n\n")
    run(repo, site)
    pages = _pages(site)
    assert len(pages) == 3
    for path, page in pages.items():
        assert page.count(BEACON) == 1, path
        assert page.endswith(BEACON + "\n</body>\n</html>\n"), path
    assert not (site / "wiki" / "analytics.html").exists(), "read, never copied into wiki/"
    assert "cloudflareinsights" not in (site / "wiki" / "search-index.json").read_text()
    assert (site / "analytics.html").read_text() == "\n" + BEACON + "\n\n", "the site's own file is left alone"


def test_analytics_snippet_is_scrubbed_with_the_pages(repo, site, tmp_path):
    write(site / "analytics.html", "<!-- Alphaterm -->\n" + BEACON + "\n")
    deny = write(tmp_path / "deny.txt", "Alphaterm\n")
    before = snapshot(site)
    with pytest.raises(WikiError, match="Alphaterm"):
        run(repo, site, denylist=deny)
    assert snapshot(site) == before


def test_analytics_snippet_must_be_a_plain_small_file(repo, site, tmp_path):
    real = write(tmp_path / "elsewhere.html", BEACON + "\n")
    (site / "analytics.html").symlink_to(real)
    with pytest.raises(WikiError, match="plain file"):
        run(repo, site)
    (site / "analytics.html").unlink()
    write(site / "analytics.html", "x" * (16 * 1024 + 1))
    with pytest.raises(WikiError, match="bytes"):
        run(repo, site)


def test_deterministic(repo, site, tmp_path):
    other = tmp_path / "site2"
    shutil.copytree(site, other)
    run(repo, site)
    run(repo, other)
    assert snapshot(site) == snapshot(other)


def test_refuses_wrong_cname(repo, site):
    write(site / "CNAME", "example.com\n")
    before = snapshot(site)
    with pytest.raises(WikiError, match=r"graftipa.com"):
        run(repo, site)
    assert snapshot(site) == before


def test_refuses_a_site_without_og_png(repo, site):
    """Every page's og:image points at the site's og.png, so it must be there."""
    (site / "og.png").unlink()
    before = snapshot(site)
    with pytest.raises(WikiError, match=r"og\.png: not found"):
        run(repo, site)
    assert snapshot(site) == before


@pytest.mark.parametrize(
    "data",
    [_png(1200, 631, (0, 0, 0)), _png(630, 1200, (0, 0, 0)), b"GIF89a not a png"],
    ids=["one-row-taller", "portrait", "not-a-png"],
)
def test_refuses_an_og_png_that_isnt_1200x630(repo, site, data):
    """The pages say og:image is 1200×630; an image of any other size makes that a lie."""
    (site / "og.png").write_bytes(data)
    before = snapshot(site)
    with pytest.raises(WikiError, match=r"og\.png: "):
        run(repo, site)
    assert snapshot(site) == before


def test_removes_stale_files_under_wiki(repo, site):
    write(site / "wiki/old/index.html", "stale")
    write(site / ".wiki-build/leftover", "stale")
    run(repo, site)
    assert not (site / "wiki/old").exists()
    assert not (site / ".wiki-build").exists()


def test_never_touches_other_site_files(repo, site):
    keep = {n: (site / n).read_bytes() for n in ("index.html", "style.css", "CNAME")}
    run(repo, site)
    assert {n: (site / n).read_bytes() for n in keep} == keep


def test_failed_build_leaves_site_untouched(repo, site):
    run(repo, site)
    before = snapshot(site)
    write(repo / "docs/wiki/first.md", "# First Page\n\nSee [x](nowhere.md).\n")
    with pytest.raises(WikiError, match="broken link"):
        run(repo, site, lastmod="2026-12-02")
    assert snapshot(site) == before


def test_private_layer_hit_fails_and_lists_all(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "# private\nAlphaterm\nBetaterm\n")
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nAlphaterm is here.\n\nAnd Betaterm too.\n\n![A shot](img/shot-light.png)\n",
    )
    before = snapshot(site)
    with pytest.raises(WikiError) as e:
        run(repo, site, denylist=deny)
    lines = str(e.value).splitlines()
    assert any("Alphaterm" in ln for ln in lines) and any("Betaterm" in ln for ln in lines)
    assert all(ln.startswith("wiki/") for ln in lines)
    assert snapshot(site) == before


def test_denylist_reports_private_layer(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Neverpresent\n")
    assert run(repo, site, denylist=deny).private_layer is True


def test_missing_denylist_is_error(repo, site, tmp_path):
    with pytest.raises(WikiError, match="scrub list"):
        run(repo, site, denylist=tmp_path / "nope.txt")


def test_builtin_hit_fails(repo, site):
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nSee /Users/alex/thing.\n\n![A shot](img/shot-light.png)\n",
    )
    with pytest.raises(WikiError, match="/Users/ path"):
        run(repo, site)


def test_term_split_by_markup_is_caught(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Two Words\n")
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nUse `Two` Words here.\n\n![A shot](img/shot-light.png)\n",
    )
    with pytest.raises(WikiError, match=r"\(text\):\d+: Two Words"):
        run(repo, site, denylist=deny)


def test_term_with_ampersand_is_caught(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Some & Co\n")
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nMade by Some & Co today.\n\n![A shot](img/shot-light.png)\n",
    )
    with pytest.raises(WikiError, match=r"index\.html \(text\):\d+: Some & Co"):
        run(repo, site, denylist=deny)


@pytest.mark.parametrize("term", ["Some & Co", "Alex's Phone"])
def test_term_only_in_alt_text_is_caught(repo, site, tmp_path, term):
    """html.escape turns `&` and `'` into entities inside the attribute, and the text views
    drop the attribute with its tag, so only the unescaped view can see the term."""
    deny = write(tmp_path / "deny.txt", f"{term}\n")
    write(
        repo / "docs/wiki/first.md",
        f"# First Page\n\nThe first page.\n\n![{term} window](img/shot-light.png)\n",
    )
    before = snapshot(site)
    with pytest.raises(WikiError) as e:
        run(repo, site, denylist=deny)
    lines = str(e.value).splitlines()
    assert lines and all(ln.startswith("wiki/first/index.html (unescaped):") for ln in lines)
    assert all(f": {term}: " in ln for ln in lines)
    assert snapshot(site) == before


def test_term_in_sitemap_is_caught(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Sitemapterm\n")
    xml = (site / "sitemap.xml").read_text()
    entry = "<url><loc>https://graftipa.com/Sitemapterm/</loc></url>\n</urlset>"
    write(site / "sitemap.xml", xml.replace("</urlset>", entry))
    with pytest.raises(WikiError, match=r"sitemap.xml"):
        run(repo, site, denylist=deny)


def test_term_in_search_index_is_caught(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Heading Only\n")
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nIntro.\n\n## Heading Only\n\nBody.\n\n![A shot](img/shot-light.png)\n",
    )
    with pytest.raises(WikiError, match=r"search-index.json"):
        run(repo, site, denylist=deny)


def test_home_must_link_every_page(repo, site):
    write(repo / "docs/wiki/README.md", "# Graft Docs\n\nWelcome.\n\n- [First](first.md)\n")
    with pytest.raises(WikiError, match="second"):
        run(repo, site)


def test_unused_image_is_error(repo, site):
    write(repo / "docs/wiki/img/extra-light.png", "x")
    with pytest.raises(WikiError, match=r"unused image.*extra-light.png"):
        run(repo, site)


def test_single_image_is_copied_and_counted(repo, site):
    (repo / "docs/wiki/img/solo.png").write_bytes(_png(6, 3, (0, 0, 0)))
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nThe first page.\n\n![A shot](img/shot-light.png)\n\n"
        "![Solo](img/solo.png)\n",
    )
    r = run(repo, site)
    assert (site / "wiki/img/solo.png").read_bytes() == _png(6, 3, (0, 0, 0))
    assert r.images == 3


def test_unused_single_image_is_error(repo, site):
    (repo / "docs/wiki/img/solo.png").write_bytes(_png(6, 3, (0, 0, 0)))
    with pytest.raises(WikiError, match=r"unused image.*solo.png"):
        run(repo, site)


def test_single_image_is_ocr_and_png_text_scanned(repo, site, tmp_path, monkeypatch):
    deny = write(tmp_path / "deny.txt", "Pngterm\n")
    chunk = (b"tEXt", b"Comment\x00Pngterm was here")
    (repo / "docs/wiki/img/solo.png").write_bytes(_png(6, 3, (0, 0, 0), extra=[chunk]))
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nThe first page.\n\n![A shot](img/shot-light.png)\n\n"
        "![Solo](img/solo.png)\n",
    )
    seen = []
    monkeypatch.setattr(build_mod, "ocr_texts", lambda paths: seen.extend(paths) or {})
    with pytest.raises(WikiError) as e:
        build(repo_root=repo, out=site, lastmod=LASTMOD, denylist=deny, check_images=True)
    assert "solo.png" in [p.name for p in seen]
    assert "wiki/img/solo.png (png text)" in str(e.value)


def test_missing_image_folder_is_fine(repo, site):
    shutil.rmtree(repo / "docs/wiki/img")
    write(repo / "docs/wiki/first.md", "# First Page\n\nThe first page.\n")
    assert run(repo, site).images == 0


PNG_TEXT_CHUNKS = {
    "tEXt": (b"tEXt", b"Comment\x00Pngterm was here"),
    "tEXt-keyword": (b"tEXt", b"Pngterm\x00a value"),
    "zTXt": (b"zTXt", b"Comment\x00\x00" + zlib.compress(b"Pngterm was here")),
    "iTXt": (b"iTXt", b"Description\x00\x00\x00en\x00\x00Pngterm was here"),
    "iTXt-compressed": (
        b"iTXt",
        b"Description\x00\x01\x00en\x00\x00" + zlib.compress(b"Pngterm was here"),
    ),
    "iTXt-translated-keyword": (b"iTXt", b"Title\x00\x00\x00en\x00Pngterm\x00a value"),
    "eXIf": (b"eXIf", b"MM\x00*\x00\x00\x00\x08Pngterm was here\x00"),
}


@pytest.mark.parametrize("chunk", PNG_TEXT_CHUNKS.values(), ids=PNG_TEXT_CHUNKS.keys())
def test_png_text_chunk_hit_fails(repo, site, tmp_path, chunk):
    deny = write(tmp_path / "deny.txt", "Pngterm\n")
    (repo / "docs/wiki/img/shot-dark.png").write_bytes(_png(4, 2, (0, 0, 0), extra=[chunk]))
    before = snapshot(site)
    with pytest.raises(WikiError) as e:
        run(repo, site, denylist=deny)
    lines = str(e.value).splitlines()
    assert len(lines) == 1 and lines[0].startswith("wiki/img/shot-dark.png (png text):")
    assert ": Pngterm: " in lines[0] and "\0" not in lines[0]
    assert snapshot(site) == before


def test_png_bytes_are_copied_unchanged(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Pngterm\n")
    data = _png(4, 2, (0, 0, 0), extra=[(b"tEXt", b"Comment\x00a harmless note")])
    (repo / "docs/wiki/img/shot-dark.png").write_bytes(data)
    run(repo, site, denylist=deny)
    assert (site / "wiki/img/shot-dark.png").read_bytes() == data


@pytest.mark.parametrize("cut", [40, 50], ids=["in-chunk-header", "in-chunk-data"])
def test_png_with_a_truncated_chunk_is_error(repo, site, cut):
    # The tEXt chunk's header runs from byte 33 to 41, its data and CRC from 41 to 69.
    data = _png(4, 2, (0, 0, 0), extra=[(b"tEXt", b"Comment\x00a harmless note")])
    (repo / "docs/wiki/img/shot-dark.png").write_bytes(data[:cut])
    with pytest.raises(WikiError, match=r"shot-dark\.png: truncated PNG"):
        run(repo, site)


def test_png_with_data_after_iend_is_error(repo, site):
    data = _png(4, 2, (0, 0, 0)) + b"placeholder trailing bytes"
    (repo / "docs/wiki/img/shot-dark.png").write_bytes(data)
    with pytest.raises(WikiError, match=r"shot-dark\.png: data after the IEND chunk"):
        run(repo, site)


def test_png_with_unreadable_compressed_text_is_error(repo, site):
    chunk = (b"zTXt", b"Comment\x00\x00placeholder, not zlib")
    (repo / "docs/wiki/img/shot-dark.png").write_bytes(_png(4, 2, (0, 0, 0), extra=[chunk]))
    with pytest.raises(WikiError, match=r"shot-dark\.png: unreadable zTXt chunk"):
        run(repo, site)


def test_check_images_ocr_hit_fails_and_leaves_site_untouched(repo, site, tmp_path, monkeypatch):
    deny = write(tmp_path / "deny.txt", "Alphaterm\n")
    monkeypatch.setattr(
        build_mod,
        "ocr_texts",
        lambda paths: {p: ("Alphaterm" if "light" in p.name else "clean") for p in paths},
    )
    before = snapshot(site)
    with pytest.raises(WikiError) as e:
        build(repo_root=repo, out=site, lastmod=LASTMOD, denylist=deny, check_images=True)
    assert str(e.value).startswith("wiki/img/shot-light.png (OCR):1: Alphaterm")
    assert len(str(e.value).splitlines()) == 1
    assert snapshot(site) == before


def test_check_images_clean_sets_ocr_flag(repo, site, monkeypatch):
    seen = []
    monkeypatch.setattr(build_mod, "ocr_texts", lambda paths: seen.extend(paths) or {})
    report = build(repo_root=repo, out=site, lastmod=LASTMOD, denylist=None, check_images=True)
    assert report.ocr and [p.name for p in seen] == ["shot-dark.png", "shot-light.png"]


def test_check_images_without_images_skips_swift(repo, site, monkeypatch):
    write(repo / "docs/wiki/first.md", "# First Page\n\nThe first page.\n")
    for f in (repo / "docs/wiki/img").iterdir():
        f.unlink()
    monkeypatch.setattr(build_mod.subprocess, "run", lambda *a, **k: pytest.fail("ran swift"))
    report = build(repo_root=repo, out=site, lastmod=LASTMOD, denylist=None, check_images=True)
    assert report.ocr and report.images == 0


def test_script_is_executable(repo):
    script = Path(__file__).resolve().parents[2] / "scripts/build-wiki"
    assert script.stat().st_mode & stat.S_IXUSR
    assert script.read_text().startswith("#!/usr/bin/env bash\n")


@pytest.fixture
def cli(repo, monkeypatch) -> list[list[str]]:
    """main() on the fake repo. Records each OCR call's file names; never launches Swift."""
    monkeypatch.setattr(build_mod, "REPO_ROOT", repo)
    monkeypatch.delenv("GRAFT_SCRUB_DENYLIST", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_mod, "ocr_texts", lambda paths: calls.append([p.name for p in paths]) or {}
    )
    return calls


SKIP_WARNING = (
    "warning: images were not OCR-checked (--skip-image-check); don't publish this build."
)


def test_main_warns_without_denylist(cli, site, capsys):
    assert main(["--out", str(site), "--lastmod", LASTMOD]) == 0
    cap = capsys.readouterr()
    assert f"built 3 pages, 2 images into {site}/wiki" in cap.out
    assert "warning: no private scrub list" in cap.err


def test_main_ocr_checks_images_by_default(cli, site, capsys):
    assert main(["--out", str(site), "--lastmod", LASTMOD]) == 0
    assert cli == [["shot-dark.png", "shot-light.png"]]
    assert "OCR" not in capsys.readouterr().err


def test_main_check_images_flag_is_a_harmless_no_op(cli, site):
    assert main(["--out", str(site), "--lastmod", LASTMOD, "--check-images"]) == 0
    assert cli == [["shot-dark.png", "shot-light.png"]]


def test_main_skip_image_check_warns_and_skips_ocr(cli, site, capsys):
    assert main(["--out", str(site), "--lastmod", LASTMOD, "--skip-image-check"]) == 0
    assert cli == []
    assert SKIP_WARNING in capsys.readouterr().err.splitlines()


def test_main_without_images_never_runs_ocr_or_warns(cli, repo, site, capsys):
    write(repo / "docs/wiki/first.md", "# First Page\n\nThe first page.\n")
    shutil.rmtree(repo / "docs/wiki/img")
    for extra in ([], ["--skip-image-check"]):
        assert main(["--out", str(site), "--lastmod", LASTMOD, *extra]) == 0
        assert "OCR" not in capsys.readouterr().err
    assert cli == []


def test_main_reads_denylist_from_env(cli, site, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRAFT_SCRUB_DENYLIST", str(write(tmp_path / "d.txt", "Neverpresent\n")))
    assert main(["--out", str(site), "--lastmod", LASTMOD]) == 0
    assert "warning" not in capsys.readouterr().err


def test_flag_beats_env(cli, site, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRAFT_SCRUB_DENYLIST", str(write(tmp_path / "d.txt", "Neverpresent\n")))
    code = main(["--out", str(site), "--lastmod", LASTMOD, "--denylist", str(tmp_path / "no.txt")])
    assert code == 1
    assert "error:" in capsys.readouterr().out


def test_main_returns_1_and_prints_errors(cli, repo, site, capsys):
    write(repo / "docs/wiki/second.md", "# Second Page\n\nSee [x](gone.md).\n")
    assert main(["--out", str(site), "--lastmod", LASTMOD]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ") and "broken link" in out


def test_main_rejects_bad_lastmod(cli, site, capsys):
    assert main(["--out", str(site), "--lastmod", "12/01/2026"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_main_without_lastmod_and_no_history_fails(cli, site, capsys):
    assert main(["--out", str(site)]) == 1
    assert (
        "error: no --lastmod and no git history for docs/wiki; pass --lastmod YYYY-MM-DD"
        in capsys.readouterr().out
    )


def test_git_lastmod_covers_page_sources_outside_the_wiki(repo, monkeypatch):
    write(repo / "CHANGELOG.md", "# Changelog\n\nEach release.\n")
    changelog = '\n[[group.page]]\nslug = "changelog"\nsource = "../../CHANGELOG.md"\n'
    write(repo / "docs/wiki/wiki.toml", WIKI_TOML + changelog)
    calls = []

    def fake_git(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="2026-11-24\n", stderr="")

    monkeypatch.setattr(build_mod.subprocess, "run", fake_git)
    assert build_mod._git_lastmod(repo) == "2026-11-24"
    assert calls[0][calls[0].index("--") + 1 :] == ["docs/wiki", "CHANGELOG.md"]


def test_term_split_inside_a_word_is_caught(repo, site, tmp_path):
    deny = write(tmp_path / "deny.txt", "Foobar\n")
    write(
        repo / "docs/wiki/first.md",
        "# First Page\n\nUse Foo*bar* here.\n\n![A shot](img/shot-light.png)\n",
    )
    with pytest.raises(WikiError, match=r"index\.html \(joined\):\d+: Foobar"):
        run(repo, site, denylist=deny)


def test_failed_swap_restores_previous_wiki_and_main_reports(cli, site, capsys, monkeypatch):
    run(build_mod.REPO_ROOT, site)
    before = snapshot(site / "wiki")
    real = Path.rename

    def flaky(self, target):
        if self.name == ".wiki-build":
            raise OSError("disk went away")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    assert main(["--out", str(site), "--lastmod", "2026-12-02"]) == 1
    assert "error: disk went away" in capsys.readouterr().out
    assert snapshot(site / "wiki") == before
    assert not (site / ".wiki-old").exists()
