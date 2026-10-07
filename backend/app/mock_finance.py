"""Mock corporate reimbursement system ("Finance API").

Exposed over HTTP under /mock-finance for inspection, and called in-process by the SubmissionTool
through `submit_reimbursement`, which behaves like a remote client: it validates the payload,
can inject failures (MOCK_FINANCE_FAILURE_RATE) and returns a claim reference.
"""
from __future__ import annotations

import random
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import db
from .settings import get_settings


class FinanceAPIError(Exception):
    pass


class MockExpense(BaseModel):
    expenseId: str
    category: str
    merchant: str
    expenseDate: str
    amount: float = Field(gt=0)
    reimbursableAmount: float = Field(ge=0)
    currency: str


class MockClaim(BaseModel):
    employeeId: str
    claimId: str
    expenses: list[MockExpense] = Field(min_length=1)
    totalAmount: float = Field(gt=0)
    currency: str


router = APIRouter(prefix="/mock-finance/v1", tags=["mock finance API"])


def submit_reimbursement(payload: dict) -> dict:
    try:
        claim = MockClaim.model_validate(payload)
    except ValidationError as e:
        raise FinanceAPIError(f"payload rejected ({e.error_count()} validation errors)") from e
    expected = round(sum(x.reimbursableAmount for x in claim.expenses), 2)
    if abs(expected - claim.totalAmount) > 0.5:
        raise FinanceAPIError("claim total does not match the sum of expenses")
    if random.random() < get_settings().mock_finance_failure_rate:
        raise FinanceAPIError("service temporarily unavailable (simulated)")
    seq = db.next_counter("finance_ref", start=1029)
    reference = f"ER-{datetime.now().year}-{seq:06d}"
    record = {"status": "SUBMITTED", "claimReference": reference, "receivedAt": db.now_iso(),
              "approvalStage": "PENDING_MANAGER_APPROVAL", "payload": payload}
    db.save_submission(reference, claim.claimId, record)
    return {"status": "SUBMITTED", "claimReference": reference, "approvalStage": "PENDING_MANAGER_APPROVAL"}


@router.post("/reimbursements")
def post_reimbursement(payload: dict):
    try:
        return submit_reimbursement(payload)
    except FinanceAPIError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/reimbursements/{reference}")
def get_reimbursement(reference: str):
    rec = db.get_submission(reference)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown claim reference")
    return {k: v for k, v in rec.items() if k != "payload"} | {"expenseCount": len(rec["payload"]["expenses"])}
