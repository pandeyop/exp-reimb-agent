"""End-to-end check of a running deployment (local, Docker or Render) using only the standard library.

    python scripts/verify_deployment.py https://<your-service>.onrender.com
    python scripts/verify_deployment.py http://127.0.0.1:8000

Runs the 12-receipt demo through the public HTTP API: upload → 8 ready / 2 need info / 1 over policy /
1 duplicate → three answers + duplicate confirmation → READY_TO_SUBMIT → submit → ER reference.
Each run creates (and submits) a new demo claim on the target.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = 90  # a free Render instance may need ~1 minute to wake up


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, status, headers = r.read().decode("utf-8", "replace"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read().decode("utf-8", "replace"), e.code, dict(e.headers)
    try:
        return status, json.loads(raw), headers
    except json.JSONDecodeError:
        return status, raw, headers


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    print(f"Verifying {BASE}\n")
    status, health, _ = call("GET", "/api/health")
    check(status == 200 and health.get("ok") is True, "GET /api/health", json.dumps(health))
    print(f"      AI mode: {health['ai']['mode']} · reference date {health['referenceDate']}")

    status, page, headers = call("GET", "/")
    check(status == 200 and '<div id="root">' in page, "GET / serves the React app")
    asset = re.search(r'src="(/assets/[^"]+\.js)"', page)
    check(bool(asset) and call("GET", asset.group(1))[0] == 200, "built JS bundle is served", asset.group(1) if asset else "")
    status, body, _ = call("GET", "/.env")
    check(status in (200, 404) and "ANTHROPIC" not in str(body), "no secrets reachable at /.env")

    status, snap, _ = call("POST", "/api/claims", {})
    check(status == 201, "create claim", snap["claim"]["id"])
    cid = snap["claim"]["id"]
    status, up, _ = call("POST", f"/api/claims/{cid}/demo-documents")
    check(status == 202 and len(up["accepted"]) == 12, "upload the 12 bundled demo receipts")

    deadline = time.time() + 120
    while time.time() < deadline:
        _, snap, _ = call("GET", f"/api/claims/{cid}")
        if snap["claim"]["dashboard"]["processing"] == 0 and any(m.get("kind") == "summary" for m in snap["messages"]):
            break
        time.sleep(1)
    d = snap["claim"]["dashboard"]
    counts = (d["complete"], d["needsInformation"], d["policyViolations"], d["duplicates"])
    check(counts == (8, 2, 1, 1), "processed: 8 ready / 2 need info / 1 over policy / 1 duplicate", str(counts))
    modes = {e["extractionMode"] for e in snap["expenses"]}
    print(f"      extraction mode(s): {', '.join(sorted(modes))}")

    def ask(kind):
        _, s, _ = call("GET", f"/api/claims/{cid}")
        return next(q for q in s["questions"] if q["kind"] == kind)

    _, r, _ = call("POST", f"/api/claims/{cid}/chat",
                   {"message": "It was a client dinner with Priya Shah and Rahul Mehta from Northwind Retail"})
    check("Client Entertainment" in r["reply"]["text"], "clarification by free text (client dinner)", r["reply"]["text"][:90])
    _, r, _ = call("POST", f"/api/claims/{cid}/chat", {"questionId": ask("fields")["id"], "option": "2026-09-23"})
    check("23 Sep" in r["reply"]["text"], "missing date answered with a quick reply")
    _, r, _ = call("POST", f"/api/claims/{cid}/chat", {"questionId": ask("over_limit")["id"], "option": "cap"})
    check("₹22,500" in r["reply"]["text"], "hotel policy violation capped at the limit")
    _, r, _ = call("POST", f"/api/claims/{cid}/chat", {"questionId": ask("duplicate")["id"], "option": "duplicate"})
    check("duplicate" in r["reply"]["text"].lower(), "duplicate confirmed and excluded")

    _, snap, _ = call("GET", f"/api/claims/{cid}")
    check(snap["claim"]["status"] == "READY_TO_SUBMIT", "claim is READY_TO_SUBMIT", f"total ₹{snap['claim']['totalAmount']:,.0f}")
    status, _, _ = call("POST", f"/api/claims/{cid}/submit", {"confirm": False})
    check(status == 400, "submit without confirmation is refused")
    status, sub, _ = call("POST", f"/api/claims/{cid}/submit", {"confirm": True})
    check(status == 200 and re.fullmatch(r"ER-\d{4}-\d{6}", sub["claimReference"] or ""), "mock submission",
          f"{sub.get('status')} {sub.get('claimReference')}")
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
