"""Tests for the agent's tools.

The tools *are* the guardrails (docs/adr/0006), so what is protected here is
mostly what the agent cannot do:

* no tool writes — and the connection they hold refuses one;
* no tool takes or returns a supplier's parameter identifier;
* a search that could not apply every constraint says so, loudly;
* a category the agent named ambiguously is refused with its candidates, not
  resolved by picking the first.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.projects import seed_resistor

from klm.model import Offer, PriceBreak
from klm.research.tools import (
    ResearchContext,
    Toolset,
    build_toolset,
    validate_arguments,
)
from klm.store.db import connect
from klm.store.paths import Paths
from klm.suppliers.base import SearchHit

CATEGORIES = [
    {"id": "100", "name": "Semiconductors", "path": "Semiconductors", "products_count": 900},
    {
        "id": "111",
        "name": "Switching regulators",
        "path": "Semiconductors/Power/Switching regulators",
        "products_count": 400,
    },
    {
        "id": "112",
        "name": "Linear regulators",
        "path": "Semiconductors/Power/Linear regulators",
        "products_count": 300,
    },
]


class FakeTme:
    """A TME adapter that records what it was asked for."""

    name = "tme"
    currency = "PLN"

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.resolution: object = None

    def categories(self) -> list[dict[str, object]]:
        return [dict(c) for c in CATEGORIES]

    def search_parametric_report(self, category, filters, *, limit=50):  # type: ignore[no-untyped-def]
        from klm.suppliers.constraints import FilterGroup, Resolution

        self.asked.append((category, dict(filters)))
        resolution = Resolution()
        for name in filters:
            if name == "Vin max":
                resolution.groups.append(FilterGroup("2", "Vin max", ("101", "102")))
            else:
                resolution.unmapped.append((name, "no such parameter in this category"))
        self.resolution = resolution
        return self.hits[:limit], resolution

    def search_parametric(self, category, filters):  # type: ignore[no-untyped-def]
        return self.search_parametric_report(category, filters)[0]

    def get_offer(self, supplier_pn: str) -> Offer | None:
        if supplier_pn != "TPS62840":
            return None
        return Offer(
            supplier="tme",
            supplier_pn="TPS62840",
            mpn="TPS62840DLCR",
            manufacturer="Texas Instruments",
            stock=1200,
            currency="PLN",
            price_breaks=[PriceBreak(100, 1.9), PriceBreak(1, 2.4)],
        )


HIT = SearchHit(
    supplier="tme",
    supplier_pn="TPS62840",
    mpn="TPS62840DLCR",
    manufacturer="Texas Instruments",
    description="Buck converter, 750 mA",
    package="SOT-563",
    stock=1200,
)


@pytest.fixture
def toolset(env) -> Toolset:  # type: ignore[no-untyped-def]
    _, conn, store = env
    seed_resistor(store, conn)
    return build_toolset(
        ResearchContext(conn=conn, store=store, adapters={"tme": FakeTme([HIT])})
    )


def call(tools: Toolset, name: str, **arguments: object) -> dict:
    return json.loads(tools.call(name, arguments))


# ---------------------------------------------------------------------------
# What the agent can reach
# ---------------------------------------------------------------------------


def test_no_tool_writes_anything(toolset: Toolset) -> None:
    """The guarantee is the tool list, not the prompt (docs/adr/0006)."""
    names = {tool.name for tool in toolset.tools}
    assert names == {"catalog_search", "footprint_lookup", "supplier_search", "supplier_get_offer"}
    for banned in ("propose", "save", "add", "write", "order", "delete", "edit"):
        assert not any(banned in name for name in names)


def test_a_read_only_connection_refuses_a_write(tmp_path: Path) -> None:
    """SQLite enforces it, so a future tool cannot quietly acquire the ability."""
    from klm.store.db import migrate

    paths = Paths(tmp_path / "home")
    paths.create()
    writable = connect(paths.db)
    migrate(writable)
    writable.close()

    conn = connect(paths.db, create=False, read_only=True)
    try:
        conn.execute("SELECT COUNT(*) FROM part").fetchone()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM part")
    finally:
        conn.close()


def test_a_supplier_with_no_credentials_removes_its_tools(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    tools = build_toolset(ResearchContext(conn=conn, store=store, adapters={}))
    names = {tool.name for tool in tools.tools}
    assert names == {"catalog_search", "footprint_lookup"}, "absence removes the tool"


def test_every_schema_refuses_what_it_did_not_ask_for(toolset: Toolset) -> None:
    for tool in toolset.tools:
        assert tool.schema["additionalProperties"] is False
        assert tool.schema["required"], f"{tool.name} accepts an empty call"


def test_definitions_are_what_the_api_is_told(toolset: Toolset) -> None:
    definition = toolset.get("catalog_search").definition()  # type: ignore[union-attr]
    assert definition["name"] == "catalog_search"
    assert definition["input_schema"]["properties"]["query"]["type"] == "string"
    assert "reusing an approved part" in definition["description"]


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


def test_an_unknown_tool_is_a_result_not_an_exception(toolset: Toolset) -> None:
    payload = call(toolset, "propose_part", mpn="X")
    assert "no tool named" in payload["error"]
    assert "catalog_search" in payload["available"]


def test_arguments_are_validated_before_a_service_sees_them(toolset: Toolset) -> None:
    """`strict` is a promise from the other end of a network connection."""
    payload = call(toolset, "catalog_search", query="LM317", limit="lots")
    assert "do not fit" in payload["error"]
    assert any("whole number" in problem for problem in payload["problems"])


def test_a_missing_required_argument_is_reported(toolset: Toolset) -> None:
    payload = call(toolset, "catalog_search")
    assert any("missing required" in problem for problem in payload["problems"])


def test_an_invented_argument_is_refused(toolset: Toolset) -> None:
    payload = call(toolset, "catalog_search", query="R", parameter_id="2")
    assert any("unknown property 'parameter_id'" in problem for problem in payload["problems"])


def test_a_supplier_outage_comes_back_as_a_result(toolset: Toolset) -> None:
    from klm.suppliers.base import SupplierUnavailable

    def explode(*_: object, **__: object) -> None:
        raise SupplierUnavailable("tme: connection refused")

    tool = toolset.get("supplier_get_offer")
    broken = Toolset((type(tool)(tool.name, tool.description, tool.schema, explode),))  # type: ignore[union-attr]
    payload = json.loads(broken.call("supplier_get_offer", {"supplier": "tme", "supplier_pn": "X"}))
    assert "connection refused" in payload["error"]


# ---------------------------------------------------------------------------
# catalog_search
# ---------------------------------------------------------------------------


def test_catalog_search_finds_a_part_and_hides_the_hashes(toolset: Toolset) -> None:
    payload = call(toolset, "catalog_search", query="RC0402")
    assert payload["count"] == 1
    (part,) = payload["parts"]
    assert part["mpn"] == "RC0402FR-074K7L"
    assert part["approved"] is True
    assert part["has_footprint"] is True
    assert "symbol_hash" not in part, "a model must not be shown a content hash"


def test_catalog_search_says_nothing_matched(toolset: Toolset) -> None:
    assert call(toolset, "catalog_search", query="TPS62840")["parts"] == []


# ---------------------------------------------------------------------------
# supplier_search
# ---------------------------------------------------------------------------


def test_supplier_search_resolves_a_category_the_agent_named(toolset: Toolset) -> None:
    payload = call(toolset, "supplier_search", category="Switching regulators")
    assert payload["category"] == "Semiconductors/Power/Switching regulators"
    assert payload["results"][0]["mpn"] == "TPS62840DLCR"


def test_an_ambiguous_category_is_refused_with_its_candidates(toolset: Toolset) -> None:
    """Picking the first match searches a category nobody asked about."""
    payload = call(toolset, "supplier_search", category="regulators")
    assert "no single category" in payload["error"]
    assert "Semiconductors/Power/Switching regulators" in payload["candidates"]
    assert "results" not in payload


def test_the_agent_states_constraints_in_human_units_and_never_an_id(
    toolset: Toolset, env
) -> None:  # type: ignore[no-untyped-def]
    payload = call(
        toolset,
        "supplier_search",
        category="Semiconductors/Power/Switching regulators",
        constraints={"Vin max": ">=18V"},
    )
    assert payload["constraints_applied"] == ["Vin max: 2 matching value(s)"]
    assert not payload["constraints_not_applied"]
    assert payload["warning"] is None
    # No identifier appears anywhere in what the model is handed.
    assert "101" not in json.dumps(payload)


def test_an_unapplied_constraint_is_shouted_about(toolset: Toolset) -> None:
    """Results not filtered by a constraint look exactly like results that were."""
    payload = call(
        toolset,
        "supplier_search",
        category="Semiconductors/Power/Switching regulators",
        constraints={"Vin max": ">=18V", "Iq": "<=100nA"},
    )
    assert payload["constraints_applied"] == ["Vin max: 2 matching value(s)"]
    assert payload["constraints_not_applied"] == ["Iq: no such parameter in this category"]
    assert "not filtered" in payload["warning"]


def test_supplier_search_is_absent_without_tme(env) -> None:  # type: ignore[no-untyped-def]
    """LCSC has no search klm may use, so LCSC alone means no supplier_search."""
    _, conn, store = env

    class Lcsc:
        name = "lcsc"
        currency = "USD"

        def get_offer(self, supplier_pn: str) -> None:
            return None

    tools = build_toolset(ResearchContext(conn=conn, store=store, adapters={"lcsc": Lcsc()}))
    assert tools.get("supplier_search") is None
    assert tools.get("supplier_get_offer") is not None


# ---------------------------------------------------------------------------
# supplier_get_offer
# ---------------------------------------------------------------------------


def test_an_offer_carries_the_live_numbers_in_break_order(toolset: Toolset) -> None:
    payload = call(toolset, "supplier_get_offer", supplier="tme", supplier_pn="TPS62840")
    assert payload["stock"] == 1200
    assert [b["qty"] for b in payload["price_breaks"]] == [1, 100]


def test_an_unknown_part_number_is_said_plainly(toolset: Toolset) -> None:
    payload = call(toolset, "supplier_get_offer", supplier="tme", supplier_pn="NOPE")
    assert "no part" in payload["error"]


def test_the_supplier_must_be_one_that_exists(toolset: Toolset) -> None:
    payload = call(toolset, "supplier_get_offer", supplier="digikey", supplier_pn="X")
    assert any("must be one of" in problem for problem in payload["problems"])


# ---------------------------------------------------------------------------
# footprint_lookup
# ---------------------------------------------------------------------------


def test_a_package_already_in_the_catalog_reads_as_reuse(toolset: Toolset, env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    from klm.services.assets import register_asset
    from klm.store.assets import AssetKind

    content_hash = store.add_bytes(b"(footprint)", AssetKind.FOOTPRINT)
    register_asset(
        conn,
        content_hash,
        AssetKind.FOOTPRINT,
        filename="R_0402_1005Metric",
        source="generated",
    )
    payload = call(toolset, "footprint_lookup", package="0402", designator="R")
    assert payload["already_in_catalog"] is True
    assert payload["source"] == "catalog"


def test_a_chip_klm_can_generate_says_generate(toolset: Toolset) -> None:
    payload = call(toolset, "footprint_lookup", package="0402", designator="R")
    assert payload["source"] in ("catalog", "kicad", "generate")
    assert payload["recognised"] is True


def test_a_package_klm_has_never_heard_of_is_not_invented(toolset: Toolset) -> None:
    payload = call(toolset, "footprint_lookup", package="WLCSP-49-0.35mm-unheard-of")
    assert payload["recognised"] is False
    assert payload["source"] == "none"
    assert "human" in payload["note"]


# ---------------------------------------------------------------------------
# The validator itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "value", "ok"),
    [
        ({"type": "string"}, "x", True),
        ({"type": "string"}, 1, False),
        ({"type": "integer"}, True, False),  # a bool is not a number here
        ({"type": "number"}, 1.5, True),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 9, False),
        ({"type": "array", "items": {"type": "string"}}, ["a"], True),
        ({"type": "array", "items": {"type": "string"}}, [1], False),
        ({"type": "string", "enum": ["tme"]}, "lcsc", False),
    ],
)
def test_the_validator_covers_the_schemas_klm_writes(schema, value, ok) -> None:
    assert (validate_arguments(schema, value) == []) is ok
