"""Headless UI tests with Streamlit's AppTest and the scripted LLM stand-in."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from facility_ops import llm
from facility_ops.schemas import Intent
from tests.test_graph import D, ScriptedLLM

APP = str(Path(__file__).resolve().parent.parent / "app.py")
pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture()
def fake(monkeypatch):
    f = ScriptedLLM()
    monkeypatch.setattr(llm, "invoke_structured", f)
    monkeypatch.setattr(llm, "invoke_tool_calls", f.tool_calls)
    monkeypatch.setattr(llm, "config_problem", lambda: None)
    return f


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [i.value for i in at.info] + [e.value for e in at.error]
    return "\n".join(str(p) for p in parts)


def sign_in(at: AppTest, person_id: str, facility: str | None = None) -> AppTest:
    at.text_input[0].set_value(person_id)
    if facility:
        at.selectbox[0].set_value(facility)
    return at.button(key="FormSubmitter:sign_in-Sign in").click().run()


def test_sign_in_screen_blocks_chat_until_signed_in(fake):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "Facility Operations Assistant" in at.header[0].value
    assert len(at.chat_input) == 0  # no chat before sign-in
    assert any(b.label == "Sign in" for b in at.button)


def test_unknown_id_is_rejected(fake):
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "EMP-9999")
    assert any("No one with ID EMP-9999" in e.value for e in at.error)
    assert len(at.chat_input) == 0


def test_occupant_cannot_pick_another_building(fake):
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "OCC-2001", "FAC-002")
    assert any("can only use that building" in e.value for e in at.error)
    assert "user_id" not in at.session_state


def test_occupant_complaint_without_typing_an_id(fake):
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "occ-2001")
    assert not at.exception
    assert at.session_state.user_id == "OCC-2001" and at.session_state.facility_id == "FAC-001"
    assert "Hello Aditi" in _text(at)
    assert next(s for s in at.selectbox if s.label == "Working facility").disabled

    fake.decisions.append(
        D(
            Intent.CREATE_WORK_ORDER,
            location="L5, east wing conference room",
            issue_description="AC not cooling",
            issue_category="hvac",
        )
    )
    at.chat_input[0].set_value("AC not cooling in the L5 east wing conference room").run()
    assert not at.exception
    assert "Reply **yes**" in _text(at)  # straight to confirmation: identity came from sign-in
    assert any("awaiting" in w.value for w in at.warning)

    fake.decisions.append(D(Intent.SMALL_TALK, confirmation="yes"))
    at.chat_input[0].set_value("yes").run()
    assert "CMP-2026-0018" in _text(at)
    assert len(at.session_state.history) == 4

    clear = next(b for b in at.button if "Clear conversation" in b.label)
    old_thread = at.session_state.thread_id
    clear.click().run()
    assert at.session_state.history == [] and at.session_state.thread_id != old_thread
    assert at.session_state.user_id == "OCC-2001"  # clearing keeps you signed in


def test_staff_switches_facility(fake):
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "EMP-1024")
    working = next(s for s in at.selectbox if s.label == "Working facility")
    assert not working.disabled and working.value == "FAC-001"

    fake.decisions.append(D(Intent.COMPLIANCE_STATUS, compliance_filter="overdue"))
    at.chat_input[0].set_value("what's overdue?").run()
    assert "Deccan" not in at.session_state.history[-1]["results"][0]["summary"]

    next(s for s in at.selectbox if s.label == "Working facility").set_value("FAC-002").run()
    assert not at.exception
    assert at.session_state.facility_id == "FAC-002"
    assert "Switched working facility" in _text(at)

    fake.decisions.append(D(Intent.COMPLIANCE_STATUS, compliance_filter="overdue"))
    at.chat_input[0].set_value("and here?").run()
    assert "Deccan Corporate Centre" in at.session_state.history[-1]["results"][0]["summary"]


def test_switch_user_returns_to_sign_in(fake):
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "EMP-1024")
    next(b for b in at.button if "Switch user" in b.label).click().run()
    assert "user_id" not in at.session_state
    assert len(at.chat_input) == 0


def test_missing_api_key_shows_banner_and_disables_chat(monkeypatch):
    monkeypatch.setattr(llm, "config_problem", lambda: "No Gemini API key found.")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Language model not configured" in e.value for e in at.error)  # shown before sign-in too
    at = sign_in(at, "EMP-1024")
    assert any("Language model not configured" in e.value for e in at.error)
    assert at.chat_input[0].disabled


def test_unexpected_crash_is_contained(fake):
    # A malformed model response makes the understand node crash with an
    # unexpected exception - the page must show a friendly error, not a stack trace.
    fake.decisions.append("not an IntentDecision")
    at = sign_in(AppTest.from_file(APP, default_timeout=30).run(), "EMP-1024")
    at.chat_input[0].set_value("hello").run()
    assert not at.exception
    assert "Something went wrong" in _text(at)
