"""
Check the LLM setup with one tiny request.

    python scripts/check_llm.py

Prints the provider, model and which API key is used (only its last 4
characters, and whether it comes from .env or from a system environment
variable), then sends a one-word prompt and reports the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facility_ops import llm  # noqa: E402
from facility_ops.config import api_key_origin  # noqa: E402


def main() -> int:
    print(f"Model : {llm.model_label()}")
    print(f"Key   : {api_key_origin()}")
    problem = llm.config_problem()
    if problem:
        print(f"Setup problem: {problem}")
        return 1
    try:
        reply = llm.get_chat_model().invoke("Reply with the single word OK.")
    except Exception as exc:  # noqa: BLE001
        raw = str(exc).splitlines()[0][:300]
        print(f"FAILED: {llm._friendly(exc)}\n        provider said: {raw}")
        return 1
    print(f"OK - the model replied: {str(reply.content).strip()[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
