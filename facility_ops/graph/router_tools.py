"""
Native tool calling for the `understand` node.

The chat model is bound to the tool schemas below (`model.bind_tools(ROUTER_TOOLS,
tool_choice="any")`) and answers every user message with one or more TOOL CALLS:
which tools to run, in what order, and with which arguments. Several calls in
one reply (parallel function calling) handle messages that ask for several
things at once.

    model tool call                    executed by the graph as
    ---------------------------------  ----------------------------------------------
    search_knowledge_base(query)       knowledge_search node -> registry tool of the same name
    lookup_asset(...)                  asset_lookup node     -> registry `lookup_asset`
    check_compliance(...)              compliance_status node-> registry `check_compliance`
    create_ticket(...)                 work_order_prepare    -> registry `prepare_work_order`
                                       (then `commit_work_order` only after the user says yes)
    answer_pending_question(yes|no)    confirms / cancels a draft or a pending question
    identify_user(person_id)           CLI without sign-in; ignored when signed in
    small_talk() / out_of_scope()      templated replies, no data tool

Why the graph, not the model, executes the tools: the requester's identity is
never taken from the model (it comes from the sign-in session), arguments are
merged with what earlier turns already collected (slot filling), required
fields and access rules are checked first, and writes need a confirmation.
The model decides WHAT to call; the graph decides WHETHER it is safe to run.

`tool_calls_to_decision` turns the calls into the internal `IntentDecision`
the rest of the graph works with; `decision_to_tool_calls` is its inverse,
used by the tests and the offline browser suite to script model replies.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from facility_ops import llm
from facility_ops.logging_config import get_logger
from facility_ops.schemas import CertificateType, ExtractedEntities, Intent, IntentDecision, TicketCategory

log = get_logger("graph.router")

MAX_CALLS = 3

# ---------------------------------------------------------------------------
# Tool schemas shown to the model (name = title, description = docstring)
# ---------------------------------------------------------------------------


class _Tool(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SearchKnowledgeBase(_Tool):
    """Search the facility knowledge base: maintenance SOPs, safety and emergency procedures, troubleshooting,
    and statutory / regulatory rules in general (what a rule requires, how often, which documents).
    e.g. "how do I reset a chiller trip alarm", "documents for fire NOC renewal", "lift inspection rules"."""

    model_config = ConfigDict(title="search_knowledge_base")
    query: str = Field(
        description="A standalone search query. Resolve follow-ups using earlier turns "
        "('and if there's an accident?' after lift rules -> 'lift accident reporting')."
    )


class LookupAsset(_Tool):
    """Look up facts about a SPECIFIC piece of equipment (AMC, warranty, service history, last service, open
    tickets on it), the status of a ticket by its ID, or the user's OWN tickets when they give no ticket ID
    ("what's the status of my AC complaint?"). Also use it for follow-ups about the equipment already being
    discussed ("and when was it last serviced?") - with no arguments."""

    model_config = ConfigDict(title="lookup_asset")
    asset_id: str | None = Field(default=None, description="Asset ID like AST-CHL-001, only if the user typed it")
    equipment_name: str | None = Field(
        default=None, description="The user's words for the equipment when no asset ID is given ('chiller 2', 'AHU L12')"
    )
    ticket_id: str | None = Field(default=None, description="WO-2026-0001 / CMP-2026-0001, only if the user typed it")
    my_tickets: bool = Field(
        default=False, description="True when the user asks about tickets THEY raised without giving a ticket ID"
    )
    keyword: str | None = Field(
        default=None, description="With my_tickets: words saying which ticket ('access card', 'AC not cooling')"
    )
    issue_category: TicketCategory | None = Field(default=None, description="With my_tickets: the kind of issue")
    facility_id: str | None = Field(default=None, description="FAC-001 etc., only if the user typed it")


class CheckCompliance(_Tool):
    """List THIS facility's statutory certificates, licences and inspections and whether they are overdue, due,
    upcoming or valid. e.g. "what compliance is overdue", "when does OUR fire NOC expire", "what's due soon".
    For what a rule requires in general, use search_knowledge_base; if unclear, call both."""

    model_config = ConfigDict(title="check_compliance")
    status_filter: Literal["all", "overdue", "due_soon", "due", "upcoming"] | None = Field(
        default=None,
        description="overdue/expired/lapsed -> overdue; 'due soon', 'coming up', 'what needs renewing' -> due_soon; "
        "due or upcoming only if the user literally asks for those; otherwise leave empty",
    )
    cert_type: CertificateType | None = Field(default=None, description="When the user asks about one certificate type")
    facility_id: str | None = Field(default=None, description="FAC-001 etc., only if the user typed it")


class CreateTicket(_Tool):
    """Report a fault or problem, or raise / log a ticket, complaint or work order. Pass whatever details the
    user gave; the system asks for anything missing and shows a draft for confirmation before creating it.
    Also call it again when the user supplies details the assistant asked for (e.g. just a location)."""

    model_config = ConfigDict(title="create_ticket")
    issue_description: str | None = Field(default=None, description="What is wrong, in the user's own words")
    location: str | None = Field(default=None, description="Floor and area as the user said it ('L5, east wing')")
    issue_category: TicketCategory | None = Field(default=None, description="The kind of issue")
    equipment_name: str | None = Field(default=None, description="The user's words for the equipment, if any")
    asset_id: str | None = Field(default=None, description="Asset ID, only if the user typed it")
    facility_id: str | None = Field(default=None, description="FAC-001 etc., only if the user typed it")


class AnswerPendingQuestion(_Tool):
    """The user answers the assistant's pending yes/no question about a draft ticket ('yes', 'ok, create it',
    'no', 'cancel'), or declines to continue while details are being collected ('no', 'never mind')."""

    model_config = ConfigDict(title="answer_pending_question")
    answer: Literal["yes", "no"]


class IdentifyUser(_Tool):
    """The user states their employee or occupant ID (EMP-1234 / OCC-1234), e.g. "I'm EMP-1024"."""

    model_config = ConfigDict(title="identify_user")
    person_id: str = Field(description="Exactly as typed")


class SmallTalk(_Tool):
    """Greetings, thanks, 'what can you do', or anything about facility operations that needs no data."""

    model_config = ConfigDict(title="small_talk")


class OutOfScope(_Tool):
    """Anything unrelated to facility operations (sports scores, general trivia, coding help...)."""

    model_config = ConfigDict(title="out_of_scope")


ROUTER_TOOLS: list[type[_Tool]] = [
    SearchKnowledgeBase,
    LookupAsset,
    CheckCompliance,
    CreateTicket,
    AnswerPendingQuestion,
    IdentifyUser,
    SmallTalk,
    OutOfScope,
]


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace {"$ref": "#/$defs/X"} with the definition (Gemini ignores $defs)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            merged = {**defs[ref.split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline_refs(merged, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _spec(tool: type[_Tool]) -> dict[str, Any]:
    spec = convert_to_openai_tool(tool)
    params = spec["function"]["parameters"]
    defs = params.pop("$defs", {})
    spec["function"]["parameters"] = _inline_refs(params, defs)
    return spec


_BY_NAME: dict[str, type[_Tool]] = {t.model_config.get("title") or t.__name__: t for t in ROUTER_TOOLS}
TOOL_NAMES = tuple(_BY_NAME)
# What is actually bound to the model: OpenAI-style function specs, enums inlined.
ROUTER_TOOL_SPECS: list[dict[str, Any]] = [_spec(t) for t in ROUTER_TOOLS]

_INTENT_OF = {
    "search_knowledge_base": Intent.KNOWLEDGE_SEARCH,
    "lookup_asset": Intent.ASSET_LOOKUP,
    "check_compliance": Intent.COMPLIANCE_STATUS,
    "create_ticket": Intent.CREATE_WORK_ORDER,
    "out_of_scope": Intent.OUT_OF_SCOPE,
}

# ---------------------------------------------------------------------------
# Tool calls <-> IntentDecision
# ---------------------------------------------------------------------------


def _parse(name: str, args: dict[str, Any]) -> _Tool | None:
    """Validate one call's arguments; drop invalid fields rather than the whole call."""
    schema = _BY_NAME.get(name)
    if schema is None:
        log.warning("Model called an unknown tool %r - ignored", name)
        return None
    args = dict(args or {})
    for _ in range(len(args) + 1):
        try:
            return schema.model_validate(args)
        except ValidationError as exc:
            bad = {str(e["loc"][0]) for e in exc.errors() if e.get("loc")}
            if not bad & set(args):
                log.warning("Unusable call %s(%s): %s", name, args, exc)
                return None
            log.warning("Dropping invalid argument(s) %s of %s", sorted(bad), name)
            args = {k: v for k, v in args.items() if k not in bad}
    return None


def _fmt(name: str, args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, False, ""))
    return f"{name}({shown})"


def tool_calls_to_decision(calls: list[dict[str, Any]]) -> IntentDecision:
    """Merge the model's tool calls into one routing decision (intents in call order)."""
    intents: list[Intent] = []
    fields: dict[str, Any] = {}
    search_query: str | None = None
    confirmation: Literal["yes", "no", "none"] = "none"

    def put(key: str, value: Any) -> None:
        if value not in (None, "", False) and key not in fields:
            fields[key] = value

    for call in calls[: MAX_CALLS + 2]:
        tool = _parse(call.get("name", ""), call.get("args") or {})
        if tool is None:
            continue
        intent = _INTENT_OF.get(tool.model_config.get("title") or "")
        if intent is not None and intent not in intents:
            intents.append(intent)
        if isinstance(tool, SearchKnowledgeBase):
            search_query = search_query or tool.query
        elif isinstance(tool, LookupAsset):
            put("asset_id", tool.asset_id)
            put("asset_hint", tool.equipment_name)
            put("ticket_id", tool.ticket_id)
            put("my_tickets", tool.my_tickets)
            put("keyword", tool.keyword)
            put("issue_category", tool.issue_category)
            put("facility_id", tool.facility_id)
        elif isinstance(tool, CheckCompliance):
            put("compliance_filter", tool.status_filter)
            put("cert_type", tool.cert_type)
            put("facility_id", tool.facility_id)
        elif isinstance(tool, CreateTicket):
            put("issue_description", tool.issue_description)
            put("location", tool.location)
            put("issue_category", tool.issue_category)
            put("asset_hint", tool.equipment_name)
            put("asset_id", tool.asset_id)
            put("facility_id", tool.facility_id)
        elif isinstance(tool, AnswerPendingQuestion):
            confirmation = tool.answer
        elif isinstance(tool, IdentifyUser):
            put("requester_id", tool.person_id)

    try:
        entities = ExtractedEntities.model_validate(fields)
    except ValidationError:  # e.g. an unparseable ID - keep what is valid
        entities = ExtractedEntities.model_validate(
            {k: v for k, v in fields.items() if k not in ("requester_id", "facility_id", "asset_id", "ticket_id")}
        )
    rationale = "tool calls: " + ("; ".join(_fmt(c.get("name", "?"), c.get("args") or {}) for c in calls) or "none")
    return IntentDecision(
        intents=intents[:MAX_CALLS] or [Intent.SMALL_TALK],
        entities=entities,
        search_query=search_query,
        confirmation=confirmation,
        rationale=rationale,
    )


def decision_to_tool_calls(decision: IntentDecision) -> list[dict[str, Any]]:
    """Inverse of `tool_calls_to_decision` - lets tests script what the model would call."""
    e = decision.entities
    calls: list[dict[str, Any]] = []
    for intent in decision.intents:
        if intent is Intent.KNOWLEDGE_SEARCH:
            calls.append({"name": "search_knowledge_base", "args": {"query": decision.search_query or ""}})
        elif intent is Intent.ASSET_LOOKUP:
            calls.append({"name": "lookup_asset", "args": {
                "asset_id": e.asset_id, "equipment_name": e.asset_hint, "ticket_id": e.ticket_id,
                "my_tickets": e.my_tickets, "keyword": e.keyword,
                "issue_category": e.issue_category.value if e.issue_category else None,
                "facility_id": e.facility_id,
            }})
        elif intent is Intent.COMPLIANCE_STATUS:
            calls.append({"name": "check_compliance", "args": {
                "status_filter": e.compliance_filter,
                "cert_type": e.cert_type.value if e.cert_type else None,
                "facility_id": e.facility_id,
            }})
        elif intent is Intent.CREATE_WORK_ORDER:
            calls.append({"name": "create_ticket", "args": {
                "issue_description": e.issue_description, "location": e.location,
                "issue_category": e.issue_category.value if e.issue_category else None,
                "equipment_name": e.asset_hint, "asset_id": e.asset_id, "facility_id": e.facility_id,
            }})
        elif intent is Intent.OUT_OF_SCOPE:
            calls.append({"name": "out_of_scope", "args": {}})
    if decision.confirmation != "none":
        calls.append({"name": "answer_pending_question", "args": {"answer": decision.confirmation}})
    if e.requester_id:
        calls.append({"name": "identify_user", "args": {"person_id": e.requester_id}})
    if not calls:
        calls.append({"name": "small_talk", "args": {}})
    for c in calls:
        c["args"] = {k: v for k, v in c["args"].items() if v not in (None, False)}
    return calls


def decide(messages: list[BaseMessage]) -> IntentDecision:
    """One model call with the router tools bound; returns the merged decision."""
    calls = llm.invoke_tool_calls(ROUTER_TOOL_SPECS, messages)
    return tool_calls_to_decision(calls)
