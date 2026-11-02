"""The agent's records: decisions (append-only, hash-chained, idempotent), questions, chat threads and cached
document texts, and splitting a row in the store."""
import datetime as dt
import hashlib
import json
from decimal import Decimal

import pytest
from pymongo.errors import DuplicateKeyError

from ledgersync.checks import booked
from ledgersync.errors import ChangedMeanwhile, NotFound
from ledgersync.models import ClientFields, DecisionDraft, Evidence, QuestionDraft, Transaction
from ledgersync.records import AgentRecords
from ledgersync.store_mongo import MongoStore
from tests.test_store_mongo import CUBE, Interleaved

pytestmark = pytest.mark.mongo

YEAR = dt.date(2025, 4, 1)


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


def draft(**changes) -> DecisionDraft:
    fields = dict(client_id=1, thread_id="t1", year_start=YEAR, action="exclude", rule="R2 Duplicate: kept the receipt",
                  status="applied", row_ids=[4], evidence=[Evidence(upload_id=2, upload_name="a.pdf", quote="Total 9.00")],
                  reason="Same payment twice", before={"4": {"excluded": False}}, after={"4": {"excluded": True}},
                  actor="agent:test-model")
    return DecisionDraft(**{**fields, **changes})


def question(**changes) -> QuestionDraft:
    fields = dict(client_id=1, year_start=YEAR, row_ids=[4], kind="date", text="When was this taxi?",
                  why="It has no date.", source="rules")
    return QuestionDraft(**{**fields, **changes})


def test_decision_is_idempotent(records):
    d1, new1 = records.append_decision(draft(), "k")
    d2, new2 = records.append_decision(draft(), "k")
    assert (new1, new2) == (True, False) and d1.id == d2.id and len(records.decisions(1)) == 1


def test_decisions_are_hash_chained(records):
    a, _ = records.append_decision(draft(), "a")
    b, _ = records.append_decision(draft(), "b")
    assert (a.prev_hash, b.prev_hash) == ("", a.hash)
    body = b.model_dump(mode="json", exclude={"hash"})
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert b.hash == hashlib.sha256((a.hash + canonical).encode()).hexdigest()


def test_each_client_has_its_own_chain(records):
    a, _ = records.append_decision(draft(client_id=1), "a")
    other, _ = records.append_decision(draft(client_id=2), "b")
    assert (a.prev_hash, other.prev_hash) == ("", "")


def test_decisions_are_filtered_by_year_and_time_and_kept_in_order(records):
    first, _ = records.append_decision(draft(), "a")
    later, _ = records.append_decision(draft(year_start=dt.date(2026, 4, 1)), "b")
    assert [d.id for d in records.decisions(1)] == [first.id, later.id]
    assert [d.id for d in records.decisions(1, YEAR)] == [first.id]
    assert [d.id for d in records.decisions(1, since=first.created_at)] == [first.id, later.id]
    assert records.decisions(1, since="9999-01-01") == []
    assert records.decisions(2) == []
    assert records.decisions(1)[0].evidence[0].quote == "Total 9.00"
    assert records.decisions(1)[0].year_start == YEAR


def test_the_idempotency_key_is_unique_in_the_database(records, mongo_db):
    d, _ = records.append_decision(draft(), "k")
    with pytest.raises(DuplicateKeyError):
        mongo_db.decisions.insert_one({"_id": "x", "idempotency_key": "k"})
    assert records.decisions(1)[0].id == d.id


def test_a_refused_decision_and_a_reversal_are_kept_as_given(records):
    d, _ = records.append_decision(draft(status="refused", reverses="abc"), "r")
    assert (d.status, d.reverses) == ("refused", "abc")


def test_question_upsert_keeps_one_per_key(records):
    q1 = records.upsert_question(question(), "date:4")
    records.set_question(q1.id, "waiting", "Not sure")
    q2 = records.upsert_question(question(text="What day was the taxi?", why="Still no date.", row_ids=[4, 5]), "date:4")
    assert (q2.id, q2.status, q2.answer, q2.created_at) == (q1.id, "waiting", "Not sure", q1.created_at)
    assert (q2.text, q2.why, q2.row_ids) == ("What day was the taxi?", "Still no date.", [4, 5])
    assert len(records.questions(1, YEAR)) == 1
    other = records.upsert_question(question(), "date:9")
    assert other.id != q1.id and q1.status == "open"


def test_questions_are_listed_by_status_and_resolving_sets_the_time(records):
    a = records.upsert_question(question(), "a")
    b = records.upsert_question(question(kind="vat_invoice"), "b")
    c = records.upsert_question(question(), "c")
    records.upsert_question(question(client_id=2), "a")      # another client may use the same key
    assert a.status == "open" and a.resolved_at is None and a.answer is None
    done = records.set_question(b.id, "resolved", "Here it is")
    assert (done.status, done.answer) == ("resolved", "Here it is") and done.resolved_at
    records.set_question(c.id, "waiting")
    assert [q.id for q in records.questions(1, YEAR)] == [a.id, c.id]
    assert [q.id for q in records.questions(1, YEAR, statuses=("resolved",))] == [b.id]
    assert records.questions(1, dt.date(2026, 4, 1)) == []
    with pytest.raises(NotFound):
        records.set_question("nope", "resolved")


def test_threads_are_created_listed_touched_and_deleted(records):
    t = records.create_thread(1, YEAR, "Taxis")
    other = records.create_thread(2, YEAR, "Rent")
    assert records.thread(t.id) == t and records.thread("nope") is None
    assert {x.id for x in records.threads()} == {t.id, other.id}
    assert [x.id for x in records.threads(1)] == [t.id]
    touched = records.touch_thread(t.id, title="Taxis 2025")
    assert touched.title == "Taxis 2025" and touched.updated_at >= t.updated_at and touched.created_at == t.created_at
    assert records.touch_thread(t.id).title == "Taxis 2025"
    records.delete_thread(t.id)
    assert records.thread(t.id) is None and [x.id for x in records.threads()] == [other.id]
    with pytest.raises(NotFound):
        records.touch_thread("nope")


def test_document_text_is_cached_by_hash(records, mongo_db):
    assert records.document_text("ab") is None
    records.save_document_text("ab", ["page one", "page two"], "vlm", "m1")
    records.save_document_text("ab", ["page one", "page two"], "vlm", "m1")   # saved again: no error
    assert records.document_text("ab") == ["page one", "page two"]
    saved = mongo_db.document_texts.find_one({"_id": "ab"})
    assert (saved["method"], saved["model"], saved["created_at"]) == ("vlm", "m1", saved["created_at"])


def test_records_use_the_database_they_are_given(records, mongo_db):
    assert records.db is mongo_db


# ─── MongoStore.add_split ──────────────────────────────────────────────────────

def lunch(**changes) -> Transaction:
    fields = dict(direction="out", gross="30.00", account_code="7500", date=dt.date(2025, 9, 1),
                  description="Cafe, lunch and wine", document_type="receipt", counterparty="Cafe", document_ref="cafe")
    return Transaction(**{**fields, **changes})


def test_add_split_supersedes_and_adds_parts(mongo_store, acme):
    mongo_store.add_upload(acme, "cafe.pdf", "pdf", [lunch(decision_ids=["d0"])], sha256="ab", year_start=YEAR)
    [original] = mongo_store.rows(acme)
    parts = [lunch(gross="20.00", vat="3.33", description="Lunch"), lunch(gross="10.00", vat="0.00", description="Wine")]
    new_ids = mongo_store.add_split(acme, original.id, parts, "dec1")
    rows = {r.id: r for r in mongo_store.rows(acme)}
    assert len(new_ids) == 2 and set(rows) == {original.id, *new_ids}
    old = rows[original.id].tx
    assert (old.superseded_by, old.decision_ids) == ("dec1", ["d0", "dec1"])
    assert not booked(old)
    assert [booked(rows[i].tx) for i in new_ids] == [True, True]
    assert sum(rows[i].tx.gross for i in new_ids) == old.gross == Decimal("30.00")
    assert [rows[i].tx.decision_ids for i in new_ids] == [["dec1"], ["dec1"]]
    assert rows[new_ids[0]].upload_id == rows[new_ids[1]].upload_id != original.upload_id
    split = next(u for u in mongo_store.uploads(acme) if u["id"] == rows[new_ids[0]].upload_id)
    assert (split["kind"], split["name"], split["sha256"], split["year_start"], split["row_count"]) == (
        "split", "cafe.pdf (split)", "", YEAR, 2)


def test_add_split_moves_the_revision_once(mongo_store, acme):
    mongo_store.add_upload(acme, "cafe.pdf", "pdf", [lunch()], year_start=YEAR)
    row_id = mongo_store.rows(acme)[0].id
    before = mongo_store.revision(acme)
    mongo_store.add_split(acme, row_id, [lunch(gross="30.00")], "dec1")
    assert mongo_store.revision(acme) == before + 1


def test_add_split_of_a_row_that_is_not_there_is_not_found(mongo_store, acme):
    with pytest.raises(NotFound):
        mongo_store.add_split(acme, 999, [lunch()], "d")
    other = mongo_store.create_client(ClientFields(name="Other Ltd", business_type="sole_trader", contact_name="Bo",
                                                      first_year_end=dt.date(2026, 3, 31)))
    mongo_store.add_upload(acme, "cafe.pdf", "pdf", [lunch()], year_start=YEAR)
    with pytest.raises(NotFound):
        mongo_store.add_split(other.id, mongo_store.rows(acme)[0].id, [lunch()], "d")


def test_add_split_keeps_rows_another_writer_added_meanwhile(mongo_store, mongo_client):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.pdf", "pdf", [lunch()], year_start=YEAR)
    row_id = mongo_store.rows(client.id)[0].id
    other = MongoStore(mongo_store.uri, mongo_store.db_name, client=mongo_client)
    mongo_store.db = Interleaved(mongo_store.db, lambda: other.add_upload(client.id, "b.pdf", "pdf", [lunch()]))
    mongo_store.add_split(client.id, row_id, [lunch(gross="30.00")], "d1")
    assert len(other.rows(client.id)) == 3


def test_add_split_that_keeps_losing_the_race_gives_up_and_changes_nothing(mongo_store, mongo_client):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.pdf", "pdf", [lunch()], year_start=YEAR)
    row_id = mongo_store.rows(client.id)[0].id
    other = MongoStore(mongo_store.uri, mongo_store.db_name, client=mongo_client)
    mongo_store.db = Interleaved(mongo_store.db, lambda: other.add_upload(client.id, "b.pdf", "pdf", [lunch()]), times=5)
    with pytest.raises(ChangedMeanwhile):
        mongo_store.add_split(client.id, row_id, [lunch()], "d1")
    assert all(r.tx.superseded_by is None for r in other.rows(client.id))
