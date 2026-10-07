"""ExpenseClassifier: decides the reimbursement category with a confidence and a plain-language reason.

Combines two independent signals:
  * the DocumentInterpreter's own category suggestion (when Claude read the document), and
  * a configurable evidence scorer over document type, merchant, line items and text
    (config/categories.json).
Agreement raises confidence; disagreement lowers it so the employee is asked to confirm.
"""
from __future__ import annotations

from ..settings import load_json_config, load_policy
from .fields import fval


def _evidence_text(expense: dict) -> str:
    parts = [
        fval(expense, "merchant") or "",
        expense.get("documentType") or "",
        " ".join(li.get("description", "") for li in expense.get("lineItems", [])),
        fval(expense, "description") or "",
        expense.get("_text", ""),
    ]
    if fval(expense, "origin") or fval(expense, "destination"):
        parts.append("trip pickup drop")
    if fval(expense, "checkIn") or fval(expense, "checkOut"):
        parts.append("check-in check-out night")
    return " ".join(parts).lower()


def score_categories(expense: dict) -> dict[str, float]:
    signals = load_json_config("categories.json")["signals"]
    text = _evidence_text(expense)
    doc_type = expense.get("documentType")
    scores: dict[str, float] = {}
    for code, sig in signals.items():
        s = 0.0
        if doc_type and doc_type in sig.get("documentTypes", []):
            s += 5
        for kw, w in sig.get("keywords", {}).items():
            if kw in text:
                s += w
        if s > 0:
            scores[code] = s
    return scores


def classify(expense: dict) -> dict:
    """Return {category, confidence, reason} — category is a policy category code."""
    policy = load_policy()
    signals = load_json_config("categories.json")["signals"]
    labels = {k: v["label"] for k, v in policy["categories"].items()}
    scores = score_categories(expense)
    total = sum(scores.values())
    best, best_score = (max(scores.items(), key=lambda kv: kv[1]) if scores else (None, 0.0))
    rule_conf = 0.0
    if best:
        share = best_score / total
        strength = min(best_score / 8.0, 1.0)
        rule_conf = round(0.45 + 0.5 * share * strength, 2)

    suggestion = expense.get("categorySuggestion")
    if suggestion and suggestion.get("category") in labels:
        ai_cat = suggestion["category"]
        ai_conf = float(suggestion.get("confidence") or 0.0)
        reason = suggestion.get("reason") or f"Classified as {labels[ai_cat]}."
        if best == ai_cat:
            conf = min(0.99, max(ai_conf, rule_conf) + 0.05)
        elif best and rule_conf > ai_conf + 0.15:
            return {"category": best, "confidence": round(min(rule_conf, 0.75), 2),
                    "reason": f"Probably {labels[best]} because {signals[best]['reason']}, although the document reader suggested {labels[ai_cat]}."}
        else:
            conf = ai_conf if not best else min(ai_conf, 0.8)
        return {"category": ai_cat, "confidence": round(conf, 2), "reason": reason}

    if not best:
        return {"category": "MISCELLANEOUS", "confidence": 0.3,
                "reason": "No strong signal identifies what kind of expense this is."}
    return {"category": best, "confidence": rule_conf,
            "reason": f"Categorised as {labels.get(best, best)} because {signals[best]['reason']}."}
