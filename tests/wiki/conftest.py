import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Before collection, so test modules can import wikigen at module level.
sys.path.insert(0, str(REPO / "scripts"))

from .helpers import make_repo, make_site  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return make_site(tmp_path / "site")
