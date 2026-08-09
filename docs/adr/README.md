# Architecture Decision Records

Each record captures one decision, the options rejected, and the consequences accepted. They exist
so that a future reader — including a future you — can tell the difference between a deliberate
trade-off and an accident.

| ADR | Decision | Phase |
|---|---|---|
| [0001](0001-sqlite-as-source-of-truth.md) | SQLite as the operational store, deterministic YAML as the git mirror | 0 |
| [0002](0002-lossless-sexpr-round-trip.md) | Generic S-expression parsing with a lossless round-trip guarantee | 0 |
| [0003](0003-part-identity.md) | Opaque immutable `KLM_ID` embedded in every generated symbol | 0 |
| [0004](0004-generated-flat-libraries.md) | Generate flat KiCad libraries; database libraries are optional | 0 |
| [0005](0005-desktop-shell.md) | Python core + local HTTP API + Tauri shell, with no logic in the shell | 8 |
| [0006](0006-agent-cannot-write.md) | The AI agent proposes; it never writes to the catalog | 9 |
| [0007](0007-clean-room-verification.md) | Self-containment is defined by clean-room CI verification, not local success | 6 |
| [0008](0008-hand-written-yaml-emitter.md) | A hand-written YAML emitter rather than a dependency, for byte-stable output | 0 |
| [0009](0009-lcsc-manual-first.md) | LCSC is manual-first; klm ships no unofficial-endpoint client | 2 |

## Format

```markdown
# ADR-NNNN — Title

**Status:** proposed | accepted | superseded by ADR-MMMM · **Phase:** N · **Related:** links

## Context
What forces are in play. Written so it still makes sense in two years.

## Options considered
Each with its genuine advantages, not straw men.

## Decision
What was chosen, stated plainly.

## Consequences
Good / Bad / Neutral. The Bad section is the important one — if it's empty,
the decision hasn't been thought through.
```

Records are immutable once accepted. A changed decision gets a new record that supersedes the old
one; the old record stays, marked superseded, because the reasoning that was true at the time is
still worth knowing.
