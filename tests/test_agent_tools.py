"""The row tools the agent works with, and the ingest step: what each tool returns, that none of them ever raises,
that the agent can change only a row's account, document type, Include, Link and Not the same, and that every
write is audited once."""
import datetime as dt

import pytest

from ledgersync import ledger
from ledgersync.agent.nodes import make_ingest
from ledgersync.agent.tools import ToolDeps, row_tools
from ledgersync.models import ClientFields, RowPatch, Transaction
from ledgersync.records.audit import AuditLog
from ledgersync.records.jobs import JobRecords

pytestmark = pytest.mark.mongo

PERIOD = {"period_start": "2025-04-01", "period_end": "2026-03-31"}
YEAR = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke")
OTHER = ClientFields(name="Other Traders", business_type="sole_trader", contact_name="Sam Other")
NO_DATE_WARNING = {"code": "date_missing", "message": "No date. Add one.", "severity": "warning", "related": []}


def row(**changes) -> Transaction:
    fields = dict(direction="out", gross="72.00", account_code="9998", date=dt.date(2026, 9, 1),
                  description="BT Business broadband", document_type="invoice", counterparty="BT Business",
                  document_ref="bt")
    return Transaction(**{**fields, **changes})


def start_job(records, client_id):
    return records.create(client_id, *YEAR)


@pytest.fixture
def records(mongo_db):
    return JobRecords(mongo_db)


@pytest.fixture
def audit(mongo_db):
    return AuditLog(mongo_db)


@pytest.fixture
def deps(mongo_store, records, audit):
    return ToolDeps(store=mongo_store, records=records, audit=audit)


@pytest.fixture
def cube_id(mongo_store):
    """Two documents and five rows: BT's bill (on Suspense, dated), then four receipts, of which three have no date."""
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "BT bill.pdf", "pdf", [
        row(),
        row(description="Mystery receipt", date=None, gross="15.00", account_code="7504", document_type="receipt",
            counterparty="Corner Shop", document_ref="r2"),
        row(description="Train fare", date=None, gross="24.00", account_code="7400", document_type="receipt",
            counterparty="Rail Co", document_ref="r3")])
    mongo_store.add_upload(client.id, "Receipts.csv", "table", [
        row(description="Software", gross="12.00", account_code="8201", document_type="receipt",
            counterparty="Soft Ltd", document_ref="r4"),
        row(description="Cafe", date=None, gross="6.50", account_code="7406", document_type="receipt",
            counterparty="Cafe Nero", document_ref="r5")])
    return client.id


@pytest.fixture
def other_id(mongo_store, cube_id):
    client = mongo_store.create_client(OTHER)
    mongo_store.add_upload(client.id, "Their bill.pdf", "pdf", [row(document_ref="theirs")])
    return client.id


@pytest.fixture
def job(records, cube_id):
    return start_job(records, cube_id)


@pytest.fixture
def tools(deps, cube_id):
    return row_tools(deps, cube_id, "j1")


@pytest.fixture
def row_id(mongo_store, cube_id):
    """BT's bill: on Suspense, with nothing wrong with it."""
    return mongo_store.rows(cube_id)[0].id


@pytest.fixture
def other_clients_row_id(mongo_store, other_id):
    return mongo_store.rows(other_id)[0].id


@pytest.fixture
def three_rows_to_review(mongo_store, cube_id):
    """The ids of the three rows with no date."""
    return [r.id for r in mongo_store.rows(cube_id) if r.tx.date is None]


def update(tools, row_id, reasoning="BT broadband bill", confidence=0.9, **fields):
    return tools["update_row"].invoke({"row_id": row_id, "reasoning": reasoning, "confidence": confidence, **fields})


def saved(mongo_store, client_id, row_id):
    return next(r for r in mongo_store.rows(client_id) if r.id == row_id)


# --- the tools the agent is given ---------------------------------------------------------------------------

def test_the_agent_gets_six_tools_by_name(tools):
    assert set(tools) == {"lookup_account", "list_rows_to_review", "get_row", "update_row", "revert_row", "list_rows"}
    assert all(t.name == name and t.description for name, t in tools.items())


def test_lookup_account_finds_by_code_and_by_words(tools):
    assert tools["lookup_account"].invoke({"query": "7502"})[0]["code"] == "7502"
    assert "7502" in [a["code"] for a in tools["lookup_account"].invoke({"query": "telephone internet"})]


def test_lookup_account_gives_code_name_and_type_and_at_most_ten(tools):
    [found] = tools["lookup_account"].invoke({"query": "7502"})
    assert found == {"code": "7502", "name": "Telephone and Internet", "type": "expense"}
    assert len(tools["lookup_account"].invoke({"query": "7"})) == 10


def test_lookup_account_offers_only_the_accounts_this_kind_of_business_can_choose(deps, cube_id, other_id):
    limited = row_tools(deps, cube_id, "j1")["lookup_account"]
    sole = row_tools(deps, other_id, "j1")["lookup_account"]
    assert limited.invoke({"query": "3260"}) == [] and sole.invoke({"query": "2250"}) == []   # drawings / director's loan
    assert sole.invoke({"query": "3260"})[0]["code"] == "3260"


def test_lookup_account_asks_for_something_to_look_for(tools):
    assert tools["lookup_account"].invoke({"query": "  "}) == {
        "ok": False, "error": "Enter part of an account's code or name."}


def test_update_row_changes_the_account_and_is_audited_once(tools, audit, cube_id, row_id):
    args = {"row_id": row_id, "account_code": "7502", "reasoning": "BT broadband bill", "confidence": 0.9}
    assert tools["update_row"].invoke(args)["ok"] is True
    tools["update_row"].invoke(args)                     # the same call replayed after a restart
    entries = audit.entries(client_id=cube_id, job_id="j1")
    assert len(entries) == 1 and entries[0]["after"] == {"account_code": "7502"}
    assert entries[0]["note"] == "BT broadband bill (confidence 0.9)"


def test_the_audit_entry_names_the_agent_the_tool_the_row_and_what_changed(tools, audit, cube_id, row_id):
    update(tools, row_id, account_code="7502")
    [entry] = audit.entries(client_id=cube_id)
    assert (entry["actor"], entry["action"], entry["client_id"], entry["job_id"], entry["target"]) == (
        "agent", "update_row", cube_id, "j1", {"row_id": row_id})
    assert (entry["before"], entry["after"]) == ({"account_code": "9998"}, {"account_code": "7502"})
    assert audit.verify() is None


def test_update_row_returns_the_short_row_with_money_as_exact_strings(tools, row_id):
    result = update(tools, row_id, account_code="7502")
    assert result == {"ok": True, "row": {
        "row_id": row_id, "date": "2026-09-01", "description": "BT Business broadband", "counterparty": "BT Business",
        "direction": "out", "gross": "72.00", "vat": None, "account_code": "7502", "document_type": "invoice",
        "upload": {"id": result["row"]["upload"]["id"], "name": "BT bill.pdf"}, "issues": []}}


def test_update_row_leaves_the_amount_the_date_and_the_vat_as_they_were(tools, mongo_store, cube_id, row_id):
    before = saved(mongo_store, cube_id, row_id).tx
    update(tools, row_id, account_code="7502", document_type="receipt", include=True)
    after = saved(mongo_store, cube_id, row_id).tx
    assert (after.gross, after.date, after.vat, after.direction, after.description, after.counterparty) == (
        before.gross, before.date, before.vat, before.direction, before.description, before.counterparty)
    assert (after.account_code, after.document_type, after.include) == ("7502", "receipt", True)


def test_update_row_changes_the_document_type_and_audits_only_what_changed(tools, audit, cube_id, row_id):
    update(tools, row_id, account_code="9998", document_type="receipt")   # the account is already 9998
    [entry] = audit.entries(client_id=cube_id)
    assert (entry["before"], entry["after"]) == ({"document_type": "invoice"}, {"document_type": "receipt"})


def test_update_row_cant_touch_another_clients_row(tools, audit, cube_id, other_id, other_clients_row_id, mongo_store):
    result = tools["update_row"].invoke({"row_id": other_clients_row_id, "account_code": "7502",
                                         "reasoning": "x", "confidence": 0.5})
    assert result == {"ok": False, "error": "This row was not found."}
    assert saved(mongo_store, other_id, other_clients_row_id).tx.account_code == "9998"
    assert audit.entries(client_id=cube_id) == [] and audit.entries(client_id=other_id) == []


def test_update_row_has_no_amount_date_or_vat_arguments(tools):
    assert {"gross", "date", "vat", "client_id", "job_id"}.isdisjoint(tools["update_row"].args)
    assert set(tools["update_row"].args) == {"row_id", "reasoning", "confidence", "account_code", "document_type",
                                             "include", "link", "apart_from"}
    for name, tool in tools.items():
        assert {"client_id", "job_id"}.isdisjoint(tool.args), name


def test_update_row_sends_only_the_fields_given_and_never_the_ones_it_may_not_change(tools, deps, monkeypatch, row_id):
    sent = []
    real = ledger.change_row
    monkeypatch.setattr(ledger, "change_row", lambda store, cid, rid, patch: sent.append(patch) or real(store, cid, rid, patch))
    update(tools, row_id, account_code="7502", include=True)
    [patch] = sent
    assert type(patch) is RowPatch and patch.model_fields_set == {"account_code", "include"}


@pytest.mark.parametrize("reasoning, confidence, error", [
    ("", 0.5, "Give your reasoning in 1 to 500 characters."),
    ("   ", 0.5, "Give your reasoning in 1 to 500 characters."),
    ("x" * 501, 0.5, "Give your reasoning in 1 to 500 characters."),
    ("ok", 1.5, "Confidence must be from 0 to 1."),
    ("ok", -0.1, "Confidence must be from 0 to 1."),
    ("ok", float("nan"), "Confidence must be from 0 to 1.")])
def test_update_row_refuses_bad_reasoning_or_confidence_without_raising_or_writing(
        tools, audit, cube_id, mongo_store, row_id, reasoning, confidence, error):
    result = update(tools, row_id, reasoning, confidence, account_code="7502")
    assert result == {"ok": False, "error": error}
    assert saved(mongo_store, cube_id, row_id).tx.account_code == "9998" and audit.entries(client_id=cube_id) == []


def test_reasoning_of_500_characters_and_confidence_at_the_ends_are_allowed(tools, row_id):
    assert update(tools, row_id, "x" * 500, 0, account_code="7502")["ok"] is True
    assert update(tools, row_id, "y", 1, account_code="7500")["ok"] is True


def test_update_row_with_nothing_to_change_says_what_it_can_change(tools, audit, cube_id, row_id):
    assert update(tools, row_id) == {"ok": False, "error": (
        "Say what to change: account_code, document_type, include, link or apart_from.")}
    assert audit.entries(client_id=cube_id) == []


def test_update_row_refuses_an_unknown_document_type_and_an_account_the_client_cant_use(tools, audit, cube_id, row_id):
    assert update(tools, row_id, document_type="memo")["error"].startswith("Choose a document type from: receipt, invoice")
    assert update(tools, row_id, account_code="1200") == {
        "ok": False, "error": "This account can't be chosen for Business Cube Ltd."}
    assert audit.entries(client_id=cube_id) == []


def test_not_the_same_can_name_only_this_clients_other_rows(tools, audit, cube_id, other_clients_row_id, three_rows_to_review, row_id):
    refused = update(tools, row_id, apart_from=[other_clients_row_id])
    assert refused == {"ok": False, "error": "Not the same can only name other rows of this client."}
    assert update(tools, row_id, apart_from=three_rows_to_review[:1])["ok"] is True
    [entry] = audit.entries(client_id=cube_id)
    assert (entry["before"], entry["after"]) == ({"apart_from": []}, {"apart_from": three_rows_to_review[:1]})


def test_the_same_include_replayed_changes_and_audits_nothing(tools, audit, cube_id, row_id):
    update(tools, row_id, include=True)
    update(tools, row_id, include=True)
    assert [e["after"] for e in audit.entries(client_id=cube_id)] == [{"include": True}]


@pytest.fixture
def bank_line(mongo_store, cube_id):
    """A bank line with a person's decision on what it pays, saved as ['b', 'a']."""
    mongo_store.add_upload(cube_id, "Barclays.csv", "table", [
        row(document_type="statement", description="BT DD", counterparty="BT BUSINESS DD", link=["b", "a"],
            document_ref="line")])
    return mongo_store.rows(cube_id)[-1].id


def test_replaying_a_link_in_another_order_changes_and_audits_nothing(tools, audit, cube_id, bank_line):
    assert update(tools, bank_line, link=["a", "b"])["ok"] is True
    assert audit.entries(client_id=cube_id) == []


def test_link_none_and_link_empty_are_different_decisions(tools, audit, cube_id, mongo_store, bank_line):
    update(tools, bank_line, link=[])                    # not a payment of any document
    [entry] = audit.entries(client_id=cube_id)
    assert (entry["before"], entry["after"]) == ({"link": ["b", "a"]}, {"link": []})
    assert saved(mongo_store, cube_id, bank_line).tx.link == []


def test_only_a_bank_line_can_be_linked(tools, audit, cube_id, row_id):
    result = update(tools, row_id, link=["x"])
    assert result["ok"] is False and "can be linked" in result["error"]
    assert audit.entries(client_id=cube_id) == []


def test_list_rows_to_review_pages_by_row_id(tools, three_rows_to_review):
    first = tools["list_rows_to_review"].invoke({"limit": 2})
    rest = tools["list_rows_to_review"].invoke({"after_row_id": first["rows"][-1]["row_id"], "limit": 2})
    assert (len(first["rows"]), first["more"], len(rest["rows"]), rest["more"]) == (2, True, 1, False)


def test_list_rows_to_review_lists_the_rows_that_need_a_look_in_row_id_order_with_their_issues(tools, three_rows_to_review):
    result = tools["list_rows_to_review"].invoke({})
    assert [r["row_id"] for r in result["rows"]] == three_rows_to_review == sorted(three_rows_to_review)
    assert result["more"] is False
    assert result["rows"][0]["issues"] == [NO_DATE_WARNING] and result["rows"][0]["date"] is None


def test_list_rows_to_review_never_lists_a_row_that_is_fine(tools, row_id):
    assert row_id not in [r["row_id"] for r in tools["list_rows_to_review"].invoke({"limit": 100})["rows"]]


def test_get_row_gives_the_short_row_and_what_it_is_matched_with(tools, row_id):
    got = tools["get_row"].invoke({"row_id": row_id})
    assert got["row_id"] == row_id and got["account_code"] == "9998" and got["gross"] == "72.00"
    assert {"candidates", "pays", "paid_by", "copy_of", "recorded_by", "claimed_in", "related_rows",
            "include", "link", "apart_from"} <= set(got)
    assert (got["candidates"], got["pays"], got["paid_by"], got["copy_of"], got["related_rows"]) == ([], [], [], None, [])


def test_get_row_shows_the_bank_line_that_paid_a_bill_and_the_bill_it_paid(deps, mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "BT bill.pdf", "pdf", [row(document_ref="bt")])
    mongo_store.add_upload(client.id, "Barclays.csv", "table", [row(
        date=dt.date(2026, 9, 5), document_type="statement", counterparty="BT BUSINESS DD", document_ref="l1")])
    tools = row_tools(deps, client.id, "j1")
    bill, line = (r.id for r in mongo_store.rows(client.id))
    assert [(s["ref"], s["amount"]) for s in tools["get_row"].invoke({"row_id": line})["pays"]] == [("bt", "72.00")]
    paid_by = tools["get_row"].invoke({"row_id": bill})["paid_by"]
    assert [(s["ref"], s["amount"], s["date"], s["kind"]) for s in paid_by] == [("l1", "72.00", "2026-09-05", "statement")]


def test_get_row_gives_the_short_rows_its_issues_are_about(deps, mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "claim.xlsx", "table", [row(
        document_type="expense_claim", counterparty="Jenny Hogg", description="Jenny Hogg - Car Park", gross="36.00",
        account_code="7400", date=dt.date(2026, 9, 9), document_ref="claim")])
    mongo_store.add_upload(client.id, "photo.jpg", "image", [row(
        document_type="receipt", counterparty="Southgate Bath Car Park", gross="36.00", account_code="7400",
        date=dt.date(2026, 8, 9), document_ref="photo")])
    tools = row_tools(deps, client.id, "j1")
    claim_line, receipt = (r.id for r in mongo_store.rows(client.id))
    got = tools["get_row"].invoke({"row_id": receipt})
    assert [i["related"] for i in got["issues"]] == [[claim_line]]
    assert [r["row_id"] for r in got["related_rows"]] == [claim_line]
    assert got["related_rows"][0]["description"] == "Jenny Hogg - Car Park"


def test_get_row_of_another_clients_row_is_not_found(tools, other_clients_row_id):
    assert tools["get_row"].invoke({"row_id": other_clients_row_id}) == {"ok": False, "error": "This row was not found."}


def test_list_rows_filters_by_account_and_upload_and_limits(tools, mongo_store, cube_id):
    everything = tools["list_rows"].invoke({})
    assert len(everything) == 5 and [r["row_id"] for r in everything] == sorted(r["row_id"] for r in everything)
    assert [r["account_code"] for r in tools["list_rows"].invoke({"account_code": "9998"})] == ["9998"]
    bill_upload = next(r for r in everything if r["account_code"] == "9998")["upload"]["id"]
    assert len(tools["list_rows"].invoke({"upload_id": bill_upload})) == 3
    assert len(tools["list_rows"].invoke({"limit": 2})) == 2
    assert tools["list_rows"].invoke({"account_code": "9998", "upload_id": 999}) == []


def test_list_rows_never_lists_another_clients_rows(tools, other_clients_row_id):
    assert other_clients_row_id not in [r["row_id"] for r in tools["list_rows"].invoke({"limit": 100})]


def test_revert_row_puts_back_what_was_read(tools, row_id):
    read = tools["get_row"].invoke({"row_id": row_id})["account_code"]
    tools["update_row"].invoke({"row_id": row_id, "account_code": "7500", "reasoning": "x", "confidence": 0.5})
    tools["revert_row"].invoke({"row_id": row_id, "reasoning": "Wrong account"})
    assert tools["get_row"].invoke({"row_id": row_id})["account_code"] == read


def test_revert_row_is_audited_with_its_reasoning_as_the_note(tools, audit, cube_id, row_id):
    update(tools, row_id, account_code="7500")
    result = tools["revert_row"].invoke({"row_id": row_id, "reasoning": "Wrong account"})
    assert result["ok"] is True and result["row"]["account_code"] == "9998"
    entry = audit.entries(client_id=cube_id, job_id="j1")[-1]
    assert (entry["action"], entry["target"], entry["note"], entry["actor"]) == (
        "revert_row", {"row_id": row_id}, "Wrong account", "agent")
    assert (entry["before"], entry["after"]) == ({"account_code": "7500"}, {"account_code": "9998"})


def test_reverting_a_row_the_model_never_changed_does_nothing_and_audits_nothing(tools, audit, cube_id, row_id):
    result = tools["revert_row"].invoke({"row_id": row_id, "reasoning": "Just checking"})
    assert result["ok"] is True and result["row"]["row_id"] == row_id
    assert audit.entries(client_id=cube_id) == []


def test_reverting_twice_audits_once(tools, audit, cube_id, row_id):
    update(tools, row_id, account_code="7500")
    for _ in range(2):
        tools["revert_row"].invoke({"row_id": row_id, "reasoning": "Wrong account"})
    assert [e["action"] for e in audit.entries(client_id=cube_id)] == ["update_row", "revert_row"]


def test_revert_row_cant_touch_another_clients_row_and_checks_its_reasoning(tools, row_id, other_clients_row_id):
    assert tools["revert_row"].invoke({"row_id": other_clients_row_id, "reasoning": "x"}) == {
        "ok": False, "error": "This row was not found."}
    assert tools["revert_row"].invoke({"row_id": row_id, "reasoning": ""}) == {
        "ok": False, "error": "Give your reasoning in 1 to 500 characters."}


def test_revert_row_wont_undo_an_amount_a_person_corrected(tools, deps, audit, cube_id, mongo_store, row_id):
    """A row's `original` is what it held before anyone's first edit, so putting it back would also undo a person's
    correction of the amount, which the agent may not change."""
    ledger.change_row(mongo_store, cube_id, row_id, RowPatch(gross="80.00"))   # a person, before the job
    update(tools, row_id, account_code="7500")
    result = tools["revert_row"].invoke({"row_id": row_id, "reasoning": "Wrong account"})
    assert result["ok"] is False and "a person" in result["error"]
    after = saved(mongo_store, cube_id, row_id).tx
    assert (str(after.gross), after.account_code) == ("80.00", "7500")
    assert [e["action"] for e in audit.entries(client_id=cube_id)] == ["update_row"]


def test_every_tool_returns_its_error_instead_of_raising(deps):
    tools = row_tools(deps, 999_999, "j1")      # a client that does not exist
    calls = {"lookup_account": {"query": "7502"}, "list_rows_to_review": {}, "get_row": {"row_id": 1},
             "update_row": {"row_id": 1, "account_code": "7502", "reasoning": "x", "confidence": 0.5},
             "revert_row": {"row_id": 1, "reasoning": "x"}, "list_rows": {}}
    assert set(calls) == set(tools)
    for name, args in calls.items():
        assert tools[name].invoke(args) == {"ok": False, "error": "This client was not found."}, name


def test_a_tool_never_raises_for_an_archived_client(deps, mongo_store, cube_id, row_id):
    mongo_store.update_client(cube_id, {"archived": True})
    result = update(row_tools(deps, cube_id, "j1"), row_id, account_code="7502")
    assert result == {"ok": False, "error": "Restore Business Cube Ltd to add documents or make changes."}


# --- the ingest step ----------------------------------------------------------------------------------------

def state(job, client_id):
    return {"job_id": job.id, "client_id": client_id, **PERIOD, "fix_rounds": 0, "gate_passed": False}


def test_ingest_reports_documents_rows_and_rows_to_review(deps, records, cube_id, job):
    make_ingest(deps)({"job_id": job.id, "client_id": cube_id, **PERIOD, "fix_rounds": 0, "gate_passed": False})
    assert records.events(job.id)[-1].message == "Found 2 documents and 5 rows. 3 rows need a look."


def test_ingest_changes_no_state_and_writes_to_the_ingest_stage(deps, records, cube_id, job):
    assert make_ingest(deps)(state(job, cube_id)) == {}
    assert [(e.stage, e.message) for e in records.events(job.id)] == [
        ("ingest", "Found 2 documents and 5 rows. 3 rows need a look.")]


@pytest.mark.parametrize("dated, message", [
    (True, "Found 1 document and 1 row. No rows need a look."),
    (False, "Found 1 document and 1 row. 1 row needs a look.")])
def test_ingest_uses_the_singular_for_one(deps, records, mongo_store, dated, message):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "BT bill.pdf", "pdf", [row(date=dt.date(2026, 9, 1) if dated else None)])
    job = start_job(records, client.id)
    make_ingest(deps)(state(job, client.id))
    assert records.events(job.id)[-1].message == message


def test_ingest_says_so_when_no_rows_need_a_look(deps, records, mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "Bills.pdf", "pdf", [row(document_ref="a"), row(document_ref="b", gross="10.00")])
    mongo_store.add_upload(client.id, "More.pdf", "pdf", [row(document_ref="c", gross="11.00")])
    job = start_job(records, client.id)
    make_ingest(deps)(state(job, client.id))
    assert records.events(job.id)[-1].message == "Found 2 documents and 3 rows. No rows need a look."
