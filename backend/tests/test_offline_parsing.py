"""Offline chat interpreter: amounts and multi-field answers typed as free text."""
import pytest

from app.agent.chat_agent import parse_money


# ------------------------------------------------------------------ parse_money

@pytest.mark.parametrize("text, expected", [
    # dates must not win over the amount
    ("Apollo Pharmacy, 23 Sep, INR, 1234", 1234),
    ("23 Sep, ₹1234", 1234),
    ("23/09/2026 1,234.50", 1234.5),
    ("22 Sep, INR, ₹642", 642),           # the chat placeholder's own format
    ("1234 on 23 Sep", 1234),
    ("Sep 23rd, 2026 — 980", 980),
    ("2026-09-23 Rs. 1,250", 1250),
    ("it was on the 23rd, 455", 455),
    ("paid 642 at 10:42 on Tuesday", 642),
    # plain amounts keep working
    ("total 642", 642),
    ("₹642", 642),
    ("642", 642),
    ("Rs 1,23,456", 123456),
    ("1,850 INR", 1850),
    # quantities and percentages are not money
    ("dinner for 4 people, 1,850", 1850),
    ("18% GST, 590", 590),
])
def test_parse_money_finds_the_amount(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", [
    "23 Sep",                 # only a date
    "no idea",
    "100 or 200",             # two plausible amounts: ask again rather than guess
    "₹100 and ₹200",
    "invoice INV-778",        # a number glued to letters is an identifier
])
def test_parse_money_returns_none_when_unsure(text):
    assert parse_money(text) is None


def test_currency_words_inside_other_words_are_not_markers():
    assert parse_money("the dinner 2 hours, 900") == 900  # "dinner"/"hours" contain "inr"/"rs"
