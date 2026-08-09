"""Opening klm in a native window, and serving it when that is not possible.

The window is the platform's own webview — WebView2 on Windows, WebKit on macOS,
WebKitGTK on Linux — wrapped by pywebview. There is no bundled browser engine and
no second toolchain: klm is already installed as Python on all three platforms,
so the shell ships in the same wheel (docs/adr/0012).

Two behaviours the rest of klm's conventions demand:

* **Absence degrades, it does not fail.** A machine with no usable webview —
  a minimal Linux container, a server over SSH — gets `klm serve` and a URL
  rather than a traceback. That is the same rule `freecadcmd` follows.
* **Localhost only.** The server binds 127.0.0.1. Binding anywhere routable
  would expose a catalog, an unauthenticated API and the ability to write to the
  user's project files, so the host is fixed rather than configurable.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

__all__ = ["HOST", "WindowUnavailable", "pick_port", "run_server", "run_window"]

#: Not configurable, deliberately. See the module docstring.
HOST = "127.0.0.1"


class WindowUnavailable(Exception):
    """No usable webview. The caller falls back to serving a URL."""


def pick_port(preferred: int = 8731) -> int:
    """The preferred port if it is free, otherwise one the OS chooses.

    A desktop app that refuses to start because a port is busy is a desktop app
    that has made the user's problem its own.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise OSError("no free port on localhost")  # pragma: no cover - unreachable in practice


def _serve(app: Any, port: int) -> Any:
    import uvicorn

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    return uvicorn.Server(config)


def run_server(catalog: str | Path | None = None, *, port: int | None = None) -> None:
    """Serve the UI and block. The browser mode, and what CI and SSH get."""
    from klm.api.server import create_app

    chosen = port or pick_port()
    print(f"klm is at http://{HOST}:{chosen}  (Ctrl-C to stop)")
    _serve(create_app(catalog), chosen).run()


def run_window(
    catalog: str | Path | None = None, *, port: int | None = None, title: str = "klm"
) -> None:
    """Open the native window. Raises :class:`WindowUnavailable` if it cannot.

    The server runs on a daemon thread so closing the window ends the process —
    which is what closing a window should do, and what a user will expect when
    there is no terminal in sight.
    """
    try:
        import webview
    except ModuleNotFoundError as exc:
        raise WindowUnavailable(
            "pywebview is not installed — run: pip install 'klm[app]'"
        ) from exc

    from klm.api.server import create_app

    chosen = port or pick_port()
    server = _serve(create_app(catalog), chosen)
    threading.Thread(target=server.run, name="klm-api", daemon=True).start()
    _await_server(chosen)

    try:
        webview.create_window(title, f"http://{HOST}:{chosen}", width=1180, height=780)
        webview.start()
    except Exception as exc:
        # On Linux this is usually a missing WebKitGTK. Naming the fallback is
        # more useful than naming the exception.
        raise WindowUnavailable(
            f"no usable webview on this machine ({exc}). "
            "On Linux install WebKitGTK (Debian/Ubuntu: gir1.2-webkit2-4.1), "
            "or use `klm serve` and open the URL in a browser."
        ) from exc


def _await_server(port: int, *, attempts: int = 100, delay: float = 0.05) -> None:
    """Wait for the port to answer, so the window never opens on a dead socket."""
    import time

    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(delay)
            if probe.connect_ex((HOST, port)) == 0:
                return
        time.sleep(delay)
