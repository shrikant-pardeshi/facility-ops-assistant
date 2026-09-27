"""
Minimal terminal chat for trying the agent before the Streamlit UI.

    python scripts/chat_cli.py                         # identify yourself in the chat
    python scripts/chat_cli.py --user EMP-1024         # signed in, registered facility
    python scripts/chat_cli.py --user EMP-1024 --facility FAC-002

With --user the CLI behaves like the Streamlit app after sign-in: the identity
is fixed for the session and staff can pick any facility.
Type 'reset' to start a new conversation, 'quit' to exit.
Needs GOOGLE_API_KEY (or OPENAI_API_KEY) in .env.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facility_ops import access, llm  # noqa: E402
from facility_ops.data_access import repositories as repo  # noqa: E402
from facility_ops.data_access.db import DataUnavailableError, ensure_database  # noqa: E402
from facility_ops.graph.builder import build_graph, run_turn  # noqa: E402
from facility_ops.logging_config import setup_logging  # noqa: E402
from facility_ops.state import SessionContext  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="sign in as this employee/occupant ID")
    parser.add_argument("--facility", help="working facility (staff: any; occupants: their own)")
    args = parser.parse_args()
    setup_logging()
    problem = llm.config_problem()
    if problem:
        print(f"LLM not configured: {problem}")
        return 1
    try:
        ensure_database()
    except DataUnavailableError as exc:
        print(f"Data problem: {exc}")
        return 1

    session = None
    if args.user:
        signed = access.sign_in(args.user, args.facility, repo.get_person, repo.get_facility)
        if not signed.ok or signed.person is None or signed.facility_id is None:
            print(f"Sign-in failed: {signed.error}")
            return 1
        session = SessionContext(requester_id=signed.person.person_id, facility_id=signed.facility_id)
        print(f"Signed in as {signed.person.name} ({signed.person.person_id}), working on {signed.facility_id}.")
    elif args.facility:
        print("--facility needs --user")
        return 1

    graph, thread = build_graph(), str(uuid.uuid4())
    print(f"Facility Operations Assistant ({llm.model_label()}). 'reset' = new conversation, 'quit' = exit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            return 0
        if text.lower() == "reset":
            thread = str(uuid.uuid4())
            print("(new conversation)\n")
            continue
        state = run_turn(graph, thread, text, session=session)
        final = state.get("final_answer")
        if final is None:
            print("\nassistant> (no answer was produced - please try again)\n")
            continue
        questions = list(final.follow_up_questions)
        # When the turn is only a question, show it as the reply itself.
        answer = final.answer or (questions.pop(0) if questions else "")
        print(f"\nassistant> {answer}")
        for q in questions:
            print(f"\n  ? {q}")
        if final.recommendations:
            print("\n  Suggestions (AI-generated - verify before acting):")
            for r in final.recommendations:
                print(f"   - {r}")
        if final.citations:
            print("\n  Sources: " + ", ".join(dict.fromkeys(c.source_id for c in final.citations)))
        print("  [path: " + " > ".join(t.node for t in state.get("trace", [])) + "]\n")


if __name__ == "__main__":
    raise SystemExit(main())
