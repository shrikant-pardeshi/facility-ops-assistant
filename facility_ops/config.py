"""
Central configuration. Every tunable value comes from environment variables
(.env in development) - no hard-coded paths, thresholds or model names in
the rest of the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str  # "gemini" (default) | "openai"
    openai_api_key: str | None
    openai_model: str
    google_api_key: str | None
    gemini_model: str
    llm_temperature: float
    llm_timeout_s: int

    # Paths
    seed_dir: Path
    kb_dir: Path
    db_path: Path
    log_dir: Path
    log_level: str

    # Business rules
    as_of_date: date | None  # pin "today" for reproducible demos
    due_soon_days: int  # <= this many days to expiry -> DUE
    upcoming_days: int  # <= this many days to expiry -> UPCOMING
    coverage_expiring_days: int  # warranty/AMC "expiring soon" window
    kb_top_k: int
    kb_min_score: float  # best document BM25 score below this -> NOT_FOUND

    def today(self) -> date:
        return self.as_of_date or date.today()

    def llm_key_present(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "gemini":
            return bool(self.google_api_key)
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    as_of = os.getenv("AS_OF_DATE", "").strip()
    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        # Either name works: GOOGLE_API_KEY (LangChain) or GEMINI_API_KEY (Google AI Studio / SDK)
        google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        llm_temperature=_float("LLM_TEMPERATURE", 0.0),
        llm_timeout_s=_int("LLM_TIMEOUT_S", 30),
        seed_dir=_path("SEED_DIR", "data/seed"),
        kb_dir=_path("KB_DIR", "data/knowledge_base"),
        db_path=_path("DB_PATH", "data/facility_ops.db"),
        log_dir=_path("LOG_DIR", "logs"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        as_of_date=date.fromisoformat(as_of) if as_of else None,
        due_soon_days=_int("DUE_SOON_DAYS", 30),
        upcoming_days=_int("UPCOMING_DAYS", 90),
        coverage_expiring_days=_int("COVERAGE_EXPIRING_DAYS", 60),
        kb_top_k=_int("KB_TOP_K", 8),
        kb_min_score=_float("KB_MIN_SCORE", 2.0),
    )


def api_key_origin() -> str:
    """Which variable supplies the LLM key and where it comes from, e.g.
    "GEMINI_API_KEY in .env, ends ...a1b2". Shows only the last 4 characters.

    Variables already set in the operating system take priority over .env
    (python-dotenv never overrides them), which is a common source of
    "wrong key" surprises - so this says which one won.
    """
    s = get_settings()
    names = ("OPENAI_API_KEY",) if s.llm_provider == "openai" else ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    for name in names:
        value = os.getenv(name)
        if value:
            where = ".env" if file_values.get(name) == value else "system environment variable (overrides .env)"
            return f"{name} from {where}, ends ...{value[-4:]}"
    return "no API key found"
