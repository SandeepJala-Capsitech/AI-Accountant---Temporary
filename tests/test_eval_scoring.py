from decimal import Decimal

import pytest

from eval_scoring import adapt_rows, expected_rows, match_rows, normalise_date, score_case, summarise

EXPECTED = expected_rows([{"date": "2026-09-03", "direction": "out", "gross": "12.50", "vat": None,
                           "account_code": "7406", "description": "Tesco Express"}])


def api_row(gross, direction="out", date="2026-09-03", description="x"):
    """An API transaction without account code or VAT, so neither is scored."""
    return {"description": description, "date": date, "direction": direction, "gross": f"{gross:.2f}"}


def test_rows_are_adapted_and_missing_codes_are_not_scored():
    rows = adapt_rows({"transactions": [api_row(72.0), api_row(3600, "in", date="02/09/2026")]})
    assert [(r["direction"], r["gross"], r["date"]) for r in rows] == [
        ("out", Decimal("72.00"), "2026-09-03"), ("in", Decimal("3600.00"), "2026-09-02")]
    assert rows[0]["has_account"] is False and rows[0]["has_vat"] is False


def test_later_results_with_codes_are_used_as_is():
    rows = adapt_rows({"transactions": [{"date": "2026-09-03", "direction": "out", "gross": "12.50",
                                         "vat": "2.08", "account_code": "7406", "description": "Tesco"}]})
    assert (rows[0]["gross"], rows[0]["vat"], rows[0]["account_code"]) == (Decimal("12.50"), Decimal("2.08"), "7406")
    assert rows[0]["has_account"] and rows[0]["has_vat"]


@pytest.mark.parametrize("raw, iso", [
    ("2026-09-03", "2026-09-03"), ("03/09/2026", "2026-09-03"), ("2026-09-03 00:00:00", "2026-09-03"),
    ("3 Sept", None), (None, None),
])
def test_dates_are_normalised_day_first(raw, iso):
    assert normalise_date(raw) == iso


def test_the_vat_the_ledger_books_is_scored_not_the_vat_on_the_document():
    # Phase 3+: "vat" is what the document showed (null when not shown); "vat_posted" is booked.
    [row] = adapt_rows({"transactions": [{"date": "2026-09-03", "direction": "out", "gross": "12.00",
                                          "vat": None, "vat_posted": "2.00", "account_code": "7502"}]})
    assert row["vat"] == Decimal("2.00") and row["has_vat"]


def test_receipt_over_extraction_is_scored_honestly():
    # Phase 1 probe: one £12.50 purchase came back as six rows (items and TOTAL as money in,
    # CASH and CHANGE as money out).
    predicted = adapt_rows({"transactions": [api_row(3.5, "in"), api_row(2.8, "in"), api_row(6.2, "in"),
                                             api_row(12.5, "in", description="TOTAL"), api_row(20.0), api_row(7.5)]})
    result = score_case(EXPECTED, predicted)
    assert (result["expected"], result["predicted"], result["matched"]) == (1, 6, 1)
    assert result["checks"]["amount"] == [1, 1]
    assert result["checks"]["direction"] == [0, 1]
    assert result["checks"]["account"] == [0, 0]      # not scored: these rows have no account codes
    assert result["exact"] is False


def test_wrong_amount_still_pairs_on_date_and_description():
    result = score_case(EXPECTED, adapt_rows({"transactions": [api_row(125.0, description="Tesco Express store")]}))
    assert result["matched"] == 1 and result["checks"]["amount"] == [0, 1]


def test_unrelated_rows_do_not_pair():
    predicted = adapt_rows({"transactions": [api_row(99.0, date="2026-10-01", description="Other")]})
    assert score_case(EXPECTED, predicted)["matched"] == 0


def test_perfect_extraction_is_exact():
    predicted = adapt_rows({"transactions": [{"date": "2026-09-03", "direction": "out", "gross": "12.50",
                                              "vat": None, "account_code": "7406", "description": "Tesco"}]})
    result = score_case(EXPECTED, predicted)
    assert result["exact"] is True and result["checks"]["account"] == [1, 1]


def test_each_expected_row_pairs_with_at_most_one_prediction():
    row = {"date": "2026-09-01", "direction": "out", "gross": "72.00", "vat": None,
           "account_code": "7502", "description": "BT"}
    predicted = adapt_rows({"transactions": [api_row(72.0, date="2026-09-01", description="BT")] * 2})
    assert match_rows(expected_rows([row]), predicted) == [(0, 0)]
    assert score_case(expected_rows([row]), predicted)["exact"] is False


def test_net_instead_of_gross_counts_as_a_wrong_amount():
    # Baseline: both invoice PDFs came back at the net amount on the right date and direction.
    # That must show up as an amount error, not vanish into recall.
    expected = expected_rows([{"date": "2026-09-30", "direction": "in", "gross": "2400.00", "vat": "400.00",
                               "account_code": "4000", "description": "Harbour & Lane Architects LLP"}])
    predicted = adapt_rows({"transactions": [api_row(2000.0, "in", date="2026-09-30",
                                                     description="Strategy consultancy")]})
    result = score_case(expected, predicted)
    assert result["matched"] == 1 and result["checks"]["amount"] == [0, 1] and result["correct"] == 0


def test_correct_rows_counts_fully_right_rows_over_all_expected_rows():
    none = {"amount": [0, 0], "date": [0, 0], "direction": [0, 0], "account": [0, 0], "vat": [0, 0],
            "document_type": [0, 0]}
    s = summarise([{"kind": "table", "expected": 4, "predicted": 1, "matched": 1, "correct": 1, "exact": False,
                    "latency_s": 1.0, "error": None, "tb_balanced": None,
                    "checks": {**none, "amount": [1, 1], "date": [1, 1], "direction": [1, 1]}}])
    assert s["field_accuracy"]["amount"] == 1.0     # every row it found had the right amount...
    assert s["correct_rows"] == 0.25                # ...but only 1 of 4 transactions is right end to end


def test_summary_aggregates_rows_fields_latency_and_errors():
    none = {"amount": [0, 0], "date": [0, 0], "direction": [0, 0], "account": [0, 0], "vat": [0, 0],
            "document_type": [0, 0]}
    cases = [
        {"kind": "text", "expected": 1, "predicted": 6, "matched": 1, "correct": 0, "exact": False,
         "latency_s": 10.0, "error": None, "tb_balanced": False,
         "checks": {**none, "amount": [1, 1], "date": [1, 1], "direction": [0, 1]}},
        {"kind": "table", "expected": 3, "predicted": 0, "matched": 0, "correct": 0, "exact": False,
         "latency_s": 30.0, "error": "ai_offline", "tb_balanced": None, "checks": none},
    ]
    s = summarise(cases)
    assert s["row_precision"] == round(1 / 6, 3) and s["row_recall"] == 0.25
    assert s["field_accuracy"] == {"amount": 1.0, "date": 1.0, "direction": 0.0, "account": None, "vat": None,
                                   "document_type": None}
    assert (s["exact_cases"], s["tb_balanced"]) == (0.0, 0.0)
    assert (s["latency_p50_s"], s["latency_p95_s"]) == (10.0, 30.0)
    assert s["errors"] == {"ai_offline": 1}


def test_the_document_type_is_scored_when_both_sides_have_it():
    expected = expected_rows([{"date": "2026-09-03", "direction": "out", "gross": "12.50", "vat": None,
                               "account_code": "7406", "description": "Tesco", "document_type": "receipt"}])
    right = adapt_rows({"transactions": [dict(api_row(12.50), document_type="receipt")]})
    wrong = adapt_rows({"transactions": [dict(api_row(12.50), document_type="invoice")]})
    assert score_case(expected, right)["checks"]["document_type"] == [1, 1]
    assert score_case(expected, wrong)["checks"]["document_type"] == [0, 1]
    assert score_case(expected, adapt_rows({"transactions": [api_row(12.50)]}))["checks"]["document_type"] == [0, 0]
