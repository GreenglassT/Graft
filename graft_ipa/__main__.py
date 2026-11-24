"""`python -m graft_ipa`: the same CLI as the `graft` command.

`prog_name` because Click would otherwise name the program after this file,
and `--help` would print `Usage: __main__.py` for a command nobody can type.
"""

import sys

from .child_env import tame_bundled_process

# Before anything reads OPENSSL_CONF or a CA bundle from the login shell.
tame_bundled_process()

from .cli import app  # noqa: E402

if __name__ == "__main__":
    sys.exit(app(prog_name="graft"))
