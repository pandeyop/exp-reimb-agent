"""End-to-end demo scenario through the public API (covers AC-01 … AC-18 in offline mode)."""
from conftest import wait_until_processed


def _by_merchant(snap, name):
    return [e for e in snap["expenses"] if (e["fields"]["merchant"]["value"] or "").startswith(name)]


def _q(snap, kind):
    return next(q for q in snap["questions"] if q["kind"] == kind)


def test_full_demo_journey(client):
    claim = client.post("/api/claims").json()["claim"]
    cid = claim["id"]
    assert claim["status"] == "DRAFT"

    # AC-01: twelve documents uploaded together are all processed automatically
    r = client.post(f"/api/claims/{cid}/demo-documents")
    assert r.status_code == 202 and len(r.json()["accepted"]) == 12
    snap = wait_until_processed(client, cid)
    d = snap["claim"]["dashboard"]
    assert d["receipts"] == 12 and d["processed"] == 12 and d["failed"] == 0

    # AC-02/03: extraction + categorisation
    hotel = _by_merchant(snap, "The Orchid Grand")[0]
    assert hotel["category"]["value"] == "HOTEL" and hotel["fields"]["total"]["value"] == 24570
    assert hotel["fields"]["nights"]["value"] == 3
    air = _by_merchant(snap, "AirVista")[0]
    assert air["category"]["value"] == "AIR_TRAVEL"

    # The headline counts from the spec's demo: 8 ready, 2 need info, 1 over policy, 1 duplicate
    assert (d["complete"], d["needsInformation"], d["policyViolations"], d["duplicates"]) == (8, 2, 1, 1)
    assert snap["claim"]["status"] == "ACTION_REQUIRED"  # AC-13
    summary = next(m for m in snap["messages"] if m["kind"] == "summary")
    assert "I only need 3 answers" in summary["text"]

    # Purpose inferred from the flight booking for trip expenses, but not for the 4-cover dinner
    coffee = _by_merchant(snap, "Bean Street")[0]
    assert coffee["fields"]["businessPurpose"]["source"] == "inferred"
    dinner = _by_merchant(snap, "Hops")[0]
    assert dinner["status"] == "NEEDS_INFORMATION"
    assert {g["field"] for g in dinner["gaps"]} == {"businessPurpose"}  # AC-04
    kinds = sorted(q["kind"] for q in snap["questions"])
    assert kinds == ["duplicate", "fields", "meal_purpose", "over_limit"]  # AC-05

    # AC-06: answer through chat (free text, routed to the right expense)
    client.post(f"/api/claims/{cid}/chat", json={
        "message": "It was a client dinner with Priya Shah and Rahul Mehta from Northwind Retail"})
    snap = client.get(f"/api/claims/{cid}").json()
    dinner = _by_merchant(snap, "Hops")[0]
    assert dinner["category"]["value"] == "CLIENT_ENTERTAINMENT"
    assert "Priya Shah" in dinner["fields"]["attendees"]["value"]
    assert dinner["status"] == "COMPLETE"

    # Missing date on the parking slip — answer with a weekday inside the trip
    q = _q(snap, "fields")
    client.post(f"/api/claims/{cid}/chat", json={"message": "It was Wednesday", "questionId": q["id"]})
    snap = client.get(f"/api/claims/{cid}").json()
    parking = _by_merchant(snap, "Tech Park")[0]
    assert parking["fields"]["expenseDate"]["value"] == "2026-09-23"

    # AC-07/08: hotel over limit explained in plain language, then capped via chat quick reply
    hotel = _by_merchant(snap, "The Orchid Grand")[0]
    over = next(r for r in hotel["policyResults"] if r["code"] == "OVER_LIMIT")
    assert over["excess"] == 2070 and "Policy limit ₹22,500" in over["message"]
    q = _q(snap, "over_limit")
    client.post(f"/api/claims/{cid}/chat", json={"questionId": q["id"], "option": "cap"})

    # AC-09/10: duplicate detected and resolved by the employee
    snap = client.get(f"/api/claims/{cid}").json()
    dup = next(e for e in snap["expenses"] if e["status"] == "POSSIBLE_DUPLICATE")
    assert dup["duplicate"]["score"] >= 0.9 and "invoice number" in dup["duplicate"]["explanation"]
    total_before = snap["claim"]["totalAmount"]
    client.post(f"/api/expenses/{dup['id']}/duplicate-resolution", json={"resolution": "duplicate"})

    # AC-14: claim becomes READY_TO_SUBMIT automatically
    snap = client.get(f"/api/claims/{cid}").json()
    assert snap["claim"]["status"] == "READY_TO_SUBMIT"
    assert any(m["kind"] == "ready" for m in snap["messages"])
    assert snap["claim"]["totalAmount"] == round(total_before - 786, 2)  # AC-12

    # AC-11/12: a manual correction revalidates and keeps the total accurate
    coffee = _by_merchant(snap, "Bean Street")[0]
    r = client.patch(f"/api/expenses/{coffee['id']}", json={"amount": 440})
    assert r.status_code == 200
    snap2 = client.get(f"/api/claims/{cid}").json()
    assert snap2["claim"]["totalAmount"] == snap["claim"]["totalAmount"] + 20
    assert snap2["claim"]["status"] == "READY_TO_SUBMIT"

    # AC-15: review summary
    summary = client.get(f"/api/claims/{cid}/summary").json()
    assert summary["payloadPreview"]["totalAmount"] == snap2["claim"]["totalAmount"]
    assert len(summary["payloadPreview"]["expenses"]) == 11

    # AC-16/17: explicit action required; mock API returns a reference
    assert client.post(f"/api/claims/{cid}/submit", json={"confirm": False}).status_code == 400
    sub = client.post(f"/api/claims/{cid}/submit", json={"confirm": True}).json()
    assert sub["status"] == "SUBMITTED" and sub["claimReference"].startswith("ER-")
    snap = client.get(f"/api/claims/{cid}").json()
    assert snap["claim"]["status"] == "SUBMITTED"
    m = snap["claim"]["metrics"]
    assert m["questionsAsked"] == 3 and m["manualEntryAvoidedPct"] >= 90
    # Submitted claims are frozen
    assert client.patch(f"/api/expenses/{coffee['id']}", json={"amount": 1}).status_code == 409


def test_blocking_until_resolved(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    wait_until_processed(client, cid)
    assert client.post(f"/api/claims/{cid}/submit", json={"confirm": True}).status_code == 409


def test_team_meal_triggers_policy_violation_and_personal_excludes(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    snap = wait_until_processed(client, cid)
    q = _q(snap, "meal_purpose")
    client.post(f"/api/claims/{cid}/chat", json={"questionId": q["id"], "option": "team"})
    snap = client.get(f"/api/claims/{cid}").json()
    dinner = _by_merchant(snap, "Hops")[0]
    assert dinner["status"] == "POLICY_VIOLATION"
    assert any(r["code"] == "OVER_LIMIT" and r["excess"] == 350 for r in dinner["policyResults"])
    # A justification turns the violation into a non-blocking warning
    client.post(f"/api/expenses/{dinner['id']}/policy-override", json={"action": "justify", "justification": "Team celebration after signing"})
    snap = client.get(f"/api/claims/{cid}").json()
    dinner = _by_merchant(snap, "Hops")[0]
    assert dinner["status"] == "WARNING"

    # The "personal" answer on a fresh claim excludes the bill and drops it from the total
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    snap = wait_until_processed(client, cid)
    total_before = snap["claim"]["totalAmount"]
    q = _q(snap, "meal_purpose")
    reply = client.post(f"/api/claims/{cid}/chat", json={"questionId": q["id"], "option": "personal"}).json()["reply"]
    assert "aren't reimbursable" in reply["text"]
    snap = client.get(f"/api/claims/{cid}").json()
    dinner = _by_merchant(snap, "Hops")[0]
    assert dinner["excluded"] and dinner["status"] == "EXCLUDED"
    assert snap["claim"]["totalAmount"] == total_before - 1850
    assert all(q["kind"] != "meal_purpose" for q in snap["questions"])


def test_client_meal_reply_states_the_real_limit(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    snap = wait_until_processed(client, cid)
    q = _q(snap, "meal_purpose")
    reply = client.post(f"/api/claims/{cid}/chat", json={
        "questionId": q["id"], "message": "Client dinner with Priya Shah from Northwind", "option": "client"}).json()["reply"]
    assert "That's within the ₹5,000 per-event limit." in reply["text"]


def test_rejects_bad_uploads(client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    files = [("files", ("notes.txt", b"hello", "text/plain")),
             ("files", ("broken.pdf", b"%PDF-1.4 garbage" * 3, "application/pdf"))]
    r = client.post(f"/api/claims/{cid}/documents", files=files)
    assert r.status_code == 202 and len(r.json()["accepted"]) == 1
    snap = wait_until_processed(client, cid)
    doc = snap["documents"][0]
    assert doc["status"] == "FAILED" and "corrupted" in doc["error"]
    assert any("couldn't accept" in m["text"] for m in snap["messages"])
