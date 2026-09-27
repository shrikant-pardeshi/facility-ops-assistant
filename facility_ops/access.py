"""
Role-based access rules (design option B - "light access control").

Mirrors how a managed-facility helpdesk works in practice:

  Occupants (OCC-)     tenant employees. They report problems and track their
                       own tickets. They do not see vendor contracts, AMC
                       terms, maintenance history or statutory compliance.
  Facility staff (EMP-) the FM team. Full operational visibility, at ANY site
                       in the portfolio (FM teams cover several buildings).

Occupants are scoped to the building they are registered at.

The rules live here, in one table, and are enforced INSIDE every tool
(defence in depth) - so even if the LLM routes a request wrongly, a tool will
not return data the requester is not allowed to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from facility_ops.schemas import Person, Ticket, TicketType


class Capability(str, Enum):
    SEARCH_KNOWLEDGE_BASE = "search_knowledge_base"
    VIEW_ASSET_DETAILS = "view_asset_details"  # asset register, warranty, AMC, history
    VIEW_ANY_TICKET = "view_any_ticket"
    VIEW_OWN_TICKETS = "view_own_tickets"
    VIEW_COMPLIANCE = "view_compliance"
    RAISE_WORK_ORDER = "raise_work_order"
    RAISE_COMPLAINT = "raise_complaint"


ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "facility_staff": frozenset(
        {
            Capability.SEARCH_KNOWLEDGE_BASE,
            Capability.VIEW_ASSET_DETAILS,
            Capability.VIEW_ANY_TICKET,
            Capability.VIEW_OWN_TICKETS,
            Capability.VIEW_COMPLIANCE,
            Capability.RAISE_WORK_ORDER,
        }
    ),
    "occupant": frozenset(
        {
            Capability.SEARCH_KNOWLEDGE_BASE,
            Capability.VIEW_OWN_TICKETS,
            Capability.RAISE_COMPLAINT,
        }
    ),
}

# What to offer instead when something is refused (shown to the user).
ROLE_ALTERNATIVES: dict[str, str] = {
    "occupant": (
        "As an occupant you can search maintenance and safety guidance, "
        "raise a complaint, and check the status of tickets you raised."
    ),
    "facility_staff": "",
}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str = ""


ALLOW = AccessDecision(True)


def _deny(person: Person, what: str) -> AccessDecision:
    alt = ROLE_ALTERNATIVES.get(person.role, "")
    return AccessDecision(False, f"{what} is restricted to the facility management team. {alt}".strip())


def can(person: Person, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(person.role, frozenset())


def can_work_at_any_facility(person: Person) -> bool:
    """FM staff cover the whole portfolio; occupants belong to one building."""
    return person.role == "facility_staff"


def check_facility(person: Person, facility_id: str | None) -> AccessDecision:
    if can_work_at_any_facility(person):
        return ALLOW
    if facility_id and facility_id != person.facility_id:
        return AccessDecision(
            False,
            f"{person.person_id} is registered at {person.facility_id} and cannot "
            f"access records for {facility_id}.",
        )
    return ALLOW


def check_asset_details(person: Person, asset_facility_id: str) -> AccessDecision:
    if not can(person, Capability.VIEW_ASSET_DETAILS):
        return _deny(person, "Asset, AMC and maintenance information")
    return check_facility(person, asset_facility_id)


def check_compliance(person: Person, facility_id: str) -> AccessDecision:
    if not can(person, Capability.VIEW_COMPLIANCE):
        return _deny(person, "Statutory compliance status")
    return check_facility(person, facility_id)


def check_ticket_view(person: Person, ticket: Ticket) -> AccessDecision:
    scope = check_facility(person, ticket.facility_id)
    if not scope.allowed:
        return scope
    if can(person, Capability.VIEW_ANY_TICKET):
        return ALLOW
    if can(person, Capability.VIEW_OWN_TICKETS) and ticket.requester_id == person.person_id:
        return ALLOW
    return AccessDecision(False, "You can only view the status of tickets you raised yourself.")


def ticket_type_for(person: Person) -> TicketType:
    """Staff raise work orders; occupants raise complaints."""
    if can(person, Capability.RAISE_WORK_ORDER):
        return TicketType.WORK_ORDER
    return TicketType.COMPLAINT


# ---------------------------------------------------------------------------
# Sign-in (used by the Streamlit UI)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignInResult:
    person: Person | None = None
    facility_id: str | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.person is not None and not self.error


def sign_in(person_id: str, facility_id: str | None, lookup_person, lookup_facility) -> SignInResult:
    """Validate a sign-in. `facility_id=None` means "my registered facility".

    Demo-grade identification (no passwords): the ID must exist in the people
    table, staff may choose any facility, occupants only their own building.
    Lookups are injected so this stays free of database imports.
    """
    from facility_ops.schemas import normalise_id

    pid = normalise_id(person_id)
    if not pid:
        return SignInResult(error="Enter your employee or occupant ID (e.g. EMP-1024 or OCC-2001).")
    person = lookup_person(pid)
    if person is None:
        return SignInResult(error=f"No one with ID {pid} is registered. Check the ID and try again.")
    chosen = normalise_id(facility_id) or person.facility_id
    if lookup_facility(chosen) is None:
        return SignInResult(error=f"Facility {chosen} does not exist.")
    scope = check_facility(person, chosen)
    if not scope.allowed:
        return SignInResult(
            error=f"{person.name} is an occupant registered at {person.facility_id} and can only use that building."
        )
    return SignInResult(person=person, facility_id=chosen)
