"""
Tool 2 - Asset & Maintenance Lookup (also answers ticket-status questions).

Lookup modes, in priority order:
    ticket_id              -> that ticket (+ a one-line asset reference)
    my_tickets (+keyword/  -> the requester's OWN tickets ("what's the status of my
     issue_category)          AC complaint?"); one match -> that ticket's status
    asset_id               -> full asset card
    asset_hint (+facility) -> resolve the name; one match -> full card,
                              several -> NEEDS_INPUT with candidates to pick from

Full asset card = register entry, warranty state, current AMC + state, last
service, recent history, and tickets on the asset that are not closed.

Access (see access.py): occupants may only look up tickets they raised.
"""

from __future__ import annotations

import re

from facility_ops import access
from facility_ops.config import get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.schemas import (
    AMCContract,
    Asset,
    AssetLookupInput,
    AssetLookupOutput,
    Citation,
    Intent,
    SourceType,
    Ticket,
    TicketStatus,
    ToolResult,
    ToolStatus,
    normalise_id,
)
from facility_ops.tools.common import coverage_state, needs_input, not_permitted, resolve_requester, safe_tool

INTENT = Intent.ASSET_LOOKUP

# Words that say nothing about WHICH ticket ("my AC issue" -> "ac").
_GENERIC = {
    "my", "the", "a", "an", "our", "of", "on", "in", "at", "for", "about", "with", "and", "to",
    "issue", "issues", "problem", "problems", "ticket", "tickets", "complaint", "complaints",
    "request", "requests", "work", "order", "orders", "status", "raised", "reported", "not", "is",
}


def _keyword_score(ticket: Ticket, keyword: str) -> int:
    words = {w for w in re.findall(r"[a-z0-9]+", keyword.lower()) if w not in _GENERIC}
    text = f"{ticket.description} {ticket.location} {ticket.category.value}".lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return sum(1 for w in words if w in tokens or (len(w) > 3 and w in text))


def _ticket_line(t: Ticket) -> str:
    return f"{t.ticket_id} ({t.category.value}, {t.priority.value}, {t.status.value}): {t.description[:70]}"


def _my_tickets(person, params: AssetLookupInput) -> ToolResult:
    """Mode 1b: the requester's own tickets, optionally narrowed down."""
    mine = repo.list_tickets_for_requester(person.person_id)
    if not mine:
        return ToolResult(tool=INTENT, status=ToolStatus.NOT_FOUND, summary=f"{person.person_id} has not raised any tickets.")
    matches = mine
    what = ""
    if params.issue_category:
        matches = [t for t in matches if t.category is params.issue_category]
        what = params.issue_category.value
    if params.keyword:
        scored = [(t, _keyword_score(t, params.keyword)) for t in matches]
        best = max((sc for _, sc in scored), default=0)
        matches = [t for t, sc in scored if best and sc == best]
        what = f"{what} '{params.keyword}'".strip()
    if not matches:
        return ToolResult(
            tool=INTENT,
            status=ToolStatus.NOT_FOUND,
            summary=f"None of {person.person_id}'s {len(mine)} ticket(s) match {what}.",
            data=AssetLookupOutput(tickets=mine).model_dump(mode="json"),
            citations=[Citation(source_type=SourceType.TICKET, source_id=t.ticket_id, title=t.description[:60]) for t in mine],
        )
    citations = [Citation(source_type=SourceType.TICKET, source_id=t.ticket_id, title=t.description[:60]) for t in matches]
    if len(matches) == 1:
        t = matches[0]
        return ToolResult(
            tool=INTENT,
            status=ToolStatus.SUCCESS,
            summary=(
                f"{t.ticket_id} ({t.ticket_type.value}, {t.priority.value}) is {t.status.value}; "
                f"last updated {t.updated_at:%d-%b-%Y %H:%M}."
            ),
            data=AssetLookupOutput(ticket=t).model_dump(mode="json"),
            citations=citations,
        )
    open_count = sum(1 for t in matches if t.status is not TicketStatus.CLOSED)
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.SUCCESS,
        summary=f"{person.person_id} has {len(matches)} {'matching ' if what else ''}ticket(s), {open_count} not closed: "
        + "; ".join(_ticket_line(t) for t in matches),
        data=AssetLookupOutput(tickets=matches).model_dump(mode="json"),
        citations=citations,
    )


def _current_amc(contracts: list[AMCContract]) -> AMCContract | None:
    """The contract covering today; otherwise the most recent one."""
    today = get_settings().today()
    for c in contracts:
        if c.start_date <= today <= c.end_date:
            return c
    return contracts[0] if contracts else None  # repo returns latest end_date first


def _asset_card(asset: Asset, history_limit: int) -> tuple[AssetLookupOutput, list[Citation]]:
    amc = _current_amc(repo.get_amc_contracts(asset.asset_id))
    history = repo.get_maintenance_history(asset.asset_id, history_limit)
    open_tickets = repo.list_unclosed_tickets_for_asset(asset.asset_id)
    output = AssetLookupOutput(
        asset=asset,
        warranty_state=coverage_state(asset.warranty_expiry),
        amc=amc,
        amc_state=coverage_state(amc.end_date if amc else None),
        last_service=history[0] if history else None,
        history=history,
        open_tickets=open_tickets,
    )
    citations = [Citation(source_type=SourceType.ASSET_REGISTER, source_id=asset.asset_id, title=asset.name)]
    if amc:
        citations.append(
            Citation(source_type=SourceType.AMC_CONTRACT, source_id=amc.amc_id, title=f"AMC - {amc.vendor}")
        )
    citations += [
        Citation(
            source_type=SourceType.MAINTENANCE_RECORD,
            source_id=h.record_id,
            title=f"{h.maintenance_type.value.upper()} {h.service_date.isoformat()}",
        )
        for h in history
    ]
    citations += [
        Citation(source_type=SourceType.TICKET, source_id=t.ticket_id, title=t.description[:60]) for t in open_tickets
    ]
    return output, citations


def _card_summary(o: AssetLookupOutput) -> str:
    a = o.asset
    assert a is not None and o.amc_state is not None and o.warranty_state is not None  # set by _asset_card
    amc = f"AMC {o.amc_state.value}" + (f" ({o.amc.contract_type}, ends {o.amc.end_date})" if o.amc else "")
    last = f"last service {o.last_service.service_date}" if o.last_service else "no service records"
    return (
        f"{a.asset_id} {a.name}: {a.operational_status}; warranty {o.warranty_state.value}; "
        f"{amc}; {last}; {len(o.open_tickets)} ticket(s) not closed."
    )


@safe_tool(INTENT)
def lookup_asset(params: AssetLookupInput) -> ToolResult:
    """Look up an asset's warranty, AMC, maintenance history and open tickets, or a ticket's status."""
    person = resolve_requester(INTENT, params.requester_id)
    if isinstance(person, ToolResult):
        return person

    # --- Mode 1: ticket status ---------------------------------------------
    if params.ticket_id:
        ticket_id = normalise_id(params.ticket_id) or ""
        ticket = repo.get_ticket(ticket_id)
        if ticket is None:
            return ToolResult(tool=INTENT, status=ToolStatus.NOT_FOUND, summary=f"No ticket with ID {ticket_id}.")
        decision = access.check_ticket_view(person, ticket)
        if not decision.allowed:
            return not_permitted(INTENT, decision.reason)
        output = AssetLookupOutput(ticket=ticket)
        return ToolResult(
            tool=INTENT,
            status=ToolStatus.SUCCESS,
            summary=(
                f"{ticket.ticket_id} ({ticket.ticket_type.value}, {ticket.priority.value}) is "
                f"{ticket.status.value}; last updated {ticket.updated_at:%d-%b-%Y %H:%M}."
            ),
            data=output.model_dump(mode="json"),
            citations=[
                Citation(source_type=SourceType.TICKET, source_id=ticket.ticket_id, title=ticket.description[:60])
            ],
        )

    # --- Mode 1b: the requester's own tickets ---------------------------------
    if params.my_tickets:
        return _my_tickets(person, params)

    # Everything below is asset detail - staff only.
    if not access.can(person, access.Capability.VIEW_ASSET_DETAILS):
        return not_permitted(INTENT, access.check_asset_details(person, person.facility_id).reason)

    # --- Mode 2: exact asset ID --------------------------------------------
    asset: Asset | None = None
    if params.asset_id:
        asset_id = normalise_id(params.asset_id) or ""
        asset = repo.get_asset(asset_id)
        if asset is None:
            return ToolResult(tool=INTENT, status=ToolStatus.NOT_FOUND, summary=f"No asset with ID {asset_id}.")

    # --- Mode 3: resolve a name within the requester's facility -------------
    elif params.asset_hint:
        facility_id = normalise_id(params.facility_id) or person.facility_id
        scope = access.check_facility(person, facility_id)
        if not scope.allowed:
            return not_permitted(INTENT, scope.reason)
        matches = repo.find_assets(facility_id, params.asset_hint)
        if not matches:
            return ToolResult(
                tool=INTENT,
                status=ToolStatus.NOT_FOUND,
                summary=f"No asset matching '{params.asset_hint}' at {facility_id}.",
            )
        if len(matches) > 1:
            output = AssetLookupOutput(candidates=matches)
            options = "; ".join(f"{a.asset_id} ({a.name})" for a in matches)
            return ToolResult(
                tool=INTENT,
                status=ToolStatus.NEEDS_INPUT,
                summary=f"'{params.asset_hint}' matches {len(matches)} assets.",
                data=output.model_dump(mode="json"),
                follow_up_question=f"Which one do you mean? {options}",
            )
        asset = matches[0]
    else:
        return needs_input(INTENT, ["asset_id"])

    decision = access.check_asset_details(person, asset.facility_id)
    if not decision.allowed:
        return not_permitted(INTENT, decision.reason)

    output, citations = _asset_card(asset, params.history_limit)
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.SUCCESS,
        summary=_card_summary(output),
        data=output.model_dump(mode="json"),
        citations=citations,
    )
