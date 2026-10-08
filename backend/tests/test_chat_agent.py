"""chat_agent.py: routing, parsing, every answer kind, and the Claude tool loop with the API mocked."""
from types import SimpleNamespace

from conftest import demo_claim, expense_by_merchant, question

from app import db
from app.agent import chat_agent
from app.agent import claim_manager as cm
from app.agent.llm import LLMError
from app.settings import get_settings


def say(client, cid, text=None, **kw):
    r = client.post(f"/api/claims/{cid}/chat", json={"message": text, **kw})
    assert r.status_code == 200, r.text
    return r.json()["reply"]["text"]


def snap_of(client, cid):
    return client.get(f"/api/claims/{cid}").json()


def enable_ai(monkeypatch):
    """Switch the chat to the Claude path — only after the demo receipts were processed offline."""
    monkeypatch.setattr(type(get_settings()), "ai_enabled", property(lambda self: True))


# ------------------------------------------------------------------ pure helpers

def test_route_picks_the_question_the_text_answers(client):
    _, snap = demo_claim(client)
    open_q = snap["questions"]
    pick = lambda text: chat_agent.route(text, open_q, snap)["kind"]  # noqa: E731
    assert pick("yes it's the same ride, remove it") == "duplicate"
    assert pick("cap it please") == "over_limit"
    assert pick("client dinner with Asha Rao") == "meal_purpose"
    assert pick("It was on Wednesday") == "fields"
    assert pick("the 23rd") == "fields"
    assert pick("the Barrels bill was a team thing") == "meal_purpose"  # merchant mention wins
    assert pick("hmm") == "fields"  # nothing fits: the field question's weak prior wins


def test_parse_date_edge_cases():
    trip = {"start": "2026-09-21", "end": "2026-09-24"}
    none = {"start": None}
    assert chat_agent.parse_date("2026-09-23", none) == "2026-09-23"
    assert chat_agent.parse_date("Sep 22, 2026", none) == "2026-09-22"
    assert chat_agent.parse_date("on 22/9/26", none) == "2026-09-22"
    assert chat_agent.parse_date("31 Feb", trip) is None  # impossible date
    assert chat_agent.parse_date("tue", trip) == "2026-09-22"
    assert chat_agent.parse_date("Tuesday", none) is None  # a weekday needs a trip window
    assert chat_agent.parse_date("Sunday", trip) is None  # no Sunday inside 21–24 Sep
    assert chat_agent.parse_date("", trip) is None


def test_attendees_and_purpose_helpers():
    assert chat_agent._attendees("Dinner with the client: Priya Shah and Rahul.") == "Priya Shah and Rahul"
    assert chat_agent._attendees("team lunch with Asha, Ravi & Meera") == "Asha, Ravi & Meera"
    assert chat_agent._attendees("lunch alone") is None
    assert chat_agent._attendees("with me") is None
    assert chat_agent._clean_purpose("it was the quarterly review.") == "Quarterly review"
    assert chat_agent._meal_choice("just me, my own dinner") == "personal"
    assert chat_agent._meal_choice("with the customers") == "client"
    assert chat_agent._meal_choice("colleagues offsite") == "team"
    assert chat_agent.parse_money("it was Rs. 1,250.50") == 1250.5 and chat_agent.parse_money("no idea") is None


# ------------------------------------------------------------------ answer kinds

def test_over_limit_cap_by_free_text(client):
    cid, _ = demo_claim(client)
    reply = say(client, cid, "just cap it at the policy amount")
    assert reply.startswith("Done — I'll claim The Orchid Grand up to the policy limit (₹22,500)")
    hotel = expense_by_merchant(snap_of(client, cid), "The Orchid Grand")
    assert hotel["reimbursableAmount"] == 22500 and hotel["policyDecision"]["type"] == "cap"


def test_over_limit_justification_paths(client):
    cid, snap = demo_claim(client)
    q = question(snap, "over_limit")
    # A full sentence is taken as the justification directly
    reply = say(client, cid, "Conference week, policy-rate hotels were sold out", questionId=q["id"])
    assert "justification for the approver" in reply
    assert expense_by_merchant(snap_of(client, cid), "The Orchid Grand")["status"] == "WARNING"

    cid, snap = demo_claim(client)
    q = question(snap, "over_limit")
    assert "one-line justification" in say(client, cid, questionId=q["id"], option="justify")
    jq = question(snap_of(client, cid), "justification")
    assert "short sentence is enough" in say(client, cid, "ok", questionId=jq["id"])
    assert "justification attached" in say(client, cid, "Client asked us to stay near their office", questionId=jq["id"])
    hotel = expense_by_merchant(snap_of(client, cid), "The Orchid Grand")
    assert hotel["policyDecision"]["justification"] == "Client asked us to stay near their office"


def test_trip_purpose_question_after_the_booking_purpose_is_cleared(client):
    cid, snap = demo_claim(client)
    air = expense_by_merchant(snap, "AirVista")
    client.patch(f"/api/expenses/{air['id']}", json={"businessPurpose": None})
    q = question(snap_of(client, cid), "trip_purpose")
    assert len(q["expenseIds"]) >= 8
    assert "few words" in say(client, cid, "x", questionId=q["id"])
    reply = say(client, cid, "Quarterly business review with Northwind Retail", questionId=q["id"])
    assert f"for {len(q['expenseIds'])} trip expenses" in reply
    snap = snap_of(client, cid)
    assert all(e["fields"]["businessPurpose"]["source"] == "user" for e in snap["expenses"] if e["id"] in q["expenseIds"])


def test_meal_purpose_other_team_and_attendees_follow_up(client):
    cid, snap = demo_claim(client)
    q = question(snap, "meal_purpose")
    assert "in a few words" in say(client, cid, "hmm", questionId=q["id"])
    assert "recorded that as the purpose" in say(client, cid, "Farewell for an intern", questionId=q["id"])

    cid, snap = demo_claim(client)
    q = question(snap, "meal_purpose")
    reply = say(client, cid, "team dinner with Asha and Ravi", questionId=q["id"])
    assert "team meal" in reply
    assert expense_by_merchant(snap_of(client, cid), "Hops")["fields"]["attendees"]["value"] == "Asha and Ravi"

    cid, snap = demo_claim(client)
    q = question(snap, "meal_purpose")
    reply = say(client, cid, questionId=q["id"], option="client")
    assert "Policy also needs the attendees" in reply and "within the ₹5,000 per-event limit" in reply
    aq = question(snap_of(client, cid), "attendees")
    assert "Who attended?" in say(client, cid, "?", questionId=aq["id"])
    assert "Priya Shah" in say(client, cid, "Priya Shah and Rahul Mehta", questionId=aq["id"])
    assert expense_by_merchant(snap_of(client, cid), "Hops")["status"] == "COMPLETE"


def test_fields_answers_money_confirm_and_unparseable(client):
    cid, snap = demo_claim(client)
    q = question(snap, "fields")  # the parking slip's missing date
    assert "couldn't find the expense date" in say(client, cid, "no clue", questionId=q["id"])
    assert "expense date 23 Sep" in say(client, cid, "it was the 23rd", questionId=q["id"])

    coffee = expense_by_merchant(snap, "Bean Street")
    client.patch(f"/api/expenses/{coffee['id']}", json={"amount": None})
    q = next(q for q in snap_of(client, cid)["questions"] if q["expenseIds"] == [coffee["id"]])
    assert q["fields"] == ["total"] and "upload a clearer copy" in q["text"]
    assert "total amount ₹420" in say(client, cid, "Rs. 420", questionId=q["id"])

    # A low-confidence value is confirmed with one tap
    exp = db.get_expense(coffee["id"])
    exp["fields"]["merchant"].update({"confidence": 0.5, "source": "extracted"})
    db.save_expense(exp)
    cm.revalidate(cid)
    q = next(q for q in snap_of(client, cid)["questions"] if q["expenseIds"] == [coffee["id"]])
    assert q["options"][0]["value"] == "confirm"
    assert "Thanks for confirming the merchant" in say(client, cid, questionId=q["id"], option="confirm")
    assert db.get_expense(coffee["id"])["fields"]["merchant"]["source"] == "user"


def test_category_answers(client):
    cid, snap = demo_claim(client)
    lunch = expense_by_merchant(snap, "Udupi")
    exp = db.get_expense(lunch["id"])
    exp["category"].update({"confidence": 0.4, "source": "classified"})
    db.save_expense(exp)
    cm.revalidate(cid)
    q = question(snap_of(client, cid), "category")
    assert "Which category fits best" in say(client, cid, "no idea", questionId=q["id"])
    assert "now categorised as Meals" in say(client, cid, "it's meals", questionId=q["id"])


def test_duplicate_answers_and_unclear_reply(client):
    cid, snap = demo_claim(client)
    q = question(snap, "duplicate")
    assert "Is it a duplicate" in say(client, cid, "hmm what", questionId=q["id"])
    assert "keeping both" in say(client, cid, "no, they're both legit", questionId=q["id"])
    snap = snap_of(client, cid)
    assert not any(e["status"] == "POSSIBLE_DUPLICATE" for e in snap["expenses"])


def test_status_submit_answered_and_frozen_claims(client):
    cid, snap = demo_claim(client)
    assert "need info" in say(client, cid, "what's left?")
    q = question(snap, "duplicate")
    say(client, cid, questionId=q["id"], option="duplicate")
    assert "already been answered" in say(client, cid, questionId=q["id"], option="duplicate")
    assert client.post(f"/api/claims/{cid}/chat", json={"message": "   "}).status_code == 400

    from conftest import make_ready
    make_ready(client, cid)
    assert "Review & Submit" in say(client, cid, "submit it for me")
    assert "READY TO SUBMIT" in say(client, cid, "status")
    client.post(f"/api/claims/{cid}/submit", json={"confirm": True})
    assert "can't be changed" in say(client, cid, "change the hotel")


# ------------------------------------------------------------------ Claude tool loop

def _turn(*blocks, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


def _use(i, name, **args):
    return SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def test_llm_turn_reports_tool_errors_and_renames(monkeypatch, client):
    cid, snap = demo_claim(client)
    enable_ai(monkeypatch)
    hotel = expense_by_merchant(snap, "The Orchid Grand")
    dinner = expense_by_merchant(snap, "Hops")
    turns = iter([
        _turn(_use(1, "resolve_over_limit", expenseId=hotel["id"], action="justify", justification=""),
              _use(2, "set_expense_fields", expenseId="EXP-missing", updates=[{"field": "total", "value": "5"}]),
              _use(3, "rename_claim", title="Northwind renewal trip"),
              _use(4, "exclude_expense", expenseId=dinner["id"], reason="Personal dinner"),
              _use(5, "no_such_tool")),
        _turn(_use(6, "confirm_field", expenseId=hotel["id"], field="merchant"),
              _use(7, "set_category", expenseId=dinner["id"], category="MEALS")),
        _turn(_text("Renamed the claim and excluded the personal dinner. What's the justification for the hotel?"), stop="end_turn"),
    ])
    seen = []
    monkeypatch.setattr(chat_agent, "tool_turn", lambda system, messages, tools, max_tokens=4000: (seen.append(messages) or next(turns)))
    reply = say(client, cid, "rename it to Northwind renewal trip, the dinner was personal, and keep the hotel")
    assert reply.startswith("Renamed the claim")
    # The loop appends to one transcript; collect every tool_result it sent back to the model.
    results = {b["tool_use_id"]: b for m in seen[-1] if m["role"] == "user" for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"}
    assert results["t1"]["is_error"] and "justification sentence" in results["t1"]["content"]
    assert results["t2"]["is_error"] and "not found" in results["t2"]["content"]
    assert results["t3"]["content"] == "ok" and results["t4"]["content"] == "ok"
    assert results["t5"]["content"] == "error: unknown tool"
    snap = snap_of(client, cid)
    assert snap["claim"]["title"] == "Northwind renewal trip"
    assert expense_by_merchant(snap, "Hops")["excluded"]
    assert expense_by_merchant(snap, "The Orchid Grand")["fields"]["merchant"]["source"] == "user"


def test_llm_failure_falls_back_to_deterministic_interpreter(monkeypatch, client):
    cid, _ = demo_claim(client)
    cid2, snap2 = demo_claim(client)
    enable_ai(monkeypatch)

    def broken(*a, **kw):
        raise LLMError("The AI service timed out")

    monkeypatch.setattr(chat_agent, "tool_turn", broken)
    reply = say(client, cid, "the parking was on the 23rd")
    assert reply == "Updated Tech Park One Visitor Parking: expense date 23 Sep."

    # An empty model reply or a loop that never ends also falls back
    monkeypatch.setattr(chat_agent, "tool_turn", lambda *a, **kw: _turn(stop="end_turn"))
    assert "Removed the duplicate" in say(client, cid, "yes the zipcab one is the same ride, remove it")
    hotel = expense_by_merchant(snap2, "The Orchid Grand")
    monkeypatch.setattr(chat_agent, "tool_turn",
                        lambda *a, **kw: _turn(_use(1, "confirm_field", expenseId=hotel["id"], field="merchant")))
    assert "policy limit" in say(client, cid2, "cap the hotel")


def test_processing_claim_tells_the_employee_to_wait(monkeypatch, client):
    cid = client.post("/api/claims").json()["claim"]["id"]
    claim = db.get_claim(cid)
    claim["status"] = "PROCESSING"
    db.save_claim(claim)
    assert "still reading your documents" in say(client, cid, "hello?")
