# 06 — Library Sync: Global ↔ Project

Solves [P6](01-vision-and-problems.md#p6--global-libraries-and-project-libraries-pull-in-opposite-directions).
This is the hardest problem in the project and the one with the least prior art, so it gets the
most detail.

## 1. The conflict

Two legitimate requirements are directly opposed:

**Working style demands global libraries.** Each feature gets prototyped on its own small board.
A part proven on the buck-converter test board must be instantly available on the next board
with no copying. Global libraries make this free.

**Publishing demands project-local libraries.** A collaborator who clones the repository must
open the project and see correct symbols and footprints, without installing your library set or
editing their KiCad configuration. That requires the parts to live *inside* the repository, with
project-level library tables and relative paths.

Doing both by hand means every part exists twice, and the copies silently diverge.

## 2. The model: two modes and a lock file

A project is in one of two modes.

### `linked` — the working mode

The project uses the global libraries. `lib_id`s look like `KLM:STM32F103C8T6`. Nothing is copied.
Adding a part to the global catalog makes it immediately usable. This is the default and where
90% of a project's life is spent.

### `vendored` — the publishable mode

The project contains everything it needs:

```
my-board/
├── my-board.kicad_pro
├── my-board.kicad_sch
├── my-board.kicad_pcb
├── sym-lib-table          # project-level, ${KIPRJMOD}-relative
├── fp-lib-table
├── klm.lock.json          # what was vendored, at which hashes
└── libraries/
    ├── my-board.kicad_sym
    ├── my-board.pretty/
    └── packages3d/        # optional; omitted by default
```

`lib_id`s are rewritten from `KLM:STM32F103C8T6` to `my-board:STM32F103C8T6`. Footprint 3D model
paths are rewritten to `${KIPRJMOD}/libraries/packages3d/…`. A fresh clone opens correctly with
no setup.

### The lock file

`klm.lock.json` is what makes this reversible and auditable rather than a one-way export:

```json
{
  "format_version": 1,
  "vendored_at": "2026-08-09T10:14:22Z",
  "klm_version": "0.4.1",
  "library_name": "my-board",
  "include_3d": false,
  "parts": [
    {
      "klm_id": "01JB4K7QW8ZR3XN5M2VYT9DCFA",
      "mpn": "STM32F103C8T6",
      "symbol_name": "STM32F103C8T6",
      "global_symbol_hash":   "sha256:3f9a…",
      "global_footprint_hash":"sha256:71c4…",
      "global_model3d_hash":  "sha256:aa02…",
      "vendored_symbol_hash": "sha256:3f9a…",
      "vendored_footprint_hash":"sha256:71c4…",
      "references": ["U1"]
    }
  ]
}
```

Recording *both* the global hash at vendoring time and the vendored hash is what lets `klm sync
status` distinguish "the global catalog moved on" from "someone edited the project copy" from
"both changed" — the three cases need different handling and conflating them is how sync tools
lose data.

The lock file is committed. It is the contract between the repository and the catalog.

## 3. Operations

### `klm vendor`

Global → project. Makes a linked project self-contained.

```
1. Parse .kicad_sch, collect every symbol instance and its lib_id.
2. Resolve each to a klm_id:
      a. read the KLM_ID field on the instance          ← authoritative
      b. else match lib_id name against the catalog     ← fallback
      c. else record as unresolved                      ← reported, not guessed
3. Abort if anything is unresolved, unless --allow-unresolved.
4. Collect the transitive asset set (symbols, footprints, 3D models).
5. Write libraries/<name>.kicad_sym  from the symbol assets.
   Write libraries/<name>.pretty/    from the footprint assets.
   Write libraries/packages3d/       if --with-3d.
6. Rewrite footprint (model …) paths → ${KIPRJMOD}/libraries/packages3d/<file>.step
7. Rewrite .kicad_sch lib_id:  KLM:X → <name>:X
   Rewrite .kicad_sch Footprint field: KLM:Y → <name>:Y
   Rewrite .kicad_pcb footprint refs:  KLM:Y → <name>:Y
8. Write project sym-lib-table and fp-lib-table (${KIPRJMOD}-relative).
9. Write klm.lock.json.
10. Set project mode = vendored.
```

Steps 7–8 are where the lossless round-trip rule ([03 §3](03-architecture.md#3-the-lossless-round-trip-rule))
earns its keep. Only `lib_id` nodes, the `Footprint` field value, and `(model …)` paths are
touched; every other node in the schematic and board is re-serialized byte-identically.

Everything is written to a staging directory and moved into place atomically, so an interrupted
vendor leaves the project untouched.

```bash
klm vendor                       # current project, no 3D models
klm vendor --with-3d             # include STEP files
klm vendor --dry-run             # print the plan and the diff
klm vendor --name shared-libs    # override the library name
```

### `klm unvendor`

Project → global-linked. The exact inverse: rewrite `lib_id`s back to `KLM:`, delete
`libraries/` and the project lib tables, set mode to `linked`. Refuses to run if any vendored
part has drifted from the catalog, so local work can't be silently destroyed.

### `klm sync status`

Reports drift without changing anything. For each part in the lock file, compare three hashes —
the recorded global hash, the *current* global hash, and the current vendored hash:

| Recorded vs current global | Recorded vs current vendored | State | Meaning |
|---|---|---|---|
| same | same | `clean` | Nothing to do |
| **differs** | same | `global-ahead` | Catalog improved; project can pull |
| same | **differs** | `project-ahead` | Someone edited the project copy |
| **differs** | **differs** | `conflict` | Both changed; needs a decision |
| — | part absent | `missing` | Vendored library lost a part |
| part absent from catalog | — | `orphan` | Project has a part the catalog doesn't |

```
$ klm sync status
  clean          38 parts
  global-ahead    2 parts
    STM32F103C8T6   footprint changed in catalog (thermal pad added)
    AMS1117-3.3     symbol changed in catalog (pin names corrected)
  project-ahead   1 part
    USB-C-16P       footprint edited locally
  orphan          1 part
    TPS62840        added to project by a collaborator, not in catalog

3 parts need attention.  See: klm sync pull | klm sync push | klm promote
```

`--exit-code` makes it CI-usable: non-zero when anything is not `clean`.

### `klm sync pull`

Bring `global-ahead` changes into the vendored project. Re-copies the assets, rewrites paths,
updates the lock file. Refuses on `conflict` unless a strategy is given.

### `klm sync push` / `klm promote`

Project → global. Two related operations:

- **`sync push`** takes a `project-ahead` part and updates the catalog's asset for it. Used when
  you fixed a footprint while working on a board and want the fix to be global.
- **`promote`** takes an `orphan` part (one that exists only in the project — typically added by
  a collaborator) and creates a catalog entry for it. This runs it through the same validation
  as any new part: field schema, asset QA, offer lookup. It lands as `draft`, requiring approval.

Both are the mechanism by which collaboration flows back into the catalog rather than being lost.

### `klm sync resolve`

Interactive conflict resolution: for each conflicting part show a diff of the global asset
against the vendored one and offer `use-global` / `use-project` / `skip` / `open-in-editor`.
Non-interactively, `--strategy prefer-global|prefer-project` applies uniformly.

## 4. Why `KLM_ID` is load-bearing

Sync depends entirely on being able to answer "which catalog part is this schematic symbol?"
after arbitrary editing. Candidate keys:

| Key | Survives rename? | Survives collaborator edit? | Survives MPN correction? |
|---|---|---|---|
| Symbol name | ✗ | ✗ | ✓ |
| MPN | ✓ | usually | ✗ |
| Content hash | ✗ | ✗ | ✓ |
| **`KLM_ID` field** | ✓ | ✓ | ✓ |

`KLM_ID` is written into every generated symbol as a hidden field. It costs a few bytes per symbol
and makes every subsequent operation exact rather than heuristic. Name-based fallback exists only
for parts that predate klm.

The failure mode to guard against: a collaborator copies a symbol to make a variant, and now two
different parts share a `KLM_ID`. `klm lint` rule R003 detects this (`same KLM_ID, divergent
content`) and prompts to fork a new identity.

## 5. Determinism

Vendored files land in a git repository, so a re-vendor with no changes must produce a zero-byte
diff. Requirements:

- Symbols emitted in a stable order (by symbol name).
- Fields within a symbol emitted in canonical schema order, not insertion order.
- Fixed float precision. KiCad writes `1.27` and `1.270000` interchangeably; klm always writes
  the canonical form.
- No timestamps in generated library files. The only timestamp is `vendored_at` in the lock file,
  and `--no-timestamp` suppresses even that for reproducible-build workflows.
- Footprint files copied byte-for-byte from the content-addressed store, with only the `(model …)`
  path rewritten.

Test: vendor, commit, vendor again, `git diff --exit-code` must pass.

## 6. Collaboration workflow

The intended end-to-end flow for publishing a board and taking contributions back:

```
  You                                     Collaborator
  ───                                     ────────────
  klm vendor
  git commit && git push
                            ────────▶     git clone
                                          opens in KiCad — works, no setup
                                          edits schematic, adds TPS62840
                                          git push
  git pull                ◀────────
  klm sync status
    → orphan: TPS62840
  klm promote TPS62840
    → draft part created, assets imported,
      offers looked up, QA run
  klm lint && review && approve
  klm sync status → clean
```

The collaborator never installs klm. They see a normal, self-contained KiCad project. All klm
machinery stays on your side. That constraint drove the design: **a vendored project must be a
valid, ordinary KiCad project with no klm-specific requirements** — `klm.lock.json` is inert
metadata a non-klm user can ignore entirely.

**Verifying it actually worked** is a separate problem, because your own machine is the one place
that cannot test it: your global libraries are registered and your environment variables are set,
so a half-vendored project opens fine for you and breaks for everyone else. The check therefore
runs on a clean CI machine with no catalog and no KiCad configuration — see
[15 — Scaffolding and CI](15-project-scaffolding-and-ci.md) and
[ADR-0007](adr/0007-clean-room-verification.md).

## 7. Edge cases and how they're handled

| Case | Handling |
|---|---|
| Part used in the PCB but not the schematic (mechanical footprint, mounting hole) | Vendor scans the PCB too; footprint-only parts are vendored without a symbol |
| Two parts vendor to the same symbol name | Suffix with a disambiguator and record it in the lock file |
| A part is deprecated in the catalog after vendoring | `sync status` reports `deprecated-upstream` (warning, not error) with the successor if one is recorded |
| Project vendored with `--with-3d`, later re-vendored without | 3D directory removed, model paths dropped from footprints; reported explicitly |
| Vendored library hand-edited in KiCad's symbol editor | Detected as `project-ahead`. `sync push` moves it into the catalog |
| The same part vendored into two projects, then fixed in one | `sync push` from project A, `sync pull` in project B |
| Lock file missing but `libraries/` present | `klm sync adopt` reconstructs the lock by hashing and matching against the catalog |
| Merge conflict in `klm.lock.json` | It's JSON with one object per part, sorted by `klm_id` — conflicts are per-part and readable. `klm sync adopt` can rebuild it from scratch if the merge is hopeless |

## 8. What is deliberately not supported

- **Partial vendoring.** All-or-nothing per project. A half-vendored project is a support burden
  with no real use case.
- **Automatic conflict merging.** Merging two versions of a footprint automatically is not
  reliably possible. klm reports and asks.
- **Vendoring from one project into another.** Global catalog is always the hub. Project-to-project
  goes via `promote` then `vendor`.
- **Watching the filesystem to auto-sync.** Sync is always an explicit, reviewable command.
  Silent background mutation of a user's schematic is exactly the failure this tool exists to prevent.
