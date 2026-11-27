"""A tweak's update comes only from the tweak's own source (2.3.1 A1).

The finding: the newest version of a package id was taken across every
configured repo and every repository canister.me indexes, and the SHA-256 only
proved the file matched its own row. Two canister rows for one id (a good 1.0,
an evil 9.9) picked the evil one's URL, and Update re-pointed every app using
the tweak at it, then built, signed and installed it.

Each check here goes through the real `canister_offers` (canister's rows and
repositories stood in for at `canister.get_packages` / `get_repository`) and
the real `repo_offers` (cached indexes on disk), so what's pinned is the
choice of source, not a stand-in for it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from graft_ipa import canister, repo, tweak_sources, updates
from graft_ipa.tweak_sources import Source

from .test_tweak_source_records import write_deb

PID = "com.example.atweak"
GOOD = "https://good.example.com"
MIRROR = "https://mirror.example.com"
EVIL = "https://evil.example.com"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Canister:
    """canister.me's rows and repositories, and every request made of it."""

    def __init__(self, monkeypatch) -> None:
        self.rows: list[dict] = []
        self.uris: dict[str, str] = {}
        self.asked: list[str] = []
        self.repositories_asked: list[str] = []
        self.down = False
        monkeypatch.setattr(canister, "get_packages", self._packages)
        monkeypatch.setattr(canister, "get_repository", self._repository)

    def row(self, repository: str, version: str, sha256: str | None = "f" * 64,
            arch: str = "iphoneos-arm64", package: str = PID) -> None:
        self.rows.append({"package_id": package, "version": version, "repository_id": repository,
                          "package_filename": f"debs/{package}_{version}_{arch}.deb",
                          "sha256_hash": sha256})

    def _packages(self, package_id: str) -> list[canister.Package]:
        self.asked.append(package_id)
        if self.down:
            raise canister.CanisterError("canister request failed: timed out")
        rows = [canister._to_package(r) for r in self.rows if r["package_id"] == package_id]
        if not rows:
            error = canister.CanisterError(f"package not found on canister: {package_id}")
            error.status = 404
            raise error
        return rows

    def _repository(self, repo_id: str) -> canister.Repository:
        self.repositories_asked.append(repo_id)
        return canister.Repository(repo_id=repo_id, name=repo_id, uri=self.uris[repo_id])


@pytest.fixture
def cdn(monkeypatch) -> Canister:
    return Canister(monkeypatch)


def make_cfg(tmp_path: Path, repos: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(state_dir=tmp_path / "state", tweaks_dir=tmp_path / "tweaks",
                           repos=[SimpleNamespace(url=u) for u in repos])


def library(cfg, name: str = "a-tweak.deb", version: str = "1.0", *, salt: str = "") -> dict:
    """One library file, and its `--tweaks` record."""
    write_deb(cfg.tweaks_dir / name, PID, version, salt=salt)
    return {"filename": name, "package_id": PID, "version": version}


def status_of(cfg, record: dict) -> updates.TweakStatus:
    (status,) = updates.statuses(cfg, [record])
    return status


def from_canister(cfg, repo_id: str, uri: str, name: str = "a-tweak.deb") -> None:
    """`name` was fetched from canister repository `repo_id` at `uri`."""
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, name,
                         Source(tweak_sources.CANISTER, repo_id, uri), sha(cfg.tweaks_dir / name))


def cache_index(cfg, url: str, stanzas: str, *, age: float = 0) -> None:
    path = repo.cache_path(cfg.state_dir, repo.normalize_url(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stanzas)
    if age:
        then = path.stat().st_mtime - age
        os.utime(path, (then, then))


def stanza(version: str, sha256: str | None = "e" * 64, package: str = PID) -> str:
    text = f"Package: {package}\nVersion: {version}\nFilename: debs/{package}_{version}.deb\n"
    return text + (f"SHA256: {sha256}\n" if sha256 else "") + "\n"


# --- a recorded source --------------------------------------------------------


def test_a_higher_version_from_another_canister_repository_never_wins(tmp_path: Path, cdn: Canister):
    """The finding, as verified: good 1.0 and evil 9.9 for one id."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.CANISTER, "good", GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cdn.uris = {"good": GOOD, "evil": EVIL}
    cdn.row("good", "1.0")
    cdn.row("good", "1.1")
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)

    assert status.available is not None
    assert status.available.version == "1.1"
    assert status.available.origin == Source(tweak_sources.CANISTER, "good", GOOD)
    assert [d.url for d in status.available.downloads] == [f"{GOOD}/debs/{PID}_1.1_iphoneos-arm64.deb"]
    assert status.available.verified


def test_a_recorded_repo_source_is_the_only_index_read(tmp_path: Path, cdn: Canister):
    """Another configured repo's higher version doesn't win either, and
    canister isn't asked at all: nothing it says could count."""
    cfg = make_cfg(tmp_path, (GOOD, EVIL))
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.REPO, GOOD, GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cache_index(cfg, GOOD, stanza("1.0") + stanza("1.2"))
    cache_index(cfg, EVIL, stanza("9.9"))

    status = status_of(cfg, record)

    assert status.available is not None and status.available.version == "1.2"
    assert status.available.downloads[0].url == f"{GOOD}/debs/{PID}_1.2.deb"
    assert status.available.verified
    assert cdn.asked == []


def test_a_recorded_repo_whose_cache_is_stale_is_unchecked(tmp_path: Path, cdn: Canister):
    """It can't fall through to canister, as it did: that's another source."""
    cfg = make_cfg(tmp_path, (GOOD,))
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.REPO, GOOD, GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cache_index(cfg, GOOD, stanza("1.2"), age=updates.CACHE_MAX_AGE + 60)
    cdn.uris = {"evil": EVIL}
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)

    assert status.unchecked and status.available is None


def test_a_record_for_other_bytes_is_not_used(tmp_path: Path, cdn: Canister):
    """A file swapped by hand under the recorded name isn't the one fetched:
    its source is inferred afresh."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.CANISTER, "good", GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    record = library(cfg, salt="swapped")
    cdn.uris = {"good": GOOD}
    cdn.row("good", "1.1")

    status = status_of(cfg, record)

    assert status.available is not None and not status.available.verified
    assert status.available.unverified_reason == f"source unknown: {GOOD}"


# --- no record: inferred from the file's own hash ----------------------------


def test_a_library_from_before_inferrs_its_source_but_asks_once(tmp_path: Path, cdn: Canister):
    """The source is inferred from the file's own hash, and shown, but never
    verified by that: any index can list any hash, and a public build's
    hash is public (re-verify #1)."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    mine = sha(cfg.tweaks_dir / "a-tweak.deb")
    cdn.uris = {"good": GOOD, "evil": EVIL}
    cdn.row("good", "1.0", sha256=mine)
    cdn.row("good", "1.1")
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)

    assert status.available is not None and status.available.version == "1.1"
    assert status.available.origin.id == "good"
    assert not status.available.verified
    assert status.available.unverified_reason == f"source not confirmed yet: {GOOD}"


def test_an_index_that_lists_the_file_s_hash_proves_nothing(tmp_path: Path, cdn: Canister):
    """The re-verify's probe: no record, and only evil lists the library
    file's hash, beside a 9.9. Shown, and never verified."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    cdn.uris = {"evil": EVIL}
    cdn.row("evil", "1.0", sha256=sha(cfg.tweaks_dir / "a-tweak.deb"))
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)

    assert status.update_available and not status.available.verified
    assert status.available.unverified_reason == f"source not confirmed yet: {EVIL}"


def test_the_same_file_in_two_repositories_needs_a_confirmation(tmp_path: Path, cdn: Canister):
    """Ambiguous: it's shown with the candidate, and not verified."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    mine = sha(cfg.tweaks_dir / "a-tweak.deb")
    cdn.uris = {"good": GOOD, "mirror": MIRROR}
    cdn.row("good", "1.0", sha256=mine)
    cdn.row("mirror", "1.0", sha256=mine)
    cdn.row("good", "1.1")
    cdn.row("mirror", "1.3")

    status = status_of(cfg, record)

    assert status.available is not None and not status.available.verified
    # Chosen by order, never by who offers the higher version.
    assert status.available.origin.id == "good"
    assert status.available.unverified_reason == f"source unknown: {GOOD}"


def test_a_configured_repo_and_its_canister_listing_are_one_source(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path, (GOOD,))
    record = library(cfg)
    mine = sha(cfg.tweaks_dir / "a-tweak.deb")
    cache_index(cfg, GOOD, stanza("1.0", sha256=mine) + stanza("1.1"))
    cdn.uris = {"good": GOOD + "/"}
    cdn.row("good", "1.0", sha256=mine)
    cdn.row("good", "1.1")

    status = status_of(cfg, record)

    assert status.available is not None and not status.available.verified
    assert status.available.unverified_reason.startswith("source not confirmed yet: "), \
        "one source, though not confirmed: not \"unknown\""


def test_a_file_no_index_lists_is_from_an_unknown_source(tmp_path: Path, cdn: Canister):
    """Built locally, or the index dropped that build: whatever is offered
    needs the person's OK."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    cdn.uris = {"evil": EVIL}
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)

    assert status.update_available
    assert not status.available.verified
    assert status.available.unverified_reason == f"source unknown: {EVIL}"


def test_without_canister_a_record_less_file_is_unchecked(tmp_path: Path, cdn: Canister):
    """The candidates can't all be known, so neither can the source."""
    cfg = make_cfg(tmp_path, (GOOD,))
    record = library(cfg)
    cache_index(cfg, GOOD, stanza("1.0", sha256=sha(cfg.tweaks_dir / "a-tweak.deb")) + stanza("1.1"))
    cdn.down = True

    status = status_of(cfg, record)

    assert status.unchecked and status.available is None


def test_a_recorded_repo_source_answers_with_canister_down(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path, (GOOD,))
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.REPO, GOOD, GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cache_index(cfg, GOOD, stanza("1.1"))
    cdn.down = True

    status = status_of(cfg, record)

    assert not status.unchecked and status.update_available


def test_a_recorded_canister_repository_that_moved_is_unverified(tmp_path: Path, cdn: Canister):
    """Matched by id, its URI is compared too: a re-registered domain, or a
    canister that says the repository is somewhere else now, isn't the
    source that was recorded (re-verify #3)."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.CANISTER, "good", GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cdn.uris = {"good": EVIL}
    cdn.row("good", "1.1")

    status = status_of(cfg, record)

    assert status.update_available and not status.available.verified
    assert status.available.unverified_reason == f"its source moved from {GOOD} to {EVIL}"


def test_the_same_uri_written_another_way_has_not_moved(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.CANISTER, "good", GOOD), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cdn.uris = {"good": GOOD.upper().replace("HTTPS", "https") + "/"}
    cdn.row("good", "1.1")

    assert status_of(cfg, record).available.verified


# --- verified, or not ----------------------------------------------------------


def test_an_http_source_is_unverified(tmp_path: Path, cdn: Canister):
    plain = "http://plain.example.com"
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    from_canister(cfg, "plain", plain)
    cdn.uris = {"plain": plain}
    cdn.row("plain", "1.1")

    status = status_of(cfg, record)

    assert status.update_available and not status.available.verified
    assert status.available.unverified_reason == f"{plain} doesn't use HTTPS"


def test_an_http_repo_is_unverified(tmp_path: Path, cdn: Canister):
    plain = "http://plain.example.com"
    cfg = make_cfg(tmp_path, (plain,))
    record = library(cfg)
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, "a-tweak.deb",
                         Source(tweak_sources.REPO, plain, plain), sha(cfg.tweaks_dir / "a-tweak.deb"))
    cache_index(cfg, plain, stanza("1.1"))

    status = status_of(cfg, record)

    assert status.update_available and not status.available.verified
    assert "HTTPS" in status.available.unverified_reason


def test_a_version_with_no_published_hash_is_unverified(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    from_canister(cfg, "good", GOOD)
    cdn.uris = {"good": GOOD}
    cdn.row("good", "1.1", sha256=None)

    status = status_of(cfg, record)

    assert status.update_available and not status.available.verified
    assert status.available.unverified_reason == f"{GOOD} lists no checksum for 1.1"


def test_builds_without_a_hash_are_left_out_when_one_has_it(tmp_path: Path, cdn: Canister):
    """A verified update downloads only builds it can check."""
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    from_canister(cfg, "good", GOOD)
    cdn.uris = {"good": GOOD}
    cdn.row("good", "1.1", sha256=None, arch="iphoneos-arm64")
    cdn.row("good", "1.1", sha256="a" * 64, arch="iphoneos-arm")

    status = status_of(cfg, record)

    assert status.available.verified
    assert [d.sha256 for d in status.available.downloads] == ["a" * 64]


# --- the lookups ----------------------------------------------------------------


def test_canister_is_asked_once_per_package_and_repository(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path)
    first = library(cfg, "a.deb")
    second = library(cfg, "b.deb", "1.0-jailed")
    cdn.uris = {"good": GOOD}
    cdn.row("good", "1.0", sha256=sha(cfg.tweaks_dir / "a.deb"))
    cdn.row("good", "1.1")

    results = updates.statuses(cfg, [first, second])

    assert cdn.asked == [PID]
    assert cdn.repositories_asked == ["good"]
    assert [r.filename for r in results] == ["a.deb", "b.deb"]


def test_a_package_canister_doesn_t_list_is_checked_not_unchecked(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path)
    status = status_of(cfg, library(cfg))
    assert not status.unchecked and status.available is None


def test_the_event_names_the_source_and_whether_it_s_verified(tmp_path: Path, cdn: Canister):
    cfg = make_cfg(tmp_path)
    record = library(cfg)
    cdn.uris = {"evil": EVIL}
    cdn.row("evil", "9.9")

    status = status_of(cfg, record)
    event = updates.as_event(status)

    assert event == {
        "filename": "a-tweak.deb", "package_id": PID, "installed": "1.0", "latest": "9.9",
        "source": EVIL, "update_available": True, "unchecked": False,
        "verified": False, "unverified_reason": f"source unknown: {EVIL}",
        # What `--trust-tweak` takes to confirm exactly this update.
        "confirm_key": updates.confirm_key("a-tweak.deb", status.available),
    }
