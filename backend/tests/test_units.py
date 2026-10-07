"""Unit tests for the agent's deterministic tools and the Claude code paths (with the API mocked)."""
from types import SimpleNamespace

from conftest import wait_until_processed

from app.agent import chat_agent, document_interpreter as interp
from app.agent.duplicates import score_pair
from app.agent.fields import inr, make_field, mask_card_numbers
from app.agent.normaliser import normalise
from app.agent.policy import evaluate
from app.settings import get_settings, load_policy


def _exp(eid, cat, total, date, seq=0, **fields):
    f = {name: make_field() for name in ("merchant", "expenseDate", "total", "currency", "nights", "invoiceNumber",
                                          "bookingReference", "origin", "destination")}
    f.update({"total": make_field(total, 0.99, "extracted"), "expenseDate": make_field(date, 0.99, "extracted"),
              "currency": make_field("INR", 0.99, "extracted"), "invoiceNumber": make_field("X1", 0.99, "extracted")})
    for k, v in fields.items():
        f[k] = make_field(v, 0.99, "extracted")
    return {"id": eid, "seq": seq, "category": {"value": cat}, "fields": f}


def test_inr_formatting_uses_indian_grouping():
    assert inr(2450000) == "₹24,50,000"
    assert inr(786) == "₹786"
    assert inr(1850.5) == "₹1,850.50"


def test_card_numbers_are_masked():
    assert mask_card_numbers("Paid with 4111 1111 1111 4417") == "Paid with XXXX-XXXX-XXXX-4417"
    raw = {"fields": {"cardLast4": {"value": "4111111111114417", "confidence": 0.9},
                      "description": {"value": "card 4111-1111-1111-4417", "confidence": 0.9}}}
    n = normalise(raw)
    assert n["fields"]["cardLast4"]["value"] == "4417"
    assert "4111" not in n["fields"]["description"]["value"]


def test_normaliser_rejects_garbage_from_the_model():
    raw = {"fields": {"total": {"value": "about a thousand", "confidence": 0.9}, "expenseDate": {"value": "next tuesday", "confidence": 0.9}},
           "multipleTotalsFound": False}
    n = normalise(raw)
    assert n["fields"]["total"]["value"] is None and n["fields"]["expenseDate"]["value"] is None
    assert any("unreadable" in note for note in n["notes"])


def test_multiple_totals_lower_confidence():
    n = normalise({"fields": {"total": {"value": "1200", "confidence": 0.98}}, "multipleTotalsFound": True})
    assert n["fields"]["total"]["confidence"] <= 0.65


def test_policy_daily_meal_limit_and_future_date():
    policy = load_policy()
    a = _exp("a", "MEALS", 900, "2026-09-22", 0)
    b = _exp("b", "MEALS", 800, "2026-09-22", 1)
    fut = _exp("c", "TAXI", 300, "2026-12-01", 2)
    res = evaluate([a, b, fut], policy, get_settings().today())
    over = next(r for r in res["b"] if r["code"] == "OVER_LIMIT")
    assert over["excess"] == 200 and over["blocking"]
    assert any(r["code"] == "PERIOD_SHARED" for r in res["a"])
    assert any(r["code"] == "FUTURE_DATED" and r["blocking"] for r in res["c"])


def test_policy_cap_sets_reimbursable_amount():
    policy = load_policy()
    h = _exp("h", "HOTEL", 24570, "2026-09-24", nights=3)
    h["policyDecision"] = {"type": "cap"}
    res = evaluate([h], policy, get_settings().today())
    assert h["reimbursableAmount"] == 22500
    assert res["h"][0]["code"] == "OVER_LIMIT_CAPPED" and not res["h"][0]["blocking"]


def test_stale_expense_is_a_non_blocking_warning():
    res = evaluate([_exp("s", "TAXI", 300, "2026-06-01")], load_policy(), get_settings().today())
    stale = next(r for r in res["s"] if r["code"] == "STALE")
    assert stale["severity"] == "warning" and not stale["blocking"]


def test_duplicate_scoring():
    a = _exp("a", "TAXI", 786, "2026-09-21", 0, merchant="ZipCab")
    b = _exp("b", "TAXI", 786, "2026-09-21", 1, merchant="ZipCab")
    score, signals = score_pair(a, b)
    assert score >= 0.9 and "invoice number" in signals
    c = _exp("c", "TAXI", 786, "2026-09-22", 2, merchant="RideNow")
    c["fields"]["invoiceNumber"] = make_field("Y9", 0.9, "extracted")
    assert score_pair(a, c)[0] < 0.75


def test_date_parsing_in_trip_context():
    ctx = {"start": "2026-09-21", "end": "2026-09-24"}
    assert chat_agent.parse_date("23 Sep", ctx) == "2026-09-23"
    assert chat_agent.parse_date("Sept 22nd", ctx) == "2026-09-22"
    assert chat_agent.parse_date("on Tuesday", ctx) == "2026-09-22"
    assert chat_agent.parse_date("the 24th", ctx) == "2026-09-24"
    assert chat_agent.parse_date("no idea", ctx) is None


def test_inspect_rejects_non_documents():
    for blob in (b"hello world", b"%PDF-1.4 broken" * 4):
        try:
            interp.inspect_file(blob)
        except interp.DocumentError:
            continue
        raise AssertionError("should have rejected")


def test_claude_extraction_path_is_validated(monkeypatch, client):
    """With AI enabled, the model's structured output is normalised; a failing call falls back offline."""
    s = get_settings()
    monkeypatch.setattr(s, "extraction_mode", "claude")
    fixture = next(iter(interp._load_fixtures().values()))["extraction"]
    calls = {"n": 0}

    def fake(insp, data, filename):
        calls["n"] += 1
        return fixture

    monkeypatch.setattr(interp, "_claude_extract", fake)
    insp = interp.InspectedFile(kind="pdf", media_type="application/pdf")
    ex = interp.extract(b"%PDF", "nohash", "x.pdf", insp)
    assert ex.mode == "claude" and calls["n"] == 1

    def boom(*a):
        raise interp.LLMError("The AI service timed out")

    monkeypatch.setattr(interp, "_claude_extract", boom)
    monkeypatch.setattr(s, "extraction_mode", "auto")
    monkeypatch.setattr(type(s), "has_anthropic_credentials", property(lambda self: True))
    ex = interp.extract(b"%PDF", "nohash", "x.pdf", interp.InspectedFile(kind="pdf", media_type="application/pdf", text=""))
    assert ex.mode == "none" and "AI extraction failed" in ex.notes[0]


def test_llm_chat_turn_applies_tool_calls(monkeypatch, client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    snap = wait_until_processed(client, cid)
    dinner = next(e for e in snap["expenses"] if e["fields"]["merchant"]["value"] == "Hops & Barrels")
    dup = next(e for e in snap["expenses"] if e["status"] == "POSSIBLE_DUPLICATE")

    turns = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", id="t1", name="set_category", input={"expenseId": dinner["id"], "category": "CLIENT_ENTERTAINMENT"}),
            SimpleNamespace(type="tool_use", id="t2", name="set_expense_fields", input={"expenseId": dinner["id"], "updates": [
                {"field": "businessPurpose", "value": "Client dinner"}, {"field": "attendees", "value": "Priya Shah (Northwind)"}]}),
            SimpleNamespace(type="tool_use", id="t3", name="resolve_duplicate", input={"expenseId": dup["id"], "resolution": "duplicate"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done — recorded the client dinner and removed the duplicate.")]),
    ])
    seen = []

    def fake_turn(system, messages, tools, max_tokens=4000):
        seen.append(messages)
        return next(turns)

    monkeypatch.setattr(chat_agent, "tool_turn", fake_turn)
    monkeypatch.setattr(type(get_settings()), "ai_enabled", property(lambda self: True))
    r = client.post(f"/api/claims/{cid}/chat", json={"message": "client dinner with Priya from Northwind, and the second ZipCab is a dup"})
    assert "client dinner" in r.json()["reply"]["text"]
    snap = client.get(f"/api/claims/{cid}").json()
    d2 = next(e for e in snap["expenses"] if e["id"] == dinner["id"])
    assert d2["category"]["value"] == "CLIENT_ENTERTAINMENT" and d2["status"] == "COMPLETE"
    assert next(e for e in snap["expenses"] if e["id"] == dup["id"])["excluded"]
    # tool results were fed back with the refreshed claim state
    assert any(b.get("type") == "tool_result" for b in seen[1][-1]["content"])
