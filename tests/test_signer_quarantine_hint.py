"""The zsign quarantine hint is given only when zsign really is quarantined.

A quarantined zsign is killed by the OS before it prints anything (returncode
-9), and the hint to clear the attribute is the right one then. It used to be
given for *every* SIGKILL — memory pressure, a crash, a user's `kill -9` —
sending people to strip an attribute that was never there. Now the binary is
asked (`xattr -p com.apple.quarantine`), and the hint names it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import signer, tools
from graft_ipa.errors import SigningError
from graft_ipa.signer import run_zsign
from tests.test_signer import make_cfg
from tests.tool_fakes import fake_path

#: Relative to each test's tmp_path: where `tools.which` finds zsign.
ZSIGN_IN = Path("path-bin") / "zsign"


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str,
         *, quarantined: bool = False) -> SigningError:
    cfg = make_cfg(tmp_path, password="pw")
    monkeypatch.setenv("TEST_P12_PASS", "pw")
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / ZSIGN_IN.parent))
    zsign = str(tmp_path / ZSIGN_IN)
    asked: list[str] = []
    monkeypatch.setattr(signer, "_quarantined",
                        lambda path: asked.append(path) or quarantined)
    fake = subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)
    with patch("subprocess.run", return_value=fake), pytest.raises(SigningError) as exc:
        run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    if quarantined:
        assert asked == [zsign]
    return exc.value


def test_a_quarantined_zsign_killed_with_no_output_gets_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OS kills a quarantined zsign: returncode -9 and no text at all."""
    err = _run(tmp_path, monkeypatch, -9, "", quarantined=True)
    assert f"xattr -d com.apple.quarantine {tmp_path / ZSIGN_IN}" in (err.hint or "")


def test_a_sigkill_without_quarantine_does_not_blame_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = _run(tmp_path, monkeypatch, -9, "", quarantined=False)
    assert "xattr" not in (err.hint or "")
    assert "SIGKILL" in (err.hint or "")


def test_a_code_signature_complaint_without_quarantine_does_not_blame_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = _run(tmp_path, monkeypatch, 1, "Killed: 9 (code signature invalid)", quarantined=False)
    assert "xattr" not in (err.hint or "")


def test_a_code_signature_complaint_on_a_quarantined_zsign_gets_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = _run(tmp_path, monkeypatch, 1, "Killed: 9", quarantined=True)
    assert f"xattr -d com.apple.quarantine {tmp_path / ZSIGN_IN}" in (err.hint or "")


def test_an_ordinary_failure_does_not_get_the_quarantine_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = _run(tmp_path, monkeypatch, 1, "boom")
    assert "xattr" not in (err.hint or "")


@pytest.mark.skipif(sys.platform != "darwin", reason="extended attributes as macOS keeps them")
def test_the_attribute_is_read_off_the_real_file(tmp_path: Path) -> None:
    """The real `xattr`, on a scratch file only."""
    plain = tmp_path / "zsign-plain"
    plain.write_bytes(b"x")
    marked = tmp_path / "zsign-marked"
    marked.write_bytes(b"x")
    subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine",
                    "0081;00000000;GraftTest;", str(marked)], check=True)
    assert signer._quarantined(str(marked)) is True
    assert signer._quarantined(str(plain)) is False
    assert signer._quarantined(str(tmp_path / "missing")) is False
    assert signer._quarantined(None) is False
