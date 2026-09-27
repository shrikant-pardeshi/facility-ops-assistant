"""
Domain and contract schemas (Pydantic v2).

Three groups of models live here:

1. Domain records   - mirror the rows in the local SQLite database
                      (facilities, people, assets, AMC contracts, maintenance
                      history, compliance certificates, tickets, KB chunks).
2. LLM contracts    - the structured outputs we force the LLM to return
                      (IntentDecision from the `understand` node,
                      FinalAnswer from the `respond` node).
3. Tool contracts   - typed inputs/outputs for each of the four tools, plus
                      the uniform ToolResult envelope every tool node writes
                      into graph state.

Design rule: the LLM never produces facts. It produces *decisions*
(IntentDecision) and *phrasing* (FinalAnswer) - every fact shown to the user
must trace back to a ToolResult.data payload and a Citation.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Identifier formats (used for validation and anti-hallucination guards)
# ---------------------------------------------------------------------------

ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "facility_id": re.compile(r"^FAC-\d{3}$"),
    "requester_id": re.compile(r"^(EMP|OCC)-\d{4}$"),  # EMP = FM staff, OCC = occupant
    "asset_id": re.compile(r"^AST-[A-Z]{3}-\d{3}$"),  # e.g. AST-CHL-001
    "ticket_id": re.compile(r"^(WO|CMP)-\d{4}-\d{4}$"),  # e.g. WO-2026-0042
    "cert_id": re.compile(r"^CERT-\d{3}$"),
}


def normalise_id(value: str | None) -> str | None:
    """Upper-case and strip an identifier; tolerate 'ast chl 001' style input."""
    if value is None:
        return None
    cleaned = re.sub(r"[\s_]+", "-", value.strip().upper())
    return cleaned or None


# Human-readable prompts used when a required field is missing. Deterministic
# (not LLM-written) so clarification questions are predictable and testable.
FIELD_PROMPTS: dict[str, str] = {
    "requester_id": "your employee or occupant ID (e.g. EMP-1024 or OCC-2001)",
    "facility_id": "the facility ID (e.g. FAC-001)",
    "asset_id": "the asset ID (e.g. AST-CHL-001) or the equipment name (e.g. 'chiller 2')",
    "location": "the exact location - floor and area (e.g. 'L5, east wing conference room')",
    "floor": "which floor this is on (e.g. L5, G, B2)",
    "issue_description": "a short description of the problem",
    "issue_category": "the type of issue (HVAC, electrical, plumbing, lift, fire safety, civil, housekeeping or security)",
    "search_query": "what you would like to look up",
}


def normalise_floor(text: str | None) -> str | None:
    """Extract a floor token from free text: 'L5 east wing' -> 'L5',
    'level 12' -> 'L12', '3rd floor' -> 'L3', 'basement 2' -> 'B2',
    'ground floor' -> 'G', 'terrace' -> 'TERRACE'. None if no floor found."""
    if not text:
        return None
    t = text.lower()
    patterns = [
        (r"\b(?:l|level|lvl)\s*-?\s*(\d{1,2})\b", "L{}"),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s*floor\b", "L{}"),
        (r"\bfloor\s*(\d{1,2})\b", "L{}"),
        (r"\b(?:b|basement)\s*-?\s*(\d)\b", "B{}"),
    ]
    for pattern, fmt in patterns:
        m = re.search(pattern, t)
        if m:
            return fmt.format(int(m.group(1)))
    if re.search(r"\b(ground|g)\s*(floor|f)?\b", t) and not re.search(r"\bg\+", t):
        return "G"
    if "terrace" in t or "roof" in t:
        return "TERRACE"
    return None


class _Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, extra="ignore")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Intent(str, Enum):
    """What the user wants. The first four map 1:1 to tool nodes."""

    KNOWLEDGE_SEARCH = "knowledge_search"
    ASSET_LOOKUP = "asset_lookup"
    CREATE_WORK_ORDER = "create_work_order"
    COMPLIANCE_STATUS = "compliance_status"
    SMALL_TALK = "small_talk"  # greetings, thanks, "what can you do?"
    OUT_OF_SCOPE = "out_of_scope"  # anything the assistant must decline

    @property
    def is_tool_intent(self) -> bool:
        return self in TOOL_INTENTS


TOOL_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.KNOWLEDGE_SEARCH,
        Intent.ASSET_LOOKUP,
        Intent.CREATE_WORK_ORDER,
        Intent.COMPLIANCE_STATUS,
    }
)


class AssetCategory(str, Enum):
    CHILLER = "chiller"
    AHU = "ahu"
    VRF = "vrf"
    LIFT = "lift"
    DG_SET = "dg_set"
    FIRE_PUMP = "fire_pump"
    FIRE_ALARM = "fire_alarm"
    TRANSFORMER = "transformer"
    UPS = "ups"
    STP = "stp"
    WTP = "wtp"
    PUMP = "pump"
    BMS = "bms"
    OTHER = "other"


class Criticality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MaintenanceType(str, Enum):
    PPM = "ppm"  # planned preventive maintenance
    BREAKDOWN = "breakdown"
    STATUTORY = "statutory"  # inspection by an authority / licensed agency
    UPGRADE = "upgrade"


class TicketType(str, Enum):
    WORK_ORDER = "work_order"  # raised by FM staff, usually asset-linked
    COMPLAINT = "complaint"  # raised by an occupant


class TicketCategory(str, Enum):
    HVAC = "hvac"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    LIFT = "lift"
    FIRE_SAFETY = "fire_safety"
    CIVIL = "civil"
    HOUSEKEEPING = "housekeeping"
    SECURITY = "security"
    OTHER = "other"


class Priority(str, Enum):
    """P1 = life-safety / business-critical. Response SLA in hours."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

    @property
    def response_sla_hours(self) -> int:
        return {"P1": 1, "P2": 4, "P3": 24, "P4": 72}[self.value]


class TicketStatus(str, Enum):
    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"
    CLOSED = "closed"

    @property
    def is_active(self) -> bool:
        return self in {self.OPEN, self.ASSIGNED, self.IN_PROGRESS, self.ON_HOLD}

    @property
    def blocks_duplicates(self) -> bool:
        """Any ticket that is not CLOSED blocks an equivalent new ticket
        (includes RESOLVED - the requester has not yet confirmed closure)."""
        return self is not TicketStatus.CLOSED


class CertificateType(str, Enum):
    FIRE_NOC = "fire_noc"  # fire NOC / renewal from Chief Fire Officer
    FIRE_FORM_B = "fire_form_b"  # half-yearly fire-system compliance certificate
    LIFT_LICENSE = "lift_license"  # lift operating licence / annual inspection
    DG_APPROVAL = "dg_approval"  # electrical inspectorate approval for DG set
    ELECTRICAL_AUDIT = "electrical_audit"
    STRUCTURAL_AUDIT = "structural_audit"
    POLLUTION_CTO = "pollution_cto"  # pollution control board consent to operate (STP/DG)
    WATER_TEST = "water_test"
    OTHER = "other"


class ComplianceState(str, Enum):
    """Computed in Python from expiry date vs. as-of date - never by the LLM."""

    VALID = "valid"
    UPCOMING = "upcoming"  # expires within UPCOMING_DAYS
    DUE = "due"  # expires within DUE_SOON_DAYS - renewal must start now
    OVERDUE = "overdue"  # already expired


class CoverageState(str, Enum):
    """Warranty / AMC coverage, computed from end date vs. as-of date."""

    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    NONE = "none"  # no warranty / no AMC on record


class ToolStatus(str, Enum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"  # valid request, no matching data
    NEEDS_INPUT = "needs_input"  # required field missing / invalid
    NEEDS_CONFIRMATION = "needs_confirmation"  # draft ready, awaiting user "yes"
    DUPLICATE = "duplicate"  # an equivalent ticket that is not closed already exists
    NOT_PERMITTED = "not_permitted"  # requester's role / facility does not allow this
    ERROR = "error"  # tool failure (file missing, DB error, ...)


class SourceType(str, Enum):
    SOP = "sop"
    COMPLIANCE_CLAUSE = "compliance_clause"
    ASSET_REGISTER = "asset_register"
    AMC_CONTRACT = "amc_contract"
    MAINTENANCE_RECORD = "maintenance_record"
    CERTIFICATE = "certificate"
    TICKET = "ticket"


# ---------------------------------------------------------------------------
# 1. Domain records  (one model per SQLite table)
# ---------------------------------------------------------------------------


class Facility(_Base):
    facility_id: str
    name: str
    address: str
    city: str
    state: str
    occupancy_type: str  # e.g. "Business (Group E)"
    floors_above_ground: int
    building_height_m: float
    built_up_area_sqm: float
    fm_manager_id: str


class Person(_Base):
    person_id: str  # EMP-xxxx or OCC-xxxx
    name: str
    role: Literal["facility_staff", "occupant"]
    designation: str  # "MEP Supervisor", "Admin Manager", ...
    organisation: str
    facility_id: str
    floor: str | None = None
    email: str | None = None
    phone: str | None = None


class Asset(_Base):
    asset_id: str
    facility_id: str
    name: str  # "Chiller 1 - 400 TR Water-Cooled Screw"
    category: AssetCategory
    make: str
    model: str
    serial_no: str
    location: str  # "Basement 2, Plant Room"
    install_date: date
    warranty_expiry: date | None = None
    criticality: Criticality
    operational_status: Literal["running", "standby", "under_maintenance", "down"]


class AMCContract(_Base):
    amc_id: str
    asset_id: str
    vendor: str
    contract_type: Literal["comprehensive", "non_comprehensive"]
    start_date: date
    end_date: date
    visit_frequency: str  # "monthly", "quarterly"
    vendor_contact: str | None = None


class MaintenanceRecord(_Base):
    record_id: str
    asset_id: str
    service_date: date
    maintenance_type: MaintenanceType
    description: str
    performed_by: str
    downtime_hours: float = 0.0
    next_due_date: date | None = None


class ComplianceCertificate(_Base):
    cert_id: str
    facility_id: str
    asset_id: str | None = None  # lift licences are per lift
    cert_type: CertificateType
    title: str
    issuing_authority: str
    reference_no: str | None = None
    issue_date: date | None = None
    expiry_date: date
    renewal_lead_days: int = 30  # how early renewal must be filed
    kb_reference: str | None = None  # links to the compliance clause in the KB


class Ticket(_Base):
    ticket_id: str
    ticket_type: TicketType
    facility_id: str
    requester_id: str
    asset_id: str | None = None
    location: str
    floor: str | None = None  # normalised floor token ("L5", "B2", "G") for duplicate matching
    category: TicketCategory
    priority: Priority
    description: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    source: Literal["seed", "assistant"] = "seed"


class KBChunk(_Base):
    """One retrievable section of a knowledge-base markdown file."""

    chunk_id: str  # "SOP-HVAC-001#3"
    doc_id: str  # "SOP-HVAC-001"
    doc_type: Literal["sop", "compliance"]
    title: str
    section: str
    text: str
    tags: list[str] = Field(default_factory=list)
    source_file: str


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class Citation(_Base):
    """Where a fact came from. Rendered under every grounded answer."""

    source_type: SourceType
    source_id: str  # doc_id / asset_id / cert_id / ticket_id ...
    title: str
    locator: str | None = None  # KB section heading, or table name


# ---------------------------------------------------------------------------
# 2. LLM contracts
# ---------------------------------------------------------------------------


class ExtractedEntities(_Base):
    """Entities found in the user's LATEST message. Leave a field null unless
    the user actually said it. Never guess or construct IDs.

    (Guard: the `understand` node keeps an ID only if it matches ID_PATTERNS
    and appears literally in the user's text.)
    """

    requester_id: str | None = Field(default=None, description="EMP-xxxx or OCC-xxxx, only if the user typed it")
    facility_id: str | None = Field(default=None, description="FAC-xxx, only if the user typed it")
    asset_id: str | None = Field(default=None, description="AST-XXX-nnn, only if the user typed it")
    asset_hint: str | None = Field(default=None, description="Free-text asset reference, e.g. 'chiller 2', 'service lift'")
    ticket_id: str | None = Field(default=None, description="WO-/CMP- id, only if the user typed it")
    location: str | None = Field(default=None, description="Floor / zone / room for a new ticket")
    issue_description: str | None = Field(default=None, description="What is wrong, in the user's words")
    issue_category: TicketCategory | None = None
    ticket_type: TicketType | None = None
    compliance_filter: Literal["all", "overdue", "due_soon", "due", "upcoming"] | None = None
    cert_type: CertificateType | None = None
    my_tickets: bool = Field(default=False, description="The user asks about their OWN tickets without a ticket ID")
    keyword: str | None = Field(default=None, description="Words identifying which of their tickets, e.g. 'access card'")

    @field_validator("requester_id", "facility_id", "asset_id", "ticket_id", mode="before")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        return normalise_id(v)


class IntentDecision(_Base):
    """Structured output of the `understand` node (LLM function calling).

    Kept free of length constraints so every provider's JSON-schema mode
    accepts it; the node enforces "1 to 3 intents" in code.
    """

    intents: list[Intent] = Field(
        ..., description="Ordered list of what the user wants in THIS message (1 to 3 items)."
    )
    entities: ExtractedEntities = Field(default_factory=lambda: ExtractedEntities())
    search_query: str | None = Field(
        default=None,
        description="For knowledge_search: a standalone query that resolves follow-ups from earlier turns "
        "('what about lifts?' -> 'lift licence inspection requirements').",
    )
    confirmation: Literal["yes", "no", "none"] = Field(
        default="none", description="The user's answer to a pending yes/no confirmation, otherwise 'none'."
    )
    rationale: str = Field(description="One short sentence explaining the routing, for the trace panel.")


class LLMAnswer(_Base):
    """Structured output the `respond` node asks the LLM for."""

    answer: str = Field(description="Markdown reply built ONLY from facts in TOOL_RESULTS.")
    recommendations: list[str] = Field(
        default_factory=list,
        description="0-3 practical next steps. General advice only - no new facts about the building.",
    )
    cited_source_ids: list[str] = Field(
        default_factory=list, description="source_id values from TOOL_RESULTS citations that the answer used."
    )


class FinalAnswer(_Base):
    """What the UI renders for one assistant turn.

    `answer` restates only facts from tool results. `recommendations` is the
    ONLY place generated advice appears, and the UI labels it AI-generated.
    `citations` are filtered to sources that actually came from tools.
    `follow_up_questions` are deterministic (written by code, not the LLM).
    """

    answer: str
    recommendations: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    generated_by: Literal["llm", "template", "fallback"] = "llm"


# ---------------------------------------------------------------------------
# 3. Tool contracts
# ---------------------------------------------------------------------------

# --- Tool 1: Knowledge Search --------------------------------------------


class KnowledgeSearchInput(_Base):
    query: str = Field(min_length=3)
    top_k: int | None = Field(default=None, ge=1, le=10, description="Defaults to KB_TOP_K")
    doc_type: Literal["sop", "compliance", "any"] = "any"


class KBHit(_Base):
    chunk: KBChunk
    score: float


class KnowledgeSearchOutput(_Base):
    query: str
    hits: list[KBHit]


# --- Tool 2: Asset & Maintenance Lookup ----------------------------------


class AssetLookupInput(_Base):
    requester_id: str  # identity is required: access rules depend on role
    asset_id: str | None = None
    asset_hint: str | None = None  # resolved against asset names in facility_id
    facility_id: str | None = None
    ticket_id: str | None = None  # "what's the status of WO-2026-0042?"
    my_tickets: bool = False  # "what's the status of my AC complaint?" - the requester's own tickets
    keyword: str | None = None  # narrows my_tickets, e.g. "access card"
    issue_category: TicketCategory | None = None  # narrows my_tickets
    history_limit: int = Field(default=5, ge=1, le=20)


class AssetLookupOutput(_Base):
    asset: Asset | None = None
    warranty_state: CoverageState | None = None
    amc: AMCContract | None = None
    amc_state: CoverageState | None = None
    last_service: MaintenanceRecord | None = None
    history: list[MaintenanceRecord] = Field(default_factory=list)
    open_tickets: list[Ticket] = Field(default_factory=list)
    ticket: Ticket | None = None  # populated for ticket-status lookups
    tickets: list[Ticket] = Field(default_factory=list)  # populated for "my tickets" lookups
    candidates: list[Asset] = Field(default_factory=list)  # ambiguous asset_hint


# --- Tool 3: Work Order / Complaint Creation -----------------------------

WORK_ORDER_REQUIRED_FIELDS: tuple[str, ...] = (
    "requester_id",
    "facility_id",
    "location",
    "issue_description",
    "issue_category",
)


class WorkOrderDraft(_Base):
    """A ticket being assembled across turns. Persisted in graph state."""

    ticket_type: TicketType | None = None  # derived from requester role if absent
    requester_id: str | None = None
    facility_id: str | None = None
    asset_id: str | None = None
    asset_hint: str | None = None  # "lift 2" - resolved to asset_id for staff
    location: str | None = None
    floor: str | None = None  # derived from location / asset, never asked of the LLM
    issue_description: str | None = None
    issue_category: TicketCategory | None = None
    priority: Priority | None = None  # derived by rule table, not by the LLM
    priority_reason: str | None = None

    def missing_fields(self) -> list[str]:
        return [f for f in WORK_ORDER_REQUIRED_FIELDS if getattr(self, f) in (None, "")]


class WorkOrderOutput(_Base):
    draft: WorkOrderDraft
    created: Ticket | None = None
    duplicates: list[Ticket] = Field(default_factory=list)


# --- Tool 4: Compliance Status -------------------------------------------


class ComplianceStatusInput(_Base):
    requester_id: str
    facility_id: str | None = None  # defaults to the requester's facility
    # due_soon = due + upcoming: everything needing attention that is not yet overdue
    status_filter: Literal["all", "overdue", "due_soon", "due", "upcoming"] = "all"
    cert_type: CertificateType | None = None


class ComplianceItem(_Base):
    certificate: ComplianceCertificate
    state: ComplianceState
    days_to_expiry: int  # negative when overdue
    renewal_should_start_on: date
    related_open_tickets: list[str] = Field(default_factory=list)  # not-closed tickets on the same asset


class ComplianceStatusOutput(_Base):
    facility: Facility
    as_of: date
    items: list[ComplianceItem]
    counts: dict[ComplianceState, int]


# --- Uniform envelope written into graph state ---------------------------


class ToolResult(_Base):
    """Uniform envelope returned by every tool and appended to
    `state['tool_results']` by the graph nodes.

    `data` holds the JSON-mode model_dump() of the tool's *Output model. It is
    the ONLY material the `respond` node may state as fact.
    """

    tool: Intent
    status: ToolStatus
    summary: str  # deterministic one-liner built in Python
    data: dict[str, Any] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    follow_up_question: str | None = None  # set for NEEDS_INPUT / NEEDS_CONFIRMATION
    error: str | None = None
