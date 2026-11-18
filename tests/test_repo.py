from __future__ import annotations

import gzip as _gzip
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import repo
from graft_ipa.repo import RepoError

# The patch target below is `requests.get`, not `graft_ipa.repo.requests.get`,
# which is what it used to be: `repo._http_get` imports `requests` inside the
# function now (measured startup cost — see that import's comment), so the
# module has no `requests` attribute to reach through. The object being
# replaced is the same one either way — the old target resolved to this same
# library module.


def test_normalize_url_strips_trailing_slash() -> None:
    assert repo.normalize_url("https://apt.example.com/") == "https://apt.example.com"
    assert repo.normalize_url("  https://apt.example.com  ") == "https://apt.example.com"


def test_normalize_url_rejects_non_http() -> None:
    with pytest.raises(RepoError):
        repo.normalize_url("ftp://apt.example.com")
    with pytest.raises(RepoError):
        repo.normalize_url("")


def test_version_key_orders_numerically() -> None:
    assert repo._version_key("1.10.0") > repo._version_key("1.2.0")
    assert repo._version_key("2.0") > repo._version_key("1.99")
    assert repo._version_key("1.0") == repo._version_key("1.0")


SAMPLE_PACKAGES = """\
Package: com.example.alpha
Name: Alpha Tweak
Version: 1.2.0
Architecture: iphoneos-arm
Description: First tweak
 with a continuation line.
Author: Alice <alice@example.com>
Section: Tweaks
Filename: debs/alpha_1.2.0.deb
Size: 12345
SHA256: aaa111
Depiction: https://example.com/depiction/alpha

Package: com.example.alpha
Name: Alpha Tweak
Version: 1.10.0
Description: Newer Alpha
Filename: debs/alpha_1.10.0.deb
Size: 13000

Package: com.example.beta
Name: Beta Tweak
Version: 0.9
Maintainer: Bob
Section: Tweaks
Filename: ./debs/beta.deb
"""


def test_parse_packages_parses_fields() -> None:
    pkgs = {p.package_id: p for p in repo.parse_packages(SAMPLE_PACKAGES)}
    beta = pkgs["com.example.beta"]
    assert beta.name == "Beta Tweak"
    assert beta.version == "0.9"
    assert beta.author == "Bob"  # falls back to Maintainer
    assert beta.filename == "./debs/beta.deb"
    assert beta.size is None  # no Size field


def test_parse_packages_dedups_keeping_highest_version() -> None:
    pkgs = {p.package_id: p for p in repo.parse_packages(SAMPLE_PACKAGES)}
    assert len(pkgs) == 2
    alpha = pkgs["com.example.alpha"]
    assert alpha.version == "1.10.0"
    assert alpha.size == 13000


def test_parse_packages_reads_description_first_line() -> None:
    pkgs = {p.package_id: p for p in repo.parse_packages(SAMPLE_PACKAGES)}
    # The continuation line is part of the description; first line is enough for us.
    assert pkgs["com.example.alpha"].description.startswith("Newer Alpha")


def test_parse_packages_handles_crlf_line_endings() -> None:
    crlf = SAMPLE_PACKAGES.replace("\n", "\r\n")
    pkgs = {p.package_id: p for p in repo.parse_packages(crlf)}
    assert set(pkgs) == {"com.example.alpha", "com.example.beta"}
    assert pkgs["com.example.alpha"].version == "1.10.0"


def _resp(status: int, *, body: bytes = b"", text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = body
    r.text = text
    r.headers = {"content-type": "application/octet-stream"}
    return r


def test_fetch_packages_falls_back_to_gz() -> None:
    gz_body = _gzip.compress(SAMPLE_PACKAGES.encode("utf-8"))

    def fake_get(url, **kwargs):
        if url.endswith("Packages.xz") or url.endswith("Packages.bz2"):
            return _resp(404)
        if url.endswith("Packages.gz"):
            return _resp(200, body=gz_body)
        return _resp(404)

    with patch("requests.get", side_effect=fake_get):
        text = repo.fetch_packages("https://apt.example.com")
    assert "com.example.alpha" in text


def test_fetch_packages_raises_when_no_variant_found() -> None:
    with patch("requests.get", return_value=_resp(404)):
        with pytest.raises(RepoError):
            repo.fetch_packages("https://apt.example.com")


def test_fetch_release_parses_origin() -> None:
    release = "Origin: Example Repo\nLabel: example\nSuite: stable\n"
    with patch("requests.get", return_value=_resp(200, text=release)):
        r = repo.fetch_release("https://apt.example.com")
    assert r.name == "Example Repo"
    assert r.origin == "Example Repo"
    assert r.url == "https://apt.example.com"


def test_fetch_release_raises_when_missing() -> None:
    with patch("requests.get", return_value=_resp(404)):
        with pytest.raises(RepoError):
            repo.fetch_release("https://apt.example.com")


# ---------------------------------------------------------------------------
# Bug 1 — download_url: mishandles ./-prefixed and absolute-URL Filenames
# ---------------------------------------------------------------------------


def _make_pkg(**kwargs) -> repo.RepoPackage:
    defaults = dict(
        package_id="com.example.x",
        name="X",
        version="1.0",
        description="",
        author="",
        section="",
        filename="",
        size=None,
        sha256=None,
        depiction=None,
    )
    defaults.update(kwargs)
    return repo.RepoPackage(**defaults)


def test_download_url_strips_dot_slash_prefix() -> None:
    pkg = _make_pkg(filename="./debs/x.deb")
    assert repo.download_url("https://r.example.com", pkg) == "https://r.example.com/debs/x.deb"


def test_download_url_passes_through_absolute_url() -> None:
    pkg = _make_pkg(filename="https://cdn.example.com/x.deb")
    assert repo.download_url("https://r.example.com", pkg) == "https://cdn.example.com/x.deb"


def test_download_url_strips_leading_slash() -> None:
    pkg = _make_pkg(filename="/pool/x.deb")
    assert repo.download_url("https://r.example.com", pkg) == "https://r.example.com/pool/x.deb"


# ---------------------------------------------------------------------------
# Bug 2 — fetch_packages: truncated gzip stream raises uncaught EOFError
# ---------------------------------------------------------------------------


def test_fetch_packages_skips_truncated_gz_variant() -> None:
    import gzip as _gz

    truncated_gz = _gz.compress(SAMPLE_PACKAGES.encode("utf-8"))[:-10]

    def fake_get(url, **kwargs):
        if url.endswith("Packages.xz") or url.endswith("Packages.bz2"):
            return _resp(404)
        if url.endswith("Packages.gz"):
            return _resp(200, body=truncated_gz)
        # plain Packages
        return _resp(200, body=SAMPLE_PACKAGES.encode("utf-8"), text=SAMPLE_PACKAGES)

    with patch("requests.get", side_effect=fake_get):
        text = repo.fetch_packages("https://apt.example.com")
    assert "com.example.alpha" in text


# ---------------------------------------------------------------------------
# Bug 3 — parse_packages: stanza splitter splits on whitespace-only lines
# ---------------------------------------------------------------------------


def test_parse_packages_keeps_stanza_with_whitespace_only_description_line() -> None:
    text = (
        "Package: com.example.x\n"
        "Name: X\n"
        "Version: 1.0\n"
        "Description: line one\n"
        "   \n"
        " line three\n"
        "Filename: debs/x.deb\n"
    )
    pkgs = repo.parse_packages(text)
    assert len(pkgs) == 1
    assert pkgs[0].package_id == "com.example.x"
    assert pkgs[0].filename == "debs/x.deb"


# ---------------------------------------------------------------------------
# Bug 4 — _version_key: ~ prereleases and epochs sort wrong
# ---------------------------------------------------------------------------


def test_version_key_tilde_sorts_before_release() -> None:
    assert repo._version_key("1.0") > repo._version_key("1.0~beta1")


def test_version_key_tilde_ordering() -> None:
    assert repo._version_key("1.0~beta2") > repo._version_key("1.0~beta1")


def test_version_key_epoch_wins() -> None:
    assert repo._version_key("1:0.1") > repo._version_key("9.9")


def test_parse_packages_dedup_prefers_release_over_prerelease() -> None:
    text = (
        "Package: com.example.x\n"
        "Version: 1.0~beta1\n"
        "Filename: debs/x_beta.deb\n"
        "\n"
        "Package: com.example.x\n"
        "Version: 1.0\n"
        "Filename: debs/x_release.deb\n"
    )
    pkgs = repo.parse_packages(text)
    assert len(pkgs) == 1
    assert pkgs[0].version == "1.0"
