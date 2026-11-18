from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    return path


def test_attach_tweak_appends_filename_and_emits_event(
    temp_config_env: Path,
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # The default `example` app starts with 4 tweaks; we'll add a new one.
    _touch(cfg.tweaks_dir / "newtweak.deb")

    buf = StringIO()
    cli_mod._do_attach_tweak(
        cfg, app_name="example", filename="newtweak.deb",
        emitter=JsonEmitter(stream=buf),
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "tweak-attached")
    assert ev["app"] == "example"
    assert ev["filename"] == "newtweak.deb"
    assert "newtweak.deb" in ev["tweaks"]

    reloaded = load_config(temp_config_env)
    assert "newtweak.deb" in reloaded.get_app("example").tweaks


def test_attach_tweak_is_idempotent(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "newtweak.deb")

    cli_mod._do_attach_tweak(
        cfg, app_name="example", filename="newtweak.deb",
        emitter=JsonEmitter(stream=StringIO()),
    )
    # Reload so the second call's in-memory `app.tweaks` reflects the post-
    # first-attach state — otherwise the dedup branch (`if filename in
    # app.tweaks`) never fires and we'd be testing nothing.
    cfg = load_config(temp_config_env)
    cli_mod._do_attach_tweak(
        cfg, app_name="example", filename="newtweak.deb",
        emitter=JsonEmitter(stream=StringIO()),
    )

    tweaks = load_config(temp_config_env).get_app("example").tweaks
    assert tweaks.count("newtweak.deb") == 1


def test_attach_tweak_missing_file_raises_tweak_not_found(
    temp_config_env: Path,
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import TweakNotFoundError

    cfg = load_config(temp_config_env)
    # File NOT created in tweaks_dir.
    with pytest.raises(TweakNotFoundError):
        cli_mod._do_attach_tweak(
            cfg, app_name="example", filename="ghost.deb",
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_attach_tweak_unknown_app_raises_configerror(
    temp_config_env: Path,
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import ConfigError

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "newtweak.deb")
    with pytest.raises(ConfigError):
        cli_mod._do_attach_tweak(
            cfg, app_name="not-a-real-app", filename="newtweak.deb",
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_detach_tweak_removes_filename_and_emits_event(
    temp_config_env: Path,
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # The seeded `example` app has `a-tweak.deb` already in its list.
    buf = StringIO()
    cli_mod._do_detach_tweak(
        cfg, app_name="example", filename="a-tweak.deb",
        emitter=JsonEmitter(stream=buf),
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "tweak-detached")
    assert ev["app"] == "example"
    assert ev["filename"] == "a-tweak.deb"
    assert "a-tweak.deb" not in ev["tweaks"]

    reloaded = load_config(temp_config_env)
    assert "a-tweak.deb" not in reloaded.get_app("example").tweaks


def test_detach_tweak_missing_filename_is_no_op(temp_config_env: Path) -> None:
    """Detaching a filename that isn't in the list is a successful no-op —
    the event still emits with the unchanged list."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    before = list(cfg.get_app("example").tweaks)
    buf = StringIO()
    cli_mod._do_detach_tweak(
        cfg, app_name="example", filename="not-attached.deb",
        emitter=JsonEmitter(stream=buf),
    )
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "tweak-detached")
    assert ev["tweaks"] == before
    assert load_config(temp_config_env).get_app("example").tweaks == before


def test_attach_wont_duplicate_a_tweak_listed_in_path_form(
    temp_config_env: Path,
) -> None:
    """Attach tested membership with `filename in app.tweaks` — exact strings.

    Every other tweak lookup in the module keys on `cfg.tweak_key` identity, so
    a config entry written in path form named the same file that the bare
    filename in the Tweaks list does. Attaching from that list appended a
    SECOND entry for it: cyan was then invoked with `-f <path> <path>`, the
    delivery badge reported the tweak as listed more than once, and detach —
    which correctly drops every spelling — removed both rows when the user
    clicked either one.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    tweak = _touch(cfg.tweaks_dir / "some-tweak.deb")

    # The same file, spelled as an absolute path — one of the forms a
    # hand-edited config.yaml or an older attach could leave behind.
    import yaml
    data = yaml.safe_load(temp_config_env.read_text())
    data["apps"]["example"]["tweaks"] = [str(tweak)]
    temp_config_env.write_text(yaml.safe_dump(data))

    cfg = load_config(temp_config_env)
    cli_mod._do_attach_tweak(
        cfg, app_name="example", filename="some-tweak.deb",
        emitter=JsonEmitter(stream=StringIO()),
    )

    tweaks = load_config(temp_config_env).get_app("example").tweaks
    assert tweaks == [str(tweak)], f"the path-form entry already names this file: {tweaks}"
