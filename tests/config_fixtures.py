"""A config with one app in it, for tests that need an app to act on.

The starter config `--init` writes used to ship a `example` app, and a large
part of this suite was written against it — `--app example`, its tweak list,
its display name. The starter now configures no apps (see
`test_starter_config.py`), so that entry is added here instead, explicitly,
with exactly the values the tests were written against.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from graft_ipa.config import add_app, write_default_config

#: The app the starter config used to ship, as it shipped it.
TEST_APP_NAME = "example"
TEST_APP: dict[str, Any] = {
    "bundle_id": "com.example.app",
    "preferred_version": "latest",
    "tweaks": [
        "a-tweak.deb",
        "some-tweak.deb",
        "c-tweak.deb",
        "libexample.dylib",
    ],
    "cyan": {
        "display_name": "Example+",
        "keep_bundle_id": True,
        "strip_extensions": False,
        "strip_supported_devices": True,
        "compression_level": 6,
    },
}


def write_test_config(path: Path, **overrides: Any) -> None:
    """`write_default_config`, plus the one app the tests act on."""
    write_default_config(path, **overrides)
    add_app(path, TEST_APP_NAME, copy.deepcopy(TEST_APP))
