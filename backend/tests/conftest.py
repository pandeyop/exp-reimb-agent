import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="expense-agent-tests-")
os.environ.update({
    "DATA_DIR": _tmp,
    "EXTRACTION_MODE": "offline",
    "OFFLINE_PROCESSING_DELAY": "0",
    "REFERENCE_DATE": "2026-10-07",
    "MOCK_FINANCE_FAILURE_RATE": "0",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


def demo_claim(client):
    """A new claim with the 12 demo receipts processed. Returns (claim_id, snapshot)."""
    cid = client.post("/api/claims").json()["claim"]["id"]
    client.post(f"/api/claims/{cid}/demo-documents")
    return cid, wait_until_processed(client, cid)


def expense_by_merchant(snap, prefix):
    return next(e for e in snap["expenses"] if (e["fields"]["merchant"]["value"] or "").startswith(prefix))


def question(snap, kind):
    return next(q for q in snap["questions"] if q["kind"] == kind)


def make_ready(client, cid):
    """Answer the demo's three questions and confirm the duplicate. Returns the READY snapshot."""
    answers = [("meal_purpose", {"option": "client", "message": "Client dinner with Priya Shah from Northwind"}),
               ("fields", {"option": "2026-09-23"}), ("over_limit", {"option": "cap"}), ("duplicate", {"option": "duplicate"})]
    for kind, body in answers:  # skip any the test already answered
        snap = client.get(f"/api/claims/{cid}").json()
        q = next((q for q in snap["questions"] if q["kind"] == kind), None)
        if q:
            client.post(f"/api/claims/{cid}/chat", json={"questionId": q["id"], **body})
    snap = client.get(f"/api/claims/{cid}").json()
    assert snap["claim"]["status"] == "READY_TO_SUBMIT"
    return snap


def wait_until_processed(client, claim_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/claims/{claim_id}").json()
        if snap["claim"]["status"] not in ("PROCESSING",) and snap["claim"]["dashboard"]["processing"] == 0:
            # wait for the batch summary message to land
            if any(m.get("kind") == "summary" for m in snap["messages"]):
                return snap
        time.sleep(0.1)
    raise AssertionError("documents did not finish processing")
