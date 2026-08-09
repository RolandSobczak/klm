"""Offers: storing them, and refreshing them from suppliers.

An offer is derived data — a cache of a remote fact, stamped with when it was
fetched (docs/02 §1). Two consequences run through this module:

* **Nothing here is authoritative.** Losing every offer costs one refresh, so
  offers are not exported to the git mirror and a failed refresh degrades to
  the last known values rather than deleting them. A stale price is useful; a
  missing one is not.
* **Discovery and refresh are different operations.** Refreshing an offer klm
  already linked re-reads stock and price for a part number a human or a
  high-confidence match already blessed. Discovering one means deciding that a
  listing *is* this part, which is a judgement with a confidence attached.
  Doing discovery on every refresh would let a wrong match appear silently
  months after the part was approved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from klm.model import Confidence, Offer, Packaging, Part, PriceBreak
from klm.services.catalog import now
from klm.store.db import transaction
from klm.suppliers.base import SupplierAdapter, SupplierError
from klm.suppliers.matching import match_mpn

__all__ = [
    "RefreshReport",
    "count_offers",
    "delete_offer",
    "list_offers",
    "refresh_offers",
    "save_offer",
    "stale_part_ids",
]


class OfferError(Exception):
    """An offer cannot be stored as given."""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_offer(conn: sqlite3.Connection, offer: Offer) -> Offer:
    """Insert or replace an offer. ``(supplier, supplier_pn)`` is its identity."""
    if not offer.klm_id:
        raise OfferError(
            f"{offer.supplier}:{offer.supplier_pn} is not linked to a part; "
            "match it before saving"
        )
    stamped = offer.fetched_at or now()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO offer (supplier, supplier_pn, klm_id, mpn, manufacturer, description,
                               packaging, moq, multiple, stock, currency, price_breaks,
                               lead_time_days, url, datasheet_url, match_confidence, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier, supplier_pn) DO UPDATE SET
                klm_id = excluded.klm_id,
                mpn = excluded.mpn,
                manufacturer = excluded.manufacturer,
                description = excluded.description,
                packaging = excluded.packaging,
                moq = excluded.moq,
                multiple = excluded.multiple,
                stock = excluded.stock,
                currency = excluded.currency,
                price_breaks = excluded.price_breaks,
                lead_time_days = excluded.lead_time_days,
                url = excluded.url,
                datasheet_url = excluded.datasheet_url,
                match_confidence = excluded.match_confidence,
                fetched_at = excluded.fetched_at
            """,
            (
                offer.supplier,
                offer.supplier_pn,
                offer.klm_id,
                offer.mpn,
                offer.manufacturer,
                offer.description,
                str(offer.packaging),
                offer.moq,
                offer.multiple,
                offer.stock,
                offer.currency,
                _dump_breaks(offer.price_breaks),
                offer.lead_time_days,
                offer.url,
                offer.datasheet_url,
                str(offer.match_confidence),
                stamped,
            ),
        )
    offer.fetched_at = stamped
    return offer


def list_offers(
    conn: sqlite3.Connection,
    *,
    klm_id: str | None = None,
    supplier: str | None = None,
) -> list[Offer]:
    """Offers, ordered by supplier then part number so output is stable."""
    clauses: list[str] = []
    params: list[object] = []
    if klm_id is not None:
        clauses.append("klm_id = ?")
        params.append(klm_id)
    if supplier is not None:
        clauses.append("supplier = ?")
        params.append(supplier)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM offer{where} ORDER BY supplier, supplier_pn", params
    ).fetchall()
    return [_row_to_offer(row) for row in rows]


def delete_offer(conn: sqlite3.Connection, supplier: str, supplier_pn: str) -> bool:
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM offer WHERE supplier = ? AND supplier_pn = ?", (supplier, supplier_pn)
        )
    return cursor.rowcount > 0


def count_offers(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT supplier, COUNT(*) AS n FROM offer GROUP BY supplier").fetchall()
    return {row["supplier"]: int(row["n"]) for row in rows}


#: klm stamps timestamps as `2026-08-09T12:00:00Z`; SQLite's `datetime()` emits
#: `2026-08-09 12:00:00`. Comparing the two as strings is wrong wherever the
#: dates are equal, because 'T' sorts after ' '. Every age comparison in SQL
#: therefore builds its bound with `strftime` in klm's own format.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def stale_part_ids(conn: sqlite3.Connection, days: int) -> list[str]:
    """Parts whose newest offer is older than ``days``, plus those with none.

    A part with no offer at all is stale in the only sense that matters here:
    running a refresh should look at it.
    """
    rows = conn.execute(
        """
        SELECT p.klm_id
        FROM part p
        LEFT JOIN offer o ON o.klm_id = p.klm_id
        WHERE p.status != 'deprecated'
        GROUP BY p.klm_id
        HAVING MAX(o.fetched_at) IS NULL
            OR MAX(o.fetched_at) < strftime(?, 'now', ?)
        ORDER BY p.klm_id
        """,
        (TIMESTAMP_FORMAT, f"-{int(days)} days"),
    ).fetchall()
    return [str(row["klm_id"]) for row in rows]


def _dump_breaks(breaks: Sequence[PriceBreak]) -> str:
    ordered = sorted(breaks, key=lambda b: b.qty)
    return json.dumps([[b.qty, b.unit_price] for b in ordered], separators=(",", ":"))


def _load_breaks(raw: str | None) -> list[PriceBreak]:
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []
    breaks: list[PriceBreak] = []
    for row in rows:
        try:
            breaks.append(PriceBreak(int(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return breaks


def _row_to_offer(row: sqlite3.Row) -> Offer:
    return Offer(
        supplier=row["supplier"],
        supplier_pn=row["supplier_pn"],
        klm_id=row["klm_id"],
        mpn=row["mpn"],
        manufacturer=row["manufacturer"],
        description=row["description"] or "",
        packaging=Packaging(row["packaging"] or Packaging.UNKNOWN),
        moq=row["moq"],
        multiple=row["multiple"],
        stock=row["stock"],
        currency=row["currency"],
        price_breaks=_load_breaks(row["price_breaks"]),
        lead_time_days=row["lead_time_days"],
        url=row["url"],
        datasheet_url=row["datasheet_url"],
        match_confidence=Confidence(row["match_confidence"]),
        fetched_at=row["fetched_at"],
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


@dataclass
class RefreshReport:
    refreshed: list[Offer] = field(default_factory=list)
    """Offers klm already had, re-read from the supplier."""
    discovered: list[Offer] = field(default_factory=list)
    """New links klm made, each with its match confidence."""
    proposed: list[tuple[str, Offer, str]] = field(default_factory=list)
    """``(klm_id, offer, reason)`` — candidates too uncertain to link. Never saved."""
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    """``(supplier, reason)`` — a supplier that could not be reached at all."""
    parts_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.unavailable

    @property
    def saved(self) -> int:
        return len(self.refreshed) + len(self.discovered)


def refresh_offers(
    conn: sqlite3.Connection,
    adapters: dict[str, SupplierAdapter],
    parts: Iterable[Part],
    *,
    discover: bool = True,
) -> RefreshReport:
    """Re-read the offers klm knows, and optionally look for new ones.

    A supplier that is down or unconfigured is recorded once and skipped for
    the rest of the run: reporting the same outage per part turns one problem
    into two hundred lines of noise, and klm's offline behaviour is to keep
    the values it has, not to lose them.
    """
    report = RefreshReport()
    dead: set[str] = set()

    for part in parts:
        report.parts_checked += 1
        known = list_offers(conn, klm_id=part.klm_id)

        for name, adapter in sorted(adapters.items()):
            if name in dead:
                continue
            try:
                _refresh_supplier(conn, adapter, part, known, report, discover=discover)
            except SupplierError as exc:
                dead.add(name)
                report.unavailable.append((name, str(exc)))

    return report


def _refresh_supplier(
    conn: sqlite3.Connection,
    adapter: SupplierAdapter,
    part: Part,
    known: Sequence[Offer],
    report: RefreshReport,
    *,
    discover: bool,
) -> None:
    linked = [o for o in known if o.supplier == adapter.name]

    if linked:
        fresh = adapter.get_offers([o.supplier_pn for o in linked])
        for existing in linked:
            updated = fresh.get(existing.supplier_pn)
            if updated is None:
                # The supplier no longer lists it. Keeping the last known offer
                # and letting it age into P003 is more useful than deleting it,
                # which would look identical to "never had one".
                continue
            updated.klm_id = existing.klm_id
            # The link was decided once, by a match or a human. A refresh
            # re-reads price and stock; it does not re-adjudicate identity.
            updated.match_confidence = existing.match_confidence
            report.refreshed.append(save_offer(conn, updated))
        return

    if not discover:
        return

    for candidate in adapter.resolve_mpn(part.mpn, part.manufacturer):
        result = match_mpn(
            part.mpn, part.manufacturer, candidate.mpn or candidate.supplier_pn,
            candidate.manufacturer or "",
        )
        if result is None:
            continue
        candidate.klm_id = part.klm_id
        candidate.match_confidence = result.confidence
        if result.auto_link:
            report.discovered.append(save_offer(conn, candidate))
        else:
            # Reported, not stored. An offer in the database is one klm is
            # willing to order against, and this one has not earned that.
            report.proposed.append((part.klm_id, candidate, result.reason))
