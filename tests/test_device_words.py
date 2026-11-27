"""What the CLI calls a device, in the text a person reads (2.3.1 §3.1).

The device page shows the CLI's hints and errors as they are, about an iPad
as often as an iPhone, and "unlock the phone" on an iPad's page reads as a
mistake. So text names an iPhone or iPad, the device, or its kind when the
code knows it; never a bare "phone", and never "the iPhone" for any device.

A scan of every string in `graft_ipa`, docstrings and comments left out:
those are for whoever reads the code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import graft_ipa

PACKAGE = Path(graft_ipa.__file__).parent

#: A bare "phone", which never means an iPad. "PhoneWentSilentError" and
#: "iPhone" aren't words of their own; "phone number" is an Apple ID's.
BARE_PHONE = re.compile(r"(?<![A-Za-z])phones?\b(?! number)", re.IGNORECASE)
#: "the iPhone", "an iPhone", "your iPhone", "On My iPhone": one kind of
#: device named for any of them.
ONE_KIND = re.compile(r"\b(?:the|an|your|On My) iPhone\b(?! or iPad|, iPad|/iPad)")


def _strings(path: Path) -> list[tuple[int, str]]:
    """Every string constant in `path` but its docstrings, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
    ]


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    return [
        f"{path.relative_to(PACKAGE.parent)}:{line}: {text[:100]!r}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in _strings(path)
        if pattern.search(text)
    ]


def test_no_text_says_phone() -> None:
    assert _offenders(BARE_PHONE) == []


def test_no_text_names_every_device_an_iphone() -> None:
    assert _offenders(ONE_KIND) == []


@pytest.mark.parametrize("text, offends", [
    ("unlock the phone", True),
    ("Phone builds can still use it", True),
    ("two phones", True),
    ("Apple ID (email or phone number): ", False),
    ("PhoneWentSilentError", False),
    ("plug the iPhone or iPad in", False),
    ("an iPhone or iPad app", False),
    ("on your iPhone, iPad or other Mac", False),
    ("the iPhone is locked", True),
])
def test_the_scan_tells_a_phone_from_an_iphone(text: str, offends: bool) -> None:
    """The patterns themselves, so a scan that matches nothing can't pass."""
    assert bool(BARE_PHONE.search(text) or ONE_KIND.search(text)) is offends


def test_the_scan_leaves_docstrings_out(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text('"""About the phone."""\n\ndef f():\n    """The phone."""\n    return "the phone"\n')
    assert [text for _, text in _strings(source)] == ["the phone"]


# --- The device's kind, where the code has it ---------------------------------

PRIMARY = "com.kdt.livecontainer.ABCDE12345"


@pytest.fixture
def strangers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A LiveContainer whose Documents has an example.ipa Graft didn't push."""
    from .test_lc_discovery import fake_device

    ipa = tmp_path / "example_1.0.ipa"
    ipa.write_bytes(b"the build")
    fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]},
                contents={(PRIMARY, "example.ipa"): b"somebody's"})
    return ipa


@pytest.mark.parametrize("product_type, folder", [
    ("iPad13,16", "On My iPad > LiveContainer"),
    ("iPhone16,1", "On My iPhone > LiveContainer"),
    (None, "On My iPhone or iPad > LiveContainer"),
])
@pytest.mark.parametrize("check_first", [True, False])
def test_a_strangers_file_names_the_files_app_folder_of_that_device(
    strangers_file, monkeypatch, product_type, folder, check_first,
) -> None:
    """The Files app says "On My iPad" on an iPad; a model the lockdown
    doesn't name gets both. Before cyan (`check_push_target`) and at the
    push alike."""
    from graft_ipa import lc_runner

    from .test_lc_discovery import _Lockdown

    monkeypatch.setattr(_Lockdown, "product_type", product_type, raising=False)
    with pytest.raises(lc_runner.LiveContainerError) as exc:
        if check_first:
            lc_runner.check_push_target("u1", inner_filename="example.ipa", lc_bundle_id=None,
                                        team=None, free_account=False, ours={},
                                        archives=[strangers_file])
        else:
            lc_runner.push_ipa_to_livecontainer(strangers_file, "u1", inner_filename="example.ipa",
                                                archives=[strangers_file])

    assert f"(Files app, {folder})" in (exc.value.hint or "")
