"""The append-only event log — "why is this part like this?".

Cheap to write and never rewritten (docs/04 §5). It matters most for the
research agent: a proposal that turns into a part should be traceable to the
searches and datasheet reads that produced it, months later, when the only
remaining question is why anyone thought this part was the right one.

The agent's *tools* cannot write anything — their connection is read-only
(docs/adr/0006). This is klm writing *about* the agent, on its own connection,
which is the distinction that makes the guarantee and the audit trail coexist.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from klm.services.catalog import now
from klm.store.db import transaction

__all__ = ["Event", "history", "record"]

ACTOR_USER = "user"
ACTOR_AGENT = "agent"
ACTOR_IMPORT = "import"


@dataclass(frozen=True)
class Event:
    at: str
    actor: str
    action: str
    subject: str | None = None
    detail: dict[str, Any] | None = None


def record(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    *,
    subject: str | None = None,
    detail: Any = None,
) -> None:
    """Append one event.

    `detail` is stored as JSON and is best-effort: a value that will not
    serialise is stored as its `repr` rather than failing the write. Losing the
    detail of an event is bad; losing the event — or failing the operation that
    was being logged — is worse.
    """
    payload = None
    if detail is not None:
        payload = json.dumps(detail, sort_keys=True, ensure_ascii=False, default=repr)
    with transaction(conn):
        conn.execute(
            "INSERT INTO event_log (at, actor, action, subject, detail) VALUES (?, ?, ?, ?, ?)",
            (now(), actor, action, subject, payload),
        )


def history(
    conn: sqlite3.Connection,
    *,
    subject: str | None = None,
    actor: str | None = None,
    limit: int = 100,
) -> list[Event]:
    """Events, newest first."""
    clauses: list[str] = []
    params: list[object] = []
    if subject is not None:
        clauses.append("subject = ?")
        params.append(subject)
    if actor is not None:
        clauses.append("actor = ?")
        params.append(actor)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT at, actor, action, subject, detail FROM event_log{where} "
        "ORDER BY id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [
        Event(
            at=str(row["at"]),
            actor=str(row["actor"]),
            action=str(row["action"]),
            subject=row["subject"],
            detail=_detail(row["detail"]),
        )
        for row in rows
    ]


def _detail(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
