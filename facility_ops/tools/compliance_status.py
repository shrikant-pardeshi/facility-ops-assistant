"""
Tool 4 - Compliance Status Lookup.

Every state is computed in Python from dates (as-of = Settings.today()),
never by the LLM:

    overdue   expiry_date has passed
    due       days to expiry <= max(DUE_SOON_DAYS, certificate's renewal_lead_days)
              -> the renewal application should already be in progress
    upcoming  days to expiry <= UPCOMING_DAYS
    valid     otherwise
"""

from __future__ import annotations

from datetime import date, timedelta

from facility_ops import access
from facility_ops.config import get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.schemas import (
    Citation,
    ComplianceCertificate,
    ComplianceItem,
    ComplianceState,
    ComplianceStatusInput,
    ComplianceStatusOutput,
    Intent,
    SourceType,
    ToolResult,
    ToolStatus,
    normalise_id,
)
from facility_ops.tools.common import not_permitted, resolve_requester, safe_tool

INTENT = Intent.COMPLIANCE_STATUS

_SEVERITY = {ComplianceState.OVERDUE: 0, ComplianceState.DUE: 1, ComplianceState.UPCOMING: 2, ComplianceState.VALID: 3}


def classify(cert: ComplianceCertificate, as_of: date) -> ComplianceItem:
    s = get_settings()
    days = (cert.expiry_date - as_of).days
    if days < 0:
        state = ComplianceState.OVERDUE
    elif days <= max(s.due_soon_days, cert.renewal_lead_days):
        state = ComplianceState.DUE
    elif days <= s.upcoming_days:
        state = ComplianceState.UPCOMING
    else:
        state = ComplianceState.VALID
    related = [t.ticket_id for t in repo.list_unclosed_tickets_for_asset(cert.asset_id)] if cert.asset_id else []
    return ComplianceItem(
        certificate=cert,
        state=state,
        days_to_expiry=days,
        renewal_should_start_on=cert.expiry_date - timedelta(days=cert.renewal_lead_days),
        related_open_tickets=related,
    )


@safe_tool(INTENT)
def check_compliance(params: ComplianceStatusInput) -> ToolResult:
    """Classify a facility's statutory certificates as overdue / due / upcoming / valid."""
    person = resolve_requester(INTENT, params.requester_id)
    if isinstance(person, ToolResult):
        return person

    facility_id = normalise_id(params.facility_id) or person.facility_id
    decision = access.check_compliance(person, facility_id)
    if not decision.allowed:
        return not_permitted(INTENT, decision.reason)

    facility = repo.get_facility(facility_id)
    if facility is None:
        return ToolResult(tool=INTENT, status=ToolStatus.NOT_FOUND, summary=f"No facility with ID {facility_id}.")

    as_of = get_settings().today()
    cert_type = params.cert_type.value if params.cert_type else None
    items = [classify(c, as_of) for c in repo.list_certificates(facility_id, cert_type)]
    counts = {state: sum(1 for i in items if i.state is state) for state in ComplianceState}

    wanted = {
        "overdue": {ComplianceState.OVERDUE},
        "due": {ComplianceState.DUE},
        "upcoming": {ComplianceState.UPCOMING},
        "due_soon": {ComplianceState.DUE, ComplianceState.UPCOMING},
    }.get(params.status_filter)
    if wanted:
        items = [i for i in items if i.state in wanted]
    items.sort(key=lambda i: (_SEVERITY[i.state], i.days_to_expiry))

    output = ComplianceStatusOutput(facility=facility, as_of=as_of, items=items, counts=counts)
    citations = [
        Citation(
            source_type=SourceType.CERTIFICATE,
            source_id=i.certificate.cert_id,
            title=i.certificate.title,
            locator=i.certificate.kb_reference,
        )
        for i in items
    ]
    count_text = ", ".join(f"{counts[s]} {s.value}" for s in ComplianceState)
    label = params.status_filter.replace("_", " ")
    scope = "" if params.status_filter == "all" else f" ({len(items)} {label} shown)"
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.SUCCESS if items else ToolStatus.NOT_FOUND,
        summary=f"{facility.name} as of {as_of.isoformat()}: {count_text}{scope}.",
        data=output.model_dump(mode="json"),
        citations=citations,
    )
