"""Talking to Claude, and the one type the rest of klm sees.

`ModelClient` is the whole interface: a system prompt, a conversation, a tool
list, and one :class:`Reply` back. The research loop is written against that
protocol, so the loop's guardrails — iteration cap, token budget, spend ceiling
— are tested against a fake that costs nothing, and a change in the SDK's
surface lands in this file and nowhere else.

Three decisions worth knowing:

* **The API key is never handled here.** The SDK resolves `ANTHROPIC_API_KEY`
  itself. klm's rule that credentials live in the environment and never in
  `config.toml` is easiest to keep when klm does not touch the credential at
  all.
* **Requests stream.** A research turn reads a parameter table and thinks about
  it; that is long enough for a non-streaming request to hit an HTTP timeout,
  and streaming also lets a caller show progress instead of a spinner. The
  final message is assembled here either way, so the loop above is unaffected.
* **Assistant content is passed back verbatim.** :attr:`Reply.content` holds
  the SDK's own blocks, and the loop appends them unchanged. Reconstructing
  them — dropping thinking blocks, rebuilding tool_use blocks — is how a
  multi-turn conversation starts failing on its third turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "DEFAULT_MODEL",
    "EXTRACTION_MODEL",
    "AnthropicClient",
    "Citation",
    "LlmError",
    "LlmUnavailable",
    "ModelClient",
    "Reply",
    "Segment",
    "ToolCall",
    "Usage",
]

#: Research is multi-step work — search, read, cross-check, compare — where the
#: difference in quality is worth the cost. A part chosen wrongly costs three
#: weeks of shipping (docs/11 §4).
DEFAULT_MODEL = "claude-opus-5"

#: Adaptive thinking at high effort. Thinking is on by default on this model;
#: stating it is documentation rather than configuration.
DEFAULT_EFFORT = "high"

#: Pulling a parameter table out of a datasheet is narrow, mechanical, and
#: done a lot — the case the small model is for (docs/11 §4).
EXTRACTION_MODEL = "claude-haiku-4-5"

DEFAULT_MAX_TOKENS = 16000


class LlmError(Exception):
    """The model could not be reached, or answered with an error."""


class LlmUnavailable(LlmError):
    """No SDK, or no credentials. Reported, never a stack trace at a user."""


@dataclass(frozen=True)
class Usage:
    """What one turn cost, in tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked klm to run."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Citation:
    """Where a claim came from, according to the API.

    Produced by the API's own citation machinery when a document block is sent
    with `citations: {enabled: true}` — `quote` is lifted from the document,
    not written by the model. That distinction is the whole reason klm uses
    citations for datasheet extraction rather than asking for a quotation: a
    model asked to quote can paraphrase, and a paraphrase that looks like a
    quote is indistinguishable from provenance.
    """

    quote: str
    start_page: int | None = None
    end_page: int | None = None
    title: str | None = None

    @property
    def page(self) -> int | None:
        return self.start_page

    def __str__(self) -> str:
        where = f"p.{self.start_page}" if self.start_page else "no page"
        if self.end_page and self.end_page != self.start_page:
            where = f"pp.{self.start_page}-{self.end_page}"
        return f'{where}: "{self.quote.strip()}"'


@dataclass(frozen=True)
class Segment:
    """One text block, with whatever it was cited from."""

    text: str
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class Reply:
    """One turn from the model."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    segments: tuple[Segment, ...] = ()
    """Text blocks with their citations. The research loop ignores these;
    datasheet extraction is built on them."""
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    content: Any = None
    """The assistant blocks, exactly as the SDK returned them. Appended to the
    conversation unchanged — never rebuilt."""


class ModelClient(Protocol):
    """What the research loop needs from a model."""

    model: str

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> Reply:
        """One turn. ``on_text`` receives text as it arrives, if given."""
        ...


class AnthropicClient:
    """:class:`ModelClient` over the Anthropic SDK."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client if client is not None else _build_client()

    def reply(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> Reply:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": list(messages),
            "tools": list(tools),
            # Adaptive thinking. `budget_tokens` is not a thing on this model —
            # depth is `effort`, and a fixed thinking budget is rejected.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }

        try:
            with self._client.messages.stream(**request) as stream:
                if on_text is not None:
                    for text in stream.text_stream:
                        on_text(text)
                message = stream.get_final_message()
        # Broad on purpose: the SDK's exception tree is its own, and this
        # module is imported where the SDK is absent.
        except Exception as exc:
            raise _translate(exc) from exc

        return _read(message)


def _build_client() -> Any:
    try:
        import anthropic  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise LlmUnavailable(
            "the research agent needs the Anthropic SDK: pip install 'klm[agent]'"
        ) from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # a missing key surfaces differently by version
        raise LlmUnavailable(f"no usable Anthropic credentials: {exc}") from exc


def _translate(exc: Exception) -> LlmError:
    """Turn an SDK exception into something a user can act on.

    By class name rather than by importing the SDK's exception tree: this
    module is imported on machines where `anthropic` is not installed, and a
    diagnostic that only works when the dependency is present is a diagnostic
    that never runs on the machine that needed it.
    """
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return LlmUnavailable(f"{name}: {exc}")
    return LlmError(f"{name}: {exc}")


def _read(message: Any) -> Reply:
    """Read the SDK's message into klm's :class:`Reply`."""
    texts: list[str] = []
    segments: list[Segment] = []
    calls: list[ToolCall] = []
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            text = str(getattr(block, "text", ""))
            texts.append(text)
            segments.append(Segment(text, _citations(block)))
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", None) or {}),
                )
            )

    raw = getattr(message, "usage", None)
    usage = Usage(
        input_tokens=_int(getattr(raw, "input_tokens", 0)),
        output_tokens=_int(getattr(raw, "output_tokens", 0)),
        cache_read_input_tokens=_int(getattr(raw, "cache_read_input_tokens", 0)),
        cache_creation_input_tokens=_int(getattr(raw, "cache_creation_input_tokens", 0)),
    )
    return Reply(
        text="\n".join(t for t in texts if t).strip(),
        tool_calls=tuple(calls),
        segments=tuple(segments),
        stop_reason=str(getattr(message, "stop_reason", "") or "end_turn"),
        usage=usage,
        content=getattr(message, "content", None),
    )


def _citations(block: Any) -> tuple[Citation, ...]:
    """Read a text block's citations, whatever location type they carry.

    A PDF cites by page, plain text by character offset. Only the page form
    means anything to a person reading a datasheet, so a character-located
    citation keeps its quote and reports no page rather than inventing one.
    """
    found: list[Citation] = []
    for item in getattr(block, "citations", None) or []:
        quote = str(getattr(item, "cited_text", "") or "").strip()
        if not quote:
            continue
        found.append(
            Citation(
                quote=quote,
                start_page=_optional_int(getattr(item, "start_page_number", None)),
                end_page=_optional_int(getattr(item, "end_page_number", None)),
                title=getattr(item, "document_title", None),
            )
        )
    return tuple(found)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
