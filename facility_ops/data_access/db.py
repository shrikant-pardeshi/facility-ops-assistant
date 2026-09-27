"""
SQLite connection management and first-run seeding.

- The database file is generated from data/seed/ (the source of truth) the
  first time the app runs, or on `python scripts/init_db.py --reset`.
- Every seed row is validated through its Pydantic model before insert, so a
  bad seed file fails loudly with the file name and row number.
- Connections are short-lived (one per operation). That keeps things
  thread-safe under Streamlit without a connection pool.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from facility_ops.config import get_settings
from facility_ops.logging_config import get_logger
from facility_ops.schemas import (
    AMCContract,
    Asset,
    ComplianceCertificate,
    Facility,
    MaintenanceRecord,
    Person,
    Ticket,
)

log = get_logger("db")


class DataUnavailableError(RuntimeError):
    """A required data file or table is missing or unreadable."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facilities (
    facility_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    address              TEXT NOT NULL,
    city                 TEXT NOT NULL,
    state                TEXT NOT NULL,
    occupancy_type       TEXT NOT NULL,
    floors_above_ground  INTEGER NOT NULL,
    building_height_m    REAL NOT NULL,
    built_up_area_sqm    REAL NOT NULL,
    fm_manager_id        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    person_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('facility_staff', 'occupant')),
    designation   TEXT NOT NULL,
    organisation  TEXT NOT NULL,
    facility_id   TEXT NOT NULL REFERENCES facilities(facility_id),
    floor         TEXT,
    email         TEXT,
    phone         TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    asset_id            TEXT PRIMARY KEY,
    facility_id         TEXT NOT NULL REFERENCES facilities(facility_id),
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,
    make                TEXT NOT NULL,
    model               TEXT NOT NULL,
    serial_no           TEXT NOT NULL,
    location            TEXT NOT NULL,
    install_date        TEXT NOT NULL,
    warranty_expiry     TEXT,
    criticality         TEXT NOT NULL,
    operational_status  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amc_contracts (
    amc_id           TEXT PRIMARY KEY,
    asset_id         TEXT NOT NULL REFERENCES assets(asset_id),
    vendor           TEXT NOT NULL,
    contract_type    TEXT NOT NULL,
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    visit_frequency  TEXT NOT NULL,
    vendor_contact   TEXT
);
CREATE TABLE IF NOT EXISTS maintenance_history (
    record_id         TEXT PRIMARY KEY,
    asset_id          TEXT NOT NULL REFERENCES assets(asset_id),
    service_date      TEXT NOT NULL,
    maintenance_type  TEXT NOT NULL,
    description       TEXT NOT NULL,
    performed_by      TEXT NOT NULL,
    downtime_hours    REAL NOT NULL DEFAULT 0,
    next_due_date     TEXT
);
CREATE TABLE IF NOT EXISTS compliance_certificates (
    cert_id            TEXT PRIMARY KEY,
    facility_id        TEXT NOT NULL REFERENCES facilities(facility_id),
    asset_id           TEXT REFERENCES assets(asset_id),
    cert_type          TEXT NOT NULL,
    title              TEXT NOT NULL,
    issuing_authority  TEXT NOT NULL,
    reference_no       TEXT,
    issue_date         TEXT,
    expiry_date        TEXT NOT NULL,
    renewal_lead_days  INTEGER NOT NULL DEFAULT 30,
    kb_reference       TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id     TEXT PRIMARY KEY,
    ticket_type   TEXT NOT NULL,
    facility_id   TEXT NOT NULL REFERENCES facilities(facility_id),
    requester_id  TEXT NOT NULL REFERENCES people(person_id),
    asset_id      TEXT REFERENCES assets(asset_id),
    location      TEXT NOT NULL,
    floor         TEXT,
    category      TEXT NOT NULL,
    priority      TEXT NOT NULL,
    description   TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'seed'
);
CREATE INDEX IF NOT EXISTS ix_assets_facility   ON assets(facility_id);
CREATE INDEX IF NOT EXISTS ix_amc_asset         ON amc_contracts(asset_id);
CREATE INDEX IF NOT EXISTS ix_history_asset     ON maintenance_history(asset_id, service_date);
CREATE INDEX IF NOT EXISTS ix_certs_facility    ON compliance_certificates(facility_id);
CREATE INDEX IF NOT EXISTS ix_tickets_match     ON tickets(facility_id, category, status);
"""

# table -> (seed file, Pydantic model). Order respects foreign keys.
SEED_PLAN: list[tuple[str, str, type[BaseModel]]] = [
    ("facilities", "facilities.csv", Facility),
    ("people", "people.csv", Person),
    ("assets", "assets.csv", Asset),
    ("amc_contracts", "amc_contracts.csv", AMCContract),
    ("maintenance_history", "maintenance_history.csv", MaintenanceRecord),
    ("compliance_certificates", "compliance_certificates.csv", ComplianceCertificate),
    ("tickets", "tickets.json", Ticket),
]
REQUIRED_TABLES = {t for t, _, _ in SEED_PLAN}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    ensure_database()
    conn = _connect(get_settings().db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_seed_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise DataUnavailableError(f"Seed file not found: {path}")
    try:
        if path.suffix == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise DataUnavailableError(f"{path.name}: expected a JSON list of records")
            return rows
        with open(path, newline="", encoding="utf-8") as f:
            # empty CSV cells -> None so optional fields validate
            return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise DataUnavailableError(f"Could not read {path.name}: {exc}") from exc


def _validated(rows: list[dict], model: type[BaseModel], file_name: str) -> list[dict]:
    out = []
    for i, row in enumerate(rows, start=1):
        try:
            out.append(model.model_validate(row).model_dump(mode="json"))
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise DataUnavailableError(f"{file_name} row {i}: invalid '{field}' - {first['msg']}") from exc
    return out


def _insert(conn: sqlite3.Connection, table: str, records: list[dict]) -> None:
    if not records:
        return
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
    placeholders = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [tuple(r.get(c) for c in cols) for r in records],
    )


def build_database(reset: bool = False) -> Path:
    """Create the schema and load every seed file. Returns the DB path."""
    s = get_settings()
    db_path = s.db_path
    if reset and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Validate everything BEFORE touching the DB, so a bad seed leaves no half-built file.
    prepared = []
    for table, file_name, model in SEED_PLAN:
        rows = _read_seed_rows(s.seed_dir / file_name)
        prepared.append((table, _validated(rows, model, file_name)))

    tmp_path = db_path.with_suffix(".building")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = _connect(tmp_path)
    try:
        conn.executescript(SCHEMA_SQL)
        for table, records in prepared:
            _insert(conn, table, records)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise DataUnavailableError(f"Failed to build database: {exc}") from exc
    conn.close()
    tmp_path.replace(db_path)
    log.info("Database built at %s (%s)", db_path, ", ".join(f"{t}={len(r)}" for t, r in prepared))
    return db_path


def _database_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        return REQUIRED_TABLES <= tables
    except sqlite3.DatabaseError:
        return False


_verified_path: Path | None = None


def ensure_database() -> None:
    """Build the DB from seed files if it is missing or incomplete.

    The check runs once per DB path per process; later calls are free.
    """
    global _verified_path
    path = get_settings().db_path
    if _verified_path == path and path.exists():
        return
    if not _database_ok(path):
        log.warning("Database missing or incomplete at %s - building from seed files", path)
        build_database(reset=True)
    _verified_path = path
