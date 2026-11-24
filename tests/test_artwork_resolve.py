"""The cascade: `artwork.resolve` picking a rung, and the cache it writes.

Every test here patches all three rungs, not just the ones it is interested in
— and specifically not only the ones it expects `resolve` to *reach*.
`icon_from_app_store` talks to the real iTunes API and `icon_from_device` to a
real phone, so an unpatched rung is not a slow test; it is a test whose result
depends on the network and on what is plugged in. Relying on an argument
(`udid=None`, say) to keep a rung unreached is not enough, because the mutation
that breaks that argument is exactly the mutation these tests exist to catch —
and it would run real USB I/O inside a rung whose broad catch then hides it.
Patching a rung that should never be reached and asserting `assert_not_called`
is the same guard read from the other side.

`_rungs` is what makes that rule hold rather than merely be written down here:
it patches all three every time, so "patch every rung" is no longer something
each new test's author has to remember.

`temp_config_env` is likewise not optional: `artwork_dir()` is derived from
`XDG_CACHE_HOME`, so without it these tests write into the developer's real
`~/.cache/graft/artwork/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from graft_ipa import artwork

#: "This rung was not given an answer", as distinct from "this rung answers
#: None". Both are legitimate: a rung that must not be reached is patched with
#: no configured return value at all, because configuring one would imply the
#: value mattered.
_UNSET = object()


def _mock_kwargs(answer: object) -> dict[str, object]:
    """`answer` as the `patch.object` keyword that expresses it.

    A callable becomes a `side_effect` — the shape a rung needs when being
    called has to *do* something (replacing the IPA mid-read, say) rather than
    just hand back bytes. Everything else, `None` included, is a `return_value`.
    """
    if answer is _UNSET:
        return {}
    if callable(answer):
        return {"side_effect": answer}
    return {"return_value": answer}


@contextmanager
def _rungs(
    *, ipa: object = _UNSET, app_store: object = _UNSET, device: object = _UNSET
) -> Iterator[tuple[MagicMock, MagicMock, MagicMock]]:
    """Patch all three rungs, and yield their mocks in cascade order.

    Every keyword is optional, and omitting one is the point rather than a
    shorthand: the rung is still patched — see the module docstring for why
    that is not negotiable — just with no answer configured, which is exactly
    what a test that goes on to `assert_not_called` wants.
    """
    with (
        patch.object(artwork, "icon_from_ipa", **_mock_kwargs(ipa)) as from_ipa,
        patch.object(artwork, "icon_from_app_store", **_mock_kwargs(app_store)) as from_store,
        patch.object(artwork, "icon_from_device", **_mock_kwargs(device)) as from_device,
    ):
        yield from_ipa, from_store, from_device


def _png(tag: bytes) -> bytes:
    """Distinguishable bytes that still pass `resolve`'s magic-byte sniff.

    The signature is eight bytes, `\x89PNG\r\n\x1a\n`, not four. A fixture
    written as `_png(b"-two")` reads as a PNG to a human and is rejected by
    the cascade — which is how this helper came to exist, and is the sniff
    working rather than a nuisance.
    """
    return b"\x89PNG\r\n\x1a\n" + tag


#: The default stand-in icon. Content is never decoded, only compared.
_PNG = _png(b"-fake")


def _an_ipa(tmp_path: Path, contents: bytes = b"version one") -> Path:
    """A stand-in for a cached IPA. Only its size, mtime and bytes matter —
    `resolve` stats it to key the cache and hashes it on the write path, and
    never opens it as a zip; the rung that would is patched out in every test
    here."""
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(contents)
    return ipa


def test_the_ipa_wins_when_it_has_an_icon(temp_config_env: Path, tmp_path: Path) -> None:
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG) as (from_ipa, from_store, from_device):
        result = artwork.resolve("com.example.App", ipa=ipa)
    assert result is not None
    assert result.source == artwork.SOURCE_IPA
    assert result.cached is False
    assert result.path.read_bytes() == _PNG
    from_ipa.assert_called_once_with(ipa)
    from_store.assert_not_called()
    from_device.assert_not_called()
    # And the cache really was redirected — without `temp_config_env` in
    # effect every assertion above would pass just as happily against the
    # developer's own artwork cache.
    assert tmp_path in result.path.parents


def test_falls_through_to_the_app_store(temp_config_env: Path, tmp_path: Path) -> None:
    """The Example path: an IPA with no flat icon, artwork from the store."""
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=None, app_store=_PNG) as (from_ipa, from_store, from_device):
        result = artwork.resolve("com.example.app", ipa=ipa, udid="UDID")
    assert result is not None
    assert result.source == artwork.SOURCE_APP_STORE
    from_ipa.assert_called_once()
    from_store.assert_called_once_with("com.example.app", store_country=None)
    # The cascade stops at the first hit; the phone is never touched.
    from_device.assert_not_called()


def test_falls_through_to_the_device(temp_config_env: Path) -> None:
    with _rungs(ipa=None, app_store=None, device=_PNG) as (_, _, from_device):
        result = artwork.resolve("com.example.App", ipa=None, udid="UDID")
    assert result is not None
    assert result.source == artwork.SOURCE_DEVICE
    assert result.path.read_bytes() == _PNG
    from_device.assert_called_once_with("com.example.App", udid="UDID")


def test_the_device_is_skipped_when_no_phone_was_named(temp_config_env: Path) -> None:
    """No udid means no device to ask — not "ask the default device".

    Reaching the rung anyway would cost a USB round trip per app on every
    machine with a phone plugged in, and would answer for whichever phone
    happened to be attached rather than the one the caller meant.
    """
    with _rungs(ipa=None, app_store=None) as (_, _, from_device):
        assert artwork.resolve("com.example.App", ipa=None, udid=None) is None
    from_device.assert_not_called()


def test_the_ipa_rung_is_skipped_when_no_ipa_was_named(temp_config_env: Path) -> None:
    """Nothing downloaded yet is the ordinary state of a freshly added app, and
    the rung takes a `Path` — it cannot be handed None to miss on."""
    with _rungs(app_store=_PNG, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=None)
    from_ipa.assert_not_called()
    assert result is not None
    assert result.source == artwork.SOURCE_APP_STORE


def test_a_total_miss_returns_none_and_writes_nothing(temp_config_env: Path) -> None:
    """A miss is an ordinary outcome — the GUI draws its letter tile — and it
    must leave no trace. A sidecar recording the miss would have to be
    understood by every later caller and would buy nothing over asking again."""
    # The device rung is patched even though `udid=None` should already keep it
    # unreached — under the mutation that removes that guard, an unpatched rung
    # here would do real USB I/O and its broad catch would swallow the evidence.
    # `_rungs` patches it whether or not this test names it.
    with _rungs(ipa=None, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=None) is None
    assert not artwork.artwork_path("com.example.App").exists()
    assert not artwork.sidecar_path("com.example.App").exists()


def test_a_cached_icon_is_served_without_re_resolving(
    temp_config_env: Path, tmp_path: Path
) -> None:
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        first = artwork.resolve("com.example.App", ipa=ipa)
    assert first is not None
    assert first.cached is False

    with _rungs(device=None) as (from_ipa, from_store, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    assert result is not None
    assert result.cached is True
    # The recorded source survives the round trip: a person reading `--artwork`
    # output needs to know which rung the cached file came from, and the answer
    # cannot be re-derived once the file is on disk.
    assert result.source == artwork.SOURCE_IPA
    assert result.path.read_bytes() == _PNG


def test_refresh_re_resolves_even_when_cached(temp_config_env: Path, tmp_path: Path) -> None:
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        artwork.resolve("com.example.App", ipa=ipa)

    with _rungs(ipa=_png(b"-new"), app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa, refresh=True)
    from_ipa.assert_called_once()
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _png(b"-new")


def test_a_changed_ipa_invalidates_the_cache(temp_config_env: Path, tmp_path: Path) -> None:
    """A new IPA version may have gained a flat icon the old one lacked, and a
    tweak may have replaced the icon outright. The sidecar records which IPA
    the answer was made against, so this is detectable without a re-download.

    The replacement is a different length on purpose. `(size, mtime_ns)` is the
    recorded identity, and a same-length rewrite would leave this test hanging
    on whether two writes microseconds apart land on distinct nanosecond
    timestamps — a real property, pinned deterministically by
    `test_a_same_size_ipa_replacement_is_still_noticed` rather than by luck.
    """
    ipa = _an_ipa(tmp_path, b"version one")
    with _rungs(ipa=_PNG, app_store=None, device=None):
        artwork.resolve("com.example.App", ipa=ipa)

    ipa.write_bytes(b"version two, and rather longer than the first")
    with _rungs(ipa=_png(b"-two"), app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _png(b"-two")


def test_a_missing_png_forces_re_resolution_even_with_a_sidecar(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Someone cleared the cache directory but the sidecar survived, or the
    other way round. The PNG is the artifact; the sidecar only describes it.

    The sidecar here is the one `resolve` itself wrote, and the intervening
    cache hit proves it — so when the second resolve re-runs the rung, the
    deleted PNG is demonstrably the reason, not a digest or id that never
    matched in the first place.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        first = artwork.resolve("com.example.App", ipa=ipa)
    assert first is not None

    with _rungs(device=None) as (from_ipa, from_store, _):
        assert artwork.resolve("com.example.App", ipa=ipa) is not None
    assert from_ipa.call_count == 0, "premise: this state is a cache hit while the PNG exists"
    from_store.assert_not_called()

    artwork.artwork_path("com.example.App").unlink()
    assert artwork.sidecar_path("com.example.App").exists()
    with _rungs(ipa=_PNG, app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _PNG


def test_a_slug_collision_re_resolves_rather_than_serving_the_wrong_icon(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Two bundle ids that slug to one filename must not share an icon.

    Not reachable with an id Apple would accept — `_slug` is the identity
    function for every legitimate bundle id — so this guards the *recovery*,
    not a live bug: whichever app asks second re-resolves instead of being
    handed the first one's artwork.

    Both resolves are passed the same IPA on purpose. That makes the recorded
    digest identical, so the id recorded in the sidecar is the only thing left
    that can tell the two apps apart.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        first = artwork.resolve("com.foo_bar", ipa=ipa)
    assert first is not None

    with _rungs(ipa=_png(b"-other"), app_store=None, device=None) as (from_ipa, _, _):
        second = artwork.resolve("com.foo/bar", ipa=ipa)
    assert from_ipa.call_count == 1, "the collision should have forced a re-resolve"
    assert second is not None
    assert second.path == first.path, "same slug, so the same file — that is the premise"
    assert second.path.read_bytes() == _png(b"-other")


def test_the_png_and_the_sidecar_describe_the_same_resolution(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Whatever the winning rung returned is what lands on disk, and the
    sidecar names that rung and that IPA.

    Nothing else pins the two writes to each other: `resolve` could plausibly
    write the bytes of one rung under the `source` of another, or stamp a
    digest it never compared against, and every cache-hit test above would
    still pass — they only ever ask whether a rung was called again.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=None, app_store=_png(b"-from-the-store"), device=None):
        result = artwork.resolve("com.example.App", ipa=ipa)

    assert result is not None
    assert result.source == artwork.SOURCE_APP_STORE
    assert result.path == artwork.artwork_path("com.example.App")
    assert result.path.read_bytes() == _png(b"-from-the-store")

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    assert sidecar["source"] == artwork.SOURCE_APP_STORE
    assert sidecar["bundle_id"] == "com.example.App"
    # Hashed here rather than via `artwork._sha256`, so this asserts against
    # the digest of the file and not against the implementation's opinion of
    # it. Provenance only — the cache-hit path compares the two fields below.
    assert sidecar["ipa_sha256"] == hashlib.sha256(ipa.read_bytes()).hexdigest()
    stat = ipa.stat()
    assert sidecar["ipa_size"] == stat.st_size
    assert sidecar["ipa_mtime_ns"] == stat.st_mtime_ns


def test_the_png_is_on_disk_before_the_sidecar_vouches_for_it(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Order, not just outcome: the image must be complete before anything
    claims it is.

    The reverse order looks identical once both writes finish, and differs
    only when the process dies between them — but that difference is the
    unrecoverable one. A sidecar with no PNG behind it costs one re-resolve
    (the PNG is checked for existence first); a sidecar vouching for a
    half-written PNG is served from the cache forever and renders as nothing.
    """
    ipa = _an_ipa(tmp_path)
    real_write_sidecar = artwork.write_sidecar
    seen: dict[str, object] = {}

    def spy(bundle_id: str, **kwargs: object) -> None:
        png = artwork.artwork_path(bundle_id)
        seen["existed"] = png.exists()
        seen["bytes"] = png.read_bytes() if png.exists() else None
        real_write_sidecar(bundle_id, **kwargs)  # type: ignore[arg-type]

    with (
        _rungs(ipa=_PNG, app_store=None, device=None),
        patch.object(artwork, "write_sidecar", spy),
    ):
        result = artwork.resolve("com.example.App", ipa=ipa)

    assert result is not None
    assert seen["existed"] is True, "the sidecar was written before the PNG existed"
    # Whole, not merely present: `_atomic_write` renames a finished temp file
    # into place, so the PNG is never observable half-written.
    assert seen["bytes"] == _PNG


def test_a_rung_returning_empty_bytes_is_a_miss(temp_config_env: Path, tmp_path: Path) -> None:
    """`b""` is nothing found, not an icon that happens to be zero bytes.

    Caching it would create a file that exists — and so satisfies the cache-hit
    branch forever — while decoding to nothing at all, which is the one failure
    mode only `--refresh-artwork` can undo.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=b"", app_store=_PNG, device=None) as (from_ipa, from_store, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    from_store.assert_called_once()
    assert result is not None
    assert result.source == artwork.SOURCE_APP_STORE
    assert result.path.read_bytes() == _PNG

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    assert sidecar["source"] == artwork.SOURCE_APP_STORE


def test_a_rung_returning_non_image_bytes_is_a_miss(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The hole this closes is rung one specifically.

    Rungs two and three sniff their own bytes. Rung one returns whatever a zip
    entry held and deliberately picks the *largest* match, out of an archive
    this project did not write — so an IPA declaring
    `CFBundleIconFiles: ["AppIcon60x60"]` and shipping an `AppIcon60x60@2x.png`
    full of HTML resolved clean, wrote the HTML to the cache under
    `source: "ipa"`, and served it as an icon forever.
    """
    ipa = _an_ipa(tmp_path)
    html = b"<!DOCTYPE html>\n<html><body>Access Denied</body></html>"
    with _rungs(ipa=html, app_store=_PNG, device=None) as (from_ipa, from_store, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    from_store.assert_called_once()
    assert result is not None
    assert result.source == artwork.SOURCE_APP_STORE
    assert result.path.read_bytes() == _PNG


def test_every_rung_returning_non_image_bytes_writes_nothing(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Rejected bytes are a miss, and a miss leaves nothing behind."""
    ipa = _an_ipa(tmp_path)
    html = b"<!DOCTYPE html>Access Denied"
    with _rungs(ipa=html, app_store=html, device=html):
        assert artwork.resolve("com.example.App", ipa=ipa, udid="UDID") is None
    assert not artwork.artwork_path("com.example.App").exists()
    assert not artwork.sidecar_path("com.example.App").exists()


def test_a_cached_app_store_icon_still_reports_the_app_store(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The cache hit reads `source` back off the sidecar rather than assuming.

    Deliberately not the IPA rung: `SOURCE_IPA` was the fallback the cache-hit
    branch used for a sidecar with no readable source, so a test resolved from
    the IPA rung could not tell "read from disk" from "hardcoded" — hardcoding
    `source=SOURCE_IPA` there passed the entire suite before this existed.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=None, app_store=_PNG, device=None):
        first = artwork.resolve("com.example.App", ipa=ipa)
    assert first is not None
    assert first.source == artwork.SOURCE_APP_STORE

    with _rungs() as (from_ipa, from_store, from_device):
        second = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    assert second is not None
    assert second.cached is True
    assert second.source == artwork.SOURCE_APP_STORE


def test_a_sidecar_with_no_readable_source_reports_unknown(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Never `ipa`. That is the highest-trust label in the vocabulary, and
    someone reading `--artwork` output to work out why an icon looks wrong
    would take it to mean the bytes came out of the app's own bundle —
    fabricated provenance misleads where "unknown" merely declines to say.

    A non-string `source` takes the same path: `str(42)` is `"42"`, which is a
    perfectly serialisable answer and a completely invented one.
    """
    ipa = _an_ipa(tmp_path)
    stat = ipa.stat()
    artwork.artwork_dir().mkdir(parents=True, exist_ok=True)
    artwork.artwork_path("com.example.App").write_bytes(_PNG)
    for bad_source in (None, "", 42, [], {"a": 1}):
        artwork.sidecar_path("com.example.App").write_text(
            json.dumps(
                {
                    "bundle_id": "com.example.App",
                    "source": bad_source,
                    "resolved_at": int(time.time()),
                    "ipa_sha256": None,
                    "ipa_size": stat.st_size,
                    "ipa_mtime_ns": stat.st_mtime_ns,
                }
            )
        )
        with _rungs() as (from_ipa, from_store, from_device):
            result = artwork.resolve("com.example.App", ipa=ipa)
        from_ipa.assert_not_called()
        from_store.assert_not_called()
        from_device.assert_not_called()
        assert result is not None, f"{bad_source!r} should still be a cache hit"
        assert result.source == artwork.SOURCE_UNKNOWN, f"{bad_source!r}"
        assert result.source != artwork.SOURCE_IPA


def test_a_refresh_that_misses_leaves_the_cached_icon_in_place(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`refresh` writes nothing on a miss, and deletes nothing either.

    An offline machine asking for a refresh should keep showing the icon it
    already has rather than trading a stale one for none at all. `resolve`
    still returns None — that is what "nothing was resolved just now" means,
    and Task 6 does the messaging — but the cache survives, so the next
    ordinary call serves it again.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        artwork.resolve("com.example.App", ipa=ipa)

    with _rungs(ipa=None, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=ipa, refresh=True) is None

    png = artwork.artwork_path("com.example.App")
    assert png.exists(), "a refresh that found nothing must not delete what it had"
    assert png.read_bytes() == _PNG
    assert artwork.sidecar_path("com.example.App").exists()

    with _rungs() as (from_ipa, from_store, from_device):
        after = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    assert after is not None
    assert after.cached is True


def test_an_entry_with_no_ipa_behind_it_expires(temp_config_env: Path) -> None:
    """An entry resolved with no IPA stores no IPA identity, so the comparison
    that invalidates every other entry is trivially equal on every call. That
    is the ordinary state of a freshly `--add-app`'d app — nothing downloaded
    yet, icon from the App Store — and it left the entry with no invalidation
    trigger at all short of `--refresh-artwork`. Verified before the fix: a
    sidecar stamped 1970 was still a cache hit."""
    with _rungs(ipa=None, app_store=_PNG, device=None):
        assert artwork.resolve("com.example.App", ipa=None) is not None

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    assert sidecar["ipa_size"] is None, "premise: this entry has no IPA behind it"
    sidecar["resolved_at"] = int(time.time()) - artwork._TTL_WITHOUT_AN_IPA_SECONDS - 1
    artwork.sidecar_path("com.example.App").write_text(json.dumps(sidecar))

    with _rungs(ipa=None, app_store=_png(b"-fresh"), device=None) as (_, from_store, _):
        result = artwork.resolve("com.example.App", ipa=None)
    from_store.assert_called_once()
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _png(b"-fresh")


def test_an_entry_within_the_age_limit_is_still_served(temp_config_env: Path) -> None:
    """The limit is a bound, not an expiry on every call — the point is to stop
    the App Store being asked again on every GUI launch."""
    with _rungs(ipa=None, app_store=_PNG, device=None):
        assert artwork.resolve("com.example.App", ipa=None) is not None

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    sidecar["resolved_at"] = int(time.time()) - artwork._TTL_WITHOUT_AN_IPA_SECONDS + 3600
    artwork.sidecar_path("com.example.App").write_text(json.dumps(sidecar))

    with _rungs() as (from_ipa, from_store, from_device):
        result = artwork.resolve("com.example.App", ipa=None)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    assert result is not None
    assert result.cached is True


def test_an_entry_with_an_ipa_behind_it_does_not_expire(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The age limit is gated on having no IPA, and the gate is the point.

    An entry whose IPA identity still matches has a better trigger already;
    expiring it too would re-fetch icons that a perfectly good comparison has
    just said are current. Ancient here — a decade — so this cannot pass merely
    by sitting inside the window.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=ipa) is not None

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    assert sidecar["ipa_size"] == ipa.stat().st_size, "premise: an IPA is recorded"
    sidecar["resolved_at"] = int(time.time()) - 10 * 365 * 24 * 60 * 60
    artwork.sidecar_path("com.example.App").write_text(json.dumps(sidecar))

    with _rungs() as (from_ipa, from_store, from_device):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    assert result is not None
    assert result.cached is True


def test_an_unreadable_resolved_at_expires_rather_than_never_expiring(
    temp_config_env: Path,
) -> None:
    """A hand-edited or missing age reads as too old. Unbounded age is the one
    thing the limit exists to prevent, so it must not be what a bad value
    silently restores."""
    with _rungs(ipa=None, app_store=_PNG, device=None):
        assert artwork.resolve("com.example.App", ipa=None) is not None

    for bad_age in ("recently", None, [], {}):
        sidecar = artwork.read_sidecar("com.example.App")
        assert sidecar is not None
        sidecar["resolved_at"] = bad_age
        artwork.sidecar_path("com.example.App").write_text(json.dumps(sidecar))
        with _rungs(ipa=None, app_store=_PNG, device=None) as (_, from_store, _):
            result = artwork.resolve("com.example.App", ipa=None)
        assert from_store.call_count == 1, f"{bad_age!r} should have forced a re-resolve"
        assert result is not None
        assert result.cached is False


def test_a_cache_hit_never_hashes_the_ipa(temp_config_env: Path, tmp_path: Path) -> None:
    """The whole point of `(size, mtime_ns)`: hitting the cache must not hash
    the IPA.

    Hashing measured at 86 ms for the 242 MB Example IPA, warm, and the GUI
    runs `--artwork` across every configured app on launch — so a digest
    compared on the hit path meant streaming gigabytes to re-confirm that
    nothing had changed. This is the regression that would otherwise be
    invisible: every other test in this file passes just as happily with the
    hash back on the hot path.
    """
    ipa = _an_ipa(tmp_path)
    with (
        _rungs(ipa=_PNG, app_store=None, device=None),
        patch.object(artwork, "_sha256", wraps=artwork._sha256) as sha,
    ):
        artwork.resolve("com.example.App", ipa=ipa)
    assert sha.call_count == 1, "the write path records a digest exactly once"

    with (
        _rungs() as (from_ipa, from_store, from_device),
        patch.object(artwork, "_sha256", wraps=artwork._sha256) as sha,
    ):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    sha.assert_not_called()
    assert result is not None
    assert result.cached is True


def test_a_same_size_ipa_replacement_is_still_noticed(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Size alone is not the key — mtime is the other half.

    A re-signed IPA can plausibly land at the same byte count. The timestamp is
    forced rather than raced, so this asserts the property instead of hoping
    two quick writes get distinct nanosecond stamps.
    """
    ipa = _an_ipa(tmp_path, b"version one")
    with _rungs(ipa=_PNG, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=ipa) is not None

    assert len(b"version one") == len(b"version two"), "premise: identical size"
    ipa.write_bytes(b"version two")
    stat = ipa.stat()
    os.utime(ipa, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    with _rungs(ipa=_png(b"-two"), app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    assert result is not None
    assert result.path.read_bytes() == _png(b"-two")


def test_a_sidecar_written_before_the_stat_key_re_resolves_once(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The two fields are additive: an older sidecar has neither, so it reads
    as "no IPA behind this entry", misses against an app that has one, and is
    rewritten. One re-resolve, no migration step, no format version."""
    ipa = _an_ipa(tmp_path)
    artwork.artwork_dir().mkdir(parents=True, exist_ok=True)
    artwork.artwork_path("com.example.App").write_bytes(_PNG)
    artwork.sidecar_path("com.example.App").write_text(
        json.dumps(
            {
                "bundle_id": "com.example.App",
                "source": artwork.SOURCE_IPA,
                "resolved_at": int(time.time()),
                "ipa_sha256": hashlib.sha256(ipa.read_bytes()).hexdigest(),
            }
        )
    )

    with _rungs(ipa=_png(b"-rewritten"), app_store=None, device=None) as (from_ipa, _, _):
        first = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    assert first is not None
    assert first.cached is False

    # And the rewrite settles it — the next call hits.
    with _rungs() as (from_ipa, from_store, from_device):
        second = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_not_called()
    from_store.assert_not_called()
    from_device.assert_not_called()
    assert second is not None
    assert second.cached is True


def test_a_zero_byte_png_is_not_served_from_the_cache(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`resolve` cannot produce one, so this is a truncating restore, a copy
    off a full disk, or somebody else's ENOSPC. Without the header check the
    file exists, the sidecar vouches for it, and it is served forever while
    decoding to nothing — a permanent "the icon just vanished"."""
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=ipa) is not None

    artwork.artwork_path("com.example.App").write_bytes(b"")
    with _rungs(ipa=_PNG, app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    from_ipa.assert_called_once()
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _PNG


def test_a_cached_png_that_is_not_an_image_is_not_served(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The magic-byte sniff runs on the cache *hit* path, not only before a write.

    Stickiness — the entire argument for sniffing at all — is a property of the
    hit. `resolve` refuses to write non-image bytes, but the cache is an
    ordinary directory: an entry written by this feature's own earlier code, a
    hand-placed file, a partial restore that is not zero-length. Whatever is in
    there was served forever under a sidecar that vouched for it, recoverable
    only via `--refresh-artwork` — which is the exact defect the write-path
    sniff was added to prevent, arriving through the door it left open.
    """
    ipa = _an_ipa(tmp_path)
    with _rungs(ipa=_PNG, app_store=None, device=None):
        assert artwork.resolve("com.example.App", ipa=ipa) is not None

    # Deliberately non-empty, so the zero-byte guard cannot be what catches it.
    artwork.artwork_path("com.example.App").write_bytes(
        b"<!DOCTYPE html>\n<html><body>Access Denied</body></html>"
    )
    with _rungs(ipa=_PNG, app_store=None, device=None) as (from_ipa, _, _):
        result = artwork.resolve("com.example.App", ipa=ipa)
    assert from_ipa.call_count == 1, "a poisoned cache entry must force a re-resolve"
    assert result is not None
    assert result.cached is False
    assert result.path.read_bytes() == _PNG


def test_an_ipa_replaced_while_a_rung_reads_it_records_no_identity(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`--provide-ipa` or a download can land between the stat and the read.

    Recording the pre-write identity against post-write contents would pin a
    wrong icon until the *next* time the IPA changed. Instead nothing is
    recorded, so the entry has no IPA behind it and the next call re-resolves.
    Same two-stat shape, and the same reasoning, as `IPAInfoMemo.info_for`.
    """
    ipa = _an_ipa(tmp_path, b"version one")

    def replace_the_ipa_mid_read(_: Path) -> bytes:
        ipa.write_bytes(b"version two, landed mid-read")
        return _PNG

    with _rungs(ipa=replace_the_ipa_mid_read, app_store=None, device=None):
        result = artwork.resolve("com.example.App", ipa=ipa)
    assert result is not None

    sidecar = artwork.read_sidecar("com.example.App")
    assert sidecar is not None
    assert sidecar["ipa_size"] is None, "a straddled read must pin no identity"
    assert sidecar["ipa_mtime_ns"] is None
    assert sidecar["ipa_sha256"] is None

    with _rungs(ipa=_png(b"-settled"), app_store=None, device=None) as (from_ipa, _, _):
        second = artwork.resolve("com.example.App", ipa=ipa)
    assert from_ipa.call_count == 1, "an entry pinned to nothing must not be served"
    assert second is not None
    assert second.path.read_bytes() == _png(b"-settled")
