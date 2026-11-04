"""Suggesting, applying and undoing an AI decision, and never redoing what a person undid."""
import datetime as dt
from decimal import Decimal

import pytest

from ledgersync import ledger, ledger_book, undo
from ledgersync.agent.locks import ClientLocks
from ledgersync.errors import InvalidInput
from ledgersync.models import RowPatch
from test_resolutions import (ACTOR, YEAR, call, costco_split, keep_receipt, nb, pages, records,  # noqa: F401
                              saved)

pytestmark = pytest.mark.mongo

QUOTE = "total   £64.80"


@pytest.fixture
def locks():
    return ClientLocks()


def lines(store, records, nb):
    return [(l.code, l.debit, l.credit) for l in ledger_book.posted_trial_balance(store, records, nb.id, YEAR).lines]


def suggest(store, records, pages, nb):
    return call(store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE, trigger="background",
                review_id="r1", suggest=True)


def apply(store, records, locks, pages, nb, decision):
    return undo.apply_suggestion(store, records, locks, client_id=nb.id, decision_id=decision.id,
                                 texts=lambda uid: pages.get(uid, []))


def test_a_suggested_resolution_changes_no_row_and_records_before_and_after(mongo_store, records, pages, nb):
    d = suggest(mongo_store, records, pages, nb)
    assert (d.status, d.trigger, d.review_id) == ("suggested", "background", "r1")
    assert d.before == {str(nb.sheet_costco): {"excluded": False}}
    assert d.after == {str(nb.sheet_costco): {"excluded": True}}
    assert d.resolution == keep_receipt(nb).model_dump(mode="json")
    assert not saved(mongo_store, nb.id, nb.sheet_costco).excluded
    assert saved(mongo_store, nb.id, nb.sheet_costco).decision_ids == []


def test_applying_a_suggestion_makes_the_change_as_the_person(mongo_store, records, locks, pages, nb):
    s = suggest(mongo_store, records, pages, nb)
    made = apply(mongo_store, records, locks, pages, nb, s)
    assert (made.status, made.actor, made.action) == ("applied", "user", "keep_one")
    assert made.id != s.id and made.review_id == "r1"
    assert saved(mongo_store, nb.id, nb.sheet_costco).excluded
    assert records.applied_suggestions(nb.id) == {s.id: made.id}
    assert records.decisions(nb.id)[0].status == "suggested"   # the suggestion itself is never changed
    with pytest.raises(InvalidInput, match="applied already"):
        apply(mongo_store, records, locks, pages, nb, s)


def test_only_a_suggestion_can_be_applied(mongo_store, records, locks, pages, nb):
    d = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE)
    with pytest.raises(InvalidInput):
        apply(mongo_store, records, locks, pages, nb, d)


def test_a_background_resolution_on_a_row_a_person_changed_is_refused(mongo_store, records, pages, nb):
    ledger.change_row(mongo_store, nb.id, nb.sheet_costco, RowPatch(description="Costco stationery"))
    refused = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE, trigger="background")
    assert refused.status == "refused"
    assert refused.reason == f"A person changed row {nb.sheet_costco}; a background review leaves it to them."
    assert call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE).status == "applied"


def test_undo_puts_the_rows_back_and_records_a_reversal(mongo_store, records, locks, pages, nb):
    from ledgersync import upkeep
    from ledgersync.models import ChangeDraft
    upkeep.after_change(mongo_store, records, locks, ChangeDraft(client_id=nb.id, actor="user", kind="row_edited",
                                                                 summary="x"), today=dt.date(2026, 12, 1))
    before_lines = lines(mongo_store, records, nb)
    before_row = saved(mongo_store, nb.id, nb.sheet_costco)
    d = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE)
    upkeep.after_change(mongo_store, records, locks, ChangeDraft(client_id=nb.id, actor="user", kind="row_edited",
                                                                 summary="x"), today=dt.date(2026, 12, 1))
    assert lines(mongo_store, records, nb) != before_lines
    undone = undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=d.id)
    assert (undone.action, undone.status, undone.reverses, undone.actor) == ("undo", "applied", d.id, "user")
    assert saved(mongo_store, nb.id, nb.sheet_costco).excluded == before_row.excluded
    assert lines(mongo_store, records, nb) == before_lines
    assert any(c.kind == "decision_undone" and c.after == {"decision_id": d.id} for c in records.changes(nb.id))
    with pytest.raises(InvalidInput, match="undone already"):
        undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=d.id)


@pytest.mark.parametrize("trigger", ["chat", "background"])
def test_an_undone_change_is_refused_when_asked_again(mongo_store, records, locks, pages, nb, trigger):
    d = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE)
    undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=d.id)
    again = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE, trigger=trigger)
    assert (again.status, again.reason) == ("refused", "A person undid this change.")
    assert not saved(mongo_store, nb.id, nb.sheet_costco).excluded


def test_undo_refuses_when_a_row_changed_since(mongo_store, records, locks, pages, nb):
    d = call(mongo_store, records, pages, nb, nb.sheet_costco, keep_receipt(nb), QUOTE)
    mongo_store.patch_row(nb.id, nb.sheet_costco, {"excluded": False})
    with pytest.raises(InvalidInput) as err:
        undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=d.id)
    assert err.value.message == f"Row {nb.sheet_costco} has changed since; change it by hand instead."


def test_undoing_a_split_removes_its_parts(mongo_store, records, locks, pages, nb):
    d = call(mongo_store, records, pages, nb, nb.receipt, costco_split(), "Printer paper 52.80 A", "Biscuits 12.00 Z")
    assert d.status == "applied" and len(mongo_store.rows(nb.id)) == 6
    undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=d.id)
    assert len(mongo_store.rows(nb.id)) == 4
    assert saved(mongo_store, nb.id, nb.receipt).superseded_by is None


def test_undo_refuses_a_decision_that_is_not_an_applied_one_of_the_client(mongo_store, records, locks, pages, nb):
    s = suggest(mongo_store, records, pages, nb)
    for bad in (s.id, "nope"):
        with pytest.raises(InvalidInput):
            undo.undo_decision(mongo_store, records, locks, client_id=nb.id, decision_id=bad)
