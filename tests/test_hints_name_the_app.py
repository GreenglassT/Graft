"""Every hint that sends someone to `graft --init` names the app's way too.

The Mac app runs the CLI with no Terminal behind it, and its banners show the
CLI's hints as they are. A hint that says only "run `graft --init`" there is
one its reader can't follow. The app sets the account up on first run and
changes it in its Account settings (`config.APP_ACCOUNT_SETTINGS`).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from graft_ipa import config

PACKAGE = Path(config.__file__).parent

#: Any mention of `graft --init`, in any case and however it is asked for
#: ("Run `graft --init`", "...again with `graft --init`"), "run --init", and
#: the `{init}` template `config.paid_upgrade` fills in. Not
#: `graft --config X --init`: that names a config the app never opens, whose
#: way is the command alone (`paid_upgrade`'s rule).
_SENDS_TO_INIT = re.compile(
    r"(?<![\w-])graft (?!--config)(?:\S+ )?--init\b|\brun `?--init\b|\{init\}", re.IGNORECASE
)

#: How a string names the app's way: the app by name; the constant, as an
#: f-string field (`«APP_ACCOUNT_SETTINGS»`); or, in a plain template, the
#: `{app}` slot `paid_upgrade` fills with it. An f-string field that happens to
#: be called `app` (a user's app name) is `«app»`, and names nothing.
_NAMES_THE_APP = ("Graft.app", "«APP_ACCOUNT_SETTINGS»", "{app}")


def _text(node: ast.AST) -> str:
    """A string literal as written, f-string fields as `«expression»`, so
    they can't be mistaken for a template's `{slot}`."""
    if isinstance(node, ast.Constant):
        return node.value
    assert isinstance(node, ast.JoinedStr)
    return "".join(
        part.value if isinstance(part, ast.Constant) else "«" + ast.unparse(part.value) + "»"
        for part in node.values
    )


def _literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal outside docstrings, as one piece per expression
    (the parser already joins implicitly concatenated parts)."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    inner = {
        id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in docstrings or id(node) in inner:
            continue
        if isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        ):
            found.append((node.lineno, _text(node)))
    return found


def test_no_string_sends_someone_to_init_without_the_app() -> None:
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        for line, text in _literals(ast.parse(path.read_text(), filename=str(path))):
            # The bare command is a value (`paid_upgrade`'s `init`, which its
            # template pairs with `app`), not a sentence anyone reads alone.
            if text.strip("` ") == "graft --init":
                continue
            if _SENDS_TO_INIT.search(text) and not any(name in text for name in _NAMES_THE_APP):
                offenders.append(f"{path.name}:{line}: {text!r}")
    assert offenders == [], "\n".join(offenders)


def test_the_search_finds_the_hints_it_is_about() -> None:
    """Not vacuous: the strings that do send someone to `--init` are found."""
    hits = [
        (path.name, text)
        for path in sorted(PACKAGE.glob("*.py"))
        for _, text in _literals(ast.parse(path.read_text()))
        if _SENDS_TO_INIT.search(text)
    ]
    names = {name for name, _ in hits}
    assert {"config.py", "setup_portal.py"} <= names, hits


def test_the_way_to_paid_names_the_app_for_the_apps_own_config() -> None:
    for short in (False, True):
        assert config.APP_ACCOUNT_SETTINGS in config.paid_upgrade(None, short=short)


def test_a_config_the_app_never_opens_is_given_the_command_alone(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "config.yaml"
    for short in (False, True):
        way = config.paid_upgrade(elsewhere, short=short)
        assert f"--config {elsewhere} --init" in way
        assert config.APP_ACCOUNT_SETTINGS not in way


@pytest.mark.parametrize("text", [
    "Run `graft --init` to create one",
    "set the account up again with `graft --init` in Terminal",
    "RUN --init",
])
def test_the_search_is_not_fooled_by_case_or_wording(text: str) -> None:
    """H1 and H3 of the 2.0 review: each passed the old, lowercase-`run`-only search."""
    assert _SENDS_TO_INIT.search(text)
    assert not any(name in text for name in _NAMES_THE_APP)


def test_an_fstring_field_called_app_does_not_name_the_app() -> None:
    """H4b: `{app}` in an f-string is a user's app name, not the app's way."""
    (node,) = [n for n in ast.walk(ast.parse('f"(reading {app}); run `graft --init`"'))
               if isinstance(n, ast.JoinedStr)]
    text = _text(node)
    assert _SENDS_TO_INIT.search(text) and not any(n in text for n in _NAMES_THE_APP)
    (field,) = [n for n in ast.walk(ast.parse('f"run `graft --init`, or use {APP_ACCOUNT_SETTINGS}"'))
                if isinstance(n, ast.JoinedStr)]
    assert any(n in _text(field) for n in _NAMES_THE_APP)


def test_a_missing_config_elsewhere_is_told_the_command_that_makes_it(tmp_path: Path) -> None:
    """F10: `graft --init` makes the default config, and the app never opens
    another, so neither creates `--config elsewhere.yaml`."""
    from graft_ipa.errors import ConfigError

    elsewhere = tmp_path / "nowhere" / "config.yaml"
    with pytest.raises(ConfigError) as err:
        config.load_config(elsewhere)
    assert err.value.hint == f"run `graft --config {elsewhere} --init` to create it"


def test_the_default_config_missing_still_names_the_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from graft_ipa.errors import ConfigError

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(ConfigError) as err:
        config.load_config(config.default_config_path())
    assert "Graft.app" in (err.value.hint or "") and "graft --init" in (err.value.hint or "")
