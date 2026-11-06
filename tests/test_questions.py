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


def test_an_applied_answer_records_a_change_and_a_repeat_does_not(store, records, acme):
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    for _ in range(2):
        answer(store, records, acme, q, DateAnswer(kind="date", date=date(2025, 8, 14)))
    [change] = [c for c in records.changes(acme) if c.kind == "question_answered"]
    assert (change.actor, change.row_ids, change.summary) == ("user", q.row_ids, f"Answered: {q.text}")


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


def test_an_answer_change_records_the_answer_and_a_repeated_one_records_nothing(store, records, acme):
    add_rows(store, acme, travel_line(), undated_taxi(), year_start=YEAR_1)
    by_kind = {q.kind: q for q in refresh(store, records, acme)}
    answer(store, records, acme, by_kind["date"], DateAnswer(kind="date", date=date(2025, 8, 14)))
    for _ in range(2):
        answer(store, records, acme, by_kind["receipt_or_invoice"], NoDocumentAnswer(kind="no_document"))

    made = [c for c in records.changes(acme) if c.kind == "question_answered"]

    assert [c.after for c in made] == [{"answer": "14 Aug 2025"}, {"answer": "I don't have it"}]


# ─── Closing the agent's own question ──────────────────────────────────────────

from ledgersync.models import Evidence, QuestionDraft  # noqa: E402
from test_resolutions import nb, pages  # noqa: E402,F401


def close_args(records, nb, pages, q, *quotes, **kw):
    uid = nb.upload("costco.txt")
    return dict(client_id=nb.id, year_start=date(2026, 4, 1), question_id=q.id, reason="The receipt says so.", actor="agent:test",
                trigger="background", review_id="r1", texts=lambda n: pages.get(n, []),
                evidence=[Evidence(upload_id=uid, quote=t) for t in quotes], **kw)


def test_a_question_is_closed_by_a_quote_from_its_rows_document(store, records, pages, nb):
    q = questions.ask(records, client_id=nb.id, year_start=date(2026, 4, 1), row_ids=[nb.receipt],
                      text="Is the paper for the office?", why="Unclear.")
    d = questions.close(store, records, **close_args(records, nb, pages, q, "Printer paper        52.80"))
    assert (d.status, d.action, d.trigger, d.review_id) == ("applied", "close_question", "background", "r1")
    assert d.evidence[0].upload_name == "costco.txt"
    assert records.question(q.id).answer == "Closed by the AI: The receipt says so."
    again = questions.close(store, records, **close_args(records, nb, pages, q, "Printer paper        52.80"))
    assert again.id == d.id   # asked again, the same decision


def test_a_suggested_closing_leaves_the_question_open_and_a_rule_question_cannot_be_closed(store, records, pages, nb):
    q = questions.ask(records, client_id=nb.id, year_start=date(2026, 4, 1), row_ids=[nb.receipt],
                      text="Is the paper for the office?", why="Unclear.")
    d = questions.close(store, records, **close_args(records, nb, pages, q, "Printer paper        52.80",
                                                     suggest=True))
    assert d.status == "suggested" and records.question(q.id).status == "open"
    assert not [c for c in records.changes(nb.id) if c.kind == "question_closed"]
    rule = records.upsert_question(QuestionDraft(client_id=nb.id, year_start=date(2026, 4, 1), row_ids=[nb.receipt],
                                                 kind="date", text="When?", why="No date.", source="rules"), "k")
    with pytest.raises(InvalidInput):
        questions.close(store, records, **close_args(records, nb, pages, rule, "Printer paper        52.80"))


def test_a_question_of_another_year_cannot_be_closed(store, records, pages, nb):
    q = questions.ask(records, client_id=nb.id, year_start=date(2026, 4, 1), row_ids=[nb.receipt], text="Q?", why="?")
    args = close_args(records, nb, pages, q, "Printer paper        52.80")
    args["year_start"] = date(2025, 4, 1)
    with pytest.raises(InvalidInput, match="That question belongs to another year."):
        questions.close(store, records, **args)
    assert records.question(q.id).status == "open"


def test_an_answer_outside_a_chat_makes_the_client_due_and_one_in_a_chat_does_not(store, records, acme):
    from datetime import datetime, timezone
    add_rows(store, acme, undated_taxi(), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    questions.answer(store, records, client_id=acme, thread_id="t1", question_id=q.id,
                     answer=DateAnswer(kind="date", date=date(2025, 8, 14)))
    assert records.review_state(acme).due_at is None   # the chat's agent handles it

    add_rows(store, acme, undated_taxi(description="taxi 2"), year_start=YEAR_1)
    [q2] = [x for x in refresh(store, records, acme) if x.id != q.id]
    at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    questions.answer(store, records, client_id=acme, thread_id=None, question_id=q2.id,
                     answer=DateAnswer(kind="date", date=date(2025, 8, 15)), now=at, wait=30)
    assert records.review_state(acme).due_at == "2026-11-04T10:00:30+00:00"


def test_a_payout_with_no_report_asks_for_it(store, records, acme):
    add_rows(store, acme, row(date=date(2025, 10, 15), direction="in", gross=D("1163.10"), account_code="1250",
                              document_type="statement", counterparty="Stripe Payments UK"), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert (q.kind, q.source, q.text) == ("payout_report", "rules", "Can you upload the Stripe payout report?")
    assert q.why == ("£1,163.10 paid out by Stripe Payments UK on 15 Oct 2025 is sales less Stripe's fees and "
                     "refunds. Held in Payment Processor Clearing until the report shows each.")


def test_a_payment_of_paye_with_no_payroll_report_asks_for_it(store, records, acme):
    add_rows(store, acme, row(date=date(2025, 10, 25), gross=D("612.40"), account_code="2210",
                              document_type="statement", counterparty="HMRC PAYE"), year_start=YEAR_1)
    [q] = refresh(store, records, acme)
    assert (q.kind, q.text) == ("payroll_report", "Can you upload the payroll report for October 2025?")
    assert q.why == ("£612.40 paid to HMRC PAYE on 25 Oct 2025, and no payroll report books the wages it is for.")
    left = answer(store, records, acme, q, NoDocumentAnswer(kind="no_document"))
    assert [(x.id, x.status) for x in left] == [(q.id, "waiting")]
