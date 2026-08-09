# ADR-0003 — Opaque immutable part identity embedded in every symbol

**Status:** accepted · **Phase:** 0 · **Related:** [02 §5](../02-domain-model.md#5-identity-in-detail),
[06 §4](../06-library-sync.md#4-why-klm_id-is-load-bearing)

## Context

Every operation that spans the catalog and a project — vendoring, sync, drift detection, BOM
resolution, promotion of a collaborator's part — needs to answer one question exactly:

> Which catalog part is this schematic symbol?

It must stay answerable after the symbol is renamed, the MPN is corrected, a collaborator edits
the schematic, or the part is merged with a duplicate.

## Options considered

| Key | Survives rename | Survives collaborator edit | Survives MPN correction | Human-meaningful |
|---|---|---|---|---|
| Symbol name | ✗ | ✗ | ✓ | ✓ |
| `(manufacturer, MPN)` | ✓ | usually | ✗ | ✓ |
| Content hash | ✗ | ✗ | ✓ | ✗ |
| Opaque ID field | ✓ | ✓ | ✓ | ✗ |

Name-based matching degrades into fuzzy matching, and fuzzy matching in a tool that rewrites
schematics is how you corrupt someone's board.

## Decision

Every part gets a `klm_id` — a ULID-like opaque identifier, generated once at creation, **never**
changed and never reused. It is written into every generated symbol as a hidden `KLM_ID` field.

Three identifiers coexist, each for its own audience:

| Identifier | Audience | Stability |
|---|---|---|
| `klm_id` | Machines | Immutable forever |
| `(manufacturer, mpn)` | Humans, suppliers | Stable in practice, occasionally corrected |
| Symbol name | KiCad, schematic readers | May be renamed freely |

Supporting rules:

- **Merging duplicates** keeps the older `klm_id` and records the other in `part_alias`, so
  schematics referencing the retired ID still resolve.
- **Deletion is soft** (`status: deprecated`). Hard deletion would orphan schematics.
- **Name-based fallback** exists only for parts that predate klm, and always reports its
  confidence rather than silently guessing.

## Consequences

**Good**
- Sync operations are exact rather than heuristic. This single decision is what makes
  [06](../06-library-sync.md) tractable at all.
- Renaming a symbol, correcting an MPN, or re-categorizing a part are all free — no references
  break.
- A collaborator's edited schematic still resolves to catalog parts.
- Duplicate detection becomes exact where IDs match and merely suggestive where they don't.

**Bad**
- An opaque 26-character field in every symbol. Invisible in the schematic, but present in the
  file and mildly ugly in a diff.
- A collaborator who copies a symbol to make a variant produces two parts sharing one `KLM_ID`.
  Detected by lint rule R003 (`same KLM_ID, divergent content`), which prompts to fork a new
  identity — a real failure mode with a real mitigation, not a theoretical one.
- Hand-created symbols lack the field and fall back to name matching.

**Neutral**
- ULID over UUID4 for lexicographic sortability by creation time and a shorter,
  case-insensitive, Crockford-Base32 representation — which is also what makes the ~8-character
  shortened form usable on a 14 mm drawer label ([10 §7](../10-ordering-and-inventory.md#7-labels-for-3d-printed-drawers)).
