"""
Graph nodes. Each node takes the full AgentState and returns a partial update.

understand          LLM with the router tools bound (native tool calling); its
                    tool calls become an IntentDecision. Resets per-turn keys, guards IDs,
                    merges entities into `slots` / the ticket draft, builds `plan`.
check_requirements  Sets `current_intent = plan[0]` and lists missing inputs.
ask_for_info        Records a NEEDS_INPUT result with a templated question.
knowledge_search    Tool 1.
asset_lookup        Tool 2 (asset card or ticket status).
compliance_status   Tool 4.
work_order_prepare  Tool 3 phase 1 (validate, priority, duplicates, confirm).
                    For P1 emergencies it also attaches the SOP's immediate steps.
work_order_commit   Tool 3 phase 2 (after the user said "yes").
advance_plan        Pops the finished intent off `plan`.
respond             LLM -> LLMAnswer grounded ONLY in `tool_results`, with a
                    deterministic fallback if the LLM is unavailable.

Tools are called through the StructuredTool registry, and every failure is
captured as a ToolResult, so no node raises into the graph.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from facility_ops import llm, prompts
from facility_ops.config import get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.graph import router_tools
from facility_ops.logging_config import get_logger
from facility_ops.schemas import (
    ID_PATTERNS,
    Citation,
    ExtractedEntities,
    FinalAnswer,
    Intent,
    LLMAnswer,
    Priority,
    TicketCategory,
    ToolResult,
    ToolStatus,
    WorkOrderDraft,
)
from facility_ops.state import AgentState, AwaitingInput, ConversationSlots, SessionContext, TraceStep
from facility_ops.tools.common import needs_input
from facility_ops.tools.registry import TOOLS

log = get_logger("graph")

# Nodes return a PARTIAL update (only the keys they change); LangGraph merges it into AgentState.
StateUpdate = dict[str, Any]

MAX_INTENTS = 3
HISTORY_MESSAGES = 8  # recent messages sent to the router for context

# Which extracted entity answers which awaited field.
_FIELD_TO_ENTITIES: dict[str, tuple[str, ...]] = {
    "requester_id": ("requester_id",),
    "facility_id": ("facility_id",),
    "asset_id": ("asset_id", "asset_hint"),
    "ticket_id": ("ticket_id",),
    "location": ("location",),
    "floor": ("location",),
    "issue_description": ("issue_description",),
    "issue_category": ("issue_category",),
}
_DRAFT_FIELDS = ("location", "issue_description", "issue_category", "asset_id", "asset_hint")
_THANKS = re.compile(r"^\s*(thanks|thank you|thx|ok thanks|great,? thanks|cheers)\b", re.I)
_CANCEL = re.compile(
    r"^\s*(no|nope|cancel|stop|abort|never ?mind|forget (it|about it)|leave it)\s*[.!]*\s*$", re.I
)
_YES_NO = re.compile(r"^\s*(yes|y|yeah|yep|ok|okay|sure|confirm|go ahead|no|n|nope|cancel)\s*[.!]*\s*$", re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trace(state: AgentState, node: str, detail: str) -> list[TraceStep]:
    return [*state.get("trace", []), TraceStep(node=node, detail=detail)]


def _last_user_text(state: AgentState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""


def _slots(state: AgentState) -> ConversationSlots:
    return (state.get("slots") or ConversationSlots()).model_copy()


def identity_notice(session: SessionContext, typed_id: str) -> ToolResult:
    return ToolResult(
        tool=Intent.SMALL_TALK,
        status=ToolStatus.NOT_PERMITTED,
        summary=(
            f"You are signed in as {session.requester_id}; IDs typed in the chat (such as {typed_id}) "
            "are not used to change who is asking. To act as someone else, use 'Switch user' in the sidebar."
        ),
    )


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def guard_ids(entities: ExtractedEntities, user_text: str) -> tuple[ExtractedEntities, list[str]]:
    """Drop any ID the LLM returned that is malformed or that the user never typed."""
    entities = entities.model_copy()
    typed = _compact(user_text)
    dropped = []
    for field in ("requester_id", "facility_id", "asset_id", "ticket_id"):
        value = getattr(entities, field)
        if value and (not ID_PATTERNS[field].match(value) or _compact(value) not in typed):
            dropped.append(f"{field}={value}")
            setattr(entities, field, None)
    return entities, dropped


def _call_tool(name: str, intent: Intent, args: dict) -> ToolResult:
    """Invoke a registered StructuredTool; never raises."""
    try:
        return ToolResult.model_validate(TOOLS[name].invoke(args))
    except ValidationError as exc:
        log.warning("Invalid arguments for %s: %s", name, exc)
        return ToolResult(
            tool=intent,
            status=ToolStatus.ERROR,
            summary="The request had invalid values.",
            error=exc.errors()[0]["msg"],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Tool %s crashed", name)
        return ToolResult(
            tool=intent,
            status=ToolStatus.ERROR,
            summary="The tool failed unexpectedly.",
            error=f"{type(exc).__name__}: {exc}",
        )


def _awaiting_from(result: ToolResult) -> AwaitingInput | None:
    if result.status is ToolStatus.NEEDS_INPUT:
        candidates = bool(result.data.get("candidates"))
        return AwaitingInput(
            kind="asset_disambiguation" if candidates else "missing_fields",
            intent=result.tool,
            fields=result.data.get("missing_fields") or (["asset_id"] if candidates else []),
            question=result.follow_up_question or "",
        )
    if result.status is ToolStatus.NEEDS_CONFIRMATION:
        return AwaitingInput(
            kind="work_order_confirmation",
            intent=Intent.CREATE_WORK_ORDER,
            question=result.follow_up_question or "",
        )
    return None


def _record(state: AgentState, node: str, results: list[ToolResult], **updates) -> StateUpdate:
    """Append results + trace; set `awaiting` if a result asks the user something."""
    out: StateUpdate = {
        "tool_results": [*state.get("tool_results", []), *results],
        "trace": _trace(state, node, " | ".join(f"{r.status.value}: {r.summary}" for r in results)),
        **updates,
    }
    for r in results:
        awaiting = _awaiting_from(r)
        if awaiting:
            out["awaiting"] = awaiting
    return out


def _context_block(slots: ConversationSlots, awaiting: AwaitingInput | None, draft: WorkOrderDraft | None) -> str:
    lines = ["", "## CONTEXT (remembered by the system)"]
    known = slots.known()
    lines.append("KNOWN: " + (", ".join(f"{k}={v}" for k, v in known.items()) if known else "nothing yet"))
    if awaiting:
        lines.append(f"PENDING QUESTION (for intent {awaiting.intent.value}): {awaiting.question}")
    if draft:
        fields = draft.model_dump(exclude_none=True, include=set(_DRAFT_FIELDS) | {"priority"}, mode="json")
        lines.append(f"DRAFT TICKET: {fields}")
    return "\n".join(lines)


def _history(state: AgentState) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))]
    return msgs[-HISTORY_MESSAGES:]


# ---------------------------------------------------------------------------
# understand
# ---------------------------------------------------------------------------


def understand(state: AgentState) -> StateUpdate:
    text = _last_user_text(state)
    reset: AgentState = {
        "decision": None,
        "plan": [],
        "current_intent": None,
        "missing_fields": [],
        "tool_results": [],
        "final_answer": None,
        "error": None,
    }
    slots = _slots(state)
    awaiting = state.get("awaiting")
    draft = state.get("pending_work_order")
    if (session := state.get("session")) is not None:
        # Let the router see who is signed in and where, even on the first turn.
        slots.requester_id, slots.facility_id = session.requester_id, slots.facility_id or session.facility_id

    messages = [
        SystemMessage(content=prompts.UNDERSTAND_SYSTEM + _context_block(slots, awaiting, draft)),
        *_history(state),
    ]
    try:
        decision = router_tools.decide(messages)  # native tool calling -> IntentDecision
    except (llm.LLMConfigError, llm.LLMCallError) as exc:
        return {**reset, "error": str(exc), "trace": [TraceStep(node="understand", detail=f"LLM unavailable: {exc}")]}

    intents = list(dict.fromkeys(decision.intents))[:MAX_INTENTS] or [Intent.SMALL_TALK]
    entities, dropped = guard_ids(decision.entities, text)
    decision = decision.model_copy(update={"intents": intents, "entities": entities})

    # --- entity memory ------------------------------------------------------
    notes = []
    extra: list[ToolResult] = []
    if session is not None:
        # Signed-in identity is authoritative; chat text can't change it.
        if entities.requester_id and entities.requester_id != session.requester_id:
            extra.append(identity_notice(session, entities.requester_id))
            notes.append(f"ignored typed requester {entities.requester_id} (signed in as {session.requester_id})")
        entities = entities.model_copy(update={"requester_id": None})
        slots.requester_id = session.requester_id
        slots.facility_id = entities.facility_id or session.facility_id
        decision = decision.model_copy(update={"entities": entities})
    elif entities.requester_id and slots.requester_id and entities.requester_id != slots.requester_id:
        # A different person took over the conversation: drop everything tied
        # to the previous person so nothing carries across identities.
        slots = ConversationSlots()
        awaiting, draft = None, None
        notes.append("identity changed -> previous context cleared")
    if entities.requester_id:
        slots.requester_id = entities.requester_id
    if entities.facility_id:
        slots.facility_id = entities.facility_id
    wants_lookup = Intent.ASSET_LOOKUP in intents or (awaiting and awaiting.intent is Intent.ASSET_LOOKUP)
    if entities.ticket_id:
        slots.ticket_id, slots.asset_id, slots.asset_hint = entities.ticket_id, None, None
    elif entities.my_tickets and wants_lookup:
        slots.ticket_id, slots.asset_id, slots.asset_hint = None, None, None
    elif entities.asset_id:
        slots.asset_id, slots.asset_hint, slots.ticket_id = entities.asset_id, None, None
    elif entities.asset_hint and wants_lookup:
        slots.asset_hint, slots.asset_id, slots.ticket_id = entities.asset_hint, None, None

    # --- plan ---------------------------------------------------------------
    plan = [i for i in intents if i.is_tool_intent]
    new_awaiting: AwaitingInput | None = None
    new_draft = draft

    if awaiting and awaiting.kind == "work_order_confirmation" and draft:
        if decision.confirmation == "yes":
            plan = [i for i in plan if i is not Intent.CREATE_WORK_ORDER]
            new_awaiting = awaiting  # the router checks it to go to work_order_commit
            notes.append("confirmation=yes -> commit")
        elif decision.confirmation == "no":
            plan = [i for i in plan if i is not Intent.CREATE_WORK_ORDER]
            new_draft = None
            extra.append(
                ToolResult(
                    tool=Intent.CREATE_WORK_ORDER,
                    status=ToolStatus.SUCCESS,
                    summary="Draft cancelled at the user's request. No ticket was created.",
                )
            )
            notes.append("confirmation=no -> draft cancelled")
        elif Intent.CREATE_WORK_ORDER not in plan:
            new_awaiting = awaiting  # still waiting for yes/no
    elif awaiting and (decision.confirmation == "no" or _CANCEL.match(text)):
        # The user declined to continue while we were collecting details.
        plan = [i for i in plan if i is not awaiting.intent]
        if awaiting.intent is Intent.CREATE_WORK_ORDER:
            new_draft = None
        extra.append(
            ToolResult(
                tool=awaiting.intent,
                status=ToolStatus.SUCCESS,
                summary="Request cancelled at the user's request. Nothing was looked up or created.",
            )
        )
        notes.append(f"cancelled pending {awaiting.intent.value}")
    elif awaiting:
        supplied = any(
            getattr(entities, e, None) for f in awaiting.fields for e in _FIELD_TO_ENTITIES.get(f, (f,))
        )
        if awaiting.intent not in plan and (supplied or not plan):
            plan = [awaiting.intent, *plan]
            notes.append(f"resuming {awaiting.intent.value}")

    # --- work-order draft ---------------------------------------------------
    if Intent.CREATE_WORK_ORDER in plan:
        continuing = draft is not None and awaiting is not None and awaiting.intent is Intent.CREATE_WORK_ORDER
        base = draft if (continuing and draft is not None) else WorkOrderDraft()
        updates = {f: getattr(entities, f) for f in _DRAFT_FIELDS if getattr(entities, f)}
        if "asset_id" in updates:
            updates["asset_hint"] = None
        elif "asset_hint" in updates:
            updates["asset_id"] = None
        if entities.facility_id:
            updates["facility_id"] = entities.facility_id
        if (
            continuing
            and awaiting is not None
            and "floor" in awaiting.fields
            and base.location
            and updates.get("location")
            and base.location.lower() not in updates["location"].lower()
        ):
            # "pantry" + answer "L6" -> "L6, pantry": keep the area, add the floor
            updates["location"] = f"{updates['location']}, {base.location}"
        new_draft = base.model_copy(update=updates)

    detail = (
        f"intents={[i.value for i in intents]} confirmation={decision.confirmation} "
        f"entities={entities.model_dump(exclude_none=True, mode='json')} - {decision.rationale[:200]}"
    )
    if dropped:
        detail += f" | DROPPED unverified IDs: {', '.join(dropped)}"
    if notes:
        detail += f" | {'; '.join(notes)}"
    log.info("understand: %s", detail)
    return {
        **reset,
        "decision": decision,
        "plan": plan[:MAX_INTENTS],
        "slots": slots,
        "awaiting": new_awaiting,
        "pending_work_order": new_draft,
        "tool_results": extra,
        "trace": [TraceStep(node="understand", detail=detail)],
    }


# ---------------------------------------------------------------------------
# check_requirements / ask_for_info
# ---------------------------------------------------------------------------


def check_requirements(state: AgentState) -> StateUpdate:
    intent = (state.get("plan") or [Intent.SMALL_TALK])[0]  # router only sends us here with a plan
    slots = _slots(state)
    missing: list[str] = []
    if intent is not Intent.KNOWLEDGE_SEARCH and not slots.requester_id:
        missing.append("requester_id")  # access rules depend on who is asking
        draft = state.get("pending_work_order")
        if intent is Intent.CREATE_WORK_ORDER and draft:
            missing += [f for f in draft.missing_fields() if f not in ("requester_id", "facility_id")]
    elif intent is Intent.ASSET_LOOKUP and not (
        slots.asset_id or slots.asset_hint or slots.ticket_id or _wants_my_tickets(state)
    ):
        missing.append("asset_id")
    return {
        "current_intent": intent,
        "missing_fields": missing,
        "trace": _trace(state, "check_requirements", f"{intent.value}: " + (f"missing {missing}" if missing else "ready")),
    }


def _wants_my_tickets(state: AgentState) -> bool:
    decision = state.get("decision")
    return bool(decision and decision.entities.my_tickets)


def ask_for_info(state: AgentState) -> StateUpdate:
    intent = state.get("current_intent") or Intent.SMALL_TALK
    result = needs_input(intent, state.get("missing_fields") or [])
    return _record(state, "ask_for_info", [result])


# ---------------------------------------------------------------------------
# Tool nodes
# ---------------------------------------------------------------------------


def knowledge_search(state: AgentState) -> StateUpdate:
    decision = state.get("decision")
    query = (decision.search_query if decision else None) or _last_user_text(state)
    if len(query.strip()) < 3:
        query = _last_user_text(state)
    result = _call_tool("search_knowledge_base", Intent.KNOWLEDGE_SEARCH, {"query": query})
    return _record(state, "knowledge_search", [result])


def asset_lookup(state: AgentState) -> StateUpdate:
    slots = _slots(state)
    decision = state.get("decision")
    entities = decision.entities if decision else ExtractedEntities()
    if entities.my_tickets and not entities.ticket_id:
        # "what's the status of my AC complaint?" - the requester's own tickets
        args = {
            "requester_id": slots.requester_id,
            "my_tickets": True,
            "keyword": entities.keyword,
            "issue_category": entities.issue_category,
        }
    else:
        args = {
            "requester_id": slots.requester_id,
            "asset_id": slots.asset_id,
            "asset_hint": slots.asset_hint,
            "ticket_id": slots.ticket_id,
            "facility_id": slots.facility_id,
        }
    result = _call_tool("lookup_asset", Intent.ASSET_LOOKUP, {k: v for k, v in args.items() if v})
    asset = result.data.get("asset") if result.status is ToolStatus.SUCCESS else None
    if asset:  # remember the resolved asset for follow-up questions
        slots.asset_id, slots.asset_hint = asset["asset_id"], None
    return _record(state, "asset_lookup", [result], slots=slots)


def compliance_status(state: AgentState) -> StateUpdate:
    slots = _slots(state)
    decision = state.get("decision")
    entities = decision.entities if decision else ExtractedEntities()
    args = {
        "requester_id": slots.requester_id,
        "facility_id": slots.facility_id,
        "status_filter": entities.compliance_filter or "all",
        "cert_type": entities.cert_type.value if entities.cert_type else None,
    }
    result = _call_tool("check_compliance", Intent.COMPLIANCE_STATUS, {k: v for k, v in args.items() if v})
    return _record(state, "compliance_status", [result])


_EMERGENCY_QUERIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"trapped|entrapment|stuck (inside|in)|lift|elevator"), "lift entrapment rescue immediate actions"),
    (re.compile(r"smoke|fire|burning"), "fire alarm activation response first 3 minutes"),
    (re.compile(r"power (failure|cut|outage)|blackout"), "mains power failure immediate actions"),
    (re.compile(r"flood|water ingress|burst|leak"), "active leak pipe burst isolate valve"),
]


def _emergency_guidance(draft: WorkOrderDraft) -> ToolResult | None:
    """For a P1 draft, fetch the matching SOP's immediate steps from the KB."""
    text = f"{draft.issue_description or ''} {draft.issue_category.value if draft.issue_category else ''}".lower()
    query = next((q for pattern, q in _EMERGENCY_QUERIES if pattern.search(text)), None)
    if query is None and draft.issue_category is TicketCategory.FIRE_SAFETY:
        query = _EMERGENCY_QUERIES[1][1]
    query = query or draft.issue_description or ""
    result = _call_tool("search_knowledge_base", Intent.KNOWLEDGE_SEARCH, {"query": query, "top_k": 2})
    if result.status is not ToolStatus.SUCCESS:
        return None
    result.data["emergency"] = True
    result.summary = f"Emergency guidance - {result.summary}"
    return result


def work_order_prepare(state: AgentState) -> StateUpdate:
    slots = _slots(state)
    draft = (state.get("pending_work_order") or WorkOrderDraft()).model_copy(
        update={"requester_id": slots.requester_id}
    )
    if not draft.facility_id and slots.facility_id:
        draft.facility_id = slots.facility_id
    result = _call_tool(
        "prepare_work_order", Intent.CREATE_WORK_ORDER, draft.model_dump(mode="json", exclude_none=True)
    )
    new_draft = WorkOrderDraft(**result.data["draft"]) if result.data.get("draft") else draft
    if result.status in (ToolStatus.DUPLICATE, ToolStatus.NOT_PERMITTED):
        new_draft = None  # nothing left to confirm

    results = [result]
    if result.status is ToolStatus.NEEDS_CONFIRMATION and new_draft and new_draft.priority is Priority.P1:
        guidance = _emergency_guidance(new_draft)
        if guidance:
            results.insert(0, guidance)  # safety steps first
    return _record(state, "work_order_prepare", results, pending_work_order=new_draft)


def work_order_commit(state: AgentState) -> StateUpdate:
    slots = _slots(state)
    pending = state.get("pending_work_order")
    if pending is None:  # router only commits when a draft exists; stay safe anyway
        result = ToolResult(
            tool=Intent.CREATE_WORK_ORDER,
            status=ToolStatus.ERROR,
            summary="There is no draft ticket to create.",
        )
        return _record(state, "work_order_commit", [result], current_intent=Intent.CREATE_WORK_ORDER, awaiting=None)
    draft = pending.model_copy(update={"requester_id": slots.requester_id})
    result = _call_tool(
        "commit_work_order", Intent.CREATE_WORK_ORDER, draft.model_dump(mode="json", exclude_none=True)
    )
    updates: dict = {"current_intent": Intent.CREATE_WORK_ORDER}
    if result.status in (ToolStatus.SUCCESS, ToolStatus.DUPLICATE, ToolStatus.NOT_PERMITTED):
        updates.update(pending_work_order=None, awaiting=None)
    elif result.status is ToolStatus.NEEDS_INPUT:
        updates["pending_work_order"] = WorkOrderDraft(**result.data["draft"]) if result.data.get("draft") else draft
    # ERROR: keep the draft and the pending confirmation so "yes" can be retried
    return _record(state, "work_order_commit", [result], **updates)


def advance_plan(state: AgentState) -> StateUpdate:
    plan = list(state.get("plan", []))
    current = state.get("current_intent")
    if plan and plan[0] == current:
        plan.pop(0)
    return {"plan": plan, "current_intent": None, "missing_fields": []}


# ---------------------------------------------------------------------------
# respond
# ---------------------------------------------------------------------------


def _results_payload(results: list[ToolResult]) -> str:
    return json.dumps(
        [
            {
                "tool": r.tool.value,
                "status": r.status.value,
                "summary": r.summary,
                "error": r.error,
                "data": r.data,
                "citations": [c.model_dump(mode="json") for c in r.citations],
            }
            for r in results
        ],
        default=str,
    )


def _fallback_answer(results: list[ToolResult]) -> str:
    """Plain rendering of tool results when the LLM cannot be used."""
    lines = ["_The language model is unavailable, so here is a plain summary of the records._", ""]
    for r in results:
        if r.status is ToolStatus.NEEDS_INPUT:
            continue
        lines.append(f"- {r.summary}")
        if r.tool is Intent.KNOWLEDGE_SEARCH and r.status is ToolStatus.SUCCESS:
            for hit in r.data.get("hits", []):
                chunk = hit["chunk"]
                lines.append(f"\n  **{chunk['doc_id']} - {chunk['section']}**\n\n  {chunk['text']}\n")
    return "\n".join(lines)


def _select_citations(results: list[ToolResult], cited_ids: list[str]) -> list[Citation]:
    """Keep only citations that came from tools; prefer the ones the LLM used."""
    pool = [c for r in results if r.status is not ToolStatus.NEEDS_INPUT for c in r.citations]
    chosen = [c for c in pool if c.source_id in set(cited_ids)] or pool
    seen, unique = set(), []
    for c in chosen:
        key = (c.source_id, c.locator)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _template_reply(state: AgentState) -> str:
    decision = state.get("decision")
    intents = decision.intents if decision else []
    text = _last_user_text(state)
    if Intent.OUT_OF_SCOPE in intents:
        return f"{prompts.OUT_OF_SCOPE_TEXT}\n\n{_capabilities(state)}"
    if _THANKS.match(text):
        return "You're welcome! Let me know if there's anything else."
    if _YES_NO.match(text):
        return "There's nothing waiting for a yes/no right now. What would you like to do next?"
    if decision and decision.entities.requester_id:
        person = repo.get_person(decision.entities.requester_id)
        if person:
            return (
                f"Thanks, {person.name}. I'll use **{person.person_id}** for this conversation.\n\n"
                + _capabilities(state)
            )
        return f"I couldn't find anyone with ID {decision.entities.requester_id}. Please check the ID."
    return _capabilities(state)


def _capabilities(state: AgentState) -> str:
    session = state.get("session")
    if session is None:
        return prompts.CAPABILITIES_TEXT
    return prompts.CAPABILITIES_CORE + f"\nYou're working on **{session.facility_id}**."


def _unescape(text: str) -> str:
    """Models occasionally return markdown with literal backslash-n sequences
    ("draft:\\n\\n- Ticket Type") that would show up as text; turn them into real line breaks."""
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def respond(state: AgentState) -> StateUpdate:
    results = state.get("tool_results", [])
    error = state.get("error")
    follow_ups = list(dict.fromkeys(r.follow_up_question for r in results if r.follow_up_question))
    pending = state.get("awaiting")
    if pending and pending.kind == "work_order_confirmation" and pending.question not in follow_ups:
        follow_ups.append(pending.question)  # a draft is still waiting for yes/no - keep asking

    if error and not results:
        final = FinalAnswer(answer=f"⚠️ {error}", generated_by="template")
    elif not results:
        final = FinalAnswer(answer=_template_reply(state), follow_up_questions=follow_ups, generated_by="template")
    elif all(r.status is ToolStatus.NEEDS_INPUT for r in results):
        final = FinalAnswer(answer="", follow_up_questions=follow_ups, generated_by="template")
    else:
        human = HumanMessage(
            content=(
                f"USER MESSAGE:\n{_last_user_text(state)}\n\n"
                f"TODAY'S DATE (system): {get_settings().today().isoformat()}\n\n"
                f"TOOL_RESULTS:\n{_results_payload(results)}"
            )
        )
        try:
            out = llm.invoke_structured(LLMAnswer, [SystemMessage(content=prompts.RESPOND_SYSTEM), human])
            final = FinalAnswer(
                answer=_unescape(out.answer).strip(),
                recommendations=[_unescape(r).strip() for r in out.recommendations if r.strip()][:3],
                citations=_select_citations(results, out.cited_source_ids),
                follow_up_questions=follow_ups,
                generated_by="llm",
            )
        except (llm.LLMConfigError, llm.LLMCallError) as exc:
            log.warning("respond fell back to template: %s", exc)
            final = FinalAnswer(
                answer=_fallback_answer(results),
                citations=_select_citations(results, []),
                follow_up_questions=follow_ups,
                generated_by="fallback",
            )

    message_text = "\n\n".join(p for p in [final.answer, *final.follow_up_questions] if p)
    return {
        "final_answer": final,
        "messages": [AIMessage(content=message_text)],
        "trace": _trace(
            state,
            "respond",
            f"{final.generated_by}; {len(final.citations)} citation(s); {len(final.follow_up_questions)} follow-up(s)",
        ),
    }
