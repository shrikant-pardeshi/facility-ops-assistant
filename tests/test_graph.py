"""End-to-end graph tests with a scripted stand-in for the LLM.

The fake returns pre-written IntentDecisions (what Gemini would decide) and
builds answers from the tool summaries, so these tests exercise everything
except the model's own judgment: routing, multi-turn state, the ID guard,
access control, confirmation, emergency guidance and failure handling.
"""

from __future__ import annotations

import json
import uuid
from typing import Literal

import pytest

from facility_ops import llm
from facility_ops.graph.builder import build_graph, run_turn
from facility_ops.graph.router_tools import decision_to_tool_calls
from facility_ops.schemas import ExtractedEntities, Intent, IntentDecision, LLMAnswer, ToolStatus
from facility_ops.state import AgentState, SessionContext

pytestmark = pytest.mark.usefixtures("fresh_db")


class ScriptedLLM:
    def __init__(self):
        self.decisions: list = []
        self.fail_answers = False
        self.calls: list[str] = []

    def tool_calls(self, tools, messages):
        """Stands in for llm.invoke_tool_calls: the scripted decision, as the model's tool calls."""
        self.calls.append("tool_calls")
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return decision_to_tool_calls(item)

    def __call__(self, schema, messages):
        self.calls.append(schema.__name__)
        if schema is IntentDecision:
            item = self.decisions.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.fail_answers:
            raise llm.LLMCallError("The LLM provider did not respond in time. Please try again.")
        payload = json.loads(messages[-1].content.split("TOOL_RESULTS:\n", 1)[1])
        return LLMAnswer(
            answer=" / ".join(r["summary"] for r in payload),
            cited_source_ids=[c["source_id"] for r in payload for c in r["citations"]],
        )


def D(
    *intents: Intent,
    confirmation: Literal["yes", "no", "none"] = "none",
    search_query: str | None = None,
    **entities,
) -> IntentDecision:
    return IntentDecision(
        intents=list(intents),
        entities=ExtractedEntities(**entities),
        confirmation=confirmation,
        search_query=search_query,
        rationale="scripted",
    )


@pytest.fixture()
def fake(monkeypatch):
    f = ScriptedLLM()
    monkeypatch.setattr(llm, "invoke_structured", f)
    monkeypatch.setattr(llm, "invoke_tool_calls", f.tool_calls)
    return f


@pytest.fixture()
def chat(fake):
    graph = build_graph()
    thread = str(uuid.uuid4())

    def send(text: str, decision) -> AgentState:
        fake.decisions.append(decision)
        return run_turn(graph, thread, text)

    return send


def statuses(state) -> list[str]:
    return [r.status.value for r in state["tool_results"]]


# --------------------------------------------------------------------------


def test_complaint_over_three_turns(chat):
    s = chat(
        "AC not cooling in the L5 east wing conference room, please raise a complaint",
        D(
            Intent.CREATE_WORK_ORDER,
            location="L5, east wing conference room",
            issue_description="AC not cooling in the conference room",
            issue_category="hvac",
        ),
    )
    assert statuses(s) == ["needs_input"]
    assert "employee or occupant ID" in s["final_answer"].follow_up_questions[0]
    assert s["awaiting"].intent is Intent.CREATE_WORK_ORDER

    s = chat("OCC-2001", D(Intent.SMALL_TALK, requester_id="OCC-2001"))
    assert statuses(s) == ["needs_confirmation"]
    assert s["pending_work_order"].priority.value == "P3"
    assert "Reply **yes**" in s["final_answer"].follow_up_questions[0]

    s = chat("yes", D(Intent.SMALL_TALK, confirmation="yes"))
    assert statuses(s) == ["success"]
    assert s["tool_results"][0].data["created"]["ticket_id"] == "CMP-2026-0018"
    assert s["pending_work_order"] is None and s["awaiting"] is None
    assert "CMP-2026-0018" in s["messages"][-1].content


def test_multi_intent_in_one_message(chat):
    s = chat(
        "I'm EMP-1024. Is the AMC on AST-CHL-001 active, and how do I reset a chiller trip alarm?",
        D(
            Intent.ASSET_LOOKUP,
            Intent.KNOWLEDGE_SEARCH,
            requester_id="EMP-1024",
            asset_id="AST-CHL-001",
            search_query="reset chiller trip alarm",
        ),
    )
    assert [r.tool for r in s["tool_results"]] == [Intent.ASSET_LOOKUP, Intent.KNOWLEDGE_SEARCH]
    assert statuses(s) == ["success", "success"]
    cited = {c.source_id for c in s["final_answer"].citations}
    assert {"AST-CHL-001", "AMC-001", "SOP-HVAC-002"} <= cited
    nodes = [t.node for t in s["trace"]]
    assert nodes.count("check_requirements") == 2  # the plan loop ran twice


def test_follow_up_uses_remembered_asset(chat):
    chat("EMP-1024: status of AHU L12", D(Intent.ASSET_LOOKUP, requester_id="EMP-1024", asset_hint="AHU L12"))
    s = chat("and when was it last serviced?", D(Intent.ASSET_LOOKUP))
    assert s["tool_results"][0].data["asset"]["asset_id"] == "AST-AHU-012"


def test_invented_id_is_dropped(chat):
    # The model "helpfully" invents AST-CHL-001 though the user only said "chiller".
    s = chat(
        "EMP-1024 - check the chiller",
        D(Intent.ASSET_LOOKUP, requester_id="EMP-1024", asset_id="AST-CHL-001", asset_hint="chiller"),
    )
    understand = s["trace"][0].detail
    assert "DROPPED unverified IDs: asset_id=AST-CHL-001" in understand
    assert statuses(s) == ["needs_input"]  # 3 chillers -> ask which one
    assert "AST-CHL-003" in s["final_answer"].follow_up_questions[0]


def test_disambiguation_then_answer(chat):
    s = chat("EMP-1024, fire pump AMC?", D(Intent.ASSET_LOOKUP, requester_id="EMP-1024", asset_hint="fire pump"))
    assert s["awaiting"].kind == "asset_disambiguation"
    s = chat("AST-FPP-003", D(Intent.ASSET_LOOKUP, asset_id="AST-FPP-003"))
    assert statuses(s) == ["success"]
    assert s["tool_results"][0].data["asset"]["asset_id"] == "AST-FPP-003"


def test_identity_asked_before_compliance(chat):
    s = chat("What compliance items are overdue?", D(Intent.COMPLIANCE_STATUS, compliance_filter="overdue"))
    assert statuses(s) == ["needs_input"]
    s = chat("EMP-1001", D(Intent.SMALL_TALK, requester_id="EMP-1001"))
    assert statuses(s) == ["success"]
    # the filter was per-turn, so turn 2 shows everything; counts are always present
    assert s["tool_results"][0].data["counts"]["overdue"] == 2


def test_occupant_refused_compliance(chat):
    s = chat("OCC-2001 - is our fire NOC valid?", D(Intent.COMPLIANCE_STATUS, requester_id="OCC-2001"))
    assert statuses(s) == ["not_permitted"]
    assert "raise a complaint" in s["final_answer"].answer


def test_p1_emergency_shows_sop_steps_first(chat):
    s = chat(
        "OCC-2002 here - two people are trapped in lift 3, L9 lift lobby!",
        D(
            Intent.CREATE_WORK_ORDER,
            requester_id="OCC-2002",
            location="L9 lift lobby",
            issue_description="two people are trapped in lift 3",
            issue_category="lift",
        ),
    )
    first, second = s["tool_results"]
    assert first.data.get("emergency") is True and first.citations[0].source_id == "SOP-LIFT-001"
    assert second.status is ToolStatus.NEEDS_CONFIRMATION
    assert s["pending_work_order"].priority.value == "P1"
    s = chat("yes", D(Intent.SMALL_TALK, confirmation="yes"))
    assert s["tool_results"][0].data["created"]["priority"] == "P1"


def test_no_cancels_the_draft(chat):
    chat(
        "EMP-1031: raise a work order, chiller 1 tripped again",
        D(
            Intent.CREATE_WORK_ORDER,
            requester_id="EMP-1031",
            asset_hint="chiller 1",
            issue_description="chiller 1 tripped again",
        ),
    )
    s = chat("no, cancel", D(Intent.SMALL_TALK, confirmation="no"))
    assert s["pending_work_order"] is None and s["awaiting"] is None
    assert "cancelled" in s["tool_results"][0].summary


def test_unrelated_question_keeps_confirmation_pending(chat):
    chat(
        "EMP-1031: raise a work order, chiller 1 tripped again",
        D(Intent.CREATE_WORK_ORDER, requester_id="EMP-1031", asset_hint="chiller 1", issue_description="tripped"),
    )
    s = chat("how do I reset it?", D(Intent.KNOWLEDGE_SEARCH, search_query="reset chiller trip alarm"))
    assert s["awaiting"].kind == "work_order_confirmation"
    assert any("Reply **yes**" in q for q in s["final_answer"].follow_up_questions)
    s = chat("ok create it", D(Intent.SMALL_TALK, confirmation="yes"))
    assert s["tool_results"][0].data["created"]["ticket_id"] == "WO-2026-0035"


def test_duplicate_is_reported_not_created(chat):
    s = chat(
        "EMP-1052: lift 2 keeps stopping at L7, raise a ticket",
        D(Intent.CREATE_WORK_ORDER, requester_id="EMP-1052", asset_hint="lift 2", issue_description="stops at L7"),
    )
    assert statuses(s) == ["duplicate"]
    assert "WO-2026-0031" in s["final_answer"].answer
    assert s["pending_work_order"] is None


def test_knowledge_not_found_needs_no_identity(chat):
    s = chat("What's the visitor parking policy?", D(Intent.KNOWLEDGE_SEARCH, search_query="visitor parking policy"))
    assert statuses(s) == ["not_found"]


def test_small_talk_and_out_of_scope_use_templates(chat, fake):
    s = chat("hi", D(Intent.SMALL_TALK))
    assert s["final_answer"].generated_by == "template" and "Knowledge search" in s["final_answer"].answer
    s = chat("what's the cricket score?", D(Intent.OUT_OF_SCOPE))
    assert "outside what I can help with" in s["final_answer"].answer
    assert fake.calls.count("LLMAnswer") == 0  # no second LLM call needed


def test_llm_unavailable_in_understand(chat):
    s = chat("hello", llm.LLMConfigError("GOOGLE_API_KEY is not set."))
    assert s["final_answer"].answer.startswith("⚠️ GOOGLE_API_KEY is not set")
    assert s["tool_results"] == []


def test_llm_unavailable_in_respond_falls_back(chat, fake):
    fake.fail_answers = True
    s = chat("How do I reset a chiller trip?", D(Intent.KNOWLEDGE_SEARCH, search_query="reset chiller trip alarm"))
    final = s["final_answer"]
    assert final.generated_by == "fallback"
    assert "SOP-HVAC-002" in final.answer and "Reset procedure" in final.answer


def test_new_thread_starts_clean(fake):
    graph = build_graph()
    fake.decisions.append(D(Intent.SMALL_TALK, requester_id="EMP-1024"))
    run_turn(graph, "thread-a", "EMP-1024")
    fake.decisions.append(D(Intent.COMPLIANCE_STATUS))
    s = run_turn(graph, "thread-b", "what's overdue?")
    assert statuses(s) == ["needs_input"]  # identity not carried across conversations


def test_stray_yes_with_nothing_pending(chat):
    s = chat("yes", D(Intent.SMALL_TALK))
    assert "nothing waiting" in s["final_answer"].answer


def test_identity_change_clears_previous_context(chat):
    chat("EMP-1024: check AHU L12", D(Intent.ASSET_LOOKUP, requester_id="EMP-1024", asset_hint="AHU L12"))
    pending = chat(
        "EMP-1024: raise a work order, chiller 1 tripped",
        D(Intent.CREATE_WORK_ORDER, requester_id="EMP-1024", asset_hint="chiller 1", issue_description="tripped"),
    )
    assert pending["awaiting"].kind == "work_order_confirmation"  # a real draft is pending
    s = chat("I'm OCC-2003 now", D(Intent.SMALL_TALK, requester_id="OCC-2003"))
    assert s["slots"].requester_id == "OCC-2003"
    assert s["slots"].asset_id is None and s["slots"].asset_hint is None
    assert s["pending_work_order"] is None and s["awaiting"] is None
    assert "identity changed" in s["trace"][0].detail


@pytest.mark.parametrize("reply, decision_confirmation", [("no", "none"), ("never mind", "none"), ("I don't want to", "no")])
def test_declining_while_details_are_collected(chat, reply, decision_confirmation):
    s = chat("Start any draft", D(Intent.CREATE_WORK_ORDER))
    assert s["awaiting"].kind == "missing_fields"
    assert s["final_answer"].follow_up_questions[0].startswith("Please share:\n- ")  # bulleted list
    s = chat(reply, D(Intent.SMALL_TALK, confirmation=decision_confirmation))
    assert s["awaiting"] is None and s["pending_work_order"] is None
    assert "cancelled" in s["tool_results"][0].summary
    assert not s["final_answer"].follow_up_questions  # not asked again


# ------------------------------------------------------ signed-in sessions --


@pytest.fixture()
def signed_in(fake):
    """Chat as a signed-in user (the Streamlit path): identity comes from the session."""
    graph = build_graph()
    thread = str(uuid.uuid4())

    def send(text: str, decision, user: str = "EMP-1024", facility: str = "FAC-001") -> AgentState:
        fake.decisions.append(decision)
        return run_turn(graph, thread, text, session=SessionContext(requester_id=user, facility_id=facility))

    return send


def test_session_identity_used_without_asking(signed_in):
    s = signed_in("what compliance is overdue?", D(Intent.COMPLIANCE_STATUS, compliance_filter="overdue"))
    assert statuses(s) == ["success"]
    assert s["tool_results"][0].data["facility"]["facility_id"] == "FAC-001"


def test_session_complaint_needs_no_id(signed_in):
    s = signed_in(
        "AC not cooling in the L5 conference room",
        D(Intent.CREATE_WORK_ORDER, location="L5 conference room", issue_description="AC not cooling", issue_category="hvac"),
        user="OCC-2001",
    )
    assert statuses(s) == ["needs_confirmation"]
    assert s["pending_work_order"].requester_id == "OCC-2001"


def test_typed_id_cannot_switch_identity(signed_in):
    s = signed_in(
        "I'm OCC-2003, is chiller 2 under AMC?",
        D(Intent.ASSET_LOOKUP, requester_id="OCC-2003", asset_hint="chiller 2"),
        user="EMP-1024",
    )
    assert s["slots"].requester_id == "EMP-1024"
    assert statuses(s) == ["not_permitted", "success"]  # notice, then the lookup as EMP-1024
    assert "Switch user" in s["tool_results"][0].summary


def test_working_facility_follows_session(signed_in):
    s = signed_in("status of lift B", D(Intent.ASSET_LOOKUP, asset_hint="lift B"), facility="FAC-002")
    assert statuses(s) == ["success"]
    assert s["tool_results"][0].data["asset"]["asset_id"] == "AST-LFT-102"
    s = signed_in("what's overdue?", D(Intent.COMPLIANCE_STATUS, compliance_filter="overdue"), facility="FAC-001")
    assert s["tool_results"][0].data["facility"]["facility_id"] == "FAC-001"


def test_staff_work_order_at_switched_facility(signed_in):
    s = signed_in(
        "Lift B door isn't closing, raise a work order",
        D(Intent.CREATE_WORK_ORDER, asset_hint="Lift B", issue_description="Lift B door not closing"),
        facility="FAC-002",
    )
    assert statuses(s) == ["needs_confirmation"]
    s = signed_in("yes", D(Intent.SMALL_TALK, confirmation="yes"), facility="FAC-002")
    created = s["tool_results"][0].data["created"]
    assert created["facility_id"] == "FAC-002" and created["asset_id"] == "AST-LFT-102"


def test_capabilities_do_not_ask_for_id_when_signed_in(signed_in):
    s = signed_in("what can you do?", D(Intent.SMALL_TALK))
    assert "employee or occupant ID" not in s["final_answer"].answer
    assert "FAC-001" in s["final_answer"].answer


def test_ticket_status_without_an_id(signed_in):
    s = signed_in("What's the status of my complaints?", D(Intent.ASSET_LOOKUP, my_tickets=True), user="OCC-2001")
    assert statuses(s) == ["success"]
    assert "CMP-2026-0017" in s["tool_results"][0].summary
    s = signed_in(
        "And my access card issue?", D(Intent.ASSET_LOOKUP, my_tickets=True, keyword="access card"), user="OCC-2001"
    )
    assert s["tool_results"][0].data["ticket"]["ticket_id"] == "CMP-2026-0017"


def test_literal_newlines_from_the_model_are_rendered():
    from facility_ops.graph.nodes import _unescape

    assert _unescape(r"Draft:\n\n- Type: WO\n- Asset: X") == "Draft:\n\n- Type: WO\n- Asset: X"
    assert _unescape("already\nfine") == "already\nfine"
