"""Unit tests for the agent's deterministic tools and the Claude code paths (with the API mocked)."""
from types import SimpleNamespace

import pytest

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
    # ₹800 fits under the ₹1,500 limit; adding the ₹900 crosses it, so ₹900 carries the ₹200 excess.
    over = next(r for r in res["a"] if r["code"] == "OVER_LIMIT")
    assert over["excess"] == 200 and over["blocking"] and over["cappedAmount"] == 700
    assert any(r["code"] == "PERIOD_SHARED" for r in res["b"])
    assert any(r["code"] == "FUTURE_DATED" and r["blocking"] for r in res["c"])


def test_period_excess_goes_to_the_expense_that_crosses_the_limit():
    """Regression: a ₹2,000 dinner then a ₹100 coffee used to charge the ₹600 excess to the coffee."""
    policy = load_policy()
    dinner = _exp("dinner", "MEALS", 2000, "2026-09-22", 0, merchant="Dinner place")
    coffee = _exp("coffee", "MEALS", 100, "2026-09-22", 1, merchant="Coffee place")
    res = evaluate([dinner, coffee], policy, get_settings().today())
    over = next(r for r in res["dinner"] if r["code"] == "OVER_LIMIT")
    assert over["excess"] == 600 and over["cappedAmount"] == 1400 and over["limit"] == 1500
    assert "Excess ₹600" in over["message"] and "Policy limit ₹1,500" in over["message"]
    assert [r["code"] for r in res["coffee"]] == ["PERIOD_SHARED"]

    # Capping reduces the dinner to ₹1,400 so the day totals exactly the limit.
    dinner["policyDecision"] = {"type": "cap"}
    evaluate([dinner, coffee], policy, get_settings().today())
    assert dinner["reimbursableAmount"] == 1400 and coffee["reimbursableAmount"] == 100

    # Even a decision recorded against the small expense can never make it negative.
    coffee["policyDecision"] = {"type": "cap"}
    dinner["policyDecision"] = None
    evaluate([dinner, coffee], policy, get_settings().today())
    assert coffee["reimbursableAmount"] == 100 and dinner["reimbursableAmount"] == 2000


def test_capped_period_payload_is_accepted_by_mock_finance():
    from app import mock_finance
    policy = load_policy()
    dinner = _exp("dinner", "MEALS", 2000, "2026-09-22", 0, merchant="Dinner place")
    coffee = _exp("coffee", "MEALS", 100, "2026-09-22", 1, merchant="Coffee place")
    dinner["policyDecision"] = {"type": "cap"}
    evaluate([dinner, coffee], policy, get_settings().today())
    lines = [{"expenseId": x["id"], "category": "MEALS", "merchant": x["fields"]["merchant"]["value"],
              "expenseDate": "2026-09-22", "amount": x["fields"]["total"]["value"],
              "reimbursableAmount": x["reimbursableAmount"], "currency": "INR"} for x in (dinner, coffee)]
    assert min(x["reimbursableAmount"] for x in lines) >= 0
    resp = mock_finance.submit_reimbursement({"employeeId": "EMP001", "claimId": "CLM-TEST", "expenses": lines,
                                              "totalAmount": 1500, "currency": "INR"})
    assert resp["status"] == "SUBMITTED" and resp["claimReference"].startswith("ER-")


def test_period_excess_split_across_three_expenses():
    """₹900 + ₹1,000 + ₹1,100 against ₹1,500/day: ₹1,500 excess, split ₹400 / ₹1,100 by who crosses the limit."""
    from app.agent.policy import period_excess_shares
    policy = load_policy()
    items = [_exp("x900", "MEALS", 900, "2026-09-23", 2), _exp("x1000", "MEALS", 1000, "2026-09-23", 0),
             _exp("x1100", "MEALS", 1100, "2026-09-23", 1)]
    shares = period_excess_shares(items, 1500)
    assert shares == {"x1000": 400, "x1100": 1100}
    assert sum(shares.values()) == 3000 - 1500
    assert all(shares[i["id"]] <= i["fields"]["total"]["value"] for i in items if i["id"] in shares)

    res = evaluate(items, policy, get_settings().today())
    assert [r["code"] for r in res["x900"]] == ["PERIOD_SHARED"]
    assert next(r for r in res["x1000"] if r["code"] == "OVER_LIMIT")["excess"] == 400
    assert next(r for r in res["x1100"] if r["code"] == "OVER_LIMIT")["cappedAmount"] == 0
    for x in items:
        x["policyDecision"] = {"type": "cap"}
    evaluate(items, policy, get_settings().today())
    assert sum(x["reimbursableAmount"] for x in items) == 1500
    assert min(x["reimbursableAmount"] for x in items) == 0


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


def test_cap_wording_uses_the_expense_share():
    from app.agent.planner import _cap_label
    policy = load_policy()
    dinner = _exp("dinner", "MEALS", 2000, "2026-09-22", 0, merchant="Dinner place")
    coffee = _exp("coffee", "MEALS", 100, "2026-09-22", 1, merchant="Coffee place")
    dinner["policyResults"] = evaluate([dinner, coffee], policy, get_settings().today())["dinner"]
    r = dinner["policyResults"][0]
    assert _cap_label(r) == "Cap to stay within the limit (claim ₹1,400)"
    assert chat_agent.cap_reply(dinner) == ("Done — I'll claim Dinner place at ₹1,400 so the total stays within "
                                            "the ₹1,500 limit; the excess of ₹600 won't be reimbursed.")
    hotel = _exp("h", "HOTEL", 24570, "2026-09-24", nights=3, merchant="Hotel")
    hotel["policyResults"] = evaluate([hotel], policy, get_settings().today())["h"]
    assert _cap_label(hotel["policyResults"][0]) == "Cap at the limit (₹22,500)"
    assert "up to the policy limit (₹22,500); the excess of ₹2,070" in chat_agent.cap_reply(hotel)


def test_total_plausibility_check():
    def raw(**vals):
        return {"fields": {k: {"value": str(v), "confidence": 0.97} for k, v in vals.items()}}

    ok = normalise(raw(subtotal=1760, tax=88, total=1850))  # ₹2 round-off is within tolerance
    assert ok["fields"]["total"]["confidence"] == 0.97 and not ok["notes"]
    with_extras = normalise(raw(subtotal=1000, tax=50, serviceCharge=100, tip=50, total=1200))
    assert with_extras["fields"]["total"]["confidence"] == 0.97

    bad = normalise(raw(subtotal=1000, tax=50, total=1500))
    assert bad["fields"]["total"]["confidence"] == 0.65
    assert "doesn't match the total" in bad["fields"]["total"]["reason"]
    assert any("don't add up" in n for n in bad["notes"])

    partial = normalise(raw(tax=50, total=1500))  # subtotal unknown: nothing to check
    assert partial["fields"]["total"]["confidence"] == 0.97


def test_demo_fixtures_pass_the_plausibility_check():
    for doc in interp._load_fixtures().values():
        n = normalise(doc["extraction"])
        assert not any("don't add up" in note for note in n["notes"]), doc["filename"]


def test_category_enum_comes_from_policy():
    schema = interp.extraction_schema()
    enum = schema["properties"]["categorySuggestion"]["properties"]["category"]["enum"]
    assert enum == list(load_policy()["categories"].keys())


def test_offline_pdf_text_parser():
    text = ("Sharma Office Mart\nGSTIN 29AAEPC6634H1Z1\nInvoice No: SOM-2291\nInvoice Date: 12-09-2026\n"
            "Pens and paper\nGST @ 18%: 90.00\nGrand Total: Rs. 590.00\n")
    out = interp._pdf_text_extract(text)
    f = out["fields"]
    assert f["merchant"]["value"] == "Sharma Office Mart" and f["invoiceNumber"]["value"] == "SOM-2291"
    assert f["expenseDate"]["value"] == "2026-09-12" and f["total"]["value"] == "590.00"
    assert f["gstin"]["value"] == "29AAEPC6634H1Z1" and f["currency"]["value"] == "INR"
    assert f["total"]["confidence"] == 0.8 and not out["multipleTotalsFound"]
    two = interp._pdf_text_extract(text + "Total: 500.00\n")
    assert two["multipleTotalsFound"] and two["fields"]["total"]["confidence"] == 0.6
    assert interp._pdf_text_extract("too short") is None


def test_offline_extract_paths_for_unknown_files():
    pdf = interp.InspectedFile(kind="pdf", media_type="application/pdf",
                               text="Some Vendor Pvt Ltd\nInvoice No: X-77\nDate: 2026-09-10\nTotal: 1,200.00 INR\n")
    assert interp.extract(b"%PDF", "unknown", "x.pdf", pdf).mode == "pdf-text"
    img = interp.InspectedFile(kind="image", media_type="image/jpeg")
    ex = interp.extract(b"\xff\xd8\xff", "unknown", "x.jpg", img)
    assert ex.mode == "none" and "offline mode has no OCR" in ex.raw["notes"][0]


def test_inspect_images():
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "PNG")
    insp = interp.inspect_file(buf.getvalue())
    assert insp.kind == "image" and insp.media_type == "image/jpeg" and len(insp.ahash) == 64
    tiny = _io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(tiny, "PNG")
    with pytest.raises(interp.DocumentError, match="too small"):
        interp.inspect_file(tiny.getvalue())
    with pytest.raises(interp.DocumentError, match="couldn't be read"):
        interp.inspect_file(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
