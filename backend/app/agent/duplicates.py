"""DuplicateDetector: scores expense pairs within the claim (and against mock history).

Signals: identical file hash, invoice number, amount, date, merchant similarity, perceptual
image hash similarity. The later-uploaded expense of a suspicious pair is flagged; nothing is
removed until the employee confirms.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..settings import load_json_config
from .fields import fval, inr, short_date


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _merchant_sim(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _hash_sim(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    x = int(a, 16) ^ int(b, 16)
    return 1 - bin(x).count("1") / 256


def score_pair(a: dict, b: dict) -> tuple[float, list[str]]:
    if a.get("fileSha256") and a.get("fileSha256") == b.get("fileSha256"):
        return 0.99, ["the same file was uploaded twice"]
    signals, score = [], 0.0
    inv_a, inv_b = _norm(fval(a, "invoiceNumber")), _norm(fval(b, "invoiceNumber"))
    if inv_a and inv_a == inv_b:
        score += 0.4
        signals.append("invoice number")
    ta, tb = fval(a, "total"), fval(b, "total")
    if ta is not None and tb is not None and abs(ta - tb) < 0.01:
        score += 0.25
        signals.append("amount")
    da, db_ = fval(a, "expenseDate"), fval(b, "expenseDate")
    if da and da == db_:
        score += 0.15
        signals.append("date")
    ms = _merchant_sim(fval(a, "merchant"), fval(b, "merchant"))
    if ms > 0.85:
        score += 0.12
        signals.append("merchant")
    hs = _hash_sim(a.get("imageHash"), b.get("imageHash"))
    if hs > 0.9:
        score += 0.1
        signals.append("receipt image")
    if fval(a, "origin") and _norm(fval(a, "origin")) == _norm(fval(b, "origin")) and _norm(fval(a, "destination")) == _norm(fval(b, "destination")):
        score += 0.04
        signals.append("route")
    # Same amount+date+merchant without an invoice number is still suspicious but less certain.
    return min(round(score, 2), 0.98), signals


def detect(active: list[dict], threshold: float) -> dict[str, dict]:
    """Return {expenseId: duplicate info} for flagged (later) expenses."""
    flagged: dict[str, dict] = {}
    ordered = sorted(active, key=lambda e: e["seq"])
    for i, later in enumerate(ordered):
        resolutions = later.get("duplicateResolutions", {})
        best = None
        for earlier in ordered[:i]:
            if resolutions.get(earlier["id"]) == "separate":
                continue
            score, signals = score_pair(earlier, later)
            if score >= threshold and (best is None or score > best["score"]):
                best = {"ofExpenseId": earlier["id"], "score": score, "signals": signals}
        if not best:
            for h in load_json_config("history.json").get("expenses", []):
                key = f"history:{h['claimReference']}:{h['invoiceNumber']}"
                if resolutions.get(key) == "separate":
                    continue
                pseudo = {"fields": {k: {"value": v} for k, v in h.items()}, "seq": -1}
                score, signals = score_pair(pseudo, later)
                if score >= threshold:
                    best = {"historyKey": key, "historyReference": h["claimReference"], "score": score, "signals": signals}
                    break
        if best:
            best["explanation"] = explain(later, best, {e["id"]: e for e in active})
            flagged[later["id"]] = best
    return flagged


def explain(expense: dict, dup: dict, by_id: dict) -> str:
    sig = dup["signals"]
    joined = ", ".join(sig[:-1]) + (" and " if len(sig) > 1 else "") + sig[-1] if sig else "content"
    if "historyReference" in dup:
        return f"This may already have been reimbursed in claim {dup['historyReference']}: {joined} match."
    other = by_id.get(dup["ofExpenseId"])
    desc = f"{fval(other, 'merchant')} {inr(fval(other, 'total'))} — {short_date(fval(other, 'expenseDate'))}" if other else "another expense"
    return f"This may duplicate {desc} because the {joined} match."
