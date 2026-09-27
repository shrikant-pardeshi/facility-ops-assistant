"""
Tool 3 - Work Order / Complaint Creation. Two phases, so the agent never
writes to the database without an explicit "yes":

    prepare_work_order(draft)  validate -> derive -> duplicate check
                               -> NEEDS_CONFIRMATION (shows the draft)
    commit_work_order(draft)   re-runs every check, then inserts

Pipeline (both phases):
    1. requester must exist                     else NEEDS_INPUT
    2. facility = requester's facility          else NOT_PERMITTED
    3. ticket type from role: staff -> work order, occupant -> complaint
    4. asset (staff only): asset_id or a name hint resolved within the facility;
       fills category / location when missing
    5. required fields present                  else NEEDS_INPUT
    6. floor derivable when there is no asset   else NEEDS_INPUT (needed for duplicates)
    7. priority from PRIORITY RULES (mirrors SOP-HD-001), never from the LLM
    8. any NOT-CLOSED ticket with same facility + category + (asset | floor)
                                                -> DUPLICATE, nothing created
"""

from __future__ import annotations

import re
from datetime import datetime

from facility_ops import access
from facility_ops.config import get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.schemas import (
    WORK_ORDER_REQUIRED_FIELDS,
    AssetCategory,
    Citation,
    Criticality,
    Intent,
    Person,
    Priority,
    SourceType,
    Ticket,
    TicketCategory,
    TicketType,
    ToolResult,
    ToolStatus,
    WorkOrderDraft,
    WorkOrderOutput,
    normalise_floor,
    normalise_id,
)
from facility_ops.tools.common import needs_input, not_permitted, resolve_requester, safe_tool

INTENT = Intent.CREATE_WORK_ORDER
PRIORITY_SOURCE = Citation(
    source_type=SourceType.SOP,
    source_id="SOP-HD-001",
    title="Helpdesk Ticket Priority Matrix and SLA",
    locator="Priority matrix",
)

ASSET_TO_TICKET_CATEGORY: dict[AssetCategory, TicketCategory] = {
    AssetCategory.CHILLER: TicketCategory.HVAC,
    AssetCategory.AHU: TicketCategory.HVAC,
    AssetCategory.VRF: TicketCategory.HVAC,
    AssetCategory.BMS: TicketCategory.HVAC,
    AssetCategory.LIFT: TicketCategory.LIFT,
    AssetCategory.DG_SET: TicketCategory.ELECTRICAL,
    AssetCategory.TRANSFORMER: TicketCategory.ELECTRICAL,
    AssetCategory.UPS: TicketCategory.ELECTRICAL,
    AssetCategory.FIRE_PUMP: TicketCategory.FIRE_SAFETY,
    AssetCategory.FIRE_ALARM: TicketCategory.FIRE_SAFETY,
    AssetCategory.STP: TicketCategory.PLUMBING,
    AssetCategory.WTP: TicketCategory.PLUMBING,
    AssetCategory.PUMP: TicketCategory.PLUMBING,
    AssetCategory.OTHER: TicketCategory.OTHER,
}

# ---------------------------------------------------------------------------
# Priority rules (SOP-HD-001). First matching rule wins.
# ---------------------------------------------------------------------------
_P1_PATTERNS = [
    r"\btrapped\b", r"\bentrapment\b", r"\bstuck (inside|in)\b", r"\bsmoke\b", r"\bburning\b",
    r"\bspark(s|ing)?\b", r"\bgas leak\b", r"\bflood(ing|ed)?\b", r"\bwater ingress\b",
    r"\b(total|complete) power (failure|cut|outage)\b", r"\bblackout\b", r"\belectric shock\b",
    r"\b(on fire|fire in|caught fire)\b",
]
_P2_PATTERNS = [
    r"\b(whole|entire) floor\b", r"\ball floors\b", r"\bout of service\b", r"\bbreakdown\b",
    r"\btripped\b", r"\bnot starting\b", r"\bfailed\b",
]
_P4_PATTERNS = [r"\bcosmetic\b", r"\bpaint(ing)?\b", r"\bsuggestion\b", r"\brequest for\b"]


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        if m := re.search(pattern, text):
            return m.group(0)
    return None


def derive_priority(
    category: TicketCategory, description: str, asset_criticality: Criticality | None
) -> tuple[Priority, str]:
    text = description.lower()
    if hit := _first_match(_P1_PATTERNS, text):
        return Priority.P1, f"emergency keyword '{hit}'"
    if category is TicketCategory.LIFT:
        return Priority.P2, "lift fault (lift out of service is P2 unless someone is trapped)"
    if category is TicketCategory.FIRE_SAFETY:
        return Priority.P2, "fire-safety system fault"
    if asset_criticality is Criticality.HIGH:
        return Priority.P2, "fault on a high-criticality asset"
    if hit := _first_match(_P2_PATTERNS, text):
        return Priority.P2, f"urgent keyword '{hit}'"
    if category is TicketCategory.HOUSEKEEPING:
        return Priority.P4, "housekeeping request"
    if hit := _first_match(_P4_PATTERNS, text):
        return Priority.P4, f"low-priority keyword '{hit}'"
    return Priority.P3, "localised issue - default priority"


# ---------------------------------------------------------------------------
# Shared validation pipeline
# ---------------------------------------------------------------------------


def _prepare(draft: WorkOrderDraft) -> tuple[WorkOrderDraft, Person | None, ToolResult | None]:
    """Complete and validate the draft. Returns (draft, person, blocking_result)."""
    d = draft.model_copy(deep=True)

    # 1. requester
    person = resolve_requester(INTENT, d.requester_id)
    if isinstance(person, ToolResult):
        person.data["draft"] = d.model_dump(mode="json")
        return d, None, person
    d.requester_id = person.person_id

    # 2. facility scope
    requested_facility = normalise_id(d.facility_id)
    scope = access.check_facility(person, requested_facility)
    if not scope.allowed:
        return d, person, not_permitted(INTENT, scope.reason)
    d.facility_id = requested_facility or person.facility_id
    if repo.get_facility(d.facility_id) is None:
        return d, person, ToolResult(tool=INTENT, status=ToolStatus.NOT_FOUND, summary=f"No facility with ID {d.facility_id}.")

    # 3. ticket type from role
    d.ticket_type = access.ticket_type_for(person)

    # 4. asset (staff only - occupants report by location)
    asset = None
    if d.ticket_type is TicketType.COMPLAINT:
        d.asset_id, d.asset_hint = None, None
    elif d.asset_id:
        d.asset_id = normalise_id(d.asset_id) or ""
        asset = repo.get_asset(d.asset_id)
        if asset is None or asset.facility_id != d.facility_id:
            d.asset_id = None
            return d, person, needs_input(
                INTENT, ["asset_id"], lead=f"Asset {draft.asset_id} was not found at {d.facility_id}."
            )
    elif d.asset_hint:
        matches = repo.find_assets(d.facility_id, d.asset_hint)
        if len(matches) == 1:
            asset = matches[0]
            d.asset_id = asset.asset_id
        elif len(matches) > 1:
            options = "; ".join(f"{a.asset_id} ({a.name})" for a in matches)
            result = ToolResult(
                tool=INTENT,
                status=ToolStatus.NEEDS_INPUT,
                summary=f"'{d.asset_hint}' matches {len(matches)} assets.",
                data={"missing_fields": ["asset_id"], "draft": d.model_dump(mode="json")},
                follow_up_question=f"Which equipment is this about? {options}",
            )
            return d, person, result
        # no match: carry on as a location-based ticket

    if asset is not None:
        d.issue_category = d.issue_category or ASSET_TO_TICKET_CATEGORY.get(asset.category, TicketCategory.OTHER)
        d.location = d.location or asset.location

    # 5. required fields
    missing = d.missing_fields()
    if missing:
        result = needs_input(INTENT, missing)
        result.data["draft"] = d.model_dump(mode="json")
        return d, person, result

    # 6. floor (needed for duplicate matching when there is no asset)
    d.floor = normalise_floor(d.location) or (normalise_floor(asset.location) if asset else None)
    if d.floor is None and asset is None:
        result = needs_input(INTENT, ["floor"])
        result.data["draft"] = d.model_dump(mode="json")
        return d, person, result

    # Required fields were checked in step 5; narrow the Optional types for the checker.
    assert d.issue_category is not None and d.issue_description and d.facility_id

    # 7. priority
    d.priority, d.priority_reason = derive_priority(
        d.issue_category, d.issue_description, asset.criticality if asset else None
    )

    # 8. duplicates (anything not CLOSED blocks)
    duplicates = repo.find_duplicate_tickets(d.facility_id, d.issue_category, d.asset_id, d.floor)
    if duplicates:
        duplicates = [_visible_to(person, t) for t in duplicates]
        existing = duplicates[0]
        output = WorkOrderOutput(draft=d, duplicates=duplicates)
        return d, person, ToolResult(
            tool=INTENT,
            status=ToolStatus.DUPLICATE,
            summary=(
                f"Not created: {existing.ticket_id} ({existing.category.value}, "
                f"{existing.floor or existing.asset_id}) is already {existing.status.value}."
            ),
            data=output.model_dump(mode="json"),
            citations=[
                Citation(source_type=SourceType.TICKET, source_id=t.ticket_id, title=t.description[:60])
                for t in duplicates
            ],
        )
    return d, person, None


def _visible_to(person: Person, ticket: Ticket) -> Ticket:
    """Occupants only see full details of tickets they raised (access rule).
    For anyone else's ticket they learn that it exists, and its status - no
    requester, description or asset."""
    if access.check_ticket_view(person, ticket).allowed:
        return ticket
    return ticket.model_copy(
        update={
            "requester_id": "(hidden)",
            "asset_id": None,
            "location": ticket.floor or "(hidden)",
            "description": "Already reported by someone else - details hidden",
        }
    )


def _draft_card(d: WorkOrderDraft) -> str:
    kind = "work order" if d.ticket_type is TicketType.WORK_ORDER else "complaint"
    asset = f", asset {d.asset_id}" if d.asset_id else ""
    category = d.issue_category.value if d.issue_category else "-"
    priority = d.priority.value if d.priority else "-"
    return (
        f"{kind} at {d.facility_id}, {d.location}{asset}; category {category}; "
        f"priority {priority} ({d.priority_reason}); issue: {d.issue_description}"
    )


@safe_tool(INTENT)
def prepare_work_order(draft: WorkOrderDraft) -> ToolResult:
    """Validate a new work order / complaint and ask for confirmation. Creates nothing."""
    d, _, blocking = _prepare(draft)
    if blocking:
        return blocking
    kind = "work order" if d.ticket_type is TicketType.WORK_ORDER else "complaint"
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.NEEDS_CONFIRMATION,
        summary=f"Ready to create {_draft_card(d)}",
        data=WorkOrderOutput(draft=d).model_dump(mode="json"),
        citations=[PRIORITY_SOURCE],
        follow_up_question=f"Shall I create this {kind}? Reply **yes** to confirm or **no** to cancel.",
    )


@safe_tool(INTENT)
def commit_work_order(draft: WorkOrderDraft) -> ToolResult:
    """Create the ticket after the user confirmed. Re-validates everything first."""
    d, _, blocking = _prepare(draft)
    if blocking:
        return blocking
    created_at = datetime.combine(get_settings().today(), datetime.now().time()).replace(microsecond=0)
    ticket = repo.insert_ticket(
        {
            "ticket_type": d.ticket_type,
            "facility_id": d.facility_id,
            "requester_id": d.requester_id,
            "asset_id": d.asset_id,
            "location": d.location,
            "floor": d.floor,
            "category": d.issue_category,
            "priority": d.priority,
            "description": d.issue_description,
        },
        created_at=created_at,
    )
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.SUCCESS,
        summary=(
            f"Created {ticket.ticket_id} ({ticket.ticket_type.value}, {ticket.priority.value}, "
            f"response SLA {ticket.priority.response_sla_hours} h), status {ticket.status.value}."
        ),
        data=WorkOrderOutput(draft=d, created=ticket).model_dump(mode="json"),
        citations=[
            Citation(source_type=SourceType.TICKET, source_id=ticket.ticket_id, title=ticket.description[:60]),
            PRIORITY_SOURCE,
        ],
    )
