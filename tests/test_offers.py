"""Storing offers, refreshing them, and the P lint group.

The refresh tests use a fake adapter rather than a fake HTTP transport: what is
under test here is the *policy* — refresh does not re-adjudicate identity,
discovery does not auto-link a doubtful match, one dead supplier is reported
once — and that policy is the same whatever the wire format underneath it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from klm.config import Config, FieldConfig, default_aliases
from klm.model import (
    Confidence,
    Lifecycle,
    Offer,
    Packaging,
    Part,
    PartStatus,
    PriceBreak,
)
from klm.services.catalog import save_part
from klm.services.lint import Selector, lint_catalog
from klm.services.offers import (
    TIMESTAMP_FORMAT,
    OfferError,
    count_offers,
    delete_offer,
    list_offers,
    refresh_offers,
    save_offer,
    stale_part_ids,
)
from klm.store.assets import AssetStore
from klm.store.db import connect, migrate
from klm.store.paths import Paths
from klm.suppliers.base import SearchHit, SupplierUnavailable


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Paths, sqlite3.Connection, AssetStore]]:
    paths = Paths(tmp_path / "home")
    paths.create()
    conn = connect(paths.db)
    migrate(conn)
    yield paths, conn, AssetStore(paths.assets)
    conn.close()


def config(**overrides: object) -> Config:
    return Config(fields=FieldConfig(aliases=default_aliases()), **overrides)  # type: ignore[arg-type]


def stamp(days_ago: int = 0) -> str:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def a_part(
    conn: sqlite3.Connection,
    klm_id: str = "01JB4K7QW8ZR3XN5M2VYT9DCFA",
    mpn: str = "STM32F103C8T6",
    manufacturer: str = "STMicroelectronics",
    **overrides: object,
) -> Part:
    settings: dict[str, object] = {"status": PartStatus.APPROVED, **overrides}
    part = Part(
        klm_id=klm_id,
        mpn=mpn,
        manufacturer=manufacturer,
        description="ARM Cortex-M3",
        **settings,  # type: ignore[arg-type]
    )
    return save_part(conn, part)


def an_offer(klm_id: str, **overrides: object) -> Offer:
    defaults: dict[str, object] = {
        "supplier": "tme",
        "supplier_pn": "STM32F103C8T6",
        "klm_id": klm_id,
        "mpn": "STM32F103C8T6",
        "manufacturer": "STMicroelectronics",
        "stock": 38,
        "currency": "PLN",
        "price_breaks": [PriceBreak(1, 18.5), PriceBreak(10, 16.2)],
    }
    return Offer(**{**defaults, **overrides})  # type: ignore[arg-type]


class FakeAdapter:
    """An adapter that answers from a script, and counts what it was asked."""

    def __init__(
        self,
        name: str = "tme",
        *,
        catalog: dict[str, Offer] | None = None,
        candidates: list[Offer] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.name = name
        self.currency = "PLN"
        self.catalog = catalog or {}
        self.candidates = candidates or []
        self.fail = fail
        self.batch_calls = 0
        self.resolve_calls = 0

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        return []

    def search_parametric(self, category: str, filters: dict[str, str]) -> list[SearchHit]:
        return []

    def get_offer(self, supplier_pn: str) -> Offer | None:
        return self.get_offers([supplier_pn]).get(supplier_pn)

    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]:
        if self.fail:
            raise self.fail
        self.batch_calls += 1
        return {pn: self.catalog[pn] for pn in supplier_pns if pn in self.catalog}

    def resolve_mpn(self, mpn: str, manufacturer: str | None = None) -> list[Offer]:
        if self.fail:
            raise self.fail
        self.resolve_calls += 1
        return list(self.candidates)

    def datasheet_url(self, supplier_pn: str) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_an_offer_round_trips_through_the_database(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)

    save_offer(conn, an_offer(part.klm_id, packaging=Packaging.REEL, moq=1, lead_time_days=3))
    (stored,) = list_offers(conn, klm_id=part.klm_id)

    assert stored.supplier_pn == "STM32F103C8T6"
    assert stored.packaging is Packaging.REEL
    assert stored.lead_time_days == 3
    assert [(b.qty, b.unit_price) for b in stored.sorted_breaks()] == [(1, 18.5), (10, 16.2)]


def test_an_unlinked_offer_is_refused_rather_than_stored_orphaned(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env

    with pytest.raises(OfferError, match="not linked to a part"):
        save_offer(conn, Offer(supplier="tme", supplier_pn="X"))


def test_saving_the_same_supplier_part_number_updates_rather_than_duplicates(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)

    save_offer(conn, an_offer(part.klm_id, stock=38))
    save_offer(conn, an_offer(part.klm_id, stock=0))

    offers = list_offers(conn, klm_id=part.klm_id)
    assert len(offers) == 1
    assert offers[0].stock == 0


def test_an_offer_is_stamped_when_it_is_saved(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)

    saved = save_offer(conn, an_offer(part.klm_id))

    assert saved.fetched_at is not None


def test_offers_can_be_filtered_by_supplier_and_counted(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id))
    save_offer(conn, an_offer(part.klm_id, supplier="lcsc", supplier_pn="C8734", currency="USD"))

    assert len(list_offers(conn, supplier="lcsc")) == 1
    assert count_offers(conn) == {"lcsc": 1, "tme": 1}


def test_deleting_an_offer_reports_whether_there_was_one(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id))

    assert delete_offer(conn, "tme", "STM32F103C8T6")
    assert not delete_offer(conn, "tme", "STM32F103C8T6")


def test_deleting_a_part_takes_its_offers_with_it(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id))

    conn.execute("DELETE FROM part WHERE klm_id = ?", (part.klm_id,))

    assert list_offers(conn) == []


def test_a_part_with_no_offer_counts_as_stale(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)

    assert stale_part_ids(conn, 30) == [part.klm_id]


def test_a_freshly_refreshed_part_is_not_stale(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, fetched_at=stamp(7)))

    assert stale_part_ids(conn, 30) == []
    assert stale_part_ids(conn, 1) == [part.klm_id]


def test_staleness_compares_timestamps_in_klms_own_format(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """Regression: SQLite's `datetime()` writes a space where klm writes 'T'.

    Compared as strings, 'T' sorts after ' ', so an offer refreshed earlier
    *today* compared against `datetime('now', '-0 days')` read as newer than
    the bound and never went stale. Only same-day comparisons were affected,
    which is exactly the case a `--stale 1h` refresh depends on.
    """
    _, conn, _ = env
    part = a_part(conn)
    stored = save_offer(conn, an_offer(part.klm_id))

    bound = conn.execute(
        "SELECT strftime(?, 'now', '+1 day')", (TIMESTAMP_FORMAT,)
    ).fetchone()[0]

    assert stored.fetched_at is not None
    assert stored.fetched_at < bound
    assert "T" in bound and bound.endswith("Z")


# ---------------------------------------------------------------------------
# Price and order arithmetic
# ---------------------------------------------------------------------------


def test_the_price_is_the_last_break_the_quantity_reaches() -> None:
    offer = an_offer("x")

    assert offer.unit_price(1) == pytest.approx(18.5)
    assert offer.unit_price(9) == pytest.approx(18.5)
    assert offer.unit_price(10) == pytest.approx(16.2)
    assert offer.unit_price(1000) == pytest.approx(16.2)


def test_below_the_smallest_break_there_is_no_price_to_quote() -> None:
    assert an_offer("x").unit_price(0) is None


def test_unknown_stock_does_not_read_as_zero() -> None:
    assert not Offer(supplier="tme", supplier_pn="X", stock=None).in_stock
    assert not Offer(supplier="tme", supplier_pn="X", stock=0).in_stock
    assert Offer(supplier="tme", supplier_pn="X", stock=1).in_stock


def test_order_quantity_respects_the_moq_and_the_multiple() -> None:
    offer = Offer(supplier="tme", supplier_pn="X", moq=10, multiple=25)

    assert offer.order_qty(3) == 25
    assert offer.order_qty(26) == 50
    assert offer.order_qty(50) == 50


def test_a_negative_price_break_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        PriceBreak(1, -0.5)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def test_refresh_updates_a_linked_offer_in_place(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, stock=38))
    adapter = FakeAdapter(catalog={"STM32F103C8T6": an_offer(part.klm_id, stock=4)})

    report = refresh_offers(conn, {"tme": adapter}, [part])

    assert len(report.refreshed) == 1
    assert list_offers(conn, klm_id=part.klm_id)[0].stock == 4


def test_refresh_does_not_re_adjudicate_a_link_a_human_made(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """A refresh re-reads price and stock. Identity was decided once."""
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, match_confidence=Confidence.MEDIUM))
    adapter = FakeAdapter(
        catalog={"STM32F103C8T6": an_offer(part.klm_id, match_confidence=Confidence.HIGH)}
    )

    refresh_offers(conn, {"tme": adapter}, [part])

    assert list_offers(conn)[0].match_confidence is Confidence.MEDIUM


def test_an_offer_the_supplier_stopped_listing_is_kept_not_deleted(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """Deleting it would look identical to never having had one."""
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, fetched_at=stamp(90)))

    refresh_offers(conn, {"tme": FakeAdapter(catalog={})}, [part])

    assert len(list_offers(conn, klm_id=part.klm_id)) == 1


def test_discovery_links_a_confident_match(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    candidate = an_offer(None, supplier_pn="STM32F103C8T6")  # type: ignore[arg-type]
    adapter = FakeAdapter(candidates=[candidate])

    report = refresh_offers(conn, {"tme": adapter}, [part])

    assert len(report.discovered) == 1
    assert list_offers(conn, klm_id=part.klm_id)[0].match_confidence is Confidence.HIGH


def test_a_doubtful_candidate_is_proposed_and_never_stored(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    candidate = an_offer(None, manufacturer="Some Other Co")  # type: ignore[arg-type]
    adapter = FakeAdapter(candidates=[candidate])

    report = refresh_offers(conn, {"tme": adapter}, [part])

    assert len(report.proposed) == 1
    assert list_offers(conn) == []


def test_a_candidate_for_a_different_part_is_not_even_proposed(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    adapter = FakeAdapter(candidates=[an_offer(None, mpn="LM317T")])  # type: ignore[arg-type]

    report = refresh_offers(conn, {"tme": adapter}, [part])

    assert report.proposed == []
    assert report.discovered == []


def test_discovery_is_skipped_once_a_supplier_already_has_a_link(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id))
    adapter = FakeAdapter(catalog={"STM32F103C8T6": an_offer(part.klm_id)})

    refresh_offers(conn, {"tme": adapter}, [part])

    assert adapter.resolve_calls == 0


def test_no_discover_refreshes_without_looking_for_anything_new(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    adapter = FakeAdapter(candidates=[an_offer(None)])  # type: ignore[arg-type]

    report = refresh_offers(conn, {"tme": adapter}, [part], discover=False)

    assert adapter.resolve_calls == 0
    assert report.discovered == []


def test_a_dead_supplier_is_reported_once_not_once_per_part(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    parts = [
        a_part(conn, klm_id=f"01JB4K7QW8ZR3XN5M2VYT9DCF{n}", mpn=f"PART-{n}") for n in "ABC"
    ]
    adapter = FakeAdapter(fail=SupplierUnavailable("tme is down"))

    report = refresh_offers(conn, {"tme": adapter}, parts)

    assert report.unavailable == [("tme", "tme is down")]
    assert not report.ok
    assert report.parts_checked == 3


def test_one_dead_supplier_does_not_stop_the_other(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, _ = env
    part = a_part(conn)
    dead = FakeAdapter("tme", fail=SupplierUnavailable("down"))
    alive = FakeAdapter("lcsc", candidates=[an_offer(None, supplier="lcsc", supplier_pn="C8734")])  # type: ignore[arg-type]

    report = refresh_offers(conn, {"tme": dead, "lcsc": alive}, [part])

    assert len(report.discovered) == 1
    assert len(report.unavailable) == 1


# ---------------------------------------------------------------------------
# Lint — the P group
# ---------------------------------------------------------------------------


def rules(report: object) -> list[str]:
    return [f.rule for f in report.findings]  # type: ignore[attr-defined]


def lint_p(
    conn: sqlite3.Connection, store: AssetStore, cfg: Config | None = None
) -> list[str]:
    report = lint_catalog(conn, store, cfg or config(), selector=Selector(select=("P",)))
    return rules(report)


def test_an_approved_part_with_no_offer_is_flagged(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    a_part(conn)

    assert "P001" in lint_p(conn, store)


def test_a_draft_part_is_not_nagged_about_sourcing(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """A draft is a part someone is still working on."""
    _, conn, store = env
    a_part(conn, status=PartStatus.DRAFT)

    assert lint_p(conn, store) == []


def test_offers_that_are_all_out_of_stock_are_flagged(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, stock=0))

    found = lint_p(conn, store)
    assert "P002" in found
    assert "P001" not in found


def test_unknown_stock_is_not_reported_as_zero_stock(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, stock=None))

    assert "P002" not in lint_p(conn, store)


def test_stale_offers_are_flagged_against_the_configured_threshold(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, fetched_at=stamp(45)))

    assert "P003" in lint_p(conn, store)
    assert "P003" not in lint_p(conn, store, config(stale_days=90))


def test_the_stale_finding_names_the_command_that_fixes_it(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """P003 is not `--fix`able: lint must never make network calls."""
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, fetched_at=stamp(45)))

    report = lint_catalog(conn, store, config(), selector=Selector(select=("P003",)))

    assert "klm refresh" in report.findings[0].message
    assert not report.findings[0].fixable


def test_an_smd_part_with_no_lcsc_number_is_flagged_for_assembly(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn, package="LQFP-48")
    save_offer(conn, an_offer(part.klm_id))

    assert "P004" in lint_p(conn, store)


def test_an_lcsc_offer_satisfies_the_assembly_rule(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn, package="LQFP-48")
    save_offer(conn, an_offer(part.klm_id, supplier="lcsc", supplier_pn="C8734"))

    assert "P004" not in lint_p(conn, store)


def test_a_through_hole_part_is_not_asked_for_an_lcsc_number(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn, package="TO-220")
    save_offer(conn, an_offer(part.klm_id))

    assert "P004" not in lint_p(conn, store)


def test_an_obsolete_lifecycle_is_flagged(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn, lifecycle=Lifecycle.OBSOLETE)
    save_offer(conn, an_offer(part.klm_id))

    assert "P005" in lint_p(conn, store)


def test_a_missing_datasheet_is_flagged(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id))

    found = lint_p(conn, store)
    assert "P006" in found


def test_a_datasheet_url_silences_p006(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn, datasheet_url="https://example.invalid/ds.pdf")
    save_offer(conn, an_offer(part.klm_id))

    assert "P006" not in lint_p(conn, store)


def test_a_low_confidence_link_is_surfaced_for_review(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    part = a_part(conn)
    save_offer(conn, an_offer(part.klm_id, match_confidence=Confidence.LOW))

    assert "P007" in lint_p(conn, store)


def test_the_p_group_can_be_ignored_wholesale(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    a_part(conn)

    report = lint_catalog(conn, store, config(), selector=Selector(ignore=("P",)))

    assert not [f for f in report.findings if f.rule.startswith("P")]
