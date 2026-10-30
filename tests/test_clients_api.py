import hashlib
import json
import threading
import time

import pytest
from fakes import ROW, FakeModel, transactions_json
from fastapi.testclient import TestClient

from ledgersync.config import Settings
from ledgersync.blobs import MemoryBlobStore
from ledgersync.errors import ModelError
from ledgersync.store_mongo import MongoStore
from server import create_app

CUBE = {"name": "Business Cube Ltd", "business_type": "limited_company", "contact_name": "Jenny Clarke",
        "contact_email": "jenny@businesscube.co.uk", "first_year_end": "2027-03-31"}
YEAR = "2026-04-01"   # the year its rows are in
BILL = {"direction": "out", "gross": "72.00", "account_code": "7502", "date": "2026-09-01", "description": "BT",
        "document_type": "invoice", "counterparty": "BT Business", "document_ref": "bt"}
LINE = {**BILL, "date": "2026-09-05", "document_type": "statement", "counterparty": "BT BUSINESS DD",
        "document_ref": "l1"}


@pytest.fixture
def api(mongo_store):
    opened = []

    def _make(model=None):
        app = create_app(Settings(warmup=False), model_client=model or FakeModel([]),
                         store=mongo_store)
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
    missing = client.get(f"/api/clients/99/years/{YEAR}/ledger")
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


def test_a_years_invoices_bank_checks_and_summary_are_served(api):
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL)
    year = f"/api/clients/{cid}/years/{YEAR}"
    [bill] = client.get(f"{year}/invoices").json()
    assert (bill["kind"], bill["total"], bill["paid"], bill["owed"]) == ("purchase", "72.00", "0.00", "72.00")
    [bank] = client.get(f"{year}/bank").json()
    assert (bank["code"], bank["books"], bank["statement"]) == ("1200", "0.00", None)
    summary = client.get(f"{year}/summary").json()
    assert (summary["rows"], summary["unpaid_purchases"], summary["trial_balance"]) == (1, "72.00", "balanced")
    for view in ("invoices", "bank", "summary"):
        assert client.get(f"/api/clients/{cid}/years/2020-04-01/{view}").status_code == 404


def test_links_and_edits_rematch_the_ledger_and_revert_undoes_an_edit(api):
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL, LINE)
    bill_id, line_id = (r["id"] for r in client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["transactions"])
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
    [bill, copy, line] = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["transactions"]
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
    lines = client.get(f"/api/clients/{cid}/years/{YEAR}/trial-balance").json()["lines"]
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
    [upload] = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"]
    assert (upload["id"], upload["name"], upload["kind"], upload["rows"]) == (
        job["result"]["upload_id"], "Barclays Sept.csv", "table", 1)
    assert upload["sha256"] == hashlib.sha256(data).hexdigest()
    assert "You keep the books of Business Cube Ltd, a UK limited company." in model.calls[0]["messages"][0]["content"]


def test_pasted_text_is_saved_and_the_clients_vat_setting_applies(api):
    client = api(FakeModel([transactions_json(dict(ROW, vat=12.0))]))
    cid = add(client, vat_registered=False)["id"]
    finish(client, client.post("/api/analyze", data={"text": "BT 72.00", "client_id": str(cid)}).json()["job_id"])
    ledger = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()
    assert (ledger["uploads"][0]["name"], ledger["transactions"][0]["vat_posted"]) == ("Pasted text", "0.00")


@pytest.mark.parametrize("reply", [transactions_json(), ModelError("The AI model's answer was not valid JSON.")])
def test_nothing_is_saved_when_a_job_finds_no_rows_or_fails(api, reply):
    client = api(FakeModel([reply]))
    cid = add(client)["id"]
    finish(client, client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"])
    assert client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


def test_a_post_from_another_web_page_cannot_add_rows(api):
    # Final review #4: a form on any web page could post here unasked and book rows to a client. The LedgerSync
    # pages send X-LedgerSync, which a form can't send.
    model = FakeModel([transactions_json(ROW)])
    client = api(model)
    cid = add(client)["id"]
    del client.headers["X-LedgerSync"]
    resp = client.post("/api/analyze", data={"text": "BT 72.00", "client_id": str(cid)})
    assert (resp.status_code, resp.json()["detail"]["code"]) == (403, "refused_request")
    assert model.calls == [] and client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


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
    assert len(client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["transactions"]) == 1


def test_a_cancelled_job_saves_nothing(api):
    gate = threading.Event()
    client = api(GatedModel([transactions_json(ROW)], gate))
    cid = add(client)["id"]
    job_id = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"]
    client.delete(f"/api/jobs/{job_id}")
    gate.set()
    assert finish(client, job_id)["status"] == "cancelled"
    assert client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


def test_analysing_without_a_client_saves_nothing(api):
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    job = finish(client, client.post("/api/analyze", data={"text": "x"}).json()["job_id"])
    assert "upload_id" not in job["result"]
    assert client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


def test_the_export_downloads_with_the_clients_name_even_when_archived(api):
    client = api()
    cid = add(client, name="Café/Bar Ltd")["id"]
    client.patch(f"/api/clients/{cid}", json={"archived": True})
    resp = client.get(f"/api/clients/{cid}/years/{YEAR}/export.xlsx")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''Caf%C3%A9%20Bar%20Ltd%20" in disposition and 'filename="Caf_ Bar Ltd ' in disposition


def test_a_statements_balances_reach_the_saved_rows_and_are_checked(api):
    reply = json.dumps({"transactions": [dict(ROW, document_type="statement", balance=928.0)],
                        "opening_balance": 1000.0, "closing_balance": 928.0})
    client = api(FakeModel([reply]))
    cid = add(client)["id"]
    finish(client, client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"])
    [upload] = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"]
    assert upload["statement"] == {"status": "ok", "difference": None}


def test_a_cancel_that_arrives_after_the_rows_were_saved_leaves_the_job_succeeded(api, monkeypatch):
    # Final review #2: the job was marked cancelled after its rows were saved, so Retry booked them twice.
    saving, release = threading.Event(), threading.Event()
    real_add = MongoStore.add_upload

    def slow_add(self, *args, **kwargs):
        upload_id = real_add(self, *args, **kwargs)
        saving.set()
        release.wait(5)
        return upload_id

    monkeypatch.setattr(MongoStore, "add_upload", slow_add)
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    job_id = client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"]
    assert saving.wait(5)
    client.delete(f"/api/jobs/{job_id}")
    release.set()
    assert finish(client, job_id)["status"] == "succeeded"
    assert len(client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"]) == 1


BT_CSV = b"Date,Description,Amount\n01/09/2026,BT Business Broadband,-72.00\n"


def test_an_analysed_file_can_be_downloaded_again(mongo_store):
    app = create_app(Settings(warmup=False), model_client=FakeModel([transactions_json(ROW)]),
                     store=mongo_store, blobs=MemoryBlobStore())
    with TestClient(app, headers={"X-LedgerSync": "1"}) as api:
        cid = add(api)["id"]
        resp = api.post("/api/analyze", data={"client_id": str(cid)}, files={"file": ("bt.csv", BT_CSV, "text/csv")})
        upload_id = finish(api, resp.json()["job_id"])["result"]["upload_id"]
        resp = api.get(f"/api/clients/{cid}/uploads/{upload_id}/file")
        assert resp.status_code == 200 and resp.content == BT_CSV
        assert 'filename="bt.csv"' in resp.headers["content-disposition"]
        assert resp.headers["content-type"].startswith("text/csv")


def test_pasted_text_has_no_file_to_download(api):
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    job = finish(client, client.post("/api/analyze", data={"text": "BT 72.00", "client_id": str(cid)}).json()["job_id"])
    resp = client.get(f"/api/clients/{cid}/uploads/{job['result']['upload_id']}/file")
    assert resp.status_code == 404 and resp.json()["detail"]["message"] == "The original file wasn't kept for this upload."


# ─── Bank accounts and credit cards ──────────────────────────────────────────

TWO_ACCOUNTS = [{"code": "1200", "name": "Barclays current", "kind": "current"},
                {"name": "Amex card", "kind": "credit_card"}]
STATEMENT_LINE = json.dumps({"transactions": [dict(ROW, document_type="statement")]})


def client_with_two_accounts(client) -> int:
    return add(client, bank_accounts=TWO_ACCOUNTS)["id"]


def test_a_client_is_saved_with_its_bank_accounts_and_cards(api):
    client = api()
    added = add(client, bank_accounts=TWO_ACCOUNTS)
    assert [(a["code"], a["name"], a["kind"]) for a in added["bank_accounts"]] == [
        ("1200", "Barclays current", "current"), ("1240", "Amex card", "credit_card")]
    assert add(client)["bank_accounts"] == [{"code": "1200", "name": "Bank current account", "kind": "current"}]
    codes = {a["code"] for a in client.get(f"/api/accounts?client_id={added['id']}").json()}
    assert {"1200", "1240"} <= codes and "1241" not in codes


def test_a_one_account_client_is_offered_the_accounts_it_was_before(api):
    client = api()
    codes = {a["code"] for a in client.get(f"/api/accounts?client_id={add(client)['id']}").json()}
    assert "1210" in codes and "1200" not in codes


def test_a_statement_for_a_client_with_two_accounts_must_say_which(api):
    # the fake model answers with one bank statement line
    client = api(FakeModel([STATEMENT_LINE]))
    cid = client_with_two_accounts(client)
    job = client.post("/api/analyze", data={"client_id": str(cid), "text": "Bank lines"}).json()
    failed = finish(client, job["job_id"])
    assert failed["status"] == "failed" and "Choose which bank account" in failed["error"]["message"]
    assert failed["error"]["message"] == "Choose which bank account this statement is for."
    assert client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


def test_a_statement_sent_with_its_account_is_saved_on_it_and_can_be_moved(api):
    model = FakeModel([STATEMENT_LINE])
    client = api(model)
    cid = client_with_two_accounts(client)
    job = client.post("/api/analyze", data={"client_id": str(cid), "text": "Amex lines", "bank_account": "1240"}).json()
    assert finish(client, job["job_id"])["status"] == "succeeded"
    ledger = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()
    [upload], [line] = ledger["uploads"], ledger["transactions"]
    assert (upload["bank_account"], line["bank_account"], line["contra_account_code"]) == ("1240", "1240", "1240")
    assert "1200 Bank Current Account" in model.calls[0]["messages"][0]["content"]   # paying the card off
    moved = client.patch(f"/api/clients/{cid}/uploads/{upload['id']}", json={"bank_account": "1200"}).json()
    assert (moved["uploads"][0]["bank_account"], moved["transactions"][0]["contra_account_code"]) == ("1200", "1200")


def test_a_bank_account_the_client_does_not_have_is_refused(api):
    client = api(FakeModel([STATEMENT_LINE]))
    cid = client_with_two_accounts(client)
    resp = client.post("/api/analyze", data={"client_id": str(cid), "text": "x", "bank_account": "1210"})
    assert (resp.status_code, resp.json()["detail"]["message"]) == (
        422, "Choose one of Business Cube Ltd's bank accounts.")


def test_a_client_with_one_account_has_its_statements_put_on_it(api):
    client = api(FakeModel([STATEMENT_LINE]))
    cid = add(client)["id"]
    finish(client, client.post("/api/analyze", data={"client_id": str(cid), "text": "x"}).json()["job_id"])
    [line] = client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["transactions"]
    assert (line["bank_account"], line["contra_account_code"]) == ("1200", "1200")


# ─── Years and opening balances ──────────────────────────────────────────────

def test_a_client_needs_its_first_year_end(api):
    resp = api().post("/api/clients", json={**CUBE, "first_year_end": ""})
    assert (resp.status_code, resp.json()["detail"]["message"]) == (422, "Enter the client's first year end.")


def test_a_clients_years_and_a_years_ledger_and_trial_balance(api):
    client = api()
    cid = add(client, first_year_end="2026-03-31")["id"]
    manual(client, cid, {**BILL, "document_type": "receipt"})
    years = client.get(f"/api/clients/{cid}/years").json()
    assert [(y["start"], y["end"], y["label"], y["closed"]) for y in years][:2] == [
        ("2025-04-01", "2026-03-31", "Year to 31 Mar 2026", False),
        ("2026-04-01", "2027-03-31", "Year to 31 Mar 2027", False)]
    first = client.get(f"/api/clients/{cid}/years/2025-04-01/ledger").json()
    second = client.get(f"/api/clients/{cid}/years/2026-04-01/ledger").json()
    assert (first["transactions"], len(second["transactions"])) == ([], 1)
    assert second["year"]["label"] == "Year to 31 Mar 2027"
    tb = client.get(f"/api/clients/{cid}/years/2026-04-01/trial-balance").json()
    assert (tb["opening"], tb["is_balanced"]) == ([], True)
    assert client.get(f"/api/clients/{cid}/years/2026-04-01/export.xlsx").status_code == 200


@pytest.mark.parametrize("path", ["ledger", "trial-balance", "export.xlsx"])
def test_a_year_the_client_does_not_have_is_not_found(api, path):
    client = api()
    cid = add(client)["id"]
    resp = client.get(f"/api/clients/{cid}/years/2026-04-02/{path}")
    assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "That year was not found.")


def test_opening_balances_are_saved_and_read_back(api):
    client = api()
    cid = add(client)["id"]
    assert client.get(f"/api/clients/{cid}/opening-balances").json() == {"lines": [], "unpaid": []}
    opening = {"lines": [{"code": "1100", "debit": "200.00", "credit": "0.00"},
                         {"code": "3200", "debit": "0.00", "credit": "200.00"}],
               "unpaid": [{"id": "1", "direction": "in", "counterparty": "Bright Cafe", "amount": "200.00",
                           "number": "INV-7", "date": None}]}
    saved = client.put(f"/api/clients/{cid}/opening-balances", json=opening)
    assert (saved.status_code, saved.json()) == (200, opening)
    tb = client.get(f"/api/clients/{cid}/years/{YEAR}/trial-balance").json()
    assert [(l["code"], l["debit"], l["credit"]) for l in tb["opening"]] == [("1100", "200.00", "0.00"),
                                                                             ("3200", "0.00", "200.00")]
    refused = client.put(f"/api/clients/{cid}/opening-balances", json={"lines": [opening["lines"][0]]})
    assert (refused.status_code, refused.json()["detail"]["message"]) == (
        422, "Opening debits £200.00 and credits £0.00 must be the same.")


def test_the_browser_may_send_put(api):
    preflight = api().options("/api/clients/1/opening-balances", headers={"Origin": "http://localhost:3000",
                                                                          "Access-Control-Request-Method": "PUT"})
    assert "PUT" in preflight.headers.get("access-control-allow-methods", "")


def test_an_analysis_is_added_to_the_year_picked(api):
    client = api(FakeModel([transactions_json(dict(ROW, date=None))]))
    cid = add(client, first_year_end="2026-03-31")["id"]
    refused = client.post("/api/analyze", data={"text": "x", "client_id": str(cid), "year_start": "2025-04-02"})
    assert (refused.status_code, refused.json()["detail"]["message"]) == (404, "That year was not found.")
    job = client.post("/api/analyze", data={"text": "x", "client_id": str(cid), "year_start": "2025-04-01"}).json()
    assert finish(client, job["job_id"])["status"] == "succeeded"
    first = client.get(f"/api/clients/{cid}/years/2025-04-01/ledger").json()
    assert first["uploads"][0]["year_start"] == "2025-04-01" and len(first["transactions"]) == 1
    assert client.get(f"/api/clients/{cid}/years/2026-04-01/ledger").json()["transactions"] == []


def test_a_year_is_closed_and_reopened(api):
    client = api()
    cid = add(client)["id"]
    manual(client, cid, BILL)
    closed = client.post(f"/api/clients/{cid}/years/{YEAR}/close")
    assert closed.status_code == 200
    [saved] = closed.json()["closed_years"]
    assert (saved["start"], saved["end"], saved["closing"]) == (YEAR, "2027-03-31",
                                                                {"2100": "-72.00", "7502": "72.00"})
    assert [y["closed"] for y in client.get(f"/api/clients/{cid}/years").json()] == [True]
    refused = client.post(f"/api/clients/{cid}/uploads", json={"transactions": [LINE]})
    assert (refused.status_code, refused.json()["detail"]) == (409, {
        "code": "year_closed", "message": "The year to 31 Mar 2027 is closed. Reopen it to change it."})
    moved = client.patch(f"/api/clients/{cid}", json={"first_year_end": "2027-12-31"})
    assert (moved.status_code, moved.json()["detail"]["message"]) == (
        422, "The year end can't change once a year is closed.")
    reopened = client.post(f"/api/clients/{cid}/years/{YEAR}/reopen")
    assert (reopened.status_code, reopened.json()["closed_years"]) == (200, [])
    assert client.post(f"/api/clients/{cid}/uploads", json={"transactions": [LINE]}).status_code == 201


def test_an_analysis_isnt_saved_into_a_closed_year(api):
    client = api(FakeModel([transactions_json(ROW)]))
    cid = add(client)["id"]
    client.post(f"/api/clients/{cid}/years/{YEAR}/close")
    job = finish(client, client.post("/api/analyze", data={"text": "x", "client_id": str(cid)}).json()["job_id"])
    assert (job["status"], job["error"]["code"]) == ("failed", "year_closed")
    assert client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"] == []


# ─── Importing a client's files from its folder ──────────────────────────────

def import_folder(client, cid, **body):
    return client.post(f"/api/clients/{cid}/import-folder", json={"year_start": YEAR, **body})


def test_import_without_a_folder_is_refused(api):
    client = api()
    cid = add(client)["id"]
    r = import_folder(client, cid)
    assert r.status_code == 422 and "Set this client's folder first" in r.text


def test_a_folder_that_is_set_must_exist(api, tmp_path):
    client = api()
    assert add(client, folder=str(tmp_path))["folder"] == str(tmp_path)
    missing = client.post("/api/clients", json={**CUBE, "folder": str(tmp_path / "nope")})
    assert (missing.status_code, missing.json()["detail"]["message"]) == (
        422, "That folder doesn't exist on this computer.")
    cid = add(client)["id"]
    assert client.patch(f"/api/clients/{cid}", json={"folder": str(tmp_path)}).json()["folder"] == str(tmp_path)
    assert client.patch(f"/api/clients/{cid}", json={"folder": ""}).json()["folder"] == ""


def test_importing_reads_new_files_once_and_reports_the_rest(mongo_store, tmp_path):
    (tmp_path / "bt.csv").write_bytes(BT_CSV)
    (tmp_path / "bt copy.csv").write_bytes(BT_CSV)
    (tmp_path / "backup.zip").write_bytes(b"PK")
    (tmp_path / "empty.csv").write_bytes(b"")
    app = create_app(Settings(warmup=False), model_client=FakeModel([transactions_json(ROW)]),
                     store=mongo_store, blobs=MemoryBlobStore())
    with TestClient(app, headers={"X-LedgerSync": "1"}) as api:
        cid = add(api, folder=str(tmp_path))["id"]
        done = import_folder(api, cid)
        assert done.status_code == 202, done.text
        body = done.json()
        assert [j["name"] for j in body["jobs"]] == ["bt copy.csv"]
        assert (body["skipped"], body["unreadable"]) == (["bt.csv"], ["backup.zip"])
        assert body["refused"] == [{"name": "empty.csv", "message": "'empty.csv' is empty."}]
        job = finish(api, body["jobs"][0]["job_id"])
        assert job["status"] == "succeeded"
        [upload] = api.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"]
        assert (upload["name"], upload["sha256"]) == ("bt copy.csv", hashlib.sha256(BT_CSV).hexdigest())
        again = import_folder(api, cid).json()      # the same contents are known now
        assert again["jobs"] == [] and again["skipped"] == ["bt copy.csv", "bt.csv"]
        assert api.get(f"/api/clients/{cid}/uploads/{upload['id']}/file").content == BT_CSV


def test_importing_checks_the_header_the_year_the_account_and_a_closed_year(api, tmp_path):
    client = api()
    cid = add(client, folder=str(tmp_path))["id"]
    unknown = import_folder(client, cid, year_start="2020-04-01")
    assert (unknown.status_code, unknown.json()["detail"]["message"]) == (404, "That year was not found.")
    assert import_folder(client, cid, bank_account="9999").status_code == 422
    assert import_folder(client, 99).status_code == 404
    client.post(f"/api/clients/{cid}/years/{YEAR}/close")
    closed = import_folder(client, cid)
    assert (closed.status_code, closed.json()["detail"]["code"]) == (409, "year_closed")
    del client.headers["X-LedgerSync"]
    assert import_folder(client, cid).status_code == 403


def test_a_missing_folder_doesnt_block_other_edits(api, tmp_path):
    client = api()
    folder = tmp_path / "acme"
    folder.mkdir()
    cid = add(client, folder=str(folder))["id"]
    folder.rmdir()
    renamed = client.patch(f"/api/clients/{cid}", json={"name": "Renamed Ltd"})
    assert (renamed.status_code, renamed.json()["name"], renamed.json()["folder"]) == (200, "Renamed Ltd", str(folder))
    refused = client.patch(f"/api/clients/{cid}", json={"folder": str(folder)})
    assert (refused.status_code, refused.json()["detail"]["message"]) == (
        422, "That folder doesn't exist on this computer.")
    assert client.patch(f"/api/clients/{cid}", json={"folder": ""}).json()["folder"] == ""


def test_a_second_import_while_the_first_is_reading_skips_its_files(api, tmp_path):
    (tmp_path / "bt.csv").write_bytes(BT_CSV)
    gate = threading.Event()
    client = api(GatedModel([transactions_json(ROW)], gate))
    cid = add(client, folder=str(tmp_path))["id"]
    try:
        first = import_folder(client, cid).json()
        [started] = first["jobs"]
        second = import_folder(client, cid).json()
        assert (second["jobs"], second["skipped"]) == ([], ["bt.csv"])
    finally:
        gate.set()
    assert finish(client, started["job_id"])["status"] == "succeeded"
    assert len(client.get(f"/api/clients/{cid}/years/{YEAR}/ledger").json()["uploads"]) == 1


def test_a_file_is_read_again_after_its_job_failed(api, tmp_path):
    (tmp_path / "bt.csv").write_bytes(BT_CSV)
    client = api(FakeModel([ModelError("The AI model's answer was not valid JSON."), transactions_json(ROW)]))
    cid = add(client, folder=str(tmp_path))["id"]
    [first] = import_folder(client, cid).json()["jobs"]
    assert finish(client, first["job_id"])["status"] == "failed"
    [again] = import_folder(client, cid).json()["jobs"]    # nothing was saved, and nothing is still reading it
    assert finish(client, again["job_id"])["status"] == "succeeded"
