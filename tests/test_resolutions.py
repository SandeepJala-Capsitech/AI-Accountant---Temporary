"""The accountant chat's resolutions (R0–R5): each checked against its evidence, applied or refused, and recorded as
a decision. Rows like Northbridge Consulting's October: a mixed-VAT Costco receipt, the Inkwell invoice, and a staff
expenses sheet that repeats both."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from helpers import net, row
from ledgersync import books
from ledgersync.checks import booked
from ledgersync.errors import InvalidInput
from ledgersync.models import ClientFields, ClosedYear, Evidence, Issue
from ledgersync.records import AgentRecords
from ledgersync.resolutions import (KeepOne, SplitPart, SplitVat, UsePrimary, VatFromDocument, record_automatic,
                                    resolve, set_date)

pytestmark = pytest.mark.mongo

YEAR = dt.date(2026, 4, 1)
ACTOR = "agent:openai/gpt-oss-120b"

COSTCO = """COSTCO WHOLESALE   Watford
Printer paper        52.80 A
Biscuits             12.00 Z
TOTAL  £64.80
VAT @ 20%   8.80
VAT No GB 123 4567 89"""
INKWELL = """Inkwell Print Ltd
Invoice INK-1043   Date 10 October 2026
Business cards   25.18
VAT 5.04
Total £30.22"""
SHEET = """Staff expenses, October 2026: Priya Shah
07/10/2026  Costco office supplies   64.80
10/10/2026  Inkwell business cards   27.50"""
DEPOT = """Office Depot
Toner   15.00
VAT     3.00
Total  18.00"""


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


def new_client(store, name: str) -> int:
    return store.create_client(ClientFields(name=name, business_type="limited_company", contact_name="Sam Patel",
                                            vat_registered=True, first_year_end=dt.date(2027, 3, 31))).id


@pytest.fixture
def pages() -> dict[int, list[str]]:
    """The text of each upload, by its id, as documents.text_of would read it."""
    return {}


def upload_named(store, client_id: int, name: str) -> int:
    return next(u["id"] for u in store.uploads(client_id) if u["name"] == name)


def add(store, client_id: int, pages: dict, name: str, text: str, *rows, kind: str = "document") -> list[int]:
    """Saves the rows as one upload whose document reads `text`; the rows' ids."""
    upload_id = store.add_upload(client_id, name, kind, list(rows), year_start=YEAR)
    pages[upload_id] = [text]
    return [r.id for r in store.rows(client_id) if r.upload_id == upload_id]


@pytest.fixture
def nb(mongo_store, pages):
    """Northbridge Consulting Ltd (year to 31 Mar 2027) with the Costco receipt, the Inkwell invoice and the staff
    expenses sheet repeating both, the Inkwell line at £27.50."""
    store = mongo_store
    client_id = new_client(store, "Northbridge Consulting Ltd")
    [receipt] = add(store, client_id, pages, "costco.txt", COSTCO, row(
        date=dt.date(2026, 10, 7), description="Costco Wholesale", counterparty="Costco Wholesale",
        gross=Decimal("64.80"), vat=Decimal("8.80"), account_code="7504", document_number="T-4471"))
    [invoice] = add(store, client_id, pages, "inkwell-invoice.pdf", INKWELL, row(
        date=dt.date(2026, 10, 10), description="Business cards", counterparty="Inkwell Print Ltd",
        gross=Decimal("30.22"), vat=Decimal("5.04"), account_code="7500", document_type="invoice",
        document_number="INK-1043"))
    sheet_costco, sheet_inkwell = add(store, client_id, pages, "staff-expenses-october.xlsx", SHEET,
                                      row(date=dt.date(2026, 10, 7), description="Costco office supplies",
                                          counterparty="Priya Shah", gross=Decimal("64.80"), account_code="7504",
                                          document_type="expense_claim", document_ref="sheet"),
                                      row(date=dt.date(2026, 10, 10), description="Inkwell business cards",
                                          counterparty="Priya Shah", gross=Decimal("27.50"), account_code="7500",
                                          document_type="expense_claim", document_ref="sheet"), kind="table")
    return SimpleNamespace(id=client_id, receipt=receipt, invoice=invoice, sheet_costco=sheet_costco,
                           sheet_inkwell=sheet_inkwell, upload=lambda name: upload_named(store, client_id, name))


def call(store, records, pages, nb, row_id, resolution, *quotes, upload=None, reason="Checked the documents."):
    """resolve() as the agent's tool calls it, citing quotes from one upload (the receipt's by default)."""
    upload_id = upload or nb.upload("costco.txt")
    return resolve(store, records, client_id=nb.id, thread_id="t1", actor=ACTOR, row_id=row_id,
                   resolution=resolution, evidence=[Evidence(upload_id=upload_id, quote=q) for q in quotes],
                   reason=reason, texts=lambda uid: pages.get(uid, []))


def saved(store, client_id: int, row_id: int):
    return next(r.tx for r in store.rows(client_id) if r.id == row_id)


def shown(store, client_id: int, row_id: int):
    """The row as the ledger shows it, matched."""
    return next(r for r in books.all_rows(store, client_id) if r.id == row_id)


def keep_receipt(nb) -> KeepOne:
    return KeepOne(action="keep_one", keep_row_id=nb.receipt, drop_row_ids=[nb.sheet_costco])


# ─── keep_one (R2) ─────────────────────────────────────────────────────────────

def test_keep_one_excludes_lower_ranked_copy(mongo_store, records, pages, nb):
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "total   £64.80")
    assert (decision.status, decision.actor, decision.action) == ("applied", ACTOR, "keep_one")
    assert decision.rule.startswith("R2")
    assert decision.row_ids == sorted([nb.receipt, nb.sheet_costco])
    assert decision.before == {str(nb.sheet_costco): {"excluded": False}}
    assert decision.after == {str(nb.sheet_costco): {"excluded": True}}
    assert decision.evidence == [Evidence(upload_id=nb.upload("costco.txt"), upload_name="costco.txt",
                                          quote="total   £64.80")]
    assert decision.year_start == YEAR and decision.thread_id == "t1"
    dropped = saved(mongo_store, nb.id, nb.sheet_costco)
    assert dropped.excluded and dropped.decision_ids == [decision.id]
    assert not booked(shown(mongo_store, nb.id, nb.sheet_costco))
    assert booked(shown(mongo_store, nb.id, nb.receipt))
    assert [d.id for d in records.decisions(nb.id)] == [decision.id]


def test_keep_one_refuses_dropping_higher_rank(mongo_store, records, pages, nb):
    wrong_way = KeepOne(action="keep_one", keep_row_id=nb.sheet_costco, drop_row_ids=[nb.receipt])
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, wrong_way, "TOTAL £64.80")
    assert decision.status == "refused" and "ranks" in decision.reason
    assert decision.before == decision.after
    assert not saved(mongo_store, nb.id, nb.receipt).excluded
    assert saved(mongo_store, nb.id, nb.receipt).decision_ids == []
    assert [d.status for d in records.decisions(nb.id)] == ["refused"]


def test_keep_one_refuses_rows_that_are_not_the_same_transaction(mongo_store, records, pages, nb):
    unrelated = KeepOne(action="keep_one", keep_row_id=nb.receipt, drop_row_ids=[nb.sheet_inkwell])
    decision = call(mongo_store, records, pages, nb, nb.receipt, unrelated, "TOTAL £64.80")
    assert decision.status == "refused" and not saved(mongo_store, nb.id, nb.sheet_inkwell).excluded


# ─── use_primary (R4) ──────────────────────────────────────────────────────────

def test_use_primary_invoice_beats_sheet(mongo_store, records, pages, nb):
    primary = UsePrimary(action="use_primary", primary_row_id=nb.invoice, other_row_id=nb.sheet_inkwell)
    decision = call(mongo_store, records, pages, nb, nb.sheet_inkwell, primary, "Total £30.22",
                    upload=nb.upload("inkwell-invoice.pdf"))
    assert decision.status == "applied" and decision.rule.startswith("R4")
    assert decision.after == {str(nb.sheet_inkwell): {"excluded": True}}
    assert booked(shown(mongo_store, nb.id, nb.invoice))
    assert not booked(shown(mongo_store, nb.id, nb.sheet_inkwell))
    tb = books.trial_balance(mongo_store, nb.id, YEAR)
    assert net(tb, "7500") == Decimal("25.18") and net(tb, "2100") == Decimal("-30.22")   # £30.22 booked


def test_use_primary_links_a_bank_line_to_the_invoice(mongo_store, records, pages, nb):
    """With the sheet's line out of the books (it held the invoice back), a bank line that disagrees with the invoice
    pays it."""
    sheet = UsePrimary(action="use_primary", primary_row_id=nb.invoice, other_row_id=nb.sheet_inkwell)
    call(mongo_store, records, pages, nb, nb.sheet_inkwell, sheet, "Total £30.22",
         upload=nb.upload("inkwell-invoice.pdf"))
    [line] = add(mongo_store, nb.id, pages, "barclays-october.csv", "12/10/2026 INKWELL PRINT 30.00",
                 row(date=dt.date(2026, 10, 12), description="INKWELL PRINT", counterparty="INKWELL PRINT",
                     gross=Decimal("30.00"), account_code="7500", document_type="statement"), kind="table")
    primary = UsePrimary(action="use_primary", primary_row_id=nb.invoice, other_row_id=line)
    decision = call(mongo_store, records, pages, nb, line, primary, "Total £30.22",
                    upload=nb.upload("inkwell-invoice.pdf"))
    ref = saved(mongo_store, nb.id, nb.invoice).document_ref
    assert decision.status == "applied" and decision.after == {str(line): {"link": [ref]}}
    after = saved(mongo_store, nb.id, line)
    assert after.link == [ref] and not after.excluded
    assert [p.ref for p in shown(mongo_store, nb.id, line).pays] == [ref]


def test_use_primary_refuses_a_lower_ranked_primary(mongo_store, records, pages, nb):
    backwards = UsePrimary(action="use_primary", primary_row_id=nb.sheet_inkwell, other_row_id=nb.invoice)
    decision = call(mongo_store, records, pages, nb, nb.invoice, backwards, "Total £30.22",
                    upload=nb.upload("inkwell-invoice.pdf"))
    assert decision.status == "refused" and not saved(mongo_store, nb.id, nb.invoice).excluded


# ─── split_vat (R3) ────────────────────────────────────────────────────────────

def costco_split(zero_part: str = "12.00") -> SplitVat:
    return SplitVat(action="split_vat", parts=[
        SplitPart(description="Printer paper", account_code="7504", gross=Decimal("52.80"), rate="standard"),
        SplitPart(description="Biscuits", account_code="7406", gross=Decimal(zero_part), rate="zero")])


def test_split_vat_parts_must_sum(mongo_store, records, pages, nb):
    decision = call(mongo_store, records, pages, nb, nb.receipt, costco_split("11.99"), "Biscuits 12.00 Z")
    assert decision.status == "refused" and "£64.79" in decision.reason
    assert saved(mongo_store, nb.id, nb.receipt).superseded_by is None
    assert len(mongo_store.rows(nb.id)) == 4


def test_split_vat_refuses_vat_that_does_not_match_the_document(mongo_store, records, pages, nb):
    both_standard = SplitVat(action="split_vat", parts=[
        SplitPart(description="Printer paper", account_code="7504", gross=Decimal("52.80"), rate="standard"),
        SplitPart(description="Biscuits", account_code="7406", gross=Decimal("12.00"), rate="standard")])
    decision = call(mongo_store, records, pages, nb, nb.receipt, both_standard, "VAT @ 20% 8.80")
    assert decision.status == "refused" and "VAT" in decision.reason


def test_split_vat_supersedes_original(mongo_store, records, pages, nb):
    decision = call(mongo_store, records, pages, nb, nb.receipt, costco_split(), "Printer paper 52.80 A",
                    "Biscuits 12.00 Z")
    assert decision.status == "applied" and decision.rule.startswith("R3")
    assert saved(mongo_store, nb.id, nb.receipt).superseded_by == decision.id
    parts = [r for r in books.all_rows(mongo_store, nb.id) if r.id in decision.row_ids and r.id != nb.receipt]
    assert [(p.description, p.account_code, p.gross, p.vat_posted) for p in parts] == [
        ("Printer paper", "7504", Decimal("52.80"), Decimal("8.80")),
        ("Biscuits", "7406", Decimal("12.00"), Decimal("0.00"))]
    assert all(booked(p) and p.decision_ids == [decision.id] for p in parts)
    assert not booked(shown(mongo_store, nb.id, nb.receipt))
    assert decision.before == {str(nb.receipt): {"superseded_by": None}}
    assert decision.after[str(nb.receipt)] == {"superseded_by": decision.id}
    assert decision.after[str(parts[0].id)] == {"description": "Printer paper", "account_code": "7504",
                                                "gross": "52.80", "vat": "8.80", "vat_treatment": "standard"}


# ─── set_vat_from_document (R5) ────────────────────────────────────────────────

@pytest.fixture
def depot(mongo_store, pages, nb) -> int:
    """An Office Depot receipt read without its VAT."""
    [receipt] = add(mongo_store, nb.id, pages, "depot.pdf", DEPOT, row(
        date=dt.date(2026, 10, 20), description="Toner", counterparty="Office Depot", gross=Decimal("18.00"),
        account_code="7504"))
    return receipt


def test_vat_from_document_must_appear_in_text(mongo_store, records, pages, nb, depot):
    cite = dict(upload=nb.upload("depot.pdf"))
    refused = call(mongo_store, records, pages, nb, depot,
                   VatFromDocument(action="set_vat_from_document", vat=Decimal("2.50")), "Total 18.00", **cite)
    assert refused.status == "refused" and saved(mongo_store, nb.id, depot).vat is None
    applied = call(mongo_store, records, pages, nb, depot,
                   VatFromDocument(action="set_vat_from_document", vat=Decimal("3.00")), "VAT 3.00", **cite)
    assert applied.status == "applied" and applied.rule.startswith("R5")
    assert applied.before == {str(depot): {"vat": None}} and applied.after == {str(depot): {"vat": "3.00"}}
    assert saved(mongo_store, nb.id, depot).vat == Decimal("3.00")


def test_vat_above_a_sixth_of_the_amount_is_refused(mongo_store, records, pages, nb, depot):
    pages[nb.upload("depot.pdf")] = [DEPOT + "\nVAT 3.60"]
    decision = call(mongo_store, records, pages, nb, depot,
                    VatFromDocument(action="set_vat_from_document", vat=Decimal("3.60")), "VAT 3.60",
                    upload=nb.upload("depot.pdf"))
    assert decision.status == "refused" and saved(mongo_store, nb.id, depot).vat is None


# ─── R0, every action ──────────────────────────────────────────────────────────

def test_quote_not_in_document_refused(mongo_store, records, pages, nb):
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £99.99")
    assert decision.status == "refused" and "TOTAL £99.99" in decision.reason
    assert not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_a_quote_with_spaces_after_commas_is_found_in_a_csv_line(mongo_store, records, pages, nb):
    pages[nb.upload("costco.txt")].append("07/10/2026,-58.00,Card Purchase,COSTCO WHOLESALE")
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb),
                    "-58.00, Card Purchase, COSTCO WHOLESALE")
    assert decision.status == "applied", decision.reason


def test_evidence_from_another_clients_upload_refused(mongo_store, records, pages, nb):
    other = new_client(mongo_store, "Other Ltd")
    add(mongo_store, other, pages, "theirs.txt", COSTCO, row(gross=Decimal("1.00")))
    theirs = next(u["id"] for u in mongo_store.uploads(other))
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80",
                    upload=theirs)
    assert decision.status == "refused" and not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_closed_year_refused(mongo_store, records, pages, nb):
    mongo_store.close_year(nb.id, ClosedYear(start=YEAR, end=dt.date(2027, 3, 31), closed_at="2027-04-30T00:00:00",
                                             closing={}))
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    assert decision.status == "refused" and "closed" in decision.reason
    assert not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_other_clients_row_refused(mongo_store, records, pages, nb):
    other = new_client(mongo_store, "Other Ltd")
    [theirs] = add(mongo_store, other, pages, "theirs.txt", COSTCO, row(
        date=dt.date(2026, 10, 7), counterparty="Costco Wholesale", gross=Decimal("64.80"), account_code="7504",
        document_type="expense_claim"))
    stray = KeepOne(action="keep_one", keep_row_id=nb.receipt, drop_row_ids=[theirs])
    decision = call(mongo_store, records, pages, nb, theirs, stray, "TOTAL £64.80")
    assert decision.status == "refused" and decision.client_id == nb.id
    assert not saved(mongo_store, other, theirs).excluded
    assert records.decisions(other) == []


def test_resolve_twice_is_one_decision(mongo_store, records, pages, nb):
    first = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    again = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    assert again.id == first.id and len(records.decisions(nb.id)) == 1
    assert saved(mongo_store, nb.id, nb.sheet_costco).decision_ids == [first.id]


def test_a_refusal_does_not_stick(mongo_store, records, pages, nb):
    """A refusal is kept but not returned for a retry: corrected, the same resolution applies."""
    first = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £99.99")
    again = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    assert (first.status, again.status) == ("refused", "applied") and again.id != first.id
    assert [d.status for d in records.decisions(nb.id)] == ["refused", "applied"]
    assert call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80").id == again.id


def test_evidence_from_an_unrelated_upload_of_the_client_refused(mongo_store, records, pages, nb):
    """The invoice's upload is the client's, but neither row kept nor row dropped comes from it."""
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "Total £30.22",
                    upload=nb.upload("inkwell-invoice.pdf"))
    assert decision.status == "refused" and not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_a_quote_too_short_to_show_anything_refused(mongo_store, records, pages, nb):
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), " 6 ")
    assert decision.status == "refused" and not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_amounts_must_be_whole_pennies(mongo_store, records, pages, nb, depot):
    split = SplitVat(action="split_vat", parts=[
        SplitPart(description="Printer paper", account_code="7504", gross=Decimal("52.805"), rate="standard"),
        SplitPart(description="Biscuits", account_code="7406", gross=Decimal("11.995"), rate="zero")])
    assert call(mongo_store, records, pages, nb, nb.receipt, split, "Biscuits 12.00 Z").status == "refused"
    vat = VatFromDocument(action="set_vat_from_document", vat=Decimal("3.001"))
    assert call(mongo_store, records, pages, nb, depot, vat, "VAT 3.00", upload=nb.upload("depot.pdf")).status \
        == "refused"
    assert saved(mongo_store, nb.id, depot).vat is None and saved(mongo_store, nb.id, nb.receipt).superseded_by is None


def test_use_primary_refuses_to_drop_the_bank_payment_of_a_payroll_report(mongo_store, records, pages, nb):
    [wages] = add(mongo_store, nb.id, pages, "payroll-october.pdf", "Net pay 2,400.00", row(
        date=dt.date(2026, 10, 28), description="Net pay", counterparty="Staff", gross=Decimal("2400.00"),
        account_code="7000", document_type="payroll", owed_to="net_pay"))
    [line] = add(mongo_store, nb.id, pages, "barclays-october.csv", "28/10/2026 SALARIES 2,350.00",
                 row(date=dt.date(2026, 10, 28), description="SALARIES", counterparty="Staff",
                     gross=Decimal("2350.00"), account_code="2220", document_type="statement"), kind="table")
    primary = UsePrimary(action="use_primary", primary_row_id=wages, other_row_id=line)
    decision = call(mongo_store, records, pages, nb, line, primary, "Net pay 2,400.00",
                    upload=nb.upload("payroll-october.pdf"))
    assert decision.status == "refused" and not saved(mongo_store, nb.id, line).excluded


def with_new_error_on(monkeypatch, row_ids):
    """Makes the ledger show an error on these rows from now on, as if the change caused it."""
    real = books.all_rows

    def all_rows(store, client_id):
        return [r.model_copy(update={"issues": r.issues + [Issue(code="same_account", severity="error",
                                                                 message="Posts to and from the same account.")]})
                if r.id in row_ids() else r for r in real(store, client_id)]
    monkeypatch.setattr(books, "all_rows", all_rows)


def test_a_change_that_leaves_a_new_error_is_put_back(mongo_store, records, pages, nb, monkeypatch):
    with_new_error_on(monkeypatch, lambda: {nb.sheet_costco})
    decision = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    assert decision.status == "refused" and "same account" in decision.reason
    assert decision.before == decision.after
    dropped = saved(mongo_store, nb.id, nb.sheet_costco)
    assert not dropped.excluded and dropped.decision_ids == []


def test_a_split_that_leaves_a_new_error_is_put_back(mongo_store, records, pages, nb, monkeypatch):
    before = {r.id for r in mongo_store.rows(nb.id)}
    with_new_error_on(monkeypatch, lambda: {r.id for r in mongo_store.rows(nb.id)} - before)   # on the parts
    decision = call(mongo_store, records, pages, nb, nb.receipt, costco_split(), "Biscuits 12.00 Z")
    assert decision.status == "refused"
    original = saved(mongo_store, nb.id, nb.receipt)
    assert original.superseded_by is None and original.decision_ids == []
    assert {r.id for r in mongo_store.rows(nb.id)} == before
    assert not any(u["kind"] == "split" for u in mongo_store.uploads(nb.id))


def test_vat_put_back_leaves_the_row_exactly_as_stored(mongo_store, records, pages, nb, depot, monkeypatch):
    """VAT is a field a person edits, so setting it keeps what was read; putting it back drops that again."""
    before = mongo_store.row_data(nb.id, [depot])
    with_new_error_on(monkeypatch, lambda: {depot})
    decision = call(mongo_store, records, pages, nb, depot,
                    VatFromDocument(action="set_vat_from_document", vat=Decimal("3.00")), "VAT 3.00",
                    upload=nb.upload("depot.pdf"))
    assert decision.status == "refused" and mongo_store.row_data(nb.id, [depot]) == before
    assert "original" not in before[depot]


def test_set_date_put_back_leaves_the_row_exactly_as_stored(mongo_store, records, nb, taxi, monkeypatch):
    before = mongo_store.row_data(nb.id, [taxi])
    with_new_error_on(monkeypatch, lambda: {taxi})
    decision = set_date(mongo_store, records, client_id=nb.id, thread_id="t1", row_id=taxi,
                        date=dt.date(2026, 10, 16), question_id="q1")
    assert decision.status == "refused" and mongo_store.row_data(nb.id, [taxi]) == before


def test_a_failure_while_changing_rows_puts_them_back(mongo_store, records, pages, nb, monkeypatch):
    """The change is written, then something fails before it is recorded: the rows go back and nothing is
    recorded, and the error is raised."""
    before = mongo_store.row_data(nb.id, [nb.receipt, nb.sheet_costco])

    def fails(store, client_id):
        raise RuntimeError("lost the connection")
    monkeypatch.setattr(books, "all_rows", fails)
    with pytest.raises(RuntimeError):
        call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), "TOTAL £64.80")
    assert mongo_store.row_data(nb.id, [nb.receipt, nb.sheet_costco]) == before
    assert records.decisions(nb.id) == []


def test_a_failure_after_a_split_removes_its_parts(mongo_store, records, pages, nb, monkeypatch):
    before = mongo_store.row_data(nb.id, [r.id for r in mongo_store.rows(nb.id)])

    def fails(store, client_id):
        raise RuntimeError("lost the connection")
    monkeypatch.setattr(books, "all_rows", fails)
    with pytest.raises(RuntimeError):
        call(mongo_store, records, pages, nb, nb.receipt, costco_split(), "Biscuits 12.00 Z")
    assert mongo_store.row_data(nb.id, [r.id for r in mongo_store.rows(nb.id)]) == before
    assert not any(u["kind"] == "split" for u in mongo_store.uploads(nb.id)) and records.decisions(nb.id) == []


# ─── set_date (R1, from an answer) ─────────────────────────────────────────────

@pytest.fixture
def taxi(mongo_store, pages, nb) -> int:
    """An undated taxi receipt, waiting in the year it was added to."""
    [receipt] = add(mongo_store, nb.id, pages, "taxi.jpg", "Taxi fare £18.00", row(
        date=None, description="Taxi", counterparty="City Cars", gross=Decimal("18.00"), account_code="7400"))
    return receipt


def test_set_date_outside_books_refused(mongo_store, records, nb, taxi):
    with pytest.raises(InvalidInput) as refused:
        set_date(mongo_store, records, client_id=nb.id, thread_id="t1", row_id=taxi, date=dt.date(2026, 3, 31),
                 question_id="q1")
    assert refused.value.message == "That date is outside the books, which run from 1 Apr 2026 to 31 Mar 2027."
    assert saved(mongo_store, nb.id, taxi).date is None and records.decisions(nb.id) == []


def test_set_date_from_an_answer_is_recorded(mongo_store, records, nb, taxi):
    decision = set_date(mongo_store, records, client_id=nb.id, thread_id="t1", row_id=taxi,
                        date=dt.date(2026, 10, 16), question_id="q1")
    assert (decision.status, decision.actor, decision.action) == ("applied", "user", "set_date")
    assert decision.rule.startswith("R1")
    assert decision.before == {str(taxi): {"date": None}} and decision.after == {str(taxi): {"date": "2026-10-16"}}
    assert saved(mongo_store, nb.id, taxi).date == dt.date(2026, 10, 16)
    again = set_date(mongo_store, records, client_id=nb.id, thread_id="t1", row_id=taxi,
                     date=dt.date(2026, 10, 16), question_id="q1")
    assert again.id == decision.id and len(records.decisions(nb.id)) == 1


# ─── record_automatic (R1, from the bank) ──────────────────────────────────────

def test_record_automatic_turns_bank_dating_into_decision(mongo_store, records, pages, nb):
    [receipt] = add(mongo_store, nb.id, pages, "parking.jpg", "NCP  £6.50", row(
        date=None, description="Parking", counterparty="Addison Lee", gross=Decimal("6.50"), account_code="7400"))
    add(mongo_store, nb.id, pages, "barclays-october.csv", "14/10/2026 ADDISON LEE LONDON 6.50",
        row(date=dt.date(2026, 10, 14), description="ADDISON LEE LONDON", counterparty="ADDISON LEE",
            gross=Decimal("6.50"), account_code="7400", document_type="statement"), kind="table")
    bank = nb.upload("barclays-october.csv")
    [decision] = record_automatic(mongo_store, records, client_id=nb.id, year_start=YEAR, thread_id="t1")
    assert (decision.rule, decision.actor, decision.status) == ("R1 Date from the bank", "rules", "applied")
    assert decision.row_ids == [receipt]
    assert decision.evidence == [Evidence(upload_id=bank, upload_name="barclays-october.csv",
                                          quote="ADDISON LEE LONDON")]
    assert decision.before == {str(receipt): {"date": None}}
    assert decision.after == {str(receipt): {"date": "2026-10-14"}}
    assert saved(mongo_store, nb.id, receipt).date is None   # matching dates it; the row is not changed
    assert record_automatic(mongo_store, records, client_id=nb.id, year_start=YEAR, thread_id="t1") == []
    assert len(records.decisions(nb.id)) == 1
