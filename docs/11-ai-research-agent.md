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

```yaml
kind: buck_converter
constraints:
  vin:        {min: 4.5, max: 18, unit: V}
  vout:       {value: 3.3, unit: V, tolerance: 0.02}
  iout:       {min: 1.0, unit: A}
  topology:   synchronous
  package:    {allow: [SOT-23-6, SOIC-8, "QFN-*"], prefer: SOT-23-6}
  temp_range: {min: -40, max: 85, unit: C}
preferences:
  - {kind: minimize, what: unit_price}
  - {kind: prefer,   what: existing_footprint_in_catalog}
  - {kind: prefer,   what: supplier, value: tme}
sourcing:
  suppliers:  [tme, lcsc]
  min_stock:  50
  lifecycle:  [active]
```

`constraints` are hard filters. `preferences` are the ranking function. Separating them means a
candidate is never silently dropped for being merely non-preferred, and the agent can say
"nothing meets your hard constraints; the closest miss is X" — which is far more useful than an
empty result.

## 3. Tools

Defined with strict JSON schemas and executed by klm, never by the model.

| Tool | Purpose | Notes |
|---|---|---|
| `catalog_search` | Search the *existing* catalog first | Deliberately first in the list; reuse beats acquisition |
| `supplier_search` | Parametric/keyword search at **TME** | Returns offers with stock and price. Not LCSC — see below |
| `supplier_get_offer` | Details for one supplier part number | |
| `datasheet_fetch` | Download and cache a datasheet PDF | Returns a document handle, not raw text |
| `datasheet_extract` | Pull parameters from a cached datasheet | Must return page number + quoted snippet per parameter |
| `footprint_lookup` | Does the catalog already have this package? | Drives the reuse preference |
| `propose_part` | Emit a structured candidate | The only "write" — and it writes to a review queue, not the catalog |

Notably absent: any tool that writes to the catalog, edits a file, or spends money. The agent
physically cannot do those things — that's an architectural guarantee, not a prompt instruction.

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

**It is rebuilt against the v2 endpoints before the agent uses it**, together with the
`POST /auth/token` bearer flow v2 requires (Q1 addendum — the token expires in 300 seconds, so a
research session outlives it and must refresh mid-flight). Building the agent on the unverified
shape first would mean debugging a model and a request shape at the same time, with no way to tell
which was lying.

## 4. Implementation

Python core, the Anthropic SDK, and the SDK's tool runner rather than a hand-written loop.

```python
import anthropic
from anthropic import beta_tool

client = anthropic.Anthropic()

@beta_tool
def catalog_search(query: str, category: str | None = None) -> str:
    """Search the local klm catalog for existing approved parts.

    Args:
        query: free-text or parametric query
        category: optional category path filter, e.g. 'IC/Power/Regulator'
    """
    return json.dumps(services.catalog.search(query, category=category))

runner = client.beta.messages.tool_runner(
    model="claude-opus-5",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},
    system=RESEARCH_SYSTEM_PROMPT,
    tools=[catalog_search, supplier_search, datasheet_fetch,
           datasheet_extract, footprint_lookup, propose_part],
    messages=[{"role": "user", "content": requirement.to_prompt()}],
)

for message in runner:
    ui.stream(message)
```

Model and parameter choices:

- **`claude-opus-5`** for research. This is multi-step work — search, read a PDF, cross-check,
  compare — where the difference in quality is worth the cost, and a part chosen wrongly costs
  three weeks of shipping.
- **`claude-haiku-4-5`** for bulk mechanical extraction (parsing a parametric table out of 40
  supplier rows), where the task is narrow and volume matters.
- **Adaptive thinking** with `effort: "high"`. Research genuinely benefits from deliberation;
  routine refreshes drop to `medium`.
- **Streaming**, so the user watches searches happen instead of staring at a spinner for a minute.
- **Structured outputs** (`output_config.format`) for the final proposal, so it validates against
  klm's schema rather than being parsed out of prose.

Costs are metered per research session and shown in the UI. An API key is required for agent
features only; klm's entire non-agent surface works without one.

## 5. Guardrails

| Guardrail | Mechanism |
|---|---|
| **Cannot write to the catalog** | No tool exists. `propose_part` writes to a review queue. |
| **Every parameter cited** | `datasheet_extract` must return page + quote; parameters without provenance are rejected at the schema level |
| **Conflicts surfaced, not resolved** | When a datasheet and a supplier field disagree, both are recorded and the candidate is flagged for review |
| **Stock and lifecycle verified** | Claims about availability come from a live offer, never from the model's recollection |
| **No invented part numbers** | Every proposed MPN must be traceable to a supplier search result. Unbacked MPNs are dropped before review |
| **Bounded** | Iteration cap, token budget per session, and a spend ceiling |
| **Logged** | Every session's tool calls and results go to `event_log`, so a bad part can be traced to the reasoning that produced it |

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

- **Datasheet Q&A**: "what's the maximum junction temperature?" against a cached PDF.
- **Substitute finding**: a stocked part goes to zero; find pin-compatible alternatives. Pin
  compatibility is checked mechanically against the catalog's own footprint and pin data, with
  the model handling only the search and the electrical-equivalence argument.
- **Categorization and description cleanup**: proposing consistent descriptions across a catalog
  imported from a messy library. Bulk, mechanical, cheap model.
- **Design review**: given a schematic's BOM, flag parts running near a rating, missing decoupling,
  or a lifecycle problem. Advisory only, clearly labelled as such.

Each is a bounded task with its own tool subset. None of them gains write access.
