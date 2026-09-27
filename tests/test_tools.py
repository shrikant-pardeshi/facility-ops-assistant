"""Tool tests against the sample data (AS_OF_DATE = 2026-10-01).

Each test maps to a demo storyline in data/README.md, plus the access rules
(option B) and failure handling.
"""

from __future__ import annotations

import json

import pytest

from facility_ops.schemas import (
    AssetLookupInput,
    CertificateType,
    Priority,
    TicketType,
    ComplianceStatusInput,
    KnowledgeSearchInput,
    TicketCategory,
    ToolStatus,
    WorkOrderDraft,
)
from facility_ops.tools.asset_lookup import lookup_asset
from facility_ops.tools.compliance_status import check_compliance
from facility_ops.tools.knowledge_search import search_knowledge_base
from facility_ops.tools.work_order import commit_work_order, derive_priority, prepare_work_order

pytestmark = pytest.mark.usefixtures("fresh_db")

STAFF = "EMP-1024"  # MEP Supervisor, FAC-001
STAFF_FAC2 = "EMP-1101"  # Facility Manager, FAC-002
OCCUPANT = "OCC-2001"  # Nimbus Analytics, FAC-001, L5


# ---------------------------------------------------------------- Tool 1 ----


@pytest.mark.parametrize(
    "query, doc_id",
    [
        ("How do I reset a chiller trip alarm?", "SOP-HVAC-002"),
        ("fire NOC renewal documents", "REG-FIRE-001"),
        ("when is Form B due", "REG-FIRE-002"),
        ("someone is stuck in the lift", "SOP-LIFT-001"),
        ("jockey pump keeps starting", "SOP-FIRE-002"),
        ("lift licence inspection rules", "REG-LIFT-001"),
        # documents in the other supported formats
        ("my access card is not working", "SOP-SEC-001"),  # .txt
        ("meeting room was not cleaned", "SOP-HK-001"),  # .docx
        ("how often do we need a fire drill", "REG-FIRE-003"),  # .pdf
    ],
)
def test_kb_finds_the_right_document(query, doc_id):
    r = search_knowledge_base(KnowledgeSearchInput(query=query))
    assert r.status is ToolStatus.SUCCESS
    assert r.citations[0].source_id == doc_id


def test_kb_off_topic_is_not_found():
    r = search_knowledge_base(KnowledgeSearchInput(query="what is the visitor parking policy"))
    assert r.status is ToolStatus.NOT_FOUND
    assert r.citations == []


def test_kb_compliance_citations_are_labelled():
    r = search_knowledge_base(KnowledgeSearchInput(query="lift licence renewal", doc_type="compliance"))
    assert {c.source_type.value for c in r.citations} == {"compliance_clause"}


# ---------------------------------------------------------------- Tool 2 ----


def test_asset_card_chiller_1():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_id="AST-CHL-001"))
    assert r.status is ToolStatus.SUCCESS
    d = r.data
    assert d["warranty_state"] == "expired"
    assert d["amc"]["amc_id"] == "AMC-001" and d["amc_state"] == "active"
    assert d["amc"]["contract_type"] == "comprehensive"
    assert d["last_service"]["service_date"] == "2026-09-08"
    assert d["open_tickets"] == []  # WO-2026-0021 is closed


def test_asset_ahu_l12_expired_amc_and_on_hold_ticket():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_hint="AHU L12"))
    assert r.data["asset"]["asset_id"] == "AST-AHU-012"
    assert r.data["amc_state"] == "expired"
    assert [t["ticket_id"] for t in r.data["open_tickets"]] == ["WO-2026-0027"]


def test_asset_in_warranty_without_amc_and_loose_id_format():
    r = lookup_asset(AssetLookupInput(requester_id="emp-1024", asset_id="ast ahu 009"))
    assert r.status is ToolStatus.SUCCESS
    assert r.data["warranty_state"] == "active"
    assert r.data["amc_state"] == "none"


def test_asset_hint_resolves_by_name_and_amc_expiring():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_hint="chiller 2"))
    assert r.data["asset"]["asset_id"] == "AST-CHL-002"
    assert r.data["amc_state"] == "expiring_soon"


def test_ambiguous_hint_asks_user_to_choose():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_hint="fire pump"))
    assert r.status is ToolStatus.NEEDS_INPUT
    assert len(r.data["candidates"]) == 3
    assert "AST-FPP-003" in (r.follow_up_question or "")


def test_unknown_asset_is_not_found():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_id="AST-CHL-009"))
    assert r.status is ToolStatus.NOT_FOUND


def test_ticket_status_lookup():
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, ticket_id="WO-2026-0029"))
    assert r.status is ToolStatus.SUCCESS
    assert r.data["ticket"]["status"] == "in_progress"


def test_missing_identity_asks_for_it():
    r = lookup_asset(AssetLookupInput(requester_id="", asset_id="AST-CHL-001"))
    assert r.status is ToolStatus.NEEDS_INPUT and r.data["missing_fields"] == ["requester_id"]
    r = lookup_asset(AssetLookupInput(requester_id="EMP-9999", asset_id="AST-CHL-001"))
    assert r.status is ToolStatus.NEEDS_INPUT and "EMP-9999" in (r.follow_up_question or "")


# -------------------------------------------------------- Access (option B) --


def test_occupant_cannot_see_asset_details():
    r = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, asset_id="AST-CHL-001"))
    assert r.status is ToolStatus.NOT_PERMITTED
    assert r.data == {} and "raise a complaint" in r.summary


def test_occupant_can_see_own_ticket_only():
    own = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, ticket_id="CMP-2026-0017"))
    assert own.status is ToolStatus.SUCCESS
    other = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, ticket_id="CMP-2026-0014"))
    assert other.status is ToolStatus.NOT_PERMITTED


def test_occupant_cannot_see_compliance():
    r = check_compliance(ComplianceStatusInput(requester_id=OCCUPANT))
    assert r.status is ToolStatus.NOT_PERMITTED


def test_staff_can_work_at_any_facility():
    # FM staff cover the whole portfolio (EMP-1024 is registered at FAC-001).
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF, facility_id="FAC-002"))
    assert r.status is ToolStatus.SUCCESS
    assert r.data["facility"]["facility_id"] == "FAC-002"
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_id="AST-LFT-101"))
    assert r.status is ToolStatus.SUCCESS
    r = lookup_asset(AssetLookupInput(requester_id=STAFF, asset_hint="lift b", facility_id="FAC-002"))
    assert r.status is ToolStatus.SUCCESS and r.data["asset"]["asset_id"] == "AST-LFT-102"


def test_staff_work_order_at_another_facility():
    r = prepare_work_order(
        WorkOrderDraft(
            requester_id=STAFF,
            facility_id="FAC-002",
            asset_hint="Lift B",
            issue_description="Lift B door not closing properly",
        )
    )
    assert r.status is ToolStatus.NEEDS_CONFIRMATION, r.summary
    draft = r.data["draft"]
    assert draft["facility_id"] == "FAC-002" and draft["asset_id"] == "AST-LFT-102"


def test_unknown_facility_is_not_found():
    r = prepare_work_order(
        WorkOrderDraft(requester_id=STAFF, facility_id="FAC-009", location="L2", issue_description="leak")
    )
    assert r.status is ToolStatus.NOT_FOUND


def test_occupant_scoped_to_own_building():
    r = prepare_work_order(
        WorkOrderDraft(
            requester_id=OCCUPANT, facility_id="FAC-002", location="L3 pantry", issue_description="tap leaking"
        )
    )
    assert r.status is ToolStatus.NOT_PERMITTED
    assert "registered at FAC-001" in r.summary


# ---------------------------------------------------------------- Tool 4 ----


def test_compliance_counts_fac_001():
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF))
    assert r.status is ToolStatus.SUCCESS
    assert r.data["counts"] == {"valid": 4, "upcoming": 2, "due": 3, "overdue": 2}
    # most severe first
    assert r.data["items"][0]["state"] == "overdue"


def test_compliance_overdue_filter_links_open_ticket():
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF, status_filter="overdue"))
    items = {i["certificate"]["cert_id"]: i for i in r.data["items"]}
    assert set(items) == {"CERT-002", "CERT-006"}
    assert items["CERT-006"]["related_open_tickets"] == ["WO-2026-0012"]
    assert items["CERT-006"]["days_to_expiry"] == -31


def test_compliance_uses_renewal_lead_days():
    # CERT-010 (pollution consent) expires in 24 days with a 60-day lead -> due
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF, cert_type=CertificateType.POLLUTION_CTO))
    item = r.data["items"][0]
    assert item["state"] == "due" and item["renewal_should_start_on"] == "2026-08-26"


def test_compliance_fac_002():
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF_FAC2))
    assert r.data["counts"] == {"valid": 3, "upcoming": 2, "due": 0, "overdue": 1}


# ---------------------------------------------------------------- Tool 3 ----


def _l5_ac_complaint(**overrides) -> WorkOrderDraft:
    fields = {
        "requester_id": OCCUPANT,
        "location": "L5, east wing conference room",
        "issue_description": "AC not cooling in the conference room",
        "issue_category": TicketCategory.HVAC,
        **overrides,
    }
    return WorkOrderDraft.model_validate(fields)


def test_complaint_flow_prepare_then_commit_then_duplicate():
    prepared = prepare_work_order(_l5_ac_complaint())
    assert prepared.status is ToolStatus.NEEDS_CONFIRMATION
    draft = WorkOrderDraft.model_validate(prepared.data["draft"])
    assert draft.ticket_type is TicketType.COMPLAINT
    assert draft.facility_id == "FAC-001" and draft.floor == "L5" and draft.priority is Priority.P3
    assert "yes" in (prepared.follow_up_question or "")

    created = commit_work_order(draft)
    assert created.status is ToolStatus.SUCCESS
    assert created.data["created"]["ticket_id"] == "CMP-2026-0018"
    assert created.data["created"]["status"] == "open"

    again = prepare_work_order(_l5_ac_complaint(issue_description="Still hot in L5 meeting room"))
    assert again.status is ToolStatus.DUPLICATE
    assert again.data["duplicates"][0]["ticket_id"] == "CMP-2026-0018"


def test_staff_lift_ticket_is_duplicate_of_in_progress_wo():
    r = prepare_work_order(
        WorkOrderDraft(
            requester_id="EMP-1052",
            asset_hint="lift 2",
            issue_description="Lift 2 stopping intermittently at L7",
        )
    )
    assert r.status is ToolStatus.DUPLICATE
    assert r.data["duplicates"][0]["ticket_id"] == "WO-2026-0031"
    assert r.data["draft"]["issue_category"] == "lift"  # derived from the asset


def test_occupant_duplicate_on_same_floor():
    r = prepare_work_order(
        WorkOrderDraft(
            requester_id="OCC-2003",
            location="L12 studio",
            issue_description="Too warm",
            issue_category=TicketCategory.HVAC,
        )
    )
    assert r.status is ToolStatus.DUPLICATE and r.data["duplicates"][0]["ticket_id"] == "CMP-2026-0014"


def test_resolved_ticket_still_blocks_but_closed_does_not():
    resolved = prepare_work_order(
        WorkOrderDraft(requester_id="EMP-1045", asset_id="AST-DGS-002", issue_description="Battery alarm again")
    )
    assert resolved.status is ToolStatus.DUPLICATE  # WO-2026-0034 is 'resolved'

    closed = prepare_work_order(
        WorkOrderDraft(requester_id="EMP-1031", asset_id="AST-CHL-001", issue_description="Chiller 1 tripped again")
    )
    assert closed.status is ToolStatus.NEEDS_CONFIRMATION  # WO-2026-0021 is 'closed'
    assert closed.data["draft"]["priority"] == "P2"  # high-criticality asset


def test_missing_fields_are_listed():
    r = prepare_work_order(WorkOrderDraft(requester_id=OCCUPANT, issue_description="Tap leaking"))
    assert r.status is ToolStatus.NEEDS_INPUT
    assert r.data["missing_fields"] == ["location", "issue_category"]


def test_location_without_floor_asks_for_floor():
    r = prepare_work_order(_l5_ac_complaint(location="the big conference room"))
    assert r.status is ToolStatus.NEEDS_INPUT and r.data["missing_fields"] == ["floor"]


def test_other_facility_is_not_permitted():
    r = prepare_work_order(_l5_ac_complaint(facility_id="FAC-002"))
    assert r.status is ToolStatus.NOT_PERMITTED


def test_occupant_asset_id_is_ignored():
    r = prepare_work_order(_l5_ac_complaint(asset_id="AST-AHU-005"))
    assert r.status is ToolStatus.NEEDS_CONFIRMATION
    assert r.data["draft"]["asset_id"] is None


def test_commit_revalidates_duplicates():
    draft = WorkOrderDraft.model_validate(prepare_work_order(_l5_ac_complaint()).data["draft"])
    assert commit_work_order(draft).status is ToolStatus.SUCCESS
    assert commit_work_order(draft).status is ToolStatus.DUPLICATE  # double "yes" creates nothing


@pytest.mark.parametrize(
    "category, text, criticality, expected",
    [
        (TicketCategory.LIFT, "Two people trapped in lift 3", None, "P1"),
        (TicketCategory.ELECTRICAL, "Sparking from DB in pantry", None, "P1"),
        (TicketCategory.LIFT, "Lift 2 door not closing", None, "P2"),
        (TicketCategory.HVAC, "No cooling on the whole floor", None, "P2"),
        (TicketCategory.HVAC, "Chiller tripped", "high", "P2"),
        (TicketCategory.HVAC, "AC not cooling in cabin", None, "P3"),
        (TicketCategory.HOUSEKEEPING, "Meeting room not cleaned", None, "P4"),
    ],
)
def test_priority_rules(category, text, criticality, expected):
    from facility_ops.schemas import Criticality

    crit = Criticality(criticality) if criticality else None
    assert derive_priority(category, text, crit)[0].value == expected


# ------------------------------------------------------- Failure handling ---


def test_missing_database_seed_becomes_error_result(monkeypatch, tmp_path):
    from facility_ops.config import get_settings
    from facility_ops.data_access import db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "other.db"))
    monkeypatch.setenv("SEED_DIR", str(tmp_path / "no_such_folder"))
    get_settings.cache_clear()
    db._verified_path = None
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF))
    assert r.status is ToolStatus.ERROR
    assert "unavailable" in r.summary and "Seed file not found" in (r.error or "")


def test_missing_knowledge_base_becomes_error_result(monkeypatch, tmp_path):
    from facility_ops.config import get_settings
    from facility_ops.data_access.knowledge_base import _cached_index

    monkeypatch.setenv("KB_DIR", str(tmp_path / "no_kb"))
    get_settings.cache_clear()
    _cached_index.cache_clear()
    r = search_knowledge_base(KnowledgeSearchInput(query="chiller trip"))
    assert r.status is ToolStatus.ERROR


def test_gemini_api_key_alias_is_accepted(monkeypatch):
    from facility_ops.config import get_settings

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    assert get_settings().google_api_key == "test-key"
    assert get_settings().llm_key_present()
    get_settings.cache_clear()


def test_duplicate_hides_another_occupants_ticket():
    # OCC-2004 reports the same L12 issue that OCC-2003 raised (CMP-2026-0014)
    r = prepare_work_order(
        WorkOrderDraft(
            requester_id="OCC-2004", location="L12 studio", issue_description="hot", issue_category=TicketCategory.HVAC
        )
    )
    dup = r.data["duplicates"][0]
    assert r.status is ToolStatus.DUPLICATE and dup["ticket_id"] == "CMP-2026-0014"
    assert dup["requester_id"] == "(hidden)" and "hidden" in dup["description"]
    assert "warm" not in json.dumps(r.data)  # original description never leaves the tool


def test_staff_see_full_duplicate_details():
    r = prepare_work_order(
        WorkOrderDraft(requester_id="EMP-1052", asset_hint="lift 2", issue_description="stops at L7")
    )
    assert r.data["duplicates"][0]["requester_id"] == "EMP-1052"


def test_compliance_due_soon_combines_due_and_upcoming():
    r = check_compliance(ComplianceStatusInput(requester_id=STAFF, status_filter="due_soon"))
    ids = [i["certificate"]["cert_id"] for i in r.data["items"]]
    # due first (most urgent), then upcoming; overdue and valid excluded
    assert set(ids) == {"CERT-004", "CERT-010", "CERT-011", "CERT-001", "CERT-008"}
    assert ids[:3] == ["CERT-011", "CERT-004", "CERT-010"]
    assert "due soon shown" in r.summary


# ------------------------------------------------- Tool 2: my tickets ----


def test_my_tickets_lists_own_tickets_open_first():
    r = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, my_tickets=True))
    assert r.status is ToolStatus.SUCCESS
    assert [t["ticket_id"] for t in r.data["tickets"]] == ["CMP-2026-0017", "CMP-2026-0009"]  # assigned, closed


def test_my_tickets_narrowed_to_one_shows_its_status():
    r = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, my_tickets=True, keyword="my access card issue"))
    assert r.status is ToolStatus.SUCCESS and r.data["ticket"]["ticket_id"] == "CMP-2026-0017"
    r = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, my_tickets=True, issue_category=TicketCategory.PLUMBING))
    assert r.data["ticket"]["ticket_id"] == "CMP-2026-0009"


def test_my_tickets_never_shows_other_peoples_tickets():
    r = lookup_asset(AssetLookupInput(requester_id=OCCUPANT, my_tickets=True, keyword="L12 studio warm"))
    assert r.status is ToolStatus.NOT_FOUND  # CMP-2026-0014 belongs to OCC-2003
    assert "CMP-2026-0014" not in str(r.data)
    r = lookup_asset(AssetLookupInput(requester_id="EMP-1101", my_tickets=True))
    assert r.status is ToolStatus.NOT_FOUND
