# Browser end-to-end tests (Playwright)

`run_e2e.py` starts the app on a **fresh, separate database** (your
`data/facility_ops.db` is never touched), drives it in Chromium like a user,
checks every reply, and writes a report with a screenshot per step.

## Setup (once)

```
pip install -r requirements-dev.txt
python -m playwright install chromium
```

## Run

```
python e2e/run_e2e.py --fake-llm      # offline: scripted model, free, repeatable (~2 min)
python e2e/run_e2e.py                 # real Gemini from your .env (~100 API calls, 15-20 min)
python e2e/run_e2e.py --only A1,D8    # selected scenarios
python e2e/run_e2e.py --headed        # watch the browser
python e2e/run_e2e.py --save-sample   # also copy the run to docs/sample_run/ (committed sample output)
python e2e/record_demo.py             # record a captioned demo video to demo_video/ (~30 model calls)
```

Output: `e2e_reports/<timestamp>/report.md` (pass/fail per step, reply
excerpt, screenshot), `results.json`, `screenshots/`. Exit code 1 if anything failed.

## What is covered

34 chat scenarios (65 steps) in `scenarios.py`, mirroring the manual test list:
knowledge search, including answers from the .txt, .docx and .pdf documents (A), asset lookup, follow-ups and ticket status without a ticket number (B), compliance and switching
the working facility (C), work orders, duplicates, cancel, emergencies and a
work order at another site (D), access control (E), multiple requests in one
message (F), robustness (G).

Before each scenario the runner signs out and signs in on the sign-in screen as
`Scenario.user` (at `Scenario.facility`, default: registered site), so the
messages themselves carry no IDs. A step created with `Switch("FAC-002", ...)`
changes the sidebar's **Working facility** instead of chatting and checks the
note posted in the chat. Scenarios with `signin_expect` test the sign-in screen
itself: an unknown ID (G2) and an occupant choosing another building (E5). Three environment scenarios
start their own app instance: no API key (H1: banner on the sign-in page, chat
disabled after sign-in), unknown model name (H2, real mode only), missing seed
data (H3).

## How checks work

Each step checks the latest assistant message **with its Agent actions panel
expanded**: answer text, follow-up questions, sources, tool status badges
("duplicate - not created", "not permitted"...), graph path and tables.
Checks target record IDs, statuses and node names rather than wording, so
they hold for real model runs. Dates match any common format (`day()` helper).

Checks for newly created tickets accept any number above the seed data
(CMP-2026-0018+, WO-2026-0035+), so `--only` subsets pass in any combination.

## Offline vs real

- **Offline (`--fake-llm`)** uses `fake_app.py`: each step's `fake=` routing
  decision is turned into the tool calls Gemini would make (`decision_to_tool_calls`),
  and the "answer" is the raw tool output. It tests
  the whole app except the model's judgment. Use it after every code change.
- **Real** tests Gemini's routing, entity extraction and wording as well. An
  occasional failure here can be model variance: read the reply excerpt in the
  report before changing code. `--pause` spaces out calls (default 4 s), and a
  rate-limit reply triggers one retry after `--rate-wait` seconds.
