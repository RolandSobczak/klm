"""Running ``kicad-cli`` for the outputs only KiCad can produce.

Gerbers, drill files, the raw placement list, DRC and ERC all come from KiCad's
own code. Reimplementing any of them would mean reimplementing a plotter, and a
gerber that is subtly wrong is a scrapped board.

The shape mirrors :mod:`klm.cad.freecad`: locate the binary, run one subprocess,
turn a non-zero exit into a message a person can act on. Two differences follow
from what this tool is for:

* **Absence is fatal here, not a degradation.** No `freecadcmd` means models
  stay meshes; no `kicad-cli` means there is no fab package at all. Callers get
  :class:`KiCadCliUnavailable` and say so plainly.
* **The runner is injectable.** KiCad is not installed on every machine that
  runs klm's tests, and a test suite that silently skips the fab pipeline is a
  fab pipeline nobody tests.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_TIMEOUT",
    "CliResult",
    "KiCadCli",
    "KiCadCliError",
    "KiCadCliUnavailable",
    "Runner",
    "find_kicad_cli",
    "subprocess_runner",
]

DEFAULT_TIMEOUT = 600.0

#: The copper and technical layers a fab needs for a two-layer board. Kept
#: explicit rather than "all layers", because `kicad-cli` plots user and
#: comment layers too and a fab that receives them sometimes charges for them.
DEFAULT_LAYERS = (
    "F.Cu",
    "B.Cu",
    "F.Paste",
    "B.Paste",
    "F.Silkscreen",
    "B.Silkscreen",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
)


class KiCadCliUnavailable(Exception):
    """`kicad-cli` is not installed. Fabrication is unavailable, not degraded."""


class KiCadCliError(Exception):
    """A `kicad-cli` invocation failed, with its own message attached."""


@dataclass(frozen=True)
class CliResult:
    args: Sequence[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def message(self) -> str:
        """The most useful line of output, for an error a person will read."""
        detail = (self.stderr or self.stdout or "").strip().splitlines()
        return detail[-1] if detail else f"exit code {self.returncode}"


#: Injected so the pipeline is testable without KiCad installed, and so a future
#: GUI can stream progress from the same calls.
Runner = Callable[[Sequence[str], float], CliResult]


def find_kicad_cli() -> Path | None:
    found = shutil.which("kicad-cli")
    return Path(found) if found else None


def subprocess_runner(args: Sequence[str], timeout: float) -> CliResult:
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise KiCadCliError(f"kicad-cli did not finish in {timeout:g}s") from exc
    except OSError as exc:
        raise KiCadCliError(f"could not run kicad-cli: {exc}") from exc
    return CliResult(args, completed.returncode, completed.stdout, completed.stderr)


class KiCadCli:
    """A thin, typed front for the handful of subcommands klm uses."""

    def __init__(
        self,
        binary: Path | None = None,
        *,
        runner: Runner | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._binary = binary
        self._runner: Runner = runner or subprocess_runner
        self.timeout = timeout

    @property
    def binary(self) -> Path:
        if self._binary is None:
            found = find_kicad_cli()
            if found is None:
                raise KiCadCliUnavailable(
                    "kicad-cli was not found. Fabrication output needs KiCad 8 or later "
                    "on the PATH."
                )
            self._binary = found
        return self._binary

    def available(self) -> bool:
        return self._binary is not None or find_kicad_cli() is not None

    def run(self, *args: str) -> CliResult:
        return self._runner([str(self.binary), *args], self.timeout)

    def check(self, *args: str) -> CliResult:
        result = self.run(*args)
        if not result.ok:
            raise KiCadCliError(f"kicad-cli {' '.join(args[:2])} failed: {result.message()}")
        return result

    # -- version --------------------------------------------------------

    def version(self) -> str:
        result = self.run("--version")
        return (result.stdout or result.stderr).strip().splitlines()[0] if result.ok else ""

    # -- board outputs --------------------------------------------------

    def export_gerbers(
        self,
        board: Path,
        output_dir: Path,
        *,
        layers: Sequence[str] = DEFAULT_LAYERS,
        protel_extensions: bool = False,
    ) -> CliResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb",
            "export",
            "gerbers",
            "--output",
            str(output_dir),
            "--layers",
            ",".join(layers),
        ]
        # Protel extensions (`.GTL`, `.GBL`, …) are what JLCPCB's uploader
        # recognises without being told which file is which layer; KiCad's own
        # `.gbr` names need a manual mapping on their side.
        if not protel_extensions:
            args.append("--no-protel-ext")
        return self.check(*args, str(board))

    def export_drill(
        self, board: Path, output_dir: Path, *, merge_pth_npth: bool = False
    ) -> CliResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb",
            "export",
            "drill",
            "--output",
            str(output_dir),
            "--format",
            "excellon",
            "--drill-origin",
            "absolute",
            "--excellon-units",
            "mm",
            "--generate-map",
            "--map-format",
            "gerberx2",
        ]
        # JLCPCB wants plated and non-plated holes in separate files; merging
        # them is the option, not the default.
        if not merge_pth_npth:
            args.append("--excellon-separate-th")
        return self.check(*args, str(board))

    def export_pos(self, board: Path, output: Path) -> CliResult:
        """The raw placement list, in mm, both sides in one CSV.

        klm reads this rather than the board's own ``(at …)`` nodes because the
        position file has its own conventions — a flipped Y axis and an origin
        that may be the drill/place origin rather than the sheet's. Getting
        either wrong puts every part in the wrong place, and it would look
        plausible.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        return self.check(
            "pcb",
            "export",
            "pos",
            "--output",
            str(output),
            "--format",
            "csv",
            "--units",
            "mm",
            "--side",
            "both",
            "--use-drill-file-origin",
            str(board),
        )

    def run_drc(self, board: Path, output: Path, *, severity_error: bool = True) -> CliResult:
        """Design-rule check. Returns the result rather than raising on findings.

        A non-zero exit here means "the board has violations", which is an
        answer, not a failure — the caller decides whether it blocks.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "pcb",
            "drc",
            "--output",
            str(output),
            "--format",
            "report",
            "--schematic-parity",
        ]
        if severity_error:
            args.append("--exit-code-violations")
        return self.run(*args, str(board))

    def run_erc(self, schematic: Path, output: Path, *, severity_error: bool = True) -> CliResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        args = ["sch", "erc", "--output", str(output), "--format", "report"]
        if severity_error:
            args.append("--exit-code-violations")
        return self.run(*args, str(schematic))

    def export_schematic_pdf(self, schematic: Path, output: Path) -> CliResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        return self.check("sch", "export", "pdf", "--output", str(output), str(schematic))
