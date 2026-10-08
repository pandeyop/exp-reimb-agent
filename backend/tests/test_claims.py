"""claim_manager.py, duplicates.py, submission.py and mock_finance.py through the API and their functions."""
import time

import pytest
from conftest import demo_claim, expense_by_merchant, make_ready, wait_until_processed

from app import mock_finance
from app.agent import claim_manager as cm
from app.agent.duplicates import detect
from app.agent.fields import FIELD_DEFS, make_field
from app.settings import get_settings

DEMO = get_settings().demo_dir


def snap_of(client, cid):
    return client.get(f"/api/claims/{cid}").json()


# ------------------------------------------------------------------ claim state machine

def test_claim_status_transitions(monkeypatch, client):
    claim = client.post("/api/claims").json()["claim"]
    assert claim["status"] == "DRAFT" and claim["dashboard"]["receipts"] == 0
    cid = claim["id"]
    monkeypatch.setattr(get_settings(), "offline_delay_seconds", 0.6)
    client.post(f"/api/claims/{cid}/documents", files=[("files", ("taxi.pdf", (DEMO / "06_ridenow_trip_receipt.pdf").read_bytes(), "application/pdf"))])
    assert snap_of(client, cid)["claim"]["status"] == "PROCESSING"
    snap = wait_until_processed(client, cid)
    # A lone taxi ride has no trip to inherit a business purpose from, so it needs information
    assert snap["claim"]["status"] == "ACTION_REQUIRED"
    taxi = snap["expenses"][0]
    assert taxi["status"] == "NEEDS_INFORMATION" and taxi["gaps"] == [{"field": "businessPurpose", "state": "MISSING"}]
    client.patch(f"/api/expenses/{taxi['id']}", json={"businessPurpose": "Customer meeting"})
    snap = snap_of(client, cid)
    assert snap["claim"]["status"] == "READY_TO_SUBMIT" and snap["messages"][-1]["kind"] == "ready"
    # Excluding the only expense leaves nothing to submit
    client.delete(f"/api/expenses/{taxi['id']}")
    assert snap_of(client, cid)["claim"]["status"] == "ACTION_REQUIRED"
    client.patch(f"/api/expenses/{taxi['id']}", json={"excluded": False})
    snap = snap_of(client, cid)
    assert snap["claim"]["status"] == "READY_TO_SUBMIT" and not snap["expenses"][0]["excluded"]
    assert sum(1 for m in snap["messages"] if m["kind"] == "ready") == 2  # announced again after returning to ready


def test_replace_and_dismiss_documents(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    files = [("files", ("blank.pdf", b"%PDF-1.4 broken" * 4, "application/pdf")),
             ("files", ("blank2.pdf", b"%PDF-1.4 broken" * 4, "application/pdf"))]
    client.post(f"/api/claims/{cid}/documents", files=files)
    snap = wait_until_processed(client, cid)
    first, second = snap["documents"]
    assert first["status"] == second["status"] == "FAILED"

    r = client.post(f"/api/documents/{first['id']}/replace",
                    files={"file": ("ride.pdf", (DEMO / "06_ridenow_trip_receipt.pdf").read_bytes(), "application/pdf")})
    assert r.status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline and snap_of(client, cid)["documents"][0]["status"] in ("UPLOADED", "PROCESSING"):
        time.sleep(0.05)
    snap = snap_of(client, cid)
    doc = snap["documents"][0]
    assert doc["filename"] == "ride.pdf" and doc["status"] in ("PROCESSED", "NEEDS_ATTENTION") and doc["expenseId"]
    assert expense_by_merchant(snap, "RideNow")["documentId"] == first["id"]

    assert client.delete(f"/api/documents/{second['id']}").status_code == 200
    assert [d["id"] for d in snap_of(client, cid)["documents"]] == [first["id"]]

    assert client.post(f"/api/documents/{first['id']}/replace",
                       files={"file": ("notes.txt", b"hello", "text/plain")}).status_code == 400
    assert client.post("/api/documents/DOC-nope/replace", files={"file": ("a.pdf", b"%PDF", "application/pdf")}).status_code == 404
    assert client.delete("/api/documents/DOC-nope").status_code == 404


def test_upload_limits(monkeypatch, client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    monkeypatch.setattr(get_settings(), "max_files_per_upload", 1)
    pdf = (DEMO / "06_ridenow_trip_receipt.pdf").read_bytes()
    r = client.post(f"/api/claims/{cid}/documents", files=[("files", ("a.pdf", pdf, "application/pdf"))] * 2)
    assert r.status_code == 400 and "at most 1 files" in r.json()["detail"]
    monkeypatch.setattr(get_settings(), "max_files_per_upload", 30)
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    r = client.post(f"/api/claims/{cid}/documents", files=[("files", ("big.pdf", pdf, "application/pdf")),
                                                         ("files", ("empty.pdf", b"", "application/pdf"))])
    assert r.status_code == 202 and r.json()["accepted"] == []
    msg = snap_of(client, cid)["messages"][-1]["text"]
    assert "larger than" in msg and "the file is empty" in msg
    assert cm.sanitise_filename("../../etc/pass<wd>.pdf") == "pass_wd_.pdf"
    assert cm.sanitise_filename("") == "upload"


def test_group_rename_override_and_claim_title(client):
    cid, snap = demo_claim(client)
    trip = snap["claim"]["groups"][0]
    snap = client.patch(f"/api/claims/{cid}", json={"groupNames": {trip["id"]: "Northwind renewal"}}).json()
    assert snap["claim"]["groups"][0]["title"] == "Northwind renewal" and snap["claim"]["title"] == "Northwind renewal"
    snap = client.patch(f"/api/claims/{cid}", json={"groupNames": {trip["id"]: ""}}).json()
    assert snap["claim"]["groups"][0]["title"] == "Bengaluru Customer Visit"

    lunch = expense_by_merchant(snap, "Udupi")
    client.patch(f"/api/expenses/{lunch['id']}", json={"groupId": "custom-team-lunches"})
    snap = snap_of(client, cid)
    assert any(g["id"] == "custom-team-lunches" and g["expenseIds"] == [lunch["id"]] for g in snap["claim"]["groups"])
    client.patch(f"/api/expenses/{lunch['id']}", json={"groupId": None})
    assert expense_by_merchant(snap_of(client, cid), "Udupi")["groupId"] == trip["id"]

    snap = client.patch(f"/api/claims/{cid}", json={"title": "Q3 Northwind visit"}).json()
    assert snap["claim"]["title"] == "Q3 Northwind visit" and snap["claim"]["titleSource"] == "user"
    client.patch(f"/api/claims/{cid}", json={"groupNames": {trip["id"]: "Renamed group"}})
    assert snap_of(client, cid)["claim"]["title"] == "Q3 Northwind visit"  # a user title is never overwritten


def test_edit_validation_errors(client):
    cid, snap = demo_claim(client)
    coffee = expense_by_merchant(snap, "Bean Street")
    assert client.patch(f"/api/expenses/{coffee['id']}", json={"amount": "lots"}).status_code == 400
    assert client.patch(f"/api/expenses/{coffee['id']}", json={"category": "SPACE_TRAVEL"}).status_code == 400
    assert client.patch(f"/api/expenses/{coffee['id']}", json={"colour": "blue"}).status_code == 400
    assert client.patch(f"/api/expenses/{coffee['id']}", json={}).status_code == 400
    assert client.patch("/api/expenses/EXP-nope", json={"amount": 1}).status_code == 404
    assert client.get("/api/claims/CLM-0").status_code == 404
    # Changing the category clears a policy decision made under the old category
    hotel = expense_by_merchant(snap, "The Orchid Grand")
    client.post(f"/api/expenses/{hotel['id']}/policy-override", json={"action": "cap"})
    client.patch(f"/api/expenses/{hotel['id']}", json={"category": "MISCELLANEOUS"})
    assert expense_by_merchant(snap_of(client, cid), "The Orchid Grand")["policyDecision"] is None
    assert client.post(f"/api/expenses/{hotel['id']}/policy-override", json={"action": "shrug"}).status_code == 400


def test_submitted_claim_is_frozen(client):
    cid, _ = demo_claim(client)
    snap = make_ready(client, cid)
    sub = client.post(f"/api/claims/{cid}/submit", json={"confirm": True}).json()
    again = client.post(f"/api/claims/{cid}/submit", json={"confirm": True}).json()
    assert again["claimReference"] == sub["claimReference"]  # idempotent
    exp = snap["expenses"][0]
    pdf = (DEMO / "06_ridenow_trip_receipt.pdf").read_bytes()
    for r in (client.post(f"/api/claims/{cid}/documents", files=[("files", ("a.pdf", pdf, "application/pdf"))]),
              client.patch(f"/api/expenses/{exp['id']}", json={"amount": 5}),
              client.delete(f"/api/expenses/{exp['id']}"),
              client.post(f"/api/expenses/{exp['id']}/policy-override", json={"action": "clear"}),
              client.patch(f"/api/claims/{cid}", json={"title": "x"}),
              client.delete(f"/api/documents/{snap['documents'][0]['id']}")):
        assert r.status_code == 409 and "already been submitted" in r.json()["detail"]


def test_read_endpoints(client):
    cid, snap = demo_claim(client)
    v = client.get(f"/api/claims/{cid}/validation").json()
    assert not v["readyToSubmit"] and len(v["openQuestions"]) == 4 and v["blockingIssues"]
    assert len(client.get(f"/api/claims/{cid}/documents").json()) == 12
    assert len(client.get(f"/api/claims/{cid}/expenses").json()) == 12
    f = client.get(snap["documents"][0]["url"])
    assert f.status_code == 200 and f.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/documents/DOC-nope/file").status_code == 404
    assert client.get("/api/health").json()["ai"]["mode"] == "offline"
    assert "MEALS" in client.get("/api/policy").json()["categories"]
    assert any(c["id"] == cid for c in client.get("/api/claims").json())


# ------------------------------------------------------------------ duplicates

def _exp(eid, seq, **values):
    fields = {name: make_field() for name in FIELD_DEFS}
    for k, v in values.items():
        fields[k] = make_field(v, 0.97, "extracted")
    return {"id": eid, "seq": seq, "fields": fields, "duplicateResolutions": {}}


def test_duplicate_against_reimbursement_history():
    again = _exp("e1", 0, merchant="Konnect Mobile", invoiceNumber="KM-BLR-2608-55102", expenseDate="2026-08-05", total=1179)
    flagged = detect([again], 0.75)["e1"]
    assert flagged["historyReference"] == "ER-2026-000874" and flagged["score"] >= 0.9
    assert "already have been reimbursed in claim ER-2026-000874" in flagged["explanation"]
    again["duplicateResolutions"][flagged["historyKey"]] = "separate"
    assert detect([again], 0.75) == {}
    new_month = _exp("e2", 0, merchant="Konnect Mobile", invoiceNumber="KM-BLR-2609-60417", expenseDate="2026-09-05", total=1179)
    assert detect([new_month], 0.75) == {}  # same amount and merchant alone isn't enough


def test_same_file_twice_and_separate_resolution_survives_revalidation(client):
    a = _exp("a", 0, merchant="Cab", total=100)
    b = _exp("b", 1, merchant="Other", total=999)
    a["fileSha256"] = b["fileSha256"] = "abc"
    assert detect([a, b], 0.75)["b"]["signals"] == ["the same file was uploaded twice"]

    cid, snap = demo_claim(client)
    dup = next(e for e in snap["expenses"] if e["status"] == "POSSIBLE_DUPLICATE")
    client.post(f"/api/expenses/{dup['id']}/duplicate-resolution", json={"resolution": "separate"})
    other = expense_by_merchant(snap, "Udupi")
    client.patch(f"/api/expenses/{other['id']}", json={"description": "Lunch near the office"})  # forces revalidation
    snap = snap_of(client, cid)
    kept = next(e for e in snap["expenses"] if e["id"] == dup["id"])
    assert kept["status"] == "COMPLETE" and not kept["excluded"] and kept["duplicateResolutions"]
    assert client.post(f"/api/expenses/{dup['id']}/duplicate-resolution", json={"resolution": "duplicate"}).status_code == 400
    assert client.post(f"/api/expenses/{dup['id']}/duplicate-resolution", json={"resolution": "maybe"}).status_code == 400


# ------------------------------------------------------------------ submission + mock finance API

def test_finance_api_failure_keeps_claim_ready(monkeypatch, client):
    cid, _ = demo_claim(client)
    make_ready(client, cid)
    monkeypatch.setattr(get_settings(), "mock_finance_failure_rate", 1.0)
    r = client.post(f"/api/claims/{cid}/submit", json={"confirm": True})
    assert r.status_code == 502 and "please try again" in r.json()["detail"]
    snap = snap_of(client, cid)
    assert snap["claim"]["status"] == "READY_TO_SUBMIT" and snap["claim"]["submission"] is None
    assert "didn't accept the claim" in snap["messages"][-1]["text"]
    monkeypatch.setattr(get_settings(), "mock_finance_failure_rate", 0.0)
    assert client.post(f"/api/claims/{cid}/submit", json={"confirm": True}).json()["status"] == "SUBMITTED"


def _payload(total=786, reimbursable=786):
    return {"employeeId": "EMP001", "claimId": "CLM-T", "currency": "INR", "totalAmount": total,
            "expenses": [{"expenseId": "E1", "category": "TAXI", "merchant": "Cab", "expenseDate": "2026-09-21",
                          "amount": 786, "reimbursableAmount": reimbursable, "currency": "INR"}]}


def test_mock_finance_validation(client):
    with pytest.raises(mock_finance.FinanceAPIError, match="does not match"):
        mock_finance.submit_reimbursement(_payload(total=900))
    with pytest.raises(mock_finance.FinanceAPIError, match="validation errors"):
        mock_finance.submit_reimbursement(_payload(reimbursable=-1, total=-1))
    r = client.post("/mock-finance/v1/reimbursements", json=_payload(total=900))
    assert r.status_code == 422
    ok = client.post("/mock-finance/v1/reimbursements", json=_payload()).json()
    rec = client.get(f"/mock-finance/v1/reimbursements/{ok['claimReference']}").json()
    assert rec["status"] == "SUBMITTED" and rec["expenseCount"] == 1 and "payload" not in rec
    assert client.get("/mock-finance/v1/reimbursements/ER-0").status_code == 404


def test_submission_preview_matches_claim(client):
    cid, _ = demo_claim(client)
    snap = make_ready(client, cid)
    preview = client.get(f"/api/claims/{cid}/summary").json()["payloadPreview"]
    assert preview["totalAmount"] == snap["claim"]["totalAmount"] == 41507
    hotel = next(x for x in preview["expenses"] if x["merchant"] == "The Orchid Grand")
    assert hotel["amount"] == 24570 and hotel["reimbursableAmount"] == 22500 and hotel["policyDecision"]["type"] == "cap"
    assert all(x["reimbursableAmount"] >= 0 for x in preview["expenses"])
