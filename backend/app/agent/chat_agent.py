"""Conversational agent.

Quick-reply buttons are applied deterministically. Free text goes to Claude (tool use over the
ClaimManager's actions) when available; otherwise — or if the AI call fails — a deterministic
interpreter maps the answer onto the open question it most likely addresses. The agent can edit
the claim but has no tool to submit it: submission always requires the employee's explicit action.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from .. import db
from ..settings import get_settings, load_policy
from . import claim_manager as cm
from .fields import FIELD_DEFS, fval, inr, short_date
from .llm import LLMError, tool_turn
from .normaliser import coerce

log = logging.getLogger("expense_agent.chat")

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class NeedsMore(Exception):
    """The answer couldn't be applied; the message is a follow-up to show the employee."""


# ================================================================== entry point

def handle(claim_id: str, text: str | None, question_id: str | None = None, option: str | None = None) -> dict:
    claim = cm.get_claim_or_404(claim_id)
    text = (text or "").strip()[:2000]
    if not text and not option:
        raise cm.ClaimError("Message is empty.")
    snap = cm.snapshot(claim_id)
    questions = {q["id"]: q for q in snap["questions"]}

    shown = text
    if option and question_id in questions:
        opt = next((o for o in questions[question_id]["options"] if o["value"] == option), None)
        shown = text or (opt["label"] if opt else option)
    db.add_message(claim_id, {"role": "user", "kind": "text", "text": shown, "questionId": question_id})

    if claim["status"] == "SUBMITTED":
        return _reply(claim_id, f"This claim was submitted as {claim['submission']['claimReference']}, so it can't be changed. Start a new claim for further expenses.")
    if claim["status"] == "PROCESSING" and not questions:
        return _reply(claim_id, "I'm still reading your documents — I'll summarise as soon as they're done.")

    reply = None
    with cm.quiet():
        try:
            if option or question_id:
                q = questions.get(question_id)
                if not q:
                    reply = "That question has already been answered — thanks!"
                else:
                    reply = apply_answer(claim_id, q, text, option)
            elif get_settings().ai_enabled:
                try:
                    reply = llm_turn(claim_id, text, snap)
                except LLMError as e:
                    log.warning("chat LLM failed, using deterministic interpreter: %s", e)
            if reply is None:
                reply = deterministic_turn(claim_id, text, snap)
        except NeedsMore as e:
            reply = str(e)
        except cm.ClaimError as e:
            reply = f"I couldn't apply that: {e}"
    msg = _reply(claim_id, reply)
    cm.revalidate(claim_id, announce=True)
    return msg


def _reply(claim_id: str, text: str) -> dict:
    return db.add_message(claim_id, {"role": "assistant", "kind": "text", "text": text})


# ================================================================== deterministic interpreter

def deterministic_turn(claim_id: str, text: str, snap: dict) -> str:
    low = text.lower()
    open_q = snap["questions"]
    if re.search(r"\bsubmit\b", low) and not open_q:
        return "Everything's in order. Please press **Review & Submit** — I never submit a claim without your explicit confirmation."
    if re.search(r"\b(status|summary|where are we|what's left|what is left|remaining)\b", low) or not open_q:
        return status_text(snap)
    q = route(text, open_q, snap)
    return apply_answer(claim_id, q, text, None)


def route(text: str, open_q: list[dict], snap: dict) -> dict:
    """Pick the open question this free-text answer most plausibly addresses."""
    low = text.lower()
    exps = {e["id"]: e for e in snap["expenses"]}
    # Explicit mention of a merchant ties the answer to that expense's question.
    for q in open_q:
        for eid in q["expenseIds"]:
            m = (fval(exps.get(eid, {"fields": {}}), "merchant") or "").lower()
            words = [w for w in re.findall(r"[a-z]{4,}", m)]
            if words and any(w in low for w in words) and len(q["expenseIds"]) == 1:
                return q
    # Otherwise score how well the text fits each kind of question and take the best fit.
    def fit(q: dict) -> float:
        k = q["kind"]
        if k == "duplicate":
            return 3 if re.search(r"\b(duplicate|same receipt|keep both|both are|separate|twice)\b", low) else 0
        if k in ("over_limit", "justification"):
            s = 3 if re.search(r"\b(cap|limit|justif|over|hotel|sold out|approv)", low) else 0
            return s + (0.5 if k == "justification" else 0)
        if k == "meal_purpose":
            return 3 if _meal_choice(low) else (1 if re.search(r"\b(dinner|lunch|meal|breakfast|food)\b", low) else 0)
        if k == "attendees":
            return 2.5 if _attendees(text) else 0.5
        if k == "category":
            return 3 if _category_from_text(low, load_policy()) else 0
        if k == "trip_purpose":
            return 2 if re.search(r"\b(trip|visit|meeting|review|customer|client|conference|training|purpose)\b", low) else 0.5
        if k == "fields":
            fields = q.get("fields") or []
            if any(FIELD_DEFS[f][1] == "date" for f in fields) and parse_date(text, {"start": None}) or re.search(
                    r"\b(" + "|".join(WEEKDAYS) + r")\b", low):
                return 2.5
            if any(FIELD_DEFS[f][1] == "money" for f in fields) and parse_money(text) is not None:
                return 2.5
            return 0.3
        return 0
    scored = sorted(enumerate(open_q), key=lambda iq: (-fit(iq[1]), iq[0]))
    return scored[0][1]


def apply_answer(claim_id: str, q: dict, text: str, option: str | None) -> str:
    kind = q["kind"]
    eids = q["expenseIds"]
    exps = {e["id"]: e for e in cm.db.list_expenses(claim_id)}
    e = exps.get(eids[0]) if eids else None
    low = (text or "").lower()

    if kind == "trip_purpose":
        purpose = _clean_purpose(text)
        if len(purpose) < 4:
            raise NeedsMore("Could you describe the trip's purpose in a few words? For example: “Quarterly review with Northwind Retail”.")
        for eid in eids:
            cm.update_fields(eid, {"businessPurpose": purpose}, via="chat")
        return f"Thanks — I've recorded “{purpose}” as the business purpose for {len(eids)} trip expense{'s' if len(eids) != 1 else ''}."

    if kind == "meal_purpose":
        choice = option or _meal_choice(low)
        merchant = fval(e, "merchant")
        if choice == "personal":
            cm.set_excluded(e["id"], True, "Personal expense — not reimbursable")
            return f"Understood — personal meals aren't reimbursable, so I've excluded the {merchant} bill from this claim."
        if choice == "client":
            attendees = _attendees(text)
            changes = {"category": "CLIENT_ENTERTAINMENT", "businessPurpose": _clean_purpose(text) if len(text) > 15 else "Client meal"}
            if attendees:
                changes["attendees"] = attendees
            cm.update_fields(e["id"], changes, via="chat")
            if attendees:
                return f"Got it — I've recategorised the {merchant} bill as Client Entertainment with attendees {attendees}. That's within the {inr(5000)} per-event limit."
            return f"Got it — I've recategorised the {merchant} bill as Client Entertainment. Policy also needs the attendees for client meals."
        if choice == "team":
            purpose = _clean_purpose(text) if len(text) > 12 else "Team meal"
            changes = {"businessPurpose": purpose}
            att = _attendees(text)
            if att:
                changes["attendees"] = att
            cm.update_fields(e["id"], changes, via="chat")
            return f"Thanks — recorded the {merchant} bill as a team meal."
        if text and len(text) >= 8:
            cm.update_fields(e["id"], {"businessPurpose": _clean_purpose(text)}, via="chat")
            return f"Thanks — I've recorded that as the purpose of the {merchant} bill."
        raise NeedsMore(f"No problem — in a few words, what was the {merchant} bill for?")

    if kind == "attendees":
        changes = {}
        if "attendees" in q["fields"]:
            att = _attendees(text) or (text.strip() if len(text.strip()) >= 3 else None)
            if not att:
                raise NeedsMore("Who attended? Names and company are enough, e.g. “Priya Shah and Rahul Mehta from Northwind”.")
            changes["attendees"] = att
        if "businessPurpose" in q["fields"]:
            changes["businessPurpose"] = _clean_purpose(text.split(" with ")[0]) if " with " in text else "Client meeting"
        cm.update_fields(e["id"], changes, via="chat")
        return f"Thanks — recorded attendees for the {fval(e, 'merchant')} bill: {changes.get('attendees', '')}."

    if kind == "category":
        policy = load_policy()
        code = option or _category_from_text(low, policy)
        if not code:
            raise NeedsMore("Which category fits best? You can tap one of the options above.")
        cm.update_fields(e["id"], {"category": code}, via="chat")
        return f"Done — {fval(e, 'merchant')} is now categorised as {policy['categories'][code]['label']}."

    if kind == "over_limit":
        choice = option
        if not choice:
            if re.search(r"\b(cap|limit|reduce|only claim|policy amount|max)\b", low):
                choice = "cap"
            elif len(text) >= 15:
                cm.policy_decision(e["id"], "justify", text)
                return "Thanks — I've kept the full amount and attached your justification for the approver. This is now a non-blocking warning."
            else:
                choice = "justify"
        if choice == "cap":
            cm.policy_decision(e["id"], "cap")
            r = next((r for r in e.get("policyResults", []) if r["code"] == "OVER_LIMIT"), {})
            return f"Done — I'll claim {fval(e, 'merchant')} up to the policy limit ({inr(r.get('limit'))}); the excess of {inr(r.get('excess'))} won't be reimbursed."
        cm.policy_decision(e["id"], "justify", None)
        return "OK — keeping the full amount. I just need a one-line justification for your approver."

    if kind == "justification":
        if len(text) < 6:
            raise NeedsMore("A short sentence is enough, e.g. “Policy-rate hotels near the client office were sold out”.")
        cm.policy_decision(e["id"], "justify", text)
        return "Thanks — justification attached. The over-limit amount is now a non-blocking warning for your approver."

    if kind == "duplicate":
        choice = option or ("duplicate" if re.search(r"\b(yes|duplicate|same|remove|delete|drop)\b", low) else
                            "separate" if re.search(r"\b(no|separate|different|both|keep|legit)\b", low) else None)
        if not choice:
            raise NeedsMore("Is it a duplicate (I'll exclude it) or two separate expenses (I'll keep both)?")
        cm.resolve_duplicate(e["id"], choice)
        return ("Removed the duplicate — it's excluded from the claim total." if choice == "duplicate"
                else "OK — keeping both as separate legitimate expenses.")

    if kind == "fields":
        if option == "confirm":
            cm.confirm_field(e["id"], q["fields"][0])
            return f"Thanks for confirming the {FIELD_DEFS[q['fields'][0]][0].lower()}."
        changes = {}
        for f in q["fields"]:
            ftype = FIELD_DEFS[f][1]
            if ftype == "date":
                v = option if (option and re.fullmatch(r"\d{4}-\d{2}-\d{2}", option)) else parse_date(text, _date_context(claim_id, e))
            elif ftype == "money":
                v = parse_money(text)
            elif ftype == "currency":
                v = coerce("currency", option or text)
            elif f == "merchant":
                v = re.sub(r"(₹|rs\.?|inr)?\s*\d[\d,]*(\.\d+)?", "", text, flags=re.I).strip(" ,.-") or None
            else:
                v = text.strip() or None
            if v is not None:
                changes[f] = v
        if not changes:
            names = " and ".join(FIELD_DEFS[f][0].lower() for f in q["fields"])
            raise NeedsMore(f"Sorry, I couldn't find the {names} in that. Could you rephrase (e.g. “23 Sep”, “₹642”)?")
        cm.update_fields(e["id"], changes, via="chat")
        pretty = ", ".join(
            f"{FIELD_DEFS[k][0].lower()} {short_date(v) if FIELD_DEFS[k][1] == 'date' else inr(v) if FIELD_DEFS[k][1] == 'money' else v}"
            for k, v in changes.items())
        return f"Updated {fval(e, 'merchant') or 'the expense'}: {pretty}."

    raise NeedsMore("I'm not sure how to apply that — you can also edit the expense directly in the table.")


def status_text(snap: dict) -> str:
    c = snap["claim"]
    d = c["dashboard"]
    s = (f"**{c['title']}** — {d['expenses']} expenses, {inr(c['totalAmount'])} reimbursable. "
         f"✓ {d['complete']} ready · ? {d['needsInformation']} need info · ⚠ {d['policyViolations']} over policy · ⊘ {d['duplicates']} duplicate. ")
    if c["status"] == "READY_TO_SUBMIT":
        return s + "The claim is **READY TO SUBMIT** — press Review & Submit when you're happy."
    open_q = [q for q in snap["questions"]]
    if open_q:
        return s + f"Still open: {len(open_q)} item{'s' if len(open_q) != 1 else ''} — see the questions above."
    return s


# ------------------------------------------------------------------ parsing helpers

def _clean_purpose(text: str) -> str:
    t = (text or "").strip()
    while True:
        stripped = re.sub(r"^(it was|it's|this was|that was|was|for|a|an|the)\s+", "", t, flags=re.I)
        if stripped == t:
            break
        t = stripped
    return (t[:1].upper() + t[1:]).rstrip(".")[:200]


def _meal_choice(low: str) -> str | None:
    if re.search(r"\b(personal|myself|my own|alone|not business|family)\b", low):
        return "personal"
    if re.search(r"\b(client|customer|prospect|partner|vendor)s?\b", low):
        return "client"
    if re.search(r"\b(team|colleague|internal|offsite|co-?workers)s?\b", low):
        return "team"
    return None


def _attendees(text: str) -> str | None:
    m = re.search(r"\bwith\s+(.+)$", text or "", re.I)
    if not m:
        return None
    s = m.group(1).strip().rstrip(".")
    s = re.sub(r"^(the\s+)?(client|customer)s?\s*[:,-]?\s*", "", s, flags=re.I)
    return s[:300] if len(s) >= 3 else None


def _category_from_text(low: str, policy: dict) -> str | None:
    for code, c in policy["categories"].items():
        lbl = c["label"].lower()
        if lbl in low or any(part.strip() and part.strip() in low for part in lbl.split("/")):
            return code
    return None


def parse_money(text: str) -> float | None:
    m = re.search(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)", text or "", re.I)
    return coerce("total", m.group(1)) if m else None


def _date_context(claim_id: str, e: dict) -> dict:
    claim = db.get_claim(claim_id)
    g = next((g for g in claim.get("groups", []) if g["id"] == e.get("groupId")), None)
    return {"start": g.get("start") if g else None, "end": g.get("end") if g else None}


def parse_date(text: str, ctx: dict) -> str | None:
    """Understand '23 Sep', 'Sep 23', '23/09', '2026-09-23', 'the 23rd', 'Tuesday' (within the trip)."""
    low = (text or "").lower()
    today = get_settings().today()
    ref = date.fromisoformat(ctx["start"]) if ctx.get("start") else today
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", low):
        return coerce("expenseDate", m.group(0))
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s*(\d{4}))?", low):
        return _mk(int(m.group(3) or ref.year), MONTHS[m.group(2)], int(m.group(1)))
    if m := re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", low):
        return _mk(int(m.group(3) or ref.year), MONTHS[m.group(1)], int(m.group(2)))
    if m := re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", low):
        y = int(m.group(3)) if m.group(3) else ref.year
        return _mk(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    for i, wd in enumerate(WEEKDAYS):
        if wd in low or re.search(rf"\b{wd[:3]}\b", low):
            if ctx.get("start"):
                d0, d1 = date.fromisoformat(ctx["start"]), date.fromisoformat(ctx["end"])
                for k in range((d1 - d0).days + 1):
                    if (d0 + timedelta(days=k)).weekday() == i:
                        return (d0 + timedelta(days=k)).isoformat()
    if m := re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low):
        return _mk(ref.year, ref.month, int(m.group(1)))
    return None


def _mk(y: int, mth: int, d: int) -> str | None:
    try:
        return date(y, mth, d).isoformat()
    except ValueError:
        return None


# ================================================================== Claude tool-use turn

def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name, "description": description, "strict": True,
        "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }


def _tools(policy: dict) -> list[dict]:
    field_names = [f for f in FIELD_DEFS]
    return [
        _tool("set_expense_fields",
              "Record values the employee told you for one expense (business purpose, attendees, dates, amounts, merchant, description, cost centre, origin/destination...). Dates as YYYY-MM-DD, amounts as plain numbers.",
              {"expenseId": {"type": "string"},
               "updates": {"type": "array", "items": {"type": "object", "properties": {
                   "field": {"type": "string", "enum": field_names}, "value": {"type": "string"}},
                   "required": ["field", "value"], "additionalProperties": False}}},
              ["expenseId", "updates"]),
        _tool("set_category", "Change an expense's reimbursement category (e.g. a meal that was actually client entertainment).",
              {"expenseId": {"type": "string"}, "category": {"type": "string", "enum": list(policy["categories"].keys())}},
              ["expenseId", "category"]),
        _tool("confirm_field", "The employee confirmed that an uncertain extracted value is correct.",
              {"expenseId": {"type": "string"}, "field": {"type": "string", "enum": field_names}}, ["expenseId", "field"]),
        _tool("resolve_duplicate", "Apply the employee's decision on a suspected duplicate. 'duplicate' excludes it; 'separate' keeps both.",
              {"expenseId": {"type": "string"}, "resolution": {"type": "string", "enum": ["duplicate", "separate"]}},
              ["expenseId", "resolution"]),
        _tool("resolve_over_limit", "Apply the employee's choice for an over-limit expense: cap at the policy limit, or keep the full amount with a justification for the approver (justification required for 'justify').",
              {"expenseId": {"type": "string"}, "action": {"type": "string", "enum": ["cap", "justify"]},
               "justification": {"type": "string", "description": "The employee's words; empty string when capping."}},
              ["expenseId", "action", "justification"]),
        _tool("exclude_expense", "Exclude an expense from the claim (e.g. the employee says it was personal). Never use this for suspected duplicates — use resolve_duplicate.",
              {"expenseId": {"type": "string"}, "reason": {"type": "string"}}, ["expenseId", "reason"]),
        _tool("rename_claim", "Rename the claim when the employee asks.", {"title": {"type": "string"}}, ["title"]),
    ]


CHAT_SYSTEM = """You are the expense assistant inside an AI-native reimbursement app for an Indian company. The employee uploaded receipts; the system already extracted, categorised, policy-checked and duplicate-checked them. Your job in this turn: understand the employee's message and apply it to the claim with the tools, then reply briefly.

Rules:
- Use tools to record answers. One message may answer several open questions — handle all of them.
- Only record what the employee actually said. Never invent amounts, dates, merchants or attendees.
- If the employee says a meal was with a client/customer, set_category to CLIENT_ENTERTAINMENT and record businessPurpose and attendees (if given).
- If they say an expense was personal, exclude it with exclude_expense.
- You cannot submit the claim. If asked, tell them to press Review & Submit.
- Ask a follow-up only when something required is still missing; never re-ask what is already known.
- Reply in 1-3 short sentences, plain and friendly, using ₹ amounts. Don't list the remaining questions — they're shown as cards in the UI."""


def _state_for_llm(snap: dict) -> str:
    exps = []
    for e in snap["expenses"]:
        if e["excluded"]:
            continue
        exps.append({
            "id": e["id"], "merchant": fval(e, "merchant"), "date": fval(e, "expenseDate"), "total": fval(e, "total"),
            "category": e["category"]["value"], "status": e["status"],
            "businessPurpose": fval(e, "businessPurpose"), "attendees": fval(e, "attendees"),
            "missing": [g["field"] for g in e.get("gaps", [])],
            "policy": [r["title"] for r in e.get("policyResults", []) if r["severity"] != "info"],
            "duplicateOf": (e.get("duplicate") or {}).get("ofExpenseId"),
        })
    qs = [{"id": q["id"], "kind": q["kind"], "expenseIds": q["expenseIds"], "text": q["text"],
           "options": [o["value"] for o in q.get("options", [])]} for q in snap["questions"]]
    return json.dumps({"claimTitle": snap["claim"]["title"], "claimStatus": snap["claim"]["status"],
                       "today": get_settings().today().isoformat(), "expenses": exps, "openQuestions": qs}, ensure_ascii=False)


def _run_tool(name: str, args: dict) -> str:
    try:
        if name == "set_expense_fields":
            changes = {u["field"]: u["value"] for u in args["updates"]}
            applied = cm.update_fields(args["expenseId"], changes, via="chat")
            return f"ok: {applied}"
        if name == "set_category":
            cm.update_fields(args["expenseId"], {"category": args["category"]}, via="chat")
            return "ok"
        if name == "confirm_field":
            cm.confirm_field(args["expenseId"], args["field"])
            return "ok"
        if name == "resolve_duplicate":
            cm.resolve_duplicate(args["expenseId"], args["resolution"])
            return "ok"
        if name == "resolve_over_limit":
            if args["action"] == "justify" and not (args.get("justification") or "").strip():
                return "error: a justification sentence from the employee is required for 'justify' — ask them for it"
            cm.policy_decision(args["expenseId"], args["action"], args.get("justification"))
            return "ok"
        if name == "exclude_expense":
            cm.set_excluded(args["expenseId"], True, args.get("reason") or "Excluded by employee")
            return "ok"
    except cm.ClaimError as e:
        return f"error: {e}"
    return "error: unknown tool"


def llm_turn(claim_id: str, text: str, snap: dict) -> str:
    policy = load_policy()
    tools = _tools(policy)
    history = [m for m in db.list_messages(claim_id, limit=14) if m.get("role") in ("user", "assistant")][:-1]
    transcript = "\n".join(f"{m['role']}: {m['text']}" for m in history[-12:])
    messages: list = [{"role": "user", "content": [
        {"type": "text", "text": f"<claim_state>\n{_state_for_llm(snap)}\n</claim_state>"},
        {"type": "text", "text": f"<recent_conversation>\n{transcript}\n</recent_conversation>"},
        {"type": "text", "text": f"Employee's new message: {text}"},
    ]}]
    for _ in range(6):
        response = tool_turn(CHAT_SYSTEM, messages, tools)
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            reply = " ".join(b.text for b in response.content if b.type == "text").strip()
            if not reply:
                raise LLMError("empty reply")
            return reply
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for u in uses:
            args = dict(u.input) if isinstance(u.input, dict) else {}
            if u.name == "rename_claim":
                try:
                    cm.update_claim(claim_id, title=str(args.get("title", "")))
                    out = "ok"
                except cm.ClaimError as e:
                    out = f"error: {e}"
            else:
                out = _run_tool(u.name, args)
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out, **({"is_error": True} if out.startswith("error") else {})})
        refreshed = cm.snapshot(claim_id)
        results.append({"type": "text", "text": f"<claim_state_after_tools>\n{_state_for_llm(refreshed)}\n</claim_state_after_tools>"})
        messages.append({"role": "user", "content": results})
    raise LLMError("tool loop did not finish")
