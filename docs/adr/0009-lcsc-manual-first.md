# ADR-0009 — LCSC is manual-first, and klm ships no unofficial-endpoint client

**Status:** accepted · **Phase:** 2 · **Related:** [07](../07-supplier-integration.md),
[14 Q2](../14-open-questions.md#q2)

## Context

[Q2](../14-open-questions.md#q2) was the highest-risk item in the register: LCSC published no
official API, and the practical options were community `jlcsearch`-style services and the EasyEDA
component endpoints that `easyeda2kicad`-class tools use. Both unofficial, both liable to change,
both of uncertain standing under LCSC's terms.

Verified in August 2026, the situation has changed but not in klm's favour:

- **LCSC now publishes an official API.** It offers roughly eight services covering search,
  pricing, stock and ordering.
- **Access is granted per company, not per person.** The application asks for a company website, a
  business licence or equivalent, contact details, an estimated order quantity, and a cooperation
  mode. Applicants who don't qualify are pointed at authorised third-party procurement providers.
- **The terms are restrictive.** They prohibit sharing the API documentation with third parties,
  aggregating the data into other public or commercial APIs, providing retrieved material to third
  parties, and sharing credentials outside the holder's company.

klm's user is a hobbyist ordering a few hundred parts a year. They will not be granted access, and
klm's authors — who also have not been granted it — are not permitted to see the documentation
they would need in order to write a client on that user's behalf.

Meanwhile LCSC remains unavoidable: it is the supplier whose part numbers JLCPCB's assembly
service consumes. "Drop LCSC" is not an option.

## Options considered

**A. Build against the unofficial endpoints anyway, isolated behind the adapter interface.** The
original mitigation in [07 §3](../07-supplier-integration.md). Rejected on re-reading it: isolation
makes the code *replaceable*, which is a maintenance property. It does nothing about permission.
Bulk-pulling pricing from an endpoint whose operator's terms forbid exactly that is not something
an interface boundary fixes, and shipping it as the default would opt every klm user into it
without their knowing.

**B. Ship an official-API client with the endpoint shapes guessed or reverse-engineered.** Two
problems, either fatal. The shapes would be fabricated — this codebase does not guess, and code
that looks verified but is not is worse than no code. And publishing a reconstruction of
documentation klm's authors are not licensed to hold is the thing the terms most directly
prohibit.

**C. Make manual entry the primary path, and leave the API as a seam.** Manual entry was already
designed as a "first-class fallback". Promote it from fallback to primary, and make the API mode a
place where a user who *has* been granted access — and therefore holds documentation klm's authors
cannot — plugs in their own client.

## Decision

**Option C.**

- `[suppliers.lcsc]` defaults to `mode = "manual"`. In manual mode every lookup **declines
  quietly** — returns nothing — rather than raising. LCSC having no automated answer is the
  expected state, not a failure, and a refresh across 200 parts must not print 200 errors about it.
- `klm offers <part> --supplier lcsc --add C25900 --price … --stock …` records what a human read
  off the product page. The link is stored at `high` confidence, because a human matching a part is
  a better signal than any string comparison klm can make.
- Everything that needs no API still works automatically: part-number validation, product and
  datasheet URLs, packaging-suffix handling, and JLCPCB's `LCSC` field.
- `mode = "api"` accepts a user-supplied client object satisfying the same `SupplierAdapter`
  protocol. klm defines where it plugs in; the user supplies what goes there.
- **klm ships no code against unofficial endpoints**, in any mode, enabled or not.

## Consequences

**Good**
- klm is fully functional for its actual audience — ordering, JLCPCB assembly, BOM export all work
  with manually entered LCSC data. The only loss is live stock.
- Nothing in klm depends on an endpoint that can disappear, so the highest-risk item in the
  register stops being a risk at all rather than being a deferred one.
- No user is silently opted into traffic against terms they never read.
- The seam is real rather than notional: TME exercises the same protocol with a full
  implementation, so the shape is known to be sufficient.

**Bad**
- LCSC prices and stock go stale, and klm cannot tell you they have. Mitigated by lint rule P003,
  which reports the age of what is stored — a manually entered offer ages exactly like a fetched
  one, and says so.
- Adding an LCSC offer is a per-part chore. Mitigated by it being one command, and by the offer
  surviving forever after; it is a chore per *part*, not per order.

**Neutral**
- If LCSC later opens API access to individuals, this is a config default change and a client
  module. Nothing above the adapter moves.
- The same shape covers every future supplier klm has no adapter for: manual entry is the general
  escape hatch, not an LCSC special case.
