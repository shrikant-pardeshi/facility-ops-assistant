"""
LangGraph state definition.

The state is split into two lifetimes:

PERSISTENT (survives across turns via the checkpointer, keyed by thread_id)
    messages            full chat transcript (reducer: add_messages)
    slots               entities collected so far (who, which facility, which asset)
    awaiting            what the assistant asked for last turn, if anything
    pending_work_order  a ticket draft waiting for missing fields or a "yes"
    session             signed-in user + working facility, supplied by the UI

PER-TURN (reset by the `understand` node at the start of every turn)
    decision, plan, current_intent, missing_fields, tool_results, trace,
    final_answer, error

Streamlit's "Clear conversation" simply starts a new thread_id, which gives a
fresh persistent state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import Field
from typing_extensions import TypedDict

from facility_ops.schemas import (
    FinalAnswer,
    Intent,
    IntentDecision,
    ToolResult,
    WorkOrderDraft,
    _Base,
)

# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class ConversationSlots(_Base):
    """Entity memory. Filled incrementally; later values overwrite earlier ones.

    Example: turn 1 "My AC isn't working" -> nothing; turn 2 "EMP-1024" ->
    requester_id; the facility_id is then auto-filled from the people table.
    """

    requester_id: str | None = None
    facility_id: str | None = None
    asset_id: str | None = None
    asset_hint: str | None = None
    ticket_id: str | None = None

    def known(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


class SessionContext(_Base):
    """Who is signed in and which site they are working on.

    Set by the UI (sign-in screen / facility switcher) and passed in with every
    turn. When present it is AUTHORITATIVE: the requester is never taken from
    chat text, so typing someone else's ID cannot switch identity.
    """

    requester_id: str
    facility_id: str


class AwaitingInput(_Base):
    """Set when a turn ends with a question to the user."""

    kind: Literal["missing_fields", "work_order_confirmation", "asset_disambiguation"]
    intent: Intent  # the intent to resume next turn
    fields: list[str] = Field(default_factory=list)
    question: str


class TraceStep(_Base):
    """One entry in the 'Agent actions' panel shown under each reply."""

    node: str
    detail: str
    at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    # ---- persistent -------------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]
    slots: ConversationSlots
    awaiting: AwaitingInput | None
    pending_work_order: WorkOrderDraft | None
    session: SessionContext | None  # signed-in user + working facility (set by the UI each turn)

    # ---- per-turn ---------------------------------------------------------
    decision: IntentDecision | None  # raw LLM routing decision
    plan: list[Intent]  # remaining tool intents, processed in order
    current_intent: Intent | None  # intent being handled right now
    missing_fields: list[str]  # output of check_requirements
    tool_results: list[ToolResult]  # every tool call this turn, in order
    trace: list[TraceStep]  # node-by-node log for the UI
    final_answer: FinalAnswer | None
    error: str | None  # fatal error surfaced to the UI


# Keys the `understand` node clears at the start of every turn.
PER_TURN_KEYS: tuple[str, ...] = (
    "decision",
    "plan",
    "current_intent",
    "missing_fields",
    "tool_results",
    "trace",
    "final_answer",
    "error",
)


def initial_state() -> AgentState:
    """Blank state for a brand-new conversation thread."""
    return AgentState(
        messages=[],
        slots=ConversationSlots(),
        awaiting=None,
        pending_work_order=None,
        session=None,
        decision=None,
        plan=[],
        current_intent=None,
        missing_fields=[],
        tool_results=[],
        trace=[],
        final_answer=None,
        error=None,
    )
