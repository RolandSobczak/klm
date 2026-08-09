# ADR-0010 — Vendoring leaves unmanaged libraries linked, and says so

**Status:** accepted · **Phase:** 4 · **Related:** [06](../06-library-sync.md),
[ADR-0007](0007-clean-room-verification.md)

## Context

[06 §3](../06-library-sync.md#klm-vendor) specifies that vendoring resolves every symbol in the
schematic to a `klm_id` and **aborts if anything is unresolved**, unless `--allow-unresolved` is
given. That rule was written with klm-managed parts in mind, and it is right for them: a symbol
whose `lib_id` says `KLM:` *is* one of the user's parts by construction, so not knowing which one
is a genuine problem, and vendoring around it produces a project silently missing a part it
believes it has.

Running it against a real board made the gap obvious. A single 3000-line schematic from the
author's own corpus references thirteen library nicknames, of which one is klm's. The rest are:

```
power                   19 symbols   #PWR01, #PWR02, …
Passive                  6 symbols   R1, R2, C1, C2, …
Mechanical / MountingHole   4        H1..H4
Connector_Generic        3           J2, J4, J5
Jumper, Connectors, Resistor_SMD, Capacitor_SMD, …
```

Nineteen of those are power flags — `GND`, `+3V3`, `+BATT`. They are not components, carry no BOM
line, and will never be catalog parts. Under the specified rule, vendoring aborts on every real
project, and the only way through is `--allow-unresolved`, which switches off the check for the
klm-managed symbols too. A safety check everyone must disable to get work done is worse than no
check, because it also disables the case it was written for.

The distinction that matters is not "resolved / unresolved". It is **whose library is this?**

## Options considered

**A. Abort on anything not vendored, as specified.** Faithful to the document. Rejected on
evidence: it makes the command unusable on every project the author has, and pushes the user to a
flag that suppresses the real errors alongside the noise.

**B. Ship a list of KiCad's standard library nicknames and treat those as fine.** Appealing —
`power`, `Device`, `Connector_Generic` and friends ship with every KiCad install, so a project
referencing them does open on a fresh clone. Rejected: the list is well over a hundred names,
changes with every KiCad release, and a name missing from it produces a *false abort* on a project
that is actually fine. It also does nothing for `Passive` and `RolandsFootprint`, which are the
author's own libraries and look identical to klm from the outside.

**C. Probe the user's installed KiCad libraries and classify by where each nickname resolves.**
Accurate on the machine it runs on, and that is the objection: it makes the outcome of vendoring
depend on the author's environment, which is precisely the trap
[ADR-0007](0007-clean-room-verification.md) exists to close.

**D. Split the two cases.** A reference inside a library klm manages that klm cannot identify is an
error and aborts. A reference to a library klm does not manage is a different fact — reported,
grouped by library, and left linked exactly as it was.

## Decision

**Option D.** `klm vendor` reports two distinct things:

- **unresolved** — a `lib_id` in `KLM:`, in the project's own vendored library, or in a library
  named with `--from-library`, that resolves to no approved catalog part. **Aborts.**
- **external** — a `lib_id` in any other library. **Reported, grouped by nickname, left linked.**

Two flags follow from it:

- `--from-library NICKNAME` says "resolve this library's symbols against the catalog by name". It
  is the adoption path: a project that predates klm references `Passive:0R_0603`, and requiring
  every symbol to be re-linked to `KLM:` by hand before vendoring would make the feature unusable
  on an existing corpus. It is a flag rather than a default because resolving *any* nickname by
  name would let `Device:R` silently claim a catalog part called `R` — the guessing klm refuses to
  do everywhere else.
- `--strict` promotes external references to errors, for anyone who wants the harder guarantee
  today.

## Consequences

**Good**
- The abort survives as a real check rather than one everybody switches off. On the corpus this was
  tested against, `klm vendor` completes and the klm-managed symbol is still guarded.
- The report is readable: thirteen grouped lines rather than fifty-two individual ones, with the
  blocking errors printed separately and in full above them.
- No list to keep current, and no dependence on what KiCad the author happens to have installed.
- `--from-library` turns out to be the feature that makes vendoring usable on projects that
  predate klm at all, which the original design had no answer for.

**Bad**
- **klm no longer claims that a vendored project is self-contained.** It cannot: `power:GND`
  resolves on a machine with KiCad installed and would not on one without KiCad's standard
  libraries, and klm has no environment-independent way to tell that from `Passive:0R_0603`. The
  guarantee is deferred to `klm verify --clean-room`
  ([ADR-0007](0007-clean-room-verification.md)), which answers it by actually resolving the project
  on a machine that has nothing. Until Phase 6 lands, `--strict` is the only in-tool answer, and it
  is a blunt one.
- A user who ignores the external report can publish a project that does not open for a
  collaborator lacking their `Passive` library. The report is printed on every run and names the
  fix, but it is a warning, and warnings get skimmed.

**Neutral**
- `klm unvendor` on a project adopted via `--from-library` restores references to `KLM:`, not to
  the original nickname. That is correct — after adoption those parts live in the catalog — but it
  means the vendor/unvendor round trip is byte-identical only for projects that were already
  klm-linked.
