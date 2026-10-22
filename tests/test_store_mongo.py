import datetime as dt
from decimal import Decimal

import pytest

from ledgersync.errors import LedgerSyncError
from ledgersync.models import ClientFields, Transaction
from ledgersync.store_mongo import MongoStore

pytestmark = pytest.mark.mongo

CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke",
                    contact_email="jenny@businesscube.co.uk")
ROW = Transaction(date=dt.date(2026, 9, 1), description="BT Business Broadband", direction="out",
                  gross=Decimal("72.00"), account_code="7502")


def test_revision_survives_a_new_store_on_the_same_database(mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.csv", "table", [ROW])
    again = MongoStore(mongo_store.uri, mongo_store.db_name)
    assert again.revision(client.id) == mongo_store.revision(client.id) > 0


def test_every_write_moves_the_revision(mongo_store):
    client = mongo_store.create_client(CUBE)
    seen = [mongo_store.revision(client.id)]
    upload_id = mongo_store.add_upload(client.id, "a.csv", "table", [ROW]); seen.append(mongo_store.revision(client.id))
    row_id = mongo_store.rows(client.id)[0].id
    mongo_store.patch_row(client.id, row_id, {"account_code": "7500"}); seen.append(mongo_store.revision(client.id))
    mongo_store.delete_upload(client.id, upload_id); seen.append(mongo_store.revision(client.id))
    assert seen == sorted(set(seen))


def test_client_edits_and_row_deletes_move_the_revision(mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.csv", "table", [ROW])
    before = mongo_store.revision(client.id)
    mongo_store.update_client(client.id, {"contact_name": "Jo"})
    after_edit = mongo_store.revision(client.id)
    mongo_store.delete_row(client.id, mongo_store.rows(client.id)[0].id)
    assert before < after_edit < mongo_store.revision(client.id)


def test_money_is_saved_as_an_exact_string(mongo_store, mongo_db):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.csv", "table", [ROW])
    saved = mongo_db.clients.find_one({"id": client.id})["rows"][0]["data"]
    assert saved["gross"] == "72.00"


class Interleaved:
    """The store's database with a clients collection that runs `meanwhile` just after patch_row reads the rows
    (its find_one on "rows.id"), as another writer would. `times` says for how many reads."""

    def __init__(self, db, meanwhile, times=1):
        self._db, self._meanwhile, self._left = db, meanwhile, times
        self.clients = self

    def find_one(self, query, *args, **kwargs):
        doc = self._db.clients.find_one(query, *args, **kwargs)
        if "rows.id" in query and self._left:
            self._left -= 1
            self._meanwhile()
        return doc

    def __getattr__(self, name):   # everything else goes to the real collection, or the real database
        return getattr(self._db.clients, name) if name in ("update_one", "find_one_and_update", "aggregate", "find") \
            else getattr(self._db, name)


def test_a_row_change_never_loses_rows_another_writer_added_meanwhile(mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.csv", "table", [ROW])
    row_id = mongo_store.rows(client.id)[0].id
    other = MongoStore(mongo_store.uri, mongo_store.db_name)   # the client's upload, saved by another thread
    mongo_store.db = Interleaved(mongo_store.db, lambda: other.add_upload(client.id, "b.csv", "table", [ROW, ROW]))
    mongo_store.patch_row(client.id, row_id, {"account_code": "7500"})
    rows = other.rows(client.id)
    assert len(rows) == 3
    assert [r.tx.account_code for r in rows if r.id == row_id] == ["7500"]


def test_a_row_change_that_keeps_losing_the_race_asks_the_person_to_try_again(mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "a.csv", "table", [ROW])
    row_id = mongo_store.rows(client.id)[0].id
    other = MongoStore(mongo_store.uri, mongo_store.db_name)
    mongo_store.db = Interleaved(mongo_store.db, lambda: other.add_upload(client.id, "b.csv", "table", [ROW]), times=5)
    with pytest.raises(LedgerSyncError, match="Try again"):
        mongo_store.patch_row(client.id, row_id, {"account_code": "7500"})
    assert len(other.rows(client.id)) == 6
    assert all(r.tx.account_code == "7502" for r in other.rows(client.id))
