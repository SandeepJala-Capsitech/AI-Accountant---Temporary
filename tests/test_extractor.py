import json

import pytest
from fakes import ROW, FakeModel, transactions_json

from ledgersync.accounts import choosable
from ledgersync.errors import ModelError, ModelUnavailable
from ledgersync.extractor import ACCOUNT_CHOICES, ExtractedTransactions, TransactionExtractor
from ledgersync.model_client import ModelAnswer


def extract(reply, text="x", images=None):
    client = FakeModel([reply])
    return TransactionExtractor(client).extract_accounting_data(text, images=images), client


def test_valid_rows_are_returned_with_model_name():
    result, _ = extract(transactions_json(ROW), "BT Broadband 72.00")
    assert result.count == 1 and result.model == "fake-model"
    assert result.data[0].amount == 72.0 and result.warnings == []


def test_a_price_before_vat_and_not_a_vat_invoice_are_read():
    # The Amazon receipt prints £14.99 and £17.99 but no VAT, and says "This is not a VAT invoice".
    result, _ = extract(transactions_json(dict(ROW, amount=17.99, document_total=17.99, document_net=14.99,
                                               not_vat_invoice=True)))
    assert (result.data[0].document_net, result.data[0].not_vat_invoice) == (14.99, True)
    result, _ = extract(transactions_json(ROW))
    assert (result.data[0].document_net, result.data[0].not_vat_invoice) == (None, False)


def test_the_model_name_says_which_host_read_the_document():
    # Kept with the upload: a host that misreads documents can be found afterwards.
    result, _ = extract(ModelAnswer(transactions_json(ROW), host="DeepInfra"))
    assert result.model == "fake-model via DeepInfra"


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


def test_every_line_of_an_expense_claim_is_read():
    # Jenny's claim had a second Southgate car park line, queried in its notes; the model left it out.
    _, client = extract(transactions_json(ROW), "x")
    system = client.calls[0]["messages"][0]["content"]
    assert "List every line, even when its payee or amount repeats another line or a note queries it" in system


def test_an_expense_claim_gives_one_transaction_per_expense_line():
    # Matt's claim of 24 expenses came back as one row: the model added the lines up by account, got three
    # sums wrong, and the rows' VAT no longer matched the printed total, so the ledger kept the claim whole.
    # Each line of a claim is its own expense: copied as printed, never added together.
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert "An expense claim or expense report lists separate expenses" in system
    assert "one transaction per expense line" in system and "Never add expense lines together" in system
    assert "the claim's grand total as document_total" in system and "document_vat is null" in system


def test_rows_carry_the_document_totals_and_the_mixed_items_flag():
    result, _ = extract(transactions_json(dict(ROW, document_total=72.0, document_vat=12.0, mixed_items=None)))
    row = result.data[0]
    assert (row.document_total, row.document_vat, row.mixed_items) == (72.0, 12.0, False)


def test_the_prompt_defines_every_account_it_offers():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert [a.code for a in choosable() if f"{a.code} {a.name}: {a.definition}" not in system] == []


def test_the_instructions_name_every_document_type():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert all(f'"{kind}"' in system for kind in ("receipt", "invoice", "expense_claim", "statement", "quote",
                                                  "pro_forma", "purchase_order", "remittance_advice",
                                                  "supplier_statement"))
    assert "a credit note is an invoice with the money going the other way" in system
    assert "The counterparty of every line is the person claiming, not the shop" in system


def test_the_instructions_explain_a_letting_agents_statement():
    # Read as a bank statement, an agent's statement booked its rent as if it had reached the bank.
    _, client = extract(transactions_json(ROW), "x")
    system = client.calls[0]["messages"][0]["content"]
    assert '"agent_statement"' in system and "the net it paid over is not a transaction" in system
    assert '"agent"' in system


def test_a_cash_machine_withdrawal_is_cash_not_a_purchase():
    # "LOYD STRATFORD WESTFLD (includes fee of GBP 1.00)", a cash machine, was read as a car park, under Travel.
    _, client = extract(transactions_json(ROW), "x")
    system = client.calls[0]["messages"][0]["content"]
    assert "A card line at a cash machine is cash taken out: 1230 Petty Cash" in system


def test_an_agents_name_comes_once_with_the_answer():
    result, _ = extract(json.dumps({"transactions": [dict(ROW, document_type="agent_statement")], "agent": " R+R PR Ltd "}))
    assert (result.data[0].document_type, result.agent) == ("agent_statement", "R+R PR Ltd")


def test_rows_carry_the_document_type_and_counterparty():
    result, _ = extract(transactions_json(dict(ROW, document_type="Invoice ", counterparty="BT plc")))
    assert (result.data[0].document_type, result.data[0].counterparty) == ("invoice", "BT plc")


def test_rows_carry_the_number_printed_on_their_document():
    result, _ = extract(transactions_json(dict(ROW, document_number="SC-2026-00718"), dict(ROW, document_number=3318),
                                          ROW))
    assert [t.document_number for t in result.data] == ["SC-2026-00718", "3318", None]


def test_a_row_without_a_document_type_is_a_receipt():
    result, _ = extract(transactions_json(ROW))
    assert (result.data[0].document_type, result.data[0].counterparty) == ("receipt", None)


def test_the_prompt_names_a_limited_company_and_offers_its_directors_loan_account():
    client = FakeModel([transactions_json(ROW)])
    TransactionExtractor(client, business_name="Business Cube Ltd",
                         business_type="limited_company").extract_accounting_data("x")
    system = client.calls[0]["messages"][0]["content"]
    assert "You keep the books of Business Cube Ltd, a UK limited company." in system
    assert "2250 Director's Loan Account:" in system and "3260 Drawings:" not in system


def test_a_sole_trader_is_offered_drawings_and_no_directors_loan():
    client = FakeModel([transactions_json(ROW)])
    TransactionExtractor(client, business_name="Jo Bloggs", business_type="sole_trader").extract_accounting_data("x")
    system = client.calls[0]["messages"][0]["content"]
    assert "You keep the books of Jo Bloggs, a UK sole trader." in system
    assert "3260 Drawings:" in system and "2250 Director's Loan Account:" not in system


def test_the_schema_accepts_any_account_a_business_could_use():
    account = ExtractedTransactions.model_json_schema()["$defs"]["AccountingTransaction"]["properties"]["account"]
    assert {"2250 Director's Loan Account", "3260 Drawings", "0055 Vans"} <= set(account["enum"])


def test_statement_balances_come_once_with_the_answer_and_running_balances_per_row():
    # A 60-row statement with its opening and closing balance on every row ran past 8,192 output tokens.
    shape = ExtractedTransactions.model_json_schema()
    assert {"opening_balance", "closing_balance"} <= set(shape["properties"])
    assert not {"opening_balance", "closing_balance"} & set(shape["$defs"]["AccountingTransaction"]["properties"])
    reply = json.dumps({"transactions": [dict(ROW, balance=1240.5)], "opening_balance": 1500.0, "closing_balance": -20.0})
    result, client = extract(reply)
    assert (result.data[0].balance, result.opening_balance, result.closing_balance) == (1240.5, 1500.0, -20.0)
    assert "opening_balance and closing_balance once" in client.calls[0]["messages"][0]["content"]
