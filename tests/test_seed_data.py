"""Integrity checks for the sample data. Run before every demo:  pytest -q

These catch the mistakes that make a demo fail silently: a typo'd asset_id in
a maintenance record, a ticket pointing at a person who does not exist, a KB
file with broken front-matter, a duplicate ID.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import frontmatter
import pytest

from facility_ops.schemas import (
    ID_PATTERNS,
    AMCContract,
    Asset,
    ComplianceCertificate,
    Facility,
    MaintenanceRecord,
    Person,
    Ticket,
)

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
KB = ROOT / "data" / "knowledge_base"


def _csv(name: str) -> list[dict]:
    with open(SEED / name, newline="", encoding="utf-8") as f:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]


@pytest.fixture(scope="module")
def data():
    return {
        "facilities": [Facility(**r) for r in _csv("facilities.csv")],
        "people": [Person(**r) for r in _csv("people.csv")],
        "assets": [Asset(**r) for r in _csv("assets.csv")],
        "amc": [AMCContract(**r) for r in _csv("amc_contracts.csv")],
        "history": [MaintenanceRecord(**r) for r in _csv("maintenance_history.csv")],
        "certs": [ComplianceCertificate(**r) for r in _csv("compliance_certificates.csv")],
        "tickets": [Ticket(**r) for r in json.loads((SEED / "tickets.json").read_text("utf-8"))],
    }


def _unique(items, attr):
    ids = [getattr(i, attr) for i in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate {attr}: {dupes}"
    return set(ids)


def test_ids_unique_and_well_formed(data):
    facilities = _unique(data["facilities"], "facility_id")
    people = _unique(data["people"], "person_id")
    assets = _unique(data["assets"], "asset_id")
    _unique(data["amc"], "amc_id")
    _unique(data["history"], "record_id")
    certs = _unique(data["certs"], "cert_id")
    tickets = _unique(data["tickets"], "ticket_id")
    for ids, key in [
        (facilities, "facility_id"),
        (people, "requester_id"),
        (assets, "asset_id"),
        (certs, "cert_id"),
        (tickets, "ticket_id"),
    ]:
        bad = [i for i in ids if not ID_PATTERNS[key].match(i)]
        assert not bad, f"malformed {key}: {bad}"


def test_foreign_keys(data):
    fac = {f.facility_id for f in data["facilities"]}
    ppl = {p.person_id: p for p in data["people"]}
    ast = {a.asset_id: a for a in data["assets"]}

    assert all(f.fm_manager_id in ppl for f in data["facilities"])
    assert all(p.facility_id in fac for p in data["people"])
    assert all(a.facility_id in fac for a in data["assets"])
    assert all(c.asset_id in ast for c in data["amc"])
    assert all(h.asset_id in ast for h in data["history"])
    for c in data["certs"]:
        assert c.facility_id in fac
        if c.asset_id:
            assert ast[c.asset_id].facility_id == c.facility_id, c.cert_id
    for t in data["tickets"]:
        assert t.facility_id in fac, t.ticket_id
        assert t.requester_id in ppl, t.ticket_id
        if t.asset_id:
            assert ast[t.asset_id].facility_id == t.facility_id, t.ticket_id


def test_ticket_type_matches_requester_role(data):
    role = {p.person_id: p.role for p in data["people"]}
    for t in data["tickets"]:
        expected = "facility_staff" if t.ticket_type.value == "work_order" else "occupant"
        assert role[t.requester_id] == expected, t.ticket_id


def test_date_sanity(data):
    for c in data["amc"]:
        assert c.start_date < c.end_date, c.amc_id
    for c in data["certs"]:
        assert c.issue_date is None or c.issue_date < c.expiry_date, c.cert_id
    for t in data["tickets"]:
        assert t.created_at <= t.updated_at, t.ticket_id


def test_knowledge_base_front_matter(data):
    files = sorted(KB.rglob("*.md"))
    assert len(files) >= 10
    doc_ids = []
    for f in files:
        post = frontmatter.load(f)
        for key in ("doc_id", "doc_type", "title", "tags"):
            assert key in post.metadata, f"{f.name}: missing {key}"
        assert post.metadata["doc_type"] in ("sop", "compliance"), f.name
        assert f.name.startswith(str(post.metadata["doc_id"])), f"{f.name}: file name / doc_id mismatch"
        assert post.content.count("\n## ") >= 3, f"{f.name}: needs >= 3 '##' sections for chunking"
        if post.metadata["doc_type"] == "compliance":
            assert "not legal text" in post.content, f"{f.name}: missing disclaimer"
        doc_ids.append(post.metadata["doc_id"])
    assert len(doc_ids) == len(set(doc_ids))
    # the .txt / .docx / .pdf documents count too (see test_knowledge_formats.py)
    from facility_ops.data_access.knowledge_base import load_documents

    all_ids = {d.doc_id for d in load_documents(KB)}
    assert set(doc_ids) <= all_ids
    # every certificate's kb_reference must resolve to a KB document
    missing = {c.kb_reference for c in data["certs"] if c.kb_reference} - all_ids
    assert not missing, f"certificates reference unknown KB docs: {missing}"
