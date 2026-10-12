"""OpenAI document reading: the same schema-constrained extraction as Claude, via the Responses API.

Used only for reading uploaded photos and PDFs (EXTRACTION_MODE=openai, or auto with only an
OPENAI_API_KEY). The output goes through the same normaliser and validation as any other extraction.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

import openai

from ..settings import get_settings
from .llm import LLMError

log = logging.getLogger("expense_agent.openai")


@lru_cache
def get_client() -> openai.OpenAI:
    # The key is read from OPENAI_API_KEY on the server; it never reaches the browser.
    return openai.OpenAI(timeout=get_settings().openai_timeout, max_retries=2)


def structured_call(system: str, content: list[dict], schema: dict, name: str = "receipt_extraction",
                    max_output_tokens: int = 8000) -> dict:
    """One Responses API request whose reply is constrained to `schema` (strict). Returns the parsed object."""
    try:
        response = get_client().responses.create(
            model=get_settings().openai_model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            max_output_tokens=max_output_tokens,
        )
    except openai.APITimeoutError as e:
        raise LLMError("The AI service timed out") from e
    except openai.RateLimitError as e:
        raise LLMError("The AI service is rate limited") from e
    except openai.AuthenticationError as e:
        raise LLMError("AI credentials were rejected") from e
    except openai.BadRequestError as e:
        raise LLMError(f"AI request rejected: {e.message}") from e
    except openai.APIStatusError as e:
        raise LLMError(f"AI service error ({e.status_code})") from e
    except openai.APIConnectionError as e:
        raise LLMError("Could not reach the AI service") from e

    for item in response.output or []:
        if getattr(item, "type", None) == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise LLMError("The AI declined to process this document")
    if getattr(response, "status", None) == "incomplete":
        raise LLMError("The AI response was truncated")
    text = response.output_text
    if not text:
        raise LLMError("The AI returned no structured output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("The AI returned malformed JSON") from e
    if not isinstance(data, dict):
        raise LLMError("The AI returned an unexpected structure")
    return data
