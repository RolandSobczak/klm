# 09 — Manufacturing Outputs

Solves [P5](01-vision-and-problems.md#p5--fabrication-output-needs-hand-fixing). Target fab is
JLCPCB; the design keeps a generic layer underneath so another house can be added.

## 1. What a fab package contains

```
fab/my-board-rev-c/
├── gerbers.zip           # copper, mask, silk, paste, edge cuts
├── drill/                # Excellon, plated + non-plated
├── bom.csv               # JLCPCB assembly BOM
├── cpl.csv               # pick-and-place, corrections applied
├── drc-report.txt        # must be clean before the package is written
├── manifest.json         # inputs, tool versions, corrections applied, hashes
└── README.txt            # human summary: layer count, thickness, finish, order notes
```

`manifest.json` is what makes a fabrication run reproducible and, more importantly, *diagnosable*
six weeks later when a board comes back wrong. It records the source commit, `kicad-cli` version,
klm version, and every rotation correction applied to every reference designator.

## 2. Generation pipeline

```
1. Preflight
     - project is committed (dirty tree → warn, --allow-dirty to override)
     - run ERC via kicad-cli; errors block
     - run DRC via kicad-cli; errors block
     - klm lint --project .; errors block
2. Gerbers + drill      kicad-cli pcb export gerbers / drill
3. Raw CPL              kicad-cli pcb export pos  (CSV, mm, top/bottom split as needed)
4. BOM                  from the schematic, grouped, with LCSC numbers
5. Apply corrections    rotation + offset per footprint pattern
6. Format for the fab   column names, units, layer naming the house expects
7. Write manifest, zip
```

Steps 1–4 are generic. Steps 5–6 are house-specific and live behind a `FabProfile`.

## 3. The rotation problem

The most persistent practical annoyance in JLCPCB assembly. JLCPCB's pick-and-place expects a
component orientation convention that differs from KiCad's footprint convention for many package
types, so a board assembles with parts rotated 90° or 180° — most visibly on polarized parts,
where it's a scrapped board rather than a cosmetic issue.

klm's model is a correction table keyed by footprint name pattern:

```sql
-- rotation_correction
pattern                  rotation  offset_x  offset_y  source     confirmed_at
'^SOT-23-3'                 180.0       0.0       0.0  bundled    NULL
'^SOT-23-6'                 180.0       0.0       0.0  bundled    NULL
'^C_0402_'                    0.0       0.0       0.0  bundled    NULL
'^SOIC-8_3.9x4.9mm'         270.0       0.0       0.0  learned    2026-07-14
'^LED_0603'                 180.0       0.0       0.0  learned    2026-06-02
```

Resolution order, most specific first:

1. Per-part override (a `KLM_FAB_ROTATION` field on the part).
2. User table entry (`source = 'user'` or `'learned'`).
3. Bundled table entry (`source = 'bundled'`).
4. No correction.

Corrections are applied as `final_rotation = (kicad_rotation + correction) mod 360`, with offsets
applied in the footprint's rotated frame.

⚠️ The exact per-package correction values are empirical community knowledge, not a published
specification, and they change. The bundled table is a **starting point that must be validated**,
not an authority. See [14 — Open questions](14-open-questions.md#q3).

### Learning from real runs

This is the feature that actually solves the problem, and nothing else does it well:

```bash
klm fab feedback fab/my-board-rev-c \
    --wrong U3:180 --wrong D1:90 --confirm-rest
```

After a board comes back, record which references were placed wrong and by how much. klm:

1. Maps each reference to its footprint.
2. Derives the correction that would have made it right.
3. Writes or updates a `learned` rotation-correction row, generalized to the footprint pattern.
4. Marks every *other* reference's footprint as `confirmed` — which is the more valuable half,
   because it converts "untested" into "verified against a physical board".

The next board with a SOT-23-6 is right the first time. Over a handful of runs, the table becomes
genuinely trustworthy for the packages you actually use — which is a much smaller set than the
universe of packages, and exactly the set that matters.

## 4. The assembly BOM

JLCPCB's assembly BOM groups by part and needs LCSC part numbers.

| Column | Source |
|---|---|
| `Comment` | canonical `Value` |
| `Designator` | comma-separated references |
| `Footprint` | footprint name |
| `LCSC Part #` | the `LCSC` field |

Preflight checks specific to assembly:

- Every non-DNP SMD part has an `LCSC` value (lint rule P004).
- Every LCSC part has stock at the required quantity — checked live, because ordering assembly
  for an out-of-stock part wastes the whole run.
- Parts flagged "extended" vs "basic" in JLCPCB's library are reported, since extended parts
  carry a per-feeder setup fee that can dominate a small order's cost. Where a basic-part
  equivalent exists in the catalog, klm suggests it.

DNP handling: KiCad 7+ has a native DNP flag on symbols. klm honours it, excludes DNP parts from
the assembly BOM and CPL, and lists them separately in the README so nothing silently disappears.

## 5. Variants

A board often ships in configurations — populated vs depopulated options, alternate values.

```toml
# in the project's klm.toml
[variants.basic]
dnp = ["U5", "J3", "R17"]

[variants.full]
dnp = []
overrides = { R4 = "10k" }
```

`klm fab --variant basic` produces a package for that configuration. Variants also flow into
ordering ([10](10-ordering-and-inventory.md)), so a build plan can specify how many of each.

## 6. Fab profiles

House-specific formatting lives in a profile, so supporting another fab is a data change:

```toml
[fab.jlcpcb]
gerber_protel_extensions = true
drill_merge_pth_npth = false
cpl_columns = ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
cpl_units = "mm"
cpl_layer_names = { top = "Top", bottom = "Bottom" }
bom_columns = ["Comment", "Designator", "Footprint", "LCSC Part #"]
apply_rotation_corrections = true
```

The generic pipeline plus a profile is what keeps house-specific quirks out of the core.

## 7. Preflight, in full

The package is not written unless these pass:

| Check | Blocks? |
|---|---|
| ERC clean | yes |
| DRC clean | yes |
| `klm lint --project .` has no errors | yes |
| Every non-DNP part resolves to a catalog part | yes |
| Every footprint has a courtyard | yes |
| Assembly parts have LCSC numbers | yes, when assembly is requested |
| Assembly parts are in stock | warning |
| Git tree is clean | warning |
| Board outline is a single closed polygon | yes |
| Silkscreen over pads | warning |

`klm fab --check` runs preflight alone, which is what you want in CI on every push.

## 8. Commands

```bash
klm fab                              # full package for the current project
klm fab --variant basic
klm fab --check                      # preflight only
klm fab --no-assembly                # bare boards: gerbers + drill only
klm fab feedback <dir> --wrong U3:180 --confirm-rest
klm fab corrections list             # inspect the correction table
klm fab corrections set '^QFN-24' 270 --source user
```
