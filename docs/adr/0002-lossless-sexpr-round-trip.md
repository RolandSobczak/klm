# ADR-0002 — Generic S-expression parsing with a lossless round-trip guarantee

**Status:** accepted · **Phase:** 0 · **Related:** [03 §3](../03-architecture.md#3-the-lossless-round-trip-rule),
[Q7](../14-open-questions.md#q7)

## Context

klm edits `.kicad_sch`, `.kicad_pcb`, `.kicad_sym` and `.kicad_mod` — files that represent hours
of a user's work and cannot be regenerated if damaged. It edits them for library sync
([06](../06-library-sync.md)), model-path rewriting, and field normalization.

KiCad's format is S-expressions and it evolves between versions. New node types appear.

## Options considered

**A. A typed KiCad-format library.** Parse into typed objects (`Symbol`, `Pad`, `Footprint`),
mutate, serialize. Ergonomic and readable. But typed parsers must decide what to do with nodes
they don't know about, and the usual answer is to drop them — which silently deletes user data the
moment KiCad adds a feature. It also couples klm to whichever format versions that library
supports.

**B. Regex/text manipulation.** Tempting for a task as narrow as "rewrite the model path". Breaks
on nested parentheses, quoted strings containing parens, and multi-line nodes. Rejected outright —
this is the classic wrong tool.

**C. Generic S-expression tree.** Parse to a minimal tree of atoms and lists that preserves
everything, mutate only nodes klm understands, re-serialize the whole tree. Less ergonomic; a
node is a list, not an object.

## Decision

**Option C.** A generic S-expression reader/writer in `klm.kicad.sexpr`, with typed *views*
layered on top for the node kinds klm actually manipulates.

```python
tree = sexpr.load(path)
for node in tree.find_all("model"):
    node[0] = rewrite_model_path(node[0])
sexpr.dump(tree, path)      # every untouched node byte-identical
```

The guarantee, tested as a property against a corpus of real files from multiple KiCad versions:
**`dump(load(f)) == f`, byte for byte.**

A separate *canonical* writer (stable ordering, fixed float precision, sorted fields) is used only
for files klm generates and owns entirely — `KLM.kicad_sym`, vendored libraries — where byte
stability across regenerations matters more than preserving input formatting.

## Consequences

**Good**
- klm survives KiCad format changes. Unknown nodes pass through untouched.
- Diffs are minimal: rewriting one model path changes one line, not the file's whole formatting.
- klm never reformats a hand-edited file as a side effect of an unrelated operation.
- No external dependency on a format library's release cadence.

**Bad**
- More verbose code. `node[3][1]` where a typed API would give `pad.at.x`. Mitigated by typed
  views over the generic tree for the handful of node kinds klm touches.
- The reader/writer must be genuinely correct — quoting, escaping, floats, UTF-8 — and is
  therefore the most heavily tested module in the project. Fuzzing against real files is warranted.
- klm can't easily do semantic validation it never parsed. Acceptable: validation happens against
  the catalog model, not the file.

**Neutral**
- If a maintained library appears that offers the same lossless guarantee, adopting it is a
  contained change behind `klm.kicad.sexpr`. The requirement is the guarantee, not the authorship.
