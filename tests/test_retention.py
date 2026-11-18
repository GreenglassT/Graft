"""The pure half of archive retention: which files a keep-N policy drops.

Every test builds its archive under `tmp_path`. Nothing here reads or writes
the real `~/.cache/graft/built/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa.retention import parse_archive, plan_prune, scan_archives


def _archive(built: Path, name: str, size: int = 16) -> Path:
    built.mkdir(parents=True, exist_ok=True)
    path = built / name
    path.write_bytes(b"\x00" * size)
    return path


def _names(archives) -> list[str]:
    return sorted(a.path.name for a in archives)


def test_parses_the_name_build_one_actually_writes(tmp_path: Path) -> None:
    path = _archive(tmp_path, "bravo_2026.18.0_20260602T014429.ipa")
    parsed = parse_archive(path)
    assert parsed is not None
    assert (parsed.app, parsed.version, parsed.stamp) == (
        "bravo",
        "2026.18.0",
        "20260602T014429",
    )


def test_app_names_containing_underscores_group_under_the_whole_name(
    tmp_path: Path,
) -> None:
    """`slugify_name` emits underscores and `add_app`'s dedup appends `_2`.

    Splitting on the first underscore would file `my_app_2` under `my`, which
    then shares a keep budget with any other app whose slug starts `my_`.
    """
    parsed = parse_archive(_archive(tmp_path, "my_app_2_1.0_20260602T014429.ipa"))
    assert parsed is not None
    assert parsed.app == "my_app_2"


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "bravo.ipa",
        "bravo_2026.18.0.ipa",
        "bravo_2026.18.0_20260602T014429.zip",
        "bravo_2026.18.0_2026-06-02T01-44-29.ipa",
    ],
)
def test_files_that_arent_ours_never_parse(tmp_path: Path, name: str) -> None:
    assert parse_archive(_archive(tmp_path, name)) is None


def test_scan_skips_dotfiles_and_directories(tmp_path: Path) -> None:
    _archive(tmp_path, "bravo_1.0_20260602T014429.ipa")
    _archive(tmp_path, ".DS_Store")
    (tmp_path / "subdir").mkdir()

    archives, unparsed = scan_archives(tmp_path)
    assert _names(archives) == ["bravo_1.0_20260602T014429.ipa"]
    assert unparsed == []


def test_a_missing_built_dir_is_an_empty_plan_not_an_error(tmp_path: Path) -> None:
    plan = plan_prune(tmp_path / "never-created", keep=5)
    assert plan.remove == [] and plan.kept == []


def test_keeps_the_newest_n_per_app_version_and_drops_the_rest(tmp_path: Path) -> None:
    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000",
                  "20260601T030000"):
        _archive(tmp_path, f"bravo_1.0_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=2)

    assert _names(plan.kept) == [
        "bravo_1.0_20260601T020000.ipa",
        "bravo_1.0_20260601T030000.ipa",
    ]
    assert _names(plan.remove) == [
        "bravo_1.0_20260601T000000.ipa",
        "bravo_1.0_20260601T010000.ipa",
    ]


def test_the_keep_budget_does_not_leak_between_apps(tmp_path: Path) -> None:
    """Three apps, keep=1: each keeps its own newest, not one newest overall."""
    for app, stamps in {
        "bravo": ("20260601T000000", "20260601T010000", "20260601T020000"),
        "delta": ("20260513T024209", "20260513T104409"),
        "example": ("20260513T011421",),
    }.items():
        for stamp in stamps:
            _archive(tmp_path, f"{app}_1.0_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=1)

    assert _names(plan.kept) == [
        "bravo_1.0_20260601T020000.ipa",
        "delta_1.0_20260513T104409.ipa",
        "example_1.0_20260513T011421.ipa",
    ]
    assert plan.apps == ["bravo", "delta"]  # example had nothing to give up


def test_a_rebuild_storm_cannot_crowd_out_an_older_version(tmp_path: Path) -> None:
    """The case this policy exists for: one night of debugging, two versions.

    Fourteen rebuilds of 21.16.2 from a single session, then a couple of the
    version that shipped before it. Grouping per app alone would spend the
    whole budget on the newest version and trash *every* 21.15.1 archive — the
    only thing in the directory worth rolling back to a week later.
    """
    for hour in range(14):
        _archive(tmp_path, f"example_21.16.2_20260602T{hour:02d}0000.ipa")
    for stamp in ("20260514T090000", "20260514T100000"):
        _archive(tmp_path, f"example_21.15.1_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=2)

    assert _names(plan.kept) == [
        "example_21.15.1_20260514T090000.ipa",
        "example_21.15.1_20260514T100000.ipa",
        "example_21.16.2_20260602T120000.ipa",
        "example_21.16.2_20260602T130000.ipa",
    ]
    # Both 21.15.1 archives survive despite being older than all 14 removals.
    assert not any(a.version == "21.15.1" for a in plan.remove)
    assert len(plan.remove) == 12


def test_a_version_under_the_budget_gives_up_nothing(tmp_path: Path) -> None:
    """Each version is measured on its own, not against the app's total."""
    for i in range(5):
        _archive(tmp_path, f"bravo_2026.18.0_20260601T0{i}0000.ipa")
    lone = _archive(tmp_path, "bravo_2026.17.0_20260501T000000.ipa")

    plan = plan_prune(tmp_path, keep=2)

    assert lone.name in _names(plan.kept)
    assert _names(plan.remove) == [
        "bravo_2026.18.0_20260601T000000.ipa",
        "bravo_2026.18.0_20260601T010000.ipa",
        "bravo_2026.18.0_20260601T020000.ipa",
    ]


def test_versions_of_one_app_do_not_share_a_budget_at_keep_1(tmp_path: Path) -> None:
    """`keep: 1` keeps the newest build of *every* version, not one overall."""
    for version, stamps in {
        "2026.18.0": ("20260601T000000", "20260601T010000"),
        "2026.17.0": ("20260501T000000", "20260501T010000"),
    }.items():
        for stamp in stamps:
            _archive(tmp_path, f"bravo_{version}_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=1)

    assert _names(plan.kept) == [
        "bravo_2026.17.0_20260501T010000.ipa",
        "bravo_2026.18.0_20260601T010000.ipa",
    ]
    assert _names(plan.remove) == [
        "bravo_2026.17.0_20260501T000000.ipa",
        "bravo_2026.18.0_20260601T000000.ipa",
    ]


def test_the_same_version_of_two_apps_stays_in_separate_budgets(tmp_path: Path) -> None:
    """The key is the pair: `bravo 1.0` and `delta 1.0` are not one group."""
    for app in ("bravo", "delta"):
        for stamp in ("20260601T000000", "20260601T010000"):
            _archive(tmp_path, f"{app}_1.0_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=1)

    assert _names(plan.kept) == [
        "bravo_1.0_20260601T010000.ipa",
        "delta_1.0_20260601T010000.ipa",
    ]


def test_fewer_than_keep_archives_is_a_no_op(tmp_path: Path) -> None:
    _archive(tmp_path, "example_21.16.2_20260513T011421.ipa")
    _archive(tmp_path, "example_21.16.2_20260513T011455.ipa")

    plan = plan_prune(tmp_path, keep=5)

    assert plan.remove == []
    assert len(plan.kept) == 2


def test_recency_comes_from_the_filename_not_mtime(tmp_path: Path) -> None:
    """A restore or an rsync rewrites mtime; the filename stamp is immutable."""
    old = _archive(tmp_path, "bravo_1.0_20260601T000000.ipa")
    new = _archive(tmp_path, "bravo_1.0_20260601T020000.ipa")
    # Make the *older* build look freshly touched, as a Trash restore would.
    import os

    os.utime(old, (2_000_000_000, 2_000_000_000))
    os.utime(new, (1_000_000_000, 1_000_000_000))

    plan = plan_prune(tmp_path, keep=1)

    assert _names(plan.kept) == [new.name]
    assert _names(plan.remove) == [old.name]


def test_excluded_paths_are_never_removed_and_buy_no_extra_slot(
    tmp_path: Path,
) -> None:
    """`keep: 0` still spares the archive the current build just produced."""
    current = _archive(tmp_path, "bravo_1.0_20260601T030000.ipa")
    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(tmp_path, f"bravo_1.0_{stamp}.ipa")

    plan = plan_prune(tmp_path, keep=0, exclude=[current])

    assert _names(plan.kept) == [current.name]
    assert len(plan.remove) == 2


def test_unparsed_files_are_reported_and_left_out_of_the_removal_set(
    tmp_path: Path,
) -> None:
    _archive(tmp_path, "bravo_1.0_20260601T000000.ipa")
    _archive(tmp_path, "bravo_1.0_20260601T010000.ipa")
    stranger = _archive(tmp_path, "hand-built.ipa")

    plan = plan_prune(tmp_path, keep=1)

    assert plan.unparsed == [stranger]
    assert stranger not in [a.path for a in plan.remove]


def test_bytes_removed_sums_only_the_doomed_files(tmp_path: Path) -> None:
    _archive(tmp_path, "bravo_1.0_20260601T000000.ipa", size=100)
    _archive(tmp_path, "bravo_1.0_20260601T010000.ipa", size=200)
    _archive(tmp_path, "bravo_1.0_20260601T020000.ipa", size=400)

    plan = plan_prune(tmp_path, keep=1)

    assert plan.bytes_removed == 300


def test_a_negative_keep_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep must be >= 0"):
        plan_prune(tmp_path, keep=-1)
