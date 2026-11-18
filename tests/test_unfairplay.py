"""`unfairplay.py`: decrypting through the user's UnfairPlay build.

Against the fake in `appstore_fakes` — never the real tool, whose model is this
Mac's FairPlay enrollment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa import unfairplay
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FakeTools, make_ipa


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


def test_the_version_is_read_off_its_own_banner(fakes: FakeTools) -> None:
    assert unfairplay.version(fakes.unfairplay) == "dev-d7503082a59e"


def test_a_missing_binary_has_no_version(tmp_path: Path) -> None:
    assert unfairplay.version(tmp_path / "nope") is None


def test_enrolled_needs_a_model_file(tmp_path: Path) -> None:
    models = tmp_path / "models"
    assert unfairplay.enrolled(models) is False, "missing directory"
    models.mkdir()
    assert unfairplay.enrolled(models) is False, "empty directory"
    (models / "notes.txt").write_text("not a model")
    assert unfairplay.enrolled(models) is False, "only a non-model file"
    (models / "d713869b.bin").mkdir()
    assert unfairplay.enrolled(models) is False, "a directory named like a model"
    (models / "d713869b-00000003.bin").write_bytes(b"x")
    assert unfairplay.enrolled(models) is True


def _decrypt(fakes: FakeTools, tmp_path: Path, **kw: object) -> unfairplay.DecryptReport:
    src = kw.pop("src", None) or make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return unfairplay.decrypt(
        fakes.unfairplay,
        input_ipa=src,  # type: ignore[arg-type]
        output_ipa=work / "out.ipa",
        report=work / "report.json",
        model_dir=fakes.models,
    )


def test_decrypt_runs_the_spec_command(fakes: FakeTools, tmp_path: Path) -> None:
    src = make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True)

    _decrypt(fakes, tmp_path, src=src)

    (call,) = fakes.calls("unfairplay")
    work = tmp_path / "work"
    assert call["argv"] == [
        "decrypt", "--input", str(src), "--output", str(work / "out.ipa"),
        "--report", str(work / "report.json"), "--model", str(fakes.models),
        "--strip-account-data",
    ]


def test_decrypt_parses_the_report(fakes: FakeTools, tmp_path: Path) -> None:
    report = _decrypt(fakes, tmp_path)

    assert report.output_path == tmp_path / "work" / "out.ipa"
    assert report.output_path.is_file()
    assert report.encrypted_images == 8
    assert report.decrypted_pages == 51088
    assert report.plaintext_verified is True


def test_unverified_plaintext_is_reported_not_judged(fakes: FakeTools, tmp_path: Path) -> None:
    """Whether `false` is acceptable is the caller's rule (`--allow-unverified`),
    so this layer only has to carry it faithfully — and `False` must not be
    confused with "missing"."""
    fakes.set(plaintext_verified=False)

    assert _decrypt(fakes, tmp_path).plaintext_verified is False


def test_a_failed_decrypt_carries_the_tools_own_words(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(decrypt_error="no enrollment model matches this IPA's SINF")

    with pytest.raises(AppStoreError) as e:
        _decrypt(fakes, tmp_path)

    assert e.value.message == (
        "UnfairPlay couldn't decrypt enc.ipa: no enrollment model matches this IPA's SINF"
    )


def test_the_report_it_leaves_beside_the_input_is_removed(
    fakes: FakeTools, tmp_path: Path
) -> None:
    """The spec's warning: `ReportPath` lands next to the *input*. For a drop
    that is the user's own folder, and the report names the account's keys."""
    fakes.set(write_default_report=True)
    src = make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True)

    _decrypt(fakes, tmp_path, src=src)

    assert not (tmp_path / "in" / "enc.decrypted.json").exists()
    assert sorted(p.name for p in (tmp_path / "in").iterdir()) == ["enc.ipa"]


def test_it_is_removed_on_failure_too(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(write_default_report=True, decrypt_error="boom")
    src = make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True)

    with pytest.raises(AppStoreError):
        _decrypt(fakes, tmp_path, src=src)

    assert not (tmp_path / "in" / "enc.decrypted.json").exists()


def test_a_file_of_that_name_that_was_already_there_is_left_alone(
    fakes: FakeTools, tmp_path: Path
) -> None:
    """Deleting by name is only safe for a name we caused. One the user had
    before the run is theirs — even though the tool then overwrote it, which
    is UnfairPlay's doing and not something to compound by deleting it."""
    src = make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True)
    mine = tmp_path / "in" / "enc.decrypted.json"
    mine.write_text("the user's")

    _decrypt(fakes, tmp_path, src=src)

    assert mine.exists()


def test_a_missing_binary_is_an_install_hint(tmp_path: Path) -> None:
    with pytest.raises(AppStoreError) as e:
        unfairplay.decrypt(
            tmp_path / "nope", input_ipa=tmp_path / "x.ipa", output_ipa=tmp_path / "y.ipa",
            report=tmp_path / "r.json", model_dir=tmp_path,
        )

    assert e.value.message == f"UnfairPlay not found at {tmp_path / 'nope'}"
