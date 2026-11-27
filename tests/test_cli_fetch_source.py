"""`--fetch` puts a tweak in the library under Graft's name, and records where
it came from, so its updates come only from there (2.3.1 A1, A2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import canister, tweak_sources
from graft_ipa import cli as cli_mod
from graft_ipa.config import add_repo, load_config
from graft_ipa.tweak_sources import Source

from .test_tweak_source_records import write_deb


def _serving(monkeypatch, tmp_path: Path, package: str, version: str, *,
             row: tuple[str, str] | None = None, name: str = "download.deb", seen: list | None = None):
    """`canister.fetch_deb` writes a real .deb, and (for a package id) says it
    took `row`: (repository id, its URI)."""
    def fetch(target, dest, expected_sha=None, *, version_=None, on_pick=None, **_k):
        if seen is not None:
            seen.append(target)
        path = write_deb(dest / name, package, version)
        if row is not None and on_pick is not None:
            repo_id, uri = row
            pkg = canister.Package(package, package, version, "", "", "", repo_id,
                                   f"debs/{package}_{version}.deb", None, None, "", None)
            on_pick(pkg, canister.Repository(repo_id, repo_id, uri))
        return path

    monkeypatch.setattr(canister, "fetch_deb", fetch)


def _run(cfg_path: Path, *args: str):
    result = CliRunner().invoke(cli_mod.app, ["--config", str(cfg_path), "--json", "--fetch", *args])
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    return result, next(e for e in events if e["type"] == "canister-fetch-end")


def _recorded(cfg, filename: str) -> Source | None:
    return tweak_sources.recorded_for(cfg.state_dir, filename,
                                      tweak_sources.file_sha256(cfg.tweaks_dir / filename))


def test_a_fetch_by_package_id_records_the_repository_it_came_from(temp_config_env: Path, monkeypatch,
                                                                  tmp_path: Path):
    _serving(monkeypatch, tmp_path, "com.example.atweak", "1.2", row=("good", "https://good.example.com/"))

    result, end = _run(temp_config_env, "com.example.atweak")

    assert result.exit_code == 0, result.output
    assert end["filename"] == "com.example.atweak_1.2.deb"
    cfg = load_config(temp_config_env)
    assert _recorded(cfg, end["filename"]) == Source(tweak_sources.CANISTER, "good", "https://good.example.com")


def test_a_fetch_of_a_url_under_a_configured_repo_records_that_repo(temp_config_env: Path, monkeypatch,
                                                                    tmp_path: Path):
    add_repo(temp_config_env, "https://apt.example.org", "Example")
    _serving(monkeypatch, tmp_path, "com.example.atweak", "1.2")

    result, end = _run(temp_config_env, "https://apt.example.org/debs/a.deb", "--fetch-sha", "ab" * 32)

    assert result.exit_code == 0, result.output
    cfg = load_config(temp_config_env)
    assert _recorded(cfg, end["filename"]) == Source(tweak_sources.REPO, "https://apt.example.org",
                                                     "https://apt.example.org")


def test_a_fetch_of_any_other_url_records_nothing(temp_config_env: Path, monkeypatch, tmp_path: Path):
    """Its source is inferred on each check, from its own hash."""
    add_repo(temp_config_env, "https://apt.example.org", "Example")
    _serving(monkeypatch, tmp_path, "com.example.atweak", "1.2")

    result, end = _run(temp_config_env, "https://apt.example.org.evil.example/a.deb")

    assert result.exit_code == 0, result.output
    assert _recorded(load_config(temp_config_env), end["filename"]) is None


def test_a_fetch_that_isnt_a_deb_never_reaches_the_library(temp_config_env: Path, monkeypatch):
    def html(_t, dest, *_a, **_k):
        (dest / "a.deb").write_bytes(b"<html>login</html>")
        return dest / "a.deb"

    monkeypatch.setattr(canister, "fetch_deb", html)

    result, end = _run(temp_config_env, "https://apt.example.org/a.deb")

    assert result.exit_code == 9
    assert end["ok"] is False and "isn't a .deb" in end["error"]
    library = load_config(temp_config_env).tweaks_dir
    assert [p.name for p in library.iterdir() if not p.name.startswith(".")] == []


def test_a_fetch_never_overwrites_a_different_library_tweak(temp_config_env: Path, monkeypatch,
                                                            tmp_path: Path):
    """2.3.1 A2 by `--fetch`: the URL's last component was the library name,
    so a URL ending `b-tweak.deb` replaced the `b-tweak.deb` apps build with."""
    cfg = load_config(temp_config_env)
    theirs = write_deb(cfg.tweaks_dir / "com.example.btweak_2.0.deb", "com.example.btweak", "2.0")
    before = theirs.read_bytes()

    def evil(_t, dest, *_a, **_k):
        return write_deb(dest / "com.example.btweak_2.0.deb", "com.example.btweak", "2.0", salt="evil")

    monkeypatch.setattr(canister, "fetch_deb", evil)

    result, end = _run(temp_config_env, "https://evil.example/com.example.btweak_2.0.deb")

    assert result.exit_code == 9
    assert "already in your tweaks library" in end["error"]
    assert theirs.read_bytes() == before


def test_a_fetch_by_package_id_must_be_that_package(temp_config_env: Path, monkeypatch, tmp_path: Path):
    _serving(monkeypatch, tmp_path, "com.example.other", "1.0")

    result, end = _run(temp_config_env, "com.example.atweak")

    assert result.exit_code == 9
    assert "com.example.other, not com.example.atweak" in end["error"]


@pytest.mark.parametrize("name", ["../../../escape.deb", ".hidden.deb", "tweak.zip"])
def test_whatever_the_repo_calls_it_the_library_name_is_graft_s(temp_config_env: Path, monkeypatch,
                                                                tmp_path: Path, name: str):
    _serving(monkeypatch, tmp_path, "com.example.atweak", "1.2", name=Path(name).name)

    result, end = _run(temp_config_env, f"https://apt.example.org/{name}")

    assert result.exit_code == 0, result.output
    assert end["filename"] == "com.example.atweak_1.2.deb"
    assert Path(end["path"]).parent == load_config(temp_config_env).tweaks_dir


def test_a_fetch_from_a_search_result_is_pinned_to_its_repository(temp_config_env: Path, monkeypatch,
                                                                  tmp_path: Path):
    seen: list = []

    def fetch(target, dest, expected_sha=None, *, repository_id=None, on_pick=None, **_k):
        seen.append(repository_id)
        return write_deb(dest / "d.deb", "com.example.atweak", "1.2")

    monkeypatch.setattr(canister, "fetch_deb", fetch)

    result, _end = _run(temp_config_env, "com.example.atweak", "--fetch-repository", "good")

    assert result.exit_code == 0, result.output
    assert seen == ["good"]


def test_fetch_repository_is_for_a_package_id(temp_config_env: Path):
    result = CliRunner().invoke(cli_mod.app, ["--config", str(temp_config_env), "--fetch",
                                              "https://apt.example.org/a.deb", "--fetch-repository", "good"])
    assert result.exit_code == 2
    assert "package id" in result.output


def test_fetch_repository_is_refused_beside_another_verb(temp_config_env: Path):
    result = CliRunner().invoke(cli_mod.app, ["--config", str(temp_config_env), "--list",
                                              "--fetch-repository", "good"])
    assert result.exit_code == 2
    assert "--fetch-repository" in result.output


def test_a_bare_fetch_several_repositories_list_exits_2_naming_them(temp_config_env: Path):
    from unittest.mock import patch

    rows = [{"package_id": "com.example.atweak", "version": v, "repository_id": r,
             "package_filename": f"debs/com.example.atweak_{v}.deb"} for r, v in (("good", "1.0"), ("evil", "9.9"))]

    def answer(path, *_a, **_k):
        if "/jailbreak/package/" in path:
            return {"data": rows}
        repo_id = path.rsplit("/", 1)[-1]
        return {"data": {"id": repo_id, "name": repo_id, "uri": f"https://{repo_id}.example"}}

    def no_download(*_a, **_k):
        raise AssertionError("downloaded a build of a package several repositories list")

    with patch.object(canister, "_get", side_effect=answer), patch.object(canister, "_download", no_download):
        result, end = _run(temp_config_env, "com.example.atweak")

    assert result.exit_code == 2
    assert end["ok"] is False
    assert "https://good.example" in end["error"] and "https://evil.example" in end["error"]
    assert "--fetch-repository" in result.output
