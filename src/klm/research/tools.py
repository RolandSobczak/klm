"""The tools the research agent is given, and the ones it deliberately is not.

A tool is a name, a strict JSON schema, and a klm function. The model chooses
*which* to call and with what; **klm executes every one of them** (docs/11 §3).
That sentence is the design: the agent's reach is the union of these four
functions, and nothing here writes.

What is absent is as deliberate as what is present:

* **No tool writes to the catalog.** Not "the prompt says not to" — there is no
  such function, and the connection these tools hold is opened `mode=ro`, so a
  write fails in SQLite rather than in a code review (docs/adr/0006).
* **No tool spends money, edits a file, or touches a project.**
* **`supplier_search` is TME-only.** Not a quality judgement: klm may not hold
  LCSC's API documentation at all (ADR-0009), so LCSC offers reach the catalog
  by a human typing a part number. A candidate the agent proposes may be
  TME-only, and the result says so rather than implying LCSC was checked.
* **No tool takes a supplier identifier.** The agent states a category by name
  and a constraint in human units; klm resolves both. A model asked for
  `parameters[0][id]=2` would eventually supply a plausible wrong one, and a
  wrong parameter ID returns *confidently wrong parts* rather than an error —
  the invented-MPN failure in another costume (docs/14 Q10).

Two rules about what comes back:

* **A tool never raises at the model.** A supplier that is down, a category
  that does not exist, a package klm has never heard of — all are results the
  agent can act on. An exception would end the session instead.
* **A tool never guesses.** An ambiguous category is returned *with its
  candidates* for the agent to choose between, and a constraint klm could not
  map is named in the response. Silently picking one, or silently dropping a
  filter and returning the results as though it had applied, is how a search
  reports success it did not achieve.

Arguments are validated here against the same schema the model was given,
before they reach a service. `strict` is a promise made by the other end of a
network connection, and a guardrail that holds only when a remote service
behaves is not a guardrail.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from klm.assets.kicad_libs import KicadLibraries
from klm.model import Offer, Part, PartStatus
from klm.services.assets import footprint_availability
from klm.services.catalog import CatalogError, search_parts
from klm.store.assets import AssetError, AssetStore
from klm.suppliers.base import SearchHit, SupplierAdapter, SupplierError
from klm.units import UnitError, ValueParseError

__all__ = [
    "ResearchContext",
    "Tool",
    "Toolset",
    "build_toolset",
    "validate_arguments",
]

MAX_RESULTS = 50


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def validate_arguments(schema: Mapping[str, Any], value: Any) -> list[str]:
    """Check a tool call against its schema; returns what is wrong with it.

    Deliberately a small subset — object, string, integer, number, boolean,
    array, `enum`, `required`, `additionalProperties: false` and numeric
    bounds — because that is all klm's tool schemas use. A dependency-free
    partial checker that covers every schema klm writes beats a full one klm's
    core is not allowed to import.
    """
    return _check(schema, value, "arguments")


def _check(schema: Mapping[str, Any], value: Any, where: str) -> list[str]:
    expected = schema.get("type")
    problems: list[str] = []

    if expected == "object":
        if not isinstance(value, Mapping):
            return [f"{where}: expected an object"]
        properties: Mapping[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{where}: missing required property {name!r}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    known = ", ".join(sorted(properties)) or "none"
                    problems.append(f"{where}: unknown property {name!r} (known: {known})")
        for name, item in value.items():
            if name in properties:
                problems.extend(_check(properties[name], item, f"{where}.{name}"))
        return problems

    if expected == "array":
        if not isinstance(value, list):
            return [f"{where}: expected an array"]
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                problems.extend(_check(items, item, f"{where}[{index}]"))
        return problems

    if expected == "string" and not isinstance(value, str):
        return [f"{where}: expected a string"]
    if expected == "boolean" and not isinstance(value, bool):
        return [f"{where}: expected true or false"]
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return [f"{where}: expected a whole number"]
    if expected == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        return [f"{where}: expected a number"]

    choices = schema.get("enum")
    if choices is not None and value not in choices:
        problems.append(f"{where}: must be one of {', '.join(map(str, choices))}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            problems.append(f"{where}: must be at least {low}")
        if high is not None and value > high:
            problems.append(f"{where}: must be at most {high}")
    return problems


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One callable the agent may reach, and the shape of a call to it."""

    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[..., Any]

    def definition(self) -> dict[str, Any]:
        """The tool as the API is told about it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


@dataclass
class ResearchContext:
    """Everything the tools are allowed to reach.

    The connection should be opened read-only (`connect(..., read_only=True)`).
    :func:`build_toolset` does not enforce that — it cannot tell — but every
    caller in klm does, and a test asserts a write through it fails.
    """

    conn: sqlite3.Connection
    store: AssetStore | None = None
    adapters: Mapping[str, SupplierAdapter] = field(default_factory=dict)
    libs: KicadLibraries | None = None


@dataclass(frozen=True)
class Toolset:
    """The tools built for one research session."""

    tools: tuple[Tool, ...]

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self.tools]

    def get(self, name: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> str:
        """Run one tool call and return the JSON the model gets back.

        Everything the agent could recover from comes back as a *result* — an
        unknown tool, arguments that do not fit, a supplier that is down. Only
        a genuine defect in klm is allowed to raise, because a bug that a
        research session silently absorbs is a bug nobody ever sees.
        """
        tool = self.get(name)
        if tool is None:
            known = ", ".join(t.name for t in self.tools)
            return _dump({"error": f"no tool named {name!r}", "available": known})

        given = dict(arguments or {})
        problems = validate_arguments(tool.schema, given)
        if problems:
            return _dump({"error": "the arguments do not fit this tool", "problems": problems})

        try:
            return _dump(tool.run(**given))
        except (
            SupplierError,
            CatalogError,
            AssetError,
            ValueParseError,
            UnitError,
            OSError,
        ) as exc:
            return _dump({"error": f"{type(exc).__name__}: {exc}"})


def _dump(payload: Any) -> str:
    # Sorted and compact: a tool result is context the model pays for, and two
    # identical results must not differ by key order across a session.
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Building the set
# ---------------------------------------------------------------------------


def build_toolset(context: ResearchContext) -> Toolset:
    """The tools available given what this machine actually has.

    No TME adapter means no `supplier_search`, and the agent learns that from
    the tool's absence rather than from a failure on its first call. Callers
    pass only adapters that can actually authenticate — credentials live in the
    environment and resolve at the point of use, so an adapter existing is not
    the same as a supplier being reachable.
    """
    tools: list[Tool] = [
        _catalog_search(context),
        _footprint_lookup(context),
    ]
    if "tme" in context.adapters:
        tools.append(_supplier_search(context))
    if context.adapters:
        tools.append(_supplier_get_offer(context))
    return Toolset(tuple(tools))


def _catalog_search(context: ResearchContext) -> Tool:
    def run(query: str, category: str | None = None, limit: int = 20) -> Any:
        found = search_parts(
            context.conn,
            query,
            category=category,
            limit=max(1, min(limit, MAX_RESULTS)),
        )
        return {
            "count": len(found),
            "parts": [_part_payload(part) for part in found],
        }

    return Tool(
        name="catalog_search",
        description=(
            "Search the parts this catalog already holds. Try this FIRST: reusing an approved "
            "part avoids a new footprint, a new order, a new drawer and a new thing to maintain. "
            "Matches a substring of the manufacturer part number, the manufacturer or the "
            "description. A part whose status is 'draft' has not been reviewed by a human yet."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring of an MPN, manufacturer or description.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional taxonomy path, e.g. 'IC/Power/Regulator'.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        run=run,
    )


def _supplier_search(context: ResearchContext) -> Tool:
    def run(
        category: str, constraints: Mapping[str, str] | None = None, limit: int = 25
    ) -> Any:
        adapter = context.adapters["tme"]
        resolved, candidates = _resolve_category(adapter, category)
        if resolved is None:
            return {
                "error": f"no single category matches {category!r}",
                "candidates": candidates,
                "hint": "call again with one of these paths, exactly as written",
            }

        report = getattr(adapter, "search_parametric_report", None)
        stated = dict(constraints or {})
        if report is None:  # pragma: no cover - every adapter with search has it
            hits = adapter.search_parametric(resolved["id"], stated)
            applied, unapplied = [], []
        else:
            hits, resolution = report(resolved["id"], stated, limit=min(limit, MAX_RESULTS))
            applied = [f"{g.name}: {len(g.value_ids)} matching value(s)" for g in resolution.groups]
            unapplied = [f"{name}: {why}" for name, why in resolution.unmapped]

        return {
            "category": resolved["path"],
            "constraints_applied": applied,
            "constraints_not_applied": unapplied,
            "warning": (
                "Some constraints were NOT applied — the results are not filtered by them. "
                "Check those yourself against each candidate."
                if unapplied
                else None
            ),
            "count": len(hits),
            "results": [_hit_payload(hit) for hit in hits[:limit]],
        }

    return Tool(
        name="supplier_search",
        description=(
            "Search TME by category and parameters. State constraints the way a person would "
            "('>=18V', '4.5..18V', '100nF') and name the parameter as the category spells it — "
            "klm turns those into TME's own identifiers. Only TME: klm has no searchable API for "
            "LCSC, so a result here says nothing about whether LCSC stocks the part. If a "
            "constraint could not be applied the response says so; treat those results as "
            "unfiltered on that parameter."
        ),
        schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category path or name, e.g. 'Switching regulators'.",
                },
                "constraints": {
                    "type": "object",
                    "description": (
                        "Parameter name to requirement, e.g. {'Vin max': '>=18V'}. "
                        "Never an identifier."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            },
            "required": ["category"],
            "additionalProperties": False,
        },
        run=run,
    )


def _supplier_get_offer(context: ResearchContext) -> Tool:
    suppliers = sorted(context.adapters)

    def run(supplier: str, supplier_pn: str) -> Any:
        adapter = context.adapters.get(supplier)
        if adapter is None:
            return {"error": f"{supplier} is not configured here", "available": suppliers}
        offer = adapter.get_offer(supplier_pn)
        if offer is None:
            return {"error": f"{supplier} has no part {supplier_pn!r}"}
        return _offer_payload(offer)

    return Tool(
        name="supplier_get_offer",
        description=(
            "Live price, stock and packaging for one supplier part number. Every claim you make "
            "about availability or price must come from this — never from recollection. Stock is "
            "null when the supplier did not state it, which is not the same as zero."
        ),
        schema={
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "enum": suppliers},
                "supplier_pn": {
                    "type": "string",
                    "description": "The supplier's own part number, from a search result.",
                },
            },
            "required": ["supplier", "supplier_pn"],
            "additionalProperties": False,
        },
        run=run,
    )


def _footprint_lookup(context: ResearchContext) -> Tool:
    def run(package: str, designator: str = "") -> Any:
        found = footprint_availability(
            context.conn,
            package,
            designator=designator,
            store=context.store,
            libs=context.libs,
        )
        return {
            "package": found.package,
            "recognised": found.known,
            "source": found.source,
            "kicad_footprint": found.kicad_id,
            "already_in_catalog": found.source == "catalog",
            "note": found.note,
        }

    return Tool(
        name="footprint_lookup",
        description=(
            "Where a package's footprint would come from: 'catalog' (klm already has it — the "
            "best outcome, prefer parts in packages that return this), 'kicad' (KiCad ships it), "
            "'generate' (klm can build the land pattern, chips only), or 'none' (a human would "
            "have to draw it — a real cost, say so in your proposal)."
        ),
        schema={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "e.g. 'SOT-23-6', '0402', 'QFN-16'."},
                "designator": {
                    "type": "string",
                    "description": (
                        "Reference designator letter for chip packages, where the same land "
                        "pattern is filed under R, C, L or D. e.g. 'C' for a 0402 capacitor."
                    ),
                },
            },
            "required": ["package"],
            "additionalProperties": False,
        },
        run=run,
    )


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
#
# Shaped by hand rather than dumped structurally. A model pays for every field
# it is shown, and content hashes and timestamps are both noise and an
# invitation to reason about identifiers it must not handle.


def _part_payload(part: Part) -> dict[str, Any]:
    return {
        "klm_id": part.klm_id,
        "mpn": part.mpn,
        "manufacturer": part.manufacturer,
        "description": part.description,
        "category": part.category,
        "package": part.package,
        "status": str(part.status),
        "approved": part.status is PartStatus.APPROVED,
        "lifecycle": str(part.lifecycle),
        "datasheet_url": part.datasheet_url,
        "has_footprint": part.footprint_hash is not None,
        "parameters": {
            parameter.name: str(parameter.value)
            for parameter in part.sorted_parameters()
            if parameter.value is not None
        },
    }


def _hit_payload(hit: SearchHit) -> dict[str, Any]:
    return {
        "supplier": hit.supplier,
        "supplier_pn": hit.supplier_pn,
        "mpn": hit.mpn,
        "manufacturer": hit.manufacturer,
        "description": hit.description,
        "package": hit.package,
        "stock": hit.stock,
        "datasheet_url": hit.datasheet_url,
    }


def _offer_payload(offer: Offer) -> dict[str, Any]:
    return {
        "supplier": offer.supplier,
        "supplier_pn": offer.supplier_pn,
        "mpn": offer.mpn,
        "manufacturer": offer.manufacturer,
        "description": offer.description,
        "packaging": str(offer.packaging),
        "stock": offer.stock,
        "moq": offer.moq,
        "currency": offer.currency,
        "price_breaks": [
            {"qty": item.qty, "unit_price": item.unit_price} for item in offer.sorted_breaks()
        ],
        "url": offer.url,
        "datasheet_url": offer.datasheet_url,
        "fetched_at": offer.fetched_at,
    }


def _resolve_category(
    adapter: SupplierAdapter, wanted: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """Turn a category *name* into the supplier's category, or report the choice.

    The agent never states an identifier, so this is where one is chosen — and
    it refuses to choose between two. Picking the first match would search a
    category the requirement never mentioned and return a confident list of
    parts from it.
    """
    categories = getattr(adapter, "categories", None)
    if categories is None:  # pragma: no cover - only TME has a tree today
        return None, []
    tree: Sequence[Mapping[str, Any]] = categories()

    needle = wanted.strip().casefold()
    exact = [c for c in tree if str(c.get("path", "")).casefold() == needle]
    if len(exact) == 1:
        return dict(exact[0]), []

    partial = [
        c
        for c in tree
        if needle in str(c.get("path", "")).casefold()
        or needle == str(c.get("name", "")).casefold()
    ]
    if len(partial) == 1:
        return dict(partial[0]), []

    # Deepest-first: the leaf categories are the searchable ones, and a caller
    # staring at eight choices wants the specific ones at the top.
    ranked = sorted(partial or tree, key=lambda c: -int(c.get("products_count") or 0))
    return None, [str(c.get("path", "")) for c in ranked[:10]]
