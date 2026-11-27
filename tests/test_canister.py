from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import canister
from graft_ipa.canister import CanisterError

# The patch target below is `requests.get`, not `graft_ipa.canister.requests.
# get`, which is what it used to be: `canister._get` and `canister.fetch_deb`
# each import `requests` inside the function now (measured startup cost — see
# those imports' comments), so the module has no `requests` attribute to reach
# through. The object being replaced is the same one either way — the old
# target resolved to this same library module.

SAMPLE_PACKAGE = {
    "package_id": "com.example.ctweak",
    "name": "Example Tweak for Example Music",
    "version": "0.0.1",
    "description": "A sample tweak for Example Music",
    "author": "example-author",
    "section": "Tweaks",
    "repository_id": "havoc",
    "package_filename": "api/download/package/abc/com.example.ctweak_0.0.1_iphoneos-arm.deb",
    "package_size": 36452,
    "sha256_hash": "deadbeef",
    "price": "Free",
    "depiction": "https://havoc.app/depiction/x",
}
SAMPLE_REPO = {
    "id": "havoc",
    "name": "Havoc",
    "uri": "https://havoc.app",
}


def _mock_response(json_payload, status=200, content_type="application/json", body=b""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_payload
    resp.text = ""
    resp.headers = {"content-type": content_type}
    resp.iter_content = MagicMock(return_value=iter([body]))
    resp.raise_for_status = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_search_dedups_by_package_id() -> None:
    payload = {"status": "200 OK", "count": 2, "data": [SAMPLE_PACKAGE, SAMPLE_PACKAGE]}
    with patch("requests.get", return_value=_mock_response(payload)):
        results = canister.search("sample")
    assert len(results) == 1
    assert results[0].package_id == "com.example.ctweak"
    assert results[0].repository_id == "havoc"


def test_search_empty_query_raises() -> None:
    with pytest.raises(CanisterError):
        canister.search("   ")


def test_download_url_constructs_from_repo() -> None:
    pkg = canister._to_package(SAMPLE_PACKAGE)
    repo = canister.Repository(repo_id="havoc", name="Havoc", uri="https://havoc.app")
    url = canister.download_url(pkg, repo)
    assert url == "https://havoc.app/api/download/package/abc/com.example.ctweak_0.0.1_iphoneos-arm.deb"


def test_download_url_rejects_missing_uri() -> None:
    pkg = canister._to_package(SAMPLE_PACKAGE)
    repo = canister.Repository(repo_id="havoc", name="Havoc", uri="")
    with pytest.raises(CanisterError):
        canister.download_url(pkg, repo)


def test_fetch_deb_by_package_id_sha_match(tmp_path: Path) -> None:
    body = b"fake-deb-bytes"
    real_sha = hashlib.sha256(body).hexdigest()
    pkg = dict(SAMPLE_PACKAGE, sha256_hash=real_sha)

    def fake_get(url, *args, **kwargs):
        if "/jailbreak/package/" in url and "search" not in url:
            return _mock_response({"data": pkg})
        if "/jailbreak/repository/" in url:
            return _mock_response({"data": SAMPLE_REPO})
        return _mock_response({}, content_type="application/octet-stream", body=body)

    with patch("requests.get", side_effect=fake_get):
        result = canister.fetch_deb("com.example.ctweak", tmp_path)

    assert result.exists()
    assert result.name.endswith(".deb")
    assert result.read_bytes() == body


def test_fetch_deb_by_package_id_sha_mismatch(tmp_path: Path) -> None:
    pkg = dict(SAMPLE_PACKAGE, sha256_hash="nope")

    def fake_get(url, *args, **kwargs):
        if "/jailbreak/package/" in url and "search" not in url:
            return _mock_response({"data": pkg})
        if "/jailbreak/repository/" in url:
            return _mock_response({"data": SAMPLE_REPO})
        return _mock_response({}, content_type="application/octet-stream", body=b"junk")

    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.ctweak", tmp_path)
    assert "sha256 mismatch" in str(exc.value)
    assert not any(tmp_path.iterdir())


def test_fetch_deb_by_url_skips_canister(tmp_path: Path) -> None:
    body = b"hello"
    with patch(
        "requests.get",
        return_value=_mock_response({}, content_type="application/octet-stream", body=body),
    ) as mock_get:
        result = canister.fetch_deb("https://repo.example/x.deb", tmp_path)
    # When fetching by URL we make exactly one request (no /jailbreak/* lookups).
    assert mock_get.call_count == 1
    assert result.read_bytes() == body
    assert result.name == "x.deb"


def test_fetch_deb_rejects_html_body(tmp_path: Path) -> None:
    with patch(
        "requests.get",
        return_value=_mock_response({}, content_type="text/html; charset=utf-8", body=b"<html/>"),
    ):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("https://repo.example/x.deb", tmp_path)
    assert "HTML" in str(exc.value)


def test_fetch_deb_url_verifies_supplied_sha_match(tmp_path: Path) -> None:
    body = b"correct-deb-content"
    real_sha = hashlib.sha256(body).hexdigest()
    with patch(
        "requests.get",
        return_value=_mock_response({}, content_type="application/octet-stream", body=body),
    ):
        result = canister.fetch_deb("https://repo.example/pkg.deb", tmp_path, expected_sha=real_sha)
    assert result.exists()
    assert result.read_bytes() == body


def test_fetch_deb_url_rejects_supplied_sha_mismatch(tmp_path: Path) -> None:
    body = b"some-deb-content"
    wrong_sha = "0" * 64
    with patch(
        "requests.get",
        return_value=_mock_response({}, content_type="application/octet-stream", body=body),
    ):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("https://repo.example/pkg.deb", tmp_path, expected_sha=wrong_sha)
    assert "sha256 mismatch" in str(exc.value)
    assert not any(tmp_path.iterdir())


def test_fetch_deb_url_sha_match_is_case_insensitive(tmp_path: Path) -> None:
    body = b"correct-deb-content"
    real_sha = hashlib.sha256(body).hexdigest().upper()
    with patch(
        "requests.get",
        return_value=_mock_response({}, content_type="application/octet-stream", body=body),
    ):
        result = canister.fetch_deb("https://repo.example/pkg.deb", tmp_path, expected_sha=real_sha)
    assert result.exists()
    assert result.read_bytes() == body


# --- a package id answers with every version and architecture it ever had -----
#
# Seen for real on com.example.sometweak (2026-11-17): the endpoint returns ~70 rows in no
# stable order. The code took row 0, so "latest" was whichever row the API
# listed first on *that request* — the status check reported 1.7.24, the
# download then fetched a 1.7.20 arm64 file the repo no longer served (404),
# and `--fetch` succeeded only because its request happened to list 1.7.24 arm
# first.

def _sometweak(version: str, arch: str) -> dict:
    return dict(
        SAMPLE_PACKAGE,
        package_id="com.example.sometweak",
        version=version,
        sha256_hash=None,
        package_filename=f"./debs/example/sometweak/com.example.sometweak_{version}_{arch}.deb",
    )


# The stale 1.7.20 arm64 row first, exactly as the real API listed it.
SOMETWEAK_ROWS = [
    _sometweak("1.7.20", "iphoneos-arm64"),
    _sometweak("1.7.24", "iphoneos-arm"),
    _sometweak("1.7.24", "iphoneos-arm64"),
    _sometweak("1.7.19", "iphoneos-arm64"),
]


def _sometweak_world(*, gone: tuple[str, ...] = (), body: bytes = b"deb"):
    """A fake canister + repo. Files named in `gone` 404 like the real repo."""
    import requests

    requested: list[str] = []

    def fake_get(url, *args, **kwargs):
        if "/jailbreak/package/" in url and "search" not in url:
            return _mock_response({"data": SOMETWEAK_ROWS})
        if "/jailbreak/repository/" in url:
            return _mock_response({"data": dict(SAMPLE_REPO, uri="https://repo.example")})
        requested.append(url)
        resp = _mock_response({}, content_type="application/octet-stream", body=body)
        if any(name in url for name in gone):
            resp.status_code = 404
            resp.raise_for_status.side_effect = requests.HTTPError(
                f"404 Client Error: Not Found for url: {url}", response=resp
            )
        return resp

    return fake_get, requested


def test_get_package_returns_the_highest_version_not_the_first_row() -> None:
    fake_get, _ = _sometweak_world()
    with patch("requests.get", side_effect=fake_get):
        package = canister.get_package("com.example.sometweak")
    assert package.version == "1.7.24"


def test_fetch_deb_downloads_the_version_it_was_told_to(tmp_path: Path) -> None:
    fake_get, requested = _sometweak_world()
    with patch("requests.get", side_effect=fake_get):
        dest = canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24")
    assert dest.name.startswith("com.example.sometweak_1.7.24_")
    assert not any("1.7.20" in u for u in requested)


def test_fetch_deb_falls_back_to_another_variant_of_the_same_version(tmp_path: Path) -> None:
    """The rootless 1.7.24 build is gone but the rootful one is up — which is
    fine here, since cyan rewrites the substrate path either way."""
    fake_get, requested = _sometweak_world(gone=("1.7.24_iphoneos-arm64.deb",))
    with patch("requests.get", side_effect=fake_get):
        dest = canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24")
    assert dest.name == "com.example.sometweak_1.7.24_iphoneos-arm.deb"
    assert dest.read_bytes() == b"deb"
    # arm64 was tried first (and 404'd), then arm — the order is the contract,
    # and SOMETWEAK_ROWS lists arm before arm64 so only the sort can produce it.
    assert [u.rsplit("/", 1)[-1] for u in requested] == [
        "com.example.sometweak_1.7.24_iphoneos-arm64.deb",
        "com.example.sometweak_1.7.24_iphoneos-arm.deb",
    ]


def test_get_package_is_the_same_whatever_order_canister_lists_rows() -> None:
    """The bug was that the answer depended on row order."""
    answers = []
    for rows in (SOMETWEAK_ROWS, list(reversed(SOMETWEAK_ROWS))):
        with patch.object(canister, "_get", return_value={"data": rows}):
            answers.append(canister.get_package("com.example.sometweak").package_filename)
    assert answers[0] == answers[1]
    assert answers[0].endswith("com.example.sometweak_1.7.24_iphoneos-arm64.deb")


def test_rows_with_no_filename_do_not_hide_the_404s(tmp_path: Path) -> None:
    rows = SOMETWEAK_ROWS + [dict(_sometweak("1.7.24", "iphoneos-arm64e"), package_filename="")]
    fake_http, _ = _sometweak_world(gone=("1.7.24_",))

    def wrapped(url, *a, **k):
        if "/jailbreak/package/" in url and "search" not in url:
            return _mock_response({"data": rows})
        return fake_http(url, *a, **k)

    with patch("requests.get", side_effect=wrapped):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24")
    assert "404" in exc.value.message
    assert "iphoneos-arm64.deb" in exc.value.message


def test_a_version_with_only_filenameless_rows_counts_as_unlisted(tmp_path: Path) -> None:
    rows = [dict(_sometweak("3.0.0", "iphoneos-arm64"), package_filename="")]
    with patch.object(canister, "_get", return_value={"data": rows}):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.sometweak", tmp_path, version="3.0.0")
    assert "lists no version 3.0.0" in exc.value.message


def test_variant_of_reads_the_architecture_off_a_canister_filename() -> None:
    assert canister.variant_of("com.example.sometweak_1.7.24_iphoneos-arm.deb") == "iphoneos-arm"
    assert canister.variant_of("com.example.sometweak_1.7.24_iphoneos-arm64e.deb") == "iphoneos-arm64e"
    assert canister.variant_of("some-tweak.deb") is None


def test_fetch_deb_never_settles_for_an_older_version(tmp_path: Path) -> None:
    fake_get, requested = _sometweak_world(
        gone=("1.7.24_iphoneos-arm64.deb", "1.7.24_iphoneos-arm.deb")
    )
    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24")
    message = exc.value.message
    assert "1.7.24" in message
    # Both URLs that 404'd, by name — not just "download failed".
    assert "com.example.sometweak_1.7.24_iphoneos-arm64.deb" in message
    assert "com.example.sometweak_1.7.24_iphoneos-arm.deb" in message
    assert not any("1.7.20" in u or "1.7.19" in u for u in requested)
    assert not any(tmp_path.iterdir())


def test_fetch_deb_refuses_a_version_canister_does_not_list(tmp_path: Path) -> None:
    fake_get, _ = _sometweak_world()
    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.sometweak", tmp_path, version="9.9.9")
    assert "9.9.9" in exc.value.message


def test_fetch_deb_does_not_swallow_a_failure_that_is_not_a_404(tmp_path: Path) -> None:
    """Only "this file is not there" earns a try at the next variant. A 500 or a
    bad hash is a reason to stop, not to quietly take a different file."""
    import requests

    def fake_get(url, *args, **kwargs):
        if "/jailbreak/package/" in url:
            return _mock_response({"data": SOMETWEAK_ROWS})
        if "/jailbreak/repository/" in url:
            return _mock_response({"data": dict(SAMPLE_REPO, uri="https://repo.example")})
        resp = _mock_response({}, content_type="application/octet-stream", body=b"x")
        resp.status_code = 503
        resp.raise_for_status.side_effect = requests.HTTPError("503", response=resp)
        return resp

    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(CanisterError) as exc:
            canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24")
    assert "503" in exc.value.message


# --- get_package chooses from the same rows `_candidates` would download -----
#
# `get_package` is what the update check reports as "latest"; `_candidates` is
# what `--fetch` / `--update-tweak` then download. They used to choose from
# different rows: a newest version whose only row has no `package_filename`
# was reported as latest and then could never be downloaded, and a same-
# version same-arch tie across two repos went to whichever canister listed
# first on that request.


def test_a_version_with_no_downloadable_row_is_never_reported_as_latest() -> None:
    rows = [*SOMETWEAK_ROWS, dict(_sometweak("9.9.9", "iphoneos-arm64"), package_filename="")]
    with patch.object(canister, "_get", return_value={"data": rows}):
        package = canister.get_package("com.example.sometweak")
    assert package.version == "1.7.24"
    assert package.package_filename.endswith("com.example.sometweak_1.7.24_iphoneos-arm64.deb")


def test_get_package_and_fetch_agree_on_the_version(tmp_path: Path) -> None:
    """The property the filter exists for, end to end: what the update check
    reports is what a fetch with no version downloads."""
    rows = [*SOMETWEAK_ROWS, dict(_sometweak("9.9.9", "iphoneos-arm64"), package_filename="")]
    fake_http, _ = _sometweak_world()

    def wrapped(url, *a, **k):
        if "/jailbreak/package/" in url and "search" not in url:
            return _mock_response({"data": rows})
        return fake_http(url, *a, **k)

    with patch("requests.get", side_effect=wrapped):
        reported = canister.get_package("com.example.sometweak").version
        dest = canister.fetch_deb("com.example.sometweak", tmp_path)
    assert reported == "1.7.24"
    assert dest.name == "com.example.sometweak_1.7.24_iphoneos-arm64.deb"


@pytest.mark.parametrize("order", [1, -1], ids=["as-listed", "reversed"])
def test_a_tie_across_repos_goes_to_the_same_repo_whatever_the_row_order(order: int) -> None:
    rows = [
        dict(_sometweak("1.7.24", "iphoneos-arm64"), repository_id="zeta"),
        dict(_sometweak("1.7.24", "iphoneos-arm64"), repository_id="alpha"),
    ][::order]
    with patch.object(canister, "_get", return_value={"data": rows}):
        package = canister.get_package("com.example.sometweak")
    assert package.repository_id == "alpha"


def test_a_package_with_no_downloadable_build_is_reported_as_absent() -> None:
    """Every row filename-less: there is nothing to update to, which the update
    check reads as "not in the index" (status 404), not as "canister failed"."""
    rows = [dict(_sometweak("3.0.0", "iphoneos-arm64"), package_filename="")]
    with patch.object(canister, "_get", return_value={"data": rows}):
        with pytest.raises(CanisterError) as exc:
            canister.get_package("com.example.sometweak")
    assert exc.value.message == "canister lists no downloadable build of com.example.sometweak"
    assert getattr(exc.value, "status", None) == 404


def test_the_update_check_reports_nothing_for_a_package_with_no_downloadable_build() -> None:
    from graft_ipa import updates

    rows = [dict(_sometweak("3.0.0", "iphoneos-arm64"), package_filename="")]

    def answer(path, *_a, **_k):
        return {"data": rows} if "/jailbreak/package/" in path else {"data": SAMPLE_REPO}

    with patch.object(canister, "_get", side_effect=answer):
        offers = updates.canister_offers("com.example.sometweak")
    # Its source lists it, with nothing that could ever be downloaded.
    assert [o.version for o in offers] == [None]
    assert updates.resolve(None, None, [], offers).available is None


def test_fetch_deb_says_which_row_and_repository_it_took(tmp_path: Path) -> None:
    """`--fetch` records the tweak's source from this: the build actually
    downloaded, after any fallback, not the first one tried."""
    fake_get, _ = _sometweak_world(gone=("1.7.24_iphoneos-arm64.deb",))
    picked: list = []
    with patch("requests.get", side_effect=fake_get):
        canister.fetch_deb("com.example.sometweak", tmp_path, version="1.7.24",
                           on_pick=lambda pkg, repo: picked.append((pkg.package_filename, repo.uri)))
    assert picked == [("./debs/example/sometweak/com.example.sometweak_1.7.24_iphoneos-arm.deb",
                       "https://repo.example")]


def test_fetch_deb_from_one_repository_never_takes_another_s_higher_version(tmp_path: Path) -> None:
    """The first fetch, from a search result: the repository the person
    picked, whatever version another repository lists (2.3.1 A1)."""
    rows = [dict(_sometweak("1.7.24", "iphoneos-arm64"), repository_id="good"),
            dict(_sometweak("9.9.9", "iphoneos-arm64"), repository_id="evil")]
    picked: list = []

    def answer(path, *_a, **_k):
        if "/jailbreak/package/" in path:
            return {"data": rows}
        repo_id = path.rsplit("/", 1)[-1]
        return {"data": {"id": repo_id, "name": repo_id, "uri": f"https://{repo_id}.example"}}

    with patch.object(canister, "_get", side_effect=answer), \
            patch.object(canister, "_download", side_effect=lambda url, dest, name, sha: dest / name):
        canister.fetch_deb("com.example.sometweak", tmp_path, repository_id="good",
                           on_pick=lambda pkg, repo: picked.append((pkg.version, repo.uri)))
    assert picked == [("1.7.24", "https://good.example")]


def test_a_repository_that_doesnt_list_the_package_is_said_plainly(tmp_path: Path) -> None:
    rows = [dict(_sometweak("1.7.24", "iphoneos-arm64"), repository_id="good")]
    with patch.object(canister, "_get", return_value={"data": rows}):
        with pytest.raises(canister.CanisterError, match="elsewhere"):
            canister.fetch_deb("com.example.sometweak", tmp_path, repository_id="elsewhere")


def _two_repositories(rows_by_repo: dict[str, str]):
    """canister lists com.example.sometweak from each repository, at each
    version given; each repository's URI is https://<id>.example."""
    rows = [dict(_sometweak(v, "iphoneos-arm64"), repository_id=r) for r, v in rows_by_repo.items()]

    def answer(path, *_a, **_k):
        if "/jailbreak/package/" in path:
            return {"data": rows}
        repo_id = path.rsplit("/", 1)[-1]
        return {"data": {"id": repo_id, "name": repo_id, "uri": f"https://{repo_id}.example"}}

    return answer


def test_a_bare_fetch_that_several_repositories_list_is_refused(tmp_path: Path) -> None:
    """Re-verify #4: by package id alone, the newest from any repository
    won, and its source was then recorded as the tweak's. Several listing
    it: name them, and ask for --fetch-repository."""
    downloads: list = []
    with patch.object(canister, "_get", side_effect=_two_repositories({"good": "1.7.24", "evil": "9.9.9"})), \
            patch.object(canister, "_download", side_effect=lambda *a: downloads.append(a)):
        with pytest.raises(canister.SeveralRepositoriesError) as refused:
            canister.fetch_deb("com.example.sometweak", tmp_path)
    assert refused.value.repositories == [("evil", "https://evil.example"), ("good", "https://good.example")]
    assert "--fetch-repository" in (refused.value.hint or "")
    assert downloads == [], "nothing downloaded"


def test_a_bare_fetch_that_one_repository_lists_goes_ahead(tmp_path: Path) -> None:
    picked: list = []
    with patch.object(canister, "_get", side_effect=_two_repositories({"good": "1.7.24"})), \
            patch.object(canister, "_download", side_effect=lambda url, dest, name, sha: dest / name):
        canister.fetch_deb("com.example.sometweak", tmp_path,
                           on_pick=lambda pkg, repo: picked.append(repo.repo_id))
    assert picked == ["good"]
