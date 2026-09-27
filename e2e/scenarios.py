"""
End-to-end scenarios, run in a real browser by e2e/run_e2e.py.

Each Scenario is one conversation. The runner signs in as `Scenario.user`
(at `Scenario.facility`, default: the user's registered site) on the sign-in
screen, so no message needs to carry an ID. Each Step sends one message and
checks the assistant's reply; a Step with `switch_facility` instead changes the
working facility in the sidebar and checks the note that appears in the chat.
A Scenario with `signin_expect` only tests the sign-in screen itself.

What a check looks at: the full text of the latest assistant message *with
its "Agent actions" panel expanded*: the answer, follow-up questions,
sources line, tool status badges, graph path and tool data tables.
Checks target stable facts (record IDs, statuses, node names), not the
model's wording, so they hold for real Gemini runs.

All patterns are case-insensitive regular expressions.

`fake` is the routing decision the scripted stand-in model returns in
offline mode (--fake-llm). It is ignored in real-LLM runs.

Scenarios run in order on a fresh database. Checks for newly created tickets
accept any number above the seed data (NEW_CMP / NEW_WO), so running a subset
with --only works too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from facility_ops.schemas import ExtractedEntities, Intent, IntentDecision

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


@dataclass
class Step:
    say: str  # the message to send (or, with switch_facility, a label for the report)
    expect: list[str] = field(default_factory=list)  # every pattern must match
    expect_any: list[str] = field(default_factory=list)  # at least one must match
    reject: list[str] = field(default_factory=list)  # none may match
    sidebar_expect: list[str] = field(default_factory=list)
    sidebar_reject: list[str] = field(default_factory=list)
    fake: IntentDecision | None = None
    switch_facility: str | None = None  # change the sidebar "Working facility" instead of chatting


@dataclass
class Scenario:
    id: str
    group: str
    title: str
    steps: list[Step]
    user: str = "EMP-1024"  # signed in before the scenario starts
    facility: str | None = None  # None = the user's registered facility
    signin_expect: list[str] = field(default_factory=list)  # sign-in should FAIL with this text
    demo: bool = False  # good material for the demo video


def Switch(facility: str, *expect: str) -> Step:
    return Step(f"(switch working facility to {facility})", expect=list(expect), switch_facility=facility)


@dataclass
class EnvScenario:
    """Starts a separate app instance with environment overrides."""

    id: str
    title: str
    env: dict[str, str]
    page_expect: list[str] = field(default_factory=list)  # text on the page after load
    chat_disabled: bool | None = None
    steps: list[Step] = field(default_factory=list)
    needs_real_llm: bool = False
    use_fake_wrapper: bool = False
    user: str | None = None  # sign in before the chat checks


def D(*intents: Intent, confirmation: Literal["yes", "no", "none"] = "none", search_query: str | None = None, **entities) -> IntentDecision:
    return IntentDecision(
        intents=list(intents),
        entities=ExtractedEntities(**entities),
        confirmation=confirmation,
        search_query=search_query,
        rationale="scripted (offline e2e)",
    )


def day(iso: str) -> str:
    """Regex matching a date written as 2026-10-31, 31-Oct-2026, 31 Oct 2026 or October 31, 2026."""
    d = date.fromisoformat(iso)
    mon, month = d.strftime("%b"), d.strftime("%B")
    return (
        rf"({re.escape(iso)}|0?{d.day}[-\s]{mon}\w*[-\s,]*{d.year}|{month}\s+0?{d.day}(st|nd|rd|th)?,?\s+{d.year})"
    )


K, A, W, C = Intent.KNOWLEDGE_SEARCH, Intent.ASSET_LOOKUP, Intent.CREATE_WORK_ORDER, Intent.COMPLIANCE_STATUS
TALK, OOS = Intent.SMALL_TALK, Intent.OUT_OF_SCOPE

AWAITS_YES = r"Reply \W*yes"
DUPLICATE = r"duplicate - not created"
REFUSED = r"not permitted"
NOT_FOUND = r"not found"
# A ticket created during the run: numbered above the seed data (last seed: CMP-2026-0017, WO-2026-0034).
# Order-independent, so --only subsets pass too; checking "newer than seed" still proves a new ticket.
NEW_CMP = r"CMP-2026-(001[89]|00[2-9]\d|0[1-9]\d\d)"
NEW_WO = r"WO-2026-(003[5-9]|00[4-9]\d|0[1-9]\d\d)"

# ---------------------------------------------------------------------------
# Chat scenarios
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [
    # --- A. Knowledge search -------------------------------------------------
    Scenario("A1", "Knowledge search", "Chiller trip reset SOP", demo=True, steps=[
        Step("How do I reset a chiller trip alarm?",
             expect=["SOP-HVAC-002", r"reset", r"knowledge_search"],
             fake=D(K, search_query="reset chiller trip alarm")),
    ]),
    Scenario("A2", "Knowledge search", "Fire NOC renewal documents", steps=[
        Step("What documents are needed for fire NOC renewal?",
             expect=["REG-FIRE-001", r"Form B"],
             fake=D(K, search_query="fire NOC renewal documents")),
    ]),
    Scenario("A3", "Knowledge search", "When Form B is due", steps=[
        Step("When is Form B due?",
             expect=["REG-FIRE-002", r"January", r"July"],
             fake=D(K, search_query="Form B half-yearly certificate January July")),
    ]),
    Scenario("A4", "Knowledge search", "Follow-up keeps context (as an occupant)", user="OCC-2003", steps=[
        Step("What are the lift inspection rules?",
             expect=["REG-LIFT-001"],
             fake=D(K, search_query="lift licence periodic inspection rules")),
        Step("And what if there's an accident?",
             expect=[r"accident"], expect_any=["REG-LIFT-001", "SOP-LIFT-001"],
             fake=D(K, search_query="lift accident reporting inspector")),
    ]),
    Scenario("A5", "Knowledge search", "Unknown topic is not invented", demo=True, steps=[
        Step("What's the visitor parking policy?",
             expect=[NOT_FOUND], reject=[r"SOP-\w+-\d{3}", r"REG-\w+-\d{3}"],
             fake=D(K, search_query="visitor parking policy")),
    ]),

    Scenario("A6", "Knowledge search", "Answers from .txt, .docx and .pdf documents", user="OCC-2001", steps=[
        Step("My access card is not working at the L5 door, what should I check?",
             expect=["SOP-SEC-001"],
             fake=D(K, search_query="access card reader not working first checks")),
        Step("How often are the washrooms cleaned?",
             expect=["SOP-HK-001", r"2 hours"],
             fake=D(K, search_query="washroom cleaning frequency")),
        Step("How often do we need to hold a fire drill?",
             expect=["REG-FIRE-003", r"twice a year"],
             fake=D(K, search_query="fire drill frequency")),
    ]),

    # --- B. Asset & maintenance lookup --------------------------------------
    Scenario("B1", "Asset lookup", "Asset card, name resolution, follow-ups, disambiguation", demo=True, steps=[
        Step("What's the AMC and service status of AST-CHL-001?",
             expect=["AMC-001", r"comprehensive", day("2027-03-31")],
             fake=D(A, asset_id="AST-CHL-001")),
        Step("Is chiller 2 under AMC?",
             expect=["AST-CHL-002", "AMC-002", day("2026-10-31"), r"expir"],
             fake=D(A, asset_hint="chiller 2")),
        Step("Check AHU L12",
             expect=["AST-AHU-012", "AMC-005", "WO-2026-0027", r"expired"],
             fake=D(A, asset_hint="AHU L12")),
        Step("When was it last serviced?",
             expect=["AST-AHU-012", day("2026-06-20")],
             fake=D(A)),
        Step("What about AHU L9?",
             expect=["AST-AHU-009", day("2028-02-09")],
             fake=D(A, asset_hint="AHU L9")),
        Step("Fire pump AMC?",
             expect=["AST-FPP-001", "AST-FPP-002", "AST-FPP-003", r"needs input"],
             fake=D(A, asset_hint="fire pump")),
        Step("The jockey pump",
             expect=["AST-FPP-003", "AMC-014"],
             fake=D(A, asset_hint="jockey pump")),
        Step("Status of WO-2026-0029?",
             expect=["WO-2026-0029", r"in.progress"],
             fake=D(A, ticket_id="WO-2026-0029")),
        Step("Check AST-CHL-009",
             expect=[NOT_FOUND],
             fake=D(A, asset_id="AST-CHL-009")),
    ]),

    Scenario("B2", "Asset lookup", "Ticket status without a ticket number", user="OCC-2001", demo=True, steps=[
        Step("What's the status of my complaints?",
             expect=["CMP-2026-0017", "CMP-2026-0009"], reject=["CMP-2026-0014"],
             fake=D(A, my_tickets=True)),
        Step("And my access card issue?",
             expect=["CMP-2026-0017", r"assigned"], reject=["CMP-2026-0014"],
             fake=D(A, my_tickets=True, keyword="access card")),
    ]),

    # --- C. Compliance and working facility ----------------------------------
    Scenario("C1", "Compliance", "Overdue, due soon, single certificate", user="EMP-1001", demo=True, steps=[
        Step("Which compliance items are overdue?",
             expect=["CERT-002", "CERT-006", "WO-2026-0012"], reject=["CERT-004"],
             fake=D(C, compliance_filter="overdue")),
        Step("What's due soon?",
             # due (<= renewal lead time) AND upcoming (<= 90 days); overdue excluded
             expect=["CERT-004", "CERT-010", "CERT-011", "CERT-001"], reject=["CERT-002", "CERT-006"],
             fake=D(C, compliance_filter="due_soon")),
        Step("When does our fire NOC expire?",
             expect=["CERT-001", day("2026-12-15")],
             fake=D(C, cert_type="fire_noc")),
    ]),
    Scenario("C2", "Compliance", "Staff registered at the second facility", user="EMP-1101", steps=[
        Step("What's our compliance status?",
             expect=["CERT-015", r"Deccan"], reject=["CERT-002"],
             fake=D(C)),
    ]),
    Scenario("C3", "Working facility", "Staff switch sites mid-conversation", demo=True, steps=[
        Step("Which compliance items are overdue?",
             expect=["CERT-002"], reject=["CERT-015"],
             fake=D(C, compliance_filter="overdue")),
        Switch("FAC-002", r"Switched working facility", r"Deccan"),
        Step("And what's overdue at this site?",
             expect=["CERT-015", r"Deccan"], reject=["CERT-002"],
             fake=D(C, compliance_filter="overdue")),
        Step("Is the AMC on lift B active?",
             expect=["AST-LFT-102"],
             fake=D(A, asset_hint="lift B")),
    ]),

    # --- D. Work orders & complaints -----------------------------------------
    Scenario("D1", "Work orders", "Complaint: asks where, confirms, then duplicate", user="OCC-2001", demo=True,
             steps=[
        Step("The AC isn't cooling, please log a complaint",
             expect=[r"location", r"work_order_prepare"],
             fake=D(W, issue_description="AC not cooling", issue_category="hvac")),
        Step("L5 east wing conference room",
             expect=[AWAITS_YES, r"P3"],
             fake=D(W, location="L5, east wing conference room")),
        Step("yes",
             expect=[NEW_CMP, r"work_order_commit"],
             fake=D(TALK, confirmation="yes")),
        Step("Please log a complaint: AC not cooling in the L5 east wing conference room",
             expect=[NEW_CMP, DUPLICATE],
             fake=D(W, location="L5 east wing conference room", issue_description="AC not cooling",
                    issue_category="hvac")),
    ]),
    Scenario("D2", "Work orders", "Duplicate of an in-progress work order", user="EMP-1052", demo=True, steps=[
        Step("Lift 2 keeps stopping at L7, raise a ticket",
             expect=["WO-2026-0031", DUPLICATE],
             fake=D(W, asset_hint="lift 2", issue_description="keeps stopping at L7")),
    ]),
    Scenario("D3", "Work orders", "A resolved ticket still blocks", user="EMP-1045", steps=[
        Step("DG 2 battery alarm again, raise a work order",
             expect=["WO-2026-0034", DUPLICATE],
             fake=D(W, asset_hint="DG 2", issue_description="battery alarm again")),
    ]),
    Scenario("D4", "Work orders", "A closed ticket does not block", user="EMP-1031", steps=[
        Step("Chiller 1 tripped again, raise a work order",
             expect=[AWAITS_YES, r"P2"],
             fake=D(W, asset_hint="chiller 1", issue_description="tripped again")),
        Step("yes",
             expect=[NEW_WO],
             fake=D(TALK, confirmation="yes")),
    ]),
    Scenario("D5", "Work orders", "Asks for the floor, then finds a duplicate", user="OCC-2004", steps=[
        Step("The pantry lights are flickering, log a complaint",
             expect_any=[r"floor", r"location"],
             fake=D(W, location="pantry", issue_description="pantry lights flickering",
                    issue_category="electrical")),
        Step("L6",
             expect=["CMP-2026-0015", DUPLICATE],
             fake=D(W, location="L6")),
    ]),
    Scenario("D6", "Work orders", "Cancel a draft", steps=[
        Step("AHU L9 filter alarm keeps coming, raise a work order",
             expect=[AWAITS_YES],
             fake=D(W, asset_hint="AHU L9", issue_description="filter alarm keeps coming")),
        Step("no",
             expect=[r"cancel"], reject=[r"Created WO-"],
             fake=D(TALK, confirmation="no")),
    ]),
    Scenario("D7", "Work orders", "Question while a draft is pending, then confirm", user="EMP-1045", steps=[
        Step("The transformer is making a loud humming noise, raise a work order",
             expect=[AWAITS_YES],
             fake=D(W, asset_hint="transformer", issue_description="loud humming noise from the transformer")),
        Step("Before that, what should I do during a mains power failure?",
             expect=["SOP-ELEC-001", AWAITS_YES],
             fake=D(K, search_query="mains power failure immediate actions")),
        Step("yes, create it",
             expect=[NEW_WO],
             fake=D(TALK, confirmation="yes")),
    ]),
    Scenario("D8", "Work orders", "Emergency: lift entrapment", user="OCC-2002", demo=True, steps=[
        Step("Two people are trapped in lift 3, L9 lobby",
             expect=["SOP-LIFT-001", r"P1", r"Emergency", AWAITS_YES],
             fake=D(W, location="L9 lift lobby", issue_description="two people are trapped in lift 3",
                    issue_category="lift")),
        Step("yes",
             expect=[NEW_CMP, r"P1"], reject=[r"work order\W+CMP-"],  # it's a complaint
             fake=D(TALK, confirmation="yes")),
    ]),
    Scenario("D9", "Work orders", "Emergency: smoke", user="EMP-1060", steps=[
        Step("Smoke coming from the L3 electrical riser, raise a work order",
             expect=[r"P1", "SOP-FIRE-001"],
             fake=D(W, location="L3 electrical riser", issue_description="smoke coming from the electrical riser",
                    issue_category="electrical")),
    ]),
    Scenario("D10", "Work orders", "Housekeeping is P4", user="OCC-2002", steps=[
        Step("Meeting room 9B on L9 wasn't cleaned this morning, log a complaint",
             expect=[r"P4", AWAITS_YES],
             fake=D(W, location="L9, meeting room 9B", issue_description="meeting room not cleaned this morning",
                    issue_category="housekeeping")),
    ]),
    Scenario("D11", "Working facility", "Staff raise a work order at another site", facility="FAC-002", demo=True,
             steps=[
        Step("Lift B door is not closing properly, please raise a work order",
             expect=["AST-LFT-102", r"P2", AWAITS_YES],
             fake=D(W, asset_hint="Lift B", issue_description="Lift B door is not closing properly")),
        Step("yes",
             expect=[NEW_WO, "FAC-002"],
             fake=D(TALK, confirmation="yes")),
    ]),

    # --- E. Access control (option B) ----------------------------------------
    Scenario("E1", "Access control", "Occupant cannot see AMC", user="OCC-2001", demo=True, steps=[
        Step("What's the AMC status of chiller 1?",
             expect=[REFUSED], reject=["AMC-001", "CoolTech"],
             fake=D(A, asset_hint="chiller 1")),
    ]),
    Scenario("E2", "Access control", "Occupant cannot see compliance", user="OCC-2001", steps=[
        Step("Which compliance items are overdue?",
             expect=[REFUSED], reject=[r"CERT-0\d\d"],
             fake=D(C, compliance_filter="overdue")),
    ]),
    Scenario("E3", "Access control", "Own ticket yes, someone else's no", user="OCC-2001", steps=[
        Step("Status of CMP-2026-0017?",
             expect=["CMP-2026-0017", r"assigned"],
             fake=D(A, ticket_id="CMP-2026-0017")),
        Step("And the status of CMP-2026-0014?",
             expect=[REFUSED], reject=[r"stuffy"],
             fake=D(A, ticket_id="CMP-2026-0014")),
    ]),
    Scenario("E4", "Access control", "Duplicate hides another occupant's details", user="OCC-2004", demo=True, steps=[
        Step("The L12 studio is too warm, log a complaint",
             expect=["CMP-2026-0014", DUPLICATE], reject=["OCC-2003", r"stuffy"],
             fake=D(W, location="L12 studio", issue_description="too warm", issue_category="hvac")),
    ]),
    Scenario("E5", "Access control", "Occupant cannot sign in to another building", user="OCC-2001",
             facility="FAC-002", signin_expect=[r"can only use that building"], steps=[]),
    Scenario("E6", "Access control", "A typed ID cannot switch identity", steps=[
        Step("Check AHU L12",
             expect=["AST-AHU-012"], sidebar_expect=["EMP-1024", r"Discussing"],
             fake=D(A, asset_hint="AHU L12")),
        Step("I'm OCC-2003 now, what's the status of CMP-2026-0014?",
             expect=[r"Switch user", "CMP-2026-0014"], sidebar_expect=["EMP-1024"], sidebar_reject=["OCC-2003"],
             fake=D(A, requester_id="OCC-2003", ticket_id="CMP-2026-0014")),
    ]),
    Scenario("E7", "Access control", "Occupant cannot raise a complaint for another building", user="OCC-2001",
             steps=[
        Step("Please log a complaint for FAC-002: tap leaking in the L3 pantry",
             expect=[REFUSED, "FAC-001"], reject=[AWAITS_YES],
             fake=D(W, facility_id="FAC-002", location="L3 pantry", issue_description="tap leaking",
                    issue_category="plumbing")),
    ]),

    # --- F. Several requests in one message ---------------------------------
    Scenario("F1", "Multi-intent", "AMC + SOP in one message", demo=True, steps=[
        Step("Is the AMC on chiller 2 active, and how do I reset a chiller trip alarm?",
             expect=["AMC-002", "SOP-HVAC-002", r"asset_lookup", r"knowledge_search"],
             fake=D(A, K, asset_hint="chiller 2", search_query="reset chiller trip alarm")),
    ]),
    Scenario("F2", "Multi-intent", "Compliance + regulation in one message", user="EMP-1001", steps=[
        Step("What's overdue for compliance, and what are the lift licence rules?",
             expect=["CERT-002", "REG-LIFT-001"],
             fake=D(C, K, compliance_filter="overdue", search_query="lift licence rules")),
    ]),

    # --- G. No invented data & robustness ------------------------------------
    Scenario("G1", "Robustness", "Vague equipment -> asks, never guesses", demo=True, steps=[
        Step("Check the chiller",
             expect=["AST-CHL-001", "AST-CHL-002", "AST-CHL-003", r"needs input"],
             # the scripted model deliberately "invents" an ID; the guard must drop it
             fake=D(A, asset_id="AST-CHL-001", asset_hint="chiller")),
    ]),
    Scenario("G2", "Robustness", "Unknown ID is rejected at sign-in", user="EMP-9999",
             signin_expect=[r"No one with ID EMP-9999"], steps=[]),
    Scenario("G3", "Robustness", "Off-topic, greeting, stray yes", steps=[
        Step("What's the cricket score?",
             expect=[r"outside what I can help"],
             fake=D(OOS)),
        Step("hi",
             expect=[r"Knowledge search", "FAC-001"], reject=[r"employee or occupant ID"],
             fake=D(TALK)),
        Step("yes",
             expect=[r"nothing waiting"],
             fake=D(TALK, confirmation="yes")),  # a "yes" with nothing pending
    ]),
]

# ---------------------------------------------------------------------------
# Environment scenarios (separate app instance each)
# ---------------------------------------------------------------------------

ENV_SCENARIOS: list[EnvScenario] = [
    EnvScenario(
        "H1", "No API key -> banner, chat disabled",
        env={"GOOGLE_API_KEY": "", "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "LLM_PROVIDER": "gemini"},
        page_expect=[r"Language model not configured"],
        chat_disabled=True,
        user="EMP-1024",
    ),
    EnvScenario(
        "H2", "Unknown model name -> friendly error",
        env={"LLM_PROVIDER": "gemini", "GEMINI_MODEL": "gemini-nonexistent-model"},
        steps=[Step("hello", expect_any=[r"not available", r"call failed", r"rejected"])],
        needs_real_llm=True,
        user="EMP-1024",
    ),
    EnvScenario(
        "H3", "Missing seed data -> clear error page",
        env={"SEED_DIR": "e2e_missing_seed_dir"},  # DB_PATH is set to a fresh file by the runner
        page_expect=[r"Facility data could not be loaded", r"Seed file not found"],
    ),
]


def fake_decisions() -> dict[str, IntentDecision]:
    """Message text -> scripted decision, for offline runs. Texts must be unique."""
    mapping: dict[str, IntentDecision] = {}
    for sc in SCENARIOS:
        for st in sc.steps:
            key = st.say.strip()
            if st.fake is None or st.switch_facility:
                continue
            if key in mapping and mapping[key] != st.fake:
                raise ValueError(f"Scenario text reused with a different scripted decision: {key!r}")
            mapping[key] = st.fake
    for env in ENV_SCENARIOS:
        for st in env.steps:
            if st.fake is not None:
                mapping.setdefault(st.say.strip(), st.fake)
    return mapping
