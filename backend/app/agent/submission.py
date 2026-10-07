"""SubmissionTool: builds the corporate payload and sends it to the (mock) reimbursement API.

Only ever invoked from the explicit Submit endpoint — the chat agent has no access to it.
"""
from __future__ import annotations

from .. import db, mock_finance
from ..observability import log_event
from . import claim_manager as cm


def build_payload(snap: dict) -> dict:
    c = snap["claim"]
    expenses = []
    for e in snap["expenses"]:
        if e["excluded"]:
            continue
        f = {k: v["value"] for k, v in e["fields"].items() if v["value"] is not None}
        expenses.append({
            "expenseId": e["id"], "documentId": e["documentId"], "category": e["category"]["value"],
            "merchant": f.get("merchant"), "invoiceNumber": f.get("invoiceNumber"), "expenseDate": f.get("expenseDate"),
            "amount": f.get("total"), "reimbursableAmount": e["reimbursableAmount"], "tax": f.get("tax"),
            "currency": f.get("currency"), "gstin": f.get("gstin"), "businessPurpose": f.get("businessPurpose"),
            "attendees": f.get("attendees"), "description": f.get("description"), "costCentre": f.get("costCentre"),
            "paymentMethod": f.get("paymentMethod"), "cardLast4": f.get("cardLast4"),
            "origin": f.get("origin"), "destination": f.get("destination"), "nights": f.get("nights"),
            "group": e.get("groupId"), "status": e["status"],
            "policyNotes": [r["message"] for r in e["policyResults"] if r["severity"] in ("warning", "info")],
            "policyDecision": e.get("policyDecision"),
        })
    return {
        "employeeId": c["employee"]["id"], "claimId": c["id"], "title": c["title"],
        "period": c["period"], "expenses": expenses, "totalAmount": c["totalAmount"], "currency": c["currency"],
        "groups": [{"id": g["id"], "title": g["title"], "total": g["total"]} for g in c["groups"]],
        "warnings": [w["text"] for w in c["warnings"]],
    }


def submit(claim_id: str, confirmed: bool) -> dict:
    if not confirmed:
        raise cm.ClaimError("Submission requires explicit confirmation from the employee.")
    with db.lock():
        snap = cm.revalidate(claim_id, announce=False)
        status = snap["claim"]["status"]
        if status == "SUBMITTED":
            return snap["claim"]["submission"]
        if status != "READY_TO_SUBMIT":
            raise cm.ClaimError("This claim still has blocking issues and can't be submitted yet.", 409)
        payload = build_payload(snap)
        claim = db.get_claim(claim_id)
        claim["status"] = "SUBMITTING"
        db.save_claim(claim)
    try:
        response = mock_finance.submit_reimbursement(payload)
    except mock_finance.FinanceAPIError as e:
        with db.lock():
            claim = db.get_claim(claim_id)
            claim["status"] = "READY_TO_SUBMIT"
            db.save_claim(claim)
        log_event("submission_failed", claim_id, "Finance API rejected or unavailable", error=type(e).__name__)
        db.add_message(claim_id, {"role": "assistant", "kind": "text",
                                  "text": f"The reimbursement system didn't accept the claim ({e}). Nothing was lost — please try Submit again."})
        raise cm.ClaimError(f"Submission failed: {e}. Your claim is unchanged; please try again.", 502)

    with db.lock():
        claim = db.get_claim(claim_id)
        claim["submission"] = {**response, "submittedAt": db.now_iso(), "totalAmount": payload["totalAmount"],
                               "expenseCount": len(payload["expenses"])}
        claim["status"] = claim["lastStatus"] = "SUBMITTED"
        db.save_claim(claim)
    log_event("claim_submitted", claim_id, "Claim submitted", reference=response["claimReference"], count=len(payload["expenses"]))
    db.add_message(claim_id, {"role": "assistant", "kind": "submitted",
                              "text": f"✓ Submitted. Your claim reference is **{response['claimReference']}**."})
    return claim["submission"]

