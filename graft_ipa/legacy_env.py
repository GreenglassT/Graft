"""Environment variables renamed with the project, and saying so.

The old names are not read: a fallback would keep them working forever and
nobody would ever move off them. But a variable that silently stopped
working is worse — an SSH key that is no longer offered reads as "the phone
refused me" — so whoever still sets an old name, without its new one, is
told which name to use instead. `cli.main` prints these beside the data-dir
migration notices, on the warning channel under `--json`.

`cert_password_env` is not here: its value is a name the user chose in their
own config, not one this tool defines.
"""

from __future__ import annotations

import os

#: old name -> the name read now.
RENAMED = {
    "TB_SSH_KEY": "GRAFT_SSH_KEY",
    "TB_SSH_USER": "GRAFT_SSH_USER",
    "TB_SSH_PASS": "GRAFT_SSH_PASS",
    "TB_FRIDA_IOS_DUMP": "GRAFT_FRIDA_IOS_DUMP",
    "TWEAK_BUILD_LC_GOLDEN": "GRAFT_LC_GOLDEN",
}


def legacy_env_notices() -> list[str]:
    """One line per old name that is set (non-empty) while its new one is not."""
    return [
        f"{old} is set but no longer read; set {new} instead"
        for old, new in RENAMED.items()
        if os.environ.get(old) and not os.environ.get(new)
    ]
