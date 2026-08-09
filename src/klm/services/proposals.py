"""The review queue — where the agent's candidates wait for a human.

The path from a proposal to a usable part has two gates and no way round
either (docs/11 §7):

```
agent proposes ──▶ review queue ──▶ human approves ──▶ draft part ──▶ QA + lint ──▶ approved
```

A proposal is a *claim with its evidence attached*: parameters carry the page
and quote they came from, constraint checks say which ones fail, offers say
what it costs, and `concerns` says what is wrong with it. A part is something
klm is willing to put on a board. Keeping them in different tables is what
stops the second from quietly becoming a copy of the first.

Approving does not create an approved part — it runs the phase-3 pipeline and
produces a **draft**, which still has to pass the asset QA gate and lint. There
is no path from agent output to a usable part that skips a human *and* the
mechanical checks.

A rejection carries a reason, and the reason is stored. That log is the raw
material for improving the requirement schema and the system prompt: the
failures say what the interface is missing, and they are worth more than the
approvals.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from klm.assets.kicad_libs import KicadLibraries
from klm.model import PartStatus
from klm.services.catalog import now
from klm.services.events import ACTOR_AGENT, ACTOR_USER, record
from klm.services.part_add import AddReport, add_part
from klm.store.assets import AssetStore
from klm.store.db import transaction
from klm.suppliers.base import SupplierAdapter

__all__ = [
    "CitedParameter",
    "ConstraintCheck",
    "Proposal",
    "ProposalError",
    "ProposedOffer",
    "approve",
    "get",
    "list_proposals",
    "reject",
    "save",
]

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"


class ProposalError(Exception):
    """The proposal cannot be stored, or cannot be acted on."""


@dataclass(frozen=True)
class CitedParameter:
    """A parameter and where in the datasheet it is written."""

    name: str
    value: str
    page: int | None = None
    quote: str = ""


@dataclass(frozen=True)
class ConstraintCheck:
    """One requirement constraint, as the agent found it.

    `status` is deliberately allowed to be `FAIL`: a near miss that is
    interesting for a stated reason is more useful than silence, provided the
    miss is unmissable (docs/11 §6).
    """

    name: str
    required: str
    actual: str
    status: str


@dataclass(frozen=True)
class ProposedOffer:
    supplier: str
    supplier_pn: str
    stock: int | None = None
    unit_price: float | None = None
    currency: str | None = None


@dataclass
class Proposal:
    """One candidate, with its evidence."""

    mpn: str
    manufacturer: str
    why: str = ""
    package: str | None = None
    category: str | None = None
    description: str = ""
    datasheet_url: str | None = None
    requirement: str | None = None
    rank: int | None = None
    parameters: list[CitedParameter] = field(default_factory=list)
    checks: list[ConstraintCheck] = field(default_factory=list)
    offers: list[ProposedOffer] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """What klm dropped or could not verify when the proposal was made."""

    id: int | None = None
    at: str | None = None
    state: str = STATE_PENDING
    decided_at: str | None = None
    reason: str | None = None
    klm_id: str | None = None

    @property
    def failing(self) -> list[ConstraintCheck]:
        return [check for check in self.checks if check.status.upper() == "FAIL"]

    def summary(self) -> str:
        where = ", ".join(f"{o.supplier}:{o.supplier_pn}" for o in self.offers) or "no offer"
        failing = f", {len(self.failing)} failing" if self.failing else ""
        return f"{self.mpn} ({self.manufacturer}) — {where}{failing}"


def save(conn: sqlite3.Connection, proposal: Proposal) -> Proposal:
    """Put a proposal in the queue."""
    if not proposal.mpn.strip():
        raise ProposalError("a proposal needs an MPN")
    if not proposal.manufacturer.strip():
        raise ProposalError(f"{proposal.mpn}: a proposal needs a manufacturer")

    detail = json.dumps(
        {
            "parameters": [asdict(p) for p in proposal.parameters],
            "checks": [asdict(c) for c in proposal.checks],
            "offers": [asdict(o) for o in proposal.offers],
            "concerns": list(proposal.concerns),
            "notes": list(proposal.notes),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    stamp = proposal.at or now()
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO proposal (at, state, requirement, rank, mpn, manufacturer, package,
                                  category, description, datasheet_url, why, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stamp,
                STATE_PENDING,
                proposal.requirement,
                proposal.rank,
                proposal.mpn.strip(),
                proposal.manufacturer.strip(),
                proposal.package,
                proposal.category,
                proposal.description,
                proposal.datasheet_url,
                proposal.why,
                detail,
            ),
        )
    proposal.id = int(cursor.lastrowid or 0)
    proposal.at = stamp
    record(
        conn,
        ACTOR_AGENT,
        "proposal.created",
        subject=str(proposal.id),
        detail={"mpn": proposal.mpn, "manufacturer": proposal.manufacturer},
    )
    return proposal


def get(conn: sqlite3.Connection, proposal_id: int) -> Proposal | None:
    row = conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
    return _row(row) if row else None


def list_proposals(
    conn: sqlite3.Connection, *, state: str | None = STATE_PENDING, limit: int = 200
) -> list[Proposal]:
    """The queue, newest first. `state=None` for everything."""
    where = " WHERE state = ?" if state else ""
    params: list[Any] = [state] if state else []
    rows = conn.execute(
        f"SELECT * FROM proposal{where} ORDER BY id DESC LIMIT ?", [*params, limit]
    ).fetchall()
    return [_row(row) for row in rows]


def reject(conn: sqlite3.Connection, proposal_id: int, reason: str) -> Proposal:
    """Reject a proposal, with the reason.

    The reason is required. A rejection log that says only "no" teaches
    nothing, and this log is the main evidence for what the requirement schema
    and the prompt are missing.
    """
    proposal = _pending(conn, proposal_id)
    if not reason.strip():
        raise ProposalError("a rejection needs a reason — it is what the log is for")

    stamp = now()
    with transaction(conn):
        conn.execute(
            "UPDATE proposal SET state = ?, decided_at = ?, reason = ? WHERE id = ?",
            (STATE_REJECTED, stamp, reason.strip(), proposal_id),
        )
    record(
        conn,
        ACTOR_USER,
        "proposal.rejected",
        subject=str(proposal_id),
        detail={"mpn": proposal.mpn, "reason": reason.strip()},
    )
    proposal.state, proposal.decided_at, proposal.reason = STATE_REJECTED, stamp, reason.strip()
    return proposal


def approve(
    conn: sqlite3.Connection,
    store: AssetStore,
    proposal_id: int,
    *,
    adapters: dict[str, SupplierAdapter] | None = None,
    libraries: KicadLibraries | None = None,
) -> tuple[Proposal, AddReport]:
    """Turn a proposal into a **draft** part.

    Draft, not approved: what comes out of this still has to pass the asset QA
    gate and field lint before anyone can put it on a board. Approving a
    proposal is a human saying "this is worth having", not "this is correct".
    """
    proposal = _pending(conn, proposal_id)

    report = add_part(
        conn,
        store,
        mpn=proposal.mpn,
        manufacturer=proposal.manufacturer,
        category=proposal.category,
        package=proposal.package,
        description=proposal.description,
        datasheet=proposal.datasheet_url,
        adapters=adapters,
        libraries=libraries,
        status=PartStatus.DRAFT,
    )

    stamp = now()
    with transaction(conn):
        conn.execute(
            "UPDATE proposal SET state = ?, decided_at = ?, klm_id = ? WHERE id = ?",
            (STATE_APPROVED, stamp, report.part.klm_id, proposal_id),
        )
    record(
        conn,
        ACTOR_USER,
        "proposal.approved",
        subject=str(proposal_id),
        detail={"mpn": proposal.mpn, "klm_id": report.part.klm_id},
    )
    proposal.state, proposal.decided_at, proposal.klm_id = (
        STATE_APPROVED,
        stamp,
        report.part.klm_id,
    )
    return proposal, report


def _pending(conn: sqlite3.Connection, proposal_id: int) -> Proposal:
    proposal = get(conn, proposal_id)
    if proposal is None:
        raise ProposalError(f"no proposal {proposal_id}")
    if proposal.state != STATE_PENDING:
        raise ProposalError(f"proposal {proposal_id} was already {proposal.state}")
    return proposal


def _row(row: sqlite3.Row) -> Proposal:
    detail = json.loads(row["detail"] or "{}")
    return Proposal(
        id=int(row["id"]),
        at=str(row["at"]),
        state=str(row["state"]),
        requirement=row["requirement"],
        rank=row["rank"],
        mpn=str(row["mpn"]),
        manufacturer=str(row["manufacturer"]),
        package=row["package"],
        category=row["category"],
        description=str(row["description"] or ""),
        datasheet_url=row["datasheet_url"],
        why=str(row["why"] or ""),
        parameters=[CitedParameter(**item) for item in detail.get("parameters", [])],
        checks=[ConstraintCheck(**item) for item in detail.get("checks", [])],
        offers=[ProposedOffer(**item) for item in detail.get("offers", [])],
        concerns=list(detail.get("concerns", [])),
        notes=list(detail.get("notes", [])),
        decided_at=row["decided_at"],
        reason=row["reason"],
        klm_id=row["klm_id"],
    )
