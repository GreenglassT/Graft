"""Comparing what the library holds against what is published.

The two failure directions are not symmetric. Missing an update is a tweak that
quietly stays old, which the user finds out about eventually. *Inventing* one —
offering to replace a build with an older or equal one — is an app that talks
you into downgrading, and this library legitimately holds versions no repo has
(anything compiled from source, and a `-jailed` variant alongside the plain
build of the same version).

Since 2.3.1 an update comes only from the tweak's own source (A1): the source
logic itself is pinned in `test_tweak_update_source.py`; these pin the
indexes it reads and `--tweak-updates` / `--update-tweak` around it.
"""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graft_ipa import canister, tweak_sources, updates
from graft_ipa.json_output import JsonEmitter
from graft_ipa.tweak_sources import Source
from graft_ipa.updates import Availability, Download, Offer, TweakStatus

from .test_tweak_source_records import write_deb

ORIGIN = Source(tweak_sources.CANISTER, "r", "https://r.example")


def _status(installed: str | None, latest: str | None, *, pid: str = "com.x") -> TweakStatus:
    return TweakStatus(
        filename="t.deb",
        package_id=pid,
        installed=installed,
        available=Availability(latest, ORIGIN, verified=True) if latest else None,
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


def test_a_deb_with_no_readable_package_id_is_still_listed(tmp_path: Path, monkeypatch):
    """A truncated download has no control stanza, so nothing can be looked up
    — but the file is in the library and the row still has to appear."""
    def never(pid, *_a):
        raise AssertionError(f"looked up {pid!r} for a file with no package id")

    monkeypatch.setattr(updates, "canister_offers", never)
    cfg = SimpleNamespace(state_dir=tmp_path, tweaks_dir=tmp_path, repos=[])
    results = updates.statuses(cfg, [{"filename": "junk.deb"}])
    assert len(results) == 1
    assert results[0].package_id is None
    assert not results[0].update_available


# --- the configured repos' cached indexes (`repo_offers`) ------------------------


def _repos(tmp_path: Path, *urls: str) -> SimpleNamespace:
    return SimpleNamespace(state_dir=tmp_path, repos=[SimpleNamespace(url=u) for u in urls])


def _cache(tmp_path: Path, url: str, text: str) -> Path:
    from graft_ipa import repo as repo_mod

    path = repo_mod.cache_path(tmp_path, repo_mod.normalize_url(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_each_repo_answers_for_itself(tmp_path: Path):
    """Not one "highest version across repos", which let any repo publishing
    the same package id win: an offer per repo, in config order."""
    stanza = "Package: com.x\nVersion: {v}\nFilename: d.deb\n\n"
    _cache(tmp_path, "https://a.example", stanza.format(v="1.0"))
    _cache(tmp_path, "https://b.example", stanza.format(v="2.0"))

    offers = updates.repo_offers(_repos(tmp_path, "https://a.example", "https://b.example"))  # type: ignore[arg-type]

    assert [(o.source.url, o.version) for o in offers["com.x"]] == [
        ("https://a.example", "1.0"), ("https://b.example", "2.0"),
    ]
    # The download comes from the same index entry the version did, so the
    # file fetched is the build whose number was compared.
    assert offers["com.x"][1].downloads == (Download("https://b.example/d.deb", None),)


def test_a_repo_lists_every_hash_it_has_for_a_package(tmp_path: Path):
    """What a library file's own hash is matched against: an older build's
    too, since that's the one the library holds."""
    _cache(tmp_path, "https://a.example",
           "Package: com.x\nVersion: 1.0\nFilename: a.deb\nSHA256: AA11\n\n"
           "Package: com.x\nVersion: 2.0\nFilename: b.deb\nSHA256: bb22\n\n")

    (offer,) = updates.repo_offers(_repos(tmp_path, "https://a.example"))["com.x"]  # type: ignore[arg-type]

    assert offer.hashes == {"aa11", "bb22"}
    assert offer.version == "2.0" and offer.downloads == (Download("https://a.example/b.deb", "bb22"),)


def test_a_repo_never_fetched_contributes_nothing(tmp_path: Path):
    """This runs behind a "check versions" button, not a "sync repos" one. A
    repo with no cache is skipped rather than downloaded on the spot."""
    assert updates.repo_offers(_repos(tmp_path, "https://never-fetched.example")) == {}  # type: ignore[arg-type]


def test_a_stanza_with_no_filename_does_not_kill_the_whole_index(tmp_path: Path):
    """`download_url` raises without a Filename, and one such stanza in any
    cached index aborted the entire check."""
    _cache(tmp_path, "https://a.example",
           "Package: com.virtual\nVersion: 1.0\n\n"      # no Filename — a meta package
           "Package: com.real\nVersion: 2.0\nFilename: r.deb\n\n")

    offers = updates.repo_offers(_repos(tmp_path, "https://a.example"))  # type: ignore[arg-type]

    assert offers["com.real"][0].version == "2.0", "one unusable stanza discarded the whole index"
    assert offers["com.virtual"][0].version is None, "never offered: nothing could download it"


def test_a_repo_url_with_a_trailing_slash_still_finds_its_cache(tmp_path: Path):
    """Every writer hashes the *normalized* URL, and `cache_path` is a SHA-1 of
    whatever string it is handed."""
    _cache(tmp_path, "https://a.example", "Package: com.x\nVersion: 2.0\nFilename: d.deb\n\n")
    offers = updates.repo_offers(_repos(tmp_path, "https://a.example/"))  # type: ignore[arg-type]
    assert offers["com.x"][0].version == "2.0"


def test_a_stale_cache_is_marked_stale(tmp_path: Path):
    """The cache is refreshed only when the user runs --repo-packages. Left
    unbounded it pins `latest` below the live figure and reports a tweak as up
    to date on evidence it knows is months old."""
    cached = _cache(tmp_path, "https://a.example", "Package: com.x\nVersion: 1.0\nFilename: d.deb\n\n")
    old = cached.stat().st_mtime - (7 * 24 * 60 * 60)
    os.utime(cached, (old, old))
    cfg = _repos(tmp_path, "https://a.example")

    assert not updates.repo_offers(cfg)["com.x"][0].fresh, "a week-old index answered as current"  # type: ignore[arg-type]
    assert updates.repo_offers(cfg, max_age=30 * 24 * 60 * 60)["com.x"][0].fresh  # type: ignore[arg-type]


# --- `--tweak-updates` and `--update-tweak` ---------------------------------------


def _install(cfg, name: str, version: str = "1.0", *, pid: str = "com.x") -> Path:
    return write_deb(cfg.tweaks_dir / name, pid, version)


def _from_source(cfg, name: str) -> None:
    """`name` was fetched from canister repository `r` (`ORIGIN`)."""
    path = cfg.tweaks_dir / name
    tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, name, ORIGIN, tweak_sources.file_sha256(path))


def _publish(monkeypatch, version: str = "2.0", *, sha256: str | None = "0" * 64,
             url: str = "https://r.example", pid: str = "com.x") -> str:
    """canister repository `r` publishes `pid` at `version`; returns its URL."""
    build = f"{url}/debs/{pid}_{version}.deb"
    origin = Source(tweak_sources.CANISTER, "r", url)

    def offers(package_id, *_a):
        if package_id != pid:
            return []
        return [Offer(origin, version, (Download(build, sha256),))]

    monkeypatch.setattr(updates, "repo_offers", lambda _c, max_age=0: {})
    monkeypatch.setattr(updates, "canister_offers", offers)
    return build


def _serves(monkeypatch, *, pid: str = "com.x", version: str = "2.0", seen: list | None = None,
            name: str = "download.deb") -> None:
    """The download writes a real .deb of `pid` at `version`, named `name` by
    the repo."""
    def fetch(target, dest, expected_sha=None, **kwargs):
        if seen is not None:
            seen.append({"target": target, "sha": expected_sha, **kwargs})
        return write_deb(dest / name, pid, version)

    monkeypatch.setattr(canister, "fetch_deb", fetch)


NEW = "com.x_2.0.deb"


def _one_app(temp_config_env: Path, tweaks: list[str]) -> tuple[object, str]:
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_config_env)
    app = next(iter(cfg.apps))
    set_app_tweaks(cfg.path, app, tweaks)
    return load_config(temp_config_env), app


def _last_event(out: StringIO) -> dict:
    return json.loads(out.getvalue().splitlines()[-1])


def test_updating_swaps_the_file_on_every_app_that_used_it(temp_config_env: Path, monkeypatch):
    """The point of the whole feature: after this, the apps build with the new
    one. A download that lands without re-pointing anything has changed a file
    on disk and nothing the user cares about."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config, load_config as _reload

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    cfg, app = _one_app(temp_config_env, ["old.deb"])
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))
    event = _last_event(out)

    assert event["updated"] is True
    assert event["new_filename"] == NEW, "Graft names the file, not the repo"
    assert event["apps"] == [app]
    assert _reload(temp_config_env).apps[app].tweaks == [NEW], (
        "the app still builds with the old .deb, so the update changed nothing that matters"
    )
    # The old file stays: it is the only way back if the new build misbehaves.
    assert (cfg.tweaks_dir / "old.deb").exists()
    # And the new one's source is on record for its next update.
    assert tweak_sources.recorded_for(cfg.state_dir, NEW, tweak_sources.file_sha256(cfg.tweaks_dir / NEW)) == ORIGIN


def test_a_download_named_like_another_tweak_does_not_overwrite_it(temp_config_env: Path, monkeypatch):
    """2.3.1 A2, in `--update-tweak`: the repo's filename is never the
    library's, so a download called `b.deb` can't replace the `b.deb` another
    app builds with."""
    from graft_ipa import cli as cli_mod

    cfg, _app = _one_app(temp_config_env, [])
    _install(cfg, "old.deb")
    other = _install(cfg, "b.deb", "5.0", pid="com.b")
    theirs = other.read_bytes()
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch, name="b.deb")

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))

    assert other.read_bytes() == theirs
    assert _last_event(out)["new_filename"] == NEW


def test_a_different_file_under_graft_s_name_is_refused(temp_config_env: Path, monkeypatch):
    from graft_ipa import cli as cli_mod
    from graft_ipa.tweak_sources import TweakFileError

    cfg, app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    taken = write_deb(cfg.tweaks_dir / NEW, "com.x", "2.0", salt="someone else's")
    there = taken.read_bytes()
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch)
    config_before = cfg.path.read_text()

    with pytest.raises(TweakFileError, match="already in your tweaks library"):
        cli_mod._do_update_tweak(cfg, "old.deb")

    assert taken.read_bytes() == there
    assert cfg.path.read_text() == config_before
    assert not (cfg.state_dir / "tweak-rollback" / "old.deb").exists()


def test_a_download_that_isnt_a_deb_is_refused(temp_config_env: Path, monkeypatch):
    from graft_ipa import cli as cli_mod
    from graft_ipa.tweak_sources import TweakFileError

    cfg, _app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)

    def html(_t, dest, *_a, **_k):
        (dest / "x.deb").write_bytes(b"<html>gone</html>")
        return dest / "x.deb"

    monkeypatch.setattr(canister, "fetch_deb", html)

    with pytest.raises(TweakFileError, match=r"isn't a \.deb"):
        cli_mod._do_update_tweak(cfg, "old.deb")

    assert sorted(p.name for p in cfg.tweaks_dir.iterdir() if not p.name.startswith(".")) == ["old.deb"]


def _key(cfg, filename: str) -> str:
    """The tweak's `confirm_key` as `--tweak-updates` reports it now."""
    from graft_ipa import cli as cli_mod

    record = next(r for r in cli_mod._collect_tweak_records(cfg) if r["filename"] == filename)
    key = updates.statuses(cfg, [record])[0].confirm_key
    assert key is not None
    return key


def test_an_unverified_update_needs_trust_tweak(temp_config_env: Path, monkeypatch):
    """2.3.1 A1: an update from a source that isn't verified (here, no
    record and no index lists this file) runs only on the person's OK, for
    exactly what they were shown (its `confirm_key`)."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.tweak_sources import UntrustedSourceError

    cfg, app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _publish(monkeypatch)
    seen: list = []
    _serves(monkeypatch, seen=seen)
    key = _key(cfg, "old.deb")

    with pytest.raises(UntrustedSourceError) as refused:
        cli_mod._do_update_tweak(cfg, "old.deb")
    assert "source unknown: https://r.example" in refused.value.message
    assert f"--trust-tweak {key}" in (refused.value.hint or "")
    assert seen == [], "nothing was downloaded"

    cli_mod._do_update_tweak(cfg, "old.deb", trusted_key=key)

    from graft_ipa.config import load_config
    assert load_config(temp_config_env).apps[app].tweaks == [NEW]
    assert tweak_sources.recorded_for(cfg.state_dir, NEW, tweak_sources.file_sha256(cfg.tweaks_dir / NEW)) == ORIGIN


def test_an_ok_for_another_version_is_refused(temp_config_env: Path, monkeypatch):
    """Re-verify #2: the row showed 2.0; by the press, the source offers
    9.9. The yes was for 2.0, so nothing is downloaded."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.tweak_sources import UntrustedSourceError

    cfg, _app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _publish(monkeypatch, "2.0")
    key = _key(cfg, "old.deb")
    _publish(monkeypatch, "9.9")
    seen: list = []
    _serves(monkeypatch, seen=seen, version="9.9")

    with pytest.raises(UntrustedSourceError, match="changed since you confirmed it"):
        cli_mod._do_update_tweak(cfg, "old.deb", trusted_key=key)
    assert seen == []


def test_an_ok_from_another_source_is_refused(temp_config_env: Path, monkeypatch):
    from graft_ipa import cli as cli_mod
    from graft_ipa.tweak_sources import UntrustedSourceError

    cfg, _app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _publish(monkeypatch, "2.0")
    key = _key(cfg, "old.deb")
    _publish(monkeypatch, "2.0", url="https://evil.example")

    with pytest.raises(UntrustedSourceError, match="changed since you confirmed it"):
        cli_mod._do_update_tweak(cfg, "old.deb", trusted_key=key)


@pytest.mark.parametrize("given", ["b.deb@" + "0" * 64, "a.deb", "a.deb@nothex"])
def test_trust_tweak_is_the_confirm_key_of_the_tweak_being_updated(temp_config_env: Path, given: str):
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, [
        "--config", str(temp_config_env), "--update-tweak", "a.deb", "--trust-tweak", given,
    ])
    assert result.exit_code == 2
    assert "confirm_key" in result.output


def test_the_key_changes_with_anything_the_person_was_shown(tmp_path: Path):
    """The file, the source, the version, and each build and its hash."""
    good = Source(tweak_sources.CANISTER, "r", "https://r.example")
    builds = (Download("https://r.example/a.deb", "ab" * 32),)

    def key(**kw) -> str:
        args = {"version": "2.0", "origin": good, "downloads": builds} | kw
        return updates.confirm_key("a.deb", Availability(args["version"], args["origin"], args["downloads"]))

    base = key()
    assert base == key(), "the same update, the same key"
    assert base.startswith("a.deb@") and len(base) == len("a.deb@") + 64
    assert len({base, key(version="2.1"), key(origin=Source(tweak_sources.CANISTER, "r", "https://evil.example")),
                key(origin=Source(tweak_sources.CANISTER, "s", "https://r.example")),
                key(downloads=(Download("https://r.example/a.deb", "cd" * 32),)),
                key(downloads=(Download("https://r.example/b.deb", "ab" * 32),)),
                updates.confirm_key("b.deb", Availability("2.0", good, builds))}) == 7


def test_updating_something_already_current_changes_nothing(temp_config_env: Path, monkeypatch):
    """A badge clicked after the library moved on. "Nothing newer" is an
    answer, not a failure — and nothing must be downloaded to deliver it."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "cur.deb", "2.0")
    _from_source(cfg, "cur.deb")
    _publish(monkeypatch, "2.0")

    def _no_fetch(*_a, **_k):
        raise AssertionError("downloaded something for a tweak that was already current")

    monkeypatch.setattr(canister, "fetch_deb", _no_fetch)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "cur.deb", emitter=JsonEmitter(stream=out))
    event = _last_event(out)

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
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    _install(cfg, "mine.deb", pid="com.mine")
    _publish(monkeypatch)

    out = StringIO()
    cli_mod._do_tweak_updates(cfg, emitter=JsonEmitter(stream=out))
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    by_file = {e.get("filename"): e for e in events}
    assert [e["type"] for e in events] == ["tweak-update", "tweak-update", "tweak-updates-end"]
    assert by_file["old.deb"]["update_available"] is True
    assert by_file["old.deb"]["verified"] is True and by_file["old.deb"]["source"] == "https://r.example"
    # Emitted even though nothing publishes it: "we looked and found nothing"
    # has to be distinguishable from "we never looked".
    assert by_file["mine.deb"]["update_available"] is False
    assert by_file["mine.deb"]["latest"] is None
    assert events[-1] == {
        "type": "tweak-updates-end", "checked": 2, "outdated": 1, "unchecked": 0
    }


def test_the_swap_keeps_the_tweak_where_it_was_in_the_list(temp_config_env: Path, monkeypatch):
    """cyan injects in list order, so an update that moves a tweak to the end
    changes what gets built — silently, and only for people with several."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "first.deb", pid="com.first")
    _install(cfg, "old.deb")
    _install(cfg, "last.deb", pid="com.last")
    cfg, app = _one_app(temp_config_env, ["first.deb", "old.deb", "last.deb"])
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert load_config(temp_config_env).apps[app].tweaks == [
        "first.deb", NEW, "last.deb"
    ], "the updated tweak moved position, changing cyan's injection order"


def test_the_swap_collapses_a_duplicate_that_comes_after_the_old_entry(
    temp_config_env: Path, monkeypatch
):
    """An app can list both builds — the user fetched the new one through
    Canister and attached it alongside. The update reuses that file (the same
    bytes) and the app lists it once, not twice."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    fetched = _install(cfg, NEW, "2.0")
    cfg, app = _one_app(temp_config_env, ["old.deb", NEW])
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)

    def same(_t, dest, *_a, **_k):
        (dest / "d.deb").write_bytes(fetched.read_bytes())
        return dest / "d.deb"

    monkeypatch.setattr(canister, "fetch_deb", same)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert load_config(temp_config_env).apps[app].tweaks == [NEW], (
        "the app lists the same tweak twice, so cyan injects it twice"
    )


def test_an_update_leaves_a_rollback(temp_config_env: Path, monkeypatch):
    """The docstring promises the old build survives so a bad update can be
    undone, outside the library, where it would list as a second tweak."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    old = _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))
    event = _last_event(out)

    rollback = Path(event["rollback"])
    assert rollback.read_bytes() == old.read_bytes()
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
    raw["apps"][first]["tweaks"] = ["old.deb"]
    raw["apps"]["second"] = dict(raw["apps"][first])
    temp_config_env.write_text(yaml.safe_dump(raw))

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)
    _serves(monkeypatch)
    apps = [first, "second"]

    calls: list[str] = []

    def _fail_on_second(path, app_name, tweaks):
        calls.append(app_name)
        if len(calls) > 1:
            raise ConfigError("config.yaml changed underneath us")

    monkeypatch.setattr(cli_mod, "set_app_tweaks", _fail_on_second)

    out = StringIO()
    cli_mod._do_update_tweak(cfg, "old.deb", emitter=JsonEmitter(stream=out))
    event = _last_event(out)

    assert event["updated"] is True, "the download landed; this is not a failed update"
    assert event["apps"] == [apps[0]], "the event must name what actually changed"
    assert "changed underneath" in event["error"]


def test_exactly_the_compared_build_is_downloaded_against_its_hash(temp_config_env: Path, monkeypatch):
    """Real repro (sometweak, 2026-11-17): the status said 1.7.24 and the
    download resolved the package id a second time and came back with 1.7.20.
    The build compared, from the source shown, is the one fetched, checked
    against its own published hash."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    build = _publish(monkeypatch, sha256="deadbeef")
    seen: list = []
    _serves(monkeypatch, seen=seen)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert [(s["target"], s["sha"]) for s in seen] == [(build, "deadbeef")]


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
    _from_source(cfg, "old.deb")
    _publish(monkeypatch)

    def _404(*_a, **_k):
        raise GraftError("download failed: 404 Client Error")

    monkeypatch.setattr(canister, "fetch_deb", _404)

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


def test_a_missing_build_leaves_config_and_tweak_untouched_and_names_the_urls(
    temp_config_env: Path, monkeypatch
):
    """End to end through the real `canister_offers` and `fetch_deb` with a
    repo that 404s every variant of the reported version: the failure names
    what was wanted and what 404'd, and nothing on disk or in config.yaml
    changed."""
    from unittest.mock import MagicMock, patch

    import requests

    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    cfg, _app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    config_before = cfg.path.read_text()

    def _row(version, arch):
        return {
            "package_id": "com.x", "version": version, "repository_id": "r",
            "package_filename": f"./debs/com.x_{version}_{arch}.deb", "sha256_hash": "ab" * 32,
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

    monkeypatch.setattr(updates, "repo_offers", lambda _c, max_age=0: {})

    with patch("requests.get", side_effect=fake_http):
        with pytest.raises(GraftError) as exc:
            cli_mod._do_update_tweak(cfg, "old.deb")

    assert "2.0" in exc.value.message
    assert "com.x_2.0_iphoneos-arm64.deb" in exc.value.message
    assert "com.x_2.0_iphoneos-arm.deb" in exc.value.message
    assert cfg.path.read_text() == config_before
    assert sorted(p.name for p in cfg.tweaks_dir.iterdir() if not p.name.startswith(".")) == ["old.deb"]


def test_update_tweak_never_takes_a_higher_version_from_another_repository(
    temp_config_env: Path, monkeypatch
):
    """2.3.1 A1 end to end, through the real `canister_offers`: canister lists
    the tweak from its own repository (`r`, 2.0) and from another (9.9). The
    download is `r`'s, and the app builds with 2.0."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    cfg, app = _one_app(temp_config_env, ["old.deb"])
    _install(cfg, "old.deb")
    _from_source(cfg, "old.deb")
    rows = [("r", "1.0"), ("r", "2.0"), ("evil", "9.9")]
    monkeypatch.setattr(canister, "get_packages", lambda _pid: [
        canister.Package("com.x", "x", v, "", "", "", repo_id, f"debs/com.x_{v}.deb", None, "ab" * 32,
                         "", None) for repo_id, v in rows
    ])
    uris = {"r": "https://r.example", "evil": "https://evil.example"}
    monkeypatch.setattr(canister, "get_repository",
                        lambda repo_id: canister.Repository(repo_id, repo_id, uris[repo_id]))
    monkeypatch.setattr(updates, "repo_offers", lambda _c, max_age=0: {})
    seen: list = []
    _serves(monkeypatch, seen=seen)

    cli_mod._do_update_tweak(cfg, "old.deb")

    assert [s["target"] for s in seen] == ["https://r.example/debs/com.x_2.0.deb"]
    assert load_config(temp_config_env).apps[app].tweaks == [NEW]
