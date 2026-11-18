"""Staging into the cache when more than one thing stages at once, and what an
IPA's own plist is allowed to put into a filename.

Both became reachable with the App Store work: a drop and a get of the same
app can now run together, and every get stages a file whose name comes from a
plist this project did not write.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from graft_ipa import provide_ipa
from graft_ipa.errors import IPAValidationError
from tests.test_provide_ipa import _make_ipa


def test_two_concurrent_provides_of_one_version_both_land_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They used to share one fixed `.{bundle_id}_{version}.partial.ipa`: the
    second copy truncated the first's temp file under it, and one `os.replace`
    then found nothing to move. The barrier holds both copies until each has
    written its temp file — exactly the interleaving that broke."""
    cache = tmp_path / "cache"
    cache.mkdir()
    a = _make_ipa(tmp_path / "a.ipa", bundle_id="com.x", version="1.0", app_name="A")
    b = _make_ipa(tmp_path / "b.ipa", bundle_id="com.x", version="1.0", app_name="B")
    barrier = threading.Barrier(2, timeout=10)
    real_copy = provide_ipa.shutil.copyfile
    temps: list[str] = []

    def copy_then_wait(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(src, dst, *args, **kwargs)
        temps.append(str(dst))
        barrier.wait()
        return result

    monkeypatch.setattr(provide_ipa.shutil, "copyfile", copy_then_wait)
    errors: list[BaseException] = []

    def run(src: Path) -> None:
        try:
            provide_ipa.provide(cache_dir=cache, expected_bundle_id="com.x", source_path=src)
        except BaseException as e:  # asserted below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(p,)) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert len(set(temps)) == 2, "each run needs its own temp file"
    assert (cache / "com.x_1.0.ipa").read_bytes() in (a.read_bytes(), b.read_bytes())
    assert sorted(p.name for p in cache.iterdir()) == ["com.x_1.0.ipa"]


def test_a_failed_copy_leaves_no_temp_file_whatever_it_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`except OSError` was the only cleanup, so a KeyboardInterrupt — or the
    App Store path's SIGTERM-as-exception — left a partial IPA behind."""
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.x", version="1.0")

    def interrupted(src, dst, *a, **k):  # type: ignore[no-untyped-def]
        Path(dst).write_bytes(b"half")
        raise KeyboardInterrupt

    monkeypatch.setattr(provide_ipa.shutil, "copyfile", interrupted)
    with pytest.raises(KeyboardInterrupt):
        provide_ipa.provide(cache_dir=cache, expected_bundle_id="com.x", source_path=src)

    assert list(cache.iterdir()) == []


@pytest.mark.parametrize("version", ["../../escaped", "1.0/evil", "1.0\\evil", ".."])
def test_a_version_that_would_leave_the_cache_is_refused(tmp_path: Path, version: str) -> None:
    """The canonical name is built from the IPA's own plist, which is not ours:
    a `CFBundleShortVersionString` of `../../x` wrote outside cache_dir.

    NUL is refused by the code too but has no case here: plistlib will not
    write a string containing one, so no fixture can carry it."""
    cache = tmp_path / "deep" / "cache"
    cache.mkdir(parents=True)
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.x", version=version)

    with pytest.raises(IPAValidationError) as e:
        provide_ipa.provide(cache_dir=cache, expected_bundle_id="com.x", source_path=src)

    assert e.value.message.startswith("IPA version")
    assert list(cache.iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "deep").iterdir()) == ["cache"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["deep", "x.ipa"]


@pytest.mark.parametrize("bundle_id", ["com/../x", "..", "com\\x"])
def test_a_bundle_id_that_would_leave_the_cache_is_refused(tmp_path: Path, bundle_id: str) -> None:
    """Belt and braces: the bundle id must equal the configured one, but the
    configured one is only text in a YAML file."""
    src = _make_ipa(tmp_path / "x.ipa", bundle_id=bundle_id, version="1.0")

    with pytest.raises(IPAValidationError) as e:
        provide_ipa.validate_ipa(src, expected_bundle_id=bundle_id)

    assert e.value.message.startswith("IPA bundle id")
