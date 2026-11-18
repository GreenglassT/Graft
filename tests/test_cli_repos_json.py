from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter
from graft_ipa.repo import Repo

SAMPLE_PACKAGES = """\
Package: com.example.alpha
Name: Alpha
Version: 1.0
Filename: debs/alpha.deb
Size: 100

Package: com.example.beta
Name: Beta
Version: 2.0
Filename: debs/beta.deb
Size: 200
"""


def test_add_repo_json_emits_repo_added(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(
        cli_mod.repo, "fetch_release",
        return_value=Repo(url="https://apt.example.com", name="Example", origin="Example"),
    ), patch.object(cli_mod.repo, "fetch_packages", return_value=SAMPLE_PACKAGES):
        cli_mod._do_add_repo(cfg, "https://apt.example.com/", emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    added = next(e for e in events if e["type"] == "repo-added")
    assert added["url"] == "https://apt.example.com"
    assert added["name"] == "Example"
    assert added["package_count"] == 2
    # The repo is now persisted in config.yaml.
    assert load_config(temp_config_env).repos[0].url == "https://apt.example.com"


def test_remove_repo_json_emits_repo_removed(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import add_repo

    add_repo(temp_config_env, "https://apt.example.com", "Example")
    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_remove_repo(cfg, "https://apt.example.com", emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert any(e["type"] == "repo-removed" for e in events)
    assert load_config(temp_config_env).repos == []


def test_add_repo_failure_does_not_persist(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.repo import RepoError

    cfg = load_config(temp_config_env)
    with patch.object(
        cli_mod.repo, "fetch_release", side_effect=RepoError("no Release file")
    ):
        with pytest.raises(RepoError):
            cli_mod._do_add_repo(
                cfg, "https://bad.example.com", emitter=JsonEmitter(stream=StringIO())
            )
    # A failed add must not persist anything to config.yaml.
    assert load_config(temp_config_env).repos == []


def test_repos_json_lists_configured_repos(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import repo as repo_mod
    from graft_ipa.config import add_repo

    add_repo(temp_config_env, "https://apt.example.com", "Example")
    cfg = load_config(temp_config_env)
    # Seed the cache so package_count + last_refreshed are populated.
    cache = repo_mod.cache_path(cfg.state_dir, "https://apt.example.com")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(SAMPLE_PACKAGES)

    buf = StringIO()
    cli_mod._do_repos(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    repos = [e for e in events if e["type"] == "repo"]
    assert len(repos) == 1
    assert repos[0]["url"] == "https://apt.example.com"
    assert repos[0]["name"] == "Example"
    assert repos[0]["package_count"] == 2
    assert repos[0]["last_refreshed"] is not None


def test_repos_json_uncached_repo_has_zero_count(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import add_repo

    add_repo(temp_config_env, "https://apt.example.com", "Example")
    cfg = load_config(temp_config_env)  # no cache file seeded

    buf = StringIO()
    cli_mod._do_repos(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    repo_event = next(e for e in events if e["type"] == "repo")
    assert repo_event["package_count"] == 0
    assert repo_event["last_refreshed"] is None
    end = next(e for e in events if e["type"] == "repos-end")
    assert end["count"] == 1


def test_repo_packages_json_uses_cache(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import repo as repo_mod

    cfg = load_config(temp_config_env)
    cache = repo_mod.cache_path(cfg.state_dir, "https://apt.example.com")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(SAMPLE_PACKAGES)

    buf = StringIO()
    # No --refresh and cache present => no network call should happen.
    with patch.object(cli_mod.repo, "fetch_packages", side_effect=AssertionError("network")):
        cli_mod._do_repo_packages(
            cfg, "https://apt.example.com", refresh=False, emitter=JsonEmitter(stream=buf)
        )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    pkgs = [e for e in events if e["type"] == "repo-package"]
    assert {p["package_id"] for p in pkgs} == {"com.example.alpha", "com.example.beta"}
    assert pkgs[0]["download_url"].startswith("https://apt.example.com/")
    end = next(e for e in events if e["type"] == "repo-packages-end")
    assert end["count"] == 2


def test_repo_packages_json_refresh_refetches(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod.repo, "fetch_packages", return_value=SAMPLE_PACKAGES) as fp:
        cli_mod._do_repo_packages(
            cfg, "https://apt.example.com", refresh=True, emitter=JsonEmitter(stream=buf)
        )
    fp.assert_called_once()
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert any(e["type"] == "repo-refresh-start" for e in events)
    assert len([e for e in events if e["type"] == "repo-package"]) == 2


def test_repo_packages_cache_write_error_raises_repoerror(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.repo import RepoError

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod.repo, "fetch_packages", return_value=SAMPLE_PACKAGES), \
         patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        with pytest.raises(RepoError):
            cli_mod._do_repo_packages(
                cfg, "https://x.example.com", refresh=True,
                emitter=JsonEmitter(stream=StringIO()),
            )


def test_repos_json_finds_cache_for_trailing_slash_config_entry(temp_config_env: Path) -> None:
    import yaml

    from graft_ipa import cli as cli_mod
    from graft_ipa import repo as repo_mod

    raw = yaml.safe_load(temp_config_env.read_text())
    raw["repos"] = [{"url": "https://apt.example.com/", "name": "Example"}]
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = load_config(temp_config_env)
    # cache is written under the NORMALIZED url (no trailing slash)
    cache = repo_mod.cache_path(cfg.state_dir, "https://apt.example.com")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(SAMPLE_PACKAGES)

    buf = StringIO()
    cli_mod._do_repos(cfg, emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    repo_ev = next(e for e in events if e["type"] == "repo")
    assert repo_ev["package_count"] == 2  # cache found despite trailing slash
    assert repo_ev["last_refreshed"].endswith("+00:00")  # UTC offset present


def test_repo_packages_json_skips_packages_without_filename(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import repo as repo_mod

    cfg = load_config(temp_config_env)
    cache = repo_mod.cache_path(cfg.state_dir, "https://apt.example.com")
    cache.parent.mkdir(parents=True, exist_ok=True)
    # One normal package, one with no Filename field.
    cache.write_text(
        "Package: com.example.good\n"
        "Name: Good\n"
        "Version: 1.0\n"
        "Filename: debs/good.deb\n"
        "\n"
        "Package: com.example.nofile\n"
        "Name: No File\n"
        "Version: 1.0\n"
    )

    buf = StringIO()
    cli_mod._do_repo_packages(
        cfg, "https://apt.example.com", refresh=False, emitter=JsonEmitter(stream=buf)
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    pkg_ids = {e["package_id"] for e in events if e["type"] == "repo-package"}
    assert pkg_ids == {"com.example.good"}
    end = next(e for e in events if e["type"] == "repo-packages-end")
    assert end["count"] == 1
