import datetime as dt
import re
from decimal import Decimal

import pytest

from ledgersync import ledger
from ledgersync.checks import issue
from ledgersync.errors import ClientArchived, InvalidInput
from ledgersync.models import ClientFields, ClientPatch, ManualUpload, RowPatch, Transaction
from ledgersync.store import Store

CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ledgersync.db")


def row(**changes) -> Transaction:
    fields = dict(direction="out", gross="72.00", account_code="7502", date=dt.date(2026, 9, 1), description="BT",
                  document_type="invoice", counterparty="BT Business")
    return Transaction(**{**fields, **changes})


def test_a_bill_and_its_payment_saved_apart_are_paired_in_the_ledger(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "BT-0905.pdf", "pdf", [row(document_ref="bt")])
    store.add_upload(client.id, "Barclays.csv", "table", [row(date=dt.date(2026, 9, 5), document_type="statement",
                                                              counterparty="BT BUSINESS DD", document_ref="l1")])
    built = ledger.build(store, client.id)
    bill, line = built.transactions
    assert ([s.ref for s in line.pays], line.paid_against, bill.owed) == (["bt"], "2100", Decimal("0.00"))
    assert [u.name for u in built.uploads] == ["Barclays.csv", "BT-0905.pdf"]


def test_the_clients_vat_setting_and_kind_of_business_are_applied(store):
    client = store.create_client(CUBE.model_copy(update={"vat_registered": False}))
    store.add_upload(client.id, "a.pdf", "pdf", [row(vat="12.00"), row(account_code="3260", document_type="receipt")])
    bill, drawings = ledger.build(store, client.id).transactions
    assert (bill.vat_posted, bill.net) == (Decimal("0.00"), Decimal("72.00"))
    assert "director_loan" in [i.code for i in drawings.issues]


def test_each_statement_upload_gets_its_balance_check(store):
    client = store.create_client(CUBE)
    line = dict(document_type="statement", counterparty="Shop")
    statement = store.add_upload(client.id, "Barclays.csv", "table", [
        row(**line, gross="20.00", balance="980.00", document_ref="l1"),
        row(**line, gross="30.00", balance="900.00", document_ref="l2")])
    receipt = store.add_upload(client.id, "r.jpg", "image", [row(document_type="receipt")])
    checks = {u.id: u.statement for u in ledger.build(store, client.id).uploads}
    assert (checks[statement].status, checks[statement].difference, checks[receipt]) == ("gap", Decimal("50.00"), None)


def test_summaries_count_rows_and_what_needs_review(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row(), row(account_code="9999", document_ref="x")])
    [summary] = ledger.summaries(store)
    assert (summary.id, summary.rows, summary.to_review) == (client.id, 2, 1)


def test_an_edit_is_checked_before_it_is_saved(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    for patch, message in ((RowPatch(gross="0"), "The amount must be above zero."),
                           (RowPatch(vat="72.00"), "VAT must be below the amount."),
                           (RowPatch(description=" "), "Enter a description."),
                           (RowPatch(account_code="3260"), "Account 3260 can't be chosen for Business Cube Ltd."),
                           (RowPatch(link=[]), "Only a bank line can be linked to documents.")):
        with pytest.raises(InvalidInput, match=re.escape(message)):
            ledger.change_row(store, client.id, rid, patch)
    assert store.rows(client.id)[0].original is None


def test_an_edit_rebooks_the_row_and_revert_brings_back_what_was_read(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    [edited] = ledger.change_row(store, client.id, rid, RowPatch(document_type="receipt", gross="70.00")).transactions
    assert (edited.edited, edited.owed, edited.contra_account_code, edited.original["gross"]) == (
        True, None, "1200", "72.00")
    [back] = ledger.change_row(store, client.id, rid, RowPatch(revert=True)).transactions
    assert (back.edited, back.gross, back.owed) == (False, Decimal("72.00"), Decimal("72.00"))


def test_a_bank_line_turned_into_an_invoice_becomes_owed(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "s.csv", "table", [row(document_type="statement", counterparty="Clearway")])
    rid = store.rows(client.id)[0].id
    [invoice] = ledger.change_row(store, client.id, rid, RowPatch(document_type="invoice")).transactions
    assert (bool(invoice.document_ref), invoice.owed, invoice.contra_account_code) == (True, Decimal("72.00"), "2100")


def test_fixing_a_misread_amount_clears_the_statement_gap(store):
    client = store.create_client(CUBE)
    line = dict(document_type="statement", counterparty="Shop")
    store.add_upload(client.id, "s.csv", "table", [row(**line, gross="20.00", balance="980.00", document_ref="l1"),
                                                   row(**line, gross="30.00", balance="900.00", document_ref="l2")])
    second = store.rows(client.id)[1].id
    built = ledger.change_row(store, client.id, second, RowPatch(gross="80.00"))
    assert (built.uploads[0].statement.status, built.transactions[1].issues) == ("ok", [])


def test_an_archived_clients_rows_cannot_change(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row()])
    rid = store.rows(client.id)[0].id
    store.update_client(client.id, {"archived": True})
    with pytest.raises(ClientArchived):
        ledger.change_row(store, client.id, rid, RowPatch(include=True))


def test_a_client_needs_a_name_a_responsible_person_and_a_real_email(store):
    for change, message in (({"name": ""}, "Enter the company name."),
                            ({"contact_name": ""}, "Enter the responsible person."),
                            ({"contact_email": "jenny"}, "Enter a valid email.")):
        with pytest.raises(InvalidInput, match=re.escape(message)):
            ledger.add_client(store, CUBE.model_copy(update=change))
    client = ledger.add_client(store, CUBE)
    with pytest.raises(InvalidInput, match="Enter the company name"):
        ledger.change_client(store, client.id, ClientPatch(name="  "))
    assert ledger.change_client(store, client.id, ClientPatch(contact_phone=" 07700 900123 ")).contact_phone == "07700 900123"


def test_a_manual_entry_is_saved_as_its_own_upload(store):
    client = store.create_client(CUBE)
    built = ledger.add_manual(store, client.id, ManualUpload(transactions=[row(document_type="receipt", method="user")]))
    assert [(u.name, u.kind, u.rows) for u in built.uploads] == [("Manual entry", "manual", 1)]
    with pytest.raises(InvalidInput):
        ledger.add_manual(store, client.id, ManualUpload(transactions=[]))


def test_the_trial_balance_comes_from_the_saved_rows(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row(document_type="receipt", vat="12.00")])
    assert [(l.code, l.debit, l.credit) for l in ledger.trial_balance(store, client.id).lines] == [
        ("1200", Decimal("0.00"), Decimal("72.00")), ("2201", Decimal("12.00"), Decimal("0.00")),
        ("7502", Decimal("60.00"), Decimal("0.00"))]


def test_an_edited_suspense_row_leaves_to_review(store):
    client = store.create_client(CUBE)
    store.add_upload(client.id, "a.pdf", "pdf", [row(account_code="9998",
                                                     issues=[issue("account_not_recognised", "Went to Suspense.")])])
    rid = store.rows(client.id)[0].id
    assert ledger.summaries(store)[0].to_review == 1
    ledger.change_row(store, client.id, rid, RowPatch(account_code="7500"))
    assert ledger.summaries(store)[0].to_review == 0
