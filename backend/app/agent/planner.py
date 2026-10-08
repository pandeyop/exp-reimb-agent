"""MissingInformationDetector + ClarificationPlanner.

A question is generated only when information is (1) mandatory under policy, (2) missing or too
uncertain, and (3) not inferable from the evidence. Related gaps are consolidated: one question
per expense, and one question for a business purpose shared by a whole trip.
"""
from __future__ import annotations

from datetime import date, timedelta

from .fields import (
    FIELD_DEFS, FINANCIAL_CORE, INFERRED, KNOWN, LOW_CONFIDENCE, MISSING, NOT_REQUIRED,
    field_state, fval, inr, label, nice_date, short_date,
)

DECISION_KINDS = {"duplicate"}


# ------------------------------------------------------------------ MissingInformationDetector

def required_fields(expense: dict, policy: dict) -> list[str]:
    rule = policy["categories"].get(expense["category"]["value"], {})
    req = list(policy["general"]["alwaysRequired"])
    for f in rule.get("requiredFields", []):
        if f not in req:
            req.append(f)
    return req


def detect_gaps(expense: dict, policy: dict) -> tuple[list[dict], dict[str, str]]:
    th = policy["confidenceThresholds"]
    req = set(required_fields(expense, policy))
    states: dict[str, str] = {}
    gaps: list[dict] = []
    for name in FIELD_DEFS:
        st = field_state(expense["fields"].get(name), th)
        if st == MISSING and name not in req:
            st = NOT_REQUIRED
        states[name] = st
        if name in req and st in (MISSING, LOW_CONFIDENCE):
            gaps.append({"field": name, "state": st})
    cat = expense["category"]
    if cat.get("source") != "user" and (cat.get("confidence") or 0) < th["review"]:
        gaps.append({"field": "category", "state": LOW_CONFIDENCE})
        states["category"] = LOW_CONFIDENCE
    else:
        states["category"] = KNOWN if cat.get("source") == "user" or (cat.get("confidence") or 0) >= th["autoAccept"] else INFERRED
    return gaps, states


# ------------------------------------------------------------------ ClarificationPlanner

def _desc(e: dict) -> str:
    """Short human description: 'the ₹1,850 Hops & Barrels bill from 22 Sep'."""
    parts = []
    total = fval(e, "total")
    merchant = fval(e, "merchant")
    if total is not None:
        parts.append(inr(total, fval(e, "currency") or "INR"))
    parts.append(merchant or "unnamed")
    noun = {"MEALS": "bill", "CLIENT_ENTERTAINMENT": "bill", "HOTEL": "invoice", "AIR_TRAVEL": "ticket",
            "TAXI": "ride", "PARKING": "slip"}.get(e["category"]["value"], "receipt")
    s = f"the {' '.join(parts)} {noun}"
    if fval(e, "expenseDate"):
        s += f" from {short_date(fval(e, 'expenseDate'))}"
    return s


def _opt(label_: str, value: str) -> dict:
    return {"label": label_, "value": value}


def plan(active: list[dict], groups: list[dict], policy: dict) -> list[dict]:
    by_id = {e["id"]: e for e in active}
    cats = policy["categories"]
    questions: list[dict] = []
    covered: set[tuple[str, str]] = set()  # (expenseId, field) handled by a consolidated question

    # 1. Trip-wide business purpose — one answer fills every expense in the trip.
    for g in groups:
        if g["kind"] != "trip":
            continue
        ids = [eid for eid in g["expenseIds"]
               if any(gap["field"] == "businessPurpose" for gap in by_id[eid]["_gaps"])
               and cats.get(by_id[eid]["category"]["value"], {}).get("inheritTripPurpose")
               and not by_id[eid].get("_inferenceBlocked")]
        if ids:
            dates = f"{short_date(g['start'])}–{short_date(g['end'])}"
            questions.append({
                "id": f"trip_purpose:{g['id']}", "kind": "trip_purpose", "expenseIds": ids,
                "fields": ["businessPurpose"],
                "text": (f"What was the business purpose of your {g['city']} trip ({dates})? "
                         f"One answer covers all {len(ids)} trip expense{'s' if len(ids) > 1 else ''} that need it."
                         if len(ids) > 1 else
                         f"What was the business purpose of {_desc(by_id[ids[0]])} ({g['city']} trip, {dates})?"),
                "options": [], "placeholder": "e.g. Quarterly business review with Northwind Retail",
            })
            covered.update((eid, "businessPurpose") for eid in ids)

    for e in sorted(active, key=lambda x: x["seq"]):
        eid, cat = e["id"], e["category"]["value"]
        gaps = [g for g in e["_gaps"] if (eid, g["field"]) not in covered]
        gap_fields = [g["field"] for g in gaps]

        # 2. Meal purpose (may turn it into client entertainment).
        if cat == "MEALS" and "businessPurpose" in gap_fields:
            covers = fval(e, "covers")
            why = ""
            if e.get("_inferenceBlocked"):
                why = f" ({'a bill for ' + str(covers) + ' guests' if covers and covers > 1 else 'a larger bill'} could be client entertainment, so I won't assume)"
            questions.append({
                "id": f"meal_purpose:{eid}", "kind": "meal_purpose", "expenseIds": [eid], "fields": ["businessPurpose"],
                "text": f"I can read the amount and date from {_desc(e)}, but I can't determine the business purpose{why}. Was this:",
                "options": [_opt("Client meal", "client"), _opt("Team meal", "team"),
                            _opt("Personal meal", "personal"), _opt("Something else", "other")],
                "placeholder": "e.g. Client dinner with Priya Shah and Rahul Mehta (Northwind)",
            })
            gap_fields.remove("businessPurpose")

        # 3. Client entertainment needs purpose + attendees, asked together.
        if cat == "CLIENT_ENTERTAINMENT" and ({"attendees", "businessPurpose"} & set(gap_fields)):
            need = [f for f in ("businessPurpose", "attendees") if f in gap_fields]
            text = (f"For {_desc(e)}, who attended? Names and company are enough."
                    if need == ["attendees"] else
                    f"For {_desc(e)} I need the client meeting purpose and who attended.")
            questions.append({
                "id": f"entertainment:{eid}", "kind": "attendees", "expenseIds": [eid], "fields": need,
                "text": text, "options": [], "placeholder": "e.g. Priya Shah and Rahul Mehta from Northwind Retail",
            })
            gap_fields = [f for f in gap_fields if f not in need]

        # 4. Category confirmation.
        if "category" in gap_fields:
            gap_fields.remove("category")
            ranked = e.get("_categoryRanking") or []
            opts = [_opt(cats[c]["label"], c) for c in ranked[:3] if c in cats]
            if not any(o["value"] == e["category"]["value"] for o in opts) and e["category"]["value"] in cats:
                opts.insert(0, _opt(cats[e["category"]["value"]]["label"], e["category"]["value"]))
            opts.append(_opt("Miscellaneous", "MISCELLANEOUS")) if not any(o["value"] == "MISCELLANEOUS" for o in opts) else None
            questions.append({
                "id": f"category:{eid}", "kind": "category", "expenseIds": [eid], "fields": ["category"],
                "text": (f"I'm not sure how to categorise {_desc(e)}. My best guess is "
                         f"{cats.get(e['category']['value'], {}).get('label', e['category']['value'])} "
                         f"({round((e['category'].get('confidence') or 0) * 100)}% confident). Which is it?"),
                "options": opts[:4], "placeholder": "Category",
            })

        # 5. Remaining missing / low-confidence fields — one consolidated question per expense.
        if gap_fields:
            questions.append(_fields_question(e, gaps, gap_fields, groups))

        # 6. Policy decisions — deferred while information is still missing, because the answer
        #    can change the category and therefore the limit (e.g. a "meal" that was client entertainment).
        for r in ([] if e["_gaps"] else e.get("policyResults", [])):
            if r["code"] == "OVER_LIMIT":
                questions.append({
                    "id": f"over_limit:{eid}", "kind": "over_limit", "expenseIds": [eid], "fields": [],
                    "text": f"⚠ {r['title']} on {_desc(e)}. {r['message'].split(' Choose')[0]} How would you like to handle it?",
                    "options": [_opt(_cap_label(r), "cap"),
                                _opt("Keep full amount & justify", "justify")],
                    "placeholder": "Or type a justification for your approver",
                })
            elif r["code"] in ("FUTURE_DATED", "NON_POSITIVE"):
                f = "expenseDate" if r["code"] == "FUTURE_DATED" else "total"
                questions.append({
                    "id": f"invalid:{eid}:{f}", "kind": "fields", "expenseIds": [eid], "fields": [f],
                    "text": f"{r['message']} What is the correct {label(f).lower()} for {fval(e, 'merchant') or 'this expense'}?",
                    "options": [], "placeholder": "e.g. 22 Sep 2026" if f == "expenseDate" else "e.g. 642",
                })
        decision = e.get("policyDecision") or {}
        if decision.get("type") == "justify" and not (decision.get("justification") or "").strip():
            questions.append({
                "id": f"justification:{eid}", "kind": "justification", "expenseIds": [eid], "fields": [],
                "text": f"Please give a one-line justification for keeping the full amount on {_desc(e)}. It goes to your approver.",
                "options": [], "placeholder": "e.g. Conference week — policy-rate hotels near the client office were sold out",
            })

        # 7. Duplicates (a decision rather than a question).
        dup = e.get("duplicate")
        if dup:
            sig = dup["signals"]
            joined = (", ".join(sig[:-1]) + " and " + sig[-1]) if len(sig) > 1 else (sig[0] if sig else "content")
            if dup.get("ofExpenseId"):
                text = (f"⊘ Possible duplicate ({round(dup['score'] * 100)}% match): two {fval(e, 'merchant')} receipts for "
                        f"{inr(fval(e, 'total'))} on {short_date(fval(e, 'expenseDate'))}. The {joined} match. Is the second one a duplicate?")
            else:
                text = f"⊘ {_desc(e).capitalize()} may already have been reimbursed: {dup['explanation']} Is it a duplicate?"
            questions.append({
                "id": f"duplicate:{eid}", "kind": "duplicate", "expenseIds": [eid], "fields": [],
                "text": text,
                "options": [_opt("Yes, it's a duplicate — remove it", "duplicate"), _opt("No, keep both", "separate")],
                "placeholder": "",
            })
    # Information first (it may change categories and limits), then policy decisions, then duplicates.
    order = {"trip_purpose": 0, "meal_purpose": 1, "attendees": 2, "fields": 3, "category": 4,
             "over_limit": 5, "justification": 5, "duplicate": 6}
    return sorted(questions, key=lambda q: order.get(q["kind"], 9))


def _cap_label(r: dict) -> str:
    """Quick-reply label for capping. For a shared daily/monthly limit this expense is only reduced by its share."""
    capped = r.get("cappedAmount", r["limit"])
    if abs(capped - r["limit"]) < 0.01:
        return f"Cap at the limit ({inr(capped)})"
    return f"Cap to stay within the limit (claim {inr(capped)})"


def _fields_question(e: dict, gaps: list[dict], gap_fields: list[str], groups: list[dict]) -> dict:
    eid = e["id"]
    states = {g["field"]: g["state"] for g in gaps}
    known_bits = []
    for f in ("total", "expenseDate"):
        if f not in gap_fields and fval(e, f) is not None:
            v = fval(e, f)
            known_bits.append(inr(v, fval(e, "currency") or "INR") if f == "total" else nice_date(v) if f == "expenseDate" else v)
    options: list[dict] = []
    if len(gap_fields) == 1 and states[gap_fields[0]] == LOW_CONFIDENCE:
        f = gap_fields[0]
        v = fval(e, f)
        shown = inr(v) if FIELD_DEFS[f][1] == "money" else nice_date(v) if FIELD_DEFS[f][1] == "date" else v
        text = f"I read the {label(f).lower()} on {_desc(e)} as {shown}, but I'm not confident. Is that right?"
        options = [_opt(f"Yes, {shown} is correct", "confirm")]
    else:
        if "total" in gap_fields and states.get("total") == MISSING:
            lead = f"I couldn't reliably read the total from the {fval(e, 'merchant') or 'uploaded'} receipt."
        elif known_bits:
            lead = f"I can read {', '.join(str(b) for b in known_bits)} from {fval(e, 'merchant') and 'the ' + fval(e, 'merchant') + ' receipt' or 'this receipt'}"
            missing_names = [label(f).lower() for f in gap_fields]
            lead += f", but I can't read the {' or '.join(missing_names)}."
        else:
            lead = "I couldn't read this document."
        need = [label(f).lower() for f in gap_fields]
        text = f"{lead} Please tell me the {', '.join(need[:-1]) + ' and ' + need[-1] if len(need) > 1 else need[0]}" + (
            " or upload a clearer copy." if "total" in gap_fields or "merchant" in gap_fields else ".")
        if gap_fields == ["expenseDate"]:
            trip = next((g for g in groups if g["id"] == e.get("groupId") and g["kind"] == "trip"), None)
            if trip:
                d0, d1 = date.fromisoformat(trip["start"]), date.fromisoformat(trip["end"])
                days = [(d0 + timedelta(days=i)) for i in range((d1 - d0).days + 1)][:5]
                text = text[:-1] + f" — it falls in your {trip['city']} trip ({short_date(trip['start'])}–{short_date(trip['end'])}). Which day?"
                options = [_opt(d.strftime("%a %d %b"), d.isoformat()) for d in days]
    return {
        "id": f"fields:{eid}:{'+'.join(gap_fields)}", "kind": "fields", "expenseIds": [eid], "fields": gap_fields,
        "text": text, "options": options,
        "placeholder": "e.g. " + ", ".join({"total": "₹642", "expenseDate": "22 Sep", "merchant": "Merchant name"}.get(f, label(f)) for f in gap_fields),
    }
