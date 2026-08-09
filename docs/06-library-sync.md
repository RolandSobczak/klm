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
      "footprint_name": "LQFP-48_7x7mm_P0.5mm",
      "model_name": null,
      "global_symbol_hash":   "sha256:3f9a…",
      "global_footprint_hash":"sha256:71c4…",
      "global_model3d_hash":  "sha256:aa02…",
      "vendored_symbol_hash": "sha256:5c81…",
      "vendored_footprint_hash":"sha256:e30d…",
      "vendored_model3d_hash": null,
      "references": ["U1"]
    }
  ]
}
```

Recording *both* the global hash at vendoring time and the vendored hash is what lets `klm sync
status` distinguish "the global catalog moved on" from "someone edited the project copy" from
"both changed" — the three cases need different handling and conflating them is how sync tools
lose data.

The two hashes for the same part are normally **different**, and that is not a bug: the vendored
symbol has been renamed and had the canonical field schema imposed on it, and the vendored
footprint's `(model …)` path points inside the project. Only the recorded-vs-current comparison
on each side means anything; comparing the global hash against the vendored one does not.

`symbol_name` is `null` for a **footprint-only** entry — a mounting hole placed on the board and
never on a sheet. `model_name` is `null` unless the project was vendored with `--with-3d`.

`vendored_at` is preserved across a re-vendor that changes nothing else, so an unchanged project
produces a zero-byte diff by default; `--no-timestamp` omits the field entirely.

The lock file is committed. It is the contract between the repository and the catalog.

## 3. Operations

### `klm vendor`

Global → project. Makes a linked project self-contained.

```
1. Parse .kicad_sch, collect every symbol instance and its lib_id.
2. Resolve each to a klm_id:
      a. read the KLM_ID field on the instance          ← authoritative
      b. else the name the lock file recorded           ← survives a rename
      c. else match lib_id name against the catalog     ← fallback
      d. else record as unresolved                      ← reported, not guessed
3. Abort if anything is unresolved, unless --allow-unresolved.
4. Collect the transitive asset set (symbols, footprints, 3D models).
5. Write libraries/<name>.kicad_sym  from the symbol assets.
   Write libraries/<name>.pretty/    from the footprint assets.
   Write libraries/packages3d/       if --with-3d.
6. Rewrite footprint (model …) paths → ${KIPRJMOD}/libraries/packages3d/<file>.step
7. Rewrite .kicad_sch lib_id:  KLM:X → <name>:X
   Rewrite .kicad_sch Footprint field: KLM:Y → <name>:Y
   Stamp KLM_ID onto each placed symbol   ← see below
   Rewrite .kicad_pcb footprint refs:  KLM:Y → <name>:Y
8. Write project sym-lib-table and fp-lib-table (${KIPRJMOD}-relative).
9. Write klm.lock.json.
10. Set project mode = vendored.
```

Steps 7–8 are where the lossless round-trip rule ([03 §3](03-architecture.md#3-the-lossless-round-trip-rule))
earns its keep. Only `lib_id` nodes, the `Footprint` field value, and `(model …)` paths are
touched; every other node in the schematic and board is re-serialized byte-identically.

**Step 7 stamps `KLM_ID` onto the schematic instances**, which is not cosmetic. A board built with
klm gets the field for free — KiCad copies a library symbol's fields onto an instance when it is
placed. A board that *predates* klm does not, and vendoring alone does not fix it, so every feature
that identifies a part by `KLM_ID` — the BOM, ordering, cost reporting — finds nothing on exactly
the projects a user already has. Vendoring already resolved the identity to rewrite the `lib_id`;
writing it down costs nothing and is the difference between those boards working and not.

Everything is written to a staging directory and moved into place atomically, so an interrupted
vendor leaves the project untouched.

```bash
klm vendor                        # current project, no 3D models
klm vendor --with-3d              # include STEP files
klm vendor --dry-run              # print the plan and the diff
klm vendor --name shared-libs     # override the library name
klm vendor --from-library Passive # also resolve this library against the catalog
klm vendor --strict               # refuse any symbol klm does not manage
```

### Which references vendoring claims — and which it only reports

Step 2 above applies to libraries klm manages: `KLM:`, the project's own vendored library, and any
nickname named with `--from-library`. A `lib_id` from **any other** library is a different fact.
`power:GND`, `Device:R` and `Connector_Generic:Conn_01x02` were never klm parts, and one real
schematic carries dozens of them — aborting there would make the command unusable and push every
user to `--allow-unresolved`, switching off the check for the klm symbols too.

So the two are reported separately: **unresolved** aborts, **external** is grouped by nickname,
left linked, and printed. `--strict` promotes external to an error.

The consequence has to be stated plainly: **`klm vendor` alone does not establish that a project
is self-contained.** Answering that requires resolving the project on a machine that has nothing,
which is `klm verify --clean-room` in [Phase 6](13-roadmap.md#phase-6--repository-scaffolding-and-ci).
See [ADR-0010](adr/0010-vendoring-leaves-unmanaged-libraries-linked.md).

`--from-library` is also the **adoption path**: a project that predates klm references its own
`Passive:0R_0603`, and re-linking every symbol to `KLM:` by hand before vendoring is not a
reasonable ask. It is a flag rather than a default because resolving any nickname by name would let
`Device:R` silently claim a catalog part called `R`.

### `klm unvendor`

Project → global-linked. The exact inverse: rewrite `lib_id`s back to `KLM:`, delete
`libraries/`, remove klm's rows from the project lib tables, set mode to `linked`. Refuses to run
if any vendored part has drifted from the catalog, so local work can't be silently destroyed;
`--force` says the loss was deliberate.

Two details worth stating. The lib *tables* are the user's files and may name libraries klm knows
nothing about, so only klm's row is removed — the file is deleted just when nothing else is left in
it. And a project adopted via `--from-library Passive` comes back as `KLM:`, not as `Passive:`,
because after adoption those parts live in the catalog; the round trip is byte-identical only for a
project that was already klm-linked.

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
| in step, but retired upstream | — | `deprecated-upstream` | Warning, not an error |

A symbol sitting in the vendored library with **no lock entry at all** is also `orphan` — that is
the collaborator case, and it is the one the state exists for. A comparison against an asset the
entry never had is skipped rather than read as a change: a part vendored without a 3D model has
not lost one.

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

### `klm sync diff`

`status` says *that* something moved; `diff` says what. It prints the same pair of comparisons the
table above is built from — **each side against its own recorded state**, never one against the
other:

```
$ klm sync diff USB-C-16P
USB-C-16P  [conflict]
  catalog: symbol; project: footprint

~ catalog: symbol USB-C-16P
--- USB-C-16P (recorded)
+++ USB-C-16P (now)
@@ …
✓ catalog: footprint USB_C_Receptacle_16P — unchanged
✓ project: symbol USB-C-16P — unchanged
~ project: footprint USB_C_Receptacle_16P
--- USB_C_Receptacle_16P (as vendored)
+++ USB_C_Receptacle_16P (now)
@@ …
```

**The catalog asset and the vendored one are never diffed against each other**, however natural
that screen sounds. §5 is why: the vendored symbol was renamed and re-fielded on the way in and
its footprint points inside the project, so those two differ permanently and by design. A diff
that never empties is one its reader learns to skip.

The catalog side comes straight out of the content-addressed store — the recorded asset is still
there, because assets are immutable. The project side is harder: klm records the vendored copy's
*hash*, never its bytes. So the "before" is rebuilt through `build_library` — the same function
that wrote it — from the catalog assets the lock recorded, and then checked against the recorded
hash. **If the rebuild does not reproduce that hash, it is reported as unavailable rather than
shown.** It happens for real: correct an MPN and the symbol's fields change, so the copy as
vendored is no longer reconstructible. A "before" assembled from today's fields would look right
and invite someone to resolve a conflict that is not there.

Both sides are canonicalised before diffing, so the text shows what the *hash* saw. Without that,
a reformat that changed no content would print a screen of whitespace next to a verdict of
"unchanged" — the kind of contradiction that costs a tool its credibility.

A 3D model is compared by hash and not shown: STEP is binary, and a diff of it would be noise.

### `klm sync pull`

Bring `global-ahead` changes into the vendored project. Re-copies the assets, rewrites paths,
updates the lock file. Refuses on `conflict` unless a strategy is given.

### `klm sync push` / `klm promote`

Project → global. Two related operations:

- **`sync push`** takes a `project-ahead` part and updates the catalog's asset for it. Used when
  you fixed a footprint while working on a board and want the fix to be global.
- **`promote`** takes an `orphan` part (one that exists only in the project — typically added by
  a collaborator) and creates a catalog entry for it. This runs it through the same validation
  as any new part: field schema and asset QA. It lands as `draft`, requiring approval.

Both are the mechanism by which collaboration flows back into the catalog rather than being lost.

Two things `push` has to get right, both of which would be invisible if wrong:

- The vendored footprint's `(model …)` path points at `${KIPRJMOD}/libraries/packages3d/…`.
  Storing that as a catalog asset would hand every other project a path that resolves only inside
  this one, so it is rewritten back to `${KLM_3DMODELS}` on the way in.
- Pushing a footprint fix is not a reason to re-approve the part, so its status is left alone —
  but it *is* a reason to re-run the QA gate, which `push` does.

`promote` also writes the new `KLM_ID` into the project's copy of the symbol. Without that the
symbol reads as an orphan again on the next `sync status` and nothing ever converges.

### `klm sync resolve`

Interactive conflict resolution: for each conflicting part, report which assets moved on each side
and offer `use-global` / `use-project` / `skip`. The answers are then applied as a `sync pull` and
a `sync push` over the two selected sets. Non-interactively — and in CI, where there is no terminal
— `--strategy prefer-global|prefer-project` applies one answer uniformly, and running without it
outside a terminal is an error rather than a silent default.

`open-in-editor` from the original sketch is not implemented. It needs an editor to launch and a
temporary checkout of both sides to launch it on, which is a Phase 8 concern; the diff belongs in
the desktop app's sync screen, where it can be shown side by side.

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
