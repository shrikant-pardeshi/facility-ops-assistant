"""
Conditional-edge functions. Pure functions of state -> next node name.

Keeping routing separate from node logic makes the control flow readable in
one place and unit-testable without an LLM.
"""

from __future__ import annotations

from facility_ops.schemas import Intent
from facility_ops.state import AgentState

# Node names (single source of truth, imported by builder.py)
UNDERSTAND = "understand"
CHECK_REQUIREMENTS = "check_requirements"
ASK_FOR_INFO = "ask_for_info"
KNOWLEDGE_SEARCH = "knowledge_search"
ASSET_LOOKUP = "asset_lookup"
COMPLIANCE_STATUS = "compliance_status"
WORK_ORDER_PREPARE = "work_order_prepare"
WORK_ORDER_COMMIT = "work_order_commit"
ADVANCE_PLAN = "advance_plan"
RESPOND = "respond"

INTENT_TO_NODE: dict[Intent, str] = {
    Intent.KNOWLEDGE_SEARCH: KNOWLEDGE_SEARCH,
    Intent.ASSET_LOOKUP: ASSET_LOOKUP,
    Intent.COMPLIANCE_STATUS: COMPLIANCE_STATUS,
    Intent.CREATE_WORK_ORDER: WORK_ORDER_PREPARE,
}


def route_after_understand(state: AgentState) -> str:
    """understand -> work_order_commit | check_requirements | respond"""
    decision = state.get("decision")
    awaiting = state.get("awaiting")

    # User said "yes" to a pending ticket draft -> create it.
    if (
        decision is not None
        and decision.confirmation == "yes"
        and state.get("pending_work_order") is not None
        and awaiting is not None
        and awaiting.kind == "work_order_confirmation"
    ):
        return WORK_ORDER_COMMIT

    if state.get("plan"):
        return CHECK_REQUIREMENTS

    # Small talk, out-of-scope, a "no" to a confirmation, or an error.
    return RESPOND


def route_after_check(state: AgentState) -> str:
    """check_requirements -> ask_for_info | <tool node>"""
    if state.get("missing_fields"):
        return ASK_FOR_INFO
    intent = state.get("current_intent")
    if intent is None or intent not in INTENT_TO_NODE:
        return ADVANCE_PLAN  # defensive: skip anything unroutable
    return INTENT_TO_NODE[intent]


def route_after_advance(state: AgentState) -> str:
    """advance_plan -> check_requirements (next intent) | respond"""
    return CHECK_REQUIREMENTS if state.get("plan") else RESPOND
