"""Offline chat interpreter: amounts and multi-field answers typed as free text."""
import io
import time

import pytest
from conftest import question
from PIL import Image, ImageDraw

from app.agent import chat_agent
from app.agent.chat_agent import NeedsMore, parse_field_answer, parse_money

CTX = {"start": None}


# ------------------------------------------------------------------ parse_money

@pytest.mark.parametrize("text, expected", [
    # dates must not win over the amount
    ("Apollo Pharmacy, 23 Sep, INR, 1234", 1234),
    ("23 Sep, ₹1234", 1234),
    ("23/09/2026 1,234.50", 1234.5),
    ("22 Sep, INR, ₹642", 642),           # the chat placeholder's own format
    ("1234 on 23 Sep", 1234),
    ("Sep 23rd, 2026 — 980", 980),
    ("2026-09-23 Rs. 1,250", 1250),
    ("it was on the 23rd, 455", 455),
    ("paid 642 at 10:42 on Tuesday", 642),
    # plain amounts keep working
    ("total 642", 642),
    ("₹642", 642),
    ("642", 642),
    ("Rs 1,23,456", 123456),
    ("1,850 INR", 1850),
    # quantities and percentages are not money
    ("dinner for 4 people, 1,850", 1850),
    ("18% GST, 590", 590),
])
def test_parse_money_finds_the_amount(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", [
    "23 Sep",                 # only a date
    "no idea",
    "100 or 200",             # two plausible amounts: ask again rather than guess
    "₹100 and ₹200",
    "invoice INV-778",        # a number glued to letters is an identifier
])
def test_parse_money_returns_none_when_unsure(text):
    assert parse_money(text) is None


def test_currency_words_inside_other_words_are_not_markers():
    assert parse_money("the dinner 2 hours, 900") == 900  # "dinner"/"hours" contain "inr"/"rs"


# ------------------------------------------------------------------ parse_field_answer

FIELDS = ["merchant", "expenseDate", "currency", "total", "description"]


def test_multi_field_reply_is_split_into_its_parts():
    got = parse_field_answer("Apollo Pharmacy, 23 Sep, INR, ₹1,234", FIELDS, CTX)
    assert got == {"merchant": "Apollo Pharmacy", "expenseDate": "2026-09-23", "currency": "INR", "total": 1234}


def test_description_only_when_clearly_given():
    got = parse_field_answer("Apollo Pharmacy; 23 Sep; ₹1,234; for first-aid kit refills", FIELDS, CTX)
    assert got["merchant"] == "Apollo Pharmacy" and got["description"] == "First-aid kit refills"
    assert got["currency"] == "INR"  # implied by the ₹ sign
    got = parse_field_answer("City Mart, 642, description: printer paper", FIELDS, CTX)
    assert got == {"merchant": "City Mart", "total": 642, "description": "Printer paper"}


def test_merchant_never_keeps_date_or_money_fragments():
    assert parse_field_answer("Apollo Pharmacy, 23 Sep", FIELDS, CTX) == {"merchant": "Apollo Pharmacy",
                                                                          "expenseDate": "2026-09-23"}
    assert parse_field_answer("Apollo Pharmacy 23 Sep ₹1,234", FIELDS, CTX)["merchant"] == "Apollo Pharmacy"


@pytest.mark.parametrize("echo", ["merchant name", "Merchant", "date", "amount", "total", "currency", "description"])
def test_label_echo_is_not_saved(echo):
    with pytest.raises(NeedsMore, match="e.g."):
        parse_field_answer(echo, FIELDS, CTX)


def test_single_text_field_takes_the_whole_reply():
    assert parse_field_answer("Client workshop in Pune", ["businessPurpose"], CTX) == {"businessPurpose": "Client workshop in Pune"}
    with pytest.raises(NeedsMore):
        parse_field_answer("business purpose", ["businessPurpose"], CTX)


def test_other_currencies():
    got = parse_field_answer("Blue Bottle, 12 Sep, USD, 18.50", FIELDS, CTX)
    assert got["currency"] == "USD" and got["total"] == 18.5 and got["merchant"] == "Blue Bottle"


# ------------------------------------------------------------------ through the API

def _image_only_pdf() -> bytes:
    img = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(img)
    for y in range(120, 1000, 60):  # grey bars standing in for unreadable print; no real data
        d.rectangle((90, y, 810 - (y % 180), y + 18), fill=(150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, "PDF")
    return buf.getvalue()


def test_scanned_pdf_answered_in_one_chat_reply(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    r = client.post(f"/api/claims/{cid}/documents", files=[("files", ("scan.pdf", _image_only_pdf(), "application/pdf"))])
    assert r.status_code == 202
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = client.get(f"/api/claims/{cid}").json()
        if snap["claim"]["dashboard"]["processing"] == 0 and snap["questions"]:
            break
        time.sleep(0.1)
    assert any("scanned image with no text layer" in n for n in snap["expenses"][0]["extractionNotes"])
    q = question(snap, "fields")
    assert {"merchant", "expenseDate", "currency", "total", "description"} <= set(q["fields"])

    reply = client.post(f"/api/claims/{cid}/chat", json={"message": "merchant name", "questionId": q["id"]}).json()["reply"]
    assert "e.g." in reply["text"]
    assert client.get(f"/api/claims/{cid}").json()["expenses"][0]["fields"]["merchant"]["value"] is None

    client.post(f"/api/claims/{cid}/chat", json={"message": "Apollo Pharmacy, 23 Sep, INR, ₹1,234", "questionId": q["id"]})
    f = client.get(f"/api/claims/{cid}").json()["expenses"][0]["fields"]
    assert f["merchant"]["value"] == "Apollo Pharmacy"
    assert f["expenseDate"]["value"] == "2026-09-23"
    assert f["total"]["value"] == 1234 and f["currency"]["value"] == "INR"
    assert f["description"]["value"] is None  # untouched


def test_route_still_prefers_the_fields_question_for_an_amount():
    q = [{"id": "f", "kind": "fields", "fields": ["total"], "expenseIds": ["x"]},
         {"id": "c", "kind": "category", "fields": ["category"], "expenseIds": ["y"]}]
    assert chat_agent.route("23 Sep, ₹1234", q, {"expenses": []})["id"] == "f"


def test_scanned_pdf_gets_a_specific_offline_message():
    from app.agent import document_interpreter as interp
    scanned = interp.inspect_file(_image_only_pdf())
    assert scanned.kind == "pdf" and len(scanned.text.strip()) < 40
    ex = interp.extract(b"%PDF", "unknown", "scan.pdf", scanned)
    assert ex.mode == "none"
    assert ex.raw["notes"] == ["This PDF is a scanned image with no text layer; offline mode can't read it. "
                               "Please enter the key details."]
    image = interp.InspectedFile(kind="image", media_type="image/jpeg")
    assert "offline mode has no OCR for unknown images" in interp.extract(b"x", "unknown", "a.jpg", image).raw["notes"][0]


def test_jfif_upload_is_accepted(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "JPEG")  # .jfif files are JPEGs
    r = client.post(f"/api/claims/{cid}/documents", files=[("files", ("receipt.jfif", buf.getvalue(), "image/jpeg"))])
    assert r.status_code == 202 and r.json()["accepted"][0]["filename"] == "receipt.jfif"
    assert r.json()["accepted"][0]["mediaType"] == "image/jpeg"
