"""Entry point for `klm-app.exe` — the windowed build.

A separate script from the CLI's because the two executables differ in exactly
one thing that cannot be decided at runtime: whether Windows attaches a console.
`klm-app.exe` is built windowed, so a double-click does not flash a black box;
`klm.exe` is built as a console program, because a CLI with nowhere to print is
not a CLI.

It deliberately does *not* re-implement `klm app`. It calls the same command
function the CLI dispatches to, so the packaged window and `klm app` in a
terminal cannot drift apart.
"""

from __future__ import annotations

import sys


def main() -> int:
    from klm.cli.main import main as cli

    # Everything after the executable name is passed through, so a shortcut can
    # carry `--catalog D:\parts` and the installed app still behaves like the
    # command it wraps.
    return cli(["app", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
