"""
LLM provider factory: Gemini (default) or OpenAI, via LangChain chat models.

The rest of the app calls two functions:

    invoke_tool_calls(tools, messages)  native function calling (routing, `understand`)
    invoke_structured(schema, messages) structured output (the grounded reply, `respond`)

Both raise one of two errors with a message that is safe to show in the UI:

    LLMConfigError  - setup problem (no API key, unknown provider)
    LLMCallError    - the call failed (bad key, unknown model, quota, timeout...)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, SecretStr

from facility_ops.config import get_settings
from facility_ops.logging_config import get_logger

log = get_logger("llm")
T = TypeVar("T", bound=BaseModel)


class LLMConfigError(RuntimeError):
    """The selected provider is not configured (e.g. missing API key)."""


class LLMCallError(RuntimeError):
    """The provider call failed. Message is user-presentable."""


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    s = get_settings()
    if s.llm_provider == "gemini":
        if not s.google_api_key:
            raise LLMConfigError(
                "No Gemini API key found. Add GOOGLE_API_KEY=... (or GEMINI_API_KEY=...) to the .env file "
                "in the project folder and save it. Get a key at https://aistudio.google.com/app/apikey."
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=s.gemini_model,
            google_api_key=s.google_api_key,
            temperature=s.llm_temperature,
            timeout=s.llm_timeout_s,
            max_retries=2,
        )
    if s.llm_provider == "openai":
        if not s.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set. Add it to your .env file (see .env.example).")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=s.openai_model,
            api_key=SecretStr(s.openai_api_key),
            temperature=s.llm_temperature,
            timeout=s.llm_timeout_s,
            max_retries=2,
        )
    raise LLMConfigError(f"Unknown LLM_PROVIDER '{s.llm_provider}'. Use 'gemini' or 'openai'.")


def model_label() -> str:
    s = get_settings()
    return f"{s.llm_provider}:{s.gemini_model if s.llm_provider == 'gemini' else s.openai_model}"


def config_problem() -> str | None:
    """None if the LLM is configured, else a message for the UI banner. No network call."""
    try:
        get_chat_model()
        return None
    except LLMConfigError as exc:
        return str(exc)


def _friendly(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    s = get_settings()
    model = s.gemini_model if s.llm_provider == "gemini" else s.openai_model
    if any(k in text for k in ("api key not valid", "api_key_invalid", "invalid api key", "401", "permission_denied", "403")):
        return "The LLM provider rejected the API key. Check the key in your .env file."
    if "404" in text or "not found" in text and "model" in text:
        return f"The model '{model}' is not available for this API key. Set a different model name in .env."
    if any(k in text for k in ("402", "credits are depleted", "prepayment", "billing")):
        return (
            "The LLM account has run out of prepaid credits, reached its spending cap, or has no billing set up. "
            "Check credits and the spend cap (Gemini: AI Studio > Billing) or put a different API key in .env."
        )
    if "429" in text or "quota" in text or "resource_exhausted" in text or "rate limit" in text:
        return "The LLM provider's rate limit or quota was reached. Wait a minute and try again."
    if "timeout" in text or "timed out" in text or "deadline" in text:
        return "The LLM provider did not respond in time. Please try again."
    if any(k in text for k in ("connection", "network", "resolve", "unreachable")):
        return "Could not reach the LLM provider. Check your internet connection."
    return "The language model call failed. Please try again."


def invoke_structured(schema: type[T], messages: list[BaseMessage]) -> T:
    """Call the chat model and parse its reply into `schema`."""
    model = get_chat_model()  # may raise LLMConfigError
    try:
        result = model.with_structured_output(schema).invoke(messages)
    except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
        log.exception("LLM call failed (%s)", schema.__name__)
        raise LLMCallError(_friendly(exc)) from exc
    if result is None:
        raise LLMCallError("The language model returned an unreadable answer. Please rephrase and try again.")
    if isinstance(result, dict):  # some providers return dicts in JSON mode
        return schema.model_validate(result)
    if not isinstance(result, schema):
        raise LLMCallError("The language model returned an unexpected answer format. Please try again.")
    return result


def invoke_tool_calls(tools: list, messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Call the chat model with `tools` bound (native function calling) and return its tool calls.

    `tool_choice="any"` makes the model answer with at least one tool call; it may
    return several in one reply (parallel function calling).
    """
    model = get_chat_model()  # may raise LLMConfigError
    try:
        reply = model.bind_tools(tools, tool_choice="any").invoke(messages)
    except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
        log.exception("LLM tool-calling request failed")
        raise LLMCallError(_friendly(exc)) from exc
    calls = [{"name": c["name"], "args": c.get("args") or {}} for c in getattr(reply, "tool_calls", None) or []]
    log.info("model tool calls: %s", calls)
    return calls
