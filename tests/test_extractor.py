import json

import pytest
from fakes import ROW, FakeModel, transactions_json

from ledgersync.accounts import choosable
from ledgersync.errors import ModelError, ModelUnavailable
from ledgersync.extractor import ACCOUNT_CHOICES, ExtractedTransactions, TransactionExtractor


def extract(reply, text="x", images=None):
    client = FakeModel([reply])
    return TransactionExtractor(client).extract_accounting_data(text, images=images), client


def test_valid_rows_are_returned_with_model_name():
    result, _ = extract(transactions_json(ROW), "BT Broadband 72.00")
    assert result.success and result.count == 1 and result.model == "fake-model"
    assert result.data[0].amount == 72.0 and result.warnings == []


def test_one_bad_row_does_not_fail_the_batch():
    no_amount = {k: v for k, v in ROW.items() if k != "amount"}
    result, _ = extract(transactions_json(no_amount, ROW, dict(ROW, direction="sideways")))
    assert result.count == 1 and len(result.warnings) == 2
    assert result.warnings[0].startswith("Row 1 skipped (amount:")
    assert result.warnings[1].startswith("Row 3 skipped (direction:")


def test_negative_amounts_are_kept_for_the_ledger_to_read_as_money_out():
    result, _ = extract(transactions_json(dict(ROW, amount=-72.0)))
    assert result.count == 1 and result.data[0].amount == -72.0


def test_invalid_json_is_a_model_error():
    with pytest.raises(ModelError) as info:
        extract("not json")
    assert info.value.code == "ai_output_invalid"


def test_answer_without_a_list_is_a_model_error():
    with pytest.raises(ModelError):
        extract(json.dumps({"rows": "nope"}))


def test_fenced_json_and_bare_lists_are_accepted():
    result, _ = extract("```json\n" + json.dumps([ROW]) + "\n```")
    assert result.count == 1


def test_images_and_schema_are_sent_to_the_model():
    _, client = extract(transactions_json(), None, images=["aGk="])
    assert client.calls[0]["images"] == ["aGk="]
    assert "transactions" in client.calls[0]["schema"]["properties"]


def test_model_failure_raises_instead_of_guessing():
    client = FakeModel([ModelUnavailable("Groq is down")])
    with pytest.raises(ModelUnavailable):
        TransactionExtractor(client).extract_accounting_data("BT Broadband 72.00")


def test_the_model_must_pick_an_account_from_the_chart():
    account = ExtractedTransactions.model_json_schema()["$defs"]["AccountingTransaction"]["properties"]["account"]
    assert account["enum"] == list(ACCOUNT_CHOICES) and "7502 Telephone and Internet" in account["enum"]
    assert not [choice for choice in account["enum"] if choice[:4] in ("1200", "2200", "2201")]


def test_instructions_and_the_document_travel_separately():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system, user = client.calls[0]["messages"]
    assert system["role"] == "system" and "7502 Telephone and Internet" in system["content"]
    assert user == {"role": "user", "content": "<document>\nBT Broadband 72.00\n</document>"}


def test_images_are_announced_in_the_user_message():
    _, client = extract(transactions_json(), None, images=["aGk=", "aGk="])
    assert client.calls[0]["messages"][1]["content"] == "The document is attached as 2 image(s)."


def test_the_business_name_tells_sales_from_purchases():
    client = FakeModel([transactions_json(ROW)])
    TransactionExtractor(client, business_name="Northbridge Consulting Ltd").extract_accounting_data("x")
    assert "An invoice issued by Northbridge Consulting Ltd is a sale" in client.calls[0]["messages"][0]["content"]


def test_rows_carry_direction_and_printed_vat():
    result, _ = extract(transactions_json(dict(ROW, direction="in", vat=12.0)))
    assert (result.data[0].direction, result.data[0].vat) == ("in", 12.0)


def test_the_instructions_spell_out_every_field_of_the_answer():
    # Groq enforces the schema but does not show it to the model: without the fields spelled out
    # in the prompt, it answered {"transactions": []} for plain bank CSVs.
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    fields = ExtractedTransactions.model_json_schema()["$defs"]["AccountingTransaction"]["properties"]
    assert fields and [name for name in fields if f'"{name}"' not in system] == []


def test_a_paid_invoice_is_still_one_transaction():
    # An invoice ending "Less Amount Paid <total> / AMOUNT DUE GBP 0.00", with payment instructions
    # below, came back with no transactions: the model read "nothing due" as "nothing to record".
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert "whether it is still to be paid or already paid" in system
    assert "an amount due of 0.00 means it has been paid, not that there is nothing to record" in system


def test_a_receipt_gives_one_transaction_per_account():
    # A note "Breakfast £20 / Travelling charges £50" holds two kinds of expense: one row per account.
    # Splitting must not lose or guess VAT, so a single VAT total that cannot be divided keeps one row.
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert "one transaction per account, not per item" in system
    assert "a single VAT total that cannot be divided" in system and "set mixed_items to true" in system
    assert "give that printed total as document_total" in system
    assert "and its printed VAT total as document_vat" in system


def test_rows_carry_the_document_totals_and_the_mixed_items_flag():
    result, _ = extract(transactions_json(dict(ROW, document_total=72.0, document_vat=12.0, mixed_items=None)))
    row = result.data[0]
    assert (row.document_total, row.document_vat, row.mixed_items) == (72.0, 12.0, False)


def test_the_prompt_defines_every_account_it_offers():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert [a.code for a in choosable() if f"{a.code} {a.name}: {a.definition}" not in system] == []
