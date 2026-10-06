import sqlite3
from contextlib import closing

import pytest

from ledgersync.errors import ClientArchived, NotFound, StorageError
from ledgersync.models import ClientFields
from ledgersync.store import Store

CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke",
                    contact_email="jenny@businesscube.co.uk")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data" / "ledgersync.db")


def test_the_database_is_created_on_first_use_with_its_schema_version(tmp_path):
    path = tmp_path / "new" / "ledgersync.db"
    store = Store(path)
    assert not path.exists()   # starting the API touches nothing until the database is used
    store.list_clients()
    with closing(sqlite3.connect(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert version == 1 and {"clients", "uploads", "rows"} <= tables


def test_a_database_that_cannot_be_opened_is_a_storage_error(tmp_path):
    (tmp_path / "taken").write_text("a file where the folder should be")
    with pytest.raises(StorageError):
        Store(tmp_path / "taken" / "ledgersync.db").list_clients()


def test_a_client_is_saved_and_read_back(store):
    client = store.create_client(CUBE)
    assert store.get_client(client.id) == client
    assert (client.name, client.vat_registered, client.archived, client.contact_phone) == (
        "Business Cube Ltd", True, False, "")


def test_clients_are_listed_by_name_and_archived_ones_apart(store):
    cube = store.create_client(CUBE)
    for name in ("Zeta Ltd", "acme trading"):
        store.create_client(CUBE.model_copy(update={"name": name}))
    assert [c.name for c in store.list_clients()] == ["acme trading", "Business Cube Ltd", "Zeta Ltd"]
    store.update_client(cube.id, {"archived": True})
    assert [c.name for c in store.list_clients()] == ["acme trading", "Zeta Ltd"]
    assert [c.name for c in store.list_clients(archived=True)] == ["Business Cube Ltd"]


def test_an_archived_client_can_only_be_restored(store):
    client = store.update_client(store.create_client(CUBE).id, {"archived": True})
    assert client.archived and client.archived_at
    with pytest.raises(ClientArchived):
        store.update_client(client.id, {"name": "Renamed"})
    assert store.update_client(client.id, {"archived": False}).archived is False


def test_editing_a_client_changes_only_what_is_sent_and_moves_updated(store, monkeypatch):
    client = store.create_client(CUBE)
    monkeypatch.setattr("ledgersync.store._now", lambda: "2030-01-01T00:00:00+00:00")
    changed = store.update_client(client.id, {"contact_phone": "07700 900123", "vat_registered": False})
    assert (changed.contact_phone, changed.vat_registered, changed.name) == ("07700 900123", False, "Business Cube Ltd")
    assert (changed.updated_at, changed.created_at) == ("2030-01-01T00:00:00+00:00", client.created_at)


def test_an_unknown_client_is_not_found(store):
    with pytest.raises(NotFound):
        store.get_client(99)
