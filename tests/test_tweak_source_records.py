"""Where each library tweak came from, and how a download gets into the library.

Two security findings meet here (2.3.1 A1, A2). An update must come from the
tweak's own source, so the source is recorded when the tweak is fetched; and
the repo must not pick the library filename, or a download named like another
tweak overwrites it, and every app using that one builds with it.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from graft_ipa import deb, tweak_sources
from graft_ipa.tweak_sources import Source, TweakFileError

from .test_deb_control import _ar, _deb

CANISTER = Source(tweak_sources.CANISTER, "examplerepo", "https://repo.example.com")
REPO = Source(tweak_sources.REPO, "https://apt.example.org", "https://apt.example.org")


def control(package: str = "com.example.atweak", version: str = "1.1", arch: str | None = None) -> str:
    text = f"Package: {package}\nVersion: {version}\n"
    return text + (f"Architecture: {arch}\n" if arch else "")


def write_deb(path: Path, package: str = "com.example.atweak", version: str = "1.1",
              arch: str | None = None, *, salt: str = "") -> Path:
    """A .deb whose control names `package` at `version`; `salt` changes its
    bytes and nothing else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_deb(text=control(package, version, arch) + (f"X-Salt: {salt}\n" if salt else "")))
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- deb.identity ------------------------------------------------------------


def test_identity_reads_the_package_version_and_architecture(tmp_path: Path):
    path = write_deb(tmp_path / "x.deb", "com.example.atweak", "1.2", "iphoneos-arm64")
    assert deb.identity(path) == ("com.example.atweak", "1.2", "iphoneos-arm64")


def test_identity_without_an_architecture(tmp_path: Path):
    path = write_deb(tmp_path / "x.deb", "com.example.atweak", "1.2")
    assert deb.identity(path) == ("com.example.atweak", "1.2", None)


@pytest.mark.parametrize("blob", [
    b"<html>not found</html>",
    b"PK\x03\x04 a zip",
    b"",
])
def test_identity_of_something_that_isnt_a_deb_is_none(tmp_path: Path, blob: bytes):
    path = tmp_path / "x.deb"
    path.write_bytes(blob)
    assert deb.identity(path) is None


def test_identity_needs_a_version(tmp_path: Path):
    path = tmp_path / "x.deb"
    path.write_bytes(_deb(text="Package: com.example.atweak\n"))
    assert deb.identity(path) is None


def test_a_zstd_control_member_is_read_on_this_python(tmp_path: Path):
    """dpkg-deb can compress the control member with zstd, which `tarfile`
    reads only from Python 3.14. With `backports.zstd` (bundled) a valid tweak
    mustn't be refused as "not a .deb" for its compression."""
    zstd = deb._zstd()
    if zstd is None:
        pytest.skip("no zstd module in this Python")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = control("com.example.ztweak", "3.0").encode()
        info = tarfile.TarInfo("./control")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    path = tmp_path / "z.deb"
    path.write_bytes(_ar([("debian-binary", b"2.0\n"), ("control.tar.zst", zstd.compress(buf.getvalue()))]))
    assert deb.identity(path) == ("com.example.ztweak", "3.0", None)
    assert deb.control_fields(path)["version"] == "3.0"


# --- the library name ----------------------------------------------------------


def test_the_library_name_is_graft_s_own():
    assert tweak_sources.library_name("com.example.atweak", "1.2.3", "iphoneos-arm64") == \
        "com.example.atweak_1.2.3_iphoneos-arm64.deb"
    assert tweak_sources.library_name("com.example.atweak", "1.2.3", None) == "com.example.atweak_1.2.3.deb"


@pytest.mark.parametrize("package,version,arch", [
    ("../../evil", "1.0", None),
    ("com.example/x", "1.0", None),
    ("com.example.x", "1:2.0", None),
    ("tweak[bold]", "1.0", "iphoneos arm64"),
    (".hidden", "1.0", None),
])
def test_the_library_name_keeps_to_safe_characters(package: str, version: str, arch: str | None):
    name = tweak_sources.library_name(package, version, arch)
    assert "/" not in name and ":" not in name and "[" not in name and " " not in name
    assert not name.startswith(".")
    assert name.endswith(".deb")


# --- the record ------------------------------------------------------------------


def test_a_recorded_source_is_read_back_for_the_same_file(tmp_path: Path):
    state, library = tmp_path / "state", tmp_path / "tweaks"
    path = write_deb(library / "com.example.atweak_1.1.deb")
    tweak_sources.record(state, library, path.name, CANISTER, sha(path))
    assert tweak_sources.recorded_for(state, path.name, sha(path)) == CANISTER


def test_a_record_does_not_vouch_for_a_different_file_of_the_same_name(tmp_path: Path):
    """A file replaced by hand under the same name isn't the one fetched."""
    state, library = tmp_path / "state", tmp_path / "tweaks"
    path = write_deb(library / "a.deb")
    tweak_sources.record(state, library, path.name, CANISTER, sha(path))
    write_deb(path, salt="other")
    assert tweak_sources.recorded_for(state, path.name, sha(path)) is None


def test_records_for_files_gone_from_the_library_are_dropped(tmp_path: Path):
    state, library = tmp_path / "state", tmp_path / "tweaks"
    a = write_deb(library / "a.deb")
    tweak_sources.record(state, library, a.name, CANISTER, sha(a))
    a.unlink()
    b = write_deb(library / "b.deb", "com.example.btweak")
    tweak_sources.record(state, library, b.name, REPO, sha(b))
    raw = json.loads((state / tweak_sources.FILENAME).read_text())
    assert set(raw["tweaks"]) == {"b.deb"}


@pytest.mark.parametrize("text", ["not json", "[]", '{"format": 99, "tweaks": {}}',
                                  '{"format": 1, "tweaks": {"a.deb": {"kind": "ftp"}}}'])
def test_a_foreign_or_broken_record_file_records_nothing(tmp_path: Path, text: str):
    state = tmp_path / "state"
    state.mkdir()
    (state / tweak_sources.FILENAME).write_text(text)
    assert tweak_sources.recorded_for(state, "a.deb", "0" * 64) is None


def test_the_record_file_is_owner_only(tmp_path: Path):
    state, library = tmp_path / "state", tmp_path / "tweaks"
    path = write_deb(library / "a.deb")
    tweak_sources.record(state, library, path.name, CANISTER, sha(path))
    assert (state / tweak_sources.FILENAME).stat().st_mode & 0o077 == 0


def test_a_source_s_label_and_whether_it_is_secure():
    assert CANISTER.label == "https://repo.example.com" and CANISTER.secure
    plain = Source(tweak_sources.REPO, "http://apt.example.org", "http://apt.example.org")
    assert not plain.secure
    unknown_uri = Source(tweak_sources.CANISTER, "examplerepo", "")
    assert unknown_uri.label == "canister repository examplerepo" and not unknown_uri.secure


# --- placing a download in the library ----------------------------------------


def test_a_download_gets_graft_s_name_in_the_library(tmp_path: Path):
    library = tmp_path / "tweaks"
    work = write_deb(tmp_path / "work" / "b-tweak.deb", "com.example.atweak", "1.1", "iphoneos-arm64")
    dest = tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1")
    assert dest == library / "com.example.atweak_1.1_iphoneos-arm64.deb"
    assert dest.is_file() and not work.exists()


def test_a_download_that_isnt_a_deb_never_reaches_the_library(tmp_path: Path):
    library = tmp_path / "tweaks"
    library.mkdir()
    work = tmp_path / "work" / "a.deb"
    work.parent.mkdir()
    work.write_bytes(b"<html>gotcha</html>")
    with pytest.raises(TweakFileError, match=r"isn't a \.deb"):
        tweak_sources.place(work, library)
    assert list(library.iterdir()) == []


def test_a_download_of_another_package_is_refused(tmp_path: Path):
    library = tmp_path / "tweaks"
    work = write_deb(tmp_path / "work" / "x.deb", "com.example.btweak", "1.1")
    with pytest.raises(TweakFileError, match=r"com\.example\.btweak"):
        tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1")
    assert not library.exists() or list(library.iterdir()) == []


def test_a_download_of_another_version_is_refused(tmp_path: Path):
    library = tmp_path / "tweaks"
    work = write_deb(tmp_path / "work" / "x.deb", "com.example.atweak", "9.9")
    with pytest.raises(TweakFileError, match=r"9\.9"):
        tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1")


def test_the_same_file_already_in_the_library_is_reused(tmp_path: Path):
    library = tmp_path / "tweaks"
    existing = write_deb(library / "com.example.atweak_1.1.deb")
    work = tmp_path / "work" / "x.deb"
    work.parent.mkdir()
    work.write_bytes(existing.read_bytes())
    assert tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1") == existing


def test_a_different_file_never_overwrites_a_library_tweak(tmp_path: Path):
    """The finding: a download named like another tweak replaced it, and the
    apps using that one built with it."""
    library = tmp_path / "tweaks"
    theirs = write_deb(library / "com.example.btweak_2.0.deb", "com.example.btweak", "2.0")
    before = theirs.read_bytes()
    work = write_deb(tmp_path / "work" / "x.deb", "com.example.btweak", "2.0", salt="evil")
    with pytest.raises(TweakFileError, match="already in your tweaks library"):
        tweak_sources.place(work, library)
    assert theirs.read_bytes() == before


# --- the record is written under a lock (re-verify, also #1) -------------------


def test_a_record_waits_for_another_writer_and_keeps_its_entry(tmp_path: Path):
    """A fetch and an update writing at once each read, changed and wrote
    the whole file, so the second write dropped the first's record. Now each
    holds `tweak-sources.json.lock` across its read and write."""
    import fcntl
    import os
    import threading
    import time

    state, library = tmp_path / "state", tmp_path / "tweaks"
    a = write_deb(library / "a.deb")
    b = write_deb(library / "b.deb", "com.example.btweak")
    tweak_sources.record(state, library, a.name, CANISTER, sha(a))
    lock = os.open(state / (tweak_sources.FILENAME + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        writer = threading.Thread(target=tweak_sources.record, args=(state, library, b.name, REPO, sha(b)))
        writer.start()
        time.sleep(0.3)
        raw = json.loads((state / tweak_sources.FILENAME).read_text())
        assert set(raw["tweaks"]) == {"a.deb"}, "it wrote while another writer held the lock"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    writer.join(5)
    raw = json.loads((state / tweak_sources.FILENAME).read_text())
    assert set(raw["tweaks"]) == {"a.deb", "b.deb"}


# --- placing a download where hard links don't work (re-verify, also #2) --------


@pytest.mark.parametrize("err", [18, 1, 45])  # EXDEV, EPERM, ENOTSUP
def test_without_hard_links_a_download_still_gets_its_name(tmp_path: Path, monkeypatch, err: int):
    """exFAT, some SMB shares, or a `.update-tmp` on another volume: the
    link fails, and the download is copied into place instead."""
    import os

    def no_link(*_a, **_k):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(tweak_sources.os, "link", no_link)
    library = tmp_path / "tweaks"
    work = write_deb(tmp_path / "work" / "x.deb", "com.example.atweak", "1.1")
    body = work.read_bytes()

    dest = tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1")

    assert dest == library / "com.example.atweak_1.1.deb" and dest.read_bytes() == body
    assert not work.exists()
    assert sorted(p.name for p in library.iterdir()) == [dest.name], "nothing left behind"


def test_without_hard_links_a_different_file_is_still_never_replaced(tmp_path: Path, monkeypatch):
    import errno
    import os

    library = tmp_path / "tweaks"
    theirs = write_deb(library / "com.example.atweak_1.1.deb", "com.example.atweak", "1.1", salt="theirs")
    before = theirs.read_bytes()
    work = write_deb(tmp_path / "work" / "x.deb", "com.example.atweak", "1.1")
    real_checked = tweak_sources.checked_name

    def name_then_race(*a, **k):
        # The name is free when it's checked; another writer takes it before
        # the copy lands.
        theirs.unlink()
        name = real_checked(*a, **k)
        theirs.write_bytes(before)
        return name

    def no_link(*_a, **_k):
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    monkeypatch.setattr(tweak_sources.os, "link", no_link)
    monkeypatch.setattr(tweak_sources, "checked_name", name_then_race)

    with pytest.raises(TweakFileError, match="already in your tweaks library"):
        tweak_sources.place(work, library, package_id="com.example.atweak", version="1.1")

    assert theirs.read_bytes() == before
    assert sorted(p.name for p in library.iterdir()) == [theirs.name], "nothing left behind"


def test_without_hard_links_the_same_file_is_reused(tmp_path: Path, monkeypatch):
    import errno
    import os

    monkeypatch.setattr(tweak_sources.os, "link",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    library = tmp_path / "tweaks"
    there = write_deb(library / "com.example.atweak_1.1.deb", "com.example.atweak", "1.1")
    work = tmp_path / "work" / "x.deb"
    work.parent.mkdir()
    work.write_bytes(there.read_bytes())

    assert tweak_sources.place(work, library) == there
    assert sorted(p.name for p in library.iterdir()) == [there.name]
