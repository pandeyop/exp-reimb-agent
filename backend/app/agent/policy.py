"""PolicyEvaluator: checks structured expenses against the configurable policy (config/policy.json).

Produces plain-language results. Each result has a severity:
  violation  — blocks submission until fixed, capped or justified
  warning    — shown to the employee and approver, does not block
  info       — explanation only
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .fields import fval, inr, nice_date


def _result(code, severity, title, message, blocking=None, **extra) -> dict:
    return {
        "code": code,
        "severity": severity,
        "blocking": severity == "violation" if blocking is None else blocking,
        "title": title,
        "message": message,
        **extra,
    }


def _period_key(per: str, e: dict) -> str | None:
    d = fval(e, "expenseDate")
    if per == "day":
        return d
    if per == "month":
        return d[:7] if d else None
    return None


def evaluate(active: list[dict], policy: dict, today: date) -> dict[str, list[dict]]:
    """Return {expenseId: [results]} and set expense['reimbursableAmount']."""
    results: dict[str, list[dict]] = defaultdict(list)
    general = policy["general"]
    cats = policy["categories"]
    base_currency = policy.get("currency", "INR")

    # ---- per-expense rules
    for e in active:
        eid = e["id"]
        total = fval(e, "total")
        currency = fval(e, "currency") or base_currency
        e["reimbursableAmount"] = total if (total or 0) > 0 else 0
        exp_date = fval(e, "expenseDate")
        cat = e["category"]["value"]
        rule = cats.get(cat, {})

        if total is not None and total <= 0 and general.get("nonPositiveInvalid", True):
            results[eid].append(_result("NON_POSITIVE", "violation", "Invalid amount",
                                        f"The amount is {inr(total, currency)}. Expenses must be greater than zero."))
        if exp_date:
            d = date.fromisoformat(exp_date)
            if d > today and general.get("futureDatedInvalid", True):
                results[eid].append(_result("FUTURE_DATED", "violation", "Future-dated expense",
                                            f"This expense is dated {nice_date(exp_date)}, which is after today. Please correct the date."))
            age = (today - d).days
            stale = general.get("staleAfterDays")
            if stale and age > stale:
                results[eid].append(_result("STALE", "warning", "Older than policy window",
                                            f"This expense is {age} days old; policy asks for claims within {stale} days. It can still be submitted but may need approver sign-off."))
        if currency != base_currency:
            results[eid].append(_result("FOREIGN_CURRENCY", "warning", "Foreign currency",
                                        f"This expense is in {currency}. Finance will convert it at the reimbursement-date rate."))
        if rule.get("invoiceRequired") and not fval(e, "invoiceNumber") and not fval(e, "bookingReference"):
            results[eid].append(_result("INVOICE_REQUIRED", "warning", "Tax invoice not identified",
                                        f"{rule.get('label', cat)} expenses need a tax invoice, but I couldn't find an invoice or booking number on this document."))
        missing_rec = [f for f in rule.get("recommendedFields", []) if not fval(e, f)]
        if missing_rec:
            names = " and ".join(f.replace("origin", "pickup").replace("destination", "drop location") for f in missing_rec)
            results[eid].append(_result("RECOMMENDED_MISSING", "warning", "Trip details incomplete",
                                        f"Policy recommends recording the {names} for {rule.get('label', cat).lower()} expenses."))
        if e.get("readability") == "unreadable" and (total or 0) > general.get("receiptRequiredAbove", 0):
            results[eid].append(_result("RECEIPT_UNREADABLE", "warning", "Receipt not legible",
                                        f"A legible receipt is required above {inr(general['receiptRequiredAbove'])}. Consider re-uploading a clearer copy."))

    # ---- limits
    period_buckets: dict[tuple, list[dict]] = defaultdict(list)
    for e in active:
        cat = e["category"]["value"]
        limit = (cats.get(cat) or {}).get("limit")
        total = fval(e, "total")
        if not limit or total is None or total <= 0:
            continue
        per, amount = limit["per"], float(limit["amount"])
        label = cats[cat].get("label", cat)
        if per in ("item", "event"):
            if total > amount:
                _over_limit(results, e, label, total, amount, f"per {per}", total - amount)
        elif per == "night":
            nights = fval(e, "nights") or 1
            allowed = amount * nights
            if total > allowed:
                _over_limit(results, e, label, total, allowed, f"{inr(amount)}/night × {nights}", total - allowed)
        else:
            key = _period_key(per, e)
            if key:
                period_buckets[(cat, per, key)].append(e)

    for (cat, per, key), items in period_buckets.items():
        amount = float(cats[cat]["limit"]["amount"])
        spent = sum(fval(x, "total") for x in items)
        if spent <= amount:
            continue
        label = cats[cat].get("label", cat)
        # Attribute the excess to the latest expense in the period; flag the others for context.
        items = sorted(items, key=lambda x: x["seq"])
        culprit = items[-1]
        when = nice_date(key) if per == "day" else date.fromisoformat(key + "-01").strftime("%B %Y")
        detail = f"{label} on {when} total {inr(spent)} across {len(items)} expenses" if len(items) > 1 else None
        _over_limit(results, culprit, label, spent, amount, f"per {per}", spent - amount, detail=detail)
        for other in items[:-1]:
            results[other["id"]].append(_result("PERIOD_SHARED", "info", "Counts toward a daily limit",
                                                f"Together with other {label.lower()} on {when}, this exceeds the {inr(amount)}/{per} limit; the excess is attributed to the latest expense."))
    return results


def _over_limit(results, e, label, claimed, limit, per_text, excess, detail=None):
    decision = e.get("policyDecision") or {}
    excess = round(excess, 2)
    base = {
        "claimed": round(claimed, 2), "limit": round(limit, 2), "excess": excess,
        "resolvable": True, "detail": detail,
    }
    msg_core = f"Claimed {inr(claimed)} · Policy limit {inr(limit)} ({per_text}) · Excess {inr(excess)}"
    if detail:
        msg_core = f"{detail} · Policy limit {inr(limit)} ({per_text}) · Excess {inr(excess)}"
    if decision.get("type") == "cap":
        e["reimbursableAmount"] = round((fval(e, "total") or 0) - excess, 2)
        results[e["id"]].append(_result("OVER_LIMIT_CAPPED", "info", f"{label} capped at policy limit",
                                        f"{msg_core}. You chose to claim only up to the limit; {inr(excess)} will not be reimbursed.",
                                        resolution="cap", **base))
    elif decision.get("type") == "justify" and (decision.get("justification") or "").strip():
        results[e["id"]].append(_result("OVER_LIMIT_JUSTIFIED", "warning", f"{label} over limit — justified",
                                        f"{msg_core}. Your justification will go to the approver: “{decision['justification'].strip()}”",
                                        resolution="justify", **base))
    else:
        results[e["id"]].append(_result("OVER_LIMIT", "violation", f"{label} limit exceeded",
                                        msg_core + ". Choose to cap the claim at the limit or add a justification for your approver.",
                                        **base))
