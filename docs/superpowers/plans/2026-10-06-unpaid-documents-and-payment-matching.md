# Unpaid Documents and Payment Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Book unpaid invoices and claims as owed (Creditors, Debtors, Expenses Owed to Staff) and let bank lines that pay them clear what is owed instead of being booked as a second expense.

**Architecture:** The model labels each row with a `document_type` and a `counterparty`. `checks.normalise` picks the other side of each posting from the type. A new pure module, `ledgersync/matching.py`, groups rows into documents by `document_ref` and matches bank lines to them. `POST /api/transactions/validate` and `POST /api/trial-balance` both run `normalise`, then `match`. The table sends all rows to the validate endpoint after every change and shows the results.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2 (`ledgersync/`), pytest; Next.js 16.3 and React (`frontend/`); the eval harness in `eval/`.

**Spec:** `docs/superpowers/specs/2026-10-06-unpaid-documents-and-payment-matching-design.md`

## Global Constraints

- Run Python with `.venv/bin/python`; run the tests with `.venv/bin/python -m pytest -q`.
- Five tests already fail because of the committed `config.py` defaults (2 jobs, "high" thinking, 3 images per request). They are not part of this plan, so ignore them:
  - `test_api.py::test_health_tells_the_ui_how_many_files_to_send_at_once`
  - `test_config.py::test_groq_settings_have_working_defaults`
  - `test_config.py::test_four_files_are_read_at_once_unless_set_otherwise`
  - `test_groq_client.py::test_chat_asks_the_configured_model_for_strict_json`
  - `test_groq_client.py::test_the_client_sends_one_page_per_request_by_default`
- Money is `Decimal` pennies inside and a string in JSON (`"12.50"`). In messages, write it as `£1,234.50`.
- Accounts: 1200 Bank Current Account, 1100 Debtors, 2100 Creditors, 2110 Expenses Owed to Staff. The model never chooses 1100, 2100 or 2110.
- A payment matches a document only when it is dated on or after the document and at most 31 days after it. One payment clears at most 5 documents.
- Inputs (what the model or a person said) are never overwritten. Everything `normalise` and `match` compute is recomputed on every call, so validating twice gives the same rows.
- Pydantic docstrings and `Field` descriptions in `ledgersync/extractor.py` are sent to the AI model. Change them only where a task says to.
- Frontend: `frontend/AGENTS.md` says to read `node_modules/next/dist/docs/` before using any Next.js API. This plan only changes React state, JSX and CSS in the existing client page, and uses no Next.js API.
- No new dependencies, in Python or npm.
- User-facing text is plain British English.
- Commits: this user decides about commits. Ask once, at the start of execution, whether to commit after each task. If they say no, skip the commit steps and leave the changes uncommitted.

## Review Focus

1. A bank statement uploaded **before** the bills it pays must give the same matches as one uploaded after them. Test: Task 6, `test_matching_does_not_depend_on_the_order_of_the_rows`.
2. The model answering `"Invoice "` or `"INVOICE"` must be read as `invoice`. Test: Task 3, `test_rows_carry_the_document_type_and_counterparty`.
3. A claim with one undated line must still be dated by its latest dated line and matched. Test: Task 6, `test_a_claim_is_dated_by_its_latest_dated_line`.
4. A bill linked twice by a person must flag the second payment as an overpayment, not pass silently. Test: Task 7, `test_a_bill_linked_twice_is_flagged_as_overpaid`.
5. Rows from older API clients, with no `document_type`, must keep their postings: paid from the bank. Test: Task 2, `test_rows_without_a_document_type_are_booked_as_paid_from_the_bank`.

## File Structure

| File | Responsibility |
|---|---|
| `ledgersync/accounts.py` | Chart: adds 2110 Expenses Owed to Staff (`STAFF_EXPENSES`); `choosable()` excludes it |
| `ledgersync/models.py` | `DocumentType`, `DOCUMENT_TYPES`, `NOT_TRANSACTIONS`, `Settlement`; new `Transaction` input fields (`document_type`, `counterparty`, `document_ref`, `include`, `link`) and output fields (`paid_against`, `pays`, `candidates`, `owed`, `paid_by`) |
| `ledgersync/checks.py` | `booked()`, `other_side()`, `MATCHING_ISSUES`; `normalise` picks the other side from the document type and flags documents that are not booked |
| `ledgersync/extractor.py` | Model fields `document_type` and `counterparty`; the prompt rule on document types |
| `ledgersync/adapter.py` | Carries the type and counterparty; gives the rows of one document a shared `document_ref` |
| `ledgersync/matching.py` (new) | `names_match()` and `match()`: the matching rules |
| `ledgersync/posting.py` | Posts a matched bank line against what it settles; skips rows that are not booked; runs `match` |
| `server.py` | The validate endpoint runs `match` |
| `frontend/src/lib/api.ts`, `frontend/src/lib/duplicates.ts`, `frontend/src/app/page.tsx`, `frontend/src/app/globals.css` | Status line, Link / Unlink / Include, posting account, totals, duplicate rule |
| `eval/eval_cases.py`, `eval/make_fixtures.py`, `eval/eval_scoring.py`, `eval/run_eval.py`, `eval/fixtures/` | Document type labels and scoring; two new documents that are not transactions |
| `README.md` | Describes the behaviour |

---

### Task 1: Account 2110 Expenses Owed to Staff

**Files:**
- Modify: `ledgersync/accounts.py` (the 2100 line in `CHART`, the `DEBTORS, CREDITORS` constants line, `choosable()`)
- Test: `tests/test_accounts.py`

**Interfaces:**
- Produces: `accounts.STAFF_EXPENSES == "2110"`; `BY_CODE["2110"]` is a liability; `choosable()` excludes 1200, 2200, 2201, 1100, 2100 and 2110.

- [ ] **Step 1: Write the failing tests**

In `tests/test_accounts.py`, replace `test_a_model_may_choose_any_account_but_the_bank_and_the_control_accounts` with:

```python
def test_a_model_may_choose_any_account_but_the_bank_and_the_control_accounts():
    # The bank is the other side of every posting, and the ledger splits VAT itself. What is owed
    # (Debtors, Creditors, Expenses Owed to Staff) is picked by the ledger from the document type.
    assert {a.code for a in choosable()} == set(BY_CODE) - {"1200", "2200", "2201", "1100", "2100", "2110"}


def test_expenses_owed_to_staff_is_a_liability_of_its_own():
    # A claim is owed to the employee, not to a trade supplier, so it stays out of 2100 Creditors.
    assert (BY_CODE["2110"].name, BY_CODE["2110"].type) == ("Expenses Owed to Staff", AccountType.LIABILITY)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_accounts.py`
Expected: FAIL with `KeyError: '2110'`.

- [ ] **Step 3: Add the account**

In `ledgersync/accounts.py`, after the 2100 Creditors line in `CHART`, add:

```python
    Account("2110", "Expenses Owed to Staff", _L, V.OUTSIDE_SCOPE, "Expense claims approved for staff and not yet reimbursed."),
```

Replace `DEBTORS, CREDITORS = "1100", "2100"` with:

```python
DEBTORS, CREDITORS, STAFF_EXPENSES = "1100", "2100", "2110"
```

Replace `choosable()` with:

```python
def choosable() -> tuple[Account, ...]:
    """Accounts a model may pick for a transaction: every account except the bank (the other
    side of each posting), the VAT control accounts (the ledger splits VAT itself), and the
    accounts for what is owed (Debtors, Creditors, Expenses Owed to Staff), which the ledger
    picks from the document type."""
    return tuple(a for a in CHART
                 if a.code not in (BANK, SALES_VAT, PURCHASE_VAT, DEBTORS, CREDITORS, STAFF_EXPENSES))
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_accounts.py tests/test_extractor.py`
Expected: all pass. The prompt test `test_the_prompt_defines_every_account_it_offers` still passes, because 2110 is not offered to the model.

- [ ] **Step 5: Commit (if the user agreed to commits)**

```bash
git add ledgersync/accounts.py tests/test_accounts.py
git commit -m "Chart: 2110 Expenses Owed to Staff, picked by the ledger, not the model"
```

---

### Task 2: Document types and booking

**Files:**
- Modify: `ledgersync/models.py` (imports; constants after `Direction`; new `Transaction` fields after `evidence`)
- Modify: `ledgersync/checks.py` (imports, `_DERIVED`, new `booked()` and `other_side()`, `normalise`)
- Modify: `ledgersync/posting.py` (`trial_balance`)
- Test: `tests/test_checks.py`, `tests/test_posting.py`

**Interfaces:**
- Consumes: `accounts.DEBTORS`, `CREDITORS`, `STAFF_EXPENSES` (Task 1).
- Produces:
  - `models.DocumentType` (a `Literal` of the 10 types); `models.DOCUMENT_TYPES: tuple[str, ...]`; `models.NOT_TRANSACTIONS: frozenset[str]` (`quote`, `pro_forma`, `purchase_order`, `remittance_advice`, `supplier_statement`, `other`).
  - New `Transaction` inputs: `document_type: DocumentType = "receipt"`, `counterparty: Optional[str]`, `document_ref: Optional[str]`, `include: bool = False`.
  - `checks.booked(tx) -> bool` and `checks.other_side(tx, settings) -> str`.
  - Issue code `not_booked` (info).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checks.py`:

```python
# ─── The other side, by document type ─────────────────────────────────────────

@pytest.mark.parametrize("document_type, direction, account, other_side", [
    ("receipt", "out", "7502", "1200"),
    ("statement", "out", "7100", "1200"),
    ("invoice", "out", "7100", "2100"),        # a bill received: owed to the supplier
    ("invoice", "in", "7100", "2100"),         # a supplier's credit note: the supplier owes us
    ("invoice", "in", "4000", "1100"),         # a sales invoice: owed by the customer
    ("invoice", "out", "4000", "1100"),        # a credit note to a customer: we owe them
    ("expense_claim", "out", "7402", "2110"),  # owed to the employee until reimbursed
])
def test_the_other_side_follows_the_document_type(document_type, direction, account, other_side):
    t = normalise(tx(direction, account=account, document_type=document_type), REGISTERED)
    assert t.contra_account_code == other_side


def test_rows_without_a_document_type_are_booked_as_paid_from_the_bank():
    # Older API clients send no document_type: their postings must not change.
    t = normalise(Transaction(direction="out", gross="72.00", account_code="7502", description="BT"), REGISTERED)
    assert (t.document_type, t.contra_account_code) == ("receipt", "1200")


def test_a_document_that_is_not_a_transaction_is_flagged_not_booked():
    assert ("not_booked", "info") in codes(normalise(tx(document_type="pro_forma"), REGISTERED))
    assert ("not_booked", "info") not in codes(normalise(tx(document_type="pro_forma", include=True), REGISTERED))


def test_the_worked_out_side_follows_an_edit():
    bill = normalise(tx(account="7100", document_type="invoice"), REGISTERED)
    assert normalise(revalidated(bill, account_code="4000"), REGISTERED).contra_account_code == "1100"


def test_an_other_side_sent_by_a_client_is_kept():
    assert normalise(tx(contra_account_code="1230"), REGISTERED).contra_account_code == "1230"
```

Append to `tests/test_posting.py`:

```python
def test_a_bill_received_is_owed_to_the_supplier_not_paid_from_the_bank():
    rent = tx("out", "12000.00", "7100", vat="2000.00", document_type="invoice")
    assert lines(rent) == [("7100", "10000.00", "0.00"), ("2201", "2000.00", "0.00"), ("2100", "0.00", "12000.00")]


def test_a_claim_is_owed_to_the_employee():
    assert lines(tx("out", "173.75", "7402", vat="28.96", document_type="expense_claim")) == [
        ("7402", "144.79", "0.00"), ("2201", "28.96", "0.00"), ("2110", "0.00", "173.75")]


def test_documents_that_are_not_transactions_stay_out_until_included():
    assert trial_balance([tx("out", "360.00", "0030", vat="60.00", document_type="pro_forma")], REGISTERED).lines == []
    included = trial_balance([tx("out", "360.00", "0030", vat="60.00", document_type="pro_forma", include=True)],
                             REGISTERED)
    assert [(l.code, str(l.debit), str(l.credit)) for l in included.lines] == [
        ("0030", "300.00", "0.00"), ("2100", "0.00", "360.00"), ("2201", "60.00", "0.00")]


def test_errors_on_a_row_that_is_not_booked_do_not_block_the_trial_balance():
    assert trial_balance([tx("out", "10.00", "9999", document_type="quote")], REGISTERED).lines == []
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_checks.py tests/test_posting.py`
Expected: FAIL. Pydantic ignores the unknown `document_type` field, so the other side stays 1200 (`'1200' != '2100'`), no `not_booked` issue appears, and `t.document_type` raises `AttributeError`. (`test_an_other_side_sent_by_a_client_is_kept` already passes; it guards today's behaviour.)

- [ ] **Step 3: Add the types and fields to `ledgersync/models.py`**

Change the typing import to `from typing import Literal, Optional, get_args`. After `class Direction`, add:

```python
# The kind of document a row comes from. The last six are not transactions: a person ticks Include to
# book one as an invoice. "other" is what the adapter makes of a type it does not know.
DocumentType = Literal["receipt", "invoice", "expense_claim", "statement", "quote", "pro_forma",
                       "purchase_order", "remittance_advice", "supplier_statement", "other"]
DOCUMENT_TYPES: tuple[str, ...] = get_args(DocumentType)
NOT_TRANSACTIONS = frozenset(DOCUMENT_TYPES[4:])
```

In `Transaction`, after `evidence: Optional[str] = None`, add:

```python
    # Inputs too: what the row comes from and who it is with (from the model), the reference shared by
    # the rows of one document (from the adapter), and a person's Include tick on a document that is
    # not a transaction. Rows without a type, as older clients send them, are receipts: paid when issued.
    document_type: DocumentType = "receipt"
    counterparty: Optional[str] = None
    document_ref: Optional[str] = None
    include: bool = False
```

- [ ] **Step 4: Book by document type in `ledgersync/checks.py`**

Change the imports to:

```python
from .accounts import BY_CODE, CREDITORS, DEBTORS, STAFF_EXPENSES, AccountType
from .models import NOT_TRANSACTIONS, BusinessSettings, Direction, Issue, Transaction
```

Add `"not_booked"` to the `_DERIVED` set. After the `issue()` function, add:

```python
_NOT_TRANSACTION_NAMES = {
    "quote": "a quote", "pro_forma": "a pro forma invoice", "purchase_order": "a purchase order",
    "remittance_advice": "a remittance advice", "supplier_statement": "a supplier's statement of account",
    "other": "a document that is not a transaction",
}


def booked(tx: Transaction) -> bool:
    """A quote, a pro forma or another document that is not a transaction is booked only when a person
    ticks Include."""
    return tx.document_type not in NOT_TRANSACTIONS or tx.include


def other_side(tx: Transaction, settings: BusinessSettings) -> str:
    """The other side of a posting. A receipt or a bank line moved money through the bank. An invoice,
    a claim or an included document is owed until a bank line pays it: a sale to the customer's account
    (Debtors), anything else to the supplier's (Creditors), and a claim to the employee."""
    if tx.document_type == "expense_claim":
        return STAFF_EXPENSES
    if tx.document_type == "invoice" or tx.document_type in NOT_TRANSACTIONS:
        account = BY_CODE.get(tx.account_code)
        return DEBTORS if account is not None and account.type == AccountType.INCOME else CREDITORS
    return settings.bank_account
```

In `normalise`, replace `contra = tx.contra_account_code or settings.bank_account` with:

```python
    # The bank and the accounts for what is owed are worked out afresh on every pass, so a row whose
    # account or document type changes moves with it; any other account sent in (petty cash, say) is kept.
    worked_out = {None, settings.bank_account, DEBTORS, CREDITORS, STAFF_EXPENSES}
    contra = other_side(tx, settings) if tx.contra_account_code in worked_out else tx.contra_account_code
```

In `normalise`, just before `net = tx.gross - posted if posted is not None else None`, add:

```python
    if not booked(tx):
        issues.append(issue("not_booked", f"Looks like {_NOT_TRANSACTION_NAMES[tx.document_type]}: not booked. "
                                          "Tick Include to book it as an invoice.", "info"))
```

- [ ] **Step 5: Skip unbooked rows in `ledgersync/posting.py`**

Change `from .checks import normalise` to `from .checks import booked, normalise`. In `trial_balance`, replace the `problems` and `journal` lines with:

```python
    posted = [(n, tx) for n, tx in enumerate(ready) if booked(tx)]   # a quote or the like waits for Include
    problems = [f"#{n + 1}: {issue.message}" for n, tx in posted
                for issue in tx.issues if issue.severity == "error"]
    if problems:
        raise InvalidTransactions(f"{len(problems)} problem(s) must be fixed before the trial balance: "
                                  + " ".join(problems[:5]))
    journal = [line for n, tx in posted for line in journal_for(tx, n)]
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q`
Expected: only the 5 known failures from Global Constraints. `test_normalising_twice_changes_nothing` and `test_trial_balance_always_balances_for_random_ledgers` still pass.

- [ ] **Step 7: Commit (if agreed)**

```bash
git add ledgersync/models.py ledgersync/checks.py ledgersync/posting.py tests/test_checks.py tests/test_posting.py
git commit -m "Book invoices and claims as owed: Creditors, Debtors, Expenses Owed to Staff"
```

---

### Task 3: The model reports the document type and counterparty

**Files:**
- Modify: `ledgersync/extractor.py` (imports, `MODEL_DOCUMENT_TYPES`, `AccountingTransaction` fields and validator, the prompt in `_instructions`)
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `models.DOCUMENT_TYPES` (Task 2).
- Produces: `AccountingTransaction.document_type: str` (lower-cased, default `"receipt"`) and `AccountingTransaction.counterparty: Optional[str]`, in `row.model_dump()` for the adapter.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor.py`:

```python
def test_the_instructions_name_every_document_type():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system = client.calls[0]["messages"][0]["content"]
    assert all(f'"{kind}"' in system for kind in ("receipt", "invoice", "expense_claim", "statement", "quote",
                                                  "pro_forma", "purchase_order", "remittance_advice",
                                                  "supplier_statement"))
    assert "a credit note is an invoice with the money going the other way" in system
    assert "The counterparty of every line is the person claiming, not the shop" in system


def test_rows_carry_the_document_type_and_counterparty():
    result, _ = extract(transactions_json(dict(ROW, document_type="Invoice ", counterparty="BT plc")))
    assert (result.data[0].document_type, result.data[0].counterparty) == ("invoice", "BT plc")


def test_a_row_without_a_document_type_is_a_receipt():
    result, _ = extract(transactions_json(ROW))
    assert (result.data[0].document_type, result.data[0].counterparty) == ("receipt", None)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_extractor.py`
Expected: FAIL with `AttributeError: 'AccountingTransaction' object has no attribute 'document_type'`, and the instructions assertions fail.

- [ ] **Step 3: Add the fields**

In `ledgersync/extractor.py`, add `from .models import DOCUMENT_TYPES` to the imports. After `ACCOUNT_CHOICES`, add:

```python
# The document types a model may give; anything else becomes "other" in the adapter.
MODEL_DOCUMENT_TYPES: tuple[str, ...] = tuple(t for t in DOCUMENT_TYPES if t != "other")
```

In `AccountingTransaction`, after the `mixed_items` field, add:

```python
    # Which kind of document the row comes from: the ledger books an unpaid invoice or claim as owed, and
    # leaves a quote, a pro forma or the like for a person to check. Any other text passes here; the
    # adapter treats it as a document that is not a transaction.
    document_type: str = Field("receipt", description="The kind of document this transaction comes from",
                               json_schema_extra={"enum": list(MODEL_DOCUMENT_TYPES)})
    counterparty: Optional[str] = Field(None, description="Who was paid or who paid, as printed: the supplier, "
                                                          "customer, employee or payee; null when not shown")
```

After the `_account_text` validator, add:

```python
    @field_validator("document_type", mode="before")
    @classmethod
    def _document_type_text(cls, v):
        return "receipt" if v is None else str(v).strip().lower()
```

- [ ] **Step 4: Add the rule to the prompt**

Run this from the project folder. It edits `_instructions` in place, and stops if any text is not found exactly once:

```bash
.venv/bin/python - <<'EOF'
import pathlib
p = pathlib.Path("ledgersync/extractor.py"); s = p.read_text()
edits = [  # bottom up, so each renumbering is unique when applied
    ('\n8. If there are no transactions', '\n9. If there are no transactions'),
    ('\n7. Dates are UK format', '\n8. Dates are UK format'),
    ('\n6. VAT: the VAT amount', '\n7. VAT: the VAT amount'),
    ('\n5. Account: choose by', '\n6. Account: choose by'),
    ('because each line carries its own VAT.\n4. A bank statement',
     'because each line carries its own VAT. The counterparty of every line is the person claiming, not the shop.\n'
     '5. A bank statement'),
    ('\n3. An expense claim or expense report', '\n4. An expense claim or expense report'),
    ('{invoices}\n2. A receipt or an invoice',
     '{invoices}\n2. Document type: give every transaction the type of the document it comes from. "receipt": a '
     'till or card receipt, or a ticket, paid when it was issued. "invoice": an invoice or bill asking for payment, '
     'even when it says paid; a credit note is an invoice with the money going the other way. "expense_claim": an '
     'expense claim or expense report. "statement": a bank statement, or bank lines pasted or typed. A quote, a pro '
     'forma invoice, a purchase order, a remittance advice and a supplier\'s statement of account are not '
     'transactions: still list their amounts, with the type "quote", "pro_forma", "purchase_order", '
     '"remittance_advice" or "supplier_statement", so a person can check them. Give as counterparty who was paid '
     'or who paid, as printed: the supplier, the customer or the payee.\n3. A receipt or an invoice'),
    ('"mixed_items": false}}]}}"""',
     '"mixed_items": false, "document_type": "receipt", "counterparty": "who was paid or who paid"}}]}}"""'),
]
for old, new in edits:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)
EOF
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_extractor.py tests/test_groq_client.py`
Expected: `test_extractor.py` all pass, including `test_the_instructions_spell_out_every_field_of_the_answer` and the claim test. `test_groq_client.py` shows only its 2 known failures.

- [ ] **Step 6: Commit (if agreed)**

```bash
git add ledgersync/extractor.py tests/test_extractor.py
git commit -m "Model reports each row's document type and counterparty"
```

---

### Task 4: The adapter carries the type, counterparty and a document reference

**Files:**
- Modify: `ledgersync/adapter.py` (imports, new `_document_type` and `_document_key`, `_documents`, `_keep_unsplittable_whole`, `to_transactions`)
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `models.DOCUMENT_TYPES` (Task 2); `document_type` and `counterparty` in the model's row dicts (Task 3).
- Produces:
  - `Transaction.document_type`, where an unknown type becomes `"other"`.
  - `Transaction.counterparty`, stripped, or `None`.
  - `Transaction.document_ref`: a 12-character hex string, shared by the rows of one document and unique for every bank line and every row without a printed total.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_adapter.py`:

```python
def test_rows_carry_the_document_type_and_counterparty():
    [t] = adapt(dict(ROW, document_type="invoice", counterparty=" BT plc "))
    assert (t.document_type, t.counterparty, t.contra_account_code) == ("invoice", "BT plc", "2100")


def test_an_unexpected_document_type_is_left_for_a_person():
    [t] = adapt(dict(ROW, document_type="delivery note"))
    assert t.document_type == "other" and ("not_booked", "info") in issues_of(t)


def test_rows_of_one_invoice_share_a_reference_and_bank_lines_do_not():
    doc = {"document_total": 60.0, "document_type": "invoice", "counterparty": "Hilton", "date": "2026-09-01"}
    bill = adapt(dict(ROW, **doc, amount=20.0, account="7406 Subsistence"),
                 dict(ROW, **doc, amount=40.0, account="7400 Travel"))
    lines = adapt(dict(ROW, document_type="statement"), dict(ROW, document_type="statement"))
    assert bill[0].document_ref == bill[1].document_ref and lines[0].document_ref != lines[1].document_ref


def test_two_invoices_with_the_same_total_on_different_dates_are_two_documents():
    doc = {"document_total": 12000.0, "document_type": "invoice", "counterparty": "Business Cube"}
    first, second = adapt(dict(ROW, **doc, amount=12000.0, date="2026-10-01"),
                          dict(ROW, **doc, amount=12000.0, date="2026-11-01"))
    assert first.document_ref != second.document_ref
    assert "total_mismatch" not in [code for t in (first, second) for code, _ in issues_of(t)]


def test_a_claims_lines_share_one_reference_whatever_their_dates():
    claim = {"document_total": 50.0, "document_type": "expense_claim", "counterparty": "Matt Barnes"}
    lines = adapt(dict(ROW, **claim, amount=18.0, date="2026-09-12"),
                  dict(ROW, **claim, amount=32.0, date="2026-09-14"))
    assert lines[0].document_ref == lines[1].document_ref
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_adapter.py`
Expected: FAIL, because the type is `receipt` and the references are `None`. The two-invoices test also fails, with a `total_mismatch` that today's grouping (by total only) produces.

- [ ] **Step 3: Implement**

In `ledgersync/adapter.py`, add `import uuid` to the imports. Change the models import to `from .models import DOCUMENT_TYPES, BusinessSettings, Direction, Transaction`. Replace `_documents` with:

```python
def _document_type(row: dict) -> str:
    """The model's document type; one the ledger does not know is left for a person to check."""
    kind = str(row.get("document_type") or "receipt").strip().lower()
    return kind if kind in DOCUMENT_TYPES else "other"


def _document_key(row: dict) -> Optional[tuple]:
    """Which document a row belongs to. Rows printed with the same total belong together when they come
    from the same kind of document, with the same counterparty and, except for a claim (whose lines have
    their own dates), the same date. None for a bank line or a row without a printed total."""
    total, kind = to_money(row.get("document_total")), _document_type(row)
    if not total or not to_money(row.get("amount")) or kind == "statement":
        return None
    who = str(row.get("counterparty") or "").strip().lower()
    return abs(total), kind, who, None if kind == "expense_claim" else row.get("date")


def _documents(rows: list[dict]) -> dict:
    """The rows of each receipt, invoice or claim, keyed by _document_key."""
    documents: dict = {}
    for row in rows:
        key = _document_key(row)
        if key:
            documents.setdefault(key, []).append(row)
    return documents
```

In `_keep_unsplittable_whole`, change the loop header `for total, parts in _documents(rows).items():` to:

```python
    for (total, *_), parts in _documents(rows).items():
```

In `to_transactions`, replace the start of the function, up to and including the `total_mismatch` check, with:

```python
def to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]:
    result = []
    rows = _keep_unsplittable_whole(rows)
    documents = _documents(rows)
    sums = {key: sum(abs(to_money(row.get("amount"))) for row in parts) for key, parts in documents.items()}
    refs = {key: uuid.uuid4().hex[:12] for key in documents}   # one reference per document
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        key = _document_key(row)
        if key and sums[key] != key[0]:
            issues.append(issue("total_mismatch", f"The rows from this document add up to £{sums[key]} but "
                                                  f"its total is £{key[0]}; check the amounts against the document."))
```

At the end of the loop, replace the `Transaction(...)` construction with:

```python
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None else None,   # sign: direction
                         account_code=code, currency=row.get("currency"), source=source,
                         method="llm", issues=issues, document_type=_document_type(row),
                         counterparty=str(row.get("counterparty") or "").strip() or None,
                         document_ref=refs[key] if key else uuid.uuid4().hex[:12])
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_adapter.py tests/test_api.py tests/test_pipeline.py`
Expected: all pass apart from the known `test_api` failure. The existing split, merge and total tests are unchanged.

- [ ] **Step 5: Commit (if agreed)**

```bash
git add ledgersync/adapter.py tests/test_adapter.py
git commit -m "Adapter: document type, counterparty and a reference shared by a document's rows"
```

---

### Task 5: Comparing names

**Files:**
- Create: `ledgersync/matching.py`
- Test: `tests/test_matching.py` (new)

**Interfaces:**
- Produces: `matching.names_match(a: Optional[str], b: Optional[str]) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_matching.py`:

```python
import pytest

from ledgersync.matching import names_match


@pytest.mark.parametrize("a, b", [
    ("M BARNES EXPENSES", "Matt Barnes"),
    ("BUS CUBE MGMT", "Business Cube Management Solutions Limited"),
    ("FIN: CURRYS ONLINE", "Currys Ltd"),
    ("BUS MGMT SOL", "Business Management Solutions"),   # only abbreviations in common
])
def test_names_that_match(a, b):
    assert names_match(a, b) and names_match(b, a)


@pytest.mark.parametrize("a, b", [
    ("ACME CONSULTING SERVICES LTD", "Northbridge Services Ltd"),   # only generic words in common
    ("HMRC PAYE", "Business Cube Management"),
    ("", "Business Cube"),
    (None, "Business Cube"),
    ("BT", "BT"),                                                   # too short to tell
])
def test_names_that_do_not_match(a, b):
    assert not names_match(a, b)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledgersync.matching'`.

- [ ] **Step 3: Implement**

Create `ledgersync/matching.py`:

```python
"""Matches bank lines to the open invoices and claims they pay (design of 2026-10-06). Pure functions:
the same rows give the same matches, whatever order they arrived in."""
from __future__ import annotations

import re
from typing import Optional

# Words that say nothing about who a business is: legal forms, bank-statement noise and generic trade words.
_IGNORED = {"ltd", "limited", "plc", "llp", "co", "the", "and",
            "bank", "payment", "payments", "fin", "card", "dd", "so", "bacs", "fps", "ref",
            "services", "solutions", "group", "uk", "online", "international", "holdings", "company", "trading"}


def _words(name: Optional[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _IGNORED]


def _abbreviates(short: str, long: str) -> bool:
    """'mgmt' abbreviates 'management': three letters or more, the same first letter, the rest in order."""
    if not 3 <= len(short) < len(long) or short[0] != long[0]:
        return False
    letters = iter(long)
    return all(ch in letters for ch in short)


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two names share a word of four or more letters, or a word of one abbreviates a word of
    the other, after legal forms, bank words, generic trade words and numbers are set aside."""
    wa, wb = _words(a), _words(b)
    return (any(w in wb for w in wa if len(w) >= 4)
            or any(_abbreviates(x, y) or _abbreviates(y, x) for x in wa for y in wb))
```

The spec lists legal words and bank words. The generic trade words (`services`, `solutions` and so on) are added so that two different companies don't match on a shared word like "Services", which fits the spec's "distinctive word".

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py`
Expected: 9 passed.

- [ ] **Step 5: Commit (if agreed)**

```bash
git add ledgersync/matching.py tests/test_matching.py
git commit -m "Matching: compare payee names, ignoring legal and bank words"
```

---

### Task 6: Matching a bank line to one document

**Files:**
- Modify: `ledgersync/models.py` (new `Settlement`; new `Transaction` fields; `_pennies` validator; computed `paid_against_name`)
- Modify: `ledgersync/checks.py` (`MATCHING_ISSUES`, `_DERIVED`)
- Modify: `ledgersync/matching.py` (add `match` and its helpers)
- Test: `tests/test_matching.py`

**Interfaces:**
- Consumes:
  - `checks.booked`, `checks.issue`, `checks.normalise`.
  - `Transaction.document_type`, `counterparty`, `document_ref` and `contra_account_code`, as set by `normalise` (Task 2).
- Produces:
  - `models.Settlement(ref: str, amount: Decimal, date: Optional[date], description: str)`.
  - New `Transaction` input: `link: Optional[list[str]] = None`.
  - New `Transaction` outputs: `paid_against: Optional[str]`, `pays: list[Settlement]`, `candidates: list[list[Settlement]]`, `owed: Optional[Decimal]`, `paid_by: list[Settlement]`, and the computed `paid_against_name`.
  - `checks.MATCHING_ISSUES`.
  - `matching.match(rows: list[Transaction], settings: BusinessSettings) -> list[Transaction]` (the input must already be normalised).
  - `matching.WINDOW = timedelta(days=31)`.
  - The internal helpers that Task 7 extends: `_Document`, `_settle`, `_decide`, `_pay`, `_gbp`, `_choices`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matching.py`:

```python
import datetime as dt
from decimal import Decimal

from ledgersync.checks import normalise
from ledgersync.matching import match
from ledgersync.models import BusinessSettings, Transaction

SETTINGS = BusinessSettings()


def row(kind, direction, gross, date, counterparty, ref, account="7100", **kw):
    return Transaction(document_type=kind, direction=direction, gross=gross, date=dt.date.fromisoformat(date),
                       counterparty=counterparty, document_ref=ref, account_code=account,
                       description=kw.pop("description", f"{counterparty} {kind}"), **kw)


def bill(gross="12000.00", date="2026-10-01", who="Business Cube Management Solutions", ref="bill-oct", **kw):
    return row("invoice", "out", gross, date, who, ref, **kw)


def bank(gross="12000.00", date="2026-10-03", who="BUSINESS CUBE MGMT", ref="line-1", direction="out", **kw):
    return row("statement", direction, gross, date, who, ref, **kw)


def matched(*rows):
    return match([normalise(t, SETTINGS) for t in rows], SETTINGS)


def codes(t):
    return [(i.code, i.severity) for i in t.issues]


def test_a_bank_line_pays_the_one_bill_it_matches():
    paid_bill, line = matched(bill(), bank())
    assert [(p.ref, p.amount) for p in line.pays] == [("bill-oct", Decimal("12000.00"))]
    assert (line.paid_against, line.paid_against_name) == ("2100", "Creditors")
    assert (line.vat_posted, line.net) == (Decimal("0.00"), Decimal("12000.00"))
    assert paid_bill.owed == Decimal("0.00") and [p.ref for p in paid_bill.paid_by] == ["line-1"]
    assert codes(line) == []


def test_an_unpaid_bill_is_still_owed():
    [open_bill] = matched(bill())
    assert open_bill.owed == Decimal("12000.00") and open_bill.paid_by == []


def test_two_bills_that_fit_ask_a_person_to_choose():
    october, later, line = matched(bill(), bill(date="2026-10-10", ref="bill-later"), bank(date="2026-10-20"))
    assert codes(line) == [("choose_payment", "error")] and line.pays == []
    assert [[c.ref for c in option] for option in line.candidates] == [["bill-oct"], ["bill-later"]]
    assert october.owed == later.owed == Decimal("12000.00")


@pytest.mark.parametrize("paid_on", ["2026-09-30", "2026-11-02"])
def test_a_payment_before_the_bill_or_more_than_31_days_after_is_not_matched(paid_on):
    open_bill, line = matched(bill(), bank(date=paid_on))
    assert line.pays == [] and open_bill.owed == Decimal("12000.00")


def test_a_payment_on_the_31st_day_is_matched():
    _, line = matched(bill(), bank(date="2026-11-01"))
    assert [p.ref for p in line.pays] == ["bill-oct"]


def test_undated_rows_are_never_matched():
    _, line = matched(bill(), bank().model_copy(update={"date": None}))
    assert line.pays == [] and line.candidates == []


def test_money_in_does_not_pay_a_bill():
    open_bill, line = matched(bill(), bank(direction="in"))
    assert line.pays == [] and open_bill.owed == Decimal("12000.00")


def test_a_sales_invoice_is_paid_by_money_in():
    invoice = row("invoice", "in", "2400.00", "2026-09-30", "Harbour & Lane Architects LLP", "inv-117", account="4000")
    paid, line = matched(invoice, bank("2400.00", "2026-10-14", "HARBOUR LANE ARCHITECTS", direction="in",
                                       account="4000"))
    assert line.paid_against == "1100" and paid.owed == Decimal("0.00")


def test_a_claim_is_paid_by_its_reimbursement():
    lines = [row("expense_claim", "out", gross, date, "Matt Barnes", "claim-matt", account=account)
             for gross, date, account in (("173.75", "2026-09-14", "7402"), ("12.88", "2026-09-14", "7406"),
                                          ("27.50", "2026-09-30", "7502"))]
    *claim, line = matched(*lines, bank("214.13", "2026-10-05", "M BARNES EXPENSES"))
    assert line.paid_against == "2110" and {t.owed for t in claim} == {Decimal("0.00")}


def test_a_claim_is_dated_by_its_latest_dated_line():
    first = row("expense_claim", "out", "100.00", "2026-09-01", "Jenny Hogg", "claim")
    undated = row("expense_claim", "out", "50.00", "2026-09-20", "Jenny Hogg", "claim").model_copy(update={"date": None})
    *_, late = matched(first, undated, bank("150.00", "2026-10-03", "J HOGG EXPENSES"))
    *_, in_time = matched(first, undated, bank("150.00", "2026-10-02", "J HOGG EXPENSES"))
    assert late.pays == [] and [p.ref for p in in_time.pays] == ["claim"]


def test_unlink_makes_an_ordinary_bank_line():
    open_bill, line = matched(bill(), bank(link=[]))
    assert line.pays == [] and line.paid_against is None and open_bill.owed == Decimal("12000.00")


def test_link_pays_the_document_a_person_chose():
    october, later, line = matched(bill(), bill(date="2026-10-10", ref="bill-later"),
                                   bank(date="2026-10-20", link=["bill-later"]))
    assert [p.ref for p in line.pays] == ["bill-later"] and codes(line) == []
    assert (october.owed, later.owed) == (Decimal("12000.00"), Decimal("0.00"))


def test_a_link_to_a_document_no_longer_in_the_table_is_dropped():
    _, line = matched(bill(), bank(link=["gone"]))
    assert ("stale_link", "warning") in codes(line) and [p.ref for p in line.pays] == ["bill-oct"]


def test_lines_a_person_linked_are_applied_before_automatic_matches():
    # The automatic line comes first by date, but the person said the later line pays the bill.
    _, first, second = matched(bill(), bank(date="2026-10-02", ref="early"),
                               bank(date="2026-10-05", ref="late", link=["bill-oct"]))
    assert first.pays == [] and [p.ref for p in second.pays] == ["bill-oct"]


def test_matching_does_not_depend_on_the_order_of_the_rows():
    rows = [bill(), bill(gross="500.00", ref="bill-small", who="Clearway Office Supplies"),
            bank(), bank("500.00", ref="line-2", who="CLEARWAY OFFICE")]
    forward = {t.document_ref: (t.owed, [p.ref for p in t.pays]) for t in matched(*rows)}
    backward = {t.document_ref: (t.owed, [p.ref for p in t.pays]) for t in matched(*reversed(rows))}
    assert forward == backward


def test_matching_again_gives_the_same_result_without_duplicate_issues():
    once = matched(bill(), bill(date="2026-10-10", ref="bill-later"), bank(date="2026-10-20"))
    again = match([normalise(Transaction.model_validate(t.model_dump(mode="json")), SETTINGS) for t in once],
                  SETTINGS)
    assert again == once


def test_receipts_are_neither_documents_nor_payments():
    receipt, line = matched(row("receipt", "out", "12000.00", "2026-10-01", "Business Cube", "r-1"), bank())
    assert line.pays == [] and receipt.owed is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py`
Expected: FAIL with `ImportError: cannot import name 'match'`.

- [ ] **Step 3: Add the fields to `ledgersync/models.py`**

Before `class Transaction`, add:

```python
class Settlement(BaseModel):
    """A payment applied to a document. On a bank line: a document it pays. On a document's rows: a bank
    line that paid it. In a bank line's candidates: a document it could pay, with what is open on it."""
    ref: str
    amount: Decimal
    date: Optional[dt.date] = None
    description: str = ""
```

In `Transaction`, after `include: bool = False`, add:

```python
    link: Optional[list[str]] = None               # a person's decision on a bank line: None automatic,
                                                   # [] not a payment of any document, refs: pays these
    # Outputs, recomputed by matching.match on every pass:
    paid_against: Optional[str] = None             # a bank line that pays documents: the account it settles
    pays: list[Settlement] = Field(default_factory=list)              # a bank line: the documents it pays
    candidates: list[list[Settlement]] = Field(default_factory=list)  # a bank line: what it could pay (Link)
    owed: Optional[Decimal] = None                 # a document's row: what is still open on the document
    paid_by: list[Settlement] = Field(default_factory=list)           # a document's row: what paid it
```

Change the `_pennies` validator decorator to `@field_validator("vat", "vat_posted", "net", "owed", mode="before")`. After the `account_name` computed field, add:

```python
    @computed_field
    @property
    def paid_against_name(self) -> Optional[str]:
        account = BY_CODE.get(self.paid_against or "")
        return account.name if account else None
```

- [ ] **Step 4: List the matching issues in `ledgersync/checks.py`**

Before `_DERIVED`, add:

```python
# Issues matching.match adds; listed here so validating again replaces them instead of adding copies.
MATCHING_ISSUES = {"choose_payment", "part_payment", "overpayment", "possible_payment", "stale_link"}
```

Then add `| MATCHING_ISSUES` to the end of the `_DERIVED = {...}` expression.

- [ ] **Step 5: Implement `match` in `ledgersync/matching.py`**

Replace the imports at the top of `ledgersync/matching.py` with:

```python
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .checks import MATCHING_ISSUES, booked, issue
from .models import BusinessSettings, Issue, Settlement, Transaction
from .money import ZERO

WINDOW = dt.timedelta(days=31)   # a payment pays a document dated at most 31 days before it
```

Append to the end of the file:

```python
@dataclass
class _Document:
    """An open invoice, claim or included document: the rows that share its document_ref."""
    ref: str
    direction: str
    account: Optional[str]        # where what is owed is held: 1100, 2100 or 2110
    counterparty: Optional[str]
    description: str
    rows: list[int] = field(default_factory=list)
    total: Decimal = ZERO
    date: Optional[dt.date] = None
    open: Decimal = ZERO
    paid_by: list[Settlement] = field(default_factory=list)

    def settlement(self, amount: Decimal) -> Settlement:
        return Settlement(ref=self.ref, amount=amount, date=self.date, description=self.description)

    def label(self) -> str:
        when = f", {self.date:%d %b %Y}" if self.date else ""
        return f"{self.description} ({_gbp(self.open)}{when})"


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}"


def match(rows: list[Transaction], settings: BusinessSettings) -> list[Transaction]:
    """Which documents each bank line pays and what is still owed on each document, for transactions
    checks.normalise has already prepared. Lines a person linked go first, the rest oldest first, so the
    result does not depend on the order the rows arrived in."""
    rows = [_cleared(tx) for tx in rows]
    documents = _documents(rows)
    changes: list[dict] = [{} for _ in rows]
    lines = sorted((n for n, tx in enumerate(rows) if tx.document_type == "statement"),
                   key=lambda n: (rows[n].link is None, rows[n].date or dt.date.max, n))
    for n in lines:
        _settle(n, rows[n], documents, changes[n])
    for doc in documents.values():
        for n in doc.rows:
            changes[n].update(owed=doc.open, paid_by=list(doc.paid_by))
    return [tx.model_copy(update=change) if change else tx for tx, change in zip(rows, changes)]


def _cleared(tx: Transaction) -> Transaction:
    """The row without the outputs of an earlier match."""
    return tx.model_copy(update={"paid_against": None, "pays": [], "candidates": [], "owed": None, "paid_by": [],
                                 "issues": [i for i in tx.issues if i.code not in MATCHING_ISSUES]})


def _documents(rows: list[Transaction]) -> dict[str, _Document]:
    """The open documents: booked invoices, claims and included documents, grouped by document_ref.
    A claim is dated by its latest dated line."""
    documents: dict[str, _Document] = {}
    for n, tx in enumerate(rows):
        if tx.document_type in ("receipt", "statement") or not booked(tx):
            continue
        ref = tx.document_ref or f"row-{n}"
        doc = documents.setdefault(ref, _Document(ref=ref, direction=tx.direction.value,
                                                  account=tx.contra_account_code,
                                                  counterparty=tx.counterparty, description=tx.description))
        doc.rows.append(n)
        doc.total += tx.gross if tx.direction.value == doc.direction else -tx.gross
        if tx.date and (doc.date is None or tx.date > doc.date):
            doc.date = tx.date
        doc.counterparty = doc.counterparty or tx.counterparty
    for doc in documents.values():
        doc.open = doc.total
    return documents


def _could_pay(line: Transaction, doc: _Document) -> bool:
    """Still open, the same direction, and the payment dated on or after the document and within 31 days."""
    return (doc.open > ZERO and doc.direction == line.direction.value and doc.date is not None
            and line.date is not None and doc.date <= line.date <= doc.date + WINDOW)


def _settle(n: int, line: Transaction, documents: dict[str, _Document], change: dict) -> None:
    found: list[Issue] = []
    if line.link == []:                                   # Unlink: an ordinary bank line
        return
    if line.link:
        linked = [documents[ref] for ref in line.link if ref in documents]
        if len(linked) == len(line.link):
            _pay(n, line, linked, change, found)          # the person's choice, as given
            _note(change, line, found)
            return
        found.append(issue("stale_link", "A document this line was linked to is no longer in the table, "
                                         "so it was matched automatically."))
    kind, options = _decide(line, [d for d in documents.values() if _could_pay(line, d)])
    if kind == "pay":
        _pay(n, line, options[0], change, found)
    elif kind == "choose":
        found.append(issue("choose_payment", f"Could pay: {_choices(options)}. Choose one.", "error"))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    _note(change, line, found)


def _decide(line: Transaction, docs: list[_Document]) -> tuple[str, list[list[_Document]]]:
    """Pay the document when exactly one from the same counterparty is owed the payment's amount; ask a
    person to choose when several are."""
    named = [d for d in docs if line.counterparty and names_match(line.counterparty, d.counterparty)]
    exact = [d for d in named if d.open == line.gross]
    if exact:
        return ("pay", [exact]) if len(exact) == 1 else ("choose", [[d] for d in exact])
    return "none", []


def _pay(n: int, line: Transaction, docs: list[_Document], change: dict, found: list[Issue]) -> None:
    """Applies the bank line to documents, oldest first: each takes what is open on it and the last takes
    the rest. The line then posts against the account holding what was owed, with no VAT: the VAT was
    booked with the document."""
    docs = sorted(docs, key=lambda d: (d.date or dt.date.max, d.ref))
    left, pays = line.gross, []
    for k, doc in enumerate(docs):
        take = left if k == len(docs) - 1 else min(doc.open, left)
        doc.open, left = doc.open - take, left - take
        pays.append(doc.settlement(take))
        doc.paid_by.append(Settlement(ref=line.document_ref or f"row-{n}", amount=take, date=line.date,
                                      description=line.description))
    change.update(paid_against=docs[0].account, pays=pays, vat_posted=ZERO, net=line.gross)


def _choices(options: list[list[_Document]]) -> str:
    return " or ".join(" + ".join(d.label() for d in option) for option in options)


def _note(change: dict, line: Transaction, found: list[Issue]) -> None:
    if found:
        change["issues"] = line.issues + found
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py tests/test_checks.py`
Expected: all pass.

- [ ] **Step 7: Commit (if agreed)**

```bash
git add ledgersync/models.py ledgersync/checks.py ledgersync/matching.py tests/test_matching.py
git commit -m "Matching: a bank line pays the one open document it matches, or a person chooses"
```

---

### Task 7: Several documents, part payments, overpayments and suggestions

**Files:**
- Modify: `ledgersync/matching.py` (imports, `MAX_SET`, `_settle`, `_decide`, `_pay`; new `_sets_adding_up_to` and `_overpaid`)
- Test: `tests/test_matching.py`

**Interfaces:**
- Consumes: the `matching` internals from Task 6; `accounts.DEBTORS` and `STAFF_EXPENSES` (Task 1).
- Produces: issue codes `part_payment`, `overpayment` and `possible_payment` (warnings), with the messages below.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matching.py`:

```python
def test_one_payment_can_clear_several_bills_from_the_same_supplier():
    a, b, line = matched(bill("300.00", ref="cw-1", who="Clearway Office Supplies"),
                         bill("200.00", date="2026-10-05", ref="cw-2", who="Clearway Office Supplies"),
                         bank("500.00", "2026-10-20", "CLEARWAY OFFICE SUPP"))
    assert sorted(p.ref for p in line.pays) == ["cw-1", "cw-2"] and a.owed == b.owed == Decimal("0.00")


def test_several_sets_that_add_up_ask_a_person_to_choose():
    docs = [bill(gross, date=date, ref=ref, who="Clearway Office Supplies")
            for gross, date, ref in (("300.00", "2026-10-01", "a"), ("200.00", "2026-10-02", "b"),
                                     ("400.00", "2026-10-03", "c"), ("100.00", "2026-10-04", "d"))]
    *_, line = matched(*docs, bank("500.00", "2026-10-20", "CLEARWAY"))
    assert codes(line) == [("choose_payment", "error")]
    assert sorted(sorted(c.ref for c in option) for option in line.candidates) == [["a", "b"], ["c", "d"]]


def test_a_part_payment_is_applied_and_flagged():
    open_bill, line = matched(bill(), bank("10000.00"))
    assert open_bill.owed == Decimal("2000.00") and [p.amount for p in line.pays] == [Decimal("10000.00")]
    assert codes(line) == [("part_payment", "warning")]
    assert line.issues[0].message == "Paid £10,000.00 of £12,000.00. £2,000.00 still owed."


def test_an_overpayment_is_applied_and_flagged():
    open_bill, line = matched(bill(), bank("12050.00"))
    assert open_bill.owed == Decimal("-50.00") and codes(line) == [("overpayment", "warning")]
    assert line.issues[0].message == "Paid £50.00 more than owed. The supplier now owes you £50.00."


def test_a_customer_who_pays_too_much_is_owed_the_difference():
    invoice = row("invoice", "in", "2400.00", "2026-09-30", "Harbour & Lane Architects", "inv-117", account="4000")
    _, line = matched(invoice, bank("2450.00", "2026-10-14", "HARBOUR LANE", direction="in", account="4000"))
    assert line.issues[0].message == "Received £50.00 more than owed. You now owe the customer £50.00."


def test_a_bill_linked_twice_is_flagged_as_overpaid():
    _, first, second = matched(bill(), bank(ref="line-1", link=["bill-oct"]),
                               bank(ref="line-2", date="2026-10-04", link=["bill-oct"]))
    assert codes(first) == [] and codes(second) == [("overpayment", "warning")]


def test_a_part_payment_with_two_open_bills_from_the_supplier_asks_a_person():
    *_, line = matched(bill(), bill("9000.00", date="2026-10-05", ref="bill-2"), bank("10000.00", "2026-10-20"))
    assert codes(line) == [("choose_payment", "error")] and len(line.candidates) == 2


def test_the_same_amount_from_a_different_name_is_only_a_suggestion():
    open_bill, line = matched(bill(), bank(who="HMRC PAYE"))
    assert codes(line) == [("possible_payment", "warning")] and line.pays == []
    assert [[c.ref for c in o] for o in line.candidates] == [["bill-oct"]] and open_bill.owed == Decimal("12000.00")


def test_a_bank_line_without_a_counterparty_is_only_a_suggestion():
    _, line = matched(bill(), bank(who=None))
    assert codes(line) == [("possible_payment", "warning")]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py`
Expected: the 9 new tests FAIL, because nothing is paid, flagged or suggested yet. The Task 5 and 6 tests still pass.

- [ ] **Step 3: Implement**

In `ledgersync/matching.py`:
- Add `from itertools import combinations` to the imports.
- Add `from .accounts import DEBTORS, STAFF_EXPENSES`.
- Change the models import to `from .models import BusinessSettings, Direction, Issue, Settlement, Transaction`.
- After `WINDOW`, add:

```python
MAX_SET = 5                      # one payment clears at most five documents from one counterparty
```

In `_settle`, after the `elif kind == "choose":` branch (before `_note(change, line, found)` at the end), add:

```python
    elif kind == "suggest":
        found.append(issue("possible_payment", f"May pay {_choices(options)}: the same amount, but the names do "
                                               "not match. Link it if it does."))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
```

Replace `_decide` with:

```python
def _decide(line: Transaction, docs: list[_Document]) -> tuple[str, list[list[_Document]]]:
    """What to do with a bank line, given the open documents it could pay (same direction, within 31 days):
    pay them, ask a person to choose, suggest them, or nothing. Only documents from the same counterparty
    are ever paid automatically."""
    named = [d for d in docs if line.counterparty and names_match(line.counterparty, d.counterparty)]
    exact = [d for d in named if d.open == line.gross]
    if exact:
        return ("pay", [exact]) if len(exact) == 1 else ("choose", [[d] for d in exact])
    sets = _sets_adding_up_to(line.gross, named)
    if sets:
        return ("pay", sets) if len(sets) == 1 else ("choose", sets)
    if named:      # a part payment or an overpayment, when only one document can be meant
        return ("pay", [named]) if len(named) == 1 else ("choose", [[d] for d in named])
    same_amount = [d for d in docs if d.open == line.gross]
    return ("suggest", [[d] for d in same_amount]) if same_amount else ("none", [])


def _sets_adding_up_to(amount: Decimal, docs: list[_Document]) -> list[list[_Document]]:
    """Sets of two to five documents whose open amounts add up to the payment, oldest documents first;
    stops once it has found more than five, since a person has to choose anyway."""
    oldest = sorted(docs, key=lambda d: (d.date, d.ref))[:20]
    found: list[list[_Document]] = []
    for size in range(2, MAX_SET + 1):
        for chosen in combinations(oldest, size):
            if sum((d.open for d in chosen), ZERO) == amount:
                found.append(list(chosen))
                if len(found) > 5:
                    return found
    return found
```

Replace `_pay` with:

```python
def _pay(n: int, line: Transaction, docs: list[_Document], change: dict, found: list[Issue]) -> None:
    """Applies the bank line to documents, oldest first: each takes what is open on it and the last takes
    the rest, so a part payment or an overpayment shows on it. The line then posts against the account
    holding what was owed, with no VAT: the VAT was booked with the document."""
    docs = sorted(docs, key=lambda d: (d.date or dt.date.max, d.ref))
    owed_before, left, pays = sum((d.open for d in docs), ZERO), line.gross, []
    for k, doc in enumerate(docs):
        take = left if k == len(docs) - 1 else min(doc.open, left)
        doc.open, left = doc.open - take, left - take
        pays.append(doc.settlement(take))
        doc.paid_by.append(Settlement(ref=line.document_ref or f"row-{n}", amount=take, date=line.date,
                                      description=line.description))
    still_owed = owed_before - line.gross
    if still_owed > ZERO:
        found.append(issue("part_payment", f"Paid {_gbp(line.gross)} of {_gbp(owed_before)}. "
                                           f"{_gbp(still_owed)} still owed."))
    elif still_owed < ZERO:
        found.append(issue("overpayment", _overpaid(line, docs[-1], -still_owed)))
    change.update(paid_against=docs[0].account, pays=pays, vat_posted=ZERO, net=line.gross)


def _overpaid(line: Transaction, doc: _Document, excess: Decimal) -> str:
    whom = {DEBTORS: "the customer", STAFF_EXPENSES: "the employee"}.get(doc.account, "the supplier")
    if line.direction == Direction.IN:
        return f"Received {_gbp(excess)} more than owed. You now owe {whom} {_gbp(excess)}."
    return f"Paid {_gbp(excess)} more than owed. {whom[0].upper()}{whom[1:]} now owes you {_gbp(excess)}."
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q tests/test_matching.py`
Expected: all pass (Tasks 5, 6 and 7).

- [ ] **Step 5: Commit (if agreed)**

```bash
git add ledgersync/matching.py tests/test_matching.py
git commit -m "Matching: several documents, part payments, overpayments and suggestions"
```

---

### Task 8: Posting matched payments, and the API

**Files:**
- Modify: `ledgersync/posting.py` (imports, `journal_for`, `trial_balance`)
- Modify: `server.py` (import, `validate_transactions`)
- Test: `tests/test_posting.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `matching.match`, `Transaction.paid_against` (Tasks 6–7), `checks.booked` (Task 2).
- Produces: `POST /api/transactions/validate` returns the matched rows; `POST /api/trial-balance` posts them, and answers 422 while a `choose_payment` is unresolved.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_posting.py`:

```python
def bank_line(gross, who, day, **kw):
    return Transaction(direction="out", gross=gross, account_code="7100", description=who, counterparty=who,
                       date=dt.date(2026, 9, day), document_type="statement", document_ref=f"line-{day}", **kw)


def test_a_bank_line_that_pays_a_bill_clears_creditors_and_counts_the_rent_once():
    rent = tx("out", "12000.00", "7100", vat="2000.00", document_type="invoice",
              counterparty="Business Cube Management Solutions", document_ref="bill")
    tb = trial_balance([rent, bank_line("12000.00", "BUSINESS CUBE MGMT", 3)], REGISTERED)
    assert [(l.code, str(l.debit), str(l.credit)) for l in tb.lines] == [
        ("1200", "0.00", "12000.00"), ("2201", "2000.00", "0.00"), ("7100", "10000.00", "0.00")]


def test_a_payment_waiting_for_a_choice_blocks_the_trial_balance():
    bills = [tx("out", "500.00", "7100", document_type="invoice", counterparty="Landmark Properties",
                document_ref=ref) for ref in ("a", "b")]
    with pytest.raises(InvalidTransactions, match="Choose one"):
        trial_balance([*bills, bank_line("500.00", "LANDMARK PROPERTIES", 5)], REGISTERED)
```

Append to `tests/test_api.py`:

```python
def test_validate_matches_a_payment_to_its_bill(make_client):
    bill = {**BT, "document_type": "invoice", "counterparty": "BT Business", "document_ref": "bt-sept"}
    line = {**BT, "date": "2026-09-05", "document_type": "statement", "counterparty": "BT BUSINESS DD",
            "document_ref": "line-1"}
    out_bill, out_line = make_client().post("/api/transactions/validate",
                                            json={"transactions": [bill, line]}).json()["transactions"]
    assert out_line["pays"] == [{"ref": "bt-sept", "amount": "72.00", "date": "2026-09-01", "description": "BT"}]
    assert (out_line["paid_against"], out_line["paid_against_name"]) == ("2100", "Creditors")
    assert (out_bill["owed"], out_bill["contra_account_code"]) == ("0.00", "2100")


def test_trial_balance_waits_for_a_person_to_choose_a_payment(make_client):
    bills = [{**BT, "document_type": "invoice", "counterparty": "BT Business", "document_ref": r} for r in "ab"]
    line = {**BT, "date": "2026-09-05", "document_type": "statement", "counterparty": "BT BUSINESS",
            "document_ref": "l"}
    resp = make_client().post("/api/trial-balance", json={"transactions": [*bills, line]})
    assert resp.status_code == 422 and "Choose one" in resp.json()["detail"]["message"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_posting.py tests/test_api.py`
Expected: the 4 new tests FAIL: the trial balance shows Rent twice, nothing blocks it, and the validate response has no `pays` field filled in. The known `test_api` failure is still listed.

- [ ] **Step 3: Implement**

In `ledgersync/posting.py`, add `from .matching import match` to the imports. Replace `journal_for` with:

```python
def journal_for(tx: Transaction, index: int) -> list[JournalLine]:
    """Journal lines for one transaction after checks.normalise and matching.match: the account and VAT
    legs, then the bank (or contra) leg. A bank line that pays a document posts against the account
    holding what was owed (paid_against); its VAT is nil, because it was booked with the document."""
    code = tx.paid_against or tx.account_code
    account = BY_CODE[code]
    vat_account = SALES_VAT if account.type == AccountType.INCOME else PURCHASE_VAT
    out = tx.direction == Direction.OUT
    lines = [JournalLine(transaction=index, code=leg, description=tx.description,
                         debit=amount if out else ZERO, credit=ZERO if out else amount)
             for leg, amount in ((code, tx.net), (vat_account, tx.vat_posted)) if amount]
    lines.append(JournalLine(transaction=index, code=tx.contra_account_code, description=tx.description,
                             debit=ZERO if out else tx.gross, credit=tx.gross if out else ZERO))
    debits, credits = sum(l.debit for l in lines), sum(l.credit for l in lines)
    if debits != credits:   # net + vat == gross by construction; this guards the invariant
        raise AssertionError(f"transaction {index} does not balance: {debits} != {credits}")
    return lines
```

In `trial_balance`, change the first line to:

```python
    ready = match([normalise(tx, settings) for tx in transactions], settings)
```

In `server.py`, add `from ledgersync.matching import match` to the imports. Replace `validate_transactions` with:

```python
    @app.post("/api/transactions/validate", response_model=TransactionList)
    def validate_transactions(request: LedgerRequest):
        """Splits VAT, works out the other side of each row, matches bank lines to the documents they pay
        and lists issues; no posting."""
        ready = [normalise(tx, request.settings) for tx in request.transactions]
        return TransactionList(transactions=match(ready, request.settings))
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q`
Expected: only the 5 known failures.

- [ ] **Step 5: Commit (if agreed)**

```bash
git add ledgersync/posting.py server.py tests/test_posting.py tests/test_api.py
git commit -m "Post matched payments against what they settle; the API matches on validate and trial balance"
```

---

### Task 9: The table: status line, Link, Unlink, Include, totals

**Files:**
- Modify: `frontend/src/lib/api.ts` (`Settlement`; optional fields on `Transaction`)
- Modify: `frontend/src/lib/duplicates.ts` (`PaymentRow.kind`; the pair rule)
- Modify: `frontend/src/app/page.tsx` (helpers, refresh effect, decisions, row markup, totals, error message)
- Modify: `frontend/src/app/globals.css` (status-line styles)

**Interfaces:**
- Consumes: the JSON fields from Tasks 2, 6 and 8: `document_type`, `counterparty`, `document_ref`, `include`, `link`, `paid_against`, `paid_against_name`, `pays`, `candidates`, `owed`, `paid_by`.
- Produces: no new exports beyond the `Settlement` type and `PaymentRow.kind`.

There is no frontend test runner, so this task is checked with the type checker and in the browser (Steps 6–7).

- [ ] **Step 1: Types in `frontend/src/lib/api.ts`**

Before `export interface Transaction`, add:

```ts
export interface Settlement {
  ref: string
  amount: string
  date: string | null
  description: string
}
```

In `Transaction`, after `issues: Issue[]`, add:

```ts
  document_type?: string             // receipt, invoice, expense_claim, statement, or one that is not a transaction
  counterparty?: string | null
  document_ref?: string | null       // shared by the rows of one document
  include?: boolean                  // a person's tick on a document that is not a transaction
  link?: string[] | null             // a person's decision on a bank line: null automatic, [] none, refs: pays these
  paid_against?: string | null       // set by the API: the account a matched bank line settles
  paid_against_name?: string | null
  pays?: Settlement[]                // set by the API on a bank line
  candidates?: Settlement[][]        // set by the API: options for the Link buttons
  owed?: string | null               // set by the API on a document's row: what is still open on it
  paid_by?: Settlement[]             // set by the API on a document's row
```

- [ ] **Step 2: The duplicate rule in `frontend/src/lib/duplicates.ts`**

In `PaymentRow`, after `date: string | null`, add:

```ts
  kind: 'document' | 'bank' | 'other' | 'not booked'   // payment matching pairs a document with a bank line
```

In `possibleDuplicates`, replace the body of `rows.map(...)` with:

```ts
  return rows.map((row, i) => {
    if (row.kind === 'not booked') return null
    for (let j = 0; j < i; j++) {
      const other = rows[j]
      if (other.sourceId === row.sourceId || other.direction !== row.direction
          || Number(other.gross) !== Number(row.gross) || other.kind === 'not booked') continue
      // A bill and the bank line that pays it are not duplicates: matching links them.
      if ((row.kind === 'document' && other.kind === 'bank') || (row.kind === 'bank' && other.kind === 'document')) continue
      if (row.date === null || other.date === null) {
        if (row.date === other.date) return j
        continue
      }
      if (Math.abs(Date.parse(row.date) - Date.parse(other.date)) <= DUPLICATE_WINDOW_DAYS * DAY_MS) return j
    }
    return null
  })
```

Replace the comment above `possibleDuplicates` with:

```ts
// For each row, the index of an earlier row from another input that it may duplicate: the same
// amount and direction, dated within DUPLICATE_WINDOW_DAYS (or both undated); otherwise null.
// Rows of one input are not compared: two equal payments on one statement are usually real.
// Rows that are not booked are never flagged, and a document is never paired with a bank line:
// payment matching links those.
```

- [ ] **Step 3: Helpers, refresh and decisions in `frontend/src/app/page.tsx`**

Add `type Settlement` to the `@/lib/api` import. After the `issueSummary` helper, add:

```tsx
const NOT_TRANSACTIONS: Record<string, string> = {
  quote: 'a quote', pro_forma: 'a pro forma invoice', purchase_order: 'a purchase order',
  remittance_advice: 'a remittance advice', supplier_statement: "a supplier's statement of account",
  other: 'a document that is not a transaction',
}

// How a row is booked: a document holding what is owed, a bank line, anything else paid when it
// happened, or not booked at all (a quote or the like, until a person ticks Include).
const kindOf = (tx: Transaction): 'document' | 'bank' | 'other' | 'not booked' => {
  const type = tx.document_type ?? 'receipt'
  if (type in NOT_TRANSACTIONS) return tx.include ? 'document' : 'not booked'
  if (type === 'invoice' || type === 'expense_claim') return 'document'
  return type === 'statement' ? 'bank' : 'other'
}

const shortDate = (iso: string | null) =>
  iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : 'no date'

const optionLabel = (option: Settlement[]) =>
  option.map(s => `${s.description} (${money(s.amount)}, ${shortDate(s.date)})`).join(' + ')
```

In the component's state block (Step 2 state), add:

```tsx
  const [rowsVersion, setRowsVersion] = useState(0)     // bumped when rows arrive or a person decides
  const [matchError, setMatchError] = useState('')
  const rowsRef = useRef<LedgerRow[]>([])
  rowsRef.current = rows
```

In `addRows`, after `setTbError('')`, add `setRowsVersion(v => v + 1)`.

After `addRows`, add:

```tsx
  // After rows arrive, or a person links, unlinks or includes, the API works out which bank lines pay
  // which documents across the whole table. A reply that comes back after the rows changed again is
  // dropped: the newer change sends its own.
  useEffect(() => {
    const sent = rowsRef.current
    if (!rowsVersion || !sent.length) return
    let current = true
    validateTransactions(sent.map(row => row.tx))
      .then(({ transactions }) => {
        if (!current) return
        setRows(sent.map((row, i) => ({ ...row, tx: transactions[i] })))
        setMatchError('')
      })
      .catch((e: unknown) => { if (current) setMatchError(e instanceof Error ? e.message : String(e)) })
    return () => { current = false }
  }, [rowsVersion])

  // A person's decisions travel on the rows, so working the matches out again never overturns them.
  const decide = useCallback((index: number, change: Partial<Transaction>) => {
    setRows(prev => prev.map((row, i) => (i === index ? { ...row, tx: { ...row.tx, ...change } } : row)))
    setRowsVersion(v => v + 1)
    setTbResult(null)
  }, [])

  const includeDocument = useCallback((ref: string | null | undefined, include: boolean) => {
    setRows(prev => prev.map(row => (ref && row.tx.document_ref === ref ? { ...row, tx: { ...row.tx, include } } : row)))
    setRowsVersion(v => v + 1)
    setTbResult(null)
  }, [])
```

In `clearTable`, add `setMatchError('')`.

- [ ] **Step 4: Status line, posting account and totals in `page.tsx`**

Replace the `duplicateOf` memo's row mapping with:

```tsx
  const duplicateOf = useMemo(() => possibleDuplicates(rows.map(row => ({
    sourceId: row.sourceId, gross: row.tx.gross, direction: row.tx.direction, date: row.tx.date,
    kind: kindOf(row.tx),
  }))), [rows])
```

Replace the `totals` memo, including its comment, with:

```tsx
  // Totals by how rows are booked, in whole pennies so they don't drift: money that moved through the
  // bank, and documents owed by you or to you. An unpaid bill is not money out, so the two are kept
  // apart; rows not booked are left out. A row with an error (impossible VAT) has no VAT or net, so its
  // group's VAT and net totals are unknown.
  const totals = useMemo(() => {
    const moved = (row: LedgerRow) => ['bank', 'other'].includes(kindOf(row.tx))
    const groups = [
      { key: 'bank-out', label: 'Paid out (bank)', keep: (r: LedgerRow) => moved(r) && r.tx.direction === 'out' },
      { key: 'bank-in', label: 'Received (bank)', keep: (r: LedgerRow) => moved(r) && r.tx.direction === 'in' },
      { key: 'owed-by-you', label: 'Owed by you', keep: (r: LedgerRow) => kindOf(r.tx) === 'document' && r.tx.direction === 'out' },
      { key: 'owed-to-you', label: 'Owed to you', keep: (r: LedgerRow) => kindOf(r.tx) === 'document' && r.tx.direction === 'in' },
    ]
    return groups.flatMap(({ key, label, keep }) => {
      const txs = rows.filter(keep).map(row => row.tx)
      const sum = (field: 'gross' | 'vat_posted' | 'net') => txs.some(tx => tx[field] == null) ? null
        : (txs.reduce((pennies, tx) => pennies + Math.round(Number(tx[field]) * 100), 0) / 100).toFixed(2)
      return txs.length ? [{ key, label, count: txs.length, gross: sum('gross'), vat: sum('vat_posted'), net: sum('net') }] : []
    })
  }, [rows])
```

Just before `return (` of the component, add:

```tsx
  // What is still owed on a document, or how it was paid.
  const owedLine = (tx: Transaction) => {
    if (tx.owed == null) return null
    const owed = Number(tx.owed)
    const who = tx.counterparty ?? 'them'
    const text = !tx.paid_by?.length
      ? (tx.contra_account_code === '1100' ? `Unpaid, owed by ${who}` : `Unpaid, owed to ${who}`)
      : owed > 0 ? `Part paid: ${money(tx.owed)} still owed`
      : owed < 0 ? `Overpaid by ${money(String(-owed))}`
      : `Paid by bank line on ${shortDate(tx.paid_by[tx.paid_by.length - 1].date)}`
    return <div className="row-status">{text}</div>
  }

  // The status under a row's description, with the person's choices: Include, Link, Unlink.
  const statusLine = (tx: Transaction, index: number) => {
    const type = tx.document_type ?? 'receipt'
    if (type in NOT_TRANSACTIONS) {
      return (
        <>
          <label className="row-status">
            <input type="checkbox" checked={!!tx.include}
                   onChange={e => includeDocument(tx.document_ref, e.target.checked)} />
            {tx.include ? 'Included as an invoice'
              : `Looks like ${NOT_TRANSACTIONS[type]}, not booked. Tick to include it as an invoice`}
          </label>
          {tx.include && owedLine(tx)}
        </>
      )
    }
    if (kindOf(tx) === 'document') return owedLine(tx)
    if (kindOf(tx) !== 'bank') return null
    if (tx.pays?.length) {
      return (
        <div className="row-status">
          Pays: {optionLabel(tx.pays)}
          <button className="link-btn" onClick={() => decide(index, { link: [] })}>Unlink</button>
        </div>
      )
    }
    if (tx.candidates?.length) {
      const choosing = tx.issues.some(i => i.code === 'choose_payment')
      return (
        <div className="row-status">
          {choosing ? 'Could pay:' : 'May pay:'}
          {tx.candidates.map((option, k) => (
            <button key={k} className="link-btn" onClick={() => decide(index, { link: option.map(s => s.ref) })}>
              Link {optionLabel(option)}
            </button>
          ))}
        </div>
      )
    }
    if (tx.link && !tx.link.length) {
      return (
        <div className="row-status">
          Not matched to a document
          <button className="link-btn" onClick={() => decide(index, { link: null })}>Match automatically</button>
        </div>
      )
    }
    return null
  }
```

In the table body:
- Change `<tr key={i}>` to `<tr key={i} className={kindOf(tx) === 'not booked' ? 'row-not-booked' : undefined}>`.
- After `<div className="row-origin" title={origin}>{origin}</div>`, add `{statusLine(tx, i)}`.
- Replace the Account cell `<td>{tx.account_code} {tx.account_name ?? ''}</td>` with:

```tsx
                            <td>
                              {tx.paid_against
                                ? `${tx.paid_against} ${tx.paid_against_name ?? ''}`
                                : `${tx.account_code} ${tx.account_name ?? ''}`}
                            </td>
```

In the `<tfoot>`, change `key={t.direction}` to `key={t.key}`, and the label cell to:

```tsx
                            <td colSpan={3}>{t.label} ({t.count} row{t.count === 1 ? '' : 's'})</td>
```

Just above `<div className="data-table-wrap">` in Step 2, add:

```tsx
                {matchError && (
                  <div className="status-msg status-warning">⚠ Could not match payments to documents: {matchError}</div>
                )}
```

- [ ] **Step 5: Styles in `frontend/src/app/globals.css`**

After the `.row-origin` rule, add:

```css
.row-status {
  margin-top: 0.2rem; font-size: 0.72rem; color: var(--text-secondary);
  display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem;
}
.row-status input { accent-color: var(--accent); }
.link-btn {
  background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--accent);
  padding: 0.05rem 0.45rem; font-size: 0.72rem; font-family: inherit; cursor: pointer;
}
.link-btn:hover { border-color: var(--accent); }
.row-not-booked td { color: var(--text-muted); }
```

- [ ] **Step 6: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no output (no errors).

Run `npm run build` only if no `next dev` server is running (`lsof -nP -iTCP:3000 -sTCP:LISTEN` prints nothing). A build disturbs a running dev server.

- [ ] **Step 7: Check in the browser**

Restart the API so it runs the new code, and start the UI. Use `preview_start` with the `api` and `web` entries in `.claude/launch.json`; if the user's own servers are running, ask them to restart the API. Then make two Groq calls by pasting two inputs one after the other:
1. `Business Cube Management Solutions Limited invoice 202600000822 dated 01/10/2026: October 2026 rent at 17 Bevis Marks, £10,000.00 plus VAT £2,000.00, total £12,000.00, due 15/10/2026.`
2. `Bank statement line 03/10/2026 BUSINESS CUBE MGMT SOL £12,000.00 paid out.`

Check each of these:
- The invoice row says "Unpaid, owed to Business Cube …" after paste 1, and "Paid by bank line on 3 Oct" after paste 2.
- The bank line says "Pays: … [Unlink]" and its Account column shows "2100 Creditors".
- Clicking **Unlink** turns the bank line into "Not matched to a document". The invoice is unpaid again.
- Clicking **Match automatically** pairs them again.
- The totals show "Owed by you" and "Paid out (bank)".
- **Generate Trial Balance** shows 7100 Rent £10,000.00, 2201 Purchase VAT £2,000.00 and 1200 Bank £12,000.00 credit, with no 2100 line.

Take a screenshot, check the console for errors, and run `preview_stop` on the servers you started.

- [ ] **Step 8: Commit (if agreed)**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/duplicates.ts frontend/src/app/page.tsx frontend/src/app/globals.css
git commit -m "Table: what each row pays or owes, with Link, Unlink and Include; totals by bank and documents"
```

---

### Task 10: The eval scores document types

**Files:**
- Modify: `eval/eval_cases.py` (`Case.document_type`; `PRO_FORMA` and `SUPPLIER_STATEMENT`; the `CASES` tuple)
- Modify: `eval/make_fixtures.py`, `eval/eval_scoring.py`, `eval/run_eval.py`
- Regenerate: `eval/fixtures/`
- Test: `tests/test_eval_fixtures.py`, `tests/test_eval_scoring.py`, `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `models.DOCUMENT_TYPES` (Task 2), and `document_type` in the API's rows (Tasks 2–4).
- Produces:
  - `"document_type"` on every expected row in `eval/fixtures/*/expected.json`.
  - `eval_scoring.FIELDS` includes `"document_type"`.
  - The run summary prints a `type` column.

- [ ] **Step 1: Write the failing tests**

In `tests/test_eval_fixtures.py`:
- Add `from ledgersync.models import DOCUMENT_TYPES` to the imports.
- In `test_there_are_31_cases_covering_every_input_kind`, rename the test to `test_there_are_33_cases_covering_every_input_kind` and change `31` to `33`.
- In `test_fixture_is_readable_and_well_formed`, add inside the row loop:

```python
        assert row["document_type"] in DOCUMENT_TYPES and row["document_type"] != "other"
```

Append to `tests/test_eval_scoring.py`:

```python
def test_the_document_type_is_scored_when_both_sides_have_it():
    expected = expected_rows([{"date": "2026-09-03", "direction": "out", "gross": "12.50", "vat": None,
                               "account_code": "7406", "description": "Tesco", "document_type": "receipt"}])
    right = adapt_rows({"transactions": [dict(api_row(12.50), document_type="receipt")]})
    wrong = adapt_rows({"transactions": [dict(api_row(12.50), document_type="invoice")]})
    assert score_case(expected, right)["checks"]["document_type"] == [1, 1]
    assert score_case(expected, wrong)["checks"]["document_type"] == [0, 1]
    assert score_case(expected, adapt_rows({"transactions": [api_row(12.50)]}))["checks"]["document_type"] == [0, 0]
```

In `tests/test_eval_scoring.py`:
- Add `"document_type": [0, 0]` to both `none = {...}` dictionaries.
- Add `"document_type": None` to the `field_accuracy` dictionary in `test_summary_aggregates_rows_fields_latency_and_errors`.

In `tests/test_eval_runner.py`, change the `"checks"` comprehension to iterate over `("amount", "date", "direction", "account", "vat", "document_type")`.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest -q tests/test_eval_fixtures.py tests/test_eval_scoring.py tests/test_eval_runner.py`
Expected: FAIL. There are 31 fixtures with no `document_type`, and `checks` has no `document_type` key.

- [ ] **Step 3: Label the cases in `eval/eval_cases.py`**

In `Case`, after `transactions: tuple[Tx, ...]`, add:

```python
    document_type: str = "statement"   # the kind of document every row comes from (models.DocumentType)
```

Before `CASES`, add:

```python
PRO_FORMA = ["Brightline IT Supplies Ltd", "8 Kings Road, Reading RG1 3AA", "PRO FORMA INVOICE",
             "Pro forma number: PF-0921", "Date: 22/09/2026", "To: Northbridge Consulting Ltd",
             "Dell 24in monitor x2                300.00", "VAT at 20%                           60.00",
             "Total payable in advance            360.00", "This is not a VAT invoice."]

SUPPLIER_STATEMENT = ["Clearway Office Supplies Ltd", "STATEMENT OF ACCOUNT", "Customer: Northbridge Consulting Ltd",
                      "Statement date: 30/09/2026", "08/09/2026  Invoice CW-20931            300.00",
                      "Balance due                           300.00"]
```

In `CASES`, add `document_type=...` as the last argument of these cases. All the others keep the default, `"statement"`:

| Case | `document_type` |
|---|---|
| `text-tesco-receipt`, `img-tesco-receipt`, `img-cafe-receipt-vat`, `img-fuel-receipt-rotated`, `img-train-ticket`, `img-supermarket-mixed-vat`, `text-mixed-receipt-one-vat` | `"receipt"` |
| `text-expense-note-mixed` | `"expense_claim"` |
| `pdf-purchase-invoice`, `pdf-paid-invoice`, `pdf-sales-invoice`, `pdf-scanned-invoice` | `"invoice"` |

For example, the first case becomes:

```python
    Case("text-tesco-receipt", "text", "input.txt", _text(TESCO), T_TESCO, document_type="receipt"),
```

and `pdf-sales-invoice` becomes:

```python
    Case("pdf-sales-invoice", "pdf", "input.pdf", lambda: _text_pdf(SALES_INVOICE),
         (Tx("2026-09-30", "Harbour & Lane Architects LLP", "2400.00", "4000", vat="400.00"),),
         document_type="invoice"),
```

After `pdf-bank-statement`, add the two new cases:

```python
    # Documents that are not transactions: their amounts are listed for a person to check, not booked.
    Case("pdf-pro-forma", "pdf", "input.pdf", lambda: _text_pdf(PRO_FORMA),
         (Tx("2026-09-22", "Brightline IT Supplies Ltd", "-360.00", "0030", vat="60.00"),), document_type="pro_forma"),
    Case("pdf-supplier-statement", "pdf", "input.pdf", lambda: _text_pdf(SUPPLIER_STATEMENT),
         (Tx("2026-09-08", "Clearway Office Supplies Ltd", "-300.00", "7504"),), document_type="supplier_statement"),
```

- [ ] **Step 4: Write, score and print the type**

In `eval/make_fixtures.py`, change the `"rows"` entry to:

```python
                    "rows": [{**tx.expected, "document_type": case.document_type} for tx in case.transactions]}
```

In `eval/eval_scoring.py`:
- Set `FIELDS = ("amount", "date", "direction", "account", "vat", "document_type")`.
- In `adapt_rows`, add these two entries to the row dict:

```python
        "document_type": row.get("document_type"),
        "has_type": "document_type" in row,
```

- In `score_case`, after the `vat` check, add:

```python
        if e.get("document_type") and p.get("has_type"):
            check("document_type", p["document_type"] == e["document_type"])
```

In `eval/run_eval.py`, in `_print_summary`:
- Add `"type"` after `"vat"` in `head`.
- Add `f["document_type"]` after `f["vat"]` in `cells`.

Then regenerate the fixtures:

Run: `.venv/bin/python eval/make_fixtures.py`
Expected: `Wrote 33 fixtures to …/eval/fixtures`. This also rewrites the PDF and Excel inputs, whose bytes may change (creation dates) though their content does not.

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest -q`
Expected: only the 5 known failures.

- [ ] **Step 6: Run the affected eval cases against Groq**

Start the API with the eval conditions using `preview_start` with `api-groq-eval-8087`, an existing entry in `.claude/launch.json` with business name Northbridge Consulting Ltd, low thinking and 1 image per request. Then run each group into one report, with `--resume` after the first:

```bash
.venv/bin/python eval/run_eval.py --api http://127.0.0.1:8087 --label document-types --cases pdf- --pause 15 --note "document types and payment matching; reasoning low; 1 image; Northbridge"
```

Repeat with `--resume` for `--cases receipt`, `--cases note`, `--cases img-`, `--cases xlsx` and `--cases csv-barclays-probe`.

Expected: correct rows 1.0 on every case, as on 2026-10-02. The new `type` column reports document-type accuracy; report it to the user, including any case where the type is wrong. Stop the server afterwards.

- [ ] **Step 7: Commit (if agreed)**

```bash
git add eval/ tests/test_eval_fixtures.py tests/test_eval_scoring.py tests/test_eval_runner.py
git commit -m "Eval: score each row's document type; add a pro forma and a supplier statement"
```

---

### Task 11: README

**Files:**
- Modify: `README.md` (after the paragraph that starts "The ledger endpoints use double entry")

- [ ] **Step 1: Describe the behaviour**

Add this section:

```markdown
### Unpaid bills, claims and payments

Documents are booked the way an accountant would (accruals):

- A till or card receipt, or a bank line, is money that moved: it posts against 1200 Bank Current Account.
- An invoice starts unpaid. A bill, or a supplier's credit note, posts against 2100 Creditors; a sales
  invoice, or a credit note to a customer, against 1100 Debtors; an expense claim against 2110 Expenses
  Owed to Staff.
- A bank line that pays an open invoice or claim clears it instead of being booked as a second expense:
  the same counterparty, dated on or after the document and at most 31 days later. One payment can clear
  up to five documents from one counterparty. When several could be meant, the table asks you to choose
  (Link) and the trial balance waits. Part payments and overpayments are applied and flagged; a payment
  of the same amount under a different name is only suggested. Unlink undoes a match.
- Quotes, pro formas, purchase orders, remittance advices and supplier statements are not booked unless
  you tick Include.

`POST /api/transactions/validate` and `POST /api/trial-balance` take `document_type`, `counterparty`,
`document_ref`, `link` and `include` on each row, and return `paid_against`, `pays`, `candidates`, `owed`
and `paid_by`. Rows without a `document_type` are receipts, so older clients keep their postings. The
design is in `docs/superpowers/specs/2026-10-06-unpaid-documents-and-payment-matching-design.md`.
```

- [ ] **Step 2: Commit (if agreed)**

```bash
git add README.md docs/superpowers/specs/2026-10-06-unpaid-documents-and-payment-matching-design.md docs/superpowers/plans/2026-10-06-unpaid-documents-and-payment-matching.md
git commit -m "README: unpaid bills, claims and payment matching"
```
