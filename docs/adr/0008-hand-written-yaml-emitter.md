# ADR-0008 — klm owns its YAML emitter rather than depending on a library

**Status:** accepted · **Phase:** 0 · **Related:** [ADR-0001](0001-sqlite-as-source-of-truth.md),
[04 §3](../04-catalog-and-storage.md#3-the-yaml-export)

## Context

[ADR-0001](0001-sqlite-as-source-of-truth.md) makes a deterministic YAML export the
git-versioned mirror of the catalog. Its value depends entirely on one property:
**byte-reproducibility.** The same catalog content must always produce the same bytes. If it
doesn't, every re-export churns the git history, `git status` is never clean, and the diffs stop
carrying information — which removes the whole reason for having an export.

## Options considered

**A. PyYAML (or ruamel.yaml).** Mature, handles the full specification, zero code to own. But
output formatting is an implementation detail, not a contract. Quoting style, line folding,
scalar-style selection and key ordering can all shift between releases. A dependency bump would
rewrite every file in the catalog for no semantic reason, and the resulting diff would be
indistinguishable from real changes. The formatting can be constrained with a custom Dumper, but
that is a private-API relationship maintained against a moving target.

**B. JSON via the standard library.** Trivially deterministic with `sort_keys=True` and a fixed
indent, no dependency, and a guaranteed-correct parser. But the catalog is meant to be *read and
occasionally hand-edited* in a pull request. JSON's mandatory quoting and comma placement make a
one-field change a noisier diff, and it cannot carry comments.

**C. A restricted emitter and reader owned by klm.** Full control over the bytes, no dependency,
and the subset can be exactly what the catalog needs. Costs roughly 250 lines that must be
correct, and re-implements something that already exists.

## Decision

**Option C.** `klm.serial.yaml` emits and reads a deliberately small subset:

- block style only, one field per line — so a one-field change is a one-line diff
- mappings, sequences, strings, integers, floats, booleans, null
- no anchors, aliases, tags, multi-document streams, flow style, or folded scalars

Anything outside the subset raises rather than being guessed at. Floats are written with `repr`,
which gives the shortest representation that reads back as the identical value. Strings are
quoted whenever a plain scalar would be re-read as a different type.

Field order is the caller's, not sorted: a part reads far better with `mpn` near the top than
alphabetically between `lifecycle` and `notes`. Determinism comes from
`part_to_mapping` building the mapping identically every time, and that ordering *is* the file
format.

## Consequences

**Good**
- The export format is a contract klm controls. No upstream release can rewrite the catalog.
- No runtime dependency for the core data path.
- Errors are specific and point at a line, because the reader was written for these files.
- The subset is small enough to test exhaustively, including a round-trip property test.

**Bad**
- ~250 lines to own and keep correct, including the awkward parts (quoting, escapes, indentation).
  Mitigated by the subset being closed: input outside it raises rather than silently
  misinterpreting.
- A hand-edited `part.yaml` using a legitimate YAML construct klm does not support will be
  rejected. Acceptable, and arguably desirable — the file is primarily machine-written, and a
  clear error beats a silent misreading.
- Not a general YAML implementation, and must never be presented as one.

**Neutral**
- If the subset ever needs to grow substantially, that is the signal to reconsider and adopt a
  library with a pinned version and a formatting test. The `klm.serial.yaml` boundary keeps that
  a contained change.
