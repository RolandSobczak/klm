"""Making fabrication output byte-reproducible.

Two runs of the same commit must produce identical files, or artifact diffing is
useless and every fab package looks modified. Gerber and Excellon files carry a
generation timestamp, so they never do by default.

KiCad offers no way out. It does not honour `SOURCE_DATE_EPOCH`, the
reproducible-builds standard: `GbrMakeCreationDateAttributeString` reads the
wall clock unconditionally, with no environment override of any kind
(docs/14 Q11). So the normalisation happens here, after the files are written.

**The timestamp is replaced, not blanked.** The design note said "zeroes", and
that is what raised the worry that a fab's parser might reject the result. A
well-formed `%TF.CreationDate,…*%` carrying an unchanging value cannot upset a
parser that accepted the original, whereas an empty or malformed field plausibly
could. Substituting a fixed, valid timestamp closes the risk by construction
rather than by testing against an upload.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["EPOCH", "normalize_directory", "normalize_text"]

#: The fixed stamp every normalised file carries. A real date, in the format
#: KiCad itself writes, chosen only for being unmistakably not "now".
EPOCH = "1980-01-01T00:00:00+00:00"
_EPOCH_PLAIN = "1980-01-01 00:00:00"

#: `%TF.CreationDate,2026-08-09T12:00:00+02:00*%` — the Gerber X2 attribute.
_TF_CREATION = re.compile(
    r"(%TF\.CreationDate,)[^*]*(\*%)", re.IGNORECASE
)
#: `G04 Created by KiCad (…) date 2026-08-09 12:00:00*` — the human-readable
#: comment, which is separate from the X2 attribute and just as unstable.
_G04_DATE = re.compile(
    r"(G04[^*\n]*?\bdate\s+)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^*\n]*)", re.IGNORECASE
)
#: `; DRILL file {KiCad …} date 2026-08-09 12:00:00` — Excellon's header.
_DRILL_DATE = re.compile(
    r"(;[^\n]*?\bdate\s+)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\n]*)", re.IGNORECASE
)

#: Suffixes worth rewriting. Anything else in a fab package is klm's own output,
#: which is already deterministic.
SUFFIXES = frozenset(
    {".gbr", ".gbrjob", ".gbl", ".gtl", ".gbs", ".gts", ".gbo", ".gto", ".gko",
     ".gm1", ".gp1", ".gp2", ".drl", ".txt", ".xln"}
)


def normalize_text(text: str) -> str:
    """Replace every generation timestamp with :data:`EPOCH`."""
    text = _TF_CREATION.sub(rf"\g<1>{EPOCH}\g<2>", text)
    text = _G04_DATE.sub(rf"\g<1>{_EPOCH_PLAIN}", text)
    return _DRILL_DATE.sub(rf"\g<1>{_EPOCH_PLAIN}", text)


def normalize_directory(directory: Path) -> int:
    """Normalise every fab file under ``directory``. Returns how many changed."""
    changed = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        try:
            original = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            # A binary file with a gerber-ish suffix is not ours to rewrite.
            continue
        replaced = normalize_text(original)
        if replaced != original:
            path.write_text(replaced, encoding="utf-8", newline="")
            changed += 1
    return changed
