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
| `supplier_search` | Parametric/keyword search at TME and LCSC | Returns offers with stock and price |
| `supplier_get_offer` | Details for one supplier part number | |
| `datasheet_fetch` | Download and cache a datasheet PDF | Returns a document handle, not raw text |
| `datasheet_extract` | Pull parameters from a cached datasheet | Must return page number + quoted snippet per parameter |
| `footprint_lookup` | Does the catalog already have this package? | Drives the reuse preference |
| `propose_part` | Emit a structured candidate | The only "write" — and it writes to a review queue, not the catalog |

Notably absent: any tool that writes to the catalog, edits a file, or spends money. The agent
physically cannot do those things — that's an architectural guarantee, not a prompt instruction.

### Reuse-first

`catalog_search` being the first tool, and the system prompt making reuse explicit, matters more
than it looks. Every avoided new part is one less thing to order, stock, label and maintain. The
agent is instructed to justify introducing a new part when a catalog part is within tolerance of
the requirement.

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
