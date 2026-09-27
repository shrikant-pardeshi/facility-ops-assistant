"""
Graph assembly: nodes + edges + conditional routing + checkpointer.

    START -> understand -(route_after_understand)-> check_requirements | work_order_commit | respond
    check_requirements -(route_after_check)-> ask_for_info | knowledge_search | asset_lookup
                                              | compliance_status | work_order_prepare
    <each tool node> -> advance_plan
    ask_for_info     -> advance_plan
    advance_plan -(route_after_advance)-> check_requirements | respond
    respond -> END
"""

from __future__ import annotations

from typing import cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from facility_ops import schemas, state
from facility_ops.graph import nodes
from facility_ops.graph.routing import (
    ADVANCE_PLAN,
    ASK_FOR_INFO,
    ASSET_LOOKUP,
    CHECK_REQUIREMENTS,
    COMPLIANCE_STATUS,
    KNOWLEDGE_SEARCH,
    RESPOND,
    UNDERSTAND,
    WORK_ORDER_COMMIT,
    WORK_ORDER_PREPARE,
    route_after_advance,
    route_after_check,
    route_after_understand,
)
from facility_ops.state import AgentState, SessionContext

TOOL_NODES = (KNOWLEDGE_SEARCH, ASSET_LOOKUP, COMPLIANCE_STATUS, WORK_ORDER_PREPARE, WORK_ORDER_COMMIT)


def _checkpoint_types() -> list[tuple[str, str]]:
    """Our Pydantic models / enums that are stored in graph state.

    LangGraph only deserialises allow-listed custom types from checkpoints,
    so every state class must be registered here.
    """
    from enum import Enum

    from pydantic import BaseModel

    allowed = []
    for module in (schemas, state):
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, (BaseModel, Enum))
                and obj.__module__ == module.__name__
            ):
                allowed.append((module.__name__, name))
    return allowed


def make_checkpointer() -> MemorySaver:
    serde = JsonPlusSerializer(allowed_msgpack_modules=_checkpoint_types())
    return MemorySaver(serde=serde)


def build_graph(checkpointer=None):
    """Compile the Facility Operations Assistant graph.

    `checkpointer` persists state per thread_id between Streamlit reruns.
    Defaults to in-memory; swap for SqliteSaver to survive app restarts.
    """
    g = StateGraph(AgentState)

    g.add_node(UNDERSTAND, nodes.understand)
    g.add_node(CHECK_REQUIREMENTS, nodes.check_requirements)
    g.add_node(ASK_FOR_INFO, nodes.ask_for_info)
    g.add_node(KNOWLEDGE_SEARCH, nodes.knowledge_search)
    g.add_node(ASSET_LOOKUP, nodes.asset_lookup)
    g.add_node(COMPLIANCE_STATUS, nodes.compliance_status)
    g.add_node(WORK_ORDER_PREPARE, nodes.work_order_prepare)
    g.add_node(WORK_ORDER_COMMIT, nodes.work_order_commit)
    g.add_node(ADVANCE_PLAN, nodes.advance_plan)
    g.add_node(RESPOND, nodes.respond)

    g.add_edge(START, UNDERSTAND)
    g.add_conditional_edges(
        UNDERSTAND,
        route_after_understand,
        [CHECK_REQUIREMENTS, WORK_ORDER_COMMIT, RESPOND],
    )
    g.add_conditional_edges(
        CHECK_REQUIREMENTS,
        route_after_check,
        [ASK_FOR_INFO, KNOWLEDGE_SEARCH, ASSET_LOOKUP, COMPLIANCE_STATUS, WORK_ORDER_PREPARE, ADVANCE_PLAN],
    )
    for node in (*TOOL_NODES, ASK_FOR_INFO):
        g.add_edge(node, ADVANCE_PLAN)
    g.add_conditional_edges(ADVANCE_PLAN, route_after_advance, [CHECK_REQUIREMENTS, RESPOND])
    g.add_edge(RESPOND, END)

    return g.compile(checkpointer=checkpointer or make_checkpointer())


def thread_config(thread_id: str) -> RunnableConfig:
    """LangGraph config that selects one conversation's checkpointed state."""
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}


def run_turn(graph, thread_id: str, user_text: str, session: SessionContext | None = None) -> AgentState:
    """Send one user message through the graph and return the final state.

    `session` is the signed-in user and working facility. The UI always passes
    it; the CLI and tests may omit it, in which case the user is identified from
    the chat (they are asked for their ID when a tool needs it).
    """
    update: dict = {"messages": [HumanMessage(content=user_text)]}
    if session is not None:
        update["session"] = session
    result = graph.invoke(update, thread_config(thread_id))
    return cast(AgentState, result)


def mermaid() -> str:
    """Mermaid source of the compiled graph (used in README / docs)."""
    return build_graph().get_graph().draw_mermaid()
