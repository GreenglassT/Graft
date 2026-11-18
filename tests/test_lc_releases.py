"""Finding the upstream LiveContainer release, and caching its IPA honestly.

The version comes from the AltStore source upstream publishes for each channel
(`apps.json` for stable, `apps_nightly.json` for nightly) — the nightly's
GitHub tag is literally `nightly`, so the release API alone cannot say which
version it is. The API is consulted for one thing: the asset's published
`sha256:` digest, which the downloaded file must match before it is cached.
No network here; `get_json` and `download` are fakes.
"""

from __future__ import annotations

import hashlib
import io
import plistlib
import zipfile
from pathlib import Path

import pytest

from graft_ipa import lc_manage
from graft_ipa.errors import ConfigError
from graft_ipa.lc_runner import LiveContainerError

NIGHTLY_IPA_URL = "https://github.com/LiveContainer/LiveContainer/releases/download/nightly/LiveContainer.ipa"
STABLE_IPA_URL = "https://github.com/LiveContainer/LiveContainer/releases/download/3.8.0/LiveContainer.ipa"


def _ipa(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "Payload/LiveContainer.app/Info.plist",
            plistlib.dumps({"CFBundleIdentifier": "com.kdt.livecontainer",
                            "CFBundleShortVersionString": version}),
        )
    return buf.getvalue()


def _source(version: str, url: str) -> dict:
    return {"apps": [{"bundleIdentifier": "com.kdt.livecontainer", "version": version,
                      "downloadURL": url, "versions": [{"version": version}]}]}


def _api(digest: str | None) -> dict:
    asset = {"name": "LiveContainer.ipa", "browser_download_url": NIGHTLY_IPA_URL}
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {"tag_name": "nightly", "assets": [
        {"name": "LiveContainer+SideStore.ipa", "digest": "sha256:" + "0" * 64}, asset,
    ]}


class FakeNet:
    def __init__(self, *, source: dict, api: dict, payload: bytes) -> None:
        self.responses = {
            lc_manage.NIGHTLY_SOURCE_URL: source,
            lc_manage.STABLE_SOURCE_URL: source,
            lc_manage.release_api_url("nightly", "3.8.14"): api,
            lc_manage.release_api_url("stable", "3.8.0"): api,
        }
        self.payload = payload
        self.downloads: list[str] = []

    def get_json(self, url: str):
        return self.responses[url]

    def download(self, url: str, dest: Path) -> None:
        self.downloads.append(url)
        dest.write_bytes(self.payload)


# ---- latest_release ----


def test_the_nightly_version_comes_from_the_nightly_source() -> None:
    net = FakeNet(source=_source("3.8.14", NIGHTLY_IPA_URL), api={}, payload=b"")

    release = lc_manage.latest_release("nightly", get_json=net.get_json)

    assert release == lc_manage.Release("nightly", "3.8.14", NIGHTLY_IPA_URL)


def test_the_stable_version_comes_from_the_stable_source() -> None:
    seen: list[str] = []

    def get_json(url: str):
        seen.append(url)
        return _source("3.8.0", STABLE_IPA_URL)

    release = lc_manage.latest_release("stable", get_json=get_json)

    assert release.version == "3.8.0"
    assert seen == [lc_manage.STABLE_SOURCE_URL]


def test_an_unknown_channel_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        lc_manage.latest_release("beta", get_json=lambda url: {})


def test_a_source_without_livecontainer_is_refused() -> None:
    with pytest.raises(LiveContainerError):
        lc_manage.latest_release("nightly", get_json=lambda url: {"apps": []})


def test_the_stable_digest_is_asked_of_the_versions_own_tag() -> None:
    assert lc_manage.release_api_url("stable", "3.8.0").endswith("/releases/tags/3.8.0")
    assert lc_manage.release_api_url("nightly", "3.8.14").endswith("/releases/tags/nightly")


# ---- ensure_ipa ----


def _release() -> lc_manage.Release:
    return lc_manage.Release("nightly", "3.8.14", NIGHTLY_IPA_URL)


def _ensure(tmp_path: Path, net: FakeNet) -> Path:
    return lc_manage.ensure_ipa(
        _release(), tmp_path, get_json=net.get_json, download=net.download
    )


def test_a_download_matching_the_published_digest_is_cached_by_version(tmp_path: Path) -> None:
    payload = _ipa("3.8.14")
    net = FakeNet(source={}, api=_api(hashlib.sha256(payload).hexdigest()), payload=payload)

    path = _ensure(tmp_path, net)

    assert path == tmp_path / "com.kdt.livecontainer_3.8.14.ipa"
    assert path.read_bytes() == payload
    assert net.downloads == [NIGHTLY_IPA_URL]


def test_a_cached_file_with_the_right_digest_is_reused(tmp_path: Path) -> None:
    payload = _ipa("3.8.14")
    (tmp_path / "com.kdt.livecontainer_3.8.14.ipa").write_bytes(payload)
    net = FakeNet(source={}, api=_api(hashlib.sha256(payload).hexdigest()), payload=b"x")

    path = _ensure(tmp_path, net)

    assert path.read_bytes() == payload
    assert net.downloads == []


def test_a_cached_file_with_the_wrong_digest_is_replaced(tmp_path: Path) -> None:
    """A nightly rebuilt under the same version number is a different file."""
    payload = _ipa("3.8.14")
    stale = tmp_path / "com.kdt.livecontainer_3.8.14.ipa"
    stale.write_bytes(_ipa("3.8.14") + b"stale")
    net = FakeNet(source={}, api=_api(hashlib.sha256(payload).hexdigest()), payload=payload)

    path = _ensure(tmp_path, net)

    assert path.read_bytes() == payload
    assert net.downloads == [NIGHTLY_IPA_URL]


def test_a_download_that_fails_the_digest_is_refused_and_not_cached(tmp_path: Path) -> None:
    net = FakeNet(source={}, api=_api("f" * 64), payload=_ipa("3.8.14"))

    with pytest.raises(LiveContainerError) as exc:
        _ensure(tmp_path, net)

    assert exc.value.message.startswith("downloaded LiveContainer.ipa does not match")
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_download_is_cached_under_the_version_it_really_is(tmp_path: Path) -> None:
    """Nightly moved between the source lookup and the download."""
    payload = _ipa("3.8.15")
    net = FakeNet(source={}, api=_api(hashlib.sha256(payload).hexdigest()), payload=payload)

    path = _ensure(tmp_path, net)

    assert path.name == "com.kdt.livecontainer_3.8.15.ipa"


def _api_down(net: FakeNet):
    """`get_json` that fails for the GitHub API only, as a rate limit does."""

    def get_json(url: str):
        if url.startswith(lc_manage.RELEASES_API):
            raise LiveContainerError(f"could not read {url}: 403 rate limit exceeded")
        return net.get_json(url)

    return get_json


def test_an_api_outage_uses_the_cached_ipa_and_warns(tmp_path: Path) -> None:
    """A rate limit is not a reason to fail an install whose IPA is already here."""
    payload = _ipa("3.8.14")
    cached = tmp_path / "com.kdt.livecontainer_3.8.14.ipa"
    cached.write_bytes(payload)
    net = FakeNet(source={}, api={}, payload=b"never")
    warnings: list[str] = []

    path = lc_manage.ensure_ipa(
        _release(), tmp_path, get_json=_api_down(net), download=net.download,
        warn=warnings.append,
    )

    assert path == cached
    assert net.downloads == []
    assert len(warnings) == 1
    assert warnings[0].startswith("couldn't fetch GitHub's sha256 for LiveContainer.ipa")
    assert "using the cached copy unverified" in warnings[0]


def test_an_api_outage_with_nothing_cached_downloads_unverified_and_warns(tmp_path: Path) -> None:
    payload = _ipa("3.8.14")
    net = FakeNet(source={}, api={}, payload=payload)
    warnings: list[str] = []

    path = lc_manage.ensure_ipa(
        _release(), tmp_path, get_json=_api_down(net), download=net.download,
        warn=warnings.append,
    )

    assert path.read_bytes() == payload
    assert net.downloads == [NIGHTLY_IPA_URL]
    assert "downloading it unverified" in warnings[0]


def test_no_published_digest_is_accepted_and_said(tmp_path: Path) -> None:
    payload = _ipa("3.8.14")
    net = FakeNet(source={}, api=_api(None), payload=payload)
    notes: list[str] = []

    path = lc_manage.ensure_ipa(
        _release(), tmp_path, get_json=net.get_json, download=net.download, note=notes.append
    )

    assert path.read_bytes() == payload
    assert notes == ["GitHub publishes no sha256 digest for this LiveContainer.ipa; not verified"]


# ---- what comes off the network is checked before it names a file ----


@pytest.mark.parametrize("version", ["../../evil", "3.8/14", "", "3.8 14", "3.8.14\n"])
def test_a_source_version_that_cannot_be_a_filename_is_refused(version: str) -> None:
    def get_json(url: str):
        return _source(version, NIGHTLY_IPA_URL)

    with pytest.raises(LiveContainerError):
        lc_manage.latest_release("nightly", get_json=get_json)


def test_a_downloaded_version_that_cannot_be_a_filename_is_refused(tmp_path: Path) -> None:
    payload = _ipa("../../evil")
    net = FakeNet(source={}, api=_api(hashlib.sha256(payload).hexdigest()), payload=payload)

    with pytest.raises(LiveContainerError):
        _ensure(tmp_path, net)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", ["3.8.14", "3.8.14-beta+1", "1.0b2"])
def test_ordinary_versions_are_fine(tmp_path: Path, version: str) -> None:
    assert lc_manage.cached_ipa_path(tmp_path, version).name == f"com.kdt.livecontainer_{version}.ipa"


@pytest.mark.parametrize("answer", [[], "nope", {"apps": "nope"}, {"apps": ["nope"]}, None])
def test_a_source_of_the_wrong_shape_is_a_clean_error(answer) -> None:
    with pytest.raises(LiveContainerError):
        lc_manage.latest_release("nightly", get_json=lambda url: answer)


@pytest.mark.parametrize("answer", [[], "nope", {"assets": "nope"}, {"assets": ["nope"]}])
def test_an_api_answer_of_the_wrong_shape_is_a_clean_error(answer) -> None:
    with pytest.raises(LiveContainerError):
        lc_manage.asset_digest(_release(), get_json=lambda url: answer)
