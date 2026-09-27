"""Shared helpers for the tools: error wrapping, identity check, date states."""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from datetime import date

from pydantic import ValidationError

from facility_ops.config import get_settings
from facility_ops.data_access import repositories as repo
from facility_ops.data_access.db import DataUnavailableError
from facility_ops.logging_config import get_logger
from facility_ops.schemas import (
    FIELD_PROMPTS,
    CoverageState,
    Intent,
    Person,
    ToolResult,
    ToolStatus,
    normalise_id,
)

log = get_logger("tools")


def safe_tool(intent: Intent) -> Callable:
    """Turn any exception inside a tool into a ToolResult(status=ERROR).

    One failing tool must never crash the conversation; the respond node
    tells the user what failed and the details go to the log.
    """

    def decorator(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> ToolResult:
            try:
                result = fn(*args, **kwargs)
                log.info("%s -> %s: %s", fn.__name__, result.status.value, result.summary)
                return result
            except DataUnavailableError as exc:
                log.error("%s data unavailable: %s", fn.__name__, exc)
                msg, err = "The facility data is currently unavailable.", str(exc)
            except sqlite3.Error as exc:
                log.exception("%s database error", fn.__name__)
                msg, err = "The facility database returned an error.", f"database error: {exc}"
            except ValidationError as exc:
                log.warning("%s invalid input: %s", fn.__name__, exc)
                msg, err = "The request had invalid values.", exc.errors()[0]["msg"]
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                log.exception("%s failed unexpectedly", fn.__name__)
                msg, err = "The tool failed unexpectedly.", f"{type(exc).__name__}: {exc}"
            return ToolResult(tool=intent, status=ToolStatus.ERROR, summary=msg, error=err)

        return wrapper

    return decorator


def needs_input(intent: Intent, fields: list[str], lead: str = "") -> ToolResult:
    asks = [FIELD_PROMPTS.get(f, f.replace("_", " ")) for f in fields]
    prefix = (lead + " ") if lead else ""
    if len(asks) == 1:
        question = f"{prefix}Please share {asks[0]}."
    else:  # a short list reads better than one long sentence
        question = f"{prefix}Please share:\n" + "\n".join(f"- {a}" for a in asks)
    return ToolResult(
        tool=intent,
        status=ToolStatus.NEEDS_INPUT,
        summary=f"Missing: {', '.join(fields)}",
        data={"missing_fields": fields},
        follow_up_question=question.strip(),
    )


def resolve_requester(intent: Intent, requester_id: str | None) -> Person | ToolResult:
    """Return the Person, or a NEEDS_INPUT result if absent/unknown."""
    rid = normalise_id(requester_id)
    if not rid:
        return needs_input(intent, ["requester_id"])
    person = repo.get_person(rid)
    if person is None:
        return needs_input(intent, ["requester_id"], lead=f"I couldn't find anyone with ID {rid}.")
    return person


def not_permitted(intent: Intent, reason: str) -> ToolResult:
    return ToolResult(tool=intent, status=ToolStatus.NOT_PERMITTED, summary=reason, error=None)


def coverage_state(end: date | None, today: date | None = None) -> CoverageState:
    """Warranty / AMC state from its end date."""
    if end is None:
        return CoverageState.NONE
    today = today or get_settings().today()
    days = (end - today).days
    if days < 0:
        return CoverageState.EXPIRED
    if days <= get_settings().coverage_expiring_days:
        return CoverageState.EXPIRING_SOON
    return CoverageState.ACTIVE
