"""
Tool 1 - Knowledge Search over SOPs + statutory compliance clauses.

One unified index: the tool does not care whether the answer lives in an SOP
or a compliance document; `doc_type` only labels the citation.
"""

from __future__ import annotations

from facility_ops.config import get_settings
from facility_ops.data_access.knowledge_base import get_index
from facility_ops.schemas import (
    Citation,
    Intent,
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    SourceType,
    ToolResult,
    ToolStatus,
)
from facility_ops.tools.common import safe_tool

INTENT = Intent.KNOWLEDGE_SEARCH


@safe_tool(INTENT)
def search_knowledge_base(params: KnowledgeSearchInput) -> ToolResult:
    """Search maintenance SOPs and statutory compliance clauses."""
    top_k = params.top_k or get_settings().kb_top_k
    hits = get_index().search(params.query, top_k=top_k, doc_type=params.doc_type)
    output = KnowledgeSearchOutput(query=params.query, hits=hits)

    if not hits:
        return ToolResult(
            tool=INTENT,
            status=ToolStatus.NOT_FOUND,
            summary=f"No knowledge-base document matches '{params.query}'.",
            data=output.model_dump(mode="json"),
        )

    citations: list[Citation] = []
    for h in hits:
        c = h.chunk
        citations.append(
            Citation(
                source_type=SourceType.SOP if c.doc_type == "sop" else SourceType.COMPLIANCE_CLAUSE,
                source_id=c.doc_id,
                title=c.title,
                locator=c.section,
            )
        )
    docs = sorted({f"{h.chunk.doc_id} ({h.chunk.title})" for h in hits})
    return ToolResult(
        tool=INTENT,
        status=ToolStatus.SUCCESS,
        summary=f"{len(hits)} section(s) found in {', '.join(docs)}.",
        data=output.model_dump(mode="json"),
        citations=citations,
    )
