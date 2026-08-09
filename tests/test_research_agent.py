"""Tests for the research loop.

The loop's job is to be *bounded* and *honest*: it must stop where it was told
to, and a session that stopped early must not read like one that finished. Both
are tested against a fake model, which is the point of the `ModelClient` seam —
a guardrail that can only be exercised by spending money is a guardrail nobody
exercises.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from tests.projects import seed_resistor

from klm.llm.client import LlmError, Reply, ToolCall, Usage
from klm.research.agent import Limits, Pricing, Transcript, research
from klm.research.requirement import parse_requirement
from klm.research.tools import ResearchContext, build_toolset
from klm.services.events import history
from klm.store.db import connect

REQUIREMENT = parse_requirement(
    {"kind": "buck_converter", "constraints": {"iout": ">=1A"}}
)


class FakeModel:
    """Replays a scripted set of replies and records what it was sent."""

    model = "fake-1"

    def __init__(self, *replies: Reply | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any],
        on_text: Any = None,
    ) -> Reply:
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        item = self.replies.pop(0) if self.replies else Reply(text="done.")
        if isinstance(item, Exception):
            raise item
        if on_text is not None and item.text:
            on_text(item.text)
        return item


def searched(query: str = "regulator", *, call_id: str = "t1") -> Reply:
    return Reply(
        text="",
        tool_calls=(ToolCall(call_id, "catalog_search", {"query": query}),),
        stop_reason="tool_use",
        usage=Usage(input_tokens=1000, output_tokens=200),
    )


@pytest.fixture
def toolset(env):  # type: ignore[no-untyped-def]
    _, conn, store = env
    seed_resistor(store, conn)
    return build_toolset(ResearchContext(conn=conn, store=store))


def run(model: FakeModel, toolset, **kwargs: Any) -> Transcript:  # type: ignore[no-untyped-def]
    return research(REQUIREMENT, toolset, model, **kwargs)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_a_tool_call_is_executed_by_klm_and_fed_back(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(searched("RC0402"), Reply(text="Use the 4k7."))

    transcript = run(model, toolset)

    assert transcript.complete
    assert transcript.answer == "Use the 4k7."
    assert [step.name for step in transcript.steps] == ["catalog_search"]
    # The result went back as a tool_result in its own user message.
    (result,) = model.calls[1]["messages"][2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert "RC0402FR-074K7L" in result["content"]


def test_the_requirement_is_what_the_model_is_asked(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(Reply(text="ok"))
    run(model, toolset)
    first = model.calls[0]
    assert "iout: >=1A" in first["messages"][0]["content"]
    assert "never disqualify" in first["messages"][0]["content"]
    assert {tool["name"] for tool in first["tools"]} == {"catalog_search", "footprint_lookup"}


def test_assistant_content_goes_back_verbatim(toolset) -> None:  # type: ignore[no-untyped-def]
    """Rebuilding blocks is how a conversation fails on its third turn."""
    blocks = [{"type": "text", "text": "thinking about it"}]
    model = FakeModel(
        Reply(text="x", tool_calls=(ToolCall("t1", "catalog_search", {"query": "r"}),),
              stop_reason="tool_use", content=blocks),
        Reply(text="done"),
    )

    run(model, toolset)

    assert model.calls[1]["messages"][1] == {"role": "assistant", "content": blocks}


def test_several_tool_calls_come_back_in_one_message(toolset) -> None:  # type: ignore[no-untyped-def]
    """Splitting them teaches the model to stop asking for parallel calls."""
    model = FakeModel(
        Reply(
            text="",
            tool_calls=(
                ToolCall("a", "catalog_search", {"query": "r"}),
                ToolCall("b", "footprint_lookup", {"package": "0402"}),
            ),
            stop_reason="tool_use",
        ),
        Reply(text="done"),
    )

    transcript = run(model, toolset)

    assert len(transcript.steps) == 2
    results = model.calls[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_an_unknown_tool_is_answered_not_raised(toolset) -> None:  # type: ignore[no-untyped-def]
    """The agent can recover from asking for something that isn't there."""
    model = FakeModel(
        Reply(text="", tool_calls=(ToolCall("t1", "propose_part", {"mpn": "X"}),),
              stop_reason="tool_use"),
        Reply(text="I could not propose it."),
    )

    transcript = run(model, toolset)

    assert transcript.complete
    assert "no tool named" in transcript.steps[0].result


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_the_iteration_cap_stops_the_session_and_says_so(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(*[searched() for _ in range(10)])

    transcript = run(model, toolset, limits=Limits(max_iterations=3))

    assert transcript.iterations == 3
    assert transcript.stopped == "iteration_cap"
    assert not transcript.complete, "a partial answer must not read as a finished one"


def test_the_token_budget_stops_before_the_next_request(toolset) -> None:  # type: ignore[no-untyped-def]
    """A limit that only trips once exceeded is a limit that is always exceeded."""
    model = FakeModel(*[searched() for _ in range(10)])

    transcript = run(model, toolset, limits=Limits(max_tokens=2500))

    assert transcript.stopped == "token_budget"
    assert transcript.usage.total < 2500 + 1200, "it stopped at the boundary, not far past it"


def test_the_spend_ceiling_stops_the_session(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(*[searched() for _ in range(20)])

    transcript = run(model, toolset, limits=Limits(max_spend=0.05))

    assert transcript.stopped == "spend_ceiling"
    assert transcript.cost >= 0.05
    assert "limit $0.05" in transcript.note


def test_cost_is_computed_from_the_published_rates() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert Pricing().cost(usage) == pytest.approx(30.0)


def test_a_refusal_ends_the_session_without_reading_content(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(Reply(text="", stop_reason="refusal", content=None))

    transcript = run(model, toolset)

    assert transcript.stopped == "refusal"
    assert not transcript.complete


def test_a_truncated_answer_is_reported_as_truncated(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(Reply(text="half an ans", stop_reason="max_tokens"))

    transcript = run(model, toolset)

    assert transcript.stopped == "max_tokens"
    assert "cut off" in transcript.note


def test_a_model_error_ends_the_session_rather_than_the_process(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(searched(), LlmError("RateLimitError: slow down"))

    transcript = run(model, toolset)

    assert transcript.stopped == "model_error"
    assert "RateLimitError" in transcript.note
    assert transcript.steps, "what it did before the failure is still reported"


def test_a_paused_turn_is_resumed_rather_than_ended(toolset) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(Reply(text="", stop_reason="pause_turn"), Reply(text="finished"))

    transcript = run(model, toolset)

    assert transcript.complete and transcript.answer == "finished"


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


def test_every_tool_call_reaches_the_event_log(env, toolset) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env
    model = FakeModel(searched("RC0402"), Reply(text="done"))

    run(model, toolset, conn=conn)

    actions = [event.action for event in history(conn, actor="agent")]
    assert "research.start" in actions
    assert "research.tool.catalog_search" in actions
    assert "research.end" in actions

    (call,) = [e for e in history(conn) if e.action == "research.tool.catalog_search"]
    assert call.detail is not None
    assert call.detail["arguments"] == {"query": "RC0402"}
    assert "RC0402FR-074K7L" in call.detail["result"]


def test_the_end_of_a_session_records_what_it_cost(env, toolset) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env

    model = FakeModel(*[searched() for _ in range(5)])
    run(model, toolset, conn=conn, limits=Limits(max_iterations=2))

    (end,) = [e for e in history(conn) if e.action == "research.end"]
    assert end.detail is not None
    assert end.detail["stopped"] == "iteration_cap"
    assert end.detail["tokens"] > 0


def test_the_agents_own_connection_still_cannot_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The audit trail is written by klm, on klm's connection — not the agent's."""
    from klm.store.db import migrate
    from klm.store.paths import Paths

    paths = Paths(tmp_path / "home")
    paths.create()
    writable = connect(paths.db)
    migrate(writable)

    reading = connect(paths.db, create=False, read_only=True)
    try:
        tools = build_toolset(ResearchContext(conn=reading))
        # klm logs through the writable connection; the tools hold the other one.
        run(FakeModel(Reply(text="ok")), tools, conn=writable)
        assert history(writable, actor="agent"), "klm recorded the session"
    finally:
        reading.close()
        writable.close()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_text_is_streamed_to_the_caller(toolset) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    run(FakeModel(Reply(text="a candidate")), toolset, on_text=seen.append)
    assert seen == ["a candidate"]


def test_each_tool_call_is_announced_as_it_happens(toolset) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    run(
        FakeModel(searched(), Reply(text="done")),
        toolset,
        on_step=lambda step: seen.append(step.name),
    )
    assert seen == ["catalog_search"]
