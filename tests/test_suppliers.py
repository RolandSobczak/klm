"""The supplier layer: HTTP behaviour, matching, and the two adapters.

Nothing here touches the network. The transport, clock and sleep are injected
precisely so that a rate limiter and a circuit breaker can be tested in
microseconds instead of minutes — an untestable one does not get tested, and
one that does not get tested is one that opens in production for the first time.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from klm.config import Config, ConfigError, SupplierConfig, load_config
from klm.model import Confidence, Packaging
from klm.suppliers.base import SupplierNotConfigured, SupplierUnavailable
from klm.suppliers.http import (
    CachedHttp,
    CircuitBreaker,
    HttpResponse,
    TokenBucket,
)
from klm.suppliers.lcsc import LcscAdapter, is_lcsc_pn, product_url
from klm.suppliers.matching import (
    match_mpn,
    normalize_mpn,
    same_manufacturer,
    strip_packaging,
)
from klm.suppliers.registry import build_adapters
from klm.suppliers.tme import TmeAdapter, sign, signature_base


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """A transport that replays canned responses and remembers the calls."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.calls.append((method, url, body))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def http(tmp_path: Path, *responses: HttpResponse | Exception, **kwargs: object) -> CachedHttp:
    clock = FakeClock()
    transport = Recorder(*responses)
    client = CachedHttp(
        "test",
        tmp_path,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        bucket=TokenBucket(rate=1000.0, capacity=1000.0, clock=clock, sleep=clock.sleep),
        **kwargs,  # type: ignore[arg-type]
    )
    client.recorder = transport  # type: ignore[attr-defined]
    client.fake_clock = clock  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


def test_bucket_allows_a_burst_up_to_its_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=3.0, clock=clock, sleep=clock.sleep)

    assert [bucket.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]


def test_bucket_sleeps_once_the_burst_is_spent() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2.0, clock=clock, sleep=clock.sleep)
    for _ in range(2):
        bucket.acquire()

    waited = bucket.acquire()

    assert waited == pytest.approx(0.5)
    assert clock.now == pytest.approx(1000.5)


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=4.0, capacity=4.0, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        bucket.acquire()

    clock.now += 1.0

    assert bucket.acquire() == 0.0


def test_bucket_refuses_a_request_it_could_never_satisfy() -> None:
    bucket = TokenBucket(rate=1.0, capacity=2.0)
    with pytest.raises(ValueError, match="cannot acquire"):
        bucket.acquire(5.0)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_opens_after_the_threshold_and_names_the_supplier() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, reset_after=60.0, clock=clock)

    breaker.record_failure()
    breaker.check("tme")  # one failure is not an outage
    breaker.record_failure()

    with pytest.raises(SupplierUnavailable, match="tme is in degraded mode"):
        breaker.check("tme")


def test_breaker_lets_one_request_through_after_the_reset_window() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, reset_after=60.0, clock=clock)
    breaker.record_failure()

    clock.now += 61.0

    breaker.check("tme")  # does not raise
    breaker.record_success()
    assert not breaker.is_open


def test_a_success_clears_the_failure_count() -> None:
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert not breaker.is_open


# ---------------------------------------------------------------------------
# Cached HTTP
# ---------------------------------------------------------------------------


def test_a_cached_response_within_the_ttl_makes_no_request(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, '{"ok":true}'))

    client.request("GET", "https://example.invalid/a", ttl=60)
    client.request("GET", "https://example.invalid/a", ttl=60)

    assert len(client.recorder.calls) == 1  # type: ignore[attr-defined]


def test_a_cached_response_past_its_ttl_is_refetched(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, "first"), HttpResponse(200, "second"))

    client.request("GET", "https://example.invalid/a", ttl=60)
    client.fake_clock.now += 120  # type: ignore[attr-defined]
    second = client.request("GET", "https://example.invalid/a", ttl=60)

    assert second.body == "second"


def test_a_null_ttl_never_reads_the_cache_but_still_writes_it(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, "live"))

    client.request("GET", "https://example.invalid/a", ttl=None)
    client.request("GET", "https://example.invalid/a", ttl=None)

    assert len(client.recorder.calls) == 2  # type: ignore[attr-defined]
    assert client.read_cache(client.cache_key("GET", "https://example.invalid/a", None))


def test_different_bodies_are_different_cache_entries(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, "x"))
    url = "https://example.invalid/a"

    assert client.cache_key("POST", url, b"one") != client.cache_key("POST", url, b"two")


def test_offline_serves_the_cache_and_never_calls_the_transport(tmp_path: Path) -> None:
    warm = http(tmp_path, HttpResponse(200, "cached"))
    warm.request("GET", "https://example.invalid/a", ttl=60)

    cold = http(tmp_path, HttpResponse(200, "live"), offline=True)
    response = cold.request("GET", "https://example.invalid/a", ttl=None)

    assert response.body == "cached"
    assert cold.recorder.calls == []  # type: ignore[attr-defined]


def test_offline_with_nothing_cached_is_an_honest_failure(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, "live"), offline=True)

    with pytest.raises(SupplierUnavailable, match="offline and not in cache"):
        client.request("GET", "https://example.invalid/missing", ttl=None)


def test_a_retryable_status_is_retried(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(503, ""), HttpResponse(200, "recovered"))

    response = client.request("GET", "https://example.invalid/a", ttl=None)

    assert response.body == "recovered"
    assert len(client.recorder.calls) == 2  # type: ignore[attr-defined]


def test_a_404_is_an_answer_not_a_failure(tmp_path: Path) -> None:
    """A malformed query must not count against the breaker."""
    client = http(tmp_path, HttpResponse(404, "not found"))

    response = client.request("GET", "https://example.invalid/a", ttl=None)

    assert response.status == 404
    assert len(client.recorder.calls) == 1  # type: ignore[attr-defined]
    assert not client.breaker.is_open


def test_a_stale_cache_entry_beats_an_outage(tmp_path: Path) -> None:
    warm = http(tmp_path, HttpResponse(200, "old but real"))
    warm.request("GET", "https://example.invalid/a", ttl=60)

    broken = http(tmp_path, SupplierUnavailable("connection refused"))
    response = broken.request("GET", "https://example.invalid/a", ttl=None)

    assert response.body == "old but real"


def test_an_outage_with_no_cache_propagates(tmp_path: Path) -> None:
    client = http(tmp_path, SupplierUnavailable("connection refused"))

    with pytest.raises(SupplierUnavailable):
        client.request("GET", "https://example.invalid/cold", ttl=None)


def test_a_corrupt_cache_entry_is_treated_as_a_miss(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, "fresh"))
    key = client.cache_key("GET", "https://example.invalid/a", None)
    path = client.cache_dir / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert client.request("GET", "https://example.invalid/a", ttl=60).body == "fresh"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_case_and_spaces_fold_but_hyphens_do_not() -> None:
    assert normalize_mpn(" stm32f103 c8t6 ") == "STM32F103C8T6"
    assert normalize_mpn("LM317-T") != normalize_mpn("LM317T")


@pytest.mark.parametrize(
    ("order_code", "base", "packaging"),
    [
        ("LM317-TR", "LM317", Packaging.REEL),
        ("LM317/TR", "LM317", Packaging.REEL),
        ("LM317-T&R", "LM317", Packaging.REEL),
        ("LM317-REEL", "LM317", Packaging.REEL),
        ("LM317-CT", "LM317", Packaging.CUT_TAPE),
        ("LM317", "LM317", Packaging.UNKNOWN),
    ],
)
def test_packaging_suffixes_are_split_off(
    order_code: str, base: str, packaging: Packaging
) -> None:
    assert strip_packaging(order_code) == (base, packaging)


def test_a_suffix_that_is_the_whole_string_is_not_stripped() -> None:
    """`-TR` alone is not a part number with an empty base."""
    assert strip_packaging("-TR") == ("-TR", Packaging.UNKNOWN)


def test_manufacturer_aliases_resolve_across_spellings() -> None:
    assert same_manufacturer("ST", "STMicroelectronics")
    assert same_manufacturer("TI", "Texas Instruments")
    assert same_manufacturer("Maxim Integrated", "Analog Devices")
    assert not same_manufacturer("Yageo", "Vishay")


def test_an_exact_match_with_an_agreeing_manufacturer_is_high_confidence() -> None:
    result = match_mpn("STM32F103C8T6", "STMicroelectronics", "STM32F103C8T6", "STMicroelectronics")

    assert result is not None
    assert result.confidence is Confidence.HIGH
    assert result.auto_link


def test_a_manufacturer_alias_weakens_an_otherwise_exact_match() -> None:
    result = match_mpn("STM32F103C8T6", "STMicroelectronics", "STM32F103C8T6", "ST")

    assert result is not None
    assert result.confidence is Confidence.MEDIUM
    assert "alias" in result.reason
    assert result.auto_link


def test_a_packaging_suffix_match_is_medium_and_remembers_the_packaging() -> None:
    result = match_mpn("LM317T", "Texas Instruments", "LM317T-TR", "Texas Instruments")

    assert result is not None
    assert result.confidence is Confidence.MEDIUM
    assert result.packaging is Packaging.REEL


def test_an_unstated_manufacturer_weakens_the_match() -> None:
    result = match_mpn("LM317T", "Texas Instruments", "LM317T", "")

    assert result is not None
    assert result.confidence is Confidence.MEDIUM
    assert "not stated" in result.reason


def test_the_same_mpn_from_a_different_company_never_auto_links() -> None:
    result = match_mpn("LM317T", "Texas Instruments", "LM317T", "Onsemi")

    assert result is not None
    assert result.confidence is Confidence.LOW
    assert not result.auto_link


def test_a_different_part_number_is_not_a_match_at_any_confidence() -> None:
    assert match_mpn("LM317T", "Texas Instruments", "LM337T", "Texas Instruments") is None


def test_an_empty_mpn_matches_nothing() -> None:
    assert match_mpn("", "Yageo", "RC0402", "Yageo") is None
    assert match_mpn("RC0402", "Yageo", "", "Yageo") is None


# ---------------------------------------------------------------------------
# TME signing
# ---------------------------------------------------------------------------


def test_the_signature_base_is_method_url_and_sorted_params() -> None:
    base = signature_base(
        "https://api.tme.eu/Products/GetProducts.json", {"Token": "abc", "Country": "PL"}
    )

    assert base.startswith("POST&https%3A%2F%2Fapi.tme.eu%2FProducts%2FGetProducts.json&")
    # Country sorts before Token, whatever order the dict was built in.
    assert base.endswith("Country%3DPL%26Token%3Dabc")


def test_parameter_order_does_not_change_the_signature() -> None:
    url = "https://api.tme.eu/Products/GetProducts.json"
    left = sign(url, {"A": "1", "B": "2"}, "secret")
    right = sign(url, {"B": "2", "A": "1"}, "secret")

    assert left == right


def test_lists_are_flattened_into_indexed_parameters() -> None:
    base = signature_base(
        "https://api.tme.eu/x.json", {"SymbolList": ["AAA", "BBB"]}
    )

    assert "SymbolList%255B0%255D%3DAAA" in base
    assert "SymbolList%255B1%255D%3DBBB" in base


def test_the_signature_changes_with_the_secret() -> None:
    url = "https://api.tme.eu/x.json"
    assert sign(url, {"A": "1"}, "one") != sign(url, {"A": "1"}, "two")


def test_the_signature_is_base64() -> None:
    import base64

    signature = sign("https://api.tme.eu/x.json", {"A": "1"}, "secret")

    assert len(base64.b64decode(signature)) == 20  # SHA-1 digest length


# ---------------------------------------------------------------------------
# TME adapter
# ---------------------------------------------------------------------------


def tme_config(**overrides: object) -> SupplierConfig:
    defaults: dict[str, object] = {
        "name": "tme",
        "enabled": True,
        "api_key_ref": "literal-token",
        "api_secret_ref": "literal-secret",
        "currency": "PLN",
    }
    return SupplierConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


PRODUCT_RESPONSE = json.dumps(
    {
        "Status": "OK",
        "Data": {
            "ProductList": [
                {
                    "Symbol": "STM32F103C8T6",
                    "OriginalSymbol": "STM32F103C8T6",
                    "Producer": "STMICROELECTRONICS",
                    "Description": "ARM Cortex-M3, LQFP-48",
                    "InStock": 38,
                    "ProductInformationPage": "//www.tme.eu/pl/details/stm32f103c8t6/",
                }
            ]
        },
    }
)

PRICE_RESPONSE = json.dumps(
    {
        "Status": "OK",
        "Data": {
            "ProductList": [
                {
                    "Symbol": "STM32F103C8T6",
                    "Amount": 38,
                    "Unit": 1,
                    "PriceCurrency": "PLN",
                    "PriceList": [
                        {"Amount": 1, "PriceValue": 18.5},
                        {"Amount": 10, "PriceValue": 16.2},
                    ],
                }
            ]
        },
    }
)


def test_tme_without_credentials_says_so_rather_than_failing_obscurely(tmp_path: Path) -> None:
    adapter = TmeAdapter(
        tme_config(api_key_ref=None, api_secret_ref=None), http(tmp_path, HttpResponse(200, "{}"))
    )

    with pytest.raises(SupplierNotConfigured, match="TME_API_KEY"):
        adapter.search("resistor")


def test_tme_builds_a_normalized_offer(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, PRODUCT_RESPONSE), HttpResponse(200, PRICE_RESPONSE))
    adapter = TmeAdapter(tme_config(), client)

    offer = adapter.get_offer("STM32F103C8T6")

    assert offer is not None
    assert offer.supplier == "tme"
    assert offer.manufacturer == "STMICROELECTRONICS"
    assert offer.stock == 38
    assert offer.currency == "PLN"
    assert offer.unit_price(1) == pytest.approx(18.5)
    assert offer.unit_price(10) == pytest.approx(16.2)


def test_tme_price_below_the_smallest_break_is_unquoted_not_extrapolated(
    tmp_path: Path,
) -> None:
    client = http(tmp_path, HttpResponse(200, PRODUCT_RESPONSE), HttpResponse(200, PRICE_RESPONSE))
    adapter = TmeAdapter(tme_config(), client)
    offer = adapter.get_offer("STM32F103C8T6")

    assert offer is not None
    assert offer.unit_price(0) is None


def test_tme_signs_every_request(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, PRODUCT_RESPONSE))
    TmeAdapter(tme_config(), client).search("stm32")

    _method, _url, body = client.recorder.calls[0]  # type: ignore[attr-defined]
    assert body is not None
    assert b"ApiSignature=" in body


def test_tme_protocol_urls_are_made_absolute(tmp_path: Path) -> None:
    client = http(tmp_path, HttpResponse(200, PRODUCT_RESPONSE))
    hits = TmeAdapter(tme_config(), client).search("stm32")

    assert hits[0].url == "https://www.tme.eu/pl/details/stm32f103c8t6/"


def test_a_tme_error_status_is_unavailability_not_a_silent_empty_result(
    tmp_path: Path,
) -> None:
    client = http(tmp_path, HttpResponse(200, json.dumps({"Status": "E_INVALID_SIGNATURE"})))
    adapter = TmeAdapter(tme_config(), client)

    with pytest.raises(SupplierUnavailable, match="signature base string"):
        adapter.search("stm32")


# ---------------------------------------------------------------------------
# LCSC adapter
# ---------------------------------------------------------------------------


def lcsc_config(**overrides: object) -> SupplierConfig:
    defaults: dict[str, object] = {
        "name": "lcsc",
        "enabled": True,
        "mode": "manual",
        "currency": "USD",
    }
    return SupplierConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["C25900", "c25900", " C1 "])
def test_lcsc_part_numbers_are_recognised(value: str) -> None:
    assert is_lcsc_pn(value)


@pytest.mark.parametrize("value", ["25900", "RC0402", "C", "CX100", ""])
def test_non_lcsc_part_numbers_are_rejected(value: str) -> None:
    assert not is_lcsc_pn(value)


def test_manual_mode_declines_quietly_rather_than_raising() -> None:
    """A refresh across 200 parts must not print 200 errors about LCSC."""
    adapter = LcscAdapter(lcsc_config())

    assert adapter.search("stm32") == []
    assert adapter.get_offer("C25900") is None
    assert adapter.get_offers(["C25900"]) == {}
    assert adapter.resolve_mpn("STM32F103C8T6") == []


def test_manual_entry_produces_a_high_confidence_offer() -> None:
    adapter = LcscAdapter(lcsc_config())

    offer = adapter.manual_offer(
        "c25900", mpn="RC0402FR-074K7L", manufacturer="Yageo", unit_price=0.0012, stock=100_000
    )

    assert offer.supplier_pn == "C25900"
    assert offer.currency == "USD"
    assert offer.match_confidence is Confidence.HIGH
    assert offer.unit_price(1) == pytest.approx(0.0012)
    assert offer.url == product_url("C25900")


def test_manual_entry_rejects_something_that_is_not_a_part_number() -> None:
    with pytest.raises(ValueError, match="not an LCSC part number"):
        LcscAdapter(lcsc_config()).manual_offer("RC0402FR-074K7L")


def test_lcsc_datasheet_link_is_the_product_page_not_a_guessed_pdf() -> None:
    adapter = LcscAdapter(lcsc_config())

    assert adapter.datasheet_url("C25900") == product_url("C25900")
    assert adapter.datasheet_url("nonsense") is None


# ---------------------------------------------------------------------------
# Configuration and registry
# ---------------------------------------------------------------------------


def test_the_defaults_enable_tme_by_api_and_lcsc_by_hand() -> None:
    config = load_config(None)

    assert config.suppliers["tme"].mode == "api"
    assert config.suppliers["lcsc"].manual


def test_a_supplier_block_merges_onto_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[suppliers.tme]\nshipping_flat = 20.0\n", encoding="utf-8")

    tme = load_config(path).suppliers["tme"]

    assert tme.shipping_flat == 20.0
    assert tme.vat_rate == 0.23  # not reset by touching one key


def test_a_secret_pasted_into_the_config_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[suppliers.tme]\napi_key = "sk-live-oops"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="env:NAME"):
        load_config(path)


def test_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TME_API_KEY", "token")
    monkeypatch.setenv("TME_API_SECRET", "secret")

    assert load_config(None).suppliers["tme"].credentials() == ("token", "secret")


def test_an_unset_variable_leaves_the_supplier_unconfigured_not_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TME_API_KEY", raising=False)
    monkeypatch.delenv("TME_API_SECRET", raising=False)

    assert not load_config(None).suppliers["tme"].has_credentials()


def test_an_unknown_mode_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[suppliers.lcsc]\nmode = "scrape"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="mode must be"):
        load_config(path)


def test_the_registry_builds_only_enabled_suppliers(tmp_path: Path) -> None:
    config = Config(
        suppliers={
            "tme": SupplierConfig(name="tme", enabled=True),
            "lcsc": SupplierConfig(name="lcsc", enabled=False),
        }
    )

    assert sorted(build_adapters(config, tmp_path)) == ["tme"]


def test_the_registry_ignores_a_supplier_with_no_implementation(tmp_path: Path) -> None:
    config = Config(suppliers={"mouser": SupplierConfig(name="mouser", enabled=True)})

    assert build_adapters(config, tmp_path) == {}
