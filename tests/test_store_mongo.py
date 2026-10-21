import datetime as dt
from decimal import Decimal

import pytest

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
