# ADR-0007 — Self-containment is defined by clean-room verification, not by local success

**Status:** accepted · **Phase:** 6 · **Related:** [15](../15-project-scaffolding-and-ci.md),
[06](../06-library-sync.md)

## Context

`klm vendor` makes a project self-contained: parts copied in, `lib_id`s rewritten, project-level
library tables written. The question is how to *know* it worked.

The obvious answer — open the project and check — is close to worthless on the developer's own
machine. That machine has the `KLM` global library registered, `KLM_LIBS` and `KLM_3DMODELS` set,
and every 3D model on disk. A project where one forgotten hierarchical sheet still references
`KLM:STM32F103C8T6` opens perfectly there and shows a broken-symbol placeholder for everyone else.

The failure is silent, delayed, and lands on the collaborator rather than the author — the worst
combination.

## Options considered

**A. Trust the vendor operation.** It rewrites everything it finds, so it should be complete.
Rejected: "everything it finds" is exactly the assumption at risk. Hierarchical sheets, PCB-only
footprints, hand-edited symbols and a collaborator's additions are all ways for a reference to
survive the sweep.

**B. Lint on the developer's machine with global libraries temporarily disabled.** Better, but
simulating absence is fragile — environment variables, KiCad's own config, cached footprint
indexes and the user's home directory all leak in, and the simulation drifts from reality.

**C. Verify on a clean machine.** A CI runner has no global libraries, no environment variables,
no catalog and no home directory of yours. If it resolves there, it resolves anywhere.

## Decision

**Option C.** Self-containment is defined as: *`klm verify --clean-room` passes on a fresh
checkout in a container with no klm catalog and no KiCad user configuration.*

This forces a specific implementation constraint that shapes the code:

> `klm verify --clean-room` must work with **no catalog and no configuration**, using only files
> inside the repository.

It is therefore a separate code path from `klm lint`, which assumes a catalog. Verification
resolves references purely against the project's own library tables and files, exactly as KiCad
would on a stranger's machine.

## Consequences

**Good**
- The self-containment guarantee becomes testable, and is tested on every push.
- The failure surfaces in a PR, on the author's screen, minutes after the mistake — instead of in
  a collaborator's confused message a week later.
- A README that says "no external library setup is required" is backed by a check rather than a
  hope.
- The constraint keeps klm honest: it forces vendoring to be genuinely complete rather than
  complete-on-the-happy-path.

**Bad**
- A second resolution code path to maintain alongside catalog-backed linting. Mitigated by keeping
  the clean-room checker deliberately small — it answers "does this resolve?", not "is this good?".
- CI becomes part of the workflow's correctness story, so CI being broken means the guarantee is
  unverified. Acceptable: it's a warning sign either way.
- Requires a pinned KiCad container, which is maintenance.

**Neutral**
- The same command is useful locally (`klm verify --clean-room` before pushing) — it just isn't
  *proof* there, because a local run can't fully escape the local environment. The docs are
  explicit that CI is the authority.
