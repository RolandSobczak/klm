"""Tests for datasheet fetch, cache and cited extraction.

The guardrail is one sentence of docs/11 §5 — every parameter comes back with
a page and a quote, and a parameter without provenance is rejected. What is
protected here is that the rejection is *mechanical*: a value the model stated
in an uncited block never reaches `parameters`, no matter how plausible it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from klm.llm.client import Citation, LlmError, Reply, Segment, Usage
from klm.services.datasheets import (
    MAX_BYTES,
    DatasheetError,
    extract,
    fetch,
    load,
)

PDF = b"%PDF-1.7\nfake datasheet\n%%EOF"
OTHER = b"%PDF-1.4\nanother\n%%EOF"


class FakeReader:
    """A model that has already read the PDF, so to speak."""

    model = "fake-reader"

    def __init__(self, *segments: Segment, error: Exception | None = None) -> None:
        self.segments = segments
        self.error = error
        self.messages: list[Any] = []

    def reply(self, *, system: str, messages: Any, tools: Any, on_text: Any = None) -> Reply:
        self.messages = list(messages)
        if self.error is not None:
            raise self.error
        return Reply(
            text="\n".join(s.text for s in self.segments),
            segments=self.segments,
            usage=Usage(input_tokens=5000, output_tokens=100),
        )


def cited(text: str, quote: str, page: int = 1) -> Segment:
    return Segment(text, (Citation(quote=quote, start_page=page, end_page=page),))


def serve(payload: bytes = PDF) -> Any:
    calls: list[str] = []

    def transport(url: str, timeout: float) -> bytes:
        calls.append(url)
        return payload

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


# ---------------------------------------------------------------------------
# Fetch and cache
# ---------------------------------------------------------------------------


def test_a_datasheet_is_fetched_once_and_then_cached(tmp_path: Path) -> None:
    transport = serve()

    first = fetch("https://example.test/a.pdf", tmp_path, transport=transport)
    second = fetch("https://example.test/a.pdf", tmp_path, transport=transport)

    assert transport.calls == ["https://example.test/a.pdf"], "the second read the cache"
    assert first.sha == second.sha
    assert first.sha.startswith("sha256:")
    assert first.size == len(PDF)


def test_two_urls_for_the_same_pdf_are_one_datasheet(tmp_path: Path) -> None:
    """Content-addressed, so "have we read this one?" has an answer."""
    a = fetch("https://a.test/x.pdf", tmp_path, transport=serve())
    b = fetch("https://b.test/y.pdf", tmp_path, transport=serve())
    assert a.sha == b.sha
    assert a.path != b.path


def test_an_html_login_page_is_not_a_datasheet(tmp_path: Path) -> None:
    """Suppliers gate PDFs behind redirects; that must not reach the model."""
    with pytest.raises(DatasheetError, match="not a PDF"):
        fetch("https://example.test/a.pdf", tmp_path, transport=serve(b"<!doctype html><html>"))
    assert list(tmp_path.glob("*.pdf")) == [], "nothing was cached"


def test_a_file_too_large_to_send_is_refused_with_its_size(tmp_path: Path) -> None:
    payload = PDF + b"x" * MAX_BYTES
    with pytest.raises(DatasheetError, match="larger than klm can send"):
        fetch("https://example.test/big.pdf", tmp_path, transport=serve(payload))


def test_a_non_http_url_is_refused_before_any_request(tmp_path: Path) -> None:
    with pytest.raises(DatasheetError, match="not a fetchable URL"):
        fetch("file:///etc/passwd", tmp_path, transport=serve())


def test_refresh_goes_back_to_the_network(tmp_path: Path) -> None:
    transport = serve()
    fetch("https://example.test/a.pdf", tmp_path, transport=transport)
    fetch("https://example.test/a.pdf", tmp_path, transport=transport, refresh=True)
    assert len(transport.calls) == 2


def test_a_handle_resolves_back_to_the_cached_file(tmp_path: Path) -> None:
    datasheet = fetch("https://example.test/a.pdf", tmp_path, transport=serve())
    assert load(datasheet.sha, tmp_path) is not None
    assert load("sha256:" + "0" * 64, tmp_path) is None


def test_the_document_block_carries_the_pdf_with_citations_on(tmp_path: Path) -> None:
    datasheet = fetch("https://example.test/a.pdf", tmp_path, transport=serve())
    block = datasheet.document_block(title="TPS62840")
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    assert block["citations"] == {"enabled": True}
    assert block["title"] == "TPS62840"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@pytest.fixture
def datasheet(tmp_path: Path):  # type: ignore[no-untyped-def]
    return fetch("https://example.test/a.pdf", tmp_path, transport=serve())


def test_a_cited_parameter_comes_back_with_its_page_and_quote(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Vin max: 6.5 V", "VIN 1.8 to 6.5 V", page=3))

    result = extract(datasheet, ["Vin max"], reader)

    (parameter,) = result.parameters
    assert parameter.value == "6.5 V"
    assert parameter.page == 3
    assert parameter.citations[0].quote == "VIN 1.8 to 6.5 V"


def test_an_uncited_value_is_dropped_and_reported(datasheet) -> None:  # type: ignore[no-untyped-def]
    """The whole point: plausible is not the same as stated in the datasheet."""
    reader = FakeReader(
        cited("Vin max: 6.5 V", "VIN 1.8 to 6.5 V"),
        Segment("Iq: 60 nA"),  # no citation
    )

    result = extract(datasheet, ["Vin max", "Iq"], reader)

    assert [p.name for p in result.parameters] == ["Vin max"]
    assert result.uncited == [("Iq", "60 nA")]


def test_a_parameter_the_datasheet_does_not_state_is_reported_as_missing(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Vin max: 6.5 V", "VIN 1.8 to 6.5 V"), Segment("Iq: not stated"))

    result = extract(datasheet, ["Vin max", "Iq"], reader)

    assert result.missing == ["Iq"]
    assert not result.uncited, "'not stated' is an answer, not a dropped claim"


def test_the_names_asked_for_are_the_names_returned(datasheet) -> None:  # type: ignore[no-untyped-def]
    """The model may spell it back differently; the caller's key must survive."""
    reader = FakeReader(cited("**VIN max**: 6.5 V", "VIN 1.8 to 6.5 V"))
    (parameter,) = extract(datasheet, ["Vin max"], reader).parameters
    assert parameter.name == "Vin max"


def test_a_parameter_nobody_asked_about_is_ignored(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Note: this device is a buck converter", "a buck converter"))
    result = extract(datasheet, ["Vin max"], reader)
    assert result.parameters == [] and result.missing == ["Vin max"]


def test_order_follows_the_request_not_the_answer(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Iq: 60 nA", "IQ 60 nA"), cited("Vin max: 6.5 V", "VIN 6.5 V"))
    result = extract(datasheet, ["Vin max", "Iq"], reader)
    assert [p.name for p in result.parameters] == ["Vin max", "Iq"]


def test_asking_for_nothing_calls_no_model(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader()
    result = extract(datasheet, [], reader)
    assert result.parameters == [] and reader.messages == []


def test_the_pdf_and_the_question_are_what_the_reader_is_sent(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Vin max: 6.5 V", "VIN 6.5 V"))
    extract(datasheet, ["Vin max"], reader, title="TPS62840")

    (message,) = reader.messages
    document, question = message["content"]
    assert document["type"] == "document" and document["title"] == "TPS62840"
    assert "Vin max" in question["text"]


def test_a_model_failure_leaves_everything_unanswered(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(error=LlmError("Overloaded"))

    result = extract(datasheet, ["Vin max", "Iq"], reader)

    assert result.parameters == []
    assert result.missing == ["Vin max", "Iq"]
    assert "Overloaded" in result.note


def test_extraction_reports_what_it_cost(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(cited("Vin max: 6.5 V", "VIN 6.5 V"))
    assert extract(datasheet, ["Vin max"], reader).usage.total == 5100


def test_explain_says_what_happened_to_every_parameter(datasheet) -> None:  # type: ignore[no-untyped-def]
    reader = FakeReader(
        cited("Vin max: 6.5 V", "VIN 1.8 to 6.5 V", page=3),
        Segment("Iq: 60 nA"),
        Segment("Tj max: not stated"),
    )

    lines = "\n".join(extract(datasheet, ["Vin max", "Iq", "Tj max"], reader).explain())

    assert 'p.3: "VIN 1.8 to 6.5 V"' in lines
    assert "Iq = 60 nA — no citation, dropped" in lines
    assert "Tj max: not found in the datasheet" in lines
