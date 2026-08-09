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

**klm bundles no correction data, and the primary key is the part, not the footprint.**
[Q3](14-open-questions.md#q3) was checked and both halves of the original plan had to change — see
[ADR-0011](adr/0011-rotation-corrections-are-learned-not-bundled.md). In short: the one community
table everything else copies is GPL-3.0 while klm is MIT, and it is keyed by footprint name, but
the correct orientation is a property of how the part sits in its *reel*. Two parts on one 0603 land
pattern can need different rotations, and a footprint-keyed table answers that confidently and
sometimes wrongly.

klm keys on the part, which it can do because it has a catalog:

```sql
-- part_rotation_correction        the specific key, and the one a board confirms
klm_id                     rotation  offset_x  offset_y  source     confirmed_at
'01JB4K7QW8ZR3XN5M2VYT9D'    180.0       0.0       0.0  learned    2026-08-09
```

The footprint-pattern table remains as the generalisation, for the packages you use repeatedly:

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

1. A per-part correction in `part_rotation_correction`.
2. A `KLM_FAB_ROTATION` field on the part — a manual override that travels with the design.
3. A `user` or `learned` pattern entry.
4. A `bundled` pattern entry. **klm ships none**; a user who has validated a table of their own can
   load one and have it rank below their learned values.
5. No correction.

Ties between patterns are broken by length, so `^SOIC-8_3.9x4.9mm` beats `^SOIC-8`.

One systematic difference is *not* in the table: KiCad mirrors bottom-side components and JLCPCB
does not. That is a whole-layer transform and lives in the fab profile
(`mirror_bottom_rotation`), because putting it in a per-package table would mean repeating it in
every row.

Corrections are applied as `final_rotation = (kicad_rotation + correction) mod 360`, with offsets
applied in the footprint's rotated frame.

⚠️ **The cost of shipping nothing, stated plainly:** the first board of any package klm has not
seen is unprotected, where a bundled table would have been right most of the time. `klm fab`
reports which references carry no confirmed correction, and the package README repeats it, so the
fab's DFM preview gets a careful look. There is no stronger mitigation, and this is the
[ADR-0011](adr/0011-rotation-corrections-are-learned-not-bundled.md) trade-off in one sentence.

### Learning from real runs

This is the feature that actually solves the problem, and nothing else does it well:

```bash
klm fab feedback fab/my-board-rev-c \
    --wrong U3:180 --wrong D1:90 --confirm-rest
```

After a board comes back, record which references were placed wrong and by how much. klm:

1. Maps each reference to its footprint.
2. Derives the correction that would have made it right.
3. Writes or updates a `learned` correction **for that part**. Corrections accumulate: a part
   already corrected 90° that still came back 180° out needs 270°, and making the user do that
   arithmetic is how the second correction gets entered wrong.
4. Marks every *other* reference `confirmed` — the more valuable half, because it converts
   "untested" into "verified against a physical board", including for the parts that needed no
   correction at all. That is most of the board, and it is knowledge nothing else records.

Generalising one observation to a footprint pattern is offered as `--generalize`, not done
automatically: one board is one data point about one reel.

The mapping from reference to part comes from the package's own `manifest.json`, which records a
`klm_id` for every placement. By the time a board comes back the schematic may have moved on, so
re-reading it would be answering a question about a different design.

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

Rows are written through a real CSV writer rather than by joining strings, because a value like
`1%, 100ppm` would otherwise shift every column after it and the file would still look fine.

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

Two notes on how these are implemented. "Every non-DNP part resolves to a catalog part" and the
lint gate are scoped to **the parts this board uses** — an unrelated draft elsewhere in the catalog
missing a datasheet is not a reason to refuse to fabricate this board. And the stock check reads
*stored* offers rather than calling a supplier, for the same reason `klm lint` never touches the
network: a check whose result depends on whether TME is up fails randomly. It says so, and names
`klm refresh`.

The board-outline check is the one that earns its place: an open outline plots, uploads, and passes
the fab's own intake — the question arrives after the order is placed.

## 8. Commands

```bash
klm bom [--project .] [--variant basic] [--format csv|json]

klm fab                              # full package for the current project
klm fab --variant basic
klm fab --check                      # preflight only, writes nothing — for CI
klm fab --no-assembly                # bare boards: gerbers + drill only
klm fab --profile generic            # KiCad's own conventions, uncorrected
klm fab --no-timestamp               # byte-reproducible manifest
klm fab feedback <dir> --wrong U3:180 --confirm-rest [--generalize]
klm fab corrections list             # inspect the correction table
klm fab corrections set '^QFN-24' 270 --source user
klm fab corrections remove '^QFN-24'
```

`klm bom` deliberately needs **no KiCad and no catalog**. It reads the `.kicad_sch` files directly,
so CI can check a BOM, the clean-room verifier can use it, and a cost estimate does not depend on
whether the person asking has `kicad-cli` on their PATH. Everything else in the pipeline does shell
out, because gerbers genuinely require KiCad's own plotting code.

Without a catalog the BOM still comes out, just without MPNs. A symbol is tied to a part by
`KLM_ID` and nothing else — name matching is not attempted, because a BOM that quietly attributes a
line to the wrong part produces a wrong order and nothing downstream catches it.
