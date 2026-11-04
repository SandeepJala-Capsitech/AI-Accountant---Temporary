"""Questions worked out from the books, and answers applied without the model: the rules ask for a date or a
document, a person's answer is applied by the services, and a question goes when the books no longer need it."""
from datetime import date
from decimal import Decimal as D

import pytest
from helpers import add_rows, net, row

from ledgersync import books, ledger_book, questions
from ledgersync.errors import InvalidInput, NotFound
from ledgersync.questions import DateAnswer, NoDocumentAnswer, NotSureAnswer
from ledgersync.records import AgentRecords

pytestmark = pytest.mark.mongo

YEAR_1 = date(2025, 4, 1)
YEAR_2 = date(2026, 4, 1)


@pytest.fixture
def store(mongo_store):
    return mongo_store


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


def travel_line(**changes):
    """A £240.00 payment to Trainline on the bank statement, with no invoice behind it."""
    return row(**{**dict(date=date(2025, 7, 1), gross=D("240.00"), account_code="7400", document_type="statement",
                         counterparty="Trainline", description="TRAINLINE"), **changes})


def trainline_invoice(**changes):
    return row(**{**dict(date=date(2025, 6, 28), gross=D("240.00"), account_code="7400", document_type="invoice",
                         counterparty="Trainline", description="Train tickets", document_ref="TL-1",
                         document_number="TL-1"), **changes})


def undated_taxi(**changes):
    return row(**{**dict(date=None, gross=D("12.00"), account_code="7400", description="taxi"), **changes})


def refresh(store, records, client_id):
    return questions.refresh(store, records, client_id, YEAR_1)


def answer(store, records, client_id, question, given):
    """The open and waiting questions the answer leaves."""
    return questions.answer(store, records, client_id=client_id, thread_id="t1", question_id=question.id,
                            answer=given).questions


def row_named(store, client_id, description):
    return next(r for r in store.rows(client_id) if r.tx.description == description)


def test_undated_receipt_without_bank_match_asks_for_date(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert (q.kind, q.source, q.status) == ("date", "rules", "open")
    assert q.text == "When was this taxi?"
    assert q.why == "£12.00, no date on it and no matching bank line."
    assert q.row_ids == [row_named(store, acme, "taxi").id] and q.year_start == YEAR_1
    assert records.db.questions.find_one({"id": q.id})["key"] == f"date:{q.row_ids[0]}"
    assert [x.id for x in refresh(store, records, acme)] == [q.id]   # asking again asks nothing new


def test_receipt_dated_from_bank_asks_nothing(store, records, acme):
    receipt = undated_taxi(description="Pret lunch", gross=D("30.00"), counterparty="Pret")
    bank = row(date=date(2025, 7, 2), gross=D("30.00"), account_code="7400", document_type="statement",
               counterparty="Pret", description="PRET")
    add_rows(store, acme, receipt, bank, year_start=YEAR_1)
    dated = next(r for r in books.ledger(store, acme, YEAR_1).transactions if r.description == "Pret lunch")
    assert dated.date == date(2025, 7, 2) and any(i.code == "date_from_bank" for i in dated.issues)
    assert refresh(store, records, acme) == []


def test_file_name_date_never_used(store, records, acme):
    store.add_upload(acme, "IMG_20261014.png", "image", [undated_taxi()], year_start=YEAR_1)
    seen = books.ledger(store, acme, YEAR_1).transactions[0]
    assert seen.date is None and seen.date_found == date(2026, 10, 14)   # the name's date is only offered
    [q] = refresh(store, records, acme)
    assert (q.kind, q.text) == ("date", "When was this taxi?")
    assert store.rows(acme)[0].tx.date is None


def test_240_payment_without_invoice_asks_for_document(store, records, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert (q.kind, q.source, q.status) == ("receipt_or_invoice", "rules", "open")
    assert q.text == "Can you upload the receipt or invoice?"
    assert q.why == ("£240.00 to Trainline on 1 Jul 2025. £50 or more, so I need it. "
                     "Held in Suspense as Documentation required until then.")
    assert records.db.questions.find_one({"id": q.id})["key"] == f"doc:{q.row_ids[0]}"


def test_receipt_over_250_asks_for_vat_invoice(store, records, acme):
    add_rows(store, acme, row(date=date(2025, 8, 1), gross=D("300.00"), vat=D("50.00"), account_code="7504",
                              counterparty="Apple", description="Laptop bag", supplier_vat_number="GB123456789"),
             year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert q.kind == "vat_invoice" and q.text == "Can you upload the full VAT invoice?"
    assert q.why == ("£300.00 to Apple on 1 Aug 2025. Over £250, so I need it. "
                     "Held in Suspense as Documentation required until then.")


def test_a_row_without_a_counterparty_is_named_by_its_description(store, records, acme):
    add_rows(store, acme, travel_line(counterparty=None), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert q.why.startswith("£240.00 to TRAINLINE on 1 Jul 2025.")


def test_answer_date_dates_row_and_resolves(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert answer(store, records, acme, q, DateAnswer(kind="date", date=date(2025, 8, 14))) == []
    assert row_named(store, acme, "taxi").tx.date == date(2025, 8, 14)
    done = records.question(q.id)
    assert (done.status, done.answer) == ("resolved", "14 Aug 2025") and done.resolved_at
    [d] = records.decisions(acme)
    assert (d.action, d.status, d.actor, d.row_ids) == ("set_date", "applied", "user", [q.row_ids[0]])
    assert ledger_book.unposted_changes(store, records, acme, YEAR_1) == 0   # answering posted the row
    assert net(ledger_book.posted_trial_balance(store, records, acme, YEAR_1), "7400") == D("12.00")


def test_answer_twice_is_one_decision(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    given = DateAnswer(kind="date", date=date(2025, 8, 14))
    answer(store, records, acme, q, given)
    answer(store, records, acme, q, given)
    assert len(records.decisions(acme)) == 1
    assert records.db.ledger_entries.count_documents({"client_id": acme}) == 2   # posted once


def test_a_retry_after_a_partial_failure_is_still_one_decision(store, records, acme):
    # The decision was made and the question left open (say the first call failed before resolving it).
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    given = DateAnswer(kind="date", date=date(2025, 8, 14))
    answer(store, records, acme, q, given)
    records.set_question(q.id, "open")
    answer(store, records, acme, q, given)
    assert records.question(q.id).status == "resolved" and len(records.decisions(acme)) == 1
    assert records.db.ledger_entries.count_documents({"client_id": acme}) == 2


def test_an_answer_to_a_resolved_question_does_nothing(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    answer(store, records, acme, q, DateAnswer(kind="date", date=date(2025, 8, 14)))
    answer(store, records, acme, q, DateAnswer(kind="date", date=date(2025, 9, 1)))
    assert row_named(store, acme, "taxi").tx.date == date(2025, 8, 14) and len(records.decisions(acme)) == 1
    answer(store, records, acme, q, NotSureAnswer(kind="not_sure"))
    assert records.question(q.id).status == "resolved" and records.question(q.id).answer == "14 Aug 2025"


def test_a_date_outside_the_books_is_refused_and_the_question_stays_open(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    with pytest.raises(InvalidInput):
        answer(store, records, acme, q, DateAnswer(kind="date", date=date(2020, 1, 1)))
    assert records.question(q.id).status == "open" and records.decisions(acme) == []


def test_no_document_keeps_suspense_and_waits(store, records, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    ledger_book.post(store, records, acme, YEAR_1)
    [waiting] = answer(store, records, acme, q, NoDocumentAnswer(kind="no_document"))
    assert (waiting.id, waiting.status, waiting.answer) == (q.id, "waiting", "I don't have it")
    assert [(x.id, x.status) for x in refresh(store, records, acme)] == [(q.id, "waiting")]   # still waiting
    tb = ledger_book.posted_trial_balance(store, records, acme, YEAR_1)
    assert net(tb, "9998") == D("240.00") and tb.suspense[0].needs == "receipt_or_invoice"
    [again] = answer(store, records, acme, q, NotSureAnswer(kind="not_sure"))
    assert (again.status, again.answer) == ("waiting", "Not sure")


def test_a_date_question_cannot_be_answered_with_no_document_nor_a_document_question_with_a_date(
        store, records, acme):
    add_rows(store, acme, undated_taxi(), travel_line(), year_start=YEAR_1)
    by_kind = {q.kind: q for q in refresh(store, records, acme)}
    with pytest.raises(InvalidInput):
        answer(store, records, acme, by_kind["date"], NoDocumentAnswer(kind="no_document"))
    with pytest.raises(InvalidInput):
        answer(store, records, acme, by_kind["receipt_or_invoice"], DateAnswer(kind="date", date=date(2025, 8, 1)))
    assert {q.status for q in records.questions(acme, YEAR_1)} == {"open"}


def test_an_agent_question_accepts_only_not_sure(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [taxi] = refresh(store, records, acme)
    asked = questions.ask(records, client_id=acme, year_start=YEAR_1, row_ids=taxi.row_ids, text="Personal trip?",
                          why="It ends at a home address.")
    with pytest.raises(InvalidInput):
        answer(store, records, acme, asked, NoDocumentAnswer(kind="no_document"))
    with pytest.raises(InvalidInput):
        answer(store, records, acme, asked, DateAnswer(kind="date", date=date(2025, 8, 1)))
    assert records.question(asked.id).status == "open"
    answer(store, records, acme, asked, NotSureAnswer(kind="not_sure"))
    assert records.question(asked.id).status == "waiting"


def test_another_clients_or_unknown_question_is_not_found(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    given = NotSureAnswer(kind="not_sure")
    with pytest.raises(NotFound):
        questions.answer(store, records, client_id=acme + 1, thread_id=None, question_id=q.id, answer=given)
    with pytest.raises(NotFound):
        questions.answer(store, records, client_id=acme, thread_id=None, question_id="nope", answer=given)


def test_uploaded_invoice_resolves_held_payment(store, records, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    ledger_book.post(store, records, acme, YEAR_1)
    assert net(ledger_book.posted_trial_balance(store, records, acme, YEAR_1), "9998") == D("240.00")
    add_rows(store, acme, trainline_invoice(), year_start=YEAR_1)
    assert refresh(store, records, acme) == []
    done = records.question(q.id)
    assert done.status == "resolved" and done.resolved_at
    result = ledger_book.post(store, records, acme, YEAR_1)
    assert result.reversals_added == 2 and result.problems == []
    tb = ledger_book.posted_trial_balance(store, records, acme, YEAR_1)
    assert net(tb, "9998") == 0 and tb.suspense == []


def test_a_question_resolved_by_the_books_takes_the_file_that_settled_it_as_its_answer(store, records, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    answer(store, records, acme, q, NoDocumentAnswer(kind="no_document"))
    assert records.question(q.id).answer == "I don't have it"
    add_rows(store, acme, trainline_invoice(), year_start=YEAR_1)
    assert refresh(store, records, acme) == []
    done = records.question(q.id)
    assert (done.status, done.answer) == ("resolved", "Matched to rows.csv") and done.resolved_at   # not "I don't have it"


def test_invoice_that_does_not_fit_stays_open(store, records, acme):
    # Another supplier's invoice for the same amount is not this payment's: the payment is still held.
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    add_rows(store, acme, trainline_invoice(counterparty="Hotel Bristol", description="Rooms", document_ref="HB-1"),
             year_start=YEAR_1)
    assert [(x.id, x.status) for x in refresh(store, records, acme)] == [(q.id, "open")]
    ledger_book.post(store, records, acme, YEAR_1)
    assert net(ledger_book.posted_trial_balance(store, records, acme, YEAR_1), "9998") == D("240.00")


def test_a_receipt_for_a_payment_over_250_is_not_enough(store, records, acme):
    # The receipt pays the bank line, but only a full VAT invoice will do: the question moves to the receipt.
    add_rows(store, acme, travel_line(gross=D("300.00")), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    add_rows(store, acme, row(date=date(2025, 7, 1), gross=D("300.00"), account_code="7400", counterparty="Trainline",
                              description="Season ticket", document_ref="R-1"), year_start=YEAR_1)
    [now] = refresh(store, records, acme)
    assert now.kind == "vat_invoice" and now.id != q.id and records.question(q.id).status == "resolved"


def test_a_question_comes_back_when_the_document_is_taken_away(store, records, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    upload_id = add_rows(store, acme, trainline_invoice(), year_start=YEAR_1)
    assert refresh(store, records, acme) == []
    store.delete_upload(acme, upload_id)
    [back] = refresh(store, records, acme)
    assert (back.id, back.status, back.resolved_at) == (q.id, "open", None)


def test_a_receipt_edited_to_over_250_asks_for_a_vat_invoice_and_back(store, records, acme):
    add_rows(store, acme, row(date=date(2025, 8, 1), gross=D("100.00"), account_code="7400", counterparty="Rail",
                              description="Tickets"), year_start=YEAR_1)
    assert refresh(store, records, acme) == []
    rid = store.rows(acme)[0].id
    store.patch_row(acme, rid, {"gross": "300.00"})
    [q] = refresh(store, records, acme)
    assert (q.kind, q.row_ids) == ("vat_invoice", [rid]) and "Over £250" in q.why
    store.patch_row(acme, rid, {"gross": "100.00"})
    assert refresh(store, records, acme) == [] and records.question(q.id).status == "resolved"


@pytest.mark.parametrize("order", [(YEAR_1, YEAR_2), (YEAR_2, YEAR_1)])
def test_a_held_line_moved_to_the_next_year_is_asked_there_only(store, records, acme, order):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    store.patch_row(acme, store.rows(acme)[0].id, {"date": "2026-05-01"})
    for year in (*order, *order):
        questions.refresh(store, records, acme, year)
    assert records.questions(acme, YEAR_1) == []
    [there] = records.questions(acme, YEAR_2)
    assert (there.id, there.status, there.year_start) == (q.id, "open", YEAR_2)
    assert "on 1 May 2026" in there.why


def test_a_held_receipt_patched_to_a_statement_line_is_asked_for_the_other_document(store, records, acme):
    add_rows(store, acme, row(date=date(2025, 8, 1), gross=D("300.00"), account_code="7400", counterparty="Rail",
                              description="Tickets"), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert q.kind == "vat_invoice"
    store.patch_row(acme, store.rows(acme)[0].id, {"document_type": "statement", "gross": "240.00"})
    [again] = refresh(store, records, acme)
    assert (again.id, again.kind, again.text) == (q.id, "receipt_or_invoice", "Can you upload the receipt or invoice?")
    assert "£50 or more" in again.why


def test_ask_creates_an_agent_question_that_refresh_leaves_alone(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [taxi] = refresh(store, records, acme)
    rid = taxi.row_ids[0]
    asked = questions.ask(records, client_id=acme, year_start=YEAR_1, row_ids=[rid], text="Was this a personal trip?",
                          why="The taxi ride ends at a home address.")
    assert (asked.kind, asked.source, asked.status, asked.row_ids) == ("other", "agent", "open", [rid])
    again = questions.ask(records, client_id=acme, year_start=YEAR_1, row_ids=[rid], text="Was this a personal trip?",
                          why="The ride ends at a home address.")
    assert again.id == asked.id and again.why == "The ride ends at a home address."
    answer(store, records, acme, taxi, DateAnswer(kind="date", date=date(2025, 8, 14)))   # the rule question goes
    assert [q.id for q in refresh(store, records, acme)] == [asked.id]
    [still] = answer(store, records, acme, asked, NotSureAnswer(kind="not_sure"))
    assert (still.id, still.status) == (asked.id, "waiting")
    assert [q.id for q in refresh(store, records, acme)] == [asked.id]
