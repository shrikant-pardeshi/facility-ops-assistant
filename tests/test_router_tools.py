"""Native tool calling: the router tool schemas and the tool-call -> decision mapping."""

from __future__ import annotations

from typing import Literal

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from facility_ops import llm
from facility_ops.graph import router_tools
from facility_ops.graph.router_tools import ROUTER_TOOL_SPECS, ROUTER_TOOLS, decision_to_tool_calls, tool_calls_to_decision
from facility_ops.schemas import ExtractedEntities, Intent, IntentDecision


def D(*intents, confirmation: Literal["yes", "no", "none"] = "none", search_query=None, **entities) -> IntentDecision:
    return IntentDecision(
        intents=list(intents), entities=ExtractedEntities(**entities), confirmation=confirmation,
        search_query=search_query, rationale="t",
    )


def test_tool_schemas_are_valid_function_declarations():
    names = [convert_to_openai_tool(t)["function"]["name"] for t in ROUTER_TOOLS]
    assert names == [
        "search_knowledge_base", "lookup_asset", "check_compliance", "create_ticket",
        "answer_pending_question", "identify_user", "small_talk", "out_of_scope",
    ]
    for t in ROUTER_TOOLS:
        fn = convert_to_openai_tool(t)["function"]
        assert fn["description"].strip(), f"{fn['name']} needs a description the model can read"
        assert "requester" not in str(fn["parameters"]), "identity must never come from the model"


def test_bound_specs_have_no_refs():
    # Gemini ignores $defs, so enum types must be inlined into the bound specs.
    assert "$ref" not in str(ROUTER_TOOL_SPECS) and "$defs" not in str(ROUTER_TOOL_SPECS)
    lookup = next(s for s in ROUTER_TOOL_SPECS if s["function"]["name"] == "lookup_asset")
    assert "hvac" in str(lookup["function"]["parameters"]["properties"]["issue_category"])


def test_parallel_calls_become_an_ordered_plan():
    d = tool_calls_to_decision([
        {"name": "lookup_asset", "args": {"equipment_name": "chiller 2"}},
        {"name": "search_knowledge_base", "args": {"query": "reset chiller trip alarm"}},
    ])
    assert d.intents == [Intent.ASSET_LOOKUP, Intent.KNOWLEDGE_SEARCH]
    assert d.entities.asset_hint == "chiller 2" and d.search_query == "reset chiller trip alarm"
    assert "lookup_asset(equipment_name='chiller 2')" in d.rationale


def test_control_tools():
    d = tool_calls_to_decision([{"name": "answer_pending_question", "args": {"answer": "yes"}}])
    assert d.confirmation == "yes" and d.intents == [Intent.SMALL_TALK]
    assert tool_calls_to_decision([{"name": "out_of_scope", "args": {}}]).intents == [Intent.OUT_OF_SCOPE]
    d = tool_calls_to_decision([{"name": "identify_user", "args": {"person_id": "emp 1024"}}])
    assert d.entities.requester_id == "EMP-1024"


def test_bad_calls_are_tolerated():
    d = tool_calls_to_decision([
        {"name": "delete_everything", "args": {}},  # not a tool we offer
        {"name": "create_ticket", "args": {"issue_description": "tap leaking", "issue_category": "plumbingg"}},
    ])
    assert d.intents == [Intent.CREATE_WORK_ORDER]
    assert d.entities.issue_description == "tap leaking" and d.entities.issue_category is None
    assert tool_calls_to_decision([]).intents == [Intent.SMALL_TALK]


@pytest.mark.parametrize("decision", [
    D(Intent.KNOWLEDGE_SEARCH, search_query="fire NOC documents"),
    D(Intent.ASSET_LOOKUP, Intent.KNOWLEDGE_SEARCH, asset_hint="chiller 2", search_query="reset trip"),
    D(Intent.ASSET_LOOKUP, my_tickets=True, keyword="access card"),
    D(Intent.COMPLIANCE_STATUS, compliance_filter="due_soon", cert_type="fire_noc"),
    D(Intent.CREATE_WORK_ORDER, location="L5", issue_description="AC not cooling", issue_category="hvac"),
    D(Intent.SMALL_TALK, confirmation="no"),
    D(Intent.SMALL_TALK, requester_id="OCC-2001"),
    D(Intent.OUT_OF_SCOPE),
])
def test_round_trip(decision):
    back = tool_calls_to_decision(decision_to_tool_calls(decision))
    assert back.intents == decision.intents
    assert back.entities == decision.entities
    assert back.confirmation == decision.confirmation
    assert back.search_query == decision.search_query


def test_decide_uses_native_tool_calling(monkeypatch):
    seen = {}

    def fake(tools, messages):
        seen["tools"] = tools
        return [{"name": "check_compliance", "args": {"status_filter": "overdue"}}]

    monkeypatch.setattr(llm, "invoke_tool_calls", fake)
    d = router_tools.decide([])
    assert seen["tools"] is ROUTER_TOOL_SPECS
    assert d.intents == [Intent.COMPLIANCE_STATUS] and d.entities.compliance_filter == "overdue"


@pytest.mark.parametrize("raw, expected", [
    ("402 RESOURCE_EXHAUSTED. Your prepayment credits are depleted.", "run out of prepaid credits"),
    ("429 RESOURCE_EXHAUSTED. Quota exceeded", "rate limit or quota"),
    ("400 API key not valid. API_KEY_INVALID", "rejected the API key"),
])
def test_provider_errors_get_distinct_messages(raw, expected):
    # Credits running out must not look like a temporary rate limit (the e2e runner
    # retries rate limits but stops at once on account problems).
    assert expected in llm._friendly(Exception(raw))
