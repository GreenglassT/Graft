"""Comparing what the library holds against what is published.

The two failure directions are not symmetric. Missing an update is a tweak that
quietly stays old, which the user finds out about eventually. *Inventing* one —
offering to replace a build with an older or equal one — is an app that talks
you into downgrading, and this library legitimately holds versions no repo has
(anything compiled from source, and a `-jailed` variant alongside the plain
build of the same version).
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graft_ipa import updates
from graft_ipa.json_output import JsonEmitter
from graft_ipa.updates import Availability, TweakStatus


def _never_called(package_id: str) -> Availability | None:
    raise AssertionError(f"looked up {package_id!r} for a file with no package id")


def _status(installed: str | None, latest: str | None, *, pid: str = "com.x") -> TweakStatus:
    return TweakStatus(
        filename="t.deb",
        package_id=pid,
        installed=installed,
        available=Availability(latest, "canister") if latest else None,
    )


@pytest.mark.parametrize("installed,latest", [
    ("1.2.14", "1.3.2"),      # the case this feature was built for
    ("1.7.19", "1.7.20"),
    ("1.0", "1.0.1"),
    ("1.9", "1.10"),          # numeric runs compare as numbers, not text
    ("1.0~beta", "1.0"),      # a prerelease is older than the release
])
def test_a_newer_published_version_is_an_update(installed: str, latest: str):
    assert _status(installed, latest).update_available


@pytest.mark.parametrize("installed,latest", [
    ("1.3.2", "1.3.2"),       # equal is not an update
    ("1.3.2", "1.2.14"),      # the repo is behind: never offer a downgrade
    ("1.10", "1.9"),
    ("1.0", "1.0~beta"),
    ("1.1.8-jailed", "1.1.8"),  # a local variant of the same upstream version
])
def test_nothing_older_or_equal_is_offered(installed: str, latest: str):
    assert not _status(installed, latest).update_available


def test_a_tweak_nothing_publishes_is_not_an_update():
    """Anything built from source is in no index at all. That has to read as
    "nothing to say", not as an error and not as up-to-date-ness it can't know."""
    status = _status("1.0.0", None)
    assert not status.update_available
    assert updates.as_event(status)["latest"] is None


def test_a_deb_with_no_readable_package_id_is_still_listed():
    """A truncated download has no control stanza, so nothing can be looked up
    — but the file is in the library and the row still has to appear."""
    results = updates.statuses([{"filename": "junk.deb"}], {}, lookup=_never_called)
    assert len(results) == 1
    assert results[0].package_id is None
    assert not results[0].update_available


def test_the_repo_catalog_answers_before_the_network():
    """A cached index costs nothing; the network costs a round trip per id."""
    asked: list[str] = []

    def lookup(pid: str):
        asked.append(pid)
        return Availability("9.9", "canister")

    catalog = {"com.x": Availability("2.0", "https://repo.example")}
    results = updates.statuses(
        [{"filename": "a.deb", "package_id": "com.x", "version": "1.0"}], catalog, lookup=lookup
    )

    assert asked == [], "the network was consulted for a package already in a cached index"
    assert results[0].available == Availability("2.0", "https://repo.example")


def test_the_network_is_asked_once_per_id_not_once_per_file():
    """Two files can carry the same package — `1.1.8` and `1.1.8-jailed` sit in
    this library right now. That is one question, asked once."""
    asked: list[str] = []

    def lookup(pid: str):
        asked.append(pid)
        return Availability("2.0", "canister")

    records = [
        {"filename": "a.deb", "package_id": "com.x", "version": "1.1.8"},
        {"filename": "b.deb", "package_id": "com.x", "version": "1.1.8-jailed"},
    ]
    results = updates.statuses(records, {}, lookup=lookup)

    assert asked == ["com.x"]
    # Both files answered for separately: either could be the one attached.
    assert [r.filename for r in results] == ["a.deb", "b.deb"]
    assert results[0].update_available
    assert results[1].update_available


def test_a_lookup_that_fails_is_not_an_update_and_not_a_crash():
    """`canister_latest` swallows everything by contract; this pins that a
    None answer degrades to silence rather than propagating."""
    results = updates.statuses(
        [{"filename": "a.deb", "package_id": "com.x", "version": "1.0"}], {}, lookup=lambda _: None
    )
    assert not results[0].update_available


def test_the_repo_catalog_keeps_the_highest_version_across_repos(tmp_path: Path):
    stanza = "Package: com.x\nVersion: {v}\nFilename: d.deb\n\n"

    cfg = SimpleNamespace(
        state_dir=tmp_path,
        repos=[SimpleNamespace(url="https://a.example"), SimpleNamespace(url="https://b.example")],
    )

    from graft_ipa import repo as repo_mod
    (tmp_path / "repos").mkdir()
    repo_mod.cache_path(tmp_path, "https://a.example").write_text(stanza.format(v="1.0"))
    repo_mod.cache_path(tmp_path, "https://b.example").write_text(stanza.format(v="2.0"))

    catalog = updates.repo_catalog(cfg)  # type: ignore[arg-type]
    assert catalog["com.x"].version == "2.0"
    assert catalog["com.x"].source == "https://b.example"
    # The download target comes from the same index entry the version did, so
    # the file fetched is the build whose number was compared.
    assert catalog["com.x"].download == "https://b.example/d.deb"


def test_a_repo_never_fetched_contributes_nothing(tmp_path: Path):
    """This runs behind a "check versions" button, not a "sync repos" one. A
    repo with no cache is skipped rather than downloaded on the spot."""
    cfg = SimpleNamespace(
        state_dir=tmp_path, repos=[SimpleNamespace(url="https://never-fetched.example")]
    )
    assert updates.repo_catalog(cfg) == {}  # type: ignore[arg-type]


def test_the_event_names_where_the_version_came_from():
    """The source is shown to the user, so it has to name somewhere they could
    go and look — not just "an update exists"."""
    event = updates.as_event(_status("1.0", "2.0"))
    assert event == {
        "filename": "t.deb",
        "package_id": "com.x",
        "installed": "1.0",
        "latest": "2.0",
        "source": "canister",
        "update_available": True,
        "unchecked": False,
    }


def _install(cfg, name: str) -> Path:
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.tweaks_dir / name
    path.write_bytes(b"x")
    return path


def test_updating_swaps_the_file_on_every_app_that_used_it(temp_config_env: Path, monkeypatch):
    """The point of the whole feature: after this, the apps build with the new
    one. A download that lands without re-pointing anything has changed a file
    on disk and nothing the user cares about."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, ["old.deb"])
    cfg = load_config(temp_config_env)

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": [app]}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(
        cli_mod.canister, "fetch_deb", lambda _t, dest, *a, **k: _install(cfg, "new.deb")
    )

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))
    event = json.loads(out.getvalue().splitlines()[-1])

    assert event["updated"] is True
    assert event["new_filename"] == "new.deb"
    assert event["apps"] == [app]
    assert load_config(temp_config_env).apps[app].tweaks == ["new.deb"], (
        "the app still builds with the old .deb, so the update changed nothing that matters"
    )
    # The old file stays: it is the only way back if the new build misbehaves.
    assert (cfg.tweaks_dir / "old.deb").exists()


def test_a_repo_that_reuses_the_filename_is_not_detached_and_reattached(
    temp_config_env: Path, monkeypatch
):
    """`a-tweak.deb` is a stable name — the download overwrites it in
    place and every app already points there. Detaching and re-attaching would
    be a no-op at best, and at worst the detach lands and the attach doesn't,
    silently dropping the tweak from the build."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_config_env)
    _install(cfg, "same.deb")
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, ["same.deb"])
    cfg = load_config(temp_config_env)

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "same.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": [app]}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "repo", "https://r/same.deb")
    )
    monkeypatch.setattr(
        cli_mod.canister, "fetch_deb", lambda _t, dest, *a, **k: _install(cfg, "same.deb")
    )

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "same.deb", emitter=JsonEmitter(stream=out))
    event = json.loads(out.getvalue().splitlines()[-1])

    assert event["replaced_in_place"] is True
    assert load_config(temp_config_env).apps[app].tweaks == ["same.deb"]


def test_updating_something_already_current_changes_nothing(temp_config_env: Path, monkeypatch):
    """A badge clicked after the library moved on. "Nothing newer" is an
    answer, not a failure — and nothing must be downloaded to deliver it."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "cur.deb")

    def _no_fetch(*_a, **_k):
        raise AssertionError("downloaded something for a tweak that was already current")

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "cur.deb", "package_id": "com.x",
                     "version": "2.0", "used_by": []}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(cli_mod.canister, "fetch_deb", _no_fetch)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "cur.deb", emitter=JsonEmitter(stream=out))
    event = json.loads(out.getvalue().splitlines()[-1])

    assert event["updated"] is False
    assert "newest" in event["reason"]


def test_updating_an_unknown_file_is_refused_by_name(temp_config_env: Path, monkeypatch):
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config
    from graft_ipa.errors import GraftError

    cfg = load_config(temp_config_env)
    monkeypatch.setattr(cli_mod, "_collect_tweak_records", lambda _c: [])
    with pytest.raises(GraftError) as excinfo:
        cli_mod._do_update_tweak(cfg, "ghost.deb")
    assert "ghost.deb" in str(excinfo.value)


def test_the_verb_emits_one_event_per_file_and_a_summary(temp_config_env: Path, monkeypatch):
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _cfg: [
            {"filename": "old.deb", "package_id": "com.x", "version": "1.0"},
            {"filename": "mine.deb", "package_id": "com.mine", "version": "1.0"},
        ],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _cfg: {})
    monkeypatch.setattr(
        updates, "canister_latest",
        lambda pid: Availability("2.0", "canister") if pid == "com.x" else None,
    )

    out = StringIO()
    cli_mod._do_tweak_updates(cfg, emitter=JsonEmitter(stream=out))
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    assert [e["type"] for e in events] == ["tweak-update", "tweak-update", "tweak-updates-end"]
    assert events[0]["update_available"] is True
    # Emitted even though nothing publishes it: "we looked and found nothing"
    # has to be distinguishable from "we never looked".
    assert events[1]["update_available"] is False
    assert events[1]["latest"] is None
    assert events[2] == {
        "type": "tweak-updates-end", "checked": 2, "outdated": 1, "unchecked": 0
    }


def test_the_swap_keeps_the_tweak_where_it_was_in_the_list(temp_config_env: Path, monkeypatch):
    """cyan injects in list order, so an update that moves a tweak to the end
    changes what gets built — silently, and only for people with several."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_config_env)
    for name in ("first.deb", "old.deb", "last.deb"):
        _install(cfg, name)
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, ["first.deb", "old.deb", "last.deb"])
    cfg = load_config(temp_config_env)

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": [app]}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(
        cli_mod.canister, "fetch_deb", lambda _t, dest, *a, **k: _install(cfg, "new.deb")
    )

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert load_config(temp_config_env).apps[app].tweaks == [
        "first.deb", "new.deb", "last.deb"
    ], "the updated tweak moved position, changing cyan's injection order"


# --- what the review found: each of these was a real way to mislead or break ---


def test_a_stanza_with_no_filename_does_not_kill_the_whole_catalog(tmp_path: Path):
    """`download_url` raises without a Filename, and one such stanza in any
    cached index aborted the entire check — taking down update checking for
    every tweak, including the ones canister answers and that never needed the
    repo at all. `_do_repo_packages` already filters these out."""
    from graft_ipa import repo as repo_mod

    (tmp_path / "repos").mkdir()
    repo_mod.cache_path(tmp_path, "https://a.example").write_text(
        "Package: com.virtual\nVersion: 1.0\n\n"      # no Filename — a meta package
        "Package: com.real\nVersion: 2.0\nFilename: r.deb\n\n"
    )
    cfg = SimpleNamespace(state_dir=tmp_path, repos=[SimpleNamespace(url="https://a.example")])

    catalog = updates.repo_catalog(cfg)  # type: ignore[arg-type]
    assert "com.real" in catalog, "one unusable stanza discarded the whole index"
    assert "com.virtual" not in catalog


def test_a_repo_url_with_a_trailing_slash_still_finds_its_cache(tmp_path: Path):
    """Every writer hashes the *normalized* URL, and `cache_path` is a SHA-1 of
    whatever string it is handed. Reading under the raw config value meant a
    trailing slash silently found nothing and the repo contributed nothing —
    swallowed by `except OSError`."""
    from graft_ipa import repo as repo_mod

    (tmp_path / "repos").mkdir()
    repo_mod.cache_path(tmp_path, repo_mod.normalize_url("https://a.example/")).write_text(
        "Package: com.x\nVersion: 2.0\nFilename: d.deb\n\n"
    )
    cfg = SimpleNamespace(state_dir=tmp_path, repos=[SimpleNamespace(url="https://a.example/")])

    assert updates.repo_catalog(cfg)["com.x"].version == "2.0"  # type: ignore[arg-type]


def test_a_stale_cache_does_not_answer_ahead_of_the_network(tmp_path: Path):
    """The cache is refreshed only when the user runs --repo-packages. Left
    unbounded it pins `latest` below the live figure and reports a tweak as up
    to date on evidence it knows is months old."""
    import os

    from graft_ipa import repo as repo_mod

    (tmp_path / "repos").mkdir()
    cached = repo_mod.cache_path(tmp_path, "https://a.example")
    cached.write_text("Package: com.x\nVersion: 1.0\nFilename: d.deb\n\n")
    old = cached.stat().st_mtime - (7 * 24 * 60 * 60)
    os.utime(cached, (old, old))
    cfg = SimpleNamespace(state_dir=tmp_path, repos=[SimpleNamespace(url="https://a.example")])

    assert updates.repo_catalog(cfg) == {}, "a week-old index answered as current"  # type: ignore[arg-type]
    # Fresh again with a wide enough window, so the bound is what excluded it.
    assert updates.repo_catalog(cfg, max_age=30 * 24 * 60 * 60)["com.x"].version == "1.0"  # type: ignore[arg-type]


def test_the_repo_answer_carries_the_hash_it_published(tmp_path: Path):
    """`fetch_deb` can only verify a URL target against a hash the caller hands
    it, so without this the repo-sourced download was the one fetch path that
    wrote an unverified .deb and then re-pointed every app at it."""
    from graft_ipa import repo as repo_mod

    (tmp_path / "repos").mkdir()
    repo_mod.cache_path(tmp_path, "https://a.example").write_text(
        "Package: com.x\nVersion: 2.0\nFilename: d.deb\nSHA256: abc123\n\n"
    )
    cfg = SimpleNamespace(state_dir=tmp_path, repos=[SimpleNamespace(url="https://a.example")])

    assert updates.repo_catalog(cfg)["com.x"].sha256 == "abc123"  # type: ignore[arg-type]


def test_an_unreachable_source_is_not_reported_as_up_to_date():
    """The one claim this feature must never make without evidence. Offline,
    every lookup returns nothing and `outdated` is 0 — which reads exactly like
    having checked and being sure."""
    def unreachable(_pid):
        raise updates.LookupUnavailable("connection timed out")

    results = updates.statuses(
        [{"filename": "a.deb", "package_id": "com.x", "version": "1.0"}], {}, lookup=unreachable
    )
    assert results[0].unchecked is True
    assert not results[0].update_available
    assert updates.as_event(results[0])["unchecked"] is True


def test_a_package_nothing_publishes_is_checked_not_unchecked():
    """A 404 is an ordinary answer for anything built from source. It must not
    look like a failed lookup, or the pane would nag about the network."""
    results = updates.statuses(
        [{"filename": "mine.deb", "package_id": "com.mine", "version": "1.0"}],
        {}, lookup=lambda _p: None
    )
    assert results[0].unchecked is False
    assert results[0].available is None


def test_the_swap_collapses_a_duplicate_that_comes_after_the_old_entry(
    temp_config_env: Path, monkeypatch
):
    """An app can list both builds — the user fetched the new one through
    Canister and attached it alongside. Collapsing only against entries already
    emitted appended the new name twice, so cyan got the same .deb twice; the
    outcome depended purely on which order the two sat in."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_config_env)
    for name in ("old.deb", "new.deb"):
        _install(cfg, name)
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, ["old.deb", "new.deb"])
    cfg = load_config(temp_config_env)

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": [app]}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(
        cli_mod.canister, "fetch_deb", lambda _t, dest, *a, **k: _install(cfg, "new.deb")
    )

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert load_config(temp_config_env).apps[app].tweaks == ["new.deb"], (
        "the app lists the same tweak twice, so cyan injects it twice"
    )


def test_an_in_place_replacement_still_leaves_a_rollback(temp_config_env: Path, monkeypatch):
    """The docstring promises the old build survives so a bad update can be
    undone. A repo serving a stable filename overwrites it — precisely the case
    the promise is for."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    target = _install(cfg, "same.deb")
    target.write_bytes(b"the-old-build")

    def _overwrite(_t, _dest, *_a, **_k):
        target.write_bytes(b"the-new-build")
        return target

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "same.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": []}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "repo", "https://r/same.deb")
    )
    monkeypatch.setattr(cli_mod.canister, "fetch_deb", _overwrite)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "same.deb", emitter=JsonEmitter(stream=out))
    event = json.loads(out.getvalue().splitlines()[-1])

    assert event["replaced_in_place"] is True
    rollback = Path(event["rollback"])
    assert rollback.read_bytes() == b"the-old-build", "the previous build is unrecoverable"
    # And not in the library, where it would list as a second tweak.
    assert rollback.parent != cfg.tweaks_dir


def test_a_write_that_fails_partway_reports_which_apps_changed(
    temp_config_env: Path, monkeypatch
):
    """The download landed and some configs are already rewritten. Calling that
    "the update failed" describes a state nobody is in."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config
    from graft_ipa.errors import ConfigError

    # Two apps, so there is a "partway" to fail at.
    raw = yaml.safe_load(temp_config_env.read_text())
    first = next(iter(raw["apps"]))
    raw["apps"]["second"] = dict(raw["apps"][first])
    temp_config_env.write_text(yaml.safe_dump(raw))

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    apps = [first, "second"]

    calls: list[str] = []

    def _fail_on_second(path, app_name, tweaks):
        calls.append(app_name)
        if len(calls) > 1:
            raise ConfigError("config.yaml changed underneath us")

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": apps}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(
        cli_mod.canister, "fetch_deb", lambda _t, dest, *a, **k: _install(cfg, "new.deb")
    )
    monkeypatch.setattr(cli_mod, "set_app_tweaks", _fail_on_second)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))
    event = json.loads(out.getvalue().splitlines()[-1])

    assert event["updated"] is True, "the download landed; this is not a failed update"
    assert event["apps"] == [apps[0]], "the event must name what actually changed"
    assert "changed underneath" in event["error"]


def test_the_repo_published_hash_reaches_the_download(temp_config_env: Path, monkeypatch):
    """`fetch_deb` verifies a URL target only against a hash the caller hands
    it. Without forwarding it, this was the one fetch path in the app that
    wrote an unverified .deb and then re-pointed every app at it."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    seen: dict = {}

    def _capture(target, dest, expected_sha=None, *_a, **_k):
        seen["target"] = target
        seen["sha"] = expected_sha
        return _install(cfg, "new.deb")

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": []}],
    )
    monkeypatch.setattr(
        updates, "repo_catalog",
        lambda _c: {"com.x": Availability("2.0", "https://r", "https://r/new.deb", "deadbeef")},
    )
    monkeypatch.setattr(cli_mod.canister, "fetch_deb", _capture)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert seen["target"] == "https://r/new.deb"
    assert seen["sha"] == "deadbeef", "the download was not verified against the published hash"


def test_a_failed_download_leaves_no_rollback_behind(temp_config_env: Path, monkeypatch):
    """The copy is only a rollback if something replaced the original. A
    published version whose .deb 404s is an ordinary upstream state — seen for
    real on sometweak 1.7.20 — so without this, state/ collects one orphan per
    failed attempt."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config
    from graft_ipa.errors import GraftError

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")

    def _404(*_a, **_k):
        raise GraftError("download failed: 404 Client Error")

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": []}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(cli_mod.canister, "fetch_deb", _404)

    with pytest.raises(GraftError):
        cli_mod._do_update_tweak(cfg, "old.deb")

    rollback_dir = cfg.state_dir / "tweak-rollback"
    assert not (rollback_dir / "old.deb").exists(), (
        "a failed update left a rollback copy for a swap that never happened"
    )


@pytest.mark.parametrize("transport,expected", [
    ("network", "Wi-Fi"),
    ("usb", "USB"),
])
def test_the_push_names_the_transport_it_actually_used(transport: str, expected: str):
    """`select_device` resolves this and stage 5 used to discard it, printing
    "over USB" while pushing 117 MB to a phone with no cable attached. A build
    log that misdescribes what it did is the thing this tool exists to be."""
    from graft_ipa import cli as cli_mod

    assert cli_mod._transport_label(transport) == expected


def test_the_download_is_pinned_to_the_version_the_status_reported(
    temp_config_env: Path, monkeypatch
):
    """Real repro (sometweak, 2026-11-17): the status said 1.7.24 and the download
    resolved the package id a second time and came back with 1.7.20. The
    version that was compared is the version that must be fetched."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    seen: dict = {}

    def _capture(target, dest, expected_sha=None, **kwargs):
        seen.update(target=target, **kwargs)
        return _install(cfg, "new.deb")

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": []}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})
    monkeypatch.setattr(
        updates, "canister_latest", lambda _p: Availability("2.0", "canister", "com.x")
    )
    monkeypatch.setattr(cli_mod.canister, "fetch_deb", _capture)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert seen["target"] == "com.x"
    assert seen["version"] == "2.0"


def test_a_missing_build_leaves_config_and_tweak_untouched_and_names_the_urls(
    temp_config_env: Path, monkeypatch
):
    """End to end through the real `fetch_deb` with a repo that 404s every
    variant of the reported version: the failure names what was wanted and what
    404'd, and nothing on disk or in config.yaml changed."""
    from unittest.mock import MagicMock, patch

    import requests

    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, set_app_tweaks
    from graft_ipa.errors import GraftError

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, ["old.deb"])
    config_before = cfg.path.read_text()
    cfg = load_config(temp_config_env)

    def _row(version, arch):
        return {
            "package_id": "com.x", "version": version, "repository_id": "r",
            "package_filename": f"./debs/com.x_{version}_{arch}.deb",
        }

    def fake_http(url, *a, **k):
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        if "/jailbreak/package/" in url:
            # Stale 1.0 first, as canister really lists them.
            resp.json.return_value = {"data": [
                _row("1.0", "iphoneos-arm64"), _row("2.0", "iphoneos-arm64"),
                _row("2.0", "iphoneos-arm"),
            ]}
        elif "/jailbreak/repository/" in url:
            resp.json.return_value = {"data": {"id": "r", "uri": "https://r.example"}}
        else:
            resp.status_code = 404
            resp.raise_for_status.side_effect = requests.HTTPError("404", response=resp)
        return resp

    monkeypatch.setattr(
        cli_mod, "_collect_tweak_records",
        lambda _c: [{"filename": "old.deb", "package_id": "com.x",
                     "version": "1.0", "used_by": [app]}],
    )
    monkeypatch.setattr(updates, "repo_catalog", lambda _c: {})

    with patch("requests.get", side_effect=fake_http):
        with pytest.raises(GraftError) as exc:
            cli_mod._do_update_tweak(cfg, "old.deb")

    assert "2.0" in exc.value.message
    assert "com.x_2.0_iphoneos-arm64.deb" in exc.value.message
    assert "com.x_2.0_iphoneos-arm.deb" in exc.value.message
    assert cfg.path.read_text() == config_before
    assert [p.name for p in cfg.tweaks_dir.iterdir()] == ["old.deb"]
