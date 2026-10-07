"""Expense field vocabulary, confidence-aware field states and formatting helpers."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# name -> (label, type). Types drive normalisation and the edit form.
FIELD_DEFS: dict[str, tuple[str, str]] = {
    "merchant": ("Merchant", "text"),
    "invoiceNumber": ("Invoice / receipt no.", "text"),
    "expenseDate": ("Expense date", "date"),
    "currency": ("Currency", "currency"),
    "subtotal": ("Subtotal", "money"),
    "tax": ("Tax / GST", "money"),
    "serviceCharge": ("Service charge", "money"),
    "tip": ("Tip", "money"),
    "total": ("Total amount", "money"),
    "merchantAddress": ("Merchant address", "text"),
    "city": ("City", "text"),
    "gstin": ("GSTIN", "text"),
    "paymentMethod": ("Payment method", "text"),
    "cardLast4": ("Card (last 4)", "text"),
    "bookingReference": ("Booking / PNR", "text"),
    "origin": ("Origin", "text"),
    "destination": ("Destination", "text"),
    "travelDate": ("Travel date", "date"),
    "returnDate": ("Return date", "date"),
    "checkIn": ("Check-in", "date"),
    "checkOut": ("Check-out", "date"),
    "nights": ("Nights", "int"),
    "covers": ("Covers / guests", "int"),
    "billingPeriod": ("Billing period", "text"),
    "businessPurpose": ("Business purpose", "text"),
    "attendees": ("Attendees", "text"),
    "description": ("Description", "text"),
    "costCentre": ("Cost centre / project", "text"),
}

FINANCIAL_CORE = ("merchant", "expenseDate", "total", "currency")

KNOWN, INFERRED, LOW_CONFIDENCE, MISSING, NOT_REQUIRED = (
    "KNOWN", "INFERRED", "LOW_CONFIDENCE", "MISSING", "NOT_REQUIRED",
)


def make_field(value: Any = None, confidence: float = 0.0, source: str = "none", reason: str | None = None) -> dict:
    if value in ("", [], None):
        return {"value": None, "confidence": 0.0, "source": "none", "reason": reason}
    return {"value": value, "confidence": round(float(confidence), 2), "source": source, "reason": reason}


def fval(expense: dict, name: str) -> Any:
    return (expense.get("fields", {}).get(name) or {}).get("value")


def field_state(field: dict | None, thresholds: dict) -> str:
    if not field or field.get("value") in (None, "", []):
        return MISSING
    source = field.get("source")
    conf = field.get("confidence") or 0.0
    if source == "user":
        return KNOWN
    if source == "inferred":
        return INFERRED if conf >= thresholds["review"] else LOW_CONFIDENCE
    if conf >= thresholds["autoAccept"]:
        return KNOWN
    if conf >= thresholds["review"]:
        return INFERRED
    return LOW_CONFIDENCE


def label(name: str) -> str:
    return FIELD_DEFS.get(name, (name, "text"))[0]


def inr(amount: float | int | None, currency: str = "INR") -> str:
    if amount is None:
        return "—"
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "INR", f"{currency} ")
    neg = amount < 0
    amount = abs(round(float(amount), 2))
    whole = int(amount)
    frac = round(amount - whole, 2)
    s = str(whole)
    if (currency or "INR") == "INR" and len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    elif len(s) > 3:
        s = f"{whole:,}"
    if frac:
        s += f"{frac:.2f}"[1:]
    return f"{'-' if neg else ''}{symbol}{s}"


def nice_date(iso: str | None) -> str:
    if not iso:
        return "unknown date"
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%d %b %Y").lstrip("0")


def short_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return date.fromisoformat(iso).strftime("%d %b").lstrip("0")
    except ValueError:
        return iso


_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,19}\b")


def mask_card_numbers(text: str | None) -> str | None:
    """Replace anything that looks like a full card number with a masked form keeping the last four."""
    if not text:
        return text

    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"XXXX-XXXX-XXXX-{digits[-4:]}"

    return _CARD_RE.sub(_mask, text)
