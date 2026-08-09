"""Running FreeCAD headless to turn a mesh into a solid (docs/08 §4).

The whole of klm's relationship with FreeCAD is one subprocess call. That is
deliberate: FreeCAD's Python is its own interpreter with its own version of
everything, and importing it into klm's process would couple klm's dependency
tree to a CAD kernel's.

Two behaviours the rest of klm depends on:

* **Absence degrades, it does not fail.** With no `freecadcmd` on the path, klm
  keeps 3D models as meshes. A missing optional tool must never produce a
  traceback (docs/12 §5).
* **Results are cached by source-mesh hash.** Conversion takes seconds to tens
  of seconds, and the same OBJ converts to the same STEP every time, so the
  second request for a model klm has already converted is free.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MESH_SUFFIXES",
    "SCRIPT_NAME",
    "ConversionResult",
    "convert_mesh",
    "find_freecad",
    "script_path",
]

SCRIPT_NAME = "obj2step.py"
DEFAULT_TOLERANCE = 0.1
#: Generous, because a complex connector genuinely takes this long.
DEFAULT_TIMEOUT = 300.0

#: Mesh formats FreeCAD's `Mesh` module reads and klm may be handed.
MESH_SUFFIXES = frozenset({".obj", ".wrl", ".stl", ".ply", ".off"})


class FreeCadUnavailable(Exception):
    """`freecadcmd` is not installed. Callers degrade rather than fail."""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    source: Path
    output: Path
    cached: bool
    """True when the STEP already existed for this mesh's hash."""
    stderr: str = ""

    @property
    def watertight(self) -> bool:
        """FreeCAD warns on stderr when the solid is not closed.

        Reported rather than enforced: an open shell still renders, and the QA
        gate is where the decision to accept it belongs.
        """
        return "not watertight" not in self.stderr


def find_freecad() -> Path | None:
    """Locate `freecadcmd`, or the GUI binary that can run a script headless."""
    for name in ("freecadcmd", "FreeCADCmd", "freecad", "FreeCAD"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def script_path() -> Path:
    """Where `obj2step.py` lives.

    Kept outside the importable package on purpose — it is not klm code, it is
    a script klm hands to another interpreter, and putting it on klm's import
    path would invite someone to import it.

    Two locations, because there are two ways klm gets installed: a wheel
    force-includes `cad/` as `klm/_cad/`, while a source checkout has it beside
    `src/`. Checking both is cheaper than a packaging bug that only appears
    once someone installs klm properly.
    """
    here = Path(__file__).resolve()
    candidates = (
        here.parents[1] / "_cad" / "scripts" / SCRIPT_NAME,  # installed wheel
        here.parents[3] / "cad" / "scripts" / SCRIPT_NAME,  # source checkout
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def mesh_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def convert_mesh(
    source: Path,
    output_dir: Path,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    timeout: float = DEFAULT_TIMEOUT,
    freecad: Path | None = None,
    script: Path | None = None,
) -> ConversionResult:
    """Convert a mesh to STEP, caching by the mesh's content hash.

    Raises :class:`FreeCadUnavailable` when the tool is missing and
    ``RuntimeError`` when the conversion itself fails — two different problems
    with two different remedies, so two different exceptions.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"no mesh at {source}")
    if source.suffix.lower() not in MESH_SUFFIXES:
        raise ValueError(
            f"{source.suffix} is not a mesh format klm converts "
            f"({', '.join(sorted(MESH_SUFFIXES))})"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    # Tolerance is part of the identity: the same mesh at 0.01 mm is a
    # different, much larger STEP than at 0.1 mm.
    digest = mesh_hash(source)
    output = output_dir / f"{digest}-{tolerance:g}.step"
    if output.is_file() and output.stat().st_size > 0:
        return ConversionResult(source, output, cached=True)

    binary = freecad or find_freecad()
    if binary is None:
        raise FreeCadUnavailable(
            "freecadcmd was not found; 3D models stay as meshes. Install FreeCAD to convert them."
        )

    runner = script or script_path()
    if not runner.is_file():
        raise RuntimeError(f"the conversion script is missing from the install: {runner}")

    try:
        completed = subprocess.run(
            [str(binary), str(runner), str(source), str(output), f"{tolerance:g}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"FreeCAD did not finish converting {source.name} in {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"could not run {binary}: {exc}") from exc

    if completed.returncode != 0 or not output.is_file():
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {completed.returncode}"
        # A half-written STEP is worse than none: it would pass a "file exists"
        # check and fail in KiCad.
        output.unlink(missing_ok=True)
        raise RuntimeError(f"converting {source.name} failed: {message}")

    return ConversionResult(source, output, cached=False, stderr=completed.stderr)
