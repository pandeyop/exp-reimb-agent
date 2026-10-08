"""Validates and normalises raw extraction output (from Claude or offline) into typed expense fields.

Nothing from the model is trusted blindly: every value is type-checked, coerced, range-checked and
dropped (with a note) if it fails. Card numbers are masked before anything is persisted.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .fields import FIELD_DEFS, make_field, mask_card_numbers

CURRENCY_ALIASES = {"₹": "INR", "RS": "INR", "RS.": "INR", "INR": "INR", "RUPEES": "INR", "$": "USD", "USD": "USD", "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP"}


def _to_money(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    if s in ("", ".", "-"):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _to_date(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if 2000 <= d.year <= 2100:
                return d.isoformat()
        except ValueError:
            continue
    return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def _to_currency(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[s]
    return s if re.fullmatch(r"[A-Z]{3}", s) else None


def coerce(name: str, value: Any) -> Any:
    """Coerce a value (from AI, a user edit or a chat answer) to the field's type. Returns None if invalid."""
    ftype = FIELD_DEFS.get(name, ("", "text"))[1]
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if ftype == "money":
        return _to_money(value)
    if ftype == "date":
        return _to_date(value)
    if ftype == "int":
        return _to_int(value)
    if ftype == "currency":
        return _to_currency(value)
    s = str(value).strip()
    if name == "cardLast4":
        digits = re.sub(r"\D", "", s)
        return digits[-4:] if len(digits) >= 4 else None
    s = mask_card_numbers(s)
    return s[:500]


def check_total_plausibility(fields: dict[str, dict], notes: list[str]) -> None:
    """Cross-check the arithmetic: subtotal + tax (+ service charge + tip) should equal the total.

    Only runs when subtotal, tax and total are all known. A mismatch beyond max(₹2, 2%) means one of
    the values was probably misread, so the total's confidence is capped below the review threshold
    and the employee is asked to confirm it.
    """
    v = {k: fields[k]["value"] for k in ("subtotal", "tax", "serviceCharge", "tip", "total")}
    if v["subtotal"] is None or v["tax"] is None or v["total"] is None:
        return
    expected = round(v["subtotal"] + v["tax"] + (v["serviceCharge"] or 0) + (v["tip"] or 0), 2)
    if abs(expected - v["total"]) <= max(2.0, 0.02 * abs(v["total"])):
        return
    parts = "subtotal + tax" + (" + service charge" if v["serviceCharge"] else "") + (" + tip" if v["tip"] else "")
    fields["total"]["confidence"] = min(fields["total"]["confidence"], 0.65)
    fields["total"]["reason"] = f"The {parts} ({expected:,.2f}) doesn't match the total ({v['total']:,.2f})."
    notes.append(f"The line amounts don't add up to the total ({parts} = {expected:,.2f} vs total {v['total']:,.2f}); please confirm the total.")


def normalise(raw: dict) -> dict:
    """Return {fields, lineItems, documentType, readability, categorySuggestion, notes, multipleTotalsFound}."""
    notes: list[str] = [mask_card_numbers(str(n))[:300] for n in (raw.get("notes") or []) if n]
    out_fields: dict[str, dict] = {}
    raw_fields = raw.get("fields") or {}
    for name in FIELD_DEFS:
        entry = raw_fields.get(name)
        if not isinstance(entry, dict):
            out_fields[name] = make_field()
            continue
        value = coerce(name, entry.get("value"))
        try:
            conf = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(max(conf, 0.0), 1.0)
        if entry.get("value") not in (None, "") and value is None:
            notes.append(f"Ignored an unreadable value for {FIELD_DEFS[name][0].lower()}.")
        out_fields[name] = make_field(value, conf, "extracted")

    if raw.get("multipleTotalsFound") and out_fields["total"]["value"] is not None:
        out_fields["total"]["confidence"] = min(out_fields["total"]["confidence"], 0.65)
        out_fields["total"]["reason"] = "More than one total appears on this document."
    check_total_plausibility(out_fields, notes)

    line_items = []
    for item in (raw.get("lineItems") or [])[:50]:
        if not isinstance(item, dict) or not item.get("description"):
            continue
        line_items.append({
            "description": mask_card_numbers(str(item["description"]))[:200],
            "quantity": _to_money(item.get("quantity")),
            "unitPrice": _to_money(item.get("unitPrice")),
            "amount": _to_money(item.get("amount")),
        })

    suggestion = raw.get("categorySuggestion")
    if not isinstance(suggestion, dict) or not suggestion.get("category"):
        suggestion = None

    return {
        "fields": out_fields,
        "lineItems": line_items,
        "documentType": str(raw.get("documentType") or "other"),
        "readability": str(raw.get("readability") or "fair"),
        "isExpenseDocument": bool(raw.get("isExpenseDocument", True)),
        "categorySuggestion": suggestion,
        "multipleTotalsFound": bool(raw.get("multipleTotalsFound")),
        "notes": notes,
        "text": raw.get("_text", ""),
    }


def nights_between(check_in: str | None, check_out: str | None) -> int | None:
    try:
        n = (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None
