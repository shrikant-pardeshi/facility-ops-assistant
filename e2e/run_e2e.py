"""
Browser end-to-end tests for the Facility Operations Assistant (Playwright).

Starts the Streamlit app on a FRESH, separate database (your data/facility_ops.db
is never touched), drives it in Chromium - signing in as each scenario's user,
chatting, switching the working facility - checks every reply, and writes a
report with a screenshot per step.

    # real Gemini run (uses your .env key; ~2 API calls per step)
    python e2e/run_e2e.py

    # offline run: scripted model, no API calls, fully repeatable
    python e2e/run_e2e.py --fake-llm

Options:
    --only A1,D8     run selected scenarios (ids from e2e/scenarios.py; env ids H1..H3)
    --headed         show the browser while it runs
    --pause 4        seconds between messages (real runs; helps with rate limits)
    --skip-env       skip the environment scenarios (no key / bad model / missing data)
    --base-url URL   test an app you already started (no fresh DB; ticket-number checks may fail)
    --save-sample    also copy the report + screenshots to docs/sample_run/ (committed sample output)

Setup (once):
    pip install -r requirements-dev.txt
    python -m playwright install chromium

Output: e2e_reports/<timestamp>/report.md, results.json, screenshots/*.png
Exit code: 0 if everything passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Page, sync_playwright  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402

from e2e.scenarios import ENV_SCENARIOS, SCENARIOS, EnvScenario, Scenario, Step  # noqa: E402

# Windows consoles may not be UTF-8; never crash on printing.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(errors="replace")

MSG = '[data-testid="stChatMessage"]'
RATE_LIMITED = re.compile(r"rate limit or quota|did not respond in time", re.I)
# Account / setup problems: retrying cannot help, so the run stops at once.
FATAL_LLM = re.compile(r"run out of prepaid credits|rejected the API key|is not available for this API key", re.I)


class FatalLLMError(Exception):
    """The model account cannot serve requests (credits, key, model name)."""

# Fixed settings so results don't depend on the tester's .env tweaks.
# (python-dotenv never overrides variables that are already set.)
PINNED_ENV = {
    "AS_OF_DATE": "2026-10-01",
    "DUE_SOON_DAYS": "30",
    "UPCOMING_DAYS": "90",
    "COVERAGE_EXPIRING_DAYS": "60",
    "KB_TOP_K": "8",
    "KB_MIN_SCORE": "2.0",
    "SEED_DIR": "data/seed",
    "KB_DIR": "data/knowledge_base",
    "PYTHONIOENCODING": "utf-8",
}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    scenario: str
    step: int
    say: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    screenshot: str = ""
    reply_excerpt: str = ""


# ---------------------------------------------------------------------------
# App server
# ---------------------------------------------------------------------------


@contextmanager
def app_server(port: int, env_overrides: dict[str, str], script: Path, log_path: Path):
    env = {**os.environ, **PINNED_ENV, **env_overrides}
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(script),
        "--server.headless", "true", "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
    ]
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        url = f"http://localhost:{port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Streamlit exited early - see {log_path}")
            try:
                with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as r:
                    if r.read().strip() == b"ok":
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Streamlit did not start within 60 s - see {log_path}")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------


def wait_idle(page: Page, timeout_s: float, settle_s: float = 1.2) -> None:
    """Wait until Streamlit has finished running the script (and stays idle)."""
    app = page.locator('[data-testid="stApp"]')
    deadline = time.time() + timeout_s
    idle_since = None
    while time.time() < deadline:
        if app.get_attribute("data-test-script-state") == "notRunning" and page.locator(
            '[data-testid="stSpinner"]'
        ).count() == 0:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= settle_s:
                return
        else:
            idle_since = None
        page.wait_for_timeout(200)
    raise TimeoutError(f"app still busy after {timeout_s:.0f} s")


def last_message_text(page: Page) -> str:
    """Text of the newest chat message. `.last` re-resolves on every attempt, so a
    Streamlit re-render between counting and reading can't leave a stale handle."""
    for _ in range(10):
        try:
            if page.locator(MSG).count() == 0:
                return ""
            return page.locator(MSG).last.inner_text(timeout=3000)
        except PlaywrightTimeout:
            page.wait_for_timeout(300)
    return ""


def send(page: Page, text: str, timeout_s: float) -> None:
    before = last_message_text(page)
    box = page.get_by_test_id("stChatInputTextArea")
    box.fill(text)
    box.press("Enter")
    deadline = time.time() + timeout_s
    while time.time() < deadline:  # wait for a new assistant message to replace the old last one
        page.wait_for_timeout(300)
        current = last_message_text(page).strip()
        # the newest message must be the assistant's reply, not our own message echoed back
        if current and current != before.strip() and current != text.strip():
            break
    wait_idle(page, max(5, deadline - time.time()))


def expand_agent_actions(page: Page) -> None:
    toggle = page.locator(MSG).last.get_by_text("Agent actions")
    if toggle.count():
        toggle.first.click()
        page.wait_for_timeout(500)


def screenshot_last_message(page: Page, path: Path) -> None:
    """Screenshot the newest message in full.

    Streamlit scrolls inside its own container, so an element taller than the
    window renders partly blank. Temporarily grow the window to fit it.
    """
    msg = page.locator(MSG).last
    box = msg.bounding_box()
    original = page.viewport_size or {"width": 1400, "height": 1000}
    needed = int(box["height"]) + 400 if box else original["height"]
    if needed > original["height"]:
        page.set_viewport_size({"width": original["width"], "height": min(needed, 8000)})
        page.wait_for_timeout(500)
    try:
        msg.scroll_into_view_if_needed()
        msg.screenshot(path=str(path))
    finally:
        page.set_viewport_size(original)


def clear_conversation(page: Page, timeout_s: float) -> None:
    page.get_by_role("button", name=re.compile("Clear conversation")).click()
    wait_idle(page, timeout_s)


def choose_option(page: Page, scope, label: str, option_pattern: str, timeout_s: float) -> None:
    """Pick an option in a Streamlit selectbox (a combobox + popover list)."""
    box = scope.get_by_test_id("stSelectbox").filter(has_text=label).first
    box.click()
    page.get_by_role("option", name=re.compile(option_pattern)).first.click()
    wait_idle(page, timeout_s)


def sign_out(page: Page, timeout_s: float) -> None:
    button = page.get_by_role("button", name=re.compile("Switch user"))
    if button.count():
        button.click()
        wait_idle(page, timeout_s)


def sign_in(page: Page, user: str, facility: str | None, timeout_s: float) -> str:
    """Fill in the sign-in form. Returns '' on success, else the error shown."""
    sign_out(page, timeout_s)
    page.get_by_label("Employee or occupant ID").fill(user)
    # Always set the dropdown: a failed attempt leaves the previous choice selected.
    option = rf"^{re.escape(facility)}\b" if facility else r"^My registered facility"
    choose_option(page, page.get_by_test_id("stMain"), "Facility", option, timeout_s)
    page.get_by_role("button", name="Sign in").click()
    wait_idle(page, timeout_s)
    if page.get_by_test_id("stChatInputTextArea").count():
        return ""
    alerts = page.get_by_test_id("stAlert")
    return alerts.last.inner_text() if alerts.count() else "(sign-in did not complete)"


def switch_facility(page: Page, facility: str, timeout_s: float) -> None:
    sidebar = page.get_by_test_id("stSidebar")
    choose_option(page, sidebar, "Working facility", rf"^{re.escape(facility)}\b", timeout_s)


def check(text: str, sidebar: str, step: Step) -> list[str]:
    failures = []
    for p in step.expect:
        if not re.search(p, text, re.I):
            failures.append(f"missing: {p}")
    if step.expect_any and not any(re.search(p, text, re.I) for p in step.expect_any):
        failures.append(f"none of: {step.expect_any}")
    for p in step.reject:
        if re.search(p, text, re.I):
            failures.append(f"should not contain: {p}")
    for p in step.sidebar_expect:
        if not re.search(p, sidebar, re.I):
            failures.append(f"sidebar missing: {p}")
    for p in step.sidebar_reject:
        if re.search(p, sidebar, re.I):
            failures.append(f"sidebar should not contain: {p}")
    return failures


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


class _StepDone(Exception):
    """Internal: a non-chat step (facility switch) finished its checks."""


def run_step(page: Page, sc_id: str, idx: int, step: Step, args, shots: Path, detect_fatal: bool = True) -> StepResult:
    t0 = time.time()
    failures: list[str] = []
    text = sidebar = ""
    try:
        if step.switch_facility:
            switch_facility(page, step.switch_facility, args.timeout)
            text = last_message_text(page)
            sidebar = page.locator('[data-testid="stSidebar"]').inner_text()
            failures = check(text, sidebar, step)
            raise _StepDone
        send(page, step.say, args.timeout)
        text = last_message_text(page)
        if detect_fatal and not args.fake_llm and FATAL_LLM.search(text):
            raise FatalLLMError(re.sub(r"\s+", " ", text)[:300])
        if not args.fake_llm and RATE_LIMITED.search(text):
            print(f"      rate-limited - waiting {args.rate_wait}s and retrying once")
            time.sleep(args.rate_wait)
            send(page, step.say, args.timeout)
        expand_agent_actions(page)
        text = last_message_text(page)
        sidebar = page.locator('[data-testid="stSidebar"]').inner_text()
        failures = check(text, sidebar, step)
    except _StepDone:
        pass
    except FatalLLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - report, don't abort the suite
        failures = [f"harness error: {type(exc).__name__}: {exc}"]

    # Passing step: screenshot just the reply. Failing step: the whole page, for context.
    shot = shots / f"{sc_id}_{idx}.png"
    try:
        if page.locator(MSG).count() and not failures:
            screenshot_last_message(page, shot)
        else:
            page.screenshot(path=str(shot), full_page=True)
    except Exception:  # noqa: BLE001
        shot = Path("")
    return StepResult(
        scenario=sc_id,
        step=idx,
        say=step.say,
        passed=not failures,
        failures=failures,
        seconds=round(time.time() - t0, 1),
        screenshot=shot.relative_to(shots.parent).as_posix() if shot.name else "",
        reply_excerpt=re.sub(r"\s+", " ", text)[:300],
    )


def run_chat_scenarios(page: Page, url: str, scenarios: list[Scenario], args, shots: Path) -> list[StepResult]:
    results: list[StepResult] = []
    page.goto(url)
    page.get_by_role("button", name="Sign in").wait_for(timeout=30000)
    wait_idle(page, args.timeout)
    for sc in scenarios:
        where = f" @ {sc.facility}" if sc.facility else ""
        print(f"\n[{sc.id}] {sc.group}: {sc.title}  (as {sc.user}{where})")
        t0 = time.time()
        error = sign_in(page, sc.user, sc.facility, args.timeout)
        if sc.signin_expect or error:
            # Either this scenario tests the sign-in screen, or sign-in unexpectedly failed.
            failures = [f"sign-in failed: {error}"] if error and not sc.signin_expect else []
            if sc.signin_expect:
                if not error:
                    failures.append("sign-in succeeded but should have been refused")
                failures += [f"missing: {p}" for p in sc.signin_expect if not re.search(p, error, re.I)]
            shot = shots / f"{sc.id}_0.png"
            page.screenshot(path=str(shot), full_page=True)
            results.append(StepResult(sc.id, 0, f"(sign in as {sc.user}{where})", not failures, failures,
                                      round(time.time() - t0, 1), shot.relative_to(shots.parent).as_posix(),
                                      re.sub(r"\s+", " ", error)[:300]))
            print(f"  {'PASS' if not failures else 'FAIL'}  sign-in check")
            for f in failures:
                print(f"          - {f}")
            if sc.signin_expect or failures:
                continue
        for i, step in enumerate(sc.steps, start=1):
            try:
                r = run_step(page, sc.id, i, step, args, shots)
            except FatalLLMError as exc:
                args.fatal = str(exc)
                results.append(StepResult(sc.id, i, step.say, False, [f"model unavailable: {exc}"]))
                print(f"\n  STOP  The language model cannot serve requests:\n        {exc}\n"
                      "        Retrying will not help. Fix the account/key in .env, then run again.")
                return results
            results.append(r)
            mark = "PASS" if r.passed else "FAIL"
            print(f"  {mark}  {i}. {step.say[:70]}  ({r.seconds}s)")
            for f in r.failures:
                print(f"          - {f}")
            if args.pause and not args.fake_llm:
                time.sleep(args.pause)
    return results


def run_env_scenario(pw, env_sc: EnvScenario, args, run_dir: Path, port: int) -> list[StepResult]:
    shots = run_dir / "screenshots"
    script = ROOT / ("e2e/fake_app.py" if env_sc.use_fake_wrapper else "app.py")
    overrides = {"DB_PATH": str(run_dir / f"{env_sc.id}.db"), **env_sc.env}
    print(f"\n[{env_sc.id}] Environment: {env_sc.title}")
    results: list[StepResult] = []
    with app_server(port, overrides, script, run_dir / f"{env_sc.id}_server.log") as url:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        t0 = time.time()
        failures = []
        try:
            page.goto(url)
            page.wait_for_timeout(1500)
            wait_idle(page, args.timeout)
            body = page.locator("body").inner_text()
            for p in env_sc.page_expect:
                if not re.search(p, body, re.I):
                    failures.append(f"page missing: {p}")
            if env_sc.user:
                error = sign_in(page, env_sc.user, None, args.timeout)
                if error and not page.get_by_test_id("stChatInputTextArea").count():
                    failures.append(f"sign-in failed: {error}")
            if env_sc.chat_disabled is not None:
                disabled = page.get_by_test_id("stChatInputTextArea").is_disabled()
                if disabled != env_sc.chat_disabled:
                    failures.append(f"chat input disabled={disabled}, expected {env_sc.chat_disabled}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"harness error: {type(exc).__name__}: {exc}")
        shot = shots / f"{env_sc.id}_0.png"
        page.screenshot(path=str(shot), full_page=True)
        if env_sc.page_expect or env_sc.chat_disabled is not None:
            results.append(
                StepResult(env_sc.id, 0, "(page load)", not failures, failures, round(time.time() - t0, 1),
                           shot.relative_to(run_dir).as_posix())
            )
            print(f"  {'PASS' if not failures else 'FAIL'}  page check")
            for f in failures:
                print(f"          - {f}")
        for i, step in enumerate(env_sc.steps, start=1):
            r = run_step(page, env_sc.id, i, step, args, shots, detect_fatal=False)
            results.append(r)
            print(f"  {'PASS' if r.passed else 'FAIL'}  {i}. {step.say}")
            for f in r.failures:
                print(f"          - {f}")
        browser.close()
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(run_dir: Path, results: list[StepResult], args, started: datetime) -> None:
    by_sc: dict[str, list[StepResult]] = {}
    for r in results:
        by_sc.setdefault(r.scenario, []).append(r)
    titles = {s.id: f"{s.group}: {s.title}" for s in SCENARIOS} | {e.id: f"Environment: {e.title}" for e in ENV_SCENARIOS}
    passed = sum(r.passed for r in results)
    mode = "offline (scripted model)" if args.fake_llm else "real LLM"
    lines = [
        "# E2E test report",
        "",
        f"- Started: {started:%Y-%m-%d %H:%M:%S} · Mode: **{mode}**",
        *([f"- ⛔ **Run stopped early - the language model could not serve requests:** {args.fatal}"] if args.fatal else []),
        f"- Steps passed: **{passed}/{len(results)}** · Scenarios fully passing: "
        f"**{sum(all(r.passed for r in rs) for rs in by_sc.values())}/{len(by_sc)}**",
        "",
        "| Scenario | Result | Steps |",
        "|---|---|---|",
    ]
    for sid, rs in by_sc.items():
        ok = all(r.passed for r in rs)
        lines.append(f"| {sid} - {titles.get(sid, '')} | {'✅ pass' if ok else '❌ FAIL'} | {sum(r.passed for r in rs)}/{len(rs)} |")
    lines.append("")
    for sid, rs in by_sc.items():
        lines += [f"## {sid} - {titles.get(sid, '')}", ""]
        for r in rs:
            lines.append(f"**{r.step}. {'✅' if r.passed else '❌'}** `{r.say}` ({r.seconds}s)")
            for f in r.failures:
                lines.append(f"- ❗ {f}")
            if r.reply_excerpt:
                lines.append(f"> {r.reply_excerpt}")
            if r.screenshot:
                lines.append(f"\n![{sid}-{r.step}]({r.screenshot})")
            lines.append("")
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (run_dir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")


def save_sample(run_dir: Path) -> None:
    """Replace docs/sample_run/ with this run (report, results, screenshots)."""
    import shutil

    target = ROOT / "docs" / "sample_run"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ("report.md", "results.json"):
        shutil.copy2(run_dir / name, target / name)
    shutil.copytree(run_dir / "screenshots", target / "screenshots")
    print(f"Sample output saved to {target}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake-llm", action="store_true", help="scripted model, no API calls")
    ap.add_argument("--only", default="", help="comma-separated scenario ids, e.g. A1,D8,H1")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between messages in real runs")
    ap.add_argument("--rate-wait", type=int, default=60, help="seconds to wait after a rate-limit reply")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds allowed per message")
    ap.add_argument("--skip-env", action="store_true", help="skip environment scenarios")
    ap.add_argument("--base-url", default="", help="use an already-running app instead of starting one")
    ap.add_argument("--port", type=int, default=8599)
    ap.add_argument("--save-sample", action="store_true", help="copy this run to docs/sample_run/")
    args = ap.parse_args()
    args.fatal = ""

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    chat = [s for s in SCENARIOS if not only or s.id in only]
    env = [] if args.skip_env or args.base_url else [e for e in ENV_SCENARIOS if not only or e.id in only]
    if args.fake_llm:
        skipped = [e.id for e in env if e.needs_real_llm]
        env = [e for e in env if not e.needs_real_llm]
        if skipped:
            print(f"(offline mode: skipping {', '.join(skipped)} - needs the real LLM)")

    started = datetime.now()
    run_dir = ROOT / "e2e_reports" / started.strftime("%Y%m%d-%H%M%S")
    (run_dir / "screenshots").mkdir(parents=True)
    print(f"Report folder: {run_dir}")

    results: list[StepResult] = []
    with sync_playwright() as pw:
        if chat:
            script = ROOT / ("e2e/fake_app.py" if args.fake_llm else "app.py")
            if args.base_url:
                ctx = nullcontext(args.base_url)
            else:
                ctx = app_server(args.port, {"DB_PATH": str(run_dir / "e2e.db")}, script, run_dir / "server.log")
            with ctx as url:
                browser = pw.chromium.launch(headless=not args.headed)
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                results += run_chat_scenarios(page, url, chat, args, run_dir / "screenshots")
                browser.close()
        if args.fatal:
            env = [e for e in env if not e.needs_real_llm]
        for i, e in enumerate(env):
            results += run_env_scenario(pw, e, args, run_dir, args.port + 1 + i)

    write_report(run_dir, results, args, started)
    failed = [r for r in results if not r.passed]
    if args.save_sample and args.fatal:
        print("Not saving a sample: the run stopped early.")
    elif args.save_sample:
        save_sample(run_dir)
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed. Report: {run_dir / 'report.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
