# 11 — AI Research Agent

Solves [P7](01-vision-and-problems.md#p7--finding-the-right-part-in-the-first-place). The agent
does the search-and-read work; a human makes every decision that changes the catalog.

## 1. What the agent is for

**In scope**: given a requirement, find candidate parts that satisfy it, are buyable locally,
and have their claimed parameters backed by a datasheet — then present them ranked, with
citations, for a human to choose.

**Out of scope**: choosing the part, deciding the circuit topology, writing anything to the
catalog, placing orders, or modifying a schematic. The agent's output is always a *proposal*.

That boundary is the design's load-bearing constraint. An agent that can write to the catalog is
an agent that can quietly fill it with hallucinated parameters, and the catalog's value is that
you can trust it.

## 2. The requirement specification

Freeform text is a bad interface for this — it makes the agent guess at constraints. klm uses a
structured requirement, which the agent can also *help draft* from a freeform description:

```toml
kind = "buck_converter"
notes = "3.3 V rail on a battery-powered sensor board."

[constraints]
vin        = { min = 4.5, max = 18, unit = "V" }
vout       = { value = 3.3, unit = "V", tolerance = 0.02 }
iout       = ">=1A"
topology   = "synchronous"
package    = { allow = ["SOT-23-6", "SOIC-8", "QFN-*"] }
temp_range = { min = -40, max = 85, unit = "°C" }

[[preferences]]
kind = "minimize"
what = "unit_price"

[[preferences]]
kind = "prefer"
what = "existing_footprint_in_catalog"

[[preferences]]
kind = "prefer"
what  = "supplier"
value = "tme"

[sourcing]
suppliers = ["tme", "lcsc"]
min_stock = 50
lifecycle = ["active"]
```

TOML, like every other file klm reads, so the schema needs no dependency and the parser is the
one the standard library already ships. `klm research check <file>` reads it, states what klm
understood, and needs neither a catalog nor an API key — the hard/soft split is far cheaper to
check there than in a list of candidates that all look plausible.

`constraints` are hard filters. `preferences` are the ranking function. Separating them means a
candidate is never silently dropped for being merely non-preferred, and the agent can say
"nothing meets your hard constraints; the closest miss is X" — which is far more useful than an
empty result. `prefer` inside a constraint is refused rather than accommodated, with a message
pointing at `[[preferences]]`: it is exactly the confusion the split exists to prevent.

Four rules make the schema honest rather than merely structured
(`klm.research.requirement`):

- **A constraint that could not be checked is `unknown`, never `pass`.** A candidate that does
  not state its input range has not met the requirement; it has not been checked. Same rule as
  the asset QA gate ([08 §5](08-asset-pipeline.md#5-the-qa-gate)), same reason.
- **A value klm cannot read is `unknown`, not `fail`** — a supplier writing "see datasheet" in a
  column must not reject the right part.
- **Ranges are checked by coverage, not overlap.** A part rated 1.8–6.5 V does not run a
  4.5–18 V rail, and the two overlap. The candidate's range has to *contain* what was asked for.
- **A bare string is text unless it carries a comparison or a range.** `package = "0402"` is a
  package; reading it as the number 402 would filter an axis nobody asked about. `iout = ">=1A"`
  is numeric because it says so.

An unknown section is an error, not a warning. A misspelled `contraints` that parsed would
produce a requirement with no hard filters at all — and an unfiltered search returns plenty of
results, so the failure would look like success right up until a part was ordered. Every problem
in a file is reported at once, because a requirement is written or drafted in one go and fixing
five mistakes one message at a time is five round trips.

Ranking is **relative to the candidate set**: `minimize unit_price` scores a candidate by where
it sits between the cheapest and the dearest *here*, because there is no absolute scale on which
0.42 USD is a good price. A preference a candidate says nothing about is left out of its average
rather than scored zero — that would punish a part for a field klm never fetched — and is named
in the explanation, so a reader can see the ranking was made on less than the whole requirement.

Numeric constraints are the same `Constraint` type TME's parametric search consumes, so a
requirement reaches `supplier_search` without a translation step in between. Text constraints
(`topology`, `package`) are deliberately *not* pushed into the supplier query: a supplier's
values for them are free text, and a wrong exclusion costs more than a larger result set that is
checked afterwards.

## 3. Tools

Defined with strict JSON schemas and executed by klm, never by the model.

| Tool | Purpose | Notes |
|---|---|---|
| ✓ `catalog_search` | Search the *existing* catalog first | Deliberately first in the list; reuse beats acquisition |
| ✓ `supplier_search` | Parametric/keyword search at **TME** | Returns hits with stock. Not LCSC — see below |
| ✓ `supplier_get_offer` | Details for one supplier part number | The only source of a price or stock claim |
| ✓ `footprint_lookup` | Does the catalog already have this package? | Drives the reuse preference |
| ✓ `datasheet_fetch` | Download and cache a datasheet PDF | Returns a handle — the file's hash — not raw text |
| ✓ `datasheet_extract` | Pull parameters from a cached datasheet | Returns page + quote per parameter, and drops anything uncited |
| ✓ `propose_part` | Emit a structured candidate | The only "write" — and it writes to a review queue, not the catalog |

`klm.research.tools`, all built. `klm research tools` lists what a session would have *on this
machine*, which is worth being able to ask before spending anything.

Notably absent: any tool that writes to the catalog, edits a file, or spends money. The agent
physically cannot do those things — that's an architectural guarantee, not a prompt instruction.
It is enforced twice over: no such function exists, and the connection the tools hold is opened
`mode=ro`, so a write fails inside SQLite rather than in a code review.

Four more properties of the tool layer, each protecting against a failure that would otherwise be
invisible:

- **Arguments are validated against the same schema the model was given, before a service sees
  them.** `strict` is a promise made by the other end of a network connection, and a guardrail
  that holds only while a remote service behaves is not one.
- **A tool never raises at the model.** A supplier that is down, a category that does not exist,
  a package klm has never heard of — all come back as results the agent can act on. An exception
  would end the session instead of redirecting it.
- **An ambiguous category is refused with its candidates**, never resolved by taking the first
  match. Picking one would search a category the requirement never mentioned and return a
  confident list of parts from it.
- **A constraint that could not be applied is named in the response, with a warning.** Results
  that were not filtered by a constraint look exactly like results that were.

A tool is also *absent* rather than failing: no TME credentials means no `supplier_search`, and
LCSC — manual by design — contributes no tools at all rather than one that answers "I don't know"
to everything.

### `supplier_search` is TME-only, and never sees an ID

Two findings from [Q10](14-open-questions.md#q10--parametric-search-quality-at-tme-and-lcsc--resolved-2026-08-09)
shape this tool, and both are load-bearing.

**LCSC is not searchable by klm at all.** Not a quality judgement —
[ADR-0009](adr/0009-lcsc-manual-first.md): the official API is granted per company and its terms
forbid klm's authors from holding the documentation. So the agent searches TME, and LCSC offers
reach the catalog the way they always have, by a human typing a part number. A candidate the agent
proposes may therefore be TME-only; the review screen says so rather than implying LCSC was checked
and came back empty.

**TME's parametric search filters by numeric IDs**: *parameter 2 has value 156 or 179*, not
"Vin_max ≥ 18 V". Values are discrete identifiers, so a numeric constraint is not a comparison the
API can express — it is the set of value IDs whose parsed number satisfies it.

klm does that translation, and the agent never touches an ID:

```
agent:  supplier_search(category="IC/Power/Regulator/Switching",
                        constraints={"Vin max": ">=18V", "Iout": ">=1A"})
klm:    resolve the category to a TME category_id
        ask TME for the parameters available in it   (scope[]=parameters)
        parse every candidate value with klm.units   ("18 V" → 18.0)
        keep the value IDs that satisfy the constraint
        search with parameters[n][id] / [values][]
```

The agent states constraints in the units a human would; klm resolves them or reports that it
cannot. A model asked to supply `parameters[0][id]=2` would eventually supply a plausible wrong
one, and a wrong parameter ID returns *confidently wrong parts* rather than an error. It is the
same failure as an invented MPN, and the same answer: make it structurally impossible instead of
asking the prompt to prevent it.

A constraint klm cannot map — no such parameter in the category, or values it cannot parse — is
**reported to the agent as unmapped**, not silently dropped. Dropping it would quietly widen the
search and present the results as if they had been filtered.

### Reuse-first

`catalog_search` being the first tool, and the system prompt making reuse explicit, matters more
than it looks. Every avoided new part is one less thing to order, stock, label and maintain. The
agent is instructed to justify introducing a new part when a catalog part is within tolerance of
the requirement.

### Prerequisite: the TME adapter's parametric path

`search_parametric` currently sends `{"CategoryId": …, "Parameters": {name: value}}` — a shape that
matches neither v2's `parameters[n][id]` form nor anything ever verified against v1. It is the one
request in the supplier layer that was written from assumption, and it is precisely what
`supplier_search` would stand on.

**Rebuilt against the v2 endpoints on 2026-08-09, before the agent was written**, together with
the `POST /auth/token` bearer flow v2 requires (Q1 addendum — the token expires in 300 seconds, so
a research session outlives it and the adapter renews 30 s early and retries once on a 401).
Building the agent on the unverified shape first would have meant debugging a model and a request
shape at the same time, with no way to tell which was lying.

One caveat carries forward into this phase: **no request has ever reached the live API**, because
klm's authors hold no TME credentials. Every shape comes from TME's published OpenAPI document and
the tests pin that document rather than the server. A field-name mismatch on first real use is
expected, not a regression.

## 4. Implementation

Python core and the Anthropic SDK, behind one seam: `klm.llm.client.ModelClient` — a system
prompt, a conversation, a tool list, one reply back. `klm.research.agent` drives the loop against
that protocol.

```python
transcript = research(
    requirement,                       # klm.research.requirement.Requirement
    toolset,                           # klm.research.tools.Toolset — what this machine has
    AnthropicClient(effort="high"),
    limits=Limits(max_iterations=20, max_tokens=400_000, max_spend=2.00),
    conn=writable,                     # the event log — klm's connection, not the agent's
    on_text=print,                     # stream the answer as it arrives
)
```

**klm owns the loop rather than using the SDK's tool runner**, which is the documented default.
Three reasons, in order of weight:

- **The guardrails are the point of this phase.** An iteration cap, a token budget, a spend
  ceiling and an audit trail are each a line of code with a test beside it here, rather than a
  hook wired into someone else's loop.
- **klm's tools are data, not decorated functions.** The tool set is built at runtime from what
  the machine actually has — no TME credentials means no `supplier_search` — and the decorator-
  based runner wants module-level functions with type annotations.
- **It is testable without an API key or the SDK.** Every guardrail is exercised against a fake
  model that costs nothing. A guardrail that can only be tested by spending money is a guardrail
  nobody tests.

Model and parameter choices:

- **`claude-opus-5`** for research. This is multi-step work — search, read a PDF, cross-check,
  compare — where the difference in quality is worth the cost, and a part chosen wrongly costs
  three weeks of shipping.
- **`claude-haiku-4-5`** for bulk mechanical extraction (parsing a parametric table out of 40
  supplier rows), where the task is narrow and volume matters.
- **Adaptive thinking** at `effort: "high"`. Thinking is on by default on this model, and a fixed
  thinking budget is not a thing it accepts — depth is `effort`, and routine work drops to
  `medium`.
- **Streaming**, so the user watches searches happen instead of staring at a spinner — and because
  a research turn is long enough for a non-streaming request to hit an HTTP timeout.
- **Structured outputs** (`output_config.format`) for the final proposal, so it validates against
  klm's schema rather than being parsed out of prose. That arrives with the proposal queue.

Costs are metered per research session and shown in the UI. An API key is required for agent
features only; klm's entire non-agent surface works without one, and the SDK is an extra
(`pip install 'klm[agent]'`) rather than a dependency.

Two rules the loop enforces, both because the failure they prevent looks like success:

- **A session that stopped early says so.** Hitting the iteration cap or the spend ceiling ends
  the run with a *partial* answer, reported as partial and exiting non-zero. An answer that was
  cut off and reads as finished is the worst possible output here.
- **A tool never fails the session.** A supplier outage or an unknown category comes back to the
  model as a result it can act on, so the loop has nothing to catch and the session redirects
  instead of ending.

`stop_reason` is checked before the reply is read. A refusal carries no usable content, and the
difference between a clear message and an `IndexError` is that check.

## 5. Guardrails

| Guardrail | Mechanism |
|---|---|
| **Cannot write to the catalog** | No tool exists. `propose_part` writes to a review queue. |
| **Every parameter cited** | `datasheet_extract` returns page + quote, **and the quote comes from the API's citation machinery rather than from the model**. A parameter stated in an uncited block is dropped and reported, never returned as a value. The same ledger checks it again at `propose_part`: a quote klm never read does not reach the proposal |
| **Conflicts surfaced, not resolved** | When a datasheet and a supplier field disagree, both are recorded and the candidate is flagged for review |
| **Stock and lifecycle verified** | Claims about availability come from a live offer, never from the model's recollection |
| **No invented part numbers** | **Mechanical, not prompted**: `klm.research.tools.Ledger` records every MPN a tool *returned*, and `propose_part` refuses one that is not in it. A plausible part number that does not exist survives review, gets ordered, and comes back three weeks later as nothing |
| **Every concern stated** | A proposal with an empty `concerns` list is refused. A candidate with nothing wrong with it is one that has not been looked at hard enough |
| **Bounded** | Iteration cap, token budget per session, and a spend ceiling — all three checked *before* each request, because a limit that trips only once exceeded is a limit that is always exceeded |
| **Logged** | Every session's tool calls and results go to `event_log`, so a bad part can be traced to the reasoning that produced it |

The last two are `klm.research.agent.Limits` and `klm.services.events`. Note which connection
writes that log: the agent's tools hold a **read-only** one, and klm records the session on its
own. klm writes about the agent; the agent cannot write anything. That split is what lets the
audit trail and the no-write guarantee coexist.

The invented-part-number rule deserves emphasis: a plausible-looking MPN that doesn't exist is the
most likely and most costly hallucination in this domain, and it's cheap to eliminate by requiring
every candidate to originate from a supplier API response rather than from generation.

## 6. Output

```yaml
candidates:
  - rank: 1
    mpn: TPS62840DLCR
    manufacturer: Texas Instruments
    confidence: high
    why: "Meets all hard constraints. Lowest Iq in the set (60 nA), which matters
          for the battery preference. SOT-563 is not in your preferred package list
          but is smaller than SOT-23-6."
    parameters:
      - {name: vin_max, value: 6.5, unit: V, source: {page: 1, quote: "…"}}
      - {name: iout_max, value: 750, unit: mA, source: {page: 1, quote: "…"}}
    constraint_check:
      vin:  {required: "4.5–18 V", actual: "1.8–6.5 V", status: FAIL}
      iout: {required: "≥1 A",     actual: "750 mA",    status: FAIL}
    offers:
      - {supplier: lcsc, pn: C2843057, stock: 3120, price_1: 0.42, currency: USD}
    assets_available: {symbol: generate, footprint: import_easyeda, model3d: convert}
    concerns:
      - "Fails your Vin and Iout constraints — included because it dominates on Iq
         and you may want to revisit those constraints."
      - "SOT-563 footprint not in catalog; would be a new footprint asset."
```

Two deliberate features of this format:

- **Constraint checks are explicit and can say FAIL.** A near-miss that's interesting for a
  stated reason is more useful than silence, provided the miss is unmissable.
- **`concerns` is required.** A candidate with an empty concerns list is suspicious, and
  requiring the field forces the model to articulate the trade-off rather than presenting
  everything as ideal.

## 7. Approval

```
agent proposes  ──▶  review queue  ──▶  human reviews diff  ──┬─▶ approve ─▶ draft part
                                                              ├─▶ edit then approve
                                                              └─▶ reject (with a reason,
                                                                  logged for prompt tuning)
```

Approving creates a **draft** part — it still has to pass the asset QA gate
([08 §5](08-asset-pipeline.md#5-the-qa-gate)) and field lint before it becomes `approved` and
usable in a design. There is no path from agent output to a usable part that skips a human and
the mechanical checks.

Rejections are logged with reasons. That log is the raw material for improving the requirement
schema and the system prompt — the failures tell you what the interface is missing.

## 8. Other agent-assisted tasks

Beyond part research, the same infrastructure supports:

- **Datasheet Q&A** (`klm ask`): "what's the maximum junction temperature?" against a cached
  PDF. Looser than `datasheet_extract` on purpose — a question has a prose answer, and demanding
  a citation per sentence would make the useful answers unavailable. What klm does instead is
  report whether the answer quoted the document *at all*, so an ungrounded one is visibly
  ungrounded rather than indistinguishable.
- **Substitute finding** (`klm substitutes`): a stocked part goes to zero; find pin-compatible
  alternatives. Pin compatibility is checked mechanically against the catalog's own footprint and
  pin data — same land pattern (pad geometry, quantised), same pin numbers, same electrical
  types, **same pin names** — with the model handling only the search and the
  electrical-equivalence argument. Names matter: two parts can share a footprint and a pin-type
  map while pin 3 is `EN` on one and `GND` on the other, both inputs, and one of them destroys
  the board. Three answers — `compatible`, `differs` (with exactly what differs, so a human can
  overrule it), and `unchecked` when an asset is missing. A `compatible` that meant "I could not
  look" is how the wrong part reaches a board.
- **Categorization and description cleanup**: proposing consistent descriptions across a catalog
  imported from a messy library. Bulk, mechanical, cheap model.
- **Design review**: given a schematic's BOM, flag parts running near a rating, missing decoupling,
  or a lifecycle problem. Advisory only, clearly labelled as such.

Each is a bounded task with its own tool subset. None of them gains write access.
