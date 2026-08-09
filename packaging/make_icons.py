"""Generate the platform icons from `src/klm/api/static/logo.png`.

    packaging/klm.ico    Windows — the installer, both executables, the taskbar
    packaging/klm.icns   macOS — the .app bundle, Dock and Finder

One source image in the repository, every derived form generated. Checking the
binaries in as well would give three files that are supposed to be the same
picture and no mechanism that says when they stop being it.

Run from the repository root; needs Pillow, which is a build-time dependency
only — klm itself has no runtime dependencies at all, and adding an imaging
library to ship an icon would be a poor trade.

    pip install pillow
    python packaging/make_icons.py

Not the tab favicon: that is `static/favicon.svg`, drawn by hand rather than
scaled, because at 16 pixels the full artwork averages to a green square.

Nothing is generated for Linux. The tarball carries no desktop integration, so
there is no icon to install; the page's own favicon is what a browser shows.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "klm" / "api" / "static" / "logo.png"
ICO = ROOT / "packaging" / "klm.ico"
ICNS = ROOT / "packaging" / "klm.icns"

#: What Windows actually asks for: the list view, the desktop, the taskbar, the
#: Alt-Tab switcher and high-DPI versions of each.
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def square_source() -> object:
    """The artwork, cropped to what is drawn and padded back to a square.

    The source is a mark on transparency inside a wider canvas. Both platforms
    scale an icon to fill its box, so that empty margin would shrink the mark
    everywhere it appears — and cropping alone leaves a non-square image, which
    they stretch rather than letterbox.
    """
    from PIL import Image

    image = Image.open(SOURCE).convert("RGBA")
    box = image.getbbox()
    if box:
        image = image.crop(box)
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return square


def main() -> int:
    # The source check comes first on purpose: with no artwork there is nothing
    # to convert, so a checkout without it should not also need an imaging
    # library installed to be told so.
    if not SOURCE.is_file():
        # Not an error, and this is the same judgement the spec and the Inno
        # script already make: the icon is artwork, and absence degrades to the
        # platform default rather than failing a release. Exiting non-zero here
        # made a build that would have worked fine stop at the second step.
        print(
            f"no source image at {SOURCE.relative_to(ROOT)} — skipping.\n"
            "  The build will use the platform's default icon.",
            file=sys.stderr,
        )
        return 0

    try:
        import PIL  # noqa: F401
    except ModuleNotFoundError:
        # A real error: there *is* artwork and the tool to convert it is absent,
        # so the build would silently ship without an icon it was meant to have.
        print("this needs Pillow: pip install pillow", file=sys.stderr)
        return 2

    square = square_source()
    square.save(ICO, format="ICO", sizes=SIZES)  # type: ignore[attr-defined]
    print(f"wrote {ICO.relative_to(ROOT)}  ({len(SIZES)} sizes)")

    # ICNS demands a square at least 16px and Pillow writes the whole set from
    # one image. It is generated on every platform, not only macOS, so a Linux
    # checkout can still tell whether the source image is usable.
    try:
        square.save(ICNS, format="ICNS")  # type: ignore[attr-defined]
        print(f"wrote {ICNS.relative_to(ROOT)}")
    except (OSError, ValueError) as exc:
        # Not fatal: the macOS build falls back to the default icon, and the
        # alternative — failing the whole release — is worse than a plain one.
        print(f"could not write {ICNS.name}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
