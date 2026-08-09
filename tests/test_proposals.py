"""Tests for the review queue and the two mechanical guards in front of it.

The queue's job is to keep the agent's output *separate* from the catalog
until a human acts, and to make two of docs/11 §5's guardrails structural
rather than prompted:

* an MPN that never came back from a tool cannot be proposed at all;
* a parameter whose quote klm never read is dropped from the proposal.

Approving produces a **draft**, which still faces the QA gate and lint.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.projects import seed_resistor

from klm.llm.client import Citation, Reply, Segment
from klm.model import PartStatus
from klm.research.tools import ResearchContext, build_toolset
from klm.services.catalog import get_part
from klm.services.events import history
from klm.services.proposals import (
    CitedParameter,
    ConstraintCheck,
    Proposal,
    ProposalError,
    ProposedOffer,
    approve,
    get,
    list_proposals,
    reject,
    save,
)

PDF = b"%PDF-1.7\nfake\n%%EOF"


def candidate(**overrides: Any) -> Proposal:
    settings: dict[str, Any] = {
        "mpn": "TPS62840DLCR",
        "manufacturer": "Texas Instruments",
        "why": "Lowest Iq in the set.",
        "package": "SOT-563",
        "category": "IC/Power/Regulator/Switching",
        "concerns": ["SOT-563 is not in the catalog; a new footprint."],
        "parameters": [CitedParameter("Vin max", "6.5 V", 1, "VIN 1.8 to 6.5 V")],
        "checks": [ConstraintCheck("iout", ">=1A", "750 mA", "FAIL")],
        "offers": [ProposedOffer("tme", "TPS62840", 1200, 1.9, "PLN")],
    }
    settings.update(overrides)
    return Proposal(**settings)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_a_proposal_round_trips_with_its_evidence(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env

    stored = save(conn, candidate())
    read = get(conn, stored.id or 0)

    assert read is not None
    assert read.mpn == "TPS62840DLCR"
    assert read.parameters[0].quote == "VIN 1.8 to 6.5 V"
    assert read.parameters[0].page == 1
    assert read.checks[0].status == "FAIL"
    assert read.offers[0].unit_price == 1.9
    assert read.concerns and read.state == "pending"


def test_a_proposal_is_not_a_part(env) -> None:  # type: ignore[no-untyped-def]
    """The whole point of a separate table."""
    _, conn, _ = env
    save(conn, candidate())
    assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0


def test_a_proposal_needs_an_mpn_and_a_manufacturer(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env
    with pytest.raises(ProposalError, match="needs an MPN"):
        save(conn, candidate(mpn="  "))
    with pytest.raises(ProposalError, match="needs a manufacturer"):
        save(conn, candidate(manufacturer=""))


def test_the_queue_lists_what_is_pending(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env
    save(conn, candidate(mpn="A"))
    second = save(conn, candidate(mpn="B"))
    reject(conn, second.id or 0, "wrong package")

    assert [p.mpn for p in list_proposals(conn)] == ["A"]
    assert len(list_proposals(conn, state=None)) == 2


def test_a_rejection_needs_a_reason(env) -> None:  # type: ignore[no-untyped-def]
    """The rejection log is the evidence for what the prompt is missing."""
    _, conn, _ = env
    stored = save(conn, candidate())
    with pytest.raises(ProposalError, match="needs a reason"):
        reject(conn, stored.id or 0, "   ")


def test_a_decision_is_made_once(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env
    stored = save(conn, candidate())
    reject(conn, stored.id or 0, "wrong package")
    with pytest.raises(ProposalError, match="already rejected"):
        reject(conn, stored.id or 0, "again")


def test_deciding_is_recorded(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, _ = env
    stored = save(conn, candidate())
    reject(conn, stored.id or 0, "Iout is short by 250 mA")

    actions = {event.action: event for event in history(conn)}
    assert "proposal.created" in actions
    rejected = actions["proposal.rejected"]
    assert rejected.detail is not None
    assert rejected.detail["reason"] == "Iout is short by 250 mA"


def test_approving_produces_a_draft_not_an_approved_part(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    stored = save(conn, candidate(mpn="RC0402FR-074K7L", manufacturer="Yageo", package="0402",
                                  category="Passive/Resistor"))

    proposal, report = approve(conn, store, stored.id or 0)

    assert proposal.state == "approved" and proposal.klm_id
    part = get_part(conn, report.part.klm_id)
    assert part is not None
    assert part.status is PartStatus.DRAFT, "a human said 'worth having', not 'correct'"


# ---------------------------------------------------------------------------
# The guards in front of the queue
# ---------------------------------------------------------------------------


class FakeReader:
    model = "fake-reader"

    def __init__(self, *segments: Segment) -> None:
        self.segments = segments

    def reply(self, *, system: Any, messages: Any, tools: Any, on_text: Any = None) -> Reply:
        return Reply(text="", segments=self.segments)


def tools_for(env, tmp_path=None, reader=None):  # type: ignore[no-untyped-def]
    _, conn, store = env
    return build_toolset(
        ResearchContext(
            conn=conn,
            store=store,
            datasheet_cache=tmp_path,
            fetcher=(lambda url, timeout: PDF) if tmp_path else None,
            reader=reader,
        )
    )


def propose(tools, **arguments: Any) -> dict:  # type: ignore[no-untyped-def]
    import json

    payload: dict[str, Any] = {
        "mpn": "RC0402FR-074K7L",
        "manufacturer": "Yageo",
        "why": "already in the catalog",
        "concerns": ["none found"],
    }
    payload.update(arguments)
    return json.loads(tools.call("propose_part", payload))


def test_an_mpn_no_tool_returned_cannot_be_proposed(env) -> None:  # type: ignore[no-untyped-def]
    """The invented-MPN guard, and why it is a function and not a prompt line."""
    tools = tools_for(env)

    payload = propose(tools, mpn="TPS99999XYZ")

    assert "has not appeared in any tool result" in payload["error"]
    assert tools.ledger.staged == []


def test_an_mpn_a_tool_returned_can_be_proposed(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    seed_resistor(store, conn)
    tools = tools_for(env)
    import json

    json.loads(tools.call("catalog_search", {"query": "RC0402"}))
    payload = propose(tools)

    assert payload["recorded"] is True
    (staged,) = tools.ledger.staged
    assert staged.mpn == "RC0402FR-074K7L"


def test_a_proposal_with_no_concerns_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    seed_resistor(store, conn)
    tools = tools_for(env)
    import json

    json.loads(tools.call("catalog_search", {"query": "RC0402"}))
    payload = propose(tools, concerns=[])

    assert "at least one concern" in payload["error"]


def test_a_parameter_klm_never_read_is_dropped_from_the_proposal(env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """It may even be true. It is not something klm read in a datasheet."""
    import json

    _, conn, store = env
    seed_resistor(store, conn)
    reader = FakeReader(
        Segment("Vin max: 6.5 V", (Citation(quote="VIN 1.8 to 6.5 V", start_page=3),))
    )
    tools = tools_for(env, tmp_path=tmp_path, reader=reader)

    json.loads(tools.call("catalog_search", {"query": "RC0402"}))
    handle = json.loads(tools.call("datasheet_fetch", {"url": "https://x.test/a.pdf"}))["handle"]
    json.loads(
        tools.call("datasheet_extract", {"handle": handle, "parameters": ["Vin max"]})
    )

    payload = propose(
        tools,
        parameters=[
            {"name": "Vin max", "value": "6.5 V", "page": 3, "quote": "VIN 1.8 to 6.5 V"},
            {"name": "Iq", "value": "60 nA", "page": 1, "quote": "IQ is 60 nA typical"},
        ],
    )

    assert payload["parameters_kept"] == 1
    assert "Iq: dropped" in payload["parameters_dropped"][0]
    (staged,) = tools.ledger.staged
    assert [p.name for p in staged.parameters] == ["Vin max"]
    assert staged.notes, "the drop is on the proposal a human reads, too"
