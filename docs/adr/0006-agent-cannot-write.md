# ADR-0006 — The AI agent proposes; it never writes to the catalog

**Status:** accepted · **Phase:** 9 · **Related:** [11](../11-ai-research-agent.md)

## Context

The research agent's job is to find candidate parts: search suppliers, read datasheets, extract
parameters, compare. The natural next step — "and then add the good one to the catalog" — is
technically easy and would remove the last manual step.

The catalog's entire value is that its contents are trustworthy. A part with a wrong pin count,
an invented MPN, or a voltage rating hallucinated from a similar part doesn't fail loudly; it
fails on a fabricated board, weeks and tens of złoty later.

## Decision

**The agent has no tool that writes to the catalog, edits a file, or spends money.** Its terminal
action is `propose_part`, which writes to a review queue.

```
agent ──▶ proposal queue ──▶ human review ──▶ draft part ──▶ QA gate + lint ──▶ approved
```

Two gates, deliberately: a human decision, then the same mechanical checks every part faces.
Approving a proposal does not produce a usable part — it produces a `draft` that must still pass
asset QA ([08 §5](../08-asset-pipeline.md#5-the-qa-gate)) and field lint
([05](../05-field-schema-and-linting.md)).

Supporting rules:

- **Every parameter carries provenance.** `datasheet_extract` must return a page number and a
  quoted snippet. A parameter without a source is rejected by the schema, not by a prompt.
- **No invented part numbers.** Every candidate MPN must trace to a supplier API response.
  Unbacked MPNs are dropped before review. This is the most likely and most expensive
  hallucination in this domain, and it's cheap to eliminate structurally.
- **Conflicts are surfaced, not resolved.** When a datasheet and a supplier field disagree, both
  are recorded and the candidate is flagged.
- **Availability claims come from live offers**, never from the model's recollection.
- **Everything is logged** to `event_log`, so a bad part can be traced back to the reasoning that
  produced it.

## Why this is architectural, not a prompt instruction

A system prompt saying "don't write to the catalog" is a request. An absent tool is a guarantee.
The distinction matters because the failure mode is silent: a hallucinated parameter looks
exactly like a correct one until a board comes back wrong.

The same reasoning drives the provenance requirement. Asking for citations in the prompt gets
citations most of the time. Making the extraction tool's schema *require* a page and a quote gets
them every time, and makes their absence a validation error rather than something to notice
during review.

## Consequences

**Good**
- The catalog stays trustworthy. Every entry passed a human and the mechanical checks.
- Every claim is auditable to a datasheet page or an API response.
- Rejections are logged with reasons — raw material for improving the requirement schema and the
  prompt. The failures tell you what the interface is missing.
- The agent can be swapped, upgraded or removed entirely without any risk to existing data.

**Bad**
- Not fully automatic. Adding a part still needs a human for a minute or two. This is the
  intended trade and, given the cost of a wrong part, the correct one.
- Review is a queue, and queues grow. Mitigated by making review fast: the proposal format shows
  constraint checks, concerns and citations up front, so the common case is a glance and an
  approval.

**Neutral**
- `propose_part` is the only tool with any write semantics, and it writes to a queue that is not
  the catalog. If the boundary ever needs revisiting, that single tool is where the conversation
  starts.
