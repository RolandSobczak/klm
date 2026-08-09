# ADR-0004 — Generate flat KiCad libraries; database libraries are an optional mode

**Status:** accepted · **Phase:** 0 · **Related:** [04 §5](../04-catalog-and-storage.md#5-generation)

## Context

KiCad needs to read klm's catalog. Two mechanisms exist.

**Flat libraries** — a `.kicad_sym` file and a `.pretty` directory, referenced from the library
tables. Universal, works everywhere, no configuration beyond a table row.

**Database libraries** — a `.kicad_dbl` descriptor pointing at a real database (SQLite via ODBC)
whose columns map to symbol fields. KiCad 7+ supports this natively, and since klm's catalog
*already is* a SQLite database, it looks like a perfect fit: no generation step, and edits appear
in KiCad instantly.

## Decision

**Generate flat libraries as the default.** Database library support is an optional mode for a
power user working alone.

## Rationale

The database-library route is genuinely attractive for the single-machine case and fails on
everything around it:

1. **ODBC setup.** Database libraries need a configured ODBC driver and DSN on every machine.
   On Linux that is `unixodbc` plus a SQLite driver plus a DSN file — a real support burden for a
   tool whose selling point is that it removes friction.
2. **It does nothing for sharing.** The core problem ([P6](../01-vision-and-problems.md#p6--global-libraries-and-project-libraries-pull-in-opposite-directions))
   is publishing a self-contained repository. A collaborator cannot be asked to install ODBC and
   restore a database. Vendored projects need flat files regardless, so the flat-file generator
   has to exist either way.
3. **Footprints aren't covered.** Database libraries map symbol fields; footprints still live in a
   `.pretty` directory. So the generation step is only half-eliminated.
4. **Generation is cheap.** Rebuilding `KLM.kicad_sym` for ~900 parts is sub-second and idempotent.
   The desktop app can run it after every edit with no perceptible cost.
5. **Generated files are inspectable.** When something looks wrong in KiCad, reading the generated
   `.kicad_sym` is a much shorter debugging path than reasoning about a field-mapping layer.

Against that, the one real advantage — no regeneration step, live edits — is worth little when
regeneration takes a second and happens automatically.

## The optional mode

`klm dbl generate` emits a `.kicad_dbl` descriptor mapping klm's canonical fields to the
`part` and `parameter` tables, with a view flattening them into the shape KiCad expects. Intended
for a user who works on one machine, never publishes, and wants a live view of the catalog.

Even in this mode klm still generates flat libraries, because vendoring needs them.

## Consequences

**Good**
- Zero configuration for the common case. A library table row and an environment variable.
- The same artifacts serve local use, vendoring and publication.
- Generated files can be inspected, diffed and hand-checked.
- No ODBC in the dependency story.

**Bad**
- A generation step exists and can go stale. Mitigated: generation is idempotent, runs
  automatically after catalog changes, and `klm doctor` reports a stale `generated/`.
- Two paths to maintain if the optional mode is built. Mitigated by treating it as genuinely
  optional — it can be dropped entirely without affecting anything else.

**Neutral**
- `generated/` is a build artifact and git-ignored. Losing it costs one `klm generate`.
