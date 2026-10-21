import logging
import threading
from unittest import mock

import pytest
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

from ledgersync.records.audit import AuditLog

pytestmark = pytest.mark.mongo


def test_entries_chain_and_verify(mongo_db):
    log = AuditLog(mongo_db)
    first = log.append(actor="agent", action="update_row", client_id=1, job_id="j1", target={"row_id": 5},
                       before={"account_code": "9998"}, after={"account_code": "7502"}, note="Broadband bill")
    second = log.append(actor="agent", action="update_row", client_id=1, job_id="j1", target={"row_id": 6},
                        before={"include": False}, after={"include": True})
    assert first["prev_hash"] == "0" * 64 and second["prev_hash"] == first["hash"]
    assert log.verify() is None


def test_an_edited_entry_breaks_the_chain_at_that_entry(mongo_db):
    log = AuditLog(mongo_db)
    log.append(actor="agent", action="update_row", client_id=1, job_id="j1", target={"row_id": 5}, before=None, after={"a": 1})
    log.append(actor="agent", action="update_row", client_id=1, job_id="j1", target={"row_id": 6}, before=None, after={"a": 2})
    mongo_db.audit_log.update_one({"seq": 1}, {"$set": {"after": {"a": 9}}})
    assert log.verify() == 1


def test_entries_are_listed_per_client_and_job_in_order(mongo_db):
    log = AuditLog(mongo_db)
    for client_id, job_id in [(1, "j1"), (2, "j2"), (1, "j1"), (1, "j3")]:
        log.append(actor="agent", action="x", client_id=client_id, job_id=job_id, target={}, before=None, after=None)
    assert [e["seq"] for e in log.entries(client_id=1, job_id="j1")] == [1, 3]


def test_entries_without_a_job_lists_every_job_of_the_client(mongo_db):
    log = AuditLog(mongo_db)
    for client_id, job_id in [(1, "j1"), (2, "j2"), (1, None), (1, "j3")]:
        log.append(actor="system", action="x", client_id=client_id, job_id=job_id, target={}, before=None, after=None)
    assert [e["seq"] for e in log.entries(client_id=1)] == [1, 3, 4]


def test_an_entry_has_the_documented_fields_and_no_mongo_id(mongo_db):
    log = AuditLog(mongo_db)
    saved = log.append(actor="a@b.co", action="update_row", client_id=1, job_id="j1", target={"row_id": 5},
                       before={"x": 1}, after={"x": 2}, note="why")
    assert "_id" not in saved
    assert saved["seq"] == 1 and saved["actor"] == "a@b.co" and saved["note"] == "why"
    assert saved["at"].endswith("+00:00") and len(saved["hash"]) == 64
    assert log.entries(client_id=1) == [saved]


def test_a_second_log_on_the_same_database_continues_the_chain(mongo_db):
    first = AuditLog(mongo_db).append(actor="agent", action="x", client_id=1, job_id="j1", target={}, before=None, after=None)
    other = AuditLog(mongo_db)
    second = other.append(actor="agent", action="x", client_id=1, job_id="j2", target={}, before=None, after=None)
    assert second["seq"] == 2 and second["prev_hash"] == first["hash"]
    assert other.verify() is None


def test_every_kind_of_value_survives_the_round_trip_and_still_verifies(mongo_db):
    log = AuditLog(mongo_db)
    log.append(actor="agent", action="update_row", client_id=1, job_id="j1",
               target={"row_id": 5, "path": ["a", {"b": None}]},
               before={"n": None, "f": 0.1 + 0.2, "i": 10 ** 12, "s": "café £", "nested": {"z": 1, "a": {"y": 2.5}}},
               after={"flag": True, "money": "12.50"}, note="café")
    log.append(actor="agent", action="x", client_id=1, job_id=None, target={}, before=None, after=None)
    assert log.verify() is None


def test_deleting_an_entry_breaks_the_chain_at_the_next_one(mongo_db):
    log = AuditLog(mongo_db)
    for n in range(3):
        log.append(actor="agent", action="x", client_id=1, job_id="j1", target={"n": n}, before=None, after=None)
    mongo_db.audit_log.delete_one({"seq": 2})
    assert log.verify() == 3


def test_an_empty_log_verifies(mongo_db):
    assert AuditLog(mongo_db).verify() is None


def test_the_log_cannot_be_updated_or_deleted_through_the_class():
    assert not any(hasattr(AuditLog, name) for name in ("update", "delete", "remove", "edit", "clear"))


def test_parallel_appends_from_several_logs_keep_one_unbroken_chain(mongo_db):
    errors = []

    def work(worker):
        try:
            log = AuditLog(mongo_db)
            for n in range(5):
                log.append(actor="agent", action="x", client_id=1, job_id=f"j{worker}", target={"n": n}, before=None, after=None)
        except Exception as exc:   # surfaced below, not lost in the thread
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [e["seq"] for e in AuditLog(mongo_db).entries(client_id=1)] == list(range(1, 21))
    assert AuditLog(mongo_db).verify() is None


def test_a_document_with_no_seq_is_reported_not_passed_over(mongo_db):
    log = AuditLog(mongo_db)
    log.append(actor="agent", action="x", client_id=1, job_id="j1", target={}, before=None, after=None)
    mongo_db.audit_log.update_one({"seq": 1}, {"$unset": {"seq": ""}})
    assert log.verify() == 0


def test_a_second_log_on_the_same_database_makes_no_index_call(mongo_db):
    AuditLog(mongo_db)   # the first one creates the unique seq index
    with mock.patch.object(Collection, "create_index") as create_index:
        log = AuditLog(mongo_db)
        log.append(actor="agent", action="x", client_id=1, job_id="j1", target={}, before=None, after=None)
    create_index.assert_not_called()


def test_a_refused_index_is_logged_once_and_does_not_stop_the_log(mongo_db, caplog):
    refused = OperationFailure("not authorized on db to execute command createIndexes", code=13)
    with caplog.at_level(logging.ERROR, logger="ledgersync.records.audit"):
        with mock.patch.object(Collection, "create_index", side_effect=refused) as create_index:
            log = AuditLog(mongo_db)    # must not raise
            AuditLog(mongo_db)          # already tried for this database, so no second call or message
    assert create_index.call_count == 1
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(messages) == 1
    assert "seq" in messages[0] and "audit_log" in messages[0] and "createIndex" in messages[0]
    first = log.append(actor="agent", action="x", client_id=1, job_id="j1", target={}, before=None, after=None)
    assert first["seq"] == 1 and log.verify() is None


def test_any_other_index_error_still_raises_and_is_tried_again_next_time(mongo_db):
    with mock.patch.object(Collection, "create_index", side_effect=RuntimeError("network down")):
        with pytest.raises(RuntimeError, match="network down"):
            AuditLog(mongo_db)
    AuditLog(mongo_db)   # not remembered as done, so this one creates the index for real
    assert "seq_1" in mongo_db.audit_log.index_information()
