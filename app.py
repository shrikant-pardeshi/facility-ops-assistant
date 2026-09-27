"""
Facility Operations Assistant - Streamlit UI.

    streamlit run app.py

- The user signs in first (employee / occupant ID + facility). The signed-in
  identity and working facility are passed to the graph with every turn as
  `session`, so nobody has to type their ID in the chat.
- FM staff can switch the working facility at any time from the sidebar;
  occupants are locked to the building they are registered at.
- Each conversation gets its own LangGraph thread_id (conversation memory).
  "Clear conversation" and "Switch user" start a new thread_id.
- The graph (and its in-memory checkpointer) is cached for the process.
"""

from __future__ import annotations

import uuid

import streamlit as st
from langchain_core.messages import AIMessage

from facility_ops import access, llm
from facility_ops.config import api_key_origin, get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.data_access.db import DataUnavailableError, ensure_database
from facility_ops.graph.builder import build_graph, run_turn, thread_config
from facility_ops.graph.routing import RESPOND
from facility_ops.logging_config import get_logger, setup_logging
from facility_ops.schemas import Facility, Person
from facility_ops.state import ConversationSlots, SessionContext
from facility_ops.ui.components import (
    assistant_entry,
    error_entry,
    note_entry,
    render_assistant,
    render_session_context,
)

st.set_page_config(page_title="Facility Operations Assistant", page_icon="🏢", layout="wide")
setup_logging()
log = get_logger("app")

# Sample prompts that name equipment use one that exists at the working facility.
SITE_EXAMPLES = {
    "FAC-001": ("chiller 2", "Chiller 2 is tripping repeatedly on high pressure, please raise a work order"),
    "FAC-002": ("lift B", "Lift B door is not closing properly, please raise a work order"),
}


@st.cache_resource(show_spinner=False)
def get_graph():
    return build_graph()


def new_conversation() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.history = []
    log.info("New conversation %s", st.session_state.thread_id)


def sign_out() -> None:
    log.info("Sign out %s", st.session_state.get("user_id"))
    for key in ("user_id", "facility_id", "working_facility"):
        st.session_state.pop(key, None)
    new_conversation()


def facility_label(f: Facility) -> str:
    return f"{f.facility_id} · {f.name}"


def sample_prompts(person: Person, facility_id: str) -> list[str]:
    if person.role == "facility_staff":
        asset, work_order = SITE_EXAMPLES.get(facility_id, SITE_EXAMPLES["FAC-001"])
        return [
            "How do I reset a chiller trip alarm?",
            f"Is the AMC on {asset} active, and when was it last serviced?",
            "Which compliance items are overdue?",
            work_order,
            "What documents are needed for fire NOC renewal?",
        ]
    floor = person.floor or "L1"
    return [
        f"AC not cooling in our {floor} conference room, please log a complaint",
        f"Water is dripping from the ceiling in the {floor} pantry",
        "What's the status of my complaints?",
        "What should I do if someone is stuck in a lift?",
        "Is chiller 2 under AMC?",  # shows the occupant access rule
    ]


if "thread_id" not in st.session_state:
    new_conversation()

settings = get_settings()

# --- Start-up checks ----------------------------------------------------------
try:
    ensure_database()
except DataUnavailableError as exc:
    st.error(f"**Facility data could not be loaded.** {exc}", icon="🗄️")
    st.caption("Check the files in data/seed/, then run `python scripts/init_db.py --reset`.")
    st.stop()

llm_problem = llm.config_problem()
if "key_logged" not in st.session_state:
    log.info("LLM %s, key: %s", llm.model_label(), api_key_origin())
    st.session_state.key_logged = True
graph = get_graph()
facilities = {f.facility_id: f for f in repo.list_facilities()}

# =============================================================================
# Sign-in screen
# =============================================================================
if not st.session_state.get("user_id"):
    with st.sidebar:
        st.title("🏢 Facility Ops Assistant")
        st.caption(
            f"Model: `{llm.model_label()}`  \nKey: {api_key_origin()}  \n"
            f"Data as of: **{settings.today():%d-%b-%Y}**"
        )
        st.caption(
            "All buildings, people and vendors are fictional. Compliance text is an illustrative "
            "paraphrase, not legal advice."
        )

    st.header("Facility Operations Assistant")
    st.caption("Sign in to start. Your identity decides what you can see and which tickets you can raise.")
    if llm_problem:
        st.error(f"**Language model not configured.** {llm_problem}", icon="🔑")

    left, right = st.columns([2, 3], gap="large")
    with left:
        with st.form("sign_in"):
            st.subheader("🔐 Sign in")
            typed_id = st.text_input("Employee or occupant ID", placeholder="e.g. EMP-1024 or OCC-2001")
            chosen = st.selectbox(
                "Facility",
                options=[None, *facilities],
                format_func=lambda fid: "My registered facility" if fid is None else facility_label(facilities[fid]),
                help="FM staff can work at any site. Occupants can only use the building they are registered at.",
            )
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            result = access.sign_in(typed_id, chosen, repo.get_person, repo.get_facility)
            if result.ok and result.person and result.facility_id:
                st.session_state.user_id = result.person.person_id
                st.session_state.facility_id = result.facility_id
                new_conversation()
                log.info("Signed in %s at %s", result.person.person_id, result.facility_id)
                st.rerun()
            st.error(result.error, icon="🚫")

    with right:
        st.subheader("👥 Demo users")
        st.caption(
            "No passwords in this demo; pick any ID. FM staff (EMP) see assets, AMC and compliance "
            "at every site and raise work orders. Occupants (OCC) raise complaints for their own building."
        )
        st.table(
            [
                {
                    "ID": p.person_id,
                    "Name": p.name,
                    "Type": "FM staff" if p.role == "facility_staff" else "Occupant",
                    "Designation": p.designation,
                    "Registered at": p.facility_id,
                }
                for p in repo.list_people()
            ],
            hide_index=True,
        )
    st.stop()

# =============================================================================
# Signed in
# =============================================================================
person = repo.get_person(st.session_state.user_id)
if person is None:  # e.g. the database was re-seeded underneath us
    sign_out()
    st.rerun()
assert person is not None
is_staff = access.can_work_at_any_facility(person)
config = thread_config(st.session_state.thread_id)


def switch_facility() -> None:
    """Sidebar callback: change the working facility, keep the conversation."""
    new = st.session_state.working_facility
    old = st.session_state.facility_id
    if new == old:
        return
    st.session_state.facility_id = new
    note = (
        f"Switched working facility from **{old}** to **{facility_label(facilities[new])}**. "
        "Equipment, compliance and new tickets now refer to this site; "
        "any unfinished ticket draft for the previous site was discarded."
    )
    try:
        if graph.get_state(config).values:
            graph.update_state(
                config,
                {
                    "slots": ConversationSlots(requester_id=st.session_state.user_id, facility_id=new),
                    "awaiting": None,
                    "pending_work_order": None,
                    "messages": [AIMessage(content=note)],
                },
                as_node=RESPOND,
            )
    except Exception:  # never block the switch on a state-update problem
        log.exception("Could not update graph state on facility switch")
    st.session_state.history.append(note_entry(note))
    log.info("%s switched facility %s -> %s", st.session_state.user_id, old, new)


# --- Sidebar ------------------------------------------------------------------
with st.sidebar:
    st.title("🏢 Facility Ops Assistant")
    st.caption(
        f"Model: `{llm.model_label()}`  \nKey: {api_key_origin()}  \n"
        f"Data as of: **{settings.today():%d-%b-%Y}**"
    )

    with st.container(border=True):
        role = "FM staff" if is_staff else "Occupant"
        st.markdown(
            f"**{person.name}** · `{person.person_id}`  \n{role} · {person.designation}  \n"
            f"{person.organisation}  \nRegistered at {person.facility_id}"
        )
        st.session_state.working_facility = st.session_state.facility_id
        st.selectbox(
            "Working facility",
            options=list(facilities),
            format_func=lambda fid: facility_label(facilities[fid]),
            key="working_facility",
            on_change=switch_facility,
            disabled=not is_staff,
            help="FM staff can switch sites at any time."
            if is_staff
            else "Occupants can only use the building they are registered at.",
        )
        if st.button("🔁 Switch user / sign out", width="stretch"):
            sign_out()
            st.rerun()

    st.subheader("This conversation")
    try:
        render_session_context(graph.get_state(config).values)
    except Exception:  # context panel must never break the page
        log.exception("Could not render session context")
    if st.button("🗑️ Clear conversation", type="primary", width="stretch"):
        new_conversation()
        st.rerun()

    with st.expander("💬 Try these", expanded=True):
        for i, sample in enumerate(sample_prompts(person, st.session_state.facility_id)):
            if st.button(sample, key=f"sample_{i}", width="stretch", disabled=bool(llm_problem)):
                st.session_state.queued_prompt = sample
                st.rerun()

    st.caption(
        "All buildings, people and vendors are fictional. Compliance text is an illustrative "
        "paraphrase, not legal advice."
    )

# --- Main chat ----------------------------------------------------------------
site = facilities[st.session_state.facility_id]
st.header("Facility Operations Assistant")
st.caption(
    f"Working on **{facility_label(site)}**, {site.city} · SOPs & compliance rules · asset, AMC & ticket "
    "lookup · work orders & complaints · compliance status"
)

if llm_problem:
    st.error(f"**Language model not configured.** {llm_problem}", icon="🔑")

if not st.session_state.history:
    with st.chat_message("assistant", avatar="🏢"):
        first = person.name.split()[0]
        if is_staff:
            what = "maintenance procedures, equipment AMC and service history, statutory compliance, or raise a work order"
        else:
            what = "maintenance and safety guidance, report a problem as a complaint, or check a complaint you raised"
        st.markdown(
            f"Hello {first}! You're working on **{site.name}**. Ask me about {what}. "
            "Sample prompts are in the sidebar."
        )

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(entry["content"])
    else:
        with st.chat_message("assistant", avatar="🏢"):
            render_assistant(entry)

typed = st.chat_input(
    "Ask a question or report an issue…" if not llm_problem else "Add an API key to .env to start chatting",
    disabled=bool(llm_problem),
)
prompt = typed or st.session_state.pop("queued_prompt", None)

if prompt and not llm_problem:
    session = SessionContext(requester_id=person.person_id, facility_id=st.session_state.facility_id)
    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="🏢"):
        with st.spinner("Working on it…"):
            try:
                entry = assistant_entry(run_turn(graph, st.session_state.thread_id, prompt, session=session))
            except Exception:  # last-resort guard: never show a stack trace
                log.exception("Turn failed")
                entry = error_entry("Something went wrong while handling that message. Please try again.")
        render_assistant(entry)
    st.session_state.history.append(entry)
    st.rerun()  # refresh the sidebar context (pending draft etc.)
