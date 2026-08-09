"""Tests for the local API and the desktop shell.

The rule these exist to protect is ADR-0005's, kept by ADR-0012: **the shell
contains no logic**. Every route is a translation of a service function, so what
is worth testing is that each one calls the right service and shapes the result —
not that the answers are correct, which the service's own tests already cover.

pywebview cannot open a window in CI, so the shell is tested at its seam: port
selection, and that a missing webview degrades to serving a URL rather than
raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.projects import RESISTOR_ID, make_project, seed_resistor

from klm.api.desktop import HOST, WindowUnavailable, pick_port, run_window
from klm.api.jobs import JobRunner, JobState
from klm.services.catalog import get_part

fastapi = pytest.importorskip("fastapi", reason="the app extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(env, monkeypatch):  # type: ignore[no-untyped-def]
    from klm.api.server import create_app

    paths, conn, store = env
    seed_resistor(store, conn)
    monkeypatch.setenv("KLM_HOME", str(paths.home))
    return TestClient(create_app(paths.home)), paths, conn


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_api_only_translates(client) -> None:
    """A part from the API is the part the service returns, not a rebuild of it."""
    api, _paths, conn = client
    payload = api.get(f"/api/parts/{RESISTOR_ID}").json()
    part = get_part(conn, RESISTOR_ID)

    assert part is not None
    assert payload["mpn"] == part.mpn
    assert payload["manufacturer"] == part.manufacturer
    assert payload["klm_id"] == part.klm_id
    assert payload["status"] == str(part.status)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_parts_list_filters_by_search_and_status(client) -> None:
    api, _paths, _conn = client
    assert len(api.get("/api/parts").json()) == 1
    assert api.get("/api/parts?q=yageo").json()[0]["mpn"] == "RC0402FR-074K7L"
    assert api.get("/api/parts?q=nothing-like-this").json() == []
    assert api.get("/api/parts?status=draft").json() == []


def test_a_missing_part_is_a_404_not_a_traceback(client) -> None:
    api, _paths, _conn = client
    assert api.get("/api/parts/nope").status_code == 404


def test_approving_a_part_goes_through_the_service(client) -> None:
    api, _paths, conn = client
    response = api.post(f"/api/parts/{RESISTOR_ID}/status", json={"status": "deprecated"})

    assert response.status_code == 200
    part = get_part(conn, RESISTOR_ID)
    assert part is not None and str(part.status) == "deprecated"


def test_an_unknown_status_is_refused(client) -> None:
    api, _paths, _conn = client
    assert api.post(f"/api/parts/{RESISTOR_ID}/status", json={"status": "great"}).status_code == 400


def test_a_part_carries_its_offers_and_stock(client) -> None:
    api, _paths, conn = client
    from klm.services import stock

    stock.adjust(conn, RESISTOR_ID, "Cabinet A/1", set_to=42)
    payload = api.get(f"/api/parts/{RESISTOR_ID}").json()
    assert payload["stock"][0]["quantity"] == 42
    assert payload["offers"] == []


def test_lint_reports_through_the_api(client) -> None:
    api, _paths, _conn = client
    payload = api.get("/api/lint").json()
    assert payload["parts_checked"] == 1
    assert all({"rule", "severity", "message"} <= set(f) for f in payload["findings"])


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_a_project_reports_its_bom_and_mode(client, tmp_path: Path) -> None:
    api, _paths, _conn = client
    root = make_project(tmp_path / "board")

    payload = api.get("/api/projects", params={"path": str(root)}).json()
    assert payload["name"] == "my-board"
    assert payload["vendored"] is False
    assert payload["bom"][0]["references"] == ["R1"]
    assert "sync" not in payload


def test_vendoring_is_a_job_and_the_project_then_reports_sync(client, tmp_path: Path) -> None:
    api, _paths, _conn = client
    root = make_project(tmp_path / "board")

    job = api.post("/api/projects/vendor", json={"path": str(root)}).json()
    assert job["state"] in (JobState.PENDING, JobState.RUNNING)

    finished = _settle(api, job["id"])
    assert finished["state"] == JobState.DONE, finished["error"]
    assert finished["result"]["symbols"] == 1

    payload = api.get("/api/projects", params={"path": str(root)}).json()
    assert payload["vendored"] is True
    assert payload["sync"][0]["state"] == "clean"


def test_verification_is_synchronous_and_needs_no_catalog(client, tmp_path: Path) -> None:
    """`klm verify --clean-room` is fast and takes a project only (ADR-0007)."""
    api, _paths, _conn = client
    root = make_project(tmp_path / "board")

    payload = api.get("/api/projects/verify", params={"path": str(root)}).json()
    assert payload["failed"] is True
    assert any("partly vendored" in f["message"] for f in payload["findings"])


def test_a_path_that_is_not_a_project_is_a_400(client, tmp_path: Path) -> None:
    api, _paths, _conn = client
    (tmp_path / "empty").mkdir()
    assert api.get("/api/projects", params={"path": str(tmp_path / "empty")}).status_code == 400


# ---------------------------------------------------------------------------
# Ordering and stock
# ---------------------------------------------------------------------------


def test_order_planning_returns_lines_carts_and_the_reasoning(client, tmp_path: Path) -> None:
    api, _paths, conn = client
    from klm.model import Offer, PriceBreak
    from klm.services.offers import save_offer

    make_project(tmp_path / "sensor-board")
    save_offer(
        conn,
        Offer(supplier="tme", supplier_pn="R-1", klm_id=RESISTOR_ID, stock=9000, moq=1,
              currency="PLN", price_breaks=[PriceBreak(1, 0.4)],
              fetched_at="2026-08-09T10:00:00Z"),
    )

    payload = api.post(
        "/api/orders/plan", json={"build": "10x sensor-board", "projects": str(tmp_path)}
    ).json()
    assert payload["lines"][0]["supplier"] == "tme"
    assert payload["lines"][0]["explain"], "the UI shows why, so the API has to carry it"
    assert payload["carts"]["tme"]["assumptions"], "every estimate names its assumptions"


def test_a_malformed_build_plan_is_a_400(client) -> None:
    api, _paths, _conn = client
    assert api.post("/api/orders/plan", json={"build": "nonsense"}).status_code == 400


def test_stock_lists_and_globs(client) -> None:
    api, _paths, conn = client
    from klm.services import stock

    stock.adjust(conn, RESISTOR_ID, "Cabinet A/1", set_to=7)
    assert api.get("/api/stock").json()[0]["quantity"] == 7
    assert api.get("/api/stock", params={"location": "Cabinet B/*"}).json() == []


def test_orders_are_listed_and_fetched(client) -> None:
    api, _paths, conn = client
    from klm.model import Offer
    from klm.services.demand import DemandLine
    from klm.services.orders import create_order
    from klm.services.split import Assignment

    create_order(
        conn,
        "tme",
        [
            Assignment(
                line=DemandLine(klm_id=RESISTOR_ID, part=None),
                supplier="tme",
                offer=Offer(supplier="tme", supplier_pn="R-1", klm_id=RESISTOR_ID),
                quantity=50,
                unit_price=0.4,
            )
        ],
        order_id="tme-1",
    )
    assert api.get("/api/orders").json()[0]["id"] == "tme-1"
    assert api.get("/api/orders/tme-1").json()["lines"][0]["qty_ordered"] == 50
    assert api.get("/api/orders/nope").status_code == 404


def test_receiving_through_the_api_stocks_the_parts(client) -> None:
    api, _paths, conn = client
    from klm.model import Offer
    from klm.services.demand import DemandLine
    from klm.services.orders import create_order
    from klm.services.split import Assignment

    create_order(
        conn, "tme",
        [Assignment(line=DemandLine(klm_id=RESISTOR_ID, part=None), supplier="tme",
                    offer=Offer(supplier="tme", supplier_pn="R-1", klm_id=RESISTOR_ID),
                    quantity=25, unit_price=0.4)],
        order_id="tme-2",
    )
    payload = api.post("/api/orders/tme-2/receive", json={"location": "A/1"}).json()
    assert payload["state"] == "received"
    assert api.get("/api/stock").json()[0]["quantity"] == 25


# ---------------------------------------------------------------------------
# Health and the UI
# ---------------------------------------------------------------------------


def test_health_reports_tools_and_what_their_absence_costs(client) -> None:
    api, paths, _conn = client
    payload = api.get("/api/health").json()

    assert payload["initialised"] is True
    assert payload["catalog"] == str(paths.home)
    names = {tool["name"] for tool in payload["tools"]}
    assert {"kicad-cli", "freecadcmd"} <= names
    for tool in payload["tools"]:
        assert tool["consequence"], "a missing tool must say what it disables"


def test_the_ui_is_served(client) -> None:
    api, _paths, _conn = client
    page = api.get("/")
    assert page.status_code == 200
    assert "klm" in page.text
    assert api.get("/static/app.js").status_code == 200


def test_the_ui_pulls_in_nothing_from_the_network() -> None:
    """A desktop app that needs a CDN is a desktop app that fails offline."""
    static = Path(__file__).resolve().parents[1] / "src" / "klm" / "api" / "static"
    for path in static.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text.replace("http://127.0.0.1", "")
        assert "https://" not in text


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_a_job_records_progress_and_its_result() -> None:
    runner = JobRunner()
    job = runner.start("demo", lambda report: [report("one"), report("two")] and "done")
    lines = list(runner.watch(job.id))

    assert runner.get(job.id).state == JobState.DONE  # type: ignore[union-attr]
    assert "one" in lines and "two" in lines


def test_a_failing_job_reports_the_failure_rather_than_vanishing() -> None:
    runner = JobRunner()

    def explode(report):  # type: ignore[no-untyped-def]
        report("about to fail")
        raise ValueError("no")

    job = runner.start("demo", explode)
    list(runner.watch(job.id))
    finished = runner.get(job.id)

    assert finished is not None
    assert finished.state == JobState.FAILED
    assert finished.error == "ValueError: no"
    assert any("Traceback" in line for line in finished.progress)


def test_a_late_watcher_sees_the_whole_story() -> None:
    """Opening the screen after a job started must not show only its tail."""
    runner = JobRunner()
    job = runner.start("demo", lambda report: report("early") or "ok")
    while runner.get(job.id).state == JobState.RUNNING:  # type: ignore[union-attr]
        pass
    assert "early" in list(runner.watch(job.id))


def test_job_events_stream_and_end(client, tmp_path: Path) -> None:
    api, _paths, _conn = client
    job = api.post("/api/generate").json()
    with api.stream("GET", f"/api/jobs/{job['id']}/events") as stream:
        body = "".join(chunk for chunk in stream.iter_text())
    assert "event: end" in body
    assert api.get(f"/api/jobs/{job['id']}").json()["state"] == JobState.DONE


def test_an_unknown_job_is_a_404(client) -> None:
    api, _paths, _conn = client
    assert api.get("/api/jobs/nope").status_code == 404
    assert api.get("/api/jobs/nope/events").status_code == 404


def _settle(api, job_id: str) -> dict:  # type: ignore[no-untyped-def]
    with api.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        for _ in stream.iter_text():
            pass
    return dict(api.get(f"/api/jobs/{job_id}").json())


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


def test_the_port_is_chosen_on_localhost_only() -> None:
    assert HOST == "127.0.0.1", "binding anywhere routable would expose the catalog"
    assert 1 <= pick_port() <= 65535


def test_a_busy_preferred_port_does_not_stop_the_app() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind((HOST, 0))
        busy = taken.getsockname()[1]
        taken.listen(1)
        assert pick_port(busy) != busy


def test_a_missing_webview_degrades_instead_of_raising(monkeypatch) -> None:
    """Absence degrades — the same rule freecadcmd follows (docs/12 §5)."""
    import builtins

    real_import = builtins.__import__

    def no_webview(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "webview":
            raise ModuleNotFoundError("No module named 'webview'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_webview)
    with pytest.raises(WindowUnavailable, match=r"klm\[app\]"):
        run_window()


def test_the_cli_falls_back_to_serving(monkeypatch, capsys, tmp_path: Path) -> None:
    from klm.cli import main as cli

    served: dict[str, object] = {}
    monkeypatch.setattr(
        "klm.api.desktop.run_window",
        lambda *a, **k: (_ for _ in ()).throw(WindowUnavailable("no webview here")),
    )
    monkeypatch.setattr(
        "klm.api.desktop.run_server", lambda *a, **k: served.setdefault("ran", True)
    )
    monkeypatch.setenv("KLM_HOME", str(tmp_path / "home"))

    assert cli.main(["app"]) == cli.EXIT_OK
    assert served.get("ran") is True
    assert "falling back to the browser" in capsys.readouterr().out
