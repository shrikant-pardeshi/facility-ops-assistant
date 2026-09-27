"""
Offline wrapper around app.py for `run_e2e.py --fake-llm`.

Replaces the LLM with a scripted stand-in:
  - the routing model's tool calls come from the `fake=` field of each scenario
    step (looked up by the exact message text), converted with
    `decision_to_tool_calls` - the same shape Gemini's native tool calls have;
  - answers are the tool summaries plus the raw tool data, so every fact the
    checks look for is present without calling any API.

Not used by the real app. Streamlit re-executes this file on every
interaction, so the patch is re-applied each run.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

from e2e.scenarios import fake_decisions  # noqa: E402
from facility_ops import llm  # noqa: E402
from facility_ops.graph.router_tools import decision_to_tool_calls  # noqa: E402
from facility_ops.schemas import LLMAnswer  # noqa: E402

DECISIONS = fake_decisions()


def scripted_tool_calls(tools, messages):
    text = next(str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)).strip()
    if text not in DECISIONS:
        raise llm.LLMCallError(f"[offline e2e] no scripted decision for message: {text!r}")
    return decision_to_tool_calls(DECISIONS[text])


def scripted_llm(schema, messages):
    payload = json.loads(messages[-1].content.split("TOOL_RESULTS:\n", 1)[1])
    lines = []
    for r in payload:
        lines.append(f"- **{r['tool']} / {r['status']}**: {r['summary']}")
        lines.append(f"  `{json.dumps(r['data'], default=str)[:4000]}`")
    return LLMAnswer(
        answer="\n".join(lines),
        cited_source_ids=[c["source_id"] for r in payload for c in r["citations"]],
    )


llm.invoke_structured = scripted_llm
llm.invoke_tool_calls = scripted_tool_calls
llm.config_problem = lambda: None
llm.model_label = lambda: "offline-e2e:scripted"

runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
