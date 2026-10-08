"""planner.py and enrich.py on in-memory expenses (no database), mirroring the steps revalidate() runs."""
from app.agent import enrich, planner
from app.agent.fields import FIELD_DEFS, make_field
from app.agent.policy import evaluate
from app.settings import get_settings, load_policy

POLICY = load_policy()


def exp(eid, cat, seq=0, cat_conf=0.97, **values):
    fields = {name: make_field() for name in FIELD_DEFS}
    fields["currency"] = make_field("INR", 0.99, "extracted")
    for k, v in values.items():
        if isinstance(v, tuple):  # (value, confidence)
            fields[k] = make_field(v[0], v[1], "extracted")
        else:
            fields[k] = make_field(v, 0.97, "extracted")
    return {"id": eid, "seq": seq, "category": {"value": cat, "confidence": cat_conf, "source": "classified"},
            "fields": fields, "policyDecision": None, "duplicateResolutions": {}}


def run(expenses, claim=None):
    """The inference → grouping → policy → gaps → plan part of ClaimManager.revalidate()."""
    claim = claim or {}
    by_id = {e["id"]: e for e in expenses}
    for e in expenses:
        e.pop("_inferenceBlocked", None)
        enrich.clear_derived(e)
        enrich.derive_fields(e)
    groups = enrich.build_groups(expenses, claim)
    enrich.infer_purposes(groups, by_id, POLICY)
    results = evaluate(expenses, POLICY, get_settings().today())
    for e in expenses:
        e["policyResults"] = results.get(e["id"], [])
        e.setdefault("duplicate", None)
        e["_gaps"], e["fieldStates"] = planner.detect_gaps(e, POLICY)
    return groups, planner.plan(expenses, groups, POLICY)


def trip(purpose=None):
    air_vals = dict(merchant="AirVista", total=11240, expenseDate="2026-09-21", travelDate="2026-09-21",
                    returnDate="2026-09-24", destination="Bengaluru (BLR)", invoiceNumber="AV-1")
    if purpose:
        air_vals["businessPurpose"] = purpose
    return exp("air", "AIR_TRAVEL", 0, **air_vals)


# ------------------------------------------------------------------ enrich

def test_purpose_is_inherited_from_the_booking_within_the_trip():
    coffee = exp("coffee", "MEALS", 1, merchant="Cafe", total=420, expenseDate="2026-09-23")
    taxi = exp("taxi", "TAXI", 2, merchant="Cab", total=642, expenseDate="2026-09-22", origin="Hotel", destination="Office")
    own = exp("own", "MEALS", 3, merchant="Bakery", total=300, expenseDate="2026-09-22")
    own["fields"]["businessPurpose"] = make_field("Snacks for the workshop", 1.0, "user")
    groups, questions = run([trip("Customer meetings - Northwind renewal"), coffee, taxi, own])
    for e in (coffee, taxi):
        f = e["fields"]["businessPurpose"]
        assert f["value"] == "Customer meetings - Northwind renewal" and f["source"] == "inferred" and f["derived"]
        assert f["confidence"] == 0.8 and "AirVista flight booking" in f["reason"]
    assert own["fields"]["businessPurpose"]["value"] == "Snacks for the workshop"  # user value never overwritten
    assert not questions
    assert groups[0]["kind"] == "trip" and groups[0]["purpose"].startswith("Customer meetings")


def test_inheritance_blocked_for_large_or_multi_cover_meals():
    big = exp("big", "MEALS", 1, merchant="Brewpub", total=1850, expenseDate="2026-09-22")
    crowd = exp("crowd", "MEALS", 2, merchant="Diner", total=600, expenseDate="2026-09-23", covers=4)
    run([trip("Customer meetings"), big, crowd])
    assert big["fields"]["businessPurpose"]["value"] is None
    assert "above ₹1,000" in big["_inferenceBlocked"][0]
    assert crowd["fields"]["businessPurpose"]["value"] is None
    assert "4 guests" in crowd["_inferenceBlocked"][0]


def test_nights_and_invoice_date_derived_from_hotel_stay():
    hotel = exp("hotel", "HOTEL", merchant="Hotel", total=20000, checkIn="2026-09-21", checkOut="2026-09-24",
                merchantAddress="ITPL Main Road, Whitefield, Bengaluru")
    enrich.derive_fields(hotel)
    assert hotel["fields"]["nights"]["value"] == 3 and hotel["fields"]["nights"]["source"] == "inferred"
    assert hotel["fields"]["expenseDate"]["value"] == "2026-09-24"
    assert hotel["fields"]["city"]["value"] == "Bengaluru"
    enrich.clear_derived(hotel)
    assert hotel["fields"]["nights"]["value"] is None and hotel["fields"]["expenseDate"]["value"] is None


def test_trip_grouping_keeps_recurring_bills_out_and_places_undated_local_expenses():
    hotel = exp("hotel", "HOTEL", 1, merchant="Hotel", total=20000, expenseDate="2026-09-24", checkIn="2026-09-21",
                checkOut="2026-09-24", city="Bengaluru")
    phone = exp("phone", "MOBILE_INTERNET", 2, merchant="Telco", total=999, expenseDate="2026-09-22")
    parking = exp("park", "PARKING", 3, merchant="Visitor Parking", total=150, merchantAddress="Whitefield, Bengaluru")
    elsewhere = exp("far", "TAXI", 4, merchant="Cab", total=200, expenseDate="2026-08-02")
    groups, _ = run([trip("Customer meetings"), hotel, phone, parking, elsewhere])
    by_id = {g["id"]: g for g in groups}
    trip_group = by_id["trip-bengaluru-2026-09-21"]
    assert set(trip_group["expenseIds"]) == {"air", "hotel", "park"}  # flight and hotel merged into one trip
    assert phone["groupId"] == "month-2026-09" and elsewhere["groupId"] == "month-2026-08"
    assert enrich.group_title(trip_group, "Customer meetings") == "Bengaluru Customer Visit"
    assert enrich.group_title(trip_group, "Retail tech summit") == "Bengaluru Training Visit"
    assert enrich.group_title(trip_group, None) == "Bengaluru Business Trip"
    assert enrich.group_title(by_id["month-2026-09"], None) == "September 2026 expenses"


def test_group_override_moves_an_expense():
    taxi = exp("taxi", "TAXI", 1, merchant="Cab", total=300, expenseDate="2026-09-22", origin="A", destination="B")
    groups, _ = run([trip("Customer meetings"), taxi], claim={"groupOverrides": {"taxi": "custom-team-offsite"}})
    custom = next(g for g in groups if g["id"] == "custom-team-offsite")
    assert custom["expenseIds"] == ["taxi"] and enrich.group_title(custom, None) == "Team Offsite"


def test_normalise_city_aliases():
    assert enrich.normalise_city("Kempegowda Intl Airport, BLR") == "Bengaluru"
    assert enrich.normalise_city("Andheri, Bombay") == "Mumbai"
    assert enrich.normalise_city("Somewhere else") is None and enrich.normalise_city(None) is None


# ------------------------------------------------------------------ planner

def test_one_trip_purpose_question_covers_every_trip_expense():
    taxi = exp("taxi", "TAXI", 1, merchant="Cab", total=642, expenseDate="2026-09-22", origin="A", destination="B")
    coffee = exp("coffee", "MEALS", 2, merchant="Cafe", total=420, expenseDate="2026-09-23")
    _, questions = run([trip(), taxi, coffee])
    assert [q["kind"] for q in questions] == ["trip_purpose"]
    q = questions[0]
    assert set(q["expenseIds"]) == {"air", "taxi", "coffee"} and q["id"] == "trip_purpose:trip-bengaluru-2026-09-21"
    assert "One answer covers all 3 trip expenses" in q["text"]


def test_meal_purpose_question_versus_attendees_question():
    dinner = exp("dinner", "MEALS", 1, merchant="Brewpub", total=1850, expenseDate="2026-09-22", covers=4)
    client = exp("client", "CLIENT_ENTERTAINMENT", 2, merchant="Grill", total=3000, expenseDate="2026-09-23",
                 businessPurpose="Client dinner")
    _, questions = run([trip("Customer meetings"), dinner, client])
    meal = next(q for q in questions if q["kind"] == "meal_purpose")
    assert meal["expenseIds"] == ["dinner"] and "a bill for 4 guests could be client entertainment" in meal["text"]
    assert [o["value"] for o in meal["options"]] == ["client", "team", "personal", "other"]
    att = next(q for q in questions if q["kind"] == "attendees")
    assert att["fields"] == ["attendees"] and att["text"].startswith("For the ₹3,000 Grill bill")
    # no over-limit question for the dinner yet — its category may still change
    assert not any(q["kind"] == "over_limit" for q in questions)


def test_low_confidence_category_offers_ranked_options():
    odd = exp("odd", "OFFICE_SUPPLIES", cat_conf=0.5, merchant="Shop", total=300, expenseDate="2026-09-05",
              businessPurpose="Cables for the demo")
    odd["_categoryRanking"] = ["SOFTWARE_SUBSCRIPTION", "OFFICE_SUPPLIES", "FUEL", "TOLL"]
    _, questions = run([odd])
    q = next(q for q in questions if q["kind"] == "category")
    assert [o["value"] for o in q["options"]] == ["SOFTWARE_SUBSCRIPTION", "OFFICE_SUPPLIES", "FUEL", "MISCELLANEOUS"]
    assert "50% confident" in q["text"] and "Office Supplies" in q["text"]


def test_smudged_date_inside_a_trip_offers_day_chips():
    parking = exp("park", "PARKING", 1, merchant="Visitor Parking", total=150, merchantAddress="Whitefield, Bengaluru")
    hotel = exp("hotel", "HOTEL", 2, merchant="Hotel", total=20000, expenseDate="2026-09-24", checkIn="2026-09-21",
                checkOut="2026-09-24", city="Bengaluru", invoiceNumber="H-1")
    _, questions = run([trip("Customer meetings"), hotel, parking])
    q = next(q for q in questions if q["kind"] == "fields")
    assert q["fields"] == ["expenseDate"] and "Bengaluru trip (21 Sep–24 Sep). Which day?" in q["text"]
    assert [o["value"] for o in q["options"]] == ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
    assert q["options"][0]["label"] == "Mon 21 Sep"


def test_low_confidence_value_asks_for_confirmation_and_missing_total_asks_for_upload():
    blurry = exp("b", "TAXI", 1, merchant="Cab", total=(786, 0.55), expenseDate="2026-08-21", origin="A", destination="B",
                 businessPurpose="Client visit")
    unreadable = exp("u", "MISCELLANEOUS", 2, description="Something")
    _, questions = run([blurry, unreadable])
    confirm = next(q for q in questions if q["expenseIds"] == ["b"])
    assert confirm["options"] == [{"label": "Yes, ₹786 is correct", "value": "confirm"}]
    missing = next(q for q in questions if q["expenseIds"] == ["u"] and q["kind"] == "fields")
    assert "couldn't reliably read the total" in missing["text"] and missing["text"].endswith("upload a clearer copy.")


def test_policy_questions_are_deferred_while_gaps_remain_then_asked():
    dinner = exp("dinner", "MEALS", 1, merchant="Brewpub", total=1850, expenseDate="2026-08-22")
    _, questions = run([dinner])
    assert [q["kind"] for q in questions] == ["meal_purpose"]
    dinner["fields"]["businessPurpose"] = make_field("Team dinner", 1.0, "user")
    _, questions = run([dinner])
    q = questions[0]
    assert q["kind"] == "over_limit" and q["options"][0]["label"] == "Cap at the limit (₹1,500)"
    assert "Excess ₹350" in q["text"]
    dinner["policyDecision"] = {"type": "justify", "justification": None}
    _, questions = run([dinner])
    assert [q["kind"] for q in questions] == ["over_limit", "justification"]


def test_future_dated_expense_asks_for_the_correct_date_and_duplicates_come_last():
    fut = exp("fut", "TAXI", 1, merchant="Cab", total=300, expenseDate="2027-01-05", origin="A", destination="B",
              businessPurpose="Client visit")
    dup = exp("dup", "TAXI", 2, merchant="Cab", total=300, expenseDate="2026-09-05", origin="A", destination="B",
              businessPurpose="Client visit")
    run([fut, dup])
    dup["duplicate"] = {"ofExpenseId": "fut", "score": 0.96, "signals": ["invoice number", "amount"], "explanation": "x"}
    _, questions = run([fut, dup])
    assert questions[0]["id"] == "invalid:fut:expenseDate" and "after today" in questions[0]["text"]
    assert questions[-1]["kind"] == "duplicate" and "96% match" in questions[-1]["text"]
    assert "invoice number and amount match" in questions[-1]["text"]
