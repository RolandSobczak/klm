"""The localhost API the desktop shell is a client of.

Every route here is a **translation** of a function in `klm.services.*` — read
the arguments, call the service, shape the result as JSON. No route computes
anything, because the moment one does, the GUI and the CLI begin to disagree
about what klm does, and the rule that keeps them honest is:

    If the GUI can do something the CLI cannot, that is a bug in the CLI.

Two constraints follow from being a desktop application rather than a service:

* **Localhost only, and no authentication.** The server binds 127.0.0.1 and is
  started by the user's own session. Adding a login to a single-user local tool
  would be security theatre; *binding to 0.0.0.0* would be the actual mistake,
  so the host is not configurable to anything routable without saying so.
* **Long operations are jobs.** Vendoring or building a fab package takes long
  enough that a blocking request looks broken (see :mod:`klm.api.jobs`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from klm.api.jobs import JobRunner
from klm.config import load_config
from klm.kicad.project import ProjectError, find_project
from klm.model import PartStatus
from klm.services.bom import extract_bom
from klm.services.catalog import count_parts, get_part, save_part, search_parts
from klm.services.demand import parse_build_plan, plan_demand
from klm.services.generate import generate
from klm.services.lint import Selector, lint_catalog
from klm.services.orders import get_order, list_orders, pins, receive
from klm.services.preview import PreviewError, render_part
from klm.services.split import split_order
from klm.services.stock import list_stock, where
from klm.services.sync import diff_part, sync_status
from klm.services.vendor import VendorError, unvendor, vendor
from klm.services.verify import verify_clean_room
from klm.store import AssetKind, AssetStore, Paths, connect

__all__ = ["LOGO", "STATIC_DIR", "create_app", "logo_path"]

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The application icon: browser tab, window header, and the native window's
#: own icon. One file in one place — the UI, the window and the wheel all read
#: it from here rather than each carrying a copy at a different size.
LOGO = STATIC_DIR / "logo.png"


def logo_path() -> Path | None:
    """The icon, or ``None`` if it is not installed.

    Absent is a normal state, not a failure: a source checkout without the
    asset, or a stripped wheel, still runs. The window opens with the
    platform's default icon and the page falls back to a text heading.
    """
    return LOGO if LOGO.is_file() else None


def _json(value: Any) -> Any:
    """Make a service's dataclasses printable without inventing a schema.

    Deliberately structural rather than a hand-written serialiser per type: the
    services own the shape, and a second description of it here would be one
    more thing to keep in step for no benefit.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _json(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def create_app(catalog: str | Path | None = None) -> Any:
    """Build the FastAPI application over one catalog.

    Imported lazily so the core stays free of the dependency: klm's CLI must
    keep working with nothing but the standard library, and `pip install klm`
    without the `app` extra has to remain a complete installation.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, Response, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by hand
        raise RuntimeError(
            "the desktop app needs its extra dependencies: pip install 'klm[app]'"
        ) from exc

    paths = Paths.resolve(catalog)
    jobs = JobRunner()
    app = FastAPI(title="klm", version="0.1.0", docs_url="/api/docs")

    def db() -> sqlite3.Connection:
        if not paths.exists():
            raise HTTPException(404, f"no catalog at {paths.home} — run `klm init`")
        return connect(paths.db, create=False)

    def project_at(path: str):  # type: ignore[no-untyped-def]
        try:
            return find_project(path)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- health --------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        from klm.environment import probe_all

        payload: dict[str, Any] = {
            "catalog": str(paths.home),
            "initialised": paths.exists(),
            "tools": [
                {
                    "name": t.name,
                    "found": t.found,
                    "version": t.version,
                    "consequence": t.consequence,
                    "install_hint": t.install_hint,
                }
                for t in probe_all()
            ],
        }
        if paths.exists():
            conn = db()
            try:
                payload["parts"] = dict(count_parts(conn))
            finally:
                conn.close()
        return payload

    # -- catalog -------------------------------------------------------

    @app.get("/api/parts")
    def parts(status: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        conn = db()
        try:
            found = search_parts(conn, q, status=PartStatus(status) if status else None)
            return [dict(_json(p)) for p in found]
        finally:
            conn.close()

    @app.get("/api/parts/{klm_id}")
    def part(klm_id: str) -> dict[str, Any]:
        conn = db()
        try:
            found = get_part(conn, klm_id)
            if found is None:
                raise HTTPException(404, f"no part {klm_id}")
            payload: dict[str, Any] = dict(_json(found))
            payload["offers"] = [_json(o) for o in _offers(conn, klm_id)]
            payload["stock"] = [_json(s) for s in where(conn, klm_id)]
            return payload
        finally:
            conn.close()

    @app.post("/api/parts/{klm_id}/status")
    def set_status(klm_id: str, body: dict[str, str]) -> dict[str, Any]:
        """Approve or deprecate. The one write the catalog screen needs."""
        conn = db()
        try:
            found = get_part(conn, klm_id)
            if found is None:
                raise HTTPException(404, f"no part {klm_id}")
            try:
                found.status = PartStatus(body.get("status", ""))
            except ValueError as exc:
                raise HTTPException(400, f"unknown status {body.get('status')!r}") from exc
            found.updated_at = None
            return dict(_json(save_part(conn, found)))
        finally:
            conn.close()

    @app.post("/api/parts")
    def create_part(body: dict[str, Any]) -> dict[str, Any]:
        """The add-part wizard's one call — a job, because it goes to the network.

        Everything the wizard collects is a keyword of `add_part`; nothing is
        interpreted here. A blank field stays blank, and `AddReport.notes` is
        what tells the user which ones klm could not fill.
        """
        mpn = str(body.get("mpn", "")).strip()
        if not mpn:
            raise HTTPException(400, "a part needs an MPN")
        offline = bool(body.get("offline"))

        def work(report):  # type: ignore[no-untyped-def]
            from klm.services.part_add import add_part
            from klm.suppliers.registry import build_adapters

            config = load_config(paths.config)
            adapters = (
                {} if offline else build_adapters(config, paths.supplier_cache, offline=False)
            )
            report(f"looking up {mpn}" + (" (offline)" if offline else ""))
            conn = db()
            try:
                added = add_part(
                    conn,
                    AssetStore(paths.assets),
                    mpn=mpn,
                    manufacturer=str(body.get("manufacturer", "")),
                    category=body.get("category") or None,
                    package=body.get("package") or None,
                    value=str(body.get("value", "")),
                    description=str(body.get("description", "")),
                    datasheet=body.get("datasheet") or None,
                    fields=dict(body.get("fields") or {}),
                    lcsc=body.get("lcsc") or None,
                    adapters=adapters,
                )
            finally:
                conn.close()

            for note in added.notes:
                report(note)
            assets = added.assets
            if assets is not None:
                for acquired in assets.acquired:
                    report(f"{acquired.kind.value}: {acquired.origin} — {acquired.detail}")
                for kind, reason in assets.unavailable:
                    report(f"{kind}: {reason}")
            for offer in added.offers:
                report(f"offer {offer.supplier}:{offer.supplier_pn}")
            return {
                "klm_id": added.part.klm_id,
                "created": added.created,
                "ok": added.ok,
                "notes": added.notes,
                "qa": [
                    {
                        "kind": a.kind.value,
                        "status": str(a.qa.status) if a.qa else "unchecked",
                        "findings": [
                            {"check": c.check, "status": str(c.status), "detail": c.detail}
                            for c in a.qa.results
                        ]
                        if a.qa
                        else [],
                    }
                    for a in (assets.acquired if assets else [])
                ],
                "unavailable": [
                    {"kind": k, "reason": r} for k, r in (assets.unavailable if assets else [])
                ],
            }

        return jobs.start("part add", work).to_json()

    @app.get("/api/parts/{klm_id}/{kind}.svg")
    def part_preview(klm_id: str, kind: str) -> Any:
        """A drawing of the symbol or footprint, rendered without KiCad."""
        if kind not in ("symbol", "footprint"):
            raise HTTPException(404, f"cannot draw {kind!r}")
        conn = db()
        try:
            found = get_part(conn, klm_id)
            if found is None:
                raise HTTPException(404, f"no part {klm_id}")
        finally:
            conn.close()
        try:
            svg = render_part(AssetStore(paths.assets), found, AssetKind(kind))
        except PreviewError as exc:
            # 404, not 500: "this part has no footprint" is a fact about the
            # catalog, not a failure of the server, and the UI shows the reason.
            raise HTTPException(404, str(exc)) from exc
        return Response(svg, media_type="image/svg+xml")

    def _offers(conn: sqlite3.Connection, klm_id: str) -> list[Any]:
        from klm.services.offers import list_offers

        return list_offers(conn, klm_id=klm_id)

    @app.get("/api/lint")
    def lint(select: str = "") -> dict[str, Any]:
        conn = db()
        try:
            report = lint_catalog(
                conn,
                AssetStore(paths.assets),
                load_config(paths.config),
                selector=Selector(tuple(s for s in select.split(",") if s)),
            )
            return {
                "parts_checked": report.parts_checked,
                "findings": [
                    {
                        "rule": f.rule,
                        "severity": str(f.severity),
                        "message": f.message,
                        "location": f.location,
                        "fixable": f.fixable,
                    }
                    for f in report.findings
                ],
            }
        finally:
            conn.close()

    # -- projects ------------------------------------------------------

    @app.get("/api/projects")
    def projects(path: str) -> dict[str, Any]:
        found = project_at(path)
        conn = db()
        try:
            payload: dict[str, Any] = {
                "name": found.name,
                "root": str(found.root),
                "vendored": found.is_vendored,
                "schematics": [p.name for p in found.schematics],
                "board": found.board.name if found.board else None,
            }
            bom = extract_bom(conn, found)
            payload["bom"] = [_json(line) for line in bom.lines]
            payload["unresolved"] = bom.unresolved
            if found.is_vendored:
                status = sync_status(conn, found)
                payload["sync"] = [
                    {
                        "mpn": row.mpn,
                        "klm_id": row.klm_id,
                        "state": str(row.state),
                        "detail": row.detail,
                    }
                    for row in status.rows
                ]
            return payload
        finally:
            conn.close()

    @app.get("/api/projects/diff")
    def project_diff(path: str, klm_id: str) -> dict[str, Any]:
        """One vendored part's drift, side by side."""
        conn = db()
        try:
            result = diff_part(conn, AssetStore(paths.assets), project_at(path), klm_id)
        except VendorError as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            conn.close()
        return {
            "klm_id": result.row.klm_id,
            "mpn": result.row.mpn,
            "state": str(result.row.state),
            "detail": result.row.detail,
            "assets": [
                {
                    "kind": str(d.kind),
                    "side": d.side,
                    "name": d.name,
                    "changed": d.changed,
                    "note": d.note,
                    "before": d.before,
                    "after": d.after,
                    "unified": d.unified,
                }
                for d in result.diffs
            ],
        }

    @app.post("/api/projects/vendor")
    def vendor_project(body: dict[str, Any]) -> dict[str, Any]:
        found = project_at(str(body.get("path", "")))
        include_3d = bool(body.get("with_3d"))
        libraries = list(body.get("from_libraries") or [])

        def work(report):  # type: ignore[no-untyped-def]
            conn = db()
            try:
                report(f"vendoring {found.name}")
                result = vendor(
                    conn,
                    AssetStore(paths.assets),
                    found,
                    include_3d=include_3d,
                    from_libraries=libraries,
                    allow_unresolved=bool(body.get("allow_unresolved")),
                )
                report(f"{result.symbols} symbols, {result.footprints} footprints")
                for item in result.plan.external:
                    report(f"left linked: {item.lib_id}")
                return {"symbols": result.symbols, "footprints": result.footprints}
            finally:
                conn.close()

        return jobs.start("vendor", work).to_json()

    @app.post("/api/projects/unvendor")
    def unvendor_project(body: dict[str, Any]) -> dict[str, Any]:
        found = project_at(str(body.get("path", "")))
        force = bool(body.get("force"))

        def work(report):  # type: ignore[no-untyped-def]
            conn = db()
            try:
                report(f"restoring {found.name}")
                result = unvendor(conn, AssetStore(paths.assets), found, force=force)
                return {"removed": [p.name for p in result.removed]}
            finally:
                conn.close()

        return jobs.start("unvendor", work).to_json()

    @app.get("/api/projects/verify")
    def verify(path: str) -> dict[str, Any]:
        """Deliberately takes no catalog — see klm.services.verify."""
        report = verify_clean_room(project_at(path))
        return {
            "failed": report.failed,
            "checks": [
                {"name": name, "summary": summary.lstrip("~"), "status": report.status(name)}
                for name, summary in report.checks.items()
            ],
            "findings": [_json(f) for f in report.findings],
        }

    # -- ordering ------------------------------------------------------

    @app.post("/api/orders/plan")
    def order_plan(body: dict[str, Any]) -> dict[str, Any]:
        conn = db()
        try:
            builds = parse_build_plan(str(body.get("build", "")))
            demand = plan_demand(conn, builds, projects_root=str(body.get("projects", ".")))
            result = split_order(demand.orderable, load_config(paths.config), pins=pins(conn))
            return {
                "lines": [
                    {
                        "mpn": a.line.mpn,
                        "klm_id": a.line.klm_id,
                        "quantity": a.quantity,
                        "supplier": a.supplier,
                        "reason": a.reason,
                        "subtotal": a.subtotal,
                        "explain": a.line.explain(),
                        "alternatives": [
                            {"supplier": s, "delta": d, "note": n} for s, d, n in a.alternatives
                        ],
                    }
                    for a in result.assignments
                ],
                # `total` is a property, and `asdict` only sees fields — without
                # this the UI reads `undefined` for the one number the screen
                # exists to show. Still a translation, not a computation: the
                # arithmetic is the service's, this only carries it across.
                "carts": {
                    name: {**_json(cart), "total": cart.total}
                    for name, cart in result.carts.items()
                },
                "unsourced": [line.mpn for line in result.unsourced],
                "improvements": result.improvements,
                "total": result.total,
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/orders")
    def orders() -> list[dict[str, Any]]:
        conn = db()
        try:
            return [
                {
                    "id": o.id,
                    "supplier": o.supplier,
                    "state": o.state,
                    "lines": len(o.lines),
                    "estimated": o.estimated,
                    "currency": o.currency,
                }
                for o in list_orders(conn)
            ]
        finally:
            conn.close()

    @app.get("/api/orders/{order_id}")
    def order(order_id: str) -> dict[str, Any]:
        conn = db()
        try:
            found = get_order(conn, order_id)
            if found is None:
                raise HTTPException(404, f"no order {order_id}")
            return dict(_json(found))
        finally:
            conn.close()

    @app.post("/api/orders/{order_id}/receive")
    def receive_order(order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        conn = db()
        try:
            report = receive(conn, order_id, location=str(body.get("location", "unfiled")))
            return {
                "state": report.order.state,
                "stocked": [
                    {"klm_id": k, "location": loc, "quantity": q} for k, loc, q in report.stocked
                ],
                "discrepancies": report.discrepancies,
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/stock")
    def stock(location: str | None = None) -> list[dict[str, Any]]:
        conn = db()
        try:
            return [
                {
                    "klm_id": item.klm_id,
                    "mpn": item.mpn,
                    "location": item.location,
                    "quantity": item.quantity,
                    "last_counted": item.last_counted,
                }
                for item in list_stock(conn, location=location)
                if item.quantity
            ]
        finally:
            conn.close()

    # -- generation ----------------------------------------------------

    @app.post("/api/generate")
    def generate_libraries() -> dict[str, Any]:
        def work(report):  # type: ignore[no-untyped-def]
            conn = db()
            try:
                report("rebuilding generated/")
                result = generate(conn, paths)
                report(f"{result.symbols} symbols, {result.footprints} footprints")
                for klm_id, reason in result.skipped:
                    report(f"skipped {klm_id}: {reason}")
                return {"symbols": result.symbols, "changed": result.changed}
            finally:
                conn.close()

        return jobs.start("generate", work).to_json()

    # -- jobs ----------------------------------------------------------

    @app.get("/api/jobs")
    def job_list() -> list[dict[str, Any]]:
        return [job.to_json() for job in jobs.all()]

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job.to_json()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str) -> Any:
        if jobs.get(job_id) is None:
            raise HTTPException(404, f"no job {job_id}")

        def stream() -> Iterator[str]:
            for line in jobs.watch(job_id):
                yield f"data: {json.dumps({'message': line})}\n\n"
            job = jobs.get(job_id)
            yield f"event: end\ndata: {json.dumps(job.to_json() if job else {})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- the UI --------------------------------------------------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> Any:
            return FileResponse(STATIC_DIR / "index.html")

    return app
