"""Thin wrapper around the Anthropic SDK used by the DocumentInterpreter and the chat agent."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

import anthropic

from ..settings import get_settings

log = logging.getLogger("expense_agent.llm")


class LLMError(RuntimeError):
    """Any failure talking to the model: transport, refusal, truncation or malformed output."""


@lru_cache
def get_client() -> anthropic.Anthropic:
    s = get_settings()
    # Credentials resolve from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
    return anthropic.Anthropic(timeout=s.anthropic_timeout, max_retries=2)


def _request_kwargs(output_config: dict) -> dict:
    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": s.anthropic_model,
        "output_config": {"effort": s.anthropic_effort, **output_config},
    }
    if s.use_refusal_fallback:
        # Server-side fallback: if the primary model declines, the API reroutes within the same call.
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
    return kwargs


def _create(**kwargs):
    client = get_client()
    try:
        if "betas" in kwargs:
            return client.beta.messages.create(**kwargs)
        return client.messages.create(**kwargs)
    except anthropic.APITimeoutError as e:
        raise LLMError("The AI service timed out") from e
    except anthropic.RateLimitError as e:
        raise LLMError("The AI service is rate limited") from e
    except anthropic.AuthenticationError as e:
        raise LLMError("AI credentials were rejected") from e
    except anthropic.BadRequestError as e:
        raise LLMError(f"AI request rejected: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"AI service error ({e.status_code})") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach the AI service") from e


def _check_stop(response) -> None:
    if response.stop_reason == "refusal":
        raise LLMError("The AI declined to process this document")
    if response.stop_reason == "max_tokens":
        raise LLMError("The AI response was truncated")


def structured_call(system: str, content: list[dict], schema: dict, max_tokens: int = 8000) -> dict:
    """One request whose reply is constrained to `schema`. Returns the parsed JSON object."""
    response = _create(
        system=system,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        **_request_kwargs({"format": {"type": "json_schema", "schema": schema}}),
    )
    _check_stop(response)
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise LLMError("The AI returned no structured output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("The AI returned malformed JSON") from e
    if not isinstance(data, dict):
        raise LLMError("The AI returned an unexpected structure")
    return data


def tool_turn(system: str, messages: list[dict], tools: list[dict], max_tokens: int = 4000):
    """One step of a manual tool-use loop. Returns the raw response for the caller to process."""
    response = _create(
        system=system,
        max_tokens=max_tokens,
        messages=messages,
        tools=tools,
        **_request_kwargs({}),
    )
    if response.stop_reason == "refusal":
        raise LLMError("The AI declined to answer")
    return response
