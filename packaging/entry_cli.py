"""Entry point for `klm.exe` — the console build.

The installer puts this on `PATH`, because the project's own rule is that if the
GUI can do something the CLI cannot, that is a bug in the CLI. An installer that
shipped only the window would make that rule unenforceable for anyone who
installed klm the easy way.
"""

from __future__ import annotations

from klm.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
