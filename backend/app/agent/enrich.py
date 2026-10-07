"""Inference and grouping: fills what can reasonably be inferred and clusters expenses into trips.

Every value produced here is marked source="inferred" and derived=True, and is recomputed from
scratch on each revalidation, so a user correction upstream (e.g. a changed date) flows through.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .fields import fval, make_field
from .normaliser import nights_between

CITY_ALIASES = {
    "blr": "Bengaluru", "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "whitefield": "Bengaluru",
    "del": "Delhi", "delhi": "Delhi", "new delhi": "Delhi", "gurugram": "Delhi",
    "bom": "Mumbai", "mumbai": "Mumbai", "bombay": "Mumbai",
    "maa": "Chennai", "chennai": "Chennai", "hyd": "Hyderabad", "hyderabad": "Hyderabad",
    "pnq": "Pune", "pune": "Pune", "ccu": "Kolkata", "kolkata": "Kolkata",
}
RECURRING = {"MOBILE_INTERNET", "SOFTWARE_SUBSCRIPTION", "FUEL"}


def normalise_city(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return CITY_ALIASES[alias]
    return None


def _set_inferred(expense: dict, name: str, value, confidence: float, reason: str) -> None:
    f = make_field(value, confidence, "inferred", reason)
    f["derived"] = True
    expense["fields"][name] = f


def clear_derived(expense: dict) -> None:
    for name, f in list(expense["fields"].items()):
        if f and f.get("derived"):
            expense["fields"][name] = make_field()


def derive_fields(expense: dict) -> None:
    """Per-expense inferences that only need the expense itself."""
    fields = expense["fields"]
    cat = expense["category"]["value"]
    if not fields["currency"]["value"] and (fields["gstin"]["value"] or expense.get("_text", "").count("₹")):
        _set_inferred(expense, "currency", "INR", 0.85, "Indian GSTIN / ₹ symbol on the document.")
    if fields["checkIn"]["value"] and fields["checkOut"]["value"]:
        n = nights_between(fields["checkIn"]["value"], fields["checkOut"]["value"])
        if n and fields["nights"]["value"] != n and fields["nights"]["source"] != "user":
            _set_inferred(expense, "nights", n, 0.95, "Calculated from the check-in and check-out dates.")
    if not fields["expenseDate"]["value"]:
        if cat == "HOTEL" and fields["checkOut"]["value"]:
            _set_inferred(expense, "expenseDate", fields["checkOut"]["value"], 0.85, "Hotel invoices are dated at check-out.")
        elif cat in ("AIR_TRAVEL", "TRAIN_TRAVEL") and fields["travelDate"]["value"]:
            _set_inferred(expense, "expenseDate", fields["travelDate"]["value"], 0.85, "Using the travel date on the ticket.")
    if cat in ("AIR_TRAVEL", "TRAIN_TRAVEL") and not fields["travelDate"]["value"] and fields["expenseDate"]["value"]:
        _set_inferred(expense, "travelDate", fields["expenseDate"]["value"], 0.75, "Assumed travel on the booking date.")
    if not fields["city"]["value"]:
        city = normalise_city(fields["merchantAddress"]["value"]) or normalise_city(fields["destination"]["value"] if cat == "HOTEL" else None)
        if city:
            _set_inferred(expense, "city", city, 0.9, "From the merchant address.")


def _d(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def build_groups(active: list[dict], claim: dict) -> list[dict]:
    """Cluster expenses into trips (anchored on travel/hotel bookings) and monthly buckets."""
    anchors = []
    for e in active:
        cat = e["category"]["value"]
        if cat in ("AIR_TRAVEL", "TRAIN_TRAVEL"):
            start = _d(fval(e, "travelDate")) or _d(fval(e, "expenseDate"))
            end = _d(fval(e, "returnDate")) or start
            city = normalise_city(fval(e, "destination"))
            if start and city:
                anchors.append({"start": start, "end": end, "city": city, "src": e})
        elif cat == "HOTEL":
            start, end = _d(fval(e, "checkIn")), _d(fval(e, "checkOut"))
            city = fval(e, "city") or normalise_city(fval(e, "merchantAddress"))
            if start and end and city:
                anchors.append({"start": start, "end": end, "city": city, "src": e})

    trips: list[dict] = []
    for a in sorted(anchors, key=lambda x: x["start"]):
        for t in trips:
            if t["city"] == a["city"] and a["start"] <= t["end"] + timedelta(days=1) and a["end"] >= t["start"] - timedelta(days=1):
                t["start"], t["end"] = min(t["start"], a["start"]), max(t["end"], a["end"])
                t["anchors"].append(a["src"])
                break
        else:
            trips.append({"city": a["city"], "start": a["start"], "end": a["end"], "anchors": [a["src"]]})

    groups: dict[str, dict] = {}
    for t in trips:
        gid = f"trip-{t['city'].lower()}-{t['start'].isoformat()}"
        groups[gid] = {"id": gid, "kind": "trip", "city": t["city"], "start": t["start"].isoformat(),
                       "end": t["end"].isoformat(), "expenseIds": [], "anchorIds": [a["id"] for a in t["anchors"]]}

    overrides = claim.get("groupOverrides", {})
    for e in active:
        gid = overrides.get(e["id"])
        if not gid:
            d = _d(fval(e, "expenseDate"))
            cat = e["category"]["value"]
            for t in trips:
                tid = f"trip-{t['city'].lower()}-{t['start'].isoformat()}"
                if cat in RECURRING:
                    continue
                if d and t["start"] <= d <= t["end"]:
                    gid = tid
                    break
                if not d and (fval(e, "city") == t["city"] or normalise_city(fval(e, "merchantAddress")) == t["city"]):
                    gid = tid
                    break
        if not gid:
            d = _d(fval(e, "expenseDate"))
            gid = f"month-{d.strftime('%Y-%m')}" if d else "unassigned"
        if gid not in groups:
            if gid.startswith("month-"):
                d = date.fromisoformat(gid[6:] + "-01")
                groups[gid] = {"id": gid, "kind": "month", "month": gid[6:], "start": d.isoformat(), "expenseIds": [], "anchorIds": []}
            else:
                groups[gid] = {"id": gid, "kind": "custom" if gid != "unassigned" else "unassigned", "expenseIds": [], "anchorIds": []}
        groups[gid]["expenseIds"].append(e["id"])
        e["groupId"] = gid

    return [g for g in groups.values() if g["expenseIds"]]


def trip_purpose(group: dict, by_id: dict[str, dict]) -> tuple[str | None, str | None, str | None]:
    """Best known business purpose for a trip: (purpose, sourceExpenseId, description of source)."""
    best = None
    for eid in group["expenseIds"]:
        e = by_id[eid]
        f = e["fields"]["businessPurpose"]
        if f["value"] and not f.get("derived") and e["category"]["value"] not in ("MEALS", "CLIENT_ENTERTAINMENT"):
            rank = 2 if f["source"] == "user" else (1 if f["confidence"] >= 0.7 else 0)
            if rank and (best is None or rank > best[0]):
                best = (rank, f["value"], eid)
    if not best:
        return None, None, None
    src = by_id[best[2]]
    what = {"AIR_TRAVEL": "flight booking", "TRAIN_TRAVEL": "train booking", "HOTEL": "hotel invoice"}.get(
        src["category"]["value"], "expense")
    return best[1], best[2], f"{fval(src, 'merchant') or 'the'} {what}"


def group_title(group: dict, purpose: str | None) -> str:
    if group["kind"] == "trip":
        p = (purpose or "").lower()
        if any(k in p for k in ("customer", "client", "sales", "renewal")):
            kind = "Customer Visit"
        elif any(k in p for k in ("training", "conference", "summit", "workshop")):
            kind = "Training Visit"
        else:
            kind = "Business Trip"
        return f"{group['city']} {kind}"
    if group["kind"] == "month":
        return date.fromisoformat(group["start"]).strftime("%B %Y") + " expenses"
    if group["kind"] == "unassigned":
        return "Unassigned"
    return group["id"].replace("custom-", "").replace("-", " ").title()


def infer_purposes(groups: list[dict], by_id: dict[str, dict], policy: dict) -> None:
    for g in groups:
        purpose, src_id, src_desc = trip_purpose(g, by_id) if g["kind"] == "trip" else (None, None, None)
        g["purpose"] = purpose
        if not purpose:
            continue
        for eid in g["expenseIds"]:
            if eid == src_id:
                continue
            e = by_id[eid]
            f = e["fields"]["businessPurpose"]
            if f["value"] and not f.get("derived"):
                continue
            rule = policy["categories"].get(e["category"]["value"], {})
            if not rule.get("inheritTripPurpose"):
                continue
            total = fval(e, "total") or 0
            covers = fval(e, "covers")
            max_amt = rule.get("inheritPurposeMaxAmount")
            max_cov = rule.get("inheritPurposeMaxCovers")
            if max_amt is not None and total > max_amt:
                e.setdefault("_inferenceBlocked", []).append(
                    f"Did not assume the trip purpose because meals above ₹{max_amt:,} may be client entertainment.")
                continue
            if max_cov is not None and covers and covers > max_cov:
                e.setdefault("_inferenceBlocked", []).append(
                    f"Did not assume the trip purpose because the bill is for {covers} guests.")
                continue
            _set_inferred(e, "businessPurpose", purpose, 0.8,
                          f"Inferred from the {src_desc} — this expense falls within the trip dates.")
