"""Routing is pure Python - testable without an LLM or database."""

from facility_ops.graph import routing as r
from facility_ops.schemas import Intent, IntentDecision, WorkOrderDraft
from typing import Literal, cast

from facility_ops.state import AgentState, AwaitingInput


def S(**values) -> AgentState:
    """Partial state for routing tests (routers only read a few keys)."""
    return cast(AgentState, values)


def _decision(*intents: Intent, confirmation: Literal["yes", "no", "none"] = "none") -> IntentDecision:
    return IntentDecision(intents=list(intents), confirmation=confirmation, rationale="test")


def test_tool_intent_goes_to_requirements_check():
    state = S(decision=_decision(Intent.ASSET_LOOKUP), plan=[Intent.ASSET_LOOKUP])
    assert r.route_after_understand(state) == r.CHECK_REQUIREMENTS


def test_small_talk_goes_straight_to_respond():
    state = S(decision=_decision(Intent.SMALL_TALK), plan=[])
    assert r.route_after_understand(state) == r.RESPOND


def test_confirmed_draft_goes_to_commit():
    state = S(
        decision=_decision(Intent.CREATE_WORK_ORDER, confirmation="yes"),
        plan=[],
        pending_work_order=WorkOrderDraft(requester_id="EMP-1024"),
        awaiting=AwaitingInput(
            kind="work_order_confirmation", intent=Intent.CREATE_WORK_ORDER, question="Create it?"
        ),
    )
    assert r.route_after_understand(state) == r.WORK_ORDER_COMMIT


def test_yes_without_pending_draft_does_not_commit():
    state = S(decision=_decision(Intent.SMALL_TALK, confirmation="yes"), plan=[])
    assert r.route_after_understand(state) == r.RESPOND


def test_missing_fields_trigger_clarification():
    state = S(current_intent=Intent.ASSET_LOOKUP, missing_fields=["asset_id"])
    assert r.route_after_check(state) == r.ASK_FOR_INFO


def test_each_tool_intent_routes_to_its_node():
    for intent, node in r.INTENT_TO_NODE.items():
        assert r.route_after_check(S(current_intent=intent, missing_fields=[])) == node


def test_multi_intent_plan_loops_back():
    assert r.route_after_advance(S(plan=[Intent.COMPLIANCE_STATUS])) == r.CHECK_REQUIREMENTS
    assert r.route_after_advance(S(plan=[])) == r.RESPOND
