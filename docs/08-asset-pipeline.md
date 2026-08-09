# 08 — Asset Pipeline

Solves [P2](01-vision-and-problems.md#p2--library-assets-are-a-chore-per-part): getting a usable
symbol, footprint and 3D model without drawing each one by hand.

## 1. Sources, in priority order

For any given part, klm tries these in order and stops at the first that yields a QA-passing
asset set:

| Priority | Source | Coverage | Quality |
|---|---|---|---|
| 1 | **Already in the catalog** | Any part whose footprint/package already exists | Best — reuse is free and consistent |
| 2 | **KiCad standard libraries** | Generic passives, connectors, common ICs | Excellent, well-tested, no licensing question |
| 3 | **Generated from a template** | Parametric packages: 0402…1210, SOT-23-x, SOIC-N, QFN, LQFP | Excellent and fully deterministic |
| 4 | **EasyEDA / LCSC import** | Almost everything with an LCSC number | Variable — needs QA |
| 5 | **Hand-drawn** | Everything else | Whatever you make it |

Priority 1 is the one that matters most for library hygiene: a new 0402 resistor should never
create a new footprint asset. The pipeline checks `package` against existing assets first, and
reuse is the common case.

## 2. Symbols

Symbol generation by category:

- **Passives and two-terminal parts**: generated from a template. Pin count, names and graphics
  are determined entirely by category and package.
- **ICs**: imported from EasyEDA, then normalized — pin names uppercased, pin types corrected
  (`power_in` for VDD/VSS, `output` for outputs), units split if the source produced one
  oversized rectangle for a multi-bank device.
- **Connectors**: KiCad's standard library covers most; the rest come from a template driven by
  pin count and pitch.

Regardless of source, klm rewrites the field set to the canonical schema
([05](05-field-schema-and-linting.md)) and injects `KLM_ID`. The imported symbol's own field
names are discarded — that's the whole point.

## 3. Footprints

- **Standard packages**: preferred from KiCad's libraries, which are IPC-compliant and
  well-reviewed. A 0402 resistor gets `Resistor_SMD:R_0402_1005Metric`, not a generated one.
- **Non-standard packages**: imported from EasyEDA, then QA-checked hard (§5), because EasyEDA
  footprints vary from excellent to unusable.
- **Hand-drawn**: KiCad's own footprint editor. klm imports the result and hashes it.

Every footprint that enters the catalog has its `(model …)` path rewritten to
`${KLM_3DMODELS}/<name>.step`. An absolute path in a stored footprint is an error, not a warning
— it's the thing that makes a library unshareable.

## 4. 3D models: mesh to solid

EasyEDA supplies mesh geometry (OBJ/WRL). KiCad renders meshes fine but wants STEP for mechanical
export, interference checking and enclosure design. The conversion runs FreeCAD headless.

```
model.obj ──freecadcmd obj2step.py──▶ model.step
```

`cad/scripts/obj2step.py` runs under **FreeCAD's own Python interpreter**, not klm's virtualenv.
It can `import Mesh, Part`; it cannot import anything from klm or klm's dependencies. Its entire
contract is argv in, file out, exit code:

```python
# cad/scripts/obj2step.py — executed by freecadcmd, NOT importable from klm
import sys, Mesh, Part

def main(src, dst, tolerance=0.1):
    mesh = Mesh.Mesh(src)
    shape = Part.Shape()
    shape.makeShapeFromMesh(mesh.Topology, tolerance)
    solid = Part.makeSolid(shape)
    solid.exportStep(dst)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2],
                  float(sys.argv[3]) if len(sys.argv) > 3 else 0.1))
```

Practical notes:

- **The mesh must be watertight** for `makeSolid` to produce a valid solid. Meshes with holes
  yield a shell that KiCad renders but mechanical tools reject. The QA gate checks this rather
  than letting a broken solid into the catalog.
- **Tolerance is a trade-off**: too tight produces enormous files, too loose loses features.
  0.1 mm is a reasonable default; it's configurable per conversion.
- **Conversion is slow** — seconds to tens of seconds per model — so it's a background job with
  progress, and results are cached by source-mesh hash.
- **`freecadcmd` availability is checked at startup.** Its absence degrades klm to mesh-only
  models rather than failing.
- Models are placed at the origin with KiCad's expected axis convention; offset/rotation/scale
  are recorded on the footprint's `(model …)` node, not baked into the geometry.

## 5. The QA gate

Nothing enters the catalog as `approved` without passing. This is the difference between an
automated pipeline and an automated mess.

### Symbol checks

| Check | Severity |
|---|---|
| Pin count matches the package's expected pin count | error |
| No duplicate pin numbers | error |
| Pin electrical types are set (not all `passive` by default) | warning |
| Power pins identified as `power_in` | warning |
| Pins on a 1.27 mm (50 mil) grid | error — off-grid pins make wiring impossible |
| Reference designator prefix matches the category | warning |
| Required fields present | error |

### Footprint checks

| Check | Severity |
|---|---|
| Pad count matches the symbol's pin count | error |
| Pad numbers correspond 1:1 with pin numbers | error |
| Courtyard layer present and closed | error |
| Silkscreen does not overlap pads | warning |
| Pin-1 marker present | warning |
| Fabrication-layer reference and value present | warning |
| Pad sizes within IPC density-level bounds for the package | warning |
| Overall dimensions within tolerance of the datasheet package | warning (needs datasheet parameters) |

### 3D model checks

| Check | Severity |
|---|---|
| File parses as valid STEP | error |
| Solid is watertight | warning |
| Bounding box matches the footprint courtyard within tolerance | warning |
| File size below a threshold (default 10 MB) | warning |
| Model is positioned at the footprint origin | warning |

The report is stored on the asset (`qa_status`, `qa_report`) so it's visible later, and shown as
a checklist in the review UI. Warnings don't block approval; errors do, unless explicitly
overridden with a recorded reason.

## 6. Licensing

Imported assets carry a provenance and a licensing question that a project intended for GitHub
cannot ignore.

- Every asset records `source` and, where known, `license_note`.
- **KiCad's standard libraries** are permissively licensed with an explicit exception allowing
  use in designs — the safest source, another reason for priority 2.
- **EasyEDA-derived assets** have an unclear redistribution status. klm records the origin and
  surfaces a warning when vendoring a project containing them into a repository intended for
  publication. It does not attempt to give legal advice, and this is flagged in
  [14 — Open questions](14-open-questions.md#q4).
- `klm licenses --project .` lists every asset with a non-permissive or unknown origin, so the
  question can at least be answered deliberately.

## 7. Commands

```bash
klm part add --lcsc C8734                 # full pipeline from an LCSC number
klm part add --mpn STM32F103C8T6 --mfr ST # resolve via suppliers, then assets
klm part add --interactive                # guided; asks at each ambiguity

klm assets acquire <klm_id>               # (re)run acquisition for an existing part
klm assets qa <klm_id>                    # re-run the QA gate
klm assets convert-3d <path.obj>          # one-off mesh → STEP
klm assets reuse-check                    # find near-duplicate footprints in the catalog
```

`reuse-check` deserves a mention: it compares footprints by pad geometry rather than name and
reports candidates for merging. Catalog hygiene decays silently otherwise, and a duplicate
footprint is the kind of thing you only notice when two identical parts order differently.
