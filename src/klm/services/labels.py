"""Drawer labels — the piece that closes the loop between catalog and bench.

The constraint is size: around 14x14 mm for a 3D-printed SMD drawer. What goes
on one is decided by what you need while *standing at the drawer*: the value and
the package (`100nF 0402 X7R`), not the MPN. You can look an MPN up; you cannot
tell two drawers of ceramics apart without the value.

**klm does not generate a Data Matrix, and that is a decision rather than an
omission.** The design called for one at roughly 6x6 mm, and
[Q6](../../docs/14-open-questions.md#q6) records that nobody has yet confirmed a
symbol that small is reliably scannable by the phone or scanner actually in use.
Writing an ECC200 encoder is a few hundred lines of Reed-Solomon that no test
here can validate — there is no scanner on this machine to read the output — and
a barcode that looks right, passes a visual check and does not scan is precisely
the failure this project refuses elsewhere (chip land patterns, QA `unchecked`,
bundled rotation data). So the label carries a **short human-readable ID** in
Crockford Base32, which `klm labels scan` resolves back to a part. When a
printer and a scanner exist to test against, the barcode drops into the space
the layout already reserves for it.

Output is PDF and PNG, both written with the standard library: a fab package
that needs a pip install to print a label is a fab package that does not get
printed.
"""

from __future__ import annotations

import sqlite3
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from klm.ids import short_id
from klm.model import Part
from klm.services.catalog import get_part, list_parts

__all__ = [
    "Label",
    "LabelSheet",
    "labels_for_parts",
    "render_png",
    "resolve_short_id",
    "write_pdf",
]

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class Label:
    """One drawer's worth of information."""

    klm_id: str
    short: str
    primary: str
    """What identifies the drawer at a glance: `100nF`, `4.7k`."""
    secondary: str = ""
    """Package and anything else that distinguishes near-identical drawers."""

    @classmethod
    def for_part(cls, part: Part) -> Label:
        detail = " ".join(
            token
            for token in (
                part.package or "",
                _parameter(part, "Dielectric") or _parameter(part, "Tolerance") or "",
            )
            if token
        )
        return cls(
            klm_id=part.klm_id,
            short=short_id(part.klm_id),
            primary=_value_of(part),
            secondary=detail,
        )


def _value_of(part: Part) -> str:
    value = _parameter(part, "Value")
    return value or part.mpn


def _parameter(part: Part, name: str) -> str:
    parameter = part.parameter(name)
    if parameter is None:
        return ""
    if parameter.value_text:
        return parameter.value_text
    return "" if parameter.value_num is None else f"{parameter.value_num:g}"


def labels_for_parts(conn: sqlite3.Connection, klm_ids: list[str]) -> list[Label]:
    out = []
    for klm_id in klm_ids:
        part = get_part(conn, klm_id)
        if part is not None:
            out.append(Label.for_part(part))
    return out


def resolve_short_id(conn: sqlite3.Connection, short: str) -> list[Part]:
    """Every part whose short ID matches — plural, because collisions are possible.

    Reported rather than resolved: klm shortens `klm_id` to something a person
    can read off a drawer, and a shortening that can collide must say so instead
    of picking one.
    """
    wanted = short.strip().upper()
    return [part for part in list_parts(conn) if short_id(part.klm_id).upper() == wanted]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelSheet:
    """A grid of labels on a page, in millimetres."""

    label_width: float = 14.0
    label_height: float = 14.0
    columns: int = 12
    rows: int = 18
    page_width: float = 210.0
    page_height: float = 297.0
    margin_x: float = 10.0
    margin_y: float = 10.0
    gap_x: float = 2.0
    gap_y: float = 2.0

    @property
    def per_page(self) -> int:
        return self.columns * self.rows

    def position(self, index: int) -> tuple[float, float]:
        """Bottom-left corner of the label at ``index`` on its page, in mm."""
        slot = index % self.per_page
        column, row = slot % self.columns, slot // self.columns
        x = self.margin_x + column * (self.label_width + self.gap_x)
        # PDF's origin is bottom-left; labels fill from the top of the page,
        # which is how a person feeds a sheet.
        y = self.page_height - self.margin_y - (row + 1) * self.label_height - row * self.gap_y
        return x, y


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_PT_PER_MM = 72.0 / 25.4


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _page_content(sheet: LabelSheet, labels: list[Label]) -> str:
    """One page of label boxes and text, as a PDF content stream."""
    parts: list[str] = ["0.5 w"]
    for index, label in enumerate(labels):
        x, y = sheet.position(index)
        px, py = x * _PT_PER_MM, y * _PT_PER_MM
        width, height = sheet.label_width * _PT_PER_MM, sheet.label_height * _PT_PER_MM
        parts.append(f"{px:.2f} {py:.2f} {width:.2f} {height:.2f} re S")

        rows = [
            (label.primary, 7.0, height - 10.0),
            (label.secondary, 5.0, height - 17.0),
            (label.short, 5.0, 3.0),
        ]
        for text, size, offset in rows:
            if not text:
                continue
            parts.append(
                f"BT /F1 {size:.1f} Tf {px + 3:.2f} {py + offset:.2f} Td "
                f"({_escape(text)}) Tj ET"
            )
    return "\n".join(parts)


def write_pdf(labels: list[Label], target: Path, *, sheet: LabelSheet | None = None) -> Path:
    """Write a label sheet as a PDF, using nothing but the standard library.

    A deliberately minimal writer: one font, one page size, no compression. The
    output is a few kilobytes and every viewer opens it, which is the whole
    requirement.
    """
    layout = sheet or LabelSheet()
    pages = [
        labels[i : i + layout.per_page] for i in range(0, max(len(labels), 1), layout.per_page)
    ] or [[]]

    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1", errors="replace"))
        return len(objects)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    for page in pages:
        stream = _page_content(layout, page)
        content_ids.append(add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
        page_ids.append(0)  # placeholder, filled below

    pages_id = len(objects) + len(pages) + 1
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {layout.page_width * _PT_PER_MM:.2f} "
            f"{layout.page_height * _PT_PER_MM:.2f}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(out))
    return target


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

#: A 5x7 bitmap font, enough for the characters a label carries. Drawn here
#: rather than pulled from a dependency because a label that needs a font
#: package installed is a label that does not get printed.
_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    " ": ("00000",) * 7,
}
_DEFAULT_GLYPH = ("11111", "10001", "10001", "10001", "10001", "10001", "11111")

_LETTERS = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "11110", "10001", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "11110", "10000", "10000", "10000", "11111"),
    "F": ("11111", "10000", "11110", "10000", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "11111", "10001", "10001", "10001", "10001"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}
_GLYPHS.update(_LETTERS)


def _draw(pixels: list[bytearray], x: int, y: int, text: str, scale: int) -> None:
    cursor = x
    for character in text.upper():
        glyph = _GLYPHS.get(character, _DEFAULT_GLYPH)
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px, py = cursor + column * scale + dx, y + row * scale + dy
                        if 0 <= py < len(pixels) and 0 <= px < len(pixels[0]):
                            pixels[py][px] = 0
        cursor += (len(glyph[0]) + 1) * scale


def render_png(label: Label, target: Path, *, dpi: int = 300, size_mm: float = 14.0) -> Path:
    """One label as a 1-bit-ish greyscale PNG, at a printable resolution."""
    side = max(32, int(size_mm / MM_PER_INCH * dpi))
    pixels = [bytearray(b"\xff" * side) for _ in range(side)]

    scale = max(1, side // 40)
    _draw(pixels, scale * 2, scale * 3, label.primary[:10], scale * 2)
    if label.secondary:
        _draw(pixels, scale * 2, scale * 18, label.secondary[:14], scale)
    _draw(pixels, scale * 2, side - scale * 10, label.short, scale)

    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_png(side, side, raw))
    return target


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


@dataclass
class LabelRun:
    labels: list[Label] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
