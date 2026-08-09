# ADR-0001 — SQLite as the operational store, YAML as the git mirror

**Status:** accepted · **Phase:** 0 · **Related:** [04](../04-catalog-and-storage.md)

## Context

The catalog must be both queryable ("every 0402 100nF X7R ≥16 V with stock at TME") and
versionable (a git history of the library, diffable and restorable). These pull in opposite
directions.

## Options considered

**A. Flat files only (YAML/JSON per part).** Excellent git behaviour, trivially backed up, no
schema migrations. But every query becomes a full directory scan; at ~900 parts × ~20 parameters
that's noticeably slow in an interactive UI, and joins (parts × offers × stock) become hand-rolled.

**B. SQLite only.** Fast, transactional, relational. But the catalog becomes a binary blob in git
with no meaningful diff, no way to review a change, and merge conflicts that can only be resolved
by picking one side wholesale.

**C. SQLite as truth + deterministic YAML export.** Both properties, at the cost of maintaining
an export/import round trip.

**D. Git-backed document database.** Solves it in principle; every option is heavier than the
problem and adds a dependency that outlives its usefulness.

## Decision

**Option C.** SQLite (`catalog.db`) is the operational store. A deterministic YAML export
(`catalog/<klm_id>/part.yaml`) is the git-versioned mirror. `klm export` and `klm import`
round-trip between them.

The database is *reconstructible* — it can be deleted and rebuilt from the export at any time.
That property, rather than a formal declaration of which one is "the" source of truth, is what
makes the arrangement safe.

## Consequences

**Good**
- Fast interactive queries; a real relational model for parts × offers × stock × orders.
- Reviewable git history; a part's evolution is a readable diff.
- Backup is `git push`. Restore is `klm import`.
- Merge conflicts land in per-part files and are usually resolvable.

**Bad**
- Two representations to keep consistent. Mitigated by a property test asserting
  `export(import(export(db)))` is byte-identical to `export(db)`.
- Export must be *rigorously* deterministic — fixed key order, sorted lists, fixed float
  precision, no incidental timestamps — or the git history fills with noise.
- Users must remember to export before committing. Mitigated by a pre-commit hook that exports
  automatically.

**Neutral**
- Schema migrations affect the database; the export format is versioned separately so an old
  export imports into a newer klm.
