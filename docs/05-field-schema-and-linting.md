# 05 — Field Schema and Linting

Solves [P3](01-vision-and-problems.md#p3--field-names-drift-so-boms-never-line-up): the reason
BOMs never line up is that the same fact is spelled four ways across a library.

## 1. The canonical field set

KiCad reserves four field names. klm defines the rest.

### Reserved by KiCad

| Field | Notes |
|---|---|
| `Reference` | `R`, `C`, `U`, `J` … — the designator prefix |
| `Value` | Normalized display value; for ICs, the MPN |
| `Footprint` | `LIB:name` — must resolve in the active lib tables |
| `Datasheet` | URL |

### Required by klm on every part

| Field | Example | Why |
|---|---|---|
| `KLM_ID` | `01JB4K7QW8ZR3XN5M2VYT9DCFA` | The join key. Invisible in the schematic; makes sync possible ([06](06-library-sync.md)) |
| `MPN` | `STM32F103C8T6` | Ordering, dedup |
| `Manufacturer` | `STMicroelectronics` | Disambiguates MPNs |
| `Description` | `ARM Cortex-M3, 64kB flash, LQFP-48` | Human BOM readability |

### Required where applicable

| Field | Applies to | Example |
|---|---|---|
| `LCSC` | Anything intended for JLCPCB assembly | `C8734` |
| `TME` | Anything buyable at TME | `STM32F103C8T6` |
| `Package` | Everything physical | `LQFP-48`, `0402` |
| `Tolerance` | Passives | `1%` |
| `Voltage` | Capacitors, some passives | `16V` |
| `Power` | Resistors | `0.1W` |
| `Dielectric` | Ceramic capacitors | `X7R` |

`LCSC` is spelled exactly that way because the JLCPCB assembly toolchain and community scripts
look for it. Deviating costs interoperability for no gain.

### Reserved namespace

Anything beginning `KLM_` belongs to klm. Anything else the user adds is preserved untouched but
must be declared in `config.toml` under `[fields.custom]` or the linter flags it as unknown —
which is how typos get caught.

## 2. The alias map

Real libraries contain historical spellings. The alias map maps them to canonical names:

```toml
[fields.aliases]
MPN = ["mpn", "Manufacturer_Part_Number", "Manufacturer Part Number",
       "Part Number", "PartNumber", "MFR_PN", "MFR#", "Mfg Part #"]
Manufacturer = ["manufacturer", "MFR", "Mfg", "Vendor", "Producent"]
LCSC = ["LCSC Part #", "LCSC_PN", "JLCPCB Part", "lcsc"]
TME = ["TME Symbol", "TME_PN", "tme"]
Package = ["package", "Case", "Case/Package", "Obudowa"]
Tolerance = ["tolerance", "Tol", "Tolerancja"]
```

Matching is case-insensitive and ignores spaces, underscores and hyphens, so `mfr_pn`, `MFR PN`
and `MfrPn` all collapse to one lookup. Polish-language aliases are included because they show up
in libraries scavenged from local sources.

`klm lint --fix` renames aliased fields to canonical form. This is a pure rename — no value is
touched — which is why it's safe to apply mechanically.

## 3. Value normalization

Values carry semantics; the display form is derived. See [02 §7](02-domain-model.md#7-units-and-normalization)
for the parser. The formatter's rules:

| Input | Stored | Displayed |
|---|---|---|
| `100n`, `0.1uF`, `100 nF`, `100NF` | `1e-7 F` | `100nF` |
| `4k7`, `4700`, `4.7k`, `4K7Ω` | `4700 Ω` | `4.7k` |
| `1R2`, `1.2R`, `1.2 ohm` | `1.2 Ω` | `1R2` |
| `10u`, `10µF`, `10uF` | `1e-5 F` | `10µF` |

Formatting choices (engineering notation, 1–3 significant figures, `µ` not `u`, R-notation for
sub-10 Ω resistors) are configurable but defaulted, because consistency matters more than which
convention wins.

`klm lint --fix` rewrites `Value` to the canonical display form only when parsing is unambiguous.
Anything ambiguous is reported, never guessed.

## 4. Lint rules

Each rule has an ID, a severity, and states whether `--fix` can resolve it.

### Schema (`S`)

| ID | Severity | Rule | Fixable |
|---|---|---|---|
| S001 | error | Required field missing | no |
| S002 | warning | Field uses a known alias instead of the canonical name | **yes** |
| S003 | warning | Unknown field not declared in config | no |
| S004 | error | `KLM_ID` missing or not resolvable in the catalog | no |
| S005 | error | `KLM_ID` present but part is `deprecated` | no |
| S006 | warning | Field present but empty | no |

### Value (`V`)

| ID | Severity | Rule | Fixable |
|---|---|---|---|
| V001 | warning | `Value` not in canonical display form | **yes** |
| V002 | error | `Value` unparseable for a part whose category implies a numeric value | no |
| V003 | error | `Value` contradicts a parameter (`Value: 100nF` vs `capacitance: 1µF`) | no |
| V004 | warning | Passive missing `Tolerance` / `Voltage` / `Power` | no |

### Asset (`A`)

| ID | Severity | Rule | Fixable |
|---|---|---|---|
| A001 | error | `Footprint` doesn't resolve in the active lib tables | no |
| A002 | error | Footprint references a 3D model by absolute path | **yes** (rewrite to env var) |
| A003 | warning | No 3D model attached | no |
| A004 | error | Referenced 3D model file missing | no |
| A005 | warning | Footprint QA status is `warn` or `fail` | no |

### Sourcing (`P`)

| ID | Severity | Rule | Fixable |
|---|---|---|---|
| P001 | warning | No offer at any configured supplier | no |
| P002 | warning | All offers show zero stock | no |
| P003 | warning | Offers older than the staleness threshold (default 30 days) | **yes** (refresh) |
| P004 | error | SMD part intended for JLCPCB assembly has no `LCSC` field | no |
| P005 | warning | Lifecycle is `obsolete` or `nrnd` | no |
| P006 | warning | Datasheet URL returns a non-200 | no |

### Project (`R`)

Run against a project rather than the catalog.

| ID | Severity | Rule | Fixable |
|---|---|---|---|
| R001 | error | Schematic references a `lib_id` that resolves to nothing | no |
| R002 | error | Symbol instance fields diverge from the catalog part | **yes** (re-sync from catalog) |
| R003 | warning | Two parts in the BOM are near-duplicates (same MPN, different `KLM_ID`) | no |
| R004 | error | Vendored project's lock file disagrees with its library contents | no |

## 5. Invocation

```bash
klm lint                                   # whole catalog
klm lint --project .                       # one project's schematic + PCB
klm lint --select S,V --ignore V004        # rule selection
klm lint --fix                             # apply mechanical fixes
klm lint --fix --dry-run                   # show the diff, change nothing
klm lint --format json                     # machine-readable, for CI
klm lint --max-severity warning            # exit non-zero on warnings too
```

Exit codes: `0` clean, `1` errors found, `2` klm itself failed. Default failure threshold is
`error`; CI typically sets `--max-severity warning`.

Output is one finding per line, prefixed `file:line` where a location exists:

```
KLM.kicad_sym:1204  S002  warning  field 'Manufacturer_Part_Number' → 'MPN'                 [fixable]
KLM.kicad_sym:1889  A002  error    3D model path is absolute: /home/rs/models/sot23.step    [fixable]
catalog/01JB…FA     P004  error    SMD part marked for assembly has no LCSC field
```

## 6. Enforcement

Three layers, increasingly strict:

1. **Interactive** — the desktop app lints on save and shows findings inline.
2. **Pre-commit** — a hook in the catalog repository and in each project repository:

   ```yaml
   - repo: local
     hooks:
       - id: klm-lint
         name: klm lint
         entry: klm lint --project . --max-severity warning
         language: system
         files: \.(kicad_sch|kicad_pcb|kicad_sym|kicad_mod)$
   ```

3. **CI** — the same command, plus `klm sync status --exit-code` so a vendored project that has
   drifted from the catalog fails the build ([06](06-library-sync.md)).

## 7. Migrating an existing library

For an established library full of drift, the intended path is:

```bash
klm import --from-kicad ~/kicad-libs/MyLib.kicad_sym --status draft
klm lint --select S002,V001 --fix --dry-run     # review the mechanical renames
klm lint --select S002,V001 --fix
klm lint                                        # what remains needs human decisions
```

Order matters: fix aliases first (mechanical, safe), then values (mostly mechanical), then work
the remaining errors by hand. Attempting the manual work first means redoing it after the
mechanical pass shuffles field names.
