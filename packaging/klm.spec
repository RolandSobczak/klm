# PyInstaller spec for the Windows build.  Run from the repository root:
#
#     pyinstaller packaging/klm.spec --noconfirm
#
# Two executables from one analysis, sharing one `dist/klm/` directory:
#
#   klm-app.exe   windowed  — the desktop app; a double-click flashes no console
#   klm.exe       console   — the full CLI, which the installer puts on PATH
#
# `onedir`, not `onefile`.  A one-file build unpacks itself to a temporary
# directory on every launch, which costs seconds on a cold start, trips
# aggressive antivirus, and makes `sys.executable` a path that will not exist
# next time.  An installer is already copying a directory, so the one thing
# onefile buys — a single portable artifact — is the thing we do not need.
#
# noqa: this file is executed by PyInstaller with `Analysis`, `EXE` and friends
# already in scope, so it does not import them and does not lint as normal code.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected


def _version() -> str:
    """klm's version, read from the source rather than imported.

    Importing the package at spec time would run it under PyInstaller's own
    interpreter before the analysis has decided anything, which works until the
    day an import in `klm/__init__.py` has a side effect. Reading the literal
    cannot.
    """
    import re

    text = (ROOT / "src" / "klm" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return found.group(1) if found else "0.0.0"


VERSION = _version()

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
# The UI is plain files with no build step (ADR-0012), so it has to be carried
# as data.  Ship it under the same relative path the package uses, because
# `klm.api.server.STATIC_DIR` is derived from `__file__` and must resolve
# identically frozen and unfrozen.
datas = [
    (str(ROOT / "src" / "klm" / "api" / "static"), "klm/api/static"),
    # `cad/scripts/obj2step.py` runs under FreeCAD's interpreter, never klm's.
    # It still has to ship or an installed klm cannot convert a mesh.
    (str(ROOT / "cad"), "klm/_cad"),
]

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# uvicorn resolves its loop, protocol and lifespan implementations by *name* at
# runtime, so a static analysis finds none of them and the frozen server starts
# and then fails on the first request.  This is the single most likely way a
# working `pip install` becomes a broken installer, which is why it is listed
# explicitly rather than left to a hook that may or may not be present.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview picks its backend at import time from what the platform has.
    # On Windows that is WinForms via pythonnet.
    *collect_submodules("webview.platforms"),
    # klm's own CLI reaches several services only through the dispatch table,
    # which is a dict of functions — traceable — but the supplier adapters are
    # built from a registry keyed by name, which is not.
    *collect_submodules("klm.suppliers"),
]

analysis = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing in klm draws with these, and leaving them in adds tens of
    # megabytes to an installer that is already large for what it does.
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest", "hypothesis"],
    noarchive=False,
)

cli_analysis = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest", "hypothesis"],
    noarchive=False,
)

MERGE((analysis, "klm-app", "klm-app"), (cli_analysis, "klm", "klm"))  # noqa: F821

app_pyz = PYZ(analysis.pure)  # noqa: F821
cli_pyz = PYZ(cli_analysis.pure)  # noqa: F821

# Each platform wants its own icon format, and all of them are generated from
# the one source image by `make_icons.py`. An absent icon is not an error: the
# build uses the platform default, which is the right behaviour for a checkout
# that has not run the generator.
_ICONS = {"win32": "klm.ico", "darwin": "klm.icns"}
_icon_file = ROOT / "packaging" / _ICONS.get(sys.platform, "")
icon = str(_icon_file) if _icon_file.is_file() else None

app_exe = EXE(  # noqa: F821
    app_pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="klm-app",
    debug=False,
    strip=False,
    upx=False,  # UPX-packed binaries are a reliable way to be flagged as malware
    console=False,
    icon=icon,
)

cli_exe = EXE(  # noqa: F821
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="klm",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=icon,
)

collected = COLLECT(  # noqa: F821
    app_exe,
    analysis.binaries,
    analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="klm",
)

if sys.platform == "darwin":
    # macOS wants a `.app`, not a directory of binaries: Finder will not launch
    # the latter by double-click and Dock/Spotlight ignore it. The CLI binary is
    # still inside the bundle at `klm.app/Contents/MacOS/klm`, and the release
    # notes say so — a Mac install must not be the one that loses the CLI.
    BUNDLE(  # noqa: F821
        collected,
        name="klm.app",
        icon=icon,
        bundle_identifier="dev.rolandsobczak.klm",
        version=VERSION,  # noqa: F821 - injected below
        info_plist={
            "CFBundleShortVersionString": VERSION,  # noqa: F821
            # Without this the window renders at 72 dpi on a Retina display and
            # every line in a footprint preview looks soft.
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
        },
    )
