"""Discovery of the things klm depends on but does not own.

Every external tool is optional. klm must never fail with a traceback because
FreeCAD is absent — it should say what is missing, what that disables, and how
to fix it (docs/12 §5).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ToolInfo", "find_kicad_config", "probe_all", "probe_tool"]

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class ToolInfo:
    """An external tool klm shells out to."""

    name: str
    path: Path | None
    version: str | None
    purpose: str
    consequence: str
    """What stops working when this tool is absent, as a complete sentence."""
    install_hint: str

    @property
    def found(self) -> bool:
        return self.path is not None


#: Ordered so the most consequential absence is reported first.
_TOOLS = [
    (
        "kicad-cli",
        ("--version",),
        "Gerber/drill/BOM export, DRC and ERC",
        "fabrication output and project verification are unavailable",
        "install KiCad 8 or later",
    ),
    (
        "freecadcmd",
        ("--version",),
        "mesh to solid STEP conversion",
        "3D models are kept as meshes rather than converted to solids",
        "apt install freecad",
    ),
]


def probe_tool(
    name: str,
    version_args: tuple[str, ...],
    purpose: str,
    consequence: str,
    install_hint: str,
) -> ToolInfo:
    """Locate a tool and read its version, tolerating every way that can fail."""
    found = shutil.which(name)
    if found is None:
        return ToolInfo(name, None, None, purpose, consequence, install_hint)

    version: str | None = None
    try:
        completed = subprocess.run(
            [found, *version_args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        match = _VERSION_RE.search(output)
        if match:
            version = match.group(1)
    except (OSError, subprocess.SubprocessError):
        version = None

    return ToolInfo(name, Path(found), version, purpose, consequence, install_hint)


def probe_all() -> list[ToolInfo]:
    return [probe_tool(name, args, *rest) for name, args, *rest in _TOOLS]


def find_kicad_config() -> tuple[Path | None, str | None]:
    """Locate the newest KiCad user-configuration directory and its version.

    The path is version-pinned (``~/.config/kicad/9.0``), so klm discovers the
    installed version rather than hardcoding one. Returns ``(path, version)``,
    both ``None`` when nothing is found.
    """
    candidates: list[Path] = []
    override = os.environ.get("KICAD_CONFIG_HOME")
    if override:
        candidates.append(Path(override).expanduser())

    xdg = os.environ.get("XDG_CONFIG_HOME")
    default_config = Path(xdg).expanduser() / "kicad" if xdg else Path.home() / ".config" / "kicad"
    candidates.append(default_config)
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata).expanduser() / "kicad")
    candidates.append(Path.home() / "Library" / "Preferences" / "kicad")

    for base in candidates:
        if not base.is_dir():
            continue
        versioned = sorted(
            (p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+", p.name)),
            key=lambda p: tuple(int(n) for n in p.name.split(".")),
            reverse=True,
        )
        if versioned:
            return versioned[0], versioned[0].name
        if (base / "kicad_common.json").exists():
            return base, None

    return None, None
