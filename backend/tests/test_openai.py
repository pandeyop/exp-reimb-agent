"""OpenAI document reading: provider selection, request shape, error handling, and the demo safeguard.

The OpenAI client is replaced with fakes; no network calls are made.
"""
import io
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from PIL import Image

from app.agent import chat_agent, openai_vision
from app.agent import document_interpreter as interp
from app.agent.llm import LLMError
from app.settings import get_settings

_REQ = httpx.Request("POST", "https://api.openai.com/v1/responses")
FIXTURE = next(iter(interp._load_fixtures().values()))["extraction"]


class FakeResponses:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _install(monkeypatch, outcome):
    fake = FakeResponses(outcome)
    monkeypatch.setattr(openai_vision, "get_client", lambda: SimpleNamespace(responses=fake))
    return fake


def _resp(text="{}", status="completed", refusal=False):
    part = SimpleNamespace(type="refusal", refusal="no") if refusal else SimpleNamespace(type="output_text", text=text)
    return SimpleNamespace(output=[SimpleNamespace(type="message", content=[part])], status=status,
                           output_text="" if refusal else text)


def _env(monkeypatch, mode, anthropic=False, openai_key=False):
    s = get_settings()
    monkeypatch.setattr(s, "extraction_mode", mode)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    if anthropic:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    if openai_key:
        monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    return s


# ------------------------------------------------------------------ provider selection

@pytest.mark.parametrize("mode, anthropic, openai_key, engine, chat", [
    ("offline", True, True, None, False),
    ("auto", False, False, None, False),
    ("auto", True, False, "claude", True),
    ("auto", False, True, "openai", False),   # OpenAI key only: reading via OpenAI, chat stays rule-based
    ("auto", True, True, "claude", True),     # both keys: Claude preferred
    ("openai", True, True, "openai", False),
    ("claude", False, False, "claude", True),
])
def test_engine_selection(monkeypatch, mode, anthropic, openai_key, engine, chat):
    s = _env(monkeypatch, mode, anthropic, openai_key)
    assert s.extraction_engine == engine and s.ai_enabled is chat
    expected_model = {"claude": s.anthropic_model, "openai": s.openai_model}.get(engine)
    assert s.extraction_model == expected_model


# ------------------------------------------------------------------ openai_vision.structured_call

def test_structured_call_request_shape_and_parse(monkeypatch):
    fake = _install(monkeypatch, _resp(json.dumps({"total": "786.00"})))
    out = openai_vision.structured_call("sys", [{"type": "input_text", "text": "read"}], {"type": "object"})
    assert out == {"total": "786.00"}
    sent = fake.calls[0]
    assert sent["model"] == get_settings().openai_model
    assert sent["input"][0] == {"role": "system", "content": "sys"} and sent["input"][1]["role"] == "user"
    assert sent["text"]["format"] == {"type": "json_schema", "name": "receipt_extraction",
                                      "schema": {"type": "object"}, "strict": True}


@pytest.mark.parametrize("response, expected", [
    (_resp(refusal=True), "declined"),
    (_resp('{"tot', status="incomplete"), "truncated"),
    (_resp(""), "no structured output"),
    (_resp("{oops"), "malformed JSON"),
    (_resp("[1]"), "unexpected structure"),
])
def test_structured_call_rejects_bad_responses(monkeypatch, response, expected):
    _install(monkeypatch, response)
    with pytest.raises(LLMError, match=expected):
        openai_vision.structured_call("sys", [], {"type": "object"})


def _status(cls, code):
    return cls("boom", response=httpx.Response(code, request=_REQ), body=None)


@pytest.mark.parametrize("exc, expected", [
    (openai.APITimeoutError(request=_REQ), "timed out"),
    (_status(openai.RateLimitError, 429), "rate limited"),
    (_status(openai.AuthenticationError, 401), "credentials were rejected"),
    (_status(openai.BadRequestError, 400), "request rejected"),
    (_status(openai.InternalServerError, 500), r"service error \(500\)"),
    (openai.APIConnectionError(request=_REQ), "Could not reach"),
])
def test_sdk_errors_map_to_llm_error(monkeypatch, exc, expected):
    _install(monkeypatch, exc)
    with pytest.raises(LLMError, match=expected):
        openai_vision.structured_call("sys", [], {"type": "object"})


def test_client_reads_key_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    openai_vision.get_client.cache_clear()
    client = openai_vision.get_client()
    assert client.api_key == "test-not-a-real-key" and client.max_retries == 2
    openai_vision.get_client.cache_clear()


# ------------------------------------------------------------------ document_interpreter with OpenAI

def _photo_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (500, 700), "white").save(buf, "JPEG")
    return buf.getvalue()


def test_openai_extract_sends_image_and_pdf_blocks(monkeypatch):
    fake = _install(monkeypatch, _resp(json.dumps(FIXTURE)))
    photo = interp.inspect_file(_photo_bytes())
    assert interp._openai_extract(photo, b"", "receipt.jpg") == FIXTURE
    image_block = fake.calls[-1]["input"][1]["content"][0]
    assert image_block["type"] == "input_image" and image_block["image_url"].startswith("data:image/jpeg;base64,")
    pdf = interp.InspectedFile(kind="pdf", media_type="application/pdf")
    interp._openai_extract(pdf, b"%PDF-1.4", "bill.pdf")
    file_block = fake.calls[-1]["input"][1]["content"][0]
    assert file_block["type"] == "input_file" and file_block["filename"] == "bill.pdf"
    assert file_block["file_data"].startswith("data:application/pdf;base64,")
    assert fake.calls[-1]["text"]["format"]["schema"] == interp.extraction_schema()


def test_extract_uses_openai_and_falls_back(monkeypatch):
    _env(monkeypatch, "auto", openai_key=True)
    _install(monkeypatch, _resp(json.dumps(FIXTURE)))
    photo = interp.inspect_file(_photo_bytes())
    assert interp.extract(b"x", "not-a-demo-file", "r.jpg", photo).mode == "openai"

    _install(monkeypatch, _status(openai.InternalServerError, 500))
    ex = interp.extract(b"x", "not-a-demo-file", "r.jpg", photo)
    assert ex.mode == "none" and "AI extraction failed" in ex.notes[0]
    assert "offline mode has no OCR" not in ex.raw["notes"][0]  # an AI is configured, it just failed

    _env(monkeypatch, "openai", openai_key=True)
    ex = interp.extract(b"x", "not-a-demo-file", "r.jpg", photo)
    assert ex.mode == "none" and ex.raw["notes"] == ex.notes


def test_demo_receipts_never_call_the_ai(monkeypatch):
    """Safeguard: the bundled receipts replay their recordings even when an AI is enabled."""
    _env(monkeypatch, "openai", openai_key=True)
    fake = _install(monkeypatch, _resp(json.dumps(FIXTURE)))
    sha, doc = next(iter(interp._load_fixtures().items()))
    ex = interp.extract(b"irrelevant", sha, doc["filename"], interp.InspectedFile(kind="image", media_type="image/jpeg"))
    assert ex.mode == "replay" and fake.calls == []


# ------------------------------------------------------------------ through the API

def test_uploaded_photo_read_by_openai_end_to_end(monkeypatch, client):
    _env(monkeypatch, "auto", openai_key=True)
    reading = json.loads(json.dumps(FIXTURE))  # a copy, used as the "model output" for an unknown photo
    reading["fields"]["merchant"] = {"value": "City Medicals", "confidence": 0.95}
    fake = _install(monkeypatch, _resp(json.dumps(reading)))
    health = client.get("/api/health").json()
    assert health["ai"] == {"mode": "openai", "model": get_settings().openai_model, "chat": "rules"}

    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/documents", files=[("files", ("photo.jpg", _photo_bytes(), "image/jpeg"))])
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = client.get(f"/api/claims/{cid}").json()
        if snap["expenses"] and snap["claim"]["dashboard"]["processing"] == 0:
            break
        time.sleep(0.1)
    exp = snap["expenses"][0]
    assert exp["extractionMode"] == "openai" and exp["fields"]["merchant"]["value"] == "City Medicals"
    assert len(fake.calls) == 1 and snap["ai"]["mode"] == "openai"

    # Free-text chat stays on the deterministic interpreter: Claude is never called with only an OpenAI key.
    monkeypatch.setattr(chat_agent, "llm_turn", lambda *a, **kw: pytest.fail("Claude chat must not run"))
    r = client.post(f"/api/claims/{cid}/chat", json={"message": "status"})
    assert r.status_code == 200
