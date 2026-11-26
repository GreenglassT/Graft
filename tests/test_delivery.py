from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import fields, replace
from pathlib import Path

import pytest

from graft_ipa import delivery
from graft_ipa.config import AppConfig, CyanOptions, load_config
from graft_ipa.delivery import (
    CYAN_KEYS,
    FORMAT_VERSION,
    DeliveredSnapshot,
    compute_inputs_digest,
    diff_delivery,
)


def _tweak(cfg, name: str, body: bytes) -> Path:
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.tweaks_dir / name
    p.write_bytes(body)
    return p


def _app(**over) -> AppConfig:
    base = dict(
        name="example",
        bundle_id="com.example.app",
        preferred_version="21.16.2",
        tweaks=["a.deb", "b.deb"],
        cyan=CyanOptions(),
        delivery="livecontainer",
    )
    base.update(over)
    return AppConfig(**base)


def test_digest_is_stable_across_calls(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    app = _app()

    first = compute_inputs_digest(cfg, app, source_version="21.16.2")
    second = compute_inputs_digest(cfg, app, source_version="21.16.2")

    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_rebuilding_a_tweak_in_place_changes_the_digest(temp_config_env: Path) -> None:
    """The case that motivated hashing contents: same filename, new bytes."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    app = _app()
    before = compute_inputs_digest(cfg, app, source_version="21.16.2")

    _tweak(cfg, "a.deb", b"AAA-rebuilt-from-source")
    after = compute_inputs_digest(cfg, app, source_version="21.16.2")

    assert before.digest != after.digest


def test_tweak_order_matters(temp_config_env: Path) -> None:
    """cyan injects in config order, so the digest must respect it."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")

    forward = compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="1")
    reverse = compute_inputs_digest(cfg, _app(tweaks=["b.deb", "a.deb"]), source_version="1")

    assert forward.digest != reverse.digest


def test_attaching_a_tweak_changes_the_digest(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    _tweak(cfg, "c.deb", b"CCC")

    two = compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="1")
    three = compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb", "c.deb"]), source_version="1")

    assert two.digest != three.digest


def test_version_and_cyan_and_delivery_all_contribute(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    base = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    assert compute_inputs_digest(cfg, _app(), source_version="22.0.0").digest != base.digest
    assert compute_inputs_digest(
        cfg, _app(cyan=CyanOptions(compression_level=9)), source_version="21.16.2"
    ).digest != base.digest
    assert compute_inputs_digest(
        cfg, _app(delivery="sideload"), source_version="21.16.2"
    ).digest != base.digest


def test_a_missing_tweak_file_is_recorded_not_raised(temp_config_env: Path) -> None:
    """A config can reference a tweak that isn't on disk. That must not crash a
    status check — it's a real state to report, not an error."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    app = _app(tweaks=["a.deb", "gone.deb"])

    d = compute_inputs_digest(cfg, app, source_version="1")

    assert d.tweaks[1].name == "gone.deb"
    assert d.tweaks[1].sha256 is None
    assert d.tweaks[1].error == "absent"
    assert d.missing == ["gone.deb"]


def test_an_unreadable_tweak_is_distinguished_from_missing(temp_config_env: Path) -> None:
    """A directory in place of a tweak (or a permission error, or any other
    unreadable file) is not the same state as a tweak that was never
    downloaded — conflating them would make an unmounted tweaks_dir look like
    every tweak in the app went missing."""
    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    (cfg.tweaks_dir / "a.deb").mkdir()  # a directory, not a tweak file
    app = _app(tweaks=["a.deb"])

    d = compute_inputs_digest(cfg, app, source_version="1")

    assert d.tweaks[0].sha256 is None
    assert d.tweaks[0].error == "unreadable"
    assert d.missing == []


def test_a_tweak_under_an_unmounted_tweaks_dir_is_unreadable_not_missing(
    temp_config_env: Path,
) -> None:
    """The headline case: `FileNotFoundError` is raised both for a file that
    just isn't in its (readable) directory and for a directory that isn't
    there at all. Classifying on the exception type alone — as an earlier
    version of this module did — would make every tweak in an unmounted
    tweaks_dir report as individually 'missing', which is precisely what the
    `missing` docstring and the spec claim can't happen. This test fails
    against that version."""
    cfg = load_config(temp_config_env)
    assert not cfg.tweaks_dir.exists()  # never created -- the unmounted-volume shape
    app = _app(tweaks=["a.deb"])

    d = compute_inputs_digest(cfg, app, source_version="1")

    assert d.tweaks[0].sha256 is None
    assert d.tweaks[0].error == "unreadable"
    assert d.missing == []


def test_recorded_cyan_is_insulated_from_later_config_mutation(temp_config_env: Path) -> None:
    """extra_args is the one CyanOptions field that's a list. Without freezing
    it, the frozen InputsDigest would hold a live reference into the app's
    own CyanOptions, so mutating the config after computing a digest would
    retroactively change what an already-recorded digest reports."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    app = _app(cyan=CyanOptions(extra_args=["-k", "somelib.dylib"]))

    d = compute_inputs_digest(cfg, app, source_version="1")
    recorded = d.cyan["extra_args"]

    app.cyan.extra_args.append("-oops")

    assert d.cyan["extra_args"] == recorded
    assert "-oops" not in d.cyan["extra_args"]


def test_cyan_keys_covers_every_field_that_reaches_cyan() -> None:
    """CYAN_KEYS is hand-maintained on purpose — it's a persisted wire format,
    so it must not follow CyanOptions automatically. This makes adding a field
    a deliberate decision rather than a silent omission."""
    assert set(CYAN_KEYS) == {f.name for f in fields(CyanOptions)}


def test_format_version_is_recorded_and_moves_the_digest(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest is persisted indefinitely, so a refactor that quietly drops
    the "v" key from the hashed payload must not go unnoticed with every
    other test still green."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    app = _app()

    base = compute_inputs_digest(cfg, app, source_version="1")
    assert base.version == delivery.FORMAT_VERSION

    monkeypatch.setattr(delivery, "FORMAT_VERSION", delivery.FORMAT_VERSION + 1)
    bumped = compute_inputs_digest(cfg, app, source_version="1")

    assert bumped.version == delivery.FORMAT_VERSION
    assert bumped.digest != base.digest


def test_the_digest_format_is_pinned(temp_config_env: Path) -> None:
    """Fails whenever the hashed payload changes. That is the point.

    If this test fails and the change to what gets hashed was intentional,
    bump FORMAT_VERSION and update the literal below in the same commit. Do
    not update the literal alone: a stored row from the previous format would
    then compare against the new algorithm and report differences nobody made.

    Both literals are written out rather than referenced. `FORMAT_VERSION` here
    would make the version assertion vacuous, and recomputing the sha256 from a
    payload the test builds itself would pass against any implementation —
    including one that had quietly stopped hashing a field.

    Every input is fixed by this file (`_app`'s literals, `CyanOptions`'
    defaults, two tweak files written here), so the expected value is a
    property of the algorithm alone and not of the machine running it.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")

    d = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    assert d.version == 1
    assert d.digest == "61c140d8261e350456056e1fbdf968a87627af60e659620382e97d1f19f50b91"


def _snapshot(d) -> DeliveredSnapshot:
    return DeliveredSnapshot(
        inputs_digest=d.digest,
        digest_version=d.version,
        bundle_id=d.bundle_id,
        source_version=d.source_version,
        delivery=d.delivery,
        tweaks=[[t.name, t.sha256] for t in d.tweaks],
        pushed_at="2026-06-02T01:44:29+00:00",
        cyan=d.cyan,
        held_version_id=d.held_version_id,
    )


@pytest.mark.parametrize("held_id", [None, "800200"])
def test_the_record_carries_every_input_the_digest_hashes(
    temp_config_env: Path, held_id: str | None,
) -> None:
    """The payload dict in `compute_inputs_digest` is the authority on what a
    change to the app means — not `InputsDigest`'s field list. An input that is
    hashed but not carried on the record can never get a reason branch in
    `diff_delivery`, so it silently reports as a build-options change. That is
    exactly how `bundle_id` was missed.

    Rebuilding the digest from the record's own fields is what pins the two
    together: a newly hashed key that isn't carried makes this fail. The
    duplicated payload shape is deliberate, in the same spirit as CYAN_KEYS —
    it's a persisted wire format, so it should not follow the implementation
    automatically.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    app = _app(cyan=CyanOptions(extra_args=["-k", "somelib.dylib"]), preferred_version_id=held_id)

    d = compute_inputs_digest(cfg, app, source_version="21.16.2")

    payload = {
        "v": d.version,
        "bundle_id": d.bundle_id,
        "source_version": d.source_version,
        "delivery": d.delivery,
        "tweaks": [[t.name, t.sha256] for t in d.tweaks],
        "cyan": d.cyan,
    }
    if d.held_version_id is not None:
        payload["held_version_id"] = d.held_version_id
    assert d.held_version_id == held_id
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )

    assert hashlib.sha256(blob).hexdigest() == d.digest


def test_moving_a_hold_to_the_other_build_is_named(temp_config_env: Path) -> None:
    """Two builds of 2026.42.0 read the same version: the version alone left
    a hold moved from build 1 to build 2 `current`, though a build now starts
    from the other file (`ipa_cache.admits`)."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    one = compute_inputs_digest(cfg, _app(preferred_version_id="800200"), source_version="21.16.2")
    two = compute_inputs_digest(cfg, _app(preferred_version_id="800250"), source_version="21.16.2")
    by_version = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    assert len({one.digest, two.digest, by_version.digest}) == 3
    for now, was in ((two, one), (by_version, one), (one, by_version)):
        state = diff_delivery(now, _snapshot(was))
        assert (state.kind, state.reasons) == ("stale", ["the held build changed"])
    assert diff_delivery(one, _snapshot(one)).kind == "current"


def test_an_app_held_by_no_build_hashes_as_before(temp_config_env: Path) -> None:
    """The id is hashed only when there is one, so every digest recorded
    before holds by build still compares equal (the pinned literal above is
    the same app with no id)."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")

    d = compute_inputs_digest(cfg, _app(), source_version="21.16.2")
    latest = compute_inputs_digest(cfg, _app(preferred_version="latest"), source_version="21.16.2")
    stray = compute_inputs_digest(
        cfg, _app(preferred_version="latest", preferred_version_id="800200"), source_version="21.16.2",
    )

    assert d.held_version_id is None
    assert d.digest == "61c140d8261e350456056e1fbdf968a87627af60e659620382e97d1f19f50b91"
    assert latest.digest == d.digest
    assert stray.digest == d.digest and stray.held_version_id is None, "latest holds no build"


def test_a_held_build_of_another_version_is_the_version_change(temp_config_env: Path) -> None:
    """From a build of 1.2.0 to a build of 1.3.0: the version says it, and a
    second reason for the same change would push a real one off the badge."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    was = compute_inputs_digest(cfg, _app(preferred_version_id="800200"), source_version="1.2.0")
    now = compute_inputs_digest(cfg, _app(preferred_version_id="800300"), source_version="1.3.0")

    state = diff_delivery(now, _snapshot(was))

    assert state.reasons == ["app version changed: 1.2.0 → 1.3.0"]


def test_a_bundle_id_change_is_named(temp_config_env: Path) -> None:
    """Hashed since Task 1, but carried on no record until now, so it fell
    through to the build-options fallback."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))
    current = compute_inputs_digest(
        cfg, _app(bundle_id="com.example.app.tweaked"), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any(
        "com.example.app" in r and "com.example.app.tweaked" in r
        for r in state.reasons
    )
    assert "build options changed" not in state.reasons


def test_no_record_is_never_delivered(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    state = diff_delivery(current, None)

    assert state.kind == "never"
    assert "never" in state.summary.lower()


def test_matching_digests_are_current(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    state = diff_delivery(current, _snapshot(current))

    assert state.kind == "current"
    assert state.reasons == []


def test_an_added_tweak_is_named(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    _tweak(cfg, "c.deb", b"CCC")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )
    current = compute_inputs_digest(
        cfg, _app(tweaks=["a.deb", "b.deb", "c.deb"]), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("c.deb" in r and "added" in r for r in state.reasons)


def test_a_removed_tweak_is_named(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )
    current = compute_inputs_digest(cfg, _app(tweaks=["a.deb"]), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("b.deb" in r and "removed" in r for r in state.reasons)


def test_a_rebuilt_tweak_is_named_as_rebuilt(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))

    _tweak(cfg, "a.deb", b"AAA-rebuilt")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("a.deb" in r and "rebuilt" in r for r in state.reasons)


def test_a_version_change_is_named(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))
    current = compute_inputs_digest(cfg, _app(), source_version="22.0.0")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("21.16.2" in r and "22.0.0" in r for r in state.reasons)
    # "app version", not a bare "version": this is rendered next to a list of
    # .deb filenames, where "version changed" reads as a tweak's version.
    assert state.reasons[0].startswith("app version changed")


def test_a_delivery_mode_change_is_named(temp_config_env: Path) -> None:
    """livecontainer and sideload produce different artifacts."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(delivery="livecontainer"), source_version="21.16.2")
    )
    current = compute_inputs_digest(cfg, _app(delivery="sideload"), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("livecontainer" in r and "sideload" in r for r in state.reasons)
    assert "build options changed" not in state.reasons


def test_reordering_tweaks_is_named_as_an_order_change(temp_config_env: Path) -> None:
    """cyan injects in config order, so a reorder changes the artifact.

    The kind was always right here; only the reason overreached, blaming
    build options for something the user did to their tweak list. A test
    asserting only `kind` would have passed against that bug.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )
    current = compute_inputs_digest(cfg, _app(tweaks=["b.deb", "a.deb"]), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert "tweak order changed" in state.reasons
    assert "build options changed" not in state.reasons


def test_an_added_tweak_does_not_also_report_an_order_change(temp_config_env: Path) -> None:
    """Adding a tweak shifts positions as a side effect. Reporting both would
    spend the two-reason cap describing one event twice."""
    cfg = load_config(temp_config_env)
    for n in "abc":
        _tweak(cfg, f"{n}.deb", n.encode())
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )
    current = compute_inputs_digest(
        cfg, _app(tweaks=["c.deb", "a.deb", "b.deb"]), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.reasons == ["c.deb added"]


def test_a_record_without_a_digest_is_unknown_not_stale(temp_config_env: Path) -> None:
    """Deliveries predating this feature must not nag.

    The summary is asserted, not just the kind, and that is what makes this
    test see the branch it names. A pre-feature row carries neither
    `inputs_digest` nor `digest_version`, so the version guard *below* the
    no-digest guard answers `unknown` for it too — on kind alone this passed
    with the no-digest guard deleted outright. The two branches differ only in
    what they tell the user, so the wording is the assertion that separates
    them: this row is not from "a different version of this tool", and telling
    someone to push again to re-establish a baseline they never had is the
    wrong instruction.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")
    legacy = DeliveredSnapshot(
        inputs_digest=None,
        digest_version=None,
        bundle_id="com.example.app",
        source_version="21.16.2",
        delivery="livecontainer",
        tweaks=[],
        pushed_at="2026-05-13T16:20:00+00:00",
    )

    state = diff_delivery(current, legacy)

    assert state.kind == "unknown"
    assert state.summary == "Delivered before this tool tracked build inputs."


def test_a_digest_from_a_future_format_is_unknown_not_stale(temp_config_env: Path) -> None:
    """A digest_version we don't produce can't be compared.

    Without this the next bump to what gets hashed flips every recorded app to
    stale exactly once, with an empty reason list — the structured parts all
    still match, only the hashing changed. See spec 4.2.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")
    future = replace(
        _snapshot(current), digest_version=FORMAT_VERSION + 1, inputs_digest="deadbeef"
    )

    state = diff_delivery(current, future)

    assert state.kind == "unknown"
    assert state.reasons == []
    # An unknown the user can act on: with no reasons to show, the summary is
    # the whole message, so it has to carry the way out.
    assert "Push again" in state.summary


def test_an_unmounted_tweaks_dir_blocks_comparison_rather_than_lying(
    temp_config_env: Path,
) -> None:
    """The motivating case: tweaks_dir on a volume that isn't mounted.

    Every tweak hashes to None, which is the same contribution an absent one
    makes, so a naive sha comparison reports every tweak in the app as
    rebuilt — and a naive missing-check reports every tweak as deleted. We
    genuinely cannot tell, so say so.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))

    shutil.rmtree(cfg.tweaks_dir)  # the whole directory is gone, not just the files
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "unknown"
    assert "a.deb" in state.summary
    assert not any("rebuilt" in r or "missing" in r for r in state.reasons)


def test_a_deleted_tweak_file_is_named_as_missing_not_rebuilt(temp_config_env: Path) -> None:
    """Still attached in config, but the file is gone — that is not a rebuild."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))

    (cfg.tweaks_dir / "a.deb").unlink()
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert any("a.deb" in r and "missing" in r for r in state.reasons)
    assert not any("rebuilt" in r for r in state.reasons)


def test_reasons_are_capped(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    for n in "abcdef":
        _tweak(cfg, f"{n}.deb", n.encode())
    delivered = _snapshot(compute_inputs_digest(cfg, _app(tweaks=["a.deb"]), source_version="1"))
    current = compute_inputs_digest(
        cfg, _app(tweaks=["a.deb", "b.deb", "c.deb", "d.deb", "e.deb"]), source_version="2"
    )

    state = diff_delivery(current, delivered)

    assert len(state.reasons) <= 3
    assert "more" in state.reasons[-1]


def test_a_third_reason_is_shown_rather_than_hidden_behind_plus_one_more(
    temp_config_env: Path,
) -> None:
    """At exactly one over the cap, "+1 more changes" is strictly worse than
    the reason it replaces: same line count, less information."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    _tweak(cfg, "c.deb", b"CCC")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="1")
    )
    current = compute_inputs_digest(
        cfg,
        _app(tweaks=["a.deb", "b.deb", "c.deb"], delivery="sideload"),
        source_version="2",
    )

    state = diff_delivery(current, delivered)

    assert len(state.reasons) == 3
    assert "c.deb added" in state.reasons
    assert not any("more" in r for r in state.reasons)


def test_a_build_breaking_reason_outranks_informational_ones_under_the_cap(
    temp_config_env: Path,
) -> None:
    """`missing from disk` is the only reason that predicts a *failed* build —
    cyan_runner raises "missing tweak files" on it. Emitted last, it was the
    first thing the cap discarded, so the user saw two changes that would have
    built fine and none of the one that won't."""
    cfg = load_config(temp_config_env)
    for n in "abcde":
        _tweak(cfg, f"{n}.deb", n.encode())
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )

    (cfg.tweaks_dir / "a.deb").unlink()
    current = compute_inputs_digest(
        cfg,
        _app(tweaks=["a.deb", "b.deb", "c.deb", "d.deb", "e.deb"]),
        source_version="21.16.2",
    )

    state = diff_delivery(current, delivered)

    assert state.reasons[0] == "a.deb missing from disk"
    assert state.reasons[-1] == "+2 more changes"
    assert "e.deb added" not in state.reasons  # informational, correctly truncated


def test_a_newly_attached_tweak_that_is_not_on_disk_says_so(temp_config_env: Path) -> None:
    """This feature's own motivating scenario, in reverse: attach a tweak you
    haven't built yet and the next build hard-fails on a file the tool already
    knew was absent. "added" alone doesn't warn."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(compute_inputs_digest(cfg, _app(), source_version="21.16.2"))
    current = compute_inputs_digest(
        cfg,
        _app(tweaks=["a.deb", "b.deb", "d-tweak.deb"]),
        source_version="21.16.2",
    )

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert state.reasons == ["d-tweak.deb added (not on disk)"]


def test_a_duplicated_tweak_entry_is_named(temp_config_env: Path) -> None:
    """config.py does no dedup and cyan_runner passes every entry with -f, so
    listing a tweak twice really does change the artifact. The set and the
    order are both unchanged, so nothing else in the diff can see it."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")
    )
    current = compute_inputs_digest(
        cfg, _app(tweaks=["a.deb", "b.deb", "a.deb"]), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert state.reasons == ["a tweak is listed more than once"]


def test_removing_a_duplicate_entry_does_not_claim_one_is_present(
    temp_config_env: Path,
) -> None:
    """The mirror case. Saying "a tweak is listed more than once" about a
    config that no longer lists one is the same failure as blaming build
    options: it sends the user looking for something that isn't there."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg, _app(tweaks=["a.deb", "b.deb", "a.deb"]), source_version="21.16.2"
        )
    )
    current = compute_inputs_digest(cfg, _app(tweaks=["a.deb", "b.deb"]), source_version="21.16.2")

    state = diff_delivery(current, delivered)

    assert state.kind == "stale"
    assert state.reasons == ["a duplicate tweak entry was removed"]
    assert "build options changed" not in state.reasons


def test_a_malformed_delivery_record_is_unknown_rather_than_an_exception(
    temp_config_env: Path,
) -> None:
    """A stored row this module can't parse must degrade to one uncertain
    badge, not an exception out of the middle of a list of every app."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")
    malformed = replace(
        _snapshot(current),
        inputs_digest="deadbeef",
        tweaks=[["a.deb", "sha", "extra"]],
    )

    state = diff_delivery(current, malformed)

    assert state.kind == "unknown"
    assert "Push again" in state.summary


def test_a_changed_cyan_option_is_named(temp_config_env: Path) -> None:
    """Spec §5.1 justifies storing cyan on the row with this exact promise:
    the diff says "display name changed: Example → Renamed"."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg, _app(cyan=CyanOptions(display_name="Example")), source_version="21.16.2"
        )
    )
    current = compute_inputs_digest(
        cfg, _app(cyan=CyanOptions(display_name="Renamed")), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.reasons == ["display name changed: Example → Renamed"]
    assert "build options changed" not in state.reasons


def test_changed_extra_args_are_named_without_leaking_a_repr(temp_config_env: Path) -> None:
    """extra_args is the one sequence-valued option. Rendered into a reason it
    reads as Python source, not as something the user typed."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg,
            _app(cyan=CyanOptions(extra_args=["-k", "libfoo.dylib"])),
            source_version="21.16.2",
        )
    )
    current = compute_inputs_digest(
        cfg, _app(cyan=CyanOptions(extra_args=[])), source_version="21.16.2"
    )

    state = diff_delivery(current, delivered)

    assert state.reasons == ["extra build arguments changed"]
    for ch in ("(", ")", "[", "]", "'"):
        assert ch not in state.reasons[0]


def test_stored_cyan_compares_equal_after_a_json_round_trip(temp_config_env: Path) -> None:
    """The stored row arrives via json.loads, so extra_args comes back a list
    while the freshly computed digest holds a tuple. Compared raw those are
    never equal, so every stale app with extra_args set would be told its build
    arguments changed when only the app version did.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    cyan = CyanOptions(extra_args=["-k", "libfoo.dylib"])
    delivered = _snapshot(compute_inputs_digest(cfg, _app(cyan=cyan), source_version="21.16.2"))
    # Exactly what Task 5 hands us: the row after a dumps/loads round trip.
    stored = replace(delivered, cyan=json.loads(json.dumps(delivered.cyan)))
    current = compute_inputs_digest(cfg, _app(cyan=cyan), source_version="22.0.0")

    state = diff_delivery(current, stored)

    assert state.reasons == ["app version changed: 21.16.2 → 22.0.0"]


def test_two_changed_cyan_options_are_both_named_in_config_key_order(
    temp_config_env: Path,
) -> None:
    """Iterating CYAN_KEYS rather than the stored row is what makes this
    deterministic. The row's key order is not ours to rely on: it arrives via
    `json.loads` of a cell some other run of this tool wrote.

    So the stored dict here is deliberately rebuilt in *reverse* CYAN_KEYS
    order, which is the only input that separates the two loops. Built the
    obvious way — straight from `compute_inputs_digest` — the row comes back
    already in CYAN_KEYS order, and `for key in delivered.cyan` produces an
    identical answer.
    """
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg,
            _app(cyan=CyanOptions(display_name="Example", compression_level=6)),
            source_version="21.16.2",
        )
    )
    delivered = replace(
        delivered, cyan={k: delivered.cyan[k] for k in reversed(CYAN_KEYS)}
    )
    current = compute_inputs_digest(
        cfg,
        _app(cyan=CyanOptions(display_name="Renamed", compression_level=9)),
        source_version="21.16.2",
    )

    state = diff_delivery(current, delivered)

    assert state.reasons == [
        "display name changed: Example → Renamed",
        "compression level changed: 6 → 9",
    ]


def test_option_values_render_in_user_vocabulary_not_python(temp_config_env: Path) -> None:
    """A badge is the last place to leak `None` and `True`/`False`. Same family
    as the extra_args repr: numbers and strings are already the user's words,
    but the implementation's sentinels are not."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg,
            _app(cyan=CyanOptions(new_bundle_id=None, strip_extensions=False)),
            source_version="21.16.2",
        )
    )
    current = compute_inputs_digest(
        cfg,
        _app(cyan=CyanOptions(new_bundle_id="com.foo.tweaked", strip_extensions=True)),
        source_version="21.16.2",
    )

    state = diff_delivery(current, delivered)

    assert state.reasons == [
        "new bundle id changed: (unset) → com.foo.tweaked",
        "strip extensions changed: no → yes",
    ]
    for reason in state.reasons:
        for leaked in ("None", "True", "False"):
            assert leaked not in reason


def test_a_row_without_stored_cyan_falls_back_to_the_generic_reason(
    temp_config_env: Path,
) -> None:
    """The only remaining way to reach "build options changed": a row written
    before the cyan column existed. There is nothing to compare against, so the
    generic string is the honest answer rather than a guess."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    delivered = _snapshot(
        compute_inputs_digest(
            cfg, _app(cyan=CyanOptions(display_name="Example")), source_version="21.16.2"
        )
    )
    legacy = replace(delivered, cyan=None)
    current = compute_inputs_digest(
        cfg, _app(cyan=CyanOptions(display_name="Renamed")), source_version="21.16.2"
    )

    state = diff_delivery(current, legacy)

    assert state.reasons == ["build options changed"]


def test_summaries_describe_the_record_not_the_device(temp_config_env: Path) -> None:
    """Spec §3: the import step is manual and unverified, and the UI must not
    imply otherwise. §10.1: say "delivered", never "installed". We have not
    touched the device — we are comparing against our own local record, and
    for livecontainer even a successful push only wrote a file into
    Documents/."""
    cfg = load_config(temp_config_env)
    _tweak(cfg, "a.deb", b"AAA")
    _tweak(cfg, "b.deb", b"BBB")
    current = compute_inputs_digest(cfg, _app(), source_version="21.16.2")
    matching = diff_delivery(current, _snapshot(current))

    changed = diff_delivery(
        compute_inputs_digest(cfg, _app(), source_version="22.0.0"), _snapshot(current)
    )

    assert matching.summary == "Up to date with the last delivery."
    assert changed.summary == "Changed since the last delivery."
    for s in (matching.summary, changed.summary):
        assert "phone" not in s.lower()
        assert "installed" not in s.lower()
