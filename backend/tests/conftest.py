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
