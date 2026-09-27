"""
Streamlit rendering helpers. Kept out of app.py so the entry point stays thin.

Chat history entries are plain dicts (JSON-safe):
    {"role": "user", "content": str}
    {"role": "assistant", "final": FinalAnswer dump, "results": [ToolResult dump], "trace": [TraceStep dump]}
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from facility_ops.schemas import FinalAnswer, Intent, ToolResult, ToolStatus
from facility_ops.state import AgentState

STATUS_BADGE = {
    ToolStatus.SUCCESS: "✅ success",
    ToolStatus.NOT_FOUND: "🔍 not found",
    ToolStatus.NEEDS_INPUT: "❓ needs input",
    ToolStatus.NEEDS_CONFIRMATION: "📝 awaiting confirmation",
    ToolStatus.DUPLICATE: "♻️ duplicate - not created",
    ToolStatus.NOT_PERMITTED: "🔒 not permitted",
    ToolStatus.ERROR: "⚠️ error",
}
TOOL_LABEL = {
    Intent.KNOWLEDGE_SEARCH: "Knowledge Search",
    Intent.ASSET_LOOKUP: "Asset & Maintenance Lookup",
    Intent.CREATE_WORK_ORDER: "Work Order / Complaint",
    Intent.COMPLIANCE_STATUS: "Compliance Status",
}
STATE_ICON = {"overdue": "🔴", "due": "🟠", "upcoming": "🟡", "valid": "🟢"}


def assistant_entry(state: AgentState) -> dict[str, Any]:
    """Convert a finished graph state into a history entry."""
    final = state.get("final_answer") or FinalAnswer(
        answer="⚠️ No answer was produced. Please try again.", generated_by="template"
    )
    return {
        "role": "assistant",
        "final": final.model_dump(mode="json"),
        "results": [r.model_dump(mode="json") for r in state.get("tool_results", [])],
        "trace": [t.model_dump(mode="json") for t in state.get("trace", [])],
    }


def note_entry(message: str) -> dict[str, Any]:
    """A system note in the chat (e.g. 'switched working facility')."""
    final = FinalAnswer(answer=f"ℹ️ {message}", generated_by="template")
    return {"role": "assistant", "final": final.model_dump(mode="json"), "results": [], "trace": []}


def error_entry(message: str) -> dict[str, Any]:
    final = FinalAnswer(answer=f"⚠️ {message}", generated_by="template")
    return {"role": "assistant", "final": final.model_dump(mode="json"), "results": [], "trace": []}


# ---------------------------------------------------------------------------
# Assistant message
# ---------------------------------------------------------------------------


def render_assistant(entry: dict[str, Any]) -> None:
    final = FinalAnswer.model_validate(entry["final"])
    results = [ToolResult.model_validate(r) for r in entry.get("results", [])]

    if any(r.data.get("emergency") for r in results):
        st.error("**Emergency (P1) reported.** Immediate safety steps from the SOP are shown first.", icon="🚨")

    if final.answer:
        st.markdown(final.answer)
    for question in final.follow_up_questions:
        st.info(question, icon="❓")

    if final.generated_by == "fallback":
        st.caption("⚠️ The language model was unavailable, so this is a plain summary of the records.")

    if final.recommendations:
        with st.container(border=True):
            st.markdown("**💡 Suggestions**  \n_AI-generated - not from the records. Verify before acting._")
            st.markdown("\n".join(f"- {r}" for r in final.recommendations))

    if final.citations:
        # One entry per source; section names are in the Agent actions panel.
        sections: dict[str, int] = {}
        for c in final.citations:
            sections[c.source_id] = sections.get(c.source_id, 0) + (1 if c.locator else 0)
        labels = [
            f"`{sid}`" + (f" ({n} sections)" if n > 1 else "") for sid, n in sections.items()
        ]
        st.caption("📎 **Sources:** " + " · ".join(labels))

    if results or entry.get("trace"):
        _render_agent_actions(results, entry.get("trace", []))


def _render_agent_actions(results: list[ToolResult], trace: list[dict]) -> None:
    label = f"🔧 Agent actions - {len(results)} tool result(s)" if results else "🔧 Agent actions"
    with st.expander(label):
        path = " → ".join(t["node"] for t in trace)
        if path:
            st.caption(f"Graph path: {path}")
        tab_results, tab_trace = st.tabs(["Tool results", "Trace"])
        with tab_results:
            if not results:
                st.caption("No tools were needed for this message.")
            for r in results:
                _render_result(r)
        with tab_trace:
            for t in trace:
                st.markdown(f"**{t['node']}**  \n{t['detail']}")


def _render_result(r: ToolResult) -> None:
    tool = TOOL_LABEL.get(r.tool, r.tool.value)
    if r.data.get("emergency"):
        tool += " (emergency guidance)"
    st.markdown(f"**{tool}** · {STATUS_BADGE.get(r.status, r.status.value)}  \n{r.summary}")
    if r.error:
        st.caption(f"Error detail: {r.error}")

    items = r.data.get("items")
    if r.tool is Intent.COMPLIANCE_STATUS and items:
        st.table(
            [
                {
                    "": STATE_ICON.get(i["state"], ""),
                    "Certificate": i["certificate"]["cert_id"],
                    "Title": i["certificate"]["title"],
                    "State": i["state"],
                    "Expiry": i["certificate"]["expiry_date"],
                    "Days": i["days_to_expiry"],
                    "Open tickets": ", ".join(i["related_open_tickets"]),
                }
                for i in items
            ],
            hide_index=True,
        )
    tickets = r.data.get("tickets")
    if r.tool is Intent.ASSET_LOOKUP and tickets:
        st.table(
            [
                {
                    "Ticket": t["ticket_id"],
                    "Category": t["category"],
                    "Priority": t["priority"],
                    "Status": t["status"],
                    "Updated": str(t["updated_at"])[:16].replace("T", " "),
                    "Description": t["description"],
                }
                for t in tickets
            ],
            hide_index=True,
        )
    history = r.data.get("history")
    if r.tool is Intent.ASSET_LOOKUP and history:
        st.table(
            [
                {
                    "Date": h["service_date"],
                    "Type": h["maintenance_type"],
                    "Description": h["description"],
                    "By": h["performed_by"],
                }
                for h in history
            ],
            hide_index=True,
        )
    if r.data:
        st.json(r.data, expanded=False)
    st.divider()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def render_session_context(values: dict) -> None:
    """What the conversation is currently about, and anything pending."""
    slots = values.get("slots")
    awaiting = values.get("awaiting")
    draft = values.get("pending_work_order")
    if not (slots and (slots.asset_id or slots.asset_hint or slots.ticket_id)) and not awaiting:
        st.caption("Nothing pending.")
    if slots and (slots.asset_id or slots.asset_hint or slots.ticket_id):
        st.caption(f"Discussing: {slots.asset_id or slots.ticket_id or slots.asset_hint}")

    if awaiting and awaiting.kind == "work_order_confirmation" and draft:
        st.warning(
            f"Draft {draft.ticket_type.value.replace('_', ' ') if draft.ticket_type else 'ticket'} "
            f"awaiting **yes/no**: {draft.issue_description} ({draft.priority.value if draft.priority else '-'})",
            icon="📝",
        )
    elif awaiting:
        st.info(f"Waiting for: {', '.join(awaiting.fields) or 'your answer'}", icon="❓")
