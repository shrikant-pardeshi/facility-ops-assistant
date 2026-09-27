"""Logging setup: console + rotating file (logs/facility_ops.log)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from facility_ops.config import get_settings

_CONFIGURED = False


def setup_logging() -> None:
    """Idempotent - safe to call on every Streamlit rerun."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = get_settings()
    s.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(
        s.log_dir / "facility_ops.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger("facility_ops")
    root.setLevel(s.log_level)
    root.addHandler(file_handler)
    root.addHandler(console)
    root.propagate = False

    # Third-party noise: google-genai warns about "automatic function calling"
    # on every call. We use structured output, not AFC, so it is irrelevant.
    for noisy in ("google_genai", "google_genai.models", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"facility_ops.{name}")
