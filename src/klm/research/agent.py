"""The research loop — a requirement in, a transcript of what was found out.

klm drives the loop itself rather than handing it to the SDK's tool runner.
That is a deliberate trade and worth stating, because the runner is the
documented default:

* **The guardrails are the point of this phase** (docs/11 §5) — an iteration
  cap, a token budget, a spend ceiling, and every tool call in `event_log`.
  Owning the loop makes each of those a line of code with a test beside it,
  rather than a hook that has to be wired into someone else's loop.
* **klm's tools are data, not decorated functions.** :class:`~klm.research.tools.Toolset`
  is built at runtime from what this machine actually has — no TME credentials
  means no `supplier_search` — and it is that list that gets validated,
  executed and logged. The decorator-based runner wants module-level functions
  with type annotations, which is the wrong shape for a tool set that differs
  per machine.
* **It is testable without an API key or the SDK.** The loop talks to
  :class:`~klm.llm.client.ModelClient`, so every guardrail here is exercised
  against a fake that costs nothing. A guardrail that can only be tested by
  spending money is a guardrail nobody tests.

Two rules the loop enforces, both of which exist because the failure they
prevent looks like success:

* **A session that stopped early says so.** Hitting the iteration cap or the
  spend ceiling ends the run with an answer that is *partial*, and a partial
  answer presented as a finished one is worse than no answer at all.
* **A tool never fails the session.** A supplier outage, an unknown category,
  an unrecognised package all come back to the model as results it can act on
  — that is `Toolset.call`'s job — so the loop has nothing to catch.

Nothing here decides anything. The output is a transcript for a human to read
and a set of candidates for a human to approve (docs/adr/0006).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from klm.llm.client import LlmError, ModelClient, Usage
from klm.research.requirement import Requirement
from klm.research.tools import Toolset
from klm.services.events import ACTOR_AGENT, record

__all__ = [
    "SYSTEM_PROMPT",
    "Limits",
    "Pricing",
    "Step",
    "Transcript",
    "research",
]


SYSTEM_PROMPT = """\
You are helping a hardware engineer choose a component for a KiCad project. \
They buy from TME (Poland, fast) and LCSC (cheap, slow, and the one JLCPCB's \
assembly service consumes). Your output is a proposal: a human reads it and \
decides. Nothing you do changes their catalog.

Work through the tools you have. They are the only source of fact available to \
you, and the rules below are about what the tools do and do not tell you.

Search the existing catalog first. A part they already have needs no new \
footprint, no new order, no new drawer and no new thing to maintain — if a \
catalog part is within tolerance of the requirement, say so and justify \
introducing a new one rather than the other way round.

Every part number you name must appear in a tool result. A plausible MPN that \
does not exist is the most expensive mistake available here: it survives \
review, gets ordered, and comes back three weeks later as nothing. If you \
cannot find a real part, say that.

Price, stock and packaging come from supplier_get_offer and nowhere else — not \
from a search result and not from recollection. Stock of null means the \
supplier did not say, which is not the same as zero.

supplier_search searches TME only. klm has no searchable API for LCSC, so a \
part being absent from your results says nothing about whether LCSC stocks it. \
Say "TME only" rather than implying LCSC came back empty.

When a search reports constraints_not_applied, the results were not filtered \
on those parameters. Check them yourself against each candidate rather than \
treating the list as filtered.

Report a near miss rather than nothing. "Nothing meets your hard constraints; \
the closest is X, which fails only on Iout" is a useful answer; an empty one is \
not. State every constraint check explicitly, including the failures.

Finish with a ranked shortlist. For each candidate give: the MPN and \
manufacturer, which constraints pass and which fail with the actual values, \
where it can be bought and at what price, whether its footprint already exists \
(footprint_lookup answers this — do not guess), and what is wrong with it. A \
candidate with nothing wrong with it is a candidate you have not looked at \
hard enough."""


@dataclass(frozen=True)
class Limits:
    """What bounds a session (docs/11 §5).

    All three exist because a research loop is open-ended by construction: it
    stops when the model decides it is finished, and "decides it is finished"
    is not a thing to bet a bill on.
    """

    max_iterations: int = 20
    max_tokens: int = 400_000
    max_spend: float = 2.00
    """In the pricing currency, USD. Checked before each request, so the
    ceiling bounds what a session *starts*, not what it has already spent."""


@dataclass(frozen=True)
class Pricing:
    """Published per-million-token rates, in USD.

    Configuration rather than a constant, and defaulted rather than fetched:
    published prices change, and a cost estimate that silently used last
    year's numbers is worse than one a user knows to check.
    """

    input_per_mtok: float = 5.00
    output_per_mtok: float = 25.00
    cache_read_per_mtok: float = 0.50
    cache_write_per_mtok: float = 6.25

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_input_tokens * self.cache_read_per_mtok
            + usage.cache_creation_input_tokens * self.cache_write_per_mtok
        ) / 1_000_000


@dataclass(frozen=True)
class Step:
    """One tool call, as it happened."""

    iteration: int
    name: str
    arguments: dict[str, Any]
    result: str
    """The JSON handed back to the model, verbatim."""


@dataclass
class Transcript:
    """What a session did, and whether it finished."""

    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    iterations: int = 0
    stopped: str = "answered"
    """`answered` | `iteration_cap` | `token_budget` | `spend_ceiling` |
    `refusal` | `max_tokens` | `context_exceeded` | `model_error`."""
    note: str = ""

    @property
    def complete(self) -> bool:
        """Whether the model finished, rather than being cut off."""
        return self.stopped == "answered"

    def tools_used(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.name] = counts.get(step.name, 0) + 1
        return counts

    def summary(self) -> list[str]:
        lines = [
            f"{self.iterations} turn(s), {len(self.steps)} tool call(s), "
            f"{self.usage.total} token(s), ~${self.cost:.2f}"
        ]
        if not self.complete:
            lines.append(f"stopped early: {self.stopped}{f' — {self.note}' if self.note else ''}")
        return lines


def research(
    requirement: Requirement,
    toolset: Toolset,
    client: ModelClient,
    *,
    limits: Limits | None = None,
    pricing: Pricing | None = None,
    conn: sqlite3.Connection | None = None,
    on_text: Callable[[str], None] | None = None,
    on_step: Callable[[Step], None] | None = None,
) -> Transcript:
    """Run one research session.

    ``conn`` is a *writable* connection, used only for the event log — the
    tools hold their own read-only one. That split is the whole guarantee:
    klm records what the agent did; the agent cannot record anything.
    """
    limits = limits or Limits()
    pricing = pricing or Pricing()
    transcript = Transcript()

    definitions = toolset.definitions()
    messages: list[dict[str, Any]] = [{"role": "user", "content": requirement.to_prompt()}]
    _log(conn, "research.start", requirement.kind, {
        "constraints": [c.name for c in requirement.constraints],
        "tools": [tool.name for tool in toolset.tools],
        "model": getattr(client, "model", "unknown"),
    })

    while True:
        stop = _over_budget(transcript, limits)
        if stop is not None:
            transcript.stopped, transcript.note = stop
            break
        if transcript.iterations >= limits.max_iterations:
            transcript.stopped = "iteration_cap"
            transcript.note = f"stopped after {limits.max_iterations} turns"
            break

        try:
            reply = client.reply(
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=definitions,
                on_text=on_text,
            )
        except LlmError as exc:
            transcript.stopped = "model_error"
            transcript.note = str(exc)
            break

        transcript.iterations += 1
        transcript.usage = transcript.usage + reply.usage
        transcript.cost = pricing.cost(transcript.usage)
        if reply.text:
            transcript.answer = reply.text

        # A refusal carries no usable content — checking `stop_reason` before
        # reading it is the difference between a clear message and an
        # IndexError.
        if reply.stop_reason == "refusal":
            transcript.stopped = "refusal"
            transcript.note = "the model declined this request"
            break

        content = reply.content if reply.content is not None else reply.text
        messages.append({"role": "assistant", "content": content})

        if reply.stop_reason == "model_context_window_exceeded":
            transcript.stopped = "context_exceeded"
            transcript.note = "the conversation outgrew the context window"
            break
        if reply.stop_reason == "max_tokens" and not reply.tool_calls:
            transcript.stopped = "max_tokens"
            transcript.note = "the answer was cut off; raise max_tokens"
            break
        if reply.stop_reason == "pause_turn":
            # Nothing for klm to run — re-send so the server resumes.
            continue
        if not reply.tool_calls:
            transcript.stopped = "answered"
            break

        results: list[dict[str, Any]] = []
        for call in reply.tool_calls:
            payload = toolset.call(call.name, call.arguments)
            step = Step(
                iteration=transcript.iterations,
                name=call.name,
                arguments=dict(call.arguments),
                result=payload,
            )
            transcript.steps.append(step)
            _log(conn, f"research.tool.{call.name}", requirement.kind, {
                "arguments": dict(call.arguments),
                "result": payload[:2000],
            })
            if on_step is not None:
                on_step(step)
            # No `is_error`: a supplier outage or an unknown category is a
            # result the agent can act on, and `Toolset.call` has already
            # shaped it as one.
            results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": payload}
            )
        messages.append({"role": "user", "content": results})

    _log(conn, "research.end", requirement.kind, {
        "stopped": transcript.stopped,
        "note": transcript.note,
        "iterations": transcript.iterations,
        "tool_calls": len(transcript.steps),
        "tokens": transcript.usage.total,
        "cost": round(transcript.cost, 4),
    })
    return transcript


def _over_budget(transcript: Transcript, limits: Limits) -> tuple[str, str] | None:
    """Whether another request may be made.

    Checked *before* a request rather than after, because a limit that stops
    the session only once it has been exceeded is a limit that is always
    exceeded.
    """
    if transcript.usage.total >= limits.max_tokens:
        return "token_budget", f"{transcript.usage.total} tokens, limit {limits.max_tokens}"
    if transcript.cost >= limits.max_spend:
        return "spend_ceiling", f"${transcript.cost:.2f}, limit ${limits.max_spend:.2f}"
    return None


def _log(
    conn: sqlite3.Connection | None, action: str, subject: str, detail: dict[str, Any]
) -> None:
    if conn is not None:
        record(conn, ACTOR_AGENT, action, subject=subject, detail=detail)
