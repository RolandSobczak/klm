"""Render the README's screenshots from the running application.

    pip install playwright pillow && playwright install chromium
    python tools/screenshots.py

Seeds a small demo catalog in a throwaway directory, starts `klm serve` against
it, drives a headless Chromium through each screen, and writes framed PNGs to
`docs/images/`.

**The pictures are of the real application.** Nothing here draws a mock-up of a
screen — a README that shows a UI the code does not produce is a lie with a long
half-life, and this project's whole discipline is that a thing which looks right
and is wrong is worse than an absence. What *is* synthetic is the data: five
parts, two suppliers and a small board, seeded by this script so the screens have
something in them. The README says so.

Regenerate after a UI change and commit the result. The screenshots are checked
in rather than built in CI because they are documentation, and a README whose
images are produced by a workflow is a README that is broken on every fork.
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "images"
VIEWPORT = {"width": 1180, "height": 760}
#: 2 gives a sharp image on a HiDPI display without a file GitHub throttles.
SCALE = 2


# ---------------------------------------------------------------------------
# A catalog worth photographing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoPart:
    mpn: str
    manufacturer: str
    category: str
    package: str
    value: str = ""
    description: str = ""
    lcsc: str = ""
    tme: str = ""
    price: float = 0.0
    stock: int = 0


DEMO_PARTS = (
    DemoPart("RC0402FR-074K7L", "Yageo", "Passive/Resistor", "0402", "4.7k",
             "4.7 kOhm 1% 1/16W thick film", lcsc="C25900", tme="RC0402FR-074K7L",
             price=0.04, stock=48000),
    DemoPart("CL05B104KO5NNNC", "Samsung", "Passive/Capacitor", "0402", "100nF",
             "100 nF 16V X7R", lcsc="C1525", tme="CL05B104KO5NNNC",
             price=0.06, stock=32000),
    DemoPart("CL05A106MQ5NUNC", "Samsung", "Passive/Capacitor", "0402", "10uF",
             "10 uF 6.3V X5R", lcsc="C15525", price=0.19, stock=8200),
    DemoPart("AMS1117-3.3", "AMS", "IC/Power/Regulator/Linear", "SOT-223",
             description="1 A low-dropout regulator, fixed 3.3 V",
             lcsc="C6186", price=0.68, stock=15400),
    DemoPart("STM32G031K8T6", "STMicroelectronics", "IC/Microcontroller", "LQFP-32",
             description="Cortex-M0+ 64 MHz, 64 kB flash, 8 kB RAM",
             lcsc="C529291", tme="STM32G031K8T6", price=9.40, stock=2600),
)

#: Deliberately not enough for the build below. Ordering is the screen where klm
#: reasons out loud, and a plan that says "you already have all of these" is a
#: screenshot of the feature not running.
STOCK = {
    "RC0402FR-074K7L": ("Cabinet A/1", 120),
    "CL05B104KO5NNNC": ("Cabinet A/2", 60),
    "AMS1117-3.3": ("Cabinet B/4", 3),
}

#: What the ordering screen is asked to plan.
DEMO_BUILD = "100x sensor-board"

#: A board that uses the parts above, so the project screen has a BOM.
SCHEMATIC_PARTS = ("RC0402FR-074K7L", "CL05B104KO5NNNC", "AMS1117-3.3")


def run(args: list[str], *, home: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KLM_HOME": str(home)}
    result = subprocess.run(
        [sys.executable, "-m", "klm.cli.main", *args],
        env=env, capture_output=True, text=True, cwd=ROOT,
    )
    if check and result.returncode > 1:
        raise RuntimeError(f"klm {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def seed(home: Path, project: Path) -> None:
    """Build the demo catalog through the CLI, exactly as a user would."""
    run(["init"], home=home)

    for part in DEMO_PARTS:
        args = [
            "part", "add", "--mpn", part.mpn, "--mfr", part.manufacturer,
            "--category", part.category, "--package", part.package, "--offline",
        ]
        if part.value:
            args += ["--value", part.value]
        if part.description:
            args += ["--description", part.description]
        run(args, home=home)

        # Offers, entered by hand — which is the supported LCSC path anyway
        # (ADR-0009), and keeps this script off the network entirely.
        for supplier, pn in (("lcsc", part.lcsc), ("tme", part.tme)):
            if not pn:
                continue
            run([
                "offers", part.mpn, "--supplier", supplier, "--add", pn,
                "--price", str(part.price), "--stock", str(part.stock),
            ], home=home)

    for mpn, (location, quantity) in STOCK.items():
        run(["stock", "adjust", mpn, "--location", location, "--set", str(quantity)], home=home)

    # Approve the passives, leave the rest as drafts. That is what a real
    # catalog looks like, and it is also true: klm generates a chip land pattern
    # and refuses to invent an LQFP-32, so the STM32 genuinely has no assets yet
    # and genuinely should not be approved.
    for mpn in ("RC0402FR-074K7L", "CL05B104KO5NNNC", "CL05A106MQ5NUNC"):
        run(["part", "approve", mpn], home=home)
    _write_project(project)
    run(["vendor", "--project", str(project), "--allow-unresolved"], home=home)
    _create_drift(home, project)


def _create_drift(home: Path, project: Path) -> None:
    """Edit the project's copy of a footprint, as KiCad's editor would.

    A screenshot of a `clean` diff demonstrates nothing — the screen exists for
    the moment two people changed the same part. So the demo contains a real
    one: the pads on the project's copy were widened, and the catalog has not
    moved, which is exactly `project-ahead`.
    """
    pretty = project / "libraries" / "sensor-board.pretty"
    for footprint in pretty.glob("R_0402*.kicad_mod"):
        text = footprint.read_text(encoding="utf-8")
        footprint.write_text(text.replace("0.54", "0.62").replace("0.5 0.6", "0.58 0.6"),
                             encoding="utf-8")


def _write_project(project: Path) -> None:
    """A small board using the demo parts, in KiCad's own format."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "sensor-board.kicad_pro").write_text("{}\n", encoding="utf-8")

    prefixes = {"RC": "R", "CL": "C"}
    placements = []
    for index, mpn in enumerate(SCHEMATIC_PARTS, start=1):
        for count in range(1, 4 if mpn.startswith("RC") else 2):
            reference = f"{prefixes.get(mpn[:2], 'U')}{count}"
            placements.append(f"""	(symbol
		(lib_id "KLM:{mpn}")
		(at {30 * index} {20 * count} 0)
		(uuid "a{index}{count}")
		(property "Reference" "{reference}" (at 0 0 0))
		(property "Value" "{mpn}" (at 0 0 0))
		(property "Footprint" "KLM:{mpn}" (at 0 0 0))
	)""")

    (project / "sensor-board.kicad_sch").write_text(
        "(kicad_sch\n\t(version 20231120)\n\t(generator \"eeschema\")\n\t(uuid \"demo\")\n"
        + "\n".join(placements)
        + "\n)\n",
        encoding="utf-8",
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for(port: int, *, attempts: int = 120) -> None:
    for _ in range(attempts):
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError("the server never came up")


# ---------------------------------------------------------------------------
# The window frame
# ---------------------------------------------------------------------------

FRAME = """
<!doctype html><meta charset="utf-8">
<style>
  html, body { margin:0; background:transparent; }
  .pad { padding:38px 44px 52px; display:inline-block; }
  .window {
    width:%(width)spx; border-radius:11px; overflow:hidden;
    box-shadow: 0 24px 60px rgba(0,0,0,.45), 0 2px 8px rgba(0,0,0,.35);
    font: 13px/1 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  }
  .bar {
    height:34px; background:linear-gradient(#3b414d, #333944);
    border-bottom:1px solid #23272f; display:flex; align-items:center;
    padding:0 12px; position:relative;
  }
  .lights { display:flex; gap:8px; }
  .lights i { width:12px; height:12px; border-radius:50%%; display:block; }
  .r { background:#ff5f57; } .y { background:#febc2e; } .g { background:#28c840; }
  .title {
    position:absolute; left:0; right:0; text-align:center; color:#b7bec9;
    font-weight:600; letter-spacing:.02em; pointer-events:none;
  }
  img { display:block; width:%(width)spx; }
</style>
<div class="pad"><div class="window">
  <div class="bar">
    <span class="lights"><i class="r"></i><i class="y"></i><i class="g"></i></span>
    <span class="title">%(title)s</span>
  </div>
  <img src="%(src)s">
</div></div>
"""


def frame(page, raw: Path, title: str, target: Path) -> None:  # type: ignore[no-untyped-def]
    """Wrap a screenshot in a macOS-style window and re-shoot it.

    Done in the browser rather than with an image library because the rounded
    corners, the shadow and the title-bar gradient are three lines of CSS and a
    day of fiddling in Pillow, and the result is the same picture.
    """
    page.set_viewport_size({"width": VIEWPORT["width"] + 120, "height": VIEWPORT["height"] + 120})
    # A data URI, not a `file://` one: `set_content` gives the page an
    # `about:blank` origin, and Chromium refuses to load local files into that.
    # The symptom is a broken-image glyph inside a perfectly rendered frame.
    encoded = base64.b64encode(raw.read_bytes()).decode("ascii")
    page.set_content(
        FRAME
        % {
            "width": VIEWPORT["width"],
            "title": title,
            "src": f"data:image/png;base64,{encoded}",
        }
    )
    page.wait_for_load_state("networkidle")
    # `omit_background` keeps the shadow soft against whatever GitHub's theme is
    # rather than baking a grey rectangle behind it.
    page.locator(".pad").screenshot(path=str(target), omit_background=True)


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


def capture(base: str, project: Path, raw_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        browser = play.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme="dark"
        )
        page = context.new_page()
        shot = context.new_page()

        def grab(name: str, title: str) -> None:
            raw = raw_dir / f"{name}-raw.png"
            page.screenshot(path=str(raw))
            frame(shot, raw, title, OUTPUT / f"{name}.png")
            print(f"  {name}.png")

        page.goto(base)
        page.wait_for_selector("#parts tbody tr")

        # -- catalog, with a part selected so the previews are visible --------
        # A passive, because it is one klm can actually draw: it generates chip
        # land patterns and nothing else, so the microcontroller in this catalog
        # has no footprint and its detail pane would show two "no asset" panels.
        page.locator("#parts tbody tr", has_text="RC0402FR-074K7L").click()
        page.wait_for_selector("#detail img")
        page.wait_for_timeout(900)  # let both SVGs decode
        grab("catalog", "klm — Catalog")

        # -- add part --------------------------------------------------------
        page.click("#add-open")
        page.wait_for_selector("dialog#add[open]")
        page.fill("input[name=mpn]", "TPS62840DLCR")
        page.fill("input[name=manufacturer]", "Texas Instruments")
        page.fill("input[name=category]", "IC/Power/Regulator/Switching")
        page.fill("input[name=package]", "SOT-563")
        page.wait_for_timeout(200)
        grab("add-part", "klm — Add a part")
        page.keyboard.press("Escape")

        # -- project ---------------------------------------------------------
        page.click("#tabs button[data-view=project]")
        page.fill("#project-path", str(project))
        page.click("#project-open")
        page.wait_for_selector("#project-body table")
        page.wait_for_timeout(300)
        grab("project", "klm — Project")

        # -- the sync diff ---------------------------------------------------
        # The row that actually drifted, not the first one — a `clean` diff is
        # a screenshot of nothing happening.
        rows = page.locator("#project-body table").last.locator("tbody tr")
        drifted = rows.filter(has_text="project-ahead")
        target = drifted.first if drifted.count() else rows.first
        if rows.count():
            target.click()
            page.wait_for_selector("dialog#diff[open]")
            page.wait_for_selector("dialog#diff .patch")
            page.wait_for_timeout(400)
            grab("sync-diff", "klm — What moved")
            page.click("#diff-close")

        # -- ordering --------------------------------------------------------
        page.click("#tabs button[data-view=orders]")
        page.fill("#build", DEMO_BUILD)
        page.fill("#projects-root", str(project.parent))
        page.click("#plan")
        page.wait_for_selector("#orders-body table tbody tr")
        page.wait_for_timeout(300)
        # A plan with no lines renders as an empty table and a caption, which
        # photographs as "the feature does nothing". Better to fail the run than
        # to commit that.
        if not page.locator("#orders-body table tbody tr").count():
            raise RuntimeError("the demo build produced no order lines")
        grab("ordering", "klm — Ordering")

        # -- health ----------------------------------------------------------
        page.click("#tabs button[data-view=health]")
        page.wait_for_selector("#health-body table")
        page.wait_for_timeout(300)
        grab("health", "klm — Health")

        browser.close()


def main() -> int:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        print("needs playwright: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    # A fixed, readable path rather than `mkdtemp`: it appears in two of the
    # screenshots, and `/tmp/klm-shots-bh8bbuwi/` photographs as clutter.
    workspace = Path(tempfile.gettempdir()) / "klm-demo"
    shutil.rmtree(workspace, ignore_errors=True)
    home = workspace / "catalog"
    project = workspace / "projects" / "sensor-board"
    raw_dir = workspace / "raw"
    raw_dir.mkdir(parents=True)

    print(f"seeding a demo catalog in {workspace}")
    seed(home, project)

    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "klm.cli.main", "serve", "--port", str(port)],
        env={**os.environ, "KLM_HOME": str(home)},
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(port)
        print(f"capturing from http://127.0.0.1:{port}")
        capture(f"http://127.0.0.1:{port}", project, raw_dir)
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(workspace, ignore_errors=True)

    print(f"\nwrote {len(list(OUTPUT.glob('*.png')))} images to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
