from __future__ import annotations

from pathlib import Path

from graft_ipa import artwork
from graft_ipa.config import default_cache_dir


def test_paths_live_beside_the_ipa_cache(temp_config_env: Path, tmp_path: Path) -> None:
    path = artwork.artwork_path("com.bravo.Bravo")
    assert path.name == "com.bravo.Bravo.png"
    assert path.parent.name == "artwork"
    # Siblings under the same graft cache root — not just two matching
    # path components, which `artwork_dir()` living under `xdg_config_home()`
    # or `xdg_state_home()` would satisfy just as well.
    assert artwork.artwork_dir().parent == default_cache_dir().parent
    # And the fixture actually isolated it. Without `temp_config_env` in
    # effect, the assertions above would just as happily pass against the
    # developer's real ~/.cache/graft/artwork/.
    assert tmp_path in path.parents


def test_a_bundle_id_cannot_escape_the_cache_directory(temp_config_env: Path) -> None:
    """A bundle id is attacker-adjacent: it comes from an IPA's Info.plist."""
    path = artwork.artwork_path("../../etc/passwd")
    assert path.parent == artwork.artwork_dir()
    assert "/" not in path.name.removesuffix(".png")


def test_a_bundle_id_that_slugs_to_nothing_falls_back_to_unnamed(
    temp_config_env: Path,
) -> None:
    """`..` is all dots, which `_slug`'s `strip(".")` reduces to "" — the cache
    still needs a real filename to write."""
    path = artwork.artwork_path("..")
    assert path.name == "unnamed.png"


def test_sidecar_round_trips(temp_config_env: Path) -> None:
    artwork.write_sidecar(
        "com.bravo.Bravo", source=artwork.SOURCE_IPA, ipa_sha256="abc123"
    )
    record = artwork.read_sidecar("com.bravo.Bravo")
    assert record is not None
    assert record["source"] == "ipa"
    assert record["ipa_sha256"] == "abc123"
    assert "resolved_at" in record


def test_sidecar_records_the_bundle_id(temp_config_env: Path) -> None:
    """`_slug` is lossy — `com.foo/bar` and `com.foo_bar` both slug to
    `com.foo_bar`, and case-insensitive APFS collapses `com.Bravo` and
    `com.bravo` onto one file. The sidecar has to carry the real id so a
    reader can notice the collision instead of serving the wrong app's icon
    under a name that merely looks right."""
    artwork.write_sidecar(
        "com.bravo.Bravo", source=artwork.SOURCE_IPA, ipa_sha256="abc123"
    )
    record = artwork.read_sidecar("com.bravo.Bravo")
    assert record is not None
    assert record["bundle_id"] == "com.bravo.Bravo"


def test_sidecar_round_trips_a_null_ipa_sha256(temp_config_env: Path) -> None:
    """The App Store and device rungs (Tasks 3-4) never hashed an IPA to begin
    with — `None` has to survive the round trip rather than becoming a
    string like "None" that a mismatch check would treat as real digest."""
    artwork.write_sidecar(
        "com.bravo.Bravo", source=artwork.SOURCE_APP_STORE, ipa_sha256=None
    )
    record = artwork.read_sidecar("com.bravo.Bravo")
    assert record is not None
    assert record["ipa_sha256"] is None


def test_reading_a_missing_sidecar_is_not_an_error(temp_config_env: Path) -> None:
    assert artwork.read_sidecar("com.nope.nope") is None


def test_a_corrupt_sidecar_reads_as_absent(temp_config_env: Path) -> None:
    """A half-written JSON file must force re-resolution, not crash a listing."""
    artwork.artwork_dir().mkdir(parents=True, exist_ok=True)
    artwork.sidecar_path("com.bravo.Bravo").write_text("{not json")
    assert artwork.read_sidecar("com.bravo.Bravo") is None


def test_a_well_formed_non_object_sidecar_reads_as_absent(temp_config_env: Path) -> None:
    """`[]`, `"abc"`, `42`, and `null` are all JSON that `json.loads` parses
    without complaint, but none of them has a `.get()` that behaves like a
    sidecar's — a consumer calling `.get("ipa_sha256")` on a list crashes, not
    degrades. Only one shape needs to be checked here to prove the guard
    exists; the mutation check (not a test) is what proves it actually bites."""
    artwork.artwork_dir().mkdir(parents=True, exist_ok=True)
    artwork.sidecar_path("com.bravo.Bravo").write_text("[]")
    assert artwork.read_sidecar("com.bravo.Bravo") is None
