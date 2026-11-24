import os
import subprocess

import pytest
from wikigen import WikiError
from wikigen.build import ocr_texts

from .conftest import REPO

needs_vision = pytest.mark.skipif(
    os.environ.get("GRAFT_WIKI_OCR_TESTS") != "1", reason="opt-in: runs the Vision OCR"
)


@needs_vision
def test_ocr_reads_the_homepage_screenshot():
    texts = ocr_texts([REPO / "docs/screenshot.png"])
    # The screenshot never shows the word "Graft"; these strings are in it.
    text = texts[REPO / "docs/screenshot.png"]
    assert "Build & Install" in text and "com.example.clips" in text


def test_ocr_of_no_images_does_not_launch_swift(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran swift"))
    assert ocr_texts([]) == {}


def test_ocr_timeout_is_a_wiki_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("swift", 180)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(WikiError, match="OCR failed: timed out"):
        ocr_texts([tmp_path / "a.png"])


def test_ocr_nonzero_exit_is_a_wiki_error(monkeypatch, tmp_path):
    done = subprocess.CompletedProcess([], 1, "", "cannot read image: a.png\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    with pytest.raises(WikiError, match="OCR failed: cannot read image"):
        ocr_texts([tmp_path / "a.png"])
