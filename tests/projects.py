"""A small catalog and a small KiCad project, for the vendoring tests.

Kept out of the test modules themselves because vendoring, sync and the CLI all
need the same starting point, and three copies of a schematic fixture would
drift apart exactly where the tests need them identical.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from klm.model import Lifecycle, Part, PartStatus
from klm.services.catalog import save_part
from klm.store.assets import AssetKind, AssetStore

SYMBOL_ASSET = """(kicad_symbol_lib
	(version 20231120)
	(generator "test")
	(symbol "SRC"
		(property "Reference" "R" (at 0 0 0))
		(property "Value" "4.7k" (at 0 0 0))
		(symbol "SRC_1_1"
			(pin passive line (at -2.54 0 0) (length 1.27) (name "~") (number "1"))
			(pin passive line (at 2.54 0 0) (length 1.27) (name "~") (number "2"))
		)
	)
)
"""

FOOTPRINT_ASSET = """(footprint "R_0402_1005Metric"
	(layer "F.Cu")
	(pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu"))
	(pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu"))
	(model "${KLM_3DMODELS}/R_0402_1005Metric.step"
		(offset (xyz 0 0 0))
	)
)
"""

MOUNT_FOOTPRINT = """(footprint "MountingHole_3.2mm"
	(layer "F.Cu")
	(pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu"))
)
"""

STEP_ASSET = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=POINT('',(0.,0.,0.));\nENDSEC;\n"

RESISTOR_ID = "01JB4K7QW8ZR3XN5M2VYT9DCFA"
MOUNT_ID = "01JB4K7QW8ZR3XN5M2VYT9DCFB"

BOARD = """(kicad_pcb
	(version 20231120)
	(generator "pcbnew")
	(footprint "KLM:R_0402_1005Metric"
		(layer "F.Cu")
		(at 50 50)
		(property "Reference" "R1" (at 0 0 0))
	)
	(footprint "KLM:MountingHole_3.2mm"
		(layer "F.Cu")
		(at 10 10)
		(property "Reference" "H1" (at 0 0 0))
	)
)
"""


def schematic(lib_id: str = "KLM:RC0402FR-074K7L", klm_id: str | None = RESISTOR_ID) -> str:
    """One placed resistor, with the cached library symbol KiCad writes beside it."""
    klm_field = f'\n\t\t(property "KLM_ID" "{klm_id}" (at 0 0 0))' if klm_id else ""
    # KiCad names a cached symbol's units after the bare name, without the
    # library nickname. Getting this wrong in a fixture hides a real bug.
    bare = lib_id.partition(":")[2] or lib_id
    return f"""(kicad_sch
	(version 20231120)
	(generator "eeschema")
	(uuid "3a1f")
	(lib_symbols
		(symbol "{lib_id}"
			(property "Reference" "R" (at 0 0 0))
			(symbol "{bare}_1_1"
				(pin passive line (at -2.54 0 0) (length 1.27) (name "~") (number "1"))
			)
		)
	)
	(symbol
		(lib_id "{lib_id}")
		(at 100 100 0)
		(uuid "b2c3")
		(property "Reference" "R1" (at 100 96 0))
		(property "Value" "4.7k" (at 100 104 0))
		(property "Footprint" "KLM:R_0402_1005Metric" (at 0 0 0)){klm_field}
	)
)
"""


def seed_resistor(store: AssetStore, conn: sqlite3.Connection, **overrides: object) -> Part:
    settings: dict[str, object] = {
        "klm_id": RESISTOR_ID,
        "mpn": "RC0402FR-074K7L",
        "manufacturer": "Yageo",
        "description": "4.7 kOhm 1% 0402",
        "category": "Passive/Resistor",
        "package": "0402",
        "lifecycle": Lifecycle.ACTIVE,
        "status": PartStatus.APPROVED,
        "symbol_hash": store.add_bytes(SYMBOL_ASSET.encode(), AssetKind.SYMBOL),
        "footprint_hash": store.add_bytes(FOOTPRINT_ASSET.encode(), AssetKind.FOOTPRINT),
        "model3d_hash": store.add_bytes(STEP_ASSET, AssetKind.MODEL3D),
    }
    settings.update(overrides)
    return save_part(conn, Part(**settings))  # type: ignore[arg-type]


def seed_mount(store: AssetStore, conn: sqlite3.Connection) -> Part:
    """A footprint-only part: on the board, never on a sheet."""
    return save_part(
        conn,
        Part(
            klm_id=MOUNT_ID,
            mpn="MountingHole_3.2mm",
            manufacturer="Mechanical",
            category="Mechanical/MountingHole",
            package="M3",
            status=PartStatus.APPROVED,
            footprint_hash=store.add_bytes(MOUNT_FOOTPRINT.encode(), AssetKind.FOOTPRINT),
        ),
    )


def make_project(root: Path, *, board: bool = False, sheet: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "my-board.kicad_pro").write_text("{}\n", encoding="utf-8")
    (root / "my-board.kicad_sch").write_text(
        sheet if sheet is not None else schematic(), encoding="utf-8"
    )
    if board:
        (root / "my-board.kicad_pcb").write_text(BOARD, encoding="utf-8")
    return root
