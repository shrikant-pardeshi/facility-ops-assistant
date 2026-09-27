"""
Typed query functions. Every function returns schemas.* models (never raw
rows), so tools work with validated objects and the SQL stays in one place.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from facility_ops.data_access.db import get_connection
from facility_ops.schemas import (
    AMCContract,
    Asset,
    ComplianceCertificate,
    Facility,
    MaintenanceRecord,
    Person,
    Ticket,
    TicketCategory,
    TicketStatus,
    TicketType,
)


def _one(conn: sqlite3.Connection, model, sql: str, params: tuple):
    row = conn.execute(sql, params).fetchone()
    return model.model_validate(dict(row)) if row else None


def _many(conn: sqlite3.Connection, model, sql: str, params: tuple = ()) -> list:
    return [model.model_validate(dict(r)) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# People & facilities
# ---------------------------------------------------------------------------


def get_person(person_id: str) -> Person | None:
    with get_connection() as conn:
        return _one(conn, Person, "SELECT * FROM people WHERE person_id = ?", (person_id,))


def list_people() -> list[Person]:
    with get_connection() as conn:
        return _many(conn, Person, "SELECT * FROM people ORDER BY facility_id, role DESC, person_id")


def list_facilities() -> list[Facility]:
    with get_connection() as conn:
        return _many(conn, Facility, "SELECT * FROM facilities ORDER BY facility_id")


def get_facility(facility_id: str) -> Facility | None:
    with get_connection() as conn:
        return _one(conn, Facility, "SELECT * FROM facilities WHERE facility_id = ?", (facility_id,))


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def get_asset(asset_id: str) -> Asset | None:
    with get_connection() as conn:
        return _one(conn, Asset, "SELECT * FROM assets WHERE asset_id = ?", (asset_id,))


def list_assets(facility_id: str) -> list[Asset]:
    with get_connection() as conn:
        return _many(conn, Asset, "SELECT * FROM assets WHERE facility_id = ? ORDER BY asset_id", (facility_id,))


# Words people use for equipment -> the words used in asset names/categories.
_SYNONYMS = {
    "elevator": "lift",
    "elevators": "lift",
    "lifts": "lift",
    "generator": "dg",
    "genset": "dg",
    "diesel": "dg",
    "chillers": "chiller",
    "transformers": "transformer",
    "pumps": "pump",
    "vrv": "vrf",
    "sewage": "stp",
    "level": "l",
}
_FILLER = {"the", "a", "an", "no", "number", "unit", "set", "system", "of", "our", "my", "in", "at", "on"}


def _tokens(text: str) -> list[str]:
    text = re.sub(r"\b(?:level|l)\s+(\d+)", r"l\1", text.lower())  # "level 12" / "l 12" -> "l12"
    toks: list[str] = re.findall(r"[a-z]+\d*|\d+", text)
    return [_SYNONYMS.get(t) or t for t in toks if t not in _FILLER]


def find_assets(facility_id: str, hint: str) -> list[Asset]:
    """Resolve a free-text reference ('chiller 2', 'AHU L12', 'service lift')
    to assets in a facility.

    An asset matches when EVERY meaningful word of the hint appears in its
    name / category / id. Numbers must match exactly, so 'chiller 2' does not
    match 'Chiller 1'. Returns all matches (the caller asks the user to pick
    when there is more than one).
    """
    wanted = _tokens(hint)
    if not wanted:
        return []
    matches = []
    for asset in list_assets(facility_id):
        # Only the part of the name before the spec (" - 400 TR ...") identifies the unit.
        label = asset.name.split(" - ")[0]
        haystack = set(_tokens(f"{label} {asset.category.value.replace('_', ' ')} {asset.asset_id}"))
        if all(w in haystack for w in wanted):
            matches.append(asset)
    return matches


def get_amc_contracts(asset_id: str) -> list[AMCContract]:
    """All contracts for an asset, latest end date first."""
    with get_connection() as conn:
        return _many(
            conn, AMCContract, "SELECT * FROM amc_contracts WHERE asset_id = ? ORDER BY end_date DESC", (asset_id,)
        )


def get_maintenance_history(asset_id: str, limit: int = 5) -> list[MaintenanceRecord]:
    with get_connection() as conn:
        return _many(
            conn,
            MaintenanceRecord,
            "SELECT * FROM maintenance_history WHERE asset_id = ? ORDER BY service_date DESC, record_id DESC LIMIT ?",
            (asset_id, limit),
        )


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


def list_certificates(facility_id: str, cert_type: str | None = None) -> list[ComplianceCertificate]:
    sql = "SELECT * FROM compliance_certificates WHERE facility_id = ?"
    params: tuple = (facility_id,)
    if cert_type:
        sql += " AND cert_type = ?"
        params += (cert_type,)
    with get_connection() as conn:
        return _many(conn, ComplianceCertificate, sql + " ORDER BY expiry_date", params)


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


def get_ticket(ticket_id: str) -> Ticket | None:
    with get_connection() as conn:
        return _one(conn, Ticket, "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))


def list_tickets_for_requester(requester_id: str) -> list[Ticket]:
    """A person's own tickets: not-closed first, then most recently updated."""
    with get_connection() as conn:
        return _many(
            conn,
            Ticket,
            "SELECT * FROM tickets WHERE requester_id = ? "
            "ORDER BY (status = ?) ASC, updated_at DESC",
            (requester_id, TicketStatus.CLOSED.value),
        )


def list_unclosed_tickets_for_asset(asset_id: str) -> list[Ticket]:
    with get_connection() as conn:
        return _many(
            conn,
            Ticket,
            "SELECT * FROM tickets WHERE asset_id = ? AND status != ? ORDER BY created_at DESC",
            (asset_id, TicketStatus.CLOSED.value),
        )


def find_duplicate_tickets(
    facility_id: str,
    category: TicketCategory,
    asset_id: str | None,
    floor: str | None,
) -> list[Ticket]:
    """Tickets that are NOT closed for the same facility + category and either
    the same asset, or (when the new ticket has no asset) the same floor."""
    sql = "SELECT * FROM tickets WHERE facility_id = ? AND category = ? AND status != ?"
    params: tuple = (facility_id, category.value, TicketStatus.CLOSED.value)
    if asset_id:
        sql += " AND asset_id = ?"
        params += (asset_id,)
    elif floor:
        sql += " AND floor = ?"
        params += (floor,)
    else:
        return []  # nothing to match on - caller must have asked for floor/asset
    with get_connection() as conn:
        return _many(conn, Ticket, sql + " ORDER BY created_at DESC", params)


def _next_ticket_id(conn: sqlite3.Connection, ticket_type: TicketType, year: int) -> str:
    prefix = f"{'WO' if ticket_type is TicketType.WORK_ORDER else 'CMP'}-{year}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(ticket_id, ?) AS INTEGER)) FROM tickets WHERE ticket_id LIKE ?",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    return f"{prefix}{(row[0] or 0) + 1:04d}"


def insert_ticket(ticket_fields: dict, created_at: datetime) -> Ticket:
    """Allocate the next ID and insert in ONE transaction (no ID races).

    `ticket_fields` must contain every Ticket field except ticket_id,
    created_at, updated_at, status and source.
    """
    ticket_type = TicketType(ticket_fields["ticket_type"])
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ticket = Ticket(
            ticket_id=_next_ticket_id(conn, ticket_type, created_at.year),
            status=TicketStatus.OPEN,
            created_at=created_at,
            updated_at=created_at,
            source="assistant",
            **ticket_fields,
        )
        record = ticket.model_dump(mode="json")
        cols = list(record)
        conn.execute(
            f"INSERT INTO tickets ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(record[c] for c in cols),
        )
        return ticket
