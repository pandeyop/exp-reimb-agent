"""llm.py: the Anthropic client is replaced with a fake; no network calls are made."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.agent import llm
from app.settings import get_settings

_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, code):
    return cls("boom", response=httpx2.Response(code, request=_REQ), body=None)


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _install(monkeypatch, outcome):
    msgs = FakeMessages(outcome)
    client = SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return msgs


def _response(text=None, stop="end_turn", blocks=None):
    content = blocks if blocks is not None else ([SimpleNamespace(type="text", text=text)] if text is not None else [])
    return SimpleNamespace(stop_reason=stop, content=content)


@pytest.mark.parametrize("exc, expected", [
    (anthropic.APITimeoutError(request=_REQ), "timed out"),
    (_status_error(anthropic.RateLimitError, 429), "rate limited"),
    (_status_error(anthropic.AuthenticationError, 401), "credentials were rejected"),
    (_status_error(anthropic.BadRequestError, 400), "request rejected"),
    (_status_error(anthropic.InternalServerError, 500), r"service error \(500\)"),
    (anthropic.APIConnectionError(request=_REQ), "Could not reach"),
])
def test_sdk_exceptions_map_to_llm_error(monkeypatch, exc, expected):
    _install(monkeypatch, exc)
    with pytest.raises(llm.LLMError, match=expected):
        llm.structured_call("sys", [{"type": "text", "text": "hi"}], {"type": "object"})


def test_structured_call_returns_parsed_json_and_sends_schema(monkeypatch):
    msgs = _install(monkeypatch, _response(json.dumps({"total": "786.00"})))
    out = llm.structured_call("sys", [{"type": "text", "text": "read this"}], {"type": "object"}, max_tokens=123)
    assert out == {"total": "786.00"}
    sent = msgs.calls[0]
    assert sent["model"] == get_settings().anthropic_model and sent["max_tokens"] == 123
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert sent["output_config"]["effort"] == get_settings().anthropic_effort


def test_refusal_fallback_flag_controls_beta_endpoint(monkeypatch):
    s = get_settings()
    msgs = _install(monkeypatch, _response("{}"))
    monkeypatch.setattr(s, "use_refusal_fallback", True)
    llm.structured_call("sys", [], {"type": "object"})
    assert msgs.calls[-1]["fallbacks"] == "default" and msgs.calls[-1]["betas"] == ["server-side-fallback-2026-07-01"]
    monkeypatch.setattr(s, "use_refusal_fallback", False)
    llm.structured_call("sys", [], {"type": "object"})
    assert "betas" not in msgs.calls[-1] and "fallbacks" not in msgs.calls[-1]


@pytest.mark.parametrize("response, expected", [
    (_response("{}", stop="refusal"), "declined"),
    (_response('{"tot', stop="max_tokens"), "truncated"),
    (_response(None), "no structured output"),
    (_response("{not json"), "malformed JSON"),
    (_response("[1, 2]"), "unexpected structure"),
])
def test_structured_call_rejects_bad_responses(monkeypatch, response, expected):
    _install(monkeypatch, response)
    with pytest.raises(llm.LLMError, match=expected):
        llm.structured_call("sys", [], {"type": "object"})


def test_tool_turn_passes_tools_and_surfaces_refusal(monkeypatch):
    msgs = _install(monkeypatch, _response("ok"))
    tools = [{"name": "t", "input_schema": {"type": "object"}}]
    assert llm.tool_turn("sys", [{"role": "user", "content": "x"}], tools).stop_reason == "end_turn"
    assert msgs.calls[0]["tools"] == tools
    _install(monkeypatch, _response("no", stop="refusal"))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.tool_turn("sys", [], tools)


def test_get_client_uses_configured_timeout():
    llm.get_client.cache_clear()
    client = llm.get_client()
    assert client.timeout == get_settings().anthropic_timeout and client.max_retries == 2
    llm.get_client.cache_clear()
