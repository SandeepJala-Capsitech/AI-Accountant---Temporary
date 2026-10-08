import hashlib
import json
import threading
import time

import pytest
from fakes import ROW, FakeModel, transactions_json
from fastapi.testclient import TestClient

from ledgersync.config import Settings
from ledgersync.errors import ModelError
from ledgersync.store import Store
from server import create_app

CUBE = {"name": "Business Cube Ltd", "business_type": "limited_company", "contact_name": "Jenny Clarke",
        "contact_email": "jenny@businesscube.co.uk"}
BILL = {"direction": "out", "gross": "72.00", "account_code": "7502", "date": "2026-09-01", "description": "BT",
        "document_type": "invoice", "counterparty": "BT Business", "document_ref": "bt"}
LINE = {**BILL, "date": "2026-09-05", "document_type": "statement", "counterparty": "BT BUSINESS DD",
        "document_ref": "l1"}


@pytest.fixture
def api(tmp_path):
    opened = []

    def _make(model=None):
        app = create_app(Settings(warmup=False), model_client=model or FakeModel([]),
                         store=Store(tmp_path / "ledgersync.db"))
        client = TestClient(app, headers={"X-LedgerSync": "1"})   # as the LedgerSync pages send
        client.__enter__()
        opened.append(client)
        return client

    yield _make
    for client in opened:
        client.__exit__(None, None, None)


def add(client, **changes):
    resp = client.post("/api/clients", json={**CUBE, **changes})
    assert resp.status_code == 201, resp.text
    return resp.json()


def manual(client, client_id, *rows):
    resp = client.post(f"/api/clients/{client_id}/uploads", json={"transactions": list(rows)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_client_is_added_listed_and_read(api):
    client = api()
    added = add(client)
    assert (added["name"], added["archived"], added["vat_registered"]) == ("Business Cube Ltd", False, True)
    [summary] = client.get("/api/clients").json()
    assert (summary["id"], summary["rows"], summary["to_review"]) == (added["id"], 0, 0)
    assert client.get(f"/api/clients/{added['id']}").json() == added


def test_archiving_hides_a_client_until_it_is_restored(api):
    client = api()
    added = add(client)
    assert client.patch(f"/api/clients/{added['id']}", json={"archived": True}).json()["archived"] is True
    assert client.get("/api/clients").json() == []
    assert [c["id"] for c in client.get("/api/clients?archived=true").json()] == [added["id"]]
    refused = client.patch(f"/api/clients/{added['id']}", json={"name": "Renamed"})
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "client_archived")
    assert client.patch(f"/api/clients/{added['id']}", json={"archived": False}).json()["archived"] is False


@pytest.mark.parametrize("changes, message", [({"name": " "}, "Enter the company name."),
                                              ({"contact_name": ""}, "Enter the responsible person."),
                                              ({"contact_email": "jenny"}, "Enter a valid email.")])
def test_a_client_needs_its_details(api, changes, message):
    resp = api().post("/api/clients", json={**CUBE, **changes})
    assert (resp.status_code, resp.json()["detail"]) == (422, {"code": "invalid_input", "message": message})


def test_an_unknown_business_type_or_client_is_refused(api):
    client = api()
    assert client.post("/api/clients", json={**CUBE, "business_type": "charity"}).status_code == 422
    missing = client.get("/api/clients/99/ledger")
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "not_found")


def test_the_ledger_pairs_a_bill_and_its_payment_saved_in_two_uploads(api):
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL)
    ledger = manual(client, cid, LINE)
    bill, line = ledger["transactions"]
    assert ([s["ref"] for s in line["pays"]], bill["owed"]) == (["bt"], "0.00")
    assert [u["name"] for u in ledger["uploads"]] == ["Manual entry", "Manual entry"]
    assert (line["edited"], line["original"]) == (False, None)


def test_links_and_edits_rematch_the_ledger_and_revert_undoes_an_edit(api):
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL, LINE)
    bill_id, line_id = (r["id"] for r in client.get(f"/api/clients/{cid}/ledger").json()["transactions"])
    unlinked = client.patch(f"/api/clients/{cid}/rows/{line_id}", json={"link": []}).json()["transactions"]
    assert (unlinked[0]["owed"], unlinked[1]["pays"]) == ("72.00", [])
    [edited, _] = client.patch(f"/api/clients/{cid}/rows/{bill_id}", json={"gross": "70.00"}).json()["transactions"]
    assert (edited["gross"], edited["edited"], edited["original"]["gross"]) == ("70.00", True, "72.00")
    bad = client.patch(f"/api/clients/{cid}/rows/{bill_id}", json={"link": []})
    assert (bad.status_code, bad.json()["detail"]["message"]) == (
        422, "Only a bank line, or an item of an agent's statement, can be linked to documents.")
    [back, _] = client.patch(f"/api/clients/{cid}/rows/{bill_id}", json={"revert": True}).json()["transactions"]
    assert (back["gross"], back["edited"]) == ("72.00", False)


def test_removing_an_upload_leaves_a_stale_link(api):
    client = api()
    cid = add(client)["id"]
    bill_upload = manual(client, cid, BILL)["uploads"][0]["id"]
    manual(client, cid, {**LINE, "link": ["bt"]})
    [line] = client.delete(f"/api/clients/{cid}/uploads/{bill_upload}").json()["transactions"]
    assert "stale_link" in [i["code"] for i in line["issues"]]


def test_removing_a_copy_row_clears_its_warning_and_books_the_bill_again(api):
    # A person removes one row: the ledger is worked out again without it, and nothing else goes.
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL, {**BILL, "document_ref": "bt-again", "counterparty": "BT", "document_number": "BT-1"},
           LINE)
    [bill, copy, line] = client.get(f"/api/clients/{cid}/ledger").json()["transactions"]
    after = client.delete(f"/api/clients/{cid}/rows/{bill['id']}")
    assert after.status_code == 200, after.text
    assert [r["id"] for r in after.json()["transactions"]] == [copy["id"], line["id"]]
    assert client.delete(f"/api/clients/{cid}/rows/{bill['id']}").status_code == 404


def test_a_line_added_by_hand_joins_its_document_and_clears_its_total_warning(api):
    client = api()
    cid = add(client)["id"]
    claim = {"date": "2026-09-09", "description": "Jenny Hogg - car park", "direction": "out", "gross": "36.00",
             "account_code": "7400", "document_type": "expense_claim", "counterparty": "Jenny Hogg",
             "document_ref": "claim", "document_total": "90.00"}
    [first] = manual(client, cid, claim)["transactions"]
    assert "total_mismatch" in [i["code"] for i in first["issues"]]
    rows = manual(client, cid, {**claim, "date": "2026-09-24", "gross": "54.00", "document_total": None})["transactions"]
    assert [[i["code"] for i in r["issues"]] for r in rows] == [[], []] and {r["owed"] for r in rows} == {"90.00"}


def test_an_archived_client_takes_no_new_rows(api):
    client = api()
    cid = add(client)["id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    resp = client.post(f"/api/clients/{cid}/uploads", json={"transactions": [BILL]})
    assert (resp.status_code, resp.json()["detail"]["code"]) == (409, "client_archived")


def test_a_clients_trial_balance_uses_its_vat_setting(api):
    client = api()
    cid = add(client, vat_registered=False)["id"]
    manual(client, cid, {**BILL, "document_type": "receipt", "vat": "12.00"})
    lines = client.get(f"/api/clients/{cid}/trial-balance").json()["lines"]
    assert [(l["code"], l["debit"], l["credit"]) for l in lines] == [("1200", "0.00", "72.00"), ("7502", "72.00", "0.00")]


def test_accounts_can_be_listed_for_a_kind_of_business(api):
    client = api()
    company = {a["code"] for a in client.get("/api/accounts?business_type=limited_company").json()}
    assert "2250" in company and "3260" not in company and "1200" not in company
    assert "1200" in {a["code"] for a in client.get("/api/accounts").json()}   # the whole chart, as before


def test_the_browser_may_send_patch(api):
    preflight = api().options("/api/clients/1", headers={"Origin": "http://localhost:3000",
                                                          "Access-Control-Request-Method": "PATCH"})
    assert "PATCH" in preflight.headers.get("access-control-allow-methods", "")


class GatedModel(FakeModel):
    """Answers only once the test opens the gate, so the test can act while the job is running."""

    def __init__(self, replies, gate):
        super().__init__(replies)
        self.gate = gate

    def chat_json(self, messages, schema, images=None):
        self.gate.wait(5)
        return super().chat_json(messages, schema, images)


def finish(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def test_analysing_for_a_client_saves_the_rows_as_an_upload(api):
    model = FakeModel([transactions_json(ROW)])
    client = api(model)
    cid = add(client)["id"]
    data = b"Date,Description,Amount\n01/09/2026,BT Business Broadband,-72.00\n"
    resp = client.post("/api/analyze", data={"client_id": str(cid)},
                       files={"file": ("Barclays Sept.csv", data, "text/csv")})
    job = finish(client, resp.json()["job_id"])
    assert (job["status"], job["result"]["client_id"]) == ("succeeded", cid)
    [upload] = client.get(f"/api/clients/{cid}/ledger").json()["uploads"]
    assert (upload["id"], upload["name"], upload["kind"], upload["rows"]) == (
        job["result"]["upload_id"], "Barclays Sept.csv", "table", 1)
    assert upload["sha256"] == hashlib.sha256(data).hexdigest()
    assert "You keep the books of Business Cube Ltd, a UK limited company." in model.calls[0]["messages"][0]["content"]


def test_pasted_text_is_saved_and_the_clients_vat_setting_applies(api):
    client = api(FakeModel([transactions_json(dict(ROW, vat=12.0))]))
    cid = add(client, vat_registered=False)["id"]
    finish(client, client.post("/api/analyze", data={"text": "BT 72.00", "client_id": str(cid)}).json()["job_id"])
    ledger = client.get(f"/api/clients/{cid}/ledger").json()
    assert (ledger["uploads"][0]["name"], ledger["transactions"][0]["vat_posted"]) == ("Pasted text", "0.00")


@pytest.mark.parametrize("reply", [transactions_json(), ModelError("The AI model's answer was not valid JSON.")])
def test_nothing_is_saved_when_a_job_finds_no_rows_or_fails(api, reply):
    client = api(FakeModel([reply]))
    cid = add(client)["id"]
    finish(client, client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"])
    assert client.get(f"/api/clients/{cid}/ledger").json()["uploads"] == []


def test_a_post_from_another_web_page_cannot_add_rows(api):
    # Final review #4: a form on any web page could post here unasked and book rows to a client. The LedgerSync
    # pages send X-LedgerSync, which a form can't send.
    model = FakeModel([transactions_json(ROW)])
    client = api(model)
    cid = add(client)["id"]
    del client.headers["X-LedgerSync"]
    resp = client.post("/api/analyze", data={"text": "BT 72.00", "client_id": str(cid)})
    assert (resp.status_code, resp.json()["detail"]["code"]) == (403, "refused_request")
    assert model.calls == [] and client.get(f"/api/clients/{cid}/ledger").json()["uploads"] == []


def test_an_unknown_or_archived_client_is_refused_before_anything_is_queued(api):
    model = FakeModel([transactions_json(ROW)])
    client = api(model)
    assert client.post("/api/analyze", data={"text": "x", "client_id": "99"}).status_code == 404
    cid = add(client)["id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    archived = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)})
    assert (archived.status_code, archived.json()["detail"]["code"]) == (409, "client_archived")
    assert model.calls == []


def test_a_job_that_finishes_after_its_client_was_archived_is_still_saved(api):
    # Review focus: archiving stops new work, not work already running.
    gate = threading.Event()
    client = api(GatedModel([transactions_json(ROW)], gate))
    cid = add(client)["id"]
    job_id = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    gate.set()
    assert finish(client, job_id)["status"] == "succeeded"
    assert len(client.get(f"/api/clients/{cid}/ledger").json()["transactions"]) == 1


def test_a_cancelled_job_saves_nothing(api):
    gate = threading.Event()
    client = api(GatedModel([transactions_json(ROW)], gate))
    cid = add(client)["id"]
    job_id = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"]
    client.delete(f"/api/jobs/{job_id}")
    gate.set()
    assert finish(client, job_id)["status"] == "cancelled"
    assert client.get(f"/api/clients/{cid}/ledger").json()["uploads"] == []


def test_analysing_without_a_client_saves_nothing(api):
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    job = finish(client, client.post("/api/analyze", data={"text": "x"}).json()["job_id"])
    assert "upload_id" not in job["result"] and client.get(f"/api/clients/{cid}/ledger").json()["uploads"] == []


def test_the_export_downloads_with_the_clients_name_even_when_archived(api):
    client = api()
    cid = add(client, name="Café/Bar Ltd")["id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    resp = client.get(f"/api/clients/{cid}/export.xlsx")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''Caf%C3%A9%20Bar%20Ltd%20" in disposition and 'filename="Caf_ Bar Ltd ' in disposition


def test_a_statements_balances_reach_the_saved_rows_and_are_checked(api):
    reply = json.dumps({"transactions": [dict(ROW, document_type="statement", balance=928.0)],
                        "opening_balance": 1000.0, "closing_balance": 928.0})
    client = api(FakeModel([reply]))
    cid = add(client)["id"]
    finish(client, client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"])
    [upload] = client.get(f"/api/clients/{cid}/ledger").json()["uploads"]
    assert upload["statement"] == {"status": "ok", "difference": None}


def test_a_cancel_that_arrives_after_the_rows_were_saved_leaves_the_job_succeeded(api, monkeypatch):
    # Final review #2: the job was marked cancelled after its rows were saved, so Retry booked them twice.
    saving, release = threading.Event(), threading.Event()
    real_add = Store.add_upload

    def slow_add(self, *args, **kwargs):
        upload_id = real_add(self, *args, **kwargs)
        saving.set()
        release.wait(5)
        return upload_id

    monkeypatch.setattr(Store, "add_upload", slow_add)
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    job_id = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"]
    assert saving.wait(5)
    client.delete(f"/api/jobs/{job_id}")
    release.set()
    assert finish(client, job_id)["status"] == "succeeded"
    assert len(client.get(f"/api/clients/{cid}/ledger").json()["uploads"]) == 1
