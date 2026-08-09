"""Datasheets: fetch them, cache them, and read parameters *with provenance*.

The guardrail this module exists to enforce is one line of docs/11 §5 —
"`datasheet_extract` must return page number + quoted snippet per parameter;
parameters without provenance are rejected". Everything below is that sentence
made mechanical.

**The quote comes from the API's citation machinery, not from the model.** The
PDF is sent as a document block with `citations: {enabled: true}`, and the
`cited_text` that comes back is lifted from the document rather than written.
A model *asked* to quote can paraphrase, and a paraphrase that looks like a
quote is indistinguishable from provenance — which would make the guardrail
decorative. So klm reads the citation metadata, pairs it with the parameter the
model stated, and **drops any parameter whose text block carried no citation**,
reporting it rather than keeping it.

Two things klm refuses to do here:

* **Guess what a file is.** A "datasheet" URL that returns an HTML login page
  is ordinary — suppliers gate PDFs behind redirects all the time. The magic
  bytes are checked, and a non-PDF is reported rather than handed to the model
  as a document.
* **Parse the PDF itself.** klm caches the bytes and lets the API read them.
  A hand-rolled text extractor would work on the simple half of datasheets and
  silently produce nonsense on the other half, which is the failure mode this
  project refuses everywhere else. It also keeps the core dependency-free.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klm.llm.client import Citation, LlmError, ModelClient, Usage

__all__ = [
    "BinaryTransport",
    "Datasheet",
    "DatasheetError",
    "ExtractedParameter",
    "Extraction",
    "extract",
    "fetch",
    "load",
    "urllib_bytes",
]

PDF_MAGIC = b"%PDF-"

#: The API caps a request at 32 MB, and base64 inflates by 4/3 — so the raw
#: file has to stay under about 24 MB to be sendable at all. Refusing here,
#: with the size, beats a 413 from three layers up.
MAX_BYTES = 24 * 1024 * 1024

USER_AGENT = "klm/0.1 (+https://github.com/RolandSobczak/klm)"
DEFAULT_TIMEOUT = 30.0

BinaryTransport = Callable[[str, float], bytes]
"""``(url, timeout) -> bytes``. Injected, so the cache is testable offline."""


class DatasheetError(Exception):
    """The datasheet could not be fetched, or is not a datasheet."""


@dataclass(frozen=True)
class Datasheet:
    """A cached PDF, addressed by the hash of its contents."""

    sha: str
    """`sha256:…` — the handle the agent is given, and the identity that goes
    on a part. Content-addressed, so the same PDF from two supplier URLs is
    one datasheet."""
    url: str
    path: Path
    size: int

    @property
    def media_type(self) -> str:
        return "application/pdf"

    def read(self) -> bytes:
        return self.path.read_bytes()

    def document_block(self, *, title: str | None = None) -> dict[str, Any]:
        """The PDF as a content block, with citations switched on."""
        block: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": b64encode(self.read()).decode("ascii"),
            },
            "citations": {"enabled": True},
        }
        if title:
            block["title"] = title
        return block


def urllib_bytes(url: str, timeout: float) -> bytes:
    """The real transport. Binary, unlike the supplier layer's text one."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read(MAX_BYTES + 1))
    except urllib.error.HTTPError as exc:
        raise DatasheetError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DatasheetError(f"{url}: {exc}") from exc


def fetch(
    url: str,
    cache_dir: Path,
    *,
    transport: BinaryTransport | None = None,
    refresh: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Datasheet:
    """Fetch a datasheet, or return the cached copy.

    Cached by the URL — that is the request being repeated — while the
    *identity* returned is the hash of the contents. Two supplier links to the
    same PDF are two cache entries and one datasheet, which is the answer that
    makes "is this the datasheet we already read?" answerable.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise DatasheetError(f"not a fetchable URL: {url!r}")

    path = cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.pdf"
    if path.exists() and not refresh:
        return _describe(url, path)

    payload = (transport or urllib_bytes)(url, timeout)
    if len(payload) > MAX_BYTES:
        raise DatasheetError(
            f"{url}: {len(payload) / 1e6:.1f} MB is larger than klm can send "
            f"({MAX_BYTES / 1e6:.0f} MB); download it by hand"
        )
    if not payload.startswith(PDF_MAGIC):
        # Suppliers gate PDFs behind logins and redirects; what comes back is
        # then an HTML page that would be sent to the model as a "datasheet".
        raise DatasheetError(f"{url}: not a PDF (starts with {payload[:8]!r})")

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _describe(url, path)


def load(sha: str, cache_dir: Path) -> Datasheet | None:
    """The cached datasheet with this content hash, if it is still here."""
    for path in sorted(cache_dir.glob("*.pdf")):
        if _sha(path.read_bytes()) == sha:
            return Datasheet(sha=sha, url="", path=path, size=path.stat().st_size)
    return None


def _describe(url: str, path: Path) -> Datasheet:
    payload = path.read_bytes()
    return Datasheet(sha=_sha(payload), url=url, path=path, size=len(payload))


def _sha(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedParameter:
    """One parameter, and the text in the datasheet that says so."""

    name: str
    value: str
    citations: tuple[Citation, ...]

    @property
    def page(self) -> int | None:
        return self.citations[0].page if self.citations else None

    def __str__(self) -> str:
        return f"{self.name} = {self.value} ({self.citations[0] if self.citations else 'uncited'})"


@dataclass
class Extraction:
    """What reading a datasheet produced, including what it did not."""

    parameters: list[ExtractedParameter] = field(default_factory=list)
    uncited: list[tuple[str, str]] = field(default_factory=list)
    """`(name, value)` the model stated with nothing cited behind it. Dropped
    from `parameters` and kept here: silently discarding them would hide that
    the model answered, and keeping them would defeat the guardrail."""
    missing: list[str] = field(default_factory=list)
    """Asked for, and not found in the document."""
    usage: Usage = field(default_factory=Usage)
    note: str = ""

    def explain(self) -> list[str]:
        lines = [str(parameter) for parameter in self.parameters]
        lines += [f"{name} = {value} — no citation, dropped" for name, value in self.uncited]
        lines += [f"{name}: not found in the datasheet" for name in self.missing]
        return lines


_SYSTEM = """\
You read component datasheets and report what they say. Every value you report \
must be quoted from the document — the machinery that records that quote runs \
on your output, so a value you state without pointing at where it is written \
is discarded rather than trusted.

Report one parameter per line, exactly `name: value`, using the name you were \
asked for and the units the datasheet uses. Cite the passage each value comes \
from. Where a datasheet gives min, typical and max, report the one asked for, \
and say which it is if it is ambiguous.

If the datasheet does not state a parameter, write `name: not stated` and cite \
nothing. That is a useful answer. Inferring it from a related figure, or from \
what parts like this usually do, is not."""

_LINE = re.compile(r"^\s*[-*]?\s*(?:\*\*)?([^:\n]{1,80}?)(?:\*\*)?\s*:\s*(.+?)\s*$")
_NOT_STATED = re.compile(r"^\s*(not stated|not specified|n/?a|unknown|—|-)\s*\.?\s*$", re.I)


def extract(
    datasheet: Datasheet,
    parameters: Sequence[str],
    client: ModelClient,
    *,
    title: str | None = None,
) -> Extraction:
    """Read `parameters` out of a datasheet, keeping only what is cited.

    The pairing rule is deliberately mechanical: a value is credited with the
    citations of the text block it was written in. A block the API attached no
    citation to yields an *uncited* parameter, which is dropped and reported.
    No heuristic tries to find a citation elsewhere in the response for it —
    that would be klm inventing provenance, which is the failure this whole
    module exists to prevent.
    """
    wanted = [name.strip() for name in parameters if name.strip()]
    if not wanted:
        return Extraction(note="no parameters were asked for")

    question = (
        "Report these parameters from the attached datasheet, one per line:\n"
        + "\n".join(f"- {name}" for name in wanted)
    )
    messages = [
        {
            "role": "user",
            "content": [datasheet.document_block(title=title), {"type": "text", "text": question}],
        }
    ]

    try:
        reply = client.reply(system=_SYSTEM, messages=messages, tools=[])
    except LlmError as exc:
        return Extraction(note=str(exc), missing=list(wanted))

    result = Extraction(usage=reply.usage)
    seen: dict[str, ExtractedParameter] = {}
    for segment in reply.segments or ():
        for name, value in _lines(segment.text):
            matched = _match(name, wanted)
            if matched is None or _NOT_STATED.match(value):
                continue
            if not segment.citations:
                result.uncited.append((matched, value))
                continue
            seen.setdefault(matched, ExtractedParameter(matched, value, segment.citations))

    result.parameters = [seen[name] for name in wanted if name in seen]
    cited_or_dropped = set(seen) | {name for name, _ in result.uncited}
    result.missing = [name for name in wanted if name not in cited_or_dropped]
    return result


def _lines(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is not None:
            found.append((match.group(1).strip(), match.group(2).strip()))
    return found


def _match(stated: str, wanted: Sequence[str]) -> str | None:
    """Map a name the model wrote back to the name that was asked for."""
    key = re.sub(r"[^a-z0-9]+", "", stated.lower())
    for name in wanted:
        if re.sub(r"[^a-z0-9]+", "", name.lower()) == key:
            return name
    return None
