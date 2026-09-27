"""
Generate the knowledge-base sample documents that are NOT Markdown, so the
knowledge base demonstrates several input formats:

    data/knowledge_base/sops/SOP-SEC-001_access_card_and_door_faults.txt
    data/knowledge_base/sops/SOP-HK-001_washroom_pantry_meeting_room_housekeeping.docx
    data/knowledge_base/compliance/REG-FIRE-003_fire_drills_and_evacuation.pdf

The generated files are committed; run this only to recreate them.

    pip install -r requirements-dev.txt      # reportlab (PDF writer)
    python scripts/make_sample_docs.py

All content is fictional / illustrative. The compliance text is a paraphrase
for demonstration, not legal text.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "data" / "knowledge_base"

# ---------------------------------------------------------------------------
# TXT - plain text with a small header and ALL-CAPS headings
# ---------------------------------------------------------------------------

TXT = """\
Title: Access Card Reader and Door Access Faults
Tags: access card, card reader, door, access control, security, badge, turnstile, not working, locked out
Doc-Type: sop

Scope: all card-controlled doors, turnstiles and lift-lobby readers in the building.
Owner: Security Supervisor. Reviewed: 2026-05-15.

WHO TO CONTACT
Occupants report access card or door faults to the helpdesk as a complaint (category: security).
The helpdesk assigns it to the security team, who work with the access-control vendor if needed.
For a door that will not lock or an alarm that keeps sounding, call the security desk immediately.

FIRST CHECKS BY THE USER
1. Try the card on a second reader on the same floor. If it works there, the first reader is faulty.
2. Check the card is not bent, cracked or near a phone case magnet.
3. Ask a colleague to try their card on the same reader.
If only your card fails on every reader, the card needs to be re-encoded or replaced (go to the security desk with your ID).

READER NOT RESPONDING (SECURITY TEAM)
1. Check the reader LED: no light means no power; steady red means offline from the controller.
2. Power-cycle the reader from the door controller panel (label shows the door ID).
3. If it stays offline, check the network link to the controller and raise it with the access-control vendor.
4. While the reader is down, post a guard or keep the door on managed access. Never prop fire doors open.
Target response: P3 - 24 hours; a main entrance or lift-lobby reader down for everyone is P2 - 4 hours.

DOOR WILL NOT LOCK OR KEEPS ALARMING
1. Treat as a security risk: station a guard until fixed.
2. Check the door closer and magnetic lock alignment; clear any obstruction.
3. Check the door contact sensor; log every forced-door or held-open alarm in the security register.

FIRE ALARM AND ACCESS CONTROL
On a fire alarm, card-controlled doors on escape routes release automatically (fail-safe).
After the all-clear, the security team confirms every door has re-locked and resets the controller.

RECORDS
Log every fault, the action taken and the closure time against the ticket. Review repeat faults monthly.
"""

# ---------------------------------------------------------------------------
# DOCX - Word document with Title, Heading 2 sections, bullets and a table
# ---------------------------------------------------------------------------

DOCX_TITLE = "Housekeeping - Washrooms, Pantries and Meeting Rooms"
DOCX_KEYWORDS = (
    "housekeeping, cleaning, washroom, toilet, pantry, meeting room, conference room, "
    "not cleaned, dirty, garbage, hygiene, consumables, soap, tissue"
)
DOCX_SECTIONS: list[tuple[str, list[str], list[list[str]] | None]] = [
    (
        "Cleaning schedule",
        ["Routine cleaning follows the frequency table below. Supervisors check each area against its checklist."],
        [
            ["Area", "Routine cleaning", "Deep cleaning"],
            ["Washrooms", "Every 2 hours (07:00-21:00)", "Weekly, Saturday"],
            ["Pantries", "Every 3 hours and after lunch", "Weekly"],
            ["Meeting rooms", "Before 09:00 daily and after each booked meeting", "Monthly"],
            ["Lift lobbies and corridors", "Twice daily", "Monthly"],
        ],
    ),
    (
        "Consumables",
        [
            "- Soap, tissue and hand towels are checked at every washroom round and refilled below 25%.",
            "- Pantry supplies (cups, dish soap) are checked at every pantry round.",
            "- Housekeeping supervisors record consumable stock daily and reorder at the minimum level.",
        ],
        None,
    ),
    (
        "Reporting a missed or poor cleaning",
        [
            "Occupants raise a complaint with the helpdesk (category: housekeeping), giving the floor and room.",
            "Housekeeping requests are priority P4, target response within 72 hours; a missed meeting-room "
            "clean before a booked meeting is attended within 30 minutes when reported by phone.",
            "The supervisor inspects the area, arranges re-cleaning and closes the ticket with the time of the fix.",
        ],
        None,
    ),
    (
        "Spills and hygiene incidents",
        [
            "- Wet floors are coned off immediately and cleaned; the cone stays until the floor is dry.",
            "- Water leaks are reported to the MEP team (see SOP-PLMB-001), not only mopped.",
            "- Blocked toilets and overflowing bins are treated as urgent and attended within 1 hour.",
        ],
        None,
    ),
]

# ---------------------------------------------------------------------------
# PDF - compliance paraphrase with a title, headings and body text
# ---------------------------------------------------------------------------

PDF_TITLE = "Fire Drills and Evacuation Training"
PDF_KEYWORDS = (
    "mock drill, evacuation drill, drill frequency, drill register, warden, assembly point, "
    "headcount, evacuation training, how often drill"
)
PDF_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "Illustrative content",
        [
            "This document is an illustrative paraphrase prepared for a demonstration project. It is not legal "
            "text; always follow the current rules issued by the local fire authority.",
        ],
    ),
    (
        "Drill frequency",
        [
            "A full-building evacuation (mock) drill is conducted at least twice a year, about six months apart. "
            "At least one drill each year is held without advance notice to occupants.",
            "New tenants join the next scheduled drill; floor wardens are trained before their first drill.",
        ],
    ),
    (
        "Warden and occupant roles",
        [
            "Floor wardens sweep their floor, check washrooms and meeting rooms, and report \"floor clear\" to the "
            "command centre. Occupants use the nearest staircase, never the lifts, and gather at the "
            "assembly point for a headcount.",
        ],
    ),
    (
        "Evacuation time target",
        [
            "The building is expected to be fully evacuated within the time set in the approved evacuation plan. "
            "Each drill records the time from the start of the drill to the last person out, and the headcount.",
        ],
    ),
    (
        "Records to keep",
        [
            "Keep a drill register with the date, time, evacuation time, headcount, observations and "
            "corrective actions. Inspectors may ask to see the drill register (see REG-FIRE-001).",
        ],
    ),
    (
        "After the drill",
        [
            "Hold a debrief within one week, assign each corrective action an owner and a due date, and raise "
            "work orders for any defects found during the drill (for example a door that did not release).",
        ],
    ),
]


def write_txt() -> Path:
    path = KB / "sops" / "SOP-SEC-001_access_card_and_door_faults.txt"
    path.write_text(TXT, encoding="utf-8")
    return path


def write_docx() -> Path:
    import docx

    document = docx.Document()
    props = document.core_properties
    props.title = DOCX_TITLE
    props.keywords = DOCX_KEYWORDS
    props.category = "sop"
    props.subject = "SOP-HK-001"
    props.author = "Apex Integrated FM Services (fictional)"
    document.add_paragraph(DOCX_TITLE, style="Title")
    document.add_paragraph("Owner: Housekeeping Supervisor. Reviewed: 2026-06-01. Applies to all floors.")
    for heading, paras, table in DOCX_SECTIONS:
        document.add_heading(heading, level=2)
        for text in paras:
            if text.startswith("- "):
                document.add_paragraph(text[2:], style="List Bullet")
            else:
                document.add_paragraph(text)
        if table:
            t = document.add_table(rows=0, cols=len(table[0]))
            t.style = "Light Grid Accent 1"
            for row in table:
                cells = t.add_row().cells
                for cell, value in zip(cells, row):
                    cell.text = value
    path = KB / "sops" / "SOP-HK-001_washroom_pantry_meeting_room_housekeeping.docx"
    document.save(str(path))
    return path


def write_pdf() -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    path = KB / "compliance" / "REG-FIRE-003_fire_drills_and_evacuation.pdf"
    styles = getSampleStyleSheet()
    story = [Paragraph(PDF_TITLE, styles["Title"]), Spacer(1, 8)]
    for heading, paras in PDF_SECTIONS:
        story.append(Paragraph(heading, styles["Heading2"]))
        story += [Paragraph(p, styles["BodyText"]) for p in paras]
        story.append(Spacer(1, 6))
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, title=PDF_TITLE, subject="compliance", keywords=PDF_KEYWORDS,
        author="Facility Operations Assistant sample data (fictional)",
    )
    doc.build(story)
    return path


def main() -> int:
    for fn in (write_txt, write_docx, write_pdf):
        print(f"wrote {fn().relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
