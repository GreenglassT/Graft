"""`python -m graft_ipa`: the same CLI as the `graft` command.

`prog_name` because Click would otherwise name the program after this file,
and `--help` would print `Usage: __main__.py` for a command nobody can type.
"""

import sys

from .cli import app

if __name__ == "__main__":
    sys.exit(app(prog_name="graft"))
