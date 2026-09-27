"""
The executable tools, as LangChain StructuredTools (name, description,
Pydantic args_schema). The graph's tool nodes run them via
`TOOLS[name].invoke(args)`, so every call is validated against its schema.

The MODEL does not see these schemas directly: it is bound to the router
tool schemas in `graph/router_tools.py` (native function calling), which map
one-to-one onto these tools but leave out what must not come from the model -
the requester's identity (taken from the sign-in session) and internal fields.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from facility_ops.schemas import (
    AssetLookupInput,
    ComplianceStatusInput,
    KnowledgeSearchInput,
    ToolResult,
    WorkOrderDraft,
)
from facility_ops.tools.asset_lookup import lookup_asset
from facility_ops.tools.compliance_status import check_compliance
from facility_ops.tools.knowledge_search import search_knowledge_base
from facility_ops.tools.work_order import commit_work_order, prepare_work_order


def _wrap(fn, schema) -> Any:
    def run(**kwargs) -> dict:
        result: ToolResult = fn(schema(**kwargs))
        return result.model_dump(mode="json")

    return run


TOOLS: dict[str, StructuredTool] = {
    t.name: t
    for t in [
        StructuredTool.from_function(
            func=_wrap(search_knowledge_base, KnowledgeSearchInput),
            name="search_knowledge_base",
            description=(
                "Search the facility knowledge base: maintenance SOPs (e.g. chiller trip reset, lift "
                "entrapment) and statutory compliance clauses (e.g. fire NOC renewal, lift licence rules)."
            ),
            args_schema=KnowledgeSearchInput,
        ),
        StructuredTool.from_function(
            func=_wrap(lookup_asset, AssetLookupInput),
            name="lookup_asset",
            description=(
                "Look up an asset's warranty, AMC status, maintenance history, last service date and open "
                "tickets (by asset_id or equipment name), or the status of a ticket by ticket_id."
            ),
            args_schema=AssetLookupInput,
        ),
        StructuredTool.from_function(
            func=_wrap(check_compliance, ComplianceStatusInput),
            name="check_compliance",
            description=(
                "List a facility's statutory certificates (fire NOC, Form B, lift licences, electrical "
                "inspection, pollution consent...) classified as overdue, due, upcoming or valid."
            ),
            args_schema=ComplianceStatusInput,
        ),
        StructuredTool.from_function(
            func=_wrap(prepare_work_order, WorkOrderDraft),
            name="prepare_work_order",
            description=(
                "Validate a new maintenance work order or occupant complaint, derive its priority and check "
                "for duplicates. Does NOT create anything - returns a draft for the user to confirm."
            ),
            args_schema=WorkOrderDraft,
        ),
        StructuredTool.from_function(
            func=_wrap(commit_work_order, WorkOrderDraft),
            name="commit_work_order",
            description="Create a previously prepared work order / complaint after the user confirmed it.",
            args_schema=WorkOrderDraft,
        ),
    ]
}
